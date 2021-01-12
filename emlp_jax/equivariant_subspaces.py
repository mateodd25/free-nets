import numpy as np
#import torch
import jax
from jax import jit
from functools import partial
import jax.numpy as jnp
from jax import device_put
import collections,itertools
from functools import lru_cache as cache
from emlp_jax.utils import disk_cache
import scipy as sp
import scipy.linalg
import functools
import random
from scipy.sparse.linalg import LinearOperator

class OrderedCounter(collections.Counter,collections.OrderedDict): pass

def repsize(ranks,d):
    return sum(d**(p+q) for (p,q) in ranks)

class TensorRep(object):
    def __init__(self,ranks,G=None,shapes=None):
        """ Constructs a tensor type based on a list of tensor ranks
            and possibly the symmetry generators gen."""
        self.ranks = ranks
        self.shapes = shapes or (self.ranks,)
        self.G=G
        self.d = self.G.d if self.G is not None else None
        self.__name__=self.__class__

    def __eq__(self, other):
        return len(self)==len(other) and all(r==rr for r,rr in zip(self.ranks,other.ranks))
    def __len__(self):
        return len(self.ranks)
    @property
    def shape(self):
        return tuple(repsize(r, self.d) for r in self.shapes)
    def size(self):
        if not self.d: return None
        return repsize(self.ranks,self.d)
    def __call__(self,G):
        self.G=G
        self.d = self.G.d
        return self
    def __add__(self, other):
        if isinstance(other,int): return self+other*Scalar
        return TensorRep(self.ranks+other.ranks,self.G if self.G is not None else other.G)
    def __radd__(self, other):
        if isinstance(other,int): return other*Scalar+self
        else: assert False, f"Unsupported operand Rep*{type(other)}"
    def __mul__(self, other):
        if isinstance(other,int): return TensorRep(other*self.ranks,self.G)
        elif isinstance(other,TensorRep):
            product_ranks = [(r1[0]+r2[0],r1[1]+r2[1]) for r1,r2 in itertools.product(self.ranks,other.ranks)]
            return TensorRep(product_ranks,self.G if self.G is not None else other.G,self.shapes+other.shapes)
        else: assert False, f"Unsupported operand Rep*{type(other)}"

    def __rmul__(self, other):
        if isinstance(other, int): return TensorRep(other * self.ranks, self.G)
        else: assert False, f"Unsupported operand Rep*{type(other)}"
    # def __iter__(self):
    #     return iter(self.ranks)
    @property
    def T(self):
        """ only swaps to adjoint representation, does not reorder elems"""
        return TensorRep([rank[::-1] for rank in self.ranks], self.G)

    def __matmul__(self, other):
        raise NotImplementedError

    def multiplicities(self):
        if self.G is not None and self.G.is_unimodular(): # orthogonal subgroup-> collapse T(p,q) to T(p+q)
            return OrderedCounter((p+q,0) for (p,q) in self.ranks)
        return OrderedCounter(self.ranks)

    def __repr__(self):
        multiplicities=  self.multiplicities()
        tensors = "+".join(f"{v if v > 1 else ''}T{key}" for key, v in multiplicities.items())
        if self.G is not None and self.G.is_unimodular():
            tensors = "+".join(f"{v if v > 1 else ''}T({q})" for (q,p), v in multiplicities.items())
        return tensors+f" @ d={self.d}" if self.d is not None else tensors
    def __str__(self):
        return repr(self)
    def __hash__(self):
        return hash(tuple(tuple(ranks) for ranks in self.shapes))
    def __eq__(self,other):
        return (tuple(ranks)==tuple(other_ranks) for ranks,other_ranks in zip(self.shapes,other.shapes)) and (self.G==other.G)
    def symmetric_subspace(self):
        dims,lazy_projection = get_active_subspaces(self.G,self)
        if len(self.shape)==1:
            return dims,lazy_projection
        else:
            perm = rep_permutation(self)
            return dims, lambda t: lazy_projection(t)[perm].reshape(*self.shape)

    def symmetric_projection(self):
        lazy_projection = get_QQT(self.G,self)
        if len(self.shape)==1:
            return lazy_projection
        else:
            perm = rep_permutation(self)
            invperm = jnp.argsort(perm)
            return lambda t: lazy_projection(t.reshape(-1)[invperm])[perm].reshape(*self.shape)

    def show_subspace(self):
        dims,projection = self.symmetric_subspace()
        vals = projection(jnp.arange(dims)+1)
        out = jnp.where(jnp.abs(vals)>1e-7,vals,jnp.zeros_like(vals)).reshape(*self.shape)
        if len(self.ranks)==1: return out.reshape(*[self.d for _ in range(self.ranks[0][0]+self.ranks[0][1])])
        return out

    def rho(self,G):
        return sp.linalg.block_diag(*[rho(G,rank) for rank in self.ranks])

    def drho(self,A):
        return sp.linalg.block_diag(*[drho(A,rank) for rank in self.ranks])

    def argsort(self):
        """ get the permutation given by converting
            from the order in ranks to the order when the ranks are grouped by
            first occurrence of a given type (p,q). (Bucket sort)"""
        ranks_indices = collections.OrderedDict(((rank, []) for rank in self.multiplicities()))
        i=0
        for (p,q) in self.ranks:
            tensor_size = self.d**(p+q)
            rrank = (p+q,0) if self.G.is_unimodular() else (p,q)
            ranks_indices[rrank].append(jnp.arange(tensor_size)+i)
            i+= tensor_size
        permutation = jnp.concatenate([jnp.concatenate([idx for idx in indices]) for indices in ranks_indices.values()])
        return permutation


def T(p,q=0,G=None):
    return TensorRep([(p,q)],G=G)


Scalar = T(0,0)
Vector = T(1,0)
Matrix = T(1,1)
Quad = T(0,2)

def size(rank,d):
    p,q = rank
    return d**(p+q)

@partial(jit,static_argnums=(1,))
def rho(G,rank):
    p,q = rank
    Gp = functools.reduce(jnp.kron,p*[G],1)
    GpGinvTq = functools.reduce(jnp.kron,q*[jnp.linalg.inv(G).T],Gp) # shouldn't this be backwards?
    return GpGinvTq

@partial(jit,static_argnums=(1,))
def drho(M,rank):
    """ Returns the Lie Algebra representation drho(M) of a matrix M
        acting on a rank (p,q) tensor.
        Inputs: [M (d,d)] [rank tuple(p,q)]
        Outputs: [drho(M) (d**(p+q),d**(p+q))]"""
    p,q = rank
    d=M.shape[0]
    rep_M = 0
    Ikron_powers = [1]
    for _ in range(p+q-1):
        Ikron_powers.append(jnp.kron(Ikron_powers[-1],jnp.eye(d)))
    for r in range(1,p+1):
        rep_M += jnp.kron(jnp.kron(Ikron_powers[r-1],M),Ikron_powers[p-r+q])
    for s in range(1,q+1):
       rep_M -= jnp.kron(jnp.kron(Ikron_powers[p+s-1],M.T),Ikron_powers[q-s])
    return rep_M

class rho_lazy(LinearOperator):
    def __init__(self,M,rank):
        self.d = M.shape[0]
        self.M = M
        self.rank = rank
        self.c = size(rank,self.d)
        self.dtype=np.float64
    @property
    def shape(self):
        return (self.c,self.c)
    def _matmat(self,V): #(c,k)
        c,k = V.shape
        p,q = self.rank
        eV = V.reshape((p+q)*[self.d]+[k])
        for i in range(p):
            eV = np.moveaxis(np.dot(self.M,eV),0,i)
        for i in range(p,p+q):
            eV = np.moveaxis(np.dot(np.linalg.inv(self.M.T),eV),0,i)
        return eV.reshape(*V.shape)

class drho_lazy(LinearOperator):
    def __init__(self,M,rank):
        self.d = M.shape[0]
        self.M = M
        self.rank = rank
        self.c = size(rank,self.d)
        self.dtype=np.float64
    @property
    def shape(self):
        return (self.c,self.c)
    def _matmat(self,V): #(c,k)
        c,k = V.shape
        p,q = self.rank
        eV = V.reshape((p+q)*[self.d]+[k])
        out = np.zeros_like(eV)
        for i in range(p):
            out += np.moveaxis(np.dot(self.M,eV),0,i)
        for i in range(p,p+q):
            out -= np.moveaxis(np.dot(self.M.T,eV),0,i)
        return out.reshape(*V.shape)
    def _adjoint(self):
        return drho_lazy(self.M.T,self.rank)

#@partial(jit,static_argnums=(0,1))
def projection_matrix(group,rank):
    """ Given a sequence of exponential generators [A1,A2,...]
        and a tensor rank (p,q), the function concatenates the representations
        [drho(A1), drho(A2), ...] into a single large projection matrix.
        Input: [generators seq(tensor(d,d))], [rank tuple(p,q)], [d int] """
    constraints = []
    constraints.extend([drho(device_put(A),rank) for A in group.lie_algebra])
    constraints.extend([rho(device_put(h),rank)-np.eye(size(rank,group.d)) for h in group.discrete_generators])
    P = jnp.concatenate(constraints,axis=0) if constraints else jnp.zeros((1,size(rank,group.d)))
    return P

def orthogonal_complement(proj):
    """ Computes the orthogonal complement to a given matrix proj"""
    U,S,VT = jnp.linalg.svd(proj,full_matrices=True) # Changed from full_matrices=True
    rank = (S>1e-5).sum()
    return VT[rank:]

#@disk_cache('_subspace_cache_jax.dat')
@cache()
def get_active_subspace(group,rank):
    """ Given an array of generators [M1,M2,...] and tensor rank (p,q)
        this function computes the orthogonal complement to the projection
        matrix formed by stacking the rows of drho(Mi) together.
        Output [Q (r,) + (p+q)*(d,)] """
    #from emlp.groups import Trivial
    if rank ==(0,0): return jnp.ones((1,1))
    #if isinstance(group,Trivial): return np.eye(size(rank,group.d))
    P = projection_matrix(group,rank)
    Q = orthogonal_complement(P)
    return Q


#@partial(jit,static_argnums=(0,1))
def get_active_subspaces(group,rep):
    """ Given a representation which is a sequence of tensors
        with ranks (p_i,q_i), computes the orthogonal complement
        to the projection matrix drho(Mi). Function returns both the
        dimension of the active subspace (r) and also a function that
        maps an array of size (*,r) to a vector v with a representaiton
        given by the rnaks that satisfies drho(Mi)v=0 for each i.
        Inputs: [generators seq(tensor(d,d))] [ranks seq(tuple(p,q))]
        Outputs: [r int] [projection (tensor(*,r)->tensor(*,rep_dim))]"""
    rank_multiplicites = rep.multiplicities()
    Qs = {rank:get_active_subspace(group,rank) for rank in rank_multiplicites}
    Qs = {rank:jax.device_put(Q.astype(np.float32)) for rank,Q in Qs.items()}
    active_dims = sum([rank_multiplicites[rank]*Qs[rank].shape[0] for rank in Qs.keys()])
    # Get the permutation of the vector when grouped by tensor rank
    inverse_perm = jnp.argsort(rep.argsort())
    # Apply the projections for each rank, concatenate, and permute back to orig rank order
    def lazy_projection(array):
        i=0
        Ws = []
        for rank, multiplicity in rank_multiplicites.items():
            Qr = Qs[rank]
            i_end = i+multiplicity*Qr.shape[0]
            elems = array[...,i:i_end].reshape(*array.shape[:-1],multiplicity,Qr.shape[0])@Qr
            Ws.append(elems.reshape(*array.shape[:-1],multiplicity*size(rank,rep.d)))
            i = i_end
        Ws = jnp.concatenate(Ws,axis=-1) #concatenate over rep axis
        return Ws[...,inverse_perm] # reorder to original rank ordering
    return active_dims,lazy_projection

# #@partial(jit,static_argnums=(0,1))
# def get_QQT(group,rep):
#     rank_multiplicites = rep.multiplicities()
#     Qs = {rank:get_active_subspace(group,rank) for rank in rank_multiplicites}
#     #Qs = {rank:jax.device_put(Q.astype(np.float32)) for rank,Q in Qs.items()}
#     # Get the permutation of the vector when grouped by tensor rank
#     perm = rep.argsort()
#     invperm = jnp.argsort(perm)
#     # Apply the projections for each rank, concatenate, and permute back to orig rank order
#     def lazy_projection(W):
#         ordered_W = W[perm]
#         PWs = []
#         i=0
#         for rank, multiplicity in rank_multiplicites.items():
#             Qr = Qs[rank]
#             i_end = i+multiplicity*size(rank,rep.d)
#             PWs.append((Qr.T@(Qr@ordered_W[i:i_end].reshape(multiplicity,size(rank,rep.d)).T)).T.reshape(-1))
#             i = i_end
#         PWs = jnp.concatenate(PWs,axis=-1) #concatenate over rep axis
#         return PWs[invperm] # reorder to original rank ordering
#     return lazy_projection

#@partial(jit,static_argnums=(0,1))
def get_QQT(group,rep):
    rank_multiplicites = rep.multiplicities()
    Ps = {rank:group.get_projector(rank) for rank in rank_multiplicites}
    #Qs = {rank:jax.device_put(Q.astype(np.float32)) for rank,Q in Qs.items()}
    # Get the permutation of the vector when grouped by tensor rank
    perm = rep.argsort()
    invperm = jnp.argsort(perm)
    # Apply the projections for each rank, concatenate, and permute back to orig rank order
    def lazy_projection(W):
        ordered_W = W[perm]
        PWs = []
        i=0
        for rank, multiplicity in rank_multiplicites.items():
            P = Ps[rank]
            i_end = i+multiplicity*size(rank,rep.d)
            PWs.append(P(ordered_W[i:i_end].reshape(multiplicity,size(rank,rep.d)).T).T.reshape(-1))
            i = i_end
        PWs = jnp.concatenate(PWs,axis=-1) #concatenate over rep axis
        return PWs[invperm] # reorder to original rank ordering
    return lazy_projection

#@partial(jit,static_argnums=(0,1))
def bilinear_weights(W_rep,x_rep):
    W_multiplicities = W_rep.multiplicities()
    x_multiplicities = x_rep.multiplicities()
    x_multiplicities.pop((0,0),None)#[(0,0)]=0 # Remove scalars
    d = x_rep.d
    nelems = lambda nx,rank: min(nx,size(rank,d))
    active_dims = sum([W_multiplicities[rank]*nelems(n,rank) for rank,n in x_multiplicities.items()])
    # Get the permutation of the vector when grouped by tensor rank
    inverse_perm = jnp.argsort(W_rep.argsort())
    rank_indices_dict = tensor_indices_dict(x_rep)
    reduced_indices_dict = {rank:jnp.concatenate(random.sample(ids,nelems(len(ids),rank)))\
                                for rank,ids in rank_indices_dict.items()}
    # Apply the projections for each rank, concatenate, and permute back to orig rank order
    def lazy_projection(params,x): # (*,r), (bs,c) #TODO: find out why backwards of this function is so slow
        bs = x.shape[0]
        i=0
        Ws = []
        for rank, W_mult in W_multiplicities.items():
            x_mult = x_multiplicities[rank]
            if rank not in x_multiplicities:
                Ws.append(jnp.zeros((bs,W_mult*size(rank,d))))
                continue
            n = nelems(x_mult,rank)
            i_end = i+W_mult*n
            bids =  reduced_indices_dict[rank]
            bilinear_params = params[i:i_end].reshape(W_mult,n)
            i = i_end  # (bs,W_mult,d^r) = (W_mult,n)@(n,d^r,bs)
            bilinear_elems = bilinear_params@x[:,bids].T.reshape(n,size(rank,d)*bs)
            bilinear_elems = bilinear_elems.reshape(W_mult*size(rank,d),bs).T
            Ws.append(bilinear_elems)
        Ws = jnp.concatenate(Ws,axis=-1) #concatenate over rep axis
        return Ws[:,inverse_perm] # reorder to original rank ordering
    return active_dims,lazy_projection

#@partial(jit,static_argnums=(1,))
def tensor_indices_dict(rep):
    index_dict = collections.defaultdict(list)
    i=0
    for (p,q) in rep.ranks:
        rank = (p+q,0) if rep.G.is_unimodular() else (p,q)
        i_end = i+size(rank,rep.d)
        index_dict[rank].append(jnp.arange(i,i_end))
        i = i_end
    return index_dict#{rank:np.concatenate(ids) for rank,ids in index_dict.items()}

@cache()
def rep_permutation(rep):
    arange = np.arange(rep.size())
    size_cumsums = [np.cumsum([0] + [rep.d ** (p + q) for (p, q) in reps]) for reps in rep.shapes]
    permutation = np.zeros([cumsum[-1] for cumsum in size_cumsums]).astype(np.int)
    indices_iter = itertools.product(*[range(len(reps)) for reps in rep.shapes])
    i = 0
    for indices in indices_iter:
        slices = tuple([slice(cumsum[idx], cumsum[idx + 1]) for idx, cumsum in zip(indices, size_cumsums)])
        slice_lengths = [sl.stop - sl.start for sl in slices]
        chunk_size = np.prod(slice_lengths)
        permutation[slices] += arange[i:i + chunk_size].reshape(*slice_lengths)
        i += chunk_size
    return permutation.reshape(-1)

@cache() #TODO: pre add self connections in tensor product before random choice (cause of variance)
def capped_tensor_ids(repin,maxrep):
    """Returns rep and ids for tensor product repin@repin
       but with terms >repin removed """
    product_rep = (repin*repin)
    tensor_multiplicities = product_rep.multiplicities()
    multiplicities = maxrep.multiplicities()
    min_mults = collections.OrderedDict((rank,min(tm,multiplicities[rank]))
                                        for rank,tm in tensor_multiplicities.items())
    # randomly select up to maxrep from each of the tensor ranks
    within_ids = collections.OrderedDict((rank,np.random.choice(v,min(v,multiplicities[rank])))
                                                for rank,v in tensor_multiplicities.items())
    all_ids= []
    i_all = 0
    d = repin.d
    for (p,q),ids in within_ids.items():
        interleaved_ids = (d**(p+q)*ids[:,None]+np.arange(d**(p+q))).reshape(-1)
        all_ids.extend(interleaved_ids+i_all)
        i_all += tensor_multiplicities[(p,q)]*d**(p+q)
    sorted_perm = product_rep.argsort()
    out_ranks = []
    for rank,mul in min_mults.items():
        out_ranks.extend(mul*[rank])
    out_rep = TensorRep(out_ranks,G=repin.G)
    # have to do some gnarly permuting to account for block ordering vs elemnt ordering
    # and then the multiplicity sorted order and the original ordering
    ids = jnp.argsort(rep_permutation(product_rep))[sorted_perm[all_ids]]
    return out_rep,ids

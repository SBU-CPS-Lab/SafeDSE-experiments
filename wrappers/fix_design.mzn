% Exact period of a returned design (exact_period.py).
%
% When the objective is not THROUGHPUT, the model only guarantees
% MCR <= mu <= period_ub (lib/mcm.mzn); a parallel CP-SAT portfolio then
% reports mu at the bound instead of the design's least period. This wrapper
% fixes every decision of the returned design -- binding, static order,
% patterns, modes, cards, SILs, partitioning, TDMA slots and buffers -- and
% the harness minimises THROUGHPUT on it, which by Proposition 1 gives
% ceil(MCR) of that design. Costs are fully determined by these decisions, so
% they cannot change. The model itself is unchanged; this file only adds
% constraints.
include "dse.mzn";

array[int] of int:  fx_proc;
array[int] of int:  fx_succ;
array[int] of int:  fx_pat;
array[int] of int:  fx_pmode;
array[int] of bool: fx_fcr_used;
array[int] of int:  fx_sil_impl;
array[int] of int:  fx_csil;
array[int] of bool: fx_partition;
array[int] of int:  fx_tdma_alloc;
array[int] of int:  fx_sendbuf;
array[int] of int:  fx_recbuf;

constraint forall(i in A)( proc[i] = fx_proc[i] /\ succ[i] = fx_succ[i]
                           /\ sil_impl[i] = fx_sil_impl[i] );
constraint forall(a in PAR)( pat[a] = fx_pat[a] );
constraint forall(p in PR)( pmode[p] = fx_pmode[p] /\ csil[p] = fx_csil[p]
                            /\ partition[p] = fx_partition[p]
                            /\ tdma_alloc[p] = fx_tdma_alloc[p] );
constraint forall(f in FCR)( fcr_used[f] = fx_fcr_used[f] );
constraint forall(c in index_set(fx_sendbuf))( sendbuf[c] = fx_sendbuf[c]
                                               /\ recbuf[c] = fx_recbuf[c] );

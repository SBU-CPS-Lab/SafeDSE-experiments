% ===========================================================================
% mcm.mzn -- throughput and deadlock-freedom for HSDF, via node potentials
%
% A period mu is feasible for a self-timed HSDF schedule iff there exist node
% potentials pot[] satisfying, for every edge (i,j):
%
%       pot[j] >= pot[i] + T[i] - tok[i,j] * mu
%
% This is the Bellman-Ford / LP-duality characterisation of the maximum cycle
% mean.  Two consequences make it do double duty:
%   * `minimize mu` returns exactly the MCM (minimum iteration period);
%   * infeasibility <=> a token-less cycle exists <=> deadlock.
% It therefore replaces BOTH the custom throughput propagator (which MiniZinc
% cannot host) and the separate deadlock machinery -- Rosvall's `rank` family,
% Bonfietti's throughput>0 test.
%
% Cost: |A| potential variables and one linear inequality per candidate edge,
% against 2|A|^2 variables for the edge-residual formulation.
%
% LINEARITY.  tok[i,j]*mu is linear only while tok is a PARAMETER.  Application
% edges and the processor wrap edge both have fixed token counts, so they stay
% linear.  The communication buffer back-edges in lib/comm.mzn have variable
% token counts, which makes them var*var; CP-SAT's native int_times handles
% them (docs/design.md#throughput-by-node-potentials).
%
% PER-APPLICATION PERIODS.  Every edge uses the period of its source node's
% application (`mu_of`), so this layer is the same for one or several
% applications.
%
% SOUNDNESS.  Potentials belonging to different periods are not in a common
% time base.  As in Rosvall's MCR propagator, period[z] is the maximum cycle
% ratio of the CONNECTED COMPONENT of the MSAG containing application z -- so
% two applications sharing a processor necessarily share a period.  With that
% coupling constraint present (see model/dse.mzn) every potential inequality
% lies within one component and therefore within one time base, and the
% formulation is sound in general
% (docs/design.md#periods-of-multiple-applications).
% ===========================================================================

% Fixed application edges.  tok[i,j] < 0 means "no channel".
% `mu_of` gives the period governing each node -- mu[app[i]] in practice.  It
% is an array rather than a scalar so that coupled and partitioned periods need
% no change here: only the mapping from node to period differs.
predicate mcm_fixed_edges(
    array[int,int] of int: tok,
    array[int] of var int: T,
    array[int] of var int: pot,
    array[int] of var int: mu_of
) =
    let { set of int: A = index_set(pot) } in
    forall (i, j in A where tok[i,j] >= 0) (
        pot[j] >= pot[i] + T[i] - tok[i,j] * mu_of[i]
    );

% Token-less edges selected by a reified Boolean matrix: the serialisation arcs
% introduced by the static order.
predicate mcm_schedule_edges(
    array[int,int] of var bool: sedge,
    array[int] of var int:      T,
    array[int] of var int:      pot
) =
    let { set of int: A = index_set(pot) } in
    forall (i, j in A where i != j) (
        sedge[i,j] -> pot[j] >= pot[i] + T[i]
    );

% The per-core static order must close into a CYCLE carrying one initial token
% -- the processor-availability token.  Each actor fires once per iteration, so
% a core's period is at least the sum of the WCETs mapped onto it.  Encoding
% the order as an open chain silently drops that bound and the model will
% happily report a 20-actor graph running on two cores at full pipeline rate.
% This is exactly what Rosvall's and Bonfietti's dummy-node + `circuit`
% encoding provides: the dummy node IS the wrap.
predicate mcm_wrap_edges(
    array[int,int] of var bool: wrap,
    array[int] of var int:      T,
    array[int] of var int:      pot,
    array[int] of var int:      mu_of
) =
    let { set of int: A = index_set(pot) } in
    forall (i, j in A) (
        wrap[i,j] -> pot[j] >= pot[i] + T[i] - mu_of[i]
    );

% Remove the free additive constant on the potentials.
predicate mcm_anchor(array[int] of var int: pot) =
    pot[min(index_set(pot))] = 0;

% Periodic-phase latency estimate (docs/design.md#latency-estimate).  In the
% periodic regime the k-th
% firing of actor i starts at approximately pot[i] + k*mu, so
%     latency(s,d) ~= pot[d] - pot[s] + rho(s,d)*mu + T[d]
% where rho is the minimum token-delay path from s to d.  Exact in the periodic
% phase, ignores the transient.  Use to constrain-and-prune; tools/verify.py
% checks returned solutions with the transient included.
function var int: mcm_periodic_latency(
    array[int] of var int: pot, array[int] of var int: T,
    int: s, int: d, int: rho, var int: mu
) = pot[d] - pot[s] + rho * mu + T[d];

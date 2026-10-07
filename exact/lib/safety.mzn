% ===========================================================================
% safety.mzn -- SIL assignment, isolation, and the development-cost trade-off
% (docs/design.md#safety-model)
%
% This is the layer that makes the framework safety-aware rather than
% reliability-aware, and it produces a real result on its own, without any
% structural safety pattern.
%
% The trade-off it exposes is Koopman's rule 2 priced out: software sharing a
% processor without a certified isolation argument is all developed to the
% highest SIL present on that processor. So the designer may either buy another
% core and keep the cheap software cheap, or co-locate and pay to develop a
% low-SIL actor to a high SIL it does not need. Which is cheaper depends on the
% ratio between hardware price and the per-SIL development multiplier, and that
% ratio is exactly what the DSE should be resolving.
%
% VARIABLES
%   sil_req[a]   parameter -- the requirement, per ACTOR
%   sil_impl[i]  decision  -- the SIL this actor is actually DEVELOPED to
%   csil[p]      decision  -- the SIL this core is provisioned/certified to
%   partition[p] decision  -- is a certified partitioning mechanism used here
%
% The gap between sil_req and sil_impl is the promotion, and dev_k prices it.
% ===========================================================================
include "globals.mzn";

array[PAR] of 0..4: sil_req_parent;
array[0..4] of int: dev_k;           % development-cost multiplier x100
array[A] of int: dev_base;           % baseline development cost per actor
bool: allow_promotion;

array[A]  of var 0..4: sil_impl;
array[PR] of var 0..4: csil;
array[PR] of var bool: partition;

% ---- core capability ------------------------------------------------------
% A core cannot be provisioned above what its type can be certified to. This is
% the pruning that heterogeneity buys back: a Cortex-M capped at SIL 2 removes
% itself from the domain of every SIL-3 actor without any search.
constraint forall(p in PR)( csil[p] <= max_sil[ctype[p]] );
constraint forall(p in PR)( not used[p] -> csil[p] = 0 /\ not partition[p] );

% partitioning is a per-core-TYPE capability, not a universal option
% (docs/design.md#partitioning)
constraint forall(p in PR)( partition[p] -> partitionable[ctype[p]] );

% ---- the requirement must be met ------------------------------------------
constraint forall(i in A)( active[i] -> sil_impl[i] >= sil_req_parent[parent[i]] );
constraint forall(i in A)( active[i] -> sil_impl[i] <= csil[proc[i]] );
constraint forall(i in A)( not active[i] -> sil_impl[i] = 0 );

% ---- Koopman rule 2: no silent mixing -------------------------------------
% Without a certified isolation argument, everything on a core is developed to
% the core's SIL -- "if one function is SIL 4, all software must be SIL 4".
% With partitioning, an actor may sit below its core's SIL, and the core pays a
% partitioning cost for the privilege.
% Communication actors are exempted from Koopman rule 2 only in comm_sil_mode 0
% (docs/design.md#communication-sil); in modes 1 and 2 they are ordinary
% software on their core and the rule applies to them like anything else.
constraint forall(i in A)(
    active[i] /\ not partition[proc[i]]
    /\ not (comm_actor[i] /\ comm_sil_exempt)
    -> sil_impl[i] = csil[proc[i]]
);

% a provisioned core must actually be justified by something running on it,
% otherwise the solver can inflate csil for free and the metric loses meaning
constraint forall(p in PR)(
    used[p] -> csil[p] = max(i in A)(
        bool2int(proc[i] = p /\ active[i]
                 /\ not (comm_actor[i] /\ comm_sil_exempt)) * sil_impl[i] )
);

% ---- promotion switch (docs/design.md#sil-promotion) ----------------------
% Default is to allow promotion, because it is the phenomenon under study.
% Forcing it off is a useful baseline: it answers "how much does allowing
% promotion save?", which is a sharper question than the absolute cost.
% Only ACTIVE nodes: an inactive pattern slot has sil_impl = 0 (above) but
% carries its owner's requirement, so posting this unguarded forced every slot
% of an actor with SIL >= 1 active, i.e. the owner's largest pattern (measured:
% with promotion forbidden, every solution activated all of its slots).
constraint not allow_promotion ->
    forall(i in A)( active[i] -> sil_impl[i] = sil_req_parent[parent[i]] );

% ---- costs ----------------------------------------------------------------
var 0..sum(i in A)(dev_base[i] * max(dev_k)) div 100: dev_cost;
constraint dev_cost = sum(i in A)( bool2int(active[i]) * dev_base[i]
                                 * dev_k[sil_impl[i]] ) div 100;

var 0..sum(p in PR)(partition_cost[ctype[p]]): partition_total;
constraint partition_total =
    sum(p in PR)( bool2int(partition[p]) * partition_cost[ctype[p]] );

% How much of the development cost is paid for SIL nobody asked for. Reported
% separately because it is the quantity the trade-off is actually about.
var int: promotion_cost;
constraint promotion_cost =
    sum(i in A)( bool2int(active[i]) * dev_base[i]
               * (dev_k[sil_impl[i]] - dev_k[sil_req_parent[parent[i]]]) ) div 100;

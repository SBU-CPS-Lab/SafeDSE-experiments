% ===========================================================================
% order.mzn -- binding and per-core static order
%
% Encoding: successor + rank, rather than Rosvall's `next` over a dummy-extended
% array closed by `circuit`.  The dummy/circuit form is a Gecode idiom that
% MiniZinc decomposes anyway, and Rosvall's `rank` family (constraints 29-42)
% is ~14 schemas with up to four nested quantifiers.  Here:
%
%   succ[i] = 0     i is the last actor on its core
%   ord[i]          position within the core's order, head anchored at 0
%   wrap[i,j]       the cycle-closing edge, last -> head on the same core
%
% Deadlock is caught by the potential constraints in mcm.mzn, so `ord` only has
% to make the static order itself acyclic, which is one constraint.
%
% ANCHORING.  `ishead` forcing ord=0 at each chain head is not cosmetic: without
% it `ord` floats and the wrap condition can never be detected, which silently
% removes the "core period >= sum of its WCETs" bound.
% ===========================================================================
include "globals.mzn";

int: n;  set of int: A = 1..n;
int: nApps;  set of int: APP = 1..nApps;
array[A] of APP: app;

% -- provenance from the SDF->HSDF unfolding --------------------------------
int: nParents;  set of int: PAR = 1..nParents;
array[A] of PAR: parent;        % which SDF actor this copy came from
array[A] of int: copy_index;
% names are carried through purely for readable output and error messages
array[A]   of string: node_name;
array[PAR] of string: parent_name;

array[A] of var PR: proc;
array[A] of var 0..n:   succ;
array[A] of var 0..n-1: ord;
array[A] of var bool:   ishead;

% Communication actors are scheduled on the BUS, not in a processor's static
% order: DeSyDe keeps them in separate sendNext/recNext circuits, and folding
% them into the computation order is both wrong and expensive. Wrong, because a
% block or send actor does not occupy a core's execution slot; expensive,
% because the order machinery is O(n^2) and communication triples the node
% count. Contention for the bus is already priced into the TDMA WCCT -- wcct_b
% is the worst-case wait for a slot and wcct_s covers whole rounds -- which is
% precisely what makes a TDMA bus time-predictable, so excluding them here does
% not make the model optimistic.
array[A] of bool: comm_actor;
set of int: CPU_A = { i | i in A where not comm_actor[i] };
constraint forall(i in A where comm_actor[i])( succ[i] = 0 /\ ord[i] = 0
                                            /\ not ishead[i] );

constraint forall(i in A)( used[proc[i]] );
% Only an ACTIVE node makes a processor used. An inactive pattern slot is
% pinned to its owner's core and must not keep an otherwise-idle core alive.
constraint forall(p in PR)( used[p] <-> exists(i in A)(proc[i] = p /\ active[i]) );

% ---- static order ---------------------------------------------------------
constraint alldifferent_except_0([succ[i] | i in CPU_A]);
constraint forall(i in CPU_A)( succ[i] != i );
constraint forall(i in CPU_A)( succ[i] > 0 -> proc[succ[i]] = proc[i] );
constraint forall(p in PR)( used[p] -> sum(i in CPU_A)(proc[i] = p /\ succ[i] = 0) = 1 );
constraint forall(i in CPU_A)( succ[i] > 0 -> ord[succ[i]] = ord[i] + 1 );
constraint forall(i, j in CPU_A where i != j)( proc[i] = proc[j] -> ord[i] != ord[j] );
constraint forall(j in CPU_A)( ishead[j] <-> not exists(i in CPU_A)(succ[i] = j) );
constraint forall(j in CPU_A)( ishead[j] -> ord[j] = 0 );

array[A,A] of var bool: sedge;
constraint forall(i, j in A)( sedge[i,j] <-> (i != j /\ not comm_actor[i] /\ succ[i] = j) );

array[A,A] of var bool: wrap;
constraint forall(i, j in A)(
    wrap[i,j] <-> ( not comm_actor[i] /\ not comm_actor[j]
                    /\ succ[i] = 0 /\ ishead[j] /\ proc[i] = proc[j] )
);

% ---- Rosvall constraints 23 and 33 (docs/design.md#copy-order-symmetry) ---
% Both concern HSDF copies of a common SDF parent, so they need the `parent[]`
% provenance that the front end emits.
%
% These are REDUNDANT constraints: copies of the same parent are genuinely
% interchangeable, so imposing an order among them removes symmetric solutions
% without removing any distinct one.  The regression test asserts that adding
% them changes runtime but not the optimum -- if the optimum moves, the
% constraint is wrong, not merely strong.
bool: use_parent_symmetry;

% (23) copies of the same parent are ordered by processor index
constraint use_parent_symmetry -> forall(i, j in A where
        i < j /\ parent[i] = parent[j] /\ copy_index[i] < copy_index[j] )(
    proc[i] <= proc[j]
);
% (33) copies of the same parent on the SAME core keep their copy order
constraint use_parent_symmetry -> forall(i, j in A where
        i < j /\ parent[i] = parent[j] /\ copy_index[i] < copy_index[j] )(
    proc[i] = proc[j] -> ord[i] < ord[j]
);

% ---- application partitioning (--period-mode partitioned) -----------------
bool: period_mode_partitioned;
constraint period_mode_partitioned ->
    forall(i, j in A where app[i] != app[j])( proc[i] != proc[j] );

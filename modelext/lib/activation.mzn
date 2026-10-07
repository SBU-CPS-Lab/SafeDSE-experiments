% ===========================================================================
% activation.mzn -- guarded superposition of safety patterns
% (docs/design.md#guarded-superposition)
%
% The front-end has already instantiated every candidate replica and channel
% into one FIXED graph, each tagged with the set of patterns that use it.  So
% topology is a PARAMETER here and only selection is a variable:
%
%       pat[a]      one decision per SDF actor -- which pattern to apply
%       active[i]  <->  node_guard[i, pat[node_owner[i]]]
%
% This is what makes the safety layer tractable.  The alternative -- a var
% adjacency matrix over all possible replicas, with decision variables in the
% index expressions -- needs O(n^2) topology variables and an `element` on a
% variable index for every access, and did not solve a five-actor instance in
% three minutes.
%
% CANONICALISATION.  An inactive node is not deleted (the array is fixed), it
% is neutralised: T = 0, pinned to its owner's processor, and excluded from
% every cost and memory sum.  A zero-WCET actor contributes nothing to any
% cycle mean, so it is invisible to throughput.  Pinning matters as much as
% zeroing: an unpinned inactive node could float over the platform and
% multiply every solution by the number of processors.
% ===========================================================================
include "globals.mzn";

int: nPat;  set of int: PAT = 1..nPat;
array[PAT] of string: pat_name;

% Which patterns may be applied to each SDF actor.  Computed in the front-end
% from achieves_sil and the selected fault model, so an actor whose SIL is
% unreachable under the chosen fault model fails at build time with an
% explanation rather than as an opaque UNSAT here.
array[PAR, PAT] of bool: pat_allowed;

array[PAR] of var PAT: pat;
constraint forall(a in PAR)( pat_allowed[a, pat[a]] );

% A pattern SLOT is itself a parent in the unfolded graph, so it gets its own
% `pat` variable. Tie it to its owner's: the decision belongs to the owner, and
% leaving the slot's copy free would add one redundant variable per slot and a
% corresponding symmetry.
array[PAR] of int: par_owner;       % base actors map to themselves
constraint forall(a in PAR)( pat[a] = pat[par_owner[a]] );

% ---- node activation ------------------------------------------------------
array[A] of int: node_owner;        % SDF actor (PAR index) that owns this node
array[A] of int: owner_node;        % HSDF node of the owner's MATCHING COPY
array[A, PAT] of bool: node_guard;  % base nodes: all true

array[A] of var bool: active;

constraint forall(i in A)( active[i] <-> node_guard[i, pat[node_owner[i]]] );

% Neutralise inactive nodes.  Pinning to the owner's processor is canonical and
% introduces no symmetry; the node still sits in that core's static order, but
% with T = 0 it costs nothing there.
constraint forall(i in A)( not active[i] -> proc[i] = proc[owner_node[i]] );

% ---- pattern edges --------------------------------------------------------
% Guarded precedence constraints, posted as a sparse list rather than a matrix.
% Declared over `int` rather than PE because the front-end always emits at
% least one row -- MiniZinc 2.8.7 segfaults on zero-length int arrays here.
% nPE still controls the index set, so the padding row is never constrained.
int: nPE;  set of int: PE = 1..nPE;
array[int] of int: pe_src;
array[int] of int: pe_dst;
array[int] of int: pe_tok;
array[int] of int: pe_owner;
array[int, PAT] of bool: pe_guard;   % 1..max(nPE,1); a dummy row when nPE=0

% Second, conjunctive condition (explicit voters, docs/design.md#voters).
%
% One guard suffices while every edge belongs to exactly one actor's pattern.
% An explicit voter breaks that: when actor u routes its output through a voter
% and feeds actor a whose pattern fans the input out to its checkers, the copy
% u -> a~k is selected by a's pattern AND by u's, and the two are different
% actors' decisions. Dropping either condition is unsound in a specific and
% quiet way -- keep only a's and the pre-voter result reaches a's checker under
% patterns that are supposed to route through the voter.
%
% There is no sentinel and no zero index. An edge with only one condition sets
% pe_owner2 = pe_owner and gets an all-true pe_guard2 row, so the conjunction
% degenerates to the single-guard case exactly. That avoids `pat[0]`, which
% would be an out-of-bounds element lookup even inside a disjunction that can
% never select it.
array[int] of int: pe_owner2;
array[int, PAT] of bool: pe_guard2;

array[PE] of var bool: pe_on;
constraint forall(e in PE)(
    pe_on[e] <-> ( pe_guard[e, pat[pe_owner[e]]]
                /\ pe_guard2[e, pat[pe_owner2[e]]] )
);

predicate mcm_pattern_edges(
    array[int] of var int: T,
    array[int] of var int: pot,
    array[int] of var int: mu_of
) =
    % MEASURED REDUNDANT. The front-end writes pattern channels into
    % the `tok` matrix as well as into pe_*, and lib/mcm.mzn posts every
    % tok[i,j] >= 0 pair UNCONDITIONALLY. So each constraint below is a weaker,
    % guarded restatement of one already posted outright, and contributes
    % nothing: setting nPE = 0 on out/v_nvp.dzn returns the identical optimum
    % (mu=1180, nprocs=6, hw_cost=624, dev_cost=5046) in the same 5.6 s.
    %
    % What actually neutralises an unselected pattern's edges is not this
    % predicate but T[i] = 0 on inactive nodes: the edge stays in the graph and
    % contributes zero delay. That is the documented design ("inactive nodes are
    % neutralised, not deleted"), and it is also what tools/verify.py
    % reconstructs, which is why the two agree.
    %
    % Kept rather than deleted because the redundancy is a property of how
    % build_dzn currently fills `tok`, not of the model: excluding guarded
    % channels from `tok` -- so that a pattern edge is posted only when its
    % pattern is selected -- would make this predicate the only thing posting
    % them, and would need the verifier changed in step. Do not assume deleting
    % it is free, and do not assume it is doing work.
    forall(e in PE where not vd_pair[pe_src[e], pe_dst[e]])(
        pe_on[e] -> pot[pe_dst[e]] >= pot[pe_src[e]] + T[pe_src[e]]
                                    - pe_tok[e] * mu_of[pe_src[e]]
    );

% ---- placement relations (docs/design.md#pairwise-placement) -------------
% Applied PAIRWISE across HSDF copies: copy i of the checker must differ from
% copy i of the doer.  The set-wise reading ("the checker set is disjoint from
% the doer set") is strictly stronger and would forbid safe interleavings.
%
%   1 SAME           same processor
%   2 DIFFERENT      different processor
%   3 DIFFERENT_FCR  different fault containment region  (random HW faults)
%   4 DIVERSE        different core type                 (systematic SW faults)
%
% All four are element lookups into PARAMETER arrays, so they are cheap.  Note
% that DIVERSE is inexpressible on a homogeneous platform: heterogeneity is a
% prerequisite for defending against systematic faults, not a refinement.
int: nPL;  set of int: PL = 1..nPL;
array[int] of int: pl_u;
array[int] of int: pl_v;
array[int] of 1..4: pl_rel;
array[int] of int: pl_owner;
array[int, PAT] of bool: pl_guard;   % 1..max(nPL,1)

array[PL] of var bool: pl_on;
constraint forall(c in PL)( pl_on[c] <-> pl_guard[c, pat[pl_owner[c]]] );

constraint forall(c in PL)( pl_on[c] -> (
    if       pl_rel[c] = 1 then proc[pl_u[c]] = proc[pl_v[c]]
    elseif   pl_rel[c] = 2 then proc[pl_u[c]] != proc[pl_v[c]]
    elseif   pl_rel[c] = 3 then fcr[proc[pl_u[c]]] != fcr[proc[pl_v[c]]]
    else                        ctype[proc[pl_u[c]]] != ctype[proc[pl_v[c]]]
    endif
));

% ---- reporting ------------------------------------------------------------
var 0..n: n_active;
constraint n_active = sum(i in A)( bool2int(active[i]) );

% Recurring (per-instance) cost of the applied patterns -- the replicated
% hardware channels they call for, priced independently of the cores.
% Summed over BASE actors only. Pattern slots are parents too, with pat tied
% to their owner's, so summing over all of PAR counted an actor's recurring
% cost 1 + (number of its slots) times; bus-transfer parents take no pattern.
array[PAT] of int: pat_recurring;
set of int: BASE_PAR = { a | a in PAR where par_owner[a] = a
                         /\ forall(i in A where parent[i] = a)(not comm_actor[i]) };
var int: pattern_cost;
constraint pattern_cost = sum(a in BASE_PAR)( pat_recurring[pat[a]] );

% ===========================================================================
% dse.mzn -- SafeDSE top level
%
% Mapping + per-core static order + per-application throughput + heterogeneous
% platform with FCRs + safety patterns + TDMA communication + switchable
% objective.  Structure: docs/architecture.md; rationale: docs/design.md.
%
% Backend: CP-SAT.  Gecode and Chuffed remain wired up in the test harness as
% small-instance correctness oracles only -- disagreement between them signals
% a modelling error, not a performance question.
% ===========================================================================
include "globals.mzn";
include "../lib/platform.mzn";
include "../lib/order.mzn";
include "../lib/symmetry.mzn";
include "../lib/mcm.mzn";
include "../lib/latency.mzn";
include "../lib/safety.mzn";
include "../lib/activation.mzn";
include "../lib/comm.mzn";
include "../lib/modelext.mzn";

% ---- application data -----------------------------------------------------
array[A,A] of int: tok;                       % -1 = no channel
array[A, CT, MD] of int: wcet;
array[A] of int: mem_req;
array[APP] of int: period_ub;

% ---- derived: WCET follows the binding ------------------------------------
% This is a 3-D element with two nested variable indices (pmode[proc[i]] is
% itself an element on a var array), which looks like it ought to flatten
% badly. A flattened "speed class" encoding -- one reified implication per
% (actor, slot) feeding a 1-D lookup into a parameter row -- was tried on the
% theory that the nesting was the scalability bottleneck. Measured head to
% head, the class encoding produced identical optima but was never faster and
% up to 4x slower (c_rasta at mu<=500: 3.46s vs 0.85s), so the simpler form is
% kept. The actual bottleneck was branch symmetry between identical HSDF
% copies (docs/design.md#patterns-at-sdf-level).
int: maxwcet = max([wcet[i,t,m] | i in A, t in CT, m in MD
                    where wcet[i,t,m] < 1000000]);
array[A] of var 0..maxwcet: T;
% An inactive node is neutralised, not deleted: zero WCET makes it invisible to
% every cycle mean, so it costs nothing in the static order it still sits in.
% Application actors take their WCET from the table; communication actors take
% theirs from the TDMA model (comm_actor_times), so the two sets are disjoint.
set of int: COMM = { ch_block[c] | c in CH } union { ch_send[c] | c in CH }
                   union { ch_rec[c] | c in CH };
constraint forall(i in A where not (i in COMM))(
    T[i] = bool2int(active[i]) * wcet[i, ctype[proc[i]], pmode[proc[i]]]
);
constraint comm_actor_times(T);

% A communication actor lives on the SENDING processor: block and send occupy
% the sender's network interface, receive the destination's local memory. Their
% placement is therefore derived, never chosen.
constraint forall(c in CH)(
    proc[ch_block[c]] = proc[ch_src[c]] /\
    proc[ch_send[c]]  = proc[ch_src[c]] /\
    proc[ch_rec[c]]   = proc[ch_dst[c]]
);
% An inactive communication actor is neutralised the same way an inactive
% pattern slot is: zero WCET, and no claim on its core.
constraint forall(c in CH)(
    not ch_remote[c] -> ( T[ch_block[c]] = 0 /\ T[ch_send[c]] = 0
                       /\ T[ch_rec[c]] = 0 )
);

% ---- throughput: per-application periods ----------------------------------
int: maxmu = sum(i in A)(maxwcet) * max([1] ++ [tok[i,j] | i,j in A where tok[i,j] > 0]);
array[APP] of var 1..maxmu: mu;
array[A] of var -maxmu..maxmu: pot;

% PER-APPLICATION PERIODS (docs/design.md#periods-of-multiple-applications).
%
% Rosvall's MCR propagator decomposes the MSAG into connected sub-graphs and
% runs the analysis per sub-graph, so every application covered by one sub-graph
% receives that sub-graph's period. Two applications land in the same sub-graph
% exactly when they share a processor, because the per-core static order links
% their actors. DeSyDe states this as a plain model constraint, not inside the
% propagator (src/cp_model/throughput.constraints):
%
%     (proc[ii]==proc[ij]) >> (period[app(ii)] == period[app(ij)])
%     (period[app(ii)] != period[app(ij)]) >> (proc[ii] != proc[ij])
%
% So the general case is one line, and it subsumes the earlier taxonomy:
% applications sharing a core are coupled to a common period, applications on
% disjoint cores get independent ones. Crucially it is also SOUND for the node
% potentials -- within a connected component every node shares one mu, so the
% potentials are in a common time base, which is the condition the potential
% formulation needs.
% Posted over APPLICATION pairs rather than actor pairs. DeSyDe writes this as
% O(n^2) reified implications over cross-application actors, which timed out at
% 32 actors here. Routing it through a per-(app, core) occupancy matrix makes it
% O(|APP|^2 + n*|APP|) and is exactly equivalent: two applications share a core
% iff some core is occupied by both.
array[APP, PR] of var bool: app_on;
constraint forall(z in APP, p in PR)(
    app_on[z, p] <-> exists(i in A where app[i] = z)( proc[i] = p /\ active[i] )
);
array[APP, APP] of var bool: app_shares;
constraint forall(y, z in APP where y < z)(
    app_shares[y, z] <-> exists(p in PR)( app_on[y, p] /\ app_on[z, p] )
);
constraint forall(y, z in APP where y < z)(
    app_shares[y, z] -> mu[y] = mu[z]
);

array[A] of var 1..maxmu: mu_of;
constraint forall(i in A)( mu_of[i] = mu[app[i]] );

constraint mcm_fixed_edges(tok, T, pot, mu_of);
constraint mcm_schedule_edges(sedge, T, pot);
constraint mcm_wrap_edges(wrap, T, pot, mu_of);
constraint mcm_pattern_edges(T, pot, mu_of);
constraint comm_msag_edges(T, pot, mu_of);
constraint mcm_anchor(pot);
constraint latency_periodic(pot, T, mu_of);

constraint forall(z in APP)( mu[z] <= period_ub[z] );

% ---- redundant constraints (from DeSyDe throughput.constraints) -----------
% These are implied by the potential formulation but stated explicitly, which
% is what lets a declarative model keep pace with a hand-written propagator:
% the solver derives them only after search, whereas stated up front they prune
% at the root. DeSyDe posts all of them alongside its propagator.

% (1) A core's period is at least the sum of the WCETs mapped onto it. The wrap
%     edge already implies this, but only through a cycle the solver has to
%     find; as a linear sum it propagates immediately.
array[PR] of var 0..maxmu: proc_load;
% Communication actors are excluded: their time is spent on the bus, not on the
% core's execution unit, which is the same reason they are outside the processor
% static order. Counting them here would charge the sender's core for time it
% does not spend.
constraint forall(p in PR)(
    proc_load[p] = sum(j in A where not comm_actor[j])(
        bool2int(proc[j] = p) * T[j] )
);
constraint forall(i in A, p in PR)( proc[i] = p -> mu[app[i]] >= proc_load[p] );

% (2) No actor may exceed its application's period bound, so bind its WCET.
constraint forall(i in A)( active[i] -> T[i] <= period_ub[app[i]] );

% (3) Two actors whose combined minimum WCETs exceed the bound cannot share a
%     core, and cannot be adjacent in a static order.
%     Only while BOTH are active: an inactive pattern slot has T = 0 and is
%     pinned to its owner's core, so posting this unguarded forbade the owner
%     from sharing a core with its own unselected slot, and a tight period_ub
%     turned feasible designs into UNSAT (measured on d_random_hw: throughput
%     optimum 235, verified, yet UNSAT at period_ub 235/258/293). Communication
%     actors run on the bus, not on the core, and are excluded outright.
array[A] of int: min_wcet;
constraint forall(i, j in A where i < j
                  /\ not comm_actor[i] /\ not comm_actor[j]
                  /\ min_wcet[i] + min_wcet[j] > min(period_ub))(
    (app[i] = app[j] /\ min_wcet[i] + min_wcet[j] > period_ub[app[i]]
     /\ active[i] /\ active[j]) ->
        (proc[i] != proc[j] /\ succ[i] != j /\ succ[j] != i)
);

% (4) An application needs at least ceil(sum of its minimum WCETs / its period
%     bound) distinct processors. A direct lower bound on the core count, which
%     is often the binding constraint and is invisible to the cycle analysis.
array[APP] of int: min_procs;
constraint forall(z in APP where min_procs[z] > 1)(
    nvalue([proc[i] | i in A where app[i] = z]) >= min_procs[z]
);

% ---- memory ---------------------------------------------------------------
constraint forall(p in PR)(
    sum(i in A)( bool2int(proc[i] = p /\ active[i]) * mem_req[i] )
    <= core_mem[ctype[p]]
);

% ---- costs ----------------------------------------------------------------
% dev_cost, partition_total and promotion_cost come from lib/safety.mzn.
var int: total_cost;
constraint total_cost = hw_cost + dev_cost + partition_total + pattern_cost;

% ---- switchable objective (docs/design.md#objective-selection) ------------
% Every metric is always computed and always bounded; the objective is a
% parameter, so switching what is optimised needs a -D flag and no model edit.
enum Metric = { THROUGHPUT, LATENCY, HWCOST, DEVCOST, TOTALCOST, POWER,
                NPROCS, PROMOTION };
Metric: opt_metric;
array[Metric] of int: ub;

var 0..P: nprocs;
constraint nprocs = sum(p in PR)( bool2int(used[p]) );

% THROUGHPUT is reported as the worst application period, so a single bound
% constrains every application even in partitioned mode.
var 1..maxmu: mu_worst;
constraint mu_worst = max(mu);

array[Metric] of var int: metric;
constraint metric = [ mu_worst, max_latency, hw_cost, dev_cost,
                      total_cost, power, nprocs, promotion_cost ];
constraint forall(m in Metric)( metric[m] <= ub[m] );

solve :: seq_search([
    int_search(proc, first_fail, indomain_min),
    int_search(succ, first_fail, indomain_min),
    int_search([mu[z] | z in APP], input_order, indomain_min),
])  minimize metric[opt_metric];

output [
  "objective(\(opt_metric)) = \(metric[opt_metric])\n",
  "mu       = \(mu)\n",
  "nprocs   = \(nprocs)   hw_cost = \(hw_cost)   power = \(power)\n",
  "proc     = \(proc)\n",
  "pmode    = \(pmode)\n",
  "used     = \(used)\n",
  "fcr_used = \(fcr_used)\n",
  "T        = \(T)\n",
  "sil_impl = \(sil_impl)\n",
  "pat      = \(pat)\n",
  "active   = \(active)\n",
  "csil     = \(csil)\n",
  "promo    = \(promotion_cost)  partition = \(partition_total)\n",
  "latency  = \(latency)\n",
  "tdma     = \(tdma_alloc)  remote=\(n_remote)\n",
  "metric   = \(metric)\n",
];

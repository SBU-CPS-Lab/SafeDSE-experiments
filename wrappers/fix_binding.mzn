% RQ2 baseline SL (safety-last), step 2.
%
% The binding of every base actor is fixed to the core it received in step 1,
% where the same instance was solved with --force-no-patterns. Patterns,
% replicas, core provisioning and the platform stay free. The model itself is
% unchanged; this file only adds constraints.
include "dse.mzn";

array[int] of int: fix_node;    % HSDF node index in this instance
array[int] of int: fix_proc;    % core slot from the step-1 solution
constraint forall(k in index_set(fix_node))( proc[fix_node[k]] = fix_proc[k] );

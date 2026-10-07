% RQ2 baselines PF-min and PF-max (pattern-first).
%
% The pattern of every SDF actor is chosen before mapping, by a rule over
% catalogue attributes only (harness.py, pf_choice), and fixed
% here. Mapping, scheduling and the platform stay free. The model itself is
% unchanged; this file only adds constraints.
include "dse.mzn";

array[PAR] of int: fix_pat;     % 0 = not fixed (pattern slots follow their owner)
constraint forall(a in PAR where fix_pat[a] > 0)( pat[a] = fix_pat[a] );

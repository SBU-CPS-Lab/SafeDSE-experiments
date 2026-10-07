% Feasibility of a complete solution in the model (mutation analysis).
%
% fix_design.mzn fixes every decision of a design and leaves the period free.
% This wrapper also fixes activation, WCETs and the reported periods, so a
% solve answers one question: does the unmodified model accept this exact
% solution? mutation.py uses it as the ground truth for a corrupted solution
% or a model mutant's solution, next to tools/verify.py. The model itself is
% unchanged; this file only adds constraints.
include "fix_design.mzn";

array[int] of bool: fx_active;
array[int] of int:  fx_T;
array[int] of int:  fx_mu;

constraint forall(i in A)( active[i] = fx_active[i] /\ T[i] = fx_T[i] );
constraint forall(z in APP)( mu[z] = fx_mu[z] );

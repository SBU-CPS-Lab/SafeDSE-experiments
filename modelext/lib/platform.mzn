% ===========================================================================
% platform.mzn -- heterogeneous cores, FCRs, and the instantiation decision
% (docs/design.md#platform-model)
%
% The front-end expands the designer's catalogue of FCR templates into a flat
% array of candidate core slots.  Topology is parameter; instantiation is
% variable -- the same move used for safety patterns and communication.
%
%   ctype[p]  : which core type occupies slot p        PARAMETER
%   fcr[p]    : which fault containment region slot p is in   PARAMETER
%   used[p]   : is this core populated?                DECISION
%   fcr_used[f]: is this card bought?                  DECISION
%   mode[p]   : which operating mode                   DECISION
%
% Because ctype and fcr are parameters, DIFFERENT_FCR and DIVERSE become
% element lookups into parameter arrays -- cheap.  A homogeneous platform
% cannot express DIVERSE at all, which is why heterogeneity is a prerequisite
% for the systematic-fault half of the safety model and not a later refinement.
% ===========================================================================
include "globals.mzn";

int: P;            set of int: PR = 1..P;
int: nFCR;         set of int: FCR = 1..nFCR;
int: nCoreTypes;   set of int: CT = 1..nCoreTypes;
int: nModes;       set of int: MD = 1..nModes;

array[PR] of CT:  ctype;
array[PR] of FCR: fcr;
array[CT] of string: core_model;
array[CT] of 0..4: max_sil;
array[CT] of int: core_mem;
array[CT] of bool: partitionable;      % capability per core type
array[CT] of int: partition_cost;
array[CT] of int: n_modes_of;
array[FCR] of int: fcr_price;
array[CT, MD] of int: core_price;
array[CT, MD] of int: core_power;

array[PR]  of var bool: used;
array[FCR] of var bool: fcr_used;
array[PR]  of var MD:   pmode;

int: nSymClasses;  int: maxSymClassSize;
array[int] of int: symClassSize;
array[int, int] of int: symClass;      % 0-padded

int: nTmplGroups;  int: maxTmplGroupSize;
array[int] of int: tmplGroupSize;
array[int, int] of int: tmplGroup;      % 0-padded


% a populated core implies its card was bought
constraint forall(p in PR)( used[p] -> fcr_used[fcr[p]] );
% and an unbought card has no populated cores
constraint forall(f in FCR)( not fcr_used[f] -> forall(p in PR where fcr[p]=f)(not used[p]) );
% only modes the core type actually has
constraint forall(p in PR)( pmode[p] <= n_modes_of[ctype[p]] );
% canonicalise unused slots so they do not generate symmetric solutions
constraint forall(p in PR)( not used[p] -> pmode[p] = 1 );

var 0..sum(fcr_price) + sum(p in PR)(max(m in 1..nModes)(core_price[ctype[p],m])):
    hw_cost;
constraint hw_cost =
    sum(f in FCR)( bool2int(fcr_used[f]) * fcr_price[f] )
  + sum(p in PR)( bool2int(used[p]) * core_price[ctype[p], pmode[p]] );

var 0..sum(p in PR)(max(m in 1..nModes)(core_power[ctype[p],m])): power;
constraint power =
    sum(p in PR)( bool2int(used[p]) * core_power[ctype[p], pmode[p]] );

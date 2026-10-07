% ===========================================================================
% modelext.mzn -- model extensions for evaluation only
% (modelext_build.py of SafeDSE-experiments). Not part of SafeDSE.
% ===========================================================================

% ---- E1: pattern-dependent doer SIL -----------------------------------------
% An owner node (a base actor's own node) needs its actor's SIL minus the
% offset of the selected pattern; a pattern component keeps the actor's SIL.
array[PAT] of 0..4: pat_doer_off;
array[A] of var 0..4: sil_need;
constraint forall(i in A)( sil_need[i] =
    if par_owner[parent[i]] = parent[i]
    then max(0, sil_req_parent[parent[i]] - pat_doer_off[pat[parent[i]]])
    else sil_req_parent[parent[i]] endif );

% ---- E2: development multiplier of node i under pattern k (x100) ------------
array[A, PAT] of int: node_devm;

% ---- E3: verdict tokens -----------------------------------------------------
int: nVD;
array[int] of int: vd_src;
array[int] of int: vd_dst;
array[int] of int: vd_owner;          % base parent whose pattern owns the edge
int: vd_rmax;
array[PAR] of int: vd_pst;            % fault-reaction bound, 0 = none
array[A, A] of bool: vd_pair = array2d(A, A,
    [ exists(k in 1..nVD)(vd_src[k] = i /\ vd_dst[k] = j) | i, j in A ]);
array[PAR] of var 1..vd_rmax: vd_r;
constraint forall(k in 1..nVD)(
    pot[vd_dst[k]] >= pot[vd_src[k]] + T[vd_src[k]]
                      - vd_r[vd_owner[k]] * mu_of[vd_src[k]] );
constraint forall(k in 1..nVD where vd_pst[vd_owner[k]] > 0)(
    vd_r[vd_owner[k]] * mu_of[vd_dst[k]] <= vd_pst[vd_owner[k]] );
constraint forall(a in PAR where not exists(k in 1..nVD)(vd_owner[k] = a))(
    vd_r[a] = 1 );

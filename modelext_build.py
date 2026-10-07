#!/usr/bin/env python3
"""Creates modelext/: a copy of SafeDSE's model/ and lib/ with three model
extensions, for a cost-benefit evaluation only. SafeDSE itself is never
changed.

E1  pattern-dependent doer SIL: the owner of a selected pattern needs only
    its actor's SIL minus the pattern's `sil_offset` (Koopman's reading:
    the checker carries the actor's SIL, the doer may be lower); pattern
    components keep the actor's SIL, as in the model.
E2  priced development multipliers: an active pattern component's
    development cost is multiplied by the catalog's `dev_cost_multiplier` of
    its role in the selected pattern, or by `dev_cost_multiplier_diverse`
    when that pattern posts a DIVERSE relation on the component (2-of-2
    under fault class S).
E3  verdict tokens as variables: every catalog edge back to the owner with
    tokens >= 1 (a verdict edge) carries r[a] tokens, r[a] in 1..vd_rmax, one
    value per actor, under the fault-reaction bound r[a] * mu <= vd_pst[a].

Every extension is driven by data (modelext.py writes it per
instance); with the neutral data (offsets 0, multipliers 100, no verdict
edges) the copy has exactly the constraints of the model.

    python3 modelext_build.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT, SAFEDSE  # noqa: E402

DST = ROOT / "modelext"

EDITS = [
    ("model/dse.mzn", 'include "../lib/comm.mzn";',
     'include "../lib/comm.mzn";\ninclude "../lib/modelext.mzn";'),
    # E1: the requirement of an owner node follows the selected pattern
    ("lib/safety.mzn",
     "constraint forall(i in A)( active[i] -> sil_impl[i] >= "
     "sil_req_parent[parent[i]] );",
     "constraint forall(i in A)( active[i] -> sil_impl[i] >= sil_need[i] );"),
    ("lib/safety.mzn",
     "forall(i in A)( active[i] -> sil_impl[i] = sil_req_parent[parent[i]] );",
     "forall(i in A)( active[i] -> sil_impl[i] = sil_need[i] );"),
    # E2: development multiplier per node and selected pattern (x100)
    ("lib/safety.mzn",
     "var 0..sum(i in A)(dev_base[i] * max(dev_k)) div 100: dev_cost;",
     "var 0..sum(i in A)(dev_base[i] * max(dev_k) * max(k in PAT)"
     "(node_devm[i, k])) div 10000: dev_cost;"),
    ("lib/safety.mzn",
     "* dev_k[sil_impl[i]] ) div 100;",
     "* dev_k[sil_impl[i]] * node_devm[i, pat[node_owner[i]]] ) div 10000;"),
    ("lib/safety.mzn",
     "* (dev_k[sil_impl[i]] - dev_k[sil_req_parent[parent[i]]]) ) div 100;",
     "* (dev_k[sil_impl[i]] - dev_k[sil_need[i]])\n"
     "               * node_devm[i, pat[node_owner[i]]] ) div 10000;"),
    # E3: verdict edges leave the fixed and the guarded edge sets
    ("lib/mcm.mzn",
     "forall (i, j in A where tok[i,j] >= 0) (",
     "forall (i, j in A where tok[i,j] >= 0 /\\ not vd_pair[i,j]) ("),
    ("lib/activation.mzn",
     "    forall(e in PE)(\n        pe_on[e] -> pot[pe_dst[e]]",
     "    forall(e in PE where not vd_pair[pe_src[e], pe_dst[e]])(\n"
     "        pe_on[e] -> pot[pe_dst[e]]"),
]

MODELEXT_LIB = """\
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
    [ exists(k in 1..nVD)(vd_src[k] = i /\\ vd_dst[k] = j) | i, j in A ]);
array[PAR] of var 1..vd_rmax: vd_r;
constraint forall(k in 1..nVD)(
    pot[vd_dst[k]] >= pot[vd_src[k]] + T[vd_src[k]]
                      - vd_r[vd_owner[k]] * mu_of[vd_src[k]] );
constraint forall(k in 1..nVD where vd_pst[vd_owner[k]] > 0)(
    vd_r[vd_owner[k]] * mu_of[vd_dst[k]] <= vd_pst[vd_owner[k]] );
constraint forall(a in PAR where not exists(k in 1..nVD)(vd_owner[k] = a))(
    vd_r[a] = 1 );
"""


def main() -> int:
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            cwd=SAFEDSE, capture_output=True, text=True,
                            check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "model",
                            "lib"], cwd=SAFEDSE, capture_output=True,
                           text=True, check=True).stdout.strip()
    if dirty:
        raise SystemExit(f"safedse model/ or lib/ has local changes:\n{dirty}")
    shutil.rmtree(DST, ignore_errors=True)
    DST.mkdir()
    for sub in ["model", "lib"]:
        shutil.copytree(SAFEDSE / sub, DST / sub)
    for f, old, new in EDITS:
        p = DST / f
        t = p.read_text()
        if t.count(old) != 1:
            raise SystemExit(f"{f}: expected one occurrence of {old!r}, "
                             f"found {t.count(old)}")
        p.write_text(t.replace(old, new))
    (DST / "lib" / "modelext.mzn").write_text(MODELEXT_LIB)
    (DST / "SOURCE.txt").write_text(
        f"SafeDSE commit {commit}, model/ and lib/, with the model extensions\n"
        f"E1-E3 of modelext_build.py (lib/modelext.mzn and {len(EDITS)} "
        f"edits).\nEvaluation copy; never part of SafeDSE.\n")
    print(f"modelext/ created from SafeDSE {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

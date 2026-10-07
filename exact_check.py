#!/usr/bin/env python3
"""Cross-check of the RQ2/RQ4 results with the exact period model.

The model in safedse is conservative for the period: a path through an
inactive pattern slot can raise a design's least period above its deployed
MCR, never below it (SafeDSE docs/design.md#known-limitations, "least period
above the MCR"). So a cost optimum under a period bound can miss a design
whose real period meets the bound, and an UNSAT under a bound may not
hold for the deployed system. Variant B (patches/, a second
"order potential" for the static-order and wrap edges) is exact: its least
period of a design equals the MCR of the deployed MSAG, and it only relaxes
the model (every solution of the model is a solution of B with qpot = pot).

This script re-runs every stored row of e2_joint, e2_tight, e2_thr, e2_var
and e4_sens with the same inputs, bound, objective, fixings and solver
settings, but with the patched copy of the model in exact/
(never in safedse), and appends one row per run to e2_exact.csv with the
stored result next to the new one. The pin step (exact_period.pin_period)
and the fix wrappers use the same copy. Every new solution goes through
tools/verify.py as in the harness. e2_reps (repetitions of joint rows) and
RQ1/RQ3 are not re-run: RQ1 instances have no pattern slots (B is then the
model), and RQ3 is not part of this cross-check.

    python3 exact_model.py      # (re)creates exact/
    python3 exact_check.py [--sources e2_joint e4_sens ...]

Resumable: a row whose key is already in e2_exact.csv is skipped.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H          # noqa: E402
import exact_period as EP    # noqa: E402

EXACT_DIR = H.ROOT / "exact"
EXACT_MODEL = EXACT_DIR / "model" / "dse.mzn"
XTMP = H.TMP / "exact"
PATH = H.RESULTS / "e2_exact.csv"

SOURCES = ["e2_joint", "e4_sens", "e2_tight", "e2_thr", "e2_var"]
KEY = ["source", "variant", "instance", "factor", "baseline"]
FIELDS = KEY + ["objective", "period_ub", "bound",
                "old_status", "old_value", "old_mu", "old_seconds",
                "status", "value", "seconds", "mu_max", "total_cost",
                "nprocs", "patterns", "verify_ok", "pin_ok", "fix_ok",
                "mu_solver", "step1_status", "step1_total_cost",
                "old_step1_total_cost", "agree"]

_orig_solve = H.solve_run
_orig_wrapper = H.wrapper
_orig_ep_wrapper = EP._wrapper


def deactivate() -> None:
    """Back to the safedse model (used by the SL diagnosis)."""
    H.solve_run = _orig_solve
    H.wrapper = _orig_wrapper
    EP._wrapper = _orig_ep_wrapper


def activate(tmp: Path = XTMP) -> None:
    """Point every solve of harness.py and exact_period.py at variant B:
    plain solves (model=None), the fix wrappers and the pin wrapper. Can be
    imported by other scripts (breadth.py multiexact)."""
    if not EXACT_MODEL.exists():
        raise SystemExit(f"{EXACT_MODEL} missing; run exact_model.py")
    tmp.mkdir(parents=True, exist_ok=True)

    def solve_run(dzn, *a, model=None, **k):
        return _orig_solve(dzn, *a, model=model or str(EXACT_MODEL), **k)

    def wrapper(name: str) -> Path:
        dst = tmp / name
        dst.write_text((H.WRAPPERS / name).read_text().replace(
            'include "dse.mzn";', f'include "{EXACT_MODEL}";'))
        return dst

    H.solve_run = solve_run
    H.wrapper = wrapper
    EP._wrapper = lambda: wrapper("fix_design.mzn")


def _value(row: dict, objective: str):
    v = row.get("mu_max") if objective == "THROUGHPUT" else row.get("total_cost")
    return "" if v in (None, "") else int(v)


def _agree(old_status: str, old_value, status: str, value) -> str:
    """True/False when both runs are conclusive (OPTIMAL or UNSAT, also
    after SL's step 1), '' when either hit the time limit."""
    conclusive = {"OPTIMAL", "UNSAT"}
    s0 = old_status.removeprefix("STEP1_")
    s1 = status.removeprefix("STEP1_")
    if s0 not in conclusive or s1 not in conclusive:
        return ""
    return str(old_status == status and str(old_value) == str(value))


def _record(done: set, key: dict, objective: str, old: dict, bound: str,
            run) -> None:
    k = tuple(str(key[c]) for c in KEY)
    if k in done:
        return
    row = run()
    if "_solution" in row and objective != "THROUGHPUT":
        row.setdefault("total_cost", row["_solution"].get("total_cost"))
    value = _value(row, objective)
    old_value = _value(old, objective)
    out = dict(key, objective=objective, period_ub=old.get("period_ub", ""),
               bound=bound, old_status=old["status"], old_value=old_value,
               old_mu=old.get("mu_max", ""), old_seconds=old["seconds"],
               old_step1_total_cost=old.get("step1_total_cost", ""),
               status=row["status"], value=value,
               agree=_agree(old["status"], old_value, row["status"], value),
               **{f: row.get(f, "") for f in
                  ["seconds", "mu_max", "total_cost", "nprocs", "patterns",
                   "verify_ok", "pin_ok", "fix_ok", "mu_solver",
                   "step1_status", "step1_total_cost"]})
    H.append_row(PATH, FIELDS, out)
    done.add(k)
    flag = "" if out["agree"] in ("True", "") else "  <-- DIFFERS"
    print(f"x {' '.join(k)}: old {old['status']} {old_value} | B "
          f"{row['status']} {value} {row['seconds']}s "
          f"verify_ok={row.get('verify_ok')}{flag}", flush=True)


def _rows(src: str) -> list[dict]:
    with (H.RESULTS / f"{src}.csv").open(newline="") as f:
        return list(csv.DictReader(f))


def check_e2(src: str, done: set) -> None:
    """e2_joint, e2_tight, e2_thr, e2_var: same instance build (variant,
    stored period_ub), same baseline or flow, same objective."""
    rows = _rows(src)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        var = r.get("variant", "base")
        fac = r.get("factor", "none") if src != "e2_thr" else "none"
        groups.setdefault((var, r["instance"], fac), []).append(r)
    for (var, name, fac), rs in groups.items():
        spec = H.E2_INSTANCES[name]
        keys = [dict(source=src, variant=var, instance=name, factor=fac,
                     baseline=r.get("baseline") or r.get("flow")) for r in rs]
        if all(tuple(str(k[c]) for c in KEY) in done for k in keys):
            continue
        objective = "THROUGHPUT" if src == "e2_thr" else "TOTALCOST"
        if fac in ("none", "-"):
            ub, bound = None, ""
        else:
            ub = int(rs[0]["period_ub"])
            bound = f"period_ub={ub}"
        dz = H.build_e2_instances(name, spec, period_ub=ub, variant=var,
                                  suffix="" if ub is None else f"_t{fac}")
        d = H.parse_dzn(str(dz["joint"]))
        vspec = spec if var == "base" else H.variant_spec(spec, var)
        for r, key in zip(rs, keys):
            b = key["baseline"]
            tag = f"xB_{src}_{var}_{name}_{fac}_{b}"
            if b == "min_period":
                def run(dz=dz, d=d, tag=tag):
                    row = H.solve_and_verify(dz["joint"], "THROUGHPUT", tag=tag)
                    return H._e2_row(row, d)
                _record(done, key, "THROUGHPUT", r, bound, run)
            else:
                _record(done, key, objective, r, bound,
                        lambda b=b, dz=dz, d=d, tag=tag: H.run_baseline(
                            b, vspec, dz, d, tag, objective))


def check_e4(done: set) -> None:
    """e4_sens: the calls of harness.rq4, one per stored row."""
    for r in _rows("e4_sens"):
        g, inst, metric = r["group"], r["instance"], r["metric"]
        bounds = {}
        if r["bound_name"] == "NPROCS":
            bounds = {"NPROCS": int(r["bound_value"])}
        if g == "comm_model":
            dzn = H.loosen_period_ub(H.OUT / "pc_sobel.dzn", 100_000,
                                     XTMP / "pc_sobel_loose.dzn")
        else:
            dzn = H.OUT / f"{inst}.dzn"
        key = dict(source="e4_sens", variant=g, instance=inst,
                   factor=r["bound_value"] or "none", baseline="joint")
        bound = f"{r['bound_name']}={r['bound_value']}" if r["bound_name"] else ""
        tag = f"xB_e4_{g}_{inst}_{r['bound_value']}"
        _record(done, key, metric, r, bound,
                lambda dzn=dzn, tag=tag, bounds=bounds: H._with_totals(
                    H.solve_and_verify(dzn, metric, bounds, tag=tag,
                                       timeout=max(H.PY_TIMEOUT, 200))))


# ---------------------------------------------------------------------------
# SL diagnosis. SL's step 1 (no patterns) can have several optima; step 2
# fixes the binding of the one returned. A differing SL row can therefore
# come from a different step-1 binding, not from the model's period
# pessimism. For every SL row whose
# result differs, step 2 is re-solved crosswise: variant B with the stored
# step-1 binding of the model run, and the model with the step-1 binding of
# the variant-B run. -> e2_exact_sl.csv
# ---------------------------------------------------------------------------
SL_PATH = H.RESULTS / "e2_exact_sl.csv"
SL_FIELDS = KEY + ["objective", "old_value", "b_value", "same_binding",
                   "b_on_old_binding_status", "b_on_old_binding_value",
                   "b_on_old_binding_verify_ok", "old_on_b_binding_status",
                   "old_on_b_binding_value", "old_on_b_binding_verify_ok",
                   "cause"]
OLD_TAG = {"e2_joint": "e2_{name}_{b}", "e2_tight": "e2t_{name}_{fac}_{b}",
           "e2_thr": "e2thr_{var}_{name}_{b}",
           "e2_var": "e2v_{var}_{name}_{fac}_{b}"}


def _binding(dz: dict, d: dict, s1: dict) -> tuple[list[int], list[int]]:
    """The fixing of harness.run_baseline's SL step 2 for step-1 solution
    s1."""
    d1 = H.parse_dzn(str(dz["nopat"]))
    core_of = {nm: s1["proc"][i] for i, nm in enumerate(d1["node_name"])
               if not d1["comm_actor"][i]}
    nodes = [i + 1 for i, nm in enumerate(d["node_name"])
             if nm in core_of and not d["comm_actor"][i]]
    return nodes, [core_of[d["node_name"][i - 1]] for i in nodes]


def _step2(dz: dict, d: dict, fix: tuple, tag: str, objective: str) -> dict:
    nodes, procs = fix
    r = H.solve_and_verify(dz["joint"], objective, tag=tag,
                           model=str(H.wrapper("fix_binding.mzn")),
                           extra=f"fix_node = {H._mzn(nodes)}; "
                                 f"fix_proc = {H._mzn(procs)};")
    if "_solution" in r and objective != "THROUGHPUT":
        r["total_cost"] = r["_solution"].get("total_cost")
    return r


def sl_diag() -> None:
    import json
    done = H.existing_keys(SL_PATH, KEY)
    diag_tmp = XTMP / "sl_diag"
    for r in _rows_exact():
        if r["baseline"] != "SL" or r["agree"] != "False":
            continue
        k = tuple(r[c] for c in KEY)
        if k in done:
            continue
        src, var, name, fac, b = k
        spec = H.E2_INSTANCES[name]
        ub = None if fac in ("none", "-") else int(r["period_ub"])
        dz = H.build_e2_instances(name, spec, period_ub=ub, variant=var,
                                  suffix="" if ub is None else f"_t{fac}")
        d = H.parse_dzn(str(dz["joint"]))
        old_tag = OLD_TAG[src].format(name=name, fac=fac, var=var, b=b)
        s1_old = json.loads((H.TMP / f"{old_tag}_step1.sol.json").read_text())
        s1_b = json.loads((H.TMP / f"xB_{src}_{var}_{name}_{fac}_SL_step1"
                           ".sol.json").read_text())
        if (r["old_step1_total_cost"]
                and str(s1_old.get("total_cost")) != r["old_step1_total_cost"]):
            raise SystemExit(f"{old_tag}: stored step-1 solution does not "
                             f"match the CSV row (overwritten?)")
        fix_old, fix_b = _binding(dz, d, s1_old), _binding(dz, d, s1_b)
        obj = r["objective"]
        activate(diag_tmp)
        rb = _step2(dz, d, fix_old, f"xBdiag_{old_tag}", obj)
        deactivate()
        ro = _step2(dz, d, fix_b, f"xOdiag_{old_tag}", obj)
        vb, vo = _value(rb, obj), _value(ro, obj)
        out = dict(zip(KEY, k), objective=obj, old_value=r["old_value"],
                   b_value=r["value"], same_binding=fix_old == fix_b,
                   b_on_old_binding_status=rb["status"],
                   b_on_old_binding_value=vb,
                   b_on_old_binding_verify_ok=rb.get("verify_ok", ""),
                   old_on_b_binding_status=ro["status"],
                   old_on_b_binding_value=vo,
                   old_on_b_binding_verify_ok=ro.get("verify_ok", ""))
        out["cause"] = sl_cause(out, r)
        H.append_row(SL_PATH, SL_FIELDS, out)
        print(f"sl {' '.join(k)}: model {r['old_value']} / B {r['value']}; "
              f"B on model binding {vb}, model on B binding {vo} -> "
              f"{out['cause']}", flush=True)


def sl_cause(d: dict, r: dict) -> str:
    """d: a diagnosis row, r: its e2_exact row. The model's period pessimism
    shows as variant B below
    the model on the same binding; on the model's side the design at B's
    value then fails the verifier by period."""
    same_old = (str(d["b_on_old_binding_value"]) == str(r["old_value"])
                and d["b_on_old_binding_status"] == r["old_status"])
    same_b = (str(d["old_on_b_binding_value"]) == str(r["value"])
              and d["old_on_b_binding_status"] == r["status"])
    if str(d["same_binding"]) == "True":
        return "period-pessimism" if not (same_old and same_b) else "none"
    if same_old and same_b:
        return "step-1 tie"
    if same_old:
        return "step-1 tie; period-pessimism on the variant-B binding"
    if same_b:
        return "step-1 tie; period-pessimism on the model binding"
    return "unexplained"


def _rows_exact() -> list[dict]:
    with PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="*", default=SOURCES)
    ap.add_argument("--sl-diag", action="store_true",
                    help="only the SL diagnosis of differing rows")
    args = ap.parse_args()
    if args.sl_diag:
        sl_diag()
        return 0
    activate()
    done = H.existing_keys(PATH, KEY)
    for src in args.sources:
        if src == "e4_sens":
            check_e4(done)
        else:
            check_e2(src, done)
    return 0


if __name__ == "__main__":
    sys.exit(main())

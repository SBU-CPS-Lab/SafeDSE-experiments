#!/usr/bin/env python3
"""Cost-benefit evaluation of three model extensions (pattern-dependent doer
SIL, priced development multipliers, verdict tokens as variables). The
extensions live in the model copy modelext/ (modelext_build.py); SafeDSE is
never changed.

Configurations (the extension data this script writes per instance):
  off     neutral data: the copy must reproduce the model (validation)
  E1      pattern-dependent doer SIL (owner at actor SIL - sil_offset)
  E2      development multipliers of the catalog (diverse = 2.0)
  E12     E1 and E2
  E3k<k>  verdict tokens r in {1, 2} per actor with r * mu <= PST,
          PST = k * B (B = the run's period bound); k in {1, 1.5, 2}

Every solve, fix wrapper and pin step of harness.py uses the copy (as
exact_check.activate does for variant B). Every solution is checked by the
unmodified tools/verify.py against a *projected* instance: the selected
pattern's doer SIL written into sil_req_parent, the multipliers into dev_base,
the chosen verdict tokens into tok. So the verifier checks the design under
the extended semantics without knowing about them. PF-cheap and PF-light use
the extended prices in their keys (pattern_additions_modelext).

    python3 modelext.py validate
    python3 modelext.py run [--configs E12 E3k1 ...]

Resumable: one row per run in results/e8_modelext.csv, keyed by
(config, instance, factor, baseline, rep).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from functools import lru_cache
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H          # noqa: E402
import exact_period as EP    # noqa: E402

sys.path.insert(0, str(H.SAFEDSE / "tools"))
from patterns import slot_layout   # noqa: E402

MODELEXT_DIR = H.ROOT / "modelext"
MODELEXT_MODEL = MODELEXT_DIR / "model" / "dse.mzn"
DTMP = H.TMP / "modelext"
PATH = H.RESULTS / "e8_modelext.csv"

KEY = ["config", "instance", "factor", "baseline", "rep"]
FIELDS = KEY + ["period_ub", "pst", "status", "seconds", "total_cost",
                "mu_max", "nprocs", "hw_cost", "dev_cost", "promotion_cost",
                "partition_total", "pattern_cost", "patterns", "verify_ok",
                "fix_ok", "pin_ok", "step1_status", "doer_lowered", "r2"]
FACTORS = ["none"] + H.TIGHT
CONFIGS = {   # config: (E1, E2, kappa or None)
    "off": (False, False, None), "E1": (True, False, None),
    "E2": (False, True, None), "E12": (True, True, None),
    "E3k1": (False, False, 1.0), "E3k15": (False, False, 1.5),
    "E3k2": (False, False, 2.0)}
# flows and repetitions per configuration
PLAN = {
    "E1": (["joint"], 1),
    "E2": (["joint"], 1),
    "E12": (["joint", "SL", "PFmin", "PFmax", "PFlight", "PFcheap", "NP"], 3),
    "E3k1": (["joint", "SL", "PFmin", "PFmax", "PFlight", "PFcheap"], 1),
    "E3k15": (["joint", "SL", "PFmin", "PFmax", "PFlight", "PFcheap"], 1),
    "E3k2": (["joint"], 1),
}
CUR: dict = {"cfg": "off", "cat": None}


# ---------------------------------------------------------------------------
# extension data per instance file
# ---------------------------------------------------------------------------
def _l(x):
    return x if isinstance(x, list) else [x]


def _grid(flat, ncol):
    flat = _l(flat)
    if flat and isinstance(flat[0], list):
        return flat
    return [flat[r * ncol:(r + 1) * ncol] for r in range(len(flat) // ncol)]


@lru_cache(maxsize=None)
def _catalog(cat: str) -> dict:
    return {p["id"]: p for p in yaml.safe_load(
        (H.SAFEDSE / cat).read_text())["patterns"]}


@lru_cache(maxsize=None)
def ext_info(dzn: str, cat: str, cfg: str, mtime: float) -> dict:
    """doer offsets, multipliers (x100) per (node, pattern), verdict edges"""
    e1, e2, kappa = CONFIGS[cfg]
    d = H.parse_dzn(dzn)
    n, names = d["n"], _l(d["pat_name"])
    npat = len(names)
    recs = _catalog(cat)
    off = [0] * npat
    if e1:
        off = [max([int(c.get("sil_offset", 0)) for c in
                    recs[nm].get("components", [])] or [0])
               if nm in recs else 0 for nm in names]
    devm = [[100] * npat for _ in range(n)]
    parent, owner = _l(d["parent"]), _l(d["par_owner"])
    nown, node_name = _l(d["node_owner"]), _l(d["node_name"])
    pname = _l(d["parent_name"])
    if e2 and npat > 1:
        allowed = _grid(d["pat_allowed"], npat)
        npl = d.get("nPL", 0)
        pl = list(zip(_l(d["pl_u"]), _l(d["pl_v"]), _l(d["pl_rel"]),
                      _l(d["pl_owner"]), _grid(d["pl_guard"], npat))) \
            if npl else []
        for i in range(n):
            nm = node_name[i].split(".", 1)[-1]
            if "~" not in nm or nm.startswith("__"):
                continue
            a = nown[i]                       # owner's PAR index (1-based)
            slot = int(nm.split("~", 1)[1].split("#", 1)[0])
            app = [names[k] for k in range(npat) if allowed[a - 1][k]]
            if any(x not in recs for x in app):
                raise SystemExit(f"{dzn}: pattern not in {cat}")
            layout = slot_layout([_PatShim(recs[x]) for x in app])[1]
            for j, x in enumerate(app):
                k = names.index(x)
                comps = recs[x].get("components", [])
                if slot not in layout[j]:
                    continue
                role = comps[layout[j].index(slot)]["role"]
                m = float(recs[x].get("dev_cost_multiplier", {})
                          .get(role, 1.0))
                div = recs[x].get("dev_cost_multiplier_diverse", {})
                if role in div and any(
                        rel == 4 and o == a and g[k] and i + 1 in (u, v)
                        for u, v, rel, o, g in pl):
                    m = float(div[role])
                devm[i][k] = round(100 * m)
    vd = []
    if kappa is not None and npat > 1:
        is_owner_node = [owner[parent[i] - 1] == parent[i] for i in range(n)]
        tok = _grid(d["tok"], n)
        for s_, t_, k_, o_ in zip(_l(d["pe_src"]), _l(d["pe_dst"]),
                                  _l(d["pe_tok"]), _l(d["pe_owner"])):
            if k_ >= 1 and is_owner_node[t_ - 1] and parent[t_ - 1] == o_ \
                    and (s_, t_, o_) not in [(a, b, c) for a, b, c in vd]:
                if tok[s_ - 1][t_ - 1] != k_:
                    raise SystemExit(f"{dzn}: tok/pe_tok differ on "
                                     f"{s_}->{t_}")
                vd.append((s_, t_, o_))
    ub = _l(d["period_ub"])
    pst = [0] * len(pname)
    if vd:
        b = max(ub)
        for _, _, o in vd:
            pst[o - 1] = int(kappa * b)
    return dict(n=n, npat=npat, off=off, devm=devm, vd=vd, pst=pst,
                pst_value=(int(kappa * max(ub)) if vd else ""),
                names=names)


class _PatShim:
    """slot_layout only reads .components"""
    def __init__(self, rec: dict):
        self.components = rec.get("components", [])


def _mzn(xs) -> str:
    return "[" + ", ".join(str(x) for x in xs) + "]"


def ext_data(dzn: str) -> str:
    p = Path(dzn)
    x = ext_info(str(p), CUR["cat"], CUR["cfg"], p.stat().st_mtime)
    vd = x["vd"] or [(1, 1, 1)]           # a dummy row, never posted
    flat = [v for row in x["devm"] for v in row]
    return (f"pat_doer_off = {_mzn(x['off'])}; "
            f"node_devm = array2d(1..{x['n']}, 1..{x['npat']}, {_mzn(flat)}); "
            f"nVD = {len(x['vd'])}; vd_src = {_mzn([a for a, _, _ in vd])}; "
            f"vd_dst = {_mzn([b for _, b, _ in vd])}; "
            f"vd_owner = {_mzn([c for _, _, c in vd])}; "
            f"vd_rmax = {2 if x['vd'] else 1}; vd_pst = {_mzn(x['pst'])};")


# ---------------------------------------------------------------------------
# projection of a solution's instance (for the unmodified verifier)
# ---------------------------------------------------------------------------
def _set_param(txt: str, name: str, value: str) -> str:
    new, k = re.subn(rf"(?ms)^{name} = .*?;[ \t]*$", f"{name} = {value};",
                     txt, count=1)
    if k != 1:
        raise SystemExit(f"projection: no parameter {name}")
    return new


def project(dzn: str, sol: dict, tag: str) -> Path:
    p = Path(dzn)
    x = ext_info(str(p), CUR["cat"], CUR["cfg"], p.stat().st_mtime)
    d = H.parse_dzn(str(p))
    n = d["n"]
    pat = _l(sol.get("pat", [1] * len(_l(d["par_owner"]))))
    txt = p.read_text()
    owner = _l(d["par_owner"])
    req = list(_l(d["sil_req_parent"]))
    for a in range(len(req)):
        if owner[a] == a + 1:
            req[a] = max(0, req[a] - x["off"][pat[a] - 1])
    txt = _set_param(txt, "sil_req_parent", _mzn(req))
    nown = _l(d["node_owner"])
    base = []
    for i, b in enumerate(_l(d["dev_base"])):
        m = x["devm"][i][pat[nown[i] - 1] - 1]
        if (b * m) % 100:
            raise SystemExit(f"projection: dev_base {b} x {m}/100 not integer")
        base.append(b * m // 100)
    txt = _set_param(txt, "dev_base", _mzn(base))
    if x["vd"]:
        tok = _grid(d["tok"], n)
        r = _l(sol["vd_r"])
        for s_, t_, o in x["vd"]:
            tok[s_ - 1][t_ - 1] = r[o - 1]
        txt = _set_param(txt, "tok", f"array2d(1..{n}, 1..{n}, "
                         f"{_mzn([v for row in tok for v in row])})")
    DTMP.mkdir(parents=True, exist_ok=True)
    out = DTMP / f"{tag}.proj.dzn"
    out.write_text(txt)
    return out


# ---------------------------------------------------------------------------
# point harness.py at the copy
# ---------------------------------------------------------------------------
_orig_solve = H.solve_run
_orig_verify = H.verify_solution
_orig_pa = H.pattern_additions


def _is_modelext(model) -> bool:
    return model is None or str(model).startswith(str(DTMP)) \
        or str(model) == str(MODELEXT_MODEL)


def activate() -> None:
    if not MODELEXT_MODEL.exists():
        raise SystemExit(f"{MODELEXT_MODEL} missing; run modelext_build.py")
    DTMP.mkdir(parents=True, exist_ok=True)
    H.E2_DIR = DTMP / "e2"        # own build directory (no clash with runs)

    def solve_run(dzn, *a, model=None, extra="", **k):
        if _is_modelext(model):
            extra = f"{extra} {ext_data(str(dzn))}"
            model = model or str(MODELEXT_MODEL)
        return _orig_solve(dzn, *a, model=model, extra=extra, **k)

    def wrapper(name: str) -> Path:
        H.E2_DIR.mkdir(parents=True, exist_ok=True)
        dst = DTMP / name
        dst.write_text((H.WRAPPERS / name).read_text().replace(
            'include "dse.mzn";', f'include "{MODELEXT_MODEL}";'))
        return dst

    def ep_wrapper() -> Path:
        dst = DTMP / "fix_design.mzn"
        dst.write_text(EP.WRAPPER_SRC.read_text().replace(
            'include "dse.mzn";', f'include "{MODELEXT_MODEL}";'))
        return dst

    def verify_solution(dzn, solution, tag):
        return _orig_verify(project(str(dzn), solution, tag), solution, tag)

    H.solve_run = solve_run
    EP.solve_run = solve_run
    H.wrapper = wrapper
    EP._wrapper = ep_wrapper
    H.verify_solution = verify_solution
    H.pattern_additions = pattern_additions_modelext


def pattern_additions_modelext(d: dict, a: int, k: int) -> tuple[int, int]:
    """harness.pattern_additions with the extended prices: added components
    at their multiplier, minus what the lower doer SIL saves (E1)."""
    w, c = _orig_pa(d, a, k)
    x = CUR["info"]
    n = d["n"]
    dk = H._dev_k(d)
    extra = 0
    comm = _l(d.get("comm_actor", [False] * n))
    for i in range(n):
        if d["node_owner"][i] != a or (i < len(comm) and comm[i]):
            continue
        sil = d["sil_req_parent"][d["parent"][i] - 1]
        if d["owner_node"][i] != i + 1:            # an added component
            if d["node_guard"][i * x["npat"] + k]:
                extra += d["dev_base"][i] * dk[sil] * (x["devm"][i][k] - 100) \
                    // 10000
        else:                                      # the owner itself
            low = max(0, sil - x["off"][k])
            extra -= d["dev_base"][i] * (dk[sil] - dk[low]) // 100
    return w, c + extra


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------
def _mu_star() -> dict[str, int]:
    out = {}
    with (H.RESULTS / "e2_tight.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            if r["baseline"] == "min_period" and r["verify_ok"] == "True":
                out[r["instance"]] = int(r["mu_max"])
    return out


def _row_extra(row: dict, d: dict, cfg: str) -> None:
    s = row.get("_solution")
    if not s:
        return
    owner = _l(d["par_owner"])
    req = _l(d["sil_req_parent"])
    par = _l(d["parent"])
    act = _l(s["active"])
    comm = _l(d.get("comm_actor", [False] * d["n"]))
    comm += [False] * (d["n"] - len(comm))
    row["doer_lowered"] = sum(
        1 for i in range(d["n"]) if act[i] and not comm[i]
        and owner[par[i] - 1] == par[i]
        and s["sil_impl"][i] < req[par[i] - 1])
    row["r2"] = sum(1 for v in _l(s.get("vd_r", [])) if v == 2)


def run(configs: list[str], only: list[str] | None) -> None:
    activate()
    done = H.existing_keys(PATH, KEY)
    mus = _mu_star()
    for cfg in configs:
        flows, reps = PLAN[cfg]
        kappa = CONFIGS[cfg][2]
        CUR["cfg"] = cfg
        for name, spec in H.E2_INSTANCES.items():
            if only and name not in only:
                continue
            CUR["cat"] = spec["catalogue"]
            for fac in FACTORS:
                if kappa is not None and fac == "none":
                    continue          # PST is defined relative to a bound
                if fac != "none" and name not in mus:
                    continue
                keys = [(cfg, name, fac, b, str(k)) for b in flows
                        for k in range(1, (reps if b == "joint" else 1) + 1)]
                if all(k in done for k in keys):
                    continue
                ub = None if fac == "none" else H._ub(mus[name], fac)
                dz = H.build_e2_instances(name, spec, period_ub=ub,
                                          suffix="" if ub is None
                                          else f"_t{fac}")
                d = H.parse_dzn(str(dz["joint"]))
                p = Path(dz["joint"])
                CUR["info"] = ext_info(str(p), CUR["cat"], cfg,
                                       p.stat().st_mtime)
                for key in keys:
                    if key in done:
                        continue
                    b, rep = key[3], key[4]
                    tag = f"modelext_{cfg}_{name}_{fac}_{b}_{rep}"
                    t0 = time.time()
                    row = H.run_baseline(b, spec, dz, d, tag)
                    _row_extra(row, d, cfg)
                    row.update(config=cfg, instance=name, factor=fac,
                               baseline=b, rep=rep,
                               period_ub=ub or spec.get("period_ub") or "",
                               pst=CUR["info"]["pst_value"])
                    H.append_row(PATH, FIELDS, row)
                    print(f"modelext {cfg} {name} {fac} {b} {rep}: {row['status']}"
                          f" {row['seconds']}s total={row.get('total_cost')}"
                          f" mu={row.get('mu_max')} low={row.get('doer_lowered')}"
                          f" r2={row.get('r2')} verify={row['verify_ok']}"
                          f" ({time.time() - t0:.0f}s)", flush=True)


def validate() -> None:
    """The copy with neutral data must give the stored optimum (joint, no
    bound and 1.25 mu*) on every instance."""
    activate()
    CUR["cfg"] = "off"
    mus = _mu_star()
    ref = {}
    for src, fac_col in [("e2_joint", None), ("e2_tight", "factor")]:
        with (H.RESULTS / f"{src}.csv").open(newline="") as f:
            for r in csv.DictReader(f):
                if r["baseline"] == "joint":
                    ref[(r["instance"], r[fac_col] if fac_col else "none")] = r
    bad = 0
    for name, spec in H.E2_INSTANCES.items():
        CUR["cat"] = spec["catalogue"]
        for fac in ["none", "1.25"]:
            ub = None if fac == "none" else H._ub(mus[name], fac)
            dz = H.build_e2_instances(name, spec, period_ub=ub,
                                      suffix="" if ub is None else f"_t{fac}")
            d = H.parse_dzn(str(dz["joint"]))
            p = Path(dz["joint"])
            CUR["info"] = ext_info(str(p), CUR["cat"], "off",
                                   p.stat().st_mtime)
            row = H.run_baseline("joint", spec, dz, d,
                                 f"modelext_off_{name}_{fac}")
            r0 = ref[(name, fac)]
            same = str(row.get("total_cost")) == r0["total_cost"]
            bad += not same or row["verify_ok"] is not True
            print(f"validate {name} {fac}: {row['status']} "
                  f"{row.get('total_cost')} (stored {r0['total_cost']}) "
                  f"verify={row['verify_ok']} {'OK' if same else 'DIFFERS'}",
                  flush=True)
    print(f"validate: {bad} problems")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["validate", "run"])
    ap.add_argument("--configs", nargs="+", default=list(PLAN),
                    choices=list(PLAN))
    ap.add_argument("--instances", nargs="*")
    a = ap.parse_args()
    t0 = time.time()
    if a.what == "validate":
        validate()
    else:
        run(a.configs, a.instances)
    print(f"done in {time.time() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

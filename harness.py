#!/usr/bin/env python3
"""Resumable experiment harness: RQ1, RQ2 (all variants), RQ3, RQ4.

Every run is (a) solved with a fixed solver thread count and a MiniZinc
--time-limit so the best incumbent survives a timeout, and
(b) independently re-checked with tools/verify.py; a run that fails the
verifier is still recorded (verify_ok=False) so it is visible, but
make_macros.py never turns it into a macro (no VERIFY OK, no number in a
table).

One CSV row is appended and flushed per finished run. A row is identified by
its leading key columns (instance/config/rep, or metric/nprocs for the
consolidation sweep); re-running the harness skips rows already present, so
`python3 harness.py rq1 rq4` is safe to interrupt and resume.

Threads: 4 CP-SAT workers per solve (`THREADS`), passed by tools/solve.py as
-p 4 plus `--params num_workers:4` (fzn-cp-sat otherwise runs 8 workers for
any -p > 1). With a parallel portfolio mu is reported anywhere between the
MCR and period_ub whenever it is not the objective (the model only guarantees
MCR <= mu <= period_ub; at -p 1 CP-SAT runs FIXED_SEARCH and the
`indomain_min` annotation happens to pick the least mu). So every
non-THROUGHPUT solution is passed through exact_period.pin_period
(wrappers/fix_design.mzn: the design fixed, mu minimised) before
verification, and the reported period is the design's least period at any
thread count; `mu_solver` keeps the value of the cost solve.

    python3 harness.py rq1
    python3 harness.py rq2
    python3 harness.py rq2tight
    python3 harness.py rq4
    python3 harness.py rq1 rq4 --time-limit 300000
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import SAFEDSE  # noqa: E402
sys.path.insert(0, str(SAFEDSE / "tools"))
from solve import METRICS, run as solve_run  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from exact_period import pin_period  # noqa: E402

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
TMP = RESULTS / "tmp"
OUT = SAFEDSE / "out"

THREADS = 4          # see module docstring
TIME_LIMIT_MS = 300_000   # 5 min; every instance here solves in seconds
PY_TIMEOUT = TIME_LIMIT_MS // 1000 + 60   # safety margin around the mzn limit


# ---------------------------------------------------------------------------
# generic CSV + verifier plumbing
# ---------------------------------------------------------------------------
def existing_keys(path: Path, keycols: list[str]) -> set[tuple]:
    if not path.exists():
        return set()
    with path.open(newline="") as f:
        return {tuple(row[k] for k in keycols) for row in csv.DictReader(f)}


def append_row(path: Path, fieldnames: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fieldnames})
        f.flush()


def verify_solution(dzn: Path, solution: dict, tag: str) -> dict:
    """Run tools/verify.py out-of-process and return its structured report.

    Never re-implements a check here: the report IS the independent
    verifier's opinion, by construction (verify.py shares no code with the
    model).
    """
    TMP.mkdir(parents=True, exist_ok=True)
    solp = TMP / f"{tag}.sol.json"
    repp = TMP / f"{tag}.report.json"
    solp.write_text(json.dumps(solution))
    subprocess.run([sys.executable, str(SAFEDSE / "tools" / "verify.py"),
                     "--dzn", str(dzn), "--solution", str(solp),
                     "--quiet", "--json-report", str(repp)],
                    check=False)
    if repp.exists():
        return json.loads(repp.read_text())
    return {"ok": False, "messages": ["verify.py produced no report"],
            "checks": []}


def _pinned(dzn: Path, optimise: str, s: dict, row: dict) -> dict:
    """For a non-THROUGHPUT objective, replace the solution by the same
    design at its least period (exact_period.pin_period) and record both.
    If pinning fails, the original solution is kept and verified as is."""
    mu = s["mu"] if isinstance(s["mu"], list) else [s["mu"]]
    row["mu_solver"] = max(mu)
    if optimise == "THROUGHPUT":
        return s
    pin = pin_period(dzn, s)
    row.update(pin_status=pin["status"], pin_seconds=pin["seconds"],
               pin_ok=pin["ok"])
    return pin["solution"] if pin["ok"] else s


def solve_and_verify(dzn: Path, optimise: str, bounds: dict | None = None,
                      tag: str | None = None, timeout: int = PY_TIMEOUT,
                      **extra) -> dict:
    r = solve_run(str(dzn), optimise, bounds or {}, timeout=timeout,
                   threads=THREADS, time_limit_ms=TIME_LIMIT_MS, **extra)
    row = {"status": r["status"], "seconds": round(r.get("seconds", 0.0), 3)}
    if "solution" in r:
        s = _pinned(dzn, optimise, r["solution"], row)
        mu = s["mu"] if isinstance(s["mu"], list) else [s["mu"]]
        row.update(mu_max=max(mu), mu_list=json.dumps(mu),
                   nprocs=s.get("nprocs"), hw_cost=s.get("hw_cost"),
                   dev_cost=s.get("dev_cost"),
                   promotion_cost=s.get("promotion_cost"),
                   power=s.get("power"))
        report = verify_solution(dzn, s, tag or Path(dzn).stem)
        row["verify_ok"] = report["ok"]
        row["_solution"] = s
        row["_report"] = report
    else:
        row["verify_ok"] = ""
    return row


def loosen_period_ub(dzn: Path, new_ub: int, dst: Path) -> Path:
    """out/pc_sobel.dzn carries Rosvall's published bound (400) as period_ub,
    which the SIL-3 pattern + TDMA composition cannot reach at all; tests/
    run_tests.py loosens it the same way before asking for the true minimum.
    """
    txt = dzn.read_text()
    txt2 = re.sub(r"^period_ub = .*$", f"period_ub = [{new_ub}];", txt,
                  flags=re.M)
    dst.write_text(txt2)
    return dst


# ---------------------------------------------------------------------------
# RQ1 -- fidelity: safety off, one-step vs two-step
# ---------------------------------------------------------------------------
E1_FIELDS = ["instance", "config", "rep", "status", "seconds",
             "step1_seconds", "step2_seconds", "mu_max", "mu_list",
             "nprocs", "hw_cost", "dev_cost", "promotion_cost", "power",
             "verify_ok", "published_bound", "mu_solver", "pin_status",
             "pin_seconds", "pin_ok"]

PUBLISHED_BOUND = {"r_a_sobel": 400, "r_b_susan": 2050, "r_c_rasta": 550}


def rq1(reps: int) -> None:
    path = RESULTS / "e1_parity.csv"
    done = existing_keys(path, ["instance", "config", "rep"])

    for stem, bound in PUBLISHED_BOUND.items():
        for rep in range(reps):
            key = (stem, "onestep", str(rep))
            if key in done:
                continue
            row = solve_and_verify(OUT / f"{stem}.dzn", "HWCOST",
                                    tag=f"e1_{stem}_{rep}")
            row.update(instance=stem, config="onestep", rep=rep,
                       published_bound=bound)
            append_row(path, E1_FIELDS, row)
            print(f"e1 {stem} onestep rep{rep}: {row['status']} "
                  f"{row['seconds']}s verify_ok={row['verify_ok']}")

    # One-step r_all: repeated like every other timing, because a 4-worker
    # portfolio is not deterministic.
    for rep in range(reps):
        key = ("r_all", "onestep", str(rep))
        if key in done:
            continue
        row = solve_and_verify(OUT / "r_all.dzn", "HWCOST",
                                tag=f"e1_r_all_onestep_{rep}")
        row.update(instance="r_all", config="onestep", rep=rep,
                   published_bound="")
        append_row(path, E1_FIELDS, row)
        print(f"e1 r_all onestep rep{rep}: {row['status']} "
              f"{row['seconds']}s verify_ok={row['verify_ok']}", flush=True)

    for rep in range(reps):
        key2 = ("r_all", "twostep", str(rep))
        if key2 not in done:
            t_row = twostep(OUT / "r_all_part.dzn", OUT / "r_all.dzn",
                            "HWCOST", tag=f"e1_r_all_twostep_{rep}")
            t_row.update(instance="r_all", config="twostep", rep=rep,
                        published_bound="")
            append_row(path, E1_FIELDS, t_row)
            print(f"e1 r_all twostep rep{rep}: {t_row['status']} "
                  f"total={t_row['seconds']}s "
                  f"(step1={t_row['step1_seconds']}s "
                  f"step2={t_row['step2_seconds']}s) "
                  f"verify_ok={t_row['verify_ok']}")


def twostep(partitioned: Path, full: Path, optimise: str, tag: str) -> dict:
    """Mirrors tools/twostep.py's logic (step 1 bounds step 2) but returns a
    harness row instead of printing to stdout, and verifies step 2's result.
    """
    r1 = solve_run(str(partitioned), optimise, timeout=PY_TIMEOUT,
                   threads=THREADS, time_limit_ms=TIME_LIMIT_MS)
    if "solution" not in r1:
        row = solve_and_verify(full, optimise, tag=tag)
        row["step1_seconds"] = round(r1.get("seconds", 0.0), 3)
        row["step2_seconds"] = row["seconds"]
        return row
    obj = r1["solution"]["metric"][METRICS.index(optimise)]
    row = solve_and_verify(full, optimise, {optimise: obj}, tag=tag)
    row["step1_seconds"] = round(r1["seconds"], 3)
    row["step2_seconds"] = row["seconds"]
    row["seconds"] = round(r1["seconds"] + row["seconds"], 3)
    return row


# ---------------------------------------------------------------------------
# RQ4 -- cost profile, fault model, communication model, consolidation, GSN
# ---------------------------------------------------------------------------
E4_FIELDS = ["group", "instance", "metric", "bound_name", "bound_value",
             "status", "seconds", "mu_max", "mu_list", "nprocs", "hw_cost",
             "dev_cost", "promotion_cost", "power", "verify_ok", "mu_solver",
             "pin_status", "pin_seconds", "pin_ok", "total_cost",
             "partition_total", "pattern_cost"]


def _with_totals(row: dict) -> dict:
    s = row.get("_solution")
    if s:
        row.update(total_cost=s.get("total_cost"),
                   partition_total=s.get("partition_total"),
                   pattern_cost=s.get("pattern_cost"))
    return row


def rq4(reps: int) -> None:
    path = RESULTS / "e4_sens.csv"
    done = existing_keys(path, ["group", "instance", "bound_name",
                                "bound_value"])

    # -- cost profile sensitivity: same instance, three cost profiles -------
    for prof in ["myklebust2015", "klosterman", "do178b"]:
        stem = f"x_{prof}"
        key = ("cost_profile", stem, "", "")
        if key in done:
            continue
        row = _with_totals(solve_and_verify(OUT / f"{stem}.dzn", "TOTALCOST",
                                            tag=f"e4_{stem}"))
        row.update(group="cost_profile", instance=stem, metric="TOTALCOST",
                   bound_name="", bound_value="")
        append_row(path, E4_FIELDS, _with_totals(row))
        print(f"e4 cost_profile {stem}: {row['status']} nprocs={row.get('nprocs')} "
              f"verify_ok={row['verify_ok']}")

    # -- cost profile x consolidation: the same three instances
    # with the core count bounded, so the figure shows the cost of each
    # consolidation level under each profile, not only the unbounded optimum
    for prof in ["myklebust2015", "klosterman", "do178b"]:
        stem = f"x_{prof}"
        for k in [3, 2, 1]:
            key = ("profile_consolidation", stem, "NPROCS", str(k))
            if key in done:
                continue
            row = _with_totals(solve_and_verify(
                OUT / f"{stem}.dzn", "TOTALCOST", {"NPROCS": k},
                tag=f"e4_{stem}_n{k}"))
            row.update(group="profile_consolidation", instance=stem,
                       metric="TOTALCOST", bound_name="NPROCS", bound_value=k)
            append_row(path, E4_FIELDS, _with_totals(row))
            print(f"e4 profile_consolidation {stem} NPROCS<={k}: "
                  f"{row['status']} total={row.get('total_cost')} "
                  f"promotion={row.get('promotion_cost')} "
                  f"verify_ok={row['verify_ok']}")

    # -- fault model / placement sensitivity ---------------------------------
    for fm in ["random_hw", "systematic_sw"]:
        stem = f"d_{fm}"
        key = ("fault_model", stem, "", "")
        if key in done:
            continue
        row = solve_and_verify(OUT / f"{stem}.dzn", "TOTALCOST",
                                tag=f"e4_{stem}", timeout=max(PY_TIMEOUT, 150))
        row.update(group="fault_model", instance=stem, metric="TOTALCOST",
                   bound_name="", bound_value="")
        append_row(path, E4_FIELDS, _with_totals(row))
        print(f"e4 fault_model {stem}: {row['status']} nprocs={row.get('nprocs')} "
              f"verify_ok={row['verify_ok']}")

    # -- consolidation sweep (new): promotion cost vs core count -------------
    for k in [3, 2, 1]:
        key = ("consolidation", "s_myklebust2015", "NPROCS", str(k))
        if key in done:
            continue
        row = solve_and_verify(OUT / "s_myklebust2015.dzn", "TOTALCOST",
                                {"NPROCS": k}, tag=f"e4_consolidation_{k}")
        row.update(group="consolidation", instance="s_myklebust2015",
                   metric="TOTALCOST", bound_name="NPROCS", bound_value=k)
        append_row(path, E4_FIELDS, _with_totals(row))
        print(f"e4 consolidation NPROCS<={k}: {row['status']} "
              f"promotion_cost={row.get('promotion_cost')} "
              f"verify_ok={row['verify_ok']}")

    # -- communication model: safety pattern + TDMA on a_sobel --------------
    key = ("comm_model", "pc_sobel", "period_ub", "100000")
    if key not in done:
        loose = loosen_period_ub(OUT / "pc_sobel.dzn", 100_000,
                                 TMP / "pc_sobel_loose.dzn")
        row = solve_and_verify(loose, "THROUGHPUT", tag="e4_pc_sobel",
                                timeout=max(PY_TIMEOUT, 200))
        row.update(group="comm_model", instance="pc_sobel",
                   metric="THROUGHPUT", bound_name="period_ub",
                   bound_value=100_000)
        append_row(path, E4_FIELDS, _with_totals(row))
        print(f"e4 comm_model pc_sobel: {row['status']} mu={row.get('mu_max')} "
              f"verify_ok={row['verify_ok']}")

    # -- explicit-voter NVP case study (its catalogue is separate from
    # data/patterns.yaml) ------------------------------------------------------
    key = ("nvp", "v_nvp", "", "")
    if key not in done:
        row = solve_and_verify(OUT / "v_nvp.dzn", "TOTALCOST", tag="e4_v_nvp",
                                timeout=max(PY_TIMEOUT, 200))
        row.update(group="nvp", instance="v_nvp", metric="TOTALCOST",
                   bound_name="", bound_value="")
        append_row(path, E4_FIELDS, _with_totals(row))
        print(f"e4 nvp v_nvp: {row['status']} mu={row.get('mu_max')} "
              f"nprocs={row.get('nprocs')} verify_ok={row['verify_ok']}")

    gsn_argument_stats()


def gsn_argument_stats() -> None:
    """Generates a fresh, verified GSN argument for the fault-model pruning
    comparison (f_random_hw vs f_both3) and for the v_nvp case study, and
    writes e4_gsn.csv. Delegated to gsn_stats.py so the statistics extraction
    (elements/goals/undeveloped/solutions/checks/DIVERSE records) lives in one
    place and is reused for the GSN figure (make_gsn_figure.py).
    """
    import gsn_stats
    gsn_stats.main()


# ---------------------------------------------------------------------------
# RQ2 -- joint exploration vs decoupled baselines
#
# Every baseline is the joint model plus extra constraints (wrappers/*.mzn) or
# a front-end input change; the model itself is never edited. All of them
# optimise TOTALCOST, the same objective as the joint run, so each baseline
# can only match or lose against it (a baseline that beats joint means joint
# timed out, or a bug).
#
#   joint   the instance as built by tools/build_all.sh
#   SL      safety-last: step 1 solves the same inputs with
#           --force-no-patterns (SIL provisioning and promotion still apply,
#           but no structural pattern); step 2 fixes every base actor to its
#           step-1 core (wrappers/fix_binding.mzn) and lets patterns,
#           replicas and the platform adapt
#   PFmin   pattern-first: fix pat[a] before mapping to the allowed pattern
#           with the fewest components (wrappers/fix_pattern.mzn)
#   PFmax   pattern-first: fix pat[a] to the allowed pattern with the widest
#           fault coverage (wrappers/fix_pattern.mzn)
#   NP      no promotion: allow_promotion="false" in the safety XML, rebuilt
#           through the front-end
#
# The allowed set per actor is the joint instance's pat_allowed row, which the
# front-end already restricts to the actor's SIL, the fault model and what the
# platform can place. Tie-breaks are fixed so the choice is reproducible and
# uses catalogue attributes only (a pattern-first designer has no mapping):
#   PFmin key: (#components, recurring cost, sum of dev multipliers, order)
#   PFmax key: (-#covered fault classes, #components, recurring cost,
#               sum of dev multipliers, catalogue order)
# ---------------------------------------------------------------------------
from patterns import load_patterns   # noqa: E402  (safedse/tools)
from verify import parse_dzn         # noqa: E402  (safedse/tools)

WRAPPERS = ROOT / "wrappers"
E2_DIR = TMP / "e2"

_CR = ["--app", "data/apps/c_rasta.hsdf.xml"]
_COMMON = ["--constraints", "data/desConst.xml",
           "--cost-model", "data/cost_model.xml"]
_M = ["--platform", "data/platform/mixed.xml",
      "--wcets", "data/WCETs_mixed.xml"] + _COMMON
_NI = ["--platform", "data/platform/mixed_noiso.xml",
       "--wcets", "data/WCETs_mixed.xml"] + _COMMON
_T3 = ["--platform", "data/platform/mixed_3type.xml",
       "--wcets", "data/WCETs_3type.xml"] + _COMMON
_PAT = "data/patterns.yaml"

# Unique pattern instances, with the build_dzn.py arguments of
# safedse/tools/build_all.sh. Two pairs in out/ are byte-identical and are run
# once (aliases): f_random_hw == p_rasta_noiso, d_systematic_sw == p_sw.
# p_none is not a pattern instance (it is built with --force-no-patterns).
E2_INSTANCES = {
    "p_rasta":         dict(args=_CR + _M,  safety="data/safety_rasta.xml",
                            catalogue=_PAT, aliases=""),
    "f_random_hw":     dict(args=_CR + _NI, safety="data/safety_fm_random_hw.xml",
                            catalogue=_PAT, aliases="p_rasta_noiso"),
    "d_random_hw":     dict(args=_CR + _NI, safety="data/safety_rasta_random_hw.xml",
                            catalogue=_PAT, aliases=""),
    "d_systematic_sw": dict(args=_CR + _NI,
                            safety="data/safety_rasta_systematic_sw.xml",
                            catalogue=_PAT, aliases="p_sw"),
    "f_sw3":           dict(args=_CR + _T3, safety="data/safety_fm_systematic_sw.xml",
                            catalogue=_PAT, aliases=""),
    "f_both3":         dict(args=_CR + _T3, safety="data/safety_fm_both.xml",
                            catalogue=_PAT, aliases=""),
    "c_pat":           dict(args=_CR + _NI + ["--comm", "tdma"],
                            safety="data/safety_rasta.xml",
                            catalogue=_PAT, aliases=""),
    # Rosvall's bound (400) is unreachable once get_pixel carries a SIL-3
    # pattern and TDMA is modelled, so every variant is built with the bound
    # RQ4 (comm_model) uses; throughput is then not binding here. The c_rasta
    # instances declare period="-1" (data/desConst.xml), so their period_ub
    # is the front-end's trivial all-on-one-slowest-core bound: throughput is
    # not binding on any RQ2 instance (rq2tight adds bound variants).
    "pc_sobel":        dict(args=["--app", "data/rosvall/a_sobel.hsdf.xml",
                                  "--platform", "data/rosvall/platform.xml",
                                  "--wcets", "data/rosvall/WCETs_pat.xml",
                                  "--constraints", "data/rosvall/desConst.xml",
                                  "--cost-model", "data/cost_model.xml",
                                  "--comm", "tdma"],
                            safety="data/safety_sobel3.xml",
                            catalogue=_PAT, aliases="", period_ub=100_000),
    # NVP is a case study with its own catalogue.
    "v_nvp":           dict(args=_CR + ["--platform", "data/platform/mixed_4type.xml",
                                        "--wcets", "data/WCETs_4type.xml"] + _COMMON,
                            safety="data/safety_fm_both.xml",
                            catalogue="data/patterns_explicit_voter_demo.yaml",
                            aliases=""),
}

E2_FIELDS = ["instance", "aliases", "baseline", "status", "seconds",
             "total_cost", "mu_max", "mu_list", "nprocs", "hw_cost",
             "dev_cost", "promotion_cost", "partition_total", "pattern_cost",
             "power", "patterns", "verify_ok", "fix_ok", "period_ub",
             "step1_status", "step1_seconds", "step1_total_cost",
             "step1_verify_ok", "mu_solver", "pin_status", "pin_seconds",
             "pin_ok"]
E2_BASELINES = ["joint", "SL", "PFmin", "PFmax", "NP", "PFlight", "PFcheap"]


def _build(name: str, spec: dict, dst: Path, *, safety: str | None = None,
           extra: list[str] | None = None) -> Path:
    cmd = [sys.executable, "tools/build_dzn.py", *spec["args"],
           "--safety", safety or spec["safety"],
           "--patterns", spec["catalogue"], *(extra or []), "-o", str(dst)]
    p = subprocess.run(cmd, cwd=SAFEDSE, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"build of {name} failed:\n{p.stderr[-1500:]}")
    return dst


# ---------------------------------------------------------------------------
# Input variants for the RQ2 sensitivity runs. Data only:
# generated copies of safedse inputs under variants/, never an
# edit in safedse.
#   tok2   every catalog edge back to the owner (the verdict / comparison
#          result: checker -> owner, channel_b -> owner, ...) carries one
#          more initial token, i.e. the fault reaction may take two periods
#          instead of one (the catalog has one token)
#   chk10  checker WCET = 10 % / 50 % of its owner's (the WCET files use
#   chk50  mkwcets.py --checker-scale 0.3); the checker entries are
#          regenerated by mkwcets.py itself (_scaled_wcets)
# ---------------------------------------------------------------------------
VARIANT_DIR = ROOT / "variants"
VARIANTS = {"tok2": ("tokens", 1), "chk10": ("checker", 0.1),
            "chk50": ("checker", 0.5)}
CHECKER_SCALE = 0.3      # mkwcets.py default, the value of the WCET files


def _variant_catalog(src: str, extra_tokens: int) -> Path:
    import yaml
    doc = yaml.safe_load((SAFEDSE / src).read_text())
    changed = 0
    for rec in doc["patterns"]:
        for e in rec.get("edges") or []:
            if e["to"] == "owner" and e.get("tokens", 0) >= 1:
                e["tokens"] += extra_tokens
                changed += 1
    dst = VARIANT_DIR / f"{Path(src).stem}_tok{1 + extra_tokens}.yaml"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(f"# GENERATED by harness.py from {src}: every "
                   f"edge back to the owner\n# with >= 1 token gets "
                   f"{extra_tokens} more ({changed} edges).\n"
                   + yaml.safe_dump(doc, sort_keys=False, width=100))
    return dst


def _scaled_wcets(spec: dict, scale: float, check: bool = False):
    """Checker WCETs at another --checker-scale, by mkwcets.py's own rule
    (nominal time x scale, then x the mode's cycle factor, each rounded):
    mkwcets.py is run on the instance's apps, platform and catalog into
    results/tmp, and only the `checker_*` blocks of the instance's WCET file
    are replaced (the files also hold entries of other applications). With
    check=True the result at CHECKER_SCALE is returned; it must equal the
    original file (checked in variant_spec)."""
    args = spec["args"]
    src = args[args.index("--wcets") + 1]
    apps = [args[k + 1] for k, a in enumerate(args) if a == "--app"]
    plat = args[args.index("--platform") + 1]
    TMP.mkdir(parents=True, exist_ok=True)
    gen = TMP / f"mkwcets_{Path(src).stem}_{scale}.xml"
    cmd = [sys.executable, "tools/mkwcets.py"]
    for a in apps:
        cmd += ["--app", a]
    cmd += ["--platform", plat, "--patterns", spec["catalogue"],
            "--checker-scale", str(scale), "-o", str(gen)]
    subprocess.run(cmd, cwd=SAFEDSE, check=True, capture_output=True)
    pat = r'(  <mapping task_type="(checker_[^"]+)">.*?</mapping>)'
    new = {t: b for b, t in re.findall(pat, gen.read_text(), re.S)}
    txt = (SAFEDSE / src).read_text()
    # Blocks of other applications' checkers (e.g. JPEG in WCETs_mixed.xml)
    # stay as they are; name-keyed types the file lacks are ignored.
    out = re.sub(pat, lambda m: new.get(m.group(2), m.group(1)), txt,
                 flags=re.S)
    if check:
        return out
    dst = VARIANT_DIR / f"{Path(src).stem}_chk{round(scale * 100)}.xml"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(out.replace(
        "<WCET_table>", f"<WCET_table>\n<!-- variant of {src}: the "
        f"checker_* entries of {', '.join(Path(a).name for a in apps)} "
        f"regenerated by mkwcets.py with checker scale {scale} "
        f"(harness.py) -->", 1))
    return dst


def variant_spec(spec: dict, variant: str) -> dict:
    kind, val = VARIANTS[variant]
    if kind == "tokens":
        return dict(spec, catalogue=str(_variant_catalog(spec["catalogue"],
                                                         val)))
    args = list(spec["args"])
    wi = args.index("--wcets") + 1
    norm = lambda t: re.sub(r"\s+/>", "/>", t)   # rosvall files: " />"
    if norm(_scaled_wcets(spec, CHECKER_SCALE, check=True)) != \
            norm((SAFEDSE / args[wi]).read_text()):
        raise SystemExit(f"{args[wi]}: mkwcets.py at --checker-scale "
                         f"{CHECKER_SCALE} does not reproduce the checker "
                         f"entries; scaling rule does not hold")
    args[wi] = str(_scaled_wcets(spec, val))
    return dict(spec, args=args)


def build_e2_instances(name: str, spec: dict, period_ub: int | None = None,
                       suffix: str = "", variant: str = "base"
                       ) -> dict[str, Path]:
    """Rebuild the joint instance (must equal out/<name>.dzn byte for byte,
    which proves the arguments above are build_all.sh's), the force-no-patterns
    variant for SL step 1, and the no-promotion variant for NP.

    period_ub (or spec["period_ub"]) replaces the application's period in a
    copy of its desConst, and every variant is built from that copy.

    variant != "base": the byte check is done on the base inputs, then
    everything is built from the variant's catalog / WCET file
    (variant_spec)."""
    E2_DIR.mkdir(parents=True, exist_ok=True)
    joint = _build(name, spec, E2_DIR / f"{name}.dzn")
    if joint.read_bytes() != (OUT / f"{name}.dzn").read_bytes():
        raise SystemExit(f"{name}: rebuild differs from out/{name}.dzn; the "
                         f"E2_INSTANCES arguments do not match build_all.sh")
    if variant != "base":
        spec = variant_spec(spec, variant)
        suffix = f"_{variant}{suffix}"
        joint = _build(name, spec, E2_DIR / f"{name}_{variant}.dzn")
    period_ub = period_ub or spec.get("period_ub")
    stem = f"{name}{suffix}"
    if period_ub:
        # Set the bound through the front-end, not by rewriting period_ub in
        # the .dzn: min_procs is derived from period_ub at build time, and a
        # rewritten file keeps the stale value (2 for pc_sobel at 400; the
        # front-end gives 1 at 100000). Measured: the RQ2 optimum does not
        # move on pc_sobel, but the instance should be the one the front-end
        # emits.
        app = spec["args"][spec["args"].index("--app") + 1]
        app = Path(app).name.split(".")[0]
        ci = spec["args"].index("--constraints") + 1
        con = (SAFEDSE / spec["args"][ci]).read_text()
        con2, k = re.subn(rf'(app_name="{app}"\s+period=")[-0-9]+"',
                          rf'\g<1>{period_ub}"', con)
        if k != 1:
            raise SystemExit(f"{spec['args'][ci]}: no period for {app}")
        loose = E2_DIR / f"{stem}_desConst.xml"
        loose.write_text(con2)
        args = list(spec["args"])
        args[ci] = str(loose)
        spec = dict(spec, args=args)
        joint = _build(name, spec, E2_DIR / f"{stem}_loose.dzn")
    nopat = _build(name, spec, E2_DIR / f"{stem}_nopat.dzn",
                   extra=["--force-no-patterns"])
    xml = (SAFEDSE / spec["safety"]).read_text()
    xml2, k = re.subn(r'allow_promotion="true"', 'allow_promotion="false"', xml)
    if k != 1:
        raise SystemExit(f"{spec['safety']}: expected one allow_promotion=\"true\"")
    saf = E2_DIR / f"{stem}_nopromo.safety.xml"
    saf.write_text(xml2)
    nopromo = _build(name, spec, E2_DIR / f"{stem}_nopromo.dzn",
                     safety=str(saf))
    return {"joint": joint, "nopat": nopat, "nopromo": nopromo}


def wrapper(name: str) -> Path:
    """The wrappers say `include "dse.mzn";` (no edit of the model). solve.py
    passes no include path, so a copy with the absolute path is solved."""
    src = (WRAPPERS / name).read_text()
    dst = E2_DIR / name
    dst.write_text(src.replace('include "dse.mzn";',
                               f'include "{SAFEDSE / "model" / "dse.mzn"}";'))
    return dst


def base_parents(d: dict) -> list[int]:
    """1-based PAR indices of the SDF actors (pattern slots follow their
    owner through par_owner and are not fixed separately)."""
    return [a + 1 for a, o in enumerate(d["par_owner"]) if o == a + 1]


PF_RULES = ["PFmin", "PFmax", "PFlight", "PFcheap"]


def _dev_k(d: dict) -> list[int]:
    v = d["dev_k"]
    if isinstance(v, str):   # "array1d(0..4, [100, 113, ...])"
        v = [int(x) for x in v[v.index("[") + 1: v.index("]")].split(",")]
    return v


def pattern_additions(d: dict, a: int, k: int) -> tuple[int, int]:
    """What pattern k (0-based) adds to base parent a (1-based), read from
    the built instance: (sum of the added nodes' least WCET over all core
    types and modes, `min_wcet`; recurring units + the added nodes'
    development cost at the actor's SIL, as lib/safety.mzn prices an active
    node without promotion). Hardware is left out: it depends on placement."""
    n, npat = d["n"], len(d["pat_name"])
    dk = _dev_k(d)
    wcet = cost = 0
    for i in range(n):
        if (d["node_owner"][i] == a and d["owner_node"][i] != i + 1
                and d["node_guard"][i * npat + k]):
            wcet += d["min_wcet"][i]
            sil = d["sil_req_parent"][d["parent"][i] - 1]
            cost += d["dev_base"][i] * dk[sil] // 100
    return wcet, d["pat_recurring"][k] + cost


def pf_candidates(d: dict, catalogue: str, rule: str, a: int) -> list:
    """Sorted [(key, pattern index 1-based)] of the allowed patterns of base
    parent a under a pattern-first rule. The last key field is always the
    catalog index, so a tie on all other fields is decided by file order
    (reported by pf_table).

    PFmin   fewest components, then recurring units, dev multipliers
    PFmax   widest fault coverage, then the PFmin key
    PFlight least added WCET (fastest core), then least added cost
    PFcheap least added cost (recurring + development), then least added
            WCET
    """
    pats = {p.id: (i, p) for i, p in
            enumerate(load_patterns(SAFEDSE / catalogue))}
    names = d["pat_name"]
    npat = len(names)
    row = d["pat_allowed"][(a - 1) * npat: a * npat]
    cands = []
    for k, ok in enumerate(row):
        if not ok:
            continue
        idx, p = pats[names[k]]
        devm = sum(float(v) for v in p.dev_cost_multiplier.values())
        base = (len(p.components), p.recurring_cost_units, devm, idx)
        if rule == "PFmin":
            key = base
        elif rule == "PFmax":
            key = (-len(p.covers_faults),) + base
        else:
            w, c = pattern_additions(d, a, k)
            key = (w, c, idx) if rule == "PFlight" else (c, w, idx)
        cands.append((key, k + 1))
    return sorted(cands)


def pf_choice(d: dict, catalogue: str, rule: str) -> list[int]:
    choice = [0] * len(d["par_owner"])
    for a in base_parents(d):
        choice[a - 1] = pf_candidates(d, catalogue, rule, a)[0][1]
    return choice


def _mzn(xs) -> str:
    return "[" + ", ".join(str(x) for x in xs) + "]"


def _e2_row(r: dict, d: dict) -> dict:
    """Adds the cost breakdown and the selected pattern per SDF actor."""
    s = r.get("_solution")
    if s:
        r.update(total_cost=s.get("total_cost"),
                 partition_total=s.get("partition_total"),
                 pattern_cost=s.get("pattern_cost"))
        names = d["pat_name"]
        pn = d["parent_name"]
        r["patterns"] = ";".join(
            f"{pn[a - 1].split('.', 1)[-1]}={names[s['pat'][a - 1] - 1]}"
            for a in base_parents(d) if not pn[a - 1].startswith("__"))
    return r


# SL step 1 tie-break. Step 1 has several optima with different bindings,
# and step 2 fixes the one the solver returns, so SL results moved between
# runs (7 rows in one comparison). Step 1 therefore returns one canonical
# optimum: least objective, then least value of the other key (period for
# TOTALCOST, total cost for THROUGHPUT), then the lexicographically smallest
# core binding of the base nodes in node order. The binding key is solved in
# chunks of SL_LEX_CHUNK nodes with the objective sum (proc - 1) * P^k, which
# stays below 2^31 for P <= 30, each chunk fixed before the next. The model
# copy for that objective differs from safedse's dse.mzn only in its solve
# item (sl_lex_model); no constraint changes.
SL_LEX_CHUNK = 6
SL_LEX_MODEL = TMP / "sl_lex" / "dse.mzn"


def sl_lex_model() -> Path:
    src = (SAFEDSE / "model" / "dse.mzn").read_text()
    old = "])  minimize metric[opt_metric];"
    if src.count(old) != 1 or src.count('include "../lib/') != 8:
        raise SystemExit("sl_lex_model: dse.mzn solve item or includes moved")
    src = src.replace('include "../lib/', f'include "{SAFEDSE / "lib"}/')
    src = src.replace(old, "])  minimize tb_key;\n" + """
% lexicographic binding tie-break of SL step 1 (harness.py)
array[int] of int: tb_node;      % nodes of this chunk, most significant first
int: tb_base;                    % > max core index - 1
array[int] of int: tb_fix_node;  % nodes of earlier chunks ...
array[int] of int: tb_fix_proc;  % ... and their cores
constraint forall(k in index_set(tb_fix_node))(
    proc[tb_fix_node[k]] = tb_fix_proc[k]);
var int: tb_key = sum(k in index_set(tb_node))(
    (proc[tb_node[k]] - 1) * pow(tb_base, max(index_set(tb_node)) - k));
""")
    SL_LEX_MODEL.parent.mkdir(parents=True, exist_ok=True)
    if not SL_LEX_MODEL.exists() or SL_LEX_MODEL.read_text() != src:
        SL_LEX_MODEL.write_text(src)
    return SL_LEX_MODEL


def sl_step1(nopat: Path, objective: str, tag: str) -> dict:
    """SL step 1 with the canonical tie-break above. Returns a
    solve_and_verify row whose solution is the canonical optimum; its
    seconds include the tie-break solves. If a tie-break stage does not end
    OPTIMAL, the row keeps the plain step-1 solution and its status says
    which stage failed (STEP1 rows then count as not canonical)."""
    r1 = solve_and_verify(nopat, objective, tag=f"{tag}_plain")
    if r1["status"] != "OPTIMAL" or "_solution" not in r1:
        return r1
    s1 = r1["_solution"]
    second = "THROUGHPUT" if objective == "TOTALCOST" else "TOTALCOST"
    val = lambda s, m: (max(s["mu"]) if isinstance(s["mu"], list)  # noqa: E731
                        else s["mu"]) if m == "THROUGHPUT" else s["total_cost"]
    secs = r1["seconds"]
    b = {objective: val(s1, objective)}
    r = solve_run(str(nopat), second, b, timeout=PY_TIMEOUT, threads=THREADS,
                  time_limit_ms=TIME_LIMIT_MS)
    secs += r.get("seconds", 0.0)
    if r["status"] != "OPTIMAL":
        r1["status"] = f"OPTIMAL_TB2_{r['status']}"
        return r1
    b[second] = val(r["solution"], second)
    d1 = parse_dzn(str(nopat))
    comm = [bool(x) for x in (d1.get("comm_actor") or [])]
    comm += [False] * (d1["n"] - len(comm))
    nodes = [i + 1 for i in range(d1["n"]) if not comm[i]]
    base = max(d1["P"], 2)
    fixed: list[tuple[int, int]] = []
    model = sl_lex_model()
    s = r["solution"]
    for c in range(0, len(nodes), SL_LEX_CHUNK):
        chunk = nodes[c:c + SL_LEX_CHUNK]
        extra = (f"tb_node = {_mzn(chunk)}; tb_base = {base}; "
                 f"tb_fix_node = {_mzn([n for n, _ in fixed])}; "
                 f"tb_fix_proc = {_mzn([p for _, p in fixed])};")
        r = solve_run(str(nopat), objective, b, timeout=PY_TIMEOUT,
                      threads=THREADS, time_limit_ms=TIME_LIMIT_MS,
                      model=str(model), extra=extra)
        secs += r.get("seconds", 0.0)
        if r["status"] != "OPTIMAL":
            r1["status"] = f"OPTIMAL_TBLEX_{r['status']}"
            return r1
        s = r["solution"]
        fixed += [(n, s["proc"][n - 1]) for n in chunk]
    # the canonical design, pinned and verified like any other solution
    row = {"status": "OPTIMAL", "seconds": round(secs, 3)}
    s = _pinned(nopat, objective, s, row)
    mu = s["mu"] if isinstance(s["mu"], list) else [s["mu"]]
    report = verify_solution(nopat, s, tag)
    row.update(mu_max=max(mu), mu_list=json.dumps(mu), nprocs=s.get("nprocs"),
               verify_ok=report["ok"], _solution=s, _report=report)
    if any(s["proc"][n - 1] != p for n, p in fixed) or \
            val(s, objective) != b[objective]:
        row["verify_ok"] = False     # the pin moved the design: never expected
    return row


def run_baseline(b: str, spec: dict, dz: dict[str, Path], d: dict,
                 tag: str, objective: str = "TOTALCOST") -> dict:
    """One RQ2 row: baseline b on the instance variants dz (see
    build_e2_instances); d is the parsed joint instance. objective
    THROUGHPUT gives the flow's least period (e2_thr.csv); SL's step 1
    then minimises the period too."""
    if b == "joint":
        row = solve_and_verify(dz["joint"], objective, tag=tag)
    elif b == "NP":
        row = solve_and_verify(dz["nopromo"], objective, tag=tag)
    elif b in PF_RULES:
        fix = pf_choice(d, spec["catalogue"], b)
        row = solve_and_verify(dz["joint"], objective, tag=tag,
                               model=str(wrapper("fix_pattern.mzn")),
                               extra=f"fix_pat = {_mzn(fix)};")
        if "_solution" in row:
            row["fix_ok"] = all(row["_solution"]["pat"][a - 1] == fix[a - 1]
                                for a in base_parents(d))
        else:   # record the fixed choice anyway
            row["patterns"] = ";".join(
                f"{d['parent_name'][a - 1].split('.', 1)[-1]}="
                f"{d['pat_name'][fix[a - 1] - 1]}"
                for a in base_parents(d)
                if not d["parent_name"][a - 1].startswith("__"))
    else:   # SL
        r1 = sl_step1(dz["nopat"], objective, tag=f"{tag}_step1")
        row = {"step1_status": r1["status"], "step1_seconds": r1["seconds"],
               "step1_verify_ok": r1["verify_ok"]}
        if "_solution" in r1 and r1["verify_ok"]:
            s1 = r1["_solution"]
            row["step1_total_cost"] = s1.get("total_cost")
            d1 = parse_dzn(str(dz["nopat"]))
            core_of = {nm: s1["proc"][i]
                       for i, nm in enumerate(d1["node_name"])
                       if not d1["comm_actor"][i]}
            nodes = [i + 1 for i, nm in enumerate(d["node_name"])
                     if nm in core_of and not d["comm_actor"][i]]
            procs = [core_of[d["node_name"][i - 1]] for i in nodes]
            r2 = solve_and_verify(dz["joint"], objective, tag=tag,
                                  model=str(wrapper("fix_binding.mzn")),
                                  extra=f"fix_node = {_mzn(nodes)}; "
                                        f"fix_proc = {_mzn(procs)};")
            r2["seconds"] = round(r1["seconds"] + r2["seconds"], 3)
            if "_solution" in r2:
                r2["fix_ok"] = all(r2["_solution"]["proc"][i - 1] == c
                                   for i, c in zip(nodes, procs))
            row.update(r2)
        else:
            row.update(status=f"STEP1_{r1['status']}", seconds=r1["seconds"],
                       verify_ok="")
    return _e2_row(row, d)


def rq2(only: list[str] | None = None, path: Path | None = None) -> None:
    path = path or RESULTS / "e2_joint.csv"
    done = existing_keys(path, ["instance", "baseline"])
    for name, spec in E2_INSTANCES.items():
        if only and name not in only:
            continue
        if all((name, b) in done for b in E2_BASELINES):
            continue
        dz = build_e2_instances(name, spec)
        d = parse_dzn(str(dz["joint"]))
        common = dict(instance=name, aliases=spec["aliases"],
                      period_ub=spec.get("period_ub") or "")
        for b in E2_BASELINES:
            if (name, b) in done:
                continue
            row = run_baseline(b, spec, dz, d, f"e2_{name}_{b}")
            row.update(common, baseline=b)
            append_row(path, E2_FIELDS, row)
            print(f"e2 {name} {b}: {row['status']} {row['seconds']}s "
                  f"total={row.get('total_cost')} nprocs={row.get('nprocs')} "
                  f"verify_ok={row['verify_ok']}", flush=True)


# ---------------------------------------------------------------------------
# RQ2, throughput-bound variants
#
# In e2_joint.csv throughput never binds (the c_rasta instances declare no
# period, pc_sobel is loosened), and SL = PF-min = joint everywhere. Here the
# period bound is tied to each instance's own throughput optimum mu*: first
# THROUGHPUT is minimised on the joint instance (row baseline=min_period),
# then every baseline is run with period_ub = ceil(f * mu*), f in TIGHT,
# set through the front-end (desConst copy), so min_procs and the
# redundant constraints see the bound. At f = 1.0 joint is feasible by
# construction; a baseline may not be.
# ---------------------------------------------------------------------------
TIGHT = ["1.00", "1.10", "1.25", "1.50", "2.00"]
E2T_FIELDS = ["instance", "factor", "mu_star"] + [
    f for f in E2_FIELDS if f != "instance"]


def _ub(mu_star: int, fac: str) -> int:
    return -(-mu_star * int(round(float(fac) * 100)) // 100)   # ceil


def rq2_tight(only: list[str] | None = None) -> None:
    path = RESULTS / "e2_tight.csv"
    done = existing_keys(path, ["instance", "factor", "baseline"])
    with_mu = {}
    if path.exists():
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                if r["baseline"] == "min_period" and r["verify_ok"] == "True":
                    with_mu[r["instance"]] = int(r["mu_max"])
    for name, spec in E2_INSTANCES.items():
        if only and name not in only:
            continue
        if name not in with_mu:
            if (name, "-", "min_period") in done:
                print(f"e2t {name}: min_period row exists but is not VERIFY "
                      f"OK; skipped", flush=True)
                continue
            dz = build_e2_instances(name, spec)
            row = solve_and_verify(dz["joint"], "THROUGHPUT",
                                   tag=f"e2t_{name}_min_period")
            row = _e2_row(row, parse_dzn(str(dz["joint"])))
            row.update(instance=name, factor="-", aliases=spec["aliases"],
                       baseline="min_period",
                       period_ub=spec.get("period_ub") or "")
            append_row(path, E2T_FIELDS, row)
            print(f"e2t {name} min_period: {row['status']} "
                  f"mu={row.get('mu_max')} verify_ok={row['verify_ok']}",
                  flush=True)
            if row["status"] != "OPTIMAL" or row["verify_ok"] is not True:
                continue
            with_mu[name] = int(row["mu_max"])
        mu_star = with_mu[name]
        for fac in TIGHT:
            if all((name, fac, b) in done for b in E2_BASELINES):
                continue
            ub = _ub(mu_star, fac)
            dz = build_e2_instances(name, spec, period_ub=ub,
                                    suffix=f"_t{fac}")
            d = parse_dzn(str(dz["joint"]))
            for b in E2_BASELINES:
                if (name, fac, b) in done:
                    continue
                row = run_baseline(b, spec, dz, d, f"e2t_{name}_{fac}_{b}")
                row.update(instance=name, factor=fac, mu_star=mu_star,
                           aliases=spec["aliases"], baseline=b, period_ub=ub)
                append_row(path, E2T_FIELDS, row)
                print(f"e2t {name} x{fac} (ub={ub}) {b}: {row['status']} "
                      f"{row['seconds']}s total={row.get('total_cost')} "
                      f"verify_ok={row['verify_ok']}", flush=True)


# ---------------------------------------------------------------------------
# RQ2 robustness
#
# rq2thr   per flow, the least period under the flow's own restriction
#          (THROUGHPUT; SL's step 1 minimises the period without patterns,
#          step 2 fixes that binding), on the base inputs and each variant
#          -> e2_thr.csv. A pattern-first flow is feasible at a bound B iff
#          its least period is <= B.
# rq2var   the bound sweep of rq2tight on each input variant, factors
#          relative to the variant's own mu*, plus no bound -> e2_var.csv
# rq2reps  5 repetitions of the joint rows (no bound and every factor), for
#          the run-time median and range -> e2_reps.csv
# pftable  the pattern each rule fixes per actor, and whether the catalog
#          file order decided it -> e2_pfchoice.csv
# ---------------------------------------------------------------------------
THR_FLOWS = ["joint", "SL", "PFmin", "PFmax", "PFlight", "PFcheap", "NP"]
E2THR_FIELDS = ["variant", "instance", "flow"] + [
    f for f in E2_FIELDS if f not in ("instance", "baseline")]
VAR_BASELINES = ["joint", "SL", "PFmin", "PFmax", "PFlight", "PFcheap"]
VAR_FACTORS = ["none"] + TIGHT
E2V_FIELDS = ["variant"] + E2T_FIELDS
REPS = 5
E2R_FIELDS = ["instance", "factor", "rep", "period_ub", "status", "seconds",
              "total_cost", "mu_max", "nprocs", "verify_ok", "pin_ok"]


def rq2_thr(only: list[str] | None = None,
            variants: list[str] | None = None) -> None:
    path = RESULTS / "e2_thr.csv"
    done = existing_keys(path, ["variant", "instance", "flow"])
    for var in variants or ["base"] + list(VARIANTS):
        for name, spec in E2_INSTANCES.items():
            if only and name not in only:
                continue
            if all((var, name, f) in done for f in THR_FLOWS):
                continue
            dz = build_e2_instances(name, spec, variant=var)
            d = parse_dzn(str(dz["joint"]))
            vspec = spec if var == "base" else variant_spec(spec, var)
            for fl in THR_FLOWS:
                if (var, name, fl) in done:
                    continue
                row = run_baseline(fl, vspec, dz, d,
                                   f"e2thr_{var}_{name}_{fl}", "THROUGHPUT")
                row.update(variant=var, instance=name, flow=fl,
                           aliases=spec["aliases"],
                           period_ub=spec.get("period_ub") or "")
                append_row(path, E2THR_FIELDS, row)
                print(f"e2thr {var} {name} {fl}: {row['status']} "
                      f"{row['seconds']}s mu={row.get('mu_max')} "
                      f"verify_ok={row['verify_ok']}", flush=True)


def rq2_var(only: list[str] | None = None,
            variants: list[str] | None = None) -> None:
    path = RESULTS / "e2_var.csv"
    done = existing_keys(path, ["variant", "instance", "factor", "baseline"])
    with_mu = {}
    if path.exists():
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                if r["baseline"] == "min_period" and r["verify_ok"] == "True":
                    with_mu[(r["variant"], r["instance"])] = int(r["mu_max"])
    for var in variants or list(VARIANTS):
        for name, spec in E2_INSTANCES.items():
            if only and name not in only:
                continue
            vspec = variant_spec(spec, var)
            common = dict(variant=var, instance=name, aliases=spec["aliases"])
            if (var, name) not in with_mu:
                if (var, name, "-", "min_period") in done:
                    print(f"e2v {var} {name}: min_period not VERIFY OK; "
                          f"skipped", flush=True)
                    continue
                dz = build_e2_instances(name, spec, variant=var)
                row = solve_and_verify(dz["joint"], "THROUGHPUT",
                                       tag=f"e2v_{var}_{name}_min_period")
                row = _e2_row(row, parse_dzn(str(dz["joint"])))
                row.update(common, factor="-", baseline="min_period",
                           period_ub=spec.get("period_ub") or "")
                append_row(path, E2V_FIELDS, row)
                print(f"e2v {var} {name} min_period: {row['status']} "
                      f"mu={row.get('mu_max')} verify_ok={row['verify_ok']}",
                      flush=True)
                if row["status"] != "OPTIMAL" or row["verify_ok"] is not True:
                    continue
                with_mu[(var, name)] = int(row["mu_max"])
            mu_star = with_mu[(var, name)]
            for fac in VAR_FACTORS:
                if all((var, name, fac, b) in done for b in VAR_BASELINES):
                    continue
                ub = None if fac == "none" else _ub(mu_star, fac)
                dz = build_e2_instances(name, spec, period_ub=ub,
                                        suffix=f"_t{fac}", variant=var)
                d = parse_dzn(str(dz["joint"]))
                for b in VAR_BASELINES:
                    if (var, name, fac, b) in done:
                        continue
                    row = run_baseline(b, vspec, dz, d,
                                       f"e2v_{var}_{name}_{fac}_{b}")
                    row.update(common, factor=fac, mu_star=mu_star,
                               baseline=b,
                               period_ub=ub or spec.get("period_ub") or "")
                    append_row(path, E2V_FIELDS, row)
                    print(f"e2v {var} {name} x{fac} (ub={ub}) {b}: "
                          f"{row['status']} {row['seconds']}s "
                          f"total={row.get('total_cost')} "
                          f"verify_ok={row['verify_ok']}", flush=True)


def rq2_reps(only: list[str] | None = None) -> None:
    path = RESULTS / "e2_reps.csv"
    done = existing_keys(path, ["instance", "factor", "rep"])
    mu = {}
    with (RESULTS / "e2_tight.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            if r["baseline"] == "min_period" and r["verify_ok"] == "True":
                mu[r["instance"]] = int(r["mu_max"])
    for name, spec in E2_INSTANCES.items():
        if only and name not in only:
            continue
        for fac in ["none"] + TIGHT:
            if fac != "none" and name not in mu:
                continue
            if all((name, fac, str(k)) in done for k in range(1, REPS + 1)):
                continue
            ub = None if fac == "none" else _ub(mu[name], fac)
            dz = build_e2_instances(name, spec, period_ub=ub,
                                    suffix="" if ub is None else f"_t{fac}")
            for k in range(1, REPS + 1):
                if (name, fac, str(k)) in done:
                    continue
                row = solve_and_verify(dz["joint"], "TOTALCOST",
                                       tag=f"e2r_{name}_{fac}_{k}")
                s = row.get("_solution") or {}
                row.update(instance=name, factor=fac, rep=k,
                           period_ub=ub or spec.get("period_ub") or "",
                           total_cost=s.get("total_cost"))
                append_row(path, E2R_FIELDS, row)
                print(f"e2r {name} {fac} rep {k}: {row['status']} "
                      f"{row['seconds']}s total={row.get('total_cost')} "
                      f"verify_ok={row['verify_ok']}", flush=True)


E2PF_FIELDS = ["variant", "instance", "rule", "actor", "sil", "chosen",
               "key", "runner_up", "runner_up_key", "order_tie"]


def pf_table(variants: list[str] | None = None) -> None:
    """Not resumable (no solves, a few seconds): rewritten on each call."""
    path = RESULTS / "e2_pfchoice.csv"
    if path.exists():
        path.unlink()
    for var in variants or ["base"] + list(VARIANTS):
        for name, spec in E2_INSTANCES.items():
            dz = build_e2_instances(name, spec, variant=var)
            d = parse_dzn(str(dz["joint"]))
            vspec = spec if var == "base" else variant_spec(spec, var)
            names, pn = d["pat_name"], d["parent_name"]
            for rule in PF_RULES:
                for a in base_parents(d):
                    if pn[a - 1].startswith("__"):
                        continue
                    c = pf_candidates(d, vspec["catalogue"], rule, a)
                    nxt = c[1] if len(c) > 1 else None
                    append_row(path, E2PF_FIELDS, dict(
                        variant=var, instance=name, rule=rule,
                        actor=pn[a - 1].split(".", 1)[-1],
                        sil=d["sil_req_parent"][a - 1],
                        chosen=names[c[0][1] - 1], key=list(c[0][0]),
                        runner_up=names[nxt[1] - 1] if nxt else "",
                        runner_up_key=list(nxt[0]) if nxt else "",
                        order_tie=bool(nxt and nxt[0][:-1] == c[0][0][:-1])))
    print(f"wrote {path}")


# ---------------------------------------------------------------------------
# RQ3 -- scalability: HSDF actor count and catalogue size, synthetic
# SDF3 graphs (gen_synthetic.py). Each instance is built with
# a period bound tied to its OWN throughput optimum (gen_synthetic's
# build_instance), the same "throughput binds" method as the RQ2 tight
# variants, then TOTALCOST is optimised at the RQ3 time limit (600 s, longer
# than the 300 s used elsewhere: these instances are far larger and the point
# is partly to see where exact search stops).
# ---------------------------------------------------------------------------
import gen_synthetic  # noqa: E402

RQ3_TIME_LIMIT_MS = 600_000
RQ3_PY_TIMEOUT = RQ3_TIME_LIMIT_MS // 1000 + 60
ACTOR_SIZES = [8, 16, 24, 32, 48, 64]
CATALOGUE_SIZES = [1, 3, 5, 9]
CATSWEEP_ACTORS = 24
RQ3_REPS = 5

E3_FIELDS = ["sweep", "param", "index", "tag", "gen_status", "actors_target",
             "n_sdf_actors", "n_hsdf_actual", "n_core_types_actual",
             "n", "mu_status", "mu_seconds", "mu_star", "mu_star_proven",
             "status", "seconds", "total_cost", "mu_max", "nprocs",
             "objective_bound", "flat_vars", "verify_ok", "build_error",
             "mu_solver", "pin_status", "pin_seconds", "pin_ok"]


def _rq3_solve_and_verify(dzn: Path, tag: str) -> dict:
    """Like solve_and_verify, but at RQ3_TIME_LIMIT_MS and carrying the
    extra solver statistics (objective bound, FlatZinc size) RQ3 records to
    show where exact search stops."""
    r = solve_run(str(dzn), "TOTALCOST", timeout=RQ3_PY_TIMEOUT,
                 threads=THREADS, time_limit_ms=RQ3_TIME_LIMIT_MS)
    row = {"status": r["status"], "seconds": round(r.get("seconds", 0.0), 3)}
    if "solution" in r:
        s = _pinned(dzn, "TOTALCOST", r["solution"], row)
        mu = s["mu"] if isinstance(s["mu"], list) else [s["mu"]]
        row.update(total_cost=s.get("total_cost"), mu_max=max(mu),
                   nprocs=s.get("nprocs"))
        report = verify_solution(dzn, s, tag)
        row["verify_ok"] = report["ok"]
        stats = r.get("stats", {})
        row["objective_bound"] = stats.get("objectiveBound", "")
        if "flatBoolVars" in stats and "flatIntVars" in stats:
            row["flat_vars"] = int(stats["flatBoolVars"]) + int(stats["flatIntVars"])
    else:
        row["verify_ok"] = ""
    return row


def _rq3_row(meta: dict) -> dict:
    d = parse_dzn(meta["dzn"]) if meta.get("dzn") else None
    row = dict(meta)
    row["gen_status"] = meta["status"]
    if d is not None:
        row["n"] = d["n"]
    return row


def rq3(sweeps: list[str] | None = None) -> None:
    path = RESULTS / "e3_scal.csv"
    done = existing_keys(path, ["sweep", "param", "index"])
    sweeps = sweeps or ["actors", "catalogue"]

    def _run(sweep: str, param: int, index: int, tag: str,
            actors: int, catalogue_size: int) -> None:
        key = (sweep, str(param), str(index))
        if key in done:
            return
        meta = gen_synthetic.build_instance(tag, actors, catalogue_size)
        row = _rq3_row(meta)
        row.update(sweep=sweep, param=param, index=index)
        if meta["status"] == "READY":
            row.update(_rq3_solve_and_verify(Path(meta["dzn"]), f"e3_{tag}"))
        append_row(path, E3_FIELDS, row)
        print(f"e3 {sweep}={param} i={index} ({tag}): gen={meta['status']} "
              f"solve={row.get('status', '')} {row.get('seconds', '')}s "
              f"total={row.get('total_cost')} verify_ok={row.get('verify_ok')}",
              flush=True)

    if "actors" in sweeps:
        for n in ACTOR_SIZES:
            for i in range(RQ3_REPS):
                _run("actors", n, i, f"actorsweep_n{n}_i{i}", n, 9)
    if "catalogue" in sweeps:
        for k in CATALOGUE_SIZES:
            for i in range(RQ3_REPS):
                _run("catalogue", k, i, f"catsweep_k{k}_i{i}",
                    CATSWEEP_ACTORS, k)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("groups", nargs="+",
                    choices=["rq1", "rq2", "rq2tight", "rq2thr", "rq2var",
                             "rq2reps", "pftable", "rq3", "rq4"])
    ap.add_argument("--variants", nargs="*", default=None,
                    help="rq2thr/rq2var/pftable only: restrict to these "
                         "variants (base, " + ", ".join(VARIANTS) + ")")
    ap.add_argument("--reps", type=int, default=5,
                    help="repetitions for RQ1 timing (default 5)")
    ap.add_argument("--instances", nargs="*", default=None,
                    help="RQ2 only: restrict to these instances")
    ap.add_argument("--e2-csv", default=None,
                    help="RQ2 only: write to this CSV instead (validation)")
    ap.add_argument("--sweeps", nargs="*", default=None,
                    choices=["actors", "catalogue"],
                    help="RQ3 only: restrict to these sweeps")
    a = ap.parse_args()
    t0 = time.time()
    if "rq1" in a.groups:
        rq1(a.reps)
    if "rq2" in a.groups:
        rq2(a.instances,
            Path(a.e2_csv) if a.e2_csv else None)
    if "rq2tight" in a.groups:
        rq2_tight(a.instances)
    if "pftable" in a.groups:
        pf_table(a.variants)
    if "rq2thr" in a.groups:
        rq2_thr(a.instances, a.variants)
    if "rq2var" in a.groups:
        rq2_var(a.instances, a.variants)
    if "rq2reps" in a.groups:
        rq2_reps(a.instances)
    if "rq3" in a.groups:
        rq3(a.sweeps)
    if "rq4" in a.groups:
        rq4(a.reps)
    print(f"done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

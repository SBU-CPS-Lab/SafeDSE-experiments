#!/usr/bin/env python3
"""RQ4 argument statistics: does the fault model prune the argument, not only
the design space, and how much of a generated argument is discharged by
verified deployment evidence?

For each instance below: solve, verify, generate the GSN argument with
tools/gsn.py (reusing the same verifier report, per its --report flag), and
record element/goal/Solution counts plus the count of DIVERSE placement
checks in the verifier's own report (the same count tests/run_tests.py's
t_gsn uses to pin "DIVERSE evidence appears only once diversity is
required"). Output is never written into safedse/out/: this script only
reads instances that are already built there and writes its own artifacts
under results/tmp/, so it never touches SafeDSE's sample outputs
(out/*.gsn.json). The solution, verifier report and argument of each
instance are also kept in results/gsn/ (tracked): the GSN figure is drawn
from results/gsn/f_both3.gsn.json by make_gsn_figure.py.

    python3 gsn_stats.py            # solve missing rows
    python3 gsn_stats.py --regen    # re-argue the kept solutions

--regen does not solve: it re-runs the verifier on each kept solution
in results/gsn/, regenerates the argument with the current tools/gsn.py, and
rewrites e4_gsn.csv. It is how a generator change reaches Fig. 4 and the RQ4
statistics without moving the design they describe.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import SAFEDSE  # noqa: E402
sys.path.insert(0, str(SAFEDSE / "tools"))
from solve import run as solve_run  # noqa: E402
from exact_period import pin_period  # noqa: E402

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
TMP = RESULTS / "tmp"
KEEP = RESULTS / "gsn"           # tracked copies of the argument inputs
OUT = SAFEDSE / "out"

THREADS = 4                      # see harness.py's module docstring
TIME_LIMIT_MS = 300_000
TIMEOUT_S = TIME_LIMIT_MS // 1000 + 60   # above the MiniZinc limit

# (label, pattern catalogue the instance was built with, optimise metric)
INSTANCES = [
    ("f_random_hw", "data/patterns.yaml", "TOTALCOST"),
    ("f_both3", "data/patterns.yaml", "TOTALCOST"),
    ("v_nvp", "data/patterns_explicit_voter_demo.yaml", "TOTALCOST"),
]

FIELDS = ["label", "fault_model", "elements", "goals", "goals_undeveloped",
          "solutions", "checks_total", "diverse_records", "out_of_scope",
          "verify_ok"]


def existing_labels(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="") as f:
        return {row["label"] for row in csv.DictReader(f)}


def one(label: str, catalogue: str, metric: str) -> dict:
    dzn = OUT / f"{label}.dzn"
    TMP.mkdir(parents=True, exist_ok=True)
    solp = TMP / f"{label}.gsn_sol.json"

    r = solve_run(str(dzn), metric, timeout=TIMEOUT_S, threads=THREADS,
                  time_limit_ms=TIME_LIMIT_MS)
    if "solution" not in r:
        return {"label": label, "fault_model": "", "elements": "",
                "goals": "", "goals_undeveloped": "", "solutions": "",
                "checks_total": "", "diverse_records": "",
                "out_of_scope": "", "verify_ok": f"NOSOLVE:{r['status']}"}
    sol = r["solution"]
    if metric != "THROUGHPUT":           # least period of this design
        pin = pin_period(dzn, sol)
        if pin["ok"]:
            sol = pin["solution"]
    solp.write_text(json.dumps(sol))
    return argue(label, catalogue, dzn, solp)


def argue(label: str, catalogue: str, dzn: Path, solp: Path) -> dict:
    """Verify the solution in `solp`, generate its argument, keep both.
    The tools run in this repository's root with relative paths, so the kept
    files name their inputs as safedse/out/... and results/... ."""
    TMP.mkdir(parents=True, exist_ok=True)
    rel = lambda p: os.path.relpath(p, ROOT)
    dzn, solp = Path(rel(dzn)), Path(rel(solp))
    repp = Path(rel(TMP / f"{label}.gsn_report.json"))
    outp = Path(rel(TMP / label))
    subprocess.run([sys.executable, str(SAFEDSE / "tools" / "verify.py"),
                     "--dzn", str(dzn), "--solution", str(solp), "--quiet",
                     "--json-report", str(repp)], check=False)
    report = json.loads(repp.read_text())

    diverse = sum(1 for c in report["checks"]
                  if c.get("kind") == "placement" and c.get("relation") == "DIVERSE")

    gsn_rc = subprocess.run(
        [sys.executable, str(SAFEDSE / "tools" / "gsn.py"),
         "--dzn", str(dzn), "--solution", str(solp), "--out", str(outp),
         "--patterns", str(SAFEDSE / catalogue), "--report", str(repp),
         "--quiet"], capture_output=True, text=True)
    if gsn_rc.returncode != 0:
        return {"label": label, "fault_model": "", "elements": "",
                "goals": "", "goals_undeveloped": "", "solutions": "",
                "checks_total": len(report["checks"]),
                "diverse_records": diverse, "out_of_scope": "",
                "verify_ok": report["ok"],
                "_gsn_error": gsn_rc.stderr.strip()}

    KEEP.mkdir(parents=True, exist_ok=True)
    for src, dst in ((solp, "sol"), (repp, "report"),
                     (Path(f"{outp}.gsn.json"), "gsn")):
        if src.resolve() != (KEEP / f"{label}.{dst}.json").resolve():
            shutil.copyfile(src, KEEP / f"{label}.{dst}.json")

    doc = json.loads(Path(f"{outp}.gsn.json").read_text())
    st = doc["stats"]
    return {"label": label, "fault_model": doc["meta"]["fault_model"],
            "elements": st["elements"], "goals": st["goals"],
            "goals_undeveloped": st["goals_undeveloped"],
            "solutions": st["solutions"],
            "checks_total": len(report["checks"]),
            "diverse_records": diverse,
            "out_of_scope": len(doc["outOfScope"]),
            "verify_ok": report["ok"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regen", action="store_true",
                    help="re-verify and re-argue the kept solutions; rewrite "
                         "e4_gsn.csv (no solving)")
    regen = ap.parse_args().regen
    os.chdir(ROOT)          # argue() passes paths relative to the root
    path = RESULTS / "e4_gsn.csv"
    if regen and path.exists():
        path.unlink()
    done = existing_labels(path)
    is_new = not path.exists()
    rows = []
    for label, catalogue, metric in INSTANCES:
        if label in done:
            continue
        if regen:
            solp = KEEP / f"{label}.sol.json"
            if not solp.exists():
                raise SystemExit(f"--regen: {solp} missing; run without "
                                 f"--regen first")
            row = argue(label, catalogue, OUT / f"{label}.dzn", solp)
        else:
            row = one(label, catalogue, metric)
        rows.append(row)
        err = row.pop("_gsn_error", None)
        print(f"gsn_stats {label}: elements={row['elements']} "
              f"goals={row['goals']} undeveloped={row['goals_undeveloped']} "
              f"solutions={row['solutions']} checks={row['checks_total']} "
              f"diverse={row['diverse_records']} "
              f"out_of_scope={row['out_of_scope']} "
              f"verify_ok={row['verify_ok']}" + (f"  ERROR: {err}" if err else ""))
    if rows:
        RESULTS.mkdir(parents=True, exist_ok=True)
        with path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if is_new:
                w.writeheader()
            for row in rows:
                w.writerow(row)
            f.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

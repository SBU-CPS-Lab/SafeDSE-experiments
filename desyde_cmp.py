#!/usr/bin/env python3
"""RQ1 head-to-head with DeSyDe.

DeSyDe at tag v0.2.1-todaes (commit 6d5cbeb), the state of Rosvall and
Sander's TODAES article, built by desyde/build.sh. Both tools read the same
files: the TODAES Experiment 3 and 4 inputs (copied unchanged to
desyde/todaes/; the SDF graphs are byte-identical to safedse/data/rosvall/).

Experiment 3 (homogeneous 8-core platform, TDMA bus) and Experiment 4 (the
same plus an accelerator for JPEG's CS actor) minimize the period of one
application (JPEG; RASTA in SoSuRa) while the others meet fixed bounds
(Sobel 400, SUSAN 2050, RASTA 550). SafeDSE's THROUGHPUT objective is the
worst application period. The two objectives coincide when every design has
the optimized application's period above all other bounds; `coincide()`
checks this from the built instance (lower bound: the largest least WCET of
the optimized application's actors, since each actor runs once per period on
one core). Scenarios where they differ (Exp. 3 SoSuRa, Exp. 4 with SUSAN) are
recorded as `skipped` with the reason, not solved with another objective.

    desyde   DeSyDe with the published configuration (two-step TODAES
             heuristic, MCR propagator, 6 h / 30 min time-outs), 4 threads
                                                   -> e7_desyde.csv
    safedse  SafeDSE: front end on the same files (--comm tdma, no safety
             specification), THROUGHPUT, 4 workers, verify.py
                                                   -> e7_safedse.csv

Both are resumable (one CSV row per run). Runs must not overlap (timing).

    python3 desyde_cmp.py safedse --reps 1
    python3 desyde_cmp.py desyde --reps 1 [--scenarios ...]
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import harness as H
from harness import (SAFEDSE, RESULTS, TMP, append_row, existing_keys,
                     verify_solution, parse_dzn)
from solve import run as solve_run   # noqa: E402  (safedse/tools, via harness)
from exact_period import pin_period

ROOT = Path(__file__).resolve().parent
DSD = ROOT / "desyde"
TODAES = DSD / "todaes"
ADSE = DSD / "build" / "bin" / "adse"
OUTDIR = RESULTS / "desyde"
WORK = TMP / "desyde"

THREADS = 4
SAFEDSE_TIME_LIMIT_MS = 3_600_000

# (scenario, optimized application); the other applications carry bounds.
SCENARIOS = [
    ("exp_3_1_rajp", "d_jpegEnc1"), ("exp_3_2_sosura", "c_rasta"),
    ("exp_3_3_sosujp", "d_jpegEnc1"), ("exp_3_4_sorajp", "d_jpegEnc1"),
    ("exp_3_5_surajp", "d_jpegEnc1"), ("exp_3_6_sosurajp", "d_jpegEnc1"),
    ("exp_4_1_rajp", "d_jpegEnc1"), ("exp_4_2_sosujp", "d_jpegEnc1"),
    ("exp_4_3_sorajp", "d_jpegEnc1"), ("exp_4_4_surajp", "d_jpegEnc1"),
    ("exp_4_5_sosurajp", "d_jpegEnc1"),
]
TARGET = dict(SCENARIOS)

# Published DeSyDe results (Rosvall and Sander, TODAES 23(2), 2017, Tables 1
# and 2): period of the optimized application, and whether step 2 completed
# (proven optimal). Runtimes are on their machine (Xeon E3, 8 threads).
PUBLISHED = {
    "exp_3_1_rajp": (2524, True), "exp_3_2_sosura": (327, True),
    "exp_3_3_sosujp": (2544, True), "exp_3_4_sorajp": (2544, True),
    "exp_3_5_surajp": (2544, True), "exp_3_6_sosurajp": (3882, True),
    "exp_4_1_rajp": (1388, True), "exp_4_2_sosujp": (1766, False),
    "exp_4_3_sorajp": (1766, False), "exp_4_4_surajp": (1766, False),
    "exp_4_5_sosurajp": (3302, False),
}
# Published run time in s, step 1 (last/best solution) + step 2, from the same
# tables; only the scenarios compared here. Exp. 4 SoRaJp's step 2 stopped at
# the 6 h limit without a proof.
PUBLISHED_SECONDS = {
    "exp_3_1_rajp": 9.517 + 0.007,
    "exp_3_3_sosujp": 31 * 60 + 38.485 + 12 * 60 + 43.758,
    "exp_3_4_sorajp": 36 * 60 + 49.245 + 15 * 60 + 15.048,
    "exp_3_5_surajp": 35 * 60 + 42.363 + 7 * 60 + 0.062,
    "exp_3_6_sosurajp": 47.665 + 30 * 60 + 57.879,
    "exp_4_1_rajp": 4 * 60 + 38.970 + 0.023,
    "exp_4_3_sorajp": 35 * 60 + 5.055 + 5 * 3600 + 59 * 60 + 59.745,
}


def _apps(scen: str) -> list[str]:
    """Application names in DeSyDe's order (files of sdfs/, sorted)."""
    return [p.name.split(".")[0]
            for p in sorted((TODAES / scen / "sdfs").glob("*.xml"))]


# ---------------------------------------------------------------------------
# SafeDSE
# ---------------------------------------------------------------------------
S_FIELDS = ["scenario", "rep", "target", "objective_match", "reason",
            "status", "seconds", "build_seconds", "target_period",
            "periods", "nprocs", "verify_ok", "nodes", "published_period",
            "published_proven", "periods_solver", "pin_status", "pin_ok"]


def build(scen: str) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    d = TODAES / scen
    dst = WORK / f"{scen}.dzn"
    cmd = [sys.executable, str(SAFEDSE / "tools" / "build_dzn.py")]
    for p in sorted((d / "sdfs").glob("*.xml")):
        cmd += ["--app", str(p)]
    cmd += ["--platform", str(d / "xmls" / "platform.xml"),
            "--wcets", str(d / "xmls" / "WCETs.xml"),
            "--constraints", str(d / "xmls" / "desConst.xml"),
            "--comm", "tdma", "-o", str(dst)]
    subprocess.run(cmd, check=True, cwd=SAFEDSE, capture_output=True)
    return dst


def coincide(dzn: Path, scen: str) -> tuple[bool, str]:
    """True if every feasible design has the target's period above every
    other application's bound, so max(mu) = mu[target]."""
    d = parse_dzn(dzn)
    apps = _apps(scen)
    t = apps.index(TARGET[scen]) + 1
    ub = d["period_ub"]
    others = [ub[z - 1] for z in range(1, len(apps) + 1) if z != t]
    # least WCET of each base actor of the target over its allowed bindings
    # (min_wcet, from the front end; communication actors excluded); a
    # period is at least the largest of them
    comm = re.compile(r"\.(block|send|rec)#\d+$")
    lb = max(w for i, (z, w) in enumerate(zip(d["app"], d["min_wcet"]))
             if z == t and not comm.search(d["node_name"][i]))
    ok = lb > max(others)
    return ok, f"target period >= {lb}, other bounds <= {max(others)}"


def skipped(path: Path, scen: str) -> bool:
    import csv
    if not path.exists():
        return False
    with path.open() as f:
        return any(r["scenario"] == scen and r["status"] == "skipped"
                   for r in csv.DictReader(f))


def _save(path: Path, s: dict) -> None:
    """Solutions are kept gzipped (about 0.5 MB of JSON each)."""
    with gzip.open(path, "wt") as f:
        json.dump(s, f)


def _periods(scen: str, s: dict) -> dict:
    mu = s["mu"] if isinstance(s["mu"], list) else [s["mu"]]
    return dict(zip(_apps(scen), mu))


def finish(dzn: Path, scen: str, rep, s: dict, row: dict) -> None:
    """Pin and verify. THROUGHPUT minimizes the worst period, here the
    target's; the other applications' periods are only bounded, so a
    parallel portfolio may report them above their least value.
    exact_period.pin_period fixes the design and re-solves single-threaded,
    which gives each component its least period; the design is checked
    unchanged (pin_ok) and the pinned solution goes to verify.py."""
    row["periods_solver"] = json.dumps(_periods(scen, s))
    pin = pin_period(dzn, s)
    row.update(pin_status=pin["status"], pin_ok=pin["ok"])
    if pin["ok"]:
        s = pin["solution"]
        _save(OUTDIR / f"safedse_{scen}_r{rep}.pinned.sol.json.gz", s)
    per = _periods(scen, s)
    row.update(target_period=per[TARGET[scen]], periods=json.dumps(per),
               nprocs=s.get("nprocs"))
    row["verify_ok"] = verify_solution(dzn, s, f"e7_{scen}_{rep}")["ok"]


def repin() -> None:
    """Pin and re-verify stored solutions (rows written before `finish`
    existed); rewrites e7_safedse.csv with the full header."""
    import csv
    path = RESULTS / "e7_safedse.csv"
    with path.open() as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        sol = OUTDIR / f"safedse_{row['scenario']}_r{row['rep']}.sol.json.gz"
        if row.get("pin_status") or not sol.exists():
            continue
        dzn = build(row["scenario"])
        with gzip.open(sol, "rt") as f:
            finish(dzn, row["scenario"], row["rep"], json.load(f), row)
        print(f"e7 repin {row['scenario']} r{row['rep']}: "
              f"{row['periods_solver']} -> {row['periods']} "
              f"pin_ok={row['pin_ok']} verify={row['verify_ok']}", flush=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=S_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in S_FIELDS})


def safedse(reps: int, only: list[str] | None) -> None:
    path = RESULTS / "e7_safedse.csv"
    done = existing_keys(path, ["scenario", "rep"])
    for scen, target in SCENARIOS:
        if only and scen not in only:
            continue
        for rep in range(reps):
            if (scen, str(rep)) in done or (rep > 0 and skipped(path, scen)):
                continue
            t0 = time.time()
            dzn = build(scen)
            row = {"scenario": scen, "rep": rep, "target": target,
                   "build_seconds": round(time.time() - t0, 3),
                   "nodes": parse_dzn(dzn)["n"],
                   "published_period": PUBLISHED[scen][0],
                   "published_proven": PUBLISHED[scen][1]}
            ok, why = coincide(dzn, scen)
            row.update(objective_match=ok, reason=why)
            if not ok:
                row["status"] = "skipped"
                append_row(path, S_FIELDS, row)
                print(f"e7 safedse {scen}: skipped ({why})", flush=True)
                break
            r = solve_run(str(dzn), "THROUGHPUT", {},
                          timeout=SAFEDSE_TIME_LIMIT_MS // 1000 + 120,
                          threads=THREADS,
                          time_limit_ms=SAFEDSE_TIME_LIMIT_MS)
            row.update(status=r["status"],
                       seconds=round(r.get("seconds", 0.0), 3))
            if "solution" in r:
                OUTDIR.mkdir(parents=True, exist_ok=True)
                _save(OUTDIR / f"safedse_{scen}_r{rep}.sol.json.gz",
                      r["solution"])
                finish(dzn, scen, rep, r["solution"], row)
            append_row(path, S_FIELDS, row)
            print(f"e7 safedse {scen} r{rep}: {row['status']} "
                  f"{row['seconds']}s period={row.get('target_period')} "
                  f"verify={row.get('verify_ok')}", flush=True)


# ---------------------------------------------------------------------------
# DeSyDe
# ---------------------------------------------------------------------------
D_FIELDS = ["scenario", "rep", "target", "threads", "exit", "wall_seconds",
            "step1_seconds", "step1_timeout", "step1_solutions",
            "step1_periods", "step2_seconds", "step2_timeout",
            "step2_solutions", "step2_periods", "target_period", "proven",
            "published_period", "published_proven", "cap_seconds", "capped"]
# Same wall-time budget as SafeDSE's solve: with 4 threads on this
# machine DeSyDe's step 2 ran > 1.5 h on Exp. 3 SoSuJp without finishing
# (the article: 12 min 44 s with 8 threads), and only its 6 h limit applies
# there. At the cap the run is killed and its best period so far is read
# from the result files; it then counts as not proven.
DESYDE_CAP_S = SAFEDSE_TIME_LIMIT_MS // 1000

_END = re.compile(r"===== search ended after: (\d+) s \((\d+) ms\)"
                  r"( due to time-out!)?.*?=====\s*\n(\d+) solutions found")
_PER = re.compile(r"^Period: \{([^}]*)\}", re.M)


def _parse(txt: str) -> dict:
    m = _END.search(txt)
    per = _PER.findall(txt)
    if not m:
        return {"periods": [int(x) for x in per[-1].split(",")]} if per else {}
    return {"seconds": int(m.group(2)) / 1000.0,
            "timeout": bool(m.group(3)),
            "solutions": int(m.group(4)),
            "periods": [int(x) for x in per[-1].split(",")] if per else None}


def desyde(reps: int, only: list[str] | None) -> None:
    path = RESULTS / "e7_desyde.csv"
    done = existing_keys(path, ["scenario", "rep"])
    for scen, target in SCENARIOS:
        if only and scen not in only:
            continue
        for rep in range(reps):
            if (scen, str(rep)) in done:
                continue
            out = OUTDIR / f"desyde_{scen}_r{rep}"
            (out / "out").mkdir(parents=True, exist_ok=True)
            WORK.mkdir(parents=True, exist_ok=True)
            cmd = [str(ADSE), "--config", f"{scen}/config.cfg",
                   "--output", f"{out}/",
                   "--log-file", str(WORK / f"desyde_{scen}_r{rep}.log"),
                   "--dse.th_prop", "MCR", "--dse.threads", str(THREADS)]
            t0 = time.time()
            capped = False
            try:
                p = subprocess.run(cmd, cwd=TODAES, capture_output=True,
                                   text=True, timeout=DESYDE_CAP_S)
                rc, txt = p.returncode, p.stdout + p.stderr
            except subprocess.TimeoutExpired as e:   # child is killed
                capped, rc = True, "killed"
                txt = (e.stdout or b"").decode(errors="replace") \
                    if isinstance(e.stdout, bytes) else (e.stdout or "")
            wall = time.time() - t0
            (WORK / f"desyde_{scen}_r{rep}.stdout").write_text(txt)
            s1 = _parse((out / "out" / "out_step0_results.txt").read_text()
                        if (out / "out" / "out_step0_results.txt").exists()
                        else "")
            s2 = _parse((out / "out" / "out.txt").read_text()
                        if (out / "out" / "out.txt").exists() else "")
            apps = _apps(scen)
            k = apps.index(target)
            best = s2.get("periods") or s1.get("periods")
            row = {"scenario": scen, "rep": rep, "target": target,
                   "threads": THREADS, "exit": rc,
                   "wall_seconds": round(wall, 3),
                   "step1_seconds": s1.get("seconds"),
                   "step1_timeout": s1.get("timeout"),
                   "step1_solutions": s1.get("solutions"),
                   "step1_periods": json.dumps(s1.get("periods")),
                   "step2_seconds": s2.get("seconds"),
                   "step2_timeout": s2.get("timeout"),
                   "step2_solutions": s2.get("solutions"),
                   "step2_periods": json.dumps(s2.get("periods")),
                   "target_period": best[k] if best else "",
                   "proven": (rc == 0 and "seconds" in s2
                              and not s2.get("timeout")),
                   "cap_seconds": DESYDE_CAP_S, "capped": capped,
                   "published_period": PUBLISHED[scen][0],
                   "published_proven": PUBLISHED[scen][1]}
            append_row(path, D_FIELDS, row)
            print(f"e7 desyde {scen} r{rep}: exit={rc} "
                  f"{row['wall_seconds']}s period={row['target_period']} "
                  f"proven={row['proven']}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("groups", nargs="+", choices=["safedse", "desyde", "repin"])
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--scenarios", nargs="*")
    a = ap.parse_args()
    for g in a.groups:
        if g == "repin":
            repin()
        else:
            {"safedse": safedse, "desyde": desyde}[g](a.reps, a.scenarios)


if __name__ == "__main__":
    main()

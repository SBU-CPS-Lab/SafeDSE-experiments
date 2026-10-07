#!/usr/bin/env python3
"""Breadth experiments: price sweep, communication variants, a
three-application instance, core-bounded pattern instances, tool timing,
flattening cost, other solvers, and exact cross-checks of the bounded
three-application rows.

Every group reuses the harness plumbing (harness.solve_and_verify: 4 workers,
300 s MiniZinc time limit, exact-period pin for cost objectives, verify.py on
every solution), writes one CSV row per finished run (append + flush) and
skips rows already present, so the whole file is resumable. Input variants
are data only, generated under variants/ or multi/; nothing in safedse is
edited.

    price    hardware price factor x cost profile x core bound on the
             consolidation instance (incl. Klosterman's lower SIL-3
             row)                                        -> e4_price.csv
    comm     communication variants of c_pat and pc_sobel: ideal bus,
             SIL of communication actors, pattern traffic on the bus
                                                         -> e4_comm.csv
    multi    three applications with mixed SILs and patterns, all RQ2
             flows, with and without per-application period bounds
                                                         -> e5_multi.csv
    patcons  pattern instances with a core bound, joint vs. NP: where
             promotion appears with patterns             -> e4_patcons.csv
    pipe     time of each tool of the flow on the Table III instances
             (front end, solve, pin, verifier, generator)
                                                         -> e4_pipe.csv
    flat     compile-only runs: flattening time, FlatZinc size, peak memory
             of the RQ3 and Table III instances          -> e3_flat.csv
    xsolver  the Table III instances with Gecode, Chuffed, HiGHS and
             single-worker CP-SAT                        -> e4_xsolver.csv
    multiexact  the bounded rows of `multi` with exact variant B
                                                         -> e5_multi_exact.csv

    python3 breadth.py price comm multi patcons pipe flat xsolver
    python3 breadth.py multiexact
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

import harness as H
from harness import (SAFEDSE, RESULTS, TMP, OUT, E2_INSTANCES, E2_BASELINES,
                     TIGHT, append_row, existing_keys, solve_and_verify,
                     run_baseline, _build, _e2_row, _ub, parse_dzn)
from solve import run as solve_run   # noqa: E402  (safedse/tools, via harness)

ROOT = Path(__file__).resolve().parent
VARIANT_DIR = ROOT / "variants"
MULTI_DIR = ROOT / "multi"
W4 = TMP / "breadth"

SOLVE_FIELDS = ["status", "seconds", "total_cost", "mu_max", "mu_list",
                "nprocs", "hw_cost", "dev_cost", "promotion_cost",
                "partition_total", "pattern_cost", "power", "patterns",
                "verify_ok", "mu_solver", "pin_status", "pin_seconds",
                "pin_ok"]


def _totals(row: dict) -> dict:
    s = row.get("_solution")
    if s:
        row.update(total_cost=s.get("total_cost"),
                   partition_total=s.get("partition_total"),
                   pattern_cost=s.get("pattern_cost"))
    return row


def _log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# price: the consolidation instance of RQ4 (RASTA without
# patterns, safety_rasta.xml) on mixed_noiso.xml with every `monetary`
# attribute multiplied by f. f = 1 is the s_<profile> instance, f = 4 the
# x_<profile> instance of Fig. 7 (mixed_costly.xml is mixed_noiso.xml x 4);
# both are checked against out/ before use. The cost model copy adds the
# profile `klosterman_low`: Klosterman's table as in data/cost_model.xml, but
# SIL 3 takes the lower of its two SIL-3 rows (ASIL C, 20-60 %, midpoint 140)
# and SIL 4 is extrapolated by the same rule as the file's (x 140/123 -> 159).
# ---------------------------------------------------------------------------
PRICE_FACTORS = [1, 2, 4, 8, 16]
PRICE_PROFILES = ["myklebust2015", "klosterman", "do178b", "klosterman_low"]
PRICE_BOUNDS = ["none", "3", "2", "1"]
E4P_FIELDS = ["factor", "profile", "k"] + SOLVE_FIELDS


def _price_platform(f: int) -> Path:
    src = (SAFEDSE / "data/platform/mixed_noiso.xml").read_text()
    out = re.sub(r'monetary="(\d+)"',
                 lambda m: f'monetary="{int(m.group(1)) * f}"', src)
    dst = VARIANT_DIR / f"platform_noiso_hw{f}.xml"
    dst.write_text(out.replace(
        "<platform ", f"<!-- variant of data/platform/mixed_noiso.xml: "
        f"every monetary value x {f} (breadth.py) -->\n<platform ",
        1))
    return dst


def _cost_model_variant() -> Path:
    src = (SAFEDSE / "data/cost_model.xml").read_text()
    low = ('  <!-- variant (breadth.py): Klosterman\'s table '
           'with the\n       ASIL C row (20-60 %, midpoint 140) for SIL 3; '
           'SIL 4 extrapolated by\n       the same ratio, 140 * 140/123 = '
           '159. -->\n'
           '  <profile name="klosterman_low">\n'
           '    <sil level="0" multiplier="100"/>\n'
           '    <sil level="1" multiplier="113"/>\n'
           '    <sil level="2" multiplier="123"/>\n'
           '    <sil level="3" multiplier="140"/>\n'
           '    <sil level="4" multiplier="159"/>\n'
           '  </profile>\n\n')
    k = src.index("  <!-- Baseline development cost")
    dst = VARIANT_DIR / "cost_model_klosterman_low.xml"
    dst.write_text(src[:k] + low + src[k:])
    return dst


def _price_build(f: int, prof: str) -> Path:
    W4.mkdir(parents=True, exist_ok=True)
    dst = W4 / f"hw{f}_{prof}.dzn"
    cmd = [sys.executable, "tools/build_dzn.py",
           "--app", "data/apps/c_rasta.hsdf.xml",
           "--platform", str(_price_platform(f)),
           "--wcets", "data/WCETs_mixed.xml",
           "--constraints", "data/desConst.xml",
           "--cost-model", str(_cost_model_variant()),
           "--safety", "data/safety_rasta.xml",
           "--cost-profile", prof, "-o", str(dst)]
    p = subprocess.run(cmd, cwd=SAFEDSE, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"price build hw{f} {prof} failed:\n{p.stderr[-1500:]}")
    ref = {1: "s", 4: "x"}.get(f)
    if ref and prof != "klosterman_low":
        strip = lambda t: re.sub(r"^% platform .*$", "", t, flags=re.M)
        if strip(dst.read_text()) != strip((OUT / f"{ref}_{prof}.dzn")
                                           .read_text()):
            raise SystemExit(f"hw{f} {prof}: differs from out/{ref}_{prof}.dzn")
    return dst


def price() -> None:
    path = RESULTS / "e4_price.csv"
    done = existing_keys(path, ["factor", "profile", "k"])
    for f in PRICE_FACTORS:
        for prof in PRICE_PROFILES:
            if all((str(f), prof, k) in done for k in PRICE_BOUNDS):
                continue
            dzn = _price_build(f, prof)
            for k in PRICE_BOUNDS:
                if (str(f), prof, k) in done:
                    continue
                b = {} if k == "none" else {"NPROCS": int(k)}
                row = _totals(solve_and_verify(dzn, "TOTALCOST", b,
                                               tag=f"e4p_hw{f}_{prof}_{k}"))
                row.update(factor=f, profile=prof, k=k)
                append_row(path, E4P_FIELDS, row)
                _log(f"price hw{f} {prof} k={k}: {row['status']} "
                     f"total={row.get('total_cost')} cores={row.get('nprocs')} "
                     f"promo={row.get('promotion_cost')} "
                     f"verify_ok={row['verify_ok']}")


# ---------------------------------------------------------------------------
# comm: the two pattern instances with the TDMA bus, rebuilt with
# one front-end flag changed. `tdma` is the instance of Table III; `ideal`
# drops --comm tdma (no communication actors); `sil_inherit` / `sil_core`
# give communication actors their sender's SIL / the core's SIL instead of
# exempting them (--comm-sil); `scope_all` also routes the channels of
# pattern components over the bus (--comm-scope all). pc_sobel keeps the
# loosened bound of RQ2/RQ4 (period 100000 through a desConst copy).
# ---------------------------------------------------------------------------
COMM_INSTANCES = ["c_pat", "pc_sobel"]
COMM_VARIANTS = {"tdma": [], "ideal": None, "sil_inherit": ["--comm-sil", "inherit"],
                 "sil_core": ["--comm-sil", "core"],
                 "scope_all": ["--comm-scope", "all"]}
E4C_FIELDS = ["instance", "variant", "metric", "n"] + SOLVE_FIELDS


def _with_period(name: str, spec: dict, periods: dict[str, int] | None,
                 stem: str) -> dict:
    """Copy of spec whose desConst gives each named application its period
    (the front end derives min_procs and the implied constraints from it)."""
    if not periods:
        return spec
    W4.mkdir(parents=True, exist_ok=True)
    args = list(spec["args"])
    ci = args.index("--constraints") + 1
    src = args[ci]
    con = (Path(src) if Path(src).is_absolute() else SAFEDSE / src).read_text()
    for app, p in periods.items():
        con, k = re.subn(rf'(app_name="{app}"\s+period=")[-0-9]+"',
                         rf'\g<1>{p}"', con)
        if k != 1:
            raise SystemExit(f"{src}: no period for {app}")
    dst = W4 / f"{stem}_desConst.xml"
    dst.write_text(con)
    args[ci] = str(dst)
    return dict(spec, args=args)


def comm() -> None:
    path = RESULTS / "e4_comm.csv"
    done = existing_keys(path, ["instance", "variant", "metric"])
    W4.mkdir(parents=True, exist_ok=True)
    for name in COMM_INSTANCES:
        spec = E2_INSTANCES[name]
        if spec.get("period_ub"):
            spec = _with_period(name, spec, {"a_sobel": spec["period_ub"]},
                                f"comm_{name}")
        for var, extra in COMM_VARIANTS.items():
            if all((name, var, m) in done for m in ("TOTALCOST", "THROUGHPUT")):
                continue
            args = list(spec["args"])
            if extra is None:
                i = args.index("--comm")
                del args[i:i + 2]
                extra = []
            dzn = _build(name, dict(spec, args=args),
                         W4 / f"comm_{name}_{var}.dzn", extra=extra)
            if var == "tdma" and not spec.get("period_ub") and \
                    dzn.read_bytes() != (OUT / f"{name}.dzn").read_bytes():
                raise SystemExit(f"comm {name}: tdma build differs from out/")
            n = parse_dzn(str(dzn))["n"]
            for metric in ("TOTALCOST", "THROUGHPUT"):
                if (name, var, metric) in done:
                    continue
                row = _totals(solve_and_verify(
                    dzn, metric, tag=f"e4c_{name}_{var}_{metric}"))
                row = _e2_row(row, parse_dzn(str(dzn)))
                row.update(instance=name, variant=var, metric=metric, n=n)
                append_row(path, E4C_FIELDS, row)
                _log(f"comm {name} {var} {metric}: {row['status']} "
                     f"{row['seconds']}s total={row.get('total_cost')} "
                     f"mu={row.get('mu_max')} verify_ok={row['verify_ok']}")


# ---------------------------------------------------------------------------
# multi: Sobel, SUSAN (Rosvall's HSDF files) and RASTA on the
# two-type card platform (mixed_noiso.xml), catalog data/patterns.yaml, fault
# model H, safety multi/safety_3app.xml (RASTA as in the running example,
# Sobel SIL 2, SUSAN SIL 0). WCETs: multi/WCETs_3app.xml, generated by
# safedse's mkwcets.py for these three applications (checker scale 0.3, as
# every other WCET file); its RASTA entries equal data/WCETs_mixed.xml.
# mu*_app: least period of each application alone (same platform, catalog
# and SILs), the reference for the per-application bounds ceil(f mu*_app).
# ---------------------------------------------------------------------------
M3_APPS = {"a_sobel": "data/rosvall/a_sobel.hsdf.xml",
           "b_susan": "data/rosvall/b_susan.hsdf.xml",
           "c_rasta": "data/apps/c_rasta.hsdf.xml"}
M3_NAME = "m_3app"
E5_FIELDS = ["instance", "factor", "baseline", "rep", "app", "mu_star",
             "period_ub"] + SOLVE_FIELDS + ["fix_ok", "step1_status",
                                            "step1_seconds", "step1_total_cost",
                                            "step1_verify_ok"]
M3_REPS = 5


def m3_spec(apps: list[str] | None = None) -> dict:
    apps = apps or list(M3_APPS)
    args = []
    for a in apps:
        args += ["--app", M3_APPS[a]]
    args += ["--platform", "data/platform/mixed_noiso.xml",
             "--wcets", str(MULTI_DIR / "WCETs_3app.xml"),
             "--constraints", str(MULTI_DIR / "desConst_3app.xml"),
             "--cost-model", "data/cost_model.xml"]
    return dict(args=args, safety=str(MULTI_DIR / "safety_3app.xml"),
                catalogue="data/patterns.yaml", aliases="")


def _check_m3_wcets() -> None:
    pat = r'(  <mapping task_type="([^"]+)">.*?</mapping>)'
    blocks = lambda t: {k: b for b, k in re.findall(pat, t, re.S)}
    gen = TMP / "mkwcets_m3_check.xml"
    cmd = [sys.executable, "tools/mkwcets.py"]
    for a in M3_APPS.values():
        cmd += ["--app", a]
    cmd += ["--platform", "data/platform/mixed_noiso.xml", "--patterns",
            "data/patterns.yaml", "-o", str(gen)]
    subprocess.run(cmd, cwd=SAFEDSE, check=True, capture_output=True)
    if gen.read_text() != (MULTI_DIR / "WCETs_3app.xml").read_text():
        raise SystemExit("multi/WCETs_3app.xml is not mkwcets.py's output")
    # RASTA's keys (types, as data/WCETs_mixed.xml uses them)
    mixed = blocks((SAFEDSE / "data/WCETs_mixed.xml").read_text())
    new = blocks(gen.read_text())
    for t in ["FrontEnd", "Rasta", "Pow", "Aud", "RFilter", "BackEnd",
              "checker_FrontEnd", "checker_BackEnd", "checker_Pow"]:
        if mixed.get(t) != new.get(t):
            raise SystemExit(f"multi WCETs: {t} differs from WCETs_mixed.xml")


def _single_app_safety(app: str) -> Path:
    """The safety spec restricted to one application's actors (the front
    end rejects actor names that are in no graph)."""
    from sdf3 import parse_sdf3
    names = {a.name for a in parse_sdf3(str(SAFEDSE / M3_APPS[app])).actors}
    txt = (MULTI_DIR / "safety_3app.xml").read_text()
    keep = [ln for ln in txt.splitlines()
            if "<actor " not in ln or re.search(r'name="([^"]+)"', ln)
            .group(1) in names]
    dst = W4 / f"safety_{app}.xml"
    dst.write_text("\n".join(keep) + "\n")
    return dst


def m3_build(periods: dict[str, int] | None, stem: str,
             apps: list[str] | None = None) -> dict[str, Path]:
    """joint / force-no-patterns / no-promotion instances, as
    harness.build_e2_instances does for one application."""
    W4.mkdir(parents=True, exist_ok=True)
    spec = m3_spec(apps)
    if apps and len(apps) == 1:
        spec = dict(spec, safety=str(_single_app_safety(apps[0])))
    spec = _with_period(M3_NAME, spec, periods, stem)
    joint = _build(M3_NAME, spec, W4 / f"{stem}.dzn")
    nopat = _build(M3_NAME, spec, W4 / f"{stem}_nopat.dzn",
                   extra=["--force-no-patterns"])
    xml = Path(spec["safety"]).read_text()
    xml2, k = re.subn(r'allow_promotion="true"', 'allow_promotion="false"', xml)
    if k != 1:
        raise SystemExit("m_3app: expected one allow_promotion=\"true\"")
    saf = W4 / f"{stem}_nopromo.safety.xml"
    saf.write_text(xml2)
    nopromo = _build(M3_NAME, spec, W4 / f"{stem}_nopromo.dzn", safety=str(saf))
    return {"joint": joint, "nopat": nopat, "nopromo": nopromo}


def multi() -> None:
    path = RESULTS / "e5_multi.csv"
    done = existing_keys(path, ["factor", "baseline", "rep", "app"])
    _check_m3_wcets()
    spec = m3_spec()
    # -- reference: each application alone, least period -------------------
    mu = {}
    if path.exists():
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                if r["baseline"] == "min_period_alone" and \
                        r["status"] == "OPTIMAL" and r["verify_ok"] == "True":
                    mu[r["app"]] = int(r["mu_max"])
    for app in M3_APPS:
        if ("-", "min_period_alone", "1", app) in done:
            continue
        dz = m3_build(None, f"m3_alone_{app}", [app])
        row = solve_and_verify(dz["joint"], "THROUGHPUT", tag=f"e5_alone_{app}")
        row = _e2_row(_totals(row), parse_dzn(str(dz["joint"])))
        row.update(instance=M3_NAME, factor="-", baseline="min_period_alone",
                   rep=1, app=app)
        append_row(path, E5_FIELDS, row)
        _log(f"multi alone {app}: {row['status']} mu={row.get('mu_max')} "
             f"verify_ok={row['verify_ok']}")
        if row["status"] == "OPTIMAL" and row["verify_ok"] is True:
            mu[app] = int(row["mu_max"])
    # -- all flows, no bound and per-application bounds ----------------------
    for fac in ["none"] + TIGHT:
        if fac != "none" and len(mu) < len(M3_APPS):
            _log(f"multi {fac}: no mu* for every application; skipped")
            continue
        periods = None if fac == "none" else {a: _ub(mu[a], fac) for a in mu}
        keys = [(fac, b, "1", "") for b in E2_BASELINES]
        if fac == "none":
            keys += [(fac, "joint", str(k), "") for k in range(2, M3_REPS + 1)]
        if all(k in done for k in keys):
            continue
        dz = m3_build(periods, f"m3_{fac}")
        if fac == "none":   # kept with the inputs (Table III reads |G+|)
            (MULTI_DIR / f"{M3_NAME}.dzn").write_bytes(dz["joint"].read_bytes())
        d = parse_dzn(str(dz["joint"]))
        pub = json.dumps(periods) if periods else ""
        for b in E2_BASELINES:
            if (fac, b, "1", "") in done:
                continue
            row = run_baseline(b, spec, dz, d, f"e5_{fac}_{b}")
            row.update(instance=M3_NAME, factor=fac, baseline=b, rep=1, app="",
                       period_ub=pub)
            append_row(path, E5_FIELDS, row)
            _log(f"multi {fac} {b}: {row['status']} {row['seconds']}s "
                 f"total={row.get('total_cost')} cores={row.get('nprocs')} "
                 f"promo={row.get('promotion_cost')} "
                 f"verify_ok={row['verify_ok']}")
        if fac == "none":
            for k in range(2, M3_REPS + 1):
                if (fac, "joint", str(k), "") in done:
                    continue
                row = run_baseline("joint", spec, dz, d, f"e5_none_joint_{k}")
                row.update(instance=M3_NAME, factor=fac, baseline="joint",
                           rep=k, app="", period_ub="")
                append_row(path, E5_FIELDS, row)
                _log(f"multi none joint rep {k}: {row['status']} "
                     f"{row['seconds']}s total={row.get('total_cost')}")


# ---------------------------------------------------------------------------
# patcons: every pattern instance of Table III (and m_3app) with at
# most k cores, k from one below the joint optimum's core count down to the
# first k where joint is infeasible; joint and NP (allow_promotion=false).
# ---------------------------------------------------------------------------
E4PC_FIELDS = ["instance", "baseline", "k", "cores_opt"] + SOLVE_FIELDS


def _joint_cores() -> dict[str, int]:
    out = {}
    with (RESULTS / "e2_joint.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            if r["baseline"] == "joint" and r["status"] == "OPTIMAL" \
                    and r["verify_ok"] == "True":
                out[r["instance"]] = int(r["nprocs"])
    p = RESULTS / "e5_multi.csv"
    if p.exists():
        with p.open(newline="") as f:
            for r in csv.DictReader(f):
                if (r["factor"], r["baseline"], r["rep"]) == ("none", "joint", "1") \
                        and r["status"] == "OPTIMAL" and r["verify_ok"] == "True":
                    out[M3_NAME] = int(r["nprocs"])
    return out


def patcons() -> None:
    path = RESULTS / "e4_patcons.csv"
    done = {}
    if path.exists():
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                done[(r["instance"], r["baseline"], r["k"])] = r["status"]
    cores = _joint_cores()
    for name in list(E2_INSTANCES) + [M3_NAME]:
        if name not in cores:
            _log(f"patcons {name}: no verified joint optimum; skipped")
            continue
        if name == M3_NAME:
            spec, dz = m3_spec(), m3_build(None, "m3_none")
        else:
            spec = E2_INSTANCES[name]
            dz = H.build_e2_instances(name, spec)
        d = parse_dzn(str(dz["joint"]))
        for k in range(cores[name] - 1, 0, -1):
            for b in ("joint", "NP"):
                if (name, b, str(k)) in done:
                    continue
                src = dz["joint"] if b == "joint" else dz["nopromo"]
                row = _totals(solve_and_verify(src, "TOTALCOST", {"NPROCS": k},
                                               tag=f"e4pc_{name}_{b}_{k}"))
                row = _e2_row(row, d)
                row.update(instance=name, baseline=b, k=k, cores_opt=cores[name])
                append_row(path, E4PC_FIELDS, row)
                done[(name, b, str(k))] = row["status"]
                _log(f"patcons {name} {b} k={k}: {row['status']} "
                     f"{row['seconds']}s total={row.get('total_cost')} "
                     f"promo={row.get('promotion_cost')} "
                     f"verify_ok={row['verify_ok']}")
            if done.get((name, "joint", str(k))) == "UNSAT":
                break


# ---------------------------------------------------------------------------
# pipe: the whole flow on each Table III instance and m_3app: front-end
# build, cost solve (incl. flattening), exact-period pin, verifier, and GSN
# generator, each timed on its own (one run).
# ---------------------------------------------------------------------------
E4PIPE_FIELDS = ["instance", "build_s", "status", "solve_s", "pin_s",
                 "verify_s", "verify_ok", "gsn_s", "gsn_rc", "gsn_elements"]
TAB3_INSTANCES = ["p_rasta", "f_random_hw", "d_random_hw", "d_systematic_sw",
                  "f_sw3", "f_both3", "c_pat", "pc_sobel", "v_nvp"]


def pipe() -> None:
    from exact_period import pin_period
    path = RESULTS / "e4_pipe.csv"
    done = existing_keys(path, ["instance"])
    for name in TAB3_INSTANCES + [M3_NAME]:
        if (name,) in done:
            continue
        spec = m3_spec() if name == M3_NAME else E2_INSTANCES[name]
        if spec.get("period_ub"):
            spec = _with_period(name, spec, {"a_sobel": spec["period_ub"]},
                                f"pipe_{name}")
        W4.mkdir(parents=True, exist_ok=True)
        dzn = W4 / f"pipe_{name}.dzn"
        t0 = time.time()
        _build(name, spec, dzn)
        row = {"instance": name, "build_s": round(time.time() - t0, 3)}
        r = solve_run(str(dzn), "TOTALCOST", timeout=H.PY_TIMEOUT,
                      threads=H.THREADS, time_limit_ms=H.TIME_LIMIT_MS)
        row.update(status=r["status"], solve_s=round(r.get("seconds", 0), 3))
        if "solution" in r:
            pin = pin_period(dzn, r["solution"])
            row["pin_s"] = pin["seconds"]
            s = pin["solution"] if pin["ok"] else r["solution"]
            solp = W4 / f"pipe_{name}.sol.json"
            repp = W4 / f"pipe_{name}.report.json"
            gsnp = W4 / f"pipe_{name}.gsn.json"
            solp.write_text(json.dumps(s))
            t0 = time.time()
            subprocess.run([sys.executable, str(SAFEDSE / "tools/verify.py"),
                            "--dzn", str(dzn), "--solution", str(solp),
                            "--quiet", "--json-report", str(repp)], check=False)
            row["verify_s"] = round(time.time() - t0, 3)
            row["verify_ok"] = json.loads(repp.read_text())["ok"]
            t0 = time.time()
            g = subprocess.run([sys.executable, str(SAFEDSE / "tools/gsn.py"),
                                "--dzn", str(dzn), "--solution", str(solp),
                                "--out", str(gsnp), "--patterns",
                                str(SAFEDSE / spec["catalogue"]), "--report",
                                str(repp), "--quiet"],
                               capture_output=True, text=True)
            row["gsn_s"] = round(time.time() - t0, 3)
            row["gsn_rc"] = g.returncode
            if gsnp.exists():
                doc = json.loads(gsnp.read_text())
                row["gsn_elements"] = len(doc.get("elements", doc.get("nodes", [])))
        append_row(path, E4PIPE_FIELDS, row)
        _log(f"pipe {name}: build {row['build_s']}s solve {row.get('solve_s')}s "
             f"pin {row.get('pin_s')}s verify {row.get('verify_s')}s "
             f"({row.get('verify_ok')}) gsn {row.get('gsn_s')}s "
             f"rc={row.get('gsn_rc')}")


# ---------------------------------------------------------------------------
# flat: compile only (minizinc -c, cp-sat back end, the data of
# the real solve: objective and bounds as solve.py passes them). Wall time,
# peak resident memory of the compiler (/usr/bin/time %M, largest child),
# FlatZinc size in bytes, variables and constraints. RQ3: the throughput
# stage on free.dzn (every graph) and the cost stage on final.dzn (where the
# throughput stage gave a period); Table III and m_3app: the cost stage.
# ---------------------------------------------------------------------------
E3F_FIELDS = ["set", "tag", "param", "index", "stage", "status", "seconds",
              "max_rss_mb", "fzn_bytes", "fzn_vars", "fzn_constraints"]
FLAT_TIMEOUT = 1800


def flatten(dzn: Path, metric: str, tag: str) -> dict:
    W4.mkdir(parents=True, exist_ok=True)
    fzn, ozn = W4 / f"{tag}.fzn", W4 / f"{tag}.ozn"
    ub = [10 ** 9] * len(H.METRICS)
    data = (f"opt_metric={metric}; ub={ub}; use_parent_symmetry=true; ")
    tf = W4 / f"{tag}.time"
    cmd = ["/usr/bin/time", "-f", "%e %M", "-o", str(tf), "minizinc",
           "--solver", "cp-sat", "-c", "--fzn", str(fzn), "--ozn", str(ozn),
           "--output-mode", "json", str(SAFEDSE / "model/dse.mzn"), str(dzn),
           "-D", data]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=FLAT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "seconds": FLAT_TIMEOUT}
    row = {"status": "OK" if p.returncode == 0 else f"RC{p.returncode}"}
    if tf.exists():
        sec, rss = tf.read_text().split()[-2:]
        row.update(seconds=float(sec), max_rss_mb=round(int(rss) / 1024, 1))
    if fzn.exists():
        row["fzn_bytes"] = fzn.stat().st_size
        nv = nc = 0   # scalar declarations; var arrays only alias them
        with fzn.open() as f:
            for ln in f:
                nv += ln.startswith("var ")
                nc += ln.startswith("constraint ")
        row.update(fzn_vars=nv, fzn_constraints=nc)
        fzn.unlink()
    if ozn.exists():
        ozn.unlink()
    return row


def flat() -> None:
    path = RESULTS / "e3_flat.csv"
    done = existing_keys(path, ["set", "tag", "stage"])
    jobs = []
    for name in TAB3_INSTANCES:
        dzn = OUT / f"{name}.dzn"
        if name == "pc_sobel":   # the loosened bound of RQ2/RQ4
            spec = _with_period(name, E2_INSTANCES[name],
                                {"a_sobel": E2_INSTANCES[name]["period_ub"]},
                                "flat_pc_sobel")
            W4.mkdir(parents=True, exist_ok=True)
            dzn = _build(name, spec, W4 / "flat_pc_sobel.dzn")
        jobs.append(("table", name, "", "", "TOTALCOST", dzn))
    jobs.append(("table", M3_NAME, "", "", "TOTALCOST", "m3"))
    with (RESULTS / "e3_scal.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            d = ROOT / "synthetic" / "derived" / r["tag"]
            if (d / "free.dzn").exists():
                jobs.append(("rq3", r["tag"], r["param"], r["index"],
                             "THROUGHPUT", d / "free.dzn"))
            if (d / "final.dzn").exists() and r["gen_status"] == "READY":
                jobs.append(("rq3", r["tag"], r["param"], r["index"],
                             "TOTALCOST", d / "final.dzn"))
    for st, tag, param, idx, metric, dzn in jobs:
        if (st, tag, metric) in done:
            continue
        if dzn == "m3":
            dzn = m3_build(None, "m3_none")["joint"]
        row = flatten(Path(dzn), metric, f"flat_{tag}_{metric}")
        row.update(set=st, tag=tag, param=param, index=idx, stage=metric)
        append_row(path, E3F_FIELDS, row)
        _log(f"flat {st} {tag} {metric}: {row['status']} "
             f"{row.get('seconds')}s {row.get('max_rss_mb')} MB "
             f"fzn {row.get('fzn_bytes')} B vars {row.get('fzn_vars')} "
             f"cons {row.get('fzn_constraints')}")


# ---------------------------------------------------------------------------
# xsolver: the Table III instances and m_3app, TOTALCOST without a
# bound, 300 s, with the other MiniZinc back ends of env.txt at one thread
# (solve.py passes CP-SAT-only flags for more), and CP-SAT with one worker
# for comparison. A solution is pinned (exact-period step, CP-SAT) and
# verified like every other; its cost is compared with the proven optimum.
# ---------------------------------------------------------------------------
XSOLVERS = {"gecode": "gecode", "chuffed": "chuffed", "highs": "highs",
            "cp-sat-1": "cp-sat"}
E4X_FIELDS = ["instance", "solver", "status", "seconds", "total_cost",
              "verify_ok", "pin_ok", "mu_solver", "note"]


def xsolver() -> None:
    from exact_period import pin_period
    path = RESULTS / "e4_xsolver.csv"
    done = existing_keys(path, ["instance", "solver"])
    for name in TAB3_INSTANCES + [M3_NAME]:
        if all((name, s) in done for s in XSOLVERS):
            continue
        if name == M3_NAME:
            dzn = m3_build(None, "m3_none")["joint"]
        elif name == "pc_sobel":
            spec = _with_period(name, E2_INSTANCES[name],
                                {"a_sobel": E2_INSTANCES[name]["period_ub"]},
                                "xs_pc_sobel")
            W4.mkdir(parents=True, exist_ok=True)
            dzn = _build(name, spec, W4 / "xs_pc_sobel.dzn")
        else:
            dzn = OUT / f"{name}.dzn"
        for sname, solver in XSOLVERS.items():
            if (name, sname) in done:
                continue
            r = solve_run(str(dzn), "TOTALCOST", solver=solver,
                          timeout=H.PY_TIMEOUT, threads=1,
                          time_limit_ms=H.TIME_LIMIT_MS)
            row = {"instance": name, "solver": sname, "status": r["status"],
                   "seconds": round(r.get("seconds", 0.0), 3)}
            if "solution" in r:
                s = r["solution"]
                row["total_cost"] = s.get("total_cost")
                mu = s["mu"] if isinstance(s["mu"], list) else [s["mu"]]
                row["mu_solver"] = max(mu)
                pin = pin_period(dzn, s)
                row["pin_ok"] = pin["ok"]
                rep = H.verify_solution(dzn, pin["solution"] if pin["ok"] else s,
                                        f"e4x_{name}_{sname}")
                row["verify_ok"] = rep["ok"]
            else:
                txt = (r.get("stderr") or "") + (r.get("stdout") or "")
                row["note"] = " ".join(txt.split())[-300:]
            append_row(path, E4X_FIELDS, row)
            _log(f"xsolver {name} {sname}: {row['status']} {row['seconds']}s "
                 f"total={row.get('total_cost')} verify={row.get('verify_ok')} "
                 f"{row.get('note', '')[:120]}")


# ---------------------------------------------------------------------------
# multiexact: the bounded rows of e5_multi.csv again with exact
# variant B (exact_check.activate(): solve, fix wrappers and pin step), so an
# infeasibility or optimum under a period bound holds for the deployed MSAG,
# as exact_check.py does for RQ2/RQ4. Same instances, flows and bounds.
# ---------------------------------------------------------------------------
E5X_FIELDS = ["factor", "baseline", "old_status", "old_total_cost"] + \
    SOLVE_FIELDS + ["agree"]


def multiexact() -> None:
    import exact_check
    exact_check.activate()
    path = RESULTS / "e5_multi_exact.csv"
    done = existing_keys(path, ["factor", "baseline"])
    with (RESULTS / "e5_multi.csv").open(newline="") as f:
        old = [r for r in csv.DictReader(f)
               if r["factor"] not in ("-", "none") and r["rep"] == "1"]
    spec = m3_spec()
    for fac in TIGHT:
        rows = [r for r in old if r["factor"] == fac]
        if not rows or all((fac, r["baseline"]) in done for r in rows):
            continue
        periods = json.loads(rows[0]["period_ub"])
        dz = m3_build(periods, f"m3x_{fac}")
        d = parse_dzn(str(dz["joint"]))
        for r in rows:
            b = r["baseline"]
            if (fac, b) in done:
                continue
            row = run_baseline(b, spec, dz, d, f"e5x_{fac}_{b}")
            row.update(factor=fac, baseline=b, old_status=r["status"],
                       old_total_cost=r["total_cost"])
            concl = {"OPTIMAL", "UNSAT", "STEP1_UNSAT"}
            if r["status"] in concl and row["status"] in concl:
                row["agree"] = (r["status"] == row["status"] and
                                str(r["total_cost"] or "") ==
                                str(row.get("total_cost") or ""))
            append_row(path, E5X_FIELDS, row)
            _log(f"multiexact {fac} {b}: {row['status']} (model "
                 f"{r['status']}) total={row.get('total_cost')} (model "
                 f"{r['total_cost']}) agree={row.get('agree')}")


def multiexactlong() -> None:
    """The joint rows of e5_multi_exact.csv where the model proved UNSAT and
    variant B was left open at the 300 s limit, again with variant B and a
    3600 s limit
    -> e5_multi_exact_long.csv (same fields)."""
    import exact_check
    exact_check.activate()
    H.TIME_LIMIT_MS = 3_600_000
    src = RESULTS / "e5_multi_exact.csv"
    path = RESULTS / "e5_multi_exact_long.csv"
    done = existing_keys(path, ["factor", "baseline"])
    with src.open(newline="") as f:
        open_rows = [r for r in csv.DictReader(f)   # an open infeasibility
                     if r["baseline"] == "joint" and r["agree"] == ""
                     and r["old_status"] == "UNSAT"]
    with (RESULTS / "e5_multi.csv").open(newline="") as f:
        pub = {r["factor"]: r["period_ub"] for r in csv.DictReader(f)
               if r["baseline"] == "joint" and r["rep"] == "1"}
    for r in open_rows:
        fac = r["factor"]
        if (fac, "joint") in done:
            continue
        dz = m3_build(json.loads(pub[fac]), f"m3x_{fac}")
        row = _totals(solve_and_verify(dz["joint"], "TOTALCOST",
                                       tag=f"e5xl_{fac}_joint", timeout=3700))
        row = _e2_row(row, parse_dzn(str(dz["joint"])))
        row.update(factor=fac, baseline="joint", old_status=r["old_status"],
                   old_total_cost=r["old_total_cost"])
        concl = {"OPTIMAL", "UNSAT"}
        if r["old_status"] in concl and row["status"] in concl:
            row["agree"] = (r["old_status"] == row["status"] and
                            str(r["old_total_cost"] or "") ==
                            str(row.get("total_cost") or ""))
        append_row(path, E5X_FIELDS, row)
        _log(f"multiexactlong {fac} joint: {row['status']} {row['seconds']}s "
             f"(model {r['old_status']}) agree={row.get('agree')}")


def xnotes() -> None:
    """solve.py reports MiniZinc's =====ERROR===== as UNKNOWN. For every
    xsolver row without a solution that ended in under 10 s, re-run the
    back end without a time limit and, if it prints =====ERROR=====, set
    the row's status to ERROR and its note to the back end's message.
    Rewrites e4_xsolver.csv (no solves of record)."""
    path = RESULTS / "e4_xsolver.csv"
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    ub = [10 ** 9] * len(H.METRICS)
    for r in rows:
        if r["total_cost"] or float(r["seconds"]) >= 10 or r["status"] == "ERROR":
            continue
        dzn = (MULTI_DIR / f"{M3_NAME}.dzn" if r["instance"] == M3_NAME else
               W4 / "xs_pc_sobel.dzn" if r["instance"] == "pc_sobel" else
               OUT / f"{r['instance']}.dzn")
        p = subprocess.run(["minizinc", "--solver", XSOLVERS[r["solver"]],
                            str(SAFEDSE / "model/dse.mzn"), str(dzn), "-D",
                            f"opt_metric=TOTALCOST; ub={ub}; "
                            f"use_parent_symmetry=true; "],
                           capture_output=True, text=True, timeout=120)
        if "=====ERROR=====" in p.stdout:
            msg = [ln for ln in p.stderr.splitlines() if ln.strip()]
            r["status"], r["note"] = "ERROR", (msg[-1] if msg else "")[:200]
            _log(f"xnotes {r['instance']} {r['solver']}: ERROR {r['note']}")
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=E4X_FIELDS)
        w.writeheader()
        w.writerows(rows)


GROUPS = {"price": price, "comm": comm, "multi": multi, "patcons": patcons,
          "pipe": pipe, "flat": flat, "xsolver": xsolver,
          "multiexact": multiexact, "multiexactlong": multiexactlong,
          "xnotes": xnotes}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("groups", nargs="+", choices=list(GROUPS))
    a = ap.parse_args()
    t0 = time.time()
    for g in a.groups:
        _log(f"=== {g} ===")
        GROUPS[g]()
    _log(f"done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

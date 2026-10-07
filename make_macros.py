#!/usr/bin/env python3
"""results/*.csv -> generated/paper/results-macros.tex, generated/paper/data/*.csv
and generated/paper/tables/tab-results.tex.

Every number in the paper is a macro generated here, never typed by hand, and
a run without VERIFY OK never becomes one. This script
also refuses (prints a WARNING, emits no macro) when repetitions of the same
run disagree on status or objective, since that is nondeterminism worth
surfacing rather than averaging away.

    python3 make_macros.py
"""
from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import PAPER_OUT, ROOT, SAFEDSE  # noqa: E402

RESULTS = ROOT / "results"
PAPER = PAPER_OUT
MACROS = PAPER / "results-macros.tex"
DATA = PAPER / "data"
TABLES = PAPER / "tables"
SAFEDSE_OUT = SAFEDSE / "out"

macros: dict[str, str] = {}
warnings: list[str] = []


def defmacro(name: str, value) -> None:
    assert name.isalpha(), f"macro name must be letters only: {name}"
    if name in macros and macros[name] != str(value):
        warnings.append(f"macro \\res{name} redefined: {macros[name]!r} -> {value!r}")
    macros[name] = str(value)


def read_csv(name: str) -> list[dict]:
    path = RESULTS / name
    if not path.exists():
        warnings.append(f"{name} not found; its macros are skipped")
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def fnum(x, nd=1) -> str:
    return f"{float(x):.{nd}f}"


# ---------------------------------------------------------------------------
# RQ1: e1_parity.csv
# ---------------------------------------------------------------------------
PROFILE_LABEL = {"myklebust2015": "Myklebust", "klosterman": "Klosterman",
                 "do178b": "DoOneSeventyEightB"}
FM_LABEL = {"random_hw": "RandomHw", "systematic_sw": "SystematicSw",
            "both": "Both"}
APP_LABEL = {"r_a_sobel": "Sobel", "r_b_susan": "Susan", "r_c_rasta": "Rasta"}


def rq1() -> None:
    rows = read_csv("e1_parity.csv")
    by_key: dict[tuple, list[dict]] = {}
    for r in rows:
        by_key.setdefault((r["instance"], r["config"]), []).append(r)

    for stem, label in APP_LABEL.items():
        grp = by_key.get((stem, "onestep"), [])
        if not grp:
            continue
        if not all(r["verify_ok"] == "True" for r in grp):
            warnings.append(f"{stem}: not all reps verify OK, skipped -- "
                            f"{[r['verify_ok'] for r in grp]}")
            continue
        mus = {r["mu_max"] for r in grp}
        # The period of an HWCOST optimum is not unique: report the
        # largest over the repetitions against the bound, and the single
        # value only when all repetitions agree.
        mu = max(int(m) for m in mus)
        bound = int(grp[0]["published_bound"])
        secs = [float(r["seconds"]) for r in grp]
        defmacro(f"OneStep{label}MuMax", mu)
        defmacro(f"OneStep{label}Bound", bound)
        defmacro(f"OneStep{label}Time", fnum(statistics.median(secs)))
        hw = {r["hw_cost"] for r in grp}
        if len(hw) == 1:
            defmacro(f"OneStep{label}HwCost", int(next(iter(hw))))
        if len(mus) > 1:
            warnings.append(f"{stem}: reps disagree on mu -- {mus} "
                            f"(only the maximum is a macro)")
        else:
            defmacro(f"OneStep{label}Mu", mu)
        if mu > bound:
            warnings.append(f"{stem}: measured mu {mu} EXCEEDS the published "
                            f"bound {bound} (expected: mu <= bound)")

    onestep = by_key.get(("r_all", "onestep"), [])
    twostep = by_key.get(("r_all", "twostep"), [])
    if onestep:
        secs = [float(r["seconds"]) for r in onestep]
        statuses = {r["status"] for r in onestep}
        defmacro("RosvallAllOnestepTime", fnum(statistics.median(secs)))
        defmacro("RosvallAllOnestepStatus", "/".join(sorted(statuses)))
    if twostep:
        if not all(r["verify_ok"] == "True" for r in twostep):
            warnings.append("r_all twostep: not all reps verify OK, skipped -- "
                            f"{[r['verify_ok'] for r in twostep]}")
        else:
            defmacro("RosvallAllTwoStepTime",
                     fnum(statistics.median(float(r["seconds"]) for r in twostep)))
            defmacro("RosvallAllTwoStepStepOneTime",
                     fnum(statistics.median(float(r["step1_seconds"]) for r in twostep)))
            defmacro("RosvallAllTwoStepStepTwoTime",
                     fnum(statistics.median(float(r["step2_seconds"]) for r in twostep)))
            nprocs = {r["nprocs"] for r in twostep}
            hwcost = {r["hw_cost"] for r in twostep}
            if len(nprocs) == 1:
                defmacro("RosvallAllTwoStepNprocs", int(next(iter(nprocs))))
            else:
                warnings.append(f"r_all twostep: reps disagree on nprocs -- {nprocs}")
            if len(hwcost) == 1:
                defmacro("RosvallAllTwoStepHwCost", int(next(iter(hwcost))))
            else:
                warnings.append(f"r_all twostep: reps disagree on hw_cost -- {hwcost}")


# ---------------------------------------------------------------------------
# RQ2: e2_joint.csv
# ---------------------------------------------------------------------------
BASELINE_LABEL = {"SL": "SL", "PFmin": "PFMin", "PFmax": "PFMax", "NP": "NP",
                  "PFlight": "PFLight", "PFcheap": "PFCheap"}
E2_REL_FIELDS = ["instance", "baseline", "status", "total_cost",
                 "joint_total_cost", "rel_cost", "loss_pct"]
E2T_REL_FIELDS = ["factor"] + E2_REL_FIELDS
e2_rel: list[dict] = []


def _baseline_summary(rows: list[dict], prefix: str, rel: list[dict],
                      extra: dict | None = None) -> None:
    """rows: one instance set (all baselines). A baseline row counts only if
    it and its instance's joint row are VERIFY OK (an UNSAT baseline has
    nothing to verify and counts as infeasible, provided the joint row is
    verified). Every baseline is the joint model plus constraints, so a
    baseline below joint is reported."""
    joint = {r["instance"]: r for r in rows if r["baseline"] == "joint"}
    ok_joint = {}
    for name, r in joint.items():
        if r["status"] == "OPTIMAL" and r["verify_ok"] == "True":
            ok_joint[name] = int(r["total_cost"])
        else:
            warnings.append(f"{prefix} {name}: joint row not OPTIMAL+VERIFY OK "
                            f"({r['status']}, verify_ok={r['verify_ok']!r}); "
                            f"instance left out")
    if not ok_joint:
        return
    defmacro(f"{prefix}Instances", len(ok_joint))
    present = {r["baseline"] for r in rows}
    for b, label in BASELINE_LABEL.items():
        if b not in present:   # e.g. NP is not run on the input variants
            continue
        losses, infeasible, equal, worse = [], 0, 0, 0
        for r in rows:
            if r["baseline"] != b or r["instance"] not in ok_joint:
                continue
            j = ok_joint[r["instance"]]
            base = dict(extra or {}, instance=r["instance"], baseline=b,
                        status=r["status"], joint_total_cost=j)
            if r["status"] in ("UNSAT", "STEP1_UNSAT"):
                infeasible += 1
                rel.append(base)
                continue
            if r["verify_ok"] != "True" or r.get("fix_ok") == "False":
                warnings.append(f"{prefix} {r['instance']} {b}: not VERIFY OK "
                                f"(verify_ok={r['verify_ok']!r}, "
                                f"fix_ok={r.get('fix_ok')!r}), left out")
                continue
            if r["status"] != "OPTIMAL":
                warnings.append(f"{prefix} {r['instance']} {b}: status "
                                f"{r['status']} (not proven optimal), left out")
                continue
            c = int(r["total_cost"])
            if c < j:
                warnings.append(f"{prefix} {r['instance']} {b}: baseline {c} "
                                f"BELOW joint {j} -- impossible for a restriction")
            loss = 100.0 * (c - j) / j
            losses.append(loss)
            equal += c == j
            worse += c > j
            rel.append(dict(base, total_cost=c, rel_cost=f"{c / j:.4f}",
                            loss_pct=f"{loss:.2f}"))
        defmacro(f"{prefix}{label}Worse", worse)
        defmacro(f"{prefix}{label}Equal", equal)
        defmacro(f"{prefix}{label}Infeasible", infeasible)
        if losses:
            defmacro(f"{prefix}{label}MaxLoss", fnum(max(losses)))
            pos = [x for x in losses if x > 0]
            if pos:
                defmacro(f"{prefix}{label}MinPosLoss", fnum(min(pos)))


def rq2() -> None:
    _baseline_summary(read_csv("e2_joint.csv"), "Baseline", e2_rel)


# Throughput-bound variants: period_ub = factor x mu*, mu* = the joint
# instance's own minimum period (row baseline=min_period).
TIGHT_LABEL = {"1.00": "Hundred", "1.10": "HundredTen",
               "1.25": "HundredTwentyFive", "1.50": "HundredFifty",
               "2.00": "TwoHundred"}
e2t_rel: list[dict] = []


def rq2_tight() -> None:
    rows = read_csv("e2_tight.csv")
    for r in rows:
        if r["baseline"] == "min_period" and r["verify_ok"] != "True":
            warnings.append(f"e2t {r['instance']}: min_period not VERIFY OK")
    for fac, label in TIGHT_LABEL.items():
        sub = [r for r in rows if r["factor"] == fac]
        if sub:
            _baseline_summary(sub, f"Tight{label}", e2t_rel,
                              extra={"factor": fac})


# ---------------------------------------------------------------------------
# RQ2 robustness: per-flow least period (e2_thr.csv), input variants
# (e2_var.csv), joint repetitions (e2_reps.csv), cycle-bound certificates
# (e2_cert.csv, certify_pf.py)
# ---------------------------------------------------------------------------
VARIANT_LABEL = {"base": "Base", "tok2": "TokTwo", "chk10": "ChkTen",
                 "chk50": "ChkFifty"}
FLOW_LABEL = dict(BASELINE_LABEL, joint="Joint")
thr_rel: list[dict] = []
var_rel: list[dict] = []


def rq2_thr() -> None:
    rows = read_csv("e2_thr.csv")
    for var, vl in VARIANT_LABEL.items():
        sub = [r for r in rows if r["variant"] == var]
        if not sub:
            continue
        ok = lambda r: r["status"] == "OPTIMAL" and r["verify_ok"] == "True"
        joint = {r["instance"]: int(r["mu_max"]) for r in sub
                 if r["flow"] == "joint" and ok(r)}
        for r in sub:
            if r["flow"] == "joint" and not ok(r):
                warnings.append(f"thr {var} {r['instance']}: joint not "
                                f"OPTIMAL+VERIFY OK; instance left out")
        if not joint:
            continue
        defmacro(f"Thr{vl}Instances", len(joint))
        defmacro(f"Thr{vl}MuStarMin", min(joint.values()))
        defmacro(f"Thr{vl}MuStarMax", max(joint.values()))
        for fl, fll in FLOW_LABEL.items():
            if fl == "joint":
                continue
            ratios = []
            for r in sub:
                if r["flow"] != fl or r["instance"] not in joint:
                    continue
                if not ok(r):
                    warnings.append(f"thr {var} {r['instance']} {fl}: "
                                    f"{r['status']} verify_ok={r['verify_ok']}"
                                    f", left out")
                    continue
                q = int(r["mu_max"]) / joint[r["instance"]]
                if q < 1:
                    warnings.append(f"thr {var} {r['instance']} {fl}: period "
                                    f"below joint mu* -- impossible")
                ratios.append(q)
                thr_rel.append(dict(variant=var, instance=r["instance"],
                                    flow=fl, mu=r["mu_max"],
                                    mu_star=joint[r["instance"]],
                                    ratio=f"{q:.4f}"))
            if not ratios:
                continue
            above = [q for q in ratios if q > 1]
            defmacro(f"Thr{vl}{fll}AtMuStar", len(ratios) - len(above))
            defmacro(f"Thr{vl}{fll}Above", len(above))
            defmacro(f"Thr{vl}{fll}MaxRatio", fnum(max(ratios), 2))
            if above:
                defmacro(f"Thr{vl}{fll}MinAboveRatio", fnum(min(above), 2))


def rq2_var() -> None:
    rows = read_csv("e2_var.csv")
    for var, vl in VARIANT_LABEL.items():
        sub = [r for r in rows if r["variant"] == var]
        if not sub:
            continue
        for fac, fl in [("none", "NoBound")] + list(TIGHT_LABEL.items()):
            s2 = [r for r in sub if r["factor"] == fac]
            if s2:
                _baseline_summary(s2, f"Var{vl}{fl}", var_rel,
                                  extra={"variant": var, "factor": fac})


def rq2_reps() -> dict[str, float]:
    """Median run time of the joint rows over the repetitions; returns the
    no-bound medians per instance for Table III."""
    rows = read_csv("e2_reps.csv")
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["instance"], r["factor"]), []).append(r)
    med, spreads, nruns = {}, [], 0
    for (inst, fac), g in sorted(groups.items()):
        good = [r for r in g if r["status"] == "OPTIMAL"
                and r["verify_ok"] == "True"]
        nruns += len(g)
        if len(good) != len(g):
            warnings.append(f"reps {inst} {fac}: {len(g) - len(good)} of "
                            f"{len(g)} not OPTIMAL+VERIFY OK")
        if not good:
            continue
        costs = {r["total_cost"] for r in good}
        if len(costs) > 1:
            warnings.append(f"reps {inst} {fac}: optimum differs across "
                            f"repetitions {costs}")
        secs = sorted(float(r["seconds"]) for r in good)
        m = statistics.median(secs)
        spreads.append(100 * (secs[-1] - secs[0]) / m)
        if fac == "none":
            med[inst] = m
    if nruns:
        defmacro("RepsRuns", nruns)
        defmacro("RepsPerRow", max(len(g) for g in groups.values()))
        defmacro("RepsMaxSpread", round(max(spreads)))
        defmacro("RepsMedianSpread", round(statistics.median(spreads)))
    return med


def rq2_cert() -> None:
    rows = read_csv("e2_cert.csv")
    if not rows:
        return
    unsat = [r for r in rows if r["source"] != "e2_thr.csv"]
    defmacro("CertUnsatRows", len(unsat))
    defmacro("CertCertified", sum(r["certified"] == "True" for r in unsat))
    defmacro("CertCertifiedSil",
             sum(r["certified_sil"] == "True" for r in unsat))
    defmacro("CertCertifiedPlace",
             sum(r["certified_place"] == "True" for r in unsat))
    for r in unsat:
        if r["certified_place"] != "True":
            warnings.append(f"cert {r['source']} {r['variant']} "
                            f"{r['instance']} {r['factor']} {r['baseline']}: "
                            f"UNSAT not certified by the cycle bound "
                            f"(lb {r['lb_place_ceil']}, ub {r['period_ub']})")
    div = {r["lb_place_ceil"] for r in unsat if r["variant"] == "base"
           and int(r["lb_place_ceil"]) > int(r["lb_sil_ceil"])}
    if len(div) == 1:
        defmacro("CertTwoOfTwoDiverse", next(iter(div)))
        if r["patterns_match"] != "True":
            warnings.append(f"cert {r['instance']} {r['baseline']}: recomputed "
                            f"pattern choice differs from the CSV row")
    for actor, label in (("compJah", "Rasta"), ("get_pixel", "Sobel")):
        b = {r["crit_local_ceil"] for r in unsat
             if r["crit_actor"] == actor and r["variant"] == "base"
             and r["crit_pattern"] == "two_of_two_high_sil"}
        if len(b) == 1:
            defmacro(f"CertTwoOfTwo{label}", next(iter(b)))
    thr = [r for r in rows if r["source"] == "e2_thr.csv" and r["mu_flow"]]
    bad = [r for r in thr if r["lb_le_mu"] != "True"]
    for r in bad:
        warnings.append(f"cert thr {r['variant']} {r['instance']} "
                        f"{r['baseline']}: bound {r['lb_place_ceil']} ABOVE the "
                        f"flow's least period {r['mu_flow']}")
    above = [r for r in thr if int(r["lb_ceil"]) > 0
             and any(t["variant"] == r["variant"]
                     and t["instance"] == r["instance"]
                     and float(t["ratio"]) > 1 and t["flow"] == r["baseline"]
                     for t in thr_rel)]
    defmacro("CertThrAbove", len(above))
    defmacro("CertThrAboveExact",
             sum(int(r["lb_place_ceil"]) == int(r["mu_flow"]) for r in above))


def rq2_exact() -> None:
    """e2_exact.csv: every RQ2/RQ4 row re-run with the exact
    period variant B (exact_check.py). A row counts as confirmed when both
    runs are conclusive (OPTIMAL or UNSAT) with the same status and the same
    objective value; every differing row is printed as a WARNING."""
    rows = read_csv("e2_exact.csv")
    if not rows:
        return
    concl = [r for r in rows if r["agree"] in ("True", "False")]
    solved = [r for r in rows if r["status"] in ("OPTIMAL", "SAT")]
    bad = [r for r in solved if r["verify_ok"] != "True"]
    for r in bad:
        warnings.append(f"exact {r['source']} {r['variant']} {r['instance']} "
                        f"{r['factor']} {r['baseline']}: variant B solution "
                        f"not VERIFY OK")
    # exact_check.py --sl-diag: cause of each differing SL row
    key = ("source", "variant", "instance", "factor", "baseline")
    cause = {tuple(r[k] for k in key): r for r in read_csv("e2_exact_sl.csv")}
    differ = [r for r in concl if r["agree"] != "True"]
    for r in differ:
        c = cause.get(tuple(r[k] for k in key))
        warnings.append(f"exact {r['source']} {r['variant']} "
                        f"{r['instance']} {r['factor']} {r['baseline']}: "
                        f"model {r['old_status']} {r['old_value']}, "
                        f"variant B {r['status']} {r['value']}; cause: "
                        f"{c['cause'] if c else 'NOT DIAGNOSED'}")
    diag = [cause[t] for t in (tuple(r[k] for k in key) for r in differ)
            if t in cause]
    defmacro("ExactDiffer", len(differ))
    defmacro("ExactDifferSL", sum(r["baseline"] == "SL" for r in differ))
    defmacro("ExactDifferTie",
             sum(c["cause"].startswith("step-1 tie") for c in diag))
    # rows where the model's period pessimism explains the difference
    # (SL step 1 is canonical, so both runs fix the same binding)
    defmacro("ExactDifferPessimism",
             sum("period-pessimism" in c["cause"] for c in diag))
    defmacro("ExactDifferSameBinding",
             sum(c["same_binding"] == "True" for c in diag))
    unsat = [r for r in concl if r["old_status"].endswith("UNSAT")]
    sl_unsat = [r for r in unsat if r["baseline"] == "SL"]
    opt = [r for r in concl if r["old_status"] == "OPTIMAL"]
    defmacro("ExactRows", len(rows))
    defmacro("ExactConclusive", len(concl))
    defmacro("ExactAgree", sum(r["agree"] == "True" for r in concl))
    defmacro("ExactOptimalRows", len(opt))
    defmacro("ExactOptimalAgree", sum(r["agree"] == "True" for r in opt))
    defmacro("ExactUnsatRows", len(unsat))
    defmacro("ExactUnsatAgree", sum(r["agree"] == "True" for r in unsat))
    defmacro("ExactSLUnsatRows", len(sl_unsat))
    defmacro("ExactSLUnsatAgree", sum(r["agree"] == "True" for r in sl_unsat))
    defmacro("ExactVerified", len(solved) - len(bad))
    defmacro("ExactInconclusive", len(rows) - len(concl))
    # variant B hit the time limit: is its incumbent the model's optimum?
    inc = [r for r in rows if r["agree"] == "" and r["status"] == "SAT"]
    defmacro("ExactInconclusiveSameValue",
             sum(r["value"] == r["old_value"] for r in inc))
    defmacro("ExactInconclusiveBetter",
             sum(r["value"] != "" and r["old_value"] != ""
                 and int(r["value"]) < int(r["old_value"]) for r in inc))
    ratios = [float(r["seconds"]) / float(r["old_seconds"]) for r in concl
              if float(r["old_seconds"] or 0) > 0 and r["seconds"]]
    if ratios:
        defmacro("ExactTimeRatioMedian", fnum(statistics.median(ratios), 2))


# ---------------------------------------------------------------------------
# RQ3: e3_scal.csv (the sweep is resumable and may still be incomplete when
# this runs -- macros cover whatever sweep points already have rows)
# ---------------------------------------------------------------------------
ACTOR_LABEL = {"8": "Eight", "16": "Sixteen", "24": "TwentyFour",
               "32": "ThirtyTwo", "48": "FortyEight", "64": "SixtyFour"}
CAT_LABEL = {"1": "One", "3": "Three", "5": "Five", "9": "Nine"}


def _rq3_group(rows: list[dict], label: str, prefix: str) -> None:
    """rows: every repetition of one sweep point. Only a row proven OPTIMAL
    and VERIFY OK enters the timing macro (unverified results never become a
    number in the paper); a rep that times out (SAT/UNKNOWN) or fails
    verification is counted but left out of the median."""
    proven = [r for r in rows
              if r["status"] == "OPTIMAL" and r["verify_ok"] == "True"]
    defmacro(f"{prefix}{label}Reps", len(rows))
    defmacro(f"{prefix}{label}Proven", len(proven))
    defmacro(f"{prefix}{label}Timeout",
             sum(1 for r in rows if r["status"] in ("SAT", "UNKNOWN")))
    if proven:
        secs = [float(r["seconds"]) for r in proven]
        defmacro(f"{prefix}{label}Time", fnum(statistics.median(secs)))
        fv = [int(r["flat_vars"]) for r in proven if r.get("flat_vars")]
        if fv:
            defmacro(f"{prefix}{label}FlatVars", int(statistics.median(fv)))
    if len(proven) < len(rows):
        warnings.append(f"e3 {prefix}{label}: {len(rows) - len(proven)}/"
                        f"{len(rows)} rep(s) not OPTIMAL+VERIFY OK, left out "
                        f"of the timing macro (timeout or verify failure)")


def rq3() -> None:
    rows = read_csv("e3_scal.csv")
    if not rows:
        return
    by_actors: dict[str, list[dict]] = {}
    by_cat: dict[str, list[dict]] = {}
    nomu: dict[str, int] = {}
    for r in rows:
        if r["gen_status"] == "MU_UNKNOWN":
            key = ("Scal" + ACTOR_LABEL[r["param"]] if r["sweep"] == "actors"
                   else "ScalCat" + CAT_LABEL[r["param"]])
            nomu[key] = nomu.get(key, 0) + 1
        if r["gen_status"] != "READY":
            continue  # generation or build failure, nothing to time
        target = by_actors if r["sweep"] == "actors" else by_cat
        target.setdefault(r["param"], []).append(r)

    proven_actor_sizes = []
    for size, label in ACTOR_LABEL.items():
        if size in by_actors:
            _rq3_group(by_actors[size], label, "Scal")
            if all(r["status"] == "OPTIMAL" and r["verify_ok"] == "True"
                   for r in by_actors[size]):
                proven_actor_sizes.append(int(size))
    if proven_actor_sizes:
        defmacro("ScalMaxProvenActors", max(proven_actor_sizes))

    for size, label in CAT_LABEL.items():
        if size in by_cat:
            _rq3_group(by_cat[size], label, "ScalCat")
    for key in ["Scal" + v for v in ACTOR_LABEL.values()]:
        defmacro(f"{key}NoMu", nomu.get(key, 0))
    defmacro("ScalCatOneBuildFail", sum(
        1 for r in rows if r["sweep"] == "catalogue" and r["param"] == "1"
        and r["gen_status"] == "BUILD_FAIL"))


# ---------------------------------------------------------------------------
# RQ4: e4_sens.csv, e4_gsn.csv
# ---------------------------------------------------------------------------
def rq4() -> None:
    rows = read_csv("e4_sens.csv")
    for r in rows:
        if r["verify_ok"] != "True":
            warnings.append(f"{r['group']}/{r['instance']}: not verify OK "
                            f"(verify_ok={r['verify_ok']!r}, status={r['status']}), skipped")
            continue
        if r["group"] == "cost_profile":
            prof = r["instance"].removeprefix("x_")
            label = PROFILE_LABEL.get(prof, prof.title())
            defmacro(f"CostProfile{label}Nprocs", int(r["nprocs"]))
        elif r["group"] == "fault_model":
            fm = r["instance"].removeprefix("d_")
            label = FM_LABEL.get(fm, fm.title())
            defmacro(f"Placement{label}Nprocs", int(r["nprocs"]))
            defmacro(f"Placement{label}Mu", int(r["mu_max"]))
            defmacro(f"Placement{label}HwCost", int(r["hw_cost"]))
        elif r["group"] == "profile_consolidation":
            prof = r["instance"].removeprefix("x_")
            label = PROFILE_LABEL.get(prof, prof.title())
            n = {"3": "Three", "2": "Two", "1": "One"}[r["bound_value"]]
            defmacro(f"ProfCons{label}{n}Total", int(r["total_cost"]))
            defmacro(f"ProfCons{label}{n}Promotion", int(r["promotion_cost"]))
            defmacro(f"ProfCons{label}{n}Nprocs", int(r["nprocs"]))
        elif r["group"] == "consolidation":
            n = {"3": "Three", "2": "Two", "1": "One"}[r["bound_value"]]
            defmacro(f"Consolidation{n}Nprocs", int(r["nprocs"]))
            defmacro(f"Consolidation{n}Promotion", int(r["promotion_cost"]))
        elif r["group"] == "comm_model":
            defmacro("CommSobelMu", int(r["mu_max"]))
            defmacro("CommSobelBound", 400)
        elif r["group"] == "nvp":
            defmacro("NvpStatus", r["status"])
            defmacro("NvpTime", fnum(r["seconds"]))
            defmacro("NvpMu", int(r["mu_max"]))
            defmacro("NvpNprocs", int(r["nprocs"]))

    grows = read_csv("e4_gsn.csv")
    label_of = {"f_random_hw": "GsnRandomHw", "f_both3": "GsnBoth",
                "v_nvp": "GsnNvp"}
    for r in grows:
        if r["verify_ok"] != "True":
            warnings.append(f"gsn {r['label']}: not verify OK "
                            f"({r['verify_ok']!r}), skipped")
            continue
        label = label_of.get(r["label"], r["label"].title().replace("_", ""))
        defmacro(f"{label}Elements", int(r["elements"]))
        defmacro(f"{label}Goals", int(r["goals"]))
        defmacro(f"{label}Undeveloped", int(r["goals_undeveloped"]))
        defmacro(f"{label}Solutions", int(r["solutions"]))
        defmacro(f"{label}Checks", int(r["checks_total"]))
        defmacro(f"{label}Diverse", int(r["diverse_records"]))
        defmacro(f"{label}OutOfScope", int(r["out_of_scope"]))
        leaves = int(r["solutions"]) + int(r["goals_undeveloped"])
        defmacro(f"{label}Leaves", leaves)
        defmacro(f"{label}SolutionShare",
                 f"{round(100 * int(r['solutions']) / leaves)}")


# ---------------------------------------------------------------------------
# Breadth experiments (breadth.py). Only rows that
# are OPTIMAL (or UNSAT, where infeasibility is the result) and VERIFY OK
# become numbers.
# ---------------------------------------------------------------------------
PRICE_LABEL = dict(PROFILE_LABEL, klosterman_low="KlostermanLow")
NUM_LABEL = {"1": "One", "2": "Two", "3": "Three", "4": "Four", "5": "Five",
             "6": "Six", "8": "Eight", "16": "Sixteen"}
_ok = lambda r: r.get("status") == "OPTIMAL" and r.get("verify_ok") == "True"


def breadth_price() -> None:
    """e4_price.csv: per profile and price factor, the unbounded
    optimum and the one-core design; Fig. 7 data (extra cost of one core in
    percent of the optimum; 0 = consolidation is optimal)."""
    rows = read_csv("e4_price.csv")
    if not rows:
        return
    t: dict[tuple, dict] = {}
    for r in rows:
        if not _ok(r):
            warnings.append(f"e4_price {r['profile']} x{r['factor']} "
                            f"k={r['k']}: not OPTIMAL+VERIFY OK")
            continue
        t.setdefault((r["profile"], int(r["factor"])), {})[r["k"]] = r
    factors = sorted({f for _, f in t})
    fig = []
    for f in factors:
        rec = {"factor": f}
        for prof, lab in PRICE_LABEL.items():
            d = t.get((prof, f), {})
            if "none" not in d or "1" not in d:
                continue
            opt, one = int(d["none"]["total_cost"]), int(d["1"]["total_cost"])
            extra = 100 * (one - opt) / opt
            rec[prof] = f"{extra:.2f}"
            rec[prof + "_cores"] = d["none"]["nprocs"]
            fl = NUM_LABEL[str(f)]
            defmacro(f"Price{lab}{fl}Nprocs", int(d["none"]["nprocs"]))
            defmacro(f"Price{lab}{fl}Total", opt)
            defmacro(f"Price{lab}{fl}OneTotal", one)
            defmacro(f"Price{lab}{fl}OneExtra", round(extra))
            defmacro(f"Price{lab}{fl}OnePromotion",
                     int(d["1"]["promotion_cost"]))
        fig.append(rec)
    for prof, lab in PRICE_LABEL.items():
        one = [f for f in factors if (prof, f) in t and "none" in t[(prof, f)]
               and t[(prof, f)]["none"]["nprocs"] == "1"]
        if one:   # least price factor at which the optimum has one core
            defmacro(f"Price{lab}ConsolidateFactor", min(one))
        else:
            defmacro(f"Price{lab}ConsolidateFactor", "none")
    defmacro("PriceMaxFactor", max(factors))
    defmacro("PriceMinFactor", min(factors))
    fields = ["factor"] + [f"{p}{s}" for p in PRICE_LABEL
                           for s in ("", "_cores")]
    with (DATA / "fig7_price.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for rec in fig:
            w.writerow({c: rec.get(c, "") for c in fields})


COMM_INST = {"c_pat": "CPat", "pc_sobel": "PcSobel"}
COMM_VAR = {"tdma": "Tdma", "ideal": "Ideal", "sil_inherit": "SilInherit",
            "sil_core": "SilCore", "scope_all": "ScopeAll"}


def breadth_comm() -> None:
    """e4_comm.csv: cost optimum and least period per
    communication variant, and the change against the TDMA instance."""
    rows = read_csv("e4_comm.csv")
    val: dict[tuple, dict] = {}
    for r in rows:
        if not _ok(r):
            warnings.append(f"e4_comm {r['instance']} {r['variant']} "
                            f"{r['metric']}: not OPTIMAL+VERIFY OK")
            continue
        val[(r["instance"], r["variant"], r["metric"])] = r
    for inst, il in COMM_INST.items():
        base_c = val.get((inst, "tdma", "TOTALCOST"))
        base_m = val.get((inst, "tdma", "THROUGHPUT"))
        for var, vl in COMM_VAR.items():
            c = val.get((inst, var, "TOTALCOST"))
            m = val.get((inst, var, "THROUGHPUT"))
            if c:
                defmacro(f"Comm{il}{vl}Cost", int(c["total_cost"]))
                defmacro(f"Comm{il}{vl}Nodes", int(c["n"]))
                defmacro(f"Comm{il}{vl}Time", fnum(c["seconds"]))
                if base_c:
                    b = int(base_c["total_cost"])
                    defmacro(f"Comm{il}{vl}CostPct",
                             round(100 * (int(c["total_cost"]) - b) / b))
            if m:
                defmacro(f"Comm{il}{vl}Mu", int(m["mu_max"]))
                if base_m:
                    b = int(base_m["mu_max"])
                    defmacro(f"Comm{il}{vl}MuPct",
                             round(100 * (int(m["mu_max"]) - b) / b))


MULTI_FAC = {"none": "None", "1.00": "Hundred", "1.10": "HundredTen",
             "1.25": "HundredTwentyFive", "1.50": "HundredFifty",
             "2.00": "TwoHundred"}
MULTI_APP = {"a_sobel": "Sobel", "b_susan": "Susan", "c_rasta": "Rasta"}
FLOW_LABEL = {"joint": "Joint", "SL": "SL", "PFmin": "PFMin",
              "PFmax": "PFMax", "PFlight": "PFLight", "PFcheap": "PFCheap",
              "NP": "NP"}


def breadth_multi() -> dict[str, float]:
    """e5_multi.csv. Returns {m_3app: median joint time} for
    Table III."""
    rows = read_csv("e5_multi.csv")
    out = {}
    for r in rows:
        if r["baseline"] == "min_period_alone" and _ok(r):
            defmacro(f"MultiMuAlone{MULTI_APP[r['app']]}", int(r["mu_max"]))
    joint = {r["factor"]: r for r in rows
             if r["baseline"] == "joint" and r["rep"] == "1"}
    reps = [r for r in rows if r["baseline"] == "joint" and r["factor"] == "none"]
    if reps and all(_ok(r) for r in reps):
        costs = {r["total_cost"] for r in reps}
        if len(costs) != 1:
            warnings.append(f"m_3app joint reps disagree on cost: {costs}")
        out["m_3app"] = statistics.median(float(r["seconds"]) for r in reps)
        defmacro("MultiTime", fnum(out["m_3app"]))
        defmacro("MultiTimeMin", fnum(min(float(r["seconds"]) for r in reps)))
        defmacro("MultiTimeMax", fnum(max(float(r["seconds"]) for r in reps)))
    j = joint.get("none")
    if j and _ok(j):
        defmacro("MultiCost", int(j["total_cost"]))
        defmacro("MultiNprocs", int(j["nprocs"]))
        defmacro("MultiPromotion", int(j["promotion_cost"]))
    for fac, fl in MULTI_FAC.items():
        jr = joint.get(fac)
        for b, bl in FLOW_LABEL.items():
            r = next((x for x in rows if x["factor"] == fac and
                      x["baseline"] == b and x["rep"] == "1"), None)
            if r is None:
                continue
            if r["status"] in ("UNSAT", "STEP1_UNSAT"):
                defmacro(f"Multi{fl}{bl}", "infeasible")
                continue
            if not _ok(r):
                warnings.append(f"m_3app {fac} {b}: {r['status']} "
                                f"verify {r['verify_ok']}")
                continue
            if jr and _ok(jr) and b != "joint":
                jc = int(jr["total_cost"])
                defmacro(f"Multi{fl}{bl}Loss",
                         fnum(100 * (int(r["total_cost"]) - jc) / jc))
            defmacro(f"Multi{fl}{bl}Cost", int(r["total_cost"]))
    # the bounded rows again with exact variant B (e5_multi_exact.csv)
    xr = read_csv("e5_multi_exact.csv") if (RESULTS /
                                            "e5_multi_exact.csv").exists() else []
    if xr:
        concl = [r for r in xr if r["agree"] in ("True", "False")]
        defmacro("MultiExactRows", len(xr))
        defmacro("MultiExactConclusive", len(concl))
        defmacro("MultiExactAgree", sum(r["agree"] == "True" for r in concl))
        for r in xr:
            if r["agree"] == "False":
                warnings.append(f"m_3app exact {r['factor']} {r['baseline']}: "
                                f"model {r['old_status']} {r['old_total_cost']}"
                                f", variant B {r['status']} {r['total_cost']}")
            if r["status"] not in ("OPTIMAL", "UNSAT", "STEP1_UNSAT") or \
                    (r["status"] == "OPTIMAL" and r["verify_ok"] != "True"):
                warnings.append(f"m_3app exact {r['factor']} {r['baseline']}:"
                                f" {r['status']} verify {r['verify_ok']}")
        defmacro("MultiExactOpen", len(xr) - len(concl))
        defmacro("MultiExactBetter", sum(
            1 for r in xr if r["total_cost"] and r["old_total_cost"]
            and int(r["total_cost"]) < int(r["old_total_cost"])))
    # the open joint infeasibilities again with variant B at 3600 s
    for r in (read_csv("e5_multi_exact_long.csv") if (
            RESULTS / "e5_multi_exact_long.csv").exists() else []):
        defmacro(f"Multi{MULTI_FAC[r['factor']]}JointExactLong", r["status"])
        defmacro(f"Multi{MULTI_FAC[r['factor']]}JointExactLongTime",
                 fnum(r["seconds"]))
    return out


def breadth_patcons() -> None:
    """e4_patcons.csv: per instance, the core bounds at which NP costs
    more than joint (promotion used by the joint optimum)."""
    rows = read_csv("e4_patcons.csv")
    if not rows:
        return
    by: dict[tuple, dict] = {(r["instance"], r["k"], r["baseline"]): r
                             for r in rows}
    insts = sorted({r["instance"] for r in rows})
    promo_inst, np_inf, losses, pairs, inf_pairs = set(), set(), [], 0, 0
    promos = []
    for inst in insts:
        for k in sorted({r["k"] for r in rows if r["instance"] == inst}):
            j, n = by.get((inst, k, "joint")), by.get((inst, k, "NP"))
            if not j or not n or not _ok(j):
                continue
            pairs += 1
            promos.append(int(j["promotion_cost"]))
            if int(j["promotion_cost"]) > 0:
                promo_inst.add(inst)
            if n["status"] == "UNSAT":
                np_inf.add(inst)
                inf_pairs += 1
            elif _ok(n):
                jc = int(j["total_cost"])
                losses.append(100 * (int(n["total_cost"]) - jc) / jc)
            else:
                warnings.append(f"patcons {inst} k={k} NP: {n['status']}")
    defmacro("PatConsInstances", len(insts))
    defmacro("PatConsPairs", pairs)
    defmacro("PatConsPromoInstances", len(promo_inst))
    defmacro("PatConsNPInfeasibleInstances", len(np_inf))
    defmacro("PatConsNPInfeasiblePairs", inf_pairs)
    if promos:
        defmacro("PatConsPromoMin", min(promos))
        defmacro("PatConsPromoMax", max(promos))
    pos = [x for x in losses if x > 0]
    defmacro("PatConsNPWorse", len(pos))
    if pos:
        defmacro("PatConsNPMaxLoss", fnum(max(pos)))


def breadth_pipe() -> None:
    rows = [r for r in read_csv("e4_pipe.csv")
            if r.get("verify_ok") == "True" and r.get("gsn_rc") == "0"]
    if not rows:
        return
    for col, lab in [("build_s", "Build"), ("verify_s", "Verify"),
                     ("gsn_s", "Gsn"), ("pin_s", "Pin"), ("solve_s", "Solve")]:
        v = [float(r[col]) for r in rows if r.get(col)]
        defmacro(f"Pipe{lab}Max", fnum(max(v)))
        defmacro(f"Pipe{lab}Median", fnum(statistics.median(v)))
    defmacro("PipeInstances", len(rows))


def breadth_flat() -> dict[str, dict]:
    """e3_flat.csv: flattening of the RQ3 graphs (throughput stage,
    the largest model: no period bound) per actor count, and of the Table III
    instances (cost stage), returned for Table III."""
    rows = read_csv("e3_flat.csv")
    table = {r["tag"]: r for r in rows
             if r["set"] == "table" and r["status"] == "OK"}
    by: dict[str, list] = {}
    for r in rows:
        if r["set"] == "rq3" and r["stage"] == "THROUGHPUT" and \
                r["tag"].startswith("actorsweep") and r["status"] == "OK":
            by.setdefault(r["param"], []).append(r)
    for size, lab in ACTOR_LABEL.items():
        g = by.get(size)
        if not g:
            continue
        defmacro(f"Flat{lab}Time",
                 fnum(statistics.median(float(r["seconds"]) for r in g)))
        defmacro(f"Flat{lab}TimeMax", fnum(max(float(r["seconds"]) for r in g)))
        defmacro(f"Flat{lab}MB", round(statistics.median(
            int(r["fzn_bytes"]) for r in g) / 1e6))
        defmacro(f"Flat{lab}RssMB", round(max(float(r["max_rss_mb"])
                                              for r in g)))
        defmacro(f"Flat{lab}Vars", round(statistics.median(
            int(r["fzn_vars"]) for r in g) / 1000))
    bad = [r for r in rows if r["status"] != "OK"]
    for r in bad:
        warnings.append(f"e3_flat {r['set']} {r['tag']} {r['stage']}: "
                        f"{r['status']}")
    if table:
        v = [int(r["fzn_vars"]) for r in table.values()]
        defmacro("FlatTableVarsMin", round(min(v) / 1000))
        defmacro("FlatTableVarsMax", round(max(v) / 1000))
        s = [float(r["seconds"]) for r in table.values()]
        defmacro("FlatTableTimeMax", fnum(max(s)))
    return table


def rq3_extra() -> None:
    """Throughput solves proven per actor count, and the largest
    relative gap of a cost solve that reached the time limit."""
    rows = [r for r in read_csv("e3_scal.csv") if r["sweep"] == "actors"]
    for size, lab in ACTOR_LABEL.items():
        g = [r for r in rows if r["param"] == size]
        if not g:
            continue
        defmacro(f"Scal{lab}MuProven",
                 sum(1 for r in g if r.get("mu_star_proven") == "True"))
        defmacro(f"Scal{lab}MuFound", sum(1 for r in g if r.get("mu_star")))
    gaps = []
    for r in read_csv("e3_scal.csv"):
        if r["status"] == "SAT" and r.get("objective_bound") and \
                r.get("total_cost") and r["verify_ok"] == "True":
            tc, lb = float(r["total_cost"]), float(r["objective_bound"])
            gaps.append(100 * (tc - lb) / tc)
    if gaps:
        defmacro("ScalGapMin", fnum(min(gaps)))
        defmacro("ScalGapMax", fnum(max(gaps)))
        defmacro("ScalGapCount", len(gaps))


def breadth_pattern_ties() -> None:
    """The joint optimum of an instance was solved twice (e2_joint.csv
    with the model, e2_exact.csv with variant B, same cost); count the
    instances whose two optima select different patterns."""
    j = {r["instance"]: r for r in read_csv("e2_joint.csv")
         if r["baseline"] == "joint" and _ok(r)}
    ties = []
    for r in read_csv("e2_exact.csv"):
        if r["source"] == "e2_joint" and r["baseline"] == "joint" and \
                r["instance"] in j and r["verify_ok"] == "True" and \
                r["total_cost"] == j[r["instance"]]["total_cost"] and \
                r["patterns"] != j[r["instance"]]["patterns"]:
            ties.append(r["instance"])
    defmacro("PatternTieInstances", len(ties))


XSOLVER_LABEL = {"gecode": "Gecode", "chuffed": "Chuffed", "highs": "Highs",
                 "cp-sat-1": "CpSatOne"}


def breadth_xsolver() -> None:
    """e4_xsolver.csv: per back end, instances proven optimal, with a
    verified solution, and with the CP-SAT optimum (e2_joint / e5_multi)."""
    rows = read_csv("e4_xsolver.csv")
    if not rows:
        return
    ref = {r["instance"]: r["total_cost"] for r in read_csv("e2_joint.csv")
           if r["baseline"] == "joint" and _ok(r)}
    for r in read_csv("e5_multi.csv"):
        if (r["factor"], r["baseline"], r["rep"]) == ("none", "joint", "1") \
                and _ok(r):
            ref["m_3app"] = r["total_cost"]
    defmacro("XsolverInstances", len({r["instance"] for r in rows}))
    for s, lab in XSOLVER_LABEL.items():
        g = [r for r in rows if r["solver"] == s]
        if not g:
            continue
        sol = [r for r in g if r.get("verify_ok") == "True"]
        opt = [r for r in sol if r["status"] == "OPTIMAL"]
        agree = [r for r in opt if r["total_cost"] == ref.get(r["instance"])]
        defmacro(f"Xsolver{lab}Solved", len(sol))
        defmacro(f"Xsolver{lab}Proven", len(opt))
        defmacro(f"Xsolver{lab}Agree", len(agree))
        defmacro(f"Xsolver{lab}Errors", sum(r["status"] == "ERROR" for r in g))
        for r in opt:
            if r["total_cost"] != ref.get(r["instance"]):
                warnings.append(f"xsolver {s} {r['instance']}: OPTIMAL "
                                f"{r['total_cost']} vs CP-SAT "
                                f"{ref.get(r['instance'])}")
        for r in g:
            if r.get("verify_ok") == "False":
                warnings.append(f"xsolver {s} {r['instance']}: solution "
                                f"fails the verifier")


# ---------------------------------------------------------------------------
# RQ1 head-to-head with DeSyDe (tag v0.2.1-todaes) on the TODAES
# Experiment 3/4 inputs; e7_safedse.csv, e7_desyde.csv (desyde_cmp.py).
# A SafeDSE result counts only if it verifies; DeSyDe's are its own output.
# ---------------------------------------------------------------------------
DSD_LABEL = {"exp_3_1_rajp": "ThreeRaJp", "exp_3_3_sosujp": "ThreeSoSuJp",
             "exp_3_4_sorajp": "ThreeSoRaJp", "exp_3_5_surajp": "ThreeSuRaJp",
             "exp_3_6_sosurajp": "ThreeSoSuRaJp", "exp_4_1_rajp": "FourRaJp",
             "exp_4_3_sorajp": "FourSoRaJp"}


def desyde_macros() -> None:
    import desyde_cmp as C
    srows = read_csv("e7_safedse.csv")
    drows = read_csv("e7_desyde.csv")
    if not srows:
        return
    defmacro("DsdScenarios", len(C.SCENARIOS))
    defmacro("DsdSkipped", sum(r["status"] == "skipped" for r in srows
                               if r["rep"] == "0"))
    defmacro("DsdThreads", C.THREADS)
    defmacro("DsdSafeTimeLimit", C.SAFEDSE_TIME_LIMIT_MS // 1000)
    comp = [r for r in srows if r["status"] != "skipped"]
    defmacro("DsdComparable", len({r["scenario"] for r in comp}))
    proven = agree = beyond = 0
    for scen, lab in DSD_LABEL.items():
        g = [r for r in comp if r["scenario"] == scen]
        if not g:
            continue
        if not all(r["verify_ok"] == "True" for r in g):
            warnings.append(f"e7 {scen}: SafeDSE solution fails the verifier")
            continue
        pers = {r["target_period"] for r in g}
        if len(pers) > 1:
            warnings.append(f"e7 {scen}: SafeDSE reps disagree {pers}")
            continue
        per = int(next(iter(pers)))
        opt = all(r["status"] == "OPTIMAL" for r in g)
        proven += opt
        defmacro(f"Dsd{lab}SafePeriod", per)
        defmacro(f"Dsd{lab}SafeTime",
                 fnum(statistics.median(float(r["seconds"]) for r in g), 0))
        defmacro(f"Dsd{lab}PubPeriod", C.PUBLISHED[scen][0])
        defmacro(f"Dsd{lab}PubTime", fnum(C.PUBLISHED_SECONDS[scen], 0))
        pub_per, pub_proven = C.PUBLISHED[scen]
        if opt and per == pub_per:
            agree += pub_proven
            beyond += not pub_proven
        if per != pub_per:
            warnings.append(f"e7 {scen}: SafeDSE {per} vs published {pub_per}")
        # exit 0 (finished) or "killed" (stopped at the wall cap: best
        # period so far, not proven); anything else is a crash
        dg = [r for r in drows if r["scenario"] == scen
              and r["exit"] in ("0", "killed") and r["target_period"]]
        if dg:
            dp = {r["target_period"] for r in dg}
            if len(dp) == 1:
                defmacro(f"Dsd{lab}DesydePeriod", next(iter(dp)))
            defmacro(f"Dsd{lab}DesydeTime", fnum(statistics.median(
                float(r["wall_seconds"]) for r in dg), 0))
            defmacro(f"Dsd{lab}DesydeProven",
                     "yes" if all(r["proven"] == "True" for r in dg) else "no")
            if dp != {str(per)}:
                warnings.append(f"e7 {scen}: DeSyDe rerun {dp} vs SafeDSE {per}")
    defmacro("DsdSafeProven", proven)
    defmacro("DsdPubProven", sum(C.PUBLISHED[s][1] for s in DSD_LABEL))
    # repetitions: the fast scenarios ran 5 times, the 1 h ones once
    defmacro("DsdMaxReps", max(sum(r["scenario"] == s for r in comp)
                               for s in DSD_LABEL))
    defmacro("DsdAgreeProven", agree)
    defmacro("DsdBeyondPublished", beyond)
    defmacro("DsdDesydeRuns", len({r["scenario"] for r in drows}))
    defmacro("DsdDesydeProven", len({r["scenario"] for r in drows
                                     if r["proven"] == "True"}))


# ---------------------------------------------------------------------------
# Table III: the pattern instances of RQ2/RQ4 with the joint optimum.
# Instance facts (application, platform, fault model) are the build inputs in
# safedse/tools/build_all.sh; node counts are read from the built .dzn; every
# measured number comes from e2_joint.csv / e2_tight.csv, and only a row that
# is OPTIMAL and VERIFY OK is printed (otherwise "--").
# ---------------------------------------------------------------------------
TAB3 = [  # (instance, footnote mark, platform, fault model); RASTA unless noted
    # platform: nT = n core types (short labels leave room for the Vars column)
    ("p_rasta", "", "2T, part.", "H"),
    ("f_random_hw", "", "2T", "H"),
    ("d_random_hw", "a", "2T", "H"),
    ("d_systematic_sw", "a", "2T", "S"),
    ("f_sw3", "", "3T", "S"),
    ("f_both3", "", "3T", "H+S"),
    ("c_pat", "", "2T, TDMA", "H"),
    ("pc_sobel", "b", "2T, TDMA", "H"),   # Rosvall's platform (note b)
    ("v_nvp", "c", "4T", "H+S"),
    ("m_3app", "d", "2T", "H"),   # three applications (breadth.py multi)
]


# ---------------------------------------------------------------------------
# Mutation analysis of the verifier and the generator (mutation.py)
# ---------------------------------------------------------------------------
# Art: SafeDSE's verifier before the extension (verifier_v1/, SafeDSE
# b830db7); Ext: the extension (verifier_ext/); Cur: SafeDSE's current
# verifier, which contains the extension (1c23d7c onward)
MUT_VERIFIER = {"safedse": "Art", "ext": "Ext", "artifact": "Cur"}


def _camel(s: str) -> str:
    return "".join(w.capitalize() for w in s.split("_"))


def mutation_macros() -> None:
    rows = read_csv("e6_mut.csv")
    if not rows:
        return
    # ground truth: the exact variant B (e6_oracle.csv; the model alone can
    # reject a correct deployment); the model's own verdict is kept
    ex = {(o["part"], o["instance"], o["operator"], o["mutant"]):
          o["exact_status"] for o in read_csv("e6_oracle.csv")}
    for r in rows:
        if r["part"] in ("solution", "model") and r["model_status"] not in (
                "", "N/A"):
            st = ex.get((r["part"], r["instance"], r["operator"], r["mutant"]))
            if st is None:
                warnings.append(f"e6: no exact-oracle verdict for {r['part']} "
                                f"{r['instance']} {r['operator']} {r['mutant']}")
                r["manifested"] = ""
            else:
                r["manifested"] = str(st in ("UNSAT", "COSTS"))
            r["pessimistic"] = (r["model_status"] in ("UNSAT", "COSTS")
                                and st == "OPTIMAL")
    sol = [r for r in rows if r["part"] == "solution"]
    mod = [r for r in rows if r["part"] == "model"]
    gen = [r for r in rows if r["part"] == "generator"
           and r["verifier"] == "safedse"]
    genc = [r for r in rows if r["part"] == "generator"
            and r["verifier"] == "artifact"]
    man = lambda r: r["manifested"] in ("True", "by construction")  # noqa: E731

    # ---- corrupted solutions ----------------------------------------------
    art = [r for r in sol if r["verifier"] == "safedse"]
    defmacro("MutSolMutants", len(art))
    defmacro("MutSolOperators", len({r["operator"] for r in art}))
    defmacro("MutSolInstances", len({r["instance"] for r in art}))
    defmacro("MutSolManifested", sum(man(r) for r in art))
    defmacro("MutSolEquivalent", sum(r["manifested"] == "False" for r in art))
    defmacro("MutSolModelOnly", sum(r.get("pessimistic") is True for r in art))
    open_ = [r for r in art if r["manifested"] == ""]
    if open_:
        warnings.append(f"e6: {len(open_)} solution mutants without a model "
                        f"verdict (model check timed out)")
    for v, lab in MUT_VERIFIER.items():
        g = [r for r in sol if r["verifier"] == v]
        m = [r for r in g if man(r)]
        rej = [r for r in m if r["verify_ok"] == "False"]
        defmacro(f"MutSolRejected{lab}", len(rej))
        defmacro(f"MutSolMissed{lab}", len(m) - len(rej))
        defmacro(f"MutSolFalseAlarm{lab}",
                 sum(r["manifested"] == "False" and r["verify_ok"] == "False"
                     for r in g))
        miss_ops = sorted({r["operator"] for r in m if r["verify_ok"] == "True"})
        defmacro(f"MutSolMissedOperators{lab}", len(miss_ops))
        hit = [r for r in rej if r["intended_hit"] != ""]
        defmacro(f"MutSolIntendedHit{lab}", sum(r["intended_hit"] == "True"
                                                for r in hit))
        defmacro(f"MutSolIntendedChecked{lab}", len(hit))
        for op in sorted({r["operator"] for r in g}):
            o = [r for r in m if r["operator"] == op]
            defmacro(f"MutOp{_camel(op)}Manifested{lab}", len(o))
            defmacro(f"MutOp{_camel(op)}Rejected{lab}",
                     sum(r["verify_ok"] == "False" for r in o))
        # the generator must refuse whenever the verifier rejected (exit 2)
        rj = [r for r in g if r["verify_ok"] == "False"]
        defmacro(f"MutGsnRejected{lab}", len(rj))
        defmacro(f"MutGsnRefused{lab}", sum(r["gsn_exit"] == "2" for r in rj))
        acc = [r for r in m if r["verify_ok"] == "True"]
        defmacro(f"MutGsnEmittedFaulty{lab}", sum(r["gsn_exit"] == "0"
                                                  for r in acc))
        if any(r["gsn_exit"] != "2" for r in rj):
            warnings.append(f"e6 {v}: the generator did not refuse a "
                            f"verifier-rejected solution")

    # ---- model mutants ----------------------------------------------------
    am = [r for r in mod if r["verifier"] == "safedse"]
    if am:
        defmacro("MutModelMutants", len({r["operator"] for r in am}))
        defmacro("MutModelRuns", len(am))
        solved = [r for r in am if r["model_status"]]
        defmacro("MutModelNoSolution", len(am) - len(solved))
        defmacro("MutModelManifested", sum(man(r) for r in solved))
        defmacro("MutModelEquivalent",
                 sum(r["manifested"] == "False" for r in solved))
        # rejected by the model only through its period pessimism, accepted
        # by variant B
        defmacro("MutModelModelOnly",
                 sum(r.get("pessimistic") is True for r in solved))
        defmacro("MutModelFamiliesManifested",
                 len({r["operator"] for r in solved if man(r)}))
        for v, lab in MUT_VERIFIER.items():
            g = [r for r in mod if r["verifier"] == v and man(r)]
            rej = [r for r in g if r["verify_ok"] == "False"]
            defmacro(f"MutModelRejected{lab}", len(rej))
            defmacro(f"MutModelMissed{lab}", len(g) - len(rej))
            fam = {r["operator"] for r in g}
            fam_miss = {r["operator"] for r in g if r["verify_ok"] == "True"}
            defmacro(f"MutModelFamiliesMissed{lab}", len(fam_miss))
            defmacro(f"MutModelFamiliesKilled{lab}", len(fam - fam_miss))
            defmacro(f"MutModelFalseAlarm{lab}",
                     sum(r["verifier"] == v and r["manifested"] == "False"
                         and r["verify_ok"] == "False" for r in mod))

    # ---- the generator's own refusals and the argument audit ----------------
    if gen:
        c = [r for r in gen if r["operator"] == "control"]
        defmacro("MutGenControls", len(c))
        defmacro("MutGenControlsEmitted", sum(r["gsn_exit"] == "0" for r in c))
        amb = [r for r in gen if r["operator"] == "fault_model" and r["note"]]
        defmacro("MutGenFaultModelAmbiguous", len(amb))   # nothing to contradict
        for op, lab in (("catalog_mismatch", "Catalog"),
                        ("fault_model", "FaultModel")):
            g = [r for r in gen if r["operator"] == op and not r["note"]]
            defmacro(f"MutGen{lab}Cases", len(g))
            defmacro(f"MutGen{lab}Refused", sum(r["intended_hit"] == "True"
                                                for r in g))
        a = [r for r in gen if r["operator"].startswith("arg_")]
        defmacro("MutArgMutants", len(a))
        defmacro("MutArgOperators", len({r["operator"] for r in a}))
        defmacro("MutArgCaughtAudit", sum(r["intended_hit"] == "True" for r in a))
        defmacro("MutArgCaughtExt", sum(r["ext_hit"] == "True" for r in a))
        bi = [r for r in a if r["operator"] == "arg_bad_index"]
        defmacro("MutArgBadIndex", len(bi))
        defmacro("MutArgBadIndexCaughtAudit",
                 sum(r["intended_hit"] == "True" for r in bi))

    # ---- the current verifier: same verdicts as the extension? -------------
    if genc:
        c = [r for r in genc if r["operator"] == "control"]
        defmacro("MutGenControlsEmittedCur", sum(r["gsn_exit"] == "0"
                                                 for r in c))
        for op, lab in (("catalog_mismatch", "Catalog"),
                        ("fault_model", "FaultModel")):
            g = [r for r in genc if r["operator"] == op and not r["note"]]
            defmacro(f"MutGen{lab}RefusedCur", sum(r["intended_hit"] == "True"
                                                   for r in g))
        a = [r for r in genc if r["operator"].startswith("arg_")]
        defmacro("MutArgCaughtCur", sum(r["intended_hit"] == "True" for r in a))
    vk = {}
    for r in rows:
        if r["part"] in ("solution", "model"):
            vk.setdefault((r["part"], r["instance"], r["operator"],
                           r["mutant"]), {})[r["verifier"]] = r["verify_ok"]
    both = [x for x in vk.values() if "ext" in x and "artifact" in x]
    if both:
        defmacro("MutCurCompared", len(both))
        defmacro("MutCurAgreesExt", sum(x["ext"] == x["artifact"]
                                        for x in both))
        if any(x["ext"] != x["artifact"] for x in both):
            warnings.append("e6: the current verifier and the extension "
                            "disagree on some mutant")

    # ---- re-check of stored verified solutions with the extension ----------
    rc = read_csv("e6_recheck.csv")
    if rc:
        ok = [r for r in rc if r["safedse_ok"] == "True"]
        defmacro("MutRecheckSolutions", len(ok))
        defmacro("MutRecheckExtAccepted", sum(r["ext_ok"] == "True" for r in ok))
        defmacro("MutRecheckInstances", len({r["dzn"] for r in ok}))
        for r in ok:
            if r["ext_ok"] != "True":
                warnings.append(f"e6 recheck: extension rejects {r['solution']}"
                                f": {r['ext_messages'][:120]}")


# ---------------------------------------------------------------------------
# model extensions, cost-benefit (e8_modelext.csv, modelext.py)
# ---------------------------------------------------------------------------
MODELEXT_CFG = {"E1": "Doer", "E2": "Diverse", "E12": "Both", "E3k1": "TokOne",
           "E3k15": "TokOneHalf", "E3k2": "TokTwo"}
MODELEXT_FAC = dict(TIGHT_LABEL, none="None")


def modelext_macros() -> None:
    rows = read_csv("e8_modelext.csv")
    if not rows:
        return
    base, tref, tok2 = {}, {}, {}
    for src in ("e2_joint.csv", "e2_tight.csv"):
        for r in read_csv(src):
            base[(r["instance"], r.get("factor", "none"), r["baseline"])] = r
    reps: dict[tuple, list[float]] = {}
    for r in read_csv("e2_reps.csv"):
        if r["status"] == "OPTIMAL":
            reps.setdefault((r["instance"], r["factor"]), []).append(
                float(r["seconds"]))
    tref = {k: statistics.median(v) for k, v in reps.items()}
    for r in read_csv("e2_var.csv"):
        if r["variant"] == "tok2" and r["baseline"] == "joint":
            tok2[(r["instance"], r["factor"])] = r
    ok = lambda r: r["status"] == "OPTIMAL" and r["verify_ok"] == "True"  # noqa: E731
    for r in rows:
        if r["status"] == "OPTIMAL" and r["verify_ok"] != "True":
            warnings.append(f"e8 {r['config']} {r['instance']} {r['factor']} "
                            f"{r['baseline']}: OPTIMAL but not VERIFY OK")
        elif r["status"] not in ("OPTIMAL", "UNSAT"):
            warnings.append(f"e8 {r['config']} {r['instance']} {r['factor']} "
                            f"{r['baseline']} {r['rep']}: {r['status']}")
    defmacro("ModExtRows", len(rows))
    defmacro("ModExtVerified", sum(ok(r) for r in rows))
    defmacro("ModExtUnsat", sum(r["status"] == "UNSAT" for r in rows))
    for cfg, lab in MODELEXT_CFG.items():
        cr = [r for r in rows if r["config"] == cfg]
        if not cr:
            continue
        joint = [r for r in cr if r["baseline"] == "joint" and r["rep"] == "1"]
        defmacro(f"ModExt{lab}JointRows", len(joint))
        defmacro(f"ModExt{lab}Instances", len({r["instance"] for r in joint}))
        cmp_ = []
        for r in joint:
            b = base.get((r["instance"], r["factor"], "joint"))
            if b and ok(r) and b["status"] == "OPTIMAL":
                cmp_.append((int(r["total_cost"]), int(b["total_cost"]), r, b))
        defmacro(f"ModExt{lab}Cheaper", sum(n < o for n, o, _, _ in cmp_))
        defmacro(f"ModExt{lab}Dearer", sum(n > o for n, o, _, _ in cmp_))
        defmacro(f"ModExt{lab}Equal", sum(n == o for n, o, _, _ in cmp_))
        sav = [100 * (o - n) / o for n, o, _, _ in cmp_ if n < o]
        ext = [100 * (n - o) / o for n, o, _, _ in cmp_ if n > o]
        if sav:
            defmacro(f"ModExt{lab}MaxSavingPct", fnum(max(sav)))
            defmacro(f"ModExt{lab}MinSavingPct", fnum(min(sav)))
        if ext:
            defmacro(f"ModExt{lab}MaxExtraPct", fnum(max(ext)))
            defmacro(f"ModExt{lab}MinExtraPct", fnum(min(ext)))
        defmacro(f"ModExt{lab}PatternsChanged",
                 sum(r["patterns"] != b["patterns"] for _, _, r, b in cmp_))
        defmacro(f"ModExt{lab}DoerRows",
                 sum(int(r["doer_lowered"] or 0) > 0 for r in joint))
        defmacro(f"ModExt{lab}TokRows", sum(int(r["r2"] or 0) > 0
                                            for r in joint))
        defmacro(f"ModExt{lab}Unsat", sum(r["status"] == "UNSAT"
                                          for r in joint))
        # solve time: median over this configuration's repetitions against
        # the median of the 5 stored joint runs (e2_reps)
        tt: dict[tuple, list[float]] = {}
        for r in cr:
            if r["baseline"] == "joint" and r["status"] in ("OPTIMAL",
                                                             "UNSAT"):
                tt.setdefault((r["instance"], r["factor"]), []).append(
                    float(r["seconds"]))
        ratio = [statistics.median(v) / tref[k] for k, v in tt.items()
                 if k in tref and tref[k] > 0]
        if ratio:
            defmacro(f"ModExt{lab}TimeRatioMed", fnum(statistics.median(ratio), 2))
            defmacro(f"ModExt{lab}TimeRatioMax", fnum(max(ratio), 2))
        if cfg == "E3k2":       # must reproduce the two-token catalog variant
            # tok2 factors are relative to the variant's own mu* (pc_sobel:
            # 256, not 333): compare only rows with the same bound
            m = [(r, tok2[(r["instance"], r["factor"])]) for r in joint
                 if (r["instance"], r["factor"]) in tok2
                 and tok2[(r["instance"], r["factor"])]["period_ub"]
                 == r["period_ub"]]
            defmacro("ModExtTokTwoVariantCompared", len(m))
            defmacro("ModExtTokTwoVariantEqual",
                     sum(r["total_cost"] == v["total_cost"]
                         and r["status"] == v["status"] for r, v in m))
            rv = [float(r["seconds"]) / float(v["seconds"]) for r, v in m
                  if float(v["seconds"]) > 0]
            if rv:
                defmacro("ModExtTokTwoVariantTimeRatioMed",
                         fnum(statistics.median(rv), 2))
            for r, v in m:
                if r["total_cost"] != v["total_cost"]:
                    warnings.append(f"e8 E3k2 {r['instance']} {r['factor']}: "
                                    f"{r['total_cost']} vs tok2 "
                                    f"{v['total_cost']}")
        # flows: infeasibility per bound and cost loss against this
        # configuration's joint optimum, and against the base study
        jv = {(r["instance"], r["factor"]): r for r in joint}
        for fl in sorted({r["baseline"] for r in cr} - {"joint"}):
            fr = [r for r in cr if r["baseline"] == fl]
            fl_lab = FLOW_LABEL[fl]
            for fac in sorted({r["factor"] for r in fr}):
                g = [r for r in fr if r["factor"] == fac]
                defmacro(f"ModExt{lab}{fl_lab}{MODELEXT_FAC[fac]}Infeasible",
                         sum(r["status"] == "UNSAT" for r in g))
                defmacro(f"ModExt{lab}{fl_lab}{MODELEXT_FAC[fac]}Rows", len(g))
            loss = [100 * (int(r["total_cost"]) - int(j["total_cost"]))
                    / int(j["total_cost"]) for r in fr if ok(r)
                    for j in [jv.get((r["instance"], r["factor"]))]
                    if j and ok(j)]
            if loss:
                defmacro(f"ModExt{lab}{fl_lab}MaxLossPct", fnum(max(loss)))
                defmacro(f"ModExt{lab}{fl_lab}Worse",
                         sum(x > 0 for x in loss))
            if any(x < 0 for x in loss):
                warnings.append(f"e8 {cfg} {fl}: a flow beats joint")
            gained = lost = 0
            for r in fr:
                b = base.get((r["instance"], r["factor"], fl))
                if not b:
                    continue
                gained += b["status"] == "UNSAT" and ok(r)
                lost += b["status"] == "OPTIMAL" and r["status"] == "UNSAT"
            defmacro(f"ModExt{lab}{fl_lab}FeasibleGained", gained)
            defmacro(f"ModExt{lab}{fl_lab}FeasibleLost", lost)


def _dzn_int(path: Path, name: str) -> int | None:
    import re
    m = re.search(rf"^{name}\s*=\s*(\d+);", path.read_text(), re.M)
    return int(m.group(1)) if m else None


def table_results(rep_median: dict[str, float] | None = None,
                  flat: dict[str, dict] | None = None) -> None:
    """t (s): median over the repetitions of e2_reps.csv where they
    exist, else the single run of e2_joint.csv. Column Vars: FlatZinc
    variables of the cost model, thousands (e3_flat.csv). Row m_3app: the
    three-application instance (e5_multi.csv), which has one
    period per application, so no single mu*."""
    rep_median = rep_median or {}
    flat = flat or {}
    joint = {r["instance"]: r for r in read_csv("e2_joint.csv")
             if r["baseline"] == "joint"}
    for r in read_csv("e5_multi.csv"):
        if (r["factor"], r["baseline"], r["rep"]) == ("none", "joint", "1"):
            joint["m_3app"] = r
    mustar = {r["instance"]: r for r in read_csv("e2_tight.csv")
              if r["baseline"] == "min_period"}
    lines = ["% GENERATED by make_macros.py (SafeDSE-experiments) from e2_joint.csv, "
             "e2_tight.csv, e2_reps.csv, e5_multi.csv, e3_flat.csv and the "
             "built .dzn files -- do not edit.",
             r"\setlength{\tabcolsep}{1.5pt}",
             r"\begin{tabular*}{\columnwidth}{@{\extracolsep{\fill}}l l c r r r c r r@{}}",
             r"\toprule",
             r"Instance & Platform & FM & $|\Gplus|$ & Vars & Cost & Cores "
             r"& $\mu^{*}$ & $t$ (s) \\",
             r"\midrule"]
    ok = lambda r: r and r["status"] == "OPTIMAL" and r["verify_ok"] == "True"
    for inst, mark, plat, fm in TAB3:
        dzn = SAFEDSE_OUT / f"{inst}.dzn"
        if inst == "m_3app":
            dzn = ROOT / "multi" / "m_3app.dzn"   # breadth.py multi
        n = _dzn_int(dzn, "n") if dzn.exists() else None
        j, m = joint.get(inst), mustar.get(inst)
        cost = j["total_cost"] if ok(j) else "--"
        cores = j["nprocs"] if ok(j) else "--"
        secs = (fnum(rep_median.get(inst, j["seconds"])) if ok(j)
                else "--")
        mu = m["mu_max"] if ok(m) else "--"
        fv = flat.get(inst)
        fv = f"{round(int(fv['fzn_vars']) / 1000)}k" if fv else "--"
        if not ok(j):
            warnings.append(f"table III {inst}: joint row not OPTIMAL+VERIFY OK")
        if not ok(m) and inst != "m_3app":
            warnings.append(f"table III {inst}: min_period row not OPTIMAL+VERIFY OK")
        if n is None:
            warnings.append(f"table III {inst}: no built .dzn for |G+|")
        code = inst.replace("_", r"\_")
        sup = f"$^{{{mark}}}$" if mark else ""
        lines.append(f"\\code{{{code}}}{sup} & {plat} & {fm} & {n or '--'} & "
                     f"{fv} & {cost} & {cores} & {mu} & {secs} \\\\")
    lines += [r"\bottomrule", r"\end{tabular*}"]
    TABLES.mkdir(parents=True, exist_ok=True)
    (TABLES / "tab-results.tex").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Plot data. pgfplots reads these files; no number is typed into a plot.
# ---------------------------------------------------------------------------
FIG5_FLOWS = ["SL", "PFmin", "PFmax", "PFlight", "PFcheap"]
FIG5_BOUNDS = ["1.00", "1.10", "1.25", "1.50", "2.00", "none"]


def fig5_counts() -> None:
    """Fig. 5: per period bound (factor of mu*, then no bound) and
    flow, the number of instances without a feasible design and the largest
    cost loss against joint among the feasible ones. One row per bound, one
    column pair per flow; x is the bound's position. NP is not plotted
    (equal to joint everywhere; stated by macro)."""
    rows = []
    for xi, fac in enumerate(FIG5_BOUNDS):
        src = e2_rel if fac == "none" else [r for r in e2t_rel
                                            if r.get("factor") == fac]
        row = {"x": xi, "factor": fac}
        for b in FIG5_FLOWS:
            sub = [r for r in src if r["baseline"] == b]
            if not sub:
                warnings.append(f"fig5: no rows for {b} at {fac}")
            inf = sum(1 for r in sub if r["status"] in ("UNSAT", "STEP1_UNSAT"))
            loss = [float(r["loss_pct"]) for r in sub
                    if r.get("loss_pct") not in (None, "")]
            row[f"inf_{b}"] = inf
            row[f"loss_{b}"] = f"{max(loss):.2f}" if loss else "nan"
            row[f"worse_{b}"] = sum(1 for x in loss if x > 0)
        rows.append(row)
    with (DATA / "fig5_sweep.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def fig6_points() -> None:
    """Fig. 6: one point per RQ3 repetition. Proven optimal and verified:
    solve time. Final solve timed out: plotted at its time with an open
    marker. No mu* within its budget (no final solve): separate file,
    plotted in a band above the time limit. x is jittered by repetition."""
    rows = read_csv("e3_scal.csv")
    out = {"proven": [], "timeout": [], "nomu": []}
    for r in rows:
        if r["gen_status"] == "BUILD_FAIL" or not r["param"]:
            continue
        i = int(r["index"])
        base = {"sweep": r["sweep"], "param": r["param"], "index": i}
        if r["gen_status"] != "READY":
            out["nomu"].append(base)
        elif r["status"] == "OPTIMAL" and r["verify_ok"] == "True":
            out["proven"].append(dict(base, seconds=r["seconds"]))
        elif r["status"] in ("SAT", "UNKNOWN"):
            out["timeout"].append(dict(base, seconds=r["seconds"]))
        else:
            warnings.append(f"fig6: row {r['sweep']}={r['param']} i={i} "
                            f"status {r['status']} verify {r['verify_ok']} "
                            f"not plotted")
    for kind, pts in out.items():
        for sweep in ["actors", "catalogue"]:
            sel = [p for p in pts if p["sweep"] == sweep]
            with (DATA / f"fig6_{sweep}_{kind}.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["param", "index", "seconds"])
                w.writeheader()
                for p in sel:
                    w.writerow({"param": p["param"], "index": p["index"],
                                "seconds": p.get("seconds", "")})


def fig7_data() -> None:
    """Fig. 7(a): total cost of the TOTALCOST optimum per cost profile and
    core bound (verified rows only), wide format for pgfplots."""
    rows = [r for r in read_csv("e4_sens.csv")
            if r["group"] == "profile_consolidation"]
    wide: dict[str, dict] = {}
    for r in rows:
        prof = r["instance"].removeprefix("x_")
        k = r["bound_value"]
        rec = wide.setdefault(k, {"k": k})
        if r["status"] == "OPTIMAL" and r["verify_ok"] == "True":
            rec[prof] = r["total_cost"]
            rec[prof + "_promo"] = r["promotion_cost"]
        else:
            warnings.append(f"fig7a {prof} k={k}: not OPTIMAL+VERIFY OK")
    # Fig. 7(b): the leaves of each generated argument -- Solutions (they
    # cite verifier checks of the deployment), undeveloped goals left to
    # process evidence, and undeveloped goals out of scope of the fault model
    # (gsn.py marks those undeveloped too, so they are subtracted).
    grows = [r for r in read_csv("e4_gsn.csv") if r["verify_ok"] == "True"]
    order = {"f_random_hw": 0, "f_both3": 1, "v_nvp": 2}
    names = {"f_random_hw": "H", "f_both3": "H+S", "v_nvp": "NVP"}
    if grows:
        with (DATA / "fig7_gsn.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["y", "label", "solutions",
                                              "process", "outofscope"])
            w.writeheader()
            for r in sorted(grows, key=lambda r: order.get(r["label"], 9)):
                und, oos = int(r["goals_undeveloped"]), int(r["out_of_scope"])
                w.writerow({"y": order.get(r["label"], 9),
                            "label": names.get(r["label"], r["label"]),
                            "solutions": r["solutions"],
                            "process": und - oos, "outofscope": oos})
    if wide:
        fields = ["k"] + [f"{p}{s}" for p in PROFILE_LABEL for s in ("", "_promo")]
        with (DATA / "fig7_profile.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for k in sorted(wide, key=int):
                w.writerow({c: wide[k].get(c, "") for c in fields})


# ---------------------------------------------------------------------------
def write_data_copies() -> None:
    """Copy the CSVs pgfplots will read directly (no numbers retyped)."""
    DATA.mkdir(parents=True, exist_ok=True)
    for name in ["e1_parity.csv", "e2_joint.csv", "e2_tight.csv",
                 "e3_scal.csv", "e4_sens.csv", "e4_gsn.csv"]:
        src = RESULTS / name
        if src.exists():
            (DATA / name).write_text(src.read_text())
    if e2_rel:   # Fig. 5: cost of each baseline relative to joint
        with (DATA / "e2_rel.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=E2_REL_FIELDS)
            w.writeheader()
            for r in e2_rel:
                w.writerow({k: r.get(k, "") for k in E2_REL_FIELDS})
    for fname, rel in (("e2_thr_rel.csv", thr_rel), ("e2_var_rel.csv", var_rel)):
        if rel:   # per-flow least period / input variants
            with (DATA / fname).open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rel[0]) + [
                    k for k in E2T_REL_FIELDS if k not in rel[0]],
                    extrasaction="ignore")
                w.writeheader()
                w.writerows(rel)
    if e2t_rel:  # throughput-bound variants, same layout plus the factor
        with (DATA / "e2t_rel.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=E2T_REL_FIELDS)
            w.writeheader()
            for r in e2t_rel:
                w.writerow({k: r.get(k, "") for k in E2T_REL_FIELDS})


def setup() -> None:
    """Run parameters (from the harness and the generator) and the machine
    (from results/env.txt), so the setup paragraph types no value by hand."""
    import inspect
    import re
    import harness
    import gen_synthetic
    defmacro("SetupThreads", harness.THREADS)
    defmacro("SetupTimeLimit", harness.TIME_LIMIT_MS // 1000)
    defmacro("SetupScalTimeLimit", harness.RQ3_TIME_LIMIT_MS // 1000)
    defmacro("SetupReps", 5)
    defmacro("SetupScalReps", harness.RQ3_REPS)
    defmacro("SetupScalCatActors", harness.CATSWEEP_ACTORS)
    defmacro("SetupScalMaxActors", max(harness.ACTOR_SIZES))
    defmacro("SetupScalMinActors", min(harness.ACTOR_SIZES))
    sig = inspect.signature(gen_synthetic.build_instance).parameters
    defmacro("SetupScalMuTimeLimit", sig["mu_time_limit_ms"].default // 1000)
    defmacro("SetupScalSlots", sig["total_slots"].default)
    defmacro("SetupScalTypes", sig["n_types"].default)
    env = (RESULTS / "env.txt").read_text()
    m = re.search(r"MiniZinc to FlatZinc converter, version ([\d.]+)", env)
    defmacro("SetupMiniZinc", m.group(1) if m else "?")
    m = re.search(r"OR Tools CP-SAT ([\d.]+)", env)
    defmacro("SetupCpSat", m.group(1) if m else "?")
    m = re.search(r"Model name:\s+(.*)", env)
    cpu = m.group(1).strip() if m else "?"
    cpu = re.sub(r"\(R\)|\(TM\)|CPU ", "", cpu).replace("  ", " ")
    defmacro("SetupCpu", cpu.replace(" @", ",").replace("GHz", r"\,GHz"))
    m = re.search(r"^CPU\(s\):\s+(\d+)", env, re.M)
    defmacro("SetupLogicalCpus", m.group(1) if m else "?")
    m = re.search(r"MemTotal (\d+) kB", env)
    defmacro("SetupRamGiB", fnum(int(m.group(1)) / 2 ** 20) if m else "?")
    defmacro("SetupCheckerScale", round(100 * harness.CHECKER_SCALE))


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    TABLES.mkdir(parents=True, exist_ok=True)
    setup()
    rq1()
    rq2()
    rq2_tight()
    rq2_thr()
    rq2_var()
    rep_median = rq2_reps()
    rq2_cert()
    rq2_exact()
    rq3()
    rq3_extra()
    rq4()
    breadth_price()
    breadth_comm()
    rep_median.update(breadth_multi())
    breadth_patcons()
    breadth_pipe()
    flat = breadth_flat()
    breadth_xsolver()
    breadth_pattern_ties()
    desyde_macros()
    mutation_macros()
    modelext_macros()
    write_data_copies()
    table_results(rep_median, flat)
    fig5_counts()
    fig6_points()
    fig7_data()
    lines = ["% generated by make_macros.py (SafeDSE-experiments) -- do not edit by hand", ""]
    for name in sorted(macros):
        lines.append(f"\\newcommand{{\\res{name}}}{{{macros[name]}}}")
    MACROS.write_text("\n".join(lines) + "\n")
    print(f"wrote {len(macros)} macros to {MACROS}")
    if warnings:
        print(f"\n{len(warnings)} WARNING(s):")
        for w in warnings:
            print(f"  {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

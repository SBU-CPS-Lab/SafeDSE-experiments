#!/usr/bin/env python3
"""Synthetic SDF instance generator for RQ3 (scalability).

There is no SDF3 benchmark suite, so RQ3 uses SDF3's own random
generator, `sdf3generate-sdf` (pinned by md5, see SDF3_MD5 below). It has no
seed option (it reads /dev/urandom), so every generated graph is stored in
the repository under `synthetic/graphs/` -- reproducibility comes from the
stored file, not from a seed.

Pipeline per instance (`build_instance`):
  1. generate (or reuse a previously stored) SDF3 graph with
     `repetitionVectorSum` set to the target HSDF size;
  2. unfold it (tools/hsdf.py) and check the three unfolding invariants;
  3. read the generator's own per-(actor, processor-type) executionTime
     entries -- this IS the measurement RQ3 uses, unlike tools/mkwcets.py's
     nominal-time scaffold -- and turn them into a WCETs.xml; a processor
     type the generator left out for an actor is thereby a forbidden
     binding, exactly as tools/build_dzn.py already treats a missing entry;
  4. synthesise a platform (FCR templates of 1-2 cores, 8 slots total, one
     core type per distinct processor type the generator used), a safety
     spec (25% SIL3, 25% SIL2, 50% SIL0-1, fault_model=random_hw), and a
     catalogue subset of the requested size (`CATALOGUE_ORDER`);
  5. build a first .dzn with an unconstrained period, solve THROUGHPUT to
     get this instance's own minimum period mu*, then rebuild the .dzn with
     period_ub = mu* so throughput binds (same method as the RQ2 tight
     variants) -- this is the .dzn the RQ3 sweep actually times.

Standalone use (validates one instance end to end -- build, solve, verify):

    python3 gen_synthetic.py --actors 8 --catalogue-size 9 \\
        --tag validate8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import SAFEDSE  # noqa: E402
sys.path.insert(0, str(SAFEDSE / "tools"))
from hsdf import check_unfolding, unfold        # noqa: E402
from sdf3 import parse_sdf3                     # noqa: E402
from solve import run as solve_run              # noqa: E402

ROOT = Path(__file__).resolve().parent
SYN = ROOT / "synthetic"
GRAPHS = SYN / "graphs"
DERIVED = SYN / "derived"
CATALOGUES = SYN / "catalogues"
TMP = SYN / "tmp"

# Only needed to generate NEW graphs; the stored ones are reused. Set SDF3_BIN
# to the sdf3generate-sdf binary of an SDF3 build.
SDF3_BIN = Path(os.environ.get(
    "SDF3_BIN", "~/Downloads/sdf3/build/release/Linux/bin/sdf3generate-sdf")
    ).expanduser()
SDF3_MD5 = "74cc948c9a65534c8e366b804dfcd8fd"     # pins the generator build

THREADS = 4                       # see harness.py's module docstring
DEFAULT_TIME_LIMIT_MS = 600_000   # 600 s

# Fixed, nested order: cat[k] = CATALOGUE_ORDER[:k]. `two_of_two_high_sil`
# reaches SIL 3-4 under both fault models, so cat3/cat5/cat9 all satisfy the
# 25% SIL3 / 25% SIL2 mix below under fault_model=random_hw; cat1 (`none`
# only, ceiling SIL 1) cannot, and is EXPECTED to fail to build for the SIL2
# and SIL3 actors -- a real result about catalogue richness (a build failure,
# not a solver outcome), not a bug. See harness.py rq3() for how this is
# recorded.
CATALOGUE_ORDER = [
    "none", "low_sil_doer_checker", "two_of_two_high_sil",
    "high_sil_isolated_checker", "mixed_sil_doer_checker",
    "same_cpu_doer_checker", "dual_two_of_two", "one_of_two_failover",
    "low_sil_diverse_doer_checker",
]


def _check_sdf3_binary() -> None:
    if not SDF3_BIN.exists():
        raise SystemExit(f"{SDF3_BIN}: not found (set SDF3_BIN)")
    got = hashlib.md5(SDF3_BIN.read_bytes()).hexdigest()
    if got != SDF3_MD5:
        print(f"WARNING: sdf3generate-sdf md5 {got} != pinned {SDF3_MD5} "
              f"; generated graphs may not match earlier ones.",
              file=sys.stderr)


def _settings_xml(n_actors: int, rep_sum: int, n_types: int) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<sdf3 type="sdf" version="1.0">
  <settings type="generate">
    <graph>
      <actors nr="{n_actors}"/>
      <degree avg="2" var="1" min="1" max="4"/>
      <rate avg="1" var="1" min="1" max="1" repetitionVectorSum="{rep_sum}"/>
      <initialTokens prop="0.3"/>
      <structure stronglyConnected="true" acyclic="false"/>
    </graph>
    <graphProperties>
      <procs nrTypes="{n_types}" mapChance="0.3"/>
      <execTime avg="10" var="5" min="1" max="30"/>
      <stateSize avg="1" var="1" min="1" max="1"/>
      <tokenSize avg="1" var="1" min="1" max="1"/>
      <bufferSize/>
      <bandwidthRequirement avg="2" var="0" min="1" max="4"/>
      <latencyRequirement avg="2" var="0" min="1" max="4"/>
      <throughputConstraint autoConcurrencyDegree="1" scaleFactor="0.1"/>
    </graphProperties>
  </settings>
</sdf3>
"""


def generate_with_retry(tag: str, actors_target: int, n_types: int,
                        max_tries: int = 8, tol: float = 0.25):
    """sdf3generate-sdf's `actors nr=` request is only approximate (measured:
    requesting nr=8 sometimes yields 6 actors, apparently when the strongly-
    connected constraint cannot place all of them), so this retries with a
    fresh graph (no seed option, so each attempt is a genuinely new one)
    until the unfolded size is within `tol` of the target or `max_tries` is
    used up, and returns the closest attempt. The actual size is always
    recorded (`build_instance`'s `n_hsdf_actual`), never assumed.
    """
    best = None
    for attempt in range(max_tries):
        t = tag if attempt == 0 else f"{tag}_r{attempt}"
        graph = generate_graph(t, actors_target, actors_target, n_types)
        g = parse_sdf3(graph)
        h = unfold(g)
        err = abs(h.n() - actors_target) / actors_target
        if best is None or err < best[3]:
            best = (graph, g, h, err)
        if err <= tol:
            return graph, g, h
    return best[0], best[1], best[2]


def generate_graph(tag: str, n_actors: int, rep_sum: int, n_types: int) -> Path:
    """Reuses a previously generated graph under this tag if present (the
    stored XML, not a seed, is what makes the instance reproducible)."""
    GRAPHS.mkdir(parents=True, exist_ok=True)
    out_path = GRAPHS / f"{tag}.sdf3.xml"
    if out_path.exists():
        return out_path
    _check_sdf3_binary()
    settings_path = GRAPHS / f"{tag}.opt"
    settings_path.write_text(_settings_xml(n_actors, rep_sum, n_types))
    p = subprocess.run([str(SDF3_BIN), "--settings", str(settings_path),
                        "--output", str(out_path)],
                       capture_output=True, text=True)
    if p.returncode != 0 or not out_path.exists():
        raise SystemExit(f"sdf3generate-sdf failed for {tag}:\n{p.stderr}")
    return out_path


def parse_proc_times(path: Path) -> dict[str, dict[str, int]]:
    """actor TYPE -> {processor type: executionTime}, read directly from
    sdf3generate-sdf's own output.

    tools/sdf3.py's parse_sdf3() keeps only one (default) executionTime per
    actor; the generator's <actorProperties> lists one <processor> element
    per (actor, admissible core type), which is the per-type WCET data RQ3
    needs, and is why RQ3 does not go through tools/mkwcets.py's nominal-time
    scaffold for the base actors.
    """
    root = ET.parse(str(path)).getroot()
    type_of = {a.get("name"): a.get("type") for a in root.findall(".//sdf/actor")}
    out: dict[str, dict[str, int]] = {}
    for ap in root.findall(".//actorProperties"):
        typ = type_of[ap.get("actor")]
        times: dict[str, int] = {}
        for pe in ap.findall("processor"):
            et = pe.find("executionTime")
            if et is not None:
                times[pe.get("type")] = int(round(float(et.get("time"))))
        out[typ] = times
    return out


def _pattern_component_wcets(proc_times: dict[str, dict[str, int]],
                             checker_scale: float = 0.3
                             ) -> dict[str, dict[str, int]]:
    """Synthetic per-type WCETs for every pattern-component `wcet_type`
    template in the FULL catalogue (data/patterns.yaml), not just the
    catalogue subset in use: build_dzn.py's guarded superposition
    instantiates a slot for every actor eligible for a pattern regardless of
    which subset ends up selectable, so every possible slot needs an entry.

    Follows tools/mkwcets.py's own scaffolding convention (a component's
    WCET is a fixed fraction of its owner's, per core type) -- this is a
    scalability study, not a source of published WCET numbers, so the same
    disclaimer applies: scaffolding, not a measurement.
    """
    import yaml
    doc = yaml.safe_load((SAFEDSE / "data" / "patterns.yaml").read_text())
    templates = {str(c.get("wcet_type", "<owner>"))
                for p in doc["patterns"] for c in (p.get("components") or [])}
    extra: dict[str, dict[str, int]] = {}
    for typ, times in proc_times.items():
        for tmpl in templates:
            name = tmpl.replace("<owner>", typ)
            if name in proc_times or name in extra:
                continue
            extra[name] = {pt: max(1, round(v * checker_scale))
                           for pt, v in times.items()}
    return extra


def write_wcets_xml(proc_times: dict[str, dict[str, int]],
                    proc_types: list[str], path: Path) -> None:
    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         "<!-- GENERATED by gen_synthetic.py: base-actor entries are "
         "sdf3generate-sdf's own per-processor executionTime output; "
         "pattern-component entries are a scaffold, see "
         "_pattern_component_wcets(). -->",
         "<WCET_table>"]
    for typ, times in sorted(proc_times.items()):
        L.append(f'  <mapping task_type="{typ}">')
        for pt in proc_types:
            if pt in times:
                L.append(f'    <wcet processor="{pt}" mode="standard" '
                         f'wcet="{times[pt]}"/>')
        L.append("  </mapping>")
    L.append("</WCET_table>")
    path.write_text("\n".join(L) + "\n")


def _templates_for_slots(slots: int) -> list[tuple[int, int]]:
    """(cores, instances) pairs summing to `slots` cores, each template
    holding 1 or 2 cores,
    always spanning AT LEAST 2 distinct FCR instances when slots >= 2.

    This matters beyond realism: an actor restricted (by a missing WCET
    entry) to a single core type must still be placeable under
    DIFFERENT_FCR (e.g. two_of_two_high_sil in safedse/data/patterns.yaml)
    or DIVERSE, which need at least two separate FCRs of a usable type.
    Measured: grouping a type's slots into one 2-core template (one FCR)
    made every actor confined to that type UNSAT under two_of_two_high_sil,
    for no reason related to scalability -- an artifact of the platform
    generator, not a result. For slots < 4, singleton (1-core) FCRs are used
    so at least two always exist; 4+ slots use 2-core FCR pairs.
    """
    if slots <= 1:
        return [(1, slots)] if slots else []
    if slots < 4:
        return [(1, slots)]
    out = [(2, slots // 2)]
    if slots % 2:
        out.append((1, 1))
    return out


def write_platform_xml(proc_types: list[str], total_slots: int,
                       path: Path) -> None:
    """One core type per distinct processor type the generator used,
    `total_slots` core slots in total, max_sil=4 uniformly: this is a
    scalability study, not a SIL-capped-hardware study, so an unstated cap
    (which platform.py would otherwise default to 4 anyway) is made
    explicit instead of left implicit."""
    n = len(proc_types)
    base, rem = divmod(total_slots, n)
    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         f'<platform name="synthetic_{n}type">']
    for i, pt in enumerate(proc_types):
        slots = base + (1 if i < rem else 0)
        monetary = 10 * (i + 1)
        for j, (cores, insts) in enumerate(_templates_for_slots(slots)):
            L.append(f'  <fcr_template name="T{i}{chr(97 + j)}" '
                     f'max_instances="{insts}" monetary="{monetary}">')
            L.append(f'    <processor model="{pt}" count="{cores}" '
                     f'max_sil="4" mem="64000" partitionable="false" '
                     f'partition_cost="0">')
            L.append(f'      <mode name="standard" cycle="1.0" '
                     f'dynPower="{50 + 10 * i}" area="10" '
                     f'monetary="{monetary}"/>')
            L.append("    </processor>")
            L.append("  </fcr_template>")
    L.append("</platform>")
    path.write_text("\n".join(L) + "\n")


def sil_mix_for(actor_names: list[str]) -> dict[str, int]:
    """25% SIL3, 25% SIL2, 50% split evenly between SIL0/SIL1, assigned by
    the generator's own actor order -- deterministic given
    the stored graph, no extra RNG."""
    n = len(actor_names)
    n3 = round(0.25 * n)
    n2 = round(0.25 * n)
    sil: dict[str, int] = {}
    for i, name in enumerate(actor_names):
        if i < n3:
            sil[name] = 3
        elif i < n3 + n2:
            sil[name] = 2
        else:
            sil[name] = 1 if (i - n3 - n2) % 2 == 0 else 0
    return sil


def write_safety_xml(actor_names: list[str], path: Path,
                     fault_model: str = "random_hw",
                     cost_profile: str = "myklebust2015") -> None:
    sil = sil_mix_for(actor_names)
    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         f'<safety fault_model="{fault_model}" cost_profile="{cost_profile}" '
         f'allow_promotion="true">']
    for name in actor_names:
        L.append(f'  <actor name="{name}" sil="{sil[name]}"/>')
    L.append("</safety>")
    path.write_text("\n".join(L) + "\n")


def write_desconst_xml(app_name: str, period: int, path: Path) -> None:
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<designConstraints>\n"
        f'    <constraint app_name="{app_name}" period="{period}" />\n'
        "</designConstraints>\n")


def write_catalogue(k: int, path: Path) -> None:
    import yaml
    doc = yaml.safe_load((SAFEDSE / "data" / "patterns.yaml").read_text())
    by_id = {p["id"]: p for p in doc["patterns"]}
    ids = CATALOGUE_ORDER[:k]
    path.write_text(yaml.safe_dump({"patterns": [by_id[i] for i in ids]},
                                   sort_keys=False))


def _build_dzn(app: Path, platform: Path, wcets: Path, constraints: Path,
              safety: Path, patterns: Path, out: Path) -> tuple[bool, str]:
    cmd = [sys.executable, "tools/build_dzn.py",
           "--app", str(app), "--platform", str(platform),
           "--wcets", str(wcets), "--constraints", str(constraints),
           "--safety", str(safety), "--patterns", str(patterns),
           "-o", str(out)]
    p = subprocess.run(cmd, cwd=SAFEDSE, capture_output=True, text=True)
    return p.returncode == 0, p.stderr


# ---------------------------------------------------------------------------
def build_instance(tag: str, actors_target: int, catalogue_size: int,
                   n_types: int = 3, total_slots: int = 8,
                   mu_time_limit_ms: int = 300_000) -> dict:
    """Builds one synthetic RQ3 instance end to end.

    Never raises for an ordinary experimental outcome (unfold failure, build
    failure, infeasible mu* search); those are recorded in the returned dict
    (`status`) for the harness to log as a CSV row: every run is recorded,
    not just the ones that go smoothly. Only a
    plumbing error (e.g. the generator binary missing) raises.

    actors_target sets BOTH the SDF actor count and repetitionVectorSum, so
    the generator is forced to single-rate graphs (every q_i = 1, so
    actors_target IS the HSDF size exactly): with `structure
    stronglyConnected="true"` the whole graph is one component, whose
    repetition-vector ratios are fixed by its rates, so sum(q) = nr forces
    every q_i = 1. Multi-rate input first hit a build_dzn.py bug (the WCET
    key of an "<owner>"-templated component of an owner with q > 1, fixed in
    SafeDSE 9454bec). The sweep stays single-rate so that the
    swept variable is the HSDF size itself, and because every other RQ in
    the paper also uses single-rate inputs (Rosvall's benchmarks are
    pre-unfolded .hsdf.xml).
    """
    d = DERIVED / tag
    d.mkdir(parents=True, exist_ok=True)
    graph, g, h = generate_with_retry(tag, actors_target, n_types)
    problems = check_unfolding(g, h)
    meta = dict(tag=tag, actors_target=actors_target,
               catalogue_size=catalogue_size, n_sdf_actors=len(g.actors),
               n_hsdf_actual=h.n(), unfold_ok=not problems,
               unfold_problems="; ".join(problems))
    if problems:
        return dict(meta, status="UNFOLD_FAIL")

    proc_times = parse_proc_times(graph)
    proc_types = sorted({t for times in proc_times.values() for t in times})
    meta["n_core_types_actual"] = len(proc_types)
    proc_times.update(_pattern_component_wcets(proc_times))

    wcets = d / "WCETs.xml"
    write_wcets_xml(proc_times, proc_types, wcets)
    platform = d / "platform.xml"
    write_platform_xml(proc_types, total_slots, platform)
    safety = d / "safety.xml"
    actor_names = [a.name for a in g.actors]
    write_safety_xml(actor_names, safety)

    CATALOGUES.mkdir(parents=True, exist_ok=True)
    catalogue = CATALOGUES / f"cat{catalogue_size}.yaml"
    if not catalogue.exists():
        write_catalogue(catalogue_size, catalogue)

    desc0 = d / "desConst_free.xml"
    write_desconst_xml(g.name, -1, desc0)
    dzn0 = d / "free.dzn"
    ok, err = _build_dzn(graph, platform, wcets, desc0, safety, catalogue, dzn0)
    meta["build1_ok"] = ok
    if not ok:
        return dict(meta, status="BUILD_FAIL", build_error=err[-1500:])

    r = solve_run(str(dzn0), "THROUGHPUT", timeout=mu_time_limit_ms // 1000 + 60,
                 threads=THREADS, time_limit_ms=mu_time_limit_ms)
    meta["mu_status"] = r["status"]
    meta["mu_seconds"] = round(r.get("seconds", 0.0), 3)
    if "solution" not in r:
        return dict(meta, status=f"MU_{r['status']}")
    mu = r["solution"]["mu"]
    mu = max(mu) if isinstance(mu, list) else mu
    meta["mu_star"] = mu
    meta["mu_star_proven"] = r["status"] == "OPTIMAL"

    desc1 = d / "desConst_tight.xml"
    write_desconst_xml(g.name, mu, desc1)
    dzn1 = d / "final.dzn"
    ok, err = _build_dzn(graph, platform, wcets, desc1, safety, catalogue, dzn1)
    meta["build2_ok"] = ok
    if not ok:
        return dict(meta, status="BUILD_FAIL2", build_error=err[-1500:])

    meta["dzn"] = str(dzn1)
    meta["status"] = "READY"
    return meta


# ---------------------------------------------------------------------------
# standalone validation: build one instance, solve TOTALCOST, verify
# ---------------------------------------------------------------------------
def _verify(dzn: Path, solution: dict, tag: str) -> dict:
    TMP.mkdir(parents=True, exist_ok=True)
    solp = TMP / f"{tag}.sol.json"
    repp = TMP / f"{tag}.report.json"
    solp.write_text(json.dumps(solution))
    subprocess.run([sys.executable, str(SAFEDSE / "tools" / "verify.py"),
                    "--dzn", str(dzn), "--solution", str(solp),
                    "--quiet", "--json-report", str(repp)], check=False)
    if repp.exists():
        return json.loads(repp.read_text())
    return {"ok": False, "messages": ["verify.py produced no report"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actors", type=int, required=True,
                    help="target HSDF size")
    ap.add_argument("--catalogue-size", type=int, default=9,
                    choices=[1, 3, 5, 9])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--n-types", type=int, default=3)
    ap.add_argument("--total-slots", type=int, default=8)
    ap.add_argument("--time-limit", type=int, default=DEFAULT_TIME_LIMIT_MS)
    ap.add_argument("--optimise", default="TOTALCOST")
    a = ap.parse_args()

    meta = build_instance(a.tag, a.actors, a.catalogue_size, a.n_types,
                          a.total_slots, mu_time_limit_ms=a.time_limit)
    print(json.dumps(meta, indent=2))
    if meta["status"] != "READY":
        return 1

    r = solve_run(meta["dzn"], a.optimise,
                 timeout=a.time_limit // 1000 + 60, threads=THREADS,
                 time_limit_ms=a.time_limit)
    print(f"solve status={r['status']} seconds={r.get('seconds')}")
    if "solution" not in r:
        return 1
    report = _verify(Path(meta["dzn"]), r["solution"], a.tag)
    print(f"verify ok={report['ok']}")
    if not report["ok"]:
        print(json.dumps(report, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

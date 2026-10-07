#!/usr/bin/env python3
"""Independently verify a solution returned by the CP model.

Rebuilds the mapping-and-schedule-aware graph (MSAG) of the deployed design
(inactive pattern slots removed, static orders spliced) from the solver's
assignment, then computes its iteration period twice -- by Lawler's parametric
maximum-cycle-ratio search (tools/golden.py) and by max-plus simulation -- and
checks both against the mu the solver reported.

This exists because the failure mode of constraint modelling is silent: a wrong
encoding returns a plausible number, not an error.  The open-chain bug that
dropped the "core period >= sum of its WCETs" bound produced perfectly
well-formed output for weeks of prototyping.  Nothing in this file shares code
with the MiniZinc model.

    verify.py --dzn out/rasta.dzn --solution out/rasta.json

Copy of SafeDSE tools/verify.py at commit b830db7 (the verifier scored as
"safedse" in results/e6_mut.csv), kept so those rows stay reproducible:
SafeDSE's verifier now has the checks of verifier_ext/. Only the import of
golden.py and the comments differ from that commit.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paths import SAFEDSE  # noqa: E402
sys.path.insert(0, str(SAFEDSE / "tools"))
from golden import mcm, selftimed_period, selftimed_trace  # noqa: E402


# --------------------------------------------------------------------------
# Structured check log (docs/inputs.md#check-log).
#
# The console output above was written for a human reading a failure.  The GSN
# generator needs the same information addressably: a Solution node in a safety
# argument has to say WHICH check discharges it, and be wrong if that check did
# not actually run.  So every check also appends a record here, and --json-report
# dumps them.
#
# This is deliberately a log of what was CHECKED, not of what is true.  A claim
# with no matching record is undischarged, and gsn.py treats the absence of a
# record exactly as it treats a failed one.  That is the point: it makes
# "nobody looked" and "someone looked and it was fine" distinguishable, which is
# the distinction a safety argument lives or dies on.
REPORT: list[dict] = []


def _rec(kind: str, ok: bool, detail: str, **extra) -> None:
    REPORT.append({"kind": kind, "ok": bool(ok), "detail": detail, **extra})


# --------------------------------------------------------------------------
def _components(n: int, edges: list[tuple[int, int, int]]) -> list[set[int]]:
    """Weakly connected components of the MSAG (union-find)."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for u, v, _ in edges:
        ru, rv = find(u), find(v)
        if ru != rv:
            parent[ru] = rv
    groups: dict[int, set[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), set()).add(i)
    return list(groups.values())


def _aslist(x):
    return x if isinstance(x, list) else [x]


def parse_dzn(path: str) -> dict:
    """Minimal .dzn reader for the arrays this framework emits."""
    txt = Path(path).read_text()
    txt = re.sub(r"^\s*%.*$", "", txt, flags=re.M)
    out: dict = {}
    for m in re.finditer(r"(\w+)\s*=\s*(.*?);", txt, re.S):
        name, body = m.group(1), m.group(2).strip()
        out[name] = _parse_value(body)
    return out


def _parse_value(body: str):
    body = body.strip()
    if body.startswith("array2d") or body.startswith("array3d"):
        inner = body[body.index("(") + 1: body.rindex(")")]
        # last bracketed group is the data
        depth, start = 0, None
        for i, ch in enumerate(inner):
            if ch in "[":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "]":
                depth -= 1
        data = inner[start:]
        return _parse_flat(data)
    if body.startswith("[|"):
        rows = [r for r in body[2:body.rindex("|]")].split("|") if r.strip()]
        return [[_atom(x) for x in r.split(",") if x.strip()] for r in rows]
    if body.startswith("["):
        return _parse_flat(body)
    return _atom(body)


def _parse_flat(body: str):
    if body.startswith("[|"):
        rows = [r for r in body[2:body.rindex("|]")].split("|") if r.strip()]
        return [x for r in rows for x in (_atom(y) for y in r.split(",") if y.strip())]
    return [_atom(x) for x in body.strip()[1:-1].split(",") if x.strip()]


def _atom(s: str):
    s = s.strip()
    if s.startswith('"'):
        return s.strip('"')
    if s in ("true", "false"):
        return s == "true"
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


# --------------------------------------------------------------------------
def build_msag(d: dict, sol: dict):
    """Reconstruct the MSAG of the DEPLOYED design.

    Nodes: the active computation nodes, plus the three communication actors
    of every channel that turned out remote. Inactive pattern slots are left
    out and each core's static order is spliced around them (the model keeps
    them in the order with zero WCET; a token-free cycle through them only has
    zero weight in the model, but here it would be reported as a deadlock of a
    design in which those nodes do not exist).
    Splicing keeps every cycle of the deployed schedule, including the wrap
    cycle, so the MCR is that of the design as it runs.

    Edges: application and pattern channels between live nodes (tok >= 0);
    serialisation (no token) and one wrap edge per core (one token); for a
    channel that the front-end refined (removed from tok), the direct
    edge with its initial tokens when local, else the path
    src -> block -> send -> rec -> dst with the initial tokens on the first
    edge, plus the two buffer back-edges -- as lib/comm.mzn posts them.

    Returns (n, edges, T, live) over the full node index space; nodes not in
    `live` have no edges.
    """
    n = d["n"]
    tokflat = d["tok"]
    if tokflat and isinstance(tokflat[0], list):
        tok = tokflat
    else:
        tok = [tokflat[r * n:(r + 1) * n] for r in range(n)]

    proc = sol["proc"]
    succ = sol["succ"]
    T = sol["T"]
    act = sol.get("active", [True] * n)
    if not isinstance(act, list):
        act = [act] * n

    # Communication actors are scheduled on the bus, not in a processor's
    # static order, so they take no part in the succ/wrap reconstruction.
    comm = d.get("comm_actor", [False] * n)
    if not isinstance(comm, list):
        comm = [comm] * n
    comm = [bool(x) for x in comm] + [False] * (n - len(comm))
    cpu = [i for i in range(n) if not comm[i]]
    on = [bool(act[i]) and not comm[i] for i in range(n)]
    live = {i for i in range(n) if on[i]}

    edges: list[tuple[int, int, int]] = []
    for i in range(n):
        for j in range(n):
            if tok[i][j] >= 0 and on[i] and on[j]:
                edges.append((i, j, tok[i][j]))

    # static order per core, spliced around inactive nodes: consecutive LIVE
    # nodes get a token-less serialisation arc, last -> first a one-token wrap
    succ_of = {i: succ[i] - 1 for i in cpu if succ[i] > 0}
    has_pred = set(succ_of.values())
    for h in cpu:
        if h in has_pred:
            continue                          # not the head of its core
        chain, x, seen = [], h, set()
        while x is not None and x not in seen:
            seen.add(x)
            chain.append(x)
            x = succ_of.get(x)
        keep = [x for x in chain if on[x]]
        for u, v in zip(keep, keep[1:]):
            edges.append((u, v, 0))
        if keep:
            edges.append((keep[-1], keep[0], 1))

    nch = d.get("nCh", 0)
    if nch:
        cs = _aslist(d["ch_src"]); cd = _aslist(d["ch_dst"])
        cb = _aslist(d["ch_block"]); csd = _aslist(d["ch_send"])
        cr = _aslist(d["ch_rec"])
        dtok = _aslist(d.get("ch_direct_tok", [0] * nch))
        sbuf = sol.get("sendbuf", [1] * nch)
        rbuf = sol.get("recbuf", [1] * nch)
        for c in range(nch):
            s_, d_ = cs[c] - 1, cd[c] - 1
            if not (on[s_] and on[d_]):
                continue
            t0 = dtok[c] if c < len(dtok) else 0
            if proc[s_] == proc[d_]:
                edges.append((s_, d_, t0))    # local: the direct edge
                continue
            b, sn, r = cb[c] - 1, csd[c] - 1, cr[c] - 1
            live |= {b, sn, r}
            edges += [(s_, b, t0), (b, sn, 0), (sn, r, 0), (r, d_, 0)]
            edges.append((b, s_, sbuf[c]))    # send-side buffer
            edges.append((r, sn, rbuf[c]))    # receive-side FIFO

    return n, edges, T, live


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dzn", required=True)
    ap.add_argument("--solution", required=True)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--json-report", help="write the structured check log here "
                                          "(consumed by tools/gsn.py)")
    a = ap.parse_args()

    d = parse_dzn(a.dzn)
    sol = json.loads(Path(a.solution).read_text())
    n, edges, T, live = build_msag(d, sol)

    mus = sol["mu"] if isinstance(sol["mu"], list) else [sol["mu"]]
    app = d.get("app", [1] * n)
    nApps = d.get("nApps", 1)
    ok, msgs = True, []

    # Rosvall computes period[z] as the MCR of the MSAG's CONNECTED COMPONENT
    # containing application z, so the check must be per component, not per
    # application: two applications sharing a processor are in one component and
    # must report the same period. Splitting by application instead would
    # compare an application against a subgraph it does not own.
    # Only LIVE nodes take part (build_msag): active computation nodes, plus
    # communication actors whose channel actually turned out remote.
    comm_flags = d.get("comm_actor", [False] * n)
    if not isinstance(comm_flags, list):
        comm_flags = [comm_flags] * n
    live_edges = [(u, v, t) for u, v, t in edges if u in live and v in live]
    comp = [c & live for c in _components(n, live_edges) if c & live]
    comp = [c for c in comp if any(not (i < len(comm_flags) and comm_flags[i])
                                   for i in c)]
    if not a.quiet and len(comp) > 1:
        print(f"  MSAG has {len(comp)} connected components")
    for members in comp:
        members = sorted(members)
        idx = {g: k for k, g in enumerate(members)}
        sub = [(idx[u], idx[v], t) for u, v, t in live_edges
               if u in idx and v in idx]
        subT = [T[g] for g in members]
        apps_here = sorted({app[g] for g in members
                            if not (g < len(comm_flags) and comm_flags[g])})
        if not apps_here:
            continue
        k = mcm(len(members), sub, subT)
        sim = selftimed_period(len(members), sub, subT)
        if k is None:
            ok = False
            msgs.append(f"component {apps_here}: MSAG deadlocks but a period "
                        f"was returned")
            continue
        # The model's mu is an INTEGER, so the least period it can report is
        # ceil(MCR); with integer WCETs and tokens, integer potentials exist
        # for every integer mu >= MCR. Demanding mu == MCR rejected every
        # solution whose MCR is fractional (observed: solver 424, MCR 847/2).
        # Equality with the ceiling stays exact.
        want = Fraction(math.ceil(k))
        for z in apps_here:
            reported = mus[z - 1] if z - 1 < len(mus) else mus[0]
            if Fraction(reported) != want:
                ok = False
                msgs.append(
                    f"app {z}: period mismatch -- solver {reported}, component "
                    f"MCR {k} (least integer period {want}). Applications "
                    f"sharing a component must share a period.")
        if sim is not None and sim != k:
            ok = False
            msgs.append(f"component {apps_here}: oracle disagreement -- "
                        f"Lawler {k}, simulation {sim}")
        _rec("period", all(Fraction(mus[z - 1] if z - 1 < len(mus) else mus[0])
                           == want for z in apps_here)
             and (sim is None or sim == k),
             f"apps {apps_here}: solver period is the least integer at or above "
             f"the MSAG's maximum cycle ratio, computed independently by Lawler's "
             f"parametric search "
             f"({k}) and by max-plus self-timed simulation ({sim})",
             apps=apps_here, lawler=str(k), simulation=str(sim))
        if not a.quiet:
            print(f"  component apps={apps_here}: Lawler {k}, simulation {sim}")

    # ---- exact latency, transient included -----------------------------
    # lib/latency.mzn uses the PERIODIC-PHASE estimate
    #     pot[d] - pot[s] + rho*mu + T[d]
    # which is exact once the schedule has settled but ignores the transient.
    # Simulate the real schedule and compare. A reported latency below the
    # simulated worst case is not a bug in the solver -- it is the documented
    # limitation of the estimate -- but it MUST be surfaced, because publishing
    # the estimate as a worst-case bound would be wrong.
    nlat = d.get("nLatCon", 0)
    if nlat and "latency" in sol:
        trace = selftimed_trace(n, edges, T, iters=40)
        src = d["lat_src"] if isinstance(d["lat_src"], list) else [d["lat_src"]]
        dst = d["lat_dst"] if isinstance(d["lat_dst"], list) else [d["lat_dst"]]
        rho = d["lat_rho"] if isinstance(d["lat_rho"], list) else [d["lat_rho"]]
        rep = sol["latency"] if isinstance(sol["latency"], list) else [sol["latency"]]
        if trace:
            for c in range(nlat):
                si, di, r = src[c] - 1, dst[c] - 1, rho[c]
                obs = max(trace[k + r][di] + T[di] - trace[k][si]
                          for k in range(len(trace) - r))
                if not a.quiet:
                    print(f"  latency[{c+1}]: model estimate {rep[c]}, "
                          f"simulated worst case {obs}")
                if obs > rep[c]:
                    msgs.append(
                        f"NOTE latency[{c+1}]: transient worst case {obs} exceeds "
                        f"the periodic-phase estimate {rep[c]} by {obs - rep[c]} "
                        f"-- expected (see lib/latency.mzn), but do not report "
                        f"the estimate as a worst-case bound")

    # ---- safety invariants ----------------------------------------------
    # These are the claims the framework exists to make, so they are checked
    # externally rather than trusted to hold because a constraint was written.
    if "sil_impl" in sol and "csil" in sol:
        sil_impl = sol["sil_impl"]
        csil = sol["csil"]
        proc = sol["proc"]
        part = sol.get("partition", [False] * len(csil))
        sreq = d["sil_req_parent"]
        par = d["parent"]
        ctype = d["ctype"]
        max_sil = d["max_sil"]
        comm_flags = d.get("comm_actor", [False] * n)
        if not isinstance(comm_flags, list):
            comm_flags = [comm_flags] * n

        act = sol.get("active", [True] * n)
        exempt0 = d.get("comm_sil_mode", 0) == 0
        for i in range(n):
            if not act[i]:
                continue          # inactive pattern slot: neutralised, not real
            if exempt0 and i < len(comm_flags) and comm_flags[i]:
                continue
            need = sreq[par[i] - 1]
            if sil_impl[i] < need:
                ok = False
                msgs.append(f"actor {i+1}: developed to SIL {sil_impl[i]} but "
                            f"requires SIL {need}")
            if sil_impl[i] > csil[proc[i] - 1]:
                ok = False
                msgs.append(f"actor {i+1}: SIL {sil_impl[i]} on a core "
                            f"provisioned only to SIL {csil[proc[i]-1]}")
            _rec("sil_actor",
                 sil_impl[i] >= need and sil_impl[i] <= csil[proc[i] - 1],
                 f"node {i+1} requires SIL {need}, is implemented at SIL "
                 f"{sil_impl[i]}, and runs on core {proc[i]} which is "
                 f"provisioned to SIL {csil[proc[i]-1]}",
                 node=i + 1, required=need, implemented=sil_impl[i],
                 core=proc[i], core_sil=csil[proc[i] - 1])

        for p_ in range(len(csil)):
            cap = max_sil[ctype[p_] - 1]
            if csil[p_] > cap:
                ok = False
                msgs.append(f"core {p_+1}: provisioned to SIL {csil[p_]} but its "
                            f"type can only be certified to SIL {cap}")
            # Koopman rule 2: without partitioning, one SIL per core
            # In exempt mode (--comm-sil exempt) communication actors are not
            # application software and Koopman rule 2 does not reach them, so
            # the model leaves them at SIL 0 and the check must skip them too.
            exempt = d.get("comm_sil_mode", 0) == 0
            if not part[p_]:
                on = [sil_impl[i] for i in range(n)
                      if proc[i] == p_ + 1 and act[i]
                      and not (exempt and i < len(comm_flags) and comm_flags[i])]
                if on and len(set(on)) > 1:
                    ok = False
                    msgs.append(f"core {p_+1} has mixed SILs {sorted(set(on))} "
                                f"with no certified partitioning -- Koopman "
                                f"rule 2 violated")
                if on and set(on) != {csil[p_]}:
                    ok = False
                    msgs.append(f"core {p_+1}: csil={csil[p_]} but actors are at "
                                f"{sorted(set(on))}")
            hosted = sorted({sil_impl[i] for i in range(n)
                             if proc[i] == p_ + 1 and act[i]
                             and not (d.get("comm_sil_mode", 0) == 0
                                      and i < len(comm_flags) and comm_flags[i])})
            _rec("isolation",
                 csil[p_] <= cap and (part[p_] or len(hosted) <= 1),
                 f"core {p_+1} is provisioned to SIL {csil[p_]} (type ceiling "
                 f"{cap}), hosts SILs {hosted}, certified partitioning "
                 f"{'available and used' if part[p_] else 'not used'}",
                 core=p_ + 1, core_sil=csil[p_], type_ceiling=cap,
                 hosted_sils=hosted, partitioned=bool(part[p_]))
        if not a.quiet:
            live = [(p_ + 1, csil[p_]) for p_ in range(len(csil)) if csil[p_] > 0]
            print(f"  isolation: {len(live)} provisioned cores {live}")

    # ---- placement relations ---------------------------------------------
    # The safety argument rests on these: a 2-of-2 pair in the same fault
    # containment region tolerates nothing.  Checked externally against the
    # returned mapping rather than trusted to the constraint that posted them.
    npl = d.get("nPL", 0)
    if npl and "pat" in sol:
        RELN = {1: "SAME", 2: "DIFFERENT", 3: "DIFFERENT_FCR", 4: "DIVERSE"}
        pl_u = _aslist(d["pl_u"]); pl_v = _aslist(d["pl_v"])
        pl_rel = _aslist(d["pl_rel"]); pl_owner = _aslist(d["pl_owner"])
        guard = d["pl_guard"]
        npat = d["nPat"]
        if guard and not isinstance(guard[0], list):
            guard = [guard[r * npat:(r + 1) * npat] for r in range(len(guard) // npat)]
        pat = sol["pat"]
        fcr = d["fcr"]; ctype = d["ctype"]
        checked = 0
        for c in range(npl):
            if not guard[c][pat[pl_owner[c] - 1] - 1]:
                continue                       # this pattern was not selected
            u, v = pl_u[c] - 1, pl_v[c] - 1
            pu, pv = sol["proc"][u], sol["proc"][v]
            rel = pl_rel[c]
            bad = (
                (rel == 1 and pu != pv) or
                (rel == 2 and pu == pv) or
                (rel == 3 and fcr[pu - 1] == fcr[pv - 1]) or
                (rel == 4 and ctype[pu - 1] == ctype[pv - 1])
            )
            checked += 1
            _rec("placement", not bad,
                 f"{RELN[rel]} between nodes {u+1} and {v+1}: cores "
                 f"{pu}/{pv}, FCRs {fcr[pu-1]}/{fcr[pv-1]}, core types "
                 f"{ctype[pu-1]}/{ctype[pv-1]}",
                 relation=RELN[rel], rel_code=rel, u=u + 1, v=v + 1,
                 owner=pl_owner[c], cores=[pu, pv],
                 fcrs=[fcr[pu - 1], fcr[pv - 1]],
                 ctypes=[ctype[pu - 1], ctype[pv - 1]])
            if bad:
                ok = False
                msgs.append(
                    f"placement {RELN[rel]} between nodes {u+1} and {v+1} is "
                    f"violated: cores {pu}/{pv}, FCRs "
                    f"{fcr[pu-1]}/{fcr[pv-1]}, types {ctype[pu-1]}/{ctype[pv-1]}")
        if not a.quiet:
            sel = sorted({d["pat_name"][p - 1] for p in pat})
            print(f"  patterns: {sel}; {checked} active placement relations "
                  f"checked")

    # the bound the open-chain bug used to lose
    # Communication actors are excluded, consistently with the model: their
    # time is spent on the bus, not on the core's execution unit. A transfer
    # overlaps computation, so it lengthens LATENCY but only lengthens the
    # PERIOD when the bus is genuinely the bottleneck.
    load = {}
    for i in range(n):
        if i < len(comm_flags) and comm_flags[i]:
            continue
        if not act[i]:
            continue
        load[sol["proc"][i]] = load.get(sol["proc"][i], 0) + T[i]
    worst = max(load.values()) if load else 0
    if max(mus) < worst:
        ok = False
        msgs.append(f"worst period {max(mus)} is below the busiest core's load "
                    f"{worst} -- the processor-availability wrap edge is missing")
    _rec("load_bound", max(mus) >= worst,
         f"the reported period {max(mus)} is at least the busiest core's total "
         f"execution demand {worst}",
         worst_period=max(mus), busiest_core_load=worst)
    if not a.quiet:
        print(f"  busiest core load = {worst}")
    for m in msgs:
        print(f"  FAIL: {m}")
    print("  VERIFY OK" if ok else "  VERIFY FAILED")

    if a.json_report:
        Path(a.json_report).write_text(json.dumps(
            {"ok": ok, "dzn": a.dzn, "solution": a.solution,
             "messages": msgs, "checks": REPORT}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

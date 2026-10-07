#!/usr/bin/env python3
"""Solver-independent lower bound on the period of a pattern-first flow.

A pattern-first flow fixes every actor's pattern before mapping. With the
patterns fixed, the set of active nodes and the channels among them are
fixed too, whatever the mapping and schedule. Every such channel, with its
initial tokens, is an edge of the deployed mapping- and scheduling-aware
graph (tools/verify.py build_msag keeps every `tok` edge between live
nodes), and every node runs at least its least WCET. Mapping and static
order only add edges. So the maximum cycle ratio of this fixed graph, with
each node at its least WCET, bounds the period of every design of the flow
from below, both in the model (which is never below the deployed MCR)
and in the deployed system. If ceil(bound) exceeds the period bound, the
flow is infeasible independently of the solver: the UNSAT is certified.

Three bounds, each valid, each stronger than the one before:
`lb` weights every node with `min_wcet` (least WCET over all core types, as
the front-end computes it; needs no safety rule); `lb_sil` with the least
WCET over the core types whose max_sil admits the actor's required SIL
(lib/safety.mzn l.40, 48: an active node needs csil >= its SIL, and csil <=
max_sil of the type); `lb_place` is max(lb_sil, the largest local bound of
one actor), where the local bound of an actor is the least MCR of its own
active nodes (owner and components) over all assignments of core types that
satisfy its active SAME and DIVERSE relations (lib/activation.mzn l.147-153:
DIVERSE = different core type; SAME = same core, hence same type), each node
at its least WCET on its assigned type. Example: 2-of-2 under
systematic faults puts the replica on another core type, which the first two
bounds ignore. `certified`, `certified_sil`, `certified_place` compare the
three with the period bound.
Communication actors are left out (removing nodes only removes cycles).
The MCR is Lawler's parametric search from safedse tools/golden.py, which
shares no code with the model.

Input rows: every PF row (PFmin, PFmax, PFlight, PFcheap) with status UNSAT
in e2_joint.csv, e2_tight.csv and e2_var.csv; and every PF flow of
e2_thr.csv, where the bound must be <= the flow's least period (a sanity
check of the bound; equality says the fixed graph alone sets the period).
Output: results/e2_cert.csv (rewritten on each call; no solver runs).

    python3 certify_pf.py
"""
from __future__ import annotations

import csv
import math
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402
from golden import mcm_karp  # noqa: E402  (safedse/tools, via harness)
from verify import parse_dzn  # noqa: E402

OUT = H.RESULTS / "e2_cert.csv"
FIELDS = ["source", "variant", "instance", "factor", "baseline", "status",
          "period_ub", "mu_flow", "lb", "lb_ceil", "lb_sil", "lb_sil_ceil",
          "lb_place", "lb_place_ceil", "crit_actor", "crit_pattern",
          "crit_local_ceil", "certified", "certified_sil", "certified_place",
          "lb_le_mu", "patterns_match"]
BIG = 1_000_000


def _fixed_graph(d: dict, choice: list[int]):
    n, npat = d["n"], len(d["pat_name"])
    tok = d["tok"]
    comm = d.get("comm_actor", [False] * n)
    on = [not comm[i] and d["node_guard"][i * npat
                                          + choice[d["node_owner"][i] - 1] - 1]
          for i in range(n)]
    edges = [(i, j, tok[i * n + j]) for i in range(n) for j in range(n)
             if on[i] and on[j] and tok[i * n + j] >= 0]
    return on, edges


def _sil_wcet(d: dict) -> list[int]:
    """Least WCET over core types that can carry the node's required SIL."""
    n, nct, nmd = d["n"], d["nCoreTypes"], d["nModes"]
    out = []
    for i in range(n):
        s = d["sil_req_parent"][d["parent"][i] - 1]
        w = [d["wcet"][(i * nct + t) * nmd + m] for t in range(nct)
             for m in range(nmd) if d["max_sil"][t] >= s]
        w = [x for x in w if x < BIG]
        out.append(min(w) if w else BIG)
    return out


def _type_wcet(d: dict, i: int) -> dict[int, int]:
    """Least WCET of node i per core type admitting its SIL."""
    nct, nmd = d["nCoreTypes"], d["nModes"]
    s = d["sil_req_parent"][d["parent"][i] - 1]
    out = {}
    for t in range(nct):
        if d["max_sil"][t] < s:
            continue
        w = [d["wcet"][(i * nct + t) * nmd + m] for m in range(nmd)]
        w = [x for x in w if x < BIG]
        if w:
            out[t] = min(w)
    return out


def _local_placed(d: dict, a: int, own: set[int], sub: list,
                  choice: list[int]) -> Fraction | None:
    """Least local MCR of actor a over core-type assignments that satisfy
    its active SAME / DIVERSE relations."""
    import itertools
    npat, n = len(d["pat_name"]), d["n"]
    rel = []
    for c in range(d.get("nPL", 0)):
        if (d["pl_owner"][c] == a
                and d["pl_guard"][c * npat + choice[a - 1] - 1]
                and d["pl_rel"][c] in (1, 4)):
            rel.append((d["pl_u"][c] - 1, d["pl_v"][c] - 1, d["pl_rel"][c]))
    nodes = sorted(own)
    tw = {i: _type_wcet(d, i) for i in nodes}
    best = None
    for ts in itertools.product(*(sorted(tw[i]) for i in nodes)):
        t = dict(zip(nodes, ts))
        if any((r == 1 and t[u] != t[v]) or (r == 4 and t[u] == t[v])
               for u, v, r in rel if u in t and v in t):
            continue
        w = [tw[i][t[i]] if i in t else 0 for i in range(n)]
        m = mcm_karp(n, sub, w)
        if m is not None and (best is None or m < best):
            best = m
    return best


def bound(d: dict, choice: list[int]) -> dict:
    on, edges = _fixed_graph(d, choice)
    n = d["n"]
    res = {}
    for key, wt in (("lb", list(d["min_wcet"])), ("lb_sil", _sil_wcet(d))):
        w = [wt[i] if on[i] else 0 for i in range(n)]
        m = mcm_karp(n, edges, w)
        if m is None:   # a token-free cycle would be a deadlock
            raise SystemExit("fixed graph has no cycle ratio")
        res[key] = m
    # the actor whose own nodes (owner + active components) set the bound
    best = (Fraction(-1), "", "")
    placed = res["lb_sil"]
    for a in H.base_parents(d):
        own = {i for i in range(n)
               if on[i] and d["node_owner"][i] == a}
        sub = [(u, v, t) for u, v, t in edges if u in own and v in own]
        w = [d["min_wcet"][i] if i in own else 0 for i in range(n)]
        m = mcm_karp(n, sub, w) if sub else None
        mp = _local_placed(d, a, own, sub, choice) if sub else None
        if mp is not None and mp > placed:
            placed = mp
        if m is not None and m > best[0]:
            best = (m, d["parent_name"][a - 1].split(".", 1)[-1],
                    d["pat_name"][choice[a - 1] - 1])
    res.update(crit_local=best[0], crit_actor=best[1], crit_pattern=best[2],
               lb_place=placed)
    return res


def _patterns_str(d: dict, choice: list[int]) -> str:
    pn = d["parent_name"]
    return ";".join(f"{pn[a - 1].split('.', 1)[-1]}="
                    f"{d['pat_name'][choice[a - 1] - 1]}"
                    for a in H.base_parents(d) if not pn[a - 1].startswith("__"))


def _rows(name: str) -> list[dict]:
    p = H.RESULTS / name
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    cache: dict[tuple, tuple] = {}

    def inst(var: str, name: str):
        if (var, name) not in cache:
            spec = H.E2_INSTANCES[name]
            dz = H.build_e2_instances(name, spec, variant=var)
            vspec = spec if var == "base" else H.variant_spec(spec, var)
            cache[(var, name)] = (parse_dzn(str(dz["joint"])), vspec)
        return cache[(var, name)]

    todo = []
    for src in ("e2_joint.csv", "e2_tight.csv", "e2_var.csv"):
        for r in _rows(src):
            if r["baseline"] in H.PF_RULES and r["status"] == "UNSAT":
                todo.append((src, r.get("variant", "base"), r["instance"],
                             r.get("factor", "none"), r["baseline"], r))
    for r in _rows("e2_thr.csv"):
        if r["flow"] in H.PF_RULES:
            todo.append(("e2_thr.csv", r["variant"], r["instance"], "-",
                         r["flow"], r))
    if OUT.exists():
        OUT.unlink()
    n_cert = n_unsat = 0
    for src, var, name, fac, rule, r in todo:
        d, vspec = inst(var, name)
        choice = H.pf_choice(d, vspec["catalogue"], rule)
        b = bound(d, choice)
        ub = int(r["period_ub"]) if r.get("period_ub") else None
        row = dict(source=src, variant=var, instance=name, factor=fac,
                   baseline=rule, status=r["status"], period_ub=ub or "",
                   lb=str(b["lb"]), lb_ceil=math.ceil(b["lb"]),
                   lb_sil=str(b["lb_sil"]), lb_sil_ceil=math.ceil(b["lb_sil"]),
                   lb_place=str(b["lb_place"]),
                   lb_place_ceil=math.ceil(b["lb_place"]),
                   crit_actor=b["crit_actor"], crit_pattern=b["crit_pattern"],
                   crit_local_ceil=math.ceil(b["crit_local"]),
                   patterns_match=_patterns_str(d, choice) == r["patterns"])
        if src == "e2_thr.csv":
            if r["status"] == "OPTIMAL" and r["verify_ok"] == "True":
                mu = int(r["mu_max"])
                row.update(mu_flow=mu,
                           lb_le_mu=math.ceil(b["lb_place"]) <= mu)
        else:
            n_unsat += 1
            row.update(certified=ub is not None and math.ceil(b["lb"]) > ub,
                       certified_sil=ub is not None
                       and math.ceil(b["lb_sil"]) > ub,
                       certified_place=ub is not None
                       and math.ceil(b["lb_place"]) > ub)
            n_cert += bool(row["certified_place"])
        H.append_row(OUT, FIELDS, row)
    print(f"{OUT}: {len(todo)} rows; {n_cert} of {n_unsat} PF UNSAT rows "
          f"certified by the cycle bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Mutation analysis of the verifier and the argument generator.

Three parts, one CSV (results/e6_mut.csv, one row per mutant, resumable):

* `solution`: corrupted solutions. Each operator takes a verified optimum
  (results/mut/base/<instance>.sol.json) and breaks one requirement: a
  placement relation, a node or core SIL, a type ceiling, the one-SIL-per-core
  rule, partitioning capability, the period, a WCET, the static order, the
  activation of pattern components, the admissible pattern set, or a field of
  the solution file. Where an operator moves a node or changes a WCET, the
  other fields are repaired so that only the intended requirement is broken:
  WCET, core SILs and FCR use follow the change, and every reported period is
  set to ceil(MCR) of the mutated deployed MSAG (computed with the verifier's
  own graph builder, so the period check cannot catch the mutant by accident;
  the hardest case for the verifier). Costs are left as they were.
* `model`: model mutants. A copy of safedse's model/ and lib/ (under
  results/tmp/mutants/, never in safedse) with one constraint family disabled
  by `where false`; each mutant is solved (TOTALCOST, 4 workers), pinned with
  the mutant model, and verified.
* `generator`: the generator's own refusal conditions on each base solution
  (catalog mismatch, contradicting fault model) and mutations of the
  generated argument checked by gsn.audit() (the argument invariant, SafeDSE
  docs/design.md#safety-argument-generation).

Ground truth for every solution and model mutant is the unmodified model
(wrappers/fix_all.mzn: all decisions, activation, WCETs and periods fixed):
a mutant has *manifested* if the model rejects its solution. The verifier is
then scored on manifested mutants only; a mutant the model accepts is
equivalent. Every verifier verdict is followed by gsn.py on the same report
(no --allow-unverified): it must refuse (exit 2) when the verifier rejected.

    python3 mutation.py base
    python3 mutation.py solution model generator
    python3 mutation.py solution --verifiers ext
    python3 mutation.py solution model_reverify generator --verifiers artifact

Verifier tags in e6_mut.csv: "safedse" is SafeDSE's verifier before the
extension (a copy in verifier_v1/, SafeDSE commit b830db7), "ext" the
extension (verifier_ext/), "artifact" SafeDSE's current tools/verify.py,
which contains the extension (commit 1c23d7c onward).
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import harness as H
from exact_period import FIELDS, pin_period  # noqa: F401

SAFEDSE = H.SAFEDSE
sys.path.insert(0, str(SAFEDSE / "tools"))
import gsn as G                       # noqa: E402
import verify as V                    # noqa: E402
from golden import mcm                # noqa: E402
from patterns import load_patterns    # noqa: E402
from solve import run as solve_run    # noqa: E402

ROOT = H.ROOT
MUT = H.RESULTS / "mut"
BASE = MUT / "base"
CSV = H.RESULTS / "e6_mut.csv"
TMP = H.TMP / "mut"
MUTANT_DIR = H.TMP / "mutants"
VERIFY = SAFEDSE / "tools" / "verify.py"
# Verifier tags: see the module docstring.
VERIFIERS = {"safedse": ROOT / "verifier_v1" / "verify.py",
             "ext": ROOT / "verifier_ext" / "verify.py",
             "artifact": VERIFY}
GSN = SAFEDSE / "tools" / "gsn.py"
CAT = SAFEDSE / "data" / "patterns.yaml"
CAT_NVP = SAFEDSE / "data" / "patterns_explicit_voter_demo.yaml"

SEED = 20261002          # mutant sampling; the base solutions are stored
PER_OP = 5               # mutants sampled per (instance, operator)
MODEL_TIME_LIMIT_MS = 120_000
CHECK_TIME_LIMIT_MS = 60_000
FORBIDDEN = 1_000_000    # wcet sentinel of the front end

# the nine RQ2 pattern instances (Table III) and the three-application one
INSTANCES = {s: (SAFEDSE / "out" / f"{s}.dzn", CAT)
             for s in ["c_pat", "d_random_hw", "d_systematic_sw", "f_both3",
                       "f_random_hw", "f_sw3", "p_rasta", "pc_sobel"]}
INSTANCES["v_nvp"] = (SAFEDSE / "out" / "v_nvp.dzn", CAT_NVP)
INSTANCES["m_3app"] = (ROOT / "multi" / "m_3app.dzn", CAT)

FIELDNAMES = ["part", "verifier", "instance", "operator", "mutant", "detail",
              "intended", "model_status", "manifested", "solve_status",
              "solve_seconds", "verify_ok", "verify_kinds", "intended_hit",
              "gsn_exit", "ext_hit", "note"]
KEY = ["part", "verifier", "instance", "operator", "mutant"]


# ---------------------------------------------------------------------------
# helpers over the parsed instance
# ---------------------------------------------------------------------------
def L(x):
    return x if isinstance(x, list) else [x]


def grid(flat, ncol):
    flat = L(flat)
    if flat and isinstance(flat[0], list):
        return flat
    return [flat[r * ncol:(r + 1) * ncol] for r in range(len(flat) // ncol)]


class Inst:
    def __init__(self, name: str):
        self.name = name
        self.dzn, self.cat = INSTANCES[name]
        d = self.d = V.parse_dzn(str(self.dzn))
        self.n = d["n"]
        self.P = d["P"]
        self.nCT = d["nCoreTypes"]
        self.nMD = d["nModes"]
        self.comm = [bool(x) for x in L(d.get("comm_actor", [False] * self.n))]
        self.comm += [False] * (self.n - len(self.comm))
        self.ctype = L(d["ctype"])
        self.fcr = L(d["fcr"])
        self.max_sil = L(d["max_sil"])
        self.partitionable = L(d["partitionable"])
        self.sreq = L(d["sil_req_parent"])
        self.parent = L(d["parent"])
        self.npat = d.get("nPat", 0)
        self.pat_name = L(d.get("pat_name", []))
        self.par_owner = L(d.get("par_owner", []))
        self.node_owner = L(d.get("node_owner", []))
        self.node_guard = grid(d.get("node_guard", []), max(self.npat, 1))
        self.pat_allowed = grid(d.get("pat_allowed", []), max(self.npat, 1))
        self.parent_name = L(d["parent_name"])
        self.exempt = d.get("comm_sil_mode", 0) == 0

    def wcet(self, i: int, p: int, mode: int) -> int:
        """i node (0-based), p core (1-based), mode (1-based)."""
        t = self.ctype[p - 1]
        return L(self.d["wcet"])[(i * self.nCT + t - 1) * self.nMD + mode - 1]

    def req(self, i: int) -> int:
        return self.sreq[self.parent[i] - 1]


def chains(I: Inst, sol: dict) -> dict[int, list[int]]:
    """core -> static order (0-based node ids, all CPU nodes incl. inactive)."""
    succ, proc = sol["succ"], sol["proc"]
    cpu = [i for i in range(I.n) if not I.comm[i]]
    nxt = {i: succ[i] - 1 for i in cpu if succ[i] > 0}
    heads = [i for i in cpu if i not in set(nxt.values())]
    out = {}
    for h in heads:
        ch, x = [], h
        while x is not None:
            ch.append(x)
            x = nxt.get(x)
        out[proc[h]] = ch
    return out


def write_chains(I: Inst, sol: dict, ch: dict[int, list[int]]) -> None:
    succ = [0] * I.n
    for c in ch.values():
        for u, v in zip(c, c[1:]):
            succ[u] = v + 1
    sol["succ"] = succ


def consistent_mu(I: Inst, sol: dict) -> bool:
    """Set every reported period to ceil(MCR) of its MSAG component, as the
    verifier will compute it. False if the mutated MSAG deadlocks."""
    n, edges, T, live = V.build_msag(I.d, sol)
    le = [(u, v, t) for u, v, t in edges if u in live and v in live]
    app = L(I.d.get("app", [1] * n))
    mus = list(L(sol["mu"]))
    for c in V._components(n, le):
        c = sorted(c & live)
        if not c or all(I.comm[i] for i in c):
            continue
        idx = {g: k for k, g in enumerate(c)}
        k = mcm(len(c), [(idx[u], idx[v], t) for u, v, t in le
                         if u in idx and v in idx], [T[g] for g in c])
        if k is None:
            return False
        for z in {app[g] for g in c if not I.comm[g]}:
            mus[z - 1] = math.ceil(k)
    sol["mu"] = mus if isinstance(sol["mu"], list) else mus[0]
    return True


def hosted(I: Inst, sol: dict, p: int) -> list[int]:
    """active nodes on core p that the one-SIL-per-core rule covers"""
    return [i for i in range(I.n) if sol["proc"][i] == p and sol["active"][i]
            and not (I.exempt and I.comm[i])]


def refresh_core(I: Inst, sol: dict, p: int) -> None:
    """csil of core p = the highest SIL it hosts (0 if nothing active)."""
    on = hosted(I, sol, p)
    sol["csil"][p - 1] = max((sol["sil_impl"][i] for i in on), default=0)
    if not on:
        sol["partition"][p - 1] = False


def level_core(I: Inst, sol: dict, p: int) -> bool:
    """Without partitioning, raise everything on core p to its highest SIL
    (promotion is allowed). False if that exceeds the type ceiling."""
    on = hosted(I, sol, p)
    top = max((sol["sil_impl"][i] for i in on), default=0)
    if top > I.max_sil[I.ctype[p - 1] - 1]:
        return False
    if not sol["partition"][p - 1]:
        for i in on:
            sol["sil_impl"][i] = top
    sol["csil"][p - 1] = top
    return True


def set_wcet(I: Inst, sol: dict, i: int) -> bool:
    p = sol["proc"][i]
    w = I.wcet(i, p, sol["pmode"][p - 1])
    if w >= FORBIDDEN:
        return False
    sol["T"][i] = w if sol["active"][i] else 0
    return True


def move(I: Inst, sol: dict, v: int, p: int) -> bool:
    """Rebind node v to core p (end of p's static order), repair WCET, core
    SILs and FCR use. False if the move needs a forbidden binding or breaks
    a type ceiling (the mutant would then break more than intended)."""
    old = sol["proc"][v]
    ch = chains(I, sol)
    ch[old] = [x for x in ch[old] if x != v]
    if not ch[old]:
        del ch[old]
    ch.setdefault(p, []).append(v)
    sol["proc"][v] = p
    write_chains(I, sol, ch)
    if not set_wcet(I, sol, v):
        return False
    sol["fcr_used"][I.fcr[p - 1] - 1] = True
    refresh_core(I, sol, old)
    if sol["partition"][old - 1] is False:
        level_core(I, sol, old)
    return level_core(I, sol, p)


def repair_costs(I: Inst, sol: dict) -> None:
    """Costs as the model derives them from the (mutated) decisions, so that
    a mutant breaks only its intended requirement (lib/platform.mzn,
    lib/safety.mzn, lib/activation.mzn)."""
    d, n, P = I.d, I.n, I.P
    proc, act = sol["proc"], sol["active"]
    used = [any(proc[i] == p + 1 and act[i] for i in range(n)) for p in range(P)]
    price = grid(d["core_price"], I.nMD)
    hw = sum(L(d["fcr_price"])[f] for f, u in enumerate(sol["fcr_used"]) if u) \
        + sum(price[I.ctype[p] - 1][sol["pmode"][p] - 1]
              for p in range(P) if used[p])
    dk = d["dev_k"]
    dk = [int(x) for x in dk[dk.index("[") + 1:dk.rindex("]")].split(",")] \
        if isinstance(dk, str) else L(dk)
    dev = sum(L(d["dev_base"])[i] * dk[sol["sil_impl"][i]]
              for i in range(n) if act[i]) // 100
    part = sum(L(d["partition_cost"])[I.ctype[p] - 1]
               for p in range(P) if sol["partition"][p])
    patc = 0
    if I.npat > 1:
        patc = sum(L(d["pat_recurring"])[sol["pat"][a] - 1]
                   for a in range(len(I.par_owner)) if I.par_owner[a] == a + 1
                   and not any(I.parent[i] == a + 1 and I.comm[i]
                               for i in range(n)))
    sol.update(hw_cost=hw, dev_cost=dev, partition_total=part,
               pattern_cost=patc, total_cost=hw + dev + part + patc)
    if isinstance(sol.get("metric"), list):
        sol["metric"] = list(sol["metric"])
        sol["metric"][2:5] = [hw, dev, sol["total_cost"]]


COSTS = ["hw_cost", "dev_cost", "partition_total", "pattern_cost",
         "total_cost"]


def used_cores(I: Inst, sol: dict) -> list[int]:
    return sorted({sol["proc"][i] for i in range(I.n)
                   if sol["active"][i] and not I.comm[i]})


def active_relations(I: Inst, sol: dict) -> list[tuple[int, int, int, int]]:
    d = I.d
    if not d.get("nPL", 0):
        return []
    g = grid(d["pl_guard"], I.npat)
    out = []
    for c in range(d["nPL"]):
        owner = L(d["pl_owner"])[c]
        if g[c][sol["pat"][owner - 1] - 1]:
            out.append((L(d["pl_u"])[c] - 1, L(d["pl_v"])[c] - 1,
                        L(d["pl_rel"])[c], owner))
    return out


# ---------------------------------------------------------------------------
# solution mutation operators: each yields (detail, intended, mutant solution)
# ---------------------------------------------------------------------------
REL = {1: "SAME", 2: "DIFFERENT", 3: "DIFFERENT_FCR", 4: "DIVERSE"}


def op_place(I, s):
    for u, v, rel, _ in active_relations(I, s):
        pu = s["proc"][u]
        used = used_cores(I, s)
        if rel == 1:
            cand = [p for p in used if p != pu]
        elif rel == 2:
            cand = [pu]
        elif rel == 3:
            same = [p for p in range(1, I.P + 1)
                    if I.fcr[p - 1] == I.fcr[pu - 1] and p != pu]
            cand = sorted(same, key=lambda p: p not in used) or [pu]
        else:
            same = [p for p in range(1, I.P + 1)
                    if I.ctype[p - 1] == I.ctype[pu - 1] and p != pu]
            cand = sorted(same, key=lambda p: p not in used) or [pu]
        for p in cand[:1]:
            m = copy.deepcopy(s)
            if move(I, m, v, p) and consistent_mu(I, m):
                yield (f"{REL[rel]} node {v+1} -> core {p} (node {u+1} on "
                       f"{pu})", "placement", m)


def op_sil_node(I, s):
    for i in range(I.n):
        if s["active"][i] and not (I.exempt and I.comm[i]) \
                and I.req(i) >= 1 and s["sil_impl"][i] == I.req(i):
            m = copy.deepcopy(s)
            m["sil_impl"][i] -= 1
            yield (f"node {i+1} SIL {I.req(i)} -> {I.req(i)-1}", "sil_actor", m)


def op_sil_core(I, s):
    for p in used_cores(I, s):
        on = hosted(I, s, p)
        if s["csil"][p - 1] >= 1 and any(I.req(i) == s["csil"][p - 1]
                                         for i in on):
            m = copy.deepcopy(s)
            m["csil"][p - 1] -= 1
            for i in on:
                m["sil_impl"][i] = min(m["sil_impl"][i], m["csil"][p - 1])
            yield (f"core {p} and its nodes SIL {s['csil'][p-1]} -> "
                   f"{m['csil'][p-1]}", "sil_actor", m)


def op_ceiling(I, s):
    for p in used_cores(I, s):
        cap = I.max_sil[I.ctype[p - 1] - 1]
        if cap < 4 and not s["partition"][p - 1]:
            m = copy.deepcopy(s)
            m["csil"][p - 1] = cap + 1
            for i in hosted(I, s, p):
                m["sil_impl"][i] = cap + 1
            yield (f"core {p} (type ceiling {cap}) provisioned to {cap+1}",
                   "isolation", m)


def op_mix(I, s):
    """one node on an unpartitioned core promoted above the others"""
    for p in used_cores(I, s):
        on = hosted(I, s, p)
        cap = I.max_sil[I.ctype[p - 1] - 1]
        if len(on) >= 2 and not s["partition"][p - 1] and s["csil"][p - 1] < cap:
            m = copy.deepcopy(s)
            m["sil_impl"][on[0]] += 1
            m["csil"][p - 1] += 1
            yield (f"core {p}: node {on[0]+1} promoted to {m['csil'][p-1]}, "
                   f"others at {s['csil'][p-1]}, no partitioning", "isolation", m)


def op_part_forge(I, s):
    """partitioning claimed on a core type that cannot provide it, used to
    host mixed SILs"""
    for p in used_cores(I, s):
        on = hosted(I, s, p)
        cap = I.max_sil[I.ctype[p - 1] - 1]
        if (len(on) >= 2 and not I.partitionable[I.ctype[p - 1] - 1]
                and s["csil"][p - 1] < cap):
            m = copy.deepcopy(s)
            m["partition"][p - 1] = True
            m["sil_impl"][on[0]] += 1
            m["csil"][p - 1] += 1
            yield (f"core {p}: partitioning on a non-partitionable type, "
                   f"node {on[0]+1} at SIL {m['csil'][p-1]}", "isolation", m)


def op_period(I, s):
    mus = L(s["mu"])
    for z in range(len(mus)):
        m = copy.deepcopy(s)
        mm = list(mus)
        mm[z] -= 1
        m["mu"] = mm if isinstance(s["mu"], list) else mm[0]
        yield (f"app {z+1} period {mus[z]} -> {mm[z]}", "period", m)


def op_wcet(I, s):
    for i in range(I.n):
        if s["active"][i] and not I.comm[i] and s["T"][i] >= 2:
            m = copy.deepcopy(s)
            m["T"][i] = s["T"][i] // 2
            if consistent_mu(I, m):
                yield (f"node {i+1} WCET {s['T'][i]} -> {m['T'][i]}", "wcet", m)


def _live_chain(I, s, c):
    return [x for x in c if s["active"][x]]


def op_order_split(I, s):
    for p, c in chains(I, s).items():
        live = _live_chain(I, s, c)
        if len(live) >= 2:
            m = copy.deepcopy(s)
            k = c.index(live[0])
            m["succ"][c[k]] = 0            # cut after the first live node
            if consistent_mu(I, m):
                yield (f"core {p}: static order cut after node {c[k]+1}",
                       "order", m)


def op_order_cycle(I, s):
    for p, c in chains(I, s).items():
        if len(_live_chain(I, s, c)) >= 2:
            m = copy.deepcopy(s)
            m["succ"][c[-1]] = c[0] + 1    # no head: a cyclic order
            if consistent_mu(I, m):
                yield (f"core {p}: static order closed into a cycle", "order", m)


def op_order_cross(I, s):
    ch = chains(I, s)
    cores = [p for p in used_cores(I, s) if p in ch]
    for a, b in zip(cores, cores[1:]):
        m = copy.deepcopy(s)
        m["succ"][ch[a][-1]] = ch[b][0] + 1   # one order across two cores
        if consistent_mu(I, m):
            yield (f"static order of core {a} continues on core {b}", "order", m)


def _is_component(I, i):
    return I.npat > 1 and I.node_owner and \
        I.parent[i] != I.node_owner[i] and not I.comm[i]


def op_deactivate(I, s):
    for i in range(I.n):
        if s["active"][i] and _is_component(I, i):
            m = copy.deepcopy(s)
            m["active"][i] = False
            m["T"][i] = 0
            m["sil_impl"][i] = 0
            refresh_core(I, m, s["proc"][i])
            if consistent_mu(I, m):
                yield (f"component node {i+1} ({I.d['node_name'][i]}) "
                       f"deactivated", "activation", m)


def op_pattern_none(I, s):
    if I.npat <= 1 or "none" not in I.pat_name:
        return
    none = I.pat_name.index("none") + 1
    for k, lbl in enumerate(I.parent_name):
        if I.par_owner[k] != k + 1 or lbl.startswith("__comm"):
            continue
        if I.sreq[k] < 2 or s["pat"][k] == none:
            continue
        m = copy.deepcopy(s)
        for a in range(len(m["pat"])):
            if I.par_owner[a] == k + 1:
                m["pat"][a] = none
        cores = set()
        for i in range(I.n):
            if I.node_owner[i] == k + 1 and I.parent[i] != k + 1:
                m["active"][i] = False
                m["T"][i] = 0
                m["sil_impl"][i] = 0
                cores.add(s["proc"][i])
        for p in cores:
            refresh_core(I, m, p)
        if consistent_mu(I, m):
            yield (f"actor {lbl} (SIL {I.sreq[k]}) without pattern", "activation",
                   m)


def op_drop(I, s):
    for f in ["pat", "sil_impl"]:
        if f in s and (f != "pat" or I.npat > 1):
            m = copy.deepcopy(s)
            del m[f]
            yield (f"field {f} missing from the solution file", "input", m)


def op_cost(I, s):
    m = copy.deepcopy(s)
    m["total_cost"] = s["total_cost"] - 1
    m["metric"] = list(s["metric"])
    m["metric"][4] = m["total_cost"]
    yield (f"total cost {s['total_cost']} -> {m['total_cost']}", "cost", m)


OPS = {"place": op_place, "sil_node": op_sil_node, "sil_core": op_sil_core,
       "ceiling": op_ceiling, "mix": op_mix, "part_forge": op_part_forge,
       "period": op_period, "wcet": op_wcet, "order_split": op_order_split,
       "order_cycle": op_order_cycle, "order_cross": op_order_cross,
       "deactivate": op_deactivate, "pattern_none": op_pattern_none,
       "drop_field": op_drop, "cost": op_cost}


# ---------------------------------------------------------------------------
# verifier, generator, model check
# ---------------------------------------------------------------------------
def classify(msgs: list[str]) -> list[str]:
    kinds = set()
    for m in msgs:
        if m.startswith("NOTE"):
            continue
        if "period mismatch" in m:
            kinds.add("period")
        elif "deadlocks" in m:
            kinds.add("deadlock")
        elif "oracle disagreement" in m:
            kinds.add("oracle")
        elif "developed to SIL" in m:
            kinds.add("sil_actor")
        elif "provisioned only to SIL" in m:
            kinds.add("sil_core")
        elif "type can only be certified" in m or "mixed SILs" in m \
                or "csil=" in m:
            kinds.add("isolation")
        elif m.startswith("placement "):
            kinds.add("placement")
        elif "busiest core" in m:
            kinds.add("load")
        elif "solution lacks" in m:                  # the extension
            kinds.add("input")
        elif "WCET" in m or "cannot run on" in m:
            kinds.add("wcet")
        elif "static order" in m or "predecessors in the static" in m \
                or "static-order successor" in m:
            kinds.add("order")
        elif "partitioning claimed" in m:
            kinds.add("isolation")
        elif "not admissible" in m or "selected pattern" in m \
                or "pattern differs" in m or "inactive node in" in m:
            kinds.add("activation")
        elif "recomputed" in m or "fault containment region is not" in m:
            kinds.add("cost")
        elif "needs memory" in m:
            kinds.add("memory")
        elif m.startswith("crash"):
            kinds.add("crash")
        else:
            kinds.add(m.split(":")[0].split(" ")[0].lower() or "other")
    return sorted(kinds)


INTENDED_KINDS = {"placement": {"placement"},
                  "sil_actor": {"sil_actor", "sil_core"},
                  "isolation": {"isolation"},
                  "period": {"period"},
                  "wcet": {"wcet"}, "order": {"order"},
                  "activation": {"activation"}, "input": {"input"},
                  "cost": {"cost"}}


def verify(I: Inst, sol: dict, tag: str, verifier: Path) -> dict:
    TMP.mkdir(parents=True, exist_ok=True)
    solp, repp = TMP / f"{tag}.sol.json", TMP / f"{tag}.report.json"
    solp.write_text(json.dumps(sol))
    repp.unlink(missing_ok=True)
    p = subprocess.run([sys.executable, str(verifier), "--dzn", str(I.dzn),
                        "--solution", str(solp), "--quiet", "--json-report",
                        str(repp)], capture_output=True, text=True)
    if repp.exists():
        r = json.loads(repp.read_text())
    else:
        r = {"ok": False, "checks": [],
             "messages": [f"crash: {p.stderr.strip().splitlines()[-1:]}"]}
    r["_sol"], r["_rep"] = solp, repp
    return r


def generate(I: Inst, rep: dict, tag: str, extra: list[str] | None = None,
             cat: Path | None = None) -> int:
    if not rep["_rep"].exists():         # verifier crashed: no report
        rep["_rep"].write_text(json.dumps({"ok": False, "messages":
                                           rep["messages"], "checks": []}))
    p = subprocess.run([sys.executable, str(GSN), "--dzn", str(I.dzn),
                        "--solution", str(rep["_sol"]), "--report",
                        str(rep["_rep"]), "--out", str(TMP / tag),
                        "--patterns", str(cat or I.cat), "--quiet"]
                       + (extra or []), capture_output=True, text=True)
    return p.returncode


def _fix_all_wrapper(model: Path) -> Path:
    fd = (ROOT / "wrappers" / "fix_design.mzn").read_text().replace(
        'include "dse.mzn";', f'include "{model}";')
    fa = (ROOT / "wrappers" / "fix_all.mzn").read_text().replace(
        'include "fix_design.mzn";', "")
    w = H.TMP / f"fix_all_{abs(hash(str(model))) % 10**8}.mzn"
    w.write_text(fd + "\n" + fa)
    return w


def model_accepts(I: Inst, sol: dict, model: Path | None = None) -> str:
    """Status of the unmodified model with the whole solution fixed:
    OPTIMAL = accepted, UNSAT = rejected."""
    need = FIELDS + ["active", "T", "mu"]
    if any(k not in sol for k in need):
        return "N/A"
    mz = lambda v: "[" + ", ".join(("true" if x else "false")  # noqa: E731
                                   if isinstance(x, bool) else str(x)
                                   for x in L(v)) + "]"
    extra = " ".join(f"fx_{k} = {mz(sol[k])};" for k in need)
    r = solve_run(str(I.dzn), "THROUGHPUT", threads=1,
                  time_limit_ms=CHECK_TIME_LIMIT_MS,
                  timeout=CHECK_TIME_LIMIT_MS // 1000 + 60,
                  model=str(_fix_all_wrapper(model or SAFEDSE / "model"
                                             / "dse.mzn")),
                  extra=extra)
    if r["status"] == "OPTIMAL" and any(
            k in sol and r["solution"].get(k) != sol[k] for k in COSTS):
        return "COSTS"          # accepted, but the reported costs are wrong
    return r["status"]


# ---------------------------------------------------------------------------
# parts
# ---------------------------------------------------------------------------
def base() -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    for name in INSTANCES:
        out = BASE / f"{name}.sol.json"
        if out.exists():
            continue
        I = Inst(name)
        row = H.solve_and_verify(I.dzn, "TOTALCOST", tag=f"e6_base_{name}")
        if row["status"] != "OPTIMAL" or row["verify_ok"] is not True:
            raise SystemExit(f"{name}: base solve {row['status']} "
                             f"verify_ok={row['verify_ok']}")
        if model_accepts(I, row["_solution"]) != "OPTIMAL":
            raise SystemExit(f"{name}: base solution not accepted by fix_all")
        out.write_text(json.dumps(row["_solution"]))
        print(f"base {name}: total_cost={row['total_cost'] if 'total_cost' in row else row['_solution']['total_cost']} "
              f"mu={row['mu_list']}", flush=True)


def _record(row: dict) -> None:
    H.append_row(CSV, FIELDNAMES, row)


def solution(verifiers: dict[str, Path], model_check: bool = True,
             only: list[str] | None = None) -> None:
    done = H.existing_keys(CSV, KEY)
    for name in INSTANCES:
        I = Inst(name)
        s0 = json.loads((BASE / f"{name}.sol.json").read_text())
        for opn, op in OPS.items():
            if only and opn not in only:
                continue
            cands, seen = [], set()        # distinct mutated solutions only
            for c in op(I, s0):
                h = json.dumps(c[2], sort_keys=True)
                if h not in seen:
                    seen.add(h)
                    cands.append(c)
            rng = random.Random(f"{SEED}/{name}/{opn}")
            pick = sorted(rng.sample(range(len(cands)),
                                     min(PER_OP, len(cands))))
            for k in pick:
                detail, intended, m = cands[k]
                if opn not in ("cost", "drop_field"):
                    repair_costs(I, m)
                todo = [v for v in verifiers
                        if ("solution", v, name, opn, str(k)) not in done]
                if not todo:
                    continue
                ms = model_accepts(I, m) if model_check else ""
                for vtag in todo:
                    tag = f"s_{vtag}_{name}_{opn}_{k}"
                    rep = verify(I, m, tag, verifiers[vtag])
                    kinds = classify(rep["messages"])
                    ik = INTENDED_KINDS.get(intended)
                    row = dict(zip(KEY, ("solution", vtag, name, opn, str(k))),
                               detail=detail, intended=intended,
                               model_status=ms,
                               manifested={"UNSAT": True, "COSTS": True, "OPTIMAL": False}.get(
                                   ms, "by construction" if ms == "N/A"
                                   else ""),
                               verify_ok=rep["ok"],
                               verify_kinds=";".join(kinds),
                               intended_hit=(bool(ik & set(kinds)) if ik
                                             else ""),
                               gsn_exit=generate(I, rep, tag))
                    _record(row)
                    print(f"{name:15s} {opn:12s} {k:3d} {vtag:7s} model={ms:8s}"
                          f" verify={rep['ok']!s:5s} {row['verify_kinds']:20s} "
                          f"gsn={row['gsn_exit']}  {detail}", flush=True)


# ---- model mutants ---------------------------------------------------------
WF = "where false"
MODEL_MUTANTS = {
    # name: (file, old, new, applicability)
    "placement": ("lib/activation.mzn",
                  "constraint forall(c in PL)( pl_on[c] -> (",
                  f"constraint forall(c in PL {WF})( pl_on[c] -> (", "pat"),
    "sil_required": ("lib/safety.mzn",
                     "constraint forall(i in A)( active[i] -> sil_impl[i] >= "
                     "sil_req_parent[parent[i]] );",
                     f"constraint forall(i in A {WF})( active[i] -> sil_impl[i]"
                     f" >= sil_req_parent[parent[i]] );", ""),
    "sil_core": ("lib/safety.mzn",
                 "constraint forall(i in A)( active[i] -> sil_impl[i] <= "
                 "csil[proc[i]] );",
                 f"constraint forall(i in A {WF})( active[i] -> sil_impl[i] <= "
                 f"csil[proc[i]] );", ""),
    "type_ceiling": ("lib/safety.mzn",
                     "constraint forall(p in PR)( csil[p] <= max_sil[ctype[p]] );",
                     f"constraint forall(p in PR {WF})( csil[p] <= "
                     f"max_sil[ctype[p]] );", ""),
    "one_sil_per_core": ("lib/safety.mzn",
                         "constraint forall(i in A)(\n    active[i] /\\ not "
                         "partition[proc[i]]",
                         f"constraint forall(i in A {WF})(\n    active[i] /\\ not"
                         f" partition[proc[i]]", ""),
    "partition_capability": ("lib/safety.mzn",
                             "constraint forall(p in PR)( partition[p] -> "
                             "partitionable[ctype[p]] );",
                             f"constraint forall(p in PR {WF})( partition[p] -> "
                             f"partitionable[ctype[p]] );", ""),
    "wcet_binding": ("model/dse.mzn",
                     "constraint forall(i in A where not (i in COMM))(\n    T[i]",
                     "constraint forall(i in A where not (i in COMM) /\\ false)("
                     "\n    T[i]", ""),
    "channel_edges": ("lib/mcm.mzn",
                      "forall (i, j in A where tok[i,j] >= 0) (",
                      "forall (i, j in A where tok[i,j] >= 0 /\\ false) (", ""),
    "order_edges": ("lib/mcm.mzn",
                    "forall (i, j in A where i != j) (\n        sedge[i,j]",
                    "forall (i, j in A where i != j /\\ false) (\n        "
                    "sedge[i,j]", ""),
    "wrap_edges": ("lib/mcm.mzn",
                   "forall (i, j in A) (\n        wrap[i,j]",
                   f"forall (i, j in A {WF}) (\n        wrap[i,j]", ""),
    "core_load": ("model/dse.mzn",
                  "constraint forall(i in A, p in PR)( proc[i] = p -> "
                  "mu[app[i]] >= proc_load[p] );",
                  f"constraint forall(i in A, p in PR {WF})( proc[i] = p -> "
                  f"mu[app[i]] >= proc_load[p] );", ""),
    "shared_period": ("model/dse.mzn",
                      "constraint forall(y, z in APP where y < z)(\n    "
                      "app_shares[y, z] -> mu[y] = mu[z]",
                      "constraint forall(y, z in APP where y < z /\\ false)(\n"
                      "    app_shares[y, z] -> mu[y] = mu[z]", "multi"),
    "comm_path": ("model/dse.mzn",
                  "constraint comm_msag_edges(T, pot, mu_of);",
                  "% mutant: comm_msag_edges removed", "comm"),
    "activation": ("lib/activation.mzn",
                   "constraint forall(i in A)( active[i] <-> "
                   "node_guard[i, pat[node_owner[i]]] );",
                   f"constraint forall(i in A {WF})( active[i] <-> "
                   f"node_guard[i, pat[node_owner[i]]] );", "pat"),
    "admissible_pattern": ("lib/activation.mzn",
                           "constraint forall(a in PAR)( pat_allowed[a, pat[a]] );",
                           f"constraint forall(a in PAR {WF})( "
                           f"pat_allowed[a, pat[a]] );", "pat"),
    "one_order_per_core": ("lib/order.mzn",
                           "constraint forall(p in PR)( used[p] -> sum(i in "
                           "CPU_A)(proc[i] = p /\\ succ[i] = 0) = 1 );",
                           f"constraint forall(p in PR {WF})( used[p] -> sum(i "
                           f"in CPU_A)(proc[i] = p /\\ succ[i] = 0) = 1 );", ""),
    "order_same_core": ("lib/order.mzn",
                        "constraint forall(i in CPU_A)( succ[i] > 0 -> "
                        "proc[succ[i]] = proc[i] );",
                        f"constraint forall(i in CPU_A {WF})( succ[i] > 0 -> "
                        f"proc[succ[i]] = proc[i] );", ""),
    "memory": ("model/dse.mzn",
               "constraint forall(p in PR)(\n    sum(i in A)( bool2int(proc[i] = "
               "p /\\ active[i]) * mem_req[i] )",
               f"constraint forall(p in PR {WF})(\n    sum(i in A)( "
               f"bool2int(proc[i] = p /\\ active[i]) * mem_req[i] )", ""),
}


def make_mutant(name: str) -> Path:
    f, old, new, _ = MODEL_MUTANTS[name]
    dst = MUTANT_DIR / name
    shutil.rmtree(dst, ignore_errors=True)
    for sub in ["model", "lib"]:
        shutil.copytree(SAFEDSE / sub, dst / sub)
    txt = (dst / f).read_text()
    if txt.count(old) != 1:
        raise SystemExit(f"mutant {name}: pattern found {txt.count(old)} times "
                         f"in {f}")
    (dst / f).write_text(txt.replace(old, new))
    return dst / "model" / "dse.mzn"


def applicable(I: Inst, need: str) -> bool:
    if need == "pat":
        return I.npat > 1
    if need == "multi":
        return I.d.get("nApps", 1) > 1
    if need == "comm":
        return I.d.get("nCh", 0) > 0
    return True


def pin_with(I: Inst, sol: dict, model: Path) -> dict:
    """exact_period.pin_period with the mutant model in the wrapper"""
    w = H.TMP / f"fix_design_{model.parent.parent.name}.mzn"
    w.write_text((ROOT / "wrappers" / "fix_design.mzn").read_text().replace(
        'include "dse.mzn";', f'include "{model}";'))
    mz = lambda v: "[" + ", ".join(("true" if x else "false")  # noqa: E731
                                   if isinstance(x, bool) else str(x)
                                   for x in L(v)) + "]"
    extra = " ".join(f"fx_{k} = {mz(sol[k])};" for k in FIELDS)
    r = solve_run(str(I.dzn), "THROUGHPUT", threads=1,
                  time_limit_ms=CHECK_TIME_LIMIT_MS,
                  timeout=CHECK_TIME_LIMIT_MS // 1000 + 60, model=str(w),
                  extra=extra)
    return r


def model(verifiers: dict[str, Path], only: list[str] | None = None) -> None:
    done = H.existing_keys(CSV, KEY)
    for mname, (_, _, _, need) in MODEL_MUTANTS.items():
        if only and mname not in only:
            continue
        mzn = None
        for name in INSTANCES:
            I = Inst(name)
            if not applicable(I, need):
                continue
            todo = [v for v in verifiers
                    if ("model", v, name, mname, "0") not in done]
            if not todo:
                continue
            mzn = mzn or make_mutant(mname)
            r = solve_run(str(I.dzn), "TOTALCOST", threads=H.THREADS,
                          time_limit_ms=MODEL_TIME_LIMIT_MS,
                          timeout=MODEL_TIME_LIMIT_MS // 1000 + 60,
                          model=str(mzn))
            row0 = dict(detail=MODEL_MUTANTS[mname][0], intended=mname,
                        solve_status=r["status"],
                        solve_seconds=round(r.get("seconds", 0), 2))
            if "solution" not in r:
                for vtag in todo:
                    _record(dict(zip(KEY, ("model", vtag, name, mname, "0")),
                                 **row0, note="no solution"))
                print(f"{name:15s} {mname:22s} {r['status']}", flush=True)
                continue
            s = r["solution"]
            pin = pin_with(I, s, mzn)
            if pin.get("solution") is not None:
                s = pin["solution"]
            else:
                row0["note"] = f"pin {pin['status']}"
            ms = model_accepts(I, s)
            (MUT / "model").mkdir(parents=True, exist_ok=True)
            (MUT / "model" / f"{name}_{mname}.sol.json").write_text(
                json.dumps(s))
            for vtag in todo:
                tag = f"m_{vtag}_{name}_{mname}"
                rep = verify(I, s, tag, verifiers[vtag])
                kinds = classify(rep["messages"])
                row = dict(zip(KEY, ("model", vtag, name, mname, "0")), **row0,
                           model_status=ms,
                           manifested={"UNSAT": True, "COSTS": True,
                                       "OPTIMAL": False}.get(ms, ""),
                           verify_ok=rep["ok"], verify_kinds=";".join(kinds),
                           gsn_exit=generate(I, rep, tag))
                _record(row)
                print(f"{name:15s} {mname:22s} {vtag:7s} {r['status']:8s} "
                      f"model={ms:8s} verify={rep['ok']!s:5s} "
                      f"{row['verify_kinds']:20s} gsn={row['gsn_exit']} "
                      f"cost={s.get('total_cost')}", flush=True)


# ---- generator refusal conditions and argument mutants ---------------------
def _arg(I: Inst, s: dict, rep: dict):
    pats = load_patterns(str(I.cat))
    import yaml
    tdoc = yaml.safe_load((SAFEDSE / "data" / "gsn_tactics.yaml").read_text())
    tactics = {t["name"]: t for t in tdoc["tactics"]}
    fm, _ = G.infer_fault_model(I.d, pats)
    return G.build(I.d, s, rep, pats, tactics, fm, I.name)


def _arg_mutants(a, nchecks: int):
    """(operator, detail, mutate(a)) over the argument a; each must make
    audit() non-empty if Definition 2 is enforced"""
    el = a.el
    sols = [e for e in el.values() if e.kind == "Solution"]
    undev = [e for e in el.values() if e.kind == "Goal" and e.undeveloped]
    strat = [e for e in el.values() if e.kind == "Strategy"]
    goals_with_sn = [e for e in el.values() if e.kind == "Goal"
                     and e.supported_by
                     and all(el[c].kind == "Solution" for c in e.supported_by)]
    for e in sols:
        yield "arg_no_evidence", e.id, lambda a, i=e.id: setattr(
            a.el[i], "evidence", [])
        yield "arg_bad_index", e.id, lambda a, i=e.id: setattr(
            a.el[i], "evidence", [nchecks + 7])
    for e in undev:
        yield "arg_unmarked", e.id, lambda a, i=e.id: setattr(
            a.el[i], "undeveloped", False)
        yield "arg_no_reason", e.id, lambda a, i=e.id: setattr(
            a.el[i], "note", "")
    for e in strat:
        yield "arg_empty_strategy", e.id, lambda a, i=e.id: setattr(
            a.el[i], "supported_by", [])
    for e in goals_with_sn:
        yield "arg_detached", e.id, lambda a, i=e.id: setattr(
            a.el[i], "supported_by", [])


def audit_ext(a, checks: list[dict]) -> list[str]:
    """audit() plus the condition Definition 2 states and audit() leaves out
    (the extension of audit()): every cited record exists in the check log
    and passed."""
    bad = a.audit()
    for e in a.el.values():
        if e.kind == "Solution":
            for j in e.evidence:
                if not (0 <= j < len(checks)) or not checks[j].get("ok"):
                    bad.append(f"{e.id} cites record {j}, which is not a "
                               f"passed record of the check log")
    return bad


def generator(vtag: str, verifier: Path) -> None:
    done = H.existing_keys(CSV, KEY)
    for name in INSTANCES:
        I = Inst(name)
        s0 = json.loads((BASE / f"{name}.sol.json").read_text())
        tag0 = f"g_{vtag}_{name}"
        rep = verify(I, s0, tag0, verifier)

        def rec(op, k, detail, code, expect, note=""):
            key = ("generator", vtag, name, op, str(k))
            if key in done:
                return
            _record(dict(zip(KEY, key), detail=detail, intended=f"exit {expect}",
                         verify_ok=rep["ok"], gsn_exit=code,
                         intended_hit=(code == expect), note=note))
            print(f"{name:15s} {op:20s} {k:3} exit={code} (want {expect}) "
                  f"{detail}", flush=True)

        rec("control", 0, "verified base solution", generate(I, rep, tag0), 0)
        if I.npat <= 1:
            continue
        # catalog mismatch: the catalog without the pattern of a SIL>=1 actor
        import yaml
        doc = yaml.safe_load(Path(I.cat).read_text())
        used = sorted({I.pat_name[s0["pat"][k] - 1]
                       for k in range(len(I.sreq)) if I.sreq[k] >= 1}
                      - {"none"})
        for k, pid in enumerate(used):
            d2 = copy.deepcopy(doc)
            key_list = "patterns" if "patterns" in d2 else None
            d2[key_list] = [p for p in d2[key_list] if p.get("id") != pid]
            cp = TMP / f"{tag0}_cat_{k}.yaml"
            cp.write_text(yaml.safe_dump(d2, sort_keys=False))
            rec("catalog_mismatch", k, f"catalog without {pid}",
                generate(I, rep, f"{tag0}_cat_{k}", cat=cp), 4)
        # a fault model that contradicts the one inferred from pat_allowed
        fm, how = G.infer_fault_model(I.d, load_patterns(str(I.cat)))
        for k, other in enumerate(sorted(set(G.FAULT_MODELS) - {fm})):
            rec("fault_model", k, f"--fault-model {other} (inferred: {fm}, "
                f"{how})", generate(I, rep, f"{tag0}_fm_{k}",
                                    extra=["--fault-model", other]), 2,
                note="" if fm else "inference ambiguous: flag accepted")
        # argument mutants against audit() (Definition 2)
        muts = list(_arg_mutants(_arg(I, s0, rep), len(rep["checks"])))
        by_op: dict[str, list] = {}
        for op, eid, f in muts:
            by_op.setdefault(op, []).append((eid, f))
        for op, lst in by_op.items():
            rng = random.Random(f"{SEED}/{name}/{op}")
            for k in sorted(rng.sample(range(len(lst)), min(PER_OP, len(lst)))):
                eid, f = lst[k]
                key = ("generator", vtag, name, op, str(k))
                if key in done:
                    continue
                a = _arg(I, s0, rep)
                f(a)
                # the current audit() takes the check log
                bad = a.audit(rep["checks"]) if vtag == "artifact" \
                    else a.audit()
                _record(dict(zip(KEY, key), detail=eid, intended="audit",
                             verify_ok=rep["ok"], intended_hit=bool(bad),
                             ext_hit=bool(audit_ext(a, rep["checks"])),
                             note=(bad[0][:120] if bad else "audit passed")))
                print(f"{name:15s} {op:20s} {k:3} {eid:6s} audit "
                      f"{'REFUSES' if bad else 'passes'}", flush=True)


def model_reverify(vtag: str, verifier: Path) -> None:
    """Model mutants for a verifier added later: the stored mutant solutions
    (results/mut/model/) are verified again, so the rows describe the same
    solutions as the "safedse" rows (re-solving with 4 workers would not
    reproduce them). Solve and model fields are copied from the "safedse" row
    of the same mutant."""
    done = H.existing_keys(CSV, KEY)
    rows = [r for r in csv.DictReader(CSV.open(newline=""))
            if r["part"] == "model" and r["verifier"] == "safedse"]
    for r in rows:
        key = ("model", vtag, r["instance"], r["operator"], r["mutant"])
        if key in done:
            continue
        row = {k: r[k] for k in FIELDNAMES if k not in
               ("verifier", "verify_ok", "verify_kinds", "gsn_exit",
                "intended_hit", "ext_hit")}
        row["verifier"] = vtag
        sp = MUT / "model" / f"{r['instance']}_{r['operator']}.sol.json"
        if r["note"] == "no solution":
            _record(row)
            continue
        I = Inst(r["instance"])
        tag = f"m_{vtag}_{r['instance']}_{r['operator']}"
        rep = verify(I, json.loads(sp.read_text()), tag, verifier)
        kinds = classify(rep["messages"])
        row.update(verify_ok=rep["ok"], verify_kinds=";".join(kinds),
                   gsn_exit=generate(I, rep, tag))
        _record(row)
        print(f"{r['instance']:15s} {r['operator']:22s} {vtag:8s} "
              f"model={r['model_status']:8s} verify={rep['ok']!s:5s} "
              f"{row['verify_kinds']:20s} gsn={row['gsn_exit']}", flush=True)


ORACLE_CSV = H.RESULTS / "e6_oracle.csv"
ORACLE_FIELDS = ["part", "instance", "operator", "mutant", "exact_status"]
EXACT_MODEL = ROOT / "exact" / "model" / "dse.mzn"


def oracle() -> None:
    """Second ground truth: the exact period variant B (exact/). The model's
    least period of a design can exceed the MCR of its deployment
    (patches/README.md), so the model alone can reject a correct deployment;
    variant B differs from the model only there. A mutant counts as
    manifested if variant B rejects its solution (make_macros.py)."""
    done = H.existing_keys(ORACLE_CSV, ORACLE_FIELDS[:4])
    rows = [r for r in csv.DictReader(CSV.open(newline=""))
            if r["verifier"] == "safedse" and r["part"] in ("solution", "model")
            and r["model_status"] not in ("", "N/A")]
    for r in rows:
        key = (r["part"], r["instance"], r["operator"], r["mutant"])
        if key in done:
            continue
        sp = (TMP / f"s_safedse_{r['instance']}_{r['operator']}_{r['mutant']}"
                    f".sol.json" if r["part"] == "solution" else
              MUT / "model" / f"{r['instance']}_{r['operator']}.sol.json")
        st = model_accepts(Inst(r["instance"]), json.loads(sp.read_text()),
                           EXACT_MODEL)
        H.append_row(ORACLE_CSV, ORACLE_FIELDS, dict(zip(ORACLE_FIELDS[:4], key),
                                                     exact_status=st))
        print(f"{key} model={r['model_status']} exact={st}", flush=True)


RECHECK_CSV = H.RESULTS / "e6_recheck.csv"
RECHECK_FIELDS = ["solution", "dzn", "safedse_ok", "ext_ok", "ext_kinds",
                  "ext_messages"]


def recheck() -> None:
    """False alarms of the extension: every solution the harness kept in
    results/tmp whose report says VERIFY OK and whose instance file still
    exists is verified again by both verifiers. Only rows the artifact's
    verifier still accepts count (instances rebuilt since then are skipped
    by that filter). Paths are recorded relative to this repository (the
    SafeDSE instances as safedse/out/...)."""
    def rel(p: Path) -> str:
        p = Path(p).resolve()
        for base, pre in ((SAFEDSE, "safedse/"), (ROOT, "")):
            if p.is_relative_to(base):
                return pre + str(p.relative_to(base))
        return str(p)

    done = H.existing_keys(RECHECK_CSV, ["solution"])
    for rp in sorted(H.TMP.glob("*.report.json")):
        sp = rp.with_name(rp.name.replace(".report.json", ".sol.json"))
        if (rel(sp),) in done or not sp.exists():
            continue
        try:
            r0 = json.loads(rp.read_text())
        except json.JSONDecodeError:
            continue
        dz = Path(r0.get("dzn", ""))
        if not r0.get("ok") or not dz.is_file():
            continue
        out = {}
        for v, path in VERIFIERS.items():
            q = subprocess.run([sys.executable, str(path), "--dzn", str(dz),
                                "--solution", str(sp), "--quiet",
                                "--json-report", str(TMP / f"re_{v}.json")],
                               capture_output=True, text=True)
            rr = json.loads((TMP / f"re_{v}.json").read_text()) \
                if q.returncode in (0, 1) else {"ok": False, "messages":
                                                ["crash"]}
            out[v] = rr
        H.append_row(RECHECK_CSV, RECHECK_FIELDS, dict(
            solution=rel(sp), dzn=rel(dz), safedse_ok=out["safedse"]["ok"],
            ext_ok=out["ext"]["ok"],
            ext_kinds=";".join(classify(out["ext"]["messages"])),
            ext_messages=" | ".join(out["ext"]["messages"])[:300]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("parts", nargs="+",
                    choices=["base", "solution", "model", "generator",
                             "recheck", "oracle", "model_reverify"])
    ap.add_argument("--verifiers", nargs="+", default=list(VERIFIERS),
                    choices=list(VERIFIERS))
    ap.add_argument("--only", nargs="*", help="operators / model mutants")
    ap.add_argument("--no-model-check", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    for part in a.parts:
        if part == "base":
            base()
        elif part == "solution":
            solution({v: VERIFIERS[v] for v in a.verifiers},
                     not a.no_model_check, a.only)
        elif part == "model":
            model({v: VERIFIERS[v] for v in a.verifiers}, a.only)
        elif part == "recheck":
            recheck()
        elif part == "oracle":
            oracle()
        elif part == "model_reverify":
            for v in a.verifiers:
                if v != "safedse":
                    model_reverify(v, VERIFIERS[v])
        else:
            for v in a.verifiers:
                if v != "ext":       # ext is scored through audit_ext
                    generator(v, VERIFIERS[v])
    print(f"done in {time.time() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

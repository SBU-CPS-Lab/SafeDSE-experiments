#!/usr/bin/env python3
"""safedse/data/patterns*.yaml -> generated/paper/tables/tab-catalogue.tex
(Table II of the paper).

The table body is generated, never typed: pattern structure, placement
relations with their fault class, SIL range and covered fault classes come
straight from the catalogue records. The main catalogue is
data/patterns.yaml; the NVP record comes from the separate
data/patterns_explicit_voter_demo.yaml (a case study), so it is
marked in the table. An unknown pattern id, role, relation or source stops
the script, so the table cannot drift silently from the data.

`none` shows no coverage claim (its covers_faults only make it admissible
under every fault model); the sources come from each record's `source:`
field; the NVP voter is marked as not replicated; the legend says how fault
class S is covered.

    python3 make_catalogue_table.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import PAPER_OUT, SAFEDSE  # noqa: E402

MAIN = SAFEDSE / "data" / "patterns.yaml"
DEMO = SAFEDSE / "data" / "patterns_explicit_voter_demo.yaml"
EXTRA = ["nvp_three_version"]            # taken from DEMO, marked with a dagger
OUT = PAPER_OUT / "tables" / "tab-catalogue.tex"

NAME = {  # display names (Koopman / Armoush pattern names, shortened)
    "none": "none",
    "low_sil_doer_checker": "Low-SIL D/C",
    "same_cpu_doer_checker": "Same-CPU D/C",
    "two_of_two_high_sil": "2-of-2",
    "high_sil_isolated_checker": "High-SIL isolated D/C",
    "mixed_sil_doer_checker": "Mixed-SIL D/C",
    "dual_two_of_two": "Dual 2-of-2",
    "one_of_two_failover": "1-of-2 failover",
    "low_sil_diverse_doer_checker": "Low-SIL diverse D/C",
    "nvp_three_version": "3-version (NVP)",
}
ROLE = {  # role -> (meaning for the table note, symbol)
    "owner": ("owner (the application actor)", "o"),
    "checker": ("checkers", "c"),
    "checker_1": ("checkers", "c_1"),
    "checker_2": ("checkers", "c_2"),
    "channel_b": ("second channel of the owner's pair", "b"),
    "primary_b": ("second channel of the owner's pair", "b"),
    "standby_a": ("standby pair", "s_a"),
    "standby_b": ("standby pair", "s_b"),
    "version_2": ("versions", "v_2"),
    "version_3": ("versions", "v_3"),
    "voter": ("voter", "w"),
}
REL = {"SAME": "=", "DIFFERENT": r"\neq_{c}", "DIFFERENT_FCR": r"\neq_{f}",
       "DIVERSE": r"\neq_{t}"}
FAULT = {"random_hw": "H", "systematic_sw": "S"}
# first word of a `source:` entry -> legend name (cite keys verified in
# refs.bib)
# Koopman and Wagner publish only the monitor/actuator
# (doer/checker) pair; the other names come from Koopman's course catalog,
# which is not cited, so the legend says so.
SOURCE = {"koopman": r"Koopman (names; D/C pair in~\cite{koopman2016challenges})",
          "douglass": r"Douglass~\cite{douglass2002realtime}",
          "armoush": r"Armoush~\cite{armoush2010design}"}
NO_SOURCE = {"baseline"}
# Citable counterparts in Douglass, Real-Time Design Patterns, Ch. 9. Kept
# here, not in SafeDSE's `source:` fields
# (paper-side attribution, no catalog change). Monitor-Actuator (9.6): a
# monitoring channel detects a fault of the actuation channel and the system
# enters its fail-safe state; it assumes fault independence, so Same-CPU D/C
# is not listed, and it has one monitor, so Mixed-SIL D/C (two checkers) is
# not either. Homogeneous Redundancy (9.3): identical channels with a
# switch-to-backup policy. Heterogeneous Redundancy (9.5) has independent
# designs; the diverse D/C is a Monitor-Actuator on another core type.
DOUGLASS = {"low_sil_doer_checker": "Monitor-Actuator",
            "high_sil_isolated_checker": "Monitor-Actuator",
            "low_sil_diverse_doer_checker": "Monitor-Actuator",
            "one_of_two_failover": "Homogeneous Redundancy"}


def sym(role: str) -> str:
    if role not in ROLE:
        sys.exit(f"unknown role {role!r}: extend ROLE in {Path(__file__).name}")
    return ROLE[role][1]


def components(p: dict) -> str:
    ss = [sym(c["role"]) for c in p["components"]]
    return f"${','.join(ss)}$" if ss else "--"


def placement(p: dict) -> str:
    by_fault: dict[str, list[str]] = {}
    for pl in p["placement"]:
        if pl["relation"] not in REL:
            sys.exit(f"unknown relation {pl['relation']!r} in {p['id']}")
        ms = [sym(m) for m in pl["members"]]
        rel = REL[pl["relation"]]
        expr = (f"{ms[0]}{rel}{ms[1]}" if len(ms) == 2
                else f"{rel}\\{{{','.join(ms)}\\}}")
        by_fault.setdefault(FAULT[pl["for"]] if "for" in pl else "",
                            []).append(expr)
    if not by_fault:
        return "--"
    # one \mbox per relation, so a line never breaks inside one
    # one \mbox per relation (the first one also holds the fault-class
    # prefix), so a line never breaks inside a relation
    return "; ".join(", ".join(
        f"\\mbox{{{(k + ': ') if k and i == 0 else ''}${e}$}}"
        for i, e in enumerate(v)) for k, v in by_fault.items())


def note(rows: list) -> str:
    used = {c["role"] for p, _ in rows for c in p["components"]}
    groups: dict[str, list[str]] = {}
    for r in ROLE:
        if r in used or r == "owner":
            g = groups.setdefault(ROLE[r][0], [])
            if ROLE[r][1] not in g:
                g.append(ROLE[r][1])
    return "; ".join(f"${','.join(v)}$ {k}" for k, v in groups.items())


def sil(p: dict) -> str:
    s = sorted(p["achieves_sil"])
    return f"{s[0]}" if len(s) == 1 else f"{s[0]}--{s[-1]}"


def covers(p: dict) -> str:
    if p["id"] == "none":                # no measure, hence no coverage
        return "--"
    return ", ".join(FAULT[f] for f in p["covers_faults"])


def sources(p: dict) -> list[str]:
    """Keys of SOURCE named by the record's `source:` entries (an entry
    counts if its first word is a key; the others are the source's own
    pattern names, e.g. "HmD", or standard clauses). A pattern other than
    `none` without any known source stops the script."""
    firsts = [str(x).split()[0].lower() for x in p.get("source", [])]
    if p["id"] in DOUGLASS:
        firsts.append("douglass")
    keys = list(dict.fromkeys(f for f in firsts if f in SOURCE))
    if not keys and not set(firsts) & NO_SOURCE:
        sys.exit(f"no known source in {p['id']}: {p.get('source')}; "
                 f"extend SOURCE")
    return keys


def source_note(rows: list) -> str:
    """One legend sentence: per source, the rows that cite it ("all except
    ..." when that is shorter). Replaces a column (the table is full)."""
    rows = [(p, m) for p, m in rows if p["id"] != "none"]
    parts = []
    for key, name in SOURCE.items():
        yes = [NAME[p["id"]] for p, _ in rows if key in sources(p)]
        no = [NAME[p["id"]] for p, _ in rows if key not in sources(p)]
        if not yes:
            continue
        which = ("all rows" if not no else
                 f"all except {', '.join(no)}" if len(no) < len(yes)
                 else ", ".join(yes))
        parts.append(f"{name}: {which}")
    return "Sources: " + "; ".join(parts) + "."


def main() -> int:
    pats = yaml.safe_load(MAIN.read_text())["patterns"]
    demo = {p["id"]: p for p in yaml.safe_load(DEMO.read_text())["patterns"]}
    rows = [(p, "") for p in pats] + [(demo[i], "$^\\dagger$") for i in EXTRA]
    for p, _ in rows:
        if p["id"] not in NAME:
            sys.exit(f"pattern {p['id']!r} has no display name in NAME")
        sources(p)
    lines = [
        f"% GENERATED by {Path(__file__).name} (SafeDSE-experiments) from",
        f"% safedse/data/{MAIN.name} and {DEMO.name} -- do not edit.",
        r"\setlength{\tabcolsep}{3pt}",
        r"\begin{tabular*}{\columnwidth}{@{\extracolsep{\fill}}"
        r"l l >{\raggedright\arraybackslash}p{1.22in} c c@{}}",
        r"\toprule",
        r"Pattern & Adds & Placement per fault class & SIL & Covers \\",
        r"\midrule",
    ]
    for p, mark in rows:
        lines.append(f"{NAME[p['id']]}{mark} & {components(p)} & "
                     f"{placement(p)} & {sil(p)} & {covers(p)} \\\\")
    lines += [r"\bottomrule", r"\end{tabular*}", "",
              r"\vspace{2pt}\parbox{\columnwidth}{\scriptsize "
              f"Roles: {note(rows)}. "
              # compact legend: relation code names; H/S are defined in
              # Sec. III-B of the paper.
              r"$\neq_{c}$, $\neq_{f}$, $\neq_{t}$: different core, FCR, "
              r"core type; $=$: same core. S is covered through different "
              r"core types or, for Same-CPU D/C, the checker's algorithm. "
              r"D/C: doer/checker. "
              r"NVP: N-version programming. "
              r"$^\dagger$Separate catalog, used in one case study; its voter "
              r"$w$ is not replicated. " + source_note(rows) + "}"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT} ({len(rows)} patterns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

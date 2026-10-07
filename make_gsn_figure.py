#!/usr/bin/env python3
"""Fig. 4: a single-column excerpt of a generated GSN argument, as TikZ.

Reads the argument that tools/gsn.py generated for the verified TOTALCOST
optimum of f_both3 (kept by gsn_stats.py in results/gsn/) together with the
verifier report it cites, selects the branch of ONE actor, and writes
generated/paper/figures/fig-gsn.tex (Fig. 4 of the paper). Nothing in the
figure is typed by hand: element ids, structure, evidence indices and the
evidence details come from the two JSON files. Element texts are shortened
by the rules in SHORT, each of which must match the generated text exactly
(the script stops otherwise), so a change in gsn.py cannot silently leave a
stale label in the paper.

Layout (GSN Community Standard v3 shapes): the actor goal
and the pattern strategy on top, the provisioning goal and its Solution to
their right; below, one row per general scenario, read left to right:
scenario goal -> tactic strategy -> deployment goal -> Solution, with the
undeveloped value-domain goal of the tactic underneath.

To save space, the undeveloped method goal ("Method implemented and
effective") is drawn only once, in the first scenario row; the later rows
omit theirs. An assumption on a deployment goal sits below that goal, its
Solution to the right.

    python3 make_gsn_figure.py [--actor backEnd]
    python3 make_gsn_figure.py --actor compJah \
        --out generated/report/figures/fig-gsn-compjah.tex
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import PAPER_OUT, ROOT  # noqa: E402

ARG = ROOT / "results" / "gsn" / "f_both3.gsn.json"
REPORT = ROOT / "results" / "gsn" / "f_both3.report.json"
OUT = PAPER_OUT / "figures" / "fig-gsn.tex"


def tex(s: str) -> str:
    return s.replace("_", r"\_").replace("&", r"\&").replace("%", r"\%")


def code(s: str) -> str:
    return r"\texttt{" + tex(s) + "}"


# (pattern over the generated text, short label). Captured groups are
# available to the label as {1}, {2}, ...; a label is TeX.
SHORT = [
    (r"Actor (\w+) of application \w+, required SIL (\d): the deployment "
     r"meets the architectural preconditions that the catalog states for SIL "
     r"\d\.",
     lambda m: f"{tex(m[1])}, required SIL {m[2]}: the deployment meets the "
               f"catalog's preconditions for SIL {m[2]}"),
    (r"Every copy of (\w+) and of its (\w+) is implemented at SIL (\d) or "
     r"above and runs on a core provisioned to at least that level\.",
     lambda m: f"{tex(m[1])} and its {tex(m[2])} at SIL~$\\ge${m[3]}, on "
               f"cores provisioned to SIL~$\\ge${m[3]}"),
    (r"Argument by application of the (\w+) safety architecture pattern, "
     r"over each of its general scenarios\.",
     lambda m: f"By application of pattern\\\\{code(m[1])}"),
    (r"An implausible output of the doer is detected by the checker\. "
     r"\[\w+/(SC\d)\]",
     lambda m: f"{m[1]}: an implausible doer output is detected by the "
               f"checker"),
    (r"A random hardware fault in the doer's fault containment region does "
     r"not prevent detection\. \[\w+/(SC\d)\]",
     lambda m: f"{m[1]}: a random HW fault in the doer's FCR does not "
               f"prevent detection"),
    (r"A systematic fault in the doer's implementation is not reproduced by "
     r"the checker\. \[\w+/(SC\d)\]",
     lambda m: f"{m[1]}: a systematic fault of the doer is not reproduced "
               f"by the checker"),
    # scenarios of two_of_two_high_sil (compJah, technical report)
    (r"A fault in either channel is detected as a disagreement between the "
     r"two channels\. \[\w+/(SC\d)\]",
     lambda m: f"{m[1]}: a fault in either channel shows as a "
               f"disagreement"),
    (r"A random hardware fault in one channel does not affect the other "
     r"channel\. \[\w+/(SC\d)\]",
     lambda m: f"{m[1]}: a random HW fault in one channel does not affect "
               f"the other"),
    (r"A systematic fault is not common to both channels\. \[\w+/(SC\d)\]",
     lambda m: f"{m[1]}: a systematic fault is not common to both "
               f"channels"),
    (r"A detected disagreement does not propagate an incorrect result to the "
     r"consumer\. \[\w+/(SC\d)\]",
     lambda m: f"{m[1]}: a disagreement does not reach the consumer"),
    (r"Achieved through the ([\w ]+?) tactic \(\w+\): .*",
     lambda m: f"Tactic: {tex(m[1])}"),
    (r"In the deployed mapping, the replicated components run in different "
     r"fault containment regions\.",
     lambda m: "Replicas run in different FCRs"),
    (r"In the deployed mapping, the diverse components run on different core "
     r"types\.",
     lambda m: "Diverse components run on different core types"),
    (r"Common-cause failures of these components through what the placement "
     r"does not separate \(.*\) are identified and controlled\.",
     lambda m: "Remaining common causes controlled"),
    (r"An IEC 61508 method realising [\w ]+ is implemented in \w+, and its "
     r"effectiveness for the faults of concern is demonstrated\.",
     lambda m: "Method implemented and effective"),
    (r"Distinct core types give distinct toolchains and object code, not "
     r"independent design\..*",
     lambda m: "Distinct core types, not independent design"),
]


def short(text: str) -> str:
    hits = [(p, f) for p, f in SHORT if re.fullmatch(p, text, re.S)]
    if len(hits) != 1:
        raise SystemExit(f"no unique shortening rule for: {text!r}")
    p, f = hits[0]
    return f(re.fullmatch(p, text, re.S))


def evidence_label(sn: dict, checks: list[dict]) -> tuple[str, str]:
    """(record index, one or two lines of what the record says)."""
    if len(sn["evidence"]) != 1:
        raise SystemExit(f"{sn['id']}: expected one cited record")
    j = sn["evidence"][0]
    c = checks[j]
    if c["kind"] == "sil_actor":
        return str(j), (f"SIL {c['implemented']} on a\\\\SIL-{c['core_sil']} "
                        f"core")
    if c["kind"] == "placement":
        # the generated Solution text names the core types; the record holds
        # only their indices
        m = re.search(r"fault containment regions (\d+)/(\d+), core types "
                      r"(\w+)/(\w+)", sn["text"])
        if not m or [int(m[1]), int(m[2])] != c["fcrs"]:
            raise SystemExit(f"{sn['id']}: text and record {j} disagree")
        rel = c["relation"]
        detail = (f"FCR {m[1]} vs.\\ {m[2]}" if rel == "DIFFERENT_FCR"
                  else f"{m[3]}/{m[4]}")
        return str(j), f"{code(rel)}\\\\{detail}"
    raise SystemExit(f"{sn['id']}: unexpected record kind {c['kind']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actor", default="backEnd")
    # the technical report writes further excerpts elsewhere
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()

    doc = json.loads(ARG.read_text())
    rep = json.loads(REPORT.read_text())
    if not (rep["ok"] and doc["meta"]["verifier_ok"]):
        raise SystemExit("the cited verifier run did not pass")
    el = {e["id"]: e for e in doc["elements"]}
    checks = rep["checks"]

    def kids(eid, kind=None):
        return [el[c] for c in el[eid]["supportedBy"]
                if kind is None or el[c]["kind"] == kind]

    top = [e for e in doc["elements"] if e["kind"] == "Goal"
           and e["text"].startswith(f"Actor {a.actor} of application")]
    if len(top) != 1:
        raise SystemExit(f"actor {a.actor} not found once")
    g = top[0]
    (g_prov,) = kids(g["id"], "Goal")
    sn_prov = kids(g_prov["id"], "Solution")     # owner, then each component
    if not 1 <= len(sn_prov) <= 2:
        raise SystemExit(f"{g_prov['id']}: expected one or two Solutions")
    (s_pat,) = kids(g["id"], "Strategy")
    scen = kids(s_pat["id"], "Goal")

    L: list[str] = []
    w = L.append

    # widths are outer widths in inches; text width = width - 2 inner xsep
    def node(style, eid, label, at, width, extra="", ix="\\IX"):
        w(f"\\node[{style},text width={width}*1in-{ix},anchor=north west"
          f"{extra}] ({eid}) at {at} {{\\textbf{{{eid}}} {label}}};")

    def strategy(eid, label, at, width):
        node("strat", eid, label, at, width, ix="\\IXS")
        w(f"\\begin{{pgfonlayer}}{{background}}\\draw[stratbg] "
          f"($({eid}.north west)+(\\SL,0)$) -- "
          f"($({eid}.north east)+(\\SL,0)$) -- "
          f"($({eid}.south east)+(-\\SL,0)$) -- "
          f"($({eid}.south west)+(-\\SL,0)$) -- cycle;"
          f"\\end{{pgfonlayer}}")

    def solution(sn, at, extra="", label=True):
        j, det = evidence_label(sn, checks)
        w(f"\\node[sol{extra}] ({sn['id']}) at {at} "
          f"{{\\textbf{{{sn['id']}}}\\\\check {j}}};")
        if label:
            w(f"\\node[evd] ({sn['id']}e) at ({sn['id']}.south) {{{det}}};")
        return det

    def undeveloped(eid):
        w(f"\\node[undev] ({eid}d) at ({eid}.south) {{}};")

    # ---- top: actor goal, pattern strategy, provisioning goal + solution
    node("goal", g["id"], short(g["text"]), "(0,0)", "\\WG")
    w(f"\\coordinate (y0) at ($({g['id']}.south)+(0,-\\TG)$);")
    strategy(s_pat["id"], short(s_pat["text"]), "(\\XA,0 |- y0)", "\\WS")
    node("goal", g_prov["id"], short(g_prov["text"]), "(\\XC,0 |- y0)",
         "\\WP")
    for c in (s_pat["id"], g_prov["id"]):
        w(f"\\draw[sup] ({c}.north -| {c}.north) ++(0,\\TG) -- "
          f"({c}.north);")
    w(f"\\node[fit=({g['id']})({s_pat['id']})({g_prov['id']}),"
      f"inner sep=0pt] (row0) {{}};")
    # The provisioning Solutions sit side by side below the provisioning
    # goal, in the Solution column of the first scenario row (empty there);
    # equal evidence labels are printed once, under the pair.
    dets = [solution(sn, f"($(\\XDT-{q}*\\DS,0 |- {g_prov['id']}.south)"
                         f"+(0,-\\PG)$)", ",anchor=north", label=False)
            for q, sn in enumerate(reversed(sn_prov))][::-1]
    first_row_extra = [sn["id"] for sn in sn_prov]
    if len(set(dets)) == 1:
        # under the pair, below the larger circle (more digits in "check j")
        low = max(sn_prov, key=lambda x: len(str(x["evidence"][0])))
        w(f"\\node[evd] (provlbl) at ($({sn_prov[0]['id']}.south)!0.5!"
          f"({sn_prov[-1]['id']}.south)$ |- {low['id']}.south) {{{dets[0]}}};")
        # keep a gap to the next row, whose Solution rises above its goal
        w("\\coordinate (provlblb) at ($(provlbl.south)+(0,-\\PG)$);")
        first_row_extra += ["provlbl", "provlblb"]
    else:
        for sn, det in zip(sn_prov, dets):
            w(f"\\node[evd] ({sn['id']}e) at ({sn['id']}.south) {{{det}}};")
            first_row_extra.append(f"{sn['id']}e")
    w(f"\\coordinate (pv) at ($({sn_prov[0]['id']}.north)+(0,0.045)$);")
    w(f"\\draw ({g_prov['id']}.south -| pv) -- (pv);")
    for sn in sn_prov:
        w(f"\\draw[sup] (pv) -| ({sn['id']}.north);")
    if kids(kids(kids(scen[0]["id"], "Strategy")[0]["id"], "Goal")[0]["id"],
            "Solution"):
        raise SystemExit("first scenario row has a Solution; re-layout")

    # ---- one row per general scenario
    prev = "row0"
    method_drawn = False
    for k, sc in enumerate(scen):
        (st,) = kids(sc["id"], "Strategy")
        subs = kids(st["id"], "Goal")
        dep = [x for x in subs if not x["undeveloped"]]
        und = [x for x in subs if x["undeveloped"]]
        if len(dep) > 1 or not 1 <= len(und) <= 2 or (len(und) == 2
                                                      and not dep):
            raise SystemExit(f"{st['id']}: unexpected sub-goals")
        w(f"\\coordinate (y{k + 1}) at ($({prev}.south)+(0,-\\RG)$);")
        node("goal", sc["id"], short(sc["text"]), f"(\\XA,0 |- y{k + 1})",
             "\\WA")
        strategy(st["id"], short(st["text"]), f"(\\XB,0 |- {sc['id']}.north)",
                 "\\WB")
        w(f"\\draw[sup] ({sc['id']}.east |- {st['id']}.west) -- "
          f"({st['id']}.west);")
        w(f"\\draw[sup] (\\XS,0 |- {s_pat['id']}.south) |- "
          f"({sc['id']}.west);")
        members = [sc["id"], st["id"]] + (first_row_extra if k == 0 else [])
        u = und[0]
        # draw the method goal once (the first row shows it)
        if dep and method_drawn:
            und = [x for x in und
                   if short(x["text"]) != "Method implemented and effective"]
        if dep:
            d = dep[0]
            node("goal", d["id"], short(d["text"]),
                 f"(\\XC,0 |- {st['id']}.north)", "\\WC")
            (sn,) = kids(d["id"], "Solution")
            asms = [el[c] for c in d["inContextOf"]]
            if any(x["kind"] != "Assumption" for x in asms) or len(asms) > 1:
                raise SystemExit(f"{d['id']}: unexpected context")
            w(f"\\draw[sup] ({st['id']}.east) -- ({d['id']}.west |- "
              f"{st['id']}.east);")
            if asms:
                # Solution right of the goal, assumption below it
                asm = asms[0]
                solution(sn, f"(\\XD,0 |- {d['id']}.center)")
                w(f"\\draw[sup] ({d['id']}.east) -- ({sn['id']}.west);")
                w(f"\\node[asm] ({asm['id']}) at "
                  f"($({d['id']}.south)+(0,-\\UG)$) "
                  f"{{\\textbf{{{asm['id']}}} {short(asm['text'])}}};")
                w(f"\\node[anchor=south east,inner sep=0pt,"
                  f"font=\\sffamily\\scriptsize\\bfseries] at "
                  f"({asm['id']}.south east) {{A}};")
                w(f"\\draw[ctx] ({d['id']}.south) -- ({asm['id']}.north);")
                members.append(asm["id"])
            else:
                solution(sn, f"(\\XD,0 |- {d['id']}.center)")
                w(f"\\draw[sup] ({d['id']}.east) -- ({sn['id']}.west);")
            members += [d["id"], sn["id"], f"{sn['id']}e"]
            # undeveloped goals stacked below the deployment goal (or its
            # assumption), each with an elbow from the strategy
            above = asms[0]["id"] if asms else d["id"]
            for u in und:
                node("goal", u["id"], short(u["text"]),
                     f"($(\\XC,0 |- {above}.south)+(0,-\\UG)$)", "\\WC")
                w(f"\\draw[sup] ($({st['id']}.east)+(0.035,0)$) |- "
                  f"({u['id']}.west);")
                undeveloped(u["id"])
                members += [u["id"], f"{u['id']}d"]
                above = u["id"]
        else:
            node("goal", u["id"], short(u["text"]),
                 f"(\\XC,0 |- {st['id']}.north)", "\\WC")
            w(f"\\draw[sup] ({st['id']}.east) -- ({u['id']}.west |- "
              f"{st['id']}.east);")
            undeveloped(u["id"])
            members += [u["id"], f"{u['id']}d"]
            method_drawn = True
        w(f"\\node[fit={' '.join(f'({m})' for m in members)},"
          f"inner ysep=0pt,inner xsep=0pt] (row{k + 1}) {{}};")
        prev = f"row{k + 1}"

    body = "\n".join(L)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(f"""% Fig. 4 -- GENERATED by make_gsn_figure.py (SafeDSE-experiments); do not edit.
% Source: {ARG.relative_to(ROOT)} (tools/gsn.py on the verified
% TOTALCOST optimum of f_both3, fault model {doc['meta']['fault_model']}) and
% {REPORT.relative_to(ROOT)} (the verifier run it cites).
% Excerpt: the branch of actor {a.actor}; ids are those of the argument.
% GSN Community Standard v3 shapes: goal (rectangle), strategy
% (parallelogram), solution (circle), assumption (ellipse, "A"),
% undeveloped (hollow diamond); SupportedBy = filled arrowhead,
% InContextOf = open arrowhead.
\\documentclass[border=1pt]{{standalone}}
\\usepackage{{tikz}}
\\input{{palette}}
\\begin{{document}}
\\begin{{tikzpicture}}[x=1in,y=1in,
  el/.style={{draw=black,line width=0.5pt,fill=white,align=left,
             inner xsep=2pt,inner ysep=1.5pt,
             execute at begin node={{\\hyphenpenalty=10000}},
             font=\\sffamily\\scriptsize\\linespread{{0.92}}\\selectfont}},
  goal/.style={{el}},
  strat/.style={{el,draw=none,fill=none,inner xsep=3.5pt}},
  stratbg/.style={{draw=black,line width=0.5pt,fill=cGrayL}},
  sol/.style={{el,circle,draw=cWarm,fill=cWarmL,align=center,
              inner sep=0.5pt,minimum size=0.36in}},
  evd/.style={{anchor=north,align=center,inner sep=1pt,text=cGray,
              font=\\sffamily\\scriptsize\\linespread{{0.92}}\\selectfont}},
  asm/.style={{el,ellipse,align=center,anchor=north,text width=0.67in,
              inner xsep=-4pt,inner ysep=-2.5pt}},
  undev/.style={{draw=black,line width=0.5pt,fill=white,diamond,
                inner sep=0pt,minimum size=5pt,anchor=north}},
  sup/.style={{-{{Stealth[length=3.2pt,width=2.6pt]}},line width=0.5pt}},
  ctx/.style={{-{{Stealth[open,length=3.4pt,width=2.8pt]}},line width=0.5pt}},
]
\\def\\IX{{4.5pt}} \\def\\IXS{{7.5pt}} \\def\\SL{{0.035}}
\\def\\WG{{3.38}} \\def\\WS{{1.56}} \\def\\TG{{0.09}}
\\def\\WP{{1.14}} \\def\\XDT{{3.20}} \\def\\DS{{0.415}} \\def\\PG{{0.07}}
\\def\\XS{{0.03}} \\def\\XA{{0.09}} \\def\\WA{{0.86}}
\\def\\XB{{1.01}} \\def\\WB{{0.655}}
\\def\\XC{{1.74}} \\def\\WC{{0.84}}
\\def\\XD{{3.03}}
\\def\\RG{{0.035}} \\def\\UG{{0.10}}
{body}
\\end{{tikzpicture}}
\\end{{document}}
""")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

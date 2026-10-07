"""Exact period of a returned design.

With a cost objective the model only guarantees MCR <= mu <= period_ub. At
-p 1 CP-SAT follows the model's `indomain_min` annotation and reports the
least mu, but a parallel portfolio (-p > 1) can report mu at the bound
(observed on RQ3 instances). `pin_period` re-solves the returned design
with every decision fixed (wrappers/fix_design.mzn) and THROUGHPUT as the
objective, so the reported period is that design's least period, ceil(MCR),
which tools/verify.py then checks independently. The design and all costs
are unchanged (checked: `pin_ok`).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import SAFEDSE  # noqa: E402
sys.path.insert(0, str(SAFEDSE / "tools"))
from solve import run as solve_run  # noqa: E402

WRAPPER_SRC = Path(__file__).resolve().parent / "wrappers" / "fix_design.mzn"
WRAPPER = Path(__file__).resolve().parent / "results" / "tmp" / "fix_design.mzn"


def _wrapper() -> Path:
    """The wrapper says `include "dse.mzn";`; solve.py passes no
    include path, so a copy with the absolute path is solved (as in
    harness.wrapper())."""
    WRAPPER.parent.mkdir(parents=True, exist_ok=True)
    WRAPPER.write_text(WRAPPER_SRC.read_text().replace(
        'include "dse.mzn";', f'include "{SAFEDSE / "model" / "dse.mzn"}";'))
    return WRAPPER
FIELDS = ["proc", "succ", "pat", "pmode", "fcr_used", "sil_impl", "csil",
          "partition", "tdma_alloc", "sendbuf", "recbuf"]
SAME = ["total_cost", "hw_cost", "dev_cost", "promotion_cost",
        "partition_total", "pattern_cost", "nprocs", "power", "pat", "proc"]
PIN_TIME_LIMIT_MS = 60_000


def _mzn(v) -> str:
    v = v if isinstance(v, list) else [v]
    return "[" + ", ".join(("true" if x else "false") if isinstance(x, bool)
                           else str(x) for x in v) + "]"


def pin_period(dzn: str | Path, solution: dict) -> dict:
    """Returns {"status", "seconds", "solution" (or None), "ok"}. Single
    thread: with every decision fixed the search is trivial, and -p 1 keeps
    it deterministic."""
    extra = " ".join(f"fx_{k} = {_mzn(solution[k])};" for k in FIELDS)
    r = solve_run(str(dzn), "THROUGHPUT", timeout=PIN_TIME_LIMIT_MS // 1000 + 60,
                  threads=1, time_limit_ms=PIN_TIME_LIMIT_MS,
                  model=str(_wrapper()), extra=extra)
    s2 = r.get("solution")
    ok = (r["status"] == "OPTIMAL" and s2 is not None
          and all(s2.get(k) == solution.get(k) for k in SAME))
    return {"status": r["status"], "seconds": round(r.get("seconds", 0.0), 3),
            "solution": s2, "ok": ok}

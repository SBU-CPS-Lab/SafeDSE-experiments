"""Locations shared by the experiment scripts.

SafeDSE is the git submodule `safedse/` of this repository; set the
environment variable SAFEDSE to use another checkout. Generated paper and
report inputs (macros, plot data, tables, figure sources) go to `generated/`.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAFEDSE = Path(os.environ.get("SAFEDSE", ROOT / "safedse")).resolve()
GENERATED = ROOT / "generated"
PAPER_OUT = GENERATED / "paper"
REPORT_OUT = GENERATED / "report"

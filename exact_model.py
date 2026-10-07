#!/usr/bin/env python3
"""Creates exact/: a copy of SafeDSE's model/ and lib/ with the exact period
variant B applied (patches/exact_period_variant_b.patch). SafeDSE itself is
never changed. The copy is tracked, and exact/SOURCE.txt records the SafeDSE
commit and the patch it was made from.

    python3 exact_model.py
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT, SAFEDSE  # noqa: E402

PATCH = ROOT / "patches" / "exact_period_variant_b.patch"
DST = ROOT / "exact"


def main() -> int:
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            cwd=SAFEDSE, capture_output=True, text=True,
                            check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "model",
                            "lib"], cwd=SAFEDSE, capture_output=True,
                           text=True, check=True).stdout.strip()
    if dirty:
        raise SystemExit(f"safedse model/ or lib/ has local changes:\n{dirty}")
    shutil.rmtree(DST, ignore_errors=True)
    DST.mkdir()
    for sub in ["model", "lib"]:
        shutil.copytree(SAFEDSE / sub, DST / sub)
    subprocess.run(["patch", "-p1", "--quiet", "-i", str(PATCH)], cwd=DST,
                   check=True)
    (DST / "SOURCE.txt").write_text(
        f"SafeDSE commit {commit}, model/ and lib/, with\n"
        f"patches/{PATCH.name} (sha256 "
        f"{hashlib.sha256(PATCH.read_bytes()).hexdigest()[:16]}) applied.\n"
        f"Exact period variant B; used by exact_check.py and breadth.py.\n"
        f"The comment in lib/activation.mzn mcm_pattern_edges describes\n"
        f"variant A; the code is variant B (patches/README.md).\n")
    print(f"exact/ created from SafeDSE {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

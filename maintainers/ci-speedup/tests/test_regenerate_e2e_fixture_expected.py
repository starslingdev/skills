"""The whole-pipeline fixture regenerator is deterministic and review-only by default."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "maintainers/ci-speedup/scripts/regenerate_e2e_fixture_expected.py"


def test_regenerator_reports_a_clean_corpus_without_writing():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO,
        text=True, capture_output=True, timeout=30)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("0 fixture expectation file(s) changed")

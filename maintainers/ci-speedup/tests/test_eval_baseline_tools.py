"""The discrimination-proof wrapper cuts exactly one catalog entry from the
session's skill copy, and never the repository's own catalog."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "eval_withhold_entry", _REPO / "maintainers" / "ci-speedup" / "scripts" / "eval_withhold_entry.py")
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)
_CATALOG = _REPO / "skills" / "ci-speedup" / "references" / "optimization-patterns.md"


def test_withholding_opt81_removes_its_section_and_only_it():
    text = _CATALOG.read_text(encoding="utf-8")
    cut = wrapper.withhold(text, "OPT81")
    assert "### OPT81 —" not in cut and "unavailable to fork pull requests" not in cut
    assert "### OPT51 —" in cut and "## Category 11: Stack-Specific" in cut
    removed = len(text) - len(cut)
    start = text.index("### OPT81 —")
    assert text[start:start + removed].rstrip().endswith("---")


def test_an_unknown_id_refuses_rather_than_running_an_unchanged_session():
    with pytest.raises(SystemExit):
        wrapper.withhold(_CATALOG.read_text(encoding="utf-8"), "OPT999")


def test_the_summary_reports_medians_ranges_and_not_run_sessions():
    spec = importlib.util.spec_from_file_location(
        "eval_baseline_summary",
        _REPO / "maintainers" / "ci-speedup" / "scripts" / "eval_baseline_summary.py")
    summary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(summary)

    def run(b, ok):
        return {"id": 11, "name": "q", "passed": ok, "checks": [{"name": "c", "passed": ok}],
                "reading": {"reference_bytes": b, "reference_files": [{}] * (b // 100),
                            "catalog_opened": b > 0,
                            "session": {"input_tokens": 1000 + b, "wall_s": 60.0}}}
    out = summary.summarize([run(0, False), run(200, True), run(400, True),
                             {"id": 11, "run": 4, "harness_error": "x"}])
    assert "| 11 | `q` | 4 | 2 | 1 | 200 (0 to 400) | 0 (0 to 0) | 2 (0 to 4) | 2/3 | 1,200 (1,000 to 1,400) |" in out
    assert "| 11 | `c` | 2/3 |" in out

"""The history-drift summary (#282) must never change the audit report.

`scripts/shift_left.py` is a separate, operator-run summary. These tests hold
that boundary: no audit-pipeline script reads it, and a findings document
carrying a shift-left result renders the same findings, credits, ranking and
Bottom line as the same document without it. Wiring shift-left into the audit
means replacing these tests with a real report-only proof, not deleting them.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "verdict_gate_findings.json"
_SPEC = importlib.util.spec_from_file_location("shift_left_isolated", SCRIPTS / "shift_left.py")
assert _SPEC and _SPEC.loader
SHIFT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(SHIFT)


def _shift_result() -> dict:
    history = [{"workflow": ".github/workflows/ci.yml", "workflow_id": "1", "job": "test",
                "runner_labels": ["ubuntu-latest"], "step_signature": ["checkout", "test"],
                "duration_s": value, "status": "completed", "conclusion": "success",
                "pr_number": 100 + index, "sha": f"sha{index}"}
               for index, value in enumerate([100] * 12 + [400] * 8)]
    l3 = SHIFT.analyze_l3(history)
    assert any(item["kind"] == "step_change" for item in l3["observations"])
    result = {"status": "measured", "l3": l3,
              "provenance": {"api_calls": 40, "api_budget": 60}}
    result["summary_markdown"] = SHIFT.render_markdown(result)
    return result


def _render(doc: dict) -> str:
    return bp.render(copy.deepcopy(doc), {}, {}, {}, "2026-10-29T05:21:00Z", {})


def test_no_audit_pipeline_script_reads_the_history_drift_summary():
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name == "shift_left.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"shift[_-]left", text), (
            f"{path.name} references shift_left; prove the audit stays report-only first")


def test_a_shift_left_result_leaves_findings_credits_ranking_and_bottom_line_unchanged():
    base = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    with_shift = copy.deepcopy(base)
    with_shift["shift_left"] = _shift_result()
    with_shift["history_drift"] = with_shift["shift_left"]

    plain_md, shift_md = _render(base), _render(with_shift)
    assert shift_md == plain_md
    assert "> **Bottom line.**" in plain_md
    assert "step_change" not in shift_md and "shift-left" not in shift_md.lower()
    # Rendering must not fold the summary into the findings either.
    assert with_shift["findings"] == base["findings"]

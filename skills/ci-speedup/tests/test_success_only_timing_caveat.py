"""Success-only headline timings disclose a materially red default branch."""

from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
from test_opt81_faster_runner import (  # noqa: E402
    _CI,
    _ONE_LABEL_PLAN,
    _Opt81Client,
)


class _FailureRateClient(_Opt81Client):
    """Keep the ten PR timing runs, then append the requested all-status rows."""

    def __init__(self, default_branch_runs):
        super().__init__(_ONE_LABEL_PLAN)
        self.default_branch_runs = default_branch_runs

    def json(self, endpoint, allow_missing=False):
        data = super().json(endpoint, allow_missing)
        if (isinstance(data, dict) and "workflow_runs" in data
                and "per_page=100" in endpoint):
            return {"workflow_runs": data["workflow_runs"] + copy.deepcopy(
                self.default_branch_runs)}
        return data


def _run(i: int, conclusion: str, *, branch: str = "main", event: str = "push"):
    return {
        "id": 1000 + i,
        "event": event,
        "head_branch": branch,
        "head_sha": f"main-{i}",
        "created_at": f"2026-01-{i + 1:02d}T00:00:00Z",
        "run_started_at": f"2026-01-{i + 1:02d}T00:00:00Z",
        "updated_at": f"2026-01-{i + 1:02d}T00:03:00Z",
        "status": "completed",
        "conclusion": conclusion,
        "run_attempt": 1,
    }


def _collect(monkeypatch, rows):
    client = _FailureRateClient(rows)
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    doc = cr.collect(
        {"repo": "o/r", "findings": [
            {"id": "f1", "pattern": "OPT1", "workflow_file": _CI},
        ]},
        "o/r",
        max_runs=len(_ONE_LABEL_PLAN),
        shallow_runs=len(_ONE_LABEL_PLAN),
    )
    return doc, bp.render(doc)


def test_six_of_ten_red_default_branch_runs_qualify_the_success_only_headline(
        monkeypatch):
    rows = [_run(i, "failure" if i < 6 else "success") for i in range(10)]
    doc, report = _collect(monkeypatch, rows)

    stamp = doc["success_only_timing_by_workflow"][_CI]
    assert stamp == {
        "eligible_runs": 10,
        "failed_runs": 6,
        "failure_pct": 60,
        "status": "caveat",
    }
    assert ("Measured over successful runs; 60% (6 of 10) of default-branch runs "
            f"of `{_CI}` failed in the window.") in report


def test_one_of_ten_failures_stays_below_the_named_threshold(monkeypatch):
    rows = [_run(i, "failure" if i == 0 else "success") for i in range(10)]
    doc, report = _collect(monkeypatch, rows)

    stamp = doc["success_only_timing_by_workflow"][_CI]
    assert stamp["status"] == "below_threshold"
    assert "Measured over successful runs" not in report


def test_four_red_runs_stamp_too_few_and_render_nothing(monkeypatch):
    doc, report = _collect(monkeypatch, [_run(i, "failure") for i in range(4)])

    stamp = doc["success_only_timing_by_workflow"][_CI]
    assert stamp["eligible_runs"] == 4
    assert stamp["failed_runs"] == 4
    assert stamp["status"] == "too_few_runs"
    assert "too few runs" in stamp["reason"]
    assert "Measured over successful runs" not in report


def test_pr_branch_failures_and_cancelled_runs_are_not_in_the_population(monkeypatch):
    rows = (
        [_run(i, "success") for i in range(10)]
        + [_run(20 + i, "failure", branch="feature", event="pull_request")
           for i in range(6)]
        + [_run(30, "cancelled"), _run(31, "skipped")]
    )
    doc, report = _collect(monkeypatch, rows)

    stamp = doc["success_only_timing_by_workflow"][_CI]
    assert stamp["eligible_runs"] == 10
    assert stamp["failed_runs"] == 0
    assert stamp["status"] == "below_threshold"
    assert "Measured over successful runs" not in report

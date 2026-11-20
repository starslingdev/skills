"""OPT108 sizing: short successful comment runs priced at their measured compute."""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL / "scripts"))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402

_WF = ".github/workflows/comment.yml"
_YAML = """on:
  issue_comment:
    types: [created]
jobs:
  react:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/github-script@v7
        with:
          script: |
            const body = context.payload.comment.body;
            if (!body.includes('/')) return;
            await react(body);
"""


def _ts(s: int) -> str:
    return f"2026-01-01T{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}Z"


class _CommentClient:
    """A fake GhClient: one `issue_comment` workflow whose sampled runs follow
    `plan[i] = (duration_s, conclusion)`; its 30-day volume is `monthly`."""

    gave_up = False

    def __init__(self, plan, monthly=600, recorder_s=0):
        self.plan, self.monthly = plan, monthly
        self.recorder_s = recorder_s
        self.queries = self.errors = 0

    def available(self):
        return True

    def _job(self, i):
        dur, conclusion = self.plan[i]
        t0 = i * 1000
        return {"id": 5000 + i, "run_id": 100 + i, "name": "react", "status": "completed",
                "conclusion": conclusion, "started_at": _ts(t0),
                "completed_at": _ts(t0 + dur), "labels": ["ubuntu-latest"],
                "runner_name": "ubuntu-latest",
                "steps": [{"name": "Run actions/github-script@v7", "number": 1,
                           "status": "completed", "conclusion": conclusion,
                           "started_at": _ts(t0), "completed_at": _ts(t0 + dur)}]}

    def json(self, endpoint, allow_missing=False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{"id": 1, "path": _WF, "name": "Comment"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            if re.search(r"per_page=1(?![0-9])", m.group(2)):
                return {"total_count": self.monthly}
            runs = [
                {"id": 100 + i, "event": "issue_comment", "head_sha": "s0",
                 "head_branch": "main", "created_at": _ts(i * 1000),
                 "run_started_at": _ts(i * 1000), "updated_at": _ts(i * 1000 + d),
                 "conclusion": c, "status": "completed", "run_attempt": 1}
                for i, (d, c) in enumerate(self.plan)]
            if "status=success" in m.group(2):
                runs = [r for r in runs if r["conclusion"] == "success"]
            return {"workflow_runs": runs}
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m:
            i = int(m.group(1)) - 100
            jobs = [self._job(i)] if 0 <= i < len(self.plan) else []
            if jobs and self.recorder_s:
                t0 = i * 1000
                jobs.append({
                    "id": 6000 + i, "run_id": 100 + i, "name": "record",
                    "status": "completed", "conclusion": "success",
                    "started_at": _ts(t0),
                    "completed_at": _ts(t0 + self.recorder_s),
                    "labels": ["ubuntu-latest"], "runner_name": "ubuntu-latest",
                })
            return {"jobs": jobs}
        if endpoint.startswith(f"repos/o/r/contents/{_WF}"):
            return {"content": base64.b64encode(_YAML.encode()).decode()}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        return None

    def text(self, endpoint, allow_missing=False):
        self.queries += 1
        return ""


def _collect(monkeypatch, client, *, duplicate=False):
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    finding = {
        "id": "f1", "pattern": "OPT108", "pattern_class": "static",
        "workflow_file": _WF, "title": "Comment filter runs after the runner starts",
        "evidence": ("comment-triggered job `react` filters comment body or author only "
                     "inside a step. Runner-minute effect: saving the affected job's "
                     "measured compute on rejected comments; not sized until matching "
                     "short successful runs confirm the rejected share."),
        "affected_jobs": ["react"], "fix_strategy": "comment-filter-before-runner-start",
    }
    findings = [finding]
    if duplicate:
        findings.append({**finding, "id": "f2"})
    doc = {"repo": "o/r", "findings": findings}
    return cr.collect(doc, "o/r", max_runs=len(client.plan),
                      shallow_runs=len(client.plan))


def _opt108(doc):
    return next(f for f in doc["findings"] if f.get("pattern") == "OPT108")


def _verify(doc, tmp_path):
    findings = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    doc["skill_commit_sha"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=_SKILL, text=True).strip()
    findings.write_text(json.dumps(doc), encoding="utf-8")
    assert bp.main(["--in", str(findings), "--out", str(report_path)]) == 0
    proc = subprocess.run(
        [sys.executable, str(_SKILL / "tests" / "verify_report.py"),
         "--report", str(report_path), "--findings", str(findings)],
        text=True, capture_output=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return report_path.read_text(encoding="utf-8")


# 20 sampled runs: 15 rejected comments (12s each), 5 handled ones (70s each).
_ISSUE_SHAPE = [(12, "success")] * 15 + [(70, "success")] * 5


def test_opt108_short_successful_comment_runs_are_sized_as_a_saving(monkeypatch, tmp_path):
    doc = _collect(monkeypatch, _CommentClient(_ISSUE_SHAPE, monthly=600))
    f = _opt108(doc)
    m = f["comment_runner_measurement"]
    assert m["status"] == "measured"
    assert (m["sampled_runs"], m["short_success_runs"], m["priced_runs"]) == (20, 15, 15)
    # 600 runs/30d x 15/20 short x 1 billed minute each (a 12s job rounds up)
    # = 450 runner-min/30d, within the workflow's 600 x (15x1 + 5x2)/20 = 750.
    assert f["runner_min_saving"] == 450.0
    assert m["affected_compute_min_30d"] == 750.0 and m["capped"] is False
    assert f["wall_clock_p50_s"] == 0.0
    assert f["sizing_basis"] == "measured"
    assert "no wall-clock effect on the pull-request path" in f["size_note"]
    assert "15 of 20 sampled runs" in f["evidence"]
    assert "not sized until" not in f["evidence"]
    report = _verify(doc, tmp_path)
    assert "15 of 20 sampled runs" in report


def test_opt108_prices_only_the_affected_job(monkeypatch, tmp_path):
    client = _CommentClient(_ISSUE_SHAPE, monthly=600, recorder_s=61)
    doc = _collect(monkeypatch, client)
    finding = _opt108(doc)
    measurement = finding["comment_runner_measurement"]
    # The unconditional two-minute recorder is present in every run, but the
    # job-level comment guard can skip only `react`.
    assert finding["runner_min_saving"] == 450.0
    assert measurement["affected_compute_min_30d"] == 750.0
    assert measurement["sized_affected_jobs"] == ["react"]
    _verify(doc, tmp_path)


def test_opt108_does_not_credit_the_same_affected_job_twice(monkeypatch, tmp_path):
    doc = _collect(
        monkeypatch, _CommentClient(_ISSUE_SHAPE, monthly=600), duplicate=True)
    rows = [f for f in doc["findings"] if f.get("pattern") == "OPT108"]
    assert [f["runner_min_saving"] for f in rows] == [450.0, None]
    assert "already credited" in rows[1]["comment_runner_measurement"]["reason"]
    _verify(doc, tmp_path)


@pytest.mark.parametrize("plan,reason", [
    ([(70, "success")] * 20, "no successful comment-triggered run finished under 30s"),
    ([(12, "failure")] * 20, "no successful comment-triggered run finished under 30s"),
])
def test_opt108_long_or_failing_comment_runs_are_not_credited(monkeypatch, tmp_path,
                                                               plan, reason):
    doc = _collect(monkeypatch, _CommentClient(plan, monthly=600))
    f = _opt108(doc)
    assert f["comment_runner_measurement"]["status"] == "unmeasured"
    assert reason in f["comment_runner_measurement"]["reason"]
    assert f["runner_min_saving"] is None
    assert reason in f["size_note"]
    _verify(doc, tmp_path)


def test_opt108_without_runs_stays_unsized_with_its_reason(monkeypatch, tmp_path):
    doc = _collect(monkeypatch, _CommentClient([], monthly=0))
    f = _opt108(doc)
    assert f["runner_min_saving"] is None
    assert f["comment_runner_measurement"]["status"] == "unmeasured"
    assert f["comment_runner_measurement"]["reason"]


def test_opt108_saving_is_capped_at_the_workflow_measured_compute():
    f = {"pattern": "OPT108", "workflow_file": _WF, "evidence": ""}
    runs = [{"id": i, "event": "issue_comment", "conclusion": "success",
             "run_started_at": _ts(0), "updated_at": _ts(10)} for i in range(4)]
    # Every sampled run is short (raw credit 100 runs x 2 billed minutes = 200),
    # but the workflow's measured runs average (4 x 2 + 4 x 1) / 8 = 1.5 billed
    # minutes: the credit is bounded by that measured compute, 150 per 30 days.
    short_jobs = [[{"started_at": _ts(0), "completed_at": _ts(61)}]] * 4
    workflow_jobs = [[{"started_at": _ts(0), "completed_at": _ts(6)}]] * 4
    cr._size_opt108_comment_runs(f, runs, short_jobs, workflow_jobs, 100)
    m = f["comment_runner_measurement"]
    assert f["runner_min_saving"] == m["affected_compute_min_30d"] == 150.0
    assert m["capped"] is True


def _load_verify_report():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "verify_report_opt108", _SKILL / "tests" / "verify_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["verify_report_opt108"] = mod
    spec.loader.exec_module(mod)
    return mod


def _measured_row(**over):
    row = {"id": "f1", "pattern": "OPT108", "affected_jobs": ["react"],
           "wall_clock_p50_s": 0.0,
           "runner_min_saving": 450.0,
           "comment_runner_measurement": {
               "status": "measured", "sampled_runs": 20, "comment_runs": 20,
               "short_success_runs": 15, "workflow_runs_30d": 600,
               "short_floor_s": 30.0, "priced_runs": 15,
               "sized_affected_jobs": ["react"],
               "mean_short_billable_min": 1.0, "affected_mean_billable_min": 1.25,
               "affected_compute_min_30d": 750.0}}
    row.update(over)
    return row


def test_verifier_accepts_a_rederived_opt108_saving():
    assert _load_verify_report()._vr_opt108_problems(_measured_row()) == []


@pytest.mark.parametrize("over,needle", [
    ({"runner_min_saving": 900.0}, "re-derived 450.0"),
    ({"wall_clock_p50_s": 12.0}, "do not gate a merge"),
    ({"comment_runner_measurement": {**_measured_row()["comment_runner_measurement"],
                                     "priced_runs": 2}}, "is below 3"),
    ({"comment_runner_measurement": {**_measured_row()["comment_runner_measurement"],
                                     "sized_affected_jobs": ["record"]}},
     "not a non-empty subset"),
    ({"comment_runner_measurement": {"status": "unmeasured", "reason": "x"}},
     "without a measured comment-run sample"),
])
def test_verifier_rejects_an_unsupported_opt108_saving(over, needle):
    problems = _load_verify_report()._vr_opt108_problems(_measured_row(**over))
    assert any(needle in p for p in problems), problems

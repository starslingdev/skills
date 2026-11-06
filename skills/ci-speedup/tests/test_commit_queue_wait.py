from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


_SKILL = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402

_VR_SPEC = importlib.util.spec_from_file_location(
    "commit_queue_verify_report", Path(__file__).with_name("verify_report.py"))
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


_CI = ".github/workflows/ci.yml"
_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(seconds: int) -> str:
    return (_BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _job(run_id: int) -> dict:
    return {
        "id": run_id * 10 + 1,
        "name": "test",
        "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
        "created_at": _iso(0),
        "started_at": _iso(900),
        "completed_at": _iso(1290),
        "steps": [{"name": "Run tests", "started_at": _iso(900),
                   "completed_at": _iso(1290)}],
    }


def _commit_fact(sha: str, run_id: int) -> dict:
    return {
        "sha": sha,
        "commit_wall_s": 1290.0,
        "created_at": _iso(0),
        "slowest_job": {"name": "test", "duration_s": 390.0},
        "next_longest_job": None,
        "partial": False,
        "jobs": [{
            "name": "test",
            "run_id": run_id,
            "queued_at": _iso(0),
            "started_at": _iso(900),
            "completed_at": _iso(1290),
        }],
    }


class _QueueClient:
    gave_up = False

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self.shas = [f"s{i}" for i in range(1, 21)]

    def available(self) -> bool:
        return True

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{"id": 1, "path": _CI, "name": "ci"}]}
        match = re.match(r"repos/o/r/actions/workflows/1/runs\?(.*)", endpoint)
        if match:
            if re.search(r"per_page=1(?![0-9])", match.group(1)):
                return {"total_count": 40}
            return {"workflow_runs": [{
                "id": 100 + i,
                "event": "pull_request",
                "head_sha": sha,
                "status": "completed",
                "conclusion": "success",
                "created_at": _iso(0),
                "run_started_at": _iso(900),
                "updated_at": _iso(1290),
            } for i, sha in enumerate(self.shas)]}
        match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if match:
            return {"jobs": [_job(int(match.group(1)))]}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            return {"check_runs": [{
                "name": "test",
                "started_at": _iso(900),
                "completed_at": _iso(1290),
            }]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": ["test"]}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


_DOC = {
    "repo": "o/r",
    "workflow_job_graph": {
        _CI: {"test": {"name": "test", "needs": []}},
    },
    "findings": [{
        "id": "code-finding",
        "pattern": "OPT24",
        "severity": "HIGH",
        "title": "Long test job without sharding",
        "workflow_file": _CI,
        "affected_jobs": ["test"],
        "sizing_basis": "measured",
        "wall_clock_p50_s": 195.0,
        "runner_min_saving": 0.0,
        "tier": 1,
        "realization": "direct",
        "size_note": "halving the measured work",
    }],
    # Supplied by the per-commit wall-clock pass. Queue/work enrichment consumes
    # these facts; it does not produce or project them.
    "commit_wall_clock": {
        "commits": [_commit_fact(f"s{i}", 100 + i) for i in range(1, 21)],
        "p50_s": 1290.0,
        "p90_s": 1290.0,
    },
}


def _write_artifacts(tmp_path: Path, doc: dict) -> tuple[str, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    return report, findings_path, report_path


def test_collect_includes_queue_in_commit_wait_and_verifier_guards_work_budget(
        monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "GhClient", _QueueClient)
    doc = cr.collect(copy.deepcopy(_DOC), "o/r", max_runs=20, shallow_runs=20)
    clock = doc["pr_critical_path"].get("commit_wall_clock") or {}

    assert clock.get("queue_p50_s") == 900.0
    assert clock.get("work_p50_s") == 390.0
    assert abs(clock.get("queue_share_p50") - 900.0 / 1290.0) < 0.000001

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert "21m 30s" in report
    assert "~15m 00s of it waiting for a runner" in report
    assert "1.18x" in report

    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok and "provenance" not in check.name]
    assert failures == [], [(check.name, check.detail) for check in failures]

    mutant = copy.deepcopy(doc)
    mutant["findings"][0]["wall_clock_p50_s"] = 900.0
    mutant_report, mutant_path, mutant_report_path = _write_artifacts(
        tmp_path / "mutant", mutant)
    check = vr.check_commit_queue_work_credit(
        mutant_report, mutant_path, mutant_report_path)
    assert check.ok is False
    assert "code-finding" in check.detail


def test_commit_queue_split_counts_idle_queued_gaps_without_double_counting():
    fact = {
        "sha": "gap",
        "commit_wall_s": 40.0,
        "created_at": _iso(0),
        "jobs": [
            {"name": "first", "queued_at": _iso(0), "started_at": _iso(10),
             "completed_at": _iso(20)},
            {"name": "second", "queued_at": _iso(15), "started_at": _iso(30),
             "completed_at": _iso(40)},
        ],
    }

    split = cr._commit_queue_work_split(fact)

    # Queue is [0, 10] plus [20, 30]. The overlapping queued/running window
    # [15, 20] is work, and is never counted twice.
    assert split == {"queue_s": 20.0, "work_s": 20.0}

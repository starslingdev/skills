from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import verify_report as vr  # noqa: E402


_TEST_WORKFLOW = ".github/workflows/test.yml"
_IMAGE_WORKFLOW = ".github/workflows/image.yml"


def _iso(seconds: int) -> str:
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


def _job(name: str, run_id: int, seconds: int) -> dict:
    return {
        "id": run_id * 10 + len(name),
        "name": name,
        "labels": ["ubuntu-latest"],
        "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
        "started_at": _iso(0),
        "completed_at": _iso(seconds),
        "steps": [{
            "name": "work", "started_at": _iso(0),
            "completed_at": _iso(seconds),
        }],
    }


class _FallbackClient:
    gave_up = False
    required_contexts: list[str] | None = None

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self.shas = [f"commit-{index:02d}" for index in range(20)]

    def available(self) -> bool:
        return True

    def _runs(self, workflow_id: int) -> list[dict]:
        base = 1000 if workflow_id == 1 else 2000
        wall = 60 if workflow_id == 1 else 1500
        return [{
            "id": base + index,
            "event": "pull_request",
            "head_sha": sha,
            "status": "completed",
            "conclusion": "success",
            "created_at": _iso(0),
            "run_started_at": _iso(0),
            "updated_at": _iso(wall),
        } for index, sha in enumerate(self.shas)]

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": _TEST_WORKFLOW, "name": "test"},
                {"id": 2, "path": _IMAGE_WORKFLOW, "name": "image"},
            ]}
        match = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if match:
            if re.search(r"per_page=1(?![0-9])", match.group(2)):
                return {"total_count": 20}
            return {"workflow_runs": self._runs(int(match.group(1)))}
        match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if match:
            run_id = int(match.group(1))
            if 1000 <= run_id < 1020:
                return {"jobs": [_job("test", run_id, 60)]}
            if 2000 <= run_id < 2020:
                return {"jobs": [_job("build-image", run_id, 1500)]}
            return {"jobs": []}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            # Reproduction: both workflow runs share the commit, but the long job's
            # check run is absent from this PR population.
            return {"check_runs": [{
                "name": "test", "started_at": _iso(0),
                "completed_at": _iso(60),
            }]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return [] if self.required_contexts is not None else None
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return ({"contexts": list(self.required_contexts)}
                    if self.required_contexts is not None else None)
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


class _ReadableRequiredClient(_FallbackClient):
    required_contexts = ["test"]


class _PartialWinnerClient(_FallbackClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        job_match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if job_match and 1000 <= int(job_match.group(1)) < 1012:
            self.queries += 1
            run_id = int(job_match.group(1))
            return {"jobs": [_job("test", run_id, 1600)]}
        check_match = re.match(
            r"repos/o/r/commits/commit-(\d+)/check-runs", endpoint)
        if check_match and int(check_match.group(1)) < 12:
            self.queries += 1
            return {"check_runs": [{
                "name": "test", "started_at": _iso(0), "completed_at": _iso(1600),
            }]}
        return super().json(endpoint, allow_missing)


_DOC = {
    "repo": "o/r",
    "skill_commit_sha": "installed:unversioned",
    "workflow_job_graph": {
        _TEST_WORKFLOW: {"test": {"name": "test", "needs": []}},
        _IMAGE_WORKFLOW: {"build-image": {"name": "build-image", "needs": []}},
    },
    "findings": [
        {"id": "test", "workflow_file": _TEST_WORKFLOW},
        {"id": "image", "workflow_file": _IMAGE_WORKFLOW},
    ],
}


def _collect(monkeypatch, client_type=_FallbackClient) -> dict:
    monkeypatch.setattr(cr, "GhClient", client_type)
    return cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=20, shallow_runs=20)


def test_fallback_crowns_the_job_that_ends_last_on_each_commit_and_verifies(
        monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch)
    cp = doc["pr_critical_path"]

    assert cp["critical_path_check"] == "build-image"
    assert cp["provenance"] in {"pr_floor_fallback", "unresolved"}
    build = next(row for row in cp["checks"] if row["name"] == "build-image")
    assert build["present_on"] == 20
    assert build["pole_n"] == 20
    assert cp["chain_summary"]["makespan_p50_s"] == 1500.0

    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_readable_required_set_keeps_required_scoped_ranking_unchanged(monkeypatch):
    doc = _collect(monkeypatch, _ReadableRequiredClient)
    cp = doc["pr_critical_path"]

    assert cp["provenance"] == "required_scoped"
    assert cp["critical_path_check"] == "test"
    assert [row["name"] for row in cp["checks"]] == ["test"]


def test_recovered_candidate_keeps_occurrences_where_another_job_is_slowest(monkeypatch):
    doc = _collect(monkeypatch, _PartialWinnerClient)
    cp = doc["pr_critical_path"]
    build = next(row for row in cp["checks"] if row["name"] == "build-image")

    assert build["present_on"] == 20
    assert build["pole_n"] == 8

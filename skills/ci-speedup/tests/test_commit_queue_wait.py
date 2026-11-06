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
    }, {
        "id": "cap-probe",
        "pattern": "OPT24",
        "severity": "LOW",
        "title": "Over-budget advisory",
        "workflow_file": _CI,
        "affected_jobs": ["test"],
        "sizing_basis": "measured",
        "wall_clock_p50_s": 900.0,
        "wall_clock_uncapped_p50_s": 1000.0,
        "wall_clock_derivation": [{
            "bound": "earlier",
            "from_s": 1000.0,
            "to_s": 900.0,
            "reason": "earlier bound",
        }],
        "runner_min_saving": 0.0,
        "tier": 1,
        "realization": "direct",
        "advisory": True,
    }, {
        "id": "rare-code-finding",
        "pattern": "OPT24",
        "severity": "HIGH",
        "title": "Rare long test job",
        "workflow_file": _CI,
        "affected_jobs": ["test"],
        "sizing_basis": "measured",
        "wall_clock_p50_s": 300.0,
        "runner_min_saving": 0.0,
        "tier": 1,
        "realization": "direct",
        "spine_rare": True,
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
    capped = next(f for f in doc["findings"] if f.get("id") == "cap-probe")
    assert capped["wall_clock_p50_s"] == 390.0
    assert capped["wall_clock_uncapped_p50_s"] == 1000.0
    assert capped["wall_clock_derivation"][-1]["bound"] == "commit-work"
    assert capped["wall_clock_derivation"][-1]["from_s"] == 900.0
    assert capped["wall_clock_derivation"][-1]["to_s"] == 390.0

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

    for forbidden in (
            "Sharding removes 5 minutes of runner queue.",
            "Reduce queue time by changing the test code.",
            "Sharding eliminates five minutes of runner queue."):
        claim_check = vr.check_commit_queue_work_credit(
            report + "\n" + forbidden, findings_path, report_path)
        assert claim_check.ok is False
        assert "cuts runner queue" in claim_check.detail
    honest = vr.check_commit_queue_work_credit(
        report + "\nSharding reduces test work while runner queue remains unchanged.",
        findings_path, report_path)
    assert honest.ok, honest.detail


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


def test_zero_queue_keeps_the_existing_report_output(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _QueueClient)
    zero = copy.deepcopy(_DOC)
    for fact in zero["commit_wall_clock"]["commits"]:
        fact["commit_wall_s"] = 390.0
        fact["jobs"][0].update({
            "queued_at": _iso(0),
            "started_at": _iso(0),
            "completed_at": _iso(390),
        })
    zero["commit_wall_clock"].update({"p50_s": 390.0, "p90_s": 390.0})

    doc = cr.collect(zero, "o/r", max_runs=20, shallow_runs=20)
    clock = doc["pr_critical_path"]["commit_wall_clock"]
    assert clock["queue_p50_s"] == 0.0
    assert clock["work_p50_s"] == 390.0

    without_clock = copy.deepcopy(doc)
    without_clock["pr_critical_path"].pop("commit_wall_clock")
    assert bp.render(copy.deepcopy(doc)) == bp.render(without_clock)


def test_collect_preserves_nested_commit_clock_handoff(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _QueueClient)
    nested = copy.deepcopy(_DOC)
    nested["pr_critical_path"] = {
        "commit_wall_clock": nested.pop("commit_wall_clock"),
    }

    doc = cr.collect(nested, "o/r", max_runs=20, shallow_runs=20)

    clock = doc["pr_critical_path"]["commit_wall_clock"]
    assert clock["queue_p50_s"] == 900.0
    assert clock["work_p50_s"] == 390.0


def test_skewed_medians_keep_projection_above_the_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "GhClient", _QueueClient)
    doc = cr.collect(copy.deepcopy(_DOC), "o/r", max_runs=20, shallow_runs=20)

    def fact(sha: str, wall: int, queue: int) -> dict:
        return {
            "sha": sha,
            "commit_wall_s": float(wall),
            "created_at": _iso(0),
            "partial": False,
            "jobs": [{
                "name": "test",
                "queued_at": _iso(0),
                "started_at": _iso(queue),
                "completed_at": _iso(wall),
            }],
        }

    clock = cr._stamp_commit_queue_work({
        "commits": [fact("a", 100, 90), fact("b", 100, 10), fact("c", 200, 20)],
        "p50_s": 100.0,
        "p90_s": 180.0,
    })
    doc["pr_critical_path"]["commit_wall_clock"] = clock
    doc["findings"] = [
        f for f in doc["findings"] if f.get("pattern") not in {"OPT43", "OPT97"}]
    doc["findings"][0]["wall_clock_p50_s"] = 90.0
    cr._cap_code_findings_at_commit_work(
        doc["findings"], cr._commit_work_budget(clock))

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert "~20s of it waiting for a runner** (20%)" in report
    assert "remaining **~1m 20s** is work" in report
    assert "to **~20s (5.00x)**" in report
    assert "runner queue measured across sampled commits" in report
    check = vr.check_commit_queue_work_credit(report, findings_path, report_path)
    assert check.ok, check.detail
    missing_work = report.replace(
        "the remaining **~1m 20s** is work", "the work decomposition is omitted")
    missing_work_check = vr.check_commit_queue_work_credit(
        missing_work, findings_path, report_path)
    assert missing_work_check.ok is False
    assert "coherent work remainder" in missing_work_check.detail


def test_full_verifier_reads_cluster_win_after_smaller_queue_clock(
        monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "GhClient", _QueueClient)
    doc = cr.collect(copy.deepcopy(_DOC), "o/r", max_runs=20, shallow_runs=20)
    commits = []
    for i in range(1, 21):
        fact = _commit_fact(f"cluster-{i}", 200 + i)
        fact["commit_wall_s"] = 450.0
        fact["jobs"][0].update({
            "queued_at": _iso(0),
            "started_at": _iso(60),
            "completed_at": _iso(450),
        })
        commits.append(fact)
    doc["pr_critical_path"]["commit_wall_clock"] = cr._stamp_commit_queue_work({
        "commits": commits,
        "p50_s": 450.0,
        "p90_s": 450.0,
    })
    cluster = copy.deepcopy(doc["findings"][0])
    cluster.update({
        "id": "cluster-finding",
        "pattern": "OPT73",
        "title": "Shared test setup across matrix legs",
        "wall_clock_p50_s": 300.0,
        "cluster_floor_lever": True,
        "cluster_legs_concurrent": True,
        "affected_jobs": ["test", "test-2"],
        "evidence": "the `test setup` step repeats across the matrix",
        "off_spine": False,
    })
    cluster.pop("spine_rare", None)
    cluster.pop("advisory", None)
    doc["findings"] = [cluster]

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    bottom = next(line for line in report.splitlines()
                  if line.startswith("> **Bottom line.**"))
    assert bottom.index("~1m 00s of it waiting") < bottom.index("removes **~5m 00s**")
    assert "**OPT73** in `ci.yml`" in bottom
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok and "provenance" not in check.name]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_partial_only_stamp_skips_and_static_report_uses_full_wait(tmp_path):
    partial = _commit_fact("partial", 1)
    partial["partial"] = True
    partial_clock = cr._stamp_commit_queue_work({"commits": [partial]})
    partial_path = tmp_path / "partial.json"
    partial_path.write_text(json.dumps({
        "pr_critical_path": {"commit_wall_clock": partial_clock},
    }), encoding="utf-8")
    partial_check = vr.check_commit_queue_work_credit("", partial_path)
    assert partial_check.ok and partial_check.skipped
    partial_claim = vr.check_commit_queue_work_credit(
        "Sharding removes five minutes of runner queue.", partial_path)
    assert partial_claim.ok is False and not partial_claim.skipped

    complete_clock = cr._stamp_commit_queue_work({
        "commits": [_commit_fact("complete", 2)],
        "p50_s": 1290.0,
        "p90_s": 1290.0,
    })
    static_doc = {
        "repo": "o/r",
        "findings": [],
        "pr_critical_path": {"poles": [], "commit_wall_clock": complete_clock},
        "data_sources": {},
    }
    static_report, static_path, static_report_path = _write_artifacts(
        tmp_path / "static", static_doc)
    assert "Commit-level developer wait was measured" in static_report
    assert "No run history to measure" not in static_report
    assert "No PR run timing was available" not in static_report
    assert "> **Bottom line.** The full developer wall is **21m 30s**" in static_report
    static_check = vr.check_commit_queue_work_credit(
        static_report, static_path, static_report_path)
    assert static_check.ok, static_check.detail

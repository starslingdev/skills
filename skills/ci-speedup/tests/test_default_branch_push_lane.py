"""Default-branch pushes are measured in a lane separate from merge gates."""

from __future__ import annotations

import importlib.util
import copy
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402


_VR_SPEC = importlib.util.spec_from_file_location(
    "push_lane_verify_report", HERE / "verify_report.py")
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


_GATE = ".github/workflows/gate.yml"
_PUBLISH = ".github/workflows/publish.yml"
_PUSH_SHAS = [f"push-{index}" for index in range(12)]


def _iso(seconds: int) -> str:
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


def _job(name: str, run_id: int, start_s: int, end_s: int) -> dict:
    return {
        "id": run_id * 10,
        "name": name,
        "labels": ["ubuntu-latest"],
        "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
        "started_at": _iso(start_s),
        "completed_at": _iso(end_s),
        "steps": [{
            "name": "work",
            "started_at": _iso(start_s),
            "completed_at": _iso(end_s),
        }],
    }


def _push_run(run_id: int, sha: str, created_s: int, completed_s: int) -> dict:
    return {
        "id": run_id,
        "event": "push",
        "head_branch": "main",
        "head_sha": sha,
        "status": "completed",
        "conclusion": "success",
        "created_at": _iso(created_s),
        "run_started_at": _iso(created_s),
        "updated_at": _iso(completed_s),
    }


class _PushLaneClient:
    gave_up = False

    def __init__(self, *, include_pushes: bool = True,
                 default_branch_known: bool = True,
                 all_status_available: bool = True,
                 frequency_probe: object = None) -> None:
        self.queries = 0
        self.errors = 0
        self.include_pushes = include_pushes
        self.default_branch_known = default_branch_known
        self.all_status_available = all_status_available
        self.frequency_probe = frequency_probe
        self.jobs = {100: [_job("merge-gate", 100, 0, 120)]}
        for index, sha in enumerate(_PUSH_SHAS):
            base = 10000 + index * 4000
            self.jobs[1000 + index] = [_job("verify", 1000 + index, base, base + 300)]
            self.jobs[2000 + index] = [
                _job("publish-image", 2000 + index, base, base + 2100)
            ]

    def available(self) -> bool:
        return True

    def _runs(self, workflow_id: int) -> list[dict]:
        pull_request = {
            "id": 100,
            "event": "pull_request",
            "head_branch": "feature",
            "head_sha": "pr-sha",
            "status": "completed",
            "conclusion": "success",
            "created_at": _iso(0),
            "run_started_at": _iso(0),
            "updated_at": _iso(120),
        }
        if not self.include_pushes:
            return [pull_request] if workflow_id == 1 else []
        pushes = []
        for index, sha in enumerate(_PUSH_SHAS):
            base = 10000 + index * 4000
            run_id = (1000 if workflow_id == 1 else 2000) + index
            pushes.append(_push_run(run_id, sha, base, base + (300 if workflow_id == 1 else 2100)))
        return [*reversed(pushes), *([pull_request] if workflow_id == 1 else [])]

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": _GATE, "name": "gate"},
                {"id": 2, "path": _PUBLISH, "name": "publish"},
            ]}
        match = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if match:
            if (not self.all_status_available
                    and "per_page=100" in match.group(2)
                    and "status=success" not in match.group(2)):
                return None
            if re.search(r"per_page=1(?![0-9])", match.group(2)):
                return ({"total_count": 30} if self.frequency_probe is None
                        else self.frequency_probe)
            return {"workflow_runs": self._runs(int(match.group(1)))}
        match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if match:
            return {"jobs": self.jobs.get(int(match.group(1)), [])}
        if endpoint == "repos/o/r/commits/pr-sha/check-runs?per_page=100":
            return {"check_runs": [{
                "name": "merge-gate",
                "started_at": _iso(0),
                "completed_at": _iso(120),
            }]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"} if self.default_branch_known else {}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": ["merge-gate"]}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


def _doc() -> dict:
    return {
        "repo": "o/r",
        "skill_commit_sha": "installed:unversioned",
        "workflow_job_graph": {
            _GATE: {"merge-gate": {"name": "merge-gate", "needs": []}},
            _PUBLISH: {"publish-image": {"name": "publish-image", "needs": []}},
        },
        "findings": [
            {"id": "gate", "workflow_file": _GATE},
            {"id": "publish", "workflow_file": _PUBLISH},
        ],
    }


def _collect(monkeypatch, *, include_pushes: bool = True,
             default_branch_known: bool = True,
             all_status_available: bool = True,
             created_before: str = "2026-01-02T00:00:00Z") -> dict:
    client = _PushLaneClient(
        include_pushes=include_pushes,
        default_branch_known=default_branch_known,
        all_status_available=all_status_available)
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    return cr.collect(
        _doc(), "o/r", max_runs=20, shallow_runs=20,
        created_before=created_before)


def _write_artifacts(tmp_path: Path, doc: dict) -> tuple[str, Path, Path]:
    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    return report, findings_path, report_path


def test_collect_renders_a_capped_default_branch_push_lane_and_verifies(
        monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch)
    lane = doc.get("default_branch_push_lane") or {}

    assert lane.get("sampled_push_count") == 10
    assert lane.get("sample_cap") == 10
    assert lane.get("sampled_run_count") == 20
    # The headline sample already fetched the gate workflow's push runs; only the
    # ten sampled publish runs are new job reads, and the memo keeps it at that.
    assert lane.get("job_fetch_count") == 10
    assert lane.get("job_fetch_failures") == 0
    assert [fact["sha"] for fact in lane.get("facts", [])] == list(reversed(_PUSH_SHAS))[:10]

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert "After merge, not a merge gate" in report
    assert "p50 **35m 00s**" in report
    assert "p90 **35m 00s**" in report
    assert "`publish-image`" in report
    assert "`verify`" in report
    # The lane uses the shared per-commit projection (4x on the slowest job),
    # capped by the next-longest job: 2100 - 2100 + max(525, 300) = 525 s.
    assert "projects to **8m 45s** (**4.00x**)" in report
    bottom_line = next(line for line in report.splitlines() if "**Bottom line.**" in line)
    assert "2m 00s" in bottom_line
    assert "35m 00s" not in bottom_line

    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    isolation = next(check for check in checks if check.name == "default-branch push lane is isolated")
    assert isolation.ok and not isolation.skipped

    mutant = report.replace("2m 00s", "35m 00s", 1)
    check = vr.check_default_branch_push_lane_isolated(mutant, findings_path)
    assert not check.ok

    isolation_mutants = [
        report.replace(
            "**2m 00s until all checks finish**",
            "**35m 00s until all checks finish**", 1),
        report.replace(
            "- 2m 00s (the gate)",
            "- 35m 00s (the gate)", 1),
        report.replace(
            "## 📋 Contents",
            "**Merge score:** 35m 00s\n\n## 📋 Contents", 1),
    ]
    for isolation_mutant in isolation_mutants:
        check = vr.check_default_branch_push_lane_isolated(
            isolation_mutant, findings_path)
        assert not check.ok


def test_push_lane_reports_the_30_day_frequency_and_rare_window(
        monkeypatch, tmp_path: Path):
    client = _PushLaneClient()
    original_runs = client._runs

    def runs_with_long_history(workflow_id: int) -> list[dict]:
        rows = original_runs(workflow_id)
        pushes = [row for row in rows if row.get("event") == "push"]
        non_pushes = [row for row in rows if row.get("event") != "push"]
        for index, row in enumerate(pushes):
            days_old = (2, 12, 29, 35, 43, 51, 59, 67, 75, 90, 90, 90)[index]
            created = datetime(2026, 1, 2, tzinfo=timezone.utc) - timedelta(days=days_old)
            row["created_at"] = created.isoformat().replace("+00:00", "Z")
        return [*pushes, *non_pushes]

    client._runs = runs_with_long_history  # type: ignore[method-assign]
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    doc = cr.collect(
        _doc(), "o/r", max_runs=20, shallow_runs=20,
        created_before="2026-01-02T00:00:00Z")

    lane = doc["default_branch_push_lane"]
    assert lane["pushes_30d"] == 3
    assert lane["window_span_days"] == 90
    assert lane["rare"] is True

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert "3 pushes to the default branch in the last 30 days" in report
    assert "rare" in report.lower()
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]

    mutant = report.replace(
        "3 pushes to the default branch in the last 30 days.\n", "", 1)
    check = vr.check_default_branch_push_lane_isolated(mutant, findings_path)
    assert not check.ok


def test_truncated_push_frequency_is_a_lower_bound_when_probe_is_unusable(
        monkeypatch, tmp_path: Path):
    client = _PushLaneClient(frequency_probe={"message": "rate limited"})
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    # Make the compact fixture exercise the same full-page path as production.
    monkeypatch.setattr(cr, "_COST_RUNLIST_MAX", 13)

    doc = cr.collect(
        _doc(), "o/r", max_runs=20, shallow_runs=20,
        created_before="2026-01-02T00:00:00Z")
    lane = doc["default_branch_push_lane"]

    assert lane["status"] == "frequency_incomplete"
    assert lane["pushes_30d"] == len(_PUSH_SHAS)
    assert lane["rare"] is False
    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert f"at least {len(_PUSH_SHAS)} pushes" in report.lower()
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    assert not [check for check in checks if not check.skipped and not check.ok]


def test_push_frequency_endpoint_quotes_the_default_branch():
    endpoint = cr._push_volume_endpoint(
        "o/r", 7, "release&event=pull_request#stable",
        "2026-01-02T00:00:00Z")

    assert "branch=release%26event%3Dpull_request%23stable" in endpoint
    assert endpoint.count("&event=") == 1


def test_frequent_push_lane_is_not_marked_rare(monkeypatch, tmp_path: Path):
    client = _PushLaneClient()
    original_runs = client._runs

    def thirty_pushes(workflow_id: int) -> list[dict]:
        pull_rows = [
            row for row in original_runs(workflow_id)
            if row.get("event") != "push"
        ]
        rows = []
        for index in range(30):
            base = 1000 + index * 100
            run_id = (3000 if workflow_id == 1 else 4000) + index
            sha = f"dense-{index}"
            rows.append(_push_run(run_id, sha, base, base + 300))
            job_name = "verify" if workflow_id == 1 else "publish-image"
            duration = 120 if workflow_id == 1 else 300
            client.jobs[run_id] = [_job(job_name, run_id, base, base + duration)]
        return [*reversed(rows), *pull_rows]

    client._runs = thirty_pushes  # type: ignore[method-assign]
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    doc = cr.collect(
        _doc(), "o/r", max_runs=40, shallow_runs=40,
        created_before="2026-01-02T00:00:00Z")

    lane = doc["default_branch_push_lane"]
    assert lane["pushes_30d"] == 30
    assert lane["rare"] is False
    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert "30 pushes to the default branch in the last 30 days" in report
    lane_block = report.split("<!-- ci-speedup:default-branch-push-lane -->", 1)[1]
    assert "rare" not in lane_block.split("---", 1)[0].lower()
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_release_only_static_hit_is_tagged_and_never_gets_wall_clock_credit(
        monkeypatch, tmp_path: Path):
    client = _PushLaneClient()
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    source = _doc()
    source["findings"] = [{
        "id": "f1",
        "pattern": "OPT102",
        "pattern_class": "static",
        "severity": "MEDIUM",
        "title": "Emulated compilation in a multi-platform image build",
        "workflow_file": _PUBLISH,
        "line": 8,
        "affected_jobs": ["publish-image"],
        "workflow_activity": {},
        "workflow_lanes": ["release"],
        "evidence": "a multi-platform image compiles in the build job",
        "fix_strategy": "compile for each target without emulation",
        "fix_recipe_anchor": "opt102",
        "wall_clock_p50_s": 600.0,
        "sizing_basis": "measured",
        "realization": "direct",
        "tier": 1,
    }]

    doc = cr.collect(
        source, "o/r", max_runs=20, shallow_runs=20,
        created_before="2026-01-02T00:00:00Z")
    finding = doc["findings"][0]
    assert finding["non_pr_lane"] is True
    assert finding["wall_clock_p50_s"] == 0.0

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert "release lane only; no merge-gate wall-clock credit" in report
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    mutant = report.replace(
        "**Lane:** release lane only; no merge-gate wall-clock credit.\n", "", 1)
    assert not vr.check_default_branch_push_lane_isolated(mutant, findings_path).ok


def test_scheduled_static_hit_on_a_pr_pole_keeps_its_lane_disclosure(
        monkeypatch, tmp_path: Path):
    client = _PushLaneClient()
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    source = _doc()
    source["findings"] = [{
        "id": "f1",
        "pattern": "OPT102",
        "pattern_class": "static",
        "severity": "MEDIUM",
        "title": "Scheduled compilation still uses emulation",
        "workflow_file": _GATE,
        "line": 8,
        "affected_jobs": ["merge-gate"],
        "workflow_activity": {},
        "workflow_lanes": ["schedule"],
        "evidence": "a scheduled multi-platform image compiles in the gate job",
        "fix_strategy": "compile for each target without emulation",
        "fix_recipe_anchor": "opt102",
        "wall_clock_p50_s": 600.0,
        "sizing_basis": "measured",
        "realization": "direct",
        "tier": 1,
    }]

    doc = cr.collect(
        source, "o/r", max_runs=20, shallow_runs=20,
        created_before="2026-01-02T00:00:00Z")
    finding = doc["findings"][0]
    assert finding["non_pr_lane"] is True
    assert finding["wall_clock_p50_s"] == 0.0

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert "Scheduled compilation still uses emulation" in report
    assert "schedule lane only; no merge-gate wall-clock credit" in report
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_unscoped_push_static_hit_is_not_assumed_to_be_non_pr(
        monkeypatch, tmp_path: Path):
    client = _PushLaneClient()
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    source = _doc()
    source["findings"] = [{
        "id": "f1",
        "pattern": "OPT102",
        "pattern_class": "static",
        "severity": "MEDIUM",
        "title": "Emulated compilation in a multi-platform image build",
        "workflow_file": _PUBLISH,
        "line": 8,
        "affected_jobs": ["publish-image"],
        "workflow_activity": {},
        "workflow_lanes": ["push"],
        "workflow_push_scopes": ["unscoped"],
        "evidence": "a multi-platform image compiles in the build job",
        "fix_strategy": "compile for each target without emulation",
        "fix_recipe_anchor": "opt102",
        "wall_clock_p50_s": 600.0,
        "sizing_basis": "measured",
        "realization": "direct",
        "tier": 1,
    }]

    doc = cr.collect(
        source, "o/r", max_runs=20, shallow_runs=20,
        created_before="2026-01-02T00:00:00Z")
    assert doc["findings"][0].get("non_pr_lane") is not True

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    assert not [check for check in checks if not check.skipped and not check.ok]


def test_group_activity_counts_each_workflow_once(monkeypatch, tmp_path: Path):
    client = _PushLaneClient()
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    source = _doc()
    source["findings"] = [
        {
            "id": f"f{index}",
            "pattern": "OPT102",
            "pattern_class": "static",
            "severity": "MEDIUM",
            "title": "Emulated compilation in a multi-platform image build",
            "workflow_file": _PUBLISH,
            "line": 8 + index,
            "affected_jobs": ["publish-image"],
            "workflow_activity": {},
            "workflow_lanes": ["release"],
            "evidence": "a multi-platform image compiles in the build job",
            "fix_strategy": "compile for each target without emulation",
            "fix_recipe_anchor": "opt102",
            "runner_min_saving": 0.0,
            "sizing_basis": "measured",
            "realization": "direct",
            "tier": 2,
        }
        for index in range(2)
    ]
    source["findings"][1]["workflow_lanes"] = ["schedule"]
    doc = cr.collect(
        source, "o/r", max_runs=20, shallow_runs=20,
        created_before="2026-01-02T00:00:00Z")
    expected = doc["findings"][0]["workflow_activity"]["runs_30d"]

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert f"**Activity:** {expected} runs in 30 days" in report
    assert f"**Activity:** {expected * 2} runs in 30 days" not in report
    assert "**Lane:** release, schedule lanes; no merge-gate wall-clock credit." in report
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    assert not [check for check in checks if not check.skipped and not check.ok]


def test_zero_push_lane_still_verifies_non_pr_finding_invariants(
        monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch, include_pushes=False)
    report, findings_path, _report_path = _write_artifacts(tmp_path, doc)
    doc["findings"].append({
        "pattern": "OPT102",
        "workflow_file": _PUBLISH,
        "workflow_lanes": ["release"],
        "wall_clock_p50_s": 123.0,
    })
    findings_path.write_text(json.dumps(doc), encoding="utf-8")

    check = vr.check_default_branch_push_lane_isolated(report, findings_path)
    assert not check.ok


def test_no_default_branch_pushes_renders_an_explicit_zero_line(monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch, include_pushes=False)
    lane = doc.get("default_branch_push_lane") or {}

    assert lane.get("sampled_push_count") == 0
    assert lane.get("facts") == []
    report, findings_path, _report_path = _write_artifacts(tmp_path, doc)
    assert "no eligible completed pushes to the default branch were sampled" in report.lower()
    check = vr.check_default_branch_push_lane_isolated(report, findings_path)
    assert check.ok and not check.skipped


def test_push_lane_survives_a_report_with_no_merge_poles(monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch)
    doc["pr_critical_path"]["poles"] = []

    report, findings_path, _report_path = _write_artifacts(tmp_path, doc)
    assert "After merge, not a merge gate" in report
    assert "p50 **35m 00s**" in report
    check = vr.check_default_branch_push_lane_isolated(report, findings_path)
    assert check.ok and not check.skipped


def test_unknown_push_discovery_is_disclosed_instead_of_rendered_as_zero(
        monkeypatch, tmp_path: Path):
    for kwargs, reason in (
        ({"default_branch_known": False}, "default branch could not be determined"),
        ({"all_status_available": False}, "push run-list coverage was incomplete"),
    ):
        doc = _collect(monkeypatch, **kwargs)
        report, findings_path, _report_path = _write_artifacts(tmp_path, doc)

        assert reason in report
        assert "no eligible completed pushes" not in report.lower()
        check = vr.check_default_branch_push_lane_isolated(report, findings_path)
        assert check.ok and not check.skipped


def test_long_push_job_names_use_the_same_rendered_label_in_the_verifier(
        monkeypatch, tmp_path: Path):
    doc = _collect(monkeypatch)
    for fact in doc["default_branch_push_lane"]["facts"]:
        fact["slowest_job"]["name"] = "publish-image-for-every-supported-platform"
        fact["next_longest_job"]["name"] = "verify-every-package-in-the-workspace"
    doc["default_branch_push_lane"]["drill"]["job"] = (
        "publish-image-for-every-supported-platform")

    report, findings_path, _report_path = _write_artifacts(tmp_path, doc)
    check = vr.check_default_branch_push_lane_isolated(report, findings_path)
    assert check.ok and not check.skipped, check.detail


def test_real_collection_times_the_push_lane_without_supplied_facts(
        monkeypatch, tmp_path: Path):
    """The lane's facts come from the shared per-commit producer on a real run,
    not from facts injected into the input findings."""
    plain = _doc()
    client = _PushLaneClient()
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    doc = cr.collect(plain, "o/r", max_runs=20, shallow_runs=20)
    lane = doc["default_branch_push_lane"]

    assert lane.get("sampled_push_count") == 10
    assert lane.get("p50_s") == 2100.0
    assert lane.get("p90_s") == 2100.0
    facts = lane.get("facts") or []
    assert len(facts) == 10
    assert all(fact.get("event") == "push" and fact.get("partial") is False
               for fact in facts)
    assert {fact["slowest_job"]["name"] for fact in facts} == {"publish-image"}
    assert {fact["next_longest_job"]["name"] for fact in facts} == {"verify"}
    assert lane.get("job_fetch_failures") == 0

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert "p50 **35m 00s**" in report
    assert "no complete commit-wide timing fact" not in report
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]


def test_unread_classic_protection_keeps_advisory_route_unconfirmed(
        monkeypatch, tmp_path: Path):
    class RulesetOnlyClient(_PushLaneClient):
        def __init__(self, classic, *, rulesets=True):
            super().__init__()
            self.classic = classic
            self.rulesets = rulesets

        def json(self, endpoint: str, allow_missing: bool = False):
            if endpoint == "repos/o/r/rulesets?includes_parents=true":
                self.queries += 1
                return [{"id": 9}] if self.rulesets else None
            if endpoint == "repos/o/r/rulesets/9":
                self.queries += 1
                return {
                    "enforcement": "active",
                    "target": "branch",
                    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}},
                    "rules": [{
                        "type": "required_status_checks",
                        "parameters": {
                            "required_status_checks": [{"context": "lint"}],
                        },
                    }],
                }
            if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
                self.queries += 1
                return self.classic
            return super().json(endpoint, allow_missing=allow_missing)

    source = _doc()
    source["workflow_job_graph"][_GATE]["merge-gate"]["continue_on_error"] = True
    client = RulesetOnlyClient(None)
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    doc = cr.collect(
        source, "o/r", max_runs=20, shallow_runs=20,
        created_before="2026-01-02T00:00:00Z")

    assert doc["required_checks"] == ["lint"]
    assert doc["required_checks_complete"] is True
    assert doc["required_checks_classic_read"] is False
    assert doc["required_checks_rulesets_read"] is True
    assert doc["required_checks_advice_complete"] is False
    assert not [row for row in doc["findings"] if row.get("pattern") == "OPT71"]

    report, findings_path, report_path = _write_artifacts(tmp_path, doc)
    assert "confirm it is not a required check" in report
    assert "OPT71 recipe (c)" not in report
    checks = vr.run_checks(report, report_path, findings_path, skill_repo=None)
    failures = [check for check in checks if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]

    mutated = copy.deepcopy(doc)
    mutated["required_checks_advice_complete"] = True
    (tmp_path / "mutated").mkdir()
    mutated_report, mutated_findings_path, mutated_report_path = _write_artifacts(
        tmp_path / "mutated", mutated)
    mutated_checks = vr.run_checks(
        mutated_report, mutated_report_path, mutated_findings_path, skill_repo=None)
    assert any(
        check.name == "required-check advice is backed by both protection reads"
        and not check.ok for check in mutated_checks)

    control_source = _doc()
    control_source["workflow_job_graph"][_GATE]["merge-gate"]["continue_on_error"] = True
    control_client = RulesetOnlyClient({"contexts": []})
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: control_client)
    control = cr.collect(
        control_source, "o/r", max_runs=20, shallow_runs=20,
        created_before="2026-01-02T00:00:00Z")
    assert control["required_checks_classic_read"] is True
    assert control["required_checks_rulesets_read"] is True
    assert control["required_checks_advice_complete"] is True
    (tmp_path / "control").mkdir()
    control_report, control_findings_path, control_report_path = _write_artifacts(
        tmp_path / "control", control)
    assert "OPT71 recipe (c)" in control_report
    control_checks = vr.run_checks(
        control_report, control_report_path, control_findings_path, skill_repo=None)
    control_failures = [
        check for check in control_checks if not check.skipped and not check.ok]
    assert control_failures == [
    ], [(check.name, check.detail) for check in control_failures]

    rulesets_unread_source = _doc()
    rulesets_unread_source["workflow_job_graph"][_GATE]["merge-gate"][
        "continue_on_error"] = True
    rulesets_unread_client = RulesetOnlyClient({"contexts": []}, rulesets=False)
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: rulesets_unread_client)
    rulesets_unread = cr.collect(
        rulesets_unread_source, "o/r", max_runs=20, shallow_runs=20,
        created_before="2026-01-02T00:00:00Z")
    assert rulesets_unread["required_checks_classic_read"] is True
    assert rulesets_unread["required_checks_rulesets_read"] is False
    assert rulesets_unread["required_checks_advice_complete"] is False

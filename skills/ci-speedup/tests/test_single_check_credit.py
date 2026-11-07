from __future__ import annotations

import importlib.util
import json
import subprocess
import re
import sys
from pathlib import Path


_SKILL = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
from wall_clock import (  # noqa: E402
    WallClockContext,
    bound_commit_projection,
    bound_measured_critical_path,
    size_wall_clock,
)


_VR_SPEC = importlib.util.spec_from_file_location(
    "single_check_credit_verify_report", Path(__file__).with_name("verify_report.py"))
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


_WORKFLOW = ".github/workflows/ci.yml"


def _iso(seconds: int) -> str:
    return f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}Z"


def _job(name: str, seconds: int, run_id: int) -> dict:
    return {
        "id": run_id * 10 + len(name),
        "name": name,
        "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
        "started_at": _iso(0),
        "completed_at": _iso(seconds),
        "steps": [{"name": "work", "started_at": _iso(0),
                   "completed_at": _iso(seconds)}],
    }


class _SingleCheckClient:
    gave_up = False
    complete_singletons = True
    include_siblings = True

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self.shas = [f"s{i}" for i in range(1, 11)]

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        self.queries += int(query)
        self.errors += int(error)

    def available(self) -> bool:
        return True

    def _runs(self) -> list[dict]:
        return [{
            "id": 100 + i,
            "event": "pull_request",
            "head_sha": sha,
            "status": "completed",
            "conclusion": "success",
            "created_at": _iso(0),
            "run_started_at": _iso(0),
            "updated_at": _iso(700),
        } for i, sha in enumerate(self.shas)]

    def _jobs(self, run_id: int) -> list[dict]:
        jobs = [_job("pole", 600, run_id)]
        if self.include_siblings and run_id >= 104:
            jobs.append(_job("sibling", 690, run_id))
        return jobs

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{"id": 1, "path": _WORKFLOW, "name": "ci"}]}
        match = re.match(r"repos/o/r/actions/workflows/1/runs\?(.*)", endpoint)
        if match:
            if re.search(r"per_page=1(?![0-9])", match.group(1)):
                return {"total_count": 30}
            return {"workflow_runs": self._runs()}
        match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if match:
            jobs = self._jobs(int(match.group(1)))
            return {"total_count": len(jobs), "jobs": jobs}
        match = re.match(r"repos/o/r/commits/(s\d+)/check-runs", endpoint)
        if match:
            index = int(match.group(1)[1:]) - 1
            checks = [{"name": "pole", "started_at": _iso(0),
                       "completed_at": _iso(600)}]
            if self.include_siblings and index >= 4:
                checks.append({"name": "sibling", "started_at": _iso(0),
                               "completed_at": _iso(690)})
            total = len(checks)
            if index < 4 and not self.complete_singletons:
                total += 1
            return {"total_count": total, "check_runs": checks}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": []}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


class _IncompleteSingletonClient(_SingleCheckClient):
    complete_singletons = False


class _OnlyCheckClient(_SingleCheckClient):
    include_siblings = False


_DOC = {
    "repo": "o/r",
    "scanned_at": "2026-11-06T00:00:00Z",
    "workflow_job_graph": {
        _WORKFLOW: {
            "pole": {"name": "pole", "needs": []},
            "sibling": {"name": "sibling", "needs": []},
        },
    },
    "findings": [{
        "id": "f1",
        "pattern": "OPT73",
        "title": "Shared work recurs across the cluster",
        "workflow_file": _WORKFLOW,
        "affected_jobs": ["pole"],
        "evidence": "the work step recurs across the concurrent jobs",
        "wall_clock_p50_s": 500.0,
        "runner_min_saving": 100.0,
        "sizing_basis": "measured",
        "tier": 1,
        "realization": "direct",
        "cluster_floor_lever": True,
        "cluster_legs_concurrent": True,
    }],
}


def _collect(monkeypatch, client_type: type[_SingleCheckClient]) -> dict:
    monkeypatch.setattr(cr, "GhClient", client_type)
    doc = cr.collect(json.loads(json.dumps(_DOC)), "o/r", max_runs=10, shallow_runs=10)
    doc["skill_commit_sha"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip()
    return doc


def _population_context(doc: dict) -> WallClockContext:
    cp = doc["pr_critical_path"]
    populations = tuple(
        (float(share), tuple((str(name), float(duration)) for name, duration in checks))
        for share, checks in cp["populations"]
    )
    return WallClockContext(
        pr_checks=populations[0][1],
        pr_check_populations=populations,
        pr_check_populations_complete=tuple(cp.get("population_check_runs_complete") or ()),
        own_check_names=frozenset({"pole"}),
        cluster_floor_lever=True,
    )


def _commit_context(doc: dict) -> WallClockContext:
    facts = doc["pr_critical_path"]["commit_wall_clock"]["facts"]
    return WallClockContext(commit_wall_facts=tuple(
        (
            float(fact["wall_s"]),
            float(fact["slowest_job"]["duration_s"]),
            float((fact.get("next_longest_job") or {}).get("duration_s") or 0.0),
        )
        for fact in facts
        if isinstance(fact.get("wall_s"), (int, float))
        and isinstance(fact.get("slowest_job"), dict)
    ))


def test_complete_single_check_prs_keep_the_full_saving(monkeypatch, tmp_path):
    doc = _collect(monkeypatch, _SingleCheckClient)
    cp = doc["pr_critical_path"]

    assert cp.get("population_check_runs_complete") == [True] * 10
    result = bound_measured_critical_path(500.0, _population_context(doc))
    assert result.value == 200.0
    assert result.reason is not None
    assert "0 on 6 of 10 PRs where `sibling` runs" in result.reason
    assert "up to 8m 20s on the other 4 of 10 PRs where it was the only check" in result.reason

    projected = bound_commit_projection(500.0, _commit_context(doc))
    assert projected.value == 254.0
    assert projected.reason is not None

    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    assert "4 of 10 PRs where it was the only check" in report

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

    floor_mutant = json.loads(json.dumps(doc))
    two_job_fact = floor_mutant["pr_critical_path"]["commit_wall_clock"]["facts"][4]
    pole_projection = next(
        row for row in two_job_fact["job_projections"] if row["name"] == "pole")
    pole_projection["independent_jobs"] = []
    floor_mutant_path = tmp_path / "floor-mutant.json"
    floor_mutant_path.write_text(json.dumps(floor_mutant), encoding="utf-8")
    floor_check = vr.check_commit_wall_clock_projection(report, floor_mutant_path)
    assert not floor_check.ok
    assert "independent floors do not re-derive" in floor_check.detail

    # Exercise the generic population-wide cap separately: no targeted job
    # means each commit's slowest job supplies the projection inputs.
    cap_doc = json.loads(json.dumps(doc))
    cap_finding = cap_doc["findings"][0]
    cap_finding["affected_jobs"] = []
    cap_finding["wall_clock_p50_s"] = projected.value
    cap_path = tmp_path / "cap-findings.json"
    cap_path.write_text(json.dumps(cap_doc), encoding="utf-8")
    assert vr.check_commit_wall_clock_projection(report, cap_path).ok

    monkeypatch.setattr(vr.statistics, "mean", vr.statistics.median)
    median_mutant = vr.check_commit_wall_clock_projection(report, cap_path)
    assert not median_mutant.ok
    assert "whole-commit cap 90.0" in median_mutant.detail


def test_collect_caps_a_non_slowest_target_against_its_own_job(monkeypatch):
    doc = _collect(monkeypatch, _SingleCheckClient)
    finding = doc["findings"][0]

    # On six commits `sibling` is slower than the targeted `pole`. Shortening
    # only `pole` cannot move those commits, so they contribute zero rather
    # than the slowest job's 90-second headroom.
    assert finding["affected_jobs"] == ["pole"]
    assert finding["wall_clock_p50_s"] == 200.0
    commit_bound = next(
        step for step in finding["wall_clock_derivation"]
        if step["bound"] == "commit-projection")
    assert commit_bound["from_s"] == 500.0
    assert commit_bound["to_s"] == 200.0


def test_collect_projects_every_job_changed_by_a_cluster_finding(monkeypatch):
    doc_in = json.loads(json.dumps(_DOC))
    doc_in["findings"][0]["affected_jobs"] = ["pole", "sibling"]
    monkeypatch.setattr(cr, "GhClient", _SingleCheckClient)

    doc = cr.collect(doc_in, "o/r", max_runs=10, shallow_runs=10)
    finding = doc["findings"][0]

    # On the six two-job commits the shared fix lowers both concurrent jobs.
    # The other affected job must not become the unchanged projection floor.
    assert finding["wall_clock_p50_s"] == 500.0


def test_population_bounds_share_one_basis_instead_of_weighting_twice():
    populations = tuple(
        [(0.1, (("pole", 600.0),))] * 4
        + [(0.1, (("sibling", 690.0), ("pole", 600.0)))] * 6)
    commit_facts = tuple(
        [(600.0, 600.0, 0.0, 600.0)] * 4
        + [(690.0, 600.0, 690.0, 600.0)] * 6)
    ctx = WallClockContext(
        pr_checks=populations[0][1],
        pr_check_populations=populations,
        pr_check_populations_complete=(True,) * 10,
        own_check_names=frozenset({"pole"}),
        commit_wall_facts=commit_facts,
    )

    result = size_wall_clock(
        500.0, ctx,
        cascade=[("commit-projection", bound_commit_projection),
                 ("measured-critical-path", bound_measured_critical_path)])

    assert result.effective_s == 200.0
    assert [step[0] for step in result.derivation] == ["commit-projection"]


def test_incomplete_single_check_reads_remain_unknown(monkeypatch):
    doc = _collect(monkeypatch, _IncompleteSingletonClient)
    cp = doc["pr_critical_path"]

    assert cp.get("population_check_runs_complete") == [False] * 4 + [True] * 6
    result = bound_measured_critical_path(500.0, _population_context(doc))
    assert result.value == 0.0
    assert result.reason is not None
    assert "unknown on 4 of 10 PRs with an incomplete check-runs read" in result.reason


def test_singleton_and_faster_sibling_ceilings_match_the_verifier(monkeypatch, tmp_path):
    doc = _collect(monkeypatch, _SingleCheckClient)
    cp = doc["pr_critical_path"]
    for population in cp["populations"][4:6]:
        sibling = next(check for check in population[1] if check[0] == "sibling")
        sibling[1] = 300.0

    graph = doc["workflow_job_graph"][_WORKFLOW]
    pole_node = graph.pop("pole")
    pole_node["name"] = "pole"
    graph["pole-key"] = pole_node
    for fact in cp["commit_wall_clock"]["facts"]:
        for row in fact.get("job_projections") or []:
            if row.get("node") == "pole":
                row["node"] = "pole-key"

    result = bound_measured_critical_path(500.0, _population_context(doc))
    assert result.value == 260.0
    assert result.reason is not None
    assert "up to 5m 00s on 2 of 10 PRs" in result.reason
    assert "up to 8m 20s on 4 of 10 PRs where it was the only check" in result.reason

    own_context = WallClockContext(commit_wall_facts=tuple(
        (float(fact["wall_s"]), 600.0,
         690.0 if index >= 4 else 0.0, 600.0)
        for index, fact in enumerate(cp["commit_wall_clock"]["facts"])
    ))
    projected = bound_commit_projection(result.value, own_context)
    assert projected.value == 104.0
    assert projected.reason is not None

    finding = doc["findings"][0]
    finding["affected_jobs"] = ["pole-key"]
    finding["wall_clock_p50_s"] = projected.value
    finding["wall_clock_uncapped_p50_s"] = 500.0
    finding["wall_clock_derivation"] = [
        {
            "bound": "measured-critical-path",
            "from_s": 500.0,
            "to_s": result.value,
            "reason": result.reason,
        },
        {
            "bound": "commit-projection",
            "from_s": result.value,
            "to_s": projected.value,
            "reason": projected.reason,
        },
    ]
    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    assert "up to 5m 00s on 2 of 10 PRs" in report
    assert "up to 10m 00s on 4 of 10 PRs where it was the only check" in report

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


def test_collect_sizing_credits_complete_single_workflow_prs(monkeypatch):
    """Through collect's own cascade, not a hand-built context: every sampled
    PR has only the finding's check, so complete reads keep the full raw saving.
    The incomplete-read control is pinned separately against the population
    bound, where the split is present and its unknown share can be re-derived."""
    complete = _collect(monkeypatch, _OnlyCheckClient)["findings"][0]
    assert complete["wall_clock_p50_s"] == 500.0

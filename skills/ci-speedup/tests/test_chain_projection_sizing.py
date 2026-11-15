"""Chain-aware whole-commit projection regression coverage."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
import sys
from types import SimpleNamespace

import wall_clock as wc


_VERIFY_PATH = Path(__file__).with_name("verify_report.py")


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "chain_projection_verify_report", _VERIFY_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _project_chain(
    *,
    wall_s: float,
    pole_s: float,
    runner_up_s: float,
    chain_s: float,
    speedup: float,
):
    """Exercise the production helper when present.

    The fallback is the pole-only projection used before chain facts reached
    the shared helper. Keeping that old behavior here makes the regression fail
    on its numeric assertion when run against an unchanged checkout.
    """
    project = getattr(wc, "project_commit_wall", None)
    if project is None:
        projected = round(
            wall_s - pole_s + max(pole_s / speedup, runner_up_s), 1
        )
        return SimpleNamespace(
            projected_s=projected,
            multiple=round(wall_s / projected, 2),
        )
    return project(
        wall_s,
        pole_s,
        runner_up_s,
        speedup,
        chain_s=chain_s,
        pole_s=pole_s,
        runner_up_s=runner_up_s,
    )


def test_serial_chain_projection_keeps_other_members_and_parallel_floor():
    # setup 120s -> test 900s -> report 60s. A 3x test speedup leaves the
    # other 180s of serialized work in place: 120 + 300 + 60 = 480s.
    serial_only = _project_chain(
        wall_s=1080.0,
        pole_s=900.0,
        runner_up_s=0.0,
        chain_s=1080.0,
        speedup=3.0,
    )
    assert serial_only.projected_s == 480.0
    assert serial_only.multiple == 2.25

    # A 600s independent path caps the whole commit at 600s. The old pole-only
    # formula adds that floor to the chain's other members and returns 780s.
    parallel_floor = _project_chain(
        wall_s=1080.0,
        pole_s=900.0,
        runner_up_s=600.0,
        chain_s=1080.0,
        speedup=3.0,
    )
    assert parallel_floor.projected_s == 600.0
    assert parallel_floor.multiple == 1.8

    # Sixty seconds elapsed outside the measured paths stays in the projected
    # wait. The path falls to 600s, then the unchanged queue/gap time is added.
    with_queue_gap = _project_chain(
        wall_s=1140.0,
        pole_s=900.0,
        runner_up_s=600.0,
        chain_s=1080.0,
        speedup=3.0,
    )
    assert with_queue_gap.projected_s == 660.0
    assert with_queue_gap.multiple == 1.73


def test_chainless_projection_keeps_the_single_job_formula():
    projection = wc.project_commit_wall(1500.0, 1440.0, 900.0, 4.0)
    assert projection.projected_s == 960.0
    assert projection.multiple == 1.56


def test_chain_cascade_does_not_round_the_projection_before_the_bound():
    ctx = wc.WallClockContext(
        pr_checks=(("test", 80.0),),
        own_check_names=frozenset({"test"}),
        chain_members=frozenset({"setup", "test"}),
        chain_p50_s=100.04,
        chain_win_s=60.0,
    )

    result = wc.bound_measured_critical_path(57.375, ctx)

    assert result.value == 57.4
    assert result.reason is None


def test_verifier_rederives_a_chained_projection_from_the_commit_fact(tmp_path: Path):
    verifier = _load_verifier()
    fact = {
        "sha": "abc123",
        "partial": False,
        "wall_s": 1140.0,
        "slowest_job": {"name": "test", "duration_s": 900.0},
        "next_longest_job": {"name": "parallel", "duration_s": 600.0},
        "slowest_chain_span_s": 1080.0,
        # 60 s outside both paths stays; the chain path falls to 405 s and is
        # floored at the 600 s independent path: 60 + 600 = 660 s.
        "projection_4x": {"job_speedup": 4.0, "projected_s": 660.0, "multiple": 1.73},
    }
    doc = {"pr_critical_path": {"commit_wall_clock": {
        "p50_s": 1140.0, "p90_s": 1140.0, "facts": [fact],
        "median_projection_multiple": 1.73,
        "worst_commit": {"sha": "abc123", "wall_s": 1140.0,
                         "workflow_file": ""},
    }}, "findings": []}
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    report = ("> **Per-commit PR wait:** p50 **19m 00s**, p90 **19m 00s**, from the "
              "earliest workflow run created to the last job completed across every "
              "workflow on that commit. At the sampled p50 commit, a 4x speedup to "
              "its slowest job projects to **11m 00s** (**1.73x**). Across commits, "
              "the median projection is **1.73x**. The worst sampled commit is "
              "`abc123` at **19m 00s**.\n")
    check = verifier.check_commit_wall_clock_projection(report, findings)
    assert check.ok, check.detail

    fact["projection_4x"].update(projected_s=840.0, multiple=1.36)
    findings.write_text(json.dumps(doc), encoding="utf-8")
    mutant = verifier.check_commit_wall_clock_projection(report, findings)
    assert not mutant.ok
    assert "840.0 != 660.0" in mutant.detail

    fact["slowest_chain_span_s"] = 800.0
    findings.write_text(json.dumps(doc), encoding="utf-8")
    shorter = verifier.check_commit_wall_clock_projection(report, findings)
    assert not shorter.ok
    assert "chain span 800.0 is shorter than its slowest job 900.0" in shorter.detail


# --- Through the collector: the commit stamp, the finding cap and the verifier ---

import collect_runs as cr  # noqa: E402
import blocking_path as bp  # noqa: E402
import test_commit_wall_clock as cw  # noqa: E402

_CHAIN_WF = ".github/workflows/first.yml"
_PARALLEL_WF = ".github/workflows/second.yml"


class _ChainClient(cw._TwoWorkflowClient):
    """setup 0-120 -> test 120-1020 -> report 1020-1080 in one workflow, and an
    independent 600 s job in another workflow on the same commit."""

    def json(self, endpoint: str, allow_missing: bool = False):
        match = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if match and not re.search(r"per_page=1(?![0-9])", match.group(2)):
            self.queries += 1
            workflow_id = int(match.group(1))
            return {"workflow_runs": [{
                "id": 101 if workflow_id == 1 else 202,
                "event": "pull_request",
                "head_sha": cw._SHA,
                "status": "completed",
                "conclusion": "success",
                "created_at": cw._iso(0),
                "run_started_at": cw._iso(0),
                "updated_at": cw._iso(1080 if workflow_id == 1 else 600),
            }]}
        match = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if match:
            self.queries += 1
            run_id = int(match.group(1))
            if run_id == 101:
                return {"jobs": [
                    cw._job("setup", run_id, 0, 120),
                    cw._job("test", run_id, 120, 1020),
                    cw._job("report", run_id, 1020, 1080),
                ]}
            if run_id == 202:
                return {"jobs": [cw._job("parallel", run_id, 0, 600)]}
            return {"jobs": []}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            self.queries += 1
            return {"check_runs": [
                {"name": "setup", "started_at": cw._iso(0),
                 "completed_at": cw._iso(120)},
                {"name": "test", "started_at": cw._iso(120),
                 "completed_at": cw._iso(1020)},
                {"name": "report", "started_at": cw._iso(1020),
                 "completed_at": cw._iso(1080)},
                {"name": "parallel", "started_at": cw._iso(0),
                 "completed_at": cw._iso(600)},
            ]}
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            self.queries += 1
            return {"contexts": ["report", "parallel"]}
        return super().json(endpoint, allow_missing)


_CHAIN_DOC = {
    "repo": "o/r",
    "skill_commit_sha": "installed:unversioned",
    "workflow_job_graph": {
        _CHAIN_WF: {
            "setup": {"name": "setup", "needs": []},
            "test": {"name": "test", "needs": ["setup"]},
            "report": {"name": "report", "needs": ["test"]},
        },
        _PARALLEL_WF: {"parallel": {"name": "parallel", "needs": []}},
    },
    "findings": [
        {"id": "chain", "workflow_file": _CHAIN_WF},
        {"id": "parallel", "workflow_file": _PARALLEL_WF},
    ],
}


def test_collector_projects_a_chained_slowest_job_along_its_chain(
        monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cr, "GhClient", _ChainClient)
    doc = cr.collect(json.loads(json.dumps(_CHAIN_DOC)), "o/r",
                     max_runs=1, shallow_runs=1)
    fact = doc["pr_critical_path"]["commit_wall_clock"]["facts"][0]
    assert fact["wall_s"] == 1080.0
    assert fact["slowest_job"]["name"] == "test"
    assert fact["next_longest_job"]["name"] == "parallel"
    # setup + test/4 + report = 405 s, floored by the 600 s independent path.
    # The single-job formula keeps setup and report AND adds the floor: 780 s.
    assert fact["projection_4x"]["projected_s"] == 600.0
    assert fact["projection_4x"]["multiple"] == 1.8

    report = bp.render(doc, captured_at="2026-11-06T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    verifier = _load_verifier()
    failures = [check for check in verifier.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]

    # A projection rewritten to the single-job formula fails re-derivation.
    fact["projection_4x"].update(projected_s=780.0, multiple=1.38)
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    check = verifier.check_commit_wall_clock_projection(report, findings_path)
    assert not check.ok
    assert "780.0 != 600.0" in check.detail


def test_commit_projection_bound_keeps_the_chain_floor():
    # Removing all 900 s of `test` leaves setup + report (180 s), floored by the
    # 600 s independent path: at most 480 s of wall clock comes back.
    ctx = wc.WallClockContext(commit_wall_facts=((1080.0, 900.0, 600.0, 1080.0),))
    assert wc.bound_commit_projection(900.0, ctx).value == 480.0
    # Chainless facts keep the single-job cap: 1500 - 1440 + 900 = 960 s, so 540 s back.
    legacy = wc.WallClockContext(commit_wall_facts=((1500.0, 1440.0, 900.0),))
    assert wc.bound_commit_projection(1440.0, legacy).value == 540.0

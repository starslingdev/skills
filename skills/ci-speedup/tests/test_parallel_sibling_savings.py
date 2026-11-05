from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402
from wall_clock import WallClockContext, bound_measured_critical_path  # noqa: E402


def _verify_report():
    path = Path(__file__).with_name("verify_report.py")
    spec = importlib.util.spec_from_file_location("parallel_sibling_verify_report", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _population_result():
    populations = (
        *((0.05, (("pole", 600.0), ("shard-matrix", 690.0))) for _ in range(12)),
        *((0.05, (("pole", 600.0), ("next-check", 240.0))) for _ in range(8)),
    )
    ctx = WallClockContext(
        workflow=".github/workflows/ci.yml",
        pr_checks=populations[0][1],
        pr_check_populations=populations,
        own_check_names=frozenset({"pole"}),
        cluster_floor_lever=True,
    )
    return bound_measured_critical_path(500.0, ctx), populations


def _doc(reason: str, populations) -> dict:
    return {
        "repo": "example/project",
        "scanned_at": "2026-11-05T00:00:00Z",
        "data_sources": {"runs_sampled": 20, "jobs_sampled": 40,
                         "workflows_analyzed": 1},
        "pr_critical_path": {
            "sampled_pr_count": 20,
            "sample_target": 20,
            "sample_complete": True,
            "critical_path_check": "pole",
            "critical_path_s": 600.0,
            "check_present_n_pr": 20,
            "populations": [[share, [[name, duration] for name, duration in checks]]
                            for share, checks in populations],
            "checks": [
                {"name": "pole", "p50_s": 600.0, "present_on": 20,
                 "pole_n": 8, "workflow_file": ".github/workflows/ci.yml"},
                {"name": "shard-matrix", "p50_s": 690.0, "present_on": 12,
                 "pole_n": 12, "workflow_file": ".github/workflows/shards.yml"},
                {"name": "next-check", "p50_s": 240.0, "present_on": 8,
                 "pole_n": 0, "workflow_file": ".github/workflows/next.yml"},
            ],
            "poles": [{
                "check": "pole", "p50_s": 600.0,
                "workflow_file": ".github/workflows/ci.yml", "job": "pole",
                "dominant_step": "test", "dominant_p50_s": 500.0,
                "steps": [{"step": "test", "category": "test", "p50_s": 500.0}],
            }],
        },
        "findings": [{
            "id": "cluster", "pattern": "OPT73",
            "title": "Shared test step recurs across the cluster",
            "workflow_file": ".github/workflows/ci.yml",
            "affected_jobs": ["pole"],
            "evidence": "the `test` step recurs across the concurrent legs",
            "wall_clock_p50_s": 144.0,
            "wall_clock_uncapped_p50_s": 500.0,
            "wall_clock_derivation": [{
                "bound": "measured-critical-path", "from_s": 500.0,
                "to_s": 144.0, "reason": reason,
            }],
            "cluster_floor_lever": True,
            "cluster_legs_concurrent": True,
            "runner_min_saving": 100.0,
            "tier": 1,
            "realization": "direct",
        }],
    }


def _clock_seconds(value: str) -> float:
    match = re.fullmatch(r"(?:(\d+)h )?(?:(\d+)m )?(\d+)s", value.strip())
    assert match, value
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def test_parallel_sibling_saving_is_disclosed_per_sampled_pr(tmp_path):
    result, populations = _population_result()
    assert result.value == 144.0
    assert result.reason is not None
    assert "0 on 12 of 20 PRs where `shard-matrix` runs" in result.reason
    assert "up to 6m 00s on the other 8 of 20 PRs" in result.reason
    assert "maximum per-PR saving is 360s" in result.reason

    doc = _doc(result.reason, populations)
    report = bp.render(doc)
    bottom = next(line for line in report.splitlines() if "**Bottom line.**" in line)
    assert "0 on 12 of 20 PRs where `shard-matrix` runs" in report
    assert "up to 6m 00s on the other 8 of 20 PRs" in report
    rendered = re.search(r"biggest single measured win is \*\*~([^*]+)\*\*", bottom)
    assert rendered
    assert _clock_seconds(rendered.group(1)) <= 360.0

    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    vr = _verify_report()
    checks = (
        vr.check_headline_cluster_lever_on_spine(report, findings_path),
        vr.check_headline_floor_presence_reconciled(report, findings_path),
    )
    assert all(check.ok for check in checks), [check.detail for check in checks]


def test_missing_sibling_observation_is_unknown_not_full_headroom():
    populations = (
        (0.5, (("pole", 600.0), ("known-floor", 240.0))),
        (0.5, (("pole", 600.0),)),
    )
    ctx = WallClockContext(
        pr_checks=populations[0][1],
        pr_check_populations=populations,
        own_check_names=frozenset({"pole"}),
    )
    result = bound_measured_critical_path(500.0, ctx)
    assert result.value == 180.0
    assert result.reason is not None
    assert "unknown on 1 of 2 PRs" in result.reason
    assert "up to 6m 00s on 1 of 2 PRs" in result.reason


def test_split_does_not_name_a_slow_check_that_misses_most_zero_saving_prs():
    pole = {
        "check": "pole", "p50_s": 600.0,
        "_cooccur": {"next-check": 8}, "_gating_n": 8,
    }
    candidates = [
        {"name": "pole", "p50_s": 600.0, "present_on": 20,
         "workflow_file": ".github/workflows/ci.yml"},
        {"name": "rare-slow", "p50_s": 690.0, "present_on": 4,
         "workflow_file": ".github/workflows/rare.yml"},
        {"name": "next-check", "p50_s": 240.0, "present_on": 8,
         "workflow_file": ".github/workflows/next.yml"},
    ]
    note = "\n".join(bp._floor_note(pole, candidates))
    assert "0 on 12 of 20 PRs where another check runs longer" in note
    assert "0 on 12 of 20 PRs where `rare-slow` runs" not in note


def _varying_floor_report(tmp_path: Path):
    populations = (
        *((0.05, (("pole", 600.0), ("shard-matrix", 690.0))) for _ in range(12)),
        *((0.05, (("pole", 600.0), ("next-check", 590.0))) for _ in range(8)),
    )
    ctx = WallClockContext(
        pr_checks=populations[0][1],
        pr_check_populations=populations,
        own_check_names=frozenset({"pole"}),
        cluster_floor_lever=True,
    )
    result = bound_measured_critical_path(500.0, ctx)
    assert result.value == 4.0 and result.reason is not None
    assert "maximum per-PR saving is 10s" in result.reason
    doc = _doc(result.reason, populations)
    doc["findings"][0]["wall_clock_p50_s"] = result.value
    doc["findings"][0]["wall_clock_derivation"][0]["to_s"] = result.value
    report = bp.render(doc)
    findings_path = tmp_path / "varying-floor-findings.json"
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    return report, findings_path


def test_headline_never_exceeds_varying_per_pr_maximum(tmp_path):
    report, _findings_path = _varying_floor_report(tmp_path)
    bottom = next(line for line in report.splitlines() if "**Bottom line.**" in line)
    rendered = re.search(r"biggest single measured win is \*\*~([^*]+)\*\*", bottom)
    assert rendered
    assert _clock_seconds(rendered.group(1)) <= 10.0


def test_verifiers_actually_validate_the_varying_per_pr_split(tmp_path):
    report, findings_path = _varying_floor_report(tmp_path)
    vr = _verify_report()
    checks = (
        vr.check_headline_cluster_lever_on_spine(report, findings_path),
        vr.check_headline_floor_presence_reconciled(report, findings_path),
    )
    assert all(check.ok and not check.skipped for check in checks), [
        {"ok": check.ok, "skipped": check.skipped, "detail": check.detail}
        for check in checks
    ]


def test_split_denominator_includes_sampled_prs_where_the_pole_did_not_run():
    populations = (
        *((0.1, (("pole", 600.0), ("next-check", 240.0))) for _ in range(4)),
        *((0.1, (("pole", 600.0), ("blocker", 690.0))) for _ in range(2)),
        *((0.1, (("other-path", 300.0),)) for _ in range(4)),
    )
    result = bound_measured_critical_path(500.0, WallClockContext(
        pr_checks=populations[0][1],
        pr_check_populations=populations,
        own_check_names=frozenset({"pole"}),
    ))
    doc = _doc(result.reason or "", populations)
    cp = doc["pr_critical_path"]
    cp["sampled_pr_count"] = cp["sample_target"] = cp["check_present_n_pr"] = 10
    cp["checks"] = [
        {"name": "pole", "p50_s": 600.0, "present_on": 6,
         "pole_n": 4, "workflow_file": ".github/workflows/ci.yml"},
        {"name": "blocker", "p50_s": 690.0, "present_on": 2,
         "pole_n": 2, "workflow_file": ".github/workflows/blocker.yml"},
        {"name": "next-check", "p50_s": 240.0, "present_on": 4,
         "pole_n": 0, "workflow_file": ".github/workflows/next.yml"},
        {"name": "other-path", "p50_s": 300.0, "present_on": 4,
         "pole_n": 4, "workflow_file": ".github/workflows/other.yml"},
    ]
    report = bp.render(doc)
    split = next(line for line in report.splitlines() if "Per-PR split:" in line)
    assert "0 on 6 of 10 PRs" in split
    assert "up to 6m 00s on the other 4 of 10 PRs" in split


def test_matrix_shared_fix_counts_prs_where_a_different_leg_wins():
    populations = (
        *((0.1, (("unit (a)", 600.0), ("unit (b)", 590.0), ("lint", 240.0)))
          for _ in range(5)),
        *((0.1, (("unit (a)", 590.0), ("unit (b)", 600.0), ("lint", 240.0)))
          for _ in range(5)),
    )
    doc = _doc("matrix shared fix", populations)
    cp = doc["pr_critical_path"]
    cp["sampled_pr_count"] = cp["sample_target"] = cp["check_present_n_pr"] = 10
    cp["critical_path_check"] = "unit (a)"
    cp["critical_path_s"] = 600.0
    cp["checks"] = [
        {"name": "unit (a)", "p50_s": 600.0, "present_on": 10,
         "pole_n": 5, "workflow_file": ".github/workflows/ci.yml"},
        {"name": "unit (b)", "p50_s": 600.0, "present_on": 10,
         "pole_n": 5, "workflow_file": ".github/workflows/ci.yml"},
        {"name": "lint", "p50_s": 240.0, "present_on": 10,
         "pole_n": 0, "workflow_file": ".github/workflows/lint.yml"},
    ]
    cp["poles"] = [{
        "check": "unit (a)", "p50_s": 600.0,
        "workflow_file": ".github/workflows/ci.yml", "job": "unit",
        "dominant_step": "test", "dominant_p50_s": 500.0,
        "steps": [{"step": "test", "category": "test", "p50_s": 500.0}],
    }]
    doc["findings"] = []
    report = bp.render(doc)
    matrix_note = next(line for line in report.splitlines()
                       if "change that speeds *every* leg at once" in line)
    assert "up to 6m 00s on 10 of 10 PRs" in matrix_note
    assert "0 on 5 of 10 PRs" not in matrix_note

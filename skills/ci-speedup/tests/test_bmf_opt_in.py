"""Default runner-minute filtering and the explicit wall-clock-only opt-in."""
from __future__ import annotations

import sys
import json
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import blocking_path as bp  # noqa: E402
import summary  # noqa: E402
import verify_report as vr  # noqa: E402


def _opt83_separate_job() -> dict:
    return {
        "id": "opt83-added-cost",
        "pattern": "OPT83",
        "workflow_file": ".github/workflows/ci.yml",
        "affected_jobs": ["test"],
        "runner_minute_effect": {
            "direction": "added_cost",
            "runner_min_per_run": 1.5,
        },
        "independent_steps": {
            "kind": "opt83_separate_jobs",
            "job": "test",
            "yaml_job": "test",
            "ceiling_s": 90.0,
            "setup_s": 30.0,
            "moved_step": "integration tests",
            "steps": [
                {"step": "unit tests", "p50_s": 120.0},
                {"step": "integration tests", "p50_s": 110.0},
            ],
        },
    }


def test_default_holds_added_cost_and_setup_repeating_levers():
    saving = {"pattern": "OPT46", "runner_minute_effect": {"direction": "saving"}}
    opt24 = {"pattern": "OPT24", "runner_minute_effect": {
        "direction": "unsized", "reason": "the repeated setup floor is unavailable"}}
    opt83 = _opt83_separate_job()
    opt81 = {"pattern": "OPT81", "faster_runner": {"half": "A2"}}
    matrix_split = {"pattern": "OPTX", "title": "Split a matrix into sibling jobs",
                    "runner_minute_effect": {"direction": "unsized",
                                             "reason": "setup was not measured"}}

    assert bp._bmf_held_back(saving) is False
    assert bp._bmf_held_back(opt24) is True
    assert bp._bmf_held_back(opt83) is True
    assert bp._bmf_held_back(opt81) is True
    assert bp._bmf_held_back(matrix_split) is True


def test_default_omits_opt83_recommendation_but_bmf_restores_signed_cost():
    finding = _opt83_separate_job()
    held = {**finding, "_bmf_held_back": True}

    assert bp._opt83_findings([held]) == []
    assert bp._opt83_findings([finding]) == [finding]
    card = "\n".join(bp._opt83_card(finding, "https://example.invalid/catalog"))
    assert "added cost, at least +1.50 runner-min per run" in card


def test_render_command_carries_bmf_opt_in():
    doc = {
        "scanned_at": "2026-11-20T00:00:00Z",
        "data_bundle": {
            "logs_dir": "/tmp/data",
            "logs": [{
                "check": "test",
                "workflow_file": ".github/workflows/ci.yml",
                "file": "test.log",
            }],
        },
    }
    default = summary.render_command(doc)
    opted_in = summary.render_command(doc, bmf=True)

    assert "--bmf" not in default
    assert "--bmf" in opted_in


def test_report_holds_both_fixture_levers_and_bmf_restores_them():
    opt83 = _opt83_separate_job()
    opt83.update(title="Split independent checks", affected_jobs=["test"])
    opt24 = {
        "id": "opt24-shard",
        "pattern": "OPT24",
        "severity": "HIGH",
        "title": "Long Test Job Without Sharding",
        "workflow_file": ".github/workflows/other.yml",
        "affected_jobs": ["slow-test"],
        "runner_min_saving": 0,
        "wall_clock_p50_s": 90.0,
    }
    doc = {
        "repo": "demo/repo",
        "scanned_at": "2026-11-20T00:00:00Z",
        "pr_critical_path": {
            "sampled_pr_count": 10,
            "sample_target": 10,
            "sample_complete": True,
            "critical_path_check": "test",
            "critical_path_s": 300.0,
            "check_present_n_pr": 10,
            "checks": [
                {"name": "test", "p50_s": 300.0, "present_on": 10,
                 "workflow_file": ".github/workflows/ci.yml"},
                {"name": "slow-test", "p50_s": 180.0, "present_on": 10,
                 "workflow_file": ".github/workflows/other.yml"},
            ],
            "populations": [[0.1, [["test", 300.0], ["slow-test", 180.0]]]
                            for _ in range(10)],
            "poles": [{
                "check": "test",
                "job": "test",
                "p50_s": 300.0,
                "workflow_file": ".github/workflows/ci.yml",
                "steps": [
                    {"step": "unit tests", "category": "test", "p50_s": 120.0},
                    {"step": "integration tests", "category": "test", "p50_s": 110.0},
                ],
            }],
        },
        "findings": [opt24, opt83],
    }

    default = bp.render(doc)
    opted_in = bp.render(doc, bmf=True)

    assert "enabled=false held=2" in default
    assert "2 wall-clock levers held back" in default
    assert "OPT24" not in default
    assert '<a id="opt83-opt83-added-cost"></a>' not in default
    assert "OPT24" in opted_in
    assert '<a id="opt83-opt83-added-cost"></a>' in opted_in
    assert "added cost, at least +1.50 runner-min per run" in opted_in


def test_verifier_rejects_a_default_report_that_recommends_a_held_lever(tmp_path):
    finding = _opt83_separate_job()
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": [finding]}), encoding="utf-8")
    card = "\n".join(bp._opt83_card(finding, "https://example.invalid/catalog"))
    bad = ("# report\n\n<!-- ci-speedup:bmf enabled=false held=1 -->\n\n"
           "1 wall-clock lever held back because they add runner minutes or change "
           "to an unpriced runner; rerun with `--bmf` to show them.\n\n" + card)
    good = bad.replace("enabled=false held=1", "enabled=true held=0")

    assert vr.check_bmf_runner_minute_filter(bad, path).ok is False
    assert vr.check_bmf_runner_minute_filter(good, path).ok is True

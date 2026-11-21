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
    # A title that names a split is not a mechanism: only enumerated patterns hold.
    assert bp._bmf_held_back(matrix_split) is False


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
           "1 wall-clock lever held back because it adds runner minutes or changes "
           "to an unpriced runner; rerun with `--bmf` to show it.\n\n" + card)
    good = bad.replace("enabled=false held=1", "enabled=true held=0")

    assert vr.check_bmf_runner_minute_filter(bad, path).ok is False
    assert vr.check_bmf_runner_minute_filter(good, path).ok is True


# --- Review pass: every lever that can add runner minutes, and only those -------

def test_unsized_work_removing_lever_with_matrix_in_its_title_is_not_held():
    # OPT39 path-filters a multi-language matrix: it removes work. Its title
    # names a matrix, which is not a split; an unsized measurement must not
    # turn a skip into a held-back wall-clock lever.
    opt39 = {"pattern": "OPT39", "title": "Multi-Language Matrix Without Path Filter",
             "fix_strategy": "multi-language-matrix-path-filter",
             "runner_minute_effect": {"direction": "unsized",
                                      "reason": "the changed-path lookup failed"}}
    assert bp._bmf_held_back(opt39) is False
    assert vr._vr_bmf_held_back(opt39) is False


def test_dropped_needs_edge_that_adds_measured_runner_minutes_is_held():
    adds = {"pattern": "OPT21", "opt21_edge_sizing": {"extra_runner_min_per_run": 2.5}}
    level = {"pattern": "OPT21", "opt21_edge_sizing": {"extra_runner_min_per_run": 0.0}}
    unmeasured = {"pattern": "OPT21"}
    for predicate in (bp._bmf_held_back, vr._vr_bmf_held_back):
        assert predicate(adds) is True
        assert predicate(level) is False
        assert predicate(unmeasured) is False


def test_split_leg_and_own_job_recipes_are_held_but_rebalancing_is_not():
    split_leg = {"pattern": "OPT25", "title": "Matrix Leg Imbalance",
                 "opt25_case": "split-leg"}
    rebalance = {"pattern": "OPT25", "title": "Shard Imbalance", "opt25_case": "rebalance"}
    atomic = {"pattern": "OPT25", "title": "Oversized Test or File in Shard Family",
              "opt25_case": "atomic-unit"}
    own_job = {"pattern": "OPT97", "title": "Queue Caused by a One-at-a-Time Resource"}
    for predicate in (bp._bmf_held_back, vr._vr_bmf_held_back):
        assert predicate(split_leg) is True
        assert predicate(own_job) is True
        assert predicate(rebalance) is False
        assert predicate(atomic) is False


def _cargo_pole_report(bmf: bool) -> str:
    from test_blocking_path import _CARGO_SERIAL_LOG, _doc_one_pole
    return bp.render(_doc_one_pole(), {"pipeline": _CARGO_SERIAL_LOG}, {}, {},
                     "2026-06-08", bmf=bmf)


def _prompt_lines(report: str, heading: str) -> list[str]:
    lines = report.splitlines()
    i = lines.index(heading)
    out = []
    for line in lines[i + 1:]:
        if not line.startswith("- "):
            break
        out.append(line)
    return out


def test_default_keeps_the_shard_only_pole_drill_but_holds_its_remedy():
    default = _cargo_pole_report(False)
    opted_in = _cargo_pole_report(True)

    # The bottleneck analysis still shows where the time goes.
    for report in (default, opted_in):
        assert "<!-- ci-speedup:leaf-crown fix_key=cargo-test-shard -->" in report
        assert "WHERE THE TIME GOES" in report and "THE MEASURED CAUSE" in report
    deliver = " ".join(_prompt_lines(default, "DELIVER & VERIFY"))
    assert "Shard the suite across parallel jobs" not in default
    assert "`--bmf`" in deliver
    assert "only then add shards or jobs" not in default
    assert "enabled=false held=1" in default
    assert ("1 wall-clock lever held back because it adds runner minutes or "
            "changes to an unpriced runner; rerun with `--bmf` to show it.") in default
    assert "Shard the suite across parallel jobs" in opted_in
    assert "Extra shard jobs repeat checkout and setup and bill each job" in opted_in
    assert "enabled=true held=0" in opted_in


def test_default_drops_the_cross_job_alternative_from_mixed_leaf_remedies():
    from test_blocking_path import _PYTEST_SERIAL_LOG, _doc_one_pole
    default = bp.render(_doc_one_pole(), {"pipeline": _PYTEST_SERIAL_LOG}, {}, {},
                        "2026-06-08")
    opted_in = bp.render(_doc_one_pole(), {"pipeline": _PYTEST_SERIAL_LOG}, {}, {},
                         "2026-06-08", bmf=True)
    assert "-n logical" in default
    assert "matrix jobs" not in default
    assert "shard the suite across matrix jobs" in opted_in
    assert "enabled=false held=1" in default


def test_verifier_rejects_a_default_report_whose_leaf_prompt_still_shards(tmp_path):
    from test_blocking_path import _doc_one_pole
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(_doc_one_pole()), encoding="utf-8")
    default = _cargo_pole_report(False)
    assert vr.check_bmf_runner_minute_filter(default, path).ok is True
    tampered = default.replace(
        "\nDELIVER & VERIFY\n- ",
        "\nDELIVER & VERIFY\n- Shard the suite across parallel jobs. ")
    assert vr.check_bmf_runner_minute_filter(tampered, path).ok is False
    assert vr.check_bmf_runner_minute_filter(_cargo_pole_report(True), path).ok is True


def test_verifier_rejects_a_default_report_that_renders_a_held_opt24_card(tmp_path):
    opt24 = {"id": "s1", "pattern": "OPT24", "title": "Long Test Job Without Sharding",
             "evidence": "job `slow-test` p50 900s over 4 runs, no shard axis observed"}
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": [opt24]}), encoding="utf-8")
    head = ("# report\n\n<!-- ci-speedup:bmf enabled=false held=1 -->\n\n"
            "1 wall-clock lever held back because it adds runner minutes or changes "
            "to an unpriced runner; rerun with `--bmf` to show it.\n\n")
    assert vr.check_bmf_runner_minute_filter(head, path).ok is True
    card = ("<summary><strong>OPT24 - Long Test Job Without Sharding</strong></summary>\n"
            "**Evidence:** job `slow-test` p50 900s over 4 runs, no shard axis observed\n")
    assert vr.check_bmf_runner_minute_filter(head + card, path).ok is False


def test_verifier_requires_the_mode_marker_when_a_lever_would_be_held(tmp_path):
    opt24 = {"id": "s1", "pattern": "OPT24", "title": "Long Test Job Without Sharding"}
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": [opt24]}), encoding="utf-8")
    assert vr.check_bmf_runner_minute_filter("# report\n", path).ok is False
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"findings": []}), encoding="utf-8")
    assert vr.check_bmf_runner_minute_filter("# report\n", empty).skipped is True


def _option_report(finding: dict, *, bmf: bool) -> str:
    return bp.render({
        "repo": "demo/repo",
        "scanned_at": "2026-11-21T00:00:00Z",
        "findings": [finding],
    }, bmf=bmf)


def test_opt89_native_runner_sharding_is_held_by_default_and_signed_under_bmf(
        tmp_path):
    finding = {
        "id": "opt89-static",
        "pattern": "OPT89",
        "severity": "HIGH",
        "title": "The slowest platform compiles its own tests",
        "workflow_file": ".github/workflows/ci.yml",
        "affected_jobs": ["arm"],
        "evidence": (
            "job `arm` compiles via `make` inside a QEMU-backed docker run "
            "for `linux/arm64` on a hosted x64 runner"
        ),
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps({"findings": [finding]}), encoding="utf-8")

    default = _option_report(finding, bmf=False)
    opted_in = _option_report(finding, bmf=True)

    assert "enabled=false held=1" in default
    assert "shard the test run across native runners" not in default
    assert "shard the test run across native runners" in opted_in
    assert "Runner-minute effect: added cost, unsized" in opted_in
    assert vr.check_bmf_runner_minute_filter(default, findings_path).ok is True
    assert vr.check_bmf_runner_minute_filter(opted_in, findings_path).ok is True


def test_opt97_bmf_own_job_recipe_states_added_runner_minute_cost(tmp_path):
    finding = {
        "id": "opt97-queue",
        "pattern": "OPT97",
        "severity": "MEDIUM",
        "title": "Queue Caused by a One-at-a-Time Resource",
        "workflow_file": ".github/workflows/ci.yml",
        "affected_jobs": ["test"],
        "evidence": "job `test` has P90 queue 8m20s with a single-slot resource",
        "wall_clock_p50_s": 120.0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps({"findings": [finding]}), encoding="utf-8")

    opted_in = _option_report(finding, bmf=True)

    assert "Runner-minute effect: added cost, unsized" in opted_in
    assert "No runner-minute saving" not in opted_in
    assert vr.check_bmf_runner_minute_filter(opted_in, findings_path).ok is True

    tampered = opted_in.replace(
        "Runner-minute effect: added cost, unsized",
        "No runner-minute saving",
    )
    assert vr.check_bmf_runner_minute_filter(tampered, findings_path).ok is False


def test_default_pre_start_wait_section_omits_a_held_opt97_card(tmp_path):
    # A measured OPT97 carries positive savable wait, so it would render in the
    # pre-start wait section; the default mode holds it, so the card must go.
    finding = {
        "id": "opt97-queue",
        "pattern": "OPT97",
        "severity": "MEDIUM",
        "title": "Queue Caused by a One-at-a-Time Resource",
        "workflow_file": ".github/workflows/ci.yml",
        "affected_jobs": ["test"],
        "evidence": "job `test` has P90 queue 8m20s with a single-slot resource",
        "wall_clock_p50_s": 120.0,
    }
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps({"findings": [finding]}), encoding="utf-8")

    default = _option_report(finding, bmf=False)

    assert "enabled=false held=1" in default
    assert "<strong>OPT97 - " not in default
    assert finding["evidence"] not in default
    assert vr.check_bmf_runner_minute_filter(default, findings_path).ok is True
    assert "<strong>OPT97 - " in _option_report(finding, bmf=True)

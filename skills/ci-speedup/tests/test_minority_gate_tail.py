"""A required check's minority slow tail stays visible without claiming savings."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402

from test_blocking_path import _doc_one_pole  # noqa: E402


def _load_verify_report():
    spec = importlib.util.spec_from_file_location(
        "minority_tail_verify_report", HERE / "verify_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tail() -> dict:
    observations = [
        {"duration_s": 60.0, "url": f"https://github.com/o/r/actions/runs/{100000 + i}"}
        for i in range(85)
    ]
    observations += [
        {"duration_s": 1500.0,
         "url": f"https://github.com/o/r/actions/runs/{200000 + i}"}
        for i in range(15)
    ]
    return {
        "n": 100,
        "p50_s": 60.0,
        "p90_s": 1500.0,
        "tail_n": 15,
        "tail_share_pct": 15.0,
        "example_url": observations[-1]["url"],
        "observations": observations,
    }


def _job(duration_s: int, run_id: int) -> dict:
    minutes, seconds = divmod(duration_s, 60)
    return {
        "name": "affected-unit", "labels": ["ubuntu-latest"],
        "started_at": "2026-11-05T10:00:00Z",
        "completed_at": f"2026-11-05T10:{minutes:02d}:{seconds:02d}Z",
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


def test_critical_path_stamps_the_tail_from_all_sampled_pr_observations():
    runs = [[_job(60, 100000 + i)] for i in range(85)]
    runs += [[_job(1500, 200000 + i)] for i in range(15)]

    tail = cr._critical_path(runs)["job_minority_tail"]["affected-unit"]
    assert tail["p50_s"] == 60.0
    assert tail["p90_s"] == 1500.0
    assert tail["tail_n"] == 15
    assert tail["tail_share_pct"] == 15.0
    assert tail["example_url"].endswith("200000")
    assert len(tail["observations"]) == 100


def test_critical_path_does_not_stamp_a_smooth_distribution():
    runs = [[_job(60 + i % 31, 300000 + i)] for i in range(100)]
    assert not cr._critical_path(runs)["job_minority_tail"]


def test_runner_label_transition_does_not_hide_the_tail():
    fast = [[dict(_job(60, 400000 + i), labels=["runner-a"])] for i in range(85)]
    slow = [[dict(_job(1500, 500000 + i), labels=["runner-b"])] for i in range(15)]
    tail = cr._critical_path(fast + slow)["job_minority_tail"]["affected-unit"]
    assert tail["tail_share_pct"] == 15.0


def test_multiple_commits_on_one_pr_count_as_one_sampled_pr():
    runs = []
    for pr_number in range(1, 86):
        job = dict(_job(60, 800000 + pr_number),
                   _run_pr_number=pr_number,
                   _run_created_at="2026-11-05T10:00:00Z")
        runs.append([job])
    for pr_number in range(86, 101):
        job = dict(_job(1500, 800000 + pr_number),
                   _run_pr_number=pr_number,
                   _run_created_at="2026-11-05T10:00:00Z")
        runs.append([job])
    for pr_number in range(1, 11):
        old = dict(_job(1500, 900000 + pr_number),
                   _run_pr_number=pr_number,
                   _run_created_at="2026-11-04T10:00:00Z")
        runs.append([old])

    tail = cr._critical_path(runs)["job_minority_tail"]["affected-unit"]
    assert tail["n"] == 100
    assert tail["tail_share_pct"] == 15.0


def test_production_attachment_maps_only_required_closure_checks():
    crit = cr._critical_path(
        [[_job(60, 600000 + i)] for i in range(85)]
        + [[_job(1500, 700000 + i)] for i in range(15)])
    crit_by_wf = {".github/workflows/pipeline.yml": crit}
    checks = [("affected-unit", 60.0)]

    attached = cr._minority_tails_for_checks(checks, crit_by_wf, True)
    assert attached["affected-unit"]["tail_share_pct"] == 15.0
    assert cr._minority_tails_for_checks(checks, crit_by_wf, False) == {}


def _doc(*, scoped: bool = True, tail: dict | None = None) -> dict:
    doc = _doc_one_pole()
    cp = doc["pr_critical_path"]
    cp["spine_required_scoped"] = scoped
    cp["check_present_n_pr"] = 100
    cp["critical_path_check"] = "affected-unit"
    cp["critical_path_s"] = 60.0
    cp["checks"] = [{
        "name": "affected-unit", "p50_s": 60.0, "present_on": 100,
        "pole_n": 100, "workflow_file": ".github/workflows/pipeline.yml",
        **({"minority_tail": tail} if tail else {}),
    }]
    pole = cp["poles"][0]
    pole.update({"check": "affected-unit", "job": "affected-unit", "p50_s": 60.0})
    doc["required_checks"] = ["affected-unit"]
    return doc


def test_required_check_minority_tail_renders_and_verifies(tmp_path: Path):
    doc = _doc(tail=_tail())
    report = bp.render(doc, captured_at="2026-11-05T00:00:00Z")

    assert "<!-- minority-gate-tail:affected-unit -->" in report
    assert "~15% of PRs" in report
    assert "25m" in report
    assert "those PRs wait on it" in report
    assert "200014" in report
    assert "No saving is credited" in report

    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    check = _load_verify_report().check_minority_gate_tails(report, findings)
    assert check.ok and not check.skipped, check


def test_uniform_required_check_has_no_minority_tail_finding():
    doc = _doc(tail=None)
    report = bp.render(doc, captured_at="2026-11-05T00:00:00Z")
    assert "minority-gate-tail:" not in report


def test_tail_outside_required_closure_has_no_own_finding():
    doc = _doc(scoped=False, tail=_tail())
    doc["required_checks"] = ["merge-gate"]
    report = bp.render(doc, captured_at="2026-11-05T00:00:00Z")
    assert "minority-gate-tail:" not in report


def test_tail_marker_neutralizes_check_name_markup():
    doc = _doc(tail=_tail())
    check = doc["pr_critical_path"]["checks"][0]
    check["name"] = "affected --> unit"
    marker = next(line for line in bp.render(doc).splitlines()
                  if "minority-gate-tail:" in line)
    assert marker.count("-->") == 1


def test_scope_prefixed_check_marker_renders_and_verifies(tmp_path: Path):
    doc = _doc(tail=_tail())
    doc["pr_critical_path"]["checks"][0]["name"] = "@scope/affected-unit"
    report = bp.render(doc)
    assert "<!-- minority-gate-tail:affected-unit -->" in report
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    check = _load_verify_report().check_minority_gate_tails(report, findings)
    assert check.ok, check


def test_verifier_binds_no_saving_disclosure_to_each_tail(tmp_path: Path):
    doc = _doc(tail=_tail())
    second = dict(doc["pr_critical_path"]["checks"][0])
    second["name"] = "affected-integration"
    second["minority_tail"] = dict(_tail(), example_url=(
        "https://github.com/o/r/actions/runs/200013"))
    doc["pr_critical_path"]["checks"].append(second)
    report = bp.render(doc)
    marker = "<!-- minority-gate-tail:affected-integration -->"
    before, after = report.split(marker, 1)
    after = after.replace("No saving is credited", "Saving is unknown", 1)
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    check = _load_verify_report().check_minority_gate_tails(before + marker + after, findings)
    assert not check.ok

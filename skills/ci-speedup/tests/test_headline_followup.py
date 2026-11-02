"""The headline's next gate and measurement instructions survive rendering."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import blocking_path as bp
from wall_clock import WallClockContext, size_wall_clock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("followup_verify", HERE / "verify_report.py")
vr = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = vr
spec.loader.exec_module(vr)


def document():
    return {
        "repo": "example/project", "repo_visibility": "public",
        "scanned_at": "2026-11-02T00:00:00Z", "commit_sha": "abcdef1234567890",
        "skill_commit_sha": "abcdef1234567890",
        "pr_critical_path": {
            "critical_path_check": "tests", "sampled_pr_count": 4,
            "sample_target": 4, "check_present_n_pr": 4,
            "checks": [
                {"name": "tests", "workflow_file": ".github/workflows/test.yml",
                 "p50_s": 600, "present_on": 4, "pole_n": 4},
                {"name": "lint", "workflow_file": ".github/workflows/quality.yml",
                 "p50_s": 350, "present_on": 4, "pole_n": 0}],
            "poles": [{"check": "tests", "job": "tests", "p50_s": 600,
                       "workflow_file": ".github/workflows/test.yml"}],
        },
        "findings": [{"id": "f1", "pattern": "OPT25", "pattern_class": "data-driven",
                      "title": "Shard imbalance", "severity": "MEDIUM",
                      "workflow_file": ".github/workflows/test.yml", "affected_jobs": ["tests"],
                      "wall_clock_p50_s": 250, "runner_min_saving": None,
                      "evidence": "One shard gates the matrix.",
                      "measured_signal": "One shard gates the matrix.",
                      "realization": "direct", "fix_strategy": "shard-imbalance"}],
    }


def render(doc=None):
    return bp.render(doc or document(), captured_at="2026-11-02T00:00:00Z")


def normalized(report):
    report = "\n".join(line.rstrip() for line in report.split("\n"))
    return re.sub(r"\(skill commit `[^`]*`(?:, scripts tree `[^`]*`)?\)",
                  "(skill commit `NORMALIZED`)", report)


def test_headline_followup_golden_report():
    report = render()
    assert "After this fix, CI is gated by `quality.yml / lint` at ~350 s." in report
    assert normalized(report) == (HERE / "fixtures/headline-followup.md").read_text()


def test_headline_followup_verifier_is_registered_and_rejects_missing_content(tmp_path):
    report = render()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(document()))
    check = vr.check_headline_followup(report, findings)
    assert check.ok and not check.skipped
    assert check.name in {c.name for c in vr.run_checks(report, tmp_path / "report.md", findings, None)}
    for fragment in ["What gates next:", "This projection assumes", "How to check it worked", "**Measure:**",
                     "**Runs:**", "**Don't**", "**Caches:**"]:
        damaged = "\n".join(line for line in report.splitlines() if fragment not in line)
        assert not vr.check_headline_followup(damaged, findings).ok, fragment


@pytest.mark.parametrize("bad", [
    "After this fix, CI is gated by `quality.yml / nonexistent` at ~350 s.",
    "After this fix, CI is gated by `quality.yml / lint` at ~999999 s.",
])
def test_headline_followup_verifier_rejects_a_gate_the_findings_do_not_support(tmp_path, bad):
    report = render().replace(
        "After this fix, CI is gated by `quality.yml / lint` at ~350 s.", bad)
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(document()))
    assert not vr.check_headline_followup(report, findings).ok


def test_missing_floor_is_disclosed_without_inventing_measurements(tmp_path):
    doc = document()
    doc["pr_critical_path"]["checks"] = doc["pr_critical_path"]["checks"][:1]
    report = render(doc)
    assert "What gates next:" in report
    assert "next gate is unavailable" in report
    assert "How to check it worked" in report


@pytest.mark.parametrize("with_bound", ["measured", "workflow", False])
def test_cluster_headline_uses_its_stamped_floor_not_a_descending_sibling(with_bound):
    doc = document()
    doc["pr_critical_path"]["checks"].append({
        "name": "helper", "workflow_file": ".github/workflows/test.yml",
        "p50_s": 550, "present_on": 4, "pole_n": 0})
    finding = doc["findings"][0]
    finding.update(pattern="OPT73", cluster_floor_lever=True,
                   affected_jobs=["tests", "helper"],
                   evidence="A shared `Setup` step runs in both legs.")
    if with_bound:
        ctx = WallClockContext(
            workflow=".github/workflows/test.yml", crit={"long_pole_p50": 600},
            own_check_names=frozenset(["tests", "helper"]), cluster_floor_lever=True,
            pr_checks=(("tests", 600), ("helper", 550), ("lint", 350))
            if with_bound == "measured" else (),
            concurrent=((".github/workflows/quality.yml", 350),)
            if with_bound == "workflow" else ())
        result = size_wall_clock(400, ctx)
        finding["wall_clock_derivation"] = [
            {"bound": b, "from_s": a, "to_s": c, "reason": why}
            for b, a, c, why in result.derivation]
        assert result.effective_s == 250
        doc["per_workflow_timing"] = {
            ".github/workflows/quality.yml": {"long_pole_job": "lint"}}

    report = render(doc)
    line = next(line for line in report.splitlines() if "What gates next:" in line)
    if with_bound:
        assert "`quality.yml / lint` at ~350 s." in line
    else:
        assert "next gate is unavailable" in line
    assert "helper" not in line


@pytest.mark.parametrize("saving", [100, None])
def test_next_gate_is_conditional_on_realizing_full_headline_ceiling(saving):
    doc = document()
    if saving is None:
        doc["findings"] = []
    else:
        doc["findings"][0]["wall_clock_p50_s"] = saving
    report = render(doc)
    assert "This projection assumes the full headline saving is realized; a smaller fix may leave the current gate in place." in report

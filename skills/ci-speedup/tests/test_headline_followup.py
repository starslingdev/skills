"""The headline's next gate and measurement instructions survive rendering."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import blocking_path as bp

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
    for fragment in ["What gates next:", "How to check it worked", "**Measure:**",
                     "**Runs:**", "**Don't**", "**Caches:**"]:
        damaged = "\n".join(line for line in report.splitlines() if fragment not in line)
        assert not vr.check_headline_followup(damaged, findings).ok, fragment


def test_missing_floor_is_disclosed_without_inventing_measurements(tmp_path):
    doc = document()
    doc["pr_critical_path"]["checks"] = doc["pr_critical_path"]["checks"][:1]
    report = render(doc)
    assert "What gates next:" in report
    assert "next gate is unavailable" in report
    assert "How to check it worked" in report


@pytest.mark.parametrize("with_bound", [True, False])
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
        finding["wall_clock_derivation"] = [{
            "bound": "bound_measured_critical_path", "from_s": 400, "to_s": 250,
            "reason": "wall-clock capped at the measured critical-path floor — `lint` (350s) is a slower concurrent check on a representative PR"}]
    report = render(doc)
    line = next(line for line in report.splitlines() if "What gates next:" in line)
    if with_bound:
        assert "`quality.yml / lint` at ~350 s." in line
    else:
        assert "next gate is unavailable" in line
    assert "helper" not in line

"""Windows setup guidance annotates existing findings without adding credit."""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import blocking_path as bp
import collect_runs as cr

_spec = importlib.util.spec_from_file_location("windows_guidance_verifier", SKILL / "tests/verify_report.py")
vr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vr
_spec.loader.exec_module(vr)


def finding(step="NuGet restore", labels=None, pattern="OPT72"):
    f = cr._new_structural_finding(
        pattern, ".github/workflows/ci.yml", "build", 1,
        "Measured setup dominates the job.", None, "Existing measured sizing.",
        decomp={"dominant_step": step, "dominant_category": "build",
                "dominant_p50": 240.0, "dominant_share": 0.8,
                "redundant_ratio": 4.0, "job_p50": 300.0})
    f["runner_labels"] = labels if labels is not None else [["windows-latest"]]
    f["wall_clock_p50_s"] = 120.0
    return f


def render(f):
    return "\n".join(bp._structural_block([f], "https://example.invalid/catalog"))


@pytest.mark.parametrize("step,target", [
    ("NuGet restore", "OPT93"), ("dotnet restore", "OPT93"),
    ("Restore NuGet packages", "OPT93"),
    ("dotnet workload install android", "OPT94"),
    ("Install .NET workloads", "OPT94"),
    ("Run actions/setup-dotnet@v4", "OPT94"),
])
def test_windows_pointer_once_without_changing_card_or_sizing(step, target):
    f = finding(step)
    before = copy.deepcopy(f)
    report = render(f)
    lines = [line for line in report.splitlines() if "Windows setup benchmark:" in line]
    assert len(lines) == 1
    assert target in lines[0]
    assert not any(c.isdigit() for c in re.sub(target, "", lines[0], flags=re.I))
    baseline = render(finding(step, labels=[]))
    assert report.replace(lines[0] + "\n", "") == baseline
    assert f == before


@pytest.mark.parametrize("step,labels,pattern", [
    ("Build", [["windows-latest"]], "OPT72"),
    ("NuGet restore", [["ubuntu-latest"]], "OPT72"),
    ("NuGet restore", [["macos-latest"]], "OPT72"),
    ("NuGet restore", [], "OPT72"),
    ("NuGet restore", [["windows-latest"], []], "OPT72"),
    ("NuGet restore", [["windows-latest"], ["ubuntu-latest"]], "OPT72"),
    ("dotnet workload install", [["windows-latest"]], "OPT75"),
    ("Restore npm dependencies", [["windows-latest"]], "OPT72"),
])
def test_pointer_requires_windows_dotnet_opt72(step, labels, pattern):
    assert "Windows setup benchmark:" not in render(finding(step, labels, pattern))


def test_collector_preserves_sampled_runner_labels():
    jobs = [{"name": "build", "labels": ["windows-latest"], "steps": [
        {"name": "Build / NuGet restore", "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:04:00Z"},
        {"name": "Tests", "started_at": "2026-01-01T00:04:00Z", "completed_at": "2026-01-01T00:05:00Z"}],
        "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:05:00Z"}]
    wf = ".github/workflows/ci.yml"
    runs = [copy.deepcopy(jobs) for _ in range(5)]
    args = ((("build", 300.0),), [], {wf: cr._critical_path(runs)}, {wf: runs}, None, {wf: {"pull_request"}}, {}, 0)
    emitted = cr._detect_structural_candidates(*args)
    f = next(f for f in emitted if f["pattern"] == "OPT72")
    assert f["runner_labels"] == [["windows-latest"]] * 5
    assert "OPT93" in render(f)
    for run in runs:
        run[0].pop("labels")
    without_labels = cr._detect_structural_candidates(*args)
    stripped = [{k: v for k, v in row.items() if k != "runner_labels"} for row in emitted]
    assert stripped == [{k: v for k, v in row.items() if k != "runner_labels"} for row in without_labels]


def test_verifier_accepts_valid_pointer_and_rejects_wrong_context(tmp_path):
    f = finding()
    report = render(f)
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": [f]}))
    assert vr.check_windows_setup_guidance(report, path).ok
    for bad in (finding("Build"), finding(labels=[["ubuntu-latest"]]), finding(pattern="OPT75")):
        path.write_text(json.dumps({"findings": [bad]}))
        assert not vr.check_windows_setup_guidance(report, path).ok
    path.write_text(json.dumps({"findings": [f]}))
    assert not vr.check_windows_setup_guidance(report.replace("OPT93", "OPT94"), path).ok
    pointer = next(line for line in report.splitlines() if "Windows setup benchmark:" in line)
    assert not vr.check_windows_setup_guidance(report + "\n" + pointer, path).ok


@pytest.mark.parametrize("step", ["dotnet workload install android", "Setup .NET", "Install .NET SDK"])
def test_full_report_headline_totals_and_sizing_are_byte_identical(tmp_path, step):
    f = finding(step)
    f["title"] += " — `build`"
    doc = {"repo": "sample/project", "scanned_at": "2026-01-01T00:00:00Z",
           "findings": [f],
           "data_sources": {"runs_sampled": 5, "jobs_sampled": 5, "workflows_analyzed": 1},
           "pr_critical_path": {"sampled_pr_count": 5, "sample_complete": True,
               "poles": [{"check": "build", "job": "build", "workflow_file": ".github/workflows/ci.yml",
                          "p50_s": 300.0, "dominant_step": "dotnet workload install android",
                          "dominant_p50_s": 240.0,
                          "steps": [{"step": "dotnet workload install android", "category": "build", "p50_s": 240.0},
                                    {"step": "Tests", "category": "test", "p50_s": 60.0}]}]}}
    baseline = copy.deepcopy(doc)
    baseline["findings"][0].pop("runner_labels")
    report = bp.render(copy.deepcopy(doc))
    original = bp.render(baseline)
    pointers = [line for line in report.splitlines() if "Windows setup benchmark:" in line]
    assert len(pointers) == 1
    assert report.replace(pointers[0] + "\n", "") == original
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(doc))
    assert vr.check_windows_setup_guidance(report, path).ok


@pytest.mark.parametrize("step,target", [
    ("NuGet restore", "OPT93"), ("dotnet workload install android", "OPT94"),
])
def test_pointer_anchor_resolves_to_its_catalog_heading(step, target):
    """The deep link must land on the target's heading (GitHub slug), not the page top."""
    catalog = (SKILL / "references" / "optimization-patterns.md").read_text(encoding="utf-8")
    heading = next(m.group(1) for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", catalog, re.M)
                   if m.group(1).startswith(f"{target} "))
    slug = re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading.strip().lower()))
    line = next(l for l in render(finding(step)).splitlines() if "Windows setup benchmark:" in l)
    assert f"https://example.invalid/catalog#{slug})" in line

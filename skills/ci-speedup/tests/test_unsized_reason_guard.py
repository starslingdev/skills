"""Guard: a real but unsized runner-minute saving never reads "no bill saving".

The rule (documented beside `no_bill_heading_violations` in verify_report.py):
a finding's runner-minute effect is SIZED (`runner_min_saving` is a number),
NO EFFECT (None plus a `runner_min_no_effect_reason` naming why the fix removes
no compute), or UNSIZED (anything else). An UNSIZED finding must carry a
literally true `runner_min_unsized_reason`, and its group heading then reads
`runner-minute saving: unsized (<reason>)`. "no bill saving" is reserved for a
fix that removes no compute.

Three layers:
  1. fixture tests of the pure rule (`runner_min_effect_unsized`,
     `no_bill_heading_violations`), each with a case that must fail;
  2. every catalog pattern in the sizing table, sized through `_size_finding`
     in each data shape that can leave it unsized, then rendered;
  3. the source detectors that size a finding themselves (OPT16 test-source
     build repeat, OPT25 positional splitter) run end to end.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402

_VR_SPEC = importlib.util.spec_from_file_location(
    "unsized_reason_verify_report", HERE / "verify_report.py")
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)


def _heading(pattern: str, metric: str) -> str:
    return (f"<summary><strong>{pattern} - Title</strong> · {metric} · "
            "MEDIUM · 1 across 1 wf</summary>")


# ---- 1. The pure rule --------------------------------------------------------

@pytest.mark.parametrize("finding, unsized", [
    ({"runner_min_saving": 12.5}, False),
    ({"runner_min_saving": 0.0}, False),
    ({"runner_min_saving": 0}, False),
    ({"runner_min_saving": None,
      "runner_min_no_effect_reason": "moves the same work between shards"}, False),
    ({"runner_min_saving": None}, True),
    ({}, True),
    ({"runner_min_saving": None, "runner_min_no_effect_reason": "  "}, True),
    ({"runner_min_saving": True}, True),
    ({"runner_min_saving": None, "runner_min_unsized_reason": "not timed"}, True),
])
def test_runner_min_effect_unsized_classifies_the_three_states(finding, unsized):
    assert vr.runner_min_effect_unsized(finding) is unsized


def test_no_bill_heading_flags_an_unsized_finding():
    findings = [{"id": "f1", "pattern": "OPT16", "runner_min_saving": None}]
    problems = vr.no_bill_heading_violations(
        findings, _heading("OPT16", "no bill saving"))
    assert problems == ["OPT16 group heading reads `no bill saving` but finding f1 is "
                        "unsized and carries no runner_min_unsized_reason"]


def test_no_bill_heading_accepts_a_sized_zero_and_a_no_effect_finding():
    findings = [
        {"id": "f1", "pattern": "OPT12", "runner_min_saving": 0.0},
        {"id": "f2", "pattern": "OPT21", "runner_min_saving": None,
         "runner_min_no_effect_reason": "changes when the job starts"},
    ]
    report = "\n".join([_heading("OPT12", "no bill saving"),
                        _heading("OPT21", "no bill saving")])
    assert vr.no_bill_heading_violations(findings, report) == []


def test_no_bill_heading_accepts_the_unsized_heading_and_ignores_advisories():
    findings = [
        {"id": "f1", "pattern": "OPT16", "runner_min_saving": None,
         "runner_min_unsized_reason": "the build was not timed"},
        {"id": "f2", "pattern": "OPT82", "runner_min_saving": None, "advisory": True},
    ]
    report = "\n".join([
        _heading("OPT16", "runner-minute saving: unsized (the build was not timed)"),
        _heading("OPT82", "no bill saving"),
        "a body line that mentions no bill saving for OPT16",
    ])
    assert vr.no_bill_heading_violations(findings, report) == []


def test_no_bill_heading_flags_a_mixed_group_with_one_unsized_member():
    findings = [
        {"id": "f1", "pattern": "OPT33", "runner_min_saving": 0.0},
        {"id": "f2", "pattern": "OPT33", "runner_min_saving": None,
         "runner_min_unsized_reason": "the job was not timed"},
    ]
    problems = vr.no_bill_heading_violations(
        findings, _heading("OPT33", "no bill saving"))
    assert problems == ["OPT33 group heading reads `no bill saving` but finding f2 is "
                        "unsized (the job was not timed)"]


@pytest.mark.parametrize("line, flagged", [
    ("Saving: runner-minute saving unsized - roughly neutral for passing runs.", True),
    (_heading("OPT25", "runner-minute saving: unsized (effect is neutral)"), True),
    (_heading("OPT16", "runner-minute saving: unsized (no bill saving)"), True),
    ("Saving: runner-minute saving unsized - the build was not timed.", False),
    (_heading("OPT16", "runner-minute saving: unsized (the build was not timed)"), False),
    ("roughly neutral, but this line states no unsized saving", False),
])
def test_an_unsized_statement_never_also_claims_no_effect(line, flagged):
    assert bool(vr.unsized_statement_violations(line)) is flagged


def test_the_check_wraps_the_pure_rule(tmp_path: Path):
    import json
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": [
        {"id": "f1", "pattern": "OPT16", "runner_min_saving": None}]}))
    bad = vr.check_no_bill_heading_for_unsized_saving(
        _heading("OPT16", "no bill saving"), path)
    assert not bad.ok and "OPT16" in bad.detail
    good = vr.check_no_bill_heading_for_unsized_saving(
        _heading("OPT16", "runner-minute saving: unsized (x)"), path)
    assert good.ok
    neutral = vr.check_no_bill_heading_for_unsized_saving(
        _heading("OPT16", "runner-minute saving: unsized (roughly neutral)"), path)
    assert not neutral.ok and "neutral" in neutral.detail
    assert vr.check_no_bill_heading_for_unsized_saving("", tmp_path / "missing").skipped


# ---- 2. Every sizing-table pattern through `_size_finding` ------------------

_POLE = {"long_pole_p50": 600.0, "floor_p50": 120.0,
         "job_p50": {"build": 400.0, "lint": 60.0}}

# Each shape is a data condition under which `_size_finding` can leave the
# runner-minute saving unsized: no sampled runs at all (static-only), runs
# sampled but no monthly volume, the affected job unresolvable, and the
# affected job below the cluster floor with a known volume.
_SHAPES: dict[str, tuple[dict[str, Any], int | None, list[str]]] = {
    "static-only": ({}, None, ["build"]),
    "no-volume": (_POLE, None, ["build"]),
    "unresolved-job": (_POLE, 300, ["unknown-job"]),
    "below-floor": (_POLE, 300, ["lint"]),
}

_FACT_SHAPES: dict[str, dict[str, Any]] = {
    "opt32-deploy-test-paths": {"pattern": "OPT32", "opt32_deploy_test_paths": ["t/"]},
    "oversized-fixture": {"pattern": "OPT113", "oversized_fixture_static": True},
    "static-cache-lint": {"pattern": "OPT4", "static_cache_lint": True},
    "opt16-analysis-repeat": {"pattern": "OPT16",
                              "opt16_analysis_repeat": {"tool": "golangci-lint"}},
    "opt9-build-state": {"pattern": "OPT9", "opt9_tool": "tsc"},
    "no-sizing-model": {"pattern": "OPT999"},
    "opt32-unmeasured": {"pattern": "OPT32", "path_filter_measurement": {
        "status": "unmeasured", "reason": "no PR number on sampled runs"}},
}


def _base(pattern: str, jobs: list[str]) -> dict[str, Any]:
    return {"id": f"{pattern}-f", "pattern": pattern, "title": f"{pattern} title",
            "severity": "MEDIUM", "workflow_file": ".github/workflows/ci.yml",
            "line": 1, "affected_jobs": list(jobs), "evidence": "evidence"}


def _assert_sized_or_reasoned(f: dict[str, Any]) -> None:
    if vr.runner_min_effect_unsized(f):
        reason = str(f.get("runner_min_unsized_reason") or "").strip()
        assert reason, (f"{f['pattern']} is unsized with no reason: "
                        f"size_note={f.get('size_note')!r}")
    lines, _n, _cut = bp._also_noticed_block([f], "http://cat")
    report = "\n".join(lines)
    assert vr.no_bill_heading_violations([f], report) == [], report


_SIZED_PATTERNS = sorted(p for p, cfg in cr._SIZING.items() if cfg["model"] != "measured")


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("pattern", _SIZED_PATTERNS)
def test_every_modeled_pattern_states_why_an_unsized_saving_is_unsized(pattern, shape):
    crit, volume, jobs = _SHAPES[shape]
    f = _base(pattern, jobs)
    cr._size_finding(f, dict(crit), volume)
    _assert_sized_or_reasoned(f)


@pytest.mark.parametrize("shape", sorted(_FACT_SHAPES))
def test_fact_keyed_sizing_branches_state_why_the_saving_is_unsized(shape):
    facts = dict(_FACT_SHAPES[shape])
    f = {**_base(facts["pattern"], ["build"]), **facts}
    cr._size_finding(f, dict(_POLE), 300)
    assert vr.runner_min_effect_unsized(f)
    _assert_sized_or_reasoned(f)


def test_opt32_measured_zero_hit_rate_is_a_no_effect_finding():
    f = {**_base("OPT32", ["build"]), "path_filter_measurement": {
        "status": "measured", "hit_rate": 0.0, "would_skip_prs": 0, "sampled_prs": 30}}
    cr._size_finding(f, dict(_POLE), 300)
    assert not vr.runner_min_effect_unsized(f)
    assert "30 sampled PRs" in f["runner_min_no_effect_reason"]
    _assert_sized_or_reasoned(f)


def test_a_zero_hit_rate_pattern_is_sized_to_zero_without_a_volume():
    f = _base("OPT12", ["build"])
    cr._size_finding(f, {}, None)
    assert f["runner_min_saving"] == 0.0


def test_opt21_dependency_edge_has_no_runner_minute_effect():
    f = _base("OPT21", ["build"])
    cr._size_finding(f, dict(_POLE), 300)
    doc = {"findings": [f], "data_sources": {"tiers_run": ["runs"]}}
    cr._stamp_runner_min_states(doc)
    assert f["runner_min_no_effect_reason"] == cr._OPT21_NO_RUNNER_MIN_EFFECT
    _assert_sized_or_reasoned(f)


@pytest.mark.parametrize("tiers, expected", [
    ([], "no CI runs were sampled, so nothing measured its runner-minutes"),
    (["runs"], "the run collection stopped before this finding was sized"),
])
def test_a_never_sized_finding_states_why(tiers, expected):
    """An early exit returns before the sizing pass; the saving is unsized."""
    unsized = _base("OPT16", ["build"])
    zero_model = _base("OPT57", ["build"])
    doc = {"findings": [unsized, zero_model], "data_sources": {"tiers_run": tiers}}
    cr._stamp_runner_min_states(doc)
    assert unsized["runner_min_unsized_reason"] == expected
    assert zero_model["runner_min_no_effect_reason"]
    for f in (unsized, zero_model):
        _assert_sized_or_reasoned(f)


# ---- 3. Source detectors that size their own finding -------------------------

import scan  # noqa: E402

_CATALOG = HERE.parent / "references" / "patterns"

_GO_BUILD_TESTS = r'''package cli
import ("os/exec"; "path/filepath"; "testing")
func TestConfigContract(t *testing.T) {
    out := filepath.Join(t.TempDir(), "tool")
    _ = exec.Command("go", "build", "-o", out, "./cmd/tool").Run()
}
func TestHelpContract(t *testing.T) {
    out := filepath.Join(t.TempDir(), "tool")
    _ = exec.Command("go", "build", "-o", out, "./cmd/tool").Run()
}
'''

_POSITIONAL_SPLITTER = '''\
def select_tests(listing, shard, count):
    names = sorted(set(listing.splitlines()))
    return names[shard::count]
'''


def _source_repo(tmp_path: Path, kind: str) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: x\non: push\njobs: {}\n", encoding="utf-8")
    if kind == "OPT16":
        source = tmp_path / "internal" / "cli"
        source.mkdir(parents=True)
        (source / "contract_test.go").write_text(_GO_BUILD_TESTS, encoding="utf-8")
    else:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "test-shard.py").write_text(_POSITIONAL_SPLITTER, encoding="utf-8")
    return tmp_path


_SOURCE_DETECTORS = {
    "OPT16": "source_test_build_repeat",
    "OPT25": "positional_splitter_static",
}


@pytest.mark.parametrize("pattern", sorted(_SOURCE_DETECTORS))
def test_source_detector_states_why_its_saving_is_unsized(pattern, tmp_path: Path):
    """OPT16's test-source build repeat (#438) read "no bill saving"; OPT25's
    positional splitter (#440) read "runner-minute saving unsized - roughly
    neutral". Both savings are real and unsized, so each says what is unmeasured."""
    scanned = scan.scan(_source_repo(tmp_path, pattern), _CATALOG)
    fact = _SOURCE_DETECTORS[pattern]
    found = [f for f in scanned["findings"]
             if f.get("pattern") == pattern and f.get(fact)]
    assert found, scanned["findings"]
    for shape, (crit, volume, _jobs) in sorted(_SHAPES.items()):
        f = dict(found[0])
        cr._size_finding(f, dict(crit), volume)
        lines, _n, _cut = bp._also_noticed_block([f], "http://cat")
        report = "\n".join(lines)
        problems = (vr.no_bill_heading_violations([f], report)
                    + vr.unsized_statement_violations(report))
        assert problems == [], (shape, problems)
        assert vr.runner_min_effect_unsized(f), shape
        assert str(f.get("runner_min_unsized_reason") or "").strip(), shape

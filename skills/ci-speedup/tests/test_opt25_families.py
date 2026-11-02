"""Family-scoped OPT25 diagnosis and sequential, floor-capped guidance."""
from datetime import datetime, timedelta, timezone

import pytest

import collect_runs as cr
from wall_clock import WallClockContext


def detect(large, packages=(159, 114, 88, 82, 74, 77, 96, 77), timing_context=True, marker="Shard ", suffix="", offset=0, **kwargs):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    jobs = []
    for family, durations in (("large", large), ("packages", packages)):
        for index, duration in enumerate(durations):
            jobs.append({"name": f"Test ({marker}{family}-{index + offset}{suffix})",
                         "started_at": start.isoformat(),
                         "completed_at": (start + timedelta(seconds=duration)).isoformat(),
                         "conclusion": "success", "html_url": "https://example.com/job"})
    durations = {j["name"]: (datetime.fromisoformat(j["completed_at"]) - start).total_seconds()
                 for j in jobs}
    ranked = sorted(durations, key=durations.get, reverse=True)
    crit = {"long_pole_job": ranked[0], "long_pole_p50": durations[ranked[0]],
            "floor_p50": durations[ranked[1]], "job_p50": durations}
    findings = cr._detect_opt25_shard_imbalance("ci.yml", [jobs, jobs], 0, crit if timing_context else {}, **kwargs)
    assert findings
    return findings[0], crit


@pytest.mark.parametrize("marker,suffix,offset", [("Shard ", "", 0), ("", "", 0), ("Shard ", ", ubuntu22", 0), ("", "", 1), ("", "", 7)])
def test_atomic_family_splits_slow_test_with_realistic_saving(marker, suffix, offset):
    finding, _ = detect([817, 439, 436, 392, 492, 466], timing_context=False, marker=marker, suffix=suffix, offset=offset)
    note = finding["measured_evidence"]["note"]
    assert "split the slow test" in note.lower(), (note, finding["wall_clock_p50_s"])
    assert "rebalance" not in note.lower()
    assert finding["wall_clock_p50_s"] == pytest.approx(325, abs=5)
    assert "large" in finding["measured_evidence"]["summary"]
    assert "atomic-unit" in finding["measured_evidence"]["summary"]


@pytest.mark.parametrize("marker", ["Shard ", ""])
def test_plain_imbalance_uses_only_its_family_mean(marker):
    finding, _ = detect([600, 500, 180], timing_context=False, marker=marker)
    assert "rebalance" in finding["measured_evidence"]["note"].lower()
    # Family mean = 426.67, before the existing next-job floor.
    assert "173" in finding["measured_evidence"]["note"] or "family mean 427" in finding["measured_evidence"]["summary"]
    assert "plain-imbalance" in finding["measured_evidence"]["summary"]
    assert finding["wall_clock_p50_s"] == pytest.approx(173.3)


@pytest.mark.parametrize("floor,combined,additional", [(430, 387, 62), (900, 0, 0)])
def test_plateau_next_step_is_capped_by_existing_cross_workflow_floor(floor, combined, additional):
    finding, crit = detect([817, 450, 440, 460, 492, 466])
    assert "raise this family's shard count" in finding["measured_evidence"]["note"].lower()
    ctx = WallClockContext(workflow="ci.yml", crit=crit,
                           concurrent=(("other.yml", floor),), events=("pull_request",))
    cr._size_opt25_next_step(finding, ctx)
    assert finding["next_step"]["additional_wall_clock_ceiling_s"] == additional
    assert finding["next_step"]["combined_wall_clock_ceiling_s"] == combined
    assert finding["wall_clock_p50_s"] == 325


def test_matrix_producer_command_names_numeric_planner_knob():
    workflow = {"jobs": {
        "plan": {"outputs": {"matrix": "${{ steps.make.outputs.matrix }}"},
                 "steps": [{"id": "unrelated", "run": "python other.py --shards 99"},
                           {"id": "make", "run": "python plan.py --split-shards 6"}]},
        "test": {"name": "Test", "needs": "plan", "strategy": {
            "matrix": "${{ fromJSON(needs.plan.outputs.matrix) }}"}},
    }}
    finding, _ = detect([817, 450, 440, 460, 492, 466], workflow_doc=workflow)
    assert "--split-shards 6" in finding["measured_evidence"]["note"]
    assert "--shards 99" not in finding["measured_evidence"]["note"]


@pytest.mark.parametrize("name", ["Test", "Test (Shard ${{ matrix.shard }})"])
@pytest.mark.parametrize("command", [
    'matrix=$(python plan.py --split-shards 6)\necho "matrix=$matrix" >> "$GITHUB_OUTPUT"',
    'echo "matrix=$(python plan.py --split-shards 6)" >> "$GITHUB_OUTPUT"',
    '# matrix=$(python old.py --shards 99)\npython plan.py --split-shards 6',
])
def test_realistic_planner_output_reaches_wall_clock_prompt(name, command):
    import blocking_path as bp
    workflow = {"jobs": {
        "plan": {"outputs": {"matrix": "${{ steps.make.outputs.matrix }}"},
                 "steps": [{"id": "make", "run": command}]},
        "test": {"name": name, "needs": "plan", "strategy": {
            "matrix": "${{ fromJSON(needs.plan.outputs.matrix) }}"}},
    }}
    finding, _ = detect([817, 450, 440, 460, 492, 466], workflow_doc=workflow)
    assert "--split-shards 6" in finding["measured_evidence"]["note"]
    prompt = "\n".join(bp._hygiene_prompt("OPT25", finding["title"], [finding],
                                       "https://example.com/catalog", saves_wc=True))
    assert "split the slow test" in prompt
    assert "raise this family's shard count" in prompt
    assert "--split-shards 6" in prompt
    assert "--shards 99" not in prompt


def _named_findings(pairs):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    jobs = [{"name": name, "started_at": start.isoformat(),
             "completed_at": (start + timedelta(seconds=seconds)).isoformat(),
             "conclusion": "success"} for name, seconds in pairs]
    return cr._detect_opt25_shard_imbalance("ci.yml", [jobs, jobs], 0, {})


@pytest.mark.parametrize("prefix", ["ubuntu", "ubuntu-", "macos-"])
def test_platform_versions_remain_heterogeneous(prefix):
    findings = _named_findings([(f"Test ({prefix}{version})", duration)
                               for version, duration in [(20, 600), (22, 500), (24, 100)]])
    assert findings
    assert findings[0]["title"] == "Matrix Leg Imbalance"
    assert findings[0]["wall_clock_p50_s"] == 100


def test_singleton_family_never_averages_other_families():
    assert _named_findings([("Test (Shard large-0)", 800),
                           ("Test (Shard packages-0)", 100),
                           ("Test (Shard packages-1)", 90)]) == []

"""Family-scoped OPT25 diagnosis and sequential, floor-capped guidance."""
from datetime import datetime, timedelta, timezone

import pytest

import collect_runs as cr
from wall_clock import WallClockContext


def detect(slow, quick=(150, 120, 95, 85, 80, 75, 100, 90), timing_context=True, marker="Shard ", suffix="", offset=0, **kwargs):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    jobs = []
    for family, durations in (("slow", slow), ("quick", quick)):
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


@pytest.mark.parametrize("marker,suffix,offset", [("Shard ", "", 0), ("Partition ", "", 0), ("Shard ", ", ubuntu22", 0), ("Shard ", "", 1), ("Shard ", "", 7)])
def test_atomic_family_splits_slow_test_with_realistic_saving(marker, suffix, offset):
    finding, _ = detect([800, 470, 455, 410, 480, 445], timing_context=False, marker=marker, suffix=suffix, offset=offset)
    note = finding["measured_evidence"]["note"]
    assert "split the slow test" in note.lower(), (note, finding["wall_clock_p50_s"])
    assert "rebalance" not in note.lower()
    assert finding["wall_clock_p50_s"] == pytest.approx(320, abs=5)
    assert "slow" in finding["measured_evidence"]["summary"]
    assert "atomic-unit" in finding["measured_evidence"]["summary"]


@pytest.mark.parametrize("marker", ["Shard ", "Partition "])
def test_plain_imbalance_uses_only_its_family_mean(marker):
    finding, _ = detect([600, 500, 180], timing_context=False, marker=marker)
    assert "rebalance" in finding["measured_evidence"]["note"].lower()
    # Family mean = 426.67, before the existing next-job floor.
    assert "173" in finding["measured_evidence"]["note"] or "family mean 427" in finding["measured_evidence"]["summary"]
    assert "plain-imbalance" in finding["measured_evidence"]["summary"]
    assert finding["wall_clock_p50_s"] == pytest.approx(173.3)


@pytest.mark.parametrize("floor,combined,additional", [(430, 370, 50), (900, 0, 0)])
def test_plateau_next_step_is_capped_by_existing_cross_workflow_floor(floor, combined, additional):
    finding, crit = detect([800, 470, 455, 410, 480, 445])
    assert "raise this family's shard count" in finding["measured_evidence"]["note"].lower()
    ctx = WallClockContext(workflow="ci.yml", crit=crit,
                           concurrent=(("other.yml", floor),), events=("pull_request",))
    cr._size_opt25_next_step(finding, ctx)
    assert finding["next_step"]["additional_wall_clock_ceiling_s"] == additional
    assert finding["next_step"]["combined_wall_clock_ceiling_s"] == combined
    assert finding["wall_clock_p50_s"] == 320


def test_matrix_producer_command_names_numeric_planner_knob():
    workflow = {"jobs": {
        "plan": {"outputs": {"matrix": "${{ steps.make.outputs.matrix }}"},
                 "steps": [{"id": "unrelated", "run": "python other.py --shards 10"},
                           {"id": "make", "run": "python plan.py --split-shards 4"}]},
        "test": {"name": "Test", "needs": "plan", "strategy": {
            "matrix": "${{ fromJSON(needs.plan.outputs.matrix) }}"}},
    }}
    finding, _ = detect([800, 470, 455, 410, 480, 445], workflow_doc=workflow)
    assert "--split-shards 4" in finding["measured_evidence"]["note"]
    assert "--shards 10" not in finding["measured_evidence"]["note"]


@pytest.mark.parametrize("name", ["Test", "Test (Shard ${{ matrix.shard }})"])
@pytest.mark.parametrize("command", [
    'matrix=$(python plan.py --split-shards 4)\necho "matrix=$matrix" >> "$GITHUB_OUTPUT"',
    'echo "matrix=$(python plan.py --split-shards 4)" >> "$GITHUB_OUTPUT"',
    '# matrix=$(python old.py --shards 10)\npython plan.py --split-shards 4',
])
def test_realistic_planner_output_reaches_wall_clock_prompt(name, command):
    import blocking_path as bp
    workflow = {"jobs": {
        "plan": {"outputs": {"matrix": "${{ steps.make.outputs.matrix }}"},
                 "steps": [{"id": "make", "run": command}]},
        "test": {"name": name, "needs": "plan", "strategy": {
            "matrix": "${{ fromJSON(needs.plan.outputs.matrix) }}"}},
    }}
    finding, _ = detect([800, 470, 455, 410, 480, 445], workflow_doc=workflow)
    assert "--split-shards 4" in finding["measured_evidence"]["note"]
    prompt = "\n".join(bp._hygiene_prompt("OPT25", finding["title"], [finding],
                                       "https://example.com/catalog", saves_wc=True))
    assert "split the slow test" in prompt
    assert "raise this family's shard count" in prompt
    assert "--split-shards 4" in prompt
    assert "--shards 10" not in prompt


def _named_findings(pairs):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    jobs = [{"name": name, "started_at": start.isoformat(),
             "completed_at": (start + timedelta(seconds=seconds)).isoformat(),
             "conclusion": "success"} for name, seconds in pairs]
    return cr._detect_opt25_shard_imbalance("ci.yml", [jobs, jobs], 0, {})


def test_skipped_matrix_legs_do_not_create_an_imbalance():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    jobs = []
    for index in range(10):
        ran = index < 5
        duration = 600 if ran else 12
        jobs.append({
            "name": f"Test (Shard {index + 1})",
            "started_at": start.isoformat(),
            "completed_at": (start + timedelta(seconds=duration)).isoformat(),
            "conclusion": "success",
            "steps": [
                {"name": "Set up job", "conclusion": "success"},
                {"name": "Run divided tests",
                 "conclusion": "success" if ran else "skipped"},
            ],
        })

    assert cr._detect_opt25_shard_imbalance(
        "ci.yml", [jobs, jobs], 0, {"long_pole_p50": 600}) == []


def test_real_imbalance_stamps_excluded_legs_without_work():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    jobs = []
    for index, duration in enumerate((600, 400, 100, 12)):
        ran = index < 3
        jobs.append({
            "name": f"Test (Shard {index + 1})",
            "started_at": start.isoformat(),
            "completed_at": (start + timedelta(seconds=duration)).isoformat(),
            "conclusion": "success",
            "steps": [
                {"name": "Set up job", "conclusion": "success"},
                {"name": "Run divided tests",
                 "conclusion": "success" if ran else "skipped"},
            ],
        })

    findings = cr._detect_opt25_shard_imbalance(
        "ci.yml", [jobs, jobs], 0, {"long_pole_p50": 600})
    assert findings[0]["legs_excluded_no_work"] == 2


@pytest.mark.parametrize("prefix", ["ubuntu", "ubuntu-", "macos-"])
def test_platform_versions_remain_heterogeneous(prefix):
    findings = _named_findings([(f"Test ({prefix}{version})", duration)
                               for version, duration in [(20, 600), (22, 500), (24, 100)]])
    assert findings
    assert findings[0]["title"] == "Matrix Leg Imbalance"
    assert findings[0]["wall_clock_p50_s"] == 100


def test_singleton_family_never_averages_other_families():
    assert _named_findings([("Test (Shard slow-0)", 800),
                           ("Test (Shard quick-0)", 100),
                           ("Test (Shard quick-1)", 90)]) == []


@pytest.mark.parametrize("legs", [
    # Version axes without a shard/partition marker are distinct configurations,
    # not interchangeable shards: no rebalance text, no oversized-test diagnosis.
    [("postgres-14", 900), ("postgres-15", 800), ("postgres-16", 250)],
    [("Node.js 18", 600), ("Node.js 20", 500), ("Node.js 22", 100)],
    [("py3.11", 900), ("py3.12", 800), ("py3.13", 250)],
])
def test_unmarked_numbered_variants_keep_the_legacy_heterogeneous_diagnosis(legs):
    findings = _named_findings([(f"Test ({variant})", seconds) for variant, seconds in legs])
    assert len(findings) == 1
    assert findings[0]["title"] == "Matrix Leg Imbalance"
    assert "rebalance the test distribution" not in findings[0]["measured_evidence"]["note"]
    assert "shard_family" not in findings[0]


def test_unmarked_version_axis_below_the_legacy_threshold_is_not_flagged():
    # 500/300 = 1.67x clears the atomic-unit cutoff but not the legacy 3x/2x
    # imbalance threshold; without a shard marker it must stay unflagged.
    assert _named_findings([("Test (postgres-14)", 500), ("Test (postgres-15)", 300),
                            ("Test (postgres-16)", 290)]) == []


def test_split_floor_includes_a_slower_leg_from_another_family():
    findings = _named_findings([("Test (Shard slow-0)", 880), ("Test (Shard slow-1)", 200),
                                ("Test (Shard quick-0)", 830), ("Test (Shard quick-1)", 100)])
    assert len(findings) == 1
    # Splitting slow-0 cannot pull the matrix below quick-0 (830s).
    assert findings[0]["wall_clock_p50_s"] == 50
    assert "Test (Shard quick-0)" in findings[0]["measured_evidence"]["note"]


def test_wrapped_planner_flag_is_found():
    workflow = {"jobs": {
        "plan": {"outputs": {"matrix": "${{ steps.make.outputs.matrix }}"},
                 "steps": [{"id": "make", "run": "python plan.py \\\n  --split-shards \\\n  4"}]},
        "test": {"name": "Test", "needs": "plan", "strategy": {
            "matrix": "${{ fromJSON(needs.plan.outputs.matrix) }}"}},
    }}
    finding, _ = detect([800, 470, 455, 410, 480, 445], workflow_doc=workflow)
    assert "--split-shards 4" in finding["measured_evidence"]["note"]

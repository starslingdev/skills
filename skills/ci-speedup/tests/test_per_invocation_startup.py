"""OPT85: repeated process startup is measured from declared tool boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import blocking_path as bp


def _stamp(at: datetime, message: str) -> str:
    return f"{at.isoformat().replace('+00:00', 'Z')} {message}"


def _startup_log(
    shape: str,
    *,
    parallel: bool = False,
    timestamps: bool = True,
    pytest_option_first: bool = False,
    pytest_output: str = "synthetic",
    cargo_integration: bool = False,
) -> str:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[tuple[datetime, str]] = []
    for i in range(10):
        start = base + timedelta(seconds=i * 5)
        if shape == "pytest":
            work = ("collecting ... collected 1 item"
                    if pytest_output == "normal" else
                    "collected 1 item"
                    if pytest_output == "piped" else
                    ". [100%]" if pytest_output == "quiet" else
                    "collecting ...")
            done = ("======= 1 passed, 1 warning in 1.00s ======="
                    if pytest_output == "normal" else "1 passed in 1.00s")
            command = (
                f"+ python -m pytest tests/case_{i}.py"
                if pytest_output == "piped" else
                f"+ python3 -m pytest -q tests/case_{i}.py"
                if pytest_option_first else
                f"+ python -m pytest tests/case_{i}.py -q")
            messages = (
                command,
                work,
                done,
            )
        elif shape == "cargo":
            messages = (
                (f"Running tests/case_{i}.rs (target/debug/deps/case_{i})"
                 if cargo_integration else
                 f"Running unittests src/bin/case_{i}.rs "
                 f"(target/debug/deps/case_{i})"),
                "running 1 test",
                "test result: ok. 1 passed; 0 failed",
            )
        elif shape == "tool-loop":
            messages = (
                f"Tool process started: plugin-builder category=build item=plugin-{i}",
                f"Tool ready: plugin-builder category=build item=plugin-{i}",
                f"Tool process finished: plugin-builder category=build item=plugin-{i}",
            )
        elif shape == "worker-pool":
            messages = (
                f"Worker pool started: integration-workers case=case-{i}",
                f"Worker pool ready: integration-workers case=case-{i}",
                f"Worker pool stopped: integration-workers case=case-{i}",
            )
        else:
            messages = (
                f"Process started: verification case-{i}",
                f"Checking verification: case-{i}",
                f"Verification passed: case-{i}",
            )
        rows.extend(((start, messages[0]),
                     (start + timedelta(seconds=3), messages[1]),
                     (start + timedelta(seconds=5), messages[2])))
    if parallel:
        rows[3] = (base + timedelta(seconds=1), rows[3][1])
    rows.sort(key=lambda row: row[0])
    if timestamps:
        return "\n".join(_stamp(at, message) for at, message in rows)
    return "\n".join(message for _at, message in rows)


def _repeated_command_log(command: str = "node scripts/build-plugin.mjs",
                          *, first_output_s: float | None = 3.5) -> str:
    """An ordinary `set -x` build loop: each invocation's own output follows its
    trace line. `first_output_s` is how long the fresh process takes to print its
    first line (None: the tool prints nothing)."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(10):
        start = base + timedelta(seconds=i * 5)
        rows.append(_stamp(start, f"+ {command} plugin-{i}"))
        if first_output_s is not None:
            rows.append(_stamp(start + timedelta(seconds=first_output_s),
                               f"[build] plugin-{i}: bundling 42 modules"))
            rows.append(_stamp(start + timedelta(seconds=4.8),
                               f"[build] plugin-{i}: wrote dist/plugin-{i}/index.js"))
    return "\n".join(rows)


def _plain_pool_log(*, isolation: str | None = None) -> str:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [f"Pool configuration: {isolation}"] if isolation else []
    for i in range(10):
        start = base + timedelta(seconds=i * 5)
        rows.extend((
            _stamp(start, f"Starting worker pool vitest for case case-{i}"),
            _stamp(start + timedelta(seconds=3),
                   f"Worker pool ready vitest for case case-{i}"),
            _stamp(start + timedelta(seconds=5),
                   f"Closing worker pool vitest for case case-{i}"),
        ))
    return "\n".join(rows)


@pytest.mark.parametrize("shape", ["pytest", "cargo", "verification"])
def test_opt85_measures_repeated_process_startup_for_declared_shapes(shape):
    leaf = bp._parse_log(_startup_log(shape))

    assert leaf is not None
    assert leaf["fix_key"] == "per-invocation-startup"
    assert leaf["iteration_count"] == 10
    assert leaf["startup_median_s"] == pytest.approx(3.0)
    assert leaf["iteration_median_s"] == pytest.approx(5.0)
    assert leaf["modeled_saving_s"] == pytest.approx(27.0)
    assert leaf["magnitude"] == {
        "label": "modeled repeated process startup", "value": 27.0, "unit": "s"}
    assert any("collecting" in line or "running 1 test" in line
               or "Checking verification" in line for line in leaf["evidence"])
    assert any("passed" in line or "test result" in line
               for line in leaf["evidence"])


def test_opt85_accepts_pytest_options_before_the_unique_target():
    leaf = bp._parse_log(_startup_log("pytest", pytest_option_first=True))

    assert leaf is not None
    assert leaf["fix_key"] == "per-invocation-startup"


def test_opt85_accepts_decorated_normal_pytest_output():
    leaf = bp._parse_log(_startup_log("pytest", pytest_output="normal"))

    assert leaf is not None
    assert leaf["fix_key"] == "per-invocation-startup"


def test_opt85_accepts_collected_line_from_real_piped_pytest_output():
    # Captured from ``python3 -m pytest test_real.py | cat`` on 2026-11-09:
    # when stdout is not a terminal pytest prints this line without the
    # transient ``collecting ...`` prefix.
    leaf = bp._parse_log(_startup_log("pytest", pytest_output="piped"))

    assert leaf is not None
    assert leaf["fix_key"] == "per-invocation-startup"
    assert leaf["magnitude"] is None
    assert leaf["modeled_saving_s"] is None
    assert "not separately measurable" in leaf["unit_label"]


def test_opt85_rejects_quiet_pytest_output_without_a_pre_work_marker():
    leaf = bp._parse_log(_startup_log("pytest", pytest_output="quiet"))

    assert leaf is None or leaf["fix_key"] != "per-invocation-startup"


def test_opt85_accepts_cargo_integration_test_binaries():
    leaf = bp._parse_log(_startup_log("cargo", cargo_integration=True))

    assert leaf is not None
    assert leaf["fix_key"] == "per-invocation-startup"


def test_opt85_rejects_textually_grouped_but_timestamp_overlapping_iterations():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(10):
        start = base + timedelta(seconds=i * 5)
        rows.extend((
            _stamp(start, f"+ pytest tests/case_{i}.py"),
            _stamp(start + timedelta(seconds=6), "collecting ..."),
            _stamp(start + timedelta(seconds=10), "1 passed in 1.00s"),
        ))

    leaf = bp._parse_log("\n".join(rows))

    assert leaf is None or leaf["fix_key"] != "per-invocation-startup"


def test_opt85_requires_verification_markers_for_the_same_process():
    text = _startup_log("verification").replace(
        "Checking verification: case-", "Checking verification: unrelated-")

    leaf = bp._parse_log(text)

    assert leaf is None or leaf["fix_key"] != "per-invocation-startup"


@pytest.mark.parametrize(
    ("shape", "kind", "fix_key"),
    [
        ("tool-loop", "tool process", "per-item-build-startup"),
        ("worker-pool", "worker pool", "per-case-worker-pool-startup"),
    ],
)
def test_opt85_measures_non_test_process_and_pool_startup(shape, kind, fix_key):
    leaf = bp._parse_log(_startup_log(shape))

    assert leaf is not None
    assert leaf["fix_key"] == fix_key
    assert leaf["process_kind"] == kind
    assert leaf["iteration_count"] == 10
    assert leaf["modeled_saving_s"] == pytest.approx(27.0)


def test_opt85_detects_repeated_real_build_commands_without_instrumentation():
    leaf = bp._parse_log(_repeated_command_log())

    assert leaf is not None
    assert leaf["fix_key"] == "per-item-build-startup"
    assert leaf["iteration_count"] == 10
    # Only the measured trace-to-first-output startup of the nine later processes
    # is removable; their per-item work (3.5s -> 5s) remains and is never credited.
    assert leaf["startup_median_s"] == pytest.approx(3.5)
    assert leaf["modeled_saving_s"] == pytest.approx(31.5)
    assert leaf["magnitude"]["value"] == pytest.approx(31.5)


def test_opt85_repeated_commands_stay_quiet_when_per_item_work_dominates():
    # Each fresh process prints at once and then spends its time on real work:
    # batching would remove almost nothing, so the whole interval is not startup.
    assert bp._parse_log(_repeated_command_log(first_output_s=0.2)) is None


def test_opt85_repeated_commands_stay_quiet_when_startup_is_unmeasurable():
    # Trace lines alone show when each process began, not how much of the
    # interval was startup; crediting the whole interval would count work.
    assert bp._parse_log(_repeated_command_log(first_output_s=None)) is None


@pytest.mark.parametrize("marker", [
    "isolate: true",
    "+ npx vitest run --pool=forks --isolate",
    "+ python -m pytest --forked tests",
])
def test_opt85_withholds_pool_reuse_for_plain_isolation_flags(marker):
    assert bp._parse_log(_plain_pool_log(isolation=marker)) is None


def test_opt85_detects_plain_runner_pool_lifecycle_output():
    leaf = bp._parse_log(_plain_pool_log())

    assert leaf is not None
    assert leaf["fix_key"] == "per-case-worker-pool-startup"
    assert leaf["iteration_count"] == 10


def test_opt85_withholds_pool_reuse_for_crash_isolation():
    assert bp._parse_log(_plain_pool_log(isolation="crash-isolation enabled")) is None


def test_opt85_withholds_per_image_scanner_processes():
    text = _repeated_command_log("trivy image")

    assert bp._parse_log(text) is None


def test_opt85_non_test_loop_requires_one_repeated_tool():
    text = _startup_log("tool-loop").replace(
        "plugin-builder category=build item=plugin-9",
        "other-builder category=build item=plugin-9",
    )

    leaf = bp._parse_log(text)

    assert leaf is None or leaf["fix_key"] != "per-item-build-startup"


def test_opt85_stays_silent_when_one_process_batches_the_items():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [_stamp(base, "Tool process started: plugin-builder batch=all")]
    rows.extend(
        _stamp(base + timedelta(seconds=i + 1), f"Built item plugin-{i}")
        for i in range(10)
    )
    rows.append(_stamp(
        base + timedelta(seconds=12),
        "Tool process finished: plugin-builder batch=all",
    ))

    leaf = bp._parse_log("\n".join(rows))

    assert leaf is None


def test_opt85_stays_silent_when_one_traced_process_builds_every_item():
    # The ordinary-output form of the batched case: one `set -x` trace line
    # hands every item to one process, so there is no repeated startup.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = " ".join(f"plugin-{i}" for i in range(10))
    rows = [_stamp(base, f"+ node scripts/build-plugin.mjs {items}")]
    rows.extend(
        _stamp(base + timedelta(seconds=3 + i), f"[build] plugin-{i}: wrote dist")
        for i in range(10)
    )

    assert bp._parse_log("\n".join(rows), dominant_category="build") is None


def test_opt85_prompt_sizes_a_monthly_ceiling_from_workflow_run_count():
    leaf = {
        "fix_key": "per-item-build-startup",
        "modeled_saving_s": 45.0,
        "unit_label": "10 repeated tool starts",
        "deeper": [],
    }
    pole = {
        "workflow_file": ".github/workflows/ci.yml",
        "check": "build plugins",
        "p50_s": 60.0,
        "workflow_runs_30d": 120,
    }

    prompt = bp._build_agent_prompt(
        leaf, pole, [], None, "example/repo", "abc123", 10, 10,
    )

    assert "runner-minute saving: at most 90.0/month" in prompt
    assert "monthly execution count" not in prompt


@pytest.mark.parametrize("modeled, runs, reason", [
    (45.0, None, "the workflow's 30-day run count is unavailable"),
    (None, 120, "the repeated-startup interval is not separately measurable"),
])
def test_opt85_prompt_unsized_reason_matches_its_branch(modeled, runs, reason):
    leaf = {"fix_key": "per-item-build-startup", "modeled_saving_s": modeled,
            "unit_label": "10 repeated tool starts", "deeper": []}
    pole = {"workflow_file": ".github/workflows/ci.yml", "check": "build plugins",
            "p50_s": 60.0}
    if runs is not None:
        pole["workflow_runs_30d"] = runs

    prompt = bp._build_agent_prompt(
        leaf, pole, [], None, "example/repo", "abc123", 10, 10,
    )

    assert f"runner-minute saving: unsized ({reason})." in prompt
    assert "at most" not in prompt.split("runner-minute saving:")[1].splitlines()[0]
    assert leaf["runner_min_unsized_reason"] == reason


def test_opt85_process_batching_recipe_is_bounded_and_states_cost_direction():
    meta = bp._FIX_META["per-invocation-startup"]

    assert "bounded worker pool" in meta["deliver"]
    assert "measured memory cap" in meta["deliver"]
    assert "Runner-minute effect: saving" in meta["constraints"]


def test_opt85_test_profile_cannot_crown_a_build_dominant_pole():
    leaf = bp._parse_log(_startup_log("pytest"))

    assert leaf is not None
    assert bp._offcategory_leaf(leaf, {"dominant_category": "build"}) is True


def test_opt85_tool_profile_cannot_crown_another_category():
    leaf = bp._parse_log(_startup_log("tool-loop"))

    assert leaf is not None
    assert bp._offcategory_leaf(leaf, {"dominant_category": "build"}) is False
    assert bp._offcategory_leaf(leaf, {"dominant_category": "scan"}) is True


@pytest.mark.parametrize("text", [
    "\n".join(f"2026-01-01T00:00:{i:02d}Z progress {i * 10}%" for i in range(10)),
    _startup_log("pytest", parallel=True),
    _startup_log("pytest").replace("case_9.py", "case_8.py"),
    _startup_log("pytest", timestamps=False),
])
def test_opt85_stays_silent_without_serial_timestamped_process_boundaries(text):
    leaf = bp._parse_log(text)
    assert leaf is None or leaf["fix_key"] != "per-invocation-startup"


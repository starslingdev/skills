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
            messages = (
                (f"+ python3 -m pytest -q tests/case_{i}.py"
                 if pytest_option_first else
                 f"+ python -m pytest tests/case_{i}.py -q"),
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


@pytest.mark.parametrize("text", [
    "\n".join(f"2026-01-01T00:00:{i:02d}Z progress {i * 10}%" for i in range(10)),
    _startup_log("pytest", parallel=True),
    _startup_log("pytest").replace("case_9.py", "case_8.py"),
    _startup_log("pytest", timestamps=False),
])
def test_opt85_stays_silent_without_serial_timestamped_process_boundaries(text):
    leaf = bp._parse_log(text)
    assert leaf is None or leaf["fix_key"] != "per-invocation-startup"


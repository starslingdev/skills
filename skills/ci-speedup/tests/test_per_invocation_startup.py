"""OPT85: repeated process startup is measured from declared tool boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import blocking_path as bp


def _stamp(at: datetime, message: str) -> str:
    return f"{at.isoformat().replace('+00:00', 'Z')} {message}"


def _startup_log(shape: str, *, parallel: bool = False, timestamps: bool = True) -> str:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[tuple[datetime, str]] = []
    for i in range(10):
        start = base + timedelta(seconds=i * 5)
        if shape == "pytest":
            messages = (
                f"+ python -m pytest tests/case_{i}.py -q",
                "collecting ...",
                "1 passed in 1.00s",
            )
        elif shape == "cargo":
            messages = (
                f"Running unittests src/bin/case_{i}.rs (target/debug/deps/case_{i})",
                "running 1 test",
                "test result: ok. 1 passed; 0 failed",
            )
        else:
            messages = (
                f"Starting verification: case-{i}",
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


@pytest.mark.parametrize("text", [
    "\n".join(f"2026-01-01T00:00:{i:02d}Z progress {i * 10}%" for i in range(10)),
    _startup_log("pytest", parallel=True),
    _startup_log("pytest").replace("case_9.py", "case_8.py"),
    _startup_log("pytest", timestamps=False),
])
def test_opt85_stays_silent_without_serial_timestamped_process_boundaries(text):
    leaf = bp._parse_log(text)
    assert leaf is None or leaf["fix_key"] != "per-invocation-startup"


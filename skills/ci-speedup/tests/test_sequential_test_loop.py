"""Detection and runner-advisory handling for sequential shell test loops."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
import blocking_path as bp  # noqa: E402


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _loop_log(count: int = 12, *, spacing_s: int = 70) -> str:
    start = datetime(2026, 11, 5, tzinfo=timezone.utc)
    lines = []
    for i in range(count):
        at = start + timedelta(seconds=i * spacing_s)
        path = f"tests/unit_{i}.sh"
        lines.append(f"{_stamp(at)} + ./run-test.sh {path}")
        lines.append(f"{_stamp(at + timedelta(seconds=70))} done {path}")
    return "\n".join(lines)


def test_serial_test_loop_leaf_requires_ten_non_overlapping_file_invocations():
    leaf = bp._parse_log(_loop_log())
    assert leaf is not None and leaf["fix_key"] == "test-loop-serial"


def test_serial_test_loop_leaf_rejects_overlapping_invocations():
    assert bp._parse_log(_loop_log(spacing_s=35)) is None


def test_serial_test_loop_leaf_rejects_only_eight_invocations():
    assert bp._parse_log(_loop_log(count=8)) is None


def test_runner_advisory_explains_why_a_serial_loop_holds_it_back():
    finding = {
        "id": "f9",
        "pattern": "OPT81",
        "workflow_file": ".github/workflows/ci.yml",
        "faster_runner": {"half": "A2", "job": "test"},
    }
    card = "\n".join(bp._opt81_card(
        finding, "https://example.invalid/catalog", held_back_by="test-loop-serial"))
    assert ("a bigger runner will not help a sequential step: it runs on one core; "
            "run the work in parallel first") in card.lower()
    assert "OPT88" in card

"""Tests for record_timing.py — the orchestrator's phase-timing recorder.

The point of the script is that the durable findings JSON ``timings`` block
ends up with the orchestrator-driven phases (Phase 2.5, Phase 5) and the
end-to-end ``total_run_s``, not just the self-timing scripts. So the tests
assert it MERGES (never clobbers the scanner's keys) and is idempotent across
the several phase calls a run makes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import record_timing  # noqa: E402


def _write(p: Path, obj: dict) -> None:
    p.write_text(json.dumps(obj), encoding="utf-8")


def test_records_phase_into_empty_timings(tmp_path: Path) -> None:
    f = tmp_path / "findings.json"
    _write(f, {"findings": []})
    rc = record_timing.main(["--findings", str(f), "--phase", "total_run_s", "--seconds", "362.4"])
    assert rc == 0
    assert json.loads(f.read_text())["timings"] == {"total_run_s": 362.4}


def test_merges_without_clobbering_scanner_keys(tmp_path: Path) -> None:
    """The scanner already wrote scan_total_s / activity_enrich_s; recording a
    later phase must add to them, not replace the block."""
    f = tmp_path / "findings.json"
    _write(f, {"findings": [], "timings": {"scan_total_s": 8.45, "activity_enrich_s": 5.96}})
    record_timing.main(["--findings", str(f), "--phase", "risk_scenario_s", "--seconds", "330"])
    record_timing.main(["--findings", str(f), "--phase", "total_run_s", "--seconds", "345.1"])
    timings = json.loads(f.read_text())["timings"]
    assert timings == {
        "scan_total_s": 8.45,
        "activity_enrich_s": 5.96,
        "risk_scenario_s": 330.0,
        "total_run_s": 345.1,
    }


def test_rounds_to_two_places_and_overwrites_same_phase(tmp_path: Path) -> None:
    f = tmp_path / "findings.json"
    _write(f, {"findings": [], "timings": {"fixes_s": 1.0}})
    record_timing.main(["--findings", str(f), "--phase", "fixes_s", "--seconds", "90.126"])
    assert json.loads(f.read_text())["timings"]["fixes_s"] == 90.13


def test_bad_findings_json_errors_nonzero(tmp_path: Path) -> None:
    f = tmp_path / "broken.json"
    f.write_text("not json", encoding="utf-8")
    assert record_timing.main(["--findings", str(f), "--phase", "total_run_s", "--seconds", "1"]) == 1

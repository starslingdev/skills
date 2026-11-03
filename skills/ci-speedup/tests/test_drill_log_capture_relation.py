"""The Data sources line compares the drill logs' fetch time with the audit's
scan time instead of always calling the logs "newer" (2026-11-03 dogfood: both
carried the same timestamp, which means the same capture)."""
from __future__ import annotations

import json
from pathlib import Path

import blocking_path as bp  # noqa: E402
from test_blocking_path import _doc_one_pole, _load_verify_report  # noqa: E402


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _logs_line(report: str) -> str:
    return next(line for line in report.splitlines()
                if "Step internals + cross-run checks" in line)


def test_same_timestamp_logs_are_the_same_capture_not_newer(tmp_path):
    doc = _doc_one_pole()
    doc["scanned_at"] = "2026-11-03T22:58:08.682115+00:00"
    report = bp.render(doc, {}, {}, {}, "2026-11-03T22:58:08.682115+00:00")
    line = _logs_line(report)
    assert "newer than" not in line, line
    assert "(the same capture as the critical-path audit above)" in line, line

    vr = _load_verify_report()
    chk = vr.check_drill_log_capture_relation(report, _write(tmp_path, doc))
    assert chk.ok and not chk.skipped, chk
    lying = report.replace("(the same capture as", "(newer than", 1)
    assert not vr.check_drill_log_capture_relation(lying, _write(tmp_path, doc)).ok


def test_later_and_earlier_logs_are_named_as_such(tmp_path):
    doc = _doc_one_pole()
    doc["scanned_at"] = "2026-11-03T10:00:00+00:00"
    later = _logs_line(bp.render(doc, {}, {}, {}, "2026-11-04T09:00:00+00:00"))
    assert "(newer than the critical-path audit above)" in later, later
    earlier = _logs_line(bp.render(doc, {}, {}, {}, "2026-11-02T09:00:00+00:00"))
    assert "(older than the critical-path audit above)" in earlier, earlier
    # A date-only capture on the audit's own day is the same day, never "newer".
    same_day = _logs_line(bp.render(doc, {}, {}, {}, "2026-11-03"))
    assert "(the same day as the critical-path audit above)" in same_day, same_day

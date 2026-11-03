"""The cost spine's rounded-up minutes are not called GitHub-billable: minutes on
runners GitHub does not host (self-hosted or third-party) are never on the GitHub bill
(2026-11-03 dogfood: "Billable min/mo" and "Share" included `starsling-ubuntu-24.04`
minutes). A runner's host is never a risk signal; the note only says who bills."""
from __future__ import annotations

import json
import re
from pathlib import Path

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
from test_blocking_path import (  # noqa: E402  (helpers only, read-only)
    _load_verify_report, _render_ready_spine_doc, _spine_job)


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _mixed_runner_spine_doc() -> dict:
    doc = _render_ready_spine_doc(row_count=1)
    wf = ".github/workflows/pipeline.yml"
    hosted = _spine_job("hosted", 61, "2026-06-01T00:00:00Z")
    vendor = dict(_spine_job("vendor", 300, "2026-06-01T00:00:00Z"),
                  labels=["starsling-ubuntu-24.04"])
    own = dict(_spine_job("own", 125, "2026-06-01T00:00:00Z"),
               labels=["self-hosted", "linux"])
    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": [[hosted, vendor, own]]}}, {}, {wf: 10}, "private",
        workflows_in_play={wf})
    assert spine is not None and spine["render_ready"] is True
    doc["runner_minute_spine"] = spine
    return doc


def test_minutes_column_is_not_headed_billable_and_names_the_non_github_runners(tmp_path):
    doc = _mixed_runner_spine_doc()
    report = bp.render(doc)

    assert "Billable min/mo" not in report
    assert ("| Workflow | Job | Runner | Event | Status | Attempt | Volume "
            "| Raw min/mo | Rounded-up min/mo | Share |") in report
    # 2 min (hosted, 61s) + 5 min (vendor) + 3 min (own), each x 10 runs a month.
    assert ("20.000 of the 100.000 min/mo ran on GitHub-hosted runner labels and 80.000 on "
            "labels not recognised as GitHub-hosted (`linux self-hosted`, "
            "`starsling-ubuntu-24.04`).") in report
    assert "A GitHub larger runner with a custom name is not recognised" in report
    assert "GitHub bills only the runners it hosts" in report

    vr = _load_verify_report()
    findings_path = _write(tmp_path, doc)
    assert vr.check_runner_minute_spine_contract(report, findings_path).ok
    chk = vr.check_cost_spine_runner_host_split(report, findings_path)
    assert chk.ok and not chk.skipped, chk
    tampered = report.replace("20.000 of the 100.000", "100.000 of the 100.000", 1)
    assert not vr.check_cost_spine_runner_host_split(tampered, findings_path).ok


def test_all_github_hosted_spine_says_so(tmp_path):
    doc = _render_ready_spine_doc(row_count=2)
    report = bp.render(doc)
    assert re.search(r"All [\d.]+ min/mo ran on GitHub-hosted runner labels\.", report)
    assert "GitHub bills only the runners it hosts" not in report
    vr = _load_verify_report()
    chk = vr.check_cost_spine_runner_host_split(report, _write(tmp_path, doc))
    assert chk.ok and not chk.skipped, chk


def test_github_hosted_label_rule():
    assert bp._is_github_hosted_label("ubuntu-latest")
    assert bp._is_github_hosted_label("macos-14")
    assert bp._is_github_hosted_label("windows-2022")
    assert not bp._is_github_hosted_label("starsling-ubuntu-24.04")
    assert not bp._is_github_hosted_label("linux self-hosted")
    assert not bp._is_github_hosted_label("self-hosted ubuntu-22.04")
    assert not bp._is_github_hosted_label("")

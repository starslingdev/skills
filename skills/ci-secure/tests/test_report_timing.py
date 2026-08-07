"""report.py closes the end-to-end timing loop.

report.py is always the last scripted step. scan.py stamps `run_start_epoch`
(and run.py adds `scripted_end_epoch`) into the findings `timings`; report.py reads them
back on render and writes `total_run_s` (the headline wall-clock) and
`risk_scenario_s` (the prose gap between the driver finishing and render)
back into the same findings file — so timing is script-owned, not stamped by
the orchestrator's memory.

Subprocess-based to mirror real invocation. A findings WITHOUT `run_start_epoch`
must be left unchanged (no crash, no spurious timing keys).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
_REPORT_SCRIPT = _SKILL_DIR / "scripts" / "report.py"


def _findings(extra_timings: dict | None) -> dict:
    f = {
        "id": "f1",
        "pattern": "P14.7",
        "severity": "HIGH",
        "title": "pull_request_target job writes the shared cache",
        "workflow_file": ".github/workflows/a.yml",
        "line": 3,
        "evidence": "3: on: pull_request_target",
        "fix_strategy": "switch-pull-request-target-to-pull-request",
        "fix_recipe_anchor": "p147",
    }
    doc = {
        "findings": [f],
        "repo": "x/y",
        "scanned_workflows": 1,
        # The scan always records its network-gated check statuses; report.py
        # must render them without the timing write-back caring either way.
        "gh_checks": {
            "P14.11": "skipped: gh unavailable (network-gated check did NOT run)"
        },
    }
    if extra_timings is not None:
        doc["timings"] = extra_timings
    return doc


def _run_report(in_path: Path, out_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_REPORT_SCRIPT), "--in", str(in_path), "--out", str(out_path)],
        capture_output=True,
        text=True,
    )


def test_report_closes_timing_loop(tmp_path: Path) -> None:
    """With `run_start_epoch` + `scripted_end_epoch` a few seconds in the past,
    report.py writes `total_run_s` (> 0) and — when a scenario was actually
    written — `risk_scenario_s` back into the findings file."""
    now = time.time()
    in_path = tmp_path / "findings.json"
    out_path = tmp_path / "report.md"
    doc = _findings(
        {
            "run_start_epoch": round(now - 5, 3),
            "scripted_end_epoch": round(now - 3, 3),
            "scripted_total_s": 2.0,
        }
    )
    doc["findings"][0]["attacker_scenario"] = (
        "Anyone who can open a fork PR poisons the shared cache."
    )
    in_path.write_text(json.dumps(doc), encoding="utf-8")

    proc = _run_report(in_path, out_path)
    assert proc.returncode == 0, f"report.py exited {proc.returncode}\n{proc.stderr}"
    assert out_path.exists()

    data = json.loads(in_path.read_text(encoding="utf-8"))
    timings = data["timings"]
    assert "total_run_s" in timings and timings["total_run_s"] > 0
    assert "risk_scenario_s" in timings and timings["risk_scenario_s"] >= 0
    # total spans the whole run; the prose gap is a strict subset of it.
    assert timings["total_run_s"] >= timings["risk_scenario_s"]
    # …and it never undercuts the scripted phase it contains.
    assert timings["total_run_s"] >= timings["scripted_total_s"]


def test_total_run_never_undercuts_the_scripted_phase(tmp_path: Path) -> None:
    """The driver's start and scan.py's wall-clock anchor are different
    instants, so `now - run_start_epoch` could come out smaller than the
    scripted phase it is supposed to contain."""
    now = time.time()
    in_path = tmp_path / "findings.json"
    out_path = tmp_path / "report.md"
    in_path.write_text(
        json.dumps(
            _findings(
                {
                    # anchor stamped AFTER the driver started: a naive
                    # subtraction gives 1s total over a 30s scripted phase.
                    "run_start_epoch": round(now - 1, 3),
                    "scripted_end_epoch": round(now, 3),
                    "scripted_total_s": 30.0,
                }
            )
        ),
        encoding="utf-8",
    )
    assert _run_report(in_path, out_path).returncode == 0
    timings = json.loads(in_path.read_text(encoding="utf-8"))["timings"]
    assert timings["total_run_s"] >= 30.0, timings


def test_no_scenario_means_no_scenario_timing(tmp_path: Path) -> None:
    """`risk_scenario_s` used to be stamped whenever report.py ran, so idle
    wall-clock — an operator reading the report, a session left open — was
    billed as time spent writing prose that never ran."""
    now = time.time()
    in_path = tmp_path / "findings.json"
    out_path = tmp_path / "report.md"
    in_path.write_text(
        json.dumps(
            _findings(
                {
                    "run_start_epoch": round(now - 600, 3),
                    "scripted_end_epoch": round(now - 590, 3),
                    "scripted_total_s": 10.0,
                }
            )
        ),
        encoding="utf-8",
    )
    assert _run_report(in_path, out_path).returncode == 0
    timings = json.loads(in_path.read_text(encoding="utf-8"))["timings"]
    assert "risk_scenario_s" not in timings, timings
    assert timings["total_run_s"] > 0


def test_re_rendering_does_not_inflate_the_recorded_run_time(
    tmp_path: Path,
) -> None:
    """Re-rendering the same findings file is not more run time.

    `total_run_s` was recomputed from `now` on every render, so a run's
    recorded duration grew every time anyone regenerated the report to look at
    a change — the number drifted away from the run it describes.
    """
    now = time.time()
    in_path = tmp_path / "findings.json"
    out_path = tmp_path / "report.md"
    doc = _findings({
        "run_start_epoch": round(now - 5, 3),
        "scripted_end_epoch": round(now - 3, 3),
        "scripted_total_s": 2.0,
    })
    doc["findings"][0]["attacker_scenario"] = (
        "Anyone who can open a fork PR poisons the shared cache."
    )
    in_path.write_text(json.dumps(doc), encoding="utf-8")

    assert _run_report(in_path, out_path).returncode == 0
    first = json.loads(in_path.read_text(encoding="utf-8"))["timings"]
    time.sleep(1.1)
    assert _run_report(in_path, out_path).returncode == 0
    second = json.loads(in_path.read_text(encoding="utf-8"))["timings"]

    assert second["total_run_s"] == first["total_run_s"], (first, second)
    assert second["risk_scenario_s"] == first["risk_scenario_s"], (first, second)


def test_a_stale_scenario_timing_is_removed_not_left_behind(
    tmp_path: Path,
) -> None:
    """Scenarios gone from the findings means the prose time is not this run's.

    Skipping the stamp was not enough — an earlier render's `risk_scenario_s`
    stayed in the file and kept billing prose time to a run whose findings now
    carry no prose at all.
    """
    now = time.time()
    in_path = tmp_path / "findings.json"
    out_path = tmp_path / "report.md"
    doc = _findings({
        "run_start_epoch": round(now - 20, 3),
        "scripted_end_epoch": round(now - 10, 3),
        "scripted_total_s": 10.0,
        "risk_scenario_s": 99.0,          # left by an earlier render
    })
    in_path.write_text(json.dumps(doc), encoding="utf-8")

    assert _run_report(in_path, out_path).returncode == 0
    timings = json.loads(in_path.read_text(encoding="utf-8"))["timings"]
    assert "risk_scenario_s" not in timings, timings


def test_report_leaves_findings_without_start_epoch_unchanged(tmp_path: Path) -> None:
    """A findings WITHOUT `run_start_epoch` must not crash report.py and must
    not gain a `total_run_s` — the loop only closes when run.py stamped a start.
    """
    in_path = tmp_path / "findings.json"
    out_path = tmp_path / "report.md"
    original = _findings(None)  # no timings block at all
    in_path.write_text(json.dumps(original), encoding="utf-8")

    proc = _run_report(in_path, out_path)
    assert proc.returncode == 0, f"report.py exited {proc.returncode}\n{proc.stderr}"
    assert out_path.exists()

    data = json.loads(in_path.read_text(encoding="utf-8"))
    # No timing keys were injected; the file is semantically unchanged.
    assert "timings" not in data or "total_run_s" not in data.get("timings", {})


def test_report_cli_renders_the_descoped_contract(tmp_path: Path) -> None:
    """End-to-end through the CLI (`--in` / `--out`), the written report
    carries the contract surfaces: the verbatim scope line and the loud
    skipped-check rendering. Subprocess-based so it mirrors real invocation
    including the module's import-safety."""
    in_path = tmp_path / "findings.json"
    out_path = tmp_path / "report.md"
    in_path.write_text(json.dumps(_findings(None)), encoding="utf-8")

    proc = _run_report(in_path, out_path)
    assert proc.returncode == 0, f"report.py exited {proc.returncode}\n{proc.stderr}"

    md = out_path.read_text(encoding="utf-8")
    assert (
        "Critical exploit-chain checks only — this is not a comprehensive audit."
        in md
    )
    assert "SKIPPED" in md and "NOT a pass" in md and "P14.11" in md
    # No fix-complexity Risk row/column (that left with score_risk.py). The
    # Fix block's authored `Risk of the change:` line is a different surface.
    assert "| **Risk** |" not in md and "| Risk |" not in md
    # The pre-drawn banner is part of the CLI contract: the orchestrator
    # copies it verbatim, so it must be in the rendered file.
    assert "CI Secure   1 critical finding  ▏1 of 10 vectors hit▕" in md
    assert "impostor check SKIPPED" in md


def test_render_plan_cli_lists_every_group(tmp_path: Path) -> None:
    """`--render-plan` still works and now returns every group present in the
    findings — nothing is trimmed out of the plan."""
    doc = _findings(None)
    doc["findings"].append({
        "id": "f2",
        "pattern": "P14.24",
        "severity": "MEDIUM",
        "title": "Unverified remote script",
        "workflow_file": ".github/workflows/b.yml",
        "line": 9,
        "evidence": "9: run: curl ... | bash",
        "fix_strategy": "pin-and-verify-remote-script",
        "fix_recipe_anchor": "p1424",
    })
    in_path = tmp_path / "findings.json"
    in_path.write_text(json.dumps(doc), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(_REPORT_SCRIPT), "--render-plan", "--in", str(in_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(proc.stdout)
    assert {g["pattern"] for g in plan} == {"P14.7", "P14.24"}

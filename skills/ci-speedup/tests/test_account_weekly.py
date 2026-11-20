"""Account-level weekly context and paired-run methodology."""
from __future__ import annotations

import sys
import json
import subprocess
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path  # noqa: E402


def _observations(*, after_hours: float = 60.0) -> dict:
    return {
        "weeks": [
            {
                "week_start": "2026-11-02",
                "runner_hours": 100.0,
                "jobs": 1_000,
                "merged_pull_requests": 50,
                "workload_change": "baseline week",
            },
            {
                "week_start": "2026-11-09",
                "runner_hours": after_hours,
                "jobs": 700,
                "merged_pull_requests": 40,
                "workload_change": "fewer scheduled validation jobs",
            },
        ],
        "paired_runs": {
            "before_runner_minutes": [120.0, 100.0],
            "after_runner_minutes": [65.0, 55.0],
        },
    }


def _run_weekly_cli(tmp_path: Path, payload: dict) -> tuple[subprocess.CompletedProcess[str], dict]:
    repo = tmp_path / "repo"
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo ok\n",
        encoding="utf-8",
    )
    source = tmp_path / "account.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "findings.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS / "run.py"),
            "--root", str(repo),
            "--out", str(output),
            "--account-weekly-series", str(source),
            "--skill-commit-sha", "".join(["abc", "def", "0"]),
            "--commit-sha", "".join(["01234567"] * 5),
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    doc = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    return completed, doc


def test_account_weekly_series_normalizes_and_compares_paired_medians(tmp_path: Path) -> None:
    completed, doc = _run_weekly_cli(tmp_path, _observations())
    assert completed.returncode == 0, completed.stderr
    summary = doc.get("account_weekly")
    assert isinstance(summary, dict), "run.py did not stamp the optional weekly series"

    assert summary["weeks"][0]["runner_hours_per_merged_pr"] == 2.0
    assert summary["weeks"][1]["runner_hours_per_merged_pr"] == 1.5
    assert summary["paired_comparison"] == {
        "available": True,
        "before_n": 2,
        "after_n": 2,
        "before_median_runner_minutes": 110.0,
        "after_median_runner_minutes": 60.0,
        "runner_minute_effect": "saving",
        "runner_minute_delta": 50.0,
        "runner_minute_change_pct": -45.5,
    }

    report = blocking_path.render({
        "repo": "example/repo",
        "repo_visibility": "private",
        "skill_commit_sha": "".join(["abc", "def", "0"]),
        "commit_sha": "".join(["01234567"] * 2),
        "findings": [],
        "account_weekly": summary,
        "data_sources": {"gh": "unavailable"},
    })
    assert "## Account-level weekly CI" in report
    assert "100.0" in report and "60.0" in report
    assert "2.00" in report and "1.50" in report
    assert "fewer scheduled validation jobs" in report
    assert "runner-minute saving: 50.0 per paired run" in report
    assert "median of 2 before and 2 after runs" in report


def test_account_weekly_series_states_no_saving_when_work_is_unchanged(tmp_path: Path) -> None:
    data = _observations(after_hours=100.0)
    data["paired_runs"] = {
        "before_runner_minutes": [60.0, 60.0],
        "after_runner_minutes": [60.0, 60.0],
    }
    completed, doc = _run_weekly_cli(tmp_path, data)
    assert completed.returncode == 0, completed.stderr
    summary = doc["account_weekly"]

    assert summary["paired_comparison"]["runner_minute_effect"] == "unchanged"
    assert summary["paired_comparison"]["runner_minute_delta"] == 0.0


def test_account_weekly_series_withholds_a_one_run_per_side_claim(tmp_path: Path) -> None:
    data = _observations()
    data["paired_runs"] = {
        "before_runner_minutes": [120.0],
        "after_runner_minutes": [55.0],
    }
    completed, doc = _run_weekly_cli(tmp_path, data)
    assert completed.returncode == 0, completed.stderr
    summary = doc["account_weekly"]

    assert summary["paired_comparison"] == {
        "available": False,
        "before_n": 1,
        "after_n": 1,
        "reason": "need at least 2 paired runs per side",
    }


def test_account_weekly_series_requires_the_workload_change(tmp_path: Path) -> None:
    data = _observations()
    del data["weeks"][1]["workload_change"]

    completed, _doc = _run_weekly_cli(tmp_path, data)
    assert completed.returncode == 1
    assert "workload_change" in completed.stderr


def test_invalid_account_weekly_series_stops_before_the_scan(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """A bad export is rejected before scan.py or the gh collection pass run."""
    import run

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("scan/collect ran before the weekly export was validated")

    monkeypatch.setattr(run, "_step", _must_not_run)
    data = _observations()
    del data["weeks"][0]["workload_change"]
    source = tmp_path / "account.json"
    source.write_text(json.dumps(data), encoding="utf-8")

    rc = run.main([
        "--root", str(tmp_path), "--out", str(tmp_path / "findings.json"),
        "--account-weekly-series", str(source),
        "--skill-commit-sha", "abcdef0", "--commit-sha", "0123456789abcdef",
    ])
    assert rc == 1
    assert "workload_change" in capsys.readouterr().err


def test_invalid_account_weekly_series_stops_before_verification_reruns(
        tmp_path: Path, monkeypatch) -> None:
    import run

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("verification started before the weekly export was validated")

    monkeypatch.setattr(run, "advance_verification", _must_not_run)
    monkeypatch.setattr(run, "GhVerificationClient", lambda: object())
    out = tmp_path / "findings.json"
    out.write_text("{}", encoding="utf-8")
    source = tmp_path / "account.json"
    source.write_text("{not json", encoding="utf-8")

    rc = run.main([
        "--root", str(tmp_path), "--out", str(out), "--repo", "example/repo",
        "--verify-branch", "fix", "--account-weekly-series", str(source),
    ])
    assert rc == 1


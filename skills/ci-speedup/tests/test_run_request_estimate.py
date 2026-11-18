"""The data pass prices its GitHub API work before spending the shared quota."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path


_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "ci_speedup_run_request_estimate", _SCRIPTS / "run.py")
run_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_mod)


def test_probe_estimates_requests_and_logs_within_its_stated_range(monkeypatch):
    responses = {
        "repos/o/r/actions/workflows?per_page=100": {
            "total_count": 3, "workflows": [{"id": 1}, {"id": 2}, {"id": 3}]},
        "repos/o/r/actions/runs?per_page=10": {
            "total_count": 80, "workflow_runs": [{"id": 101}]},
        "repos/o/r/actions/runs/101/jobs?per_page=100": {
            "total_count": 6, "jobs": [{}] * 6},
        "rate_limit": {"resources": {"core": {"remaining": 5000, "reset": 2_000_000_000}}},
    }
    monkeypatch.setattr(run_mod, "_gh_json", lambda endpoint: responses[endpoint])

    estimate = run_mod._estimate_data_pass("o/r", with_logs=True)

    # A representative fixture's 67 calls is inside the advertised interval.
    assert estimate.request_low <= 67 <= estimate.request_high
    assert estimate.request_high >= 100  # the 100-commit PR sample is priced
    assert estimate.log_low == 0
    assert estimate.log_high > 0
    assert estimate.remaining == 5000
    assert estimate.probe_requests == 4


def test_large_estimate_requires_confirmation_before_the_data_pass(monkeypatch):
    estimate = run_mod._RequestEstimate(
        request_low=80, request_high=120, log_low=0, log_high=5,
        remaining=200, reset_epoch=2_000_000_000, probe_requests=4)
    answers: list[str] = []

    monkeypatch.setattr("builtins.input", lambda prompt: answers.append(prompt) or "n")

    assert run_mod._confirm_request_budget(estimate) is False
    assert answers and "half" in answers[0].lower()


def test_small_estimate_does_not_prompt(monkeypatch):
    estimate = run_mod._RequestEstimate(
        request_low=10, request_high=20, log_low=0, log_high=0,
        remaining=100, reset_epoch=2_000_000_000, probe_requests=4)
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(
        AssertionError("small estimates must not prompt")))
    assert run_mod._confirm_request_budget(estimate) is True


def test_log_downloads_count_toward_the_confirmation_budget(monkeypatch):
    estimate = run_mod._RequestEstimate(
        request_low=20, request_high=40, log_low=0, log_high=20,
        remaining=100, reset_epoch=2_000_000_000, probe_requests=4)
    answers: list[str] = []

    monkeypatch.setattr("builtins.input", lambda prompt: answers.append(prompt) or "n")

    assert run_mod._confirm_request_budget(estimate) is False
    assert answers and "60" in answers[0]


def test_probe_without_a_recent_run_counts_only_the_requests_it_made(monkeypatch):
    responses = {
        "repos/o/r/actions/workflows?per_page=100": {"total_count": 1, "workflows": []},
        "repos/o/r/actions/runs?per_page=10": {"total_count": 0, "workflow_runs": []},
        "rate_limit": {"resources": {"core": {"remaining": 5000, "reset": 1}}},
    }
    calls: list[str] = []

    def fake_gh(endpoint):
        calls.append(endpoint)
        return responses[endpoint]

    monkeypatch.setattr(run_mod, "_gh_json", fake_gh)
    estimate = run_mod._estimate_data_pass("o/r", with_logs=False)

    assert estimate.probe_requests == len(calls) == 3


def test_one_workflow_log_range_includes_cache_probe_limits(monkeypatch):
    responses = {
        "repos/o/r/actions/workflows?per_page=100": {"total_count": 1},
        "repos/o/r/actions/runs?per_page=10": {"workflow_runs": [{"id": 101}]},
        "repos/o/r/actions/runs/101/jobs?per_page=100": {"total_count": 1},
        "rate_limit": {"resources": {"core": {"remaining": 5000, "reset": 1}}},
    }
    monkeypatch.setattr(run_mod, "_gh_json", lambda endpoint: responses[endpoint])
    assert run_mod._estimate_data_pass("o/r", with_logs=True).log_high >= 4


def test_post_fix_verification_never_runs_the_bulk_request_probe(monkeypatch, tmp_path):
    findings = tmp_path / "findings.json"
    findings.write_text('{"findings": []}', encoding="utf-8")
    monkeypatch.setattr(run_mod, "_estimate_data_pass", lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("post-fix verification must not price a data pass it will not run")))
    monkeypatch.setattr(run_mod, "advance_verification", lambda *_a, **_kw: {"status": "done"})

    assert run_mod.main([
        "--root", str(tmp_path), "--out", str(findings), "--repo", "o/r",
        "--verify-branch", "fix/topic",
    ]) == 0


def test_offline_replay_never_runs_the_live_request_probe(monkeypatch, tmp_path):
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(tmp_path / "fixtures"))
    monkeypatch.setattr(run_mod, "_estimate_data_pass", lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("offline replay must not contact the live GitHub API")))

    def fake_step(cmd, *, capture_stdout):
        if any(str(part).endswith("scan.py") for part in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout='{"findings": []}')
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(run_mod, "_step", fake_step)
    assert run_mod.main([
        "--root", str(tmp_path), "--out", str(tmp_path / "findings.json"),
        "--repo", "o/r", "--skill-commit-sha", "deadbee", "--commit-sha", "cafef00",
    ]) == 0


def test_main_prints_estimate_then_counts_probe_calls_in_actual_total(
        monkeypatch, tmp_path, capsys):
    estimate = run_mod._RequestEstimate(10, 20, 0, 0, 5000, 2_000_000_000, 4)

    def fake_estimate(*_args, **kwargs):
        kwargs["probe"].requests = 4
        return estimate

    monkeypatch.setattr(run_mod, "_estimate_data_pass", fake_estimate)

    def fake_step(cmd, *, capture_stdout):
        if any(str(part).endswith("scan.py") for part in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout='{"findings": []}')
        out = Path(cmd[cmd.index("--out") + 1])
        doc = json.loads(out.read_text(encoding="utf-8"))
        doc["data_sources"] = {"gh_query_count": 10, "gh_error_count": 0}
        out.write_text(json.dumps(doc), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(run_mod, "_step", fake_step)
    out = tmp_path / "findings.json"
    assert run_mod.main([
        "--root", str(tmp_path), "--out", str(out), "--repo", "o/r",
        "--skill-commit-sha", "deadbee", "--commit-sha", "cafef00",
    ]) == 0
    stdout = capsys.readouterr().out
    assert stdout.index("Data-pass request estimate: 10-20") < stdout.index(
        "Actual gh API cost: 14 call(s)")


def test_failed_preflight_still_counts_each_probe_it_issued(monkeypatch, tmp_path, capsys):
    calls: list[str] = []

    def failing_gh(endpoint):
        calls.append(endpoint)
        if len(calls) == 3:
            raise RuntimeError("rate endpoint unavailable")
        if "workflows" in endpoint:
            return {"total_count": 1}
        return {"workflow_runs": []}

    monkeypatch.setattr(run_mod, "_gh_json", failing_gh)

    def fake_step(cmd, *, capture_stdout):
        if any(str(part).endswith("scan.py") for part in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout='{"findings": []}')
        out = Path(cmd[cmd.index("--out") + 1])
        doc = json.loads(out.read_text(encoding="utf-8"))
        doc["data_sources"] = {"gh_query_count": 10, "gh_error_count": 0}
        out.write_text(json.dumps(doc), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(run_mod, "_step", fake_step)
    assert run_mod.main([
        "--root", str(tmp_path), "--out", str(tmp_path / "findings.json"),
        "--repo", "o/r", "--skill-commit-sha", "deadbee", "--commit-sha", "cafef00",
    ]) == 0
    assert len(calls) == 3
    assert "Actual gh API cost: 13 call(s)" in capsys.readouterr().out

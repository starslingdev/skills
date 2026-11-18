"""The data pass prices its GitHub API work before spending the shared quota."""
from __future__ import annotations

import importlib.util
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

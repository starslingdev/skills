"""Infrastructure failures are scoped to the first failing step, never assertions."""
import collect_runs as cr
import blocking_path as bp
import pytest


def job(i, infra=True, runner="worker-a", attempt=1, conclusion="failure"):
    return {"id": i, "name": "checks", "run_id": i // 10,
            "run_attempt": attempt, "head_sha": str(i // 10),
            "runner_name": runner, "labels": ["self-hosted", "linux"],
            "conclusion": conclusion, "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:02:00Z",
            "steps": [{"name": "Run checks", "number": 1, "conclusion": conclusion,
                       "started_at": "2026-01-01T00:00:00Z",
                       "completed_at": "2026-01-01T00:02:00Z"}],
            "html_url": f"https://github.com/example/project/actions/jobs/{i}"}


def detect(n=6, total=20, **kw):
    jobs = [job(i * 10) for i in range(total)]
    logs = {j["id"]: "2026-01-01T00:01:00Z " + (
        "The runner has received a shutdown signal" if i < n else
        "AssertionError: expected true, received false") for i, j in enumerate(jobs)}
    return cr._detect_opt100_infra_faults(".github/workflows/check.yml", jobs,
                                        logs, {"checks"}, 0, **kw)


def test_infra_six_of_twenty_class_breakdown():
    findings, notes = detect()
    assert findings[0]["pattern"] == "OPT100"
    assert findings[0]["infra_failures"]["classes"] == {"lost-runner": 6}
    assert findings[0]["infra_failures"]["failed_jobs"] == 20
    assert findings[0]["runner_min_saving"] == 0


def test_assertions_keep_general_failure_signal():
    findings, _ = detect(0)
    assert findings == []
    assert cr._SIZING["OPT48"]["model"] == "measured"


def test_runner_cluster_named_in_prompt():
    findings, _ = detect()
    assert "worker-a" in findings[0]["infra_failures"]["degraded_runner"]
    assert "worker-a" in findings[0]["agent_prompt"]
    assert "sling" in findings[0]["agent_prompt"]


@pytest.mark.parametrize("n,total", [(4, 20), (5, 26)])
def test_below_count_or_share_threshold(n, total):
    assert detect(n, total)[0] == []


@pytest.mark.parametrize("text", ["AssertionError: expected true", "test asserts browserType.launch fails", "expected Killed but received OK", "assert exit code 137 equals expected", "ECONNRESET assertion failed"])
def test_signature_table_never_matches_assertions(text):
    assert bp._classify_infra_failure(text, "Run tests") is None


@pytest.mark.parametrize("text,step,expected", [
    ("The runner has received a shutdown signal", "Run checks", "lost-runner"),
    ("Process completed with exit code 137.", "Run checks", "oom"),
    ("Killed", "Run checks", "oom"),
    ("Target page, context or browser has been closed", "Run browser", "browser-crash"),
    ("npm ERR! code ECONNRESET", "npm ci", "network"),
    ("502 Bad Gateway registry.npmjs.org", "Install packages", "network"),
    ("ECONNRESET", "Run unit tests", None),
])
def test_signature_classes(text, step, expected):
    assert bp._classify_infra_failure(text, step) == expected


def test_only_first_failed_step_log_is_classified():
    j = job(10)
    j["steps"].append({"name": "Cleanup", "number": 2, "conclusion": "failure", "started_at": "2026-01-01T00:02:01Z", "completed_at": "2026-01-01T00:03:00Z"})
    log = "2026-01-01T00:01:00Z AssertionError\n2026-01-01T00:02:30Z Killed"
    assert cr._opt100_job_class(j, log) is None


def test_missing_logs_are_disclosed_not_inferred():
    jobs = [job(i) for i in range(20)]
    result, notes = cr._detect_opt100_infra_faults("ci.yml", jobs, {}, {"checks"}, 0)
    assert result == []
    assert notes["unreadable_logs"] == 20


def test_only_gating_jobs_fire():
    jobs = [job(i) for i in range(6)]
    logs = {j["id"]: "2026-01-01T00:01:00Z Killed" for j in jobs}
    assert cr._detect_opt100_infra_faults("ci.yml", jobs, logs, set(), 0)[0] == []


def test_retry_minutes_and_delay_from_sampled_attempts_only():
    jobs, logs = [], {}
    for i in range(6):
        failed, passed = job(i * 10), job(i * 10 + 1, attempt=2, conclusion="success")
        passed.update(started_at="2026-01-01T00:05:00Z", completed_at="2026-01-01T00:07:00Z")
        jobs.extend([failed, passed])
        logs[failed["id"]] = "2026-01-01T00:01:00Z Killed"
    f = cr._detect_opt100_infra_faults("ci.yml", jobs, logs, {"checks"}, 0, monthly_volume=6, sample_denominator=6)[0][0]
    assert f["runner_min_saving"] == 12
    assert f["wall_clock_p50_s"] == 90
    assert f["infra_failures"]["rerun_delay_p50_s"] == 180


def test_live_collection_fetches_first_failed_step_logs(monkeypatch):
    jobs = [job(i) for i in range(6)]
    fetched = []
    def fetch(client, repo, j):
        fetched.append(j["id"])
        return "2026-01-01T00:01:00Z Killed"
    monkeypatch.setattr(cr, "_fetch_job_log", fetch)
    findings, notes = cr._collect_opt100(object(), "example/project", "ci.yml", jobs, {"checks"}, 0)
    assert len(findings) == 1
    assert len(fetched) == 6
    assert notes["unreadable_logs"] == 0


def test_unclassified_fail_then_pass_adds_quarantine_pointer():
    jobs = [job(i * 10) for i in range(7)]
    jobs.append(job(61, attempt=2, conclusion="success"))
    logs = {j["id"]: "2026-01-01T00:01:00Z " + ("Killed" if i < 6 else "AssertionError") for i, j in enumerate(jobs[:7])}
    f = cr._detect_opt100_infra_faults("ci.yml", jobs, logs, {"checks"}, 0)[0][0]
    assert f["infra_failures"]["flakes_observed"]
    assert "quarantine" in f["agent_prompt"]


def test_matrix_gating_names_are_resolved_from_observed_jobs():
    graph = {"checks": {"name": "checks", "matrix": True}}
    assert cr._opt100_gating_names([dict(name="checks (linux)")], graph, {"checks": {}}) == {"checks (linux)"}


def test_missing_job_page_withholds_ratio(monkeypatch):
    monkeypatch.setattr(cr, "_fetch_job_log", lambda *a: "2026-01-01T00:01:00Z Killed")
    findings, notes = cr._collect_opt100(object(), "example/project", "ci.yml", [job(i) for i in range(6)], {"checks"}, 0, complete_population=False)
    assert findings == []
    assert notes["incomplete_job_population"]


def test_below_count_floor_never_fetches_logs(monkeypatch):
    calls = []
    monkeypatch.setattr(cr, "_fetch_job_log", lambda *a: calls.append(1))
    cr._collect_opt100(object(), "example/project", "ci.yml", [job(i) for i in range(4)], {"checks"}, 0)
    assert calls == []


def test_monthly_scaling_preserves_observed_retry_minutes():
    jobs, logs = [], {}
    for i in range(6):
        failed, passed = job(i * 10), job(i * 10 + 1, attempt=2, conclusion="success")
        jobs.extend([failed, passed])
        logs[failed["id"]] = "2026-01-01T00:01:00Z Killed"
    f = cr._detect_opt100_infra_faults("ci.yml", jobs, logs, {"checks"}, 0, monthly_volume=12, sample_denominator=6)[0][0]
    assert f["runner_min_saving"] == 24
    assert f["infra_failures"]["sampled_rerun_minutes"] == 12
    assert f["infra_failures"]["monthly_scale"] == 2


def test_unknown_monthly_volume_never_labels_sample_as_monthly():
    jobs = [job(i*10) for i in range(6)] + [job(i*10+1,attempt=2,conclusion="success") for i in range(6)]
    logs = {j["id"]: "2026-01-01T00:01:00Z Killed" for j in jobs[:6]}
    f = cr._detect_opt100_infra_faults("ci.yml", jobs, logs, {"checks"}, 0)[0][0]
    assert f["runner_min_saving"] is None
    assert f["infra_failures"]["sampled_rerun_minutes"] == 12


def test_run_failure_upper_bound_avoids_impossible_candidates():
    assert not cr._opt100_failure_possible([dict(conclusion="failure",run_attempt=1)]*4)
    assert cr._opt100_failure_possible([dict(conclusion="success",run_attempt=6)])


def test_browser_launch_configuration_error_is_not_infrastructure():
    assert bp._classify_infra_failure("Error: browserType.launch: Executable doesn't exist", "Run browser tests") is None


def test_missing_prior_attempt_page_withholds_ratio(monkeypatch):
    monkeypatch.setattr(cr, "_fetch_job_log", lambda *a: "2026-01-01T00:01:00Z Killed")
    findings, notes = cr._collect_opt100(object(), "example/project", "ci.yml", [job(i) for i in range(6)], {"checks"}, 0, prior_attempt_failures=1)
    assert findings == []
    assert notes["incomplete_job_population"]

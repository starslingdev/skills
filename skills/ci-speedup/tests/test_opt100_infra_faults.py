"""Infrastructure failures are scoped to the first failing step, never assertions."""
import collect_runs as cr
import blocking_path as bp
import pytest

OOM = "##[error]Process completed with exit code 137."


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
    class Counts:
        def json(self, endpoint):
            return {"total_count": 120 if "status=failure" in endpoint else 300}
    general = cr._detect_opt48_failure_rate(Counts(), "example/project", 1, "ci.yml", 120, 420, 0)
    assert [f["pattern"] for f in general] == ["OPT48"]
    assert general[0]["advisory"]


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
    (OOM, "Run checks", "oom"),
    ("Error: page.goto: Target crashed", "Run browser", "browser-crash"),
    ("npm ERR! code ECONNRESET", "npm ci", "network"),
    ("502 Bad Gateway registry.npmjs.org", "Install packages", "network"),
    ("ECONNRESET", "Run unit tests", None),
    ("ECONNRESET", "pnpm test", None),
    ("ETIMEDOUT", "yarn test", None),
    ("EAI_AGAIN", "pip check", None),
    ("ECONNRESET", "pnpm install", "network"),
    ("ETIMEDOUT", "yarn install", "network"),
    ("EAI_AGAIN", "pip install", "network"),
])
def test_signature_classes(text, step, expected):
    assert bp._classify_infra_failure(text, step) == expected


def test_only_first_failed_step_log_is_classified():
    j = job(10)
    j["steps"].append({"name": "Cleanup", "number": 2, "conclusion": "failure", "started_at": "2026-01-01T00:02:01Z", "completed_at": "2026-01-01T00:03:00Z"})
    log = "2026-01-01T00:01:00Z AssertionError\n2026-01-01T00:02:30Z " + OOM
    assert cr._opt100_job_class(j, log) is None


def test_missing_logs_are_disclosed_not_inferred():
    jobs = [job(i) for i in range(20)]
    result, notes = cr._detect_opt100_infra_faults("ci.yml", jobs, {}, {"checks"}, 0)
    assert result == []
    assert notes["unreadable_logs"] == 20


def test_only_gating_jobs_fire():
    jobs = [job(i) for i in range(6)]
    logs = {j["id"]: "2026-01-01T00:01:00Z " + OOM for j in jobs}
    assert cr._detect_opt100_infra_faults("ci.yml", jobs, logs, set(), 0)[0] == []


def test_retry_minutes_and_delay_from_sampled_attempts_only():
    jobs, logs = [], {}
    for i in range(6):
        failed, passed = job(i * 10), job(i * 10 + 1, attempt=2, conclusion="success")
        passed.update(started_at="2026-01-01T00:05:00Z", completed_at="2026-01-01T00:07:00Z")
        setup = job(i * 10 + 2, conclusion="success")
        setup.update(name="setup", completed_at="2026-01-01T00:00:30Z")
        carried = dict(setup, id=i * 10 + 3, run_attempt=2)
        jobs.extend([failed, passed, setup, carried])
        logs[failed["id"]] = "2026-01-01T00:01:00Z " + OOM
    f = cr._detect_opt100_infra_faults("ci.yml", jobs, logs, {"checks"}, 0, monthly_volume=6, sample_denominator=6)[0][0]
    assert f["runner_min_saving"] == 12
    assert f["wall_clock_p50_s"] == 90
    assert f["infra_failures"]["rerun_delay_p50_s"] == 180


def test_live_collection_fetches_first_failed_step_logs(monkeypatch):
    jobs = [job(i) for i in range(6)]
    fetched = []
    def fetch(client, repo, j):
        fetched.append(j["id"])
        return "2026-01-01T00:01:00Z " + OOM
    monkeypatch.setattr(cr, "_fetch_job_log", fetch)
    findings, notes = cr._collect_opt100(object(), "example/project", "ci.yml", jobs, {"checks"}, 0)
    assert len(findings) == 1
    assert len(fetched) == 6
    assert notes["unreadable_logs"] == 0


def test_unclassified_fail_then_pass_adds_quarantine_pointer():
    jobs = [job(i * 10) for i in range(7)]
    jobs.append(job(61, attempt=2, conclusion="success"))
    logs = {j["id"]: "2026-01-01T00:01:00Z " + (OOM if i < 6 else "AssertionError") for i, j in enumerate(jobs[:7])}
    f = cr._detect_opt100_infra_faults("ci.yml", jobs, logs, {"checks"}, 0)[0][0]
    assert f["infra_failures"]["flakes_observed"]
    assert "quarantine" in f["agent_prompt"]


def test_matrix_gating_names_are_resolved_from_observed_jobs():
    graph = {"checks": {"name": "checks", "matrix": True}}
    assert cr._opt100_gating_names([dict(name="checks (linux)")], graph, {"checks": {}}) == {"checks (linux)"}


def test_missing_job_page_withholds_ratio(monkeypatch):
    monkeypatch.setattr(cr, "_fetch_job_log", lambda *a: "2026-01-01T00:01:00Z " + OOM)
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
        logs[failed["id"]] = "2026-01-01T00:01:00Z " + OOM
    f = cr._detect_opt100_infra_faults("ci.yml", jobs, logs, {"checks"}, 0, monthly_volume=12, sample_denominator=6)[0][0]
    assert f["runner_min_saving"] == 24
    assert f["infra_failures"]["sampled_rerun_minutes"] == 12
    assert f["infra_failures"]["monthly_scale"] == 2


def test_unknown_monthly_volume_never_labels_sample_as_monthly():
    jobs = [job(i*10) for i in range(6)] + [job(i*10+1,attempt=2,conclusion="success") for i in range(6)]
    logs = {j["id"]: "2026-01-01T00:01:00Z " + OOM for j in jobs[:6]}
    f = cr._detect_opt100_infra_faults("ci.yml", jobs, logs, {"checks"}, 0)[0][0]
    assert f["runner_min_saving"] is None
    assert f["infra_failures"]["sampled_rerun_minutes"] == 12


def test_run_failure_upper_bound_avoids_impossible_candidates():
    assert not cr._opt100_failure_possible([dict(conclusion="failure",run_attempt=1)]*4)
    assert cr._opt100_failure_possible([dict(conclusion="success",run_attempt=6)])


def test_browser_launch_configuration_error_is_not_infrastructure():
    assert bp._classify_infra_failure("Error: browserType.launch: Executable doesn't exist", "Run browser tests") is None


def test_missing_prior_attempt_page_withholds_ratio(monkeypatch):
    monkeypatch.setattr(cr, "_fetch_job_log", lambda *a: "2026-01-01T00:01:00Z " + OOM)
    findings, notes = cr._collect_opt100(object(), "example/project", "ci.yml", [job(i) for i in range(6)], {"checks"}, 0, prior_attempt_failures=1)
    assert findings == []
    assert notes["incomplete_job_population"]


def test_class_counts_link_to_sampled_jobs_without_raw_log_text():
    f = detect()[0][0]
    rows = f["measured_evidence"]["table"]["rows"]
    assert any("https://github.com/example/project/actions/jobs/" in str(r) for r in rows)
    assert "The runner has received" not in str(f)


def test_seven_digit_log_timestamps_are_classified():
    """Job logs carry seven fractional digits, which older parsers reject."""
    log = ("2026-01-01T00:00:59.9876543Z Running checks\n"
           "2026-01-01T00:01:00.1234567Z ##[error]The runner has received a shutdown signal.")
    assert cr._opt100_job_class(job(10), log) == "lost-runner"


def test_exit_line_after_whole_second_step_end_is_in_window():
    """Step completion is whole seconds; the runner's exit line lands just after it."""
    log = ("2026-01-01T00:01:59.9000000Z Running checks\n"
           "2026-01-01T00:02:00.4000000Z ##[error]Process completed with exit code 137.")
    assert cr._opt100_job_class(job(10), log) == "oom"


def test_window_stops_at_the_steps_own_exit_line():
    """The one-second allowance never reaches the next step's output."""
    log = ("2026-01-01T00:01:00.0000000Z AssertionError\n"
           "2026-01-01T00:02:00.3000000Z ##[error]Process completed with exit code 1.\n"
           "2026-01-01T00:02:00.5000000Z ##[error]The runner has received a shutdown signal.")
    assert cr._opt100_job_class(job(10), log) is None


def test_window_ends_one_second_after_step_end():
    log = "2026-01-01T00:02:01.0000000Z ##[error]The runner has received a shutdown signal."
    assert cr._opt100_job_class(job(10), log) is None


@pytest.mark.parametrize("text", [
    "Error: locator.click: Target page, context or browser has been closed",
    "Test timeout of 30000ms exceeded.",
])
def test_ordinary_browser_timeout_is_not_a_crash(text):
    assert bp._classify_infra_failure(text, "Run e2e tests") is None


@pytest.mark.parametrize("text", [
    "Error: page.goto: Target crashed",
    "Error: the browser has crashed",
    "Browser closed unexpectedly",
    "Error: browser has disconnected",
    "Error: browserType.launch: Target closed",
])
def test_browser_crash_wording(text):
    assert bp._classify_infra_failure(text, "Run e2e tests") == "browser-crash"


def test_assertion_quoting_crash_wording_is_not_infrastructure():
    assert bp._classify_infra_failure(
        "expect(received).toContain(expected): Target crashed", "Run e2e tests") is None


@pytest.mark.parametrize("text,step", [
    ("Packages: +1502", "pnpm install"),
    ("added 1503 packages in 12s", "npm ci"),
    ("Downloading packages (503.2 kB)", "Install dependencies"),
    ("Progress: resolved 503, reused 502, downloaded 1, added 503", "pnpm install"),
])
def test_package_counts_are_not_registry_errors(text, step):
    assert bp._classify_infra_failure(text, step) is None


@pytest.mark.parametrize("text,step", [
    ("npm ERR! 503 Service Unavailable - GET https://registry.example.test/pkg", "npm ci"),
    ("ERROR: HTTP error 503 while getting https://packages.example.test/pkg.whl", "pip install"),
    ("Request failed with status code 502", "yarn install"),
    ("Response status code does not indicate success: 503 (Service Unavailable).", "Set up job"),
    ("ECONNRESET", "Set up job"),
])
def test_registry_http_errors_are_network(text, step):
    assert bp._classify_infra_failure(text, step) == "network"


@pytest.mark.parametrize("step", [
    "Restore test database and run tests",
    "Download fixtures and run tests",
    "Install browsers and run e2e",
])
def test_test_steps_named_for_setup_work_are_not_install_steps(step):
    assert bp._classify_infra_failure("ECONNRESET", step) is None


@pytest.mark.parametrize("text", [
    "Killed",
    "exit code 137",
    "Error: Command failed with exit code 137",
    "child output: ##[error]Process completed with exit code 137.",
])
def test_oom_requires_the_runners_own_exit_line(text):
    """A test's own subprocess may print 137 or Killed; only the runner's step exit counts."""
    assert bp._classify_infra_failure(text, "Run tests") is None


LOST = ("The hosted runner: example-runner-7 lost communication with the server. "
        "Anything in your workflow that terminates the runner process can cause this error.")


def lost_runner_job(i):
    """A lost runner leaves no failed step and no log; only the check run says why."""
    j = job(i)
    j["steps"] = [{"name": "Set up job", "number": 1, "conclusion": "success",
                   "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:00:02Z"},
                  {"name": "Run checks", "number": 2, "conclusion": None,
                   "started_at": "2026-01-01T00:00:02Z", "completed_at": None}]
    return j


class AnnotationClient:
    def __init__(self, messages):
        self.messages, self.calls = messages, []

    def json(self, endpoint, allow_missing=False):
        self.calls.append(endpoint)
        if self.messages is None:
            return None
        return [{"annotation_level": "failure", "message": m} for m in self.messages] + [
            {"annotation_level": "warning", "message": "Node.js 16 actions are deprecated."}]


def test_lost_runner_is_classified_from_check_run_annotations(monkeypatch):
    fetched = []
    monkeypatch.setattr(cr, "_fetch_job_log", lambda c, r, j: fetched.append(j["id"]))
    client = AnnotationClient([LOST])
    jobs = [lost_runner_job((i + 1) * 10) for i in range(6)]
    findings, notes = cr._collect_opt100(client, "example/project", "ci.yml", jobs, {"checks"}, 0)
    assert [f["infra_failures"]["classes"] for f in findings] == [{"lost-runner": 6}]
    assert notes["unreadable_logs"] == 0
    assert fetched == []
    assert client.calls == [f"repos/example/project/check-runs/{(i + 1) * 10}/annotations?per_page=50"
                            for i in range(6)]


def test_unreadable_annotations_are_disclosed_not_inferred(monkeypatch):
    monkeypatch.setattr(cr, "_fetch_job_log", lambda *a: None)
    jobs = [lost_runner_job(i * 10) for i in range(6)]
    findings, notes = cr._collect_opt100(AnnotationClient(None), "example/project", "ci.yml", jobs, {"checks"}, 0)
    assert findings == []
    assert notes["unreadable_logs"] == 6


def test_warning_annotations_never_classify():
    jobs = [lost_runner_job(i * 10) for i in range(6)]
    warn = {j["id"]: [] for j in jobs}
    assert cr._detect_opt100_infra_faults("ci.yml", jobs, {}, {"checks"}, 0, annotations=warn)[0] == []


@pytest.mark.parametrize("text", [
    "##[error]The runner has received a shutdown signal. This can happen when the runner service is stopped.",
    "##[error]Runner worker-3 lost communication with the server.",
    "##[error]The hosted runner: example-2 was lost.",
    "##[error]The cloud provider interrupted the spot instance running this job.",
])
def test_lost_runner_signatures(text):
    assert bp._classify_infra_failure(text, "") == "lost-runner"


def retried_run(i, *later):
    """Attempt 1 fails `checks` on infrastructure; `later` adds jobs for attempt 2+."""
    failed = job(i * 10)
    return [failed] + [dict(job(i * 10 + n + 1), **extra) for n, extra in enumerate(later)]


def test_retry_minutes_skip_jobs_that_never_ran_in_the_failed_attempt():
    """A downstream job skipped behind the failure would have run anyway."""
    jobs, logs = [], {}
    for i in range(1, 7):
        run = retried_run(
            i,
            dict(name="deploy", run_attempt=1, conclusion="skipped", steps=[]),
            dict(run_attempt=2, conclusion="success"),
            dict(name="deploy", run_attempt=2, conclusion="success",
                 started_at="2026-01-01T00:03:00Z", completed_at="2026-01-01T00:13:00Z"))
        jobs.extend(run)
        logs[run[0]["id"]] = "2026-01-01T00:01:00Z " + OOM
    f = cr._detect_opt100_infra_faults("ci.yml", jobs, logs, {"checks"}, 0,
                                      monthly_volume=6, sample_denominator=6)[0][0]
    assert f["infra_failures"]["sampled_rerun_minutes"] == 12
    assert f["runner_min_saving"] == 12

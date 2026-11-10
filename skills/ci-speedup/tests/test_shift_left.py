from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "shift_left_cases.json").read_text())
SPEC = importlib.util.spec_from_file_location("shift_left", ROOT / "scripts" / "shift_left.py")
assert SPEC and SPEC.loader
SHIFT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHIFT)


def record(duration, *, job="test", workflow="ci.yml", workflow_id=1,
           labels=None, steps=None, conclusion="success", pr=None, sha=None):
    return {
        "workflow": workflow,
        "workflow_id": workflow_id,
        "job": job,
        "runner_labels": labels or ["self-hosted", "linux"],
        "step_signature": steps or ["checkout", "test"],
        "duration_s": duration,
        "status": "completed",
        "conclusion": conclusion,
        "pr_number": pr,
        "sha": sha or f"sha-{pr or duration}-{job}",
    }


def kinds(report):
    return [item["kind"] for item in report["observations"]]


def test_noisy_job_does_not_flag_a_slowdown():
    case = FIXTURES["noisy_l2"]
    history = [record(x) for x in case["history"]]
    report = SHIFT.analyze_l2(history, [record(case["current"])])
    assert "slowdown" not in kinds(report)
    assert report["jobs"][0]["evidence"] == "threshold_not_exceeded"


def test_zero_duration_baseline_does_not_crash_or_hide_a_detected_slowdown():
    report = SHIFT.analyze_l2([record(0) for _ in range(8)], [record(3)])
    slowdown = next(item for item in report["observations"] if item["kind"] == "slowdown")
    assert slowdown["change_percent"] is None
    assert report["jobs"][0]["evidence"] == "slowdown"


def test_real_step_change_is_flagged_and_attributed_to_first_merge_where_it_holds():
    case = FIXTURES["step_l3"]
    history = [record(d, pr=p) for d, p in zip(case["durations"], case["prs"])]
    report = SHIFT.analyze_l3(history)
    step = next(item for item in report["observations"] if item["kind"] == "step_change")
    assert step["attribution"]["pr_number"] == 113
    assert step["conclusion"] == "warning"


def test_one_percent_per_merge_creep_is_flagged():
    case = FIXTURES["creep_l3"]
    history = [record(d, pr=p) for d, p in zip(case["durations"], case["prs"])]
    report = SHIFT.analyze_l3(history)
    creep = next(item for item in report["observations"] if item["kind"] == "creep")
    assert creep["change_percent"] > 10
    assert creep["attribution"]["pr_number"] == 204


def test_three_run_spike_followed_by_recovery_is_not_a_step_change():
    values = FIXTURES["spike_then_recovery_l3"]["durations"]
    report = SHIFT.analyze_l3([record(value, pr=index) for index, value in enumerate(values)])
    assert "step_change" not in kinds(report)


def test_single_jump_and_multi_step_shapes_are_not_mislabeled_as_creep():
    for name in ("single_jump_l3", "multi_step_l3"):
        values = FIXTURES[name]["durations"]
        assert SHIFT._creep([record(value) for value in values], 15) is None


def test_renamed_job_is_reported_as_rename_not_slowdown():
    case = FIXTURES["rename_l2"]
    history = [record(x, job=case["previous_job"]) for x in case["history"]]
    report = SHIFT.analyze_l2(history, [record(case["current"], job=case["current_job"])])
    assert kinds(report) == ["renamed_job"]
    assert report["observations"][0]["previous_job"] == case["previous_job"]


def test_new_job_is_not_called_a_slowdown():
    case = FIXTURES["new_job_l2"]
    history = [record(x, job=case["existing_job"]) for x in case["history"]]
    current = [record(case["existing_current"], job=case["existing_job"]),
               record(case["new_current"], job=case["new_job"])]
    report = SHIFT.analyze_l2(history, current)
    new = next(item for item in report["observations"] if item["job"] == case["new_job"])
    assert new["kind"] == "new_job"
    assert "slowdown" not in [x["kind"] for x in report["observations"]
                              if x["job"] == case["new_job"]]


def test_same_duration_with_a_different_step_signature_is_new_not_renamed():
    history = [record(100, job="unit-tests") for _ in range(8)]
    report = SHIFT.analyze_l2(history, [record(100, job="tests", steps=["checkout", "other"])])
    assert kinds(report) == ["new_job"]


def test_multiple_exact_rename_candidates_are_ambiguous():
    history = [record(100, job=name) for name in ("old-a", "old-b") for _ in range(8)]
    report = SHIFT.analyze_l2(history, [record(100, job="tests")])
    assert kinds(report) == ["ambiguous_rename"]
    assert report["observations"][0]["candidates"] == ["old-a", "old-b"]


def test_cancelled_and_failed_runs_are_excluded_from_baseline():
    case = FIXTURES["status_filter"]
    history = [record(x) for x in case["successful"]]
    history += [record(x, conclusion="failure") for x in case["failed"]]
    history += [record(x, conclusion="cancelled") for x in case["cancelled"]]
    report = SHIFT.analyze_l2(history, [record(case["current"])])
    job = report["jobs"][0]
    assert job["baseline_runs"] == 8
    assert "slowdown" in kinds(report)


def test_too_little_history_says_no_baseline_never_no_slowdown():
    case = FIXTURES["no_baseline_l2"]
    report = SHIFT.analyze_l2([record(x) for x in case["history"]],
                              [record(case["current"])])
    assert kinds(report) == ["no_baseline"]
    assert report["jobs"][0]["evidence"] == "no_baseline"
    assert "no slowdown" not in json.dumps(report).lower()


def test_performance_observations_never_make_the_check_red():
    case = FIXTURES["report_only_l2"]
    history = [record(x) for x in case["history"]]
    report = SHIFT.analyze_l2(history, [record(case["current"])])
    assert "slowdown" in kinds(report)
    assert report["job_conclusion"] == "success"


def test_l3_without_an_eligible_identity_reports_no_baseline():
    report = SHIFT.analyze_l3([])
    assert kinds(report) == ["no_baseline"]


def test_main_collection_requests_and_filters_push_runs_only():
    class Client:
        def __init__(self):
            self.endpoints = []

        def get(self, endpoint):
            self.endpoints.append(endpoint)
            if "/actions/workflows?" in endpoint:
                return {"total_count": 1, "workflows": [
                    {"id": 1, "state": "active", "path": ".github/workflows/ci.yml"}
                ]}
            return {"total_count": 0, "workflow_runs": []}

    client = Client()
    main, pull_request, coverage = SHIFT.collect_history(
        "owner/repo", "main", pr_number=None, max_runs=8, client=client)
    assert main == [] and pull_request == []
    assert coverage["partial_workflows"] == {}
    assert any("event=push" in endpoint for endpoint in client.endpoints)


def test_collector_fetches_and_reports_failed_and_cancelled_pr_jobs():
    class Client:
        calls = 0
        budget = 64

        def get(self, endpoint):
            self.calls += 1
            if endpoint.endswith("/pulls/7"):
                return {"head": {"sha": "head"}}
            if "head_sha=head" in endpoint:
                return {"total_count": 1, "workflow_runs": [{
                    "id": 10, "workflow_id": 1, "status": "completed",
                    "conclusion": "failure", "event": "pull_request", "head_sha": "head"
                }]}
            if "/actions/runs/10/jobs" in endpoint:
                return {"total_count": 2, "jobs": [
                    {"name": "test", "status": "completed", "conclusion": "failure",
                     "labels": ["linux"], "steps": [{"name": "test"}]},
                    {"name": "lint", "status": "completed", "conclusion": "cancelled",
                     "labels": ["linux"], "steps": [{"name": "lint"}]},
                ]}
            if "/actions/workflows/1/runs" in endpoint:
                return {"total_count": 0, "workflow_runs": []}
            raise AssertionError(endpoint)

    main, current, _ = SHIFT.collect_history(
        "owner/repo", "main", pr_number=7, max_runs=8, client=Client())
    assert main == []
    assert [row["conclusion"] for row in current] == ["failure", "cancelled"]
    report = SHIFT.analyze_l2(main, current)
    assert [row["kind"] for row in report["observations"]] == ["excluded", "excluded"]


def test_l2_collection_fills_each_pr_identity_from_its_workflow_history():
    class Client:
        calls = 0
        budget = 64

        def get(self, endpoint):
            self.calls += 1
            if endpoint.endswith("/pulls/7"):
                return {"head": {"sha": "head"}}
            if "head_sha=head" in endpoint:
                return {"total_count": 1, "workflow_runs": [{
                    "id": 10, "workflow_id": 1, "status": "completed",
                    "conclusion": "success", "event": "pull_request", "head_sha": "head"
                }]}
            if "/actions/runs/10/jobs" in endpoint:
                return {"total_count": 1, "jobs": [timed_job("test", 120)]}
            if "/actions/workflows/1/runs" in endpoint:
                runs = [{"id": 100 + index, "workflow_id": 1, "status": "completed",
                         "conclusion": "success", "event": "push", "head_branch": "main"}
                        for index in range(10)]
                return {"total_count": len(runs), "workflow_runs": runs}
            if "/actions/runs/" in endpoint and "/jobs" in endpoint:
                return {"total_count": 1, "jobs": [timed_job("test", 100)]}
            raise AssertionError(endpoint)

    main, current, coverage = SHIFT.collect_history(
        "owner/repo", "main", pr_number=7, max_runs=20, client=Client())
    assert len([row for row in main if SHIFT._eligible(row)]) == 10
    assert len([row for row in current if SHIFT._eligible(row)]) == 1
    assert coverage["partial_workflows"] == {}
    assert SHIFT.analyze_l2(main, current)["jobs"][0]["baseline_runs"] == 10


def test_candidate_collection_keeps_only_latest_supported_run_per_workflow():
    class Client:
        calls = 0
        budget = 64

        def __init__(self):
            self.job_run_ids = []

        def get(self, endpoint):
            self.calls += 1
            if endpoint.endswith("/pulls/7"):
                return {"head": {"sha": "head"}}
            if "head_sha=head" in endpoint:
                assert "event=pull_request" in endpoint
                runs = [
                    {"id": 7, "workflow_id": 1, "status": "completed", "event": "workflow_dispatch", "head_sha": "head"},
                    {"id": 8, "workflow_id": 1, "status": "completed", "event": "push", "head_sha": "head"},
                    {"id": 9, "workflow_id": 1, "status": "completed", "event": "pull_request", "head_sha": "head", "created_at": "2026-01-01T00:00:00Z", "run_attempt": 1},
                    {"id": 10, "workflow_id": 1, "status": "completed", "event": "pull_request", "head_sha": "head", "created_at": "2026-01-02T00:00:00Z", "run_attempt": 1},
                    {"id": 11, "workflow_id": 1, "status": "completed", "event": "pull_request", "head_sha": "head", "created_at": "2026-01-02T00:00:00Z", "run_attempt": 2},
                    {"id": 20, "workflow_id": 2, "status": "completed", "event": "pull_request", "head_sha": "head", "created_at": "2026-01-01T00:00:00Z"},
                    {"id": 21, "workflow_id": 3, "status": "completed", "event": "pull_request", "head_sha": "other", "created_at": "2026-01-03T00:00:00Z"},
                ]
                return {"total_count": len(runs), "workflow_runs": runs}
            if "/actions/runs/" in endpoint and "/jobs" in endpoint:
                run_id = int(endpoint.split("/runs/")[1].split("/")[0])
                self.job_run_ids.append(run_id)
                return {"total_count": 1, "jobs": [{"name": "test", "status": "completed",
                    "conclusion": "failure", "labels": ["linux"], "steps": [{"name": "test"}]}]}
            raise AssertionError(endpoint)

    client = Client()
    _, current, _ = SHIFT.collect_history(
        "owner/repo", "main", pr_number=7, max_runs=20, client=client)
    assert client.job_run_ids == [11, 20]
    assert [row["run_id"] for row in current] == [11, 20]


def test_complete_empty_candidate_history_renders_no_candidate_data():
    class Client:
        calls = 0
        budget = 64

        def get(self, endpoint):
            self.calls += 1
            if endpoint.endswith("/pulls/7"):
                return {"head": {"sha": "head"}}
            if "head_sha=head" in endpoint:
                return {"total_count": 0, "workflow_runs": []}
            raise AssertionError(endpoint)

    main, current, coverage = SHIFT.collect_history(
        "owner/repo", "main", pr_number=7, max_runs=20, client=Client())
    report = SHIFT.analyze_l2(main, current)
    rendered = SHIFT.render_markdown({"status": "measured", "coverage": coverage,
                                      "l2": report})
    assert kinds(report) == ["no_candidate_data"]
    assert r"no\_candidate\_data" in rendered
    assert "Thresholds were not crossed" not in rendered


def test_complete_fifteen_to_nineteen_run_l3_history_is_valid_not_partial():
    class Client:
        calls = 0
        budget = 64

        def get(self, endpoint):
            self.calls += 1
            if "/actions/workflows?" in endpoint:
                return {"total_count": 1, "workflows": [
                    {"id": 1, "state": "active", "path": ".github/workflows/ci.yml"}]}
            if "/actions/workflows/1/runs" in endpoint:
                runs = [{"id": 100 + index, "workflow_id": 1, "status": "completed",
                         "conclusion": "success", "event": "push", "head_branch": "main"}
                        for index in range(16)]
                return {"total_count": len(runs), "workflow_runs": runs}
            if "/actions/runs/" in endpoint and "/jobs" in endpoint:
                return {"total_count": 1, "jobs": [timed_job("test", 100)]}
            raise AssertionError(endpoint)

    main, _, coverage = SHIFT.collect_history(
        "owner/repo", "main", pr_number=None, max_runs=20, client=Client())
    report = SHIFT.analyze_l3(main)
    assert coverage["partial_workflows"] == {}
    assert report["jobs"][0]["history_runs"] == 16
    assert report["jobs"][0]["evidence"] == "no_sustained_shift_established"


def timed_job(name, duration):
    return {"name": name, "status": "completed", "conclusion": "success",
            "labels": ["linux"], "steps": [{"name": "test"}],
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": f"2026-01-01T00:{duration // 60:02d}:{duration % 60:02d}Z"}


def test_pagination_cap_is_explicit_and_partial_invalidates_clean_evidence():
    class Client:
        def get(self, endpoint):
            return {"total_count": 300, "workflow_runs": [
                {"id": index} for index in range(100)
            ]}

    rows, partial = SHIFT._paginate(
        Client(), "repos/owner/repo/actions/runs?event=push", "workflow_runs", max_pages=2)
    assert len(rows) == 200 and partial is True
    report = {"jobs": [{"workflow": "ci.yml", "workflow_id": "1", "job": "test",
                        "evidence": "threshold_not_exceeded"}], "observations": []}
    SHIFT._apply_coverage(report, {"partial_workflows": {"1": "page cap"}})
    assert report["jobs"][0]["evidence"] == "partial"
    assert report["observations"][0]["kind"] == "partial"


def test_identity_scoped_partial_history_preserves_other_job_slowdowns():
    report = {"jobs": [
        {"workflow": "ci.yml", "workflow_id": "1", "job": "test",
         "runner_labels": ["linux"], "step_signature": ["test"],
         "evidence": "slowdown"},
        {"workflow": "ci.yml", "workflow_id": "1", "job": "new",
         "runner_labels": ["linux"], "step_signature": ["test"],
         "evidence": "new_job"},
    ], "observations": [
        {"kind": "slowdown", "workflow": "ci.yml", "job": "test"},
        {"kind": "new_job", "workflow": "ci.yml", "job": "new"},
    ]}
    coverage = {"partial_identities": [{
        "workflow_id": "1", "job": "new", "runner_labels": ["linux"],
        "step_signature": ["test"], "reason": "run list reached page cap"}]}
    SHIFT._apply_coverage(report, coverage)
    assert report["jobs"][0]["evidence"] == "slowdown"
    assert report["jobs"][1]["evidence"] == "partial"
    assert any(item["kind"] == "slowdown" for item in report["observations"])


def test_attribution_distinguishes_zero_one_and_multiple_merged_prs():
    class Client:
        def get(self, endpoint):
            sha = endpoint.split("/")[4]
            return {
                "zero": [],
                "one": [{"number": 7, "merged_at": "2026-01-01T00:00:00Z"}],
                "many": [
                    {"number": 8, "merged_at": "2026-01-01T00:00:00Z"},
                    {"number": 9, "merged_at": "2026-01-01T00:00:00Z"},
                ],
            }[sha]

    report = {"observations": [
        {"attribution": {"sha": "zero", "pr_number": None}},
        {"attribution": {"sha": "one", "pr_number": None}},
        {"attribution": {"sha": "many", "pr_number": None}},
    ]}
    SHIFT._fill_attribution("owner/repo", report, Client())
    attrs = [row["attribution"] for row in report["observations"]]
    assert [row["status"] for row in attrs] == ["unavailable", "unique", "ambiguous"]
    assert attrs[0]["candidates"] == []
    assert attrs[1]["pr_number"] == 7
    assert attrs[2]["candidates"] == [8, 9] and attrs[2]["pr_number"] is None


def test_attribution_failure_preserves_detection_evidence():
    class Client:
        def get(self, endpoint):
            raise SHIFT.CollectionError("budget exhausted")

    report = {"observations": [{"kind": "step_change", "change_percent": 20,
        "attribution": {"sha": "abc", "pr_number": None}}]}
    SHIFT._fill_attribution("owner/repo", report, Client())
    assert report["observations"][0]["kind"] == "step_change"
    assert report["observations"][0]["change_percent"] == 20
    assert report["observations"][0]["attribution"]["status"] == "partial"


def test_exact_rename_keeps_metadata_and_continues_timing_comparison():
    history = [record(100, job="old") for _ in range(8)]
    report = SHIFT.analyze_l2(history, [record(130, job="new")])
    assert kinds(report) == ["renamed_job", "slowdown"]
    assert report["jobs"][0]["renamed_from"] == "old"
    assert report["jobs"][0]["evidence"] == "slowdown"


def test_markdown_is_bounded_advisory_and_never_claims_no_slowdown():
    report = SHIFT.analyze_l3([])
    report["observations"].append({"kind": "step_change", "workflow": "ci.yml",
        "job": "test", "attribution": {"status": "unique", "pr_number": 42,
                                          "sha": "abcdef1234567890"}})
    result = {"status": "measured", "l3": report}
    rendered = SHIFT.render_markdown(result, max_bytes=1024)
    assert len(rendered.encode()) <= 1024
    assert "Advisory timing evidence" in rendered
    assert "no slowdown" not in rendered.lower()
    assert "PR #42" in rendered


def test_markdown_escapes_untrusted_html_and_markdown_names():
    hostile = "</strong><script>alert(1)</script>[click](https://evil) **owned**"
    report = {"observations": [{"kind": "new_job", "workflow": hostile,
                                 "job": hostile, "conclusion": "neutral"}]}
    rendered = SHIFT.render_markdown({"status": "measured", "l2": report})
    assert "<script>" not in rendered
    assert "</strong>" not in rendered
    assert "[click](https://evil)" not in rendered
    assert "**owned**" not in rendered
    assert "&lt;script&gt;" in rendered

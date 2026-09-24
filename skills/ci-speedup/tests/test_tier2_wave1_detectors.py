"""Tier-2 wave detectors. OPT46 (superseded runs), OPT47
(push+pull_request double-trigger), OPT36 (schedule burn), OPT35 (fail-fast),
OPT64 (rerun attempts), OPT65 (billing rounding waste), and OPT57 (timeout
default burn) all measure wasted compute or billable minutes from sampled run
history.
"""

from __future__ import annotations

import datetime as _dt
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import collect_runs as cr  # noqa: E402


# ========================= fixtures =========================

def _jobs_per_run(n_runs=5, secs=300.0):
    """n runs, each one `test` job of `secs` seconds (secs/60 min compute/run).
    n_runs is the timed-run count the per-run mean rests on (≥3 to clear the floor)."""
    start = "2026-06-01T00:00:00Z"
    end = f"2026-06-01T00:{int(secs // 60):02d}:00Z"
    return [[{"name": "test", "started_at": start, "completed_at": end}]
            for _ in range(n_runs)]


def _span_job(name, seconds, conclusion="success", run_id=None):
    start = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
    end = start + _dt.timedelta(seconds=seconds)
    out = {
        "name": name,
        "started_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if conclusion is not None:
        out["conclusion"] = conclusion
    if run_id is not None:
        out["_run_id"] = run_id
    return out


def _spine_job(name, seconds, run_created, labels=None):
    out = _span_job(name, seconds)
    out["labels"] = labels or ["ubuntu-latest"]
    out["_run_created_at"] = run_created
    return out


def _precise_span_job(name, seconds, conclusion="success", run_id=None):
    start = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
    end = start + _dt.timedelta(seconds=seconds)

    def fmt(ts):
        return ts.isoformat(timespec="microseconds").replace("+00:00", "Z")

    out = {"name": name, "started_at": fmt(start), "completed_at": fmt(end)}
    if conclusion is not None:
        out["conclusion"] = conclusion
    if run_id is not None:
        out["_run_id"] = run_id
    return out


def _run(branch, sha, event, start=None, end=None, run_id=None):
    r = {"head_branch": branch, "head_sha": sha, "event": event}
    if start:
        r["run_started_at"] = start
        r["created_at"] = start
    if end:
        r["updated_at"] = end
    if run_id is not None:
        r["id"] = run_id
    return r


def _overlapping(branch, n, event="push", base_sha="s"):
    """n runs that all RACE: each starts 1 min after the previous and runs 30 min,
    so every earlier run is still in flight when the next starts → n-1 superseded."""
    return [_run(branch, f"{base_sha}{i}", event,
                 f"2026-06-01T00:{i:02d}:00Z", f"2026-06-01T00:{i + 30:02d}:00Z",
                 run_id=f"{branch}-{i}")
            for i in range(n)]


def test_cost_deepen_candidates_rank_workflows_by_billable_minutes():
    spine = {"rows": [
        {"workflow_file": ".github/workflows/a.yml",
         "billable_equiv_min_per_month": 12.0},
        {"workflow_file": ".github/workflows/b.yml",
         "billable_equiv_min_per_month": 20.0},
        {"workflow_file": ".github/workflows/a.yml",
         "billable_equiv_min_per_month": 9.0},
        {"workflow_file": ".github/workflows/c.yml",
         "billable_equiv_min_per_month": 0.0},
        {"workflow_file": ".github/workflows/neg.yml",
         "billable_equiv_min_per_month": -1.0},
        {"workflow_file": ".github/workflows/d.yml",
         "billable_equiv_min_per_month": "not numeric"},
        {"workflow_file": ".github/workflows/e.yml",
         "billable_equiv_min_per_month": float("inf")},
        {"workflow_file": ".github/workflows/f.yml",
         "billable_equiv_min_per_month": "nan"},
    ]}

    assert cr._cost_deepen_candidates_from_spine(spine) == [
        ".github/workflows/a.yml",
        ".github/workflows/b.yml",
    ]


def test_cost_deepen_candidates_are_capped_and_tie_stable():
    spine = {"rows": [
        {"workflow_file": ".github/workflows/z.yml",
         "billable_equiv_min_per_month": 5.0},
        {"workflow_file": ".github/workflows/a.yml",
         "billable_equiv_min_per_month": 5.0},
        {"workflow_file": ".github/workflows/m.yml",
         "billable_equiv_min_per_month": 4.0},
    ]}

    assert cr._cost_deepen_candidates_from_spine(spine, cap=2) == [
        ".github/workflows/a.yml",
        ".github/workflows/z.yml",
    ]
    assert cr._cost_deepen_candidates_from_spine(spine, cap=0) == []
    assert cr._cost_deepen_candidates_from_spine({"rows": "bad"}, cap=2) == []


def test_finding_seed_workflow_paths_excludes_source_file_findings():
    workflow_paths = {".github/workflows/ci.yml", ".github/workflows/docs.yaml"}
    findings = [
        {"workflow_file": ".github/workflows/ci.yml"},
        {"workflow_file": ".github/workflows/missing.yml"},
        {"workflow_file": ".github/workflows/missing.yaml"},
        {"workflow_file": "tests/conftest.py"},
        {"workflow_file": "config/ci.yml"},
        {"workflow_file": ""},
        {"workflow_file": None},
        "malformed",
    ]

    assert cr._finding_seed_workflow_paths(findings, workflow_paths) == {
        ".github/workflows/ci.yml",
        ".github/workflows/missing.yml",
        ".github/workflows/missing.yaml",
    }
    assert cr._finding_seed_workflow_paths("malformed", workflow_paths) == set()


def test_runner_minute_spine_uses_billable_basis_and_occurrence_frequency():
    wf = ".github/workflows/ci.yml"
    runs = [
        [_spine_job("build", 61, "2026-06-01T00:00:00Z"),
         _spine_job("conditional", 10, "2026-06-01T00:00:00Z")],
        [_spine_job("build", 59, "2026-06-02T00:00:00Z")],
    ]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 100}, "private")

    assert spine is not None
    assert spine["schema_version"] == 1
    assert spine["source"] == "jobs_api_sampled_runs"
    assert spine["coverage_scope"] == "sampled_workflows_with_job_data"
    assert spine["complete_repo_coverage"] is False
    assert spine["render_ready"] is False
    assert spine["render_blocker"]
    assert spine["extrapolation_basis"] == (
        "sampled_job_occurrence_fraction_x_all_status_30d_workflow_volume")
    assert spine["attempt_coverage"] == "latest_and_prior"
    # Derived fact (PR-S2): the flag equals `prior_attempt_row_count > 0` — it
    # states what the SAMPLE contains, not what the pipeline is capable of.
    assert spine["prior_attempts_included"] is False
    assert spine["latest_attempt_row_count"] == 2
    assert spine["prior_attempt_row_count"] == 0
    assert spine["repo_visibility"] == "private"
    rows = {r["job_name"]: r for r in spine["rows"]}
    assert rows["build"]["event_scope"] == "all-events"
    assert rows["build"]["status_filter"] == "success"
    assert rows["build"]["attempt_filter"] == "latest"
    assert rows["build"]["volume_filter"] == "all-status"
    assert rows["build"]["sample_window_start"] == "2026-06-01T00:00:00Z"
    assert rows["build"]["sample_window_end"] == "2026-06-02T00:00:00Z"
    assert rows["build"]["sampled_workflow_run_count"] == 2
    assert rows["build"]["sampled_job_occurrence_count"] == 2
    assert rows["build"]["effective_monthly_job_volume"] == 100.0
    assert rows["build"]["raw_compute_runner_min_per_month"] == 100.0
    assert rows["build"]["billable_equiv_min_per_month"] == 150.0
    assert rows["conditional"]["sampled_job_occurrence_count"] == 1
    assert rows["conditional"]["occurrence_fraction"] == 0.5
    assert rows["conditional"]["effective_monthly_job_volume"] == 50.0
    assert rows["conditional"]["billable_equiv_min_per_month"] == 50.0
    assert spine["totals"]["billable_equiv_min_per_month"] == 200.0
    assert rows["build"]["share_of_all_row_total"] == 0.75
    assert rows["conditional"]["share_of_all_row_total"] == 0.25


def test_runner_minute_spine_keeps_runner_label_identity_for_starsling_rows():
    # A job on a StarSling runner renders a spine row whose runner_label is the
    # exact label; a co-present GitHub row stays byte-identical to the GitHub-only
    # path. Minutes only — no rate/sku/dollar surface.
    wf = ".github/workflows/ci.yml"
    runs = [
        [_spine_job("build", 61, "2026-06-01T00:00:00Z",
                    labels=["starsling-ubuntu-24.04-8"]),
         _spine_job("lint", 61, "2026-06-01T00:00:00Z", labels=["ubuntu-latest"])],
        [_spine_job("build", 59, "2026-06-02T00:00:00Z",
                    labels=["starsling-ubuntu-24.04-8"]),
         _spine_job("lint", 61, "2026-06-02T00:00:00Z", labels=["ubuntu-latest"])],
    ]
    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 100}, "public")
    assert spine is not None
    rows = {r["job_name"]: r for r in spine["rows"]}

    ss = rows["build"]
    assert ss["runner_label"] == "starsling-ubuntu-24.04-8"
    assert ss["billable_equiv_min_per_month"] == 150.0   # 61s->2, 59s->1; mean 1.5 x 100

    gh = rows["lint"]
    assert gh["runner_label"] == "ubuntu-latest"
    assert gh["billable_equiv_min_per_month"] == 200.0   # mean 2.0 x 100


def test_runner_minute_spine_marks_complete_when_workflows_in_play_have_rows():
    wf = ".github/workflows/ci.yml"
    runs = [[_spine_job("build", 60, "2026-06-01T00:00:00Z")]]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 10}, "private",
        workflows_in_play={wf})

    assert spine["coverage_scope"] == "sampled_workflows_in_play_with_job_data"
    assert spine["complete_repo_coverage"] is True
    assert spine["render_ready"] is True
    assert spine["render_blocker"] == ""
    assert spine["workflow_coverage"] == {
        "scope": "positive_30d_workflows_in_play",
        "workflow_count": 1,
        "row_workflow_count": 1,
        "omitted_workflows": [],
        "unknown_volume_workflows": [],
        "triaged_workflows_included": [],
        "job_fetch_failures": 0,
    }


def test_runner_minute_spine_keeps_coverage_open_for_omitted_workflow():
    wf = ".github/workflows/ci.yml"
    omitted = ".github/workflows/docs.yml"
    runs = [[_spine_job("build", 60, "2026-06-01T00:00:00Z")]]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 10, omitted: 5}, "private",
        workflows_in_play={wf, omitted})

    assert spine["coverage_scope"] == "sampled_workflows_in_play_with_job_data"
    assert spine["complete_repo_coverage"] is False
    assert spine["workflow_coverage"]["omitted_workflows"] == [omitted]
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == []
    assert spine["workflow_coverage"]["workflow_count"] == 2
    assert spine["workflow_coverage"]["row_workflow_count"] == 1


def test_runner_minute_spine_keeps_coverage_open_for_unknown_volume_workflow():
    wf = ".github/workflows/ci.yml"
    unknown = ".github/workflows/docs.yml"
    runs = [[_spine_job("build", 60, "2026-06-01T00:00:00Z")]]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 10, unknown: None}, "private",
        workflows_in_play={wf, unknown})

    assert spine["coverage_scope"] == "sampled_workflows_in_play_with_job_data"
    assert spine["complete_repo_coverage"] is False
    assert spine["workflow_coverage"]["workflow_count"] == 1
    assert spine["workflow_coverage"]["row_workflow_count"] == 1
    assert spine["workflow_coverage"]["omitted_workflows"] == []
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == [unknown]


def test_runner_minute_spine_treats_bool_volume_as_unknown():
    wf = ".github/workflows/ci.yml"
    bool_volume = ".github/workflows/docs.yml"
    runs = [[_spine_job("build", 60, "2026-06-01T00:00:00Z")]]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}, bool_volume: {"pull_request": runs}},
        {}, {wf: 10, bool_volume: True}, "private",
        workflows_in_play={wf, bool_volume})

    assert spine["coverage_scope"] == "sampled_workflows_in_play_with_job_data"
    assert spine["complete_repo_coverage"] is False
    assert spine["workflow_coverage"]["workflow_count"] == 1
    assert spine["workflow_coverage"]["row_workflow_count"] == 1
    assert spine["workflow_coverage"]["omitted_workflows"] == []
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == [bool_volume]
    assert {row["workflow_file"] for row in spine["rows"]} == {wf}


def test_runner_minute_spine_treats_negative_volume_as_unknown():
    wf = ".github/workflows/ci.yml"
    negative = ".github/workflows/negative.yml"
    runs = [[_spine_job("build", 60, "2026-06-01T00:00:00Z")]]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}, negative: {"pull_request": runs}},
        {}, {wf: 10, negative: -1}, "private",
        workflows_in_play={wf, negative})

    assert spine["coverage_scope"] == "sampled_workflows_in_play_with_job_data"
    assert spine["complete_repo_coverage"] is False
    assert spine["render_ready"] is False
    assert spine["workflow_coverage"]["workflow_count"] == 1
    assert spine["workflow_coverage"]["row_workflow_count"] == 1
    assert spine["workflow_coverage"]["omitted_workflows"] == []
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == [negative]
    assert {row["workflow_file"] for row in spine["rows"]} == {wf}


def test_runner_minute_spine_excludes_zero_volume_workflow_from_completion_denominator():
    wf = ".github/workflows/ci.yml"
    dormant = ".github/workflows/nightly.yml"
    runs = [[_spine_job("build", 60, "2026-06-01T00:00:00Z")]]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 10, dormant: 0}, "private",
        workflows_in_play={wf, dormant})

    assert spine["coverage_scope"] == "sampled_workflows_in_play_with_job_data"
    assert spine["complete_repo_coverage"] is True
    assert spine["render_ready"] is True
    assert spine["workflow_coverage"]["workflow_count"] == 1
    assert spine["workflow_coverage"]["row_workflow_count"] == 1
    assert spine["workflow_coverage"]["omitted_workflows"] == []
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == []


def test_runner_minute_spine_keeps_coverage_open_for_job_fetch_failure():
    wf = ".github/workflows/ci.yml"
    runs = [[_spine_job("build", 60, "2026-06-01T00:00:00Z")]]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 10}, "private",
        workflows_in_play={wf}, coverage_fetch_failures=1)

    assert spine["coverage_scope"] == "sampled_workflows_in_play_with_job_data"
    assert spine["complete_repo_coverage"] is False
    assert spine["workflow_coverage"]["omitted_workflows"] == []
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == []
    assert spine["workflow_coverage"]["workflow_count"] == 1
    assert spine["workflow_coverage"]["row_workflow_count"] == 1
    assert spine["workflow_coverage"]["job_fetch_failures"] == 1


def test_runner_minute_spine_keeps_unpriced_runner_identity_stable():
    wf = ".github/workflows/deploy.yml"
    runs = [[_spine_job("deploy", 60, "2026-06-01T00:00:00Z",
                        labels=["self-hosted", "linux"])]]

    spine = cr._build_runner_minute_spine(
        {wf: {"push": runs}}, {}, {wf: 10}, "private")

    row = spine["rows"][0]
    assert row["runner_label"] == "linux self-hosted"
    assert spine["totals"]["billable_equiv_min_per_month"] == 10.0


def test_runner_minute_spine_clamps_negative_job_spans_to_zero():
    wf = ".github/workflows/ci.yml"
    runs = [[_spine_job("pre-job", -5, "2026-06-01T00:00:00Z")]]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 10}, "private")

    row = spine["rows"][0]
    assert row["mean_sampled_compute_seconds"] == 0.0
    assert row["raw_compute_runner_min_per_month"] == 0.0
    assert row["billable_equiv_min_per_month"] == 0.0
    assert row["share_of_all_row_total"] == 0.0


def test_runner_minute_spine_excludes_skipped_jobs_from_compute_rows():
    wf = ".github/workflows/ci.yml"
    skipped = _spine_job("conditional", 30, "2026-06-01T00:00:00Z")
    skipped["conclusion"] = "skipped"
    runs = [
        [skipped],
        [_spine_job("conditional", 30, "2026-06-02T00:00:00Z")],
    ]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 100}, "private")

    row = spine["rows"][0]
    assert row["sampled_job_occurrence_count"] == 1
    assert row["occurrence_fraction"] == 0.5
    assert row["mean_sampled_compute_seconds"] == 30.0
    assert row["mean_sampled_billable_equiv_minutes"] == 1.0
    assert row["effective_monthly_job_volume"] == 50.0
    assert row["raw_compute_runner_min_per_month"] == 25.0
    assert row["billable_equiv_min_per_month"] == 50.0


def test_runner_minute_spine_keeps_coverage_block_when_all_jobs_skipped():
    wf = ".github/workflows/ci.yml"
    skipped = _spine_job("conditional", 30, "2026-06-01T00:00:00Z")
    skipped["conclusion"] = "skipped"

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": [[skipped]]}}, {}, {wf: 100}, "private",
        workflows_in_play={wf})

    assert spine is not None
    assert spine["rows"] == []
    assert spine["complete_repo_coverage"] is False
    assert spine["render_ready"] is False
    assert spine["render_blocker"]
    assert spine["latest_attempt_row_count"] == 0
    assert spine["prior_attempt_row_count"] == 0
    assert spine["workflow_coverage"]["workflow_count"] == 1
    assert spine["workflow_coverage"]["row_workflow_count"] == 0
    assert spine["workflow_coverage"]["omitted_workflows"] == [wf]
    assert spine["totals"]["row_count"] == 0
    assert spine["totals"]["billable_equiv_min_per_month"] == 0.0


def test_runner_minute_spine_derives_monthly_fields_from_stored_rounded_means():
    wf = ".github/workflows/ci.yml"
    runs = [
        [_spine_job("non-exact", 1, "2026-06-01T00:00:00Z")],
        [_spine_job("non-exact", 1, "2026-06-02T00:00:00Z")],
        [_spine_job("non-exact", 61, "2026-06-03T00:00:00Z")],
    ]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 100}, "private")

    row = spine["rows"][0]
    assert row["mean_sampled_compute_seconds"] == 21.0
    assert row["mean_sampled_billable_equiv_minutes"] == 1.333
    assert row["raw_compute_runner_min_per_month"] == 35.0
    assert row["billable_equiv_min_per_month"] == 133.3
    assert spine["totals"]["billable_equiv_min_per_month"] == 133.3


def test_runner_minute_spine_includes_prior_attempt_rows_with_all_status_denominator():
    wf = ".github/workflows/ci.yml"
    latest_runs = [
        [_spine_job("test", 60, "2026-06-01T00:00:00Z")],
        [_spine_job("test", 60, "2026-06-02T00:00:00Z")],
    ]
    prior_runs = [
        [_spine_job("test", 120, "2026-06-01T00:00:00Z")],
    ]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": latest_runs}}, {}, {wf: 100}, "private",
        prior_attempt_jobs_by_wf={
            wf: {
                "event_scope": "all-events",
                "sampled_workflow_run_count": 10,
                "runs": prior_runs,
            },
        })

    rows = {(r["job_name"], r["attempt_filter"]): r for r in spine["rows"]}
    latest = rows[("test", "latest")]
    prior = rows[("test", "prior")]
    assert latest["status_filter"] == "success"
    assert latest["sampled_workflow_run_count"] == 2
    assert latest["billable_equiv_min_per_month"] == 100.0
    assert prior["status_filter"] == "all-status"
    assert prior["sampled_workflow_run_count"] == 10
    assert prior["sampled_job_occurrence_count"] == 1
    assert prior["occurrence_fraction"] == 0.1
    assert prior["effective_monthly_job_volume"] == 10.0
    assert prior["raw_compute_runner_min_per_month"] == 20.0
    assert prior["billable_equiv_min_per_month"] == 20.0
    assert spine["latest_attempt_row_count"] == 1
    assert spine["prior_attempt_row_count"] == 1
    assert spine["prior_attempts_included"] is True  # derived: count > 0 (PR-S2)
    assert spine["totals"]["billable_equiv_min_per_month"] == 120.0
    assert latest["share_of_all_row_total"] == 0.833
    assert prior["share_of_all_row_total"] == 0.167


def test_runner_minute_spine_omits_rows_without_sample_window():
    wf = ".github/workflows/ci.yml"
    runs = [[_span_job("missing-window", 60)]]
    runs[0][0]["labels"] = ["ubuntu-latest"]

    spine = cr._build_runner_minute_spine(
        {wf: {"pull_request": runs}}, {}, {wf: 10}, "private")

    assert spine is None


def _sequential(branch, n, event="push"):
    """n runs that never overlap (each finishes before the next starts) → 0 superseded."""
    return [_run(branch, f"s{i}", event,
                 f"2026-06-01T{i:02d}:00:00Z", f"2026-06-01T{i:02d}:05:00Z",
                 run_id=f"{branch}-seq-{i}")
            for i in range(n)]


_PUSH_WF = {"on": {"push": {}, "pull_request": {}}, "name": "CI"}
_DT_WF = {"on": {"push": {}, "pull_request": {}}, "name": "CI"}
_SCHEDULE_WF = {"on": {"schedule": [{"cron": "*/5 * * * *"}]}, "name": "Cleanup"}
_FAIL_FAST_WF = {
    "on": {"pull_request": {}},
    "name": "CI",
    "jobs": {
        "test": {
            "strategy": {"fail-fast": False, "matrix": {"shard": [1, 2, 3]}},
            "steps": [{"run": "pnpm test --shard ${{ matrix.shard }}/3"}],
        }
    },
}
_TWO_FAIL_FAST_WF = {
    "on": {"pull_request": {}},
    "name": "CI",
    "jobs": {
        "unit": {
            "strategy": {"fail-fast": False, "matrix": {"shard": [1, 2]}},
            "steps": [{"run": "pnpm test:unit --shard ${{ matrix.shard }}/2"}],
        },
        "e2e": {
            "strategy": {"fail-fast": False, "matrix": {"shard": [1, 2]}},
            "steps": [{"run": "pnpm test:e2e --shard ${{ matrix.shard }}/2"}],
        },
    },
}
_DIAGNOSTIC_WF = {
    "on": {"pull_request": {}},
    "name": "CI",
    "jobs": {
        "test": {
            "strategy": {"fail-fast": False, "matrix": {"adapter": ["pg", "mysql"]}},
            "steps": [{"run": "pnpm test --filter ${{ matrix.adapter }}"}],
        }
    },
}


# ========================= helpers =========================

def _job(mins):
    return {"name": "a", "started_at": "2026-06-01T00:00:00Z",
            "completed_at": f"2026-06-01T00:{int(mins):02d}:00Z"}


def test_mean_run_compute_min_averages_job_seconds():
    assert cr._mean_run_compute_min([[_job(5)]]) == (5.0, 1)
    assert cr._mean_run_compute_min([]) == (0.0, 0)
    assert cr._mean_run_compute_min([[]]) == (0.0, 0)


def test_mean_run_compute_min_excludes_empty_runs_from_denominator():
    # An empty/failed-fetch run must NOT be averaged in as a 0-minute run —
    # (5+15)/2 = 10, NOT (5+0+15)/3 = 6.67 (the exact shape partial fetches produce).
    mean, n = cr._mean_run_compute_min([[_job(5)], [], [_job(15)]])
    assert mean == 10.0 and n == 2
    mean, n = cr._mean_run_compute_min([[_job(10)], [{"started_at": "nope", "completed_at": "x"}]])
    assert mean == 10.0 and n == 1


class _EndpointSpy:
    """Records the endpoints a fetcher actually asks for. Asserting on the REQUEST
    (rather than on the fetcher's source text, as these tests used to) keeps them
    honest across refactors of how the URL is built, and still fails loudly if the
    request itself ever changes shape."""

    def __init__(self, doc=None):
        self.seen = []
        self._doc = doc if doc is not None else {}
        self.queries = 0
        self.errors = 0

    def json(self, endpoint, allow_missing=False):
        self.seen.append(endpoint)
        return self._doc

    def text(self, endpoint, allow_missing=False):
        self.seen.append(endpoint)
        return ""

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        # Post-merge the run-list / paginated fetchers funnel through main's `_run_list`
        # and `_paginate`, which count their own malformed/short-page coverage gaps via
        # `client._bump(error=True)`. The default empty `{}` doc these endpoint-shape tests
        # return has no `workflow_runs`/`jobs` key, so that path fires — the spy must honour
        # the counter contract (the assertions read `last`, the recorded endpoint, not the
        # result, so a no-op tally is enough).
        self.queries += int(query)
        self.errors += int(error)

    @property
    def last(self):
        return self.seen[-1]


def test_all_status_endpoint_omits_status_filter():
    c = _EndpointSpy()
    cr._all_status_runs(c, "o/r", 7, 50, created_before=None)
    assert "status=success" not in c.last
    assert "per_page=50" in c.last
    assert c.last.startswith("repos/o/r/actions/workflows/7/runs?")


def test_monthly_event_volume_filters_by_event():
    c = _EndpointSpy()
    cr._monthly_event_volume(c, "o/r", 7, "schedule", created_before=None)
    assert "event=schedule" in c.last
    assert "created=" in c.last
    assert "per_page=1" in c.last


def test_schedule_run_fetches_are_event_scoped():
    c = _EndpointSpy()
    cr._sample_event_runs(c, "o/r", 7, "schedule", 20, created_before=None)
    assert "status=success" in c.last and "event=schedule" in c.last
    c = _EndpointSpy()
    cr._all_status_event_runs(c, "o/r", 7, "schedule", 50, created_before=None)
    assert "event=schedule" in c.last
    assert "status=success" not in c.last


def test_rerun_attempt_fetches_make_job_filters_explicit():
    assert cr._fetch_run_jobs_all_attempts.__defaults__ is None
    c = _EndpointSpy()
    cr._fetch_run_jobs_all_attempts(c, "o/r", 123)
    assert c.last == "repos/o/r/actions/runs/123/jobs?per_page=100&filter=all"
    cr._fetch_run_jobs_latest_attempt(c, "o/r", 123)
    assert c.last == "repos/o/r/actions/runs/123/jobs?per_page=100&filter=latest"
    # The UNfiltered fetcher must stay distinct from both (it maps to its own fixture).
    cr._fetch_run_jobs(c, "o/r", 123)
    assert c.last == "repos/o/r/actions/runs/123/jobs?per_page=100"


def test_superseded_remainder_count_parity_with_superseded_count():
    # PARITY GUARD (#89 review residual): `_superseded_remainder` duplicates
    # `_superseded_count`'s superseded-detection loop; a future drift would silently
    # split the rendered evidence COUNT from the CREDIT basis. Pin them equal across
    # the shapes that exercise every branch of the predicate: overlapping chains,
    # sequential (zero), single run, boundary start_j == end_i (strict-< => not
    # superseded), zero-duration runs, unsorted input, and missing timestamps.
    shapes = [
        _overlapping("f", 4), _sequential("f", 4), _overlapping("f", 1),
        # boundary: successor starts exactly when the first ends — NOT superseded
        [{"run_started_at": "2026-07-20T00:00:00Z", "updated_at": "2026-07-20T00:10:00Z"},
         {"run_started_at": "2026-07-20T00:10:00Z", "updated_at": "2026-07-20T00:20:00Z"}],
        # zero-duration superseder inside a longer run's span
        [{"run_started_at": "2026-07-20T00:00:00Z", "updated_at": "2026-07-20T00:30:00Z"},
         {"run_started_at": "2026-07-20T00:05:00Z", "updated_at": "2026-07-20T00:05:00Z"}],
        # unsorted input
        [{"run_started_at": "2026-07-20T00:20:00Z", "updated_at": "2026-07-20T00:40:00Z"},
         {"run_started_at": "2026-07-20T00:00:00Z", "updated_at": "2026-07-20T00:30:00Z"}],
        # one run missing timestamps among valid raced runs
        [{"run_started_at": "2026-07-20T00:00:00Z", "updated_at": "2026-07-20T00:30:00Z"},
         {"run_started_at": None, "updated_at": None},
         {"run_started_at": "2026-07-20T00:10:00Z", "updated_at": "2026-07-20T00:40:00Z"}],
    ]
    for runs in shapes:
        assert cr._superseded_remainder(runs).superseded_n == cr._superseded_count(runs), runs


def test_superseded_count_only_racing_runs():
    assert cr._superseded_count(_overlapping("f", 4)) == 3       # all but last raced
    assert cr._superseded_count(_sequential("f", 4)) == 0        # none overlapped
    assert cr._superseded_count(_overlapping("f", 1)) == 0       # a single run


def test_opt35_failed_workflow_runs_filters_fetchable_sample_to_failed_runs():
    runs = [
        _run("feature/x", "a", "pull_request", run_id="ok") | {"conclusion": "success"},
        _run("feature/x", "b", "pull_request", run_id="bad") | {"conclusion": "failure"},
        _run("feature/x", "c", "pull_request", run_id="slow") | {"conclusion": "timed_out"},
        _run("feature/x", "d", "pull_request", run_id="cancel") | {"conclusion": "cancelled"},
    ]
    assert [r["id"] for r in cr._opt35_failed_workflow_runs(runs)] == ["bad", "slow"]


def test_rerun_attempt_runs_filter_run_attempt_gt_one():
    runs = [
        _run("feature/x", "a", "pull_request", run_id="first") | {"run_attempt": 1},
        _run("feature/x", "b", "pull_request", run_id="retry") | {"run_attempt": 2},
        _run("feature/x", "c", "pull_request", run_id="missing"),
    ]
    assert [r["id"] for r in cr._rerun_attempt_runs(runs)] == ["retry"]


# ========================= OPT65 billing rounding waste =========================

def _round_job(name, seconds):
    minutes, secs = divmod(int(seconds), 60)
    return {
        "name": name,
        "started_at": "2026-06-01T00:00:00Z",
        "completed_at": f"2026-06-01T00:{minutes:02d}:{secs:02d}Z",
        "labels": ["ubuntu-latest"],
    }


def _rounding_run(*seconds):
    return [_round_job(f"tiny ({i + 1})", sec) for i, sec in enumerate(seconds)]


def _rounding_crit(*, floor=120.0, p50=None, runner="ubuntu-latest"):
    names = ["tiny (1)", "tiny (2)", "tiny (3)"]
    job_p50 = p50 or {name: 30.0 for name in names}
    return {
        "floor_p50": floor,
        "long_pole_p50": floor + 30.0,
        "job_p50": job_p50,
        "job_runner": {name: runner for name in job_p50},
        "runner_scope": runner,
    }


def _opt65(jpr, crit=None, monthly=10):
    return cr._detect_opt65_billing_rounding_waste(
        "ci.yml", jpr, crit or _rounding_crit(), monthly, 0)


def test_billing_rounding_waste_formula_is_exact():
    assert cr._billing_rounding_waste_min([20.0, 20.0, 20.0]) == 2
    assert cr._billing_rounding_waste_min([50.0, 50.0, 50.0]) == 0


def test_opt65_rounding_waste_promotes_measured_below_floor_matrix():
    out = _opt65([_rounding_run(20, 20, 20), _rounding_run(30, 10, 10)])
    assert len(out) == 1
    f = out[0]
    assert f["pattern"] == "OPT65"
    assert f["affected_jobs"] == ["tiny (1)", "tiny (2)", "tiny (3)"]
    assert f["runner_min_saving"] == 20.0  # 4 sampled waste min * 10/2
    assert f["wall_clock_p50_s"] == 0.0
    assert f["realization"] == "none"
    assert f["sizing_basis"] == "measured"
    assert "sum(ceil(job_seconds/60))-ceil(sum(job_seconds)/60)" in f["measured_signal"]
    assert f["tier2_neutrality"]["proof"] == "below_cluster_floor"
    assert f["tier2_neutrality"]["margin_s"] == 30.0
    assert f["rounding_waste"]["sampled_waste_min"] == 4
    assert f["rounding_waste"]["max_combined_leg_p50_s"] == 90.0
    assert "Do not merge or serialize" in f["guardrail"]


def test_opt65_rounding_waste_no_fire_when_waste_is_zero():
    assert _opt65([_rounding_run(50, 50, 50)]) == []


def test_opt65_rounding_waste_no_fire_when_any_matrix_leg_is_on_floor():
    crit = _rounding_crit(floor=30.0, p50={
        "tiny (1)": 20.0, "tiny (2)": 30.0, "tiny (3)": 20.0,
    })
    assert _opt65([_rounding_run(20, 20, 20)], crit=crit) == []


def test_opt65_rounding_waste_no_fire_when_combined_leg_p50_hits_floor():
    crit = _rounding_crit(floor=80.0)
    assert _opt65([_rounding_run(20, 20, 20)], crit=crit) == []


def test_opt65_rounding_waste_requires_monthly_volume():
    jpr = [_rounding_run(20, 20, 20)]
    assert _opt65(jpr, monthly=0) == []
    assert _opt65(jpr, monthly=None) == []


def test_opt65_rounding_waste_requires_same_known_sku():
    crit = _rounding_crit()
    crit["job_runner"]["tiny (3)"] = "windows-latest"
    assert _opt65([_rounding_run(20, 20, 20)], crit=crit) == []
    crit = _rounding_crit(runner="self-hosted")
    assert _opt65([_rounding_run(20, 20, 20)], crit=crit) == []


def test_opt65_rounding_waste_requires_each_sampled_occurrence_tiny_and_same_sku():
    assert _opt65([_rounding_run(20, 20, 70)]) == []
    run = _rounding_run(20, 20, 20)
    run[2]["labels"] = ["windows-latest"]
    assert _opt65([run]) == []


def test_opt65_rounding_waste_credits_only_the_tiny_matrix_legs():
    crit = _rounding_crit(p50={
        "tiny": 10.0,
        "tiny (1)": 30.0,
        "tiny (2)": 30.0,
        "tiny (3)": 30.0,
    })
    crit["job_runner"]["tiny"] = "windows-latest"
    f = _opt65([_rounding_run(20, 20, 20)], crit=crit)[0]
    assert f["affected_jobs"] == ["tiny (1)", "tiny (2)", "tiny (3)"]
    assert f["rounding_waste"]["runner_label"] == "ubuntu-latest"


def test_opt65_monthly_volume_matches_critical_path_event_scope():
    class Client:
        endpoint = ""

        def json(self, endpoint):
            self.endpoint = endpoint
            return {"total_count": 7}

    client = Client()
    out = cr._tier2_monthly_volume_for_scope(
        client, "owner/repo", 123, {"event_scope": "pull_request"}, 99,
        observed_events={"pull_request", "schedule"})
    assert out == 7
    assert "event=pull_request" in client.endpoint
    client.endpoint = ""
    assert cr._tier2_monthly_volume_for_scope(
        client, "owner/repo", 123, {"event_scope": "pull_request"}, 99,
        observed_events={"pull_request"}) == 99
    assert client.endpoint == ""
    assert cr._tier2_monthly_volume_for_scope(
        client, "owner/repo", 123, {"event_scope": "all-events"}, 99) == 99


# ========================= OPT36 schedule burn =========================

def _schedule_runs(*shas):
    return [_run("main", sha, "schedule",
                 f"2026-06-01T{i:02d}:00:00Z", f"2026-06-01T{i:02d}:05:00Z",
                 run_id=f"sched-{i}")
            for i, sha in enumerate(shas)]


def _opt36(wf, all_runs, monthly=30, jpr=None):
    return cr._detect_opt36_schedule_burn(
        "cleanup.yml", all_runs, jpr if jpr is not None else _jobs_per_run(), wf, monthly, 0)


def test_opt36_schedule_burn_promotes_measured_non_pr_event_cert():
    out = _opt36(_SCHEDULE_WF, _schedule_runs("a", "a", "b", "b", "b"))
    assert len(out) == 1
    f = out[0]
    assert f["pattern"] == "OPT36" and f["wall_clock_p50_s"] == 0.0
    assert f["sizing_basis"] == "measured"
    assert f["tier2_neutrality"]["proof"] == "non_pr_event"
    assert f["tier2_run_subset_events"] == ["schedule"]
    assert f["tier2_sample_run_ids"] == ["sched-1", "sched-3", "sched-4"]
    assert "event=schedule" in f["measured_signal"]
    assert "same-head_sha" in f["measured_signal"]
    assert "operational SLA" in f["measured_evidence"]["note"]


def test_opt36_schedule_burn_requires_consecutive_same_sha_schedule_runs():
    assert _opt36(_SCHEDULE_WF, _schedule_runs("a", "b", "a", "b")) == []
    assert _opt36(_SCHEDULE_WF, [_run("main", "a", "push"), *_schedule_runs("a")]) == []


def test_opt36_schedule_burn_floors():
    runs = _schedule_runs("a", "a", "a")
    assert _opt36({"on": {"push": {}}}, runs) == []
    assert _opt36(_SCHEDULE_WF, runs, monthly=0) == []
    assert _opt36(_SCHEDULE_WF, runs, monthly=None) == []
    assert _opt36(_SCHEDULE_WF, runs, jpr=_jobs_per_run(n_runs=2)) == []


# ========================= OPT35 fail-fast waste =========================

def _ff_job(name, start_min, end_min, conclusion="success", run_id="run-1"):
    return {
        "name": name,
        "started_at": f"2026-06-01T00:{start_min:02d}:00Z",
        "completed_at": f"2026-06-01T00:{end_min:02d}:00Z",
        "conclusion": conclusion,
        "_run_id": run_id,
    }


def _fail_fast_run(run_id="run-1"):
    return [
        _ff_job("test (1)", 0, 10, "failure", run_id),
        _ff_job("test (2)", 0, 25, "success", run_id),
        _ff_job("test (3)", 0, 8, "success", run_id),
    ]


def _opt35(wf, run_jobs, monthly=1):
    return cr._detect_opt35_fail_fast_waste("ci.yml", run_jobs, wf, monthly, 0)


def test_opt35_fail_fast_promotes_measured_post_completion_waste():
    out = _opt35(_FAIL_FAST_WF, [_fail_fast_run()])
    assert len(out) == 1
    f = out[0]
    assert f["pattern"] == "OPT35" and f["wall_clock_p50_s"] == 0.0
    assert f["runner_min_saving"] == 15.0
    assert f["sizing_basis"] == "measured"
    assert f["affected_jobs"] == ["test"]
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"
    assert "first failed shard" in f["tier2_neutrality"]["ref"]
    assert "fail-fast:false" in f["measured_signal"]
    assert "post-failure" in f["measured_signal"]
    assert f["tier2_sample_run_ids"] == ["run-1"]
    assert "diagnostic matrices" in f["measured_evidence"]["note"]


def test_opt35_fail_fast_extrapolates_observed_post_failure_minutes():
    out = _opt35(_FAIL_FAST_WF, [_fail_fast_run("r1"), _fail_fast_run("r2")], monthly=10)
    # Each sampled run wastes 15 minutes after first failure; 30 sampled minutes
    # over 2 sampled runs scales to 150 min/mo at monthly volume 10.
    assert out[0]["runner_min_saving"] == 150.0


def test_opt35_fail_fast_scales_by_all_status_sample_when_job_fetch_partial():
    out = cr._detect_opt35_fail_fast_waste(
        "ci.yml", [_fail_fast_run("r1")], _FAIL_FAST_WF, monthly_volume=100,
        start_idx=0, sample_denominator=100)
    assert out[0]["runner_min_saving"] == 15.0
    assert "100 sampled all-status run(s)" in out[0]["evidence"]


def test_opt35_fail_fast_emits_per_matrix_job_findings():
    out = _opt35(_TWO_FAIL_FAST_WF, [[
        _ff_job("unit (1)", 0, 10, "failure"),
        _ff_job("unit (2)", 0, 25, "success"),
        _ff_job("e2e (1)", 0, 6, "failure"),
        _ff_job("e2e (2)", 0, 16, "success"),
    ]])
    assert [f["affected_jobs"] for f in out] == [["unit"], ["e2e"]]
    assert [f["runner_min_saving"] for f in out] == [15.0, 10.0]
    assert [f["id"] for f in out] == ["f1", "f2"]


def test_opt35_fail_fast_floors_and_carveouts():
    assert _opt35(_DIAGNOSTIC_WF, [_fail_fast_run()]) == []
    assert _opt35({"jobs": {"test": {"strategy": {"matrix": {"shard": [1, 2]}}}}},
                  [_fail_fast_run()]) == []
    assert _opt35(_FAIL_FAST_WF, [[
        _ff_job("test (1)", 0, 10, "failure"),
        _ff_job("test (2)", 0, 9, "success"),
    ]]) == []
    assert _opt35(_FAIL_FAST_WF, [[
        _ff_job("test (1)", 0, 10, "success"),
        _ff_job("test (2)", 0, 25, "success"),
    ]]) == []
    assert _opt35(_FAIL_FAST_WF, [_fail_fast_run()], monthly=0) == []
    assert _opt35(_FAIL_FAST_WF, [_fail_fast_run()], monthly=None) == []


# ========================= OPT64 rerun attempt waste =========================

def _attempt_job(name, attempt, start_min, end_min, conclusion="success", job_id=None):
    return {
        "id": job_id or f"{name}-{attempt}-{start_min}-{end_min}",
        "name": name,
        "run_attempt": attempt,
        "started_at": f"2026-06-01T00:{start_min:02d}:00Z",
        "completed_at": f"2026-06-01T00:{end_min:02d}:00Z",
        "conclusion": conclusion,
    }


def _attempt_sample(latest_jobs=None, prior_extra=None, run_id="run-1"):
    run = _run("feature/retry", "abc", "pull_request", run_id=run_id) | {"run_attempt": 2}
    latest = latest_jobs if latest_jobs is not None else [
        _attempt_job("setup", 2, 0, 5, "success", "setup-2"),
        _attempt_job("test", 2, 0, 18, "success", "test-2"),
    ]
    prior = [
        _attempt_job("setup", 1, 0, 5, "success", "setup-1"),
        _attempt_job("test", 1, 0, 20, "failure", "test-1"),
    ]
    if prior_extra:
        prior.extend(prior_extra)
    return [(run, [*prior, *latest], latest)]


def _opt64(samples, monthly=1, denominator=1):
    return cr._detect_opt64_rerun_attempt_waste(
        "ci.yml", samples, monthly, denominator, 0)


def test_opt64_rerun_attempt_promotes_measured_post_completion_waste():
    out = _opt64(_attempt_sample())
    assert len(out) == 1
    f = out[0]
    assert f["pattern"] == "OPT64"
    assert f["wall_clock_p50_s"] == 0.0
    assert f["runner_min_saving"] == 25.0
    assert f["sizing_basis"] == "measured"
    assert f["affected_jobs"] == []
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"
    assert "tier2_sample_run_ids" not in f
    assert f["rerun_dominant_job"] == "test"
    assert "run_attempt>1" in f["measured_signal"]
    assert "filter=all" in f["measured_signal"]
    assert "filter=latest" in f["measured_signal"]
    assert "dominant failing job" in f["measured_signal"]
    assert "Prior attempt compute min" in f["measured_evidence"]["table"]["headers"]


def test_opt64_scales_by_all_status_sample_denominator():
    out = _opt64(_attempt_sample(), monthly=100, denominator=100)
    assert out[0]["runner_min_saving"] == 25.0
    assert "100 sampled all-status run(s)" in out[0]["evidence"]


def test_opt64_can_use_job_id_delta_when_job_attempt_missing():
    samples = _attempt_sample()
    run, all_jobs, latest = samples[0]
    for job in all_jobs:
        job.pop("run_attempt", None)
    out = _opt64([(run, all_jobs, latest)])
    assert out[0]["runner_min_saving"] == 25.0


def test_opt64_id_delta_fallback_skips_paginated_payload_without_attempt_fields():
    run = _run("feature/retry", "abc", "pull_request", run_id="run-big") | {"run_attempt": 2}
    latest = [
        _attempt_job(f"job-{i}", 2, 0, 1, "success", f"latest-{i}")
        for i in range(100)
    ]
    prior = [_attempt_job("test", 1, 0, 20, "failure", "test-1")]
    all_jobs = [*prior, *latest[:99]]
    for job in [*all_jobs, *latest]:
        job.pop("run_attempt", None)
    assert _opt64([(run, all_jobs, latest)]) == []


def test_opt64_withholds_on_a_full_first_page_even_when_run_attempt_is_present():
    """A TRUNCATED payload is UNKNOWN — the explicit `run_attempt` field does not make
    it decidable, it only makes the wrong answer look confident.

    This case used to assert the opposite (that the attempt fields could be trusted on a
    full page), which is precisely the bug: the truncation guard sat BELOW the
    explicit-attempt path, so on a real >100-job run — where every job carries
    `run_attempt` (verified on the recorded dbt-core run 29121623799, see
    `test_recorded_attempt_runs.py`) — the guard never ran. The prior set returned here
    is whatever happened to land on page 1: silently SHORT. OPT64 would then size re-run
    waste against a partial set and `_dominant_prior_failing_job` could crown a job that
    only wins because the real dominant one is among the jobs the page cut off.

    UNKNOWN (no finding) is the honest answer. The saving is not lost — the run pays for
    an explicit `filter=latest` fetch (`_attempt_job_samples`), and paginating the jobs
    fetchers restores the finding outright."""
    run = _run("feature/retry", "abc", "pull_request", run_id="run-big") | {"run_attempt": 2}
    prior = [_attempt_job("test", 1, 0, 20, "failure", "test-1")]
    latest = [
        _attempt_job("test", 2, 0, 1, "success", "test-2"),
        *[
            _attempt_job(f"job-{i}", 2, 0, 1, "success", f"latest-{i}")
            for i in range(98)
        ],
    ]
    all_jobs = [*prior, *latest]

    assert len(all_jobs) == cr._JOBS_PAGE_SIZE
    assert all(j.get("run_attempt") is not None for j in all_jobs), (
        "the trap this guards is a page where every job HAS run_attempt — a "
        "'missing basis' guard sails straight past it")
    assert cr._prior_attempt_jobs(run, all_jobs, latest) == [], (
        "a possibly-truncated filter=all payload yielded a CONFIDENT prior-attempt set; "
        "on a real big-matrix run that set is silently short and OPT64 sizes against it")
    assert _opt64([(run, all_jobs, latest)]) == []

    # ...and one job fewer — a page that is provably COMPLETE — is decided as before.
    short_all = all_jobs[:-1]
    assert cr._prior_attempt_jobs(run, short_all, latest[:-1]) == prior
    assert _opt64([(run, short_all, latest[:-1])])[0]["rerun_dominant_job"] == "test"


def test_a_paginated_complete_payload_is_not_mistaken_for_a_truncated_one():
    """The truncation verdict comes from the ENDPOINT's own `total_count` when the
    fetcher tagged the payload — not from its length.

    This is what keeps the guard from becoming a permanent OPT64 kill-switch on
    big-matrix repos once the jobs fetchers are paginated: a COMPLETE 150-job payload
    (`total_count == 150`) is decidable and OPT64 fires, while the same 100 jobs handed
    over as page 1 of 150 is UNKNOWN. Length alone cannot tell those two apart."""
    run = _run("feature/retry", "abc", "pull_request", run_id="run-big") | {"run_attempt": 2}
    prior = [_attempt_job("test", 1, 0, 20, "failure", "test-1")]
    latest = [_attempt_job("test", 2, 0, 1, "success", "test-2"),
              *[_attempt_job(f"job-{i}", 2, 0, 1, "success", f"latest-{i}")
                for i in range(148)]]
    jobs = [*prior, *latest]
    assert len(jobs) == 150 > cr._JOBS_PAGE_SIZE

    complete = cr._jobs_payload({"total_count": 150, "jobs": jobs})
    complete_latest = cr._jobs_payload({"total_count": 149, "jobs": latest})
    assert not complete.truncated and not complete_latest.truncated
    assert cr._prior_attempt_jobs(run, complete, complete_latest) == prior
    assert _opt64([(run, complete, complete_latest)])[0]["rerun_dominant_job"] == "test"

    # The same 100 jobs as PAGE ONE of 150: truncated, therefore UNKNOWN.
    page_one = cr._jobs_payload({"total_count": 150, "jobs": jobs[:100]})
    assert page_one.truncated
    assert cr._prior_attempt_jobs(run, page_one, complete_latest) == []
    assert _opt64([(run, page_one, complete_latest)]) == []


def test_latest_attempt_jobs_selects_the_right_attempt():
    """Shape coverage for the derivation on synthetic payloads. The LOAD-BEARING
    oracle — "the derived set equals what REST's `filter=latest` actually returns" —
    is NOT here and cannot be: any expectation written in this file re-states the
    implementation's own predicate and passes for every input, correct or not. It
    lives in `test_recorded_attempt_runs.py`, against recorded server payloads (a
    real 3-attempt PARTIAL re-run, and a truncated big-matrix run). These cases pin
    the attempt-selection shapes around it."""
    # A 2-attempt run: prior-attempt jobs must NOT leak into the latest set.
    run2, all_jobs, latest = _attempt_sample()[0]
    assert cr._latest_attempt_jobs(run2, all_jobs) == latest

    # A 3-attempt run: only attempt 3 is "latest"; attempts 1 AND 2 are prior.
    run3 = _run("feature/retry", "abc", "pull_request", run_id="run-3") | {"run_attempt": 3}
    a1 = [_attempt_job("test", 1, 0, 20, "failure", "test-1")]
    a2 = [_attempt_job("test", 2, 0, 19, "failure", "test-2")]
    a3 = [_attempt_job("setup", 3, 0, 5, "success", "setup-3"),
          _attempt_job("test", 3, 0, 18, "success", "test-3")]
    all3 = [*a1, *a2, *a3]
    assert cr._latest_attempt_jobs(run3, all3) == a3
    # ...and the prior-attempt subtraction (what OPT64 sizes) is unchanged by deriving.
    assert cr._prior_attempt_jobs(run3, all3, a3) == [*a1, *a2]

    # A 1-attempt run: every job is latest.
    run1 = _run("main", "abc", "push", run_id="run-1") | {"run_attempt": 1}
    only = [_attempt_job("test", 1, 0, 18, "success", "test-1")]
    assert cr._latest_attempt_jobs(run1, only) == only


def test_latest_attempt_jobs_is_unknown_on_a_full_first_page():
    """The truncation guard (the recorded-payload proof is in
    `test_recorded_attempt_runs.py`; this pins the boundary exactly).

    `filter=all` is fetched UNPAGINATED and ordered oldest-attempt-first, so a full
    page may hold nothing but prior-attempt jobs. At 99 jobs the payload is complete
    and decidable; at 100 it may be truncated and the honest answer is UNKNOWN — even
    though every job carries `run_attempt` and the missing-basis guard stays quiet."""
    run = _run("feature/retry", "abc", "pull_request", run_id="big") | {"run_attempt": 2}

    # 99 jobs, all from the prior attempt + one latest: complete page, derivable.
    under = [_attempt_job(f"j{i}", 1, 0, 5, "failure", f"p-{i}") for i in range(98)]
    under.append(_attempt_job("test", 2, 0, 9, "success", "l-1"))
    assert len(under) == 99
    assert cr._latest_attempt_jobs(run, under) == [under[-1]]

    # 100 jobs: FULL page -> may be truncated -> UNKNOWN, re-fetch.
    full = [*under, _attempt_job("test-b", 2, 0, 9, "success", "l-2")]
    assert len(full) == cr._JOBS_PAGE_SIZE
    assert cr._latest_attempt_jobs(run, full) is None

    # The nastiest shape, and the one that motivated the guard: a full page whose jobs
    # are ALL prior-attempt. The derivation would return [] — a confident, wrong "the
    # latest attempt ran nothing" that silently disables OPT64.
    all_prior = [_attempt_job(f"j{i}", 1, 0, 5, "failure", f"p-{i}") for i in range(100)]
    assert cr._latest_attempt_jobs(run, all_prior) is None


def test_latest_attempt_jobs_is_unknown_when_the_RUN_has_no_attempt():
    """The run-side basis is exactly as load-bearing as the job-side one: it is the
    KEY the selection matches on. Defaulting a missing `run_attempt` to 1 (which
    `_run_attempt` does, for callers that only ask "is this a re-run?") would select
    the attempt-1 jobs of a run whose real latest attempt is 3."""
    jobs = [_attempt_job("test", 1, 0, 20, "failure", "t-1"),
            _attempt_job("test", 2, 0, 18, "success", "t-2")]
    no_attempt = _run("f", "abc", "pull_request", run_id="r")
    no_attempt.pop("run_attempt", None)
    assert cr._run_attempt_opt(no_attempt) is None
    assert cr._latest_attempt_jobs(no_attempt, jobs) is None
    # ...while the defaulting reading stays available for the callers that want it.
    assert cr._run_attempt(no_attempt) == 1


def test_latest_attempt_jobs_never_returns_an_empty_set():
    """An attempt-run with zero latest-attempt jobs is not a thing GitHub produces —
    every attempt materializes the full job graph. So an empty derivation means the
    payload misled us (a truncated page), and the honest answer is UNKNOWN, which
    routes the run to the explicit fetch. Returning `[]` instead makes OPT64's
    dominant-failing-job gate structurally unreachable, and it reports CLEAN."""
    run = _run("f", "abc", "pull_request", run_id="r") | {"run_attempt": 3}
    only_prior = [_attempt_job("test", 1, 0, 20, "failure", "t-1"),
                  _attempt_job("test", 2, 0, 18, "failure", "t-2")]
    assert cr._latest_attempt_jobs(run, only_prior) is None


def test_latest_attempt_jobs_returns_none_when_the_basis_is_absent():
    """No `run_attempt` on a job = no basis. Deriving from a partial basis would
    drop real latest-attempt jobs (and over-credit the waste that subtracts from
    them), so the caller must be told to re-fetch instead."""
    run = _run("feature/retry", "abc", "pull_request", run_id="r") | {"run_attempt": 2}
    jobs = [_attempt_job("test", 2, 0, 18, "success", "test-2")]
    stripped = [{k: v for k, v in j.items() if k != "run_attempt"} for j in jobs]
    assert cr._latest_attempt_jobs(run, stripped) is None
    # Mixed payload (some jobs carry it, some don't) is also "no basis" — not a
    # partial derive.
    assert cr._latest_attempt_jobs(run, [*jobs, *stripped]) is None


def test_attempt_job_samples_uses_one_fetch_and_falls_back_when_undecidable():
    """The call-count contract: with `run_attempt` present, `_attempt_job_samples`
    issues ZERO extra fetches; without it, it falls back to REST `filter=latest`."""
    run, all_jobs, latest = _attempt_sample()[0]
    calls: list[object] = []

    def _fetch(_client, _repo, run_id):
        calls.append(run_id)
        return latest

    orig = cr._fetch_run_jobs_latest_attempt
    cr._fetch_run_jobs_latest_attempt = _fetch
    try:
        samples, failures = cr._attempt_job_samples(object(), "demo/repo", [(run, all_jobs)])
        assert samples == [(run, all_jobs, latest)] and failures == 0
        assert calls == []                      # derived — no second fetch

        stripped_all = [{k: v for k, v in j.items() if k != "run_attempt"}
                        for j in all_jobs]
        samples, failures = cr._attempt_job_samples(
            object(), "demo/repo", [(run, stripped_all)])
        assert samples == [(run, stripped_all, latest)] and failures == 0
        assert calls == [run["id"]]             # no basis — fell back to the API
    finally:
        cr._fetch_run_jobs_latest_attempt = orig


def test_attempt_job_samples_counts_a_failed_fallback_fetch_once():
    """The failure-accounting bug the two-fetch path had: an unreachable attempt-run
    was charged to the cost-spine coverage gap TWICE (once per fetch). One fetch =
    one charge; the run is dropped (unknown != no prior attempt)."""
    run, all_jobs, _latest = _attempt_sample()[0]
    stripped_all = [{k: v for k, v in j.items() if k != "run_attempt"} for j in all_jobs]
    orig = cr._fetch_run_jobs_latest_attempt
    cr._fetch_run_jobs_latest_attempt = lambda _c, _r, _rid: None   # fetch FAILED
    try:
        samples, failures = cr._attempt_job_samples(
            object(), "demo/repo", [(run, stripped_all)])
    finally:
        cr._fetch_run_jobs_latest_attempt = orig
    assert samples == []
    assert failures == 1


def test_job_fetch_memo_collapses_a_duplicate_run_id_fetch():
    """The memo's whole job: a run id this data pass ALREADY fetched is not fetched
    again (the OPT36 schedule probe re-samples the main pass's very runs). Same
    payload, no aliasing, and a DIFFERENT fetch flavour is not confused for a hit."""
    runs = [_run("main", "a", "schedule", run_id=7001),
            _run("main", "b", "schedule", run_id=7002)]
    fetched: list[int] = []

    def _fetch(_client, _repo, run_id):
        fetched.append(run_id)
        return [_attempt_job("test", 1, 0, 5, "success", f"j-{run_id}")]

    _fetch.__name__ = "_fetch_run_jobs"          # the default flavour
    memo = cr._JobFetchMemo()
    first, _ = cr._gather_run_jobs(object(), "demo/repo", runs, fetch=_fetch, memo=memo)
    second, _ = cr._gather_run_jobs(object(), "demo/repo", runs, fetch=_fetch, memo=memo)

    # Sorted: post-#215 the two first-pass fetches run CONCURRENTLY on the shared
    # warm pool, so the side-effect append order races (flaky under a full-suite
    # run, deterministic in isolation). The invariant is "each id fetched exactly
    # once and the second pass fetched NOTHING" — order is incidental.
    assert sorted(fetched) == [7001, 7002]
    assert [jobs for _run, jobs in second] == [jobs for _run, jobs in first]  # same value
    # ...but not the same objects: a caller stamping run context onto one sample must
    # not mutate the other's jobs.
    assert second[0][1][0] is not first[0][1][0]

    # A failed fetch is NOT memoized — the next caller re-tries it rather than
    # inheriting a transient gh error.
    fails: list[int] = []

    def _failing(_client, _repo, run_id):
        fails.append(run_id)
        return None

    _failing.__name__ = "_fetch_run_jobs_all_attempts"   # a DIFFERENT flavour
    for _ in range(2):
        kept, failures = cr._gather_run_jobs(
            object(), "demo/repo", runs[:1], fetch=_failing, memo=memo)
        assert kept == [] and failures == 1
    assert fails == [7001, 7001]


def test_success_sample_derives_from_the_all_status_page():
    """#3's basis: the success sample is the all-status page minus the non-successes,
    newest-first — no second query. A page that can't reach `max_runs` successes is
    NOT a valid substitute (it means the page doesn't reach far enough back)."""
    page = []
    for i in range(30):
        r = _run("main", f"s{i}", "push", run_id=i)
        r["status"] = "completed"
        r["conclusion"] = "success" if i % 2 == 0 else "failure"
        page.append(r)
    in_flight = _run("main", "live", "push", run_id=99)
    in_flight["status"] = "in_progress"
    in_flight["conclusion"] = None
    page.insert(0, in_flight)

    got = cr._success_runs_from_all_status(page, 5)
    assert [r["id"] for r in got] == [0, 2, 4, 6, 8]       # newest-first, successes only
    assert len(cr._success_runs_from_all_status(page, 20)) == 15   # short — caller falls back
    assert cr._success_runs_from_all_status([], 20) == []


# The fallback's WIRING into `collect` used to be "guarded" here by an
# `inspect.getsource` string grep. That is not a test: it passes if the fallback is
# dead code, breaks on any reformat, and cannot catch an off-by-one in the condition.
# It is replaced by two behavioral tests that drive `collect()` and observe which gh
# endpoints it actually calls — see
# `test_offline_pipeline_e2e.test_collect_issues_the_success_query_ONLY_for_a_truncated_run_page`
# (when the fallback fires) and `..._derived_success_sample_equals_the_recorded_success_payload`
# (that the derived sample IS what the server's `status=success` filter returns).


def test_opt64_floors_and_ambiguity_guards():
    assert cr._rerun_attempt_runs([_run("f", "a", "pull_request") | {"run_attempt": 1}]) == []
    assert _opt64(_attempt_sample(), monthly=0) == []
    assert _opt64(_attempt_sample(), monthly=None) == []
    assert _opt64(_attempt_sample(), denominator=0) == []

    # Two equal-duration failing jobs: no unique dominant retry cause.
    ambiguous = _attempt_sample(
        latest_jobs=[
            _attempt_job("test", 2, 0, 10, "success", "test-2"),
            _attempt_job("lint", 2, 0, 10, "success", "lint-2"),
        ],
        prior_extra=[_attempt_job("lint", 1, 0, 20, "failure", "lint-1")],
    )
    assert _opt64(ambiguous) == []

    # The prior failing job disappeared from the latest attempt: not the same
    # repeated job, so don't charge a workflow-level retry finding.
    missing_latest = _attempt_sample(
        latest_jobs=[_attempt_job("setup", 2, 0, 5, "success", "setup-2")])
    assert _opt64(missing_latest) == []


def test_opt64_requires_same_dominant_failure_across_prior_attempts():
    run = _run("feature/retry", "abc", "pull_request", run_id="run-3") | {"run_attempt": 3}
    latest = [
        _attempt_job("test", 3, 0, 10, "success", "test-3"),
        _attempt_job("lint", 3, 0, 5, "success", "lint-3"),
    ]
    mixed_prior = [
        _attempt_job("test", 1, 0, 20, "failure", "test-1"),
        _attempt_job("lint", 2, 0, 10, "failure", "lint-2"),
    ]
    assert _opt64([(run, [*mixed_prior, *latest], latest)]) == []

    same_prior = [
        _attempt_job("test", 1, 0, 20, "failure", "test-1"),
        _attempt_job("setup", 1, 0, 5, "success", "setup-1"),
        _attempt_job("test", 2, 0, 10, "failure", "test-2"),
        _attempt_job("setup", 2, 0, 4, "success", "setup-2"),
    ]
    out = _opt64([(run, [*same_prior, *latest], latest)])
    assert out[0]["rerun_dominant_job"] == "test"
    assert out[0]["runner_min_saving"] == 39.0


def test_opt64_keeps_matrix_legs_distinct_for_rerun_cause():
    run = _run("feature/retry", "abc", "pull_request", run_id="run-matrix") | {"run_attempt": 3}
    latest = [
        _attempt_job("test (1)", 3, 0, 8, "success", "test-1-3"),
        _attempt_job("test (2)", 3, 0, 8, "success", "test-2-3"),
    ]
    prior = [
        _attempt_job("test (1)", 1, 0, 20, "failure", "test-1-1"),
        _attempt_job("test (2)", 2, 0, 10, "failure", "test-2-2"),
    ]
    assert _opt64([(run, [*prior, *latest], latest)]) == []


def test_opt64_output_composes_with_pr1_stamps():
    f = _opt64(_attempt_sample())[0]
    crit = {"job_runner": {}, "job_p50": {}, "floor_p50": 0.0,
            "runner_scope": "ubuntu-latest"}
    cr._stamp_sizing_basis(f)
    cr._stamp_tier2_neutrality(f, crit)
    assert f["sizing_basis"] == "measured"
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"


# ========================= OPT46 superseded =========================

def _opt46(wf, all_runs, jpr=None, monthly=42):
    return cr._detect_opt46_superseded_runs(
        "ci.yml", all_runs, jpr if jpr is not None else _jobs_per_run(), wf, monthly, 0)


def test_opt46_fires_only_on_overlapping_runs_with_cert():
    out = _opt46(_PUSH_WF, _overlapping("feature/x", 5) + _sequential("main", 1))
    assert len(out) == 1
    f = out[0]
    assert f["pattern"] == "OPT46" and f["wall_clock_p50_s"] == 0.0
    assert f["runner_min_saving"] > 0
    assert "remainder" in f["measured_signal"] and "superseded" in f["measured_signal"]
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"
    assert f["tier2_sample_run_ids"] == [
        "feature/x-0", "feature/x-1", "feature/x-2", "feature/x-3"]
    assert "inference" in f["evidence"].lower()
    assert f["runner_min_range_s"][0] <= f["runner_min_range_s"][1]


def test_opt46_does_NOT_fire_on_sequential_default_branch_runs():
    # THE core adversarial fix: a busy main with sequential (non-racing) commits
    # must NOT be charged as superseded — cancel-in-progress would cancel nothing.
    assert _opt46(_PUSH_WF, _sequential("main", 40)) == []


def test_opt46_skips_top_level_and_job_level_cancelling_concurrency():
    runs = _overlapping("feature/x", 5)
    top = {"on": {"push": {}}, "concurrency": {"group": "g", "cancel-in-progress": True}}
    job = {"on": {"push": {}}, "jobs": {"t": {"concurrency": {"cancel-in-progress": True}}}}
    yes = {"on": {"push": {}}, "concurrency": {"cancel-in-progress": "yes"}}
    assert _opt46(top, runs) == []
    assert _opt46(job, runs) == []      # job-level cancel — was a false positive
    assert _opt46(yes, runs) == []      # quoted truthy


def test_opt46_release_like_carve_out_is_broad():
    runs = _overlapping("feature/x", 5)
    for name in ("Ship", "cut-tag", "gh-pages", "docker-push", "CD", "Deploy to prod"):
        assert _opt46({"on": {"push": {}}, "name": name}, runs) == [], name


def test_opt46_prompt_guardrail_never_prescribes_bare_cancel_in_progress():
    """The GUARDRAIL sentence of OPT46's measured-evidence note is lifted VERBATIM
    into the per-finding coding-agent prompt (blocking_path `_tier2_guardrail_sentence`),
    where it sits ABOVE the catalog link and outranks it. It must therefore never name
    a bare `cancel-in-progress: true` as the thing to add — the catalog's recipe scopes
    the cancellation with an expression, and a bare `true` kills in-flight runs on the
    default branch and on release tags. The only admissible mention is a negation."""
    f = _opt46(_PUSH_WF, _overlapping("feature/x", 5))[0]
    note = f["measured_evidence"]["note"]
    guardrail = note[note.index("GUARDRAIL"):]
    assert "never a bare `cancel-in-progress: true`" in guardrail
    assert "catalog recipe" in guardrail
    for token in ("cancel-in-progress: true", "cancel-in-progress:true"):
        idx = 0
        while (idx := guardrail.find(token, idx)) >= 0:
            window = guardrail[max(0, idx - 20):idx]
            assert "bare" in window, (
                f"OPT46's agent prompt prescribes `{token}` — unsafe on the push/main/tag "
                f"runs this pattern fires on. Point at the catalog's expression recipe "
                f"instead. Context: ...{window}{token}...")
            idx += len(token)


def test_opt46_prompt_names_the_trigger_set_so_routing_is_mechanical():
    """F5: the catalog offers TWO predicates — the default (PR-scoped) one and the
    widened one — and the choice is decided entirely by whether the workflow has a
    `pull_request` trigger (without one, the PR-scoped predicate is never true and
    saves nothing). OPT45's evidence line names its trigger set; OPT46's must too,
    or the agent has to open the workflow to route. The trigger set has to sit in
    the note's GUARDRAIL tail — that tail is the only part `blocking_path`
    `_tier2_guardrail_sentence` lifts into the per-finding prompt."""
    def tail(wf):
        note = _opt46(wf, _overlapping("feature/x", 5))[0]["measured_evidence"]["note"]
        return note[note.index("GUARDRAIL"):]

    both = tail({"on": {"push": {}, "pull_request": {}}, "name": "CI"})
    assert "ROUTING:" in both
    assert "triggers on `pull_request`/`push`" in both

    push_only = tail({"on": {"push": {}}, "name": "CI"})
    assert "triggers on `push`" in push_only
    assert "`pull_request`" not in push_only.split("triggers on", 1)[1].split("—")[0]
    assert "WIDENED" in push_only and "DEFAULT" in push_only


def test_opt46_no_trigger_no_fire():
    assert _opt46({"on": {"schedule": [{"cron": "0 0 * * *"}]}}, _overlapping("f", 5)) == []


def test_opt46_no_multi_run_branch_no_fire():
    assert _opt46(_PUSH_WF, _overlapping("a", 1) + _overlapping("b", 1, base_sha="b")) == []


def test_opt46_no_measured_compute_no_fire():
    assert _opt46(_PUSH_WF, _overlapping("feature/x", 5), jpr=[[]]) == []


def test_opt46_requires_min_timed_runs():
    # A mean resting on <3 timed runs is outlier-fragile → withheld.
    assert _opt46(_PUSH_WF, _overlapping("feature/x", 5), jpr=_jobs_per_run(n_runs=2)) == []
    assert len(_opt46(_PUSH_WF, _overlapping("feature/x", 5), jpr=_jobs_per_run(n_runs=3))) == 1


def test_opt46_dormant_or_unknown_volume_no_fire():
    # monthly_volume 0 (dormant) or None (API gap) can't yield an honest /mo figure.
    assert _opt46(_PUSH_WF, _overlapping("feature/x", 5), monthly=0) == []
    assert _opt46(_PUSH_WF, _overlapping("feature/x", 5), monthly=None) == []


def test_opt46_extrapolates_both_directions():
    runs = _overlapping("feature/x", 5)  # sampled_n = 5, superseded = 4
    up = _opt46(_PUSH_WF, runs, monthly=50)      # scale 10
    same = _opt46(_PUSH_WF, runs, monthly=5)     # scale 1
    down = _opt46(_PUSH_WF, runs, monthly=2)     # scale 0.4 (low-frequency, spans >30d)
    base = same[0]["runner_min_saving"]
    # `scale` multiplies `remainder_units × per_run_min` (both computed on the
    # sampled window, BEFORE scaling — issue #89 §4), then the product rounds to
    # 0.1 at EACH scale. The old integer overlap count made round(x·k,1)==round(x,1)·k
    # hold by coincidence; the fractional remainder_units (3.8667 here, not 4) breaks
    # it, so assert proportionality WITHIN the per-scale rounding budget (|Δ| ≤ 0.05·k
    # + 0.05) instead of exact k× equality — the invariant is that `scale` is applied
    # multiplicatively, not that rounding commutes with it.
    assert abs(up[0]["runner_min_saving"] - base * 10) <= 0.6
    assert abs(down[0]["runner_min_saving"] - base * 0.4) <= 0.1


def _raced_pair(start_b, branch="f", end_a="00:30:00"):
    """A superseded pair on ONE branch: run A [00:00:00 .. end_a], run B starts at
    `start_b` (< end_a, so it supersedes A) and runs 30 min. The later B starts, the
    LESS of A a cancel reclaims — that remainder is what OPT46 must now credit (#89)."""
    return [_run(branch, "a", "push", "2026-06-01T00:00:00Z", f"2026-06-01T{end_a}Z",
                 run_id=f"{branch}-0"),
            _run(branch, "b", "push", f"2026-06-01T{start_b}Z",
                 "2026-06-01T00:59:30Z", run_id=f"{branch}-1")]


def test_opt46_late_supersession_credits_far_below_whole_run():
    """RED on main, GREEN here (issue #89). A run superseded 30s before its natural
    finish is reclaimable for only that 30s — its remainder fraction is ~1.7% of its
    1800s run. The whole-run pricing main ships would charge it its FULL mean compute
    (credited == the naive upper bound when confirmed==naive==1); the remainder basis
    credits <5% of that. This assertion FAILS against the whole-run figure."""
    runs = _raced_pair("00:29:30")           # B starts 30s before A ends
    f = _opt46(_PUSH_WF, runs, monthly=42)[0]
    lower, upper = f["runner_min_range_s"]
    assert f["runner_min_saving"] == lower
    assert lower <= upper                     # range invariant, explicit
    # remainder ≈ 30s / 1800s → credited is a sliver of the whole-run upper bound;
    # main (whole-run) credits lower == upper here, so this goes red on main.
    assert lower < upper * 0.05
    assert 0.0 <= f["superseded_remainder_ratio"] <= 0.05


def test_opt46_early_supersession_matches_whole_run_figure():
    """The complement: a run superseded right after it starts has almost all of its
    compute still ahead of it, so its remainder ≈ the whole run and the credited figure
    ≈ the old whole-run figure (within rounding). Proves the remainder basis only
    DISCOUNTS late supersession, never silently shrinks an honest early one (#89)."""
    runs = _raced_pair("00:00:30")           # B starts 30s after A starts
    f = _opt46(_PUSH_WF, runs, monthly=42)[0]
    lower, upper = f["runner_min_range_s"]
    assert lower <= upper
    assert lower >= upper * 0.95              # ≈ the whole-run figure
    assert f["superseded_remainder_ratio"] >= 0.95


def test_opt46_missing_timestamp_run_is_disclosed_not_crashed():
    """A run missing a usable timestamp contributes to NEITHER the count nor the
    remainder — but it is COUNTED as a disclosed skip (never a crash, never silently
    folded in), stamped on the finding and named in the evidence note (#89 §1)."""
    runs = [
        _run("f", "a", "push", "2026-06-01T00:00:00Z", "2026-06-01T00:30:00Z", run_id="f-0"),
        _run("f", "b", "push", "2026-06-01T00:01:00Z", "2026-06-01T00:31:00Z", run_id="f-1"),
        # no `updated_at` → no usable span
        {"head_branch": "f", "head_sha": "c", "event": "push",
         "run_started_at": "2026-06-01T00:02:00Z", "created_at": "2026-06-01T00:02:00Z",
         "id": "f-2"},
    ]
    out = _opt46(_PUSH_WF, runs, monthly=42)
    assert len(out) == 1                       # still fires on the valid overlap
    f = out[0]
    assert f["superseded_skipped_missing_ts"] == 1
    assert "lacked usable timestamps" in f["measured_evidence"]["note"]


def test_opt46_remainder_ratio_stays_within_unit_interval():
    """The clamp's OBSERVABLE guarantee: no superseded run can ever credit more than
    its own whole compute, so the mean remainder fraction is always in [0, 1] and the
    effective (remainder) count never exceeds the honest overlap count (#89 §1). The
    clamp itself is defensive — sorted spans make cancel_at ∈ (start, end], so a
    remainder > duration is unreachable by construction — but this pins the invariant
    it protects across the late/early/multi fixtures."""
    for runs in (_raced_pair("00:29:30"), _raced_pair("00:00:30"),
                 _overlapping("feature/x", 5)):
        f = _opt46(_PUSH_WF, runs, monthly=42)[0]
        assert 0.0 <= f["superseded_remainder_ratio"] <= 1.0
        # effective (remainder-weighted) count ≤ honest overlap count
        confirmed = int(f["measured_evidence"]["table"]["rows"][0][1].split()[0])
        assert f["superseded_remainder_units"] <= confirmed + 1e-9


def test_opt46_stamped_remainder_reproduces_credited_single_door():
    """§3 single-door discipline: the credited figure and any downstream re-derivation
    must come from ONE stamped basis. Pin the identity
    `credited == round(remainder_units × per_run_min × scale, 1)` and the ratio↔units
    relation, so the two surfaces can never split back to whole-run pricing (#89)."""
    runs = _overlapping("feature/x", 5)        # 4 superseded; per-run 5.0 min; scale 1 @ monthly=5
    f = _opt46(_PUSH_WF, runs, monthly=5)[0]
    per_run_min, scale = 5.0, 1.0
    units = f["superseded_remainder_units"]
    assert f["runner_min_saving"] == round(units * per_run_min * scale, 1)
    assert f["runner_min_saving"] == f["runner_min_range_s"][0]
    # ratio == units / confirmed overlap count, in [0, 1]
    assert abs(f["superseded_remainder_ratio"] - units / 4) < 1e-4
    # the credited remainder is strictly below the whole-run upper bound (ratio < 1)
    assert f["runner_min_saving"] < f["runner_min_range_s"][1]


def test_opt46_credited_never_exceeds_total_workflow_compute():
    runs = _overlapping("feature/x", 5)
    monthly, per_run_min = 500, 5.0
    out = _opt46(_PUSH_WF, runs, monthly=monthly)
    assert out[0]["runner_min_saving"] <= monthly * per_run_min


def test_opt46_evidence_discloses_overlap_basis_and_multiplier():
    out = _opt46(_PUSH_WF, _overlapping("feature/x", 5), monthly=50)
    ev, note = out[0]["evidence"], out[0]["measured_evidence"]["note"]
    assert "superseded" in ev and "×" in ev and "timed run" in ev
    assert "different populations" in note and "deploy/release/publish" in note


# ========================= OPT47 double-trigger =========================

def _dup_runs(sha="abc", push_branch="feature/y"):
    return [_run(push_branch, sha, "push"), _run(push_branch, sha, "pull_request"),
            _run(push_branch, "def", "push")]


def _opt47(wf, all_runs, default="main", monthly=42):
    return cr._detect_opt47_double_trigger(
        "ci.yml", all_runs, _jobs_per_run(), wf, monthly, default, 0)


def test_opt47_fires_on_nondefault_branch_dup_without_cert():
    out = _opt47(_DT_WF, _dup_runs(push_branch="feature/y"))
    assert len(out) == 1
    f = out[0]
    assert f["pattern"] == "OPT47" and f["wall_clock_p50_s"] == 0.0
    assert f["runner_min_saving"] > 0
    assert "tier2_neutrality" not in f
    assert "side effect" in f["measured_evidence"]["note"].lower()


def test_opt47_excludes_default_branch_push_post_merge():
    # THE core adversarial fix: a PR sha that reappears as a DEFAULT-branch push
    # (rebase/FF merge validation) is NOT a redundant double-trigger — the fix
    # keeps that push, so it must not be counted.
    runs = [_run("main", "abc", "push"), _run("feature/y", "abc", "pull_request")]
    assert _opt47(_DT_WF, runs) == []


def test_opt47_list_form_triggers():
    assert len(_opt47({"on": ["push", "pull_request"]}, _dup_runs())) == 1


def test_opt47_push_scoped_to_default_no_fire():
    wf = {"on": {"push": {"branches": ["main"]}, "pull_request": {}}}
    assert _opt47(wf, _dup_runs()) == []


def test_opt47_release_like_carve_out():
    # A deploy workflow's per-commit push may have a side effect; carve it out.
    assert _opt47({"on": {"push": {}, "pull_request": {}}, "name": "Deploy preview"}, _dup_runs()) == []


def test_opt47_tags_only_push_no_fire():
    wf = {"on": {"push": {"tags": ["v*"]}, "pull_request": {}}}
    assert _opt47(wf, _dup_runs()) == []


def test_opt47_no_duplicate_sha_no_fire():
    assert _opt47(_DT_WF, [_run("f", "a", "push"), _run("f", "b", "pull_request")]) == []


def test_opt47_push_only_no_fire():
    assert _opt47({"on": {"push": {}}}, _dup_runs()) == []


def test_opt47_floors():
    assert _opt47(_DT_WF, _dup_runs(), monthly=0) == []
    assert cr._detect_opt47_double_trigger(
        "ci.yml", _dup_runs(), _jobs_per_run(n_runs=2), _DT_WF, 42, "main", 0) == []


def test_push_double_trigger_predicate_edge_cases():
    dt = cr._push_double_triggers_with_pr
    assert dt({"push": {}, "pull_request": {}}, "main") is True
    assert dt(["push", "pull_request"], "main") is True
    assert dt({"push": {"branches": ["main"]}, "pull_request": {}}, "main") is False
    assert dt({"push": {"branches": ["master"]}, "pull_request": {}}, "main") is False
    assert dt({"push": {"branches": ["main", "dev"]}, "pull_request": {}}, "main") is True
    assert dt({"push": {"branches-ignore": ["docs/**"]}, "pull_request": {}}, "main") is True
    assert dt({"push": {"branches": "main"}, "pull_request": {}}, "main") is False
    assert dt({"push": {"branches": "release/*"}, "pull_request": {}}, "main") is True
    assert dt({"push": "x", "pull_request": {}}, "main") is True
    assert dt({"push": {"tags": ["v*"]}, "pull_request": {}}, "main") is False  # tags-only
    assert dt({"push": {"branches": ["main"]}, "pull_request": {}}, None) is False
    assert dt({"push": {}}, "main") is False
    assert dt({"pull_request": {}}, "main") is False


# ========================= invariants =========================

def test_run_elimination_findings_are_bill_only_wall_clock_zero():
    a35 = _opt35(_FAIL_FAST_WF, [_fail_fast_run()])
    a46 = _opt46(_PUSH_WF, _overlapping("feature/x", 5))
    a47 = _opt47(_DT_WF, _dup_runs())
    a36 = _opt36(_SCHEDULE_WF, _schedule_runs("a", "a", "b"))
    a64 = _opt64(_attempt_sample())
    assert a35 and a36 and a46 and a47 and a64
    for f in a35 + a36 + a46 + a47 + a64:
        assert f["wall_clock_p50_s"] == 0.0
        assert f["realization"] == "none"
        if f["pattern"] != "OPT35":
            assert f["affected_jobs"] == []


def test_reconcile_still_vacuous_with_opt46_only_certified():
    findings = [
        {"pattern": "OPT46", "sizing_basis": "measured",
         "tier2_neutrality": {"proof": "post_completion_waste"}, "runner_min_saving": 83.0},
        {"pattern": "OPT47", "sizing_basis": "measured", "runner_min_saving": 40.0},
    ]
    before = [f["runner_min_saving"] for f in findings]
    cr._reconcile_tier2_overlap(findings)
    assert [f["runner_min_saving"] for f in findings] == before


def test_detectors_wired_into_collect():
    import inspect
    src = inspect.getsource(cr.collect)
    assert "_detect_opt65_billing_rounding_waste(" in src
    assert "_rerun_attempt_runs(" in src
    assert "_fetch_run_jobs_all_attempts" in src
    # `filter=latest` is DERIVED from the `filter=all` payload, not fetched again —
    # collect must go through `_attempt_job_samples` (which owns the derivation and
    # the rare re-fetch fallback), never call the latest-attempt fetcher directly.
    assert "_attempt_job_samples(" in src
    assert "_fetch_run_jobs_latest_attempt" not in src
    assert "_detect_opt64_rerun_attempt_waste(" in src
    assert "_detect_opt35_fail_fast_waste(" in src
    assert "_opt35_shard_job_specs(" in src
    assert "_supersede_static_opt35(" in src
    assert "_detect_opt57_timeout_default_burn(" in src
    assert "_opt57_timeout_job_specs(" in src
    assert "workflow_job_graph" in src
    assert "opt57_seed_workflows" in src
    # OPT57's event-scoped denominator comes from the EVENT page, not the all-status one.
    # (Pinned by name, not by an exact assignment line — the previous form pinned source
    # FORMATTING and broke on a reformat that changed no behavior.)
    assert "opt57_scope_runs" in src and "_all_status_event_runs(" in src
    assert "_detect_opt36_schedule_burn(" in src
    assert "_monthly_event_volume(" in src
    assert "_sample_event_runs(" in src
    # OPT36 is sized against the SCHEDULE page, never the all-status one.
    assert "schedule_runs, schedule_jobs, wf_doc, schedule_monthly" in src
    assert "wf_path, all_runs, jobs_per_run, wf_doc, schedule_monthly" not in src
    assert "_detect_opt46_superseded_runs(" in src
    assert "_detect_opt47_double_trigger(" in src
    assert "_all_status_runs(" in src


def test_opt57_seed_workflow_paths_include_timeoutless_non_matrix_workflows():
    graph = {
        ".github/workflows/timeout.yml": {
            "integration": {"timeout_minutes": False, "matrix": False},
            "unit": {"timeout_minutes": True, "matrix": False},
        },
        ".github/workflows/matrix.yml": {
            "sharded": {"timeout_minutes": False, "matrix": True},
        },
        ".github/workflows/missing.yml": {
            "integration": {"timeout_minutes": False, "matrix": False},
        },
    }
    assert cr._opt57_seed_workflow_paths(
        graph, {".github/workflows/timeout.yml", ".github/workflows/matrix.yml"}) == {
            ".github/workflows/timeout.yml"}


def test_measured_opt36_supersedes_static_residual_row():
    static = {"id": "f1", "pattern": "OPT36", "workflow_file": "cleanup.yml",
              "title": "Cron Schedule Too Frequent", "runner_min_saving": 10.0}
    measured = _opt36(_SCHEDULE_WF, _schedule_runs("a", "a", "b"))
    cr._supersede_static_opt36([static], measured)
    assert static["tier2_superseded_by"] == measured[0]["id"]
    import blocking_path as bp
    lines, count, _on_path = bp._also_noticed_block([static, *measured], "http://catalog")
    assert lines == [] and count == 0


def test_measured_opt35_supersedes_static_residual_row():
    static = {"id": "f1", "pattern": "OPT35", "workflow_file": "ci.yml",
              "title": "Missing `fail-fast`", "runner_min_saving": 10.0,
              "affected_jobs": ["test"]}
    measured = _opt35(_FAIL_FAST_WF, [_fail_fast_run()])
    cr._supersede_static_opt35([static], measured)
    assert static["tier2_superseded_by"] == measured[0]["id"]
    import blocking_path as bp
    lines, count, _on_path = bp._also_noticed_block([static, *measured], "http://catalog")
    assert lines == [] and count == 0


def test_measured_opt35_supersedes_only_matching_static_job():
    statics = [
        {"id": "f1", "pattern": "OPT35", "workflow_file": "ci.yml",
         "title": "Missing `fail-fast`", "runner_min_saving": 10.0,
         "affected_jobs": ["unit"]},
        {"id": "f2", "pattern": "OPT35", "workflow_file": "ci.yml",
         "title": "Missing `fail-fast`", "runner_min_saving": 20.0,
         "affected_jobs": ["e2e"]},
    ]
    measured = _opt35(_TWO_FAIL_FAST_WF, [[
        _ff_job("unit (1)", 0, 10, "failure"),
        _ff_job("unit (2)", 0, 25, "success"),
    ]])
    cr._supersede_static_opt35(statics, measured)
    assert statics[0]["tier2_superseded_by"] == measured[0]["id"]
    assert "tier2_superseded_by" not in statics[1]


def test_opt35_output_composes_with_pr1_stamps():
    f = _opt35(_FAIL_FAST_WF, [_fail_fast_run()])[0]
    crit = {"job_runner": {"test (1)": "ubuntu-latest"},
            "job_p50": {"test (1)": 600.0}, "floor_p50": 0.0,
            "runner_scope": "ubuntu-latest"}
    cr._stamp_sizing_basis(f)
    cr._stamp_tier2_neutrality(f, crit)
    assert f["sizing_basis"] == "measured"
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"


def test_measured_opt35_survives_late_sizing_and_neutrality_passes():
    f = _opt35(_FAIL_FAST_WF, [_fail_fast_run()])[0]
    crit = {"job_runner": {"test (1)": "ubuntu-latest"},
            "job_p50": {"test (1)": 600.0}, "floor_p50": 1000.0,
            "long_pole_p50": 1200.0, "runner_scope": "ubuntu-latest"}
    cr._size_finding(f, crit, monthly_volume=10)
    cr._stamp_tier2_neutrality(f, crit)
    assert f["runner_min_saving"] == 15.0
    assert f["sizing_basis"] == "measured"
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"


def test_opt36_output_composes_with_pr1_stamps():
    f = _opt36(_SCHEDULE_WF, _schedule_runs("a", "a", "b"))[0]
    crit = {"job_runner": {}, "job_p50": {}, "floor_p50": 0.0, "runner_scope": "ubuntu-latest"}
    cr._stamp_sizing_basis(f)
    cr._stamp_tier2_neutrality(f, crit)
    assert f["sizing_basis"] == "measured"
    assert f["tier2_neutrality"]["proof"] == "non_pr_event"


def test_opt65_output_composes_with_pr1_stamps():
    f = _opt65([_rounding_run(20, 20, 20), _rounding_run(30, 10, 10)])[0]
    crit = _rounding_crit()
    cr._size_finding(f, crit, monthly_volume=10)
    cr._stamp_sizing_basis(f)
    cr._stamp_tier2_neutrality(f, crit)
    assert f["runner_min_saving"] == 20.0
    assert f["sizing_basis"] == "measured"
    assert f["tier2_neutrality"]["proof"] == "below_cluster_floor"


# ========================= OPT57 timeout default burn =========================

_TIMEOUT_WF = {
    "on": {"pull_request": {}},
    "name": "CI",
    "jobs": {
        "integration": {
            "name": "Integration Tests",
            "runs-on": "ubuntu-latest",
            "steps": [{"run": "pnpm test:integration"}],
        }
    },
}


def _timeout_success_runs(n=5, seconds=1200.0):
    return [[_span_job("Integration Tests", seconds, "success", f"success-{i}")]
            for i in range(n)]


def _timeout_failed_runs(seconds=21600.0, conclusion="timed_out"):
    return [[_span_job("Integration Tests", seconds, conclusion, "timeout-1")]]


def _opt57(timeout_runs=None, success_runs=None, wf=None, monthly=30, denominator=6):
    return cr._detect_opt57_timeout_default_burn(
        "ci.yml",
        timeout_runs if timeout_runs is not None else _timeout_failed_runs(),
        success_runs if success_runs is not None else _timeout_success_runs(),
        wf if wf is not None else _TIMEOUT_WF,
        monthly,
        0,
        sample_denominator=denominator,
    )


def test_opt57_emits_measured_timeout_default_burn():
    out = _opt57()
    assert len(out) == 1
    f = out[0]
    assert f["pattern"] == "OPT57"
    assert f["affected_jobs"] == ["integration"]
    assert f["runner_min_saving"] == 1650.0
    assert f["wall_clock_p50_s"] == 0.0
    assert f["realization"] == "none"
    assert f["sizing_basis"] == "measured"
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"
    assert "near-default timeout burn" in f["measured_signal"]
    assert "successful p99" in f["measured_signal"]
    burn = f["timeout_default_burn"]
    assert burn["kind"] == "opt57_timeout_default_burn"
    assert burn["default_timeout_minutes"] == 360
    assert burn["recommended_timeout_minutes"] == 30
    assert burn["successful_duration_p99_s"] == 1200.0
    assert burn["successful_duration_samples"] == 5
    assert burn["sampled_timeout_burn_min"] == 330.0
    assert burn["run_ids"] == ["timeout-1"]
    assert f["measured_evidence"]["table"]["headers"][-1] == "Default-timeout burn min"


def test_opt57_requires_missing_timeout_minutes():
    wf = {
        **_TIMEOUT_WF,
        "jobs": {
            "integration": {
                **_TIMEOUT_WF["jobs"]["integration"],
                "timeout-minutes": 45,
            }
        },
    }
    assert _opt57(wf=wf) == []


def test_opt57_requires_successful_p99_basis():
    assert _opt57(success_runs=_timeout_success_runs(n=2)) == []


def test_opt57_requires_explicit_success_conclusion_for_p99_basis():
    missing_conclusion = [
        [_span_job("Integration Tests", 1200.0, None, f"unknown-{i}")]
        for i in range(5)
    ]
    assert _opt57(success_runs=missing_conclusion) == []


def test_opt57_does_not_match_suffixed_sibling_job_names():
    suffixed_success = [
        [_span_job("Integration Tests (smoke)", 1200.0, "success", f"success-{i}")]
        for i in range(5)
    ]
    suffixed_failure = [
        [_span_job("Integration Tests (smoke)", 21600.0, "timed_out", "timeout-1")]
    ]
    assert _opt57(timeout_runs=suffixed_failure, success_runs=suffixed_success) == []


def test_opt57_requires_near_default_failed_duration():
    below = cr._OPT57_NEAR_DEFAULT_TIMEOUT_S - 60.0
    assert _opt57(timeout_runs=_timeout_failed_runs(seconds=below)) == []


def test_opt57_withholds_recommendation_near_default_timeout():
    assert _opt57(success_runs=_timeout_success_runs(seconds=14000.0)) == []


def test_opt57_persists_full_precision_p99_for_timeout_recommendation():
    success_runs = [
        [_precise_span_job("Integration Tests", 1200.0004, "success", f"success-{i}")]
        for i in range(5)
    ]
    f = _opt57(success_runs=success_runs)[0]
    burn = f["timeout_default_burn"]
    assert burn["successful_duration_p99_s"] == 1200.0004
    assert burn["recommended_timeout_minutes"] == 31


def test_opt57_withholds_matrix_jobs_until_per_variant_p99_is_safe():
    wf = {
        **_TIMEOUT_WF,
        "jobs": {
            "integration": {
                **_TIMEOUT_WF["jobs"]["integration"],
                "strategy": {"matrix": {"shard": [1, 2, 3]}},
            }
        },
    }
    assert _opt57(wf=wf) == []


def test_opt57_failed_runs_filter_to_timing_event_scope():
    runs = [
        {"id": "push-fail", "event": "push", "conclusion": "timed_out"},
        {"id": "pr-fail", "event": "pull_request", "conclusion": "failure"},
        {"id": "pr-success", "event": "pull_request", "conclusion": "success"},
    ]
    assert [r["id"] for r in cr._opt57_scoped_workflow_runs(runs, "pull_request")] == [
        "pr-fail", "pr-success"]
    assert [r["id"] for r in cr._opt57_failed_workflow_runs(runs, "pull_request")] == [
        "pr-fail"]
    assert [r["id"] for r in cr._opt57_failed_workflow_runs(runs, "all-events")] == [
        "push-fail", "pr-fail"]


def test_opt57_run_ids_deoverlap_with_whole_run_eliminators():
    whole_run = {
        "id": "f-whole",
        "pattern": "OPT46",
        "sizing_basis": "measured",
        "tier2_neutrality": {"proof": "post_completion_waste"},
        "runner_min_saving": 2000.0,
        "tier2_sample_run_ids": ["timeout-1"],
    }
    timeout = _opt57()[0]
    cr._reconcile_tier2_overlap([whole_run, timeout])
    assert timeout["runner_min_saving"] == 0.0
    assert timeout["runner_min_overlap_s"] == 1650.0
    assert "already credited" in timeout["tier2_overlap_note"]


def test_opt57_overlap_uses_exact_scaled_sample_waste():
    whole_run = {
        "id": "f-whole",
        "pattern": "OPT46",
        "sizing_basis": "measured",
        "tier2_neutrality": {"proof": "post_completion_waste"},
        "runner_min_saving": 5000.0,
        "tier2_sample_run_ids": ["timeout-1"],
    }
    timeout = _opt57(timeout_runs=[
        [_span_job("Integration Tests", 21600.0, "timed_out", "timeout-1")],
        [_span_job("Integration Tests", 21000.0, "timed_out", "timeout-2")],
    ])[0]
    assert timeout["runner_min_saving"] == 3250.0
    cr._reconcile_tier2_overlap([whole_run, timeout])
    assert timeout["runner_min_saving"] == 1600.0
    assert timeout["runner_min_overlap_s"] == 1650.0


def test_opt57_job_scoped_rows_in_same_run_remain_additive():
    wf = {
        **_TIMEOUT_WF,
        "jobs": {
            "integration": _TIMEOUT_WF["jobs"]["integration"],
            "e2e": {
                "name": "E2E Tests",
                "runs-on": "ubuntu-latest",
                "steps": [{"run": "pnpm test:e2e"}],
            },
        },
    }
    success_runs = [
        [
            _span_job("Integration Tests", 1200.0, "success", f"success-{i}"),
            _span_job("E2E Tests", 1200.0, "success", f"success-{i}"),
        ]
        for i in range(5)
    ]
    timeout_runs = [[
        _span_job("Integration Tests", 21600.0, "timed_out", "timeout-1"),
        _span_job("E2E Tests", 21600.0, "timed_out", "timeout-1"),
    ]]
    findings = _opt57(timeout_runs=timeout_runs, success_runs=success_runs, wf=wf)
    assert {f["affected_jobs"][0] for f in findings} == {"integration", "e2e"}
    cr._reconcile_tier2_overlap(findings)
    assert [f["runner_min_saving"] for f in findings] == [1650.0, 1650.0]
    assert all("runner_min_overlap_s" not in f for f in findings)


def test_opt57_output_composes_with_pr1_stamps():
    f = _opt57()[0]
    crit = {"job_runner": {"integration": "ubuntu-latest"},
            "job_p50": {"integration": 1200.0}, "floor_p50": 0.0,
            "runner_scope": "ubuntu-latest"}
    cr._size_finding(f, crit, monthly_volume=30)
    cr._stamp_sizing_basis(f)
    cr._stamp_tier2_neutrality(f, crit)
    assert f["runner_min_saving"] == 1650.0
    assert f["sizing_basis"] == "measured"
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"


def test_opt46_output_composes_with_pr1_stamps():
    # The detector's finding must flow cleanly through the PR-1 Tier-2 stamp pass
    # (sizing_basis / neutrality) — the end-to-end composition the minimal offline
    # fixture can't exercise under the ≥3-timed-run floor.
    f = _opt46(_PUSH_WF, _overlapping("feature/x", 5))[0]
    crit = {"job_runner": {}, "job_p50": {}, "floor_p50": 0.0, "runner_scope": "ubuntu-latest"}
    cr._stamp_sizing_basis(f)
    cr._stamp_tier2_neutrality(f, crit)   # must NOT overwrite the detector's cert
    assert f["sizing_basis"] == "measured"          # OPT46 model is "measured"
    assert f["tier2_neutrality"]["proof"] == "post_completion_waste"  # detector cert survives


# --- the fetch-failure contract: a FAILED fetch is never served as a real answer ----
#
# The rule the pipeline states in `_fetch_run_jobs` and `_JobFetchMemo`: a fetch that
# FAILED returns None, and None is never cached, never coerced to `[]`, never handed to
# a detector as data. `[]` means "genuinely nothing", and only that. These pin the two
# places that got it wrong.

class _FlakyRunListClient:
    """Fails the all-status run-list fetch for one workflow N times, then succeeds."""

    def __init__(self, fail_times: int = 1) -> None:
        self.queries = 0
        self.errors = 0
        self.fail_times = fail_times
        self.all_status_calls = 0

    def available(self) -> bool:
        return True

    def text(self, endpoint, **kw):
        return None

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": ".github/workflows/ci.yml", "name": "ci"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            qs = m.group(2)
            if qs.startswith("per_page=1&"):
                return {"total_count": 30}
            if "status=success" in qs:
                return {"workflow_runs": [_runlist_row(300 + i) for i in range(20)]}
            self.all_status_calls += 1
            if self.all_status_calls <= self.fail_times:
                self.errors += 1
                return None                      # the gh error / timeout
            return {"workflow_runs": [_runlist_row(300 + i) for i in range(20)]}
        if re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint):
            return {"jobs": [{"id": 1, "name": "test", "run_attempt": 1,
                              "status": "completed", "conclusion": "success",
                              "started_at": "2026-01-01T00:00:00Z",
                              "completed_at": "2026-01-01T00:05:00Z",
                              "runner_name": "ubuntu-latest", "steps": []}]}
        return None


def _runlist_row(rid: int) -> dict:
    return {"id": rid, "event": "pull_request", "head_sha": f"h{rid}",
            "status": "completed", "conclusion": "success",
            "created_at": "2026-01-01T00:00:00Z",
            "run_started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:05:00Z"}


def test_all_status_runs_returns_None_on_a_failed_fetch_not_empty():
    """`[]` and "the fetch failed" are DIFFERENT facts and must not share a value.

    This page is the entire basis of the run-elimination family (OPT35/46/47/57/64) and
    supplies their `sample_denominator`. If a gh error becomes `[]`, every one of them
    reports CLEAN over a literal "0 of 0 runs" evidence line, while the main success
    sample recovers and the report looks perfectly healthy. One transient timeout, a
    silent all-clear."""
    class _Failing:
        queries = errors = 0
        def json(self, endpoint, allow_missing=False):
            return None                          # gh error

    class _Empty:
        queries = errors = 0
        def json(self, endpoint, allow_missing=False):
            return {"workflow_runs": []}         # a workflow that genuinely has no runs

    assert cr._all_status_runs(_Failing(), "o/r", 1, 100) is None
    assert cr._all_status_runs(_Empty(), "o/r", 1, 100) == []


def test_collect_never_caches_a_failed_run_list_as_an_empty_page(monkeypatch):
    """The cache must not turn one failed fetch into a permanent empty answer.

    The shallow loop fetches this page and the detector loop reads it back. Caching the
    failure as `[]` would serve the poisoned empty to the whole run-elimination family
    as a CACHE HIT — no retry, no error, no disclosure. A failure is not cached, so the
    detector loop re-fetches; and the error is still counted, so the report discloses
    incomplete coverage."""
    client = _FlakyRunListClient(fail_times=1)
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    doc = {"findings": [{"id": "f1", "pattern": "OPT1",
                         "workflow_file": ".github/workflows/ci.yml"}],
           "data_sources": {}}
    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    # The shallow loop's fetch failed; the detector loop RE-FETCHED rather than reading
    # back a cached `[]`.
    assert client.all_status_calls == 2, (
        "the failed all-status fetch was cached and served as a hit — the "
        f"run-elimination detectors got a laundered empty page (calls={client.all_status_calls})")
    # The failure is disclosed, not swallowed.
    assert out["data_sources"]["gh_error_count"] >= 1
    # ...and the run sample still exists (the success query is the independent path).
    assert out["data_sources"]["runs_sampled"] > 0


def test_collect_skips_run_elimination_when_the_page_stays_unavailable(monkeypatch):
    """When the page cannot be fetched AT ALL, the run-elimination detectors are SKIPPED
    (their absence is honest) rather than run against an empty sample and reported clean
    over "0 of 0 runs" — AND THE SKIP IS DISCLOSED, by workflow and by detector.

    Skipping alone is only half a fix. A finding that never appears reads exactly like a
    finding that looked and found nothing: the reader gets a report showing zero re-run
    waste, zero superseded runs and zero double-triggers on this workflow, which is a
    false-negative dressed as an all-clear. `client.errors` cannot carry the disclosure
    — it renders as "a few runs/jobs are absent, the P50s are marginally thinner", which
    is a DIFFERENT failure and false here (no P50 is affected). So the skip is DATA:
    `data_sources.detectors_skipped`, naming the workflow and every detector that never
    ran."""
    client = _FlakyRunListClient(fail_times=99)   # never succeeds
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    doc = {"findings": [{"id": "f1", "pattern": "OPT1",
                         "workflow_file": ".github/workflows/ci.yml"}],
           "data_sources": {}}
    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    assert out["data_sources"]["gh_error_count"] >= 1
    # No run-elimination finding may be emitted off an unavailable page — in particular
    # none may carry a zero denominator.
    for f in out["findings"]:
        if f.get("pattern") in ("OPT35", "OPT46", "OPT47", "OPT57", "OPT64"):
            raise AssertionError(
                f"{f['pattern']} was emitted although the all-status page was never "
                "fetched — it can only have been sized against a laundered empty page")

    skipped = out["data_sources"]["detectors_skipped"]
    assert skipped, (
        "the detectors were skipped but NOTHING says so — their absence from the "
        "findings is indistinguishable from 'we looked and it was clean'")
    entry = next(e for e in skipped if e["workflow"] == ".github/workflows/ci.yml")
    assert set(entry["detectors"]) >= {"OPT46", "OPT47", "OPT64"}, (
        f"the skipped detectors are not named: {entry}")
    assert "run list" in entry["reason"]


def test_a_persistently_unavailable_run_list_is_counted_once_not_once_per_retry(
        monkeypatch):
    """One unavailable RESOURCE, one error — not one per attempt at it.

    `_all_status_page` is called from the shallow loop and again from the detector loop
    (the retry is deliberate: a transient timeout must not permanently disable the
    run-elimination family). But the coverage banner reads `gh_error_count` as "how many
    things we couldn't get", so charging a persistently dead page twice inflates it
    against the only question its reader is asking."""
    client = _FlakyRunListClient(fail_times=99)
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    doc = {"findings": [{"id": "f1", "pattern": "OPT1",
                         "workflow_file": ".github/workflows/ci.yml"}],
           "data_sources": {}}
    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    assert client.all_status_calls >= 2, "the deliberate retry must still happen"
    assert out["data_sources"]["gh_error_count"] == 1, (
        "one unavailable run list was billed once per RETRY rather than once per "
        f"RESOURCE (got {out['data_sources']['gh_error_count']})")


def test_short_derived_sample_survives_a_failed_fallback_query(monkeypatch):
    """The fallback must never DESTROY a good derived sample.

    `_sample_runs` returns `[]` on a gh failure. Overwriting the derived sample with it
    unconditionally means: valid successes in hand -> the page looks short -> the
    fallback query times out -> the workflow now has NO sample, goes dormant, drops out
    of the p50 and contributes nothing — because the SECOND query failed. A short real
    sample beats no sample.

    Driven through `collect()`, and the oracle is the SAMPLE THAT SURVIVED
    (`runs_sampled` > 0, jobs fetched for those runs). An earlier version of this test
    re-implemented the production expression (`_sample_runs(...) or derived`) in its own
    body and never called `collect()` — so deleting the `or runs` from production left
    it green. A test whose expectation restates the implementation guards nothing."""
    client = _ShortPageFailedFallbackClient()
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    doc = {"findings": [{"id": "f1", "pattern": "OPT1",
                         "workflow_file": ".github/workflows/ci.yml"}],
           "data_sources": {}}
    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    assert client.success_query_calls >= 1, (
        "the fallback success query never fired — this corpus (a FULL page yielding "
        "only 12 successes) is supposed to trigger it, so the test is not testing "
        "what it claims")
    assert out["data_sources"]["runs_sampled"] == 12, (
        "the failed fallback query wiped out a perfectly good 12-run derived sample: "
        f"the workflow went dormant with {out['data_sources']['runs_sampled']} runs")
    assert out["data_sources"]["jobs_sampled"] > 0


class _ShortPageFailedFallbackClient:
    """A workflow whose FULL all-status page holds only 12 successes (so the derived
    sample is short and the explicit `status=success` fallback fires) — and whose
    fallback query then FAILS."""

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self.success_query_calls = 0

    def available(self) -> bool:
        return True

    def text(self, endpoint, **kw):
        return None

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": ".github/workflows/ci.yml", "name": "ci"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            qs = m.group(2)
            if qs.startswith("per_page=1&"):
                return {"total_count": 30}
            if "status=success" in qs:
                self.success_query_calls += 1
                self.errors += 1
                return None                      # the fallback query FAILS
            page = [_runlist_row(400 + i) for i in range(cr._COST_RUNLIST_MAX)]
            for r in page[12:]:
                r["conclusion"] = "failure"      # only 12 successes on a FULL page
            return {"workflow_runs": page}
        if re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint):
            return {"total_count": 1,
                    "jobs": [{"id": 1, "name": "test", "run_attempt": 1,
                              "status": "completed", "conclusion": "success",
                              "started_at": "2026-01-01T00:00:00Z",
                              "completed_at": "2026-01-01T00:05:00Z",
                              "runner_name": "ubuntu-latest", "steps": []}]}
        return None


def test_job_fetch_memo_makes_exactly_one_call_when_threads_race_the_same_run(
        monkeypatch):
    """The memo de-duplicates CONCURRENT first accesses, not just sequential ones.

    A plain check-then-act (read the map, drop the lock, fetch, write back) leaves a
    window where two threads of the same pool both miss and both call GitHub. Today's
    call sites hand it de-duplicated run lists so the window is rarely open; a
    cross-workflow job pool (PR #215) opens it wide. The contract the docstring states
    — a run id this pass has already fetched is not fetched again — has to hold under
    concurrency or it isn't a contract."""
    import threading

    calls: list[int] = []
    started = threading.Barrier(8)
    call_lock = threading.Lock()

    def _slow_fetch(_client, _repo, run_id):
        with call_lock:
            calls.append(run_id)
        time.sleep(0.05)                          # hold the window wide open
        return [_attempt_job("test", 1, 0, 5, "success", "j1")]

    memo = cr._JobFetchMemo()
    fetch = memo.wrap(_slow_fetch)
    results: list[object] = [None] * 8

    def _racer(i: int) -> None:
        started.wait(timeout=5)
        results[i] = fetch(object(), "o/r", 7001)

    threads = [threading.Thread(target=_racer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert calls == [7001], (
        f"{len(calls)} threads raced the same run id and each made its own gh call — "
        "the memo de-duplicates sequentially but not concurrently")
    assert all(r is not None and [j["name"] for j in r] == ["test"] for r in results), (
        "a racing caller got a wrong or missing payload")
    # ...and each caller still gets its OWN dicts: the memo hands out deep copies, so a
    # caller stamping run context onto its jobs cannot corrupt another's sample.
    assert len({id(r[0]) for r in results}) == len(results)


def test_job_fetch_memo_does_not_alias_two_anonymous_fetchers():
    """Two injected lambdas both answer to `__name__ == "<lambda>"`. Keying the memo on
    that name makes the SECOND one read the FIRST one's payload — a wrong-value bug, not
    just a wrong-count one."""
    memo = cr._JobFetchMemo()
    runs = [_run("main", "a", "push", run_id=8001)]
    a = memo.wrap(lambda _c, _r, rid: [_attempt_job("from-A", 1, 0, 5, "success", "a")])
    b = memo.wrap(lambda _c, _r, rid: [_attempt_job("from-B", 1, 0, 5, "success", "b")])

    got_a = a(object(), "o/r", 8001)
    got_b = b(object(), "o/r", 8001)
    assert [j["name"] for j in got_a] == ["from-A"]
    assert [j["name"] for j in got_b] == ["from-B"], (
        "the second anonymous fetcher was served the FIRST one's memoized payload — the "
        "memo collapsed two distinct fetchers into one flavour key")


def test_all_status_event_runs_returns_None_on_a_failed_fetch_not_empty():
    """The same no-silent-drop contract on the EVENT-scoped page. It is OPT57's
    event-scoped denominator and OPT36's schedule-run basis, so a failed fetch laundered
    into `[]` sizes both against zero runs and reports a schedule-burning, timeout-less
    workflow CLEAN."""
    class _Failing:
        queries = errors = 0
        def json(self, endpoint, allow_missing=False):
            return None

    class _Empty:
        queries = errors = 0
        def json(self, endpoint, allow_missing=False):
            return {"workflow_runs": []}

    assert cr._all_status_event_runs(_Failing(), "o/r", 1, "schedule", 100) is None
    assert cr._all_status_event_runs(_Empty(), "o/r", 1, "schedule", 100) == []


def test_one_failed_all_status_page_is_disclosed_as_ONE_gap_not_two(monkeypatch):
    """Regression (post-merge adversarial review of #212): the OPT57 skip branch used to
    re-note the all-status page's failure as a SECOND `run_list_fetch_failures` entry
    whenever OPT57 was scoped-to-all — one failed physical page disclosed as TWO
    "workflow samples", framing a workflow whose critical path WAS measured (the success
    sample worked) as "MISSING from the sample". The dedup keeps the run-list gap entry
    unique per physical page; `detectors_skipped` still names OPT57 separately (that is
    the correct channel for "the detector never ran")."""
    import base64
    yaml_txt = ("on: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: echo hi\n")   # no timeout-minutes -> OPT57-eligible

    def _run_row(rid, wall=300):
        mm, ss = wall // 60, wall % 60
        return {"id": rid, "event": "push", "head_sha": f"h{rid}", "status": "completed",
                "conclusion": "success", "created_at": "2026-01-01T00:00:00Z",
                "run_started_at": "2026-01-01T00:00:00Z",
                "updated_at": f"2026-01-01T00:{mm:02d}:{ss:02d}Z"}

    class _AllStatusPageFails:
        """flaky.yml: all-status page persistently FAILS; the success sample works, so
        the workflow is measured — exactly the shape where the double-note fired."""
        def __init__(self):
            self.queries = 0
            self.errors = 0
        def available(self):
            return True
        def text(self, endpoint, **kw):
            return None
        def _bump(self, *, query=False, error=False):
            self.queries += int(query)
            self.errors += int(error)
        def json(self, endpoint, allow_missing=False):
            self.queries += 1
            if endpoint.startswith("repos/o/r/actions/workflows?"):
                return {"workflows": [{"id": 1, "path": ".github/workflows/flaky.yml",
                                       "name": "flaky", "state": "active"}]}
            if endpoint.startswith("repos/o/r/contents/"):
                return {"content": base64.b64encode(yaml_txt.encode()).decode()}
            m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
            if m:
                qs = m.group(2)
                if re.search(r"per_page=1(?![0-9])", qs):
                    return {"total_count": 30}
                if "status=success" in qs:
                    return {"workflow_runs": [_run_row(1000 + i) for i in range(20)]}
                if not allow_missing:            # the all-status page: persistent failure
                    self.errors += 1
                return None
            if re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint):
                return {"total_count": 1, "jobs": [
                    {"id": 1, "name": "build", "run_attempt": 1, "status": "completed",
                     "conclusion": "success", "started_at": "2026-01-01T00:00:00Z",
                     "completed_at": "2026-01-01T00:05:00Z",
                     "runner_name": "ubuntu-latest", "steps": []}]}
            return None

    client = _AllStatusPageFails()
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    doc = {"findings": [{"id": "f1", "pattern": "OPT1",
                         "workflow_file": ".github/workflows/flaky.yml"}],
           "data_sources": {}}
    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10,
                     created_before="2026-01-15T00:00:00Z")

    ds = out["data_sources"]
    gaps = [g for g in ds["run_list_fetch_failures"]
            if g["workflow_file"] == ".github/workflows/flaky.yml"]
    assert len(gaps) == 1, (
        "ONE failed physical page must be ONE gap entry, not one per consumer: "
        f"{gaps}")
    # The detector-skip channel still names OPT57 — the dedup removes the duplicate
    # run-list entry, never the skip disclosure.
    skipped = {d for e in ds.get("detectors_skipped", []) for d in e["detectors"]
               if e["workflow"] == ".github/workflows/flaky.yml"}
    assert "OPT57" in skipped, f"OPT57's skip must stay disclosed: {skipped}"


def test_opt65_custom_runner_label_is_never_credited():
    # #104 review (P1): OPT65's saving IS the per-job round-up delta, a billing rule
    # only known for GitHub-hosted / StarSling label families. A custom or plainly
    # self-hosted label must resolve None (base skipped), never credit "measured"
    # waste that may not exist on that runner's (unknown) billing.
    crit = {"job_runner": {"t (1)": "my-custom-box"}, "job_p50": {"t (1)": 5.0}}
    assert cr._billed_job_runner("t (1)", crit) is None
    for lbl in ("ubuntu-latest", "windows-2022", "macos-14", "starsling-ubuntu-24.04-8"):
        crit = {"job_runner": {"t (1)": lbl}, "job_p50": {"t (1)": 5.0}}
        assert cr._billed_job_runner("t (1)", crit) == lbl


# ============ OPT77 repeated fixed setup across independent small jobs ============
#
# The motivating shape: N independent checks in one workflow, each starting a
# runner, checking out and installing before a few seconds of real work.
# Consolidating them into ONE job removes (N-1) payments of that setup prefix.
# Nothing here claims a speedup — every case pins `wall_clock_p50_s == 0.0`.

def _setup_job(name, setup_s, work_s, runner="ubuntu-latest"):
    """A job whose step timeline is a leading setup prefix then one work step."""
    t = 0.0

    def _stamp(offset):
        m, s = divmod(int(offset), 60)
        return f"2026-06-01T00:{m:02d}:{s:02d}Z"

    steps = []
    half = setup_s / 2.0
    for step_name, dur in (("Set up job", half),
                           ("Run actions/checkout@v4", setup_s - half),
                           ("Run tests", work_s)):
        steps.append({"name": step_name, "number": len(steps) + 1,
                      "started_at": _stamp(t), "completed_at": _stamp(t + dur)})
        t += dur
    return {
        "name": name,
        "started_at": _stamp(0),
        "completed_at": _stamp(setup_s + work_s),
        "labels": [runner],
        "steps": steps,
    }


_OPT77_NAMES = ("lint", "typecheck", "audit")


def _opt77_run(setup_s=80.0, work_s=10.0, runner="ubuntu-latest", names=_OPT77_NAMES):
    return [_setup_job(n, setup_s, work_s, runner) for n in names]


def _opt77_crit(*, floor=600.0, setup_s=80.0, work_s=10.0, runner="ubuntu-latest",
                names=_OPT77_NAMES):
    job_p50 = {n: setup_s + work_s for n in names}
    # A real workflow has something besides the candidate checks, and the
    # consolidated job is measured against the tallest job that REMAINS after the
    # consolidation — so the fixture carries one. At `floor` it plays the part the
    # old cluster-floor comparison played, keeping these cases' margins unchanged.
    job_p50["test"] = floor
    return {
        "floor_p50": floor,
        "long_pole_p50": floor + 60.0,
        "job_p50": job_p50,
        "job_runner": dict({n: runner for n in names}, test=runner),
        "runner_scope": runner,
    }


def _opt77_wf(names=_OPT77_NAMES, needs=None):
    jobs = {n: {"runs-on": "ubuntu-latest"} for n in names}
    for key, parents in (needs or {}).items():
        jobs[key]["needs"] = parents
    return {"on": {"pull_request": {}}, "jobs": jobs}


def _opt77(jpr=None, crit=None, wf=None, monthly=100):
    return cr._detect_opt77_repeated_setup_across_small_jobs(
        "ci.yml", jpr if jpr is not None else [_opt77_run(), _opt77_run()],
        crit or _opt77_crit(), wf if wf is not None else _opt77_wf(), monthly, 0)


def test_opt77_promotes_measured_setup_consolidation():
    out = _opt77()
    assert len(out) == 1
    f = out[0]
    assert f["pattern"] == "OPT77"
    assert f["affected_jobs"] == ["audit", "lint", "typecheck"]
    assert f["wall_clock_p50_s"] == 0.0
    assert f["sizing_basis"] == "measured"
    assert f["realization"] == "none"
    # 2 sampled runs x (3-1) removed setups x 80s = 320s = 5.333 min sampled,
    # scaled by 100/2 = 50  ->  266.7 runner-min/mo.
    assert f["runner_min_saving"] == 266.7
    sc = f["setup_consolidation"]
    assert sc["kind"] == "opt77_repeated_setup"
    assert sc["credited_jobs"] == ["audit", "lint", "typecheck"]
    assert sc["removed_setup_payments"] == 2
    assert sc["setup_p50_s"] == 80.0
    assert sc["projected_consolidated_p50_s"] == 90.0
    assert sc["occurrences"] == 2
    assert f["tier2_neutrality"]["proof"] == "below_cluster_floor"
    assert f["tier2_neutrality"]["margin_s"] == 510.0
    # The recipe's concurrency precondition must be stated in the size note, with
    # the sequential cost named — a serial consolidation costs setup + SUM(work).
    assert "concurrently" in f["size_note"].lower()
    assert "sequential" in f["size_note"].lower()
    assert "required status check" in f["guardrail"]


def test_opt77_needs_at_least_three_independent_jobs():
    two = ("lint", "typecheck")
    assert _opt77(
        jpr=[_opt77_run(names=two), _opt77_run(names=two)],
        crit=_opt77_crit(names=two), wf=_opt77_wf(names=two)) == []


def test_opt77_requires_one_known_billed_runner_across_the_group():
    run = _opt77_run()
    run[2]["labels"] = ["windows-latest"]
    crit = _opt77_crit()
    crit["job_runner"]["audit"] = "windows-latest"
    assert _opt77(jpr=[run, run]) == []          # observed label disagrees with crit
    assert _opt77(crit=crit) == []               # declared labels disagree
    assert _opt77(jpr=[_opt77_run(runner="self-hosted"), _opt77_run(runner="self-hosted")],
                  crit=_opt77_crit(runner="self-hosted")) == []


def test_opt77_withholds_when_a_needs_edge_links_the_group():
    wf = _opt77_wf(needs={"audit": ["lint"]})
    assert _opt77(wf=wf) == []
    # Indirect edges count too: audit -> typecheck -> lint is still not a
    # consolidatable set of independent jobs.
    wf = _opt77_wf(needs={"typecheck": "lint", "audit": ["typecheck"]})
    assert _opt77(wf=wf) == []


def test_opt77_requires_setup_to_dominate_useful_work():
    # 20s setup against 200s of useful work: consolidating saves almost nothing
    # relative to what these jobs actually do, so the pattern must not fire.
    jpr = [_opt77_run(setup_s=20.0, work_s=200.0),
           _opt77_run(setup_s=20.0, work_s=200.0)]
    assert _opt77(jpr=jpr, crit=_opt77_crit(setup_s=20.0, work_s=200.0)) == []


def test_opt77_withholds_when_the_consolidated_job_reaches_the_cluster_floor():
    # setup 80s + max useful 10s = 90s projected; a 90s floor means the merge
    # gate would not stay strictly faster than the consolidated job.
    assert _opt77(crit=_opt77_crit(floor=90.0)) == []


def test_opt77_requires_jobs_resolvable_to_independent_yaml_jobs():
    # No workflow YAML -> the independence gate cannot be evaluated -> withhold.
    assert _opt77(wf={}) == []
    # A matrix leg's display name resolves to no YAML job of that name.
    legs = ("check (a)", "check (b)", "check (c)")
    assert _opt77(jpr=[_opt77_run(names=legs), _opt77_run(names=legs)],
                  crit=_opt77_crit(names=legs),
                  wf={"jobs": {"check": {"runs-on": "ubuntu-latest"}}}) == []


def test_opt77_requires_monthly_volume():
    assert _opt77(monthly=0) == []
    assert _opt77(monthly=None) == []


def test_opt77_is_wired_into_collect():
    import inspect
    assert "_detect_opt77_repeated_setup_across_small_jobs(" in inspect.getsource(cr.collect)


def _setup_job_named(name, setup_steps, work_s, runner="ubuntu-latest"):
    """A job whose leading setup prefix is the GIVEN named steps, then one work step."""
    t = 0.0

    def _stamp(offset):
        m, s = divmod(int(offset), 60)
        return f"2026-06-01T00:{m:02d}:{s:02d}Z"

    steps = []
    for step_name, dur in list(setup_steps) + [("Run tests", work_s)]:
        steps.append({"name": step_name, "number": len(steps) + 1,
                      "started_at": _stamp(t), "completed_at": _stamp(t + dur)})
        t += dur
    return {"name": name, "started_at": _stamp(0), "completed_at": _stamp(t),
            "labels": [runner], "steps": steps}


_OPT77_DISTINCT_SETUPS = {
    # Same runner, same setup DURATION, same useful work — but three different
    # toolchains. A consolidation still has to install all three, so only the
    # shared `Set up job` + checkout is actually removable.
    "lint": [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
             ("Run actions/setup-node@v4", 15.0), ("Install npm dependencies", 50.0)],
    "typecheck": [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
                  ("Run actions/setup-python@v5", 15.0), ("Install pip requirements", 50.0)],
    "audit": [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
              ("Run actions/setup-go@v5", 15.0), ("Install go modules", 50.0)],
}


def _opt77_distinct_run(work_s=10.0, runner="ubuntu-latest"):
    return [_setup_job_named(n, s, work_s, runner)
            for n, s in _OPT77_DISTINCT_SETUPS.items()]


def test_opt77_withholds_when_the_setup_prefixes_are_not_the_same_work():
    """Jobs are grouped by runner label, so three checks with entirely DIFFERENT
    dependency installs (node / python / go) can land in one group. Consolidating
    them removes nothing but the one shared checkout — every distinct install must
    still run. Crediting `(N-1) x setup_p50` there overstates the removable
    runner-minutes, and projecting `max(setup) + max(useful)` understates the
    consolidated job, so the cluster-floor guard can pass when the real job would
    blow through the floor. The finding must be WITHHELD."""
    names = tuple(_OPT77_DISTINCT_SETUPS)
    jpr = [_opt77_distinct_run(), _opt77_distinct_run()]
    crit = _opt77_crit(setup_s=80.0, work_s=10.0, names=names)
    assert _opt77(jpr=jpr, crit=crit, wf=_opt77_wf(names=names)) == []


def test_opt77_still_fires_when_every_job_shares_one_setup_prefix():
    """The complement of the gate above: an identical prefix across the group is
    exactly the shape the saving model is valid for, and must still be credited."""
    shared = [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
              ("Run actions/setup-node@v4", 15.0), ("Install npm dependencies", 50.0)]
    names = ("lint", "typecheck", "audit")
    jpr = [[_setup_job_named(n, shared, 10.0) for n in names] for _ in range(2)]
    out = _opt77(jpr=jpr, crit=_opt77_crit(setup_s=80.0, work_s=10.0, names=names),
                 wf=_opt77_wf(names=names))
    assert len(out) == 1
    assert out[0]["pattern"] == "OPT77"
    assert out[0]["wall_clock_p50_s"] == 0.0


def test_opt77_door_note_describes_its_own_basis():
    """Every OPT77 finding stamps `runner_min_door_note`. The generic `measured`
    branch describes a RUN-ELIMINATION detector whose basis is an eliminated-runs /
    prior-attempt slice — which OPT77 is not, and does not use. Shipping that string
    on an OPT77 finding is provenance text that misdescribes the number beside it."""
    policy, reason = cr._rm_door_policy("OPT77")
    assert policy == cr._RM_DOOR_NOT_DERIVABLE
    low = reason.lower()
    assert "run-elimination" not in low
    assert "eliminated runs" not in low
    assert "prior-attempt" not in low
    assert "setup" in low


def test_opt77_guardrail_reaches_the_copy_paste_agent_prompt():
    """The rendered agent prompt is the artifact that performs the fix, and it is
    built to be self-contained: `_tier2_guardrail_sentence` lifts the note's text
    from the literal token GUARDRAIL to the end. Without that token OPT77's prompt
    ships with no failure mode at all — not the intra-job concurrency requirement
    (run sequentially the consolidated job costs setup + the SUM of the tasks), not
    the required-status-check rename, not the lost failure isolation — and it never
    names which jobs to merge."""
    import importlib.util as _ilu
    import pathlib as _pl
    _bp_path = _pl.Path(cr.__file__).with_name("blocking_path.py")
    _spec = _ilu.spec_from_file_location("_bp_for_opt77", _bp_path)
    bp = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(bp)

    out = _opt77()
    assert len(out) == 1
    sentence = bp._tier2_guardrail_sentence(out[0])
    assert sentence, "OPT77's note carries no GUARDRAIL token, so the prompt has none"
    low = sentence.lower()
    assert "concurrent" in low            # the requirement
    assert "sum" in low                   # and WHY it is load-bearing
    assert "required status check" in low  # branch protection must be updated
    assert "re-run" in low                # the failure-isolation trade
    for name in _OPT77_NAMES:             # the group the fix must actually merge
        assert name in sentence


def test_opt77_withholds_a_job_name_that_is_not_one_job():
    """`_consolidation_yaml_key` resolves an observed job name against the YAML.
    A MATRIX job that declares a static `name:` (no ${{ matrix.* }} in it) renders
    every leg under that one name, so it resolves to exactly one YAML job and slips
    past the matrix exclusion — while `split[name]` keeps only the last leg seen, so
    the credited medians describe one arbitrary leg of a matrix, not an independent
    job. A name carried by more than one job in a single run is not one job."""
    names = ("lint", "typecheck", "audit")
    run = _opt77_run(names=names)
    # `audit` is a 2-leg matrix with a static name: both legs render as "audit".
    collided = run + [_setup_job("audit", 80.0, 10.0)]
    assert _opt77(jpr=[collided, list(collided)],
                  crit=_opt77_crit(names=names), wf=_opt77_wf(names=names)) == []


def test_setup_classifier_counts_an_unnamed_dependency_install():
    """GitHub names an unnamed `run:` step after its command, so the single largest
    part of a real setup prefix arrives as `Run npm ci` / `Run pip install -r ...`.
    Those have to classify as setup: `_leading_setup_prefix` stops at the first
    non-setup step, so if the install is not setup it is both missing from the
    measured prefix AND counted as "useful work", which withholds the finding on
    exactly the shape the pattern exists to report."""
    for name in ("Run npm ci", "Run pnpm install --frozen-lockfile",
                 "Run yarn install", "Run pip install -r requirements.txt",
                 "Run bundle install", "Run poetry install",
                 "Run go mod download", "Run cargo fetch", "Run composer install"):
        assert cr._classify_step(name) == "setup", name
    # …and the widening must not swallow steps that are not setup.
    for name in ("Run actions/upload-artifact@v4", "Run ./my-composite-action",
                 "Run npm test", "Run eslint .", "Run docker/build-push-action@v5"):
        assert cr._classify_step(name) != "setup", name


def test_opt77_withholds_when_only_the_projection_reaches_the_floor():
    """The projection-vs-floor gate is this pattern's ENTIRE wall-clock-safety
    claim, and the homogeneous fixture cannot prove it: there every job p50 equals
    the projection, so the per-job `p50 < floor` gate catches the case too and
    either gate can be deleted with the suite still green. This group is
    heterogeneous — every job p50 (90/102/100) is below the 110s floor, yet the
    consolidated job projects to max(setup) + max(useful) = 100 + 40 = 140s, which
    is NOT. Nothing but the projection comparison can withhold it — the per-job
    medians are all comfortably clear. (Two lines enforce that comparison: the
    strict check and the float-precision margin guard behind it, so deleting
    either one alone still withholds. This pins the property, not the line.)"""
    names = ("lint", "typecheck", "audit")
    spec = {"lint": (80.0, 10.0), "typecheck": (100.0, 2.0), "audit": (60.0, 40.0)}
    run = [_setup_job(n, s, w) for n, (s, w) in spec.items()]
    # `test` at 110s is the tallest job that REMAINS after the consolidation, so
    # it is what the projection is measured against.
    crit = dict(_opt77_crit(names=names), floor_p50=110.0,
                job_p50=dict({n: s + w for n, (s, w) in spec.items()}, test=110.0))
    assert _opt77(jpr=[run, list(run)], crit=crit, wf=_opt77_wf(names=names)) == []


def test_opt77_credits_the_smallest_setup_in_a_heterogeneous_group():
    """The saving is deliberately `(N-1) x the SMALLEST` setup p50 — the conservative
    floor on what each removed payment was worth. Every other fixture gives the jobs
    an identical setup, so min == max and flipping one for the other changes nothing.
    Here the setups differ (80/40/120), so `max` would stamp 120s and roughly triple
    the credited runner-minutes."""
    names = ("lint", "typecheck", "audit")
    spec = {"lint": (80.0, 10.0), "typecheck": (40.0, 10.0), "audit": (120.0, 10.0)}
    run = [_setup_job(n, s, w) for n, (s, w) in spec.items()]
    crit = dict(_opt77_crit(names=names), floor_p50=600.0,
                job_p50=dict({n: s + w for n, (s, w) in spec.items()}, test=600.0))
    out = _opt77(jpr=[run, list(run)], crit=crit, wf=_opt77_wf(names=names))
    assert len(out) == 1
    sc = out[0]["setup_consolidation"]
    assert sc["setup_p50_s"] == 40.0
    assert sc["runner_min_saving"] == 133.3
    assert sc["projected_consolidated_p50_s"] == 130.0


def test_opt77_withholds_on_a_needs_chain_through_a_non_credited_job():
    """Transitive independence, for real. The existing chain fixture links two
    CREDITED jobs directly, so it never exercises the ancestor walk. Here the chain
    runs `audit -> build -> lint` through `build`, which is far too big to be a
    candidate and so never appears in the group — the only thing that can catch it
    is the transitive walk."""
    names = ("lint", "typecheck", "audit")
    wf = _opt77_wf(names=names)
    wf["jobs"]["build"] = {"runs-on": "ubuntu-latest", "needs": ["lint"]}
    wf["jobs"]["audit"]["needs"] = ["build"]
    assert _opt77(wf=wf) == []


def test_opt77_credits_only_runs_where_every_job_in_the_group_appeared():
    """`occurrences` counts runs in which ALL credited jobs ran, because a run where
    one of them was skipped paid no setup for it and so had no duplicate payment to
    remove. Counting runs where ANY of them appeared would over-credit."""
    names = ("lint", "typecheck", "audit")
    full = _opt77_run(names=names)
    partial = _opt77_run(names=("lint", "typecheck"))     # `audit` did not run
    out = _opt77(jpr=[full, partial], crit=_opt77_crit(names=names),
                 wf=_opt77_wf(names=names))
    assert len(out) == 1
    sc = out[0]["setup_consolidation"]
    assert sc["occurrences"] == 1
    assert sc["runner_min_saving"] == 133.3


def test_setup_classifier_never_reads_a_build_or_test_command_as_setup():
    """`_classify_step` is the ONE shared definition of a setup step, and OPT49
    ("Slow Setup Step") and OPT51 (install ratio) consume it too — so a build or
    test command misread as setup does not just inflate a consolidation, it invents
    a sized "cache this setup" finding about a step that is neither.

    `mvn install` COMPILES and tests; it is a build, not a dependency install. And
    an install keyword must be the tool's own subcommand, never a word occurring
    somewhere later on the line — a test filter naming a `Get*` class, or a spec
    file called `get_spec.rb`, is not setup."""
    for name in ("Run mvn install", "Run mvn -B install -DskipTests",
                 "Run mvn verify", "Run mvn -B test -Dtest=GetUserIT",
                 "Run gradle test --tests '*.GetSpec'",
                 "Run gradle build", "Run bundle exec rspec spec/get_spec.rb",
                 "Run poetry run pytest tests/get_user_test.py",
                 "Run poetry run python scripts/get_data.py",
                 "Run npm run download-fixtures"):
        assert cr._classify_step(name) != "setup", name


def test_setup_classifier_recognises_the_remaining_install_idioms():
    """Complements the negative list above: the tool's own install subcommand still
    has to classify as setup, including the ones the first pass missed entirely
    (`mix deps.get` could never match, and `uv sync` is the modern uv idiom)."""
    for name in ("Run mix deps.get", "Run uv sync", "Run uv pip install -r req.txt",
                 "Run pipenv install --deploy", "Run npm i --prefer-offline",
                 "Run python3 -m pip install -r requirements.txt",
                 "Run mvn dependency:go-offline"):
        assert cr._classify_step(name) == "setup", name


def test_opt77_reports_one_long_job_beside_a_flat_row_of_small_checks():
    """The motivating shape, which used to be the one shape that could not fire.

    The gate asks "does the merge gate get longer if these jobs are collapsed?",
    and the gate afterwards is set by the jobs that were NOT touched. Measuring
    against a floor the group's own members define measures against something the
    fix removes. Here five equally-sized checks sit beside one 9-minute test job:
    the second-tallest job in the workflow is itself a group member, so the old
    comparison rejected every candidate and reported nothing. The consolidated job
    projects to 90s against the 540s test job that remains."""
    names = ("lint", "typecheck", "audit", "format-check", "spellcheck")
    run = [_setup_job(n, 80.0, 10.0) for n in names]
    job_p50 = {n: 90.0 for n in names}
    job_p50["test"] = 540.0
    crit = {"floor_p50": 90.0, "long_pole_p50": 540.0, "job_p50": job_p50,
            "job_runner": dict({n: "ubuntu-latest" for n in names}, test="ubuntu-latest"),
            "runner_scope": "ubuntu-latest"}
    wf = _opt77_wf(names=names)
    wf["jobs"]["test"] = {"runs-on": "ubuntu-latest"}
    out = _opt77(jpr=[run, list(run)], crit=crit, wf=wf)
    assert len(out) == 1, out
    sc = out[0]["setup_consolidation"]
    assert sorted(sc["credited_jobs"]) == sorted(names)
    assert sc["remaining_tallest_job"] == "test"
    assert sc["remaining_tallest_p50_s"] == 540.0
    assert sc["projected_consolidated_p50_s"] == 90.0
    assert out[0]["tier2_neutrality"]["margin_s"] == 450.0
    assert out[0]["wall_clock_p50_s"] == 0.0


def test_opt77_withholds_when_no_job_outside_the_group_is_taller():
    """The complement: if nothing outside the group is taller than the consolidated
    job, collapsing the group can only make the consolidated job the new gate.
    A workflow that is nothing but the candidate checks has no remaining job to
    measure against at all, and must report nothing rather than guess."""
    names = ("lint", "typecheck", "audit")
    run = [_setup_job(n, 80.0, 10.0) for n in names]
    crit = {"floor_p50": 90.0, "long_pole_p50": 90.0,
            "job_p50": {n: 90.0 for n in names},
            "job_runner": {n: "ubuntu-latest" for n in names},
            "runner_scope": "ubuntu-latest"}
    assert _opt77(jpr=[run, list(run)], crit=crit, wf=_opt77_wf(names=names)) == []
    # And a remaining job that is SHORTER than the projection is no protection.
    crit2 = dict(crit, job_p50=dict({n: 90.0 for n in names}, docs=45.0))
    wf2 = _opt77_wf(names=names)
    wf2["jobs"]["docs"] = {"runs-on": "ubuntu-latest"}
    assert _opt77(jpr=[run, list(run)], crit=crit2, wf=wf2) == []


# ==== OPT77 supersedes OPT65 when both describe the same jobs ====
#
# The two were described as disjoint because matrix legs never resolve to a
# single YAML job by name. But OPT65 never required a declared matrix: it groups
# on a trailing parenthetical in the OBSERVED job name. Three ordinary jobs that
# happen to be named `lint (eslint)`, `lint (biome)`, `lint (stylelint)` resolve
# to one YAML job each AND form an OPT65 "matrix base", so both fire on the same
# three jobs and one edit is reported as two levers.

_OPT65_OVERLAP_NAMES = ("lint (biome)", "lint (eslint)", "lint (stylelint)")


def _overlap_run():
    """Three tiny same-runner checks sharing one setup prefix, beside two taller
    jobs so there is something for the consolidation to be measured against."""
    return [_setup_job(n, 14.0, 6.0) for n in _OPT65_OVERLAP_NAMES]


def _overlap_crit():
    p50 = {n: 20.0 for n in _OPT65_OVERLAP_NAMES}
    p50["build"] = 300.0
    p50["test"] = 540.0
    return {"floor_p50": 300.0, "long_pole_p50": 540.0, "job_p50": p50,
            "job_runner": {n: "ubuntu-latest" for n in p50},
            "runner_scope": "ubuntu-latest"}


def _overlap_wf():
    jobs = {n.replace(" ", "-").replace("(", "").replace(")", ""): {
        "runs-on": "ubuntu-latest", "name": n} for n in _OPT65_OVERLAP_NAMES}
    jobs["build"] = {"runs-on": "ubuntu-latest"}
    jobs["test"] = {"runs-on": "ubuntu-latest"}
    return {"on": {"pull_request": {}}, "jobs": jobs}


def test_both_levers_really_do_fire_on_the_same_three_plain_jobs():
    """The premise of the supersede rule, pinned so it cannot quietly stop being
    true: these are plain jobs, not a matrix, and both detectors claim them."""
    runs = [_overlap_run(), _overlap_run()]
    crit, wf = _overlap_crit(), _overlap_wf()
    o65 = cr._detect_opt65_billing_rounding_waste("ci.yml", runs, crit, 100, 0)
    o77 = cr._detect_opt77_repeated_setup_across_small_jobs(
        "ci.yml", runs, crit, wf, 100, 0)
    assert len(o65) == 1, o65
    assert len(o77) == 1, o77
    assert sorted(o65[0]["affected_jobs"]) == sorted(_OPT65_OVERLAP_NAMES)
    assert sorted(o77[0]["affected_jobs"]) == sorted(_OPT65_OVERLAP_NAMES)


def test_opt77_supersedes_opt65_so_one_edit_is_one_lever():
    """One edit — merge those three checks into one job — must render as ONE
    lever. OPT65's round-up minutes are real and go unreported in the overlap,
    which slightly understates the total; that is accepted deliberately, because
    OPT77's saving model is raw removed compute and folding a billable-round-up
    quantity into it would break its measured basis."""
    runs = [_overlap_run(), _overlap_run()]
    crit, wf = _overlap_crit(), _overlap_wf()
    findings = (cr._detect_opt65_billing_rounding_waste("ci.yml", runs, crit, 100, 0)
                + cr._detect_opt77_repeated_setup_across_small_jobs(
                    "ci.yml", runs, crit, wf, 100, 1))
    kept = cr._supersede_opt65_with_opt77(findings)
    assert [f["pattern"] for f in kept] == ["OPT77"], [f["pattern"] for f in kept]


def test_opt65_survives_when_it_describes_different_jobs():
    """The supersede is scoped to the overlapping job set, not to the pattern:
    an OPT65 finding about a genuine matrix elsewhere in the workflow is
    untouched."""
    o65 = {"id": "f1", "pattern": "OPT65", "workflow_file": "ci.yml",
           "affected_jobs": ["unit (3.9)", "unit (3.11)", "unit (3.12)"]}
    o77 = {"id": "f2", "pattern": "OPT77", "workflow_file": "ci.yml",
           "affected_jobs": list(_OPT65_OVERLAP_NAMES)}
    kept = cr._supersede_opt65_with_opt77([o65, o77])
    assert [f["id"] for f in kept] == ["f1", "f2"]
    # …and a same-named job set in a DIFFERENT workflow is also untouched.
    other = {"id": "f3", "pattern": "OPT65", "workflow_file": "release.yml",
             "affected_jobs": list(_OPT65_OVERLAP_NAMES)}
    kept = cr._supersede_opt65_with_opt77([other, o77])
    assert [f["id"] for f in kept] == ["f3", "f2"]


# ==== what "the same setup prefix" tolerates, and what it must not ====
#
# Comparing the prefix as written makes the gate correct but unusably strict:
# one job pinning `setup-node@v3` while the others are on `@v4`, or one passing
# `--prefer-offline`, is the SAME setup work and must still group. A different
# toolchain is not, and must still split. Without a stated contract the lever
# quietly never fires on a real repo, which is indistinguishable from it working.

def _prefix_variant_fires(first_job_steps):
    names = ("lint", "typecheck", "audit")
    base = [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
            ("Run actions/setup-node@v4", 15.0), ("Run npm ci", 50.0)]
    jobs = [_setup_job_named(names[0], first_job_steps, 10.0)] + [
        _setup_job_named(n, base, 10.0) for n in names[1:]]
    crit = _opt77_crit(names=names)
    return bool(_opt77(jpr=[jobs, list(jobs)], crit=crit, wf=_opt77_wf(names=names)))


def test_opt77_tolerates_version_and_flag_churn_within_one_setup_prefix():
    """A pinned-action bump and an extra command flag are not different setup."""
    assert _prefix_variant_fires(
        [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
         ("Run actions/setup-node@v4", 15.0), ("Run npm ci", 50.0)]), "control"
    assert _prefix_variant_fires(
        [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
         ("Run actions/setup-node@v3", 15.0), ("Run npm ci", 50.0)]), "action version bump"
    assert _prefix_variant_fires(
        [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
         ("Run actions/setup-node@v4", 15.0),
         ("Run npm ci --prefer-offline --no-audit", 50.0)]), "extra command flags"


def test_opt77_still_splits_on_genuinely_different_setup_work():
    """The complement, and the reason the gate exists at all: a different
    toolchain, a different thing being installed, or an extra step are different
    setup work, and must not be merged into one credited group."""
    assert not _prefix_variant_fires(
        [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
         ("Run actions/setup-python@v5", 15.0),
         ("Run pip install -r requirements.txt", 50.0)]), "different toolchain"
    assert not _prefix_variant_fires(
        [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
         ("Run actions/setup-node@v4", 15.0),
         ("Run npm ci frontend/package.json", 50.0)]), "different install target"
    assert not _prefix_variant_fires(
        [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
         ("Run actions/cache@v4", 3.0), ("Run actions/setup-node@v4", 15.0),
         ("Run npm ci", 47.0)]), "an extra step is a different prefix"


# ==== the prefix is a SHAPE, and a zero-second step must not change it ====
#
# GitHub stamps step timestamps at ONE-SECOND granularity, so a step that takes
# under half a second is stamped start == end and measures 0s. `Set up job`, a
# warm `setup-node`, and a cache restore all do this — in some runs and not
# others, on byte-identical YAML. Reading the prefix off the TIMED steps only
# therefore made the prefix's SHAPE depend on jitter: the step vanished, the
# signature changed length, the "one stable prefix" gate saw two signatures and
# dropped the job, and the group died. Worse, a job whose `Set up job` is
# stably 1s while its siblings' are stably 0s was split from them forever.
#
# The complement fails the other way: a 0-second NON-setup step between two
# setup steps also vanished, so the prefix walk never broke on it and reported
# `checkout → npm ci` as contiguous when a `Run lint` sat between them.

def _jitter_job(name, steps, runner="ubuntu-latest"):
    """A job built from explicit (step name, duration) pairs — durations may be 0."""
    t = 0.0

    def _stamp(offset):
        m, s = divmod(int(offset), 60)
        return f"2026-06-01T00:{m:02d}:{s:02d}Z"

    out = []
    for step_name, dur in steps:
        out.append({"name": step_name, "number": len(out) + 1,
                    "started_at": _stamp(t), "completed_at": _stamp(t + dur)})
        t += dur
    return {"name": name, "started_at": _stamp(0), "completed_at": _stamp(t),
            "labels": [runner], "steps": out}


_JITTER_NAMES = ("lint", "typecheck", "audit")


def _jitter_case(per_job_setup_seconds):
    """Two sampled runs of three jobs; `per_job_setup_seconds[name]` is that job's
    `Set up job` duration in run 1 and run 2 respectively."""
    runs = []
    for run_i in range(2):
        jobs = []
        for n in _JITTER_NAMES:
            boot = per_job_setup_seconds[n][run_i]
            jobs.append(_jitter_job(n, [
                ("Set up job", boot),
                ("Run actions/checkout@v4", 10.0),
                ("Run actions/setup-node@v4", 15.0),
                ("Run npm ci", 55.0),
                ("Run tests", 10.0)]))
        runs.append(jobs)
    crit = _opt77_crit(names=_JITTER_NAMES, setup_s=80.0, work_s=10.0)
    return _opt77(jpr=runs, crit=crit, wf=_opt77_wf(names=_JITTER_NAMES))


def test_opt77_survives_one_second_of_step_timestamp_jitter():
    """Three cases that differ only in sub-second noise must all behave the same."""
    stable = {n: (1.0, 1.0) for n in _JITTER_NAMES}
    assert len(_jitter_case(stable)) == 1, "stable 1s boot"
    jitter = {n: (0.0, 1.0) for n in _JITTER_NAMES}
    assert len(_jitter_case(jitter)) == 1, "0s/1s boot jitter across runs"
    split = {"lint": (1.0, 1.0), "typecheck": (0.0, 0.0), "audit": (0.0, 0.0)}
    assert len(_jitter_case(split)) == 1, "one job stably 1s, siblings stably 0s"


def test_opt77_prefix_breaks_on_a_zero_second_non_setup_step():
    """A 0-second `Run lint` between checkout and `npm ci` ends the prefix. The
    catalog promises the credited prefix is CONTIGUOUS and leading; reporting
    `checkout → npm ci` across a work step in the middle breaks that promise."""
    job = _jitter_job("lint", [
        ("Set up job", 2.0), ("Run actions/checkout@v4", 3.0),
        ("Run lint", 0.0), ("Run npm ci", 60.0), ("Run tests", 5.0)])
    sig, shown, total = cr._leading_setup_prefix(job)
    assert list(sig) == ["set up job", "actions/checkout"], sig
    assert total == 5.0, total


def test_leading_setup_prefix_counts_only_the_leading_run():
    """Directly on the helper, and again through the detector: a setup-looking
    step AFTER the work has started is not part of the fixed prefix a
    consolidation pays once, so it must not be summed into it."""
    job = _jitter_job("lint", [
        ("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
        ("Run tests", 10.0), ("Run actions/cache@v4", 60.0)])
    sig, _shown, total = cr._leading_setup_prefix(job)
    assert total == 15.0, total
    assert list(sig) == ["set up job", "actions/checkout"], sig


def test_opt77_prefix_of_purely_human_authored_names_is_not_evidence():
    """A prefix with no recognizable action and no install command is not
    evidence that two jobs do the same setup work — and a group formed on the
    implicit `Set up job` alone shares nothing but runner boot."""
    runs = [[_jitter_job(n, [("Set up job", 60.0), ("Run tests", 10.0)])
             for n in _JITTER_NAMES] for _ in range(2)]
    crit = _opt77_crit(names=_JITTER_NAMES, setup_s=60.0, work_s=10.0)
    assert _opt77(jpr=runs, crit=crit, wf=_opt77_wf(names=_JITTER_NAMES)) == []


def test_opt77_withholds_a_setup_prefix_too_small_to_be_worth_consolidating():
    """Below an absolute floor the removed payments are noise against the cost of
    coupling N independently-re-runnable checks into one."""
    runs = [[_jitter_job(n, [("Set up job", 1.0), ("Run actions/checkout@v4", 2.0),
                             ("Run tests", 2.0)])
             for n in _JITTER_NAMES] for _ in range(2)]
    crit = _opt77_crit(names=_JITTER_NAMES, setup_s=3.0, work_s=2.0)
    assert _opt77(jpr=runs, crit=crit, wf=_opt77_wf(names=_JITTER_NAMES)) == []


def test_opt77_withholds_when_a_prefix_step_is_genuinely_absent_in_one_run():
    """The complement of the jitter fix: tolerating a 0-second step must not
    tolerate a step that is not there at all. A cache hit that skipped `npm ci`
    in run 2 means these jobs have no ONE prefix a consolidation would pay once."""
    full = [("Set up job", 2.0), ("Run actions/checkout@v4", 10.0),
            ("Run actions/setup-node@v4", 15.0), ("Run npm ci", 55.0),
            ("Run tests", 10.0)]
    hit = [("Set up job", 2.0), ("Run actions/checkout@v4", 10.0),
           ("Run actions/setup-node@v4", 15.0), ("Run tests", 10.0)]
    runs = [[_jitter_job(n, full) for n in _JITTER_NAMES],
            [_jitter_job(n, hit) for n in _JITTER_NAMES]]
    crit = _opt77_crit(names=_JITTER_NAMES, setup_s=82.0, work_s=10.0)
    assert _opt77(jpr=runs, crit=crit, wf=_opt77_wf(names=_JITTER_NAMES)) == []


def test_opt77_still_fires_when_the_group_shares_a_needs_parent_outside_it():
    """The independence gate must not be read as "any `needs:` disqualifies".
    `lint`, `typecheck` and `audit` all waiting on one `build` is the single most
    common real layout for exactly this shape: none of them is an ancestor of
    another, so consolidating them changes nothing about what runs when."""
    names = ("lint", "typecheck", "audit")
    wf = _opt77_wf(names=names)
    wf["jobs"]["build"] = {"runs-on": "ubuntu-latest"}
    for n in names:
        wf["jobs"][n]["needs"] = ["build"]
    out = _opt77(wf=wf)
    assert len(out) == 1, out
    assert sorted(out[0]["affected_jobs"]) == sorted(names)


def test_opt77_withholds_when_a_job_outside_the_group_waits_on_a_member():
    """A downstream dependent makes the consolidation wall-clock NEGATIVE, and the
    finding ships `wall_clock_p50_s = 0` with a neutrality certificate. The
    projection is always at least as tall as the tallest member, so every job that
    waits on a member starts later; the bare-duration comparison is only valid when
    nothing downstream is watching."""
    names = ("lint", "typecheck", "audit")
    wf = _opt77_wf(names=names)
    wf["jobs"]["deploy"] = {"runs-on": "ubuntu-latest", "needs": list(names)}
    assert _opt77(wf=wf) == []
    # …and transitively, through a job that is not a member either.
    wf2 = _opt77_wf(names=names)
    wf2["jobs"]["package"] = {"runs-on": "ubuntu-latest", "needs": ["lint"]}
    wf2["jobs"]["deploy"] = {"runs-on": "ubuntu-latest", "needs": ["package"]}
    assert _opt77(wf=wf2) == []


def test_opt77_withholds_on_a_dangling_needs_reference():
    """A `needs:` naming a job the workflow does not declare is dropped by the
    parent walk, which STRENGTHENS the independence claim on YAML nobody can
    reason about. Withhold instead."""
    names = ("lint", "typecheck", "audit")
    wf = _opt77_wf(names=names)
    wf["jobs"]["audit"]["needs"] = ["prepare"]      # no `prepare` job exists
    assert _opt77(wf=wf) == []


def test_opt77_withholds_when_the_yaml_setup_steps_diverge():
    """Two jobs can render the same step NAMES while doing different work: a
    named `Install dependencies` step with a different `working-directory`, a
    different `run:` body, or an unnamed `setup-python@v5` with a different
    `python-version:`. Consolidating removes NEITHER install, so the credit is
    wrong and the guardrail's "consolidate exactly these jobs" cannot be done."""
    names = ("lint", "typecheck", "audit")
    steps = [("Set up job", 5.0), ("Run actions/checkout@v4", 10.0),
             ("Run actions/setup-python@v5", 15.0), ("Run pip install -r r.txt", 50.0)]
    runs = [[_setup_job_named(n, steps, 10.0) for n in names] for _ in range(2)]
    crit = _opt77_crit(names=names, setup_s=80.0, work_s=10.0)

    def _wf_with(steps_by_job):
        wf = _opt77_wf(names=names)
        for k, v in steps_by_job.items():
            wf["jobs"][k]["steps"] = v
        return wf

    same = [{"uses": "actions/checkout@v4"},
            {"uses": "actions/setup-python@v5", "with": {"python-version": "3.12"}},
            {"run": "pip install -r r.txt"}]
    assert len(_opt77(jpr=runs, crit=crit,
                      wf=_wf_with({n: list(same) for n in names}))) == 1, "control"

    diverged_input = {n: list(same) for n in names}
    diverged_input["audit"] = [
        {"uses": "actions/checkout@v4"},
        {"uses": "actions/setup-python@v5", "with": {"python-version": "3.9"}},
        {"run": "pip install -r r.txt"}]
    assert _opt77(jpr=runs, crit=crit, wf=_wf_with(diverged_input)) == [], "action input"

    diverged_wd = {n: list(same) for n in names}
    diverged_wd["audit"] = [
        {"uses": "actions/checkout@v4"},
        {"uses": "actions/setup-python@v5", "with": {"python-version": "3.12"}},
        {"run": "pip install -r r.txt", "working-directory": "backend"}]
    assert _opt77(jpr=runs, crit=crit, wf=_wf_with(diverged_wd)) == [], "working-directory"

    diverged_run = {n: list(same) for n in names}
    diverged_run["audit"] = [
        {"uses": "actions/checkout@v4"},
        {"uses": "actions/setup-python@v5", "with": {"python-version": "3.12"}},
        {"run": "pip install -r requirements-dev.txt"}]
    assert _opt77(jpr=runs, crit=crit, wf=_wf_with(diverged_run)) == [], "run body"


def test_opt77_keeps_an_install_target_and_drops_only_bare_flags():
    """`npm ci -w frontend` and `npm ci -w backend` are different installs, not
    churn; `--prefer-offline` is churn. Flag stripping that ate the value token
    merged installs that remove nothing when consolidated."""
    ident = cr._setup_step_identity
    assert ident("Run npm ci --prefer-offline") == ident("Run npm ci")
    assert ident("Run npm ci --frozen-lockfile") == ident("Run npm ci")
    assert ident("Run pnpm install --filter a") != ident("Run pnpm install --filter b")
    assert ident("Run pnpm install --filter=a") != ident("Run pnpm install --filter=b")
    assert ident("Run npm ci -w frontend") != ident("Run npm ci -w backend")
    assert ident("Run pip install --target build") != ident("Run pip install --target dist")
    assert ident("Run actions/setup-node@v4") == ident("Run actions/setup-node@v3")


def test_setup_step_identity_never_strips_a_container_digest():
    """`@` plus `/` is not enough to call something an action ref: a pinned image
    digest has both, and cutting at the `@` erases exactly the part that says
    WHICH image."""
    ident = cr._setup_step_identity
    a = ident("Run docker pull ghcr.io/org/img@sha256:" + "a" * 64)
    b = ident("Run docker pull ghcr.io/org/img@sha256:" + "b" * 64)
    assert a != b
    assert "sha256" in a


def test_opt77_withholds_when_the_tallest_remaining_job_barely_ever_runs():
    """The whole neutrality proof rests on one job outside the group being taller.
    A conditional job that ran in a minority of the sampled runs cannot carry it:
    on the other runs the group's own members are the tallest thing left."""
    names = ("lint", "typecheck", "audit")
    tall = _jitter_job("release-notes", [("Set up job", 5.0), ("Run tests", 535.0)])
    small = [_setup_job(n, 80.0, 10.0) for n in names]
    job_p50 = dict({n: 90.0 for n in names}, **{"release-notes": 540.0})
    crit = {"floor_p50": 90.0, "long_pole_p50": 540.0, "job_p50": job_p50,
            "job_runner": dict({n: "ubuntu-latest" for n in names},
                               **{"release-notes": "ubuntu-latest"}),
            "runner_scope": "ubuntu-latest"}
    wf = _opt77_wf(names=names)
    wf["jobs"]["release-notes"] = {"runs-on": "ubuntu-latest"}
    # Present in 1 of 4 sampled runs: a minority.
    runs = [list(small) + [tall]] + [list(small) for _ in range(3)]
    assert _opt77(jpr=runs, crit=crit, wf=wf) == []
    # Present throughout, the same group is credited.
    assert len(_opt77(jpr=[list(small) + [tall] for _ in range(4)],
                      crit=crit, wf=wf)) == 1


# ==== a silent detector and a dead one look identical ====
#
# OPT77 has around thirty withhold points, and every "withholds" test asserts
# `== []` — exactly what a detector broken into never firing also returns. Two
# such bugs have already shipped on this lever. The gate that stopped a group is
# counted and logged, so a zero firing rate is visible in the artifact instead of
# being inferred from an absence.

def test_opt77_names_the_gate_that_withheld_each_group():
    names = ("lint", "typecheck", "audit")
    counts = {}
    wf = _opt77_wf(names=names)
    wf["jobs"]["deploy"] = {"runs-on": "ubuntu-latest", "needs": list(names)}
    assert cr._detect_opt77_repeated_setup_across_small_jobs(
        "ci.yml", [_opt77_run(), _opt77_run()], _opt77_crit(), wf, 100, 0,
        withheld=counts) == []
    assert counts.get("downstream_job_needs_a_member") == 1, counts

    counts = {}
    two = ("lint", "typecheck")
    assert cr._detect_opt77_repeated_setup_across_small_jobs(
        "ci.yml", [_opt77_run(names=two), _opt77_run(names=two)],
        _opt77_crit(names=two), _opt77_wf(names=two), 100, 0,
        withheld=counts) == []
    assert counts.get("fewer_than_min_jobs_share_the_prefix") == 1, counts

    counts = {}
    assert cr._detect_opt77_repeated_setup_across_small_jobs(
        "ci.yml", [_opt77_run(), _opt77_run()], _opt77_crit(), {}, 100, 0,
        withheld=counts) == []
    assert counts.get("workflow_yaml_unparsed") == 1, counts

    # A group that fires records nothing.
    counts = {}
    assert len(cr._detect_opt77_repeated_setup_across_small_jobs(
        "ci.yml", [_opt77_run(), _opt77_run()], _opt77_crit(), _opt77_wf(),
        100, 0, withheld=counts)) == 1
    assert counts == {}, counts


def test_opt77_withhold_counts_reach_the_findings_document():
    """The counter is only useful if it is stamped where a reader can see it."""
    import inspect
    src = inspect.getsource(cr.collect)
    assert "opt77_withheld_by_gate" in src


# ==== the detector and its verifier arm, coupled ====
#
# The verifier re-derives OPT77's saving and margin from the stamped block. A
# hand-written block proves only that the verifier reads what the test wrote; the
# coupling that matters is that what the DETECTOR stamps survives it.

def _opt77_findings_doc(out, crit, wf_path="ci.yml"):
    return {"findings": out,
            "per_workflow_timing": {wf_path: {"job_p50": dict(crit["job_p50"])}}}


def test_opt77_detector_output_passes_its_own_verifier_arm():
    import verify_report as vr
    names = ("lint", "typecheck", "audit")
    crit = _opt77_crit(names=names)
    out = _opt77(crit=crit, wf=_opt77_wf(names=names))
    assert len(out) == 1
    margin, problems = vr._opt77_consolidation_rederived(
        out[0], _opt77_findings_doc(out, crit))
    assert problems == [], problems
    assert margin == out[0]["tier2_neutrality"]["margin_s"]


def test_opt77_verifier_catches_a_credited_job_with_a_different_prefix():
    """The grouping is the whole saving model, and until each job stamped its OWN
    measured prefix the verifier could only check that the group's list was
    non-empty — it had to take on faith that all N jobs re-pay it."""
    import verify_report as vr
    names = ("lint", "typecheck", "audit")
    crit = _opt77_crit(names=names)
    out = _opt77(crit=crit, wf=_opt77_wf(names=names))
    f = out[0]
    f["setup_consolidation"]["per_job"]["audit"]["setup_steps"] = [
        "set up job", "actions/setup-python"]
    _margin, problems = vr._opt77_consolidation_rederived(
        f, _opt77_findings_doc(out, crit))
    assert any("is not the credited shared prefix" in p for p in problems), problems


def test_opt77_verifier_bounds_occurrences_by_the_sampled_run_count():
    """`occurrences` multiplies the entire saving and nothing else bounds it."""
    import verify_report as vr
    names = ("lint", "typecheck", "audit")
    crit = _opt77_crit(names=names)
    out = _opt77(crit=crit, wf=_opt77_wf(names=names))
    f = out[0]
    f["setup_consolidation"]["occurrences"] = 40
    _margin, problems = vr._opt77_consolidation_rederived(
        f, _opt77_findings_doc(out, crit))
    assert any("exceeds the" in p and "sampled run" in p for p in problems), problems


def test_opt77_verifier_rejects_a_tallest_remaining_job_from_outside_the_stated_set():
    """The tallest-remaining job must come from the set the detector said it chose
    from, and every job outside the group must be either in that set or excluded
    with a reason."""
    import verify_report as vr
    names = ("lint", "typecheck", "audit")
    crit = _opt77_crit(names=names)
    out = _opt77(crit=crit, wf=_opt77_wf(names=names))
    f = out[0]
    f["setup_consolidation"]["remaining_eligible_jobs"] = []
    _margin, problems = vr._opt77_consolidation_rederived(
        f, _opt77_findings_doc(out, crit))
    assert any("remaining_eligible_jobs missing" in p for p in problems), problems

    f = _opt77(crit=crit, wf=_opt77_wf(names=names))[0]
    f["setup_consolidation"]["remaining_eligible_jobs"] = ["lint"]
    _margin, problems = vr._opt77_consolidation_rederived(
        f, _opt77_findings_doc([f], crit))
    assert any("credited group members" in p for p in problems), problems


def test_opt77_verifier_checks_the_restated_saving_numbers():
    """The block restates the saving, its sampled basis and its scale factor.
    Numbers nobody reads drift."""
    import verify_report as vr
    names = ("lint", "typecheck", "audit")
    crit = _opt77_crit(names=names)
    for field, bogus, needle in (("runner_min_saving", 9999.0, "runner_min_saving"),
                                 ("sampled_saved_s", 1.0, "sampled_saved_s"),
                                 ("scale", 999.0, "scale")):
        f = _opt77(crit=crit, wf=_opt77_wf(names=names))[0]
        f["setup_consolidation"][field] = bogus
        _margin, problems = vr._opt77_consolidation_rederived(
            f, _opt77_findings_doc([f], crit))
        assert any(needle in p for p in problems), (field, problems)


# ==== a dropped lever has to be visible somewhere ====
#
# Superseding OPT65 deliberately loses real minutes: its billing round-up for the
# overlapping jobs goes unreported so one edit renders as one lever. Nothing said
# so anywhere a reader could look, and the overlap is tested on INTERSECTION, so
# a four-leg OPT65 can be dropped for a three-job OPT77 with the fourth leg's
# round-up disappearing without a word.

def test_supersede_discloses_every_dropped_lever_and_what_it_gave_up():
    o65 = {"id": "f1", "pattern": "OPT65", "workflow_file": "ci.yml",
           "affected_jobs": ["lint (a)", "lint (b)", "lint (c)", "lint (d)"]}
    o77 = {"id": "f2", "pattern": "OPT77", "workflow_file": "ci.yml",
           "affected_jobs": ["lint (a)", "lint (b)", "lint (c)"]}
    disclosure = []
    kept = cr._supersede_opt65_with_opt77([o65, o77], disclosure=disclosure)
    assert [f["id"] for f in kept] == ["f2"]
    assert len(disclosure) == 1, disclosure
    d = disclosure[0]
    assert d["id"] == "f1"
    assert d["pattern"] == "OPT65"
    assert d["workflow_file"] == "ci.yml"
    assert d["superseded_by"] == ["f2"]
    # The leg the consolidation does NOT cover is named: its round-up minutes are
    # real and go unreported.
    assert d["unreported_jobs"] == ["lint (d)"]
    assert "one lever" in d["reason"]


def test_supersede_discloses_nothing_when_it_drops_nothing():
    o65 = {"id": "f1", "pattern": "OPT65", "workflow_file": "release.yml",
           "affected_jobs": ["unit (3.9)", "unit (3.12)"]}
    o77 = {"id": "f2", "pattern": "OPT77", "workflow_file": "ci.yml",
           "affected_jobs": ["lint", "typecheck", "audit"]}
    disclosure = []
    assert len(cr._supersede_opt65_with_opt77([o65, o77], disclosure=disclosure)) == 2
    assert disclosure == []


def test_supersede_disclosure_reaches_the_findings_document():
    import inspect
    assert "superseded_findings" in inspect.getsource(cr.collect)


def test_verifier_rejects_a_suppression_with_no_surviving_consolidation():
    """The trade — lose OPT65's minutes so one edit renders once — is only
    defensible while the lever that replaced it is actually in the report."""
    import verify_report as vr
    data = {"findings": [{"id": "f2", "pattern": "OPT77", "workflow_file": "ci.yml"}],
            "superseded_findings": [
                {"id": "f1", "pattern": "OPT65", "workflow_file": "ci.yml",
                 "superseded_by": ["f2"], "unreported_jobs": []}]}
    assert vr._opt65_suppressions_are_accounted_for(data) == []

    orphan = {"findings": [],
              "superseded_findings": [
                  {"id": "f1", "pattern": "OPT65", "workflow_file": "ci.yml",
                   "superseded_by": ["f2"], "unreported_jobs": []}]}
    problems = vr._opt65_suppressions_are_accounted_for(orphan)
    assert any("no such OPT77 consolidation survives" in p for p in problems), problems

    unnamed = {"findings": [],
               "superseded_findings": [
                   {"id": "f1", "pattern": "OPT65", "workflow_file": "ci.yml"}]}
    problems = vr._opt65_suppressions_are_accounted_for(unnamed)
    assert any("without naming what superseded it" in p for p in problems), problems

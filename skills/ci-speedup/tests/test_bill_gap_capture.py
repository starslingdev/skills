from __future__ import annotations

import json
from pathlib import Path

import blocking_path as bp
import collect_runs as cr


def _row(
    workflow: str,
    job: str,
    billable: float,
    *,
    raw: float | None = None,
    # `sku` / `usd` are accepted but inert: pricing was excised (2026-07-20) and the
    # spine row no longer carries sku/usd/weighted/billing_class. Bill-gap matching is
    # workflow + jobs, so leaving these as ignored kwargs keeps call sites unchanged.
    sku: str = "linux_2_core",
    usd: float | None = 0.08,
    event_scope: str = "all-events",
    status_filter: str = "success",
    attempt_filter: str = "latest",
    volume_filter: str = "all-status",
) -> dict:
    return {
        "workflow_file": workflow,
        "job_name": job,
        "runner_label": "ubuntu-latest",
        "event_scope": event_scope,
        "status_filter": status_filter,
        "attempt_filter": attempt_filter,
        "volume_filter": volume_filter,
        "sample_window_start": "2026-07-01T00:00:00Z",
        "sample_window_end": "2026-07-07T00:00:00Z",
        "sampled_workflow_run_count": 10,
        "sampled_job_occurrence_count": 10,
        "occurrence_fraction": 1.0,
        "workflow_30d_volume": 30,
        "effective_monthly_job_volume": 30.0,
        "mean_sampled_compute_seconds": 120.0,
        "mean_sampled_billable_equiv_minutes": 2.0,
        "raw_compute_runner_min_per_month": raw if raw is not None else billable,
        "billable_equiv_min_per_month": billable,
    }


def _doc(rows: list[dict], findings: list[dict] | None = None) -> dict:
    return {
        "repo": "owner/repo",
        "commit_sha": "abc123",
        "skill_commit_sha": "def456",
        "scanned_at": "2026-07-07T00:00:00Z",
        "data_sources": {
            "skill_commit_sha": "def456",
            "sampled_runs_created_before": "2026-07-07T00:00:00Z",
            "tiers_run": ["gh-timing"],
            "cost_spine_job_fetch_failures": 0,
        },
        "findings": findings or [],
        "runner_minute_spine": {
            "render_ready": True,
            "rows": rows,
            "totals": {"row_count": len(rows)},
        },
    }


def _source_backed_finding(workflow: str, job: str, saving: float) -> dict:
    return {
        "pattern": "OPT46",
        "workflow_file": workflow,
        "affected_jobs": [job],
        "sizing_basis": "measured",
        "runner_min_saving": saving,
        "tier2_neutrality": {"class": "post_completion_waste"},
    }


def _measured_finding(
    workflow: str,
    saving: float,
    *,
    pattern: str,
    job: str | None = None,
    rerun_dominant_job: str | None = None,
    source_filter: dict | None = None,
) -> dict:
    out = {
        "pattern": pattern,
        "workflow_file": workflow,
        "sizing_basis": "measured",
        "runner_min_saving": saving,
        "tier2_neutrality": {"class": "post_completion_waste"},
    }
    if job:
        out["affected_jobs"] = [job]
    if rerun_dominant_job:
        out["rerun_dominant_job"] = rerun_dominant_job
    if pattern == "OPT64":
        # A rankable OPT64 rests on the minimum sample with no pass on a retry.
        out["rerun_sample"] = {"retried_runs": 3, "passed_on_retry": 0,
                               "min_retried_runs": 3}
    if source_filter:
        out["runner_minute_source_filter"] = source_filter
    return out


def test_bill_gap_candidates_rank_uncovered_workflows_and_skip_source_backed() -> None:
    rows = [
        _row("expensive.yml", "build", 250.0),
        _row("expensive.yml", "test", 50.0),
        _row("covered.yml", "covered-job", 180.0, raw=200.0, usd=1.0),
        _row("small.yml", "lint", 30.0),
    ]
    findings = [_source_backed_finding("covered.yml", "covered-job", 40.0)]

    candidates = cr._bill_gap_candidates_from_doc(_doc(rows, findings), cap=10)

    assert [c["workflow_file"] for c in candidates] == ["expensive.yml", "small.yml"]
    assert candidates[0]["billable_equiv_min_per_month"] == 300.0
    assert [j["job_name"] for j in candidates[0]["top_jobs"]] == ["build", "test"]
    assert candidates[0]["coverage_reason"] == (
        "no source-backed Tier-2 finding covers this workflow")


def test_bill_gap_source_backed_exclusion_matches_renderer_predicate() -> None:
    rows = [
        _row("covered.yml", "covered-job", 180.0, raw=200.0, usd=1.0),
        _row("unbacked.yml", "expensive-job", 20.0, raw=20.0, usd=0.1),
    ]
    findings = [
        _source_backed_finding("covered.yml", "covered-job", 40.0),
        _source_backed_finding("unbacked.yml", "expensive-job", 200.0),
    ]
    doc = _doc(rows, findings)

    renderer_backed = {
        f["workflow_file"]
        for f in findings
        if bp._is_tier2_source_backed_finding(f, doc)
    }

    assert cr._bill_gap_source_backed_workflows(findings, rows) == renderer_backed
    assert renderer_backed == {"covered.yml"}


def test_z_bill_gap_opt64_wide_binding_and_group_guard_match_renderer() -> None:
    """PR-Z (S2-1 lockstep pin): the bill-gap copy of the OPT64 binding must
    agree with the renderer twin on BOTH new PR-S2 behaviors — the wide
    prior-row cover (a whole-run claim only the FULL prior set can cover) and
    the sibling no-double-count guard (a group over-claim disqualifies every
    sibling). Reverting either bill-gap half must fail this parity, not just an
    implementation-detail unit test."""
    prior = {"status_filter": "all-status", "attempt_filter": "prior"}
    # Wide cover: the dominant job's own prior row (30 raw) cannot cover the
    # 100 min/mo whole-run claim; the full prior-attempt set (30+90+60) can.
    wide_rows = [
        _row("retry.yml", "flaky", 40.0, raw=30.0, **prior),
        _row("retry.yml", "sib-linux", 110.0, raw=90.0, **prior),
        _row("retry.yml", "sib-mac", 80.0, raw=60.0, **prior),
    ]
    wide_finding = _measured_finding("retry.yml", 100.0, pattern="OPT64",
                                     rerun_dominant_job="flaky")
    wide_doc = _doc(wide_rows, [wide_finding])
    assert bp._is_tier2_source_backed_finding(wide_finding, wide_doc)
    assert cr._bill_gap_source_backed_workflows(
        [wide_finding], wide_rows) == {"retry.yml"}

    # Group over-claim: each sibling alone fits under the shared 150-raw
    # cover, combined 200 does not — NEITHER twin may call it covered.
    group_rows = [
        _row("retry.yml", "flaky-a", 100.0, raw=80.0, usd=0.6, **prior),
        _row("retry.yml", "flaky-b", 90.0, raw=70.0, usd=0.54, **prior),
    ]
    sib_a = _measured_finding("retry.yml", 100.0, pattern="OPT64",
                              rerun_dominant_job="flaky-a")
    sib_b = _measured_finding("retry.yml", 100.0, pattern="OPT64",
                              rerun_dominant_job="flaky-b")
    # Distinct lines: real detector siblings never share a location, and the
    # renderer's location dedupe would otherwise collapse the fixtures.
    sib_a["line"], sib_b["line"] = 21, 22
    siblings = [sib_a, sib_b]
    group_doc = _doc(group_rows, siblings)
    assert not any(bp._is_tier2_source_backed_finding(f, group_doc)
                   for f in siblings)
    assert cr._bill_gap_source_backed_workflows(siblings, group_rows) == set()


def test_bill_gap_source_backed_parity_covers_special_filter_paths() -> None:
    rows = [
        _row(
            "retry.yml",
            "retry job",
            30.0,
            raw=30.0,
            usd=1.0,
            status_filter="all-status",
            attempt_filter="prior",
        ),
        _row("rounding.yml", "tiny leg", 10.0, raw=1.0, usd=1.0),
        _row("schedule.yml", "cron", 40.0, raw=40.0, usd=1.0, event_scope="schedule"),
        _row("conflict.yml", "conflict", 40.0, raw=40.0, usd=1.0),
    ]
    findings = [
        _measured_finding(
            "retry.yml",
            10.0,
            pattern="OPT64",
            rerun_dominant_job="retry job",
        ),
        _measured_finding("rounding.yml", 8.0, pattern="OPT65", job="tiny leg"),
        _measured_finding(
            "schedule.yml",
            10.0,
            pattern="OPT36",
            job="cron",
            source_filter={"event_scope": "schedule"},
        ),
        _measured_finding(
            "conflict.yml",
            10.0,
            pattern="OPT36",
            job="conflict",
            source_filter={"status_filter": "all-status"},
        ),
    ]
    doc = _doc(rows, findings)

    renderer_backed = {
        f["workflow_file"]
        for f in findings
        if bp._is_tier2_source_backed_finding(f, doc)
    }

    assert cr._bill_gap_source_backed_workflows(findings, rows) == renderer_backed
    assert renderer_backed == {"retry.yml", "rounding.yml", "schedule.yml"}


def test_bill_gap_candidates_require_render_ready_spine() -> None:
    doc = _doc([_row("expensive.yml", "build", 250.0)])
    doc["runner_minute_spine"]["render_ready"] = False

    assert cr._bill_gap_candidates_from_doc(doc) == []


def test_bill_gap_candidates_reject_stale_static_only_spine() -> None:
    doc = _doc([_row("ci.yml", "build", 200.0)])
    doc["data_sources"] = {
        "tiers_run": [],
        "gh_available": False,
        "partial_reason": "no --repo supplied; static-only run",
    }

    assert cr._bill_gap_candidates_from_doc(doc) == []

    doc = _doc([_row("ci.yml", "build", 200.0)])
    doc["data_sources"].pop("cost_spine_job_fetch_failures")

    assert cr._bill_gap_candidates_from_doc(doc) == []

    doc = _doc([_row("expensive.yml", "build", 250.0)])
    doc["runner_minute_spine"].pop("totals")

    assert cr._bill_gap_candidates_from_doc(doc) == []


def test_bill_gap_capture_writes_namespaced_local_artifacts(tmp_path: Path) -> None:
    rows = [_row("ci.yml", "build", 200.0)]
    captured = cr._capture_bill_gap_workflows(_doc(rows), gaps_root=tmp_path, cap=1)

    assert len(captured) == 1
    dest = captured[0]
    assert dest.parent == tmp_path
    assert dest.name == "owner-repo__ci.yml"
    assert not (dest / "job.log").exists()

    artifact = json.loads((dest / "bill-gap.json").read_text(encoding="utf-8"))
    assert artifact["schema"] == cr._BILL_GAP_SCHEMA
    assert artifact["source"] == "runner_minute_spine"
    assert artifact["candidate"]["workflow_file"] == "ci.yml"
    assert "not a detector" in artifact["promotion_contract"]
    assert (dest / "README.md").exists()


def test_bill_gap_capture_does_not_publish_partial_directory_on_write_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original = Path.write_text

    def fail_readme(path: Path, *args, **kwargs):
        if path.name == "README.md":
            raise OSError("simulated README failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_readme)

    captured = cr._capture_bill_gap_workflows(
        _doc([_row("ci.yml", "build", 200.0)]), gaps_root=tmp_path, cap=1)

    assert captured == []
    assert not (tmp_path / "owner-repo__ci.yml").exists()
    assert not list(tmp_path.glob(".owner-repo__ci.yml.tmp-*"))


def test_bill_gap_capture_skips_installed_copy_when_default_root_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cr, "_bill_gap_root_default", lambda: None)

    captured = cr._capture_bill_gap_workflows(
        _doc([_row("ci.yml", "build", 200.0)]), gaps_root=None, cap=1)

    assert captured == []
    assert not any(tmp_path.iterdir())


def test_bill_gap_default_root_uses_repo_gap_namespace(monkeypatch) -> None:
    monkeypatch.setattr(cr, "_bill_gap_is_maintainer_source", lambda: True)
    monkeypatch.setattr(
        cr.subprocess,
        "run",
        lambda *_a, **_k: cr.subprocess.CompletedProcess([], 0, "/repo/root\n"),
    )

    assert cr._bill_gap_root_default() == (
        Path("/repo/root") / ".ci-speedup-gaps" / "bill-workflows")

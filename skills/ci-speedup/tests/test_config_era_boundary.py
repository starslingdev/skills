"""Regression tests for issue #66 — a sample straddling a workflow-config change must NEVER
blend the two CI configurations (a stale headline, a fabricated cross-era drill, a recoverable
ceiling above the typical wait).

The class (internal-dev-repo re-audit, the second-run journey audit -> fix -> re-audit): the
collector sampled PRs from BOTH sides of the `ci.yml` change, drilled a PRE-change run for `test`
(the full guard step) and a POST-change run for `guard shard 3/4` (the quarter slice), and
synthesized a FABRICATED "guard runs twice, once whole and once sharded" redundancy that no PR ever
ran. The headline also strained: a `2m46s` typical wait (post-fix population) beside `~6m28s
recoverable` (pre-fix drill) — a ceiling >2x the wait it recovers.

Two fixes, one PR:
  1. **Config-era boundary in the collector** (`collect_runs._workflow_change_boundary` +
     `_partition_config_era`): detect each workflow file's last-change commit; partition its sampled
     runs at that boundary; keep the POST-change era when it is sufficient (narrowed window) else the
     PRE-change era WITH a prominent disclosure. Either way ONE era survives per workflow, so a drill
     can't blend eras. Stamped in `pr_critical_path.config_eras`; `blocking_path` renders the
     disclosure; `verify_report.check_config_era_boundary` re-derives the bind.
  2. **Recoverable-within-wait coherence** (`blocking_path._recoverable_reconciliation`): a rendered
     recoverable ceiling above the typical wait co-renders the slow-mode/worst-case reconciliation;
     `verify_report.check_recoverable_within_wait` is the bounds-family guard.

Run: pytest -v skills/ci-speedup/tests/test_config_era_boundary.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_TESTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import verify_report as vr  # noqa: E402


# ── collect_runs: boundary detection (ONE commits?path= call per workflow) ────────────────────────

class _FakeClient:
    """Records every endpoint and serves a scripted `commits?path=` reply."""
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.endpoints: list[str] = []

    def json(self, endpoint: str, allow_missing: bool = False) -> object:
        self.endpoints.append(endpoint)
        return self.reply


# NOTE (issue #77): `_workflow_change_boundary` now returns a 4-tuple
# `(last, prev, last_sha, prev_sha)` — the two commit SHAs are needed to fetch the pre/post workflow
# BLOBS for content-era classification. These assertions were updated from the pre-#77 2-tuple.
def test_boundary_reads_the_last_change_commit_date_with_one_call():
    reply = [{"sha": "cafe1", "commit": {"committer": {"date": "2026-07-15T09:00:00Z"},
                                         "author": {"date": "2026-07-14T00:00:00Z"}}}]
    c = _FakeClient(reply)
    last, prev, last_sha, prev_sha = cr._workflow_change_boundary(
        c, "acme/app", ".github/workflows/ci.yml", "2026-07-18T00:00:00Z")
    assert last == "2026-07-15T09:00:00Z"          # committer date preferred
    assert prev is None                            # only one commit in the reply
    assert last_sha == "cafe1" and prev_sha is None  # #77: the boundary commit SHA (POST-blob ref)
    # Exactly ONE call, the commits?path= lookup, pinned with until=<canonical pin>.
    assert len(c.endpoints) == 1
    ep = c.endpoints[0]
    assert ep.startswith("repos/acme/app/commits?")
    assert "path=.github/workflows/ci.yml" in ep and "per_page=2" in ep
    assert "until=2026-07-18T00:00:00Z" in ep


def test_boundary_reads_the_prior_boundary_for_a_twice_changed_workflow():
    # per_page=2 returns the two most-recent commits touching the file → (last, prev) + their SHAs.
    reply = [{"sha": "s_last", "commit": {"committer": {"date": "2026-07-15T09:00:00Z"}}},
             {"sha": "s_prev", "commit": {"committer": {"date": "2026-07-08T09:00:00Z"}}}]
    last, prev, last_sha, prev_sha = cr._workflow_change_boundary(_FakeClient(reply), "r", "w", None)
    assert last == "2026-07-15T09:00:00Z" and prev == "2026-07-08T09:00:00Z"
    assert last_sha == "s_last" and prev_sha == "s_prev"


def test_boundary_falls_back_to_author_date_and_returns_none_on_empty():
    c = _FakeClient([{"sha": "s0", "commit": {"author": {"date": "2026-07-10T00:00:00Z"}}}])
    assert cr._workflow_change_boundary(c, "r", "w", None) == ("2026-07-10T00:00:00Z", None, "s0", None)
    for empty in ([], None, "not-a-list", [{}], [{"commit": {}}]):
        assert cr._workflow_change_boundary(_FakeClient(empty), "r", "w", None) == (
            None, None, None, None)


# ── collect_runs: the partition rules (both branches + the no-op) ─────────────────────────────────

def _runs(*created_ats: str) -> list[dict]:
    return [{"created_at": ts, "head_sha": f"sha{i}", "event": "pull_request"}
            for i, ts in enumerate(created_ats)]


def test_partition_no_boundary_and_no_straddle_are_byte_identical_noops():
    runs = _runs("2026-07-16T00:00:00Z", "2026-07-17T00:00:00Z")
    # No boundary → identity (same object), so downstream rendering is byte-identical.
    kept, fact = cr._partition_config_era(runs, None)
    assert kept is runs and fact is None
    # Boundary older than every run (all runs POST-date it) → no straddle → identity.
    kept, fact = cr._partition_config_era(runs, "2026-07-01T00:00:00Z")
    assert kept is runs and fact is None
    # Boundary newer than every run (all runs PRE-date it) → no straddle → identity.
    kept, fact = cr._partition_config_era(runs, "2026-08-01T00:00:00Z")
    assert kept is runs and fact is None


def test_partition_straddle_sufficient_post_uses_post_only():
    # 2 pre-change + 6 post-change (>= _RARE_PRESENCE_MIN_PR) → keep POST only.
    pre = ["2026-07-10T00:00:00Z", "2026-07-11T00:00:00Z"]
    post = [f"2026-07-1{d}T00:00:00Z" for d in range(4, 10)]  # 6 runs, all >= boundary
    runs = _runs(*pre, *post)
    kept, fact = cr._partition_config_era(runs, "2026-07-14T00:00:00Z")
    assert fact is not None
    assert fact["rule"] == "post_only" and fact["kept_era"] == "post"
    assert fact["pre_count"] == 2 and fact["post_count"] == 6
    assert all(r["created_at"] >= "2026-07-14T00:00:00Z" for r in kept)
    assert len(kept) == 6


def test_partition_straddle_insufficient_post_keeps_pre_and_flags_disclosure():
    # 7 pre-change + 2 post-change (< _RARE_PRESENCE_MIN_PR) → keep PRE only, flag disclosure.
    pre = [f"2026-07-0{d}T00:00:00Z" for d in range(1, 8)]  # 7 runs
    post = ["2026-07-16T00:00:00Z", "2026-07-17T00:00:00Z"]
    runs = _runs(*pre, *post)
    kept, fact = cr._partition_config_era(runs, "2026-07-14T00:00:00Z")
    assert fact is not None
    assert fact["rule"] == "disclosed_pre" and fact["kept_era"] == "pre"
    assert fact["pre_count"] == 7 and fact["post_count"] == 2
    assert all(r["created_at"] < "2026-07-14T00:00:00Z" for r in kept)
    assert len(kept) == 7
    # In BOTH straddle branches exactly one era survives — a drill can never blend eras.
    assert fact["sufficiency_min"] == cr._RARE_PRESENCE_MIN_PR
    # Single-change disclosed_pre: multi_change is False, the whole pre set is kept.
    assert fact["multi_change"] is False and fact["kept_count"] == 7


def test_partition_multi_boundary_disclosed_pre_narrows_to_the_immediately_prior_era():
    """Issue #66 multi-boundary: a workflow changed TWICE in the window (prev @07-05, last @07-14)
    with a thin post sample → disclosed_pre. The pre-side runs span the two OLDER eras; the kept
    set must narrow to the single `[prev, last)` era so the drill never blends the two."""
    era1 = ["2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", "2026-07-03T00:00:00Z"]  # oldest era
    era2 = ["2026-07-06T00:00:00Z", "2026-07-08T00:00:00Z",
            "2026-07-10T00:00:00Z", "2026-07-12T00:00:00Z"]                          # prior era
    post = ["2026-07-16T00:00:00Z", "2026-07-17T00:00:00Z"]                          # thin (<6)
    runs = _runs(*era1, *era2, *post)
    kept, fact = cr._partition_config_era(runs, "2026-07-14T00:00:00Z", "2026-07-05T00:00:00Z")
    assert fact["rule"] == "disclosed_pre" and fact["kept_era"] == "pre"
    assert fact["multi_change"] is True
    # ONLY the era2 runs survive — none of the oldest-era runs, none of the post runs.
    assert all("2026-07-05T00:00:00Z" <= r["created_at"] < "2026-07-14T00:00:00Z" for r in kept)
    assert len(kept) == 4 and fact["kept_count"] == 4
    assert fact["pre_count"] == 7   # pre_count still records the FULL pre side, for honesty


def test_partition_excludes_runs_with_missing_created_at_from_both_eras(tmp_path):
    """A run with no `created_at` can't be placed in an era — it must NOT silently land in the pre
    bucket (an empty string sorts before every ISO date). It is excluded from both sides."""
    pre = ["2026-07-10T00:00:00Z", "2026-07-11T00:00:00Z"]
    post = [f"2026-07-1{d}T00:00:00Z" for d in range(4, 10)]  # 6 → post_only
    runs = _runs(*pre, *post)
    runs.append({"head_sha": "shaX", "event": "pull_request"})   # NO created_at
    kept, fact = cr._partition_config_era(runs, "2026-07-14T00:00:00Z")
    # The malformed run counts toward neither era and is dropped from the kept (post) set.
    assert fact["rule"] == "post_only" and fact["pre_count"] == 2 and fact["post_count"] == 6
    assert all(r.get("created_at") for r in kept) and len(kept) == 6


def test_partition_prev_boundary_outside_window_is_a_noop_narrow():
    """The common single-change case: `prev_boundary` predates every sampled run, so the narrow
    keeps the whole pre set — byte-identical to the un-narrowed disclosed_pre path."""
    pre = [f"2026-07-0{d}T00:00:00Z" for d in range(1, 8)]   # 7 runs
    post = ["2026-07-16T00:00:00Z", "2026-07-17T00:00:00Z"]
    runs = _runs(*pre, *post)
    kept, fact = cr._partition_config_era(runs, "2026-07-14T00:00:00Z", "2026-06-01T00:00:00Z")
    assert fact["rule"] == "disclosed_pre" and fact["multi_change"] is False
    assert len(kept) == 7 and fact["kept_count"] == 7


def test_partition_multi_boundary_narrow_empties_falls_back_to_full_pre():
    """`prev_boundary` NEWER than every pre-change run → the narrow would empty the kept set, so we
    fall back to the FULL pre set (disclosed but NOT flagged multi_change) rather than hand the drill
    zero runs. Guards the `narrowed and ...` truthiness check in the fallback."""
    pre = [f"2026-07-0{d}T00:00:00Z" for d in range(1, 8)]   # 7 runs, all < 07-13
    post = ["2026-07-16T00:00:00Z", "2026-07-17T00:00:00Z"]  # thin (<6)
    runs = _runs(*pre, *post)
    kept, fact = cr._partition_config_era(runs, "2026-07-14T00:00:00Z", "2026-07-13T00:00:00Z")
    assert fact["rule"] == "disclosed_pre" and fact["kept_era"] == "pre"
    assert fact["multi_change"] is False        # an empty narrow is not a "multi" narrow
    assert len(kept) == 7 and fact["kept_count"] == 7   # full pre, never the empty set


def test_engine_renders_multi_change_pre_disclosure_clause(tmp_path):
    """The renderer's multi-boundary clause (the honesty reassurance that the pre-side is NOT itself
    a blend of older configs) renders only when `multi_change` is stamped."""
    doc = _era_doc()
    doc["pr_critical_path"]["config_eras"] = [{
        "workflow_file": ".github/workflows/ci.yml", "boundary": "2026-07-17T00:00:00Z",
        "kept_era": "pre", "rule": "disclosed_pre", "pre_count": 7, "post_count": 2,
        "sufficiency_min": 6, "multi_change": True}]
    report = bp.render(doc)
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in report
    assert "also changed earlier in the window" in report and "not a blend" in report
    # A single-change disclosed_pre (no multi_change) must NOT render the clause.
    doc["pr_critical_path"]["config_eras"][0]["multi_change"] = False
    assert "also changed earlier in the window" not in bp.render(doc)


def test_config_era_guard_fails_on_spine_only_pre_era_without_disclosure(tmp_path):
    """Guard's spine-only FAIL branch: a disclosed_pre era whose workflow binds NO drilled pole —
    the spine still owes the disclosure, so a report lacking the marker FAILs."""
    doc = _era_doc()
    doc["pr_critical_path"]["config_eras"] = [{
        "workflow_file": ".github/workflows/other.yml", "boundary": "2026-07-17T00:00:00Z",
        "kept_era": "pre", "rule": "disclosed_pre", "pre_count": 7, "post_count": 2,
        "sufficiency_min": 6}]   # the only pole is on ci.yml, so nothing binds other.yml
    chk = vr.check_config_era_boundary("# x no disclosure\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "other.yml" in chk.detail


# ── blocking_path: the recoverable-within-wait reconciliation helper ──────────────────────────────

def test_reconciliation_fires_only_above_the_wait():
    assert bp._recoverable_reconciliation(390.0, 60.0)   # ceiling >> wait → non-empty
    assert bp._RECOVERABLE_RECONCILE_MARKER in bp._recoverable_reconciliation(390.0, 60.0)
    assert bp._recoverable_reconciliation(60.0, 390.0) == ""   # ceiling < wait → empty
    assert bp._recoverable_reconciliation(61.0, 60.0) == ""    # within tolerance → empty
    assert bp._recoverable_reconciliation(390.0, 0.0) == ""    # no wall → empty


# ── Shared realistic doc: a minority-present slow `guard` whose ceiling exceeds the typical wait ───

def _era_doc() -> dict:
    """A doc as the POST-fix engine produces it: `guard` (390s) runs on only 3/12 PRs, so the
    population-typical wait is ~1m (lint's 60s); the addressable ceiling (~5m30s) far exceeds it."""
    populations = ([[1 / 12, [["lint", 60.0]]]] * 9
                   + [[1 / 12, [["guard", 390.0], ["lint", 60.0]]]] * 3)
    return {
        "repo": "acme/app", "repo_visibility": "public",
        "scanned_at": "2026-07-18T00:00:00Z", "skill_commit_sha": "7039302",
        "commit_sha": "abc123", "findings": [],
        "pr_critical_path": {
            "critical_path_check": "guard", "critical_path_s": 390.0,
            "sampled_pr_count": 12, "sample_target": 12, "check_present_n_pr": 12,
            "checks": [
                {"name": "guard", "workflow_file": ".github/workflows/ci.yml",
                 "p50_s": 390.0, "present_on": 3, "pole_n": 3},
                {"name": "lint", "workflow_file": ".github/workflows/ci.yml",
                 "p50_s": 60.0, "present_on": 12, "pole_n": 0},
            ],
            "poles": [{"check": "guard", "job": "guard",
                       "workflow_file": ".github/workflows/ci.yml", "p50_s": 390.0,
                       "dominant_step": "run guard", "dominant_category": "test",
                       "dominant_p50_s": 360.0, "dominant_share": 0.92, "job_p50_s": 390.0,
                       "steps": [{"step": "run guard", "category": "test", "p50_s": 360.0},
                                 {"step": "checkout", "category": "setup", "p50_s": 30.0}]}],
            "populations": populations, "populations_n": 12, "population_weighted": True,
            "check_present": {"guard": 3, "lint": 12},
            "config_eras": [],
        },
    }


def _write(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


# ── Engine fix 2: a recoverable ceiling above the wait co-renders the reconciliation ──────────────

def test_engine_renders_reconciliation_and_guard_passes(tmp_path):
    doc = _era_doc()
    report = bp.render(doc)
    # Headline win (~5m30s) and the per-pole floor note both exceed the ~1m00s typical wait.
    assert "biggest single measured win is **~5m 30s**" in report
    assert "A typical PR waits **1m 00s**" in report
    # BOTH carry the reconciliation.
    assert report.count(bp._RECOVERABLE_RECONCILE_MARKER) >= 2
    chk = vr.check_recoverable_within_wait(report, _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_guard_fails_when_a_ceiling_above_the_wait_lacks_the_reconciliation(tmp_path):
    """The FAIL discriminator (mirrors the pre-fix engine): a report whose headline win exceeds the
    typical wait but carries NO worst-case reconciliation."""
    report = (
        "# acme/app — why is the merge slow?\n\n"
        "**1m 00s until all checks finish** - `guard` is the slowest check a typical PR "
        "waits on (~6m 30s), but it ran on only 3/12 sampled PRs.\n\n"
        "> **Bottom line.** A typical PR waits **1m 00s** for all checks to finish. "
        "The biggest single measured win is **~5m 30s** off the slowest fixable check, "
        "`guard` - see [Long pole 1](#pole-1) for the drill-down to the biggest lever.\n\n"
        "## Long pole 1: `ci.yml` ▸ guard\n\nsome body\n")
    chk = vr.check_recoverable_within_wait(report, None, None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "typical" in chk.detail.lower()
    # PASS discriminator: add the reconciliation marker.
    fixed = report.replace("biggest lever.",
                           "biggest lever. (This is the "
                           + bp._RECOVERABLE_RECONCILE_MARKER
                           + " this check is the pole; it exceeds the ~1m 00s typical merge wait.)")
    chk2 = vr.check_recoverable_within_wait(fixed, None, None)
    assert chk2.ok and not chk2.skipped, chk2.detail


def test_recoverable_guard_skips_static_only():
    chk = vr.check_recoverable_within_wait("_No measured critical path_\n", None, None)
    assert chk.ok and chk.skipped


# ── Engine fix 1: the era disclosure renders (disclosed_pre) / narrowed note (post_only) ──────────

def test_engine_renders_disclosed_pre_prominently_and_guard_passes(tmp_path):
    doc = _era_doc()
    doc["pr_critical_path"]["config_eras"] = [{
        "workflow_file": ".github/workflows/ci.yml", "boundary": "2026-07-17T00:00:00Z",
        "kept_era": "pre", "rule": "disclosed_pre", "pre_count": 10, "post_count": 2,
        "sufficiency_min": 6}]
    report = bp.render(doc)
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in report      # the loud pre-config caveat
    assert "ci.yml" in report
    chk = vr.check_config_era_boundary(report, _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_engine_renders_post_only_narrowed_note(tmp_path):
    doc = _era_doc()
    doc["pr_critical_path"]["config_eras"] = [{
        "workflow_file": ".github/workflows/ci.yml", "boundary": "2026-07-17T00:00:00Z",
        "kept_era": "post", "rule": "post_only", "pre_count": 3, "post_count": 8,
        "sufficiency_min": 6}]
    report = bp.render(doc)
    assert "narrowed to the current configuration" in report
    # Bill-scope honesty (issue #66 L2): a straddle co-renders the caveat that the runner-minute /
    # cost-spine figures keep the full sample and so still blend the earlier configuration.
    assert "keep the full sample by design" in report and "shard split" in report
    # No pre-era measurement → the guard passes without requiring the loud disclosure.
    chk = vr.check_config_era_boundary(report, _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_post_only_banner_activates_every_era_guard(tmp_path):
    """A rendered era banner is the verifier activation stamp even when the current-config fact
    predates the per-era enumeration sets. All four guards must inspect it instead of reporting a
    coverage SKIP; deleting the banner must remain detectable."""
    doc = _era_doc()
    cp = doc["pr_critical_path"]
    cp["sampled_pr_count"] = 8
    cp["era_dropped_pr_count"] = 12
    cp["config_eras"] = [{
        "workflow_file": ".github/workflows/ci.yml",
        "boundary": "2026-07-17T00:00:00Z",
        "kept_era": "post",
        "rule": "post_only",
        "kept_count": 8,
        "post_count": 8,
        "pre_count": 12,
        "sufficiency_min": 6,
    }]
    report = bp.render(doc)
    assert bp._CONFIG_ERA_NARROWED_MARKER in report
    findings = _write(tmp_path, doc)

    checks = [
        vr.check_config_era_boundary(report, findings, None),
        vr.check_era_enumeration_bound(report, findings, None),
        vr.check_era_disclosure_matches_enumeration(report, findings, None),
        vr.check_era_chain_spine_bound_to_kept_era(report, findings, None),
    ]
    assert all(check.ok and not check.skipped for check in checks), [
        (check.name, check.ok, check.skipped, check.detail) for check in checks
    ]

    without_banner = report.replace(bp._CONFIG_ERA_NARROWED_MARKER, "")
    mutants = [
        vr.check_config_era_boundary(without_banner, findings, None),
        vr.check_era_enumeration_bound(without_banner, findings, None),
        vr.check_era_disclosure_matches_enumeration(without_banner, findings, None),
        vr.check_era_chain_spine_bound_to_kept_era(without_banner, findings, None),
    ]
    assert any(not check.ok and not check.skipped for check in mutants)


def test_degenerate_no_pole_render_still_surfaces_the_era_disclosure():
    """Silent-drop guard (issue #66): a straddle is stamped in collect() independent of whether any
    pole is crownable, so `config_eras` can be set while `poles == []` (all-fileless/managed gate).
    The degenerate no-pole render arms must STILL surface the disclosure, never drop it."""
    doc = {
        "repo": "acme/app", "repo_visibility": "public", "scanned_at": "2026-07-18T00:00:00Z",
        "skill_commit_sha": "7039302", "commit_sha": "abc123", "findings": [],
        "pr_critical_path": {
            "sampled_pr_count": 0, "poles": [], "checks": [], "populations": [],
            "config_eras": [{
                "workflow_file": ".github/workflows/ci.yml", "boundary": "2026-07-17T00:00:00Z",
                "kept_era": "pre", "rule": "disclosed_pre", "pre_count": 7, "post_count": 2,
                "sufficiency_min": 6}],
        },
    }
    report = bp.render(doc)
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in report   # not the bare "no critical path" note
    # And the post_only shape surfaces its narrowed note on the same degenerate arm.
    doc["pr_critical_path"]["config_eras"] = [{
        "workflow_file": ".github/workflows/ci.yml", "boundary": "2026-07-17T00:00:00Z",
        "kept_era": "post", "rule": "post_only", "pre_count": 3, "post_count": 8,
        "sufficiency_min": 6}]
    assert bp._CONFIG_ERA_NARROWED_MARKER in bp.render(doc)


def test_config_era_guard_fails_on_post_only_without_narrowed_note(tmp_path):
    """Symmetric to the pre-era FAIL: a stamped post_only straddle whose narrowed-window note was
    dropped from the report is a silent drop (a shortened window that looks full) → FAIL."""
    doc = _era_doc()
    doc["pr_critical_path"]["config_eras"] = [{
        "workflow_file": ".github/workflows/ci.yml", "boundary": "2026-07-17T00:00:00Z",
        "kept_era": "post", "rule": "post_only", "pre_count": 3, "post_count": 8,
        "sufficiency_min": 6}]
    chk = vr.check_config_era_boundary("# x no narrowed note\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    # PASS discriminator: the same findings + a report carrying the narrowed marker.
    ok = vr.check_config_era_boundary(
        "# x\n\n> **`ci.yml` changed ~1 day ago — " + vr._CONFIG_ERA_NARROWED_MARKER + ".**\n",
        _write(tmp_path, doc), None)
    assert ok.ok and not ok.skipped, ok.detail


def test_no_config_change_is_byte_identical(tmp_path):
    """No-regression pin: an empty `config_eras` (nothing straddled) renders byte-for-byte the same
    as a doc with the key absent entirely — the #66 code path is a pure no-op off the straddle."""
    doc_absent = _era_doc()
    del doc_absent["pr_critical_path"]["config_eras"]
    doc_empty = _era_doc()  # config_eras == []
    assert bp.render(doc_absent) == bp.render(doc_empty)
    # And neither carries any era disclosure text.
    assert bp._CONFIG_ERA_DISCLOSED_MARKER not in bp.render(doc_empty)
    assert "narrowed to the current configuration" not in bp.render(doc_empty)


# ── Verify fix 1: FAIL / PASS / SKIP discriminators ───────────────────────────────────────────────

def _pre_era_findings() -> dict:
    doc = _era_doc()
    doc["pr_critical_path"]["config_eras"] = [{
        "workflow_file": ".github/workflows/ci.yml", "boundary": "2026-07-17T00:00:00Z",
        "kept_era": "pre", "rule": "disclosed_pre", "pre_count": 10, "post_count": 2,
        "sufficiency_min": 6}]
    return doc


def test_config_era_guard_fails_on_pre_era_pole_without_disclosure(tmp_path):
    """The core guard: a drilled pole bound to a `kept_era == "pre"` workflow, in a report that
    omits the era disclosure, FAILs (the pre-fix renderer's shape — reverting blocking_path.py
    reds exactly here)."""
    doc = _pre_era_findings()
    fp = _write(tmp_path, doc)
    report_no_disclosure = (
        "# acme/app\n\n> **Bottom line.** A typical PR waits **1m 00s** for all checks.\n\n"
        "## Long pole 1: `ci.yml` ▸ guard\n\nbody\n")
    chk = vr.check_config_era_boundary(report_no_disclosure, fp, None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "pre-change" in chk.detail.lower()
    # PASS discriminator: the same findings + a report that DOES carry the disclosure.
    with_disclosure = report_no_disclosure.replace(
        "for all checks.",
        "for all checks.\n\n> **⚠️ `ci.yml` changed ~1 day ago — this audit "
        + vr._CONFIG_ERA_DISCLOSED_MARKER + ".**")
    chk2 = vr.check_config_era_boundary(with_disclosure, fp, None)
    assert chk2.ok and not chk2.skipped, chk2.detail


def test_config_era_guard_skips_legacy_artifact_without_the_stamp(tmp_path):
    doc = _era_doc()
    del doc["pr_critical_path"]["config_eras"]   # pre-#66 artifact
    chk = vr.check_config_era_boundary("# x\n", _write(tmp_path, doc), None)
    assert chk.ok and chk.skipped
    assert "pre-#66" in chk.detail


def test_config_era_guard_passes_when_all_straddles_narrowed_to_current(tmp_path):
    doc = _era_doc()
    doc["pr_critical_path"]["config_eras"] = [{
        "workflow_file": ".github/workflows/ci.yml", "boundary": "2026-07-17T00:00:00Z",
        "kept_era": "post", "rule": "post_only", "pre_count": 3, "post_count": 8,
        "sufficiency_min": 6}]
    # No LOUD pre disclosure needed — post_only measures the current config — but the narrowed-window
    # note IS required (the window was silently shortened), so it must be present for a PASS.
    report = "# x\n\n> **`ci.yml` changed ~1 day ago — " + vr._CONFIG_ERA_NARROWED_MARKER + ".**\n"
    chk = vr.check_config_era_boundary(report, _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


# ── The two new checks are registered and classified ─────────────────────────────────────────────

def test_both_new_checks_are_registered_in_run_checks():
    names = {c.name for c in vr.run_checks("# x\n", None, None, skill_repo=None)}
    assert "recoverable ceiling above the typical wait carries a worst-case reconciliation" in names
    assert "no drilled pole measures a pre-change config era without the era disclosure" in names


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Issue #69 — the config-era partition must bind CHECK ENUMERATION (not just spine timing) to the
# kept era. The live shape (internal-dev-repo, skill 7024782): a disclosed_pre report whose Level-1
# chart rendered `test` @ 8m58s (the PRE-#195 full-guard config) BESIDE `guard shard 1/4..4/4` —
# jobs that exist ONLY post-#195, enumerated from 2 post-change PRs in the check sample. Pole 2
# (`guard shard 3/4`) rendered under a disclosure claiming everything reflects the config BEFORE the
# change, and the close reproduced #66's fabricated-redundancy shape ("the full-suite guard overlaps
# the sharded version") through this new path. No config ever ran both.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

_ENUM_BOUNDARY = "2026-07-19T00:00:00Z"
_CI = ".github/workflows/ci.yml"


def _era_wf_of(name: str) -> str:
    """Both `test` (pre-era job, mapped via sampled timing) and every `guard shard N/4` (post-era
    job, mapped via the current scanned job graph) attribute to `ci.yml` — exactly the two mappers
    `collect_runs` feeds `_era_scope_enumeration` in the live shape."""
    return _CI


def _website_enum_sample():
    """The live internal-dev-repo enumeration inputs: pre-change PRs run `test`; the 2 post-change
    PRs run the `guard shard N/4` matrix. `repr_shas`/`per_sha_checks`/`rep_ts` are index-aligned as
    `collect()` builds them (rep_ts from `_group_dev_shas_by_pr`)."""
    shards = {f"guard shard {i}/4": 140.0 + i for i in range(1, 5)}
    repr_shas, per_sha_checks, rep_ts = [], [], {}
    for d in range(11, 19):                       # 8 pre-change PRs, all < boundary, run `test`
        sha = f"pre{d}"
        repr_shas.append(sha); per_sha_checks.append({"test": 538.0})
        rep_ts[sha] = f"2026-07-{d}T00:00:00Z"
    for h in (2, 5):                              # 2 post-change PRs, > boundary, run the shards
        sha = f"post{h}"
        repr_shas.append(sha); per_sha_checks.append(dict(shards))
        rep_ts[sha] = f"2026-07-19T0{h}:00:00Z"
    pr_check_p50 = {"test": 538.0, **shards}
    return pr_check_p50, repr_shas, per_sha_checks, rep_ts


def _disclosed_pre_fact() -> dict:
    return {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "pre",
            "rule": "disclosed_pre", "pre_count": 8, "post_count": 2, "sufficiency_min": 6}


# ── collect_runs._era_pr_side: kept/dropped/None per straddle direction ────────────────────────────

def test_era_pr_side_disclosed_pre_and_post_only_and_missing():
    pre = _disclosed_pre_fact()
    assert cr._era_pr_side("2026-07-15T00:00:00Z", pre) == "kept"     # before boundary → kept (pre)
    assert cr._era_pr_side("2026-07-19T05:00:00Z", pre) == "dropped"  # after boundary → the new config
    assert cr._era_pr_side("", pre) is None                           # no timestamp → unplaceable
    post = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post", "rule": "post_only"}
    assert cr._era_pr_side("2026-07-19T05:00:00Z", post) == "kept"    # after boundary → kept (post)
    assert cr._era_pr_side("2026-07-15T00:00:00Z", post) == "dropped" # before boundary → retired config
    # Multi-boundary disclosed_pre: the kept window is [prev, boundary); an even-older run is dropped.
    multi = {**pre, "multi_change": True, "prev_boundary": "2026-07-10T00:00:00Z"}
    assert cr._era_pr_side("2026-07-12T00:00:00Z", multi) == "kept"
    assert cr._era_pr_side("2026-07-05T00:00:00Z", multi) == "dropped"


# ── collect_runs._era_scope_enumeration: drop the other config's checks, stamp the sets ────────────

def test_enum_scope_drops_post_era_only_shards_in_disclosed_pre():
    """The core #69 fix: a disclosed_pre straddle keeps ONLY the kept (pre) era's checks. The post-
    era-only `guard shard N/4` (observed on the 2 post PRs) drop from the enumeration and are stamped
    as `other_era_checks`; `test` (pre era) survives as the sole enumerated + `kept_checks` member."""
    pr_check_p50, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    fact = _disclosed_pre_fact()
    scoped = cr._era_scope_enumeration(
        pr_check_p50, repr_shas, per_sha_checks, rep_ts, [fact], _era_wf_of)
    assert set(scoped) == {"test"}                        # the shards no longer enumerate
    assert fact["kept_checks"] == ["test"]
    assert fact["other_era_checks"] == [
        "guard shard 1/4", "guard shard 2/4", "guard shard 3/4", "guard shard 4/4"]


def test_enum_scope_post_only_converse_drops_pre_era_only_checks():
    """Converse: a post_only straddle keeps the CURRENT (post) era. The retired config's `test`
    (observed only on pre-change PRs) drops and is named; the shards survive."""
    pr_check_p50, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only", "pre_count": 8, "post_count": 2, "sufficiency_min": 6}
    scoped = cr._era_scope_enumeration(
        pr_check_p50, repr_shas, per_sha_checks, rep_ts, [fact], _era_wf_of)
    assert set(scoped) == {"guard shard 1/4", "guard shard 2/4", "guard shard 3/4", "guard shard 4/4"}
    assert fact["other_era_checks"] == ["test"]
    assert fact["kept_checks"] == [
        "guard shard 1/4", "guard shard 2/4", "guard shard 3/4", "guard shard 4/4"]


def test_enum_scope_is_a_noop_when_nothing_straddled():
    """L2 byte-identity: with no straddle fact, the enumeration is returned UNCHANGED (same object) —
    a non-straddling repo's enumeration path is a pure no-op."""
    pr_check_p50, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    out = cr._era_scope_enumeration(pr_check_p50, repr_shas, per_sha_checks, rep_ts, [], _era_wf_of)
    assert out is pr_check_p50


def test_enum_scope_leaves_unattributed_and_foreign_workflow_checks_untouched():
    """Only checks ATTRIBUTED to the straddling workflow are cut. A check the mapper ties to a
    DIFFERENT workflow (or can't attribute at all) is never dropped — even if it happens to appear
    only on post-change PRs — so a non-straddling sibling's recency is not mistaken for an era add."""
    pr_check_p50, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    # `lint` runs only on the 2 post PRs but belongs to a DIFFERENT (non-straddling) workflow.
    for i, sha in enumerate(repr_shas):
        if sha.startswith("post"):
            per_sha_checks[i]["lint"] = 60.0
    pr_check_p50["lint"] = 60.0
    wf_of = lambda n: ".github/workflows/lint.yml" if n == "lint" else _CI
    fact = _disclosed_pre_fact()
    scoped = cr._era_scope_enumeration(
        pr_check_p50, repr_shas, per_sha_checks, rep_ts, [fact], wf_of)
    assert "lint" in scoped                                # foreign workflow — untouched
    assert set(scoped) == {"test", "lint"}                 # shards (ci.yml, post-only) still dropped
    assert "lint" not in fact["other_era_checks"]


def test_era_resolve_thin_flip_flips_disclosed_pre_with_empty_kept_side_and_redrills():
    """Issue #74 direction (a) — the flip DECISION + spine re-drill. A disclosed_pre straddle whose
    kept (pre) era carries no gate-bearing check in the PR sample (the gate PRs are all post-change)
    flips to post_only_thin, and its spine is re-drilled from the POST runs — the injected redrill
    records exactly the post runs it was handed (both >= boundary), proving the spine flips WITH the
    rule (not just the enumeration)."""
    shards = {f"guard shard {i}/4": 145.0 + i for i in range(1, 5)}
    repr_shas = ["post2", "post5"]                          # only post-change PRs carry any gate check
    per_sha_checks = [dict(shards), dict(shards)]
    rep_ts = {"post2": "2026-07-19T02:00:00Z", "post5": "2026-07-19T05:00:00Z"}
    fact = _disclosed_pre_fact()
    sampled = {_CI: _runs("2026-07-11T00:00:00Z", "2026-07-15T00:00:00Z",   # 2 pre-boundary
                          "2026-07-19T02:00:00Z", "2026-07-19T05:00:00Z")}   # 2 post-boundary
    redrilled: dict[str, list] = {}
    flipped = cr._era_resolve_thin_flip(
        [fact], repr_shas, per_sha_checks, rep_ts, _era_wf_of, sampled,
        lambda wf, post_runs: redrilled.__setitem__(wf, post_runs))
    assert flipped == [_CI]
    assert fact["rule"] == "post_only_thin" and fact["kept_era"] == "post"
    assert fact["thin_sample"] is True and fact["redrilled_post_n"] == 2
    # Re-drilled off the POST runs ONLY (both >= the 2026-07-19 boundary) — the pre runs are gone.
    assert [r["created_at"] for r in redrilled[_CI]] == [
        "2026-07-19T02:00:00Z", "2026-07-19T05:00:00Z"]


def test_era_resolve_thin_flip_no_flip_when_pre_era_has_a_gate_check():
    """No flip (and NO re-drill) when the pre era carries a gate check (`test` on the 8 pre PRs) —
    the legit #69 disclosed_pre stands, byte-identical to before."""
    _pr, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    fact = _disclosed_pre_fact()
    called: list = []
    flipped = cr._era_resolve_thin_flip(
        [fact], repr_shas, per_sha_checks, rep_ts, _era_wf_of,
        {_CI: []}, lambda wf, r: called.append(wf))
    assert flipped == [] and not called
    assert fact["rule"] == "disclosed_pre" and fact["kept_era"] == "pre"


def test_enum_scope_binds_an_already_flipped_post_only_thin_fact():
    """`_era_scope_enumeration` no longer flips — it binds the enumeration of an ALREADY-resolved
    fact. Given a post_only_thin fact (post-only shards, empty pre side), it keeps the shards and
    stamps them as the kept era with nothing other-era."""
    shards = {f"guard shard {i}/4": 145.0 + i for i in range(1, 5)}
    repr_shas = ["post2", "post5"]
    per_sha_checks = [dict(shards), dict(shards)]
    rep_ts = {"post2": "2026-07-19T02:00:00Z", "post5": "2026-07-19T05:00:00Z"}
    fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only_thin", "thin_sample": True, "pre_count": 18, "post_count": 2,
            "sufficiency_min": 6}
    scoped = cr._era_scope_enumeration(
        dict(shards), repr_shas, per_sha_checks, rep_ts, [fact], _era_wf_of)
    assert set(scoped) == set(shards)
    assert fact["kept_checks"] == ["guard shard 1/4", "guard shard 2/4",
                                   "guard shard 3/4", "guard shard 4/4"]
    assert fact["other_era_checks"] == []


def test_enum_scope_residual_empty_leaves_stamps_so_the_guard_can_fail():
    """The mirror degenerate the flip does NOT resolve: a post_only straddle whose CURRENT era carries
    no gate check while the retired one did → the cut would empty the spine. We leave the enumeration
    intact BUT keep the stamps (never clear them — a cleared stamp is what blinded the #74 guard), so
    `check_era_enumeration_bound` FAILs loudly on the leak rather than skipping blind."""
    pr_check_p50, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    # post_only, but every gate check (`test` + shards) is observed only on the DROPPED (pre) side.
    only_pre = {"test": 538.0}
    repr_shas2 = [s for s in repr_shas if s.startswith("pre")]
    per_sha_checks2 = [{"test": 538.0} for _ in repr_shas2]
    rep_ts2 = {s: rep_ts[s] for s in repr_shas2}
    fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only", "pre_count": 8, "post_count": 8, "sufficiency_min": 6}
    scoped = cr._era_scope_enumeration(
        dict(only_pre), repr_shas2, per_sha_checks2, rep_ts2, [fact], _era_wf_of)
    assert set(scoped) == {"test"}                          # intact, not wiped
    # Stamps SURVIVE — `test` is named as other-era, so the enum guard FAILs on the leak (never blind).
    assert fact["other_era_checks"] == ["test"] and fact["kept_checks"] == []


# ── blocking_path: the era note NAMES what the other configuration adds/removes ────────────────────

def _enum_doc(fact: dict, checks: list[dict], poles: list[dict] | None = None) -> dict:
    doc = _era_doc()
    cp = doc["pr_critical_path"]
    cp["config_eras"] = [fact]
    cp["checks"] = checks
    cp["poles"] = poles if poles is not None else []
    cp["populations"] = []
    cp["critical_path_check"] = checks[0]["name"] if checks else None
    cp["critical_path_s"] = checks[0]["p50_s"] if checks else 0.0
    return doc


def test_renderer_names_the_added_checks_in_the_disclosed_pre_note():
    fact = {**_disclosed_pre_fact(),
            "kept_checks": ["test"],
            "other_era_checks": ["guard shard 1/4", "guard shard 2/4",
                                 "guard shard 3/4", "guard shard 4/4"]}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in report          # the loud pre caveat still renders
    assert bp._CONFIG_ERA_OTHER_CHECKS_MARKER in report       # + the enumeration-bind naming clause
    assert "The new configuration adds checks" in report
    assert "guard shard 1/4" in report and "guard shard 4/4" in report
    # The shards are NAMED in the era note but never rendered as a drilled pole (poles == []).
    assert "Long pole" not in report


def test_renderer_names_the_removed_checks_in_the_post_only_note():
    fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only", "pre_count": 8, "post_count": 8, "sufficiency_min": 6,
            "kept_checks": ["guard shard 1/4"], "other_era_checks": ["test"]}
    doc = _enum_doc(fact, [{"name": "guard shard 1/4", "workflow_file": _CI, "p50_s": 146.0,
                            "present_on": 8, "pole_n": 8}])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_NARROWED_MARKER in report
    assert "The previous configuration ran checks" in report and "`test`" in report


def test_renderer_omits_the_naming_clause_when_no_other_era_checks():
    """A single-config straddle (nothing added/removed) renders the era note WITHOUT the naming
    clause — no dangling 'adds checks:' with an empty list."""
    doc = _enum_doc({**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": []},
                    [{"name": "test", "workflow_file": _CI, "p50_s": 538.0, "present_on": 8,
                      "pole_n": 8}])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in report
    assert "The new configuration adds checks" not in report


# ── verify_report.check_era_enumeration_bound: FAIL / PASS / SKIP discriminators ──────────────────

def _mixed_era_findings() -> dict:
    """A hand-built MIXED artifact reproducing the live leak: a disclosed_pre straddle whose
    `other_era_checks` names the shards, yet the report STILL enumerates `guard shard 3/4` as a pole
    and every shard as a Level-1 bar (`checks`) and a population member."""
    fact = {**_disclosed_pre_fact(),
            "kept_checks": ["test"],
            "other_era_checks": ["guard shard 1/4", "guard shard 2/4",
                                 "guard shard 3/4", "guard shard 4/4"]}
    checks = [{"name": "test", "workflow_file": _CI, "p50_s": 538.0, "present_on": 8, "pole_n": 8}]
    checks += [{"name": f"guard shard {i}/4", "workflow_file": _CI, "p50_s": 140.0 + i,
                "present_on": 2, "pole_n": 0} for i in range(1, 5)]
    poles = [{"check": "guard shard 3/4", "job": "guard shard 3/4", "workflow_file": _CI,
              "p50_s": 150.0}]
    pops = [[1 / 2, [["guard shard 3/4", 150.0]]]]
    doc = _enum_doc(fact, checks, poles)
    doc["pr_critical_path"]["populations"] = pops
    return doc


def test_enum_bound_guard_fails_on_a_mixed_era_artifact(tmp_path):
    """The guard FAILs a report that enumerates a check stamped as belonging to the OTHER config —
    the live internal-dev-repo shape."""
    chk = vr.check_era_enumeration_bound("# report\n", _write(tmp_path, _mixed_era_findings()), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "guard shard" in chk.detail


def test_enum_bound_guard_passes_when_only_the_kept_era_enumerates(tmp_path):
    """PASS discriminator: the same straddle, but the shards are bound OUT — only `test` (the kept
    era) enumerates as a bar/pole; the shards live only in `other_era_checks`."""
    fact = {**_disclosed_pre_fact(),
            "kept_checks": ["test"],
            "other_era_checks": ["guard shard 1/4", "guard shard 2/4",
                                 "guard shard 3/4", "guard shard 4/4"]}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}],
                    [{"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 538.0}])
    chk = vr.check_era_enumeration_bound("# report\n", _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_enum_bound_guard_runs_on_era_fact_without_enumeration_sets(tmp_path):
    """The renderer recognizes the config-era fact even without later enumeration sets, so the
    verifier runs too; there are simply no stamped other-era names for this guard to reject."""
    doc = _enum_doc(_disclosed_pre_fact(),
                    [{"name": "test", "workflow_file": _CI, "p50_s": 538.0, "present_on": 8,
                      "pole_n": 8},
                     {"name": "guard shard 3/4", "workflow_file": _CI, "p50_s": 150.0,
                      "present_on": 2, "pole_n": 0}])
    chk = vr.check_era_enumeration_bound("# report\n", _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_enum_bound_guard_skips_when_no_config_eras(tmp_path):
    doc = _era_doc()          # config_eras == []
    chk = vr.check_era_enumeration_bound("# report\n", _write(tmp_path, doc), None)
    assert chk.ok and chk.skipped
    doc2 = _era_doc()
    del doc2["pr_critical_path"]["config_eras"]   # pre-#66 artifact
    chk2 = vr.check_era_enumeration_bound("# report\n", _write(tmp_path, doc2), None)
    assert chk2.ok and chk2.skipped and "pre-#66" in chk2.detail


def test_enum_bound_check_is_registered_and_classified():
    names = {c.name for c in vr.run_checks("# x\n", None, None, skill_repo=None)}
    assert "check enumeration is bound to the kept config era (no other-config check leaks in)" in names


# ── Review-hardening (PR #72 review): the common-case survival + surface-independence gaps ─────────

def test_enum_scope_keeps_a_check_present_in_both_eras():
    """The COMMON case an edit produces: a check that ran UNCHANGED across the boundary (`lint`,
    present on both pre and post PRs) is observed on the KEPT side, so it is kept and never stamped
    as other-era — only the post-era-ONLY shards drop. Guards the `if on_kept:`/`elif on_drop:`
    split against a regression that would drop a check merely because it also ran post-boundary."""
    pr_check_p50, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    for i in range(len(repr_shas)):            # `lint` runs on EVERY sampled PR (both eras)
        per_sha_checks[i]["lint"] = 60.0
    pr_check_p50["lint"] = 60.0
    fact = _disclosed_pre_fact()
    scoped = cr._era_scope_enumeration(
        pr_check_p50, repr_shas, per_sha_checks, rep_ts, [fact], _era_wf_of)
    assert set(scoped) == {"test", "lint"}                 # both-eras `lint` survives; shards drop
    assert "lint" in fact["kept_checks"]
    assert "lint" not in fact["other_era_checks"]


def test_enum_bound_guard_fails_when_leak_is_only_in_poles(tmp_path):
    """Surface independence: the leak hides ONLY in `poles` (the `checks` list enumerates just the
    kept-era `test`). The guard must still FAIL — proving the `poles` arm of `_era_rendered_check_names`
    is load-bearing, not shadowed by the `checks` arm the mixed fixture also trips."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": ["guard shard 3/4"]}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}],
                    [{"check": "guard shard 3/4", "job": "guard shard 3/4",
                      "workflow_file": _CI, "p50_s": 150.0}])
    chk = vr.check_era_enumeration_bound("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "guard shard 3/4" in chk.detail


def test_enum_bound_guard_fails_when_leak_is_only_in_populations(tmp_path):
    """Surface independence: the leak hides ONLY in `populations`. The guard must still FAIL —
    proving the `populations` arm of `_era_rendered_check_names` is load-bearing on its own."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": ["guard shard 3/4"]}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}])
    doc["pr_critical_path"]["populations"] = [[1 / 2, [["guard shard 3/4", 150.0]]]]
    chk = vr.check_era_enumeration_bound("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "guard shard 3/4" in chk.detail


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Issue #74 — the era cut's never-empties fallback silently blended while the disclosure claimed
# pre-only. The live shape (internal-dev-repo, skill dd51d85, boundary 16h ago): the sole gate-bearing
# sampled PRs were BOTH post-change, so #72's disclosed_pre run-count cut would empty the enumeration.
# The old skip-whole fallback left the blend intact AND cleared the stamps the enum guard re-derives
# from, so `test` @ 8m58s + `guard shard 3/4` rendered as poles under a "reflect the configuration
# BEFORE it" disclosure — a report whose disclosure lied about its own contents.
#
# Fix: the flip makes the state space TOTAL. Every straddle resolves to exactly one of
# {post_only, post_only_thin, disclosed_pre}, each with surviving stamps and a matching disclosure;
# the blended-while-claiming-purity state is unreachable.
# ══════════════════════════════════════════════════════════════════════════════════════════════════


def _live_74_enum_inputs():
    """The live #74 enumeration inputs: the ONLY gate-bearing sampled PRs are the 2 post-change ones,
    which run `test` + the `guard shard N/4` matrix. The pre-change PRs carry NO gate check-run in the
    sample (they were not gate-bearing), so the kept (pre) era is check-EMPTY — the flip trigger."""
    shards = {f"guard shard {i}/4": 145.0 + i for i in range(1, 5)}
    checks = {"test": 538.0, **shards}
    repr_shas, per_sha_checks, rep_ts = [], [], {}
    for h in (2, 5):                              # the 2 post-change gate PRs (both > boundary)
        sha = f"post{h}"
        repr_shas.append(sha); per_sha_checks.append(dict(checks))
        rep_ts[sha] = f"2026-07-19T0{h}:00:00Z"
    return dict(checks), repr_shas, per_sha_checks, rep_ts


def _live_74_doc(fact: dict) -> dict:
    """A doc as the FLIPPED + RE-DRILLED engine renders it: `test` + shards enumerate as the current
    config, measured from the thin (npop=2) POST-change sample — so `test` carries its NEW-config
    p50 (~156s), NOT the retired 538s. Poles are stamped with a POST-boundary representative run
    (`repr_run_created_at`), so the timing-provenance guard leg sees the spine derives from post."""
    _post_ts = "2026-07-19T02:00:00Z"                       # > _ENUM_BOUNDARY (2026-07-19T00:00:00Z)
    checks = [{"name": "test", "workflow_file": _CI, "p50_s": 156.0, "present_on": 2, "pole_n": 2}]
    checks += [{"name": f"guard shard {i}/4", "workflow_file": _CI, "p50_s": 145.0 + i,
                "present_on": 2, "pole_n": 0} for i in range(1, 5)]
    poles = [{"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 156.0,
              "dominant_step": "run guard", "dominant_category": "test", "dominant_p50_s": 140.0,
              "dominant_share": 0.90, "job_p50_s": 156.0, "repr_run_created_at": _post_ts,
              "steps": [{"step": "run guard", "category": "test", "p50_s": 140.0}]},
             {"check": "guard shard 3/4", "job": "guard shard 3/4", "workflow_file": _CI,
              "p50_s": 149.0, "dominant_step": "verify guards", "dominant_category": "other",
              "dominant_p50_s": 107.0, "dominant_share": 0.72, "job_p50_s": 149.0,
              "repr_run_created_at": _post_ts,
              "steps": [{"step": "verify guards", "category": "other", "p50_s": 107.0}]}]
    doc = _enum_doc(fact, checks, poles)
    doc["pr_critical_path"]["critical_path_check"] = "test"
    doc["pr_critical_path"]["critical_path_s"] = 156.0
    # Thin sample: npop=2 (< _RARE_PRESENCE_MIN_PR) → the presence machinery is inert (no minority
    # demotion, no populations), so no sub-floor pretend-confident framing fires; the thin disclosure
    # carries the reduced confidence.
    doc["pr_critical_path"]["populations"] = [[1 / 2, [["test", 156.0]]],
                                              [1 / 2, [["guard shard 3/4", 149.0]]]]
    doc["pr_critical_path"]["populations_n"] = 2
    doc["pr_critical_path"]["sampled_pr_count"] = 2
    return doc


def test_live_74_flips_to_post_only_thin_end_to_end(tmp_path):
    """Drive the flip resolution + enumeration on the live #74 inputs, render, and run all three era
    guards. `_era_resolve_thin_flip` flips the fact (its redrill is a no-op here — the doc below
    hand-builds the post-era crit; the pipeline-driven test proves the re-drill produces post
    timings). After the flip: post_only_thin, shards enumerated AS the config, a thin-sample
    (provisional) disclosure, NO pre-only disclosure, and every guard GREEN."""
    pr_check_p50, repr_shas, per_sha_checks, rep_ts = _live_74_enum_inputs()
    fact = _disclosed_pre_fact()
    flipped = cr._era_resolve_thin_flip(
        [fact], repr_shas, per_sha_checks, rep_ts, _era_wf_of,
        {_CI: []}, lambda wf, r: None)
    assert flipped == [_CI]
    assert fact["rule"] == "post_only_thin" and fact["kept_era"] == "post"
    scoped = cr._era_scope_enumeration(
        pr_check_p50, repr_shas, per_sha_checks, rep_ts, [fact], _era_wf_of)
    # The shards are KEPT (they are the current config), stamps survive.
    assert set(scoped) == {"test", "guard shard 1/4", "guard shard 2/4",
                           "guard shard 3/4", "guard shard 4/4"}
    assert fact["other_era_checks"] == []

    doc = _live_74_doc(fact)
    report = bp.render(doc)
    # Thin-sample (provisional) disclosure renders; the pre-only lie is GONE.
    assert bp._CONFIG_ERA_THIN_MARKER in report
    assert bp._CONFIG_ERA_DISCLOSED_MARKER not in report
    assert "measures ONLY the new configuration on a thin sample" in report
    # The shards enumerate AS the config (a pole in the body), not as an "other era" aside.
    assert "guard shard 3/4" in report
    # Bill-scope caveat still renders on the straddle (part C: the full sample is kept by design).
    assert "keep the full sample by design" in report

    fp = _write(tmp_path, doc)
    for chk in (vr.check_config_era_boundary(report, fp, None),
                vr.check_era_enumeration_bound(report, fp, None),
                vr.check_era_disclosure_matches_enumeration(report, fp, None)):
        assert chk.ok and not chk.skipped, f"{chk.name}: {chk.detail}"


def test_disclosed_pre_still_valid_when_pre_side_has_a_gate_check(tmp_path):
    """The legit disclosed_pre (#69) shape is UNCHANGED by the flip: when the pre era DOES carry a
    gate check (`test` on 8 pre PRs), the cut drops only the post-only shards and the outcome stays
    disclosed_pre — byte-identical to the pre-#74 engine."""
    pr_check_p50, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    fact = _disclosed_pre_fact()
    scoped = cr._era_scope_enumeration(
        pr_check_p50, repr_shas, per_sha_checks, rep_ts, [fact], _era_wf_of)
    assert fact["rule"] == "disclosed_pre" and fact["kept_era"] == "pre"   # NOT flipped
    assert set(scoped) == {"test"}
    assert fact["kept_checks"] == ["test"]
    assert fact["other_era_checks"] == [
        "guard shard 1/4", "guard shard 2/4", "guard shard 3/4", "guard shard 4/4"]


# ── PIPELINE-DRIVEN: the timing SPINE flips with the rule (issue #74 direction (a)) ───────────────

def _pipe_job(name: str, dur_s: float) -> dict:
    """A minimal job dict the real drill primitives (`_accumulate_jobs` → `_crit_for` →
    `_critical_path`) accept: name + a started→completed span. `_run_created_at` is stamped by
    `_accumulate_jobs` from the run, so it isn't set here."""
    return {"name": name, "started_at": "2020-01-01T00:00:00Z",
            "completed_at": f"2020-01-01T00:{int(dur_s // 60):02d}:{int(dur_s % 60):02d}Z",
            "html_url": f"https://example/{name}"}


def _era_fake_fetch(jobs_by_run_id: dict):
    def fetch(client, repo, run_id):
        got = jobs_by_run_id.get(run_id)
        return [dict(j) for j in got] if got is not None else None
    return fetch


def test_live_74_pipeline_redrills_spine_from_post_runs():
    """PIPELINE-DRIVEN (item 2): partition → drill → crit_by_wf → (flip) re-drill → crit_by_wf, using
    the REAL drill primitives (`_gather_run_jobs`/`_accumulate_jobs`/`_crit_for`) against a fake job
    fetch. Proves the timing SPINE flips WITH the rule: after the flip, crit_by_wf derives from the
    POST-era runs (`test` @ 156s, the new config), NOT the pre-era drill (`test` @ 538s). RED against
    the pre-redesign commit (which flipped only the enumeration, leaving crit_by_wf at 538s), so it
    catches exactly the defect the reviewer caught (pole timing/links from pre-era runs)."""
    B = "2026-07-19T00:00:00Z"
    pre_runs = [{"id": f"pre{i}", "created_at": f"2026-07-1{i}T00:00:00Z",
                 "event": "pull_request", "head_sha": f"presha{i}"} for i in (1, 2, 3)]
    post_runs = [{"id": f"post{h}", "created_at": f"2026-07-19T0{h}:00:00Z",
                  "event": "pull_request", "head_sha": f"postsha{h}"} for h in (2, 5)]
    jobs = {r["id"]: [_pipe_job("test", 538.0)] for r in pre_runs}       # OLD config: heavy test
    for r in post_runs:                                                  # NEW config: fast test + shard
        jobs[r["id"]] = [_pipe_job("test", 156.0), _pipe_job("guard shard 3/4", 149.0)]
    fetch = _era_fake_fetch(jobs)

    # PRE drill (runs_for_spine = pre) → crit_by_wf reflects the retired config.
    kept, _ = cr._gather_run_jobs(None, "acme/app", pre_runs, fetch=fetch)
    jpr, jbe = [], {}
    cr._accumulate_jobs(kept, jpr, jbe)
    crit0, runs0 = cr._crit_for(jpr, jbe)
    crit_by_wf = {_CI: crit0}
    jobs_per_run_by_wf = {_CI: runs0}
    assert crit_by_wf[_CI]["job_p50"]["test"] == 538.0     # the DEFECT baseline: pre-era spine

    # The flip's injected redrill — exactly what collect()'s `_era_redrill` closure does.
    def redrill(wf, prs):
        k, _f = cr._gather_run_jobs(None, "acme/app", prs, fetch=fetch)
        j2, e2 = [], {}
        cr._accumulate_jobs(k, j2, e2)
        c, cr2 = cr._crit_for(j2, e2)
        crit_by_wf[wf] = c
        jobs_per_run_by_wf[wf] = cr2

    fact = {"workflow_file": _CI, "boundary": B, "kept_era": "pre", "rule": "disclosed_pre",
            "pre_count": 3, "post_count": 2, "sufficiency_min": 6}
    repr_shas = [r["head_sha"] for r in post_runs]
    per_sha_checks = [{"test": 156.0, "guard shard 3/4": 149.0} for _ in post_runs]
    rep_ts = {r["head_sha"]: r["created_at"] for r in post_runs}
    cr._era_resolve_thin_flip([fact], repr_shas, per_sha_checks, rep_ts,
                              lambda n: _CI, {_CI: pre_runs + post_runs}, redrill)

    # THE KEY ASSERTION: the spine flipped WITH the rule — `test` is now the POST 156s, not 538s.
    assert fact["rule"] == "post_only_thin" and fact["kept_era"] == "post"
    assert crit_by_wf[_CI]["job_p50"]["test"] == 156.0
    assert "guard shard 3/4" in crit_by_wf[_CI]["job_p50"]        # the new config's shard is measured

    # And the pole's representative-run era stamps POST (>= boundary), so the timing-provenance guard
    # leg passes. On the pre-redesign commit the drilled runs would be pre → stamp < boundary → FAIL.
    poles = [{"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 156.0}]
    cr._stamp_pole_repr_run_era(poles, [fact], jobs_per_run_by_wf)
    assert poles[0]["repr_run_created_at"] >= B


# ── blocking_path: the post_only_thin disclosure renders ──────────────────────────────────────────

def test_renderer_emits_post_only_thin_disclosure():
    fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only_thin", "thin_sample": True, "pre_count": 18, "post_count": 2,
            "sufficiency_min": 6, "kept_checks": ["guard shard 3/4"], "other_era_checks": []}
    doc = _enum_doc(fact, [{"name": "guard shard 3/4", "workflow_file": _CI, "p50_s": 149.0,
                            "present_on": 2, "pole_n": 2}])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_THIN_MARKER in report
    assert bp._CONFIG_ERA_DISCLOSED_MARKER not in report      # NOT the pre-only lie
    assert bp._CONFIG_ERA_NARROWED_MARKER not in report       # NOT the (full-sample) narrowed note
    assert "Only 2 sampled runs have run on the new configuration" in report
    # Bill-scope caveat rides along (any straddle keeps the full sample by design).
    # Post-#75 it renders in the Data sources footer, which this minimal fragment's
    # no-findings early-return arm skips — assert at the emitter (the full-path render
    # always includes it via the Data sources footer; production post_only_thin docs
    # always take the full path since the flip guarantees kept checks).
    assert "keep the full sample by design" in "".join(bp._bill_scope_era_note(doc))


# ── verify_report.check_era_disclosure_matches_enumeration: FAIL / PASS / SKIP ─────────────────────

def test_disclosure_match_guard_fails_on_hollow_disclosed_pre(tmp_path):
    """The #74 guard's core FAIL: a disclosed_pre straddle whose kept (pre) era enumerates NOTHING
    (empty kept_checks) while every enumerated check is post-era — the live blended-under-a-pre-caveat
    lie, now catchable because the flip makes stamps always survive."""
    fact = {**_disclosed_pre_fact(), "kept_checks": [],
            "other_era_checks": ["guard shard 1/4", "guard shard 2/4",
                                 "guard shard 3/4", "guard shard 4/4"]}
    checks = [{"name": f"guard shard {i}/4", "workflow_file": _CI, "p50_s": 145.0 + i,
               "present_on": 2, "pole_n": 0} for i in range(1, 5)]
    doc = _enum_doc(fact, checks, [{"check": "guard shard 3/4", "job": "guard shard 3/4",
                                    "workflow_file": _CI, "p50_s": 149.0}])
    chk = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "issue #74" in chk.detail.lower() or "post_only_thin" in chk.detail


def test_disclosure_match_guard_fails_on_pre_only_marker_over_all_post(tmp_path):
    """FAIL: the report RENDERS the pre-only disclosure but no stamped straddle is disclosed_pre (they
    all flipped to post_only_thin) — the caveat is rendered over an all-post measurement."""
    fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only_thin", "thin_sample": True, "pre_count": 18, "post_count": 2,
            "sufficiency_min": 6, "kept_checks": ["guard shard 3/4"], "other_era_checks": []}
    doc = _enum_doc(fact, [{"name": "guard shard 3/4", "workflow_file": _CI, "p50_s": 149.0,
                            "present_on": 2, "pole_n": 2}])
    report = "# r\n\n> **⚠️ `ci.yml` changed ~16 hours ago - this audit " \
             + vr._CONFIG_ERA_DISCLOSED_MARKER + ".**\n"
    chk = vr.check_era_disclosure_matches_enumeration(report, _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "no stamped straddle is disclosed_pre" in chk.detail


def test_disclosure_match_guard_fails_on_post_check_under_pre_marker(tmp_path):
    """FAIL: a genuine disclosed_pre (pre era measured `test`) but the report ALSO renders the pre-only
    marker AND enumerates a post-only shard named in other_era_checks — keyed on the rendered marker,
    independent of the enum-bound guard's stamp-only path."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": ["guard shard 3/4"]}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}],
                    [{"check": "guard shard 3/4", "job": "guard shard 3/4",
                      "workflow_file": _CI, "p50_s": 149.0}])
    report = "# r\n\n> this audit " + vr._CONFIG_ERA_DISCLOSED_MARKER + ".\n"
    chk = vr.check_era_disclosure_matches_enumeration(report, _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "guard shard 3/4" in chk.detail


def test_disclosure_match_guard_passes_on_healthy_post_only_thin(tmp_path):
    """PASS: a post_only_thin straddle with the provisional disclosure and no pre-only marker — the
    honest #74 outcome."""
    fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only_thin", "thin_sample": True, "pre_count": 18, "post_count": 2,
            "sufficiency_min": 6, "kept_checks": ["guard shard 3/4"], "other_era_checks": []}
    doc = _enum_doc(fact, [{"name": "guard shard 3/4", "workflow_file": _CI, "p50_s": 149.0,
                            "present_on": 2, "pole_n": 2}])
    report = bp.render(doc)
    chk = vr.check_era_disclosure_matches_enumeration(report, _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_disclosure_match_guard_fails_on_pre_era_pole_under_post_claim(tmp_path):
    """Timing-provenance leg (item 3): a post_only_thin straddle (post-claiming) whose pole is stamped
    with a PRE-boundary representative run — the pole's timing/drill derives from the OLD config while
    the disclosure claims the new one. This is the exact defect the reviewer caught (crit_by_wf from
    pre-era runs); the guard FAILs on the stamped era."""
    fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only_thin", "thin_sample": True, "pre_count": 18, "post_count": 2,
            "sufficiency_min": 6, "kept_checks": ["test"], "other_era_checks": []}
    # The pole's drilled run PREDATES the boundary — a pre-era timing under a post claim.
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 2, "pole_n": 2}],
                    [{"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 538.0,
                      "repr_run_created_at": "2026-07-18T00:00:00Z"}])   # < boundary
    chk = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "predate" in chk.detail.lower() and "post-claiming" in chk.detail.lower()
    # PASS discriminator: same fact, but the pole's drilled run is POST-boundary (re-drilled spine).
    doc["pr_critical_path"]["poles"][0]["repr_run_created_at"] = "2026-07-19T02:00:00Z"
    ok = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc), None)
    assert ok.ok and not ok.skipped, ok.detail


def test_disclosure_match_guard_passes_on_healthy_disclosed_pre(tmp_path):
    """PASS: a legit disclosed_pre (pre era measured `test`, kept_checks non-empty) whose shards are
    bound OUT — the #69 healthy shape. A pre-era pole under the PRE claim is correct, never flagged."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"],
            "other_era_checks": ["guard shard 1/4", "guard shard 2/4",
                                 "guard shard 3/4", "guard shard 4/4"]}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}],
                    [{"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 538.0}])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in report          # pre-only disclosure IS owed here
    chk = vr.check_era_disclosure_matches_enumeration(report, _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_disclosure_match_guard_runs_on_era_fact_without_enumeration_sets(tmp_path):
    """An era fact activates disclosure verification even when later enumeration sets are absent."""
    doc = _enum_doc(_disclosed_pre_fact(),
                    [{"name": "guard shard 3/4", "workflow_file": _CI, "p50_s": 149.0,
                      "present_on": 2, "pole_n": 0}])
    chk = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_disclosure_match_guard_skips_when_no_straddle(tmp_path):
    doc = _era_doc()          # config_eras == []
    chk = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc), None)
    assert chk.ok and chk.skipped
    doc2 = _era_doc()
    del doc2["pr_critical_path"]["config_eras"]   # pre-#66 artifact
    chk2 = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc2), None)
    assert chk2.ok and chk2.skipped and "pre-#66" in chk2.detail


def test_disclosure_match_check_is_registered_and_classified():
    names = {c.name for c in vr.run_checks("# x\n", None, None, skill_repo=None)}
    assert ("the rendered era disclosure matches the enumerated config "
            "(no pre-only caveat over post-era checks)") in names


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Issue #116 — a non-PR-gating workflow's config change must NOT globalize a "whole report reflects
# the OLD/thin config" caveat. The LIVE shape (astro `build-sandbox-image.yml`, `on: push[main] +
# workflow_dispatch`, 0/33 spine checks; biome `preview.yml`/`repository_dispatch.yml`, cron-only):
# the straddle is stamped disclosed_pre from RUN counts alone, but its workflow never gates a PR and
# contributes NO check to the enumerated spine (kept_checks + other_era_checks both empty), so the
# global caveat impugns a headline it cannot affect. The fix scopes the GLOBAL caveat to spine-
# relevant straddles (`_era_stamp_spine_relevance` / `blocking_path._era_fact_spine_relevant`); the
# bill-side staleness still surfaces in the Data-sources bill-scope note.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

_NON_GATING = ".github/workflows/build-sandbox-image.yml"     # on: push[main] + workflow_dispatch


def _spine_irrelevant_disclosed_pre() -> dict:
    """The live astro/biome shape: disclosed_pre by run counts, but the enumeration bound NO check to
    it (kept + other both empty) — the straddle touches zero PR-gating spine checks."""
    return {"workflow_file": _NON_GATING, "boundary": _ENUM_BOUNDARY, "kept_era": "pre",
            "rule": "disclosed_pre", "pre_count": 18, "post_count": 2, "sufficiency_min": 6,
            "kept_checks": [], "other_era_checks": []}


def _test_pole() -> dict:
    """A fully-crownable `test`-on-ci.yml pole so `bp.render` takes the NORMAL (non-degenerate) path —
    the degenerate branch omits the headline + the Data-sources bill-scope note we assert on."""
    return {"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 538.0,
            "dominant_step": "run tests", "dominant_category": "test", "dominant_p50_s": 500.0,
            "dominant_share": 0.93, "job_p50_s": 538.0,
            "steps": [{"step": "run tests", "category": "test", "p50_s": 500.0},
                      {"step": "checkout", "category": "setup", "p50_s": 38.0}]}


# ── collect_runs._era_stamp_spine_relevance: the stamp ────────────────────────────────────────────

def test_stamp_spine_relevance_dev_event_with_a_spine_check_is_relevant():
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": []}
    cr._era_stamp_spine_relevance([fact], {_CI: {"on": {"pull_request": {}}}}, {})
    assert fact["spine_relevant"] is True and fact["developer_event"] is True


def test_stamp_spine_relevance_merge_group_trigger_counts_as_developer_event():
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"]}
    cr._era_stamp_spine_relevance([fact], {_CI: {"on": {"merge_group": {}}}}, {})
    assert fact["developer_event"] is True and fact["spine_relevant"] is True


def test_stamp_spine_relevance_cron_only_workflow_is_irrelevant():
    # workflow_dispatch + schedule only, no PR trigger, and its check is only a TIMING-LESS scan match
    # (empty crit_by_wf → not developer-timed) — so it can't caveat the headline.
    fact = {**_spine_irrelevant_disclosed_pre(), "kept_checks": ["x"]}   # even WITH a scan check
    cr._era_stamp_spine_relevance(
        [fact], {_NON_GATING: {"on": {"schedule": [{"cron": "0 0 * * *"}], "workflow_dispatch": {}}}},
        {})
    assert fact["developer_event"] is False and fact["spine_relevant"] is False


def test_stamp_spine_relevance_dev_event_but_no_spine_check_is_irrelevant():
    fact = _spine_irrelevant_disclosed_pre()      # kept + other empty
    cr._era_stamp_spine_relevance([fact], {_NON_GATING: {"on": {"pull_request": {}}}}, {})
    assert fact["developer_event"] is True and fact["spine_relevant"] is False


def test_stamp_spine_relevance_pull_request_target_counts_as_developer_event():
    # Issue #116 review (Greptile P1): `pull_request_target` is the fork-PR gate — a developer wait
    # too (it's in the canonical `_PR_TRIGGER_EVENTS`). The stamp must recognize it, not just the
    # `pull_request`/`merge_group` pair, else a real fork-PR gate loses its old-config caveat.
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": []}
    cr._era_stamp_spine_relevance([fact], {_CI: {"on": {"pull_request_target": {}}}}, {})
    assert fact["developer_event"] is True and fact["spine_relevant"] is True


def test_stamp_spine_relevance_dev_timed_check_overrides_stale_push_only_on():
    # Issue #116 review (silent-failure hunt): a workflow that DROPPED `pull_request` in the new config
    # keeps its PRE era (disclosed_pre), whose kept check is a real developer-timed gate — but the
    # fetched HEAD `on:` is now push-only. The STRONG signal (dev-timed spine check) must override the
    # stale trigger read so the loud "headline reflects the OLD config" caveat is NOT silently dropped.
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": []}
    push_only = {_CI: {"on": {"push": {"branches": ["main"]}}}}          # stale HEAD: no PR trigger
    dev_timed_crit = {_CI: {"job_p50": {"test": 538.0}}}                 # sampled PR-timed `test` job
    cr._era_stamp_spine_relevance([fact], push_only, dev_timed_crit)
    assert fact["developer_event"] is True and fact["spine_relevant"] is True


def test_stamp_spine_relevance_unknown_wf_doc_leaves_fact_unstamped():
    # Issue #116 review (Greptile P1 "Missing Metadata Means Non-Gating"): a workflow ABSENT from
    # `wf_docs` (doc unreadable/unfetchable — `_fetch_workflow_docs`'s "unknown != absent") with NO
    # strong (dev-timed) evidence must NOT be stamped `spine_relevant=False` and silently lose a real
    # gate's caveat. The fact is left UNSTAMPED so the renderer/guard fallback re-derives from the
    # enumeration sets (bool(kept|other)).
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": []}
    cr._era_stamp_spine_relevance([fact], {}, {})       # ci.yml absent from wf_docs, no dev-timed crit
    assert "spine_relevant" not in fact and "developer_event" not in fact
    # ...and the renderer's fallback still renders the caveat (a real spine check is present).
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}])
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in bp.render(doc)


# ── blocking_path: the GLOBAL caveat is gated on spine relevance ──────────────────────────────────

def test_global_caveat_suppressed_for_spine_irrelevant_disclosed_pre():
    """RED→GREEN: the live shape. A spine-irrelevant disclosed_pre suppresses the global pre-only
    caveat, yet the report still renders (a real `test` headline on ci.yml) AND keeps the Data-sources
    bill-scope note (the config change's runner-minute staleness is still disclosed there)."""
    doc = _enum_doc(_spine_irrelevant_disclosed_pre(),
                    [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                      "present_on": 8, "pole_n": 8}], [_test_pole()])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_DISCLOSED_MARKER not in report          # the overreaching caveat is gone
    assert "keep the full sample by design" in report             # bill-side staleness preserved
    assert "A typical PR waits" in report                         # headline unaffected


def test_global_caveat_still_fires_for_spine_touching_disclosed_pre():
    """NO-REGRESSION PIN: a disclosed_pre that DOES touch the spine (kept_checks non-empty) STILL
    emits the loud pre-only caveat — the honest disclosure is untouched."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": []}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in report


def test_global_thin_caveat_suppressed_for_spine_irrelevant():
    """The post_only_thin marker is gated on the SAME spine relevance — a thin-flip on a straddle
    that touches no spine check can't caveat the headline either."""
    fact = {"workflow_file": _NON_GATING, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only_thin", "thin_sample": True, "pre_count": 18, "post_count": 2,
            "sufficiency_min": 6, "kept_checks": [], "other_era_checks": []}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}], [_test_pole()])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_THIN_MARKER not in report
    assert "keep the full sample by design" in report             # bill-side note still there


def test_global_caveat_gate_trusts_explicit_spine_relevant_stamp():
    """The renderer trusts a stamped `spine_relevant` over the check-derived fallback (future
    artifacts carry it): False suppresses even with checks present; True renders even with none."""
    suppress = {**_disclosed_pre_fact(), "kept_checks": ["test"], "spine_relevant": False}
    doc = _enum_doc(suppress, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                                "present_on": 8, "pole_n": 8}])
    assert bp._CONFIG_ERA_DISCLOSED_MARKER not in bp.render(doc)
    fire = {**_disclosed_pre_fact(), "kept_checks": [], "other_era_checks": [],
            "spine_relevant": True}
    doc2 = _enum_doc(fire, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                             "present_on": 8, "pole_n": 8}])
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in bp.render(doc2)


def test_legacy_disclosed_pre_without_enum_keys_still_renders():
    """A truly pre-enumeration fact (neither kept_checks nor other_era_checks stamped) is NOT
    re-derivable — the renderer defaults to rendering (byte-identical to pre-#116), so a legacy
    artifact never silently loses its disclosure."""
    doc = _enum_doc(_disclosed_pre_fact(),                        # no enum keys at all
                    [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                      "present_on": 8, "pole_n": 8}])
    assert bp._CONFIG_ERA_DISCLOSED_MARKER in bp.render(doc)


# ── verify_report: the converse leg + the boundary guard's spine-relevance filter ─────────────────

def test_disclosure_match_guard_fails_on_global_caveat_for_spine_irrelevant(tmp_path):
    """RED on the OLD reports: a global pre-only caveat rendered for a spine-irrelevant straddle
    (marker + the workflow file on the same caveat line) FAILs — the #116 overreach."""
    doc = _enum_doc(_spine_irrelevant_disclosed_pre(),
                    [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                      "present_on": 8, "pole_n": 8}])
    report = ("# r\n\n> **⚠️ `" + _NON_GATING + "` changed ~140 days ago - this audit "
              + vr._CONFIG_ERA_DISCLOSED_MARKER + ".** so the headline and every drill-down below "
              "reflect the configuration BEFORE it.\n")
    chk = vr.check_era_disclosure_matches_enumeration(report, _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "issue #116" in chk.detail and "never gates" in chk.detail


def test_disclosure_match_guard_passes_when_spine_irrelevant_caveat_suppressed(tmp_path):
    """GREEN on the re-render: the SAME spine-irrelevant fact with the caveat SUPPRESSED (no marker)
    passes — the honest post-fix report."""
    doc = _enum_doc(_spine_irrelevant_disclosed_pre(),
                    [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                      "present_on": 8, "pole_n": 8}])
    report = bp.render(doc)
    assert vr._CONFIG_ERA_DISCLOSED_MARKER not in report
    chk = vr.check_era_disclosure_matches_enumeration(report, _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_disclosure_match_guard_fails_on_incoherent_spine_relevant_stamp(tmp_path):
    """Stamp-integrity arm: a `spine_relevant=True` stamp whose basis (developer_event AND a spine
    check) says False is incoherent — the guard FAILs it independently of any rendered marker."""
    fact = {**_spine_irrelevant_disclosed_pre(),                  # kept + other empty
            "developer_event": True, "spine_relevant": True}      # ...yet claims relevant
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}])
    chk = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "spine_relevant" in chk.detail and "issue #116" in chk.detail


def test_disclosure_match_guard_fails_on_global_THIN_caveat_for_spine_irrelevant(tmp_path):
    """Overreach arm, `post_only_thin` branch (mirror of the disclosed_pre pin): a rendered THIN
    marker for a spine-irrelevant straddle FAILs on the `_CONFIG_ERA_THIN_MARKER` constant/rule —
    the guard's thin branch must key on the thin marker, not only the pre-only one."""
    fact = {"workflow_file": _NON_GATING, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only_thin", "thin_sample": True, "pre_count": 18, "post_count": 2,
            "sufficiency_min": 6, "kept_checks": [], "other_era_checks": []}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}])
    report = ("# r\n\n> **⚠️ `" + _NON_GATING + "` changed ~140 days ago — this audit measures ONLY "
              "the new configuration on a thin sample.** " + vr._CONFIG_ERA_THIN_MARKER + ".\n")
    chk = vr.check_era_disclosure_matches_enumeration(report, _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "issue #116" in chk.detail and "never gates" in chk.detail


def test_disclosure_match_guard_overreach_trusts_stamp_over_checks(tmp_path):
    """Verify-side mirror of the renderer's stamp-over-fallback pin: a `spine_relevant=False` stamp
    with NON-empty `kept_checks` (the fallback would call it relevant) yet a rendered pre-only marker
    still FAILs the overreach arm — the guard trusts the stamp, not its own check-derived fallback."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": [],
            "developer_event": False, "spine_relevant": False}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}])
    report = ("# r\n\n> **⚠️ `" + _CI + "` changed ~140 days ago - this audit "
              + vr._CONFIG_ERA_DISCLOSED_MARKER + ".** so the headline reflects the config BEFORE it.\n")
    chk = vr.check_era_disclosure_matches_enumeration(report, _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "issue #116" in chk.detail and "never gates" in chk.detail


def test_config_era_boundary_guard_does_not_demand_caveat_for_spine_irrelevant_pre(tmp_path):
    """The boundary guard must not FAIL a report that (correctly) OMITS the disclosure for a spine-
    irrelevant pre straddle — else the suppression and the guard would fight."""
    doc = _enum_doc(_spine_irrelevant_disclosed_pre(),
                    [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                      "present_on": 8, "pole_n": 8}])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_DISCLOSED_MARKER not in report
    chk = vr.check_config_era_boundary(report, _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_config_era_boundary_guard_still_demands_caveat_for_spine_relevant_pre(tmp_path):
    """NO-REGRESSION: a spine-TOUCHING pre straddle whose disclosure was dropped still FAILs the
    boundary guard (the #66 silent-drop protection is intact for genuine pre-era measurements)."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": []}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}])
    chk = vr.check_config_era_boundary("# r\n\n(no disclosure)\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail


def test_config_era_boundary_guard_does_not_demand_THIN_note_for_spine_irrelevant(tmp_path):
    """Issue #116 review (renderer/guard lockstep, thin side): the renderer suppresses the loud thin
    marker for a spine-irrelevant `post_only_thin` straddle, so the boundary guard must NOT then FAIL
    the report for omitting the provisional note — else guard and renderer fight (the pre-side twin of
    this pin already exists; the thin-side demand was left un-gated by the initial #116 patch)."""
    fact = {"workflow_file": _NON_GATING, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only_thin", "thin_sample": True, "pre_count": 18, "post_count": 2,
            "sufficiency_min": 6, "kept_checks": [], "other_era_checks": []}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}], [_test_pole()])
    report = bp.render(doc)
    assert bp._CONFIG_ERA_THIN_MARKER not in report          # renderer honestly suppressed it
    chk = vr.check_config_era_boundary(report, _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_config_era_boundary_guard_still_demands_THIN_note_for_spine_relevant(tmp_path):
    """NO-REGRESSION (thin side): a spine-RELEVANT `post_only_thin` straddle whose provisional note was
    dropped still FAILs — the #74 thin-sample protection is intact for genuine current-config thin
    measurements; the lockstep filter only excuses the spine-IRRELEVANT ones."""
    fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
            "rule": "post_only_thin", "thin_sample": True, "pre_count": 18, "post_count": 2,
            "sufficiency_min": 6, "kept_checks": [], "other_era_checks": ["test"],
            "developer_event": True, "spine_relevant": True}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 8, "pole_n": 8}])
    chk = vr.check_config_era_boundary("# r\n\n(no provisional note)\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Issue #77 — content-keyed config-era classification. The live failure (internal-dev-repo, 2026-07-20):
# the two PRs that CARRIED the CI fix ran the NEW ci.yml from their own heads MINUTES BEFORE the merge
# boundary, so their `created_at` classified them "pre". Cascade: their new-config makespan rendered
# under a pre-only disclosure; the #74 thin-flip was suppressed (they looked like kept-side gate PRs);
# the verify guard's repr-run leg compared the same timestamps and passed. The fix classifies a
# straddling workflow's sampled runs by the workflow-file CONTENT their head_sha carries.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

_POST_BLOB = "blobPOST"
_PRE_BLOB = "blobPRE"
_LIVE_B = "2026-07-19T02:26:12Z"          # the live ci.yml merge-commit boundary


class _BlobClient:
    """Serves `commits?path=` (boundary lookup) and `contents/{wf}?ref=` (blob identity) replies, and
    RECORDS every endpoint so a test can assert the exact extra-call budget. `ref -> blob sha` maps a
    commit/head to the workflow blob it carries; an unmapped ref returns None (→ timestamp fallback)."""
    def __init__(self, ref_to_blob: dict, commits_reply: object = None) -> None:
        self.ref_to_blob = ref_to_blob
        self.commits_reply = commits_reply
        self.endpoints: list[str] = []

    def json(self, endpoint: str, allow_missing: bool = False) -> object:
        self.endpoints.append(endpoint)
        if "/commits?" in endpoint:
            return self.commits_reply
        if "/contents/" in endpoint and "ref=" in endpoint:
            ref = endpoint.split("ref=", 1)[1]
            blob = self.ref_to_blob.get(ref)
            return {"sha": blob} if blob else None
        return None

    def contents_calls(self) -> list[str]:
        return [e for e in self.endpoints if "/contents/" in e]


def _live77_runs():
    """The live shape: 18 pre / 2 post runs BY TIMESTAMP. Of the 18 timestamp-pre, TWO are the fix-PR
    `pull_request` runs whose head carried the NEW ci.yml (content POST) — created just before the
    boundary. The other 16 are genuine pre-config runs; the 2 timestamp-post are genuine new-config
    push runs. head_sha carries the content identity used by `_resolve_content_eras`."""
    runs = []
    # 16 genuine pre-config runs (content pre), spread before the boundary.
    for i in range(16):
        runs.append({"created_at": f"2026-07-1{8 if i < 9 else 7}T0{i % 9}:00:00Z",
                     "head_sha": f"presha{i}", "event": "push"})
    # 2 fix-PRs: timestamp PRE-boundary, content POST (ran the new ci.yml from their own head).
    runs.append({"created_at": "2026-07-19T02:11:47Z", "head_sha": "228e025", "event": "pull_request"})
    runs.append({"created_at": "2026-07-19T02:19:21Z", "head_sha": "90a7d99", "event": "pull_request"})
    # 2 genuine post-merge new-config runs (content post, timestamp post).
    runs.append({"created_at": "2026-07-19T02:40:00Z", "head_sha": "postsha0", "event": "push"})
    runs.append({"created_at": "2026-07-19T03:00:00Z", "head_sha": "postsha1", "event": "push"})
    return runs


def _live77_ref_to_blob():
    m = {"s_last": _POST_BLOB, "s_prev": _PRE_BLOB}
    for i in range(16):
        m[f"presha{i}"] = _PRE_BLOB
    m["228e025"] = _POST_BLOB           # the fix-PRs' heads carry the NEW workflow
    m["90a7d99"] = _POST_BLOB
    m["postsha0"] = _POST_BLOB
    m["postsha1"] = _POST_BLOB
    return m


# ── the new helpers, in isolation ─────────────────────────────────────────────────────────────────

def test_timestamp_straddles_gate():
    runs = _live77_runs()
    assert cr._timestamp_straddles(runs, _LIVE_B)                      # both sides non-empty
    assert not cr._timestamp_straddles(runs, "2026-06-01T00:00:00Z")  # all runs post → not a straddle
    assert not cr._timestamp_straddles(runs, "2027-01-01T00:00:00Z")  # all runs pre → not a straddle
    assert not cr._timestamp_straddles([], _LIVE_B)
    assert not cr._timestamp_straddles(runs, "")                       # no boundary → never


def test_workflow_blob_at_reads_the_blob_sha_with_one_call():
    c = _BlobClient({"abc": "blob123"})
    assert cr._workflow_blob_at(c, "r", ".github/workflows/ci.yml", "abc") == "blob123"
    assert len(c.contents_calls()) == 1
    ep = c.contents_calls()[0]
    assert ep == "repos/r/contents/.github/workflows/ci.yml?ref=abc"
    # An unmapped ref (missing file / failed fetch) → None, still one call.
    assert cr._workflow_blob_at(_BlobClient({}), "r", "w", "zzz") is None
    # An empty ref never fetches.
    c2 = _BlobClient({})
    assert cr._workflow_blob_at(c2, "r", "w", "") is None and not c2.contents_calls()


def test_resolve_content_eras_classifies_by_content_with_bounded_calls():
    runs = _live77_runs()
    c = _BlobClient(_live77_ref_to_blob())
    era_by_sha, basis = cr._resolve_content_eras(
        c, "acme/site", ".github/workflows/ci.yml", runs, "s_last", "s_prev")
    # The fix-PRs classify POST by content though their timestamps are pre-boundary.
    assert era_by_sha["228e025"] == "post" and era_by_sha["90a7d99"] == "post"
    assert era_by_sha["presha0"] == "pre" and era_by_sha["postsha0"] == "post"
    assert basis["content"] == 20 and basis["timestamp"] == 0
    assert basis["boundary_blob_resolved"] is True
    # Call budget: 2 boundary blobs + 1 per UNIQUE head (20 unique) = 22 contents calls, no more.
    assert len(c.contents_calls()) == 2 + 20


def test_resolve_content_eras_neither_blob_falls_back_to_timestamp():
    """A head whose blob matches NEITHER the pre nor post boundary blob (an unrelated intermediate
    edit carried on that branch) is OMITTED from the map → the caller times-stamps it. basis counts it
    as a timestamp fallback."""
    runs = [{"created_at": "2026-07-19T02:11:00Z", "head_sha": "weird", "event": "pull_request"},
            {"created_at": "2026-07-19T02:40:00Z", "head_sha": "postsha0", "event": "push"}]
    ref_to_blob = {"s_last": _POST_BLOB, "s_prev": _PRE_BLOB,
                   "weird": "blobOTHER", "postsha0": _POST_BLOB}
    era_by_sha, basis = cr._resolve_content_eras(
        _BlobClient(ref_to_blob), "r", "w", runs, "s_last", "s_prev")
    assert "weird" not in era_by_sha and era_by_sha["postsha0"] == "post"
    assert basis["content"] == 1 and basis["timestamp"] == 1


def test_resolve_content_eras_no_post_blob_skips_per_head_fetches():
    """If the POST (boundary) blob fails to resolve, content classification is impossible; return
    empty WITHOUT paying for the per-head fetches (2 boundary attempts only)."""
    runs = _live77_runs()
    c = _BlobClient({})            # every ref unmapped → post_blob is None
    era_by_sha, basis = cr._resolve_content_eras(c, "r", "w", runs, "s_last", "s_prev")
    assert era_by_sha == {} and basis["boundary_blob_resolved"] is False
    assert len(c.contents_calls()) == 1        # only the POST-blob attempt; no per-head fetches


def test_run_config_era_is_content_first_timestamp_fallback():
    B = _LIVE_B
    content = {"228e025": "post"}
    # content wins over the pre timestamp
    assert cr._run_config_era({"created_at": "2026-07-19T02:11:47Z", "head_sha": "228e025"},
                              B, content) == "post"
    # no content match → timestamp
    assert cr._run_config_era({"created_at": "2026-07-19T02:11:47Z", "head_sha": "other"},
                              B, content) == "pre"
    assert cr._run_config_era({"created_at": "2026-07-19T03:00:00Z", "head_sha": "other"},
                              B, content) == "post"
    # no content, no timestamp → unplaceable
    assert cr._run_config_era({"head_sha": "other"}, B, content) is None


# ── content-keyed partition: the fix-PRs move pre→post ────────────────────────────────────────────

def test_partition_content_keys_the_fix_prs_to_post():
    runs = _live77_runs()
    era_by_sha, _ = cr._resolve_content_eras(
        _BlobClient(_live77_ref_to_blob()), "r", ".github/workflows/ci.yml", runs, "s_last", "s_prev")
    kept, fact = cr._partition_config_era(runs, _LIVE_B, None, era_by_sha)
    # By CONTENT: 16 pre, 4 post (2 fix + 2 genuine). 4 < 6 → disclosed_pre, kept = the 16 content-pre.
    assert fact["rule"] == "disclosed_pre" and fact["pre_count"] == 16 and fact["post_count"] == 4
    kept_shas = {r["head_sha"] for r in kept}
    assert "228e025" not in kept_shas and "90a7d99" not in kept_shas   # dropped from the pre spine
    # Pure-timestamp partition (no content map) MISCLASSIFIES: 18 pre / 2 post, fix-PRs in the pre spine.
    _kept_ts, fact_ts = cr._partition_config_era(runs, _LIVE_B, None, None)
    assert fact_ts["pre_count"] == 18 and fact_ts["post_count"] == 2
    assert "228e025" in {r["head_sha"] for r in _kept_ts}


def test_stale_branch_converse_post_timestamp_but_pre_content_classifies_pre():
    """The converse: a stale branch merged AFTER the boundary runs the OLD workflow (content pre). Its
    `created_at` is post, but content places it pre — so with the 3 stale runs the ONLY post-timestamp
    runs, content collapses the straddle (post=0) and the sample resolves to a single (pre) era."""
    runs = [{"created_at": f"2026-07-18T0{i}:00:00Z", "head_sha": f"pre{i}", "event": "push"}
            for i in range(10)]
    runs += [{"created_at": f"2026-07-19T0{i + 3}:00:00Z", "head_sha": f"stale{i}",
              "event": "pull_request"} for i in range(3)]          # timestamp POST (> 02:26), content PRE
    ref_to_blob = {"s_last": _POST_BLOB, "s_prev": _PRE_BLOB}
    for i in range(10):
        ref_to_blob[f"pre{i}"] = _PRE_BLOB
    for i in range(3):
        ref_to_blob[f"stale{i}"] = _PRE_BLOB                       # stale branch ran the OLD workflow
    era_by_sha, _ = cr._resolve_content_eras(
        _BlobClient(ref_to_blob), "r", "w", runs, "s_last", "s_prev")
    kept, fact = cr._partition_config_era(runs, _LIVE_B, None, era_by_sha)
    assert fact is None and kept is runs        # content collapses the straddle → single era, no blend
    # Pure timestamp would have WRONGLY straddled (10 pre / 3 post).
    _k, fact_ts = cr._partition_config_era(runs, _LIVE_B, None, None)
    assert fact_ts is not None and fact_ts["post_count"] == 3


# ── the end-to-end pin: the thin-flip FIRES under content keying, SUPPRESSED under pure timestamps ──

def test_live77_thin_flip_fires_under_content_and_is_suppressed_under_timestamps():
    """THE regression pin. Build the fact via the REAL pipeline (`_resolve_content_eras` →
    `_partition_config_era`), then run `_era_resolve_thin_flip` twice: WITH the content map on the
    fact (the fix — the fix-PRs read POST/dropped, kept pre is check-empty → FLIP) and WITHOUT it
    (pure timestamps — the fix-PRs read PRE/kept → the flip is SUPPRESSED, the live bug). Provably
    red/green off the single content-keying change."""
    runs = _live77_runs()
    era_by_sha, basis = cr._resolve_content_eras(
        _BlobClient(_live77_ref_to_blob()), "acme/site", _CI, runs, "s_last", "s_prev")
    _kept, fact = cr._partition_config_era(runs, _LIVE_B, None, era_by_sha)
    fact["workflow_file"] = _CI
    fact["content_era_by_sha"] = era_by_sha
    assert fact["rule"] == "disclosed_pre"

    # The gate sample is EXACTLY the 2 fix-PRs (sampled_pr_count=2 on the live shape); both carry the
    # new-config gate check. rep_ts are their PRE-boundary timestamps.
    repr_shas = ["228e025", "90a7d99"]
    per_sha_checks = [{"test": 156.0, "guard shard 3/4": 149.0} for _ in repr_shas]
    rep_ts = {"228e025": "2026-07-19T02:11:47Z", "90a7d99": "2026-07-19T02:19:21Z"}
    sampled = {_CI: runs}

    # WITH content: the fix-PRs classify dropped(post) → kept(pre) is check-empty → FLIP + re-drill.
    redrilled: dict[str, list] = {}
    flipped = cr._era_resolve_thin_flip(
        [fact], repr_shas, per_sha_checks, rep_ts, _era_wf_of, sampled,
        lambda wf, prs: redrilled.__setitem__(wf, prs))
    assert flipped == [_CI]
    assert fact["rule"] == "post_only_thin" and fact["kept_era"] == "post"
    # The re-drill's post_runs are CONTENT-keyed: the 2 fix-PRs (content post, timestamp pre) AND the
    # 2 genuine post runs — 4 post runs, so the new-config spine is measured on the fullest sample.
    redrill_shas = {r["head_sha"] for r in redrilled[_CI]}
    assert redrill_shas == {"228e025", "90a7d99", "postsha0", "postsha1"}

    # WITHOUT content (strip the map): pure timestamps place the fix-PRs PRE/kept → the flip is
    # SUPPRESSED — the exact live bug this change fixes.
    fact2 = dict(fact)
    fact2["rule"] = "disclosed_pre"
    fact2["kept_era"] = "pre"
    fact2.pop("content_era_by_sha", None)
    called: list = []
    flipped2 = cr._era_resolve_thin_flip(
        [fact2], repr_shas, per_sha_checks, rep_ts, _era_wf_of, sampled,
        lambda wf, prs: called.append(wf))
    assert flipped2 == [] and not called
    assert fact2["rule"] == "disclosed_pre"       # the misclassification the fix removes


def test_live77_zero_extra_calls_when_not_straddling():
    """Hard byte-identity requirement: a workflow whose sample does NOT timestamp-straddle its
    boundary (or has none) makes ZERO content-blob calls — the collect() gate is
    `_timestamp_straddles`, and a non-straddling repo never pays."""
    # All runs pre-date the boundary → not a straddle → the collect() gate would skip the fetch.
    runs = [{"created_at": f"2026-07-1{i}T00:00:00Z", "head_sha": f"s{i}", "event": "push"}
            for i in range(5)]
    B = "2026-08-01T00:00:00Z"
    assert not cr._timestamp_straddles(runs, B)
    # And the partition off a None content map is byte-identical to the pre-#77 path (no fact).
    kept, fact = cr._partition_config_era(runs, B, None, None)
    assert kept is runs and fact is None


# ── _era_pr_side: content-first placement ─────────────────────────────────────────────────────────

def test_era_pr_side_content_first_placement():
    fact = {**_disclosed_pre_fact(), "content_era_by_sha": {"fixpr": "post", "oldpr": "pre"}}
    # A fix-PR: timestamp PRE (< boundary) but content POST → dropped (not a kept pre gate PR).
    assert cr._era_pr_side("2026-07-18T00:00:00Z", fact, "fixpr") == "dropped"
    # An old PR: content pre → kept.
    assert cr._era_pr_side("2026-07-18T00:00:00Z", fact, "oldpr") == "kept"
    # A head absent from the map falls back to timestamp (kept, since pre).
    assert cr._era_pr_side("2026-07-18T00:00:00Z", fact, "unknown") == "kept"
    # No head_sha at all → pure timestamp (the legacy callers / tests).
    assert cr._era_pr_side("2026-07-18T00:00:00Z", fact) == "kept"
    # post_only converse: a content-pre head is dropped even with a post timestamp.
    post_fact = {"workflow_file": _CI, "boundary": _ENUM_BOUNDARY, "kept_era": "post",
                 "rule": "post_only", "content_era_by_sha": {"stale": "pre"}}
    assert cr._era_pr_side("2026-07-19T05:00:00Z", post_fact, "stale") == "dropped"


# ── _stamp_pole_repr_run_era + verify leg: the content basis closes the timestamp blind spot ───────

def test_stamp_pole_repr_run_era_stamps_content_basis():
    """A post_only_thin pole whose earliest drilled run is a fix-PR (content POST, timestamp PRE) is
    stamped `repr_run_era=post` / `basis=content` — NOT mislabeled pre by its pre-boundary timestamp."""
    fact = {"workflow_file": _CI, "boundary": _LIVE_B, "kept_era": "post", "rule": "post_only_thin",
            "content_era_by_sha": {"228e025": "post", "postsha0": "post"}}
    # Drilled runs: the fix-PR (earliest, timestamp PRE-boundary) + a genuine post run.
    jobs_per_run_by_wf = {_CI: [
        [{"name": "test", "_run_created_at": "2026-07-19T02:11:47Z", "_run_head_sha": "228e025"}],
        [{"name": "test", "_run_created_at": "2026-07-19T02:40:00Z", "_run_head_sha": "postsha0"}]]}
    poles = [{"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 156.0}]
    cr._stamp_pole_repr_run_era(poles, [fact], jobs_per_run_by_wf)
    p = poles[0]
    assert p["repr_run_created_at"] == "2026-07-19T02:11:47Z"    # earliest (a pre-boundary timestamp)
    assert p["repr_run_head_sha"] == "228e025"
    assert p["repr_run_era"] == "post" and p["repr_run_era_basis"] == "content"


def test_disclosure_leg_passes_content_post_pole_with_pre_timestamp(tmp_path):
    """The blind-spot closer: a post_only_thin pole whose repr run is content POST but timestamp PRE
    PASSES (basis content, era post, coherent with the fact map) — where the pre-#77 timestamp-only
    leg would have FAILed it."""
    fact = {"workflow_file": _CI, "boundary": _LIVE_B, "kept_era": "post", "rule": "post_only_thin",
            "thin_sample": True, "pre_count": 18, "post_count": 4, "sufficiency_min": 6,
            "kept_checks": ["test"], "other_era_checks": [],
            "content_era_by_sha": {"228e025": "post"}}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 156.0,
                            "present_on": 2, "pole_n": 2}],
                    [{"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 156.0,
                      "repr_run_created_at": "2026-07-19T02:11:47Z",   # < boundary, but content post
                      "repr_run_head_sha": "228e025", "repr_run_era": "post",
                      "repr_run_era_basis": "content"}])
    chk = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_disclosure_leg_fails_content_pre_pole_under_post_claim(tmp_path):
    """A genuine pre-config pole (content PRE) under a post claim still FAILs — the content basis does
    not launder a real pre-era leak, it just judges it by content instead of timestamp."""
    fact = {"workflow_file": _CI, "boundary": _LIVE_B, "kept_era": "post", "rule": "post_only_thin",
            "thin_sample": True, "pre_count": 18, "post_count": 4, "sufficiency_min": 6,
            "kept_checks": ["test"], "other_era_checks": [],
            "content_era_by_sha": {"presha0": "pre"}}
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 2, "pole_n": 2}],
                    [{"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 538.0,
                      "repr_run_created_at": "2026-07-19T03:00:00Z",   # > boundary by timestamp!
                      "repr_run_head_sha": "presha0", "repr_run_era": "pre",
                      "repr_run_era_basis": "content"}])
    chk = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "pre-era" in chk.detail.lower()


def test_disclosure_leg_fails_incoherent_content_stamps(tmp_path):
    """Tamper/bug guard: the pole's self-stamped `repr_run_era` DISAGREES with the fact-level
    `content_era_by_sha` for the same head sha — the two independent stamps are incoherent → FAIL
    (the leg is not reduced to trusting a lone self-stamp)."""
    fact = {"workflow_file": _CI, "boundary": _LIVE_B, "kept_era": "post", "rule": "post_only_thin",
            "thin_sample": True, "pre_count": 18, "post_count": 4, "sufficiency_min": 6,
            "kept_checks": ["test"], "other_era_checks": [],
            "content_era_by_sha": {"presha0": "pre"}}       # the map says PRE
    doc = _enum_doc(fact, [{"name": "test", "workflow_file": _CI, "p50_s": 538.0,
                            "present_on": 2, "pole_n": 2}],
                    [{"check": "test", "job": "test", "workflow_file": _CI, "p50_s": 538.0,
                      "repr_run_created_at": "2026-07-19T03:00:00Z",
                      "repr_run_head_sha": "presha0", "repr_run_era": "post",   # self-stamp says POST
                      "repr_run_era_basis": "content"}])
    chk = vr.check_era_disclosure_matches_enumeration("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "incoherent" in chk.detail.lower()


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Issue #80 — the PER-PR chain/makespan spine door. #66/#68 scoped the spine RUNS, #69 the enumerated
# CHECK SET, but the per-PR SAMPLE feeding chain_facts → chain_summary → makespan_p50_s (the "typical
# PR waits N" headline + the #24 physical-bound cap), populations, and presence denominators was still
# the raw sample, filtered only by check NAME. A check name survives a config change, so a dropped-era
# PR's `test` interval blended into a pre-claiming makespan (live: 166s post-era makespan over a 538s
# pre gate). `_era_scope_pr_spine_sample` scopes the sample; the guard re-derives the bind offline.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def _iv(*names_starts_ends):
    """{name: (start, end)} interval map from flat (name, start, end) triples."""
    return {n: (s, e) for n, s, e in names_starts_ends}


def test_spine_door_disclosed_pre_drops_post_side_prs_keeps_pre():
    """The core #80 door: 8 pre PRs run `test`, 2 post PRs run the shards; under a disclosed_pre fact
    the 2 post PRs are dropped-side (content map places their heads post) → the door removes them from
    the per-PR spine sample. The 8 pre PRs survive; dropped_pr_count == 2."""
    _pr, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    intervals = {s: _iv(("test", rep_ts[s], "2026-07-19T00:08:58Z")) for s in repr_shas
                 if s.startswith("pre")}
    for s in repr_shas:
        if s.startswith("post"):
            intervals[s] = _iv(*[(f"guard shard {i}/4", rep_ts[s], "2026-07-19T00:02:46Z")
                                 for i in range(1, 5)])
    fact = {**_disclosed_pre_fact(),
            "content_era_by_sha": {"post2": "post", "post5": "post"}}
    kept_shas, kept_checks, kept_iv, dropped = cr._era_scope_pr_spine_sample(
        repr_shas, per_sha_checks, intervals, rep_ts, {"test"}, [fact], _era_wf_of)
    assert dropped == 2
    assert kept_shas == [s for s in repr_shas if s.startswith("pre")]
    assert all("test" in m for m in kept_checks)
    assert set(kept_iv) == set(kept_shas)                       # post intervals removed


def test_spine_door_mixed_kept_and_dropped_uses_kept_only():
    """Mixed case: kept-side PRs present AND dropped-side present → the door keeps only the kept side.
    Here the kept (pre) era's own `test` still runs on 2 pre PRs, alongside 2 dropped (post) PRs whose
    `test` is the NEW fast config — those post `test` intervals must not blend into the makespan."""
    repr_shas = ["preA", "preB", "postC", "postD"]
    per_sha_checks = [{"test": 538.0}, {"test": 538.0}, {"test": 166.0}, {"test": 166.0}]
    rep_ts = {"preA": "2026-07-15T00:00:00Z", "preB": "2026-07-16T00:00:00Z",
              "postC": "2026-07-19T03:00:00Z", "postD": "2026-07-19T05:00:00Z"}
    intervals = {s: _iv(("test", rep_ts[s], rep_ts[s])) for s in repr_shas}
    fact = {**_disclosed_pre_fact(),
            "content_era_by_sha": {"postC": "post", "postD": "post"}}
    kept_shas, kept_checks, _kv, dropped = cr._era_scope_pr_spine_sample(
        repr_shas, per_sha_checks, intervals, rep_ts, {"test"}, [fact], _era_wf_of)
    assert kept_shas == ["preA", "preB"] and dropped == 2      # only the pre `test` survives
    assert [m["test"] for m in kept_checks] == [538.0, 538.0]  # never the 166s post value


def test_spine_door_surgical_keeps_neutral_sibling_workflow_check():
    """Per-workflow surgical cut: a PR dropped-side of the straddling `ci.yml` but ALSO carrying a
    check from a NON-straddling sibling workflow keeps its row — only ci.yml's checks are removed, the
    era-neutral sibling check (a real gate the PR waits on) survives."""
    repr_shas = ["postX"]
    per_sha_checks = [{"guard shard 1/4": 140.0, "lint": 60.0}]      # shard=ci.yml, lint=sibling wf
    rep_ts = {"postX": "2026-07-19T05:00:00Z"}
    intervals = {"postX": _iv(("guard shard 1/4", rep_ts["postX"], "2026-07-19T05:02:00Z"),
                              ("lint", rep_ts["postX"], "2026-07-19T05:01:00Z"))}
    wf_of = lambda n: ".github/workflows/lint.yml" if n == "lint" else _CI
    fact = {**_disclosed_pre_fact(), "content_era_by_sha": {"postX": "post"}}
    kept_shas, kept_checks, kept_iv, dropped = cr._era_scope_pr_spine_sample(
        repr_shas, per_sha_checks, intervals, rep_ts, {"lint", "guard shard 1/4"}, [fact], wf_of)
    assert kept_shas == ["postX"] and dropped == 0
    assert kept_checks == [{"lint": 60.0}]                     # shard cut, lint kept
    assert set(kept_iv["postX"]) == {"lint"}                   # shard interval cut too


def test_spine_door_is_a_noop_when_nothing_straddles():
    """L2 byte-identity: no straddle fact → the ORIGINAL objects are returned unchanged."""
    _pr, repr_shas, per_sha_checks, rep_ts = _website_enum_sample()
    intervals = {}
    ks, kc, kiv, dropped = cr._era_scope_pr_spine_sample(
        repr_shas, per_sha_checks, intervals, rep_ts, {"test"}, [], _era_wf_of)
    assert ks is repr_shas and kc is per_sha_checks and kiv is intervals and dropped == 0


# ── the verify_report guard: three legs re-derive the bind offline ────────────────────────────────

def _spine_doc(fact, checks, chain_facts, chain_summary, sampled_pr_count):
    doc = _enum_doc(fact, checks)
    cp = doc["pr_critical_path"]
    cp["chain_facts"] = chain_facts
    cp["chain_summary"] = chain_summary
    cp["sampled_pr_count"] = sampled_pr_count
    return doc


def test_spine_guard_fails_when_chain_n_exceeds_kept_sampled(tmp_path):
    """Leg 1 (n-bound): chain_summary.n counts MORE PRs than the kept-side sampled_pr_count — the
    per-PR chain layer wasn't era-scoped though the count was (a partial-revert seam)."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": []}
    doc = _spine_doc(
        fact,
        [{"name": "test", "workflow_file": _CI, "p50_s": 538.0, "present_on": 8, "pole_n": 8}],
        [{"sha": f"pre{i}", "chain": ["test"], "makespan_s": 538.0} for i in range(10)],
        {"n": 10, "makespan_p50_s": 538.0, "modal_chain": ["test"], "chain_p50_s": 538.0},
        sampled_pr_count=8)
    chk = vr.check_era_chain_spine_bound_to_kept_era("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "exceeds" in chk.detail


def test_spine_guard_fails_on_dropped_head_carrying_kept_member(tmp_path):
    """Leg 2 (content sha-provenance): a chain fact for a DROPPED (post) head whose chain includes the
    kept (pre) era `test` — post-era timing feeding the pre-claiming makespan (the live #80 blend)."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": [],
            "content_era_by_sha": {"postblend": "post"}}
    doc = _spine_doc(
        fact,
        [{"name": "test", "workflow_file": _CI, "p50_s": 538.0, "present_on": 2, "pole_n": 2}],
        [{"sha": "postblend", "chain": ["test"], "makespan_s": 166.0}],
        {"n": 1, "makespan_p50_s": 166.0, "modal_chain": ["test"], "chain_p50_s": 165.5},
        sampled_pr_count=2)
    chk = vr.check_era_chain_spine_bound_to_kept_era("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "postblend" in chk.detail and "test" in chk.detail


def test_spine_guard_fails_on_makespan_below_unanimous_kept_gate(tmp_path):
    """Leg 3 (makespan physical floor) — the live-shape pin. disclosed_pre, kept `test` p50 538s
    present on ALL sampled PRs, yet chain_summary.makespan_p50_s == 166s (measured on the dropped-era
    fast PRs). A makespan below a unanimous kept gate is physically impossible in the kept era."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": []}
    doc = _spine_doc(
        fact,
        [{"name": "test", "workflow_file": _CI, "p50_s": 538.0, "present_on": 2, "pole_n": 2}],
        [{"sha": "post2", "chain": ["test"], "makespan_s": 166.0},
         {"sha": "post5", "chain": ["test"], "makespan_s": 166.0}],
        {"n": 2, "makespan_p50_s": 166.0, "modal_chain": ["test"], "chain_p50_s": 165.5},
        sampled_pr_count=2)
    chk = vr.check_era_chain_spine_bound_to_kept_era("# r\n", _write(tmp_path, doc), None)
    assert not chk.ok and not chk.skipped, chk.detail
    assert "below" in chk.detail and "unanimous" in chk.detail


def test_spine_guard_passes_when_spine_is_scoped_to_kept(tmp_path):
    """GREEN discriminator: the SAME disclosed_pre straddle after the door — the chain facts are the 8
    kept (pre) PRs, makespan == the pre gate (538s), sampled_pr_count == 8. All three legs pass."""
    fact = {**_disclosed_pre_fact(), "kept_checks": ["test"], "other_era_checks": [],
            "content_era_by_sha": {"post2": "post", "post5": "post"}}
    doc = _spine_doc(
        fact,
        [{"name": "test", "workflow_file": _CI, "p50_s": 538.0, "present_on": 8, "pole_n": 8}],
        [{"sha": f"pre{d}", "chain": ["test"], "makespan_s": 538.0} for d in range(11, 19)],
        {"n": 8, "makespan_p50_s": 538.0, "modal_chain": ["test"], "chain_p50_s": 538.0},
        sampled_pr_count=8)
    doc["pr_critical_path"]["era_dropped_pr_count"] = 2
    chk = vr.check_era_chain_spine_bound_to_kept_era("# r\n", _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_spine_guard_runs_on_era_fact_without_enumeration_sets(tmp_path):
    """An era fact activates the spine guard; absent enumeration sets make its dependent legs
    vacuous but must not turn the rendered era state into a verifier SKIP."""
    doc = _spine_doc(_disclosed_pre_fact(),
                     [{"name": "test", "workflow_file": _CI, "p50_s": 538.0, "present_on": 8}],
                     [], {"n": 0}, sampled_pr_count=8)
    chk = vr.check_era_chain_spine_bound_to_kept_era("# r\n", _write(tmp_path, doc), None)
    assert chk.ok and not chk.skipped, chk.detail


def test_spine_guard_skips_when_nothing_straddled(tmp_path):
    doc = _era_doc()                                    # config_eras == []
    chk = vr.check_era_chain_spine_bound_to_kept_era("# report\n", _write(tmp_path, doc), None)
    assert chk.ok and chk.skipped


def test_spine_guard_is_registered():
    names = {c.name for c in vr.run_checks("# x\n", None, None, skill_repo=None)}
    assert ("the per-PR chain/makespan spine is bound to the kept config era "
            "(no dropped-era PR blends in)") in names

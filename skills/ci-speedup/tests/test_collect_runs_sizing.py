"""Unit tests for the collect_runs sizing guardrails (Phase 4).

Guardrail A — D12 serial-gate: patterns whose fix inserts a build-once / fan-out
serial gate (OPT14/OPT15) are wall-clock-NEGATIVE and must never rank as a
Tier-1 wall-clock lever, but still carry their runner-minute (bill) saving.

Guardrail B — below-cluster-floor: a finding whose affected job finishes at/below
the cluster floor (i.e. not the long pole) saves runner-minutes but ZERO
wall-clock, relative to the repo's own critical path.

These call _size_finding directly (ci-speedup/scripts is on pythonpath via
pyproject) so the guardrail math is pinned independent of the gh pass.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_collect_runs_sizing.py
"""

from __future__ import annotations

from collect_runs import (
    _bimodal_split,
    _cap_wall_clock_cross_workflow,
    _concurrent_workflows,
    _critical_path,
    _DEVELOPER_EVENTS,
    _developer_event,
    _cap_opt19_wall_clock,
    _normalize_pin,
    _PR_VOLUME_EVENTS,
    _resolve_job_p50,
    _sample_runs,
    _SERIAL_GATE_PATTERNS,
    _size_finding,
    _volume_is_ci_clean,
    _window_30d,
)


def _job(name, dur_s, labels):
    return {"name": name, "labels": labels,
            "started_at": "2026-05-29T10:00:00Z",
            "completed_at": f"2026-05-29T10:{dur_s // 60:02d}:{dur_s % 60:02d}Z"}


def test_critical_path_scopes_each_job_to_its_own_dominant_runner():
    # The `test` job runs mostly on the fast runner (4x arm 100s) but
    # occasionally on the slow one (1x latest 450s); the prisma job runs on
    # arm (300s). A blended `test` p50 would be inflated and could
    # outrank prisma (the better-auth bug). Per-job dominant-runner scoping
    # measures `test` on arm (100s) → prisma (300s) is the true long pole.
    runs = []
    for _ in range(4):
        runs.append([_job("prisma Integration Test", 300, ["ubuntu-24.04-arm"]),
                     _job("test", 100, ["ubuntu-24.04-arm"])])
    runs.append([_job("prisma Integration Test", 300, ["ubuntu-24.04-arm"]),
                 _job("test", 450, ["ubuntu-latest"])])
    crit = _critical_path(runs)
    assert crit["long_pole_job"] == "prisma Integration Test"
    assert crit["long_pole_p50"] == 300.0
    assert crit["job_p50"]["test"] == 100.0          # measured on arm, not blended
    assert crit["runner_scope"] == "ubuntu-24.04-arm"


def test_critical_path_handles_heavy_and_light_jobs_on_different_runners():
    # Heavy jobs on an arm runner, light aggregator on ubuntu-latest (the real
    # better-auth shape). Each job is measured on its OWN runner; the light
    # aggregator doesn't drag the long pole and isn't dropped.
    runs = []
    for _ in range(3):
        runs.append([
            _job("prisma Integration Test", 300, ["ubuntu-24.04-arm"]),
            _job("ci", 3, ["ubuntu-latest"]),   # alls-green aggregator
        ])
    crit = _critical_path(runs)
    assert crit["long_pole_job"] == "prisma Integration Test"
    assert crit["job_p50"]["ci"] == 3.0          # light job kept, measured on its runner


def test_developer_event_prefers_pull_request():
    # PR runs drive the wall-clock critical path; pull_request wins over push.
    assert _developer_event({"pull_request": [[]], "push": [[]]}) == "pull_request"
    assert _developer_event({"merge_group": [[]], "push": [[]]}) == "merge_group"
    # ORDERING precedence (the fix #4 design decision): a workflow firing on BOTH
    # pull_request AND pull_request_target scopes to the PRIMARY `pull_request` — _DEVELOPER_EVENTS
    # is deliberately ordered pull_request → pull_request_target → merge_group. A reorder would
    # silently rescope such a workflow to its PRT runs; pin it here.
    assert _developer_event({"pull_request": [[]], "pull_request_target": [[]]}) == "pull_request"
    # pull_request_target alone (the labeler case) is still a developer event when it's the only one.
    assert _developer_event({"pull_request_target": [[]], "push": [[]]}) == "pull_request_target"


def test_developer_event_none_when_only_non_developer_facing():
    # push/schedule-only workflow → no developer-facing event → fall back to all.
    assert _developer_event({"push": [[]], "schedule": [[]]}) is None
    assert _developer_event({}) is None


def test_volume_is_ci_clean_excludes_chatter_triggered_workflows():
    # A workflow's 30d total_count is only a trustworthy CI-run proxy when none
    # of its triggers are human-chatter events that dwarf CI volume. This is the
    # guard that stops an issue_comment workflow (e.g. mastra's
    # major-version-check.yml at ~45.6k comment-driven runs/30d) from becoming
    # the run-share denominator and deflating every other workflow's share.
    assert _volume_is_ci_clean({"pull_request"})
    assert _volume_is_ci_clean({"pull_request", "merge_group", "push"})
    assert _volume_is_ci_clean({"push", "schedule", "workflow_dispatch"})
    assert _volume_is_ci_clean(None)
    assert _volume_is_ci_clean(set())
    # contaminated: a CI trigger alongside a chatter trigger
    assert not _volume_is_ci_clean({"pull_request", "issue_comment"})
    assert not _volume_is_ci_clean({"pull_request_review"})
    assert not _volume_is_ci_clean({"push", "discussion_comment"})


def test_pr_volume_events_include_pull_request_target():
    # The run-share DENOMINATOR ("busiest PR workflow") must count
    # pull_request_target — a PR also waits on those checks (fork PRs, triage).
    # Omitting it let a busy pull_request_target workflow's volume EXCEED the
    # chosen denominator, yielding a nonsensical run-share > 100% (mastra
    # pr-triage at 9.5k runs once a contaminated workflow was excluded).
    assert "pull_request_target" in _PR_VOLUME_EVENTS
    # UN-MASKED pin (greptile P2): `_PR_VOLUME_EVENTS` keeps `pull_request_target` via an explicit
    # `| {"pull_request_target"}` union, so the assertion above stays true even if PRT were removed
    # from `_DEVELOPER_EVENTS` — masking the regression where `_crit_for` (which reads `_DEVELOPER_
    # EVENTS` directly) would silently revert PRT workflows to all-events blending (the fix #4 bug).
    # Pin the SOURCE so a removal fails loudly here, not silently in critical-path scoping.
    assert "pull_request_target" in _DEVELOPER_EVENTS
    assert set(_DEVELOPER_EVENTS).issubset(_PR_VOLUME_EVENTS)
    # chatter/non-PR events still don't belong in the PR-volume reference
    assert "issue_comment" not in _PR_VOLUME_EVENTS
    assert "push" not in _PR_VOLUME_EVENTS

# Mirrors the real mastra PR fan-out: four workflows on pull_request + one
# publish workflow on a different trigger.
_PR_EVENTS = {
    "changed-test-gate.yml": {"pull_request"},
    "e2e-docs.yml": {"pull_request"},
    "prebuild.yml": {"pull_request"},
    "npm-publish.yml": {"push", "release"},
}
_WF_CRIT = {
    "changed-test-gate.yml": {"long_pole_p50": 410.0},
    "e2e-docs.yml": {"long_pole_p50": 344.0},
    "prebuild.yml": {"long_pole_p50": 327.0},
    "npm-publish.yml": {"long_pole_p50": 1208.0},
}


def test_concurrent_workflows_only_shares_trigger():
    # npm-publish is slower (1208s) but fires on push/release, NOT pull_request,
    # so it must NOT count as concurrent with a PR workflow.
    conc = _concurrent_workflows("changed-test-gate.yml", _PR_EVENTS, _WF_CRIT)
    names = [w for w, _ in conc]
    assert "npm-publish.yml" not in names
    assert names == ["e2e-docs.yml", "prebuild.yml"]  # slowest sibling first


def test_concurrent_workflows_counts_triaged_sibling_via_wall_fallback():
    # A run-list-TRIAGED sibling has `long_pole_p50 == 0` (no job sample) but still runs
    # concurrently on the PR, so it must keep setting the cross-workflow floor via its
    # `concurrent_wall_p50` fallback — else shortening the gate could overstate the saving
    # down to ~0 instead of flooring at the still-running fast sibling.
    events = {"slow-tests.yml": {"pull_request"}, "lint.yml": {"pull_request"}}
    crit = {"slow-tests.yml": {"long_pole_p50": 600.0},
            # triaged: drilled long pole is 0, but it ran ~80s wall alongside the gate.
            "lint.yml": {"long_pole_p50": 0.0, "concurrent_wall_p50": 80.0}}
    conc = _concurrent_workflows("slow-tests.yml", events, crit)
    assert conc == [("lint.yml", 80.0)]                 # triaged sibling still counted
    capped, note = _cap_wall_clock_cross_workflow(600.0, "slow-tests.yml", crit["slow-tests.yml"], conc)
    assert capped == 520.0                              # 600 - 80 floor, NOT the full 600
    assert "cross-workflow floor" in note and "lint.yml" in note


def test_cross_workflow_cap_floors_at_slowest_sibling():
    # changed-test-gate IS the slowest PR workflow (410s), so it gates the PR —
    # but sharding it only saves down to the next sibling (e2e-docs 344s) = 66s,
    # not the full 205s per-workflow figure.
    conc = _concurrent_workflows("changed-test-gate.yml", _PR_EVENTS, _WF_CRIT)
    capped, note = _cap_wall_clock_cross_workflow(
        205.0, "changed-test-gate.yml", _WF_CRIT["changed-test-gate.yml"], conc)
    assert capped == 66.0
    assert "cross-workflow floor" in note and "e2e-docs.yml" in note


def test_cross_workflow_cap_zeroes_non_gate_workflow():
    # prebuild (327s) runs alongside changed-test-gate (410s), which gates the
    # PR — so shortening prebuild saves ZERO developer wall-clock.
    conc = _concurrent_workflows("prebuild.yml", _PR_EVENTS, _WF_CRIT)
    capped, note = _cap_wall_clock_cross_workflow(
        160.0, "prebuild.yml", _WF_CRIT["prebuild.yml"], conc)
    assert capped == 0.0
    assert "no wall-clock saving" in note and "changed-test-gate.yml" in note


def test_cross_workflow_cap_noop_without_concurrent_siblings():
    # A workflow that fires on a trigger nothing else shares keeps its saving.
    capped, note = _cap_wall_clock_cross_workflow(
        100.0, "npm-publish.yml", _WF_CRIT["npm-publish.yml"], [])
    assert capped == 100.0
    assert note == ""


def test_window_30d_unpinned_has_no_upper_bound():
    # Unpinned: window ends "now", only a lower bound is expressed (original
    # behavior — the volume query stays `created>=now-30d`).
    since, upper = _window_30d(None)
    assert upper is None
    assert since.endswith("Z")


def test_window_30d_pinned_ends_at_the_pin():
    # Pinned: the 30-day window ENDS at the pin, so a regen samples the exact
    # same window the original audit did instead of drifting forward.
    since, upper = _window_30d("2026-05-31T18:28:55Z")
    assert upper == "2026-05-31T18:28:55Z"
    assert since == "2026-05-01T18:28:55Z"  # pin − 30d


# =============================================================================
# Bug 2 — the `_sample_runs` pin must be Z-normalized before it hits the gh
# query string. The raw `scanned_at = datetime.now(timezone.utc).isoformat()`
# pin ends `+00:00`; an unencoded `+` in a query decodes as a space, silently
# breaking the `created=` filter so a "pinned" regen samples a different run
# window than the (correctly Z-normalized) volume window.
# =============================================================================

class _FakeClient:
    """Duck-types GhClient.json(), capturing the endpoint string it was called
    with instead of hitting the network."""

    def __init__(self) -> None:
        self.endpoint: str | None = None

    def json(self, endpoint: str, allow_missing: bool = False) -> dict:
        self.endpoint = endpoint
        return {"workflow_runs": []}


def test_normalize_pin_strips_plus_and_z_normalizes():
    assert _normalize_pin("2026-07-01T12:00:00+00:00") == "2026-07-01T12:00:00Z"
    assert _normalize_pin("2026-07-01T12:00:00Z") == "2026-07-01T12:00:00Z"
    assert _normalize_pin(None) is None
    assert _normalize_pin("") is None
    assert _normalize_pin("not-a-timestamp") is None  # malformed → no pin, not a crash


def test_normalize_pin_converts_offset_to_utc():
    # A non-UTC pin must be CONVERTED to UTC before the `Z` is appended, not have
    # its wall-clock fields relabeled. `12:00:00+05:00` is `07:00:00Z`, so the
    # sampling window lands on the correct instant (else it's off by the offset,
    # up to ±14h).
    assert _normalize_pin("2026-07-01T12:00:00+05:00") == "2026-07-01T07:00:00Z"
    assert _normalize_pin("2026-07-01T12:00:00-08:00") == "2026-07-01T20:00:00Z"


def test_normalize_pin_accepts_lowercase_z():
    # A lowercase `z` suffix is a valid ISO 8601 UTC designator; it must round-trip
    # to a pinned value, not silently degrade to unpinned (None).
    assert _normalize_pin("2026-07-01T12:00:00z") == "2026-07-01T12:00:00Z"


def test_sample_runs_pin_is_z_normalized_no_plus():
    client = _FakeClient()
    _sample_runs(client, "owner/repo", 42, 20, created_before="2026-07-01T12:00:00+00:00")
    assert client.endpoint is not None
    assert "created=<=2026-07-01T12:00:00Z" in client.endpoint
    assert "+" not in client.endpoint


def test_sample_runs_unpinned_has_no_created_clause():
    client = _FakeClient()
    _sample_runs(client, "owner/repo", 42, 20, created_before=None)
    assert client.endpoint is not None
    assert "created=" not in client.endpoint

# A repo whose long pole is `test` (400s) with a cluster floor of 100s.
_CRIT = {
    "long_pole_job": "test",
    "long_pole_p50": 400.0,
    "floor_p50": 100.0,
    "job_p50": {"lint": 50.0, "typecheck": 100.0, "test": 400.0},
}


def test_serial_gate_is_wall_clock_negative_but_keeps_bill():
    f = {"pattern": "OPT14", "affected_jobs": ["lint", "test"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["tier"] == 2
    assert f["runner_min_saving"] and f["runner_min_saving"] > 0
    assert "NEGATIVE" in f["size_note"]
    assert "OPT14" in _SERIAL_GATE_PATTERNS


def test_below_floor_job_saves_bill_not_wall_clock():
    # `lint` (50s) finishes below the 100s floor → fixing it can't move the
    # 400s long pole. Runner-minute saving only.
    f = {"pattern": "OPT61", "affected_jobs": ["lint"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["tier"] == 2
    assert f["runner_min_saving"] and f["runner_min_saving"] > 0
    assert "below the cluster floor" in f["size_note"]


def test_long_pole_job_keeps_wall_clock_credit():
    # `test` IS the long pole (400s > 100s floor) → wall-clock credited.
    f = {"pattern": "OPT61", "affected_jobs": ["test"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] > 0
    assert f["tier"] == 1
    assert f["realization"] == "direct"


def test_second_pole_at_floor_is_demoted():
    # `typecheck` (100s) == floor → fixing it doesn't move wall-clock.
    f = {"pattern": "OPT61", "affected_jobs": ["typecheck"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["tier"] == 2


def test_no_timing_credits_no_wall_clock():
    # No run timing sampled at all (static-only report — empty critical path). We
    # cannot prove the affected job is the long pole, so wall-clock must NOT be
    # credited: a positive wall_clock_p50_s is the report's on-critical-path signal
    # (blocking_path._saves_wall_clock), and a static-only report renders NO spine,
    # so crediting the nominal estimate made it claim "ON the merge-gating critical
    # path / See the spine above" against a spine that doesn't exist.
    f = {"pattern": "OPT61", "affected_jobs": ["whatever"]}
    crit = {"long_pole_job": "", "long_pole_p50": 0.0, "floor_p50": 0.0,
            "job_p50": {}}
    _size_finding(f, crit, monthly_volume=None)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["tier"] == 2
    assert f["realization"] == "none"
    assert "wall-clock unproven" in f["size_note"]


def test_direct_unresolvable_affected_job_credits_bill_not_wall_clock():
    # A 'direct' finding names a job that resolves to NO sampled timing (a
    # reusable-workflow caller / renamed job). We can't confirm it's the long pole,
    # so wall-clock must NOT be credited off the global pole — bill saving only.
    f = {"pattern": "OPT17", "affected_jobs": ["ghost-job"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["tier"] == 2
    assert f["runner_min_saving"] and f["runner_min_saving"] > 0   # bill stands
    assert "couldn't be resolved" in f["size_note"]


def test_direct_workflow_level_finding_still_credits_wall_clock():
    # An EMPTY affected_jobs (a workflow-level direct finding) is NOT the
    # unresolvable-job case — it still sizes off the global headroom as before.
    f = {"pattern": "OPT17", "affected_jobs": []}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] > 0


def test_parallel_rebalance_sizes_off_flagged_job_not_global_pole():
    # OPT23 on a 200s job while the workflow long pole is 400s: the halving must be
    # off the FLAGGED job (200/2 = 100), not the global pole (400/2 = 200).
    crit = {"long_pole_job": "build", "long_pole_p50": 400.0, "floor_p50": 50.0,
            "job_p50": {"build": 400.0, "slow-suite": 200.0}}
    f = {"pattern": "OPT23", "affected_jobs": ["slow-suite"]}
    _size_finding(f, crit, monthly_volume=None)
    assert f["wall_clock_p50_s"] == 100.0   # 200/2, not the old 400/2 = 200


def test_parallel_rebalance_below_floor_saves_no_wall_clock():
    # OPT23 on a job at/below the cluster floor isn't the pole — rebalancing it
    # saves zero wall-clock (it was credited long_pole/2 before).
    crit = {"long_pole_job": "build", "long_pole_p50": 400.0, "floor_p50": 150.0,
            "job_p50": {"build": 400.0, "small-suite": 100.0}}
    f = {"pattern": "OPT23", "affected_jobs": ["small-suite"]}
    _size_finding(f, crit, monthly_volume=None)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["tier"] == 2
    assert "saves no wall-clock" in f["size_note"]


def test_parallel_rebalance_unresolvable_affected_job_is_unproven():
    # OPT23 names a job that resolves to no sampled timing — mirror the direct
    # model: don't credit wall-clock off the global pole (it would overstate).
    crit = {"long_pole_job": "build", "long_pole_p50": 400.0, "floor_p50": 100.0,
            "job_p50": {"build": 400.0}}
    f = {"pattern": "OPT23", "affected_jobs": ["ghost-suite"]}
    _size_finding(f, crit, monthly_volume=None)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["tier"] == 2
    assert "unproven" in f["size_note"]


def test_opt19_wall_clock_capped_to_global_long_pole():
    # OPT19's summed source-sleep total (600s) can't exceed the repo's longest
    # measured job (300s) — you can't save more wall-clock than the slowest run.
    f = {"pattern": "OPT19", "wall_clock_p50_s": 600.0}
    _cap_opt19_wall_clock(f, global_long_pole_p50=300.0)
    assert f["wall_clock_p50_s"] == 300.0
    assert f["wall_clock_uncapped_p50_s"] == 600.0
    assert "longest measured job" in f["size_note"]


def test_opt19_under_pole_is_untouched_and_no_pole_is_noop():
    # A total that already fits under the bound is left as-is...
    f = {"pattern": "OPT19", "wall_clock_p50_s": 120.0}
    _cap_opt19_wall_clock(f, global_long_pole_p50=300.0)
    assert f["wall_clock_p50_s"] == 120.0
    assert "wall_clock_uncapped_p50_s" not in f
    # ...and with no measured pole, the static estimate stands (can't cap honestly).
    g = {"pattern": "OPT19", "wall_clock_p50_s": 600.0}
    _cap_opt19_wall_clock(g, global_long_pole_p50=0.0)
    assert g["wall_clock_p50_s"] == 600.0
    # non-OPT19 findings are never touched
    h = {"pattern": "OPT24", "wall_clock_p50_s": 600.0}
    _cap_opt19_wall_clock(h, global_long_pole_p50=100.0)
    assert h["wall_clock_p50_s"] == 600.0


def test_matrix_job_name_resolves_to_display_name():
    # YAML key `unit` vs GitHub display names `unit (shard 1)` / `unit (shard 2)`.
    job_p50 = {"unit (shard 1)": 60.0, "unit (shard 2)": 80.0, "build": 400.0}
    assert _resolve_job_p50("unit", job_p50) == 80.0   # max over matrix legs
    assert _resolve_job_p50("build", job_p50) == 400.0  # exact match
    assert _resolve_job_p50("missing", job_p50) == 0.0  # unresolvable → 0


def test_below_floor_demote_works_on_matrix_jobs():
    # Regression: affected_jobs are YAML keys, job_p50 keyed by display names.
    # `unit` legs (60/80s) sit below the 100s floor → runner-min only.
    crit = {"long_pole_job": "build", "long_pole_p50": 400.0, "floor_p50": 100.0,
            "job_p50": {"unit (shard 1)": 60.0, "unit (shard 2)": 80.0, "build": 400.0}}
    f = {"pattern": "OPT61", "affected_jobs": ["unit"]}
    _size_finding(f, crit, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["tier"] == 2


def test_parallel_rebalance_model():
    # OPT23 (single-threaded matrix) → parallel-rebalance: wall-clock-positive,
    # Tier-1, zero runner-min, capped at the floor headroom.
    f = {"pattern": "OPT23", "affected_jobs": ["test"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == min(400.0 / 2.0, 400.0 - 100.0)  # 200
    assert f["runner_min_saving"] == 0.0
    assert f["tier"] == 1
    assert f["realization"] == "direct"


def test_runner_min_only_model():
    # OPT45 (missing concurrency) → runner-min-only whole-run cancel: zero
    # wall-clock, tier 2. The provisional (no-spine) bill sizes off the SUM of
    # the affected jobs' own p50 (× hit_rate × volume / 60), never the workflow
    # long pole (#33). Here affected = ["test"] (p50 400): 0.2 * 400 * 1000 / 60.
    f = {"pattern": "OPT45", "affected_jobs": ["test"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["tier"] == 2
    assert f["runner_min_saving"] == round(0.2 * 400.0 * 1000 / 60.0, 1)


def test_opt12_is_maintainability_only_zero_savings():
    # A composite-action dedup changes ZERO runtime — neither wall-clock nor
    # runner-minutes. It must book 0 (not a fictional bill saving) with an
    # honest maintainability-only note.
    f = {"pattern": "OPT12", "affected_jobs": ["lint", "test"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == 0.0
    assert f["runner_min_saving"] == 0.0
    assert "maintainability only" in f["size_note"]
    assert "below the cluster floor" not in f["size_note"]


def test_unknown_pattern_degrades_qualitatively():
    f = {"pattern": "OPT999", "affected_jobs": ["test"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] is None
    assert f["runner_min_saving"] is None
    assert "no sizing model" in f["size_note"]


def test_measured_model_preserves_inline_axes():
    # OPT19 is sized inline by scan.py; the "measured" model must not clobber it.
    f = {"pattern": "OPT19", "affected_jobs": [], "wall_clock_p50_s": 344.1,
         "runner_min_saving": None, "tier": 1, "realization": "direct"}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == 344.1  # preserved, not overwritten
    assert f["tier"] == 1


def test_opt40_runner_min_scoped_to_affected_job_not_long_pole():
    # OPT40 skips ONE small gate job, so its bill saving must be sized off that
    # job's OWN p50, not the workflow long pole. `peerdeps-check` (40s) must NOT
    # be credited with the 400s long pole (the old bug → ~9,300 min/mo).
    crit = {"long_pole_job": "test", "long_pole_p50": 400.0, "floor_p50": 100.0,
            "job_p50": {"peerdeps-check": 40.0, "test": 400.0}}
    f = {"pattern": "OPT40", "affected_jobs": ["peerdeps-check"]}
    _size_finding(f, crit, monthly_volume=1000)
    assert f["wall_clock_p50_s"] == 0.0
    # bill = hit_rate(0.3) * own_p50(40) * volume(1000) / 60 = 200, NOT
    # 0.5 * 400 * 1000 / 60 = 3333 (long-pole basis).
    assert f["runner_min_saving"] == round(0.3 * 40.0 * 1000 / 60.0, 1)


def test_opt40_unresolvable_job_renders_qualitatively_not_long_pole():
    # If the affected job can't be located in job_p50 (reusable-workflow caller
    # or unmappable name), DON'T substitute the workflow long pole — render
    # qualitatively (rm=None) so the report never shows a long-pole-inflated
    # per-job number. Omit rather than fake.
    crit = {"long_pole_job": "test", "long_pole_p50": 400.0, "floor_p50": 100.0,
            "job_p50": {"test": 400.0}}
    f = {"pattern": "OPT40", "affected_jobs": ["renamed-job"]}
    _size_finding(f, crit, monthly_volume=1000)
    assert f["runner_min_saving"] is None
    assert "couldn't be resolved" in f["size_note"]


def test_resolve_job_p50_matches_name_overridden_key():
    # A `name:`-override means the YAML key (`integration`) differs from the
    # GitHub display name ("Integration test"). Resolve via normalized leading
    # word so per-job sizing works instead of falling back to long pole.
    from collect_runs import _resolve_job_p50
    jp = {"Integration test": 72.5, "Smoke test (22.x)": 90.5,
          "Smoke test (24.x)": 88.0, "Workspace Tests / test-docker (azure)": 200.0}
    assert _resolve_job_p50("integration", jp) == 72.5
    assert _resolve_job_p50("smoke", jp) == 90.5          # max over the two legs
    # A reusable-workflow CALLER ("Workspace Tests / …" legs) stays UNRESOLVED —
    # sizing it by one leg would be muddy, so it renders qualitatively instead.
    assert _resolve_job_p50("workspace-tests", jp) == 0.0
    assert _resolve_job_p50("nonexistent", jp) == 0.0


def test_job_scoped_unresolved_with_no_timings_is_qualitative_not_zero():
    # Regression: when NO run timings were sampled (long_pole falsy) but the
    # workflow has a monthly volume, a job-scoped finding whose job can't be
    # resolved must render qualitatively (rm=None), NOT a confident 0.0.
    crit = {"long_pole_job": "", "long_pole_p50": 0.0, "floor_p50": 0.0,
            "job_p50": {}}
    f = {"pattern": "OPT33", "affected_jobs": ["some-job"]}
    _size_finding(f, crit, monthly_volume=1000)
    assert f["runner_min_saving"] is None
    assert "couldn't be resolved" in f["size_note"]


def test_opt33_sized_by_own_job_not_long_pole():
    # Regression for the dominant audit defect: OPT33 (and OPT30/31/34/39) skip
    # ONE job, so each is sized by its own duration — not the workflow long pole
    # that made N findings show identical inflated runner-min.
    crit = {"long_pole_job": "adapter-integration", "long_pole_p50": 307.0,
            "floor_p50": 90.0,
            "job_p50": {"Smoke test (22.x)": 90.5, "Integration test": 72.5,
                        "adapter-integration": 307.0}}
    smoke = {"pattern": "OPT33", "affected_jobs": ["smoke"]}
    integ = {"pattern": "OPT33", "affected_jobs": ["integration"]}
    _size_finding(smoke, crit, monthly_volume=1305)
    _size_finding(integ, crit, monthly_volume=1305)
    # Each differs (own duration), NOT both == 0.3*307*1305/60 = 2003.
    assert smoke["runner_min_saving"] == round(0.3 * 90.5 * 1305 / 60.0, 1)
    assert integ["runner_min_saving"] == round(0.3 * 72.5 * 1305 / 60.0, 1)
    assert smoke["runner_min_saving"] != integ["runner_min_saving"]


def test_ephemeral_cache_caveat_appended_for_suspect_patterns():
    # OPT3/8/9 carry the warm-cache caveat so the report never overstates an
    # ephemeral-runner wall-clock saving.
    for pat in ("OPT3", "OPT8", "OPT9"):
        f = {"pattern": pat, "affected_jobs": ["test"]}
        _size_finding(f, _CRIT, monthly_volume=1000)
        assert "ephemeral-runner caveat" in f["size_note"], pat


def test_non_suspect_pattern_has_no_ephemeral_caveat():
    f = {"pattern": "OPT61", "affected_jobs": ["test"]}
    _size_finding(f, _CRIT, monthly_volume=1000)
    assert "ephemeral-runner caveat" not in f.get("size_note", "")


def test_runner_min_demoted_for_unrealizable_patterns():
    # The adversarial-review headline-inflation fix: a finding's credited
    # runner_min_saving must reflect only REALIZABLE compute savings. Patterns
    # whose modeled bill saving isn't realizable are demoted out of the credited
    # field (→ None, so the bill headline / Also-noticed appendix skip them) with the
    # modeled amount preserved for annotated display.
    #   OPT21 — needs-removal is queue-wait, not compute (M3)
    #   OPT3/OPT9 — warm-local-cache unproven on ephemeral runners (M4/M5)
    #   OPT32 — missing paths filter is a gross upper bound (B3)
    for pat in ("OPT21", "OPT3", "OPT9", "OPT32"):
        f = {"pattern": pat, "affected_jobs": ["changed-tests"]}
        _size_finding(f, _CRIT, monthly_volume=4950)
        assert f["runner_min_saving"] is None, pat
        assert f["runner_min_unrealizable"] is True, pat
        assert (f.get("runner_min_unrealizable_s") or 0) > 0, pat
        assert f.get("runner_min_note"), pat


def test_realizable_runner_min_pattern_is_not_demoted():
    # A genuine runner-min bill saving (e.g. OPT2 caching a download — remote
    # actions/cache persists across ephemeral runners) keeps its credited value.
    f = {"pattern": "OPT2", "affected_jobs": ["test"]}
    _size_finding(f, _CRIT, monthly_volume=4950)
    assert f["runner_min_saving"] and f["runner_min_saving"] > 0
    assert not f.get("runner_min_unrealizable")


def _opt24_job(name, secs):
    mm, ss = divmod(int(secs), 60)
    return {"name": name, "html_url": "http://x",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": f"2026-01-01T00:{mm:02d}:{ss:02d}Z"}


def _opt24_job_with_steps(name, steps):
    """An OPT24 job whose wall-clock is decomposed into back-to-back steps.
    `steps` is a list of (step_name, seconds); job duration = their sum."""
    out_steps = []
    t = 0
    for sname, secs in steps:
        mm0, ss0 = divmod(int(t), 60)
        t += int(secs)
        mm1, ss1 = divmod(int(t), 60)
        out_steps.append({
            "name": sname,
            "started_at": f"2026-01-01T00:{mm0:02d}:{ss0:02d}Z",
            "completed_at": f"2026-01-01T00:{mm1:02d}:{ss1:02d}Z",
        })
    mm, ss = divmod(int(t), 60)
    return {"name": name, "html_url": "http://x",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": f"2026-01-01T00:{mm:02d}:{ss:02d}Z",
            "steps": out_steps}


def test_opt24_sizes_only_the_shardable_payload_not_the_serial_build():
    # Infisical/infisical regression: a test job dominated by a SERIAL build step
    # (`Build FIPS test image`, ~995s) followed by the actual test step
    # (`Run integration test`, ~533s). Sharding re-runs the build on every shard,
    # so it can't be halved — only the test payload shards. The saving must be
    # capped at half the payload (~266s), NOT half the whole 1528s job (~764s),
    # which would contradict OPT72's "the build is the dominant step" decomposition
    # of the same job (in the real Infisical run the build was ~53% of the job; this
    # synthetic two-step repro makes it 995/1528 ≈ 65% to land the payload halving
    # cleanly on 533/2 = 266.5).
    from collect_runs import _detect_opt24_long_test_no_sharding
    job = lambda: _opt24_job_with_steps(
        "Run integration test",
        [("Build FIPS test image", 995), ("Run integration test", 533)])
    runs = [[job()] for _ in range(6)]
    out = _detect_opt24_long_test_no_sharding("run-backend-tests.yml", runs, 0,
                                              monthly_volume=300)
    assert len(out) == 1
    wc = out[0]["wall_clock_p50_s"]
    # Half the shardable payload (533/2), never half the whole job (1528/2=764).
    assert wc == 266.5, wc
    assert wc < 764.0


def test_opt24_skips_dynamic_changed_file_gate():
    # B2/M2 regression: `changed-tests` diffs base..head and runs only affected
    # tests — no fixed suite to shard, and it self-skips (~65s) on docs PRs. It
    # must NOT be flagged OPT24 (never headline a -Ns sharding saving on a job
    # sharding can't touch); a steady shardable suite (Docs E2E) still is.
    from collect_runs import _detect_opt24_long_test_no_sharding
    runs = [[_opt24_job("changed-tests", s), _opt24_job("Docs E2E tests", 344)]
            for s in [752, 760, 745, 750, 740, 755, 65, 70, 748, 752]]
    out = _detect_opt24_long_test_no_sharding("changed-test-gate.yml", runs, 0,
                                              monthly_volume=1593)
    flagged = [j for f in out for j in f["affected_jobs"]]
    assert "changed-tests" not in flagged
    assert "Docs E2E tests" in flagged


def test_opt24_skips_bimodal_job_even_without_gate_name():
    # Name-independent backstop: a job with a large self-skip cluster is dynamic,
    # not a steady suite — skip regardless of name.
    from collect_runs import _detect_opt24_long_test_no_sharding
    runs = [[_opt24_job("integration test", s)]
            for s in [600, 620, 610, 590, 605, 615, 40, 45, 38, 42]]  # ~40% short
    out = _detect_opt24_long_test_no_sharding("ci.yml", runs, 0, monthly_volume=100)
    assert out == []


def test_sharded_bases_detects_split_axis_and_pytest_split_command():
    # reflex-dev/reflex regression (b1): `integration-app-harness` is already sharded
    # via BOTH a numeric `split_index` matrix axis AND pytest-split (`--splits 2
    # --group`), neither of which leaves a literal `shard`/`partition` token in the
    # rendered job name. Its `-playwright` sibling has neither, so it must NOT be
    # reported as sharded.
    from collect_runs import _sharded_bases
    doc = {"jobs": {
        "integration-app-harness": {
            "strategy": {"matrix": {"state_manager": ["redis", "memory"],
                                    "python-version": ["3.12"],
                                    "split_index": [1, 2]}},
            "steps": [{"name": "Run app harness tests",
                       "run": "uv run pytest tests/integration --reruns 3 "
                              "--splits 2 --group ${{matrix.split_index}}"}],
        },
        "integration-app-harness-playwright": {
            "strategy": {"matrix": {"state_manager": ["redis", "memory"],
                                    "python-version": ["3.12"]}},
            "steps": [{"name": "Run playwright tests",
                       "run": "uv run pytest tests/integration/tests_playwright "
                              "--reruns 3 --maxfail=5"}],
        },
    }}
    sharded = _sharded_bases(doc)
    assert "integration-app-harness" in sharded
    assert "integration-app-harness-playwright" not in sharded


def test_opt24_suppressed_for_already_sharded_base_keeps_unsharded_sibling():
    # The detector must honor the YAML-derived sharded set: a pytest-split-sharded base
    # is NOT flagged "no shard axis observed", while a genuinely unsharded sibling in
    # the same workflow still is. (Pairs with the parser test above.)
    from collect_runs import _detect_opt24_long_test_no_sharding
    runs = [[_opt24_job("integration-app-harness", h),
             _opt24_job("integration-app-harness-playwright", p)]
            for h, p in [(430, 360), (440, 365), (420, 355), (450, 362), (410, 358),
                         (460, 366), (425, 359), (435, 361), (445, 357), (415, 363)]]
    out = _detect_opt24_long_test_no_sharding(
        "integration_app_harness.yml", runs, 0, monthly_volume=300,
        sharded_bases={"integration-app-harness"})
    flagged = [j for f in out for j in f["affected_jobs"]]
    assert "integration-app-harness" not in flagged          # already sharded → suppressed
    assert "integration-app-harness-playwright" in flagged    # genuinely unsharded → kept


def test_numeric_shard_matrix_legs_recognizes_iN_but_not_version_or_config():
    # The (i, N) numeric-shard recognizer must catch a real shard axis while leaving a
    # version/OS/config matrix (which OPT24 legitimately treats) alone — soundness.
    from collect_runs import _numeric_shard_matrix_legs
    shard = [[{"name": f"Unit Tests: Internal ({i}, 8)"} for i in (2, 8)]]
    assert _numeric_shard_matrix_legs(shard) == {
        "Unit Tests: Internal (2, 8)", "Unit Tests: Internal (8, 8)"}
    # `(i/N)` slash form is the same shard render.
    assert _numeric_shard_matrix_legs(
        [[{"name": "e2e (1/4)"}, {"name": "e2e (3/4)"}]]) == {"e2e (1/4)", "e2e (3/4)"}
    # A node-version matrix is NOT a numeric shard (single non-integer component).
    assert _numeric_shard_matrix_legs(
        [[{"name": "test (22.x)"}, {"name": "test (24.x)"}]]) == set()
    # A python × state-manager config matrix is not a numeric shard (non-numeric axis).
    assert _numeric_shard_matrix_legs(
        [[{"name": "test (3.12, redis)"}, {"name": "test (3.12, memory)"}]]) == set()
    # A second number that is NOT a shard index in [1, N] (an OS/node matrix that happens
    # to carry a constant 2) must not be mistaken for a shard.
    assert _numeric_shard_matrix_legs(
        [[{"name": "build (20, 2)"}, {"name": "build (22, 2)"}]]) == set()


def test_opt24_skips_numeric_shard_matrix_not_contradicting_opt25():
    # triggerdotdev/trigger.dev regression: `Unit Tests: Internal (2, 8)` … `(8, 8)` is an
    # 8-way NUMERIC shard matrix (constant shard-count 8, varying index i ∈ [1, 8]). OPT24's
    # name-only heuristic saw no literal `shard`/`partition` token and collapsed the legs to
    # base `Unit Tests: Internal`, falsely reporting "no shard axis observed" — directly
    # contradicting OPT25, which groups the same legs and reports the imbalance on the SAME
    # base + runs. OPT24 must now defer the numeric-shard base to OPT25 (no OPT24 finding),
    # while a genuinely unsharded sibling test job in the same workflow is still flagged.
    from collect_runs import _detect_opt24_long_test_no_sharding
    legs = [("Unit Tests: Internal (2, 8)", 800), ("Unit Tests: Internal (8, 8)", 200),
            ("Unit Tests: Internal (4, 8)", 400), ("Unit Tests: Internal (6, 8)", 350)]
    runs = []
    for _ in range(8):
        run = [_opt24_job(n, s) for n, s in legs]
        run.append(_opt24_job("Unit Tests: Webapp", 354))  # unsharded sibling, no parens
        runs.append(run)
    out = _detect_opt24_long_test_no_sharding("release.yml", runs, 0, monthly_volume=300)
    flagged = [j for f in out for j in f["affected_jobs"]]
    assert "Unit Tests: Internal" not in flagged   # numeric shard axis → deferred to OPT25
    assert "Unit Tests: Webapp" in flagged          # genuinely unsharded → still flagged


def test_segment_pr_populations_splits_on_bimodal_gate():
    # M2: a bimodal gate (changed-tests ~750s on code PRs, self-skips/absent on
    # docs PRs) yields one population per sampled PR, so the bound can credit the
    # expected wall-clock over the real PR distribution.
    from collect_runs import _segment_pr_populations
    code = [{"changed-tests": 750.0, "Lint": 260.0} for _ in range(8)]
    docs = [{"Docs E2E tests": 344.0, "Lint": 260.0} for _ in range(8)]  # gate absent
    per_sha = code + docs
    p50 = {"changed-tests": 750.0, "Docs E2E tests": 344.0, "Lint": 260.0}
    pops = _segment_pr_populations(per_sha, p50, job_p50_all={})
    assert len(pops) == 16  # one per sampled PR
    assert all(abs(share - 1 / 16) < 1e-6 for share, _ in pops)
    # The docs-PR populations have Docs E2E as the pole (gate absent there).
    docs_pole = [checks[0][0] for _share, checks in pops if checks and checks[0][0] == "Docs E2E tests"]
    assert docs_pole  # at least the docs PRs surface Docs E2E as their pole


def test_segment_pr_populations_empty_when_no_bimodal_check():
    # All checks steady across PRs → no segmentation (aggregate path suffices).
    from collect_runs import _segment_pr_populations
    per_sha = [{"a": 300.0, "b": 200.0} for _ in range(8)]
    p50 = {"a": 300.0, "b": 200.0}
    assert _segment_pr_populations(per_sha, p50, job_p50_all={}) == []


def test_segment_pr_populations_caps_inflated_span_at_job_p50():
    # When a REAL bimodal gate triggers segmentation, each population's check
    # magnitude is still capped at the reliable job p50: a queue/re-run-inflated
    # span can't fabricate a pole WITHIN a population. The gate (700s on code PRs,
    # absent on docs PRs) is the genuine bimodal signal; on the docs PRs `build`
    # carries an inflated 1800s span but a 90s job p50.
    from collect_runs import _segment_pr_populations
    code = [{"gate": 700.0, "Lint": 260.0} for _ in range(8)]
    docs = [{"build": 1800.0, "Lint": 260.0} for _ in range(8)]  # inflated span
    per_sha = code + docs
    p50 = {"gate": 700.0, "build": 1800.0, "Lint": 260.0}
    pops = _segment_pr_populations(per_sha, p50, job_p50_all={"build": 90.0})
    assert len(pops) == 16
    # On the docs populations, build is capped 1800→90, so Lint (260) is the pole,
    # never the inflated build span.
    docs_pops = [checks for _s, checks in pops if any(n == "build" for n, _ in checks)]
    assert docs_pops and all(checks[0][0] == "Lint" for checks in docs_pops)


def test_segment_pr_populations_inflated_span_does_not_fabricate_bimodal():
    # Regression: bimodality DETECTION caps spans at the job p50 first, so a
    # single queue-inflated check-run span on an otherwise-steady check can't
    # masquerade as a population split. `build` is steady (~90s job p50) with one
    # 1800s outlier → not bimodal → [] (use the aggregate path).
    from collect_runs import _segment_pr_populations
    per_sha = [{"build": 90.0, "Lint": 260.0} for _ in range(15)]
    per_sha.append({"build": 1800.0, "Lint": 260.0})  # one inflated span
    p50 = {"build": 90.0, "Lint": 260.0}
    assert _segment_pr_populations(per_sha, p50, job_p50_all={"build": 90.0}) == []


def test_segment_pr_populations_shares_sum_to_one_despite_empty_pr():
    # The share denominator is the number of EMITTED populations, not the raw
    # sampled-PR count: a sampled PR that fetched no tracked check (empty map)
    # must not shrink each share below 1/m and understate every saving. Here 2 of
    # 14 sampled PRs are empty, so 12 populations each carry share 1/12 and the
    # shares sum to ~1.0 (NOT 12/14).
    from collect_runs import _segment_pr_populations
    code = [{"changed-tests": 750.0, "Lint": 260.0} for _ in range(6)]
    docs = [{"Docs E2E tests": 344.0, "Lint": 260.0} for _ in range(6)]
    empty = [{}, {}]  # no check-run data fetched for these PRs
    per_sha = code + docs + empty
    p50 = {"changed-tests": 750.0, "Docs E2E tests": 344.0, "Lint": 260.0}
    pops = _segment_pr_populations(per_sha, p50, job_p50_all={})
    assert len(pops) == 12  # the 2 empty PRs contribute no population
    assert all(share == round(1 / 12, 4) for share, _ in pops)  # 1/m, not 1/14
    assert abs(sum(share for share, _ in pops) - 1.0) < 0.01  # ~1.0, not 12/14


def test_segment_pr_populations_high_floor_skips_short_checks():
    # A check whose peak magnitude is below the 120s floor is never a bimodal
    # signal, even when it's active on some PRs and absent on others.
    from collect_runs import _segment_pr_populations
    per_sha = ([{"quick": 119.0, "Lint": 260.0} for _ in range(8)]
               + [{"Lint": 260.0} for _ in range(8)])  # quick absent, but < 120s
    p50 = {"quick": 119.0, "Lint": 260.0}
    assert _segment_pr_populations(per_sha, p50, job_p50_all={}) == []


def test_segment_pr_populations_active_threshold_boundary():
    # The active/inactive cutoff is 0.4*high. A gate self-skipping to exactly
    # 0.4*high counts as ACTIVE everywhere → not bimodal; one second under the
    # cutoff splits the population.
    from collect_runs import _segment_pr_populations
    p50 = {"gate": 300.0, "Lint": 260.0}
    at = ([{"gate": 300.0, "Lint": 260.0} for _ in range(8)]
          + [{"gate": 120.0, "Lint": 260.0} for _ in range(8)])  # 120 == 0.4*300
    assert _segment_pr_populations(at, p50, job_p50_all={}) == []
    below = ([{"gate": 300.0, "Lint": 260.0} for _ in range(8)]
             + [{"gate": 119.0, "Lint": 260.0} for _ in range(8)])  # just under
    assert len(_segment_pr_populations(below, p50, job_p50_all={})) == 16


def test_segment_pr_populations_bimodal_job_slow_mode_can_win_population():
    # Regression (embrace-android-sdk): a BIMODAL job (gradle (test): ~1119s =
    # 18m39s on ~38% of runs, ~227s on the rest) must be allowed to win the
    # populations on its slow-mode PRs. The per-PR span cap was `min(raw, job_p50)`,
    # but a bimodal job's overall p50 sits on the FAST mode (227s), so every genuine
    # slow-mode span (1119s) was clamped to 227s and could never be the pole — making
    # the gate-frequency (_gate_counts) undercount the very slow mode the same code
    # path detects and warns about in the bimodal banner. The reliable upper bound for
    # a bimodal job is its SLOW-mode median (high_p50_s), so cap there, not at p50.
    from collect_runs import _segment_pr_populations
    slow = [{"gradle (test)": 1119.0, "emulator": 600.0} for _ in range(8)]
    fast = [{"gradle (test)": 227.0, "emulator": 600.0} for _ in range(12)]
    # A docs workflow present on a few PRs and absent on the rest gives the
    # segmentation its present/absent trigger INDEPENDENT of the clamped gradle job
    # (it never wins — 500 < emulator's 600), exactly as in the real run.
    for d in fast[:6]:
        d["docs"] = 500.0
    per_sha = slow + fast
    p50 = {"gradle (test)": 227.0, "emulator": 600.0, "docs": 500.0}
    job_p50_all = dict(p50)
    job_bimodal_all = {"gradle (test)": {"slow_frac": 0.38, "low_p50_s": 227.0,
                                         "high_p50_s": 1119.0}}

    def _winners(pops):
        return [checks[0][0] for _s, checks in pops if checks]

    # OLD behavior (cap at job p50 only): gradle clamped 1119->227 < emulator 600 on
    # every PR, so gradle never wins -> gate-frequency undercounts it to zero.
    old = _segment_pr_populations(per_sha, p50, job_p50_all=job_p50_all)
    assert len(old) == 20
    assert _winners(old).count("gradle (test)") == 0  # the undercount bug

    # FIXED: cap gradle's slow-mode span at its slow-mode median (1119s), so on the 8
    # slow PRs gradle (1119) beats emulator (600) and is correctly the pole there.
    fixed = _segment_pr_populations(per_sha, p50, job_p50_all=job_p50_all,
                                    job_bimodal_all=job_bimodal_all)
    assert len(fixed) == 20
    assert _winners(fixed).count("gradle (test)") == 8


def test_segment_pr_populations_bimodal_cap_guards_are_no_ops():
    # The cap-raise guards: a bimodal entry with a missing/zero `high_p50_s` must be a
    # no-op (no crash, no zero-cap), and a `high_p50_s` BELOW the job p50 must never
    # LOWER the cap (the `max(...)` guard). In both cases gradle stays clamped to its
    # 227s job p50 and never beats emulator (600), exactly as the unfixed path.
    from collect_runs import _segment_pr_populations
    slow = [{"gradle (test)": 1119.0, "emulator": 600.0} for _ in range(8)]
    fast = [{"gradle (test)": 227.0, "emulator": 600.0} for _ in range(12)]
    for d in fast[:6]:
        d["docs"] = 500.0
    per_sha = slow + fast
    p50 = {"gradle (test)": 227.0, "emulator": 600.0, "docs": 500.0}
    job_p50_all = dict(p50)

    def _winners(pops):
        return [checks[0][0] for _s, checks in pops if checks]

    for bad_hi in (None, 0.0, 100.0):  # missing, zero, and below the 227s job p50
        bi = {"gradle (test)": {"slow_frac": 0.38, "low_p50_s": 227.0,
                                "high_p50_s": bad_hi}}
        pops = _segment_pr_populations(per_sha, p50, job_p50_all=job_p50_all,
                                       job_bimodal_all=bi)
        assert len(pops) == 20, bad_hi
        # cap never rose above the job p50 → gradle clamped → never wins.
        assert _winners(pops).count("gradle (test)") == 0, bad_hi


def test_select_repr_shas_fetches_concurrently():
    # The check-run fetches run concurrently — N latency-bound fetches take roughly
    # ceil(N / _FETCH_CONCURRENCY) batch-latencies, not N sequential ones. Prove it
    # with a fetch that sleeps: 16 fetches at 50ms would be 0.8s sequential but ~2
    # batches (~0.1s) concurrent. Assert comfortably under half the sequential time
    # (robust to CI scheduling jitter).
    import time
    from collect_runs import _select_repr_shas, _FETCH_CONCURRENCY
    assert _FETCH_CONCURRENCY >= 8
    sha_ts = {f"{i:03d}": f"{1000 - i:04d}" for i in range(16)}
    _SLEEP = 0.05

    def fetch(sha):
        time.sleep(_SLEEP)
        return {"build": 100.0}

    t0 = time.monotonic()
    repr_shas, _per, _durs, _diag = _select_repr_shas(
        sha_ts, fetch, req_names=frozenset(), target=16)
    elapsed = time.monotonic() - t0
    assert len(repr_shas) == 16
    assert elapsed < 16 * _SLEEP / 2, f"fetches did not run concurrently: {elapsed:.2f}s"


def test_select_repr_shas_walks_newest_first_and_caps():
    # Candidates are considered newest-first by their most-recent run timestamp, and
    # SELECTION stops at `target`. The batch is capped to the number still needed, so
    # once the newest two qualify the older candidate is NEVER fetched — frugal, even
    # though fetches run concurrently.
    import threading
    from collect_runs import _select_repr_shas
    sha_ts = {"old": "2026-06-01T00:00:00Z",
              "new": "2026-06-03T00:00:00Z",
              "mid": "2026-06-02T00:00:00Z"}
    fetched: list[str] = []
    _flock = threading.Lock()

    def fetch(sha):
        with _flock:                        # concurrent callers; the list is shared
            fetched.append(sha)
        return {"build": 100.0, "lint": 50.0}

    repr_shas, per_sha, durs, diag = _select_repr_shas(
        sha_ts, fetch, req_names=frozenset(), target=2)
    assert repr_shas == ["new", "mid"]      # newest two, in recency order
    assert len(per_sha) == 2
    assert durs["build"] == [100.0, 100.0]  # each kept PR's durations accumulate
    assert set(fetched) == {"new", "mid"}   # "old" never fetched (batch capped to need)
    assert diag["complete"] is True and diag["kept"] == 2 and diag["fetch_failures"] == 0


def test_select_repr_shas_post_target_failure_is_not_a_coverage_gap():
    # Regression: the concurrent batch is capped to the number STILL NEEDED, so a
    # candidate PAST the target is never fetched — a (hypothetical) failure on it can't
    # be mis-counted as a coverage gap / partial data on an already-complete sample.
    # (Without the cap, the target-landing batch would over-fetch the failing 3rd PR
    # and bump fetch_failures even though 2 valid PRs were selected.)
    from collect_runs import _select_repr_shas
    sha_ts = {f"{i:02d}": f"{99 - i:02d}" for i in range(3)}  # newest-first == index order
    fetched: list[str] = []

    def fetch(sha):
        fetched.append(sha)
        return None if int(sha) >= 2 else {"build": 100.0}  # the 3rd would FAIL

    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, fetch, req_names=frozenset(), target=2)
    assert repr_shas == ["00", "01"]
    assert diag["kept"] == 2 and diag["complete"] is True
    assert diag["fetch_failures"] == 0   # the post-target failing fetch was never issued
    assert diag["fetched"] == 2          # only the two needed PRs fetched
    assert "02" not in fetched


def test_select_repr_shas_keeps_only_full_required_suite():
    # A PR is kept only if its check-runs include EVERY required status check —
    # a docs PR that self-skips the gate is walked past, never sampled.
    from collect_runs import _select_repr_shas
    sha_ts = {"docs": "2026-06-03T00:00:00Z",   # newest, but skips the gate
              "code": "2026-06-02T00:00:00Z"}    # ran the full suite
    checks = {"docs": {"lint": 40.0},
              "code": {"lint": 40.0, "changed-tests": 700.0}}
    repr_shas, per_sha, _durs, diag = _select_repr_shas(
        sha_ts, lambda s: checks[s], req_names=frozenset({"changed-tests"}), target=20)
    assert repr_shas == ["code"]            # docs PR excluded despite being newer
    assert per_sha == [checks["code"]]
    assert diag["required_suite_scoped"] is True


def test_select_repr_shas_skips_empty_and_falls_back_to_recency():
    # An empty check-run map (no durations) is skipped; with no readable required
    # set, any PR that ran >=1 tracked check is kept (recency-only fallback).
    from collect_runs import _select_repr_shas
    sha_ts = {"a": "2026-06-03T00:00:00Z",
              "b": "2026-06-02T00:00:00Z",
              "c": "2026-06-01T00:00:00Z"}
    checks = {"a": {}, "b": {"lint": 30.0}, "c": {"build": 90.0}}
    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, lambda s: checks[s], req_names=frozenset(), target=20)
    assert repr_shas == ["b", "c"]          # "a" (empty) skipped, no required gate
    assert diag["required_suite_scoped"] is False  # recency-only fallback
    assert diag["fetch_failures"] == 0      # an empty PR is NOT a fetch failure


def test_select_repr_shas_failed_fetch_is_not_an_empty_pr():
    # A fetch that FAILED (returns None) must be counted as a coverage gap, not
    # laundered into "this PR ran nothing" — otherwise a gh error silently drifts
    # the sample to older commits with no signal.
    from collect_runs import _select_repr_shas
    sha_ts = {"a": "2026-06-03T00:00:00Z",   # newest — fetch fails
              "b": "2026-06-02T00:00:00Z",   # ran the suite
              "c": "2026-06-01T00:00:00Z"}   # ran the suite
    checks = {"a": None, "b": {"lint": 30.0}, "c": {"build": 90.0}}
    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, lambda s: checks[s], req_names=frozenset(), target=2)
    assert repr_shas == ["b", "c"]          # "a" (failed) skipped, not sampled
    assert diag["fetch_failures"] == 1      # the failure is recorded, not hidden
    assert diag["complete"] is True


def test_select_repr_shas_caps_fetches_when_candidates_rejected():
    # The walk pursues the full target (reach 20), but a pathologically large
    # window of all-rejected candidates is still bounded by the runaway backstop —
    # it must not fetch check-runs for every one of an unbounded number of PRs.
    from collect_runs import _select_repr_shas
    sha_ts = {f"{i:03d}": f"2026-06-{(i % 28) + 1:02d}T00:00:00Z" for i in range(200)}
    fetched: list[str] = []

    def fetch(sha):
        fetched.append(sha)
        return {}                            # nobody qualifies

    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, fetch, req_names=frozenset(), target=20)
    assert repr_shas == []                   # none qualified
    assert diag["complete"] is False         # shortfall surfaced
    assert len(fetched) == 160               # max(20*8, 120) backstop, not all 200
    assert diag["fetched"] == len(fetched)


def test_select_repr_shas_backstop_stops_partial_sample_and_marks_incomplete():
    # The boundary the larger backstop introduces: qualifiers EXIST but are sparse
    # enough that the 160-fetch ceiling halts the walk before reaching the target.
    # The partial sample must be surfaced honestly — kept < target, complete False,
    # fetched pinned at the backstop — never passed off as a full sample.
    from collect_runs import _select_repr_shas
    # Newest-first == index order; 1 in 10 qualifies, so the 20th qualifier would
    # sit at index 190 — past the 160 backstop, which stops the walk at 16 kept.
    sha_ts = {f"{i:03d}": f"{1000 - i:04d}" for i in range(200)}
    fetched: list[str] = []

    def fetch(sha):
        fetched.append(sha)
        return {"lint": 30.0} if int(sha) % 10 == 0 else {}   # 1 in 10 qualifies

    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, fetch, req_names=frozenset(), target=20)
    assert len(repr_shas) == 16              # backstop bit before the target
    assert len(fetched) == 160               # pinned at the runaway backstop
    assert diag["complete"] is False         # partial sample surfaced, not laundered
    assert diag["kept"] == 16 and diag["fetched"] == 160
    assert diag["fetch_failures"] == 0       # empty maps are skips, not failures


def test_select_repr_shas_walks_past_old_cap_to_reach_target():
    # Reaching the 20-PR floor is worth extra gh calls: when only 1 in 5 recent PRs
    # ran the full suite, the 20th qualifying PR sits at fetch 96 — past the old
    # `target*3`=60 cap (which would have stopped at 12/20). The walk now continues
    # and reaches the full target of 20.
    from collect_runs import _select_repr_shas
    # Encode timestamps so newest-first == index order (000 is newest).
    sha_ts = {f"{i:03d}": f"{1000 - i:04d}" for i in range(200)}
    fetched: list[str] = []

    def fetch(sha):
        fetched.append(sha)
        return {"lint": 30.0} if int(sha) % 5 == 0 else {}   # 1 in 5 qualifies

    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, fetch, req_names=frozenset(), target=20)
    assert len(repr_shas) == 20               # full target reached
    assert diag["complete"] is True
    assert len(fetched) == 96                 # walked past the old 60-fetch cap
    assert len(fetched) > max(20 * 3, 20 + 8)  # the old cap would have stopped short


def test_select_repr_shas_reports_shortfall():
    # Fewer qualifying PRs than the target → the sample is marked incomplete so
    # the report can caveat it instead of presenting it as a full sample.
    from collect_runs import _select_repr_shas
    sha_ts = {"a": "2026-06-03T00:00:00Z", "b": "2026-06-02T00:00:00Z"}
    checks = {"a": {"lint": 30.0}, "b": {"lint": 40.0}}
    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, lambda s: checks[s], req_names=frozenset(), target=20)
    assert repr_shas == ["a", "b"]
    assert diag["complete"] is False and diag["kept"] == 2 and diag["target"] == 20


def test_sampling_provenance_fields_locks_diag_to_doc_key_mapping():
    # Seam guard: collect() copies _select_repr_shas's sample_diag onto the
    # `sample_*` doc keys that _provenance_block reads. A typo there (wiring
    # sample_fetched to the wrong diag key, say) would silently zero a coverage
    # caveat — and nothing else fails, since collect() makes live gh calls and is
    # never exercised in the suite. Lock the mapping on the pure helper.
    from collect_runs import _sampling_provenance_fields, _select_repr_shas
    diag = {"target": 20, "kept": 16, "fetched": 160, "fetch_failures": 3,
            "complete": False, "required_suite_scoped": True,
            "required_suite_unsatisfiable": False,
            "required_checks_unobservable": ["Devin Review"]}
    assert _sampling_provenance_fields(diag) == {
        "sample_target": 20,
        "sample_complete": False,
        "sample_fetch_failures": 3,
        "sample_fetched": 160,
        "required_suite_scoped": True,
        "required_suite_unsatisfiable": False,
        "required_checks_unobservable": ["Devin Review"],
    }
    # Drift guard: a REAL diag must feed the helper without KeyError and yield
    # exactly these doc keys — so renaming/dropping a diag key breaks loudly here
    # rather than silently in production.
    sha_ts = {f"{i:03d}": f"{1000 - i:04d}" for i in range(5)}
    _shas, _per, _durs, real_diag = _select_repr_shas(
        sha_ts, lambda s: {"lint": 1.0}, req_names=frozenset(), target=3)
    mapped = _sampling_provenance_fields(real_diag)
    assert set(mapped) == {"sample_target", "sample_complete",
                           "sample_fetch_failures", "sample_fetched",
                           "required_suite_scoped", "required_suite_unsatisfiable",
                           "required_checks_unobservable"}
    assert mapped["sample_fetched"] == real_diag["fetched"]
    assert mapped["sample_complete"] == real_diag["complete"]


def test_select_repr_shas_promotes_recency_when_required_suite_unsatisfiable():
    # External-gate case: a readable required suite that NO sampled PR carries (every
    # required check is external/managed). The required filter keeps zero, so the walk
    # promotes the recency-only pool instead of returning an empty sample — the basis
    # for the demoted PR-floor critical path downstream.
    from collect_runs import _select_repr_shas
    sha_ts = {"a": "2026-06-03T00:00:00Z",
              "b": "2026-06-02T00:00:00Z"}
    # Neither PR carries the external required check `enterprise/ci`; both ran real work.
    checks = {"a": {"server-ci": 600.0}, "b": {"webapp-ci": 400.0}}
    repr_shas, per_sha, durs, diag = _select_repr_shas(
        sha_ts, lambda s: checks[s], req_names=frozenset({"enterprise/ci"}), target=20)
    assert repr_shas == ["a", "b"]                       # recency pool promoted, not empty
    assert per_sha == [checks["a"], checks["b"]]
    assert "server-ci" in durs and "webapp-ci" in durs   # durations carried through
    assert diag["required_suite_unsatisfiable"] is True
    assert diag["required_suite_scoped"] is False         # the final sample is recency-scoped
    assert diag["required_checks_unobservable"] == []     # case d: all-external ⇒ subset re-select never fires
    assert diag["kept"] == 2


def test_select_repr_shas_excludes_status_only_required_check_from_suite_test():
    # trigger.dev regression: the required set mixes in-repo gates with a STATUS-ONLY
    # external check (`Devin Review` — a GitHub-App commit status, never a check-run). The
    # strict subset test would reject every PR (Devin Review is never present), falsely
    # emptying the required-suite sample and tipping the repo into the external-gate /
    # PR-floor fallback even though `All PR Checks` gates every PR. The unobservable
    # required check must be excluded from the per-PR suite test and disclosed.
    from collect_runs import _select_repr_shas
    sha_ts = {f"{i:02d}": f"{99 - i:02d}" for i in range(8)}   # newest-first == index order
    # Every PR runs the two OBSERVABLE required checks + real work; NONE runs Devin Review.
    checks = {s: {"All PR Checks": 3.0, "CodeQL": 2.0, "webapp": 200.0} for s in sha_ts}
    req = frozenset({"All PR Checks", "CodeQL", "Devin Review"})
    repr_shas, per_sha, _durs, diag = _select_repr_shas(
        sha_ts, lambda s: checks[s], req_names=req, target=6)
    assert len(repr_shas) == 6                                  # full required-suite sample
    assert diag["required_suite_scoped"] is True               # scoped, NOT external fallback
    assert diag["required_suite_unsatisfiable"] is False
    assert diag["required_checks_unobservable"] == ["Devin Review"]   # disclosed, not silent
    assert all("webapp" in m for m in per_sha)                 # real gating work is in the sample


def test_select_repr_shas_observable_subset_inert_when_all_required_observed():
    # No-regression guard: when EVERY required check is observable, the subset re-selection
    # is inert (req_unobservable empty) — a genuinely partial-suite repo that's merely short
    # is left exactly as before, never silently widened by dropping a present required check.
    from collect_runs import _select_repr_shas
    sha_ts = {"docs": "03", "code": "02"}
    checks = {"docs": {"lint": 40.0}, "code": {"lint": 40.0, "changed-tests": 700.0}}
    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, lambda s: checks[s], req_names=frozenset({"changed-tests"}), target=20)
    assert repr_shas == ["code"]                                # docs still excluded
    assert diag["required_checks_unobservable"] == []           # nothing dropped
    assert diag["required_suite_scoped"] is True


def test_select_repr_shas_observable_subset_split_across_prs_falls_back_to_pr_floor():
    # Edge of the observable-subset re-selection: the OBSERVABLE required checks are spread
    # across different PRs so no single PR carries the full observable subset. Re-selection
    # keeps zero, control falls to the unsatisfiable promotion (PR-floor fallback). The
    # contradictory state must NOT leak: when the suite collapses to the PR-floor,
    # `required_checks_unobservable` is cleared (the external-gate disclosure owns the
    # messaging), so we never report a populated unobservable list alongside scoped=False.
    from collect_runs import _select_repr_shas
    sha_ts = {f"{i:02d}": f"{99 - i:02d}" for i in range(4)}
    # `A` and `B` are both observable required checks, but no PR runs BOTH; `Devin Review`
    # (status-only) is never present.
    checks = {"00": {"A": 5.0, "work": 300.0}, "01": {"B": 5.0, "work": 280.0},
              "02": {"A": 5.0, "work": 290.0}, "03": {"B": 5.0, "work": 270.0}}
    req = frozenset({"A", "B", "Devin Review"})
    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, lambda s: checks[s], req_names=req, target=20)
    assert repr_shas == ["00", "01", "02", "03"]                # PR-floor: full recency pool
    assert diag["required_suite_unsatisfiable"] is True
    assert diag["required_suite_scoped"] is False
    assert diag["required_checks_unobservable"] == []           # cleared — not double-disclosed


def test_select_repr_shas_observable_subset_reselection_can_be_short():
    # The re-selection draws only from the capped recency pool, so it can yield FEWER than
    # target PRs — an honest short sample (complete=False), still required-scoped and with the
    # excluded status-only check disclosed.
    from collect_runs import _select_repr_shas
    sha_ts = {f"{i:02d}": f"{99 - i:02d}" for i in range(3)}
    checks = {s: {"All PR Checks": 3.0, "work": 200.0} for s in sha_ts}   # no Devin Review
    req = frozenset({"All PR Checks", "Devin Review"})
    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, lambda s: checks[s], req_names=req, target=20)
    assert len(repr_shas) == 3                                  # short — only 3 PRs available
    assert diag["complete"] is False                           # honestly flagged short
    assert diag["required_suite_scoped"] is True
    assert diag["required_checks_unobservable"] == ["Devin Review"]


def test_select_pr_floor_workflows_ranks_pr_volume_clean_workflows_only():
    # The PR-floor synthesis: only PR-volume, CI-clean workflows with a timed long pole
    # qualify, ranked slowest-first. A push-only/release workflow (no PR-volume event),
    # a comment-contaminated workflow, and an un-timed workflow are all excluded — they
    # are not part of the developer's merge wait.
    from collect_runs import _select_pr_floor_workflows
    crit_by_wf = {
        "ci.yml": {"long_pole_job": "build", "long_pole_p50": 300.0},
        "test.yml": {"long_pole_job": "e2e", "long_pole_p50": 500.0},
        "release.yml": {"long_pole_job": "publish", "long_pole_p50": 900.0},
        "triage.yml": {"long_pole_job": "label", "long_pole_p50": 120.0},
        "notime.yml": {"long_pole_job": "lint", "long_pole_p50": 0.0},
    }
    events_by_wf = {
        "ci.yml": {"pull_request"},
        "test.yml": {"merge_group"},
        "release.yml": {"push"},                       # no PR-volume event -> excluded
        "triage.yml": {"pull_request", "issue_comment"},  # contaminated -> excluded
        "notime.yml": {"pull_request"},                # long_pole_p50 == 0 -> excluded
    }
    floor = _select_pr_floor_workflows(crit_by_wf, events_by_wf)
    assert floor == [(500.0, "test.yml", "e2e"), (300.0, "ci.yml", "build")]


def test_select_pr_floor_workflows_falls_back_to_push_on_push_only_repo():
    # Regression (webflow/js-webflow-api): a PUSH-ONLY repo (no pull_request flow — the
    # team merges straight to the default branch, PR sample 0/20) had a clear measured
    # long pole (`test` p50 51.5s) but synthesized NO PR-floor, so the renderer dead-ended
    # to static-only "no run history" and the slowest measured job vanished. With no
    # PR-volume workflow to anchor the floor, the push CI IS the developer's merge wait, so
    # it must anchor a (clearly-demoted) PR-floor spine. A still-present comment-contaminated
    # push workflow stays excluded (its 30d volume is not a CI proxy).
    from collect_runs import _select_pr_floor_workflows
    crit_by_wf = {
        "ci.yml": {"long_pole_job": "test", "long_pole_p50": 51.5},
        "build.yml": {"long_pole_job": "compile", "long_pole_p50": 25.5},
        "noisy.yml": {"long_pole_job": "label", "long_pole_p50": 90.0},
        "notime.yml": {"long_pole_job": "lint", "long_pole_p50": 0.0},
    }
    events_by_wf = {
        "ci.yml": {"push"},
        "build.yml": {"push"},
        "noisy.yml": {"push", "issue_comment"},   # contaminated volume -> excluded
        "notime.yml": {"push"},                    # long_pole_p50 == 0 -> excluded
    }
    floor = _select_pr_floor_workflows(crit_by_wf, events_by_wf)
    assert floor == [(51.5, "ci.yml", "test"), (25.5, "build.yml", "compile")]


def test_select_pr_floor_push_fallback_does_not_displace_a_pr_spine():
    # Guard: the push fallback fires ONLY when NO PR-volume workflow ran. A normal PR
    # repo with a post-merge `push` deploy workflow keeps its PR spine — the deploy
    # workflow (push-only) must NOT be synthesized as the floor.
    from collect_runs import _select_pr_floor_workflows
    crit_by_wf = {
        "pr.yml": {"long_pole_job": "test", "long_pole_p50": 200.0},
        "deploy.yml": {"long_pole_job": "publish", "long_pole_p50": 900.0},
    }
    events_by_wf = {"pr.yml": {"pull_request"}, "deploy.yml": {"push"}}
    floor = _select_pr_floor_workflows(crit_by_wf, events_by_wf)
    assert floor == [(200.0, "pr.yml", "test")]   # deploy.yml (push-only) excluded


def test_collapse_pr_floor_siblings_drops_within_workflow_concurrency_sibling():
    # Regression (mattermost/mattermost): the PR-floor fallback case (1) flagged
    # EVERY top-p50 file pole in place, so two jobs from ONE workflow
    # (server-ci.yml: `Postgres (shard 1)` @636s and `Run mmctl tests / mmctl`
    # @374s) were BOTH drilled as co-equal long poles — even though they run
    # concurrently and the 374s mmctl job finishes behind several longer
    # concurrent server-ci jobs, so it never independently gates the merge. That
    # directly contradicted the run's OWN OPT24 finding on mmctl ("a longer
    # concurrent job gates the run, so sharding this one saves ~0 wall-clock").
    # The collapse keeps only the slowest pole per workflow_file, mirroring case
    # (2)'s one-pole-per-workflow floor.
    from collect_runs import _collapse_pr_floor_siblings
    postgres = {"check": "Postgres (shard 1)", "p50_s": 636.0,
                "workflow_file": "server-ci.yml", "job": "test-postgres-normal"}
    mmctl = {"check": "Run mmctl tests / mmctl", "p50_s": 374.0,
             "workflow_file": "server-ci.yml", "job": "test-mmctl"}
    other_wf = {"check": "build", "p50_s": 500.0,
                "workflow_file": "webapp-ci.yml", "job": "build"}
    fileless = {"check": "CLA bot", "p50_s": 800.0}  # no workflow_file/job
    poles = [postgres, mmctl, other_wf, fileless]

    kept = _collapse_pr_floor_siblings(poles)

    # The concurrency-dominated server-ci sibling is gone; its longer sibling stays.
    assert mmctl not in kept
    assert postgres in kept
    # Exactly one pole survives per workflow_file.
    server_ci = [p for p in kept if p.get("workflow_file") == "server-ci.yml"]
    assert server_ci == [postgres]
    # A pole from a DIFFERENT workflow is untouched, and fileless poles always stay.
    assert other_wf in kept
    assert fileless in kept
    # Input order of the kept poles is preserved.
    assert kept == [postgres, other_wf, fileless]


def test_select_repr_shas_empty_window_is_not_an_external_gate():
    # Boundary: a required suite is in force but NO PR carried any timed check (every
    # fetch returns {}). The recency pool stays empty too, so the result is a genuinely
    # empty sample — NOT an external-gate fallback. `required_suite_unsatisfiable` must be
    # False, so a no-data window isn't mislabeled as a confirmed external/managed gate.
    from collect_runs import _select_repr_shas
    sha_ts = {"a": "2026-06-03T00:00:00Z", "b": "2026-06-02T00:00:00Z"}
    repr_shas, _per, _durs, diag = _select_repr_shas(
        sha_ts, lambda s: {}, req_names=frozenset({"enterprise/ci"}), target=20)
    assert repr_shas == []
    assert diag["required_suite_unsatisfiable"] is False
    assert diag["kept"] == 0


def test_pole_mapping_pin_wins_over_name_resolution():
    # The bug the `mapping` pin exists to prevent: a PR-floor pole's "check" IS a job
    # name that can collide across workflows. A caller-supplied pin must be used VERBATIM,
    # never re-resolved by name (which could bind it to a different workflow's same-named
    # job). With no pin, it delegates to _map_check_to_job.
    from collect_runs import _pole_mapping
    crit_by_wf = {
        "a.yml": {"job_p50": {"build": 100.0}},
        "b.yml": {"job_p50": {"build": 900.0}},   # a same-named job in a second workflow
    }
    # Pin to the SLOWER-losing workflow; the pin must win regardless.
    assert _pole_mapping("build", crit_by_wf, ("a.yml", "build")) == ("a.yml", "build")
    # No pin -> cross-workflow same-name ambiguity: `_map_check_to_job` now REFUSES to
    # guess the slowest (issue #59), so name resolution honestly resolves to None rather
    # than mis-binding `build` to whichever workflow happened to be slower. The pin above
    # is exactly how a known collision is bound correctly.
    assert _pole_mapping("build", crit_by_wf, None) is None


def test_bimodal_split_flags_fast_slow_clusters_only():
    # A genuine fast/slow split (e.g. turbo cache hit vs miss) -> reported.
    test22 = [330, 132, 372, 145, 383, 351, 335, 308, 128, 355, 145, 123, 134,
              118, 136, 137, 133, 104, 94, 351, 339, 375, 100, 395, 357]
    bi = _bimodal_split(test22)
    assert bi is not None
    assert bi["slow_n"] == 12 and bi["slow_frac"] == 0.48
    assert bi["low_p50_s"] < 200 < bi["high_p50_s"]   # two well-separated clusters
    # NOT bimodal: a tight cluster, a smooth spread, a lone outlier, too few samples.
    assert _bimodal_split([530, 540, 545, 549, 549, 549, 549, 549, 549, 549]) is None
    assert _bimodal_split([100, 120, 140, 160, 180, 200, 220, 240, 260, 280]) is None
    assert _bimodal_split([100, 102, 104, 106, 108, 110, 112, 113, 360]) is None  # 1 outlier
    assert _bimodal_split([140, 360]) is None                                     # n < min_n


def test_critical_path_attaches_bimodal_to_a_split_job():
    # changed-tests-style job: half the runs fast, half slow -> job_bimodal carries it.
    runs = [[{"name": "test", "started_at": "2026-01-01T00:00:00Z",
              "completed_at": f"2026-01-01T00:0{d // 60}:{d % 60:02d}Z"}]
            for d in ([140] * 6 + [360] * 6)]
    crit = _critical_path(runs)
    assert "test" in crit["job_bimodal"]
    assert crit["job_bimodal"]["test"]["high_p50_s"] > crit["job_bimodal"]["test"]["low_p50_s"]


# --- concurrent per-run jobs fetch (`_gather_run_jobs`) -------------------------
# The dominant data-pass cost. The contract: fetch CONCURRENTLY but return results
# in INPUT order (so per-event buckets stay deterministic), drop runs that genuinely
# had no jobs (`[]`), and COUNT runs whose fetch FAILED (`None`) as a coverage gap
# rather than laundering them into the clean sample. `fetch` is injectable so the
# contract is pinned without real gh calls.

def test_gather_run_jobs_preserves_input_order_under_out_of_order_finish():
    # Stub fetcher that sleeps LONGER for EARLIER runs, so completion order is the
    # reverse of input order. The result must still be input-ordered (pool.map), or
    # a run's jobs would land in the wrong event bucket downstream.
    import time
    from collect_runs import _gather_run_jobs
    runs = [{"id": i, "event": "pull_request"} for i in range(8)]

    def fetch(_client, _repo, run_id):
        time.sleep((8 - run_id) * 0.005)        # run 0 finishes LAST
        return [{"name": "build", "id": run_id}]

    kept, failures = _gather_run_jobs(None, "o/r", runs, fetch=fetch)
    assert failures == 0
    assert [run["id"] for run, _jobs in kept] == list(range(8))   # INPUT order
    assert [jobs[0]["id"] for _run, jobs in kept] == list(range(8))


def test_gather_run_jobs_counts_failures_and_drops_empties_distinctly():
    # None == fetch FAILED (counted); [] == genuinely no jobs (dropped, NOT counted);
    # a real list is kept. The three must not be conflated — a failed fetch shown as
    # "ran nothing" is the silent coverage drop this distinction exists to prevent.
    from collect_runs import _gather_run_jobs
    runs = [{"id": 0, "event": "pull_request"},   # ok
            {"id": 1, "event": "pull_request"},   # FAILED (None)
            {"id": 2, "event": "push"},           # genuinely empty ([])
            {"id": 3, "event": "pull_request"}]   # ok
    table = {0: [{"name": "a"}], 1: None, 2: [], 3: [{"name": "b"}, {"name": "c"}]}

    kept, failures = _gather_run_jobs(
        None, "o/r", runs, fetch=lambda _c, _r, rid: table[rid])
    assert failures == 1                                  # only the None
    assert [run["id"] for run, _ in kept] == [0, 3]       # empties dropped, order kept
    assert sum(len(jobs) for _, jobs in kept) == 3


# --- adaptive 2-pass run sampling (shallow + deepen) ------------------------------
# (reuses the module's `_job(name, dur_s, labels)` helper above.)

def _aj(name, secs):  # one ubuntu-latest job, for the adaptive tests
    return _job(name, secs, ["ubuntu-latest"])


def test_accumulate_jobs_tags_jobs_with_run_created_at():
    # OPT43 measures queue from the RUN's trigger; _accumulate_jobs carries
    # run.created_at onto each job as `_run_created_at` (the run is otherwise dropped).
    from collect_runs import _accumulate_jobs
    jpr, jbe = [], {}
    run = {"event": "pull_request", "created_at": "2026-01-01T00:00:00Z"}
    jobs = [_aj("a", 60), _aj("b", 30)]
    _accumulate_jobs([(run, jobs)], jpr, jbe)
    assert all(j["_run_created_at"] == "2026-01-01T00:00:00Z" for j in jobs)


def test_accumulate_jobs_folds_and_extends_in_place():
    # The shallow pass folds (run, jobs) into the accumulators; the deepen pass calls
    # it AGAIN on the same lists to extend them — so a workflow's deepened sample is
    # shallow + the refetched rest, not a re-fetch.
    from collect_runs import _accumulate_jobs
    jpr, jbe = [], {}
    nr, nj = _accumulate_jobs(
        [({"event": "pull_request"}, [_aj("a", 60), _aj("b", 30)]),
         ({"event": "push"}, [_aj("a", 60)])], jpr, jbe)
    assert (nr, nj) == (2, 3)
    assert len(jpr) == 2 and len(jbe["pull_request"]) == 1 and len(jbe["push"]) == 1
    nr2, nj2 = _accumulate_jobs(
        [({"event": "pull_request"}, [_aj("a", 60)])], jpr, jbe)
    assert (nr2, nj2) == (1, 1)
    assert len(jpr) == 3 and len(jbe["pull_request"]) == 2   # extended, not replaced


def test_crit_for_scopes_to_the_developer_event():
    # The critical path must measure DEVELOPER (PR) wait: a workflow that runs on both
    # pull_request (build, 300s) and push (a heavier deploy, 900s) must scope to the PR
    # runs — else the push long pole sizes the PR wait against the wrong job.
    from collect_runs import _crit_for
    jbe = {"pull_request": [[_aj("build", 300)] for _ in range(5)],
           "push": [[_aj("deploy", 900)] for _ in range(5)]}
    jpr = [r for rs in jbe.values() for r in rs]
    crit, _ = _crit_for(jpr, jbe)
    assert crit["event_scope"] == "pull_request"
    assert crit["long_pole_job"] == "build"


def test_crit_for_falls_back_to_all_events_when_no_dev_event():
    from collect_runs import _crit_for
    jbe = {"push": [[_aj("deploy", 120)] for _ in range(3)]}
    jpr = [r for rs in jbe.values() for r in rs]
    crit, _ = _crit_for(jpr, jbe)
    assert crit["event_scope"] == "all-events" and crit["long_pole_job"] == "deploy"


def test_crit_for_scopes_to_pull_request_target_not_all_events():
    # The pull_request_target blend bug (roboflow/supervision pr-conflict-labeler): a
    # fork-PR conflict labeler fires ONLY on `push` (re-scan every open PR post-merge)
    # and `pull_request_target: synchronize`. pull_request_target IS a developer-wait
    # event (a PR waits on its checks), so the critical path must scope to its runs —
    # a fast ~4s mode — NOT fall back to all-events and BLEND the heavy push runs (a
    # single 120s retry sleep inside the labeler action, gating ZERO merges) into the
    # PR wait, which manufactured a false "~2m on ~30% of PRs" bimodal gate.
    from collect_runs import _crit_for
    jbe = {"pull_request_target": [[_aj("main", 4)] for _ in range(7)],
           "push": [[_aj("main", 124)] for _ in range(3)]}
    jpr = [r for rs in jbe.values() for r in rs]
    crit, crit_runs = _crit_for(jpr, jbe)
    assert crit["event_scope"] == "pull_request_target"
    assert crit["long_pole_p50"] == 4.0   # the PR-wait mode, NOT the blended push 124s
    assert len(crit_runs) == 7            # only the pull_request_target runs, push excluded


def test_deepen_candidates_use_full_breadth_events_not_shallow_scope():
    # The eligibility bug the adversarial review found: keying off the shallow
    # `event_scope` lets a [push, pull_request] workflow whose recent window is all
    # push read "all-events" and be excluded — then measured against push runs a full
    # pass would PR-scope. Eligibility now keys off FULL-BREADTH observed events.
    from collect_runs import _deepen_candidates
    crit_by_wf = {
        "gate.yml": {"event_scope": "pull_request", "long_pole_p50": 900.0},
        "pushwin.yml": {"event_scope": "all-events", "long_pole_p50": 800.0},  # shallow=push
        "pushonly.yml": {"event_scope": "all-events", "long_pole_p50": 2000.0},
        "empty.yml": {"event_scope": "pull_request", "long_pole_p50": 0.0},
    }
    events_by_wf = {
        "gate.yml": {"pull_request"},
        "pushwin.yml": {"push", "pull_request"},   # full breadth SEES the PR runs
        "pushonly.yml": {"push", "schedule"},      # genuinely never a PR gate
        "empty.yml": {"pull_request"},
    }
    cand = _deepen_candidates(crit_by_wf, events_by_wf)
    assert cand == {"gate.yml", "pushwin.yml"}     # pushwin INCLUDED (full breadth); pushonly + empty out


def test_deepen_check_keys_include_every_job_and_the_bimodal_tail():
    # The deepen loop ranks per-CHECK keys so every check the chart renders is covered:
    # each job's p50 (the chart sorts by p50) PLUS the long-pole p95, so a fast-median
    # / high-tail (bimodal) gate isn't buried below the deepen cut by its median.
    from collect_runs import _deepen_check_keys
    crit = {"job_p50": {"build": 300.0, "lint": 40.0}, "long_pole_p50": 300.0,
            "long_pole_p95": 900.0}   # bimodal tail well above any median
    keys = _deepen_check_keys(crit)
    assert 300.0 in keys and 40.0 in keys      # every job's p50 is a candidate key
    assert max(keys) == 900.0                  # the p95 tail surfaces a bimodal gate


def test_shallow_then_deepen_recovers_the_full_depth_p50():
    # The core invariant the validation proved: a shallow sample ranks the pole, and
    # deepening it (extend + recompute) yields the SAME p50 as one full-depth pass.
    from collect_runs import _accumulate_jobs, _crit_for
    full = [[_aj("build", 300 + (i % 7) * 20)] for i in range(20)]   # 20 PR runs
    shallow_runs = [({"event": "pull_request"}, r) for r in full[:10]]
    rest_runs = [({"event": "pull_request"}, r) for r in full[10:]]
    # adaptive: shallow then deepen the SAME accumulators
    jpr, jbe = [], {}
    _accumulate_jobs(shallow_runs, jpr, jbe)
    shallow_crit, _ = _crit_for(jpr, jbe)
    _accumulate_jobs(rest_runs, jpr, jbe)
    deep_crit, _ = _crit_for(jpr, jbe)
    # one full pass over all 20
    fjpr, fjbe = [], {}
    _accumulate_jobs([({"event": "pull_request"}, r) for r in full], fjpr, fjbe)
    full_crit, _ = _crit_for(fjpr, fjbe)
    assert deep_crit["long_pole_p50"] == full_crit["long_pole_p50"]   # exact recovery
    assert deep_crit["long_pole_job"] == full_crit["long_pole_job"]


# --- #33: OPT45 whole-run-cancel saving derives from the affected jobs' MEASURED
# billable compute (never exceeds it). Producer (sizing → spine re-ground) → the
# `check_saving_within_measured_compute` invariant (verify_report, PR #30). ---

def _load_verify_report_for_bounds():
    """Load THIS skill's verify_report by path under a unique name (ci-secure ships
    one too, so a bare `import verify_report` can bind the wrong module)."""
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parent / "verify_report.py"
    name = "ci_speedup_verify_report_for_sizing_bounds"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Mastodon-shaped OPT45: a build workflow whose cancelled jobs run on only a
# fraction of its runs, so the MEASURED spine compute (occurrence-diluted) is far
# below what long-pole × full-volume — or even affected-p50-sum × full-volume —
# would model. Summed measured billable = 892.8 min/mo (mirrors issue #33).
_OPT45_WF = ".github/workflows/build-image.yml"
_OPT45_JOBS = ["compute-suffix", "build-image", "build-image-streaming"]
_OPT45_BILLABLE = {"compute-suffix": 100.0, "build-image": 350.0,
                   "build-image-streaming": 442.8}   # Σ = 892.8
_OPT45_CRIT = {
    "long_pole_job": "build-image-streaming", "long_pole_p50": 1800.0,
    "floor_p50": 60.0,
    # affected-job p50s: their SUM (2760s) × 0.2 × 400 / 60 = 3680 min/mo — well
    # above the 892.8 measured bound, so the sizing pass alone can't stay in bound.
    "job_p50": {"compute-suffix": 60.0, "build-image": 900.0,
                "build-image-streaming": 1800.0},
}
_OPT45_VOL = 400


def _opt45_doc(saving: float) -> dict:
    return {"findings": [{"pattern": "OPT45", "workflow_file": _OPT45_WF,
                          "affected_jobs": list(_OPT45_JOBS),
                          "runner_min_saving": saving}],
            "runner_minute_spine": {"render_ready": True, "rows": [
                {"workflow_file": _OPT45_WF, "job_name": j,
                 "billable_equiv_min_per_month": _OPT45_BILLABLE[j]}
                for j in _OPT45_JOBS]}}


def _bounds_tag(doc: dict, tmp_path) -> str:
    import json
    vr = _load_verify_report_for_bounds()
    fp = tmp_path / "opt45-findings.json"
    fp.write_text(json.dumps(doc), encoding="utf-8")
    res = vr.check_saving_within_measured_compute("# report\n", fp)
    return "PASS" if (res.ok and not res.skipped) else ("SKIP" if res.skipped else "FAIL")


def test_opt45_sizing_alone_overshoots_measured_compute(tmp_path):
    # The provisional sizing pass (affected-jobs p50 sum × hit_rate × full volume)
    # ignores occurrence dilution, so it credits MORE than the affected jobs
    # measurably consume — the exact overshoot the #30 guard catches.
    f = {"pattern": "OPT45", "workflow_file": _OPT45_WF,
         "affected_jobs": list(_OPT45_JOBS)}
    _size_finding(f, _OPT45_CRIT, monthly_volume=_OPT45_VOL)
    sized = f["runner_min_saving"]
    assert sized == round(0.2 * 2760.0 * _OPT45_VOL / 60.0, 1) == 3680.0
    # Above the 892.8 min/mo measured bound → the guard FAILs on the sized value.
    assert _bounds_tag(_opt45_doc(sized), tmp_path) == "FAIL"


def test_opt45_regrounds_to_measured_billable_and_passes_the_guard(tmp_path):
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT45", "workflow_file": _OPT45_WF,
         "affected_jobs": list(_OPT45_JOBS)}
    _size_finding(f, _OPT45_CRIT, monthly_volume=_OPT45_VOL)
    # Build the artifact around the SAME finding dict the sizing pass just wrote,
    # so the re-ground pass and the guard both operate on f.
    doc = _opt45_doc(f["runner_min_saving"])
    doc["findings"] = [f]
    _reground_whole_run_cancel_saving(doc["findings"], doc["runner_minute_spine"])
    # Derived (not clamped): hit_rate(0.2) × Σ(measured billable 892.8) = 178.6.
    assert f["runner_min_saving"] == round(0.2 * 892.8, 1) == 178.6
    assert f["runner_min_basis"] == "measured_spine_billable"
    assert "MEASURED" in f["size_note"] and "892.8 min/mo" in f["size_note"]
    # And the physical-bound guard now PASSes on the re-grounded artifact.
    assert _bounds_tag(doc, tmp_path) == "PASS"


def test_reground_unmatched_opt45_is_unsized_at_source(tmp_path):
    # An OPT45 whose affected jobs resolve to NO spine row (under a render-ready
    # spine that DOES carry other rows) must be UNSIZED at the source, never left
    # carrying its provisional over-credit: bound (c)'s loud SKIP only fires when
    # EVERY runner-minute finding misses the spine, so in a mixed report an
    # unmatched OPT45's unbounded provisional would otherwise render green (#33).
    from collect_runs import _reground_whole_run_cancel_saving
    ghost = {"pattern": "OPT45", "workflow_file": _OPT45_WF,
             "affected_jobs": ["ghost-job"], "runner_min_saving": 999.0}
    # A non-cost_basis runner-min-only finding (OPT40) is untouched by the pass.
    other = {"pattern": "OPT40", "workflow_file": _OPT45_WF,
             "affected_jobs": ["build-image"], "runner_min_saving": 500.0}
    spine = {"render_ready": True, "rows": [
        {"workflow_file": _OPT45_WF, "job_name": "build-image",
         "billable_equiv_min_per_month": 350.0}]}
    _reground_whole_run_cancel_saving([ghost, other], spine)
    assert ghost["runner_min_saving"] is None
    assert ghost["runner_min_basis"] == "unmeasured_no_spine_match"
    assert "not sized" in ghost["size_note"]
    assert other["runner_min_saving"] == 500.0 and "runner_min_basis" not in other
    # A mixed report where `other` is NOT bounded either — the guard would SKIP
    # loud; but the unmatched OPT45 must not render regardless. Its saving is None,
    # so it drops out of the guard's saving>0 loop entirely (never a green number).
    doc = {"findings": [ghost], "runner_minute_spine": spine}
    assert _bounds_tag(doc, tmp_path) == "SKIP"      # no credited saving to bound


def test_reground_partial_coverage_sums_only_resolved_subset(tmp_path):
    # Two of three affected jobs resolve to spine rows; the third is absent. The
    # re-ground must sum ONLY the resolved subset's measured billable (an
    # understatement, safe) and disclose the partial coverage in the note.
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT45", "workflow_file": _OPT45_WF,
         "affected_jobs": ["compute-suffix", "build-image", "ghost-job"],
         "runner_min_saving": 3680.0}
    spine = {"render_ready": True, "rows": [
        {"workflow_file": _OPT45_WF, "job_name": "compute-suffix",
         "billable_equiv_min_per_month": 100.0},
        {"workflow_file": _OPT45_WF, "job_name": "build-image",
         "billable_equiv_min_per_month": 350.0}]}
    _reground_whole_run_cancel_saving([f], spine)
    # 0.2 × (100 + 350) = 90.0 — the ghost job's minutes are NOT in the sum.
    assert f["runner_min_saving"] == round(0.2 * 450.0, 1) == 90.0
    assert f["runner_min_basis"] == "measured_spine_billable"
    assert "(2/3 affected jobs measured)" in f["size_note"]


def test_reground_reusable_workflow_by_job_fallback(tmp_path):
    # The finding's workflow_file does NOT match the spine row's (a reusable-workflow
    # caller loses the callee's file), but the job base name does → the second-tier
    # `by_job` fallback grounds on max(single-row bills) for that base name.
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT45", "workflow_file": ".github/workflows/caller.yml",
         "affected_jobs": ["build-image"], "runner_min_saving": 3680.0}
    spine = {"render_ready": True, "rows": [
        {"workflow_file": ".github/workflows/reusable-build.yml",
         "job_name": "build-image", "billable_equiv_min_per_month": 275.0}]}
    _reground_whole_run_cancel_saving([f], spine)
    assert f["runner_min_saving"] == round(0.2 * 275.0, 1) == 55.0
    assert f["runner_min_basis"] == "measured_spine_billable"


def test_reground_sums_matrix_legs_via_base_key(tmp_path):
    # A bare affected-job name must join (and SUM) its expanded matrix legs in the
    # spine — the crux of the "at least as strict as the verifier's `_base`" claim.
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT45", "workflow_file": _OPT45_WF,
         "affected_jobs": ["build-image"], "runner_min_saving": 3680.0}
    spine = {"render_ready": True, "rows": [
        {"workflow_file": _OPT45_WF, "job_name": "build-image (arm64)",
         "billable_equiv_min_per_month": 200.0},
        {"workflow_file": _OPT45_WF, "job_name": "build-image (amd64)",
         "billable_equiv_min_per_month": 150.0}]}
    _reground_whole_run_cancel_saving([f], spine)
    # Both legs collapse to base `build-image` and SUM: 0.2 × (200 + 150) = 70.0.
    assert f["runner_min_saving"] == round(0.2 * 350.0, 1) == 70.0
    assert f["runner_min_basis"] == "measured_spine_billable"


def test_reground_noop_without_render_ready_spine(tmp_path):
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT45", "workflow_file": _OPT45_WF,
         "affected_jobs": list(_OPT45_JOBS), "runner_min_saving": 3680.0}
    not_ready = {"render_ready": False, "rows": [
        {"workflow_file": _OPT45_WF, "job_name": j,
         "billable_equiv_min_per_month": _OPT45_BILLABLE[j]} for j in _OPT45_JOBS]}
    _reground_whole_run_cancel_saving([f], not_ready)
    assert f["runner_min_saving"] == 3680.0   # not render-ready → the guard SKIPs; leave as-is


def test_reground_matched_but_zero_billable_is_unsized_not_zeroed(tmp_path):
    # An affected job resolves to spine rows that carry null/0 billable (a
    # job-presence entry with missing cost data). measured stays 0, so the pass
    # must UNSIZE (omit rather than fake) rather than silently credit 0.0 — the
    # matched-but-zero divergence greptile flagged.
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT45", "workflow_file": _OPT45_WF,
         "affected_jobs": ["build-image"], "runner_min_saving": 3680.0}
    spine = {"render_ready": True, "rows": [
        # a sibling row keeps by_wf_job non-empty (so the pass doesn't early-return)
        {"workflow_file": _OPT45_WF, "job_name": "compute-suffix",
         "billable_equiv_min_per_month": 100.0},
        # build-image is present but its billable is null → _bill() reads 0.0
        {"workflow_file": _OPT45_WF, "job_name": "build-image",
         "billable_equiv_min_per_month": None}]}
    _reground_whole_run_cancel_saving([f], spine)
    assert f["runner_min_saving"] is None            # not a faked 0.0
    assert f["runner_min_basis"] == "unmeasured_no_spine_match"


def test_opt45_sizing_time_unresolved_is_None_not_faked():
    # The PROVISIONAL (no-spine) path: when NONE of the affected jobs' durations
    # resolve from sampled timings, the cost_basis:affected_jobs branch omits
    # rather than fakes — runner_min_saving is None with a qualitative note, never
    # the long pole substituted in.
    f = {"pattern": "OPT45", "workflow_file": _OPT45_WF,
         "affected_jobs": ["job-with-no-timing"]}
    _size_finding(f, _OPT45_CRIT, monthly_volume=_OPT45_VOL)
    assert f["runner_min_saving"] is None
    assert "not sized" in f["size_note"]


# --- #113: OPT29 (merge-queue STEP-LEVEL skip) must DERIVE its runner-minute
# saving from the affected job's MEASURED billable — never the workflow long
# pole. The live biome repro (biomejs/biome, merge_group, benchmark.yml): the
# `changes` gate provisions a runner on merge_group but skips every step. The
# pre-fix model priced 0.1 × the 941s workflow long pole (a heavy Bench job, NOT
# `changes`) × 823 runs/mo = 1290.7 min/mo — MORE than the `changes` job's whole
# measured monthly billable (823 min/mo). A fix cannot save more minutes than the
# job burns; `check_saving_within_measured_compute` (#30 guard) caught it. ---
_OPT29_WF = ".github/workflows/benchmark.yml"
_OPT29_JOB = "changes"
_OPT29_BILLABLE = 823.0          # `changes` measured monthly billable (cost spine)
_OPT29_LONG_POLE = 941.0         # benchmark.yml long pole (a heavy Bench job, NOT `changes`)
_OPT29_JOB_P50 = 120.0           # the `changes` gate's OWN p50 (small)
_OPT29_VOL = 823
_OPT29_CRIT = {
    "long_pole_job": "Bench JS / biome_js_formatter", "long_pole_p50": _OPT29_LONG_POLE,
    "floor_p50": 30.0,
    "job_p50": {_OPT29_JOB: _OPT29_JOB_P50},
}


def _opt29_doc(saving) -> dict:
    return {"findings": [{"pattern": "OPT29", "workflow_file": _OPT29_WF,
                          "affected_jobs": [_OPT29_JOB],
                          "runner_min_saving": saving}],
            "runner_minute_spine": {"render_ready": True, "rows": [
                {"workflow_file": _OPT29_WF, "job_name": _OPT29_JOB,
                 "billable_equiv_min_per_month": _OPT29_BILLABLE}]}}


def test_opt29_long_pole_pricing_would_break_the_physical_bound(tmp_path):
    # Documents the #113 defect MAGNITUDE: pricing the step-level skip off the
    # WORKFLOW long pole × full volume credits 1290.7 min/mo — more than the
    # `changes` job's ENTIRE measured monthly billable (823) — the live biome FAIL.
    over_credit = round(0.1 * _OPT29_LONG_POLE * _OPT29_VOL / 60.0, 1)
    assert over_credit == 1290.7
    assert _bounds_tag(_opt29_doc(over_credit), tmp_path) == "FAIL"


def test_opt29_regrounds_to_measured_billable_and_passes_the_guard(tmp_path):
    # RED on main (OPT29 had no cost_basis → the reground pre-pass skipped it and
    # the saving stayed at the long-pole-priced 1290.7). GREEN post-#113.
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT29", "workflow_file": _OPT29_WF,
         "affected_jobs": [_OPT29_JOB]}
    _size_finding(f, _OPT29_CRIT, monthly_volume=_OPT29_VOL)
    # Provisional now sizes off the affected job's OWN p50 (cost_basis:affected_jobs),
    # never the long pole — already far below the pre-fix 1290.7.
    assert f["runner_min_saving"] == round(0.1 * _OPT29_JOB_P50 * _OPT29_VOL / 60.0, 1)
    doc = _opt29_doc(f["runner_min_saving"])
    doc["findings"] = [f]
    _reground_whole_run_cancel_saving(doc["findings"], doc["runner_minute_spine"])
    # DERIVEd (not clamped): hit_rate(0.1) × measured billable(823) = 82.3 ≤ 823.
    assert f["runner_min_saving"] == round(0.1 * _OPT29_BILLABLE, 1) == 82.3
    assert f["runner_min_basis"] == "measured_spine_billable"
    assert "MEASURED" in f["size_note"] and "823 min/mo" in f["size_note"]
    # The physical-bound guard PASSes on the re-grounded artifact (82.3 ≤ 823).
    assert _bounds_tag(doc, tmp_path) == "PASS"


def test_opt29_clamp_disclosure_is_honest_ceiling(tmp_path):
    # The re-grounded note must DISCLOSE that the credit is a CEILING (only runner
    # provisioning is wasted — the steps already skip on merge_group) and that it's
    # the merge_group-run share, so the basis never reads as a measured whole-job
    # saving.
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT29", "workflow_file": _OPT29_WF, "affected_jobs": [_OPT29_JOB],
         "runner_min_saving": 999.0}
    _reground_whole_run_cancel_saving([f], _opt29_doc(999.0)["runner_minute_spine"])
    note = f["size_note"]
    assert "ceiling" in note and "provisioning" in note and "merge_group" in note
    assert f["runner_min_basis"] == "measured_spine_billable"
    assert f["runner_min_saving"] == 82.3


def test_opt29_unmatched_job_is_unsized_not_faked(tmp_path):
    # The live biome f24 (parser_conformance.yml `coverage`): a SHALLOW spine
    # workflow whose `coverage` job never resolved to a cost-spine row. The DERIVE
    # pass must UNSIZE (omit rather than fake) — never keep the long-pole figure.
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT29", "workflow_file": ".github/workflows/parser_conformance.yml",
         "affected_jobs": ["coverage"], "runner_min_saving": 85.7}
    spine = {"render_ready": True, "rows": [
        {"workflow_file": ".github/workflows/parser_conformance.yml",
         "job_name": "Parser conformance", "billable_equiv_min_per_month": 958.8}]}
    _reground_whole_run_cancel_saving([f], spine)
    assert f["runner_min_saving"] is None
    assert f["runner_min_basis"] == "unmeasured_no_spine_match"
    assert "not sized" in f["size_note"]


def test_opt35_static_fallback_sizes_off_the_matrix_job_not_the_long_pole(tmp_path):
    # Sibling audit (#113): OPT35's STATIC fallback names ONE matrix job. A shard
    # matrix that is NOT the workflow long pole (a 200s matrix beside a 2000s job)
    # must be sized off ITS OWN p50 (scope:"job"), never the long pole. RED on main
    # (no scope → priced at the 2000s long pole → 2000 min/mo, above the matrix
    # job's 400 min/mo measured billable → physical-bound FAIL). GREEN post-fix.
    wf = ".github/workflows/test.yml"
    crit = {"long_pole_job": "build", "long_pole_p50": 2000.0, "floor_p50": 30.0,
            "job_p50": {"shard-tests": 200.0}}
    f = {"pattern": "OPT35", "workflow_file": wf, "affected_jobs": ["shard-tests"]}
    _size_finding(f, crit, monthly_volume=600)
    # Sized off the matrix job's OWN p50: 0.1 × 200 × 600 / 60 = 200 (not 2000).
    assert f["runner_min_saving"] == round(0.1 * 200.0 * 600 / 60.0, 1) == 200.0
    doc = {"findings": [f], "runner_minute_spine": {"render_ready": True, "rows": [
        {"workflow_file": wf, "job_name": "shard-tests",
         "billable_equiv_min_per_month": 400.0}]}}
    assert _bounds_tag(doc, tmp_path) == "PASS"           # 200 ≤ 400
    # And the pre-fix long-pole price (2000) would have broken the bound.
    over = dict(f); over["runner_min_saving"] = round(0.1 * 2000.0 * 600 / 60.0, 1)
    assert over["runner_min_saving"] == 2000.0
    assert _bounds_tag({"findings": [over], "runner_minute_spine": doc["runner_minute_spine"]},
                       tmp_path) == "FAIL"


def test_opt29_flows_through_the_real_door_not_just_the_prepass(tmp_path):
    # The other OPT29 tests call the reground PRE-PASS directly; pin that OPT29 also
    # resolves correctly through `_reground_runner_minute_savings` (the actual door
    # the pipeline runs), which dispatches DERIVE via _rm_door_policy → the OPT45
    # pre-pass. A mis-registration (dropped override) would leave it unstamped /
    # UNCLASSIFIED here rather than measured_spine_billable.
    from collect_runs import _reground_runner_minute_savings
    f = {"pattern": "OPT29", "workflow_file": _OPT29_WF, "affected_jobs": [_OPT29_JOB],
         "runner_min_saving": 1290.7}          # the pre-fix long-pole-priced figure
    _reground_runner_minute_savings([f], _opt29_doc(1290.7)["runner_minute_spine"])
    assert f["runner_min_saving"] == 82.3      # DERIVEd from measured billable, not 1290.7
    assert f["runner_min_basis"] == "measured_spine_billable"


def test_opt35_static_fuzzy_wrong_sibling_is_backstopped_by_the_guard(tmp_path):
    # Greptile P2 / test-analyzer gap: scope:"job" routes OPT35 static through the
    # fuzzy _resolve_job_p50, whose leading-word fallback can bind the affected key
    # `shard-tests` to a DIFFERENT sibling display name `shard-tests-helpers` when no
    # exact/matrix p50 exists ("shard tests helpers".startswith("shard tests ")). The
    # provisional then sizes off the wrong (here larger) job — but this can never SHIP
    # an over-credit: the physical-bound guard joins the finding's OWN affected job
    # (`shard-tests`) to the spine and FAILs when the credited figure exceeds that
    # job's measured billable. (The total name-override miss — real job absent from
    # the spine — is the pre-existing #123-class identity gap shared by every
    # scope:"job" pattern, not introduced here; it renders as an unbounded SKIP.)
    wf = ".github/workflows/test.yml"
    crit = {"long_pole_job": "build", "long_pole_p50": 2000.0, "floor_p50": 30.0,
            "job_p50": {"shard-tests-helpers": 500.0}}   # only the SIBLING is timed
    f = {"pattern": "OPT35", "workflow_file": wf, "affected_jobs": ["shard-tests"]}
    _size_finding(f, crit, monthly_volume=600)
    # Fuzzy mis-resolution: sized off the sibling's 500, not the real (untimed) job.
    assert f["runner_min_saving"] == round(0.1 * 500.0 * 600 / 60.0, 1) == 500.0
    # Guard backstop: the real `shard-tests` measures 400 in the spine → 500 > 400
    # is caught as a loud FAIL, so the mis-priced figure cannot ship green.
    doc = {"findings": [dict(f)], "runner_minute_spine": {"render_ready": True, "rows": [
        {"workflow_file": wf, "job_name": "shard-tests",
         "billable_equiv_min_per_month": 400.0}]}}
    assert _bounds_tag(doc, tmp_path) == "FAIL"


# --- The measured sizing DOOR (issues #43/#44/#45) ---------------------------
# `_reground_runner_minute_savings` is the single post-spine pass every rm-crediting
# finding flows through: OPT45 derives, OPT73 clamps, the rest stamp a visible basis.
_OPT73_WF = ".github/workflows/test.yml"
# nrwl/nx round-3b: OPT73 credited 1919.7 min/mo but its 4 affected cluster jobs
# measure 1404.4 min/mo in the cost spine — the #43 overstatement. (Distinct job
# bases, like the real nx cluster — that is why `check_saving_within_measured_compute`
# summed 1404.4 and caught the overshoot instead of double-counting one base.)
_OPT73_JOBS = ["e2e-nx", "e2e-vite", "e2e-webpack", "e2e-esbuild"]
_OPT73_BILLABLE = {"e2e-nx": 351.1, "e2e-vite": 351.1,
                   "e2e-webpack": 351.1, "e2e-esbuild": 351.1}   # Σ = 1404.4


def _door_spine(billable: dict, wf=_OPT73_WF) -> dict:
    return {"render_ready": True, "rows": [
        {"workflow_file": wf, "job_name": j, "billable_equiv_min_per_month": b}
        for j, b in billable.items()]}


def test_opt73_saving_clamped_to_measured_billable(tmp_path):
    # #43 proving instance (nrwl/nx): OPT73's modeled cluster-step credit (1919.7)
    # exceeds the affected jobs' MEASURED billable (1404.4). The door clamps it
    # DOWN to the measured figure, stamps the basis, and the physical-bounds guard
    # then PASSES on the re-grounded artifact.
    from collect_runs import _reground_runner_minute_savings
    f = {"pattern": "OPT73", "workflow_file": _OPT73_WF,
         "affected_jobs": list(_OPT73_JOBS), "runner_min_saving": 1919.7}
    doc = {"findings": [f], "runner_minute_spine": _door_spine(_OPT73_BILLABLE)}
    _reground_runner_minute_savings(doc["findings"], doc["runner_minute_spine"])
    assert f["runner_min_saving"] == 1404.4, "clamped to the measured billable"
    assert f["runner_min_basis"] == "measured_spine_clamped"
    assert "cannot save more minutes than the jobs consume" in f["size_note"]
    assert _bounds_tag(doc, tmp_path) == "PASS"


def test_opt73_within_measured_is_confirmed_not_clamped():
    # An OPT73 already within the measured billable is CONFIRMED (basis stamped)
    # and its figure is untouched — the clamp only ever lowers.
    from collect_runs import _reground_runner_minute_savings
    f = {"pattern": "OPT73", "workflow_file": _OPT73_WF,
         "affected_jobs": list(_OPT73_JOBS), "runner_min_saving": 900.0}
    _reground_runner_minute_savings([f], _door_spine(_OPT73_BILLABLE))
    assert f["runner_min_saving"] == 900.0
    assert f["runner_min_basis"] == "measured_spine_billable"


# --- issue #52: exact job identity — matrix legs of one job counted ONCE ------
# mastodon round-5: build-push-pr.yml's OPT73 named its cluster as the two matrix
# legs of ONE reusable-workflow job (base `build-image / build-image`), and the
# spine ALSO carries a name-similar-but-DIFFERENT job (`build-image-streaming /
# build-image`). The pre-fix join iterated the raw legs and re-added the base's
# already-summed billable once per leg, doubling the bound so an over-credit escaped
# the clamp. The fix reduces the affected jobs to DISTINCT (wf, base) identities.
_COLLIDE_WF = ".github/workflows/build-push-pr.yml"
_COLLIDE_LEGS = ["build-image / build-image (linux/amd64)",
                 "build-image / build-image (linux/arm64)"]
_COLLIDE_ROWS = {
    "build-image / build-image (linux/amd64)": 8128.996,
    "build-image / build-image (linux/arm64)": 8513.411,          # Σ build-image = 16,642.407
    "build-image-streaming / build-image (linux/amd64)": 1154.111,  # DIFFERENT job — must not fold in
    "build-image-streaming / build-image (linux/arm64)": 1202.596,
}
_COLLIDE_BILLABLE = 16642.407   # the two build-image legs, counted once


def test_measured_billable_dedupes_matrix_legs_of_one_job():
    # Unit proof of the join: listing two matrix legs of ONE job sums that job's
    # billable ONCE (distinct==1), never once-per-leg. Streaming stays separate.
    from collect_runs import _measured_billable_index, _measured_billable_for_jobs
    idx = _measured_billable_index(_door_spine(_COLLIDE_ROWS, wf=_COLLIDE_WF))
    by_wf_job, by_job = idx
    measured, matched, distinct = _measured_billable_for_jobs(
        _COLLIDE_WF, list(_COLLIDE_LEGS), by_wf_job, by_job)
    assert round(measured, 3) == _COLLIDE_BILLABLE   # 16,642.407, NOT 33,284.814
    assert matched == 1 and distinct == 1            # ONE job identity, fully covered


def test_measured_billable_bare_base_still_aggregates_legs():
    # No under-match regression: a bare base job name must still join AND SUM all
    # its expanded matrix legs (the "legs of one job still aggregate" direction).
    from collect_runs import _measured_billable_index, _measured_billable_for_jobs
    idx = _measured_billable_index(_door_spine(_COLLIDE_ROWS, wf=_COLLIDE_WF))
    by_wf_job, by_job = idx
    measured, matched, distinct = _measured_billable_for_jobs(
        _COLLIDE_WF, ["build-image / build-image"], by_wf_job, by_job)
    assert round(measured, 3) == _COLLIDE_BILLABLE   # both legs summed under the base
    assert matched == 1 and distinct == 1


def test_opt73_matrix_leg_collision_clamps_not_double_counts(tmp_path):
    # THE issue #52 shape. A credit (18,165.8) that sits BETWEEN the honest bound
    # (16,642.4, the two build-image legs) and the pre-fix doubled bound (33,284.8)
    # must CLAMP to the honest bound — pre-fix it read as within measured compute and
    # rendered unclamped. The name-similar `build-image-streaming` legs never fold in.
    from collect_runs import _reground_runner_minute_savings
    f = {"pattern": "OPT73", "workflow_file": _COLLIDE_WF,
         "affected_jobs": list(_COLLIDE_LEGS), "runner_min_saving": 18165.8}
    doc = {"findings": [f], "runner_minute_spine": _door_spine(_COLLIDE_ROWS, wf=_COLLIDE_WF)}
    _reground_runner_minute_savings(doc["findings"], doc["runner_minute_spine"])
    assert f["runner_min_saving"] == 16642.4, "clamped to the two build-image legs, counted once"
    assert f["runner_min_basis"] == "measured_spine_clamped"
    assert "cannot save more minutes than the jobs consume" in f["size_note"]
    # And the physical-bounds guard PASSes on the re-grounded artifact (the two gates
    # tighten together — the door never out-derives the guard).
    assert _bounds_tag(doc, tmp_path) == "PASS"


def test_door_bound_never_exceeds_guard_bound_on_collision(tmp_path):
    # Subset-strictness (PR #38/#47): the door's join may never match MORE compute
    # than the guard's. Both now dedupe by DISTINCT (wf, base) identity, so on the
    # collision shape they compute the SAME bound (equal ⊆) — 16,642.4, not doubled.
    from collect_runs import _measured_billable_index, _measured_billable_for_jobs
    spine = _door_spine(_COLLIDE_ROWS, wf=_COLLIDE_WF)
    by_wf_job, by_job = _measured_billable_index(spine)
    door_bound, _m, _d = _measured_billable_for_jobs(
        _COLLIDE_WF, list(_COLLIDE_LEGS), by_wf_job, by_job)
    # A saving well above the guard bound (past its 2% directional tolerance) must
    # FAIL; the door's bound must not exceed the guard's, so the door can never
    # certify a saving the guard would reject.
    doc = {"findings": [{"pattern": "OPT73", "workflow_file": _COLLIDE_WF,
                         "affected_jobs": list(_COLLIDE_LEGS),
                         "runner_min_saving": round(door_bound, 1) * 1.10}],
           "runner_minute_spine": spine}
    assert _bounds_tag(doc, tmp_path) == "FAIL"      # guard bound == door bound; +10% overshoots
    assert round(door_bound, 3) == _COLLIDE_BILLABLE


def test_measured_billable_two_distinct_matrix_bases_both_sum():
    # Over-dedup guard (the dangerous UNDER-match direction): a finding naming legs
    # of TWO DIFFERENT matrix jobs must sum BOTH distinct bases, never collapse them.
    # `build-image / build-image` (16,642.4) + `build-image-streaming / build-image`
    # (2,356.7) → 18,999.1, distinct==2 — the #33 multi-job shape.
    from collect_runs import _measured_billable_index, _measured_billable_for_jobs
    by_wf_job, by_job = _measured_billable_index(_door_spine(_COLLIDE_ROWS, wf=_COLLIDE_WF))
    measured, matched, distinct = _measured_billable_for_jobs(
        _COLLIDE_WF,
        ["build-image / build-image (linux/amd64)",
         "build-image-streaming / build-image (linux/arm64)"],
        by_wf_job, by_job)
    assert round(measured, 3) == round(16642.407 + 2356.707, 3)   # both bases summed
    assert matched == 2 and distinct == 2


def test_measured_billable_dedupes_legs_via_by_job_fallback():
    # The dedupe must also cover the reusable-workflow `by_job` fallback (finding's
    # workflow_file misses the spine row's file): two legs of ONE job resolving via
    # the workflow-agnostic fallback must take that base's figure ONCE, not per leg.
    from collect_runs import _measured_billable_index, _measured_billable_for_jobs
    spine = {"render_ready": True, "rows": [
        {"workflow_file": ".github/workflows/reusable-build.yml",
         "job_name": "build-image / build-image (linux/amd64)",
         "billable_equiv_min_per_month": 8128.996},
        {"workflow_file": ".github/workflows/reusable-build.yml",
         "job_name": "build-image / build-image (linux/arm64)",
         "billable_equiv_min_per_month": 8513.411}]}
    by_wf_job, by_job = _measured_billable_index(spine)
    measured, matched, distinct = _measured_billable_for_jobs(
        ".github/workflows/caller.yml", list(_COLLIDE_LEGS), by_wf_job, by_job)
    # Fallback grounds on max(single-row bills) for the base, counted ONCE (not 2×).
    assert round(measured, 3) == 8513.411
    assert matched == 1 and distinct == 1


def test_door_clamp_partial_note_counts_distinct_bases_not_legs(tmp_path):
    # Denominator pin (clamp path): with legs of ONE job that resolve PLUS a distinct
    # unresolved base, the partial-coverage note must read the DISTINCT-base fraction
    # (1/2), not the raw-leg fraction (1/3). Reverting the note to `len(jobs)` would
    # mislabel this "(1/3 affected jobs measured)".
    from collect_runs import _reground_runner_minute_savings
    f = {"pattern": "OPT73", "workflow_file": _COLLIDE_WF,
         "affected_jobs": list(_COLLIDE_LEGS) + ["ghost / ghost (linux/amd64)"],
         "runner_min_saving": 25000.0}
    _reground_runner_minute_savings([f], _door_spine(_COLLIDE_ROWS, wf=_COLLIDE_WF))
    assert f["runner_min_saving"] == 16642.4          # clamped to the one resolved job
    assert f["runner_min_basis"] == "measured_spine_clamped"
    assert "(1/2 affected jobs measured)" in f["size_note"]
    assert "1/3" not in f["size_note"]


def test_door_derive_partial_note_counts_distinct_bases_not_legs():
    # Denominator pin (OPT45 DERIVE path): same distinct-vs-leg fraction on the
    # cancel-rate note built by `_reground_whole_run_cancel_saving`.
    from collect_runs import _reground_whole_run_cancel_saving
    f = {"pattern": "OPT45", "workflow_file": _COLLIDE_WF,
         "affected_jobs": list(_COLLIDE_LEGS) + ["ghost / ghost (linux/amd64)"],
         "runner_min_saving": 3680.0}
    _reground_whole_run_cancel_saving([f], _door_spine(_COLLIDE_ROWS, wf=_COLLIDE_WF))
    assert f["runner_min_saving"] == round(0.2 * 16642.407, 1)   # rate × the one resolved job
    assert "(1/2 affected jobs measured)" in f["size_note"]
    assert "1/3" not in f["size_note"]


def test_guard_partial_label_counts_distinct_bases_not_legs(tmp_path):
    # Denominator pin (guard side): the guard's partial-coverage label must read the
    # DISTINCT-base fraction (1/2), mirroring the door — reverting to `len(jobs)`
    # would emit a spurious "1/3 jobs".
    import json
    vr = _load_verify_report_for_bounds()
    doc = {"findings": [{"pattern": "OPT73", "workflow_file": _COLLIDE_WF,
                         "affected_jobs": list(_COLLIDE_LEGS) + ["ghost / ghost (linux/amd64)"],
                         "runner_min_saving": 16642.4}],
           "runner_minute_spine": _door_spine(_COLLIDE_ROWS, wf=_COLLIDE_WF)}
    fp = tmp_path / "partial-findings.json"
    fp.write_text(json.dumps(doc), encoding="utf-8")
    res = vr.check_saving_within_measured_compute("# report\n", fp)
    assert res.ok and not res.skipped, res.detail
    assert "OPT73(1/2 jobs)" in res.detail
    assert "1/3 jobs" not in res.detail


def test_opt73_unmatched_jobs_unsized_at_source():
    # An OPT73 whose affected jobs resolve to NO spine row is UNSIZED (None +
    # basis), never left carrying its unbounded modeled figure (the #38 discipline).
    from collect_runs import _reground_runner_minute_savings
    f = {"pattern": "OPT73", "workflow_file": _OPT73_WF,
         "affected_jobs": ["ghost (1)", "ghost (2)"], "runner_min_saving": 1919.7}
    # a sibling row keeps the spine index non-empty
    spine = _door_spine({"other-job": 200.0})
    _reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] is None
    assert f["runner_min_basis"] == "unmeasured_no_spine_match"


def test_door_stamps_not_derivable_whitelist_with_reason():
    # A modeled/measured pattern outside the spine-bound set retains its saving but
    # is STAMPED not_spine_derivable with a visible reason — a whitelist, not a
    # silent bypass. (OPT46 = a measured run-elimination detector.)
    from collect_runs import _reground_runner_minute_savings
    f = {"pattern": "OPT46", "workflow_file": _OPT73_WF,
         "affected_jobs": ["e2e (1)"], "runner_min_saving": 500.0}
    _reground_runner_minute_savings([f], _door_spine(_OPT73_BILLABLE))
    assert f["runner_min_saving"] == 500.0
    assert f["runner_min_basis"] == "not_spine_derivable"
    assert f["runner_min_door_note"]   # a reason is recorded


def test_door_policy_is_total_and_flags_unclassified():
    # A rm-crediting pattern with NO declared door policy stamps the loud
    # UNCLASSIFIED sentinel (so verify_report FAILs) — a new pattern cannot ship
    # an unmeasured sizing path silently.
    from collect_runs import _reground_runner_minute_savings, _rm_door_policy
    assert _rm_door_policy("OPT45")[0] == "derive"
    # OPT29 (#113) joined the DERIVE door as a cost_basis:affected_jobs pattern —
    # pin it so dropping the _RM_DOOR_OVERRIDES entry (while keeping cost_basis)
    # can't silently route it back through an unstamped path.
    assert _rm_door_policy("OPT29")[0] == "derive"
    assert _rm_door_policy("OPT73")[0] == "clamp"
    assert _rm_door_policy("OPT13")[0] == "not_spine_derivable"   # direct model
    assert _rm_door_policy("OPT99_new")[0] == "unclassified"
    # The structural step-decomposition levers are on the EXPLICIT whitelist (per-job
    # step basis, not per-job billable) — pin them so flipping an override is caught.
    for pat in ("OPT70", "OPT71", "OPT72", "OPT74", "OPT75"):
        pol, why = _rm_door_policy(pat)
        assert pol == "not_spine_derivable", f"{pat} must stay whitelisted"
        assert why, f"{pat} whitelist reason recorded"
    # And the measured run-elimination detectors resolve via their _SIZING model.
    for pat in ("OPT46", "OPT47", "OPT64", "OPT65"):
        assert _rm_door_policy(pat)[0] == "not_spine_derivable"
    f = {"pattern": "OPT99_new", "workflow_file": _OPT73_WF,
         "affected_jobs": ["e2e (1)"], "runner_min_saving": 42.0}
    _reground_runner_minute_savings([f], _door_spine(_OPT73_BILLABLE))
    assert f["runner_min_basis"] == "UNCLASSIFIED_door_policy"


def test_door_subsumes_opt45_derive_unchanged(tmp_path):
    # The generalized pass must SUBSUME the OPT45 reground (#38), not break it:
    # OPT45 still derives hit_rate × measured billable with its measured basis.
    from collect_runs import _reground_runner_minute_savings
    f = {"pattern": "OPT45", "workflow_file": _OPT45_WF,
         "affected_jobs": list(_OPT45_JOBS)}
    _size_finding(f, _OPT45_CRIT, monthly_volume=_OPT45_VOL)
    doc = _opt45_doc(f["runner_min_saving"])
    doc["findings"] = [f]
    _reground_runner_minute_savings(doc["findings"], doc["runner_minute_spine"])
    assert f["runner_min_saving"] == round(0.2 * 892.8, 1) == 178.6
    assert f["runner_min_basis"] == "measured_spine_billable"
    assert _bounds_tag(doc, tmp_path) == "PASS"


def test_door_no_render_ready_spine_keeps_figure_stamps_unmeasured():
    # No render-ready spine at all: the verifier's basis invariant SKIPs, so there
    # is nothing to require. The door stamps `unmeasured_no_spine` and KEEPS the
    # modeled figure — the two gates agree to do nothing.
    from collect_runs import _reground_runner_minute_savings
    f = {"pattern": "OPT73", "workflow_file": _OPT73_WF,
         "affected_jobs": list(_OPT73_JOBS), "runner_min_saving": 1919.7}
    _reground_runner_minute_savings([f], None)
    assert f["runner_min_saving"] == 1919.7
    assert f["runner_min_basis"] == "unmeasured_no_spine"


def test_door_render_ready_spine_but_no_joinable_rows_unsizes():
    # A render-ready spine whose rows carry NO joinable workflow_file/job_name
    # yields an empty index. The verifier does NOT skip here (render-ready + rows),
    # so keeping the modeled figure stamped `unmeasured_no_spine` would read green
    # unbounded — the door's own fail-open. The door must UNSIZE at the source
    # instead, exactly like a per-finding join miss.
    from collect_runs import _reground_runner_minute_savings
    spine = {"render_ready": True,
             "rows": [{"billable_equiv_min_per_month": 300.0}]}   # no wf/job keys
    f = {"pattern": "OPT73", "workflow_file": _OPT73_WF,
         "affected_jobs": list(_OPT73_JOBS), "runner_min_saving": 1919.7}
    _reground_runner_minute_savings([f], spine)
    assert f["runner_min_saving"] is None
    assert f["runner_min_basis"] == "unmeasured_no_spine_match"


def test_door_derive_without_prepass_is_flagged_not_silently_clamped():
    # A DERIVE pattern re-derives rate × measured in its OWN pre-pass; reaching the
    # shared clamp loop unstamped means its pre-pass never ran. The door must FLAG
    # it (UNCLASSIFIED → verify FAILs), never fall through to min(modeled, measured)
    # — for rate < 1 that clamp OVERSTATES by 1/rate while stamping a trustworthy
    # basis. Register a derive pattern with no pre-pass and prove it's flagged.
    from collect_runs import (_reground_runner_minute_savings, _RM_DOOR_OVERRIDES,
                              _RM_DOOR_DERIVE)
    _RM_DOOR_OVERRIDES["OPT_TEST_DERIVE"] = (_RM_DOOR_DERIVE, "test — no pre-pass")
    try:
        f = {"pattern": "OPT_TEST_DERIVE", "workflow_file": _OPT73_WF,
             "affected_jobs": list(_OPT73_JOBS), "runner_min_saving": 5000.0}
        _reground_runner_minute_savings([f], _door_spine(_OPT73_BILLABLE))
        assert f["runner_min_basis"] == "UNCLASSIFIED_door_policy"
        assert f["runner_min_saving"] == 5000.0, "not silently clamped to measured"
    finally:
        del _RM_DOOR_OVERRIDES["OPT_TEST_DERIVE"]


# --- issue #2: YAML key ↔ `name:` override join -------------------------------
# biomejs/biome: OPT33 names its job by YAML KEY (`lint` in pull_request.yml), but
# the spine records that job under its `name:` OVERRIDE, expanded per matrix leg
# (`Lint project (depot-ubuntu-24.04-arm-16)` + `(depot-windows-2022)`, Σ 13,381.6
# min/mo). The bare key missed the join, and the cross-workflow same-name FALLBACK
# then mis-bound it to an UNRELATED job literally named `lint` in
# pull_request_markdown.yml (553.0) → false FAIL. The join must resolve key ↔ name
# through the scanned `workflow_job_graph`, and a same-workflow resolution must
# always beat the cross-workflow fallback.
_NAME_WF = ".github/workflows/pull_request.yml"
_NAME_OTHER_WF = ".github/workflows/pull_request_markdown.yml"
_NAME_ROWS = [
    (_NAME_WF, "Lint project (depot-ubuntu-24.04-arm-16)", 2411.8),
    (_NAME_WF, "Lint project (depot-windows-2022)", 10969.8),   # Σ Lint project = 13,381.6
    (_NAME_OTHER_WF, "lint", 553.0),                            # UNRELATED job, same bare name
]
_NAME_BILLABLE = 13381.6
_NAME_GRAPH = {
    _NAME_WF: {"lint": {"name": "Lint project", "needs": [], "matrix": True}},
    _NAME_OTHER_WF: {"lint": {"name": "lint", "needs": [], "matrix": False}},
}


def _name_doc(saving: float, jobs=("lint",), wf=_NAME_WF, graph=_NAME_GRAPH) -> dict:
    doc = {"findings": [{"pattern": "OPT33", "workflow_file": wf,
                         "affected_jobs": list(jobs), "runner_min_saving": saving}],
           "runner_minute_spine": {"render_ready": True, "rows": [
               {"workflow_file": w, "job_name": j, "billable_equiv_min_per_month": b}
               for w, j, b in _NAME_ROWS]}}
    if graph is not None:
        doc["workflow_job_graph"] = graph
    return doc


def _name_result(doc: dict, tmp_path):
    import json
    vr = _load_verify_report_for_bounds()
    fp = tmp_path / "name-override-findings.json"
    fp.write_text(json.dumps(doc), encoding="utf-8")
    return vr.check_saving_within_measured_compute("# report\n", fp)


def test_name_overridden_job_binds_via_the_scanned_job_graph(tmp_path):
    # THE issue #2 shape. 3187.9 is well within the real job's 13,381.6 measured
    # compute; pre-fix the key `lint` fell through to the cross-workflow fallback
    # and bound the unrelated 553.0 job → false FAIL.
    res = _name_result(_name_doc(3187.9), tmp_path)
    assert res.ok and not res.skipped, res.detail
    assert "partial coverage" not in res.detail and "coverage gap" not in res.detail


def test_name_override_join_aggregates_matrix_legs_and_beats_the_fallback(tmp_path):
    # The bound is the SUM of the resolved job's matrix legs (13,381.6) — not one
    # leg, and never the same-named foreign job (553.0). A credit just above the
    # summed legs still FAILs, so the wider join never becomes a blanket pass.
    assert _name_result(_name_doc(_NAME_BILLABLE * 0.99), tmp_path).ok
    over = _name_result(_name_doc(_NAME_BILLABLE * 1.10), tmp_path)
    assert not over.ok and not over.skipped
    assert "13381.6" in over.detail   # bounded by the real job, not the 553.0 namesake


def test_name_override_join_resolves_a_display_name_back_to_its_key(tmp_path):
    # Reverse direction: a finding that names the DISPLAY name resolves the same
    # way (and a bare leg name still folds into its base's summed compute).
    assert _name_result(_name_doc(3187.9, jobs=("Lint project",)), tmp_path).ok
    assert _name_result(
        _name_doc(3187.9, jobs=("Lint project (depot-windows-2022)",)), tmp_path).ok


def test_name_override_join_leaves_an_absent_job_uncovered(tmp_path):
    # No new severity semantics: a job genuinely absent from the spine stays a
    # coverage gap (nothing bounded → loud SKIP), exactly as before the fix.
    res = _name_result(_name_doc(3187.9, jobs=("documentation",)), tmp_path)
    assert res.skipped and "coverage gap" in res.detail


def test_name_override_alias_never_widens_the_cross_workflow_fallback(tmp_path):
    # A graph-resolved alias is evidence about ITS OWN workflow only. `deploy` in
    # pull_request.yml renders as `Deploy app` but has NO spine row there; an
    # UNRELATED `Deploy app` in another workflow bills 50,000. Carrying the alias
    # into the cross-workflow fallback would bind that foreign compute and let a
    # 40,000 credit read "within measured compute" — an inflated upper bound
    # masking an oversized finding. The fallback stays on the LITERAL base, so
    # this stays an honest coverage gap (nothing bounded → loud SKIP).
    doc = _name_doc(40000.0, jobs=("deploy",))
    doc["workflow_job_graph"] = {
        _NAME_WF: {"deploy": {"name": "Deploy app", "needs": [], "matrix": False}},
        _NAME_OTHER_WF: {"deploy_app": {"name": "Deploy app", "needs": [], "matrix": False}},
    }
    doc["runner_minute_spine"]["rows"].append(
        {"workflow_file": _NAME_OTHER_WF, "job_name": "Deploy app",
         "billable_equiv_min_per_month": 50000.0})
    res = _name_result(doc, tmp_path)
    assert res.skipped and "coverage gap" in res.detail, res.detail


def test_name_override_alias_skips_a_colliding_display_name(tmp_path):
    # Two job KEYS in one workflow rendering to the SAME display name are one row in
    # the spine (which indexes by name), so that row's compute belongs to BOTH jobs.
    # Aliasing either key onto it would bound the finding by the SUM and inflate the
    # ceiling — a 9,000 credit against a 5,000-min job would read "within measured
    # compute" off its twin's 5,000. An ambiguous name yields no alias, so this stays
    # an honest coverage gap (nothing bounded → loud SKIP).
    doc = _name_doc(9000.0, jobs=("test_unit",))
    doc["workflow_job_graph"] = {
        _NAME_WF: {"test_unit": {"name": "Test suite", "needs": [], "matrix": False},
                   "test_e2e": {"name": "Test suite", "needs": [], "matrix": False}},
    }
    doc["runner_minute_spine"]["rows"].append(
        {"workflow_file": _NAME_WF, "job_name": "Test suite",
         "billable_equiv_min_per_month": 10000.0})
    res = _name_result(doc, tmp_path)
    assert res.skipped and "coverage gap" in res.detail, res.detail


def test_name_override_join_without_a_graph_keeps_the_legacy_fallback(tmp_path):
    # An artifact predating `workflow_job_graph` keeps today's behavior: the bare
    # key resolves only through the cross-workflow same-name fallback (553.0), so
    # the same credit FAILs. The graph is what makes the join correct.
    res = _name_result(_name_doc(3187.9, graph=None), tmp_path)
    assert not res.ok and not res.skipped and "553" in res.detail

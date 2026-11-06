"""The wall-clock bound cascade (wall_clock.size_wall_clock).

`wall_clock` is a stdlib-only leaf module, so — unlike `report` — it imports
cleanly under pytest without the config/report module-name collision. These
tests pin the cascade's two invariants (monotonic-down, no-silent-shrink), the
derivation chain, and the two shipped bounds (within-workflow floor, cross-
workflow concurrency floor).

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_wall_clock_cascade.py
"""

from __future__ import annotations

import pytest

from wall_clock import (
    BoundResult,
    SharedSubstepCredit,
    WallClockContext,
    bound_cross_workflow,
    bound_developer_facing,
    bound_measured_critical_path,
    bound_within_workflow,
    credit_detrigger,
    credit_shared_substep,
    size_wall_clock,
)


def test_measured_critical_path_zeroes_non_gating_check():
    # The mastra e2e-docs bug: Docs E2E (334s) is NOT the slowest check on a
    # representative PR (CodeQL 1359s, changed-tests 788s run concurrently), so
    # shortening it saves ZERO developer wall-clock. The measured floor — sourced
    # from check-runs, which see CodeQL even though it has no workflow file —
    # zeroes it with a reason naming the gating check.
    ctx = WallClockContext(
        workflow="e2e-docs.yml",
        own_check_names=frozenset({"Docs E2E tests"}),
        pr_checks=(("CodeQL", 1359.0), ("changed-tests", 788.0),
                   ("Docs E2E tests", 334.0)))
    res = size_wall_clock(150.0, ctx)
    assert res.effective_s == 0.0
    assert res.derivation[-1][0] == "measured-critical-path"
    assert "CodeQL" in res.derivation[-1][3]


def test_measured_critical_path_zeroes_modeled_finding_with_no_sampled_job():
    # The mastra OPT58 bug: a turbo.json config finding has NO sampled job
    # (own_check_names empty) and a purely MODELED 30s. We DO have the measured
    # PR critical path (changed-tests 752, CodeQL ...), proving turbo.json
    # doesn't run as a check on a representative PR. A modeled saving on a
    # workflow that isn't on the critical path at all isn't developer wall-clock
    # — zero it (it keeps its runner-min / cache-hygiene value).
    ctx = WallClockContext(
        workflow="turbo.json",
        own_check_names=frozenset(),
        pr_checks=(("changed-tests", 752.5), ("CodeQL", 700.0),
                   ("Docs E2E tests", 341.5)))
    res = size_wall_clock(30.0, ctx)
    assert res.effective_s == 0.0
    assert res.derivation[-1][0] == "measured-critical-path"
    assert "no sampled run" in res.derivation[-1][3]


def test_measured_critical_path_population_weighted_when_gate_is_bimodal():
    # M2: the merge gate (changed-tests) is bimodal — it gates code PRs (752s)
    # but self-skips on docs PRs, where Docs E2E (344s) becomes the pole. A
    # finding on Docs E2E must earn its real wall-clock for the docs population,
    # share-weighted, instead of being zeroed by a gate it never runs alongside.
    full = (("changed-tests", 752.0), ("Docs E2E tests", 344.0), ("Lint", 263.0))
    skip = (("Docs E2E tests", 344.0), ("Lint", 263.0))  # gate self-skipped
    ctx = WallClockContext(
        workflow="e2e-docs.yml",
        own_check_names=frozenset({"Docs E2E tests"}),
        pr_checks=full,
        pr_check_populations=((0.6, full), (0.4, skip)))
    res = size_wall_clock(170.0, ctx)
    # 60% gated by changed-tests → 0; 40% pole is Docs E2E → min(170, 344-263)=81.
    assert res.effective_s == round(0.6 * 0 + 0.4 * 81.0, 1)  # 32.4
    assert res.derivation[-1][0] == "measured-critical-path"
    assert "population-weighted" in res.derivation[-1][3]


def test_measured_critical_path_full_value_when_pole_in_every_population():
    # When this finding's check is the pole in EVERY population (bimodal gate, but
    # the gate never out-runs this check), the share-weighted expected value
    # equals the raw saving → pass through the full value with NO floor reason
    # (nothing gated it anywhere). Guards the `weighted >= value` short-circuit.
    a = (("Docs E2E tests", 900.0), ("Lint", 100.0))
    b = (("Docs E2E tests", 880.0), ("Lint", 120.0))
    ctx = WallClockContext(
        workflow="e2e-docs.yml",
        own_check_names=frozenset({"Docs E2E tests"}),
        pr_checks=a,
        pr_check_populations=((0.5, a), (0.5, b)))
    res = size_wall_clock(170.0, ctx)
    assert res.effective_s == 170.0  # nothing gated it in any population
    # No measured-critical-path floor step was applied (it didn't lower the value).
    assert all(step[0] != "measured-critical-path" for step in res.derivation)


def test_measured_critical_path_caps_a_gating_check_at_next_check():
    # If THIS workflow IS the slowest check (CodeQL 1359), its saving floors at
    # the next-slowest concurrent check (changed-tests 788) — not zero.
    ctx = WallClockContext(
        workflow="codeql",
        own_check_names=frozenset({"CodeQL"}),
        pr_checks=(("CodeQL", 1359.0), ("changed-tests", 788.0),
                   ("Docs E2E tests", 334.0)))
    res = size_wall_clock(800.0, ctx)
    assert res.effective_s == 571.0  # 1359 - 788
    assert "changed-tests" in res.derivation[-1][3]


def test_measured_critical_path_passthrough_without_check_data():
    # No check-runs available → measured bound is a no-op (cross-workflow
    # fallback handles it).
    ctx = WallClockContext(workflow="e2e-docs.yml",
                           own_check_names=frozenset({"Docs E2E tests"}))
    assert size_wall_clock(150.0, ctx).effective_s == 150.0

# ci.yml-like: long pole `test` 439s, cluster floor (2nd job) 168s.
_CRIT = {"long_pole_job": "test", "long_pole_p50": 439.0, "floor_p50": 168.0,
         "job_p50": {"test": 439.0, "lint": 168.0, "typecheck": 74.0}}


# A two-bound cascade (within → cross) for exercising ordering/derivation. The
# DEFAULT CASCADE is cross-only (within is applied during per-finding sizing,
# see wall_clock.CASCADE comment); we pass this explicitly where we want both.
_WITHIN_THEN_CROSS = [
    ("within-workflow", bound_within_workflow),
    ("cross-workflow", bound_cross_workflow),
]


def test_passthrough_without_timing():
    # No crit (no sampled timing) → raw estimate survives, empty derivation.
    res = size_wall_clock(200.0, WallClockContext(workflow="ci.yml", crit=None))
    assert res.effective_s == 200.0
    assert res.derivation == []


def test_within_workflow_caps_at_long_pole_headroom():
    # `test` IS the long pole; a 400s raw estimate floors at headroom 439-168=271.
    ctx = WallClockContext(workflow="ci.yml", crit=_CRIT, affected_jobs=("test",))
    assert bound_within_workflow(400.0, ctx).value == 271.0
    assert "critical-path headroom" in bound_within_workflow(400.0, ctx).reason


def test_within_workflow_zeroes_below_floor_job():
    # `lint` (168s) == floor → not the long pole → 0 wall-clock, with a reason.
    ctx = WallClockContext(workflow="ci.yml", crit=_CRIT, affected_jobs=("lint",))
    r = bound_within_workflow(50.0, ctx)
    assert r.value == 0.0 and "below the cluster floor" in r.reason


def test_default_cascade_is_cross_only():
    # within-workflow is NOT in the default cascade (applied during sizing), so
    # a long raw estimate on the long-pole job is NOT capped by the default run.
    ctx = WallClockContext(workflow="ci.yml", crit=_CRIT, affected_jobs=("test",))
    assert size_wall_clock(400.0, ctx).effective_s == 400.0  # no cross siblings


def test_cross_workflow_floor_caps_and_records_derivation():
    # mastra-like: changed-test-gate long pole 410, no within cap (single-job
    # floor 0), but e2e-docs (344) runs concurrently → saving floors at 66.
    crit = {"long_pole_job": "changed-tests", "long_pole_p50": 410.0,
            "floor_p50": 0.0, "job_p50": {"changed-tests": 410.0}}
    ctx = WallClockContext(workflow="changed-test-gate.yml", crit=crit,
                           concurrent=(("e2e-docs.yml", 344.0),),
                           affected_jobs=("changed-tests",))
    res = size_wall_clock(205.0, ctx)
    assert res.effective_s == 66.0
    names = [d[0] for d in res.derivation]
    assert names == ["cross-workflow"]
    assert "cross-workflow floor" in res.derivation[-1][3]


def test_both_caps_fire_in_order():
    # Raw 400 → within caps to 271 (439-168) → a slow sibling (e2e 300) floors
    # it further to 139 (439-300). Derivation records BOTH steps, in order.
    ctx = WallClockContext(workflow="ci.yml", crit=_CRIT,
                           concurrent=(("e2e.yml", 300.0),),
                           affected_jobs=("test",))
    res = size_wall_clock(400.0, ctx, cascade=_WITHIN_THEN_CROSS)
    assert [d[0] for d in res.derivation] == ["within-workflow", "cross-workflow"]
    assert res.derivation[0][1] == 400.0 and res.derivation[0][2] == 271.0
    assert res.derivation[1][1] == 271.0 and res.derivation[1][2] == 139.0
    assert res.effective_s == 139.0


def test_developer_facing_zeroes_non_pr_workflow():
    # A workflow that only ran on push/schedule (no PR-facing trigger) → its
    # wall-clock is post-merge time, not developer PR wait → 0, with a reason.
    ctx = WallClockContext(workflow="release.yml", crit=_CRIT,
                           affected_jobs=("test",), events=("push", "schedule"))
    res = size_wall_clock(120.0, ctx)
    assert res.effective_s == 0.0
    assert res.derivation[0][0] == "developer-facing"
    assert "non-developer-facing" in res.derivation[0][3]


def test_developer_facing_keeps_pr_workflow():
    ctx = WallClockContext(workflow="ci.yml", crit=_CRIT, affected_jobs=("test",),
                           events=("pull_request", "push"))
    assert size_wall_clock(120.0, ctx).effective_s == 120.0


def test_developer_facing_passthrough_when_events_unknown():
    # No sampled events → don't guess; pass through.
    ctx = WallClockContext(workflow="ci.yml", crit=_CRIT, affected_jobs=("test",),
                           events=())
    assert size_wall_clock(120.0, ctx).effective_s == 120.0


def test_monotonic_down_invariant_is_enforced():
    bad = [("evil", lambda v, ctx: BoundResult(v + 1.0, "grow"))]
    with pytest.raises(AssertionError, match="monotonic-down"):
        size_wall_clock(100.0, WallClockContext(), cascade=bad)


def test_no_silent_shrink_invariant_is_enforced():
    bad = [("quiet", lambda v, ctx: BoundResult(v - 1.0))]  # no reason
    with pytest.raises(AssertionError, match="without a reason"):
        size_wall_clock(100.0, WallClockContext(), cascade=bad)


def test_bounds_are_individually_callable():
    # Each bound has the uniform (value, ctx) -> BoundResult shape.
    ctx = WallClockContext(workflow="ci.yml", crit=_CRIT, affected_jobs=("test",),
                           concurrent=(("e2e.yml", 300.0),))
    assert isinstance(bound_within_workflow(400.0, ctx), BoundResult)
    assert isinstance(bound_cross_workflow(271.0, ctx), BoundResult)


# =============================================================================
# Structural sizing primitives — credit_detrigger / credit_shared_substep.
# These set the RAW estimate before the cascade; their FLOOR-to-zero behavior is
# what prevents a phantom saving, so it is pinned directly (not just transitively
# through one happy-path OPT71/OPT73 scenario).
# =============================================================================

def test_credit_detrigger_floors_at_zero_when_a_sibling_is_slower():
    # A slower concurrent check still gates the PR → removing this one saves no
    # wall-clock (only runner-minutes). Must be a MEASURED 0.0, not negative.
    assert credit_detrigger(100.0, [250.0]) == 0.0


def test_credit_detrigger_is_own_minus_slowest_other():
    assert credit_detrigger(300.0, [100.0, 250.0]) == 50.0


def test_credit_detrigger_no_others_keeps_full_value():
    # Nothing else gates the PR → the whole check is the wait.
    assert credit_detrigger(120.0, []) == 120.0


def test_credit_detrigger_nonpositive_own_is_zero():
    assert credit_detrigger(0.0, [10.0]) == 0.0
    assert credit_detrigger(-5.0, []) == 0.0


def test_credit_shared_substep_floors_wall_clock_at_zero_below_warm_floor():
    # A step already at/under its warm floor has no addressable time → 0 saving
    # on BOTH axes (this is what makes _detect_shared_substep skip a non-finding).
    credit = credit_shared_substep(step_p50_s=8.0, warm_floor_s=10.0,
                                   cluster_job_count=4)
    assert credit.wall_clock_s == 0.0
    assert credit.runner_min_s == 0.0
    assert credit.job_count == 4


def test_credit_shared_substep_sizes_both_axes():
    # per-job = 60 - 10 = 50; runner-min = 50 * 3 jobs.
    credit = credit_shared_substep(step_p50_s=60.0, warm_floor_s=10.0,
                                   cluster_job_count=3)
    assert credit.wall_clock_s == 50.0
    assert credit.runner_min_s == 150.0
    assert credit.job_count == 3


def test_credit_shared_substep_job_count_floored_at_one():
    credit = credit_shared_substep(step_p50_s=30.0, warm_floor_s=0.0,
                                   cluster_job_count=0)
    assert credit.job_count == 1


def test_shared_substep_credit_rejects_negative_fields():
    # The __post_init__ guard turns the factory's convention into a guarantee.
    with pytest.raises(ValueError):
        SharedSubstepCredit(wall_clock_s=-1.0, runner_min_s=0.0, job_count=1)
    with pytest.raises(ValueError):
        SharedSubstepCredit(wall_clock_s=0.0, runner_min_s=0.0, job_count=0)


# --- ENG-1 PR-N3: the measured-critical-path bound goes chain-aware ------------
# The deepgram defect: `compile` (a `needs:` predecessor of `test`) was floored
# by its own dependent as if concurrent, stamping 2.5s for a ~38s serial lever.

def _chain_ctx(**kw):
    base = dict(
        workflow=".github/workflows/ci.yml",
        crit={"job_p50": {"compile": 38.0, "test": 66.0}},
        pr_checks=(("compile", 38.0), ("test", 66.0), ("External Bot", 58.0)),
        chain_members=frozenset({"compile", "test"}),
        chain_p50_s=104.0,
        chain_win_s=46.0,  # chain 104 - runner-up 58 (the external peer)
    )
    base.update(kw)
    return WallClockContext(**base)


def test_chain_member_is_not_floored_by_its_own_chain():
    # compile's full 38s is a 1:1 chain lever (within the 46s chain headroom):
    # its dependent `test` must not be treated as a concurrent floor.
    ctx = _chain_ctx(own_check_names=frozenset({"compile"}))
    res = bound_measured_critical_path(38.0, ctx)
    assert res.value == 38.0, res.reason
    assert res.reason is None or "chain" in res.reason


def test_chain_member_lever_caps_at_the_chain_headroom():
    # A member raw saving beyond the whole-chain headroom caps there — past it
    # the next-longest competing path gates instead.
    ctx = _chain_ctx(own_check_names=frozenset({"test"}))
    res = bound_measured_critical_path(66.0, ctx)
    assert res.value == 46.0
    assert res.reason and "chain" in res.reason


def test_non_member_is_floored_by_the_chain_sum_not_its_slowest_member():
    # The external peer (58s) is slower than nothing individually — but the
    # CHAIN (104s) gates the merge; shortening the peer buys no wall-clock.
    ctx = _chain_ctx(own_check_names=frozenset({"External Bot"}))
    res = bound_measured_critical_path(58.0, ctx)
    assert res.value == 0.0
    assert res.reason and "chain" in res.reason


def test_mixed_member_and_non_member_caps_at_the_member_own_span():
    # The mixed cell (found by N3's adversarial pass; semantics corrected on
    # real data): a finding spanning a chain member AND a non-member keeps the
    # member path — its credit is deliverable via the member alone — but the
    # credit is bounded by the member's OWN span: `compile` (38s) cannot give
    # back the finding's full 58s, so min(58, win 46, span 38) = 38. Routing
    # mixed sets to the general floor instead was rejected: the synthetic
    # chain gate contains the member's own span, so it self-floors real
    # member levers to 0 (it zeroed deepgram's OPT73 — the measured
    # narrative's genuine cross-leg lever).
    ctx = _chain_ctx(own_check_names=frozenset({"compile", "External Bot"}))
    res = bound_measured_critical_path(58.0, ctx)
    assert res.value == 38.0, res.reason
    assert res.reason and "member's own span" in res.reason


def test_shared_step_cluster_spanning_member_and_siblings_keeps_its_chain_win():
    # The OPT73 shape from the deepgram acceptance repo: a shared step across
    # all matrix legs, one of which is the chain member. The member portion
    # delivers the win 1:1 up to the chain headroom — the cluster must NOT be
    # zeroed against the chain it belongs to.
    ctx = _chain_ctx(own_check_names=frozenset(
        {"test", "test (3.10)", "External Bot"}))
    res = bound_measured_critical_path(19.0, ctx)
    # min(value 19, chain win 46, member span 66) = 19 — full value.
    assert res.value == 19.0, res.reason


def test_mixed_set_with_member_not_on_the_gate_stays_on_the_member_path():
    # An own check that is NOT observed on the sampled gate (not in pr_checks)
    # must not demote the finding off the member path.
    ctx = _chain_ctx(own_check_names=frozenset({"compile", "never-sampled-job"}))
    res = bound_measured_critical_path(38.0, ctx)
    assert res.value == 38.0, res.reason


def _mastodon_cluster_ctx(cluster_floor_lever: bool) -> WallClockContext:
    # #44 mastodon: the `Run bin/flatware rspec` step recurs across 3 concurrent
    # matrix legs. The modal chain's rspec node is a member; its runner-up is a
    # SIBLING leg (rspec (2)) whose gap is chain_win_s = 40.5s. The true non-sibling
    # floor is Elastic Search (202s), so the ceiling should be 841-202 = 639s.
    return WallClockContext(
        workflow=".github/workflows/test.yml",
        crit={"job_p50": {"rspec (1)": 841.0, "rspec (2)": 800.0,
                          "rspec (3)": 780.0, "Elastic Search": 202.0}},
        pr_checks=(("rspec (1)", 841.0), ("rspec (2)", 800.0),
                   ("rspec (3)", 780.0), ("Elastic Search", 202.0)),
        own_check_names=frozenset({"rspec (1)", "rspec (2)", "rspec (3)"}),
        chain_members=frozenset({"rspec (1)"}),
        chain_p50_s=841.0,
        chain_win_s=40.5,   # the gap to the sibling rspec (2) runner-up
        cluster_floor_lever=cluster_floor_lever)


def test_cluster_floor_lever_escapes_sibling_chain_win_cap():
    # The ~15x mastodon understatement: WITHOUT the cluster flag the shared-step
    # lever is capped at chain_win_s (40.5s) by a sibling leg. WITH it, the sibling
    # legs are all in `own` (they descend together), so the ceiling floors at the
    # slowest NON-sibling check (Elastic Search 202s) → 841-202 = 639s.
    off = bound_measured_critical_path(639.0, _mastodon_cluster_ctx(False))
    assert off.value == 40.5, "pre-fix: a sibling leg caps the cluster lever ~15x under"
    assert off.reason and "chain" in off.reason
    on = bound_measured_critical_path(639.0, _mastodon_cluster_ctx(True))
    assert on.value == 639.0, on.reason   # floors at the non-sibling Elastic Search


def test_cluster_floor_lever_still_floors_at_a_slower_non_sibling():
    # The cluster flag does not hand out free wall-clock: a NON-sibling check
    # slower than the cluster still floors it. Raise Elastic Search above the
    # cluster's own_max — the lever floors to 0 (bill-only).
    ctx = _mastodon_cluster_ctx(True)
    ctx = WallClockContext(
        workflow=ctx.workflow, crit=ctx.crit,
        pr_checks=(("rspec (1)", 841.0), ("rspec (2)", 800.0),
                   ("rspec (3)", 780.0), ("Elastic Search", 900.0)),
        own_check_names=ctx.own_check_names, chain_members=ctx.chain_members,
        chain_p50_s=ctx.chain_p50_s, chain_win_s=ctx.chain_win_s,
        cluster_floor_lever=True)
    res = bound_measured_critical_path(639.0, ctx)
    assert res.value == 0.0, "a slower non-sibling concurrent check still gates"


def test_no_chain_data_keeps_the_classic_floor():
    # Defaults (no chain fields) — behavior identical to the pre-N3 cascade:
    # compile floored by the "concurrent" test (the known-wrong legacy model,
    # kept byte-stable for chainless/legacy artifacts).
    ctx = WallClockContext(
        workflow=".github/workflows/ci.yml",
        crit={"job_p50": {"compile": 38.0, "test": 66.0}},
        pr_checks=(("compile", 38.0), ("test", 66.0)),
        own_check_names=frozenset({"compile"}),
    )
    res = bound_measured_critical_path(38.0, ctx)
    assert res.value == 0.0, "legacy behavior must not change without chain data"


def test_single_population_cap_names_the_targets_own_span_not_a_blocker():
    ctx = WallClockContext(
        pr_checks=(("only", 60.0),),
        pr_check_populations=((1.0, (("only", 60.0),)),),
        pr_check_populations_complete=(True,),
        own_check_names=frozenset({"only"}),
    )

    result = bound_measured_critical_path(100.0, ctx)

    assert result.value == 60.0
    assert result.reason is not None
    assert "this check's observed span (60s)" in result.reason
    assert "`None`" not in result.reason

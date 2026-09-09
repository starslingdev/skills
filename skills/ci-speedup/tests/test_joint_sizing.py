"""Joint scenarios for supported parallel checks (SPEC-2026-09-08 §6).

These tests pin the *numbers*, not the vocabulary. Every supported case asserts
the exact seconds the calculator must produce; every unsupported case asserts
the exact rejection code. A test that only matched prose would pass with the
numbers swapped, which is precisely the failure this suite exists to prevent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import joint_scenario as js  # noqa: E402


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------

BASIS = "era=e1;runner=ubuntu-latest;population=pr"


def obs(observation_id, durations, *, basis=BASIS, validated=True, residual=0.0):
    return js.GatingObservation(
        observation_id=observation_id,
        basis=basis,
        check_durations=dict(durations),
        concurrency_validated=validated,
        scheduling_residual_s=residual,
    )


def gating(observations, *, topology="independent_concurrent", basis=BASIS,
           check_names=None):
    if check_names is None:
        check_names = set()
        for o in observations:
            check_names |= set(o.check_durations)
    return js.GatingSet(
        topology=topology,
        check_names=frozenset(check_names),
        basis=basis,
        observations=tuple(observations),
    )


def effect(finding_id, work_id, per_obs, *, basis=BASIS, local=True,
           matrix_resolved=True, reduction_basis="step_p50_measured",
           assumption="modeled: the named step is removed from the job"):
    """per_obs: {observation_id: {check_name: (reduction_s, affected_work_s)}}"""
    rows = []
    for oid, checks in per_obs.items():
        for check, (red, work) in checks.items():
            rows.append(js.EffectObservation(
                observation_id=oid, check_name=check,
                reduction_s=red, affected_work_s=work))
    return js.ScenarioEffect(
        finding_id=finding_id,
        workflow=".github/workflows/ci.yml",
        work_id=work_id,
        basis=basis,
        assumption=assumption,
        evidence_refs=("run/1#step/3",),
        local_runtime_only=local,
        matrix_identity_resolved=matrix_resolved,
        reduction_basis=reduction_basis,
        observations=tuple(rows),
    )


def constant_effect(finding_id, work_id, check, reduction, work, oids,
                    **kw):
    return effect(finding_id, work_id,
                  {oid: {check: (reduction, work)} for oid in oids}, **kw)


# --------------------------------------------------------------------------
# §6 headline counterexample: A=300 / B=299, local reductions 100 each
# --------------------------------------------------------------------------

def _ab_gating(extra=None, n=3):
    rows = []
    for i in range(n):
        d = {"A": 300.0, "B": 299.0}
        if extra:
            d.update(extra)
        rows.append(obs(f"r{i}", d))
    return gating(rows)


def test_ab_pair_individual_1_and_0_joint_100():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    ea = constant_effect("F-A", "A::pytest", "A", 100.0, 250.0, oids)
    eb = constant_effect("F-B", "B::npm-test", "B", 100.0, 250.0, oids)

    res = js.evaluate_scenario(g, [ea, eb])
    assert res.supported is True, res.rejection
    assert res.median_t_before_s == 300.0
    assert res.median_t_after_s == 200.0
    assert res.median_delta_s == 100.0
    assert res.individual_delta_s == {"F-A": 1.0, "F-B": 0.0}
    assert res.modeled_check_after_s == {"A": 200.0, "B": 199.0}


def test_capped_stamps_cannot_drive_the_joint_result():
    """The existing capped stamps for this shape are A=1s and B=0s. Feeding
    those as local reductions yields joint 1s, not 100s — so the contract must
    refuse a capped stamp as a reduction basis rather than silently model it."""
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    capped_a = constant_effect("F-A", "A::pytest", "A", 1.0, 250.0, oids)
    capped_b = constant_effect("F-B", "B::npm-test", "B", 0.0, 250.0, oids)

    numeric = js.evaluate_scenario(g, [capped_a, capped_b])
    assert numeric.supported is True
    assert numeric.median_delta_s == 1.0  # the wrong answer, arithmetically

    declared = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::pytest", "A", 1.0, 250.0, oids,
                        reduction_basis="wall_clock_p50_s"),
        capped_b,
    ])
    assert declared.supported is False
    assert declared.rejection.code == "capped_stamp_not_a_local_reduction"

    uncapped = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::pytest", "A", 100.0, 250.0, oids,
                        reduction_basis="wall_clock_uncapped_p50_s"),
        capped_b,
    ])
    assert uncapped.supported is False
    assert uncapped.rejection.code == "capped_stamp_not_a_local_reduction"


def test_unaffected_competitor_at_295_caps_the_joint_saving_at_5():
    g = _ab_gating(extra={"C": 295.0})
    oids = [o.observation_id for o in g.observations]
    ea = constant_effect("F-A", "A::pytest", "A", 100.0, 250.0, oids)
    eb = constant_effect("F-B", "B::npm-test", "B", 100.0, 250.0, oids)

    res = js.evaluate_scenario(g, [ea, eb])
    assert res.supported is True
    assert res.median_t_before_s == 300.0
    assert res.median_t_after_s == 295.0
    assert res.median_delta_s == 5.0
    assert res.individual_delta_s == {"F-A": 1.0, "F-B": 0.0}


def test_unaffected_competitor_at_300_makes_the_joint_saving_zero():
    g = _ab_gating(extra={"C": 300.0})
    oids = [o.observation_id for o in g.observations]
    ea = constant_effect("F-A", "A::pytest", "A", 100.0, 250.0, oids)
    eb = constant_effect("F-B", "B::npm-test", "B", 100.0, 250.0, oids)

    res = js.evaluate_scenario(g, [ea, eb])
    assert res.supported is True
    assert res.median_t_before_s == 300.0
    assert res.median_t_after_s == 300.0
    assert res.median_delta_s == 0.0
    # zero savings is an honest, supported answer — it just earns no block
    assert js.select_joint_block(g, [ea, eb]) is None


def test_zero_joint_saving_is_not_a_rejection():
    g = _ab_gating(extra={"C": 300.0})
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::pytest", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::npm-test", "B", 100.0, 250.0, oids)])
    assert res.rejection is None


# --------------------------------------------------------------------------
# median(max) is not max(median)
# --------------------------------------------------------------------------

def test_median_of_max_differs_from_max_of_median():
    g = gating([
        obs("r0", {"A": 300.0, "B": 100.0}),
        obs("r1", {"A": 100.0, "B": 300.0}),
        obs("r2", {"A": 300.0, "B": 100.0}),
        obs("r3", {"A": 100.0, "B": 300.0}),
    ])
    # per-check medians are both 200s, so max(median) would say 200s
    assert js.median([300.0, 100.0, 300.0, 100.0]) == 200.0
    ea = constant_effect("F-A", "A::s", "A", 50.0, 90.0, ["r0", "r1", "r2", "r3"])
    eb = constant_effect("F-B", "B::s", "B", 50.0, 90.0, ["r0", "r1", "r2", "r3"])
    res = js.evaluate_scenario(g, [ea, eb])
    assert res.supported is True
    # median(max_j d_before) == 300, not 200
    assert res.median_t_before_s == 300.0
    assert res.median_t_after_s == 250.0
    assert res.median_delta_s == 50.0


def test_medians_are_reported_separately_and_need_not_subtract():
    """T_before/T_after/delta are three independent medians. This fixture makes
    median(T_before) - median(T_after) != median(delta) so a renderer that
    subtracts the first two cannot fake the third."""
    g = gating([
        obs("r0", {"A": 100.0, "B": 10.0}),
        obs("r1", {"A": 200.0, "B": 10.0}),
        obs("r2", {"A": 300.0, "B": 10.0}),
    ])
    ea = js.ScenarioEffect(
        finding_id="F-A", workflow=".github/workflows/ci.yml", work_id="A::s",
        basis=BASIS, assumption="modeled", evidence_refs=("e",),
        local_runtime_only=True, matrix_identity_resolved=True,
        reduction_basis="step_p50_measured",
        observations=(
            js.EffectObservation("r0", "A", 90.0, 90.0),
            js.EffectObservation("r1", "A", 10.0, 90.0),
            js.EffectObservation("r2", "A", 20.0, 90.0),
        ))
    eb = constant_effect("F-B", "B::s", "B", 0.0, 5.0, ["r0", "r1", "r2"])
    res = js.evaluate_scenario(g, [ea, eb])
    assert res.supported is True
    assert res.median_t_before_s == 200.0
    assert res.median_t_after_s == 190.0   # after = 10, 190, 280
    assert res.median_delta_s == 20.0      # deltas = 90, 10, 20
    assert res.median_t_before_s - res.median_t_after_s != res.median_delta_s


# --------------------------------------------------------------------------
# matrix legs
# --------------------------------------------------------------------------

def test_matrix_effect_covering_all_legs_versus_one_leg():
    rows = [obs(f"r{i}", {"T (a)": 300.0, "T (b)": 300.0, "T (c)": 300.0,
                          "Lint": 50.0}) for i in range(3)]
    g = gating(rows)
    oids = [o.observation_id for o in g.observations]
    one_leg = effect("F-1", "T::pytest",
                     {oid: {"T (a)": (100.0, 250.0)} for oid in oids})
    lint = constant_effect("F-L", "Lint::eslint", "Lint", 10.0, 40.0, oids)

    res_one = js.evaluate_scenario(g, [one_leg, lint])
    assert res_one.supported is True
    # unaffected sibling legs remain floors at 300s
    assert res_one.median_t_after_s == 300.0
    assert res_one.median_delta_s == 0.0

    all_legs = effect("F-1", "T::pytest",
                      {oid: {"T (a)": (100.0, 250.0),
                             "T (b)": (100.0, 250.0),
                             "T (c)": (100.0, 250.0)} for oid in oids})
    res_all = js.evaluate_scenario(g, [all_legs, lint])
    assert res_all.supported is True
    assert res_all.median_t_after_s == 200.0
    assert res_all.median_delta_s == 100.0


def test_unknown_matrix_mapping_is_rejected():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    ea = constant_effect("F-A", "A::pytest", "A", 100.0, 250.0, oids,
                         matrix_resolved=False)
    eb = constant_effect("F-B", "B::npm", "B", 100.0, 250.0, oids)
    res = js.evaluate_scenario(g, [ea, eb])
    assert res.supported is False
    assert res.rejection.code == "ambiguous_matrix_identity"


def test_effect_naming_a_check_outside_the_gating_set_is_rejected():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    off = constant_effect("F-X", "X::s", "X", 100.0, 250.0, oids)
    eb = constant_effect("F-B", "B::npm", "B", 100.0, 250.0, oids)
    res = js.evaluate_scenario(g, [off, eb])
    assert res.supported is False
    assert res.rejection.code == "off_gating_set_check"


# --------------------------------------------------------------------------
# overlap / compatibility
# --------------------------------------------------------------------------

def test_two_findings_touching_the_same_step_are_unsupported():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    e1 = constant_effect("F-1", "A::pytest", "A", 60.0, 250.0, oids)
    e2 = constant_effect("F-2", "A::pytest", "A", 80.0, 250.0, oids)
    res = js.evaluate_scenario(g, [e1, e2])
    assert res.supported is False
    assert res.rejection.code == "overlapping_affected_work"
    assert res.median_delta_s is None


def test_two_findings_on_different_steps_of_the_same_job_compose():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    e1 = constant_effect("F-1", "A::install", "A", 60.0, 70.0, oids)
    e2 = constant_effect("F-2", "A::pytest", "A", 41.0, 180.0, oids)
    res = js.evaluate_scenario(g, [e1, e2])
    assert res.supported is True
    assert res.median_t_after_s == 299.0   # A -> 199, B stays 299
    assert res.median_delta_s == 1.0


# --------------------------------------------------------------------------
# topology rejections
# --------------------------------------------------------------------------

@pytest.mark.parametrize("topology,code", [
    ("needs_chain", "unsupported_topology"),
    ("required_aggregator", "unsupported_topology"),
    ("shared_serial_upstream", "unsupported_topology"),
    ("unknown", "unsupported_topology"),
])
def test_non_independent_topologies_are_unsupported(topology, code):
    rows = [obs(f"r{i}", {"A": 300.0, "B": 299.0}) for i in range(3)]
    g = gating(rows, topology=topology)
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == code
    assert res.median_delta_s is None


def test_relocation_and_other_non_runtime_effects_are_ineligible():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids, local=False),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "not_local_runtime_only"


# --------------------------------------------------------------------------
# observation-level rejections
# --------------------------------------------------------------------------

def test_missing_competitor_in_one_observation_is_rejected():
    g = gating([
        obs("r0", {"A": 300.0, "B": 299.0}),
        obs("r1", {"A": 300.0}),          # B did not run / was not sampled
        obs("r2", {"A": 300.0, "B": 299.0}),
    ], check_names={"A", "B"})
    oids = ["r0", "r1", "r2"]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "missing_competitor"


def test_unvalidated_concurrency_is_rejected():
    rows = [obs("r0", {"A": 300.0, "B": 299.0}, validated=False),
            obs("r1", {"A": 300.0, "B": 299.0}),
            obs("r2", {"A": 300.0, "B": 299.0})]
    g = gating(rows)
    oids = ["r0", "r1", "r2"]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "concurrency_unvalidated"


def test_scheduling_residual_within_tolerance_is_kept_and_disclosed():
    rows = [obs(f"r{i}", {"A": 300.0, "B": 299.0},
                residual=js.CONCURRENCY_TOLERANCE_S) for i in range(3)]
    g = gating(rows)
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is True
    assert res.median_delta_s == 100.0
    assert res.tolerated_residual_s == js.CONCURRENCY_TOLERANCE_S


def test_scheduling_residual_beyond_tolerance_is_rejected():
    rows = [obs(f"r{i}", {"A": 300.0, "B": 299.0},
                residual=js.CONCURRENCY_TOLERANCE_S + 0.5) for i in range(3)]
    g = gating(rows)
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "scheduling_residual_exceeds_tolerance"


def test_stale_era_observation_is_rejected():
    rows = [obs("r0", {"A": 300.0, "B": 299.0}),
            obs("r1", {"A": 300.0, "B": 299.0}, basis="era=e0;runner=x;population=pr"),
            obs("r2", {"A": 300.0, "B": 299.0})]
    g = gating(rows)
    oids = ["r0", "r1", "r2"]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "basis_mismatch"


def test_effect_on_a_different_basis_is_rejected():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids,
                        basis="era=e0;runner=x;population=pr"),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "basis_mismatch"


def test_effect_missing_an_observation_is_insufficient_evidence():
    g = _ab_gating()
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, ["r0", "r1"]),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, ["r0", "r1", "r2"])])
    assert res.supported is False
    assert res.rejection.code == "insufficient_effect_evidence"


def test_no_observations_at_all_is_rejected():
    g = gating([], check_names={"A", "B"})
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, []),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, [])])
    assert res.supported is False
    assert res.rejection.code == "no_matched_observations"


# --------------------------------------------------------------------------
# numeric rejections
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_duration_is_rejected(bad):
    rows = [obs("r0", {"A": bad, "B": 299.0}),
            obs("r1", {"A": 300.0, "B": 299.0}),
            obs("r2", {"A": 300.0, "B": 299.0})]
    g = gating(rows)
    oids = ["r0", "r1", "r2"]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "non_finite_input"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_reduction_is_rejected(bad):
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", bad, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "non_finite_input"


def test_negative_duration_is_rejected():
    rows = [obs("r0", {"A": -1.0, "B": 299.0})] + \
           [obs(f"r{i}", {"A": 300.0, "B": 299.0}) for i in (1, 2)]
    g = gating(rows)
    oids = ["r0", "r1", "r2"]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "negative_input"


def test_negative_reduction_is_rejected():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", -5.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "negative_input"


def test_reduction_exceeding_affected_work_is_rejected():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 90.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "reduction_exceeds_affected_work"


def test_affected_work_exceeding_the_observed_duration_is_rejected():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 400.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "affected_work_exceeds_duration"


def test_negative_post_fix_duration_is_rejected():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    # two disjoint steps whose combined reduction exceeds the job duration
    res = js.evaluate_scenario(g, [
        constant_effect("F-1", "A::install", "A", 200.0, 200.0, oids),
        constant_effect("F-2", "A::pytest", "A", 150.0, 150.0, oids)])
    assert res.supported is False
    assert res.rejection.code == "negative_post_fix_duration"


# --------------------------------------------------------------------------
# admission: off-spine vs below-floor
# --------------------------------------------------------------------------

def test_below_floor_required_check_may_enter_a_pair():
    """B never gates on its own (0s individual) but is a gating competitor, so
    it is a legitimate half of a joint scenario."""
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    res = js.evaluate_scenario(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)])
    assert res.individual_delta_s["F-B"] == 0.0
    assert res.supported is True
    assert res.median_delta_s == 100.0


def test_off_spine_finding_cannot_enter_a_pair():
    """A check that is not part of the measured gating set is off the spine;
    it has no competitor semantics and must not join a numeric scenario."""
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    pairs = js.eligible_pairs(g, [
        constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids),
        constant_effect("F-OFF", "Nightly::s", "Nightly", 100.0, 250.0, oids)])
    assert pairs == []


# --------------------------------------------------------------------------
# selection rule
# --------------------------------------------------------------------------

def test_block_renders_only_when_joint_beats_both_individuals():
    g = _ab_gating()
    oids = [o.observation_id for o in g.observations]
    ea = constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids)
    eb = constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)
    chosen = js.select_joint_block(g, [ea, eb])
    assert chosen is not None
    assert chosen.finding_ids == ("F-A", "F-B")
    assert chosen.median_delta_s == 100.0

    # a pair whose joint equals the better individual earns no block
    g2 = gating([obs(f"r{i}", {"A": 300.0, "B": 100.0}) for i in range(3)])
    only_a = constant_effect("F-A", "A::s", "A", 50.0, 250.0, ["r0", "r1", "r2"])
    tiny_b = constant_effect("F-B", "B::s", "B", 10.0, 90.0, ["r0", "r1", "r2"])
    assert js.evaluate_scenario(g2, [only_a, tiny_b]).median_delta_s == 50.0
    assert js.evaluate_scenario(g2, [only_a]).median_delta_s == 50.0
    assert js.select_joint_block(g2, [only_a, tiny_b]) is None


def test_pairs_rank_by_joint_reduction_then_by_stable_finding_ids():
    g = gating([obs(f"r{i}", {"A": 300.0, "B": 299.0, "C": 298.0})
                for i in range(3)])
    oids = [o.observation_id for o in g.observations]
    ea = constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids)
    eb = constant_effect("F-B", "B::s", "B", 100.0, 250.0, oids)
    ec = constant_effect("F-C", "C::s", "C", 100.0, 250.0, oids)
    ranked = js.eligible_pairs(g, [ec, eb, ea])
    # every pair leaves the third check as a floor, so all three tie at 2s/1s;
    # A+B leaves C=298 (2s), A+C leaves B=299 (1s), B+C leaves A=300 (0s)
    assert [r.finding_ids for r in ranked] == [("F-A", "F-B"), ("F-A", "F-C")]
    assert ranked[0].median_delta_s == 2.0
    assert ranked[1].median_delta_s == 1.0


def test_selection_uses_displayed_precision():
    """Joint 100.4 vs individual 100.0 does not clear the bar at whole seconds."""
    g = gating([obs(f"r{i}", {"A": 300.0, "B": 200.0}) for i in range(3)])
    oids = [o.observation_id for o in g.observations]
    ea = constant_effect("F-A", "A::s", "A", 100.0, 250.0, oids)
    eb = constant_effect("F-B", "B::s", "B", 0.4, 90.0, oids)
    joint = js.evaluate_scenario(g, [ea, eb])
    assert joint.median_delta_s == pytest.approx(100.0)
    assert js.select_joint_block(g, [ea, eb]) is None


# --------------------------------------------------------------------------
# artifact contract: what a producer would have to stamp
# --------------------------------------------------------------------------

def test_todays_artifact_shape_reports_the_inputs_as_absent():
    """No producer stamps the contract today. Reading a current findings doc
    must say so explicitly rather than fabricating a scenario from the
    aggregate stamps that happen to be present."""
    doc = {
        "findings": [{"pattern": "OPT73", "affected_jobs": ["A", "B"],
                      "wall_clock_p50_s": 1.0,
                      "wall_clock_uncapped_p50_s": 100.0,
                      "cluster_floor_lever": True}],
        "pr_critical_path": {"populations": [[0.5, [["A", 300.0], ["B", 299.0]]]]},
        "per_workflow_timing": {".github/workflows/ci.yml":
                                {"job_p50": {"A": 300.0, "B": 299.0}}},
    }
    g, effects, rejection = js.load_inputs(doc)
    assert g is None
    assert effects == ()
    assert rejection.code == "contract_inputs_absent"


def test_a_stamped_contract_round_trips_to_the_same_numbers():
    doc = {
        js.JOINT_SCENARIO_INPUTS_KEY: {
            "contract_version": js.SCENARIO_CONTRACT_VERSION,
            "topology": "independent_concurrent",
            "basis": BASIS,
            "check_names": ["A", "B"],
            "observations": [
                {"observation_id": f"r{i}", "basis": BASIS,
                 "check_durations": {"A": 300.0, "B": 299.0},
                 "concurrency_validated": True,
                 "scheduling_residual_s": 0.0} for i in range(3)],
            "effects": [
                {"finding_id": "F-A", "workflow": ".github/workflows/ci.yml",
                 "work_id": "A::pytest", "basis": BASIS,
                 "assumption": "modeled", "evidence_refs": ["e1"],
                 "local_runtime_only": True, "matrix_identity_resolved": True,
                 "reduction_basis": "step_p50_measured",
                 "observations": [
                     {"observation_id": f"r{i}", "check_name": "A",
                      "reduction_s": 100.0, "affected_work_s": 250.0}
                     for i in range(3)]},
                {"finding_id": "F-B", "workflow": ".github/workflows/ci.yml",
                 "work_id": "B::npm", "basis": BASIS,
                 "assumption": "modeled", "evidence_refs": ["e2"],
                 "local_runtime_only": True, "matrix_identity_resolved": True,
                 "reduction_basis": "step_p50_measured",
                 "observations": [
                     {"observation_id": f"r{i}", "check_name": "B",
                      "reduction_s": 100.0, "affected_work_s": 250.0}
                     for i in range(3)]},
            ],
        }
    }
    g, effects, rejection = js.load_inputs(doc)
    assert rejection is None
    res = js.evaluate_scenario(g, effects)
    assert res.median_delta_s == 100.0
    assert res.median_t_before_s == 300.0
    assert res.median_t_after_s == 200.0


def test_a_future_contract_version_is_not_interpreted():
    doc = {js.JOINT_SCENARIO_INPUTS_KEY: {
        "contract_version": js.SCENARIO_CONTRACT_VERSION + 1,
        "topology": "independent_concurrent", "basis": BASIS,
        "check_names": ["A"], "observations": [], "effects": []}}
    g, effects, rejection = js.load_inputs(doc)
    assert g is None
    assert rejection.code == "unsupported_contract_version"


def test_the_missing_producer_evidence_is_named_in_the_module():
    """The stop condition is part of the contract: name the fields a producer
    would have to add, so a later adapter has a target instead of a guess."""
    missing = js.MISSING_PRODUCER_EVIDENCE
    assert isinstance(missing, tuple) and len(missing) >= 5
    assert all(isinstance(m, str) and m.strip() for m in missing)

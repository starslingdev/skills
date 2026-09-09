"""Joint scenarios for supported parallel checks — the data contract and the
pure calculator.

The methodology this implements is
`references/wall-clock-methodology.md` §8.

WHY THIS MODULE EXISTS
----------------------
Two findings on two *different* concurrent checks can each be worth ~nothing on
their own and a great deal together: if A takes 300s and B takes 299s, cutting
100s off A moves the merge gate by 1s and cutting 100s off B moves it by 0s, but
doing both moves it by 100s. The report has no way to say that today.

WHY IT DOES NOT READ THE EXISTING SAVINGS STAMPS
------------------------------------------------
`wall_clock_p50_s` is NOT a post-fix duration. It is an *effective merge-wait
saving* that has already been through the cross-cutting bound cascade in
`wall_clock.py` — developer-facing gate, measured population-weighted
critical-path floor, cross-workflow floor — so for the shape above it stamps
A=1s and B=0s. Subtracting those from the observed durations yields post-fix
durations of 299s/299s and a joint saving of 1s: the wrong answer by two orders
of magnitude. `wall_clock_uncapped_p50_s` is not a substitute either — it is a
single workflow-level scalar that does not describe each affected job, does not
decompose across matrix legs, and is absent entirely when no bound fired.

So this module refuses to guess. It consumes an explicit, producer-stamped
contract of matched observations and per-observation local effects, validates
every admission condition, and either returns exact numbers or a named
rejection. `MISSING_PRODUCER_EVIDENCE` records what a producer would have to
stamp for that contract to be satisfiable; nothing in the engine stamps it yet.

SCOPE
-----
V1 supports independent, concurrent checks with a fixed measured gating set. It
is NOT a general DAG scheduler: a `needs:` chain, a required aggregator, a
shared serial upstream, an unresolved competitor, a conditional check
population, an ambiguous matrix identity, or insufficient per-job effect
evidence all yield an *unsupported* verdict. Unsupported is not zero, and a
zero saving is not unsupported — the two are different answers and this module
keeps them apart.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------
# contract constants
# --------------------------------------------------------------------------

SCENARIO_CONTRACT_VERSION = 1

#: Top-level findings-artifact key a producer would stamp to feed this module.
JOINT_SCENARIO_INPUTS_KEY = "joint_scenario_inputs"

#: The only gating topology v1 can model.
SUPPORTED_TOPOLOGY = "independent_concurrent"

#: Seconds of start/finish skew tolerated when an observation claims its checks
#: ran concurrently. A residual inside this bound is kept and DISCLOSED; the
#: runtime-only model does not attempt to credit or debit it. Beyond it the
#: observation is rejected rather than modelled with an unmodelled queue effect.
CONCURRENCY_TOLERANCE_S = 5.0

#: The report renders whole seconds; scenario selection compares at that
#: precision so a block is never crowned on a difference the reader cannot see.
DISPLAY_DECIMALS = 0

#: Fields that are already-capped or already-aggregated savings stamps. None of
#: them is a per-observation local duration reduction, so none may be declared
#: as an effect's `reduction_basis`.
CAPPED_STAMP_FIELDS = frozenset({
    "wall_clock_p50_s",
    "wall_clock_uncapped_p50_s",
    "wall_clock_derivation",
    "cluster_floor_lever",
    "runner_min_saving",
    "chain_win_s",
})

#: What no current producer stamps. This is the stop condition for the
#: workstream, kept in code so a later adapter has a target rather than a guess.
MISSING_PRODUCER_EVIDENCE = (
    "per-observation gating durations for the WHOLE gating set: "
    "`pr_critical_path.chain_facts` is the closest artifact and it does carry "
    "per-sha, era-scoped `member_spans_s`, but only for the members of that "
    "PR's winning chain, span-capped per check — never every competitor, and "
    "with no attempt or runner identity. `populations` is bimodal-gated and "
    "identity-free, and raw per-run job durations survive only as a local in "
    "the collector, which returns aggregated job_p50/job_p95",
    "stamped concurrency validation: overlap is inferred structurally from the "
    "`needs:` closure and DEFAULTS TO CONCURRENT when no job graph is "
    "available; per-check start/finish intervals exist in memory but are "
    "reduced to a single makespan scalar and never persisted",
    "per-observation local duration reductions: raw pre-cascade estimates are "
    "one scalar per finding (and one scalar across ALL legs for a cluster "
    "finding), never a value per matched observation bounded by that "
    "observation's affected work",
    "affected step/work identity: findings carry no `affected_steps` key. A "
    "cluster finding does stamp `measured_evidence.waterfall.shared_step` (the "
    "one step its fix changes) and every other finding reaches step identity "
    "only as `decomposition.dominant_step` — one dominant step, which cannot "
    "decide whether two findings touch disjoint work",
    "stable matrix-leg identity: `affected_jobs` holds YAML job keys on the "
    "scan path and GitHub display names on the measured path (`wall_clock.py` "
    "bridges the two), and a job key names ALL of that job's legs at once, so "
    "nothing maps a finding to the exact legs it changes",
    "a local-runtime-only certificate: nothing on a finding asserts that its "
    "fix leaves scheduling, coverage and the job set unchanged, which is the "
    "precondition for composing two effects at all",
)


# --------------------------------------------------------------------------
# data contract
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GatingObservation:
    """One matched observation (run/attempt) of the WHOLE gating set."""

    observation_id: str
    basis: str
    check_durations: Mapping[str, float]
    concurrency_validated: bool = False
    scheduling_residual_s: float = 0.0


@dataclass(frozen=True)
class EffectObservation:
    """One finding's modeled local reduction on one check in one observation."""

    observation_id: str
    check_name: str
    reduction_s: float
    #: The duration of the work this finding actually changes in this
    #: observation. The reduction may not exceed it.
    affected_work_s: float


@dataclass(frozen=True)
class ScenarioEffect:
    """One finding's complete, explicit scenario effect."""

    #: Unique within a scenario: solo reductions are keyed by it, so a blank or
    #: repeated id would change which comparison the admission rule makes.
    finding_id: str
    #: Required. Gating durations are keyed by bare check name, so two
    #: workflows claiming one name are refused rather than conflated.
    workflow: str
    #: Affected step/work identity, required. Two effects sharing a
    #: (check, work_id) pair are overlapping alternatives and are rejected,
    #: never summed; blank work is not disjoint work and is refused too.
    work_id: str
    basis: str
    #: The assumption that generated the reductions, carried into the report.
    assumption: str
    evidence_refs: tuple[str, ...]
    #: True only for a local runtime change with unchanged scheduling, coverage
    #: and job set. Relocation, new sharding, cancellation and trigger changes
    #: are ineligible in v1.
    local_runtime_only: bool
    matrix_identity_resolved: bool
    #: Where the reductions came from. May not name a capped savings stamp.
    reduction_basis: str
    observations: tuple[EffectObservation, ...]

    @property
    def check_names(self) -> frozenset[str]:
        return frozenset(o.check_name for o in self.observations)


@dataclass(frozen=True)
class GatingSet:
    """The fixed measured gating set and its matched observations."""

    topology: str
    check_names: frozenset[str]
    basis: str
    observations: tuple[GatingObservation, ...]


@dataclass(frozen=True)
class ScenarioRejection:
    code: str
    detail: str


@dataclass(frozen=True)
class ScenarioResult:
    supported: bool
    rejection: ScenarioRejection | None = None
    finding_ids: tuple[str, ...] = ()
    n_observations: int = 0
    basis: str = ""
    median_t_before_s: float | None = None
    median_t_after_s: float | None = None
    median_delta_s: float | None = None
    individual_delta_s: Mapping[str, float] = field(default_factory=dict)
    modeled_check_after_s: Mapping[str, float] = field(default_factory=dict)
    tolerated_residual_s: float = 0.0
    assumptions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    contract_version: int = SCENARIO_CONTRACT_VERSION


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def median(values: Iterable[float]) -> float:
    """Plain median. Exported so callers and tests share one definition and
    nobody re-derives a 'median' that is really a mean or a p50 of a p50."""
    return float(statistics.median(list(values)))


def _display(value: float) -> float:
    return round(float(value), DISPLAY_DECIMALS)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def _reject(code: str, detail: str) -> ScenarioResult:
    return ScenarioResult(supported=False,
                          rejection=ScenarioRejection(code, detail))


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _validate_gating(g: GatingSet) -> ScenarioRejection | None:
    if g.topology != SUPPORTED_TOPOLOGY:
        return ScenarioRejection(
            "unsupported_topology",
            f"gating topology {g.topology!r} is not {SUPPORTED_TOPOLOGY!r}; "
            "a needs: chain, required aggregator or shared serial upstream "
            "needs a DAG counterfactual v1 does not implement")
    if not g.observations:
        return ScenarioRejection("no_matched_observations",
                                 "no matched observations of the gating set")
    if not g.check_names:
        return ScenarioRejection("no_matched_observations",
                                 "the gating set names no checks")

    seen: set[str] = set()
    for o in g.observations:
        if o.observation_id in seen:
            return ScenarioRejection(
                "duplicate_observation",
                f"observation {o.observation_id!r} appears twice")
        seen.add(o.observation_id)
        if o.basis != g.basis:
            return ScenarioRejection(
                "basis_mismatch",
                f"observation {o.observation_id!r} is on basis {o.basis!r}, "
                f"not the scenario basis {g.basis!r}")
        for name, dur in o.check_durations.items():
            if not _finite(dur):
                return ScenarioRejection(
                    "non_finite_input",
                    f"{name!r} in {o.observation_id!r} has a non-finite "
                    f"duration {dur!r}")
            if float(dur) < 0:
                return ScenarioRejection(
                    "negative_input",
                    f"{name!r} in {o.observation_id!r} has a negative "
                    f"duration {dur!r}")
        missing = sorted(set(g.check_names) - set(o.check_durations))
        if missing:
            return ScenarioRejection(
                "missing_competitor",
                f"observation {o.observation_id!r} has no duration for "
                f"{', '.join(missing)}; an unresolved competitor cannot be "
                "treated as absent from the gate")
        if not o.concurrency_validated:
            return ScenarioRejection(
                "concurrency_unvalidated",
                f"observation {o.observation_id!r} does not carry a validated "
                "concurrent timing span; similar durations are not evidence "
                "of concurrency")
        res = o.scheduling_residual_s
        if not _finite(res) or float(res) < 0:
            return ScenarioRejection(
                "non_finite_input" if not _finite(res) else "negative_input",
                f"observation {o.observation_id!r} has scheduling residual "
                f"{res!r}")
        if float(res) > CONCURRENCY_TOLERANCE_S:
            return ScenarioRejection(
                "scheduling_residual_exceeds_tolerance",
                f"observation {o.observation_id!r} has a {res}s scheduling "
                f"residual, beyond the {CONCURRENCY_TOLERANCE_S}s tolerance; "
                "a staggered start is not modelled by a runtime-only scenario")
    return None


def _validate_effects(g: GatingSet,
                      effects: Sequence[ScenarioEffect]
                      ) -> ScenarioRejection | None:
    obs_by_id = {o.observation_id: o for o in g.observations}
    all_ids = set(obs_by_id)

    # Attribution identity comes first: a numeric result may not rest on an
    # effect that cannot be told apart from another one. Solo results are keyed
    # by finding ID and overlap is keyed by (check, work_id), so a blank or
    # repeated identity silently changes which comparison the admission rule
    # makes rather than producing an error.
    seen_finding_ids: set[str] = set()
    for e in effects:
        if not (e.finding_id or "").strip():
            return ScenarioRejection(
                "missing_finding_id",
                "an effect declares no finding id; solo reductions are keyed "
                "by it and an unnamed effect cannot be compared against the "
                "joint result")
        if e.finding_id in seen_finding_ids:
            return ScenarioRejection(
                "duplicate_finding_id",
                f"{e.finding_id!r} is declared by more than one effect; the "
                "later solo reduction would overwrite the earlier one and the "
                "joint result would be admitted against one half, not both")
        seen_finding_ids.add(e.finding_id)
        if not (e.workflow or "").strip():
            return ScenarioRejection(
                "missing_workflow_identity",
                f"{e.finding_id} names no workflow; gating durations are keyed "
                "by check name, so an unattributed check cannot be told apart "
                "from a same-named check in another workflow")
        if not (e.work_id or "").strip():
            return ScenarioRejection(
                "missing_work_identity",
                f"{e.finding_id} names no affected work; unknown work is not "
                "disjoint work, and it would key the overlap check differently "
                "from the named step it may actually be")
        if not [r for r in e.evidence_refs if (r or "").strip()]:
            return ScenarioRejection(
                "missing_evidence_refs",
                f"{e.finding_id} carries no source evidence references; a "
                "supported scenario states where its reductions came from")

    # Gating durations are keyed by bare check name, so one name means one
    # check. Two workflows claiming it are two different checks sharing one
    # duration entry, and summing both reductions onto it invents a saving.
    check_workflow: dict[str, str] = {}
    for e in effects:
        for check in sorted(e.check_names):
            prior = check_workflow.setdefault(check, e.workflow)
            if prior != e.workflow:
                return ScenarioRejection(
                    "ambiguous_check_identity",
                    f"{check!r} is claimed by both {prior!r} and "
                    f"{e.workflow!r}; the gating set carries one duration for "
                    "that name, so the two cannot be modelled as one check")

    for e in effects:
        if not e.local_runtime_only:
            return ScenarioRejection(
                "not_local_runtime_only",
                f"{e.finding_id} is not a local runtime change with unchanged "
                "scheduling, coverage and job set; relocation, new sharding, "
                "cancellation and trigger changes are ineligible in v1")
        if not e.matrix_identity_resolved:
            return ScenarioRejection(
                "ambiguous_matrix_identity",
                f"{e.finding_id} does not resolve which matrix legs it changes")
        if not (e.reduction_basis or "").strip():
            return ScenarioRejection(
                "missing_reduction_basis",
                f"{e.finding_id} declares no reduction basis")
        if e.reduction_basis in CAPPED_STAMP_FIELDS:
            return ScenarioRejection(
                "capped_stamp_not_a_local_reduction",
                f"{e.finding_id} declares {e.reduction_basis!r} as its "
                "reduction basis; that field is an already-capped or "
                "workflow-level savings stamp, not a per-observation local "
                "duration reduction")
        if not (e.assumption or "").strip():
            return ScenarioRejection(
                "missing_assumption",
                f"{e.finding_id} states no modelling assumption")
        if e.basis != g.basis:
            return ScenarioRejection(
                "basis_mismatch",
                f"{e.finding_id} is on basis {e.basis!r}, not the scenario "
                f"basis {g.basis!r}")

        off = sorted(e.check_names - set(g.check_names))
        if off:
            return ScenarioRejection(
                "off_gating_set_check",
                f"{e.finding_id} names {', '.join(off)}, which is not in the "
                "measured gating set; an off-spine check has no competitor "
                "semantics and cannot enter a joint scenario")

        seen: set[tuple[str, str]] = set()
        for eo in e.observations:
            key = (eo.observation_id, eo.check_name)
            if key in seen:
                return ScenarioRejection(
                    "duplicate_effect_row",
                    f"{e.finding_id} states {eo.check_name!r} twice for "
                    f"{eo.observation_id!r}")
            seen.add(key)
            if eo.observation_id not in obs_by_id:
                return ScenarioRejection(
                    "insufficient_effect_evidence",
                    f"{e.finding_id} refers to unmatched observation "
                    f"{eo.observation_id!r}")
            if not _finite(eo.reduction_s) or not _finite(eo.affected_work_s):
                return ScenarioRejection(
                    "non_finite_input",
                    f"{e.finding_id} has a non-finite reduction or affected "
                    f"work in {eo.observation_id!r}")
            if float(eo.reduction_s) < 0 or float(eo.affected_work_s) < 0:
                return ScenarioRejection(
                    "negative_input",
                    f"{e.finding_id} has a negative reduction or affected "
                    f"work in {eo.observation_id!r}")
            if float(eo.reduction_s) > float(eo.affected_work_s):
                return ScenarioRejection(
                    "reduction_exceeds_affected_work",
                    f"{e.finding_id} models {eo.reduction_s}s off "
                    f"{eo.check_name!r} in {eo.observation_id!r} but only "
                    f"{eo.affected_work_s}s of affected work is present")
            d_before = float(
                obs_by_id[eo.observation_id].check_durations[eo.check_name])
            if float(eo.affected_work_s) > d_before:
                return ScenarioRejection(
                    "affected_work_exceeds_duration",
                    f"{e.finding_id} claims {eo.affected_work_s}s of affected "
                    f"work in {eo.check_name!r} but {eo.observation_id!r} "
                    f"observed only {d_before}s")

        covered = {eo.observation_id for eo in e.observations}
        if covered != all_ids:
            gap = sorted(all_ids - covered)
            return ScenarioRejection(
                "insufficient_effect_evidence",
                f"{e.finding_id} has no modeled effect for "
                f"{', '.join(gap) or 'some observations'}; a per-job effect "
                "must be stated for every matched observation")

    # compatibility: v1 accepts only disjoint affected work
    owner: dict[tuple[str, str], str] = {}
    for e in effects:
        for check in sorted(e.check_names):
            key = (check, e.work_id)
            prior = owner.get(key)
            if prior is not None:
                return ScenarioRejection(
                    "overlapping_affected_work",
                    f"{prior} and {e.finding_id} both change {e.work_id!r} in "
                    f"{check!r}; overlapping alternatives are rejected, never "
                    "summed or heuristically de-overlapped")
            owner[key] = e.finding_id

    # Disjointness is a producer-declared label, so it is also budgeted against
    # the observation. Two effects that each claim 250s of affected work in one
    # 300s check cannot both be telling the truth, whatever their work ids say,
    # and summing their reductions would take the same seconds off twice.
    work_budget: dict[tuple[str, str], float] = {}
    for e in effects:
        for eo in e.observations:
            key = (eo.observation_id, eo.check_name)
            work_budget[key] = work_budget.get(key, 0.0) + float(eo.affected_work_s)
    for (oid, check), claimed in sorted(work_budget.items()):
        observed = float(obs_by_id[oid].check_durations[check])
        if claimed > observed:
            return ScenarioRejection(
                "affected_work_sum_exceeds_duration",
                f"the selected effects claim {claimed}s of affected work in "
                f"{check!r} in {oid!r}, which observed only {observed}s; work "
                "declared disjoint that does not fit inside the check is "
                "double-counted, not composed")
    return None


# --------------------------------------------------------------------------
# calculation
# --------------------------------------------------------------------------

def _after_durations(o: GatingObservation,
                     effects: Sequence[ScenarioEffect]
                     ) -> dict[str, float] | ScenarioRejection:
    after = {k: float(v) for k, v in o.check_durations.items()}
    for e in effects:
        for eo in e.observations:
            if eo.observation_id != o.observation_id:
                continue
            after[eo.check_name] = after[eo.check_name] - float(eo.reduction_s)
    for name, value in after.items():
        if value < 0:
            return ScenarioRejection(
                "negative_post_fix_duration",
                f"{name!r} in {o.observation_id!r} models to {value}s; the "
                "selected reductions exceed the observed duration")
    return after


def evaluate_scenario(gating: GatingSet,
                      effects: Sequence[ScenarioEffect]) -> ScenarioResult:
    """Model the merge gate under the selected effects, or say why it cannot.

    Every finding's solo effect is evaluated with the same machinery, so the
    individual and joint numbers below can never drift apart.

        d_after(r,j,S) = d_before(r,j) - sum(eligible local reductions for j)
        T_before(r)    = max_j d_before(r,j)
        T_after(r,S)   = max_j d_after(r,j,S)
        delta(r,S)     = T_before(r) - T_after(r,S)
        scenario_delta_p50(S) = median_r delta(r,S)

    `max` is taken PER OBSERVATION, before any aggregation: median(max(checks))
    is not max(median(checks)), and per-check medians throw away exactly the
    co-occurrence information the gate is made of. The three medians are
    reported separately and are NOT required to subtract into one another.
    """
    return _evaluate(gating, tuple(effects), with_individuals=True)


def _evaluate(gating: GatingSet, effects: tuple[ScenarioEffect, ...],
              *, with_individuals: bool) -> ScenarioResult:
    rej = _validate_gating(gating)
    if rej is None:
        rej = _validate_effects(gating, effects)
    if rej is not None:
        return _reject(rej.code, rej.detail)

    t_before: list[float] = []
    t_after: list[float] = []
    deltas: list[float] = []
    after_by_check: dict[str, list[float]] = {n: [] for n in gating.check_names}

    for o in gating.observations:
        after = _after_durations(o, effects)
        if isinstance(after, ScenarioRejection):
            return _reject(after.code, after.detail)
        before_max = max(float(o.check_durations[n]) for n in gating.check_names)
        after_max = max(after[n] for n in gating.check_names)
        t_before.append(before_max)
        t_after.append(after_max)
        deltas.append(before_max - after_max)
        for n in gating.check_names:
            after_by_check[n].append(after[n])

    individual: dict[str, float] = {}
    if with_individuals:
        for e in effects:
            solo = _evaluate(gating, (e,), with_individuals=False)
            if not solo.supported:
                return _reject(
                    solo.rejection.code,
                    f"{e.finding_id} alone: {solo.rejection.detail}")
            individual[e.finding_id] = solo.median_delta_s

    return ScenarioResult(
        supported=True,
        finding_ids=tuple(sorted(e.finding_id for e in effects)),
        n_observations=len(gating.observations),
        basis=gating.basis,
        median_t_before_s=median(t_before),
        median_t_after_s=median(t_after),
        median_delta_s=median(deltas),
        individual_delta_s=individual,
        modeled_check_after_s={n: median(v) for n, v in after_by_check.items()},
        tolerated_residual_s=max(
            (float(o.scheduling_residual_s) for o in gating.observations),
            default=0.0),
        assumptions=tuple(e.assumption for e in effects),
        evidence_refs=tuple(r for e in effects for r in e.evidence_refs),
    )


def eligible_pairs(gating: GatingSet,
                   effects: Sequence[ScenarioEffect]) -> list[ScenarioResult]:
    """Every supported pair with a positive modeled joint reduction, ranked by
    that reduction and then by stable finding IDs. Unsupported pairs are
    dropped silently here — the caller renders the limitation, not a number."""
    ordered = sorted(effects, key=lambda e: e.finding_id)
    out: list[ScenarioResult] = []
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            res = evaluate_scenario(gating, [ordered[i], ordered[j]])
            if res.supported and _display(res.median_delta_s) > 0:
                out.append(res)
    out.sort(key=lambda r: (-_display(r.median_delta_s), r.finding_ids))
    return out


def select_joint_block(gating: GatingSet,
                       effects: Sequence[ScenarioEffect]
                       ) -> ScenarioResult | None:
    """The at-most-one pair worth rendering: the highest-ranked eligible pair
    whose joint reduction exceeds BOTH individual reductions at the report's
    displayed precision. This is a scenario-selection rule, not a significance
    or noise threshold — a pair that merely ties its best half tells the reader
    nothing the single finding did not already say."""
    for res in eligible_pairs(gating, effects):
        joint = _display(res.median_delta_s)
        if all(joint > _display(v) for v in res.individual_delta_s.values()):
            return res
    return None


# --------------------------------------------------------------------------
# artifact loading
# --------------------------------------------------------------------------

def load_inputs(doc: Mapping[str, Any]
                ) -> tuple[GatingSet | None, tuple[ScenarioEffect, ...],
                           ScenarioRejection | None]:
    """Read a producer-stamped contract out of a findings artifact.

    No producer stamps it today, so on a current artifact this returns
    `contract_inputs_absent` — deliberately, rather than reconstructing a
    scenario out of the aggregate stamps that happen to be present.
    See `MISSING_PRODUCER_EVIDENCE`.
    """
    raw = doc.get(JOINT_SCENARIO_INPUTS_KEY) if isinstance(doc, Mapping) else None
    if not isinstance(raw, Mapping) or not raw:
        return None, (), ScenarioRejection(
            "contract_inputs_absent",
            f"no {JOINT_SCENARIO_INPUTS_KEY!r} in the findings artifact; "
            "joint sizing needs stamped per-observation effects, not the "
            "capped per-finding savings stamps")

    version = raw.get("contract_version")
    if version != SCENARIO_CONTRACT_VERSION:
        return None, (), ScenarioRejection(
            "unsupported_contract_version",
            f"contract version {version!r} is not "
            f"{SCENARIO_CONTRACT_VERSION}; refusing to interpret it")

    try:
        gating = GatingSet(
            topology=str(raw.get("topology") or ""),
            check_names=frozenset(str(n) for n in (raw.get("check_names") or ())),
            basis=str(raw.get("basis") or ""),
            observations=tuple(
                GatingObservation(
                    observation_id=str(o.get("observation_id") or ""),
                    basis=str(o.get("basis") or ""),
                    check_durations={str(k): float(v) for k, v
                                     in (o.get("check_durations") or {}).items()},
                    concurrency_validated=bool(o.get("concurrency_validated")),
                    scheduling_residual_s=float(
                        o.get("scheduling_residual_s") or 0.0),
                )
                for o in (raw.get("observations") or ())),
        )
        effects = tuple(
            ScenarioEffect(
                finding_id=str(e.get("finding_id") or ""),
                workflow=str(e.get("workflow") or ""),
                work_id=str(e.get("work_id") or ""),
                basis=str(e.get("basis") or ""),
                assumption=str(e.get("assumption") or ""),
                evidence_refs=tuple(str(r) for r in (e.get("evidence_refs") or ())),
                local_runtime_only=bool(e.get("local_runtime_only")),
                matrix_identity_resolved=bool(e.get("matrix_identity_resolved")),
                reduction_basis=str(e.get("reduction_basis") or ""),
                observations=tuple(
                    EffectObservation(
                        observation_id=str(eo.get("observation_id") or ""),
                        check_name=str(eo.get("check_name") or ""),
                        reduction_s=float(eo.get("reduction_s")),
                        affected_work_s=float(eo.get("affected_work_s")),
                    )
                    for eo in (e.get("observations") or ())),
            )
            for e in (raw.get("effects") or ()))
    except (AttributeError, TypeError, ValueError) as exc:
        return None, (), ScenarioRejection(
            "malformed_contract_inputs", f"could not read the contract: {exc}")

    return gating, effects, None

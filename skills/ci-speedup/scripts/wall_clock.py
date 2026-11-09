"""Wall-clock lever model — the cascade of physical bounds that decides whether
a finding actually shortens a developer's wait.

A finding's raw wall-clock estimate is squeezed down through a series of
physical bounds toward the true developer-wait it removes:

    raw per-pattern estimate
      → CAP 1  within-workflow critical path   (_cap_wall_clock)
      → CAP 2  cross-workflow concurrency floor (_cap_wall_clock_cross_workflow)
      → effective Δ wall-clock

The mental model: developer wall-clock wait = max over all workflows triggered
on the PR of (that workflow's critical path). A finding is a real wall-clock
lever ⟺ it shortens the long pole of the GATING workflow, and only down to the
next floor.

This module is a LEAF — it depends only on the stdlib (no `collect_runs`, no
`config`), so the bounds are unit-testable in isolation (`import wall_clock`).
`collect_runs` owns the data sampling and the `_critical_path` aggregation that
PRODUCES the `crit` dict these bounds consume; the bounds here only reason over
already-measured inputs (a crit dict, a concurrency list, a raw estimate).

Step 1 of the extraction moved the existing bound functions here verbatim.
Step 2 wraps them in a uniform `Bound` contract behind `size_wall_clock()` with
monotonic-down + no-silent-shrink invariants; Step 3 adds further bounds.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


def observed_commit_wall_s(
    jobs: list[dict[str, Any]],
    *,
    cohort_created_at: list[str] | None = None,
) -> float | None:
    """Return earliest supplied creation to last-job-completed wall time.

    Callers pass one complete, exact-head push cohort and one attempt per
    workflow. Cohort completeness and attempt identity remain the collector's
    responsibility; this helper owns the shared timestamp formula. By default
    job creation is the lower boundary. Existing collector callers may supply
    workflow-run creation timestamps for their established commit-wall metric.
    """
    created: list[float] = []
    completed: list[float] = []
    for job in jobs:
        if not job.get("created_at") or not job.get("completed_at"):
            return None
        try:
            created.append(datetime.fromisoformat(
                str(job["created_at"]).replace("Z", "+00:00")).timestamp())
            completed.append(datetime.fromisoformat(
                str(job["completed_at"]).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            return None
    if cohort_created_at is not None:
        try:
            created = [datetime.fromisoformat(
                str(value).replace("Z", "+00:00")).timestamp()
                for value in cohort_created_at]
        except (TypeError, ValueError):
            return None
    if not created or not completed or max(completed) < min(created):
        return None
    return round(max(completed) - min(created), 3)


def _resolve_job_p50(job_key: str, job_p50: dict[str, float]) -> float:
    """Resolve a YAML job key to its sampled p50, bridging the namespace gap:
    scan.py's `affected_jobs` are YAML job *keys* (`lint`), while `job_p50` is
    keyed by GitHub's job *display names*, which for a matrix job carry a
    suffix (`lint (ubuntu-latest, 18)`). Exact match first; else the max over
    display names that start with `<key> (` (the matrix expansion). Returns
    0.0 when nothing resolves (a `name:`-overridden job we can't map — the
    caller treats that as "can't locate, don't demote")."""
    if job_key in job_p50:
        return job_p50[job_key]
    prefix = job_key + " ("
    cands = [v for name, v in job_p50.items() if name.startswith(prefix)]
    if cands:
        return max(cands)
    # Fallback: the GitHub display name is often a `name:`-override of the YAML
    # key (`integration` → "Integration test", `smoke` → "Smoke test (22.x)",
    # `workspace-tests` → "Workspace Tests / test-docker (azure)"). Match the key
    # as a normalized LEADING WORD of the display name (hyphens≈spaces, case-
    # insensitive), bounded by a space / "(" / "/" so a longer word can't match.
    # Take the max (slowest leg) — a conservative per-job figure that never
    # exceeds the job's own work, and never the unrelated workflow long pole.
    # A reusable-workflow leg's display name is "CalledWorkflowName / jobname"
    # (it carries a " / "). Matching a CALLER job key to one such leg would size
    # the whole called workflow by a single leg — semantically muddy and
    # inconsistent (some callers match, some don't). Exclude " / " names so a
    # reusable-workflow caller stays unresolved (→ rendered qualitatively),
    # while plain `name:` renames (e.g. "Integration test") still resolve.
    norm = job_key.replace("-", " ").lower().strip()
    if norm:
        word = [v for name, v in job_p50.items()
                if " / " not in name
                and ((dn := name.replace("-", " ").lower()) == norm
                     or dn.startswith(norm + " ") or dn.startswith(norm + "("))]
        if word:
            return max(word)
    return 0.0


def _wf_basename(wf_path: str) -> str:
    return wf_path.rsplit("/", 1)[-1] if wf_path else wf_path


# =============================================================================
# CAP 1 — within-workflow critical path
# =============================================================================

def _cap_wall_clock(wc: float, job_name: str, crit: dict[str, Any] | None) -> tuple[float, str]:
    """Cap a per-step/per-job wall-clock saving against the workflow's critical
    path so it can NEVER exceed (or come near) the whole run. Returns (capped_wc,
    note). Without timing context (`crit` empty) the raw estimate is returned.

    - If the step's JOB finishes at/below the cluster floor it is NOT the long
      pole, so fixing it saves runner-minutes but ZERO wall-clock.
    - Otherwise the saving is capped at the long-pole headroom (long_pole −
      floor) — you can only shorten the critical path down to the next job."""
    if not crit:
        return round(wc, 1), ""
    long_pole = crit.get("long_pole_p50", 0.0) or 0.0
    floor = crit.get("floor_p50", 0.0) or 0.0
    if not long_pole:
        return round(wc, 1), ""
    own = _resolve_job_p50(job_name, crit.get("job_p50") or {})
    if own > 0 and own <= floor:
        return 0.0, ("affected job finishes below the cluster floor — "
                     "runner-minute (bill) only, no wall-clock")
    headroom = max(long_pole - floor, 0.0)
    if wc > headroom:
        return round(headroom, 1), ("wall-clock capped at the critical-path "
                                    "headroom (the next job gates the run)")
    return round(wc, 1), ""


# =============================================================================
# CAP 2 — cross-workflow concurrency floor
# =============================================================================

def _concurrent_workflows(
    wf_path: str,
    events_by_wf: dict[str, set[str]],
    crit_by_wf: dict[str, dict[str, Any]],
) -> list[tuple[str, float]]:
    """Other workflows that share a trigger event with `wf_path` and therefore
    run CONCURRENTLY with it (e.g. every workflow that fires on the same
    `pull_request`). Returns [(other_wf_path, its_long_pole_p50)], sorted
    slowest-first. These set the floor for a wall-clock saving: shortening
    `wf_path` only cuts the developer's wait down to the slowest sibling that
    is still running alongside it."""
    my_events = events_by_wf.get(wf_path) or set()
    if not my_events:
        return []
    # Concurrency is PER EVENT: two workflows only run alongside each other on
    # a trigger they BOTH fire on. Size against the developer-wait event — the
    # PR path (`pull_request`/`merge_group`) the report's wall-clock models —
    # not `push`-to-main or release triggers a developer doesn't sit waiting on.
    # Picking one event also avoids a `push`-only sibling getting counted as
    # concurrent with a workflow that merely ALSO runs on push.
    primary = next(
        (e for e in ("pull_request", "merge_group", "push") if e in my_events),
        next(iter(sorted(my_events))),
    )
    out: list[tuple[str, float]] = []
    for other, ev in events_by_wf.items():
        if other == wf_path or primary not in ev:
            continue
        oc = crit_by_wf.get(other) or {}
        # A run-list-TRIAGED sibling (collect_runs skipped its job fetch) has
        # `long_pole_p50 == 0` but still runs concurrently on the PR, so it falls back
        # to `concurrent_wall_p50` (its run-list wall-time) — a conservative floor
        # contribution (the whole-run wall is >= its long-pole job, so it can only
        # TIGHTEN the saving, never overstate). Absent for non-triaged siblings.
        lp = (oc.get("long_pole_p50", 0.0) or 0.0) or (oc.get("concurrent_wall_p50", 0.0) or 0.0)
        if lp > 0:
            out.append((other, lp))
    out.sort(key=lambda kv: -kv[1])
    return out


def _cap_wall_clock_cross_workflow(
    wc: float, wf_path: str, crit: dict[str, Any] | None,
    concurrent: list[tuple[str, float]],
) -> tuple[float, str]:
    """Cap a wall-clock saving at the CROSS-workflow floor. The developer waits
    for every workflow triggered on the PR; shortening this one only helps down
    to the slowest sibling running alongside it. Returns (capped_wc, note).

    - If a sibling workflow is already at least as slow as this one's long pole,
      this workflow is NOT the developer's critical path → 0 wall-clock saved
      (the fix still saves runner-minutes; that figure is untouched).
    - Otherwise cap at (this long pole − slowest sibling long pole)."""
    if not concurrent or not crit:
        return round(wc, 1), ""
    long_pole = crit.get("long_pole_p50", 0.0) or 0.0
    if not long_pole:
        return round(wc, 1), ""
    slowest_sibling = concurrent[0][1]
    headroom = max(long_pole - slowest_sibling, 0.0)
    if wc <= headroom:
        return round(wc, 1), ""
    sib_path, sib_lp = concurrent[0]
    base = _wf_basename(sib_path)
    if headroom <= 0:
        return 0.0, (f"no wall-clock saving — `{base}` ({sib_lp:.0f}s) runs "
                     f"concurrently on the same trigger and gates the PR, so "
                     f"shortening this workflow doesn't reduce the developer's wait")
    return round(headroom, 1), (
        f"wall-clock capped at the cross-workflow floor — `{base}` "
        f"({sib_lp:.0f}s) runs concurrently on the same trigger, so the saving "
        f"floors there (not the full per-workflow figure)")


# =============================================================================
# The bound cascade — a uniform contract over the caps above.
#
# A finding's RAW per-pattern wall-clock estimate passes through an ordered
# list of Bounds. Each Bound takes (value, ctx) and returns a BoundResult: the
# (possibly tightened) value plus a REASON whenever it changed. `size_wall_clock`
# runs the cascade and enforces two invariants so the report can never overstate
# a saving or shrink one without explanation:
#   - monotonic-down: a bound may only LOWER the saving, never raise it.
#   - no-silent-shrink: any reduction MUST carry a human-readable reason.
# The applied (name, before, after, reason) steps are returned as `derivation`
# so the report can show exactly which physical bound set the final number.
# =============================================================================

# (population_share, that PR's pr_checks tuple) — one sampled PR's critical path
# when the merge gate is bimodal. `share` ~= 1/m over the m emitted populations;
# the inner tuple is the SAME (check_name, p50_seconds) shape as `pr_checks`.
PrPopulation = tuple[float, tuple[tuple[str, float], ...]]


@dataclass(frozen=True)
class WallClockContext:
    """Everything a bound may read about ONE finding's workflow. All inputs are
    already MEASURED (collect_runs samples them); bounds only reason over them."""
    workflow: str = ""
    crit: dict[str, Any] | None = None
    # (other_workflow, its_long_pole_p50), slowest-first — from _concurrent_workflows.
    concurrent: tuple[tuple[str, float], ...] = ()
    # YAML job keys this finding touches (for the within-workflow floor check).
    affected_jobs: tuple[str, ...] = ()
    # Trigger events that actually fired this workflow (run.event). Empty = unknown.
    events: tuple[str, ...] = ()
    # MEASURED critical path: every check that ran on a representative PR, as
    # (check_name, p50_seconds) — sourced from the gh check-runs API, so it
    # includes checks with NO workflow file (CodeQL default setup, third-party
    # app checks) that the file-based scan can't see. Empty = unavailable.
    pr_checks: tuple[tuple[str, float], ...] = ()
    # The check names that belong to THIS finding's workflow (its job display
    # names) — used to separate "this workflow's checks" from the concurrent
    # floor in `pr_checks`.
    own_check_names: frozenset[str] = frozenset()
    # M2 — per-PR-POPULATION critical paths, when the merge gate is bimodal. A
    # conditional gate (e.g. mastra `changed-tests`, which self-skips to well
    # under its full duration on docs-only PRs and runs ~750s on code PRs) means
    # there is no single critical path: the slowest check differs by population.
    # Each entry is one PrPopulation; shares are 1/m over the m populations and
    # sum to ~1.0 (modulo per-share rounding). The measured-critical-path bound
    # then credits a finding the SHARE-WEIGHTED saving across populations — so a
    # check that's the pole only when the gate self-skips (e.g. e2e-docs on docs
    # PRs) earns its real wall-clock for that population instead of being zeroed
    # by the gate it never runs alongside. Empty → fall back to the single
    # `pr_checks` path (no bimodal gate found).
    pr_check_populations: tuple[PrPopulation, ...] = ()
    # Completeness of the check-runs read behind each population. A complete
    # singleton proves that the target was the PR's only check; an incomplete
    # read keeps the missing sibling floor unknown. Empty/misaligned preserves
    # the legacy conservative behavior for older artifacts.
    pr_check_populations_complete: tuple[bool, ...] = ()
    # ENG-1 PR-N3 — the chain facts (from `pr_critical_path.chain_summary`),
    # empty/zero on chainless or legacy artifacts (behavior then identical to
    # the pre-chain cascade). `chain_members` = the modal chain's check names;
    # `chain_p50_s` = the typical whole-chain wait; `chain_win_s` = the
    # median per-PR headroom above the next-longest competing path.
    chain_members: frozenset[str] = frozenset()
    chain_p50_s: float = 0.0
    chain_win_s: float = 0.0
    # One tuple per complete sampled commit: (whole-commit wall, slowest job,
    # next-longest job outside the slowest job's `needs:` chain[, that chain's
    # span on the commit]). A fifth value may carry a targeted multi-job path
    # population as (chain span, job duration, affected) tuples. The commit
    # projection bound uses every sampled tuple, then takes their share-weighted
    # mean allowed saving. Without the fourth value the slowest job is its own
    # chain.
    commit_wall_facts: tuple[tuple[Any, ...], ...] = ()
    # #44 — a CLUSTER-FLOOR lever (OPT73): its fix lowers ALL of `own_check_names`
    # (the concurrent sibling legs running the shared step) in lockstep, so a
    # SIBLING leg can never be the floor that caps it — the sibling descends WITH
    # the fix. When True the chain-aware branch is BYPASSED: the chain's own
    # runner-up / headroom (`chain_win_s`) is a sibling-vs-sibling gap that must not
    # cap the win; the ceiling instead floors at the slowest NON-sibling concurrent
    # check (the `own_check_names` scoping already computes this). Default False =
    # every other finding shortens ONE leg, so its own siblings DO floor it (classic
    # chain-aware behavior, byte-stable).
    cluster_floor_lever: bool = False


@dataclass(frozen=True)
class BoundResult:
    value: float
    reason: str | None = None


@dataclass(frozen=True)
class WallClockResult:
    raw_s: float
    effective_s: float
    # ordered (bound_name, before, after, reason) for each bound that fired.
    derivation: list[tuple[str, float, float, str]] = field(default_factory=list)


@dataclass(frozen=True)
class CommitProjection:
    projected_s: float
    multiple: float | None


def project_commit_wall(
    wall_s: float,
    slowest_job_s: float,
    next_longest_s: float,
    job_speedup: float,
    *,
    chain_s: float | None = None,
    pole_s: float | None = None,
    runner_up_s: float | None = None,
    round_result: bool = True,
) -> CommitProjection:
    """Project a whole-commit wait after speeding up its slowest job.

    The default is the single-job projection: work outside the slowest job is
    unchanged and the next-longest independent job is the floor. When the job
    belongs to a serial ``needs:`` chain, the chain inputs replace that
    single-job path. Other chain members remain in series, while ``runner_up_s``
    is the longest path that does not pass through the pole. Queue time and
    other gaps already present in ``wall_s`` remain unchanged.
    """
    wall = max(float(wall_s), 0.0)
    slowest = max(float(slowest_job_s), 0.0)
    next_longest = max(float(next_longest_s), 0.0)
    speedup = float(job_speedup)
    if speedup <= 0:
        raise ValueError("job_speedup must be greater than zero")

    chain_values = (chain_s, pole_s, runner_up_s)
    if all(value is None for value in chain_values):
        projected = wall - slowest + max(slowest / speedup, next_longest)
    elif any(value is None for value in chain_values):
        raise ValueError(
            "chain_s, pole_s and runner_up_s must be supplied together"
        )
    else:
        chain = max(float(chain_s), 0.0)
        pole = max(float(pole_s), 0.0)
        runner_up = max(float(runner_up_s), 0.0)
        if pole > chain:
            raise ValueError("pole_s cannot exceed chain_s")
        unchanged = max(wall - max(chain, runner_up), 0.0)
        projected_path = max(chain - pole + pole / speedup, runner_up)
        projected = unchanged + projected_path

    projected = max(projected, 0.0)
    if round_result:
        projected = round(projected, 1)
    multiple = round(wall / projected, 2) if projected > 0 else None
    return CommitProjection(projected, multiple)


def _affected_floor_p50(ctx: WallClockContext) -> float:
    """Max p50 across the finding's affected jobs (0.0 if none resolve) —
    mirrors collect_runs._affected_job_p50, so the within-workflow bound makes
    the SAME below-floor decision the inline sizing always has."""
    jp = (ctx.crit or {}).get("job_p50") or {}
    vals = [v for v in (_resolve_job_p50(j, jp) for j in ctx.affected_jobs) if v > 0]
    return max(vals) if vals else 0.0


def bound_within_workflow(value: float, ctx: WallClockContext) -> BoundResult:
    """CAP 1 — the saving can't exceed the workflow's own long-pole headroom,
    and is zero when the affected job finishes at/below the cluster floor (it
    isn't the long pole). Replicates _cap_wall_clock over the affected-jobs set."""
    crit = ctx.crit
    if not crit:
        return BoundResult(round(value, 1))
    long_pole = crit.get("long_pole_p50", 0.0) or 0.0
    floor = crit.get("floor_p50", 0.0) or 0.0
    if not long_pole:
        return BoundResult(round(value, 1))
    own = _affected_floor_p50(ctx)
    if own > 0 and own <= floor:
        return BoundResult(0.0, "affected job finishes below the cluster floor — "
                                "runner-minute (bill) only, no wall-clock")
    headroom = max(long_pole - floor, 0.0)
    if value > headroom:
        return BoundResult(round(headroom, 1),
                           "wall-clock capped at the critical-path headroom "
                           "(the next job gates the run)")
    return BoundResult(round(value, 1))


# Events a developer actively waits on before merging. A workflow that runs
# ONLY on other triggers (push-to-main, schedule, workflow_run after merge) is
# not on the PR critical path — its time is post-merge / scheduled, not the
# developer wait the report's primary metric measures.
_DEVELOPER_FACING_EVENTS = ("pull_request", "merge_group")


def bound_developer_facing(value: float, ctx: WallClockContext) -> BoundResult:
    """Bound 3/4 sharpened — gate wall-clock on the workflow actually being on
    the developer's PR critical path. If its sampled triggers are ALL
    non-developer-facing (push-to-main, schedule, workflow_run), a saving there
    is post-merge/scheduled time, not PR developer-wait → 0 wall-clock (the
    runner-minute saving is untouched). Unknown events (none sampled) pass
    through — we don't guess."""
    if value <= 0 or not ctx.events:
        return BoundResult(round(value, 1))
    if any(e in _DEVELOPER_FACING_EVENTS for e in ctx.events):
        return BoundResult(round(value, 1))
    return BoundResult(0.0,
                       "workflow runs only on non-developer-facing triggers "
                       f"({', '.join(sorted(ctx.events))}) — its wall-clock is "
                       "post-merge/scheduled time, not developer PR wait; "
                       "runner-minute (bill) saving only")


def bound_measured_critical_path(value: float, ctx: WallClockContext) -> BoundResult:
    """CAP 2 (measured) — floor the saving at the slowest OTHER check that ran
    on a representative PR, sourced from the gh check-runs API. This is the
    developer's true wall-clock: the PR isn't mergeable until the slowest check
    finishes, and check-runs include checks with no workflow file (CodeQL
    default setup, app checks) that the file-based model otherwise misses.

    A finding's job is only on the critical path if THIS workflow's slowest
    check is at/above every other check; otherwise shortening it can't move the
    PR's wall-clock (e.g. shaving Docs E2E 334s when CodeQL runs 1359s saves 0).
    Empty `pr_checks` (no check-run data) → pass through to the sampled-workflow
    fallback (`bound_cross_workflow`)."""
    if value <= 0 or not ctx.pr_checks:
        return BoundResult(round(value, 1))
    # No sampled job for this finding's workflow, yet we DO have the measured PR
    # critical path: the workflow doesn't run as a check on a representative PR
    # at all (a config-file finding like turbo.json, or a dormant/0-run
    # workflow). Its wall-clock is a pure MODELED estimate with no measured
    # presence on the critical path — not developer wait we ever observed. Zero
    # it; the finding keeps its runner-minute / cache-hygiene value.
    if not ctx.own_check_names:
        return BoundResult(0.0, (
            "not on the developer's critical path — this finding's workflow has "
            "no sampled run on a representative PR (modeled estimate only), so a "
            "saving here removes no measured PR wait; runner-minute / hygiene "
            "value only"))
    # One population unless a bimodal merge gate split the sample (M2).
    pops = ctx.pr_check_populations or ((1.0, ctx.pr_checks),)
    # ENG-1 PR-N3 — chain-aware flooring. A `needs:` chain member is NOT
    # floored by its own chain (serialized, not concurrent): its saving is
    # 1:1 up to the whole-chain headroom (`chain_win_s`, already the bound
    # above the next-longest competing path). A NON-member is floored by the
    # chain's SUM, not merely its slowest member — the chain gates as one
    # unit. Chain fields are aggregate (per-summary), applied uniformly
    # across populations; empty fields reproduce the classic behavior
    # exactly (legacy/chainless artifacts byte-stable).
    _chain_on = bool(ctx.chain_members) and ctx.chain_p50_s > 0
    # #44 — a cluster-floor lever bypasses BOTH the chain_win_s cap (the
    # `_own_in_chain` branch below) AND the chain-collapse floor (further down):
    # its sibling legs all descend together, so a sibling can neither cap the win
    # (via chain_win_s) nor floor it (via the collapsed gate chain). The ceiling is
    # the slowest NON-sibling concurrent check, which the plain population floor
    # computes with `own = the sibling legs`.
    _cluster = ctx.cluster_floor_lever
    _own_in_chain = _chain_on and not _cluster and bool(ctx.own_check_names & ctx.chain_members)
    if _own_in_chain:
        # Mixed cell (found by N3's adversarial pass, semantics corrected on
        # real data): a finding may span a chain member AND non-members (a
        # shared-step cluster does). Its credit is justified by the MEMBER
        # portion alone - the fix takes `value` seconds off the member 1:1,
        # shrinking the chain - so the member path stands for any
        # intersection, but the credit is additionally bounded by the
        # member's own observed span: a member can't give back more time
        # than it runs (the reviewer's over-credit case, value > member
        # span, lands on that bound). Routing mixed sets to the general
        # concurrent floor instead was tried and rejected: on the deepgram
        # acceptance repo it zeroed OPT73 - the very cross-leg lever the
        # measured narrative names - because the synthetic chain gate
        # contains the member's own span (self-flooring, the exact bug
        # class N3 exists to fix).
        _member_span = max((d for _share, checks in pops for n, d in checks
                            if n in (ctx.own_check_names & ctx.chain_members)),
                           default=0.0)
        # The chain and member spans are independently reduced aggregates. A
        # slow population can make the member maximum exceed the chain median,
        # so use a coherent projection baseline without changing the existing
        # conservative headroom cap.
        _chain_for_projection = max(ctx.chain_p50_s, _member_span)
        _runner_up = max(_chain_for_projection - ctx.chain_win_s, 0.0)
        _remaining = max(_member_span - value, 0.0)
        _speedup = (_member_span / _remaining
                    if _remaining > 0 else float("inf"))
        _projection = project_commit_wall(
            _chain_for_projection,
            _member_span,
            _runner_up,
            _speedup,
            chain_s=_chain_for_projection,
            pole_s=_member_span,
            runner_up_s=_runner_up,
            round_result=False,
        )
        cap = max(0.0, min(
            value,
            _chain_for_projection - _projection.projected_s,
            _member_span,
        ))
        if round(cap, 1) >= round(value, 1):
            return BoundResult(round(value, 1))
        _bound_via = ("chain's headroom" if ctx.chain_win_s <= _member_span
                      else "member's own span")
        return BoundResult(round(cap, 1), (
            f"chain member — `needs:` serializes it, so time cut here comes 1:1 "
            f"off the chain wait, capped at the {_bound_via} "
            f"(~{min(ctx.chain_win_s, _member_span):.0f}s)"))
    weighted = 0.0
    completion_aligned = len(ctx.pr_check_populations_complete) == len(pops)
    per_pop: list[tuple[float, float, str | None, float, float, bool, bool, bool]] = []
    for pop_idx, (share, checks) in enumerate(pops):
        own = [d for n, d in checks if n in ctx.own_check_names]
        others = [(n, d) for n, d in checks if n not in ctx.own_check_names]
        if _chain_on and not _cluster:
            # Collapse the chain's members into ONE synthetic gate at the
            # chain's summed wait — its members' individual spans understate
            # the serialized whole. Skipped for a cluster-floor lever (#44): the
            # chain's pole is a SIBLING leg that descends WITH the fix, so
            # collapsing it would re-introduce a sibling's span as the floor
            # (self-flooring) and understate the win — the exact ~15x mastodon
            # bug. The sibling legs are already in `own`, so `others` here is the
            # genuine non-sibling floor set.
            others = [(n, d) for n, d in others if n not in ctx.chain_members]
            others.append(("the gate chain", ctx.chain_p50_s))
        own_max = max(own) if own else 0.0
        if own_max <= 0:
            # This finding's check doesn't run in this population → no saving here.
            cap, floor_name, floor = 0.0, None, 0.0
            unknown = incomplete = single_check = False
        elif not others:
            # With several sampled PR populations, an absent sibling is missing
            # evidence, not proof that this pole has the whole PR wall to itself.
            # Keep the legacy one-population fallback, but give an unobserved
            # sibling population no credit until a concurrent floor is known.
            complete = (completion_aligned
                        and ctx.pr_check_populations_complete[pop_idx])
            cap, floor_name, floor = (
                (min(value, own_max), None, 0.0)
                if len(pops) == 1 or complete else (0.0, None, 0.0))
            unknown = len(pops) > 1 and not complete
            incomplete = unknown and completion_aligned
            single_check = len(pops) > 1 and complete
        else:
            floor_name, floor = max(others, key=lambda kv: kv[1])
            cap = max(0.0, min(value, own_max - floor))
            unknown = incomplete = single_check = False
        weighted += share * cap
        per_pop.append((share, cap, floor_name, floor, own_max, unknown,
                        incomplete, single_check))
    weighted = round(weighted, 1)
    if weighted >= round(value, 1):
        return BoundResult(round(value, 1))  # nothing gated it in any population
    # Single population — the classic floor message (names the gating check).
    if len(pops) == 1:
        _, cap, floor_name, floor, own_max, _unknown, _incomplete, _single = per_pop[0]
        if cap <= 0:
            _kind = ("gates the merge as a serialized whole"
                     if floor_name == "the gate chain"
                     else "is a slower concurrent check on a representative PR and "
                          "gates the merge")
            return BoundResult(0.0, (
                f"no wall-clock saving — `{floor_name}` ({floor:.0f}s) {_kind}; this "
                f"workflow's slowest check is only {own_max:.0f}s, so shortening it "
                f"doesn't move the developer's wait"))
        if floor_name is None:
            return BoundResult(weighted, (
                "wall-clock capped at this check's observed span "
                f"({own_max:.0f}s) — it was the only check in the sampled "
                "population, so there is no concurrent floor"))
        return BoundResult(weighted, (
            f"wall-clock capped at the measured critical-path floor — `{floor_name}` "
            f"({floor:.0f}s) sets the concurrent merge-gate floor on a representative PR"))
    # Multiple populations — the merge gate is bimodal, so the saving is credited
    # only on the PRs where this check is near the pole, share-weighted (the
    # expected wall-clock over the sampled PR critical paths).
    total = len(per_pop)
    unknown_n = sum(1 for row in per_pop if row[5])
    incomplete_n = sum(1 for row in per_pop if row[6])
    known = [row for row in per_pop if not row[5]]
    zero = [row for row in known if row[1] <= 0]
    single = [row for row in known if row[1] > 0 and row[7]]
    positive = [row for row in known if row[1] > 0 and not row[7]]

    def _duration(seconds: float) -> str:
        whole = max(0, round(seconds))
        hours, rem = divmod(whole, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes:02d}m {secs:02d}s"
        if minutes:
            return f"{minutes}m {secs:02d}s"
        return f"{secs}s"

    clauses: list[str] = []
    if zero:
        names = {str(row[2]) for row in zero if row[2]}
        all_zero_rows_name_a_floor = all(row[2] for row in zero)
        suffix = (f" where `{next(iter(names))}` runs"
                  if len(names) == 1 and all_zero_rows_name_a_floor
                  else " where another check runs longer")
        clauses.append(f"0 on {len(zero)} of {total} PRs{suffix}")
    if positive:
        caps = {round(row[1], 1) for row in positive}
        if len(caps) == 1:
            cap = next(iter(caps))
            other = "the other " if zero and len(zero) + len(positive) == total else ""
            clauses.append(f"up to {_duration(cap)} on {other}{len(positive)} of {total} PRs")
        else:
            for cap in sorted(caps):
                count = sum(1 for row in positive if round(row[1], 1) == cap)
                clauses.append(f"up to {_duration(cap)} on {count} of {total} PRs")
    if single:
        by_cap: dict[float, int] = {}
        for row in single:
            cap = round(row[1], 1)
            by_cap[cap] = by_cap.get(cap, 0) + 1
        for cap, count in sorted(by_cap.items()):
            other = "the other " if zero and len(zero) + count == total else ""
            clauses.append(
                f"up to {_duration(cap)} on {other}{count} of {total} PRs "
                "where it was the only check")
    if incomplete_n:
        clauses.append(
            f"unknown on {incomplete_n} of {total} PRs with an incomplete check-runs read")
    legacy_unknown_n = unknown_n - incomplete_n
    if legacy_unknown_n:
        clauses.append(
            f"unknown on {legacy_unknown_n} of {total} PRs with a missing sibling observation")
    maximum = max((row[1] for row in known), default=0.0)
    split = "; ".join(clauses)
    return BoundResult(weighted, (
        f"per-PR measured floor — {split}; the maximum per-PR saving is {maximum:.0f}s; "
        f"the population-weighted expected Δ is {_duration(weighted)} of the "
        f"{_duration(value)} raw saving"))


def bound_cross_workflow(value: float, ctx: WallClockContext) -> BoundResult:
    """CAP 2 (fallback) — when check-runs aren't available, floor the saving at
    the slowest sampled workflow sharing this one's trigger. Superseded by
    bound_measured_critical_path when `pr_checks` is present (it's the full,
    measured check set), so this is a no-op then."""
    if ctx.pr_checks:
        return BoundResult(round(value, 1))  # measured bound handled it
    v, note = _cap_wall_clock_cross_workflow(
        value, ctx.workflow, ctx.crit, list(ctx.concurrent))
    return BoundResult(v, note or None)


def bound_commit_projection(value: float, ctx: WallClockContext) -> BoundResult:
    """Cap a finding at the whole-commit saving its job change can produce."""
    if value <= 0 or not ctx.commit_wall_facts:
        return BoundResult(round(value, 1))
    caps: list[float] = []
    for fact in ctx.commit_wall_facts:
        wall, slowest, next_longest = fact[:3]
        chain = fact[3] if len(fact) > 3 else slowest
        if wall <= 0:
            continue
        # A targeted multi-job finding carries every sampled path as
        # (chain span, targeted job duration, affected). Shorten all affected
        # jobs by the same finding saving, with each job saturating at zero,
        # then retain the slowest remaining path as the commit floor.
        paths = fact[4] if len(fact) > 4 else ()
        if paths:
            original_path = max(float(path[0]) for path in paths)
            projected_path = max(
                float(path[0]) - min(value, float(path[1]))
                if bool(path[2]) else float(path[0])
                for path in paths
            )
            caps.append(max(0.0, original_path - projected_path))
            continue
        if slowest <= 0:
            caps.append(0.0)
            continue
        remaining = max(slowest - value, 0.0)
        speedup = slowest / remaining if remaining > 0 else float("inf")
        projected = project_commit_wall(
            wall, slowest, next_longest, speedup,
            **({"chain_s": chain, "pole_s": slowest, "runner_up_s": next_longest}
               if chain > slowest else {}))
        caps.append(max(0.0, wall - projected.projected_s))
    if not caps:
        return BoundResult(round(value, 1))
    # Every sampled commit is one observed population share. A median collapses
    # a bimodal gate to its majority case (for example, six sibling-gated PRs
    # erase the larger saving on four singleton PRs); the expected saving is
    # the share-weighted mean across those observed commits.
    cap = round(float(statistics.mean(caps)), 1)
    if cap >= round(value, 1):
        return BoundResult(round(value, 1))
    return BoundResult(cap, (
        "whole-commit projection floor — after this job is shortened, the "
        "next-longest job outside its `needs:` chain still gates the sampled commit"))


# =============================================================================
# Structural-lever sizing helpers
#
# The hygiene caps above shrink a per-step saving DOWN to the headroom above the
# cluster floor — correct for a fix that shortens ONE job, because the next job
# then gates the run. Two STRUCTURAL levers are different and need their own
# (still floor-honest) math:
#
#   - De-triggering a non-required pole REMOVES that check from the parallel set
#     entirely, so the developer's wait drops to the next-slowest concurrent
#     check — the whole check's time above that floor, not a per-step slice.
#   - A shared sub-step that recurs across MULTIPLE cluster jobs is the one lever
#     that BEATS the floor: cutting it lowers every cluster job at once, so the
#     long pole AND the floor both drop by the step's per-job cost. The
#     wall-clock saving is the per-job step time itself (the long pole stays the
#     long pole, now shorter), not capped at the old long_pole−floor headroom.
#     The runner-minute saving is the per-job cost times the number of jobs.
#
# Both still pass through `size_wall_clock` afterward so the CROSS-workflow
# measured-critical-path floor (other concurrent checks gating the same PR) is
# enforced — these helpers only set the RAW estimate the cascade then bounds.
# =============================================================================

def credit_detrigger(own_check_s: float, other_check_s: list[float]) -> float:
    """Wall-clock removed by de-triggering / gating an expensive check so it no
    longer runs on the developer's PR path: the developer now waits for the
    slowest OTHER concurrent check instead of this one. = own − max(others),
    floored at 0 (if a sibling is already slower, removing this one saves no
    wall-clock — only runner-minutes). The cross-workflow cascade re-derives the
    same floor against the full measured check set; this is the raw estimate."""
    if own_check_s <= 0:
        return 0.0
    floor = max(other_check_s) if other_check_s else 0.0
    return round(max(own_check_s - floor, 0.0), 1)


@dataclass(frozen=True)
class SharedSubstepCredit:
    """The two-axis saving from making a shared sub-step cheap in EVERY cluster
    job that runs it. `wall_clock_s` lowers the cluster floor (it beats the
    headroom cap because the floor moves too); `runner_min_s` is the bill saving
    summed across all containing jobs (each parallel copy gets cheaper)."""
    wall_clock_s: float
    runner_min_s: float
    job_count: int

    def __post_init__(self) -> None:
        # Turn the factory's conventions into a guarantee: savings are never
        # negative and a "shared" step runs in at least one job. Catches a
        # future direct construction that desyncs the fields.
        if self.wall_clock_s < 0 or self.runner_min_s < 0 or self.job_count < 1:
            raise ValueError(
                f"invalid SharedSubstepCredit: wall_clock_s={self.wall_clock_s}, "
                f"runner_min_s={self.runner_min_s}, job_count={self.job_count}")


def credit_shared_substep(step_p50_s: float, warm_floor_s: float,
                          cluster_job_count: int) -> SharedSubstepCredit:
    """Size a shared-sub-step (cluster-floor) lever. `step_p50_s` is the step's
    per-job p50, `warm_floor_s` the irreducible warm-cache cost (e.g. a cache
    restore), `cluster_job_count` how many critical-path jobs run the step.

    Wall-clock saving = the addressable per-job step time (step − warm floor):
    because the step recurs across the whole cluster, cutting it lowers every
    cluster job together, so the long pole stays the long pole but shorter — the
    floor moved too. This is the ONLY lever that isn't capped at long_pole−floor.
    Runner-minute saving = that per-job time × the number of jobs (every copy
    gets cheaper). Both are RAW; the cross-workflow cascade still floors the
    wall-clock at the slowest other concurrent check.

    The runner-minute figure applies the cluster's cheapest-member per-job step
    time (the caller's `min(material.values())` conservative shared floor) to EVERY
    leg's volume. Because it is a MODELED step-p50 × monthly-volume figure while the
    cost spine measures billed job minutes (per-run minute rounding, and not every
    counted run actually ran the step), it can still exceed the affected legs' Σ
    measured billable — the basis mismatch, not a slowest-member over-attribution
    (issue #52 re-check). That over-credit is bounded downstream by the measured
    sizing door: OPT73 is a CLAMP pattern, so `_reground_runner_minute_savings`
    clamps this saving to the affected legs' Σ measured billable
    (`_measured_billable_for_jobs`, now joined by EXACT job identity). Bounding it
    there — the one authoritative measured chokepoint with the full cost spine —
    rather than re-deriving per-leg step means here (which has only step p50s, and
    would risk the two paths drifting) keeps a single derivation path."""
    per_job = max(step_p50_s - max(warm_floor_s, 0.0), 0.0)
    n = max(int(cluster_job_count), 1)
    return SharedSubstepCredit(
        wall_clock_s=round(per_job, 1),
        runner_min_s=round(per_job * n, 1),
        job_count=n,
    )


Bound = Callable[[float, WallClockContext], BoundResult]

# CAP 1 (within-workflow) is applied during PER-FINDING sizing in collect_runs
# (`_size_finding` / the data-driven detectors), because it is model-specific:
# sharding (OPT24) intentionally skips it — sharding parallelizes the long pole,
# whose "floor" is the same job's other matrix leg, so the generic headroom cap
# would wrongly shrink it. The CASCADE below is the set of CROSS-CUTTING bounds
# applied AFTER sizing, uniformly to every finding's wall-clock saving. Bounds
# 3-6 (per-event paths, workflow_run chains, required-check gating, matrix
# residuals) append here as they're added; they then flow through every finding
# automatically with the cascade's invariants enforced.
CASCADE: list[tuple[str, Bound]] = [
    ("developer-facing", bound_developer_facing),
    ("commit-projection", bound_commit_projection),
    ("measured-critical-path", bound_measured_critical_path),
    ("cross-workflow", bound_cross_workflow),
]


def size_wall_clock(raw_s: float, ctx: WallClockContext,
                    cascade: list[tuple[str, Bound]] | None = None) -> WallClockResult:
    """Run the raw estimate through the bound cascade. Enforces monotonic-down
    and no-silent-shrink (raises if a bound violates either — these are
    programming errors, not runtime conditions). Returns the effective saving
    plus the derivation chain."""
    steps = CASCADE if cascade is None else cascade
    quantized_raw_s = round(float(raw_s), 1)
    v = quantized_raw_s
    population_basis: float | None = None
    derivation: list[tuple[str, float, float, str]] = []
    for name, fn in steps:
        if name == "commit-projection":
            population_basis = v
        # These two bounds summarize the same sampled PR population. Feeding
        # one share-weighted result into the other weights that population a
        # second time. Evaluate both from their common input and keep the
        # tighter result before continuing the cascade.
        bound_input = (population_basis
                       if name == "measured-critical-path"
                       and population_basis is not None else v)
        r = fn(bound_input, ctx)
        if r.value > bound_input + 1e-9:
            raise AssertionError(
                f"wall-clock bound {name!r} RAISED the saving "
                f"({bound_input} -> {r.value}); bounds must be monotonic-down")
        bounded = min(v, r.value)
        if abs(bounded - v) > 1e-9:
            if not r.reason:
                raise AssertionError(
                    f"wall-clock bound {name!r} shrank the saving "
                    f"({v} -> {bounded}) without a reason")
            derivation.append((name, round(v, 1), round(bounded, 1), r.reason))
            v = bounded
    return WallClockResult(quantized_raw_s, round(v, 1), derivation)

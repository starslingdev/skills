"""Price the GitHub API work of one ci-speedup data pass before it starts.

`run.py` probes four cheap facts (workflow list, newest run, its job count and the
rate limit), then calls `estimate()` here. Every request family `collect_runs.py`
(and the `pr_lifecycle` GraphQL block it calls) can issue is listed in `FAMILIES`,
keyed by its templated route. `tests/test_request_estimate_families.py` scans both
modules for every endpoint they build and fails when one has no entry, so a new
request family cannot ship unpriced.

Each family carries three figures:

* `low`: what the pass issues on an ordinary repository: every workflow's fixed
  reads and its shallow job sample.
* `high`: `low` plus every sampling stage at its design size (the deepened run
  sample, the 100-commit PR sample, retried-run views) and each conditional
  detector read firing on ONE workflow. This is the figure the quota gate uses.
* `ceiling`: every conditional detector firing on every workflow at the
  collector's own caps. It is printed and recorded for context only; the gate
  never stops on it.

Job-log downloads use the same REST quota but are reported separately because they
are the large responses. GraphQL and search have their own quotas and are reported
on their own lines.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import collect_runs as cr
from pr_sample_contract import (
    PR_SAMPLE_CANDIDATE_PAGE_CAP,
    PR_SAMPLE_JOB_LIST_READ_CAP,
    PR_SAMPLE_TARGET,
)

# `collect_runs.py --max-runs` default. run.py never overrides it, so this is the
# per-workflow success sample the collector deepens to.
COLLECT_MAX_RUNS = 20
# `pr_lifecycle.collect` walks `range(1, 5)`: at most four GraphQL queries.
LIFECYCLE_GRAPHQL_PAGES = 4
# The ruleset list does not say how many rulesets it holds until it is read; each
# one costs a detail read. Ten is an allowance, not a collector cap.
RULESET_DETAIL_ALLOWANCE = 10
# OPT112 reads fresh and old restores of one Go cache key per drilled pole.
_OPT112_LOGS_PER_KEY = cr._OPT112_MAX_FRESH_LOGS + cr._OPT112_MAX_OLD_LOGS
# `_magnitude_sample(k_max=8)`: extra logs read per drilled pole under --with-logs.
_MAGNITUDE_SAMPLE_MAX = 8
# `_attach_cache_log_evidence(cap=4)`: sampled runs quoted per cache finding.
_CACHE_EVIDENCE_CAP = 4


@dataclass(frozen=True)
class Facts:
    """What the preflight probe learned. Every field is cheap to read."""

    workflows: int            # workflows the collector will sample
    job_pages: int            # job-list pages per run (the newest run's job count)
    repo_runs: int | None     # all-time run count: distinct per-run reads never exceed it
    self_hosted: bool         # the newest run used a self-hosted runner (OPT97 reads)
    scanner_schedules: int    # checkout workflows with a schedule and paths-ignore (OPT36)
    with_logs: bool


def _runs(f: Facts, n: int) -> int:
    """Distinct runs read for `n` planned runs: never more runs than the repo has."""
    return n if f.repo_runs is None else min(n, max(0, f.repo_runs))


def _pr_reads(f: Facts) -> int:
    """Job-list reads the 100-commit PR sample may spend (its own hard cap)."""
    return min(PR_SAMPLE_JOB_LIST_READ_CAP, PR_SAMPLE_TARGET * f.workflows * f.job_pages)


def _opt97_populations(f: Facts, every: bool) -> int:
    if f.self_hosted:
        return f.workflows if every else 1
    return 1 if every else 0


def _plain_job_reads_high(f: Facts, every: bool) -> int:
    r = COLLECT_MAX_RUNS
    per_wf = (
        r                                   # success sample, shallow then deepened
        + cr._DEFAULT_BRANCH_PUSH_SAMPLE_MAX  # default-branch push lane
        + cr._OPT43_CANCELLED_RUN_PROBE_MAX   # cancelled-run queue bounds
    )
    conditional = (
        r      # OPT21 outcome runs
        + r    # OPT35 / OPT57 failed runs
        + r    # schedule success sample (OPT36)
    )
    planned = f.workflows * (per_wf + (conditional if every else 0))
    if not every:
        planned += conditional
    return _runs(f, planned + _pr_reads(f) // max(1, f.job_pages)) * f.job_pages


def _attempt_reads(f: Facts, every: bool) -> int:
    """`filter=all` plus `filter=latest` views of retried runs."""
    n = COLLECT_MAX_RUNS * (f.workflows if every else 1)
    return 2 * _runs(f, n) * f.job_pages


def _dense_job_reads(f: Facts, every: bool) -> int:
    return _runs(f, cr._OPT97_DENSE_JOB_RUNS * _opt97_populations(f, every))


def _run_list_reads(f: Facts, every: bool) -> int:
    fixed = 3            # all-status page, 30-day volume, success fallback
    conditional = (
        1                # OPT65 event-scoped volume
        + 2              # OPT48 failure/success counts
        + 1              # OPT57 event-scoped run page
        + 3              # schedule volume, schedule runs, schedule successes
        + 2              # default-branch push volume
    )
    deeper = PR_SAMPLE_CANDIDATE_PAGE_CAP - 1  # PR sample's deeper candidate pages
    if every:
        return f.workflows * (fixed + conditional + deeper)
    return f.workflows * (fixed + 1) + conditional + deeper


def _log_reads(f: Facts, every: bool) -> int:
    w = f.workflows if every else 1
    total = (
        cr._OPT79_REPO_LOG_BUDGET                        # OPT79 cache probes (repo budget)
        + w * cr._OPT80_LOG_PROBE_MAX                    # OPT80 checkout tail runs
        + f.workflows                                    # OPT24 shard-floor representatives
        + w * _OPT112_LOGS_PER_KEY                       # OPT112 Go cache trim
        + 2 * cr._CACHE_PUSH_PROBE_MAX                   # cache distribution / path sample
        + 1                                              # default-branch push representative
        + w * COLLECT_MAX_RUNS                           # OPT100 failed gating jobs
    )
    if f.with_logs:
        poles = cr._DRILL_DISTINCT_MATRICES
        total += poles * (1 + _MAGNITUDE_SAMPLE_MAX)     # drilled pole logs + spread
        total += w * _CACHE_EVIDENCE_CAP                 # cache-finding evidence
    return total


Count = Callable[[Facts], int]


@dataclass(frozen=True)
class Family:
    route: str        # templated route: `{…}` parameters collapsed to `*`
    quota: str        # "core", "graphql" or "search"
    kind: str         # "json" or "log"
    low: Count
    high: Count
    ceiling: Count


def _const(n: int) -> Count:
    return lambda _f: n


def _wf(n: int) -> Count:
    return lambda f: n * f.workflows


FAMILIES: tuple[Family, ...] = (
    Family("repos/*", "core", "json", _const(1), _const(1), _const(1)),
    Family("repos/*/actions/workflows", "core", "json",
           lambda f: math.ceil(f.workflows / 100), lambda f: math.ceil(f.workflows / 100),
           lambda f: math.ceil(f.workflows / 100)),
    Family("repos/*/rulesets", "core", "json", _const(1), _const(1), _const(1)),
    Family("repos/*/rulesets/*", "core", "json", _const(0),
           _const(RULESET_DETAIL_ALLOWANCE), _const(RULESET_DETAIL_ALLOWANCE)),
    Family("repos/*/branches/*/protection/required_status_checks", "core", "json",
           _const(0), _const(1), _const(1)),
    Family("repos/*/actions/cache/usage", "core", "json", _const(0), _const(1), _const(1)),
    Family("repos/*/actions/runners", "core", "json", _const(0), _const(1), _const(1)),
    Family("repos/*/actions/caches", "core", "json", _const(0), _const(1),
           _const(cr._DRILL_DISTINCT_MATRICES)),
    Family("repos/*/actions/workflows/*/runs", "core", "json", _wf(2),
           lambda f: _run_list_reads(f, False), lambda f: _run_list_reads(f, True)),
    # Config-era boundary: one last-change lookup per workflow with >= 2 runs.
    Family("repos/*/commits", "core", "json", _const(0), _wf(1), _wf(1)),
    # Workflow blobs at the two era commits plus one per sampled head, the
    # contents-API fallback for a workflow the checkout cannot serve, and OPT79's
    # package.json read.
    Family("repos/*/contents/*", "core", "json", _const(0),
           lambda f: 2 + COLLECT_MAX_RUNS + 1,
           lambda f: f.workflows * (2 + COLLECT_MAX_RUNS + 1) + 1),
    Family("repos/*/actions/runs/*/jobs", "core", "json",
           lambda f: _runs(f, f.workflows * cr._SHALLOW_RUNS) * f.job_pages,
           lambda f: (_plain_job_reads_high(f, False) + _attempt_reads(f, False)
                      + _dense_job_reads(f, False)),
           lambda f: (_plain_job_reads_high(f, True) + _attempt_reads(f, True)
                      + _dense_job_reads(f, True))),
    # OPT97's dense window read: up to three run pages per measured population.
    Family("repos/*/actions/runs", "core", "json", _const(0),
           lambda f: cr._OPT97_DENSE_RUN_PAGES * _opt97_populations(f, False),
           lambda f: cr._OPT97_DENSE_RUN_PAGES * _opt97_populations(f, True)),
    # One check-run page per sampled PR commit.
    Family("repos/*/commits/*/check-runs", "core", "json", _const(0),
           lambda f: _runs(f, PR_SAMPLE_TARGET), lambda f: _runs(f, PR_SAMPLE_TARGET)),
    # OPT32 / cache-guard changed files: one page per sampled PR (a PR touching
    # over 100 files costs up to `_OPT32_CHANGED_FILES_MAX_PAGES`).
    Family("repos/*/pulls/*/files", "core", "json", _const(0),
           _const(COLLECT_MAX_RUNS),
           lambda f: min(PR_SAMPLE_TARGET, f.workflows * COLLECT_MAX_RUNS)),
    # OPT39 exact changed files per sampled PR run; OPT36 consecutive schedule pairs.
    Family("repos/*/compare/*", "core", "json", _const(0),
           lambda f: COLLECT_MAX_RUNS + min(1, f.scanner_schedules) * (cr._COST_RUNLIST_MAX - 1),
           lambda f: (f.workflows * COLLECT_MAX_RUNS
                      + f.scanner_schedules * (cr._COST_RUNLIST_MAX - 1))),
    # OPT100 annotations for a failed gating job with no failed step to read.
    Family("repos/*/check-runs/*/annotations", "core", "json", _const(0),
           _const(COLLECT_MAX_RUNS), _wf(COLLECT_MAX_RUNS)),
    # OPT98: merged-PR timelines, only above 30 merges a day.
    Family("repos/*/issues/*/timeline", "core", "json", _const(0),
           _const(cr._OPT98_CHURN_SAMPLE), _const(cr._OPT98_CHURN_SAMPLE)),
    Family("repos/*/actions/jobs/*/logs", "core", "log", _const(0),
           lambda f: _log_reads(f, False), lambda f: _log_reads(f, True)),
    Family("search/issues", "search", "json", _const(0), _const(1), _const(1)),
    Family("graphql", "graphql", "json", _const(1),
           _const(LIFECYCLE_GRAPHQL_PAGES), _const(LIFECYCLE_GRAPHQL_PAGES)),
)


@dataclass(frozen=True)
class Totals:
    json_low: int
    json_high: int
    json_ceiling: int
    log_low: int
    log_high: int
    log_ceiling: int
    graphql_high: int
    search_high: int
    graphql_low: int = 0
    search_low: int = 0


def estimate(facts: Facts) -> Totals:
    """Sum every family's low / high / ceiling by quota and response kind."""
    sums: dict[tuple[str, str, str], int] = {}
    for fam in FAMILIES:
        for tier, fn in (("low", fam.low), ("high", fam.high), ("ceiling", fam.ceiling)):
            key = (fam.quota, fam.kind, tier)
            sums[key] = sums.get(key, 0) + max(0, int(fn(facts)))
    get = lambda q, k, t: sums.get((q, k, t), 0)  # noqa: E731
    return Totals(
        json_low=get("core", "json", "low"), json_high=get("core", "json", "high"),
        json_ceiling=get("core", "json", "ceiling"),
        log_low=get("core", "log", "low"), log_high=get("core", "log", "high"),
        log_ceiling=get("core", "log", "ceiling"),
        graphql_low=get("graphql", "json", "low"), graphql_high=get("graphql", "json", "high"),
        search_low=get("search", "json", "low"), search_high=get("search", "json", "high"),
    )


def checkout_workflows(root: Path | None) -> dict[str, str] | None:
    """`.github/workflows/<file>` path -> text from the checkout, None when unreadable."""
    if root is None:
        return None
    wf_dir = Path(root) / ".github" / "workflows"
    try:
        files = [p for p in wf_dir.iterdir()
                 if p.is_file() and p.suffix in (".yml", ".yaml")]
    except OSError:
        return None
    out: dict[str, str] = {}
    for p in files:
        try:
            out[f".github/workflows/{p.name}"] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out[f".github/workflows/{p.name}"] = ""
    return out


def facts_from_probe(workflows: dict, runs: dict, jobs: dict, *, root: Path | None,
                     with_logs: bool) -> Facts:
    """Turn the probe's three JSON payloads (plus the checkout) into `Facts`.

    The collector samples the workflows the checkout's scan names, so the count is
    the listed workflows present in the checkout; without a readable checkout it is
    every active listed workflow."""
    listed = workflows.get("workflows") if isinstance(workflows.get("workflows"), list) else []
    local = checkout_workflows(root)
    paths = {str(w.get("path") or "") for w in listed if isinstance(w, dict)}
    if local is not None and paths:
        count = len(paths & set(local))
    elif listed:
        count = sum(1 for w in listed
                    if isinstance(w, dict) and str(w.get("state") or "active") == "active")
    else:
        count = int(workflows.get("total_count") or 0)
    # The probe reads one 100-workflow page; the collector reads them all. Count
    # the workflows past that page as sampled too.
    total = workflows.get("total_count")
    if listed and isinstance(total, int) and total > len(listed):
        count += total - len(listed)
    job_rows = jobs.get("jobs") if isinstance(jobs.get("jobs"), list) else []
    job_count = int(jobs.get("total_count") or len(job_rows) or 0)
    total_runs = runs.get("total_count")
    self_hosted = any(
        "self-hosted" in {str(x).lower() for x in (j.get("labels") or [])}
        for j in job_rows if isinstance(j, dict))
    scanner_schedules = sum(
        1 for text in (local or {}).values()
        if "schedule" in text and "paths-ignore" in text)
    return Facts(
        workflows=max(1, count),
        job_pages=max(1, math.ceil(max(1, job_count) / 100)),
        repo_runs=int(total_runs) if isinstance(total_runs, int) and not isinstance(
            total_runs, bool) else None,
        self_hosted=self_hosted,
        scanner_schedules=scanner_schedules,
        with_logs=with_logs,
    )

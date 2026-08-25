#!/usr/bin/env python3
"""ci-speedup gh-data sizer.

Reads the static-scan findings JSON, samples GitHub Actions run history via
the gh API, and attaches measured wall-clock + runner-minute deltas to every
finding using the critical-path / cluster-floor model from
`references/wall-clock-methodology.md`.

Frugal gh strategy:
  - one `workflows` list per repo
  - one `total_count` query per workflow (30-day volume — Term 0)
  - one `jobs` listing per sampled successful run (default: 8)
  - no per-step API calls; step timings come from the job JSON
  - atomic-write the output JSON (`.partial` → rename)

CLI:
    collect_runs.py --in <findings.json> --out <out.json> --repo <owner/name>
                    [--max-runs N]
"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as _dt
import fnmatch
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Module logger — inherits the entry point's logging config (INFO by default,
# DEBUG under STARSLING_LOG_LEVEL). Used for diagnosable-but-non-fatal gh paths
# (e.g. an expected-absent rulesets endpoint) so a real collection failure is
# traceable without spamming the default run.
logger = logging.getLogger(__name__)

# The wall-clock lever model (critical-path / cross-workflow bound cascade)
# lives in its own leaf module so the bounds are unit-testable in isolation.
# Re-imported here so existing call sites + test imports resolve unchanged.
from wall_clock import (  # noqa: E402
    PrPopulation,
    WallClockContext,
    _cap_wall_clock,
    _cap_wall_clock_cross_workflow,
    _concurrent_workflows,
    _resolve_job_p50,
    _wf_basename,
    credit_detrigger,
    credit_shared_substep,
    size_wall_clock,
)


# =============================================================================
# gh client — minimal wrapper around the gh CLI
# =============================================================================

# Filesystem-safe chars a fixture filename may contain verbatim; everything
# else in the endpoint string collapses to `_`. Kept as a plain set (no `re`
# import) so this helper — the whole surface of the replay seam outside the
# class — stays a trivial, auditable one-off.
_FIXTURE_SAFE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)


# The one pair of REAL colliding endpoints the pipeline actually issues: a monthly-
# volume window (`created=>=X`) and a pinned sampling window (`created=<=X`) differ
# only in an operator whose chars are both unsafe, so both used to collapse to
# `…_created___X` — opposite windows, one filename. Spell the operators out BEFORE the
# safe-char pass so they survive it. Order matters: `>=`/`<=` before the bare forms.
_FIXTURE_OPERATOR_WORDS = ((">=", "gte"), ("<=", "lte"), (">", "gt"), ("<", "lt"))


def _fixture_name(endpoint: str, ext: str) -> str:
    """Deterministic, filesystem-safe fixture filename for a gh `endpoint`
    string, shared by replay mode (`GhClient` reads it from
    `CI_SPEEDUP_GH_FIXTURES`) and record mode (`GhClient` writes it to
    `CI_SPEEDUP_GH_RECORD`) — the two must agree on the mapping or a
    recording never replays. Comparison operators are spelled out (`>=` → `gte`),
    then every remaining char outside `[A-Za-z0-9._-]` becomes `_`; endpoints whose
    safe form exceeds 200 chars are truncated to 200 and suffixed with an 8-char
    sha256 hash of the FULL endpoint (so two long `contents/` paths that differ only
    past char 200 stay distinct).

    Still LOSSY in principle: the char→`_` mapping is not injective, so two DISTINCT
    short endpoints could in theory map to one filename, and the sha8 disambiguation
    only fires on the >200-char branch. The one collision the pipeline ACTUALLY
    issued (the `created=>=` / `created=<=` windows above) is now spelled out of
    existence, and `GhClient._record` RAISES on any collision it does see rather than
    overwriting — because last-writer-wins there means replay serves one endpoint's
    body under another's name, i.e. valid-but-WRONG JSON (a `{"default_branch":…}`
    body answering a check-runs request reads back as "this commit ran no checks").
    Do not ASSUME distinct endpoints yield distinct filenames; rely on the guard."""
    endpoint_safe = endpoint
    for op, word in _FIXTURE_OPERATOR_WORDS:
        endpoint_safe = endpoint_safe.replace(op, word)
    safe = "".join(c if c in _FIXTURE_SAFE_CHARS else "_" for c in endpoint_safe)
    if len(safe) > 200:
        safe = safe[:200] + hashlib.sha256(endpoint.encode()).hexdigest()[:8]
    return f"{safe}.{ext}"


# =============================================================================
# Fetch concurrency — ONE process-wide pool + ONE token-wide rate governor
# =============================================================================
#
# Every gh call this module makes is an idempotent, latency-bound GET. They are
# issued through ONE shared, bounded pool (`_fetch_pool`) rather than a pool per
# call site: a pool per workflow spends most of its life partly-idle (an 8-wide
# pool over 10 runs = one full wave + a 25%-utilised tail, then teardown), and N
# live pools can put N × width calls in flight at once. One shared executor makes
# the in-flight ceiling exactly `_FETCH_CONCURRENCY`, everywhere, always.
#
# WIDTH 12 — the measured-safe default, raised from the original 8 (see the block below for
# the before/after and why 12). It is env-overridable via `CI_SPEEDUP_FETCH_CONCURRENCY`.
#
# The re-orchestration to ONE shared pool was itself peak-neutral: 8 was exactly the width
# this module already used at its three pre-existing pooled call sites, so consolidating them
# did not raise peak in-flight. Raising the DEFAULT to 12 does raise it — peak is now 12, up
# from 8 (and the raw-log prefetch buffer scales with it; see `_TEXT_WINDOW`). Peak
# concurrency is NOT the whole rate-limit story, though (see `_REST_RATE_PER_MIN`): finishing
# the same ~700 calls faster also raises the SUSTAINED request rate, and GitHub's binding
# secondary limit is a rate. The governor below is what makes that rate safe; the width is
# what bounds the blast radius when it isn't.
#
# The precondition for a wider pool — rate-limit DETECTION + BACKOFF — has now LANDED (the
# main merge). Every live call funnels through `GhClient._invoke`, which classifies a
# secondary-limit 403/429, honours the server's `retry-after` / `x-ratelimit-reset`, retries
# with jittered backoff, pauses every worker (`_record_block`), and trips a global breaker
# on a sustained block. A lost 403 is no longer silent data loss, and every live attempt
# (including `_invoke` retries and `_paginate`'s page 2+) is paced by the governor below.
#
# DEFAULT WIDTH = 12, up from the original 8, with the before/after the earlier comment
# here asked for. The pass is latency-bound (each `gh api` call is a fresh subprocess +
# TLS handshake, ~0.5s), so more in-flight calls is a near-linear wall-clock win. Measured
# on gravitational/teleport (541 sampled runs — the largest CI history in the dogfood set,
# and the WORST case for the secondary rate limit): 8 → 151s, 12 → 120s (−21%), 16 → 110s
# (−27%), all with ZERO rate-limit blocks and identical findings. Smaller repos see −30-41%
# at width 12-16 (mid-size 6-repo A/B). 12 is the DEFAULT because it clears the worst case
# with margin: width 20 on that same repo DID trip GitHub's secondary limit (2x SLOWER +
# half the findings dropped) — the governor below assumes 1 point/request, but the
# `actions/*` endpoints cost more, so a wide-enough burst outruns it. 12 stays comfortably
# under that cliff.
#
# The knob is env-overridable (`CI_SPEEDUP_FETCH_CONCURRENCY`) for operators on a DEDICATED
# token who want to push toward 16 — but note ~20+ risks the secondary-limit trip on very
# large repos. That trip is no longer silent: an up-front block renders a loud
# `collection_failed` banner (see the `diagnose_unavailability` fix), so an over-eager
# override fails visibly rather than shipping a quiet half-audit.
_FETCH_CONCURRENCY_DEFAULT = 12
# GitHub's hard secondary limit is "no more than 100 concurrent requests" (see the block
# below), so a width above 100 can never help and only guarantees the trip — clamp there.
_FETCH_CONCURRENCY_MAX = 100


def _resolve_fetch_concurrency() -> int:
    """Resolve the pool width from `CI_SPEEDUP_FETCH_CONCURRENCY`, defensively.

    The override is operator-supplied, so it must never crash the whole pass: a non-integer
    value (a typo) would otherwise raise at MODULE IMPORT and take the entire skill down, and
    a value < 1 would blow up later at `ThreadPoolExecutor(max_workers=...)` mid-run. Both
    degrade to the safe default instead, with a warning. The width is also clamped to
    [1, 100] — 100 is GitHub's documented concurrent-request ceiling, above which a wider
    pool cannot help and only guarantees the secondary-limit trip."""
    raw = os.environ.get("CI_SPEEDUP_FETCH_CONCURRENCY")
    if not raw:
        return _FETCH_CONCURRENCY_DEFAULT
    try:
        width = int(raw)
    except ValueError:
        logger.warning("CI_SPEEDUP_FETCH_CONCURRENCY=%r is not an integer; using default %d",
                       raw, _FETCH_CONCURRENCY_DEFAULT)
        return _FETCH_CONCURRENCY_DEFAULT
    clamped = max(1, min(width, _FETCH_CONCURRENCY_MAX))
    if clamped != width:
        logger.warning("CI_SPEEDUP_FETCH_CONCURRENCY=%d out of range [1, %d]; clamped to %d",
                       width, _FETCH_CONCURRENCY_MAX, clamped)
    return clamped


_FETCH_CONCURRENCY = _resolve_fetch_concurrency()

# PEAK-MEMORY bound on the raw-log (text) prefetch — see `GhClient.prefetch_text`.
#
# Job logs are the heaviest responses in the pass (multi-MB each). A flat `pool.map` over
# the whole log plan holds EVERY planned log live at once (O(plan) — hundreds of MB to GB
# on a large repo); the serial path it replaced held exactly one. So the log plan is drained
# through a window: at most `_TEXT_WINDOW` logs parked at any moment, refilled once the
# parked count drops to `_TEXT_LOW_WATER`.
#
# The low-water mark is what keeps the refills WAVES rather than a serial trickle: refilling
# on every single pop would issue batches of one. At the default width 12, 24/12 means each
# refill is a 12-endpoint batch — a full-width wave — with one wave's worth of results
# already in hand. Both scale with `_FETCH_CONCURRENCY`, so a wider pool raises the peak
# raw-log (multi-MB each) buffer proportionally: width 12 parks up to 24 logs live (vs 16 at
# the old width 8), a ~50% higher peak-memory ceiling that a raised override scales further.
_TEXT_WINDOW = 2 * _FETCH_CONCURRENCY      # 24 at the default width 12
_TEXT_LOW_WATER = _FETCH_CONCURRENCY       # 12 at the default width 12

# ---------------------------------------------------------------------------
# WHAT GITHUB ACTUALLY LIMITS (read this before touching the two constants below)
# ---------------------------------------------------------------------------
#
# From "Rate limits for the REST API" → Secondary rate limits, verbatim:
#
#   "No more than 100 concurrent requests are allowed. This limit is shared across the
#    REST API and GraphQL API."
#   "No more than 900 points per minute are allowed for REST API endpoints, and no more
#    than 2,000 points per minute are allowed for the GraphQL API endpoint."
#   "In general, no more than 80 content-generating requests per minute [...] are allowed."
#
# The 900 is an **AGGREGATE** budget for the token across the whole REST API — NOT a
# per-route allowance. The giveaway is the sentence's own parallel structure: the GraphQL
# half scores 2,000/min against "the GraphQL API endpoint", which is a SINGLE route. There
# is no per-route allowance anywhere in GitHub's rate-limit documentation; the only other
# secondary limits are the 100-concurrent and 80-content-generating ones, both per-token.
#
# An earlier version of this module keyed the token bucket PER ROUTE and cited "900 points
# per minute per REST route". That was a misreading, and it made the governor close to
# inert: N routes × one bucket each = N × the budget admissible in aggregate, and a route
# whose key was never collapsed (a per-SHA check-runs read, a per-file `contents` read) got
# a fresh full bucket on every call — i.e. no pacing at all. The bucket is now GLOBAL: one
# bucket for the process, which is the thing GitHub scores.
#
# Every call this module issues is a GET = 1 point, so points == requests here.

# SUSTAINED pace for the WHOLE REST API on this token, in requests/minute.
#
# 600/min is 67% of the documented 900. The remaining third is not slack for its own sake:
#   * the budget is the TOKEN's, and the user's `gh` token is usually also driving their
#     shell, their editor, and any other tooling in the session — we do not get all 900;
#   * "Some REST API endpoints have a different point cost that is not shared publicly"
#     (GitHub's words), so a request is a LOWER BOUND on its point cost, not a known one;
#   * a 403 still COSTS: `_invoke`'s backoff/`retry-after` recovery (see `_FETCH_CONCURRENCY`)
#     turns it into a slower run rather than lost data, but an exhausted retry budget is lost
#     data again, and every block pauses the whole pool. Riding at 94% of the ceiling to lean
#     on that recovery path is not a tradeoff worth making for wall-clock we do not measurably
#     gain — see below.
#
# It costs nothing measurable: the pass is latency-bound at width 8 (measured ~534 calls/min
# on `better-auth/better-auth`), so a 600/min sustained cap does not bind on a real repo.
# It binds exactly where it should — a large repo whose responses come back fast enough for
# the pool to outrun the budget.
_REST_RATE_PER_MIN = 600

# BURST allowance, in requests — the token bucket's CAPACITY, deliberately decoupled from
# the refill rate.
#
# A token bucket admits, in any 60s window, at most `capacity + rate × 60` requests. So a
# bucket whose capacity IS the per-minute budget admits TWICE the budget in the first
# minute of a cold start — which is the exact shape of a large repo's job-listing wave and
# the exact failure the governor exists to prevent. Capacity is therefore its own knob:
#
#     worst case in any 60s window = _REST_BURST + _REST_RATE_PER_MIN
#                                  =         100 +                600  =  700  <  900 ✅
#
# 100 requests of cold-start burst is still ~12 full waves at width 8, so a small repo
# (whose whole pass is a few hundred latency-bound calls) never notices the governor; a
# large one is paced from the first second instead of the 44th.
_REST_BURST = 100

_ROUTE_ID_RE = re.compile(r"\d+")
# A commit SHA / tree-ish in a path segment: hex, 7-40 chars. `abc` (3) and a workflow
# FILE NAME (`ci.yml` — has a dot) are correctly excluded; `deadbeef` is not a route.
_ROUTE_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def _route_key(endpoint: str) -> str:
    """A TEMPLATED-ROUTE LABEL for an endpoint — `repos/*/*/actions/runs/*/jobs` — used
    for DEBUG observability ("which family is the governor pausing on?"), NOT for
    admission control.

    ADMISSION IS GLOBAL (`_RestRateLimiter` holds ONE bucket): GitHub's 900 points/min is
    an aggregate budget for the token across the REST API, so a per-route bucket would
    admit `N_routes × the budget` and is not a partition of anything GitHub scores. It is
    emphatically NOT "more conservative" to key more narrowly — more buckets means MORE
    admissions, which is precisely how the earlier per-route version of this governor came
    to pace almost nothing.

    The template collapses every part of the path that is a PARAMETER rather than a route:

      * owner and repo (`repos/{owner}/{repo}/…`) — a process auditing N repos is hitting
        ONE route as far as GitHub is concerned;
      * numeric ids (run / job / workflow);
      * SHA-shaped hex segments — `commits/{sha}/check-runs` is one route, not one per
        commit;
      * everything under `/contents/` — `contents/.github/workflows/ci.yml` and
        `contents/.github/workflows/release.yml` are one route, not one per file (and the
        path can contain slashes, so it collapses to a single `*`).
    """
    segs = endpoint.split("?", 1)[0].split("/")
    out: list[str] = []
    i = 0
    while i < len(segs):
        seg = segs[i]
        if seg == "repos" and i + 2 < len(segs):
            out.extend(["repos", "*", "*"])       # {owner}/{repo} are params, not routes
            i += 3
            continue
        if seg == "contents":
            out.extend(["contents", "*"])         # {path} may contain slashes — one param
            break
        out.append("*" if (_ROUTE_ID_RE.fullmatch(seg) or _ROUTE_SHA_RE.fullmatch(seg))
                   else seg)
        i += 1
    return "/".join(out)


class _RestRateLimiter:
    """ONE token bucket for the whole REST API on this token, pacing live gh calls at
    `_REST_RATE_PER_MIN` (see the constants above for why the bucket is global, and what
    GitHub's documentation actually says).

    The bucket holds up to `burst` tokens (`_REST_BURST`) and refills at `per_min / 60`
    tokens/sec. `acquire(route)` takes one token, blocking exactly long enough for the next
    token to accrue when the bucket is dry. `route` is a LABEL — it appears in the DEBUG
    line and nowhere else; it does not select a bucket. The limiter is shared across the
    fetch pool's threads, so it paces the process's AGGREGATE request rate, not per thread.

    CAPACITY IS NOT THE PER-MINUTE BUDGET (see `_REST_BURST`). The most the bucket can
    admit in any 60s window is `burst + per_min`, so setting capacity = per_min would admit
    2× the documented budget from a cold start — the governor would only start pacing in
    the *second* minute of sustained load, and GitHub's secondary limit is precisely
    burst-sensitive. 100 + 600 = 700 < 900 keeps the worst case inside the budget.

    `now`/`sleep` are injected so the pacing is unit-testable against a fake clock
    (no real sleeping in tests)."""

    def __init__(self, per_min: int = _REST_RATE_PER_MIN,
                 burst: int = _REST_BURST,
                 now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._capacity = float(burst)
        self._rate = per_min / 60.0          # tokens per second
        self._now = now
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = float(burst)
        self._last = now()

    # Floating-point slack on the "is a whole token available?" test. `wait` is computed as
    # exactly the time for the missing fraction of a token to accrue, but
    # `tokens + wait * rate` re-rounds to a hair UNDER 1.0 — so an exact `>= 1.0` can loop
    # forever, sleeping ever-tinier slivers and never admitting. One nanosecond of a token
    # is not a rate-limit risk; an infinite loop in the fetch path is.
    _TOKEN_EPS = 1e-9

    def acquire(self, route: str = "") -> None:
        while True:
            with self._lock:
                now = self._now()
                tokens = min(self._capacity,
                             self._tokens + max(0.0, now - self._last) * self._rate)
                self._last = now
                if tokens >= 1.0 - self._TOKEN_EPS:
                    self._tokens = max(0.0, tokens - 1.0)
                    return
                self._tokens = tokens
                wait = (1.0 - tokens) / self._rate
            logger.debug("rate governor: REST budget dry (next call on route %s) — "
                         "pausing %.2fs", route or "?", wait)
            self._sleep(wait)


_POOL_LOCK = threading.Lock()
_FETCH_POOL: ThreadPoolExecutor | None = None


def _fetch_pool() -> ThreadPoolExecutor:
    """The one process-wide fetch executor (lazily created, never shut down —
    interpreter exit joins its threads). Every concurrent gh site maps over THIS pool,
    so at most `_FETCH_CONCURRENCY` gh calls are ever in flight for the whole run, no
    matter how many fan-out sites are active."""
    global _FETCH_POOL
    with _POOL_LOCK:
        if _FETCH_POOL is None:
            _FETCH_POOL = ThreadPoolExecutor(max_workers=_FETCH_CONCURRENCY,
                                             thread_name_prefix="gh-fetch")
        return _FETCH_POOL


# Sentinel for "not in the prefetch buffer" (None is a legitimate buffered value —
# a failed fetch — and must not be confused with a miss).
_NO_PREFETCH = object()


_NO_BUFFER_WARNED: set[str] = set()


def _note_no_buffer(client: Any, method: str) -> None:
    """Say ONCE, per client class + method, that this client has no prefetch buffer.

    The duck-typed lookup below is deliberately forgiving (see `_prefetch_json`), but a
    silent `getattr` miss is also exactly what a RENAME looks like: every prefetch wave in
    the run would quietly revert to serial, the drift guard (`drain_prefetch`) would
    disable itself, and nothing anywhere would say so. Naming the class once at DEBUG makes
    "the buffer wasn't there" a fact you can read in a log rather than infer from a
    stopwatch."""
    key = f"{type(client).__module__}.{type(client).__name__}.{method}"
    if key in _NO_BUFFER_WARNED:
        return
    _NO_BUFFER_WARNED.add(key)
    logger.debug("prefetch: client %s has no %s() — this run's fetches for that seam "
                 "are issued serially at their call sites", type(client).__name__, method)


def _prefetch_json(client: Any, endpoints: Any, *, allow_missing: bool = False) -> None:
    """Warm `client`'s prefetch buffer, if it HAS one.

    Prefetch is a pure accelerator: it changes when a call is issued, never which call
    or what it returns. So it is an OPTIONAL part of the client contract — a duck-typed
    client that only implements `json()`/`text()` (the detectors' test doubles, and any
    caller wiring in its own client) simply fetches serially at the original call site,
    which is exactly the behaviour that existed before this seam. Never raise here: a
    client without a buffer is a slower run, not a broken one — but it IS logged once
    (`_note_no_buffer`), so a rename can't silently turn the whole pass serial."""
    fn = getattr(client, "prefetch_json", None)
    if fn is None:
        _note_no_buffer(client, "prefetch_json")
        return
    fn(endpoints, allow_missing=allow_missing)


def _prefetch_text(client: Any, endpoints: Any) -> None:
    """`_prefetch_json` for the raw-text (job-log) endpoints."""
    fn = getattr(client, "prefetch_text", None)
    if fn is None:
        _note_no_buffer(client, "prefetch_text")
        return
    fn(endpoints)


# --- failure classification + retry policy ------------------------------------
#
# Every live gh call funnels through `GhClient._invoke`, so the retry/backoff
# policy is stated ONCE here rather than per call site. The failure mode this
# exists to kill: a GitHub secondary-rate-limit block used to be indistinguishable
# from a 404 — both were a `returncode != 0` logged at DEBUG (invisible at the
# default INFO level) that returned `None`. A rate-limited run therefore SHRANK
# its own sample and still rendered a confident, plausible, WRONG report. A
# truncated sample presented as complete is a false negative; it must be loud.

# Attempts per endpoint (the first try plus retries) for a RETRYABLE failure.
_GH_MAX_ATTEMPTS = 3
# GitHub's documented floor for a secondary-rate-limit block: "wait at least one
# minute before retrying". Also the FLOOR under any header-derived rate-limit wait:
# an `x-ratelimit-reset` describes the PRIMARY hourly bucket, so a secondary block
# discovered seconds before that bucket rolls over would otherwise back off ~3s,
# retry into the same block, and burn the whole attempt budget in a few seconds.
_GH_RATE_LIMIT_FLOOR_S = 60.0
# Base for the transient (5xx / timeout) backoff: 1s / 2s / 4s, plus jitter.
_GH_TRANSIENT_BASE_S = 1.0
# Random jitter added to EVERY backoff (both paths). Without it, N concurrent
# workers blocked at the same instant compute the identical wait, wake together,
# and fire N simultaneous requests straight back into the limit — the classic
# thundering herd, which burns the attempt budget in lockstep.
_GH_JITTER_S = 5.0
# Ceiling on any single sleep. A primary-limit `x-ratelimit-reset` can be an hour
# out; blocking a whole audit on that is worse than giving up and surfacing a loud
# coverage gap, so we cap the wait and let the attempt budget run out.
_GH_MAX_BACKOFF_S = 300.0
# --- the GLOBAL circuit breaker -----------------------------------------------
# The attempt budget above is PER CALL. That alone makes a sustained OUTAGE (a
# primary 5000/hr bucket exhausted mid-audit with its reset 40 minutes out; a
# GitHub 5xx incident; an API that accepts the connection and never answers)
# unbounded in aggregate: every remaining call burns its attempts, gives up — and
# the NEXT call starts fresh at attempt 1. Several hundred remaining calls is HOURS
# of wall-clock with no terminal condition: a fast wrong answer traded for a hang,
# which is its own product failure.
#
# So the "better to fail loudly than hang" rule is applied GLOBALLY, at the client:
# once either budget below is spent, the client is terminally `gave_up`, every later
# call short-circuits to None WITHOUT sleeping or spawning a subprocess, and
# `collect()` aborts through the same disclosed-coverage-gap path the workflow-list
# failure uses.
#
# The breaker counts EVERY retryable exhaustion — rate limit, 5xx, AND timeout —
# not just rate limits. An earlier revision fed it only rate-limit give-ups, which
# left the worst case completely unbounded: a hung API times out each attempt at
# `timeout` seconds (60 for json, 90 for text), so ONE endpoint costs 3 minutes and
# a several-hundred-call audit is still the multi-hour hang the breaker exists to
# kill. A timeout means the same thing a 5xx does — "the data exists; GitHub isn't
# giving it to us" — and a sustained one has to end the run just as loudly.
#
# Consecutive exhaustions (each already cost `_GH_MAX_ATTEMPTS` tries and their
# backoffs) before the breaker trips. Two is enough evidence that the failure is
# sustained rather than a one-off burst we out-waited; a single success resets the
# count, so a healthy audit with two unlucky calls spread across it never trips.
_GH_MAX_GIVEUPS = 2
# Cumulative seconds this client may spend SLEEPING on backoff (rate-limit or
# transient) across the WHOLE run. An audit that has already burned this much
# waiting is not going to finish inside a useful wall-clock; abort with a disclosed
# gap instead.
_GH_TOTAL_BACKOFF_BUDGET_S = 900.0
# Pages a single paginated endpoint may walk before we stop and shout. 100
# items/page × 50 pages far exceeds any real run's job count or commit's check
# count; a walk that long means the stop condition is broken, and spinning against
# the API forever is worse than a loud gap.
_GH_MAX_PAGES = 50

_HTTP_STATUS_RE = re.compile(r"http\D{0,3}(\d{3})", re.IGNORECASE)
# The status line `gh api -i` prints as the first stdout line: `HTTP/2.0 403 Forbidden`.
_HTTP_STATUS_LINE_RE = re.compile(r"^HTTP/[\d.]+\s+(\d{3})", re.IGNORECASE)


def _http_status_from_stderr(stderr: str) -> int | None:
    """The HTTP status gh reported, parsed out of its stderr line (gh writes
    e.g. `gh: Not Found (HTTP 404)` or `HTTP 403: ... (https://api.github.com/…)`).
    None when the message carries no recognisable status (gh not installed, a
    network error, an auth failure)."""
    m = _HTTP_STATUS_RE.search(stderr or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:                                          # pragma: no cover
        return None


# The blank-line terminators that can end an HTTP header block. `subprocess.run(
# text=True)` applies universal-newline translation, so a live gh response reaches us
# with `\n` only — the `\r`-bearing forms exist for a stub/pipe that hands us raw bytes.
_HEADER_TERMINATORS = ("\n\n", "\n\r\n", "\r\n\r\n", "\r\n\n")


def _split_header_block(rest: str) -> tuple[str, str] | None:
    """Split the post-status-line stdout at the FIRST blank line — the real HTTP
    framing boundary — into (header block, body). None when there is NO blank line
    at all, i.e. the header block is unterminated and the split cannot be trusted.

    Scans only far enough to find the terminator (a `find`, not a full line-split of
    the payload): a job log is multi-MB, and materializing every one of its lines to
    locate a boundary that lives in the first ~1KB is O(n) work and a transient copy
    of the whole body on every fetch."""
    if rest.startswith("\n"):                   # blank line immediately: no headers
        return "", rest[1:]
    if rest.startswith("\r\n"):
        return "", rest[2:]
    best: tuple[int, int] | None = None         # (index, terminator length)
    for term in _HEADER_TERMINATORS:
        i = rest.find(term)
        if i == -1:
            continue
        if best is None or i < best[0] or (i == best[0] and len(term) > best[1]):
            best = (i, len(term))
    if best is None:
        return None
    i, n = best
    return rest[:i], rest[i + n:]


def _split_headers_body(stdout: str) -> tuple[int | None, dict[str, str], str]:
    """Split the stdout of `gh api -i` into (status, headers, body).

    Every live call runs with `-i` (see `GhClient._invoke`), which prints the
    response's status line + headers, a blank line, then the body — on SUCCESS and
    on FAILURE alike. Verified against live endpoints on both paths: a 404 puts the
    header block (including `x-ratelimit-reset`) on stdout while gh's message goes to
    stderr, and the job-log SUCCESS path (`/jobs/{id}/logs` → 302 → blob storage)
    yields exactly ONE header block — Go's HTTP client follows the redirect and prints
    only the final response — so every log byte crosses this seam intact. One request
    therefore yields both the body AND the server's own retry guidance; the
    alternative (a second `gh api -i` probe fired at the endpoint that just
    rate-limited us, from every blocked worker) is exactly what GitHub's
    secondary-limit guidance tells you not to do.

    The split point is the first blank line AFTER the status line, so a body that
    happens to contain `\\n\\nKey: value` cannot move it.

    Two degrade-to-raw cases return `(None, {}, stdout)` — the ENTIRE stdout, unharmed,
    so a malformed split can never silently truncate (or empty) a job log:
      - no status line at all (never seen from gh; a stub/pipe could produce it);
      - a status line + headers with NO terminating blank line. This one used to fall
        out of the loop with `body = ""` and hand back an EMPTY body on a SUCCESSFUL
        (`rc == 0`) call — a job log silently reading as empty, which `_persist_pole_logs`
        would take as a fetched-but-empty log rather than a gap: precisely the
        silent-drop class this seam exists to kill.

    NOTE the body is not byte-for-byte identical to the wire: `subprocess.run(text=True)`
    applies universal-newline translation upstream of us, so a lone `\\r` in a job log
    (a progress bar) is already an `\\n` by the time we see it. That is pre-existing and
    orthogonal to the split; what this function guarantees is that it neither drops nor
    reorders what `text=True` handed it."""
    if not stdout:
        return None, {}, ""
    first, sep, rest = stdout.partition("\n")
    m = _HTTP_STATUS_LINE_RE.match(first.strip())
    if not sep or not m:
        return None, {}, stdout
    split = _split_header_block(rest)
    if split is None:                           # unterminated header block
        return None, {}, stdout
    head, body = split
    status = int(m.group(1))
    headers: dict[str, str] = {}
    for line in head.splitlines():
        key, kv_sep, value = line.partition(":")
        if kv_sep:
            headers[key.strip().lower()] = value.strip()
    return status, headers, body


def _classify_gh_failure(stderr: str, status: int | None = None,
                         headers: dict[str, str] | None = None) -> str:
    """Bucket a failed `gh api` call by what the failure MEANS, which is what
    decides whether we retry and whether it counts as a coverage gap:

      `rate_limit`   — 429, or ANY message that says rate limit / abuse detection.
                       RETRYABLE with backoff; if it never succeeds it is a REAL
                       coverage gap (the data exists, we were blocked from it) —
                       loud even on an `allow_missing` endpoint.
      `server_error` — 5xx. RETRYABLE (transient); same coverage-gap rule.
      `not_found`    — 404. The `allow_missing` contract's intended case.
      `forbidden`    — a 403 that is NOT a rate limit (no admin on the audited
                       repo). Also an `allow_missing` case, never retried.
      `other`        — unclassifiable (no status in the message). Legacy
                       behaviour: honour `allow_missing`, do not retry.

    `status` and `headers` are the status line and header block `gh api -i`
    returned; when the status is absent we fall back to parsing gh's stderr message.

    THE HEADERS ARE EVIDENCE, and outrank the prose. `-i` already put
    `x-ratelimit-remaining: 0` / `retry-after` in hand, so a 403/429 carrying either
    is a rate limit *no matter what gh's message says*. Without this, a 403 whose
    body gh renders without the keywords classifies as `forbidden` — no retry, and
    on an `allow_missing` endpoint (which is EVERY job log) not even a count: a
    rate-limited audit that silently shrinks its own sample to nothing and reports
    zero errors. That is the exact silent-truncation bug this classifier exists to
    kill.

    The rate-limit KEYWORD is likewise decisive on its OWN — it does not require the
    status to parse. A stderr that literally says "You have exceeded a secondary rate
    limit" must never be classified `other` just because gh wrapped the message, a
    proxy rewrote it, or gh changed its format. A false positive costs one retry; a
    false negative costs a silently truncated report."""
    s = (stderr or "").lower()
    if status is None:
        status = _http_status_from_stderr(s)
    limited = ("rate limit" in s or "abuse detection" in s
               or "exceeded a secondary" in s)
    h = headers or {}
    # The response's OWN rate-limit evidence, for a status that CAN be one (a 404
    # served while the bucket happens to read 0 is still a 404).
    if status in (403, 429):
        remaining = str(h.get("x-ratelimit-remaining", "")).strip()
        if remaining == "0" or str(h.get("retry-after", "")).strip():
            return "rate_limit"
    if status == 429 or limited:
        return "rate_limit"
    if status == 404:
        return "not_found"
    if status == 403:
        return "forbidden"
    if status is not None and 500 <= status <= 599:
        return "server_error"
    return "other"


class GhClient:
    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        # The check-run sampling fetches concurrently (see _select_repr_shas), so
        # the `queries`/`errors` counters are read-modify-write under threads — guard
        # them. The gh subprocess itself is thread-safe (its own process); only these
        # tallies (and `_blocked_until` / `_recorded_from` below) need the lock.
        self._lock = threading.Lock()
        # Token-wide REST pacing for LIVE calls (replay never touches the network, so it
        # never acquires). Shared by every thread in `_fetch_pool`.
        self._governor = _RestRateLimiter()
        # PREFETCH BUFFER — how this module parallelises without restructuring every
        # consumer. `prefetch_json`/`prefetch_text` issue a batch of endpoints through
        # the shared pool and park each response here; the ordinary `json()`/`text()`
        # call at the original site then finds it already fetched. Consumption is
        # POP-ONCE (`_take_prefetched`), never a cache: a second request for the same
        # endpoint re-issues live, exactly as today. That keeps the run's gh call
        # multiset byte-identical to the serial version — this PR changes WHEN calls
        # are issued, never WHICH or HOW MANY.
        #
        # KEYED BY `(endpoint, allow_missing)`, NOT BY ENDPOINT ALONE. `allow_missing`
        # decides the ACCOUNTING RULE the fetch was made under (does a failure count
        # toward `errors`, and so toward the report's partial-coverage banner?). It is
        # applied at PREFETCH time, on a pool thread. If a value fetched under the
        # permissive rule could be served to a call site that passes the strict one, a
        # failure would already have gone uncounted and the consumer would just see
        # `None` — a manufactured silent drop. The flag is part of the key, so a
        # mismatched consumer MISSES the buffer and fetches live under its own rule
        # (and the mismatch is logged: it means a plan has drifted from its call site).
        #
        # A QUEUE PER KEY, not a single slot: a wave never skips an endpoint that is
        # already parked-but-unconsumed. Skipping is how a pop-once buffer quietly
        # becomes a cross-phase CACHE — a later call site would pop a value fetched in
        # an earlier phase instead of issuing its own call. Each planned endpoint is
        # fetched, parked, and popped in FIFO order, so plan N's response goes to call
        # site N.
        self._prefetched_json: dict[tuple[str, bool], deque[Any]] = {}
        self._prefetched_text: dict[str, deque[str | None]] = {}
        # The RAW-LOG plan, not yet fetched — see `prefetch_text` / `_pump_text`. A job log
        # is the heaviest response in the pass (multi-MB), so the text buffer is a bounded
        # WINDOW, not a flat wave: only `_TEXT_WINDOW` logs are ever held live, no matter
        # how long the plan is.
        self._text_plan: deque[str] = deque()
        # Prefetched-but-never-consumed endpoints would be calls the serial path never
        # made. Nothing should land here; a non-zero count at the end of a run means a
        # prefetch plan drifted from its call site, and is logged loudly.
        self.prefetch_unconsumed = 0
        # Shared rate-limit deadline (epoch seconds): when ANY worker is told to back
        # off, every worker waits — one client is shared across the fetch pool, and 8
        # threads independently hammering an endpoint GitHub just blocked is how a
        # 3-attempt budget evaporates in seconds. Read/written under `_lock`.
        self._blocked_until = 0.0
        # --- the global circuit breaker (see `_GH_MAX_GIVEUPS`) -------------------
        # Terminal: once set, every later call short-circuits to None WITHOUT sleeping,
        # so a sustained failure (rate limit, 5xx, OR timeout) ends the audit in bounded
        # time with a disclosed gap instead of hanging for hours. Read/written under
        # `_lock` (the fetch pool shares one client, so one worker's give-up must stop
        # the others too).
        self._gave_up = False
        self._giveups = 0
        self._backoff_spent = 0.0
        # Deterministic offline replay/record — see `_fixture_name` above. Read
        # once here (not re-checked per call) so a run's mode is fixed for its
        # lifetime. Both unset (the default) is byte-identical to the live path
        # that existed before this seam: neither branch below is ever taken.
        self._fixtures_dir = os.environ.get("CI_SPEEDUP_GH_FIXTURES") or None
        self._record_dir = os.environ.get("CI_SPEEDUP_GH_RECORD") or None
        # Record-mode collision guard: fixture filename -> the endpoint that
        # first wrote it this session. `_fixture_name` is lossy (see its
        # docstring), so two DISTINCT endpoints can target one file; the second
        # write would silently clobber the first's body, and replay would then
        # serve valid-but-WRONG JSON. We can't widen the filename scheme without
        # renaming the committed corpus, so instead we detect the collision at
        # record time and log it (never raise — recording is best-effort).
        self._recorded_from: dict[str, str] = {}
        # Replay-mode consumption log (test-only, like the seam above): when
        # `CI_SPEEDUP_GH_FIXTURES_LOG` names a file, every fixture the run
        # actually READS in replay mode is appended to it. Lets an offline e2e
        # assert its committed corpus is fully consumed — so an `allow_missing`
        # fixture (whose absence degrades gracefully rather than failing) is
        # still a regression backstop: a change that stops requesting it is
        # caught by its absence from the log.
        self._replay_access_log = os.environ.get("CI_SPEEDUP_GH_FIXTURES_LOG") or None

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        with self._lock:
            self.queries += int(query)
            self.errors += int(error)

    @property
    def gave_up(self) -> bool:
        """True once the global breaker tripped: the client is DONE talking to gh for
        the rest of this run. `collect()` reads this and aborts with a disclosed
        coverage gap — a loud, fast, honest failure instead of a multi-hour hang."""
        with self._lock:
            return self._gave_up

    def _trip_breaker(self, why: str) -> None:
        with self._lock:
            already = self._gave_up
            self._gave_up = True
        if not already:
            logger.warning(
                "gh: GIVING UP on the whole gh pass — %s. Every remaining call will "
                "fail immediately (no backoff). A sustained block cannot be waited "
                "out inside a useful wall-clock, so the audit aborts with a DISCLOSED "
                "coverage gap rather than hanging for hours and then rendering a "
                "confident report off whatever survived.", why)

    def _note_giveup(self, kind: str) -> None:
        """One endpoint just burned its whole attempt budget against a RETRYABLE
        failure — a rate limit, a 5xx, or a timeout. Enough of those in a row means
        the failure is sustained, not a burst.

        Every retryable kind counts, not just `rate_limit`. A hung API is the worst
        case, not the mildest: each attempt costs the full `timeout` (60s for json,
        90s for text), so an unbounded run against one is hours of nothing. Feeding
        only rate limits in here left exactly that case with no terminal condition."""
        with self._lock:
            self._giveups += 1
            n = self._giveups
        if n >= _GH_MAX_GIVEUPS:
            self._trip_breaker(
                f"{n} endpoint(s) exhausted their retries against a sustained "
                f"{kind.replace('_', ' ')} (limit {_GH_MAX_GIVEUPS})")

    def _note_success(self) -> None:
        """A call succeeded, so whatever failure we hit was transient — reset the
        consecutive-give-up count. The breaker fires on a SUSTAINED failure, not on two
        unlucky calls spread across an otherwise healthy audit."""
        with self._lock:
            self._giveups = 0

    def _spend_backoff(self, seconds: float) -> None:
        """Charge a wait (rate-limit OR transient) against the run-wide backoff
        budget. Both paths charge here: a budget that only sees rate-limit sleeps
        cannot bound a 5xx storm."""
        with self._lock:
            self._backoff_spent += max(0.0, seconds)
            spent = self._backoff_spent
        if spent >= _GH_TOTAL_BACKOFF_BUDGET_S:
            self._trip_breaker(
                f"cumulative gh backoff reached {spent:.0f}s "
                f"(budget {_GH_TOTAL_BACKOFF_BUDGET_S:.0f}s)")

    def _record(self, endpoint: str, ext: str, payload: str) -> None:
        """Write-through a successful live response to `CI_SPEEDUP_GH_RECORD`,
        using the same filename `_fixture_name` computes for replay — this is the
        canonical statement of the record↔replay filename contract (record and
        replay MUST agree on the mapping or a recording never replays). Best-effort:
        a write failure (e.g. read-only fixture dir, or the record path is a file)
        must never fail the collection run that's recording it. Logged at WARNING,
        not DEBUG: the default level is INFO, and a silently-incomplete recorded
        corpus is exactly what a maintainer recording fixtures needs to see.

        Collision guard: because `_fixture_name` is lossy, a DIFFERENT endpoint
        can map to a filename already written this session. Last-writer-wins there
        is NOT survivable: the file would hold endpoint B's body under endpoint A's
        name, and replay would serve A valid-but-WRONG JSON — e.g. a check-runs
        fixture overwritten by `{"default_branch": "main"}`, which reads back as
        "this commit ran no checks" and builds a clean critical path on nothing.
        So a collision RAISES: it refuses to write the second body, and the recording
        run fails loudly rather than producing a corpus that cannot faithfully replay
        what it recorded. (Record mode is maintainer-only and opt-in via
        `CI_SPEEDUP_GH_RECORD`; no end-user run can reach this.) A write ERROR — a
        read-only dir, a bad path — stays best-effort and only warns; that damages
        nothing already on disk.

        `_recorded_from` is read-modify-written from the pooled fetch threads, so
        it is guarded by the same `_lock` as the counters."""
        fname = _fixture_name(endpoint, ext)
        with self._lock:
            prior = self._recorded_from.get(fname)
        if prior is not None and prior != endpoint:
            raise RuntimeError(
                f"gh record: fixture name collision on {fname!r} — endpoint "
                f"{endpoint!r} maps to the same file already recorded from "
                f"{prior!r} (the lossy _fixture_name mapping). Refusing to "
                f"overwrite: the corpus could not replay both, and the survivor "
                f"would be served under the loser's name (valid-but-WRONG JSON). "
                f"Widen _fixture_name or record these endpoints separately.")
        try:
            Path(self._record_dir).mkdir(parents=True, exist_ok=True)
            (Path(self._record_dir) / fname).write_text(payload, encoding="utf-8")
        except OSError as e:
            logger.warning("gh record: failed to write fixture for %s: %s", endpoint, e)
            return
        # Record the source endpoint only after a successful write, so a failed
        # write doesn't mask a real later collision as "already seen".
        with self._lock:
            self._recorded_from[fname] = endpoint

    def _note_replay_access(self, fname: str) -> None:
        """Append a successfully-read fixture filename to the replay-access log
        (`CI_SPEEDUP_GH_FIXTURES_LOG`), if set. Best-effort — never raises. Held under
        `self._lock` because replay reads now also run from the shared fetch pool."""
        if not self._replay_access_log:
            return
        try:
            with self._lock, open(self._replay_access_log, "a", encoding="utf-8") as fh:
                fh.write(fname + "\n")
        except OSError:
            pass

    # -- prefetch ------------------------------------------------------------
    #
    # The parallelism seam. A caller that is ABOUT to issue a known set of endpoints
    # serially hands them here first; they are fetched through the one shared pool and
    # parked, so each original `json()`/`text()` call site is then a buffer hit. The
    # consuming code is untouched — same order, same results, same call count — which
    # is what keeps the sampled data provably identical.
    #
    # CONTRACT (both directions matter):
    #   - Prefetch ONLY endpoints the caller is certain to request. A parked response
    #     nobody consumes is a gh call the serial path never made (counted in
    #     `prefetch_unconsumed` and logged at the end of the run).
    #   - Consumption is POP-ONCE. Requesting the same endpoint twice issues two calls,
    #     as it does today; a caller that legitimately re-requests loses nothing.
    #   - The json buffer's key is `(endpoint, allow_missing)`. A consumer passing a
    #     DIFFERENT `allow_missing` than the plan used misses the buffer and fetches
    #     live under its own accounting rule — the flag is never silently reused.

    def _take_prefetched(self, buf: dict[Any, deque], key: Any) -> Any:
        """Pop the OLDEST response parked under `key` (FIFO), or `_NO_PREFETCH`."""
        with self._lock:
            q = buf.get(key)
            if not q:
                return _NO_PREFETCH
            value = q.popleft()
            if not q:
                del buf[key]
            return value

    def _park(self, buf: dict[Any, deque], key: Any, value: Any) -> None:
        buf.setdefault(key, deque()).append(value)

    def prefetch_json(self, endpoints: Any, *, allow_missing: bool = False) -> None:
        """Issue `endpoints` (deduped within the wave, order-insensitive) through the
        shared fetch pool and park each response for the matching `json()` call.

        `allow_missing` is part of the buffer KEY, so a call site that passes a different
        value than the plan did does not get this response — it misses, fetches live under
        its own accounting rule, and the mismatch is logged (see `json`). A wave never
        skips an endpoint that is already parked: responses queue per key, FIFO."""
        todo = [e for e in dict.fromkeys(endpoints) if e]
        if not todo:
            return
        logger.debug("prefetch: %d json endpoint(s) at width %d",
                     len(todo), _FETCH_CONCURRENCY)
        results = list(_fetch_pool().map(
            lambda e: self._json_live(e, allow_missing), todo))
        with self._lock:
            for endpoint, result in zip(todo, results):
                self._park(self._prefetched_json, (endpoint, bool(allow_missing)), result)

    def prefetch_text(self, endpoints: Any) -> None:
        """`prefetch_json` for the raw-text (job-log) endpoints — but fetched through a
        BOUNDED WINDOW, not as one flat wave. Every text fetch is a job log, always issued
        with allow_missing=True (see `_pump_text` / `_fetch_job_log`), so the accounting
        rule is CONSTANT across every text call and the buffer key is the endpoint alone —
        unlike the json buffer, which keys on `(endpoint, allow_missing)`.

        WHY THIS ONE IS NOT A FLAT WAVE. A job log is the heaviest response in the whole
        pass (multi-MB; a big test job's log runs to tens of MB). `pool.map` over the whole
        plan materialises EVERY planned log before the first consumer runs, and the buffer
        then holds all of them live until each is popped — peak memory O(plan), which on a
        large repo (every cache-family finding × `cap` runs, plus the pole drills) is
        hundreds of MB to GB. The serial path it replaced held exactly one log at a time:
        fetch, parse, discard.

        So the plan is QUEUED here and drained in chunks: at most `_TEXT_WINDOW` logs are
        ever parked, and the window is topped back up (in `text()`) as the call site
        consumes them. Peak memory is O(window) — independent of plan length — while the
        pool still runs full-width inside each chunk, so the wall-clock win is unchanged.

        The JSON waves stay flat on purpose: their responses (run lists, job listings) are
        RETAINED by the consumer for the rest of the pass anyway (`jobs_per_run_by_wf` holds
        the entire sample), so buffering them adds no new peak — unlike logs, which are
        parse-and-discard."""
        todo = [e for e in dict.fromkeys(endpoints) if e]
        if not todo:
            return
        logger.debug("prefetch: %d log endpoint(s) queued, window %d at width %d",
                     len(todo), _TEXT_WINDOW, _FETCH_CONCURRENCY)
        with self._lock:
            self._text_plan.extend(todo)
        self._pump_text()

    def _pump_text(self) -> None:
        """Top the raw-log window back up to `_TEXT_WINDOW` from the queued plan.

        Refills only when the parked count has fallen to `_TEXT_LOW_WATER` or below, so the
        refill is a WAVE (≥ `_FETCH_CONCURRENCY` endpoints — the pool runs full-width),
        never a one-at-a-time trickle that would quietly re-serialise the log fetches."""
        with self._lock:
            parked = sum(len(q) for q in self._prefetched_text.values())
            if parked > _TEXT_LOW_WATER or not self._text_plan:
                return
            take = min(_TEXT_WINDOW - parked, len(self._text_plan))
            batch = [self._text_plan.popleft() for _ in range(take)]
        # Every prefetched log is a job log, which the only `text()` call site
        # (`_fetch_job_log`) fetches with allow_missing=True — an absent log (retention
        # window, or a job that never ran) is a disclosed drill-down gap, not a collection
        # error. Fetch under the SAME rule here so the buffered value's accounting matches
        # the live call it stands in for (a rate-limit / 5xx block still counts — `_invoke`).
        results = list(_fetch_pool().map(
            lambda e: self._text_live(e, allow_missing=True), batch))
        with self._lock:
            for endpoint, result in zip(batch, results):
                self._park(self._prefetched_text, endpoint, result)

    def _unqueue_text(self, endpoint: str) -> None:
        """Drop `endpoint` from the not-yet-fetched log plan — the call site is asking for
        it NOW and will fetch it live, so the window must not fetch it a second time later.
        (Only reachable when a call site consumes its plan far out of order; the three
        planned sites consume in plan order.)"""
        with self._lock:
            try:
                self._text_plan.remove(endpoint)
            except ValueError:
                pass

    def _warn_flag_mismatch(self, endpoint: str, allow_missing: bool) -> None:
        """A buffer MISS on `(endpoint, allow_missing)` while the SAME endpoint sits
        parked under the other flag is not a coincidence — it is a prefetch plan that
        disagrees with its call site about whether a failure here counts. The response is
        NOT served (that would apply the plan's accounting rule to the call site's call);
        say so, because the parked value is now a paid-for call nobody will consume."""
        with self._lock:
            stranded = (endpoint, not bool(allow_missing)) in self._prefetched_json
        if stranded:
            logger.warning(
                "prefetch: %s was prefetched with allow_missing=%s but requested with "
                "allow_missing=%s — the plan disagrees with its call site about whether a "
                "failure here counts; fetching live under the call site's rule",
                endpoint, not bool(allow_missing), bool(allow_missing))

    def drain_prefetch(self) -> int:
        """Discard every prefetch a call site never consumed and return how many there
        were. Non-zero means a prefetch plan drifted from its call site — the caller logs
        it loudly AND records it in the findings document's `data_sources`, so a committed
        report carries the evidence.

        THREE kinds of leftover, all drift, all counted:
          * a parked JSON response nobody popped — a gh call the serial path never made;
          * a parked LOG nobody popped — likewise;
          * a QUEUED log endpoint the window never reached (`_text_plan`). This one cost no
            gh call (the bounded window never issued it), but it is the same bug: a plan
            that asked for a log its call site does not read. Counting it keeps the guard's
            sensitivity to a drifted TAIL, which a flat wave would have caught by paying
            for it."""
        with self._lock:
            n = (sum(len(q) for q in self._prefetched_json.values())
                 + sum(len(q) for q in self._prefetched_text.values())
                 + len(self._text_plan))
            self._prefetched_json.clear()
            self._prefetched_text.clear()
            self._text_plan.clear()
        self.prefetch_unconsumed += n
        return n

    def _replay_json(self, endpoint: str, allow_missing: bool) -> Any:
        """Replay-mode `json()`: read the canned response from
        `CI_SPEEDUP_GH_FIXTURES` instead of spawning `gh`. An absent fixture
        file mirrors the live path's failure/missing return (`None`) — it
        never raises — and bumps `errors` under the same `allow_missing` rule
        the live path uses."""
        path = Path(self._fixtures_dir) / _fixture_name(endpoint, "json")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            if not allow_missing:
                self._bump(error=True)
            return None
        self._note_replay_access(path.name)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            if not allow_missing:
                self._bump(error=True)
            return None

    def _replay_text(self, endpoint: str, allow_missing: bool) -> str | None:
        """Replay-mode `text()`: same contract as `_replay_json` but for the
        raw-text fixtures (`.txt`), including the same `allow_missing` rule."""
        path = Path(self._fixtures_dir) / _fixture_name(endpoint, "txt")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            if not allow_missing:
                self._bump(error=True)
            return None
        self._note_replay_access(path.name)
        return raw

    def _rate_limit_wait(self, headers: dict[str, str], attempt: int) -> float:
        """How long to wait before retrying a rate-limited call, in the order
        GitHub documents: `retry-after` (seconds) if present, else
        `x-ratelimit-reset` (epoch seconds), else the documented floor ("wait at
        least one minute") doubled per attempt. `headers` come from the FAILED
        response itself (`gh api -i`), so this costs no extra request.

        A header-derived wait is FLOORED at `_GH_RATE_LIMIT_FLOOR_S`: the reset
        header describes the PRIMARY hourly bucket, but a SECONDARY block (the one
        a concurrent audit actually trips) needs ≥60s regardless. Honouring a
        3-seconds-from-rollover `x-ratelimit-reset` literally would retry straight
        back into the same block and spend the whole attempt budget in ~3 seconds.
        `retry-after` IS the server's answer for this block and is taken as given.

        Jittered so N blocked workers don't wake in lockstep, and capped at
        `_GH_MAX_BACKOFF_S` — a wait longer than that is better spent failing
        loudly than hanging."""
        wait: float | None = None
        floored = True
        try:
            if headers.get("retry-after"):
                wait = float(headers["retry-after"])
                floored = False             # the server's own answer for THIS block
            elif headers.get("x-ratelimit-reset"):
                wait = float(headers["x-ratelimit-reset"]) - time.time()
        except (TypeError, ValueError):
            wait = None
        if wait is None or wait <= 0:
            wait = _GH_RATE_LIMIT_FLOOR_S * (2 ** (attempt - 1))
        elif floored:
            wait = max(wait, _GH_RATE_LIMIT_FLOOR_S)
        wait += random.uniform(0, _GH_JITTER_S)
        return min(max(wait, 1.0), _GH_MAX_BACKOFF_S)

    def _record_block(self, wait: float) -> None:
        """Publish a rate-limit block to EVERY worker: the pool shares one client,
        so one thread's discovery must pause the others. Without this, 8 threads
        each independently discover the same block, each burns its own attempt
        budget hammering the endpoint GitHub just told us to stop hitting, and the
        audit ends with 8 coverage gaps instead of one honest pause."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.time() + wait)

    def _sleep_until_unblocked(self) -> None:
        """Sleep off any block another worker (or a previous attempt) recorded.
        The single place a backoff is actually served, so a thread never
        double-sleeps its own block.

        RE-CHECKS after waking: another worker can EXTEND `_blocked_until` while this
        thread is asleep, and a single-shot sleep would wake straight back into the
        live block and burn an attempt on a call GitHub is still refusing. Each
        DISTINCT deadline is served at most once — the loop re-reads the deadline and
        sleeps again only if someone pushed it FURTHER OUT. (Not "sleep until the clock
        passes it": `time.sleep` can return early — a signal, or a test stub — and a
        clock-based loop would then spin.) Every served wait is charged to the run-wide
        backoff budget, so a sustained block trips the breaker instead of hanging."""
        served_until = 0.0
        while True:
            with self._lock:
                if self._gave_up:
                    return
                until = self._blocked_until
            if until <= served_until:   # already served this block; a further wait is
                return                  # the caller's next attempt to earn, not ours
            delay = until - time.time()
            if delay <= 0:
                return
            served_until = until
            served = min(delay, _GH_MAX_BACKOFF_S)
            time.sleep(served)
            self._spend_backoff(served)

    def _invoke(self, endpoint: str, *, timeout: int,
                allow_missing: bool) -> str | None:
        """THE choke point: run `gh api -i <endpoint>` and return the response
        BODY, or None on failure. Every live gh byte this skill reads passes
        through here, so the retry / classification / loudness policy is stated
        exactly once.

        `-i` (headers + body) rather than a bare `gh api`: the failed response's
        OWN `retry-after` / `x-ratelimit-reset` is then already in hand, so the
        backoff honours the server instead of guessing — with no second request
        fired at the endpoint that just rate-limited us (which is precisely what
        GitHub's secondary-limit guidance says not to do). `_split_headers_body`
        hands back the body untouched if the header block is missing, so the body
        path cannot be corrupted by the split.

        Failure handling (see `_classify_gh_failure`):
          - rate limit (429, or any rate-limit / abuse-detection message): retried
            up to `_GH_MAX_ATTEMPTS` at the server's own `retry-after` /
            `x-ratelimit-reset` (floored at GitHub's documented one minute),
            jittered, and published to every other worker (`_record_block`).
            Logged at WARNING, NEVER debug — a silent rate-limit block is how a run
            quietly shrinks its sample and still renders a confident, WRONG report.
          - 5xx, and a TIMEOUT (the data exists; we just didn't get it — the same
            meaning as a 5xx): retried with 1s/2s/4s + jitter.
          - Exhausting the retries on any of those is a REAL COVERAGE GAP: it
            counts toward `errors` (tripping the report's partial-coverage banner)
            EVEN on an `allow_missing` endpoint. `allow_missing` means "a 404
            here is expected", not "swallow whatever goes wrong here".
          - 404 / non-rate-limit 403 / unclassifiable: unchanged legacy
            behaviour — honour `allow_missing`, no retry.
          - gh not installed (`FileNotFoundError`): terminal, never retried.

        The attempt budget is PER CALL; the GLOBAL breaker (`gave_up`) is what bounds
        the RUN. EVERY retryable exhaustion feeds it — rate limit, 5xx, and timeout
        alike — and every backoff second (both paths) is charged to its budget. Once
        it trips, this returns None IMMEDIATELY — no sleep, no subprocess — and counts
        the gap, so the hundreds of calls still queued behind a sustained failure cost
        seconds, not hours. The timeout case is the one that most needs this: each
        attempt against a hung API costs the full `timeout`, so without the breaker a
        several-hundred-call audit is a multi-hour hang."""
        if self.gave_up:
            self._bump(error=True)
            logger.debug("gh api %s: skipped — the client gave up (rate-limit "
                         "breaker tripped); counted as a coverage gap", endpoint)
            return None
        for attempt in range(1, _GH_MAX_ATTEMPTS + 1):
            self._sleep_until_unblocked()
            if self.gave_up:            # the breaker can trip while we were asleep
                self._bump(error=True)
                return None
            # Pace EVERY live attempt against the token-wide REST governor — the first try
            # AND each retry. GitHub's binding secondary limit is an AGGREGATE rate, so a
            # retry storm (N workers all backing off, then all firing at once) is precisely
            # when the run must not outrun the budget. `_invoke` is the live path only —
            # replay short-circuits in `_json_live`/`_text_live` and never reaches here — so
            # the governor is acquired once per real subprocess and never in offline tests.
            self._governor.acquire(_route_key(endpoint))
            headers: dict[str, str] = {}
            status: int | None = None
            try:
                r = subprocess.run(
                    ["gh", "api", "-i", endpoint],
                    capture_output=True, text=True, timeout=timeout,
                )
            except FileNotFoundError as e:      # gh isn't installed — retrying can't help
                if not allow_missing:
                    self._bump(error=True)
                    print(f"gh error: {e}", file=sys.stderr)
                return None
            except subprocess.TimeoutExpired as e:
                kind, stderr, r = "server_error", f"timed out after {timeout}s: {e}", None
            except (OSError, UnicodeDecodeError) as e:
                # WAVE-SAFETY (the parallel pass runs `_invoke` on the shared pool's
                # threads): SPAWNING the subprocess is itself an OS operation that can fail
                # for reasons other than "gh isn't installed" (`FileNotFoundError`, caught
                # above) — EMFILE / ENOMEM / EAGAIN when the 8th concurrent `gh` is forked
                # under process-table pressure — and `text=True` DECODES the child's stdout,
                # so a multi-MB job log with one invalid UTF-8 byte raises `UnicodeDecodeError`
                # here. Uncaught, either escapes the pool's `map()` and discards the WHOLE
                # wave (hundreds of good responses) instead of one call. It is a fetch failure
                # like any other — NOT an expected-absent 404, whatever `allow_missing` says —
                # so it is always counted, returned as None, and the run continues with a
                # disclosed gap. Not retried: a resource/decoding failure won't clear on an
                # immediate re-run of the same call.
                self._bump(error=True)
                print(f"gh error: {e}", file=sys.stderr)
                return None
            else:
                status, headers, body = _split_headers_body(r.stdout or "")
                if r.returncode == 0:
                    # A success means the failure (if any) was transient — don't let two
                    # unlucky calls spread across a healthy audit trip the breaker.
                    self._note_success()
                    return body
                stderr = (r.stderr or "").strip()
                kind = _classify_gh_failure(stderr, status, headers)
            if kind in ("rate_limit", "server_error"):
                if attempt < _GH_MAX_ATTEMPTS:
                    if kind == "rate_limit":
                        wait = self._rate_limit_wait(headers, attempt)
                        self._record_block(wait)    # pauses every worker, not just this one
                    else:
                        wait = (_GH_TRANSIENT_BASE_S * (2 ** (attempt - 1))
                                + random.uniform(0, 0.5))
                        time.sleep(wait)
                        # Charged to the SAME run-wide budget as a rate-limit wait: a
                        # budget blind to transient sleeps cannot bound a 5xx storm.
                        self._spend_backoff(wait)
                    logger.warning(
                        "gh api %s: %s (attempt %d/%d) — backing off %.0fs then "
                        "retrying: %s",
                        endpoint, kind.replace("_", " "), attempt,
                        _GH_MAX_ATTEMPTS, wait, stderr[:200])
                    continue
                self._bump(error=True)
                logger.warning(
                    "gh api %s: %s — GAVE UP after %d attempts. This is a REAL "
                    "COVERAGE GAP (the data exists; we were blocked from it), not "
                    "an empty result — the report will disclose partial coverage: %s",
                    endpoint, kind.replace("_", " "), _GH_MAX_ATTEMPTS, stderr[:200])
                # Feed the GLOBAL breaker. EVERY retryable kind — a 5xx storm and a
                # hung API are exactly as unbounded as a rate limit, and the timeout
                # case is the most expensive of the three (a full `timeout` per
                # attempt), so gating this on `rate_limit` left the worst case with no
                # terminal condition at all.
                self._note_giveup(kind)
                return None
            if not allow_missing:
                self._bump(error=True)
                logger.debug("gh api %s failed (rc=%s, %s): %s",
                             endpoint, r.returncode, kind, stderr[:200])
            return None
        return None                                             # pragma: no cover

    def json(self, endpoint: str, allow_missing: bool = False) -> Any:
        """Run `gh api <endpoint>` and parse JSON. Returns None on failure.

        `allow_missing=True` is for endpoints whose absence is EXPECTED, not a
        collection error — e.g. branch-protection / rulesets return 404 when you
        lack admin on the audited repo (the normal case auditing a repo you
        don't own). A failure there is "data unavailable", so it does NOT count
        toward `errors` (which trips the report's partial-coverage banner). It
        does NOT cover a rate-limit / 5xx block — see `_invoke`.

        When `CI_SPEEDUP_GH_FIXTURES` is set, this never spawns a subprocess —
        see `_replay_json`.

        A response already parked by `prefetch_json` under the SAME `allow_missing` is
        returned straight from the buffer (it was fetched — and counted — there); anything
        else, including a parked response fetched under the OTHER flag, is fetched here
        under this call site's own accounting rule, exactly as before."""
        parked = self._take_prefetched(self._prefetched_json,
                                       (endpoint, bool(allow_missing)))
        if parked is not _NO_PREFETCH:
            return parked
        self._warn_flag_mismatch(endpoint, allow_missing)
        return self._json_live(endpoint, allow_missing)

    def _json_live(self, endpoint: str, allow_missing: bool = False) -> Any:
        """`json()` minus the prefetch-buffer lookup — the actual fetch. Called both
        by `json()` (buffer miss) and by `prefetch_json` (from the pool's threads), so
        it must stay thread-safe: it touches only `_bump`, `_record`, and locals."""
        self._bump(query=True)
        if self._fixtures_dir:
            return self._replay_json(endpoint, allow_missing)
        out = self._invoke(endpoint, timeout=60, allow_missing=allow_missing)
        if out is None:
            return None
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            # A non-JSON body on an ALLOW_MISSING endpoint is the same "data
            # unavailable" case as a 404 (gh can emit a non-JSON diagnostic on
            # the error paths this flag exists for) — it must NOT trip the
            # partial-coverage banner. Only count it as a collection error when
            # the endpoint was expected to return JSON.
            if not allow_missing:
                self._bump(error=True)
                logger.debug("gh api %s: non-JSON response (%d bytes)",
                             endpoint, len(out or ""))
            return None
        if self._record_dir:
            self._record(endpoint, "json", out)
        return parsed

    def text(self, endpoint: str, allow_missing: bool = False) -> str | None:
        """Run `gh api <endpoint>` and return raw stdout (for non-JSON
        endpoints like `…/jobs/{id}/logs`). Returns None on failure.

        `allow_missing` carries the same meaning as in `json()`: an EXPECTED
        absence (GitHub 404s the logs of a job that never ran) is "data
        unavailable", not a collection error, and must not trip the report's
        partial-coverage banner. A rate-limit / 5xx block still counts — see
        `_invoke`.

        When `CI_SPEEDUP_GH_FIXTURES` is set, this never spawns a subprocess —
        see `_replay_text`. A response parked by `prefetch_text` is returned from the
        buffer; anything else is fetched here.

        Consuming a parked log is also what ADVANCES the bounded prefetch window
        (`_pump_text`) — the next chunk of the plan is fetched as this one is drained, so
        the pool stays busy while peak memory stays O(`_TEXT_WINDOW`)."""
        parked = self._take_prefetched(self._prefetched_text, endpoint)
        if parked is not _NO_PREFETCH:
            self._pump_text()
            return parked
        # A miss on an endpoint still sitting in the plan means the call site is consuming
        # far out of plan order. Serve it live — and take it OFF the plan, or the window
        # would fetch it again later and bill a call nobody consumes.
        self._unqueue_text(endpoint)
        return self._text_live(endpoint, allow_missing)

    def _text_live(self, endpoint: str, allow_missing: bool = False) -> str | None:
        """`text()` minus the prefetch-buffer lookup — the actual fetch, routed through
        the single `_invoke` choke point (retry/backoff/breaker/governor apply). Called
        both by `text()` (buffer miss) and by `prefetch_text` (from the pool's threads),
        so it must stay thread-safe: it touches only `_bump`, `_invoke`, `_record`, and
        locals. See `_json_live`."""
        self._bump(query=True)
        if self._fixtures_dir:
            return self._replay_text(endpoint, allow_missing)
        out = self._invoke(endpoint, timeout=90, allow_missing=allow_missing)
        if out is None:
            return None
        if self._record_dir:
            self._record(endpoint, "txt", out)
        return out

    def available(self) -> bool:
        """Best-effort check that gh is installed and authenticated.

        Replay mode (`CI_SPEEDUP_GH_FIXTURES` set) always returns True — the
        offline chain never needs a real `gh auth status`.

        NOTE: `gh auth status` makes a live API call to verify the token, so it also
        returns non-zero when the token is fine but the API is REFUSING us (a
        secondary-rate-limit block returns 403 on the verify call). A False here is
        therefore "gh cannot talk to the API right now" — NOT necessarily "gh is
        unauthenticated". `diagnose_unavailability` tells the two apart."""
        if self._fixtures_dir:
            return True
        try:
            r = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def diagnose_unavailability(self) -> str:
        """Called only when `available()` is False, to say WHY — the two causes are
        OPPOSITE and must not be conflated:

          * ``"absent"`` — the gh binary is missing OR no token is configured. The gh
            pass genuinely cannot run; a static-scan-only report is the honest fallback
            (`gh_unavailable`), and its empty spine may legitimately read as a quiet repo.
          * ``"api_blocked"`` — gh IS installed and a token IS configured, but the
            `available()` probe still failed: the credential check did NOT come back
            clean. That covers a rate-limit / secondary-limit block, a transport / 5xx
            error, AND an invalid credential (an expired, revoked, or too-narrowly-scoped
            token) — the offline probes CANNOT tell these apart, because `gh auth token`
            only proves a token STRING is stored, never that the API would accept it. All
            of them are a COLLECTION FAILURE, not a missing tool: a static-only report
            here would masquerade as a complete audit of a quiet repo while the truth is
            the token was refused — so the caller stamps the LOUD `collection_failed` kind
            instead of `gh_unavailable`, and its remediation names BOTH "wait out a rate
            limit" and "re-authenticate", since we can't say which applies.

        Both probes are OFFLINE (`gh --version`, `gh auth token` — neither hits the API),
        so the diagnosis itself can never be the thing that is rate-limited."""
        # Defensive parity with `available()`: unreachable via `collect()` today (replay
        # mode makes `available()` return True, so this method is never called then), but
        # kept so a direct caller in replay mode can't misread fixtures as `api_blocked`.
        if self._fixtures_dir:
            return "absent"
        try:
            binary = subprocess.run(
                ["gh", "--version"], capture_output=True, text=True, timeout=10,
            ).returncode == 0
            token = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
            ).returncode == 0
        except FileNotFoundError:
            # The gh binary is genuinely not on PATH — the honest `absent` fallback.
            return "absent"
        except (OSError, subprocess.TimeoutExpired):
            # gh IS present (`available()` already spawned it to get its False), but a
            # probe hung or errored. That is NOT absence — and this PR's whole thesis is
            # that an AMBIGUOUS gh failure must default to the LOUD side rather than ship
            # a silent static-only report. Route it to `api_blocked`, not `absent`.
            return "api_blocked"
        return "api_blocked" if (binary and token) else "absent"


def _paginate(client: GhClient, path: str, key: str, *, params: str = "",
              per_page: int = 100) -> list[Any] | None:
    """Walk a paginated list endpoint to COMPLETION and return every item.

    `path` is the endpoint without a query string; `params` is any extra query
    beyond `per_page` (e.g. `filter=all`). Page 1's endpoint string is exactly
    `<path>?per_page=<n>[&<params>]` — byte-identical to the string these call
    sites used before pagination existed, which keeps the record/replay fixture
    names (`_fixture_name`) stable.

    Why this exists: the call sites used to request `per_page=100` and stop,
    keeping whatever page 1 held. Measured on better-auth/better-auth @ 6f20f44,
    a commit's check-runs endpoint reported `total_count = 103` and returned 100
    — three checks SILENTLY DROPPED from the critical-path sample. Had one of them
    been the merge pole, the computed gate would have been WRONG.

    Stop condition: a SHORT PAGE (one GitHub could not fill). `total_count` is a
    warn-only cross-check, never a stop — see the inline note. The `_GH_MAX_PAGES`
    ceiling is NOT a stop condition either — it is a FAILURE (see below).

    Returns None if ANY page fails — never a short list. A partial walk is a
    coverage gap, and handing back a truncated list that LOOKS complete is exactly
    the silent-drop bug this replaces. "Any page fails" means all of:
      - the fetch failed (`json()` returned None — already counted in
        `client.errors`);
      - the page is MALFORMED (not an object, or its list key isn't a list). The one
        exception is a page 1 GitHub explicitly declares empty with `total_count == 0`
        (some endpoints send the count and omit the key) — a genuinely empty
        collection, which every caller already reads as "nothing ran". A body that
        merely CONTAINS the key is NOT evidence of emptiness: `{"jobs": "oops"}` /
        `{"jobs": {…}}` / `{"jobs": null}` are malformed and fail like any other bad
        page. (A real empty list needs no exception — `[]` IS a list, so it stops on
        the short page and returns `[]`.);
      - the walk hits `_GH_MAX_PAGES`. 100 items/page × 50 pages far exceeds any
        real run's job count or commit's check count, so reaching it means the stop
        condition is BROKEN — the accumulated items are of unknown completeness.
        Returning them (as this once did, with `errors == 0`) would hand back a
        short list that looks complete: the precise bug. It counts as an error so
        the report's partial-coverage banner fires.

    Callers keep None (fetch failed) and `[]` (genuinely empty) apart, and surface
    None as a disclosed gap.

    A free function, not a `GhClient` method, so it composes over anything with a
    `json()` AND a `_bump()` (the malformed/ceiling paths count their own coverage
    gaps — `json()` only counts the failures IT sees) — it adds no new surface to the
    seam every gh byte flows through."""
    items: list[Any] = []
    page = 1
    while True:
        endpoint = f"{path}?per_page={per_page}"
        if params:
            endpoint += f"&{params}"
        if page > 1:
            endpoint += f"&page={page}"
        doc = client.json(endpoint)
        if doc is None:
            logger.warning(
                "gh paginate %s: page %d failed — abandoning the whole fetch "
                "(%d item(s) already read) rather than returning a silently "
                "truncated list", path, page, len(items))
            return None
        if not isinstance(doc, dict):
            client._bump(error=True)
            logger.warning(
                "gh paginate %s: page %d returned a non-object body (%s) — "
                "abandoning the whole fetch (%d item(s) already read) rather than "
                "returning a silently truncated list",
                path, page, type(doc).__name__, len(items))
            return None
        total = doc.get("total_count")
        has_total = isinstance(total, int) and not isinstance(total, bool)
        chunk = doc.get(key)
        if not isinstance(chunk, list):
            # A page-1 body whose list key isn't a LIST is malformed — full stop. The
            # ONLY non-failure shape here is GitHub explicitly declaring the collection
            # empty via `total_count == 0` (some endpoints send the count and omit the
            # key). A body that merely CONTAINS the key is not evidence of emptiness:
            # this used to test `key in doc`, so `{"jobs": "oops"}`, `{"jobs": {...}}`
            # and `{"jobs": null}` each returned `[]` with errors == 0 — via
            # `_list_workflows` that reads as "this repo runs NOTHING", and the audit
            # renders static-only with no coverage note at all. Emptiness is a `[]`, and
            # a real `[]` needs no exception: it IS a list, so it falls through to the
            # normal short-page stop below and returns `[]` on its own.
            if page == 1 and has_total and total == 0:
                return []               # genuinely empty collection, not a failure
            client._bump(error=True)
            logger.warning(
                "gh paginate %s: page %d is malformed (%r is %s, not a list; "
                "total_count=%r) — abandoning the whole fetch (%d item(s) already "
                "read) rather than returning a silently truncated list",
                path, page, key, type(chunk).__name__, total, len(items))
            return None
        items.extend(chunk)
        # THE authoritative stop is the SHORT PAGE — a page GitHub could not fill is
        # the end of the collection, by definition. `total_count` is only a
        # cross-check: it is a field we don't control, and if an endpoint ever
        # UNDER-reports it (the `filter=all` jobs view is the one to worry about),
        # stopping on `len(items) >= total` would silently drop pages 2+ — the exact
        # truncation this function exists to prevent. Cost of not trusting it: one
        # extra call on an endpoint whose item count lands exactly on a page boundary.
        if len(chunk) < per_page:
            if has_total and total > len(items):
                logger.warning(
                    "gh paginate %s: short page %d ended the walk at %d item(s) but "
                    "total_count=%d — GitHub's own count disagrees with what it "
                    "served; reporting what was actually returned",
                    path, page, len(items), total)
            break
        page += 1
        if page > _GH_MAX_PAGES:
            client._bump(error=True)
            logger.warning(
                "gh paginate %s: hit the %d-page ceiling with %d item(s) — the "
                "stop condition is broken, so the accumulated list is of UNKNOWN "
                "completeness. Failing the whole fetch (a disclosed coverage gap) "
                "rather than returning a short list that looks complete",
                path, _GH_MAX_PAGES, len(items))
            return None
    return items


# --- the partial-coverage disclosure (data → renderer → verify invariant) ------
#
# `data_sources.partial_kind` is the SEVERITY of the disclosure, carried as DATA.
# The renderer keys its wording off this key and NEVER string-matches
# `partial_reason`: the reason is a sentence written for a human, and a renderer that
# guesses severity from its wording is one reword away from stapling "so a few
# runs/jobs are absent from the sample" onto "NO workflow could be measured" — the
# report contradicting its own disclosure and downgrading a total collection failure
# to a rounding error.
_PARTIAL_STATIC_ONLY = "static_only"            # no --repo: the gh pass never ran
_PARTIAL_GH_UNAVAILABLE = "gh_unavailable"      # gh missing / not authenticated
_PARTIAL_COLLECTION_FAILED = "collection_failed"  # NOTHING was measured (whole-repo gap)
_PARTIAL_WORKFLOW_MISSING = "workflow_missing"  # >=1 workflow dropped out of the sample
_PARTIAL_SAMPLE_THINNED = "sample_thinned"      # some calls failed; the sample is thinner
# Only this kind may be rendered as a minor caveat. Everything else is a hole.
_PARTIAL_MINOR_KINDS = frozenset({_PARTIAL_SAMPLE_THINNED})


def _partial_reason(errors: int,
                    sample_gaps: list[dict[str, str]] | None = None,
                    gave_up: bool = False) -> str | None:
    """The report's partial-coverage sentence, or None when coverage is clean.

    A bare count ("4 gh API call(s) failed") is true but useless: it reads as a
    rounding error, when what actually happened may be that a WHOLE WORKFLOW dropped
    out of the sample. So every workflow whose sample fetch failed is NAMED here —
    the reader can then see that e.g. the merge gate wasn't measured at all, rather
    than assuming the missing calls thinned a P50 by a run or two.

    `sample_gaps` is the union of the two ways a workflow can vanish from the measured
    sample (each entry `{"workflow_file": …, "fetch": …}`, so the sentence says WHICH
    fetch died):
      - its RUN-LIST fetch failed (`run_list_fetch_failures`) — no runs at all; and
      - every one of its per-run JOB fetches failed (`job_fetch_failures`) — runs, but
        no job timing, so its checks fall back to queue-inflated check-run spans.
    Both leave the critical path computed from the SURVIVORS, which is the news.

    Pair it with `_partial_kind`: this is what the reader reads, that is what the
    renderer branches on."""
    gaps = sample_gaps or []
    if not errors and not gaps and not gave_up:
        return None
    parts = []
    if gave_up:
        parts.append(
            "collection was ABORTED after repeated GitHub API failures (rate-limit "
            "blocks, server errors, or timeouts), so the gh pass is INCOMPLETE — "
            "whatever was measured before the abort is not a full picture of the repo")
    if errors:
        parts.append(f"{errors} gh API call(s) failed during collection")
    if gaps:
        named = "; ".join(f"{g.get('workflow_file')} ({g.get('fetch')})" for g in gaps)
        parts.append(
            f"the sample fetch FAILED for {len(gaps)} workflow sample(s) — "
            f"{named} — so those workflows are MISSING from the sample, not empty")
    return ", and ".join(parts)


def _partial_kind(errors: int,
                  sample_gaps: list[dict[str, str]] | None = None,
                  gave_up: bool = False) -> str | None:
    """The severity of `_partial_reason`'s sentence, as a machine-readable key.
    None when coverage is clean. Ordered worst-first: an abort outranks a vanished
    workflow, which outranks a merely thinner sample."""
    if gave_up:
        return _PARTIAL_COLLECTION_FAILED
    if sample_gaps:
        return _PARTIAL_WORKFLOW_MISSING
    if errors:
        return _PARTIAL_SAMPLE_THINNED
    return None


# =============================================================================
# Helpers
# =============================================================================

def _parse_dt(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _duration_s(start: str | None, end: str | None) -> float | None:
    a, b = _parse_dt(start), _parse_dt(end)
    return (b - a).total_seconds() if (a and b) else None


def _dedupe_tied(tied: "list[tuple[list[str], int]]") -> "list[tuple[list[str], int]]":
    """Cell 10: an UNOBSERVED job extending a path contributes zero weight and
    IDENTICAL members — the same physical wait, not a competing path. Distinct
    physical paths = distinct member tuples; duplicates keep the max ways
    (they inherit the same upstream tie count)."""
    best_by_members: dict[tuple[str, ...], int] = {}
    for members, ways in tied:
        key = tuple(members)
        best_by_members[key] = max(best_by_members.get(key, 0), ways)
    return [(list(k), w) for k, w in sorted(best_by_members.items())]


def _chain_path_pool(
    capped: dict[str, float],
    graph: "dict[str, dict[str, dict[str, Any]]]",
    node_checks: "dict[tuple[str, str], list[str]]",
    peers: list[str],
) -> "tuple[list[tuple[float, list[str], int]], dict[str, str]]":
    """The competing-path pool for one weight map: every distinct leaf path per
    workflow (deduped by member identity, cell 10) plus the unresolved peers.
    Shared by BOTH `_chain_facts_for_pr` passes — the full-weight pass (chain +
    co-longest) and the members-zeroed pass (the whole-chain `runner_up_s`) —
    so the two can never disagree on walk semantics. Returns (candidates,
    fallback): candidates = (path_s, members root->leaf, ways); fallback =
    {workflow: reason} for cell-7 cycle fail-opens (whose observed checks
    compete as singletons under today's model)."""
    fallback: dict[str, str] = {}
    candidates: list[tuple[float, list[str], int]] = []

    def _rep(names: list[str]) -> tuple[str, float]:
        top = max(capped[n] for n in names)
        return sorted(n for n in names if capped[n] == top)[0], top

    by_wf: dict[str, dict[str, tuple[str, float]]] = {}
    for (wf, jid), names in node_checks.items():
        by_wf.setdefault(wf, {})[jid] = _rep(names)

    for wf, observed in sorted(by_wf.items()):
        jobs = graph.get(wf) or {}

        # best(jid) = (path_s, members, ways); walk parents via `needs`.
        memo: dict[str, tuple[float, list[str], int]] = {}
        visiting: set[str] = set()
        state = {"cycle": False}

        def best(jid: str) -> tuple[float, list[str], int]:
            if jid in memo:
                return memo[jid]
            if jid in visiting:
                state["cycle"] = True
                return (0.0, [], 1)
            visiting.add(jid)
            rep = observed.get(jid)
            w = rep[1] if rep else 0.0
            members = [rep[0]] if rep else []
            parents = (jobs.get(jid, {}) or {}).get("needs") or []
            parents = [parents] if isinstance(parents, str) else list(parents)
            parents = [p for p in parents if p in jobs]
            if parents:
                scored = [best(p) for p in parents]
                top = max(s for s, _, _ in scored)
                tied = _dedupe_tied(
                    [(m, ways) for s, m, ways in scored if s == top])
                ways = sum(w2 for _, w2 in tied) if top > 0 else 1
                out = (top + w, tied[0][0] + members, ways)
            else:
                out = (w, members, 1)
            visiting.discard(jid)
            memo[jid] = out
            return out

        leaves = [best(jid) for jid in sorted(jobs)]
        if state["cycle"]:
            # Cell 7: fail open for THIS workflow — its checks compete as
            # singletons under today's model, and the reason is stamped.
            # (As-built narrowing, recorded in the plan: only a cycle stamps a
            # reason. A parse-failed workflow has no graph entry at all — its
            # checks resolve to cell-4 peers, today's model, nothing to
            # disclose; reusable-caller children collapse to the slowest
            # child on the caller node, like matrix legs.)
            fallback[wf] = "needs cycle"
            for jid, (name, span) in sorted(observed.items()):
                candidates.append((span, [name], 1))
            continue
        # EVERY distinct leaf path competes globally, deduped by member
        # identity per cell 10; keep, per member tuple, the best score.
        by_members: dict[tuple[str, ...], tuple[float, int]] = {}
        for s, m, ways in leaves:
            if not m:
                continue
            key = tuple(m)
            cur = by_members.get(key)
            if cur is None or (s, ways) > cur:
                by_members[key] = (s, ways)
        for key, (s, ways) in sorted(by_members.items()):
            if s > 0:
                candidates.append((s, list(key), ways))

    for name in peers:
        if capped.get(name):
            candidates.append((capped[name], [name], 1))
    return candidates, fallback


def _chain_facts_for_pr(
    checks: dict[str, float],
    caps: dict[str, float],
    job_graph: "dict[str, dict[str, dict[str, Any]]] | None",
    crit_by_wf: dict[str, dict[str, Any]],
) -> "dict[str, Any] | None":
    """ENG-1 PR-N1: the per-PR chain TIMING facts — the longest path through each
    workflow-local `needs:` DAG, path length = sum of the members' CAPPED spans
    (the same `_pole_caps` values the pole/populations pipeline caps on, so the
    facts and the ranking can never disagree about a member's magnitude).

    Data-only in N1: nothing reads these facts for gate/render behavior yet
    (that is PR-N2). The decision table lives in `tests/test_chain_facts.py`;
    the cells in short: matrix legs collapse to one node gated by the slowest
    observed leg (fan-in waits for the LAST leg); a check no job produces is a
    parallel peer (its own single-member path); an absent/skipped predecessor
    contributes its observed span — zero when absent, never invented; a
    workflow whose graph can't be walked (a `needs:` cycle) FAILS OPEN to
    today's model for that workflow only, with the reason stamped in
    `fallback`; a `needs:` ref naming no job in the file is dropped (GitHub
    rejects such workflows, so the cell is theoretical); co-longest competing
    paths are both counted (`co_longest_n` — distinct MEMBER TUPLES only, so
    a zero-weight unobserved dependent never doubles a path — cell 10; the same
    both-counted principle as `_pole_frequencies`' co-slowest tie rule) with
    a deterministic lexicographic representative. Returns None when the PR
    carries no positive-span check."""
    capped = {n: min(s, caps.get(n, s)) for n, s in checks.items() if s and s > 0}
    if not capped:
        return None
    graph = job_graph or {}

    # Resolve each observed check to its (workflow, job-id) node; unresolved
    # checks are parallel peers. Matrix legs share a node — the node's weight
    # is its slowest observed leg, its representative the slowest leg's name
    # (ties: lexicographic, for a deterministic stamp).
    node_checks: dict[tuple[str, str], list[str]] = {}
    peers: list[str] = []
    for name in sorted(capped):
        node = _check_to_job_node(name, graph, crit_by_wf)
        if node is None:
            peers.append(name)
        else:
            node_checks.setdefault(node, []).append(name)

    candidates, fallback = _chain_path_pool(capped, graph, node_checks, peers)
    if not candidates:
        return None
    top = max(s for s, _, _ in candidates)
    tied = _dedupe_tied([(m, ways) for s, m, ways in candidates if s == top])
    chain = tied[0][0]
    # The runner-up is the WHOLE-CHAIN bound (pass-A review, PR-N2): the wait
    # that remains with every chain member's span zeroed — re-walk the same
    # pools with the members excluded. This handles fan-in diamonds (the
    # second path through the chain's own leaf survives at its non-shared
    # cost) and competitors sharing chain members (the shared segment is
    # zeroed, only the divergent tail competes) uniformly; an external peer
    # simply keeps its span. 0.0 when nothing remains.
    zeroed = {n: (0.0 if n in set(chain) else v) for n, v in capped.items()}
    z_candidates, _z_fallback = _chain_path_pool(zeroed, graph, node_checks, peers)
    runner_up = max((s for s, _, _ in z_candidates), default=0.0)
    # #45 — the per-PR win is `top - runner_up`, but a SINGLE sampled PR whose
    # competing path ran BELOW its population norm inflates that gap. Floor the
    # runner-up at the POPULATION p50 of the SURVIVING competitor's own pole leg
    # (`caps` already carries each check's population p50, raised to any bimodal
    # slow mode) — never below the p50 co-occurrence floor. electron: one PR's
    # 4640s runner-up vs the 4761s p50 leg produced a 6m56s (416s) claim; the p50
    # floor caps it at 4m55s (295s), moving toward the ~3m29s the report's own
    # OPT25 co-occurrence finding on the same job implies. The floor only ever
    # RAISES the runner-up (lowers the win), so a chainless/no-caps facts call
    # (caps={}) is byte-stable and the win never grows.
    #
    # CRITICAL: exclude the ZEROED chain members from the pole cap. A competing
    # path reaches the chain's leaf via a different parent, so its `members` list
    # still carries the chain leaf's NAME (with a zeroed span). `caps` is the
    # population p50 dict — untouched by the zeroing — so including a chain
    # member here would re-introduce the p50 of the very node being fixed as the
    # competitor's floor, understating (or zeroing) the win on every fan-in
    # diamond (b needs [a, x]: caps[b]=66 would floor the x-path competitor at 66
    # instead of its own 30). The floor must reflect only the surviving path's
    # OWN, non-chain legs.
    _chain_set = set(chain)
    runner_up_floor = 0.0
    for s, members, _ways in z_candidates:
        if round(s, 3) == round(runner_up, 3):
            pole_cap = max((caps.get(n, 0.0) for n in members if n not in _chain_set),
                           default=0.0)
            runner_up_floor = max(runner_up_floor, pole_cap)
    runner_up_eff = max(runner_up, runner_up_floor)
    win = max(round(top, 3) - round(runner_up_eff, 3), 0.0)
    return {
        "chain": chain,
        "member_spans_s": {n: capped[n] for n in chain},
        "chain_s": round(top, 3),
        "co_longest_n": sum(w for _, w in tied),
        # The wait remaining if the WHOLE chain were free (members zeroed) —
        # the bound the PR-N2 headline win derives from. 0.0 when nothing
        # competes. #45: floored at the population p50 of the competing path's
        # pole (`caps`), so a single sampled PR's below-norm runner-up can't
        # inflate the win; the per-PR identity `chain_win_s == chain_s -
        # runner_up_s` holds whenever the win is positive (the common case). The
        # sole exception is the degenerate corner where the floor raises the
        # runner-up ABOVE the chain top — then `chain_win_s` clamps to 0 (correct:
        # a competitor whose p50 exceeds this chain gates it) while
        # `chain_s - runner_up_s` would go negative; the rendered win is the
        # clamped 0, never the negative difference.
        "runner_up_s": round(runner_up_eff, 3),
        # Per-PR whole-chain win. The summary medians THESE (never the
        # difference of two independent medians — chain and runner-up lengths
        # correlate across PRs).
        "chain_win_s": round(win, 3),
        "fallback": fallback or None,
    }


def _cite_chain_in_opt21_evidence(findings: "list[dict[str, Any]]",
                                  chain_summary: "dict[str, Any] | None",
                                  jg: "dict[str, Any] | None",
                                  crit_by_wf: "dict[str, Any]") -> None:
    """ENG-1 PR-N3: append the measured-gate-chain note to every OPT21
    (unnecessary `needs:`) finding on the chain's own workflow - the edge
    OPT21 questions IS the serialization the headline measures. Evidence
    annotation only (sizing unchanged); idempotent (the note is never
    appended twice); a <2-member modal chain or an unresolvable chain
    workflow is a no-op."""
    modal = [str(m) for m in ((chain_summary or {}).get("modal_chain") or [])]
    if len(modal) < 2:
        return
    chain_wf = (_check_to_job_node(modal[-1], jg, crit_by_wf) or (None,))[0]
    if not chain_wf:
        return
    note = (" NOTE: this workflow hosts the measured gate chain "
            f"({' -> '.join(modal)}) — the `needs:` edge in "
            "question is the serialization the headline measures; "
            "removing a truly-unnecessary edge would collapse the "
            "chain into parallel checks.")
    for f in findings:
        if f.get("pattern") == "OPT21" and str(f.get("workflow_file")) == chain_wf:
            if note not in str(f.get("evidence") or ""):
                f["evidence"] = str(f.get("evidence") or "") + note


def _chain_summary(chain_facts: "list[dict[str, Any]]") -> "dict[str, Any] | None":
    """ENG-1 PR-N2: reduce the per-PR chain facts to the render-consumable
    aggregate (re-derived independently by the verifier): the p50 chain wait,
    the MODAL chain (most-frequent member tuple; ties lexicographic), how many
    PRs stamp it, the p50 competing-path bound, and the signed chain-vs-
    makespan divergence — POSITIVE when the chain sum exceeds the observed
    wall (re-run-inflated member spans), NEGATIVE when the wall exceeds the
    sum (queue gaps between stages; both possible by design, see the capture
    note on attempt scoping)."""
    if not chain_facts:
        return None
    chains = [tuple(cf.get("chain") or ()) for cf in chain_facts]
    counts: dict[tuple[str, ...], int] = {}
    for c in chains:
        counts[c] = counts.get(c, 0) + 1
    modal = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    makespans = [float(cf["makespan_s"]) for cf in chain_facts
                 if cf.get("makespan_s") is not None]
    makespan_p50 = round(statistics.median(makespans), 3) if makespans else None
    chain_p50 = round(statistics.median(
        [float(cf.get("chain_s") or 0.0) for cf in chain_facts]), 3)
    divergence = (round((chain_p50 - makespan_p50) / makespan_p50 * 100.0, 2)
                  if makespan_p50 else None)
    return {
        "n": len(chain_facts),
        "chain_p50_s": chain_p50,
        "modal_chain": list(modal[0]),
        "modal_n": modal[1],
        "runner_up_p50_s": round(statistics.median(
            [float(cf.get("runner_up_s") or 0.0) for cf in chain_facts]), 3),
        # Median of the PER-PR whole-chain wins — never p50(chain) minus
        # p50(runner_up): the two lengths correlate across PRs, and the
        # difference of independent medians can overstate the honest win 10x
        # (pass-A probe, PR-N2 review).
        "chain_win_p50_s": round(statistics.median(
            [float(cf.get("chain_win_s") or 0.0) for cf in chain_facts]), 3),
        "makespan_p50_s": makespan_p50,
        "divergence_pct": divergence,
    }


def _stamp_chain_facts(
    repr_shas: list[str],
    per_sha_checks: "list[dict[str, float]]",
    spine_checks: set,
    caps: dict[str, float],
    job_graph: "dict[str, dict[str, dict[str, Any]]] | None",
    crit_by_wf: dict[str, dict[str, Any]],
    sha_intervals: "dict[str, dict[str, tuple[str, str]]]",
) -> "list[dict[str, Any]]":
    """PR-N2 requirement (i), as a pure seam so its revert turns a unit test
    red: BOTH legs of the chain cross-check are scoped to the SPINE's check
    set (`pr_check_p50` post-drop — non-PR-gating and non-required checks
    removed). A dropped rider can neither be nor extend a stamped chain (the
    mastra-class phantom-gate shape), and it cannot inflate the empirical
    makespan either — the two legs of the divergence comparison must measure
    the same population or the disclosure misattributes the gap."""
    out: list[dict[str, Any]] = []
    for sha, sha_checks in zip(repr_shas, per_sha_checks):
        scoped = {n: v for n, v in sha_checks.items() if n in spine_checks}
        cf = _chain_facts_for_pr(scoped, caps, job_graph, crit_by_wf)
        if cf is None:
            continue
        intervals = {n: iv for n, iv in (sha_intervals.get(sha) or {}).items()
                     if n in spine_checks}
        ms = _pr_makespan(intervals, caps)
        out.append({
            "sha": sha,
            **cf,
            "makespan_s": ms,
            # The basis describes a measurement — never stamped for one that
            # doesn't exist (makespan None when no interval survived).
            **({"makespan_basis":
                "latest-attempt check-run intervals, span-capped per check"}
               if ms is not None else {}),
        })
    return out


def _pr_makespan(intervals: "dict[str, tuple[str, str]]",
                 caps: dict[str, float]) -> "float | None":
    """ENG-1 PR-N1: the per-PR empirical makespan — max(end) − min(start) over
    the PR's latest-attempt check-run intervals, each interval CLAMPED to its
    check's `_pole_caps` bound before the span is taken. Raw-timestamp
    arithmetic is banned here: a re-run hours later inflates a check-run span
    in exactly the way the caps exist to defeat (the collector body records an
    80s job whose check-run read 1871s)."""
    starts: list[float] = []
    ends: list[float] = []
    for name, (start, end) in intervals.items():
        a, b = _parse_dt(start), _parse_dt(end)
        if not (a and b):
            continue
        s = a.timestamp()
        e = b.timestamp()
        cap = caps.get(name)
        if cap is not None:
            e = min(e, s + float(cap))
        if e > s:
            starts.append(s)
            ends.append(e)
    if not starts:
        return None
    return round(max(ends) - min(starts), 3)


def _check_presence(per_sha_checks: list[dict[str, float]],
                    candidates: "set[str] | frozenset[str]") -> tuple[dict[str, int], int]:
    """Per candidate check, how many sampled PRs it actually ran on, and the denominator
    (PRs that ran >=1 candidate check). The presence signal behind rare-pole demotion: a
    check on a small fraction of PRs is opt-in/conditional (label-gated, path-filtered),
    not the gate a typical PR waits on. Computed from the per-PR check maps already in
    hand (no extra gh call)."""
    maps = [{c for c in m if c in candidates} for m in per_sha_checks]
    maps = [s for s in maps if s]
    present: dict[str, int] = {c: 0 for c in candidates}
    for s in maps:
        for c in s:
            present[c] += 1
    return present, len(maps)


def _pole_caps(job_p50_all: dict[str, float],
               job_bimodal_all: dict[str, dict[str, Any]]) -> dict[str, float]:
    """The reliable per-check upper bound for capping an inflatable per-PR check-run span: the
    job p50, RAISED to the bimodal SLOW-mode median (`high_p50_s`) so a genuinely-slow mode isn't
    clamped to the fast p50. A single source for BOTH the pole-frequency ranking basis
    (`_pole_frequencies`) and the per-population magnitudes (`_segment_pr_populations`), so the
    headline the engine crowns and the `populations` `verify_report` re-derives from can never
    disagree about which check is the per-PR pole (they cap on identical values)."""
    caps: dict[str, float] = dict(job_p50_all)
    for name, bi in job_bimodal_all.items():
        hi = (bi or {}).get("high_p50_s")
        if hi:
            caps[name] = max(caps.get(name, 0.0), float(hi))
    return caps


def _partition_fileless_checks(
    pr_check_p50: dict[str, float],
    check_timing_source: dict[str, str],
    crit_by_wf: dict[str, dict[str, Any]],
    job_graph: "dict[str, dict[str, dict[str, Any]]] | None",
) -> tuple[dict[str, float], dict[str, float]]:
    """Issue #12: split the merge-wait spine into the JOB-GROUNDABLE crowning basis and the
    FILELESS/managed set that must never crown the headline.

    A fileless/managed status check — a bot gate, a label gate, an external app check — produces
    NO sampled workflow job, so its only timing is the `pr_check_runs` span: the wall from when the
    check was CREATED to when it completed. For a label/bot gate that span is PR-LIFETIME
    status-gating latency (a label that sat open for 8 days reads as an 8-day "CI wait"), not CI
    wall-clock. `_pole_caps` can only de-inflate a span it has a sampled job p50 for, so a fileless
    check is never capped and its raw span would crown `critical_path_check` /
    `chain_summary.makespan_p50_s` (electron/electron: `Backport Labels Added` crowning ~8 days
    while the file-backed poles trace <1% of it). The product rule: PR-lifetime latency of a
    fileless check is NEVER a valid basis for the CI merge-wait headline.

    "Job-groundable" reuses the existing check->file binders (never a re-derivation): a check is
    groundable iff it has a sampled developer-timed job (`workflow_jobs`), OR the sampled-timing
    mapper resolves it, OR the SCANNED job graph maps it to a workflow file (a triage-skipped but
    file-backed check the crown-recovery pass can still recover — it is real CI compute, only its
    per-run drill was skipped). A check with NO workflow anywhere (bot/app/label gate) is fileless.

    Returns (job_groundable, fileless) as two disjoint dicts partitioning `pr_check_p50`. The
    caller drops the fileless set from the crowning basis (ranking, critical path, populations,
    chain/makespan all re-derive from `pr_check_p50`) and stamps it separately for disclosure."""
    jg = job_graph or {}
    groundable: dict[str, float] = {}
    fileless: dict[str, float] = {}
    for name, span in pr_check_p50.items():
        if (check_timing_source.get(name) == "workflow_jobs"
                or _map_check_to_job(name, crit_by_wf, require_developer_timing=True) is not None
                or (bool(jg) and _check_to_job_node_scanned(name, jg) is not None)):
            groundable[name] = span
        else:
            fileless[name] = span
    return groundable, fileless


def _pole_frequencies(per_sha_checks: list[dict[str, float]],
                      candidates: "set[str] | frozenset[str]",
                      caps: dict[str, float] | None = None) -> dict[str, int]:
    """Per candidate check, on how many sampled PRs it is the ACTUAL critical path — i.e. the
    SLOWEST candidate check that PR ran (its concurrent checks run in parallel, so the slowest
    is the one the developer waits on). This is the honest "is it the merge gate?" signal behind
    the recurrence floor: a check that is the slowest on many PRs genuinely gates; one that is
    NEVER the slowest (however often it runs) is not the gate. Computed from the per-PR check
    maps already in hand (no extra gh call).

    Each per-PR span is CAPPED at `caps` (the job-p50 bound from `_pole_caps`) before the argmax
    — the SAME de-inflation `_segment_pr_populations` applies to build `populations`. Without it
    the ranker would argmax on raw check-run spans (inflatable by queue / re-run time), so a
    queue-spiked light check could out-rank a real gate AND the headline could disagree with the
    `populations` `verify_report.check_headline_pole_actually_gates` re-derives from. `caps=None`
    (tests / legacy) means no capping.

    TIES credit EVERY co-slowest check on the PR (not just the first): two suites with identical
    (capped) durations both genuinely gate that PR, so both earn the recurrence credit —
    otherwise insertion order would starve one co-equal gate of all credit and wrongly demote it.
    The sum can therefore exceed the PR count (it is a per-check gate rate, not a partition). Kept
    behaviour-coupled to `verify_report._vr_pole_frequencies` by a self-test."""
    freq: dict[str, int] = {c: 0 for c in candidates}
    for m in per_sha_checks:
        scoped: dict[str, float] = {}
        for c, v in m.items():
            if c in candidates and isinstance(v, (int, float)):
                # Round to 0.1s to match `_segment_pr_populations`'s `round(eff, 1)` EXACTLY, so the
                # engine's argmax and the verifier's re-derivation from `populations` can never
                # disagree on a sub-decisecond tie (a no-op on real second-granularity data, but the
                # code shouldn't rely on that — adversarial-review latent gap).
                scoped[c] = round(min(v, caps[c]) if caps and c in caps else v, 1)
        if not scoped:
            continue
        mx = max(scoped.values())
        for c, v in scoped.items():
            if v == mx:
                freq[c] += 1
    return freq


def _rank_spine_present_first(
    pr_check_p50: dict[str, float],
    per_sha_checks: list[dict[str, float]],
    req_names: "frozenset[str] | set[str]",
    caps: dict[str, float] | None = None,
) -> tuple[tuple[tuple[str, float], ...], dict[str, int], int, dict[str, int]]:
    """Order the merge-wait spine so checks that ACTUALLY gate the merge — the slowest job a PR
    waits on, on at least `_POLE_RECUR_FLOOR` sampled PRs — rank ABOVE one-path outliers, each
    tier by p50 desc, so the headline pole is a genuine recurring gate, not a lightweight
    always-present check nor a single-PR giant. A required check gates by definition and is
    EXEMPT (never demoted). INERT (plain p50 order) below `_RARE_PRESENCE_MIN_PR` PRs, where the
    frequency is noise. Only REORDERS (no drops). Returns (sorted_tuple, present_counts, n_pr,
    pole_freq) — presence AND pole-frequency counts are emitted so the renderer/report can show
    both how often each check ran and how often it was the actual gate.

    Why pole-FREQUENCY, not presence>half (the bug this fixes — expo/expo): on a path-partitioned
    monorepo each heavy suite runs on a MINORITY of PRs, so a presence>50% cutoff demotes the
    whole heavy-job set and crowns an always-present check that is the actual bottleneck on ZERO
    PRs. Ranking by how often a check is the real critical path picks the recurring gates a
    developer actually waits on (validated on expo: playwright-windows/android-build lead;
    check-packages, the actual pole on 0/20 PRs, drops out of the headline)."""
    present, n_pr = _check_presence(per_sha_checks, set(pr_check_p50))
    pole_freq = _pole_frequencies(per_sha_checks, set(pr_check_p50), caps)
    tier_ok = n_pr >= _RARE_PRESENCE_MIN_PR

    def is_rare(c: str) -> bool:
        # Rare == not a recurring actual gate: the slowest job on FEWER than the floor of sampled
        # PRs. Required checks gate by definition (exempt). Below the min-sample floor the
        # frequency is noise, so demote nothing (plain p50 order).
        return bool(tier_ok and c not in req_names
                    and pole_freq.get(c, 0) < _POLE_RECUR_FLOOR)

    order = tuple(sorted(pr_check_p50.items(),
                         key=lambda kv: (1 if is_rare(kv[0]) else 0, -kv[1])))
    return order, present, n_pr, pole_freq


def _order_drill_matrices(
    matrices: "list[tuple[str, float]]",
    impact: "Callable[[str, float], float]",
    is_typical: "Callable[[str], bool]",
) -> "list[tuple[str, float]]":
    """Order the mappable job matrices for drill-bundle CAPTURE exactly as
    ``blocking_path.render`` orders the poles it RENDERS: the gate (the slowest-median
    typical matrix, already first in the present-first spine) leads, then the remaining
    TYPICAL matrices by impact, then the rare/opt-in ones by impact.

    Keeping this identical to the renderer is load-bearing: the captured (drilled) pole
    set must equal the rendered pole set. A flat impact sort here pulls a rare-but-slow
    conditional pole (e.g. a path-filtered `build` leg present on a minority of PRs, but
    long when it runs) above a TYPICAL pole — so capture drills the rare one while the
    renderer demotes it and renders the typical pole instead. That typical pole then has
    no captured log, and ``_match_key``'s exact-workflow-stem rule binds it to a SIBLING
    pole's bundle (a same-workflow drill), making the report show the wrong job's
    timeline/magnitudes. Tiering here keeps every rendered pole's own log captured.
    ``is_typical`` MUST encode the same `_RARE_PRESENCE_*` / required-check rule the
    renderer uses, so the two never disagree about which pole is the rare one."""
    if not matrices:
        return []
    gate = matrices[0]
    rest = matrices[1:]
    rest_typ = sorted((m for m in rest if is_typical(m[0])),
                      key=lambda m: -impact(m[0], m[1]))
    rest_rare = sorted((m for m in rest if not is_typical(m[0])),
                       key=lambda m: -impact(m[0], m[1]))
    return [gate, *rest_typ, *rest_rare]


def _run_wall_s(r: dict[str, Any]) -> float | None:
    """Wall-time of a SINGLE run from run-LIST metadata alone (no per-run job fetch):
    `run_started_at`|`created_at` → `updated_at`. None when the timestamps are
    missing/unparseable — the caller must treat that as UNKNOWN, never as 0/fast."""
    return _duration_s(r.get("run_started_at") or r.get("created_at"),
                       r.get("updated_at"))


def _max_sampled_run_wall_s(runs: list[dict[str, Any]]) -> float:
    """Slowest KNOWN wall-time among `runs`, from run-list metadata alone. Runs whose
    duration is unknown contribute 0.0; empty → 0.0. This folds unknown→0.0, so it is for
    the debug/disclosure line only — the triage DECISION uses `_should_triage_workflow`,
    which refuses to triage on any unknown-duration run rather than masking it here."""
    return max((_run_wall_s(r) or 0.0 for r in runs), default=0.0)


def _should_triage_workflow(runs: list[dict[str, Any]]) -> bool:
    """True when this workflow's per-run job fetch can be SKIPPED — every sampled run's
    duration is known AND the slowest is a positive wall-time strictly under
    `_TRIAGE_WALLCLOCK_FLOOR_S`, so it can't hold the merge pole (a triaged workflow still
    contributes to the cross-workflow floor via the stub's `concurrent_wall_p50`). False
    when there are no runs, OR ANY sampled run's wall-time is unknown (missing/unparseable
    timestamps): an unknown run could have been long, so we never triage on incomplete data —
    the workflow is fetched, never silently skipped. (A `max()` that folded unknown→0.0 would
    let a fast run mask an unmeasured long one in the same window.) The `< floor` is strict,
    so a run at exactly the floor is fetched; the `0.0 <` lower bound means a degenerate
    all-zero-duration workflow (identical start/end stamps) is also fetched, not triaged."""
    if not runs:
        return False
    durs = [_run_wall_s(r) for r in runs]
    if any(d is None for d in durs):
        return False
    return 0.0 < max(durs) < _TRIAGE_WALLCLOCK_FLOOR_S


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile; pct in [0, 100]. Empty → 0.0."""
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


# =============================================================================
# Per-workflow data collection
# =============================================================================

def _list_workflows(client: GhClient, repo: str) -> list[dict[str, Any]] | None:
    """Every workflow registered on the repo, or None when the fetch FAILED.

    PAGINATED: a monorepo can register >100 workflows, and keeping page 1 dropped
    the rest from the scan silently.

    None (fetch failed), never `[]`: `[]` reads downstream as "this repo has no
    workflows", which would render a confident, complete-looking audit of NOTHING —
    the worst instance of the silent-drop class, since every later phase is scoped
    by this list. The caller aborts with a disclosed coverage gap instead."""
    return _paginate(client, f"repos/{repo}/actions/workflows", "workflows")


def _run_list(client: GhClient, endpoint: str) -> list[dict[str, Any]] | None:
    """The `workflow_runs` array of a run-list endpoint, or None when the fetch
    FAILED (gh error / rate-limit exhaustion — already counted in `client.errors`)
    or the body was malformed.

    None, never `[]`. These run-list endpoints are the entry point to EVERY
    per-workflow measurement, and `[]` reads downstream as "this workflow has no
    runs" — so a rate-limited fetch would make the workflow vanish from the audit
    while the report still rendered as though it had been measured. A failed fetch
    must stay distinguishable from an empty result; the callers disclose it BY NAME
    (`data_sources.run_list_fetch_failures`).

    NOT paginated on purpose: `per_page` here IS the sample size (the latest N
    runs), not a page of a set we intend to walk to completion."""
    doc = client.json(endpoint)
    if not isinstance(doc, dict):
        return None
    runs = doc.get("workflow_runs")
    if isinstance(runs, list):
        return runs
    # A dict with no (or a non-list) `workflow_runs` is MALFORMED, not empty. Returning
    # `[]` here would launder it into "this workflow has no runs" — with `errors`
    # unbumped, no `_note_run_list_gap`, and no `unavailable` marker, i.e. the workflow
    # silently deleted from the audit while the report renders as though it were
    # measured. `_paginate` already fails on exactly this shape; so does this.
    client._bump(error=True)
    logger.warning(
        "gh run list %s: the body is malformed ('workflow_runs' is %s, not a list) — "
        "failing the fetch. This is NOT 'the workflow has no runs'; it is a coverage "
        "gap, and it will be disclosed by name.",
        endpoint, type(runs).__name__)
    return None


def _normalize_pin(created_before: str | None) -> str | None:
    """Parse a `created_before` pin (any Z- or +00:00-suffixed ISO 8601
    timestamp) and re-emit gh's canonical `%Y-%m-%dT%H:%M:%SZ` form, safe to
    interpolate unencoded into a `created=` query clause.

    The pin source is `scan.py`'s `scanned_at = datetime.now(timezone.utc)
    .isoformat()`, which ends `+00:00` — the literal `+` decodes as a space
    in an unencoded query string and silently breaks the filter (matching a
    different run window than intended). Always go through this helper
    before putting a pin in a gh query. Returns None (no pin) when
    `created_before` is falsy or fails to parse.

    A pin with a non-UTC offset (e.g. a user-supplied `--created-before`
    ending `+05:00`) is CONVERTED to UTC before the canonical `Z` form is
    emitted — the `Z` is a UTC designator, so relabeling the local wall-clock
    fields without converting would shift the sampling window by the offset
    (up to ±14h). Accepts both `Z` and lowercase `z` UTC suffixes."""
    if not created_before:
        return None
    try:
        dt = _dt.datetime.fromisoformat(
            created_before.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp (no offset) is assumed already-UTC; astimezone on an
    # aware one converts to UTC so the appended `Z` is truthful.
    if dt.tzinfo is not None:
        dt = dt.astimezone(_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# The UNPINNED window's "now", resolved ONCE per process (see `_unpinned_now`).
_UNPINNED_NOW: _dt.datetime | None = None
_UNPINNED_NOW_LOCK = threading.Lock()


def _unpinned_now() -> _dt.datetime:
    """The single "now" every unpinned 30-day window in this run is measured against.

    Resolved once, then reused. `datetime.now()` has SECOND resolution and a real
    collection runs for minutes, so re-reading the clock per call let the 30-day window
    SLIDE mid-run: two workflows' volume probes issued a second apart were counted over
    two different windows. Small, but it is exactly the drift that makes a "measured"
    number irreproducible — and it made one logical query stringify differently at
    different moments, so the prefetch buffer (keyed by the endpoint string) would miss
    and silently pay for the same call twice. One window per run, fixed at first use.

    (A `--created-before` pin bypasses this entirely — that path was already stable.)"""
    global _UNPINNED_NOW
    with _UNPINNED_NOW_LOCK:
        if _UNPINNED_NOW is None:
            _UNPINNED_NOW = _dt.datetime.now(_dt.timezone.utc)
        return _UNPINNED_NOW


def _reset_unpinned_now() -> None:
    """Forget the pinned "now" so the NEXT run resolves a fresh one.

    The window must be fixed for a RUN, not for the interpreter: a process that audits
    several repos in sequence (the hunt/dogfood harnesses import `collect` and call it in
    a loop) would otherwise measure every later repo's 30-day volume against the FIRST
    call's clock — hours stale, and stale in a way no report would disclose. `collect()`
    calls this on entry, so "one window per run" is literally true."""
    global _UNPINNED_NOW
    with _UNPINNED_NOW_LOCK:
        _UNPINNED_NOW = None


def _window_30d(created_before: str | None) -> tuple[str, str | None]:
    """(lower-bound-iso, upper-bound-iso-or-None) for a 30-day window.

    Unpinned: the window ends "now" and only the lower bound is expressed
    (`created>=now-30d`), preserving the original behavior. Pinned to
    `created_before`: the window ENDS at the pin (`created=now-30d..pin`),
    so a regen samples the exact same window the original audit did instead
    of drifting forward as new runs land. The pin is the audit's scan time.

    "Now" is `_unpinned_now()` — fixed for the process, never re-read per call."""
    upper = _normalize_pin(created_before)
    end = (_dt.datetime.strptime(upper, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
           if upper else _unpinned_now())
    since = (end - _dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return since, upper


# ── Config-era boundary (issue #66) ──────────────────────────────────────────
# When a workflow file changes mid-sample-window, the runs BEFORE the change and
# the runs AFTER it measure two DIFFERENT CI configurations. Blending them yields
# a stale headline, a drill that mixes the old step layout with the new (the
# fabricated "guard runs twice, once whole and once sharded" cross-era synthesis),
# and a recoverable ceiling drilled from the retired config that exceeds the
# typical wait under the current one. The fix partitions each workflow's sampled
# runs at its last-change commit so a workflow's spine + drill contributions never
# blend the change's own before/after step layouts (the fabricated retired-vs-current
# synthesis is structurally impossible — one side is always dropped whole); on the
# disclosed-pre side, a workflow that changed AGAIN earlier is narrowed to the single
# prior era whenever a sampled run pins it (see `_partition_config_era`).
#
# Sufficiency floor: a post-change sample is trustworthy for the spine/drill once
# it reaches `_RARE_PRESENCE_MIN_PR` runs — the SAME sample-size floor below which
# `pr_critical_path` already treats a check's frequency as noise. Below it, the
# post-change window is too thin to drill, so the audit measures the PRE-change
# era instead and discloses that it reflects the previous configuration.


def _commit_date(entry: object) -> str | None:
    """The ISO date of one `commits?path=` list entry — committer date preferred (when the
    change landed on the branch), author date the fallback. Both are ISO 8601 `...Z`, directly
    string-comparable to a run's `created_at`. None on any malformed entry."""
    if not isinstance(entry, dict):
        return None
    commit = entry.get("commit")
    if not isinstance(commit, dict):
        return None
    for who in ("committer", "author"):
        meta = commit.get(who)
        if isinstance(meta, dict) and meta.get("date"):
            return str(meta["date"])
    return None


def _commit_sha(entry: object) -> str | None:
    """The commit SHA of one `commits?path=` list entry (top-level `sha`), or None on a malformed
    entry. Content-era classification (issue #77) fetches the workflow blob AT this commit — the
    boundary commit's version is the POST-change blob, its predecessor's the PRE-change blob."""
    if isinstance(entry, dict) and entry.get("sha"):
        return str(entry["sha"])
    return None


def _workflow_change_boundary(
        client: "GhClient", repo: str, wf_path: str, created_before: str | None
) -> tuple[str | None, str | None, str | None, str | None]:
    """The `(last, prev, last_sha, prev_sha)` of the two most-recent commits that touched `wf_path`
    — `last`/`prev` are ISO dates (the config-era boundary and the era immediately before it),
    `last_sha`/`prev_sha` the matching commit SHAs (for the content-era blob fetch, issue #77).
    at/before the audit window's close — `last` is the config-era boundary for that workflow
    file; `prev` is the boundary of the era IMMEDIATELY before it (None when the file was
    touched only once at/before the pin). ONE `commits?path=&per_page=2` REST call per workflow
    (the runs API exposes no workflow-content hash, so per-run content diffing would cost one
    `/contents/` fetch per run, N≫K; this commit-history lookup is O(1) per workflow and strictly
    cheaper — `per_page=2` is still a SINGLE call, so the call budget is unchanged). Pinned with
    `&until=<pin>` so an edit LATER than the audited window is never treated as the boundary
    (a regen samples the same window). `(None, None, None, None)` on any failure / empty history —
    the caller then no-ops (byte-identical to the pre-#66 engine); a workflow that did not change in
    the window has no boundary and so NEVER triggers a content-era blob fetch (issue #77).

    Why `prev` (issue #66, multi-boundary): if a workflow changed TWICE inside the window, the
    runs BEFORE `last` themselves span two eras. `prev` lets `_partition_config_era`'s disclosed-pre
    fallback keep ONLY the `[prev, last)` era, so the pre-side is a single era too — except in the
    rare ≥3-change corner where that era holds no sampled run, which falls back to a disclosed wider
    set (per_page=2 sees only the two most-recent boundaries; deeper history isn't fetched).

    The debug line records the endpoint only (path + query), never a response body, per the
    STARSLING_LOG_LEVEL disclosure contract; the workflow path DOES appear."""
    params = f"path={wf_path}&per_page=2"
    pin = _normalize_pin(created_before)
    if pin:
        params += f"&until={pin}"
    endpoint = f"repos/{repo}/commits?{params}"
    logger.debug("config-era: last-change commit lookup %s", endpoint)
    data = client.json(endpoint, allow_missing=True)
    if not isinstance(data, list) or not data:
        return None, None, None, None
    last = _commit_date(data[0])
    prev = _commit_date(data[1]) if len(data) > 1 else None
    last_sha = _commit_sha(data[0])
    prev_sha = _commit_sha(data[1]) if len(data) > 1 else None
    return last, prev, last_sha, prev_sha


def _workflow_blob_at(client: "GhClient", repo: str, wf_path: str, ref: str) -> str | None:
    """The git BLOB sha of `wf_path` as it exists at commit `ref` — ONE
    `contents/{wf_path}?ref={ref}` REST call (issue #77). The blob sha is the content identity: two
    refs whose workflow file is byte-identical share it, and any edit changes it. `allow_missing=True`
    — a ref that lacks the file (deleted / unreachable) or a failed fetch returns None, so the caller
    falls back to timestamp classification for that ref. The debug line records the endpoint only
    (path + ref), never a body, per the STARSLING_LOG_LEVEL disclosure contract."""
    if not ref:
        return None
    endpoint = f"repos/{repo}/contents/{wf_path}?ref={ref}"
    logger.debug("config-era: workflow blob lookup %s", endpoint)
    data = client.json(endpoint, allow_missing=True)
    if isinstance(data, dict) and data.get("sha"):
        return str(data["sha"])
    return None


def _timestamp_straddles(runs: list[dict[str, Any]], boundary: str) -> bool:
    """True iff `runs` has at least one run on EACH side of `boundary` by `created_at` — the cheap,
    zero-fetch gate that decides whether a workflow is straddle-eligible (issue #77). Only a
    timestamp-straddling workflow pays for the content-era blob fetches; a workflow whose whole
    sample sits one side of the boundary (or has no boundary) never fetches a blob — the hard
    byte-identity requirement for non-straddling repos. Mirrors `_partition_config_era`'s own
    `not pre or not post` straddle test on the raw `created_at`."""
    if not boundary:
        return False
    has_pre = has_post = False
    for r in runs:
        ca = str(r.get("created_at") or "")
        if not ca:
            continue
        if ca < boundary:
            has_pre = True
        else:
            has_post = True
        if has_pre and has_post:
            return True
    return False


def _resolve_content_eras(
        client: "GhClient", repo: str, wf_path: str, runs: list[dict[str, Any]],
        last_sha: str | None, prev_sha: str | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Classify each sampled run of a STRADDLING workflow by the workflow-file CONTENT its
    `head_sha` carries, not by its `created_at` (issue #77 — the correctness fix). A `pull_request`
    run executes the workflow file from the PR's OWN head, so a PR that already carried the new CI
    config runs the NEW workflow even while its `created_at` pre-dates the merge boundary (and the
    converse: a stale branch merged AFTER the boundary runs the OLD config). Timestamp classification
    misreads both; content identity does not.

    Returns `(era_by_sha, basis)`:
      * `era_by_sha` — head_sha -> "pre" | "post", for every unique sampled head_sha whose workflow
        blob MATCHES the pre- or post-boundary blob. A head whose blob matches NEITHER (an unrelated
        intermediate edit carried on that branch) is OMITTED, so the caller falls back to timestamp
        for it.
      * `basis` — bookkeeping stamped onto the era fact: `content` / `timestamp` sampled-run counts
        and whether the boundary blobs resolved.

    **API cost (the worst-case bound):** ≤2 calls for the boundary blobs (POST at `last_sha`, PRE at
    `prev_sha`) + ≤1 call per UNIQUE sampled head_sha (memoized here), and ONLY for a workflow the
    caller already found to timestamp-straddle. If the POST blob fails to resolve, content
    classification is impossible, so we return empty (all runs fall to timestamp) WITHOUT paying for
    the per-head fetches."""
    post_blob = _workflow_blob_at(client, repo, wf_path, last_sha or "")
    basis: dict[str, Any] = {"content": 0, "timestamp": 0, "boundary_blob_resolved": bool(post_blob)}
    if not post_blob:
        # No POST blob → nothing to match against; every run stays timestamp-classified. Don't spend
        # the per-head fetches on a classification that can't succeed.
        basis["timestamp"] = len({str(r.get("head_sha") or "") for r in runs if r.get("head_sha")})
        return {}, basis
    pre_blob = _workflow_blob_at(client, repo, wf_path, prev_sha or "") if prev_sha else None
    era_by_sha: dict[str, str] = {}
    for sha in dict.fromkeys(str(r.get("head_sha") or "") for r in runs):
        if not sha:
            continue
        blob = _workflow_blob_at(client, repo, wf_path, sha)   # 1 call per UNIQUE head (deduped by dict.fromkeys above)
        if blob and blob == post_blob:
            era_by_sha[sha] = "post"
            basis["content"] += 1
        elif blob and pre_blob and blob == pre_blob:
            era_by_sha[sha] = "pre"
            basis["content"] += 1
        else:
            basis["timestamp"] += 1   # NEITHER blob → timestamp fallback for this head
    return era_by_sha, basis


def _run_config_era(run: dict[str, Any], boundary: str,
                    content_era: dict[str, str] | None) -> str | None:
    """The config era ("pre"/"post") of one run relative to `boundary` — CONTENT-first (issue #77),
    timestamp-fallback. `content_era` (head_sha -> era) wins when it resolves the run's head_sha;
    otherwise the run is placed by `created_at` vs `boundary` exactly as before. None when the run
    is unplaceable (no content match and no `created_at`)."""
    if content_era:
        e = content_era.get(str(run.get("head_sha") or ""))
        if e in ("pre", "post"):
            return e
    ca = str(run.get("created_at") or "")
    if not ca:
        return None
    return "post" if ca >= boundary else "pre"


def _partition_config_era(
        runs: list[dict[str, Any]], boundary: str | None, prev_boundary: str | None = None,
        content_era: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Split a workflow's sampled `runs` at its config-era `boundary` (issue #66).

    Returns `(kept_runs, era_fact)`:
      * `era_fact is None` — the sample does NOT straddle (all runs one side of the
        boundary, or no boundary): a no-op, `kept_runs is runs`, byte-identical to
        the pre-#66 engine. A workflow that did not change keeps its FULL sample
        (L2: partition only the workflows that actually straddle).
      * STRADDLE, post-change sample sufficient (`>= _RARE_PRESENCE_MIN_PR`): keep
        ONLY the post-change runs (drop the retired config), `rule="post_only"`,
        `kept_era="post"` — measures the CURRENT config on a narrowed window.
      * STRADDLE, post-change sample too thin: keep the pre-change runs (drop the
        too-few post runs), `rule="disclosed_pre"`, `kept_era="pre"` — measures the
        PREVIOUS config and flags the era disclosure. When `prev_boundary` is set AND
        it falls inside the pre-change runs (the workflow changed AGAIN earlier in the
        window — a MULTI-boundary sample), the kept set is narrowed to the single
        `[prev_boundary, boundary)` era so the pre-side is one era too, never a blend
        of the two OLDER configs. `multi_change`/`kept_count` record when that fired.
        (In the common single-change case `prev_boundary` predates the whole window,
        so the narrow keeps every pre run — byte-identical to the un-narrowed path.)

    NOTE (issue #74): the `disclosed_pre` decision here is made from RUN counts alone. Only
    `_era_resolve_thin_flip` (which sees the per-PR check observations, pre-drill) can tell whether the
    kept PRE era actually produced a gate-bearing check in the sample; when it did NOT, it FLIPS this
    fact to `post_only_thin` AND re-drills the spine from the thin post sample, rather than let a
    check-empty pre era render a "measures the previous configuration" disclosure over an all-post
    enumeration (or a pre-era timing under a "measures the new configuration" claim).

    In BOTH straddle branches the change's OWN before/after never blend — one side is
    always dropped whole — so the fabricated retired-vs-current synthesis is structurally
    impossible. The disclosed-pre side is a single era whenever a sampled run pins the
    `[prev, last)` window; in the rare ≥3-change corner where it does not (only the two
    most-recent boundaries are fetched), the kept set falls back to a disclosed wider set
    that may span older eras — best-effort, never silent.

    **Content classification (issue #77).** When `content_era` (head_sha -> "pre"/"post", from
    `_resolve_content_eras`) resolves a run's head_sha, that CONTENT era wins over the run's
    `created_at` — a PR that already carried the new workflow classifies post even though it ran
    before the merge boundary, and a stale branch merged after the boundary running the old workflow
    classifies pre. A head the content map does not resolve (or when no map is supplied — the
    timestamp-only path, byte-identical to the pre-#77 engine) falls back to `created_at`. The
    multi-boundary `[prev, last)` narrowing below stays `created_at`-based: the content blobs
    identify only the last boundary's two sides, not the older eras a deeper history would."""
    if not boundary:
        return runs, None
    # A run with NO content match AND no `created_at` can't be placed in an era, so it must NOT tilt
    # the straddle detection or silently land in the pre bucket (an empty string sorts before every
    # ISO date). Exclude it from both sides — if there's no straddle we still return the FULL `runs`
    # below, so a malformed run is only ever dropped from an era-partitioned sample it couldn't
    # belong to. `_run_config_era` is CONTENT-first, `created_at`-fallback (issue #77).
    pre = [r for r in runs if _run_config_era(r, boundary, content_era) == "pre"]
    post = [r for r in runs if _run_config_era(r, boundary, content_era) == "post"]
    if not pre or not post:
        return runs, None
    fact: dict[str, Any] = {"boundary": boundary, "pre_count": len(pre),
                            "post_count": len(post),
                            "sufficiency_min": _RARE_PRESENCE_MIN_PR}
    if len(post) >= _RARE_PRESENCE_MIN_PR:
        fact.update(kept_era="post", rule="post_only")
        return post, fact
    # disclosed_pre: keep the pre-change era. If the sample ALSO straddles an EARLIER
    # boundary (`prev_boundary` lands among the pre runs), narrow to the single
    # `[prev_boundary, boundary)` era — otherwise a twice-changed workflow's pre side
    # would blend the two older configs, the very class #66 kills. Fall back to the
    # full pre set if the narrow would empty it (the immediately-prior era has no
    # sampled run — keeping the wider set is the honest best-effort, still disclosed).
    kept = pre
    multi = False
    if prev_boundary:
        narrowed = [r for r in pre if str(r.get("created_at") or "") >= prev_boundary]
        if narrowed and len(narrowed) < len(pre):
            kept, multi = narrowed, True
    fact.update(kept_era="pre", rule="disclosed_pre",
                prev_boundary=prev_boundary, multi_change=multi,
                kept_count=len(kept))
    return kept, fact


def _era_pr_side(ts: str, fact: dict[str, Any], head_sha: str = "") -> str | None:
    """Which era a sampled PR falls in relative to a straddle `fact`: `"kept"` (the era the report
    describes), `"dropped"` (the OTHER configuration), or `None` when it is unplaceable.

    CONTENT-first (issue #77): when `head_sha` is given and the fact carries a `content_era_by_sha`
    entry for it (the workflow blob that head actually ran), the PR is placed by that CONTENT era,
    not its `created_at`/`ts` — so a fix-PR that carried the new workflow while running before the
    merge boundary lands on the POST side (dropped, for a disclosed_pre fact) instead of masquerading
    as a kept pre-era gate PR. A head the content map does not resolve, or a call with no `head_sha`
    (the pure-timestamp callers / tests), falls back to `ts` — byte-identical to the pre-#77 rule.

    Mirrors `_partition_config_era`'s kept-run selection so the CHECK enumeration and the SPINE runs
    agree on the era cut:
      * `post_only` (kept_era="post") — kept iff the PR is on the POST side.
      * `disclosed_pre` (kept_era="pre") — kept iff the PR is on the PRE side, AND (when the workflow
        also changed earlier in the window, `multi_change`) `ts >= prev_boundary` — so the kept side
        is the single `[prev_boundary, boundary)` era. The multi-boundary narrowing stays `ts`-based:
        the content blobs distinguish only the last boundary's two sides, not the older eras."""
    boundary = str(fact.get("boundary") or "")
    if not boundary:
        return None
    content = None
    if head_sha:
        cmap = fact.get("content_era_by_sha")
        c = cmap.get(head_sha) if isinstance(cmap, dict) else None
        if c in ("pre", "post"):
            content = c
    if content == "post":
        side_post = True
    elif content == "pre":
        side_post = False
    elif ts:
        side_post = ts >= boundary
    else:
        return None   # no content match and no timestamp — unplaceable
    if fact.get("kept_era") == "post":
        return "kept" if side_post else "dropped"
    # disclosed_pre — kept side is PRE
    if side_post:
        return "dropped"
    if fact.get("multi_change") and ts:
        prev = str(fact.get("prev_boundary") or "")
        if prev and ts < prev:
            return "dropped"   # an even-older era, outside the kept [prev, boundary) window
    return "kept"


def _era_resolve_thin_flip(
        config_era_facts: list[dict[str, Any]],
        repr_shas: list[str],
        per_sha_checks: list[dict[str, float]],
        rep_ts: dict[str, str],
        check_wf_of: "Callable[[str], str | None]",
        sampled_runs_by_wf: dict[str, list[dict[str, Any]]],
        redrill: "Callable[[str, list[dict[str, Any]]], None]",
) -> list[str]:
    """Decide the `disclosed_pre` → `post_only_thin` flip PRE-DRILL and re-drill the flipped
    workflow's spine from its POST runs (issue #74, owner-adjudicated direction (a): the timing
    spine flips WITH the rule).

    `_partition_config_era` chose `disclosed_pre` from RUN counts alone, but only the PR gate sample
    (`per_sha_checks`) reveals whether the kept PRE era actually produced a gate-bearing check IN THE
    SAMPLE. When it did NOT — the live #74 shape, where the sole gate-bearing PRs are all post-change,
    so every attributed check is observed only on the DROPPED side and the kept side is check-empty —
    "measure the old configuration" is UNAVAILABLE (there is nothing pre-era to show). The honest
    outcome is `post_only_thin`: measure the NEW config from the thin post sample. Because the spine
    was already drilled off the PRE runs (`runs_for_spine`), the fact's `redrill(wf, post_runs)` MUST
    rebuild that workflow's crit/jobs from the post-era runs — so crit_by_wf, `pr_check_p50`, the
    representative-run links, and the makespans all derive from the new configuration, never a pre-era
    number under a "measures the new configuration" claim. The rule/kept_era are flipped only AFTER
    the re-drill lands.

    The decision is a pure function of the sample (the trigger is data, not fetches); the single side
    effect, the re-drill, is INJECTED so the decision stays unit-testable. Mirrors `_era_pr_side`'s
    kept/dropped split so the spine and the later `_era_scope_enumeration` agree on the era cut.
    Returns the flipped workflow files (for logging / stamping). Non-straddling / non-`disclosed_pre`
    facts are untouched, so this is a pure no-op when nothing flips (byte-identity)."""
    flipped: list[str] = []
    if not config_era_facts:
        return flipped
    indexed = list(zip(repr_shas, per_sha_checks))
    for fact in config_era_facts:
        if fact.get("rule") != "disclosed_pre":
            continue
        wf = fact.get("workflow_file")
        if not wf:
            continue
        kept_has = other_has = False
        for sha, m in indexed:
            # CONTENT-first era placement (issue #77): a fix-PR that carried the new workflow while
            # running pre-boundary lands on the DROPPED (post) side here, so its gate check counts
            # toward `other_has` — the empty-kept trigger — instead of masquerading as a kept pre PR.
            side = _era_pr_side(rep_ts.get(sha, ""), fact, sha)
            if side is None:
                continue
            for name in m:
                if check_wf_of(name) != wf:
                    continue
                if side == "kept":
                    kept_has = True
                else:
                    other_has = True
        if kept_has or not other_has:
            continue   # the PRE era HAS a gate check (disclosed_pre stands), or nothing to flip
        boundary = str(fact.get("boundary") or "")
        # Re-drill from the POST runs, CONTENT-first (issue #77): a fix-PR run that carried the new
        # config counts toward the post spine even though its `created_at` predates the boundary, so
        # the re-drill measures the new config on the fullest available post sample.
        content_era = fact.get("content_era_by_sha")
        content_era = content_era if isinstance(content_era, dict) else None
        post_runs = [r for r in sampled_runs_by_wf.get(wf, [])
                     if _run_config_era(r, boundary, content_era) == "post"]
        redrill(str(wf), post_runs)                    # SPINE now derives from the POST era
        fact["rule"] = "post_only_thin"
        fact["kept_era"] = "post"
        fact["thin_sample"] = True
        fact["redrilled_post_n"] = len(post_runs)
        logger.warning(
            "config-era: %s straddle's kept (pre) era carries no gate-bearing check in the PR "
            "sample (the gate PRs are all post-change) — flipping disclosed_pre → post_only_thin "
            "and re-drilling the SPINE from %d post-change run(s) so every rendered number derives "
            "from the new config", wf, len(post_runs))
        flipped.append(str(wf))
    return flipped


def _stamp_pole_repr_run_era(
        poles: list[dict[str, Any]],
        config_era_facts: list[dict[str, Any]],
        jobs_per_run_by_wf: dict[str, list[list[dict[str, Any]]]]) -> None:
    """Stamp each pole on a straddling workflow with the earliest created_at of its DRILLED runs, plus
    that run's CONTENT era + basis (issue #74 guard leg; content-keyed for issue #77). After the
    thin-flip re-drills a `post_only_thin` workflow's spine from its POST runs, `jobs_per_run_by_wf[wf]`
    holds only post-era runs — so a pole whose drilled runs are PRE-era is a pre-era timing leak under
    a post-claiming disclosure. Uses the EARLIEST drilled run (`repr_run_created_at`, so a single
    pre-era run can't hide) and stamps its `repr_run_head_sha`, `repr_run_era`, and
    `repr_run_era_basis`:
      * basis "content" — the run's head_sha resolved against the fact's `content_era_by_sha`; the
        era is that CONTENT era. This is the honest signal on the live #77 shape, where a re-drilled
        post run may carry a `created_at` before the merge boundary (it ran the new config early).
      * basis "timestamp" — no content match; the era is `created_at` vs boundary, the pre-#77 rule.
    `verify_report.check_era_disclosure_matches_enumeration` reads these: with basis "content" a
    stamped era < boundary is NOT a contradiction (the run genuinely ran the new config early); with
    basis "timestamp" it re-derives the old `created_at`-vs-boundary check. A pure no-op when nothing
    straddles (byte-identity — the stamps are only added on a straddling workflow's poles)."""
    if not config_era_facts:
        return
    fact_by_wf = {str(f.get("workflow_file")): f
                  for f in config_era_facts if f.get("workflow_file")}
    for p in poles:
        wf = str(p.get("workflow_file") or "")
        job = str(p.get("job") or "")
        fact = fact_by_wf.get(wf)
        if fact is None or not job:
            continue
        runs_meta = [(str(j.get("_run_created_at") or ""), str(j.get("_run_head_sha") or ""))
                     for run in jobs_per_run_by_wf.get(wf, [])
                     for j in run if str(j.get("name", "")) == job and j.get("_run_created_at")]
        if not runs_meta:
            continue
        created, head_sha = min(runs_meta, key=lambda t: t[0])   # earliest drilled run
        p["repr_run_created_at"] = created
        boundary = str(fact.get("boundary") or "")
        content_map = fact.get("content_era_by_sha")
        content = content_map.get(head_sha) if isinstance(content_map, dict) else None
        if head_sha:
            p["repr_run_head_sha"] = head_sha
        if content in ("pre", "post"):
            p["repr_run_era"] = content
            p["repr_run_era_basis"] = "content"
        elif boundary and created:
            p["repr_run_era"] = "post" if created >= boundary else "pre"
            p["repr_run_era_basis"] = "timestamp"


def _era_scope_enumeration(
        pr_check_p50: dict[str, float],
        repr_shas: list[str],
        per_sha_checks: list[dict[str, float]],
        rep_ts: dict[str, str],
        config_era_facts: list[dict[str, Any]],
        check_wf_of: "Callable[[str], str | None]",
) -> dict[str, float]:
    """Bind the CHECK ENUMERATION to the kept config era (issue #69).

    #66/#68 partitioned the workflow RUNS used for SPINE TIMING (`runs_for_spine`) at each
    straddling workflow's config-era boundary, but the enumerated CHECK SET was still drawn from
    the raw PR-gate check sample — which can include post-boundary PRs carrying the NEW config's
    checks (e.g. `guard shard 1/4..4/4`). A `disclosed_pre` report then rendered those post-era-only
    jobs as poles / Level-1 bars BESIDE the pre-era `test` timing, under a disclosure claiming
    everything reflects the config BEFORE the change — a configuration that never ran, and the seed
    of the fabricated "the full-suite guard overlaps the sharded version" redundancy (issue #66's
    shape, through this new path).

    For each straddling workflow this splits the sampled PRs at its boundary (`_era_pr_side`) and
    keeps only the checks ATTRIBUTED to that workflow (`check_wf_of`) that were OBSERVED on the KEPT
    side. A check attributed to the workflow but observed ONLY on the dropped side is the OTHER
    configuration's add/remove: dropped from `pr_check_p50` (so it never enumerates as a pole / bar /
    population) and recorded on the fact as `other_era_checks` for the era note. The kept-side
    observations are recorded as `kept_checks`. Checks from NON-straddling workflows, and checks the
    mapper can't attribute, are untouched — so with nothing straddling this is a pure no-op
    (`pr_check_p50` returned unchanged; L2 byte-identity).

    **Runs on ALREADY-RESOLVED facts (issue #74).** The `disclosed_pre` → `post_only_thin` flip is
    decided EARLIER — pre-drill, in `_era_resolve_thin_flip` — so that when it fires the workflow's
    whole SPINE (crit_by_wf, jobs, pr_check_p50, representative-run links) is re-drilled from the POST
    runs and every rendered number derives from the new configuration, not a pre-era run under a
    "measures the new configuration" claim. By the time this binder runs, a flipped fact is already
    `post_only_thin`/`kept_era=post`, so the split below simply keeps its post-side checks and stamps
    them — the same code path as a `post_only` straddle. This function no longer mutates the rule.

    The stamped `kept_checks` / `other_era_checks` are the re-derivation surface
    `verify_report.check_era_enumeration_bound` reads: no rendered pole/check may be a member of any
    era's `other_era_checks` (a check absent from the kept era). One report describes ONE
    configuration and names what the other configuration adds/removes."""
    if not config_era_facts:
        return pr_check_p50
    indexed = list(zip(repr_shas, per_sha_checks))
    to_drop: set[str] = set()
    for fact in config_era_facts:
        wf = fact.get("workflow_file")
        if not wf:
            continue
        # Compute each sampled PR's side under the fact's (already-resolved) kept_era. An unplaceable
        # PR (`None` — missing/malformed timestamp) is in NEITHER bucket, so count it and log the
        # split at DEBUG: a spike in unplaceable PRs is a timestamp-plumbing regression that would
        # quietly narrow the era evidence, and this is the only place it surfaces.
        sides = [(m, _era_pr_side(rep_ts.get(sha, ""), fact, sha)) for sha, m in indexed]
        kept_idx = [m for m, s in sides if s == "kept"]
        drop_idx = [m for m, s in sides if s == "dropped"]
        unplaceable = sum(1 for _, s in sides if s is None)
        kept_checks: list[str] = []
        other_checks: list[str] = []
        for name in pr_check_p50:
            if check_wf_of(name) != wf:
                continue   # a different (or unattributable) workflow — not this era's to cut
            on_kept = any(name in m for m in kept_idx)
            on_drop = any(name in m for m in drop_idx)
            if on_kept:
                kept_checks.append(name)
            elif on_drop:
                other_checks.append(name)
        logger.debug("config-era enumeration [%s]: %d kept-side / %d dropped-side / %d unplaceable "
                     "PR(s), %d kept-check(s) / %d other-era-check(s)",
                     wf, len(kept_idx), len(drop_idx), unplaceable,
                     len(kept_checks), len(other_checks))
        fact["kept_checks"] = sorted(kept_checks)
        fact["other_era_checks"] = sorted(other_checks)
        to_drop |= set(other_checks)
    if not to_drop:
        return pr_check_p50
    scoped = {n: v for n, v in pr_check_p50.items() if n not in to_drop}
    if not scoped:
        # Residual degenerate (post the #74 flip this is unreachable for a disclosed_pre straddle,
        # which is re-drilled to post BEFORE it ever reaches here): a post_only straddle whose CURRENT
        # era produced no gate-bearing check while the retired one did. Leave the enumeration intact
        # BUT keep the stamps: `check_era_enumeration_bound` then FAILs loudly on the leak rather than
        # skipping blind (a cleared stamp is what made the #74 guard go blind — never clear them here).
        logger.warning("config-era: check enumeration scoping would empty the spine "
                       "(%d check(s) all bound to the dropped era, no flip applied) — leaving the "
                       "enumeration intact; the stamps survive so the guard FAILs on the leak",
                       len(to_drop))
        return pr_check_p50
    logger.info("config-era: dropped %d post/pre-boundary-only check(s) from the enumeration so "
                "the spine describes ONE configuration: %s",
                len(to_drop), ", ".join(sorted(to_drop)[:8]))
    return scoped


def _era_stamp_spine_relevance(
        config_era_facts: list[dict[str, Any]],
        wf_docs: dict[str, dict[str, Any]],
        crit_by_wf: dict[str, dict[str, Any]],
) -> None:
    """Stamp `spine_relevant` (+ the `developer_event` re-derivation basis) on each straddle fact
    so the renderer can gate the GLOBAL "the headline reflects the old/thin config" caveat, and
    `verify_report` can re-derive that gate offline (issue #116).

    A straddle may caveat the HEADLINE only when its workflow actually touches the PR-gating spine:
    it fires on a developer event AND contributed at least one check to the bound enumeration
    (`kept_checks` or `other_era_checks` non-empty — `_era_scope_enumeration` stamped those just
    above). A push-only / cron-only workflow, or one that contributed ZERO spine checks (live: astro
    `build-sandbox-image.yml`, `on: push[main] + workflow_dispatch`, 0/33 spine checks; biome
    `preview.yml` / `repository_dispatch.yml`, `workflow_dispatch + schedule`), still changed its
    runner-minute layout — the bill-scope note in Data sources covers that — but CANNOT make the
    merge-wait headline reflect the old config, so its global caveat is suppressed.

    `developer_event` combines TWO signals, strong-first, so a STALE trigger read can never veto real
    evidence (issue #116 review — the silent-drop the reviewers caught):
      * STRONG — a `kept_checks`/`other_era_checks` entry attributed via the DEVELOPER-TIMED mapper
        (`_map_check_to_job(..., require_developer_timing=True)`): direct proof the workflow produced a
        PR-gating check that drove the rendered spine IN THE SAMPLE. This does NOT read `on:`, so it is
        immune to the boundary skew below. It is the signal that matters for a straddle that CHANGED
        its trigger set — e.g. a workflow that DROPPED `pull_request` in the new config: the audit
        keeps the PRE era (`disclosed_pre`, spine drilled off the pre runs), its kept check is a real
        developer-timed gate, yet the fetched HEAD `on:` is push-only. Strong-first renders its caveat.
      * WEAK — the static `on:` parse via the canonical `_on_has_pr_trigger` (`_PR_TRIGGER_EVENTS`:
        `pull_request` / `pull_request_target` / `merge_group`, incl. the fork-PR gate). Only needed to
        confirm relevance for a check attributed via the timing-LESS job-graph scan (a name match on a
        non-PR workflow — which SHOULD stay suppressed unless the current triggers say PR-gating).
    The `on:` is parsed from the checkout/HEAD (`_fetch_workflow_docs`), which for a straddle can lag
    or lead the sampled runs — hence it is only the weak, tie-break signal, never a veto.

    UNKNOWN != absent: a straddle whose workflow doc we could neither read nor fetch is OMITTED from
    `wf_docs` (`_fetch_workflow_docs`'s explicit contract). With NO strong signal either, we cannot
    tell if it gates — so leave it UNSTAMPED (neither key) rather than assert `developer_event=False`
    and silently drop a real gate's caveat; the renderer's / guard's fallback then re-derives from the
    enumeration sets (`bool(kept or other)`), preserving disclosure on a missing read. A strong
    (developer-timed) signal needs no doc, so a dev-timed straddle is stamped even when its doc is
    absent.

    Design: this SCOPES a RENDER decision, it does not drop the fact. Every other era consumer
    (the bill-scope methodology note, the `post_only` narrowed note, the cost-spine full-sample note,
    era-scoped bill figures) keeps reading the SAME `config_eras` facts, byte-identical but for the two
    added keys they ignore — so their behaviour is unchanged. `developer_event` is stamped as the
    guard's independent re-derivation basis; because `developer_event` folds in the strong signal,
    `spine_relevant == developer_event AND (kept|other)` stays algebraically exact (the strong signal
    implies a non-empty enumeration set), so `verify_report`'s integrity arm needs no change. Mutates
    the facts in place."""
    for fact in config_era_facts:
        wf = fact.get("workflow_file")
        if not wf:
            continue
        kept = fact.get("kept_checks") or []
        other = fact.get("other_era_checks") or []
        has_spine_check = bool(kept or other)
        # STRONG signal: a kept/other check the developer-timed mapper pins to THIS workflow — real
        # PR-gate evidence, independent of the (possibly stale) fetched `on:`.
        dev_timed = any(
            (_map_check_to_job(str(c), crit_by_wf, require_developer_timing=True) or (None,))[0]
            == str(wf) for c in (*kept, *other))
        known_doc = str(wf) in wf_docs
        # UNKNOWN != absent: no doc AND no strong evidence — leave unstamped for the fallback.
        if not known_doc and not dev_timed:
            continue
        dev_event = dev_timed or (known_doc and _on_has_pr_trigger(_wf_on(wf_docs[str(wf)])))
        fact["developer_event"] = dev_event
        fact["spine_relevant"] = dev_event and has_spine_check
        logger.debug("config-era spine relevance [%s]: dev_timed=%s dev_event=%s kept=%d other=%d "
                     "-> %s", wf, dev_timed, dev_event, len(kept), len(other), fact["spine_relevant"])


def _era_scope_pr_spine_sample(
        repr_shas: list[str],
        per_sha_checks: list[dict[str, float]],
        sha_intervals: "dict[str, dict[str, tuple[str, str]]]",
        rep_ts: dict[str, str],
        spine_checks: "set[str] | frozenset[str]",
        config_era_facts: list[dict[str, Any]],
        check_wf_of: "Callable[[str], str | None]",
) -> "tuple[list[str], list[dict[str, float]], dict[str, dict[str, tuple[str, str]]], int]":
    """Bind the PER-PR SPINE SAMPLE to the kept config era (issue #80).

    #66/#68 scoped the workflow RUNS used for spine timing, and #69 (`_era_scope_enumeration`) scoped
    the enumerated CHECK SET, but the PER-PR layer — the sample `_stamp_chain_facts` (chain facts +
    empirical makespans), `_segment_pr_populations`, and `_rank_spine_present_first` (presence
    denominators) all consume — was still the RAW sample, filtered only by check NAME. Because a check
    NAME survives a config change (`test` is still `test` after it gets 3× faster), a DROPPED-era PR's
    latest-attempt interval for a kept-NAMED check flowed straight into `chain_facts → chain_summary →
    makespan_p50_s` — the "typical PR waits N" headline and the #24 physical-bound cap — under a
    disclosure claiming everything reflects the KEPT era (live: a 166s post-era makespan cap on a
    538s-gate pre era).

    The cut is **surgical and per straddling workflow**: a sampled PR's era side is decided per
    workflow (`_era_pr_side`, content-first via head_sha). A PR on workflow W's DROPPED side ran the
    OTHER configuration of W, so its checks ATTRIBUTED to W (`check_wf_of`) are removed from both its
    check map and its interval map — that timing is the wrong era for W. Checks from NON-straddling
    workflows, from workflows the PR sits KEPT-side of, and checks the mapper can't attribute (fileless
    bots, foreign workflows) are RETAINED — the straddle only poisons W's own checks, so a PR that
    straddles W but carries an era-neutral gate from a sibling workflow keeps that row (minus W's
    checks). A PR left with NO gate-bearing (spine) check after the cut drops out of the sample
    entirely (its whole row is the wrong era for every gate it carried).

    Returns `(kept_shas, kept_per_sha_checks, kept_intervals, dropped_pr_count)` — the scoped sample
    the spine-facing consumers read, plus how many PR rows the door fully removed (stamped as
    `era_dropped_pr_count` so the sampling-honesty caveat stays honest and the drop is visible, never
    silent). A pure no-op returning the ORIGINAL objects when nothing straddles (byte-identity for
    non-straddling repos).

    **Ordering (issue #74).** The `disclosed_pre → post_only_thin` flip is decided EARLIER
    (`_era_resolve_thin_flip`, on the FULL sample so it can SEE the dropped-side gate PRs). By the
    time this door runs the facts are resolved, so an emptied kept side has already flipped to
    `post_only_thin` (kept_era=post) and the once-dropped post-side PRs are now KEPT-side here — never
    an empty-spine render under a pre claim. A genuine `disclosed_pre` with a real pre-era gate keeps
    its pre PRs and drops the post ones; if a non-flip straddle still ends with zero surviving PRs the
    caller's `_chain_summary([])` is None and the renderer's chainless path handles it — never a
    dropped-era makespan."""
    if not config_era_facts:
        return repr_shas, per_sha_checks, sha_intervals, 0
    facts = [f for f in config_era_facts if f.get("workflow_file")]
    if not facts:
        return repr_shas, per_sha_checks, sha_intervals, 0
    kept_shas: list[str] = []
    kept_checks: list[dict[str, float]] = []
    kept_intervals: dict[str, dict[str, tuple[str, str]]] = {}
    dropped = 0
    for sha, m in zip(repr_shas, per_sha_checks):
        ts = rep_ts.get(sha, "")
        dropped_wfs = {str(f["workflow_file"]) for f in facts
                       if _era_pr_side(ts, f, sha) == "dropped"}
        if not dropped_wfs:
            # Kept-side (or unplaceable/neutral) for every straddling workflow — the whole row stays.
            kept_shas.append(sha)
            kept_checks.append(m)
            if sha in sha_intervals:
                kept_intervals[sha] = sha_intervals[sha]
            continue
        scoped_m = {n: v for n, v in m.items() if check_wf_of(n) not in dropped_wfs}
        if not any(n in spine_checks for n in scoped_m):
            # Every gate-bearing check this PR carried belongs to a workflow it ran the OTHER config
            # of — the whole row is the wrong era, so it leaves the sample.
            dropped += 1
            logger.debug("config-era spine door: dropped PR %s — every gate-bearing check belongs to "
                         "a workflow it ran the dropped config of (%s)", sha, sorted(dropped_wfs))
            continue
        iv = sha_intervals.get(sha) or {}
        scoped_iv = {n: t for n, t in iv.items() if check_wf_of(n) not in dropped_wfs}
        kept_shas.append(sha)
        kept_checks.append(scoped_m)
        if scoped_iv:
            kept_intervals[sha] = scoped_iv
    if dropped or len(kept_shas) != len(repr_shas):
        logger.info("config-era spine door: scoped the per-PR spine sample to the kept era — "
                    "%d PR(s) kept, %d dropped-era PR row(s) removed from chain_facts / makespan / "
                    "populations / presence (issue #80)", len(kept_shas), dropped)
    return kept_shas, kept_checks, kept_intervals, dropped


# Developer-facing trigger events, most-PR-relevant first. The critical path
# for wall-clock sizing is computed from runs of the first of these the workflow
# actually fired on (the developer's wait event), not blended across triggers.
#
# `pull_request_target` is a developer-wait event too: it is the fork-PR / triage
# variant of `pull_request` (the workflow runs in the BASE repo's context against
# the PR head), so a PR genuinely waits on its checks. Omitting it was a bug — a
# workflow triggered on `push` + `pull_request_target` (no plain `pull_request`)
# found NO developer event, fell back to `all-events` scoping in `_crit_for`, and
# BLENDED its post-merge `push` runs into the PR critical path. On a fork-PR
# conflict labeler that re-scans every open PR on each push-to-default, that push
# mode (a single 120s retry sleep inside the labeler action, gating ZERO merges)
# manufactured a false "~2m on ~30% of PRs" bimodal gate out of runs no PR ever
# waits on. Scoping to `pull_request_target` measures only the PR-wait runs.
# Ordered AFTER plain `pull_request` so a workflow firing on both scopes to the
# primary PR event; BEFORE `merge_group` (the PR-review wait precedes the queue).
_DEVELOPER_EVENTS = ("pull_request", "pull_request_target", "merge_group")

# Trigger events whose run volume is driven by human chatter (comments, reviews,
# issue/discussion activity), not CI work — they can fire orders of magnitude
# more often than pull_request/push. A workflow triggered by ANY of these has a
# 30-day total_count that is NOT a trustworthy proxy for how often a developer
# waits on its CI, so it is excluded from the run-share reference denominator and
# its own run-share is left UNMEASURED (which defaults to full ranking weight)
# rather than printed at an inflated value. Real case: a `pull_request` +
# `issue_comment` workflow showed ~45.6k runs/30d on a busy repo (almost all from
# comments) and wrongly became "the busiest PR workflow", deflating every other
# workflow's share against that inflated denominator.
_VOLUME_CONTAMINATING_EVENTS = frozenset({
    "issue_comment", "pull_request_review", "pull_request_review_comment",
    "issues", "discussion", "discussion_comment",
})


def _volume_is_ci_clean(events: set[str] | None) -> bool:
    """True when a workflow's 30d total_count is a trustworthy CI-run proxy —
    i.e. none of its triggers are human-chatter events that dwarf CI volume."""
    return not (set(events or ()) & _VOLUME_CONTAMINATING_EVENTS)


def _fold_observed_events(events_by_wf: dict[str, set[str]], wf_path: str,
                          runs: list[dict[str, Any]] | None) -> None:
    """Union the events actually observed in `runs` into the persisted events
    mirror for `wf_path`.

    `events_by_wf` is first built from the MAIN sampling pass's SUCCESS slice
    (`_success_runs_from_all_status`), which can miss a trigger the workflow
    genuinely fires on — e.g. a `[push, schedule]` workflow whose recent
    successes happen to be all `push`. Dedicated event-scoped probes (OPT36's
    schedule run list) observe those runs directly, so fold their events back in
    to keep the mirror a faithful UNION of every event the pipeline actually saw
    for the workflow. That mirror is the re-derivation surface
    `verify_report.py`'s `non_pr_event` corroboration reads: without the fold a
    real schedule-burn certificate fails its `subset ⊆ events_by_wf[wf]` check
    even though the workflow demonstrably runs on schedule. The union is
    monotonic and `schedule` is neither a PR-volume nor a volume-contaminating
    event, so every downstream `& _PR_VOLUME_EVENTS` / `_volume_is_ci_clean`
    selection is unchanged."""
    observed = {str(r.get("event")) for r in (runs or []) if r.get("event")}
    if observed:
        events_by_wf.setdefault(wf_path, set()).update(observed)


# PR-developer-wait events for the run-share DENOMINATOR (the "busiest PR
# workflow"). Now EQUAL to _DEVELOPER_EVENTS — since `pull_request_target` (the
# fork-PR / triage variant a PR also waits on) became a critical-path developer
# event, a busy pull_request_target workflow is a valid run-share reference here
# too. Kept as a distinct, explicitly-unioned name so the denominator stays a
# superset by construction even if the two event lists ever diverge again
# (omitting pull_request_target once let such a workflow's volume exceed the
# chosen denominator, producing a nonsensical run-share > 100%).
_PR_VOLUME_EVENTS = frozenset(_DEVELOPER_EVENTS) | {"pull_request_target"}

# Push-only-repo merge-wait events. On a repo with NO pull_request flow (the team
# merges straight to the default branch), the developer's merge wait IS the `push`
# CI — there is no PR workflow to anchor the PR-floor. Used ONLY as a fallback by
# `_select_pr_floor_workflows` when no `_PR_VOLUME_EVENTS` workflow ran, so a normal
# PR repo's post-merge `push` (deploy) workflow never displaces its PR spine.
_PUSH_VOLUME_EVENTS = frozenset({"push"})

# Merge-queue (`merge_group`) runs execute on a GitHub-generated temporary branch
# named `gh-readonly-queue/<base>/pr-<N>-<sha>` — one synthetic commit (a
# DISTINCT head_sha) per PR the queue is validating. That head_sha is NOT the PR's
# own head, so keying the presence population by raw head_sha counts each queue run
# as a SEPARATE "PR" in the denominator. On a repo that runs its heavy suite only in
# the merge queue, the heavy suite then reads as present on a MINORITY of the sampled
# "PRs" and the presence-weighted ranking (#26/#27/#57) demotes the REAL merge gate,
# crowning a lighter check (issue #58). `_merge_group_pr_number` recovers the
# originating PR number from the queue branch so a queue run can be folded back onto
# its PR's population row instead of inflating the denominator.
#
# `<base>` is `.+` (greedy), NOT a single `[^/]+` segment: a merge queue keeps the
# base branch's slashes in the temp branch, so a queue targeting `release/1.x` pushes
# `gh-readonly-queue/release/1.x/pr-<N>-<sha>`. Greedy `.+` backtracks to the LAST
# `/pr-<N>-<hex>` suffix — the real PR marker is always last — so a slashed base (and
# even a base whose own name contains a `pr-<n>-<hex>`-shaped segment) still resolves
# to the true PR number. A `[^/]+` base would fail to parse every slashed-base queue
# run and dump it into the orphan class, silently forfeiting the dedup on exactly the
# release-branch repos this fix targets.
_MERGE_QUEUE_BRANCH_RE = re.compile(
    r"^gh-readonly-queue/.+/pr-(\d+)-[0-9A-Fa-f]+$")


def _merge_group_pr_number(head_branch: Any) -> int | None:
    """The originating PR number encoded in a merge_group run's queue branch
    (`gh-readonly-queue/<base>/pr-<N>-<sha>`), or None when the branch is absent or
    doesn't match the GitHub merge-queue naming scheme."""
    m = _MERGE_QUEUE_BRANCH_RE.match(str(head_branch or ""))
    return int(m.group(1)) if m else None


def _run_pr_number(run: dict[str, Any]) -> int | None:
    """The PR number a developer-event run belongs to, for presence-population
    dedup: from the queue branch for a `merge_group` run, else from the run's
    `pull_requests[0].number` (populated for same-repo pull_request runs, empty for
    fork-PR runs). None when it can't be derived (a fork-PR run with no
    `pull_requests`, or a queue branch off the naming scheme) — such runs fall to the
    non-derivable path in `_group_dev_shas_by_pr`."""
    ev = str(run.get("event") or "")
    if ev == "merge_group":
        return _merge_group_pr_number(run.get("head_branch"))
    prs = run.get("pull_requests")
    if isinstance(prs, list) and prs and isinstance(prs[0], dict):
        n = prs[0].get("number")
        if isinstance(n, int):
            return n
    return None


# The synthetic single-population key every UNLINKABLE merge_group run collapses
# onto (see `_group_dev_shas_by_pr`). A sentinel string, never a real head_sha.
_QUEUE_ORPHAN_KEY = "\x00merge-queue-orphans"


def _group_dev_shas_by_pr(
    sha_meta: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Collapse the per-head-sha presence candidates to PR IDENTITY so a merge-queue
    run never inflates the presence denominator (issue #58).

    `sha_meta` maps each sampled developer-event head_sha to its `event`, derived PR
    number (`pr_num`, via `_run_pr_number`), and most-recent run timestamp (`ts`).
    Returns `(rep_ts, rep_members)`:
      * `rep_ts`      — REPRESENTATIVE head_sha -> the group's latest timestamp (the
                        walk key `_select_repr_shas` sorts newest-first);
      * `rep_members` — representative head_sha -> every member head_sha whose
                        check-runs are fetched and UNIONED into that one population row.

    The dedup rule:
      * NO merge_group runs in the sample -> every sha is its own group, BYTE-FOR-BYTE
        the pre-#58 per-sha behaviour (the queue fold can never fire, so a normal PR
        repo's populations/presence/chain_facts are unchanged).
      * a merge_group run WITH a derivable PR number, and any pull_request run of the
        SAME PR, share one `("pr", N)` group -> the queue's heavy suite and the PR
        event's checks union onto ONE population row, so a gate that runs only in the
        queue is present on that PR (correctly crowned) instead of counted as a
        separate minority "PR".
      * a merge_group run whose PR number is NOT derivable (a queue branch off the
        naming scheme) collapses onto a SINGLE orphan group -> the queue timing is
        still measured (one row), but N unlinkable queue runs add 1 to the denominator,
        not N. They cannot dilute PR presence, and are never silently dropped.
      * a pull_request run is folded only when its PR ALSO appears in the queue set;
        otherwise it stays its own per-sha row (a PR that never entered the queue is a
        distinct PR, exactly as before — this gate keeps non-queue repos untouched).

    The representative head_sha is the group's NEWEST member (a real sha, so the
    stamped `chain_facts.sha` and the makespan interval lookups stay real head-shas)."""
    has_merge_group = any(
        m.get("event") == "merge_group" for m in sha_meta.values())
    queue_pr_nums = {
        m.get("pr_num") for m in sha_meta.values()
        if m.get("event") == "merge_group" and m.get("pr_num") is not None}

    def _identity(sha: str, m: dict[str, Any]) -> Any:
        if not has_merge_group:
            return ("sha", sha)  # no queue in play — pre-#58 per-sha identity
        ev = m.get("event")
        pr = m.get("pr_num")
        if ev == "merge_group":
            return ("pr", pr) if pr is not None else _QUEUE_ORPHAN_KEY
        # pull_request / pull_request_target: fold ONLY into a PR the queue touched.
        if pr is not None and pr in queue_pr_nums:
            return ("pr", pr)
        return ("sha", sha)

    groups: dict[Any, list[str]] = {}
    for sha, m in sha_meta.items():
        groups.setdefault(_identity(sha, m), []).append(sha)

    rep_ts: dict[str, str] = {}
    rep_members: dict[str, list[str]] = {}
    for shas in groups.values():
        # Deterministic representative: newest ts first, head_sha as the tiebreak.
        ordered = sorted(
            shas, key=lambda s: (sha_meta[s].get("ts") or "", s), reverse=True)
        rep = ordered[0]
        rep_ts[rep] = sha_meta[rep].get("ts") or ""
        rep_members[rep] = ordered
    return rep_ts, rep_members


def _union_member_checks(
    members: list[str],
    fetch: "Callable[[str], list[dict[str, Any]] | None]",
) -> "tuple[dict[str, float], dict[str, tuple[str, str]]] | None":
    """Union the check-runs across a PR-identity group's member head-shas
    (`_group_dev_shas_by_pr`) into ONE population row — max duration per check,
    latest-attempt interval per check — so a PR's `pull_request` head and its
    `gh-readonly-queue` commit share a single row.

    COVERAGE-GAP semantics (issue #58 corollary): returns None if ANY member's
    `fetch` fails (returns None). A partially-fetched group is NOT laundered into a
    complete-looking row — a failed heavy-suite member merged with a successful
    light member would silently read as "this PR ran only the light checks", diluting
    the heavy gate's presence and reintroducing the very #58 demotion this fix removes,
    through the transient-failure door. The pre-#58 per-sha path already counted a
    failed fetch as a coverage gap (never "this PR is clean"); a multi-member group
    upholds the SAME contract — one member unfetched makes the whole PR's presence
    vector untrustworthy, so the caller counts it as a fetch failure exactly as before.
    A member that genuinely ran no timed check contributes `{}` (not a failure) and the
    union proceeds. On a no-queue repo every group is single-member, so this reduces
    byte-for-byte to the pre-#58 behaviour (fetch None -> None, else the sha's map)."""
    m: dict[str, float] = {}
    iv: dict[str, tuple[str, str]] = {}
    for member in members:
        crs = fetch(member)
        if crs is None:
            return None  # any member unfetched -> the PR's presence row is a gap
        for cr in crs:
            d = _duration_s(cr.get("started_at"), cr.get("completed_at"))
            if d is not None and d > 0:
                name = str(cr.get("name", "?"))
                m[name] = max(m.get(name, 0.0), d)
                start = str(cr.get("started_at"))
                prev = iv.get(name)
                if prev is None or start > prev[0]:
                    iv[name] = (start, str(cr.get("completed_at")))
    return m, iv


def _developer_event(jobs_by_event: dict[str, list]) -> str | None:
    """The developer-facing event the workflow has sampled runs for (PR first),
    or None when it only ran on non-developer-facing triggers."""
    return next((e for e in _DEVELOPER_EVENTS if jobs_by_event.get(e)), None)


def _job_runner_label(job: dict[str, Any]) -> str:
    """The runner a job ran on, from the gh jobs API `labels` array (e.g.
    `ubuntu-latest`, `ubuntu-24.04-arm`). Empty when absent."""
    labels = job.get("labels") or []
    return " ".join(sorted(str(x) for x in labels)) if labels else ""


# -----------------------------------------------------------------------------
# Run-list endpoints
#
# Each `_*_endpoint` builder is the SINGLE source of truth for one run-list call's
# URL, shared by the fetcher below it and by the prefetch planners in `collect()`.
# Prefetch works by parking a response under its endpoint string, so a planner that
# spelled a URL even slightly differently from its call site would silently prefetch
# one endpoint and then fetch another — paying for both. Going through these builders
# makes that class of drift impossible.
# -----------------------------------------------------------------------------

def _volume_endpoint(repo: str, wf_id: int, created_before: str | None,
                     event: str | None = None) -> str:
    """30-day total-run-count probe (`per_page=1`, read for `total_count`)."""
    since_iso, upper = _window_30d(created_before)
    created = f"{since_iso}..{upper}" if upper else f">={since_iso}"
    ev = f"&event={event}" if event else ""
    return (f"repos/{repo}/actions/workflows/{wf_id}/runs"
            f"?per_page=1{ev}&created={created}")


def _run_list_endpoint(repo: str, wf_id: int, max_runs: int,
                       created_before: str | None, *,
                       status: str | None = None, event: str | None = None) -> str:
    """A run-list slice: `status=success` for the sampling passes, unfiltered for the
    all-status (superseded / double-trigger / rerun) detectors, optionally scoped to a
    single event. Pinned by `created<=<pin>` for deterministic regen."""
    pin_norm = _normalize_pin(created_before)
    pin = f"&created=<={pin_norm}" if pin_norm else ""
    st = f"&status={status}" if status else ""
    ev = f"&event={event}" if event else ""
    return (f"repos/{repo}/actions/workflows/{wf_id}/runs"
            f"?per_page={max_runs}{st}{ev}{pin}")


def _status_count_endpoint(repo: str, wf_id: int, status: str,
                           created_before: str | None) -> str:
    """30-day run count for one conclusion (`status=failure` / `status=success`) — the
    two probes OPT48's failure rate is the ratio of."""
    since_iso, upper = _window_30d(created_before)
    created = f"{since_iso}..{upper}" if upper else f">={since_iso}"
    return (f"repos/{repo}/actions/workflows/{wf_id}/runs"
            f"?per_page=1&status={status}&created={created}")


def _run_jobs_endpoint(repo: str, run_id: Any, filter_value: str | None = None) -> str:
    """A run's job listing — the single most-issued endpoint in the whole pass."""
    flt = f"&filter={filter_value}" if filter_value else ""
    return f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100{flt}"


def _job_log_endpoint(repo: str, job_id: Any) -> str:
    """A job's raw log (the `text()` endpoints — can be multi-MB)."""
    return f"repos/{repo}/actions/jobs/{job_id}/logs"


def _monthly_volume(client: GhClient, repo: str, wf_id: int,
                    created_before: str | None = None) -> int | None:
    """30-day total run count for the workflow (Term 0)."""
    doc = client.json(_volume_endpoint(repo, wf_id, created_before))
    if not isinstance(doc, dict):
        return None
    return doc.get("total_count")


def _monthly_event_volume(client: GhClient, repo: str, wf_id: int, event: str,
                          created_before: str | None = None) -> int | None:
    """30-day total run count for one workflow event (Term 0 for event-subset levers)."""
    doc = client.json(_volume_endpoint(repo, wf_id, created_before, event=event))
    if not isinstance(doc, dict):
        return None
    return doc.get("total_count")


def _sample_runs(client: GhClient, repo: str, wf_id: int,
                 max_runs: int, created_before: str | None = None
                 ) -> list[dict[str, Any]] | None:
    """The latest N successful runs of the workflow (≤ `created_before` if pinned).
    None when the fetch failed — see `_run_list`."""
    pin_norm = _normalize_pin(created_before)
    pin = f"&created=<={pin_norm}" if pin_norm else ""
    return _run_list(
        client,
        f"repos/{repo}/actions/workflows/{wf_id}/runs"
        f"?per_page={max_runs}&status=success{pin}")


def _success_runs_from_all_status(all_runs: list[dict[str, Any]],
                                  max_runs: int) -> list[dict[str, Any]]:
    """The `status=success` sample DERIVED from an already-fetched all-status run
    page — the same runs `_sample_runs` would return, without the second call.

    Both endpoints are the same run list newest-first; `status=success` is a server-side
    filter over it, so the latest N successes are just the first N runs of the all-status
    page that COMPLETED successfully. Verified across 32 real workflows: 29 derive the
    20-run success sample exactly. The other 3 are low-success-rate workflows with fewer
    than 20 successes inside the most-recent `_COST_RUNLIST_MAX` runs — the all-status
    page simply doesn't reach far enough back. So this is only a valid substitute when it
    yields the FULL `max_runs`; a short result means "can't see far enough", NOT "that's
    all there is", and the caller MUST fall back to the explicit query rather than audit
    a silently thinner sample."""
    out = [r for r in all_runs
           if r.get("conclusion") == "success" and r.get("status") == "completed"]
    return out[:max_runs]


def _sample_event_runs(client: GhClient, repo: str, wf_id: int, event: str,
                       max_runs: int, created_before: str | None = None
                       ) -> list[dict[str, Any]] | None:
    """Latest successful runs for one workflow event. None when the fetch failed."""
    pin_norm = _normalize_pin(created_before)
    pin = f"&created=<={pin_norm}" if pin_norm else ""
    return _run_list(
        client,
        f"repos/{repo}/actions/workflows/{wf_id}/runs"
        f"?per_page={max_runs}&status=success&event={event}{pin}")


def _all_status_runs(client: GhClient, repo: str, wf_id: int,
                     max_runs: int, created_before: str | None = None
                     ) -> list[dict[str, Any]] | None:
    """The latest N runs of ALL conclusions (≤ `created_before` if pinned) — no
    `status=success` filter, so cancelled/superseded/duplicate runs ARE visible.
    The success-only `_sample_runs` above literally can't see the runs the
    superseded-runs (OPT46) and double-trigger (OPT47) detectors measure. Same
    endpoint shape as `_sample_runs` minus the status filter, so it maps to a
    DISTINCT `_fixture_name` (the `status=success` substring differs) and replays
    cleanly. Pinned by `created<=<pin>` for deterministic regen.

    Returns None when the fetch itself FAILED (gh error/timeout — already counted in
    `client.errors`), as distinct from `[]` for a workflow that genuinely has no runs
    — the same None-vs-`[]` contract `_fetch_run_jobs` documents, and for the same
    reason. This page feeds the ENTIRE run-elimination detector family (OPT35, OPT46,
    OPT47, OPT57, OPT64) plus their `sample_denominator`. Folding a failed fetch into
    `[]` would make every one of them report CLEAN off a "0 of 0 runs" evidence line
    while the rest of the report looks healthy — a transient timeout laundered into a
    silent all-clear. The caller must keep the two apart and never cache the None.
    The contract is implemented once, in `_run_list`."""
    pin_norm = _normalize_pin(created_before)
    pin = f"&created=<={pin_norm}" if pin_norm else ""
    return _run_list(
        client,
        f"repos/{repo}/actions/workflows/{wf_id}/runs"
        f"?per_page={max_runs}{pin}")


def _all_status_event_runs(client: GhClient, repo: str, wf_id: int, event: str,
                           max_runs: int, created_before: str | None = None
                           ) -> list[dict[str, Any]] | None:
    """Latest runs of ALL conclusions for one workflow event.

    Same None-vs-`[]` contract as `_all_status_runs`, and for the same reason: this page
    is OPT57's event-scoped denominator and OPT36's schedule-run basis, so a failed fetch
    laundered into `[]` sizes them against zero runs and reports them CLEAN. None = the
    fetch failed; `[]` = the workflow genuinely has no runs for this event. Implemented
    by `_run_list`."""
    pin_norm = _normalize_pin(created_before)
    pin = f"&created=<={pin_norm}" if pin_norm else ""
    return _run_list(
        client,
        f"repos/{repo}/actions/workflows/{wf_id}/runs"
        f"?per_page={max_runs}&event={event}{pin}")


def _mean_run_compute_min(jobs_per_run: list[list[dict[str, Any]]]) -> tuple[float, int]:
    """(mean runner-MINUTES of compute per run, the COUNT of runs the mean rests
    on). For each run, sum its jobs' durations (compute, not wall — jobs may run
    in parallel); a run with no measurable job is EXCLUDED from the denominator
    (not averaged in as a zero) AND from the count. Returning the count lets the
    detector disclose the mean's real basis honestly — `jobs_per_run` is a
    success-run sample for the event being priced, thinned by fetch failures /
    zero-job runs, so it is typically smaller than the all-status slice the
    elimination COUNT comes from.
    Mean (not p50) per savings-methodology.md's runner-minute rule. (0.0, 0)
    when no run has a measurable job."""
    per_run: list[float] = []
    for run_jobs in jobs_per_run:
        secs = 0.0
        for j in run_jobs:
            d = _duration_s(j.get("started_at"), j.get("completed_at"))
            if d is not None and d > 0:
                secs += d
        if secs > 0:
            per_run.append(secs)
    if not per_run:
        return 0.0, 0
    return (sum(per_run) / len(per_run)) / 60.0, len(per_run)


def _elim_scale_and_basis(monthly_volume: int | None, sampled_n: int,
                          n_timed: int) -> tuple[float, str]:
    """(scale, basis-disclosure) for a run-elimination finding.

    `scale` maps the sampled per-run elimination rate to the 30-day run volume —
    in BOTH directions. The all-status slice is capped at the most-recent
    `_COST_RUNLIST_MAX` runs with no 30-day lower bound, so a low-frequency
    workflow's slice spans MORE than 30 days; crediting its raw count as one
    month over-states, so `monthly_volume / sampled_n` (which is < 1 there) must
    be applied, not clamped to ≥1. The disclosure states the sample size, the
    timed-run basis of the mean, and the multiplier so the /mo figure is
    self-justifying rather than a bare number."""
    scale = (monthly_volume / sampled_n) if (monthly_volume and sampled_n) else 1.0
    parts = [f"mean over {n_timed} timed run(s)"]
    if monthly_volume and sampled_n:
        parts.append(f"×{scale:.2f} to the 30d volume ({monthly_volume} runs)")
    else:
        parts.append("30d volume unknown — credited over the sampled window")
    parts.append(f"{sampled_n}-run recent slice (not a full 30d census)")
    if n_timed < 3:
        parts.append("LOW CONFIDENCE: compute mean rests on <3 timed runs")
    return scale, "; ".join(parts)


# The `per_page` every per-run jobs fetch below uses — and, because none of them
# PAGINATE, also the point at which a jobs payload may be TRUNCATED. A payload with
# this many jobs is "possibly incomplete", never "complete and exactly this long":
# `filter=all` orders jobs OLDEST-ATTEMPT-FIRST, so a >100-job run's first page can
# hold nothing but prior-attempt jobs. Anything deriving an attempt-scoped job set
# from a payload must treat a truncated payload as UNKNOWN (`_latest_attempt_jobs`,
# `_prior_attempt_jobs`) rather than as an answer.
#
# It is INTERPOLATED into every jobs URL below, never re-typed as a literal: a `100`
# hardcoded in the query string while the guards compare against this constant is a
# split-brain waiting to happen — drop `per_page` to 50 and `len(jobs) >= 100` quietly
# stops firing, so the truncation guards go dead while still LOOKING correct, and the
# silent-truncation false negative they exist to stop comes straight back.
_JOBS_PAGE_SIZE = 100


class _JobsPayload(list):
    """A run's jobs from ONE unpaginated jobs page, plus whether that page was
    TRUNCATED — i.e. whether the endpoint holds MORE jobs than it just handed us.

    A plain `list` subclass, so every consumer that only wants the jobs keeps treating
    it as the list it always was; only the attempt-scoped derivations (which are wrong
    on an incomplete payload) read `.truncated`.

    Truncation is decided from the endpoint's OWN `total_count` when it is there
    (`len(jobs) < total_count`), and only falls back to the page-size heuristic
    (a FULL page is "possibly more", never "exactly this many") when it isn't. That
    ordering matters for the future: if the fetchers are ever PAGINATED (PR #212), a
    complete 114-job payload carries `total_count == 114`, `truncated` is False, and
    every guard keyed on it becomes a no-op instead of wrongly declaring the complete
    payload UNKNOWN. A paginated fetcher should return `_JobsPayload(jobs,
    truncated=False)` and nothing else here needs to change."""

    truncated: bool = False

    def __init__(self, jobs: Any = (), *, truncated: bool = False) -> None:
        super().__init__(jobs)
        self.truncated = bool(truncated)


def _jobs_payload(doc: Any) -> _JobsPayload:
    """The `jobs` array of a jobs-endpoint response, tagged with its truncation."""
    if not isinstance(doc, dict):
        return _JobsPayload()
    jobs = doc.get("jobs") or []
    total = doc.get("total_count")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return _JobsPayload(jobs, truncated=len(jobs) < total)
    return _JobsPayload(jobs, truncated=len(jobs) >= _JOBS_PAGE_SIZE)


def _jobs_truncated(jobs: Any) -> bool:
    """Is this jobs payload possibly INCOMPLETE (so an attempt-scoped set derived from
    it is UNKNOWN, not an answer)?

    Authoritative when the payload came from a fetcher that tagged it (`_JobsPayload`).
    A bare `list` — a test's hand-built payload, a caller that rebuilt the list — has no
    tag, so fall back to the page-size heuristic rather than assuming completeness:
    UNKNOWN must never quietly become "complete"."""
    flag = getattr(jobs, "truncated", None)
    if flag is not None:
        return bool(flag)
    return len(jobs) >= _JOBS_PAGE_SIZE


def _fetch_run_jobs(client: GhClient, repo: str,
                    run_id: int) -> list[dict[str, Any]] | None:
    """A run's jobs (per-job timing/steps). Returns None when the fetch itself
    FAILED (gh error/timeout — already counted in `client.errors`), as distinct
    from `[]` for a run that genuinely has no jobs. The caller must keep the two
    apart: folding a failed fetch into "this run ran nothing" would launder a
    collection error into a smaller-than-reported P50 sample — the same rule
    `_fetch_check_runs` follows.

    PAGINATED (`_paginate`): a matrix-heavy run can exceed 100 jobs, and a single
    `per_page=100` page would silently drop the rest. The walk is complete by
    construction, so the payload is tagged `truncated=False` — exactly the shape
    `_JobsPayload`'s docstring prescribes for a paginated fetcher, and every
    attempt-scoped truncation guard downstream becomes a no-op."""
    items = _paginate(client, f"repos/{repo}/actions/runs/{run_id}/jobs", "jobs")
    if items is None:
        return None
    return _JobsPayload(items, truncated=False)


def _fetch_run_jobs_filtered(client: GhClient, repo: str, run_id: int,
                             filter_value: str) -> list[dict[str, Any]] | None:
    """A run's jobs with GitHub's run-attempt filter made explicit.

    The jobs endpoint defaults to `filter=latest`, which hides prior attempts.
    Re-run waste needs both views: `filter=all` to expose prior-attempt jobs and
    `filter=latest` as the subtraction basis for the attempt that currently owns
    the run's signal. Same None-vs-[] contract as `_fetch_run_jobs`, and the same
    full pagination (`filter=all` on a re-run repo is exactly where >100 jobs
    shows up first).
    """
    items = _paginate(client, f"repos/{repo}/actions/runs/{run_id}/jobs", "jobs",
                      params=f"filter={filter_value}")
    if items is None:
        return None
    return _JobsPayload(items, truncated=False)


def _fetch_run_jobs_all_attempts(client: GhClient, repo: str,
                                 run_id: int) -> list[dict[str, Any]] | None:
    return _fetch_run_jobs_filtered(client, repo, run_id, "all")


def _fetch_run_jobs_latest_attempt(client: GhClient, repo: str,
                                   run_id: int) -> list[dict[str, Any]] | None:
    return _fetch_run_jobs_filtered(client, repo, run_id, "latest")


class _JobFetchMemo:
    """Run-id-keyed memo over the per-run jobs fetch (`GET .../runs/{id}/jobs`).

    The data pass fetches the same run's jobs from more than one place: a
    schedule-only workflow's OPT36 probe re-samples the very runs the main pass
    already fetched (its `event_scope` IS `schedule`), and the OPT35/OPT57 failure
    probes overlap each other and the main sample on repos whose failures land in
    the sampled window. Those are pure duplicate calls — same endpoint, same
    response — so memoize the payload and let the second caller read it.

    Keyed on (fetch flavour, repo, run id): `filter=all` and the default
    `filter=latest` view of one run are DIFFERENT payloads and must not collide.

    Only SUCCESSFUL fetches are stored. A failed fetch (`None`) is left
    un-memoized so a later caller re-tries it rather than inheriting a transient
    gh error — and so the failure accounting each call site does stays honest.

    Hits return a deep copy. Callers stamp run context onto the job dicts
    (`_stamp_run_context`) and accumulate them into per-workflow samples; handing
    two call sites the same mutable dicts would alias those samples together,
    which the un-memoized code never did. The copy keeps this a pure call-count
    optimization with no aliasing semantics attached.

    De-duplication holds for CONCURRENT first accesses too, not just sequential ones.
    A plain check-then-act (read the map, drop the lock, fetch, write it back) leaves a
    window in which two threads of the same pool both miss and both call GitHub — the
    values stay correct, but the memo silently stops de-duplicating exactly when the
    pool is wide. Today's call sites hand it de-duplicated run lists so the window is
    rarely open; a cross-workflow job pool (PR #215) opens it. So the miss path takes a
    PER-KEY lock: threads racing the SAME run id serialize (the loser reads the winner's
    payload — one call), while different run ids stay fully parallel."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str, Any], list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        # One lock per memo key, minted under `_lock`. Held across the underlying fetch,
        # so it must never be the map-wide lock — that would serialize the whole pool.
        self._key_locks: dict[tuple[str, str, Any], threading.Lock] = {}

    def _key_lock(self, key: tuple[str, str, Any]) -> threading.Lock:
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock

    @staticmethod
    def _flavour(fetch: Callable[..., Any]) -> str:
        """A key component that separates `filter=all` from `filter=latest` from the
        default view — and never MERGES two distinct fetchers.

        `__qualname__` (not `__name__`) so a nested/injected fetcher carries its
        enclosing scope. Anonymous fetchers (`<lambda>`, the injected test seams) all
        share one qualname, so they'd alias each other's payloads into one memo entry;
        fall back to `repr`, which embeds the object id and is therefore unique per
        fetcher object."""
        name = getattr(fetch, "__qualname__", None) or getattr(fetch, "__name__", "")
        if not name or "<lambda>" in name or "<locals>" in name:
            return repr(fetch)
        return name

    def wrap(self, fetch: Callable[..., list[dict[str, Any]] | None]
             ) -> Callable[..., list[dict[str, Any]] | None]:
        """`fetch` with the memo in front of it — same signature, so it drops
        straight into `_gather_run_jobs`'s injectable `fetch` seam."""
        flavour = self._flavour(fetch)

        def _memoized(client: GhClient, repo: str,
                      run_id: Any) -> list[dict[str, Any]] | None:
            key = (flavour, repo, run_id)
            with self._lock:
                hit = self._hits.get(key)
            if hit is not None:
                logger.debug("job-fetch memo hit: %s run %s", flavour, run_id)
                return copy.deepcopy(hit)
            if run_id is None:
                return fetch(client, repo, run_id)   # never memoized; nothing to race
            # Miss. Serialize the fetch PER KEY and re-check under it: whoever loses the
            # race reads the winner's payload instead of making a second identical call.
            with self._key_lock(key):
                with self._lock:
                    hit = self._hits.get(key)
                if hit is not None:
                    logger.debug("job-fetch memo hit (raced): %s run %s", flavour, run_id)
                    return copy.deepcopy(hit)
                jobs = fetch(client, repo, run_id)
                if jobs is not None:
                    with self._lock:
                        self._hits[key] = copy.deepcopy(jobs)
                return jobs

        return _memoized


def _gather_run_jobs(
    client: GhClient, repo: str, runs: list[dict[str, Any]], *,
    fetch=_fetch_run_jobs,
    keep_empty: bool = False,
    memo: "_JobFetchMemo | None" = None,
) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]]]], int]:
    """Fetch each run's jobs CONCURRENTLY, preserving INPUT order. One
    `GET .../runs/{id}/jobs` per sampled run is the dominant call cost on a busy
    repo (hundreds of runs); run serially it walls the whole data pass. `gh` is its
    own process (thread-safe), the fetcher never raises, and `pool.map` preserves
    INPUT order, so the result is deterministic regardless of finish order.

    Runs on the ONE shared `_fetch_pool` (not a fresh per-call pool), so N concurrent
    fan-out sites still put at most `_FETCH_CONCURRENCY` gh calls in flight. Callers
    that already parked these runs' job listings with `prefetch_json` (the global
    shallow-sample pool in `collect`) hit the buffer here and pay nothing.

    Returns `(kept, failures)`:
      - `kept` is `[(run, jobs), …]` in INPUT order for runs that returned a
        NON-EMPTY job list — what the caller buckets into the P50 sample. When
        `keep_empty` is true, empty successful fetches are retained for join
        callers that need to distinguish "fetched zero jobs" from "fetch failed".
      - `failures` counts runs whose fetch FAILED (`None`) — distinct from a run
        that genuinely had no jobs (`[]`, dropped from `kept` but NOT counted as a
        failure). The caller surfaces this as a coverage gap so a degraded sample
        is disclosed, not laundered into 'ran nothing / clean'.

    `fetch` is injectable so the concurrency/ordering contract is unit-testable
    without real gh calls. `memo` (a `_JobFetchMemo`) de-duplicates run ids this
    data pass has ALREADY fetched — see its docstring; it changes no returned
    value, only the call count.

    The memo wrap sits BENEATH the shared pool: a run id already fetched this pass
    returns its memoized payload without a gh call, and everything else fans out over
    the ONE `_fetch_pool` so the in-flight ceiling stays `_FETCH_CONCURRENCY` no matter
    how many gather sites are live."""
    if memo is not None:
        fetch = memo.wrap(fetch)
    run_jobs = list(_fetch_pool().map(
        lambda r: fetch(client, repo, r.get("id")), runs))
    kept: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    failures = 0
    for run, jobs in zip(runs, run_jobs):
        if jobs is None:
            failures += 1
            continue
        if jobs or keep_empty:
            kept.append((run, jobs))
    return kept, failures


def _fetch_check_runs(client: GhClient, repo: str, sha: str) -> list[dict[str, Any]] | None:
    """ALL check-runs on a commit — the full set of checks a PR waits on,
    INCLUDING ones with no workflow file (CodeQL default setup, third-party app
    checks). This is the ground truth for the developer's wall-clock critical
    path; the file-based scan can't see fileless checks.

    Returns None when the fetch itself FAILED (gh error/timeout — already counted
    in `client.errors`), as distinct from `[]` for a commit that genuinely ran no
    checks. The caller must keep the two apart: folding a failed fetch into "this
    PR ran nothing" would launder a collection error into a clean sample.

    PAGINATED (`client.paginate`). This endpoint is where the truncation bug was
    MEASURED: better-auth/better-auth @ 6f20f44 reported `total_count = 103` and
    a single `per_page=100` page returned 100 — three checks silently missing from
    the critical-path sample, any of which could have been the merge pole."""
    return _paginate(client, f"repos/{repo}/commits/{sha}/check-runs", "check_runs")


# Conclusions for which GitHub has NO log to serve BECAUSE THE JOB NEVER STARTED:
# `actions/jobs/{id}/logs` 404s. Fetching it anyway is a guaranteed 404, and a 404
# used to bump `errors` — which is how a fully-successful better-auth run acquired
# `gh_error_count: 4` and rendered a phantom partial-coverage banner.
#
# `cancelled` is deliberately NOT here. The test is whether the job STARTED, not
# what it concluded: a job cancelled MID-RUN (a superseded push, a
# `cancel-in-progress` concurrency group, a manual cancel) has a real, partial log
# that GitHub serves 200 — and it can be long enough to BE the drilled pole
# (`_persist_pole_logs` picks the representative job by DURATION and never filters
# on conclusion). Skipping it outright would silently delete that pole's drill-down:
# a coverage regression dressed up as a coverage fix. Only a job cancelled while
# still QUEUED has no log, and that one has no `started_at` — see `_job_has_log`.
_JOB_CONCLUSIONS_WITHOUT_LOGS = frozenset({"skipped"})


def _job_started(job: dict[str, Any]) -> bool:
    """Whether the job ever began executing (so GitHub has bytes to serve).
    `started_at` is the primary signal; a non-empty `steps` array is the backstop
    for a payload shape that omits it."""
    if job.get("started_at"):
        return True
    steps = job.get("steps")
    return isinstance(steps, list) and bool(steps)


def _job_has_log(job: dict[str, Any] | None) -> bool:
    """Whether this job can have a log at all.

    Three "no":
      - `skipped` — the job never executed (`_JOB_CONCLUSIONS_WITHOUT_LOGS`).
      - `conclusion is None` — the job is QUEUED or IN PROGRESS. In-flight runs ARE
        sampled (`_all_status_runs` applies no `status=completed` filter), so on a
        repo audited mid-CI-storm this is common; a queued job's log is a guaranteed
        404 and would re-manufacture the phantom partial-coverage banner.
      - `cancelled` AND never started — cancelled while still queued, so there is no
        log. A cancelled job that DID start has a real partial log (see the note on
        `_JOB_CONCLUSIONS_WITHOUT_LOGS`) and IS fetched."""
    if not job:
        return False
    conclusion = job.get("conclusion")
    if conclusion is None or not str(conclusion).strip():
        return False                                   # queued / in progress
    c = str(conclusion).strip().lower()
    if c in _JOB_CONCLUSIONS_WITHOUT_LOGS:
        return False
    if c == "cancelled":
        return _job_started(job)
    return True


def _fetch_job_log(client: GhClient, repo: str,
                   job: dict[str, Any] | None) -> str | None:
    """A job's raw log, or None.

    Skips the fetch entirely for a job that has no log to serve (`_job_has_log`):
    that 404 is EXPECTED, and an expected 404 is not a collection error. That keeps
    `errors` — and therefore the report's partial-coverage banner — a signal about
    real failures.

    `allow_missing=True` for the same reason on the fetches we DO make: GitHub
    deletes job logs after the repo's `retention-days` (default 90), and this skill
    supports pinned windows (`--created-before`), so auditing a window older than
    retention 404s EVERY log — which would fire the phantom banner at full strength.
    An absent log is a disclosed drill-down gap (`_persist_pole_logs` warns), not a
    collection error. This is only safe BECAUSE `_invoke` counts a rate-limit / 5xx /
    timeout exhaustion regardless of `allow_missing` — a job whose log we were
    BLOCKED from still counts."""
    if not _job_has_log(job):
        logger.debug("skipping log fetch for job %s (conclusion=%r, started=%s — "
                     "no log to serve)", (job or {}).get("id"),
                     (job or {}).get("conclusion"), _job_started(job or {}))
        return None
    jid = (job or {}).get("id")
    if not jid:
        # Every job the gh API returns carries an id, so this is unreachable in
        # practice — but "couldn't identify it → nothing here" is precisely the
        # silent-drop conversion this module exists to prevent, so say it out loud
        # rather than returning a quiet None that reads downstream as "no log".
        logger.warning(
            "job log fetch SKIPPED: job %r has no id, so its log cannot be "
            "requested — treating it as an absent log (a drill-down gap, not an "
            "empty log)", job)
        return None
    return client.text(f"repos/{repo}/actions/jobs/{jid}/logs", allow_missing=True)


def _select_repr_shas(
    sha_ts: dict[str, str],
    fetch_checks: Callable[[str], dict[str, float] | None],
    req_names: frozenset[str],
    target: int,
) -> tuple[list[str], list[dict[str, float]], dict[str, list[float]], dict[str, Any]]:
    """Pick up to `target` PR head-SHAs for the measured critical path.

    Walks candidates NEWEST-FIRST (by their most-recent run timestamp in
    `sha_ts`), fetches each one's `{check_name: duration}` via `fetch_checks`,
    and keeps it only if it ran the FULL required suite (`req_names` is a subset
    of the PR's checks). With no readable required set (`req_names` empty), keeps
    any PR whose fetched check-runs carried >=1 timed check — a recency-only
    fallback. Stops as soon as `target` PRs are kept, so no more check-runs are
    fetched than needed.

    OBSERVABLE required subset: a required check that never appears as a check-run
    anywhere in the sampled recency pool (a status-only external gate — a `Devin Review`
    GitHub-App commit STATUS, an enterprise status check) CANNOT be sampled, so requiring it on
    every PR would empty the suite even on a repo whose in-repo aggregator gates every
    PR. So when the strict walk comes up short, the suite test is re-run over the
    recency pool using only the required checks actually OBSERVED at least once; the
    excluded ones are reported in `required_checks_unobservable`. This fires only when
    the required set MIXES observable and unobservable checks — a fully-external suite
    (nothing observed) is left to the external-gate fallback below.

    EXTERNAL-GATE fallback: when the required suite IS readable but no sampled PR
    carries it — every required check is external/managed (a CLA bot, enterprise
    CI, label-gated full-e2e, a mergeability gate) that maps to no workflow in this
    repo — the required-suite filter keeps zero. Rather than return an empty sample
    (and a dead-ended critical path), a recency-only pool is accumulated alongside
    the walk and PROMOTED only when the required filter kept nothing. The critical
    path is then built from the file-backed work a normal PR actually runs, clearly
    demoted downstream as the PR-floor. Costs nothing on a normal repo: when the
    suite is satisfiable, `repr_shas` fills and the walk stops early before the pool
    matters. `required_suite_unsatisfiable` in `sample_diag` records the promotion.

    `fetch_checks` returns None for a FAILED fetch (gh error/timeout) vs `{}` for
    a PR that genuinely ran no timed checks. The two are kept apart: a failed
    fetch is counted as a coverage gap, NOT silently folded into "this PR is
    clean" (which would drift the sample to older commits with no signal). The
    walk pursues the FULL `target`: it stops only when `target` qualifying PRs are
    kept or the candidate window is exhausted. A generous `max(target*8, 120)`-fetch
    ceiling remains as a runaway backstop for a pathologically large window — it is
    NOT the primary limiter (the old tight `max(target*3, target+8)` cap — 60 at
    the default target — could stop the walk at ~10/20 on repos whose recent PRs
    mostly ran a partial suite, leaving the sample short of the 20-PR floor;
    reaching the floor is now worth the extra gh calls).

    Returns `(repr_shas, per_sha_checks, check_durs, sample_diag)`:
    `per_sha_checks` is one `{check: dur}` map per kept PR (for population
    segmentation); `check_durs` accumulates each check's durations across the
    kept PRs (for the p50); `sample_diag` records the sampling outcome — target,
    kept, fetched, fetch_failures, complete, required_suite_scoped — so a SHORT or
    fetch-degraded sample is surfaced honestly rather than passed off as full."""
    candidates = sorted(sha_ts, key=lambda s: sha_ts[s], reverse=True)
    # Runaway backstop only — the walk normally stops at `target` kept or window
    # exhausted; this just bounds gh calls on a pathologically large window.
    max_fetch = max(target * 8, 120)
    check_durs: dict[str, list[float]] = {}
    per_sha_checks: list[dict[str, float]] = []
    repr_shas: list[str] = []
    # Recency-only fallback pool, accumulated only while a required suite is in force.
    # Promoted ONLY if the required filter keeps zero (an all-external/managed required
    # suite no PR carries); otherwise discarded untouched. Capped at `target`.
    fb_shas: list[str] = []
    fb_per_sha: list[dict[str, float]] = []
    fb_durs: dict[str, list[float]] = {}
    fetched = 0
    fetch_failures = 0
    # Each check-run fetch is independent, latency-bound gh I/O, so issue them in
    # CONCURRENT batches but PROCESS each batch's results in candidate order — the
    # "newest-first, first `target` qualifying" selection and the fetch-failure /
    # coverage-gap accounting stay identical to a sequential walk, only faster. The
    # batch is capped to the number STILL NEEDED (`target - kept`) as well as the
    # concurrency width and the `max_fetch` ceiling, so we NEVER issue a call past the
    # target: an over-fetched failure can't be mis-counted as a coverage gap, the
    # quick-qualifying case stays frugal, and we don't burn rate limit on PRs we don't
    # need. (`pool.map` returns results in input order, so selection is deterministic
    # regardless of which thread finishes first; only the final convergence — when
    # fewer than the width remain to find — runs narrower than the full width.)
    idx, n = 0, len(candidates)
    pool = _fetch_pool()
    while idx < n and len(repr_shas) < target and fetched < max_fetch:
        size = min(_FETCH_CONCURRENCY, n - idx, max_fetch - fetched,
                   target - len(repr_shas))
        batch = candidates[idx:idx + size]
        idx += size
        for sha, m in zip(batch, pool.map(fetch_checks, batch)):
            fetched += 1
            if m is None:
                # The check-runs fetch FAILED — count it as a gap, don't launder
                # it into "ran nothing" and silently substitute an older commit.
                # (The batch cap guarantees this fetch was NEEDED, not over-fetched
                # past an already-complete target.)
                fetch_failures += 1
                continue
            if not m:
                continue
            # Recency-only pool (any PR with >=1 timed check), kept ONLY while a
            # required suite is in force and promoted iff that suite turns out
            # unsatisfiable. On a normal repo this fills harmlessly and is discarded.
            if req_names and len(fb_shas) < target:
                fb_shas.append(sha)
                fb_per_sha.append(m)
                for name, d in m.items():
                    fb_durs.setdefault(name, []).append(d)
            # "Ran the full CI suite" = the PR's check-runs include every required
            # status check. With no readable required set, accept any PR that ran
            # >=1 tracked check (recency-only fallback).
            if req_names and not req_names <= set(m):
                continue
            repr_shas.append(sha)
            per_sha_checks.append(m)
            for name, d in m.items():
                check_durs.setdefault(name, []).append(d)
    # Status-only / external required checks: a required check that never appears as a
    # check-run anywhere in the sampled window (e.g. a "Devin Review" GitHub-App commit
    # STATUS — not a check-run, or an enterprise gate that posts a status) CANNOT be
    # sampled, so the strict subset test above (`req_names <= set(m)`) rejects EVERY PR and
    # empties the required-suite sample on an otherwise-normal repo — wrongly tipping it
    # into the external-gate fallback even though an in-repo required aggregator (e.g.
    # `All PR Checks`) gates every PR. If the walk came up short, re-select over the
    # recency pool using only the OBSERVABLE required checks (those seen at least once).
    # Fires ONLY when some required check is unobservable AND some is observable: a
    # genuinely all-external suite leaves `req_observable` empty and is left to the
    # unsatisfiable promotion below (PR-floor fallback, unchanged); a fully-observable
    # suite that's merely short is also left as-is (a genuinely partial-suite repo).
    required_checks_unobservable: list[str] = []
    if req_names and len(repr_shas) < target and fb_per_sha:
        observed: set[str] = set().union(*(set(fm) for fm in fb_per_sha))
        req_observable = set(req_names) & observed
        req_unobservable = set(req_names) - observed
        if req_observable and req_unobservable:
            required_checks_unobservable = sorted(req_unobservable)
            repr_shas, per_sha_checks, check_durs = [], [], {}
            for sha, fm in zip(fb_shas, fb_per_sha):
                if req_observable <= set(fm):
                    repr_shas.append(sha)
                    per_sha_checks.append(fm)
                    for name, d in fm.items():
                        check_durs.setdefault(name, []).append(d)
    # External-gate promotion: a readable required suite that no sampled PR carried
    # kept zero. Fall back to the recency pool so the critical path is built from the
    # file-backed work a normal PR runs (demoted to the PR-floor downstream).
    required_suite_unsatisfiable = bool(req_names) and not repr_shas and bool(fb_shas)
    if required_suite_unsatisfiable:
        repr_shas, per_sha_checks, check_durs = fb_shas, fb_per_sha, fb_durs
        # The suite collapsed to the PR-floor fallback, whose own external-gate disclosure
        # owns the messaging — so the observable-subset narrative no longer applies. Clear
        # the field so the same external gate isn't double-disclosed (both "unsatisfiable"
        # AND "excluded as unobservable"), which would otherwise be a contradictory state
        # (`required_suite_scoped=False` alongside a populated unobservable list).
        required_checks_unobservable = []
    sample_diag = {
        "target": target,
        "kept": len(repr_shas),
        "fetched": fetched,
        "fetch_failures": fetch_failures,
        "complete": len(repr_shas) >= target,
        # True only when the FINAL sample is the required-suite one; a promoted
        # recency fallback is recency-scoped, so it reads False here.
        "required_suite_scoped": bool(req_names) and not required_suite_unsatisfiable,
        "required_suite_unsatisfiable": required_suite_unsatisfiable,
        # Required checks that never appeared as a check-run in the window (status-only /
        # external) and were therefore excluded from the per-PR suite test — disclosed so
        # the report can name what was scoped out, never silently treated as satisfied.
        "required_checks_unobservable": required_checks_unobservable,
    }
    # The walk now pursues the full target past the old `max(target*3, target+8)`
    # cap, so a rejection-heavy window can issue many more check-run fetches than the
    # kept count implies. Surface that cost when it exceeds the old cap so a slow run
    # is explained (and walk depth is visible in the logs, not just the report).
    if fetched >= max(target * 3, target + 8):
        logger.info(
            "repr-PR walk reached %d check-run fetches (kept %d/%d) to clear the "
            "required-suite filter — at or past the old %d-fetch cap",
            fetched, len(repr_shas), target, max(target * 3, target + 8))
    return repr_shas, per_sha_checks, check_durs, sample_diag


def _required_check_conditionality(doc: dict[str, Any]) -> dict[str, str]:
    """Per required check: the PR-trigger conditionality of the workflow that
    carries it — unconditional / path_scoped / branch_scoped /
    merge_group_scoped / not_pr_triggered / unknown. The dead-required check
    may call a required check dead ONLY when it is unconditional in YAML
    yet absent across the whole sample; everything unresolvable lands `unknown`
    (never fail) — blame-precision over coverage.

    The join uses the scan's `workflow_job_graph` name templates against each
    required check name (exact, matrix `name (`-prefix, reusable `name / `-
    prefix; templates are cut at their first `${{` placeholder). Multiple
    matching workflows must AGREE or the answer is `unknown`."""
    conditionality = doc.get("workflow_trigger_conditionality") or {}
    job_graph = doc.get("workflow_job_graph") or {}
    out: dict[str, str] = {}
    for check in doc.get("required_checks") or []:
        name = str(check)
        matched: set[str] = set()
        for wf, jobs in job_graph.items():
            for meta in (jobs or {}).values():
                template = str((meta or {}).get("name") or "")
                template = template.split("${{", 1)[0].rstrip()
                if not template:
                    continue
                if (name == template or name.startswith(template + " (")
                        or ((meta or {}).get("reusable")
                            and name.startswith(template + " / "))):
                    matched.add(str(wf))
        kinds = {conditionality.get(wf, "unknown") for wf in matched}
        out[name] = kinds.pop() if len(kinds) == 1 else "unknown"
    return out


def _sampling_provenance_fields(sample_diag: dict[str, Any]) -> dict[str, Any]:
    """Map `_select_repr_shas`'s `sample_diag` onto the `sample_*` doc keys that
    `blocking_path._provenance_block` reads. Isolated as a pure function so the
    key-mapping (diag key -> doc key) is unit-testable: a typo here would silently
    zero out a coverage caveat in the report, which no end-to-end test would catch
    (collect() makes live gh calls, so it isn't exercised in the suite)."""
    return {
        "sample_target": sample_diag["target"],
        "sample_complete": sample_diag["complete"],
        "sample_fetch_failures": sample_diag["fetch_failures"],
        "sample_fetched": sample_diag["fetched"],
        "required_suite_scoped": sample_diag["required_suite_scoped"],
        # Present on real diags; defaulted so an older/hand-built diag (or a test
        # fixture) without it maps cleanly instead of raising.
        "required_suite_unsatisfiable":
            sample_diag.get("required_suite_unsatisfiable", False),
        "required_checks_unobservable":
            sample_diag.get("required_checks_unobservable", []),
    }


def _pole_provenance(gate_kind: str | None, spine_required_scoped: bool,
                     pole_required_reachable: bool = True) -> str:
    """The cross-repo `provenance` stamp on `pr_critical_path` — HOW we know the
    headlined pole is the thing a PR merge actually waits on. The ci-harness auto-fixer
    consumes this and HALTs on `unresolved` rather than optimizing a pole it can't trust
    (DESIGN-PROPOSAL §3.2 "Provenance, not just shape" / §4.1 the skill-side counterpart;
    see PLAN-…-fixes.md, cross-repo seam). Isolated as a pure
    function so the mapping is unit-testable: `collect()` makes live gh calls and isn't
    exercised in the suite, so an inline-only version would have no test.

      required_scoped   — the spine was narrowed to confirmed merge-blocking (required)
                          checks (`spine_required_scoped`) AND the headlined pole is itself
                          required-reachable (`pole_required_reachable`): the pole IS a
                          required gate. Both are needed — a narrowed spine can still keep a
                          file-backed-but-unpinnable check (matrix / reusable display-name
                          mismatch) that isn't actually merge-blocking; if THAT is the pole,
                          stamping `required_scoped` would be the langfuse class narrowed,
                          not closed, so it falls through to `unresolved`.
      pr_floor_fallback — no file-backed required gate to drill; the pole is the measured
                          PR-floor (the work a normal PR runs). A real, useful state
                          (#86's no-run-history / external-managed-gate path).
      unresolved        — required checks were UNREADABLE / partial (branch protection
                          404s), OR the headlined pole couldn't be confirmed merge-blocking,
                          so it's a best guess the consumer must not trust as the gate.
    """
    if gate_kind == "pr_floor_fallback":
        return "pr_floor_fallback"
    if spine_required_scoped and pole_required_reachable:
        return "required_scoped"
    return "unresolved"


def _segment_pr_populations(
    per_sha_checks: list[dict[str, float]],
    pr_check_p50: dict[str, float],
    job_p50_all: dict[str, float],
    job_bimodal_all: dict[str, dict[str, Any]] | None = None,
) -> list[PrPopulation]:
    """Return ONE population per sampled PR THAT RAN >=1 tracked check —
    (1/m, [(check, dur), ...]) for the checks that ran on that PR, where m is
    that count — when at least one check is BIMODAL across PRs (mastra
    `changed-tests`: ~750s on code PRs, self-skips to well under its full
    duration on docs PRs). The measured-critical-path bound then credits a
    finding the EXPECTED wall-clock over the real PR distribution: a check that's
    the pole only on docs PRs (where the gate self-skips) earns its saving
    weighted by the docs-PR fraction, while a gate that's long only on code PRs
    caps savings to those. This is correct where a flat 2-way split is not — the
    "non-code" PRs are a mix (docs, misc) and only the docs subset has e2e-docs
    as the pole, so per-PR weighting captures it; a coarse split dilutes it to
    zero.

    The share denominator m is the number of EMITTED populations (PRs with >=1
    tracked check), NOT the raw sampled-PR count: a sampled PR whose check-runs
    were never fetched (empty map) or carried no tracked check must not silently
    shrink each population's share below 1/m and understate every finding's
    saving. With m as the denominator the shares sum to ~1.0 (modulo per-share
    rounding), so the bound's expected value is over the PRs we actually have a
    critical path for.

    Per-PR magnitudes are the check-run spans CAPPED at the reliable job p50
    (when the check maps to a sampled job) — a check-run's start→complete span can
    be inflated by queue/re-run time, and an inflated span must not fabricate a
    pole. The bimodal DECISION uses the SAME job-p50-capped magnitudes, so a
    single queue-inflated span can't fabricate a population split.

    Exception for a job that is itself BIMODAL (`job_bimodal_all[name]`): its
    overall p50 sits on the FAST mode, so capping every span at p50 would clamp
    the genuine SLOW-mode runs to the fast median and they could never be the pole
    on the PRs where they really ran slow — undercounting the gate-frequency of
    the very slow mode `_bimodal_split` already detected and the report warns about
    (embrace-android-sdk: `gradle (test)` ~18m39s on ~38% of runs read as "gates
    2/20" because the slow spans were clamped to the 227s p50). For such a job the
    reliable upper bound is its SLOW-mode median (`high_p50_s`), so cap there: the
    slow mode survives and can win its populations, while queue inflation above the
    slow mode is still clamped. Returns [] when too few PRs to form an expected
    value, or when no check is bimodal (the single aggregate path gives the same
    answer)."""
    if len(per_sha_checks) < 6 or not pr_check_p50:
        return []
    job_bimodal_all = job_bimodal_all or {}
    # The reliable per-job upper bound for capping: the job p50, RAISED to the
    # slow-mode median for a bimodal job so its slow mode isn't clamped away (see
    # docstring). Anything above this bound is queue/re-run inflation and clamped.
    # The SAME cap the pole-frequency ranking uses (`_pole_caps`), so the headline the engine
    # crowns and the per-population magnitudes are inflation-proofed on identical values.
    cap_all: dict[str, float] = _pole_caps(job_p50_all, job_bimodal_all)
    # Cap each per-PR span at that bound up front, and keep only PRs that ran >=1
    # tracked check. Capping here means BOTH the bimodal decision below and the
    # per-population magnitudes are computed from the same inflation-proof values;
    # dropping empty maps keeps the share denominator honest (see docstring).
    capped: list[dict[str, float]] = []
    for m in per_sha_checks:
        cm: dict[str, float] = {}
        for name, raw in m.items():
            if name not in pr_check_p50:
                continue
            eff = min(raw, cap_all[name]) if name in cap_all else raw
            if eff > 0:
                cm[name] = round(eff, 1)
        if cm:
            capped.append(cm)
    n = len(capped)
    if n < 6:
        return []
    # Only segment if SOME check is a real population signal — long on some PRs
    # but INACTIVE (absent, or self-skipped well under its full duration) on
    # others. Both forms matter: a gate that self-skips AND a workflow that
    # simply doesn't run on a PR type each split the population. Otherwise the
    # single aggregate path gives the same answer.
    bimodal = False
    for name in pr_check_p50:
        high = max((cm[name] for cm in capped if name in cm), default=0.0)
        if high < 120:
            continue
        active = sum(1 for cm in capped if (cm.get(name) or 0.0) >= 0.4 * high)
        if 0 < active < n:  # active on some PRs, inactive on others
            bimodal = True
            break
    if not bimodal:
        return []
    share = round(1.0 / n, 4)
    pops: list[PrPopulation] = []
    for cm in capped:
        checks = sorted(cm.items(), key=lambda kv: -kv[1])
        pops.append((share, tuple(checks)))
    return pops


def _job_duration_s(job: dict[str, Any]) -> float | None:
    return _duration_s(job.get("started_at"), job.get("completed_at"))


def _billable_equiv_min(job: dict[str, Any]) -> int | None:
    """Derived billed-equivalent minutes for ONE job: ceil(seconds / 60), per
    GitHub's documented per-job round-up. A MINUTES concept — no rates involved.
    Returns:
      - 0    for a `conclusion == "skipped"` job (a KNOWN zero — bills nothing),
             or a clamped-negative / zero span.
      - None when the duration is UNKNOWN (missing/unparseable timestamps) — an
             unpriceable coverage gap, never silently counted as 0 billed
             minutes (the 'no silent drops' rule: unknown != free).
      - ceil(secs/60) otherwise.
    The start->finish span (jobs-API `started_at`->`completed_at`) excludes queue
    time, and the ceil is the per-job round-UP. This matches BOTH GitHub's and
    StarSling's published billing MECHANICS (round up to the minute, billed job
    start->finish, queue unbilled) — a rate-free minutes count either vendor
    would multiply by its own per-minute rate. (Relocated from the retired
    billing.py, 2026-07-20 pricing excision; the pre-public development archive
    preserves the module.)"""
    if (job.get("conclusion") or "").lower() == "skipped":
        return 0
    secs = _job_duration_s(job)
    if secs is None:
        return None
    if secs <= 0:
        return 0
    return int(math.ceil(secs / 60.0))


def _bimodal_split(durs: list[float], *, min_n: int = 8, min_frac: float = 0.30,
                   min_ratio: float = 1.6, min_high_s: float = 60.0) -> dict[str, Any] | None:
    """Detect a fast/slow split in a job's duration sample. A check's P50 ranks it
    on the merge-wait spine; a strongly BIMODAL job (e.g. turbo-cache hit vs miss,
    or changed-files conditional) whose median lands on the fast mode is ranked low
    and silently dropped from the spine even though it is a long gate on a large
    share of PRs. This flags that case so the report can surface it instead.

    Splits at the largest gap between sorted durations and returns the two clusters'
    medians + the slow-cluster share, but ONLY when both clusters are substantial
    (>= min_frac) and well-separated (high/low >= min_ratio) — so a lone outlier or
    a smooth spread is NOT called bimodal. Returns None otherwise."""
    vals = sorted(d for d in durs if d and d > 0)
    n = len(vals)
    if n < min_n:
        return None
    best_gap, split = -1.0, 0
    for i in range(1, n):
        gap = vals[i] - vals[i - 1]
        if gap > best_gap:
            best_gap, split = gap, i
    if not split:
        return None
    low, high = vals[:split], vals[split:]
    if min(len(low), len(high)) < min_frac * n:
        return None
    lo_med, hi_med = _percentile(low, 50), _percentile(high, 50)
    if lo_med <= 0 or hi_med / lo_med < min_ratio:
        return None
    if hi_med < min_high_s:
        return None  # a sub-minute "slow" mode isn't a meaningful gate (e.g. 2s->4s)
    return {"n": n, "slow_n": len(high), "slow_frac": round(len(high) / n, 2),
            "low_p50_s": round(lo_med, 1), "high_p50_s": round(hi_med, 1)}


def _critical_path(jobs_per_run: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """For each job name, compute p50 ON THAT JOB'S OWN DOMINANT RUNNER, then
    long pole = max p50; floor = second-tallest. The model holds when
    wall_clock ≈ long_pole (cluster floor gates further wall-clock savings).

    Per-job runner scoping is what keeps the long pole honest on repos that run
    the same job across heterogeneous runners (github-hosted vs self-hosted,
    which can differ ~2-3x). A job's p50 blended across runners is meaningless,
    and — worse — a small sample catching a job's slow-runner runs while another
    job catches its fast-runner runs inverts the ranking (the better-auth bug:
    a github-hosted `test` looked slower than a self-hosted prisma-adapter job
    when, on either runner consistently, the adapter job is the real long pole).
    Each job is measured on the runner it ACTUALLY runs on most — its
    representative duration. Heavy jobs and light aggregator jobs that sit on
    different runners are each measured correctly."""
    # name -> runner_label -> [durations]
    by_job_runner: dict[str, dict[str, list[float]]] = {}
    for run_jobs in jobs_per_run:
        for j in run_jobs:
            d = _job_duration_s(j)
            if d is None or d <= 0:
                continue
            name = str(j.get("name", "?"))
            label = _job_runner_label(j) or "?"
            by_job_runner.setdefault(name, {}).setdefault(label, []).append(d)

    job_p50: dict[str, float] = {}
    job_p95: dict[str, float] = {}
    job_runner: dict[str, str] = {}
    job_bimodal: dict[str, dict[str, Any]] = {}
    for name, by_runner in by_job_runner.items():
        # This job's dominant runner = the one it ran on most (most samples).
        dominant = max(sorted(by_runner), key=lambda r: len(by_runner[r]))
        job_p50[name] = _percentile(by_runner[dominant], 50)
        job_p95[name] = _percentile(by_runner[dominant], 95)
        job_runner[name] = dominant
        # Bimodality on the SAME (dominant-runner) sample the p50 comes from, so a
        # fast-median-but-often-slow job can be surfaced instead of silently dropped.
        bi = _bimodal_split(by_runner[dominant])
        if bi is not None:
            job_bimodal[name] = bi
    if not job_p50:
        return {"long_pole_job": "", "long_pole_p50": 0.0, "long_pole_p95": 0.0,
                "floor_p50": 0.0, "job_p50": {}, "job_bimodal": {},
                "runner_scope": "all-runners"}
    ranked = sorted(job_p50.items(), key=lambda kv: -kv[1])
    long_pole = ranked[0]
    floor = ranked[1][1] if len(ranked) > 1 else 0.0
    lp_runner = job_runner.get(long_pole[0], "")
    return {
        "long_pole_job": long_pole[0],
        "long_pole_p50": long_pole[1],
        # The long-pole job's p95 (dominant runner) — its slow tail. The adaptive
        # deepen ranker uses max(p50, p95) so a fast-median/high-tail (bimodal) gate
        # isn't buried below the deepen cut by its median alone.
        "long_pole_p95": job_p95.get(long_pole[0], long_pole[1]),
        "floor_p50": floor,
        "job_p50": job_p50,
        "job_bimodal": job_bimodal,
        # Per-job dominant runner label string (job name -> sorted labels, e.g.
        # "ubuntu-latest"). Kept so OPT65's same-runner rounding check can resolve
        # a finding's runner from its affected job; additive, read only there.
        "job_runner": job_runner,
        # The long-pole job's runner — the population that gates the wait.
        "runner_scope": lp_runner if lp_runner and lp_runner != "?" else "all-runners",
    }


# =============================================================================
# Sizing model — per pattern
# =============================================================================

# Each entry says how to size a finding of this pattern:
#   - "direct": wall-clock saving = `addressable seconds` per run; runner-min
#     scales by monthly volume × addressable seconds.
#   - "parallel-rebalance": wall-clock saving comes from spreading work across
#     more shards; runner-min near 0.
#   - "runner-min-only": no wall-clock saving (below floor or off the critical
#     path); runner-min = monthly volume × per-run cost × hit-rate.
#   - "wall-clock-negative": fix saves runner-min but adds wall-clock (e.g.,
#     build-once + fan-out is a serial gate); flagged so the report can warn.
#
# A pattern in the catalog that isn't here is sized as None/None and rendered
# qualitatively. That's the honest path — the scanner never invents a number.

_SIZING: dict[str, dict[str, Any]] = {
    # --- Static fast-path patterns (scan.py registers detectors for these) ---
    "OPT17": {"model": "direct", "default_s": 30.0},
    "OPT23": {"model": "parallel-rebalance"},
    "OPT28": {"model": "direct", "default_s": 5.0},
    "OPT32": {"model": "runner-min-only", "hit_rate": 0.3},
    # OPT35's STATIC fallback (residual appendix rows, when a shard matrix has no
    # failed-run history for the measured upgrade to fire) names ONE matrix job in
    # affected_jobs — so it must be sized off THAT job's own p50, never the
    # workflow long pole. Long-pole pricing is the same #113 defect class as OPT29:
    # a shard matrix that is NOT the long pole (a small matrix beside a bigger job)
    # would be credited 0.1 × the long pole × full volume, which can exceed the
    # matrix job's own measured billable and trip `check_saving_within_measured_compute`.
    # `scope: "job"` (mirroring OPT30/31/40) bounds it to the affected job's own
    # compute. The MEASURED OPT35 detector (sizing_basis "measured") is untouched —
    # it credits actual post-failure sibling waste, a subset of the job's compute.
    "OPT35": {"model": "runner-min-only", "hit_rate": 0.1, "scope": "job"},
    "OPT36": {"model": "runner-min-only", "hit_rate": 1.0},
    # OPT45 (missing concurrency) cancels superseded runs → a WHOLE-RUN cancel.
    # `cost_basis: affected_jobs` routes its saving off the AFFECTED jobs' own
    # compute (their summed p50, then re-grounded in the cost spine's MEASURED
    # billable compute), never the workflow long pole priced at full volume —
    # which over-credited the bill vs. what those jobs measurably consume (#33).
    "OPT45": {"model": "runner-min-only", "hit_rate": 0.2, "cost_basis": "affected_jobs",
              "cost_basis_label": "the share reclaimed by cancelling superseded runs"},
    # --- Data-driven detectors (numbers come from sampled gh data, not
    # the table — listed here only so the "no sizing model" fallback
    # doesn't fire). ---
    "OPT24": {"model": "measured"},
    "OPT25": {"model": "measured"},
    "OPT43": {"model": "measured"},
    "OPT48": {"model": "measured"},
    "OPT49": {"model": "measured"},
    "OPT50": {"model": "measured"},
    "OPT51": {"model": "measured"},
    # --- Static patterns reached via the agentic catalog walk. Per-pattern
    # default_s and hit_rate are conservative defaults derived from the
    # corresponding catalog body's "Fix" / "Wall-clock vs runner-minutes"
    # discussion. They sit alongside the measured layers so qualitative
    # findings get into the runner-min ranking; the report's "X sized
    # of Y findings" disclosure keeps the basis honest. ---
    # Caching family — adding a cache typically saves the affected step's
    # install/build cost. Catalog bodies cite 30-90s ranges; 30s is a
    # conservative default that won't overstate.
    "OPT1":  {"model": "direct", "default_s": 30.0},  # unnecessary tool install
    "OPT2":  {"model": "direct", "default_s": 30.0},  # uncached large downloads
    "OPT3":  {"model": "direct", "default_s": 30.0},  # turbo cache misconfig
    "OPT4":  {"model": "direct", "default_s": 60.0},  # docker layer cache missing
    "OPT5":  {"model": "direct", "default_s": 25.0},  # pnpm store not cached
    "OPT6":  {"model": "direct", "default_s": 30.0},  # cache key entropy too high
    "OPT7":  {"model": "direct", "default_s": 5.0},   # pnpm version drift
    "OPT8":  {"model": "direct", "default_s": 20.0},  # cache key granularity
    "OPT9":  {"model": "direct", "default_s": 15.0},  # tool-specific cache flag
    # Redundancy family.
    "OPT11": {"model": "runner-min-only", "hit_rate": 0.0},  # redundant env vars — cosmetic
    # OPT12 (duplicated setup → composite action) is wall-clock-NEUTRAL: each
    # job still runs the shared steps in parallel, so the critical path is
    # unchanged. It dedups maintenance / a little runner-min, never wall-clock.
    # (The artifact-handoff variant of the fix is a serial gate → see OPT14.)
    # OPT12 (duplicated setup → composite action) saves ZERO runtime: every job
    # still runs checkout + install; only the YAML is de-duplicated. So it's
    # neither a wall-clock nor a runner-minute lever — it's a maintainability
    # refactor. hit_rate 0.0 → runner_min 0 with an honest note (don't book
    # fictional savings, and don't let it inflate the total).
    "OPT12": {"model": "runner-min-only", "hit_rate": 0.0,  # duplicated setup
              "note": "maintainability only — a composite action de-duplicates "
                      "the YAML but changes zero runtime (each job still runs "
                      "checkout + install); no wall-clock or runner-minute saving"},
    "OPT13": {"model": "direct", "default_s": 90.0},  # build step in jobs that don't need it
    "OPT14": {"model": "direct", "default_s": 45.0},  # repeated checkout/setup
    "OPT15": {"model": "direct", "default_s": 90.0},  # cross-workflow build redundancy
    "OPT16": {"model": "direct", "default_s": 10.0},  # within-job duplicate commands
    # Docker family.
    "OPT18": {"model": "direct", "default_s": 10.0},  # all containers started
    # OPT19 is sized inline by scan.py from the MEASURED per-run sleep total
    # (summed sleep_ms across test source). "measured" preserves it rather than
    # overwriting with a flat default.
    "OPT19": {"model": "measured"},  # test source sleep dominance
    "OPT20": {"model": "runner-min-only", "hit_rate": 0.0},  # unpinned docker tags — reliability
    # Parallelization (non fast-path).
    "OPT21": {"model": "direct", "default_s": 30.0},  # unnecessary needs:
    "OPT22": {"model": "direct", "default_s": 60.0},  # sequential workflow_run
    # Actions/Checkout (non fast-path).
    "OPT26": {"model": "runner-min-only", "hit_rate": 0.0},  # outdated action major
    "OPT27": {"model": "direct", "default_s": 5.0},   # duplicate setup-node
    # Conditional Execution.
    # OPT29 is a STEP-LEVEL skip on a SINGLE job (the job provisions a runner on
    # merge_group events but its steps all skip). The waste is confined to that
    # ONE job — never the whole workflow. Pricing it off the workflow long pole ×
    # full volume credited the WHOLE run's compute to a step-skip on a tiny gate
    # job (biome: `changes`, an 823 min/mo gate, credited 1290.7 min/mo off the
    # 941s benchmark long pole — a physically-impossible saving that #113's
    # `check_saving_within_measured_compute` caught). `cost_basis: affected_jobs`
    # routes it through the SAME DERIVE machinery OPT45 uses (`hit_rate × the
    # affected job's MEASURED billable`, re-grounded in the cost spine by
    # `_reground_whole_run_cancel_saving`) — within the physical bound by
    # construction (hit_rate ≤ 1). hit_rate 0.1 is the merge_group-run share; the
    # credited figure is a CEILING (only provisioning is actually wasted today,
    # since the steps already skip), disclosed as such.
    "OPT29": {"model": "runner-min-only", "hit_rate": 0.1, "cost_basis": "affected_jobs",
              "cost_basis_rate_label": "the merge_group-run share",
              "cost_basis_label": "the merge_group-run share of the job's compute reclaimed by "
                                  "skipping the whole job (only runner provisioning is wasted today — "
                                  "the steps already skip on merge_group; this is a ceiling)"},  # merge queue skip step level
    # OPT30/31/33/34/39 all gate (or skip) a SINGLE job, not the whole workflow,
    # so they are scope:"job" — sized by the affected job's OWN duration, never
    # the workflow long-pole (which produced identical inflated runner-min across
    # many per-job findings in one workflow — e.g. mastra prebuild's six OPT33).
    "OPT30": {"model": "runner-min-only", "hit_rate": 0.2, "scope": "job"},  # matrix without job conditional
    "OPT31": {"model": "runner-min-only", "hit_rate": 0.2, "scope": "job"},  # conditional step uncond setup
    # Trigger and Scope.
    "OPT33": {"model": "runner-min-only", "hit_rate": 0.3, "scope": "job"},  # no draft PR gate (~30% of PRs are drafts)
    "OPT34": {"model": "runner-min-only", "hit_rate": 0.4, "scope": "job"},  # no changed-package filtering
    "OPT37": {"model": "runner-min-only", "hit_rate": 0.0},  # cache race — needs log evidence
    "OPT38": {"model": "runner-min-only", "hit_rate": 0.1},  # PR-edited triggers (~10% are metadata edits)
    "OPT39": {"model": "runner-min-only", "hit_rate": 0.5, "scope": "job"},  # multi-lang matrix no path filter
    # OPT40 skips ONE job (not the whole workflow) on PRs that don't touch its
    # app, so size by the affected job's OWN duration (scope:"job"), never the
    # workflow long-pole — and a conservative skip rate. Sizing off the long
    # pole credited a tiny per-app gate with the whole run (mastra peerdeps-check
    # → an implausible ~9,300 min/mo).
    "OPT40": {"model": "runner-min-only", "hit_rate": 0.3, "scope": "job"},  # monorepo job over-runs
    # Release.
    "OPT41": {"model": "direct", "default_s": 60.0},  # TURBO_FORCE: true
    "OPT42": {"model": "direct", "default_s": 30.0},  # TURBO_CACHE: remote:rw in release
    # Concurrency (non fast-path).
    "OPT44": {"model": "runner-min-only", "hit_rate": 0.1},  # concurrency too restrictive
    # OPT46/OPT47 are MEASURED run-elimination detectors (their own gh sizing
    # from the all-status run slice); "measured" so _size_finding preserves the
    # detector's numbers instead of overwriting with a static hit_rate.
    "OPT46": {"model": "measured"},  # superseded runs — measured (was dead hit_rate 0.4)
    "OPT47": {"model": "measured"},  # push+PR double-trigger — measured
    # Stack-Specific.
    "OPT52": {"model": "direct", "default_s": 30.0},  # turbo missing outputs
    "OPT53": {"model": "direct", "default_s": 30.0},  # turbo unstable env vars
    "OPT54": {"model": "direct", "default_s": 30.0},  # full-repo pnpm -r
    "OPT55": {"model": "runner-min-only", "hit_rate": 0.0},  # vitest watch in CI — cosmetic
    "OPT56": {"model": "runner-min-only", "hit_rate": 0.5},  # playwright traces unconditional
    "OPT57": {"model": "runner-min-only", "hit_rate": 0.0},  # missing timeout-minutes — reliability
    "OPT58": {"model": "direct", "default_s": 30.0},  # turbo missing inputs
    "OPT59": {"model": "direct", "default_s": 30.0},  # turbo globalEnv runtime-only
    # CI-config/log-verbosity hygiene (ui/futureFlags/outputLogs) does NOT move
    # the critical path — runner-minute/overhead hygiene only, not wall-clock.
    "OPT60": {"model": "runner-min-only", "hit_rate": 0.0,
              "note": "log/CI-config hygiene — does not move wall-clock"},
    # Build Caching.
    "OPT61": {"model": "direct", "default_s": 30.0},  # missing dependency caching
    "OPT62": {"model": "direct", "default_s": 60.0},  # build artifacts destroyed
    "OPT63": {"model": "direct", "default_s": 30.0},  # dep install with cache disabled
    "OPT64": {"model": "measured"},  # rerun/attempt waste — measured prior-attempt jobs
    "OPT65": {"model": "measured"},  # billing rounding waste — exact sampled matrix legs
    # Hidden Failures and Dead Config.
    "OPT68": {"model": "runner-min-only", "hit_rate": 0.0},  # broken step masked — reliability
    "OPT69": {"model": "runner-min-only", "hit_rate": 0.0},  # dead env vars — cosmetic
}

# Patterns whose canonical fix inserts a SERIAL gate (build-once → fan-out,
# artifact handoff). Per wall-clock-methodology.md §4 (serial-gate /
# consolidate-then-fan-out findings can be wall-clock-NEGATIVE): the fix
# removes parallel-overlapped compute — a real runner-minute win — but ADDS
# wall-clock behind the new gate, because in the baseline the N copies run in
# parallel so wall-clock pays for one. They must never rank as a Tier-1
# wall-clock lever: force the wall-clock-negative model so wall-clock is 0 and
# the report warns to warm the cache instead.
_SERIAL_GATE_PATTERNS = {"OPT14", "OPT15"}

# Tool-/build-level cache patterns whose claimed wall-clock saving assumes a
# WARM local cache. On GitHub-hosted / ephemeral runners that warm cache often
# doesn't exist (or the tool re-validates every file despite an `actions/cache`
# hit because cache files embed absolute paths / stat metadata that differ
# across runner instances — see OPT8's catalog guardrail). So their saving is
# unconfirmed until a warm-vs-cold step-time delta is measured. We append a
# caveat to the size_note rather than hard-zeroing the wall-clock (the saving is
# real when the cache works) so the report never overstates an ephemeral case.
_EPHEMERAL_CACHE_SUSPECT = {"OPT3", "OPT8", "OPT9"}
_EPHEMERAL_CAVEAT = (
    "ephemeral-runner caveat — this saving assumes a warm local cache, which "
    "GitHub-hosted/ephemeral runners don't have; verify a warm-vs-cold step-time "
    "delta before claiming wall-clock (OPT8 guardrail)")
# The same warm-local-cache assumption also makes the RUNNER-MINUTE (bill)
# saving unproven on ephemeral runners: a tool's `--cache`/turbo-local-cache
# writes to a working-dir path the next ephemeral runner discards, so unless an
# `actions/cache` persists that dir AND a warm-vs-cold delta is measured, the
# per-run compute saving doesn't materialize. We therefore move the modeled
# runner-min out of the credited field (→ unrealizable) for these patterns,
# rather than printing a concrete monthly number we can't stand behind.
_EPHEMERAL_RUNNER_MIN_CAVEAT = (
    "runner-minute saving is ALSO unproven on ephemeral runners — the cache "
    "writes to a working-dir path the next runner discards; realizable only "
    "with a persisted actions/cache for that dir + a measured warm-vs-cold "
    "delta (none here), so this is config-hygiene, not a credited bill saving")

# Patterns whose modeled runner-min is NOT a realizable compute saving. The
# credited `runner_min_saving` is set to None (so the bill headline / Also-noticed
# appendix skip it) and the modeled amount is preserved in `runner_min_unrealizable_s` so the
# report can show it, annotated, instead of summing it into the bill total.
#   OPT21 — removing a `needs:` edge changes WHEN a job starts (queue wait), not
#           how long it runs; compute is unchanged, so the bill saving is 0.
_WALL_CLOCK_ONLY_NO_RUNNER_MIN = {"OPT21"}
#   OPT32 — a missing `paths:` filter's bill saving is the compute on irrelevant
#           runs the filter would skip; it's a GROSS UPPER BOUND that's only
#           realizable where the workflow's jobs don't ALREADY self-skip (via
#           `if:` guards / an internal change-gate) and the trigger can be
#           narrowed safely — a per-workflow judgment we don't fake.
_RUNNER_MIN_UPPER_BOUND = {"OPT32"}


def _affected_job_p50(f: dict[str, Any], crit: dict[str, Any]) -> float:
    """Max p50 across the finding's affected jobs (0.0 if none resolve)."""
    job_p50 = crit.get("job_p50") or {}
    return max((_resolve_job_p50(j, job_p50) for j in (f.get("affected_jobs") or [])),
               default=0.0)


def _affected_jobs_p50_sum(f: dict[str, Any], crit: dict[str, Any]) -> float:
    """Sum of p50 across the finding's affected jobs (0.0 if none resolve).

    Used to size a WHOLE-RUN cancel (OPT45): cancelling a superseded run reclaims
    ALL the affected jobs' compute, so the per-run cost is their SUM — not the
    single workflow long pole (a different, often larger job) and not the MAX of
    one job (that is the per-job skip case, `scope: job`). Provisional only; when
    a cost spine exists this is re-grounded in the affected jobs' MEASURED
    billable compute by `_reground_whole_run_cancel_saving`."""
    job_p50 = crit.get("job_p50") or {}
    return sum(_resolve_job_p50(j, job_p50) for j in (f.get("affected_jobs") or []))


def _resolve_job_p50_strict(job_key: str, job_p50: dict[str, float]) -> float:
    """Like _resolve_job_p50 but HIGH-CONFIDENCE only: exact key, else the
    matrix expansion (`<key> (`). It deliberately omits `_resolve_job_p50`'s
    leading-word fuzzy fallback — that fallback can bind a YAML key (`test`) to
    an unrelated fast sibling display name (`test-helpers`) while the finding's
    real job is a `name:`-overridden pole (`Unit test`), which would let the
    neutrality stamp mint a false 'below the floor' certificate on a finding
    whose real job IS the long pole. Returns 0.0 when nothing resolves with
    confidence — the caller then withholds the certificate."""
    if job_key in job_p50:
        return job_p50[job_key]
    prefix = job_key + " ("
    cands = [v for name, v in job_p50.items() if name.startswith(prefix)]
    return max(cands) if cands else 0.0


def _affected_job_p50_strict(f: dict[str, Any], crit: dict[str, Any]) -> float:
    """Max p50 across affected jobs using ONLY the strict (exact/matrix)
    resolver — the confidence bar the neutrality certificate requires. Never
    used for sizing (that stays on the fuzzy _affected_job_p50, unchanged), so
    the byte-identical render invariant holds."""
    job_p50 = crit.get("job_p50") or {}
    return max((_resolve_job_p50_strict(j, job_p50) for j in (f.get("affected_jobs") or [])),
               default=0.0)


def _demote_runner_min(f: dict[str, Any], note: str) -> None:
    """Move a finding's modeled runner-min OUT of the credited `runner_min_saving`
    field (→ None) into `runner_min_unrealizable_s`, and flag it. The bill
    headline and Also-noticed appendix read `runner_min_saving`, so a None drops
    out of both automatically — but the modeled amount is preserved so the report can SHOW it
    annotated (`runner_min_note`) instead of silently summing an unrealizable
    number into the total. Idempotent."""
    rm = f.get("runner_min_saving")
    if isinstance(rm, (int, float)) and rm > 0:
        f["runner_min_unrealizable_s"] = rm
    f["runner_min_saving"] = None
    f["runner_min_unrealizable"] = True
    existing = f.get("runner_min_note") or ""
    f["runner_min_note"] = f"{existing}; {note}" if existing else note


def _size_finding(f: dict[str, Any], crit: dict[str, Any],
                  monthly_volume: int | None) -> None:
    """Set wall_clock_p50_s and runner_min_saving on the finding in-place."""
    if f.get("sizing_basis") == "measured" and (
            "wall_clock_p50_s" in f or "runner_min_saving" in f):
        # Detector-issued measured rows already carry their axes. Some catalog
        # IDs (OPT35/OPT36) still have a static fallback model for residual
        # appendix rows, so the global sizing pass must not re-model a measured
        # upgrade back through that static table.
        wc = f.get("wall_clock_p50_s")
        f.setdefault("tier", 1 if isinstance(wc, (int, float)) and wc > 0 else 2)
        f.setdefault("realization", "direct" if isinstance(wc, (int, float)) and wc > 0 else "none")
        f.setdefault("size_note", "")
        return
    pat = f.get("pattern", "")
    cfg = _SIZING.get(pat)
    if cfg is None:
        # Pattern not in the sizing table → render qualitatively. The report
        # already handles None as "—" and falls back to evidence prose.
        f["wall_clock_p50_s"] = None
        f["runner_min_saving"] = None
        f["tier"] = 2
        f["realization"] = "none"
        f["size_note"] = "no sizing model wired for this pattern yet"
        return

    long_pole = crit.get("long_pole_p50", 0.0)
    floor = crit.get("floor_p50", 0.0)
    model = cfg["model"]
    # Guardrail A — D12 serial-gate auto-demote.
    if pat in _SERIAL_GATE_PATTERNS:
        model = "wall-clock-negative"

    if model == "direct":
        s = cfg.get("default_s", 0.0)
        rm = round(s * (monthly_volume or 0) / 60.0, 1) if monthly_volume else None
        # Guardrail B — below-cluster-floor demote. Wall-clock is the time on
        # the critical path. If this finding's affected job finishes at/below
        # the cluster floor (i.e. it is NOT the long pole), fixing it saves
        # runner-minutes but ZERO wall-clock — the long pole still gates the
        # run. Only credit wall-clock when the finding touches the long pole.
        own = _affected_job_p50(f, crit)
        affected = bool(f.get("affected_jobs"))
        if not long_pole:
            # No run timing sampled at all (a static-only report — no measured
            # critical path, empty spine). We cannot prove this job is the long
            # pole, so we must NOT credit wall-clock: a positive wall_clock_p50_s
            # is the report's signal that a finding survived the cross-workflow
            # cascade and sits ON the merge-gating critical path
            # (blocking_path._saves_wall_clock). Crediting the nominal estimate
            # here made the static-only renderer flag bill-only hygiene as a
            # wall-clock lever "ON the merge-gating critical path / See the spine
            # above" in a report that deliberately renders NO spine. Bill saving
            # (where the pattern carries one) still stands on `rm` below.
            wc = 0.0
            note = ("no run timing sampled — can't measure the critical path; "
                    "runner-minute (bill) only, wall-clock unproven")
        elif own <= 0 and affected:
            # The finding names affected job(s) but NONE resolve to a sampled
            # duration (a reusable-workflow caller, a name-overridden job). We can't
            # confirm this job is the long pole, so don't credit wall-clock off the
            # GLOBAL pole — that would overstate an unresolvable job exactly the way
            # the runner-min-only branch already guards against. Bill saving stands;
            # wall-clock is unproven.
            wc = 0.0
            note = ("affected job's duration couldn't be resolved from sampled runs — "
                    "runner-minute (bill) only, wall-clock unproven")
        elif own > 0 and own <= floor:
            wc = 0.0
            note = ("affected job finishes below the cluster floor — "
                    "runner-minute (bill) only, no wall-clock")
        else:
            # Cap at the headroom above the floor — cutting the long pole only
            # saves wall-clock until it reaches the floor.
            wc = min(s, max(long_pole - floor, 0.0))
            note = ""
        f["wall_clock_p50_s"] = round(wc, 1)
        f["runner_min_saving"] = rm
        f["tier"] = 1 if wc > 0 else 2
        f["realization"] = "direct" if wc > 0 else "none"
        f["size_note"] = note

    elif model == "parallel-rebalance":
        # Sharding spreads a job's work across N shards (N=2 conservative floor).
        # Ground the saving in the FLAGGED job, not the global long pole — mirror
        # the 'direct' model's Guardrail B: a flagged job at/below the cluster floor
        # is NOT the pole, so rebalancing it saves zero wall-clock; and a flagged job
        # shorter than the global pole must be halved off ITS OWN p50, not the pole's
        # (else a 200s job next to a 400s pole would be credited a false ~200s).
        own = _affected_job_p50(f, crit)
        affected = bool(f.get("affected_jobs"))
        if long_pole and own <= 0 and affected:
            # Named affected job(s) but none resolve to a sampled duration — same
            # unresolvable case the direct model guards: don't credit wall-clock off
            # the global pole (it would overstate an unsized job).
            wc = 0.0
            f["size_note"] = ("affected job's duration couldn't be resolved from "
                              "sampled runs — wall-clock unproven")
        elif long_pole and own > 0 and own <= floor:
            wc = 0.0
            f["size_note"] = ("affected job finishes at/below the cluster floor — not "
                              "the long pole; rebalancing it saves no wall-clock")
        else:
            base_p50 = own if own > 0 else long_pole  # no timing at all → nominal pole
            s = base_p50 / 2.0
            wc = min(s, max(long_pole - floor, 0.0))
            f["size_note"] = "parallelizes / rebalances the long pole"
        f["wall_clock_p50_s"] = round(wc, 1)
        f["runner_min_saving"] = 0.0
        f["tier"] = 1 if wc > 0 else 2
        f["realization"] = "direct" if wc > 0 else "none"

    elif model == "runner-min-only":
        # No single-run wall-clock impact; bill saving = monthly_volume × hit
        # rate × per-run cost. Default per-run cost is the workflow long-pole
        # (the whole run is skipped/cancelled). A job-scoped finding
        # (scope:"job", e.g. OPT33/OPT40) only removes ONE job, so it must be
        # sized by that job's OWN duration — long-pole wildly overstates a small
        # gate job and made N per-job findings in one workflow show identical
        # inflated runner-min. If the job's duration can't be resolved from
        # sampled timings (a reusable-workflow caller, an unmappable name), we
        # do NOT substitute the long pole — we render qualitatively (omit rather
        # than fake), since we genuinely can't size the job's own cost.
        rate = cfg.get("hit_rate", 0.0)
        cost_basis = cfg.get("cost_basis")
        if cfg.get("scope") == "job":
            per_run_s = _affected_job_p50(f, crit)
            # A job-scoped finding whose job we couldn't time is UNRESOLVED
            # regardless of whether the workflow long pole was sampled. (Real
            # jobs never have a 0 p50, so per_run_s <= 0 only ever means
            # "couldn't resolve this job".) Don't gate on long_pole — doing so
            # routed the no-timings case to a confident 0.0 instead of the honest
            # qualitative None.
            unresolved = per_run_s <= 0 and bool(monthly_volume)
        elif cost_basis == "affected_jobs":
            # Whole-run cancel (OPT45): cancelling a superseded run reclaims the
            # AFFECTED jobs' compute, so size the per-run cost off the SUM of their
            # own p50 — never the workflow long pole. Pricing ~20% of the (often
            # larger, differently-gated) long pole at the FULL workflow volume
            # overstated the bill vs. what the affected jobs measurably consume
            # (#33). This is the PROVISIONAL (no-spine) figure;
            # `_reground_whole_run_cancel_saving` re-derives it from the affected
            # jobs' MEASURED monthly billable compute whenever a cost spine is
            # built — which also folds in each job's occurrence fraction (a
            # conditional build job that runs on only a few of the workflow's runs
            # consumes far less than its p50 × full volume).
            per_run_s = _affected_jobs_p50_sum(f, crit)
            unresolved = (per_run_s <= 0 and bool(f.get("affected_jobs"))
                          and bool(monthly_volume))
        else:
            per_run_s = long_pole or 0.0
            unresolved = False
        f["wall_clock_p50_s"] = 0.0
        f["tier"] = 2
        f["realization"] = "none"
        if unresolved and cost_basis == "affected_jobs":
            f["runner_min_saving"] = None
            f["size_note"] = ("whole-run cancel runner-minutes not sized — none of "
                              "the affected jobs' durations could be resolved from "
                              "sampled run timings; saving ≈ the affected jobs' own "
                              "minutes × superseded-run rate × volume")
        elif unresolved:
            f["runner_min_saving"] = None
            f["size_note"] = ("per-job runner-minutes not sized — this finding "
                              "skips a single job, but that job's duration "
                              "couldn't be resolved from sampled run timings "
                              "(reusable-workflow caller or name-overridden job); "
                              "saving ≈ the job's own minutes × skip-rate × volume")
        else:
            f["runner_min_saving"] = (
                round(rate * per_run_s * (monthly_volume or 0) / 60.0, 1)
                if monthly_volume else None)
            if cost_basis == "affected_jobs":
                rate_label = cfg.get("cost_basis_rate_label", "the superseded-run rate")
                f["size_note"] = (
                    "runner-minute (bill) only — at most the affected jobs' own "
                    f"compute × {rate_label}; re-grounded in the cost "
                    "spine's MEASURED billable compute when a spine is built")
            else:
                # Most runner-min-only patterns are below the cluster floor; some
                # (e.g. OPT12, a composite-action dedup) are wall-clock-NEUTRAL by
                # parallelism instead. Let the pattern override the note.
                f["size_note"] = cfg.get(
                    "note", "below the cluster floor — runner-minute (bill) only, no wall-clock")

    elif model == "wall-clock-negative":
        # The serial-gate fix removes duplicated build/install compute (a real
        # runner-minute win) but adds wall-clock behind the gate — so credit
        # the bill saving, zero the wall-clock, and warn.
        s = cfg.get("default_s", 0.0)
        rm = round(s * (monthly_volume or 0) / 60.0, 1) if (monthly_volume and s) else None
        f["wall_clock_p50_s"] = 0.0
        f["runner_min_saving"] = rm
        f["tier"] = 2
        f["realization"] = "none"
        # Explicit flag so the Also-noticed appendix can disclose it as
        # wall-clock-negative (it cuts the bill but ADDS developer wait).
        f["wall_clock_negative"] = True
        f["size_note"] = ("serial-gate fix is wall-clock-NEGATIVE (build-once + "
                          "fan-out adds a serial gate) — prefer warming the cache "
                          "so each parallel copy stays cheap; runner-minute "
                          "(bill) saving only")

    elif model == "measured":
        # Data-driven detector already set the axes from measured timings;
        # don't overwrite. Just ensure tier/realization are present.
        f.setdefault("wall_clock_p50_s", None)
        f.setdefault("runner_min_saving", None)
        f.setdefault("tier", 2)
        f.setdefault("realization", "tail")
        f.setdefault("size_note", "")

    # Ephemeral-runner cache caveat — appended after the model set its size_note,
    # so the report carries the warm-cache warning for tool-/build-cache patterns
    # whose wall-clock saving is unproven on GitHub-hosted runners. The same
    # assumption makes the runner-minute (bill) saving unproven too, so demote it
    # out of the credited field rather than printing a concrete monthly number.
    if pat in _EPHEMERAL_CACHE_SUSPECT:
        existing = f.get("size_note") or ""
        f["size_note"] = f"{existing}; {_EPHEMERAL_CAVEAT}" if existing else _EPHEMERAL_CAVEAT
        _demote_runner_min(f, _EPHEMERAL_RUNNER_MIN_CAVEAT)
    # Removing a `needs:` edge is a queue-wait (wall-clock) lever, never a
    # compute saving — its modeled runner-min is not realizable.
    if pat in _WALL_CLOCK_ONLY_NO_RUNNER_MIN:
        _demote_runner_min(f, (
            "removing a `needs:` edge changes WHEN a job starts (queue wait), "
            "not how long it runs — compute is unchanged, so there is no "
            "runner-minute saving (the wall-clock/queue effect is sized separately)"))
    # A missing `paths:` filter's bill saving is a gross upper bound, realizable
    # only where the jobs don't already self-skip — don't credit it confidently.
    if pat in _RUNNER_MIN_UPPER_BOUND:
        _demote_runner_min(f, (
            "gross upper bound — assumes every irrelevant run runs the full job "
            "and a `paths:` filter skips it; realizable only where the workflow's "
            "jobs don't ALREADY self-skip (via `if:` guards or an internal "
            "change-gate) and the trigger can be narrowed safely — verify per "
            "workflow before crediting"))


def _cap_opt19_wall_clock(f: dict[str, Any], global_long_pole_p50: float) -> None:
    """Cap OPT19's wall-clock at the repo's longest measured job, in place.

    OPT19's `wall_clock_p50_s` is the STATIC summed source-sleep total (scan.py greps
    every test file and adds up the hardcoded sleeps). That sum is uncapped — it can
    exceed any real job's duration (those sleeps are spread across files/jobs that may
    run in parallel or not all in one run), so claiming it as wall-clock saving can be
    physically impossible: you can't save more wall-clock than the slowest run takes.
    OPT19's `workflow_file` is a test SOURCE file (no per-workflow critical path), so
    the bound is the GLOBAL long pole — the longest measured job across all workflows.
    Records the uncapped value + a note; no-op when there's no measured pole (the
    static estimate stands honestly) or the total already fits under the bound."""
    if f.get("pattern") != "OPT19":
        return
    wc = f.get("wall_clock_p50_s")
    if not wc or not global_long_pole_p50 or wc <= global_long_pole_p50:
        return
    f["wall_clock_uncapped_p50_s"] = wc
    f["wall_clock_p50_s"] = round(global_long_pole_p50, 1)
    prev = f.get("size_note") or ""
    note = (f"sleep total capped to the repo's longest measured job "
            f"({global_long_pole_p50:.0f}s) — can't save more wall-clock than the "
            f"slowest run takes")
    f["size_note"] = f"{prev}; {note}" if prev else note


# =============================================================================
# Tier-2 (runner-minute) stamps — ADDITIVE data on each finding, consumed by
# verify_report's tier2 checks and (in a later PR) the renderer. PR-1 is
# data-only: nothing here is rendered, so the committed worked examples stay
# byte-identical. Every stamp is deterministic and independently re-derivable
# from findings.json, so no rendered prose is ever trusted for a Tier-2 claim.
# =============================================================================


def _derive_repo_visibility(repo_info: dict[str, Any] | None) -> str | None:
    """"public" / "private" from the repos API payload. Prefer the explicit
    `visibility` field; fall back to the `private` bool; None when neither is
    present (an unreadable repo / API failure). Recorded on the cost spine as
    informational metadata about the audited repo."""
    info = repo_info or {}
    vis = info.get("visibility")
    if vis:
        return vis
    priv = info.get("private")
    if priv is not None:
        return "private" if priv else "public"
    return None


def _sizing_basis(pat: str) -> str | None:
    """The sizing basis for a pattern: 'measured' iff the sizing comes entirely
    from sampled run history (the data-driven detectors, model == "measured");
    'modeled' when any _SIZING heuristic constant (a hit_rate OR a default_s) is
    in the chain; None for a pattern with no sizing model (stays qualitative,
    never promotable). The measured-basis admission gate (plan §5.2) keys off
    this.

    NB: this stamps ONLY the basis — it deliberately does NOT write a
    `measured_signal`. That field pre-exists on the data-driven findings, set by
    their detectors to the REAL evidence string (e.g. "job X p50 346s over 20
    runs"); the measured-basis gate reads that existing signal. Writing a
    generic constant here would clobber real evidence (and break the
    additive-only invariant)."""
    cfg = _SIZING.get(pat)
    if cfg is None:
        return None
    return "measured" if cfg.get("model") == "measured" else "modeled"


def _resolve_job_runner(job_key: str, job_runner: dict[str, str],
                        job_p50: dict[str, float]) -> str:
    """Resolve a YAML job key to the runner label of the leg its COST is sized
    from — exact match, else the matrix expansion (`<key> (`) taking the
    SLOWEST leg (max p50, name tie-break). This must agree with
    `_resolve_job_p50` (which sizes off the max-p50 leg): pricing the slowest
    leg's minutes at a different (e.g. alphabetically-first macOS) leg's rate
    was a up-to-10x misprice. Returns "" when nothing resolves — UNPRICED,
    never another job's runner."""
    if job_key in job_runner:
        return job_runner[job_key]
    prefix = job_key + " ("
    legs = [name for name in job_runner if name.startswith(prefix)]
    if not legs:
        return ""
    # Same leg _resolve_job_p50 sizes with: max p50, deterministic name tie-break.
    best = min(legs, key=lambda n: (-(job_p50.get(n) or 0.0), n))
    return job_runner[best]


def _stamp_sizing_basis(f: dict[str, Any]) -> None:
    """Stamp `sizing_basis` in place (never touches the pre-existing
    `measured_signal` — see _sizing_basis)."""
    if f.get("sizing_basis") in {"measured", "modeled"}:
        return
    basis = _sizing_basis(f.get("pattern", ""))
    if basis is not None:
        f["sizing_basis"] = basis


def _stamp_tier2_neutrality(f: dict[str, Any], crit: dict[str, Any]) -> None:
    """Attach a typed, DERIVED wall-clock-neutrality certificate to a finding
    whose wall-clock saving is zero but which carries a real runner-minute
    saving. The margin is COMPUTED here and re-derived independently by
    verify_report — never asserted from a canned note (that would re-open the
    prose-asserts-underived-facts bug class the plan §2/§5.1 warns about).

    PR-1 mints ONLY the `below_cluster_floor` proof, and DELIBERATELY
    CONSERVATIVELY — a false 'this cannot slow a merge' certificate is far worse
    than a missing one, so every doubtful case is withheld:

      - The finding must be a job-local bill lever: sizing model
        `runner-min-only` (a whole-job skip/cancel/gate). `direct`-model
        findings that merely fell below the floor are cache/setup/checkout fixes
        whose effect BLEEDS onto other jobs (incl. the pole) — never certified.
        `wall_clock_negative` (serial-gate) findings are excluded outright.
      - The affected job must resolve with HIGH CONFIDENCE (exact/matrix, via
        `_affected_job_p50_strict`) — never the leading-word fuzzy fallback,
        which can bind a YAML key to a fast decoy sibling while the real job is
        a renamed pole, minting a false certificate.
      - `margin_s = floor_p50 − affected_job_p50` must be STRICTLY positive
        after rounding (own strictly below floor; a 0.0 margin is withheld).

    Deferred to a later PR: the `off_spine` proof (a not-required check can
    still gate an all-checks-green merge, so 'off the required spine' is not by
    itself proof of wall-clock-neutrality), and the whole-run/workflow-scoped
    certificates the measured detectors will produce.

    Findings with any positive OR unknown (None) wall_clock get no certificate:
    only an explicit measured 0/0.0 is neutral-eligible (unknown ≠ zero)."""
    if isinstance(f.get("tier2_neutrality"), dict):
        return                      # detector-issued certificate; do not downgrade
    wc = f.get("wall_clock_p50_s")
    if wc not in (0, 0.0):
        return                      # positive lever, or UNKNOWN (None) — not certifiable
    if f.get("wall_clock_negative"):
        return                      # serial-gate fix ADDS merge wait — never neutral
    rm = f.get("runner_min_saving")
    if not isinstance(rm, (int, float)) or rm <= 0:
        return                      # no credited bill saving to certify
    # Only genuinely job-local bill levers (whole-job skip/cancel/gate). A
    # direct/cache/setup fix below the floor can lower the pole too → not neutral.
    if (_SIZING.get(f.get("pattern", "")) or {}).get("model") != "runner-min-only":
        return
    floor = crit.get("floor_p50") or 0.0
    own = _affected_job_p50_strict(f, crit)
    if not (floor > 0 and own > 0 and own < floor):
        return
    margin = round(floor - own, 1)
    if margin <= 0:                 # float-precision guard: margin must be strictly positive
        return
    f["tier2_neutrality"] = {
        "proof": "below_cluster_floor",
        "margin_s": margin,
        "ref": "per_workflow_timing[wf]: floor_p50 - affected_job_p50 (exact/matrix match)",
    }


def _tier2_sample_run_ids(f: dict[str, Any]) -> list[str]:
    # Only whole-run detectors can use sampled workflow-run IDs as an overlap
    # basis. OPT35 emits per-matrix-job post-failure minutes; two matrices in
    # the same failed workflow run are disjoint job-seconds and must remain
    # additive even though they share the run ID evidence. OPT64 is also
    # additive with whole-run eliminations: it credits prior-attempt job seconds,
    # not the latest-attempt run seconds OPT36/OPT46 price, so a bare workflow
    # run ID would be too coarse an overlap key. OPT65 credits per-job billing
    # round-up deltas, not whole-run eliminations, so run IDs are too coarse
    # there as well. OPT57 credits failed/timed-out job seconds inside concrete
    # workflow runs, so it uses job-scoped samples from
    # `_tier2_opt57_overlap_samples` instead of bare run IDs.
    pat = str(f.get("pattern") or "")
    if pat in {"OPT36", "OPT46"}:
        raw = f.get("tier2_sample_run_ids")
    else:
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _finite_float(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        out = float(v)
    else:
        try:
            out = float(str(v))
        except (TypeError, ValueError):
            return None
    return out if math.isfinite(out) else None


def _tier2_opt57_overlap_samples(f: dict[str, Any]) -> list[tuple[str, str, float]]:
    """Return `(run_id, job_scoped_key, scaled_runner_min)` for OPT57 samples."""
    if str(f.get("pattern") or "") != "OPT57":
        return []
    burn = f.get("timeout_default_burn")
    if not isinstance(burn, dict):
        return []
    scale = _finite_float(burn.get("scale"))
    if scale is None or scale <= 0:
        return []
    job_key = str(burn.get("job_key") or "").strip()
    if not job_key:
        return []
    out: list[tuple[str, str, float]] = []
    samples = burn.get("samples")
    if not isinstance(samples, list):
        return []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        rid = str(sample.get("run_id") or "").strip()
        waste_s = _finite_float(sample.get("waste_s"))
        if not rid or waste_s is None or waste_s <= 0:
            continue
        out.append((rid, f"OPT57:{rid}:{job_key}", (waste_s / 60.0) * scale))
    return out


def _tier2_runner_min_value(f: dict[str, Any]) -> float:
    v = f.get("runner_min_saving")
    if isinstance(v, (int, float)):
        return float(v)
    return 0.0


def _reconcile_tier2_overlap(findings: list[dict[str, Any]]) -> None:
    """De-overlap credited runner-minutes across promotion-eligible Tier-2
    findings so no step-second is counted twice (plan §5.4).

    SCOPED TO PROMOTION-ELIGIBLE FINDINGS ONLY — eligible iff a finding is both
    `sizing_basis == "measured"` AND carries a `tier2_neutrality` certificate.

    Measured whole-run findings can stamp `tier2_sample_run_ids`, the concrete
    sampled workflow-run IDs behind their credited run count. When two promoted
    findings name the same sampled run ID, this pass gives precedence to the
    larger credited finding and moves the later row's duplicate credit to
    `runner_min_overlap_s`. That preserves the section's additive total while
    keeping the pre-overlap amount visible as `runner_min_saving +
    runner_min_overlap_s`.

    Findings without a whole-run sampled-ID overlap basis remain compatible and
    unchanged; they are still expected to be disjoint by construction until
    their detector stamps a concrete overlap basis. OPT35 keeps run IDs only as
    evidence because it credits per-job matrix seconds, not whole-run seconds.
    OPT47 remains outside `eligible` while its certificate is deferred, so the
    historical OPT46/OPT47 overlap stays latent rather than user-visible."""
    eligible = [f for f in findings
                if f.get("sizing_basis") == "measured" and f.get("tier2_neutrality")]
    if len(eligible) < 2:
        return                      # nothing can overlap yet
    ordered = sorted(
        eligible,
        key=lambda f: (-_tier2_runner_min_value(f),
                       str(f.get("pattern") or ""),
                       str(f.get("workflow_file") or "")),
    )
    used_run_ids: set[str] = set()
    used_job_keys: set[str] = set()
    used_job_minutes_by_run: dict[str, float] = {}
    for f in ordered:
        ids = _tier2_sample_run_ids(f)
        opt57_samples = _tier2_opt57_overlap_samples(f)
        rm = _tier2_runner_min_value(f)
        if (not ids and not opt57_samples) or rm <= 0:
            continue
        displaced_raw = 0.0
        overlap_count = 0
        if ids:
            per_sample = rm / len(ids)
            for rid in ids:
                job_overlap = used_job_minutes_by_run.get(rid, 0.0)
                if rid in used_run_ids:
                    displaced_raw += per_sample
                    overlap_count += 1
                elif job_overlap > 0:
                    displaced_raw += min(per_sample, job_overlap)
                    overlap_count += 1
        else:
            for rid, job_key, scaled_min in opt57_samples:
                if rid in used_run_ids or job_key in used_job_keys:
                    displaced_raw += scaled_min
                    overlap_count += 1
        if displaced_raw > 0:
            displaced = round(min(rm, displaced_raw), 1)
            kept = round(max(0.0, rm - displaced), 1)
            f["runner_min_saving"] = kept
            f["runner_min_overlap_s"] = round(
                float(f.get("runner_min_overlap_s") or 0.0) + displaced, 1)
            unit = "sampled run id(s)" if ids else "timeout sample(s)"
            f["tier2_overlap_note"] = (
                f"{overlap_count} {unit} already credited by a higher-ranked "
                "Tier-2 finding; displaced before section totals.")
        used_run_ids.update(ids)
        for rid, job_key, scaled_min in opt57_samples:
            used_job_keys.add(job_key)
            used_job_minutes_by_run[rid] = used_job_minutes_by_run.get(rid, 0.0) + scaled_min


def _whole_run_cancel_base_key(name: str) -> str:
    """Join key for matching an affected-job name to a cost-spine row.

    At least as STRICT as verify_report's `_base`/`_cmp_name` join (matrix
    `(variant)` stripped, render artifacts dropped, case-folded) but WITHOUT
    scope-stripping — so the engine's re-derivation can only ever match a SUBSET
    of the rows the verifier bounds against. That keeps the re-grounded saving
    within the measured-compute bound by construction (it can never join a row
    the guard wouldn't, so it can never out-derive the guard)."""
    base = re.sub(r"\s*\([^()]*\)\s*$", "", str(name or "")).strip()
    return re.sub(r"[`*]", "", base).strip().lower()


# Sentinel: distinguishes "index argument not supplied, build it" from "index
# supplied as None (no render-ready spine)".
_UNSET: Any = object()


def _spine_binds(spine: dict[str, Any] | None) -> bool:
    """True when `spine` is the render-ready, non-empty shape under which the door
    MUST produce a measured basis — the EXACT gate `check_saving_carries_measured_basis`
    /`check_saving_within_measured_compute` use to decide NOT to skip. Keeping the
    engine and the verifier on one predicate is what stops them from disagreeing:
    when this is True, a positive-saving finding that fails the join must UNSIZE at
    the source (not keep an unbounded modeled figure stamped `unmeasured_no_spine`),
    because the verifier will not skip it."""
    return (isinstance(spine, dict) and spine.get("render_ready") is True
            and isinstance(spine.get("rows"), list) and len(spine.get("rows")) > 0)


def _reground_whole_run_cancel_saving(
        findings: list[dict[str, Any]], spine: dict[str, Any] | None,
        index: "tuple[dict[tuple[str, str], float], dict[str, list[float]]] | None"
        = _UNSET) -> None:
    """Re-derive every `cost_basis: affected_jobs` DERIVE finding (OPT45 whole-run
    cancel, OPT29 merge-queue step-level skip) from the cost spine's MEASURED
    billable compute so a credited runner-minute saving never exceeds what the
    affected jobs measurably consume (#33/#113; the `check_saving_within_measured_compute`
    invariant, PR #30).

    A missing-concurrency fix cancels superseded runs; over a month it reclaims
    at most ~hit_rate of the affected jobs' compute. The affected jobs' MEASURED
    monthly billable compute (`billable_equiv_min_per_month`, a minutes figure
    that already folds in each job's occurrence fraction and billing round-up) is
    the physical ceiling of that saving. So the credited figure becomes `hit_rate ×
    Σ(affected-job measured billable from the spine)` — a derivation expressed in
    measured terms, NOT a cosmetic clamp to the bound — and, since hit_rate ≤ 1,
    it is within the verifier's bound by construction.

    A finding whose affected jobs ALL miss the join is UNSIZED at the source
    (`runner_min_saving = None` + a qualitative note), never left carrying its
    provisional (full-volume) figure: the verifier's loud coverage-gap SKIP only
    fires when EVERY runner-minute finding misses the spine (`checked == 0`), so
    a mixed report — where some other finding IS bounded — would otherwise render
    an unmatched OPT45's unbounded provisional as green. Omit rather than fake
    (mirrors the sizing-time unresolved path).

    `index` is the shared `_measured_billable_index(spine)`; the door passes its
    already-built index so the spine is scanned ONCE per report. A standalone
    caller (unit test) omits it and this builds it from `spine`."""
    if index is _UNSET:
        index = _measured_billable_index(spine)
    if index is None:
        return
    by_wf_job, by_job = index

    for f in findings:
        cfg = _SIZING.get(str(f.get("pattern") or "")) or {}
        if cfg.get("model") != "runner-min-only" or cfg.get("cost_basis") != "affected_jobs":
            continue
        saving = f.get("runner_min_saving")
        if not isinstance(saving, (int, float)) or isinstance(saving, bool) or saving <= 0:
            continue
        jobs = [str(j) for j in (f.get("affected_jobs") or []) if str(j).strip()]
        if not jobs:
            continue
        wf = str(f.get("workflow_file") or "")
        measured, matched, distinct = _measured_billable_for_jobs(wf, jobs, by_wf_job, by_job)
        if matched == 0 or measured <= 0:
            # A render-ready spine exists but NONE of this finding's affected jobs
            # resolved to a spine row WITH measured billable compute (either no join
            # at all, or the only matched rows carry null/0 billable) → no measured
            # basis to bound the provisional (full-volume) figure. The verifier's
            # coverage-gap SKIP only fires when EVERY runner-minute finding misses
            # (checked==0), so a mixed report would render this unbounded provisional
            # as green, and a matched-but-zero row would silently ZERO the saving
            # instead. Omit rather than fake — unsize at the source (mirrors the
            # sizing-time unresolved path).
            f["runner_min_saving"] = None
            f["runner_min_basis"] = "unmeasured_no_spine_match"
            f["size_note"] = ("runner-minutes not sized — no affected job resolved "
                              "to a cost-spine row with measured billable compute, "
                              "so there is no measured basis to bound the saving")
            continue
        rate = cfg.get("hit_rate", 0.0)
        f["runner_min_saving"] = round(rate * measured, 1)
        f["runner_min_basis"] = "measured_spine_billable"
        coverage = "" if matched == distinct else f" ({matched}/{distinct} affected jobs measured)"
        label = cfg.get("cost_basis_label", "the share reclaimed by cancelling superseded runs")
        f["size_note"] = (
            f"runner-minute (bill) only — at most ~{rate:.0%} of the affected jobs' "
            f"MEASURED monthly billable compute ({round(measured, 1):g} min/mo from the "
            f"cost spine){coverage}, {label}")


def _measured_billable_index(
        spine: dict[str, Any] | None
) -> "tuple[dict[tuple[str, str], float], dict[str, list[float]]] | None":
    """Build the SHARED measured-billable join index from a render-ready cost
    spine, once, for the whole sizing door. Returns `(by_wf_job, by_job)` — the
    two-tier index every door-bound finding joins against — or None when there is
    no render-ready spine to bound against.

    `by_wf_job[(workflow_file, base_job)]` sums the measured monthly billable
    compute across a job's matrix legs / event scopes / attempts;
    `by_job[base_job]` keeps a workflow-agnostic list for the reusable-workflow
    fallback (a caller loses the callee's workflow_file). Both use
    `_whole_run_cancel_base_key` (at least as strict as the verifier's `_base`
    join), so a door-derived saving can only ever match a SUBSET of the rows the
    verifier bounds against — it can never out-derive the guard."""
    if not isinstance(spine, dict) or spine.get("render_ready") is not True:
        return None
    rows = spine.get("rows")
    if not isinstance(rows, list):
        return None

    def _bill(row: dict[str, Any]) -> float:
        v = row.get("billable_equiv_min_per_month")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

    by_wf_job: dict[tuple[str, str], float] = {}
    by_job: dict[str, list[float]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        wf = str(r.get("workflow_file") or "")
        jb = _whole_run_cancel_base_key(r.get("job_name") or "")
        if not (wf and jb):
            continue
        bill = _bill(r)
        by_wf_job[(wf, jb)] = by_wf_job.get((wf, jb), 0.0) + bill
        by_job.setdefault(jb, []).append(bill)
    if not by_wf_job:
        return None
    return by_wf_job, by_job


def _measured_billable_for_jobs(
        wf: str, jobs: "list[str]",
        by_wf_job: dict[tuple[str, str], float],
        by_job: dict[str, list[float]]) -> tuple[float, int, int]:
    """Sum the measured monthly billable compute of a finding's affected `jobs`
    against the shared index. Returns
    `(measured_min_per_month, matched_jobs, distinct_jobs)` — matched < distinct
    means partial coverage (the sum is an understatement, the safe direction).
    Prefers the exact (workflow_file, base) row; falls back to the workflow-agnostic
    `by_job` max for a reusable-workflow caller that lost the callee's file.

    EXACT JOB IDENTITY (issue #52). The affected jobs are first reduced to their
    DISTINCT (workflow_file, base-job) identities — a base-job being the full job
    name modulo its matrix-leg parenthetical (`_whole_run_cancel_base_key`). The
    index already SUMS a base's matrix legs into one figure, so listing several legs
    of ONE job (`build-image / build-image (linux/amd64)` and `(linux/arm64)`, whose
    base is `build-image / build-image`) must add that summed figure ONCE, not once
    per leg. Iterating the raw legs double-counted it (mastodon: the two build-image
    legs' 16,642.4 added twice → 33,284.8, wide enough that OPT73's 18,165.8 credit
    escaped the clamp). A DIFFERENT base (`build-image-streaming / build-image`) is a
    different job and stays its own identity — it never folds into `build-image`.
    The guard (`verify_report.check_saving_within_measured_compute`) dedupes by the
    same principle through its own base key (`_base`/`_cmp_name`); this door key is at
    least as strict as the guard's (no scope-stripping), so the door still matches at
    most the rows the guard bounds against."""
    measured = 0.0
    matched = 0
    seen: set[str] = set()
    for j in jobs:
        base = _whole_run_cancel_base_key(j)
        if not base or base in seen:
            continue
        seen.add(base)
        key = (wf, base)
        if key in by_wf_job:
            matched += 1
            measured += by_wf_job[key]
        else:
            alt = by_job.get(base)
            if alt:
                matched += 1
                measured += max(alt)
    return measured, matched, len(seen)


# =============================================================================
# The measured sizing DOOR (issues #43 / #44 / #45)
#
# Every finding that credits a `runner_min_saving` passes through ONE post-spine
# pass. A finding's saving either DERIVES from, or is CLAMPED to, the MEASURED
# cost-spine billable of its affected jobs (via the shared join above) — or the
# pattern is on an EXPLICIT, reasoned NOT-SPINE-DERIVABLE whitelist. Every sized
# finding stamps `runner_min_basis`, so:
#   - a new pattern CANNOT ship its own unmeasured sizing path (an unclassified
#     rm-crediting pattern stamps `UNCLASSIFIED_door_policy` → verify FAILs);
#   - a saving that overstates measured compute is caught at the SOURCE, not just
#     by the verifier (#43: OPT73 nx 1919.7 credited vs 1404.4 measured → clamped).
# Per-pattern SEMANTICS still differ (a cancel-rate model multiplies differently
# than a step-decomposition credit); only the MEASURED BASIS and the JOIN are
# shared, in one place.
# =============================================================================
_RM_DOOR_DERIVE = "derive"                  # saving = rate x measured (OPT45 cancel model)
_RM_DOOR_CLAMP = "clamp"                    # saving = min(modeled, measured) (OPT73 cluster model)
_RM_DOOR_NOT_DERIVABLE = "not_spine_derivable"  # visible whitelist — retained, stamped, reasoned
_RM_DOOR_UNCLASSIFIED = "unclassified"      # loud sentinel — a pattern with no declared policy

# Per-pattern door policy OVERRIDES — for patterns whose policy isn't implied by a
# _SIZING model, or that carry NO _SIZING row because they are data-driven inline
# detectors (OPT70-OPT75). This table is the VISIBLE, reviewable whitelist the
# thesis requires: no pattern skips the door silently.
_RM_DOOR_OVERRIDES: dict[str, tuple[str, str]] = {
    # DERIVE — whole-run cancel re-derives hit_rate x the affected jobs' measured
    # billable (implemented by `_reground_whole_run_cancel_saving`, run first).
    "OPT45": (_RM_DOOR_DERIVE,
              "whole-run cancel — hit_rate x the affected jobs' measured billable"),
    # DERIVE — merge-queue STEP-LEVEL skip (#113): the job provisions a runner on
    # merge_group but skips every step, so the honest saving is confined to that
    # ONE job's compute, re-derived as hit_rate (merge_group-run share) × the
    # affected job's measured billable — never the workflow long pole. Runs
    # through the SAME `_reground_whole_run_cancel_saving` pre-pass as OPT45
    # (both are `cost_basis: affected_jobs`), so it's within the physical bound by
    # construction. Credited figure is a ceiling (only provisioning is wasted).
    "OPT29": (_RM_DOOR_DERIVE,
              "merge-queue step-level skip — hit_rate x the affected job's measured billable "
              "(only runner provisioning is wasted; the credited figure is a ceiling)"),
    # CLAMP — the cluster-floor lever's MODELED shared-step credit (#43 proving
    # instance): clamp to the affected jobs' measured billable so it can never
    # exceed what the jobs consume (nx: 1919.7 -> <= 1404.4).
    "OPT73": (_RM_DOOR_CLAMP,
              "cluster-floor lever — modeled shared-step credit, clamped to the "
              "affected jobs' measured cost-spine billable"),
    # NOT DERIVABLE — the OTHER structural step-decomposition levers (OPT70-72/74/
    # 75) credit a measured per-job STEP cost, not the whole job's billable; their
    # basis is the step decomposition, disclosed as such. Retained + stamped.
    # (Tightening these from whitelist -> clamp, like OPT73, is the flagged
    # follow-up — see the PR body; done here would widen the blast radius past the
    # #43 class cut without a measured fixture per pattern.)
    "OPT70": (_RM_DOOR_NOT_DERIVABLE, "structural step decomposition — per-job step basis, disclosed as a decomposition estimate"),
    "OPT71": (_RM_DOOR_NOT_DERIVABLE, "structural step decomposition — per-job step basis, disclosed as a decomposition estimate"),
    "OPT72": (_RM_DOOR_NOT_DERIVABLE, "structural step decomposition — per-job step basis, disclosed as a decomposition estimate"),
    "OPT74": (_RM_DOOR_NOT_DERIVABLE, "structural step decomposition — per-job step basis, disclosed as a decomposition estimate"),
    "OPT75": (_RM_DOOR_NOT_DERIVABLE, "structural step decomposition — per-job step basis, disclosed as a decomposition estimate"),
}


def _rm_door_policy(pattern: str) -> tuple[str, str]:
    """(policy, reason) for a pattern's runner-minute saving under the sizing door.
    TOTAL over every rm-crediting pattern: an override, else the pattern's _SIZING
    model family, else a LOUD UNCLASSIFIED sentinel (never a silent default) so a
    new rm-crediting pattern cannot slip through undeclared."""
    if pattern in _RM_DOOR_OVERRIDES:
        return _RM_DOOR_OVERRIDES[pattern]
    cfg = _SIZING.get(pattern)
    if cfg is None:
        return (_RM_DOOR_UNCLASSIFIED,
                f"pattern {pattern!r} credits a runner-minute saving but has no door "
                "policy — classify it in _RM_DOOR_OVERRIDES or give it a _SIZING model")
    model = cfg.get("model")
    if model == "measured":
        return (_RM_DOOR_NOT_DERIVABLE,
                "measured run-elimination detector — basis is the eliminated runs / "
                "prior-attempt slice (its own gh sizing), not the per-job cost spine")
    if model in ("direct", "runner-min-only", "wall-clock-negative", "parallel-rebalance"):
        return (_RM_DOOR_NOT_DERIVABLE,
                "modeled static estimate (default_s / hit_rate), disclosed as modeled "
                "in the report's sized-of-total ratio — not a spine-billable derivation")
    return (_RM_DOOR_UNCLASSIFIED,
            f"unknown _SIZING model {model!r} for {pattern!r} — declare its door policy")


def _positive_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _reground_runner_minute_savings(
        findings: list[dict[str, Any]], spine: dict[str, Any] | None) -> None:
    """THE sizing door (issues #43/#44/#45). Run EVERY finding that credits a
    positive `runner_min_saving` through the shared measured basis:

      1. OPT45 whole-run-cancel DERIVES from the spine first (unchanged path).
      2. Every remaining positive-rm finding gets a `runner_min_basis` stamp:
         - CLAMP patterns (OPT73) whose affected jobs join the spine → saving is
           clamped to their measured billable (`measured_spine_clamped`, or
           `measured_spine_billable` when already within it); no join → UNSIZED at
           the source (`unmeasured_no_spine_match`, saving None — the #38
           discipline: omit rather than fake).
         - NOT-DERIVABLE patterns → retained, stamped `not_spine_derivable` with the
           whitelist reason recorded in `runner_min_door_note` (visible, never a
           silent bypass).
         - a pattern with NO declared policy → stamped `UNCLASSIFIED_door_policy`
           so verify_report FAILs (the door cannot be silently skipped).

    Idempotent per pass; keys only on `runner_min_saving` + `affected_jobs` +
    `workflow_file`. Without a render-ready spine (`_spine_binds` False) the
    verifier's basis invariant SKIPs, so CLAMP/DERIVE findings stamp
    `unmeasured_no_spine` and keep their figure — the two gates agree to do
    nothing. Under a render-ready spine they MUST resolve to a measured basis or
    unsize at the source; there is no keep-the-figure escape."""
    index = _measured_billable_index(spine)
    # OPT45 derives first (also stamps its own basis / unsizes on no join). Pass
    # the shared index so the spine is scanned once, not twice.
    _reground_whole_run_cancel_saving(findings, spine, index=index)
    spine_binds = _spine_binds(spine)

    def _unsize_no_basis(f: dict[str, Any]) -> None:
        # Omit rather than fake (#38 discipline): a render-ready spine exists but
        # this finding's affected jobs resolve to NO cost-spine row with measured
        # billable, so there is no measured basis to bound the modeled figure.
        # Unsizing (saving None) is the only honest outcome — keeping the figure
        # would render an unbounded modeled saving green in a mixed report where
        # some OTHER finding IS bounded (bound (c)'s coverage SKIP only fires when
        # EVERY rm finding misses the spine).
        f["runner_min_saving"] = None
        f["runner_min_basis"] = "unmeasured_no_spine_match"
        f["size_note"] = (
            (str(f.get("size_note") or "") + "; " if f.get("size_note") else "")
            + "runner-minutes not sized — no affected job resolved to a cost-spine "
            "row with measured billable compute, so there is no measured basis to "
            "bound the saving")

    for f in findings:
        saving = f.get("runner_min_saving")
        if not _positive_number(saving):
            continue
        if f.get("runner_min_basis"):
            continue                      # already stamped this pass (OPT45)
        pattern = str(f.get("pattern") or "")
        policy, reason = _rm_door_policy(pattern)
        if policy == _RM_DOOR_NOT_DERIVABLE:
            f["runner_min_basis"] = "not_spine_derivable"
            f["runner_min_door_note"] = reason
            continue
        if policy == _RM_DOOR_UNCLASSIFIED:
            # Loud, on purpose: a rm-crediting pattern with no declared door policy
            # must NOT render as measured; verify_report's basis invariant fails on
            # this sentinel so the gap surfaces in review, not in a shipped report.
            f["runner_min_basis"] = "UNCLASSIFIED_door_policy"
            f["runner_min_door_note"] = reason
            continue
        # DERIVE / CLAMP need the measured basis.
        if index is None:
            if spine_binds:
                # Render-ready spine, non-empty rows, but the index came back empty
                # (no row carries a joinable workflow_file/job_name). The verifier
                # does NOT skip here, so keeping the modeled figure stamped
                # `unmeasured_no_spine` would read green unbounded — the door's own
                # fail-open. Unsize at the source instead, exactly like a per-finding
                # join miss.
                _unsize_no_basis(f)
            else:
                # No render-ready spine at all — the verifier's basis invariant
                # SKIPs, so there is nothing to require. Stamp and keep the figure.
                f["runner_min_basis"] = "unmeasured_no_spine"
            continue
        by_wf_job, by_job = index
        wf = str(f.get("workflow_file") or "")
        jobs = [str(j) for j in (f.get("affected_jobs") or []) if str(j).strip()]
        measured, matched, distinct = (_measured_billable_for_jobs(wf, jobs, by_wf_job, by_job)
                                       if jobs else (0.0, 0, 0))
        if matched == 0 or measured <= 0:
            _unsize_no_basis(f)
            continue
        if policy == _RM_DOOR_DERIVE:
            # A DERIVE pattern re-derives `rate × measured` in its OWN pre-pass
            # (OPT45's `_reground_whole_run_cancel_saving`, run above), which stamps
            # the basis BEFORE this loop — so a DERIVE reaching here unstamped means
            # its pre-pass never ran (a derive pattern registered without one). Do
            # NOT fall through to the clamp `min(modeled, measured)` below: for a
            # rate < 1 that OVERSTATES by 1/rate (OPT45's 0.2 → 5×) while stamping a
            # trustworthy-looking basis. Flag it loudly so verify_report FAILs.
            f["runner_min_basis"] = "UNCLASSIFIED_door_policy"
            f["runner_min_door_note"] = (
                f"pattern {pattern!r} declares a DERIVE door policy but reached the "
                "shared clamp loop unstamped — its derivation pre-pass did not run; "
                "wire it like OPT45's _reground_whole_run_cancel_saving")
            continue
        measured = round(measured, 1)
        if float(saving) > measured + 1e-9:
            # The modeled credit exceeds what the jobs measurably consume (#43,
            # OPT73 nx). Clamp DOWN to the measured billable and re-price dollars.
            f["runner_min_saving"] = measured
            f["runner_min_basis"] = "measured_spine_clamped"
            cov = "" if matched == distinct else f" ({matched}/{distinct} affected jobs measured)"
            f["size_note"] = (
                (str(f.get("size_note") or "") + "; " if f.get("size_note") else "")
                + f"clamped to the affected jobs' MEASURED monthly billable compute "
                f"({measured:g} min/mo from the cost spine){cov} — a fix cannot save "
                f"more minutes than the jobs consume")
        else:
            # Already within the measured billable — confirmed, not clamped.
            f["runner_min_basis"] = "measured_spine_billable"


def _round3(value: float) -> float:
    return round(float(value), 3)


def _runner_minute_spine_source_runs(
    events_jobs_by_wf: dict[str, dict[str, list[list[dict[str, Any]]]]],
    jobs_per_run_by_wf: dict[str, list[list[dict[str, Any]]]],
    wf_path: str,
) -> tuple[str, list[list[dict[str, Any]]]]:
    by_event = events_jobs_by_wf.get(wf_path) or {}
    if by_event:
        runs = [run for event in sorted(by_event) for run in by_event[event]]
        return "all-events", runs
    return "sampled-scope", list(jobs_per_run_by_wf.get(wf_path) or [])


def _spine_identity_text(value: object) -> str:
    """Normalize a cost-spine identity value (e.g. a job name) to the exact form
    the renderer emits, so the stamped source row round-trips through the markdown
    table and stays re-derivable from findings.json.

    A markdown table cell cannot carry a raw newline: the renderer's
    `_spine_identity_cell` flattens CR/LF to a space, so a value with embedded
    whitespace — a `name: |` block-scalar job name, e.g. the
    `Cypress E2E -\\nDocumentation` job on vuestorefront/storefront-ui — rendered
    to a DIFFERENT string than the JSON stored, and verify_report's
    `check_runner_minute_spine_contract` could not match the rendered row back to
    any source row. Collapsing `\\s+` to a single space here (matching
    `_tier2_source_job_name`'s join key and `_flatten_cell`) makes the stored
    identity byte-for-byte what the renderer emits and the verifier parses back."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _append_runner_minute_spine_rows(
    rows: list[dict[str, Any]],
    *,
    wf_path: str,
    workflow_volume: int,
    runs: list[list[dict[str, Any]]],
    sampled_workflow_runs: int,
    event_scope: str,
    status_filter: str,
    attempt_filter: str,
    volume_filter: str,
) -> None:
    if sampled_workflow_runs <= 0:
        return
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for run_jobs in runs:
        for job in run_jobs:
            name = _spine_identity_text(job.get("name"))
            if not name:
                continue
            duration_s = _job_duration_s(job)
            billed_min = _billable_equiv_min(job)
            if duration_s is None or billed_min is None:
                continue
            if (job.get("conclusion") or "").lower() == "skipped":
                continue
            duration_s = max(float(duration_s), 0.0)
            runner_label = _job_runner_label(job)
            if not runner_label:
                continue
            key = (name, runner_label)
            bucket = buckets.setdefault(key, {
                "durations_s": [],
                "billable_min": [],
                "sample_window": [],
            })
            bucket["durations_s"].append(float(duration_s))
            bucket["billable_min"].append(float(billed_min))
            created = str(job.get("_run_created_at") or "").strip()
            if created:
                bucket["sample_window"].append(created)
    for (job_name, runner_label), bucket in sorted(buckets.items()):
        occurrences = len(bucket["durations_s"])
        if occurrences <= 0:
            continue
        # Positive-duration occurrences are the ones GitHub actually bills (each
        # rounds UP to >= 1 minute). Zero-span occurrences (started_at ==
        # completed_at) bill 0 (_billable_equiv_min), so a bucket that
        # mixes them has a positive MEAN compute-second but a sub-1.0 MEAN
        # billable-minute — a legitimate shape the aggregate row cannot otherwise
        # be distinguished from a broken (un-rounded) engine. Stamping the count
        # makes the per-occurrence 1-minute floor re-derivable from the row alone.
        positive_duration_occurrences = sum(1 for d in bucket["durations_s"] if d > 0)
        occurrence_fraction = occurrences / sampled_workflow_runs
        effective_monthly = workflow_volume * occurrence_fraction
        mean_compute_s = _round3(sum(bucket["durations_s"]) / occurrences)
        mean_billed = _round3(sum(bucket["billable_min"]) / occurrences)
        raw_min_month = (mean_compute_s / 60.0) * effective_monthly
        billed_min_month = mean_billed * effective_monthly
        window = sorted(bucket["sample_window"])
        if not window:
            continue
        rows.append({
            "workflow_file": wf_path,
            "job_name": job_name,
            "runner_label": runner_label,
            "event_scope": event_scope,
            "status_filter": status_filter,
            "attempt_filter": attempt_filter,
            "volume_filter": volume_filter,
            "sample_window_start": window[0],
            "sample_window_end": window[-1],
            "sampled_workflow_run_count": sampled_workflow_runs,
            "sampled_job_occurrence_count": occurrences,
            "sampled_positive_duration_occurrence_count": positive_duration_occurrences,
            "occurrence_fraction": _round3(occurrence_fraction),
            "workflow_30d_volume": workflow_volume,
            "effective_monthly_job_volume": _round3(effective_monthly),
            "mean_sampled_compute_seconds": mean_compute_s,
            "mean_sampled_billable_equiv_minutes": mean_billed,
            "raw_compute_runner_min_per_month": _round3(raw_min_month),
            "billable_equiv_min_per_month": _round3(billed_min_month),
        })


def _build_runner_minute_spine(
    events_jobs_by_wf: dict[str, dict[str, list[list[dict[str, Any]]]]],
    jobs_per_run_by_wf: dict[str, list[list[dict[str, Any]]]],
    vol_by_wf: dict[str, int | None],
    repo_visibility: str | None,
    prior_attempt_jobs_by_wf: dict[str, dict[str, Any]] | None = None,
    workflows_in_play: list[str] | set[str] | None = None,
    triaged_workflows_included: list[str] | set[str] | None = None,
    coverage_fetch_failures: int = 0,
) -> dict[str, Any] | None:
    """Top-level cost-spine source block.

    Rows are all-events scoped when the collector has full sampled event buckets,
    so the 30d workflow volume and sample population share one denominator. A
    fallback `sampled-scope` row is still stamped for tests/old callers that only
    pass the event-scoped job list.
    """
    rows: list[dict[str, Any]] = []
    prior_attempt_jobs_by_wf = prior_attempt_jobs_by_wf or {}

    def _is_volume_count(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    workflows_in_play_set = {
        str(wf) for wf in (workflows_in_play or []) if str(wf)
    }
    triaged_included = sorted({
        str(wf) for wf in (triaged_workflows_included or []) if str(wf)
    })
    for wf_path in sorted(set(events_jobs_by_wf) | set(jobs_per_run_by_wf) |
                          set(prior_attempt_jobs_by_wf)):
        workflow_volume = vol_by_wf.get(wf_path)
        if not _is_volume_count(workflow_volume) or workflow_volume < 0:
            continue
        event_scope, runs = _runner_minute_spine_source_runs(
            events_jobs_by_wf, jobs_per_run_by_wf, wf_path)
        _append_runner_minute_spine_rows(
            rows, wf_path=wf_path, workflow_volume=workflow_volume, runs=runs,
            sampled_workflow_runs=len(runs),
            event_scope=event_scope, status_filter="success", attempt_filter="latest",
            volume_filter="all-status")
        prior = prior_attempt_jobs_by_wf.get(wf_path) or {}
        prior_runs = prior.get("runs") if isinstance(prior, dict) else None
        prior_sampled_n = prior.get("sampled_workflow_run_count") if isinstance(prior, dict) else None
        if (isinstance(prior_runs, list) and _is_volume_count(prior_sampled_n)):
            _append_runner_minute_spine_rows(
                rows, wf_path=wf_path, workflow_volume=workflow_volume, runs=prior_runs,
                sampled_workflow_runs=prior_sampled_n,
                event_scope=str(prior.get("event_scope") or "all-events"),
                status_filter="all-status", attempt_filter="prior",
                volume_filter="all-status")
    if not rows and not workflows_in_play_set:
        return None
    latest_attempt_row_count = sum(1 for row in rows if row.get("attempt_filter") == "latest")
    prior_attempt_row_count = sum(1 for row in rows if row.get("attempt_filter") == "prior")
    total_raw = sum(float(r["raw_compute_runner_min_per_month"]) for r in rows)
    total_billed = sum(float(r["billable_equiv_min_per_month"]) for r in rows)
    for row in rows:
        denom = total_billed
        row["share_of_all_row_total"] = _round3(
            float(row["billable_equiv_min_per_month"]) / denom) if denom > 0 else 0.0
    row_workflows = {str(row.get("workflow_file") or "") for row in rows}
    coverage_eligible = sorted(
        wf for wf in workflows_in_play_set
        if _is_volume_count(vol_by_wf.get(wf)) and (vol_by_wf.get(wf) or 0) > 0)
    unknown_volume = sorted(
        wf for wf in workflows_in_play_set
        if not _is_volume_count(vol_by_wf.get(wf)) or vol_by_wf.get(wf) < 0)
    omitted = sorted(set(coverage_eligible) - row_workflows)
    has_explicit_coverage = bool(workflows_in_play_set)
    complete_repo_coverage = (
        has_explicit_coverage and bool(rows) and not omitted and not unknown_volume and
        int(coverage_fetch_failures) == 0)
    coverage_scope = (
        "sampled_workflows_in_play_with_job_data"
        if has_explicit_coverage else "sampled_workflows_with_job_data")
    render_blocker = (
        "" if complete_repo_coverage else
        "rows cover sampled workflows with job data; one or more workflows in play "
        "still lack positive-volume confirmation, cost-spine job rows, or had "
        "cost-spine job-fetch failures")
    return {
        "schema_version": 1,
        "source": "jobs_api_sampled_runs",
        "coverage_scope": coverage_scope,
        "complete_repo_coverage": complete_repo_coverage,
        "render_ready": complete_repo_coverage,
        "render_blocker": render_blocker,
        "extrapolation_basis": (
            "sampled_job_occurrence_fraction_x_all_status_30d_workflow_volume"),
        "attempt_coverage": "latest_and_prior",
        # Derived fact (PR-S2): equals `prior_attempt_row_count > 0` — it
        # states what THIS sample contains, not what the pipeline can fetch
        # (that is `attempt_coverage`). verify_report enforces the equality.
        "prior_attempts_included": prior_attempt_row_count > 0,
        "latest_attempt_row_count": latest_attempt_row_count,
        "prior_attempt_row_count": prior_attempt_row_count,
        "repo_visibility": repo_visibility,
        "workflow_coverage": {
            "scope": "positive_30d_workflows_in_play",
            "workflow_count": len(coverage_eligible),
            "row_workflow_count": len(row_workflows & set(coverage_eligible)),
            "omitted_workflows": omitted,
            "unknown_volume_workflows": unknown_volume,
            "triaged_workflows_included": triaged_included,
            "job_fetch_failures": int(coverage_fetch_failures),
        },
        "rows": rows,
        "totals": {
            "row_count": len(rows),
            "raw_compute_runner_min_per_month": _round3(total_raw),
            "billable_equiv_min_per_month": _round3(total_billed),
            "percentage_denominator": "all_rows_billable_equiv_min_per_month",
        },
    }


# =============================================================================
# Data-driven detectors — read run-history that collect() already sampled and
# emit findings whose pattern matches a catalog "Detection heuristic".
# =============================================================================

import re as _re
import statistics as _stats

_SETUP_STEP_RE = _re.compile(
    r"^(set up |setup |checkout|install|cache|restore|fetch|"
    r"docker (login|pull|compose up)|configure|init |bootstrap|"
    r"pnpm/action-setup|setup-node|setup-python|setup-go|setup-pnpm)",
    _re.IGNORECASE,
)
_WORK_STEP_RE = _re.compile(
    r"^(run )?(test|lint|build|type-?check|coverage|format|"
    r"vitest|jest|pytest|playwright|cypress|nextest|cargo (build|test)|"
    r"go test|npm test|pnpm (test|lint|build)|tsc|eslint|prettier)",
    _re.IGNORECASE,
)
_POST_STEP_RE = _re.compile(r"^Post[\s:]", _re.IGNORECASE)


_CHECKOUT_STEP_RE = _re.compile(r"checkout|fetch.?depth|fetch repo|clone", _re.I)


def _effective_volume(monthly_volume: int | None, n_samples: int,
                      n_runs: int) -> float:
    """Scale workflow monthly volume to a single JOB's observed run frequency.

    A per-step / per-job finding must be sized by how often THAT job actually
    runs, not the workflow's total volume. A job behind an `if:` gate (or a
    reusable-workflow leg) shows up in only a fraction of the sampled runs; the
    detector already has that fraction as `n_samples / n_runs`. Sizing every
    such job at the full workflow volume was the dominant over-statement in the
    mastra report (gated docs jobs, reusable-workflow legs credited the full
    ~5k/mo). `n_samples / n_runs` is a measured lower bound on the gate hit rate.
    """
    if not monthly_volume or n_runs <= 0:
        return 0.0
    frac = min(max(n_samples / n_runs, 0.0), 1.0)
    return monthly_volume * frac


def _classify_step(name: str) -> str:
    if _POST_STEP_RE.match(name or ""):
        return "post"
    if _SETUP_STEP_RE.match(name or ""):
        return "setup"
    if _WORK_STEP_RE.match(name or ""):
        return "work"
    return "other"


def _step_durations(job: dict[str, Any]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for s in job.get("steps") or []:
        if not isinstance(s, dict):
            continue
        d = _duration_s(s.get("started_at"), s.get("completed_at"))
        if d is not None and d > 0:
            out.append((str(s.get("name", "")), d))
    return out


def _step_timeline(job: dict[str, Any], job_name: str,
                   job_dur_s: float) -> dict[str, Any]:
    """One job instance's per-step timeline in EXECUTION ORDER: each step's start
    offset (from job start) and its duration. This is what lets the report draw the
    step level as a succession timeline (steps run one after another) rather than
    left-aligned bars. The jobs listing already carries started_at/completed_at, so
    this costs no extra gh call."""
    j0 = _parse_dt(job.get("started_at"))
    steps: list[dict[str, Any]] = []
    for s in job.get("steps") or []:
        if not isinstance(s, dict):
            continue
        st = _parse_dt(s.get("started_at"))
        en = _parse_dt(s.get("completed_at"))
        if not st or not en:
            continue
        start = (st - j0).total_seconds() if j0 else 0.0
        # `number` is GitHub's 1-based step index, used to deep-link the job log at
        # `…/job/<id>#step:<number>:1` so the report's drill is auditable to the step.
        steps.append({"name": str(s.get("name", "")),
                      "number": s.get("number"),
                      "start_s": round(max(start, 0.0), 1),
                      "dur_s": round((en - st).total_seconds(), 1)})
    job_url = str(job.get("html_url", ""))
    return {
        "job": job_name,
        "run_url": job_url.split("/job/")[0],
        "job_url": job_url,        # the job page - deep-linkable per step (#step:N:1)
        "job_id": job.get("id"),
        "job_dur_s": job_dur_s,
        "steps": steps,
    }


# Fine-grained step categories for STRUCTURAL decomposition — checkout /
# install / build / test / scan / package / setup. This is intentionally a
# SEPARATE classifier from `_classify_step` (the coarse setup/work/post/other
# bucket the hygiene detectors OPT49/OPT51 depend on): extending `_classify_step`
# would silently shift what those detectors count as "setup". Order matters —
# the first matching category wins, so the more specific patterns lead.
#
# Token fragments shared by the COMBINED payload+build entries (below) and used to
# recognize a single step that BOTH builds AND runs payload work. `_BUILD_HINT` is a
# broad "there is a build verb somewhere in this name" signal — it only steers a
# classification when a payload-exec token co-occurs, so breadth here is safe.
# `_PAYLOAD_TEST_EXEC` guards the bare `test(s)` token against build-artifact
# compounds (`test image`, `test fixtures`, `test binary`, … name a build OUTPUT,
# not test execution) so "Build FIPS test image" stays a `build`.
_BUILD_ARTIFACT_NOUNS = (
    r"images?|fixtures?|binar(?:y|ies)|bins?|apps?|harness(?:es)?|containers?|"
    r"artifacts?|jars?|wheels?|libs?|projects?|targets?|data|bundles?")
_BUILD_HINT = (
    r"\bbuild\b|\bcompile\b|webpack|rollup|esbuild|\bturbo\b|\bnx\b|next build|"
    r"cargo build|go build|gradle|\bbazel\b|dotnet build|\bmvn\b|transpile|\bbundle\b")
_PAYLOAD_TEST_EXEC = (
    # Bare `unit` is OMITTED here (kept only in the standalone `test` entry): next to
    # a build token it is usually part of a "unit test <artifact>" build name. The
    # unambiguous runner tokens need no artifact guard. The artifact noun may be
    # whitespace- OR hyphen-joined ("test image" / "test-image") — both are builds.
    r"\btests?\b(?![\s-]+(?:" + _BUILD_ARTIFACT_NOUNS + r"))|\bspec\b|\be2e\b|"
    r"unittest|pytest|jest|vitest|playwright|cypress|nextest|rspec|"
    r"phpunit|coverage")
_PAYLOAD_SCAN_EXEC = (
    r"\blint\b|eslint|codeql|\bsast\b|semgrep|trivy|snyk|typecheck|type.?check|"
    r"biome|ruff|clippy|sonar")
_STEP_CATEGORY_RES: list[tuple[str, "_re.Pattern[str]"]] = [
    ("post", _re.compile(r"^Post[\s:]", _re.IGNORECASE)),
    ("checkout", _re.compile(r"checkout|fetch.?depth|git fetch|\bclone\b|sparse.?checkout", _re.IGNORECASE)),
    ("install", _re.compile(
        r"\binstall\b|npm ci|npm install|pnpm i\b|pnpm install|yarn install|"
        r"bundle install|pip install|poetry install|uv (pip |sync)|cargo fetch|"
        r"go mod (download|tidy)|restore (dependencies|deps|cache|nuget)|"
        r"download deps|cache restore|setup-node|setup-python|setup-go|setup-java|"
        r"setup-dotnet|pnpm/action-setup|actions/cache", _re.IGNORECASE)),
    # Artifact UPLOAD / publish / release steps are packaging even when the name also
    # carries the broad `build`/`bundle` token (e.g. "Upload build artifacts" —
    # `\bbuild\b` would otherwise steal it for `build` below). Only the unambiguously-
    # TERMINAL signals live here, so build-flavored packaging like `mvn package`
    # (intended as a build) still classifies via the `build` entry's `mvn (package|…)`.
    ("package", _re.compile(
        r"upload\b.*\bartifacts?\b|create release|push image|docker push|"
        r"\bbundle artifact\b", _re.IGNORECASE)),
    # COMBINED payload+build steps lead `build`. A SINGLE step whose name carries
    # BOTH a build token AND a genuine PAYLOAD-EXECUTION token — e.g. nrwl/nx's
    # "Run Checks/Lint/Test/Build" (one `nx affected` step that lints + tests +
    # builds, with `playwright test` running inside it) — IS the payload the job
    # exists for, so it must bin as PAYLOAD, not setup/build. Binning it 100% as
    # `build` (a `_SETUP_BUILD_CATEGORIES` member) puts the whole step in the
    # redundant-work numerator (setup+build ÷ payload), inflates the ratio past 2.0,
    # and misroutes the pole onto OPT72 "warm the build cache" when the step's time
    # is test/lint execution. This mirrors the `package`-before-`build` precedent
    # above (a more-specific co-occurring signal overrides the broad build token).
    # It is deliberately additive and requires BOTH tokens: a pure build step keeps
    # its category, and the bare `\btest(s)\b` payload token is GUARDED against
    # build-artifact compounds ("Build FIPS test image", "Compile test fixtures" —
    # `test <artifact-noun>` names a build OUTPUT, not test execution) so a genuine
    # build-of-test-artifacts step still classifies as `build`. The `-test` half
    # routes to `test`; a build step that only ALSO lints (no test-exec token)
    # routes to `scan` via the second entry.
    ("test", _re.compile(
        r"(?=.*(?:" + _BUILD_HINT + r"))"
        r"(?=.*(?:" + _PAYLOAD_TEST_EXEC + r"))", _re.IGNORECASE)),
    ("scan", _re.compile(
        r"(?=.*(?:" + _BUILD_HINT + r"))"
        r"(?=.*(?:" + _PAYLOAD_SCAN_EXEC + r"))", _re.IGNORECASE)),
    ("build", _re.compile(
        r"\bbuild\b|\bcompile\b|\btsc\b|webpack|vite build|rollup|esbuild|"
        r"turbo run build|turbo build|nx (run|build)|next build|cargo build|"
        r"go build|gradle|\bbazel\b|\bmake\b|dotnet build|mvn (package|compile)|"
        r"transpile|bundle\b", _re.IGNORECASE)),
    ("test", _re.compile(
        # `unittest` is a BARE substring (no `\b`): the boundary-anchored
        # `\btest\b`/`\btests\b`/`\bunit\b` all miss the glued "unittests"/
        # "unittest" token (e.g. a "Run unittests" step running `pytest`), which
        # otherwise falls through to `other` and escapes the PAYLOAD set used in
        # redundant-ratio sizing.
        r"\btest\b|\btests\b|\bspec\b|\be2e\b|integration|vitest|jest|pytest|"
        r"playwright|cypress|nextest|go test|cargo test|\bunit\b|unittest|coverage|"
        r"npm test|pnpm test|gradle test|rspec|phpunit", _re.IGNORECASE)),
    ("scan", _re.compile(
        r"\bscan\b|codeql|\blint\b|eslint|\baudit\b|security|sast|semgrep|"
        r"trivy|snyk|type.?check|typecheck|\btsc --noemit\b|biome|ruff|"
        r"clippy|vet\b|analyze|sonar", _re.IGNORECASE)),
    # Remaining packaging signals that DON'T conflict with build (checked after build
    # so `mvn package`/`docker build` stay builds): bare package/publish/archive verbs
    # and `docker buildx` (no `\bbuild\b` boundary before the `x`, so build misses it).
    ("package", _re.compile(
        r"\bpackage\b|docker buildx|\bpublish\b|\barchive\b|\btar\b|\bzip\b",
        _re.IGNORECASE)),
    ("setup", _re.compile(
        r"^(set up |setup |configure|init |bootstrap|provision|toolchain|"
        r"docker (login|pull|compose up))", _re.IGNORECASE)),
]

# Step categories that are SETUP/redundant work (the numerator of the
# redundant-work ratio) vs PAYLOAD (the work the job exists to do).
_SETUP_BUILD_CATEGORIES = frozenset({"checkout", "install", "build", "setup"})
_PAYLOAD_CATEGORIES = frozenset({"test", "scan", "package"})


def _step_category(name: str) -> str:
    """Fine-grained category for a step name (structural decomposition). Returns
    one of checkout/install/build/test/scan/package/setup/post/other."""
    n = name or ""
    for cat, rx in _STEP_CATEGORY_RES:
        if rx.search(n):
            return cat
    return "other"


def _slow_mode_instances(
    job_instances: list[dict[str, Any]], bimodal: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Restrict a BIMODAL job's sampled instances to its SLOW cluster so a
    structural decomposition reconciles with the slow-mode run the report
    actually drills (`blocking_path` targets the slow-cluster median,
    `high_p50_s`, for a bimodal pole).

    A bimodal long pole gates *because of* its slow mode (e.g. a cold Docker
    build that is cached-warm on most PRs). Blending the per-step p50 across BOTH
    modes drags the bimodal step's p50 below the steady steps and crowns the
    wrong dominant category — the structural router then misfires (OPT75 instead
    of OPT72) and the pole's own headline contradicts its slow-mode drill, audit
    link, and agent prompt. Keep only instances whose total duration sits at or
    above the midpoint between the two cluster medians. Returns the input
    unchanged when the pole is not bimodal, the split is degenerate, or the
    filter would empty the set (there must always be something to decompose)."""
    if not bimodal:
        return job_instances
    try:
        lo = float(bimodal.get("low_p50_s"))
        hi = float(bimodal.get("high_p50_s"))
    except (TypeError, ValueError):
        return job_instances
    if hi <= lo:
        return job_instances
    mid = (lo + hi) / 2.0
    slow = [j for j in job_instances if (_job_duration_s(j) or 0.0) >= mid]
    return slow or job_instances


# How many of the most-recent sampled instances count as the "current version" era when picking the
# decomposition anchor. Small so a real workflow MIGRATION still drops old-era steps, but >1 so a
# single cancelled/failed newest run can't truncate the anchor's step set (see _current_version_steps).
_CURRENT_VERSION_ANCHOR_RUNS = 3


def _current_version_steps(
    job_instances: list[dict[str, Any]],
) -> set[str] | None:
    """The step-name set of the MOST-COMPLETE run among the most-recent sampled instances — i.e. the
    workflow version the audited tip executes. The structural decomposition aggregates per-step p50s by
    name; across a workflow MIGRATION (steps renamed/added/removed) that union mixes versions and
    injects phantom steps the audited commit no longer runs. Restricting to a recent anchor's step
    names drops the old-era names while keeping every step the current version declares (the anchor's
    full `steps` list is used, not only its >0-duration steps, so a step that happened to be
    instant/skipped is still kept).

    The anchor is NOT simply the most-recently-started run: a newest run that was CANCELLED or FAILED
    early reports only the steps that ran before it stopped — a strict SUBSET — so a recency-only
    anchor would silently drop every real step on a truncated newest run and crown boilerplate (e.g.
    `Set up job`). Instead, among the most-recent `_CURRENT_VERSION_ANCHOR_RUNS` instances, anchor on
    the one declaring the MOST steps (recency breaks ties, so a genuine version that ADDED a step still
    wins). Limiting to the recent window keeps version-currency — an old migration's removed steps are
    not in any recent run; a migration WITHIN the last few runs is far rarer than a single aborted run,
    and re-keeping one removed step for a cycle is far less harmful than dropping the whole decomposition.

    Returns None when no instance carries a usable timestamp (the caller then does NOT filter — unknown
    != stale)."""
    def _key(j: dict[str, Any]) -> "_dt.datetime | None":
        return (_parse_dt(j.get("started_at")) or _parse_dt(j.get("completed_at"))
                or _parse_dt(j.get("_run_created_at")))
    dated = [(k, j) for j in job_instances if (k := _key(j)) is not None]
    if not dated:
        return None
    dated.sort(key=lambda kj: kj[0], reverse=True)            # most-recent first
    recent = dated[:_CURRENT_VERSION_ANCHOR_RUNS]             # the current-version era
    def _step_names(j: dict[str, Any]) -> set[str]:
        return {str(s.get("name", "")) for s in (j.get("steps") or []) if isinstance(s, dict)}
    # most steps = most complete; tie -> most recent (the (k) recency key, already sorted desc).
    _k, anchor = max(recent, key=lambda kj: (len(_step_names(kj[1])), kj[0]))
    return _step_names(anchor) or None


def _decompose_job_steps(
    job_instances: list[dict[str, Any]],
    bimodal: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Break a job (its sampled instances) into per-step p50s, classify each by
    category, and report the DOMINANT lever + its share of the job. The dominant
    lever is the step CATEGORY with the largest AGGREGATE p50, not the single
    largest step: many jobs run the same payload back-to-back under several
    configs (e.g. one test suite run four times against four state backends —
    four sequential `test` steps, none individually dominant but together the
    bulk of the job). Crowning only the single max step there understates the real
    lever (sizing/evidence/audit would scale to one 23%-of-job step instead of the
    ~77% test phase), so comparable same-category steps are aggregated.

    Returns None when there are no step timings. Otherwise a dict:
      dominant_step (the slowest step in the dominant category, OR an aggregate label
        "<step> + N more <cat> steps" when the category spans several comparable steps)
      dominant_category / dominant_p50 (the category aggregate) / dominant_share
      setup_build_s / payload_s / redundant_ratio (setup+build ÷ payload)
      steps: [(name, category, p50)] slowest-first, job_p50.

    When `bimodal` is supplied (the job's fast/slow split), the decomposition is
    computed over the SLOW-cluster instances only, so the dominant category it
    crowns agrees with the slow-mode run the report drills — otherwise a bimodal
    pole's headline contradicts its own drill (see `_slow_mode_instances`)."""
    job_instances = _slow_mode_instances(job_instances, bimodal)
    # Reconcile to a SINGLE workflow version before aggregating. The sampled window
    # can SPAN a workflow migration (e.g. cortex/ripasso swapped `rust.yml` from
    # `actions-rs/*` + checkout@v3 to `actions-rust-lang/setup-rust-toolchain` +
    # direct `run:` + checkout@v6). Keying `by_step` by step NAME across instances of
    # BOTH versions injects PHANTOM steps that are ABSENT from the audited commit
    # (`Run actions-rs/cargo@v1`) and double-counts a renamed operation — and the
    # phantom aggregate can then out-vote the real current step, crowning a lever the
    # fix agent cannot find in the tree. Anchor on the step-name set of the MOST
    # RECENTLY-started instance (the version the audited tip runs) and aggregate only
    # those names, so old-era step names never enter the decomposition. (Falls back to
    # no filtering when no instance carries a usable timestamp — unknown != stale.)
    current_steps = _current_version_steps(job_instances)
    by_step: dict[str, list[float]] = {}
    for j in job_instances:
        for sname, d in _step_durations(j):
            if current_steps is not None and sname not in current_steps:
                continue
            by_step.setdefault(sname, []).append(d)
    if not by_step:
        return None
    steps = [
        (name, _step_category(name), _percentile(ds, 50))
        for name, ds in by_step.items()
    ]
    steps.sort(key=lambda s: -s[2])
    job_p50 = sum(p for _n, _c, p in steps)
    if job_p50 <= 0:
        return None
    # The dominant lever = the category with the largest aggregate p50. Tie-break
    # to the category owning the single largest step (steps are duration-desc, so
    # `max` over them is deterministic). For a category with one step this is the
    # old single-step behaviour; for several it credits the whole phase.
    #
    # Pick the dominant ONLY over load-bearing steps: setup/teardown boilerplate
    # (set up job, checkout, post, setup-node — `_NON_WORK_STEP_RE`) is excluded
    # here, the SAME way the cross-run check and the agent prompt exclude it
    # (`_dominant_step_sample` / `_dominant_step_from_timeline`). Without this,
    # a job whose boilerplate out-aggregates its one real step crowned a non-work
    # `setup` step as the "dominant step" while the rest of the report named the
    # longest non-setup step — two different dominant steps for one pole (deepgram
    # `Title Check`: setup 5s out-aggregated the addressable `Install commitlint`
    # 4s). Boilerplate still counts toward `setup_build_s` / `redundant_ratio`
    # below — it's real cost — just not an addressable dominant lever. Fall back to
    # the full set only when a job is ALL boilerplate (nothing else to crown).
    sel = [s for s in steps if not _NON_WORK_STEP_RE.match(s[0])] or steps
    cat_p50: dict[str, float] = {}
    for _n, c, p in sel:
        cat_p50[c] = cat_p50.get(c, 0.0) + p
    dom_cat = max(sel, key=lambda s: (cat_p50[s[1]], s[2]))[1]
    cat_steps = [s for s in sel if s[1] == dom_cat]
    dom_p50 = cat_p50[dom_cat]
    if len(cat_steps) == 1:
        dom_name = cat_steps[0][0]
    else:
        # Make the aggregation explicit: name the slowest step in the phase and
        # how many comparable same-category steps it stands in for, so no single
        # step is crowned as if it were the entire lever.
        extra = len(cat_steps) - 1
        dom_name = (f"{cat_steps[0][0]} + {extra} more {dom_cat} "
                    f"step{'s' if extra != 1 else ''}")
    setup_build_s = sum(p for _n, c, p in steps if c in _SETUP_BUILD_CATEGORIES)
    payload_s = sum(p for _n, c, p in steps if c in _PAYLOAD_CATEGORIES)
    ratio = (setup_build_s / payload_s) if payload_s > 0 else float("inf")
    return {
        "dominant_step": dom_name,
        "dominant_category": dom_cat,
        "dominant_p50": round(dom_p50, 1),
        "dominant_share": round(dom_p50 / job_p50, 3),
        "setup_build_s": round(setup_build_s, 1),
        "payload_s": round(payload_s, 1),
        "redundant_ratio": (round(ratio, 2) if ratio != float("inf") else None),
        "steps": [(n, c, round(p, 1)) for n, c, p in steps],
        "job_p50": round(job_p50, 1),
    }


def _dominant_category_lead(named_durs: "list[tuple[str, float]]") -> "tuple[str, float] | None":
    """The LEAD step (name, dur) of the dominant CATEGORY — the SAME crown
    `_decompose_job_steps` uses (the category with the largest non-boilerplate aggregate
    p50, then its slowest step; boilerplate `_NON_WORK_STEP_RE` excluded; fall back to
    the full set only when a job is all-boilerplate). Returns None when there are no
    positive-duration steps.

    Single source of truth so the cross-run magnitude check (`_dominant_step_sample`) and
    the agent prompt validate/name the SAME step the structural decomposition crowns —
    instead of the single longest step overall, which lives in a NON-dominant category
    when a multi-step phase (e.g. four sequential `test` steps) out-aggregates one big
    `build` step. Mirrors `_decompose_job_steps`'s selection exactly (same `_step_category`
    + `_NON_WORK_STEP_RE` + max-by-(aggregate, dur) tie-break)."""
    items = [(n, d) for n, d in named_durs if isinstance(d, (int, float)) and d > 0]
    if not items:
        return None
    work = [(n, d) for n, d in items if not _NON_WORK_STEP_RE.match(n)] or items
    cat_p50: dict[str, float] = {}
    for n, d in work:
        cat_p50[_step_category(n)] = cat_p50.get(_step_category(n), 0.0) + d
    dom_cat = _step_category(max(work, key=lambda nd: (cat_p50[_step_category(nd[0])], nd[1]))[0])
    return max((nd for nd in work if _step_category(nd[0]) == dom_cat), key=lambda nd: nd[1])


@dataclass(frozen=True)
class RequiredChecks:
    """The repo's required status-check contexts, plus a completeness flag.

    `names` is always trustworthy for membership: a check IN the set is
    definitely required. `complete` is False when we could read SOME but not
    ALL of the required-status configuration — a ruleset we know exists
    couldn't be read, or the default branch was unknown so classic branch
    protection couldn't be checked. When `complete` is False, a check ABSENT
    from `names` is UNKNOWN, never not-required, so a required check hidden in
    an unreadable ruleset is never mistaken for an OPT71 de-trigger candidate."""
    names: frozenset[str]
    complete: bool


def _ruleset_ref_in_scope(detail: dict[str, Any], branch: str | None) -> bool:
    """Does this ruleset actually govern the branch we're scoring?

    A ruleset's `required_status_checks` rules only make a check merge-blocking for the
    branches the ruleset TARGETS. GitHub encodes that in two fields already present in
    the fetched `detail`:
      - `target` — "branch" | "tag" | "push". Only a branch ruleset gates a PR merge.
      - `conditions.ref_name.{include,exclude}` — pattern lists over `refs/heads/...`,
        with the specials `~ALL` (every branch) and `~DEFAULT_BRANCH` (the repo's
        default branch — which IS the branch this audit scores).
    So a `release/*`-only ruleset, or one targeting tags, does NOT make its checks
    required for `main` — counting them would headline a non-gating check as the pole.

    Conservative, mirroring `_fetch_required_checks`'s "absent info → UNKNOWN, never
    not-required" stance (OPT71's guardrail): returns True whenever scope can't be
    determined (`branch` unknown, or `target`/`conditions`/`ref_name` absent or an
    unexpected shape). We return False ONLY when we can affirmatively read that the
    scored branch is out of this ruleset's scope."""
    if not branch:
        return True
    target = detail.get("target")
    if isinstance(target, str) and target != "branch":
        return False  # a tag / push ruleset can't gate a branch merge
    cond = detail.get("conditions")
    if not isinstance(cond, dict):
        return True
    ref = cond.get("ref_name")
    if not isinstance(ref, dict):
        return True
    include = ref.get("include")
    exclude = ref.get("exclude")
    if include is None and exclude is None:
        return True
    full = f"refs/heads/{branch}"

    def _hit(patterns: Any) -> bool:
        for p in patterns or []:
            if not isinstance(p, str):
                continue
            # ~ALL matches every branch; ~DEFAULT_BRANCH matches the default branch,
            # which is the branch this function is scoring.
            if p in ("~ALL", "~DEFAULT_BRANCH"):
                return True
            # fnmatch lets `*` cross `/`, where GitHub's ref globs keep `*` within a
            # segment (only `**` crosses). This is an intentional approximation that
            # leans PERMISSIVE (more likely to keep → never under-detect a gate); it is
            # moot for the slash-free default branch this audit always scores.
            if p == full or fnmatch.fnmatch(full, p):
                return True
        return False

    if _hit(exclude):
        return False  # branch explicitly excluded from this ruleset
    if include is None:
        return True  # only excludes given, branch not among them → in scope
    include_strs = [p for p in include if isinstance(p, str)]
    if include and not include_strs:
        # Include is present but UNREADABLE — non-empty yet carrying no string
        # patterns (e.g. a malformed `[null]` from the API). We can't affirm the
        # branch is out of scope, so keep it (conservative — matches the absent-info
        # stance). An EMPTY include (`[]`) is different: it's a readable
        # "matches no ref", so it falls through to `_hit([]) == False` and the
        # ruleset is dropped (gating nothing, dropping it can't under-detect).
        return True
    return _hit(include_strs)


def _fetch_required_checks(
    client: "GhClient", repo: str, branch: str | None = "main",
) -> RequiredChecks | None:
    """The repo's REQUIRED status-check contexts, from rulesets + classic branch
    protection. Returns a `RequiredChecks` (names + completeness), or None when
    NOTHING was readable (the common case auditing a repo you don't own: rulesets
    and branch protection both 404 without admin). None means "required status
    UNKNOWN" — callers must NOT then assert a check is non-required (OPT71's
    guardrail).

    A PARTIAL read returns `RequiredChecks(names, complete=False)`: the names
    found are real, but a check absent from them is UNKNOWN (not not-required),
    so we never recommend de-triggering a check that an unread ruleset — or the
    unchecked default branch — might require. This is the safe direction for the
    "no silent false-negative findings" rule: a partial read must never
    masquerade as an authoritative not-required answer.

    Repo-agnostic: driven entirely off the GitHub API, no hardcoded names."""
    names: set[str] = set()
    found_any = False
    complete = True
    # Rulesets (the modern mechanism) — readable without admin on many repos.
    rulesets = client.json(f"repos/{repo}/rulesets?includes_parents=true",
                           allow_missing=True)
    if isinstance(rulesets, list):
        for rs in rulesets:
            rid = rs.get("id") if isinstance(rs, dict) else None
            if rid is None:
                # A ruleset we can't even identify may carry required checks.
                complete = False
                continue
            detail = client.json(f"repos/{repo}/rulesets/{rid}", allow_missing=True)
            if not isinstance(detail, dict):
                # We KNOW this ruleset exists (it was in the list) but couldn't
                # read it — it may require checks we'd otherwise miss. Mark the
                # read incomplete so absent checks stay UNKNOWN, not not-required.
                complete = False
                continue
            if (detail.get("enforcement") or "active") != "active":
                # `evaluate` (dry-run) or `disabled` rulesets are NOT live merge
                # gates — their required checks don't block a merge. Counting them
                # would crown a non-gating check as the pole. Skip (a missing field
                # defaults to "active" — the conservative keep).
                continue
            if not _ruleset_ref_in_scope(detail, branch):
                # This ruleset governs other refs (a release/* branch, a tag), not the
                # branch we're scoring — its checks aren't required for THIS branch.
                continue
            for rule in detail.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                if rule.get("type") == "required_status_checks":
                    found_any = True
                    params = rule.get("parameters") or {}
                    for c in params.get("required_status_checks") or []:
                        if isinstance(c, dict) and c.get("context"):
                            names.add(str(c["context"]))
    elif rulesets is not None:
        # We got a body but not the expected list — an error object (e.g.
        # `{"message": ...}`) or an unexpected shape, NOT a clean 404 (which
        # returns None via allow_missing). We can't trust we saw every ruleset,
        # so the read is incomplete: absent checks stay UNKNOWN, never
        # not-required (a check could live in the ruleset payload we couldn't read).
        complete = False
    # Classic branch protection (404 without admin → leave as unknown unless a
    # ruleset already answered). Needs the real default branch; if we couldn't
    # resolve it, skip rather than guess "main" (a wrong guess 404s and would
    # silently under-detect required checks), and mark the read incomplete.
    if branch:
        prot = client.json(
            f"repos/{repo}/branches/{branch}/protection/required_status_checks",
            allow_missing=True)
        if isinstance(prot, dict):
            found_any = True
            for c in prot.get("contexts") or []:
                names.add(str(c))
            for c in prot.get("checks") or []:
                if isinstance(c, dict) and c.get("context"):
                    names.add(str(c["context"]))
    else:
        complete = False
    if not found_any and complete:
        return None
    return RequiredChecks(frozenset(names), complete)


def _matrix_base_name(job_name: str) -> str:
    """Strip the trailing matrix-args parenthetical, e.g. `test (18, ubuntu)` → `test`.

    Delegates to blocking_path._matrix_base_RAW — the canonical trailing-anchored
    reduction — so the renderer and the drill engine never diverge on where a
    matrix leg's base ends (they used to: a hand-rolled duplicate here matched
    the FIRST `(` while blocking_path's leaf helper had its own semantics). The
    RAW helper (not the display `_matrix_base`) is deliberate: this function
    builds required-check MATCHING KEYS, so it must be scope- and case-PRESERVING
    — `@a/pkg build (18)` and `@b/pkg build (18)` stay distinct keys, and `Build`
    doesn't fold into `build`. Falls back to the original (stripped) name when
    there's no trailing parenthetical: unlike `_matrix_base_raw`'s `None` ("not a
    matrix leg"), this helper's contract is a plain str reduction."""
    import blocking_path as bp  # same-skill module; local import mirrors other call sites here
    base = bp._matrix_base_raw(job_name)
    return base if base is not None else job_name.strip()


def _detect_common_suffix_matrices(
    jobs_per_run: list[list[dict[str, Any]]],
) -> dict[str, str]:
    """Return {job_name: matrix_base} for jobs whose matrix axis appears as a
    VARYING PREFIX (e.g. `@org/prisma-adapter Integration Test` vs
    `@org/drizzle-adapter Integration Test` — same suffix, different package).

    The conventional `_matrix_base_name` only strips trailing parens, so
    prefix-varying matrices like better-auth's adapter integration tests
    each become their own singleton and the imbalance detector never sees
    the group. This function detects them by finding the longest trailing
    N-gram (N ≥ 2 tokens) shared by ≥ 3 jobs and using that as the base.
    """
    job_names = set()
    for run_jobs in jobs_per_run:
        for j in run_jobs:
            name = str(j.get("name", "")).strip()
            if name:
                job_names.add(name)
    if len(job_names) < 3:
        return {}
    # Build a {trailing-N-gram: [job_names...]} map for N in {2,3,4}.
    by_suffix: dict[str, list[str]] = {}
    for name in job_names:
        toks = name.split()
        for n in range(2, min(5, len(toks))):
            suffix = " ".join(toks[-n:])
            by_suffix.setdefault(suffix, []).append(name)
    # Pick the longest suffix with ≥ 3 distinct prefixes; that's the matrix.
    # Sort suffixes by token count (desc) then by group size (desc), so the
    # most specific shared suffix wins.
    candidates = [
        (suffix, members)
        for suffix, members in by_suffix.items()
        if len(set(members)) >= 3
    ]
    if not candidates:
        return {}
    candidates.sort(key=lambda kv: (-len(kv[0].split()), -len(set(kv[1]))))
    out: dict[str, str] = {}
    used: set[str] = set()
    for suffix, members in candidates:
        new_members = [m for m in members if m not in used]
        if len(set(new_members)) < 3:
            continue
        for m in new_members:
            out[m] = suffix
            used.add(m)
    return out


def _numeric_shard_matrix_legs(
    jobs_per_run: list[list[dict[str, Any]]],
) -> set[str]:
    """Job NAMES that are legs of a NUMERIC `(i, N)` shard matrix — the render of a
    classic shard axis (`matrix: { shard: [1..N] }`), e.g. `Unit Tests: Internal (2, 8)`
    … `(8, 8)`: a constant shard-count `N` and a varying shard-index `i ∈ [1, N]`.

    OPT24's name-only `_SHARD_NAME_RE` heuristic only sees a LITERAL `shard`/`partition`
    token, and `_detect_common_suffix_matrices` only catches PREFIX-varying matrices —
    so a numeric `(i, N)` shard leg slips past both, and OPT24 collapses the legs to their
    base and falsely reports 'no shard axis observed', contradicting OPT25 (which groups
    the same legs by `_matrix_base_name` and DOES see the matrix) on the same base + runs.
    Recognising the numeric shard axis here lets OPT24 defer such a base to OPT25, exactly
    as it already does for prefix-varying matrix legs.

    Sound by construction — no false suppression of a version/OS matrix (`(22.x)` /
    `(24.x)`) or a config matrix (`(3.12, redis)` / `(3.12, memory)`): a base qualifies
    ONLY when its legs share a CONSTANT integer position `N >= 2` (the shard count, `>=`
    the number of distinct legs seen — sampling may miss legs) AND a DIFFERENT position
    whose values are integers that all fall in `[1, N]` and vary (the shard index). A
    version string (`22.x`), a float axis (`3.12`), a non-numeric axis (`redis`), or an
    index outside `[1, N]` fails the test, so those matrices keep their OPT24 treatment.

    LIMITATION (conservative, by design): a shard axis CROSSED with another axis is only
    recognised when the sampled legs stayed within the shard count (`N >= len(legs)`); a
    FULLY-sampled cross (e.g. shard×OS, 16 legs, `N == 8 < 16`) fails the `n >= len(legs)`
    guard and is NOT deferred to OPT25 — OPT24 keeps its base treatment there, no worse
    than before this fix (no false suppression; the common sampled-subset case is handled).
    """
    legs_by_base: dict[str, set[str]] = {}
    for run_jobs in jobs_per_run:
        for j in run_jobs:
            name = str(j.get("name", "")).strip()
            if not name:
                continue
            base = _matrix_base_name(name)
            if base != name:  # has a trailing (variant) parenthetical
                legs_by_base.setdefault(base, set()).add(name)

    def _to_int(tok: str) -> "int | None":
        return int(tok) if _re.fullmatch(r"\d+", tok) else None

    out: set[str] = set()
    for base, legs in legs_by_base.items():
        if len(legs) < 2:
            continue
        # Parse each leg's trailing parens into its comma-/slash-separated components.
        parsed: list[list[str]] = []
        ragged = False
        for leg in sorted(legs):
            m = _MATRIX_PARENS_RE.search(leg)
            if not m:
                ragged = True
                break
            inner = m.group(0).strip()[1:-1]  # drop the surrounding ()
            parsed.append([tok.strip() for tok in _re.split(r"[,/]", inner)])
        if ragged or not parsed:
            continue
        ncols = len(parsed[0])
        # An `(i, N)` shard needs at least TWO components (index + count); a single-axis
        # `(i)` is genuinely ambiguous (could be a version/OS axis), so leave it to the
        # YAML-derived `sharded_bases` path rather than guess here.
        if ncols < 2 or any(len(p) != ncols for p in parsed):
            continue
        cols = [[_to_int(p[c]) for p in parsed] for c in range(ncols)]
        # A constant integer column == the shard COUNT N; a different column of varying
        # shard INDICES, each an integer in [1, N], is the shard axis.
        for c, col in enumerate(cols):
            vals = set(col)
            if None in vals or len(vals) != 1:
                continue
            n = next(iter(vals))
            if n < 2 or n < len(legs):
                continue
            for v, vcol in enumerate(cols):
                if v == c or None in vcol:
                    continue
                if len(set(vcol)) >= 2 and all(1 <= i <= n for i in vcol):
                    out.update(legs)
                    break
            if legs <= out:
                break
    return out


def _job_dur_url(j: dict[str, Any]) -> tuple[float | None, str]:
    """(duration_seconds, job-log URL) for a sampled job. The GitHub job object
    carries `html_url` (a permalink to that job's log inside its run) — that is
    the verifiable evidence for a timing finding, far more useful than a
    workflow file:line."""
    d = _duration_s(j.get("started_at"), j.get("completed_at"))
    return d, str(j.get("html_url") or "")


def _link(seconds: float, url: str) -> str:
    """`[12s](job-url)` if we have a URL to cite, else plain `12s`."""
    return f"[{seconds:.0f}s]({url})" if url else f"{seconds:.0f}s"


def _measured_evidence(headers: list[str], rows: list[list[str]],
                       summary: str, note: str = "") -> dict[str, Any]:
    return {"summary": summary, "table": {"headers": headers, "rows": rows},
            "note": note}


def _new_finding(pat: str, severity: str, title: str, wf_path: str,
                 job_name: str, evidence: str, fix_strategy: str,
                 anchor: str, finding_idx: int,
                 wc_p50: float | None = None, rm: float | None = None,
                 size_note: str = "", realization: str = "tail",
                 measured_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "id": f"f{finding_idx}",
        "pattern": pat,
        "pattern_class": "data-driven",
        "severity": severity,
        "title": title,
        "workflow_file": wf_path,
        "line": 0,
        "affected_jobs": [job_name] if job_name else [],
        "workflow_activity": {},
        "evidence": evidence,
        # The validator requires data-driven findings to carry a measured
        # signal — the concise quantitative claim sourced from sampled runs.
        # Mirror the evidence text since it IS the measured claim.
        "measured_signal": evidence,
        "fix_strategy": fix_strategy,
        "fix_recipe_anchor": anchor,
        "wall_clock_p50_s": wc_p50,
        "runner_min_saving": rm,
        "tier": 2,
        "realization": realization,
        "size_note": size_note,
    }
    # Structured, run-cited proof of the measured claim — a table of per-entity
    # durations plus links to the actual GitHub job logs. A data-driven finding
    # is proven by run TIMINGS, not by a workflow file:line, so the report
    # renders THIS instead of a (meaningless) code permalink.
    if measured_evidence is not None:
        out["measured_evidence"] = measured_evidence
    return out


# =============================================================================
# Structural / critical-path findings — a SECOND class of finding that is NOT
# catalog-bound to a declarative YAML match. Routed from the measured critical
# path: decompose the long pole, cross-reference required checks, find shared /
# redundant work. They carry a catalog OPT-id (OPT70-OPT75, class: structural)
# so the report and tests stay catalog-keyed, but the catalog is no longer the
# only thing that can produce a finding.
#
# Every structural pattern carries a RISK rating + a mandatory GUARDRAIL + a
# conservative ROLLOUT. Structural changes are far riskier than hygiene and
# several can degrade CORRECTNESS (not just performance) — so the report ranks
# on savings AND risk as separate axes and NEVER presents one as a safe quick
# win. The per-pattern metadata below is the structured (report-renderable) form
# of each catalog body's risk profile; the catalog body is the long form.
# =============================================================================

def _struct_anchor(opt_id: str, title: str) -> str:
    raw = f"{opt_id.lower()}--{title.lower()}"
    raw = _re.sub(r"[^\w\s-]", "", raw)
    return _re.sub(r"\s", "-", raw)


_STRUCTURAL_META: dict[str, dict[str, Any]] = {
    "OPT70": {
        "severity": "HIGH", "risk": "HIGH",
        "title": "Scope the long-pole build/test to changed targets",
        "heading": "Scope the Build/Test to Only What Changed",
        "fix_strategy": "scope-build-test-to-changed",
        "failure_mode": (
            "scoping to \"only what changed\" can MISS an undeclared/transitive "
            "dependency, silently DROP coverage (a speedup that is a regression), "
            "turn a build/import error into a FALSE PASS in an exit-code gate, and "
            "DIVERGE from the shipped artifact (unbuilt source vs dist)"),
        "guardrail": (
            "MANDATORY: run the full build/suite as a fallback on any resolution "
            "error or empty-affected-set; keep a build-error exit distinct from a "
            "test-failure exit; output-diff scoped vs full before adoption"),
        "rollout": (
            "run scoped + full in parallel for N runs (>=1-2 weeks of PRs) and "
            "compare pass/fail + coverage before cutting over; keep the full job "
            "on main/merge-queue after cutover"),
    },
    "OPT71": {
        "severity": "HIGH", "risk": "MEDIUM",
        # NEUTRAL framing: the detector only knows the check is expensive + not in
        # the required set. Whether the remedy is "de-scope" depends on whether
        # the check is genuinely ADVISORY (a label/comment/preview) or a real
        # test/build VERIFICATION gate that merely isn't branch-protected - which
        # the router can't tell apart (a demo build and a build-validation gate
        # are both "build"-dominant). So the title must NOT presuppose de-scoping;
        # the user's agent (via the prompt) decides between de-scope / gate / speed-it-up.
        "title": "Expensive non-required critical-path check (de-scope, gate, or speed up)",
        "heading": "Expensive Non-Required Check on the Critical Path",
        "fix_strategy": "expensive-non-required-check",
        "failure_mode": (
            "if the check is a real test/build VERIFICATION gate (not an advisory "
            "label/comment/preview), de-scoping it drops genuine correctness signal "
            "- the remedy is to speed it up, not turn it off; and de-scoping a "
            "check whose required-status is UNKNOWN could strand a real gate"),
        "guardrail": (
            "first classify the check: only an ADVISORY check (feeds a label / "
            "comment / preview, nothing needs: it) may be de-scoped or trigger-"
            "narrowed; a real test/build gate must be sped up instead, never "
            "turned off; never de-scope a check whose required-status is unknown"),
        "rollout": (
            "if de-scoping: narrow the trigger on a branch, watch a week of PRs, "
            "confirm no one re-requests the dropped signal; if speeding up: the "
            "cache/scope lever's rollout"),
    },
    "OPT72": {
        "severity": "MEDIUM", "risk": "MEDIUM",
        "title": "Long pole spends most of its time on setup, not the work",
        "heading": "Redundant-Work Ratio (build/install >> payload)",
        "fix_strategy": "redundant-work-ratio",
        "failure_mode": (
            "the cache path is safe but a warm-cache claim is unproven on "
            "ephemeral runners; the SCOPE path (only build what the payload needs) "
            "inherits OPT70's correctness exposure — a missed transitive dep tests "
            "against a stale build"),
        "guardrail": (
            "prefer warming the cache (removes the cost of the redundancy, not the "
            "redundancy itself — correctness unchanged); if scoping the setup "
            "instead, carry OPT70's full-build fallback + output diff"),
        "rollout": (
            "cache path: measure warm-vs-cold step time over 5 PRs; scope path: "
            "OPT70's parallel-run rollout"),
    },
    "OPT73": {
        "severity": "HIGH", "risk": "LOW",
        "title": "Shared step recurs across the cluster — fix once, lower the floor",
        "heading": "Shared Sub-Step Across Critical-Path Jobs (cluster-floor lever)",
        "fix_strategy": "shared-substep-floor",
        "failure_mode": (
            "low — caching a shared setup step changes no test/build semantics; "
            "escalates only if the shared step is a build whose cache could serve "
            "stale outputs"),
        "guardrail": (
            "make each parallel copy cheap (shared warm cache / prebuilt base "
            "image) — do NOT consolidate into one upstream job the others needs: "
            "(that adds a serial gate, OPT14/wall-clock-negative); verify the "
            "cache key captures the step's real inputs so a hit never serves stale "
            "artifacts"),
        "rollout": (
            "ship the shared cache, measure the cluster FLOOR (second-tallest job "
            "p50) before/after across 5 PRs — the win shows only when the whole "
            "cluster comes down"),
    },
    "OPT74": {
        "severity": "MEDIUM", "risk": "MEDIUM",
        "title": "Untrusted fork-PR job redoes cold work it can't cache securely",
        "heading": "Trust-Boundary-Forced Cold Work (producer/consumer split)",
        "fix_strategy": "trust-boundary-cache-split",
        "failure_mode": (
            "security-sensitive: a careless split (untrusted job WRITING the shared "
            "cache, or a trusted job CONSUMING fork-produced artifacts) is a "
            "cache-poisoning / supply-chain hole worse than the slow CI it fixes"),
        "guardrail": (
            "trusted producer (post-merge/scheduled) publishes a ref-keyed "
            "artifact; untrusted consumer restores READ-ONLY with a cold-build "
            "fallback; never let an untrusted job write a cache the trusted side "
            "reads; key by an input the consumer independently derives; validate "
            "restored content before use"),
        "rollout": (
            "stand up the trusted producer first; confirm the consumer restores "
            "warm on same-base PRs and falls back cold on a forced miss"),
    },
    "OPT75": {
        "severity": "HIGH", "risk": "MEDIUM",
        # NEUTRAL framing: OPT75 is the catch-all for any non-build long pole, so
        # its title must fit whatever remedy the agent lands on — shard /
        # parallelize a test, cache an install, OR relocate a fileless scan off the
        # PR path. It must NOT presuppose "decompose/split" (wrong for a fileless
        # CodeQL check you can only relocate) or "scope/drop" (wrong + dangerous for
        # a test whose real fix is parallelism). The agent (via the prompt) names
        # the specific remedy; the title only promises the dominant step is addressable.
        "title": "The long pole's time is one addressable step — speed it up or move it off the PR path",
        "heading": "Long Pole: Optimize or Relocate the Dominant Step",
        "fix_strategy": "decompose-inherent-cost-pole",
        "failure_mode": (
            "the dominant-step remedy ranges from LOW (cache an install) to HIGH "
            "(scope a test/build, inheriting OPT70) — the candidate carries the "
            "risk of whichever specific lever its dominant category routes to"),
        "guardrail": (
            "carry the guardrail of the routed lever (e.g. OPT70's full-suite "
            "fallback if the dominant step is a test being scoped); never present "
            "the decomposition as free"),
        "rollout": (
            "the routed lever's rollout; re-measure the pole's p50 after the "
            "dominant step is attacked — the next-largest step becomes the target"),
    },
}

# Per-step warm/irreducible floor by category — the conservative cost a cache /
# scope leaves behind. Used to bound a structural saving's RAW estimate before
# the cross-workflow cascade floors it further. (test has no fixed warm floor —
# scoping a suite is sized by a conservative scopeable FRACTION instead.)
_STRUCT_WARM_FLOOR_S: dict[str, float] = {
    "checkout": 30.0, "install": 5.0, "setup": 5.0,
    "build": 15.0, "scan": 15.0, "package": 10.0,
}
# Conservative share of a test suite that scoping-to-changed can remove on the
# AVERAGE PR. Sizing assumes scoping cuts at most this fraction of the dominant
# test step — never the single best-case PR. Flagged in the size_note.
_STRUCT_TEST_SCOPE_FRACTION = 0.5


def _new_structural_finding(
    pat: str, wf_path: str, job_name: str, finding_idx: int,
    evidence: str, measured_evidence: dict[str, Any] | None,
    size_note: str, decomp: dict[str, Any] | None = None,
    required_status: str | None = None,
    affected_jobs: list[str] | None = None,
) -> dict[str, Any]:
    """Build a STRUCTURAL finding (pattern_class: structural). Carries the risk
    axis (risk + guardrail + rollout + failure_mode) so the report can rank on
    savings AND risk and never label it a quick win.

    Sizing is filled in AFTER construction: wall_clock_p50_s by the floor-capped
    cascade (size_wall_clock) for OPT70/72/75, or directly by credit_detrigger
    for OPT71 (which bypasses the cascade). runner_min_saving stays None for
    these per-check levers — the runner-minute saving of "scope the build to
    changed targets" depends on how much is scoped out, which we can't size
    conservatively from run history, so we leave it unstated rather than show an
    unjustifiable number. It is set only for the shared-sub-step lever (OPT73),
    where the per-copy step time × job count is measured."""
    meta = _STRUCTURAL_META[pat]
    out: dict[str, Any] = {
        "id": f"f{finding_idx}",
        "pattern": pat,
        "pattern_class": "structural",
        "severity": meta["severity"],
        "title": meta["title"],
        "workflow_file": wf_path,
        "line": 0,
        "affected_jobs": affected_jobs or ([job_name] if job_name else []),
        "workflow_activity": {},
        "evidence": evidence,
        "measured_signal": evidence,
        "fix_strategy": meta["fix_strategy"],
        "fix_recipe_anchor": _struct_anchor(pat, meta["heading"]),
        "wall_clock_p50_s": None,
        "runner_min_saving": None,
        "tier": 1,
        "realization": "direct",
        "size_note": size_note,
        # --- the risk axis (non-negotiable for structural findings) ---
        "structural": True,
        "risk": meta["risk"],
        "guardrail": meta["guardrail"],
        "rollout": meta["rollout"],
        "failure_mode": meta["failure_mode"],
    }
    if required_status:
        out["required_status"] = required_status
    if decomp is not None:
        out["decomposition"] = {
            "dominant_step": decomp["dominant_step"],
            "dominant_category": decomp["dominant_category"],
            "dominant_p50_s": decomp["dominant_p50"],
            "dominant_share": decomp["dominant_share"],
            "redundant_ratio": decomp["redundant_ratio"],
            "job_p50_s": decomp["job_p50"],
        }
    if measured_evidence is not None:
        out["measured_evidence"] = measured_evidence
    return out


def _detect_opt25_shard_imbalance(
    wf_path: str, jobs_per_run: list[list[dict[str, Any]]], start_idx: int,
    crit: dict[str, Any],
) -> list[dict[str, Any]]:
    """Shard durations per matrix base name across runs; flag if max/min > 3
    (or > 2 if the slow shard sits on the workflow's long pole). Catalog body
    is OPT25. `crit` (the workflow's critical path) caps the rebalance/split saving
    against the within-workflow floor: rebalancing a matrix that ISN'T the workflow's
    long pole (a faster matrix beside a slower `build`) saves ~0 actual wall-clock —
    the slower job still gates — so the raw `wc` is run through `_cap_wall_clock`."""
    long_pole = (crit or {}).get("long_pole_p50", 0.0)
    # Matrix-base by job-name. Tries (1) the trailing-parens convention, then
    # (2) the prefix-varying convention (better-auth's adapter integration
    # tests use the latter, see _detect_common_suffix_matrices). We also record
    # whether the matrix is a HOMOGENEOUS sharded suite (an explicit
    # `shard`/`partition` axis — legs are interchangeable, so rebalancing
    # distribution is the fix) or a HETEROGENEOUS matrix of distinct legs
    # (different packages / configs doing genuinely different work — you can't
    # rebalance across them; the slow leg must be split itself).
    suffix_base = _detect_common_suffix_matrices(jobs_per_run)
    by_base: dict[str, dict[str, list[tuple[float, str]]]] = {}
    base_sharded: dict[str, bool] = {}
    for run_jobs in jobs_per_run:
        for j in run_jobs:
            name = str(j.get("name", ""))
            mbase = _matrix_base_name(name)
            if mbase != name:
                base = mbase
                variant = name[len(mbase):].strip().strip("()")
                sharded = bool(_SHARD_NAME_RE.search(variant))  # explicit shard/partition axis
            elif name in suffix_base:
                base = suffix_base[name]
                sharded = False  # prefix varies → distinct packages, not shards
            else:
                continue  # neither convention matched → not a matrix job
            d, url = _job_dur_url(j)
            if d is None or d <= 0:
                continue
            by_base.setdefault(base, {}).setdefault(name, []).append((d, url))
            # A base counts as sharded only if EVERY member looks like a shard.
            base_sharded[base] = sharded if base not in base_sharded else (
                base_sharded[base] and sharded)
    out: list[dict[str, Any]] = []
    idx = start_idx
    for base, shards in by_base.items():
        if len(shards) < 2:
            continue
        medians = {n: _stats.median([d for d, _ in ds]) for n, ds in shards.items()}
        ranked = sorted(medians, key=medians.get, reverse=True)
        slow_name, fast_name = ranked[0], ranked[-1]
        slow, fast = medians[slow_name], medians[fast_name]
        second = medians[ranked[1]]  # next-slowest leg = the new pole after a fix
        if fast <= 0:
            continue
        ratio = slow / fast
        # Threshold from catalog: >3x normally, >2x if slow shard is the long pole.
        thresh = 2.0 if slow >= 0.9 * long_pole else 3.0
        if ratio <= thresh:
            continue
        idx += 1
        is_sharded = base_sharded.get(base, False)
        n_samples = sum(len(v) for v in shards.values())
        # Build the run-cited evidence table: every leg, slowest→fastest, each
        # linking to the actual job log of its worst observed run.
        rows = []
        for leg in ranked:
            samples = shards[leg]
            worst_d, worst_url = max(samples, key=lambda du: du[0])
            shown = f"[{worst_d:.0f}s]({worst_url})" if worst_url else f"{worst_d:.0f}s"
            rows.append([f"`{leg}`", f"{medians[leg]:.0f}s", str(len(samples)), shown])
        leg_word = "shard" if is_sharded else "leg"
        if is_sharded:
            # Homogeneous suite: tests are interchangeable, so rebalance toward
            # the mean (achievable by redistributing test files / timing-split).
            balanced = sum(medians.values()) / len(medians)
            wc = round(max(slow - balanced, 0.0), 1)
            wc, cap_note = _cap_wall_clock(wc, slow_name, crit)
            title = "Shard Imbalance"
            summary = (f"Across {n_samples} sampled `{base}` runs, the slowest "
                       f"shard (`{slow_name}`, median {slow:.0f}s) is {ratio:.1f}× "
                       f"the fastest (`{fast_name}`, {fast:.0f}s). The matrix's "
                       f"wall-clock is bounded by the slowest shard.")
            note = ("Each link opens that shard's worst observed run. The legs are "
                    "interchangeable shards of one suite, so rebalance the test "
                    "distribution (timing-based splitting, e.g. pytest-split / "
                    "nextest timing data) or raise the shard count — reclaims "
                    f"~{wc:.0f}s of critical-path wall-clock.")
            size_note = f"rebalance shard distribution — saves ~{wc:.0f}s wall-clock per run"
        else:
            # Heterogeneous matrix: legs do different work and can't be
            # rebalanced. Splitting the slow leg in two floors at the
            # NEXT-slowest leg, which becomes the new long pole.
            wc = round(max(slow - max(slow / 2.0, second), 0.0), 1)
            wc, cap_note = _cap_wall_clock(wc, slow_name, crit)
            title = "Matrix Leg Imbalance"
            summary = (f"Across {n_samples} sampled `{base}` runs, the slowest leg "
                       f"(`{slow_name}`, median {slow:.0f}s) is {ratio:.1f}× the "
                       f"fastest (`{fast_name}`, {fast:.0f}s) and gates the matrix. "
                       f"These legs do DIFFERENT work, so they cannot be "
                       f"rebalanced — the slow leg is the long pole.")
            note = (f"Each link opens that leg's worst observed run. Fix: SPLIT the "
                    f"slowest leg `{slow_name}` itself (sub-shard its suite, or "
                    f"split its backends into parallel jobs) — not rebalance across "
                    f"legs. That floors at the next-slowest leg `{ranked[1]}` "
                    f"({second:.0f}s), which becomes the new pole, so the realistic "
                    f"first-step saving is ~{wc:.0f}s; split that leg next to go "
                    f"lower.")
            size_note = (f"split the slow leg `{slow_name}`; saving (~{wc:.0f}s) is "
                         f"bounded by the next-slowest leg ({second:.0f}s)")
        me = {
            "summary": summary,
            "table": {"headers": [leg_word.capitalize(), "Median", "Samples",
                                  "Slowest run (job log)"], "rows": rows},
            "note": note,
        }
        # The within-workflow floor cap (a matrix that isn't the workflow long pole saves
        # ~0 wall-clock) is disclosed in the size_note; a zeroed saving renders with
        # `realization="none"` (below) so it isn't credited as a wall-clock win.
        if cap_note:
            size_note = f"{size_note}; {cap_note}"
        out.append(_new_finding(
            "OPT25", "MEDIUM", title, wf_path, base,
            f"{leg_word} `{slow_name}` median {slow:.0f}s vs `{fast_name}` "
            f"{fast:.0f}s — {ratio:.1f}× imbalance over {n_samples} sampled runs"
            + ("" if is_sharded else " (heterogeneous legs — split the slow leg, "
               "don't rebalance)"),
            "shard-imbalance", "opt25--shard-imbalance", idx,
            wc_p50=wc, rm=None, size_note=size_note,
            realization=("direct" if wc > 0 else "none"), measured_evidence=me,
        ))
    return out


def _detect_opt43_queue_time(
    wf_path: str, jobs_per_run: list[list[dict[str, Any]]], start_idx: int,
    is_pr_workflow: bool,
    all_status_runs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """P90 wait-to-start per job across runs; flag >60s PR / >120s release. Catalog body OPT43.

    Wait = job `started_at` − the RUN's `created_at` (the trigger), tagged onto each job
    as `_run_created_at` by `_accumulate_jobs`. Anchoring on the run's created_at — not
    the job's — is the load-bearing choice: GitHub stamps a GATED job's own `created_at`
    when its `needs:` dependency RESOLVES, so `started − job.created` sees only this
    job's own runner pickup and HIDES everything the developer waited on upstream — the
    gating job(s)' queue AND run time (~4m vs an ~18m real wait-to-start on one measured
    repo). The run trigger captures that full pre-start wait the developer experiences.
    CAVEAT: for a gated job this number therefore includes the
    gating job's RUN time, so it is wall-clock time-to-start, not pure queue — the note
    + size_note say so, and the realized saving is bounded by the gating job's own fix.
    Falls back to the job's `created_at` only when the run trigger time is absent (older
    data / direct unit calls)."""
    # Run windows for the sibling-overlap attribution below: a queued job
    # whose wait overlapped ANOTHER in-flight run of the SAME workflow is
    # plausibly self-inflicted (a concurrency group would have prevented the
    # race — config-controllable); a wait with no sibling in flight came from
    # outside this workflow. The windows MUST come from the ALL-STATUS run
    # slice: the runs that block a queue are often the cancelled/superseded
    # ones, which never enter the success-only job sample — computing overlap
    # over successes alone structurally exonerates the guilty runs and turns
    # self-inflicted queueing into a "provider congestion" PASS. With no
    # all-status slice there is NO attribution (None), never a confident one.
    run_windows: list[tuple[str, Any, Any]] = []
    for r in all_status_runs or []:
        w_start = _parse_dt(r.get("run_started_at") or r.get("created_at"))
        w_end = _parse_dt(r.get("updated_at"))
        if w_start and w_end and w_end > w_start:
            run_windows.append((_run_id(r), w_start, w_end))

    by_job: dict[str, list[tuple[float, str, int]]] = {}
    for run_idx, run_jobs in enumerate(jobs_per_run):
        for j in run_jobs:
            name = str(j.get("name", ""))
            q = _duration_s(j.get("_run_created_at") or j.get("created_at"),
                            j.get("started_at"))
            if q is not None and q >= 0:
                by_job.setdefault(name, []).append(
                    (q, str(j.get("html_url") or ""), run_idx))

    def _sibling_overlap_ratio(samples: list[tuple[float, str, int]]) -> float | None:
        """Share of queued samples whose queue interval overlapped an in-flight
        sibling run of this workflow (windows from the ALL-STATUS slice; a
        sibling is any OTHER run, so the sampled run's own window is excluded
        by identity of its start). None when nothing is computable — and None
        MUST stay None downstream: no attribution is never an exoneration."""
        if not run_windows:
            return None
        checked = overlapped = 0
        for _q, _url, run_idx in samples:
            jobs = jobs_per_run[run_idx]
            # Self-exclusion is by RUN ID, never by timestamp equality: the
            # window starts at run_started_at while the queue interval starts
            # at created_at, so those two are never equal and a timestamp
            # guard leaves the run's own window in the sibling set — every
            # multi-job run then "overlaps itself" and self-blame inflates.
            own_ids = {str(j.get("run_id")) for j in jobs if j.get("run_id") is not None}
            if not own_ids:
                continue  # can't identify the run: unattributable, not a vote
            q_starts = [_parse_dt(j.get("_run_created_at") or j.get("created_at"))
                        for j in jobs]
            q_ends = [_parse_dt(j.get("started_at")) for j in jobs]
            q_start = min((s for s in q_starts if s), default=None)
            q_end = max((e for e in q_ends if e), default=None)
            if not (q_start and q_end) or q_end <= q_start:
                continue
            checked += 1
            for rid, w_start, w_end in run_windows:
                if rid in own_ids:
                    continue  # the sampled run's own window
                if w_start < q_end and w_end > q_start:
                    overlapped += 1
                    break
        return (overlapped / checked) if checked else None
    threshold = 60.0 if is_pr_workflow else 120.0
    out: list[dict[str, Any]] = []
    idx = start_idx
    for job_name, samples in by_job.items():
        if len(samples) < 3:
            continue
        qs = [q for q, _url, _ri in samples]
        p90 = _percentile(qs, 90)
        if p90 > threshold:
            idx += 1
            worst_q, worst_url, _worst_ri = max(samples, key=lambda qu: qu[0])
            kind = "PR" if is_pr_workflow else "release/scheduled"
            overlap = _sibling_overlap_ratio(samples)
            if overlap is None:
                cause, cause_note = None, None
            elif overlap >= 0.5:
                cause = "config_controllable"
                cause_note = (f"{overlap:.0%} of queued samples overlapped an "
                              "in-flight sibling run of this workflow — a "
                              "concurrency group would have prevented the race")
            elif overlap == 0.0:
                cause = "provider_congestion"
                cause_note = ("no queued sample overlapped a sibling run of this "
                              "workflow — the wait came from outside it (account-"
                              "level concurrency cap / other workflows' load); no "
                              "change to this workflow's YAML would remove it")
            else:
                cause = "unknown"
                cause_note = (f"mixed evidence ({overlap:.0%} sibling overlap) — "
                              "cause not attributable either way")
            me = _measured_evidence(
                ["Job", "P90 queue", "Samples", "Worst queued run (job log)"],
                [[f"`{job_name}`", f"{p90:.0f}s", str(len(samples)),
                  _link(worst_q, worst_url)]],
                summary=(f"job `{job_name}` waited P90 {p90:.0f}s in queue over "
                         f"{len(samples)} sampled runs (threshold {threshold:.0f}s "
                         f"for {kind} workflows)."),
                note="Wait-to-start (run trigger → job started) is developer wait "
                     "before this job runs. For a job gated by `needs:` it spans the "
                     "gating job(s)' own queue AND run time — so the savable portion is "
                     "whatever the gating job's own fix removes, not necessarily the "
                     "whole number. The link opens the worst-queued run.")
            f = _new_finding(
                "OPT43", "MEDIUM", "Excessive Queue Time", wf_path, job_name,
                f"job `{job_name}` P90 queue {p90:.0f}s over {len(samples)} runs "
                f"(threshold {threshold:.0f}s for {kind} workflows)",
                "excessive-queue-time", "opt43--excessive-queue-time", idx,
                wc_p50=round(p90, 1), rm=None,
                size_note="wall-clock wait before the job starts (a gated job's "
                          "number includes the gating job(s) it waits on)",
                realization="direct", measured_evidence=me,
            )
            # Structured cause attribution (config-controllable causes vs
            # provider-side congestion, which displays but is never charged).
            # None = interval data unavailable.
            if cause is not None:
                f["queue_cause"] = cause
                f["queue_cause_note"] = cause_note
            out.append(f)
    return out


def _detect_opt48_failure_rate(
    client: GhClient, repo: str, wf_id: int, wf_path: str,
    long_pole: float, monthly_volume: int | None, start_idx: int,
    created_before: str | None = None,
) -> list[dict[str, Any]]:
    """Failure rate over 30d via `runs?status=failure|success`. Catalog body OPT48."""
    if monthly_volume is None or monthly_volume < 100:
        return []  # catalog: ignore low-volume workflows
    fail_doc = client.json(_status_count_endpoint(repo, wf_id, "failure", created_before))
    succ_doc = client.json(_status_count_endpoint(repo, wf_id, "success", created_before))
    if not isinstance(fail_doc, dict) or not isinstance(succ_doc, dict):
        return []
    fails = fail_doc.get("total_count") or 0
    succs = succ_doc.get("total_count") or 0
    total = fails + succs
    if total < 100:
        return []
    rate = fails / total
    if rate <= 0.15:
        return []
    # A high failure rate is a RELIABILITY signal, not a CI-config optimization:
    # ci-speedup can't write the fix (that's "make the failing tests pass" — a
    # root-cause change in the tests, or recognizing a deliberate policy gate).
    # So this is emitted ADVISORY — excluded from the report and never carrying a
    # fabricated runner-minute "saving" (it stays in the findings JSON). The honest
    # remedy is "make the failing tests pass" (not a CI-config change), which is
    # why it's advisory; the evidence a human needs is the FAILURE RATE
    # itself, which lives on the GitHub Actions performance dashboard (a per-run
    # link can't show a rate) — so we link that, not a list of individual runs.
    dashboard = (f"https://github.com/{repo}/actions/metrics/performance"
                 f"?sort=failureRate%2CORDER_BY_DIRECTION_ASC")
    me = _measured_evidence(
        ["Workflow", "Failure rate (30d)", "Verify on the failure-rate dashboard"],
        [[f"`{wf_path}`", f"{rate*100:.1f}% ({fails} failed / {total} terminal runs)",
          f"[GitHub Actions → Metrics → Performance]({dashboard})"]],
        summary=(f"`{wf_path}` failed {rate*100:.1f}% of its runs over 30d "
                 f"({fails} failed / {total} terminal). This is a reliability "
                 f"signal, not a CI-config optimization — the fix is to make the "
                 f"failing tests pass reliably (or, if these are a deliberate "
                 f"policy gate, the failures are the gate working as intended)."),
        note=("Verify the rate on the linked GitHub Actions performance dashboard "
              "(sort/filter to this workflow) — that's where the failure-rate "
              "trend is shown; an individual run link can't establish a rate. "
              "Open a recent failed run from there to find the dominant failing "
              "step, which is the thing to fix."))
    f = _new_finding(
        "OPT48", "MEDIUM", "High Job-Level Failure Rate (>15% over 30d)",
        wf_path, "",
        f"workflow failed {rate*100:.1f}% of runs over 30d "
        f"({fails} failed / {total} terminal runs) — reliability signal, fix the "
        f"failing tests (not a CI-config change)",
        "high-job-level-failure-rate-15-over-30d",
        "opt48--high-job-level-failure-rate--15--over-30d-", start_idx + 1,
        wc_p50=None, rm=None,
        size_note=("reliability signal — no CI-config saving to claim; remedy is "
                   "fixing the failing tests (root-cause specific) or confirming "
                   "a deliberate policy gate"),
        realization="none", measured_evidence=me,
    )
    # Always advisory: a failure-rate signal is never a ranked optimization.
    f["advisory"] = True
    return [f]


# --- Run-elimination detectors (OPT46 superseded, OPT47 double-trigger) ------
# Both read the ALL-STATUS run-list slice (_all_status_runs) — the success-only
# sample can't see cancelled/superseded/duplicate runs — and size the wasted
# compute from the measured mean per-run job-minutes. Wave-1 of the
# runner-minutes tier (spec §7).

_COST_RUNLIST_MAX = 100  # one page of all-status runs for branch/dup grouping
_MIN_TIMED_RUNS = 3  # a per-run compute mean needs ≥3 timed runs to be stable
# Deliberately BROAD: this is the safety carve-out for run-cancellation fixes,
# so a false positive (skip a real finding) is acceptable but a false negative
# (advise cancel-in-progress on a deploy → corrupt a partial deploy/tag/publish)
# is a hazard. Covers deploy/publish/release + the common deploy-name variants
# adversarial review found slipping through (ship, cd, gh-pages, docker, image,
# registry, upload, artifact, tag, sign, notarize, provenance, goreleaser).
_RELEASE_LIKE_RE = _re.compile(
    r"(release|deploy|publish|canary|promote|rollout|\bship\b|\bcd\b|gh-?pages|"
    r"\bpages\b|docker|image|registry|upload|artifact|\btag\b|sign|notari[sz]e|"
    r"provenance|goreleaser|npm-?publish|to-?prod|production)", _re.IGNORECASE)
_OPT35_SHARD_AXIS_RE = _re.compile(r"shard|partition|chunk|group[-_]?(index|num)", _re.IGNORECASE)
_FAIL_FAST_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out"})
_GHA_DEFAULT_JOB_TIMEOUT_S = 360.0 * 60.0
_OPT57_NEAR_DEFAULT_TIMEOUT_S = 0.95 * _GHA_DEFAULT_JOB_TIMEOUT_S
_OPT57_TIMEOUT_BUFFER_S = 10.0 * 60.0
_OPT57_TIMEOUT_MULTIPLIER = 1.5
_OPT57_MIN_TIMEOUT_S = 15.0 * 60.0


def _superseded_count(runs: list[dict[str, Any]]) -> int:
    """Count runs on a branch that were genuinely SUPERSEDED — a later-created
    run STARTED before this run finished, i.e. they overlapped in time (raced).
    Sequential, non-overlapping runs (distinct commits minutes+ apart, e.g.
    default-branch pushes each testing a merged commit) count 0 — adding
    cancel-in-progress would cancel NOTHING there, so charging them as waste is
    both over-stated and unactionable. ISO-8601 timestamps compare
    lexicographically = chronologically."""
    spans: list[tuple[str, str]] = []
    for r in runs:
        start = r.get("run_started_at") or r.get("created_at")
        end = r.get("updated_at")
        if start and end and str(start) <= str(end):
            spans.append((str(start), str(end)))
    spans.sort()
    n = 0
    for i, (_start_i, end_i) in enumerate(spans):
        # superseded iff some later-STARTED run began before run i finished
        if any(spans[j][0] < end_i for j in range(i + 1, len(spans))):
            n += 1
    return n


class _RemainderTally:
    """The remainder-basis aggregates for OPT46 sizing on ONE branch.

    `cancel-in-progress` cancels a superseded run at the MOMENT its successor
    starts, so the reclaimable compute is only the portion that run would have
    burned AFTER that moment — the overlap remainder — not the whole run
    (issue #89). Compute spent BEFORE supersession is spent either way. This
    tally carries what the detector needs to size that remainder:

    - `superseded_n` mirrors `_superseded_count` (the honest COUNT, for evidence
      and `superseded_confirmed_n`);
    - `remainder_units` = Σ over superseded runs of `remainder_i / duration_i`,
      an "effective superseded count" in [0, superseded_n] — the pro-rata credit
      multiplier applied to the MEAN per-run compute (exact per-second compute is
      unknowable because a run's jobs run in parallel, so we scale the mean by
      each run's wall-clock remainder fraction);
    - `sum_remainder_s` / `sum_duration_s` are the wall-clock aggregates behind
      the disclosed `Σremainder/Σduration` ratio;
    - `skipped_missing_ts` counts runs dropped for a missing/unordered timestamp
      (they contribute nothing to the count OR the credit — a disclosed skip,
      never a crash)."""

    __slots__ = ("superseded_n", "remainder_units", "sum_remainder_s",
                 "sum_duration_s", "skipped_missing_ts")

    def __init__(self) -> None:
        self.superseded_n = 0
        self.remainder_units = 0.0
        self.sum_remainder_s = 0.0
        self.sum_duration_s = 0.0
        self.skipped_missing_ts = 0

    def add(self, other: "_RemainderTally") -> None:
        self.superseded_n += other.superseded_n
        self.remainder_units += other.remainder_units
        self.sum_remainder_s += other.sum_remainder_s
        self.sum_duration_s += other.sum_duration_s
        self.skipped_missing_ts += other.skipped_missing_ts

    @property
    def mean_remainder_fraction(self) -> float:
        """The single stamped basis ratio: `remainder_units / superseded_n` —
        the mean per-run remainder fraction, in [0, 1]. Multiplying it by
        `superseded_n × mean-per-run compute × scale` reproduces the credited
        figure EXACTLY, so both the detector and any downstream regrounding
        derive from ONE stamped number (issue #89 §3; the #19 single-door
        discipline). 0.0 when nothing was superseded."""
        return (self.remainder_units / self.superseded_n) if self.superseded_n else 0.0


def _superseded_remainder(runs: list[dict[str, Any]]) -> _RemainderTally:
    """Remainder-basis tally for one branch's runs (issue #89). Superseded
    detection is IDENTICAL to `_superseded_count` (a later-STARTED run began
    before this one finished, ISO timestamps compared lexicographically), so the
    `superseded_n` this returns always equals `_superseded_count(runs)`; it just
    ALSO measures how much of each superseded run was still reclaimable.

    For each superseded run i, `remainder_i = end_i − cancel_at`, where
    `cancel_at` is the EARLIEST later start that is strictly before `end_i` (the
    instant cancel-in-progress would have fired). By construction
    `cancel_at ∈ (start_i, end_i]`, so `remainder_i ∈ (0, duration_i]`; we clamp
    to `[0, duration_i]` anyway (remainder > duration is impossible, but a
    defensive clamp keeps a clock-skew timestamp from ever crediting more than
    the whole run)."""
    tally = _RemainderTally()
    spans: list[tuple[str, str]] = []
    for r in runs:
        start = r.get("run_started_at") or r.get("created_at")
        end = r.get("updated_at")
        if start and end and str(start) <= str(end):
            spans.append((str(start), str(end)))
        else:
            # A run missing either timestamp (or with end < start) can be neither
            # superseded nor a supersession boundary — excluded here exactly as it
            # is from `_superseded_count`'s spans, but COUNTED as a disclosed skip.
            tally.skipped_missing_ts += 1
    spans.sort()
    for i, (start_i, end_i) in enumerate(spans):
        later_starts = [spans[j][0] for j in range(i + 1, len(spans)) if spans[j][0] < end_i]
        if not later_starts:
            continue
        tally.superseded_n += 1
        cancel_at = min(later_starts)  # earliest later start < end_i
        dur = _duration_s(start_i, end_i)
        rem = _duration_s(cancel_at, end_i)
        if dur is None or dur <= 0 or rem is None:
            continue  # superseded ⇒ dur > 0; guard against a degenerate parse anyway
        rem = max(0.0, min(rem, dur))  # clamp: remainder can never exceed the run
        tally.remainder_units += rem / dur
        tally.sum_remainder_s += rem
        tally.sum_duration_s += dur
    return tally


def _run_id(run: dict[str, Any]) -> str:
    for key in ("id", "run_id", "databaseId"):
        val = run.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _as_positive_int(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _run_attempt_opt(run: dict[str, Any]) -> int | None:
    """Workflow run attempt number, or None when the run carries no usable one —
    the HONEST reading. `_run_attempt` below defaults that None to 1; a caller that
    derives the latest-attempt job set from it must not (a missing basis on the RUN
    side is exactly as undecidable as a missing basis on the JOB side, and silently
    assuming "attempt 1" would select the wrong jobs on a re-run)."""
    return _as_positive_int(run.get("run_attempt"))


def _run_attempt(run: dict[str, Any]) -> int:
    """Workflow run attempt number. Missing/unparseable means first attempt.

    The defaulting reading, for callers that only ask "is this a re-run?"
    (`_rerun_attempt_runs`) or compare against job attempts already known to exist.
    Anything DERIVING a job set from the attempt number wants `_run_attempt_opt`."""
    return _run_attempt_opt(run) or 1


def _job_run_attempt(job: dict[str, Any]) -> int | None:
    return _as_positive_int(job.get("run_attempt"))


def _rerun_attempt_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs whose current workflow attempt is not the first attempt."""
    return [r for r in runs if _run_attempt(r) > 1]


def _attempt_job_key(job: dict[str, Any]) -> str:
    """Exact GitHub job name used as the rerun cause key.

    Do not collapse matrix legs here. A retry caused by `test (1)` is not proven
    to have the same cause as one caused by `test (2)`, even though they share a
    YAML job base.
    """
    name = str(job.get("name") or "").strip()
    return name


def _job_id(job: dict[str, Any]) -> str:
    for key in ("id", "databaseId"):
        val = job.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _job_compute_s(job: dict[str, Any]) -> float:
    d = _duration_s(job.get("started_at"), job.get("completed_at"))
    return d if d is not None and d > 0 else 0.0


_ROUNDING_MIN_MATRIX_LEGS = 3
_ROUNDING_TINY_JOB_MAX_S = 60.0


def _billing_rounding_waste_min(durations_s: list[float]) -> int:
    """Exact GitHub per-job round-up waste for a candidate consolidation group.

    The returned unit is billable minutes, not runtime minutes:
    sum(ceil(job_seconds / 60)) - ceil(sum(job_seconds) / 60).
    """
    vals = [float(d) for d in durations_s if isinstance(d, (int, float)) and d > 0]
    if len(vals) < 2:
        return 0
    per_job = sum(math.ceil(d / 60.0) for d in vals)
    consolidated = math.ceil(sum(vals) / 60.0)
    return max(0, int(per_job - consolidated))


# Runner-label families with a KNOWN billed-per-minute-rounded-up rule: GitHub-hosted
# standard/larger labels and StarSling vendor labels. OPT65's whole saving IS the
# per-job round-up delta, so a label outside these families (a custom / self-hosted
# runner whose billing rule is unknowable — often not billed per-minute, or at all)
# must not be credited "measured" waste that may not exist (#104 review). Rate-FREE:
# this gates on the rounding RULE, not on any price.
_PER_MINUTE_BILLED_LABEL_RE = _re.compile(
    r"^(ubuntu-|windows-|macos-|starsling-)", _re.IGNORECASE)


def _rounding_job_runner(job_name: str, crit: dict[str, Any]) -> str | None:
    """The runner label a matrix leg resolves to — or None when unresolvable OR
    when the label's billing rule is unknown. OPT65 uses runner-label equality to
    confirm all credited legs bill on the SAME runner before consolidating their
    round-up minutes, and `_PER_MINUTE_BILLED_LABEL_RE` to confirm that runner is
    actually billed per-minute-rounded-up (GitHub-hosted / StarSling). A custom or
    self-hosted label returns None → the base is skipped, mirroring the retired
    SKU-class check's conservatism without needing a rates table."""
    labels = _resolve_job_runner(
        job_name, crit.get("job_runner") or {}, crit.get("job_p50") or {})
    if not labels or not _PER_MINUTE_BILLED_LABEL_RE.match(labels):
        return None
    return labels


def _rounding_occurrence_runner(job: dict[str, Any]) -> str | None:
    return _job_runner_label(job) or None


def _opt65_scope_event(
    crit: dict[str, Any],
    *,
    observed_events: set[str] | None = None,
    wf_doc: dict[str, Any] | None = None,
) -> str | None:
    """The event OPT65 needs a scoped 30-day volume for, or None when the workflow's
    plain monthly volume already IS that number (single-event workflow) or the scope is
    all-events. Pulled out of `_opt65_monthly_volume_for_scope` so the prefetch planner
    can ask "will this workflow issue an event-volume call?" WITHOUT issuing it — one
    predicate, so the plan and the call site can never disagree about whether the call
    happens."""
    event_scope = str(crit.get("event_scope") or "")
    if not event_scope or event_scope == "all-events":
        return None
    declared = _workflow_declared_events(_wf_on(wf_doc or {})) if wf_doc else set()
    known_events = declared or set(observed_events or ())
    if known_events and known_events <= {event_scope}:
        return None
    return event_scope


def _opt65_monthly_volume_for_scope(
    client: GhClient | None,
    repo: str | None,
    wf_id: int | None,
    crit: dict[str, Any],
    monthly_volume: int | None,
    created_before: str | None = None,
    observed_events: set[str] | None = None,
    wf_doc: dict[str, Any] | None = None,
) -> int | None:
    if _opt65_scope_event(crit, observed_events=observed_events, wf_doc=wf_doc) is None:
        return monthly_volume
    if client is not None and repo and wf_id is not None:
        return _monthly_event_volume(
            client, repo, wf_id, str(crit.get("event_scope") or ""), created_before)
    return monthly_volume


def _detect_opt65_billing_rounding_waste(
    wf_path: str,
    jobs_per_run: list[list[dict[str, Any]]],
    crit: dict[str, Any],
    monthly_volume: int | None,
    start_idx: int,
) -> list[dict[str, Any]]:
    """Billing rounding waste from tiny matrix legs (catalog OPT65) — measured.

    Credits only the exact per-job billing-minute delta from sampled job
    timestamps, and only for trailing-parenthetical matrix bases whose observed
    legs are all tiny, same-runner, and strictly below the workflow cluster floor.
    No runtime or wall-clock speedup is claimed.
    """
    if not monthly_volume or monthly_volume <= 0 or not jobs_per_run:
        return []
    floor = float(crit.get("floor_p50") or 0.0)
    job_p50 = crit.get("job_p50") or {}
    if floor <= 0 or not job_p50:
        return []

    observed_by_base: dict[str, set[str]] = {}
    for run_jobs in jobs_per_run:
        for job in run_jobs:
            name = str(job.get("name") or "").strip()
            if not name:
                continue
            base = _matrix_base_name(name)
            if base == name:
                continue
            observed_by_base.setdefault(base, set()).add(name)

    safe_bases: dict[str, dict[str, Any]] = {}
    for base, names in observed_by_base.items():
        if len(names) < _ROUNDING_MIN_MATRIX_LEGS:
            continue
        p50s = [float(job_p50.get(name) or 0.0) for name in names]
        if any(p <= 0 or p >= floor or p >= _ROUNDING_TINY_JOB_MAX_S for p in p50s):
            continue
        runners = {_rounding_job_runner(name, crit) for name in names}
        if None in runners or len(runners) != 1:
            continue
        safe_bases[base] = {"names": names, "runner": next(iter(runners))}
    if not safe_bases:
        return []

    buckets: dict[str, dict[str, Any]] = {
        base: {
            "waste_min": 0,
            "occurrences": 0,
            "rows": [],
            "samples": [],
            "credited_jobs": set(),
            "max_combined_p50_s": 0.0,
            "runner": meta["runner"],
            "unsafe": False,
        }
        for base, meta in safe_bases.items()
    }
    for run_idx, run_jobs in enumerate(jobs_per_run, 1):
        by_name = {str(j.get("name") or "").strip(): j for j in run_jobs}
        for base, meta in safe_bases.items():
            names = meta["names"]
            expected_runner = meta["runner"]
            bucket = buckets[base]
            if bucket["unsafe"]:
                continue
            durations: list[float] = []
            present_names: list[str] = []
            unsafe_run = False
            for name in sorted(names):
                job = by_name.get(name)
                if not job:
                    continue
                dur = _job_compute_s(job)
                if dur > 0:
                    actual_runner = _rounding_occurrence_runner(job)
                    if dur >= _ROUNDING_TINY_JOB_MAX_S or actual_runner != expected_runner:
                        unsafe_run = True
                        break
                    durations.append(dur)
                    present_names.append(name)
            if unsafe_run:
                bucket["unsafe"] = True
                continue
            if len(durations) < _ROUNDING_MIN_MATRIX_LEGS:
                continue
            combined_p50 = sum(float(job_p50.get(name) or 0.0) for name in present_names)
            if combined_p50 <= 0 or combined_p50 >= floor:
                bucket["unsafe"] = True
                continue
            waste = _billing_rounding_waste_min(durations)
            if waste <= 0:
                continue
            bucket["waste_min"] += waste
            bucket["occurrences"] += 1
            bucket["credited_jobs"].update(present_names)
            bucket["max_combined_p50_s"] = max(
                float(bucket["max_combined_p50_s"]), combined_p50)
            shown = ", ".join(f"{d:.0f}s" for d in durations[:6])
            if len(durations) > 6:
                shown += ", ..."
            bucket["rows"].append([
                f"sample {run_idx}",
                f"{len(durations)} legs",
                f"{shown}",
                f"{waste}",
            ])
            bucket["samples"].append({
                "sample": run_idx,
                "jobs": present_names,
                "durations_s": [round(d, 3) for d in durations],
                "waste_min": waste,
            })

    scale = float(monthly_volume) / float(len(jobs_per_run))
    basis = f"{monthly_volume}/30d ÷ {len(jobs_per_run)} sampled successful run(s)"
    title = "Billing Rounding Waste from Tiny Matrix Legs"
    out: list[dict[str, Any]] = []
    for base, bucket in sorted(buckets.items(),
                              key=lambda kv: (-kv[1]["waste_min"], kv[0])):
        if bucket["unsafe"]:
            continue
        sampled_waste = int(bucket["waste_min"])
        if sampled_waste <= 0:
            continue
        credited_jobs = sorted(str(j) for j in bucket["credited_jobs"] if str(j))
        if len(credited_jobs) < _ROUNDING_MIN_MATRIX_LEGS:
            continue
        combined_p50 = round(float(bucket["max_combined_p50_s"] or 0.0), 1)
        if combined_p50 <= 0 or combined_p50 >= floor:
            continue
        margin = round(floor - combined_p50, 1)
        if margin <= 0:
            continue
        credited = round(sampled_waste * scale, 1)
        if credited <= 0:
            continue
        occurrence_n = int(bucket["occurrences"])
        evidence = (
            f"{occurrence_n} sampled `{base}` matrix run(s) had exact billing-rounding "
            f"waste: sum(ceil(job_seconds/60)) - ceil(sum(job_seconds)/60) = "
            f"{sampled_waste} sampled billable minute(s), scaled to ~{credited:.0f} "
            f"billable min/mo ({basis}). Every credited leg is a tiny same-runner "
            f"matrix leg, and each credited run's combined leg p50 stays below "
            f"the {floor:.0f}s cluster floor.")
        me = _measured_evidence(
            ["Sample", "Rounded legs", "Job durations", "Rounding waste min"],
            bucket["rows"][:6],
            summary=evidence,
            note=("Measured from jobs API started_at/completed_at timestamps with the exact "
                  "per-job billing formula sum(ceil(job_seconds/60)) - "
                  "ceil(sum(job_seconds)/60). This credits billing round-up only: "
                  "job runtime is unchanged, wall_clock_p50_s is 0, and the detector "
                  "emits only when observed occurrences are tiny, same-runner, and "
                  "their combined credited leg p50 is strictly below the workflow "
                  "cluster floor."))
        f = _new_finding(
            "OPT65", "LOW", title, wf_path, base, evidence,
            "billing-rounding-waste-from-tiny-matrix-legs",
            _catalog_anchor("OPT65", title), start_idx + len(out) + 1,
            wc_p50=0.0, rm=credited,
            size_note=("billing-rounding only — consolidating these billable minutes "
                       "does not reduce job runtime; only tiny same-runner matrix legs "
                       "whose combined p50 stays below the cluster floor are credited."),
            realization="none", measured_evidence=me)
        f["affected_jobs"] = credited_jobs
        f["sizing_basis"] = "measured"
        f["measured_signal"] = (
            "exact billing rounding delta "
            "sum(ceil(job_seconds/60))-ceil(sum(job_seconds)/60) "
            f"for matrix base `{base}` ({sampled_waste} sampled billable min; "
            f"scale {scale:.3g})")
        f["rounding_waste"] = {
            "kind": "opt65_billing_rounding",
            "matrix_base": base,
            "credited_jobs": credited_jobs,
            "sampled_waste_min": sampled_waste,
            "sampled_successful_run_count": len(jobs_per_run),
            "monthly_volume": monthly_volume,
            "scale": round(scale, 6),
            "runner_min_saving": credited,
            "max_combined_leg_p50_s": combined_p50,
            "runner_label": bucket["runner"],
            "samples": bucket["samples"],
        }
        f["tier2_neutrality"] = {
            "proof": "below_cluster_floor",
            "margin_s": margin,
            "ref": (f"per_workflow_timing[wf]: matrix base `{base}` combined "
                    f"credited leg p50 {combined_p50:.1f}s below floor_p50 {floor:.1f}s"),
        }
        f["guardrail"] = (
            "Do not merge or serialize any matrix leg that can sit on the merge gate. "
            "Apply only to off-spine tiny legs, or restructure setup/runner allocation "
            "without adding a serial `needs:` stage or lowering parallelism for the "
            "gating matrix.")
        out.append(f)
    return out


def _latest_attempt_jobs(run: dict[str, Any],
                         all_jobs: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The `filter=latest` view of a run, DERIVED from its `filter=all` payload —
    or None when the payload cannot decide it (the caller then re-fetches).

    REST's `filter=latest` is a server-side filter on one field the `filter=all`
    payload already carries: each job's `run_attempt`. GitHub materializes the FULL
    job graph into every attempt — a job that a partial re-run ("Re-run failed jobs")
    did NOT re-execute is still emitted under the new attempt, with a NEW job id, the
    new `run_attempt`, and its ORIGINAL timestamps. So `filter=latest` is exactly the
    jobs stamped with the run's own attempt number, on re-run-all AND partial re-runs
    alike. Verified against the recorded `filter=all` / `filter=latest` pair for
    dbt-core run 29147972600 — a 3-attempt PARTIAL re-run — in
    `tests/fixtures/gh_recorded/` (`test_recorded_attempt_runs.py` asserts the derived
    set against the real server payload, not against a restatement of this predicate).

    Returns None — "undecidable, go ask REST" — in four cases. Never `[]`: an empty
    derived set is not a real answer for a run that HAS a latest attempt.

    1. A TRUNCATED payload (`_jobs_truncated`). `filter=all` is fetched unpaginated and
       returns jobs OLDEST-ATTEMPT-FIRST, so a big-matrix run with >100 jobs across
       attempts can have a page 1 that is ENTIRELY prior-attempt jobs. Every job on
       it still carries `run_attempt`, so the basis guard below does NOT trip — the
       derivation would return `[]` and OPT64's `_dominant_prior_failing_job` would
       silently withhold on exactly the big-matrix repos where re-run waste is most
       expensive. (Recorded: dbt-core run 29121623799 — attempt 2, 228 jobs, page 1 =
       100 jobs all from attempt 1; REST `filter=latest` returns 114.) This mirrors
       the guard `_prior_attempt_jobs` carries for the same truncation.
    2. Any job missing `run_attempt` — a partial basis would drop real latest jobs.
    3. The RUN missing `run_attempt` — the selection key itself is unknown.
    4. An empty selection: impossible for a real run, so it means the payload lied
       to us (see 1) rather than "this attempt ran nothing"."""
    if _jobs_truncated(all_jobs):
        return None
    attempts = [_job_run_attempt(j) for j in all_jobs]
    if any(a is None for a in attempts):
        return None
    latest_attempt = _run_attempt_opt(run)
    if latest_attempt is None:
        return None
    latest = [j for j, a in zip(all_jobs, attempts) if a == latest_attempt]
    return latest or None


def _attempt_job_samples(
    client: GhClient, repo: str,
    kept_all: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *, memo: "_JobFetchMemo | None" = None,
) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]], int]:
    """`(run, all_jobs, latest_jobs)` per attempt-run, from ONE `filter=all` fetch —
    plus a `filter=latest` fetch for the runs the payload can't decide.

    The `filter=latest` view is derived per run (`_latest_attempt_jobs`) whenever the
    payload can decide it, which on real corpora is nearly always: the saving is
    conditional, not unconditional. A run whose payload is UNDECIDABLE — most
    importantly a >=100-job (possibly truncated) payload, see `_latest_attempt_jobs` —
    falls back to the explicit REST fetch and pays the second call, which is the price
    of not silently deriving an empty latest set on a big-matrix run. Cost is bounded
    by the number of full-page attempt-runs (zero on the better-auth corpus).

    This also fixes a double-count: the old two-fetch path ran the fetch-failure
    accounting ONCE PER FETCH, so a single unreachable attempt-run was charged to the
    cost-spine coverage gap TWICE. Returns `(samples, fetch_failures)` — failures from
    the fallback fetch only, counted once, and a run whose fallback fetch fails is
    dropped from the samples (unknown != no prior attempt) exactly as the join did.

    Preserves `kept_all`'s input order."""
    needs_fetch = [run for run, all_jobs in kept_all
                   if _latest_attempt_jobs(run, all_jobs) is None]
    latest_by_run: dict[Any, list[dict[str, Any]]] = {}
    failures = 0
    if needs_fetch:
        logger.debug("attempt-runs: %d/%d run payloads cannot decide filter=latest "
                     "(truncated page, or no run_attempt) — falling back to an "
                     "explicit filter=latest fetch",
                     len(needs_fetch), len(kept_all))
        kept_latest, failures = _gather_run_jobs(
            client, repo, needs_fetch, fetch=_fetch_run_jobs_latest_attempt,
            keep_empty=True, memo=memo)
        latest_by_run = {_run_id(run): jobs for run, jobs in kept_latest if _run_id(run)}
    out: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for run, all_jobs in kept_all:
        latest = _latest_attempt_jobs(run, all_jobs)
        if latest is None:
            rid = _run_id(run)
            if rid not in latest_by_run:
                continue
            latest = latest_by_run[rid]
        out.append((run, all_jobs, latest))
    return out, failures


def _prior_attempt_jobs(
    run: dict[str, Any], all_jobs: list[dict[str, Any]],
    latest_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Jobs from attempts before the current latest attempt.

    TRUNCATION IS CHECKED FIRST, before any basis. An incomplete payload is UNKNOWN,
    and that is true of BOTH bases — not just the id-set-difference fallback:

      * a truncated `filter=all` page yields a prior-attempt set that is silently
        SHORT (dbt-core run 29121623799: 100 of 114 real attempt-1 jobs), so OPT64
        would size re-run waste against a partial set and `_dominant_prior_failing_job`
        could crown the WRONG dominant job — the true one being among the missing;
      * a truncated `filter=latest` page yields short `latest_keys`, so a prior failing
        job that IS present in the latest attempt looks absent and the finding is
        withheld.

    Every job on those pages carries `run_attempt`, so the explicit-attempt path below
    would happily return the short set: putting the guard AFTER it made the guard dead
    code on precisely the big-matrix runs it was written for. UNKNOWN (no jobs, no
    finding) is the honest answer; the alternative is a confident wrong number.

    With the truncation ruled out: prefer the jobs API's explicit `run_attempt` field.
    If replay/live payloads omit it, fall back to the set difference between
    `filter=all` and `filter=latest` job IDs. If neither basis exists, return no jobs
    rather than guessing and over-crediting.
    """
    if _jobs_truncated(all_jobs) or _jobs_truncated(latest_jobs):
        return []
    latest_attempt = _run_attempt(run)
    attempted = [(j, _job_run_attempt(j)) for j in all_jobs]
    if any(a is not None for _j, a in attempted):
        prior = [j for j, a in attempted if a is not None and a < latest_attempt]
        if prior:
            return prior
    latest_ids = {_job_id(j) for j in latest_jobs if _job_id(j)}
    if latest_ids:
        return [j for j in all_jobs if _job_id(j) and _job_id(j) not in latest_ids]
    return []


def _dominant_prior_failing_job(
    prior_jobs: list[dict[str, Any]], latest_jobs: list[dict[str, Any]],
) -> tuple[str, float, int] | None:
    """Unique failed/timed-out prior-attempt job that also appears latest.

    The detector is intentionally not "this workflow retries a lot". It needs an
    actionable dominant failing job, and that job must be present in the latest
    attempt to prove the prior attempt was superseded by the same run's retry.
    Equal top failing-job durations are ambiguous and withheld. When a run has
    multiple prior attempts, every explicit prior attempt must identify the same
    dominant failing job; mixed-cause reruns are withheld instead of aggregating
    them into one claim.
    """
    latest_keys = {_attempt_job_key(j) for j in latest_jobs if _attempt_job_key(j)}
    if not latest_keys:
        return None
    attempts = [_job_run_attempt(j) for j in prior_jobs]
    if any(a is not None for a in attempts):
        if any(a is None for a in attempts):
            return None
        groups = [
            [j for j in prior_jobs if _job_run_attempt(j) == attempt]
            for attempt in sorted({a for a in attempts if a is not None})
        ]
    else:
        groups = [prior_jobs]

    chosen_key = ""
    total_fail_s = 0.0
    total_fail_n = 0
    for group in groups:
        fail_s_by_key: dict[str, float] = {}
        fail_n_by_key: dict[str, int] = {}
        for job in group:
            if str(job.get("conclusion") or "").lower() not in _FAIL_FAST_FAILURE_CONCLUSIONS:
                continue
            key = _attempt_job_key(job)
            dur = _job_compute_s(job)
            if not key or dur <= 0:
                continue
            fail_s_by_key[key] = fail_s_by_key.get(key, 0.0) + dur
            fail_n_by_key[key] = fail_n_by_key.get(key, 0) + 1
        if not fail_s_by_key:
            return None
        ranked = sorted(fail_s_by_key.items(), key=lambda kv: (-kv[1], kv[0]))
        key, fail_s = ranked[0]
        if fail_s <= 0:
            return None
        if len(ranked) > 1 and abs(ranked[1][1] - fail_s) < 0.001:
            return None
        if key not in latest_keys:
            return None
        if chosen_key and key != chosen_key:
            return None
        chosen_key = key
        total_fail_s += fail_s
        total_fail_n += fail_n_by_key.get(key, 0)
    if not chosen_key:
        return None
    return chosen_key, total_fail_s, total_fail_n


def _opt35_failed_workflow_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Workflow runs worth fetching for OPT35.

    Successes cannot contain a first failed shard, so fetching their job lists is
    pure overhead. Keep the scaling denominator as the full all-status sample;
    this helper only trims API calls.
    """
    return [
        r for r in runs
        if str(r.get("conclusion") or "").lower() in _FAIL_FAST_FAILURE_CONCLUSIONS
    ]


def _opt57_scoped_workflow_runs(
    runs: list[dict[str, Any]], event_scope: str | None,
) -> list[dict[str, Any]]:
    scope = str(event_scope or "")
    if scope and scope != "all-events":
        return [r for r in runs if str(r.get("event") or "") == scope]
    return runs


def _opt57_failed_workflow_runs(
    runs: list[dict[str, Any]], event_scope: str | None,
) -> list[dict[str, Any]]:
    return _opt35_failed_workflow_runs(_opt57_scoped_workflow_runs(runs, event_scope))


def _opt57_seed_workflow_paths(
    workflow_graph: object, workflow_paths: set[str],
) -> set[str]:
    """Workflows that need run sampling solely for measured OPT57 evidence."""
    if not isinstance(workflow_graph, dict):
        return set()
    out: set[str] = set()
    for wf_path, jobs in workflow_graph.items():
        path = str(wf_path)
        if not isinstance(jobs, dict) or path not in workflow_paths:
            continue
        if any(isinstance(job, dict)
               and not job.get("timeout_minutes")
               and not job.get("matrix")
               for job in jobs.values()):
            out.add(path)
    return out


def _finding_seed_workflow_paths(
    findings: object, workflow_paths: set[str],
) -> set[str]:
    """Finding `workflow_file` values that should enter workflow coverage.

    Some static findings use `workflow_file` for the source file where the issue
    was found (for example OPT19 sleep calls under tests/). Those files should
    keep their finding-level provenance, but they are not workflow-volume
    coverage inputs for the runner-minute spine. Repo-rooted GitHub workflow
    files stay in the set even when the workflow list lookup missed them; the
    collector then records them as unknown-volume workflows instead of silently
    dropping a real coverage gap.
    """
    if not isinstance(findings, list):
        return set()
    out: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        path = str(finding.get("workflow_file") or "")
        if path in workflow_paths or (
            path.startswith(".github/workflows/") and _wf_is_file_backed(path)
        ):
            out.add(path)
    return out


def _superseded_run_ids(runs: list[dict[str, Any]]) -> list[str]:
    spans: list[tuple[str, str, str]] = []
    for r in runs:
        start = r.get("run_started_at") or r.get("created_at")
        end = r.get("updated_at")
        rid = _run_id(r)
        if start and end and rid and str(start) <= str(end):
            spans.append((str(start), str(end), rid))
    spans.sort()
    out: list[str] = []
    for i, (_start_i, end_i, rid) in enumerate(spans):
        if any(spans[j][0] < end_i for j in range(i + 1, len(spans))):
            out.append(rid)
    return out


def _catalog_anchor(opt_id: str, title: str) -> str:
    """GFM-ish heading anchor, mirroring scan._slug_anchor so a data-driven
    finding deep-links to the same catalog section a static finding would."""
    raw = f"{opt_id.lower()}--{title.lower()}"
    raw = _re.sub(r"[^\w\s-]", "", raw)
    return _re.sub(r"\s", "-", raw)


def _wf_on(wf_doc: dict[str, Any]) -> Any:
    """The workflow's `on:` config. PyYAML parses a bare `on:` key as the bool
    True, so fall back to that."""
    on = wf_doc.get("on")
    return wf_doc.get(True) if on is None else on


def _workflow_declared_events(on: Any) -> set[str]:
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(e) for e in on if e}
    if isinstance(on, dict):
        return {str(e) for e in on if e}
    return set()


def _on_has_event(on: Any, event: str) -> bool:
    if isinstance(on, str):
        return on == event
    if isinstance(on, list):
        return event in on
    if isinstance(on, dict):
        return event in on
    return False


def _concurrency_cancels(conc: Any) -> bool:
    """True when a `concurrency:` block cancels superseded runs. Conservative: a
    bare truthy scalar (`true`/`yes`/`on`, quoted or not) OR an expression is
    treated as cancelling — we don't flag when the author plausibly intended to
    cancel; only a literal false / absent leaves it eligible."""
    if not isinstance(conc, dict):
        return False
    cip = conc.get("cancel-in-progress")
    if cip is True:
        return True
    if isinstance(cip, str):
        s = cip.strip().lower()
        return s in ("true", "yes", "on") or s.startswith("${{")
    return False


def _wf_has_cancelling_concurrency(wf_doc: dict[str, Any]) -> bool:
    """True when the workflow already cancels superseded runs — at the TOP level
    OR at the JOB level. Checking only the top level (the prior bug) false-flags
    a workflow that cancels per-job. Conservative: any cancelling concurrency
    anywhere leaves it out of OPT46."""
    if _concurrency_cancels(wf_doc.get("concurrency")):
        return True
    jobs = wf_doc.get("jobs")
    if isinstance(jobs, dict):
        return any(_concurrency_cancels(j.get("concurrency"))
                   for j in jobs.values() if isinstance(j, dict))
    return False


def _wf_explicit_cancel_disabled(wf_doc: dict[str, Any]) -> bool:
    """True when a concurrency block EXISTS (top or job level) with an explicit
    `cancel-in-progress: false` — the written-down keep-superseded-runs decision.
    A missing concurrency block, or a bare group with no cancel key, is NOT
    explicit. This distinguishes a deliberate keep-superseded-runs choice from a
    plain omission, so a downstream consumer can treat the two differently
    instead of charging the deliberate decision as a defect."""
    def _explicit_false(conc: Any) -> bool:
        # EXACTLY False: a null value or a templated string expression
        # (`${{ ... }}`) is not the written keep-superseded-runs decision.
        return (isinstance(conc, dict)
                and conc.get("cancel-in-progress") is False)

    if _explicit_false(wf_doc.get("concurrency")):
        return True
    jobs = wf_doc.get("jobs")
    if isinstance(jobs, dict):
        return any(_explicit_false(j.get("concurrency"))
                   for j in jobs.values() if isinstance(j, dict))
    return False


def _wf_is_release_like(wf_path: str, wf_doc: dict[str, Any]) -> bool:
    """Release/deploy/publish workflows carve out of run-elimination fixes —
    cancelling a mid-flight deploy is unsafe (catalog OPT45/OPT46 guardrail)."""
    if _RELEASE_LIKE_RE.search(wf_path or ""):
        return True
    name = wf_doc.get("name")
    return bool(isinstance(name, str) and _RELEASE_LIKE_RE.search(name))


def _opt35_matrix_axis_keys(strategy: dict[str, Any]) -> list[str]:
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return []
    return [str(k) for k in matrix if k not in ("include", "exclude")]


def _opt35_matrix_is_shard_indexed(strategy: dict[str, Any]) -> bool:
    return any(_OPT35_SHARD_AXIS_RE.search(k) for k in _opt35_matrix_axis_keys(strategy))


def _opt35_shard_job_specs(wf_doc: dict[str, Any]) -> list[dict[str, str]]:
    jobs = wf_doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    out: list[dict[str, str]] = []
    for key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy") or {}
        if not (isinstance(strategy, dict) and strategy.get("fail-fast") is False):
            continue
        if not _opt35_matrix_is_shard_indexed(strategy):
            continue
        name = job.get("name")
        template = str(name) if isinstance(name, str) and name.strip() else str(key)
        out.append({"key": str(key), "template": template})
    return out


def _opt35_job_matches(spec: dict[str, str], job_name: str) -> bool:
    template = str(spec.get("template") or "").strip()
    key = str(spec.get("key") or "").strip()
    candidates = [c for c in (template, key) if c]
    if template and "${{" in template and _name_template_regex(template).match(job_name):
        return True
    for cand in candidates:
        if job_name == cand or job_name.startswith(cand + " ("):
            return True
        if _matrix_base_name(job_name) == cand:
            return True
    return False


def _opt57_job_matches(spec: dict[str, str], job_name: str) -> bool:
    """Exact runtime job match for non-matrix OPT57 candidates."""
    name = str(job_name or "").strip()
    template = str(spec.get("template") or "").strip()
    key = str(spec.get("key") or "").strip()
    if not name:
        return False
    if template and "${{" in template:
        return bool(_name_template_regex(template).match(name))
    return name in {c for c in (template, key) if c}


def _opt57_timeout_job_specs(wf_doc: dict[str, Any]) -> list[dict[str, str]]:
    """Workflow jobs eligible for measured OPT57.

    Absence of `timeout-minutes` is only the structural gate. The detector below
    still requires run-history evidence of near-default timeout burn before it
    credits any runner-minute saving.
    """
    jobs = wf_doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    out: list[dict[str, str]] = []
    for key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "timeout-minutes" in job:
            continue
        strategy = job.get("strategy")
        if isinstance(strategy, dict) and strategy.get("matrix"):
            # A single timeout-minutes value applies to every matrix leg. PR11
            # withholds matrix jobs until the detector can prove a safe timeout
            # across each variant instead of flattening fast and slow legs.
            continue
        name = job.get("name")
        template = str(name) if isinstance(name, str) and name.strip() else str(key)
        out.append({"key": str(key), "template": template})
    return out


def _opt57_recommended_timeout_s(p99_s: float) -> float:
    target = max(
        _OPT57_MIN_TIMEOUT_S,
        p99_s + _OPT57_TIMEOUT_BUFFER_S,
        p99_s * _OPT57_TIMEOUT_MULTIPLIER,
    )
    return float(math.ceil(target / 60.0) * 60)


def _opt57_success_durations_s(
    spec: dict[str, str], jobs_per_run: list[list[dict[str, Any]]],
) -> list[float]:
    out: list[float] = []
    for run_jobs in jobs_per_run:
        for job in run_jobs:
            name = str(job.get("name") or "").strip()
            if not name or not _opt57_job_matches(spec, name):
                continue
            conclusion = str(job.get("conclusion") or "").lower()
            if conclusion != "success":
                continue
            dur = _job_compute_s(job)
            if dur > 0:
                out.append(dur)
    return out


def _run_id_from_jobs(run_jobs: list[dict[str, Any]]) -> str:
    for j in run_jobs:
        rid = str(j.get("_run_id") or "").strip()
        if rid:
            return rid
    return ""


def _post_failure_waste_s(legs: list[dict[str, Any]]) -> tuple[float, str, list[str], str]:
    failures: list[tuple[_dt.datetime, str, dict[str, Any]]] = []
    for j in legs:
        if str(j.get("conclusion") or "").lower() not in _FAIL_FAST_FAILURE_CONCLUSIONS:
            continue
        end = _parse_dt(j.get("completed_at"))
        if end:
            failures.append((end, str(j.get("name") or ""), j))
    if not failures:
        return 0.0, "", [], ""
    fail_end, fail_name, fail_job = min(failures, key=lambda t: (t[0], t[1]))
    wasted = 0.0
    sibling_names: list[str] = []
    for j in legs:
        if j is fail_job:
            continue
        start = _parse_dt(j.get("started_at"))
        end = _parse_dt(j.get("completed_at"))
        if not (start and end and end > fail_end):
            continue
        sec = (end - max(start, fail_end)).total_seconds()
        if sec > 0:
            wasted += sec
            name = str(j.get("name") or "")
            if name:
                sibling_names.append(name)
    return wasted, fail_name, sibling_names, fail_end.isoformat()


def _detect_opt64_rerun_attempt_waste(
    wf_path: str,
    attempt_samples: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]],
    monthly_volume: int | None,
    sample_denominator: int,
    start_idx: int,
) -> list[dict[str, Any]]:
    """Repeated workflow attempts (catalog OPT64) — measured.

    GitHub's default jobs endpoint hides prior attempts. This detector compares
    `filter=all` with `filter=latest`, credits only the prior-attempt job delta,
    and emits only when a unique dominant failed/timed-out job from those prior
    attempts is still present in the latest attempt.
    """
    if not monthly_volume or monthly_volume <= 0 or sample_denominator <= 0:
        return []
    if not attempt_samples:
        return []

    by_job: dict[str, dict[str, Any]] = {}
    for run, all_jobs, latest_jobs in attempt_samples:
        latest_attempt = _run_attempt(run)
        if latest_attempt <= 1:
            continue
        prior_jobs = _prior_attempt_jobs(run, all_jobs, latest_jobs)
        if not prior_jobs:
            continue
        prior_s = sum(_job_compute_s(j) for j in prior_jobs)
        if prior_s <= 0:
            continue
        dominant = _dominant_prior_failing_job(prior_jobs, latest_jobs)
        if dominant is None:
            continue
        key, fail_s, fail_n = dominant
        attempts = sorted({
            a for a in (_job_run_attempt(j) for j in prior_jobs)
            if a is not None and a < latest_attempt
        })
        prior_attempt_count = len(attempts) if attempts else max(latest_attempt - 1, 1)
        rid = _run_id(run)
        bucket = by_job.setdefault(key, {
            "waste_s": 0.0, "failure_s": 0.0, "occurrences": 0,
            "failures": 0, "run_ids": [], "rows": [],
        })
        bucket["waste_s"] += prior_s
        bucket["failure_s"] += fail_s
        bucket["occurrences"] += 1
        bucket["failures"] += fail_n
        if rid:
            bucket["run_ids"].append(rid)
        bucket["rows"].append([
            f"`{rid}`" if rid else "`run_attempt>1`",
            str(latest_attempt),
            f"`{key}`",
            str(prior_attempt_count),
            f"{prior_s / 60.0:.1f}",
            f"{fail_s / 60.0:.1f}",
        ])

    if not by_job:
        return []

    scale = float(monthly_volume) / float(sample_denominator)
    basis = f"{monthly_volume}/30d ÷ {sample_denominator} sampled all-status run(s)"
    title = "Repeated Workflow Attempts From Same Failing Job"
    out: list[dict[str, Any]] = []
    for key, bucket in sorted(by_job.items(), key=lambda kv: (-kv[1]["waste_s"], kv[0])):
        prior_min = float(bucket["waste_s"]) / 60.0
        credited = round(prior_min * scale, 1)
        if credited <= 0:
            continue
        occurrence_n = int(bucket["occurrences"])
        failure_n = int(bucket["failures"])
        failure_min = float(bucket["failure_s"]) / 60.0
        evidence = (
            f"{occurrence_n} sampled run_attempt>1 workflow run(s) had prior-attempt "
            f"jobs present in `filter=all` but absent from `filter=latest`; the "
            f"unique dominant failing job was `{key}` ({failure_n} failed/timed-out "
            f"prior-attempt job(s), {failure_min:.1f} failed min) and it appeared "
            f"again in the latest attempt. ~{credited:.0f} runner-min/mo of "
            f"prior-attempt compute ({basis}).")
        me = _measured_evidence(
            ["Run", "Latest attempt", "Dominant failing job", "Prior attempts",
             "Prior attempt compute min", "Dominant failure min"],
            bucket["rows"][:6],
            summary=evidence,
            note=("Measured from GitHub jobs API `filter=all` minus `filter=latest` "
                  "for workflow runs whose run_attempt > 1. The finding is emitted "
                  "only when each credited prior attempt has the same unique "
                  "dominant failed/timed-out job and that dominant failing job "
                  "appears in the latest attempt; ambiguous ties, mixed-cause "
                  "attempts, and retry-only volume are withheld."))
        f = _new_finding(
            "OPT64", "LOW", title, wf_path, "", evidence,
            "repeated-workflow-attempts-from-same-failing-job",
            _catalog_anchor("OPT64", title), start_idx + len(out) + 1,
            wc_p50=0.0, rm=credited,
            size_note=("runner-minute (bill) only — prior workflow attempts are "
                       "superseded by the latest attempt's signal."),
            realization="none", measured_evidence=me)
        f["sizing_basis"] = "measured"
        f["measured_signal"] = (
            f"run_attempt>1 prior-attempt job delta from filter=all minus "
            f"filter=latest; dominant failing job `{key}` present in latest "
            f"attempt ({prior_min:.1f} sampled prior-attempt min; scale {scale:.3g})")
        f["tier2_neutrality"] = {
            "proof": "post_completion_waste",
            "margin_s": None,
            "ref": ("run_attempt>1: `filter=all` exposes prior-attempt jobs, "
                    "`filter=latest` is the superseding latest attempt; the "
                    f"dominant failing job `{key}` identifies the retry cause, so "
                    "prior-attempt compute is post-completion waste"),
        }
        f["rerun_dominant_job"] = key
        out.append(f)
    return out


def _detect_opt35_fail_fast_waste(
    wf_path: str, all_run_jobs: list[list[dict[str, Any]]], wf_doc: dict[str, Any],
    monthly_volume: int | None, start_idx: int, sample_denominator: int | None = None,
) -> list[dict[str, Any]]:
    """Missing fail-fast on shard-indexed matrices (catalog OPT35) — measured.

    Static OPT35 already proves the YAML shape is a shard/partition matrix with
    `fail-fast: false` and keeps diagnostic matrices out. This measured upgrade
    adds run-history proof: in a failed sampled run, sibling shards continued
    consuming compute after the first shard had already failed the run."""
    specs = _opt35_shard_job_specs(wf_doc)
    if not specs or not monthly_volume or monthly_volume <= 0:
        return []
    if not all_run_jobs:
        return []

    by_job: dict[str, dict[str, Any]] = {}
    for run_jobs in all_run_jobs:
        rid = _run_id_from_jobs(run_jobs)
        for spec in specs:
            legs = [j for j in run_jobs
                    if _opt35_job_matches(spec, str(j.get("name") or ""))]
            if len(legs) < 2:
                continue
            waste_s, fail_name, sibling_names, fail_ts = _post_failure_waste_s(legs)
            if waste_s <= 0:
                continue
            key = spec["key"]
            bucket = by_job.setdefault(key, {"waste_s": 0.0, "occurrences": 0,
                                             "run_ids": [], "rows": []})
            bucket["waste_s"] += waste_s
            bucket["occurrences"] += 1
            if rid:
                bucket["run_ids"].append(rid)
            bucket["rows"].append([
                f"`{key}`",
                fail_name or "first failed shard",
                str(len(sibling_names)),
                f"{waste_s / 60.0:.1f}",
                fail_ts,
            ])
    if not by_job:
        return []

    sampled_n = max(len(all_run_jobs), int(sample_denominator or 0))
    if sampled_n <= 0:
        return []
    scale = float(monthly_volume) / float(sampled_n)
    basis = f"{monthly_volume}/30d ÷ {sampled_n} sampled all-status run(s)"

    title = "Missing `fail-fast` on Non-Diagnostic Matrix Dimensions"
    out: list[dict[str, Any]] = []
    for key, bucket in sorted(by_job.items(), key=lambda kv: (-kv[1]["waste_s"], kv[0])):
        total_waste_s = float(bucket["waste_s"])
        wasted_occurrences = int(bucket["occurrences"])
        credited = round((total_waste_s / 60.0) * scale, 1)
        if credited <= 0:
            continue
        evidence = (
            f"{wasted_occurrences} sampled failed matrix occurrence(s) left shard "
            f"sibling jobs running after the first failed shard; ~{credited:.0f} "
            f"runner-min/mo of post-failure matrix compute ({basis}).")
        me = _measured_evidence(
            ["Matrix job", "First failed shard", "Sibling shards still running",
             "Post-failure min", "Failure time"],
            bucket["rows"][:6],
            summary=evidence,
            note=("Counts only shard-indexed matrices with `strategy.fail-fast: false` "
                  "(diagnostic matrices are excluded by the same structural gate as static OPT35). "
                  "Post-failure minutes are measured after the first failed/timed-out shard "
                  "completed; sibling compute after that point cannot restore the failed run."))
        f = _new_finding(
            "OPT35", "LOW", title, wf_path, key, evidence,
            "missing-fail-fast-on-non-diagnostic-matrix-dimensions",
            _catalog_anchor("OPT35", title), start_idx + len(out) + 1,
            wc_p50=0.0, rm=credited,
            size_note=("runner-minute (bill) only — failed-run sibling shards after the "
                       "first failure are post-completion waste; diagnostic matrices stay excluded."),
            realization="none", measured_evidence=me)
        f["sizing_basis"] = "measured"
        f["measured_signal"] = (
            f"fail-fast:false shard matrix post-failure sibling compute "
            f"({wasted_occurrences} failed matrix occurrence(s); "
            f"{total_waste_s / 60.0:.1f} sampled min; "
            f"scale {scale:.3g})")
        f["tier2_neutrality"] = {
            "proof": "post_completion_waste",
            "margin_s": None,
            "ref": ("fail-fast:false shard matrix: first failed shard already makes the "
                    "run fail; sibling shard compute after that failure is post-completion waste"),
        }
        if bucket["run_ids"]:
            f["tier2_sample_run_ids"] = sorted(set(bucket["run_ids"]))
        out.append(f)
    return out


def _detect_opt57_timeout_default_burn(
    wf_path: str,
    timeout_run_jobs: list[list[dict[str, Any]]],
    jobs_per_run: list[list[dict[str, Any]]],
    wf_doc: dict[str, Any],
    monthly_volume: int | None,
    start_idx: int,
    sample_denominator: int | None = None,
) -> list[dict[str, Any]]:
    """Missing `timeout-minutes` with observed near-default timeout burn (OPT57).

    Generic "job lacks timeout-minutes" YAML is a reliability smell, not a
    measured saving. This detector credits only sampled failed/timed-out jobs
    that burned near GitHub's 360 minute default and only when successful
    samples for the same workflow job support an explicit timeout above p99.
    """
    specs = _opt57_timeout_job_specs(wf_doc)
    if not specs or not timeout_run_jobs or not monthly_volume or monthly_volume <= 0:
        return []
    sampled_n = max(len(timeout_run_jobs), int(sample_denominator or 0))
    if sampled_n <= 0:
        return []

    success_by_key: dict[str, dict[str, Any]] = {}
    for spec in specs:
        durations = _opt57_success_durations_s(spec, jobs_per_run)
        if len(durations) < _MIN_TIMED_RUNS:
            continue
        p99 = _percentile(durations, 99)
        recommended_s = _opt57_recommended_timeout_s(p99)
        if recommended_s >= _OPT57_NEAR_DEFAULT_TIMEOUT_S:
            continue
        success_by_key[spec["key"]] = {
            "spec": spec,
            "durations": durations,
            "p99_s": p99,
            "recommended_s": recommended_s,
            "recommended_min": int(recommended_s / 60.0),
        }
    if not success_by_key:
        return []

    by_key: dict[str, dict[str, Any]] = {}
    for run_jobs in timeout_run_jobs:
        rid = _run_id_from_jobs(run_jobs)
        for spec in specs:
            success = success_by_key.get(spec["key"])
            if not success:
                continue
            matching = [
                j for j in run_jobs
                if _opt57_job_matches(spec, str(j.get("name") or ""))
            ]
            for job in matching:
                conclusion = str(job.get("conclusion") or "").lower()
                if conclusion not in _FAIL_FAST_FAILURE_CONCLUSIONS:
                    continue
                dur = _job_compute_s(job)
                if dur < _OPT57_NEAR_DEFAULT_TIMEOUT_S:
                    continue
                recommended_s = float(success["recommended_s"])
                waste_s = max(0.0, dur - recommended_s)
                if waste_s <= 0:
                    continue
                key = spec["key"]
                bucket = by_key.setdefault(key, {
                    "spec": spec,
                    "success": success,
                    "waste_s": 0.0,
                    "occurrences": 0,
                    "run_ids": [],
                    "rows": [],
                    "samples": [],
                })
                bucket["waste_s"] += waste_s
                bucket["occurrences"] += 1
                if rid:
                    bucket["run_ids"].append(rid)
                job_name = str(job.get("name") or key)
                bucket["rows"].append([
                    f"`{rid}`" if rid else "`failed/timed_out run`",
                    f"`{job_name}`",
                    f"{dur / 60.0:.1f}",
                    f"{float(success['p99_s']) / 60.0:.1f}",
                    str(success["recommended_min"]),
                    f"{waste_s / 60.0:.1f}",
                ])
                bucket["samples"].append({
                    "run_id": rid,
                    "job_name": job_name,
                    "conclusion": conclusion,
                    "duration_s": round(dur, 3),
                    "waste_s": round(waste_s, 3),
                })

    if not by_key:
        return []

    scale = float(monthly_volume) / float(sampled_n)
    basis = f"{monthly_volume}/30d ÷ {sampled_n} sampled all-status run(s)"
    catalog_title = "Missing `timeout-minutes` on Known-Flaky Integration Jobs"
    out: list[dict[str, Any]] = []
    for key, bucket in sorted(by_key.items(), key=lambda kv: (-kv[1]["waste_s"], kv[0])):
        total_waste_s = float(bucket["waste_s"])
        credited = round((total_waste_s / 60.0) * scale, 1)
        if credited <= 0:
            continue
        success = bucket["success"]
        occurrence_n = int(bucket["occurrences"])
        p99_s = float(success["p99_s"])
        recommended_min = int(success["recommended_min"])
        success_n = len(success["durations"])
        evidence = (
            f"{occurrence_n} sampled failed/timed-out `{key}` job occurrence(s) burned "
            f"near GitHub's 360 minute default timeout while the same job's successful "
            f"samples had p99 {p99_s / 60.0:.1f} min over {success_n} timed sample(s). "
            f"An explicit `timeout-minutes: {recommended_min}` stays above the measured "
            f"p99 and would avoid ~{credited:.0f} runner-min/mo of default-timeout "
            f"burn ({basis}).")
        me = _measured_evidence(
            ["Run", "Job", "Failed duration min", "Successful p99 min",
             "Recommended timeout min", "Default-timeout burn min"],
            bucket["rows"][:6],
            summary=evidence,
            note=("Counts only jobs whose workflow YAML lacks `timeout-minutes`, whose "
                  "failed/timed-out sampled job duration reached at least 95% of "
                  "GitHub's 360 minute default, and whose successful samples provide "
                  f"at least {_MIN_TIMED_RUNS} timed runs for a p99-backed timeout. "
                  "The recommendation is the next minute above max(p99+10m, p99*1.5, "
                  "15m), and is withheld if that approaches the 360 minute default."))
        f = _new_finding(
            "OPT57", "MEDIUM", catalog_title, wf_path, key, evidence,
            "missing-timeout-minutes-on-known-flaky-integration-jobs",
            _catalog_anchor("OPT57", catalog_title), start_idx + len(out) + 1,
            wc_p50=0.0, rm=credited,
            size_note=("runner-minute (bill) only — failed/timed-out run burn after "
                       "a p99-backed timeout cannot produce a green merge result."),
            realization="none", measured_evidence=me)
        f["sizing_basis"] = "measured"
        f["measured_signal"] = (
            f"near-default timeout burn for `{key}` "
            f"({occurrence_n} failed/timed-out occurrence(s); "
            f"{total_waste_s / 60.0:.1f} sampled min above timeout-minutes "
            f"{recommended_min}; successful p99 {p99_s:.1f}s over {success_n} "
            f"timed sample(s); scale {scale:.3g})")
        f["tier2_neutrality"] = {
            "proof": "post_completion_waste",
            "margin_s": None,
            "ref": ("failed/timed-out run: near-default timeout burn happens after "
                    "the job has exceeded a timeout-minutes value derived above "
                    "the same job's successful p99; that failed-run compute cannot "
                    "produce a green merge result"),
        }
        f["timeout_default_burn"] = {
            "kind": "opt57_timeout_default_burn",
            "job_key": key,
            "job_template": str(bucket["spec"].get("template") or key),
            "default_timeout_minutes": int(_GHA_DEFAULT_JOB_TIMEOUT_S / 60.0),
            "near_default_threshold_s": round(_OPT57_NEAR_DEFAULT_TIMEOUT_S, 3),
            "successful_duration_p99_s": p99_s,
            "successful_duration_samples": success_n,
            "recommended_timeout_minutes": recommended_min,
            "sampled_timeout_occurrences": occurrence_n,
            "sampled_timeout_burn_min": round(total_waste_s / 60.0, 1),
            "sample_denominator": sampled_n,
            "monthly_volume": monthly_volume,
            "scale": round(scale, 6),
            "runner_min_saving": credited,
            "run_ids": sorted(set(str(r) for r in bucket["run_ids"] if str(r))),
            "samples": bucket["samples"],
        }
        f["guardrail"] = (
            "Set timeout-minutes above the measured successful p99 with buffer, "
            "then re-run the workflow. Do not use this recommendation for jobs "
            "whose legitimate successful runtime is close to GitHub's default "
            "timeout or whose long tail is an expected workload.")
        out.append(f)
    return out


def _run_start_ts(run: dict[str, Any]) -> str:
    return str(run.get("run_started_at") or run.get("created_at") or "")


def _consecutive_same_sha_count(runs: list[dict[str, Any]]) -> tuple[int, int]:
    """(redundant_count, groups) for consecutive runs on the same head_sha.

    Counts only the second+ run in a consecutive same-sha group. Missing SHAs
    break the sequence rather than being grouped together."""
    ordered = sorted(runs, key=_run_start_ts)
    redundant = 0
    groups = 0
    prev_sha: str | None = None
    group_len = 0
    for r in ordered:
        sha = str(r.get("head_sha") or "")
        if not sha:
            prev_sha = None
            group_len = 0
            continue
        if sha == prev_sha:
            group_len += 1
            redundant += 1
            if group_len == 2:
                groups += 1
        else:
            prev_sha = sha
            group_len = 1
    return redundant, groups


def _consecutive_same_sha_run_ids(runs: list[dict[str, Any]]) -> list[str]:
    ordered = sorted(runs, key=_run_start_ts)
    out: list[str] = []
    prev_sha: str | None = None
    for r in ordered:
        sha = str(r.get("head_sha") or "")
        if not sha:
            prev_sha = None
            continue
        rid = _run_id(r)
        if sha == prev_sha:
            if rid:
                out.append(rid)
        else:
            prev_sha = sha
    return out


def _detect_opt36_schedule_burn(
    wf_path: str, all_runs: list[dict[str, Any]],
    jobs_per_run: list[list[dict[str, Any]]], wf_doc: dict[str, Any],
    monthly_schedule_volume: int | None, start_idx: int,
) -> list[dict[str, Any]]:
    """Cron schedule burn (catalog OPT36) — measured upgrade.

    Static OPT36 already flags "too frequent cron". This measured promotion
    requires positive run-history evidence: consecutive scheduled runs on the
    same head_sha, i.e. the workflow ran again without any code change. It sizes
    only the schedule-event subset, using the event-filtered 30-day total count
    and the workflow's measured mean job-minutes per run."""
    on = _wf_on(wf_doc)
    if not _on_has_event(on, "schedule"):
        return []
    if not monthly_schedule_volume or monthly_schedule_volume <= 0:
        return []
    schedule_runs = [r for r in all_runs if str(r.get("event") or "") == "schedule"]
    if len(schedule_runs) < 2:
        return []
    redundant, groups = _consecutive_same_sha_count(schedule_runs)
    redundant_run_ids = _consecutive_same_sha_run_ids(schedule_runs)
    if redundant <= 0:
        return []

    per_run_min, n_timed = _mean_run_compute_min(jobs_per_run)
    if per_run_min <= 0 or n_timed < _MIN_TIMED_RUNS:
        return []
    scale, basis = _elim_scale_and_basis(monthly_schedule_volume, len(schedule_runs), n_timed)
    credited = round(redundant * per_run_min * scale, 1)
    if credited <= 0:
        return []
    repeated_pct = round(100 * redundant / max(len(schedule_runs), 1))

    title = "Cron Schedule Too Frequent"
    evidence = (
        f"{redundant} scheduled run(s) in {groups} consecutive same-head_sha group(s) "
        f"re-ran without a code change in the sampled schedule slice "
        f"({repeated_pct}% of {len(schedule_runs)} schedule run(s)); "
        f"~{credited:.0f} runner-min/mo of schedule-event compute ({basis}).")
    me = _measured_evidence(
        ["Workflow", "Consecutive same-head_sha schedule runs", "Mean compute/run", "Credited runner-min/mo"],
        [[f"`{wf_path}`", f"{redundant} redundant run(s) in {groups} group(s)",
          f"{per_run_min:.1f} job-min over {n_timed} timed run(s)", f"~{credited:.0f}"]],
        summary=evidence,
        note=("Schedule burn is counted only on event=schedule runs whose head_sha repeats "
              "consecutively, so the detector proves the workflow ran again without a "
              "code change. Basis: the count is from the all-status schedule slice; "
              f"the per-run price is the mean of {n_timed} successful schedule-event "
              "timed run(s). "
              "GUARDRAIL: confirm the current cadence is not an operational SLA before "
              "increasing the cron interval."))
    f = _new_finding(
        "OPT36", "LOW", title, wf_path, "", evidence,
        "cron-schedule-too-frequent",
        _catalog_anchor("OPT36", title), start_idx + 1,
        wc_p50=0.0, rm=credited,
        size_note=("runner-minute (bill) only — schedule-event runs do not gate a PR; "
                   "frequency change must preserve any operational cadence requirement."),
        realization="none", measured_evidence=me)
    f["sizing_basis"] = "measured"
    f["measured_signal"] = (
        f"event=schedule total_count x mean job-minutes "
        f"({monthly_schedule_volume} schedule run(s)/30d; {redundant} same-head_sha "
        f"redundant run(s); {n_timed} timed run(s); scale {scale:.3g})")
    f["tier2_neutrality"] = {
        "proof": "non_pr_event",
        "margin_s": None,
        "ref": ("event=schedule subset only; consecutive same-head_sha schedule runs; "
                "schedule is not a developer PR/merge event"),
    }
    f["tier2_run_subset_events"] = ["schedule"]
    if redundant_run_ids:
        f["tier2_sample_run_ids"] = redundant_run_ids
    f["guardrail"] = ("Confirm the cron cadence is not an operational SLA; prefer widening "
                      "the interval only for cleanup/triage/build jobs where delayed execution "
                      "is acceptable.")
    return [f]


def _supersede_static_pattern(findings: list[dict[str, Any]], measured: list[dict[str, Any]],
                              pattern: str, reason: str) -> None:
    measured_by_wf = {
        str(f.get("workflow_file") or ""): str(f.get("id") or "")
        for f in measured
        if str(f.get("pattern") or "") == pattern and f.get("id")
    }
    if not measured_by_wf:
        return
    for f in findings:
        wf = str(f.get("workflow_file") or "")
        if (str(f.get("pattern") or "") == pattern
                and wf in measured_by_wf
                and f.get("sizing_basis") != "measured"
                and not f.get("tier2_neutrality")):
            f["tier2_superseded_by"] = measured_by_wf[wf]
            f["tier2_superseded_reason"] = reason


def _supersede_static_opt35(findings: list[dict[str, Any]],
                            measured: list[dict[str, Any]]) -> None:
    measured_by_job = {
        (str(f.get("workflow_file") or ""), str(job)): str(f.get("id") or "")
        for f in measured
        if str(f.get("pattern") or "") == "OPT35" and f.get("id")
        for job in (f.get("affected_jobs") or [])
        if str(job)
    }
    if not measured_by_job:
        return
    for f in findings:
        if (str(f.get("pattern") or "") != "OPT35"
                or f.get("sizing_basis") == "measured"
                or f.get("tier2_neutrality")):
            continue
        wf = str(f.get("workflow_file") or "")
        matches = sorted({
            measured_by_job[(wf, str(job))]
            for job in (f.get("affected_jobs") or [])
            if (wf, str(job)) in measured_by_job
        })
        if matches:
            f["tier2_superseded_by"] = matches[0]
            f["tier2_superseded_reason"] = (
                "measured OPT35 fail-fast finding for the same workflow/job")


def _supersede_static_opt36(findings: list[dict[str, Any]],
                            measured: list[dict[str, Any]]) -> None:
    _supersede_static_pattern(
        findings, measured, "OPT36",
        "measured OPT36 schedule-burn finding for the same workflow")


def _detect_opt46_superseded_runs(
    wf_path: str, all_runs: list[dict[str, Any]],
    jobs_per_run: list[list[dict[str, Any]]], wf_doc: dict[str, Any],
    monthly_volume: int | None, start_idx: int,
) -> list[dict[str, Any]]:
    """Superseded runs never cancelled (catalog OPT46) — measured. Structural
    gate (triggers on push/PR, no cancelling concurrency at ANY level, not
    release-like) THEN measured proof: group the all-status run slice by
    `head_branch` and count runs that were genuinely SUPERSEDED — a later run
    STARTED before this one finished (they raced). Sequential, non-overlapping
    runs (distinct default-branch commits) count 0, so a busy `main` no longer
    fabricates waste. Sizes the CANCELLABLE REMAINDER — the mean per-run compute
    pro-rated by how much of each superseded run a cancel would actually have
    reclaimed (`Σ remainder_i/duration_i`, issue #89), not the whole run —
    extrapolated (both directions) to the 30d volume; the naive `(runs-1)`
    whole-run sum is the loose upper bound. Cancellation cause is unknowable → INFERENCE.
    Carries its own post_completion_waste neutrality certificate."""
    on = _wf_on(wf_doc)
    if not (_on_has_event(on, "push") or _on_has_event(on, "pull_request")):
        return []
    if _wf_has_cancelling_concurrency(wf_doc) or _wf_is_release_like(wf_path, wf_doc):
        return []
    if not monthly_volume or monthly_volume <= 0:
        return []  # dormant / unknown 30d volume → can't size a /mo figure honestly

    by_branch: dict[str, list[dict[str, Any]]] = {}
    for r in all_runs:
        b = r.get("head_branch")
        if b:
            by_branch.setdefault(str(b), []).append(r)
    multi = {b: rs for b, rs in by_branch.items() if len(rs) >= 2}
    if not multi:
        return []

    # Measure the runs that ACTUALLY RACED (overlapped in time) AND how much of
    # each was still reclaimable when its successor started — the honest remainder
    # basis (issue #89). The naive (runs-1) is kept only as the loose upper bound.
    tally = _RemainderTally()
    n_overlap_branches = 0
    for rs in multi.values():
        bt = _superseded_remainder(rs)
        tally.add(bt)
        if bt.superseded_n > 0:
            n_overlap_branches += 1
    elim_confirmed = tally.superseded_n
    if elim_confirmed <= 0:
        return []  # no runs overlapped → cancel-in-progress would save nothing
    elim_naive = sum(len(rs) - 1 for rs in multi.values())

    per_run_min, n_timed = _mean_run_compute_min(jobs_per_run)
    if per_run_min <= 0 or n_timed < _MIN_TIMED_RUNS:
        return []  # too few timed runs to price the mean reliably (outlier-fragile)

    sampled_n = len(all_runs)
    scale, basis = _elim_scale_and_basis(monthly_volume, sampled_n, n_timed)
    # Lower (credited) = the REMAINDER figure: the mean per-run compute pro-rated
    # by the share of each superseded run a cancel would actually have reclaimed
    # (`remainder_units` = Σ remainder_i/duration_i). Upper = the naive whole-run
    # (runs-1) bound, unchanged. The old whole-run "overlap-confirmed" figure is
    # now NEITHER bound — it over-charged every late-superseded run its full cost
    # (issue #89). `remainder_ratio` is the single stamped basis: credited ==
    # elim_confirmed × remainder_ratio × per_run_min × scale (equal by definition).
    remainder_ratio = tally.mean_remainder_fraction  # in [0, 1]
    credited = round(tally.remainder_units * per_run_min * scale, 1)
    upper_min = round(elim_naive * per_run_min * scale, 1)
    if credited <= 0:
        return []
    rng = f"{credited:.0f}–{upper_min:.0f}"
    rem_pct = f"{remainder_ratio * 100:.0f}%"
    sigma_pct = (f"{tally.sum_remainder_s / tally.sum_duration_s * 100:.0f}%"
                 if tally.sum_duration_s > 0 else "n/a")
    # The trigger set, named in the note so the Default-vs-Widened routing is
    # decidable FROM THE PROMPT (mirrors OPT45's "workflow triggers on …"
    # evidence line) instead of forcing the agent to open the workflow first.
    trig = "/".join(f"`{t}`" for t in ("pull_request", "push")
                    if _on_has_event(on, t))

    title = ("Superseded Runs Not Cancelled (Missing Concurrency or "
             "`cancel-in-progress: false`)")
    skip_note = (
        f" {tally.skipped_missing_ts} run(s) on these branches lacked usable timestamps "
        "and were excluded from both the count and the remainder."
        if tally.skipped_missing_ts else "")
    evidence = (
        f"{elim_confirmed} run(s) across {n_overlap_branches} branch(es) were superseded "
        f"(a newer run started before they finished) in the sampled window; "
        f"~{rng} runner-min/mo of cancellable-remainder compute — the lower figure credits "
        f"only the {rem_pct} mean remainder each superseded run would have burned AFTER its "
        f"successor started, not the whole run ({basis}). Superseded attribution is INFERENCE "
        f"— the API marks no run 'cancelled-by-concurrency'.")
    me = _measured_evidence(
        ["Workflow", "Overlapping (raced) runs", "Mean compute/run (timed basis)",
         "Reclaimable remainder (mean per run)", "Reclaimable runner-min/mo (range)"],
        [[f"`{wf_path}`", f"{elim_confirmed} confirmed (naive {elim_naive})",
          f"{per_run_min:.1f} job-min over {n_timed} timed run(s)",
          f"{rem_pct} of run (Σremainder/Σduration {sigma_pct})",
          f"~{rng} (lower=remainder, upper=naive runs-1)"]],
        summary=evidence,
        note=("Superseded = a run a NEWER run started before it finished — measured "
              "by timestamp overlap, so sequential (non-racing) commits are NOT charged. "
              "Cancellation cause is unknowable from the API, so the attribution is "
              "INFERENCE. REMAINDER BASIS: cancel-in-progress cancels a superseded run the "
              "moment its successor starts, so only the compute AFTER that moment is "
              "reclaimable — the credited (lower) figure prices the MEAN per-run compute "
              f"pro-rated by each superseded run's wall-clock remainder fraction (mean "
              f"{rem_pct}; Σremainder/Σduration {sigma_pct}), NOT the whole run; exact "
              "per-second compute is unknowable because a run's jobs run in parallel, so the "
              "pro-rata of the mean is the honest estimate. The whole-run figure is now only "
              "the loose UPPER bound (naive runs-1). "
              f"Basis: the superseded COUNT and the remainder ratio are from the all-status "
              f"slice ({sampled_n} runs, from each run's own timestamps); the per-run PRICE "
              f"is the mean of {n_timed} PR-success timed runs (superseded runs' own jobs "
              f"aren't fetched) — different populations.{skip_note} "
              "GUARDRAIL: verify this is NOT a deploy/release/publish workflow (a "
              "mid-flight run may be uploading artifacts / pushing a tag) before "
              "enabling cancellation — and take the predicate from the catalog recipe, "
              "which scopes cancellation with an expression; never a bare "
              "`cancel-in-progress: true`, which also kills in-flight runs on the "
              "default branch and on release tags. ROUTING: this workflow triggers on "
              f"{trig} — with a `pull_request` trigger use the catalog's DEFAULT "
              "(PR-scoped) predicate; without one, the PR-scoped predicate is never "
              "true and saves nothing, so use the catalog's WIDENED predicate."))
    f = _new_finding(
        "OPT46", "MEDIUM", title, wf_path, "", evidence,
        "superseded-runs-not-cancelled-missing-concurrency-or-cancel-",
        _catalog_anchor("OPT46", title), start_idx + 1,
        wc_p50=0.0, rm=credited,
        size_note=("runner-minute (bill) only — superseded runs don't gate the merge "
                   "(the latest run does); range is lower(cancellable remainder)–"
                   "upper(naive runs-1)."),
        realization="none", measured_evidence=me)
    f["measured_signal"] = (
        f"remainder-weighted superseded runs x mean job-minutes "
        f"({elim_confirmed} confirmed, {rem_pct} mean remainder; naive {elim_naive}; "
        f"{n_timed} timed run(s); scale {scale:.3g})")
    # Structured occurrence counts (counts, never parsed prose): numerator and
    # denominator from the SAME population — the all-status run slice the
    # overlap count was taken over.
    f["superseded_confirmed_n"] = int(elim_confirmed)
    f["superseded_slice_n"] = int(sampled_n)
    f["explicit_cancel_disabled"] = _wf_explicit_cancel_disabled(wf_doc)
    # Its own neutrality certificate: superseded runs are compute burned after
    # their signal is dead. Stamped by the detector (the below_cluster_floor
    # path in _stamp_tier2_neutrality only certifies runner-min-only levers).
    f["tier2_neutrality"] = {
        "proof": "post_completion_waste",
        "margin_s": None,
        "ref": ("superseded runs: same head_branch, a newer run started before this "
                "one finished (timestamp overlap); cancellation cause is inference"),
    }
    f["runner_min_range_s"] = [credited, upper_min]
    # The remainder basis, stamped so BOTH the credited figure and any downstream
    # regrounding derive from ONE number (issue #89 §3). `remainder_ratio` is the
    # mean per-run remainder fraction: credited == elim_confirmed × remainder_ratio
    # × per_run_min × scale. A regrounding that re-derives OPT46 from the cost spine
    # MUST scale its whole-run figure by this ratio, or the two surfaces split and
    # the whole-run bug returns on the surface users see. (Today OPT46 is a `measured`
    # detector, so the sizing door stamps `not_spine_derivable` and leaves this
    # figure intact — no spine re-derivation runs — but the ratio is carried anyway.)
    f["superseded_remainder_ratio"] = round(remainder_ratio, 4)
    f["superseded_remainder_units"] = round(tally.remainder_units, 4)
    f["superseded_remainder_seconds"] = [round(tally.sum_remainder_s, 1),
                                         round(tally.sum_duration_s, 1)]
    if tally.skipped_missing_ts:
        f["superseded_skipped_missing_ts"] = int(tally.skipped_missing_ts)
    run_ids = [rid for rs in multi.values() for rid in _superseded_run_ids(rs)]
    if run_ids:
        f["tier2_sample_run_ids"] = run_ids
    return [f]


def _push_double_triggers_with_pr(on: Any, default_branch: str | None) -> bool:
    """True when `on:` runs the workflow on BOTH `pull_request` and a `push`
    that is NOT restricted to the default branch — the shape that runs a PR's
    commits twice (once per event). A push scoped to `branches: [<default>]`
    (only) does NOT double-fire on PR branches, so it's excluded."""
    if not (_on_has_event(on, "push") and _on_has_event(on, "pull_request")):
        return False
    if not isinstance(on, dict):
        return True  # list/str form (e.g. `on: [push, pull_request]`) — unscoped
    push = on.get("push")
    if not isinstance(push, dict):
        return True  # `push:` with no sub-config → all branches
    branches = push.get("branches")
    if branches is None:
        # A push scoped ONLY by `tags:` never fires on PR branches (PRs aren't
        # tags), so it can't double-fire — not a double-trigger.
        if "tags" in push and "branches-ignore" not in push:
            return False
        # No positive `branches:` allow-list → push still fires on PR feature
        # branches (a `branches-ignore:`/`paths:` list excludes only specific
        # branches/paths, not all PR branches), so it double-fires. The measured
        # dup-sha gate in the caller prevents any false positive.
        return True
    if isinstance(branches, str):
        branches = [branches]
    if not isinstance(branches, list):
        return True
    defaults = {default_branch, "main", "master"} - {None}
    # Restricted to (a subset of) default-branch names only → no PR-branch dup.
    return not all(str(b) in defaults for b in branches)


def _detect_opt47_double_trigger(
    wf_path: str, all_runs: list[dict[str, Any]],
    jobs_per_run: list[list[dict[str, Any]]], wf_doc: dict[str, Any],
    monthly_volume: int | None, default_branch: str | None, start_idx: int,
) -> list[dict[str, Any]]:
    """push + pull_request double-trigger (catalog OPT47) — measured. A workflow
    on both events, with push unrestricted to the default branch, runs each PR
    commit TWICE. Structural gate THEN positive instance proof: two runs sharing
    a `head_sha` with different `event`s in the all-status slice. Sizes the
    redundant (push-event) runs' compute. Emits a measured bill finding; the
    wall-clock-neutrality certificate is DEFERRED (whether removing the push run
    is merge-safe depends on which check the repo requires — handled in a later
    PR), so this stays an appendix bill finding, not yet promotion-eligible."""
    on = _wf_on(wf_doc)
    if not _push_double_triggers_with_pr(on, default_branch):
        return []
    # A deploy/publish workflow's PER-COMMIT push run often has a side effect the
    # PR run lacks (a preview deploy, a sha-tagged image, a cache warm) — filtering
    # push would BREAK it. We can't see side effects from the run list, so carve
    # out release-like workflows (same broad set OPT46 uses).
    if _wf_is_release_like(wf_path, wf_doc):
        return []
    if not monthly_volume or monthly_volume <= 0:
        return []  # dormant / unknown 30d volume → can't size a /mo figure honestly

    # Positive instance evidence: same head_sha ran on a PR-family event AND a
    # push whose branch is NOT the default. A push on the DEFAULT branch sharing a
    # PR sha is a post-merge (rebase/FF) run — the merge validation, NOT a
    # feature-branch double-fire — and the prescribed `branches:` fix keeps it, so
    # it is not redundant and must not be counted.
    defaults = {default_branch, "main", "master"} - {None}
    sha_pr: set[str] = set()
    sha_push_nondefault: set[str] = set()
    for r in all_runs:
        sha, ev, br = r.get("head_sha"), str(r.get("event") or ""), r.get("head_branch")
        if not sha:
            continue
        if ev in _DEVELOPER_EVENTS:
            sha_pr.add(str(sha))
        elif ev == "push" and br and str(br) not in defaults:
            sha_push_nondefault.add(str(sha))
    dup_shas = sha_pr & sha_push_nondefault
    if not dup_shas:
        return []  # no feature-branch double-fire — structural match alone isn't a finding

    per_run_min, n_timed = _mean_run_compute_min(jobs_per_run)
    if per_run_min <= 0 or n_timed < _MIN_TIMED_RUNS:
        return []
    sampled_n = len(all_runs)
    scale, basis = _elim_scale_and_basis(monthly_volume, sampled_n, n_timed)
    # One redundant (push) run per duplicated sha in the sample.
    redundant_min = round(len(dup_shas) * per_run_min * scale, 1)
    if redundant_min <= 0:
        return []
    dup_pct = round(100 * len(dup_shas) / max(sampled_n, 1))

    title = "Redundant push + pull_request Double-Trigger"
    evidence = (
        f"{len(dup_shas)} feature-branch commit(s) in the sampled window ran on BOTH a "
        f"push (non-default branch) and a pull_request event (~{dup_pct}% of {sampled_n} "
        f"sampled runs) — each builds twice; ~{redundant_min:.0f} runner-min/mo of "
        f"redundant push-event compute ({basis}).")
    me = _measured_evidence(
        ["Workflow", "Feature-branch dup commits (push + PR)", "Redundant runner-min/mo"],
        [[f"`{wf_path}`", f"{len(dup_shas)} of {sampled_n} sampled", f"~{redundant_min:.0f}"]],
        summary=evidence,
        note=("The fix is a `branches:` filter on the `push:` trigger (keep push on the "
              "default branch; PRs still run via pull_request). Only NON-default-branch "
              "push+PR dups are counted (a default-branch push sharing a PR sha is a "
              "post-merge run the fix keeps). Basis: the redundant push run is priced at "
              f"the mean of {n_timed} PR-success timed runs — different populations. "
              "GUARDRAILS: (1) confirm the merge-required check is the pull_request run, "
              "not the push run (branch protection); (2) confirm the push run has NO "
              "side effect the PR run lacks — a per-commit preview deploy, a sha-tagged "
              "image/artifact, or a cache warm makes it NOT redundant and the fix would "
              "break it."))
    f = _new_finding(
        "OPT47", "MEDIUM", title, wf_path, "", evidence,
        "redundant-push-pull_request-double-trigger",
        _catalog_anchor("OPT47", title), start_idx + 1,
        wc_p50=0.0, rm=redundant_min,
        size_note=("runner-minute (bill) only — redundant push-event copy of each PR "
                   "commit; neutrality certificate deferred (requires confirming the "
                   "required check is the pull_request run)."),
        realization="none", measured_evidence=me)
    return [f]


def _detect_opt49_step_outliers(
    wf_path: str, jobs_per_run: list[list[dict[str, Any]]], start_idx: int,
    monthly_volume: int | None = None, crit: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """NOT DISPATCHED — OPT49 was CUT (see the collect() dispatch loop). It fired
    on a *setup* step whose MEDIAN exceeds 60s and inferred "uncached" from the
    duration alone, never verifying a missing/cold cache — so it was the "a step
    is slow" observation SKILL.md's admission gate forbids, with a one-size "add
    a cache" fix mis-applied across checkout/install/setup and an overstated
    runner-min figure. The verified slow-setup signal now comes from the cache
    family (OPT3/5/8/9, which prove the cache is cold from --with-logs) and from
    OPT73 (a shared setup step across the cluster). The detector is retained for
    reference only.

    (Historical behavior, for reference: a *setup* step whose MEDIAN duration
    exceeded 60s; the former high-run-to-run-variance case was already cut as a
    non-finding; wall-clock capped at the job's critical-path slice.)"""
    n_runs = len(jobs_per_run)
    by_step: dict[tuple[str, str], list[tuple[float, str]]] = {}
    for run_jobs in jobs_per_run:
        for j in run_jobs:
            job_name = str(j.get("name", ""))
            url = str(j.get("html_url") or "")
            for sname, d in _step_durations(j):
                by_step.setdefault((job_name, sname), []).append((d, url))
    out: list[dict[str, Any]] = []
    idx = start_idx
    for (job_name, sname), samples in by_step.items():
        if len(samples) < 3:
            continue
        ds = [d for d, _ in samples]
        p50 = _percentile(ds, 50)
        p95 = _percentile(ds, 95)
        lo, hi = min(ds), max(ds)
        cls = _classify_step(sname)
        # ONLY the slow-SETUP case fires. A *setup* step whose MEDIAN exceeds 60s
        # is a concrete, evidence-backed, cacheable cost. The former
        # high-variance case ("this step's duration swings") was an OBSERVATION,
        # not an actionable defect — it was cut (a varying number is not a
        # finding; if a step is flaky it surfaces via OPT48 failure-rate). Median
        # (not mean) gates it so noise doesn't trip it.
        setup_too_long = cls == "setup" and p50 > 60.0
        if not setup_too_long:
            continue
        # A checkout can't reach ~5s (a fetch-depth:0 clone of a big repo is
        # irreducible past a shallow-checkout floor ~30s); other setup → ~5s.
        target = 30.0 if _CHECKOUT_STEP_RE.search(sname) else 5.0
        raw_wc = max(p50 - target, 0.0)
        title = "Slow Setup Step"
        why = f"setup step median {p50:.0f}s > 60s (P95 {p95:.0f}s)"
        realization = "direct"
        fix_hint = ("Cache and pin this setup (lockfile-keyed `actions/cache` "
                    "or `setup-*` `cache:`, pinned action/toolchain, mirrored "
                    "downloads); the warm-cache run is the floor.")
        idx += 1
        # Cap wall-clock at the job's slice of the critical path — a step can
        # never save more wall-clock than the run has, and a sub-floor job's
        # step is runner-minute only.
        wc, cap_note = _cap_wall_clock(raw_wc, job_name, crit)
        # Size runner-min by THIS job's observed run frequency (samples /
        # sampled runs), not the full workflow volume.
        eff_vol = _effective_volume(monthly_volume, len(samples), n_runs)
        rm = round(raw_wc * eff_vol / 60.0, 1) if eff_vol else None
        slow_d, slow_url = max(samples, key=lambda du: du[0])
        me = _measured_evidence(
            ["Step", "Job", "P50 (typical)", "P95", "Min–Max", "Samples",
             "Slowest run (job log)"],
            [[f"`{sname}`", f"`{job_name}`", f"{p50:.0f}s", f"{p95:.0f}s",
              f"{lo:.0f}–{hi:.0f}s", str(len(samples)), _link(slow_d, slow_url)]],
            summary=(f"step `{sname}` in job `{job_name}`: {why}, over "
                     f"{len(samples)} sampled runs."),
            note=("P50 is the typical run, P95/Max the tail — the gap is the "
                  "finding. The link opens the slowest run's job log. " + fix_hint))
        size_note = ("uncached/slow setup — saving is setup time above a warm "
                     "cache floor")
        if cap_note:
            size_note += "; " + cap_note
        out.append(_new_finding(
            "OPT49", "MEDIUM", title, wf_path, job_name,
            f"step `{sname}` in job `{job_name}`: {why} over {len(samples)} runs",
            "step-duration-outlier", "opt49--slow-setup-step", idx,
            wc_p50=wc, rm=rm, size_note=size_note,
            realization=realization, measured_evidence=me,
        ))
    return out


def _detect_opt50_long_post_steps(
    wf_path: str, jobs_per_run: list[list[dict[str, Any]]], start_idx: int,
    monthly_volume: int | None = None, crit: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per "Post *" step, mean > 30s. Catalog body OPT50.

    NOT DISPATCHED — OPT50 was cut: "a post-step takes a while" is an
    observation, not an actionable optimization. The detector is retained for
    reference (and so the catalog pattern can be reported as detector-less for
    coverage honesty), but `collect()` never calls it. See the dispatch loop.
    """
    n_runs = len(jobs_per_run)
    by_post: dict[tuple[str, str], list[tuple[float, str]]] = {}
    for run_jobs in jobs_per_run:
        for j in run_jobs:
            job_name = str(j.get("name", ""))
            url = str(j.get("html_url") or "")
            for sname, d in _step_durations(j):
                if _classify_step(sname) == "post":
                    by_post.setdefault((job_name, sname), []).append((d, url))
    out: list[dict[str, Any]] = []
    idx = start_idx
    for (job_name, sname), samples in by_post.items():
        if len(samples) < 2:
            continue
        ds = [d for d, _ in samples]
        mean = _stats.mean(ds)
        if mean <= 30.0:
            continue
        idx += 1
        # Wall-clock saving: trim post step from mean to ~10s target, capped at
        # the job's slice of the critical path.
        raw_wc = max(mean - 10.0, 0.0)
        wc, cap_note = _cap_wall_clock(raw_wc, job_name, crit)
        eff_vol = _effective_volume(monthly_volume, len(samples), n_runs)
        rm = round(raw_wc * eff_vol / 60.0, 1) if eff_vol else None
        slow_d, slow_url = max(samples, key=lambda du: du[0])
        me = _measured_evidence(
            ["Post-step", "Job", "Mean", "Samples", "Slowest run (job log)"],
            [[f"`{sname}`", f"`{job_name}`", f"{mean:.0f}s", str(len(samples)),
              _link(slow_d, slow_url)]],
            summary=(f"post-step `{sname}` in job `{job_name}` averages {mean:.0f}s "
                     f"over {len(samples)} sampled runs (catalog threshold 30s)."),
            note="Post-steps (cache save, cleanup) run before the job completes, "
                 "so they sit on the critical path. The link opens the slowest run.")
        size_note = "post-step cost is on the critical path before job completes"
        if cap_note:
            size_note += "; " + cap_note
        out.append(_new_finding(
            "OPT50", "LOW", "Post Steps Taking Too Long", wf_path, job_name,
            f"post-step `{sname}` in job `{job_name}` mean {mean:.0f}s over "
            f"{len(samples)} runs (catalog threshold 30s)",
            "post-steps-taking-too-long", "opt50--post-steps-taking-too-long",
            idx,
            wc_p50=wc, rm=rm,
            size_note=size_note,
            realization="direct", measured_evidence=me,
        ))
    return out


def _detect_opt51_install_ratio(
    wf_path: str, jobs_per_run: list[list[dict[str, Any]]], start_idx: int,
    monthly_volume: int | None = None, crit: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per job, ratio setup_time / total_time across runs (median per job).
    Flag if > 50%. Catalog body OPT51."""
    n_runs = len(jobs_per_run)
    by_job_ratio: dict[str, list[float]] = {}
    by_job_total: dict[str, list[tuple[float, str]]] = {}
    for run_jobs in jobs_per_run:
        for j in run_jobs:
            name = str(j.get("name", ""))
            steps = _step_durations(j)
            if not steps:
                continue
            setup_s = sum(d for n, d in steps if _classify_step(n) == "setup")
            total_s = sum(d for _, d in steps)
            if total_s < 30.0:
                continue  # ignore micro-jobs (catalog implication)
            by_job_ratio.setdefault(name, []).append(setup_s / total_s)
            by_job_total.setdefault(name, []).append((total_s, str(j.get("html_url") or "")))
    out: list[dict[str, Any]] = []
    idx = start_idx
    for name, ratios in by_job_ratio.items():
        if len(ratios) < 2:
            continue
        med_ratio = _stats.median(ratios)
        totals = by_job_total[name]
        med_total = _stats.median([t for t, _ in totals])
        if med_ratio <= 0.5:
            continue
        # Work-floor: if the actual work is tiny (a few-second validator/script),
        # a high setup ratio is STRUCTURAL — you can't make 1s of work 70% of an
        # 85s job. Those aren't a caching defect; skip them (they belong to a
        # "merge trivial validators" suggestion, not install-ratio).
        work_s = med_total * (1.0 - med_ratio)
        if work_s < 20.0:
            continue
        idx += 1
        raw_wc = round(med_total * (med_ratio - 0.3), 1)  # bring ratio down to 30%
        # Cap at the job's slice of the critical path (a setup-heavy job that
        # isn't the long pole saves runner-minutes, not wall-clock).
        wc, cap_note = _cap_wall_clock(raw_wc, name, crit)
        # Size by the job's observed run frequency, not the full workflow volume
        # (gated jobs / reusable-workflow legs run in a fraction of runs).
        eff_vol = _effective_volume(monthly_volume, len(ratios), n_runs)
        rm = round(raw_wc * eff_vol / 60.0, 1) if eff_vol else None
        slow_t, slow_url = max(totals, key=lambda tu: tu[0])
        me = _measured_evidence(
            ["Job", "Setup/total (median)", "Median total", "Samples", "Example run (job log)"],
            [[f"`{name}`", f"{med_ratio*100:.0f}%", f"{med_total:.0f}s",
              str(len(ratios)), _link(slow_t, slow_url)]],
            summary=(f"job `{name}` spends a median {med_ratio*100:.0f}% of its "
                     f"{med_total:.0f}s runtime on setup (checkout/install/cache "
                     f"restore) rather than actual test/build work, over "
                     f"{len(ratios)} sampled runs."),
            note="The link opens an example run's job log (step timings visible "
                 "in the log). Artifact handoff or a warm dependency cache shifts "
                 "the ratio back toward work.")
        out.append(_new_finding(
            "OPT51", "HIGH", "Install-to-Test Ratio >50%", wf_path, name,
            f"job `{name}` setup/total median {med_ratio*100:.0f}% "
            f"({med_total:.0f}s total) over {len(ratios)} runs",
            "install-to-test-ratio-50", "opt51--install-to-test-ratio--50",
            idx,
            wc_p50=wc, rm=rm,
            size_note=("setup-heavy jobs benefit from artifact handoff or a "
                       "composite-action consolidation"
                       + ("; " + cap_note if cap_note else "")),
            realization="direct", measured_evidence=me,
        ))
    return out


def _struct_toks(s: str) -> frozenset[str]:
    return frozenset(t for t in _re.split(r"[^a-z0-9]+", (s or "").lower()) if t)


def _job_name_scope_prefixed(check_name: str, job_name: str) -> bool:
    """True only when `job_name` appears in `check_name` as a SCOPE/MATRIX-prefixed
    phrase — the one shape under which a token-SUBSET check→job match is real. A
    monorepo check-run prepends the package scope (space-separated) to a reusable
    job's name (`@scope/pkg Integration Test` ⊃ job `Integration Test`), and a
    reusable child reads `<caller> / <child>` — in both the job name survives
    INTACT, bounded by whitespace / `/`.

    Rejects the look-alike where the job's tokens are a subset only because they
    are FUSED into a hyphen/underscore COMPOUND identifier of the check — e.g.
    `macos-build` ⊃ `build`, two DIFFERENT jobs on different runners. Binding the
    expotools `build` job (114s TypeScript compile) to the `macos-build` gate
    (853s) drills the wrong job and leaks its step decomposition onto the pole.
    Tokenization can't tell `macos-build` from `macos build` (both → {macos,
    build}), so this compares the RAW strings: the job-name occurrence must be
    bounded on each side by start/end or a non-alphanumeric, non-`-`/`_` char (a
    scope separator), never by token fusion."""
    def _fusion(ch: str) -> bool:
        # "" is start/end of string (a clean boundary), NOT fusion — and `"" in
        # "-_"` is truthy, so guard it explicitly.
        return bool(ch) and (ch.isalnum() or ch in "-_")
    c = check_name.lower()
    j = job_name.lower().strip()
    if not j:
        return False
    start = 0
    while True:
        i = c.find(j, start)
        if i < 0:
            return False
        before = c[i - 1] if i > 0 else ""
        after = c[i + len(j)] if i + len(j) < len(c) else ""
        if not _fusion(before) and not _fusion(after):
            return True
        start = i + 1


# Patterns whose `wall_clock_p50_s` measures a PRE-START / queue-wait axis (time
# the developer waits BEFORE the job runs), NOT a reduction of the job's RUN
# time. OPT43 (Excessive Queue Time) reports the job's wait-to-start as its
# wall-clock number; that fix shortens time-to-start, it does not shorten the
# dominant step. So such a finding must NOT count toward the "covered" map that
# suppresses a pole's structural RUN-time lever — the suppression in
# `_detect_structural_candidates` asks "did a hygiene fix already MATERIALLY
# shorten this pole's RUN time?", and a queue-time saving answers a different
# question, leaving the real OPT75/etc lever wrongly hidden on the headline gate.
_PRESTART_AXIS_PATTERNS = frozenset({"OPT43"})


def _build_covered_job_savings(
    findings: list[dict[str, Any]],
) -> dict[frozenset[str], float]:
    """Map each affected job's token-set -> the best RUN-time hygiene saving on it.

    Only RUN-time-axis findings count: a finding whose `wall_clock_p50_s`
    measures a pre-start / queue wait (see `_PRESTART_AXIS_PATTERNS`) is skipped,
    because it shortens time-to-start, not the pole's dominant step — counting it
    would suppress that pole's structural lever against the wrong axis."""
    covered: dict[frozenset[str], float] = {}
    for f in findings:
        if f.get("pattern") in _PRESTART_AXIS_PATTERNS:
            continue
        try:
            s = float(f.get("wall_clock_p50_s") or 0.0)
        except (TypeError, ValueError):
            s = 0.0
        if s <= 0:
            continue
        for j in (f.get("affected_jobs") or []):
            jt = _struct_toks(str(j))
            if jt:
                covered[jt] = max(covered.get(jt, 0.0), s)
    return covered


def _workflows_matching_check(
    check_name: str, crit_by_wf: dict[str, dict[str, Any]],
) -> "set[str]":
    """ALL workflows with a job matching `check_name`, using the SAME exact-then-scoped-subset
    rule as `_map_check_to_job` (which returns only the single slowest match). A same-named
    job can live in several workflows (a PR gate + a push-only sibling); this surfaces every
    one so `_is_pr_gate_check` can keep the check when ANY of them is PR-triggered."""
    return {wf for wf, _ in _check_job_identities(check_name, crit_by_wf)}


def _check_job_identities(
    check_name: str, crit_by_wf: dict[str, dict[str, Any]],
) -> "list[tuple[str, frozenset[str]]]":
    """Every `(workflow_file, job-token-set)` whose sampled job matches `check_name`, by the
    SAME exact-then-scoped-subset precedence as `_map_check_to_job` / `_workflows_matching_check`
    (exact-token matches win; the scoped-subset set is used only when none is exact). Unlike
    `_map_check_to_job` (single slowest mapping), returns ALL matches — used by the off-spine
    stamp, where a same-named job dropped from the spine is off it in EVERY workflow that
    defines it."""
    ct = _struct_toks(check_name)
    if not ct:
        return []
    exact: list[tuple[str, frozenset[str]]] = []
    subset: list[tuple[str, frozenset[str]]] = []
    for wf, crit in crit_by_wf.items():
        for jname in (crit.get("job_p50") or {}):
            jt = _struct_toks(jname)
            if not jt:
                continue
            if jt == ct:
                exact.append((wf, jt))
            elif jt < ct and _job_name_scope_prefixed(check_name, jname):
                subset.append((wf, jt))
    return exact or subset


def _stamp_off_spine_findings(
    findings: list[dict[str, Any]],
    dropped_check_names: list[str],
    kept_check_names: list[str],
    crit_by_wf: dict[str, dict[str, Any]],
) -> None:
    """Stamp `off_spine=True` on each finding whose job was DROPPED from the merge-gating
    spine (encord §6 Cause 2), so the renderer never frames it "on the critical path" while
    the spine footnote records it as excluded. A non-required job that is its OWN workflow's
    long pole (e.g. encord `Run integration tests`, job p50 ~1040s — gates every PR but isn't
    required) carries a large credited `wall_clock_p50_s` (~512s here) that survives the
    concurrency cascade, yet is off the required-scoped spine; the 30s magnitude floor in
    `_saves_wall_clock` can't catch it, so membership is decided here, where the dropped/kept
    check sets are known. Best-effort by identity: a finding lacking a `workflow_file` or
    `affected_jobs` can't be resolved and is left to the magnitude floor (the prior behavior).

    IDENTITY-AWARE (workflow_file + job tokens), NOT bare name. Each dropped/kept check is
    resolved to EVERY `(workflow_file, job-token-set)` it matches (via `_check_job_identities`,
    same exact-then-scoped-subset precedence as `_map_check_to_job`) — NOT just the single
    slowest mapping: a check dropped from the spine is off it in every workflow that defines
    that job (encord `Run integration tests` lives in both `sdk-pr.yml` and `test-sdk.yml`,
    both non-required, so both must be stamped — the single-slowest resolver missed the
    sibling). A finding is stamped only when its `(workflow_file, job)` matches a DROPPED check
    AND matches NO KEPT (on-spine) check in the same workflow.

    The on-spine refusal is the safety net against a monorepo FALSE coverage gap (`@a/pkg
    build` dropped vs `@b/pkg build` on-spine — both collapse to job `build` under the
    scoped-subset rule): the dropped check resolves to BOTH `a.yml` and `b.yml`, but so does
    the kept `@b/pkg build`, so the on-spine match fires on `b.yml` AND `a.yml` and the stamp
    is refused for both. A real, file-backed on-spine pole is never silently turned into a
    coverage gap — that worst case is fully closed. The residual cost is the inverse and far
    milder: the genuinely-dropped `a.yml/build` finding is also left UNSTAMPED, so if its
    credited saving clears the 30s floor it can keep an appendix "on the critical path"
    framing (a residual contradiction in that rare same-token cross-scope collision only). We
    accept that — an under-stamp is a recoverable framing imprecision; an over-stamp is the
    irrecoverable false coverage gap."""
    dropped_ids = [p for n in dropped_check_names
                   for p in _check_job_identities(n, crit_by_wf)]
    if not dropped_ids:
        return
    kept_ids = [p for n in kept_check_names
                for p in _check_job_identities(n, crit_by_wf)]

    def _job_on(ids: "list[tuple[str, frozenset[str]]]", wf: str,
                jobs: "list[frozenset[str]]") -> bool:
        return any(wf == iw and jt == ijt for iw, ijt in ids for jt in jobs)

    for f in findings:
        wf = f.get("workflow_file")
        if not wf:
            continue
        fjobs = [t for t in (_struct_toks(str(j))
                             for j in (f.get("affected_jobs") or []) if str(j)) if t]
        if not fjobs:
            continue
        if _job_on(kept_ids, wf, fjobs):
            continue  # this job IS on the spine — never stamp it off (false-gap guard)
        if _job_on(dropped_ids, wf, fjobs):
            f["off_spine"] = True


def _crit_has_developer_timing(crit: dict[str, Any]) -> bool:
    """True when this workflow's job p50s were measured on a PR/merge event.

    `event_scope == all-events` means `_crit_for` had no sampled developer-facing
    run for this workflow and measured whatever events were available instead
    (often push/schedule). Those job timings are valid for bill/hygiene context,
    but must not become the PR merge-wait spine. Missing `event_scope` is treated
    as developer-timed for older/direct unit-test fixtures that construct a bare
    `_critical_path()` result without the `_crit_for()` wrapper.
    """
    return str(crit.get("event_scope") or "") != "all-events"


def _map_check_to_job(
    check_name: str, crit_by_wf: dict[str, dict[str, Any]], *,
    require_developer_timing: bool = False,
) -> tuple[str, str] | None:
    """Map a measured check-run name to the ONE (workflow, job display-name) that
    produced it — or None when we cannot pin exactly one. Prefers an EXACT token
    match, falling back to token-subset only when none is exact — a matrix/reusable
    job `Integration Test` surfaces as a check `@scope/pkg Integration Test`, but
    subset ALONE mis-fires on compound matrix names (job `prisma-adapter Integration
    Test` is a token-subset of the DIFFERENT check `kysely-prisma-adapter Integration
    Test`, which would attach the wrong job's step decomposition to the kysely pole).
    The subset fallback is further gated by `_job_name_scope_prefixed`, so a job whose
    tokens are a subset only because they are FUSED into a hyphen/underscore compound
    of the check (`build` ⊂ `macos-build`, a different untimed job) never binds.

    CROSS-WORKFLOW same-name ambiguity → REFUSE, don't guess. When the winning match
    tier (exact, else subset) lands in MORE THAN ONE workflow file — a monorepo's
    copy-pasted or reusable same-named jobs (two `build.yml`/`test.yml` package
    workflows each declaring a `build` job) — there is NO evidence here to pick the
    right one: the check-runs endpoint carries no workflow path, and a check-run's own
    started→completed span is queue-inflated (an 80s job can read 1871s), so it can't
    be trusted to select the candidate whose job p50 is "closest". Keeping the SLOWEST
    (the old behaviour) bound the pole's `workflow_file`, step decomposition, and fix
    recipe to a DIFFERENT workflow — a confident, hard-to-detect mis-attribution. So
    this returns None on genuine cross-workflow ambiguity and lets the caller withhold
    the per-file drill/fix — the check renders UNATTRIBUTED and the collision is
    DISCLOSED (`_check_producing_workflows`-detected as ambiguous rather than
    mislabelled a third-party app check), exactly as `_check_to_job_node_scanned`
    refuses cross-file ambiguity. This is a FILE attribution bail only: the crown
    MAGNITUDE is not ambiguous (a PR waits on the slowest same-named job), so the spine
    still grounds it via `_check_grounded_job_p50` and keeps a real ambiguous merge gate
    in the crowning basis — it is NOT dropped to the fileless bucket. Same-name jobs
    WITHIN one workflow (matrix legs) are not ambiguous for the file attribution and
    still resolve to the slowest.

    Returns the slowest matching job's (wf, job) when exactly one workflow matches,
    or None for a fileless check no sampled job produced, a fused-compound-only
    subset, OR cross-workflow same-name ambiguity.

    When `require_developer_timing` is true, all-events workflow timings are
    ignored. This is used by the PR-critical-path spine so a push/schedule sample
    from a workflow that also declares `pull_request` cannot lend its job p50 or
    step decomposition to a PR pole. That filter is itself disambiguating evidence:
    when only ONE candidate workflow is PR/merge-timed, the ambiguity is resolved and
    this never bails. The PR check-run sample remains the fallback timing source.
    """
    ct = _struct_toks(check_name)
    if not ct:
        return None
    exact: list[tuple[float, str, str]] = []
    subset: list[tuple[float, str, str]] = []
    for wf, crit in crit_by_wf.items():
        if require_developer_timing and not _crit_has_developer_timing(crit):
            continue
        for jname, p in (crit.get("job_p50") or {}).items():
            jt = _struct_toks(jname)
            if not jt:
                continue
            if jt == ct:
                exact.append((p, wf, jname))
            elif jt < ct and _job_name_scope_prefixed(check_name, jname):
                subset.append((p, wf, jname))
    tier = exact or subset
    if not tier:
        return None
    if len({wf for _p, wf, _j in tier}) > 1:
        return None  # cross-workflow same-name ambiguity — refuse rather than guess
    best = max(tier, key=lambda t: t[0])  # slowest within the one matching workflow
    return (best[1], best[2])


def _check_producing_workflows(
    check_name: str, crit_by_wf: dict[str, dict[str, Any]], *,
    require_developer_timing: bool = False,
) -> "set[str]":
    """The DISTINCT workflow files whose SAMPLED job matches `check_name` by the same
    exact-then-scoped-subset precedence `_map_check_to_job` uses (honoring
    `require_developer_timing`). `len() > 1` is EXACTLY the cross-workflow same-name
    ambiguity `_map_check_to_job` now refuses to guess — used by the disclosure path to
    tell an ambiguous FILE-BACKED check (produced by same-named jobs in several
    workflows, unattributable to one) apart from a genuinely fileless bot/app check, so
    the honest-degradation message never calls a real in-repo check "third-party"."""
    ct = _struct_toks(check_name)
    if not ct:
        return set()
    exact: set[str] = set()
    subset: set[str] = set()
    for wf, crit in crit_by_wf.items():
        if require_developer_timing and not _crit_has_developer_timing(crit):
            continue
        for jname in (crit.get("job_p50") or {}):
            jt = _struct_toks(jname)
            if not jt:
                continue
            if jt == ct:
                exact.add(wf)
            elif jt < ct and _job_name_scope_prefixed(check_name, jname):
                subset.add(wf)
    return exact or subset


def _check_grounded_job_p50(
    check_name: str, crit_by_wf: dict[str, dict[str, Any]], *,
    require_developer_timing: bool = False,
) -> "float | None":
    """The SLOWEST sampled job p50 for `check_name` across ALL producing workflows, by the same
    exact-then-scoped-subset precedence `_map_check_to_job` uses (honoring `require_developer_timing`).

    Where `_map_check_to_job` returns None on cross-workflow same-name ambiguity — because it can't
    pick ONE workflow FILE to attribute steps/fix to — the crowning MAGNITUDE is NOT ambiguous: a PR
    carries a check-run named `Build` from each colliding workflow and waits on the SLOWEST, so the
    max candidate job p50 is the honest real-CI-compute wall that merge gate takes. This lets the
    spine keep an ambiguous but FILE-BACKED merge gate in the crowning basis (never mis-dropped into
    the fileless / PR-lifetime-latency bucket, which would silently uncrown a real pole and mislabel
    it status-gating latency) while its per-file drill/fix stays withheld and the collision is
    disclosed (`_check_producing_workflows`). This is a sampled job p50, NOT the queue-inflated
    check-run span, so it is a trustworthy crown number. Returns None only when NO sampled
    (developer-timed, when required) job produces the check — a genuinely fileless bot/app check."""
    ct = _struct_toks(check_name)
    if not ct:
        return None
    exact: list[float] = []
    subset: list[float] = []
    for crit in crit_by_wf.values():
        if require_developer_timing and not _crit_has_developer_timing(crit):
            continue
        for jname, p in (crit.get("job_p50") or {}).items():
            jt = _struct_toks(jname)
            if not jt:
                continue
            if jt == ct:
                exact.append(p)
            elif jt < ct and _job_name_scope_prefixed(check_name, jname):
                subset.append(p)
    tier = exact or subset
    return max(tier) if tier else None


def _name_template_regex(template: str) -> "_re.Pattern[str]":
    """Compile a workflow job `name:` template into a regex that matches its expanded
    check-run names. A matrix job's `name` carries `${{...}}` placeholders (e.g.
    `UNIT Test (Shard ${{ matrix.shard }})`); each placeholder expands per leg to a
    concrete check-run (`UNIT Test (Shard 4)`). Replacing every `${{...}}` with `.+?`
    and escaping the literal spans yields a matcher for all of a job's legs. A name with
    no placeholder compiles to an exact match."""
    parts = _re.split(r"\$\{\{.*?\}\}", template)
    return _re.compile("^" + ".+?".join(_re.escape(p) for p in parts) + "$")


def _name_template_is_degenerate(template: str) -> bool:
    """True iff a job `name:` template is ENTIRELY `${{...}}` placeholder(s) with no
    literal anchoring text (`${{ matrix.target }}`, `${{a}}${{b}}`, or placeholders glued
    only by whitespace) — the case `_name_template_regex` compiles to the match-ANYTHING
    pattern `^.+?$`.

    Such a template carries ZERO discriminating signal: every check-run name is a plausible
    expansion of it, so a "match" is evidence-free. That is harmless when the candidate set is
    already scoped to a workflow's own runtime jobs (OPT35/OPT57 runtime-name matching), but it
    is a real bug in the check->file binders below, whose candidate set INCLUDES foreign
    managed/external check-runs (a Netlify/CLA/app check the workflow never produces): a
    degenerate template grabs the first such check-run and mis-anchors it to the workflow file,
    fabricating a file-backed long pole + a wrong-file agent prompt. So the scanned-graph binders
    refuse a degenerate-template match — a check whose ONLY match is `^.+?$` stays honestly
    fileless (upholding `_check_to_job_node_scanned`'s stated invariant that a genuinely fileless
    check matches nothing -> returns None -> stays fileless). A degenerate-named job's own legs
    still resolve via the SAMPLED-timing anchor (`_map_check_to_job`), which a foreign external
    check — having no sampled job — never reaches."""
    if "${{" not in template:
        return False
    return all(not p.strip() for p in _re.split(r"\$\{\{.*?\}\}", template))


def _name_template_leads_with_placeholder(template: str) -> bool:
    """True iff a job `name:` template BEGINS with a `${{...}}` placeholder (no literal text
    before the first one) — `${{ matrix.variant }} / build`, `${{ matrix.os }} test`. Its
    compiled regex starts with `.+?`, which can greedily consume ACROSS a `" / "` boundary and
    so impersonate a reusable-workflow `<caller> / <child>` check-run name: a foreign matrix job
    `${{ matrix.variant }} / build` matches the reusable check `Suite / build` (the `.+?` eats
    `Suite`), preempting the genuine reusable caller `Suite` in another file. Distinct from
    `_name_template_is_degenerate` (ENTIRELY placeholder → matches anything); here there IS
    trailing literal text, but no LEADING anchor to stop `.+?` crossing the reusable separator.

    The cross-workflow SCANNED binders refuse such a match ONLY when a genuine reusable caller
    actually competes for the check (`_reusable_caller_claims`) — issue #118 follow-up. Without a
    sampled-timing anchor a leading-placeholder template can't be told apart from a foreign check
    whose reusable `<caller> / <child>` reading belongs to a different file, and mis-binding the
    wrong file is the worse failure; but when NO reusable caller claims the check the
    leading-placeholder job IS its sole real producer (`${{ matrix.variant }} / build` genuinely
    makes `linux / build`), so the match must stand or a real matrix job reads fileless. A
    leading-placeholder job's OWN legs also resolve via the sampled-timing anchor
    (`_map_check_to_job`), which is workflow-scoped and never crosses to a foreign reusable caller."""
    if "${{" not in template:
        return False
    return not _re.split(r"\$\{\{.*?\}\}", template)[0].strip()


def _reusable_caller_claims(
    check: str, job_graph: dict[str, dict[str, dict[str, Any]]] | None,
) -> bool:
    """True iff some REUSABLE-caller job in `job_graph` would claim `check` as its leaf
    `<caller> / <child>` check-run — a reusable job whose `name` equals the check or is a
    `<name> / ` prefix of it (the exact reusable match `_check_to_job_node`'s last-resort split
    uses). This scopes the leading-placeholder refusal in the scanned binders: a foreign
    `${{…}} / build` template must not steal a check a genuine reusable caller produces
    (`Suite / build` → the `Suite` caller), but when NO such caller exists the leading-placeholder
    job is the check's real sole producer and must still bind (`linux / build` from a lone
    `${{ matrix.variant }} / build`), else a real matrix job reads fileless (issue #118, PR #126)."""
    if not job_graph or " / " not in check:
        return False
    for jobs in job_graph.values():
        for jid, info in jobs.items():
            if not info.get("reusable"):
                continue
            nm = info.get("name") or jid
            if check == nm or check.startswith(nm + " / "):
                return True
    return False


def _static_matrix_leg_match(name: str, is_matrix: bool, check: str) -> bool:
    """True iff a matrix-flagged job's STATIC `name:` (no `${{ matrix.* }}` to expand) matches
    `check` allowing GitHub's appended ` (<leg…>)` suffix — `Matrix Test` → `Matrix Test (1.13.*)`.
    Gated on `is_matrix` and bounded by a trailing ` (...)`, so a check merely sharing a name
    prefix (`Matrix Testing`) never matches. Complements `_name_template_regex` (which handles the
    `${{ matrix.* }}`-TEMPLATED name); a matrix job's `name:` may be static OR templated. Used by
    ALL static check→job/file mappers so a static-name matrix check resolves consistently
    everywhere (else one report site calls it file-backed and another fileless/external)."""
    if not is_matrix:
        return False
    return bool(_re.match("^" + _re.escape(str(name)) + r"(?: \(.*\))?$", check))


def _check_to_job_node(
    check: str, job_graph: dict[str, dict[str, dict[str, Any]]],
    crit_by_wf: dict[str, dict[str, Any]],
) -> tuple[str, str] | None:
    """Map a measured check-run name to its (workflow_file, job_id) in `job_graph`,
    or None when no job produces it (a fileless/managed/external check, or one we
    can't pin). A SAME-WORKFLOW job (plain or matrix-templated) is resolved FIRST; the
    reusable-workflow `<caller> / <child>` split is the LAST resort:

    - **Same-workflow job (tried first).** Anchor the workflow file via `_map_check_to_job`
      (which uses the sampled timing, so a check produced in only ONE sampled file resolves,
      and a genuine cross-file same-name collision is REFUSED rather than guessed), then match
      the check against that file's job `name` (exact → matrix `${{…}}` template regex →
      static-name matrix leg). When no sampled job anchors the file — or the anchored file's
      jobs don't name-match the check — fall back to `_check_to_job_node_scanned`, which
      name-matches against the scanned job graph alone and bails to None on cross-file
      ambiguity (so a triage-skipped workflow's jobs, never fetched, still bind if unambiguous).
    - **Reusable-workflow child (LAST resort).** GitHub names a reusable invocation's leaf
      check-runs `<caller job name> / <child job>`. So a check equal to a reusable caller's
      name, or prefixed `<caller name> / `, maps to that CALLER job (its children are grouped
      under it — the caller's `needs:` and required-status flow to the whole invocation).

    Ordering rationale (issue #118): a job whose OWN `name:` contains `" / "` — a matrix
    template like `test / ${{ matrix.type }}` producing the check `test / ethereum` — must
    bind to ITS job. Parsing the `" / "` as a reusable caller/child separator FIRST demoted
    such a real gate to `workflow_file=None` ("fileless/managed, don't investigate"). Trying
    the same-workflow match first, and the reusable split only when NO same-workflow job
    (plain or templated) produced the check, fixes that while keeping genuine reusable-child
    names (`<caller> / build`, which match no same-workflow job) resolving to their caller."""
    # 1. Same-workflow job anchored by sampled timing: pick the one workflow whose sampled
    #    job produced the check, then resolve its job id by display name.
    m = _map_check_to_job(check, crit_by_wf)
    if m is not None:
        wf = m[0]
        jobs = job_graph.get(wf) or {}
        # DIRECT ownership: the anchoring sampled job's name token-equals the check (exact tier,
        # not a scoped token-SUBSET) — this workflow definitively RAN a job producing this exact
        # check, so its own matrix template legitimately owns it and a same-named reusable caller
        # in ANOTHER file must not steal it (issue #118 / PR #126 5th review). BUT exact timing only
        # proves the WORKFLOW ran the check, not that the matrix TEMPLATE (vs a co-resident reusable
        # caller) owns it: if a reusable caller IN THIS SAME workflow claims the check as its
        # `<caller> / <child>` leaf, it keeps precedence over the leading-placeholder matrix template
        # (PR #126 6th review), so direct-ownership only bypasses the refusal for a FOREIGN caller.
        # The refusal below therefore applies to a WEAKER subset anchor OR a same-workflow reusable
        # collision — never to a lone matrix producer that directly sampled the check.
        direct_owned = (_struct_toks(m[1]) == _struct_toks(check)
                        and not _reusable_caller_claims(check, {wf: jobs}))
        for jid, info in jobs.items():           # exact display-name match
            if (info.get("name") or jid) == check:
                return (wf, jid)
        for jid, info in jobs.items():           # matrix-template match
            nm = info.get("name") or jid
            # Same leading-placeholder refusal as the scanned/static resolvers, but only for a
            # NON-direct (token-subset) anchor: `_map_check_to_job` anchors a WORKFLOW, so a subset
            # match could land in a file whose foreign `${{…}} / build` template-matches
            # `Suite / build` and preempts a genuine reusable `Suite` caller. Refuse only on a real
            # reusable collision (`_reusable_caller_claims`) AND when this file doesn't directly own
            # the check — so a lone `${{ variant }} / build` producing `linux / build`, and a file
            # that directly sampled the check, both still bind (issue #118, PR #126 review).
            if (not (not direct_owned
                     and _name_template_leads_with_placeholder(nm)
                     and _reusable_caller_claims(check, job_graph))
                    and _name_template_regex(nm).match(check)):
                return (wf, jid)
        # Static-name matrix job: GitHub appends ` (<leg…>)` to a literal `name:` with no
        # `${{ matrix.* }}` placeholder (`Matrix Test` → check `Matrix Test (1.13.*)`).
        for jid, info in jobs.items():           # static-name matrix-leg match
            if _static_matrix_leg_match(info.get("name") or jid, info.get("matrix"), check):
                return (wf, jid)
    # 2. Same-workflow templated/plain match WITHOUT a sampled anchor (the scan saw the job
    #    but a triage-skipped workflow left no sampled job to anchor). Scan-only, template-
    #    aware, and bails to None on cross-file ambiguity — this binds a SINGLE-producer
    #    triage-skipped templated check like `test / ${{ matrix.type }}` → `test / ethereum`
    #    when its one workflow's jobs weren't fetched (issue #118). When >1 workflow produces
    #    the same expanded name (reth's real case: unit.yml AND integration.yml), it stays
    #    None here and the pole is stamped `ambiguous_workflows` downstream, not bound.
    scanned = _check_to_job_node_scanned(check, job_graph)
    if scanned is not None:
        return scanned
    # 3. LAST RESORT: reusable-workflow child. Only reached when NO same-workflow job (plain
    #    or templated) produced the check, so a job whose own name contains `" / "` already
    #    won above (issue #118); a genuine `<caller> / <child>` name resolves here.
    for wf, jobs in job_graph.items():
        for jid, info in jobs.items():
            if not info.get("reusable"):
                continue
            nm = info.get("name") or jid
            if check == nm or check.startswith(nm + " / "):
                return (wf, jid)
    return None


def _check_to_workflow_file_static(
    check: str, job_graph: dict[str, dict[str, dict[str, Any]]] | None,
) -> str | None:
    """Resolve a check-run name to its producing workflow FILE from ONLY the static
    job graph (the scanned YAML), independent of sampled timing.

    The timing-based `_map_check_to_job` returns None when a workflow was triaged out
    of job-fetching (a fast lint/validate workflow under the wall-clock floor) — it has
    no sampled job to name-match. But the producing file IS known from the scan, so a
    structural finding for that pole can still anchor its REAL `workflow_file` instead of
    a name-derived stub. Returns the file iff EXACTLY ONE workflow's job matches the check
    by display name (exact, or matrix-`name:` template); returns None on zero or
    cross-file ambiguous matches, so an unresolvable check stays honestly unanchored
    rather than bound to the wrong file. Like `_check_to_job_node_scanned`, it refuses a
    degenerate template AND (for a `" / "`-bearing check) a LEADING-placeholder template
    (`${{ matrix.variant }} / build` → `^.+? / build$`), whose `.+?` would eat across the
    `" / "` and impersonate a reusable `<caller> / <child>` check-run, mis-anchoring a
    genuine reusable check to a foreign matrix workflow (issue #118 / PR #126 review)."""
    if not job_graph:
        return None
    hits: set[str] = set()
    for wf, jobs in job_graph.items():
        for jid, info in jobs.items():
            nm = info.get("name") or jid
            if (nm == check
                    or (not _name_template_is_degenerate(nm)
                        and not (_name_template_leads_with_placeholder(nm)
                                 and _reusable_caller_claims(check, job_graph))
                        and _name_template_regex(nm).match(check))
                    or _static_matrix_leg_match(nm, info.get("matrix"), check)):
                hits.add(wf)
                break
    return next(iter(hits)) if len(hits) == 1 else None
def _check_to_job_node_scanned(
    check: str, job_graph: dict[str, dict[str, dict[str, Any]]],
) -> tuple[str, str] | None:
    """Map a check to its (workflow_file, job_id) using ONLY the scanned job graph's job
    NAME templates — no sampled timing. Fallback for `_check_wf` when a check's workflow was
    TRIAGE-SKIPPED (judged too fast to hold the pole, so its jobs weren't fetched): the
    timing mapper then misses it and it would be mislabeled fileless/external, even though
    the scanner saw the workflow. A gate matrix check (`Python 3.9`) thus still ties to its
    editable `test-suite.yml` `tests` job. Exact display-name first, then matrix-template
    regex, across all workflows. A genuinely fileless check (a CLA/AI bot, or a Netlify-managed
    `Redirect rules` / `Header rules` check with no workflow job) matches nothing → returns None →
    stays fileless, as it should. A job whose `name:` is an ENTIRELY-placeholder matrix template
    (`${{ matrix.target }}`) is a match-ANYTHING regex (`^.+?$`) that would otherwise grab such a
    foreign check; `_name_template_is_degenerate` refuses it here (that job's own legs resolve via
    the sampled-timing anchor a foreign check never reaches), so a degenerate template can no
    longer fabricate a file binding for a managed/external check.

    Both passes bail to None on cross-workflow ambiguity: without the sampled-timing anchor
    there's no way to disambiguate the same job name living in two files (two workflows each
    declaring a `test` job → two check-runs both literally named `test` — which is also why
    GitHub's required-status config can't tell them apart), so binding the wrong file (the
    worse failure for the auto-fixer) must be refused, not guessed."""
    exact = [(wf, jid) for wf, jobs in job_graph.items()
             for jid, info in jobs.items()
             if (info.get("name") or jid) == check]
    if exact:
        return exact[0] if len({wf for wf, _ in exact}) == 1 else None
    # Matrix-template regex. Without the sampled-timing anchor this can't disambiguate
    # same-shaped templates across files (`test (${{matrix.node}})` in one workflow vs
    # `test (${{matrix.python}})` in another both expand to `test (.+?)`), so if the
    # template pass matches in MORE THAN ONE workflow, stay None (honestly fileless)
    # rather than confidently bind the wrong file — the worse failure for the auto-fixer.
    # A LEADING-placeholder template (`${{ matrix.variant }} / build` → `^.+? / build$`) is
    # refused ONLY when a genuine reusable caller actually competes for the check
    # (`_reusable_caller_claims`): its `.+?` can eat across the `" / "` boundary and impersonate a
    # reusable `<caller> / <child>` name, stealing that caller's check for an unrelated foreign
    # matrix job (issue #118 follow-up). But when NO reusable caller claims the check, the
    # leading-placeholder job is its sole real producer (`linux / build` from a lone
    # `${{ matrix.variant }} / build`) and the match must stand or a real matrix job reads fileless.
    # A refused job's OWN legs still resolve via the workflow-scoped sampled anchor, which never
    # crosses to a foreign caller; leaving the reusable `" / "` split (the last resort) to win.
    tmpl = [(wf, jid) for wf, jobs in job_graph.items()
            for jid, info in jobs.items()
            if ((not _name_template_is_degenerate(info.get("name") or jid)
                 and not (_name_template_leads_with_placeholder(info.get("name") or jid)
                          and _reusable_caller_claims(check, job_graph))
                 and _name_template_regex(info.get("name") or jid).match(check))
                or _static_matrix_leg_match(info.get("name") or jid, info.get("matrix"), check))]
    if not tmpl or len({wf for wf, _ in tmpl}) > 1:
        return None
    return tmpl[0]


def _required_reachable_jobs(
    req_names: "frozenset[str] | set[str]",
    job_graph: dict[str, dict[str, dict[str, Any]]],
    crit_by_wf: dict[str, dict[str, Any]],
) -> "set[tuple[str, str]]":
    """The (workflow_file, job_id) nodes that are merge-blocking: the jobs the required
    checks map to (anchors), ∪ their downward `needs:` closure (jobs an anchor transitively
    depends on). Shared by `_required_reachable_checks` and `_pole_is_required_reachable`
    so the spine narrowing and the per-pole provenance check use the SAME reachability."""
    anchors: set[tuple[str, str]] = set()
    for r in req_names:
        node = _check_to_job_node(r, job_graph, crit_by_wf)
        if node is not None:
            anchors.add(node)
    reachable: set[tuple[str, str]] = set(anchors)
    stack = list(anchors)
    while stack:
        wf, jid = stack.pop()
        for dep in (job_graph.get(wf, {}).get(jid, {}) or {}).get("needs", []):
            node = (wf, dep)
            if dep in job_graph.get(wf, {}) and node not in reachable:
                reachable.add(node)
                stack.append(node)
    return reachable


def _pole_is_required_reachable(
    pole_check: str | None,
    req_names: "frozenset[str] | set[str]",
    job_graph: dict[str, dict[str, dict[str, Any]]] | None,
    crit_by_wf: dict[str, dict[str, Any]],
) -> bool:
    """Is the HEADLINED pole genuinely merge-blocking — a required check, or a job the
    required work `needs:`? This separates the cat-1/cat-2 keeps from the cat-3
    'file-backed but unpinnable to a job id' keep that `_required_reachable_checks`
    RETAINS (never silently drop a sampled check) but which may NOT gate the merge.

    It keeps `provenance=required_scoped` honest: the spine can be `spine_required_scoped`
    while its slowest surviving check is an unconfirmed cat-3 keep (matrix / reusable-
    workflow display-name mismatch) — stamping that pole `required_scoped` is the langfuse
    class narrowed, not closed (the harness would then consume it without HALT). Returns
    True (don't downgrade) when reachability can't be computed (no graph / no required
    set) — that path is already gated by `spine_required_scoped`."""
    if not pole_check or not req_names or not job_graph:
        return True
    if pole_check in req_names:
        return True
    node = _check_to_job_node(pole_check, job_graph, crit_by_wf)
    return node is not None and node in _required_reachable_jobs(
        req_names, job_graph, crit_by_wf)


def _required_reachable_checks(
    candidate_checks: "frozenset[str] | set[str] | list[str]",
    req_names: "frozenset[str] | set[str]",
    job_graph: dict[str, dict[str, dict[str, Any]]] | None,
    crit_by_wf: dict[str, dict[str, Any]],
) -> set[str]:
    """The subset of `candidate_checks` that is **merge-blocking**: a required check, or
    a job the required work transitively `needs:`. This is the accurate signal for
    "would speeding this move time-to-merge" — `needs:`-reachability, not file
    co-residence (which over-includes an independent sibling sharing a required file) and
    not a name prefix (which collapses a bare-aggregator gate).

    Mechanism (repo-agnostic, from the YAML `needs:` graph):
      1. **Anchors** = the jobs the required checks map to (a reusable caller is an anchor
         when any of its `<caller> / *` children is required — that's the merge-reports
         rollup pattern).
      2. **Reachable jobs** = anchors ∪ their downward `needs:` closure (everything an
         anchor depends on — e.g. a required reusable invocation that `needs: [changes,
         build]` pulls those in; a required aggregator that `needs:` its test jobs pulls
         those in).
      3. A candidate is kept iff it is required, OR maps to a reachable job, OR (safe
         direction) is produced by a sampled workflow job we couldn't pin to an id — a
         file-backed check is never silently dropped. A fileless/external check (no
         workflow job at all, e.g. a managed bot) that isn't required is dropped: it
         doesn't gate the merge.

    With no usable graph, falls back to literal required membership (can't compute
    reachability — degrade to the conservative subset rather than guess)."""
    candidate_checks = set(candidate_checks)
    if not req_names or not job_graph:
        return {c for c in candidate_checks if c in req_names}

    reachable = _required_reachable_jobs(req_names, job_graph, crit_by_wf)
    kept: set[str] = set()
    for c in candidate_checks:
        if c in req_names:
            kept.add(c)
            continue
        node = _check_to_job_node(c, job_graph, crit_by_wf)
        if node is not None:
            if node in reachable:
                kept.add(c)
        elif _workflows_matching_check(c, crit_by_wf):
            # File-backed but unpinnable to a job id — keep (never silently drop a
            # check a sampled workflow produced); only fileless/external checks fall
            # through to "dropped". Probe the AMBIGUITY-AWARE full match set, not the
            # single-pick `_map_check_to_job` (which now bails to None on cross-workflow
            # same-name collisions, issue #59) — otherwise a duplicated monorepo gate
            # would be mis-dropped here as "non-required" the moment a required set
            # resolves, silently undoing the spine's `_check_grounded_job_p50` crowning.
            # Byte-identical for unambiguous checks (a single-workflow match is a
            # one-element set, exactly `_map_check_to_job is not None`).
            kept.add(c)
            logger.debug("required-scope: kept file-backed but unresolved check %r", c)
    return kept


def _required_suite_all_external(
    required_checks: "RequiredChecks | None",
    req_names: "frozenset[str] | set[str]",
    crit_by_wf: dict[str, dict[str, Any]],
    job_graph: dict[str, dict[str, dict[str, Any]]] | None,
) -> bool:
    """True when a COMPLETE, non-empty required suite resolved but NOT ONE required check
    anchors a file-backed job in this repo — every required check is external/managed (a
    Socket Security app, a CLA bot posting commit statuses) with no workflow file to drill.

    This is the exact condition under which `_scope_spine_to_required` stays inert via its
    all-external guard (so `spine_required_scoped` is False and there is no file-backed
    merge-blocking path to scope to). The PR-floor fallback consumes this to demote the
    file-backed poles even when the external suite WAS observable on every sampled PR —
    i.e. `required_suite_unsatisfiable` is False, the case the unsatisfiable-keyed fallback
    branch cannot catch. Without it, a non-required file-backed check (Test/Lint) headlines
    "why is the merge slow?" though it gates zero merges.

    Needs a job graph to tell external from file-backed; without one we can't prove
    external, so return False (conservative — leaves the spine as an unresolved best guess
    rather than over-claiming a PR-floor). Shares `_check_to_job_node` with the scoper, so
    the two never disagree on which case a repo is in."""
    if not (required_checks is not None and required_checks.complete
            and req_names and job_graph):
        return False
    return not any(
        _check_to_job_node(r, job_graph, crit_by_wf) is not None for r in req_names)


def _scope_spine_to_required(
    pr_check_p50: dict[str, float],
    required_checks: "RequiredChecks | None",
    req_names: "frozenset[str] | set[str]",
    crit_by_wf: dict[str, dict[str, Any]],
    job_graph: dict[str, dict[str, dict[str, Any]]] | None,
    required_suite_unsatisfiable: bool,
) -> tuple[dict[str, float], list[str], bool]:
    """Restrict the critical-path spine to merge-blocking (required-reachable) checks,
    returning (kept_p50, dropped_sorted, scoped). The pole is the slowest thing on the spine,
    so an unscoped spine lets a non-required check headline "why is the merge slow?"
    even though speeding it moves zero time-to-merge.

    `scoped` is True ONLY when the active narrowing below actually fired (a complete,
    satisfiable required set with a job graph and a file-backed required anchor); it is False
    on every inert early-return. A caller needs this to tell a genuinely required-scoped spine
    (every surviving check is required-reachable) from one left unchanged — the sampling-level
    `required_suite_scoped` flag does NOT carry that distinction (it's True on a partial /
    anchorless read where this function stayed inert and the spine still holds non-required
    checks).

    Inert (returns the input unchanged, no drops) unless a real required set resolved
    that we can trust to be EXHAUSTIVE for the sampled checks:
      - `required_checks is None` / empty → required status unknown; don't drop.
      - `not required_checks.complete` → a PARTIAL read; an absent check is UNKNOWN,
        not not-required, so dropping it could discard a check an unread ruleset gates.
      - `required_suite_unsatisfiable` → the required suite ran on no sampled PR, so its
        names aren't in `pr_check_p50` at all (the PR-floor fallback renders here);
        filtering would empty the spine.
      - **no required check anchors a file-backed job** → every required check is
        external/managed (a Socket Security / CLA bot with no workflow file in this repo),
        so there is no file-backed merge-blocking path to scope to. The PR-floor fallback
        is the designed handler for that external-gate case (it renders the file-backed
        floor + a demotion banner); scoping here would drop the very file-backed work the
        fallback then re-surfaces, contradicting it. Stay inert and let the fallback own it.
    Also never empties the spine on an over-tight match (`if not keep`).

    With no usable job graph (a degraded scan, or a findings doc predating the
    `workflow_job_graph` key) reachability can't be computed at all, so stay inert and
    keep the whole spine - the safe direction, and consistent with
    `_required_reachable_checks`'s own no-graph fallback."""
    if not (required_checks is not None and required_checks.complete and req_names
            and not required_suite_unsatisfiable and job_graph):
        return pr_check_p50, [], False
    # The required suite must anchor at least one in-repo job for reachability to mean
    # anything; an all-external required set (no required check maps to a workflow job)
    # belongs to the PR-floor fallback, not this filter.
    if not any(_check_to_job_node(r, job_graph, crit_by_wf) is not None for r in req_names):
        return pr_check_p50, [], False
    keep = _required_reachable_checks(pr_check_p50, req_names, job_graph, crit_by_wf)
    if not keep:
        return pr_check_p50, [], False
    dropped = sorted(set(pr_check_p50) - keep)
    return {n: v for n, v in pr_check_p50.items() if n in keep}, dropped, True


def _is_pr_gate_check(
    check_name: str, crit_by_wf: dict[str, dict[str, Any]],
    events_by_wf: dict[str, set[str]], pr_workflows: frozenset[str] = frozenset(),
    req_names: "frozenset[str] | set[str]" = frozenset(),
) -> bool:
    """Is this check-run something a developer actually waits on to MERGE a PR?

    A check that maps to a workflow which only fires on push / schedule (e.g. a
    deploy-to-staging or release job) is NOT a PR gate: its check-run can attach
    to a sampled commit SHA (post-merge `main`, a release tag) and look like part
    of the critical path, but the developer never waits on it to merge. Those are
    dropped from the measured critical path.

    A check is KEPT (err toward keeping - never silently hide a real gate) when:
      - it is FILELESS (no sampled job maps to it - CodeQL default setup, a GitHub
        App / AI review bot): a genuine PR check-run we can't map to a workflow; OR
      - its workflow's DECLARED triggers include a PR event (`pr_workflows`, read
        from the workflow's `on:` block - ground truth, independent of which runs
        the success-only sample happened to catch); OR
      - the OBSERVED sampled events include a PR event.
    Only a check we can POSITIVELY tie to a non-PR workflow (declared push/schedule
    only AND no observed PR event) is dropped - so a real PR gate whose recent
    successes were all push runs is not excised on a sampling artifact."""
    # A REQUIRED check is merge-blocking by definition — never drop it, even if its
    # workflow only fires on `push` (a push-triggered status that satisfies the PR's
    # required-status gate). Dropping it would silently zero a confirmed merge gate.
    if check_name in req_names:
        return True
    # encord §6 Cause 1: a check name can be defined by a job in MORE THAN ONE workflow —
    # e.g. `Run integration tests` in both a `pull_request` workflow (the real PR gate) AND
    # a `push`-only one. Decide from the FULL set of workflows whose sampled job matches this
    # check (`_workflows_matching_check`), NOT the single slowest `_map_check_to_job` mapping:
    # keep the check if ANY of them is PR-triggered (declared `on:` PR event, or an observed
    # PR event). Using the mapper's single pick would drop a genuine PR gate whenever the
    # push-only sibling happened to be slower — and now that the mapper BAILS on cross-workflow
    # same-name ambiguity (issue #59), a `mapping is None` short-circuit could no longer tell a
    # genuinely fileless check apart from an ambiguous file-backed one. The empty set IS the
    # fileless case (no sampled job maps to it — CodeQL default setup, an app/bot check): keep
    # it (err toward keeping — never silently hide a real gate).
    matching = _workflows_matching_check(check_name, crit_by_wf)
    if not matching:
        return True  # fileless / unmapped check-run we can't place — safe keep
    for wf_path in matching:
        if wf_path in pr_workflows or ((events_by_wf.get(wf_path) or set()) & _PR_VOLUME_EVENTS):
            return True
    return False


def _apply_structural_cascade(
    f: dict[str, Any], raw_wc: float, wf_path: str,
    crit: dict[str, Any], events_by_wf: dict[str, set[str]],
    pr_checks_tuple: tuple[tuple[str, float], ...],
    pr_check_populations: list,
    own_check_names: "frozenset[str] | None" = None,
    chain_members: frozenset = frozenset(),
    chain_p50_s: float = 0.0,
    chain_win_s: float = 0.0,
    cluster_floor_lever: bool = False,
) -> None:
    """Run a structural candidate's RAW wall-clock estimate through the SAME
    cross-cutting bound cascade every other finding uses (developer-facing gate +
    measured population-weighted critical-path floor + cross-workflow floor), so
    structural savings are floor-capped and population-weighted, never a
    single-PR best case. Stamps wall_clock_p50_s + derivation in place.

    `own_check_names` scopes the measured-critical-path floor to the leg(s) THIS
    finding actually speeds up. The floor uses max(own) as the headroom ceiling
    (own_max − slowest_other). If `own` is the WHOLE workflow's job set, a
    non-pole matrix leg (e.g. `Unit Tests (delete)`, 164s) inherits the
    workflow's TOP pole (`Unit Tests (wal)`, 200s) as its own_max and dodges the
    floor — crediting wall-clock to a concurrent leg that finishes BEFORE the
    gate, and mislabeling it the critical path. Passing the finding's own leg(s)
    puts the slower sibling into `others`, so a non-gating leg floors to 0. When
    None (legacy callers), defaults to the whole workflow's job set."""
    if own_check_names is None:
        own_check_names = frozenset((crit.get("job_p50") or {}).keys())
    else:
        # Keep only legs actually present on the measured PR critical path; if none
        # of the finding's legs resolve to a sampled check there, fall back to the
        # workflow's job set rather than zero a real saving on an unmatched name.
        pr_names = {n for n, _ in pr_checks_tuple}
        scoped = frozenset(c for c in own_check_names if c in pr_names)
        own_check_names = scoped or frozenset((crit.get("job_p50") or {}).keys())
    # The measured-critical-path bound floors against the full pr_checks set, so
    # the cross-workflow fallback (which needs `concurrent`) is a no-op here.
    ctx = WallClockContext(
        workflow=wf_path, crit=crit,
        affected_jobs=tuple(f.get("affected_jobs") or []),
        events=tuple(sorted(events_by_wf.get(wf_path) or ())),
        pr_checks=pr_checks_tuple, own_check_names=own_check_names,
        pr_check_populations=tuple(pr_check_populations),
        chain_members=chain_members, chain_p50_s=chain_p50_s,
        chain_win_s=chain_win_s, cluster_floor_lever=cluster_floor_lever)
    res = size_wall_clock(float(raw_wc), ctx)
    f["wall_clock_p50_s"] = res.effective_s
    if res.derivation:
        f["wall_clock_uncapped_p50_s"] = res.raw_s
        f["wall_clock_derivation"] = [
            {"bound": b, "from_s": a, "to_s": c, "reason": why}
            for b, a, c, why in res.derivation
        ]
        note = "; ".join(d[3] for d in res.derivation)
        prev = f.get("size_note") or ""
        f["size_note"] = f"{prev}; {note}" if prev else note
    if res.effective_s <= 0:
        f["tier"] = 2
        f["realization"] = "none"


def _structural_raw_estimate(
    decomp: dict[str, Any],
) -> tuple[float, str]:
    """Conservative RAW wall-clock estimate for attacking a job's dominant lever
    (the dominant step CATEGORY — which may span several comparable steps, see
    `_decompose_job_steps`). Returns (raw_seconds, assumption_note). Floored at a
    warm/scope target so it never claims the whole phase; the cascade floors it
    further."""
    cat = decomp["dominant_category"]
    dom = decomp["dominant_p50"]
    # How many comparable steps the dominant category spans. The warm/cached floor is
    # per step instance, so a multi-step phase keeps one floor PER step — subtracting a
    # single floor from the aggregate would under-floor and over-claim the saving.
    n_dom_steps = sum(1 for _n, c, _p in decomp.get("steps", []) if c == cat) or 1
    if cat == "test":
        raw = dom * _STRUCT_TEST_SCOPE_FRACTION
        phase = "test phase" if n_dom_steps > 1 else "test step"
        return round(raw, 1), (
            f"conservative: assumes scoping removes at most "
            f"{_STRUCT_TEST_SCOPE_FRACTION*100:.0f}% of the {dom:.0f}s {phase} "
            f"on the average PR (never the single best-case PR)")
    floor = _STRUCT_WARM_FLOOR_S.get(cat, 10.0) * n_dom_steps
    raw = max(dom - floor, 0.0)
    phase = f"{cat} phase ({n_dom_steps} steps)" if n_dom_steps > 1 else f"{cat} step"
    return round(raw, 1), (
        f"{phase} {dom:.0f}s above a ~{floor:.0f}s warm/cached floor")


# How deep into the measured critical path the per-check structural router
# looks. Recorded in the findings JSON (`structural_analysis_top_n`) so the
# report can tell "analyzed, genuinely inherent cost" apart from "ranked below
# this depth, never structurally analyzed" — a check beyond this depth with no
# lever is NOT proven inherent, it just wasn't examined.
_STRUCTURAL_TOP_N = 5
# Distinct job matrices the report drills (gate + next distinct by impact); matches
# blocking_path._TOP_WORKFLOWS. Their gates are always decomposed + logged.
_DRILL_DISTINCT_MATRICES = 2

# Adaptive 2-pass run sampling. The dominant gh cost is one `GET /runs/{id}/jobs`
# per sampled run; sampling all workflows at full --max-runs is wasteful because a
# check's IDENTITY is stable by ~10 runs while only its exact p50 needs the full
# sample. So: shallow-fetch jobs for the first `_SHALLOW_RUNS` runs of every
# workflow, then DEEPEN (to full --max-runs) the workflows that own the top
# `_DEEPEN_TOP_CHECKS` concurrent checks — iterating to convergence so the entire
# RENDERED report (gate, drill-set, the cross-workflow floor, the Level-1
# concurrent-check chart, and bimodal flags) is depth-invariant == a full pass. Only
# the per-run JOB fetch is depth-gated — the run-list (1 call/wf), monthly volume,
# events, and the PR-sha pool stay at full breadth. Off-path hygiene runner-minutes
# (summed over ALL workflows) stay shallow for non-deepened workflows — disclosed in
# the provenance, never silent. Validated by Monte-Carlo subsampling + end-to-end
# diff vs a full pass on mastra/ollama/better-auth/opentrons/mattermost.
# `_SHALLOW_RUNS >= --max-runs` disables the two-pass (one full pass).
_SHALLOW_RUNS = 10
# Run-list triage floor. The run-list (1 call/workflow, already fetched) carries each
# run's created_at/updated_at, so a run's wall-time is known WITHOUT a per-run job fetch
# (the dominant gh cost). A workflow whose SLOWEST sampled run finishes under this floor
# cannot hold a merge long pole — that lives in the minutes-scale workflows — so its per-run
# job fetch is skipped. It CAN still be a concurrent sibling on the PR, so the triage stub
# carries `concurrent_wall_p50` (its run-list wall) and the cross-workflow floor still counts
# it (`wall_clock._concurrent_workflows`) — the floor is preserved, not dropped. Conservative
# on purpose: gated on the MAX wall-time over the SAMPLED WINDOW (the shallow slice we'd fetch
# anyway — so a workflow that ran long anywhere in that window is fetched) and set far below
# any plausible pole, so the gate/pole/floor/bimodal stay exact;
# only the skipped workflow's job-level hygiene/queue degrade to run-list-only, disclosed
# in `triaged_fast_workflows` (never silent). Validated by measured before/after that the
# gate/pole/floor are unchanged vs a full pass (ARCHITECTURE §2.1).
#
# This absolute 90s floor is only a COARSE, fetch-cheap PRE-filter: on a seconds-scale repo
# the measured gate can itself sit at/under 90s (e.g. roboflow/supervision's 85s gate), so a
# workflow under the floor is NOT automatically too small to matter — its check can still be
# the second-ranked concurrent check (a drilled secondary pole) and the binding wall-clock
# FLOOR the headline buys the gate down to. Triaging it on the absolute floor alone would
# dismiss it as "can't hold the merge pole" while the report silently uses its check as the
# headline's floor and drills a LOWER check instead. So after the shallow pass measures the
# gate, `collect()` RECOVERS (job-fetches) any triaged workflow whose run-list wall reaches
# `_TRIAGE_RECOVER_GATE_FRAC` of the gate — making triage relative to the measured pole rank,
# not the absolute constant (a minutes-scale gate is far above the floor, so nothing under it
# qualifies and triage is fully preserved == zero behavior change there).
_TRIAGE_WALLCLOCK_FLOOR_S = 90.0
# Relative triage-recovery band (see `_TRIAGE_WALLCLOCK_FLOOR_S`). A triaged workflow whose
# run-list wall is >= this fraction of the measured gate's long pole is RECOVERED (job-fetched
# + ranked + drilled like any pole) instead of staying dismissed as too-fast-to-matter, so the
# drilled secondary poles and the headline's binding floor are derived from the SAME pole rank.
# Far below the gate on a minutes-scale repo (a 50s lint vs a 1400s gate is ~3.5%), so no
# sub-floor workflow is recovered there; it only bites when the gate is near the 90s floor.
_TRIAGE_RECOVER_GATE_FRAC = 0.5
# A spine check present on AT MOST this fraction of sampled PRs is "rare" (opt-in /
# label-gated / path-conditional) and is ranked BELOW the checks a typical PR runs — so the
# headline pole is the common merge gate, not a rare giant (a label-gated benchmark) that's
# only the slowest thing when it happens to run. Mirrors the renderer's typical/minority
# split. A required check gates by definition and is exempt.
_RARE_PRESENCE_FRAC = 0.5
# Below this many sampled PRs the presence fraction is too noisy to demote on — keep the
# plain p50 order rather than guess a check is rare from a tiny sample.
_RARE_PRESENCE_MIN_PR = 6
# The spine is ranked by ACTUAL-CRITICAL-PATH FREQUENCY (how often a check is the slowest job a
# PR waits on), not mere presence. A check that is the actual pole on FEWER than this many
# sampled PRs is a one-path outlier and demoted below the genuine recurring gates. Why this and
# NOT presence>50%: on a path-partitioned monorepo (expo/expo) every heavy suite runs on a
# MINORITY of PRs (each PR touches only part of the tree), so a presence>50% cutoff demotes the
# ENTIRE heavy-job set and crowns a lightweight always-present check that is the actual
# bottleneck on ZERO PRs. Recurrence-as-the-actual-pole is the honest test of "is this the merge
# gate?" A required check gates by definition and is exempt (never demoted).
_POLE_RECUR_FLOOR = 2
# How many top concurrent checks the deepen pass guarantees at full depth. Must be
# >= the renderer's Level-1 chart depth (blocking_path renders `src[:9]`), plus a
# buffer for infrequent giants the chart filters out of `typical` but that still rank
# high globally. The owners of these checks get deepened; matrix legs collapse to one
# workflow, so this rarely deepens this many distinct workflows.
_DEEPEN_TOP_CHECKS = 12
# PR18 introduced this selector as the v2 bill-pole preflight; PR32 accepted the
# convergence panel, and PR33 wires it into collection. Keep the ranking
# deterministic and source-block-derived so future panel reruns can compare the
# same bill-mass decision boundary.
_COST_DEEPEN_TOP_WORKFLOWS = 12
_BILL_GAP_TOP_WORKFLOWS = _COST_DEEPEN_TOP_WORKFLOWS
_BILL_GAP_TOP_JOBS = 8
_BILL_GAP_SCHEMA = "ci-speedup.bill-gap.workflow.v1"


def _bill_gap_slug(value: object) -> str:
    return re.sub(r"[^\w.-]+", "-", str(value)).strip("-") or "x"


def _bill_gap_skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _bill_gap_is_maintainer_source() -> bool:
    """True only in the tracked skills monorepo source, never an installed copy."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_bill_gap_skill_root()), "ls-files",
             "--error-unmatch", "scripts/collect_runs.py"],
            capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _bill_gap_root_default() -> Path | None:
    """Default local artifact root for bill-gap workflow captures.

    Installed skill copies return None so end-user runs never write local loop
    feedstock under the shipped skill directory. Maintainer source checkouts root
    under the repo-level `.ci-speedup-gaps/bill-workflows/` namespace, alongside
    but separate from log-backed phase-4b captures.
    """
    if not _bill_gap_is_maintainer_source():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(_bill_gap_skill_root()), "rev-parse",
             "--show-toplevel"],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()) / ".ci-speedup-gaps" / "bill-workflows"
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    logger.warning("bill-gap capture skipped: maintainer source detected but repo "
                   "root could not be resolved")
    return None


def _bill_gap_source_job_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _bill_gap_source_job_candidates(f: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        name = _bill_gap_source_job_name(value)
        if name and name not in seen:
            seen.add(name)
            out.append(name)

    for job in f.get("affected_jobs") if isinstance(f.get("affected_jobs"), list) else []:
        add(job)
    add(f.get("rerun_dominant_job"))
    burn = f.get("timeout_default_burn") if isinstance(f.get("timeout_default_burn"), dict) else {}
    add(burn.get("job_template"))
    add(burn.get("job_key"))
    for sample in burn.get("samples") if isinstance(burn.get("samples"), list) else []:
        sample_dict = sample if isinstance(sample, dict) else {}
        add(sample_dict.get("job_name"))
    return out


def _bill_gap_required_source_filters(f: dict[str, Any]) -> dict[str, str] | None:
    filters: dict[str, str] = {
        "status_filter": "success",
        "attempt_filter": "latest",
        "volume_filter": "all-status",
    }
    if str(f.get("pattern") or "") == "OPT64" or f.get("rerun_dominant_job"):
        filters.update({
            "status_filter": "all-status",
            "attempt_filter": "prior",
            "volume_filter": "all-status",
        })
    explicit = f.get("runner_minute_source_filter")
    explicit = explicit if isinstance(explicit, dict) else {}
    for key in ("event_scope", "status_filter", "attempt_filter", "volume_filter"):
        value = str(explicit.get(key) or f.get(key) or "").strip()
        if value:
            if key in filters and filters[key] != value:
                return None
            filters[key] = value
    return filters


def _bill_gap_row_matches_required_filters(row: dict[str, Any],
                                           filters: dict[str, str]) -> bool:
    return all(str(row.get(key) or "").strip() == expected
               for key, expected in filters.items())


def _bill_gap_source_rows_matching_jobs(rows: list[dict[str, Any]],
                                        jobs: list[str]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    seen: set[int] = set()
    for job in jobs:
        affected = _bill_gap_source_job_name(job)
        if not affected:
            continue
        exact = [
            row for row in rows
            if _bill_gap_source_job_name(row.get("job_name")) == affected
        ]
        candidates = exact or [
            row for row in rows
            if _matrix_base_name(_bill_gap_source_job_name(row.get("job_name"))) == affected
        ]
        for row in candidates:
            ident = id(row)
            if ident not in seen:
                seen.add(ident)
                matched.append(row)
    return matched


def _bill_gap_spine_source_rows(f: dict[str, Any],
                                spine_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wf = str(f.get("workflow_file") or "").strip()
    wide_opt64 = str(f.get("pattern") or "") == "OPT64"
    if not wf:
        return []
    rows = [
        row for row in spine_rows
        if str(row.get("workflow_file") or "").strip() == wf
    ]
    filters = _bill_gap_required_source_filters(f)
    if filters is None:
        return []
    rows = [row for row in rows if _bill_gap_row_matches_required_filters(row, filters)]
    if wide_opt64:
        # PR-Z (S2-1): mirror of the report twins' R1 WIDE binding
        # (blocking_path/_tier2_spine_source_rows) — without it, the bill-gap
        # capture decision disagrees with the report's promotion decision
        # (false gap feedstock, or a real gap suppressed). Dominant failing
        # job must be visible among the prior rows, exactly like the twins.
        dominant = _bill_gap_source_job_name(f.get("rerun_dominant_job"))
        if not dominant or not _bill_gap_source_rows_matching_jobs(rows, [dominant]):
            return []
        return rows
    jobs = _bill_gap_source_job_candidates(f)
    if jobs:
        rows = _bill_gap_source_rows_matching_jobs(rows, jobs)
    return rows


def _bill_gap_source_rows_cover_saving(f: dict[str, Any],
                                       rows: list[dict[str, Any]]) -> bool:
    saving = _finite_float(f.get("runner_min_saving")) or 0.0
    if saving <= 0 or not rows:
        return False
    if str(f.get("pattern") or "") == "OPT65":
        source_billable = sum(
            _finite_float(row.get("billable_equiv_min_per_month")) or 0.0
            for row in rows)
        if saving > source_billable + 0.011:
            return False
    else:
        source_raw = sum(
            _finite_float(row.get("raw_compute_runner_min_per_month")) or 0.0
            for row in rows)
        if saving > source_raw + 0.011:
            return False
    return True


def _bill_gap_is_tier2_finding(f: dict[str, Any]) -> bool:
    return (not f.get("advisory")
            and f.get("sizing_basis") == "measured"
            and isinstance(f.get("tier2_neutrality"), dict)
            and bool(f.get("tier2_neutrality"))
            and (_finite_float(f.get("runner_min_saving")) or 0.0) > 0)


def _bill_gap_opt64_group_cover_ok(f: dict[str, Any],
                                   findings: list[dict[str, Any]],
                                   spine_rows: list[dict[str, Any]]) -> bool:
    """PR-Z (S2-1): mirror of the report twins' sibling no-double-count guard
    (_tier2_opt64_group_cover_ok) — sibling OPT64 findings share ONE wide
    prior-row cover; a group over-claim means NONE is source-backed."""
    if str(f.get("pattern") or "") != "OPT64":
        return True
    rows = _bill_gap_spine_source_rows(f, spine_rows)
    if not rows:
        return False
    wf = str(f.get("workflow_file") or "").strip()
    sibs = [g for g in findings
            if isinstance(g, dict)
            and str(g.get("pattern") or "") == "OPT64"
            and str(g.get("workflow_file") or "").strip() == wf
            and _bill_gap_is_tier2_finding(g)]
    claimed = sum(_finite_float(g.get("runner_min_saving")) or 0.0 for g in sibs)
    tol = 0.011 + 0.05 * len(sibs)
    raw_total = sum(_finite_float(row.get("raw_compute_runner_min_per_month")) or 0.0
                    for row in rows)
    if claimed > raw_total + tol:
        return False
    return True


def _bill_gap_source_backed_workflows(findings: list[dict[str, Any]],
                                      spine_rows: list[dict[str, Any]]) -> set[str]:
    covered: set[str] = set()
    for f in findings:
        if not isinstance(f, dict) or not _bill_gap_is_tier2_finding(f):
            continue
        rows = _bill_gap_spine_source_rows(f, spine_rows)
        if (_bill_gap_source_rows_cover_saving(f, rows)
                and _bill_gap_opt64_group_cover_ok(f, findings, spine_rows)):
            wf = str(f.get("workflow_file") or "").strip()
            if wf:
                covered.add(wf)
    return covered


def _bill_gap_row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_name": row.get("job_name"),
        "runner_label": row.get("runner_label"),
        "billable_equiv_min_per_month": row.get("billable_equiv_min_per_month"),
        "raw_compute_runner_min_per_month": row.get("raw_compute_runner_min_per_month"),
        "sampled_workflow_run_count": row.get("sampled_workflow_run_count"),
        "sampled_job_occurrence_count": row.get("sampled_job_occurrence_count"),
        "occurrence_fraction": row.get("occurrence_fraction"),
        "event_scope": row.get("event_scope"),
        "status_filter": row.get("status_filter"),
        "attempt_filter": row.get("attempt_filter"),
        "volume_filter": row.get("volume_filter"),
        "sample_window_start": row.get("sample_window_start"),
        "sample_window_end": row.get("sample_window_end"),
    }


def _bill_gap_candidates_from_doc(
    doc: dict[str, Any],
    *,
    cap: int = _BILL_GAP_TOP_WORKFLOWS,
) -> list[dict[str, Any]]:
    """Top cost-spine workflows not already covered by source-backed Tier-2 findings."""
    if cap <= 0:
        return []
    data_sources = doc.get("data_sources") if isinstance(doc, dict) else None
    data_sources = data_sources if isinstance(data_sources, dict) else {}
    tiers_run = data_sources.get("tiers_run")
    if not isinstance(tiers_run, list) or "gh-timing" not in tiers_run:
        return []
    if "cost_spine_job_fetch_failures" not in data_sources:
        return []
    spine = doc.get("runner_minute_spine") if isinstance(doc, dict) else None
    if not isinstance(spine, dict) or spine.get("render_ready") is not True:
        return []
    raw_rows = spine.get("rows")
    if not isinstance(raw_rows, list) or not isinstance(spine.get("totals"), dict):
        return []
    rows = [row for row in raw_rows if isinstance(row, dict)]
    findings = doc.get("findings") if isinstance(doc.get("findings"), list) else []
    covered_workflows = _bill_gap_source_backed_workflows(findings, rows)
    by_workflow: dict[str, dict[str, Any]] = {}
    for row in rows:
        wf = str(row.get("workflow_file") or "").strip()
        billable = _finite_float(row.get("billable_equiv_min_per_month"))
        if not wf or wf in covered_workflows or billable is None or billable <= 0:
            continue
        entry = by_workflow.setdefault(wf, {
            "workflow_file": wf,
            "row_count": 0,
            "billable_equiv_min_per_month": 0.0,
            "raw_compute_runner_min_per_month": 0.0,
            "rows": [],
        })
        entry["row_count"] += 1
        entry["billable_equiv_min_per_month"] += billable
        entry["raw_compute_runner_min_per_month"] += (
            _finite_float(row.get("raw_compute_runner_min_per_month")) or 0.0)
        entry["rows"].append(row)
    candidates: list[dict[str, Any]] = []
    for wf, entry in by_workflow.items():
        top_rows = sorted(
            entry["rows"],
            key=lambda row: (-(_finite_float(row.get("billable_equiv_min_per_month")) or 0.0),
                             str(row.get("job_name") or "")))[:_BILL_GAP_TOP_JOBS]
        candidates.append({
            "workflow_file": wf,
            "rank_basis": "billable_equiv_min_per_month",
            "coverage_reason": "no source-backed Tier-2 finding covers this workflow",
            "row_count": entry["row_count"],
            "billable_equiv_min_per_month": _round3(entry["billable_equiv_min_per_month"]),
            "raw_compute_runner_min_per_month": _round3(entry["raw_compute_runner_min_per_month"]),
            "top_jobs": [_bill_gap_row_summary(row) for row in top_rows],
        })
    return sorted(
        candidates,
        key=lambda item: (-float(item["billable_equiv_min_per_month"]),
                          str(item["workflow_file"])))[:cap]


def _bill_gap_artifact(candidate: dict[str, Any],
                       doc: dict[str, Any]) -> dict[str, Any]:
    ds = doc.get("data_sources") if isinstance(doc.get("data_sources"), dict) else {}
    return {
        "schema": _BILL_GAP_SCHEMA,
        "schema_version": 1,
        "repo": doc.get("repo"),
        "commit_sha": doc.get("commit_sha"),
        "skill_commit_sha": ds.get("skill_commit_sha") or doc.get("skill_commit_sha"),
        "scanned_at": doc.get("scanned_at"),
        "sampled_runs_created_before": ds.get("sampled_runs_created_before"),
        "source": "runner_minute_spine",
        "promotion_contract": (
            "Local discovery feedstock only: this is not a detector and must not "
            "render as a finding or promote a detector without a later "
            "human-reviewed catalog/test PR grounded in real logs or equivalent "
            "deterministic evidence."),
        "candidate": candidate,
    }


def _replace_bill_gap_capture_dir(tmp: Path, dest: Path) -> None:
    """Atomically publish a fully-written capture directory.

    The temp dir already contains every required file. For a new capture, rename
    it into place. For an existing capture, swap through a hidden backup so a
    failed publish keeps the previous complete directory rather than exposing a
    half-written one.
    """

    def remove_path(path: Path) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            pass

    if not dest.exists():
        tmp.replace(dest)
        return

    backup = dest.with_name(
        f".{dest.name}.previous-{os.getpid()}-{time.time_ns()}")
    dest.replace(backup)
    try:
        tmp.replace(dest)
    except OSError:
        try:
            backup.replace(dest)
        except OSError:
            pass
        raise
    remove_path(backup)


def _capture_bill_gap_workflows(
    doc: dict[str, Any],
    *,
    gaps_root: Path | None = None,
    cap: int = _BILL_GAP_TOP_WORKFLOWS,
) -> list[Path]:
    candidates = _bill_gap_candidates_from_doc(doc, cap=cap)
    if not candidates:
        return []
    if gaps_root is None:
        gaps_root = _bill_gap_root_default()
    if gaps_root is None:
        return []
    repo_slug = _bill_gap_slug(doc.get("repo") or "repo")
    captured: list[Path] = []
    for candidate in candidates:
        wf = str(candidate.get("workflow_file") or "workflow")
        dest = gaps_root / f"{repo_slug}__{_bill_gap_slug(wf)}"
        tmp = gaps_root / f".{dest.name}.tmp-{os.getpid()}-{time.time_ns()}"
        try:
            tmp.mkdir(parents=True, exist_ok=False)
            artifact = _bill_gap_artifact(candidate, doc)
            (tmp / "bill-gap.json").write_text(
                json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            (tmp / "README.md").write_text(
                "# ci-speedup bill-gap workflow capture\n\n"
                "Maintainer-local feedstock from `runner_minute_spine`. This is "
                "not a detector, not a rendered claim, and not safe to commit.\n",
                encoding="utf-8")
            _replace_bill_gap_capture_dir(tmp, dest)
        except OSError as exc:
            logger.warning("bill-gap capture failed for %s: %s", wf, exc)
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        captured.append(dest)
    if captured:
        logger.info("captured %d bill-gap workflow candidate(s) under %s",
                    len(captured), gaps_root)
    return captured


def _accumulate_jobs(
    kept: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    jobs_per_run: list[list[dict[str, Any]]],
    jobs_by_event: dict[str, list[list[dict[str, Any]]]],
) -> tuple[int, int]:
    """Fold a batch of (run, jobs) into the per-run + per-event accumulators (the
    same shaping the single-pass loop did inline). Returns (runs_added, jobs_added)
    for the counters. Used by BOTH the shallow pass and the deepen pass, so the
    second pass extends the first's accumulators rather than re-fetching."""
    nr = nj = 0
    for run, jobs in kept:
        _stamp_run_context(run, jobs)
        ev = str(run.get("event") or "")
        jobs_per_run.append(jobs)
        if ev:
            jobs_by_event.setdefault(ev, []).append(jobs)
        nr += 1
        nj += len(jobs)
    return nr, nj


def _cost_deepen_candidates_from_spine(
    spine: dict[str, Any],
    cap: int = _COST_DEEPEN_TOP_WORKFLOWS,
) -> list[str]:
    """Top workflow candidates for the bill-pole deepening pass.

    Rank by summed billable-equivalent minutes/month from `runner_minute_spine`,
    not by rendered row order or dollars. That keeps public capacity, private
    spend, and unpriced runners on the same runner-minute mass axis while the
    convergence experiment decides whether paying for deeper samples is worth it.
    """
    if cap <= 0 or not isinstance(spine, dict):
        return []
    rows = spine.get("rows")
    if not isinstance(rows, list):
        return []
    by_workflow: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        wf = str(row.get("workflow_file") or "")
        billable = _finite_float(row.get("billable_equiv_min_per_month"))
        if not wf or billable is None or billable <= 0:
            continue
        by_workflow[wf] = by_workflow.get(wf, 0.0) + billable
    ranked = sorted(by_workflow.items(), key=lambda item: (-item[1], item[0]))
    return [wf for wf, _total in ranked[:cap]]


def _clone_events_jobs_by_wf(
    events_jobs_by_wf: dict[str, dict[str, list[list[dict[str, Any]]]]],
) -> dict[str, dict[str, list[list[dict[str, Any]]]]]:
    return {
        wf: {event: list(runs) for event, runs in by_event.items()}
        for wf, by_event in events_jobs_by_wf.items()
    }


def _clone_jobs_per_run_by_wf(
    jobs_per_run_by_wf: dict[str, list[list[dict[str, Any]]]],
) -> dict[str, list[list[dict[str, Any]]]]:
    return {wf: list(runs) for wf, runs in jobs_per_run_by_wf.items()}


def _stamp_run_context(run: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    # Tag each job with the RUN's created_at (trigger time). OPT43 wait-to-start is
    # measured from the trigger, not the job's own `created_at`: GitHub stamps a
    # GATED job's `created_at` when its `needs:` dependency resolves, so
    # `started - job.created` sees only this job's own pickup and HIDES the upstream
    # gating cost (the gating job's queue + run time) the developer also waited on.
    # OPT43 reads `_run_created_at` (see its docstring for the not-pure-queue caveat).
    run_created = run.get("created_at")
    ev = str(run.get("event") or "")
    rid = _run_id(run)
    # Head repo (owner/name) of the run — a fork PR's head repo differs from the
    # audited repo, and a fork run runs colder (no repo secrets, so a secrets-gated
    # remote cache is unreachable to it). Stamped here (free —
    # already on the run object) so the cache-distribution grounding can exclude fork
    # PRs from the upstream hit-rate median without a second fetch.
    head_repo = str((run.get("head_repository") or {}).get("full_name") or "")
    # Head SHA of the run — the commit whose workflow-file CONTENT executed. Stamped here
    # (free — already on the run object) so `_stamp_pole_repr_run_era` can content-classify a
    # drilled representative run's config era (issue #77), not just timestamp it.
    head_sha = str(run.get("head_sha") or "")
    for job in jobs:
        job["_run_created_at"] = run_created
        job["_run_event"] = ev
        job["_run_id"] = rid
        job["_run_head_repo"] = head_repo
        job["_run_head_sha"] = head_sha


def _crit_for(
    jobs_per_run: list[list[dict[str, Any]]],
    jobs_by_event: dict[str, list[list[dict[str, Any]]]],
) -> tuple[dict[str, Any], list[list[dict[str, Any]]]]:
    """Event-scoped critical path for a workflow: the developer-facing event's runs
    (PR, else merge_group) if present, else all runs — `_critical_path` then scopes
    each job to its own dominant runner. Returns (crit, crit_runs)."""
    dev_event = _developer_event(jobs_by_event)
    crit_runs = jobs_by_event[dev_event] if dev_event else jobs_per_run
    crit = _critical_path(crit_runs)
    crit["event_scope"] = dev_event or "all-events"
    return crit, crit_runs


def _deepen_check_keys(crit: dict[str, Any]) -> list[float]:
    """The per-workflow values that could rank into the rendered concurrent-check
    chart (and thus must be full-depth): every job's p50 (the chart sorts checks by
    p50), PLUS the long-pole's p95 (so a fast-median / high-tail bimodal gate, whose
    slow mode is the whole reason it gates, isn't buried below the deepen cut by its
    median). The deepen loop ranks these ACROSS workflows and deepens the owners of
    the top keys, so every check the report renders is measured at full depth."""
    keys = list((crit.get("job_p50") or {}).values())
    keys.append(float(crit.get("long_pole_p95") or 0.0))
    return keys


def _deepen_candidates(crit_by_wf: dict[str, dict[str, Any]],
                       events_by_wf: dict[str, set[str]]) -> set[str]:
    """Workflows ELIGIBLE for deepening: those a developer waits on to merge,
    determined by FULL-BREADTH observed events (`events_by_wf`, built from all sampled
    runs), NOT the shallow `event_scope`. Keying off the shallow scope was a bug — a
    `[push, pull_request]` workflow whose 10 newest runs are all push would read
    `all-events`, be excluded, and get measured against push runs a full pass would
    PR-scope. Full-breadth eligibility forces any potential PR gate into the deepen
    pass, where its own full sample re-derives the correct scope. Push/schedule-only
    workflows never gate a merge and stay shallow."""
    devset = set(_DEVELOPER_EVENTS)
    return {wf for wf, c in crit_by_wf.items()
            if (events_by_wf.get(wf) or set()) & devset
            and (c.get("long_pole_p50") or 0) > 0}


def _pole_mapping(check_name: str, crit_by_wf: dict[str, dict[str, Any]],
                  mapping: tuple[str, str] | None,
                  job_graph: dict[str, dict[str, dict[str, Any]]] | None = None,
                  *, require_developer_timing: bool = False,
                  ) -> tuple[str, str] | None:
    """Resolve a pole's (workflow_file, job). A caller-supplied `mapping` PIN wins
    verbatim; otherwise the job's workflow is resolved by name. The PR-floor fallback
    relies on the pin because its "check" IS a job name that can collide across
    workflows — name-resolution could bind it to the wrong workflow's same-named job.

    Falls back to the SCANNED job graph when the sampled-timing mapper misses the check
    (its workflow was triage-skipped → jobs not fetched): without this the pole renders
    workflow_file/job = None → the headline wrongly declares the editable gate
    "managed/external, no workflow to drill" (the httpx matrix-gate bug)."""
    if mapping is not None:
        return mapping
    m = _map_check_to_job(
        check_name, crit_by_wf,
        require_developer_timing=require_developer_timing)
    if m is not None:
        return m
    return _check_to_job_node_scanned(check_name, job_graph) if job_graph else None


def _select_pr_floor_workflows(
    crit_by_wf: dict[str, dict[str, Any]],
    events_by_wf: dict[str, set[str]],
) -> list[tuple[float, str, str]]:
    """The PR-FLOOR synthesis: the file-backed workflows a normal PR actually runs,
    each reduced to its long-pole job, sorted slowest-first. Only PR-volume,
    CI-clean workflows with a timed long pole qualify (a release workflow is not part
    of the developer's merge wait). When NO PR-volume workflow ran — a push-only repo
    that merges straight to the default branch, so its `push` CI IS the merge wait —
    fall back to push-triggered workflows so a measured long pole still anchors a
    (clearly-demoted) PR-floor spine instead of dead-ending to static-only "no run
    history". Returns `[(long_pole_p50, workflow_file, long_pole_job), …]`; the caller
    slices to top-N. Pure (no gh / no `collect()` locals) so the selection is
    unit-testable."""
    def _qualifying(volume_events: frozenset[str]) -> list[tuple[float, str, str]]:
        out: list[tuple[float, str, str]] = []
        for wf_path, crit in crit_by_wf.items():
            lp_job = str(crit.get("long_pole_job") or "")
            lp_p50 = float(crit.get("long_pole_p50") or 0.0)
            evs = events_by_wf.get(wf_path) or set()
            if (lp_job and lp_p50 > 0 and (evs & volume_events)
                    and _volume_is_ci_clean(evs)):
                out.append((lp_p50, wf_path, lp_job))
        return out

    floor = _qualifying(_PR_VOLUME_EVENTS)
    if not floor:
        # No pull_request flow at all (push-only repo, e.g. webflow/js-webflow-api:
        # PR sample 0/20, `test` job p50 51.5s measured only on push runs). Without
        # this the floor stayed empty and the renderer buried the slowest measured job
        # under a static-only hygiene appendix. The push CI is the developer's merge
        # wait here, so anchor the PR-floor on it.
        floor = _qualifying(_PUSH_VOLUME_EVENTS)
    floor.sort(key=lambda t: -t[0])
    return floor


def _crown_recovery_wf(
    crown: str | None,
    triaged_fast_workflows: "set[str] | list[str] | None",
    job_graph: "dict[str, dict[str, dict[str, Any]]] | None",
    recoverable: "set[str] | dict[str, Any] | None",
) -> str | None:
    """The workflow to job-fetch so the HEADLINE crown is drillable, or None.

    The crowned `critical_path_check` is what the report HEADLINES as the merge gate ("the
    slowest check a typical PR waits on"). If it maps to a workflow that run-list triage
    skipped (`triaged_fast_workflows` — jobs never fetched, disclosed as "can't hold the merge
    pole"), the headline pole has NO sampled job to decompose and dead-ends ("no captured log"
    / "NO CATALOG PATTERN MATCHED"), directly contradicting that disclosure. This happens when
    every heavier pole is minority-present so the crown falls to a sub-floor lint: the relative-
    recovery pass (`_TRIAGE_RECOVER_GATE_FRAC`) references the rare-giant gate p50, which the
    fast-lint crown never reaches, so it slips through triaged.

    Resolve the crown via the SCANNED job graph (the timing mapper misses a triaged workflow —
    its jobs weren't fetched), and return that workflow iff it is BOTH triaged AND has retained
    runs to fetch — so the caller can un-triage the headline. None when the crown is
    fileless/external (no workflow → a legitimate no-drill headline), already drillable (not in
    the triaged set), or has no retained runs. Pure (no gh / no `collect()` locals) so the
    selection is unit-testable."""
    if not crown:
        return None
    node = _check_to_job_node_scanned(crown, job_graph) if job_graph else None
    wf = node[0] if node else None
    if not (wf and wf in set(triaged_fast_workflows or ())):
        return None
    # `recoverable` is either a set of workflow keys (membership is the whole signal) or the
    # `triage_recover_stash` dict {wf: [runs]}. For the dict form require a NON-EMPTY run list:
    # the contract is "has retained runs to fetch", and a present-but-empty entry (`{wf: []}`)
    # is nothing to job-fetch, so it can never make the crown drillable.
    if isinstance(recoverable, dict):
        return wf if recoverable.get(wf) else None
    return wf if wf in set(recoverable or ()) else None


def _structural_pole_candidates(
    pr_checks_tuple: "tuple[tuple[str, float], ...]",
    crit_by_wf: dict[str, dict[str, Any]],
    job_graph: "dict[str, dict[str, dict[str, Any]]] | None",
    triaged_fast_workflows: "set[str] | list[str] | None",
    top_n: int = _STRUCTURAL_TOP_N,
) -> list[tuple[str, float]]:
    """The top-N spine checks to DECOMPOSE as structural long poles, EXCLUDING any check
    whose producing workflow was triaged-fast (jobs never fetched, disclosed in
    `data_sources.triaged_fast_workflows` as "can't hold the merge pole").

    A triaged workflow's check-run still rides along on the sampled PRs, so its check-run
    p50 lands in `pr_checks_tuple` and can rank into the top-N — but there is no sampled
    job to decompose, so `_decompose_pole` would emit a BARE pole (no `dominant_step` /
    `steps`, rendering "no captured log" + "NO CATALOG PATTERN MATCHED"). That bare pole
    directly contradicts the report's own triage coverage note. Resolve each candidate's
    workflow with the SAME `_pole_mapping` (timing mapper → scanned job-graph fallback)
    `_decompose_pole` uses, so the skip and the would-be `workflow_file` agree, and drop
    it when that workflow is triaged. The dropped check stays in the `checks` spine (its
    honest fast-check disclosure); it is simply never a drilled pole. Pure (no gh / no
    `collect()` locals) so the selection is unit-testable.

    By pole-build time the relative-recovery pass (`_TRIAGE_RECOVER_GATE_FRAC`) has already
    removed any near-gate workflow from `triaged_fast_workflows` and given it a real job
    sample, so what remains is only genuinely-fast, never-fetched workflows — excluding
    them from poles is always correct. A check whose workflow is only known through the
    scanned graph but whose sampled job timing is explicit `all-events` is still a real
    PR pole when sampled PR check-runs prove it gates the merge; `_decompose_pole` keeps
    it visible as `timing_source=pr_check_runs` and withholds the step drill instead of
    borrowing push/schedule job timings."""
    triaged = set(triaged_fast_workflows or ())
    out: list[tuple[str, float]] = []
    for check_name, check_p50 in pr_checks_tuple[:top_n]:
        timed = _map_check_to_job(
            check_name, crit_by_wf, require_developer_timing=True)
        if timed is not None:
            if timed[0] in triaged:
                logger.debug("critical path: skipping structural pole %r — its workflow %s "
                             "was triaged-fast (jobs not fetched, can't drill); it stays on "
                             "the spine, never a bare undrilled pole", check_name, timed[0])
                continue
            out.append((check_name, check_p50))
            continue
        scanned = _check_to_job_node_scanned(check_name, job_graph) if job_graph else None
        if scanned is not None:
            if scanned[0] in triaged:
                logger.debug("critical path: skipping structural pole %r — its workflow %s "
                             "was triaged-fast (jobs not fetched, can't drill); it stays on "
                             "the spine, never a bare undrilled pole", check_name, scanned[0])
                continue
            out.append((check_name, check_p50))
            continue
        # Fileless/external checks have no workflow file to drill; keep the old behavior
        # of carrying them through as non-file poles so the renderer can explain there is
        # no editable workflow rather than inventing a file-backed drill.
        out.append((check_name, check_p50))
    return out


def _collapse_pr_floor_siblings(
    poles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """PR-FLOOR fallback case (1) sibling collapse: within ONE workflow the jobs
    run concurrently (subject to `needs:`), so only the slowest gates the merge.
    A slower-finishing-but-not-gating sibling (e.g. mattermost server-ci `mmctl`
    @374s behind `Postgres (shard 1)` @636s in the SAME `server-ci.yml`) never
    independently gates the developer's merge wait, and must NOT be drilled as a
    co-equal long pole — its own OPT24 sizing already says sharding it saves ~0
    wall-clock ("a longer concurrent job gates the run"). Reduce the file-backed
    poles to one per `workflow_file` (the slowest by p50), keeping every fileless
    pole untouched and preserving input order of the kept poles. Mirrors case
    (2)'s `_select_pr_floor_workflows`, which already reduces each workflow to its
    single long-pole job. Pure (no gh / no `collect()` locals) so it is
    unit-testable."""
    best_by_wf: dict[str, dict[str, Any]] = {}
    for p in poles:
        if not (p.get("workflow_file") and p.get("job")):
            continue
        wf = str(p.get("workflow_file"))
        cur = best_by_wf.get(wf)
        if cur is None or float(p.get("p50_s") or 0.0) > float(cur.get("p50_s") or 0.0):
            best_by_wf[wf] = p
    kept = {id(p) for p in best_by_wf.values()}
    return [p for p in poles
            if not (p.get("workflow_file") and p.get("job")) or id(p) in kept]


# VERBATIM-SYNC INVARIANT: the body of this function MUST match `_wf_is_file_backed` in
# skills/ci-speedup/tests/verify_report.py EXACTLY (a source-equality test pins it). The structural
# admission gate DROPS a name-inferred file-backed lever here; verify_report's invariant FLAGS one in a
# rendered findings.json. The producer (drop) and the checker (flag) must agree on file-backedness, or a
# freshly-generated report could fail its own invariant (false positive) or let a fabrication through
# (false negative). verify_report is standalone-by-design (no skill imports), so the two can't share
# code — they are a deliberate verbatim copy, kept honest by the test, exactly like gh_utils.py.
def _wf_is_file_backed(wf) -> bool:
    return str(wf or "").strip().lower().endswith((".yml", ".yaml"))


def _detect_structural_candidates(
    pr_checks_tuple: tuple[tuple[str, float], ...],
    pr_check_populations: list,
    crit_by_wf: dict[str, dict[str, Any]],
    jobs_per_run_by_wf: dict[str, list[list[dict[str, Any]]]],
    required_checks: "RequiredChecks | None",
    events_by_wf: dict[str, set[str]],
    covered_job_savings: dict[frozenset[str], float],
    start_idx: int,
    top_n: int = _STRUCTURAL_TOP_N,
    vol_by_wf: dict[str, int | None] | None = None,
    job_graph: dict[str, dict[str, dict[str, Any]]] | None = None,
    job_bimodal_all: dict[str, dict[str, Any]] | None = None,
    check_present: dict[str, int] | None = None,
    present_n_pr: int = 0,
    chain_members: frozenset = frozenset(),
    chain_p50_s: float = 0.0,
    chain_win_s: float = 0.0,
    check_pole_freq: dict[str, int] | None = None,
    triaged_fast_workflows: "set[str] | list[str] | None" = None,
) -> list[dict[str, Any]]:
    """Route the top measured critical-path checks into STRUCTURAL candidates
    instead of the old inherent-cost dead-end. For each top check: map it to its
    job, decompose the long pole into steps, cross-reference the required-check
    set, and emit the best-fit structural lever (OPT70-72/75) with a risk axis.
    Plus the per-workflow shared-sub-step (OPT73) lever. (OPT74, the
    trust-boundary cache split, is catalogued for human application but has no
    auto-detector here — it needs a fork-vs-trusted policy read scan.py can't
    infer; scan.py reports it in catalog_structural_patterns_without_detector for
    honesty. Note the fork/upstream SIGNAL itself now exists at drill time — every
    job carries a `_run_head_repo` stamp and cache poles record a fork-split
    `cache_dist` — it is the OPT74 fix POLICY, not the data, that stays human-only.)

    Detection is deterministic (category/share/ratio/required-status/recurrence);
    the report hands each candidate to the user's agent via a per-finding prompt —
    ci-speedup prescribes no fix. Sizing reuses the population-weighted,
    floor-capped cascade — never a best case."""
    out: list[dict[str, Any]] = []
    idx = start_idx
    job_bimodal_all = job_bimodal_all or {}
    if not pr_checks_tuple:
        return out  # structural routing is measured-critical-path driven

    all_check_p50 = {n: p for n, p in pr_checks_tuple}
    triaged = set(triaged_fast_workflows or ())
    seen_jobs: set[tuple[str, str]] = set()
    # The merge-blocking (required-reachable) check set for the OPT71 de-trigger guard,
    # computed once over the same checks via the SAME `needs:`-reachability the spine
    # filter uses — so a kept leg's stamp agrees with its presence on the spine (a leg the
    # required work `needs:` is `required`, never offered as a de-trigger candidate). Empty
    # when no required set resolved; the None-guard below still wins first.
    req_names = required_checks.names if required_checks is not None else frozenset()
    reachable_required = _required_reachable_checks(
        [n for n, _ in pr_checks_tuple], req_names, job_graph, crit_by_wf)

    # ---- 1. Top-N critical-path checks → per-check structural lever ----
    for check_name, check_p50 in pr_checks_tuple[:top_n]:
        mapping = _map_check_to_job(
            check_name, crit_by_wf, require_developer_timing=True)
        # The de-trigger floor: the slowest OTHER concurrent check on the PR.
        others = [p for n, p in pr_checks_tuple if n != check_name]
        if mapping is None:
            wf_path, job_name, decomp = "", "", None
        else:
            wf_path, job_name = mapping
            if (wf_path, job_name) in seen_jobs:
                continue
            job_inst = [j for run in jobs_per_run_by_wf.get(wf_path, [])
                        for j in run if str(j.get("name", "")) == job_name]
            # Decompose the SLOW mode for a bimodal pole, so the dominant category
            # (and thus the OPT70/72/75 route) agrees with the slow-mode run the
            # report drills — not a fast/slow-blended p50 that crowns the wrong
            # category and misroutes (e.g. OPT75 instead of OPT72 for a cold build).
            decomp = _decompose_job_steps(
                job_inst, bimodal=job_bimodal_all.get(check_name))
            # Skip the structural lever ONLY if a hygiene fix already MATERIALLY
            # shortened this pole (>= half its measured p50). A trivial cache (e.g.
            # a 2s Playwright download on a 342s pole) must NOT suppress the
            # dominant step's own structural lever — otherwise the pole renders with
            # an off-target 2s fix instead of the real one.
            jt = _struct_toks(job_name)
            if jt:
                # EXACT job identity, not a bidirectional token-subset: a hygiene saving on
                # `Test` (`{test}`) must not suppress a DISTINCT `Integration Test`
                # (`{integration, test}`) pole's own structural lever just because one token
                # set contains the other — that credited the win to a job that never benefits.
                cov = max((s for ct, s in covered_job_savings.items()
                           if jt == ct), default=0.0)
                if cov >= 0.5 * check_p50:
                    continue
        # Required-status: required-REACHABLE (in the set, OR a job the required work
        # transitively `needs:`) → required; data absent OR incomplete → UNKNOWN (never
        # assert non-required on a partial read — that would de-trigger a check an unread
        # ruleset requires); else not-required. Uses the SAME reachability as the spine
        # filter: a leg the spine keeps (e.g. a sharded `Test` job feeding a required
        # `Merge Test Reports` rollup) must NOT be stamped not-required, or OPT71 would
        # tell the user to drop a merge-gating leg.
        if required_checks is None:
            req_status = "unknown"
        elif check_name in reachable_required:
            req_status = "required"
        elif required_checks.complete:
            req_status = "not-required"
        else:
            req_status = "unknown"

        crit = crit_by_wf.get(wf_path, {})

        # Route this pole to a structural pattern. CORE PRINCIPLE: every pole maps
        # to an addressable speed-up category — never a de-scope-only answer, never
        # "no lever". The routing is category-based; the concrete remedy is decided
        # by the user's own agent via the finding's prompt (OPT75 is the generic
        # "decompose this step" pattern the agent reasons about from the measured
        # behaviour). A provably NON-REQUIRED check ALSO gets the OPT71 de-scope as
        # a SECOND option (drop it entirely) — but de-scope never replaces the
        # speed-up route; a developer wants to know how to make the check faster,
        # not just that they could delete it.
        picks: list[tuple[str, float, str, str]] = []  # (pattern, raw, assumption, sizing)
        if decomp is not None:
            dom = decomp["dominant_category"]
            ratio = decomp["redundant_ratio"]
            if dom == "build" and ratio is not None and ratio > 2.0:
                sp = "OPT72"  # build dominated by redundant setup/build → cache it
            elif dom == "build":
                sp = "OPT70"  # build → scope to changed targets (the safe build lever)
            else:
                # test / install / setup / scan / format / package → OPT75, the
                # NEUTRAL pattern. The user's agent picks the actual remedy from the
                # measured behaviour (shard/parallelize a test, cache an install,
                # relocate a scan), so a `test`-dominant pole is NEVER stamped with
                # OPT70's "scope/drop your tests" + HIGH coverage-loss framing when
                # the real fix is, e.g., turning on intra-shard parallelism.
                sp = "OPT75"
            raw_s, assum_s = _structural_raw_estimate(decomp)
            picks.append((sp, raw_s, assum_s, "cascade"))
        elif mapping is None:
            # No SAMPLED job/steps for this check (`_map_check_to_job` returned None).
            # STILL route a speed-up by the check NAME's category, and let the user's
            # agent reason about the source. Qualitative size — no measured step to
            # floor against. But the evidence must NOT conflate two distinct reasons
            # there are no sampled steps:
            #   (a) TRIAGED-FAST: a real in-repo workflow file DOES produce this check
            #       (resolvable from the static job graph), its workflow was just triaged
            #       out of job-fetching (a fast workflow under the long-pole floor), so
            #       its steps were never sampled. It is FILE-BACKED — calling it
            #       "fileless / third-party app check" is false (the finding even anchors
            #       its real `workflow_file` below via `_check_to_workflow_file_static`).
            #   (b) GENUINELY FILELESS: no workflow file produces it at all (CodeQL
            #       default setup, an external app / bot check) — nothing to decompose.
            name_cat = _step_category(check_name)
            static_wf = _check_to_workflow_file_static(check_name, job_graph)
            if static_wf is not None and static_wf in triaged:
                assum = (
                    f"steps not sampled — workflow `{static_wf}` was triaged as fast "
                    f"(under the long-pole floor, so its jobs weren't step-sampled); "
                    f"this is a file-backed in-repo check whose steps simply weren't "
                    f"fetched. Name suggests a `{name_cat}` cost, addressable in the "
                    f"workflow")
            elif static_wf is not None:
                # A file-backed PR check with no developer-event job timing is already
                # represented by the PR check-run pole. Do not infer a structural fix from
                # the bare check name, and do not borrow all-events push/schedule steps.
                assum = ""
            elif len(_check_producing_workflows(
                    check_name, crit_by_wf, require_developer_timing=True)) > 1:
                # (c) CROSS-WORKFLOW AMBIGUOUS: a same-named job produces this check in
                #     MORE THAN ONE workflow (a monorepo copy-paste / reusable job), so
                #     `_map_check_to_job` refused to guess one file (bailed to None) and
                #     `_check_to_workflow_file_static` likewise found no single file. It is
                #     FILE-BACKED — calling it a "third-party app check" is false — but we
                #     cannot attribute it to one workflow to drill its steps/fix. Disclose
                #     the ambiguity honestly with the actionable remediation (give the
                #     jobs distinct names) instead of a fabricated fileless label.
                assum = (
                    f"produced by a same-named job in MORE THAN ONE workflow (a monorepo "
                    f"copy-paste / reusable job), so this check-run can't be attributed to a "
                    f"single workflow file to drill — it stays unmapped rather than bound to "
                    f"the wrong file's steps/fix. Name suggests a `{name_cat}` cost; give the "
                    f"colliding jobs distinct names to attribute and drill it")
            else:
                assum = (
                    f"fileless check (no sampled job/steps — e.g. a default setup or "
                    f"third-party app check); name suggests a `{name_cat}` cost, "
                    f"addressable at the check's source")
            if assum:
                picks.append(("OPT75", 0.0, assum, "qualitative"))
        # else: mapping is not None but decomp is None — a FILE-BACKED check whose
        #   job was triaged OUT of step-sampling (a fast workflow under the long-pole
        #   floor: it has a real workflow file/job but no sampled instances, so
        #   `_decompose_job_steps` returned None). There is NO measured dominant step
        #   to crown, so the structural track's admission gate (positive instance
        #   evidence — the long-pole job DECOMPOSED into steps) is unmet. Do NOT
        #   fabricate an OPT75 speed-up that calls a file-backed check "fileless" and
        #   infers its cost category from the check NAME alone (that is the
        #   infer-root-cause-from-a-bare-name pattern OPT49/OPT51 were CUT for). Emit
        #   no speed-up lever here; a measured de-trigger (OPT71) may still apply below.
        if req_status == "not-required":
            picks.append(("OPT71", credit_detrigger(check_p50, others), (
                "de-triggering this non-required check removes it from the PR "
                "path; the developer then waits for the slowest OTHER concurrent "
                "check"), "detrigger"))

        # Shared evidence + measured step table (decomp-derived; identical for
        # every lever on this check). The per-step decomposition is the runtime
        # behaviour the user's agent reasons about (handed to it in the finding's
        # prompt) — not just the workflow YAML.
        if decomp is not None:
            steps = decomp["steps"][:6]
            rows = [[f"`{n}`", c, f"{p:.0f}s",
                     f"{(p/decomp['job_p50']*100):.0f}%"] for n, c, p in steps]
            me = _measured_evidence(
                ["Step", "Category", "p50", "Share of job"], rows,
                summary=(f"check `{check_name}` ({check_p50:.0f}s) maps to job "
                         f"`{job_name}`; dominant step `{decomp['dominant_step']}` "
                         f"({decomp['dominant_category']}) is "
                         f"{decomp['dominant_share']*100:.0f}% of the job. Redundant-"
                         f"work ratio (setup+build / payload) = "
                         f"{decomp['redundant_ratio']}."),
                note=("Decomposed from the sampled job's per-step timings. The "
                      "dominant step is the addressable cost — attack it by "
                      "category, don't declare the whole check inherent."))
            evidence_base = (f"critical-path check `{check_name}` ({check_p50:.0f}s): "
                             f"dominant step `{decomp['dominant_step']}` "
                             f"({decomp['dominant_category']}, "
                             f"{decomp['dominant_share']*100:.0f}% of job `{job_name}`)")
        else:
            me = None
            evidence_base = None

        # Emit one finding per lever. Fileless check (no sampled job → job_name ==
        # ""): anchor `affected_jobs` to the CHECK name so the critical-path table
        # still matches it (`_pr_critical_path_block` builds its `_covered` set from
        # `affected_jobs` token-sets; an empty list matches nothing, leaving the
        # check wrongly labelled "no lever" even though this finding targets it).
        # When the timing mapper couldn't anchor the file (the workflow was triaged out
        # of job-fetching, so `crit_by_wf` holds an empty-`job_p50` stub), recover the
        # REAL workflow file from the static job graph before falling back to a
        # name-derived stub — otherwise a triaged fast check's finding gets the first
        # token of the check name ("Check"/"Lint"/"Validate"), a one-word non-path that
        # contradicts the pole's known file and points the report's per-finding location
        # (and the ci-harness auto-fixer) at a nonexistent file.
        wf_for_finding = (
            wf_path or _check_to_workflow_file_static(check_name, job_graph)
            or check_name.split(" ")[0])
        for pat, raw, assumption, sizing in picks:
            idx += 1
            evidence = evidence_base or (
                f"critical-path check `{check_name}` ({check_p50:.0f}s): {assumption}")
            f = _new_structural_finding(
                pat, wf_for_finding, job_name, idx,
                evidence, me, assumption, decomp=decomp, required_status=req_status,
                affected_jobs=([check_name] if mapping is None else None),
            )
            # Disambiguate per-check rows: append the target check so two levers on
            # different checks (or the speed-up + de-scope on one) read distinctly.
            f["title"] = f"{_STRUCTURAL_META[pat]['title']} — `{check_name}`"
            if pat == "OPT71":
                f["required_status_note"] = (
                    "NOT in the repo's required-status-check set"
                    if req_status == "not-required" else "")
                # De-trigger saving is ALREADY floored at the slowest other
                # concurrent check (credit_detrigger over the full pr_checks set),
                # so it doesn't need the cascade. raw == 0 means a slower check
                # still gates: a MEASURED zero (bill-only), not a qualitative unknown.
                f["wall_clock_p50_s"] = round(raw, 1)
                if raw <= 0:
                    f["realization"] = "none"
                    f["tier"] = 2
                    prev = f.get("size_note") or ""
                    bill = ("removing this check saves 0 wall-clock here — a slower "
                            "concurrent check still gates the PR; the value is "
                            "runner-minutes (stop running an advisory check on every "
                            "push), sized against your push frequency")
                    f["size_note"] = f"{prev}; {bill}" if prev else bill
            elif sizing == "cascade" and raw > 0 and wf_path and crit:
                # Scope the floor to THIS check's own leg, not the whole workflow:
                # a non-pole matrix leg must not inherit the workflow's top pole as
                # its own_max and dodge a slower concurrent sibling's floor.
                _apply_structural_cascade(
                    f, raw, wf_path, crit, events_by_wf,
                    pr_checks_tuple, pr_check_populations,
                    own_check_names=frozenset({check_name}),
                    chain_members=chain_members,
                    chain_p50_s=chain_p50_s, chain_win_s=chain_win_s)
            else:
                # NAME-INFERRED structural pick (`sizing == "qualitative"` — set ONLY by the
                # `mapping is None` branch, where there is NO sampled job/step): the cost category is
                # inferred from the check NAME, not a measured dominant step. For a FILE-BACKED check
                # (a real `.yml` workflow) that is the admission-gate violation the no-decomp sibling
                # branch already refuses — DROP it. The pole keeps a hand-off: OPT71 if the check is
                # non-required, ELSE the renderer's UNCONDITIONAL generic dominant-step agent prompt
                # (`blocking_path._build_generic_agent_prompt`, the anti-dead-end guarantee — NOT the
                # phase-4a LLM gap-fill, which needs a captured log a triaged-fast pole doesn't have).
                # (The OPT75-fabrication class: mixpanel `Validate PR title`, apple `Analyze PR for
                # labeling`, rootly `Docs Drift`.) Gating on `sizing == "qualitative"` (NOT merely
                # `raw <= 0`) is what keeps a LEGITIMATE bill-only finding — a decomp-derived OPT70/72
                # with a measured step whose addressable wall-clock floored to ~0 because a slower check
                # gates (rootly `Build`, apple `merge-build`), which uses `sizing == "cascade"`. A
                # GENUINELY-FILELESS check (workflow_file is a bare app/check name, not a `.yml` path —
                # CodeQL, Analyze) also keeps its honest qualitative hand-off at the source. The class
                # fix; verify_report `check_structural_pole_has_measured_step` asserts the property. The
                # `_wf_is_file_backed` is a VERBATIM copy of the checker's (pinned by a source-equality
                # test) — see its definition above for why they can't share code.
                if sizing == "qualitative" and _wf_is_file_backed(f.get("workflow_file")):
                    continue
                f["wall_clock_p50_s"] = None if raw <= 0 else round(raw, 1)
                if raw <= 0:
                    f["realization"] = "none"
                    f["tier"] = 2
            out.append(f)
        if mapping is not None:
            seen_jobs.add((wf_path, job_name))

    # ---- 2. Shared sub-step across critical-path cluster jobs (OPT73) ----
    out += _detect_shared_substep(
        crit_by_wf, jobs_per_run_by_wf, events_by_wf,
        pr_checks_tuple, pr_check_populations, start_idx + len(out),
        vol_by_wf=vol_by_wf, job_bimodal_all=job_bimodal_all, job_graph=job_graph,
        check_present=check_present, present_n_pr=present_n_pr, req_names=req_names,
        check_pole_freq=check_pole_freq, chain_members=chain_members,
        chain_p50_s=chain_p50_s, chain_win_s=chain_win_s)
    # Re-number every emitted structural finding to a contiguous id block so the
    # two sub-detectors can't collide on an index.
    for k, f in enumerate(out, start=start_idx + 1):
        f["id"] = f"f{k}"
    return out


def _cluster_jobs_concurrent(
    job_names: "list[str] | set[str]", wf_path: str,
    job_graph: dict[str, dict[str, dict[str, Any]]] | None,
    crit_by_wf: dict[str, dict[str, Any]],
) -> bool:
    """True when the cluster jobs all run in PARALLEL — no `needs:` edge wires any
    two of them in sequence — so the cluster-FLOOR premise holds: one fix lowers
    them all *at the same time*. False when at least one cluster job transitively
    `needs:` another: those stages run SERIALLY (e.g. `test` needs `compile`), so
    the shared step is paid one stage after the other, not concurrently, and the
    'concurrent / at the same time' framing would be a falsehood.

    Resolves each measured cluster job name to its (workflow, job_id) node via the
    same `needs:`-graph mapping the spine scoper uses, then asks whether any node
    reaches another cluster node through its downward `needs:` closure. With no
    usable job graph we can't prove a dependency, so default to concurrent — the
    common cluster is matrix legs of ONE job (same job id, genuinely parallel),
    and this keeps the behavior-preserving, conservative direction."""
    if not job_graph:
        return True
    nodes: set[tuple[str, str]] = set()
    for n in job_names:
        node = _check_to_job_node(n, job_graph, crit_by_wf)
        if node is not None:
            nodes.add(node)
    # Scope to this workflow's nodes; matrix legs of one job collapse to a single
    # id (parallel by construction).
    local_jids = {jid for wf, jid in nodes if wf == wf_path}
    if len(local_jids) < 2:
        return True
    wf_jobs = job_graph.get(wf_path, {})
    for jid in local_jids:
        seen: set[str] = set()
        stack = list((wf_jobs.get(jid, {}) or {}).get("needs", []))
        while stack:
            dep = stack.pop()
            if dep in seen:
                continue
            seen.add(dep)
            if dep in local_jids:
                return False  # a cluster job depends on another -> sequential
            stack.extend((wf_jobs.get(dep, {}) or {}).get("needs", []))
    return True


def _affected_jobs_all_rare(
    affected_jobs: "list[str] | set[str]",
    check_pole_freq: dict[str, int],
    present_n_pr: int,
    req_names: "frozenset[str] | set[str]",
) -> bool:
    """True when EVERY affected job is a one-path outlier — the actual critical path (slowest job)
    on FEWER than `_POLE_RECUR_FLOOR` sampled PRs and never required — so the cluster this finding
    targets is NOT on the typical merge-gating critical path.

    Mirrors `_rank_spine_present_first`'s `is_rare` rule (the spine's own pole-frequency demotion),
    so the two models can't contradict: a job the spine excludes from the typical critical path
    must not also carry a credited on-critical-path wall-clock win. Used to floor a cross-cluster
    (OPT73) wall-clock saving to 0 (bill-only) when its whole cluster never actually gates — a job
    that is never the slowest saves zero merge-wait (a slower concurrent sibling still gates), so
    crediting it wall-clock would contradict the spine that demotes it. (Before the pole-frequency
    ranking fix this used PRESENCE; a majority-present-but-never-slowest job then kept its credit
    while the spine demoted it — the exact expo/expo contradiction.)

    Conservative — returns False (no demotion) unless we are SURE the cluster never gates:
      - below `_RARE_PRESENCE_MIN_PR` sampled PRs the frequency is noise;
      - an empty affected set, or ANY affected job with no resolved pole-frequency, is UNKNOWN
        (not rare) — we never demote on partial information;
      - ANY required job, or any job that is the actual pole on >= `_POLE_RECUR_FLOOR` PRs, keeps
        the credit (a real recurring gate is never demoted)."""
    if present_n_pr < _RARE_PRESENCE_MIN_PR or not affected_jobs:
        return False
    if any(j not in check_pole_freq for j in affected_jobs):
        return False
    if any(j in req_names for j in affected_jobs):
        return False
    return all(check_pole_freq[j] < _POLE_RECUR_FLOOR for j in affected_jobs)


def _leg_presence_eligible(
    leg: str,
    check_pole_freq: dict[str, int],
    present_n_pr: int,
    req_names: "frozenset[str] | set[str]",
) -> bool:
    """The inverse of `_rank_spine_present_first`'s `is_rare` — is this cluster leg one the
    presence-weighted pole ranking KEEPS (not a one-path outlier it demotes)? For any leg the spine
    actually ranks (i.e. one present in the dense `_pole_frequencies` map) this is the exact
    complement of `is_rare`; the one deliberate divergence is the unknown-to-the-map branch, which
    `is_rare` would read as freq-0/rare but which we treat as ELIGIBLE — mirroring the sibling
    `_affected_jobs_all_rare`'s "never act on partial information" stance for a leg the spine never
    ranked. A leg is presence-eligible iff: the sample is too small to judge (frequency is noise →
    inert, all eligible); OR it is required (gates by definition); OR it is UNKNOWN to the frequency
    map (never exclude on partial information); OR it is the ACTUAL pole (the slowest check a PR
    waits on) on >= `_POLE_RECUR_FLOOR` sampled PRs. So a minority-present leg the pole ranking
    excluded (playwright #56: `Test msedge-dev on macos-latest`, the actual pole on ~0/20) can never
    anchor the OPT73 sizing nor lead its Evidence. ONE notion of 'is this on the typical merge path',
    shared with the spine — never a parallel one (the L3/L5 mirror-the-engine discipline)."""
    if present_n_pr < _RARE_PRESENCE_MIN_PR:
        return True
    if leg in req_names:
        return True
    if leg not in check_pole_freq:
        return True
    return check_pole_freq[leg] >= _POLE_RECUR_FLOOR


def _workflow_gates_minority(wf_gate_freq: int, present_n_pr: int) -> bool:
    """Does the cluster's WORKFLOW gate only a MINORITY of sampled PRs? `wf_gate_freq` is the workflow
    gate count `blocking_path._toc_block` renders beside a pole (`\\`wf\\` gates N/npop PRs`, N = the
    summed pole frequency of the workflow's checks — see `_workflow_gate_freq`); this applies a
    workflow-level MAJORITY test to it. That is a COARSER aggregate than the spine's per-check typical
    split (`blocking_path._typical_check` / `is_rare`: a single check is a recurring gate at
    `pole_n >= _POLE_RECUR_FLOOR`) — deliberately so: an OPT73 cluster fix only saves the TYPICAL PR
    wall-clock when the whole workflow is on the typical critical path, a stricter bar than one leg
    recurring twice. Below the sample-size floor the fraction is noise → treat as majority (inert,
    never demote). When a workflow gates a minority (playwright #56: `tests_secondary.yml` gates
    2/20), a TYPICAL PR never waits on it, so the OPT73 cluster's WALL-CLOCK credit is not honest on
    the typical-PR bottom line — the saving belongs on the bill (runner-minute) axis only, exactly
    like the all-legs-rare demotion."""
    if present_n_pr < _RARE_PRESENCE_MIN_PR:
        return False
    return wf_gate_freq <= present_n_pr * _RARE_PRESENCE_FRAC


def _workflow_gate_freq(
    check_pole_freq: dict[str, int],
    crit_by_wf: dict[str, dict[str, Any]],
    wf_path: str,
) -> int:
    """How often ANY of `wf_path`'s checks is the per-PR slowest — the workflow gate count
    `_workflow_gates_minority` tests, and the SAME signal the renderer sums for its "gates N/npop"
    line (`blocking_path._toc_block`, summed over the workflow's checks).

    Summed in `check_pole_freq`'s OWN key domain: that map is keyed by check-run CONTEXT names,
    while `crit["job_p50"]` keys are raw job-API names, and the two do not always coincide — the
    `_map_check_to_job` layer exists precisely for that gap. A raw `check_pole_freq.get(job_name, 0)`
    sum would score every name-divergent check 0, dragging a genuine MAJORITY-gating workflow under
    the minority line and flooring a real developer wall-clock lever to bill-only (the CONVERSE of
    the burial the OPT73 presence-weighting prevents, and a demotion the verify guard — which
    re-derives by `workflow_file` — cannot backstop once `wall_clock` is already 0). So attribute
    each spine check to its workflow the way the renderer/guard do (`_map_check_to_job`), never by a
    job-name lookup — the same "never act on partial/mismatched info" discipline
    `_affected_jobs_all_rare` enforces.

    Attribute via the AMBIGUITY-AWARE full match set (`_workflows_matching_check`), NOT the
    single-pick `_map_check_to_job` — which now bails to None on a cross-workflow same-name
    collision (issue #59) and would credit a duplicated monorepo gate's frequency to NO
    workflow, dragging a genuine majority gate under the minority line and flooring its
    wall-clock lever to bill-only (the exact unbackstoppable undercount this docstring warns
    against). Crediting every producing workflow keeps the lever visible — the safe direction,
    and the same "keep if ANY matching workflow gates" stance `_is_pr_gate_check` takes.
    Byte-identical for an unambiguous check (its match set is a single workflow, so
    `wf_path in {wf}` ⟺ the old `_map_check_to_job(...)[0] == wf_path`)."""
    return sum(freq for c, freq in check_pole_freq.items()
               if wf_path in _workflows_matching_check(c, crit_by_wf))


def _detect_shared_substep(
    crit_by_wf: dict[str, dict[str, Any]],
    jobs_per_run_by_wf: dict[str, list[list[dict[str, Any]]]],
    events_by_wf: dict[str, set[str]],
    pr_checks_tuple: tuple[tuple[str, float], ...],
    pr_check_populations: list,
    start_idx: int,
    vol_by_wf: dict[str, int | None] | None = None,
    job_bimodal_all: dict[str, dict[str, Any]] | None = None,
    job_graph: dict[str, dict[str, dict[str, Any]]] | None = None,
    check_present: dict[str, int] | None = None,
    present_n_pr: int = 0,
    req_names: "frozenset[str] | set[str]" = frozenset(),
    check_pole_freq: dict[str, int] | None = None,
    chain_members: frozenset = frozenset(),
    chain_p50_s: float = 0.0,
    chain_win_s: float = 0.0,
) -> list[dict[str, Any]]:
    """A step CATEGORY (normalized) that recurs across >=2 cluster jobs of one
    workflow is a cluster-FLOOR lever (OPT73): fixing it once lowers every
    cluster job, so it beats the long_pole-floor cap. Credit the saving across
    all containing jobs (wall_clock = per-job step time; runner-min = per-run
    seconds × the workflow's monthly run volume, like every other finding -
    NEVER per-run, which would under-count the bill by ~the monthly-run factor)."""
    vol_by_wf = vol_by_wf or {}
    job_bimodal_all = job_bimodal_all or {}
    check_present = check_present or {}
    check_pole_freq = check_pole_freq or {}
    out: list[dict[str, Any]] = []
    idx = start_idx
    for wf_path, crit in crit_by_wf.items():
        long_pole = crit.get("long_pole_p50", 0.0) or 0.0
        if long_pole <= 0:
            continue
        # Cluster = jobs within striking distance of the long pole.
        band = 0.6 * long_pole
        cluster_jobs = {n for n, p in (crit.get("job_p50") or {}).items()
                        if p >= band}
        if len(cluster_jobs) < 2:
            continue
        # Per cluster job, the p50 of each STEP NAME (aggregated across runs).
        # A genuine shared sub-step is the SAME NAMED step recurring across jobs —
        # the literal "fix it once, lower every job" premise (e.g. matrix legs of
        # one job each running the same `Adapter Integration` step, or each calling
        # the same `Setup pnpm/node` action). Clustering by NAME (not category)
        # keeps the finding honest AND lets the evidence name the actual step: it
        # never lumps heterogeneous same-category work (distinct test suites that
        # merely share the `test` category) into a fake "shared step".
        # step-name -> {job -> p50 of that named step}; also keep each job's FULL
        # decomposition so the evidence can SHOW the slowest job's waterfall (the
        # actual long pole), not just assert a shared-step number.
        name_job_p50: dict[str, dict[str, float]] = {}
        job_decomp: dict[str, dict[str, Any]] = {}
        for jname in cluster_jobs:
            inst = [j for run in jobs_per_run_by_wf.get(wf_path, [])
                    for j in run if str(j.get("name", "")) == jname]
            # Size a bimodal cluster job off its SLOW mode, like the OPT70/72/75 path:
            # a shared step that's cache-warm on most PRs and cold on a minority would
            # otherwise be floored by its blended (warm-dragged) p50 — under-crediting the
            # shared lever and, if blended < the material threshold, silently dropping a
            # step that is real in the slow mode it's drilled against.
            d = _decompose_job_steps(inst, bimodal=job_bimodal_all.get(jname))
            if not d:
                continue
            job_decomp[jname] = d
            for n, c, p in d["steps"]:
                if c in ("post", "other"):
                    continue
                slot = name_job_p50.setdefault(str(n), {})
                slot[jname] = slot.get(jname, 0.0) + p
        # The step NAME shared across >=2 cluster jobs, each paying a material cost,
        # picking the one with the highest shared floor (= min cost across its jobs).
        best: tuple[float, str, str, dict[str, float]] | None = None
        for step_name, jobs in name_job_p50.items():
            material = {j: p for j, p in jobs.items() if p >= 15.0}
            if len(material) < 2:
                continue
            per_job = min(material.values())  # conservative shared floor
            if best is None or per_job > best[0]:
                best = (per_job, step_name, _step_category(step_name), material)
        if best is None:
            continue
        per_job_s, step_name, cat, jobs = best
        max_job_s = max(jobs.values())
        floor = _STRUCT_WARM_FLOOR_S.get(cat, 10.0)
        credit = credit_shared_substep(per_job_s, floor, len(jobs))
        # NOTE: this is NOT a bill-only drop. credit.wall_clock_s and
        # credit.runner_min_s both derive from the SAME `per_job` (= step −
        # warm-floor), so wall_clock_s <= 0 here means the shared step is already
        # at/below its warm floor — genuinely nothing to save on EITHER axis, so
        # there is no finding to keep. The bill-only case (0 wall-clock, real
        # runner-min) arises LATER, from `_apply_structural_cascade` flooring the
        # wall-clock against other workflows' checks while leaving runner-min
        # intact — and that finding is kept (it's demoted to bill-only notes).
        if credit.wall_clock_s <= 0:
            continue
        idx += 1
        # SHOW THE WORK. Per job: its total p50, the shared step's cost, and the
        # step's SHARE of the job — so the reader sees the step is the actual
        # bottleneck, not just a number. Plus the slowest job's full step waterfall
        # (the real long pole) with the shared step marked.
        job_total = crit.get("job_p50") or {}

        def _tot(j: str) -> float:
            # The job total is the SHARE denominator, and the numerator `jobs[j]`
            # is the shared step's SLOW-mode p50 (decomposed off the slow cluster,
            # like OPT70/72/75 — see `_decompose_job_steps(bimodal=...)` above). The
            # denominator MUST use that same slow-mode basis: `crit.job_p50` is the
            # BLENDED (warm-dragged) p50, and dividing a slow-mode step by a blended
            # job renders a physically impossible >100% share (a step can't exceed
            # its containing job). Prefer the slow-mode decomposition total so both
            # sides share one aggregation; fall back to the blended figure only when
            # a job has no decomposition (it never reaches `jobs` without one).
            return float((job_decomp.get(j) or {}).get("job_p50") or job_total.get(j) or 0.0)

        # ANCHOR on a PRESENCE-ELIGIBLE leg (#56): a leg the presence-weighted pole ranking
        # excluded as minority-present (the actual pole on < `_POLE_RECUR_FLOOR` PRs) must not lead
        # the Evidence, be named the `slowest cluster job`, or become `affected_jobs[0]` (the
        # appendix `**Where:**` lead). Eligible legs sort ahead of rare ones; within each tier,
        # slowest-p50 first (unchanged). When every leg is eligible — the common matrix / small-
        # sample case, and every majority cluster with no rare leg (mastodon test-ruby, the electron
        # build chain) — this is byte-identical to the prior pure `-_tot` order.
        def _anchor_key(j: str) -> tuple[int, float]:
            eligible = _leg_presence_eligible(j, check_pole_freq, present_n_pr, req_names)
            return (0 if eligible else 1, -_tot(j))

        ordered = sorted(jobs.keys(), key=_anchor_key)
        slowest = ordered[0]
        rows = []
        for j in ordered:
            tot = _tot(j)
            share = f"{jobs[j] / tot * 100:.0f}%" if tot > 0 else "—"
            rows.append([f"`{j}`", f"{tot:.0f}s", f"{jobs[j]:.0f}s", share])
        waterfall = [
            {"step": str(n), "category": c, "p50_s": round(p, 1),
             "shared": str(n) == step_name}
            for n, c, p in (job_decomp.get(slowest, {}).get("steps") or [])[:8]
        ]
        slow_tot = _tot(slowest)
        slow_share = f"{jobs[slowest] / slow_tot * 100:.0f}%" if slow_tot > 0 else ""
        span = (f"~{per_job_s:.0f}s" if max_job_s - per_job_s < 1
                else f"~{per_job_s:.0f}–{max_job_s:.0f}s")
        # Cluster-floor framing only holds for PARALLEL jobs. When the cluster
        # jobs are wired in a `needs:` chain (sequential stages) the shared step
        # is paid serially, so "concurrent / at the same time" is false — describe
        # them as sequential and credit per-stage (still conservative). Default to
        # concurrent when the graph can't prove a dependency (matrix-leg case).
        concurrent = _cluster_jobs_concurrent(
            jobs.keys(), wf_path, job_graph, crit_by_wf)
        rel = "concurrent" if concurrent else "sequential"
        if concurrent:
            summary = (f"The **`{step_name}`** (`{cat}`) step is the root-cause cost in "
                       f"{len(jobs)} concurrent critical-path jobs of `{wf_path}` — "
                       f"**{slow_share} of the slowest job** (`{slowest}`, "
                       f"{slow_tot:.0f}s). The SAME step runs in each, so optimizing it "
                       f"once lowers all of them at the same time (shared floor "
                       f"~{per_job_s:.0f}s, the cheapest job's cost).")
            note = ("Cluster-floor lever: shaving any one pole stops at the floor, but "
                    "the same step shared across concurrent jobs lowers all of them "
                    "together. Credit spans every job that runs it.")
        else:
            summary = (f"The **`{step_name}`** (`{cat}`) step is the root-cause cost in "
                       f"{len(jobs)} sequential (`needs:`-chained) critical-path jobs of "
                       f"`{wf_path}` — **{slow_share} of the slowest job** (`{slowest}`, "
                       f"{slow_tot:.0f}s). The SAME step runs in each stage, so "
                       f"optimizing it once lowers every stage (shared floor "
                       f"~{per_job_s:.0f}s, the cheapest job's cost).")
            note = ("Cluster-floor lever: the same step recurs across these "
                    "`needs:`-chained sequential stages, so fixing it once lowers each "
                    "stage; because they run serially the per-stage savings compound on "
                    "the critical path (credited here at the conservative single-stage "
                    "floor). Credit spans every job that runs it.")
        me = _measured_evidence(
            ["Cluster job", "Job p50", f"`{step_name}` p50", "Share of job"], rows,
            summary=summary, note=note)
        me["waterfall"] = {"job": slowest, "job_p50_s": round(slow_tot, 1),
                           "shared_step": step_name, "steps": waterfall}
        evidence = (f"the `{step_name}` step is {slow_share} of the slowest cluster "
                    f"job `{slowest}` ({slow_tot:.0f}s) and recurs across "
                    f"{len(jobs)} {rel} jobs of `{wf_path}` ({span} per job) — "
                    f"a cluster-floor lever")
        # Bill saving (per MONTH): per-run seconds saved across all parallel
        # copies × the workflow's monthly run volume ÷ 60. Cluster jobs run on
        # essentially every run of the workflow, so the full monthly volume
        # applies (not an effective/gated fraction).
        monthly_vol = vol_by_wf.get(wf_path)
        rm_per_mo = (round(credit.runner_min_s * monthly_vol / 60.0, 1)
                     if isinstance(monthly_vol, int) and monthly_vol > 0 and credit.runner_min_s
                     else None)
        rm_note = (f"~{rm_per_mo:,.0f} runner-min/mo" if rm_per_mo
                   else f"~{credit.runner_min_s:.0f}s/run (monthly volume unknown)")
        f = _new_structural_finding(
            "OPT73", wf_path, "", idx, evidence, me,
            size_note=(f"cluster-floor lever — saving credited across all "
                       f"{len(jobs)} jobs running the shared `{cat}` step "
                       f"({rm_note})"),
            # SLOWEST-first (same order as `evidence`, `me` rows, and `slowest`), NOT
            # alphabetical: the appendix `**Where:**` line displays `affected_jobs[0]`, and
            # when this finding sits ON the critical path that lead job must be the DOMINANT
            # (slowest) cluster job the on-path claim rests on — the one `evidence` names — not
            # an alphabetically-first sibling leg that may be a spine-rare-demoted job (tauri
            # `test (macos-latest)` led while the gate is the typical `test (windows-latest)`),
            # which would double-frame a name the spine footnote demotes as opt-in on-path.
            affected_jobs=ordered,
        )
        # #49 — PERSIST the cluster-floor-lever marker onto the SAVED finding. The
        # sizing cascade below already consumes `cluster_floor_lever=True` in-memory,
        # but the marker never reached findings.json — so the render-layer headline
        # selection couldn't lead the bottom line with the stamped cluster ceiling, and
        # verify_report's burial guard SKIPped on every real report (the flag was
        # absent). Stamping it here closes both: the renderer leads with this finding's
        # `wall_clock_p50_s` when it beats the sibling-capped per-leg win, and the guard
        # can re-derive the burial. Legacy artifacts lack the marker → render
        # byte-identically and the guard SKIPs loud (never reads clean).
        f["cluster_floor_lever"] = True
        # #49 — also persist the cluster's concurrency nature so the render-layer headline
        # phrases it honestly. `_cluster_jobs_concurrent` already decided this for the
        # evidence/summary wording above (concurrent matrix legs vs `needs:`-chained
        # sequential stages); the Bottom line must not hardcode "concurrent … in lockstep"
        # for a sequential cluster (the deepgram f19 mislabel this repo already forbids in
        # the appendix). Missing marker (legacy) defaults to concurrent, the matrix case.
        f["cluster_legs_concurrent"] = bool(concurrent)
        crit_wf = crit_by_wf.get(wf_path, {})
        # Scope the floor to the cluster legs the shared step actually lowers (they
        # run concurrently, so own_max = the slowest of THESE legs); a slower
        # non-cluster check on the PR then correctly floors the saving.
        _apply_structural_cascade(
            f, credit.wall_clock_s, wf_path, crit_wf, events_by_wf,
            pr_checks_tuple, pr_check_populations,
            own_check_names=frozenset(jobs.keys()),
            chain_members=chain_members,
            chain_p50_s=chain_p50_s, chain_win_s=chain_win_s,
            # #44 — OPT73 IS a cluster-floor lever by construction: the shared step
            # recurs across all these concurrent legs, so cutting it lowers them
            # together. A sibling leg must not cap the win via chain_win_s (mastodon
            # f83: capped at ~40s vs the true ~639s non-sibling floor).
            cluster_floor_lever=True)
        # Rare-presence demotion. The cascade floors by population-weighted
        # MAGNITUDE but does not know the spine's PRESENCE demotion: when every
        # cluster job is opt-in/rare (path-/label-gated, present on <= half the
        # sampled PRs), the spine excludes them from the typical merge-gating
        # critical path. Such a finding must NOT also carry a credited
        # on-critical-path wall-clock win — the report's appendix would then
        # banner it as "sits ON the merge-gating critical path" while the spine's
        # own footnote calls the same jobs rarely-run throughput/cost. Floor the
        # wall-clock to 0 (bill-only) so the two models agree; the runner-minute
        # saving is untouched.
        # Presence-weight the wall-clock credit (#56). Two demotions, both floor it to bill-only:
        #   (b1) ALL cluster jobs are one-path outliers (`_affected_jobs_all_rare`) — even a
        #        universally-present cluster that is never the per-PR slowest; or
        #   (b2) the whole WORKFLOW gates a MINORITY of sampled PRs (`_workflow_gates_minority`) —
        #        e.g. `tests_secondary.yml` gates 2/20, so a TYPICAL PR never waits on the cluster
        #        even though ONE of its legs (`Windows (firefox)`, pole 2/20) clears the recurring-
        #        gate floor and so escapes (b1). Both mean the cluster is off the typical merge-
        #        gating critical path, so an on-critical-path WALL-CLOCK win would contradict the
        #        spine; the runner-minute (bill) saving survives untouched.
        # `wf_gate_freq` (how often ANY of this workflow's checks is the per-PR slowest) is summed in
        # `check_pole_freq`'s CHECK-CONTEXT key domain via `_map_check_to_job`, NOT by looking up
        # `crit["job_p50"]`'s raw job-API names — see `_workflow_gate_freq` for why a raw job-name
        # sum would spuriously demote a name-divergent majority workflow.
        wf_gate_freq = _workflow_gate_freq(check_pole_freq, crit_by_wf, wf_path)
        _all_rare = _affected_jobs_all_rare(sorted(jobs.keys()), check_pole_freq,
                                            present_n_pr, req_names)
        _wf_minority = _workflow_gates_minority(wf_gate_freq, present_n_pr)
        if (f.get("wall_clock_p50_s") or 0) > 0 and (_all_rare or _wf_minority):
            f["wall_clock_p50_s"] = 0.0
            f["realization"] = "none"
            f["tier"] = 2
            prev = f.get("size_note") or ""
            # Family framing OUTSIDE the claims layer — deliberate: this `size_note` flows into
            # findings.json and is NEVER rendered into the report (blocking_path reads size_note
            # only for a boolean substring test), so it needs no `Claim`. If size_note ever
            # starts rendering, this must be routed through the claims layer (plan 007) — this
            # comment is the tripwire, and verify_report's coverage check would catch the output.
            if _all_rare:
                # State the ACTUAL demotion reason: the cascade fires on pole FREQUENCY
                # (`_affected_jobs_all_rare` → each job is the per-PR slowest on < _POLE_RECUR_FLOOR
                # PRs), NOT on presence. Narrating "run on only ~20/20 sampled PRs (opt-in /
                # path-gated)" for a cluster present on every PR contradicts its own presence count
                # (the expo/expo case). Report the max pole frequency across the cluster jobs — how
                # often ANY of them is actually the slowest check a PR waits on.
                pole_max = max((check_pole_freq.get(j, 0) for j in jobs), default=0)
                demote = (
                    f"off the typical merge-gating critical path — the cluster jobs are the actual "
                    f"slowest check a PR waits on, on only ~{pole_max}/{present_n_pr} sampled PRs "
                    f"(a slower concurrent check gates ahead of them), so the spine demotes them; "
                    f"this is a runner-minute (bill) saving, not developer wall-clock")
            else:
                # (b2) WORKFLOW-level minority: the whole workflow is the per-PR slowest on only a
                # minority of PRs, so a typical PR never waits on this cluster at all.
                demote = (
                    f"off the typical merge-gating critical path — `{wf_path}` is the per-PR "
                    f"slowest workflow on only ~{wf_gate_freq}/{present_n_pr} sampled PRs, so a "
                    f"typical PR does not wait on this cluster (a minority-present anchor cannot "
                    f"crown the typical-PR bottom line); this is a runner-minute (bill) saving, "
                    f"not developer wall-clock")
            f["size_note"] = f"{prev}; {demote}" if prev else demote
        f["runner_min_saving"] = rm_per_mo
        out.append(f)
    return out


_TEST_JOB_RE = _re.compile(
    r"\b(test|tests|e2e|integration|playwright|vitest|jest|pytest|cypress|spec|specs)\b",
    _re.IGNORECASE,
)
_MATRIX_PARENS_RE = _re.compile(r"\([^)]+\)\s*$")
_SHARD_NAME_RE = _re.compile(r"shard|partition", _re.IGNORECASE)
# A dynamic changed-file test gate (mastra `changed-tests`) diffs base..head at
# runtime and runs only the AFFECTED tests. It has NO fixed suite to shard —
# OPT24's remedy (`--shard`/matrix split) can't apply to an unknown-at-author-
# time test set — and it self-skips (~65s) on docs/no-test PRs, so its p50 is
# bimodal, not a steady long suite. Such a job stays a measured long pole
# (inherent cost, surfaced in the critical-path table) but is NOT an actionable
# OPT24 sharding finding. Match the job name; the bimodality check below is the
# name-independent backstop.
_DYNAMIC_TEST_GATE_RE = _re.compile(r"\b(changed|affected|impacted|since)\b", _re.IGNORECASE)


def _job_needs_relations(doc: dict[str, Any], target_base: str) -> dict[str, str]:
    """Classify every OTHER base job's run-order relationship to `target_base`,
    derived from the workflow's `needs:` graph — NOT from measured durations.

    Returns `{base_name: relation}` where relation is one of:
      - "before":   `target_base` transitively `needs:` it, so it runs first (serial)
      - "after":    it transitively `needs:` `target_base`, so it runs after (serial)
      - "parallel": no `needs:` path either way — genuinely concurrent

    Bases absent from the returned map (or when the doc carries no usable `jobs:`)
    are treated as "parallel" by the caller. That is the conservative,
    behavior-preserving default the rest of the report uses when the graph can't
    prove a dependency — without it OPT24's evidence would call a `needs:`-chained
    sibling "in parallel" purely because its p50 is shorter (the same falsehood
    OPT73 carried for cluster jobs). The default is also SAFE: it never invents a
    false sequential edge, only ever falls back to the prior parallel wording.

    `needs:` references job KEYS, so each key is mapped to a base name using the
    same name-vs-key heuristic as `_sharded_bases` — `name:` when it is a plain
    literal, else the job key. (Unlike `_sharded_bases`, which keeps a set and so
    can stash BOTH candidates, this returns a 1:1 key→base map and keeps the single
    primary candidate.) KNOWN LIMITATION: a matrix job whose `name:` interpolates
    `${{ }}` renders at runtime as e.g. `test 3.10` (the matrix value inline, no
    parens), which `_matrix_base_name` cannot reduce back to the job key — so its
    `target_base` won't match this map and the helper returns {}, leaving the
    caller on the safe parallel default. Plain matrix rendering (`unit-test (3.10)`,
    key + parens) resolves correctly."""
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return {}
    key_base: dict[str, str] = {}
    needs_of: dict[str, list[str]] = {}
    for key, job in jobs.items():
        if isinstance(job, dict):
            name = job.get("name")
            cand = name if isinstance(name, str) and "${{" not in name else key
            n = job.get("needs")
            if isinstance(n, str):
                n = [n]
            needs_of[str(key)] = [str(d) for d in (n or []) if isinstance(d, str)]
        else:
            cand = key
            needs_of[str(key)] = []
        key_base[str(key)] = _matrix_base_name(str(cand))
    target_keys = {k for k, b in key_base.items() if b == target_base}
    if not target_keys:
        return {}

    def _closure(seed: "set[str]", edges: dict[str, list[str]]) -> "set[str]":
        seen: set[str] = set()
        stack = list(seed)
        while stack:
            for nxt in edges.get(stack.pop(), []):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    # Downward closure: jobs the target transitively `needs:` run BEFORE it.
    before_keys = _closure(target_keys, needs_of)
    # Upward closure (reverse edges): jobs that transitively `needs:` the target
    # run AFTER it.
    rdeps: dict[str, list[str]] = {}
    for k, deps in needs_of.items():
        for d in deps:
            rdeps.setdefault(d, []).append(k)
    after_keys = _closure(target_keys, rdeps)

    rel: dict[str, str] = {}
    for b in set(key_base.values()):
        if b == target_base:
            continue
        keys = {k for k, bb in key_base.items() if bb == b}
        # A base on a `needs:` path either way is sequential; prefer the truthful
        # sequential label if any of its keys straddle the target.
        if keys & before_keys:
            rel[b] = "before"
        elif keys & after_keys:
            rel[b] = "after"
        else:
            rel[b] = "parallel"
    return rel


def _detect_opt24_long_test_no_sharding(
    wf_path: str, jobs_per_run: list[list[dict[str, Any]]], start_idx: int,
    monthly_volume: int | None = None, sharded_bases: "set[str] | None" = None,
    wf_doc: "dict[str, Any] | None" = None,
) -> list[dict[str, Any]]:
    """Long test jobs with no sharding — catalog body OPT24.

    Heuristic (verbatim from the catalog):
      - Identify test jobs with wall-clock time > 5 min.
      - Check if sharding is configured (presence of a `shard`/`partition`
        matrix axis would appear in the job's name parens).
      - Flag if a test-named job sustains p50 > 5 min AND has no shard
        marker.

    Static-only detection can't measure the >5 min threshold, so this
    detector lives in collect_runs and uses the gh-sampled durations.

    `sharded_bases` (from `_sharded_bases` over the parsed workflow YAML) names base
    jobs already sharded via a numeric split axis (`split_index: [1,2]`) or a
    command-line splitter (pytest-split `--splits`, `--shard=i/n`, nextest partition).
    Those leave no literal `shard`/`partition` token in the rendered job name, so the
    name-only heuristic below can't see them — without this they were false-positived
    as 'no shard axis observed' on a job that is already split.
    """
    sharded_bases = sharded_bases or set()
    # Jobs that are LEGS of a heterogeneous matrix (different packages/configs)
    # are OPT25's domain ("Matrix Leg Imbalance — split the slow leg"), not
    # OPT24's ("unsharded suite"). Flagging such a leg here too double-counts
    # the same job with a contradictory saving, so skip them.
    matrix_legs = set(_detect_common_suffix_matrices(jobs_per_run))
    # `_detect_common_suffix_matrices` only catches PREFIX-varying matrices, and the
    # name-only `_SHARD_NAME_RE` below only matches a literal `shard`/`partition` token —
    # so a trailing NUMERIC `(i, N)` shard leg (`… Internal (2, 8)` … `(8, 8)`) slips past
    # both, and OPT24 would collapse the 8 legs to their base and report 'no shard axis
    # observed', contradicting OPT25 (which DOES group those legs) on the SAME base + runs.
    # Treat such a numeric shard axis as already-sharded — defer the base to OPT25.
    matrix_legs |= _numeric_shard_matrix_legs(jobs_per_run)
    by_base: dict[str, list[tuple[float, str]]] = {}
    # Sampled job INSTANCES per base (kept alongside `by_base`'s durations) so the
    # sharding saving can decompose the job into its per-step waterfall and credit
    # only the SHARDABLE test payload — a build/setup step re-runs on every shard,
    # so it can't be halved (see the payload cap in the sizing below).
    inst_by_base: dict[str, list[dict[str, Any]]] = {}
    # Stats for EVERY job (matrix legs collapsed to their base), so the evidence
    # can show the flagged test job IN CONTEXT of its sibling jobs — the proof
    # that it's the serial long pole worth sharding, not just a long job.
    all_by_base: dict[str, list[tuple[float, str]]] = {}
    has_shard_sibling: dict[str, bool] = {}
    for run_jobs in jobs_per_run:
        for j in run_jobs:
            name = str(j.get("name", ""))
            base = _matrix_base_name(name)
            d, url = _job_dur_url(j)
            if d is None or d <= 0:
                continue
            all_by_base.setdefault(base, []).append((d, url))
            if name in matrix_legs:
                continue  # a heterogeneous-matrix leg → OPT25 handles it
            # Skip jobs without a test-y base name.
            if not _TEST_JOB_RE.search(base):
                continue
            # Track per-base p50.
            by_base.setdefault(base, []).append((d, url))
            inst_by_base.setdefault(base, []).append(j)
            # Does the job have a sibling with `shard` / `partition` in its
            # name? That is the only signal we have that sharding is wired
            # in the YAML — emit name parens like "test (shard 1/4)" or
            # "test (partition 1)".
            inner = _MATRIX_PARENS_RE.search(name)
            if inner and _SHARD_NAME_RE.search(inner.group(0)):
                has_shard_sibling[base] = True
            else:
                has_shard_sibling.setdefault(base, False)
    out: list[dict[str, Any]] = []
    idx = start_idx
    for base, samples in by_base.items():
        if has_shard_sibling.get(base) or base in sharded_bases:
            continue  # sharding already configured (name parens, split axis, or splitter cmd)
        if len(samples) < 2:
            continue
        durs = [d for d, _ in samples]
        p50 = _percentile(durs, 50)
        if p50 < 300:  # catalog threshold: 5 min
            continue
        # A dynamic changed-file gate has no fixed suite to shard — exclude it
        # (it stays a measured long pole in the critical-path table, just not an
        # OPT24 finding). Never headline a -Ns sharding saving on a job sharding
        # can't touch.
        if _DYNAMIC_TEST_GATE_RE.search(base):
            continue
        # Name-independent backstop: bimodal durations (a self-skip cluster well
        # below p50 plus a full-run cluster) mark a conditional/dynamic job, not
        # a steady suite. Sharding a workload that's absent on a large share of
        # runs doesn't cleanly apply, so skip.
        short = [d for d in durs if d < max(90.0, 0.25 * p50)]
        if len(short) >= max(2, round(0.2 * len(durs))):
            continue
        idx += 1
        # Wall-clock saving from sharding: split the job across N shards (assume
        # N=2 conservative). Sharding does not save runner-min (catalog body
        # is explicit on this), so leave rm=0.
        #
        # Only the TEST PAYLOAD shards — a build/setup/install step (e.g. a serial
        # `Build … image`) re-runs on every shard, so it can't be halved. Crediting
        # half the WHOLE job over-claims exactly on the build-dominated jobs OPT72
        # flags (it would render a ~p50/2 saving that contradicts OPT72's own
        # "build is 53% of this job" decomposition on the same job). Decompose the
        # job into its per-step waterfall and cap the saving at half the shardable
        # payload (never more than half the whole job). Falls back to p50/2 only
        # when there are no step timings to decompose.
        wc = round(p50 / 2.0, 1)
        decomp = _decompose_job_steps(inst_by_base.get(base, []))
        if decomp is not None and decomp["payload_s"] > 0:
            shardable = min(decomp["payload_s"], p50)
            wc = round(shardable / 2.0, 1)
        p95 = _percentile(durs, 95)
        slow_d, slow_url = max(samples, key=lambda du: du[0])
        # Rank every job in the workflow by P50 so the evidence shows WHY
        # sharding helps: the flagged test job is the long pole that gates the
        # whole run, towering over the jobs that already finish in parallel.
        # Without this context "422s" alone doesn't prove a shard is needed —
        # the run's wait could be gated by some other equally-slow job.
        ranked = sorted(
            ((b, _percentile([d for d, _ in s], 50),
              _percentile([d for d, _ in s], 95), len(s),
              max(s, key=lambda du: du[0]))
             for b, s in all_by_base.items()),
            key=lambda r: -r[1])
        siblings = [r for r in ranked if r[0] != base]
        if siblings:
            # Multi-job workflow: show the long pole IN CONTEXT of its siblings.
            # Run-order vs. the long pole comes from the `needs:` graph, NOT the
            # measured durations — a shorter sibling wired downstream via `needs:`
            # runs AFTER this job, not in parallel, so labeling it "parallel" off
            # its p50 alone would be a falsehood (the OPT73 bug in another guise).
            relations = _job_needs_relations(wf_doc or {}, base)
            seq_sibs = [r for r in siblings
                        if relations.get(r[0]) in ("before", "after")]
            par_sibs = [r for r in siblings
                        if relations.get(r[0]) not in ("before", "after")]
            # Is the flagged TEST job actually the wall-clock pole? It was flagged only
            # for being a >5min unsharded test job. A longer CONCURRENT (parallel)
            # sibling means it is NOT the pole — that sibling gates the run, and
            # sharding this job saves ~0 wall-clock. A longer SEQUENTIAL sibling does
            # NOT disqualify it: both are on the critical path, so sharding this serial
            # segment still cuts the run. So judge "pole" against the PARALLEL set via
            # the needs: graph — never the raw global max (the OPT73 lesson again).
            # `>=`, not `>`: a concurrent sibling at EQUAL max p50 still gates the run after
            # this job is sharded (the run can't finish before that tied job), so `base` is
            # not the sole pole and sharding it saves ~0 — exactly the overcredit a strict
            # `>` let through (two symmetric suites both at p50).
            parallel_longer = [r for r in par_sibs if r[1] >= p50]
            is_global_pole = not parallel_longer
            # Sharding can't pull the run below a longer concurrent sibling — when one
            # exists the real wall-clock saving is 0, not p50/2.
            wc_eff = 0.0 if parallel_longer else wc
            rows: list[list[str]] = []
            for b, bp50, bp95, bn, (bsd, bsurl) in ranked:
                if b == base:
                    role = ("**long pole — one serial job, no shard axis**"
                            if is_global_pole else
                            "**flagged: long unsharded test job (a concurrent job gates the run)**")
                elif relations.get(b) == "after":
                    role = "runs after — `needs:` this job (sequential)"
                elif relations.get(b) == "before":
                    role = "runs before — this job `needs:` it (sequential)"
                elif bp50 > p50:
                    role = "runs in parallel, LONGER than this job"
                elif bp50 == p50:
                    role = "runs in parallel, AS LONG as this job (also gates)"
                else:
                    role = "runs in parallel, finishes before this job"
                rows.append([f"`{b}`", f"{bp50:.0f}s", f"{bp95:.0f}s", str(bn),
                             _link(bsd, bsurl), role])
            # Describe the siblings by their ACTUAL mix, never claiming a
            # concurrency that isn't there: all-parallel keeps the original
            # wording; all-sequential says so outright; a mix names both. Either
            # way the job is still the serial long pole that gates wall-clock.
            if parallel_longer:
                # A CONCURRENT sibling is longer — IT gates wall-clock, and sharding
                # this job alone can't pull the run below it. (A longer SEQUENTIAL
                # sibling is NOT this case — it's on the path WITH this job, so the
                # `is_global_pole` branches below still treat this job as a real serial
                # segment worth sharding.)
                slower = parallel_longer[0]
                gating = (f"`{slower[0]}` runs concurrently and is at least as long "
                          f"({slower[1]:.0f}s), so IT — not this job — gates the run's "
                          f"wall-clock; sharding this job alone won't cut the run.")
            elif not seq_sibs:
                gating = ("Every other job finishes earlier and in parallel, so "
                          "the run's wall-clock is gated by this one serial job.")
            elif par_sibs:
                gating = ("Every other job is either concurrent with it or "
                          "`needs:`-chained around it, so the run's wall-clock is "
                          "still gated by this one serial job.")
            else:
                gating = ("Every other job is `needs:`-chained around it — running "
                          "before or after, never concurrently — so the run's "
                          "wall-clock is still gated by this one serial job.")
            # Gap to the next-longest job — only a CLAIM when this job is genuinely the
            # global max (every sibling, parallel or sequential, is strictly shorter);
            # otherwise a longer sibling exists and the prose above already names it.
            next_job = siblings[0]
            gap_clause = ""
            if p50 > next_job[1] > 0:
                ratio = p50 / next_job[1]
                gap_clause = (f" — {ratio:.1f}× the next-longest job "
                              f"(`{next_job[0]}`, {next_job[1]:.0f}s)")
            pole_phrase = ("the long pole of this workflow" if is_global_pole
                           else "a long unsharded test job in this workflow")
            if is_global_pole:
                why = (f"The run waits on `{base}` because no concurrent job takes as "
                       f"long; splitting it across parallel shards parallelizes the long "
                       f"pole, cutting the critical path by ~{wc_eff:.0f}s")
            else:
                why = (f"`{parallel_longer[0][0]}` runs concurrently and is at least as "
                       f"long, so sharding `{base}` alone doesn't cut the run's wall-clock "
                       f"(~0s saved until that job is addressed too)")
            me = _measured_evidence(
                ["Job", "P50", "P95", "Samples", "Slowest run (job log)", "Role"],
                rows,
                summary=(f"`{base}` is {pole_phrase} at P50 {p50:.0f}s "
                         f"(P95 {p95:.0f}s) over {len(samples)} sampled runs{gap_clause}, "
                         f"and it runs as a single job with no `shard`/`partition` axis. "
                         f"{gating}"),
                note=(f"Each link opens that job's slowest observed run. {why} "
                      f"(does not reduce runner-minutes)."))
        else:
            # Single-job workflow: no siblings to contextualize against, so the
            # evidence is just that this one job runs long and unsharded.
            me = _measured_evidence(
                ["Job", "P50", "P95", "Samples", "Slowest run (job log)"],
                [[f"`{base}`", f"{p50:.0f}s", f"{p95:.0f}s", str(len(samples)),
                  _link(slow_d, slow_url)]],
                summary=(f"`{base}` runs unsharded at P50 {p50:.0f}s (P95 {p95:.0f}s) "
                         f"over {len(samples)} sampled runs — no `shard`/`partition` "
                         f"axis in any sampled job name."),
                note=("The link opens the slowest observed run's job log. Sharding "
                      "splits this work across parallel jobs, cutting the critical "
                      f"path by ~{wc:.0f}s (does not reduce runner-minutes)."))
        # `wc_eff` is `wc` for a single-job workflow and the genuine pole; it is 0 when
        # a longer concurrent sibling means sharding saves no wall-clock. The size_note
        # must not claim "the long pole" for a non-pole finding (it survives into the
        # report via the "measured" sizing model's setdefault).
        wc_final = wc if not siblings else wc_eff
        size_note = ("sharding parallelizes the long pole — saves wall-clock, does NOT "
                     "save runner-min (more jobs, more setup tax)") if (
                         not siblings or is_global_pole) else (
            "a longer concurrent job gates the run, so sharding this one saves ~0 "
            "wall-clock until that job is addressed; no runner-min saving either")
        out.append(_new_finding(
            "OPT24", "HIGH", "Long Test Job Without Sharding", wf_path, base,
            f"job `{base}` p50 {p50:.0f}s over {len(samples)} runs, no shard "
            "axis observed (job names lack a `shard` / `partition` matrix marker)",
            "long-test-job-without-sharding",
            "opt24--long-test-job-without-sharding", idx,
            wc_p50=wc_final, rm=0.0,
            size_note=size_note,
            # A zeroed `wc_final` (a longer/tied concurrent sibling gates the run) isn't a
            # realizable wall-clock win — mirror OPT25 and label it `none`, not `direct`.
            realization=("direct" if wc_final > 0 else "none"), measured_evidence=me,
        ))
    return out


# Declared triggers that mean "a developer waits on this workflow to merge a PR".
_PR_TRIGGER_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})


def _on_has_pr_trigger(on: Any) -> bool:
    """Does a parsed workflow `on:` block declare a PR-gating trigger? Handles the
    three YAML shapes `on:` can take — a bare string (`on: pull_request`), a list
    (`on: [push, pull_request]`), or a mapping (`on: {pull_request: {...}}`)."""
    if isinstance(on, str):
        return on in _PR_TRIGGER_EVENTS
    if isinstance(on, (list, dict)):
        return any(e in _PR_TRIGGER_EVENTS for e in on)
    return False


def _git_out(root: "Path", *args: str) -> str | None:
    """`git -C <root> <args>` stdout, or None if git isn't there / the command fails
    / `root` isn't a checkout. Never raises — every caller degrades to "unknown"."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("git %s failed under %s: %s", " ".join(args), root, e)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _repo_slug_from_remote(url: str) -> str | None:
    """`owner/name` from a git remote URL — the SSH, HTTPS and git-protocol forms."""
    u = url.strip().removesuffix(".git")
    m = re.search(r"[:/]([^/:]+/[^/]+)$", u)
    return m.group(1).lower() if m else None


def _root_is_clone_of(root: "Path | None", repo: str | None) -> bool:
    """Is the checkout at `root` actually a clone of `repo` (`owner/name`)?

    The local workflow read (below) feeds the SIZING pipeline the YAML it parses, while
    every timing number comes from `--repo`'s API. A `--root` pointing at a DIFFERENT
    repo would silently marry one repo's workflow definitions to another's measurements
    — so verify the origin remote and fall back to the API when it doesn't match (or
    when we cannot tell: no git, no remote, a detached tarball). Unknown != match."""
    if root is None or not repo:
        return False
    url = _git_out(root, "remote", "get-url", "origin")
    if not url:
        logger.debug("no origin remote under %s — using the gh contents API for "
                     "workflow YAML rather than an unverifiable checkout", root)
        return False
    slug = _repo_slug_from_remote(url)
    if slug != repo.strip().lower():
        logger.warning(
            "--root %s is a checkout of %s, not %s — reading workflow YAML from the "
            "gh contents API instead. (A mismatched --root would size one repo's "
            "workflows against another repo's timings.)", root, slug or url, repo)
        return False
    return True


def _workflows_are_dirty(root: "Path | None") -> bool:
    """Does the checkout have UNCOMMITTED changes under `.github/workflows`?

    The report stamps the checkout's HEAD sha as the audited commit, and the detectors
    now parse the checkout's WORKING TREE. If a user edits a workflow and re-runs, the
    detectors read the EDITED yaml while every timing number still comes from the API's
    runs on the UNEDITED branch — a report that says "clean", stamped with a commit
    whose YAML never contained the edit. `run.py` turns a True here into a `<sha>-dirty`
    provenance stamp so the skew is disclosed rather than silently absorbed."""
    if root is None:
        return False
    out = _git_out(root, "status", "--porcelain", "--", ".github/workflows")
    return bool(out)


def _root_branch_skew(root: "Path | None",
                      default_branch: str | None) -> dict[str, Any] | None:
    """Is the checkout we're about to parse workflow YAML from on a DIFFERENT commit
    line than the branch the sampled runs came from? Returns a disclosure record, or
    None for "no skew we can see".

    `_root_is_clone_of` only proves the checkout is the RIGHT REPO. It says nothing
    about WHICH COMMIT, and that is the half that bites: a clean checkout on a feature
    branch, or a `main` that hasn't been pulled in three weeks, is not DIRTY — no
    `-dirty` marker fires, the stamped sha is perfectly true — and yet the detectors are
    parsing YAML that produced NONE of the sampled runs. That is the hazard
    `_fetch_workflow_docs` names (a workflow that gained a `pull_request` trigger last
    week has PR runs in the sample but a push-only `on:` block in an old checkout, so
    `_declared_pr_workflows` drops a real PR gate), and `--root` is now on for every
    run — so it has to be checked, not just documented.

    We DISCLOSE rather than silently switch to the API: "parse what you stamp as
    audited" is the invariant the reader can check, and quietly parsing the default
    branch while stamping the checkout's sha would break it in the other direction.
    Named in the report; the reader decides whether the skew matters.

    What we can see, cheaply and offline:
      * the checked-out branch differs from the repo's default branch (incl. a detached
        HEAD), or
      * it IS the default branch but the local tracking ref `origin/<default>` shows the
        checkout is behind (stale) or ahead (unpushed local commits).
    A checkout with no `origin/<default>` ref (never fetched, a shallow tarball) can't
    be compared on the second point — we say nothing rather than assert freshness."""
    if root is None or not default_branch:
        return None
    branch = _git_out(root, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch:
        return None
    if branch == "HEAD":
        return {"branch": None, "default_branch": default_branch,
                "reason": "the checkout is on a DETACHED HEAD, not the default branch "
                          f"`{default_branch}`"}
    if branch != default_branch:
        return {"branch": branch, "default_branch": default_branch,
                "reason": f"the checkout is on branch `{branch}`, not the default "
                          f"branch `{default_branch}` the sampled runs came from"}
    remote_ref = f"origin/{default_branch}"
    if _git_out(root, "rev-parse", "--verify", "--quiet", remote_ref) is None:
        return None                       # no tracking ref — nothing we can compare to
    counts = _git_out(root, "rev-list", "--left-right", "--count",
                      f"HEAD...{remote_ref}")
    if not counts:
        return None
    try:
        ahead, behind = (int(x) for x in counts.split())
    except ValueError:
        return None
    if not ahead and not behind:
        return None
    parts = []
    if behind:
        parts.append(f"{behind} commit(s) BEHIND `{remote_ref}`")
    if ahead:
        parts.append(f"{ahead} local commit(s) not on `{remote_ref}`")
    return {"branch": branch, "default_branch": default_branch,
            "ahead": ahead, "behind": behind,
            "reason": f"the checkout is on `{branch}` but is " + " and ".join(parts)}


def _read_local_workflow(root: "Path | None", wf_path: str) -> str | None:
    """The workflow file's text read from the LOCAL checkout, or None if `root`
    wasn't supplied / the file isn't there / it can't be read.

    `wf_path` is the repo-relative path the gh API uses (`.github/workflows/x.yml`),
    which is exactly where it sits under `--root`. Resolved and re-checked against
    `root` before reading, so a path that escapes the checkout (`..`) falls through
    to the API rather than reading an arbitrary file off the maintainer's disk.

    `ValueError` is caught alongside `OSError`: `Path.resolve()` / `is_relative_to`
    raise it (not OSError) on a path the OS rejects outright — an embedded null byte,
    say — and a crash there is a strictly worse outcome than the API fallback."""
    if root is None or not wf_path:
        return None
    try:
        base = root.resolve()
        target = (base / wf_path).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as e:
        logger.debug("could not read local workflow %s: %s", wf_path, e)
        return None


def _fetch_workflow_docs(
    client: "GhClient", repo: str, wf_paths: Any,
    *, root: "Path | None" = None,
    source_counts: dict[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Parse each workflow FILE once into `{wf_path: parsed_yaml}`, so the consumers
    that need the file body (the declared-trigger guard and the OPT24 shard
    recognizer) share ONE read per workflow rather than each doing its own.

    Why read the file at all? The REST `GET /actions/workflows` listing carries only
    id/name/path/state — neither the `on:` block nor the jobs' matrix/run steps — so
    they have to come from the content.

    Read from the LOCAL CHECKOUT (`root`, i.e. `run.py --root`) when the file is
    there, falling back to `GET /contents/{path}` when it isn't (no `--root`, a
    `--root` that isn't a clone of `--repo`, or a workflow that exists on the default
    branch but not in this checkout).

    WHICH SOURCE IS RIGHT is a real tradeoff, and the choice here is deliberate:
      - `/contents/` serves the DEFAULT BRANCH's HEAD. The report stamps the CHECKOUT's
        sha as the audited commit, so on a checkout that lags or leads the default
        branch the API silently parses different YAML than the report claims.
      - The checkout's WORKING TREE is what the report stamps, so parsing it makes the
        stamp true — but it can also be dirty, or on a feature branch that produced
        NONE of the sampled runs (a workflow that gained a `pull_request` trigger last
        week would have PR runs in the sample but a push-only `on:` block in an old
        checkout, and `_declared_pr_workflows` would drop a real PR gate).
    Neither source is unconditionally right; they disagree exactly when the checkout
    and the default branch disagree. We prefer the checkout — it is the thing we STAMP,
    and "parse what you claim to have audited" is the invariant a reader can check —
    and we DISCLOSE the skew rather than hide it: `run.py` stamps `<sha>-dirty` when
    `.github/workflows` has uncommitted edits (`_workflows_are_dirty`), and
    `source_counts` (surfaced in `data_sources.workflow_yaml_source`) records how many
    workflows came off disk vs the API.

    The API is the fallback for a local read that produces NO USABLE DOC — not only for
    a MISSING file. A file that is present but empty, half-written, carrying merge
    conflict markers, or otherwise un-`safe_load`-able is exactly as uninformative as an
    absent one, and dropping it there would silently no-op every `wf_doc`-gated detector
    (`_opt35_shard_job_specs`, `_opt57_timeout_job_specs`, OPT24's shard recognizer,
    `_declared_pr_workflows`) for that workflow — findings absent, which a reader reads
    as CLEAN. The default-branch copy still parses, so fetch it: a broken working copy
    degrades the source, never the coverage.

    A path we can neither read nor fetch, or that neither source can parse, or that
    parses to something EMPTY, is OMITTED (unknown != absent): each caller then falls
    back to its own safe default, never silently dropping a real signal on a missing
    read. An empty `{}` is not "a workflow with no triggers and no jobs" — it is "we
    learned nothing"."""
    try:
        import yaml  # PyYAML is already a skill dependency (scan.py parses with it)
    except ImportError:
        logger.debug("PyYAML unavailable — workflow-file signals fall back to "
                     "observed/heuristic defaults")
        return {}
    counts = source_counts if source_counts is not None else {}
    counts.setdefault("checkout", 0)
    counts.setdefault("api", 0)

    def _parse(text: str | None, wf_path: str, source: str) -> dict[str, Any] | None:
        """A NON-EMPTY mapping, or None for "we learned nothing from this source".

        `yaml.safe_load("")` is None and an all-comments file parses to None too;
        coercing either to `{}` would record "we read this workflow and it declares
        nothing", which is a claim we did not earn."""
        if text is None:
            return None
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as e:
            logger.debug("could not parse %s from the %s: %s", wf_path, source, e)
            return None
        return parsed if isinstance(parsed, dict) and parsed else None

    def _api_text(wf_path: str) -> str | None:
        doc = client.json(f"repos/{repo}/contents/{wf_path}", allow_missing=True)
        content = doc.get("content") if isinstance(doc, dict) else None
        if not content:
            return None
        try:
            return base64.b64decode(content).decode("utf-8", "replace")
        except ValueError as e:
            logger.debug("could not decode %s: %s", wf_path, e)
            return None

    paths = list(dict.fromkeys(p for p in wf_paths if p))  # dedup, keep order
    docs: dict[str, dict[str, Any]] = {}
    # First read every workflow from the LOCAL CHECKOUT — a local-disk read (no gh call),
    # so it stays serial. The `contents` API is only the FALLBACK for a path the checkout
    # can't serve (missing / empty / conflict-markered / invalid YAML — all the same fact,
    # "no doc"). Collect exactly those API-bound paths so LEVER 3 can fan their contents
    # reads out in ONE prefetch wave. Prefetching EVERY path (as the pre-merge concurrency
    # PR did, before the checkout-first source existed) would park a response for every
    # checkout-served workflow that no `_api_text` call ever consumes — counted as
    # `prefetch_unconsumed` drift, tripping the drain-prefetch invariant. Parsing below
    # stays in `paths` order, so `docs` is built identically.
    api_paths: list[str] = []
    for wf_path in paths:
        parsed = _parse(_read_local_workflow(root, wf_path), wf_path, "checkout")
        if parsed is not None:
            docs[wf_path] = parsed
            counts["checkout"] += 1
        else:
            api_paths.append(wf_path)
    # LEVER 3: one `contents` read per API-bound workflow, issued as a single wave. The
    # reads are independent; `allow_missing` matches `_api_text`'s call site (an
    # unfetchable path is OMITTED, never faked).
    _prefetch_json(client, [f"repos/{repo}/contents/{p}" for p in api_paths],
                   allow_missing=True)
    for wf_path in api_paths:
        parsed = _parse(_api_text(wf_path), wf_path, "contents API")
        if parsed is not None:
            docs[wf_path] = parsed
            counts["api"] += 1
    return docs


def _declared_pr_workflows(
    client: "GhClient", repo: str, wf_paths: Any,
    *, wf_docs: dict[str, dict[str, Any]] | None = None,
) -> frozenset[str]:
    """The subset of `wf_paths` whose workflow FILE declares a PR-gating trigger,
    read from the YAML `on:` block. This is ground truth — independent of which
    sampled runs we happened to catch — so a real PR gate whose recent successes
    were all push runs is not excised on a sampling artifact.

    `wf_docs` is the shared parsed-workflow map from `_fetch_workflow_docs`; when
    omitted (direct callers / tests) it is fetched here. A path absent from the map
    (unfetchable/unparseable) is OMITTED (unknown != not-a-PR-gate): the caller then
    falls back to the workflow's OBSERVED sampled events for it."""
    docs = wf_docs if wf_docs is not None else _fetch_workflow_docs(client, repo, wf_paths)
    out: set[str] = set()
    for wf_path, parsed in docs.items():
        on = parsed.get("on")
        if on is None:
            on = parsed.get(True)  # PyYAML parses the bare key `on:` as boolean True
        if _on_has_pr_trigger(on):
            out.add(wf_path)
    return frozenset(out)


# Sharding idioms beyond a literal `shard`/`partition` matrix-axis NAME, which the
# OPT24 job-name heuristic alone can't see. A numeric split axis renders only a bare
# integer in the job-name parens (`… (redis, 3.14, 1)`), and command-line splitters
# (pytest-split, jest/playwright --shard, nextest partition, knapsack ci-node) leave
# no token in the name at all — so a job sharded that way was wrongly flagged "no shard
# axis observed". Detect both from the workflow YAML.
# `group` REQUIRES an index-like suffix (`group_index`/`group-num`/…) — a bare `group`
# axis is usually a test-TYPE split (`group: [frontend, backend]`), not a shard index, and
# matching it would wrongly suppress a valid OPT24 finding on a genuinely un-sharded suite.
_SPLIT_AXIS_RE = _re.compile(r"shard|partition|chunk|split|group[-_](index|num|id)|"
                             r"ci[-_]?node[-_]?(index|total)", _re.IGNORECASE)
_SHARD_CMD_RE = _re.compile(
    r"--splits?\b|--split[-_]?index\b|--shard(?:[=\s/]|\b)|--num[-_]?shards\b|"
    r"--partition\b|nextest\s+\S*\s*partition|--ci-node-index\b|--ci-node-total\b",
    _re.IGNORECASE)


def _sharded_bases(doc: dict[str, Any]) -> set[str]:
    """Base job names in this workflow that are ALREADY sharded — via a split/shard
    matrix axis OR a command-line splitter in their run steps — so OPT24 must not flag
    them as 'no shard axis observed' just because the rendered job name carries a bare
    integer (the split index) instead of a literal `shard` token. Keyed to the base
    name the runtime job renders as (job `name:` when a plain literal, else the job
    key), matched against `_matrix_base_name(<runtime job name>)`."""
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return set()
    out: set[str] = set()
    for key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy") or {}
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
        axis_sharded = isinstance(matrix, dict) and any(
            _SPLIT_AXIS_RE.search(str(k)) for k in matrix
            if k not in ("include", "exclude"))
        cmd_sharded = any(
            isinstance(s, dict) and _SHARD_CMD_RE.search(str(s.get("run", "") or ""))
            for s in (job.get("steps") or []))
        if not (axis_sharded or cmd_sharded):
            continue
        name = job.get("name")
        # The gh job base is `name:` when it's a plain literal (no matrix interpolation),
        # otherwise the job key; add both candidates, parens-stripped, to be safe.
        for cand in (name if isinstance(name, str) and "${{" not in name else None, key):
            if cand:
                out.add(_matrix_base_name(str(cand)))
    return out


# =============================================================================
# Cache-finding log evidence (--with-logs). A cache finding claims "this work
# is re-done because the cache misses" — the only way a human verifies that is
# the actual cache-hit/miss line in the job log. We fetch the affected job's
# log across a few sampled runs, quote the verbatim cache line, and link the
# run. If the logs show the cache actually HITTING, we say so (and flag the
# finding) rather than assert a miss that isn't there (catalog OPT6/OPT8/OPT37
# guardrails).
# =============================================================================

# Job-level cache findings whose proof lives in run logs. (turbo.json config
# patterns OPT52/53/58/59/60 are verified against the file, not run logs.)
_CACHE_FAMILY = {"OPT2", "OPT3", "OPT5", "OPT6", "OPT9", "OPT41", "OPT42", "OPT61"}

_CACHE_MISS_RE = _re.compile(r"cache not found for|cache miss|no cache entry", _re.I)
_CACHE_HIT_RE = _re.compile(
    r"cache restored from key|cache hit for|cache restored successfully|"
    r"cache hit, replaying", _re.I)
_TS_PREFIX_RE = _re.compile(r"^\S+Z\s+")  # leading ISO timestamp on each log line

# Positive evidence that the cacheable WORK actually ran in this job — an
# install/build line. A cache finding must show the work it claims is uncached
# actually happens; otherwise "no cache line in the log" proves nothing (the job
# may not install/build at all). Used to gate cache findings: no cache line AND
# no install/build activity AND no measurable cost ⇒ drop (unprovable).
_INSTALL_ACTIVITY_RE = _re.compile(
    r"\b(pnpm|npm|yarn|bun)\s+(install|ci|i)\b|--frozen-lockfile|"
    r"packages:\s*\+|added \d+ packages|resolved \d+|"
    r"turbo run|nx run|tsc\b|vite build|next build|"
    r"playwright install|cargo build|go build|pip install|poetry install",
    _re.I)


def _log_has_install_activity(log: str) -> bool:
    return bool(_INSTALL_ACTIVITY_RE.search(log or ""))

# Per-pattern keyword(s) that scope the cache log lines to the cache the
# finding is actually about — so an OPT5 (pnpm store) finding doesn't cite a
# Turbo cache miss. Empty tuple = any cache line is relevant.
_CACHE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "OPT5": ("pnpm",),
    "OPT2": ("playwright",),
    "OPT3": ("turbo",), "OPT41": ("turbo",), "OPT42": ("turbo",),
    "OPT6": (), "OPT9": (), "OPT61": (),
}


def _clean_log_line(line: str) -> str:
    return _TS_PREFIX_RE.sub("", line).strip()[:160]


def _cache_status_from_log(log: str, keywords: tuple[str, ...] = ()) -> tuple[str, str]:
    """(status, representative verbatim line) scoped to `keywords` (the cache
    the finding is about). Prefer a MISS line (the problem); else a HIT line;
    else ('unknown', '') — which for a 'not cached at all' finding is itself
    the proof (no cache line ⇒ the work ran full)."""
    def _relevant(line: str) -> bool:
        return not keywords or any(k.lower() in line.lower() for k in keywords)
    first_hit = ""
    for raw_line in log.splitlines():
        if not _relevant(raw_line):
            continue
        if _CACHE_MISS_RE.search(raw_line):
            return "miss", _clean_log_line(raw_line)
        if not first_hit and _CACHE_HIT_RE.search(raw_line):
            first_hit = _clean_log_line(raw_line)
    if first_hit:
        return "hit", first_hit
    return "unknown", ""


def _job_instances(jobs_per_run: list[list[dict[str, Any]]], job_name: str
                   ) -> list[dict[str, Any]]:
    """The affected job's dicts across sampled runs. Matches the YAML key
    exactly or the matrix display-name prefix (`key (shard 1)`)."""
    out = []
    prefix = job_name + " ("
    for run_jobs in jobs_per_run:
        for j in run_jobs:
            name = str(j.get("name", ""))
            if name == job_name or name.startswith(prefix):
                out.append(j)
    return out


def _attach_cache_log_evidence(client: "GhClient", repo: str,
                               findings: list[dict[str, Any]],
                               jobs_per_run_by_wf: dict[str, list],
                               cap: int = 4) -> None:
    """For each cache-family finding, fetch the affected job's log across up to
    `cap` sampled runs, quote the verbatim cache hit/miss line, and attach
    measured_evidence linking each run. Logs are fetched at most once per job
    id (deduped). Honest: if the cache HITS on every sampled run, the summary
    says so and flags the finding for re-review rather than claiming a miss."""
    log_cache: dict[tuple, tuple[str, str, bool]] = {}  # (job_id, keywords) -> (status, line, install)

    def _targets() -> list[tuple[dict[str, Any], tuple[str, ...], list[dict[str, Any]]]]:
        """(finding, keywords, capped job instances) for every cache-family finding with
        sampled instances — the exact set the walk below reads a log for. One selector,
        used by both the prefetch plan and the walk, so they cannot disagree about which
        logs get fetched."""
        sel = []
        for f in findings:
            pat = f.get("pattern")
            if pat not in _CACHE_FAMILY:
                continue
            jobs = f.get("affected_jobs") or []
            if not jobs:
                continue
            instances = _job_instances(
                jobs_per_run_by_wf.get(f.get("workflow_file", ""), []), jobs[0])
            if not instances:
                continue
            sel.append((f, _CACHE_KEYWORDS.get(pat, ()), instances[:cap]))
        return sel

    targets = _targets()
    # LEVER 3. Job logs are the heaviest single responses in the pass (multi-MB), and
    # these were downloaded strictly back-to-back. Prefetch every log the walk will read
    # in one wave, replaying the walk's own `(job_id, keywords)` dedup so we plan exactly
    # the fetches it performs: a job already resolved under the SAME keywords is served
    # from `log_cache` and never re-requested, so it must not be prefetched twice either.
    _planned: set[tuple] = set()
    _log_todo: list[str] = []
    for _f, _kw, _instances in targets:
        for _j in _instances:
            _jid = _j.get("id")
            # Plan EXACTLY the fetches `_fetch_job_log` performs at the call site below:
            # it skips a job with no id AND a job with no log to serve (`_job_has_log` —
            # never started / skipped), so prefetching those would park a response no
            # consumer pops (prefetch drift) and cost a guaranteed-404 gh call the serial
            # path never made.
            if not _jid or not _job_has_log(_j) or (_jid, _kw) in _planned:
                continue
            _planned.add((_jid, _kw))
            _log_todo.append(_job_log_endpoint(repo, _jid))
    _prefetch_text(client, _log_todo)

    for f, keywords, instances in targets:
        rows, miss_n, hit_n, unknown_n = [], 0, 0, 0
        saw_install = False          # positive evidence the cacheable work runs
        max_dur = 0.0                # largest measured job duration across samples
        for j in instances:          # already capped to `cap` by `_targets`
            jid = j.get("id")
            url = j.get("html_url") or ""
            dur = _job_duration_s(j)
            if dur and dur > max_dur:
                max_dur = dur
            dur_cell = f"{dur:.0f}s" if dur else "—"
            key = (jid, keywords)
            if key in log_cache:
                status, line, install = log_cache[key]
            else:
                log = _fetch_job_log(client, repo, j)
                status, line = _cache_status_from_log(log, keywords) if log else ("unknown", "")
                install = _log_has_install_activity(log) if log else False
                log_cache[key] = (status, line, install)
            saw_install = saw_install or install
            if status == "miss":
                miss_n += 1
            elif status == "hit":
                hit_n += 1
            else:
                unknown_n += 1
            label = {"miss": "MISS", "hit": "hit", "unknown": "no cache line"}[status]
            quoted = f"`{line}`" if line else "_no matching cache line in this run's log_"
            run_link = f"[run]({url})" if url else "run"
            rows.append([run_link, f"**{label}** — {quoted}", dur_cell])
        if not rows:
            continue
        sampled = len(rows)
        noun = f"{keywords[0]} cache" if keywords else "cache"   # "pnpm cache"
        the_cache = f"the {noun}"                                  # "the pnpm cache"
        if miss_n and hit_n:
            summary = (f"Across {sampled} sampled run(s), {the_cache} MISSED on "
                       f"{miss_n} and hit on {hit_n} — bimodal, so the saving is "
                       f"the miss-mode cost × miss rate, not the full step:")
            note = ("Each link opens that run's job log with the verbatim cache "
                    "line. Compare the MISS vs hit run durations to size the win.")
        elif miss_n:
            extra = f" ({unknown_n} showed no cache line)" if unknown_n else ""
            summary = (f"{the_cache.capitalize()} MISSED on {miss_n} of {sampled} "
                       f"sampled run(s){extra} — the work ran full. Verbatim cache "
                       f"line(s) from each run's job log:")
            note = ("Each link opens that run's job log; the quoted line is the "
                    "verbatim cache-miss line from it.")
        elif hit_n:
            summary = (f"⚠️ {the_cache.capitalize()} HIT on all {hit_n} sampled "
                       f"run(s) — no miss observed in logs. Config-hygiene only "
                       f"unless a miss is shown; do NOT claim wall-clock savings:")
            note = ("Logs show the cache restoring. Re-review: the configured "
                    "issue may not actually cost time on this runner.")
            f["size_note"] = ("logs show the cache HITTING on every sampled run — "
                              "treat as config hygiene, savings unconfirmed")
            # The evidence here REFUTES a wall-clock saving (the cache already
            # restores), so don't keep a sized wall-clock number that the
            # evidence contradicts — zero it. (Config hygiene = no measured
            # wall-clock win until a miss is actually shown.)
            f["wall_clock_p50_s"] = 0.0
            f["tier"] = 2
            f["realization"] = "none"
        else:
            # No cache line at all. "No line" alone does NOT prove a defect — the
            # job may not run the cacheable work. Require POSITIVE evidence: an
            # install/build line in the logs, plus a measurable job cost. Without
            # both, the finding is unprovable → drop it (admission gate).
            if not (saw_install and max_dur > 5.0):
                f["_drop"] = (
                    "cache finding unprovable from sampled logs — no cache line "
                    "AND no install/build activity / measurable cost observed, so "
                    "we can't show the cacheable work actually runs here")
                continue
            summary = (f"No {noun} is configured for this job, and its logs show "
                       f"the install/build step running uncached on all {sampled} "
                       f"sampled run(s) (job cost up to {max_dur:.0f}s). Nothing "
                       f"restores {the_cache}, so that work repeats every run:")
            note = ("Each link opens the run's job log — you'll see the "
                    "install/build step run with no `Cache restored` line. Adding "
                    f"{the_cache} skips it on the next run.")
        f["measured_evidence"] = {
            "summary": summary,
            "table": {"headers": ["Run", "Cache result (verbatim log line)",
                                  "Job duration"], "rows": rows},
            "note": note,
        }
        f["measured_signal"] = f.get("measured_signal") or summary


# A magnitude bracket wider than this (range / median) is "wide" - the fix's payoff
# varies run to run and a 3-run probe can't tell a smooth spread from two clusters
# (bimodality: cache sometimes-warm, flaky retries). Only then do we widen the sample.
_MAG_WIDE_REL = 0.25

# --- Cache-distribution grounding (the cache-hit-rate class) ---------------------
# A cache-miss finding (turbo cold/partial, buildx cold, install-lifecycle build) is
# born from ONE drilled run's log. That single run's miss rate is NOT evidence that the
# cache is a broad wall-clock lever: a job can hit the cache ~90% of the time yet the
# drilled (slow-mode, deliberately picked) run shows a heavy miss. So before the
# renderer frames a cache pole as a top "cache-miss / churn" lever we measure the miss
# rate ACROSS the sampled runs, split PR vs push and fork vs upstream, and stamp a
# `cache_dist` verdict the renderer + verify_report both key on. The thresholds:
#
# `_CACHE_PUSH_PROBE_MAX` — targeted-escalation cap. The PR-bucket distribution reuses
# logs already fetched for the cross-run magnitude check (zero new gh calls); the ONLY
# new fetches are up to this many default-branch (push) run logs, and only when a cache
# leaf actually fired. 3 is enough to tell "push is warm" (the healthy majority case)
# from "push also misses" without eroding the skill's frugal-gh budget.
_CACHE_PUSH_PROBE_MAX = 3
# `_CACHE_TAIL_MIN_FRAC` — a miss-heavy minority smaller than this share of qualifying
# (non-fork) PR runs is a TAIL, not the typical developer experience: the fix helps the
# cache-miss-heavy runs, not "CI by X%". At/above it the miss cost is frequent enough to
# keep the finding's lever framing. Parallels `blocking_path._PARTIAL_MISS_FLOOR_PCT`'s
# "most PRs touch few packages" rationale — a quarter of runs paying the miss is real.
_CACHE_TAIL_MIN_FRAC = 0.25
# `_CACHE_COLD_MISS_PCT` — a per-run miss rate at/above this is "effectively 0 cached"
# (a genuinely cold run), the case where a broad cache claim is legitimate. Kept just
# under 100 so a lone restored package doesn't demote an otherwise-cold run.
_CACHE_COLD_MISS_PCT = 99.5
# The leaf fix_keys whose magnitude is a cache miss rate — the poles `cache_dist` grounds.
# Mirror of `blocking_path._CACHE_LEAF_KEYS` (kept coupled by a self-test).
_CACHE_LEAF_KEYS = frozenset({
    "turbo-remote-cache", "turbo-partial-cache", "buildx-no-cache",
    "install-lifecycle-build",
})

# Setup/teardown step names that are NOT the load-bearing work, so they don't get
# picked as a pole's "dominant step" for the generic cross-run check.
_NON_WORK_STEP_RE = _re.compile(
    r"^(set up job|complete job|post\b|checkout\b|set up |setup [a-z]*node)",
    _re.IGNORECASE)


def _dominant_step_sample(
    timeline: dict[str, Any], qual: list[tuple[float, dict[str, Any]]],
    repr_job: dict[str, Any],
) -> dict[str, Any] | None:
    """Fallback cross-run check for a pole whose log matched NO catalog detector (so
    `_magnitude_sample` returns None): validate the DOMINANT STEP's wall time across
    the sampled runs. The dominant step is the longest non-setup step in the drilled
    run's timeline - the same step the waterfall highlights - so the report's deepest
    finding (the step) is still cross-run-validated, not taken from one run on faith.
    No extra gh calls: every qualifying job already carries its steps' start/end.
    Returns a magnitude dict shaped like `_magnitude_sample`'s (unit "s",
    kind "step-wall") or None when there's no usable step."""
    def _f(x: Any) -> float | None:
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    steps = [s for s in timeline.get("steps", [])
             if _f(s.get("dur_s")) and not _NON_WORK_STEP_RE.match(str(s.get("name", "")))]
    if not steps:
        return None
    # Validate the step the structural decomposition CROWNS — the lead step of the
    # dominant CATEGORY — not the single longest step overall (which can sit in a
    # non-dominant category). Keeps the cross-run check, the waterfall, and the agent
    # prompt naming ONE lever for a pole. `_dominant_category_lead` is the shared crown.
    lead = _dominant_category_lead(
        [(str(s.get("name", "")), _f(s.get("dur_s")) or 0.0) for s in steps])
    if lead is None:
        return None
    # Use the duration the helper already chose (the SLOWEST occurrence of the lead step),
    # not a first-textual-match re-lookup — a step name can recur with two non-zero legs (a
    # retried step), and `step_dur` below resolves the MAX occurrence for the per-run values,
    # so `this_run` must too, or the finding contradicts itself + falsely reads "stable".
    name, this_run = lead[0], lead[1]

    def step_dur(job: dict[str, Any]) -> float | None:
        # A job can legitimately carry MULTIPLE steps with the same name — a guarded /
        # skipped zero-duration variant plus the real run (e.g. an emulator step emitted
        # once as a no-op and once for real). `dom` above was picked by MAX duration, so
        # resolve the SAME occurrence here: take the longest-running same-named step, not
        # the first textual match. Matching the first occurrence collapsed the cross-run
        # sample to the wrong (often zero-duration) leg, so the per-run values contradicted
        # this_run and the renderer wrongly reported the magnitude "stable across runs".
        best: float | None = None
        for s in job.get("steps") or []:
            if isinstance(s, dict) and str(s.get("name", "")) == name:
                d = _duration_s(s.get("started_at"), s.get("completed_at"))
                if d is not None and (best is None or d > best):
                    best = d
        return best

    values: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    # Order: drilled first, then fastest + slowest qualifying (brackets the spread) -
    # the same cheap 3-run probe shape `_magnitude_sample` uses.
    for job in [repr_job, qual[0][1], qual[-1][1]]:
        jid = job.get("id")
        if not jid or jid in seen_ids:
            continue
        v = step_dur(job)
        if v is None:
            continue
        seen_ids.add(jid)
        values.append({"run_url": str(job.get("html_url", "")).split("/job/")[0],
                       "value": round(v, 1), "drilled": jid == repr_job.get("id")})
    if not values or this_run is None:
        return None
    return {"label": f"the `{name}` step (wall)", "unit": "s", "kind": "step-wall",
            "this_run": round(this_run, 1), "escalated": False, "values": values}


def _magnitude_sample(
    bp: Any, client: "GhClient", repo: str, qual: list[tuple[float, dict[str, Any]]],
    repr_job: dict[str, Any], repr_log: str, k_probe: int = 3, k_max: int = 8,
    wide_rel: float = _MAG_WIDE_REL, state_fn: Any = None,
) -> dict[str, Any] | None:
    """Cross-run check on the ONE load-bearing magnitude (migration share, cache-miss
    rate, import share, …) so the single drilled run's number isn't taken on faith.

    ADAPTIVE: a cheap `k_probe`-run probe (drilled + fastest + slowest qualifying)
    brackets the spread for the cost of ~2 extra log fetches. If that bracket is
    TIGHT, stop - stability is proven cheaply. If it's WIDE (`wide_rel`), widen to
    `k_max` runs spread across the qualifying set, because that's the only case where
    more fetches change the conclusion: a smooth spread (the fix reliably helps) vs.
    two clusters (the cause is conditional). Reuses `blocking_path`'s leaf detectors
    as the single source of truth. Returns {label, unit, this_run, values, escalated}
    or None when the finding has no scalar magnitude (e.g. a sequencing issue).

    `state_fn` (cache leaves only): a `log -> {miss_pct, cold, remote_off} | None`
    reader. When set, each value entry is ANNOTATED with the run's event, head-repo,
    fork flag (head repo != audited repo — a fork PR gets no repo secrets, so a
    secrets-gated remote cache is unreachable to it),
    job duration, and parsed cache state, from the SAME already-fetched log. This is
    what `_cache_distribution` reads to ground a cache pole in a per-event, fork-aware
    hit-rate distribution instead of the single drilled run's miss. No extra fetches:
    it annotates the logs this function already downloads for the spread check."""
    primary = bp._parse_log(repr_log) or {}
    mag = primary.get("magnitude")
    if not mag or mag.get("value") is None:
        return None  # categorical-only finding (e.g. sequential playwright)
    primary_unit = mag.get("unit")

    parsed: dict[Any, float | None] = {}
    states: dict[Any, Any] = {}   # jid -> cache_state (only populated when state_fn set)
    units: dict[Any, Any] = {}    # jid -> the run's own magnitude unit (for unit consistency)

    def _record_state(jid: Any, log: str | None) -> None:
        if state_fn is not None and jid not in states:
            states[jid] = (state_fn(log) if log else None)

    def value_of(j: dict[str, Any]) -> float | None:
        jid = j.get("id")
        if jid in parsed:
            return parsed[jid]
        log = (repr_log if jid == repr_job.get("id")
               else _fetch_job_log(client, repo, j))
        leaf = bp._parse_log(log) if log else None
        v = (leaf or {}).get("magnitude")
        parsed[jid] = v["value"] if v and v.get("value") is not None else None
        units[jid] = (v or {}).get("unit")
        _record_state(jid, log)
        return parsed[jid]

    def add(jobs: list[dict[str, Any]], cap: int) -> None:
        # Fetch this batch's not-yet-parsed logs CONCURRENTLY (the dominant cost — a job
        # log can be multi-MB). The drilled run reuses `repr_log`; the rest overlap instead
        # of downloading back-to-back. The adaptive probe→escalate decision below still runs
        # sequentially between batches.
        #
        # IN CHUNKS OF `_TEXT_WINDOW`, not one flat map: `Executor.map` submits every task
        # up front and each future holds its result until it is yielded, so a flat map over
        # the batch holds every log of the batch live at once. Chunking bounds the live set
        # at `_TEXT_WINDOW` logs (parse-and-discard as we go) while each chunk still fills
        # the pool. Same fetches, same order, same results — just not all in RAM together.
        todo = [j for j in jobs
                if j.get("id") and j.get("id") not in parsed
                and j.get("id") != repr_job.get("id")]
        # Shared pool + `_fetch_job_log` (skip-no-log, allow_missing), chunked at
        # `_TEXT_WINDOW` so at most a window's worth of multi-MB logs is live at once
        # (parse-and-discard as we go) while each chunk still fills the pool.
        for chunk in (todo[i:i + _TEXT_WINDOW] for i in range(0, len(todo), _TEXT_WINDOW)):
            for jid, log in _fetch_pool().map(
                    lambda j: (j.get("id"), _fetch_job_log(client, repo, j)),
                    chunk):
                leaf = bp._parse_log(log) if log else None
                v = (leaf or {}).get("magnitude")
                parsed[jid] = v["value"] if v and v.get("value") is not None else None
                units[jid] = (v or {}).get("unit")
                _record_state(jid, log)
        # The drilled run's own state is parsed from repr_log (never re-fetched).
        _record_state(repr_job.get("id"), repr_log)
        for j in jobs:
            if len(values) >= cap:
                break
            jid = j.get("id")
            if not jid or jid in seen_ids:
                continue
            val = value_of(j)   # hits the prefetch cache (or reuses repr_log for the drill)
            if state_fn is not None:
                # CACHE leaf: a run belongs in the cross-run distribution iff its cache STATE parsed
                # (read by `state_fn` from the log regardless of whether the leaf re-fires). This one
                # rule (a) KEEPS warm runs whose miss is below the leaf's fire floor — without it a
                # turbo-partial-cache pole's distribution held only >= 40%-miss runs and could never
                # reach `mostly-warm`, defeating the grounding (F2); and (b) EXCLUDES a sibling that
                # parsed to a different, non-cache metric — its `state_fn` returns None (F4).
                st = states.get(jid)
                if not isinstance(st, dict):
                    continue
                # A magnitude in a DIFFERENT unit (install-lifecycle seconds vs turbo percent) must
                # not enter the rendered cross-run line — keep the run for its state, drop its value.
                if val is not None and units.get(jid) != primary_unit:
                    val = None
            elif val is None:
                continue
            seen_ids.add(jid)
            # `value` may be None for a warm/off-unit cache run kept only for its `cache_state`; it is
            # excluded from the rendered cross-run magnitude line (`_mag_line` filters None) but its
            # `cache_state.miss_pct` still feeds the distribution verdict.
            entry = {"run_url": str(j.get("html_url", "")).split("/job/")[0],
                     "value": val, "drilled": jid == repr_job.get("id")}
            if state_fn is not None:
                # Fork = the run's head repo differs from the audited repo. A fork PR gets no
                # repo secrets (secrets-gated remote caches are unreachable) and restores only
                # the base branch's scope, so it runs colder than an upstream PR and must be
                # EXCLUDED from the upstream median — else external-contributor
                # PRs drag a warm cache's miss rate up and manufacture a "churn" verdict.
                hr = str(j.get("_run_head_repo") or "")
                entry["event"] = str(j.get("_run_event") or "")
                entry["head_repo"] = hr
                entry["fork"] = bool(hr) and hr.lower() != repo.lower()
                entry["duration_s"] = round(_job_duration_s(j) or 0.0, 1)
                entry["cache_state"] = states.get(jid)
            values.append(entry)

    values: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    # Cheap probe: drilled + fastest + slowest qualifying (brackets the spread).
    add([repr_job, qual[0][1], qual[-1][1]], k_probe)
    escalated = False
    # The spread/escalation decision is about the MAGNITUDE, so it ignores warm cache runs kept
    # only for their `cache_state` (value None). The cache distribution reads those separately.
    nums = [v["value"] for v in values if v.get("value") is not None]
    if len(nums) >= 2:
        med = statistics.median(nums)
        rel = (max(nums) - min(nums)) / med if med else 0.0
        if rel > wide_rel and len(qual) > len(values):
            escalated = True
            # Fill in runs spread evenly across the qualifying set (by duration) so
            # the in-between region - where a second cluster would hide - is sampled.
            idx = sorted({round(i * (len(qual) - 1) / (k_max - 1))
                          for i in range(k_max)})
            add([qual[i][1] for i in idx], k_max)
    if not values:
        return None
    return {"label": mag["label"], "unit": mag.get("unit", "%"),
            "this_run": mag["value"], "escalated": escalated, "values": values}


def _job_is_fork(j: dict[str, Any], repo: str) -> bool:
    """A run whose head repository differs from the audited repo — a fork PR, which runs
    colder (no repo secrets, so a secrets-gated remote cache is unreachable). Reads
    the `_run_head_repo` stamp
    `_accumulate_jobs` puts on every job (no extra fetch)."""
    hr = str(j.get("_run_head_repo") or "")
    return bool(hr) and hr.lower() != str(repo).lower()


def _cache_verdict(pr_values: list[dict[str, Any]], tail: dict[str, Any], *,
                   floor_pct: float, tail_min_frac: float, cold_pct: float) -> str:
    """The cache-health verdict a cache pole's framing must match, re-derivable from the
    RAW stored distribution alone (so verify_report can recompute it and never trust a
    stamped enum). Pure function.

      cold        — every upstream (non-fork) sampled run is effectively 0-cached /
                    remote-caching-off: a broad "cache is cold" claim is legitimate.
      churn       — the upstream median miss rate is at/above `floor_pct`: the drilled
                    run's heavy miss is the typical case (unstable cache key).
      miss-tail   — median below the floor, but the miss-heavy tail is a material share
                    (`tail_min_frac`) of qualifying PR runs: keep the lever, framed as a
                    tail that helps cache-miss-heavy PRs, not "CI by X%".
      mostly-warm — otherwise: the cache mostly HITS across PRs; the drilled (slow-mode)
                    run is a minority. Demote the cache framing.
      insufficient— fewer than 2 upstream parsed runs: no distribution to judge.

    The miss metric is ALWAYS the per-run `cache_state.miss_pct` (never the entry's `value`): the
    install-lifecycle leaf's value is a build wall in seconds and the buildx leaf's value is the
    slowest cold layer's share, so only `cache_state.miss_pct` (computed uniformly by
    `_cache_state_of_log` for every cache leaf) is a comparable miss rate."""
    def _miss(v: dict[str, Any]) -> float | None:
        st = v.get("cache_state") if isinstance(v.get("cache_state"), dict) else {}
        m = st.get("miss_pct")
        return float(m) if isinstance(m, (int, float)) else None

    upstream = [v for v in pr_values
                if isinstance(v, dict) and not v.get("fork") and _miss(v) is not None]
    if len(upstream) < 2:
        return "insufficient"

    def _is_cold(v: dict[str, Any]) -> bool:
        st = v.get("cache_state") if isinstance(v.get("cache_state"), dict) else {}
        return bool(st.get("remote_off")) or bool(st.get("cold")) \
            or (_miss(v) or 0.0) >= cold_pct

    if all(_is_cold(v) for v in upstream):
        return "cold"
    med = statistics.median([_miss(v) for v in upstream])
    if med >= floor_pct:
        return "churn"
    prevalence = tail.get("prevalence_max") if isinstance(tail, dict) else None
    if isinstance(prevalence, (int, float)) and prevalence >= tail_min_frac:
        return "miss-tail"
    return "mostly-warm"


def _cache_distribution(
    bp: Any, client: "GhClient", repo: str, fix_key: str, mag: dict[str, Any],
    qual: list[tuple[float, dict[str, Any]]], pole: dict[str, Any],
    events_jobs_by_wf: dict[str, dict[str, list]], state_fn: Any,
) -> dict[str, Any]:
    """Assemble the per-pole `cache_dist` field: the PR-bucket miss-rate distribution
    (reusing the already-annotated `_magnitude_sample` values — no new fetches), a
    duration-proxy tail-prevalence bound over the full qualifying sample, and a capped
    push (default-branch) probe, plus the re-derivable `verdict`. Only called for a
    cache leaf, so its fetches (≤ `_CACHE_PUSH_PROBE_MAX` push logs) are the targeted
    escalation, not paid on every audit."""
    floor_pct = bp._PARTIAL_MISS_FLOOR_PCT

    def _miss(v: dict[str, Any]) -> float | None:
        st = v.get("cache_state") if isinstance(v.get("cache_state"), dict) else {}
        m = st.get("miss_pct")
        return float(m) if isinstance(m, (int, float)) else None

    pr_values = [v for v in (mag.get("values") or []) if isinstance(v, dict)]
    up_vals = [_miss(v) for v in pr_values if not v.get("fork") and _miss(v) is not None]
    fork_n = sum(1 for v in pr_values if v.get("fork"))
    # A non-fork run whose log exposed NO parseable cache summary (`_cache_state_of_log` → None):
    # it is NOT behind the median, so it is disclosed separately (`no_summary_n`) rather than
    # folded into the count — the renderer says the median is over `upstream_n` runs, not `n`.
    no_summary_n = sum(1 for v in pr_values if not v.get("fork") and _miss(v) is None)
    pr = {
        "values": pr_values,
        "n": len(pr_values),
        "fork_n": fork_n,
        "upstream_n": len(up_vals),
        "no_summary_n": no_summary_n,
        "upstream_median": round(statistics.median(up_vals), 2) if up_vals else None,
        "upstream_range": [round(min(up_vals), 2), round(max(up_vals), 2)] if up_vals else None,
    }

    # Tail prevalence — an UPPER BOUND on how often a qualifying (non-fork) PR run is
    # miss-heavy, computed for free from durations already in hand: the fraction of
    # qualifying non-fork runs at least as slow as the SLOWEST sampled miss-heavy upstream
    # run. Cache misses drive these steps' wall, so "at least as slow" over-counts the
    # miss-heavy set — conservative toward KEEPING the finding (miss-tail over mostly-warm).
    qual_nonfork = [(d, j) for d, j in qual if not _job_is_fork(j, repo)]
    missheavy_durs = [v.get("duration_s") for v in pr_values
                      if not v.get("fork") and _miss(v) is not None
                      and _miss(v) >= floor_pct
                      and isinstance(v.get("duration_s"), (int, float))]
    if missheavy_durs and qual_nonfork:
        threshold = min(missheavy_durs)
        prevalence = sum(1 for d, _ in qual_nonfork if d >= threshold) / len(qual_nonfork)
    else:
        threshold, prevalence = None, 0.0
    tail = {"threshold_dur_s": threshold,
            "prevalence_max": round(prevalence, 3),
            "qual_n": len(qual_nonfork)}

    # Push (default-branch) probe: the newest ≤ cap push runs of the pole's job, fetched
    # and parsed for their miss rate. This is the ONLY new fetch cost and only for cache
    # poles — it answers "is the cache warm on main?" (the strongest production signal).
    job_name = str(pole.get("job") or "")
    wf = str(pole.get("workflow_file") or "")
    push_runs = _as_list_local((events_jobs_by_wf.get(wf) or {}).get("push"))
    push_vals: list[dict[str, Any]] = []
    push_fetched = 0    # SUCCESSFUL fetches (a failed fetch is NOT counted — see below)
    push_matched = 0    # push runs where the pole's job was present (a fetch was attempted)
    push_errors = 0     # attempted fetches that returned no log (API error / timeout)
    # The probe's job set is decided by run METADATA already in hand (match the pole's job
    # name AND that it actually ran — `_job_has_log` — newest-first, stop at the cap): no
    # fetch is needed to pick it. So select first, prefetch the whole (≤ cap) set in one
    # wave (LEVER 3), then walk it via `_fetch_job_log` — same jobs, same order, same
    # error accounting as the serial path, one round-trip instead of `cap` of them.
    probe_jobs: list[dict[str, Any]] = []
    for run_jobs in push_runs:
        if len(probe_jobs) >= _CACHE_PUSH_PROBE_MAX:
            break
        # A job that never RAN (skipped/cancelled) carries no cache signal and has no log
        # to fetch — it is neither a probe nor an error, so it must not be counted as
        # either. `_job_has_log` keeps it out of both tallies AND out of the prefetch plan
        # (so no log is parked that the walk never consumes).
        job = next((j for j in run_jobs
                    if str(j.get("name", "")) == job_name and _job_has_log(j)), None)
        if job and job.get("id"):
            probe_jobs.append(job)
    # LEVER 3: fan the (≤ cap) probe logs out in one wave; the walk reads them from the
    # buffer via `_fetch_job_log`.
    _prefetch_text(client, [_job_log_endpoint(repo, j["id"]) for j in probe_jobs])
    for job in probe_jobs:
        push_matched += 1
        log = _fetch_job_log(client, repo, job)
        if not log:
            # A FETCH FAILURE (counted in client.errors) is NOT "the cache ran no step" — keep it
            # loud so the renderer never renders a failed probe as a benign "no summary".
            push_errors += 1
            continue
        push_fetched += 1
        st = state_fn(log)
        if st and isinstance(st.get("miss_pct"), (int, float)):
            push_vals.append({"run_url": str(job.get("html_url", "")).split("/job/")[0],
                              "value": round(float(st["miss_pct"]), 2)})
    if not push_runs:
        push, push_reason = None, "no push runs sampled for this workflow"
    elif push_matched == 0:
        # Push runs exist but none ran the pole's job (it's PR-only) — distinct from "ran but
        # exposed no cache summary" and from a fetch failure.
        push, push_reason = None, "the pole's job did not run on any sampled push run"
    else:
        push = {"values": push_vals, "n": len(push_vals), "errors": push_errors,
                "median": round(statistics.median([v["value"] for v in push_vals]), 2)
                if push_vals else None}
        push_reason = (f"{push_errors} of {push_matched} push log fetch(es) failed"
                       if push_errors and not push_vals else None)

    verdict = _cache_verdict(pr_values, tail, floor_pct=floor_pct,
                             tail_min_frac=_CACHE_TAIL_MIN_FRAC,
                             cold_pct=_CACHE_COLD_MISS_PCT)
    return {
        "fix_key": fix_key, "metric": "miss_pct", "floor_pct": floor_pct,
        "tail_min_frac": _CACHE_TAIL_MIN_FRAC,
        "pr": pr, "tail": tail, "push": push, "push_reason": push_reason,
        "verdict": verdict, "push_logs_fetched": push_fetched,
    }


def _as_list_local(v: object) -> list:
    """`[]` for any non-list — a tiny local coercion so a malformed push bucket can't
    crash the cache probe (mirrors verify_report's `_as_list` defensiveness)."""
    return v if isinstance(v, list) else []


def _as_dict_local(v: object) -> dict:
    """`{}` for any non-dict (companion to `_as_list_local`)."""
    return v if isinstance(v, dict) else {}


# A gating job that self-skips on a PR finishes in a few seconds (just runner
# setup, then the skip). No real long-pole job — the thing we drill — runs that
# fast, so anything below this is treated as a short-circuit / no-op and dropped
# from the representative-run selection. Absolute backstop to the half-median floor.
_NOOP_FLOOR_S = 30.0


def _persist_pole_logs(
    client: "GhClient", repo: str, poles: list[dict[str, Any]],
    jobs_per_run_by_wf: dict[str, list], data_dir: "Path", mag_runs: int = 3,
    events_jobs_by_wf: dict[str, dict[str, list]] | None = None,
) -> list[dict[str, Any]]:
    """Capture the long-pole jobs' raw logs ONCE into a local data bundle, so the
    report (and the fix-agent it hands off to) can read the step's INTERNAL timing
    - per-test-file / per-suite / repeated-setup cost - without re-downloading from
    gh.

    Which run's log? The instance whose duration is **closest to the typical (P50)
    time** - so the drill's job total matches the level-1 headline instead of
    drifting high. Selection is over QUALIFYING instances only (a gated job like
    `changed-tests` short-circuits on PRs that don't trigger it; those near-zero
    instances are dropped so they can't be picked). One drill fetch per pole, deduped
    by job id.

    The bundle also records, per pole: a cross-run duration `sample` (every
    qualifying instance's url + duration - free, reuses the sampled instances), the
    drilled run's per-step `steps_file` timeline, and a `mag_file` cross-run check on
    the load-bearing magnitude (median + range across a few runs). The magnitude check
    is a cheap `mag_runs`-run probe (~`mag_runs`-1 extra log fetches, reusing the
    drilled log); only when that probe's bracket is WIDE does `_magnitude_sample`
    escalate to its `k_max` (up to ~5 further fetches) - see that function."""
    import blocking_path as bp  # same-skill module; leaf detectors live there
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []

    def _f(x: Any) -> float | None:
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def _select_drills() -> list[tuple[dict[str, Any], str, str,
                                       list[tuple[float, dict[str, Any]]],
                                       float, dict[str, Any], Any]]:
        """Choose the representative run to drill per pole — a PURE pass over the
        already-sampled job data (no gh call). Split out from the capture walk below so
        the walk's logs can be fetched in ONE wave (LEVER 3): the drill logs are the
        heaviest responses in the bundle and were downloaded one after another, even
        though which log to fetch never depends on any other pole's log."""
        out = []
        seen: set[Any] = set()
        logged_matrix: list[tuple[str, str]] = []  # one (check, wf) rep per matrix
        for p in poles:
            wf = p.get("workflow_file")
            job = p.get("job")
            if not wf or not job:
                continue
            # Only the drilled poles need a log: one rep per distinct matrix (sibling legs
            # share a fix and aren't drilled separately), capped at what the report shows.
            # Pass the workflow file so two SAME-NAMED jobs in DIFFERENT workflows (e.g. a
            # `Python 3.13` leg in both datasets-test.yml and framework-test.yml) aren't
            # collapsed into one matrix and one log — each is a distinct job to drill.
            check = str(p.get("check", ""))
            if any(bp._same_matrix(check, c, str(wf), cwf) for c, cwf in logged_matrix):
                continue
            if len(logged_matrix) >= _DRILL_DISTINCT_MATRICES:
                break
            logged_matrix.append((check, str(wf)))
            durs = sorted(
                ((_job_duration_s(j) or 0.0, j) for run in jobs_per_run_by_wf.get(wf, [])
                 for j in run if str(j.get("name", "")) == job and _job_duration_s(j)),
                key=lambda dj: dj[0])
            if not durs:
                continue
            # Drop short-circuit / no-op instances before choosing the representative.
            # Floor at HALF THE MEDIAN duration, NOT half the slowest: a lone slow
            # OUTLIER must not raise the floor above the typical cluster and force itself
            # to be picked as "representative" (the drill must reconcile with the P50
            # headline, so the representative is the median run, never the outlier). An
            # absolute backstop (`_NOOP_FLOOR_S`) covers the degenerate case where no-ops
            # are the MAJORITY (a gated job that self-skips on most PRs): there the median
            # is itself a no-op, so the relative floor alone wouldn't exclude them. `or
            # durs` keeps everything when every run is short (a genuinely fast job).
            med = statistics.median([d for d, _ in durs])
            floor = max(0.5 * med, _NOOP_FLOOR_S)
            qual = [dj for dj in durs if dj[0] >= floor] or durs
            # B: pick the qualifying run CLOSEST to the typical (P50) time, so the drill
            # reconciles with the level-1 headline. Falls back to the qualifying median
            # when no P50 is on the pole.
            #
            # For a BIMODAL pole, drill a SLOW-mode run (target = the slow cluster median),
            # so the drill shows WHY it's a long gate on its slow PRs - the median run is
            # the fast mode and would hide the root cause. Otherwise target the P50.
            slow = _f((p.get("bimodal") or {}).get("high_p50_s"))
            target = (slow or _f(p.get("job_p50_s")) or _f(p.get("p50_s"))
                      or qual[len(qual) // 2][0])
            repr_dur, repr_job = min(qual, key=lambda dj: abs(dj[0] - target))
            jid = repr_job.get("id")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            out.append((p, str(wf), str(job), qual, repr_dur, repr_job, jid))
        return out

    drills = _select_drills()
    # LEVER 3: prefetch the drilled logs in one wave. Filter by `_job_has_log` to match
    # `_fetch_job_log`'s fetch decision at the walk below — a repr job with no log to serve
    # is not fetched there, so parking one here would be an unconsumed prefetch (drift) and
    # a gh call the serial path never made.
    _prefetch_text(client, [_job_log_endpoint(repo, jid)
                            for *_r, repr_job, jid in drills if _job_has_log(repr_job)])

    for p, wf, job, qual, repr_dur, repr_job, jid in drills:
        log = _fetch_job_log(client, repo, repr_job)
        if not log:
            logger.warning("no log captured for pole job %r (id=%s) - fetch returned "
                           "empty; this pole will render without a drill-down", job, jid)
            continue
        safe = _re.sub(r"[^A-Za-z0-9._-]+", "-", str(job))[:60].strip("-") or "job"
        fn = f"{safe}-{jid}.log"
        try:
            (data_dir / fn).write_text(log, encoding="utf-8")
        except OSError as e:
            logger.warning("could not save pole log %s: %s", fn, e)
            continue
        # Capture this SAME run's per-step timeline (execution order + start/end
        # offsets) so the report can draw the step level as a succession timeline -
        # steps run one after another, unlike the concurrent level-1 jobs. No extra
        # gh call: the jobs listing already carries each step's started_at/
        # completed_at. Tying it to the same repr_job keeps level-2 (timeline) and
        # the level-3 log drill on the SAME run.
        steps_fn: str | None = None
        timeline = _step_timeline(repr_job, job, round(repr_dur, 1))
        if timeline["steps"]:
            steps_fn = f"{safe}-{jid}.steps.json"
            try:
                (data_dir / steps_fn).write_text(
                    json.dumps(timeline, indent=2), encoding="utf-8")
            except OSError as e:
                logger.warning("could not save pole step timeline %s: %s", steps_fn, e)
                steps_fn = None
        # C: cross-run check on the load-bearing magnitude (a few extra log fetches).
        # When the log matches no catalog detector (so there's no scalar cause to
        # track), fall back to validating the DOMINANT STEP's wall time across runs -
        # so an undetected pole still gets a cross-run check + a finding shaped like a
        # matched pole's, not just a bare timeline.
        # A CACHE leaf (turbo / buildx / install-lifecycle) additionally gets a
        # `cache_dist` grounding stamped on the pole: the cross-run miss-rate split PR
        # vs push and fork vs upstream, so the renderer can't frame a mostly-warm cache
        # as a top miss/churn lever off the single (slow-mode) drilled run. `state_fn`
        # annotates the SAME logs `_magnitude_sample` already fetches (no PR-bucket cost).
        leaf0 = bp._parse_log(log) or {}
        _cache_fk = leaf0.get("fix_key") if leaf0.get("fix_key") in _CACHE_LEAF_KEYS else None
        _state_fn = (lambda lg, _fk=_cache_fk: bp._cache_state_of_log(lg, _fk)) if _cache_fk else None
        mag_fn: str | None = None
        mag = _magnitude_sample(bp, client, repo, qual, repr_job, log, mag_runs,
                                state_fn=_state_fn)
        if _cache_fk and mag:
            p["cache_dist"] = _cache_distribution(
                bp, client, repo, _cache_fk, mag, qual, p,
                events_jobs_by_wf or {}, _state_fn)
        if not mag:
            mag = _dominant_step_sample(timeline, qual, repr_job)
        if mag:
            mag_fn = f"{safe}-{jid}.mag.json"
            try:
                (data_dir / mag_fn).write_text(
                    json.dumps(mag, indent=2), encoding="utf-8")
            except OSError as e:
                logger.warning("could not save pole magnitude sample %s: %s", mag_fn, e)
                mag_fn = None
        manifest.append({
            "job": job, "check": p.get("check"), "job_id": jid, "file": fn,
            "steps_file": steps_fn, "mag_file": mag_fn,
            "html_url": repr_job.get("html_url", ""), "workflow_file": wf,
            "duration_s": round(repr_dur, 1), "selected": "nearest-p50",
            "sample": [{"html_url": j.get("html_url", ""), "duration_s": round(d, 1)}
                       for d, j in qual],
        })
        logger.debug("saved pole log for %s -> %s (closest to P50 of %d qualifying "
                     "runs)", job, fn, len(qual))
    return manifest


# =============================================================================
# Orchestration
# =============================================================================

def collect(findings_doc: dict[str, Any], repo: str | None,
            max_runs: int, with_logs: bool = False,
            created_before: str | None = None,
            data_dir: "Path | None" = None,
            shallow_runs: int = _SHALLOW_RUNS,
            root: "Path | None" = None) -> dict[str, Any]:
    # One unpinned 30-day window per RUN (not per interpreter): a harness that collects
    # several repos in one process must not measure the later ones against the first
    # call's clock. See `_reset_unpinned_now`.
    _reset_unpinned_now()
    # Guard the shallow depth: `--shallow-runs 0` (or negative) would make the shallow
    # pass fetch nothing, leave every workflow at p50=0, deepen nothing, and silently
    # emit a report with no timing — a coverage-gap masquerade. Clamp to >=1 (the
    # check-level convergence still deepens the top checks to full depth, so the
    # headline is unaffected; a tiny shallow sample just costs a few extra deepens).
    shallow_runs = max(1, shallow_runs)
    # `--repo` is optional: when the caller didn't supply one (e.g. a static-
    # only audit on a local checkout with no GitHub coordinates), skip the
    # gh pass entirely and render qualitatively.
    #
    # EVERY exit from collect() stamps the four disclosure keys — `partial_reason`,
    # `partial_kind`, `run_list_fetch_failures`, `job_fetch_failures` — even when they
    # are empty. `verify_report.check_run_list_gaps_named` fails CLOSED on their
    # absence once the gh tier ran, and an early return that omitted them made the
    # invariant SKIP: a guard that skips is a guard that isn't there.
    if not repo:
        findings_doc["data_sources"] = {
            "tiers_run": [], "gh_available": False,
            "partial_reason": "no --repo supplied; static-only run",
            "partial_kind": _PARTIAL_STATIC_ONLY,
            "run_list_fetch_failures": [], "job_fetch_failures": [],
        }
        return findings_doc
    client = GhClient()
    gh_ok = client.available()
    if not gh_ok:
        # `available()` fails for two OPPOSITE reasons that must not be conflated. If gh
        # is genuinely absent (no binary / no token), a static-only report is the honest
        # fallback. But if gh is installed AND a token is configured, the auth probe
        # still failed — the GitHub API did NOT accept the credential: a rate-limit /
        # secondary-limit block, a transport error, OR an invalid token (expired /
        # revoked / too narrowly scoped). We can't tell which offline, but they are all a
        # collection failure, not a missing tool. Labeling THAT `gh_unavailable` (a
        # NOT-MEASURED kind) is the silent-drop bug: the report renders static-only and
        # reads as a complete audit of a quiet repo while the token was actually refused.
        # Route it to the LOUD `collection_failed` kind — the same severity the
        # mid-collection abort (`gave_up`) already uses — so the coverage banner and
        # `_measurement_is_broken` announce the hole instead of hiding it.
        if client.diagnose_unavailability() == "api_blocked":
            findings_doc["data_sources"] = {
                "tiers_run": [], "gh_available": True,
                "gh_query_count": client.queries, "gh_error_count": client.errors,
                "partial_reason": (
                    "gh is installed and a token is configured, but the GitHub API "
                    "REFUSED the collection before any run could be sampled (a rate-limit "
                    "/ secondary-limit block, a transport error, or an expired / revoked "
                    "/ insufficiently-scoped token) — so NO CI data was measured. This "
                    "report is static-scan-only and is NOT a trustworthy CI audit; re-run "
                    "once the block clears — wait out a rate limit, or re-authenticate "
                    "gh (`gh auth status`) if the token is invalid."),
                "partial_kind": _PARTIAL_COLLECTION_FAILED,
                "run_list_fetch_failures": [], "job_fetch_failures": [],
            }
            return findings_doc
        findings_doc["data_sources"] = {
            "tiers_run": [], "gh_available": False,
            "partial_reason": "gh CLI not available or not authenticated",
            "partial_kind": _PARTIAL_GH_UNAVAILABLE,
            "run_list_fetch_failures": [], "job_fetch_failures": [],
        }
        return findings_doc

    # The local checkout is only trusted as a workflow-YAML source once it is VERIFIED
    # to be a clone of `--repo`. A mismatched `--root` would feed one repo's YAML into
    # the other repo's timings (`_root_is_clone_of`); an unverifiable one falls back to
    # the API, which is exactly the pre-`--root` behavior.
    if root is not None and not _root_is_clone_of(root, repo):
        root = None
    workflow_yaml_source: dict[str, int] = {}
    # The dirty check belongs on THIS side of the root decision, not in run.py: a `--root`
    # we just REJECTED contributes no YAML, so its uncommitted edits are not a skew in this
    # report and must not be disclosed as one. Only a root we are actually going to read
    # from can make the audited commit a lie.
    if root is not None and _workflows_are_dirty(root):
        findings_doc["workflows_tree_dirty"] = True
        logger.warning(
            "%s/.github/workflows has UNCOMMITTED changes. The detectors parse the "
            "WORKING TREE, while every timing comes from runs of the COMMITTED branch — "
            "so a finding you have already fixed locally can read as clean against "
            "timings that still contain it. The report marks the audited commit `-dirty`.",
            root)

    # Look up workflow ids by path. A FAILED fetch here (rate-limit exhaustion, a
    # 5xx) is not "this repo has no workflows" — it is the audit having no idea what
    # the repo runs. Rendering an empty-but-confident audit off that is the silent-drop
    # bug at its worst, so abort with a disclosed gap instead.
    wfs = _list_workflows(client, repo)
    if wfs is None:
        logger.warning("the workflow-list fetch FAILED for %s — aborting the gh pass "
                       "rather than auditing the repo as though it had no workflows", repo)
        findings_doc["data_sources"] = {
            "tiers_run": [], "gh_available": True,
            "gh_query_count": client.queries,
            "gh_error_count": client.errors,
            "partial_reason": (
                "the workflow-list fetch failed (gh API error), so NO workflow could be "
                "measured — this is a collection failure, not a repo with no workflows"),
            # The severity marker the renderer branches on: a WHOLE-REPO gap, never to
            # be dressed up as "a few runs are absent from the sample".
            "partial_kind": _PARTIAL_COLLECTION_FAILED,
            "run_list_fetch_failures": [], "job_fetch_failures": [],
        }
        return findings_doc
    wf_by_path = {w.get("path"): w for w in wfs if w.get("path")}
    workflow_paths = set(wf_by_path)
    findings = findings_doc.get("findings") or []
    seeded_workflows = _finding_seed_workflow_paths(findings, workflow_paths)
    opt57_seed_workflows = _opt57_seed_workflow_paths(
        findings_doc.get("workflow_job_graph"), workflow_paths)
    workflows_in_play = sorted(seeded_workflows | opt57_seed_workflows)

    crit_by_wf: dict[str, dict[str, Any]] = {}
    vol_by_wf: dict[str, int | None] = {}
    activity_by_wf: dict[str, dict[str, Any]] = {}
    jobs_per_run_by_wf: dict[str, list[list[dict[str, Any]]]] = {}
    prior_attempt_jobs_by_wf: dict[str, dict[str, Any]] = {}
    # Retry-tax substrate (per workflow, PR-event scoped): counts from the
    # all-status slice + prior-attempt job minutes where fetched. Aggregated
    # over the gating workflows into data_sources.attempt_stats at the end.
    attempt_stats_by_wf: dict[str, dict[str, Any]] = {}
    sampled_runs_by_wf: dict[str, list[dict[str, Any]]] = {}
    # Config-era facts (issue #66): one entry per workflow whose sample STRADDLES its
    # last-change commit — the boundary, the kept era, the rule applied, and the
    # pre/post counts. Stamped onto `pr_critical_path.config_eras` so verify_report
    # can re-derive that no drilled pole blends eras. Empty when no workflow straddled
    # (the byte-identical no-op case).
    config_era_facts: list[dict[str, Any]] = []
    # Per-workflow event buckets (`jobs_by_event`), retained so a cache pole's push
    # (default-branch) runs can be probed for their hit rate — `jobs_per_run_by_wf`
    # keeps only the developer-event bucket, dropping the push runs the cache-distribution
    # check needs. Stored by REFERENCE, so the deepen pass (which mutates the bucket dict
    # in place) extends it for free; the push jobs are already fetched, only their logs
    # are new (capped by `_CACHE_PUSH_PROBE_MAX`, and only when a cache leaf fires).
    events_jobs_by_wf: dict[str, dict[str, list[list[dict[str, Any]]]]] = {}
    # The trigger events that actually fired each workflow in the sampled
    # window (run.event — "pull_request", "push", "schedule", …). Two workflows
    # whose event sets intersect run CONCURRENTLY on that event, so the
    # developer's wall-clock wait is gated by the slowest of them — used to cap
    # a per-workflow wall-clock saving at the cross-workflow floor.
    events_by_wf: dict[str, set[str]] = {}
    # Candidate PR head-commits with their most-recent run timestamp, across all
    # workflows — the pool the measured-critical-path sample is drawn from (the
    # 20 most recent that ran the full required-check suite).
    pr_sha_ts: dict[str, str] = {}
    # #58: per-head-sha identity metadata (event + derived PR number) so merge-queue
    # runs can be folded onto their PR before the presence denominator is formed. Fed
    # to `_group_dev_shas_by_pr` below; empty of merge_group runs => no-op (each sha
    # stays its own population row, unchanged from before #58).
    sha_meta: dict[str, dict[str, Any]] = {}
    runs_sampled = 0
    jobs_sampled = 0
    # Per-run jobs fetches that FAILED (gh error/timeout) rather than returned an
    # empty job set — a coverage gap surfaced in the report's coverage note, not
    # laundered into a quietly smaller P50 sample (mirrors the check-run sampler's
    # `fetch_failures`).
    jobs_fetch_failures = 0
    # RUN-LIST fetches that FAILED (gh error / rate-limit exhaustion), by workflow and
    # which list. A failed run-list is NOT "this workflow has no runs" — the workflow
    # simply drops out of whatever it feeds, so it must be disclosed BY NAME: the
    # error COUNT alone ("4 gh calls failed") never tells the reader that the merge-gate
    # workflow is missing from the sample entirely. Rendered via `partial_reason`.
    run_list_fetch_failures: list[dict[str, str]] = []
    # JOB-fetch WIPEOUTS, by workflow: every one of its sampled runs' job fetches failed,
    # so it has runs but NO job timing. The second way a workflow silently leaves the
    # measured sample, and until now the unnamed one — `jobs_fetch_failures` (a bare int)
    # only ever produced the MINOR "a few runs/jobs are absent … marginally fewer runs"
    # note. It is not minor: with an empty `crit`, `_map_check_to_job(…,
    # require_developer_timing=True)` finds nothing, and the workflow's checks fall back
    # to CHECK-RUN SPAN timing — which is queue-INFLATED (an 80s `Integration test` job
    # whose check-run reads 1871s). That inflated number can outrank the true gate and
    # HEADLINE the report. So a wipeout is named like a run-list gap (same severe kind,
    # same coverage note, same verify invariant) AND its queue-inflated fallback is
    # barred from the spine — see `_bar_queue_inflated_fallback` below.
    job_fetch_failures: list[dict[str, str]] = []

    def _note_run_list_gap(wf_path: str, which: str) -> None:
        logger.warning(
            "run-list fetch FAILED for %s (%s) — the workflow is MISSING from that "
            "sample, not empty; disclosing it as a coverage gap", wf_path, which)
        run_list_fetch_failures.append({"workflow_file": wf_path, "fetch": which})

    def _note_job_fetch_wipeout(wf_path: str, n_runs: int) -> None:
        logger.warning(
            "every per-run JOB fetch FAILED for %s (%d run(s)) — the workflow has NO job "
            "timing, so it is MISSING from the measured sample, not fast; disclosing it "
            "as a coverage gap and barring its queue-inflated check-run fallback",
            wf_path, n_runs)
        job_fetch_failures.append(
            {"workflow_file": wf_path, "fetch": "per-run job sample"})

    def _sample_gaps() -> list[dict[str, str]]:
        """Every way a workflow vanished from the MEASURED sample, as one list — the
        input to `_partial_reason` / `_partial_kind`, so both kinds of wipeout get the
        same severity, the same by-name disclosure, and the same verify invariant."""
        return run_list_fetch_failures + job_fetch_failures

    def _abort_gh_pass() -> dict[str, Any]:
        """The GLOBAL breaker tripped mid-sample: GitHub is sustainedly failing us
        (rate limits, 5xx, or timeouts), so the remaining calls cannot be waited out
        inside a useful wall-clock. Abort down the SAME disclosed-coverage-gap path the
        workflow-list failure uses — a loud, fast, honest "we didn't measure this repo"
        — rather than grinding through the rest of the run at ~0 data and then rendering
        a confident critical path off the handful of workflows that got in first.
        `scan.py`'s static findings survive on `findings_doc`; only the gh tier is
        withheld, and `tiers_run: []` says so."""
        logger.warning(
            "gh pass ABORTED for %s after the circuit breaker tripped — %d call(s) "
            "made, %d failed. Disclosing a whole-repo coverage gap instead of "
            "rendering a partial audit as though it were complete.",
            repo, client.queries, client.errors)
        findings_doc["data_sources"] = {
            "tiers_run": [], "gh_available": True,
            "gh_query_count": client.queries,
            "gh_error_count": client.errors,
            "run_list_fetch_failures": run_list_fetch_failures,
            "job_fetch_failures": job_fetch_failures,
            "partial_reason": _partial_reason(
                client.errors, _sample_gaps(), gave_up=True),
            "partial_kind": _PARTIAL_COLLECTION_FAILED,
        }
        return findings_doc
    # Fetch failures in job samples that feed `runner_minute_spine` rows. Keep this
    # narrower than `jobs_fetch_failures`, which also includes detector-only probes.
    cost_spine_fetch_failures_by_wf: dict[str, int] = {}
    # De-duplicates `GET .../runs/{id}/jobs` across the data pass: the same run's jobs
    # are reachable from the main sample AND from the event-scoped detector probes
    # (OPT36 schedule, OPT35/OPT57 failures). Shared by every `_gather_run_jobs` call
    # below — it changes no sampled value, only the call count.
    job_memo = _JobFetchMemo()
    # The all-status run page per workflow (`_COST_RUNLIST_MAX` runs, all conclusions),
    # fetched ONCE and reused by the run-elimination detectors (OPT46/OPT47/OPT64) —
    # which used to re-fetch the very same page. It also derives the success-run sample
    # (`_success_runs_from_all_status`), so the success-only query is only issued as a
    # fallback when the page is TRUNCATED and still can't reach `max_runs` successes.
    #
    # ONLY SUCCESSFUL fetches are cached, and `[]` (a workflow that genuinely has no
    # runs) is a successful fetch. A FAILED fetch is a None that is never stored, so a
    # later caller re-tries instead of inheriting it — the same rule `_JobFetchMemo`
    # states. Caching a failure as `[]` here would serve "0 of 0 runs" to every
    # run-elimination detector and render them all CLEAN off one transient timeout.
    all_status_runs_by_wf: dict[str, list[dict[str, Any]]] = {}
    # Workflows whose all-status page has ALREADY failed once. The retry from the
    # detector loop is deliberate (a transient timeout must not permanently disable the
    # run-elimination family) — but ONE unavailable resource must not be BILLED twice.
    # `client.errors` drives the report's coverage banner, and charging a persistently
    # unreachable page 2 (or n) errors inflates it against "how many distinct things
    # were unavailable", which is the question the banner's reader is actually asking.
    all_status_failed: set[str] = set()
    # Detectors that were NOT EVALUATED because their basis page was unfetchable, keyed
    # by workflow. This is the whole point of the None-vs-`[]` discipline: skipping the
    # detector keeps us from reporting it CLEAN off a laundered empty page, but a
    # finding that never appears reads as "no problem found" just the same. The skip is
    # only honest if it is DISCLOSED — so it is stamped into `data_sources` and NAMED in
    # the rendered report, not left to a stderr warning nobody sees and a generic
    # "some calls failed" footnote that describes a different failure entirely.
    detectors_skipped: dict[str, dict[str, Any]] = {}

    def _skip_detectors(wf_path: str, patterns: list[str], reason: str) -> None:
        entry = detectors_skipped.setdefault(
            wf_path, {"workflow": wf_path, "detectors": [], "reason": reason})
        for pat in patterns:
            if pat not in entry["detectors"]:
                entry["detectors"].append(pat)
        if reason not in str(entry["reason"]):
            entry["reason"] = f"{entry['reason']}; {reason}"

    def _all_status_page(wf_path: str, wf_id: Any) -> list[dict[str, Any]] | None:
        """Fetch-once accessor for a workflow's all-status run page. None = the fetch
        FAILED (not cached, not `[]`)."""
        if wf_path in all_status_runs_by_wf:
            return all_status_runs_by_wf[wf_path]
        before = client.errors
        page = _all_status_runs(client, repo, wf_id, _COST_RUNLIST_MAX, created_before)
        if page is None:
            if wf_path in all_status_failed:
                # A RE-try of an already-failed page. The call was worth making; the
                # second charge was not — roll it back so the banner counts unavailable
                # RESOURCES, not attempts at them.
                client.errors = before
            all_status_failed.add(wf_path)
            logger.debug("all-status run page for %s could not be fetched — the "
                         "run-elimination detectors will be told UNKNOWN, not empty",
                         wf_path)
            return None
        all_status_runs_by_wf[wf_path] = page
        return page

    def _add_cost_spine_fetch_failures(wf_path: str, count: int) -> None:
        if count <= 0:
            return
        cost_spine_fetch_failures_by_wf[wf_path] = (
            cost_spine_fetch_failures_by_wf.get(wf_path, 0) + count)
    # Adaptive 2-pass: the shallow loop stashes each workflow's not-yet-fetched
    # deeper runs (+ its job accumulators) so the deepen pass can extend them in
    # place. `wf_path -> (remaining_run_metas, jobs_per_run, jobs_by_event)`.
    deepen_stash: dict[str, tuple[list[dict[str, Any]],
                                  list[list[dict[str, Any]]],
                                  dict[str, list[list[dict[str, Any]]]]]] = {}
    # Workflows whose per-run job fetch was skipped by run-list triage (slowest sampled run
    # under `_TRIAGE_WALLCLOCK_FLOOR_S` — can't hold the pole). Disclosed in data_sources.
    triaged_fast_workflows: list[str] = []
    # Triaged workflows' sampled run metadata, retained so the post-shallow relative-recovery
    # pass (see `_TRIAGE_RECOVER_GATE_FRAC`) can job-fetch a workflow whose wall turns out to
    # reach the measured gate — i.e. one the absolute 90s floor wrongly skipped. `wf_path ->
    # runs`. No extra gh cost: these run-lists are already fetched for every workflow.
    triage_recover_stash: dict[str, list[dict[str, Any]]] = {}

    # === RUN-LIST PASS (fan-out, then walk) ==================================
    # LEVER 1. The run-list family — one volume probe + one all-status run page per
    # workflow — used to be issued INSIDE the per-workflow loop, one workflow at a
    # time. They are the most expensive individual calls in the pass (a `per_page=100`
    # run list measures ~2.5s) and they are pure fan-out: every call is keyed by
    # `wf_id` and writes into a per-workflow dict, with zero cross-workflow ordering.
    # So hoist the FETCHES out of the loop into one shared-pool wave and keep the
    # per-workflow BOOKKEEPING serial below — same calls, same results, same order,
    # ~7× less wall.
    #
    # PREFETCH WHAT THE LOOP ACTUALLY FETCHES (post-merge, #213): the loop reads the
    # ALL-STATUS page (`_all_status_page` → `_all_status_runs` at `_COST_RUNLIST_MAX`)
    # and DERIVES the success sample from it (`_success_runs_from_all_status`), issuing
    # a separate `status=success` query only as a FALLBACK (an all-status fetch that
    # failed, or a derived sample too short). So the wave prefetches the volume probe +
    # the all-status page — byte-identical endpoint strings to what the loop requests.
    # Prefetching `status=success` here instead would park a page no derive-path consumes
    # (prefetch drift) while the all-status page it DOES read went unfetched.
    _prefetch_json(client,
        [e
         for wf_path in workflows_in_play
         for wf in [wf_by_path.get(wf_path)] if wf and wf.get("id") is not None
         for e in (_volume_endpoint(repo, wf["id"], created_before),
                   _run_list_endpoint(repo, wf["id"], _COST_RUNLIST_MAX, created_before))])

    # Workflows whose shallow job sample we still owe: (wf_path, runs, shallow_n).
    # Filled by the walk below, drained by the ONE global job pool after it.
    shallow_plan: list[tuple[str, list[dict[str, Any]], int]] = []

    for wf_path in workflows_in_play:
        # The global circuit breaker is terminal (rate limit, 5xx, or timeout): every
        # remaining call would short-circuit to a coverage gap anyway, so stop and say so.
        if getattr(client, "gave_up", False):
            return _abort_gh_pass()
        # Normalize the relative form returned by scan.py to the repo-rooted
        # path the gh API uses (".github/workflows/foo.yml").
        gh_path = wf_path
        wf = wf_by_path.get(gh_path)
        if not wf:
            crit_by_wf[wf_path] = {"long_pole_job": "", "long_pole_p50": 0.0,
                                   "floor_p50": 0.0, "job_p50": {}}
            vol_by_wf[wf_path] = None
            # Canonical "couldn't check" marker: `status == "unavailable"` means
            # activity could not be determined (distinct from "0 active runs").
            # A repo-file finding (e.g. turbo.json/OPT60) lands here because its
            # "workflow_file" isn't a real workflow.
            #
            # HONEST SCOPE: this is a machine-readable stamp on findings.json (the
            # artifact the fix-agent reads), NOT a rendering switch. No ci-speedup
            # renderer consumes `workflow_activity` today — an earlier comment here
            # claimed it flipped the report to "activity unavailable", which was never
            # true in this skill and made the marker look like the disclosure when it
            # wasn't one. The HUMAN-facing disclosure of an unmeasurable workflow is
            # `data_sources.run_list_fetch_failures` → `partial_reason` →
            # blocking_path's `_coverage_note` (which NAMES the workflow) → verified by
            # `verify_report.check_run_list_gaps_named`. Don't add a disclosure here
            # without a renderer that reads it.
            activity_by_wf[wf_path] = {"status": "unavailable"}
            jobs_per_run_by_wf[wf_path] = []
            continue
        wf_id = wf.get("id")
        runs_30d = _monthly_volume(client, repo, wf_id, created_before)
        vol_by_wf[wf_path] = runs_30d
        # ONE run-list page per workflow. The all-status page is a superset of the
        # success-only sample (same list, newest-first, minus the server-side filter),
        # so derive the sample from it instead of paying a second query — and hand the
        # same page to the run-elimination detectors, which used to re-fetch it.
        #
        # The explicit success query still fires, but ONLY when the derived sample is
        # short AND the page is FULL (`>= _COST_RUNLIST_MAX`, i.e. truncated). Those two
        # conditions together are what "the page doesn't reach far enough back" means. A
        # SHORT page is the workflow's whole visible history, so its successes are all
        # the successes there are and the fallback would re-fetch the identical runs.
        # Getting this condition wrong is not just a wasted call: a fallback on every
        # short-history workflow makes a monorepo full of small/rarely-run workflows pay
        # TWO run-list calls where it used to pay one.
        all_runs = _all_status_page(wf_path, wf_id)
        if all_runs is None:
            # UNKNOWN, not empty (see `_all_status_runs`). Fall back to the success
            # query for the timing sample; the detectors that need the all-status page
            # will re-try it (and skip themselves if it fails again) rather than
            # reading a laundered `[]`.
            runs = _sample_runs(client, repo, wf_id, max_runs, created_before)
            if runs is None:
                # BOTH run-list fetches FAILED. `[]` here would read as "this workflow
                # never ran" and quietly delete it from the audit; instead mark it
                # unavailable (the same canonical "couldn't check" marker the
                # no-such-workflow branch uses) and disclose the gap by name.
                _note_run_list_gap(wf_path, "success run sample")
                crit_by_wf[wf_path] = {"long_pole_job": "", "long_pole_p50": 0.0,
                                       "floor_p50": 0.0, "job_p50": {}}
                activity_by_wf[wf_path] = {"status": "unavailable"}
                jobs_per_run_by_wf[wf_path] = []
                continue
        else:
            runs = _success_runs_from_all_status(all_runs, max_runs)
            if len(runs) < max_runs and len(all_runs) >= _COST_RUNLIST_MAX:
                logger.debug("run-list: %s yields only %d/%d successes in its FULL "
                             "%d-run all-status page — the page cannot reach far "
                             "enough back; falling back to the success query",
                             wf_path, len(runs), max_runs, len(all_runs))
                # `or runs`: a failed success query (None) must not DESTROY a perfectly
                # good derived sample — the workflow would go dormant, drop out of the
                # p50 and contribute nothing, all because the SECOND query timed out. A
                # short real sample beats no sample.
                runs = _sample_runs(client, repo, wf_id, max_runs, created_before) or runs
        sampled_runs_by_wf[wf_path] = runs
        # Config-era partition (issue #66). `sampled_runs_by_wf` (above) keeps the FULL
        # sample — the runner-minute / relative-recovery consumers below read it and are
        # about total compute, not the PR spine. But the SPINE + DRILL contributions use
        # `runs_for_spine`, which drops the retired-config era when this workflow's sample
        # straddles its last-change commit, so a drilled pole never blends two step layouts.
        # The boundary lookup is ONE `commits?path=` call, gated to samples that CAN straddle
        # (>= 2 runs); no-op (byte-identical) for every workflow that did not change.
        runs_for_spine = runs
        _era_fact = None
        if len(runs) >= 2:
            _era_boundary, _era_prev, _era_last_sha, _era_prev_sha = _workflow_change_boundary(
                client, repo, wf_path, created_before)
            # CONTENT-keyed era classification (issue #77). Fetch the workflow-file blobs and classify
            # each sampled run's head_sha by CONTENT only when the sample TIMESTAMP-straddles the
            # boundary — the cheap zero-fetch gate (`_timestamp_straddles`). A workflow that did not
            # change (no boundary) or whose whole sample sits one side never fetches a blob: the hard
            # byte-identity requirement for non-straddling repos. Worst-case extra calls on a
            # straddling workflow: ≤2 boundary blobs + ≤1 per unique sampled head_sha.
            _content_era: dict[str, str] | None = None
            _era_basis: dict[str, Any] | None = None
            if _era_boundary and _timestamp_straddles(runs, _era_boundary):
                _content_era, _era_basis = _resolve_content_eras(
                    client, repo, wf_path, runs, _era_last_sha, _era_prev_sha)
            runs_for_spine, _era_fact = _partition_config_era(
                runs, _era_boundary, _era_prev, _content_era)
            if _era_fact is not None:
                _era_fact["workflow_file"] = wf_path
                if _content_era is not None:
                    # Consumed by `_era_pr_side` (thin-flip / enumeration / redrill) and the pole
                    # stamps; `classification` is the offline-guard bookkeeping (content vs timestamp
                    # sampled-run counts, boundary-blob resolution).
                    _era_fact["content_era_by_sha"] = _content_era
                    _era_fact["classification"] = _era_basis
                config_era_facts.append(_era_fact)
                logger.debug("config-era: %s straddles %s — rule=%s (pre=%d/post=%d), "
                             "spine/drill uses the %s era; classification=%s",
                             wf_path, _era_fact["boundary"], _era_fact["rule"],
                             _era_fact["pre_count"], _era_fact["post_count"],
                             _era_fact["kept_era"], _era_basis)
        events_by_wf[wf_path] = {
            str(r.get("event")) for r in runs if r.get("event")
        }
        # Most-recent-run date: the first sampled run's `updated_at`, since
        # the gh API returns runs newest-first. A workflow with no recent
        # successful runs gets last_run="" and dormant=True.
        last_run = ""
        for r in runs:
            ts = r.get("updated_at") or r.get("created_at") or ""
            if isinstance(ts, str) and ts > last_run:
                last_run = ts
        dormant = (runs_30d == 0) if isinstance(runs_30d, int) else False
        activity_by_wf[wf_path] = {
            "runs_30d": runs_30d if isinstance(runs_30d, int) else None,
            "last_run": last_run,
            "dormant": dormant,
        }
        shallow_n = min(shallow_runs, len(runs))
        # Run-list triage: skip the per-run JOB fetch (the dominant gh cost) for a workflow
        # whose SLOWEST sampled run finishes under the floor — it can't hold the merge pole
        # or move the cross-workflow floor. Wall-time comes from run metadata already in
        # hand (run_started_at/created_at → updated_at), no extra call. Conservative (max,
        # not median) so a workflow that ran long anywhere in the sampled window is fetched.
        # The PR-sha pool, events, and volume here still run for triaged workflows (they use
        # run metadata, not jobs); only job-level hygiene/queue for this workflow degrade to
        # run-list-only.
        # Triage off the ERA-partitioned runs (issue #66): the triage gate AND the
        # `concurrent_wall_p50` cross-workflow floor contribution below must reflect the kept era,
        # not the blended sample — else a straddling triaged workflow feeds a blended wall to the
        # floor. `runs_for_spine is runs` for every non-straddling workflow, so this is byte-identical
        # except on a straddle.
        if wf_path not in opt57_seed_workflows and _should_triage_workflow(runs_for_spine[:shallow_n]):
            # Triaged: no job fetch. The shared PR-sha / events / volume code still
            # runs (run metadata, no job cost); only this workflow's job-level signal is
            # absent (empty crit), degrading its hygiene/queue to run-list-only.
            triaged_fast_workflows.append(wf_path)
            # `long_pole_p50: 0.0` keeps the triaged workflow OUT of pole/deepen selection
            # (`_deepen_candidates` / `_select_pr_floor_workflows` gate on `long_pole_p50 > 0`)
            # — it has no job sample to drill. But it DOES still run concurrently on the PR, so
            # it must keep contributing to the cross-workflow wall-clock floor (else a saving
            # could overstate by up to the floor when the binding sibling is itself sub-90s).
            # `concurrent_wall_p50` carries its run-list wall-time (already in hand) for exactly
            # that — read ONLY by `wall_clock._concurrent_workflows`, never by pole selection.
            crit_by_wf[wf_path] = {"long_pole_job": "", "long_pole_p50": 0.0,
                                   "floor_p50": 0.0, "job_p50": {},
                                   "concurrent_wall_p50": _max_sampled_run_wall_s(
                                       runs_for_spine[:shallow_n])}
            jobs_per_run_by_wf[wf_path] = []
            # Retain the runs so relative-recovery (below) can job-fetch this workflow if its
            # wall turns out to reach the measured gate (the absolute floor mis-triaged it).
            # Stash the ERA-partitioned set so a recovered workflow's drill also stays in one
            # era (issue #66) — never the retired-config runs.
            triage_recover_stash[wf_path] = runs_for_spine
            logger.debug("triage: skipped job-fetch for fast workflow %r "
                         "(max sampled run wall %.0fs < %.0fs floor)",
                         wf_path, _max_sampled_run_wall_s(runs_for_spine[:shallow_n]),
                         _TRIAGE_WALLCLOCK_FLOOR_S)
        else:
            # DEFER the per-run job fetch. It is the single most-issued endpoint in the
            # pass, so instead of fetching per workflow here (one ~10-run wave plus a
            # quarter-utilised tail, 31 times over) we record the plan and drain it in ONE
            # global wave below (LEVER 2). Seed BOTH per-workflow dicts here, in
            # `workflows_in_play` order, even though the shallow job pass below overwrites
            # them: their KEY ORDER is load-bearing (`jobs_per_run_by_wf` drives the
            # detector loop's finding-id sequence; ties in the workflow floor break on
            # `crit_by_wf` order), and dict insertion order survives an overwrite but not a
            # late first-insert.
            crit_by_wf[wf_path] = {"long_pole_job": "", "long_pole_p50": 0.0,
                                   "floor_p50": 0.0, "job_p50": {}}
            jobs_per_run_by_wf[wf_path] = []
            # DRILL off the era-partitioned runs (issue #66): the shallow/deepen job
            # passes build `jobs_per_run_by_wf` (→ the drilled poles) from this list, so a
            # straddling workflow contributes one era's step layout only.
            shallow_plan.append((wf_path, runs_for_spine,
                                 min(shallow_runs, len(runs_for_spine))))
        # Candidate PR head-SHAs where THIS workflow ran (developer-facing
        # events), with their most-recent run timestamp — the pool the measured
        # critical-path sample (20 most recent full-suite PRs) is drawn from.
        #
        # Draw from `runs_for_spine` (issue #66) so a NON-straddling workflow is byte-identical.
        # On a STRADDLE (issue #77), seed from the FULL sample instead: content-keying the spine
        # partition drops the DROPPED-side runs from `runs_for_spine` (e.g. a disclosed_pre keeps
        # only pre-content runs), but `_era_resolve_thin_flip` must still SEE the dropped-side
        # (content-post fix-PR) gate checks to detect a check-empty kept era and fire the flip.
        # The per-era CHECK scoping happens downstream in `_era_scope_enumeration`/`_era_pr_side`,
        # so a dropped-side PR that enters the pool here is still era-scoped out of the rendered
        # enumeration — it is only made VISIBLE to the flip decision, never blended into the spine.
        _pool_runs = runs if _era_fact is not None else runs_for_spine
        for r in _pool_runs:
            if str(r.get("event") or "") in _DEVELOPER_EVENTS and r.get("head_sha"):
                sha = str(r["head_sha"])
                ts = str(r.get("created_at") or r.get("updated_at") or "")
                if ts > pr_sha_ts.get(sha, ""):
                    pr_sha_ts[sha] = ts
                # #58: keep the identity metadata for the run with the latest ts,
                # using the SAME strict `>` guard as `pr_sha_ts` above so the two
                # updates mirror each other exactly — the metadata always describes the
                # run whose ts `pr_sha_ts` keys on, with no equal-ts iteration-order
                # ambiguity between them.
                prev_meta = sha_meta.get(sha)
                if prev_meta is None or ts > str(prev_meta.get("ts") or ""):
                    sha_meta[sha] = {"event": str(r.get("event") or ""),
                                     "pr_num": _run_pr_number(r), "ts": ts}

    # === SHALLOW JOB PASS (ONE global pool) ==================================
    # LEVER 2. The per-run job listing is the single most-issued endpoint in the pass.
    # It used to be pooled PER WORKFLOW — ~10 runs into an 8-wide pool, i.e. one full
    # wave plus a quarter-utilised tail, then teardown and rebuild, 31 times over. The
    # runs are independent across workflows, so flatten the whole shallow sample into
    # ONE wave: prefetch every planned run's job listing through the shared pool, then
    # let each workflow's `_gather_run_jobs` below read it straight from the buffer.
    # `_gather_run_jobs` still zips results back in INPUT order per workflow (and dedups
    # via `job_memo`), so the per-workflow sample is bit-for-bit what the serial version
    # produced. Only page 1 is prefetched; a >100-job run paginates live through the
    # governed `_invoke` from inside `_paginate`.
    _prefetch_json(client, [_run_jobs_endpoint(repo, r.get("id"))
                          for _wf, runs, n in shallow_plan for r in runs[:n]])

    for wf_path, runs, shallow_n in shallow_plan:
        jobs_per_run: list[list[dict[str, Any]]] = []
        jobs_by_event: dict[str, list[list[dict[str, Any]]]] = {}
        # SHALLOW pass: jobs for only the first _SHALLOW_RUNS runs (the dominant per-run
        # call cost). Enough to rank which workflows are poles; the deepen pass below
        # fetches the rest for the top candidates so their p50 is exact. A FAILED fetch
        # is a coverage gap (`wf_failures`); a genuinely empty run is just dropped.
        kept, wf_failures = _gather_run_jobs(client, repo, runs[:shallow_n], memo=job_memo)
        jobs_fetch_failures += wf_failures
        _add_cost_spine_fetch_failures(wf_path, wf_failures)
        if wf_failures and not kept:
            # WIPEOUT: not one sampled run's jobs came back. The workflow has runs but no
            # job timing — MISSING from the measured sample, not fast. Named as a coverage
            # gap (a bare failed-call count would render as the MINOR "marginally fewer
            # runs" caveat) and its checks are barred from the queue-inflated check-run
            # fallback below.
            _note_job_fetch_wipeout(wf_path, shallow_n)
        nr, nj = _accumulate_jobs(kept, jobs_per_run, jobs_by_event)
        runs_sampled += nr
        jobs_sampled += nj
        # Per-event scope: wall-clock measures DEVELOPER (PR) wait, so scope the
        # critical path to the developer-facing event's runs when present (blending in
        # push-to-main runs would size the PR wait against the wrong long pole); fall
        # back to all runs otherwise. `_critical_path` then scopes each job to its own
        # dominant runner, so heterogeneous-runner repos aren't blended.
        crit, crit_runs = _crit_for(jobs_per_run, jobs_by_event)
        crit_by_wf[wf_path] = crit
        jobs_per_run_by_wf[wf_path] = crit_runs
        events_jobs_by_wf[wf_path] = jobs_by_event   # by-ref; deepen extends in place
        # Stash the runs we DIDN'T job-fetch so the deepen pass can extend this
        # workflow's sample in place if it ranks among the top poles.
        if len(runs) > shallow_n:
            deepen_stash[wf_path] = (runs[shallow_n:], jobs_per_run, jobs_by_event)

    # The breaker can trip on the shallow job pass's fetches, after the triage loop's own
    # check — the fetches are deferred to the ONE global wave above, so abort here.
    if getattr(client, "gave_up", False):
        return _abort_gh_pass()

    # DEEPEN pass (adaptive, iterate to CONVERGENCE). The shallow ranking is in
    # `crit_by_wf`; deepen the owners of the top `_DEEPEN_TOP_CHECKS` concurrent CHECKS
    # (each candidate's per-job p50s plus its long-pole p95 tail) to the full sample and
    # recompute, then re-rank and repeat. Re-ranking matters: a deepened check's corrected
    # (often lower) p50 can
    # let a previously sub-cut candidate rise into the top region, and deepening it in
    # turn keeps a shallow p50 from ever sitting in the gate/drill/floor. At
    # convergence the top region is entirely full-depth, so the gate, drill-set, the
    # cross-workflow floor, and their bimodal flags are depth-invariant (== a single
    # full-depth pass — validated by Monte-Carlo subsampling on 5 repos). Eligibility is
    # FULL-BREADTH (`events_by_wf`), not the shallow `event_scope`, so a push-heavy
    # shallow window can't hide a PR gate from deepening. Skipped when the shallow depth
    # already covers --max-runs (`deepen_stash` empty → one full pass).
    #
    # CONCURRENCY BOUNDARY — the rounds are ORDER-DEPENDENT and stay strictly serial.
    # Each round's ranking is computed from the PREVIOUS round's corrected p50s; that
    # feedback IS the convergence guarantee above. Overlapping two rounds would rank
    # against a half-deepened sample and could settle on a different (wrong) top region.
    # What IS safe is the fan-out WITHIN one round: the workflows a round selects are
    # independent of each other, so `_deepen_round_prefetch` issues all of their
    # remaining run-job fetches in one wave before the round's serial `_deepen` calls
    # consume them from the buffer. Parallel inside a round; never across rounds.
    def _deepen_round_prefetch(wf_paths: list[str]) -> None:
        _prefetch_json(client, [
            _run_jobs_endpoint(repo, r.get("id"))
            for w in wf_paths if deepen_stash.get(w)
            for r in deepen_stash[w][0]])

    def _deepen(wf_path: str) -> None:
        nonlocal jobs_fetch_failures
        nonlocal runs_sampled, jobs_sampled
        rest, jpr, jbe = deepen_stash[wf_path]
        kept2, fail2 = _gather_run_jobs(client, repo, rest, memo=job_memo)
        jobs_fetch_failures += fail2
        _add_cost_spine_fetch_failures(wf_path, fail2)
        nr, nj = _accumulate_jobs(kept2, jpr, jbe)
        runs_sampled += nr
        jobs_sampled += nj
        crit, crit_runs = _crit_for(jpr, jbe)
        crit_by_wf[wf_path] = crit
        jobs_per_run_by_wf[wf_path] = crit_runs
        events_jobs_by_wf[wf_path] = jbe   # same dict; already extended in place above

    # Relative triage RECOVERY (see `_TRIAGE_RECOVER_GATE_FRAC`). The absolute
    # `_TRIAGE_WALLCLOCK_FLOOR_S` is a coarse pre-filter; on a seconds-scale repo it can sit
    # at/above the measured gate, so a workflow under the floor may still own a check that
    # ranks among the drilled secondary poles (and silently sets the headline's binding
    # floor). Now that the shallow pass has measured the gate, job-fetch any triaged workflow
    # whose run-list wall reaches `frac` of the gate's long pole, so it is ranked + drilled
    # like any pole rather than dismissed as "can't hold the merge pole" — making triage
    # relative to the pole rank, not the absolute constant. Runs BEFORE `_deepen_candidates`
    # so a recovered workflow flows into the deepen pass like any other pole candidate.
    # Metric note: `gate_p50` is a job-level MEDIAN (`long_pole_p50` from `_critical_path`),
    # while `wall` below is `concurrent_wall_p50` — a run-list MAX sampled wall time
    # (`_max_sampled_run_wall_s`), not a p50. Comparing a max against a median is intentional
    # here: this is a deliberately COARSE, fetch-cheap pre-filter ("is this triaged workflow
    # even in the ballpark of the gate before we pay to job-fetch it?"). The skew is toward
    # over-inclusion (recover-more), never wrongly excluding a pole, so the looseness is safe.
    gate_p50 = max((c.get("long_pole_p50") or 0.0 for c in crit_by_wf.values()),
                   default=0.0)
    recover_floor = gate_p50 * _TRIAGE_RECOVER_GATE_FRAC
    recovered_workflows: list[str] = []

    def _recovers(wf_path: str) -> list[dict[str, Any]]:
        """The runs this triaged workflow would be job-fetched for (empty = not
        recovered). Pure — the gate reads only `crit_by_wf`, which is settled by now —
        so the prefetch wave below can plan the exact fetch set the loop will issue."""
        wall = (crit_by_wf.get(wf_path) or {}).get("concurrent_wall_p50") or 0.0
        runs = triage_recover_stash.get(wf_path) or []
        if recover_floor <= 0 or wall < recover_floor or not runs:
            return []
        return runs[:min(shallow_runs, len(runs))]

    # Recovery is itself pure fan-out across workflows — one wave, then the serial walk.
    _prefetch_json(client, [_run_jobs_endpoint(repo, r.get("id"))
                          for w in list(triaged_fast_workflows)
                          for r in _recovers(w)])
    for wf_path in list(triaged_fast_workflows):
        # ONE definition of "is this workflow recovered, and which runs for?" — the same
        # `_recovers` the prefetch plan above used. The loop must never re-state the
        # predicate inline: two copies of one gate in adjacent code is precisely how a
        # plan drifts from its call site and starts paying for responses nobody consumes.
        recover_runs = _recovers(wf_path)
        if not recover_runs:
            continue
        runs = triage_recover_stash.get(wf_path) or []
        wall = (crit_by_wf.get(wf_path) or {}).get("concurrent_wall_p50") or 0.0  # disclosure only
        shallow_n = len(recover_runs)
        jobs_per_run: list[list[dict[str, Any]]] = []
        jobs_by_event: dict[str, list[list[dict[str, Any]]]] = {}
        # `recover_runs` == `runs[:shallow_n]` by construction (shallow_n = len(recover_runs));
        # `_recovers` is the one definition the prefetch wave above used. `memo=job_memo`
        # dedups against the rest of the pass (these run ids are fresh — triaged workflows
        # weren't job-fetched in the shallow pass — so it never masks the prefetched page).
        kept, wf_failures = _gather_run_jobs(client, repo, recover_runs, memo=job_memo)
        jobs_fetch_failures += wf_failures
        nr, nj = _accumulate_jobs(kept, jobs_per_run, jobs_by_event)
        runs_sampled += nr
        jobs_sampled += nj
        crit, crit_runs = _crit_for(jobs_per_run, jobs_by_event)
        if (crit.get("long_pole_p50") or 0.0) <= 0:
            # The recovery job-fetch produced no usable job timing (e.g. every run's job
            # fetch errored — a live possibility under rate-limit pressure). Do NOT overwrite
            # the triaged stub: it still holds this workflow's `concurrent_wall_p50`, which
            # keeps it contributing to the cross-workflow wall-clock floor. Replacing it with
            # an empty crit (no `long_pole_p50`, no `concurrent_wall_p50`) would SILENTLY drop
            # that floor — overstating a headline saving — and mislabel the workflow as
            # "recovered" when its data is actually worse than the stub. Leave it triaged; the
            # fetch failure is surfaced via `jobs_fetch_failures`. (A partial fetch yields a
            # positive `long_pole_p50` and recovers normally — only total failure stays here.)
            logger.warning("triage: recovery fetch for %r yielded no job timing; keeping it "
                           "triaged (stub wall-clock floor preserved) rather than "
                           "false-recovering with empty data", wf_path)
            continue
        _add_cost_spine_fetch_failures(wf_path, wf_failures)
        crit_by_wf[wf_path] = crit          # real job timing replaces the triaged stub
        jobs_per_run_by_wf[wf_path] = crit_runs
        events_jobs_by_wf[wf_path] = jobs_by_event   # by-ref; deepen extends in place
        if len(runs) > shallow_n:
            deepen_stash[wf_path] = (runs[shallow_n:], jobs_per_run, jobs_by_event)
        triaged_fast_workflows.remove(wf_path)
        recovered_workflows.append(wf_path)
        logger.info("triage: recovered fast workflow %r (run-list wall %.0fs >= %.0fs = "
                    "%.0f%% of the %.0fs gate) — job-fetched so it ranks/drills like any pole",
                    wf_path, wall, recover_floor, _TRIAGE_RECOVER_GATE_FRAC * 100, gate_p50)

    eligible = _deepen_candidates(crit_by_wf, events_by_wf)
    deepened: list[str] = []          # actually fetched (already-complete wfs aren't)
    settled: set[str] = set()         # owners of the top checks, at full depth
    converged = True
    for _round in range(len(eligible) + 2):   # bounded; converges well before the cap
        # Rank every candidate CHECK key (each job's p50 + the long-pole tail) across
        # the eligible workflows; the owners of the top _DEEPEN_TOP_CHECKS are the ones
        # whose shallow p50 could land in the rendered chart / floor and so must be
        # full-depth. Re-ranking each round matters: a deepened check's corrected p50
        # can let a previously sub-cut check rise into the rendered region.
        ranked = sorted(
            ((wf, k) for wf in eligible for k in _deepen_check_keys(crit_by_wf[wf])),
            key=lambda t: -t[1])
        want: list[str] = []
        for wf, _k in ranked[:_DEEPEN_TOP_CHECKS]:
            if wf not in want:
                want.append(wf)
        new = [w for w in want if w not in settled]
        if not new:
            break
        # One fan-out wave for THIS round's selected workflows (see the boundary note
        # on `_deepen_round_prefetch`); the per-workflow bookkeeping stays serial, and
        # the NEXT round is not planned until this one has fully landed.
        _deepen_round_prefetch(new)
        for w in new:
            settled.add(w)
            if deepen_stash.get(w):       # has un-fetched deeper runs
                _deepen(w)
                deepened.append(w)
    else:
        # The cap was hit without the top region settling — surface it, never present
        # an unconverged ranking as full-depth-equivalent.
        converged = False
        logger.warning("adaptive sampling: deepen did not converge in %d rounds; the "
                       "top region may rest on a shallow sample", len(eligible) + 2)
    if deepened:
        logger.info("adaptive sampling: shallow %d-run pass, then deepened %d pole "
                    "candidate(s) to %d runs (%d round(s))",
                    shallow_runs, len(deepened), max_runs, _round + 1)

    # Required-status checks (branch protection / rulesets) — fetched here (not
    # later) so the measured-critical-path sample can be scoped to PRs that ran
    # the FULL required suite. None when unreadable (no admin / no protection).
    repo_info = client.json(f"repos/{repo}")
    default_branch = (repo_info or {}).get("default_branch")
    # `--root` is on for EVERY run now, so every workflow's YAML comes off the working
    # tree. `_root_is_clone_of` proved it is the right REPO; this asks the other half —
    # is it the right COMMIT LINE? A clean feature branch or a stale `main` is not dirty,
    # so nothing else would ever say a word about it, while the detectors parse YAML that
    # produced none of the sampled runs. Checked HERE because `default_branch` is only
    # known now, and consumed before `_fetch_workflow_docs` runs below.
    workflow_yaml_skew = _root_branch_skew(root, default_branch)
    if workflow_yaml_skew:
        logger.warning(
            "workflow YAML source skew: %s. The detectors parse this checkout's YAML "
            "while every timing comes from runs of `%s` — a trigger or job added since "
            "this checkout will be measured but not parsed (and vice versa). The report "
            "names the skew.",
            workflow_yaml_skew["reason"], default_branch)
    # Repo visibility — recorded as informational metadata on the cost spine.
    findings_doc["repo_visibility"] = _derive_repo_visibility(repo_info)
    required_checks = _fetch_required_checks(client, repo, default_branch)
    findings_doc["required_checks"] = (
        sorted(required_checks.names) if required_checks is not None else None)
    findings_doc["required_checks_complete"] = (
        required_checks.complete if required_checks is not None else False)

    # MEASURED CRITICAL PATH — fetch the full check-run set for the 20 most recent
    # PRs that ran the FULL CI suite (their check-runs include every required
    # status check), so the sampled critical path reflects what a real code PR
    # actually waits on — not a docs-only PR that self-skips the gate. check-runs
    # include checks with NO workflow file (CodeQL default setup, app checks) that
    # scan.py can't see, so this is the true concurrent-check floor for sizing.
    # Falls back to the 20 most recent dev-event PRs when required checks are
    # unreadable. Candidates are walked newest-first; we fetch each one's
    # check-runs and keep it only if it ran the full required suite.
    _REPR_PR_TARGET = 20
    req_names = required_checks.names if required_checks is not None else frozenset()

    # ENG-1 PR-N1: per-sha latest-attempt check-run intervals, stashed on the
    # side for the empirical-makespan stamp (`_pr_makespan`). Latest attempt =
    # the interval with the max started_at per check name. NOTE the deliberate
    # asymmetry with the span map below (`m[name] = max(...)` keeps the MAX
    # span across attempts): chain spans measure the worst observed cost,
    # while the makespan measures the PR's actual latest wall — so
    # makespan >= chain_s is NOT an invariant, and PR-N2's divergence
    # disclosure must expect negative divergence. Caps apply at stamp time.
    sha_check_intervals: dict[str, dict[str, tuple[str, str]]] = {}

    # #58: dedupe the presence candidates to PR identity BEFORE sampling, so a
    # merge-queue (`merge_group`) run reinforces its PR's population row instead of
    # creating a new denominator "PR". Every downstream presence/populations/
    # chain_facts consumer inherits the correction (they all re-derive from
    # `per_sha_checks`), and `verify_report`'s mirrors re-derive from the stamped
    # `populations` — so a PR-deduped denominator needs no verifier change. On a repo
    # with no merge_group runs this is the identity map: one member per group, exactly
    # the pre-#58 per-sha behaviour.
    _rep_ts, _rep_members = _group_dev_shas_by_pr(sha_meta)

    def _checks_for(rep_sha: str) -> dict[str, float] | None:
        # UNION the check-runs across every head_sha the PR ran on (its pull_request
        # head plus, when merge-queued, its `gh-readonly-queue` commit) so the queue's
        # heavy suite and the PR event's checks share one row. A gap on ANY member
        # makes the whole row a coverage gap (None) — never a laundered partial row
        # (see `_union_member_checks`) — so `_select_repr_shas` counts it as a fetch
        # failure exactly as the pre-#58 per-sha path did.
        res = _union_member_checks(
            _rep_members.get(rep_sha, [rep_sha]),
            lambda member: _fetch_check_runs(client, repo, member))
        if res is None:
            return None  # propagate the fetch failure — don't fake a partial PR
        m, iv = res
        sha_check_intervals[rep_sha] = iv
        return m

    repr_shas, per_sha_checks, check_durs, sample_diag = _select_repr_shas(
        _rep_ts, _checks_for, req_names, _REPR_PR_TARGET)
    # Config-era thin-flip (issue #74, direction (a): the timing spine flips WITH the rule). Now that
    # the PR gate sample (`per_sha_checks`) is in hand we can tell whether a `disclosed_pre` straddle's
    # kept PRE era actually produced a gate-bearing check IN THE SAMPLE. When it did not (the gate PRs
    # are all post-change), measuring the old config is unavailable — flip to `post_only_thin` and
    # RE-DRILL that workflow's spine from its POST runs, BEFORE `pr_check_p50` is built, so every
    # rendered number (crit_by_wf, poles, representative-run links, makespans) derives from the new
    # config. `check_wf_of` attributes a post-only check via the scanned job graph even before the
    # re-drill lands (crit_by_wf still holds pre timing at decision time); after the re-drill it
    # attributes via the post timing too. A pure no-op when nothing flips (byte-identity for
    # post_only / disclosed_pre-with-a-pre-gate-check / no-straddle).
    _era_jg = findings_doc.get("workflow_job_graph") or {}

    def _era_check_wf_of(name: str) -> str | None:
        m = _map_check_to_job(name, crit_by_wf, require_developer_timing=True)
        if m is not None:
            return m[0]
        if _era_jg:
            node = _check_to_job_node_scanned(name, _era_jg)
            if node is not None:
                return node[0]
        return None

    def _era_redrill(wf: str, post_runs: list[dict[str, Any]]) -> None:
        # Re-drill the flipped workflow's crit/jobs from its POST runs. The post sample is thin by
        # construction (< _RARE_PRESENCE_MIN_PR — the very reason disclosed_pre was chosen), so a
        # single job-fetch pass covers it (no deepen). Its jobs were never fetched (the shallow/deepen
        # passes drilled `runs_for_spine` = the PRE runs), so this is a real fetch, gated to the one
        # flipping workflow. crit_by_wf / jobs_per_run_by_wf / events_jobs_by_wf are replaced in place
        # so every downstream consumer (pr_check_p50, poles, the detector loop) sees the post era.
        nonlocal jobs_fetch_failures, runs_sampled, jobs_sampled
        post_slice = post_runs[:max_runs]
        _prefetch_json(client, [_run_jobs_endpoint(repo, r.get("id")) for r in post_slice])
        kept_jobs, wf_failures = _gather_run_jobs(client, repo, post_slice, memo=job_memo)
        # Surface a post re-drill fetch failure through the SAME coverage machinery as every other
        # job-fetch path (issue #74 review): never swallow it into unmeasured-silence.
        jobs_fetch_failures += wf_failures
        _add_cost_spine_fetch_failures(wf, wf_failures)
        jpr: list[list[dict[str, Any]]] = []
        jbe: dict[str, list[list[dict[str, Any]]]] = {}
        # The post runs are DISTINCT gh runs (the shallow/deepen passes drilled the PRE runs), so
        # their fetch counts into the sample provenance like every sibling path (shallow 13738,
        # deepen 13795, recovery 13856) — else `data_sources.runs_sampled` would count the discarded
        # pre-era runs and omit the rendered post-era ones for exactly the flipped workflow.
        nr, nj = _accumulate_jobs(kept_jobs, jpr, jbe)
        runs_sampled += nr
        jobs_sampled += nj
        crit, crit_runs = _crit_for(jpr, jbe)
        # Era-safety coverage gap: the pre spine is DISCARDED regardless (a pre timing must never
        # render under the post claim), so if the POST re-drill yields no usable developer timing —
        # a fetch WIPEOUT, or runs that fetched but carry no developer-event job timing (the same
        # `long_pole_p50 <= 0` signal the triage-recovery pass guards on) — the flipped workflow has
        # NO measurable post spine. NAME it a coverage gap (drives `partial_kind` AND bars the
        # spine-wide queue-inflated fallback) so the thin disclosure never renders over an empty
        # spine, and the flipped workflow's now-timing-less checks don't fall through to the
        # queue-inflated check-run fallback — whose `check_durs` is NOT era-scoped and would
        # reintroduce a pre-blended number under the "measures the new configuration" claim.
        if (crit.get("long_pole_p50") or 0.0) <= 0.0:
            _note_job_fetch_wipeout(wf, len(post_slice))
        crit_by_wf[wf] = crit
        jobs_per_run_by_wf[wf] = crit_runs
        events_jobs_by_wf[wf] = jbe

    _era_resolve_thin_flip(config_era_facts, repr_shas, per_sha_checks, _rep_ts,
                           _era_check_wf_of, sampled_runs_by_wf, _era_redrill)
    # Reliable per-job execution times from the jobs API (runner-scoped, no
    # queue), across all sampled workflows. A check-run's started→completed span
    # can be inflated by queue waits and re-runs (e.g. an 80s `Integration test`
    # job whose check-run reads 1871s), so for any check that maps to a sampled
    # JOB we use the job's p50; check-run durations are used only for checks with no
    # sampled developer-event job. That includes genuinely fileless checks (CodeQL
    # default setup, app checks) and a workflow check whose only sampled job timing is
    # all-events. The latter is intentionally NOT capped by push/schedule job timing:
    # queue-inflated PR check-runs are conservative but honest, while borrowing
    # all-events job timing would reintroduce the event-scope leak this path prevents.
    job_p50_all: dict[str, float] = {}
    job_bimodal_all: dict[str, dict[str, Any]] = {}
    for c in crit_by_wf.values():
        if not _crit_has_developer_timing(c):
            continue
        for name, p in (c.get("job_p50") or {}).items():
            job_p50_all[name] = max(job_p50_all.get(name, 0.0), p)
        for name, bi in (c.get("job_bimodal") or {}).items():
            # Prefer the split with the larger slow share (more clearly bimodal).
            cur = job_bimodal_all.get(name)
            if cur is None or bi.get("slow_frac", 0) > cur.get("slow_frac", 0):
                job_bimodal_all[name] = bi
    pr_check_p50: dict[str, float] = {}
    check_timing_source: dict[str, str] = {}
    # A JOB-FETCH WIPEOUT bars the check-run-span fallback for the WHOLE spine.
    #
    # The fallback (`pr_check_runs`) is a check's queue-INFLATED span: it measures from
    # the check being CREATED, so an 80s job whose runner waited 30 minutes reads as
    # 1871s. That is tolerable for a check no sampled job produced (a fileless app
    # check) — conservative but honest. It is NOT tolerable when the reason a check has
    # no job timing is that we FAILED TO FETCH IT: the inflated span then stands in for
    # a real job, can outrank the true gate, and HEADLINES the report with a number that
    # is mostly queue.
    #
    # We cannot attribute a check-run back to the wiped workflow (the check-runs endpoint
    # carries no workflow path, and the job names that would match it are precisely what
    # the failed fetch would have told us). So the bar is spine-wide while a wipeout
    # stands: a fallback-timed check is DROPPED from the critical path rather than
    # allowed to headline off a number we can't trust. Every wiped workflow is NAMED in
    # the coverage note, and `partial_kind` is severe, so the report says the spine is
    # incomplete instead of quietly headlining the wrong gate. Restored the moment the
    # wipeout is: with no `job_fetch_failures`, this is exactly the old behaviour.
    _bar_queue_inflated_fallback = bool(job_fetch_failures)
    for n, ds in check_durs.items():
        mapped = _map_check_to_job(n, crit_by_wf, require_developer_timing=True)
        if mapped is not None:
            wf, job = mapped
            jp = (crit_by_wf.get(wf, {}).get("job_p50") or {}).get(job)
            if jp is not None:
                pr_check_p50[n] = round(jp, 1)
                check_timing_source[n] = "workflow_jobs"
                if job in job_bimodal_all and n not in job_bimodal_all:
                    job_bimodal_all[n] = job_bimodal_all[job]
                continue
        # Cross-workflow same-name ambiguity (issue #59): `_map_check_to_job` bailed because it
        # can't attribute ONE workflow file to drill/fix — but the crowning MAGNITUDE is
        # unambiguous (a PR waits on the SLOWEST same-named job). Ground the spine on that real job
        # p50 so a duplicated monorepo merge gate stays in the crowning basis instead of being
        # mis-partitioned into the fileless / PR-lifetime bucket (which would silently uncrown a
        # real pole and mislabel it status-gating latency). Its per-file drill stays withheld —
        # `_pole_mapping`/`_map_check_to_job` still return None, so `_decompose_pole` renders it
        # unattributed and the collision is disclosed (`_check_producing_workflows`). This is a
        # sampled job p50, NOT the queue-inflated check-run span, so the wipeout bar below (which
        # guards against inflated fallback spans) does not apply — hence the `continue` above it.
        amb_jp = _check_grounded_job_p50(n, crit_by_wf, require_developer_timing=True)
        if amb_jp is not None:
            pr_check_p50[n] = round(amb_jp, 1)
            check_timing_source[n] = "workflow_jobs"
            # Deliberately NOT propagating a `job_bimodal_all` entry (unlike the mapped branch
            # above): there is no single job to key a bimodal split on, so the ambiguous crown is
            # scored on a flat p50 — per-PR population segmentation still applies downstream.
            continue
        if _bar_queue_inflated_fallback:
            logger.warning(
                "check %r has no sampled job timing and a job-fetch WIPEOUT is in play "
                "(%s) — dropping it from the critical path rather than letting a "
                "queue-inflated check-run span headline the report",
                n, ", ".join(g["workflow_file"] for g in job_fetch_failures))
            continue
        pr_check_p50[n] = round(_percentile(ds, 50), 1)
        check_timing_source[n] = "pr_check_runs"
    # Keep only checks a developer actually waits on to MERGE: drop checks that map
    # to a push-only / scheduled workflow (deploy-to-staging, release) whose
    # check-run merely rode along on a sampled SHA. Without this, a repo that
    # deploys on push (e.g. langfuse) shows ecs-deploy jobs as the "merge gate".
    # PR-gating is decided from the workflow's DECLARED triggers (ground truth),
    # not just the success-only sample, so a real PR check is never excised because
    # its recent successes happened to be push runs. The workflow files are read +
    # parsed ONCE here (from the local checkout when `--root` gives us one — same commit
    # the report stamps; the API's default-branch HEAD only as a fallback) and reused
    # both for the trigger guard and for OPT24's shard recognizer in the loop below.
    _wf_docs = _fetch_workflow_docs(
        client, repo, set(crit_by_wf) | set(jobs_per_run_by_wf), root=root,
        source_counts=workflow_yaml_source)
    logger.info("workflow YAML: %d read from the checkout, %d fetched from the gh "
                "contents API (default-branch HEAD)",
                workflow_yaml_source.get("checkout", 0),
                workflow_yaml_source.get("api", 0))
    _pr_workflows = _declared_pr_workflows(
        client, repo, set(crit_by_wf) | set(jobs_per_run_by_wf), wf_docs=_wf_docs)
    _dropped_non_pr = [n for n in pr_check_p50
                       if not _is_pr_gate_check(n, crit_by_wf, events_by_wf,
                                                _pr_workflows, req_names)]
    if _dropped_non_pr:
        logger.info("critical path: dropped %d push-only / non-PR-gating check(s): %s",
                    len(_dropped_non_pr), _dropped_non_pr[:8])
        pr_check_p50 = {n: v for n, v in pr_check_p50.items()
                        if n not in set(_dropped_non_pr)}
    # Required-relevance scope — the report's title is "why is the merge slow?", so the
    # spine (and thus the headline pole) must be the merge-BLOCKING checks. When a real,
    # COMPLETE, non-empty required set resolved, drop checks that are neither required nor
    # something the required work transitively `needs:`. A non-required check that gates
    # zero merges must never headline (live-confirmed on mastra, where the non-required
    # `changed-tests` outranked the genuinely-gating required path). Scoping is by
    # `needs:`-reachability over the workflow job graph (not file co-residence, which
    # over-includes an independent sibling sharing a required file). The gating +
    # never-empty rules live in `_scope_spine_to_required` (pure, unit-tested); a drop is
    # recorded, never hidden.
    pr_check_p50, dropped_non_required, spine_required_scoped = _scope_spine_to_required(
        pr_check_p50, required_checks, req_names, crit_by_wf,
        findings_doc.get("workflow_job_graph"),
        bool(sample_diag.get("required_suite_unsatisfiable")))
    if dropped_non_required:
        logger.info("critical path: dropped %d non-required (non-merge-gating) "
                    "check(s): %s", len(dropped_non_required),
                    dropped_non_required[:8])
    # Config-era enumeration binding (issue #69). #66/#68 scoped the SPINE RUNS to one era, but
    # the CHECK SET was still enumerated from the raw PR sample — so a disclosed_pre report leaked
    # post-era-only checks (the NEW config's `guard shard N/4` jobs, carried by 2 post-change PRs)
    # into the poles / Level-1 chart / populations beside the pre-era `test` timing. Bind the
    # enumeration to the kept era: a check attributed to a straddling workflow but observed only on
    # the DROPPED side is the other configuration's add/remove — dropped here (so it never crowns,
    # bars, or segments) and named on the fact for the era note. A pure no-op when nothing straddled
    # (`config_era_facts` empty), so a non-straddling repo is byte-identical (L2). Placed AFTER the
    # non-PR / non-required drops (operate on the gate set) and BEFORE ranking / fileless partition /
    # populations / poles, so every downstream consumer inherits the single-config enumeration.
    # `_era_check_wf_of` is defined above (with the thin-flip), reused here on the now-resolved facts.
    pr_check_p50 = _era_scope_enumeration(
        pr_check_p50, repr_shas, per_sha_checks, _rep_ts,
        config_era_facts, _era_check_wf_of)
    # Issue #116: stamp spine relevance now that the enumeration is bound AND the workflow triggers
    # are in hand, so the renderer suppresses the GLOBAL era caveat for a straddle that cannot touch
    # the headline (push/cron-only, or check-neutral — 0 spine checks) while every bill-side era
    # consumer keeps the full fact. Pure stamp; drops nothing.
    _era_stamp_spine_relevance(config_era_facts, _wf_docs, crit_by_wf)
    # Spine ordering: the headline pole must be the gate a TYPICAL PR waits on. Rank the
    # checks a majority of sampled PRs ran ABOVE rare/opt-in ones (a label-gated benchmark
    # that's slowest only when it runs), each tier by p50 desc. Presence is from the per-PR
    # check maps (no extra gh call); a required check gates by definition and is never
    # demoted; inert on a tiny sample where the fraction is noise. This is the single source
    # so the headline (`critical_path_check`), the drilled `poles`, structural targeting,
    # and the data-pass summary all agree the rare giant isn't the merge gate. The renderer
    # applies the same split for its visualization/labels.
    #
    # Floor-consistency invariant: this only REORDERS pr_check_p50 (no drops), and a check
    # demoted here that is also a SLOW one is, by construction, bimodal across PRs (present
    # on some, absent on others) — so `_segment_pr_populations` always emits populations for
    # it, and `size_wall_clock` floors each finding per-population (excluding the rare giant
    # on the PRs where it didn't run). I.e. a demoted slow pole never sizes a typical fix's
    # saving against itself via the flat (no-population) floor — the two conditions co-occur.
    # ("always emits populations" relies on `_segment_pr_populations`'s `high >= 120s`
    # bimodal threshold; a demoted SLOW pole is well above 120s, so keep that threshold below
    # pole scale or this co-occurrence weakens. It also assumes the capped population count
    # stays >= 6 — below that `_segment_pr_populations` returns [] and the flat floor applies;
    # since demotion shares the same `n_pr >= 6` floor, the two stay aligned in the common
    # case, but a sample that drops below 6 PRs during capping falls back to the flat floor.)
    # Fileless-span headline cap (issue #12). Exclude fileless/managed status checks (bot gates,
    # label gates, external app checks — anything that produces no sampled workflow job) from the
    # CROWNING BASIS before ranking. Their only timing is a `pr_check_runs` span measured from the
    # check's CREATION, which for a label/bot gate is PR-LIFETIME status-gating latency, not CI
    # wall-clock — and `_pole_caps` cannot de-inflate a span it has no sampled job p50 for, so an
    # uncapped 8-day label span would otherwise crown `critical_path_check` /
    # `chain_summary.makespan_p50_s` over file-backed poles tracing <1% of it (electron/electron).
    # The ranking, the populations, the chain facts, and the makespan ALL re-derive from
    # `pr_check_p50`, so dropping the fileless set here removes it from every crowning input at
    # once. It is not discarded: stamped in `fileless_status_checks` below so the renderer discloses
    # the slowest one as PR-lifetime status-gating latency. A triage-skipped BUT file-backed check
    # (scanned-graph mapped) stays in the basis — it is real CI compute the crown-recovery pass can
    # still recover, not a fileless gate.
    pr_check_p50, _fileless_check_p50 = _partition_fileless_checks(
        pr_check_p50, check_timing_source, crit_by_wf,
        findings_doc.get("workflow_job_graph"))
    if _fileless_check_p50:
        logger.info(
            "critical path: excluded %d fileless/managed check(s) from the headline/makespan "
            "basis (PR-lifetime status-gating latency, not CI compute): %s",
            len(_fileless_check_p50), ", ".join(sorted(_fileless_check_p50)[:8]))
    # Config-era PER-PR spine door (issue #80). #66/#68 scoped the spine RUNS and #69 the enumerated
    # CHECK SET, but the PER-PR SAMPLE that feeds the chain facts / empirical makespan / populations /
    # presence denominators was still the raw sample — filtered only by check NAME. A check name
    # survives a config change, so a DROPPED-era PR's `test` interval flowed into
    # `chain_summary.makespan_p50_s` (the "typical PR waits N" headline + the #24 physical-bound cap)
    # under a disclosure claiming the KEPT era. Scope the sample to the kept side, surgically per
    # straddling workflow (a dropped-side PR loses only that workflow's checks; a row with no
    # gate-bearing check left drops whole). Runs AFTER the thin-flip resolved the facts (issue #74),
    # so an emptied kept side already flipped to post_only_thin and its post PRs are kept here — never
    # an empty-spine render under a pre claim. Pure no-op (original objects) when nothing straddled.
    _era_repr_shas, _era_per_sha_checks, _era_sha_intervals, _era_dropped_pr_count = (
        _era_scope_pr_spine_sample(
            repr_shas, per_sha_checks, sha_check_intervals, _rep_ts,
            set(pr_check_p50), config_era_facts, _era_check_wf_of))
    # One caps source for the ranking, the populations, AND the chain facts —
    # they must never disagree about a check's de-inflated magnitude (ENG-1 N1).
    _span_caps = _pole_caps(job_p50_all, job_bimodal_all)
    pr_checks_tuple, check_present, present_n_pr, check_pole_freq = _rank_spine_present_first(
        pr_check_p50, _era_per_sha_checks, req_names, _span_caps)
    # Headline-crown recovery. The crowned `critical_path_check` (pr_checks_tuple[0]) is what the
    # report HEADLINES as the merge gate. When it maps to a workflow that run-list triage skipped
    # (jobs never fetched), the headline pole has no sampled job to drill and dead-ends ("no
    # captured log" / "NO CATALOG PATTERN MATCHED") — a contradiction with the triage disclosure
    # (`_crown_recovery_wf`). The relative-recovery pass above references the rare-giant gate p50,
    # so a fast-lint crown that only becomes the headline because every heavier pole is
    # minority-present slips under it. Recover the crown ITSELF: job-fetch its workflow so the
    # headline is drillable and drops out of `triaged_fast_workflows`. Bounded, single-shot (one
    # workflow); no re-rank (the crown's job p50 ~= its check-run p50, so its rank is unchanged —
    # only its per-step drill was missing). No-op on a healthy repo (crown never triaged).
    _crown = pr_checks_tuple[0][0] if pr_checks_tuple else None
    _crown_wf = _crown_recovery_wf(
        _crown, triaged_fast_workflows,
        findings_doc.get("workflow_job_graph"), triage_recover_stash)
    if _crown_wf is not None:
        _cr_runs = triage_recover_stash.get(_crown_wf) or []
        _cr_shallow = min(shallow_runs, len(_cr_runs))
        _cr_jpr: list[list[dict[str, Any]]] = []
        _cr_jbe: dict[str, list[list[dict[str, Any]]]] = {}
        _cr_kept, _cr_fail = _gather_run_jobs(client, repo, _cr_runs[:_cr_shallow],
                                              memo=job_memo)
        jobs_fetch_failures += _cr_fail
        _add_cost_spine_fetch_failures(_crown_wf, _cr_fail)
        _cr_nr, _cr_nj = _accumulate_jobs(_cr_kept, _cr_jpr, _cr_jbe)
        runs_sampled += _cr_nr
        jobs_sampled += _cr_nj
        _cr_crit, _cr_crit_runs = _crit_for(_cr_jpr, _cr_jbe)
        if (_cr_crit.get("long_pole_p50") or 0.0) > 0:
            # Usable job timing came back — but the crown is only truly RECOVERED once its spine
            # check refreshes to `workflow_jobs`. Un-triaging without that refresh leaves the
            # headline STILL undrillable (`_decompose_pole` withholds the step drill on a stale
            # `pr_check_runs` source → `job_timing_unavailable`) AND silences the loud
            # `_crown_triaged_offender` invariant (which keys only on triaged-set membership). So
            # compute the refresh FIRST — tentatively installing the real crit so the mapping
            # probe can see the crown's jobs — and only un-triage / mark recovered if it lands.
            # If the timing exists but the crown check has no DEVELOPER-scoped job (e.g. every
            # recovered run fired on push/schedule, so `_crit_for` scoped to `all-events` and
            # `require_developer_timing` rejects it), restore the stub and leave the crown TRIAGED
            # so the invariant fires — mirroring the sibling relative-recovery pass above, which
            # likewise refuses to false-recover on unusable data.
            _cr_prev = crit_by_wf.get(_crown_wf)
            crit_by_wf[_crown_wf] = _cr_crit          # tentative — needed for the mapping probe
            _cr_map = _map_check_to_job(
                _crown, crit_by_wf, require_developer_timing=True)
            _cr_jp = None
            if _cr_map is not None:
                _cr_wf2, _cr_job2 = _cr_map
                _cr_jp = (crit_by_wf.get(_cr_wf2, {}).get("job_p50") or {}).get(_cr_job2)
            if _cr_jp is not None:
                # Refresh landed: real job timing replaces the triaged stub, the crown's spine
                # flips to `workflow_jobs`, and it drops out of `triaged_fast_workflows`.
                jobs_per_run_by_wf[_crown_wf] = _cr_crit_runs
                events_jobs_by_wf[_crown_wf] = _cr_jbe
                # Deliberately NOT stashed into `deepen_stash`: the deepen pass already ran above,
                # so a stash here could never deepen the crown but WOULD inflate the
                # `shallow_capped`/`capped_workflows`/`shallow_remaining_workflows` provenance —
                # mislabeling this single-shot recovery as a workflow the adaptive cap left
                # un-deepened. The crown's valid shallow p50 lives in `crit_by_wf` /
                # `jobs_per_run_by_wf`, not the stash, so nothing is lost by skipping it.
                triaged_fast_workflows.remove(_crown_wf)
                recovered_workflows.append(_crown_wf)
                pr_check_p50[_crown] = round(_cr_jp, 1)
                check_timing_source[_crown] = "workflow_jobs"
                job_p50_all[_cr_job2] = max(job_p50_all.get(_cr_job2, 0.0), _cr_jp)
                for _bn, _bi in (crit_by_wf.get(_cr_wf2, {}).get(
                        "job_bimodal") or {}).items():
                    job_bimodal_all.setdefault(_bn, _bi)
                if _cr_job2 in job_bimodal_all and _crown not in job_bimodal_all:
                    job_bimodal_all[_crown] = job_bimodal_all[_cr_job2]
                logger.info("triage: recovered headline-crown workflow %r (check %r) so the "
                            "headline pole is drillable rather than dead-ending on a triaged "
                            "fast workflow", _crown_wf, _crown)
            else:
                # Timing exists but no developer-scoped job to refresh the crown check to: restore
                # the triaged stub and keep it triaged (loud on purpose — see the block comment).
                if _cr_prev is not None:
                    crit_by_wf[_crown_wf] = _cr_prev
                else:
                    crit_by_wf.pop(_crown_wf, None)
                logger.warning("triage: headline-crown recovery fetch for %r produced job timing "
                               "but no developer-scoped timing for crown check %r (recovered runs "
                               "likely all push/schedule); keeping it triaged so the undrillable "
                               "headline stays a flagged dead-end rather than a false recovery",
                               _crown_wf, _crown)
        else:
            # Total fetch failure (every recovered run's job fetch errored / came back empty): no
            # usable timing at all. Leave the crown triaged, mirroring the relative-recovery pass
            # above ("recovery fetch ... yielded no job timing; keeping it triaged").
            logger.warning("triage: headline-crown recovery fetch for %r yielded no job timing; "
                           "keeping it triaged rather than false-recovering with empty data",
                           _crown_wf)
    # M2 — segment into PR populations when the merge gate is bimodal (a top
    # check that self-skips on some PRs, e.g. changed-tests ~65s on docs PRs vs
    # ~750s on code PRs). Each population gets its own critical path, so a check
    # that's only the pole when the gate self-skips earns its real wall-clock for
    # that population instead of being zeroed by a gate it never runs alongside.
    pr_check_populations = _segment_pr_populations(
        _era_per_sha_checks, pr_check_p50, job_p50_all, job_bimodal_all)
    # Map each spine check to its producing workflow file (when it maps to a sampled job),
    # emitted per check so the renderer can tell a file-backed check from a fileless/external
    # one (a Socket/CLA bot) WITHOUT relying on whether the check happened to be drilled as a
    # pole — a rare file check below the top poles would otherwise be mislabeled "external".
    _check_wf: dict[str, str] = {}
    _jg = findings_doc.get("workflow_job_graph") or {}
    for _n, _ in pr_checks_tuple:
        _m = _map_check_to_job(_n, crit_by_wf, require_developer_timing=True)
        if _m:
            _check_wf[_n] = _m[0]
        elif _jg:
            # The timing mapper misses a check whose workflow was triage-skipped (jobs not
            # fetched). Fall back to the SCANNED job graph so a gate matrix check stays tied
            # to its editable workflow file instead of being mislabeled fileless/external.
            _node = _check_to_job_node_scanned(_n, _jg)
            if _node is not None:
                _check_wf[_n] = _node[0]
                logger.debug("critical path: check %r mapped to %s via scanned job "
                             "graph (timing mapper missed it — triage-skipped workflow)",
                             _n, _node[0])
            else:
                # None here is either a genuinely fileless bot OR a cross-workflow
                # ambiguity bail; both leave the check unmapped (rendered external), so
                # trace it — an ambiguous-internal check looks identical to a real bot.
                logger.debug("critical path: check %r left unmapped (no scanned job-graph "
                             "match, or ambiguous across workflows) — renders external", _n)
    # ENG-1 PR-N1: per-PR chain TIMING facts (data-only; nothing renders or
    # ranks on them until PR-N2). One entry per sampled repr sha — the longest
    # `needs:` chain (capped member spans, `_span_caps` — the caps the ranking
    # uses) plus the attempt-scoped, span-capped empirical makespan cross-check.
    _chain_facts = _stamp_chain_facts(
        _era_repr_shas, _era_per_sha_checks, set(pr_check_p50), _span_caps, _jg,
        crit_by_wf, _era_sha_intervals)
    _chain_summary_val = _chain_summary(_chain_facts)
    # ENG-1 PR-N3: an OPT21 (unnecessary `needs:`) finding whose workflow hosts
    # the measured gate chain now cites that fact — the edge it questions IS
    # the serialization the headline measures (evidence annotation only; OPT21
    # keeps its modeled sizing, follow-up trigger in the plan). Named helper so
    # the path is directly red/green-testable (the e2e corpus keeps OPT21
    # deliberately quiet, so it can never exercise this).
    _cite_chain_in_opt21_evidence(findings, _chain_summary_val, _jg, crit_by_wf)
    # ENG-1 PR-N3: the chain fields the sizing cascade consumes (empty/zero on
    # chainless repos — the cascade then behaves exactly as before).
    _cs_modal = [str(m) for m in ((_chain_summary_val or {}).get("modal_chain") or [])]
    _chain_ctx_members = frozenset(_cs_modal) if len(_cs_modal) >= 2 else frozenset()
    _chain_ctx_p50 = (float((_chain_summary_val or {}).get("chain_p50_s") or 0.0)
                      if _chain_ctx_members else 0.0)
    _chain_ctx_win = (float((_chain_summary_val or {}).get("chain_win_p50_s") or 0.0)
                      if _chain_ctx_members else 0.0)
    findings_doc["pr_critical_path"] = {
        # KEPT-side count post the #80 spine door: `sampled_pr_count` is the number of PRs whose
        # timing actually feeds the rendered chain/makespan/populations/presence — a dropped-era PR
        # removed by the door is NOT counted here (its timing describes the other configuration).
        "sampled_pr_count": len(_era_repr_shas),
        # #80: how many sampled PR rows the config-era spine door removed as dropped-era (their gate
        # checks all belonged to a workflow they ran the OTHER config of). Stamped only on a straddle
        # that dropped >=1 PR, so the "measured from N/20 sampled PRs" caveat can disclose the drop
        # instead of silently shrinking the denominator. Omitted (byte-identity) when nothing dropped.
        **({"era_dropped_pr_count": _era_dropped_pr_count} if _era_dropped_pr_count else {}),
        # Config-era facts (issue #66): per-workflow record of every sample that
        # straddled its last-change commit — boundary, kept era, rule, pre/post counts.
        # Empty when nothing straddled. blocking_path renders the disclosure off this;
        # verify_report.check_config_era_boundary re-derives that no drilled pole bound
        # to a `kept_era == "pre"` workflow ships without the era disclosure.
        "config_eras": config_era_facts,
        # Sampling honesty: how many PRs we ASKED for vs got, whether any
        # check-runs fetch failed, how deep the walk went (sample_fetched —
        # far above sampled_pr_count means the sample was drawn from a deeper,
        # older window), and whether the sample was scoped to the full required
        # suite or fell back to recency only (required set unreadable). The report
        # surfaces a caveat when the sample is short, deep, or recency-only so a
        # degraded critical path is never presented as a complete one. Mapped via
        # the pure helper so the diag -> doc key-mapping is unit-testable.
        **_sampling_provenance_fields(sample_diag),
        "critical_path_check": pr_checks_tuple[0][0] if pr_checks_tuple else None,
        "critical_path_s": pr_checks_tuple[0][1] if pr_checks_tuple else 0.0,
        # Per-PR presence behind the rare-pole demotion (denominator = PRs that ran >=1
        # tracked check). Emitted so the renderer's typical/rare split is robust even when
        # M2 didn't emit `populations`, and so a reader can see how often each check ran.
        "check_present_n_pr": present_n_pr,
        "checks": [
            {"name": n, "p50_s": p, "present_on": check_present.get(n, 0),
             # pole_n = on how many sampled PRs this check was the ACTUAL critical path (slowest
             # job). The recurrence signal behind the headline pick + the typical/rare split; the
             # renderer reads it so its demotion agrees with `critical_path_check`, and
             # verify_report re-derives it from `populations` to catch a phantom gate.
             "pole_n": check_pole_freq.get(n, 0),
             **({"workflow_file": _check_wf[n]} if n in _check_wf else {}),
             **({"timing_source": check_timing_source[n]}
                if n in check_timing_source else {}),
             **({"bimodal": job_bimodal_all[n]} if n in job_bimodal_all else {})}
            for n, p in pr_checks_tuple],
        # When a check is bimodal across PRs, wall-clock is sized as the EXPECTED
        # value over the per-PR critical paths (one population per sampled PR), so
        # a check that's the pole on only some PRs is credited proportionally.
        "population_weighted": bool(pr_check_populations),
        "populations_n": len(pr_check_populations),
        # ENG-1 PR-N1: the per-PR chain timing facts (see the block above).
        # Persisted with per-member spans because `populations` is bimodal-only
        # (empty on non-bimodal repos) — without them no chain sum could be
        # re-derived from a committed artifact.
        "chain_facts": _chain_facts,
        # ENG-1 PR-N2: the render-consumable reduction (p50 chain wait, modal
        # chain, competing-path bound, signed divergence) — the verifier
        # re-derives it from `chain_facts`.
        "chain_summary": _chain_summary_val,
        # Per-population check sets (one population per sampled PR that ran >=1
        # tracked check; share = 1/m). Persisted so the report's "Long poles"
        # section can show how OFTEN each check is the actual pole across sampled
        # PRs (e.g. mastra `Lint` gates 8/16, `changed-tests` only 3/16) rather
        # than crowning the aggregate critical path's biggest-but-rare check.
        # Empty when the gate isn't bimodal (the aggregate is representative).
        "populations": [
            [share, [[n, p] for n, p in checks]]
            for share, checks in pr_check_populations
        ],
        # Checks excluded from the critical path as non-PR-gating (push/schedule-only
        # workflows whose check-run rode along on a sampled SHA) - recorded so the
        # exclusion is visible, never a silent drop.
        "dropped_non_pr_checks": _dropped_non_pr,
        # Checks excluded as non-required (neither a required check nor a leg feeding a
        # required rollup) when a complete, non-empty required set resolved - recorded
        # so the merge-scope narrowing is visible, never a silent drop. Empty when the
        # required set was unreadable/partial/unsatisfiable (the filter stayed inert).
        "dropped_non_required_checks": dropped_non_required,
        # True ONLY when `_scope_spine_to_required` actually narrowed the spine to
        # required-reachable checks (complete required set + job graph + a file-backed required
        # anchor). The renderer keys its "required · path-conditional" relabel of a demoted
        # minority leg off THIS, not the sampling-level `required_suite_scoped`: that flag is
        # True on a partial/anchorless read where the spine was left UNSCOPED and still holds
        # non-required checks, where the required framing would be wrong.
        "spine_required_scoped": spine_required_scoped,
        # Issue #12: fileless/managed status checks (bot gates, label gates, external app checks —
        # no sampled workflow job) EXCLUDED from the crowning basis above, kept here so the renderer
        # can DISCLOSE the slowest one instead of silently dropping it. `span_s` is the raw
        # `pr_check_runs` span (PR-lifetime status-gating latency, NOT CI compute); the `basis`
        # label names that framing so no reader mistakes it for a CI wall-clock. Sorted slowest
        # first. Empty on a repo with no fileless gating checks.
        "fileless_status_checks": [
            {"name": n, "span_s": round(s, 1),
             "basis": "pr_lifetime_status_gating_latency"}
            for n, s in sorted(_fileless_check_p50.items(),
                               key=lambda kv: (-kv[1], kv[0]))
        ],
        # True ONLY in the degenerate case where EVERY tracked check was fileless — there is no
        # job-groundable check left to crown, so the renderer says so rather than crowning a
        # status-gating span. (`pr_check_p50` is the post-exclusion crowning basis.)
        "all_checks_fileless": bool(_fileless_check_p50 and not pr_check_p50),
    }
    # Per-pole step decomposition — WHY each long pole is slow. For each top
    # critical-path check, map it to its producing job and break that job into
    # its per-step waterfall (`_decompose_job_steps`), recording the dominant
    # (addressable) step. Unconditional: unlike the structural router this does
    # NOT skip a check already covered by a hygiene finding, so EVERY top pole
    # (e.g. `Lint`) carries its "why", not just the structural-routed ones. The
    # report's "long poles" section reads this directly — decoupled from findings.
    poles: list[dict[str, Any]] = []
    import blocking_path as bp  # same-skill module; _same_matrix lives there

    def _decompose_pole(check_name: str, check_p50: float,
                        mapping: tuple[str, str] | None = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"check": check_name, "p50_s": round(check_p50, 1)}
        timing_source = check_timing_source.get(check_name, "workflow_jobs")
        entry["timing_source"] = timing_source
        # `mapping` lets a caller pin the (workflow_file, job) directly — used by the
        # PR-floor fallback, whose "check" IS a job name and must bind to ITS workflow,
        # not whichever workflow `_map_check_to_job` happens to resolve a same-named job in.
        mapping = _pole_mapping(check_name, crit_by_wf, mapping,
                                findings_doc.get("workflow_job_graph"),
                                require_developer_timing=True)
        if mapping is not None:
            wf_path, job_name = mapping
            entry["workflow_file"] = wf_path
            entry["job"] = job_name
            if timing_source != "workflow_jobs":
                entry["job_timing_unavailable"] = (
                    "PR check-run timing was measured, but no sampled workflow job "
                    "for this check ran on a developer-facing event; step drill is "
                    "withheld rather than borrowing push/schedule job timings.")
                return entry
            if check_name in job_bimodal_all:
                entry["bimodal"] = job_bimodal_all[check_name]
            job_inst = [j for run in jobs_per_run_by_wf.get(wf_path, [])
                        for j in run if str(j.get("name", "")) == job_name]
            # Same slow-mode decomposition as the structural router: a bimodal
            # pole's "why it's slow" headline must match the slow-mode run the
            # report drills below it, not a fast/slow-blended p50.
            decomp = _decompose_job_steps(
                job_inst, bimodal=job_bimodal_all.get(check_name))
            if decomp is not None:
                entry["dominant_step"] = decomp["dominant_step"]
                entry["dominant_category"] = decomp["dominant_category"]
                entry["dominant_p50_s"] = decomp["dominant_p50"]
                entry["dominant_share"] = decomp["dominant_share"]
                entry["job_p50_s"] = decomp["job_p50"]
                entry["steps"] = [{"step": n, "category": c, "p50_s": round(p, 1)}
                                  for n, c, p in decomp["steps"]]
        else:
            # Job-backed but file-AMBIGUOUS (issue #118): the check maps to no single
            # workflow file, yet it is NOT fileless/external — MORE THAN ONE workflow
            # produces it under the same check name (matrix legs colliding: reth's
            # `test / ethereum` is produced by BOTH unit.yml and integration.yml). The
            # deliberate cross-workflow refusal (issue #59) still withholds the per-file
            # step drill, but the check is a REAL CI job worth investigating. Stamp the
            # candidate workflow set so the summary frames it as ambiguous — not as a
            # "don't investigate" fileless gate — and can NAME the workflows. The stamp is a
            # re-derivable data fact (recomputable from `per_workflow_timing` via
            # `_check_producing_workflows`), though no verify check re-derives it today. Only
            # stamp when >1 workflow genuinely produces it (a single producer would have
            # resolved above; zero producers is a real fileless check).
            producing = _check_producing_workflows(
                check_name, crit_by_wf, require_developer_timing=True)
            if len(producing) > 1:
                entry["ambiguous_workflows"] = sorted(producing)
        return entry

    for check_name, check_p50 in _structural_pole_candidates(
            pr_checks_tuple, crit_by_wf, findings_doc.get("workflow_job_graph"),
            triaged_fast_workflows, _STRUCTURAL_TOP_N):
        poles.append(_decompose_pole(check_name, check_p50))
    # The report drills _DRILL_DISTINCT_MATRICES distinct job MATRICES (matrix legs that
    # share one fix collapse to one). Beyond the gate (the slowest by median), the next
    # matrix is chosen by IMPACT = max(median, bimodal slow mode) - so a bimodal lever
    # (a job cheap on the typical PR but a long gate on a large share, e.g. a `test`
    # job) is drilled as the 2nd finding instead of a higher-median but trivial check.
    # Those gates can rank below _STRUCTURAL_TOP_N by median or hide under a big matrix
    # in ONE workflow, so decompose them here even if absent from the top-N.
    def _impact(check: str, p50: float) -> float:
        bi = job_bimodal_all.get(check) or {}
        return max(p50, float(bi.get("high_p50_s") or 0.0))

    matrices: list[tuple[str, float]] = []
    for check_name, check_p50 in pr_checks_tuple:
        _cm = _map_check_to_job(
            check_name, crit_by_wf, require_developer_timing=True)
        if _cm is None:
            continue  # fileless / unmapped (e.g. an AI review bot) - nothing to drill
        # Disambiguate by workflow file: a `Python 3.13` job in framework-test.yml is a
        # DISTINCT matrix from a `Python 3.13` job in datasets-test.yml, even though
        # GitHub gives them identical check-run names. Without the workflow key they
        # collapse and only one is drilled, mis-binding the other's steps to a sibling.
        if any(bp._same_matrix(check_name, m, _cm[0],
                               (_map_check_to_job(
                                   m, crit_by_wf,
                                   require_developer_timing=True) or ("", ""))[0])
               for m, _ in matrices):
            continue
        matrices.append((check_name, check_p50))
    drill_poles: list[dict[str, Any]] = []
    if matrices:
        # Mirror the renderer's typical-first pole tiering so the captured (drilled) set
        # equals the rendered set — otherwise a rare-but-slow pole gets drilled here but
        # demoted in the report, leaving the rendered pole logless and mis-bound to a
        # sibling's bundle by `_match_key`'s exact-workflow-stem rule. `_is_typical_check`
        # uses the SAME pole-FREQUENCY demotion (`check_pole_freq >= _POLE_RECUR_FLOOR`), the
        # `_RARE_PRESENCE_MIN_PR` sample-size floor, and the required-check exemption as
        # `_rank_spine_present_first` and the renderer's `_typical_check` — so the drilled
        # (captured) pole set can never disagree with `critical_path_check`.
        def _is_typical_check(check: str) -> bool:
            if present_n_pr < _RARE_PRESENCE_MIN_PR or check in req_names:
                return True
            return check_pole_freq.get(check, 0) >= _POLE_RECUR_FLOOR
        ordered = _order_drill_matrices(matrices, _impact, _is_typical_check)
        for check_name, check_p50 in ordered[:_DRILL_DISTINCT_MATRICES]:
            existing = next((p for p in poles if p.get("check") == check_name), None)
            if existing is None:
                existing = _decompose_pole(check_name, check_p50)
                poles.append(existing)
            drill_poles.append(existing)   # the exact poles the renderer drills + we log
    # ENG-1 PR-N3 (the N2 as-built record): on a chain-gated repo the gate IS
    # the chain — its members drill first, in chain order, then the rest in
    # their existing span order. `critical_path_check` keeps its
    # slowest-single-check semantics (recorded in the plan).
    if _chain_ctx_members:
        _by_check = {str(p.get("check", "")): p for p in poles}
        _member_poles = [_by_check[m] for m in _cs_modal if m in _by_check]
        _member_ids = {id(p) for p in _member_poles}
        poles = _member_poles + [p for p in poles if id(p) not in _member_ids]
    findings_doc["pr_critical_path"]["poles"] = poles

    # --- PR-FLOOR FALLBACK -------------------------------------------------------
    # No file-backed REQUIRED gate to drill. Three ways we land here, all rendered as a
    # clearly-demoted PR-FLOOR spine (the file-backed work a normal PR runs) — never as
    # the branch-protection gate:
    #   (1) The required suite is external/managed (a CLA bot, enterprise CI, label-
    #       gated full-e2e, a mergeability gate) and ran on NO sampled PR, so the
    #       sampler promoted a recency-only sample (`required_suite_unsatisfiable`).
    #       `poles` then ALREADY holds the file-backed PR-floor (the external gate
    #       isn't among the sampled checks), so just flag + demote it in place. Keying
    #       off the SAMPLER signal — not "are the poles file-backed" — is essential:
    #       the promoted sample normally DOES yield file-backed poles, so that test
    #       would be false here and the report would wrongly render the merge-gate
    #       framing for the exact case this fallback exists for.
    #   (1b) The required suite is all external/managed but WAS observable — it ran on the
    #       sampled PRs (so `required_suite_unsatisfiable` is False and (1) misses it), yet
    #       not one required check anchors a workflow file here (a Socket-Security-only
    #       repo). `_scope_spine_to_required` stayed inert (no file-backed required anchor),
    #       so without this the file-backed poles (Test/Lint) would headline "why is the
    #       merge slow?" though they gate zero merges. Same all-external test the scoper
    #       uses, so the two never disagree — flag + demote in place exactly like (1).
    #   (2) There are no file-backed poles at all (the gates are all fileless/managed,
    #       or the sample produced none) — synthesize the floor from per-workflow
    #       timing so we render a spine instead of dead-ending.
    cp = findings_doc["pr_critical_path"]
    unsatisfiable = bool(sample_diag.get("required_suite_unsatisfiable"))
    required_all_external = not unsatisfiable and _required_suite_all_external(
        required_checks, req_names, crit_by_wf, findings_doc.get("workflow_job_graph"))
    file_poles = [p for p in poles if p.get("workflow_file") and p.get("job")]
    if (unsatisfiable or required_all_external) and file_poles:
        cp["gate_kind"] = "pr_floor_fallback"
        # Collapse concurrency-dominated within-workflow siblings before flagging.
        # A normal PR runs a workflow's jobs concurrently, so only its long pole
        # gates; drilling a slower-finishing-but-not-gating sibling as a co-equal
        # long pole would contradict its own OPT24 "a longer concurrent job gates
        # the run, saves ~0 wall-clock" sizing. Mirrors case (2)'s
        # one-pole-per-workflow floor (`_select_pr_floor_workflows`).
        collapsed = _collapse_pr_floor_siblings(poles)
        cp["poles"] = collapsed
        _kept_ids = {id(p) for p in collapsed}
        drill_poles = [p for p in drill_poles if id(p) in _kept_ids]
        file_poles = [p for p in collapsed if p.get("workflow_file") and p.get("job")]
        for p in file_poles:
            p["pr_floor_fallback"] = True
    elif not file_poles:
        floor = _select_pr_floor_workflows(crit_by_wf, events_by_wf)
        fb_poles: list[dict[str, Any]] = []
        for lp_p50, wf_path, lp_job in floor[:_STRUCTURAL_TOP_N]:
            entry = _decompose_pole(lp_job, lp_p50, mapping=(wf_path, lp_job))
            entry["pr_floor_fallback"] = True
            # DISTINCT flag for the genuine PUSH-only floor synthesis (this branch only — a repo with
            # NO file-backed pole at all). Unlike the case-1/1b file poles above (also flagged
            # `pr_floor_fallback` but real, drillable, PR-scoped structural poles), THIS pole is
            # synthesized from `push` timing: it is honestly `all-events`-scoped (no developer event
            # exists) and may carry no per-step breakdown (the job has no sampled steps). verify_report
            # exempts ONLY this flag from the blend + stunted guards — so a case-1/1b structural pole
            # that is genuinely event-blended is still caught, while a push-only floor report is not
            # false-failed into an unsatisfiable gate.
            entry["pr_floor_push_fallback"] = True
            fb_poles.append(entry)
        if fb_poles:
            cp["poles"] = fb_poles
            cp["gate_kind"] = "pr_floor_fallback"
            # Populate `checks` so the renderer's Level-1 concurrent-floor chart and
            # headline read the PR-floor jobs (they run in parallel on a normal PR).
            cp["checks"] = [
                {"name": p["check"], "p50_s": p["p50_s"],
                 **({"bimodal": p["bimodal"]} if p.get("bimodal") else {})}
                for p in fb_poles]
            cp["critical_path_check"] = fb_poles[0]["check"]
            cp["critical_path_s"] = fb_poles[0]["p50_s"]
            drill_poles = fb_poles[:_DRILL_DISTINCT_MATRICES]
            logger.info(
                "critical path: no file-backed required gate — falling back to the "
                "measured PR-floor (%d workflows; top pole %s @ %.0fs)",
                len(fb_poles), fb_poles[0]["check"], fb_poles[0]["p50_s"])
    # provenance — the cross-repo handshake with the ci-harness auto-fixer (consumed via
    # its map-cioptimize-findings.sh). Additive, non-breaking. The 3-way decision lives in
    # the pure `_pole_provenance` helper above; `required_scoped` also requires the HEADLINED
    # pole itself be required-reachable (not a kept-but-unpinnable check), so a narrowed
    # spine whose pole isn't confirmed merge-blocking falls through to `unresolved` (HALT).
    # Timing-provenance stamp (issue #74 guard leg): after the thin-flip re-drills a post_only_thin
    # workflow off its POST runs, each pole's drilled runs are one era — stamp the earliest so the
    # disclosure-matches-enumeration guard can FAIL a pre-era pole under a post-claiming disclosure.
    _stamp_pole_repr_run_era(cp.get("poles") or [], config_era_facts, jobs_per_run_by_wf)
    cp["provenance"] = _pole_provenance(
        cp.get("gate_kind"), spine_required_scoped,
        _pole_is_required_reachable(
            cp.get("critical_path_check"), req_names,
            findings_doc.get("workflow_job_graph"), crit_by_wf))
    # Run-frequency is a CORE input: a wall-clock saving on a workflow that
    # rarely runs removes little developer wait, no matter how big per-run. Share
    # = this workflow's 30d volume / the busiest PR-triggered workflow's volume.
    pr_vols = [v for w, v in vol_by_wf.items()
               if isinstance(v, int)
               and (events_by_wf.get(w) or set()) & _PR_VOLUME_EVENTS
               and _volume_is_ci_clean(events_by_wf.get(w))]
    max_pr_vol = max(pr_vols, default=0)

    # Data-driven detection over the sampled runs. The catalog body's
    # detection heuristic for each pattern drives the logic verbatim.
    #
    # LEVER 1 (second half). The detector loop opens with the SAME serial run-list
    # problem the sampling loop had: per workflow it issues the OPT48 fail/success
    # count probes, one all-status run list, an optional event-scoped volume (OPT65),
    # an optional event-scoped all-status list (OPT57), and the schedule block's three
    # lists — one workflow at a time. Every one is a `wf_id`-keyed read with no
    # cross-workflow ordering, so plan them all up front and fetch them in ONE wave.
    #
    # The plan must issue EXACTLY the calls the loop below issues — no more (a parked
    # response nobody consumes is a gh call the serial path never made) and no fewer
    # (an unplanned one just falls through to a live fetch, which is merely slow). So
    # each entry mirrors its call site's guard, and the shared predicates
    # (`_opt65_scope_event`, `_opt57_timeout_job_specs`, `_on_has_event`) are the very
    # ones the loop re-evaluates — they cannot drift apart. `prefetch_json` dedups, and
    # consumption is pop-once, so a workflow whose event scope IS `schedule` (its OPT57
    # list and its schedule list being the same endpoint) still issues exactly the two
    # calls it does today.
    def _detector_run_list_plan() -> list[str]:
        out: list[str] = []
        for wf_path, jobs_per_run in jobs_per_run_by_wf.items():
            if not jobs_per_run:
                continue
            wf_id = wf_by_path.get(wf_path, {}).get("id")
            crit = crit_by_wf[wf_path]
            wf_doc = _wf_docs.get(wf_path, {})
            monthly = vol_by_wf.get(wf_path)
            scope_event = _opt65_scope_event(
                crit, observed_events=events_by_wf.get(wf_path), wf_doc=wf_doc)
            if wf_id is None:
                continue
            if scope_event:                                    # OPT65 scoped volume
                out.append(_volume_endpoint(repo, wf_id, created_before,
                                            event=scope_event))
            if monthly is not None and monthly >= 100:         # OPT48's two probes
                out.append(_status_count_endpoint(repo, wf_id, "failure", created_before))
                out.append(_status_count_endpoint(repo, wf_id, "success", created_before))
            # NOTE (#213 reconciliation): the UN-scoped all-status page is NOT prefetched
            # here. Post-merge the run-elimination detectors read the page `_all_status_page`
            # already fetched and CACHED in the run-list loop above (`all_status_runs_by_wf`)
            # — they no longer re-fetch it — so prefetching `runs?per_page=_COST_RUNLIST_MAX`
            # in this plan would park a page every detector reads from the cache instead,
            # i.e. a guaranteed unconsumed prefetch. The event-SCOPED pages below are still
            # fetched fresh (they are not cached by `_all_status_page`), so they stay.
            event_scope = str(crit.get("event_scope") or "")
            if (_opt57_timeout_job_specs(wf_doc)
                    and event_scope and event_scope != "all-events"):
                out.append(_run_list_endpoint(repo, wf_id, _COST_RUNLIST_MAX,
                                              created_before, event=event_scope))
            if _on_has_event(_wf_on(wf_doc), "schedule"):      # the schedule block
                out.append(_volume_endpoint(repo, wf_id, created_before,
                                            event="schedule"))
                out.append(_run_list_endpoint(repo, wf_id, _COST_RUNLIST_MAX,
                                              created_before, event="schedule"))
                out.append(_run_list_endpoint(repo, wf_id, max_runs, created_before,
                                              status="success", event="schedule"))
        return out

    _prefetch_json(client, _detector_run_list_plan())

    next_id = max((int(f["id"][1:]) for f in findings if f.get("id", "").startswith("f") and f["id"][1:].isdigit()), default=0)
    for wf_path, jobs_per_run in jobs_per_run_by_wf.items():
        if not jobs_per_run:
            continue
        wf = wf_by_path.get(wf_path, {})
        wf_id = wf.get("id")
        crit = crit_by_wf[wf_path]
        long_pole = crit.get("long_pole_p50", 0.0)
        is_pr = wf_path in _pr_workflows
        monthly = vol_by_wf.get(wf_path)

        new = _detect_opt24_long_test_no_sharding(
            wf_path, jobs_per_run, next_id, monthly,
            sharded_bases=_sharded_bases(_wf_docs.get(wf_path, {})),
            wf_doc=_wf_docs.get(wf_path, {}))
        next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
        findings.extend(new)

        new = _detect_opt25_shard_imbalance(wf_path, jobs_per_run, next_id, crit)
        next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
        findings.extend(new)

        opt65_monthly = _opt65_monthly_volume_for_scope(
            client, repo, wf_id, crit, monthly, created_before,
            observed_events=events_by_wf.get(wf_path),
            wf_doc=_wf_docs.get(wf_path, {}))
        new = _detect_opt65_billing_rounding_waste(
            wf_path, jobs_per_run, crit, opt65_monthly, next_id)
        next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
        findings.extend(new)

        # The sibling windows come from the SAME cached all-status page the
        # run-elimination block below fetches (net zero extra gh calls); with
        # no wf_id the page is unfetchable and the attribution stays None.
        new = _detect_opt43_queue_time(
            wf_path, jobs_per_run, next_id, is_pr,
            all_status_runs=(_all_status_page(wf_path, wf_id)
                             if wf_id is not None else None))
        next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
        findings.extend(new)

        if wf_id is not None:
            new = _detect_opt48_failure_rate(client, repo, wf_id, wf_path, long_pole, monthly, next_id, created_before)
            next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
            findings.extend(new)

            # Run-elimination detectors (OPT46 superseded, OPT47 double-trigger)
            # share ONE all-status run-list slice (the success-only sample can't
            # see cancelled/superseded/duplicate runs) — the page the shallow loop
            # already fetched for this workflow. The accessor re-fetches only when the
            # page isn't cached (a workflow that never passed through that loop, or one
            # whose fetch FAILED there — a failure is never cached, so this is a real
            # retry, not a cache hit on a poisoned empty).
            wf_doc = _wf_docs.get(wf_path, {})
            all_runs = _all_status_page(wf_path, wf_id)
            failure_run_jobs: list[list[dict[str, Any]]] | None = None
            # `all_runs is None` = the page could NOT be fetched (not "no runs"). Every
            # detector keyed on it below is a RUN-ELIMINATION detector whose entire
            # basis IS this page and whose `sample_denominator` is its length. Handing
            # them a laundered `[]` would report each one CLEAN over a "0 of 0 runs"
            # evidence line — a transient gh timeout rendered as an all-clear, on a
            # report that otherwise looks healthy. So each is SKIPPED (its absence is
            # honest) while the detectors that don't need the page (OPT36) still run.
            #
            # SKIPPING IS NOT ENOUGH. A finding that doesn't appear reads exactly like a
            # finding that found nothing — so the skip is recorded by workflow and by
            # detector (`detectors_skipped`), stamped into `data_sources`, and NAMED in
            # the report. `client.errors` alone can't carry this: it renders as "a few
            # runs/jobs are absent, the P50s are over marginally fewer runs", which is
            # FALSE here (no P50 is affected) and describes a different failure.
            if all_runs is None:
                # Name only the detectors that WOULD have been dispatched for this
                # workflow: OPT35 and OPT57 are additionally gated on the workflow's own
                # YAML (which we DO have), so a workflow with no shard/timeout job specs
                # was never going to run them and must not be reported as missing them.
                # (OPT57's own skip, incl. its event-scoped page, is recorded below.)
                _skipped = ["OPT46", "OPT47", "OPT64"]
                if _opt35_shard_job_specs(wf_doc):
                    _skipped.insert(0, "OPT35")
                _skip_detectors(wf_path, _skipped,
                                "its all-status run list could not be fetched")
                # Also disclosed on the RUN-LIST channel by name: `detectors_skipped`
                # says what was not evaluated; `run_list_fetch_failures` says which
                # fetch failed — the coverage note renders both.
                _note_run_list_gap(wf_path, "all-status run list")
                logger.warning(
                    "%s: all-status run page unavailable — skipping the run-elimination "
                    "detectors (OPT35/46/47/64, and OPT57 where it is scoped to that "
                    "page) rather than reporting them clean off an empty sample",
                    wf_path)
            # Retry-tax substrate: per-workflow PR-event attempt counts from the
            # all-status slice (structured counts, never parsed prose).
            # Prior-attempt minutes are added below only when the
            # attempt jobs were actually fetched; the counts alone are cheap
            # and always accumulated. Aggregated over the GATING workflows and
            # stamped as data_sources.attempt_stats at the end of collect().
            if all_runs is not None:
                _pr_runs = [r for r in all_runs
                            if str(r.get("event")) in ("pull_request", "pull_request_target")]
                attempt_stats_by_wf[wf_path] = {
                    "pr_event_runs": len(_pr_runs),
                    "pr_event_runs_multi_attempt": sum(
                        1 for r in _pr_runs if _run_attempt(r) > 1),
                    "prior_attempt_job_minutes": 0.0,
                }
            attempt_runs = _rerun_attempt_runs(all_runs) if all_runs is not None else []
            if attempt_runs:
                # Prefetch the `filter=all` leg ONLY — never the `filter=latest` leg.
                #
                # ONE fetch per attempt-run: `filter=latest` is DERIVED from the `filter=all`
                # payload's per-job `run_attempt` (see `_latest_attempt_jobs` /
                # `_attempt_job_samples`), not fetched again — so the failure count is added
                # ONCE per run (the old two-fetch path charged an unreachable run to the
                # cost-spine gap twice). A prefetch plan must never be more eager than its
                # consumer: planning a `latest` leg the call site no longer issues would park
                # N responses per workflow that nobody pops — they land in
                # `prefetch_unconsumed`, get drained, and the only symptom is one warning.
                #
                # Nothing is lost by prefetching only `all`: `_gather_run_jobs` fans the
                # `all` leg out over the same shared pool, so it is already parallel — one
                # more wave, not a serial walk. Page 1 is parked here; a >100-job run
                # paginates live through the governed `_invoke` inside `_paginate`.
                _prefetch_json(client, [_run_jobs_endpoint(repo, r.get("id"), "all")
                                        for r in attempt_runs])
                kept_all, wf_failures = _gather_run_jobs(
                    client, repo, attempt_runs, fetch=_fetch_run_jobs_all_attempts,
                    memo=job_memo)
                attempt_samples, latest_failures = _attempt_job_samples(
                    client, repo, kept_all, memo=job_memo)
                wf_failures += latest_failures
                jobs_fetch_failures += wf_failures
                _add_cost_spine_fetch_failures(wf_path, wf_failures)
                prior_runs: list[list[dict[str, Any]]] = []
                for run, all_jobs, latest_jobs in attempt_samples:
                    prior_jobs = _prior_attempt_jobs(run, all_jobs, latest_jobs)
                    if prior_jobs:
                        _stamp_run_context(run, prior_jobs)
                        prior_runs.append(prior_jobs)
                        if (wf_path in attempt_stats_by_wf
                                and str(run.get("event")) in ("pull_request",
                                                              "pull_request_target")):
                            attempt_stats_by_wf[wf_path]["prior_attempt_job_minutes"] += sum(
                                (_duration_s(j.get("started_at"), j.get("completed_at")) or 0.0)
                                for j in prior_jobs) / 60.0
                if prior_runs:
                    prior_attempt_jobs_by_wf[wf_path] = {
                        "event_scope": "all-events",
                        "sampled_workflow_run_count": len(all_runs),
                        "runs": prior_runs,
                    }
                new = _detect_opt64_rerun_attempt_waste(
                    wf_path, attempt_samples, monthly, len(all_runs), next_id)
                next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
                findings.extend(new)

            if all_runs is not None and _opt35_shard_job_specs(wf_doc):
                failure_run_jobs = []
                opt35_runs = _opt35_failed_workflow_runs(all_runs)
                kept, wf_failures = _gather_run_jobs(client, repo, opt35_runs,
                                                     memo=job_memo)
                jobs_fetch_failures += wf_failures
                _accumulate_jobs(kept, failure_run_jobs, {})
                new = _detect_opt35_fail_fast_waste(
                    wf_path, failure_run_jobs, wf_doc, monthly, next_id,
                    sample_denominator=len(all_runs))
                next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
                _supersede_static_opt35(findings, new)
                findings.extend(new)

            event_scope = str(crit.get("event_scope") or "")
            # OPT57's denominator is the event-scoped page when the critical path is
            # scoped to one event, else the all-status page. EITHER can be UNKNOWN (a
            # failed fetch), and an unknown denominator is not zero: sizing OPT57 against
            # `len(None or [])` would report it clean over "0 of 0 runs". Resolve the
            # scope first, and skip the detector when it can't be resolved.
            opt57_scoped_to_all = event_scope in ("", "all-events")
            opt57_scope_runs: list[dict[str, Any]] | None = None
            if _opt57_timeout_job_specs(wf_doc):
                opt57_scope_runs = all_runs if opt57_scoped_to_all else (
                    _all_status_event_runs(client, repo, wf_id, event_scope,
                                           _COST_RUNLIST_MAX, created_before))
            if opt57_scope_runs is None:
                if _opt57_timeout_job_specs(wf_doc):
                    _scope_name = "all-status" if opt57_scoped_to_all else event_scope
                    _skip_detectors(
                        wf_path, ["OPT57"],
                        f"its {_scope_name} run list could not be fetched")
                    if not opt57_scoped_to_all:
                        # Scoped-to-all means OPT57's basis IS the all-status page,
                        # whose failure was already noted above — re-noting it would
                        # disclose ONE failed page as TWO "workflow samples" and
                        # frame a measured workflow as missing. Only an EVENT-scoped
                        # page is a distinct fetch worth its own gap entry.
                        _note_run_list_gap(wf_path, f"{_scope_name} run list (OPT57)")
                    logger.warning(
                        "%s: OPT57's %s run page unavailable — skipping it rather than "
                        "sizing it against zero runs", wf_path, _scope_name)
            else:
                opt57_monthly = opt65_monthly
                if opt57_scoped_to_all and failure_run_jobs is not None:
                    opt57_failure_jobs = failure_run_jobs
                else:
                    opt57_runs = _opt57_failed_workflow_runs(opt57_scope_runs, event_scope)
                    opt57_failure_jobs = []
                    kept, wf_failures = _gather_run_jobs(client, repo, opt57_runs,
                                                         memo=job_memo)
                    jobs_fetch_failures += wf_failures
                    _accumulate_jobs(kept, opt57_failure_jobs, {})
                new = _detect_opt57_timeout_default_burn(
                    wf_path, opt57_failure_jobs, jobs_per_run, wf_doc, opt57_monthly, next_id,
                    sample_denominator=len(opt57_scope_runs))
                next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
                findings.extend(new)

            if _on_has_event(_wf_on(wf_doc), "schedule"):
                schedule_monthly = _monthly_event_volume(
                    client, repo, wf_id, "schedule", created_before)
                schedule_runs = _all_status_event_runs(
                    client, repo, wf_id, "schedule", _COST_RUNLIST_MAX, created_before)
                if schedule_runs is None:
                    # Same rule: an unfetchable schedule page is UNKNOWN, and OPT36 sized
                    # against it would report a schedule-burn workflow clean. Skip OPT36
                    # ONLY — the rest of this workflow's detectors are unaffected — and
                    # DISCLOSE the skip, or the reader sees a schedule-heavy workflow
                    # with no schedule finding and reads it as clean.
                    _skip_detectors(wf_path, ["OPT36"],
                                    "its schedule run list could not be fetched")
                    _note_run_list_gap(wf_path, "schedule run list (OPT36)")
                    logger.warning(
                        "%s: schedule run page unavailable — skipping OPT36 rather than "
                        "reporting it clean off an empty sample", wf_path)
                else:
                    # The dedicated schedule probe observed real event=schedule
                    # runs of this workflow; fold them into the persisted events
                    # mirror so OPT36's non_pr_event certificate re-derives from
                    # events_by_wf even when the main-pass success slice was all
                    # push. See _fold_observed_events.
                    _fold_observed_events(events_by_wf, wf_path, schedule_runs)
                    schedule_success = _sample_event_runs(
                        client, repo, wf_id, "schedule", max_runs, created_before)
                    if schedule_success is None:
                        # The PRICING sample failed (already counted in client.errors and
                        # the coverage note). OPT36's basis/denominator is schedule_runs
                        # (guarded above); an empty timing sample just means the finding
                        # prices from a 0-run mean, which the detector discloses.
                        schedule_success = []
                    schedule_jobs: list[list[dict[str, Any]]] = []
                    kept, wf_failures = _gather_run_jobs(client, repo, schedule_success,
                                                         memo=job_memo)
                    jobs_fetch_failures += wf_failures
                    _accumulate_jobs(kept, schedule_jobs, {})
                    new = _detect_opt36_schedule_burn(
                        wf_path, schedule_runs, schedule_jobs, wf_doc, schedule_monthly,
                        next_id)
                    next_id = max(next_id, max((int(f["id"][1:]) for f in new),
                                               default=next_id))
                    _supersede_static_opt36(findings, new)
                    findings.extend(new)

            if all_runs is not None:
                new = _detect_opt46_superseded_runs(
                    wf_path, all_runs, jobs_per_run, wf_doc, monthly, next_id)
                next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
                findings.extend(new)

                new = _detect_opt47_double_trigger(
                    wf_path, all_runs, jobs_per_run, wf_doc, monthly, default_branch, next_id)
                next_id = max(next_id, max((int(f["id"][1:]) for f in new), default=next_id))
                findings.extend(new)

        # OPT49 (slow SETUP step) is CUT. "A setup step takes >60s" is an
        # OBSERVATION, not a finding: the detector inferred the root cause
        # (uncached) purely from the duration, never verifying a missing/cold
        # cache the way the cache family (OPT3/5/8/9, --with-logs) does — so it
        # was exactly the "a step is slow" finding SKILL.md's admission gate
        # forbids. It also stapled one generic "add a cache" fix across
        # heterogeneous steps (a 61s `Checkout` is a git fetch, not uncached
        # deps), sprawled one monorepo-wide shared-setup root cause into N
        # near-identical occurrences, and carried a grossly overstated
        # runner-min figure. The real, VERIFIED signal is carried by the cache
        # family (which proves the cache is cold from logs) and by OPT73 (a
        # shared setup step across the cluster, sized honestly). Detector
        # retained but not dispatched.

        # OPT50 (post-step duration) is CUT — "a post-step takes a while" is an
        # observation, not an actionable optimization. Detector retained but not
        # dispatched (catalog pattern reported as detector-less for honesty).

        # OPT51 (Install-to-Test Ratio >50%) is CUT — same flaw as OPT49. A high
        # setup/total RATIO is an OBSERVATION, not a verified lever: the detector
        # credited `med_total * (med_ratio - 0.3)` as guaranteed savings (assuming
        # setup is reducible to 30% of the job) without ever proving the setup IS
        # reducible. A high ratio is just as often STRUCTURAL — a peer-dependency
        # validator, a docs-lint, or a Docker/Playwright job is mostly install by
        # nature, and you can't cache that away. So it sized large runner-min
        # figures (mastra: ~26.8k min/mo across 12 jobs, none on the critical
        # path) onto unrealizable savings, exactly the "a step/job is slow"
        # observation SKILL.md's admission gate forbids. The VERIFIED setup signal
        # is carried by the cache family (proves a cold cache from logs), by OPT73
        # (a shared setup step across the cluster, sized honestly), and by the
        # artifact-handoff patterns (a concrete, realizable lever). Detector
        # retained but not dispatched.

    findings_doc["findings"] = findings

    # The repo's longest measured job across ALL workflows — the physical ceiling on
    # any wall-clock saving (you can't save more than the slowest run takes). Used to
    # cap OPT19, whose source-sleep total is summed statically and isn't bound to a
    # workflow's own critical path.
    global_long_pole = max(
        ((c.get("long_pole_p50", 0.0) or 0.0) for c in crit_by_wf.values()),
        default=0.0)

    # Size each finding (data-driven ones already have measured values
    # inline; _size_finding's "measured" model preserves them) AND stamp
    # workflow_activity (runs_30d / last_run / dormant) from the sampling
    # above so the report can show "N runs in 30d · most recent YYYY-MM-DD"
    # per finding-group.
    for f in findings:
        wf_path = f.get("workflow_file", "")
        crit = crit_by_wf.get(wf_path, {})
        vol = vol_by_wf.get(wf_path)
        _size_finding(f, crit, vol)
        _cap_opt19_wall_clock(f, global_long_pole)
        # Cross-workflow concurrency floor: a per-workflow wall-clock saving is
        # only real developer-wait if THIS workflow is the slowest one running
        # on the PR. Cap it at the floor set by the slowest workflow sharing its
        # trigger, and record the concurrent set so the report can SHOW why.
        wc = f.get("wall_clock_p50_s")
        concurrent = _concurrent_workflows(wf_path, events_by_wf, crit_by_wf)
        if concurrent:
            f["concurrent_workflows"] = [
                {"workflow": w, "long_pole_p50_s": round(lp, 1)}
                for w, lp in concurrent
            ]
            f["workflow_long_pole_p50_s"] = round(
                (crit.get("long_pole_p50", 0.0) or 0.0), 1)
            f["workflow_long_pole_event"] = crit.get("event_scope") or "all-events"
            f["workflow_long_pole_runner"] = crit.get("runner_scope") or "all-runners"
        # Run-frequency (core input): share of the busiest PR workflow's volume.
        # Skip workflows whose 30d count is contaminated by human-chatter events
        # (issue_comment etc.): an inflated share is worse than none (unmeasured
        # → full ranking weight, never a silent demotion).
        wf_vol = vol_by_wf.get(wf_path)
        if (isinstance(wf_vol, int) and max_pr_vol
                and _volume_is_ci_clean(events_by_wf.get(wf_path))):
            f["workflow_run_share"] = round(wf_vol / max_pr_vol, 3)
            f["workflow_runs_30d"] = wf_vol
        # The measured PR critical path + this workflow's own check names, so the
        # cascade can floor wall-clock at the slowest OTHER concurrent check.
        own_check_names = frozenset((crit.get("job_p50") or {}).keys())
        if pr_checks_tuple:
            f["pr_critical_path_s"] = pr_checks_tuple[0][1]
            f["pr_critical_path_check"] = pr_checks_tuple[0][0]
        # Run every wall-clock-positive finding through the cross-cutting bound
        # CASCADE (developer-facing gate + measured critical-path floor + ...),
        # regardless of whether it has concurrent siblings. The cascade enforces
        # monotonic-down + no-silent-shrink and returns the derivation.
        if isinstance(wc, (int, float)) and wc > 0:
            ctx = WallClockContext(
                workflow=wf_path, crit=crit,
                concurrent=tuple(concurrent),
                affected_jobs=tuple(f.get("affected_jobs") or []),
                events=tuple(sorted(events_by_wf.get(wf_path) or ())),
                pr_checks=pr_checks_tuple, own_check_names=own_check_names,
                pr_check_populations=tuple(pr_check_populations),
                chain_members=_chain_ctx_members, chain_p50_s=_chain_ctx_p50,
                chain_win_s=_chain_ctx_win)
            res = size_wall_clock(float(wc), ctx)
            if res.derivation:
                f["wall_clock_p50_s"] = res.effective_s
                f["wall_clock_uncapped_p50_s"] = res.raw_s
                f["wall_clock_derivation"] = [
                    {"bound": b, "from_s": a, "to_s": c, "reason": why}
                    for b, a, c, why in res.derivation
                ]
                note = "; ".join(d[3] for d in res.derivation)
                prev = f.get("size_note") or ""
                f["size_note"] = f"{prev}; {note}" if prev else note
                if res.effective_s <= 0:
                    f["tier"] = 2
                    f["realization"] = "none"
        # Stamp activity for every finding whose workflow we actually sampled;
        # leave the scan.py-set empty dict alone otherwise so the report can
        # tell "unknown" apart from "dormant".
        act = activity_by_wf.get(wf_path)
        if act is not None:
            f["workflow_activity"] = act

    # ---- STRUCTURAL / critical-path findings (the second, non-catalog-bound
    # track). Routed from the measured critical path AFTER hygiene sizing, so a
    # check already shortened by a wall-clock-positive hygiene finding isn't
    # double-surfaced. Cross-references the repo's required-status checks (404 →
    # "unknown", never asserted non-required). ----
    # Only RUN-time hygiene savings may suppress a pole's structural lever; a
    # pre-start/queue-axis finding (OPT43) shortens time-to-start, not the
    # dominant step, so it must not stand in for the OPT75/etc lever on the pole.
    covered_job_savings = _build_covered_job_savings(findings)
    # (required_checks + the measured-critical-path sample were fetched above,
    # scoped to the 20 most recent full-suite PRs.)
    struct_start = max(
        (int(f["id"][1:]) for f in findings
         if f.get("id", "").startswith("f") and f["id"][1:].isdigit()),
        default=0)
    structural = _detect_structural_candidates(
        pr_checks_tuple, pr_check_populations, crit_by_wf, jobs_per_run_by_wf,
        required_checks, events_by_wf, covered_job_savings, struct_start,
        vol_by_wf=vol_by_wf, job_graph=findings_doc.get("workflow_job_graph"),
        job_bimodal_all=job_bimodal_all,
        check_present=check_present, present_n_pr=present_n_pr,
        check_pole_freq=check_pole_freq,
        triaged_fast_workflows=triaged_fast_workflows,
        chain_members=_chain_ctx_members, chain_p50_s=_chain_ctx_p50,
        chain_win_s=_chain_ctx_win)
    # Record the analysis depth so the report can distinguish a check that was
    # structurally analyzed and found genuinely inherent from one ranked below
    # this depth that was never examined (don't claim "inherent cost" for it).
    findings_doc["structural_analysis_top_n"] = _STRUCTURAL_TOP_N
    # Stamp the per-finding context the report renders (run-share, activity, the
    # measured PR critical path) on each structural finding, mirroring the
    # hygiene loop above.
    for f in structural:
        wf_path = f.get("workflow_file", "")
        crit = crit_by_wf.get(wf_path, {})
        wf_vol = vol_by_wf.get(wf_path)
        if (isinstance(wf_vol, int) and max_pr_vol
                and _volume_is_ci_clean(events_by_wf.get(wf_path))):
            f["workflow_run_share"] = round(wf_vol / max_pr_vol, 3)
            f["workflow_runs_30d"] = wf_vol
        if pr_checks_tuple:
            f["pr_critical_path_s"] = pr_checks_tuple[0][1]
            f["pr_critical_path_check"] = pr_checks_tuple[0][0]
        act = activity_by_wf.get(wf_path)
        if act is not None:
            f["workflow_activity"] = act
    findings.extend(structural)
    findings_doc["findings"] = findings

    # encord §6 Cause 2: stamp `off_spine` on findings whose job was DROPPED from the
    # merge-gating spine, so `blocking_path._saves_wall_clock` never frames them "on the
    # critical path" while the spine footnote says they were excluded.
    _stamp_off_spine_findings(
        findings, _dropped_non_pr + dropped_non_required,
        [n for n, _ in pr_checks_tuple], crit_by_wf)

    # --with-logs: attach verbatim cache hit/miss log lines (+ run links) to
    # every cache-family finding, so a "cache miss" claim points at the actual
    # miss in the actual run's log.
    tiers = ["gh-timing"]
    logs_fetched: int | None = None
    if with_logs:
        _attach_cache_log_evidence(client, repo, findings, jobs_per_run_by_wf)
        tiers.append("job-logs")
        # Capture the long-pole jobs' raw logs into a local data bundle so the
        # report / fix-agent can read the step's internal timing without
        # re-downloading. One fetch per pole job (the median-representative instance).
        if data_dir is not None:
            pole_logs = _persist_pole_logs(
                client, repo, drill_poles or poles, jobs_per_run_by_wf, data_dir,
                events_jobs_by_wf=events_jobs_by_wf)
            logs_fetched = len(pole_logs)
            if pole_logs:
                findings_doc["data_bundle"] = {
                    "logs_dir": str(data_dir.resolve()),
                    "logs": pole_logs,
                }
        # Admission gate: drop cache findings the log evidence couldn't prove
        # (no cache line AND no install/build activity / measurable cost) — an
        # unprovable absence is not a finding. Re-filter the already-set list.
        dropped = [f for f in findings if f.get("_drop")]
        if dropped:
            findings = [f for f in findings if not f.get("_drop")]
            findings_doc["findings"] = findings
            findings_doc["dropped_unprovable"] = [
                {"id": f.get("id"), "pattern": f.get("pattern"),
                 "affected_jobs": f.get("affected_jobs"), "reason": f["_drop"]}
                for f in dropped
            ]

    # The cost spine is measured runner-MINUTES (no rates involved), so it is
    # built unconditionally. The rate-free stamps (sizing_basis, neutrality)
    # run for every finding.
    cost_spine_events_jobs_by_wf = _clone_events_jobs_by_wf(events_jobs_by_wf)
    cost_spine_jobs_per_run_by_wf = _clone_jobs_per_run_by_wf(jobs_per_run_by_wf)
    cost_spine_triaged_workflows_included: list[str] = []
    cost_spine_triaged_runs_sampled = 0
    cost_spine_triaged_jobs_sampled = 0
    cost_deepen_candidate_workflows: list[str] = []
    cost_deepened_workflows: list[str] = []
    cost_deepen_runs_sampled = 0
    cost_deepen_jobs_sampled = 0
    for wf_path in sorted(triaged_fast_workflows):
        runs = triage_recover_stash.get(wf_path) or []
        if not runs:
            continue
        sample_n = min(shallow_runs, len(runs))
        jobs_per_run: list[list[dict[str, Any]]] = []
        jobs_by_event: dict[str, list[list[dict[str, Any]]]] = {}
        kept, wf_failures = _gather_run_jobs(client, repo, runs[:sample_n],
                                             memo=job_memo)
        _add_cost_spine_fetch_failures(wf_path, wf_failures)
        nr, nj = _accumulate_jobs(kept, jobs_per_run, jobs_by_event)
        if nr <= 0:
            continue
        cost_spine_triaged_runs_sampled += nr
        cost_spine_triaged_jobs_sampled += nj
        cost_spine_jobs_per_run_by_wf[wf_path] = jobs_per_run
        cost_spine_events_jobs_by_wf[wf_path] = jobs_by_event
        cost_spine_triaged_workflows_included.append(wf_path)

    findings_doc["data_sources"] = {
        "tiers_run": tiers,
        "gh_available": True,
        "workflows_analyzed": len(workflows_in_play),
        "runs_sampled": runs_sampled,
        "jobs_sampled": jobs_sampled,
        "jobs_fetch_failures": jobs_fetch_failures,
        # Adaptive 2-pass provenance. `shallow_capped` = some workflow had more than
        # `shallow_runs` runs (so the shallow pass actually capped sampling — distinct
        # from `shallow_runs >= max_runs`, a single full pass). `capped_workflows` = how
        # many were capped at the shallow depth; `deepened_workflows` = how many of those
        # were deepened to full --max-runs. The off-path-shallow disclosure keys off
        # `capped_workflows > deepened_workflows` (a capped workflow left at shallow
        # depth), NOT `pole_candidates`, so a shallow cron workflow can't go undisclosed
        # just because every PR pole was deepened. `pole_candidates` = PR-gating
        # workflows eligible for deepening. `deepen_converged` = the top region settled
        # before the round cap (False → the gate/floor may rest on a shallow sample).
        "shallow_runs": shallow_runs,
        "max_runs": max_runs,
        "shallow_capped": bool(deepen_stash),
        "capped_workflows": len(deepen_stash),
        "pole_candidates": len(eligible),
        "deepened_workflows": len(deepened),
        "deepen_converged": converged,
        # Run-list triage: workflows whose per-run job fetch was skipped because their
        # slowest sampled run finished under `_TRIAGE_WALLCLOCK_FLOOR_S` (can't hold the
        # pole). Each saved ~shallow_runs job calls. Disclosed so the skipped workflows'
        # run-list-only hygiene/queue coverage is never silent.
        "triaged_fast_workflows": sorted(triaged_fast_workflows),
        "triaged_fast_count": len(triaged_fast_workflows),
        # Workflows the absolute floor triaged but relative-recovery (>= a fraction of the
        # measured gate, `_TRIAGE_RECOVER_GATE_FRAC`) re-fetched + drilled like any pole — so a
        # seconds-scale repo's near-gate secondary pole isn't dismissed as "can't hold the pole".
        "recovered_fast_workflows": sorted(recovered_workflows),
        "recovered_fast_count": len(recovered_workflows),
        "cost_spine_triaged_workflows_included": sorted(
            cost_spine_triaged_workflows_included),
        "cost_spine_triaged_workflow_count": len(cost_spine_triaged_workflows_included),
        "cost_spine_triaged_runs_sampled": cost_spine_triaged_runs_sampled,
        "cost_spine_triaged_jobs_sampled": cost_spine_triaged_jobs_sampled,
        "logs_fetched": logs_fetched,
        # Where each workflow's YAML was PARSED from. The two sources can disagree (the
        # checkout is what the report stamps as audited; `GET /contents/` is the DEFAULT
        # BRANCH's HEAD), so which one fed the detectors is a fact about the report, not
        # an implementation detail. `run.py` additionally stamps the audited commit
        # `<sha>-dirty` when `.github/workflows` has uncommitted edits.
        "workflow_yaml_source": {
            "checkout": workflow_yaml_source.get("checkout", 0),
            "api": workflow_yaml_source.get("api", 0),
        },
        # The checkout we PARSED is on a different commit line than the branch the
        # sampled runs came from (a feature branch, a detached HEAD, a stale `main`).
        # Not dirty, so no `-dirty` marker fires and the stamped sha is true — and yet
        # the detectors read YAML that produced none of the timings. None = no skew we
        # could see. See `_root_branch_skew`.
        "workflow_yaml_skew": workflow_yaml_skew,
        # Detectors that were NOT EVALUATED for a named workflow because their basis run
        # list was unfetchable. Their absence from the findings is UNKNOWN, not clean —
        # the whole reason this is DATA and not just a log line. The renderer must name
        # each affected workflow (verify_report enforces it).
        "detectors_skipped": sorted(detectors_skipped.values(),
                                    key=lambda e: str(e.get("workflow"))),
        "gh_query_count": client.queries,
        "gh_error_count": client.errors,
        # Sampling window pin: when set, the run sample is the latest N
        # successful runs created ≤ this timestamp, so the finding set is
        # reproducible across regens instead of drifting as new runs land.
        # null = unpinned (always the latest runs at collection time).
        "sampled_runs_created_before": created_before,
        # Workflows whose RUN-LIST fetch failed — they are MISSING from the sample,
        # not empty. Disclosed by name (and folded into `partial_reason` below)
        # because the error count alone can't say WHICH workflow vanished: "4 gh
        # calls failed" reads as a rounding error, while "the merge-gate workflow
        # was dropped from the sample" is the actual news.
        "run_list_fetch_failures": run_list_fetch_failures,
        # Workflows whose per-run JOB fetches ALL failed — runs, but no job timing, so
        # they are MISSING from the measured sample too (and their queue-inflated
        # check-run fallback is barred from the spine). Same severity, same by-name
        # disclosure, same verify invariant as a run-list gap.
        "job_fetch_failures": job_fetch_failures,
        # `partial_reason` / `partial_kind` are RE-STAMPED at the end of collect()
        # (more gh calls happen below — the cost spine deepens), so these two are
        # provisional. The re-stamp goes through the SAME helpers; see the note there.
        "partial_reason": _partial_reason(client.errors, _sample_gaps()),
        "partial_kind": _partial_kind(client.errors, _sample_gaps()),
    }
    # Cache-distribution probe provenance. Set whenever logs were captured — even with
    # ZERO cache poles — so its PRESENCE is the schema stamp verify_report keys its
    # graceful SKIP on (an old-schema findings.json lacks it → the cache-framing
    # invariants skip rather than false-fail). Discloses the targeted-escalation cost:
    # PR-bucket evidence reuses already-fetched logs (`pr_logs_reused`), and the only new
    # fetches are the capped push-run logs (`push_logs_fetched`, ≤ _CACHE_PUSH_PROBE_MAX
    # per cache pole).
    if with_logs and data_dir is not None:
        _cache_poles = [p for p in (drill_poles or poles)
                        if isinstance(p, dict) and isinstance(p.get("cache_dist"), dict)]
        findings_doc["data_sources"]["cache_dist_probe"] = {
            "cache_poles": len(_cache_poles),
            "push_logs_fetched": sum(int(p["cache_dist"].get("push_logs_fetched") or 0)
                                     for p in _cache_poles),
            "pr_logs_reused": sum(int(_as_dict_local(p["cache_dist"].get("pr")).get("n") or 0)
                                  for p in _cache_poles),
        }
    # Stamp each workflow's OBSERVED trigger events onto its timing entry. This is the
    # ground truth verify_report's primary-section invariant re-derives merge-path
    # eligibility from (a push-only repo's `push` CI is its merge wait), mirroring the
    # `_PR_VOLUME_EVENTS`/`_PUSH_VOLUME_EVENTS`/`_volume_is_ci_clean` selection above —
    # so a measured merge-path long pole can never be silently dropped to static-only.
    for _wf_path, _crit in crit_by_wf.items():
        _crit["events"] = sorted(events_by_wf.get(_wf_path) or ())
    findings_doc["per_workflow_timing"] = crit_by_wf
    final_volume_workflows = set(vol_by_wf)

    # --- Tier-2 (runner-minute) stamps — ADDITIVE; data-only in PR-1 (nothing
    # is rendered from these yet), so the committed worked examples stay
    # byte-identical. Each stamp is independently re-derivable from findings.json
    # by verify_report, so no Tier-2 claim ever rests on rendered prose.
    for _f in findings_doc.get("findings") or []:
        _crit = crit_by_wf.get(_f.get("workflow_file", ""), {})
        _stamp_sizing_basis(_f)
        _stamp_tier2_neutrality(_f, _crit)
    _reconcile_tier2_overlap(findings_doc.get("findings") or [])
    finding_seed_workflows = _finding_seed_workflow_paths(findings, workflow_paths)

    def _coverage_fetch_failures(workflows: set[str]) -> int:
        return sum(cost_spine_fetch_failures_by_wf.get(wf, 0) for wf in workflows)

    def _row_workflows_from_spine(spine_obj: dict[str, Any] | None) -> set[str]:
        if not isinstance(spine_obj, dict):
            return set()
        return {
            str(row.get("workflow_file") or "")
            for row in (spine_obj.get("rows") or [])
            if isinstance(row, dict) and str(row.get("workflow_file") or "")
        }

    def _spine_sample_counts_by_wf(spine_obj: dict[str, Any] | None) -> dict[str, int]:
        if not isinstance(spine_obj, dict):
            return {}
        counts: dict[str, int] = {}
        for row in spine_obj.get("rows") or []:
            if not isinstance(row, dict):
                continue
            wf = str(row.get("workflow_file") or "")
            if not wf:
                continue
            try:
                n = int(row.get("sampled_workflow_run_count") or 0)
            except (TypeError, ValueError):
                n = 0
            counts[wf] = min(counts.get(wf, n), n)
        return counts

    def _expected_sampled_runs(wf_path: str) -> int:
        runs = sampled_runs_by_wf.get(wf_path) or triage_recover_stash.get(wf_path) or []
        return min(max_runs, len(runs))

    def _build_cost_spine() -> tuple[dict[str, Any] | None, set[str], set[str]]:
        spine_obj = _build_runner_minute_spine(
            cost_spine_events_jobs_by_wf, cost_spine_jobs_per_run_by_wf, vol_by_wf,
            findings_doc.get("repo_visibility"),
            prior_attempt_jobs_by_wf=prior_attempt_jobs_by_wf,
            workflows_in_play=finding_seed_workflows,
            triaged_workflows_included=cost_spine_triaged_workflows_included,
            coverage_fetch_failures=_coverage_fetch_failures(finding_seed_workflows))
        coverage = set(finding_seed_workflows)
        rows = _row_workflows_from_spine(spine_obj)
        if spine_obj is not None:
            # OPT57 seeds extra workflows so the detector can look for measured
            # timeout-default burn. If a probe emits no finding and no spine rows,
            # it should not become a render-readiness coverage requirement.
            coverage = finding_seed_workflows | (opt57_seed_workflows & rows)
            if coverage != finding_seed_workflows:
                rebuilt_spine = _build_runner_minute_spine(
                    cost_spine_events_jobs_by_wf, cost_spine_jobs_per_run_by_wf,
                    vol_by_wf, findings_doc.get("repo_visibility"),
                    prior_attempt_jobs_by_wf=prior_attempt_jobs_by_wf,
                    workflows_in_play=coverage,
                    triaged_workflows_included=cost_spine_triaged_workflows_included,
                    coverage_fetch_failures=_coverage_fetch_failures(coverage))
                if rebuilt_spine is not None:
                    spine_obj = rebuilt_spine
                    rows = _row_workflows_from_spine(spine_obj)
        return spine_obj, coverage, rows

    spine, coverage_workflows, row_workflows = _build_cost_spine()
    deepened_workflow_set = set(deepened)

    def _cost_deepen_workflow(wf_path: str) -> tuple[int, int, int]:
        nonlocal cost_deepen_runs_sampled, cost_deepen_jobs_sampled
        # Includes fast-triaged workflows: sampled_runs_by_wf is populated
        # immediately after each workflow run list is fetched, before any
        # job-fetch triage can skip the wall-clock sample. The stash fallback
        # keeps future refactors from silently skipping triaged bill-pole
        # candidates if that assignment ever moves.
        runs = sampled_runs_by_wf.get(wf_path) or triage_recover_stash.get(wf_path) or []
        if not runs:
            return 0, 0, 0
        if wf_path in deepened_workflow_set:
            # The wall-clock deepen pass already extended this workflow to max depth.
            return 0, 0, 0
        start = min(shallow_runs, len(runs))
        rest = runs[start:]
        if not rest:
            return 0, 0, 0
        jobs_per_run = cost_spine_jobs_per_run_by_wf.setdefault(wf_path, [])
        jobs_by_event = cost_spine_events_jobs_by_wf.setdefault(wf_path, {})
        kept, wf_failures = _gather_run_jobs(client, repo, rest, memo=job_memo)
        _add_cost_spine_fetch_failures(wf_path, wf_failures)
        nr, nj = _accumulate_jobs(kept, jobs_per_run, jobs_by_event)
        cost_deepen_runs_sampled += nr
        cost_deepen_jobs_sampled += nj
        return nr, nj, wf_failures

    if spine is not None:
        cost_deepen_candidate_workflows = _cost_deepen_candidates_from_spine(spine)
        cost_deepen_spine_dirty = False
        for wf_path in cost_deepen_candidate_workflows:
            nr, _nj, wf_failures = _cost_deepen_workflow(wf_path)
            if nr > 0 or wf_failures > 0:
                cost_deepen_spine_dirty = True
            if nr > 0 and wf_failures == 0 and _coverage_fetch_failures({wf_path}) == 0:
                cost_deepened_workflows.append(wf_path)
        if cost_deepened_workflows:
            logger.info("adaptive sampling: deepened %d bill-pole workflow "
                        "candidate(s) to %d runs",
                        len(cost_deepened_workflows), max_runs)
        if cost_deepen_spine_dirty:
            spine, coverage_workflows, row_workflows = _build_cost_spine()

    if spine is not None:
        final_volume_workflows = coverage_workflows | row_workflows
        findings_doc["runner_minute_spine"] = spine
        # THE sizing door (#43/#44/#45): re-ground EVERY finding's credited
        # runner-minute saving in the spine's MEASURED billable compute —
        # OPT45 derives, OPT73 clamps, the rest stamp a visible basis — so a
        # credited saving never exceeds what its affected jobs consume and
        # nothing renders a saving without a `runner_min_basis`. Runs AFTER the
        # spine is final.
        _reground_runner_minute_savings(findings_doc.get("findings") or [], spine)
    else:
        final_volume_workflows = coverage_workflows
    findings_doc["data_sources"]["cost_spine_job_fetch_failures"] = (
        _coverage_fetch_failures(coverage_workflows))
    full_depth_workflows = sorted(set(deepened))
    shallow_remaining_workflows = sorted(set(deepen_stash) - set(deepened))
    cost_spine_full_depth_workflows: list[str] = []
    cost_spine_shallow_workflows: list[str] = []
    if isinstance(findings_doc.get("runner_minute_spine"), dict):
        _spine = findings_doc.get("runner_minute_spine")
        _sample_counts = _spine_sample_counts_by_wf(_spine)
        _row_workflows = _row_workflows_from_spine(_spine)
        for _wf_path in sorted(_row_workflows):
            _expected = _expected_sampled_runs(_wf_path)
            if _expected <= shallow_runs:
                continue
            _sampled = _sample_counts.get(_wf_path, 0)
            if (_sampled >= _expected and
                    cost_spine_fetch_failures_by_wf.get(_wf_path, 0) == 0):
                cost_spine_full_depth_workflows.append(_wf_path)
            else:
                cost_spine_shallow_workflows.append(_wf_path)
        cost_deepened_workflows = sorted(
            set(cost_deepened_workflows) & set(cost_spine_full_depth_workflows))
    findings_doc["data_sources"].update({
        "cost_deepen_candidate_workflows": cost_deepen_candidate_workflows,
        "cost_deepen_candidate_count": len(cost_deepen_candidate_workflows),
        "cost_deepened_workflows": sorted(cost_deepened_workflows),
        "cost_deepened_workflow_count": len(cost_deepened_workflows),
        "cost_deepen_runs_sampled": cost_deepen_runs_sampled,
        "cost_deepen_jobs_sampled": cost_deepen_jobs_sampled,
        "full_depth_workflows": full_depth_workflows,
        "full_depth_workflow_count": len(full_depth_workflows),
        "shallow_remaining_workflows": shallow_remaining_workflows,
        "shallow_remaining_workflow_count": len(shallow_remaining_workflows),
        "cost_spine_full_depth_workflows": cost_spine_full_depth_workflows,
        "cost_spine_full_depth_workflow_count": len(cost_spine_full_depth_workflows),
        "cost_spine_shallow_workflows": cost_spine_shallow_workflows,
        "cost_spine_shallow_workflow_count": len(cost_spine_shallow_workflows),
    })
    # Every prefetch wave should have been drained by its call sites (a parked response
    # nobody consumed is a gh call the serial path never made). Nothing should be left;
    # if something is, a prefetch plan has drifted from its call site — say so loudly
    # rather than quietly billing the user's rate limit for it.
    #
    # This guard earns its keep: it caught 31 real unconsumed prefetches while this pass
    # was being built, so "the plan mirrors the call site" is empirically fragile. A
    # stderr WARNING dies with the scrollback, so the count is ALSO written into
    # `data_sources` — where it is DISCLOSED (the report's data-collection block renders a
    # line for it when non-zero) and CHECKED (`verify_report.py` fails a report whose
    # `prefetch_unconsumed` is non-zero). Note the committed worked-example reports predate
    # this key: they were rendered from findings documents collected before it existed, so
    # they carry no `prefetch_unconsumed` at all. The key is a guarantee about FUTURE
    # collected reports, not evidence about the committed ones — the serial-vs-parallel
    # equivalence test in `tests/test_gh_concurrency.py` is what proves this pass.
    _drain = getattr(client, "drain_prefetch", None)
    if _drain is None:
        # A client with `prefetch_json` but no `drain_prefetch` (a rename) would silently
        # disable the drift guard itself. Never fatal — but never silent either.
        _note_no_buffer(client, "drain_prefetch")
        leftover = 0
    else:
        leftover = _drain()
    if leftover:
        logger.warning("prefetch: %d response(s) fetched but never consumed — a prefetch "
                       "plan has drifted from its call site (extra gh calls were paid "
                       "for; the sampled data is unaffected)", leftover)
    # FINAL coverage re-stamp. It exists because gh calls keep happening after the
    # data_sources dict is first built (the cost-spine deepen pass), so the counts
    # there are provisional and must be refreshed from the client.
    #
    # It MUST go through the same `_partial_reason` / `_partial_kind` helpers as the
    # first stamp. It used to re-derive the sentence inline from `client.errors`
    # alone, which silently CLOBBERED the by-name disclosure written above: a
    # vanished merge-gate workflow was re-stamped as the bare "N gh API call(s)
    # failed during collection" — a rounding error — and the critical path was then
    # headlined confidently off the survivors. On `main` both writes happened to
    # agree, which is exactly why the clobber went unnoticed. Any future upgrade to
    # a `data_sources` key that a later line re-stamps has this failure mode; keep
    # the derivation in ONE helper so the two writes cannot drift.
    findings_doc["data_sources"]["gh_query_count"] = client.queries
    findings_doc["data_sources"]["gh_error_count"] = client.errors
    findings_doc["data_sources"]["run_list_fetch_failures"] = run_list_fetch_failures
    findings_doc["data_sources"]["job_fetch_failures"] = job_fetch_failures
    # Prefetch-drift disclosure (the parallel pass): non-zero means a plan over-fetched.
    # DISCLOSED here and CHECKED by `verify_report.py`; see the drain guard above.
    findings_doc["data_sources"]["prefetch_unconsumed"] = leftover
    findings_doc["data_sources"]["partial_reason"] = _partial_reason(
        client.errors, _sample_gaps(), gave_up=getattr(client, "gave_up", False))
    findings_doc["data_sources"]["partial_kind"] = _partial_kind(
        client.errors, _sample_gaps(), gave_up=getattr(client, "gave_up", False))
    findings_doc["per_workflow_monthly_volume"] = {
        wf: vol_by_wf[wf] for wf in sorted(final_volume_workflows) if wf in vol_by_wf
    }
    # Explicit top-level events mirror (also nested per-workflow in
    # per_workflow_timing[wf]["events"]) — a clean re-derivation surface for a
    # Tier-2 non_pr_event certificates. Sets, not lists, live in events_by_wf,
    # so sort to a serializable list.
    findings_doc["events_by_wf"] = {
        _wf: sorted(_evs) for _wf, _evs in events_by_wf.items()}
    # Join required-check names to their workflows' PR-trigger conditionality
    # (the dead-required check's load-bearing conjunct). Additive metadata on
    # the critical path; harmless if unconsumed.
    findings_doc["pr_critical_path"]["required_check_conditionality"] = (
        _required_check_conditionality(findings_doc))
    # Retry-tax attempt stats, aggregated over the GATING workflows (the pole
    # workflow files — the retry tax is developer wait on the gate, not
    # repo-wide rerun volume). Structured because the spec bans prose parsing.
    _gate_wfs = {str(p.get("workflow_file"))
                 for p in findings_doc["pr_critical_path"].get("poles") or []
                 if p.get("workflow_file")}
    _gate_stats = [s for wf, s in attempt_stats_by_wf.items() if wf in _gate_wfs]
    findings_doc["data_sources"]["attempt_stats"] = {
        "scope": "pr-event runs of gating workflows (all-status slice)",
        "gating_workflows": sorted(_gate_wfs & set(attempt_stats_by_wf)),
        # Gating workflows whose all-status slice never materialized (fetch
        # failed / triaged out). NON-EMPTY = partial coverage: the retry-tax
        # line must not render — a failed fetch is not a 0.0 tax.
        "workflows_missing": sorted(_gate_wfs - set(attempt_stats_by_wf)),
        "pr_event_runs": sum(s["pr_event_runs"] for s in _gate_stats),
        "pr_event_runs_multi_attempt": sum(
            s["pr_event_runs_multi_attempt"] for s in _gate_stats),
        "prior_attempt_gating_minutes": round(
            sum(s["prior_attempt_job_minutes"] for s in _gate_stats), 1),
        "sampled_prs": int(findings_doc["pr_critical_path"].get("sampled_pr_count") or 0),
    }
    return findings_doc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--in", dest="input_path", type=Path, required=True)
    p.add_argument("--out", dest="output_path", type=Path, required=True)
    p.add_argument("--repo", default=None,
                   help="owner/name for gh calls. Omit to skip the gh pass "
                        "(findings stay un-sized, rendered qualitatively).")
    # The local checkout scan.py already walked. Workflow YAML is read from it
    # instead of over `GET /contents/` — one fewer gh call per workflow, and the
    # YAML we parse is then the SAME commit the report stamps as audited (the
    # contents API serves the default branch's HEAD, which can differ). Omit to
    # fall back to the API for every workflow (the pre-`--root` behavior).
    p.add_argument("--root", dest="root", type=Path, default=None,
                   help="Repo root on disk (as passed to run.py --root). Workflow "
                        "files are read from here; the gh contents API is the fallback.")
    # Default 20 (was 8): the critical path is scoped to the dominant runner, so
    # a repo on mixed runners needs enough samples to (a) identify the dominant
    # runner correctly and (b) leave a stable per-job p50 after scoping. 8 was
    # too thin — it mis-identified ci.yml's dominant runner and inverted the
    # better-auth long-pole ranking.
    p.add_argument("--max-runs", type=int, default=20)
    # Adaptive 2-pass: shallow-sample every workflow at --shallow-runs, then deepen
    # the top pole candidates to --max-runs. Set `--shallow-runs` >= `--max-runs` to
    # force a single full-depth pass (the pre-adaptive behavior).
    p.add_argument("--shallow-runs", type=int, default=_SHALLOW_RUNS)
    # Pin the run-sampling window so a regen reproduces the same finding set
    # instead of drifting as new runs land. Pass the prior audit's scan time
    # (ISO-8601, e.g. 2026-05-31T18:28:55Z) to re-sample the exact same runs.
    p.add_argument("--created-before", dest="created_before", default=None,
                   help="ISO-8601 timestamp; sample only runs created at or "
                        "before it (reproducible window). Default: latest runs.")
    # The following flags exist so run.py's orchestration contract is honored.
    # `--target` is a downstream/orchestrator flag, not consumed here; run.py
    # passes it through this stage transparently — accept and ignore.
    p.add_argument("--target", type=int, default=10,
                   help="(orchestrator flag, accepted for contract parity; unused here).")
    # `--with-logs` fetches the affected job's logs for each cache-family
    # finding and attaches the verbatim cache hit/miss line (+ a link to that
    # run) as measured_evidence — so a "cache miss" claim shows the actual miss.
    p.add_argument("--with-logs", action="store_true",
                   help="Fetch the affected job's logs for cache findings and "
                        "attach the verbatim cache hit/miss line + run link.")
    p.add_argument("--data-dir", dest="data_dir", type=Path, default=None,
                   help="Directory to save the long-pole jobs' raw logs into (the "
                        "data bundle the report/fix-agent reads for the step's "
                        "internal timing). Only used with --with-logs.")
    # `--catalog` is also for downstream tools; collect_runs doesn't currently
    # need the catalog content, but accept the flag for contract parity.
    p.add_argument("--catalog", type=Path, default=None,
                   help="(orchestrator flag, ignored here; scan.py uses it).")
    args = p.parse_args(argv)

    try:
        findings_doc = json.loads(args.input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read findings JSON {args.input_path}: {e}", file=sys.stderr)
        return 1

    t0 = time.time()
    findings_doc = collect(findings_doc, args.repo, args.max_runs,
                           with_logs=args.with_logs,
                           created_before=args.created_before,
                           data_dir=args.data_dir,
                           shallow_runs=args.shallow_runs,
                           root=args.root)
    _capture_bill_gap_workflows(findings_doc)
    timings = findings_doc.setdefault("timings", {})
    timings["gh_timing_s"] = round(time.time() - t0, 2)

    # Atomic write — `.partial` then rename, so a partial file never replaces a
    # good one if the process dies mid-write.
    out = args.output_path
    partial = out.with_name(out.name + ".partial")
    partial.write_text(json.dumps(findings_doc, indent=2) + "\n", encoding="utf-8")
    partial.replace(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

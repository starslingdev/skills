"""Class-level guard for ci-speedup finding *sizing* (the §③(b) sizing-cap guard).

This is the sizing analog of the claim guards in `test_evidence_claim_guards.py`.
Those lock the invariant "no finding may assert a workflow-shape fact it never
read from the data"; this one locks the physical invariant on the *number* a
finding credits:

  > No finding may claim a wall-clock saving larger than is physically possible.

A CI fix cannot save a developer more wall-clock than (a) the entire slowest job
on the measured critical path (the "long pole" — eliminate it completely and the
path only drops to the next job), nor (b) the affected job's own measured
duration (you cannot remove more than the job takes). Either ceiling exceeded =
an over-credit: the report tells the user "save ~Xs" when X is impossible. That
is the bug class the dogfood fleet kept re-surfacing (Class C — sizing
over-credit below the floor) — `own_check_names` scoped to the whole workflow,
a saving credited with no floor, a leg sized off the global pole. Each was a
per-pattern point-fix; this guard asserts the INVARIANT so the next detector to
over-credit fails in CI instead of on a real repo.

Why a property guard and not more point-fixes: the sizing logic lives in two
places and a fix in one doesn't protect the other —
  1. the central `_size_finding` layer (direct / parallel-rebalance / runner-min
     / serial-gate models), which structurally caps at the long-pole headroom; and
  2. the data-driven ("measured") detectors that size INLINE off sampled gh
     timings (OPT24 sharding; OPT25/OPT49/OPT50/OPT51 via the shared
     `_cap_wall_clock`; OPT19 via `_cap_opt19_wall_clock`), bypassing #1's cap.
This guard exercises BOTH paths and a coverage sentinel so a newly-added
`measured` detector can't quietly ship un-guarded.

EXEMPT axes (measured but NOT bounded by the run-time long pole):
  - OPT43 (`_PRESTART_AXIS_PATTERNS`) measures wait-to-START — time *before* the
    job runs — which legitimately exceeds the job's own run duration.
  - OPT48 is advisory and carries no wall-clock saving (`wc_p50=None`).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL_DIR / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import collect_runs as cr  # noqa: E402  (uniquely-named module; no cross-skill clash)


# Durations in the fixtures are second-granular and the sizing layer rounds to
# 0.1s, so allow a small slack before calling a value an over-credit. Real
# over-credits are 10s–100s+ (a whole extra job), never within this band.
_EPS = 2.0


# --------------------------------------------------------------------------- #
# The physical-bound checker — reused by every test below AND self-tested so it
# can never pass vacuously.
# --------------------------------------------------------------------------- #
def _physical_bound_violation(f: dict, crit: dict) -> str | None:
    """Return a message if finding `f`'s `wall_clock_p50_s` exceeds the physical
    ceiling for a RUN-TIME-axis saving, else None.

    Ceiling = min(the critical-path long pole, the finding's own affected-job
    p50 when it resolves). Pre-start/queue-axis patterns (`_PRESTART_AXIS_PATTERNS`)
    measure time before the job runs and are exempt."""
    pat = f.get("pattern", "")
    if pat in cr._PRESTART_AXIS_PATTERNS:
        return None
    wc = f.get("wall_clock_p50_s")
    if not isinstance(wc, (int, float)) or wc <= 0:
        return None  # None / 0 / qualitative — nothing credited, nothing to bound
    long_pole = (crit or {}).get("long_pole_p50") or 0.0
    if long_pole and wc > long_pole + _EPS:
        return (f"{pat}: wall_clock {wc}s exceeds the critical-path long pole "
                f"{long_pole}s — a fix can't save more wall-clock than the "
                f"slowest job on the path")
    own = cr._affected_job_p50(f, crit) if f.get("affected_jobs") else 0.0
    if own > 0 and wc > own + _EPS:
        return (f"{pat}: wall_clock {wc}s exceeds its own affected job's p50 "
                f"{own}s — a fix can't save more on a job than the job takes")
    return None


def test_physical_bound_checker_catches_overcredit_and_clears_legit():
    """Pin the checker on synthetic findings so a refactor can't neuter it: an
    over-credit past the long pole and one past the own-job p50 must both trip;
    an honest saving, a queue-axis (OPT43) saving above the pole, and a
    qualitative (None) finding must all clear."""
    crit = {"long_pole_job": "test", "long_pole_p50": 400.0, "floor_p50": 100.0,
            "job_p50": {"lint": 50.0, "test": 400.0}}
    # over the long pole
    assert _physical_bound_violation(
        {"pattern": "OPT24", "wall_clock_p50_s": 900.0, "affected_jobs": []}, crit)
    # over the own job (lint is 50s; crediting 200s is impossible) even though
    # 200s < the 400s long pole — the per-job ceiling is the tighter one here
    assert _physical_bound_violation(
        {"pattern": "OPT17", "wall_clock_p50_s": 200.0, "affected_jobs": ["lint"]}, crit)
    # honest: at the long pole, no affected job named → clear
    assert _physical_bound_violation(
        {"pattern": "OPT19", "wall_clock_p50_s": 400.0, "affected_jobs": []}, crit) is None
    # honest per-job: on the long pole, under its own duration → clear
    assert _physical_bound_violation(
        {"pattern": "OPT24", "wall_clock_p50_s": 150.0, "affected_jobs": ["test"]}, crit) is None
    # queue axis (OPT43): a wait-to-start above the long pole is legitimate → exempt
    assert _physical_bound_violation(
        {"pattern": "OPT43", "wall_clock_p50_s": 900.0, "affected_jobs": ["test"]}, crit) is None
    # qualitative / no credit → nothing to bound
    assert _physical_bound_violation(
        {"pattern": "OPT61", "wall_clock_p50_s": None, "affected_jobs": ["test"]}, crit) is None


def _effective_model(pattern: str) -> str | None:
    """The sizing model `_size_finding` will actually use for `pattern`, applying
    the serial-gate override (those demote to wall-clock-negative)."""
    if pattern in cr._SERIAL_GATE_PATTERNS:
        return "wall-clock-negative"
    return (cr._SIZING.get(pattern) or {}).get("model")


def _headroom_violation(f: dict, crit: dict) -> str | None:
    """The TIGHTER bound for the central layer's `direct` / `parallel-rebalance`
    models, which both cap at `min(s, long_pole − floor)`: a saving on the long
    pole can only shorten the path down to the next job (the floor), never past
    it. This is the "credited with no floor" subcase the dogfood Class-C names —
    a finding crediting 350s where the floor leaves only 20s of headroom passes
    the gross long-pole ceiling but is still an over-credit. The inline measured
    detectors (OPT24 sharding) intentionally beat this floor and are NOT subject
    to it, so this is asserted only for the two central models that promise it."""
    wc = f.get("wall_clock_p50_s")
    if not isinstance(wc, (int, float)) or wc <= 0:
        return None
    long_pole = (crit or {}).get("long_pole_p50") or 0.0
    if not long_pole:
        return None
    floor = (crit or {}).get("floor_p50") or 0.0
    headroom = max(long_pole - floor, 0.0)
    if wc > headroom + _EPS:
        return (f"{f.get('pattern')}: wall_clock {wc}s exceeds the critical-path "
                f"headroom {headroom}s (long pole {long_pole}s − floor {floor}s) — a "
                f"fix on the long pole only shortens the path down to the next job")
    return None


# --------------------------------------------------------------------------- #
# Path 1 — the central `_size_finding` layer, EVERY pattern in the sizing table.
# Iterating `cr._SIZING` means a newly-added central-layer pattern is covered the
# moment it's registered, with no new test — the "by construction" property.
# --------------------------------------------------------------------------- #
# A repo whose long pole is `test` (400s); floor (next job) is 100s. The
# scenarios place the affected job at every meaningful position relative to the
# pole and floor, plus the unresolvable and no-timing edges the sizing layer
# special-cases.
_CRIT = {
    "long_pole_job": "test", "long_pole_p50": 400.0, "floor_p50": 100.0,
    "job_p50": {"lint": 50.0, "typecheck": 100.0, "build": 250.0, "test": 400.0},
}
_NO_TIMING = {"long_pole_job": "", "long_pole_p50": 0.0, "floor_p50": 0.0, "job_p50": {}}
# A repo whose floor sits just below the long pole: the physical max wall-clock
# saving is only the 20s headroom, so the gross long-pole ceiling (400s) is far
# too loose here — this scenario is what makes `_headroom_violation` bite for the
# direct/parallel models (the "credited with no floor" Class-C subcase).
_TIGHT_FLOOR = {"long_pole_job": "test", "long_pole_p50": 400.0, "floor_p50": 380.0,
                "job_p50": {"build": 380.0, "test": 400.0}}

_SCENARIOS = {
    "on-long-pole": (_CRIT, ["test"]),          # the affected job IS the pole
    "above-floor-below-pole": (_CRIT, ["build"]),  # 250s — between floor and pole
    "below-floor": (_CRIT, ["lint"]),            # 50s — under the floor
    "at-floor": (_CRIT, ["typecheck"]),          # 100s — exactly the floor
    "unresolvable-job": (_CRIT, ["ghost-job"]),  # named but not in job_p50
    "workflow-level": (_CRIT, []),               # no affected job
    "no-timing": (_NO_TIMING, ["whatever"]),     # static-only, empty critical path
    "tight-floor-on-pole": (_TIGHT_FLOOR, ["test"]),  # only 20s of real headroom
}


@pytest.mark.parametrize("pattern", sorted(cr._SIZING))
@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_size_finding_never_overcredits(pattern: str, scenario: str):
    """No pattern, in any affected-job position, may have `_size_finding` credit a
    wall-clock saving beyond the physical ceiling. Measured patterns size INLINE
    (this layer leaves their wc as None) and are covered by the path-2 tests."""
    crit, jobs = _SCENARIOS[scenario]
    f = {"pattern": pattern, "affected_jobs": list(jobs)}
    cr._size_finding(f, crit, monthly_volume=1000)
    violation = _physical_bound_violation(f, crit)
    assert violation is None, violation
    # The two central models that cap at the floor headroom must also respect the
    # TIGHTER bound (the gross long-pole ceiling alone wouldn't catch a dropped
    # `− floor` term — the "credited with no floor" Class-C subcase).
    if _effective_model(pattern) in ("direct", "parallel-rebalance"):
        hv = _headroom_violation(f, crit)
        assert hv is None, hv


def test_size_finding_battery_is_non_vacuous():
    """Sentinel: the battery must actually exercise positive wall-clock credits in
    the central layer, or the guard above would pass while bounding nothing (e.g.
    if every model silently degraded to None). At least the direct (OPT17) and
    parallel-rebalance (OPT23) models must credit a positive, in-bounds saving on
    the long pole."""
    credited = 0
    for pattern in ("OPT17", "OPT23"):
        f = {"pattern": pattern, "affected_jobs": ["test"]}
        cr._size_finding(f, _CRIT, monthly_volume=1000)
        wc = f.get("wall_clock_p50_s") or 0
        if wc > 0:
            credited += 1
            assert _physical_bound_violation(f, _CRIT) is None
    assert credited == 2, (
        "neither the direct nor the parallel-rebalance model credited a positive "
        "wall-clock saving — the central-layer guard is bounding nothing; refresh "
        "the scenarios")


def test_direct_and_parallel_caps_are_structural_not_catalog(monkeypatch):
    """The cap must come from `_size_finding`'s structure (`min(s, headroom)`),
    NOT from the catalog's `default_s` happening to be small. Inject synthetic
    patterns with an absurd per-run estimate and confirm both wall-clock models
    still cap to the critical-path headroom — so a future pattern with a large
    estimate (or a removed cap) is caught here."""
    monkeypatch.setitem(cr._SIZING, "OPTDIRECTTEST",
                        {"model": "direct", "default_s": 99999.0})
    monkeypatch.setitem(cr._SIZING, "OPTPARALLELTEST", {"model": "parallel-rebalance"})
    # parallel-rebalance halves the affected job; give it a job far larger than
    # the pole so an un-capped halving (≫ headroom) would trip the checker.
    big_crit = {"long_pole_job": "huge", "long_pole_p50": 5000.0, "floor_p50": 100.0,
                "job_p50": {"huge": 5000.0, "test": 400.0}}
    for pat, crit, jobs in (("OPTDIRECTTEST", _CRIT, ["test"]),
                            ("OPTPARALLELTEST", big_crit, ["huge"])):
        f = {"pattern": pat, "affected_jobs": jobs}
        cr._size_finding(f, crit, monthly_volume=1000)
        assert _physical_bound_violation(f, crit) is None, pat
        # The tighter headroom bound too — a removed `− floor` term must trip here.
        assert _headroom_violation(f, crit) is None, pat
        assert f["wall_clock_p50_s"] <= max(
            crit["long_pole_p50"] - crit["floor_p50"], 0.0) + _EPS, pat


# --------------------------------------------------------------------------- #
# Path 2 — the data-driven ("measured") detectors that size INLINE, bypassing
# `_size_finding`'s cap. Each must independently respect the same ceiling.
# --------------------------------------------------------------------------- #
def _job(name: str, secs: float) -> dict:
    mm, ss = divmod(int(secs), 60)
    return {"name": name, "html_url": "http://x",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": f"2026-01-01T00:{mm:02d}:{ss:02d}Z"}


def test_cap_wall_clock_never_exceeds_long_pole():
    """The shared cap OPT25/OPT49/OPT50/OPT51 route their raw estimate through
    must never return a value above the long pole, for ANY raw input — including
    a raw estimate far larger than the whole critical path."""
    crit = {"long_pole_job": "build", "long_pole_p50": 400.0, "floor_p50": 100.0,
            "job_p50": {"build": 400.0, "lint": 50.0}}
    for raw in (0.0, 50.0, 300.0, 400.0, 1000.0, 99999.0):
        for job in ("build", "lint", "ghost"):
            capped, _note = cr._cap_wall_clock(raw, job, crit)
            assert capped <= crit["long_pole_p50"] + _EPS, (raw, job, capped)


def test_opt24_inline_sizing_respects_physical_bound():
    """OPT24 sizes off half the shardable payload and intentionally skips the
    shared cap (sharding parallelizes the long pole itself). Its credit must
    still never exceed the test job's own measured duration nor the long pole —
    on a heavy long-test job beside a lighter sibling."""
    runs = [[_job("integration test", 600), _job("lint", 90)] for _ in range(8)]
    crit = cr._critical_path(runs)
    out = cr._detect_opt24_long_test_no_sharding("ci.yml", runs, 0, monthly_volume=300)
    assert out, "fixture should produce an OPT24 finding"
    for f in out:
        v = _physical_bound_violation(f, crit)
        assert v is None, v
        assert (f.get("wall_clock_p50_s") or 0) > 0  # non-vacuous: it credited a saving


def test_opt25_inline_sizing_respects_physical_bound():
    """OPT25 (matrix/shard imbalance) sizes a rebalance/split saving and routes it
    through `_cap_wall_clock(crit)`. With a slow leg beside a slower non-matrix
    `build` long pole, the credit must stay within the physical ceiling."""
    runs = [[_job("suite (fast)", 120), _job("suite (slow)", 480), _job("build", 500)]
            for _ in range(6)]
    crit = cr._critical_path(runs)
    out = cr._detect_opt25_shard_imbalance("ci.yml", runs, 0, crit)
    assert out, "fixture should produce an OPT25 imbalance finding"
    for f in out:
        v = _physical_bound_violation(f, crit)
        assert v is None, v


def test_opt19_cap_respects_physical_bound():
    """OPT19's static summed sleep total is capped to the longest measured job by
    `_cap_opt19_wall_clock`. A total far above the pole must be brought to the
    pole, never left over-credited."""
    f = {"pattern": "OPT19", "wall_clock_p50_s": 5000.0, "affected_jobs": []}
    cr._cap_opt19_wall_clock(f, global_long_pole_p50=400.0)
    crit = {"long_pole_job": "x", "long_pole_p50": 400.0, "floor_p50": 100.0, "job_p50": {}}
    v = _physical_bound_violation(f, crit)
    assert v is None, v
    assert f["wall_clock_p50_s"] == 400.0


# --------------------------------------------------------------------------- #
# Coverage sentinel — a new `measured` detector can't ship un-guarded.
# --------------------------------------------------------------------------- #
# Every `measured` pattern either sizes a run-time wall-clock saving (and so must
# be exercised by a path-2 test above / route through a cap) or is explicitly
# exempt. If someone registers a new `measured` pattern, this fails until they
# either drive it through the bound checker here or justify an exemption — so the
# class-wide guard can't go blind to a detector added later.
_RUNTIME_MEASURED_COVERED = {"OPT19", "OPT24", "OPT25", "OPT49", "OPT50", "OPT51"}
# OPT46/OPT47/OPT64 are run-elimination bill levers, OPT65 is exact billing
# rounding waste below the cluster floor, and OPT77 credits removed setup
# payments for a consolidation that must project below the tallest job that
# REMAINS after it (not the cluster floor, which the group's own members help
# define): wall_clock is always 0/None, so there is no positive wall-clock
# bound to cap.
# Their runner-minute/bill-minute sizing is covered by
# test_tier2_wave1_detectors.py.
# OPT80 credits a checkout step's TAIL EXCESS (mean - p50) as runner-minutes and
# stamps wall_clock_p50_s = 0 by construction: the median run has no stall, so
# capping the tail cannot move the p50 merge gate. With no positive wall-clock
# claim there is no physical bound to cap — the tail runs' own improvement is
# stamped (`tail_run_wall_clock_s`) and deliberately left uncredited. Its
# runner-minute sizing is covered by test_tier2_wave1_detectors.py and
# independently re-derived by verify_report's OPT80 certificate arm.
_MEASURED_EXEMPT = {
    "OPT43", "OPT48", "OPT46", "OPT47", "OPT64", "OPT65", "OPT77", "OPT80",
}  # queue/advisory/bill-only


def test_every_measured_pattern_is_covered_or_exempt():
    declared = {p for p, c in cr._SIZING.items() if c.get("model") == "measured"}
    # OPT43's exemption must stay anchored to the real axis set, not a guessed string.
    assert "OPT43" in cr._PRESTART_AXIS_PATTERNS
    accounted = _RUNTIME_MEASURED_COVERED | _MEASURED_EXEMPT
    missing = declared - accounted
    assert not missing, (
        f"new 'measured' pattern(s) {sorted(missing)} are not covered by the "
        "sizing-cap guard. Add a path-2 test driving the detector through "
        "`_physical_bound_violation`, or add it to `_MEASURED_EXEMPT` with a "
        "reason (queue/pre-start axis, or no wall-clock saving).")


def test_runtime_measured_detectors_route_through_a_cap():
    """Structural backstop: every run-time `measured` detector that sets a
    wall-clock saving inline must route it through a cap helper, so a new one
    that forgets is caught even before a behavioral fixture exists for it.
    OPT24 caps via its payload halving (`_cap_wall_clock` on the payload or the
    intrinsic ≤own bound); OPT25/49/50/51 via `_cap_wall_clock`; OPT19 via
    `_cap_opt19_wall_clock`."""
    src = (_SCRIPTS / "collect_runs.py").read_text(encoding="utf-8")
    # OPT19's cap is wired in collect() and separately guarded (invariant 5);
    # here we assert the per-detector inline sizers reference the shared cap.
    for detector in ("_detect_opt25_shard_imbalance", "_detect_opt49_step_outliers",
                     "_detect_opt50_long_post_steps", "_detect_opt51_install_ratio"):
        body = _function_body(src, detector)
        # Match a real CALL, not a comment/docstring mention (a prose `_cap_wall_clock(`
        # in the detector's docstring must not satisfy the wiring backstop).
        called = any("_cap_wall_clock(" in ln and not ln.lstrip().startswith("#")
                     for ln in body.splitlines())
        assert called, (
            f"{detector} sets a wall-clock saving but does not route it through "
            "`_cap_wall_clock` — an inline over-credit can ship un-bounded")


def _function_body(src: str, name: str) -> str:
    """The source text of `def <name>(...)` up to the next top-level `def`."""
    lines = src.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith(f"def {name}(")), None)
    assert start is not None, f"{name} not found in collect_runs.py"
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^def \w", lines[i])), len(lines))
    return "\n".join(lines[start:end])


class _FakeGh:
    """Minimal stand-in for the gh client OPT48 reads — returns just the
    failure/success run counts its two queries need."""
    def __init__(self, fails: int, succs: int) -> None:
        self._fails, self._succs = fails, succs

    def json(self, path: str) -> dict:
        if "status=failure" in path:
            return {"total_count": self._fails}
        if "status=success" in path:
            return {"total_count": self._succs}
        return {}


def test_opt48_is_exempt_because_it_credits_no_wall_clock():
    """OPT48's exemption is anchored to behavior, not just a code comment: its
    detector emits a finding with `wall_clock_p50_s=None` (a reliability signal,
    not a sizable optimization). If a future change made OPT48 credit a
    wall-clock saving, this fails and forces it into the guarded set."""
    out = cr._detect_opt48_failure_rate(
        _FakeGh(fails=25, succs=120), "o/r", 1, "ci.yml",
        long_pole=400.0, monthly_volume=500, start_idx=0)
    assert out, "fixture should produce an OPT48 advisory finding (rate > 15%)"
    for f in out:
        assert f.get("wall_clock_p50_s") is None, (
            "OPT48 now credits a wall-clock saving — it can no longer be exempt "
            "from the sizing-cap guard; drive it through `_physical_bound_violation`")

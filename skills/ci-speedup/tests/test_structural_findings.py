"""Structural / critical-path findings — the second, non-catalog-bound finding
class that attacks the actual long pole instead of declaring it inherent cost.

These tests pin the behaviours the structural track must guarantee:

  - the long pole is DECOMPOSED into steps and the dominant one is found;
  - an expensive NON-REQUIRED critical-path check is flagged (OPT71), and a
    repo whose required-status is UNKNOWN (branch-protection 404) is never
    asserted non-required;
  - a HIGH-risk scoping candidate (OPT70) is correctly DOWN-RANKED below a
    boring safe one and is NEVER labelled a quick win;
  - structural sizing stays population-weighted and FLOOR-CAPPED (the measured
    critical-path floor caps a structural saving, never a single-PR best case);
  - the risk axis is load-bearing: a structural finding missing `risk`/
    `guardrail` is pulled out at the render boundary, never silently shipped;
  - the report renders the structural section and replaces the old inherent-cost
    dead-end with a pointer into it.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_structural_findings.py
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL_DIR / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)
import collect_runs as cr  # noqa: E402

# NOTE: `report` is deliberately NOT imported here. It is not a unique module
# name across the repo (ci-secure and starsling-runners-migration also ship
# `scripts/report.py` + `scripts/config.py`), and the whole-repo `pytest` run
# puts every skill's scripts dir on pythonpath — so a bare `import report` (and
# its internal `from config import …`) resolves to whichever skill was cached
# first. Like every other ci-speedup report test, we exercise report.py through
# its CLI via subprocess (a fresh process self-bootstraps its own sys.path), so
# ranking / section / validation behaviour is asserted on the RENDERED output,
# never via a fragile cross-skill import.


# --------------------------------------------------------------------------- #
# Fixtures — build sampled job/step timings the way the gh jobs API returns them
# --------------------------------------------------------------------------- #

def _job(name: str, steps: list[tuple[str, int]]) -> dict:
    sj, t = [], 0
    for sn, d in steps:
        sj.append({
            "name": sn,
            "started_at": f"2026-01-01T00:{t//60:02d}:{t%60:02d}Z",
            "completed_at": f"2026-01-01T00:{(t+d)//60:02d}:{(t+d)%60:02d}Z",
        })
        t += d
    return {
        "name": name, "html_url": "https://github.com/demo/demo/runs/1",
        "steps": sj, "started_at": "2026-01-01T00:00:00Z",
        "completed_at": f"2026-01-01T00:{t//60:02d}:{t%60:02d}Z",
    }


_BUILD_TEST = [("Checkout", 20), ("Install deps", 40), ("Build", 180), ("Run tests", 60)]
_LINT = [("Checkout", 20), ("Install deps", 40), ("Lint", 40)]


def _scenario(required):
    runs = [[_job("build-and-test", _BUILD_TEST), _job("lint", _LINT)]
            for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit = cr._critical_path(runs)
    crit_by_wf = {".github/workflows/ci.yml": crit}
    pr_checks = (("build-and-test", 300.0), ("CodeQL", 250.0), ("lint", 100.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    # A bare set is the COMPLETE-read case; pass a cr.RequiredChecks directly to
    # exercise a partial read, or None for "nothing readable".
    if isinstance(required, (set, frozenset)):
        required = cr.RequiredChecks(frozenset(required), complete=True)
    return cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, {}, 0)


# --------------------------------------------------------------------------- #
# Decomposition
# --------------------------------------------------------------------------- #

def test_long_pole_is_decomposed_to_its_dominant_step():
    d = cr._decompose_job_steps([_job("build-and-test", _BUILD_TEST)])
    assert d is not None
    assert d["dominant_step"] == "Build"
    assert d["dominant_category"] == "build"
    assert abs(d["dominant_share"] - 0.60) < 0.01
    # setup+build (20+40+180=240) ÷ payload (test 60) = 4.0
    assert d["redundant_ratio"] == 4.0


def test_decompose_aggregates_same_category_steps_not_single_max():
    # A multi-step pole with NO single dominant step: the job runs the same suite
    # back-to-back under four state-backend configs (four sequential `test` steps,
    # none individually dominant — 58/81/84/86s — but together ~309s of the 369s
    # job). Crowning only the single largest step (86s, 23%) understates the real
    # lever (the serial 4×-full-suite run). The decomposer must aggregate the
    # comparable same-category steps so sizing/evidence/audit scale to the whole
    # test phase, not one 23% step.
    steps = [("Checkout", 20), ("Install deps", 40),
             ("Run unit tests without db dependencies", 58),
             ("Run unit tests", 81),
             ("Run unit tests w/ redis", 84),
             ("Run unit tests w/ redis and OPLOCK_ENABLED", 86)]
    d = cr._decompose_job_steps([_job("unit-tests", steps)])
    assert d is not None
    assert d["dominant_category"] == "test"
    # The lever is the whole test phase (~309s), never the single 86s step.
    assert d["dominant_p50"] >= 300, d["dominant_p50"]
    assert d["dominant_share"] > 0.7, d["dominant_share"]
    # The dominant_step label must not crown the single max step as if it WERE the
    # entire lever — it has to make the multi-step aggregation explicit.
    assert d["dominant_step"] != "Run unit tests w/ redis and OPLOCK_ENABLED"
    assert "more test step" in d["dominant_step"]


def test_decompose_picks_aggregate_winner_over_single_largest_step_category():
    # The discriminating case the previous test misses: the SINGLE largest step is in
    # a DIFFERENT category than the aggregate winner. One `Build` step (100s) is the
    # single max, but four `test` steps (40s each = 160s aggregate) are the real lever.
    # The old single-max code (`steps[0]`) would crown `build`; the fix must pick the
    # category with the largest AGGREGATE p50 (`test`). A regression to "category of the
    # single largest step, then aggregate that category" would fail here, not above.
    steps = [("Checkout", 20), ("Build full image", 100),
             ("Run unit tests", 40), ("Run integration tests", 40),
             ("Run e2e tests", 40), ("Run smoke tests", 40)]
    d = cr._decompose_job_steps([_job("build-and-test", steps)])
    assert d is not None
    assert d["dominant_category"] == "test"          # NOT "build" (the single max)
    assert d["dominant_p50"] == 160.0                # the 4×40s test aggregate
    assert "Build full image" not in d["dominant_step"]
    assert "more test steps" in d["dominant_step"]   # 3 more → plural


def test_decompose_two_step_category_uses_singular_more_step_label():
    # Pluralization: exactly two same-category steps stand in for "+ 1 more" (singular),
    # exercising the `extra == 1` branch the four-step fixtures never hit.
    steps = [("Checkout", 20), ("Run unit tests", 90), ("Run integration tests", 80)]
    d = cr._decompose_job_steps([_job("tests", steps)])
    assert d is not None
    assert d["dominant_category"] == "test"
    assert d["dominant_p50"] == 170.0
    assert d["dominant_step"] == "Run unit tests + 1 more test step"  # singular


def test_decompose_excludes_setup_boilerplate_from_dominant_step():
    # Regression (deepgram/deepgram-python-sdk `Title Check`): a short pole whose
    # setup boilerplate out-aggregates its one load-bearing step. `Set up job` (1s)
    # + `Setup Node.js` (4s) sum to 5s of `setup`, beating the single `Install
    # commitlint` (4s, `install`). The category-by-aggregate selector USED to crown
    # the non-work `setup` phase as the "dominant step" (`Setup Node.js + 1 more
    # setup step`) — yet the cross-run check, the drilled timeline step, and the
    # agent prompt all name the longest NON-setup step (`Install commitlint`), via
    # `_NON_WORK_STEP_RE`. That printed TWO different dominant steps for one pole and
    # routed the DELIVER&VERIFY prompt at a step the OPT75 lever did not credit.
    # The decomposition must pick its dominant over load-bearing steps only, so it
    # agrees with the rest of the report.
    steps = [("Set up job", 1), ("Checkout", 2), ("Setup Node.js", 4),
             ("Install commitlint", 4), ("Run commitlint", 1)]
    d = cr._decompose_job_steps([_job("Title Check", steps)])
    assert d is not None
    assert d["dominant_category"] == "install"          # NOT "setup" (boilerplate)
    assert d["dominant_step"] == "Install commitlint"
    # ...and it must name the SAME step the generic cross-run check picks, so the
    # report's structural section and its Cross-run check / hand-off don't disagree.
    dom = bp._dominant_step_from_timeline(
        {"job_dur_s": 12, "steps": [{"name": n, "dur_s": p} for n, p in steps]})
    assert dom is not None and dom[0] == d["dominant_step"]
    # Boilerplate still counts as real setup cost in the redundant-work ratio
    # (setup+build 1+2+4+4=11 ÷ payload 0 → inf → None), never silently dropped.
    assert d["redundant_ratio"] is None


def test_decompose_all_boilerplate_job_still_returns_a_dominant():
    # Degenerate guard: a job that is ONLY non-work steps must still crown SOMETHING
    # (the fallback to the full set), not return a dominant over an empty selection.
    steps = [("Set up job", 3), ("Checkout", 5), ("Post Checkout", 2)]
    d = cr._decompose_job_steps([_job("noop", steps)])
    assert d is not None
    assert d["dominant_step"] == "Checkout"  # longest of the boilerplate-only set


def _job_on(day: str, name: str, steps: list[tuple[str, int]]) -> dict:
    """Like `_job`, but anchored on a calendar `day` so a set of sampled instances
    carries a real recency order — the audited tip's workflow version is the one the
    MOST-RECENTLY-started run executed."""
    sj, t = [], 0
    for sn, d in steps:
        sj.append({
            "name": sn,
            "started_at": f"{day}T00:{t//60:02d}:{t%60:02d}Z",
            "completed_at": f"{day}T00:{(t+d)//60:02d}:{(t+d)%60:02d}Z",
        })
        t += d
    return {
        "name": name, "html_url": "https://github.com/demo/demo/runs/1",
        "steps": sj, "started_at": f"{day}T00:00:00Z",
        "completed_at": f"{day}T00:{t//60:02d}:{t%60:02d}Z",
    }


def test_decompose_does_not_mix_workflow_versions():
    """A sampled window can SPAN a workflow migration: cortex/ripasso swapped
    `rust.yml` from `actions-rs/*` + checkout@v3 to `actions-rust-lang/setup-rust-
    toolchain` + direct `run:` + checkout@v6. Keying `by_step` by step NAME across
    instances of BOTH versions injects PHANTOM steps absent from the audited commit
    (`Run actions-rs/cargo@v1`) and double-counts a renamed operation — and the
    phantom `other`-category aggregate then out-votes the real current step
    (`cargo clippy --tests`), crowning a step that does not exist in the audited tree
    as the dominant lever. The decomposition must reconcile to a SINGLE version: the
    one the most-recently-started sampled run executed."""
    # OLD era (older runs): actions-rs/* steps. `Run actions-rs/*` classify as `other`.
    old = [("Set up job", 3), ("Run actions/checkout@v3", 5),
           ("Run actions-rs/toolchain@v1", 45),
           ("Run actions-rs/cargo@v1", 106)]
    # NEW era (the audited tip): direct cargo run-steps. `Run cargo clippy` is the
    # single longest current bar (scan); `cargo test --all` is a second test step.
    new = [("Set up job", 3), ("Run actions/checkout@v6", 5),
           ("Install actions-rust-lang/setup-rust-toolchain@v1", 30),
           ("Run cargo clippy", 111), ("cargo test --all", 70)]
    insts = ([_job_on("2026-01-01", "Clippy", old) for _ in range(4)]
             + [_job_on("2026-03-01", "Clippy", new) for _ in range(4)])
    d = cr._decompose_job_steps(insts)
    assert d is not None
    rendered = {s[0] for s in d["steps"]}
    # No step from the migrated-away version may appear — it is not in the audited tree.
    assert "Run actions-rs/cargo@v1" not in rendered
    assert "Run actions-rs/toolchain@v1" not in rendered
    assert "Run actions/checkout@v3" not in rendered
    # The crowned lever is the real current step, not a phantom `other` aggregate.
    assert d["dominant_category"] == "scan"
    assert d["dominant_step"] == "Run cargo clippy"


def test_current_version_steps_anchors_on_most_complete_recent_run_not_truncated_newest():
    """Regression: the version anchor is the MOST-COMPLETE run among the recent window, NOT simply the
    most-recently-started. A newest run that was cancelled/failed early reports only the steps that ran
    before it stopped (a strict subset), so a recency-only anchor silently drops every real step. Here
    the newest run is truncated to just `Set up job`; the real current step-set must survive."""
    def _inst(day: str, names: list[str]) -> dict:
        return {"started_at": f"{day}T00:00:00Z", "steps": [{"name": n} for n in names]}
    full = ["Set up job", "Run actions/checkout@v6", "Run cargo build", "cargo test"]
    insts = ([_inst("2026-03-01", full) for _ in range(4)]
             + [_inst("2026-03-05", ["Set up job"])])          # newest, but TRUNCATED (cancelled early)
    assert cr._current_version_steps(insts) == set(full), \
        "a truncated newest run must not shrink the anchor's step set"
    # No instance carries a usable timestamp → None (the caller then does NOT filter — unknown != stale,
    # never an empty set that would drop all steps).
    assert cr._current_version_steps([{"steps": [{"name": "x"}]}]) is None


def test_decompose_survives_a_truncated_most_recent_run():
    """End-to-end: with 4 complete current-version runs + 1 truncated newest run, the decomposition must
    still crown the real `Run cargo build` (200s), not the `Set up job` boilerplate the truncated run
    would leave as the only surviving step under a recency-only anchor."""
    full = [("Set up job", 3), ("Run actions/checkout@v6", 5), ("Run cargo build", 200), ("cargo test", 70)]
    insts = ([_job_on("2026-03-01", "build", full) for _ in range(4)]
             + [_job_on("2026-03-05", "build", [("Set up job", 3)])])   # newest = truncated
    d = cr._decompose_job_steps(insts)
    assert d is not None
    assert "Run cargo build" in {s[0] for s in d["steps"]}
    assert d["dominant_step"] == "Run cargo build", d["dominant_step"]


# --------------------------------------------------------------------------- #
# Bimodal pole: the structural decomposition must agree with the slow-mode drill
# --------------------------------------------------------------------------- #

# A BDD-test job whose Docker BUILD step is bimodal: cached-warm (118s) on most
# PRs, cold (643s) on a minority. The other (`Start Infisical`, …) and test steps
# are steady. This is the Infisical/infisical "Run BDD tests" pole: blended across
# both modes the build step's p50 (118s) sits BELOW the steady `other` aggregate
# (259s), so an all-runs decomposition crowns `other` — yet blocking_path drills
# the SLOW-mode run, where the cold build is the clear dominant step. The two
# views must not contradict; the slow mode is why the pole gates.
_BDD_OTHER = [("Start Infisical", 100), ("Wait for backend", 80),
              ("Migrate database", 79)]  # 259s of `other`, every run


def _bdd_job(build_s: int) -> dict:
    steps = [("Checkout", 10), ("Install deps", 20),
             ("Build Infisical backend Docker image with caching", build_s),
             *_BDD_OTHER, ("Run BDD tests", 30)]
    return _job("Run BDD tests", steps)


def _bdd_runs() -> list[list[dict]]:
    # 7 cached-warm runs + 3 cold runs → a real fast/slow split (slow_frac 0.30).
    return ([[_bdd_job(118)] for _ in range(7)]
            + [[_bdd_job(643)] for _ in range(3)])


def test_bimodal_pole_decomposes_slow_mode_not_blended_p50():
    runs = _bdd_runs()
    insts = [j for run in runs for j in run]
    bi = cr._critical_path(runs)["job_bimodal"]["Run BDD tests"]
    assert bi["high_p50_s"] == 962.0 and bi["low_p50_s"] == 437.0
    # Blended across both modes the build p50 is dragged under the steady `other`
    # aggregate — the (wrong) headline that contradicts the slow-mode drill.
    d_all = cr._decompose_job_steps(insts)
    assert d_all["dominant_category"] == "other"
    # Given the bimodal split, decompose the SLOW cluster the report actually
    # drills: the cold Docker build is the dominant step, by a build-cache margin.
    d_slow = cr._decompose_job_steps(insts, bimodal=bi)
    assert d_slow["dominant_category"] == "build"
    assert "Build Infisical backend Docker image" in d_slow["dominant_step"]
    assert d_slow["redundant_ratio"] > 2.0  # setup+build ≫ payload → OPT72 territory


def test_bimodal_build_pole_routes_opt72_not_opt75():
    runs = _bdd_runs()
    jpr = {".github/workflows/bdd.yml": runs}
    crit = cr._critical_path(runs)
    crit_by_wf = {".github/workflows/bdd.yml": crit}
    jba = crit["job_bimodal"]  # {"Run BDD tests": <split>}
    pr_checks = (("Run BDD tests", 500.0),)
    events = {".github/workflows/bdd.yml": {"pull_request"}}
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, None, events, {}, 0,
        job_bimodal_all=jba)
    bdd = [f for f in out if "Run BDD tests" in (f.get("affected_jobs") or [])]
    assert bdd, "expected a structural candidate for the BDD pole"
    # The gating slow mode is a cold Docker build → OPT72 (build-cache), NEVER the
    # generic OPT75 the fast/slow-blended `other`-dominant decomposition would pick.
    assert bdd[0]["pattern"] == "OPT72", bdd[0]["pattern"]


# --------------------------------------------------------------------------- #
# Data bundle: long-pole job logs captured once + referenced in the prompt
# --------------------------------------------------------------------------- #

def _job_inst(name: str, jid: int, dur_s: int, conclusion: str = "success") -> dict:
    # `conclusion` is always present on a completed job in the real jobs API, and the
    # log-fetch sites gate on it (`_job_has_log`: a queued job has no log; a job
    # cancelled before it started has none either) — so a fixture standing in for a
    # job that RAN must carry one.
    return {
        "name": name, "id": jid,
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/actions/runs/9/job/{jid}",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": f"2026-01-01T00:{dur_s // 60:02d}:{dur_s % 60:02d}Z",
    }


def test_persist_pole_logs_picks_run_nearest_the_p50(tmp_path: Path):
    # The data bundle captures each long-pole job's raw log ONCE - the instance whose
    # duration is CLOSEST to the typical (P50) time, so the drill's job total matches
    # the level-1 headline (not the slowest, which overstates; not a high-skewed
    # qualifying median). Deduped by job id, one drill fetch per pole. A short-circuit
    # / near-zero instance is dropped first so it can't be picked. Also records a
    # cross-run duration sample.
    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []

        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            self.calls.append(endpoint)
            return f"raw log body for {endpoint}"  # unrecognized -> no magnitude

    # Pole P50 is 300s; the run nearest it (310s) wins over the slowest (400s).
    poles = [{"check": "test", "workflow_file": ".github/workflows/ci.yml",
              "job": "test", "p50_s": 300.0}]
    jpr = {".github/workflows/ci.yml": [
        [_job_inst("test", 111, 2)],     # short-circuit / no-op -> dropped
        [_job_inst("test", 222, 210)],
        [_job_inst("test", 333, 310)],   # closest to the 300s P50 -> chosen
        [_job_inst("test", 444, 400)],   # slowest -> NOT chosen
    ]}
    client = FakeClient()
    manifest = cr._persist_pole_logs(client, "o/r", poles, jpr, tmp_path)
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry["job_id"] == 333                      # nearest P50, not 444 (slowest)
    assert entry["duration_s"] == 310.0
    assert entry["selected"] == "nearest-p50"
    assert (tmp_path / entry["file"]).exists()
    # One drill fetch; NO extra magnitude fetches (the fake log has no magnitude, so
    # _magnitude_sample bails before sampling more runs).
    assert sum("/logs" in c for c in client.calls) == 1
    # Cross-run duration sample excludes the dropped no-op (3 qualifying runs).
    assert len(entry["sample"]) == 3
    assert 2.0 not in [r["duration_s"] for r in entry["sample"]]


def test_persist_pole_logs_picks_real_run_when_noops_are_the_majority(tmp_path: Path):
    # The hard case: a gated job (e.g. `changed-tests`) self-skips on MOST sampled
    # PRs, so no-op instances are the MAJORITY. Here the median is ITSELF a no-op, so
    # the half-median floor alone wouldn't exclude them - the absolute `_NOOP_FLOOR_S`
    # backstop catches this case, so a real run is chosen rather than a ~0s no-op
    # whose log has nothing to drill.
    class FakeClient:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            return "log " + endpoint

    poles = [{"check": "changed-tests",
              "workflow_file": ".github/workflows/gate.yml", "job": "changed-tests"}]
    # Five instances: four 2s no-ops (the majority) + two real runs (700s, 720s).
    jpr = {".github/workflows/gate.yml": [
        [_job_inst("changed-tests", 1, 2)], [_job_inst("changed-tests", 2, 2)],
        [_job_inst("changed-tests", 3, 2)], [_job_inst("changed-tests", 4, 2)],
        [_job_inst("changed-tests", 5, 700)], [_job_inst("changed-tests", 6, 720)],
    ]}
    manifest = cr._persist_pole_logs(FakeClient(), "o/r", poles, jpr, tmp_path)
    assert len(manifest) == 1
    # A REAL run (>= 360s), never a 2s no-op, despite no-ops being the majority.
    assert manifest[0]["duration_s"] >= 360.0
    assert all(r["duration_s"] >= 360.0 for r in manifest[0]["sample"])


def test_persist_pole_logs_a_slow_outlier_does_not_displace_the_median(tmp_path: Path):
    # The representative must be the MEDIAN run, not a lone slow outlier. A floor
    # anchored to the slowest run (the old 0.5*max) would exclude the whole typical
    # cluster and force the outlier to be picked; the half-median floor keeps the
    # cluster so the nearest-P50 run wins.
    class FakeClient:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            return "log " + endpoint

    poles = [{"check": "test", "workflow_file": ".github/workflows/ci.yml",
              "job": "test", "p50_s": 100.0}]
    # A tight 100s cluster + one 300s outlier (e.g. a flaky-slow run).
    jpr = {".github/workflows/ci.yml": [
        [_job_inst("test", 1, 100)], [_job_inst("test", 2, 100)],
        [_job_inst("test", 3, 100)], [_job_inst("test", 4, 100)],
        [_job_inst("test", 5, 300)],   # outlier -> must NOT be chosen
    ]}
    manifest = cr._persist_pole_logs(FakeClient(), "o/r", poles, jpr, tmp_path)
    assert manifest[0]["duration_s"] == 100.0      # the median, not the 300s outlier
    # The outlier still rides along in the cross-run sample (it qualifies as a real
    # run); it just isn't crowned the representative.
    assert 300.0 in [r["duration_s"] for r in manifest[0]["sample"]]


def test_persist_pole_logs_drills_the_headline_runner_population(tmp_path: Path):
    # A job whose sampled runs span MORE THAN ONE runner label inside one sampling
    # window (a runner change part-way through the window) has a headline P50 scoped
    # by `_critical_path` to the label the job runs on MOST — while the drill's
    # qualifying floor is half the median of every label mixed together. When the
    # labels differ enough in speed, that floor lands ABOVE the headline population
    # and discards every instance of it as a "no-op": the representative run stamped
    # `nearest-p50`, its step timeline, and its whole cross-run sample then come from
    # a runner the headline never measured, and the drill overstates the level-1
    # figure it exists to reconcile with. The absolute no-op backstop must still
    # apply — this clamps the RELATIVE floor only.
    class FakeClient:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            return "log " + endpoint

    def _labeled(jid: int, dur_s: int, label: str) -> dict:
        job = _job_inst("test", jid, dur_s)
        job["labels"] = [label]
        return job

    wf = ".github/workflows/ci.yml"
    # Nine sampled runs of one job: four on the label it now runs on most (240s),
    # five split across the two labels it is leaving behind (600s).
    plan = ([("fast-label", 240)] * 4
            + [("slow-label-a", 600)] * 3
            + [("slow-label-b", 600)] * 2)
    runs = [[_labeled(100 + idx, dur, label)]
            for idx, (label, dur) in enumerate(plan)]

    crit = cr._critical_path(runs)
    # The headline measures the job on the label it actually runs on most.
    assert crit["job_runner"]["test"] == "fast-label"
    headline_p50 = crit["job_p50"]["test"]
    assert headline_p50 == 240.0

    poles = [{"check": "test", "workflow_file": wf, "job": "test",
              "job_p50_s": headline_p50, "p50_s": headline_p50}]
    manifest = cr._persist_pole_logs(FakeClient(), "o/r", poles, {wf: runs}, tmp_path)

    assert len(manifest) == 1
    entry = manifest[0]
    assert entry["selected"] == "nearest-p50"
    # The drilled run belongs to the population the headline named, not to a label
    # the job has largely stopped using.
    assert entry["duration_s"] == headline_p50, (
        f"drilled representative run is {entry['duration_s']}s against a "
        f"{headline_p50}s headline — the drill reconciles with nothing")
    # And the cross-run check is not drawn exclusively from the other labels.
    assert headline_p50 in [r["duration_s"] for r in entry["sample"]]


def test_persist_pole_logs_cross_run_sample_scoped_to_headline_runner(tmp_path: Path):
    # The cross-run check validates the dominant step's wall time to prove the drilled
    # run's magnitude is stable across runs — when a magnitude is available. This
    # stability evidence must be drawn from the SAME population the headline was
    # measured on, not from runs the headline never measured.
    #
    # When a job changes runners part-way through the sampling window, the headline
    # P50 is scoped (by _critical_path) to the runner it runs on MOST. But the
    # qualifying floor (_persist_pole_logs) starts as half the MIXED median of every
    # runner, and when the runners differ in speed, the clamp can still leave BOTH
    # runners' runs in qual. When the cross-run sample is built from this mixed qual,
    # it reports wall-time variation across two different machines — not stability
    # evidence for the headline job. The sample must be further scoped to the runs
    # whose runner label matches the headline label, falling back to unfiltered only
    # if no label context exists or if filtering would empty the pool.
    class FakeClient:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            return "log " + endpoint

    def _labeled(jid: int, dur_s: int, label: str) -> dict:
        job = _job_inst("test", jid, dur_s)
        job["labels"] = [label]
        return job

    wf = ".github/workflows/ci.yml"
    # Nine sampled runs: four on fast-label (240s, the dominant runner), five on
    # slow labels (600s, runners being left behind). All clear the clamped floor.
    plan = ([("fast-label", 240)] * 4
            + [("slow-label-a", 600)] * 3
            + [("slow-label-b", 600)] * 2)
    runs = [[_labeled(100 + idx, dur, label)]
            for idx, (label, dur) in enumerate(plan)]

    crit = cr._critical_path(runs)
    assert crit["job_runner"]["test"] == "fast-label"
    headline_p50 = crit["job_p50"]["test"]
    assert headline_p50 == 240.0

    poles = [{"check": "test", "workflow_file": wf, "job": "test",
              "job_p50_s": headline_p50, "p50_s": headline_p50,
              "headline_runner": crit["job_runner"]["test"]}]
    manifest = cr._persist_pole_logs(FakeClient(), "o/r", poles, {wf: runs}, tmp_path)

    assert len(manifest) == 1
    entry = manifest[0]
    # The cross-run sample reports only runs from the headline population (fast-label).
    # Runs on the slow labels (600s) MUST NOT appear — they're stability evidence
    # across two machines, not across the headline's own population.
    sample_durations = [r["duration_s"] for r in entry["sample"]]
    assert all(d == 240.0 for d in sample_durations), (
        f"cross-run sample includes runs from non-headline runners: {sample_durations}. "
        f"Stability evidence must be from the headline population (fast-label, 240s only), "
        f"not a mix of machines with different speeds.")


def test_persist_pole_logs_floor_computed_on_headline_runner_population(tmp_path: Path):
    # The qualifying floor itself must be computed on the headline runner's own runs.
    # Scoping the sample AFTER the floor is not enough: the floor starts as half the
    # MIXED median, so a slow non-headline population inflates the median that sets the
    # floor, and the floor then discards genuine headline runs as if they were no-ops.
    # The clamp to the stamped typical time bounds the floor at the P50 — but the P50
    # is the MIDDLE of the headline population, so every headline run below it is still
    # dropped, and the cross-run check reports the top half of the population as if it
    # were the whole of it.
    class FakeClient:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            return "log " + endpoint

    def _labeled(jid: int, dur_s: int, label: str) -> dict:
        job = _job_inst("test", jid, dur_s)
        job["labels"] = [label]
        return job

    wf = ".github/workflows/ci.yml"
    # The headline population spreads around its own P50 (240s); six much slower runs
    # sit on labels the job is leaving behind.
    plan = ([("fast-label", d) for d in (200, 220, 240, 260, 280)]
            + [("slow-label-a", 2000)] * 3
            + [("slow-label-b", 2000)] * 3)
    runs = [[_labeled(100 + idx, dur, label)]
            for idx, (label, dur) in enumerate(plan)]

    crit = cr._critical_path(runs)
    assert crit["job_runner"]["test"] == "fast-label"
    headline_p50 = crit["job_p50"]["test"]
    assert headline_p50 == 240.0

    poles = [{"check": "test", "workflow_file": wf, "job": "test",
              "job_p50_s": headline_p50, "p50_s": headline_p50,
              "headline_runner": crit["job_runner"]["test"]}]
    manifest = cr._persist_pole_logs(FakeClient(), "o/r", poles, {wf: runs}, tmp_path)

    assert len(manifest) == 1
    sample_durations = sorted(r["duration_s"] for r in manifest[0]["sample"])
    # Every run of the headline population is real work and belongs in the cross-run
    # check — none of them is a no-op the floor exists to exclude.
    assert sample_durations == [200.0, 220.0, 240.0, 260.0, 280.0], (
        f"cross-run sample is {sample_durations}: runs the headline itself measured "
        f"were discarded by a floor computed on a pool the headline never measured")


def test_step_timeline_is_execution_order_with_offsets():
    # The timeline preserves step ORDER and records each step's start offset (from
    # job start) + duration - the data the report needs to draw steps as a
    # succession timeline rather than left-aligned bars.
    job = {
        "name": "tests-web",
        "html_url": "https://github.com/o/r/actions/runs/9/job/77",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "id": 77,
        "steps": [
            {"name": "checkout", "number": 2, "started_at": "2026-01-01T00:00:05Z",
             "completed_at": "2026-01-01T00:00:15Z"},      # start 5s, dur 10s
            {"name": "run tests", "number": 3, "started_at": "2026-01-01T00:00:15Z",
             "completed_at": "2026-01-01T00:04:55Z"},      # start 15s, dur 280s
            {"name": "skipped step", "started_at": None, "completed_at": None},
        ],
    }
    tl = cr._step_timeline(job, "tests-web", 300.0)
    assert tl["job"] == "tests-web" and tl["job_dur_s"] == 300.0
    assert tl["run_url"] == "https://github.com/o/r/actions/runs/9"   # /job/ stripped
    # Job URL + id + step number are captured so the report can deep-link the drill.
    assert tl["job_url"] == "https://github.com/o/r/actions/runs/9/job/77"
    assert tl["job_id"] == 77
    # Order preserved; the no-timestamp step dropped (can't place it).
    assert [s["name"] for s in tl["steps"]] == ["checkout", "run tests"]
    assert tl["steps"][0] == {"name": "checkout", "number": 2,
                              "start_s": 5.0, "dur_s": 10.0}
    assert tl["steps"][1]["start_s"] == 15.0 and tl["steps"][1]["dur_s"] == 280.0


def test_persist_pole_logs_writes_step_timeline_sidecar(tmp_path: Path):
    # The data bundle saves the representative run's per-step timeline alongside its
    # log (same run), so level-2 (timeline) and the level-3 log drill stay coherent.
    class FakeClient:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            return "raw log"

    job = _job_inst("test", 333, 300)
    job["steps"] = [
        {"name": "checkout", "started_at": "2026-01-01T00:00:05Z",
         "completed_at": "2026-01-01T00:00:20Z"},
        {"name": "run tests", "started_at": "2026-01-01T00:00:20Z",
         "completed_at": "2026-01-01T00:05:00Z"},
    ]
    poles = [{"check": "test", "workflow_file": ".github/workflows/ci.yml",
              "job": "test"}]
    jpr = {".github/workflows/ci.yml": [[job]]}
    manifest = cr._persist_pole_logs(FakeClient(), "o/r", poles, jpr, tmp_path)
    entry = manifest[0]
    assert entry["steps_file"] and (tmp_path / entry["steps_file"]).exists()
    tl = json.loads((tmp_path / entry["steps_file"]).read_text())
    assert [s["name"] for s in tl["steps"]] == ["checkout", "run tests"]
    assert tl["job_dur_s"] == entry["duration_s"]


def test_persist_pole_logs_falls_back_to_dominant_step_magnitude(tmp_path: Path):
    # When the log matches NO catalog detector, the bundle must still record a
    # cross-run check - on the DOMINANT STEP's wall time across runs - so an undetected
    # pole renders a complete finding, not a bare timeline. No extra gh calls: each
    # qualifying job already carries its steps' start/end.
    class FakeClient:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            return "a log that matches no detector"   # _parse_log -> None -> fallback

    def _with_build(jid: int, build_s: int, dur_s: int) -> dict:
        j = _job_inst("Validate build outputs", jid, dur_s)
        j["steps"] = [
            {"name": "Set up job", "started_at": "2026-01-01T00:00:00Z",
             "completed_at": "2026-01-01T00:00:05Z"},
            {"name": "Build", "started_at": "2026-01-01T00:00:05Z",
             "completed_at": f"2026-01-01T00:{(5 + build_s) // 60:02d}:{(5 + build_s) % 60:02d}Z"},
        ]
        return j

    jobs = [_with_build(101, 300, 360), _with_build(102, 432, 500),
            _with_build(103, 410, 480)]
    poles = [{"check": "Validate build outputs", "job": "Validate build outputs",
              "workflow_file": ".github/workflows/lint.yml"}]
    jpr = {".github/workflows/lint.yml": [[j] for j in jobs]}
    manifest = cr._persist_pole_logs(FakeClient(), "o/r", poles, jpr, tmp_path)
    entry = manifest[0]
    assert entry["mag_file"], "undetected pole should still get a magnitude sidecar"
    mag = json.loads((tmp_path / entry["mag_file"]).read_text())
    assert mag["kind"] == "step-wall"
    assert "Build" in mag["label"] and mag["unit"] == "s"
    # Drilled run + fastest + slowest qualifying -> 3 per-run Build durations.
    assert len(mag["values"]) == 3
    assert any(v.get("drilled") for v in mag["values"])


def test_persist_pole_logs_writes_cross_run_magnitude_sample(tmp_path: Path):
    # C: the bundle records a cross-run check on the load-bearing magnitude (here the
    # turbo cache-miss rate) - the drilled run + the fastest & slowest qualifying, so
    # the single run's number has a median + range behind it. Reuses blocking_path's
    # detectors as the source of truth; costs a couple extra log fetches.
    def turbo_log(cached: int, total: int = 149) -> str:
        miss = total - cached
        return "\n".join(
            ["• Remote caching disabled"]
            + [f"cache miss, executing x{i}" for i in range(min(miss, 6))]
            + [f"Tasks:    {total} successful, {total} total",
               f"Cached:    {cached} cached, {total} total", "Time:    9m48.772s"])

    logs_by_jid = {5: turbo_log(0), 6: turbo_log(0), 7: turbo_log(15)}  # 7 is warmer

    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []

        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            self.calls.append(endpoint)
            jid = int(endpoint.split("/jobs/")[1].split("/")[0])
            return logs_by_jid.get(jid, turbo_log(0))

    poles = [{"check": "changed-tests",
              "workflow_file": ".github/workflows/gate.yml", "job": "changed-tests",
              "p50_s": 720.0}]
    jpr = {".github/workflows/gate.yml": [
        [_job_inst("changed-tests", 5, 700)],
        [_job_inst("changed-tests", 6, 720)],   # nearest the 720s P50 -> drilled
        [_job_inst("changed-tests", 7, 740)],
    ]}
    client = FakeClient()
    manifest = cr._persist_pole_logs(client, "o/r", poles, jpr, tmp_path, mag_runs=3)
    entry = manifest[0]
    assert entry["job_id"] == 6 and entry["mag_file"]
    mag = json.loads((tmp_path / entry["mag_file"]).read_text())
    assert mag["unit"] == "%" and mag["this_run"] == 100.0
    vals = sorted(v["value"] for v in mag["values"])
    assert len(mag["values"]) == 3              # drilled + fastest + slowest
    assert min(vals) < 100.0 <= max(vals)       # the warm run widens the range
    # The drill fetch + exactly 2 extra magnitude fetches (the warm/cold spread). EXACT
    # upper bound (not >=): the concurrent-batch prefetch must not over-fetch vs the old
    # sequential path — the probe is drilled (reused) + fastest + slowest = 3 distinct logs.
    assert sum("/logs" in c for c in client.calls) == 3


def test_persist_pole_logs_escalates_sample_on_a_wide_spread(tmp_path: Path):
    # Adaptive: a TIGHT 3-run probe stops cheap, but a WIDE bracket widens the sample
    # (to tell a smooth spread from two clusters). Here one run has a warm cache (low
    # rebuild %), so the probe spread is wide and the sample grows past 3.
    def turbo_log(cached: int, total: int = 149) -> str:
        miss = total - cached
        return "\n".join(
            ["• Remote caching disabled"]
            + [f"cache miss, executing x{i}" for i in range(min(miss, 6))]
            + [f"Tasks:    {total} successful, {total} total",
               f"Cached:    {cached} cached, {total} total", "Time:    9m48.772s"])

    # 8 qualifying runs (durations 700..770s); run id 1 is warm-cached (≈50% rebuilt),
    # the rest are fully cold (100%). Probe = drilled + fastest(id1) + slowest -> wide.
    cached_by_jid = {1: 75}  # the rest default to 0 (cold)

    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []

        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            self.calls.append(endpoint)
            jid = int(endpoint.split("/jobs/")[1].split("/")[0])
            return turbo_log(cached_by_jid.get(jid, 0))

    poles = [{"check": "changed-tests",
              "workflow_file": ".github/workflows/gate.yml", "job": "changed-tests",
              "p50_s": 735.0}]
    jpr = {".github/workflows/gate.yml": [
        [_job_inst("changed-tests", i, 690 + 10 * i)] for i in range(1, 9)]}
    client = FakeClient()
    manifest = cr._persist_pole_logs(client, "o/r", poles, jpr, tmp_path)
    mag = json.loads((tmp_path / manifest[0]["mag_file"]).read_text())
    assert mag["escalated"] is True
    assert len(mag["values"]) > 3                 # widened past the cheap probe
    vals = [v["value"] for v in mag["values"]]
    assert min(vals) < 60.0 and max(vals) == 100.0  # the warm run is in the sample
    # EXACT (not >=): the concurrent escalate-batch prefetch must not over-fetch vs the old
    # sequential path. The 8-qual-run fixture fetches each distinct run's log once: 1 drill +
    # 2 probe (fastest/slowest) + 5 escalate (the remaining spread) = 8. A regression that
    # fetched the whole batch regardless of `cap` would push this past 8.
    assert sum("/logs" in c for c in client.calls) == 8


def test_persist_pole_logs_no_magnitude_for_sequencing_finding(tmp_path: Path):
    # A finding with no scalar magnitude (sequential playwright) gets NO mag_file and
    # triggers NO extra fetches - the cross-run check only runs where it means
    # something.
    pw = "\n".join(["$ pnpm exec playwright test tests/smoke.spec.ts --project=desktop",
                    "$ pnpm exec playwright test tests/nav.spec.ts"])

    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []

        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            self.calls.append(endpoint)
            return pw

    poles = [{"check": "docs", "workflow_file": ".github/workflows/e2e-docs.yml",
              "job": "Docs E2E tests", "p50_s": 300.0}]
    jpr = {".github/workflows/e2e-docs.yml": [
        [_job_inst("Docs E2E tests", 1, 280)],
        [_job_inst("Docs E2E tests", 2, 320)],
    ]}
    client = FakeClient()
    manifest = cr._persist_pole_logs(client, "o/r", poles, jpr, tmp_path)
    assert manifest[0]["mag_file"] is None
    assert sum("/logs" in c for c in client.calls) == 1   # only the drill fetch


def test_pr_gate_filter_drops_push_only_deploy_checks():
    # A check that maps to a push-only workflow (deploy-to-staging) is NOT a PR
    # gate - its check-run merely rode along on a sampled SHA. It must be dropped
    # from the critical path, while a pull_request-triggered test check is kept and
    # a fileless check (CodeQL / AI review bot - maps to no workflow) is kept.
    crit_by_wf = {
        ".github/workflows/deploy.yml": {
            "job_p50": {"ecs-deploy (web, staging) / ecs-deploy": 400.0}},
        ".github/workflows/pipeline.yml": {
            "job_p50": {"tests-web (node24, pg15, mode)": 250.0}},
    }
    events = {
        ".github/workflows/deploy.yml": {"push"},
        ".github/workflows/pipeline.yml": {"pull_request"},
    }
    assert cr._is_pr_gate_check(
        "ecs-deploy (web, staging) / ecs-deploy", crit_by_wf, events) is False
    assert cr._is_pr_gate_check(
        "tests-web (node24, pg15, mode)", crit_by_wf, events) is True
    # Fileless check (no sampled job maps to it) is kept - it's a real PR check-run.
    assert cr._is_pr_gate_check("Claude Code Review", crit_by_wf, events) is True

    # A workflow whose DECLARED triggers include a PR event is kept even if its
    # SUCCESS-ONLY sample happened to catch only push runs (no sampling artifact
    # may excise a real PR gate). pr_workflows is read from the `on:` block.
    push_only_observed = {
        ".github/workflows/pipeline.yml": {"push"},  # sample caught only push
        ".github/workflows/deploy.yml": {"push"},
    }
    pr_declared = frozenset({".github/workflows/pipeline.yml"})
    assert cr._is_pr_gate_check(
        "tests-web (node24, pg15, mode)", crit_by_wf, push_only_observed,
        pr_declared) is True   # declared pull_request -> kept
    assert cr._is_pr_gate_check(
        "ecs-deploy (web, staging) / ecs-deploy", crit_by_wf, push_only_observed,
        pr_declared) is False  # not declared PR, no observed PR event -> dropped
    # A workflow observed on BOTH push and pull_request is kept.
    assert cr._is_pr_gate_check(
        "tests-web (node24, pg15, mode)", crit_by_wf,
        {".github/workflows/pipeline.yml": {"push", "pull_request"}}) is True


def test_declared_pr_workflows_reads_the_on_block_from_file_content():
    # The producer of the declared-trigger set: the REST workflow listing carries no
    # `events`, so the `on:` block is read from each workflow's file content. Covers
    # the three YAML shapes + the PyYAML `on:`->True quirk, and that an unfetchable
    # file is OMITTED (unknown != not-a-PR-gate -> caller falls back to observed).
    import base64

    files = {
        ".github/workflows/pr.yml": "on: [push, pull_request]\njobs: {}",
        ".github/workflows/pr_map.yml": "on:\n  pull_request:\n    branches: [main]\n",
        ".github/workflows/merge.yml": "on: merge_group\njobs: {}",
        ".github/workflows/deploy.yml": "on:\n  push:\n    branches: [main]\n",
        # PyYAML parses the bare key `on:` as boolean True - must still be read.
        ".github/workflows/quirk.yml": "on: pull_request_target\njobs: {}",
    }

    class FakeClient:
        def json(self, endpoint: str, allow_missing: bool = False):
            for path, body in files.items():
                if endpoint.endswith(f"contents/{path}"):
                    return {"content": base64.b64encode(body.encode()).decode()}
            return None  # e.g. private/missing file -> omitted from the set

    got = cr._declared_pr_workflows(
        FakeClient(), "o/r",
        list(files) + [".github/workflows/missing.yml"])
    assert got == frozenset({
        ".github/workflows/pr.yml", ".github/workflows/pr_map.yml",
        ".github/workflows/merge.yml", ".github/workflows/quirk.yml"})
    # The push-only deploy and the unfetchable file are NOT declared PR gates.
    assert ".github/workflows/deploy.yml" not in got
    assert ".github/workflows/missing.yml" not in got


def test_step_category_classifier_spans_ecosystems():
    cases = {
        "Checkout": "checkout", "actions/checkout@v4": "checkout",
        "pnpm install": "install", "cargo fetch": "install",
        "go mod download": "install", "Restore deps": "install",
        "turbo run build": "build", "bazel build //...": "build",
        "gradle assemble": "build", "tsc -b": "build",
        "Run vitest": "test", "pytest -q": "test", "go test ./...": "test",
        # Concatenated test-runner tokens must still classify as `test`: the
        # word-boundary alternatives (`\btest\b`, `\btests\b`, `\bunit\b`) all
        # miss "unittests"/"unittest" (no boundary inside the glued token), so a
        # bare `unittest` substring is required. Regression for the `mindee/doctr`
        # "Run unittests" step (a `coverage run -m pytest` step) that fell to
        # `other` and so escaped the PAYLOAD set used in redundant-ratio sizing.
        "Run unittests": "test", "Run unittest": "test",
        "python -m unittest": "test", "Run UnitTests": "test",
        "CodeQL Analyze": "scan", "eslint .": "scan", "trivy fs": "scan",
        # `docker build` is genuinely a build (layer-cache routing applies);
        # publish/push/upload are packaging. `docker buildx` has no `\bbuild\b`
        # word boundary (the `x`), so it falls through `build` to packaging.
        "docker build": "build", "docker push": "package",
        "docker buildx": "package",
        "Upload artifact": "package", "Publish to npm": "package",
        # "Upload build artifacts" is PACKAGING (the terminal upload), not a build —
        # the broad `\bbuild\b` must not steal it. But `mvn package` stays a build
        # (Maven's package phase is the build), so the artifact pre-entry is narrow.
        "Upload build artifacts": "package", "mvn package": "build",
    }
    for name, want in cases.items():
        assert cr._step_category(name) == want, (name, want)


def test_combined_payload_and_build_step_classifies_as_payload_not_build():
    # Regression (nrwl/nx): a SINGLE combined step that lints + tests + builds via
    # `nx affected` — named "Run Checks/Lint/Test/Build", with `playwright test`
    # running inside it — IS the payload the job exists for. The broad `\bbuild\b`
    # token must NOT steal it for the `build` category (a `_SETUP_BUILD_CATEGORIES`
    # member): binning payload work as setup/build inflates the redundant-work ratio
    # and misroutes the pole onto OPT72 "warm the build cache". Payload signals
    # (test/scan) lead `build`, mirroring the `package`-before-`build` precedent.
    assert cr._step_category("Run Checks/Lint/Test/Build") == "test"
    assert cr._step_category("Run Checks / Lint / Test / Build") == "test"
    assert cr._step_category("Lint and Build") == "scan"
    assert cr._step_category("Build and test") == "test"
    # A PURE build step (no payload token) still classifies as `build`, so the
    # genuine OPT70/OPT72 build levers are untouched.
    assert cr._step_category("Build") == "build"
    assert cr._step_category("Build production bundle") == "build"
    assert cr._step_category("nx run-many --target=build") == "build"
    # Artifact nouns joined by hyphen bind exactly like whitespace-joined ones:
    # "Build test-image" builds an artifact, it does not run tests (Greptile P1
    # on the OPT72 fix — the lookahead originally guarded only `\s+`).
    assert cr._step_category("Build test-image") == "build"
    assert cr._step_category("Build test-harness") == "build"
    assert cr._step_category("Build test image") == "build"


def test_combined_payload_build_pole_is_not_misrouted_to_opt72():
    # End-to-end (nrwl/nx pole 1): the long-pole job's main work is one combined
    # `nx affected` step ("Run Checks/Lint/Test/Build", 250s of lint+test+build)
    # plus a smaller build step and an e2e step. With the combined step binned as
    # `build` (the bug), setup+build ≫ payload → redundant_ratio > 2 → the pole
    # was crowned build-dominant and routed to OPT72 ("warm the build cache") even
    # though the step's time is test execution. Once the combined step counts as
    # PAYLOAD, its 250s move out of the setup/build numerator: the ratio drops
    # below 2, the dominant category is `test`, and the pole routes to the neutral
    # OPT75 (decompose/parallelize the payload), never OPT72.
    steps = [("Checkout", 10), ("Install deps", 30),
             ("Run Checks/Lint/Test/Build", 250),  # nx affected: lint+test+build
             ("Build storybook", 50),              # a genuine build step
             ("Run e2e tests", 40)]                # payload
    d = cr._decompose_job_steps([_job("main-linux", steps)])
    assert d is not None
    assert d["dominant_category"] == "test", d["dominant_category"]
    # The combined step's 250s is no longer redundant setup/build: ratio well < 2.
    assert d["redundant_ratio"] is not None and d["redundant_ratio"] < 2.0, d["redundant_ratio"]

    runs = [[_job("main-linux", steps)] for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit = cr._critical_path(runs)
    crit_by_wf = {".github/workflows/ci.yml": crit}
    pr_checks = (("main-linux", 380.0),)
    events = {".github/workflows/ci.yml": {"pull_request"}}
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr,
        cr.RequiredChecks(frozenset({"main-linux"}), complete=True), events, {}, 0)
    pole = [f for f in out if "main-linux" in f["title"]]
    assert pole, "expected a structural pole for the combined payload+build job"
    assert pole[0]["pattern"] == "OPT75", pole[0]["pattern"]
    assert pole[0]["decomposition"]["dominant_category"] == "test"


# --------------------------------------------------------------------------- #
# A structural finding is surfaced, with the mandatory risk axis populated
# --------------------------------------------------------------------------- #

def test_structural_candidate_surfaced_with_risk_axis():
    out = _scenario(required={"build-and-test"})
    assert out, "expected >=1 structural candidate"
    # Every structural finding carries the non-negotiable risk axis.
    for f in out:
        assert f["pattern_class"] == "structural"
        assert f["structural"] is True
        assert f["risk"] in ("LOW", "MEDIUM", "HIGH")
        assert f["guardrail"].strip()
        assert f["rollout"].strip()
        assert f["failure_mode"].strip()
    # The build-dominated required long pole has redundant_ratio 4.0 (> 2.0), so
    # it routes specifically to the redundant-work OPT72 candidate (the OPT70
    # scope branch is pinned separately in the ratio<=2.0 test below).
    top = [f for f in out if "build-and-test" in f["title"]]
    assert top and top[0]["pattern"] == "OPT72"
    assert top[0]["decomposition"]["dominant_category"] == "build"


def test_non_pole_matrix_leg_does_not_credit_wall_clock():
    # Regression (superfly/litefs): two matrix legs of the same workflow run
    # CONCURRENTLY — `Unit Tests (wal)` (200s) is the merge gate, `Unit Tests
    # (delete)` (164s) finishes ~36s BEFORE it. Speeding `delete` removes 0 merge
    # wait, so its OPT75 finding must floor to 0 wall-clock. The bug: the cascade
    # scoped `own_check_names` to the WHOLE workflow's job set, so `delete`'s
    # own_max became the workflow's top pole (`wal`, 200s) and it dodged the floor,
    # crediting a concurrent non-gating leg with phantom wall-clock and mislabeling
    # it the critical path. The fix scopes `own` to the finding's OWN leg.
    wal = [("Checkout", 10), ("Install deps", 20), ("Run unit tests", 170)]      # 200s
    delete = [("Checkout", 10), ("Install deps", 20), ("Run unit tests", 134)]   # 164s
    runs = [[_job("Unit Tests (wal)", wal), _job("Unit Tests (delete)", delete)]
            for _ in range(5)]
    jpr = {".github/workflows/push.yml": runs}
    crit = cr._critical_path(runs)
    crit_by_wf = {".github/workflows/push.yml": crit}
    pr_checks = (("Unit Tests (wal)", 200.0), ("Unit Tests (delete)", 164.0),
                 ("release linux", 57.0))
    events = {".github/workflows/push.yml": {"pull_request"}}
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, None, events, {}, 0)
    by_leg = {leg: f for f in out if f.get("pattern") == "OPT75"
              for leg in ("wal", "delete")
              if f"Unit Tests ({leg})" in (f.get("title") or "")}
    assert "delete" in by_leg, "expected an OPT75 finding for the non-pole leg"
    assert "wal" in by_leg, "expected an OPT75 finding for the gate leg"
    # The non-pole leg finishes before the gate → 0 wall-clock (a slower concurrent
    # sibling floors it), demoted to bill-only.
    assert by_leg["delete"]["wall_clock_p50_s"] == 0.0
    assert by_leg["delete"].get("realization") == "none"
    # The actual gate leg keeps a positive wall-clock saving (it IS the pole).
    assert by_leg["wal"]["wall_clock_p50_s"] > 0


def test_collapsed_sibling_leg_structural_lever_is_annotated_not_dropped():
    # Regression (silent-drop): two matrix sibling legs share a base (`build`) + the
    # same workflow, so `by_matrix` COLLAPSES them into ONE rendered representative pole
    # (the slowest leg). The FASTER, collapsed-out leg carries its OWN structural lever
    # (OPT75). Because `_structural_for_pole` no longer folds a distinct sibling leg AND
    # `_also_noticed_block` excludes every per-pole structural lever (`_is_pole_structural`),
    # that finding routes to NEITHER the rendered pole NOR the appendix — it survives only
    # in findings.json. The renderer must ANNOTATE the representative pole with it, carrying
    # the sibling's own measured numbers, so the lever never vanishes from the markdown.
    rep_leg = "build (x86_64-unknown-linux-gnu)"    # slowest → rendered representative
    fast_leg = "build (aarch64-unknown-linux-gnu)"  # faster  → collapsed out
    wf = ".github/workflows/ci.yml"
    doc = {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": 3, "sample_target": 3, "sample_complete": True,
            "poles": [
                {"check": rep_leg, "p50_s": 300.0, "workflow_file": wf, "job": rep_leg,
                 "dominant_step": "Build", "dominant_p50_s": 180.0,
                 "steps": [{"step": "Build", "category": "build", "p50_s": 180.0}]},
                {"check": fast_leg, "p50_s": 200.0, "workflow_file": wf, "job": fast_leg,
                 "dominant_step": "Build", "dominant_p50_s": 120.0,
                 "steps": [{"step": "Build", "category": "build", "p50_s": 120.0}]},
            ]},
        "findings": [{
            "pattern": "OPT75", "pattern_class": "structural", "structural": True,
            "title": f"Long Job Without Caching — {fast_leg}",
            "affected_jobs": [fast_leg],
            "risk": "MEDIUM",
            "evidence": "`Build` step runs 120s uncached on every run",
            "guardrail": "Verify the cache key invalidates on lockfile change",
            "rollout": "Land behind the existing matrix; no API change",
            "failure_mode": "A stale cache could mask a dependency bump",
            "fix_recipe_anchor": "opt75",
        }],
    }
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    # The two legs collapse: only the slow representative leg headlines as Long pole 1.
    assert "Long pole 1:" in md and "x86_64" in md
    assert "Long pole 2:" not in md, "the sibling legs must collapse to one pole"
    # The faster sibling leg's lever must appear in the RENDERED markdown, attributed to
    # that leg — not dropped to findings.json only (the silent drop this guards).
    assert "Sibling matrix leg" in md
    assert fast_leg in md
    assert "OPT75" in md
    assert "`Build` step runs 120s uncached" in md   # carries the leg's OWN numbers
    assert "Verify the cache key invalidates" in md   # the risk-axis guardrail surfaced


# ── Identical-lever sibling collapse (issue #53) ──────────────────────────────
_OPT75_GUARDRAIL = ("carry the guardrail of the routed lever (e.g. OPT70's full-suite "
                    "fallback if the dominant step is a test being scoped); never present "
                    "the decomposition as free")
_OPT75_ROLLOUT = ("the routed lever's rollout; re-measure the pole's p50 after the "
                  "dominant step is attacked — the next-largest step becomes the target")
_OPT75_FAILURE = ("the dominant-step remedy ranges from LOW (cache an install) to HIGH "
                  "(scope a test/build, inheriting OPT70) — the candidate carries the "
                  "risk of whichever specific lever its dominant category routes to")
# Dash-free fragments for occurrence-counting: the renderer normalizes the em-dashes in the
# full constants above to ASCII hyphens (verify_report enforces ASCII-only), so count on a
# stable substring that carries no dash. Each fragment is unique to its boilerplate line.
_GUARDRAIL_FRAG = "never present the decomposition as free"
_ROLLOUT_FRAG = "re-measure the pole's p50 after the dominant step is attacked"
_FAILURE_FRAG = "the dominant-step remedy ranges from LOW"


def _guard_shard_leg_finding(shard: str, check_p50: int, share_pct: int,
                             *, step_suffix: bool, category: str = "other",
                             base_step: str = "Verify the guards can actually fail "
                                               "(mutation registry)") -> dict:
    """An OPT75 structural finding modelled on the LIVE internal-dev-repo guard-shard
    matrix (issue #53). `step_suffix` toggles the `+ N more other step` category-
    aggregation suffix that leg 1/4 lacks but 2/4/3/4/4/4 carry — the whole point of the
    collapse identity is that these are the SAME step modulo that suffix."""
    leg = f"guard shard {shard}"
    step = base_step + (" + 1 more other step" if step_suffix else "")
    return {
        "pattern": "OPT75", "pattern_class": "structural", "structural": True,
        "title": f"The long pole's time is one addressable step — `{leg}`",
        "affected_jobs": [leg], "workflow_file": ".github/workflows/ci.yml",
        "risk": "MEDIUM",
        "evidence": (f"critical-path check `{leg}` ({check_p50}s): dominant step "
                     f"`{step}` ({category}, {share_pct}% of job `{leg}`)"),
        "guardrail": _OPT75_GUARDRAIL, "rollout": _OPT75_ROLLOUT,
        "failure_mode": _OPT75_FAILURE,
        "fix_recipe_anchor": "opt75--long-pole-optimize-or-relocate-the-dominant-step",
        "decomposition": {"dominant_step": step, "dominant_category": category,
                          "dominant_p50_s": check_p50 * share_pct / 100.0,
                          "dominant_share": share_pct / 100.0,
                          "job_p50_s": float(check_p50)},
    }


def _guard_shard_doc(findings: list[dict], legs: list[tuple[str, int, int]]) -> dict:
    """A minimal blocking-path doc whose only matrix is `guard shard N/4`. `legs` is
    `(shard, job_p50, dominant_p50)` per leg; the slowest becomes the rendered rep."""
    wf = ".github/workflows/ci.yml"
    poles = [{"check": f"guard shard {s}", "p50_s": float(jp), "workflow_file": wf,
              "job": f"guard shard {s}", "dominant_step": "Verify the guards can "
              "actually fail (mutation registry)", "dominant_p50_s": float(dp),
              "dominant_share": dp / jp,
              "steps": [{"step": "Verify the guards can actually fail (mutation "
                         "registry)", "category": "other", "p50_s": float(dp)},
                        {"step": "Build", "category": "build", "p50_s": 24.0}]}
             for s, jp, dp in legs]
    return {
        "repo": "o/r", "scanned_at": "2026-07-20T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": {"sampled_pr_count": 3, "sample_target": 3,
                             "sample_complete": True, "poles": poles},
        "findings": findings,
    }


def _long_pole_2_span(md: str) -> str:
    """The rendered body from the sole Long-pole header to the next `## ` (or end)."""
    m = re.search(r"^## .*Long pole \d+:.*$", md, re.MULTILINE)
    assert m, "expected a Long-pole section"
    rest = md[m.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def test_identical_lever_sibling_legs_collapse_to_one_compact_line():
    # LIVE repro (issue #53): four `guard shard N/4` legs of one matrix all carry the SAME
    # OPT75 lever on the SAME dominant step. The old renderer emitted the pole's own block
    # PLUS three full "Sibling matrix leg … also carries a structural lever" blocks, each
    # repeating the identical guardrail/rollout/failure-mode boilerplate (~40 lines to say
    # one thing). The collapse keeps the pole's own block ONCE and folds the three siblings
    # into a single compact per-leg measurement line.
    legs = [("2/4", 159, 113), ("1/4", 158, 112),   # 2/4 slowest → rendered rep
            ("3/4", 152, 105), ("4/4", 149, 102)]
    findings = [
        _guard_shard_leg_finding("2/4", 159, 71, step_suffix=True),   # the rep's OWN lever
        _guard_shard_leg_finding("1/4", 158, 71, step_suffix=False),  # suffixless
        _guard_shard_leg_finding("3/4", 152, 69, step_suffix=True),
        _guard_shard_leg_finding("4/4", 149, 68, step_suffix=True),
    ]
    md = bp.render(_guard_shard_doc(findings, legs), {}, {}, {}, "2026-07-20")
    span = _long_pole_2_span(md)
    # The four legs collapse to ONE rendered pole.
    assert "Long pole 1:" in md
    assert "Long pole 2:" not in md, "the four shards must collapse to one pole"
    # The boilerplate renders EXACTLY ONCE within the pole section (was 4× before the fix).
    assert span.count(_GUARDRAIL_FRAG) == 1, "guardrail boilerplate must appear once"
    assert span.count(_ROLLOUT_FRAG) == 1
    assert span.count(_FAILURE_FRAG) == 1
    # No repeated full sibling block header.
    assert "also carries a structural lever" not in span, \
        "identical siblings must not render full blocks"
    # The compact line names EVERY collapsed leg with its OWN measured p50 + share (anti-drop).
    assert "Sibling legs carry the same lever on the same step" in span
    for leg, p50, share in (("1/4", "158s", "71%"), ("3/4", "152s", "69%"),
                            ("4/4", "149s", "68%")):
        assert f"`guard shard {leg}`" in span
        assert f"{p50} · {share}" in span, (leg, p50, share)


def test_suffix_normalized_leg_collapses_with_suffixless_sibling():
    # A leg whose dominant step carries the "+ 1 more other step" aggregation suffix must
    # collapse with a suffixless leg of the SAME base step — the suffix is a cosmetic count
    # difference, not a different lever. Rep (2/4) carries the suffix; the sibling (1/4)
    # does not, yet both name `Verify the guards can actually fail (mutation registry)`.
    legs = [("2/4", 159, 113), ("1/4", 158, 112)]
    findings = [
        _guard_shard_leg_finding("2/4", 159, 71, step_suffix=True),
        _guard_shard_leg_finding("1/4", 158, 71, step_suffix=False),
    ]
    md = bp.render(_guard_shard_doc(findings, legs), {}, {}, {}, "2026-07-20")
    span = _long_pole_2_span(md)
    assert "also carries a structural lever" not in span
    assert "Sibling legs carry the same lever on the same step" in span
    assert "`guard shard 1/4` 158s · 71%" in span


def test_distinct_step_sibling_keeps_full_block_alongside_collapsed_ones():
    # Mixed matrix: two siblings share the pole's dominant step (collapse) while a third
    # runs a genuinely DIFFERENT dominant step (a test suite, not the mutation guard) — its
    # lever is not the same one, so it KEEPS its full "Sibling matrix leg" block (today's
    # behaviour) while the identical ones fold into the compact line.
    legs = [("2/4", 159, 113), ("1/4", 158, 112), ("3/4", 152, 105), ("4/4", 149, 102)]
    distinct = _guard_shard_leg_finding(
        "4/4", 149, 68, step_suffix=False, category="test",
        base_step="Run the integration suite")
    findings = [
        _guard_shard_leg_finding("2/4", 159, 71, step_suffix=True),
        _guard_shard_leg_finding("1/4", 158, 71, step_suffix=False),
        _guard_shard_leg_finding("3/4", 152, 69, step_suffix=True),
        distinct,
    ]
    md = bp.render(_guard_shard_doc(findings, legs), {}, {}, {}, "2026-07-20")
    span = _long_pole_2_span(md)
    # The identical siblings (1/4, 3/4) collapse to the compact line …
    assert "Sibling legs carry the same lever on the same step" in span
    assert "`guard shard 1/4` 158s · 71%" in span
    assert "`guard shard 3/4` 152s · 69%" in span
    # … and the compact line does NOT swallow the distinct leg.
    assert "`guard shard 4/4`" not in span.split(
        "Sibling legs carry the same lever")[1].split("\n")[0]
    # … while the distinct-step leg (4/4) KEEPS its full block, named and boilerplated.
    assert "Sibling matrix leg `guard shard 4/4` also carries a structural lever" in span
    assert "Run the integration suite" in span
    # Boilerplate now appears twice: once for the pole, once for the distinct full block.
    assert span.count(_GUARDRAIL_FRAG) == 2


def test_leg_measure_share_search_scoped_after_duration_match():
    # #90 bot review: a step NAME containing "% of job" before the real share must not
    # shadow it — the share is parsed only from the text after the `(Ns):` duration.
    f = {"evidence": "critical-path check `guards` (158s): dominant step "
                     "`Run 90% of job tests` (test, 71% of job `guards`)",
         "decomposition": {"dominant_step": "Run 90% of job tests",
                           "dominant_category": "test"}}
    import blocking_path as bp
    assert bp._leg_measure(f) == ("158s", "71%")


def test_leg_measure_ignores_parenthesized_duration_token_in_check_name():
    # A check NAME that itself carries a parenthesized `(Ns)` token — e.g. a timeout-matrix
    # leg rendered as `test (3s)` — must NOT shadow the REAL check duration. The evidence
    # grammar always closes the check duration with `s):`, so `_leg_measure` anchors there
    # and reads 158s, never the 3s buried in the name. A wrong-but-plausible per-leg number
    # would silently misreport the collapsed line's measurement.
    f = {"evidence": "critical-path check `test (3s)` (158s): dominant step "
                     "`Run suite` (test, 71% of job `test (3s)`)"}
    assert bp._leg_measure(f) == ("158s", "71%")
    # A parenthesized `(45s)` inside the STEP name (after the duration) is likewise ignored —
    # `.search` still returns the first `s):`-anchored match.
    g = {"evidence": "critical-path check `x` (158s): dominant step "
                     "`Run timeout (45s) guard` (test, 69% of job `x`)"}
    assert bp._leg_measure(g) == ("158s", "69%")
    # Malformed evidence with no numbers falls back to the structured decomposition, never
    # renders `None`/garbage.
    h = {"evidence": "no numbers here",
         "decomposition": {"job_p50_s": 149.4, "dominant_share": 0.683}}
    assert bp._leg_measure(h) == ("149s", "68%")


def test_dominant_step_base_strips_only_the_aggregation_suffix():
    # The `+ N more <category> step(s)` aggregation suffix is stripped so two legs that differ
    # ONLY by how many same-category steps they aggregate compare equal (issue #53) …
    assert bp._dominant_step_base("Verify guards + 1 more other step") == "Verify guards"
    assert bp._dominant_step_base("Install deps + 2 more install steps") == "Install deps"
    # … but a REAL step name that merely contains `+ N more <noun>` (noun != step) keeps ALL
    # its content — the greedy strip used to clip `Deploy + 2 more regions` down to `Deploy`.
    assert bp._dominant_step_base("Deploy + 2 more regions") == "Deploy + 2 more regions"
    assert bp._dominant_step_base("Run step + 3 laps more work") == "Run step + 3 laps more work"
    assert bp._dominant_step_base("Build + test suite") == "Build + test suite"


def test_pole_structural_lever_below_top_n_is_disclosed_not_dropped():
    # No-silent-drop regression at the RAISED render depth: the renderer now shows the top
    # _TOP_WORKFLOWS (5) poles. A per-pole structural lever (OPT75) on a check that ranks
    # BELOW the top 5 — in a workflow with no rendered pole — joins neither
    # `_structural_for_pole` (its pole isn't rendered) nor the off-path appendix
    # (`_is_pole_structural` excludes it), so it would survive only in findings.json. It must
    # be DISCLOSED as a count instead. Uses SIX distinct matrices so one genuinely ranks out
    # past the depth-5 render (the depth-2 version of this test no longer exercises the drop).
    wfs = [f".github/workflows/{c}.yml" for c in "abcdef"]
    pole = lambda check, wf, p50, dom: {
        "check": check, "p50_s": p50, "workflow_file": wf, "job": check,
        "dominant_step": "Build", "dominant_p50_s": dom, "dominant_share": dom / p50,
        "steps": [{"step": "Build", "category": "build", "p50_s": dom}]}
    # check `f6` (slowest step but lowest p50) ranks 6th → past the 5 rendered poles.
    ranked_out_lever = {
        "pattern": "OPT75", "pattern_class": "structural", "structural": True,
        "title": "Long Job Without Caching — f6", "affected_jobs": ["f6"],
        "workflow_file": wfs[5],
        "risk": "MEDIUM", "evidence": "`Build` runs 150s uncached on f6",
        "fix_recipe_anchor": "opt75"}
    poles = [pole("f1", wfs[0], 600.0, 400.0), pole("f2", wfs[1], 500.0, 350.0),
             pole("f3", wfs[2], 400.0, 300.0), pole("f4", wfs[3], 350.0, 250.0),
             pole("f5", wfs[4], 300.0, 200.0), pole("f6", wfs[5], 250.0, 150.0)]
    doc = {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300, "workflows_analyzed": 6},
        "pr_critical_path": {
            "sampled_pr_count": 3, "sample_target": 3, "sample_complete": True,
            "poles": poles,
        },
        "findings": [ranked_out_lever],
    }
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    # The top 5 workflows render as poles; f6 ranks out.
    assert "Long pole 1:" in md and "Long pole 5:" in md
    assert "Long pole 6:" not in md
    # f6's lever renders nowhere as a pole or appendix row → must be DISCLOSED.
    assert "structural lever(s) on lower-ranked poles" in md
    assert "OPT75" in md

    # Negative: when the SAME lever is on a RENDERED pole (`f1`), it renders AS that pole
    # and the disclosure NOTE must NOT appear (the fix must not over-fire).
    doc_rendered = {**doc, "findings": [{**ranked_out_lever,
                    "title": "Long Job Without Caching — f1", "affected_jobs": ["f1"],
                    "workflow_file": wfs[0],
                    "evidence": "`Build` runs 400s uncached on f1"}]}
    md2 = bp.render(doc_rendered, {}, {}, {}, "2026-06-08")
    assert "structural lever(s) on lower-ranked poles" not in md2
    assert "OPT75" in md2   # still rendered — as the pole's structural block


def test_unrendered_lever_on_managed_check_is_disclosed_as_managed_not_below():
    # R3 partition (the OTHER half): an OPT75 lever whose home check has no real workflow file
    # — a managed/app check gets a NAME-STUB `workflow_file` ("Greptile") from collect_runs'
    # `check_name.split(" ")[0]` fallback — must be disclosed as "managed/unresolved", NOT
    # mis-claimed as "ranks below the rendered poles", and NEVER silently dropped.
    wf_a = ".github/workflows/a.yml"
    pole = lambda check, wf, p50, dom: {
        "check": check, "p50_s": p50, "workflow_file": wf, "job": check,
        "dominant_step": "Build", "dominant_p50_s": dom, "dominant_share": dom / p50,
        "steps": [{"step": "Build", "category": "build", "p50_s": dom}]}
    managed_lever = {
        "pattern": "OPT75", "pattern_class": "structural", "structural": True,
        "title": "Long Job Without Caching — Greptile Review", "affected_jobs": ["Greptile Review"],
        "workflow_file": "Greptile",  # NAME-STUB — no real workflow file
        "risk": "MEDIUM", "evidence": "`Build` runs 150s uncached", "fix_recipe_anchor": "opt75"}
    doc = {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300, "workflows_analyzed": 2},
        "pr_critical_path": {
            "sampled_pr_count": 3, "sample_target": 3, "sample_complete": True,
            "poles": [pole("build", wf_a, 400.0, 300.0)]},
        "findings": [managed_lever],
    }
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    assert "structural lever(s) on managed/unresolved checks" in md
    assert "structural lever(s) on lower-ranked poles" not in md  # not mis-claimed
    assert "OPT75" in md


def test_shared_substep_rare_cluster_is_demoted_to_bill_only():
    # Regression (lancedb/lancedb npm-publish.yml): the cluster jobs are the actual pole (slowest
    # job) on FEWER than `_POLE_RECUR_FLOOR` sampled PRs, so the spine demotes them off the typical
    # merge-gating critical path. OPT73 must NOT then credit an on-critical-path wall-clock win to
    # them — otherwise the report banners the finding "sits ON the merge-gating critical path"
    # while the spine's own footnote calls the same jobs one-path throughput/cost. The cascade
    # floors by population-weighted MAGNITUDE but not by pole frequency, so the demotion is applied
    # explicitly (keyed on `check_pole_freq`, the spine's own signal). The bill saving survives.
    a = [("Checkout", 20), ("Install deps", 80), ("Build", 180), ("Bundle app", 40)]
    b = [("Checkout", 20), ("Install deps", 80), ("Build", 180), ("Bundle web", 40)]
    runs = [[_job("app", a), _job("web", b)] for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit_by_wf = {".github/workflows/ci.yml": cr._critical_path(runs)}
    pr_checks = (("app", 320.0), ("web", 320.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    common = dict(vol_by_wf={".github/workflows/ci.yml": 1000})

    # Control: cluster is the actual pole on a majority of the 20 sampled PRs → a real recurring
    # gate, the wall-clock credit is kept (the demotion must not over-fire).
    out_typical = cr._detect_shared_substep(
        crit_by_wf, jpr, events, pr_checks, [], 0,
        check_present={"app": 18, "web": 18}, present_n_pr=20,
        check_pole_freq={"app": 18, "web": 18}, **common)
    typ = [f for f in out_typical if f["pattern"] == "OPT73"][0]
    assert (typ.get("wall_clock_p50_s") or 0) > 0, (
        "a typical (recurring-gate) cluster keeps its wall-clock credit")

    # One-path cluster: neither job is ever the actual pole (pole_freq 0 < floor) → demote, even
    # though they're present on many PRs (the expo/expo case presence got wrong).
    out_rare = cr._detect_shared_substep(
        crit_by_wf, jpr, events, pr_checks, [], 0,
        check_present={"app": 18, "web": 18}, present_n_pr=20,
        check_pole_freq={"app": 0, "web": 0}, **common)
    rare = [f for f in out_rare if f["pattern"] == "OPT73"][0]
    assert rare.get("wall_clock_p50_s") == 0.0, (
        "a rarely-run cluster the spine demotes must not carry an on-critical-path "
        "wall-clock credit")
    assert rare.get("realization") == "none"
    assert rare.get("tier") == 2
    # The bill saving is untouched — this stays an actionable runner-minute finding.
    assert rare.get("runner_min_saving") == typ.get("runner_min_saving")
    assert "typical merge-gating critical path" in (rare.get("size_note") or "")


# --------------------------------------------------------------------------- #
# #56 — OPT73 cluster anchor + on-path label are presence-weighted: a MINORITY-
# present workflow (2/20) cannot crown the typical-PR headline, and a rare
# minority-present leg cannot anchor the sizing / lead the Evidence.
# --------------------------------------------------------------------------- #
def _playwright_cluster(pole_freq: dict[str, int]):
    """The playwright shape: one workflow, four matrix legs sharing a `Run test` step, the FATTEST
    leg (`Test msedge-dev on macos-latest`) present-but-never-the-pole, and `Windows (firefox)` the
    only leg that clears the recurring-gate floor. The caller sets `check_pole_freq` to choose the
    minority-workflow (2/20) vs majority-workflow (13/20) shape."""
    shared = ("Run test", 260)
    msedge = [("Checkout", 20), ("Run test", 300)]           # fattest (320) — the rare anchor
    firefox = [("Checkout", 20), ("Run test", 290)]          # 310 — the only eligible leg
    chrome = [("Checkout", 20), shared]                      # 280
    android = [("Checkout", 20), ("Run test", 200)]          # 220
    runs = [[_job("Test msedge-dev on macos-latest", msedge), _job("Windows (firefox)", firefox),
             _job("Test chrome on macos-latest", chrome), _job("Android", android)]
            for _ in range(5)]
    wf = ".github/workflows/tests_secondary.yml"
    jpr = {wf: runs}
    crit_by_wf = {wf: cr._critical_path(runs)}
    pr_checks = (("Test msedge-dev on macos-latest", 320.0), ("Windows (firefox)", 310.0),
                 ("Test chrome on macos-latest", 280.0), ("Android", 220.0))
    events = {wf: {"pull_request"}}
    return cr._detect_shared_substep(
        crit_by_wf, jpr, events, pr_checks, [], 0,
        check_present={"Test msedge-dev on macos-latest": 2, "Windows (firefox)": 2,
                       "Test chrome on macos-latest": 2, "Android": 2},
        present_n_pr=20, check_pole_freq=pole_freq,
        vol_by_wf={wf: 1000})


def test_opt73_minority_workflow_cluster_is_demoted_to_bill_only():
    # (b) the playwright #56 defect: `tests_secondary.yml` gates only 2/20 sampled PRs (only
    # `Windows (firefox)` is ever the per-PR slowest, pole_freq 2). `_affected_jobs_all_rare` does
    # NOT fire (firefox clears the pole floor), so the PRE-fix engine credited a wall-clock win and
    # let it crown the typical-PR bottom line. A minority-gate WORKFLOW is off the typical merge
    # path, so its OPT73 wall-clock must floor to bill-only; the runner-minute saving survives.
    out = _playwright_cluster(
        {"Test msedge-dev on macos-latest": 0, "Windows (firefox)": 2,
         "Test chrome on macos-latest": 0, "Android": 0})
    f = [f for f in out if f["pattern"] == "OPT73"][0]
    assert f.get("wall_clock_p50_s") == 0.0, "a 2/20 minority-gate cluster must not crown wall-clock"
    assert f.get("realization") == "none" and f.get("tier") == 2
    assert (f.get("runner_min_saving") or 0) > 0, "the bill (runner-minute) saving survives"
    assert "per-PR slowest workflow on only ~2/20" in (f.get("size_note") or "")
    # The Evidence + affected_jobs must ANCHOR on the presence-eligible leg (`Windows (firefox)`),
    # never on the fatter-but-minority-present `Test msedge-dev on macos-latest`.
    assert f["affected_jobs"][0] == "Windows (firefox)"
    assert "Windows (firefox)" in (f.get("evidence") or "")
    assert "Test msedge-dev on macos-latest" not in (f.get("evidence") or "")


def test_opt73_majority_workflow_with_one_rare_leg_reanchors_but_keeps_wallclock():
    # (a) a MAJORITY-gate workflow (13/20 via `Windows (firefox)`) with ONE minority-present fat leg
    # keeps its wall-clock credit (it IS on the typical merge path) but must NOT anchor its Evidence
    # on the excluded leg — re-anchor on the most-present eligible leg instead.
    out = _playwright_cluster(
        {"Test msedge-dev on macos-latest": 0, "Windows (firefox)": 13,
         "Test chrome on macos-latest": 0, "Android": 0})
    f = [f for f in out if f["pattern"] == "OPT73"][0]
    assert (f.get("wall_clock_p50_s") or 0) > 0, "a majority-gate cluster keeps its wall-clock credit"
    assert f.get("realization") == "direct"
    # Re-anchored on the eligible leg, not the fatter minority-present one.
    assert f["affected_jobs"][0] == "Windows (firefox)"
    assert "Test msedge-dev on macos-latest" not in (f.get("evidence") or "")
    # The bill still spans EVERY leg that runs the shared step (presence-weighting touches the
    # wall-clock/anchor, never the runner-minute reality that all four legs pay the step).
    assert set(f["affected_jobs"]) == {"Test msedge-dev on macos-latest", "Windows (firefox)",
                                       "Test chrome on macos-latest", "Android"}


def test_opt73_leg_presence_eligible_mirrors_the_spine_is_rare():
    # The anchor-eligibility predicate is the inverse of the spine's `is_rare` (exact complement for
    # any spine-ranked leg; unknown-to-the-map legs treated as eligible), one notion shared.
    req = frozenset({"required-leg"})
    # Below the sample floor → inert, everything eligible.
    assert cr._leg_presence_eligible("x", {"x": 0}, cr._RARE_PRESENCE_MIN_PR - 1, frozenset()) is True
    # Required leg gates by definition → eligible.
    assert cr._leg_presence_eligible("required-leg", {"required-leg": 0}, 20, req) is True
    # Unknown to the frequency map → never exclude on partial info → eligible.
    assert cr._leg_presence_eligible("y", {}, 20, frozenset()) is True
    # The actual pole on >= floor PRs → eligible; below it → rare/ineligible.
    assert cr._leg_presence_eligible("z", {"z": cr._POLE_RECUR_FLOOR}, 20, frozenset()) is True
    assert cr._leg_presence_eligible("z", {"z": cr._POLE_RECUR_FLOOR - 1}, 20, frozenset()) is False


def test_opt73_workflow_gates_minority_threshold():
    # Mirrors the renderer's majority split; inert below the sample floor.
    assert cr._workflow_gates_minority(2, 20) is True            # 2/20 → minority
    assert cr._workflow_gates_minority(10, 20) is True           # exactly half → minority
    assert cr._workflow_gates_minority(11, 20) is False          # majority
    assert cr._workflow_gates_minority(0, cr._RARE_PRESENCE_MIN_PR - 1) is False  # noise → inert


def test_opt73_workflow_gate_freq_summed_in_check_name_domain_not_job_names():
    # Regression (PR #57 review — code-reviewer + silent-failure-hunter): the workflow gate count
    # feeding `_workflow_gates_minority` must be summed in `check_pole_freq`'s CHECK-CONTEXT key
    # domain, attributing each check to its workflow via `_map_check_to_job` — NOT by looking up
    # `crit["job_p50"]`'s raw job-API names. When a check's run-context name diverges from its job
    # name (the common case the `_map_check_to_job` layer exists for), a raw
    # `check_pole_freq.get(job_name, 0)` sum scores every check 0, dragging a genuine MAJORITY-gating
    # workflow under the minority line and spuriously flooring a real wall-clock lever to bill-only —
    # the CONVERSE of the burial this PR prevents, and a demotion the verify guard (which re-derives
    # by `workflow_file`) cannot backstop once `wall_clock` is already 0.
    runs = [[_job("Windows (firefox)", [("Checkout", 20), ("Run test", 290)]),
             _job("Test msedge-dev on macos-latest", [("Checkout", 20), ("Run test", 300)])]
            for _ in range(5)]
    wf = ".github/workflows/tests_secondary.yml"
    crit_by_wf = {wf: cr._critical_path(runs)}          # job_p50 keyed by job-API names
    # `check_pole_freq` is keyed by check-run CONTEXT names (`CI / <job>`), which differ from the
    # job-API names in `crit["job_p50"]` — the exact divergence the raw job-name lookup mishandles.
    cpf_majority = {"CI / Windows (firefox)": 13, "CI / Test msedge-dev on macos-latest": 0}
    cpf_minority = {"CI / Windows (firefox)": 2, "CI / Test msedge-dev on macos-latest": 0}

    # The fix maps each divergent context name back to its workflow, so the count is faithful (13,
    # not 0). A raw `sum(cpf.get(j, 0) for j in crit["job_p50"])` would score 0 here — the bug.
    assert cr._workflow_gate_freq(cpf_majority, crit_by_wf, wf) == 13
    assert cr._workflow_gate_freq(cpf_minority, crit_by_wf, wf) == 2
    assert sum(cpf_majority.get(j, 0) for j in crit_by_wf[wf]["job_p50"]) == 0, \
        "sanity: the raw job-name lookup DOES miss under context-name divergence (the pre-fix bug)"

    # So the majority workflow is NOT demoted (13/20 is a majority), while the minority still is.
    assert cr._workflow_gates_minority(
        cr._workflow_gate_freq(cpf_majority, crit_by_wf, wf), 20) is False
    assert cr._workflow_gates_minority(
        cr._workflow_gate_freq(cpf_minority, crit_by_wf, wf), 20) is True


def test_shared_substep_demotion_note_states_frequency_not_presence():
    # F2 (PR #126 4th adversarial review): the OPT73 rare-cluster demotion fires on pole FREQUENCY
    # (`_affected_jobs_all_rare`), which by design includes a cluster present on EVERY PR that is
    # never the per-PR slowest. The `size_note` used to narrate the PRESENCE ("run on only ~20/20
    # sampled PRs (opt-in / path-gated)") — self-contradictory for a universally-present cluster and
    # the wrong reason. It must state the frequency reason (how often it is actually the gate).
    a = [("Checkout", 20), ("Install deps", 80), ("Build", 180), ("Bundle app", 40)]
    b = [("Checkout", 20), ("Install deps", 80), ("Build", 180), ("Bundle web", 40)]
    runs = [[_job("app", a), _job("web", b)] for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit_by_wf = {".github/workflows/ci.yml": cr._critical_path(runs)}
    pr_checks = (("app", 320.0), ("web", 320.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    # Cluster present on ALL 20 sampled PRs, but never the per-PR slowest (pole_freq 0).
    out = cr._detect_shared_substep(
        crit_by_wf, jpr, events, pr_checks, [], 0,
        check_present={"app": 20, "web": 20}, present_n_pr=20,
        check_pole_freq={"app": 0, "web": 0},
        vol_by_wf={".github/workflows/ci.yml": 1000})
    note = [f for f in out if f["pattern"] == "OPT73"][0].get("size_note") or ""
    # No presence-based contradiction, and the frequency reason (0/20 actual-gate) is stated.
    assert "run on only ~20/20 sampled PRs (opt-in / path-gated)" not in note
    assert "opt-in / path-gated" not in note
    assert "the actual slowest check a PR waits on, on only ~0/20 sampled PRs" in note


# --------------------------------------------------------------------------- #
# Expensive non-required check is flagged; unknown required-status is never
# asserted to be non-required
# --------------------------------------------------------------------------- #

def test_shared_substep_across_cluster_is_a_floor_lever():
    # Two cluster jobs each run the SAME named `Build` step (180s) — fixing it once
    # lowers the whole cluster floor (OPT73), the one lever that beats the
    # long_pole−floor cap. The job-specific steps ("Bundle app"/"Bundle web") differ
    # by name, so the NAME-based detector does NOT treat them as one shared step.
    a = [("Checkout", 20), ("Install deps", 80), ("Build", 180), ("Bundle app", 40)]
    b = [("Checkout", 20), ("Install deps", 80), ("Build", 180), ("Bundle web", 40)]
    runs = [[_job("app", a), _job("web", b)] for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit = cr._critical_path(runs)
    crit_by_wf = {".github/workflows/ci.yml": crit}
    pr_checks = (("app", 320.0), ("web", 320.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    out = cr._detect_shared_substep(
        crit_by_wf, jpr, events, pr_checks, [], 0,
        vol_by_wf={".github/workflows/ci.yml": 1000})
    opt73 = [f for f in out if f["pattern"] == "OPT73"]
    assert opt73, "expected a shared-substep (OPT73) cluster-floor lever"
    f = opt73[0]
    assert f["risk"] == "LOW"
    # Credited across BOTH cluster jobs that run the shared step.
    assert set(f["affected_jobs"]) == {"app", "web"}
    # #49 — the persisted marker must be stamped at the construction site (not only in
    # the in-memory sizing cascade). This pins the stamp: dropping it from collect_runs
    # turns the render-layer headline selection + verify_report burial guard back into a
    # silent fail-open, so this artifact-path assertion (not just the synthetic unit
    # tests) must go red if the stamp is removed.
    assert f.get("cluster_floor_lever") is True
    # #49 — a parallel matrix cluster persists concurrency=True so the render-layer
    # headline keeps the honest "concurrent … in lockstep" framing.
    assert f.get("cluster_legs_concurrent") is True
    # The detector picks the largest recurring NAMED step (here `Build`, 180s in
    # each job — NOT the differently-named `Bundle app`/`Bundle web`). Runner-min is
    # PER MONTH: warm floor 15s -> 165s per job × 2 jobs = 330s/run; × 1000 runs/mo
    # ÷ 60 = 5500 min/mo. The evidence names the `Build` step.
    assert f["runner_min_saving"] == round((180 - 15) * 2 * 1000 / 60.0, 1) == 5500.0
    assert "`Build`" in (f.get("measured_evidence") or {}).get("summary", "")


def test_shared_substep_labels_sequential_needs_chain_truthfully():
    # Regression: two cluster jobs that share a step but are wired in a `needs:`
    # chain (`test` needs `compile`) are SEQUENTIAL, not concurrent. OPT73 must
    # not assert they run "concurrently" / "at the same time" — the cluster-floor
    # premise's parallel framing is false for serial stages. (Seen on
    # deepgram/deepgram-python-sdk: f19 claimed `compile`/`test` were concurrent.)
    shared = ("Bootstrap poetry", 100)
    a = [("Checkout", 20), shared, ("Compile", 60)]
    b = [("Checkout", 20), shared, ("Run tests", 60)]
    runs = [[_job("compile", a), _job("test", b)] for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit_by_wf = {".github/workflows/ci.yml": cr._critical_path(runs)}
    pr_checks = (("compile", 180.0), ("test", 180.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    job_graph = {".github/workflows/ci.yml": {
        "compile": {"name": "compile", "needs": [], "reusable": False},
        "test": {"name": "test", "needs": ["compile"], "reusable": False},
    }}
    out = cr._detect_shared_substep(
        crit_by_wf, jpr, events, pr_checks, [], 0,
        vol_by_wf={".github/workflows/ci.yml": 1000}, job_graph=job_graph)
    opt73 = [f for f in out if f["pattern"] == "OPT73"]
    assert opt73, "expected a shared-substep (OPT73) lever for the shared step"
    f = opt73[0]
    assert set(f["affected_jobs"]) == {"compile", "test"}
    # The claim must reflect the SEQUENTIAL needs: chain, never "concurrent".
    me = f.get("measured_evidence") or {}
    blob = " ".join([
        f.get("evidence", ""), me.get("summary", ""), me.get("note", ""),
    ])
    assert "concurrent" not in blob.lower(), (
        f"OPT73 mislabels a needs:-chained sequential cluster as concurrent: {blob!r}")
    assert "at the same time" not in blob.lower(), blob
    assert "sequential" in blob.lower()
    # #49 — the persisted concurrency marker must record the sequential nature so the
    # render-layer headline can phrase it honestly (not just the appendix evidence).
    assert f.get("cluster_legs_concurrent") is False

    # Without a job graph (or with a genuinely parallel cluster) the wording stays
    # "concurrent" — the fix must not regress the common matrix-leg case.
    out_par = cr._detect_shared_substep(
        crit_by_wf, jpr, events, pr_checks, [], 0,
        vol_by_wf={".github/workflows/ci.yml": 1000})
    par = [f for f in out_par if f["pattern"] == "OPT73"][0]
    par_blob = " ".join([
        par.get("evidence", ""),
        (par.get("measured_evidence") or {}).get("summary", ""),
        (par.get("measured_evidence") or {}).get("note", ""),
    ])
    assert "concurrent" in par_blob.lower()
    assert par.get("cluster_legs_concurrent") is True


def test_shared_substep_runner_min_is_per_month_not_per_run():
    # Regression: OPT73 runner-min must scale with the workflow's monthly volume.
    # The same finding sized at 10x the volume must report 10x the bill saving.
    a = [("Checkout", 20), ("Install deps", 80), ("Build app", 180)]
    b = [("Checkout", 20), ("Install deps", 80), ("Build web", 180)]
    runs = [[_job("app", a), _job("web", b)] for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit_by_wf = {".github/workflows/ci.yml": cr._critical_path(runs)}
    pr_checks = (("app", 280.0), ("web", 280.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}

    def rm(vol):
        out = cr._detect_shared_substep(crit_by_wf, jpr, events, pr_checks, [], 0,
                                        vol_by_wf={".github/workflows/ci.yml": vol})
        return out[0]["runner_min_saving"]

    assert rm(2000) == round(rm(200) * 10, 1)
    # Unknown volume -> None (no fabricated bill number), not a per-run figure.
    out = cr._detect_shared_substep(crit_by_wf, jpr, events, pr_checks, [], 0)
    assert out[0].get("runner_min_saving") is None


def test_shared_substep_sizes_bimodal_step_off_slow_cluster():
    # OPT73 bimodal threading: a shared `Build` step that is cache-warm on most PRs (40s) and
    # cold on a minority (300s) makes each cluster job bimodal. Sized off the BLENDED p50 the
    # shared floor is the warm 40s (under-credit); sized off the SLOW cluster the report drills
    # it is 300s. Passing `job_bimodal_all` must select the slow-mode floor — and prevents the
    # silent-drop edge where a slow-mode-only material step falls below the 15s warm floor.
    # `Build` is the largest shared step in BOTH modes (Install/Checkout are smaller), so the
    # detector crowns it either way — isolating the comparison to its SIZING, not step choice.
    warm_a = [("Checkout", 20), ("Install deps", 20), ("Build", 40), ("Bundle app", 40)]  # 120
    cold_a = [("Checkout", 20), ("Install deps", 20), ("Build", 300), ("Bundle app", 40)]  # 380
    warm_b = [("Checkout", 20), ("Install deps", 20), ("Build", 40), ("Bundle web", 40)]
    cold_b = [("Checkout", 20), ("Install deps", 20), ("Build", 300), ("Bundle web", 40)]
    runs = ([[_job("app", warm_a), _job("web", warm_b)] for _ in range(7)]
            + [[_job("app", cold_a), _job("web", cold_b)] for _ in range(3)])
    jpr = {".github/workflows/ci.yml": runs}
    crit = cr._critical_path(runs)
    crit_by_wf = {".github/workflows/ci.yml": crit}
    jba = crit["job_bimodal"]
    assert "app" in jba and "web" in jba, "both cluster jobs should be detected bimodal"
    pr_checks = (("app", 120.0), ("web", 120.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    vol = {".github/workflows/ci.yml": 1000}

    def floor(job_bimodal_all):
        out = cr._detect_shared_substep(crit_by_wf, jpr, events, pr_checks, [], 0,
                                        vol_by_wf=vol, job_bimodal_all=job_bimodal_all)
        opt73 = [f for f in out if f["pattern"] == "OPT73"]
        assert opt73, "expected a shared-substep (OPT73) lever"
        assert set(opt73[0]["affected_jobs"]) == {"app", "web"}
        assert "`Build`" in (opt73[0].get("measured_evidence") or {}).get("summary", "")
        return opt73[0]["runner_min_saving"]

    # Blended (no split): warm 40s floor -> (40-15)*2*1000/60 = 833.3 (under-credit).
    assert floor(None) == round((40 - 15) * 2 * 1000 / 60.0, 1) == 833.3
    # Slow cluster (bimodal split threaded): cold 300s floor -> (300-15)*2*1000/60 = 9500.0.
    assert floor(jba) == round((300 - 15) * 2 * 1000 / 60.0, 1) == 9500.0


def test_shared_substep_share_is_physically_bounded_for_bimodal_jobs():
    # Faithfulness regression: OPT73's "step is X% of the slowest cluster job"
    # must be PHYSICALLY BOUNDED (<=100%) — a step cannot exceed its containing
    # job. The bug: the share NUMERATOR is the shared step's SLOW-mode p50 (sized
    # off the slow cluster, like OPT70/72/75), while the DENOMINATOR was the job's
    # BLENDED (warm-dragged) p50 from `crit.job_p50`. Mixing aggregations renders
    # an impossible >100% share. Here each cluster job is bimodal: `Build` is 40s
    # warm (7 runs, job total 120s) and 300s cold (3 runs, job total 380s), so the
    # BLENDED job p50 is 120s while the slow-mode `Build` is 300s — 300/120 = 250%
    # under the bug. The denominator must use the SAME slow-mode basis (slow-mode
    # job total 380s) so the share is 300/380 = 79%.
    warm_a = [("Checkout", 20), ("Install deps", 20), ("Build", 40), ("Bundle app", 40)]   # 120
    cold_a = [("Checkout", 20), ("Install deps", 20), ("Build", 300), ("Bundle app", 40)]  # 380
    warm_b = [("Checkout", 20), ("Install deps", 20), ("Build", 40), ("Bundle web", 40)]
    cold_b = [("Checkout", 20), ("Install deps", 20), ("Build", 300), ("Bundle web", 40)]
    runs = ([[_job("app", warm_a), _job("web", warm_b)] for _ in range(7)]
            + [[_job("app", cold_a), _job("web", cold_b)] for _ in range(3)])
    jpr = {".github/workflows/ci.yml": runs}
    crit = cr._critical_path(runs)
    crit_by_wf = {".github/workflows/ci.yml": crit}
    jba = crit["job_bimodal"]
    assert crit["job_p50"]["app"] == 120.0  # blended p50 (the wrong denominator)
    pr_checks = (("app", 120.0), ("web", 120.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    vol = {".github/workflows/ci.yml": 1000}
    out = cr._detect_shared_substep(crit_by_wf, jpr, events, pr_checks, [], 0,
                                    vol_by_wf=vol, job_bimodal_all=jba)
    opt73 = [f for f in out if f["pattern"] == "OPT73"]
    assert opt73, "expected a shared-substep (OPT73) lever"
    f = opt73[0]

    # Every rendered share — evidence prose, the measured-evidence summary, and
    # every table row — must be <= 100%.
    me = f["measured_evidence"]
    blobs = [f["evidence"], me["summary"]]
    shares = re.findall(r"(\d+)%", " ".join(blobs))
    assert shares, f"expected a rendered share percentage in OPT73 evidence: {blobs!r}"
    for pct in shares:
        assert int(pct) <= 100, (
            f"OPT73 renders a physically impossible >100% share (slow-mode step "
            f"p50 over a BLENDED job p50): {int(pct)}% in {blobs!r}")
    for row in me["table"]["rows"]:
        job_total_s = float(row[1].rstrip("s"))
        step_s = float(row[2].rstrip("s"))
        share_pct = int(row[3].rstrip("%"))
        assert step_s <= job_total_s, (
            f"shared step {step_s}s exceeds its job total {job_total_s}s in {row!r}")
        assert share_pct <= 100, f"row share >100%: {row!r}"

    # The denominator must be the SLOW-mode job total (380s), not the blended p50
    # (120s) — that is the basis consistent with the slow-mode step numerator.
    assert "380s" in f["evidence"] and "120s" not in f["evidence"], f["evidence"]


def test_shared_substep_share_uses_decomposition_total_for_nonbimodal_variance():
    # Companion to the bimodal >100% guard: the OPT73 share denominator is
    # `_decompose_job_steps` job_p50 (the SUM of per-step medians) for EVERY job, not only
    # bimodal ones. For a non-bimodal but NOISY cluster the sum-of-step-medians differs from
    # `crit.job_p50` (the MEDIAN of job totals) whenever step peaks land in different runs.
    # Here the shared `Run Integration Tests` and the job-specific `Build`/`Bundle` are
    # anti-correlated across 7 runs, so per-step medians sum to 220s while the job totals
    # (a gentle 220-240s spread, NOT bimodal) median to 230s. The share must use the 220s
    # decomposition basis (consistent with the step-median numerator), never the 230s
    # blended total — and stay physically bounded (<=100%).
    its     = [200, 190, 180, 30, 20, 10, 100]   # shared step, median 100
    builds  = [20,  30,  40,  180, 190, 200, 100] # job-specific, median 100 (anti-correlated)
    runs = [[_job("app", [("Checkout", 20), ("Run Integration Tests", it), ("Build", b)]),
             _job("web", [("Checkout", 20), ("Run Integration Tests", it), ("Bundle web", b)])]
            for it, b in zip(its, builds)]
    jpr = {".github/workflows/ci.yml": runs}
    crit = cr._critical_path(runs)
    crit_by_wf = {".github/workflows/ci.yml": crit}
    # Sanity: the two bases genuinely differ here (else the test proves nothing).
    assert crit["job_p50"]["app"] == 230.0  # blended MEDIAN of job totals (the wrong basis)
    pr_checks = (("app", 230.0), ("web", 230.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    out = cr._detect_shared_substep(
        crit_by_wf, jpr, events, pr_checks, [], 0,
        vol_by_wf={".github/workflows/ci.yml": 1000})
    opt73 = [f for f in out if f["pattern"] == "OPT73"]
    assert opt73, "expected a shared-substep (OPT73) lever for the non-bimodal noisy cluster"
    f = opt73[0]
    assert set(f["affected_jobs"]) == {"app", "web"}
    me = f["measured_evidence"]
    blobs = [f["evidence"], me["summary"]]
    shares = re.findall(r"(\d+)%", " ".join(blobs))
    assert shares, f"expected a rendered share percentage in OPT73 evidence: {blobs!r}"
    for pct in shares:
        assert int(pct) <= 100, (
            f"OPT73 renders a physically impossible >100% share on a non-bimodal job: "
            f"{int(pct)}% in {blobs!r}")
    for row in me["table"]["rows"]:
        job_total_s = float(row[1].rstrip("s"))
        step_s = float(row[2].rstrip("s"))
        assert step_s <= job_total_s, (
            f"shared step {step_s}s exceeds its job total {job_total_s}s in {row!r}")
    # The denominator must be the per-step-median decomposition total (220s), NOT the
    # blended median-of-totals (230s).
    assert "220s" in f["evidence"] and "230s" not in f["evidence"], f["evidence"]


def test_expensive_non_required_check_is_flagged():
    out = _scenario(required={"build-and-test"})  # CodeQL + lint NOT required
    opt71 = [f for f in out if f["pattern"] == "OPT71"]
    assert opt71, "expected an OPT71 expensive-non-required finding"
    names = " ".join(f["title"] for f in opt71)
    assert "CodeQL" in names or "lint" in names
    for f in opt71:
        assert f["required_status"] == "not-required"


def test_fileless_check_anchors_affected_jobs_to_the_check_name():
    # CodeQL in `_scenario`'s pr_checks has no sampled job (fileless app/default-
    # setup check), so `_map_check_to_job` returns None. The finding must still
    # carry the CHECK NAME in `affected_jobs` — `_pr_critical_path_block` builds
    # its "ranked finding below" coverage exclusively from `affected_jobs` token-
    # sets, and an empty list would leave the gating check wrongly labelled
    # "inherent cost (no lever)" even though this routed finding targets it.
    out = _scenario(required={"build-and-test"})  # CodeQL not required → OPT71
    codeql = [f for f in out if "CodeQL" in f["title"]]
    assert codeql, "expected a routed structural finding for the fileless CodeQL check"
    for f in codeql:
        assert f["affected_jobs"] == ["CodeQL"], (
            "fileless finding must anchor affected_jobs to the check name so the "
            "critical-path table can match it")


def test_triaged_check_finding_anchors_the_real_workflow_file_not_a_name_stub():
    # A fast lint/validate workflow is triaged out of job-fetching (its slowest sampled
    # run is under the wall-clock floor), so `crit_by_wf` holds an empty-`job_p50` stub
    # and `_map_check_to_job` returns None. The structural finding for that pole used to
    # fall back to `check_name.split(" ")[0]` — yielding a one-word NON-PATH ("Check")
    # that contradicts the pole's known file and points the report / auto-fixer at a
    # nonexistent file. The real file IS in the static job graph (built from the scanned
    # YAML, independent of triage), so the finding must anchor it.
    check = "Check Typo using codespell"
    wf_file = ".github/workflows/check-typo.yml"
    crit_by_wf = {  # the triaged stub: no sampled job to name-match
        wf_file: {"long_pole_job": "", "long_pole_p50": 0.0,
                  "floor_p50": 0.0, "job_p50": {}}}
    jpr = {wf_file: []}
    pr_checks = ((check, 30.0),)
    events = {wf_file: {"pull_request"}}
    job_graph = {wf_file: {"check-typo": {
        "name": check, "needs": [], "reusable": False}}}
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, None, events, {}, 100,
        job_graph=job_graph)
    # CLASS fix: a file-backed (.yml) check triaged out of step-sampling (mapping is None) has no
    # measured step, so it gets NO name-inferred structural lever (the admission gate, enforced
    # uniformly with the no-decomp sibling branch). The original anchoring concern ("anchor the
    # real file, not a one-word name stub") is now moot for this case — there is no structural
    # finding to anchor; the pole falls to OPT71 or the renderer's generic dominant-step agent
    # prompt (NOT phase-4a, which needs a captured log a triaged-fast pole lacks). (Anchoring for the
    # GENUINELY-fileless case stays covered by test_genuinely_fileless_check_still_labeled_fileless.)
    structural = [f for f in out if f["pattern"] in ("OPT70", "OPT72", "OPT75") and check in f["title"]]
    assert not structural, (
        "a file-backed (.yml) triaged check with no sampled step must NOT get a name-inferred "
        f"structural lever: {[(f['pattern'], f['workflow_file']) for f in structural]}")


def test_file_backed_but_triaged_check_does_not_fabricate_fileless_opt75():
    # paradedb regression: a FAST check (`Check Typo using codespell`, 12s) is a real
    # FILE-BACKED job — it appears in `crit_by_wf`'s job_p50, so `_map_check_to_job`
    # maps it to .github/workflows/check-typo.yml — but it was triaged OUT of step-
    # sampling as a fast workflow under the long-pole floor. So `jobs_per_run_by_wf`
    # carries NO sampled instances for it and `_decompose_job_steps` returns None
    # (mapping is not None, decomp is None). The structural track must NOT fabricate an
    # OPT75 that calls it a "fileless ... third-party app check" and infers the cost
    # category from the check NAME — that is the infer-root-cause-from-a-bare-name
    # pattern OPT49/OPT51 were CUT for, emitted with ZERO measured step evidence (the
    # admission gate requires the long-pole job DECOMPOSED into steps, which never
    # happened here). Only a GENUINELY fileless check (mapping is None) earns the
    # name-inferred OPT75.
    crit_by_wf = {".github/workflows/check-typo.yml":
                  {"job_p50": {"Check Typo using codespell": 12.0}}}
    jpr = {".github/workflows/check-typo.yml": []}  # triaged out → no sampled steps
    pr_checks = (("Check Typo using codespell", 12.0),)
    events = {".github/workflows/check-typo.yml": {"pull_request"}}
    required = cr.RequiredChecks(frozenset({"Check Typo using codespell"}), complete=True)
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, {}, 0)
    typo = [f for f in out if "Check Typo" in f.get("title", "")]
    assert not [f for f in typo if f["pattern"] == "OPT75"], (
        "a file-backed-but-triaged check (real workflow file, no sampled steps) must "
        "NOT get a name-inferred OPT75 — no measured step evidence; the structural "
        "admission gate is unmet")
    assert not any("fileless" in (f.get("evidence") or "") for f in typo), (
        "must not claim a file-backed check is 'fileless'")
    # Sanity: a GENUINELY fileless check (maps to no job) still earns its OPT75.
    out_fileless = _scenario(required={"build-and-test"})  # CodeQL maps to no job
    assert [f for f in out_fileless
            if "CodeQL" in f["title"] and f["pattern"] == "OPT75"], (
        "a genuinely fileless check must still route to a name-inferred OPT75")


def test_unknown_required_status_is_never_asserted_non_required():
    # required_checks=None (branch-protection 404) → "unknown", never "not-required".
    out = _scenario(required=None)
    assert out
    statuses = {f.get("required_status") for f in out}
    assert "not-required" not in statuses
    assert "unknown" in statuses
    # And no OPT71 (we don't recommend de-scoping a check that might be required).
    assert not [f for f in out if f["pattern"] == "OPT71"]


# --------------------------------------------------------------------------- #
# Required-REACHABILITY scope — the spine (and headline pole) must be the
# merge-BLOCKING checks: a required check, or a job the required work transitively
# `needs:`. A non-required check that gates zero merges must never headline "why is
# the merge slow?". The matcher is needs-reachability over the workflow job graph
# (repo-agnostic); these fixtures exercise it against the two real shapes it must
# handle — the reusable-rollup and the bare-aggregator — plus the independent-sibling
# bug it fixes, all synthetic so no behavior is tuned to one repo's names.
# --------------------------------------------------------------------------- #

def _reusable_rollup_fixture():
    """The reusable-rollup shape: a reusable caller `Suite` (a job with `uses:`) whose
    required child `Suite / Merge Reports` rollup is produced by the invocation; the caller
    `needs: [changes, Build]`. A separate `lint.yml` has a required `Lint` and a NON-required
    INDEPENDENT sibling `Validate build outputs` — both only `needs: changes`, and `Lint`
    does NOT need the sibling. Returns (pr_check_p50, crit_by_wf, job_graph, required)."""
    job_graph = {
        ".github/workflows/prebuild.yml": {
            "changes":  {"name": "Detect code changes", "needs": [], "reusable": False},
            "prebuild": {"name": "Build", "needs": ["changes"], "reusable": False},
            "suite":    {"name": "Suite", "needs": ["changes", "prebuild"], "reusable": True},
        },
        ".github/workflows/lint.yml": {
            "changes": {"name": "Detect code changes", "needs": [], "reusable": False},
            "lint":    {"name": "Lint", "needs": ["changes"], "reusable": False},
            "bundle":  {"name": "Validate build outputs", "needs": ["changes"], "reusable": False},
        },
    }
    crit_by_wf = {
        ".github/workflows/prebuild.yml": {"job_p50": {
            "Build": 425.0, "Detect code changes": 67.0,
            "Suite / UNIT Test (Shard 1)": 391.0, "Suite / Merge Reports": 30.0,
        }},
        ".github/workflows/lint.yml": {"job_p50": {
            "Lint": 263.0, "Validate build outputs": 433.0,
        }},
    }
    pr_check_p50 = {
        "Validate build outputs": 433.0,        # NON-required INDEPENDENT sibling — slowest
        "Build": 425.0,                         # the required reusable caller `needs:` it
        "Suite / UNIT Test (Shard 1)": 391.0,   # reusable child of a required rollup
        "Lint": 263.0,                          # required
        "Detect code changes": 67.0,            # Lint + the caller `needs:` it
        "Suite / Merge Reports": 30.0,          # required
        "Socket Security": 8.0,                 # fileless/external, non-required
    }
    required = cr.RequiredChecks(
        frozenset({"Lint", "Suite / Merge Reports"}), complete=True)
    return pr_check_p50, crit_by_wf, job_graph, required


def test_required_reachable_keeps_needs_closure_and_reusable_children():
    pr_check_p50, crit_by_wf, job_graph, required = _reusable_rollup_fixture()
    keep = cr._required_reachable_checks(
        pr_check_p50, required.names, job_graph, crit_by_wf)
    assert {"Lint", "Suite / Merge Reports"} <= keep          # required themselves
    assert "Suite / UNIT Test (Shard 1)" in keep              # reusable-invocation grouping
    assert "Build" in keep                                    # required reusable caller needs it
    assert "Detect code changes" in keep                      # needed by Lint and the caller
    # The slowest check overall is an INDEPENDENT sibling of required `Lint` (Lint does
    # NOT `needs:` it) — needs-reachability drops it where file co-residence would keep it.
    assert "Validate build outputs" not in keep
    assert "Socket Security" not in keep                      # fileless/external, non-required


def test_required_reachable_keeps_an_ambiguous_file_backed_check_via_the_safety_net():
    # Issue #59 blast radius: `_map_check_to_job` now bails to None on a cross-workflow same-name
    # collision, and `_check_to_job_node` (which anchors via that mapper) bails with it. A non-required
    # duplicated monorepo gate (`Build` in TWO package workflows) therefore can't be pinned to a
    # reachable job id — but it IS file-backed and must NOT be silently dropped as "non-required" the
    # moment a required set resolves (that would undo the spine's `_check_grounded_job_p50` crowning).
    # The cat-3 safety net keeps it by probing the AMBIGUITY-AWARE full match set, not the single-pick
    # mapper.
    job_graph = {
        ".github/workflows/pkg-a.yml": {
            "build": {"name": "Build", "needs": [], "reusable": False},
            "lint":  {"name": "Lint", "needs": [], "reusable": False}},
        ".github/workflows/pkg-b.yml": {
            "build": {"name": "Build", "needs": [], "reusable": False}},
    }
    crit_by_wf = {
        ".github/workflows/pkg-a.yml": {"job_p50": {"Build": 120.0, "Lint": 60.0}},
        ".github/workflows/pkg-b.yml": {"job_p50": {"Build": 900.0}},
    }
    req_names = frozenset({"Lint"})   # complete required set; `Build` is NOT required
    # `Build` is ambiguous -> unpinnable, so both the mapper and the node anchor bail...
    assert cr._map_check_to_job("Build", crit_by_wf) is None
    assert cr._check_to_job_node("Build", job_graph, crit_by_wf) is None
    # ...but the ambiguity-aware net keeps the file-backed gate (never silently dropped).
    keep = cr._required_reachable_checks({"Build", "Lint"}, req_names, job_graph, crit_by_wf)
    assert "Build" in keep, "an ambiguous FILE-BACKED gate must survive required-scoping"
    assert "Lint" in keep
    # A genuinely fileless/external check with no producing job anywhere is still dropped.
    keep2 = cr._required_reachable_checks(
        {"Build", "Lint", "Socket Security"}, req_names, job_graph, crit_by_wf)
    assert "Socket Security" not in keep2


def test_workflow_gate_freq_credits_every_producing_workflow_of_an_ambiguous_gate():
    # Issue #59 blast radius: an ambiguous same-named gate's per-PR pole frequency must not be lost.
    # `_map_check_to_job` bails to None on the collision, so the old single-pick attribution credited
    # NO workflow — dragging a real majority gate under the minority line and flooring its wall-clock
    # lever to bill-only. The ambiguity-aware attribution credits every producing workflow instead.
    crit_by_wf = {
        ".github/workflows/pkg-a.yml": {"job_p50": {"Build": 120.0}},
        ".github/workflows/pkg-b.yml": {"job_p50": {"Build": 900.0}},
    }
    check_pole_freq = {"Build": 12}
    assert cr._workflow_gate_freq(
        check_pole_freq, crit_by_wf, ".github/workflows/pkg-a.yml") == 12
    assert cr._workflow_gate_freq(
        check_pole_freq, crit_by_wf, ".github/workflows/pkg-b.yml") == 12
    # Unambiguous check: credited to its one workflow only (byte-identical to the old behaviour).
    solo = {".github/workflows/only.yml": {"job_p50": {"Lint": 60.0}}}
    assert cr._workflow_gate_freq({"Lint": 5}, solo, ".github/workflows/only.yml") == 5
    assert cr._workflow_gate_freq({"Lint": 5}, solo, ".github/workflows/other.yml") == 0


def test_scope_spine_drops_independent_sibling_so_a_real_gating_job_wins():
    pr_check_p50, crit_by_wf, job_graph, required = _reusable_rollup_fixture()
    kept, dropped, scoped = cr._scope_spine_to_required(
        pr_check_p50, required, required.names, crit_by_wf, job_graph, False)
    assert "Validate build outputs" in dropped    # slowest overall, but gates no merge
    assert "Socket Security" in dropped
    # The pole = slowest KEPT = `Build` (the required reusable caller `needs:` it), NOT the
    # slowest-overall non-gating sibling. This is the whole point of the fix: speeding a
    # check that gates zero merges removes zero time-to-merge, so it must not headline.
    assert max(kept.items(), key=lambda kv: kv[1])[0] == "Build"
    assert scoped is True   # the narrowing actually fired (active path), so the spine is required-scoped


def test_required_reachable_handles_bare_aggregator_jobs():
    # The bare-aggregator shape: required `ci`/`e2e` are trivial ~3s jobs that `needs:` the
    # real slow work (which shares NO name prefix). needs-reachability keeps the slow jobs
    # (a name-PREFIX matcher would collapse the gate to the 3s aggregator), and DROPS a
    # non-required sibling that lives in the same file but the aggregator doesn't need
    # (where FILE co-residence would wrongly keep it).
    job_graph = {
        ".github/workflows/ci.yml": {
            "build":     {"name": "build", "needs": [], "reusable": False},
            "test":      {"name": "test (22.x)", "needs": ["build"], "reusable": False},
            "spellcheck": {"name": "Spell check", "needs": [], "reusable": False},
            "ci":        {"name": "ci", "needs": ["build", "test"], "reusable": False},
        },
        ".github/workflows/e2e.yml": {
            "prisma": {"name": "prisma-adapter Integration Test", "needs": [], "reusable": False},
            "e2e":    {"name": "e2e", "needs": ["prisma"], "reusable": False},
        },
    }
    crit_by_wf = {
        ".github/workflows/ci.yml": {"job_p50": {
            "ci": 3.0, "test (22.x)": 335.0, "build": 60.0, "Spell check": 40.0}},
        ".github/workflows/e2e.yml": {"job_p50": {
            "e2e": 3.0, "prisma-adapter Integration Test": 568.0}},
    }
    pr_check_p50 = {"prisma-adapter Integration Test": 568.0, "test (22.x)": 335.0,
                    "build": 60.0, "Spell check": 40.0, "ci": 3.0, "e2e": 3.0}
    required = cr.RequiredChecks(frozenset({"ci", "e2e"}), complete=True)
    keep = cr._required_reachable_checks(
        pr_check_p50, required.names, job_graph, crit_by_wf)
    assert {"prisma-adapter Integration Test", "test (22.x)", "build", "ci", "e2e"} <= keep
    # `Spell check` lives in ci.yml (a required file) but `ci` does NOT `needs:` it →
    # dropped. File-membership would have kept it; needs-reachability is tighter + correct.
    assert "Spell check" not in keep


def test_scope_spine_is_inert_unless_required_set_is_complete_and_satisfiable():
    pr_check_p50, crit_by_wf, job_graph, required = _reusable_rollup_fixture()
    # complete=False (partial read) → an absent check is UNKNOWN, not droppable → inert.
    partial = cr.RequiredChecks(required.names, complete=False)
    assert cr._scope_spine_to_required(
        pr_check_p50, partial, partial.names, crit_by_wf, job_graph, False) == (pr_check_p50, [], False)
    # required_suite_unsatisfiable → required names aren't in the sample (PR-floor
    # fallback renders); filtering would empty the spine → inert.
    assert cr._scope_spine_to_required(
        pr_check_p50, required, required.names, crit_by_wf, job_graph, True) == (pr_check_p50, [], False)
    # No required set readable at all → status unknown → inert.
    assert cr._scope_spine_to_required(
        pr_check_p50, None, frozenset(), crit_by_wf, job_graph, False) == (pr_check_p50, [], False)
    # `job_graph is None` (a findings doc predating the workflow_job_graph key, or a
    # degraded scan) → reachability can't be computed → inert, NOT a crash. Drives the
    # all-external anchor guard's `_check_to_job_node` call, which iterates the graph: the
    # `and job_graph` inert guard must short-circuit before it dereferences None.
    assert cr._scope_spine_to_required(
        pr_check_p50, required, required.names, crit_by_wf, None, False) == (pr_check_p50, [], False)


def test_scope_spine_never_empties_on_an_over_tight_match():
    # A complete required set whose checks reach NONE of the sampled checks (a fileless
    # managed gate with no job graph entry) would empty the spine — the guard returns the
    # input unchanged so the report still has a measured floor to render, not a dead end.
    pr_check_p50 = {"build (22.x)": 200.0, "lint": 50.0}
    crit_by_wf = {".github/workflows/ci.yml": {"job_p50": {"build (22.x)": 200.0,
                                                           "lint": 50.0}}}
    job_graph = {".github/workflows/ci.yml": {
        "b": {"name": "build (22.x)", "needs": [], "reusable": False},
        "l": {"name": "lint", "needs": [], "reusable": False}}}
    required = cr.RequiredChecks(frozenset({"External Mergeability Bot"}), complete=True)
    kept, dropped, scoped = cr._scope_spine_to_required(
        pr_check_p50, required, required.names, crit_by_wf, job_graph, False)
    assert (kept, dropped, scoped) == (pr_check_p50, [], False)   # inert ⇒ scoped False


def test_scope_spine_is_inert_when_every_required_check_is_external():
    # vellum-assistant shape: the ONLY required checks are external/managed (Socket
    # Security bots) with no workflow job in this repo. Reachability can't anchor on any
    # job, so scoping would drop ALL the file-backed work — which the PR-floor fallback
    # then re-surfaces as the spine, contradicting a "dropped non-required" footnote. The
    # filter must stay inert and let the external-gate fallback own it.
    job_graph = {".github/workflows/ci.yml": {
        "test": {"name": "Test", "needs": [], "reusable": False},
        "lint": {"name": "Lint", "needs": [], "reusable": False}}}
    crit_by_wf = {".github/workflows/ci.yml": {"job_p50": {"Test": 234.0, "Lint": 56.0}}}
    pr_check_p50 = {"Test": 234.0, "Lint": 56.0,
                    "Socket Security: Project Report": 8.0}
    required = cr.RequiredChecks(
        frozenset({"Socket Security: Project Report",
                   "Socket Security: Pull Request Alerts"}), complete=True)
    kept, dropped, scoped = cr._scope_spine_to_required(
        pr_check_p50, required, required.names, crit_by_wf, job_graph, False)
    assert (kept, dropped, scoped) == (pr_check_p50, [], False)   # inert — no file-backed work dropped


def test_all_external_observable_required_suite_falls_back_to_pr_floor():
    # vellum-assistant shape: the ONLY required checks are external/managed (Socket
    # Security bots) with NO workflow file here, yet they DID run on every sampled PR, so
    # `required_suite_unsatisfiable` is False. `_scope_spine_to_required` stays inert
    # (spine_required_scoped=False) because no required check anchors a file-backed job,
    # so the file-backed Test/Lint poles would otherwise headline "why is the merge
    # slow?" though they gate zero merges. `_required_suite_all_external` is the signal
    # the PR-floor fallback uses to demote them in place even when the external suite was
    # OBSERVABLE — the case the `required_suite_unsatisfiable`-keyed branch can't catch.
    job_graph = {".github/workflows/ci.yml": {
        "test": {"name": "Test", "needs": [], "reusable": False},
        "lint": {"name": "Lint", "needs": [], "reusable": False}}}
    crit_by_wf = {".github/workflows/ci.yml": {"job_p50": {"Test": 273.0, "Lint": 56.0}}}
    required = cr.RequiredChecks(
        frozenset({"Socket Security: Project Report",
                   "Socket Security: Pull Request Alerts"}), complete=True)
    # All required checks are external/managed → all-external → PR-floor fallback fires.
    assert cr._required_suite_all_external(
        required, required.names, crit_by_wf, job_graph) is True

    # A required check that DOES anchor a file-backed job → NOT all-external → False
    # (a normal gate; the spine scopes to it, no PR-floor demotion).
    required_filebacked = cr.RequiredChecks(frozenset({"Test"}), complete=True)
    assert cr._required_suite_all_external(
        required_filebacked, required_filebacked.names, crit_by_wf, job_graph) is False

    # A PARTIAL read (complete=False) is UNKNOWN, never asserted external → False.
    partial = cr.RequiredChecks(
        frozenset({"Socket Security: Project Report"}), complete=False)
    assert cr._required_suite_all_external(
        partial, partial.names, crit_by_wf, job_graph) is False

    # No required set / no job graph → can't prove external → False (conservative).
    assert cr._required_suite_all_external(None, frozenset(), crit_by_wf, job_graph) is False
    assert cr._required_suite_all_external(
        required, required.names, crit_by_wf, None) is False


def test_static_name_matrix_check_maps_to_its_job_not_external():
    # rootlyhq/terraform-provider-rootly shape: the test.yml `test` job declares a STATIC
    # `name: "Matrix Test"` (NO ${{ matrix.* }} placeholder) but HAS a matrix strategy, so
    # GitHub appends the leg → check-run `Matrix Test (1.13.*)`. The static name compiles to
    # `^Matrix Test$`, which does NOT match the appended-leg check, so the required check used
    # to resolve to None → misclassified external/managed → a spurious PR-floor fallback and a
    # FALSE "no file-backed required gate" headline, though findings.json proves the pole IS
    # this file-backed job. A matrix job's static name plus an appended ` (<leg>)` parenthetical
    # MUST map back to its job.
    wf = ".github/workflows/test.yml"
    job_graph = {wf: {
        "test": {"name": "Matrix Test", "needs": [], "reusable": False, "matrix": True},
        "lint": {"name": "Lint", "needs": [], "reusable": False, "matrix": False}}}
    crit_by_wf = {wf: {"job_p50": {"Matrix Test (1.13.*)": 540.0, "Lint": 30.0}}}
    assert cr._check_to_job_node("Matrix Test (1.13.*)", job_graph, crit_by_wf) == (wf, "test")
    # ...so the required suite is NOT all-external → no spurious PR-floor fallback.
    required = cr.RequiredChecks(frozenset({"Matrix Test (1.13.*)"}), complete=True)
    assert cr._required_suite_all_external(
        required, required.names, crit_by_wf, job_graph) is False


def test_static_name_matrix_match_keeps_templated_form_and_does_not_overmatch():
    # The appended-leg tolerance is GUARDED to matrix jobs and must not over-match: a
    # different check that merely shares a name prefix (no ` (<leg>)` boundary) stays None.
    # And the existing ${{ matrix.* }}-template form keeps mapping unchanged.
    wf = ".github/workflows/test.yml"
    job_graph = {wf: {
        "test": {"name": "Matrix Test", "needs": [], "reusable": False, "matrix": True},
        "unit": {"name": "UNIT Test (Shard ${{ matrix.shard }})",
                 "needs": [], "reusable": False, "matrix": True}}}
    crit_by_wf = {wf: {"job_p50": {
        "Matrix Test (1.13.*)": 540.0, "Matrix Testing": 12.0,
        "UNIT Test (Shard 4)": 90.0}}}
    # Prefix-sharing but no appended-leg parenthetical → does NOT map to the matrix job.
    assert cr._check_to_job_node("Matrix Testing", job_graph, crit_by_wf) is None
    # Templated matrix name still maps via the existing ${{ matrix.* }}-template path.
    assert cr._check_to_job_node("UNIT Test (Shard 4)", job_graph, crit_by_wf) == (wf, "unit")


def test_templated_slash_job_name_binds_to_its_job_not_reusable_split():
    # Issue #118: a matrix job whose OWN `name:` contains `" / "` — `test / ${{ matrix.type }}`
    # in unit.yml, matrix.type=[ethereum] → check-run `test / ethereum` (a real 13m gate) — was
    # mis-parsed: the `" / "` inside the job name was read as GitHub's reusable-workflow
    # `<workflow> / <job>` separator FIRST, and the unexpanded `${{ matrix.type }}` template
    # couldn't equality-match the expanded check name, so the resolver returned None and the
    # render/summary demoted the gate as "fileless/managed, don't investigate."
    #
    # The scan sees the job (its workflow was triage-skipped, so it carries no sampled job to
    # anchor `_map_check_to_job`), so the scanned-graph template match must bind it.
    jg = {".github/workflows/unit.yml": {
        "test": {"name": "test / ${{ matrix.type }}", "needs": [],
                 "reusable": False, "matrix": True}}}
    # RED before #118: reusable-split-first + no sampled anchor → None. GREEN: same-workflow
    # templated match binds it to its own job by the literal-prefix-anchored template regex.
    assert cr._check_to_job_node("test / ethereum", jg, {}) == (".github/workflows/unit.yml", "test")
    # It is therefore NOT fileless: the partitioner keeps it groundable via the scanned graph,
    # agreeing with the resolved workflow_file (the two "fileless" notions reconcile).
    groundable, fileless = cr._partition_fileless_checks(
        {"test / ethereum": 800.0}, {"test / ethereum": "workflow_jobs"}, {}, jg)
    assert "test / ethereum" in groundable and "test / ethereum" not in fileless


def test_reusable_workflow_slash_name_still_resolves_to_the_caller_converse():
    # The CONVERSE that must keep working (next.js shape): a GENUINE reusable-workflow leaf
    # check `<caller job name> / <child>` — where NO same-workflow job produces the check —
    # must still resolve to its reusable CALLER job. The `" / "` split is the LAST resort, so
    # it still fires when nothing else matches.
    jg = {".github/workflows/ci.yml": {
        "suite": {"name": "Suite", "needs": [], "reusable": True, "matrix": False}}}
    assert cr._check_to_job_node("Suite / build", jg, {}) == (".github/workflows/ci.yml", "suite")
    # A bare caller-name check (no ` / <child>`) resolves to the same caller too.
    assert cr._check_to_job_node("Suite", jg, {}) == (".github/workflows/ci.yml", "suite")


def test_plain_name_without_slash_resolves_unchanged():
    # A plain check with NO `" / "` is untouched by the reorder: it resolves via the
    # sampled-timing anchor exactly as before.
    jg = {".github/workflows/ci.yml": {
        "lint": {"name": "Lint", "needs": [], "reusable": False, "matrix": False}}}
    crit = {".github/workflows/ci.yml": {"job_p50": {"Lint": 60.0}}}
    assert cr._check_to_job_node("Lint", jg, crit) == (".github/workflows/ci.yml", "lint")


def test_templated_same_workflow_job_wins_over_a_reusable_parse_ambiguity():
    # Issue #118 ambiguity pin: a check name that matches BOTH (a) a matrix-templated
    # same-workflow job `test / ${{ matrix.type }}` in unit.yml AND (b) a reusable-workflow
    # `" / "` split (a reusable caller job literally named `test` in another workflow, so
    # `test / ethereum` reads as its `<caller> / <child>`) must resolve to the SAME-WORKFLOW
    # job — the split is the documented last resort.
    jg = {
        ".github/workflows/unit.yml": {
            "test": {"name": "test / ${{ matrix.type }}", "reusable": False, "matrix": True}},
        ".github/workflows/other.yml": {
            "caller": {"name": "test", "reusable": True, "matrix": False}},
    }
    # RED before #118: the reusable split ran first → mis-bound to other.yml/caller.
    assert cr._check_to_job_node("test / ethereum", jg, {}) == (".github/workflows/unit.yml", "test")


def test_cross_workflow_same_expanded_matrix_check_still_bails_unpinnable():
    # Guard the boundary the #118 reorder must NOT cross: when TWO workflows each carry a
    # matrix-templated `test` job whose legs expand to the SAME check name (reth's live
    # super-case — unit.yml `test / ${{ matrix.type }}` AND integration.yml
    # `test / ${{ matrix.network }}`, both with an `ethereum` leg → two identically-named
    # `test / ethereum` check-runs), the check is genuinely unpinnable to ONE file. The
    # resolver must still refuse (return None) rather than silently guess a file — the
    # deliberate cross-workflow refusal (issue #59) is preserved.
    jg = {
        ".github/workflows/unit.yml": {
            "test": {"name": "test / ${{ matrix.type }}", "reusable": False, "matrix": True}},
        ".github/workflows/integration.yml": {
            "test": {"name": "test / ${{ matrix.network }}", "reusable": False, "matrix": True}},
    }
    crit = {
        ".github/workflows/unit.yml": {"job_p50": {"test / ethereum": 800.0}},
        ".github/workflows/integration.yml": {"job_p50": {"test / ethereum": 794.0}},
    }
    assert cr._check_to_job_node("test / ethereum", jg, crit) is None
    # ...but the candidate set IS known (the honest-labeling stamp `_decompose_pole` writes):
    # both producing workflows, so the summary/render can name them instead of calling the
    # real 13m job "fileless, don't investigate" (issue #118 honest-labeling arm).
    assert cr._check_producing_workflows(
        "test / ethereum", crit, require_developer_timing=True) == {
        ".github/workflows/unit.yml", ".github/workflows/integration.yml"}


def test_reusable_caller_wins_over_foreign_leading_placeholder_template():
    # Issue #118 follow-up (Greptile PR #126 P1): the reorder made the reusable-caller `" / "`
    # split the LAST resort, so a check like `Suite / build` first goes through the scanned
    # template pass. A FOREIGN matrix job in another workflow named `${{ matrix.variant }} / build`
    # compiles to `^.+? / build$` — whose leading `.+?` eats `Suite`, so it template-matches
    # `Suite / build` and (being the sole scanned match) would preempt the genuine reusable
    # caller `Suite`, mis-drilling the wrong workflow. `_check_to_job_node_scanned` now refuses a
    # LEADING-placeholder template for a `" / "`-bearing check (`_name_template_leads_with_placeholder`),
    # so the resolver falls through to the reusable caller as intended.
    jg = {
        ".github/workflows/reusable-caller.yml": {
            "suite": {"name": "Suite", "needs": [], "reusable": True, "matrix": False}},
        ".github/workflows/other.yml": {
            "buildjob": {"name": "${{ matrix.variant }} / build", "needs": [],
                         "reusable": False, "matrix": True}},
    }
    # RED before the leading-placeholder guard: mis-bound to other.yml/buildjob. GREEN: the
    # genuine reusable caller wins.
    assert cr._check_to_job_node("Suite / build", jg, {}) == (
        ".github/workflows/reusable-caller.yml", "suite")
    # The SIBLING static file-resolver (`_check_to_workflow_file_static`, used directly by the
    # structural-finding callers) carries the SAME guard (PR #126 review, second Greptile P1):
    # it too must NOT anchor the reusable check to the foreign matrix workflow — it has no
    # reusable fallback, so refusing the leading-placeholder match leaves it honestly unanchored
    # (None) rather than mis-bound to other.yml.
    assert cr._check_to_workflow_file_static("Suite / build", jg) is None
    # ...but a LITERAL-prefix `" / "` templated check (single producer) still resolves statically,
    # and an ordinary leading-placeholder matrix check without a `" / "` is unaffected.
    assert cr._check_to_workflow_file_static("test / ethereum", {
        ".github/workflows/unit.yml": {
            "test": {"name": "test / ${{ matrix.type }}", "reusable": False, "matrix": True}},
    }) == ".github/workflows/unit.yml"
    assert cr._check_to_workflow_file_static("ubuntu-build", {
        ".github/workflows/ci.yml": {
            "osbuild": {"name": "${{ matrix.os }}-build", "reusable": False, "matrix": True}},
    }) == ".github/workflows/ci.yml"
    # CRUCIALLY (PR #126 review, third Greptile P1): the refusal is scoped to an ACTUAL reusable
    # collision. A LONE leading-placeholder `" / "` job with NO competing reusable caller is the
    # check's real sole producer and must STILL bind — else `${{ matrix.variant }} / build`'s own
    # `linux / build` leg reads fileless. `_reusable_caller_claims` gates the refusal on a real caller.
    jg_lone = {".github/workflows/ci.yml": {
        "buildjob": {"name": "${{ matrix.variant }} / build", "needs": [],
                     "reusable": False, "matrix": True}}}
    assert cr._check_to_job_node("linux / build", jg_lone, {}) == (".github/workflows/ci.yml", "buildjob")
    assert cr._check_to_workflow_file_static("linux / build", jg_lone) == ".github/workflows/ci.yml"
    assert cr._reusable_caller_claims("Suite / build", jg) is True
    assert cr._reusable_caller_claims("linux / build", jg_lone) is False
    # The guard is scoped to `" / "` checks and leading placeholders only: an ORDINARY
    # leading-placeholder matrix check (no `" / "`) still binds via the scanned template pass.
    jg2 = {".github/workflows/ci.yml": {
        "osbuild": {"name": "${{ matrix.os }}-build", "needs": [],
                    "reusable": False, "matrix": True}}}
    assert cr._check_to_job_node("ubuntu-build", jg2, {}) == (".github/workflows/ci.yml", "osbuild")
    # ...and a LITERAL-prefix templated `" / "` job (the #118 core, `test / ${{…}}`) is untouched.
    assert cr._name_template_leads_with_placeholder("${{ matrix.variant }} / build") is True
    assert cr._name_template_leads_with_placeholder("test / ${{ matrix.type }}") is False


def test_sampled_anchor_step1_keeps_direct_ownership_over_a_foreign_reusable_caller():
    # PR #126 review (fifth Greptile P1, correcting the fourth): the step-1 leading-placeholder
    # guard must be scoped to a NON-direct (token-subset) anchor. When `_map_check_to_job` anchors
    # `linux / build` to workflow A because A DIRECTLY sampled a job named `linux / build` (its
    # `${{ matrix.variant }} / build` leg, variant=linux), A definitively produced the check — a
    # same-named reusable `linux` caller in ANOTHER file B must NOT steal it. Over-broad refusal
    # would reject A's real match, the scanned pass would reject it again, and the check would be
    # mis-attributed to B. Direct sampled ownership keeps A's matrix match.
    jg = {
        ".github/workflows/a.yml": {
            "buildjob": {"name": "${{ matrix.variant }} / build", "needs": [],
                         "reusable": False, "matrix": True}},
        ".github/workflows/b.yml": {
            "linux": {"name": "linux", "needs": [], "reusable": True, "matrix": False}},
    }
    crit = {".github/workflows/a.yml": {"job_p50": {"linux / build": 500.0}}}
    # RED before the direct-ownership carve-out: A's match refused → mis-attributed to B's `linux`
    # caller. GREEN: A directly owns `linux / build`, so its matrix job binds.
    assert cr._check_to_job_node("linux / build", jg, crit) == (".github/workflows/a.yml", "buildjob")
    # The scanned/no-sampled-anchor collision path still defers to the reusable caller (issue #118
    # core): with NO sampled timing there is no direct owner, so a foreign leading-placeholder
    # template can't preempt a genuine reusable caller.
    jg_scan = {
        ".github/workflows/reusable-caller.yml": {
            "suite": {"name": "Suite", "needs": [], "reusable": True, "matrix": False}},
        ".github/workflows/other.yml": {
            "buildjob": {"name": "${{ matrix.variant }} / build", "needs": [],
                         "reusable": False, "matrix": True}},
    }
    assert cr._check_to_job_node("Suite / build", jg_scan, {}) == (
        ".github/workflows/reusable-caller.yml", "suite")
    # And the counterpart (PR #126 6th Greptile P1): when the reusable caller lives in the SAME
    # workflow as the matrix job and that workflow directly sampled the check, exact timing proves
    # only that the WORKFLOW ran the check — not that the leading-placeholder matrix TEMPLATE owns
    # it. The co-resident reusable caller keeps precedence over the matrix template.
    jg_same = {".github/workflows/a.yml": {
        "suite": {"name": "Suite", "needs": [], "reusable": True, "matrix": False},
        "buildjob": {"name": "${{ matrix.variant }} / build", "needs": [],
                     "reusable": False, "matrix": True},
    }}
    crit_same = {".github/workflows/a.yml": {"job_p50": {"Suite / build": 500.0}}}
    assert cr._check_to_job_node("Suite / build", jg_same, crit_same) == (
        ".github/workflows/a.yml", "suite")


def test_single_producer_templated_check_resolves_and_is_not_stamped():
    # The stamp boundary (issue #118 honest-labeling): the `ambiguous_workflows` stamp fires in
    # `_decompose_pole` only when the resolver returns None AND >1 workflow produces the check.
    # This pins the "exactly 1 producer" side: a single-producer templated check RESOLVES to its
    # one file (resolver is NOT None), so the else-branch/stamp is never reached — it takes the
    # file-backed step drill instead. A regression relaxing the `> 1` gate to `>= 1` would wrongly
    # stamp this resolvable pole; this test guards that seam alongside the >1 bail test above.
    jg = {".github/workflows/unit.yml": {
        "test": {"name": "test / ${{ matrix.type }}", "reusable": False, "matrix": True}}}
    crit = {".github/workflows/unit.yml": {"job_p50": {"test / ethereum": 800.0}}}
    # Resolves (not None) → file-backed path, no stamp.
    assert cr._check_to_job_node("test / ethereum", jg, crit) == (".github/workflows/unit.yml", "test")
    # And exactly one developer-timed producer → the stamp gate (`> 1`) is False.
    producing = cr._check_producing_workflows("test / ethereum", crit, require_developer_timing=True)
    assert producing == {".github/workflows/unit.yml"} and len(producing) == 1


def test_static_name_matrix_check_resolves_consistently_across_all_three_mappers():
    # The static-name matrix tolerance must apply to ALL three static check→job/file mappers,
    # not just `_check_to_job_node` — else the SAME check renders file-backed in one report site
    # (required-reachability) and fileless/external in another (OPT75 evidence via
    # `_check_to_workflow_file_static`; populations labeling via `_check_to_job_node_scanned`),
    # a self-contradiction. All three must map `Matrix Test (1.13.*)` to the file-backed job and
    # must not over-match a prefix-sharing check.
    wf = ".github/workflows/test.yml"
    job_graph = {wf: {
        "test": {"name": "Matrix Test", "needs": [], "reusable": False, "matrix": True},
        "lint": {"name": "Lint", "needs": [], "reusable": False, "matrix": False}}}
    assert cr._check_to_workflow_file_static("Matrix Test (1.13.*)", job_graph) == wf
    assert cr._check_to_job_node_scanned("Matrix Test (1.13.*)", job_graph) == (wf, "test")
    # No over-match: a prefix-sharing check with no appended-leg boundary stays unmapped in both.
    assert cr._check_to_workflow_file_static("Matrix Testing", job_graph) is None
    assert cr._check_to_job_node_scanned("Matrix Testing", job_graph) is None


def test_required_reachable_keeps_a_fileless_required_check():
    # A required check no sampled job produced (a managed bot, no job graph entry) is kept
    # via literal set membership — it can't anchor a needs-closure, but it IS merge-gating.
    job_graph = {".github/workflows/ci.yml": {
        "b": {"name": "build", "needs": [], "reusable": False}}}
    crit_by_wf = {".github/workflows/ci.yml": {"job_p50": {"build": 100.0}}}
    keep = cr._required_reachable_checks(
        {"Some Managed Bot", "build"}, frozenset({"Some Managed Bot"}), job_graph, crit_by_wf)
    assert "Some Managed Bot" in keep          # required, kept despite having no job
    assert "build" not in keep                 # not required and nothing required needs it


def test_required_reachable_falls_back_to_literal_membership_without_a_graph():
    # No job graph (degraded scan) → can't compute reachability → keep only the literal
    # required set, the conservative subset (never guess a non-required check is gating).
    crit_by_wf = {".github/workflows/ci.yml": {"job_p50": {"build": 100.0, "lint": 50.0}}}
    keep = cr._required_reachable_checks(
        {"build", "lint"}, frozenset({"build"}), None, crit_by_wf)
    assert keep == {"build"}


def test_required_reachable_terminates_on_a_needs_cycle():
    # A malformed `needs:` cycle (a needs b, b needs a) must not hang the downward-closure
    # walk — termination relies on the `node not in reachable` guard. Both jobs are reached
    # (required `a` anchors, pulls in `b`, and the back-edge to `a` is a no-op), and the walk
    # returns rather than looping forever.
    job_graph = {".github/workflows/ci.yml": {
        "a": {"name": "a", "needs": ["b"], "reusable": False},
        "b": {"name": "b", "needs": ["a"], "reusable": False}}}
    crit_by_wf = {".github/workflows/ci.yml": {"job_p50": {"a": 100.0, "b": 50.0}}}
    keep = cr._required_reachable_checks(
        {"a", "b"}, frozenset({"a"}), job_graph, crit_by_wf)
    assert keep == {"a", "b"}          # both reachable, and the walk terminated


def test_required_reachable_ignores_a_dangling_needs_reference():
    # A `needs:` pointing at a non-existent job id (a typo, or a job removed from the YAML)
    # must be skipped, not crash or invent a phantom node — the `if dep in job_graph[wf]`
    # guard. The real dep is still pulled in; the dangling one contributes nothing.
    job_graph = {".github/workflows/ci.yml": {
        "agg":  {"name": "agg", "needs": ["build", "ghost"], "reusable": False},
        "build": {"name": "build", "needs": [], "reusable": False},
        "other": {"name": "other", "needs": [], "reusable": False}}}   # resolvable, unreachable
    crit_by_wf = {".github/workflows/ci.yml": {
        "job_p50": {"agg": 10.0, "build": 200.0, "other": 50.0}}}
    keep = cr._required_reachable_checks(
        {"agg", "build", "other"}, frozenset({"agg"}), job_graph, crit_by_wf)
    assert keep == {"agg", "build"}   # `ghost` skipped; unrelated `other` resolvable but not reached


def test_name_template_regex_expands_matrix_placeholders():
    # `_name_template_regex` turns a job `name:` template into a matcher for its expanded
    # check-runs. Exercise the branches the rollup/bare-aggregator fixtures don't reach:
    # multiple placeholders, a placeholder at the start AND end, and regex-special chars in
    # the literal spans (the `(` `)` must stay escaped around the `.+?`).
    multi = cr._name_template_regex(
        "Test (${{ matrix.os }}, Node ${{ matrix.node }})")
    assert multi.match("Test (ubuntu-latest, Node 22)")
    assert not multi.match("Test (ubuntu-latest, Node 22) extra")   # anchored ^...$
    edges = cr._name_template_regex("${{ matrix.shard }} of ${{ matrix.total }}")
    assert edges.match("4 of 8")                                    # leading + trailing placeholder
    plain = cr._name_template_regex("Lint")
    assert plain.match("Lint") and not plain.match("Lint and Format")  # no placeholder -> exact


def test_check_to_job_node_resolves_a_matrix_leg_via_the_template_branch():
    # The matrix-template fallback in `_check_to_job_node` (the second loop, after exact
    # display-name match fails) for a PLAIN (non-reusable) job. A sharded check-run resolves
    # to its matrix job only via the regex branch — the rollup fixtures all hit the first
    # (exact) loop, so this pins the second.
    job_graph = {".github/workflows/ci.yml": {
        "test": {"name": "UNIT Test (Shard ${{ matrix.shard }})",
                 "needs": [], "reusable": False}}}
    crit_by_wf = {".github/workflows/ci.yml": {
        "job_p50": {"UNIT Test (Shard 4)": 300.0}}}
    node = cr._check_to_job_node("UNIT Test (Shard 4)", job_graph, crit_by_wf)
    assert node == (".github/workflows/ci.yml", "test")


def test_affected_jobs_all_rare_conservative_guards():
    # The demotion floors a cluster's wall-clock to 0 — a wrong demotion silently deletes a real
    # long-pole win, so every "never demote on partial information" guard must hold. The rule is
    # POLE-FREQUENCY (mirror of the spine's `is_rare`): demote only when every affected job is the
    # actual pole on FEWER than `_POLE_RECUR_FLOOR` PRs (a never/rarely-slowest job saves no
    # merge-wait). The 2nd arg is now `check_pole_freq`, not presence.
    assert cr._POLE_RECUR_FLOOR == 2
    req = frozenset({"required-job"})
    # All one-path (pole on < floor PRs, n >= MIN_PR, known, not required) → demote to bill-only.
    assert cr._affected_jobs_all_rare(
        ["app", "web"], {"app": 1, "web": 0}, 20, frozenset()) is True
    # Boundary: pole on exactly floor (2) is a genuine recurring gate → NOT rare → keep the credit.
    assert cr._affected_jobs_all_rare(
        ["app"], {"app": 2}, 20, frozenset()) is False
    # Just below the floor (1) → one-path outlier → demote.
    assert cr._affected_jobs_all_rare(
        ["app"], {"app": 1}, 20, frozenset()) is True
    # A never-slowest job (pole on 0 PRs, even if present on many) → demote — the expo/expo case a
    # PRESENCE rule got wrong (it kept the credit for a majority-present-but-never-slowest job).
    assert cr._affected_jobs_all_rare(
        ["app"], {"app": 0}, 20, frozenset()) is True
    # Below the min sampled-PR floor → frequency is noise → never demote.
    assert cr._affected_jobs_all_rare(
        ["app"], {"app": 0}, cr._RARE_PRESENCE_MIN_PR - 1, frozenset()) is False
    # An affected job with NO resolved pole-frequency is UNKNOWN, not rare → never demote.
    assert cr._affected_jobs_all_rare(
        ["app", "web"], {"app": 0}, 20, frozenset()) is False
    # ANY required job keeps the credit (a real gate is never demoted).
    assert cr._affected_jobs_all_rare(
        ["app", "required-job"], {"app": 0, "required-job": 0}, 20, req) is False
    # An empty affected set is UNKNOWN, not rare.
    assert cr._affected_jobs_all_rare([], {}, 20, frozenset()) is False


def test_check_to_workflow_file_static_unique_template_and_ambiguous():
    # Resolves a check to its producing workflow FILE from the static job graph alone
    # (independent of sampled timing), but ONLY when exactly one workflow's job matches —
    # a cross-file ambiguous match must return None so the finding stays honestly unanchored.
    # Unique literal-name match → the file.
    jg_unique = {".github/workflows/check-typo.yml": {
        "check-typo": {"name": "Check Typo using codespell", "needs": [], "reusable": False}}}
    assert cr._check_to_workflow_file_static(
        "Check Typo using codespell", jg_unique) == ".github/workflows/check-typo.yml"
    # Unique match via a matrix `name:` TEMPLATE (not a literal).
    jg_template = {".github/workflows/test.yml": {
        "py": {"name": "Python ${{ matrix.python }}", "needs": [], "reusable": False}}}
    assert cr._check_to_workflow_file_static(
        "Python 3.13", jg_template) == ".github/workflows/test.yml"
    # AMBIGUOUS: the same templated name in TWO workflows → None (the collision case the
    # whole PR fights; never bind to an arbitrary one).
    jg_ambiguous = {
        ".github/workflows/datasets-test.yml": {
            "py": {"name": "Python ${{ matrix.python }}", "needs": [], "reusable": False}},
        ".github/workflows/framework-test.yml": {
            "py": {"name": "Python ${{ matrix.python }}", "needs": [], "reusable": False}}}
    assert cr._check_to_workflow_file_static("Python 3.13", jg_ambiguous) is None
    # Zero matches → None; no job graph → None.
    assert cr._check_to_workflow_file_static("Nonexistent", jg_unique) is None
    assert cr._check_to_workflow_file_static("Check Typo using codespell", None) is None


def test_map_check_to_job_subset_rejects_a_fused_compound_token_match():
    # Regression (expo/expo pole 2): the check `macos-build` is its OWN job in
    # test-suite-macos.yml, but that job was never SAMPLED (no job_p50 entry).
    # The only timed job whose tokens are a subset is expotools.yml's `build`
    # ({build} ⊂ {macos, build}) — a DIFFERENT 114s TypeScript-compile job on a
    # different runner. The old token-subset fallback bound `macos-build` (an 853s
    # gate) to expotools/build, drilling the wrong job and leaking "42% of job
    # `build`". `build` is fused into the hyphen-compound `macos-build`, not a
    # scope/matrix PREFIX of it, so the subset match must be REJECTED → None.
    crit_by_wf = {
        ".github/workflows/expotools.yml": {"job_p50": {"build": 114.0}},
    }
    assert cr._map_check_to_job("macos-build", crit_by_wf) is None
    # `macos_build` (underscore compound) is the same fusion case.
    assert cr._map_check_to_job("macos_build", crit_by_wf) is None
    # And `buildkite` must not bind to `build` via a right-edge fusion.
    assert cr._map_check_to_job(
        "buildkite", {".github/workflows/x.yml": {"job_p50": {"build": 9.0}}}
    ) is None


def test_map_check_to_job_subset_still_maps_a_scope_prefixed_matrix_check():
    # No-regression: the legitimate subset case the fallback exists for — a
    # monorepo check-run prepends the package scope (space-separated) to a
    # reusable/matrix job's name. `Integration Test` IS a clean, whitespace-bounded
    # suffix of `@scope/pkg Integration Test`, so the subset match still fires.
    crit_by_wf = {
        ".github/workflows/ci.yml": {"job_p50": {"Integration Test": 300.0}},
    }
    assert cr._map_check_to_job("@scope/pkg Integration Test", crit_by_wf) == (
        ".github/workflows/ci.yml", "Integration Test")
    # A reusable-child `Suite / build` likewise scope-prefixes `build`.
    assert cr._map_check_to_job(
        "Suite / build", {".github/workflows/s.yml": {"job_p50": {"build": 50.0}}}
    ) == (".github/workflows/s.yml", "build")


def test_map_check_to_job_bails_on_cross_workflow_same_name_ambiguity():
    # Issue #59: a monorepo declares a same-named job (`Build`) in TWO package workflows
    # with DIFFERENT timings/steps. GitHub gives both check-runs the identical name `Build`
    # and the check-runs endpoint carries no workflow path, so there is no evidence here to
    # pick the right file. The OLD rule kept the SLOWEST match, mis-attributing the pole's
    # workflow_file / step decomposition / fix recipe to the WRONG workflow. The mapper must
    # now REFUSE to guess and return None (the honest unmapped path), NOT the slowest.
    crit_by_wf = {
        ".github/workflows/pkg-a.yml": {"job_p50": {"Build": 120.0}},
        ".github/workflows/pkg-b.yml": {"job_p50": {"Build": 900.0}},  # slower — old winner
    }
    # RED before the fix (returned ('.github/workflows/pkg-b.yml', 'Build')); GREEN after.
    assert cr._map_check_to_job("Build", crit_by_wf) is None
    assert cr._map_check_to_job(
        "Build", crit_by_wf, require_developer_timing=True) is None
    # The ambiguity is precisely observable — both workflows produce the check.
    assert cr._check_producing_workflows("Build", crit_by_wf) == {
        ".github/workflows/pkg-a.yml", ".github/workflows/pkg-b.yml"}
    # Subset-tier ambiguity bails too: `@x/pkg Build` scope-prefixes a `Build` job in BOTH.
    assert cr._map_check_to_job("@x/pkg Build", crit_by_wf) is None


def test_map_check_to_job_developer_timing_filter_disambiguates_before_bailing():
    # The require_developer_timing filter is itself evidence: when only ONE candidate
    # workflow is PR/merge-timed (the other measured on push/schedule → event_scope
    # all-events), the ambiguity is resolved and the mapper binds the dev-timed file
    # cleanly instead of bailing. Without the filter both match → ambiguous → None.
    crit_by_wf = {
        ".github/workflows/pkg-a.yml": {
            "job_p50": {"Build": 120.0}},                       # developer-timed (default)
        ".github/workflows/pkg-b.yml": {
            "event_scope": "all-events", "job_p50": {"Build": 900.0}},  # push/schedule only
    }
    assert cr._map_check_to_job(
        "Build", crit_by_wf, require_developer_timing=True) == (
        ".github/workflows/pkg-a.yml", "Build")
    # Unfiltered, both are candidates → genuinely ambiguous → bail.
    assert cr._map_check_to_job("Build", crit_by_wf) is None


def test_map_check_to_job_single_workflow_multi_job_slowest_unchanged():
    # No-regression pin: same-named jobs WITHIN one workflow are NOT a file-attribution
    # ambiguity (matrix legs / two jobs whose display names tokenize identically). The
    # mapper still returns the slowest, exactly as before — only CROSS-workflow collisions
    # bail. Byte-identical to the pre-fix result for every unambiguous (one-workflow) input.
    crit_by_wf = {
        ".github/workflows/ci.yml": {
            "job_p50": {"Integration Test": 300.0, "Test Integration": 900.0}},
    }
    # Both jobs tokenize to {integration, test} == the check's tokens; one workflow, so the
    # slowest wins (unchanged behaviour) — no bail.
    assert cr._map_check_to_job("Integration Test", crit_by_wf) == (
        ".github/workflows/ci.yml", "Test Integration")


def test_ambiguous_check_disclosed_as_cross_workflow_not_fileless():
    # L8 honest degradation: when the mapper bails on cross-workflow ambiguity, the pole
    # must NOT be mislabelled a "fileless / third-party app check" (it IS produced by
    # in-repo workflows) and must NOT be silently dropped. The disclosure names the real
    # cause — a same-named job in more than one workflow — with an actionable remedy.
    crit_by_wf = {
        ".github/workflows/pkg-a.yml": {"job_p50": {"Build": 120.0}},
        ".github/workflows/pkg-b.yml": {"job_p50": {"Build": 900.0}},
    }
    jpr = {".github/workflows/pkg-a.yml": [], ".github/workflows/pkg-b.yml": []}
    pr_checks = (("Build", 900.0),)
    events = {".github/workflows/pkg-a.yml": {"pull_request"},
              ".github/workflows/pkg-b.yml": {"pull_request"}}
    job_graph = {
        ".github/workflows/pkg-a.yml": {"build": {
            "name": "Build", "needs": [], "reusable": False}},
        ".github/workflows/pkg-b.yml": {"build": {
            "name": "Build", "needs": [], "reusable": False}},
    }
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, None, events, {}, 0, job_graph=job_graph)
    build = [f for f in out if "Build" in f.get("title", "")]
    assert build, "the ambiguous pole must still surface a finding (never silently dropped)"
    ev = " ".join((f.get("evidence") or "") for f in build)
    assert "MORE THAN ONE workflow" in ev, (
        f"ambiguous check must be disclosed as cross-workflow, got: {ev!r}")
    assert "third-party app check" not in ev and "fileless check" not in ev, (
        f"a file-backed ambiguous check must NOT be called fileless/third-party: {ev!r}")


def test_leg_feeding_a_required_rollup_is_stamped_required_never_detriggered():
    # The required_status stamp must use the SAME needs-reachability as the spine filter.
    # The required `Suite / Merge Reports` rollup is produced by a reusable caller `Suite`;
    # its sibling shard `Suite / UNIT Test (Shard 1)` is reachable (same reusable
    # invocation) → stamped "required", never offered an OPT71 de-trigger of a merge-gating
    # leg.
    shard = [("Checkout", 20), ("Install deps", 40), ("Run tests", 300)]
    rollup = [("Checkout", 10), ("Merge reports", 20)]
    runs = [[_job("Suite / UNIT Test (Shard 1)", shard),
             _job("Suite / Merge Reports", rollup)] for _ in range(5)]
    jpr = {".github/workflows/prebuild.yml": runs}
    crit_by_wf = {".github/workflows/prebuild.yml": cr._critical_path(runs)}
    pr_checks = (("Suite / UNIT Test (Shard 1)", 360.0),
                 ("Suite / Merge Reports", 30.0))
    events = {".github/workflows/prebuild.yml": {"pull_request"}}
    job_graph = {".github/workflows/prebuild.yml": {
        "suite": {"name": "Suite", "needs": [], "reusable": True}}}
    required = cr.RequiredChecks(frozenset({"Suite / Merge Reports"}), complete=True)
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, {}, 0, job_graph=job_graph)
    shard_f = [f for f in out if "Shard 1" in f["title"]]
    assert shard_f, "expected a structural finding for the sharded leg"
    for f in shard_f:
        assert f["required_status"] == "required"     # reachable from the required rollup
    # No OPT71 de-trigger of a merge-gating leg.
    assert not [f for f in out if f["pattern"] == "OPT71"]


class _FakeRulesetClient:
    """Stub GhClient.json driving _fetch_required_checks: maps endpoint substrings
    to canned responses (a value, or None to simulate a failed/404 fetch)."""

    def __init__(self, responses: dict):
        self.responses = responses

    def json(self, endpoint: str, allow_missing: bool = False):
        for key, val in self.responses.items():
            if key in endpoint:
                return val
        return None


_RULE_BUILD = {"rules": [{"type": "required_status_checks",
                          "parameters": {"required_status_checks": [
                              {"context": "build"}]}}]}

# An ACTIVE ruleset scoped to the default branch — the live merge gate.
_RULE_ACTIVE_MAIN = {
    "target": "branch", "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}},
    "rules": [{"type": "required_status_checks",
               "parameters": {"required_status_checks": [{"context": "build"}]}}]}
# Active, but scoped to release/* — does NOT gate main.
_RULE_RELEASE = {
    "target": "branch", "enforcement": "active",
    "conditions": {"ref_name": {"include": ["refs/heads/release/*"], "exclude": []}},
    "rules": [{"type": "required_status_checks",
               "parameters": {"required_status_checks": [{"context": "release-only"}]}}]}
# Default-branch scoped but in `evaluate` (dry-run) mode — not a live gate.
_RULE_EVALUATE = {
    "target": "branch", "enforcement": "evaluate",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}},
    "rules": [{"type": "required_status_checks",
               "parameters": {"required_status_checks": [{"context": "dry-run-check"}]}}]}


def test_ruleset_ref_in_scope_predicate():
    # The pure scoping predicate: only an affirmatively out-of-scope ruleset returns
    # False; everything ambiguous is conservatively kept (never under-detect a gate).
    f = cr._ruleset_ref_in_scope
    # in scope: the specials, an exact ref, and a glob covering the branch
    assert f({"conditions": {"ref_name": {"include": ["~ALL"]}}}, "main")
    assert f({"conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}}}, "main")
    assert f({"conditions": {"ref_name": {"include": ["refs/heads/main"]}}}, "main")
    assert f({"conditions": {"ref_name": {"include": ["refs/heads/ma*"]}}}, "main")
    # out of scope: a release/*-only ruleset, or one targeting tags
    assert not f({"conditions": {"ref_name": {"include": ["refs/heads/release/*"]}}}, "main")
    assert not f({"target": "tag", "conditions": {"ref_name": {"include": ["~ALL"]}}}, "main")
    # exclude wins over include
    assert not f({"conditions": {"ref_name": {
        "include": ["~ALL"], "exclude": ["refs/heads/main"]}}}, "main")
    # conservative keeps: unknown branch, or absent / odd conditions
    assert f({"conditions": {"ref_name": {"include": ["refs/heads/release/*"]}}}, None)
    assert f({}, "main")
    assert f({"conditions": {}}, "main")
    assert f({"conditions": {"ref_name": {}}}, "main")
    # a PRESENT-but-empty include is "matches no ref" (gates nothing), NOT a keep:
    # dropping it can't under-detect a gate.
    assert not f({"conditions": {"ref_name": {"include": []}}}, "main")
    # a non-empty but UNREADABLE include (only non-string entries, e.g. a malformed
    # [null] from the API) is a conservative KEEP — we can't affirm out-of-scope.
    assert f({"conditions": {"ref_name": {"include": [None, 123]}}}, "main")


def test_fetch_required_checks_skips_release_scoped_ruleset():
    # A required_status_checks rule on a `release/*`-scoped ruleset is NOT required for
    # main — harvesting it would let a release-only check headline the PR critical path.
    client = _FakeRulesetClient({
        "rulesets?includes_parents": [{"id": 1}, {"id": 2}],
        "rulesets/1": _RULE_ACTIVE_MAIN,
        "rulesets/2": _RULE_RELEASE,
    })
    rc = cr._fetch_required_checks(client, "demo/demo", "main")
    assert rc is not None
    assert "build" in rc.names           # the default-branch gate survives
    assert "release-only" not in rc.names  # the release/*-scoped one is dropped


def test_fetch_required_checks_skips_evaluate_mode_ruleset():
    # An `evaluate` (dry-run) ruleset does not block a merge, so its checks are not
    # required — treating them as a live gate is the langfuse-class false "required".
    client = _FakeRulesetClient({
        "rulesets?includes_parents": [{"id": 1}, {"id": 2}],
        "rulesets/1": _RULE_ACTIVE_MAIN,
        "rulesets/2": _RULE_EVALUATE,
    })
    rc = cr._fetch_required_checks(client, "demo/demo", "main")
    assert rc is not None
    assert "build" in rc.names
    assert "dry-run-check" not in rc.names


def test_fetch_required_checks_keeps_active_default_branch_scoped():
    # Sanity that the new filters are not over-eager: an ACTIVE, default-branch-scoped
    # ruleset still contributes its required check, and the read is complete.
    client = _FakeRulesetClient({
        "rulesets?includes_parents": [{"id": 1}],
        "rulesets/1": _RULE_ACTIVE_MAIN,
    })
    rc = cr._fetch_required_checks(client, "demo/demo", "main")
    assert rc is not None
    assert rc.names == frozenset({"build"})
    assert rc.complete is True


def test_pole_provenance_maps_three_states():
    # The cross-repo provenance stamp the ci-harness gate reads: fallback wins, a
    # required-scoped spine is `required_scoped`, and a picked-but-unconfirmed pole is
    # `unresolved` (the harness must HALT rather than optimize it).
    p = cr._pole_provenance
    assert p("pr_floor_fallback", False) == "pr_floor_fallback"
    assert p("pr_floor_fallback", True) == "pr_floor_fallback"
    assert p(None, True) == "required_scoped"
    assert p(None, False) == "unresolved"


def test_fetch_required_checks_partial_when_a_ruleset_detail_fails():
    # The list is readable (two rulesets exist) but one detail fetch fails. The
    # names we DID read are real, but the read is incomplete — so complete=False,
    # and the consumer must treat absent checks as unknown, not not-required.
    client = _FakeRulesetClient({
        "rulesets?includes_parents": [{"id": 1}, {"id": 2}],
        "rulesets/1": _RULE_BUILD,
        "rulesets/2": None,  # detail fetch failed → partial read
    })
    rc = cr._fetch_required_checks(client, "demo/demo", "main")
    assert rc is not None
    assert rc.names == frozenset({"build"})
    assert rc.complete is False


def test_fetch_required_checks_complete_when_all_rulesets_read():
    client = _FakeRulesetClient({
        "rulesets?includes_parents": [{"id": 1}],
        "rulesets/1": _RULE_BUILD,
        # classic protection 404s (expected without admin) → does not taint
        # completeness because a ruleset already answered.
    })
    rc = cr._fetch_required_checks(client, "demo/demo", "main")
    assert rc is not None
    assert rc.names == frozenset({"build"})
    assert rc.complete is True


def test_fetch_required_checks_unknown_branch_marks_incomplete():
    # default branch couldn't be resolved (branch=None) → classic protection is
    # skipped rather than guessed, and the read is marked incomplete.
    client = _FakeRulesetClient({"rulesets?includes_parents": []})
    rc = cr._fetch_required_checks(client, "demo/demo", None)
    assert rc is not None
    assert rc.complete is False


def test_fetch_required_checks_none_when_nothing_readable():
    # Rulesets + protection all 404 (auditing a repo you don't own) → None, the
    # "required status entirely UNKNOWN" sentinel.
    client = _FakeRulesetClient({})
    rc = cr._fetch_required_checks(client, "demo/demo", "main")
    assert rc is None


def test_partial_required_read_never_asserts_not_required():
    # We read SOME required checks but not all (a ruleset detail fetch failed) →
    # complete=False. A check ABSENT from the partial set is UNKNOWN, never
    # not-required — so we never recommend de-triggering (OPT71) a check that an
    # unread ruleset might actually require. This is the dangerous false-negative
    # the completeness flag exists to prevent.
    partial = cr.RequiredChecks(frozenset({"build-and-test"}), complete=False)
    out = _scenario(required=partial)
    assert out
    statuses = {f.get("required_status") for f in out}
    assert "not-required" not in statuses
    assert "unknown" in statuses
    assert not [f for f in out if f["pattern"] == "OPT71"]


def test_test_dominant_pole_routes_to_neutral_opt75_not_scope_drop_opt70():
    # A TEST-dominant pole must NOT route to OPT70 ("Scope the Build/Test to Only
    # What Changed", HIGH coverage-loss risk) — its real fix is usually
    # shard/parallelize, and stamping a "drop your tests" title + scary risk banner
    # on a safe parallelism change is dangerous. It routes to the NEUTRAL OPT75
    # (MEDIUM risk); the fix step names the actual lever from the measured data.
    steps = [("Checkout", 20), ("Install deps", 40), ("Build", 60), ("Run tests", 300)]
    runs = [[_job("build-and-test", steps)] for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit_by_wf = {".github/workflows/ci.yml": cr._critical_path(runs)}
    pr_checks = (("build-and-test", 420.0),)
    events = {".github/workflows/ci.yml": {"pull_request"}}
    required = cr.RequiredChecks(frozenset({"build-and-test"}), complete=True)
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, {}, 0)
    pole = [f for f in out if "build-and-test" in f["title"]]
    assert pole and pole[0]["pattern"] == "OPT75", "test pole must route to neutral OPT75"
    assert pole[0]["risk"] == "MEDIUM"  # never OPT70's HIGH coverage-loss risk
    assert pole[0]["decomposition"]["dominant_category"] == "test"


def test_build_dominant_pole_below_ratio_routes_to_opt70_scope():
    # A BUILD-dominant pole with redundant_ratio <= 2.0 (so not the OPT72 cache
    # path) is where OPT70 (scope the build to changed targets) legitimately
    # belongs — scoping a build is a real, if HIGH-risk, lever.
    steps = [("Checkout", 20), ("Install deps", 40), ("Build", 300), ("Run tests", 250)]
    runs = [[_job("build-and-test", steps)] for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit_by_wf = {".github/workflows/ci.yml": cr._critical_path(runs)}
    pr_checks = (("build-and-test", 610.0),)
    events = {".github/workflows/ci.yml": {"pull_request"}}
    required = cr.RequiredChecks(frozenset({"build-and-test"}), complete=True)
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, {}, 0)
    pole = [f for f in out if "build-and-test" in f["title"]]
    assert pole and pole[0]["pattern"] == "OPT70", "build pole (ratio<=2) routes to OPT70"
    assert pole[0]["risk"] == "HIGH"
    assert pole[0]["decomposition"]["dominant_category"] == "build"
    assert pole[0]["decomposition"]["redundant_ratio"] <= 2.0


def test_hygiene_covered_pole_is_not_double_surfaced_structurally():
    # A pole MATERIALLY shortened by a hygiene finding (covers >= half its p50)
    # must NOT be re-surfaced as a per-check structural candidate — that would
    # double-count the same saving. A trivial cover does NOT suppress it.
    runs = [[_job("build-and-test", _BUILD_TEST), _job("lint", _LINT)]
            for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit_by_wf = {".github/workflows/ci.yml": cr._critical_path(runs)}
    pr_checks = (("build-and-test", 300.0), ("lint", 100.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    required = cr.RequiredChecks(frozenset({"build-and-test"}), complete=True)
    # 200s saving on a 300s pole (>= 150 = half) -> materially covered -> suppressed.
    covered = {cr._struct_toks("build-and-test"): 200.0}
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, covered, 0)
    assert not [f for f in out if "build-and-test" in f.get("title", "")], \
        "a materially-covered pole must not be re-surfaced as a per-check candidate"
    # A TRIVIAL cover (10s on a 300s pole) must NOT suppress the structural lever.
    trivial = {cr._struct_toks("build-and-test"): 10.0}
    out2 = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, trivial, 0)
    assert [f for f in out2 if "build-and-test" in f.get("title", "")], \
        "a trivial hygiene cover must NOT suppress the pole's structural lever"


def test_build_covered_job_savings_excludes_queue_axis_findings():
    # Regression: a queue-time (OPT43) finding's wall_clock_p50_s measures
    # PRE-START wait, not a reduction of the job's RUN time, so it must NOT be
    # recorded in covered_job_savings — otherwise it suppresses that pole's
    # structural RUN-time lever against the wrong axis (superfly/litefs:
    # `Unit Tests (wal)` queue 107.9s wrongly hid its OPT75).
    findings = [
        {"pattern": "OPT43", "wall_clock_p50_s": 107.9,
         "affected_jobs": ["Unit Tests (wal)"]},
        {"pattern": "OPT5", "wall_clock_p50_s": 30.0,
         "affected_jobs": ["Unit Tests (delete)"]},
    ]
    covered = cr._build_covered_job_savings(findings)
    assert cr._struct_toks("Unit Tests (wal)") not in covered, \
        "a queue-time (OPT43) saving must not enter covered_job_savings"
    # A genuine RUN-time hygiene saving is still recorded.
    assert covered[cr._struct_toks("Unit Tests (delete)")] == 30.0


def test_queue_time_saving_does_not_suppress_the_pole_structural_lever():
    # End-to-end: the headline gate `Unit Tests (wal)` is TEST-dominant (real
    # lever = OPT75). It also has a P90 queue wait of 107.9s (> half its 200s
    # p50). Building `covered` the real way (via _build_covered_job_savings)
    # must NOT let that queue-axis saving suppress the OPT75 on `wal`.
    steps = [("Checkout", 10), ("Install deps", 20), ("Run tests", 170)]
    runs = [[_job("Unit Tests (wal)", steps)] for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit_by_wf = {".github/workflows/ci.yml": cr._critical_path(runs)}
    pr_checks = (("Unit Tests (wal)", 200.0),)
    events = {".github/workflows/ci.yml": {"pull_request"}}
    required = cr.RequiredChecks(frozenset({"Unit Tests (wal)"}), complete=True)
    # The OPT43 queue finding on the SAME pole: 107.9s >= half of 200s.
    queue_finding = [{"pattern": "OPT43", "wall_clock_p50_s": 107.9,
                      "affected_jobs": ["Unit Tests (wal)"]}]
    covered = cr._build_covered_job_savings(queue_finding)
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, covered, 0)
    pole = [f for f in out if "wal" in f.get("title", "")]
    assert pole and pole[0]["pattern"] == "OPT75", \
        "a queue-time saving must not suppress the headline pole's OPT75 lever"
def test_subset_token_cover_does_not_suppress_a_distinct_pole():
    # Regression for the cover-suppression match being EXACT job-token identity,
    # not a bidirectional subset. A hygiene saving on `Test` (`{test}`) must NOT
    # suppress a DISTINCT `Integration Test` (`{integration, test}`) pole — `{test}`
    # is a STRICT SUBSET of `{integration, test}`, so the OLD `jt <= ct or ct <= jt`
    # logic credited the cover to a job that never benefits and silently dropped the
    # real lever. With `jt == ct` the distinct pole keeps its structural finding.
    runs = [[_job("Integration Test", _BUILD_TEST), _job("lint", _LINT)]
            for _ in range(5)]
    jpr = {".github/workflows/ci.yml": runs}
    crit_by_wf = {".github/workflows/ci.yml": cr._critical_path(runs)}
    pr_checks = (("Integration Test", 300.0), ("lint", 100.0))
    events = {".github/workflows/ci.yml": {"pull_request"}}
    required = cr.RequiredChecks(frozenset({"Integration Test"}), complete=True)
    # 200s saving keyed on `Test` (`{test}`) — a strict subset of the pole's
    # `{integration, test}` and >= half the 300s pole, the exact shape the old
    # subset logic suppressed on.
    subset_cover = {cr._struct_toks("Test"): 200.0}
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, subset_cover, 0)
    assert [f for f in out if "Integration Test" in f.get("title", "")], \
        "a subset-token cover on a DIFFERENT job must not suppress this pole's lever"
    # Control: an EXACT-identity cover of the same size still suppresses (the
    # genuine double-count the guard is meant to prevent), proving the test
    # discriminates subset from identity rather than just always-passing.
    exact_cover = {cr._struct_toks("Integration Test"): 200.0}
    out2 = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, required, events, exact_cover, 0)
    assert not [f for f in out2 if "Integration Test" in f.get("title", "")], \
        "an exact-identity cover >= half the pole must still suppress it"


# --------------------------------------------------------------------------- #
# Sizing stays population-weighted and FLOOR-CAPPED
# --------------------------------------------------------------------------- #

def test_structural_saving_is_floor_capped_not_best_case():
    out = _scenario(required={"build-and-test"})
    top = [f for f in out if "build-and-test" in f["title"]][0]
    # Raw addressable build time is ~165s (180 − 15 warm floor), but the measured
    # critical-path floor is CodeQL at 250s and this workflow's pole is 300s, so
    # the saving is capped at the 50s headroom — never the raw best case.
    assert top["wall_clock_p50_s"] == 50.0
    assert top.get("wall_clock_uncapped_p50_s", 0) > 50.0
    deriv = top.get("wall_clock_derivation") or []
    assert any("critical-path floor" in d["reason"] for d in deriv)


def _struct(pattern, risk, wc, title):
    return {
        "id": "fx", "pattern": pattern, "pattern_class": "structural",
        "structural": True, "severity": "HIGH", "title": title, "line": 0,
        "workflow_file": ".github/workflows/ci.yml", "affected_jobs": ["j"],
        "risk": risk, "guardrail": "g", "rollout": "r", "failure_mode": "fm",
        "wall_clock_p50_s": wc, "workflow_run_share": 1.0, "evidence": "e",
        "fix_strategy": "s", "fix_recipe_anchor": "a",
    }


def _opt71(job: str, wc: float, title: str, line: int = 0) -> dict:
    return {
        "id": "x", "pattern": "OPT71", "pattern_class": "structural",
        "structural": True, "severity": "HIGH", "title": title, "line": line,
        "workflow_file": ".github/workflows/ci.yml", "affected_jobs": [job],
        "risk": "MEDIUM", "guardrail": "g", "rollout": "r", "failure_mode": "fm",
        "wall_clock_p50_s": wc, "wall_clock_uncapped_p50_s": max(wc, 50.0),
        "workflow_run_share": 1.0, "evidence": "e",
        "fix_strategy": "expensive-non-required-check", "fix_recipe_anchor": "a",
        "effort": "MEDIUM",
    }


def _opt70(job: str, wc: float, line: int = 0) -> dict:
    return {
        "id": "x", "pattern": "OPT70", "pattern_class": "structural",
        "structural": True, "severity": "HIGH", "title": "scope it", "line": line,
        "workflow_file": ".github/workflows/ci.yml", "affected_jobs": [job],
        "risk": "MEDIUM", "guardrail": "g", "rollout": "r", "failure_mode": "fm",
        "wall_clock_p50_s": wc, "wall_clock_uncapped_p50_s": max(wc, 50.0),
        "workflow_run_share": 1.0, "evidence": "e",
        "fix_strategy": "scope-build-test-to-changed", "fix_recipe_anchor": "a",
        "effort": "HIGH",
    }


def _struct_at(pattern, risk, wc, title, wf, jobs, prose=None, fid="f1"):
    f = _struct(pattern, risk, wc, title)
    f["workflow_file"] = wf
    f["affected_jobs"] = jobs
    f["id"] = fid
    if prose is not None:
        f["fix_prose"] = prose
    return f


def test_opt71_title_does_not_presuppose_descope():
    # The OPT71 framing must NOT presuppose de-scoping: the router can't tell an
    # advisory check from a real test/build gate that merely isn't branch-
    # protected, so the title offers the full menu (de-scope / gate / speed up)
    # and the guardrail demands advisory-vs-gate classification first. A rename
    # back to "De-scope ..." would re-introduce the bug this PR fixed.
    out = _scenario(required={"build-and-test"})  # CodeQL + lint are not-required
    opt71 = [f for f in out if f["pattern"] == "OPT71"]
    assert opt71, "expected an OPT71 finding"
    for f in opt71:
        t = f["title"].lower()
        assert not t.startswith("de-scope") and not t.startswith("remove")
        assert "speed up" in t or "gate" in t
        g = f["guardrail"].lower()
        assert "advisory" in g
        assert "never de-scope" in g or "sped up" in g
        # The failure mode must name the real-gate hazard, not a preview/comment.
        assert "verification" in f["failure_mode"].lower()


def test_opt72_vs_opt70_routing_at_ratio_boundary():
    # The redundant-work-ratio threshold is strict `> 2.0`: a build-dominant pole
    # ABOVE 2.0 routes to the safe-cache OPT72; AT/BELOW 2.0 routes to the
    # correctness-risky scope OPT70. Flipping `>` to `>=` would mis-route a HIGH-
    # risk scope into a safe-cache recommendation, so pin the boundary.
    def pole(setup_build, payload):
        # one cluster job: checkout + (build) + (test payload)
        steps = [("Checkout", 10), ("Build", setup_build - 10), ("Run tests", payload)]
        runs = [[_job("build-and-test", steps)] for _ in range(5)]
        crit = cr._critical_path(runs)
        pr_checks = (("build-and-test", float(setup_build + payload)),)
        out = cr._detect_structural_candidates(
            pr_checks, [], {".github/workflows/ci.yml": crit},
            {".github/workflows/ci.yml": runs},
            cr.RequiredChecks(frozenset({"build-and-test"}), complete=True),
            {".github/workflows/ci.yml": {"pull_request"}}, {}, 0)
        return [f for f in out if "build-and-test" in f["title"]][0]["pattern"]

    assert pole(setup_build=250, payload=100) == "OPT72"   # ratio 2.5 > 2.0
    assert pole(setup_build=180, payload=100) == "OPT70"   # ratio 1.8 <= 2.0


def test_rare_presence_constants_match_across_data_layer_and_renderer():
    # The rare-pole thresholds are DUPLICATED in collect_runs (the spine ranking) and
    # blocking_path (the renderer's typical/minority split). They MUST stay identical or the
    # renderer would demote/label a check the data layer kept (contradicting
    # `critical_path_check`). `_MIN_PR` is incidentally pinned by literal `== 6` asserts in
    # both suites; `_FRAC` had no cross-file pin — a drift to 0.6 in one file would ship
    # green. This is the single guard that fails CI on ANY drift of EITHER constant.
    assert bp._RARE_PRESENCE_FRAC == cr._RARE_PRESENCE_FRAC == 0.5
    assert bp._RARE_PRESENCE_MIN_PR == cr._RARE_PRESENCE_MIN_PR == 6


def test_vr_rare_presence_thresholds_stay_coupled_to_data_layer():
    # verify_report hand-copies the rare-presence thresholds as `_VR_*` (it is standalone — no skill
    # imports) and divides by them in `check_headline_lever_is_presence_eligible`'s minority test.
    # The guard above pins the `bp.`/`cr.` copies; this pins the THIRD copy, so the verify guard
    # can't silently drift to a different minority threshold than the engine that demotes (#57
    # review — the `_VR_` copies had no cross-file pin, so a drift to 0.6 would have shipped green).
    vr = _load_vr_module()
    assert vr._VR_RARE_PRESENCE_FRAC == cr._RARE_PRESENCE_FRAC == bp._RARE_PRESENCE_FRAC == 0.5
    assert vr._VR_RARE_PRESENCE_MIN_PR == cr._RARE_PRESENCE_MIN_PR == bp._RARE_PRESENCE_MIN_PR == 6


def test_max_sampled_run_wall_s_drives_conservative_triage():
    # Run-list wall-time from metadata alone (no job fetch): run_started_at|created_at ->
    # updated_at. The triage skips a workflow's job fetch only when its SLOWEST sampled run
    # is under the floor — so it can't hold the merge pole.
    fast = [{"run_started_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:00:40Z"},
            {"created_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:01:00Z"}]
    assert cr._max_sampled_run_wall_s(fast) == 60.0                       # max of 40s, 60s
    assert 0.0 < cr._max_sampled_run_wall_s(fast) < cr._TRIAGE_WALLCLOCK_FLOOR_S   # triaged
    # One long run -> max is long -> NOT triaged (never skip a workflow that ever ran long).
    mixed = fast + [{"created_at": "2026-06-10T00:00:00Z", "updated_at": "2026-06-10T00:25:00Z"}]
    assert cr._max_sampled_run_wall_s(mixed) == 1500.0
    assert cr._max_sampled_run_wall_s(mixed) >= cr._TRIAGE_WALLCLOCK_FLOOR_S
    # Missing timestamps -> 0.0 -> the `0.0 < max` guard keeps unknown-duration workflows in
    # (fetched, not silently skipped).
    assert cr._max_sampled_run_wall_s([{"event": "pull_request"}]) == 0.0
    assert cr._max_sampled_run_wall_s([]) == 0.0


def test_check_presence_counts_pr_membership_for_rare_demotion():
    # Per-check PR presence from per-PR check maps (no gh call): the signal that demotes a
    # rare/opt-in pole below the typical gate. Denominator = PRs that ran >=1 tracked check.
    per_sha = [
        {"Test suite": 1400.0, "lint": 50.0},
        {"Test suite": 1380.0, "lint": 48.0},
        {"Test suite": 1410.0, "Run Benchmark Jobs": 9000.0},   # benchmark ran on 1 PR
        {"lint": 51.0},
        {},                                                     # empty -> not counted in denom
    ]
    present, n_pr = cr._check_presence(per_sha, {"Test suite", "lint", "Run Benchmark Jobs"})
    assert n_pr == 4                                            # the empty map is excluded
    assert present["Test suite"] == 3
    assert present["lint"] == 3
    assert present["Run Benchmark Jobs"] == 1                   # rare: 1/4 <= 0.5 -> demoted
    # A check absent from candidates / all maps -> 0; unknown candidate stays 0.
    present2, _ = cr._check_presence(per_sha, {"never-ran"})
    assert present2["never-ran"] == 0


def test_rank_spine_present_first_demotes_one_path_giant_below_recurring_gate():
    # The headline (critical_path_check == order[0]) must be a check that ACTUALLY gates the
    # merge (is the slowest job) on >= _POLE_RECUR_FLOOR PRs, not a giant that is the pole on a
    # SINGLE PR (a one-path outlier / label-gated benchmark). Bench is the slowest on 1 PR only.
    p50 = {"Bench": 9000.0, "Test": 1400.0, "lint": 200.0}
    per_sha = ([{"Bench": 9000.0, "Test": 1400.0}]                 # Bench is the pole here (1 PR)
               + [{"Test": 1400.0, "lint": 200.0}] * 13            # Test is the pole (13 PRs)
               + [{"lint": 200.0}] * 6)                            # lint the pole (6 PRs)
    order, present, n, pole_freq = cr._rank_spine_present_first(p50, per_sha, frozenset())
    assert n == 20 and pole_freq["Bench"] == 1        # actual pole on only 1 PR -> below floor
    assert order[0][0] == "Test"      # recurring gate headlines, NOT the slower one-path Bench
    assert order[-1][0] == "Bench"    # one-path giant demoted to last


def test_rank_spine_first_surfaces_recurring_heavy_gate_over_never_slowest_check():
    # THE expo/expo class: a lightweight check present on a MAJORITY of PRs but the actual pole
    # (slowest) on ZERO of them must NOT headline over a heavy suite present on a MINORITY that
    # IS the actual pole on several. `Light` runs on every PR but is never slowest; `Heavy` runs
    # on 4 PRs and is the pole on all 4. The old presence>50% rule crowned `Light`; the
    # pole-frequency rule correctly surfaces `Heavy`.
    p50 = {"Light": 175.0, "Heavy": 1400.0}
    per_sha = ([{"Light": 175.0, "Heavy": 1400.0}] * 4            # Heavy is the pole (4 PRs)
               + [{"Light": 175.0}] * 12)                         # Light alone, but never a giant
    order, present, n, pole_freq = cr._rank_spine_present_first(p50, per_sha, frozenset())
    assert n == 16 and present["Light"] == 16 and present["Heavy"] == 4
    assert pole_freq["Heavy"] == 4 and pole_freq["Light"] == 12   # Light IS the pole on light PRs
    # Both clear the floor here (Light gates the 12 light-only PRs), so both are typical and the
    # order is by p50 desc — the heavy recurring gate leads, never the lighter one.
    assert order[0][0] == "Heavy"


def test_rank_spine_first_demotes_never_slowest_majority_check():
    # The precise expo inversion: `Light` present on ALL PRs but NEVER the slowest (a heavier
    # sibling always co-runs), `Heavy` the pole on >= floor PRs. `Light` must be demoted despite
    # its majority presence — presence is not gating; being the actual critical path is.
    p50 = {"Light": 175.0, "Heavy": 1400.0}
    per_sha = [{"Light": 175.0, "Heavy": 1400.0}] * 8            # Heavy always the pole; Light never
    order, present, n, pole_freq = cr._rank_spine_present_first(p50, per_sha, frozenset())
    assert present["Light"] == 8 and pole_freq["Light"] == 0     # present everywhere, pole nowhere
    assert order[0][0] == "Heavy" and order[-1][0] == "Light"    # never-slowest majority demoted


def test_rank_spine_present_first_exempts_required_check():
    # A required check that is the pole on a MINORITY is still never demoted (it gates by
    # definition — branch protection blocks the merge until it passes).
    p50 = {"Bench": 9000.0, "Test": 1400.0}
    per_sha = [{"Bench": 9000.0}] + [{"Test": 1400.0}] * 19
    order, _, _, _ = cr._rank_spine_present_first(p50, per_sha, frozenset({"Bench"}))
    assert order[0][0] == "Bench"     # required + slowest -> stays the headline (exempt)


def test_rank_spine_present_first_inert_on_tiny_sample():
    # Below _RARE_PRESENCE_MIN_PR PRs the frequency is noise -> plain p50 order, no demotion.
    p50 = {"Bench": 9000.0, "Test": 1400.0}
    per_sha = [{"Bench": 9000.0}] + [{"Test": 1400.0}] * 4   # n_pr = 5 < 6
    order, _, n, _ = cr._rank_spine_present_first(p50, per_sha, frozenset())
    assert n == 5
    assert order[0][0] == "Bench"     # NOT demoted on a tiny sample -> raw p50 order


def test_rank_spine_present_first_active_at_min_pr_boundary():
    # The first ACTIVE sample size (n_pr == _RARE_PRESENCE_MIN_PR == 6) MUST demote a one-path
    # giant — pins the `>=` threshold so a flip to `>` (demotion silently off at exactly 6 PRs)
    # fails CI. Bench is the pole on 1 PR (below _POLE_RECUR_FLOOR); Test on the other 5.
    assert cr._RARE_PRESENCE_MIN_PR == 6 and cr._POLE_RECUR_FLOOR == 2
    p50 = {"Bench": 9000.0, "Test": 1400.0}
    per_sha = [{"Bench": 9000.0, "Test": 1400.0}] + [{"Test": 1400.0}] * 5   # n_pr = 6
    order, present, n, pole_freq = cr._rank_spine_present_first(p50, per_sha, frozenset())
    assert n == 6 and pole_freq["Bench"] == 1      # pole on 1/6 PRs < floor -> rare
    assert order[0][0] == "Test" and order[-1][0] == "Bench"   # demoted at the boundary


def test_rank_spine_first_recurring_giant_at_floor_is_typical():
    # The recurrence FLOOR boundary: a heavy check that is the actual pole on EXACTLY
    # _POLE_RECUR_FLOOR (2) PRs is a genuine recurring gate and LEADS — pins that `>=` so a flip
    # to `>` (which would wrongly demote a real 2-PR gate) fails CI. This is the expo case: a
    # heavy suite on a minority of PRs that genuinely gates must surface, not be buried.
    p50 = {"Heavy": 9000.0, "Most": 1400.0}
    per_sha = ([{"Heavy": 9000.0, "Most": 1400.0}] * 2           # Heavy the pole on exactly 2 PRs
               + [{"Most": 1400.0}] * 8)                         # Most the pole on the other 8
    order, present, n, pole_freq = cr._rank_spine_present_first(p50, per_sha, frozenset())
    assert n == 10 and pole_freq["Heavy"] == 2      # exactly at the floor
    assert order[0][0] == "Heavy"    # recurring gate at the floor headlines (>= floor is typical)


def test_should_triage_workflow_boundary_and_unknown():
    base = "2026-06-10T00:00:00Z"
    def wf(updated):
        return [{"created_at": base, "updated_at": updated}]
    assert cr._should_triage_workflow(wf("2026-06-10T00:01:29Z")) is True    # 89s < 90 -> triage
    assert cr._should_triage_workflow(wf("2026-06-10T00:01:30Z")) is False   # exactly 90 -> fetch
    assert cr._should_triage_workflow(wf("2026-06-10T00:01:31Z")) is False   # 91s -> fetch
    assert cr._should_triage_workflow([]) is False                           # no runs -> fetch
    assert cr._should_triage_workflow([{"event": "pull_request"}]) is False  # unknown dur -> fetch
    # Max-gated: a fast run plus one slow run in the window -> NOT triaged.
    assert cr._should_triage_workflow(
        wf("2026-06-10T00:00:30Z") + wf("2026-06-10T00:05:00Z")) is False
    # Mixed known-fast + UNKNOWN-duration run -> NOT triaged. A fast run must not mask an
    # unmeasured run (which could have been long): any unknown duration forces a fetch, so
    # `_max_sampled_run_wall_s` folding unknown->0.0 can never permit a triage here.
    assert cr._should_triage_workflow(
        wf("2026-06-10T00:00:30Z") + [{"event": "pull_request"}]) is False
    assert cr._max_sampled_run_wall_s(
        wf("2026-06-10T00:00:30Z") + [{"event": "pull_request"}]) == 30.0   # folds unknown->0


# ---------------------------------------------------------------------------
# collect() integration — the new emitted fields are actually WIRED UP.
#
# Every other test for the rare-pole / triage features exercises the helpers
# (`_rank_spine_present_first`, `_should_triage_workflow`) or the renderer
# against a hand-built doc. This drives the REAL `collect()` against a canned
# `GhClient` so a wiring break (helper correct, call site wrong) can't ship
# green: that the triage branch appends to `triaged_fast_workflows` and the
# spine emit lands `check_present_n_pr` / `present_on` / `workflow_file` in
# `pr_critical_path` AND demotes the rare giant out of the headline.
# ---------------------------------------------------------------------------

def _iso(secs: int) -> str:
    # Seconds past a fixed base, as an ISO-Z timestamp (the gh API shape).
    base = _dt.datetime(2026, 6, 10, 0, 0, 0, tzinfo=_dt.timezone.utc)
    return (base + _dt.timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _collect_job(name: str, dur_s: int, run_offset: int = 0) -> dict:
    return {"name": name, "labels": ["ubuntu-latest"],
            "started_at": _iso(run_offset), "completed_at": _iso(run_offset + dur_s),
            "steps": [{"name": "run", "started_at": _iso(run_offset),
                       "completed_at": _iso(run_offset + dur_s)}]}


class _FakeCollectClient:
    """Canned GhClient for a 3-workflow repo `o/r`, 8 sampled PRs (s1..s8):
      - test.yml      — `Test suite` ~1400s, ran on all 8 PRs (the typical gate);
      - benchmark.yml — `Run Benchmark Jobs` ~9000s, ran on only s1 (rare/opt-in);
      - lint.yml      — `lint` ~50s on all 8 PRs; every run is under the 90s triage
                        floor, so its per-run job fetch must be SKIPPED.
    Required-status is unreadable (rulesets/protection 404 -> None), the recency-only
    path that exercises rare-pole demotion without a required scope."""

    # Part of the GhClient contract collect() reads: the global rate-limit breaker
    # (False = this client never gave up, so collect() runs to completion) and the
    # counter bump `_run_list` / `_paginate` call when a body comes back MALFORMED.
    gave_up = False

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        self.queries += int(query)
        self.errors += int(error)

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self._shas = [f"s{i}" for i in range(1, 9)]
        # run_id -> jobs (only fetched for the non-triaged workflows).
        self._jobs = {101 + i: [_collect_job("Test suite", 1400)] for i in range(8)}
        self._jobs[201] = [_collect_job("Run Benchmark Jobs", 9000)]
        self._jobs.update({301 + i: [_collect_job("lint", 50)] for i in range(8)})

    def available(self) -> bool:
        return True

    def _runs(self, wf_id: int) -> list[dict]:
        def run(rid, sha, wall):
            # `status`/`conclusion` are on EVERY real run object, and collect now
            # DERIVES the success sample from the all-status page by filtering on them
            # (rather than paying a second `status=success` query). A fake that omits
            # them models a run list GitHub never returns, and would derive an empty
            # sample.
            return {"id": rid, "event": "pull_request", "head_sha": sha,
                    "status": "completed", "conclusion": "success",
                    "created_at": _iso(0), "run_started_at": _iso(0),
                    "updated_at": _iso(wall)}
        if wf_id == 1:    # test.yml — 1410s wall (not triaged)
            return [run(101 + i, s, 1410) for i, s in enumerate(self._shas)]
        if wf_id == 2:    # benchmark.yml — 9010s wall on s1 only
            return [run(201, "s1", 9010)]
        if wf_id == 3:    # lint.yml — 50s wall (under the floor -> triaged)
            return [run(301 + i, s, 50) for i, s in enumerate(self._shas)]
        return []

    def _check_runs(self, sha: str) -> list[dict]:
        def cr_(name, dur):
            return {"name": name, "started_at": _iso(0), "completed_at": _iso(dur)}
        checks = [cr_("Test suite", 1400), cr_("lint", 50)]
        if sha == "s1":
            checks.append(cr_("Run Benchmark Jobs", 9000))
        return checks

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": ".github/workflows/test.yml", "name": "test"},
                {"id": 2, "path": ".github/workflows/benchmark.yml", "name": "benchmark"},
                {"id": 3, "path": ".github/workflows/lint.yml", "name": "lint"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            wf_id, qs = int(m.group(1)), m.group(2)
            if re.search(r"per_page=1(?![0-9])", qs):   # monthly volume ONLY (kept <100 so OPT48 is inert)
                return {"total_count": 30}
            return {"workflow_runs": self._runs(wf_id)}
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m:
            return {"jobs": self._jobs.get(int(m.group(1)), [])}
        m = re.match(r"repos/o/r/commits/([^/]+)/check-runs", endpoint)
        if m:
            return {"check_runs": self._check_runs(m.group(1))}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        # rulesets / branch protection / file contents -> unreadable (allow_missing).
        return None

    def text(self, endpoint: str, allow_missing: bool = False):       # job logs (magnitude sampling) — benign
        self.queries += 1
        return ""


class _FakeCollectClientTriagedJobFailure(_FakeCollectClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m and int(m.group(1)) >= 301:
            self.queries += 1
            self.errors += 1
            return None
        return super().json(endpoint, allow_missing=allow_missing)


class _FakeCollectClientTriagedEmptyJobs(_FakeCollectClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m and int(m.group(1)) >= 301:
            self.queries += 1
            return {"jobs": []}
        return super().json(endpoint, allow_missing=allow_missing)


class _FakeCollectClientCostDeepenFailure(_FakeCollectClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m and int(m.group(1)) >= 304:
            self.queries += 1
            self.errors += 1
            return None
        return super().json(endpoint, allow_missing=allow_missing)


class _FakeCollectClientOpt57OnlyNoRows(_FakeCollectClient):
    def json(self, endpoint: str, allow_missing: bool = False):
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            self.queries += 1
            return {"workflows": [
                {"id": 1, "path": ".github/workflows/test.yml", "name": "test"},
                {"id": 2, "path": ".github/workflows/benchmark.yml", "name": "benchmark"},
                {"id": 3, "path": ".github/workflows/lint.yml", "name": "lint"},
                {"id": 4, "path": ".github/workflows/timeout.yml", "name": "timeout"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m and int(m.group(1)) == 4:
            self.queries += 1
            qs = m.group(2)
            if re.search(r"per_page=1(?![0-9])", qs):   # monthly volume ONLY (not per_page=100)
                return {"total_count": 5}
            if "status=success" in qs:
                return {"workflow_runs": []}
            return {"workflow_runs": []}
        return super().json(endpoint, allow_missing=allow_missing)


class _FakeCollectClientOpt57OnlyWithRows(_FakeCollectClientOpt57OnlyNoRows):
    def __init__(self) -> None:
        super().__init__()
        self._jobs[401] = [_collect_job("timeoutless", 120)]

    def json(self, endpoint: str, allow_missing: bool = False):
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m and int(m.group(1)) == 4:
            self.queries += 1
            qs = m.group(2)
            if re.search(r"per_page=1(?![0-9])", qs):   # monthly volume ONLY (not per_page=100)
                return {"total_count": 5}
            return {"workflow_runs": [{
                "id": 401,
                "event": "pull_request",
                "head_sha": "s1",
                "status": "completed",
                "conclusion": "success",
                "created_at": _iso(0),
                "run_started_at": _iso(0),
                "updated_at": _iso(130),
            }]}
        return super().json(endpoint, allow_missing=allow_missing)


class _FakeCollectClientOpt57OnlyFetchFailure(_FakeCollectClientOpt57OnlyWithRows):
    def json(self, endpoint: str, allow_missing: bool = False):
        if endpoint == "repos/o/r/actions/runs/401/jobs?per_page=100":
            self.queries += 1
            self.errors += 1
            return None
        return super().json(endpoint, allow_missing=allow_missing)


class _FakeCollectClientOpt57OnlyRowsAndFetchFailure(_FakeCollectClientOpt57OnlyWithRows):
    def json(self, endpoint: str, allow_missing: bool = False):
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m and int(m.group(1)) == 4:
            self.queries += 1
            qs = m.group(2)
            if re.search(r"per_page=1(?![0-9])", qs):   # monthly volume ONLY (not per_page=100)
                return {"total_count": 5}
            return {"workflow_runs": [
                {
                    "id": 401,
                    "event": "pull_request",
                    "head_sha": "s1",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": _iso(0),
                    "run_started_at": _iso(0),
                    "updated_at": _iso(130),
                },
                {
                    "id": 402,
                    "event": "pull_request",
                    "head_sha": "s2",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": _iso(30),
                    "run_started_at": _iso(30),
                    "updated_at": _iso(170),
                },
            ]}
        if endpoint == "repos/o/r/actions/runs/402/jobs?per_page=100":
            self.queries += 1
            self.errors += 1
            return None
        return super().json(endpoint, allow_missing=allow_missing)


class _FakeCollectClientOnlyOpt57NoRows(_FakeCollectClientOpt57OnlyNoRows):
    def json(self, endpoint: str, allow_missing: bool = False):
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            self.queries += 1
            return {"workflows": [
                {"id": 4, "path": ".github/workflows/timeout.yml", "name": "timeout"}]}
        return super().json(endpoint, allow_missing=allow_missing)


def _triage_collect_doc(extra_findings: list[dict] | None = None) -> dict:
    findings = [
        {"id": "f1", "workflow_file": ".github/workflows/test.yml"},
        {"id": "f2", "workflow_file": ".github/workflows/benchmark.yml"},
        {"id": "f3", "workflow_file": ".github/workflows/lint.yml"},
        {"id": "f4", "pattern": "OPT19", "workflow_file": "tests/conftest.py"}]
    findings.extend(extra_findings or [])
    return {"findings": findings}


def test_collect_wires_triage_and_presence_fields(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClient)
    doc = _triage_collect_doc()
    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    # Triage wiring: the fast workflow's job fetch was skipped and DISCLOSED.
    ds = out["data_sources"]
    assert ds["triaged_fast_workflows"] == [".github/workflows/lint.yml"]
    assert ds["triaged_fast_count"] == 1
    assert ds["cost_spine_triaged_workflows_included"] == [".github/workflows/lint.yml"]
    assert ds["cost_spine_triaged_workflow_count"] == 1
    assert ds["cost_spine_triaged_runs_sampled"] == 8
    assert ds["cost_spine_triaged_jobs_sampled"] == 8
    assert ds["cost_spine_job_fetch_failures"] == 0
    assert ds["workflows_analyzed"] == 3
    assert ds["gh_error_count"] == 0          # every canned endpoint resolved
    # The triaged stub still carries its run-list wall (`concurrent_wall_p50`) so the
    # cross-workflow floor keeps counting it — long_pole_p50 stays 0 (no job sample to drill).
    lint_crit = out["per_workflow_timing"][".github/workflows/lint.yml"]
    assert lint_crit["long_pole_p50"] == 0.0
    assert lint_crit["concurrent_wall_p50"] == 50.0   # lint.yml ran 50s wall, under the floor

    cp = out["pr_critical_path"]
    # Spine-emit wiring: the new presence fields landed in the doc.
    assert cp["check_present_n_pr"] == 8
    by_name = {c["name"]: c for c in cp["checks"]}
    assert by_name["Test suite"]["present_on"] == 8           # typical gate, every PR
    assert by_name["Run Benchmark Jobs"]["present_on"] == 1   # rare giant, 1/8
    # `workflow_file` is emitted per file-backed check (so the renderer needn't rely on
    # whether the check was drilled) — the benchmark is file-backed even though demoted.
    assert by_name["Test suite"]["workflow_file"] == ".github/workflows/test.yml"
    assert by_name["Run Benchmark Jobs"]["workflow_file"] == ".github/workflows/benchmark.yml"

    # Rare-pole demotion end to end: the headline is the typical gate, NOT the 9000s giant.
    assert cp["critical_path_check"] == "Test suite"
    spine = out["runner_minute_spine"]
    assert spine["complete_repo_coverage"] is True
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == []
    assert spine["workflow_coverage"]["triaged_workflows_included"] == [
        ".github/workflows/lint.yml"]
    assert any(row["workflow_file"] == ".github/workflows/lint.yml"
               and row["job_name"] == "lint"
               for row in spine["rows"])


def test_collect_deepens_bill_pole_workflows_for_cost_spine(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClient)

    out = cr.collect(_triage_collect_doc(), "o/r", max_runs=8, shallow_runs=3)

    ds = out["data_sources"]
    assert ds["triaged_fast_workflows"] == [".github/workflows/lint.yml"]
    assert ".github/workflows/lint.yml" in ds["cost_deepen_candidate_workflows"]
    assert ds["cost_deepened_workflows"] == [".github/workflows/lint.yml"]
    assert ds["cost_deepened_workflow_count"] == 1
    assert ds["cost_deepen_runs_sampled"] == 5
    assert ds["cost_deepen_jobs_sampled"] == 5
    assert ".github/workflows/lint.yml" not in ds["full_depth_workflows"]
    assert ".github/workflows/test.yml" in ds["full_depth_workflows"]
    assert ".github/workflows/lint.yml" in ds["cost_spine_full_depth_workflows"]
    assert ds["cost_spine_shallow_workflows"] == []
    assert ds["shallow_remaining_workflows"] == []
    assert out["pr_critical_path"]["critical_path_check"] == "Test suite"
    assert out["per_workflow_timing"][".github/workflows/lint.yml"]["long_pole_p50"] == 0.0

    lint_rows = [
        row for row in out["runner_minute_spine"]["rows"]
        if row["workflow_file"] == ".github/workflows/lint.yml"
    ]
    assert lint_rows
    assert {row["sampled_workflow_run_count"] for row in lint_rows} == {8}


def test_collect_cost_deepen_failure_rebuilds_spine_fail_closed(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClientCostDeepenFailure)

    out = cr.collect(_triage_collect_doc(), "o/r", max_runs=8, shallow_runs=3)

    ds = out["data_sources"]
    assert ".github/workflows/lint.yml" in ds["cost_deepen_candidate_workflows"]
    assert ds["cost_deepened_workflows"] == []
    assert ds["cost_deepen_runs_sampled"] == 0
    assert ds["cost_spine_job_fetch_failures"] == 5
    assert ds["gh_error_count"] == 5
    assert ds["partial_reason"] == "5 gh API call(s) failed during collection"
    assert ".github/workflows/lint.yml" in ds["cost_spine_shallow_workflows"]

    spine = out["runner_minute_spine"]
    assert spine["complete_repo_coverage"] is False
    assert spine["render_ready"] is False
    assert spine["workflow_coverage"]["job_fetch_failures"] == 5


def test_cost_spine_triaged_fetch_failure_keeps_coverage_open(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClientTriagedJobFailure)

    out = cr.collect(_triage_collect_doc(), "o/r", max_runs=20, shallow_runs=10)

    ds = out["data_sources"]
    assert ds["triaged_fast_workflows"] == [".github/workflows/lint.yml"]
    assert ds["cost_spine_triaged_workflows_included"] == []
    assert ds["cost_spine_triaged_workflow_count"] == 0
    assert ds["cost_spine_triaged_runs_sampled"] == 0
    assert ds["cost_spine_triaged_jobs_sampled"] == 0
    assert ds["cost_spine_job_fetch_failures"] == 8
    lint_crit = out["per_workflow_timing"][".github/workflows/lint.yml"]
    assert lint_crit["long_pole_p50"] == 0.0
    assert lint_crit["concurrent_wall_p50"] == 50.0
    spine = out["runner_minute_spine"]
    assert spine["complete_repo_coverage"] is False
    assert spine["workflow_coverage"]["omitted_workflows"] == [
        ".github/workflows/lint.yml"]
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == []
    assert spine["workflow_coverage"]["job_fetch_failures"] == 8


def test_cost_spine_triaged_empty_jobs_keep_coverage_open(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClientTriagedEmptyJobs)

    out = cr.collect(_triage_collect_doc(), "o/r", max_runs=20, shallow_runs=10)

    ds = out["data_sources"]
    assert ds["triaged_fast_workflows"] == [".github/workflows/lint.yml"]
    assert ds["cost_spine_triaged_workflows_included"] == []
    assert ds["cost_spine_triaged_workflow_count"] == 0
    assert ds["cost_spine_triaged_runs_sampled"] == 0
    assert ds["cost_spine_triaged_jobs_sampled"] == 0
    assert ds["cost_spine_job_fetch_failures"] == 0
    lint_crit = out["per_workflow_timing"][".github/workflows/lint.yml"]
    assert lint_crit["long_pole_p50"] == 0.0
    assert lint_crit["concurrent_wall_p50"] == 50.0
    spine = out["runner_minute_spine"]
    assert spine["complete_repo_coverage"] is False
    assert spine["workflow_coverage"]["omitted_workflows"] == [
        ".github/workflows/lint.yml"]
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == []
    assert spine["workflow_coverage"]["job_fetch_failures"] == 0


def test_opt57_only_workflow_without_rows_does_not_block_cost_spine(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClientOpt57OnlyNoRows)
    doc = _triage_collect_doc()
    doc["workflow_job_graph"] = {
        ".github/workflows/timeout.yml": {
            "timeoutless": {
                "name": "timeoutless",
                "needs": [],
                "reusable": False,
                "matrix": False,
                "timeout_minutes": False,
            }
        }
    }

    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    assert out["data_sources"]["workflows_analyzed"] == 4
    assert not any(f.get("pattern") == "OPT57" for f in out["findings"])
    assert ".github/workflows/timeout.yml" not in out["per_workflow_monthly_volume"]
    spine = out["runner_minute_spine"]
    assert spine["complete_repo_coverage"] is True
    assert spine["workflow_coverage"]["omitted_workflows"] == []
    assert ".github/workflows/timeout.yml" not in {
        row["workflow_file"] for row in spine["rows"]}


def test_only_opt57_no_row_workflow_leaves_no_spine_or_volume(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClientOnlyOpt57NoRows)
    doc = {
        "findings": [],
        "workflow_job_graph": {
            ".github/workflows/timeout.yml": {
                "timeoutless": {
                    "name": "timeoutless",
                    "needs": [],
                    "reusable": False,
                    "matrix": False,
                    "timeout_minutes": False,
                }
            }
        },
    }

    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    assert out["data_sources"]["workflows_analyzed"] == 1
    assert out["per_workflow_monthly_volume"] == {}
    assert "runner_minute_spine" not in out


def test_opt57_only_workflow_with_rows_stays_in_cost_spine(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClientOpt57OnlyWithRows)
    doc = _triage_collect_doc()
    doc["workflow_job_graph"] = {
        ".github/workflows/timeout.yml": {
            "timeoutless": {
                "name": "timeoutless",
                "needs": [],
                "reusable": False,
                "matrix": False,
                "timeout_minutes": False,
            }
        }
    }

    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    assert out["data_sources"]["workflows_analyzed"] == 4
    assert ".github/workflows/timeout.yml" in out["per_workflow_monthly_volume"]
    spine = out["runner_minute_spine"]
    assert spine["complete_repo_coverage"] is True
    assert spine["workflow_coverage"]["workflow_count"] == 4
    assert spine["workflow_coverage"]["row_workflow_count"] == 4
    assert spine["workflow_coverage"]["omitted_workflows"] == []
    timeout_rows = [
        row for row in spine["rows"]
        if row["workflow_file"] == ".github/workflows/timeout.yml"
    ]
    assert {row["job_name"] for row in timeout_rows} == {"timeoutless"}


def test_opt57_only_fetch_failure_is_not_accounted_as_cost_spine(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClientOpt57OnlyFetchFailure)
    doc = _triage_collect_doc()
    doc["workflow_job_graph"] = {
        ".github/workflows/timeout.yml": {
            "timeoutless": {
                "name": "timeoutless",
                "needs": [],
                "reusable": False,
                "matrix": False,
                "timeout_minutes": False,
            }
        }
    }

    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    ds = out["data_sources"]
    assert ds["gh_error_count"] == 1
    assert ds["cost_spine_job_fetch_failures"] == 0
    assert ".github/workflows/timeout.yml" not in out["per_workflow_monthly_volume"]
    spine = out["runner_minute_spine"]
    assert spine["complete_repo_coverage"] is True
    assert spine["workflow_coverage"]["job_fetch_failures"] == 0


def test_opt57_only_rows_with_fetch_failure_stays_in_denominator_and_fails_closed(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClientOpt57OnlyRowsAndFetchFailure)
    doc = _triage_collect_doc()
    doc["workflow_job_graph"] = {
        ".github/workflows/timeout.yml": {
            "timeoutless": {
                "name": "timeoutless",
                "needs": [],
                "reusable": False,
                "matrix": False,
                "timeout_minutes": False,
            }
        }
    }

    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    ds = out["data_sources"]
    assert ds["gh_error_count"] == 1
    assert ds["cost_spine_job_fetch_failures"] == 1
    assert ".github/workflows/timeout.yml" in out["per_workflow_monthly_volume"]
    spine = out["runner_minute_spine"]
    assert spine["complete_repo_coverage"] is False
    assert spine["render_ready"] is False
    assert spine["workflow_coverage"]["omitted_workflows"] == []
    assert spine["workflow_coverage"]["job_fetch_failures"] == 1
    assert [
        row["job_name"] for row in spine["rows"]
        if row["workflow_file"] == ".github/workflows/timeout.yml"
    ] == ["timeoutless"]


def test_collect_keeps_workflow_file_missing_from_api_as_unknown_volume(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeCollectClient)
    doc = _triage_collect_doc([
        {"id": "f5", "workflow_file": ".github/workflows/missing.yml"},
    ])

    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    assert out["data_sources"]["workflows_analyzed"] == 4
    spine = out["runner_minute_spine"]
    assert spine["complete_repo_coverage"] is False
    assert spine["workflow_coverage"]["unknown_volume_workflows"] == [
        ".github/workflows/missing.yml"]
    assert "tests/conftest.py" not in spine["workflow_coverage"]["unknown_volume_workflows"]


class _FakeDeclaredPrButPushSampleClient:
    """Canned GhClient for the Playwright-shaped event-scope regression:

    `.github/workflows/secondary.yml` declares `pull_request`, so its
    `Windows (firefox)` check is a real PR check-run, but the workflow-run sample
    available to `collect_runs` caught only push runs for that workflow. The PR
    spine must use the sampled PR check-run duration (300s), not the push job's
    900s workflow-job timing or step decomposition.
    """
    # Part of the GhClient contract collect() reads: the global rate-limit
    # breaker (False = never gave up) and the counter bump `_run_list` /
    # `_paginate` call when a body comes back MALFORMED.
    gave_up = False

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        self.queries += int(query)
        self.errors += int(error)


    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self._shas = [f"s{i}" for i in range(1, 9)]
        self._jobs = {101 + i: [_collect_job("Windows (firefox)", 900)]
                      for i in range(8)}
        self._jobs.update({201 + i: [_collect_job("lint", 100)]
                           for i in range(8)})

    def available(self) -> bool:
        return True

    def _runs(self, wf_id: int) -> list[dict]:
        def run(rid, sha, wall, event):
            # See _FakeCollectClient._runs: the derived success sample filters on
            # `status`/`conclusion`, which every real run object carries.
            return {"id": rid, "event": event, "head_sha": sha,
                    "status": "completed", "conclusion": "success",
                    "created_at": _iso(0), "run_started_at": _iso(0),
                    "updated_at": _iso(wall)}
        if wf_id == 1:
            return [run(101 + i, s, 910, "push") for i, s in enumerate(self._shas)]
        if wf_id == 2:
            return [run(201 + i, s, 105, "pull_request")
                    for i, s in enumerate(self._shas)]
        return []

    def _check_runs(self, sha: str) -> list[dict]:
        def cr_(name, dur):
            return {"name": name, "started_at": _iso(0), "completed_at": _iso(dur)}
        return [cr_("Windows (firefox)", 300), cr_("lint", 100)]

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": ".github/workflows/secondary.yml",
                 "name": "secondary"},
                {"id": 2, "path": ".github/workflows/lint.yml", "name": "lint"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            wf_id, qs = int(m.group(1)), m.group(2)
            if re.search(r"per_page=1(?![0-9])", qs):   # monthly volume ONLY (not per_page=100)
                return {"total_count": 30}
            return {"workflow_runs": self._runs(wf_id)}
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m:
            return {"jobs": self._jobs.get(int(m.group(1)), [])}
        m = re.match(r"repos/o/r/commits/([^/]+)/check-runs", endpoint)
        if m:
            return {"check_runs": self._check_runs(m.group(1))}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint.endswith("contents/.github/workflows/secondary.yml"):
            body = "on: [push, pull_request]\njobs:\n  win:\n    name: Windows (firefox)\n"
            return {"content": base64.b64encode(body.encode()).decode()}
        if endpoint.endswith("contents/.github/workflows/lint.yml"):
            body = "on: pull_request\njobs:\n  lint:\n    name: lint\n"
            return {"content": base64.b64encode(body.encode()).decode()}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


def test_collect_uses_pr_check_run_timing_when_declared_pr_workflow_sample_is_push_only(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeDeclaredPrButPushSampleClient)
    doc = {
        "workflow_job_graph": {
            ".github/workflows/secondary.yml": {
                "win": {"name": "Windows (firefox)", "needs": []}},
            ".github/workflows/lint.yml": {
                "lint": {"name": "lint", "needs": []}},
        },
        "findings": [
            {"id": "f1", "workflow_file": ".github/workflows/secondary.yml"},
            {"id": "f2", "workflow_file": ".github/workflows/lint.yml"},
        ],
    }
    out = cr.collect(doc, "o/r", max_runs=8, shallow_runs=8)

    pwt = out["per_workflow_timing"]
    assert pwt[".github/workflows/secondary.yml"]["event_scope"] == "all-events"

    cp = out["pr_critical_path"]
    assert cp["critical_path_check"] == "Windows (firefox)"
    by_name = {c["name"]: c for c in cp["checks"]}
    assert by_name["Windows (firefox)"]["p50_s"] == 300.0  # PR check-run, not push job 900s
    assert by_name["Windows (firefox)"]["workflow_file"] == ".github/workflows/secondary.yml"
    assert by_name["Windows (firefox)"]["timing_source"] == "pr_check_runs"

    assert cp.get("gate_kind") != "pr_floor_fallback"
    pole = cp["poles"][0]
    assert pole["check"] == "Windows (firefox)"
    assert pole["p50_s"] == 300.0
    assert pole["timing_source"] == "pr_check_runs"
    assert pole["workflow_file"] == ".github/workflows/secondary.yml"
    assert pole["job"] == "win"
    assert "job_timing_unavailable" in pole
    assert "steps" not in pole
    assert "dominant_step" not in pole

    md = bp.render(out, {}, {}, {}, "2026-07-06")
    assert "Long pole 1" in md
    assert "Windows (firefox)" in md
    assert "workflow-job drill withheld" in md
    assert "No per-step breakdown was captured" not in md
    assert not any(
        f.get("workflow_file") == ".github/workflows/secondary.yml"
        and f.get("pattern") in {"OPT70", "OPT71", "OPT72", "OPT75"}
        for f in out.get("findings", [])
    ), "no structural fix should be inferred from all-events push job timing"


# ---------------------------------------------------------------------------
# Relative triage RECOVERY — the absolute 90s floor is only a coarse pre-filter.
#
# On a seconds-scale repo the measured gate can itself sit at/under 90s, so a
# workflow under the floor is NOT automatically too-fast-to-matter: its check can
# be the second-ranked concurrent pole AND the binding wall-clock floor the headline
# buys the gate down to. The absolute floor alone would triage it away (no job fetch),
# dismiss it as "can't hold the merge pole", and drill a LOWER check instead while
# silently using its check as the headline's floor. `collect()` must RECOVER it:
# once the shallow pass measures the gate, any triaged workflow whose run-list wall
# reaches `_TRIAGE_RECOVER_GATE_FRAC` of the gate is job-fetched + drilled like a pole.
# (Repro: roboflow/supervision — 85s gate, ci-build-docs `Test docs build` 59.5s.)
# ---------------------------------------------------------------------------

class _FakeSecondsScaleClient:
    """Canned GhClient for a seconds-scale repo `o/r`, 8 PRs (s1..s8):
      - gate.yml  — `build (windows)` ~85s, 100s wall  → NOT triaged (the gate);
      - docs.yml  — `Test docs build` ~60s,  85s wall  → triaged by the absolute 90s
                    floor, but 85s wall >= 0.5 * 85s gate → must be RECOVERED + drilled;
      - tiny.yml  — `lint` ~20s, 30s wall → triaged AND 30s < 42.5s floor → STAYS triaged.
    Required-status unreadable (404 -> None): the recency-only path."""
    # Part of the GhClient contract collect() reads: the global rate-limit
    # breaker (False = never gave up) and the counter bump `_run_list` /
    # `_paginate` call when a body comes back MALFORMED.
    gave_up = False

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        self.queries += int(query)
        self.errors += int(error)


    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self._shas = [f"s{i}" for i in range(1, 9)]
        self._jobs = {101 + i: [_collect_job("build (windows)", 85)] for i in range(8)}
        self._jobs.update({201 + i: [_collect_job("Test docs build", 60)] for i in range(8)})
        self._jobs.update({301 + i: [_collect_job("lint", 20)] for i in range(8)})

    def available(self) -> bool:
        return True

    def _runs(self, wf_id: int) -> list[dict]:
        def run(rid, sha, wall):
            # `status`/`conclusion` are on EVERY real run object, and collect now
            # DERIVES the success sample from the all-status page by filtering on them
            # (rather than paying a second `status=success` query). A fake that omits
            # them models a run list GitHub never returns, and would derive an empty
            # sample.
            return {"id": rid, "event": "pull_request", "head_sha": sha,
                    "status": "completed", "conclusion": "success",
                    "created_at": _iso(0), "run_started_at": _iso(0),
                    "updated_at": _iso(wall)}
        if wf_id == 1:    # gate.yml — 100s wall (>= 90 floor -> fetched)
            return [run(101 + i, s, 100) for i, s in enumerate(self._shas)]
        if wf_id == 2:    # docs.yml — 85s wall (< 90 floor -> triaged, then recovered)
            return [run(201 + i, s, 85) for i, s in enumerate(self._shas)]
        if wf_id == 3:    # tiny.yml — 30s wall (< 90 floor AND < recover band -> stays triaged)
            return [run(301 + i, s, 30) for i, s in enumerate(self._shas)]
        return []

    def _check_runs(self, sha: str) -> list[dict]:
        def cr_(name, dur):
            return {"name": name, "started_at": _iso(0), "completed_at": _iso(dur)}
        return [cr_("build (windows)", 85), cr_("Test docs build", 60), cr_("lint", 20)]

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": ".github/workflows/gate.yml", "name": "gate"},
                {"id": 2, "path": ".github/workflows/docs.yml", "name": "docs"},
                {"id": 3, "path": ".github/workflows/tiny.yml", "name": "tiny"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            wf_id, qs = int(m.group(1)), m.group(2)
            if re.search(r"per_page=1(?![0-9])", qs):   # monthly volume ONLY (not per_page=100)
                return {"total_count": 30}
            return {"workflow_runs": self._runs(wf_id)}
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m:
            return {"jobs": self._jobs.get(int(m.group(1)), [])}
        m = re.match(r"repos/o/r/commits/([^/]+)/check-runs", endpoint)
        if m:
            return {"check_runs": self._check_runs(m.group(1))}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


class _FakeRecoveryFetchFailsClient(_FakeSecondsScaleClient):
    """Same seconds-scale repo, but docs.yml's recovery job-fetch returns NO jobs (total
    fetch failure, e.g. every run errors under rate-limit pressure). docs.yml still clears the
    recover band on its 85s run-list wall, so the recovery loop attempts the fetch — and must
    NOT false-recover it: keep it triaged with its stub wall-clock floor intact rather than
    overwrite the stub with empty job data (which would drop the floor and overstate savings)."""

    def __init__(self) -> None:
        super().__init__()
        for rid in range(201, 209):   # docs.yml runs → empty jobs
            self._jobs[rid] = []


def test_collect_keeps_triaged_when_recovery_fetch_yields_no_jobs(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeRecoveryFetchFailsClient)
    doc = {"findings": [
        {"id": "f1", "workflow_file": ".github/workflows/gate.yml"},
        {"id": "f2", "workflow_file": ".github/workflows/docs.yml"},
        {"id": "f3", "workflow_file": ".github/workflows/tiny.yml"}]}
    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)
    ds = out["data_sources"]
    pwt = out["per_workflow_timing"]
    # The recovery fetch produced no job timing, so docs.yml must NOT be reported recovered...
    assert ds["recovered_fast_count"] == 0
    assert ".github/workflows/docs.yml" not in ds["recovered_fast_workflows"]
    # ...it stays triaged, with its stub preserved (long_pole_p50 still the 0.0 stub value, so
    # its concurrent_wall_p50 keeps holding the cross-workflow floor — not dropped to empty).
    assert ".github/workflows/docs.yml" in ds["triaged_fast_workflows"]
    assert pwt[".github/workflows/docs.yml"]["long_pole_p50"] == 0.0
    # The gate is unaffected.
    assert pwt[".github/workflows/gate.yml"]["long_pole_p50"] == 85.0


def test_collect_recovers_near_gate_triaged_workflow(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _FakeSecondsScaleClient)
    doc = {"findings": [
        {"id": "f1", "workflow_file": ".github/workflows/gate.yml"},
        {"id": "f2", "workflow_file": ".github/workflows/docs.yml"},
        {"id": "f3", "workflow_file": ".github/workflows/tiny.yml"}]}
    out = cr.collect(doc, "o/r", max_runs=20, shallow_runs=10)

    ds = out["data_sources"]
    pwt = out["per_workflow_timing"]
    # docs.yml fell under the absolute 90s floor but its 85s wall reaches half the 85s
    # gate, so it must be RECOVERED — job-fetched and drilled like any pole, NOT left as a
    # triaged stub the report dismisses while using its check as the headline's binding floor.
    assert ds["recovered_fast_workflows"] == [".github/workflows/docs.yml"]
    assert ds["recovered_fast_count"] == 1
    assert ".github/workflows/docs.yml" not in ds["triaged_fast_workflows"]
    assert pwt[".github/workflows/docs.yml"]["long_pole_p50"] == 60.0   # drilled now
    # tiny.yml is genuinely too fast (30s wall < 42.5s recover band) — stays triaged, no fetch.
    assert ds["triaged_fast_workflows"] == [".github/workflows/tiny.yml"]
    assert pwt[".github/workflows/tiny.yml"]["long_pole_p50"] == 0.0
    # The gate itself is never triaged (100s wall >= 90s floor).
    assert pwt[".github/workflows/gate.yml"]["long_pole_p50"] == 85.0
    assert ds["gh_error_count"] == 0


# ---------------------------------------------------------------------------
# Headline-crown recovery — the crown (`critical_path_check` = slowest TYPICAL check) can
# fall on a TRIAGED workflow whose jobs were never fetched (paradedb/paradedb class): the
# headline pole then dead-ends with "no captured log". `collect()` job-fetches that one
# workflow so the headline is drillable — but ONLY un-triages it once the crown check
# actually refreshes to `workflow_jobs`. If the fetch yields job timing with no
# developer-scoped job (e.g. the recovered runs all fired on push), the crown STAYS triaged
# (loud dead-end for `verify_report._crown_triaged_offender`) rather than being falsely
# marked recovered while remaining undrillable.
# ---------------------------------------------------------------------------

class _FakeCrownTriagedClient:
    """Canned GhClient for `o/r`, 8 PRs (s1..s8) where the HEADLINE crown lands on a TRIAGED
    workflow:
      - gate.yml — `build` job ~200s, 210s wall → NOT triaged (the gate); provides the sampled
                   PR SHAs. Its PR check-run maps to the job (timing_source=workflow_jobs).
      - lint.yml — `lint` job ~25s, 30s wall → TRIAGED (< 90s floor) AND 30s < 100s recover band
                   (0.5 * 200s gate), so the relative-recovery pass leaves it triaged. Its PR
                   check-run reads 300s (queue-inflated), so it CROWNS the spine over the 200s
                   gate — the headline pole with no sampled job to drill.
    Required-status unreadable (recency-only path). Only the headline-crown recovery pass can
    make the crown drillable; without it the report headlines an undrillable triaged lint."""
    # Part of the GhClient contract collect() reads: the global rate-limit
    # breaker (False = never gave up) and the counter bump `_run_list` /
    # `_paginate` call when a body comes back MALFORMED.
    gave_up = False

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        self.queries += int(query)
        self.errors += int(error)


    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self._shas = [f"s{i}" for i in range(1, 9)]
        self._jobs = {101 + i: [_collect_job("build", 200)] for i in range(8)}
        self._jobs.update({201 + i: [_collect_job("lint", 25)] for i in range(8)})
        self._lint_event = "pull_request"

    def available(self) -> bool:
        return True

    def _runs(self, wf_id: int) -> list[dict]:
        def run(rid, sha, wall, event):
            # See _FakeCollectClient._runs: the derived success sample filters on
            # `status`/`conclusion`, which every real run object carries.
            return {"id": rid, "event": event, "head_sha": sha,
                    "status": "completed", "conclusion": "success",
                    "created_at": _iso(0), "run_started_at": _iso(0),
                    "updated_at": _iso(wall)}
        if wf_id == 1:    # gate.yml — 210s wall (>= 90 floor -> fetched, the gate)
            return [run(101 + i, s, 210, "pull_request") for i, s in enumerate(self._shas)]
        if wf_id == 2:    # lint.yml — 30s wall (< 90 floor -> triaged; crown via inflated check-run)
            return [run(201 + i, s, 30, self._lint_event) for i, s in enumerate(self._shas)]
        return []

    def _check_runs(self, sha: str) -> list[dict]:
        def cr_(name, dur):
            return {"name": name, "started_at": _iso(0), "completed_at": _iso(dur)}
        # lint check-run 300s (queue-inflated) > gate job 200s -> lint is the crowned pole.
        return [cr_("build", 200), cr_("lint", 300)]

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": ".github/workflows/gate.yml", "name": "gate"},
                {"id": 2, "path": ".github/workflows/lint.yml", "name": "lint"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            wf_id, qs = int(m.group(1)), m.group(2)
            if re.search(r"per_page=1(?![0-9])", qs):   # monthly volume ONLY (not per_page=100)
                return {"total_count": 30}
            return {"workflow_runs": self._runs(wf_id)}
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m:
            return {"jobs": self._jobs.get(int(m.group(1)), [])}
        m = re.match(r"repos/o/r/commits/([^/]+)/check-runs", endpoint)
        if m:
            return {"check_runs": self._check_runs(m.group(1))}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


class _FakeCrownTriagedPushOnlyClient(_FakeCrownTriagedClient):
    """Same crown-on-triaged repo, but lint.yml's runs all fire on `push` (not pull_request).
    The recovery job-fetch then yields job timing but NO developer-scoped timing (`_crit_for`
    scopes it to `all-events`), so the crown check can't refresh to `workflow_jobs`. It must
    STAY triaged — a flagged dead-end — rather than be falsely marked recovered while remaining
    undrillable (the exact pre-Finding-1 bug: un-triage on `long_pole_p50 > 0` alone)."""

    def __init__(self) -> None:
        super().__init__()
        self._lint_event = "push"


_CROWN_DOC = {
    "workflow_job_graph": {
        # `timeout_minutes` set so neither job seeds OPT57 (which would bypass triage — the
        # whole point of the fixture is that lint.yml IS triaged and needs crown recovery).
        ".github/workflows/gate.yml": {
            "build": {"name": "build", "needs": [], "timeout_minutes": 30}},
        ".github/workflows/lint.yml": {
            "lint": {"name": "lint", "needs": [], "timeout_minutes": 5}},
    },
    "findings": [
        {"id": "f1", "workflow_file": ".github/workflows/gate.yml"},
        {"id": "f2", "workflow_file": ".github/workflows/lint.yml"},
    ],
}


def test_collect_recovers_headline_crown_workflow(monkeypatch):
    # Happy path: the crown (lint) maps to a triaged workflow with retained PR runs, and the
    # recovery fetch yields developer-scoped job timing → the crown refreshes to workflow_jobs,
    # becomes drillable, and drops out of the triaged set into recovered_fast_workflows.
    monkeypatch.setattr(cr, "GhClient", _FakeCrownTriagedClient)
    out = cr.collect(dict(_CROWN_DOC), "o/r", max_runs=20, shallow_runs=10)
    ds = out["data_sources"]
    cp = out["pr_critical_path"]
    pwt = out["per_workflow_timing"]

    assert cp["critical_path_check"] == "lint"           # the crown is still the headline pole
    by_name = {c["name"]: c for c in cp["checks"]}
    assert by_name["lint"].get("timing_source") == "workflow_jobs"   # now DRILLABLE
    assert ds["recovered_fast_workflows"] == [".github/workflows/lint.yml"]
    assert ds["recovered_fast_count"] == 1
    assert ".github/workflows/lint.yml" not in ds["triaged_fast_workflows"]
    assert pwt[".github/workflows/lint.yml"]["long_pole_p50"] == 25.0   # real job timing
    # The recovery is single-shot: it must NOT be counted as an adaptive-cap hit (Finding 2).
    assert ".github/workflows/lint.yml" not in ds["shallow_remaining_workflows"]
    assert ds["gh_error_count"] == 0


def test_collect_keeps_crown_triaged_when_recovery_lacks_developer_timing(monkeypatch):
    # Corrected behavior (Finding 1): the recovery fetch returns job timing but only on `push`
    # runs, so the crown check has no developer-scoped job to refresh to. The crown must STAY
    # triaged (so the loud invariant fires) and NOT be falsely recovered / left undrillable.
    monkeypatch.setattr(cr, "GhClient", _FakeCrownTriagedPushOnlyClient)
    out = cr.collect(dict(_CROWN_DOC), "o/r", max_runs=20, shallow_runs=10)
    ds = out["data_sources"]
    cp = out["pr_critical_path"]
    pwt = out["per_workflow_timing"]

    assert cp["critical_path_check"] == "lint"           # still the headline crown
    by_name = {c["name"]: c for c in cp["checks"]}
    # The crown did NOT refresh — its spine timing stays pr_check_runs (an undrillable headline).
    assert by_name["lint"].get("timing_source") == "pr_check_runs"
    # ...so it is NOT reported recovered, and STAYS triaged (the flagged dead-end the invariant
    # keys on). This assertion FAILS against the pre-Finding-1 code, which un-triaged the crown
    # and appended it to recovered_fast_workflows on `long_pole_p50 > 0` alone.
    assert ds["recovered_fast_count"] == 0
    assert ".github/workflows/lint.yml" not in ds["recovered_fast_workflows"]
    assert ".github/workflows/lint.yml" in ds["triaged_fast_workflows"]
    # Stub preserved (long_pole_p50 still 0.0), so its concurrent_wall_p50 keeps holding the floor.
    assert pwt[".github/workflows/lint.yml"]["long_pole_p50"] == 0.0


def test_dominant_category_lead_agrees_with_decompose_crown():
    # The single longest step (Build, 60) is NOT the dominant lever — the `test` phase
    # (3×40=120) is. `_dominant_category_lead` and `_decompose_job_steps` must crown the
    # SAME category, so the cross-run check / agent prompt can't disagree with the
    # structural decomposition (the dominant_step-disagreement class).
    steps = [("Build", 60), ("Run test a", 40), ("Run test b", 40), ("Run test c", 40)]
    lead = cr._dominant_category_lead([(n, float(d)) for n, d in steps])
    assert lead is not None
    assert cr._step_category(lead[0]) == "test"      # dominant category, NOT build (the lone max)
    assert lead[1] == 40.0                            # the slowest step in that category
    d = cr._decompose_job_steps([_job("svc", steps)])
    assert d["dominant_category"] == "test" == cr._step_category(lead[0])   # they agree


def test_dominant_step_sample_validates_dominant_category_lead_not_global_max():
    # The cross-run magnitude check must validate the dominant-category LEAD (a `test`
    # step), not the lone bigger `Build` step — matching `_decompose_job_steps`.
    steps = [("Build", 60), ("Run test a", 40), ("Run test b", 40), ("Run test c", 40)]
    timeline = {"job_dur_s": 180.0,
                "steps": [{"name": n, "dur_s": float(d)} for n, d in steps]}
    repr_job = _job("svc", steps); repr_job["id"] = 1
    j2 = _job("svc", steps); j2["id"] = 2
    mag = cr._dominant_step_sample(timeline, [(0.0, repr_job), (0.0, j2)], repr_job)
    assert mag is not None
    assert "Run test a" in mag["label"]      # dominant-category lead
    assert "Build" not in mag["label"]        # NOT the lone global-max step


def test_dominant_step_sample_this_run_is_max_occurrence_for_duplicate_names():
    # A step name can recur with two non-zero legs (a retried step). `this_run` must be the
    # SLOWEST occurrence (matching step_dur's per-run resolution), not the first textual
    # match — else the drilled value contradicts the per-run values and reads "stable".
    timeline = {"job_dur_s": 120.0, "steps": [
        {"name": "Run tests", "dur_s": 10.0},   # first (smaller) leg
        {"name": "Run tests", "dur_s": 80.0},   # the real (slower) leg — the lead
        {"name": "Build", "dur_s": 30.0}]}
    repr_job = _job("svc", [("Run tests", 10), ("Run tests", 80), ("Build", 30)])
    repr_job["id"] = 1
    j2 = _job("svc", [("Run tests", 10), ("Run tests", 80), ("Build", 30)]); j2["id"] = 2
    mag = cr._dominant_step_sample(timeline, [(0.0, repr_job), (0.0, j2)], repr_job)
    assert mag is not None
    assert "Run tests" in mag["label"]
    assert mag["this_run"] == 80.0   # the slowest occurrence, NOT the first (10.0)


def test_pole_provenance_downgrades_when_pole_not_required_reachable():
    # Gap A: a narrowed spine can still keep a file-backed-but-unpinnable check that isn't
    # merge-blocking. If THAT is the headlined pole, `required_scoped` would be the langfuse
    # class narrowed not closed — so an unconfirmed pole falls through to `unresolved`.
    p = cr._pole_provenance
    assert p(None, True, True) == "required_scoped"            # spine scoped + pole reachable
    assert p(None, True, False) == "unresolved"                # spine scoped but pole NOT reachable -> HALT
    assert p("pr_floor_fallback", True, False) == "pr_floor_fallback"  # fallback wins
    assert p(None, False, True) == "unresolved"
    assert p(None, True) == "required_scoped"                  # default reachable=True (back-compat)


def test_pole_is_required_reachable_separates_unpinnable_cat3_keep():
    # The pole is merge-blocking iff it's a required check OR a job the required work
    # `needs:`. A file-backed check that's neither (an independent job sharing the file) is
    # NOT reachable, even though `_required_reachable_checks` keeps it on the spine.
    job_graph = {".github/workflows/ci.yml": {
        "Build": {"name": "Build", "needs": []},
        "indep": {"name": "indep", "needs": []}}}
    crit_by_wf = {".github/workflows/ci.yml": {
        "job_p50": {"Build": 100.0, "indep": 200.0}}}
    req = frozenset({"Build"})
    assert cr._pole_is_required_reachable("Build", req, job_graph, crit_by_wf) is True   # required
    assert cr._pole_is_required_reachable("indep", req, job_graph, crit_by_wf) is False  # cat-3 keep
    assert cr._pole_is_required_reachable("indep", req, None, crit_by_wf) is True         # no graph -> defer
    assert cr._pole_is_required_reachable(None, req, job_graph, crit_by_wf) is True       # no pole -> defer


def test_check_to_job_node_scanned_maps_matrix_check_without_timing():
    # httpx bug: a TRIAGE-SKIPPED workflow's gate matrix check (`Python 3.9`) must still map
    # to its editable job via the SCANNED name template (no sampled timing), instead of being
    # mislabeled fileless/external ("no workflow to drill"). Exact + matrix-template match;
    # a genuinely fileless bot check stays unmapped.
    job_graph = {".github/workflows/test-suite.yml": {
        "tests": {"name": "Python ${{ matrix.python-version }}", "needs": []},
        "coverage": {"name": "coverage", "needs": ["tests"]}}}
    assert cr._check_to_job_node_scanned("Python 3.9", job_graph) == (
        ".github/workflows/test-suite.yml", "tests")          # matrix template
    assert cr._check_to_job_node_scanned("Python 3.13", job_graph) == (
        ".github/workflows/test-suite.yml", "tests")
    assert cr._check_to_job_node_scanned("coverage", job_graph) == (
        ".github/workflows/test-suite.yml", "coverage")       # exact name
    assert cr._check_to_job_node_scanned("Socket Security", job_graph) is None  # fileless bot
    # Ambiguous template match across TWO workflows → stay None (don't confidently bind the
    # wrong file); the timing mapper would disambiguate but it's unavailable on a triaged wf.
    ambig = {".github/workflows/release.yml": {"test": {"name": "test (${{ matrix.node }})"}},
             ".github/workflows/ci.yml": {"test": {"name": "test (${{ matrix.python }})"}}}
    assert cr._check_to_job_node_scanned("test (3.9)", ambig) is None
    single = {".github/workflows/ci.yml": {"test": {"name": "test (${{ matrix.python }})"}}}
    assert cr._check_to_job_node_scanned("test (3.9)", single) == (
        ".github/workflows/ci.yml", "test")
    # Same guard for an EXACT display-name collision across two workflows (two `lint` jobs,
    # two check-runs both literally named `lint`): with no timing anchor it can't tell them
    # apart, so it must bail to None, not bind whichever workflow iterates first.
    exact_ambig = {".github/workflows/a.yml": {"lint": {"name": "lint"}},
                   ".github/workflows/b.yml": {"lint": {"name": "lint"}}}
    assert cr._check_to_job_node_scanned("lint", exact_ambig) is None
    # ...but the SAME exact name appearing once binds normally.
    exact_single = {".github/workflows/a.yml": {"lint": {"name": "lint"}}}
    assert cr._check_to_job_node_scanned("lint", exact_single) == (
        ".github/workflows/a.yml", "lint")


def test_scanned_binders_refuse_degenerate_all_placeholder_matrix_name():
    # REGRESSION (tokio-rs): a job whose `name:` is ENTIRELY a matrix placeholder
    # (`${{ matrix.target }}`) compiles to the match-ANYTHING regex `^.+?$`. The no-timing
    # scanned binders then bound Netlify-managed external checks (`Redirect rules - tokio-rs`
    # & siblings — which appear in NO workflow YAML) to `ci.yml` ▸ `wasm32-wasip1`, fabricating
    # a file-backed long pole + a wrong-file agent prompt. A degenerate all-placeholder template
    # carries zero discriminating signal, so the scanned binders must refuse it: a check whose
    # ONLY match is `^.+?$` stays honestly fileless (`_check_to_job_node_scanned`'s own invariant).
    jg = {".github/workflows/ci.yml": {
        "wasm32-wasip1": {"name": "${{ matrix.target }}", "needs": [], "reusable": False,
                          "matrix": True},
        "test": {"name": "Test suite", "needs": [], "reusable": False}}}
    for ext in ("Redirect rules - tokio-rs", "Header rules - tokio-rs", "Pages changed - tokio-rs"):
        assert cr._check_to_job_node_scanned(ext, jg) is None, ext
        assert cr._check_to_workflow_file_static(ext, jg) is None, ext
    # The degenerate detector itself: all-placeholder (incl. whitespace-only anchoring) → True;
    # any real literal span → False.
    assert cr._name_template_is_degenerate("${{ matrix.target }}") is True
    assert cr._name_template_is_degenerate("${{ matrix.a }}${{ matrix.b }}") is True
    assert cr._name_template_is_degenerate("${{ matrix.a }} ${{ matrix.b }}") is True  # ws-only
    assert cr._name_template_is_degenerate("Python ${{ matrix.python }}") is False
    assert cr._name_template_is_degenerate("${{ matrix.shard }} of ${{ matrix.total }}") is False
    assert cr._name_template_is_degenerate("Lint") is False
    # A degenerate-named matrix job's OWN legs still resolve via the SAMPLED-timing anchor
    # (`_check_to_job_node` calls `_map_check_to_job` first) — only the no-timing scanned fallback
    # refuses. A foreign external check, having NO sampled job, never reaches that anchor.
    crit = {".github/workflows/ci.yml": {"job_p50": {"wasm32-wasip1": 22.0}}}
    assert cr._check_to_job_node("wasm32-wasip1", jg, crit) == (
        ".github/workflows/ci.yml", "wasm32-wasip1")
    assert cr._check_to_job_node("Redirect rules - tokio-rs", jg, crit) is None
    # A NON-degenerate matrix template still binds without timing (unchanged behavior).
    jg2 = {".github/workflows/test.yml": {
        "py": {"name": "Python ${{ matrix.python }}", "needs": [], "reusable": False,
               "matrix": True}}}
    assert cr._check_to_job_node_scanned("Python 3.9", jg2) == (".github/workflows/test.yml", "py")
    assert cr._check_to_workflow_file_static("Python 3.9", jg2) == ".github/workflows/test.yml"


def test_pole_mapping_scanned_fallback_for_triaged_workflow():
    # The POLE (not just the check) must get its (workflow_file, job) from the scanned graph
    # when the timing mapper misses a triage-skipped workflow — else the headline declares the
    # editable gate "managed/external, no workflow to drill" (the httpx end-to-end bug).
    jg = {".github/workflows/test-suite.yml": {
        "tests": {"name": "Python ${{ matrix.python-version }}", "needs": []}}}
    # crit_by_wf empty (workflow triaged → not fetched) → timing mapper returns None
    assert cr._pole_mapping("Python 3.9", {}, None, jg) == (
        ".github/workflows/test-suite.yml", "tests")
    # a caller-pinned mapping still wins verbatim; no graph → None (unchanged)
    assert cr._pole_mapping("x", {}, ("wf.yml", "j"), jg) == ("wf.yml", "j")
    assert cr._pole_mapping("Python 3.9", {}, None, None) is None


def test_is_pr_gate_check_never_drops_a_required_check():
    # A REQUIRED check is merge-blocking by definition — never excised from the spine, even
    # if its workflow only fires on `push` (a push status that satisfies the PR's required gate).
    crit_by_wf = {".github/workflows/release.yml": {"job_p50": {"build": 100.0}}}
    events = {".github/workflows/release.yml": {"push"}}   # push-only, no PR trigger
    # without the required short-circuit, a push-only check is dropped...
    assert cr._is_pr_gate_check("build", crit_by_wf, events, frozenset(), frozenset()) is False
    # ...but a REQUIRED check is kept.
    assert cr._is_pr_gate_check(
        "build", crit_by_wf, events, frozenset(), frozenset({"build"})) is True


def test_pr_gate_kept_when_a_same_named_job_lives_in_a_pr_workflow():
    # encord §6 Cause 1: `Run integration tests` is defined by a job in TWO workflows — a
    # `pull_request` one (the real PR gate) and a `push`-only one. Issue #59: `_map_check_to_job`
    # used to keep the SLOWEST mapping (here the push-only `test-sdk.yml`), which mis-attributed
    # the pole's workflow_file/steps. It now REFUSES cross-workflow same-name ambiguity and bails
    # to None. That never drops the PR gate: `_is_pr_gate_check` keeps a check whose SET of
    # matching workflows (via `_workflows_matching_check`, which enumerates ALL of them) includes
    # any PR-triggered one — the safe direction is unaffected by the mapper's bail.
    crit_by_wf = {
        ".github/workflows/sdk-pr.yml": {"job_p50": {"Run integration tests": 1040.0}},
        ".github/workflows/test-sdk.yml": {"job_p50": {"Run integration tests": 1200.0}},
    }
    events = {
        ".github/workflows/sdk-pr.yml": {"pull_request"},
        ".github/workflows/test-sdk.yml": {"push", "workflow_dispatch"},
    }
    # Cross-workflow same-name ambiguity → the mapper refuses to guess a single file.
    assert cr._map_check_to_job("Run integration tests", crit_by_wf) is None
    # ...but ANY matching workflow being PR-triggered keeps the check as a PR gate.
    assert cr._workflows_matching_check("Run integration tests", crit_by_wf) == {
        ".github/workflows/sdk-pr.yml", ".github/workflows/test-sdk.yml"}
    assert cr._is_pr_gate_check(
        "Run integration tests", crit_by_wf, events) is True
    # Sanity: when NO matching workflow is PR-triggered, the check is still dropped.
    push_only = {
        ".github/workflows/sdk-pr.yml": {"push"},
        ".github/workflows/test-sdk.yml": {"push", "workflow_dispatch"},
    }
    assert cr._is_pr_gate_check(
        "Run integration tests", crit_by_wf, push_only, frozenset()) is False


def test_off_spine_stamp_on_dropped_required_scoped_job_kills_long_pole_framing():
    # encord §6 Cause 2: the only required check is `Linting and type checking`, so the spine
    # is required-scoped. `Run integration tests` gates every PR (p50 ~1040s) but isn't
    # required → dropped to dropped_non_required_checks. It is defined in TWO workflows — the
    # PR gate `sdk-pr.yml` AND a push-only `test-sdk.yml` — and BOTH must be stamped: the
    # dropped finding group spans both, so a single-workflow stamp would miss `sdk-pr.yml` and
    # the group's OPT24 would still be framed on the critical path (the live dogfood bug).
    crit_by_wf = {
        ".github/workflows/sdk-pr.yml": {
            "job_p50": {"Run integration tests": 1040.0,
                        "Linting and type checking": 90.0}},
        ".github/workflows/test-sdk.yml": {
            "job_p50": {"Run integration tests": 1200.0}},
    }
    opt24_pr = {"id": "f1", "pattern": "OPT24",
                "workflow_file": ".github/workflows/sdk-pr.yml",
                "affected_jobs": ["Run integration tests"], "wall_clock_p50_s": 512.0}
    opt24_push = {"id": "f2", "pattern": "OPT24",
                  "workflow_file": ".github/workflows/test-sdk.yml",
                  "affected_jobs": ["Run integration tests"], "wall_clock_p50_s": 0.0}
    lint = {"id": "f3", "pattern": "OPT24",
            "workflow_file": ".github/workflows/sdk-pr.yml",
            "affected_jobs": ["Linting and type checking"], "wall_clock_p50_s": 40.0}
    cr._stamp_off_spine_findings(
        [opt24_pr, opt24_push, lint], ["Run integration tests"],
        ["Linting and type checking"], crit_by_wf)
    # The dropped, non-required job is stamped off-spine in BOTH workflows; neither is a long
    # pole, so the OPT24 group can't be framed on the critical path...
    assert opt24_pr.get("off_spine") is True
    assert opt24_push.get("off_spine") is True
    assert bp._saves_wall_clock(opt24_pr) is False
    # ...while the on-spine required check is untouched and stays a long pole.
    assert lint.get("off_spine") is None
    assert bp._saves_wall_clock(lint) is True


def test_off_spine_monorepo_name_collision_never_false_gaps_an_on_spine_pole():
    # The adversarial case the first attempt's NAME-only match failed: a monorepo where
    # `@a/pkg build` is dropped and `@b/pkg build` is ON the spine — both collapse to `build`.
    # A name-blind stamp would mark the on-spine `@b/pkg build` finding off-spine → a FALSE
    # coverage gap (the campaign's worst outcome). The identity-aware stamp must not, no matter
    # which workflow `_map_check_to_job` resolves the colliding check to.
    for a_p50, b_p50 in ((300.0, 500.0), (500.0, 300.0)):  # exercise both slowest-arrangements
        crit_by_wf = {
            ".github/workflows/a.yml": {"job_p50": {"build": a_p50}},
            ".github/workflows/b.yml": {"job_p50": {"build": b_p50}},
        }
        on_spine = {"id": "f1", "pattern": "OPT24",
                    "workflow_file": ".github/workflows/b.yml",
                    "affected_jobs": ["build"], "wall_clock_p50_s": 480.0}
        off_spine = {"id": "f2", "pattern": "OPT24",
                     "workflow_file": ".github/workflows/a.yml",
                     "affected_jobs": ["build"], "wall_clock_p50_s": 290.0}
        cr._stamp_off_spine_findings(
            [on_spine, off_spine], ["@a/pkg build"], ["@b/pkg build"], crit_by_wf)
        # The on-spine pole is NEVER stamped off-spine — its long-pole framing is preserved.
        assert on_spine.get("off_spine") is None, (a_p50, b_p50)
        assert bp._saves_wall_clock(on_spine) is True


def test_off_spine_stamp_via_the_scope_prefixed_subset_path():
    # POSITIVE coverage of the scoped-subset resolution path (the monorepo collision test above
    # asserts only the negative no-false-gap property and would survive a no-op stamper). Here a
    # scope-prefixed check `@a/pkg build` is dropped with NO colliding kept `@b` twin, so its
    # `build` job (a token-SUBSET of the check, scope-prefixed) must resolve and get stamped.
    crit_by_wf = {".github/workflows/a.yml": {"job_p50": {"build": 480.0}}}
    f = {"id": "f1", "pattern": "OPT24", "workflow_file": ".github/workflows/a.yml",
         "affected_jobs": ["build"], "wall_clock_p50_s": 300.0}
    cr._stamp_off_spine_findings([f], ["@a/pkg build"], ["Linting"], crit_by_wf)
    assert f.get("off_spine") is True
    assert bp._saves_wall_clock(f) is False


def test_off_spine_stamp_refused_when_any_affected_job_is_on_spine():
    # A finding touching BOTH an off-spine and an on-spine job must NOT be stamped (the kept
    # check is tested BEFORE the dropped check, so any on-spine job vetoes the stamp). Pins the
    # ordering: reversing the two `_job_on` checks would silently turn a partly-on-spine finding
    # into a false coverage gap — the worst-outcome class this campaign exists to prevent.
    crit_by_wf = {".github/workflows/ci.yml": {
        "job_p50": {"build": 500.0, "lint": 90.0}}}
    f = {"id": "f1", "pattern": "OPT24", "workflow_file": ".github/workflows/ci.yml",
         "affected_jobs": ["build", "lint"], "wall_clock_p50_s": 400.0}
    cr._stamp_off_spine_findings([f], ["build"], ["lint"], crit_by_wf)
    assert f.get("off_spine") is None
    assert bp._saves_wall_clock(f) is True


# --------------------------------------------------------------------------- #
# Triaged-fast file-backed check is NOT mislabeled "fileless / third-party"
# --------------------------------------------------------------------------- #

def test_wf_is_file_backed_predicate_stays_verbatim_synced():
    # The structural admission gate (collect_runs `_wf_is_file_backed`, which DROPS a name-inferred
    # file-backed lever) and the verify_report invariant (`_wf_is_file_backed`, which FLAGS one) must
    # agree on file-backedness — or a freshly-generated report could fail its own invariant (false
    # positive) or let a fabrication through (false negative). verify_report is standalone-by-design (no
    # skill imports), so the two can't share code; they are a deliberate VERBATIM copy, pinned here by
    # source equality (the gh_utils.py verbatim-copy pattern). Editing one predicate without the other
    # turns this red — the automated cross-file assertion greptile asked for.
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]

    def _return_body(rel: str) -> str:
        src = (root / rel).read_text(encoding="utf-8")
        m = re.search(
            r"def _wf_is_file_backed\(wf\)[^\n]*:\n(?:[ \t]+\"\"\".*?\"\"\"\n)?[ \t]+(return [^\n]+)",
            src, re.S)
        assert m, f"could not locate _wf_is_file_backed return in {rel}"
        return m.group(1).strip()

    engine = _return_body("skills/ci-speedup/scripts/collect_runs.py")
    checker = _return_body("skills/ci-speedup/tests/verify_report.py")
    expected = 'return str(wf or "").strip().lower().endswith((".yml", ".yaml"))'
    assert engine == checker == expected, (
        f"_wf_is_file_backed predicate drift — engine={engine!r} checker={checker!r} "
        "(the producer drop and the checker flag must stay verbatim-synced)")


def test_triaged_fast_file_backed_check_gets_no_name_inferred_structural_lever():
    # CLASS fix (supersedes the #110 wording-only patch on this exact case): a `Docs Drift`
    # check produced by an in-repo workflow (`drift.yml`) whose workflow was TRIAGED as fast
    # (jobs never step-sampled, so `_map_check_to_job` returns None) has NO measured dominant
    # step. #110 kept routing a qualitative OPT75 for it (just fixing the "fileless" wording) —
    # but routing ANY structural decomposition lever here infers the cost category from the
    # check NAME alone, the OPT49/OPT51 anti-pattern the no-decomp SIBLING branch already
    # refuses (test_file_backed_but_triaged_check_does_not_fabricate_fileless_opt75). The two
    # no-step branches must agree: a FILE-BACKED check with no sampled step gets NO structural
    # lever (it falls to OPT71 if non-required, else the renderer's generic dominant-step agent
    # prompt — NOT phase-4a, which needs a captured log a triaged-fast pole lacks). Enforced
    # uniformly at the sizing chokepoint; verify_report.check_structural_pole_has_measured_step
    # asserts the property. (rootlyhq/terraform-provider-rootly: drift.yml `Docs Drift`.)
    pr_checks = (("Docs Drift", 120.0),)
    crit_by_wf = {".github/workflows/drift.yml": {"job_p50": {}, "long_pole_p50": 0.0}}
    jpr: dict = {}
    events = {".github/workflows/drift.yml": {"pull_request"}}
    job_graph = {".github/workflows/drift.yml": {
        "docs-drift": {"name": "Docs Drift", "needs": []}}}
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, None, events, {}, 0, job_graph=job_graph)
    structural = [f for f in out if f["pattern"] in ("OPT70", "OPT72", "OPT75")]
    assert not structural, (
        "a file-backed (.yml) check with no sampled step must NOT get a name-inferred structural "
        f"lever (admission gate): {[(f['pattern'], f['workflow_file']) for f in structural]}")


def test_genuinely_fileless_check_still_labeled_fileless():
    # Keep the genuinely-fileless case (no producing workflow file anywhere — an external
    # app / bot check) labeled as before: the static job graph resolves NO file for it.
    pr_checks = (("CLA Assistant", 90.0),)
    crit_by_wf: dict = {}
    jpr: dict = {}
    events: dict = {}
    job_graph = {".github/workflows/ci.yml": {
        "build": {"name": "build", "needs": []}}}  # no `CLA Assistant` job
    out = cr._detect_structural_candidates(
        pr_checks, [], crit_by_wf, jpr, None, events, {}, 0, job_graph=job_graph)
    opt75 = [f for f in out if f["pattern"] == "OPT75"]
    assert opt75, "expected an OPT75 lever for the fileless check"
    ev = opt75[0]["evidence"].lower()
    assert "fileless" in ev, opt75[0]["evidence"]


def test_vr_event_sets_stay_coupled_to_collect_runs():
    """verify_report hand-copies collect_runs' event-volume sets as `_VR_*` (it is standalone — no
    skill imports), and re-derives merge-path eligibility from them (`_measured_merge_path_pole`). A
    silent divergence from collect_runs is the Class-A "mirror the wrong SELECTION" bug. Pin EQUALITY
    of the EVALUATED frozensets — `collect_runs._PR_VOLUME_EVENTS` is COMPUTED (`frozenset(_DEVELOPER_
    EVENTS) | {...}`), so a textual literal compare wouldn't catch a drift introduced via _DEVELOPER_
    EVENTS. (verify_report is stdlib-only, so this by-path load is clean and collision-proof.)"""
    import importlib.util
    vp = _SKILL_DIR / "tests" / "verify_report.py"
    spec = importlib.util.spec_from_file_location("ci_speedup_vr_event_coupling", vp)
    vr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = vr
    spec.loader.exec_module(vr)
    assert vr._VR_PR_VOLUME_EVENTS == cr._PR_VOLUME_EVENTS, \
        (vr._VR_PR_VOLUME_EVENTS, cr._PR_VOLUME_EVENTS)
    assert vr._VR_PUSH_VOLUME_EVENTS == cr._PUSH_VOLUME_EVENTS, \
        (vr._VR_PUSH_VOLUME_EVENTS, cr._PUSH_VOLUME_EVENTS)
    assert vr._VR_VOLUME_CONTAMINATING_EVENTS == cr._VOLUME_CONTAMINATING_EVENTS, \
        (vr._VR_VOLUME_CONTAMINATING_EVENTS, cr._VOLUME_CONTAMINATING_EVENTS)


def _load_vr_module():
    """By-path load of verify_report under a unique name (it is stdlib-only + standalone, and
    ci-secure ships a same-named file, so a plain import would collide)."""
    import importlib.util
    vp = _SKILL_DIR / "tests" / "verify_report.py"
    spec = importlib.util.spec_from_file_location("ci_speedup_vr_cache_coupling", vp)
    vr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = vr
    spec.loader.exec_module(vr)
    return vr


def test_vr_cache_literals_stay_coupled_to_renderer():
    """verify_report's cache-framing checks key on EXACT renderer prose + engine thresholds, hand-copied
    as `_VR_*` (it is standalone — no skill imports). A silent reword/retune would unhook the guard
    (Lesson L7). Pin: the machine marker + the three tuned constants are EQUAL across the files, and each
    cache-claim regex literal actually appears in blocking_path's source (so a renamed render string
    breaks THIS test, forcing a lockstep update)."""
    vr = _load_vr_module()
    # Marker + thresholds equal across engine (blocking_path / collect_runs) and verifier.
    assert vr._VR_CACHE_CONTEXT_MARKER == bp._CACHE_CONTEXT_MARKER
    assert vr._VR_PARTIAL_MISS_FLOOR_PCT == bp._PARTIAL_MISS_FLOOR_PCT
    assert vr._VR_CACHE_TAIL_MIN_FRAC == cr._CACHE_TAIL_MIN_FRAC
    assert vr._VR_CACHE_COLD_MISS_PCT == cr._CACHE_COLD_MISS_PCT
    # The demotion literals the renderer strips must match what the verifier looks for.
    assert vr._VR_BIGGEST_LEVER in bp._BIGGEST_LEVER
    # The engine + verifier must agree on the cache-leaf fix_key set.
    assert bp._CACHE_LEAF_KEYS == cr._CACHE_LEAF_KEYS
    # Each cache-claim regex must have a real anchor in the renderer source, so a renamed
    # claim string can't silently escape the framing guard.
    src = (_SKILL_DIR / "scripts" / "blocking_path.py").read_text(encoding="utf-8")
    anchors = {
        r"cache-key churn": "cache-key churn",
        r"rebuilt despite caching ON": "rebuilt despite caching ON",
        r"packages rebuilt from scratch \(cache-miss\)": "packages rebuilt from scratch (cache-miss)",
        r"Remote caching DISABLED": "Remote caching DISABLED",
        r"rebuilds \d+ layers from scratch": "rebuilds ",
        # The lifecycle note renders "build work runs during dependency install" but is split
        # across f-string lines in source; anchor on the contiguous tail fragment.
        r"build work runs during dependency install": "during dependency install",
    }
    patterns = {rx.pattern for rx in vr._VR_CACHE_CLAIM_RES}
    for pat, literal in anchors.items():
        assert pat in patterns, f"claim regex {pat!r} not registered in verify_report"
        assert literal in src, f"renderer no longer emits {literal!r} — claim regex {pat!r} is now dead"


def test_vr_cache_verdict_matches_engine_verdict():
    """The verifier's `_vr_cache_verdict` must re-derive the SAME verdict as the engine's
    `collect_runs._cache_verdict` for every distribution shape — else a report could be judged against a
    verdict the engine never assigns (a false FAIL, or worse a masked FAIL). Property-style grid over
    cold / churn / miss-tail / mostly-warm / insufficient, including a fork-excluded case."""
    vr = _load_vr_module()

    def val(miss, fork=False):
        return {"value": miss, "fork": fork, "duration_s": 100.0,
                "cache_state": {"miss_pct": miss, "cold": miss >= 99.5, "remote_off": False}}

    grid = [
        ([val(9), val(12), val(7), val(44), val(3), val(2)], {"prevalence_max": 0.1}),   # mostly-warm
        ([val(9), val(12), val(7), val(44), val(3), val(2)], {"prevalence_max": 0.4}),   # miss-tail
        ([val(44), val(55), val(48), val(60)], {"prevalence_max": 0.5}),                 # churn
        ([val(100), val(100), val(100)], {"prevalence_max": 1.0}),                       # cold
        ([val(50)], {"prevalence_max": 0.0}),                                            # insufficient (n<2)
        ([val(9, fork=True), val(44)], {"prevalence_max": 0.0}),                         # fork excluded -> n<2
        ([val(9, fork=True), val(9), val(12), val(7)], {"prevalence_max": 0.05}),        # fork excluded, warm
    ]
    for values, tail in grid:
        engine = cr._cache_verdict(values, tail, floor_pct=bp._PARTIAL_MISS_FLOOR_PCT,
                                   tail_min_frac=cr._CACHE_TAIL_MIN_FRAC,
                                   cold_pct=cr._CACHE_COLD_MISS_PCT)
        mirror = vr._vr_cache_verdict({"pr": {"values": values}, "tail": tail})
        assert engine == mirror, (values, tail, engine, mirror)


def test_vr_pole_freq_literals_stay_coupled_to_collect_runs():
    """The phantom-gate invariant (`check_headline_pole_actually_gates`) re-derives the recurrence
    floor + min-sample gate that `collect_runs._rank_spine_present_first` ranks by. Pin EQUALITY so
    a retune on one side breaks this test, not the guard silently (Lesson L7)."""
    vr = _load_vr_module()
    assert vr._VR_POLE_RECUR_FLOOR == cr._POLE_RECUR_FLOOR
    assert vr._VR_RARE_PRESENCE_MIN_PR == cr._RARE_PRESENCE_MIN_PR
    # blocking_path's renderer split must use the same floor (the typical/rare demotion mirror).
    assert bp._POLE_RECUR_FLOOR == cr._POLE_RECUR_FLOOR
    # The presence-minority fraction drives `_rare_demoted_check_names`' opt-in clause; it is a
    # VERBATIM copy of blocking_path's `_RARE_PRESENCE_FRAC`, so pin EQUALITY (like the other two)
    # or a retune of the renderer's minority cutoff would silently desync the verifier's mirror.
    assert vr._VR_RARE_PRESENCE_FRAC == bp._RARE_PRESENCE_FRAC


def test_vr_pole_frequencies_matches_engine():
    """`_vr_pole_frequencies` (from populations, slowest-first) must agree with the engine's
    `collect_runs._pole_frequencies` (from per-PR check maps) for the same PRs — else the invariant
    judges the headline against a pole count the engine never computed. Covers candidate
    restriction, job-p50 capping, and co-slowest TIES (all three must match)."""
    vr = _load_vr_module()
    # per-PR check maps (engine input) and the slowest-first populations shape (verifier input).
    per_sha = ([{"check-packages": 180.0, "heavy": 1400.0}] * 4
               + [{"check-packages": 180.0, "med": 400.0}] * 6
               + [{"solo": 90.0}] * 2)
    cands = set().union(*[set(m) for m in per_sha])
    eng = cr._pole_frequencies(per_sha, cands)
    ver = vr._vr_pole_frequencies(_populations_from_maps(per_sha), cands)
    assert eng["heavy"] == 4 and eng["med"] == 6 and eng["solo"] == 2 and eng["check-packages"] == 0
    for k in cands:
        assert ver.get(k, 0) == eng.get(k, 0), (k, ver.get(k, 0), eng.get(k, 0))

    # TIES: two co-equal-slowest checks on every PR both earn credit (sum > n_pr).
    tied = [{"A": 100.0, "B": 100.0}] * 5
    eng_t = cr._pole_frequencies(tied, {"A", "B"})
    ver_t = vr._vr_pole_frequencies(_populations_from_maps(tied), {"A", "B"})
    assert eng_t == {"A": 5, "B": 5} and ver_t == {"A": 5, "B": 5}

    # CAP: a raw span inflated above the job p50 is de-inflated before the argmax, so an inflated
    # light check does NOT out-rank a stable heavier one (engine caps; populations are pre-capped
    # so the verifier compares against already-capped magnitudes).
    caps = {"light": 200.0, "heavy": 300.0}
    inflated = [{"light": 900.0, "heavy": 300.0}] * 6   # light's raw span 900 > its p50 cap 200
    eng_c = cr._pole_frequencies(inflated, {"light", "heavy"}, caps)
    assert eng_c["heavy"] == 6 and eng_c["light"] == 0
    # populations feed the verifier already capped (min(raw, cap)); it must agree.
    capped_pops = _populations_from_maps([{k: min(v, caps[k]) for k, v in m.items()}
                                          for m in inflated])
    assert vr._vr_pole_frequencies(capped_pops, {"light", "heavy"}) == {"heavy": 6}


def _populations_from_maps(per_sha):
    """Per-PR {check: dur} maps → the slowest-first `populations` shape the verifier reads."""
    return [sorted(((k, v) for k, v in m.items()), key=lambda kv: -kv[1]) for m in per_sha]


def test_cache_verdict_exact_boundaries():
    """Pin the verdict operators at their exact thresholds (PR #126 review): miss==40 -> churn,
    prevalence==0.25 -> miss-tail, miss==99.5 -> cold. A flip of any `>=` to `>` fails here."""
    def val(miss, fork=False, cold=None, remote_off=False):
        st = {"miss_pct": miss, "cold": (miss >= 99.5 if cold is None else cold),
              "remote_off": remote_off}
        return {"value": miss, "fork": fork, "duration_s": 100.0, "cache_state": st}
    kw = dict(floor_pct=bp._PARTIAL_MISS_FLOOR_PCT, tail_min_frac=cr._CACHE_TAIL_MIN_FRAC,
              cold_pct=cr._CACHE_COLD_MISS_PCT)
    # median exactly at the 40% floor -> churn
    assert cr._cache_verdict([val(40), val(40)], {"prevalence_max": 0.0}, **kw) == "churn"
    # median just below -> not churn; prevalence exactly 0.25 -> miss-tail
    assert cr._cache_verdict([val(39), val(39), val(1)], {"prevalence_max": 0.25}, **kw) == "miss-tail"
    # same low median, prevalence just below 0.25 -> mostly-warm
    assert cr._cache_verdict([val(39), val(39), val(1)], {"prevalence_max": 0.24}, **kw) == "mostly-warm"
    # miss exactly at cold threshold 99.5 (state not pre-flagged) -> cold via the miss>=cold_pct arm
    assert cr._cache_verdict([val(99.5, cold=False), val(99.5, cold=False)],
                             {"prevalence_max": 0.0}, **kw) == "cold"
    # fewer than 2 upstream (one fork) -> insufficient
    assert cr._cache_verdict([val(50, fork=True), val(50)], {"prevalence_max": 0.0}, **kw) == "insufficient"


def test_pole_caps_raises_to_bimodal_slow_mode():
    # T4 (2nd review): _pole_caps had no direct test. The cap is the job p50 RAISED to the bimodal
    # slow-mode median, so a genuinely-slow bimodal gate isn't clamped to its fast p50.
    caps = cr._pole_caps({"A": 100.0, "B": 300.0}, {"A": {"high_p50_s": 250.0}})
    assert caps["A"] == 250.0    # raised from 100 to the slow-mode median
    assert caps["B"] == 300.0    # no bimodal entry -> unchanged
    assert cr._pole_caps({}, {}) == {}


def test_cache_distribution_keeps_warm_runs_reaches_mostly_warm(tmp_path: Path):
    # F2 (2nd adversarial review): the drilled run is miss-heavy (fires turbo-partial-cache) but the
    # sibling runs are WARM (D2 doesn't re-fire on them). Before the fix those warm runs were dropped
    # from the sample, so the distribution held only >=40%-miss runs and the verdict was stuck at
    # churn. Now the warm runs enter via their cache_state, so the pole correctly reads mostly-warm.
    def partial_log(cached: int, total: int = 128) -> str:  # caching ON (no "disabled" line)
        return "\n".join([f"Tasks:    {total} total",
                          f"Cached:    {cached} cached, {total} total", "Time:    3m00s"])
    logs_by_jid = {5: partial_log(60),    # drilled: 53% miss -> turbo-partial-cache fires
                   4: partial_log(122),   # warm: ~5% miss -> D2 does NOT fire
                   6: partial_log(120)}   # warm: ~6% miss -> D2 does NOT fire

    class FakeClient:
        queries = errors = 0
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            jid = int(endpoint.split("/jobs/")[1].split("/")[0])
            return logs_by_jid[jid]

    poles = [{"check": "build", "workflow_file": ".github/workflows/ci.yml", "job": "build",
              "p50_s": 180.0}]
    jpr = {".github/workflows/ci.yml": [
        [_job_inst("build", 4, 170)],
        [_job_inst("build", 5, 180)],   # nearest P50 -> drilled (miss-heavy)
        [_job_inst("build", 6, 190)],
    ]}
    cr._persist_pole_logs(FakeClient(), "o/r", poles, jpr, tmp_path, mag_runs=3,
                          events_jobs_by_wf={})
    cd = poles[0].get("cache_dist")
    assert cd is not None, "a turbo-partial-cache pole must carry a cache_dist"
    misses = sorted(round(v["cache_state"]["miss_pct"]) for v in cd["pr"]["values"]
                    if v.get("cache_state"))
    assert 5 in misses and 6 in misses, ("warm sibling runs must be in the distribution", misses)
    # The upstream median is now warm (~6%), so the verdict is DEMOTED off churn (here miss-tail:
    # the drilled 53% run is a duration-tail). Pre-fix the warm runs were dropped -> only [53] ->
    # churn. That demotion is the whole point of F2.
    assert cd["pr"]["upstream_median"] < 40.0
    assert cd["verdict"] in ("miss-tail", "mostly-warm") and cd["verdict"] != "churn", cd["verdict"]

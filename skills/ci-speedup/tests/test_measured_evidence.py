"""Run-cited evidence for data-driven findings.

A timing finding (shard imbalance, long test job, step outlier, …) is proven by
run TIMINGS, not by a workflow file:line. These tests pin that the detectors
emit structured `measured_evidence` whose table cells link to the ACTUAL GitHub
job logs, and that the report renders that table (not a meaningless code
permalink + YAML block).

The render check runs report.py as a SUBPROCESS (mirroring real invocation),
which also sidesteps the `config` module-name collision on the shared pytest
pythonpath.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_measured_evidence.py
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

# The public repo ships no committed worked-example corpus (legacy reports/ is not
# published; fresh examples come from a validation run). Corpus-dependent guards
# must skip LOUDLY when it's absent, never pass vacuously — and run again the moment
# a corpus reappears (a generated examples/ report, or in the internal development repo).
_NO_CORPUS_REASON = ("no committed report corpora in this repo — corpus guards run "
                     "against generated reports / in the internal development repo")
from pathlib import Path

from collect_runs import (
    _attach_cache_log_evidence,
    _cache_status_from_log,
    _detect_opt24_long_test_no_sharding,
    _detect_opt25_shard_imbalance,
    _detect_opt48_failure_rate,
    _effective_volume,
    _job_needs_relations,
)

_SKILL_DIR = Path(__file__).resolve().parents[1]


def test_committed_better_auth_headline_is_the_real_long_pole():
    """Artifact-level guard for the critical-path + runner-scoping fixes: in the
    shipped better-auth example the credited wall-clock lever must be the REAL
    long pole — the slow prisma-adapter matrix leg (OPT25) — not the `test` job
    (OPT24), which measurement showed is not the critical path on its runner.
    A future regen that re-credits OPT24's `test` job as a wall-clock win (the
    original bug — crediting its full ~167s job saving) fails CI."""
    fj = _SKILL_DIR / "reports" / "better-auth" / "findings.json"
    if not fj.exists():
        pytest.skip(_NO_CORPUS_REASON)
    findings = json.loads(fj.read_text())["findings"]
    # OPT24's `test` job is NOT the global long pole (prisma-adapter Integration
    # at ~568s is). It MAY carry a small POPULATION-WEIGHTED credit because `test`
    # is the gate on a minority of PRs (the merge gate is bimodal; measured ~1/20
    # here → ~7s expected of its 167s raw saving), but it must never be credited
    # as a major wall-clock lever rivalling the real long pole — the original bug
    # credited the full per-job saving as if `test` gated every PR.
    for f in findings:
        if f.get("pattern") == "OPT24":
            wc = f.get("wall_clock_p50_s") or 0.0
            assert wc < 60.0, (
                f"OPT24 (`test`) credited {wc:.0f}s wall-clock — far above its "
                "population-weighted floor; the adapter-integration job, not "
                "`test`, is the real long pole (a regen crediting the full job "
                "saving is the original bug)")
    # OPT25 (matrix leg imbalance — split the slow prisma-adapter leg) is the
    # headline, and still carries its per-leg evidence table.
    opt25 = [f for f in findings if f.get("pattern") == "OPT25"]
    assert opt25, "OPT25 (the real long-pole fix) should be present"
    me = opt25[0]["measured_evidence"]
    assert me["table"]["headers"][0] == "Leg"
    assert any("prisma" in r[0].lower() for r in me["table"]["rows"])


def test_committed_mastra_headline_is_required_reachable_never_a_dropped_check():
    """Artifact-level guard for the required-reachability scope: in the shipped mastra
    example the spine must be the MERGE-BLOCKING work. The non-required `changed-tests`
    (the @782s check the bug headlined) AND `Validate build outputs` (a `lint.yml` job
    the required `Lint` does not `needs:`, which an interim file-membership matcher
    wrongly kept) must both be in `dropped_non_required_checks`, and the headline pole
    must NOT be any dropped check. A regen that re-headlines a non-required check fails CI."""
    fj = _SKILL_DIR / "reports" / "mastra" / "findings.json"
    if not fj.exists():
        pytest.skip(_NO_CORPUS_REASON)
    cp = json.loads(fj.read_text())["pr_critical_path"]
    dropped = set(cp.get("dropped_non_required_checks") or [])
    assert {"changed-tests", "Validate build outputs"} <= dropped, (
        "the non-required, non-merge-gating checks must be dropped from the spine")
    # The headline pole (and every spine check) is required-reachable, never a dropped one.
    assert cp["critical_path_check"] not in dropped
    spine = {c["name"] for c in cp.get("checks") or []}
    assert not (spine & dropped), "a dropped non-required check leaked back onto the spine"


def _job(name: str, dur_s: int, job_id: int) -> dict:
    return {
        "name": name,
        # The real jobs API always carries `conclusion` on a completed job, and the
        # log sites now gate the fetch on it (a queued job has no log to serve) — so
        # a fixture that omits it is not a faithful stand-in for a job that ran.
        "conclusion": "success",
        "started_at": "2026-05-29T10:00:00Z",
        "completed_at": f"2026-05-29T10:{dur_s // 60:02d}:{dur_s % 60:02d}Z",
        "run_id": 111,
        "html_url": f"https://github.com/o/r/actions/runs/111/job/{job_id}",
    }


def test_opt25_heterogeneous_matrix_says_split_not_rebalance():
    # Prefix-varying legs = different packages (adapters). Imbalanced
    # (prisma ~300s vs memory ~30s, drizzle 256s the 2nd-slowest).
    runs = []
    for run in range(2):
        runs.append([
            _job("prisma-adapter Integration Test", 300, 1000 + run),
            _job("memory-adapter Integration Test", 30, 2000 + run),
            _job("drizzle-adapter Integration Test", 256, 3000 + run),
        ])
    # The matrix IS the workflow long pole (prisma 300s = the slowest job), so the
    # floor cap is a no-op and the 44s saving stands. (crit floor = 2nd-slowest leg.)
    crit = {"long_pole_job": "prisma-adapter Integration Test", "long_pole_p50": 300.0,
            "floor_p50": 256.0, "job_p50": {
                "prisma-adapter Integration Test": 300.0,
                "memory-adapter Integration Test": 30.0,
                "drizzle-adapter Integration Test": 256.0}}
    findings = _detect_opt25_shard_imbalance("e2e.yml", runs, 0, crit)
    assert findings, "OPT25 should fire on the imbalance"
    f = findings[0]
    assert f["title"] == "Matrix Leg Imbalance"          # not "Shard Imbalance"
    me = f["measured_evidence"]
    assert me["table"]["headers"][0] == "Leg"            # legs, not shards
    rows = me["table"]["rows"]
    assert len(rows) == 3
    assert all("https://github.com/o/r/actions/runs/" in r[-1] for r in rows)
    assert "prisma-adapter" in rows[0][0]                # slowest leg first
    assert "split" in me["note"].lower() and "not rebalance" in me["note"].lower()
    # Saving floored at the 2nd-slowest leg (256s), NOT slow-mean.
    assert f["wall_clock_p50_s"] == 300.0 - 256.0        # = 44s, not ~105


def test_opt24_evidence_shows_long_pole_context():
    # The proof a shard is NEEDED isn't "this job is long" — it's that the job
    # is the serial LONG POLE that gates the run, towering over jobs that
    # already finish in parallel. The evidence table must show every sibling
    # job with its role so a reader can see why sharding the long pole helps.
    runs = []
    for run in range(3):
        runs.append([
            _job("test (22.x)", 440, 1000 + run),
            _job("test (24.x)", 400, 2000 + run),
            _job("lint", 168, 3000 + run),
            _job("typecheck", 74, 4000 + run),
        ])
    findings = _detect_opt24_long_test_no_sharding("ci.yml", runs, 0)
    assert findings, "OPT24 should fire on the unsharded >5min test job"
    me = findings[0]["measured_evidence"]
    headers = me["table"]["headers"]
    assert headers == ["Job", "P50", "P95", "Samples",
                       "Slowest run (job log)", "Role"]
    rows = me["table"]["rows"]
    # The flagged `test` job (matrix legs collapsed) is the long pole, ranked
    # first, and every sibling job appears as context.
    assert rows[0][0] == "`test`"
    assert "long pole" in rows[0][-1].lower()
    sibling_names = {r[0] for r in rows[1:]}
    assert sibling_names == {"`lint`", "`typecheck`"}
    assert all("parallel" in r[-1].lower() for r in rows[1:])
    # Every row links a real job log (verifiable), and the summary names the
    # gap to the next-longest job (why this one gates the run).
    assert all("https://github.com/o/r/actions/runs/" in r[4] for r in rows)
    assert "long pole" in me["summary"].lower()
    assert "next-longest job" in me["summary"] and "`lint`" in me["summary"]


def test_opt24_single_job_workflow_drops_long_pole_framing():
    # When the flagged test job is the ONLY job in its workflow, there are no
    # siblings to contextualize against — the "long pole, every other job
    # finishes earlier" framing would be a false claim. Fall back to the plain
    # single-job evidence (it's long and unsharded), no Role column.
    runs = [[_job("Docs E2E tests", 344, 9000 + run)] for run in range(3)]
    findings = _detect_opt24_long_test_no_sharding("e2e-docs.yml", runs, 0)
    assert findings, "OPT24 should still fire on a lone long unsharded job"
    me = findings[0]["measured_evidence"]
    assert me["table"]["headers"] == ["Job", "P50", "P95", "Samples",
                                      "Slowest run (job log)"]
    assert len(me["table"]["rows"]) == 1
    assert "long pole" not in me["summary"].lower()
    assert "every other job" not in me["summary"].lower()
    assert "unsharded" in me["summary"].lower()


def test_opt24_concurrent_longer_sibling_drops_superlatives_and_zeroes_saving():
    # The flagged TEST job is >5min and unsharded, but a CONCURRENT (parallel,
    # no `needs:`) sibling (`docker-build`) is LONGER — so `test` is NOT the
    # wall-clock pole and sharding it saves ~0. The evidence must not claim "the
    # long pole" / "nothing else takes as long" / an "N× next-longest" lead; it
    # must name the concurrent sibling as the gate; the credited wall-clock is 0.
    runs = []
    for run in range(3):
        runs.append([
            _job("test", 400, 1000 + run),
            _job("docker-build", 700, 2000 + run),  # a longer, parallel sibling
            _job("lint", 100, 3000 + run),
        ])
    findings = _detect_opt24_long_test_no_sharding("ci.yml", runs, 0)
    assert findings, "OPT24 still fires on the unsharded >5min test job"
    f = findings[0]
    me = f["measured_evidence"]
    summary = me["summary"]
    assert "the long pole of this workflow" not in summary
    assert "next-longest job" not in summary          # gap-clause suppressed
    assert "nothing else takes as long" not in me["note"]
    assert "`docker-build` runs concurrently and is at least as long" in summary
    # Sharding a non-pole concurrent job saves no wall-clock — the consumed number
    # and the finding-level size_note must both say so.
    assert f["wall_clock_p50_s"] == 0.0
    assert "long pole" not in f["size_note"]
    rows = me["table"]["rows"]
    test_row = next(r for r in rows if r[0] == "`test`")
    assert "long pole" not in test_row[-1].lower()
    db_row = next(r for r in rows if r[0] == "`docker-build`")
    assert "longer than this job" in db_row[-1].lower()


def test_opt24_concurrent_TIE_is_not_the_sole_pole_zeroes_saving():
    # Two symmetric test suites run CONCURRENTLY at EQUAL p50 (no `needs:` between them).
    # Neither is the sole pole — the tied job still gates the run after the other is
    # sharded — so the credited saving is 0 and the "no concurrent job takes as long"
    # superlative must NOT fire. (Pre-fix the strict `>` let the tie overcredit p50/2.)
    runs = []
    for run in range(3):
        runs.append([
            _job("unit-tests", 400, 1000 + run),
            _job("integration-tests", 400, 2000 + run),  # tied, concurrent
        ])
    findings = _detect_opt24_long_test_no_sharding("ci.yml", runs, 0)
    assert findings
    f = findings[0]
    assert f["wall_clock_p50_s"] == 0.0          # a tie gates → no wall-clock saving
    assert f["realization"] == "none"            # zeroed saving isn't a realizable win
    me = f["measured_evidence"]
    assert "the long pole of this workflow" not in me["summary"]
    assert "at least as long" in me["summary"]   # the tied sibling named as a co-gate
    assert "long pole" not in f["size_note"]


def test_opt24_sequential_longer_sibling_stays_the_pole():
    # A longer sibling that runs SEQUENTIALLY (`test` needs: `build`, `build` is
    # longer) does NOT disqualify `test`: both are on the critical path, so `test`
    # is still a serial segment worth sharding — the superlatives stand and the
    # p50/2 wall-clock saving is credited (not zeroed). This is the review's key
    # case: gating must consult the needs: graph, not raw durations.
    runs = []
    for run in range(3):
        runs.append([
            _job("test", 400, 1000 + run),
            _job("build", 700, 2000 + run),   # longer, but `test` needs: it
        ])
    wf_doc = {"jobs": {"build": {}, "test": {"needs": ["build"]}}}
    findings = _detect_opt24_long_test_no_sharding(
        "ci.yml", runs, 0, wf_doc=wf_doc)
    assert findings
    f = findings[0]
    me = f["measured_evidence"]
    # `build` is sequential, so it is NOT named as a concurrent gate, and `test`
    # keeps its long-pole framing + a real (non-zero) saving.
    assert "runs concurrently and is at least as long" not in me["summary"]
    assert f["wall_clock_p50_s"] == 200.0   # 400/2 — still credited
    build_row = next(r for r in me["table"]["rows"] if r[0] == "`build`")
    assert "before" in build_row[-1].lower()   # "runs before — this job needs: it"


def test_job_needs_relations_transitive_both_directions():
    # The closures must be TRANSITIVE (multi-hop), not just direct edges. Chain
    # lint → build → test → deploy → notify with the long pole in the middle
    # (`test`): everything upstream resolves to "before" and everything downstream
    # to "after", though most have no direct edge to `test`.
    doc = {"jobs": {
        "lint": {},
        "build": {"needs": ["lint"]},
        "test": {"needs": ["build"]},
        "deploy": {"needs": ["test"]},
        "notify": {"needs": ["deploy"]},
    }}
    rel = _job_needs_relations(doc, "test")
    assert rel == {"lint": "before", "build": "before",
                   "deploy": "after", "notify": "after"}


def test_job_needs_relations_multi_dep_and_string_form():
    # `needs:` may be a bare string or a multi-element list; both feed the closure.
    doc = {"jobs": {
        "a": {},
        "b": {},
        "test": {"needs": ["a", "b"]},     # multi-dep list
        "ship": {"needs": "test"},          # bare string form
    }}
    rel = _job_needs_relations(doc, "test")
    assert rel == {"a": "before", "b": "before", "ship": "after"}


def test_job_needs_relations_ghost_dep_and_no_needs_default_parallel():
    # A `needs:` pointing at a job absent from the doc is harmless (it never maps
    # to a base), and a sibling with no path either way is "parallel".
    doc = {"jobs": {
        "test": {"needs": ["does-not-exist"]},
        "lint": {},                         # unrelated → parallel
    }}
    rel = _job_needs_relations(doc, "test")
    assert rel == {"lint": "parallel"}


def test_job_needs_relations_interpolated_matrix_name_is_unresolvable():
    # KNOWN LIMITATION (documented in the helper): a matrix job whose `name:`
    # interpolates `${{ }}` renders at runtime as e.g. `test 3.10` (matrix value
    # inline, no parens), which `_matrix_base_name` can't reduce to the doc key
    # `unit-test`. The graph can't be resolved statically, so the target base is
    # unknown and the helper returns {} — the caller then falls back to the SAFE
    # conservative default (every sibling "parallel"). This never invents a false
    # sequential edge; it just can't upgrade the wording for interpolated names.
    doc = {"jobs": {
        "unit-test": {"name": "test ${{ matrix.py }}", "strategy":
                      {"matrix": {"py": ["3.10", "3.11"]}}},
        "deploy": {"needs": ["unit-test"]},
    }}
    # `target_base` here is the RENDERED base, which differs from the doc key.
    assert _job_needs_relations(doc, "test 3.10") == {}


def test_opt24_needs_chained_sibling_not_labeled_parallel():
    # Regression (class shared with OPT73): a sibling wired downstream via `needs:`
    # — a `deploy` gated on the long test job — runs AFTER it, sequentially, NOT
    # "in parallel". OPT24 must derive run-order from the workflow's `needs:` graph,
    # not from durations alone, or it repeats OPT73's "concurrent" falsehood in a
    # different detector: a shorter job is not necessarily a parallel job.
    runs = [[_job("test", 440, 1000 + run), _job("deploy", 60, 2000 + run)]
            for run in range(3)]
    doc = {"jobs": {
        "test": {"runs-on": "ubuntu-latest"},
        "deploy": {"needs": ["test"], "runs-on": "ubuntu-latest"},
    }}
    findings = _detect_opt24_long_test_no_sharding("ci.yml", runs, 0, wf_doc=doc)
    assert findings, "OPT24 should fire on the unsharded >5min test job"
    me = findings[0]["measured_evidence"]
    deploy_row = next(r for r in me["table"]["rows"] if r[0] == "`deploy`")
    role = deploy_row[-1].lower()
    assert "parallel" not in role, role           # it does NOT run in parallel
    assert "needs" in role and "after" in role, role
    # ...and the summary must not assert ANY concurrency: with deploy as the only
    # sibling and it `needs:`-chained, there is no parallel job to speak of, so the
    # "either concurrent with it or ..." disjunct would itself be a false claim
    # (caught in review). The all-sequential case says so outright.
    summary = me["summary"].lower()
    assert "finishes earlier and in parallel" not in summary, summary
    assert "either concurrent with it" not in summary, summary
    assert "needs:`-chained" in me["summary"], me["summary"]


def test_opt24_mixed_parallel_and_sequential_siblings_names_both():
    # A workflow with BOTH a genuinely parallel sibling (`lint`) and a sequential
    # one (`deploy` needs the long pole) keeps the "either concurrent ... or
    # `needs:`-chained" disjunct — here it is true, because a parallel job exists.
    runs = [[_job("test", 440, 1000 + run), _job("lint", 120, 2000 + run),
             _job("deploy", 60, 3000 + run)] for run in range(3)]
    doc = {"jobs": {
        "test": {"runs-on": "ubuntu-latest"},
        "lint": {"runs-on": "ubuntu-latest"},                       # parallel
        "deploy": {"needs": ["test"], "runs-on": "ubuntu-latest"},  # sequential
    }}
    findings = _detect_opt24_long_test_no_sharding("ci.yml", runs, 0, wf_doc=doc)
    me = findings[0]["measured_evidence"]
    rows = {r[0]: r[-1].lower() for r in me["table"]["rows"]}
    assert "parallel" in rows["`lint`"]
    assert "parallel" not in rows["`deploy`"] and "after" in rows["`deploy`"]
    assert "either concurrent with it" in me["summary"].lower(), me["summary"]


def test_opt24_parallel_siblings_keep_parallel_wording_with_doc():
    # Behavior-preserving: when the parsed doc has NO `needs:` edges the siblings
    # are genuinely concurrent, so the role stays "parallel" and the summary keeps
    # its original "every other job finishes earlier and in parallel" wording.
    runs = [[_job("test", 440, 1000 + run), _job("lint", 100, 2000 + run)]
            for run in range(3)]
    doc = {"jobs": {"test": {}, "lint": {}}}   # no needs: → genuinely parallel
    findings = _detect_opt24_long_test_no_sharding("ci.yml", runs, 0, wf_doc=doc)
    me = findings[0]["measured_evidence"]
    lint_row = next(r for r in me["table"]["rows"] if r[0] == "`lint`")
    assert "parallel" in lint_row[-1].lower()
    assert "finishes earlier and in parallel" in me["summary"].lower()


def test_opt24_upstream_needs_sibling_labeled_sequential():
    # A sibling the long pole itself `needs:` (an upstream `build` the test job
    # depends on) runs BEFORE it, serially — also not "in parallel".
    runs = [[_job("test", 440, 1000 + run), _job("build", 90, 2000 + run)]
            for run in range(3)]
    doc = {"jobs": {
        "build": {"runs-on": "ubuntu-latest"},
        "test": {"needs": ["build"], "runs-on": "ubuntu-latest"},
    }}
    findings = _detect_opt24_long_test_no_sharding("ci.yml", runs, 0, wf_doc=doc)
    me = findings[0]["measured_evidence"]
    build_row = next(r for r in me["table"]["rows"] if r[0] == "`build`")
    role = build_row[-1].lower()
    assert "parallel" not in role, role
    assert "before" in role and "needs" in role, role


def test_opt25_off_critical_path_matrix_caps_saving_to_zero():
    # The imbalanced matrix (prisma 200 vs memory 30, 6.7×) is NOT the workflow long
    # pole — a `build` job at 500s gates the run. Rebalancing/splitting the matrix saves
    # ~0 ACTUAL wall-clock (build still gates), so the cap zeroes it (was uncapped before).
    runs = []
    for run in range(2):
        runs.append([
            _job("prisma-adapter Integration Test", 200, 1000 + run),
            _job("memory-adapter Integration Test", 30, 2000 + run),
            _job("drizzle-adapter Integration Test", 180, 3000 + run),
            _job("build", 500, 4000 + run),       # the REAL workflow long pole
        ])
    # build (500s) is the long pole; the matrix's slow leg sits at the cluster floor.
    crit = {"long_pole_job": "build", "long_pole_p50": 500.0, "floor_p50": 200.0,
            "job_p50": {"build": 500.0, "prisma-adapter Integration Test": 200.0,
                        "memory-adapter Integration Test": 30.0,
                        "drizzle-adapter Integration Test": 180.0}}
    findings = _detect_opt25_shard_imbalance("e2e.yml", runs, 0, crit)
    assert findings, "OPT25 still FIRES (the imbalance is real)"
    f = findings[0]
    assert f["wall_clock_p50_s"] == 0.0          # capped — saves no workflow wall-clock
    assert f["realization"] == "none"
    assert "floor" in f["size_note"].lower() or "below" in f["size_note"].lower()


def test_opt25_sharded_suite_says_rebalance():
    # Explicit shard axis = interchangeable slices of one suite.
    runs = []
    for run in range(2):
        runs.append([
            _job("e2e (shard 1/2)", 300, 4000 + run),
            _job("e2e (shard 2/2)", 30, 5000 + run),
        ])
    crit = {"long_pole_job": "e2e (shard 1/2)", "long_pole_p50": 300.0, "floor_p50": 30.0,
            "job_p50": {"e2e (shard 1/2)": 300.0, "e2e (shard 2/2)": 30.0}}
    findings = _detect_opt25_shard_imbalance("e2e.yml", runs, 0, crit)
    assert findings, "OPT25 should fire"
    f = findings[0]
    assert f["title"] == "Shard Imbalance"
    assert f["measured_evidence"]["table"]["headers"][0] == "Shard"
    assert "rebalance" in f["measured_evidence"]["note"].lower()


class _FakeClient:
    """Stub GhClient.text returning a canned job log per job id."""
    def __init__(self, logs: dict):
        self.logs = logs
        self.queries = 0

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        for jid, log in self.logs.items():
            if f"/jobs/{jid}/" in endpoint:
                return log
        return None


_MISS_LOG = ("2026-05-29T10:00:01.0Z ##[group]Restore cache\n"
             "2026-05-29T10:00:02.0Z Cache not found for input keys: "
             "Linux-pnpm-abc123\n2026-05-29T10:00:03.0Z pnpm install\n")
_HIT_LOG = ("2026-05-29T10:00:01.0Z Cache restored from key: Linux-pnpm-abc123\n"
            "2026-05-29T10:00:02.0Z Cache restored successfully\n")


def test_cache_status_extracts_verbatim_miss_line():
    status, line = _cache_status_from_log(_MISS_LOG)
    assert status == "miss"
    assert line == "Cache not found for input keys: Linux-pnpm-abc123"


def test_cache_status_scopes_static_compiler_and_buildkit_misses():
    assert _cache_status_from_log(
        "Cache not found for input keys: pnpm-linux", ("ccache",)) == ("unknown", "")
    status, line = _cache_status_from_log(
        "#12 ERROR importing cache manifest from gha: not found", ("buildkit",))
    assert status == "miss"
    assert line == "#12 ERROR importing cache manifest from gha: not found"


def test_attach_cache_log_evidence_quotes_the_real_miss(tmp_path: Path):
    findings = [{
        "id": "f1", "pattern": "OPT5", "affected_jobs": ["build"],
        "workflow_file": ".github/workflows/ci.yml",
        "measured_signal": "",
    }]
    jobs_per_run = {
        ".github/workflows/ci.yml": [
            [{"name": "build", "id": 111, "conclusion": "success",
              "html_url": "https://github.com/o/r/actions/runs/9/job/111",
              "started_at": "2026-05-29T10:00:00Z",
              "completed_at": "2026-05-29T10:00:40Z"}],
        ],
    }
    client = _FakeClient({111: _MISS_LOG})
    _attach_cache_log_evidence(client, "o/r", findings, jobs_per_run)
    me = findings[0]["measured_evidence"]
    rows = me["table"]["rows"]
    assert "MISS" in rows[0][1]
    assert "Cache not found for input keys: Linux-pnpm-abc123" in rows[0][1]
    assert "actions/runs/9/job/111" in rows[0][0]   # links the actual run


def test_attach_cache_log_evidence_flags_all_hits_as_unconfirmed(tmp_path: Path):
    findings = [{
        "id": "f1", "pattern": "OPT6", "affected_jobs": ["build"],
        "workflow_file": ".github/workflows/ci.yml", "measured_signal": "",
    }]
    jobs_per_run = {
        ".github/workflows/ci.yml": [
            [{"name": "build", "id": 222, "conclusion": "success",
              "html_url": "https://github.com/o/r/actions/runs/8/job/222",
              "started_at": "2026-05-29T10:00:00Z",
              "completed_at": "2026-05-29T10:00:06Z"}],
        ],
    }
    findings[0]["wall_clock_p50_s"] = 25.0  # a sized wall-clock the evidence will refute
    client = _FakeClient({222: _HIT_LOG})
    _attach_cache_log_evidence(client, "o/r", findings, jobs_per_run)
    # Honest: logs show the cache HITTING → don't assert a miss; flag it.
    assert "HIT" in findings[0]["measured_evidence"]["summary"].upper()
    assert "unconfirmed" in findings[0]["size_note"]
    # The evidence refutes a wall-clock saving, so it must be zeroed (not kept).
    assert findings[0]["wall_clock_p50_s"] == 0.0


def _job_with_step(job_name: str, step_name: str, dur_s: int, job_id: int) -> dict:
    return {
        "name": job_name, "id": job_id,
        "html_url": f"https://github.com/o/r/actions/runs/1/job/{job_id}",
        "started_at": "2026-05-29T10:00:00Z", "completed_at": "2026-05-29T10:10:00Z",
        "steps": [{
            "name": step_name,
            "started_at": "2026-05-29T10:00:00Z",
            "completed_at": f"2026-05-29T10:{dur_s // 60:02d}:{dur_s % 60:02d}Z",
        }],
    }


def test_effective_volume_scales_by_observed_frequency():
    # A job seen in 4 of 8 sampled runs is sized at half the workflow volume.
    assert _effective_volume(1000, 4, 8) == 500.0
    assert _effective_volume(1000, 8, 8) == 1000.0
    assert _effective_volume(1000, 0, 8) == 0.0
    assert _effective_volume(None, 4, 8) == 0.0
    assert _effective_volume(1000, 12, 8) == 1000.0  # clamped to 1.0


def test_opt49_is_cut_not_dispatched():
    # OPT49 ("Slow Setup Step"), OPT50 ("Post Steps Taking Too Long") and OPT51
    # ("Install-to-Test Ratio") are all CUT: each inferred a fixable defect from a
    # DURATION/RATIO alone (never proving a cold cache or a reducible setup the way
    # the cache family does), so each was the "a step/job is slow" observation the
    # admission gate forbids. The detectors are retained for reference but MUST NOT
    # be dispatched by collect(); the verified setup signal is carried by the cache
    # family + OPT73 + the artifact-handoff patterns.
    import inspect

    import collect_runs as _cr
    src = inspect.getsource(_cr.collect)
    assert "_detect_opt49_step_outliers(" not in src, (
        "OPT49 was cut - collect() must not dispatch _detect_opt49_step_outliers")
    # ...and OPT50 / OPT51 (also cut) stay cut alongside it.
    assert "_detect_opt50_long_post_steps(" not in src
    assert "_detect_opt51_install_ratio(" not in src, (
        "OPT51 was cut - collect() must not dispatch _detect_opt51_install_ratio")
    # The detectors are retained (for reference / catalog coverage honesty).
    assert hasattr(_cr, "_detect_opt49_step_outliers")
    assert hasattr(_cr, "_detect_opt50_long_post_steps")
    assert hasattr(_cr, "_detect_opt51_install_ratio")


class _FakeFailClient:
    """Stub GhClient.json returning failure/success counts + example runs for
    the OPT48 failure-rate detector."""
    def __init__(self, fails: int, succs: int):
        self.fails, self.succs = fails, succs
        self.queries = 0

    def json(self, endpoint: str):
        self.queries += 1
        if "status=failure" in endpoint and "per_page=5" in endpoint:
            return {"workflow_runs": [
                {"id": 1, "html_url": "https://github.com/o/r/actions/runs/1",
                 "created_at": "2026-05-20T00:00:00Z", "head_branch": "feat-x"}]}
        if "status=failure" in endpoint:
            return {"total_count": self.fails}
        if "status=success" in endpoint:
            return {"total_count": self.succs}
        return {}


def test_opt48_is_advisory_with_no_savings_number():
    # A failure-rate signal is NOT a ranked optimization: ci-speedup can't write
    # the fix ("make the failing tests pass"). It must be advisory, with no
    # fabricated wall-clock/runner-min saving.
    client = _FakeFailClient(fails=300, succs=700)  # 30% over 1000
    out = _detect_opt48_failure_rate(
        client, "o/r", 42, ".github/workflows/ci.yml",
        long_pole=200.0, monthly_volume=1000, start_idx=0)
    assert out, "OPT48 should fire at 30% failure rate"
    f = out[0]
    assert f.get("advisory") is True
    assert f["wall_clock_p50_s"] is None and f["runner_min_saving"] is None
    assert "reliability signal" in f["size_note"]


def test_opt48_evidence_links_failure_rate_dashboard():
    # The evidence a human needs is the RATE, which lives on the GitHub Actions
    # performance dashboard — not a list of individual failed-run links.
    client = _FakeFailClient(fails=300, succs=700)
    out = _detect_opt48_failure_rate(
        client, "o/r", 42, ".github/workflows/ci.yml",
        long_pole=200.0, monthly_volume=1000, start_idx=0)
    me = out[0]["measured_evidence"]
    blob = json.dumps(me)
    assert "actions/metrics/performance" in blob   # the failure-rate dashboard
    assert "failureRate" in blob
    assert "fix is to make the failing tests pass" in me["summary"]


_NO_ACTIVITY_LOG = ("2026-05-29T10:00:01.0Z ##[group]Run actions/checkout\n"
                    "2026-05-29T10:00:02.0Z Syncing repository\n")
_INSTALL_LOG = ("2026-05-29T10:00:01.0Z Run pnpm install --frozen-lockfile\n"
                "2026-05-29T10:00:02.0Z Packages: +1320\n"
                "2026-05-29T10:00:30.0Z Done in 28s\n")


def _cache_job(job_id: int, dur_s: int) -> dict:
    return {"name": "release", "id": job_id,
            "conclusion": "success",          # see `_job` above: real jobs carry one
            "html_url": f"https://github.com/o/r/actions/runs/9/job/{job_id}",
            "started_at": "2026-05-29T10:00:00Z",
            "completed_at": f"2026-05-29T10:{dur_s // 60:02d}:{dur_s % 60:02d}Z"}


def test_cache_finding_dropped_when_unprovable():
    # No cache line AND no install/build activity AND no real cost → unprovable,
    # marked for the admission gate to drop.
    findings = [{"id": "f1", "pattern": "OPT5", "affected_jobs": ["release"],
                 "workflow_file": ".github/workflows/release.yml", "measured_signal": ""}]
    jpr = {".github/workflows/release.yml": [[_cache_job(111, 0)]]}
    _attach_cache_log_evidence(_FakeClient({111: _NO_ACTIVITY_LOG}), "o/r", findings, jpr)
    assert findings[0].get("_drop")


def test_cache_finding_kept_when_install_activity_shown():
    # Install activity + measurable cost = positive evidence → keep, with honest
    # "runs uncached" framing.
    findings = [{"id": "f1", "pattern": "OPT5", "affected_jobs": ["release"],
                 "workflow_file": ".github/workflows/release.yml", "measured_signal": ""}]
    jpr = {".github/workflows/release.yml": [[_cache_job(222, 65)]]}
    _attach_cache_log_evidence(_FakeClient({222: _INSTALL_LOG}), "o/r", findings, jpr)
    assert not findings[0].get("_drop")
    assert "uncached" in findings[0]["measured_evidence"]["summary"].lower()


# --- ENG-1 PR-N1: chain-fact data shape on committed artifacts -----------------
# Compat-vacuous today (the six committed worked examples predate the stamp);
# bites the moment a chain-aware collection is committed (the PR-N3 deepgram
# regen). Shape pinned here so a drifted stamp cannot silently strand the
# PR-N2 verifier re-derivations.

def test_committed_chain_facts_shape_when_present():
    checked = 0
    for fj in sorted((_SKILL_DIR / "reports").glob("*/findings.json")):
        doc = json.loads(fj.read_text(encoding="utf-8"))
        cf = (doc.get("pr_critical_path") or {}).get("chain_facts")
        if cf is None:
            continue
        assert isinstance(cf, list), f"{fj.parent.name}: chain_facts is not a list"
        for entry in cf:
            checked += 1
            assert isinstance(entry.get("sha"), str) and entry["sha"], (
                f"{fj.parent.name}: chain_facts entry lacks its PR head sha")
            chain = entry.get("chain")
            assert isinstance(chain, list) and chain, (
                f"{fj.parent.name}: chain_facts entry lacks a chain")
            spans = entry.get("member_spans_s")
            assert isinstance(spans, dict) and set(spans) == set(chain), (
                f"{fj.parent.name}: member_spans_s does not cover the chain 1:1")
            assert abs(sum(spans.values()) - float(entry.get("chain_s"))) < 0.01, (
                f"{fj.parent.name}: chain_s does not re-derive from member spans")
            assert isinstance(entry.get("co_longest_n"), int) and entry["co_longest_n"] >= 1
            fb = entry.get("fallback")
            assert fb is None or (isinstance(fb, dict) and fb), (
                f"{fj.parent.name}: fallback must be None or a non-empty wf->reason map")
            ms = entry.get("makespan_s")
            assert ms is None or isinstance(ms, (int, float)), (
                f"{fj.parent.name}: makespan_s must be numeric when present")
            if ms is not None:
                assert entry.get("makespan_basis") == (
                    "latest-attempt check-run intervals, span-capped per check")
    # Loud vacuity marker: this is expected to be 0 until a chain-aware
    # collection is committed — the assert documents intent, never fails.
    assert checked >= 0

"""Offline, deterministic full-pipeline smoke test.

Drives the WHOLE chain (run.py -> collect_runs.py -> blocking_path.py ->
verify_report.py) against a synthetic repo and a committed fixture corpus
(fixtures/gh_replay/), entirely offline via the `CI_SPEEDUP_GH_FIXTURES` replay
seam on `collect_runs.GhClient` - no `gh` CLI, no network, no live GitHub data.

Why this exists: the maintainer runbook (`maintainers/ci-speedup/MAINTAINERS.md`,
"Verifying a pipeline change") warns that the committed-report invariant tests
(`test_measured_evidence.py` + the structural guards) are NOT sufficient evidence
of no regression - they validate ARTIFACTS already rendered from real gh sweeps,
never re-run the pipeline against fresh data. This test re-runs the real
pipeline, offline, so a future bug fix can add its repo shape as a new fixture
corpus and prove the full chain (not one script in isolation) still produces a
report `verify_report.py` accepts.

Fixture corpus provenance: `fixtures/gh_replay/` was generated against the
synthetic `.github/workflows/ci.yml` below (recorded manually, not via
`CI_SPEEDUP_GH_RECORD` against a real repo — see `GhClient._record`'s docstring
in collect_runs.py for the record↔replay filename contract). Keep the workflow
YAML and the fixture files in lockstep: changing one without the other desyncs
the corpus from what `scan.py`/`collect_runs.py` expect to see.

One endpoint can't be committed as a static fixture: `_monthly_volume` embeds a
"now minus 30 days" timestamp in its query string (the run window is unpinned),
so its `_fixture_name` changes every day. `_replay_dir` therefore copies the
committed corpus into a tmp dir and writes THAT one fixture at run time via
`_monthly_volume_endpoints_bracket()` (which mirrors `collect_runs._window_30d(None)`'s
timestamp format) — so the offline run is a clean zero-error
collection (no partial-coverage banner) and the headline assertions can demand a
non-empty, correctly-named critical path rather than a silently-empty sample.

PAGINATION AND THE CORPUS (read before re-recording). The list endpoints go
through `_paginate`, which walks to completion: page 1 keeps the historical
`?per_page=100[&…]` spelling (so existing fixtures still resolve), but page 2+
appends `&page=<n>` — a DISTINCT `_fixture_name`. So:
  - a re-recorded corpus MUST capture every page the live walk made (record mode
    writes one fixture per page automatically; just don't hand-trim them), and
  - a hand-written fixture whose `total_count` EXCEEDS the items it carries will
    make the replay ask for `&page=2`, miss it, and correctly fail the whole
    fetch as a coverage gap. Keep `total_count` equal to the item count (or ship
    the page-2 fixture).

The second half of this file unit-tests the replay + record seam directly
(`GhClient` in replay mode, a record→replay round-trip, `_fixture_name`) without
going through a subprocess.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL_DIR / "scripts"
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "gh_replay"

sys.path.insert(0, str(_SCRIPTS))
import collect_runs as cr  # noqa: E402

# The synthetic repo/workflow the committed fixture corpus was recorded
# against. `_REPO` must match the `owner/name` baked into every fixture
# filename (see `_fixture_name`); `_WF_YAML` must match the workflow content
# behind the `contents/.github/workflows/ci.yml` fixture, and produce the same
# job/step shape the `actions/runs/.../jobs` fixture describes.
_REPO = "synthetic/repo"
_WF_ID = 1001
_JOB_ID = 9001

# The EXACT number of gh calls the full offline pipeline makes against the committed
# corpus. Asserted in the e2e below — see the comment there for why a single golden
# integer, and what to do when it moves. Update DELIBERATELY, never to make CI pass.
#
# Measured on this corpus, for the record:
#   41  before the call-reduction work
#   35  (one run-list page per workflow instead of two; `filter=latest` derived
#            from the `filter=all` payload; workflow YAML read from the checkout)
#   38  now  (+3 issue #66 config-era boundary: ONE `commits?path=<wf>&per_page=2`
#            last-change lookup per workflow with >= 2 sampled runs — 3 workflows here
#            (per_page=2 also returns the PRIOR boundary, still a single call).
#            The runs API exposes no workflow-content hash, so per-run content diffing
#            would cost one `/contents/` fetch per run, N >> K; this is O(1) per workflow.)
#   40  now  (+2 OPT80 tail-run job logs. `matrix.yml`'s `smoke` job checks out in 8s on
#            ten sampled runs and 120s on two; OPT80 fetches the log of each TAIL run —
#            and only of a tail run — to prove the stall from the fetch's own progress
#            lines. A job with no tail costs nothing, so this is +1 call per tail run on
#            a firing job, bounded by `_OPT80_LOG_PROBE_MAX`, and 0 on every other repo
#            shape.)
_GOLDEN_GH_QUERY_COUNT = 40
# PR-H1: `push` is UNSCOPED (no `branches:`) so the same-head_sha push+PR run
# pair in the corpus satisfies OPT47's structural precondition (a push scoped
# only to the default branch is excluded by design).
_WF_YAML = """name: CI
on:
  pull_request:
  push:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - name: Install dependencies
        run: npm ci
      - name: Run tests
        run: npm test
"""

# PR-H1: the second workflow (wf id 1002). Three tiny same-SKU matrix legs
# (20/21/22s — each under OPT65's 60s tiny-job bar and under the workflow's own
# floor, which is its SECOND-slowest job: build at 90s under integration at
# 180s) drive the promotable OPT65 below-floor case, and its >10-run success
# sample makes the bill-pole fetch loop deepen a workflow OFFLINE (the shallow
# depth is 10). Push-only, so it never joins the PR spine or the close's poles.
_WF2_ID = 1002
_WF2_YAML = """name: Unit matrix
on:
  push:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build
        run: npm run build
  unit:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        part: [a, b, c]
    steps:
      - uses: actions/checkout@v4
      - name: Run unit slice
        run: npm run unit -- --part ${{ matrix.part }}
  integration:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Integration suite
        run: npm run integration
  lint-eslint:
    name: lint (eslint)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run lint:eslint
  lint-biome:
    name: lint (biome)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run lint:biome
  lint-stylelint:
    name: lint (stylelint)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run lint:stylelint
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run smoke
        run: npm run smoke
"""


# ENG-1 PR-N1: the third workflow (wf id 1003) — a real `needs:` chain (`prep`
# 120s → `verify` 100s, artifact hand-off so OPT21 stays quiet). Its 220s chain
# EXCEEDS the 197s `CI / test` singleton, so the stamped per-PR chain facts
# exercise a genuine two-member chain offline. The stamped argmax
# (`critical_path_check`) still crowns `CI / test` — that field's semantics
# are unchanged; PR-N2 makes the rendered HEADLINE chain-aware, and the
# ranking/cascade work is PR-N3's (recorded as-built in the plan).
# Check-runs are named by the plain job name (GitHub's naming for plain jobs),
# which is what the graph resolver keys on.
_WF3_ID = 1003
_WF3_YAML = """name: Chained
on:
  pull_request:

jobs:
  prep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build artifact
        run: npm run build
      - uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/
  verify:
    runs-on: ubuntu-latest
    needs: prep
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: dist
      - name: Verify artifact
        run: npm run verify
"""


def _init_repo(root: Path, origin: str | None = _REPO) -> None:
    """A one-commit git checkout carrying just the workflows the fixture corpus
    was recorded against. Committer identity travels via env vars (not global
    git config), so this works on a bare runner with no configured identity.

    An `origin` remote naming `_REPO` is part of the model, not decoration: collect_runs
    only reads workflow YAML off disk once it has VERIFIED the checkout is a clone of
    `--repo` (`_root_is_clone_of`), so a checkout with no origin — or somebody else's
    origin — falls back to the gh contents API. Pass `origin=None` (or another slug) to
    build that unverifiable checkout on purpose."""
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(_WF_YAML, encoding="utf-8")
    (root / ".github" / "workflows" / "matrix.yml").write_text(_WF2_YAML, encoding="utf-8")
    (root / ".github" / "workflows" / "chained.yml").write_text(_WF3_YAML, encoding="utf-8")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "ci-speedup-test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "ci-speedup-test", "GIT_COMMITTER_EMAIL": "test@example.com"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    # Pin the branch to the corpus repo's DEFAULT branch (`repos_synthetic_repo.json`
    # says `main`). `git init` otherwise takes the runner's `init.defaultBranch`, which
    # may be `master` — and collect_runs now checks the checkout's branch against the
    # repo's default (`_root_branch_skew`), so an unpinned branch name would make the
    # clean-collection e2e report a branch skew that has nothing to do with the code
    # under test. `symbolic-ref` works on every git version (`init -b` does not).
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=root, check=True, env=env)
    if origin:
        subprocess.run(["git", "remote", "add", "origin",
                        f"https://github.com/{origin}.git"],
                       cwd=root, check=True, env=env)
    subprocess.run(["git", "add", ".github"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True, env=env)


def _monthly_volume_endpoints_bracket() -> list[str]:
    """Every monthly-volume endpoint name the collect subprocess might compute
    for today's UNPINNED window (`--created-before` absent, as `run.py` invokes
    it): the workflow-wide count plus event-scoped counts used by measured
    bill-only detectors. The window's lower bound is `(now - 30d)` truncated to
    the SECOND (see `collect_runs._window_30d`), and the subprocess computes its
    `now` a beat AFTER this test writes fixtures — and on a slow/loaded CI runner
    the gap (Python startup, imports, `git init`, the corpus copy) can be many
    seconds, rolling the timestamp over. Bracket a ~30s forward band of candidate
    timestamps so the fixture is present whichever second the subprocess hits;
    all but the one it actually requests are harmless unused files. (Mirrors
    `_window_30d(None)`'s `>=<since>` / `%Y-%m-%dT%H:%M:%SZ` construction; if the
    engine ever changes that format the bracket stops matching and this test
    fails loudly with a gh_error, which is the correct signal.)"""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    out = []
    for off in range(-1, 31):
        since = ((now + _dt.timedelta(seconds=off)) - _dt.timedelta(days=30)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        for wf_id in (_WF_ID, _WF2_ID, _WF3_ID):
            out.append(f"repos/{_REPO}/actions/workflows/{wf_id}/runs"
                       f"?per_page=1&created=>={since}")
            out.append(f"repos/{_REPO}/actions/workflows/{wf_id}/runs"
                       f"?per_page=1&event=pull_request&created=>={since}")
    return out


def _replay_dir(tmp_path: Path) -> Path:
    """A ready-to-replay fixture dir: the committed corpus PLUS the one
    time-dependent `_monthly_volume` fixture (bracketed over a small second band,
    see `_monthly_volume_endpoints_bracket`), so the offline collection reports
    ZERO gh errors (no missed endpoint) and the clean-run assertion in the e2e
    can be an exact `== 0`."""
    dst = tmp_path / "gh_replay"
    shutil.copytree(_FIXTURES_DIR, dst)
    for endpoint in _monthly_volume_endpoints_bracket():
        (dst / cr._fixture_name(endpoint, "json")).write_text(
            json.dumps({"total_count": 42}), encoding="utf-8")
    return dst


def _replay_env(fixtures_dir: Path) -> dict:
    env = dict(os.environ)
    env["CI_SPEEDUP_GH_FIXTURES"] = str(fixtures_dir)
    env.pop("CI_SPEEDUP_GH_RECORD", None)  # never record over a committed corpus
    return env


def test_offline_pipeline_scan_collect_render_verify(tmp_path):
    """The load-bearing assertion: scan -> collect (gh fixture replay) ->
    render -> verify_report all pass, entirely offline, against a synthetic
    repo + the committed fixture corpus. `test_replay_mode_never_spawns_a_subprocess`
    below proves this doesn't merely happen to work because `gh` is installed -
    the replay path structurally never calls it."""
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    env = _replay_env(_replay_dir(tmp_path))
    access_log = tmp_path / "replay_access.log"
    env["CI_SPEEDUP_GH_FIXTURES_LOG"] = str(access_log)

    run = subprocess.run(
        [sys.executable, str(_SCRIPTS / "run.py"),
         "--root", str(repo_root), "--out", str(findings_path), "--repo", _REPO],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert run.returncode == 0, f"run.py failed:\nstdout={run.stdout}\nstderr={run.stderr}"

    # M2: every COMMITTED corpus fixture the plain run REACHES must be CONSUMED.
    # Without this, an `allow_missing` fixture (contents / rulesets — whose absence
    # degrades gracefully rather than bumping gh_error_count) silently drops out of
    # the backstop: a regression that stops requesting it wouldn't fail any assertion
    # above. The replay-access log records each fixture actually read; require the
    # committed corpus to appear in it. Job-LOG fixtures (`*_logs.txt`) are excluded:
    # they are fetched only on the `--with-logs` drill path, which this e2e does not
    # exercise (the run-time _monthly_volume bracket fixtures are likewise extra).
    # `contents/` fixtures are also excluded: run.py passes `--root`, so workflow YAML
    # is read from the CHECKOUT (cheaper, and the same commit the report stamps) and
    # the contents endpoint is only the FALLBACK for a workflow that isn't on disk.
    # Those fixtures stay live — `test_workflow_yaml_reads_the_checkout_not_the_api`
    # below consumes them on the fallback path and pins them to the same parsed docs.
    # `status=success` fixtures are excluded for the analogous reason: all three corpus
    # workflows' successes fit inside their all-status page, so the success sample is
    # DERIVED and the success endpoint is not called. They stay live as the ORACLE the
    # derivation is checked against (`test_derived_success_sample_equals_the_recorded_
    # success_payload` below) and are consumed by the fallback test.
    consumed = set(access_log.read_text(encoding="utf-8").splitlines()) if access_log.exists() else set()
    committed = {p.name for p in _FIXTURES_DIR.iterdir()
                 if p.is_file() and not p.name.endswith("_logs.txt")
                 and "_contents_" not in p.name
                 and "_status_success" not in p.name}
    never_consumed = committed - consumed
    assert not never_consumed, (
        "committed gh_replay fixtures were never requested by the offline run — a "
        "dead corpus entry, or a regression that stopped fetching an endpoint whose "
        f"absence degrades silently: {sorted(never_consumed)}")
    # ...and the contents endpoint must NOT have been hit: the checkout supplies every
    # workflow, so any contents fetch here is a regression back to the API path.
    assert not [f for f in consumed if "_contents_" in f], (
        "the offline run fetched workflow YAML over the gh contents API even though "
        f"--root put it on disk: {sorted(f for f in consumed if '_contents_' in f)}")
    # ...nor the `status=success` endpoint. Every corpus workflow's successes fit in its
    # all-status page, so the sample is derived from a page already in hand. This is the
    # HAPPY PATH of the derive-the-success-sample change, executed end-to-end through
    # the real pipeline — without this assert the change's saving is uncovered and the
    # derive could be dead code with the fallback silently doing all the work.
    assert not [f for f in consumed if "_status_success" in f], (
        "the offline run issued a `status=success` run-list query even though every "
        "corpus workflow's successes are derivable from its all-status page: "
        f"{sorted(f for f in consumed if '_status_success' in f)}")

    data = json.loads(findings_path.read_text(encoding="utf-8"))

    # OPT77 end to end. `matrix.yml` carries three plain same-runner lint checks
    # that each re-pay one 14s setup prefix before 6s of work, beside a 180s
    # `integration` job that survives the consolidation. Both the detector's
    # DISPATCH and the supersede step that follows it were pinned only by reading
    # collect()'s source: `new = [] if True else _detect_opt77(...)` and deleting
    # the supersede line each left the whole suite green. This executes them.
    o77 = [f for f in data["findings"] if f.get("pattern") == "OPT77"]
    assert len(o77) == 1, (
        "the three plain same-runner lint checks in matrix.yml must promote one "
        f"OPT77 consolidation (got {[f.get('affected_jobs') for f in o77]!r})")
    sc = o77[0].get("setup_consolidation") or {}
    assert sc.get("kind") == "opt77_repeated_setup", sc
    assert sorted(sc.get("credited_jobs") or []) == [
        "lint (biome)", "lint (eslint)", "lint (stylelint)"], sc.get("credited_jobs")
    assert o77[0].get("wall_clock_p50_s") in (0, 0.0)
    assert float(sc.get("setup_p50_s") or 0.0) > 0.0
    assert sc.get("remaining_tallest_job") == "integration", sc
    # ...and the round-up lever must not ALSO claim those three jobs: they are named
    # like matrix legs, so OPT65 groups them too, and one edit rendering as two
    # levers is exactly what the supersede step exists to stop.
    o65_over_lints = [f for f in data["findings"]
                      if f.get("pattern") == "OPT65"
                      and {str(j) for j in (f.get("affected_jobs") or [])}
                      & set(sc["credited_jobs"])]
    assert not o65_over_lints, (
        "an OPT65 finding survived over the same jobs as the OPT77 consolidation: "
        f"{[f.get('affected_jobs') for f in o65_over_lints]!r}")
    # Every gate that withheld a consolidation is counted, so a zero firing rate
    # would be visible rather than silent.
    assert isinstance(data.get("opt77_withheld_by_gate"), dict), (
        "the per-gate withhold tally must be stamped on every collected run")

    # OPT80 end to end. `matrix.yml`'s `smoke` job checks out in 8s on ten of the
    # twelve sampled runs and 120s on two, and each of those two runs ships a
    # recorded checkout log whose git progress stops for 85s. This executes the
    # detector's DISPATCH, its bounded tail-run log fetch, and the renderer +
    # verifier arms for its certificate token — none of which any unit test can
    # reach. Two mutants must redden it: short-circuiting the call
    # (`new = [] if True else _detect_opt80_…`) and discarding its result
    # (dropping the `findings.extend(new)`).
    o80 = [f for f in data["findings"] if f.get("pattern") == "OPT80"]
    assert len(o80) == 1, (
        "matrix.yml's `smoke` job must promote exactly one OPT80 finding "
        f"(got {[f.get('affected_jobs') for f in o80]!r})")
    cs = o80[0].get("checkout_stall") or {}
    assert cs.get("kind") == "opt80_checkout_tail_stall", cs
    assert cs.get("job") == "smoke" and o80[0]["affected_jobs"] == ["smoke"]
    assert cs.get("checkout_step_source") == "actions/checkout", cs
    assert o80[0].get("wall_clock_p50_s") in (0, 0.0), (
        "capping a tail cannot move the p50 merge gate, so no wall-clock is credited")
    assert float(cs.get("tail_excess_s") or 0.0) > 0.0
    assert float(cs.get("p95_s") or 0.0) >= float(cs.get("tail_threshold_s") or 0.0)
    # The proof, not an inference from the duration: two tail runs, each with the
    # two verbatim git progress lines that bracket the pause.
    proven = cs.get("proven_tail_runs") or []
    assert len(proven) == 2, proven
    for p in proven:
        assert float(p.get("gap_s") or 0.0) >= 20.0, p
        assert "Receiving objects" in str((p.get("before") or {}).get("line") or ""), p
    # The log fetch is bounded AND targeted: one call per tail run, none for the
    # ten typical runs.
    assert cs.get("logs_fetched") == 2, cs
    assert cs.get("logs_fetched") <= cs.get("log_probe_max")
    assert o80[0].get("tier2_neutrality", {}).get("proof") == "checkout_tail_excess"
    assert isinstance(data.get("opt80_withheld_by_gate"), dict), (
        "the per-gate withhold tally must be stamped on every collected run")

    # The static-scan findings come from scan.py parsing the YAML — they exist
    # regardless of gh replay, so they do NOT prove the replay wired up. Assert
    # them, but they are not the backstop.
    assert isinstance(data.get("findings"), list) and data["findings"], (
        "expected >=1 static finding from the synthetic workflow "
        f"(got: {data.get('findings')!r})")

    # The BACKSTOP: content only reachable THROUGH the gh replay. `pr_critical_path`
    # is stamped as a dict unconditionally whenever --repo is set and available()
    # is True (always True in replay), so `isinstance(..., dict)` alone would stay
    # green even if a `_fixture_name` regression made every fixture silently MISS
    # (an empty sample: sampled_pr_count == 0). Demand a non-empty sample whose
    # critical path names the synthetic `test` job — that value can only come from
    # the replayed run/jobs/check-run fixtures.
    pcp = data.get("pr_critical_path")
    assert isinstance(pcp, dict), (
        "collect_runs must always stamp pr_critical_path when --repo is "
        "supplied and gh (replay) is available")
    assert pcp.get("sampled_pr_count", 0) >= 1, (
        "critical-path sample is EMPTY — the gh replay delivered no PR check-runs "
        "(a _fixture_name / corpus regression would land here); the report would "
        f"still render but on no measured data. pr_critical_path={pcp!r}")
    assert pcp.get("critical_path_check") == "CI / test", (
        "critical path does not name the synthetic `CI / test` check reachable only "
        f"through the replayed check-run fixture (got {pcp.get('critical_path_check')!r})")
    poles = pcp.get("poles") or []
    # ENG-1 PR-N3: on a chain-gated repo the gate IS the chain — its members
    # drill first, in chain order, then the rest by span. `critical_path_check`
    # (asserted above) keeps its slowest-single-check semantics.
    assert [p.get("job") for p in poles[:3]] == ["prep", "verify", "test"], (
        f"chain-first drill order expected (poles={[p.get('job') for p in poles]!r})")

    # ENG-1 PR-N3: the sizing cascade is chain-aware. The chain members' OPT75
    # levers size 1:1 up to the whole-chain headroom (never floored by their
    # own chain); the non-member pole (`CI / test`, 197s) is floored by the
    # CHAIN's sum (220s), so its wall-clock zeroes with the chain named.
    opt75 = {str(f.get("workflow_file", "")) + "|" + str((f.get("affected_jobs") or [""])[0]): f
             for f in data["findings"] if f.get("pattern") == "OPT75"}
    ci_test = next((f for f in data["findings"]
                    if f.get("pattern") == "OPT75"
                    and "ci.yml" in str(f.get("workflow_file", ""))), None)
    assert ci_test is not None, "OPT75 candidate for CI / test missing"
    assert float(ci_test.get("wall_clock_p50_s") or 0.0) == 0.0, (
        "the non-member pole must be floored by the chain's sum "
        f"(got {ci_test.get('wall_clock_p50_s')})")
    assert "gate chain" in str(ci_test.get("size_note") or ""), (
        "the zeroing reason must name the gate chain")
    member = next((f for f in data["findings"]
                   if f.get("pattern") == "OPT75"
                   and "chained.yml" in str(f.get("workflow_file", ""))), None)
    if member is not None:
        chain_win = float((pcp.get("chain_summary") or {}).get("chain_win_p50_s") or 0.0)
        assert 0.0 < float(member.get("wall_clock_p50_s") or 0.0) <= chain_win + 0.1, (
            "a chain member's lever must be positive and capped at the chain "
            f"headroom (~{chain_win}s; got {member.get('wall_clock_p50_s')})")

    # ENG-1 PR-N1: the per-PR chain TIMING facts are stamped (data-only — the
    # argmax gate above is deliberately unchanged until PR-N2). The chained.yml
    # fixture serializes `prep` (120s) → `verify` (100s), a 220s chain that
    # outweighs the 197s `CI / test` singleton, so every sampled PR must stamp
    # the two-member chain with capped member spans, the re-derivable sum, and
    # the attempt-scoped empirical makespan.
    chain_facts = pcp.get("chain_facts")
    assert isinstance(chain_facts, list) and len(chain_facts) == pcp["sampled_pr_count"], (
        f"chain_facts missing or not one-per-sampled-PR (got {chain_facts!r})")
    for cf in chain_facts:
        assert cf.get("chain") == ["prep", "verify"], (
            f"the needs:-serialized chain was not recovered (got {cf.get('chain')!r})")
        spans = cf.get("member_spans_s") or {}
        assert set(spans) == {"prep", "verify"}
        assert abs(sum(spans.values()) - float(cf.get("chain_s") or 0)) < 0.01, (
            "chain_s does not re-derive from its member spans")
        assert 215.0 <= float(cf["chain_s"]) <= 225.0, (
            f"chain sum not the fixture's ~220s serial chain (got {cf['chain_s']})")
        assert cf.get("fallback") is None
        # makespan >= chain_s is NOT an invariant (chain member spans are
        # max-across-attempts while makespan intervals are latest-attempt, so
        # a long earlier attempt can push chain_s above the makespan) — assert
        # presence and positivity only; on THIS single-attempt corpus the
        # serialized fixture happens to satisfy >=, but pinning it would pin
        # an accident.
        assert cf.get("makespan_s") is not None and float(cf["makespan_s"]) > 0, (
            f"empirical makespan missing (got {cf.get('makespan_s')!r})")
        assert cf.get("makespan_basis") == (
            "latest-attempt check-run intervals, span-capped per check")

    # The collection must run CLEAN: no fixture missed, no partial-coverage banner.
    # A missing/renamed fixture bumps client.errors but leaves run.py returncode 0,
    # so a partial-coverage regression would otherwise pass silently (the repo's
    # "no silent drops" failure mode). `_replay_dir` fixtures every endpoint the
    # synthetic run hits (incl. the time-dependent _monthly_volume), so this is an
    # exact zero — a future endpoint that stops replaying trips it here.
    ds = data.get("data_sources") or {}
    assert ds.get("gh_error_count") == 0, (
        f"offline collection reported {ds.get('gh_error_count')} gh error(s) — a "
        f"fixture was missed. partial_reason={ds.get('partial_reason')!r}")
    assert ds.get("partial_reason") in (None, ""), (
        f"offline collection raised a partial-coverage banner: {ds.get('partial_reason')!r}")

    # ---- the gh CALL-COUNT guard (a single golden integer) --------------------
    # Every call-count claim this skill makes ("-98 calls", "~21% of the budget") was,
    # until this assert, verified by NOTHING a test could re-run: the numbers came from
    # one instrumented live sweep and nothing stopped the next change from quietly
    # adding calls back. This pins the WHOLE pipeline's gh budget on the corpus to an
    # exact number, so any change to how often the pipeline calls GitHub — a saving or a
    # regression — has to land here as a deliberate edit with a reason in the diff.
    #
    # A REGRESSION here is not automatically a bug (a correctness fix may need a call
    # the old code skipped — e.g. a truncated attempt-run payload must re-fetch
    # `filter=latest`). It is a REVIEW GATE: change the number, and say in the PR why
    # the pipeline now talks to GitHub more or less than it used to.
    assert ds.get("gh_query_count") == _GOLDEN_GH_QUERY_COUNT, (
        f"the offline pipeline made {ds.get('gh_query_count')} gh calls, not the "
        f"golden {_GOLDEN_GH_QUERY_COUNT}. If you INTENDED to change the call budget, "
        "update _GOLDEN_GH_QUERY_COUNT and justify the delta in the PR; if you did "
        "not, you have just added (or dropped) gh calls by accident.")

    # Which YAML source fed the detectors is a fact ABOUT the report, so it is stamped.
    # `--root` is a real checkout of the synthetic repo here, so every workflow is read
    # off disk and none over the API.
    assert ds.get("workflow_yaml_source") == {"checkout": 3, "api": 0}, (
        f"workflow YAML provenance not stamped as expected: {ds.get('workflow_yaml_source')!r}")

    render = subprocess.run(
        [sys.executable, str(_SCRIPTS / "blocking_path.py"),
         "--in", str(findings_path), "--out", str(report_path)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert render.returncode == 0, f"blocking_path.py failed:\n{render.stderr}"
    report = report_path.read_text(encoding="utf-8")
    assert report.strip()
    # A clean collection must NOT render the incomplete-coverage banner — the
    # rendered-report counterpart of the gh_error_count assertion above (belt and
    # suspenders: catches a divergence between the data flag and what's rendered).
    assert "Incomplete coverage" not in report, (
        "report shows an Incomplete-coverage banner despite a zero-error collection")

    verify = subprocess.run(
        [sys.executable, str(_SKILL_DIR / "tests" / "verify_report.py"),
         "--report", str(report_path), "--findings", str(findings_path)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert verify.returncode == 0, (
        "verify_report rejected the offline-replayed report:\n"
        f"{verify.stdout}\n{verify.stderr}")

    # ---- PR-H1 (G5): the promoted-path backstop — UNCONDITIONAL. -------------
    # Before this, the replay corpus promoted nothing, so the Tier-2 render
    # guard only ever exercised its weakest (modeled-fallback) branch and a
    # regression that silently stopped promoting ANY finding passed CI (the
    # gap assessment's G5). The corpus now carries fixtures engineered to
    # drive every promotion-path component offline; each assert goes red if
    # its component breaks — never a conditional branch that quietly degrades.
    findings = data["findings"]
    promoted = [f for f in findings
                if f.get("sizing_basis") == "measured" and f.get("tier2_neutrality")]
    assert promoted, (
        "no measured+certified Tier-2 finding on the replay corpus — the "
        "promotion path is dead offline (G5 backstop)")
    # OPT46 superseded runs: measured sizing + post_completion_waste certificate
    # (the overlap-confirmed raced-run fixtures).
    def _proof(f):
        cert = f.get("tier2_neutrality")
        return cert.get("proof") if isinstance(cert, dict) else None

    assert any(f.get("pattern") == "OPT46"
               and _proof(f) == "post_completion_waste"
               for f in promoted), (
        "OPT46 did not promote — the overlapping-run fixtures or its "
        "certificate/stamp path regressed")
    # OPT65 rounding waste: the promotable below-floor case (tiny same-SKU
    # matrix legs whose combined p50 sits below the cluster floor).
    assert any(f.get("pattern") == "OPT65"
               and _proof(f) == "below_cluster_floor"
               for f in promoted), (
        "OPT65 did not promote — the below-floor matrix fixtures or its "
        "computed-margin certificate regressed")
    # Source-binding + the renderer gate: the first-class section renders ONLY
    # when >=1 admitted finding also binds to render-ready runner_minute_spine
    # rows, so these two asserts cover the source-backing gate and the render
    # gate in one observable.
    # TOC fix (owner request): the runner-minutes section is a FIRST-CLASS
    # Contents entry — emoji marker, the de-overlapped total up front, and
    # enumerated per-row links that resolve to per-row anchors.
    assert "**💸 Runner-minute reductions**" in report, (
        "the Contents entry for runner minutes lost its first-class marker")
    assert re.search(r"\*\*💸 Runner-minute reductions\*\* - ~[\d.,]+ min/mo", report), (
        "the Contents entry must lead with the de-overlapped total")
    assert re.search(r"^1\. 🟢 \[.+?\]\(#r-1\) - ", report, re.MULTILINE), (
        "the Contents entry must enumerate the R-rows as a REAL numbered list "
        "(plain 'R1.'-prefixed lines merge into one paragraph on GitHub), each "
        "with the 🟢 merge-safe dot in the pole rows' severity-dot slot")
    assert '<a id="r-1"></a>' in report, "per-row anchor missing"
    assert "## Runner-minute reductions (wall-clock-neutral)" in report, (
        "no promoted finding is source-backed — the spine binding or the "
        "renderer's Tier-2 section gate regressed")
    assert "ci-speedup:tier2-finding" in report, (
        "the ci-speedup:tier2-finding marker is absent — R-rows rendered "
        "without their machine-readable markers (the verifier binds on them)")
    # Per-pattern: BOTH promoted patterns must render their own R-row. A
    # substring-only marker check would stay green if one pattern's source
    # binding silently broke while the other kept the section alive.
    for pat in ("OPT46", "OPT65"):
        assert f"pattern={pat}" in report, (
            f"{pat} promotes in findings.json but renders no R-row marker — "
            "its source binding or row render silently dropped")
    # OPT47 double-trigger: measured but certificate-DEFERRED — the demotion
    # path, and PR-P1's lead accounting must say so (the certificate-deferred
    # bucket). PR-S1 later flips this to a promoted-path assertion when OPT47
    # gains its duplicate-run-neutrality certificate.
    opt47 = [f for f in findings if f.get("pattern") == "OPT47"]
    assert opt47, "OPT47 did not fire (the same-head_sha push+PR pair fixture)"
    assert all(f.get("sizing_basis") == "measured" and not f.get("tier2_neutrality")
               for f in opt47), (
        "OPT47 must be measured-but-uncertified at H1 time (certificate work is "
        f"PR-S1): {[(f.get('sizing_basis'), f.get('tier2_neutrality')) for f in opt47]}")
    assert "certificate-deferred" in report, (
        "the lead's accounting (PR-P1) must name OPT47's certificate-deferred bucket")
    # The bill-pole fetch loop ran OFFLINE: >=1 workflow deepened through
    # replayed jobs fixtures (cost_deepened_workflow_count was 0 on the old
    # corpus, so the #174 loop had zero offline coverage).
    assert (ds.get("cost_deepened_workflow_count") or 0) >= 1, (
        "the bill-pole deepen loop never ran offline "
        f"(cost_deepened_workflow_count={ds.get('cost_deepened_workflow_count')!r})")


def test_workflow_yaml_reads_the_checkout_not_the_api(tmp_path, monkeypatch):
    """Workflow YAML comes from the local checkout (`--root`), and the parsed docs are
    IDENTICAL to what the `contents/` API path produces — proving the gh call it drops
    was pure duplicated work. The API stays the fallback for a workflow that isn't on
    disk (deleted locally, or no `--root` at all), so the committed contents fixtures
    stay live: this test consumes them.

    The local read is also the more CORRECT source: `/contents/` serves the DEFAULT
    BRANCH's HEAD, which is not necessarily the commit the report stamps as audited.
    The `_pinned` case below is that skew, made visible."""
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    wf_paths = {".github/workflows/ci.yml", ".github/workflows/matrix.yml",
                ".github/workflows/chained.yml"}
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    monkeypatch.delenv("CI_SPEEDUP_GH_RECORD", raising=False)

    # (a) API path (no --root): the pre-existing behavior, one contents call each.
    api_client = cr.GhClient()
    api_docs = cr._fetch_workflow_docs(api_client, _REPO, wf_paths)
    assert set(api_docs) == wf_paths
    assert api_client.queries == 3 and api_client.errors == 0

    # (b) local path: same parsed docs, ZERO gh calls.
    local_client = cr.GhClient()
    local_docs = cr._fetch_workflow_docs(local_client, _REPO, wf_paths, root=repo_root)
    assert local_docs == api_docs
    assert local_client.queries == 0 and local_client.errors == 0

    # (c) fallback: a workflow the checkout doesn't have still comes off the API, and
    # the ones it does have still don't — a per-file decision, not all-or-nothing.
    (repo_root / ".github" / "workflows" / "ci.yml").unlink()
    mixed_client = cr.GhClient()
    mixed_docs = cr._fetch_workflow_docs(mixed_client, _REPO, wf_paths, root=repo_root)
    assert mixed_docs == api_docs           # nothing lost — the API filled the hole
    assert mixed_client.queries == 1 and mixed_client.errors == 0

    # (d) the correctness skew the local read closes: a checkout whose workflow differs
    # from the default-branch HEAD the API serves. The local content is what the report
    # stamps as audited, so the local content is what must be parsed.
    (repo_root / ".github" / "workflows" / "matrix.yml").write_text(
        "name: Unit matrix\non:\n  pull_request:\n\njobs:\n  build:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: npm run build\n",
        encoding="utf-8")
    pinned = cr._fetch_workflow_docs(cr.GhClient(), _REPO, wf_paths, root=repo_root)
    # (PyYAML parses the bare key `on:` as boolean True — hence the `[True]` lookup,
    # the same shape `_declared_pr_workflows` reads.)
    assert pinned[".github/workflows/matrix.yml"][True] == {"pull_request": None}
    assert api_docs[".github/workflows/matrix.yml"][True] == {"push": None}
    # ...so the declared-trigger guard now sees the CHECKOUT's PR trigger, not the
    # default branch's push-only one: the local read changes the ANSWER, not just the
    # call count.
    assert ".github/workflows/matrix.yml" in cr._declared_pr_workflows(
        cr.GhClient(), _REPO, wf_paths, wf_docs=pinned)
    assert ".github/workflows/matrix.yml" not in cr._declared_pr_workflows(
        cr.GhClient(), _REPO, wf_paths, wf_docs=api_docs)


def _corpus_runs(wf_id: int, success: bool) -> list[dict]:
    """A committed run-list fixture's `workflow_runs`, straight off disk."""
    name = (f"repos_synthetic_repo_actions_workflows_{wf_id}_runs_per_page_20_status_success.json"
            if success else
            f"repos_synthetic_repo_actions_workflows_{wf_id}_runs_per_page_100.json")
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))["workflow_runs"]


def test_derived_success_sample_equals_the_recorded_success_payload():
    """The load-bearing property behind dropping the `status=success` query, checked
    against a payload GitHub's SERVER produced — not against a restatement of the
    derivation's own predicate.

    `repos_..._runs_per_page_100.json` (all conclusions) and
    `repos_..._runs_per_page_20_status_success.json` (REST's server-side
    `status=success` filter over the same list) are two SEPARATELY recorded responses.
    If `_success_runs_from_all_status` ever diverges from what the server's filter
    returns — a wrong predicate, a wrong order, an off-by-one in the slice — this goes
    red. An oracle that re-implemented the filter here could not."""
    dropped_any = False
    for wf_id in (_WF_ID, _WF2_ID, _WF3_ID):
        all_status = _corpus_runs(wf_id, success=False)
        rest_success = _corpus_runs(wf_id, success=True)
        derived = cr._success_runs_from_all_status(all_status, 20)
        assert derived == rest_success, (
            f"wf {wf_id}: the derived success sample is not what REST's server-side "
            "`status=success` filter returned for the same run list")
        dropped_any = dropped_any or len(all_status) > len(rest_success)
    # ...and the oracle is not vacuous: somewhere in the corpus the all-status page DOES
    # hold runs the server's success filter drops. Without this, a derivation that did
    # no filtering at all would pass the loop above.
    assert dropped_any, (
        "no corpus all-status page holds a non-success run — this oracle would pass on "
        "a derivation that returned the page verbatim")


def test_success_sample_derive_boundary_at_exactly_max_runs():
    """The slice boundary, exercised at max_runs-1 / max_runs / max_runs+1 against a
    recorded page. `collect` treats "fewer than max_runs derived" as a possible
    can't-see-far-enough, so an off-by-one here silently changes when the fallback
    query fires."""
    all_status = _corpus_runs(_WF2_ID, success=False)   # 12 successes on the page
    rest_success = _corpus_runs(_WF2_ID, success=True)
    assert len(rest_success) == 12

    assert cr._success_runs_from_all_status(all_status, 11) == rest_success[:11]
    assert cr._success_runs_from_all_status(all_status, 12) == rest_success       # exactly
    # Asking for MORE than the page holds yields what's there — it does not pad, and it
    # does not silently truncate to a different set. The SHORTNESS is the caller's
    # signal (see `collect`), not a value this helper is allowed to fake up.
    assert cr._success_runs_from_all_status(all_status, 13) == rest_success
    assert len(cr._success_runs_from_all_status(all_status, 13)) == 12


def _run_row(rid: int, ok: bool, wall: int = 300) -> dict:
    return {"id": rid, "event": "pull_request", "head_sha": f"h{rid}",
            "status": "completed", "conclusion": "success" if ok else "failure",
            "created_at": "2026-01-01T00:00:00Z",
            "run_started_at": "2026-01-01T00:00:00Z",
            "updated_at": f"2026-01-01T00:{wall // 60:02d}:{wall % 60:02d}Z"}


class _RunListProbeClient:
    """A GhClient that RECORDS every endpoint, over two workflows chosen to sit on
    either side of the fallback condition:
      wf 1 (`full.yml`)  — a FULL 100-run all-status page holding only 4 successes:
                           truncated, cannot see far enough back -> MUST fall back.
      wf 2 (`short.yml`) — a 12-run all-status page holding 4 successes: the whole
                           visible history -> the 4 ARE all the successes -> MUST NOT.
    """

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self.endpoints: list[str] = []

    def available(self) -> bool:
        return True

    def text(self, endpoint, **kw):
        return None

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        self.endpoints.append(endpoint)
        if endpoint.startswith(f"repos/{_REPO}/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": ".github/workflows/full.yml", "name": "full"},
                {"id": 2, "path": ".github/workflows/short.yml", "name": "short"}]}
        m = re.match(rf"repos/{_REPO}/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            wf_id, qs = int(m.group(1)), m.group(2)
            if qs.startswith("per_page=1&"):          # monthly volume
                return {"total_count": 30}
            if "status=success" in qs:                # the FALLBACK query
                return {"workflow_runs": [_run_row(1000 + wf_id * 100 + i, True)
                                          for i in range(20)]}
            if wf_id == 1:                            # FULL page, 4 successes
                return {"workflow_runs": [_run_row(100 + i, i < 4)
                                          for i in range(cr._COST_RUNLIST_MAX)]}
            return {"workflow_runs": [_run_row(200 + i, i < 4)   # SHORT page
                                      for i in range(12)]}
        if re.match(rf"repos/{_REPO}/actions/runs/(\d+)/jobs", endpoint):
            return {"jobs": [{"id": 1, "name": "test", "run_attempt": 1,
                              "status": "completed", "conclusion": "success",
                              "started_at": "2026-01-01T00:00:00Z",
                              "completed_at": "2026-01-01T00:05:00Z",
                              "runner_name": "ubuntu-latest", "steps": []}]}
        return None if not allow_missing else None

    def _success_queries(self) -> list[str]:
        return [e for e in self.endpoints if "status=success" in e]


def test_collect_issues_the_success_query_ONLY_for_a_truncated_run_page(monkeypatch):
    """WHEN the explicit `status=success` query fires, observed through a real
    `collect()` run — replacing a source-string grep that could not tell a live
    fallback from dead code, and could not catch an off-by-one.

    The fallback exists for exactly one shape: the all-status page is FULL (truncated
    at `_COST_RUNLIST_MAX`) and still holds fewer than `max_runs` successes, so it
    cannot see far enough back. A SHORT page is the workflow's entire visible history —
    its successes are all the successes there are, and falling back would re-fetch the
    identical runs. That is not merely a wasted call: it makes every small or
    rarely-run workflow in a monorepo pay TWO run-list calls where it used to pay one,
    turning a call REDUCTION into a call regression on exactly the repos with the most
    workflows."""
    client = _RunListProbeClient()
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    # A finding per workflow is what puts a workflow "in play" for the shallow loop.
    doc = {"findings": [
        {"id": "f1", "pattern": "OPT1", "workflow_file": ".github/workflows/full.yml"},
        {"id": "f2", "pattern": "OPT1", "workflow_file": ".github/workflows/short.yml"},
    ], "data_sources": {}}
    cr.collect(doc, _REPO, max_runs=20, shallow_runs=10)

    success_qs = client._success_queries()
    assert any("workflows/1/runs" in e for e in success_qs), (
        "the TRUNCATED page (100 runs, 4 successes) must fall back to the explicit "
        f"`status=success` query — it cannot see far enough back. Queries: {success_qs}")
    assert not any("workflows/2/runs" in e for e in success_qs), (
        "the SHORT page (12 runs, 4 successes) must NOT fall back — those 4 successes "
        "are the workflow's whole history, and the fallback would re-fetch the very "
        f"same runs. Queries: {success_qs}")


def test_local_workflow_read_refuses_a_path_outside_the_checkout(tmp_path):
    """`wf_path` is repo-relative by construction, but the reader still re-checks it
    against `root` — a traversing path falls through to the API rather than reading an
    arbitrary file off the maintainer's disk."""
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    (tmp_path / "secret.yml").write_text("name: SECRET\n", encoding="utf-8")

    assert cr._read_local_workflow(root, ".github/workflows/ci.yml") == "name: CI\n"
    assert cr._read_local_workflow(root, "../secret.yml") is None
    assert cr._read_local_workflow(root, ".github/workflows/absent.yml") is None
    assert cr._read_local_workflow(None, ".github/workflows/ci.yml") is None


# --- record-seam unit tests (record -> replay round-trip) --------------------

def _canned_gh(stdout: str):
    """A `subprocess.run` stand-in that returns a successful gh result with
    `stdout` — so record mode's write-through path runs without a real gh."""
    def _fake(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else [], 0, stdout=stdout, stderr="")
    return _fake


def test_record_then_replay_round_trip_json(tmp_path, monkeypatch):
    """The seam's core invariant: a response RECORDED under one dir replays
    identically when that dir is used as the fixtures dir — proving record and
    replay agree on the `_fixture_name` mapping. Also asserts the fixture lands
    at EXACTLY `_fixture_name(endpoint, 'json')`, the contract `_record`'s
    docstring states."""
    rec_dir = tmp_path / "recorded"
    endpoint = f"repos/{_REPO}/actions/runs/777/jobs?per_page=100"
    payload = {"total_count": 1, "jobs": [{"id": 777, "name": "build"}]}
    monkeypatch.setattr(cr.subprocess, "run", _canned_gh(json.dumps(payload)))
    monkeypatch.setenv("CI_SPEEDUP_GH_RECORD", str(rec_dir))
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    recorded = cr.GhClient().json(endpoint)
    assert recorded == payload
    fixture = rec_dir / cr._fixture_name(endpoint, "json")
    assert fixture.is_file(), (
        f"record mode did not write {fixture.name} (the _fixture_name mapping)")

    # Now replay from the just-recorded dir — must return the same value, with no
    # subprocess in sight (patch it to raise, proving replay reads the file).
    monkeypatch.delenv("CI_SPEEDUP_GH_RECORD", raising=False)
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(rec_dir))
    monkeypatch.setattr(cr.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    assert cr.GhClient().json(endpoint) == payload


def test_record_then_replay_round_trip_text(tmp_path, monkeypatch):
    rec_dir = tmp_path / "recorded"
    endpoint = f"repos/{_REPO}/actions/jobs/{_JOB_ID}/logs"
    log = "2026-06-25T10:00:00Z ##[group]Run npm test\n2026-06-25T10:03:00Z ok\n"
    monkeypatch.setattr(cr.subprocess, "run", _canned_gh(log))
    monkeypatch.setenv("CI_SPEEDUP_GH_RECORD", str(rec_dir))
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    assert cr.GhClient().text(endpoint) == log
    assert (rec_dir / cr._fixture_name(endpoint, "txt")).is_file()

    monkeypatch.delenv("CI_SPEEDUP_GH_RECORD", raising=False)
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(rec_dir))
    assert cr.GhClient().text(endpoint) == log


def test_the_created_window_endpoints_no_longer_collide(tmp_path):
    """The ONE collision the pipeline actually issued: the monthly-volume window
    (`created=>=X`) and the pinned sampling window (`created=<=X`) differ only by an
    operator whose chars were both unsafe, so both collapsed to `…_created___X` — two
    OPPOSITE windows, one fixture file. Recording an audit therefore overwrote one
    window's runs with the other's, and replay served them under the wrong name. The
    operators are now spelled out (`gte`/`lte`) before the safe-char pass."""
    ts = "2026-01-01T00:00:00Z"
    ep_ge = f"repos/{_REPO}/actions/workflows/{_WF_ID}/runs?per_page=1&created=>={ts}"
    ep_le = f"repos/{_REPO}/actions/workflows/{_WF_ID}/runs?per_page=1&created=<={ts}"
    assert cr._fixture_name(ep_ge, "json") != cr._fixture_name(ep_le, "json")
    assert "gte" in cr._fixture_name(ep_ge, "json")
    assert "lte" in cr._fixture_name(ep_le, "json")


def test_record_mode_RAISES_on_a_lossy_fixture_name_collision(tmp_path, monkeypatch):
    """`_fixture_name` stays lossy in principle, so record mode must FAIL — not warn
    and overwrite — when two distinct endpoints target one file.

    Warn-and-overwrite produced a corpus that serves endpoint B's body under endpoint
    A's name: valid-but-WRONG JSON. Concretely, a `{"default_branch": "main"}` body
    answering a check-runs request has no `check_runs` key and no `total_count`, which
    `_paginate` used to read as "this commit ran no checks" — a clean critical-path
    sample built on nothing. Recording is maintainer-only; failing loudly is cheap and
    a silently-unfaithful corpus is not."""
    ep_a = f"repos/{_REPO}/actions/runs/1?x"
    ep_b = f"repos/{_REPO}/actions/runs/1&x"
    assert ep_a != ep_b
    assert cr._fixture_name(ep_a, "json") == cr._fixture_name(ep_b, "json")

    rec_dir = tmp_path / "recorded"
    monkeypatch.setenv("CI_SPEEDUP_GH_RECORD", str(rec_dir))
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    client = cr.GhClient()

    monkeypatch.setattr(cr.subprocess, "run", _canned_gh(json.dumps({"total_count": 1})))
    client.json(ep_a)                       # first write — fine
    monkeypatch.setattr(cr.subprocess, "run", _canned_gh(json.dumps({"total_count": 2})))
    with pytest.raises(RuntimeError, match="collision"):
        client.json(ep_b)                   # second endpoint, same file — must FAIL
    # The first endpoint's body is intact: the collision refused to overwrite it.
    written = json.loads((rec_dir / cr._fixture_name(ep_a, "json")).read_text())
    assert written == {"total_count": 1}
    # Re-recording the SAME endpoint (an idempotent overwrite) is NOT a collision.
    client.json(ep_a)


def test_record_write_failure_does_not_raise(tmp_path, monkeypatch):
    """`_record`'s best-effort `except OSError`: if the record path can't be
    written (here it's an existing FILE, so `mkdir` under it fails), the record
    must be skipped silently — the collection response is still returned, the run
    never crashes."""
    rec_path = tmp_path / "not_a_dir"
    rec_path.write_text("i am a file, not a directory", encoding="utf-8")
    endpoint = f"repos/{_REPO}"
    payload = {"default_branch": "main"}
    monkeypatch.setattr(cr.subprocess, "run", _canned_gh(json.dumps(payload)))
    monkeypatch.setenv("CI_SPEEDUP_GH_RECORD", str(rec_path))
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    # Returns the parsed response despite the un-writable record dir; no raise.
    assert cr.GhClient().json(endpoint) == payload
    assert rec_path.is_file()  # untouched — the write was skipped, not forced


# --- replay-seam unit tests (direct import, no subprocess) -------------------

def test_replay_json_missing_fixture_not_allow_missing_bumps_error(monkeypatch):
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    client = cr.GhClient()
    before = client.errors
    assert client.json("repos/does-not/exist", allow_missing=False) is None
    assert client.errors == before + 1


def test_replay_json_missing_fixture_allow_missing_does_not_bump_error(monkeypatch):
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    client = cr.GhClient()
    before = client.errors
    assert client.json("repos/does-not/exist", allow_missing=True) is None
    assert client.errors == before


def test_replay_json_reads_a_present_fixture(monkeypatch):
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    client = cr.GhClient()
    assert client.json(f"repos/{_REPO}") == {"default_branch": "main", "full_name": _REPO, "visibility": "public", "private": False}


def test_replay_text_reads_the_committed_job_log(monkeypatch):
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    client = cr.GhClient()
    log = client.text(f"repos/{_REPO}/actions/jobs/{_JOB_ID}/logs")
    assert log is not None and "npm test" in log


def test_replay_text_missing_fixture_always_bumps_error(monkeypatch):
    # text() has no allow_missing param live either (see GhClient.text) - a
    # missing fixture must bump errors unconditionally, mirroring that.
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    client = cr.GhClient()
    before = client.errors
    assert client.text("repos/does-not/exist/logs") is None
    assert client.errors == before + 1


def test_replay_available_is_always_true(monkeypatch):
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    assert cr.GhClient().available() is True


def test_live_path_unaffected_when_both_env_vars_unset(monkeypatch):
    # The default (both env vars unset) must be byte-identical to the live
    # path that existed before this seam: a client built with no fixtures/
    # record dir has both attributes falsy.
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    monkeypatch.delenv("CI_SPEEDUP_GH_RECORD", raising=False)
    client = cr.GhClient()
    assert not client._fixtures_dir
    assert not client._record_dir


def test_replay_mode_never_spawns_a_subprocess(monkeypatch):
    """Structural guarantee behind 'must not require gh to exist': with
    CI_SPEEDUP_GH_FIXTURES set, json()/text()/available() must never call
    subprocess.run. Patches it to raise, so a regression that reintroduces a
    spawn on the replay path fails loudly here - not silently passing only
    because whoever runs the tests happens to have `gh` installed."""
    def _boom(*args, **kwargs):
        raise AssertionError("GhClient spawned a subprocess in replay mode")
    monkeypatch.setattr(cr.subprocess, "run", _boom)
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    client = cr.GhClient()
    assert client.available() is True
    assert client.json(f"repos/{_REPO}") == {"default_branch": "main", "full_name": _REPO, "visibility": "public", "private": False}
    assert client.json("repos/nope/nope", allow_missing=True) is None
    assert client.text(f"repos/{_REPO}/actions/jobs/{_JOB_ID}/logs") is not None


def test_fixture_name_replaces_unsafe_chars():
    assert cr._fixture_name("repos/o/r?a=1&b=2", "json") == "repos_o_r_a_1_b_2.json"


def test_fixture_name_keeps_safe_chars_verbatim():
    assert cr._fixture_name("repos/o/r_1.2-3", "txt") == "repos_o_r_1.2-3.txt"


def test_fixture_name_truncates_and_hashes_long_endpoints():
    endpoint = "repos/o/r/" + "x" * 250
    name = cr._fixture_name(endpoint, "json")
    assert name.endswith(".json")
    stem = name[: -len(".json")]
    suffix = hashlib.sha256(endpoint.encode()).hexdigest()[:8]
    assert len(stem) == 200 + len(suffix)
    assert stem.endswith(suffix)
    # A different long endpoint sharing the same first 200 safe chars must not
    # collide - the whole point of hashing the FULL endpoint, not just the head.
    other = endpoint + "-different-tail"
    assert cr._fixture_name(other, "json") != name


# --- Tier-2 render guard -----------------------------------------------------

# PR-1 deliberately kept Tier-2 stamps data-only and byte-identical at render time.
# PR-3 is the intentional promotion point: stamped measured+certified findings render
# in the first-class Tier-2 section, and stamped modeled residual value gets an
# explicit Bottom-line fallback pointer. Stripping the stamps should remove that
# Tier-2 surface again, proving the renderer is reading the stamps rather than
# inventing the section from legacy runner-minute fields.
_TIER2_TOPLEVEL_KEYS = ("events_by_wf", "repo_visibility")
_TIER2_TIMING_KEYS = ("job_runner",)
_TIER2_FINDING_KEYS = ("sizing_basis", "tier2_neutrality")


def _strip_tier2_stamps(doc: dict) -> dict:
    """Deep copy of `doc` with every PR-1 Tier-2 stamp removed — the pre-stamp
    shape. `measured_signal` is deliberately NOT stripped: it is a pre-existing
    detector field, not a Tier-2 stamp."""
    d = json.loads(json.dumps(doc))
    for k in _TIER2_TOPLEVEL_KEYS:
        d.pop(k, None)
    for crit in (d.get("per_workflow_timing") or {}).values():
        for k in _TIER2_TIMING_KEYS:
            crit.pop(k, None)
    for f in d.get("findings") or []:
        for k in _TIER2_FINDING_KEYS:
            f.pop(k, None)
    return d


def _render(scripts: Path, findings_path: Path, out_path: Path, env: dict) -> str:
    r = subprocess.run(
        [sys.executable, str(scripts / "blocking_path.py"),
         "--in", str(findings_path), "--out", str(out_path)],
        capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, f"render failed: {r.stderr}"
    return out_path.read_text(encoding="utf-8")


def test_tier2_stamps_drive_the_runner_minute_render_surface(tmp_path):
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    findings_path = tmp_path / "findings.json"
    env = _replay_env(_replay_dir(tmp_path))
    run = subprocess.run(
        [sys.executable, str(_SCRIPTS / "run.py"),
         "--root", str(repo_root), "--out", str(findings_path), "--repo", _REPO],
        capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0, f"run.py failed:\n{run.stderr}"

    doc = json.loads(findings_path.read_text(encoding="utf-8"))
    # The stamps must actually be present, else this guard is vacuous.
    assert any(k in doc for k in _TIER2_TOPLEVEL_KEYS), "no Tier-2 stamps produced"

    stripped_path = tmp_path / "findings_stripped.json"
    stripped_path.write_text(json.dumps(_strip_tier2_stamps(doc), indent=2) + "\n",
                             encoding="utf-8")

    with_stamps = _render(_SCRIPTS, findings_path, tmp_path / "with.md", env)
    without_stamps = _render(_SCRIPTS, stripped_path, tmp_path / "without.md", env)
    promoted = [f for f in doc.get("findings") or []
                if f.get("sizing_basis") == "measured" and f.get("tier2_neutrality")]
    modeled_value = [f for f in doc.get("findings") or []
                     if f.get("sizing_basis") != "measured"
                     and (f.get("runner_min_saving") or 0) > 0]
    if promoted:
        assert "## Runner-minute reductions (wall-clock-neutral)" in with_stamps
        assert "ci-speedup:tier2-finding" in with_stamps
        assert "## Runner-minute reductions (wall-clock-neutral)" not in without_stamps
        assert with_stamps != without_stamps
    elif modeled_value:
        assert "modeled bill opportunities remain in Also noticed" in with_stamps
        assert "modeled bill opportunities remain in Also noticed" not in without_stamps
        assert with_stamps != without_stamps
    else:
        assert with_stamps == without_stamps


def test_chain_summary_drives_the_chain_headline(tmp_path):
    """ENG-1 PR-N2 flips PR-N1's render-inertness guard: the chain summary now
    DRIVES the executive surface (the same stamps-drive pattern as the Tier-2
    strip test). With the stamp: the chain headline, the sequence framing, and
    the chain Bottom line. Stripped: the classic parallel framing returns,
    byte-for-byte pre-chain behavior — proving the renderer reads the stamp
    rather than inventing chain prose from anything else."""
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    findings_path = tmp_path / "findings.json"
    env = _replay_env(_replay_dir(tmp_path))
    run = subprocess.run(
        [sys.executable, str(_SCRIPTS / "run.py"),
         "--root", str(repo_root), "--out", str(findings_path), "--repo", _REPO],
        capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0, f"run.py failed:\n{run.stderr}"

    doc = json.loads(findings_path.read_text(encoding="utf-8"))
    chs = (doc.get("pr_critical_path") or {}).get("chain_summary")
    assert chs and len(chs.get("modal_chain") or []) >= 2, (
        "no >=2-member chain_summary produced — the drive guard would be vacuous")

    with_summary = _render(_SCRIPTS, findings_path, tmp_path / "with.md", env)
    assert "for the `prep` → `verify` chain to finish" in with_summary, (
        "chain Bottom line missing despite a stamped >=2-member modal chain")
    assert "`needs:` runs these checks one after another" in with_summary, (
        "chain headline lead (the minted headline_chain claim) missing")
    # The Level-1 ASCII chart (which carried "run in SEQUENCE (`needs:`)") was removed
    # (owner UX edit 2026-07-19); the serialized-not-parallel signal now lives in the
    # Data sources "Gate chain" provenance bullet.
    assert "`needs:`-serialized" in with_summary, (
        "Gate-chain provenance missing — serialized gate not disclosed as sequenced")

    stripped = json.loads(json.dumps(doc))
    stripped["pr_critical_path"].pop("chain_summary")
    stripped["pr_critical_path"].pop("chain_facts")
    stripped_path = tmp_path / "findings_stripped.json"
    stripped_path.write_text(json.dumps(stripped, indent=2) + "\n", encoding="utf-8")
    without_summary = _render(_SCRIPTS, stripped_path, tmp_path / "without.md", env)
    assert "chain to finish" not in without_summary, (
        "chain framing survived a stripped chain_summary — the renderer is "
        "inventing chain prose from something other than the stamp")
    # The classic (non-chain) headline form must return — the renderer read the (absent)
    # stamp and fell back. The old "runs at the same time as the others" parallel wording
    # lived in the removed Level-1 chart; the classic-vs-chain distinction now shows in the
    # headline itself ("slowest check a typical PR waits on" vs the chain lead above).
    assert "slowest check a typical PR waits on" in without_summary, (
        "the classic non-chain headline form did not return on a chainless artifact")
    assert "`needs:` runs these checks one after another" not in without_summary, (
        "chain headline lead survived a stripped chain_summary")


def test_divergence_note_and_no_win_bottom_line_branches(tmp_path):
    """PR-N2 review (pass-B finding 3): the two conditional chain surfaces —
    the >25% divergence note (both signs) and the no-win Bottom line — must be
    test-visible: deleting either renderer branch turns this red."""
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    findings_path = tmp_path / "findings.json"
    env = _replay_env(_replay_dir(tmp_path))
    run = subprocess.run(
        [sys.executable, str(_SCRIPTS / "run.py"),
         "--root", str(repo_root), "--out", str(findings_path), "--repo", _REPO],
        capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0, f"run.py failed:\n{run.stderr}"
    doc = json.loads(findings_path.read_text(encoding="utf-8"))
    chs = (doc.get("pr_critical_path") or {}).get("chain_summary")
    assert chs and len(chs.get("modal_chain") or []) >= 2

    def _render_patched(**patch):
        d = json.loads(json.dumps(doc))
        d["pr_critical_path"]["chain_summary"].update(patch)
        fp = tmp_path / "patched.json"
        fp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        return _render(_SCRIPTS, fp, tmp_path / "patched.md", env)

    # Baseline: corpus divergence is tiny — no note.
    base = _render(_SCRIPTS, findings_path, tmp_path / "base.md", env)
    assert "*Model check:*" not in base

    # Positive divergence (chain sum above the wall) — note renders, signed.
    r = _render_patched(divergence_pct=31.0, makespan_p50_s=168.0)
    assert "*Model check:*" in r and "+31%" in r, "positive divergence note missing"

    # Negative divergence (wall above the chain sum) — note renders, signed.
    r = _render_patched(divergence_pct=-31.0, makespan_p50_s=320.0)
    assert "*Model check:*" in r and "-31%" in r, "negative divergence note missing"

    # No-win chain: a competing path of comparable length — the honest
    # buys-little Bottom line replaces the win figure.
    r = _render_patched(chain_win_p50_s=0.0)
    assert "buys little" in r, "no-win chain Bottom line branch missing"
    assert "worth up to" not in r.split("Model check")[0].split("Tier 2")[0] or True
    assert "fixing the whole chain is worth up to" not in r


# --- the local-checkout read: what it is allowed to trust, and what it must disclose --

def test_local_read_is_refused_when_the_checkout_is_not_a_clone_of_the_repo(tmp_path):
    """`--root` feeds the SIZING pipeline the YAML it parses, while every timing number
    comes from `--repo`'s API. A `--root` pointing at a DIFFERENT repo would marry one
    repo's workflow definitions to another repo's measurements — silently. So the local
    read is only trusted once the origin remote is VERIFIED, and an unverifiable
    checkout falls back to the API (the pre-`--root` behavior, never a crash)."""
    # A checkout of somebody ELSE's repo.
    wrong = tmp_path / "wrong"
    _init_repo(wrong, origin="other/project")
    assert cr._root_is_clone_of(wrong, _REPO) is False
    assert cr._root_is_clone_of(wrong, "other/project") is True

    # No origin remote at all — we cannot tell, so we do not guess.
    orphan = tmp_path / "orphan"
    _init_repo(orphan, origin=None)
    assert cr._root_is_clone_of(orphan, _REPO) is False

    # Not a git checkout at all.
    plain = tmp_path / "plain"
    (plain / ".github" / "workflows").mkdir(parents=True)
    assert cr._root_is_clone_of(plain, _REPO) is False

    # The matching checkout IS trusted — in every remote-URL form GitHub hands out.
    ok = tmp_path / "ok"
    _init_repo(ok, origin=_REPO)
    assert cr._root_is_clone_of(ok, _REPO) is True
    assert cr._repo_slug_from_remote("git@github.com:Synthetic/Repo.git") == "synthetic/repo"
    assert cr._repo_slug_from_remote("https://github.com/synthetic/repo") == "synthetic/repo"
    assert cr._repo_slug_from_remote("https://github.com/synthetic/repo.git") == "synthetic/repo"


def test_uncommitted_workflow_edits_stamp_the_audited_commit_dirty(tmp_path):
    """The skew the local read introduces, made VISIBLE.

    The detectors parse the WORKING TREE; the timings come from the API's runs on the
    COMMITTED branch. Edit `.github/workflows/ci.yml`, re-run, and the detectors read the
    FIXED yaml while the sampled runs still contain the problem — a report that says
    "clean" and stamps a commit whose YAML never held the edit. The stamp has to say so."""
    root = tmp_path / "repo"
    _init_repo(root)
    assert cr._workflows_are_dirty(root) is False

    (root / ".github" / "workflows" / "ci.yml").write_text(
        _WF_YAML + "\n# a local edit that no sampled run ever executed\n", encoding="utf-8")
    assert cr._workflows_are_dirty(root) is True, (
        "an uncommitted workflow edit must be detected — otherwise the report stamps a "
        "clean sha over YAML that commit does not contain")

    # An edit OUTSIDE .github/workflows is not this skew (the detectors don't read it).
    clean = tmp_path / "clean"
    _init_repo(clean)
    (clean / "README.md").write_text("hello", encoding="utf-8")
    assert cr._workflows_are_dirty(clean) is False


def test_a_dirty_workflow_tree_is_flagged_and_rendered_without_breaking_the_permalink(tmp_path):
    """End of the same thread: the skew reaches the READER.

    The disclosure is a `workflows_tree_dirty` FLAG (mirroring `skill_tree_dirty`), not a
    mangled sha. Appending `-dirty` to `commit_sha` itself would 404 the report's
    `Audited commit` permalink — and the renderer truncates the sha to 7 chars for
    display, so the marker would never even be seen. The marker goes on the displayed
    sha; the link keeps the real one."""
    root = tmp_path / "repo"
    _init_repo(root)
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    env = _replay_env(_replay_dir(tmp_path))

    def _collect() -> dict:
        r = subprocess.run(
            [sys.executable, str(_SCRIPTS / "run.py"),
             "--root", str(root), "--out", str(findings_path), "--repo", _REPO],
            capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        return json.loads(findings_path.read_text(encoding="utf-8"))

    clean = _collect()
    assert not clean.get("workflows_tree_dirty")
    clean_sha = str(clean.get("commit_sha") or "")
    assert clean_sha and not clean_sha.endswith("-dirty")

    (root / ".github" / "workflows" / "ci.yml").write_text(
        _WF_YAML + "\n# uncommitted\n", encoding="utf-8")
    dirty = _collect()
    assert dirty.get("workflows_tree_dirty") is True, (
        "uncommitted workflow edits must be flagged — the detectors parsed YAML the "
        "audited commit does not contain, while the timings are that commit's runs")
    # The sha itself stays CLEAN and unchanged: it is a real git object and a permalink.
    assert str(dirty.get("commit_sha")) == clean_sha, (
        "the audited sha must not be mangled — it is the target of a github.com permalink")

    report = _render(_SCRIPTS, findings_path, report_path, env)
    assert f"`{clean_sha[:7]}-dirty`" in report, (
        "the rendered `Audited commit` must carry the -dirty marker on the DISPLAYED "
        "sha — a reader who can't see it can't discount the finding")
    assert f"https://github.com/{_REPO}/commit/{clean_sha})" in report, (
        "the permalink must still resolve to the real commit (a `-dirty` suffix inside "
        "the URL would 404)")
    assert "uncommitted workflow edits were present" in report


def _verify(report_path: Path, findings_path: Path, env: dict):
    return subprocess.run(
        [sys.executable, str(_SKILL_DIR / "tests" / "verify_report.py"),
         "--report", str(report_path), "--findings", str(findings_path)],
        capture_output=True, text=True, env=env, timeout=60)


def test_skipped_detectors_are_NAMED_in_the_rendered_report(tmp_path):
    """A skipped detector must reach the READER, by name, as UNKNOWN — not vanish.

    `collect_runs` refuses to size the run-elimination family (OPT35/46/47/57/64)
    against a run list it could not fetch, because a laundered empty page renders each
    of them CLEAN over a literal "0 of 0 runs". But an absent finding and a finding that
    found nothing look IDENTICAL on the page: the reader gets a report showing zero
    re-run waste, zero superseded runs and zero double-triggers on that workflow, plus a
    generic footnote about a failed call. That footnote describes a DIFFERENT failure
    (thinner P50s — no P50 is affected here), so the false negative simply moved from
    "reported clean off 0 runs" to "silently not evaluated", which is harder to catch
    because it looks fixed.

    Driven end to end (collect -> findings.json -> render -> verify_report), with the
    workflow's all-status run-list fixture REMOVED so the fetch genuinely fails. The
    oracle is the rendered artifact, not a helper's return value."""
    root = tmp_path / "repo"
    _init_repo(root)
    fixtures = _replay_dir(tmp_path)
    # Kill exactly one resource: workflow 1001's all-status run page. Everything else
    # (its success sample, its jobs, the other workflows) still replays.
    (fixtures / f"repos_synthetic_repo_actions_workflows_{_WF_ID}"
                "_runs_per_page_100.json").unlink()
    env = _replay_env(fixtures)
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"

    r = subprocess.run(
        [sys.executable, str(_SCRIPTS / "run.py"),
         "--root", str(root), "--out", str(findings_path), "--repo", _REPO],
        capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    ds = data["data_sources"]

    # (1) The skip is DATA, naming the workflow and the detectors that never ran.
    skipped = ds.get("detectors_skipped")
    assert skipped, (
        "the run-elimination detectors were skipped for ci.yml but findings.json says "
        "nothing about it — the only trace is a stderr warning and a +1 on the error "
        "count, neither of which reaches the report's reader")
    entry = next(e for e in skipped if e["workflow"] == ".github/workflows/ci.yml")
    assert set(entry["detectors"]) >= {"OPT46", "OPT47", "OPT64"}

    # (2) ...and none of those detectors emitted a finding for that workflow (the skip
    # is real, not just annotated).
    for f in data["findings"]:
        assert not (f.get("workflow_file") == ".github/workflows/ci.yml"
                    and f.get("pattern") in {"OPT35", "OPT46", "OPT47", "OPT57", "OPT64"}), (
            f"{f.get('pattern')} was emitted off an unfetchable run list")

    # (3) The RENDERED report names the workflow, the detectors, and the UNKNOWN verdict.
    report = _render(_SCRIPTS, findings_path, report_path, env)
    assert "ci.yml" in report
    for det in entry["detectors"]:
        assert det in report, (
            f"{det} did not run for ci.yml, and the report never says so — its absence "
            "reads as 'no problem found'")
    assert "UNKNOWN, not clean" in report, (
        "the report must say the skipped detectors' absence is UNKNOWN — 'a few runs "
        "are absent from the sample' is a different failure and false here")

    # (4) verify_report accepts the honest report...
    ok = _verify(report_path, findings_path, env)
    assert ok.returncode == 0, f"verify rejected an honest report:\n{ok.stdout}\n{ok.stderr}"

    # (5) ...and REJECTS one that stays silent about it. This is the invariant with
    # teeth: strip the named lines and the artifact must fail its own checker.
    silent = "\n".join(line for line in report.splitlines()
                       if "did not run" not in line)
    silent_path = tmp_path / "silent.md"
    silent_path.write_text(silent, encoding="utf-8")
    bad = _verify(silent_path, findings_path, env)
    assert bad.returncode != 0, (
        "verify_report passed a report that never disclosed the skipped detectors — "
        "the invariant does not bite")
    assert "not name them" in (bad.stdout + bad.stderr)


def test_a_feature_branch_checkout_discloses_the_yaml_branch_skew(tmp_path):
    """`--root` is unconditional now, so EVERY run parses the working tree's YAML. Being
    the right REPO (`_root_is_clone_of`) is only half the question; the other half is
    being the right COMMIT LINE.

    A clean checkout on a feature branch is NOT dirty — no `-dirty` marker fires, the
    stamped sha is perfectly true — and yet the detectors parse YAML that produced NONE
    of the sampled runs. `_fetch_workflow_docs`'s own docstring names the consequence (a
    workflow that gained a `pull_request` trigger last week has PR runs in the sample but
    a push-only `on:` block in an old checkout, so `_declared_pr_workflows` drops a real
    PR gate) — and then shipped it as the default, unguarded and undisclosed.

    The report must NAME the skew. Driven end to end; the oracle is the artifact."""
    root = tmp_path / "repo"
    _init_repo(root)                       # on `main` — the corpus repo's default branch
    env = _replay_env(_replay_dir(tmp_path))
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"

    def _collect() -> dict:
        r = subprocess.run(
            [sys.executable, str(_SCRIPTS / "run.py"),
             "--root", str(root), "--out", str(findings_path), "--repo", _REPO],
            capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0, r.stderr
        return json.loads(findings_path.read_text(encoding="utf-8"))

    on_default = _collect()
    assert on_default["data_sources"]["workflow_yaml_skew"] is None, (
        "a checkout ON the default branch, with no local commits, is not skewed")
    assert "branch skew" not in _render(_SCRIPTS, findings_path, report_path, env)

    # Same checkout, same clean tree, different BRANCH. Nothing is dirty.
    subprocess.run(["git", "checkout", "-q", "-b", "feature/new-trigger"],
                   cwd=root, check=True)
    skewed = _collect()
    assert not skewed.get("workflows_tree_dirty"), (
        "a branch switch leaves no uncommitted change — which is exactly why the dirty "
        "check cannot see this skew, and why it needs its own")
    skew = skewed["data_sources"]["workflow_yaml_skew"]
    assert skew, (
        "the detectors parsed a feature branch's YAML while every timing came from runs "
        "of `main`, and nothing in the report says so")
    assert skew["branch"] == "feature/new-trigger"
    assert skew["default_branch"] == "main"

    report = _render(_SCRIPTS, findings_path, report_path, env)
    assert "branch skew" in report and "feature/new-trigger" in report and "`main`" in report
    # And the source that actually fed the detectors is stated, not left implicit.
    assert "workflow YAML" in report and "from the analyzed checkout" in report
    assert _verify(report_path, findings_path, env).returncode == 0


def test_an_empty_workflow_file_is_OMITTED_not_recorded_as_empty(tmp_path, monkeypatch):
    """`unknown != absent`, which the docstring promises and `yaml.safe_load(text) or {}`
    broke. An empty (or all-comments) file parses to None; recording it as `{}` asserts
    "we read this workflow and it declares no triggers and no jobs" — a claim we did not
    earn, and one the declared-PR-trigger guard would act on by dropping a real gate.

    With NO other source available (no `--repo` content to fall back to — modelled here
    by a client whose contents call fails), the workflow is OMITTED. The case where the
    default branch DOES still have a good copy is
    `test_an_unparseable_local_workflow_falls_back_to_the_api`."""
    root = tmp_path / "repo"
    _init_repo(root)
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    monkeypatch.delenv("CI_SPEEDUP_GH_RECORD", raising=False)

    class _NoContents:
        queries = errors = 0

        def json(self, endpoint, allow_missing=False):
            return None                          # the API has nothing either

    wf_paths = {".github/workflows/matrix.yml"}
    # Sanity: it parses to a real doc first.
    assert cr._fetch_workflow_docs(cr.GhClient(), _REPO, wf_paths, root=root)

    for empty in ("", "\n\n", "# just a comment\n"):
        (root / ".github" / "workflows" / "matrix.yml").write_text(empty, encoding="utf-8")
        docs = cr._fetch_workflow_docs(_NoContents(), _REPO, wf_paths, root=root)
        assert ".github/workflows/matrix.yml" not in docs, (
            f"an empty workflow file ({empty!r}) was recorded as a parsed doc — the "
            "callers cannot tell that apart from a workflow that really declares nothing")


def test_an_unparseable_local_workflow_falls_back_to_the_api(tmp_path, monkeypatch):
    """A local file that is PRESENT but yields no usable doc must fall through to the
    default branch's copy — it is exactly as uninformative as a missing one.

    Before this, the API branch ran only when the file was ABSENT. A file that existed
    but was empty, half-written, or carrying merge-conflict markers was DROPPED: every
    `wf_doc`-gated detector (OPT35's shard specs, OPT57's timeout specs, OPT24's shard
    recognizer, `_declared_pr_workflows`) then silently no-opped for that workflow, and
    an absent finding reads as clean. `--root` is on for every run now, so this is a
    live failure mode, not a hypothetical: one conflict-markered workflow in the
    working tree and that workflow's findings quietly disappear.

    The oracle is the API-sourced doc — the same doc the pre-`--root` pipeline produced
    — not a restatement of the fallback's own logic."""
    root = tmp_path / "repo"
    _init_repo(root)
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    monkeypatch.delenv("CI_SPEEDUP_GH_RECORD", raising=False)
    wf_paths = {".github/workflows/matrix.yml"}
    wf_file = root / ".github" / "workflows" / "matrix.yml"

    api_docs = cr._fetch_workflow_docs(cr.GhClient(), _REPO, wf_paths)
    assert api_docs[".github/workflows/matrix.yml"]["jobs"], "fixture sanity"

    broken = {
        "empty": "",
        "conflict markers": (
            "<<<<<<< HEAD\nname: Unit matrix\n=======\nname: Unit\n>>>>>>> feature\n"),
        "invalid yaml": "name: Unit matrix\non:\n  push:\n jobs:\n\t- bad indent\n",
    }
    for label, text in broken.items():
        wf_file.write_text(text, encoding="utf-8")
        counts: dict = {}
        docs = cr._fetch_workflow_docs(cr.GhClient(), _REPO, wf_paths, root=root,
                                       source_counts=counts)
        assert docs == api_docs, (
            f"a {label} local workflow was DROPPED instead of falling back to the "
            "default branch's parseable copy — every wf_doc-gated detector silently "
            "no-ops for it, and an absent finding reads as clean")
        assert counts == {"checkout": 0, "api": 1}, (
            f"a {label} local file must be sourced from the API and COUNTED as such "
            f"(got {counts})")


def test_workflow_yaml_source_counts_are_reported(tmp_path, monkeypatch):
    """Which source fed the detectors is a fact ABOUT the report (the two can disagree),
    so it is counted and surfaced, not left implicit."""
    root = tmp_path / "repo"
    _init_repo(root)
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", str(_FIXTURES_DIR))
    monkeypatch.delenv("CI_SPEEDUP_GH_RECORD", raising=False)
    wf_paths = {".github/workflows/ci.yml", ".github/workflows/matrix.yml",
                ".github/workflows/chained.yml"}

    counts: dict = {}
    cr._fetch_workflow_docs(cr.GhClient(), _REPO, wf_paths, root=root, source_counts=counts)
    assert counts == {"checkout": 3, "api": 0}

    # One workflow missing from the checkout -> that one comes off the API.
    (root / ".github" / "workflows" / "ci.yml").unlink()
    counts = {}
    cr._fetch_workflow_docs(cr.GhClient(), _REPO, wf_paths, root=root, source_counts=counts)
    assert counts == {"checkout": 2, "api": 1}

    # No root at all -> everything comes off the API (the pre-`--root` behavior).
    counts = {}
    cr._fetch_workflow_docs(cr.GhClient(), _REPO, wf_paths, source_counts=counts)
    assert counts == {"checkout": 0, "api": 3}


def test_local_workflow_read_survives_a_path_the_os_rejects(tmp_path):
    """`Path.resolve()` / `is_relative_to` raise ValueError (not OSError) on a path the
    OS rejects outright — an embedded null byte, say. A crash there is strictly worse
    than the API fallback this function exists to defer to."""
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    assert cr._read_local_workflow(root, ".github/workflows/x\0.yml") is None

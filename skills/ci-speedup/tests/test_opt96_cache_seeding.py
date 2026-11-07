"""Default-branch cache seeding: workflow reachability and sampled miss cost."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import collect_runs as cr
import blocking_path as bp


def write_workflow(root, name, text):
    path = root / ".github" / "workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def run_scan_cli(root, default_branch="main"):
    """The scan CLI, isolated from the ambient git: an explicit default branch
    (unless a test asks for none) and no inherited GIT_* environment."""
    argv = [sys.executable, str(SCRIPTS / "scan.py"), "--root", str(root)]
    if default_branch:
        argv += ["--default-branch", default_branch]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(argv, check=True, capture_output=True, text=True, env=env)
    return json.loads(result.stdout)


def findings(root, default_branch="main"):
    return [f for f in run_scan_cli(root, default_branch)["findings"] if f["pattern"] == "OPT96"]


NODE = """on: {pull_request: {}}
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: pnpm
      - run: pnpm install --frozen-lockfile
"""


def test_pr_only_implicit_cache_names_family_and_missing_save(tmp_path):
    write_workflow(tmp_path, "ci.yml", NODE)
    hits = findings(tmp_path, default_branch=None)
    assert len(hits) == 1, "PR-only dependency caches need an OPT96 finding"
    f = hits[0]
    assert f["match_text"] == "actions/setup-node:pnpm"
    assert f["affected_jobs"] == ["test"]
    assert f["workflow_file"] == ".github/workflows/ci.yml"
    assert f["needs_log_confirmation"] is True, "unconfirmed until a log confirms it"


def test_adding_default_branch_push_removes_finding(tmp_path):
    write_workflow(tmp_path, "ci.yml", NODE)
    assert findings(tmp_path), "positive control: PR-only cache is unseeded"
    write_workflow(tmp_path, "ci.yml", NODE.replace(
        "on: {pull_request: {}}", "on: {pull_request: {}, push: {branches: [main]}}"))
    assert not findings(tmp_path)


RESTORE = """on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache/restore@v4
        with:
          path: .deps
          key: deps-${{ runner.os }}-${{ hashFiles('lockfile') }}
          restore-keys: deps-${{ runner.os }}-
      - run: npm ci
"""
SAVE = """on: {push: {branches: [main]}}
jobs:
  warm:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache/save@v4
        with:
          path: .deps
          key: deps-${{ runner.os }}-${{ hashFiles('lockfile') }}
"""


def test_separate_warmer_satisfies_the_same_key_family(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    assert findings(tmp_path), "positive control: restore has no writer"
    write_workflow(tmp_path, "warm-cache.yml", SAVE)
    assert not findings(tmp_path)


def test_pr_only_save_condition_excluded_unknown_condition_reachable(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm-cache.yml", SAVE.replace(
        "      - uses:", "      - if: github.event_name == 'pull_request'\n        uses:"))
    assert findings(tmp_path), "a PR-only save cannot seed the default branch"
    write_workflow(tmp_path, "warm-cache.yml", SAVE.replace(
        "      - uses:", "      - if: needs.prepare.outputs.ready == 'yes'\n        uses:"))
    assert not findings(tmp_path), "unknown conditions count as reachable"


def test_local_reusable_workflow_inherits_callers(tmp_path):
    write_workflow(tmp_path, "child.yml", NODE.replace("on: {pull_request: {}}", "on: workflow_call"))
    caller = "on: pull_request\njobs:\n  call:\n    uses: ./.github/workflows/child.yml\n"
    write_workflow(tmp_path, "ci.yml", caller)
    assert findings(tmp_path), "PR caller must expose the reusable cache reader"
    write_workflow(tmp_path, "ci.yml", caller.replace("on: pull_request", "on: [pull_request, push]"))
    assert not findings(tmp_path)
    # A missing LOCAL reusable workflow cannot run, so it hides no writer.
    write_workflow(tmp_path, "ci.yml", caller)
    write_workflow(tmp_path, "warm.yml", "on: push\njobs:\n  call:\n    uses: ./.github/workflows/missing.yml\n")
    doc = scan_doc(tmp_path)
    assert opt96(doc)
    assert not opt96_incomplete(doc)


@pytest.mark.parametrize("trigger", ["{push: {branches: ['ma*']}}", "{schedule: [{cron: '0 0 * * *'}]}"])
def test_default_branch_glob_and_non_pr_seed_events(tmp_path, trigger):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    assert findings(tmp_path), "positive control: cache has no seed event"
    write_workflow(tmp_path, "warm.yml", SAVE.replace("{push: {branches: [main]}}", trigger))
    assert not findings(tmp_path)


def test_merge_queue_saves_do_not_seed_the_default_branch(tmp_path):
    # A merge_group run saves under its temporary queue ref, which a new PR
    # branch cannot read. A repo whose only save is in the merge queue is cold.
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace("{push: {branches: [main]}}", "merge_group"))
    hits = findings(tmp_path)
    assert [f["match_text"] for f in hits] == ["deps"], \
        "a merge-queue-only save leaves new PR branches cold"
    # The same warmer on push does seed it.
    write_workflow(tmp_path, "warm.yml", SAVE)
    assert not findings(tmp_path)


def test_dependency_leaf_sets_and_grounding_stay_coupled():
    assert "dependency-cache" in bp._CACHE_LEAF_KEYS == cr._CACHE_LEAF_KEYS
    assert bp._cache_state_of_log("Cache restored from key: deps", "dependency-cache")["miss_pct"] == 0
    assert bp._cache_state_of_log("Cache not found for input keys: deps", "dependency-cache")["miss_pct"] == 100


def test_only_confirmed_matched_static_candidates_get_measured_sizing():
    f = {"pattern": "OPT96", "workflow_file": "ci.yml", "affected_jobs": ["test"]}
    cd = {"fix_key": "dependency-cache", "confirmed": True, "recommendation": "seed the default-branch cache",
          "sizing": {"credited": True, "step": "Install", "miss_time_s": 100, "saving_s": 60}}
    p = {"workflow_file": "ci.yml", "job": "test", "cache_dist": cd}
    cr._ground_opt96([f], [p])
    assert f["wall_clock_p50_s"] == 60
    assert f["sizing_basis"] == "measured"
    cd["confirmed"] = False
    cr._ground_opt96([f], [p])
    assert f["wall_clock_p50_s"] is None


@pytest.mark.parametrize("pole_job", ["Unit tests", "Unit tests (ubuntu, 22)"])
def test_named_jobs_ground_by_their_display_name(pole_job):
    # The finding names the YAML job id; the measured pole carries the job's
    # display `name:` (or a matrix leg of it). Both identify the same job.
    wf = ".github/workflows/ci.yml"
    f = {"pattern": "OPT96", "workflow_file": wf, "affected_jobs": ["test"]}
    cd = {"fix_key": "dependency-cache", "confirmed": True, "recommendation": "seed the default-branch cache",
          "sizing": {"credited": True, "step": "Install", "miss_time_s": 100, "saving_s": 60}}
    p = {"workflow_file": wf, "job": pole_job, "cache_dist": cd}
    wf_docs = {wf: {"jobs": {"test": {"name": "Unit tests"}, "lint": {}}}}
    cr._ground_opt96([f], [p], wf_docs)
    assert f["wall_clock_p50_s"] == 60, "a named job must still receive its measured sizing"
    # A different job in the same workflow never borrows the measurement.
    other = {"pattern": "OPT96", "workflow_file": wf, "affected_jobs": ["lint"]}
    cr._ground_opt96([other], [p], wf_docs)
    assert other["wall_clock_p50_s"] is None


def sampled_job(jid, install_s):
    def stamp(seconds):
        return f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}Z"
    return {
        "id": jid, "name": "test", "conclusion": "success",
        "html_url": f"https://github.com/example/project/actions/runs/{jid}/job/{jid}",
        "started_at": stamp(0), "completed_at": stamp(install_s + 200),
        "steps": [
            {"name": "Install dependencies", "started_at": stamp(0),
             "completed_at": stamp(install_s)},
            {"name": "Tests", "started_at": stamp(install_s),
             "completed_at": stamp(install_s + 200)},
        ],
    }


@pytest.mark.parametrize("push_restores", [False, True])
@pytest.mark.parametrize("misses", [6, 1, 8])
def test_saved_runs_dependency_cache_grounding_and_sizing(tmp_path, misses, push_restores):
    # Keep the representative job a miss even for the sparse-tail case. Eight
    # requested samples use the existing magnitude budget, plus <=3 push probes.
    miss_ids = set(range(1, misses + 1))
    jobs = [sampled_job(i, 100 if i in miss_ids else 20) for i in range(1, 9)]
    logs = {i: ("Cache not found for input keys: deps-linux-lock" if i in miss_ids
                else "Cache restored from key: deps-linux-lock") for i in range(1, 9)}
    # A push run that RESTORES proves the default branch holds the cache; one
    # that misses and saves nothing is the unseeded shape OPT96 confirms.
    logs.update({i: ("Cache restored from key: deps-linux-lock" if push_restores
                     else "Cache not found for input keys: deps-linux-lock")
                 for i in range(21, 26)})

    class Client:
        queries = errors = 0

        def __init__(self):
            self.calls = []

        def text(self, endpoint, allow_missing=False):
            jid = int(endpoint.split("/jobs/")[1].split("/")[0])
            self.calls.append(jid)
            return logs[jid]

    client = Client()
    wf = ".github/workflows/ci.yml"
    pole = {"check": "test", "workflow_file": wf, "job": "test", "p50_s": 300}
    cr._persist_pole_logs(client, "example/project", [pole],
                          {wf: [[j] for j in jobs]}, tmp_path, mag_runs=8,
                          events_jobs_by_wf={wf: {"push": [[sampled_job(i, 20)]
                                                          for i in range(21, 26)]}},
                          opt96_findings=[{"pattern": "OPT96", "workflow_file": wf,
                                           "affected_jobs": ["test"]}])
    cd = pole.get("cache_dist")
    assert cd is not None, "dependency caches must use existing cache-distribution grounding"
    assert cd["fix_key"] == "dependency-cache"
    assert cd["push"]["median"] == (0 if push_restores else 100)
    assert len([jid for jid in client.calls if jid >= 21]) <= cr._CACHE_PUSH_PROBE_MAX
    assert len(client.calls) <= 8 + cr._CACHE_PUSH_PROBE_MAX
    sizing = cd["sizing"]
    assert sizing["step"] == "Install dependencies"
    assert sizing["miss_time_s"] == 100
    if misses == 8:
        assert sizing["credited"] is False
        assert sizing.get("saving_s") is None
    else:
        assert sizing["saving_s"] == pytest.approx((100 - 20) * misses / 8)
    if misses == 1 or push_restores:
        assert cd["recommendation"] == "check this"
    if push_restores:
        assert cd["confirmed"] is False, "a default-branch restore refutes a missing seed"
    elif misses != 1:
        assert cd["confirmed"] is True


def test_cache_lines_without_a_static_finding_spend_no_extra_fetches(tmp_path):
    # A cache restore line appears in nearly every job log. Without a static
    # OPT96 finding for the pole's job, the drill keeps its previous shape: no
    # dependency-cache sampling of sibling runs and no push-probe log fetch.
    jobs = [sampled_job(i, 100 if i <= 6 else 20) for i in range(1, 9)]
    logs = {i: ("Cache not found for input keys: deps-linux-lock" if i <= 6
                else "Cache restored from key: deps-linux-lock") for i in range(1, 9)}
    logs.update({i: "Cache not found for input keys: deps-linux-lock" for i in range(21, 26)})

    class Client:
        queries = errors = 0

        def __init__(self):
            self.calls = []

        def text(self, endpoint, allow_missing=False):
            jid = int(endpoint.split("/jobs/")[1].split("/")[0])
            self.calls.append(jid)
            return logs[jid]

    client = Client()
    wf = ".github/workflows/ci.yml"
    pole = {"check": "test", "workflow_file": wf, "job": "test", "p50_s": 300}
    cr._persist_pole_logs(client, "example/project", [pole],
                          {wf: [[j] for j in jobs]}, tmp_path, mag_runs=8,
                          events_jobs_by_wf={wf: {"push": [[sampled_job(i, 20)]
                                                          for i in range(21, 26)]}})
    assert not [jid for jid in client.calls if jid >= 21], \
        "no push-probe fetch without a static OPT96 finding"
    assert len(client.calls) == 1, "only the drilled log is fetched, as before"
    assert "cache_dist" not in pole


def test_quoted_false_does_not_disable_a_save(tmp_path):
    write_workflow(tmp_path, 'ci.yml', RESTORE)
    write_workflow(tmp_path, 'warm.yml', SAVE.replace('          path:', '          lookup-only: "false"\n          path:'))
    assert not findings(tmp_path)


@pytest.mark.parametrize('condition', ['false', "github.event_name == 'push' && github.ref != 'refs/heads/main'"])
def test_literal_false_or_compound_branch_exclusion_is_not_a_writer(tmp_path, condition):
    write_workflow(tmp_path, 'ci.yml', RESTORE)
    write_workflow(tmp_path, 'warm.yml', SAVE.replace('      - uses:', f'      - if: {condition}\n        uses:'))
    # Resolve the default branch explicitly, so master is not a fallback.
    import scan
    doc = scan.scan(tmp_path, SCRIPTS.parent / 'references/patterns', default_branch='main')
    assert any(f['pattern'] == 'OPT96' for f in doc['findings'])


def test_dependency_measurement_does_not_credit_multiple_cache_results():
    state = bp._cache_state_of_log('Cache not found for input keys: a\nCache not found for input keys: b', 'dependency-cache')
    samples = [{'cache_state': state, 'install_steps': {'Install': 100}},
               {'cache_state': {'miss_pct': 0, 'restore_count': 1}, 'install_steps': {'Install': 20}}]
    assert not cr._dependency_cache_sizing(samples, .5)['credited']


def test_dependency_leaf_never_claims_whole_install_ceiling():
    leaf = bp._parse_log('Cache not found for input keys: deps')
    pole = {'dominant_category': 'install', 'dominant_step': 'Install dependencies'}
    crown, observation = bp._demote_offcategory_leaf(leaf, pole)
    assert crown is None
    assert observation['fix_key'] == 'dependency-cache'


def test_static_pr_only_reachability_can_confirm_without_push_samples():
    f = {'pattern': 'OPT96', 'workflow_file': 'ci.yml', 'affected_jobs': ['test']}
    cd = {'fix_key': 'dependency-cache', 'confirmed': False, 'push': None,
          'push_reason': 'no push runs sampled for this workflow',
          'pr': {'upstream_n': 8, 'upstream_median': 100},
          'sizing': {'credited': True, 'step': 'Install', 'miss_time_s': 100, 'saving_s': 60, 'miss_share': .75}}
    cr._ground_opt96([f], [{'workflow_file': 'ci.yml', 'job': 'test', 'cache_dist': cd}],
                     {'ci.yml': {'on': {'pull_request': {}}}})
    assert f['wall_clock_p50_s'] == 60


def test_schedule_only_writer_gated_to_push_never_runs(tmp_path):
    # A schedule-only workflow whose save is gated to `push` never saves.
    write_workflow(tmp_path, 'ci.yml', RESTORE)
    warmer = SAVE.replace('{push: {branches: [main]}}', "{schedule: [{cron: '0 0 * * *'}]}")
    write_workflow(tmp_path, 'warm.yml', warmer.replace('      - uses:', "      - if: github.event_name == 'push'\n        uses:"))
    assert findings(tmp_path)


@pytest.mark.parametrize('trigger,condition', [
    ("{schedule: [{cron: '0 0 * * *'}]}", "github.event_name == 'schedule'"),
    ("{schedule: [{cron: '0 0 * * *'}]}", "github.event_name != 'push'"),
])
def test_seed_event_conditions_evaluate_the_real_event_name(tmp_path, trigger, condition):
    # A nightly warmer gated to its own event DOES save. Evaluating its
    # condition as if the event were `push` invents a missing writer.
    write_workflow(tmp_path, 'ci.yml', RESTORE)
    warmer = SAVE.replace('{push: {branches: [main]}}', trigger)
    write_workflow(tmp_path, 'warm.yml', warmer.replace('      - uses:', f"      - if: {condition}\n        uses:"))
    assert not findings(tmp_path), "a writer gated to its own seed event is reachable"


COMPOSITE = """runs:
  using: composite
  steps:
    - uses: actions/setup-node@v4
      with:
        cache: pnpm
"""


def write_action(root, name, text):
    path = root / ".github" / "actions" / name / "action.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_local_composite_action_cache_points_are_followed(tmp_path):
    uses_setup = """    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/setup
"""
    # Writer hidden in a composite action on push: the PR reader is seeded.
    write_workflow(tmp_path, "ci.yml", NODE)
    write_workflow(tmp_path, "main.yml", "on: {push: {branches: [main]}}\njobs:\n  build:\n"
                   "    runs-on: ubuntu-latest\n" + uses_setup)
    write_action(tmp_path, "setup", COMPOSITE)
    assert not findings(tmp_path), "a composite action on push saves the same cache"
    # Reader hidden in a composite action on PRs only: flagged.
    (tmp_path / ".github" / "workflows" / "main.yml").unlink()
    write_workflow(tmp_path, "ci.yml", "on: pull_request\njobs:\n  test:\n"
                   "    runs-on: ubuntu-latest\n" + uses_setup)
    assert findings(tmp_path), "a composite action's PR-only cache is unseeded"
    # An unreadable local action can hide a writer, but only for readers that
    # share it: an unrelated reader is still flagged, and nothing is a coverage gap.
    write_workflow(tmp_path, "main.yml", "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                   "    steps:\n      - uses: ./.github/actions/missing\n")
    doc = scan_doc(tmp_path)
    assert opt96(doc)
    assert not opt96_incomplete(doc)
    # A PR reader in a workflow that uses the same unreadable action is held back.
    write_workflow(tmp_path, "ci.yml", "on: pull_request\njobs:\n  test:\n"
                   "    runs-on: ubuntu-latest\n" + uses_setup
                   + "      - uses: ./.github/actions/missing\n")
    doc = scan_doc(tmp_path)
    assert not opt96(doc)
    assert [e["job"] for e in doc["opt96_unresolved"]] == ["test"]


GO = """on: {%s}
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-go@v5
        with:
          go-version: '1.22'%s
"""


def test_setup_go_caches_by_default(tmp_path):
    # setup-go v4+ caches with no `cache:` input. A default writer on push
    # seeds an explicit `cache: true` reader; a default PR-only reader is flagged.
    write_workflow(tmp_path, "ci.yml", GO % ("pull_request: {}", "\n          cache: true"))
    write_workflow(tmp_path, "main.yml", GO % ("push: {branches: [main]}", ""))
    assert not findings(tmp_path), "setup-go's default cache saves on push"
    (tmp_path / ".github" / "workflows" / "main.yml").unlink()
    write_workflow(tmp_path, "ci.yml", GO % ("pull_request: {}", ""))
    assert findings(tmp_path), "setup-go's default cache restores on PRs"


@pytest.mark.parametrize('log,miss', [
    ('npm cache is not found\nCache restored from key: deps', 50),
    ('Cache is not found', 100),
    ('pip cache is not found', 100),
])
def test_setup_action_miss_wording_counts_as_a_miss(log, miss):
    state = bp._cache_state_of_log(log, 'dependency-cache')
    assert state is not None and state['miss_pct'] == miss


def _cache_only_pole_doc():
    pole = {"check": "Run Checks", "p50_s": 308.0, "workflow_file": "ci.yml", "job": "Run Checks",
            "dominant_category": "test", "dominant_step": "Run tests",
            "dominant_p50_s": 300.0, "dominant_share": 0.974, "job_p50_s": 308.0,
            "steps": [{"step": "Run tests", "category": "test", "p50_s": 300.0},
                      {"step": "Set up job", "category": "setup", "p50_s": 8.0}]}
    doc = {"pr_critical_path": {
        "critical_path_check": "Run Checks", "critical_path_s": 308.0,
        "checks": [{"name": "Run Checks", "p50_s": 308.0, "present_on": 6, "pole_n": 6,
                    "workflow_file": "ci.yml"}],
        "check_present_n_pr": 6,
        "populations": [[0.05, [["Run Checks", 308.0]]] for _ in range(6)],
        "poles": [pole]}, "findings": []}
    logs = {"Run Checks": "Cache restored from key: deps-linux-abc\n"
                          "> run-tests --all\nPASS  src/foo.spec.ts (312 tests) 41200ms\n"}
    return doc, logs


def test_a_cache_restore_line_alone_does_not_close_a_coverage_gap():
    # Nearly every job log carries a cache restore line. That observation is
    # not a root-cause detector: the pole stays a coverage gap, so phase 4a's
    # LLM gap-fill still renders and the gap capture still sees it.
    doc, logs = _cache_only_pole_doc()
    assert len(bp._gap_poles(doc, logs)) == 1, "cache lines must not hide a coverage gap"
    analysis = {"cause": "SENTINEL-LLM-CAUSE slow suite.", "breakdown": [["tests", "~300s"]],
                "evidence": ["PASS  src/foo.spec.ts (312 tests) 41200ms"],
                "prompt": "SENTINEL-LLM-PROMPT investigate the suite."}
    out = bp.render(doc, logs, analyses={"Run Checks": analysis})
    assert "SENTINEL-LLM-CAUSE" in out, "the gap-fill must not be suppressed by a cache line"
    assert bp._opt81_holding_leaf(None, bp._parse_log(logs["Run Checks"])) is None


# ---------------------------------------------------------------------------
# Review fixes: regression tests (each failed before its fix).
# ---------------------------------------------------------------------------

CATALOG = SCRIPTS.parent / "references" / "patterns"


def scan_doc(root, default_branch="main"):
    import scan
    return scan.scan(root, CATALOG, default_branch=default_branch)


def opt96(doc):
    return [f for f in doc["findings"] if f["pattern"] == "OPT96"]


def opt96_incomplete(doc):
    return [e for e in doc.get("scan_incomplete") or [] if "OPT96" in str(e.get("reason"))]


REMOTE_PUSH = ("on: {push: {branches: [main]}}\njobs:\n  build:\n"
               "    uses: someorg/shared/.github/workflows/build.yml@v1\n")


def test_unrelated_unresolved_call_neither_suppresses_nor_reports_incomplete(tmp_path):
    # One unresolved writer must not suppress an unrelated reader family, and
    # OPT96 uncertainty is never a workflow-file coverage gap.
    write_workflow(tmp_path, "ci.yml", NODE)
    write_workflow(tmp_path, "release.yml", REMOTE_PUSH)
    doc = scan_doc(tmp_path)
    assert [f["match_text"] for f in opt96(doc)] == ["actions/setup-node:pnpm"]
    assert not opt96_incomplete(doc)


def test_reader_sharing_the_unresolved_repo_is_withheld_and_disclosed(tmp_path):
    write_workflow(tmp_path, "ci.yml", NODE.replace(
        "    steps:\n", "    steps:\n      - uses: someorg/shared/.github/actions/prep@v1\n"))
    write_workflow(tmp_path, "release.yml", REMOTE_PUSH)
    doc = scan_doc(tmp_path)
    assert not opt96(doc)
    assert not opt96_incomplete(doc)
    held = doc["opt96_unresolved"]
    assert [(e["workflow_file"], e["job"]) for e in held] == [(".github/workflows/ci.yml", "test")]
    assert "someorg/shared" in held[0]["reason"]


ALL_EXPR = RESTORE.replace("deps-${{ runner.os }}-${{ hashFiles('lockfile') }}",
                           "${{ runner.os }}-${{ hashFiles('lockfile') }}").replace(
    "          restore-keys: deps-${{ runner.os }}-\n", "")


def test_all_expression_reader_with_no_writer_is_flagged(tmp_path):
    write_workflow(tmp_path, "ci.yml", ALL_EXPR)
    doc = scan_doc(tmp_path)
    assert len(opt96(doc)) == 1
    assert not opt96_incomplete(doc)


def test_all_expression_writer_is_undecidable_withheld_not_incomplete(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace(
        "deps-${{ runner.os }}-${{ hashFiles('lockfile') }}",
        "${{ runner.os }}-${{ hashFiles('lockfile') }}"))
    doc = scan_doc(tmp_path)
    assert not opt96(doc), "an undecidable match is never flagged"
    assert not opt96_incomplete(doc)
    assert [e["job"] for e in doc["opt96_unresolved"]] == ["test"]


def test_opt96_disclosure_renders_in_data_sources_not_the_coverage_banner():
    doc, logs = _cache_only_pole_doc()
    doc["opt96_unresolved"] = [{"workflow_file": ".github/workflows/ci.yml", "job": "test",
                                "family": "deps", "reason": "a remote action may save it"}]
    out = bp.render(doc, logs)
    assert "Incomplete coverage" not in out
    row = [ln for ln in out.splitlines() if ln.startswith("| cache seeding: held back |")]
    assert row and "`test`" in row[0]


def test_no_dependency_cache_note_without_opt96_sampling():
    doc, logs = _cache_only_pole_doc()
    out = bp.render(doc, logs)
    assert "Dependency cache" not in out, "a restore line alone renders no check-this note"
    pole = doc["pr_critical_path"]["poles"][0]
    _, leaf, offcat = bp._derive_pole_leaf(pole, "Run Checks", logs)
    assert leaf is None and offcat is None


class _FakeBP:
    def __init__(self, leaves):
        self.leaves = leaves

    def _parse_log(self, log, iso=None):
        return self.leaves.get(log)


class _LogClient:
    queries = errors = 0

    def __init__(self, logs):
        self.logs, self.calls = logs, []

    def text(self, endpoint, allow_missing=False):
        jid = int(endpoint.split("/jobs/")[1].split("/")[0])
        self.calls.append(jid)
        return self.logs[jid]


@pytest.mark.parametrize("with_state", [False, True])
def test_sibling_with_a_different_leaf_never_enters_the_magnitude(with_state):
    primary = {"fix_key": "turbo-partial-cache",
               "magnitude": {"label": "turbo misses", "unit": "%", "value": 60}}
    # A sibling below the turbo floor that falls through to the dependency leaf.
    dep = {"fix_key": "dependency-cache",
           "magnitude": {"label": "dependency cache misses", "unit": "%", "value": 100}}
    fake = _FakeBP({"P": primary, "S": dep, "T": primary})
    jobs = [sampled_job(i, 20 * i) for i in (1, 2, 3)]
    client = _LogClient({1: "P", 2: "T", 3: "S"})
    qual = [(float(j["id"]), j) for j in jobs]
    mag = cr._magnitude_sample(fake, client, "example/project", qual, jobs[0], "P",
                               state_fn=(lambda lg: {"miss_pct": 50.0}) if with_state else None)
    assert 100 not in [v["value"] for v in mag["values"]]


def dep_harness(tmp_path, misses=6, push_logs=None, push_jobs=None, opt96_findings=None,
                wf=".github/workflows/ci.yml"):
    miss_ids = set(range(1, misses + 1))
    jobs = [sampled_job(i, 100 if i in miss_ids else 20) for i in range(1, 9)]
    logs = {i: ("Cache not found for input keys: deps-linux-lock" if i in miss_ids
                else "Cache restored from key: deps-linux-lock") for i in range(1, 9)}
    logs.update(push_logs or {i: "Cache not found for input keys: deps-linux-lock"
                              for i in range(21, 26)})
    client = _LogClient(logs)
    pole = {"check": "test", "workflow_file": wf, "job": "test", "p50_s": 300}
    if push_jobs is None:
        push_jobs = [[sampled_job(i, 20)] for i in range(21, 26)]
    if opt96_findings is None:
        opt96_findings = [{"pattern": "OPT96", "workflow_file": wf, "affected_jobs": ["test"]}]
    manifest = cr._persist_pole_logs(client, "example/project", [pole],
                                     {wf: [[j] for j in jobs]}, tmp_path, mag_runs=8,
                                     events_jobs_by_wf={wf: {"push": push_jobs}},
                                     opt96_findings=opt96_findings)
    return pole, client, manifest


def test_opt96_pole_magnitude_stays_the_dominant_step(tmp_path):
    pole, _, manifest = dep_harness(tmp_path)
    assert pole["cache_dist"]["fix_key"] == "dependency-cache"
    mag = json.loads((tmp_path / manifest[0]["mag_file"]).read_text())
    assert mag["kind"] == "step-wall", "cache outcomes live in cache_dist, not the magnitude"


def test_dependency_cache_floor_is_mirrored_in_verify_report():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vr_opt96_floor", Path(__file__).resolve().parent / "verify_report.py")
    vr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = vr
    spec.loader.exec_module(vr)
    values = [{"cache_state": {"miss_pct": 33.33}}, {"cache_state": {"miss_pct": 50.0}}]
    cd = {"fix_key": "dependency-cache", "pr": {"values": values}, "tail": {"prevalence_max": 0.5}}
    engine = cr._cache_verdict(values, cd["tail"], floor_pct=50.0,
                               tail_min_frac=cr._CACHE_TAIL_MIN_FRAC,
                               cold_pct=cr._CACHE_COLD_MISS_PCT)
    assert engine == "miss-tail"
    assert vr._vr_cache_verdict(cd) == engine


def test_wildcard_parts_of_a_key_match_a_concrete_writer(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE.replace(
        "deps-${{ runner.os }}-${{ hashFiles('lockfile') }}",
        "${{ runner.os }}-node-${{ matrix.node }}-${{ hashFiles('p') }}").replace(
        "          restore-keys: deps-${{ runner.os }}-\n", ""))
    write_workflow(tmp_path, "warm.yml", SAVE.replace(
        "deps-${{ runner.os }}-${{ hashFiles('lockfile') }}",
        "${{ runner.os }}-node-20-${{ hashFiles('p') }}"))
    assert not opt96(scan_doc(tmp_path))


def test_remote_action_on_a_seed_event_withholds_the_family_it_names(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", "on: {push: {branches: [main]}}\njobs:\n  warm:\n"
                   "    runs-on: ubuntu-latest\n    steps:\n"
                   "      - uses: someorg/warm-cache@v1\n        with:\n"
                   "          key: deps-${{ runner.os }}\n")
    doc = scan_doc(tmp_path)
    assert not opt96(doc)
    assert [e["job"] for e in doc["opt96_unresolved"]] == ["test"]


def test_known_non_caching_actions_do_not_withhold(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", "on: {push: {branches: [main]}}\njobs:\n  warm:\n"
                   "    runs-on: ubuntu-latest\n    steps:\n"
                   "      - uses: actions/checkout@v4\n        with:\n          ref: deps\n")
    doc = scan_doc(tmp_path)
    assert len(opt96(doc)) == 1
    assert not doc.get("opt96_unresolved")


def test_null_branches_ignore_does_not_crash_the_scan(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace(
        "{push: {branches: [main]}}", "{push: {branches-ignore: }}"))
    scan_doc(tmp_path)


def test_tags_with_branches_ignore_still_runs_on_main(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace(
        "{push: {branches: [main]}}", "{push: {tags: ['v*'], branches-ignore: ['feature/*']}}"))
    assert not opt96(scan_doc(tmp_path))


def test_string_branches_ignore_is_one_pattern(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace(
        "{push: {branches: [main]}}", "{push: {branches-ignore: main}}"))
    assert opt96(scan_doc(tmp_path)), "a warmer that ignores main never seeds it"


@pytest.mark.parametrize("trigger", ["{workflow_run: {workflows: [CI], types: [completed]}}",
                                     "{repository_dispatch: {types: [warm]}}"])
def test_default_branch_context_events_seed(tmp_path, trigger):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace("{push: {branches: [main]}}", trigger))
    assert not opt96(scan_doc(tmp_path))


def test_manual_warmer_is_disclosed_in_the_evidence(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace("{push: {branches: [main]}}",
                                                      "workflow_dispatch"))
    hits = opt96(scan_doc(tmp_path))
    assert len(hits) == 1
    assert "workflow_dispatch" in hits[0]["evidence"]


@pytest.mark.parametrize("push_jobs", [
    [[dict(sampled_job(i, 20), conclusion=None)] for i in range(21, 26)],   # in progress
    [[dict(sampled_job(i, 20), name="test (ubuntu)")] for i in range(21, 26)],  # matrix name
])
def test_unverifiable_push_jobs_never_confirm(tmp_path, push_jobs):
    pole, _, _ = dep_harness(tmp_path, push_jobs=push_jobs)
    assert pole["cache_dist"]["confirmed"] is False


def _pr_only_cd():
    return {"fix_key": "dependency-cache", "confirmed": False, "push": None,
            "push_reason": "no push runs sampled for this workflow",
            "pr": {"upstream_n": 8, "upstream_median": 100},
            "sizing": {"credited": True, "step": "Install", "miss_time_s": 100, "saving_s": 60}}


@pytest.mark.parametrize("on,gaps", [
    ({"pull_request": {}, "push": {"branches": ["main"]}}, set()),
    ({"pull_request": {}}, {"ci.yml"}),
])
def test_pr_only_shortcut_needs_a_truly_pr_only_workflow(on, gaps):
    f = {"pattern": "OPT96", "workflow_file": "ci.yml", "affected_jobs": ["test"]}
    cd = _pr_only_cd()
    cr._ground_opt96([f], [{"workflow_file": "ci.yml", "job": "test", "cache_dist": cd}],
                     {"ci.yml": {"on": on, "jobs": {"test": {}}}}, fetch_gaps=gaps)
    assert f["wall_clock_p50_s"] is None
    assert "push probe unavailable" in f["size_note"]


def test_origin_head_branch_is_labelled_possibly_stale(tmp_path, monkeypatch):
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "symbolic-ref", "refs/remotes/origin/HEAD",
                    "refs/remotes/origin/develop"], check=True)
    write_workflow(tmp_path, "ci.yml", RESTORE)
    hits = opt96(scan_doc(tmp_path, default_branch=None))
    assert "local origin/HEAD (may be stale)" in hits[0]["evidence"]


def test_gh_default_branch_mismatch_demotes_and_discloses():
    f = {"pattern": "OPT96", "workflow_file": "ci.yml", "affected_jobs": ["test"],
         "match_text": "deps"}
    doc = {"findings": [f], "opt96_default_branch": {"name": "main", "source": "origin-head"}}
    cr._opt96_check_default_branch(doc, "develop")
    cd = dict(_pr_only_cd(), confirmed=True, recommendation="seed the default-branch cache")
    cr._ground_opt96([f], [{"workflow_file": "ci.yml", "job": "test", "cache_dist": cd}],
                     {"ci.yml": {"on": {"pull_request": {}}}})
    assert f["wall_clock_p50_s"] is None
    assert "develop" in f["size_note"]
    assert any("develop" in e["reason"] for e in doc["opt96_unresolved"])


def test_reusable_workflow_reader_grounds_on_the_callers_pole(tmp_path):
    write_workflow(tmp_path, "child.yml", NODE.replace("on: {pull_request: {}}", "on: workflow_call"))
    write_workflow(tmp_path, "ci.yml", "on: pull_request\njobs:\n  call:\n"
                   "    uses: ./.github/workflows/child.yml\n")
    [f] = opt96(scan_doc(tmp_path))
    pole = {"workflow_file": ".github/workflows/ci.yml", "job": "call / test"}
    assert cr._opt96_pole_matches(f, pole)
    assert not cr._opt96_pole_matches(f, dict(pole, job="call / lint"))


@pytest.mark.parametrize("probe,reason", [
    (None, "pole log not fetched"),
    ("other-leaf:turbo-partial-cache", "another detector"),
    ("no-restore-line", "confirmation failed"),
])
def test_unconfirmed_size_note_names_its_reason(probe, reason):
    f = {"pattern": "OPT96", "workflow_file": "ci.yml", "affected_jobs": ["test"]}
    pole = {"workflow_file": "ci.yml", "job": "test"}
    if probe:
        pole["opt96_probe"] = probe
    cr._ground_opt96([f], [pole])
    assert reason in f["size_note"]
    g = {"pattern": "OPT96", "workflow_file": "ci.yml", "affected_jobs": ["other"]}
    cr._ground_opt96([g], [pole])
    assert "no matching pole" in g["size_note"]


# ---------------------------------------------------------------------------
# Coverage for behavior the mutation run showed untested (T1-T8).
# ---------------------------------------------------------------------------

MISS = "Cache not found for input keys: deps-linux-lock"
HIT = "Cache restored from key: deps-linux-lock"


def test_push_miss_that_saves_does_not_confirm(tmp_path):
    pole, _, _ = dep_harness(tmp_path, push_logs={
        i: MISS + "\nPost job cleanup.\nCache saved successfully" for i in range(21, 26)})
    cd = pole["cache_dist"]
    assert cd["push"]["median"] == 100
    assert cd["confirmed"] is False, "a push run that saves seeds the default branch"
    f = {"pattern": "OPT96", "workflow_file": pole["workflow_file"], "affected_jobs": ["test"]}
    cr._ground_opt96([f], [pole])
    assert f["wall_clock_p50_s"] is None


def test_low_pr_miss_median_never_confirms(tmp_path):
    # Push runs all miss and save nothing, but most PR runs hit: below the 50% floor.
    pole, _, _ = dep_harness(tmp_path, misses=3)
    assert pole["cache_dist"]["pr"]["upstream_median"] < cr._DEPENDENCY_CACHE_FLOOR_PCT
    assert pole["cache_dist"]["confirmed"] is False


def test_cancelled_push_job_never_confirms(tmp_path):
    # A cancelled job that started has a partial log, but its post-step save never
    # had its chance: it is not evidence that nothing saves.
    pole, _, _ = dep_harness(tmp_path, push_jobs=[
        [dict(sampled_job(i, 20), conclusion="cancelled")] for i in range(21, 26)])
    assert pole["cache_dist"]["confirmed"] is False


@pytest.mark.parametrize("log,saved", [
    (MISS + "\nCache saved successfully", True),
    (MISS + "\nCache saved with key: deps-linux-lock", True),
    (MISS, False),
])
def test_cache_state_records_whether_the_log_saved(log, saved):
    assert bp._cache_state_of_log(log, "dependency-cache")["saved"] is saved


@pytest.mark.parametrize("change", [
    {"pr": {"upstream_n": 1, "upstream_median": 100}},
    {"pr": {"upstream_n": 8, "upstream_median": 30}},
    {"push_reason": "1 of 1 push log fetch(es) failed"},
])
def test_pr_only_shortcut_negatives_stay_unconfirmed(change):
    f = {"pattern": "OPT96", "workflow_file": "ci.yml", "affected_jobs": ["test"]}
    cd = dict(_pr_only_cd(), **change)
    cr._ground_opt96([f], [{"workflow_file": "ci.yml", "job": "test", "cache_dist": cd}],
                     {"ci.yml": {"on": {"pull_request": {}}}})
    assert cd["confirmed"] is False
    assert f["wall_clock_p50_s"] is None


def test_confirmed_but_uncredited_sizing_credits_nothing():
    f = {"pattern": "OPT96", "workflow_file": "ci.yml", "affected_jobs": ["test"]}
    cd = {"fix_key": "dependency-cache", "confirmed": True,
          "recommendation": "seed the default-branch cache",
          "sizing": {"credited": False, "step": "Install", "miss_time_s": 100,
                     "reason": "no hit sample for the same install step"}}
    cr._ground_opt96([f], [{"workflow_file": "ci.yml", "job": "test", "cache_dist": cd}])
    assert f["wall_clock_p50_s"] is None
    assert f["size_note"].endswith("savings uncredited")


def test_writer_for_a_different_family_does_not_clear_a_reader(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace("key: deps-", "key: tools-"))
    assert [f["match_text"] for f in findings(tmp_path)] == ["deps"]


REVISED = RESTORE.replace("key: deps-${{ runner.os }}", "key: deps-v2-${{ runner.os }}")


def test_restore_keys_prefix_accepts_an_older_family(tmp_path):
    write_workflow(tmp_path, "ci.yml", REVISED.replace(
        "restore-keys: deps-${{ runner.os }}-", "restore-keys: deps-"))
    write_workflow(tmp_path, "warm.yml", SAVE.replace("key: deps-", "key: deps-v1-"))
    assert not findings(tmp_path), "restore-keys `deps-` restores a `deps-v1-` save"
    write_workflow(tmp_path, "ci.yml", REVISED.replace(
        "          restore-keys: deps-${{ runner.os }}-\n", ""))
    assert findings(tmp_path), "without restore-keys the v1 save never matches v2"


@pytest.mark.parametrize("warmer", [
    SAVE.replace("actions/cache/save@v4", "actions/cache/restore@v4"),
    SAVE.replace("actions/cache/save@v4", "actions/cache@v4").replace(
        "          path:", "          lookup-only: true\n          path:"),
])
def test_non_writers_on_push_leave_the_finding(tmp_path, warmer):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", warmer)
    assert findings(tmp_path)


def test_read_only_setup_cache_on_push_leaves_the_finding(tmp_path):
    write_workflow(tmp_path, "ci.yml", NODE)
    write_workflow(tmp_path, "main.yml", NODE.replace(
        "on: {pull_request: {}}", "on: {push: {branches: [main]}}").replace(
        "          cache: pnpm\n", "          cache: pnpm\n          cache-read-only: true\n"))
    assert findings(tmp_path)


@pytest.mark.parametrize("trigger,flagged", [
    ("{push: {tags: ['v*']}}", True),
    ("{push: {branches-ignore: [main]}}", True),
    ("{push: {branches: ['**', '!main']}}", True),
    ("{push: {branches: [develop]}}", True),
    ("{push: {branches-ignore: ['feature/*']}}", False),
])
def test_push_filters_decide_whether_the_warmer_runs_on_main(tmp_path, trigger, flagged):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace("{push: {branches: [main]}}", trigger))
    assert bool(findings(tmp_path)) is flagged


def test_repo_default_branch_replaces_the_main_master_guess(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace("[main]", "[develop]"))
    assert not opt96(scan_doc(tmp_path, default_branch="develop"))
    write_workflow(tmp_path, "warm.yml", SAVE)
    [f] = opt96(scan_doc(tmp_path, default_branch="develop"))
    assert "from repo metadata" in f["evidence"] and "assumed" not in f["evidence"]


def test_default_branch_cli_flag(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace("[main]", "[develop]"))
    assert not findings(tmp_path, default_branch="develop")
    assert findings(tmp_path, default_branch="main")


def test_origin_head_fallback_picks_the_branch(tmp_path, monkeypatch):
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "symbolic-ref", "refs/remotes/origin/HEAD",
                    "refs/remotes/origin/develop"], check=True)
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace("[main]", "[develop]"))
    assert not findings(tmp_path, default_branch=None), "origin/HEAD names develop"


def test_pole_in_another_workflow_never_borrows_sizing():
    f = {"pattern": "OPT96", "workflow_file": ".github/workflows/ci.yml", "affected_jobs": ["test"]}
    assert cr._opt96_pole_matches(f, {"workflow_file": ".github/workflows/ci.yml", "job": "test"})
    assert not cr._opt96_pole_matches(
        f, {"workflow_file": ".github/workflows/nightly.yml", "job": "test"})


def test_named_job_is_sampled_through_persist_pole_logs(tmp_path):
    wf = ".github/workflows/ci.yml"
    miss_ids = set(range(1, 7))
    jobs = [dict(sampled_job(i, 100 if i in miss_ids else 20), name="Unit tests")
            for i in range(1, 9)]
    logs = {i: (MISS if i in miss_ids else HIT) for i in range(1, 9)}
    logs.update({i: MISS for i in range(21, 26)})
    client = _LogClient(logs)
    pole = {"check": "Unit tests", "workflow_file": wf, "job": "Unit tests", "p50_s": 300}
    cr._persist_pole_logs(
        client, "example/project", [pole], {wf: [[j] for j in jobs]}, tmp_path, mag_runs=8,
        events_jobs_by_wf={wf: {"push": [[dict(sampled_job(i, 20), name="Unit tests")]
                                         for i in range(21, 26)]}},
        opt96_findings=[{"pattern": "OPT96", "workflow_file": wf, "affected_jobs": ["test"]}],
        wf_docs={wf: {"jobs": {"test": {"name": "Unit tests"}}}})
    assert pole["cache_dist"]["fix_key"] == "dependency-cache"


def test_finding_for_another_job_triggers_no_sampling(tmp_path):
    pole, client, _ = dep_harness(tmp_path, opt96_findings=[
        {"pattern": "OPT96", "workflow_file": ".github/workflows/ci.yml",
         "affected_jobs": ["lint"]}])
    assert "cache_dist" not in pole
    assert client.calls == [pole_id for pole_id in client.calls if pole_id < 21]
    assert len(client.calls) == 1


def test_job_level_false_writer_is_not_a_writer(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace("    runs-on:", "    if: false\n    runs-on:"))
    assert findings(tmp_path)


@pytest.mark.parametrize("condition", ["github.event_name == 'pull_request'",
                                       "${{ github.event_name == 'pull_request' }}"])
def test_wrapped_and_bare_conditions_agree(tmp_path, condition):
    write_workflow(tmp_path, "ci.yml", RESTORE)
    write_workflow(tmp_path, "warm.yml", SAVE.replace(
        "      - uses:", f'      - if: "{condition}"\n        uses:'))
    assert findings(tmp_path), "a PR-only save does not seed, wrapped or bare"


def test_main_only_reader_step_is_not_a_pr_reader(tmp_path):
    write_workflow(tmp_path, "ci.yml", RESTORE.replace(
        "      - uses:", "      - if: github.ref == 'refs/heads/main'\n        uses:"))
    assert not findings(tmp_path)


def test_cache_false_reader_is_not_a_reader(tmp_path):
    write_workflow(tmp_path, "ci.yml", NODE.replace("cache: pnpm", "cache: false"))
    assert not findings(tmp_path)


def test_setup_go_v3_has_no_default_cache(tmp_path):
    write_workflow(tmp_path, "ci.yml", (GO % ("pull_request: {}", "")).replace("@v5", "@v3"))
    assert not findings(tmp_path)
    write_workflow(tmp_path, "ci.yml", (GO % ("pull_request: {}", "")).replace("@v5", "@v4"))
    assert findings(tmp_path)


def test_fork_samples_are_excluded_from_the_miss_share():
    samples = [{"cache_state": {"miss_pct": 100, "restore_count": 1},
                "install_steps": {"Install": 100}},
               {"cache_state": {"miss_pct": 0, "restore_count": 1},
                "install_steps": {"Install": 20}}]
    upstream_only = cr._dependency_cache_sizing(samples, 0.5)
    assert upstream_only["saving_s"] == pytest.approx(40)


def test_fork_runs_do_not_count_toward_confirmation(tmp_path):
    # Fork PRs read only the base branch, so they run cold. Drop them all and the
    # remaining upstream runs mostly hit: no confirmation, a small miss share.
    wf = ".github/workflows/ci.yml"
    jobs = []
    for i in range(1, 9):
        j = sampled_job(i, 100 if i <= 6 else 20)
        j["_run_head_repo"] = "someone/fork" if i <= 5 else "example/project"
        jobs.append(j)
    logs = {i: (MISS if i <= 6 else HIT) for i in range(1, 9)}
    logs.update({i: MISS for i in range(21, 26)})
    client = _LogClient(logs)
    pole = {"check": "test", "workflow_file": wf, "job": "test", "p50_s": 300}
    cr._persist_pole_logs(client, "example/project", [pole], {wf: [[j] for j in jobs]},
                          tmp_path, mag_runs=8,
                          events_jobs_by_wf={wf: {"push": [[sampled_job(i, 20)]
                                                           for i in range(21, 26)]}},
                          opt96_findings=[{"pattern": "OPT96", "workflow_file": wf,
                                           "affected_jobs": ["test"]}])
    cd = pole["cache_dist"]
    assert cd["pr"]["fork_n"] >= 1
    assert cd["sizing"]["miss_share"] < 0.5


def test_push_median_100_with_one_restore_is_check_this(tmp_path):
    pole, _, _ = dep_harness(tmp_path, push_logs={21: MISS, 22: HIT, 23: MISS,
                                                  24: MISS, 25: MISS})
    cd = pole["cache_dist"]
    assert cd["push"]["median"] == 100
    assert cd["confirmed"] is False
    assert cd["recommendation"] == "check this"


def test_apply_cache_dist_respects_the_static_gate():
    leaf = bp._parse_log(MISS)
    cd = {"fix_key": "dependency-cache", "recommendation": "seed the default-branch cache"}
    assert bp._apply_cache_dist(dict(leaf), cd)["unit_label"] == "dependency cache: check this"
    gated = bp._apply_cache_dist(dict(leaf), dict(cd, static_confirmed=True))
    assert gated["unit_label"] == "dependency cache: seed the default-branch cache"


def test_collect_grounds_opt96_before_sizing_within_its_workflow(tmp_path, monkeypatch):
    # End to end through collect(): the pole logs are captured BEFORE sizing, so
    # the confirmed measurement enters the ordinary cascade, and the
    # within-workflow bound caps it at the workflow's own measured wall.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ci_speedup_opt96_collect_fakes",
        Path(__file__).resolve().parent / "test_disclosure_reaches_the_artifact.py")
    fakes = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fakes
    spec.loader.exec_module(fakes)
    seen = {}

    def _persist(client, repo, poles, *a, **kw):
        seen["opt96"] = kw.get("opt96_findings")
        for p in poles:
            if p.get("job") == "Test suite":
                p["cache_dist"] = {
                    "fix_key": "dependency-cache", "confirmed": True,
                    "recommendation": "seed the default-branch cache", "push": None,
                    "push_reason": None, "pr": {"upstream_n": 8, "upstream_median": 100},
                    "sizing": {"credited": True, "step": "Install", "miss_time_s": 9000,
                               "saving_s": 9000.0}}
        return []

    monkeypatch.setattr(cr, "GhClient", fakes._TwoWorkflowClient)
    monkeypatch.setattr(cr, "_persist_pole_logs", _persist)
    finding = {"id": "f9", "pattern": "OPT96", "workflow_file": fakes._GATE,
               "affected_jobs": ["Test suite"], "evidence": "cache family `deps`"}
    doc = {"repo": "o/r", "findings": [finding]}
    cr.collect(doc, "o/r", max_runs=8, shallow_runs=8, with_logs=True,
               data_dir=tmp_path / "bundle")
    assert [f["id"] for f in seen["opt96"]] == ["f9"]
    [f] = [f for f in doc["findings"] if f.get("pattern") == "OPT96"]
    assert f["cache_dist"]["static_confirmed"] is True
    assert f["wall_clock_p50_s"] is None
    assert f["cache_sizing_guard"]["status"] == "not_measured"
    assert "not measured" in f["size_note"]

"""Default-branch cache seeding: workflow reachability and sampled miss cost."""

from __future__ import annotations

import json
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


def findings(root):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "scan.py"), "--root", str(root)],
        check=True, capture_output=True, text=True,
    )
    return [f for f in json.loads(result.stdout)["findings"] if f["pattern"] == "OPT96"]


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
    hits = findings(tmp_path)
    assert len(hits) == 1, "PR-only dependency caches need an OPT96 finding"
    evidence = json.dumps(hits[0])
    assert "pnpm" in evidence
    assert "no save on push to the default branch" in evidence
    assert "assumed" in evidence


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


def test_local_reusable_workflow_inherits_callers_and_unresolved_suppresses(tmp_path):
    write_workflow(tmp_path, "child.yml", NODE.replace("on: {pull_request: {}}", "on: workflow_call"))
    caller = "on: pull_request\njobs:\n  call:\n    uses: ./.github/workflows/child.yml\n"
    write_workflow(tmp_path, "ci.yml", caller)
    assert findings(tmp_path), "PR caller must expose the reusable cache reader"
    write_workflow(tmp_path, "ci.yml", caller.replace("on: pull_request", "on: [pull_request, push]"))
    assert not findings(tmp_path)
    write_workflow(tmp_path, "ci.yml", caller)
    write_workflow(tmp_path, "warm.yml", "on: push\njobs:\n  call:\n    uses: ./.github/workflows/missing.yml\n")
    assert not findings(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPTS / "scan.py"), "--root", str(tmp_path)],
                            capture_output=True, text=True, check=True)
    assert any("OPT96" in entry["reason"] and "unresolved" in entry["reason"]
               for entry in json.loads(result.stdout)["scan_incomplete"])


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
    assert hits, "a merge-queue-only save leaves new PR branches cold"
    assert "merge_group" not in json.dumps(hits[0]["evidence"]), \
        "the evidence must not list merge_group as a seed event"


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
    cr._ground_opt96([f], [{'workflow_file': 'ci.yml', 'job': 'test', 'cache_dist': cd}])
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
    # An unreadable local action can hide a writer: no flag, disclosed.
    write_workflow(tmp_path, "main.yml", "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                   "    steps:\n      - uses: ./.github/actions/missing\n")
    result = subprocess.run([sys.executable, str(SCRIPTS / "scan.py"), "--root", str(tmp_path)],
                            capture_output=True, text=True, check=True)
    doc = json.loads(result.stdout)
    assert not [f for f in doc["findings"] if f["pattern"] == "OPT96"]
    assert any("OPT96" in entry["reason"] and "local action" in entry["reason"]
               for entry in doc["scan_incomplete"])


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

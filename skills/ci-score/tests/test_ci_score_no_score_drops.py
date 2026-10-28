"""The two frozen calibration controls, fact by fact, and OD-CS23 against the base engine.

OD-CS23 states that both frozen controls keep every one of the eleven facts
identical, state AND evidence, and that no score can drop. Both are asserted
here rather than stated: `_FROZEN_FACTS` is the full fact set the current
engine produces for each control (recomputed by running the collector over
the frozen trees; never hand-written; measured identical to the pre-OD-CS23
engine's output when it was recorded), and the same trees are scored by the
pre-OD-CS23 engine (`_baseline_engine`) to check no fact moved away from
credit and neither score fell. The same direction check runs over an explicit
list of OD-CS23 cases (`OD_CS23_CASES`), with `INTENDED_DROPS` as the
allowlist later changes extend on purpose. Where the base engine cannot be
read (no repository around this copy), those tests skip visibly.

The one documented exception to "no score can drop" is not a drop: a
repository whose only applicable check was job timeouts (every job delegated
cross-repo) moves from a 0 grade to the all-not-applicable refusal. At the
fact level that is fail -> not_applicable, which `TOWARD_CREDIT` allows.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from _baseline_engine import assert_toward_credit, baseline_engine
from _fixture_checkouts import materialize

_SKILL_DIR = Path(__file__).resolve().parents[1]


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


cc_mod = _load("ci_score_collect_config_no_score_drops", "scripts/collect_config.py")

_FROZEN_FACTS = {'mastra': {'ci.cache.dependency-cache': ('pass',
                                          'dependency caching configured in 4 workflow/action '
                                          'file(s)'),
            'ci.cache.build-cache': ('pass',
                                     'turbo detected; cache mechanism configured in 11 '
                                     'file(s)'),
            'ci.checkout.shallow-clone': ('pass',
                                          '9 job(s) in PR-triggered workflow(s) check out full '
                                          'history and are exempt: 9 walk(s) git history, so '
                                          'shallowing the checkout would break the job; no '
                                          'other PR-gating job takes full history'),
            'ci.parallel.test-sharding': ('pass',
                                          'shard/matrix strategy on test job(s) in 6 '
                                          'workflow(s)'),
            'ci.build.change-scoped': ('pass',
                                       'change-scoped build/test mechanisms in 3 file(s)'),
            'ci.trigger.concurrency-groups': ('pass',
                                              'concurrency group declared on 6 of 8 PR '
                                              'workflow(s)'),
            'ci.trigger.cancel-superseded': ('pass',
                                             'concurrency + cancel-in-progress on 6 of 8 PR '
                                             'workflow(s)'),
            'ci.trigger.path-filter': ('pass', 'paths filters on 2 of 8 PR workflow(s)'),
            'ci.hygiene.job-timeouts': ('pass', 'timeout-minutes set in 13 workflow(s)'),
            'ci.security.scoped-id-token': ('pass',
                                            'id-token: write scoped at job level in 1 '
                                            'workflow(s)'),
            'ci.security.pinned-action-shas': ('fail',
                                               '106 of 125 remote action references SHA-pinned '
                                               '(workflow + local composite action files; '
                                               'passes at >=95%)')},
 'better-auth': {'ci.cache.dependency-cache': ('pass',
                                               'dependency caching configured in 6 '
                                               'workflow/action file(s)'),
                 'ci.cache.build-cache': ('pass',
                                          'turbo detected; cache mechanism configured in 5 '
                                          'file(s)'),
                 'ci.checkout.shallow-clone': ('fail',
                                               'fetch-depth: 0 on 4 PR-gating workflow(s), '
                                               'e.g. .github/workflows/ci.yml, '
                                               '.github/workflows/demo.yml, '
                                               '.github/workflows/e2e.yml'),
                 'ci.parallel.test-sharding': ('pass',
                                               'shard/matrix strategy on test job(s) in 2 '
                                               'workflow(s)'),
                 'ci.build.change-scoped': ('fail',
                                            'CI builds/tests run unscoped (no '
                                            '--filter=...[base], affected/changed modes, or '
                                            'changed-files step)'),
                 'ci.trigger.concurrency-groups': ('pass',
                                                   'concurrency group declared on 5 of 10 PR '
                                                   'workflow(s)'),
                 'ci.trigger.cancel-superseded': ('pass',
                                                  'concurrency + cancel-in-progress on 4 of 10 '
                                                  'PR workflow(s)'),
                 'ci.trigger.path-filter': ('pass', 'paths filters on 1 of 10 PR workflow(s)'),
                 'ci.hygiene.job-timeouts': ('pass', 'timeout-minutes set in 2 workflow(s)'),
                 'ci.security.scoped-id-token': ('pass',
                                                 'id-token: write scoped at job level in 2 '
                                                 'workflow(s)'),
                 'ci.security.pinned-action-shas': ('pass',
                                                    '77 of 77 remote action references '
                                                    'SHA-pinned (workflow + local composite '
                                                    'action files; passes at >=95%)')}}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(root)})


def _control(name: str, tmp_path: Path) -> Path:
    root = tmp_path / name
    root.mkdir()
    materialize(name, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")  # throwaway fixture repo, not a working tree
    _git(root, "commit", "-qm", "fixture")
    return root


def _collect(root: Path, engine=None) -> dict:
    if engine is None:
        doc, code = cc_mod.collect(root)
    else:
        real = cc_mod._load_sibling

        def load(mod_name, filename):
            return engine if filename == "practice_facts.py" else real(mod_name, filename)

        cc_mod._load_sibling = load
        try:
            doc, code = cc_mod.collect(root)
        finally:
            cc_mod._load_sibling = real
    assert code == 0, doc.get("data_sources")
    return doc


@pytest.mark.parametrize("name", sorted(_FROZEN_FACTS))
def test_every_fact_of_a_frozen_control_is_pinned(name, tmp_path):
    doc = _collect(_control(name, tmp_path))
    got = {k: (v["state"], v["evidence"]) for k, v in doc["practice_facts"].items()}
    assert got == _FROZEN_FACTS[name]
    assert "unresolved_setup_defaults" not in doc["data_sources"]


def _base_or_skip():
    base = baseline_engine()
    if base is None:
        pytest.skip("no repository around this copy: the base engine cannot be read "
                    "from git history, so the comparison does not run here")
    return base


@pytest.mark.parametrize("name", sorted(_FROZEN_FACTS))
def test_no_control_fact_or_score_moves_away_from_credit(name, tmp_path):
    """`_FROZEN_FACTS` above pins every fact exactly; this adds the direction
    check and the score against the base engine."""
    base = _base_or_skip()
    root = _control(name, tmp_path)
    before, after = _collect(root, base), _collect(root)
    assert_toward_credit(before["practice_facts"], after["practice_facts"], f"{name}: ")
    assert after["ci_score"]["value"] >= before["ci_score"]["value"]


# ---- OD-CS23 cases, through both engines ------------------------------------
#
# An explicit list: each OD-CS23 change's credited shape and the withheld
# shapes next to it. Every case is scored by the base engine and the current
# one, and no fact may move away from credit. Not every test in the suite runs
# through the base engine (later changes may move verdicts on purpose); a later
# PR that drops a verdict here on purpose adds it to INTENDED_DROPS with the
# owner decision or PR that made the call.
_SHA = "8f4b7f84864484a7bf31766abe9204da3cbe65b3"
_PR_ON = "on:\n  pull_request:\n"
_GO = {"go.mod": "module x\n", "go.sum": ""}
_UV = {"pyproject.toml": "[project]\n"}


def _wf(*steps: str, runs_on: str = "ubuntu-latest") -> str:
    body = "".join(f"      - {s}\n" for s in steps)
    return _PR_ON + f"jobs:\n  test:\n    runs-on: {runs_on}\n    steps:\n" + body


_CO = "uses: actions/checkout@v5"
OD_CS23_CASES = {
    # 1. dependency caching on by default, and the shapes it is withheld from
    "go-v5": (_GO, [_wf(_CO, "uses: actions/setup-go@v5")]),
    "go-sha-v5": (_GO, [_wf(_CO, f"uses: actions/setup-go@{_SHA} # v5")]),
    "go-sha-v3": (_GO, [_wf(_CO, f"uses: actions/setup-go@{_SHA} # v3")]),
    "go-sha-date": (_GO, [_wf(_CO, f"uses: actions/setup-go@{_SHA} # 2024-05-01")]),
    "go-no-checkout": (_GO, [_wf("uses: actions/setup-go@v5")]),
    "go-dep-path-expr": ({"go.mod": "module x\n"}, [_wf(
        _CO, "{uses: actions/setup-go@v5, with: {cache-dependency-path: '${{ inputs.p }}'}}")]),
    "uv-v6": (_UV, [_wf(_CO, "uses: astral-sh/setup-uv@v6")]),
    "uv-self-hosted": (_UV, [_wf(_CO, "uses: astral-sh/setup-uv@v6", runs_on="self-hosted")]),
    "uv-expr-runner": (_UV, [_wf(_CO, "uses: astral-sh/setup-uv@v6",
                                 runs_on="${{ matrix.os }}")]),
    "ruby-bundler": ({"Gemfile": "source 'x'\n"},
                     [_wf(_CO, "{uses: ruby/setup-ruby@v1, with: {bundler-cache: true}}")]),
    "gradle": ({"build.gradle": ""}, [_wf(_CO, "uses: gradle/actions/setup-gradle@v4")]),
    "node-v5-npm": ({"package.json": '{"packageManager": "npm@10.9.0"}'},
                    [_wf(_CO, "uses: actions/setup-node@v5")]),
    "node-v6-dev": ({"package.json": '{"devEngines": {"packageManager": {"name": "npm"}}}'},
                    [_wf(_CO, "uses: actions/setup-node@v6")]),
    "node-sha-yarn-v5": ({"package.json": '{"packageManager": "yarn@4.1.0"}'},
                         [_wf(_CO, f"uses: actions/setup-node@{_SHA} # v5")]),
    "node-sha-yarn-v6": ({"package.json": '{"packageManager": "yarn@4.1.0"}'},
                         [_wf(_CO, f"uses: actions/setup-node@{_SHA} # v6")]),
    "node-bad-json": ({"package.json": "not json"}, [_wf(_CO, "uses: actions/setup-node@v5")]),
    "node-racing": ({"package.json": '{"packageManager": "npm@10.9.0"}'}, [_wf(
        "{uses: actions/checkout@v5, background: true}", "uses: actions/setup-node@v5")]),
    # explicit inputs in any case
    "node-v4-Cache-npm": ({"package.json": "{}"},
                          [_wf(_CO, "{uses: actions/setup-node@v4, with: {Cache: npm}}")]),
    "uv-v4-Enable-Cache": (_UV, [_wf(_CO, "{uses: astral-sh/setup-uv@v4, "
                                          "with: {Enable-Cache: true}}")]),
    "go-v5-Cache-false": (_GO, [_wf(_CO, "{uses: actions/setup-go@v5, with: {Cache: false}}")]),
    # 2. a matrix generated at runtime
    "generated-matrix": ({}, [_PR_ON + "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
                          "    strategy:\n      matrix: ${{ fromJSON(needs.plan.outputs.m) }}\n"
                          "    steps:\n      - run: pytest\n"]),
    # 3. every job delegated to a cross-repo reusable workflow
    "all-delegated": ({}, [_PR_ON + "jobs:\n"
                       "  ci:\n    uses: my-org/workflows/.github/workflows/ci.yml@v1\n"]),
    # 4. a full-history checkout step that never runs on a pull request
    "step-if-push": ({}, ["on: [pull_request, push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                          "    steps:\n      - uses: actions/checkout@v4\n"
                          "        if: github.event_name == 'push'\n"
                          "        with:\n          fetch-depth: 0\n      - run: pytest\n"]),
}
# {(case_id, check): "<owner decision / PR that made the drop on purpose>"}
INTENDED_DROPS: dict[tuple[str, str], str] = {
    ("all-delegated", "ci.checkout.shallow-clone"):
        "OD-CS26: every PR-gating job calls an external reusable workflow; "
        "checkout is not visible, so pass becomes not_applicable",
    ("all-delegated", "ci.security.pinned-action-shas"):
        "OD-CS24 / #156: a job-level call to another repository's reusable "
        "workflow is a remote reference and counts for pinning, pinned only by "
        "a full SHA; a repository whose only remote reference is such a call "
        "on a tag moves not_applicable -> fail ('0 of 1')",
}

_pf_now = _load("ci_score_practice_facts_no_score_drops", "scripts/practice_facts.py")


@pytest.mark.parametrize("case_id", sorted(OD_CS23_CASES))
def test_no_od_cs23_case_moves_away_from_credit(case_id, tmp_path):
    base = _base_or_skip()
    files, workflows = OD_CS23_CASES[case_id]
    for rel, text in files.items():
        (tmp_path / rel).write_text(text)
    import yaml
    parsed = [(f"w{i}.yml", yaml.safe_load(raw) or {}, raw) for i, raw in enumerate(workflows)]
    allowed = frozenset(check for (cid, check) in INTENDED_DROPS if cid == case_id)
    assert_toward_credit(base._practice_facts(parsed, tmp_path),
                         _pf_now._practice_facts(parsed, tmp_path), f"{case_id}: ", allowed)


def test_intended_drops_name_listed_cases():
    for case_id, check in INTENDED_DROPS:
        assert case_id in OD_CS23_CASES, case_id
        assert INTENDED_DROPS[(case_id, check)].strip(), (case_id, check)


def test_the_base_engine_takes_a_newer_collectors_arguments(tmp_path):
    """A later collector may pass more arguments (e.g. `parse_errors`); the
    old two-argument engine must still run under it."""
    base = _base_or_skip()
    parsed = [("ci.yml", {"on": "pull_request", "jobs": {}}, "")]
    assert base._practice_facts(parsed, tmp_path, [], parse_errors=[]) == \
        base._practice_facts(parsed, tmp_path)
    assert base._unresolved_setup_defaults(parsed, tmp_path) == []


def test_an_unreadable_base_commit_raises_with_gits_error(monkeypatch):
    """Inside a repository, a base commit git cannot read is an error, never a
    silent None that would turn the comparison off."""
    import _baseline_engine as be
    if not (be._REPO_ROOT / ".git").exists():
        pytest.skip("no repository around this copy: nothing to read the base engine from")
    monkeypatch.setattr(be, "_cached", [])
    monkeypatch.setattr(be, "BASE_COMMIT", "0" * 40)
    with pytest.raises(AssertionError, match="cannot be read from this checkout.*fatal"):
        be.baseline_engine()


def test_no_repository_means_no_base_engine(monkeypatch, tmp_path):
    import _baseline_engine as be
    monkeypatch.setattr(be, "_cached", [])
    monkeypatch.setattr(be, "_REPO_ROOT", tmp_path)
    assert be.baseline_engine() is None

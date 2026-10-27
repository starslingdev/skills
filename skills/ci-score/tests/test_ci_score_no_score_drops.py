"""The two frozen calibration controls, fact by fact, and against the base engine.

OD-CS23 states that both frozen controls keep every one of the eleven facts
identical, state AND evidence, and that no score can drop. Both are asserted
here rather than stated: `_FROZEN_FACTS` is the full fact set the current
engine produces for each control (recomputed by running the collector over
the frozen trees; never hand-written), and the same trees are scored by the
pre-OD-CS23 engine (`_baseline_engine`) to prove no fact moved away from
credit and neither score fell. The per-case version of the second check runs
inside `test_ci_score_undeserved_fails.facts_for`.

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

        if not hasattr(engine, "_unresolved_setup_defaults"):
            engine._unresolved_setup_defaults = lambda *_a: []   # added by OD-CS23
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


@pytest.mark.parametrize("name", sorted(_FROZEN_FACTS))
def test_no_control_fact_or_score_moves_away_from_credit(name, tmp_path):
    base = baseline_engine()
    if base is None:
        pytest.skip("installed copy: no git history to read the base engine from")
    root = _control(name, tmp_path)
    before, after = _collect(root, base), _collect(root)
    assert_toward_credit(before["practice_facts"], after["practice_facts"], f"{name}: ")
    assert after["ci_score"]["value"] >= before["ci_score"]["value"]
    # and, measured: nothing moved at all on either control
    assert after["practice_facts"] == before["practice_facts"]

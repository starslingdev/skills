"""The default-on setup-cache table is duplicated, so pin it two-sided.

ci-speedup decides whether a `setup-*` step restores a cache with no `cache:`
input (setup-go v4+, setup-uv v5+, setup-ruby's `bundler-cache`, setup-node v5+
with a package-manager field) to price a cache it measures. ci-score carries a
COPY of the same rules to credit Dependency caching. Each skill installs
standalone, so a copy is the only option; this test is the coupling. If the two
drift, one repository gets "you cache" from one engine and "you do not" from
the other about the same line of YAML.

It lives at the repo ROOT: a cross-skill read is normal here and forbidden
inside either skill's own suite.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEEDUP_DIR = _ROOT / "skills" / "ci-speedup" / "scripts"
_SCORE = _ROOT / "skills" / "ci-score" / "scripts" / "practice_facts.py"


def _load(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sides():
    pytest.importorskip("yaml")
    if str(_SPEEDUP_DIR) not in sys.path:
        sys.path.insert(0, str(_SPEEDUP_DIR))
    speed = _load("_parity_setup_cache_collect_runs", _SPEEDUP_DIR / "collect_runs.py")
    score = _load("_parity_setup_cache_practice_facts", _SCORE)
    return speed, score


def test_the_tables_match(sides):
    speed, score = sides
    assert score._SETUP_CACHE_INPUTS == speed._OPT79_SETUP_CACHE_INPUTS
    assert (score._SETUP_NODE_AUTO_CACHE_FROM_MAJOR
            == speed._OPT79_SETUP_NODE_AUTO_CACHE_FROM_MAJOR)
    assert score._SETUP_NODE_V5_PM_RE.pattern == speed._OPT79_SETUP_NODE_V5_PM_RE.pattern
    assert score._SETUP_NODE_NPM_RE.pattern == speed._OPT79_SETUP_NODE_NPM_RE.pattern
    assert score._SETUP_OFF_VALUES == speed._OPT79_OFF_VALUES


def test_every_gradle_style_default_on_action_is_one_ci_speedup_knows(sides):
    speed, score = sides
    for action in ("gradle/actions/setup-gradle", "gradle/gradle-build-action",
                   "bahmutov/npm-install"):
        assert score._DEFAULT_ON_CACHE_ACTIONS_RE.match(action)
        assert speed._OPT79_OTHER_CACHE_USES_RE.match(action + "@v1")


_PJ_NPM = {"packageManager": "npm@10.9.0"}
_PJ_PNPM = {"packageManager": "pnpm@9.1.0"}
_PJ_DEV = {"devEngines": {"packageManager": [{"name": "npm"}]}}
_SHA = "8f4b7f84864484a7bf31766abe9204da3cbe65b3"
_REFS = ["v3", "v4", "v4.2.1", "v5", "v6", "5", _SHA, "49933ea5288caeca8642d1e84afbd3f7d6820020", "main", ""]
_ACTIONS = ["actions/setup-go", "astral-sh/setup-uv", "ruby/setup-ruby",
            "actions/setup-node", "actions/setup-python", "actions/setup-java"]
_WITHS = [{}, {"cache": False}, {"cache": "false"}, {"cache": "no"}, {"cache": ""},
          {"cache": "npm"}, {"cache": True}, {"cache": "${{ inputs.c }}"},
          {"enable-cache": False}, {"enable-cache": "auto"}, {"enable-cache": True},
          {"bundler-cache": True}, {"bundler-cache": False},
          {"package-manager-cache": False}, {"package-manager-cache": "true"},
          {"package-manager-cache": "${{ x }}"}]
_PJS = [None, {}, _PJ_NPM, _PJ_PNPM, _PJ_DEV, {"packageManager": "npm"}]


@pytest.mark.parametrize("action", _ACTIONS)
@pytest.mark.parametrize("ref", _REFS)
def test_the_verdicts_match(sides, action, ref):
    speed, score = sides
    for with_in in _WITHS:
        for pj in _PJS:
            uses = f"{action}@{ref}" if ref else action
            want = speed._opt79_setup_cache_state(uses, with_in, pj)
            got = score._setup_cache_state(action, ref, with_in, pj)
            assert got == want, (uses, with_in, pj)

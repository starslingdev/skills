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

DOCUMENTED CARVE-OUTS (two owner rules, 2026-10-27; recorded under OD-CS23,
PROPOSED). ci-score narrows the shared verdict in its per-step wrapper or
the walk that calls it (`_step_caches_by_default`, `_dependency_cache_walk`),
and nowhere else, so `_setup_cache_state` stays identical to ci-speedup's:

1. A SHA ref carrying a version comment (`# vN`, `# tag=vN`,
   `# ratchet:owner/action@vN`, `# pin@vN`) is re-checked at that major; a
   comment naming a release from before the default turned on, or naming no
   release at all, earns nothing. ci-speedup does not read version comments.
2. setup-uv with no `enable-cache` input is credited only on a published
   GitHub-hosted `runs-on` label (`enable-cache: auto` caches only there).
   ci-speedup does not gate on the runner.

`with:` keys are read case-insensitively in both directions: the shared
verdict is computed from the lower-cased inputs, so `Cache: npm` reads exactly
as `cache: npm` and `Cache: false` as `cache: false` (the test below checks an
upper-cased spelling never earns more than the lower-cased one). And readings
that only withhold: setup-node's `package-manager-cache: ${{ … }}` read as its default `true`
(so the automatic cache still needs a manager in package.json); setup-go's
default only after a finished root checkout and with a go.sum.

ONE documented exception may ADD credit (owner decision 3, 2026-10-27): a
SHA-pinned setup-node the v5 and v6 rules disagree on (`maybe`) is credited
when it carries version comment(s) and every commented major caches.
`test_the_wrapper_only_withholds` allows exactly that case and fails on any
other credit the wrapper adds.
"""
from __future__ import annotations

import importlib.util
import re
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


def _alternatives(pattern: str) -> set[str]:
    """The action names in an `^(a|b|c)...` alternation."""
    m = re.search(r"\^\(([^()]*)\)", pattern)
    assert m, pattern
    return {alt.lower() for alt in m.group(1).split("|")}


def test_the_default_on_cache_actions_match_both_ways(sides):
    """Every non-setup action ci-speedup treats as restoring its own cache is
    credited by ci-score (by name or by its own table), and every entry in
    ci-score's table is one ci-speedup knows: an action added on one side
    fails here until the other side has it too."""
    speed, score = sides
    speed_names = _alternatives(speed._OPT79_OTHER_CACHE_USES_RE.pattern)
    for name in speed_names:
        assert (score._CACHE_ACTION_RE.search(name)
                or score._DEFAULT_ON_CACHE_ACTIONS_RE.match(name)), name
    for name in _alternatives(score._DEFAULT_ON_CACHE_ACTIONS_RE.pattern):
        assert name in speed_names, name
        assert speed._OPT79_OTHER_CACHE_USES_RE.match(name + "@v1")
        assert score._DEFAULT_ON_CACHE_ACTIONS_RE.match(name.upper())


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
            "actions/setup-node", "actions/setup-python", "actions/setup-java",
            "Actions/Setup-Go", "ASTRAL-SH/setup-uv", "gradle/actions/setup-gradle",
            "Gradle/Gradle-Build-Action"]
_WITHS = [{}, {"cache": False}, {"cache": "false"}, {"cache": "no"}, {"cache": ""},
          {"cache": "npm"}, {"cache": True}, {"cache": "${{ inputs.c }}"},
          {"enable-cache": False}, {"enable-cache": "auto"}, {"enable-cache": True},
          {"bundler-cache": True}, {"bundler-cache": False},
          {"package-manager-cache": False}, {"package-manager-cache": "true"},
          {"package-manager-cache": "${{ x }}"},
          {"cache-disabled": True}, {"cache-disabled": "false"}]
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


def _decision_3(score, action, ref, with_in, pj, comments) -> bool:
    """The one credit the wrapper may add over the shared verdict (owner
    decision 3): a SHA-pinned setup-node left undecided, whose every
    commented major caches for this package.json."""
    majors = comments.get((action.lower(), ref.lower()), set()) if comments else set()
    return (action.lower() == "actions/setup-node" and bool(score._SHA_REF_RE.match(ref))
            and bool(majors) and None not in majors
            and all(score._setup_cache_state(action, f"v{m}", with_in, pj) == "on"
                    for m in majors))


def _shared_or_gradle(speed, score, action, uses, with_in, pj) -> bool:
    if score._DEFAULT_ON_CACHE_ACTIONS_RE.match(action):
        return speed._OPT79_OTHER_CACHE_USES_RE.match(uses) is not None and str(
            with_in.get("cache-disabled", "")).strip().lower() != "true"
    return speed._opt79_setup_cache_state(uses, with_in, pj) in ("on", "expr")


def _upper_keys(with_in: dict) -> dict:
    return {k.title(): v for k, v in with_in.items()}


@pytest.mark.parametrize("action", _ACTIONS)
@pytest.mark.parametrize("ref", _REFS)
def test_the_wrapper_only_withholds(sides, action, ref, tmp_path):
    speed, score = sides
    sha = ref.lower() if score._SHA_REF_RE.match(ref) else None
    go_sum_root = tmp_path / "with-go-sum"
    go_sum_root.mkdir()
    (go_sum_root / "go.sum").write_text("")
    bare_root = tmp_path / "bare"
    bare_root.mkdir()
    key = (action.lower(), sha)
    comment_sets = ({}, {key: {3}}, {key: {4, 5}}, {key: {3, 5}}, {key: {5}}, {key: {6}},
                    {key: {None}}) if sha else ({},)
    for with_in in _WITHS:
        for pj in _PJS:
            uses = f"{action}@{ref}" if ref else action
            shared = _shared_or_gradle(speed, score, action, uses, with_in, pj)
            step = {"uses": uses, "with": with_in}
            # No comment, GitHub-hosted, checked out, no tree to check: the
            # carve-outs are inert, and only the package-manager-cache
            # expression reading (withhold-only) can differ.
            pmc_expr = (action.lower() == "actions/setup-node"
                        and "${{" in str(with_in.get("package-manager-cache") or ""))
            got = score._step_caches_by_default(step, pj, False)
            assert got == shared or (pmc_expr and not got), (uses, with_in, pj)
            for comments in comment_sets:
                for hosted in (True, False):
                    for ws_unknown in (False, True):
                        for root in (None, go_sum_root, bare_root):
                            for w in (with_in, _upper_keys(with_in)):
                                st = {"uses": uses, "with": w}
                                got = score._step_caches_by_default(
                                    st, pj, ws_unknown, comments, hosted, root=root)
                                allowed = shared or (
                                    not ws_unknown and _decision_3(
                                        score, action, ref, with_in, pj, comments))
                                assert not got or allowed, (uses, w, pj, comments, hosted,
                                                            ws_unknown, root)


def test_decision_3_is_reachable_and_exact(sides):
    """The exception above is not vacuous: the yarn case it exists for adds
    credit, and the neighbouring cases do not."""
    _speed, score = sides
    step = {"uses": f"actions/setup-node@{_SHA}", "with": {}}
    yarn = {"packageManager": "yarn@4.1.0"}
    key = ("actions/setup-node", _SHA)
    assert score._setup_cache_state("actions/setup-node", _SHA, {}, yarn) == "maybe"
    assert score._step_caches_by_default(step, yarn, False, {key: {5}}) is True
    assert score._step_caches_by_default(step, yarn, False, {key: {6}}) is False
    assert score._step_caches_by_default(step, yarn, False, {key: {5, 6}}) is False
    assert score._step_caches_by_default(step, yarn, False, {key: {None}}) is False
    assert score._step_caches_by_default(step, yarn, False, {}) is False
    assert score._step_caches_by_default(step, yarn, True, {key: {5}}) is False


_WS_SHAPES = [
    [],
    ["co"],
    ["co", "node"],
    ["node", "co"],
    ["co-sub", "co", "node"],
    ["co-repo", "node"],
    ["co-bg", "node"],
    ["co-bg", "run", "node"],
    [["co", "node"]],
    [["co", "run"], "node"],
    [["run", ["co"]], "node"],
    ["co", ["co-sub", "node"]],
    [["co-bg", "run"], "node"],
]
_WS_STEPS = {
    "co": {"uses": "actions/checkout@v5"},
    "co-sub": {"uses": "actions/checkout@v5", "with": {"path": "src"}},
    "co-repo": {"uses": "actions/checkout@v5", "with": {"repository": "o/r"}},
    "co-bg": {"uses": "actions/checkout@v5", "background": True},
    "node": {"uses": "actions/setup-node@v5"},
    "run": {"run": "echo"},
}


def _ws_build(shape):
    out = []
    for item in shape:
        if isinstance(item, list):
            out.append({"parallel": _ws_build(item)})
        else:
            out.append(dict(_WS_STEPS[item]))
    return out


@pytest.mark.parametrize("shape", _WS_SHAPES, ids=str)
def test_the_workspace_state_walks_match(sides, shape):
    """ci-score's `_workspace_states` is a copy of ci-speedup's
    `_opt79_workspace_states`; both must agree step for step on sequence,
    `parallel:` groups and `background: true` checkouts."""
    speed, score = sides
    import workflow_steps
    steps = _ws_build(shape)
    leaves = [lf for lf in workflow_steps.walk_steps(steps).leaves
              if isinstance(lf.step, dict)]
    want = speed._opt79_workspace_states(leaves)
    group_of: dict = {}
    mine = score._job_steps({"steps": steps}, group_of)
    assert [id(s) for s in mine] == [id(lf.step) for lf in leaves]
    assert score._workspace_states(mine, group_of) == want, shape

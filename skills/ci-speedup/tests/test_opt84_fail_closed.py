"""OPT84 fails closed: anything the read cannot follow holds the step back.

A verdict (`tool_not_in_table`, ...) is only for a step known not to be a
swap. A shell script, a local executable, a wrapper whose command cannot be
read, a subshell, a package-manager builtin that runs a script, or a package
manager pointed at another directory each hold the step back and list it.
Also: ESLint config reading matches OPT82's, Prettier plugins make the swap a
dual-run, an unreadable package.json on the version walk holds the tsc row,
the tsc package count is the packages that need the swap, and an ambiguous
pole with no name is not counted as a listed hold-back.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
sys.path.insert(0, str(_SKILL_DIR / "tests"))
import collect_runs as cr  # noqa: E402
import test_opt84_tool_swap as t84  # noqa: E402  (helpers only, read-only)

_run, _block, _pole, _doc = t84._run, t84._block, t84._pole, t84._doc
_held, _rows, _tool, _config = t84._held, t84._rows, t84._tool, t84._config
_NO_FACTS, _WF, _STEP = t84._NO_FACTS, t84._WF, t84._STEP


def _gate_only(withheld, cands, gate):
    assert withheld == {gate: 1}, withheld
    assert _held(cands, gate), cands


# ---- 1. opaque leaves hold the step back --------------------------------------

@pytest.mark.parametrize("run,scripts", [
    ("bash scripts/lint.sh", {"": {}}),
    ("sh scripts/lint.sh", {"": {}}),
    ("zsh scripts/lint.sh", {"": {}}),
    ("scripts/lint.sh", {"": {}}),
    ("./scripts/check", {"": {}}),
    ("../tools/check", {"": {}}),
    ("pnpm lint", {"": {"lint": "bash scripts/lint.sh"}}),
    ("pnpm lint", {"": {"lint": "./scripts/lint"}}),
    ("git diff --name-only | xargs eslint", {"": {}}),
    ("time bash scripts/lint.sh", {"": {}}),
    ("env -S 'eslint .'", {"": {}}),
    ("npm test", {"": {}}),
    ("npm start", {"": {}}),
    ("yarn test", {"": {}}),
    ("npm test", {"": {"test": "npm test"}}),
])
def test_an_opaque_leaf_holds_the_step_back(run, scripts):
    out, withheld, cands = _run({"run": run}, block=_block(scripts=scripts))
    assert out == []
    _gate_only(withheld, cands, "script_unresolvable")


@pytest.mark.parametrize("run", ["./configure && make -j4", "./gradlew build",
                                 "time make -C bld"])
def test_a_native_build_entry_point_stays_a_verdict(run):
    out, withheld, cands = _run({"run": run}, pole=_pole(category="build"),
                                block=_block(scripts={}))
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


@pytest.mark.parametrize("run,row", [
    ("time npx eslint .", "eslint"),
    ("env NODE_OPTIONS=--max-old-space-size=4096 eslint .", "eslint"),
    ("env -i PATH=/usr/bin npx eslint .", "eslint"),
    ("timeout 10m npx prettier --check .", "prettier"),
    ("timeout --signal=KILL 600 npx prettier --check .", "prettier"),
    ("nice -n 10 npx tsc --noEmit", "tsc"),
    ("nohup npx eslint .", "eslint"),
    ("exec npx eslint .", "eslint"),
])
def test_a_wrapper_is_stripped_and_its_command_classified(run, row):
    out, withheld, cands = _run({"run": run})
    assert len(out) == 1, (withheld, cands)
    assert _rows(out[0]) == [row]


def test_a_wrapped_package_script_is_followed():
    out, withheld, cands = _run({"run": "time pnpm run lint"},
                                block=_block(scripts=t84._MASTRA))
    assert len(out) == 1, (withheld, cands)
    assert sorted(_rows(out[0])) == ["eslint", "prettier"]


@pytest.mark.parametrize("script", ["(cd packages/web && eslint .)", "(eslint .)"])
def test_a_subshell_leaf_is_command_changes_directory(script):
    out, withheld, cands = _run({"run": "pnpm lint"},
                                block=_block(scripts={"": {"lint": script}}))
    assert out == []
    _gate_only(withheld, cands, "command_changes_directory")


@pytest.mark.parametrize("run", ["npm test", "pnpm test", "yarn test", "npm t",
                                 "npm --silent test"])
def test_a_package_manager_test_builtin_is_followed_into_its_script(run):
    out, withheld, cands = _run({"run": run},
                                block=_block(scripts={"": {"test": "eslint ."}}))
    assert len(out) == 1, (withheld, cands)
    assert _rows(out[0]) == ["eslint"]


def test_npm_test_forwards_its_args_into_the_followed_script():
    out, withheld, cands = _run({"run": "npm test -- --max-warnings 0"},
                                block=_block(scripts={"": {"test": "eslint ."}}))
    assert len(out) == 1, (withheld, cands)
    assert _tool(out[0], "eslint")["leaf_command"] == "eslint . --max-warnings 0"


def test_a_followed_test_script_with_no_table_tool_is_a_verdict():
    out, withheld, cands = _run({"run": "npm test"},
                                block=_block(scripts={"": {"test": "jest --ci"}}))
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


def test_a_shell_test_inside_a_yarn_script_is_not_a_package_manager_call():
    out, withheld, cands = _run(
        {"run": "yarn lint"},
        block=_block(scripts={"": {"lint": "test -f eslint.config.js && eslint ."}}))
    assert len(out) == 1, (withheld, cands)
    assert _rows(out[0]) == ["eslint"]


@pytest.mark.parametrize("run", ["pnpm -C web exec eslint .",
                                 "pnpm --dir web exec tsc --noEmit",
                                 "npm --prefix web exec eslint ."])
def test_a_package_manager_pointed_at_another_directory_is_held_back(run):
    out, withheld, cands = _run({"run": run})
    assert out == []
    _gate_only(withheld, cands, "working_directory_unknown")


# ---- 2. ESLint config reading matches OPT82 -----------------------------------

_FLAT_AND_RC = dict(
    configs=[_config("eslint.config.js"), _config(".eslintrc.json", format="eslintrc")],
    facts={"eslint.config.js": dict(_NO_FACTS),
           ".eslintrc.json": {**_NO_FACTS, "plugins": ["react"]}})


@pytest.mark.parametrize("where", ["step", "job", "workflow", "inline"])
def test_eslint_use_flat_config_false_reads_the_eslintrc(where):
    step = {"run": "npx eslint ."}
    if where == "step":
        step["env"] = {"ESLINT_USE_FLAT_CONFIG": "false"}
    if where == "inline":
        step["run"] = "ESLINT_USE_FLAT_CONFIG=false npx eslint ."
    doc = _doc(step)
    if where == "job":
        doc["jobs"]["lint"]["env"] = {"ESLINT_USE_FLAT_CONFIG": "false"}
    if where == "workflow":
        doc["env"] = {"ESLINT_USE_FLAT_CONFIG": "false"}
    out, withheld, cands = _run(step, doc=doc, block=_block(**_FLAT_AND_RC))
    assert len(out) == 1, (withheld, cands)
    t = _tool(out[0], "eslint")
    assert t["fit"] == "dual-run"
    assert "framework plugin react in `.eslintrc.json`" in t["fit_reasons"]


def test_without_the_flag_the_flat_config_is_read():
    out, withheld, _c = _run({"run": "npx eslint ."}, block=_block(**_FLAT_AND_RC))
    assert len(out) == 1 and _tool(out[0], "eslint")["fit"] == "drop-in", withheld


_NESTED = dict(configs=[_config("eslint.config.js"),
                        _config("packages/a/eslint.config.js")],
               facts={"eslint.config.js": dict(_NO_FACTS),
                      "packages/a/eslint.config.js": dict(_NO_FACTS)})


def test_a_nested_flat_config_with_an_unknown_eslint_major_is_held_back():
    out, withheld, cands = _run({"run": "npx eslint ."}, block=_block(**_NESTED))
    assert out == []
    _gate_only(withheld, cands, "eslint_config_unreadable")


def test_a_nested_flat_config_under_eslint_9_is_not_ambiguous():
    b = _block(**_NESTED)
    b["eslint_versions"] = {"": "^9.10.0"}
    out, withheld, _c = _run({"run": "npx eslint ."}, block=b)
    assert len(out) == 1, withheld


# ---- 3. Prettier plugins make the swap a dual-run ------------------------------

def test_a_prettier_plugin_at_the_root_is_a_dual_run_reason():
    b = _block()
    b["prettier_plugins"] = {"": ["prettier-plugin-tailwindcss"]}
    out, withheld, _c = _run({"run": "npx prettier --check ."}, block=b)
    assert len(out) == 1, withheld
    t = _tool(out[0], "prettier")
    assert t["fit"] == "dual-run"
    assert t["fit_reasons"] == [
        "Prettier plugin prettier-plugin-tailwindcss in `package.json`: oxfmt may "
        "not carry it"]


def test_a_prettier_plugin_in_the_package_and_at_the_root_are_both_named():
    b = _block()
    b["prettier_plugins"] = {"web": ["@ianvs/prettier-plugin-sort-imports"],
                             "": ["prettier-plugin-svelte"], "other": ["x"]}
    out, withheld, _c = _run({"run": "npx prettier --check .",
                              "working-directory": "web"}, block=b)
    assert len(out) == 1, withheld
    reasons = _tool(out[0], "prettier")["fit_reasons"]
    assert reasons == [
        "Prettier plugin @ianvs/prettier-plugin-sort-imports in `web/package.json`: "
        "oxfmt may not carry it",
        "Prettier plugin prettier-plugin-svelte in `package.json`: oxfmt may not "
        "carry it"]


@pytest.mark.parametrize("plugins", [None, {}, {"": []}, {"other": ["x"]}])
def test_no_prettier_plugin_on_the_walk_or_an_older_scan_stays_drop_in(plugins):
    b = _block()
    b.pop("prettier_plugins", None)
    if plugins is not None:
        b["prettier_plugins"] = plugins
    out, withheld, _c = _run({"run": "npx prettier --check ."}, block=b)
    assert len(out) == 1 and _tool(out[0], "prettier")["fit"] == "drop-in", withheld


# ---- 4. an unreadable package.json on the version walk holds the tsc row -------

def test_an_unreadable_package_json_below_the_declaring_one_holds_tsc():
    out, withheld, cands = _run(
        {"run": "npx tsc --noEmit", "working-directory": "packages/x"},
        block=_block(unreadable_pkgs=["packages/x"]))
    assert out == []
    _gate_only(withheld, cands, "typescript_version_unreadable")


def test_an_unreadable_package_json_does_not_hold_eslint_or_prettier():
    out, withheld, _c = _run(
        {"run": "npx eslint . && npx prettier --check .",
         "working-directory": "packages/x"},
        block=_block(unreadable_pkgs=["packages/x"]))
    assert len(out) == 1, withheld
    for row in ("eslint", "prettier"):
        assert _tool(out[0], row)["source_version"] is None


def test_an_unreadable_package_json_above_the_declaring_one_does_not_hold_tsc():
    out, withheld, _c = _run(
        {"run": "npx tsc --noEmit", "working-directory": "packages/x"},
        block=_block(tool_versions={"packages/x": {"typescript": "^5.6.2"}},
                     unreadable_pkgs=["packages"]))
    assert len(out) == 1, withheld

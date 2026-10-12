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
    # a C / JVM repo: no JS manifest anywhere (in a JS repo `make` is held back)
    out, withheld, cands = _run({"run": run}, pole=_pole(category="build"),
                                block=t84._no_js_block())
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


@pytest.mark.parametrize("plugins", [{}, {"": []}, {"other": ["x"]}])
def test_no_prettier_plugin_on_the_walk_stays_drop_in(plugins):
    # `other` is a sibling of `web`, never below the folder the run formats
    b = _block()
    b["prettier_plugins"] = plugins
    out, withheld, _c = _run({"run": "npx prettier --check .",
                              "working-directory": "web"}, block=b)
    assert len(out) == 1 and _tool(out[0], "prettier")["fit"] == "drop-in", withheld


def test_a_block_with_no_prettier_plugins_map_is_run_both():
    b = _block()
    b.pop("prettier_plugins", None)
    out, withheld, _c = _run({"run": "npx prettier --check ."}, block=b)
    t = _tool(out[0], "prettier")
    assert t["fit"] == "dual-run" and any("were not read" in r for r in t[
        "fit_reasons"]), t


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


# ---- 5. the tsc package count is the packages below TypeScript 7 ---------------

def test_tsc_package_count_excludes_packages_already_on_typescript_7():
    scripts = {"": {}, "a": {"typecheck": "tsc --noEmit"},
               "b": {"typecheck": "tsc --noEmit"}, "c": {"typecheck": "tsc --noEmit"}}
    tv = {"": {"typescript": "^5.6.2"}, "c": {"typescript": "^7.0.0"}}
    out, withheld, _c = _run({"run": "pnpm -r typecheck"},
                             block=_block(scripts=scripts, tool_versions=tv))
    assert len(out) == 1, withheld
    t = _tool(out[0], "tsc")
    assert t["packages"] == 2 and t["fit_reasons"] == []


# ---- 6. an ambiguous pole with no name is not file-backed ----------------------

def test_an_ambiguous_pole_with_no_job_or_check_is_pole_not_file_backed():
    out, withheld, cands = _run({"run": "npx eslint ."},
                                poles=[{"ambiguous_workflows": ["a", "b"]}])
    assert out == [] and withheld == {"pole_not_file_backed": 1}, withheld
    assert cands == []


# ---- 8. task-runner forms through OPT84 -----------------------------------------

@pytest.mark.parametrize("run", ["pnpm --dir web -r lint", "pnpm -C web -r lint"])
def test_pnpm_recursive_in_another_directory_is_held_back(run):
    out, withheld, cands = _run(
        {"run": run}, block=_block(scripts={"": {}, "web/a": {"lint": "eslint ."}}))
    assert out == []
    _gate_only(withheld, cands, "delegated_script_unresolved")


@pytest.mark.parametrize("run", ["npx nx affected --target=lint,typecheck",
                                 "npx nx run-many -t lint,typecheck",
                                 "npx nx run-many -t lint typecheck"])
def test_nx_comma_and_multi_target_forms_expand_every_task(run):
    scripts = {"": {}, "a": {"lint": "eslint .", "typecheck": "tsc --noEmit"},
               "b": {"lint": "eslint src"}}
    out, withheld, cands = _run({"run": run}, block=_block(scripts=scripts))
    assert len(out) == 1, (withheld, cands)
    assert sorted(_rows(out[0])) == ["eslint", "tsc"]
    assert _tool(out[0], "eslint")["packages"] == 2


def test_turbo_forwards_args_after_the_separator_into_each_package_leaf():
    scripts = {"": {}, "apps/web": {"lint": "eslint ."},
               "apps/docs": {"lint": "eslint src"}}
    res = cr._resolve_run_leaves("npx turbo run lint -- --max-warnings 0", "",
                                 scripts)
    assert res.unresolved == [], res.unresolved
    assert sorted((lf["wd"], lf["command"]) for lf in res.leaves) == [
        ("apps/docs", "eslint src --max-warnings 0"),
        ("apps/web", "eslint . --max-warnings 0")]
    out, withheld, _c = _run({"run": "npx turbo run lint -- --max-warnings 0"},
                             block=_block(scripts=scripts))
    assert len(out) == 1, withheld
    assert _tool(out[0], "eslint")["leaf_command"].endswith("--max-warnings 0")


# =============================================================================
# Review round A: the scan block, the stamp contract, and the reads that must
# fail closed (Prettier fit, ESLint fit, the typescript version, generic
# runners and actions, a truncated walk under a task runner)
# =============================================================================

def _raw(poles, block, docs=None, opt82=None):
    w: dict[str, int] = {}
    c: list[dict] = []
    out = cr._detect_opt84_tool_swap(
        poles, docs if docs is not None else {_WF: _doc({"run": "npx eslint ."})},
        block, opt82 or [], 1, withheld=w, withheld_candidates=c)
    return out, w, c


# ---- A1. the scan block: missing or errored holds every pole back first -------

def test_a_missing_scan_block_holds_each_pole_back_before_candidate_work():
    other = _pole(category="test")                     # no candidate step at all
    other["job"] = other["check"] = "Other"
    out, w, c = _raw([_pole(), _pole(), other], None)  # Lint listed twice
    assert out == [] and w == {"type_aware_lint_scan_missing": 2}, w
    assert c == [{"workflow_file": _WF, "job": "Lint",
                  "gate": "type_aware_lint_scan_missing"},
                 {"workflow_file": _WF, "job": "Other",
                  "gate": "type_aware_lint_scan_missing"}]


def test_an_errored_scan_block_holds_back_and_never_takes_the_no_manifest_path():
    blk = t84._no_js_block(error="RuntimeError")
    out, w, c = _raw([_pole()], blk)
    assert out == [] and w == {"type_aware_config_reader_failed": 1}, w
    assert c == [{"workflow_file": _WF, "job": "Lint",
                  "gate": "type_aware_config_reader_failed"}]
    _out, w, _c = t84._curl_case(blk)
    assert w == {"type_aware_config_reader_failed": 1}, w


def test_the_two_scan_gates_are_held_back_gates():
    assert {"type_aware_lint_scan_missing", "type_aware_config_reader_failed"} <= (
        cr._OPT84_HELD_BACK_GATES)
    assert len(cr._OPT84_HELD_BACK_GATES) == 17 and len(cr._OPT84_VERDICT_GATES) == 6


def test_counting_an_unknown_gate_or_an_unlisted_hold_back_raises():
    with pytest.raises(ValueError):
        cr._opt84_count({}, [], "made_up_gate", _WF, "Lint")
    with pytest.raises(ValueError):
        cr._opt84_count({}, [], "script_unresolvable", _WF, "")
    with pytest.raises(ValueError):                    # a step gate needs its step
        cr._opt84_count({}, [], "script_unresolvable", _WF, "Lint")
    with pytest.raises(ValueError):                    # a pole gate names no step
        cr._opt84_count({}, [], "workflow_yaml_unavailable", _WF, "Lint", "s")
    assert cr._OPT84_STEP_GATES < cr._OPT84_HELD_BACK_GATES
    assert cr._OPT84_HELD_BACK_GATES - cr._OPT84_STEP_GATES == {
        "pole_workflow_ambiguous", "pole_step_entries_unreadable",
        "pole_step_timings_unavailable", "workflow_yaml_unavailable",
        "pole_job_not_matched_in_yaml", "type_aware_lint_scan_missing",
        "type_aware_config_reader_failed"}
    w: dict[str, int] = {}
    c: list[dict] = []
    cr._opt84_count(w, c, "no_candidate_step", _WF, "")
    cr._opt84_count(w, c, "script_unresolvable", _WF, "Lint", "s")
    assert w == {"no_candidate_step": 1, "script_unresolvable": 1}
    assert c == [{"workflow_file": _WF, "job": "Lint", "gate": "script_unresolvable",
                  "step": "s"}]


# ---- A0. the stamp contract ----------------------------------------------------

_OPT82_ON_STEP = [{"pattern": "OPT82", "workflow_file": _WF, "job": "Lint",
                   "type_aware_lint": {"yaml_job": "lint", "lint_step": _STEP}}]
_INVARIANT_CASES = [
    ({"run": "pnpm lint"}, dict(scripts=t84._MASTRA), None),
    ({"run": "pnpm turbo run lint"}, dict(scripts=t84._TURBO_ESLINT), None),
    ({"run": "pnpm -r typecheck"},
     dict(scripts={"": {}, "a": {"typecheck": "tsc --noEmit"},
                   "b": {"typecheck": "tsc --noEmit"}}), None),
    ({"run": "pnpm lint"}, dict(scripts=t84._MASTRA), _OPT82_ON_STEP),
    ({"run": "npx eslint . && npx prettier --check ."},
     dict(facts={"eslint.config.js": {**_NO_FACTS, "plugins": ["vue"]}}), None),
]


@pytest.mark.parametrize("step,kw,opt82", _INVARIANT_CASES)
def test_fit_reasons_are_non_empty_exactly_when_the_fit_is_dual_run(step, kw, opt82):
    out, withheld, _c = _run(step, block=_block(**kw), opt82=opt82)
    assert len(out) == 1, withheld
    ts = out[0]["tool_swap"]
    for t in ts["tools"]:
        assert bool(t["fit_reasons"]) == (t["fit"] == "dual-run"), t
        assert not any("workspace package" in r for r in t["fit_reasons"]), t
        assert t["leaf_command"] in ts["resolved_leaves"], (t, ts["resolved_leaves"])
        assert t["published_range"]


def test_the_package_count_is_the_single_source_of_the_workspace_note():
    out, _w, _c = _run({"run": "pnpm turbo run lint"},
                       block=_block(scripts=t84._TURBO_ESLINT))
    t = _tool(out[0], "eslint")
    assert t["packages"] == 2 and t["fit"] == "drop-in" and t["fit_reasons"] == []


# ---- A2. Prettier fit fails closed -------------------------------------------

def _pfit(step, **block_kw):
    extra = block_kw.pop("extra", {})
    b = _block(**block_kw)
    b.update(extra)
    out, withheld, cands = _run(step, block=b)
    assert len(out) == 1, (withheld, cands)
    return _tool(out[0], "prettier")


def test_an_unreadable_package_json_on_the_prettier_walk_is_run_both():
    t = _pfit({"run": "npx prettier --check .", "working-directory": "packages/x"},
              unreadable_pkgs=["packages/x"])
    assert t["fit"] == "dual-run"
    assert ("`packages/x/package.json` could not be read; its Prettier plugins are "
            "unknown") in t["fit_reasons"], t["fit_reasons"]


@pytest.mark.parametrize("wd,truncated", [("a/b", True), (".github/tools", False)])
def test_a_prettier_walk_through_unread_ground_is_run_both(wd, truncated):
    t = _pfit({"run": "npx prettier --check .", "working-directory": wd},
              truncated=truncated)
    assert t["fit"] == "dual-run"
    assert (f"`{wd}/package.json` could not be read; its Prettier plugins are "
            "unknown") in t["fit_reasons"], t["fit_reasons"]


def test_a_truncated_walk_below_a_read_root_is_run_both():
    t = _pfit({"run": "npx prettier --check ."}, truncated=True)
    assert t["fit"] == "dual-run", t
    assert any("stopped early" in r for r in t["fit_reasons"]), t["fit_reasons"]


def test_prettier_with_no_package_json_read_on_the_walk_is_run_both():
    t = _pfit({"run": "npx prettier --check ."}, scripts={})
    assert t["fit"] == "dual-run"
    assert t["fit_reasons"] == [
        "no `package.json` was read at or above `.`; its Prettier plugins are unknown"]


def test_a_prettier_plugin_from_a_config_file_names_that_file():
    t = _pfit({"run": "npx prettier --check ."},
              extra={"prettier_plugins": {"": ["prettier-plugin-astro"]},
                     "prettier_plugin_files": {"": {"prettier-plugin-astro":
                                                    ".prettierrc.json"}}})
    assert t["fit_reasons"] == [
        "Prettier plugin prettier-plugin-astro in `.prettierrc.json`: oxfmt may not "
        "carry it"]


def test_a_prettier_config_this_audit_does_not_open_is_run_both():
    t = _pfit({"run": "npx prettier --check .", "working-directory": "web"},
              scripts={"": {}, "web": {}},
              extra={"prettier_config_unreadable": {"web": ["web/prettier.config.mjs"]}})
    assert t["fit"] == "dual-run"
    assert t["fit_reasons"] == [
        "`web/prettier.config.mjs`: a Prettier config this audit does not open may "
        "load plugins"]


@pytest.mark.parametrize("run", ["npx prettier --check .", "npx prettier -c",
                                 "npx prettier --check \"**/*.{ts,svelte}\"",
                                 "npx pretty-quick --check"])
def test_a_root_prettier_run_reads_every_subpackage_below_it(run):
    t = _pfit({"run": run},
              scripts={"": {}, "packages/web": {}},
              extra={"prettier_plugins": {"packages/web": ["prettier-plugin-svelte"]}})
    assert t["fit"] == "dual-run"
    assert t["fit_reasons"] == [
        "Prettier plugin prettier-plugin-svelte in `packages/web/package.json`: oxfmt "
        "may not carry it"]


def test_a_prettier_run_on_one_folder_does_not_read_a_sibling_package():
    t = _pfit({"run": "npx prettier --check src"},
              scripts={"": {}, "packages/web": {}},
              extra={"prettier_plugins": {"packages/web": ["prettier-plugin-svelte"]}})
    assert t["fit"] == "drop-in" and t["fit_reasons"] == []


def test_a_prettier_plugin_flag_on_the_command_is_run_both():
    t = _pfit({"run": "npx prettier --plugin prettier-plugin-x --check ."})
    assert t["fit"] == "dual-run"
    assert any("`--plugin`" in r for r in t["fit_reasons"]), t["fit_reasons"]


# ---- A3. ESLint fit -------------------------------------------------------------

def _efit(run="npx eslint .", **block_kw):
    out, withheld, cands = _run({"run": run}, block=_block(**block_kw))
    assert len(out) == 1, (withheld, cands)
    return _tool(out[0], "eslint")


def test_a_type_aware_config_is_run_both():
    t = _efit(configs=[_config(type_aware="on")])
    assert t["fit"] == "dual-run"
    assert t["fit_reasons"] == [
        "type-aware rules in `eslint.config.js`: oxlint runs them only through tsgolint"]


def test_an_unresolved_type_aware_read_is_run_both():
    t = _efit(configs=[_config(type_aware="unresolved")])
    assert t["fit_reasons"] == [
        "whether `eslint.config.js` is type-aware could not be resolved"]


@pytest.mark.parametrize("flag", ["--rulesdir rules", "--plugin acme", "--rule 'x: 2'",
                                  "--parser babel-eslint", "--ext .ts",
                                  "--rulesdir=rules"])
def test_eslint_cli_flags_that_load_rules_are_run_both(flag):
    t = _efit(f"npx eslint {flag} .")
    name = flag.split("=", 1)[0].split()[0]
    assert t["fit"] == "dual-run"
    assert any(f"ESLint CLI flag `{name}`" in r for r in t["fit_reasons"]), t


def test_eslint_plugin_oxlint_in_the_config_means_the_step_already_runs_both():
    facts = {"eslint.config.js": {**_NO_FACTS, "plugins": ["oxlint"]}}
    out, withheld, cands = _run({"run": "npx eslint ."}, block=_block(facts=facts))
    assert out == [] and withheld == {"target_already_in_use": 1} and cands == []


@pytest.mark.parametrize("wd,tv", [
    ("", {"": {"eslint": "^9", "oxlint": "^1.0.0"}}),
    ("web", {"": {"oxlint": "^1.0.0"}, "web": {"eslint": "^9"}}),
])
def test_oxlint_declared_on_the_walk_means_the_eslint_row_is_already_swapped(wd, tv):
    step = {"run": "npx eslint ."} | ({"working-directory": wd} if wd else {})
    out, withheld, _c = _run(step, block=_block(tool_versions=tv,
                                                scripts={"": {}, "web": {}}))
    assert out == [] and withheld == {"target_already_in_use": 1}, withheld


def test_oxfmt_declared_on_the_walk_means_the_prettier_row_is_already_swapped():
    out, withheld, _c = _run({"run": "npx prettier --check ."},
                             block=_block(tool_versions={"": {"oxfmt": "0.9.0"}}))
    assert out == [] and withheld == {"target_already_in_use": 1}, withheld


def test_oxlint_declared_in_a_sibling_package_does_not_silence_the_row():
    out, withheld, _c = _run({"run": "npx eslint .", "working-directory": "web"},
                             block=_block(tool_versions={"other": {"oxlint": "^1"}},
                                          scripts={"": {}, "web": {}}))
    assert len(out) == 1, withheld


# ---- A4. the typescript version read -------------------------------------------

@pytest.mark.parametrize("wd,truncated", [("a/b", True), (".github/tools", False),
                                          ("dist/app", False)])
def test_a_typescript_version_walk_through_unread_ground_holds_tsc(wd, truncated):
    out, withheld, cands = _run(
        {"run": "npx tsc --noEmit", "working-directory": wd},
        block=_block(truncated=truncated))
    assert out == []
    _gate_only(withheld, cands, "typescript_version_unreadable")


def test_a_read_step_directory_under_a_truncated_walk_reads_the_root_version():
    out, withheld, _c = _run({"run": "npx tsc --noEmit", "working-directory": "a/b"},
                             block=_block(truncated=True,
                                          scripts={"": {}, "a/b": {}}))
    assert len(out) == 1, withheld


@pytest.mark.parametrize("spec", [">=5.0.0", ">4", "*", "latest", ">= 5"])
def test_an_open_ended_typescript_range_is_unreadable(spec):
    assert cr._opt84_major(spec) is None
    out, withheld, _c = _run({"run": "npx tsc --noEmit"},
                             block=_block(tool_versions={"": {"typescript": spec}}))
    assert out == [] and withheld == {"typescript_version_unreadable": 1}, withheld


@pytest.mark.parametrize("run", ["npx tsc --noemit", "npx tsc -noEmit",
                                 "npx tsc -p . -NOEMIT"])
def test_noemit_is_read_case_insensitively_and_with_one_dash(run):
    out, withheld, _c = _run({"run": run})
    assert len(out) == 1 and _rows(out[0]) == ["tsc"], withheld


def test_a_catalog_ref_with_an_unreadable_workspace_file_holds_tsc_and_says_why():
    b = _block(tool_versions={"": {"typescript": "catalog:", "eslint": "catalog:"}})
    b["pnpm_catalogs"] = {}
    b["pnpm_catalogs_unreadable"] = True
    out, withheld, cands = _run({"run": "npx tsc --noEmit"}, block=b)
    assert out == []
    _gate_only(withheld, cands, "typescript_version_unreadable")
    out, withheld, _c = _run({"run": "npx eslint ."}, block=b)
    assert len(out) == 1, withheld
    assert _tool(out[0], "eslint")["source_version"] == (
        "catalog: (pnpm-workspace.yaml unreadable)")


# ---- A5. generic runners, scripts and actions ---------------------------------

@pytest.mark.parametrize("run,gate", [
    ("make lint", "delegated_script_unresolved"),
    ("just lint", "delegated_script_unresolved"),
    ("task lint", "delegated_script_unresolved"),
    ("pre-commit run --all-files", "delegated_script_unresolved"),
    ("npx concurrently 'npm:lint:*'", "delegated_script_unresolved"),
    ("npx ng lint", "delegated_script_unresolved"),
    ("npx gulp lint", "delegated_script_unresolved"),
    ("npx grunt lint", "delegated_script_unresolved"),
    ("npx xo", "delegated_script_unresolved"),
    ("npx standard", "delegated_script_unresolved"),
    ("docker run --rm img lint", "delegated_script_unresolved"),
    ("docker-compose run lint", "delegated_script_unresolved"),
    ("pwsh ./lint.ps1", "script_unresolvable"),
    ("fish lint.fish", "script_unresolvable"),
    ("ksh lint.ksh", "script_unresolvable"),
    ("bin/lint", "script_unresolvable"),
    ("tools/ci/check --all", "script_unresolvable"),
])
def test_a_generic_runner_in_a_js_repo_holds_the_step_back(run, gate):
    out, withheld, cands = _run({"run": run})
    assert out == []
    _gate_only(withheld, cands, gate)


def _no_js(step, category="scan"):
    return _run(step, pole=_pole(category=category), block=t84._no_js_block())


def test_a_table_tool_named_directly_in_a_repo_with_no_manifest_still_fires():
    out, withheld, cands = _no_js({"run": "npx --yes prettier@3 --check ."})
    assert len(out) == 1, (withheld, cands)
    t = _tool(out[0], "prettier")
    assert t["source_version"] is None and t["fit"] == "dual-run"
    assert t["fit_reasons"] == [
        "no `package.json` was read at or above `.`; its Prettier plugins are unknown"]


@pytest.mark.parametrize("run", ["make -C bld", "cmake --build bld", "bash ci/build.sh",
                                 "./configure && make -j4", "bin/build"])
def test_a_native_build_in_a_repo_with_no_manifest_is_the_verdict(run):
    out, withheld, cands = _no_js({"run": run}, category="build")
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


@pytest.mark.parametrize("uses", [
    "actions/setup-node@v4", "github/codeql-action/analyze@v3",
    "docker/build-push-action@v6", "pnpm/action-setup@v4", "oven-sh/setup-bun@v2",
    "Swatinem/rust-cache@v2", "dtolnay/rust-toolchain@stable",
    "cygwin/cygwin-install-action@v4", "msys2/setup-msys2@v2",
    "actions-rust-lang/setup-rust-toolchain@v1", "ruby/setup-ruby@v1",
    "astral-sh/setup-uv@v5"])
def test_an_allow_listed_action_is_the_verdict(uses):
    doc = _doc({})
    doc["jobs"]["lint"]["steps"][2] = {"uses": uses, "name": "Do the thing"}
    out, withheld, cands = _run(None, doc=doc, pole=_pole(step="Do the thing"))
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


@pytest.mark.parametrize("uses", ["pre-commit/action@v3.0.1", "some/thing@v1",
                                  "./.github/actions/build"])
def test_an_unknown_or_lint_ish_action_is_held_back(uses):
    doc = _doc({})
    doc["jobs"]["lint"]["steps"][2] = {"uses": uses, "name": "Do the thing"}
    out, withheld, cands = _run(None, doc=doc, pole=_pole(step="Do the thing"))
    assert out == [] and withheld == {"candidate_is_an_action": 1}, withheld
    assert [c["gate"] for c in cands] == ["candidate_is_an_action"]


# ---- A6. task-runner expansion respects the walk ------------------------------

def test_a_task_runner_under_a_truncated_walk_is_delegated():
    out, withheld, cands = _run(
        {"run": "pnpm turbo run lint"},
        block=_block(scripts={"": {}, "a": {"lint": "eslint ."}}, truncated=True))
    assert out == []
    _gate_only(withheld, cands, "delegated_script_unresolved")


# ---- A7. evidence wording ------------------------------------------------------

def test_the_evidence_says_native_replacement():
    out, _w, _c = _run({"run": "npx eslint ."})
    ev = out[0]["evidence"]
    assert "Each has a native replacement." in ev and "native drop-in" not in ev, ev


# ---- A8. branches the test review named ---------------------------------------

def test_a_config_found_with_no_facts_entry_is_run_both():
    t = _efit(facts={})
    assert t["fit_reasons"] == [
        "`eslint.config.js`: its plugins and local rules were not read"]


def test_an_unresolved_config_import_is_run_both():
    t = _efit(configs=[_config(unresolved_imports=["./base.js"])])
    assert t["fit_reasons"] == ["a config import in `eslint.config.js` was not followed"]


@pytest.mark.parametrize("wd", ["/home/runner/work/x", "~/src"])
def test_an_absolute_or_home_working_directory_is_held_back(wd):
    out, withheld, cands = _run({"run": "npx eslint .", "working-directory": wd})
    assert out == []
    _gate_only(withheld, cands, "working_directory_unknown")


def test_two_yaml_steps_with_one_display_name_are_not_matched():
    doc = _doc({"run": "npx eslint ."})
    doc["jobs"]["lint"]["steps"].append({"name": _STEP, "run": "npx prettier -c ."})
    out, withheld, cands = _run(None, doc=doc)
    assert out == []
    _gate_only(withheld, cands, "measured_step_not_matched_in_yaml")


def test_cache_location_and_strategy_are_dropped_from_the_eslint_baseline():
    out, _w, _c = _run({"run": "npx eslint --cache --cache-location .cache/es "
                        "--cache-strategy content --cache-location=.c2 src"})
    base = _tool(out[0], "eslint")["benchmark_commands"][0]
    assert "cache" not in base and base.endswith("eslint src)"), base


def test_a_workflow_level_default_working_directory_expression_is_held_back():
    doc = _doc({"run": "npx eslint ."}, wf_defaults={"working-directory": "${{ env.D }}"})
    out, withheld, cands = _run(None, doc=doc)
    assert out == []
    _gate_only(withheld, cands, "command_has_expression")


def test_every_gate_the_leaf_helpers_return_is_in_a_set():
    import inspect
    import re
    src = (inspect.getsource(cr._opt84_opaque_leaf)
           + inspect.getsource(cr._opt84_expand_leaves)
           + inspect.getsource(cr._opt84_wrapped)
           + inspect.getsource(cr._opt84_strip_flags))
    got = set(re.findall(r'"hold",\s*\(?"([a-z0-9_]+)"', src))
    got |= set(re.findall(r'return None,\s*"([a-z0-9_]+)"', src))
    got |= set(re.findall(r'gate or\s*"([a-z0-9_]+)"', src))
    got |= set(re.findall(r'gates\.append\(\s*"([a-z0-9_]+)"', src))
    got |= set(re.findall(r'"([a-z0-9_]+)"\s+if\b', src))
    got |= set(re.findall(r'\belse\s+"([a-z0-9_]+)"', src))
    got |= {str(v) for v in cr._OPT84_OPAQUE_BINARIES.values()}
    assert {"script_unresolvable", "command_changes_directory",
            "working_directory_unknown", "delegated_script_unresolved"} <= got, got
    assert got <= cr._OPT84_HELD_BACK_GATES, got - cr._OPT84_HELD_BACK_GATES


# ---- nameless `Run <command>` steps that run a non-JS tool ---------------------

_CLIPPY_YAML = ("cargo clippy --target ${{ matrix.target }} --all-targets "
                "--all-features -- -D warnings")
_CLIPPY_MEASURED = ("Run cargo clippy --target aarch64-apple-darwin --all-targets "
                    "--all-features -- -D warnings")


def _nameless(run_or_step, measured):
    doc = _doc({})
    doc["jobs"]["lint"]["steps"][2] = (run_or_step if isinstance(run_or_step, dict)
                                       else {"run": run_or_step})
    return _run(None, doc=doc, pole=_pole(step=measured))


def test_a_nameless_rust_step_whose_yaml_join_fails_is_the_verdict():
    # The matrix expression makes the measured name differ from the YAML
    # display, so the join fails; a Rust lint is never a tool swap (JS
    # manifest present).
    out, withheld, cands = _nameless(_CLIPPY_YAML, _CLIPPY_MEASURED)
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


def test_a_joined_rust_step_with_an_expression_is_the_verdict():
    out, withheld, cands = _nameless(_CLIPPY_YAML, "Run " + _CLIPPY_YAML)
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


def test_a_nameless_js_step_with_an_expression_is_still_held_back():
    run = "pnpm lint --filter ${{ matrix.pkg }}"
    out, withheld, cands = _nameless(run, "Run " + run)
    assert out == [] and withheld == {"command_has_expression": 1}, withheld
    assert [c["gate"] for c in cands] == ["command_has_expression"]


def test_a_joined_step_mixing_a_non_js_tool_and_a_table_tool_is_not_the_verdict():
    # The YAML joins, so its whole command is read, not just the first line
    # GitHub puts in the name: the `npx eslint` keeps the normal path.
    out, withheld, cands = _nameless("cargo build\nnpx eslint .", "Run cargo build")
    assert len(out) == 1, (withheld, cands)


def test_a_nameless_make_step_in_a_js_repo_is_still_held_back():
    # `make` runs whatever the Makefile says, ESLint included.
    out, withheld, cands = _nameless("make lint", "Run make lint")
    assert out == [] and withheld == {"delegated_script_unresolved": 1}, withheld


def test_a_nameless_action_named_after_a_tool_is_not_read_as_that_tool():
    # `Run actions-rs/cargo@v1` is an action ref, not a `cargo` command.
    out, withheld, cands = _nameless({"uses": "some-org/make@v1"},
                                     "Run some-org/make@v2")
    assert withheld == {"measured_step_not_matched_in_yaml": 1}, withheld


@pytest.mark.parametrize("uses", ["rustsec/audit-check@v2",
                                  "EmbarkStudios/cargo-deny-action@v2",
                                  "taiki-e/install-action@v2",
                                  "obi1kenobi/cargo-semver-checks-action@v2"])
def test_rust_ecosystem_actions_are_the_verdict(uses):
    out, withheld, cands = _nameless({"uses": uses, "name": "rust audit"},
                                     "rust audit")
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


def test_an_unknown_eslint_action_is_still_held_back():
    out, withheld, cands = _nameless(
        {"uses": "some-org/eslint-action@v1", "name": "Do the thing"}, "Do the thing")
    assert out == [] and withheld == {"candidate_is_an_action": 1}, withheld
    assert [c["gate"] for c in cands] == ["candidate_is_an_action"]


# ---- E1. wrappers and shims re-classify what they run --------------------------

def test_corepack_runs_the_package_manager_and_its_script_is_followed():
    out, withheld, cands = _run({"run": "corepack pnpm lint"},
                                block=_block(scripts={"": {"lint": "eslint ."}}))
    assert len(out) == 1 and _rows(out[0]) == ["eslint"], (withheld, cands)


@pytest.mark.parametrize("run,gate", [
    ("sudo make lint", "delegated_script_unresolved"),
    ("sudo -E make lint", "delegated_script_unresolved"),
    ("doas make lint", "delegated_script_unresolved"),
    ("env CI=1 make lint", "delegated_script_unresolved"),
    ("uv run pre-commit run --all-files", "delegated_script_unresolved"),
    ("uv run --frozen pre-commit run --all-files", "delegated_script_unresolved"),
    ("uvx pre-commit run --all-files", "delegated_script_unresolved"),
    ("poetry run pre-commit run -a", "delegated_script_unresolved"),
    ("pipx run pre-commit run", "delegated_script_unresolved"),
    ("bundle exec pre-commit run", "delegated_script_unresolved"),
    ("lefthook run pre-commit", "delegated_script_unresolved"),
    ("npx lint-staged", "delegated_script_unresolved"),
    ("npx husky", "delegated_script_unresolved"),
    ("gmake lint", "delegated_script_unresolved"),
    ("bazel test //...", "delegated_script_unresolved"),
    ("bazelisk test //...", "delegated_script_unresolved"),
    ("deno task lint", "delegated_script_unresolved"),
    ("python scripts/lint.py", "script_unresolvable"),
    ("python3 -u scripts/lint.py --fix", "script_unresolvable"),
    ("uv run python scripts/lint.py", "script_unresolvable"),
    ("deno run -A lint.ts", "script_unresolvable"),
    ("cargo run --bin xtask -- lint", "script_unresolvable"),
    ("uv run --directory web pre-commit run", "working_directory_unknown"),
])
def test_a_wrapper_or_shim_in_a_js_repo_holds_the_step_back(run, gate):
    out, withheld, cands = _run({"run": run})
    assert out == []
    _gate_only(withheld, cands, gate)


@pytest.mark.parametrize("run,row", [
    ("sudo npx eslint .", "eslint"),
    ("uv run npx prettier --check .", "prettier"),
    ("bundle exec npx eslint .", "eslint"),
    ("deno run -A npm:eslint .", "eslint"),
])
def test_a_wrapper_runs_a_table_tool_and_it_fires(run, row):
    out, withheld, cands = _run({"run": run})
    assert len(out) == 1 and _rows(out[0]) == [row], (withheld, cands)


@pytest.mark.parametrize("run", ["uv sync --frozen", "poetry install", "bundle install",
                                 "bundle exec rubocop", "python -m pytest",
                                 "corepack enable", "deno fmt --check"])
def test_a_wrapper_tool_that_runs_no_hidden_command_is_the_verdict(run):
    out, withheld, cands = _run({"run": run})
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


@pytest.mark.parametrize("run", ["uv run pre-commit run --all-files",
                                 "lefthook run pre-commit", "python scripts/lint.py",
                                 "bazel test //..."])
def test_a_wrapper_or_shim_in_a_repo_with_no_manifest_is_the_verdict(run):
    out, withheld, cands = _no_js({"run": run})
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


@pytest.mark.parametrize("run", ["uv run pre-commit run --all-files",
                                 "poetry run pre-commit run -a",
                                 "sudo make lint", "bazel test //..."])
def test_a_nameless_wrapped_runner_in_a_js_repo_is_held_not_a_verdict(run):
    out, withheld, cands = _nameless(run, "Run " + run)
    assert out == [] and set(withheld) <= cr._OPT84_HELD_BACK_GATES, withheld
    assert len(cands) == 1, cands


@pytest.mark.parametrize("cmd", ["uv run pre-commit run --all-files",
                                 "poetry run pre-commit run -a",
                                 "pipx run pre-commit run", "bundle exec make lint",
                                 "cargo run --bin xtask", "sudo make lint",
                                 "bazel test //...", "uv run python scripts/lint.py"])
def test_the_nameless_non_js_read_reclassifies_a_wrapper(cmd):
    assert cr._opt84_runs_only_non_js(cmd) is False


@pytest.mark.parametrize("cmd", ["cargo clippy --all-targets", "uv sync --frozen",
                                 "poetry install", "bundle install", "time cargo build",
                                 "sudo cmake --build bld"])
def test_the_nameless_non_js_read_still_reads_plain_non_js_tools(cmd):
    assert cr._opt84_runs_only_non_js(cmd) is True


# ---- E3. --parser-options turns on type-aware linting --------------------------

@pytest.mark.parametrize("flag", ["--parser-options project:./tsconfig.json",
                                  "--parser-options=project:./tsconfig.json"])
def test_eslint_parser_options_on_the_command_is_run_both(flag):
    t = _efit(f"npx eslint {flag} .")
    assert t["fit"] == "dual-run", t
    assert any("`--parser-options`" in r and "type-aware" in r
               for r in t["fit_reasons"]), t["fit_reasons"]


@pytest.mark.parametrize("flag", ["-f ./formatter.js", "--format=stylish"])
def test_an_eslint_custom_formatter_is_not_a_reason(flag):
    t = _efit(f"npx eslint {flag} .")
    assert t["fit"] == "drop-in", t


# ---- E4. the nameless fallback needs the YAML job to join ----------------------

def test_a_nameless_rust_step_with_no_workflow_yaml_is_held_back():
    withheld: dict[str, int] = {}
    cands: list[dict] = []
    out = cr._detect_opt84_tool_swap(
        [_pole(step="Run cargo fmt --check")], {}, _block(), [], 40,
        withheld=withheld, withheld_candidates=cands)
    assert out == [] and withheld == {"workflow_yaml_unavailable": 1}, withheld
    assert [c["gate"] for c in cands] == ["workflow_yaml_unavailable"]


def test_a_nameless_rust_step_on_an_unmatched_job_is_held_back():
    doc = _doc({"run": "cargo fmt --check"})
    doc["jobs"] = {"other": {**doc["jobs"]["lint"], "name": "Other"}}
    out, withheld, cands = _run(None, doc=doc, pole=_pole(step="Run cargo fmt --check"))
    assert out == [] and withheld == {"pole_job_not_matched_in_yaml": 1}, withheld
    assert [c["gate"] for c in cands] == ["pole_job_not_matched_in_yaml"]


def test_a_nameless_rust_step_with_duplicate_display_names_is_the_verdict():
    doc = _doc({})
    doc["jobs"]["lint"]["steps"][2] = {"run": "cargo fmt --check"}
    doc["jobs"]["lint"]["steps"].append({"run": "cargo fmt --check"})
    out, withheld, cands = _run(None, doc=doc, pole=_pole(step="Run cargo fmt --check"))
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld


# ---- E6. `tsc --noEmit false` emits --------------------------------------------

@pytest.mark.parametrize("run", ["npx tsc --noEmit false", "npx tsc -p . --noemit FALSE"])
def test_tsc_noemit_false_emits(run):
    out, withheld, cands = _run({"run": run})
    assert out == [] and withheld == {"tsc_emits_not_in_v1": 1}, withheld


def test_tsc_noemit_true_still_fires():
    out, withheld, cands = _run({"run": "npx tsc --noEmit true"})
    assert len(out) == 1 and _rows(out[0]) == ["tsc"], withheld

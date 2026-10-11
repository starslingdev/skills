"""OPT84 — A slow step on the long pole runs a tool that has a native drop-in.

Engine half: the scan facts it reads (`scan._read_type_aware_lint`'s
`tool_versions`, `config_files`, `eslint_config_facts`), the shared command
resolver (`collect_runs._resolve_run_leaves`, which OPT82 also calls through),
and the detector (`collect_runs._detect_opt84_tool_swap`): one fixture per
shape, every gate, and the finding stamp the renderer and verifier build on.
"""
from __future__ import annotations

import gzip
import inspect
import json
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
import collect_runs as cr  # noqa: E402
import scan  # noqa: E402

_WF = ".github/workflows/lint.yml"
_STEP = "Run lint and format checks"


# ---- fixtures ----------------------------------------------------------------

def _doc(step, *, job_defaults=None, wf_defaults=None, wrap_parallel=False):
    target = {"name": _STEP, **step}
    if wrap_parallel:
        target = {"parallel": [target, {"name": "Spell check", "run": "npx cspell ."}]}
    job = {"name": "Lint", "runs-on": "ubuntu-latest",
           "steps": [{"uses": "actions/checkout@v4"}, {"run": "pnpm install"}, target]}
    if job_defaults:
        job["defaults"] = {"run": job_defaults}
    doc = {"name": "Lint", "on": "pull_request", "jobs": {"lint": job}}
    if wf_defaults:
        doc["defaults"] = {"run": wf_defaults}
    return doc


def _pole(step_p50=52.0, job_p50=162.0, category="scan", step=_STEP):
    rest = round(job_p50 - step_p50 - 5.0, 1)
    return {"check": "Lint", "workflow_file": _WF, "job": "Lint", "job_p50_s": job_p50,
            "steps": [{"step": step, "category": category, "p50_s": step_p50},
                      {"step": "Run pnpm install", "category": "install", "p50_s": rest},
                      {"step": "Set up job", "category": "setup", "p50_s": 5.0}]}


def _config(path="eslint.config.js", **kw):
    c = {"path": path, "dir": path.rsplit("/", 1)[0] if "/" in path else "",
         "format": "flat", "type_aware": "off", "type_aware_evidence": [],
         "unresolved": [], "presets": [], "rules": [], "rule_unresolved": [],
         "custom_rules": [], "unresolved_imports": [], "sets_off": False,
         "root": False, "shared_configs": []}
    c.update(kw)
    return c


_NO_FACTS = {"plugins": [], "local_rules": False, "exhaustive_deps": False}


def _block(scripts=None, tool_versions=None, configs=None, facts=None,
           unreadable_pkgs=(), eslint_unreadable=(), truncated=False):
    return scan._tal_block(
        configs=[_config()] if configs is None else configs,
        unreadable=list(eslint_unreadable),
        truncated=truncated,
        package_scripts={"": {}} if scripts is None else scripts,
        package_scripts_unreadable=list(unreadable_pkgs),
        tool_versions=({"": {"eslint": "^9.10.0", "prettier": "^3.3.0",
                             "typescript": "^5.6.2"}}
                       if tool_versions is None else tool_versions),
        eslint_config_facts=({"eslint.config.js": dict(_NO_FACTS)}
                             if facts is None else facts))


def _run(step, *, pole=None, block=None, opt82=None, doc=None, poles=None):
    withheld: dict[str, int] = {}
    cands: list[dict] = []
    out = cr._detect_opt84_tool_swap(
        poles if poles is not None else [pole or _pole()],
        {_WF: doc if doc is not None else _doc(step)},
        _block() if block is None else block, opt82 or [], 40,
        withheld=withheld, withheld_candidates=cands)
    return out, withheld, cands


_MASTRA = {"": {"lint": "eslint . && prettier --check ."}}


def _rows(f):
    return [t["row"] for t in f["tool_swap"]["tools"]]


def _tool(f, row):
    return next(t for t in f["tool_swap"]["tools"] if t["row"] == row)


def _held(cands, gate):
    return [{"workflow_file": _WF, "job": "Lint", "gate": gate, "step": _STEP}] == cands


# ---- OPT82 keeps its results through the shared resolver ------------------------

_SNAPSHOT = _SKILL_DIR / "tests" / "fixtures" / "opt82_resolver_snapshot.json.gz"


def _norm(x):
    return json.loads(json.dumps(
        x, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o)))


def _snapshot():
    return json.loads(gzip.decompress(_SNAPSHOT.read_bytes()))


def test_opt82_resolver_results_are_byte_identical_to_the_pre_refactor_snapshot():
    # Recorded from every `_opt82_resolve_lint` call the OPT82 test module made on
    # main a7c2db4, before the resolver was generalized.
    snap = _snapshot()
    assert len(snap["resolve"]) > 100
    for e in snap["resolve"]:
        cmd, wd, sbd, chain, depth, unreadable, runner = e["args"]
        got = cr._opt82_resolve_lint(cmd, wd, sbd, chain, depth, frozenset(unreadable),
                                     runner)
        assert _norm(got) == e["out"], e["args"]


def test_opt82_detector_results_are_byte_identical_to_the_pre_refactor_snapshot():
    snap = _snapshot()
    assert len(snap["detect"]) > 50
    for e in snap["detect"]:
        wf, jpr, crit, doc, block, idx = e["args"]
        w = None if e["withheld_in"] is None else dict(e["withheld_in"])
        c = None if e["cands_in"] is None else list(e["cands_in"])
        got = cr._detect_opt82_type_aware_lint(wf, jpr, crit, doc, block, idx, w, c)
        assert _norm(got) == e["out"]
        assert _norm(w) == e["withheld"] and _norm(c) == e["cands"]


def test_opt82_resolver_calls_through_the_shared_leaf_resolver():
    assert "_resolve_run_leaves(" in inspect.getsource(cr._opt82_resolve_lint)


# ---- the shared resolver ---------------------------------------------------------

def test_resolver_returns_every_leaf_through_package_scripts():
    res = cr._resolve_run_leaves("pnpm lint", "", _MASTRA)
    assert [lf["command"] for lf in res.leaves] == ["eslint .", "prettier --check ."]
    assert all(lf["chain"] == ["pnpm run lint"] for lf in res.leaves)
    assert all(lf["runner"] == "pnpm exec" for lf in res.leaves)
    assert res.unresolved == []


def test_resolver_splits_on_every_separator_and_unwraps_runners():
    res = cr._resolve_run_leaves("npx eslint . || true; pnpm exec prettier -c .\n"
                                 "yarn tsc --noEmit | tee out", "", {"": {}},
                                 bin_names=cr._OPT84_BIN_NAMES)
    assert [lf["command"] for lf in res.leaves] == [
        "eslint .", "true", "prettier -c .", "tsc --noEmit", "tee out"]


@pytest.mark.parametrize("cmd,scripts,unreadable,kind", [
    ("npx nx format:check --all", {"": {}}, (), "delegated"),
    ("node ./script/lint.js", {"": {}}, (), "delegated"),
    ("pnpm run missing", {"": {}}, (), "script"),
    ("pnpm lint", {}, ("",), "package_json"),
    ("pnpm ${{ matrix.task }}", {"": {}}, (), "expression"),
    ("cd $DIR && pnpm lint", _MASTRA, (), "cd"),
])
def test_resolver_records_what_it_cannot_follow(cmd, scripts, unreadable, kind):
    res = cr._resolve_run_leaves(cmd, "", scripts, frozenset(unreadable))
    assert [u["kind"] for u in res.unresolved][:1] == [kind], res.unresolved
    assert all(u.get("reason") for u in res.unresolved)


def test_an_undeclared_package_manager_name_fails_closed_unless_it_is_a_table_binary():
    res = cr._resolve_run_leaves("pnpm vitest run", "", {"": {}},
                                 bin_names=cr._OPT84_BIN_NAMES)
    assert [u["kind"] for u in res.unresolved] == ["script"] and res.leaves == []
    res = cr._resolve_run_leaves("pnpm prettier --check .", "", {"": {}},
                                 bin_names=cr._OPT84_BIN_NAMES)
    assert [lf["command"] for lf in res.leaves] == ["pnpm prettier --check ."]
    assert res.unresolved == []


def test_resolver_stops_at_the_depth_cap():
    scripts = {"": {f"s{i}": f"pnpm s{i + 1}" for i in range(8)}}
    res = cr._resolve_run_leaves("pnpm s0", "", scripts)
    assert [u["kind"] for u in res.unresolved] == ["script"]


# ---- the scan facts ------------------------------------------------------------

def _repo(tmp_path, pkg, files=None):
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    for name, text in (files or {}).items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_scan_records_tool_versions_and_config_files(tmp_path):
    root = _repo(tmp_path, {
        "scripts": {"lint": "eslint ."},
        "dependencies": {"next": "15.1.0", "react": "19.0.0"},
        "devDependencies": {"eslint": "^9.10.0", "prettier": "^3.3.0",
                            "typescript": "^5.6.2", "oxlint": {"bad": 1}}},
        {"eslint.config.js": "export default [];\n", ".prettierrc.json": "{}",
         "next.config.mjs": "export default {};\n", "babel.config.js": "",
         "README.md": ""})
    block = scan._read_type_aware_lint(root)
    assert block["tool_versions"] == {"": {"typescript": "^5.6.2", "prettier": "^3.3.0",
                                           "eslint": "^9.10.0", "next": "15.1.0"}}
    assert block["config_files"] == {"": [".prettierrc.json", "babel.config.js",
                                          "next.config.mjs"]}
    # The existing fields are untouched.
    assert block["eslint_versions"] == {"": "^9.10.0"}
    assert block["package_scripts"] == {"": {"lint": "eslint ."}}


def test_scan_records_eslint_config_fit_facts(tmp_path):
    cfg = ("import react from 'eslint-plugin-react';\n"
           "import tailwind from 'eslint-plugin-tailwindcss';\n"
           "import local from './tools/eslint-rules/plugin.js';\n"
           "export default [{ plugins: { react, tailwind, local },\n"
           "  rules: { 'react-hooks/exhaustive-deps': 'error' } }];\n")
    block = scan._read_type_aware_lint(_repo(tmp_path, {}, {"eslint.config.js": cfg}))
    facts = block["eslint_config_facts"]["eslint.config.js"]
    assert {"react", "tailwindcss"} <= set(facts["plugins"])
    assert facts["local_rules"] is True and facts["exhaustive_deps"] is True


def test_scan_records_plain_config_without_fit_facts(tmp_path):
    cfg = "import js from '@eslint/js';\nexport default [js.configs.recommended];\n"
    block = scan._read_type_aware_lint(_repo(tmp_path, {}, {"eslint.config.js": cfg}))
    assert block["eslint_config_facts"]["eslint.config.js"] == _NO_FACTS


def test_scan_block_defaults_are_in_the_one_shape_definition():
    b = scan._tal_block()
    assert b["tool_versions"] == {} and b["config_files"] == {}
    assert b["eslint_config_facts"] == {}


def test_scan_lists_opt84_as_a_routed_structural_pattern():
    src = inspect.getsource(scan.scan)
    assert '"OPT84"' in src.split("structural_without_detector")[0]


# ---- firing shapes ---------------------------------------------------------------

def test_mastra_one_step_two_tools_one_finding_with_the_step_as_ceiling():
    out, withheld, cands = _run({"run": "pnpm lint"}, block=_block(scripts=_MASTRA))
    assert len(out) == 1, withheld
    f = out[0]
    ts = f["tool_swap"]
    assert ts["kind"] == "opt84_tool_swap"
    assert _rows(f) == ["eslint", "prettier"]
    assert ts["resolved_leaves"] == ["eslint .", "prettier --check ."]
    assert ts["command"] == "pnpm lint" and ts["step"] == _STEP
    assert ts["ceiling_s"] == 52.0 and ts["step_p50_s"] == 52.0
    assert ts["split_unknown"] is True
    assert ts["ceiling_basis"] == cr._OPT84_CEILING_BASIS
    assert ts["share"] == round(52.0 / 162.0, 4)
    assert ts["min_share"] == cr._OPT84_MIN_SHARE and ts["min_step_s"] == cr._OPT84_MIN_STEP_S
    assert ts["job"] == "Lint" and ts["yaml_job"] == "lint" and ts["workflow_file"] == _WF
    assert ts["pole_check"] == "Lint" and ts["job_p50_s"] == 162.0
    assert ts["category"] == "scan" and ts["working_directory"] == ""
    assert _tool(f, "eslint")["fit"] == "drop-in"
    assert _tool(f, "eslint")["target"] == "oxlint"
    assert _tool(f, "prettier")["target"] == "oxfmt"
    assert _tool(f, "eslint")["source_version"] == "^9.10.0"
    assert f["severity"] == "HIGH" and f["risk"] == "HIGH"
    assert f["pattern"] == "OPT84" and f["id"] == "f41"
    assert f["pattern_class"] == "structural" and f["sizing_basis"] == "uncredited"
    assert f["realization"] == "none"
    assert withheld == {} and cands == []


def test_the_ceiling_is_never_a_saving():
    out, _w, _c = _run({"run": "pnpm lint"}, block=_block(scripts=_MASTRA))
    f = out[0]
    assert f["wall_clock_p50_s"] is None and f["runner_min_saving"] is None
    assert "OPT84" not in cr._SIZING
    assert cr._RM_DOOR_OVERRIDES["OPT84"][0] == cr._RM_DOOR_NOT_DERIVABLE
    assert "uncredited" in f["size_note"] and "CEILING" in f["size_note"]


def test_every_tool_carries_two_benchmark_commands_and_docs():
    out, _w, _c = _run({"run": "pnpm lint"}, block=_block(scripts=_MASTRA))
    for t in out[0]["tool_swap"]["tools"]:
        assert len(t["benchmark_commands"]) == 2
        assert all(c.startswith("(time ") for c in t["benchmark_commands"])
        assert t["target"].split()[0] in t["benchmark_commands"][1]
        assert t["docs_url"] == cr._OPT84_DOCS[t["row"]]["docs_url"]
        assert t["leaf_command"] and t["source_tool"]
        assert isinstance(t["fit_reasons"], list)


def test_published_range_is_stamped_per_tool_as_the_catalog_states_it():
    out, _w, _c = _run({"run": "pnpm lint && npx tsc --noEmit"},
                       block=_block(scripts=_MASTRA))
    got = {t["row"]: t["published_range"] for t in out[0]["tool_swap"]["tools"]}
    assert got == {"eslint": "~8-20×", "prettier": "~4-30×",
                   "tsc": "8-12× (vendor-stated)"}


def test_docs_table_points_at_the_target_tools_own_pages():
    d = cr._OPT84_DOCS
    assert set(d) == {"prettier", "eslint", "tsc"}
    assert d["prettier"]["docs_url"].startswith("https://oxc.rs/")
    assert d["eslint"]["docs_url"].startswith("https://oxc.rs/")
    assert d["tsc"]["docs_url"].startswith("https://devblogs.microsoft.com/typescript/")


def test_guardrail_and_rollout_are_one_sentence_each():
    out, _w, _c = _run({"run": "pnpm lint"}, block=_block(scripts=_MASTRA))
    for key in ("guardrail", "rollout"):
        text = out[0][key]
        assert text.endswith(".") and ". " not in text, text


def test_contract_names():
    assert cr._OPT84_TITLE == ("A Slow Step on the Long Pole Runs a Tool That Has a "
                               "Native Drop-in")
    assert cr._OPT84_FIX_STRATEGY == "slow-tool-on-pole-has-native-drop-in"
    assert cr._OPT84_WITHHELD_DOC_KEY == "opt84_withheld_candidates"
    assert cr._OPT84_MIN_STEP_S == 20.0 and cr._OPT84_MIN_SHARE == 0.15


@pytest.mark.parametrize("run", ["npx prettier --check .", "prettier -c src",
                                 "pnpm exec prettier --list-different .",
                                 "npx prettier -l .", "npx pretty-quick --check"])
def test_tauri_prettier_check_is_the_prettier_row(run):
    out, withheld, _c = _run({"run": run})
    assert len(out) == 1, withheld
    f = out[0]
    assert _rows(f) == ["prettier"]
    assert _tool(f, "prettier")["fit"] == "drop-in"
    assert _tool(f, "prettier")["source_version"] == "^3.3.0"
    assert f["tool_swap"]["split_unknown"] is False
    assert f["severity"] == "MEDIUM" and f["risk"] == "MEDIUM"


def test_prettier_write_is_not_in_the_table():
    out, withheld, cands = _run({"run": "npx prettier --write ."})
    assert out == [] and withheld == {"tool_not_in_table": 1} and cands == []


@pytest.mark.parametrize("run", ["npx tsc --noEmit", "tsc -p tsconfig.json --noEmit",
                                 "pnpm exec tsc --project web --noEmit"])
def test_tsc_noemit_with_typescript_5_is_the_tsc_row(run):
    out, withheld, _c = _run({"run": run})
    assert len(out) == 1, withheld
    t = _tool(out[0], "tsc")
    assert t["fit"] == "drop-in" and t["source_version"] == "^5.6.2"
    assert t["typescript6_alias_needed"] is False
    assert out[0]["severity"] == "MEDIUM"


def test_tsc_row_needs_the_typescript6_alias_when_any_eslint_config_is_type_aware():
    block = _block(configs=[_config(), _config("web/eslint.config.js", type_aware="on")])
    out, _w, _c = _run({"run": "npx tsc --noEmit"}, block=block)
    assert _tool(out[0], "tsc")["typescript6_alias_needed"] is True


def test_tsc_already_on_typescript_7_is_silent():
    block = _block(tool_versions={"": {"typescript": "^7.0.2"}})
    out, withheld, cands = _run({"run": "npx tsc --noEmit"}, block=block)
    assert out == [] and withheld == {"target_already_in_use": 1} and cands == []


@pytest.mark.parametrize("run", ["npx tsc -p tsconfig.build.json",
                                 "tsc --build --noEmit", "tsc -b"])
def test_emitting_tsc_is_a_v1_verdict(run):
    out, withheld, cands = _run({"run": run})
    assert out == [] and withheld == {"tsc_emits_not_in_v1": 1} and cands == []


@pytest.mark.parametrize("tv", [{"": {"eslint": "^9"}}, {"": {"typescript": "workspace:*"}},
                                {"": {"typescript": "catalog:"}}])
def test_tsc_with_an_unreadable_typescript_version_is_held_back(tv):
    out, withheld, cands = _run({"run": "npx tsc --noEmit"}, block=_block(tool_versions=tv))
    assert out == [] and withheld == {"typescript_version_unreadable": 1}
    assert _held(cands, "typescript_version_unreadable")


def test_mastodon_oxfmt_already_in_use_leaves_only_the_eslint_row_as_dual_run():
    scripts = {"": {"lint": "eslint --cache .", "format:check": "oxfmt --check"}}
    facts = {"eslint.config.js": {"plugins": ["jsx-a11y", "react"], "local_rules": False,
                                  "exhaustive_deps": False}}
    out, withheld, _c = _run({"run": "pnpm format:check && pnpm lint"},
                             block=_block(scripts=scripts, facts=facts))
    assert len(out) == 1, withheld
    f = out[0]
    assert _rows(f) == ["eslint"]
    t = _tool(f, "eslint")
    assert t["fit"] == "dual-run" and any("react" in r for r in t["fit_reasons"])
    assert "--cache" not in t["benchmark_commands"][0]
    assert f["tool_swap"]["split_unknown"] is False


def test_eslint_beside_oxlint_is_already_swapped():
    out, withheld, _c = _run({"run": "npx oxlint . && npx eslint ."})
    assert out == [] and withheld == {"target_already_in_use": 1}


@pytest.mark.parametrize("facts,config_kw,reason", [
    ({"plugins": [], "local_rules": True, "exhaustive_deps": False}, {}, "custom"),
    ({"plugins": [], "local_rules": False, "exhaustive_deps": False},
     {"custom_rules": [{"rule": "local/no-x", "source": "rules/no-x.js"}]}, "custom"),
    ({"plugins": ["tailwindcss"], "local_rules": False, "exhaustive_deps": False}, {},
     "tailwindcss"),
    ({"plugins": ["vue"], "local_rules": False, "exhaustive_deps": False}, {}, "vue"),
    ({"plugins": [], "local_rules": False, "exhaustive_deps": True}, {}, "exhaustive-deps"),
    ({"plugins": [], "local_rules": False, "exhaustive_deps": False},
     {"shared_configs": ["eslint-config-acme"]}, "eslint-config-acme"),
])
def test_eslint_with_rules_oxlint_may_lack_is_dual_run(facts, config_kw, reason):
    block = _block(configs=[_config(**config_kw)], facts={"eslint.config.js": facts})
    out, withheld, _c = _run({"run": "npx eslint ."}, block=block)
    assert len(out) == 1, withheld
    t = _tool(out[0], "eslint")
    assert t["fit"] == "dual-run"
    assert any(reason in r for r in t["fit_reasons"]), t["fit_reasons"]
    assert out[0]["risk"] == "HIGH" and out[0]["severity"] == "HIGH"


def test_eslint_without_any_gate_is_drop_in():
    out, _w, _c = _run({"run": "npx eslint ."})
    t = _tool(out[0], "eslint")
    assert t["fit"] == "drop-in" and t["fit_reasons"] == []


@pytest.mark.parametrize("kw", [
    {"configs": [], "eslint_unreadable": ["eslint.config.js"]},
    {"configs": []},
    {"configs": [], "truncated": True},
])
def test_eslint_whose_config_cannot_be_read_is_held_back(kw):
    out, withheld, cands = _run({"run": "npx eslint ."}, block=_block(**kw))
    assert out == [] and withheld == {"eslint_config_unreadable": 1}
    assert _held(cands, "eslint_config_unreadable")


def test_opt82_on_the_step_defers_eslint_and_keeps_prettier():
    opt82 = [{"pattern": "OPT82", "workflow_file": _WF, "job": "Lint",
              "type_aware_lint": {"yaml_job": "lint", "lint_step": _STEP}}]
    out, withheld, _c = _run({"run": "pnpm lint"}, block=_block(scripts=_MASTRA),
                             opt82=opt82)
    assert len(out) == 1, withheld
    f = out[0]
    assert _tool(f, "eslint")["fit"] == "deferred_to_opt82"
    assert _tool(f, "prettier")["fit"] == "drop-in"
    assert f["severity"] == "MEDIUM" and f["risk"] == "MEDIUM"


def test_opt82_on_an_eslint_only_step_is_a_verdict():
    opt82 = [{"pattern": "OPT82", "workflow_file": _WF, "job": "Lint",
              "type_aware_lint": {"yaml_job": "lint", "lint_step": _STEP}}]
    out, withheld, cands = _run({"run": "npx eslint ."}, opt82=opt82)
    assert out == [] and withheld == {"eslint_reported_by_opt82": 1} and cands == []


def test_opt82_on_another_step_does_not_defer():
    opt82 = [{"pattern": "OPT82", "workflow_file": _WF, "job": "Lint",
              "type_aware_lint": {"yaml_job": "lint", "lint_step": "Other lint"}}]
    out, _w, _c = _run({"run": "npx eslint ."}, opt82=opt82)
    assert _tool(out[0], "eslint")["fit"] == "drop-in"


def test_a_step_inside_a_parallel_group_is_still_a_candidate():
    out, withheld, _c = _run(None, doc=_doc({"run": "npx prettier --check ."},
                                            wrap_parallel=True))
    assert len(out) == 1, withheld
    assert _rows(out[0]) == ["prettier"]


def test_working_directory_is_read_for_the_manifest():
    block = _block(scripts={"": {}, "web": _MASTRA[""]},
                   tool_versions={"web": {"eslint": "^8.57.0", "prettier": "^2.8.0"}},
                   configs=[_config("web/eslint.config.js")],
                   facts={"web/eslint.config.js": dict(_NO_FACTS)})
    out, withheld, _c = _run({"run": "pnpm lint", "working-directory": "./web"},
                             block=block)
    assert len(out) == 1, withheld
    ts = out[0]["tool_swap"]
    assert ts["working_directory"] == "web"
    assert _tool(out[0], "prettier")["source_version"] == "^2.8.0"
    assert all(c.startswith("(cd web && time ")
               for t in ts["tools"] for c in t["benchmark_commands"])


# ---- held back -----------------------------------------------------------------

@pytest.mark.parametrize("step,gate", [
    ({"run": "npx nx format:check --all"}, "delegated_script_unresolved"),
    ({"run": "pnpm turbo run lint"}, "delegated_script_unresolved"),
    ({"run": "node ./script/lint.js"}, "script_unresolvable"),
    ({"run": "pnpm run lint:missing"}, "script_unresolvable"),
    ({"run": "pnpm ${{ matrix.task }}"}, "command_has_expression"),
    ({"run": "npx eslint .", "env": {"X": "${{ secrets.T }}"}}, "command_has_expression"),
    ({"run": "npx eslint .", "working-directory": "${{ inputs.dir }}"},
     "command_has_expression"),
    ({"run": "npx eslint .", "shell": "${{ matrix.shell }}"}, "command_has_expression"),
    ({"run": "cd web && npx eslint ."}, "command_changes_directory"),
    ({"run": "npx eslint .", "working-directory": "../elsewhere"},
     "working_directory_unknown"),
    ({"uses": "wearerequired/lint-action@v2"}, "candidate_is_an_action"),
])
def test_held_back_with_job_and_step(step, gate):
    out, withheld, cands = _run(step)
    assert out == [] and withheld == {gate: 1}, withheld
    assert _held(cands, gate), cands


def test_expression_in_defaults_working_directory_is_held_back():
    doc = _doc({"run": "npx eslint ."}, job_defaults={"working-directory": "${{ inputs.d }}"})
    out, withheld, cands = _run(None, doc=doc)
    assert out == [] and withheld == {"command_has_expression": 1}
    assert _held(cands, "command_has_expression")


def test_unreadable_package_json_is_held_back():
    out, withheld, cands = _run({"run": "pnpm lint"},
                                block=_block(scripts={}, unreadable_pkgs=[""]))
    assert out == [] and withheld == {"package_json_unreadable": 1}
    assert _held(cands, "package_json_unreadable")


# ---- pole-level exits ------------------------------------------------------------

def test_step_below_the_seconds_floor_is_no_candidate():
    out, withheld, cands = _run({"run": "npx eslint ."},
                                pole=_pole(step_p50=19.9, job_p50=40.0))
    assert out == [] and withheld == {"no_candidate_step": 1} and cands == []


def test_step_below_the_share_floor_is_no_candidate():
    # 25s is above the 20s floor and only 12.5% of a 200s pole.
    out, withheld, cands = _run({"run": "npx eslint ."},
                                pole=_pole(step_p50=25.0, job_p50=200.0))
    assert out == [] and withheld == {"no_candidate_step": 1} and cands == []


@pytest.mark.parametrize("p50,job,fires", [(20.0, 100.0, True), (30.0, 200.0, True),
                                           (29.9, 200.0, False)])
def test_the_floors_are_inclusive(p50, job, fires):
    out, _w, _c = _run({"run": "npx eslint ."}, pole=_pole(step_p50=p50, job_p50=job))
    assert bool(out) is fires


@pytest.mark.parametrize("cat", ["test", "install", "package", "other"])
def test_only_scan_and_build_steps_are_candidates(cat):
    out, withheld, _c = _run({"run": "npx eslint ."}, pole=_pole(category=cat))
    assert out == [] and withheld == {"no_candidate_step": 1}


def test_build_category_is_a_candidate():
    out, _w, _c = _run({"run": "npx tsc --noEmit"}, pole=_pole(category="build"))
    assert len(out) == 1


def test_pole_level_gates():
    def gate(pole, doc=None):
        w: dict[str, int] = {}
        c: list[dict] = []
        cr._detect_opt84_tool_swap([pole], {_WF: doc} if doc is not None else {},
                                   _block(), [], 1, withheld=w, withheld_candidates=c)
        return w, c
    assert gate("nope")[0] == {"pole_not_file_backed": 1}
    assert gate({"job": "Lint"})[0] == {"pole_not_file_backed": 1}
    w, c = gate({"check": "Lint", "job": "Lint", "ambiguous_workflows": ["a", "b"]})
    assert w == {"pole_workflow_ambiguous": 1}
    assert c == [{"workflow_file": "", "job": "Lint", "gate": "pole_workflow_ambiguous"}]
    bad = _pole()
    bad["steps"][0]["p50_s"] = "n/a"
    assert gate(bad, _doc({"run": "npx eslint ."}))[0] == {"pole_step_timings_unavailable": 1}
    dup = _pole()
    dup["steps"][1]["step"] = _STEP
    assert gate(dup, _doc({"run": "npx eslint ."}))[0] == {"pole_step_entries_unreadable": 1}
    assert gate(_pole())[0] == {"workflow_yaml_unavailable": 1}
    other = _doc({"run": "npx eslint ."})
    other["jobs"]["lint"]["name"] = "Something else"
    assert gate(_pole(), other)[0] == {"pole_job_not_matched_in_yaml": 1}
    renamed = _doc({"run": "npx eslint ."})
    renamed["jobs"]["lint"]["steps"][-1]["name"] = "Renamed"
    w, c = gate(_pole(), renamed)
    assert w == {"measured_step_not_matched_in_yaml": 1}
    assert c == [{"workflow_file": _WF, "job": "Lint",
                  "gate": "measured_step_not_matched_in_yaml", "step": _STEP}]


def test_one_finding_per_pole_step_even_when_the_pole_is_listed_twice():
    seen: set = set()
    docs = {_WF: _doc({"run": "npx eslint ."})}
    a = cr._detect_opt84_tool_swap([_pole()], docs, _block(), [], 1, seen=seen)
    b = cr._detect_opt84_tool_swap([_pole()], docs, _block(), [], 2, seen=seen)
    assert len(a) == 1 and b == []


def test_gate_sets_are_pinned():
    assert cr._OPT84_VERDICT_GATES == frozenset({
        "pole_not_file_backed", "no_candidate_step", "tool_not_in_table",
        "target_already_in_use", "tsc_emits_not_in_v1", "eslint_reported_by_opt82"})
    assert cr._OPT84_HELD_BACK_GATES == frozenset({
        "pole_workflow_ambiguous", "pole_step_entries_unreadable",
        "pole_step_timings_unavailable", "workflow_yaml_unavailable",
        "pole_job_not_matched_in_yaml", "measured_step_not_matched_in_yaml",
        "command_has_expression", "command_changes_directory", "script_unresolvable",
        "delegated_script_unresolved", "package_json_unreadable",
        "candidate_is_an_action", "eslint_config_unreadable",
        "typescript_version_unreadable", "working_directory_unknown"})
    assert not cr._OPT84_VERDICT_GATES & cr._OPT84_HELD_BACK_GATES


def test_every_gate_the_detector_can_count_is_in_a_set():
    src = inspect.getsource(cr._detect_opt84_tool_swap)
    import re
    used = set(re.findall(r'_no\(\s*"([a-z0-9_]+)"', src)) | set(
        re.findall(r'"([a-z0-9_]+)"', inspect.getsource(cr._opt84_gate_for_unresolved)))
    allowed = cr._OPT84_VERDICT_GATES | cr._OPT84_HELD_BACK_GATES
    assert used and used - {"delegated", "script", "package_json", "expression", "cd",
                            "node", "tsx", "ts-node", "kind", "tool"} <= allowed


# ---- wiring ------------------------------------------------------------------

def test_collect_wires_opt84_after_opt83_with_its_own_counters():
    src = inspect.getsource(cr.collect)
    i83 = src.index("_detect_opt83_parallel_steps(")
    i84 = src.index("_detect_opt84_tool_swap(")
    assert i83 < i84
    tail = src[i84 - 2500:i84 + 2500]
    assert '"opt84_withheld_by_gate"' in tail
    assert "_OPT84_WITHHELD_DOC_KEY" in tail
    assert '_skip_detectors(' in tail and '"OPT84"' in tail


# ---- only JS tooling is in OPT84's world (curl: a C build is never a swap) -------

_CURL_BUILD = ("if [ \"${MATRIX_BUILD}\" = 'cmake' ]; then\n  cmake --build bld\n"
               "else\n  make -C bld V=1\nfi\n")


@pytest.mark.parametrize("run", [
    _CURL_BUILD, "make -C bld", "make lint", "./configure && make -j4",
    "cargo build --release", "go build ./...", "mvn -B verify", "gradle build",
    "for d in a b; do make -C $d; done", "case $X in a) cmake --build bld;; esac",
])
def test_a_non_js_build_is_a_tool_not_in_the_table_never_held_back(run):
    out, withheld, cands = _run({"run": run}, pole=_pole(category="build"),
                                block=_block(scripts={}))
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


def test_make_is_a_leaf_for_the_all_leaves_read_and_a_delegator_for_opt82():
    res = cr._resolve_run_leaves("make -C bld V=1", "", {})
    assert res.unresolved == [] and [lf["command"] for lf in res.leaves] == [
        "make -C bld V=1"]
    assert cr._opt82_resolve_lint("make lint", "", {}) == (
        "unresolvable", "delegated", [])


def test_shell_control_flow_around_js_tooling_still_reaches_the_tool():
    out, withheld, _c = _run(
        {"run": "if [ -n \"$CI\" ]; then\n  npx prettier --check .\nfi"})
    assert len(out) == 1 and _rows(out[0]) == ["prettier"], withheld


def test_a_node_script_stays_script_unresolvable():
    out, withheld, cands = _run({"run": "node ./script/lint.js"})
    assert withheld == {"script_unresolvable": 1} and _held(cands, "script_unresolvable")


# ---- JS task runners resolve through the workspace's package scripts -------------

# mastra's real shape (census of its package.json `lint` scripts): the root `lint`
# hands `lint` and `lint:format` to turbo; most packages run oxlint then ESLint, a
# few ESLint or Prettier alone; the root `lint:format` is oxfmt.
_MASTRA_REAL = {
    "": {"lint": 'pnpm turbo --filter "!./examples/**/*" --filter "!./docs/**/*" '
                 '--filter "!@internal/playground" lint lint:format',
         "lint:format": "oxfmt --check . '!docs/**/*' '!examples/**/*'",
         "typecheck": 'pnpm --filter "!./explorations/**/*" -r typecheck'},
    "packages/core": {"lint": "oxlint . && eslint ."},
    "packages/cli": {"lint": "oxlint . && eslint --no-warn-ignored ."},
    "stores/pg": {"lint": "eslint ."},
    "client-sdks/js": {"lint": "prettier --check '**/*.{js,ts}'"},
    "packages/_internals/x": {"lint": "oxlint --deny-warnings src test"},
}


def test_mastra_turbo_lint_resolves_and_both_rows_are_already_swapped():
    out, withheld, cands = _run({"run": "pnpm lint"}, block=_block(scripts=_MASTRA_REAL))
    assert out == [] and withheld == {"target_already_in_use": 1}, withheld
    assert cands == []


def test_resolver_expands_a_task_runner_over_every_package_defining_the_task():
    res = cr._resolve_run_leaves("pnpm lint", "", _MASTRA_REAL,
                                 bin_names=cr._OPT84_BIN_NAMES)
    assert res.unresolved == [], res.unresolved
    got = sorted((lf["wd"], lf["command"]) for lf in res.leaves)
    assert ("", "oxfmt --check . '!docs/**/*' '!examples/**/*'") in got
    assert ("stores/pg", "eslint .") in got and ("packages/core", "oxlint .") in got
    # the root `lint` is the script that called turbo: never re-entered
    assert not any(lf["command"].startswith("pnpm turbo") for lf in res.leaves)
    assert all(lf.get("task_runner", {}).get("tool") == "turbo" for lf in res.leaves)


_TURBO_ESLINT = {"": {"lint": "turbo run lint --filter=web"},
                 "apps/web": {"lint": "eslint ."},
                 "apps/docs": {"lint": "eslint src"},
                 "packages/ui": {"build": "tsup"}}


@pytest.mark.parametrize("run", ["pnpm lint", "pnpm turbo run lint --filter=web",
                                 "npx turbo lint --filter web"])
def test_turbo_eslint_across_packages_is_one_row_with_the_package_count(run):
    out, withheld, cands = _run({"run": run}, block=_block(scripts=_TURBO_ESLINT))
    assert len(out) == 1, (withheld, cands)
    assert _rows(out[0]) == ["eslint"]
    t = _tool(out[0], "eslint")
    assert t["packages"] == 2
    assert "across 2 workspace packages; task-runner filters not applied" in t[
        "fit_reasons"]
    assert t["fit"] == "drop-in"


@pytest.mark.parametrize("run,scripts", [
    ("pnpm -r typecheck", {"": {}, "a": {"typecheck": "tsc --noEmit"},
                           "b": {"typecheck": "tsc --noEmit -p ."}}),
    ("pnpm --filter ./packages/** run typecheck",
     {"": {}, "a": {"typecheck": "tsc --noEmit"}, "b": {"typecheck": "tsc --noEmit"}}),
    ("npx nx run-many -t typecheck", {"": {}, "a": {"typecheck": "tsc --noEmit"},
                                      "b": {"typecheck": "tsc --noEmit"}}),
    ("npx lerna run typecheck", {"": {}, "a": {"typecheck": "tsc --noEmit"},
                                 "b": {"typecheck": "tsc --noEmit"}}),
])
def test_every_task_runner_form_expands_through_the_workspace(run, scripts):
    out, withheld, _c = _run({"run": run}, block=_block(scripts=scripts))
    assert len(out) == 1, withheld
    assert _rows(out[0]) == ["tsc"] and _tool(out[0], "tsc")["packages"] == 2


@pytest.mark.parametrize("scripts", [
    {"": {}, "a": {"lint": "turbo run lint:inner"}, "b": {"lint": "eslint ."}},
    {"": {}, "a": {"lint": "node ./tools/lint.js"}, "b": {"lint": "eslint ."}},
    {"": {}, "a": {"lint": "pnpm run lint:missing"}},
    {"": {}},
])
def test_task_runner_leaves_the_read_cannot_follow_hold_the_step(scripts):
    out, withheld, cands = _run({"run": "pnpm turbo run lint"},
                                block=_block(scripts=scripts))
    assert out == [] and withheld == {"delegated_script_unresolved": 1}, withheld
    assert _held(cands, "delegated_script_unresolved")


def test_task_runner_with_an_unreadable_workspace_package_json_is_held_back():
    out, withheld, cands = _run({"run": "pnpm turbo run lint"},
                                block=_block(scripts={"": {}, "a": {"lint": "eslint ."}},
                                             unreadable_pkgs=["b"]))
    assert out == [] and withheld == {"package_json_unreadable": 1}, withheld
    assert _held(cands, "package_json_unreadable")


# ---- pnpm catalogs and npm aliases in the typescript version ---------------------

_CATALOGS = {"default": {"typescript": "^7.0.2", "oxfmt": "0.59.0"},
             "ts6": {"typescript": "^6.0.3"}}


def _cat_block(tv):
    b = _block(tool_versions=tv)
    b["pnpm_catalogs"] = _CATALOGS
    return b


def test_scan_reads_pnpm_workspace_catalogs(tmp_path):
    root = _repo(tmp_path, {"devDependencies": {"typescript": "catalog:"}},
                 {"pnpm-workspace.yaml":
                  "packages:\n  - packages/*\ncatalog:\n  typescript: ^7.0.2\n"
                  "  oxfmt: 0.59.0\ncatalogs:\n  ts6:\n    typescript: ^6.0.3\n"})
    block = scan._read_type_aware_lint(root)
    assert block["pnpm_catalogs"] == _CATALOGS


def test_scan_block_defaults_carry_pnpm_catalogs():
    assert scan._tal_block()["pnpm_catalogs"] == {}


def test_default_catalog_typescript_7_is_already_in_use():
    out, withheld, _c = _run({"run": "npx tsc --noEmit"},
                             block=_cat_block({"": {"typescript": "catalog:"}}))
    assert out == [] and withheld == {"target_already_in_use": 1}, withheld


def test_named_catalog_typescript_6_fires_the_tsc_row():
    out, withheld, _c = _run(
        {"run": "npx tsc --noEmit", "working-directory": "packages/x"},
        block=_cat_block({"": {"typescript": "catalog:"},
                          "packages/x": {"typescript": "catalog:ts6"}}))
    assert len(out) == 1, withheld
    assert _rows(out[0]) == ["tsc"]
    assert _tool(out[0], "tsc")["source_version"] == "^6.0.3"


@pytest.mark.parametrize("spec", ["catalog:missing", "catalog:"])
def test_an_unresolvable_catalog_ref_stays_unreadable(spec):
    b = _block(tool_versions={"": {"typescript": spec}})
    b["pnpm_catalogs"] = {"ts6": {"typescript": "^6.0.3"}}
    out, withheld, _c = _run({"run": "npx tsc --noEmit"}, block=b)
    assert out == [] and withheld == {"typescript_version_unreadable": 1}, withheld


@pytest.mark.parametrize("spec,major", [
    ("npm:@typescript/typescript6@6.0.2", 6), ("npm:typescript@^7.0.2", 7),
    ("npm:typescript", None), ("npm:@typescript/typescript6", None), ("^5.6.2", 5)])
def test_npm_alias_specs_read_the_aliased_version(spec, major):
    assert cr._opt84_major(spec) == major


def test_mastodon_npm_alias_to_typescript6_fires_the_tsc_row():
    out, withheld, _c = _run(
        {"run": "npx tsc --noEmit"},
        block=_block(tool_versions={"": {"typescript":
                                         "npm:@typescript/typescript6@6.0.2"}}))
    assert len(out) == 1 and _rows(out[0]) == ["tsc"], withheld


# ---- action steps: held back only when the action plausibly lints / formats ------

@pytest.mark.parametrize("step", [
    {"uses": "github/codeql-action/analyze@v3", "name": "Perform CodeQL Analysis"},
    {"uses": "actions/upload-artifact@v4", "name": "Upload results"},
    {"uses": "./.github/actions/build", "name": "Build"},
])
def test_an_action_that_cannot_be_a_tool_swap_is_a_verdict_not_listed(step):
    doc = _doc({})
    doc["jobs"]["lint"]["steps"][2] = step | {"name": step.get("name", _STEP)}
    pole = _pole(step=step.get("name", _STEP))
    out, withheld, cands = _run(None, doc=doc, pole=pole)
    assert out == [] and withheld == {"tool_not_in_table": 1}, withheld
    assert cands == []


@pytest.mark.parametrize("step", [
    {"uses": "./.github/actions/lint"},
    {"uses": "./.github/actions/setup", "name": "Run checks"},
    {"uses": "$/.github/actions/format-check"},
    {"uses": "wearerequired/lint-action@v2"},
    {"uses": "creyD/prettier_action@v4"},
    {"uses": "some/action@v1", "name": "Typecheck"},
])
def test_an_action_that_plausibly_lints_formats_or_type_checks_stays_held_back(step):
    name = step.get("name", _STEP)
    doc = _doc({})
    doc["jobs"]["lint"]["steps"][2] = step | {"name": name}
    out, withheld, cands = _run(None, doc=doc, pole=_pole(step=name))
    assert out == [] and withheld == {"candidate_is_an_action": 1}, withheld
    assert cands == [{"workflow_file": _WF, "job": "Lint",
                      "gate": "candidate_is_an_action", "step": name}]


# ---- a directory change inside a resolved script ---------------------------------

_SVELTE = {"": {"check": "cd packages/svelte && pnpm build && cd ../../ && pnpm -r check",
                "lint": "eslint && prettier --check ."},
           "packages/svelte": {"build": "rollup -c", "check": "tsc --noEmit"}}


@pytest.mark.parametrize("run,scripts", [
    ("pnpm check", _SVELTE),
    ("pnpm fmt", {"": {"fmt": "pushd web && prettier --check . && popd"}}),
    ("pnpm lint", {"": {"lint": "pnpm run inner", "inner": "cd web && eslint ."},
                   "web": {}}),
])
def test_a_directory_change_inside_a_resolved_script_is_command_changes_directory(
        run, scripts):
    out, withheld, cands = _run({"run": run}, block=_block(scripts=scripts))
    assert out == [] and withheld == {"command_changes_directory": 1}, withheld
    assert _held(cands, "command_changes_directory")


# ---- the share floor is one constant ---------------------------------------------

def test_the_share_floor_is_decided_by_one_constant(monkeypatch):
    # sveltejs: `lint` is 29s of a 318.5s job (9.1%): below today's 15% floor.
    pole = _pole(step_p50=29.0, job_p50=318.5)
    out, withheld, _c = _run({"run": "npx eslint ."}, pole=pole)
    assert out == [] and withheld == {"no_candidate_step": 1}
    monkeypatch.setattr(cr, "_OPT84_MIN_SHARE", 0.0)
    out, withheld, _c = _run({"run": "npx eslint ."}, pole=pole)
    assert len(out) == 1, withheld

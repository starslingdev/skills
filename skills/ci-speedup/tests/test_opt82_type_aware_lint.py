"""OPT82 — Lint builds the whole type graph.

A lint job that runs ESLint with TYPE-AWARE parsing on builds a TypeScript
program for every file it lints: lint costs roughly what a type-check costs.
The finding names the type-aware rules actually enabled and hands the agent a
benchmark-first, ledger-guarded split. It never carries a saving number and
never recommends turning rules off.

Three halves are tested here:

- the SCAN half (`scan.py::_read_type_aware_lint`) reads the ESLint config(s)
  from the repo tree: whether type-aware parsing is on (a literal, read; an
  unresolvable expression fails closed), and which type-aware rules are on
  (the committed `references/type-aware-lint-rules.tsv` list, plus custom rules
  whose source calls `getParserServices` / `getTypeChecker`);
- the COLLECT half (`collect_runs._detect_opt82_type_aware_lint`) joins that
  fact to a workflow job that runs ESLint and to its measured timings;
- the RENDER / VERIFY half: the card's prompt carries the SIZING ceiling, the
  benchmark, the ledger requirement and the rail, never the verb "disable"
  applied to rules, and `verify_report` fails a finding that carries a number,
  names no rule, or renders without the ledger sentence.

Run: pytest -v skills/ci-speedup/tests/test_opt82_type_aware_lint.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import scan  # noqa: E402

_DATA = _SKILL_DIR / "references" / "type-aware-lint-rules.tsv"


def _vr():
    name = "_vr_for_opt82"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent / "verify_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- repo-tree fixtures -------------------------------------------------------

_FLAT_CONFIG_ON = """\
// Type-aware lint for the whole repo.
import tseslint from 'typescript-eslint';
import local from './eslint-rules/index.mjs';

export default tseslint.config(
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  {
    plugins: { local },
    rules: {
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': ['error', { checksVoidReturn: false }],
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unnecessary-condition': 'off',
      'local/no-unsafe-enum-access': 'error',
      'local/no-todo-comments': 'warn',
    },
  },
);
"""

_RULES_INDEX = """\
import noUnsafeEnumAccess from './no-unsafe-enum-access.mjs';
import noTodoComments from './no-todo-comments.mjs';
export default { rules: { 'no-unsafe-enum-access': noUnsafeEnumAccess,
                          'no-todo-comments': noTodoComments } };
"""

_TYPED_RULE = """\
import { ESLintUtils } from '@typescript-eslint/utils';
export default {
  meta: { type: 'problem', schema: [] },
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    const checker = services.program.getTypeChecker();
    return { MemberExpression(node) { void checker; void node; } };
  },
};
"""

_UNTYPED_RULE = """\
export default {
  meta: { type: 'suggestion', schema: [] },
  create(context) { return { Program() { void context; } }; },
};
"""

_PACKAGE_JSON = json.dumps({
    "name": "fixture",
    "private": True,
    "scripts": {
        "lint": "npm run lint:eslint",
        "lint:eslint": "eslint . --cache --max-warnings 0",
        "test": "vitest run",
    },
}, indent=2)


def _tree(tmp_path: Path, config: str = _FLAT_CONFIG_ON,
          name: str = "eslint.config.mjs") -> Path:
    root = tmp_path / "repo"
    (root / "eslint-rules").mkdir(parents=True)
    (root / name).write_text(config, encoding="utf-8")
    (root / "eslint-rules" / "index.mjs").write_text(_RULES_INDEX, encoding="utf-8")
    (root / "eslint-rules" / "no-unsafe-enum-access.mjs").write_text(
        _TYPED_RULE, encoding="utf-8")
    (root / "eslint-rules" / "no-todo-comments.mjs").write_text(
        _UNTYPED_RULE, encoding="utf-8")
    (root / "package.json").write_text(_PACKAGE_JSON, encoding="utf-8")
    return root


# --- the committed data file --------------------------------------------------

def _data_rows() -> list[list[str]]:
    rows = []
    for line in _DATA.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        rows.append(line.split("\t"))
    return rows


def test_data_file_is_sorted_and_deduplicated():
    names = [r[0] for r in _data_rows()]
    assert names, "the type-aware rule list is empty"
    assert names == sorted(names), "type-aware-lint-rules.tsv must stay sorted"
    assert len(names) == len(set(names)), "type-aware-lint-rules.tsv has duplicates"


def test_data_file_rows_are_well_formed_and_cite_their_source():
    text = _DATA.read_text(encoding="utf-8")
    header = "\n".join(l for l in text.splitlines() if l.startswith("#"))
    assert "typescript-eslint" in header and "2026-10-07" in header
    assert "requiresTypeChecking" in header
    for row in _data_rows():
        assert len(row) == 3, row
        rule, base, presets = row
        assert rule.startswith("@typescript-eslint/"), rule
        assert base == "-" or re.fullmatch(r"[a-z-]+", base), row
        assert presets == "-" or set(presets.split(",")) <= {
            "recommended", "strict", "stylistic"}, row
    # the loader reads exactly what the file holds
    loaded = scan._load_type_aware_rules()
    assert set(loaded) == {r[0] for r in _data_rows()}
    assert loaded["@typescript-eslint/require-await"]["syntax_only_equivalent"] == "require-await"
    assert loaded["@typescript-eslint/no-floating-promises"]["syntax_only_equivalent"] is None


# --- the scan half ------------------------------------------------------------

def test_scan_reads_type_aware_on_and_enumerates_rules(tmp_path):
    block = scan._read_type_aware_lint(_tree(tmp_path))
    assert block["error"] is None
    cfg = next(c for c in block["configs"] if c["path"] == "eslint.config.mjs")
    assert cfg["type_aware"] == "on"
    assert any("projectService: true" in e for e in cfg["type_aware_evidence"])
    # explicit-off and non-type-aware rules are never enumerated
    assert cfg["rules"] == ["@typescript-eslint/no-floating-promises",
                            "@typescript-eslint/no-misused-promises",
                            "local/no-unsafe-enum-access"]
    assert [c["rule"] for c in cfg["custom_rules"]] == ["local/no-unsafe-enum-access"]
    assert block["package_scripts"][""]["lint:eslint"].startswith("eslint .")


def test_scan_reads_a_type_checked_preset_as_on_and_expands_it(tmp_path):
    cfg_text = ("import tseslint from 'typescript-eslint';\n"
                "export default tseslint.config(...tseslint.configs.recommendedTypeChecked,\n"
                "  { rules: { '@typescript-eslint/require-await': 'off' } });\n")
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))
    cfg = block["configs"][0]
    assert cfg["type_aware"] == "on"
    assert "@typescript-eslint/no-floating-promises" in cfg["rules"]
    assert "@typescript-eslint/require-await" not in cfg["rules"]   # explicitly off


def test_disable_type_checked_preset_never_reads_as_on(tmp_path):
    cfg_text = ("import tseslint from 'typescript-eslint';\n"
                "export default [tseslint.configs.disableTypeChecked,\n"
                "  { rules: { 'no-console': 'warn' } }];\n")
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))
    assert block["configs"][0]["type_aware"] == "off"


def test_unresolvable_project_expression_fails_closed(tmp_path):
    cfg_text = _FLAT_CONFIG_ON.replace("projectService: true",
                                       "projectService: process.env.CI === 'true'")
    cfg = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))["configs"][0]
    assert cfg["type_aware"] == "unresolved"
    assert any("process.env.CI" in u for u in cfg["unresolved"])


def test_type_aware_mentions_inside_comments_are_ignored(tmp_path):
    cfg_text = ("// parserOptions: { project: true }\n"
                "/* '@typescript-eslint/no-floating-promises': 'error' */\n"
                "export default [{ rules: { 'no-console': 'warn' } }];\n")
    cfg = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))["configs"][0]
    assert cfg["type_aware"] == "off"
    assert cfg["rules"] == []


def test_eslintrc_json_with_project_reads_on(tmp_path):
    rc = json.dumps({
        "parser": "@typescript-eslint/parser",
        "parserOptions": {"project": ["./tsconfig.json"]},
        "extends": ["plugin:@typescript-eslint/recommended-requiring-type-checking"],
        "rules": {"@typescript-eslint/no-floating-promises": "off",
                  "@typescript-eslint/strict-boolean-expressions": ["error"]},
    })
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / ".eslintrc.json").write_text(rc, encoding="utf-8")
    cfg = scan._read_type_aware_lint(root)["configs"][0]
    assert cfg["path"] == ".eslintrc.json" and cfg["type_aware"] == "on"
    assert "@typescript-eslint/strict-boolean-expressions" in cfg["rules"]
    assert "@typescript-eslint/await-thenable" in cfg["rules"]       # via the preset
    assert "@typescript-eslint/no-floating-promises" not in cfg["rules"]


def test_reader_crash_is_a_fail_closed_block_never_a_dead_scan(tmp_path, monkeypatch, capsys):
    """CRASH TRIPWIRE (scan half): an exception inside the reader yields a block
    the consumer withholds on, and says so on stderr — never a crashed scan and
    never a block that reads as "type-aware is off"."""
    def boom(_root):
        raise RuntimeError("synthetic reader failure")
    monkeypatch.setattr(scan, "_read_type_aware_lint_unguarded", boom)
    block = scan._read_type_aware_lint(_tree(tmp_path))
    assert block["error"] == "RuntimeError"
    assert block["configs"] == [] and block["truncated"] is True
    assert "OPT82" in capsys.readouterr().err


# --- the collect half ----------------------------------------------------------

_WF = {
    "name": "Lint",
    "on": {"push": None},
    "jobs": {
        "eslint": {
            "runs-on": "ubuntu-latest",
            "steps": [
                {"uses": "actions/checkout@v4"},
                {"run": "npm ci"},
                {"name": "Lint", "run": "npm run lint"},
            ],
        },
        "unit": {
            "runs-on": "ubuntu-latest",
            "steps": [{"uses": "actions/checkout@v4"}, {"run": "npm test"}],
        },
    },
}


def _job(name: str, jid: int, start: str, dur_s: int, steps: list[tuple[str, int]]):
    import datetime as dt
    t0 = dt.datetime.fromisoformat(start)
    out_steps, t = [], t0
    for i, (sname, sdur) in enumerate(steps, 1):
        out_steps.append({"name": sname, "number": i, "status": "completed",
                          "conclusion": "success",
                          "started_at": t.isoformat().replace("+00:00", "Z"),
                          "completed_at": (t + dt.timedelta(seconds=sdur))
                          .isoformat().replace("+00:00", "Z")})
        t += dt.timedelta(seconds=sdur)
    return {"id": jid, "name": name, "status": "completed", "conclusion": "success",
            "labels": ["ubuntu-latest"],
            "started_at": t0.isoformat().replace("+00:00", "Z"),
            "completed_at": (t0 + dt.timedelta(seconds=dur_s)).isoformat()
            .replace("+00:00", "Z"),
            "html_url": f"https://github.com/o/r/actions/runs/1/job/{jid}",
            "steps": out_steps}


def _runs(lint_s: int, unit_s: int, n: int = 3):
    runs = []
    for i in range(n):
        start = f"2026-10-0{i + 1}T10:00:00+00:00"
        runs.append([
            _job("eslint", 100 + i, start, lint_s,
                 [("Set up job", 2), ("Run actions/checkout@v4", 3),
                  ("Run npm ci", 10), ("Lint", lint_s - 17), ("Complete job", 2)]),
            _job("unit", 200 + i, start, unit_s,
                 [("Set up job", 2), ("Run npm test", unit_s - 2)]),
        ])
    return runs


def _detect(block, lint_s=95, unit_s=20, wf=None):
    runs = _runs(lint_s, unit_s)
    crit = cr._critical_path(runs)
    withheld: dict[str, int] = {}
    cands: list[dict] = []
    out = cr._detect_opt82_type_aware_lint(
        ".github/workflows/lint.yml", runs, crit, wf or _WF, block, 0,
        withheld=withheld, withheld_candidates=cands)
    return out, withheld, cands


def test_detector_fires_on_a_slow_type_aware_lint_job_and_carries_no_number(tmp_path):
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, cands = _detect(block, lint_s=95, unit_s=20)
    assert len(out) == 1, withheld
    f = out[0]
    assert f["pattern"] == "OPT82" and f["affected_jobs"] == ["eslint"]
    assert not (f.get("wall_clock_p50_s") or 0) > 0
    assert not f.get("runner_min_saving")
    assert f["sizing_basis"] == "uncredited"
    tal = f["type_aware_lint"]
    assert tal["kind"] == "opt82_type_aware_lint"
    assert [r["rule"] for r in tal["rules"]] == ["@typescript-eslint/no-floating-promises",
                                                 "@typescript-eslint/no-misused-promises",
                                                 "local/no-unsafe-enum-access"]
    # resolved through `npm run lint` -> `npm run lint:eslint` -> eslint
    assert tal["lint_command"].startswith("eslint .")
    assert tal["ceiling_basis"] == "lint_step" and tal["ceiling_s"] == 78.0
    assert tal["min_lint_p50_s"] == cr._OPT82_MIN_LINT_P50_S == 60.0
    # the benchmark: same lint, cold, type-aware parsing unset, enumerated rules off
    fast = tal["benchmark_commands"]["without_type_information"]
    assert "--cache" not in fast and "projectService:false" in fast
    assert "--rule '@typescript-eslint/no-floating-promises: off'" in fast
    assert "OPT82" not in cr._SIZING
    assert cands == []


def test_below_threshold_off_path_lint_is_a_verdict_not_a_hold(tmp_path):
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, cands = _detect(block, lint_s=40, unit_s=50)
    assert out == [] and withheld.get("lint_job_below_cost_threshold") == 1
    assert cands == []


def test_a_fast_lint_job_on_the_critical_path_still_fires(tmp_path):
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, _w, _c = _detect(block, lint_s=45, unit_s=20)     # under 60s, but the long pole
    assert len(out) == 1 and out[0]["type_aware_lint"]["on_critical_path"] is True


def test_type_aware_off_withholds_as_a_verdict(tmp_path):
    cfg_text = "export default [{ rules: { 'no-console': 'warn' } }];\n"
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))
    out, withheld, cands = _detect(block)
    assert out == [] and withheld.get("type_aware_parsing_off") == 1 and cands == []


def test_unresolvable_setting_is_held_back_and_named(tmp_path):
    cfg_text = _FLAT_CONFIG_ON.replace("projectService: true", "projectService: isCI")
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))
    out, withheld, cands = _detect(block)
    assert out == []
    assert withheld.get("type_aware_setting_unresolvable") == 1
    assert cands == [{"workflow_file": ".github/workflows/lint.yml", "job": "eslint",
                      "gate": "type_aware_setting_unresolvable"}]


def test_no_enumerable_rule_is_held_back(tmp_path):
    cfg_text = ("export default [{ languageOptions: { parserOptions: "
                "{ project: './tsconfig.json' } }, rules: { 'no-console': 'warn' } }];\n")
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))
    out, withheld, cands = _detect(block)
    assert out == [] and withheld.get("no_enumerable_type_aware_rule") == 1
    assert cands and cands[0]["gate"] == "no_enumerable_type_aware_rule"


def test_unresolvable_lint_script_is_held_back(tmp_path):
    wf = json.loads(json.dumps(_WF))
    wf["jobs"]["eslint"]["steps"][2]["run"] = "npm run lint:ci"   # not declared
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, cands = _detect(block, wf=wf)
    assert out == [] and withheld.get("lint_script_unresolvable") == 1
    assert cands[0]["gate"] == "lint_script_unresolvable"


def test_reader_failure_withholds_every_lint_job(tmp_path):
    block = scan._tal_block(error="RuntimeError")
    out, withheld, cands = _detect(block)
    assert out == [] and withheld.get("type_aware_config_reader_failed") == 1
    assert cands[0]["gate"] == "type_aware_config_reader_failed"


def test_every_held_back_gate_has_a_phrase_and_no_verdict_does():
    phrases = bp._OPT82_WITHHOLD_PHRASES
    assert set(phrases) == set(cr._OPT82_HELD_BACK_GATES)
    assert not set(phrases) & set(cr._OPT82_VERDICT_GATES)
    assert _vr()._VR_OPT82_WITHHOLD_PHRASES == phrases
    for p in phrases.values():
        assert p and "_" not in p and "$" not in p, p
    assert (bp._OPT82_WITHHELD_DOC_KEY == cr._OPT82_WITHHELD_DOC_KEY
            == _vr()._VR_OPT82_WITHHELD_DOC_KEY == "opt82_withheld_candidates")


# --- render + verify -----------------------------------------------------------

_DISABLE_RULE_RE = re.compile(
    r"\bdisabl\w*\b[^.\n]{0,60}\brules?\b|\brules?\b[^.\n]{0,60}\bdisabl\w*", re.I)


def _rendered_card(tmp_path):
    block = scan._read_type_aware_lint(_tree(tmp_path))
    f = _detect(block)[0][0]
    lines, n, _on = bp._also_noticed_block([f], "https://example.invalid/cat.md")
    return f, "\n".join(lines)


def test_rendered_card_carries_sizing_benchmark_ledger_and_rail(tmp_path):
    f, card = _rendered_card(tmp_path)
    assert "OPT82 - Lint Builds the Whole Type Graph" in card
    assert bp._OPT82_LEDGER_SENTENCE in card
    assert "SIZING:" in card and "CEILING" in card
    assert "no saving is credited" in card
    assert "projectService:false" in card                  # the benchmark is in the prompt
    assert "@typescript-eslint/no-floating-promises" in card
    assert "local/no-unsafe-enum-access" in card
    assert card.count("does NOT prescribe the fix") == 1
    assert card.count("NEVER BUY SPEED BY CHECKING LESS") == 1
    # the generic off-path bill line must not describe a numberless lint finding
    assert "off the merge-gating critical path" not in card
    assert "runner-min/mo" not in card


def test_rendered_prompt_never_says_disable_about_rules(tmp_path):
    _f, card = _rendered_card(tmp_path)
    m = _DISABLE_RULE_RE.search(card)
    assert m is None, m.group(0)
    # and the guard itself is live
    assert _DISABLE_RULE_RE.search("disable the type-aware rules")


def test_card_renders_even_when_every_job_is_a_drilled_pole(tmp_path):
    """A numberless finding on a job that IS a drilled pole would be dropped by
    the valueless-pole exclusion; OPT82 is numberless by design and must not be."""
    block = scan._read_type_aware_lint(_tree(tmp_path))
    f = _detect(block)[0][0]
    lines, n, _ = bp._also_noticed_block(
        [f], "u", pole_jobs={("lint.yml", "eslint")})
    assert n == 1 and bp._OPT82_LEDGER_SENTENCE in "\n".join(lines)


def _vr_check(tmp_path, f, card):
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({"findings": [f]}), encoding="utf-8")
    return _vr().check_opt82_type_aware_lint_uncredited(card, p)


def test_verifier_passes_a_well_formed_card(tmp_path):
    f, card = _rendered_card(tmp_path)
    c = _vr_check(tmp_path, f, card)
    assert c.ok and not c.skipped, c.detail


@pytest.mark.parametrize("mutate,needle", [
    (lambda f: f.__setitem__("wall_clock_p50_s", 42.0), "wall_clock_p50_s"),
    (lambda f: f.__setitem__("runner_min_saving", 12.5), "runner_min_saving"),
    (lambda f: f["type_aware_lint"].__setitem__("rules", []), "rule"),
])
def test_verifier_fails_a_finding_that_carries_a_number_or_no_rule(tmp_path, mutate, needle):
    f, card = _rendered_card(tmp_path)
    mutate(f)
    c = _vr_check(tmp_path, f, card)
    assert not c.ok and needle in c.detail, c.detail


def test_verifier_fails_a_card_without_the_ledger_sentence(tmp_path):
    f, card = _rendered_card(tmp_path)
    c = _vr_check(tmp_path, f, card.replace(bp._OPT82_LEDGER_SENTENCE, ""))
    assert not c.ok and "ledger" in c.detail.lower(), c.detail
    assert _vr()._VR_OPT82_LEDGER_SENTENCE == bp._OPT82_LEDGER_SENTENCE


def test_verifier_fails_a_card_that_says_disable_about_rules(tmp_path):
    f, card = _rendered_card(tmp_path)
    c = _vr_check(tmp_path, f, card.replace(
        "</details>", "Then disable the type-aware rules.\n</details>", 1))
    assert not c.ok and "disable" in c.detail.lower(), c.detail


# The collect-half crash tripwire runs through the real collector:
# test_offline_pipeline_e2e.py::test_opt82_detector_crash_skips_and_discloses_through_collect


def test_the_two_readers_of_the_rule_list_agree():
    """scan.py and collect_runs.py each read the committed list (collect_runs
    must not import scan); pinned equal so neither can drift."""
    assert cr._opt82_rules_catalog() == scan._load_type_aware_rules()

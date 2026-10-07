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


@pytest.mark.parametrize("lint_s,fires", [(60, True), (59, False)])
def test_the_cost_bar_is_inclusive_at_sixty_seconds(tmp_path, lint_s, fires):
    """Off the long pole (the unit job is slower), so only the cost bar decides:
    a lint job at exactly `_OPT82_MIN_LINT_P50_S` fires, one second under does not."""
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, _c = _detect(block, lint_s=lint_s, unit_s=200)
    assert bool(out) is fires, (out, withheld)
    if not fires:
        assert withheld.get("lint_job_below_cost_threshold") == 1


# --- per-job cards, cap exemption, ceiling basis, verifier coverage -------------

def _second_lint_finding(f):
    """A second, DISTINCT OPT82 lever: another workflow's lint job, its own
    config, its own rules and its own benchmark (run from its own directory)."""
    import copy
    g = copy.deepcopy(f)
    g["id"] = "f2"
    g["workflow_file"] = ".github/workflows/web-lint.yml"
    g["affected_jobs"] = ["web-eslint"]
    g["evidence"] = g["measured_signal"] = (
        "`web-eslint` runs ESLint with type-aware parsing on; 1 type-aware rule(s) "
        "are on: `@typescript-eslint/no-unsafe-assignment`.")
    tal = g["type_aware_lint"]
    tal["job"] = "web-eslint"
    tal["working_directory"] = "apps/web"
    tal["configs"] = ["apps/web/eslint.config.mjs"]
    tal["rules"] = [{"rule": "@typescript-eslint/no-unsafe-assignment",
                     "source": "typescript-eslint",
                     "config": "apps/web/eslint.config.mjs"}]
    tal["ceiling_s"] = 140.0
    tal["benchmark_commands"] = {
        "as_ci_runs_it": "(cd apps/web && time npx eslint src)",
        "without_type_information": "(cd apps/web && time npx eslint src "
                                    "--parser-options project:false "
                                    "--parser-options projectService:false "
                                    "--rule '@typescript-eslint/no-unsafe-assignment: off')"}
    return g


def _opt82_cards(text):
    return _vr()._VR_OPT82_CARD_RE.findall(text)


def test_two_distinct_lint_jobs_each_get_their_own_card(tmp_path):
    """Folded by pattern id, the second lint job's rules, config and benchmark
    never reached the prompt (it read `members[0]` only). Each lint job is its
    own lever, so each gets its own card, like OPT73/OPT77/OPT79."""
    f, _card = _rendered_card(tmp_path)
    g = _second_lint_finding(f)
    lines, n, _ = bp._also_noticed_block([f, g], "u")
    cards = _opt82_cards("\n".join(lines))
    assert n == 2 and len(cards) == 2, cards
    first = next(c for c in cards if "(eslint)" in c)
    second = next(c for c in cards if "(web-eslint)" in c)
    assert "  - @typescript-eslint/no-floating-promises" in first
    assert "  - @typescript-eslint/no-unsafe-assignment" in second
    assert "  - @typescript-eslint/no-unsafe-assignment" not in first
    assert "Type-aware rules apps/web/eslint.config.mjs turns on" in second
    assert "(cd apps/web && time npx eslint src)" in second
    assert "SIZING: uncredited. The lint step measured 140s" in second
    assert "cd apps/web" not in first


def _billed_hygiene(n):
    return [{"id": f"h{i}", "pattern": f"OPT{i + 1}", "title": f"Hygiene {i}",
             "severity": "LOW", "workflow_file": ".github/workflows/ci.yml",
             "line": 10 + i, "affected_jobs": [f"job{i}"], "evidence": "measured",
             "runner_min_saving": 100.0 - i, "wall_clock_p50_s": 0.0}
            for i in range(n)]


def test_opt82_is_never_cut_by_the_also_noticed_cap(tmp_path):
    """OPT82 has no bill saving, so it ranks last; past `_ALSO_NOTICED_CAP` it
    fell into the "+N more" tail, the one place its rules and ledger reach the
    reader gone. It always renders."""
    f, _card = _rendered_card(tmp_path)
    many = _billed_hygiene(bp._ALSO_NOTICED_CAP + 1)
    lines, _n, _ = bp._also_noticed_block(many + [f], "u")
    text = "\n".join(lines)
    assert len(_opt82_cards(text)) == 1, text[-800:]
    assert bp._OPT82_LEDGER_SENTENCE in text
    # the billed tail is still capped and disclosed
    assert "more hygiene pattern(s)" in text


def test_job_basis_ceiling_is_never_called_the_lint_step(tmp_path):
    """When the lint step was not measured, the ceiling is the JOB's p50 and
    every surface says so."""
    block = scan._read_type_aware_lint(_tree(tmp_path))
    runs = _runs(95, 20)
    for run_jobs in runs:
        for j in run_jobs:
            for st in j["steps"]:
                if st["name"] == "Lint":
                    st["name"] = "Lint (renamed in the API)"
    out = cr._detect_opt82_type_aware_lint(
        ".github/workflows/lint.yml", runs, cr._critical_path(runs), _WF, block, 0,
        withheld={}, withheld_candidates=[])
    assert len(out) == 1
    f = out[0]
    assert f["type_aware_lint"]["ceiling_basis"] == "lint_job"
    assert "lint step's measured p50" not in f["size_note"], f["size_note"]
    assert "lint job's measured p50 (95s)" in f["size_note"], f["size_note"]
    lines, _n, _ = bp._also_noticed_block([f], "u")
    card = "\n".join(lines)
    assert "The whole lint job (its lint step was not separately measured)" in card
    assert "lint step measured" not in card


def test_verifier_fails_a_finding_with_no_card(tmp_path):
    f, _card = _rendered_card(tmp_path)
    c = _vr_check(tmp_path, f, "")
    assert not c.ok and "no card" in c.detail, c.detail


def test_verifier_fails_a_card_missing_one_of_its_findings_rules(tmp_path):
    f, card = _rendered_card(tmp_path)
    c = _vr_check(tmp_path, f, card.replace("local/no-unsafe-enum-access", "local/x"))
    assert not c.ok and "local/no-unsafe-enum-access" in c.detail, c.detail


def test_verifier_fails_a_second_finding_folded_into_the_first_card(tmp_path):
    f, card = _rendered_card(tmp_path)
    g = _second_lint_finding(f)
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({"findings": [f, g]}), encoding="utf-8")
    c = _vr().check_opt82_type_aware_lint_uncredited(card, p)
    assert not c.ok and "f2" in c.detail, c.detail


def test_verifier_passes_two_cards_for_two_lint_jobs(tmp_path):
    f, _card = _rendered_card(tmp_path)
    g = _second_lint_finding(f)
    lines, _n, _ = bp._also_noticed_block([f, g], "u")
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({"findings": [f, g]}), encoding="utf-8")
    c = _vr().check_opt82_type_aware_lint_uncredited("\n".join(lines), p)
    assert c.ok and not c.skipped, c.detail


def test_pole_verifier_accepts_an_opt82_card_on_a_drilled_pole(tmp_path):
    """The engine keeps OPT82 on a drilled-pole lint job (numberless by design,
    not valueless); the verifier's pole double-frame check must mirror that."""
    block = scan._read_type_aware_lint(_tree(tmp_path))
    f = _detect(block)[0][0]
    lines, n, _ = bp._also_noticed_block([f], "u", pole_jobs={("lint.yml", "eslint")})
    assert n == 1
    report = ("## 🐢 Long pole 1: `lint.yml` ▸ `eslint` - 1m 35s\n\nthe pole body\n\n"
              + "\n".join(lines) + "\n")
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({"findings": [f]}), encoding="utf-8")
    c = _vr().check_pole_not_reframed_as_hygiene(report, p)
    assert c.ok and not c.skipped, c.detail
    # and the check is live on this report shape: a valueless non-OPT82 twin fails
    twin = dict(f, pattern="OPT24", id="t1")
    p.write_text(json.dumps({"findings": [twin]}), encoding="utf-8")
    assert not _vr().check_pole_not_reframed_as_hygiene(
        report.replace("OPT82 - ", "OPT24 - "), p).ok


@pytest.mark.parametrize("edit", [
    lambda c: c.replace("**Where:**", "**Saving:** ~72s wall-clock per run, "
                        "120 runner-min/mo\n**Where:**", 1),
    lambda c: c.replace("uncredited, benchmark first", "saves ~72s", 1),
    lambda c: c.replace("Fix order:", "Splitting lint saves ~1m 10s per run.\n"
                        "Fix order:", 1),
    lambda c: c.replace("Fix order:", "Expected: 120 runner-min/mo back.\n"
                        "Fix order:", 1),
])
def test_verifier_fails_a_card_that_claims_a_saving(tmp_path, edit):
    """OPT82 is uncredited: the findings fields carry no number, and the card
    must not either. Its only figures are the labelled SIZING ceiling, the
    evidence's measured p50s and the benchmark commands."""
    f, card = _rendered_card(tmp_path)
    edited = edit(card)
    assert edited != card
    c = _vr_check(tmp_path, f, edited)
    assert not c.ok and "saving" in c.detail.lower(), c.detail


# --- the hand-off text: attribution, ledger, PR-pass scoping, disable rail -----

def _card_for(f):
    lines, _n, _on = bp._also_noticed_block([f], "https://example.invalid/cat.md")
    return "\n".join(lines)


def _flat(card):
    return " ".join(card.split())


def test_linear_result_is_attributed_to_the_rewrite_not_the_split(tmp_path):
    """Linear's post credits the drop to rewriting their custom type-aware
    rules over the syntax tree, which let ESLint drop TypeScript: API lint
    -68%, full-repository lint -55%. It is not a split result."""
    _f, card = _rendered_card(tmp_path)
    flat = _flat(card)
    assert "after splitting" not in flat
    assert "rewrote their custom type-aware rules" in flat, flat
    assert "API lint time 68%" in flat and "full-repository lint time 55%" in flat
    assert "their result, not a forecast" in flat


def test_ledger_allows_a_reviewed_replacement_row_never_a_missing_row():
    s = bp._OPT82_LEDGER_SENTENCE
    assert "REPLACED BY <rule>" in s
    assert "no longer checks" in s and "human" in s
    assert "has a REPLACED row" in s and "A rule with no row" in s
    # the old by-construction-false union claim is gone
    assert "equals the original set" not in s
    assert _vr()._VR_OPT82_LEDGER_SENTENCE == s


def test_collector_names_the_workflows_that_declare_a_merge_queue():
    docs = {
        ".github/workflows/lint.yml": {"on": {"push": None}},
        ".github/workflows/queue.yml": {True: ["pull_request", "merge_group"]},
        ".github/workflows/ci.yml": {"on": {"merge_group": {"types": ["checks_requested"]}}},
        ".github/workflows/odd.yml": "not a mapping",
    }
    assert cr._opt82_merge_group_workflows(docs) == [
        ".github/workflows/ci.yml", ".github/workflows/queue.yml"]
    assert cr._opt82_merge_group_workflows({}) == []


def test_with_a_merge_queue_the_pr_pass_lists_changed_files_never_cache(tmp_path):
    f, _ = _rendered_card(tmp_path)
    f["type_aware_lint"]["merge_group_workflows"] = [".github/workflows/queue.yml"]
    flat = _flat(_card_for(f))
    assert "git diff --name-only" in flat, flat
    assert ".github/workflows/queue.yml" in flat
    assert "Do NOT use ESLint's `--cache` for the type-aware pass" in flat
    assert "cross-file type dependencies" in flat
    assert "(`--cache`, catalog OPT9)" not in flat


@pytest.mark.parametrize("mq", [None, []])
def test_without_a_merge_queue_the_full_type_aware_pass_stays_required(tmp_path, mq):
    f, _ = _rendered_card(tmp_path)
    if mq is None:
        f["type_aware_lint"].pop("merge_group_workflows", None)
    else:
        f["type_aware_lint"]["merge_group_workflows"] = mq
    flat = _flat(_card_for(f))
    assert "no merge queue" in flat, flat
    assert "REQUIRED pull-request check over the whole tree" in flat
    assert "never scoped to changed files" in flat
    assert "git diff --name-only" not in flat
    assert "(`--cache`, catalog OPT9)" not in flat


_SYNONYMS = [
    "Then disable the type-aware rules.",
    "Then turn the type-aware rules off.",
    "Then turn off the\ntype-aware rules.",                  # across a line wrap
    "Drop the slow rules from the config.",
    "Remove the type-aware rules.",
    "Set those rules to 'off' in the config.",
    "Set `'@typescript-eslint/no-floating-promises': 'off'` in the config.",
    "Extend tseslint.configs.disableTypeChecked for every file.",
    "Move the type-aware rules to a non-blocking job.",
]


@pytest.mark.parametrize("text", _SYNONYMS)
def test_verifier_fails_every_way_of_switching_rules_off(tmp_path, text):
    f, card = _rendered_card(tmp_path)
    inside = card.replace("Do: run the benchmark first",
                          text + "\nDo: run the benchmark first", 1)
    assert inside != card
    c = _vr_check(tmp_path, f, inside)
    assert not c.ok and "never" in c.detail.lower(), (text, c.detail)


def test_verifier_does_not_fail_on_a_lint_command_in_the_evidence(tmp_path):
    """The evidence quotes the CI's own lint command; a flag such as
    `--report-unused-disable-directives` or a `--rule '...: off'` there is data,
    not an instruction, and must not fail the card."""
    f, card = _rendered_card(tmp_path)
    cmd = "eslint . --report-unused-disable-directives --rule 'no-console: off'"
    noisy = card.replace("`eslint . --cache --max-warnings 0`", f"`{cmd}`")
    assert noisy.count(cmd) == 2, noisy
    c = _vr_check(tmp_path, f, noisy)
    assert c.ok, c.detail


def test_the_rendered_card_passes_the_widened_rail(tmp_path):
    """The labelled benchmark-only run (its `--rule '...: off'` command and the
    sentence that explains it) stays legal under the widened rail, in both the
    merge-queue and the no-merge-queue wording."""
    f, card = _rendered_card(tmp_path)
    assert _vr_check(tmp_path, f, card).ok
    f["type_aware_lint"]["merge_group_workflows"] = [".github/workflows/queue.yml"]
    assert _vr_check(tmp_path, f, _card_for(f)).ok


# --- reader forms: comments, config formats, preset and option spellings ------

def _eslintrc_tree(tmp_path: Path, name: str, text: str) -> Path:
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / name).write_text(text, encoding="utf-8")
    return root


def test_a_block_commented_project_option_reads_off(tmp_path):
    cfg_text = ("/*\n"
                "export default [{ languageOptions: { parserOptions: { project: true } } }];\n"
                "*/\n"
                "export default [{ rules: { 'no-console': 'warn' } }];\n")
    cfg = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))["configs"][0]
    assert cfg["type_aware"] == "off", cfg["type_aware_evidence"]
    assert cfg["type_aware_evidence"] == []


def test_a_url_inside_a_string_does_not_comment_out_the_rest_of_the_line(tmp_path):
    cfg_text = ("const docs = 'https://typescript-eslint.io/getting-started'; "
                "export default [{ languageOptions: { parserOptions: "
                "{ projectService: true } }, rules: "
                "{ '@typescript-eslint/no-floating-promises': 'error' } }];\n")
    cfg = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))["configs"][0]
    assert cfg["type_aware"] == "on"
    assert cfg["rules"] == ["@typescript-eslint/no-floating-promises"]


def test_eslintrc_overrides_enabling_type_aware_rules_read_on(tmp_path):
    rc = json.dumps({
        "root": True,
        "rules": {"no-console": "warn"},
        "overrides": [{
            "files": ["*.ts"],
            "parser": "@typescript-eslint/parser",
            "parserOptions": {"project": "./tsconfig.json"},
            "rules": {"@typescript-eslint/no-floating-promises": "error"},
        }],
    })
    cfg = scan._read_type_aware_lint(_eslintrc_tree(tmp_path, ".eslintrc.json", rc))["configs"][0]
    assert cfg["type_aware"] == "on"
    assert cfg["rules"] == ["@typescript-eslint/no-floating-promises"]


def test_package_json_eslint_config_is_read(tmp_path):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    pkg = json.loads(_PACKAGE_JSON)
    pkg["eslintConfig"] = {
        "parserOptions": {"project": "./tsconfig.json"},
        "rules": {"@typescript-eslint/no-misused-promises": "error"},
    }
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    block = scan._read_type_aware_lint(root)
    cfg = next(c for c in block["configs"] if c["path"] == "package.json")
    assert cfg["format"] == "package.json" and cfg["type_aware"] == "on"
    assert cfg["rules"] == ["@typescript-eslint/no-misused-promises"]


def test_yaml_eslintrc_is_read(tmp_path):
    rc = ("parser: '@typescript-eslint/parser'\n"
          "parserOptions:\n"
          "  project: true\n"
          "rules:\n"
          "  '@typescript-eslint/no-floating-promises': error\n")
    block = scan._read_type_aware_lint(_eslintrc_tree(tmp_path, ".eslintrc.yml", rc))
    assert block["unreadable"] == []
    cfg = block["configs"][0]
    assert cfg["path"] == ".eslintrc.yml" and cfg["type_aware"] == "on"
    assert cfg["rules"] == ["@typescript-eslint/no-floating-promises"]


def test_kebab_type_checked_preset_reads_on_and_expands(tmp_path):
    rc = json.dumps({
        "parser": "@typescript-eslint/parser",
        "extends": ["plugin:@typescript-eslint/recommended-type-checked"],
    })
    cfg = scan._read_type_aware_lint(_eslintrc_tree(tmp_path, ".eslintrc.json", rc))["configs"][0]
    assert cfg["type_aware"] == "on" and cfg["presets"] == ["recommended"]
    assert "@typescript-eslint/no-floating-promises" in cfg["rules"]


@pytest.mark.parametrize("name,text", [
    ("eslint.config.mjs", "import tseslint, { configs } from 'typescript-eslint';\n"
                          "export default tseslint.config(configs.all);\n"),
    (".eslintrc.json", json.dumps({"extends": ["plugin:@typescript-eslint/all"]})),
])
def test_the_all_preset_reads_on_and_expands_to_every_rule(tmp_path, name, text):
    root = _eslintrc_tree(tmp_path, name, text) if name != "eslint.config.mjs" \
        else _tree(tmp_path, text)
    cfg = scan._read_type_aware_lint(root)["configs"][0]
    assert cfg["type_aware"] == "on" and cfg["presets"] == ["all"]
    assert cfg["rules"] == sorted(scan._load_type_aware_rules())


@pytest.mark.parametrize("option", [
    "projectService: { allowDefaultProject: ['*.js'] }",
    "project: ['./tsconfig.json']",
])
def test_object_project_service_and_array_project_read_on(tmp_path, option):
    cfg_text = ("export default [{ languageOptions: { parserOptions: { " + option
                + " } }, rules: { '@typescript-eslint/no-floating-promises': 'error' } }];\n")
    cfg = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))["configs"][0]
    assert cfg["type_aware"] == "on", cfg
    assert cfg["unresolved"] == []


def test_project_false_reads_off(tmp_path):
    cfg_text = ("export default [{ languageOptions: { parserOptions: { project: false } },"
                " rules: { '@typescript-eslint/no-floating-promises': 'error' } }];\n")
    cfg = scan._read_type_aware_lint(_tree(tmp_path, cfg_text))["configs"][0]
    assert cfg["type_aware"] == "off"
    assert cfg["type_aware_evidence"] == [] and cfg["unresolved"] == []


@pytest.mark.parametrize("call", [
    "ESLintUtils.getParserServices(context)",
    "context.parserServices.program.getTypeChecker()",
])
def test_each_type_information_entry_point_alone_marks_a_custom_rule_typed(tmp_path, call):
    root = _tree(tmp_path)
    (root / "eslint-rules" / "no-unsafe-enum-access.mjs").write_text(
        "export default { meta: { type: 'problem', schema: [] },\n"
        f"  create(context) {{ const x = {call}; return {{ Program() {{ void x; }} }}; }} }};\n",
        encoding="utf-8")
    cfg = scan._read_type_aware_lint(root)["configs"][0]
    assert [c["rule"] for c in cfg["custom_rules"]] == ["local/no-unsafe-enum-access"]
    assert "local/no-unsafe-enum-access" in cfg["rules"]


# --- collect: sample matching, cost bar, step fallback, benchmark ---------------

def _detect_with(block, crit, runs=None, wf=None):
    runs = runs if runs is not None else _runs(95, 20)
    withheld: dict[str, int] = {}
    cands: list[dict] = []
    out = cr._detect_opt82_type_aware_lint(
        ".github/workflows/lint.yml", runs, crit, wf or _WF, block, 0,
        withheld=withheld, withheld_candidates=cands)
    return out, withheld, cands


def test_a_lint_job_that_never_ran_in_the_sample_is_tallied_as_a_verdict(tmp_path):
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, cands = _detect_with(
        block, {"job_p50": {"unit": 20.0}, "long_pole_job": "unit"})
    assert out == []
    assert withheld == {"lint_job_never_ran_in_sample": 1}
    assert cands == []


@pytest.mark.parametrize("p50,fires", [(60.0, True), (59.9, False)])
def test_the_cost_bar_boundary_at_sub_second_precision(tmp_path, p50, fires):
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, _c = _detect_with(
        block, {"job_p50": {"eslint": p50, "unit": 200.0}, "long_pole_job": "unit"})
    assert bool(out) is fires, withheld
    if not fires:
        assert withheld == {"lint_job_below_cost_threshold": 1}


def test_a_matrix_suffixed_job_name_matches_its_yaml_job(tmp_path):
    runs = _runs(95, 20)
    for run in runs:
        for j in run:
            if j["name"] == "eslint":
                j["name"] = "eslint (node-20)"
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, _c = _detect_with(
        block, {"job_p50": {"eslint (node-20)": 95.0, "unit": 20.0},
                "long_pole_job": "eslint (node-20)"}, runs=runs)
    assert len(out) == 1, withheld
    assert out[0]["affected_jobs"] == ["eslint (node-20)"]


def test_an_unnamed_lint_step_is_measured_under_its_run_fallback_name(tmp_path):
    wf = json.loads(json.dumps(_WF))
    del wf["jobs"]["eslint"]["steps"][2]["name"]
    runs = _runs(95, 20)
    for run in runs:
        for j in run:
            for st in j["steps"]:
                if st["name"] == "Lint":
                    st["name"] = "Run npm run lint"
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, _c = _detect_with(
        block, {"job_p50": {"eslint": 95.0, "unit": 20.0}, "long_pole_job": "eslint"},
        runs=runs, wf=wf)
    assert len(out) == 1, withheld
    tal = out[0]["type_aware_lint"]
    assert tal["ceiling_basis"] == "lint_step" and tal["ceiling_s"] == 78.0
    assert "its lint step `Run npm run lint` measures 78s" in out[0]["evidence"]


def test_benchmark_strips_cache_location_and_its_value(tmp_path):
    root = _tree(tmp_path)
    pkg = json.loads(_PACKAGE_JSON)
    pkg["scripts"]["lint:eslint"] = ("eslint . --cache --cache-location .cache/eslint "
                                     "--max-warnings 0")
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    block = scan._read_type_aware_lint(root)
    out, withheld, _c = _detect(block)
    assert len(out) == 1, withheld
    bench = out[0]["type_aware_lint"]["benchmark_commands"]
    for cmd in bench.values():
        assert "--cache" not in cmd and ".cache/eslint" not in cmd, cmd
        assert "--max-warnings 0" in cmd, cmd


# --- render: the SIZING basis sentence ------------------------------------------

def test_sizing_line_names_the_basis_the_ceiling_was_measured_on(tmp_path):
    f, card = _rendered_card(tmp_path)
    assert "SIZING: uncredited. The lint step measured 78s at p50" in card
    assert "its lint step was not separately measured" not in card
    f["type_aware_lint"]["ceiling_basis"] = "lint_job"
    lines, _n, _on = bp._also_noticed_block([f], "https://example.invalid/cat.md")
    job_card = "\n".join(lines)
    assert ("SIZING: uncredited. The whole lint job (its lint step was not "
            "separately measured) measured 78s at p50") in job_card


# --- verify: orphan card, missing evidence block --------------------------------

def test_verifier_fails_an_opt82_card_with_no_opt82_finding(tmp_path):
    _f, card = _rendered_card(tmp_path)
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({"findings": []}), encoding="utf-8")
    c = _vr().check_opt82_type_aware_lint_uncredited(card, p)
    assert not c.ok and "no OPT82 finding" in c.detail, c.detail


def test_verifier_fails_a_finding_missing_its_evidence_block_kind(tmp_path):
    f, card = _rendered_card(tmp_path)
    f["type_aware_lint"]["kind"] = "something_else"
    c = _vr_check(tmp_path, f, card)
    assert not c.ok and "evidence block" in c.detail, c.detail


def test_benchmark_tells_the_agent_to_confirm_the_full_type_aware_rule_list(tmp_path):
    """The audit reads rules from source text; a typed rule it could not read
    (an unresolved setting, a shared config) stays on in the timing-only run,
    and ESLint then fails for want of type information instead of timing. The
    card must have the agent confirm the list with `--print-config` first."""
    _f, card = _rendered_card(tmp_path)
    flat = " ".join(card.split())
    assert "--print-config" in flat.split("LEDGER")[0], flat
    assert "fails" in flat and "type information" in flat


# --- detection gates: every "could not tell" is held back, never a verdict ----
# Each test below builds a case the audit cannot decide and asserts the gate is
# HELD BACK (counted AND listed with a phrase), not filed as the
# `type_aware_parsing_off` verdict and not silently skipped.

_OFF_CONFIG = "export default [{ rules: { 'no-console': 'warn' } }];\n"


def _wf_with(run: "str | None" = None, wd: "str | None" = None,
             uses: "str | None" = None, name: str = "Lint") -> dict:
    wf = json.loads(json.dumps(_WF))
    step: dict = {"name": name}
    if uses is not None:
        step["uses"] = uses
    else:
        step["run"] = run
    if wd is not None:
        step["working-directory"] = wd
    wf["jobs"]["eslint"]["steps"][2] = step
    return wf


def _held(withheld, cands, gate):
    assert withheld.get(gate) == 1, withheld
    assert [c["gate"] for c in cands] == [gate], cands
    assert gate in cr._OPT82_HELD_BACK_GATES
    assert bp._OPT82_WITHHOLD_PHRASES.get(gate), gate


def test_a_truncated_walk_that_never_reached_the_lint_dir_is_held_back(tmp_path):
    root = _tree(tmp_path, _OFF_CONFIG)
    deep = root / "packages" / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "package.json").write_text("{}", encoding="utf-8")
    (deep / "eslint.config.mjs").write_text(_FLAT_CONFIG_ON, encoding="utf-8")
    block = scan._read_type_aware_lint(root)
    assert block["truncated"] is True
    out, withheld, cands = _detect(
        block, wf=_wf_with("npx eslint .", wd="packages/a/b/c/d/e"))
    assert out == []
    _held(withheld, cands, "eslint_config_walk_incomplete")


def test_an_unfollowable_relative_import_is_held_back_not_read_as_off(tmp_path):
    cfg = ("import base from './eslint/missing-base.mjs';\n"
           "export default [...base, { rules: { 'no-console': 'warn' } }];\n")
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg))
    out, withheld, cands = _detect(block)
    assert out == []
    _held(withheld, cands, "config_import_unfollowed")


def test_an_unreadable_package_json_is_held_back_by_name(tmp_path):
    root = _tree(tmp_path)
    (root / "package.json").write_text("{ not json", encoding="utf-8")
    out, withheld, cands = _detect(scan._read_type_aware_lint(root))
    assert out == []
    _held(withheld, cands, "package_json_unreadable")


@pytest.mark.parametrize("name,text", [
    (".eslintrc.json", json.dumps({"extends": ["@acme/eslint-config"],
                                   "rules": {"no-console": "warn"}})),
    ("eslint.config.mjs", "import config from '@acme/eslint-config';\n"
                          "export default [...config, { rules: { 'no-console': 'warn' } }];\n"),
    (".eslintrc.js", "module.exports = { extends: ['airbnb', 'prettier'], "
                     "rules: { 'no-console': 'warn' } };\n"),
])
def test_a_shared_config_package_is_held_back_not_read_as_off(tmp_path, name, text):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / name).write_text(text, encoding="utf-8")
    out, withheld, cands = _detect(scan._read_type_aware_lint(root))
    assert out == []
    _held(withheld, cands, "shared_config_unfollowable")


def test_a_known_non_type_aware_shared_config_stays_a_verdict(tmp_path):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / ".eslintrc.json").write_text(json.dumps(
        {"extends": ["eslint:recommended", "plugin:@typescript-eslint/recommended",
                     "prettier", "next/core-web-vitals"]}), encoding="utf-8")
    out, withheld, cands = _detect(scan._read_type_aware_lint(root))
    assert out == [] and withheld.get("type_aware_parsing_off") == 1 and cands == []


def test_a_typed_rule_with_no_parser_setting_in_view_is_held_back(tmp_path):
    cfg = ("export default [{ rules: "
           "{ '@typescript-eslint/no-floating-promises': 'error' } }];\n")
    out, withheld, cands = _detect(scan._read_type_aware_lint(_tree(tmp_path, cfg)))
    assert out == []
    _held(withheld, cands, "type_aware_rule_without_parser_setting")


@pytest.mark.parametrize("body", [
    "turbo run lint", "next lint", "run-p lint:*", "nx run-many -t lint",
    "node scripts/lint.mjs", "lerna run lint",
])
def test_a_lint_script_delegated_to_another_tool_is_held_back(tmp_path, body):
    root = _tree(tmp_path)
    pkg = json.loads(_PACKAGE_JSON)
    pkg["scripts"]["lint"] = body
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    out, withheld, cands = _detect(scan._read_type_aware_lint(root))
    assert out == []
    _held(withheld, cands, "lint_delegated_to_unread_tool")


@pytest.mark.parametrize("step", [
    {"run": "turbo run lint"}, {"run": "npx nx lint web"},
    {"run": "make lint"}, {"run": "pnpm turbo lint"},
    {"uses": "./.github/actions/lint"},
])
def test_a_lint_step_delegated_to_another_tool_is_held_back(tmp_path, step):
    wf = _wf_with(step.get("run"), uses=step.get("uses"))
    out, withheld, cands = _detect(scan._read_type_aware_lint(_tree(tmp_path)), wf=wf)
    assert out == []
    _held(withheld, cands, "lint_delegated_to_unread_tool")


def test_a_lint_script_in_another_workspace_is_held_back(tmp_path):
    out, withheld, cands = _detect(scan._read_type_aware_lint(_tree(tmp_path)),
                                   wf=_wf_with("yarn workspace web lint"))
    assert out == []
    _held(withheld, cands, "lint_script_unresolvable")


@pytest.mark.parametrize("run", ["npx eslint@8 .", "npx --yes eslint@9 ."])
def test_a_version_pinned_eslint_is_still_eslint(tmp_path, run):
    out, withheld, _c = _detect(scan._read_type_aware_lint(_tree(tmp_path)),
                                wf=_wf_with(run))
    assert len(out) == 1, withheld


@pytest.mark.parametrize("run", ["golangci-lint run", "actionlint",
                                 "npx stylelint '**/*.css'", "npm run lint:biome"])
def test_non_eslint_linters_are_never_held_back(tmp_path, run):
    root = _tree(tmp_path)
    pkg = json.loads(_PACKAGE_JSON)
    pkg["scripts"]["lint:biome"] = "biome lint ."
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    out, withheld, cands = _detect(scan._read_type_aware_lint(root), wf=_wf_with(run))
    assert out == [] and withheld == {} and cands == [], (run, withheld)


@pytest.mark.parametrize("run,wd", [
    ("npm run lint:${{ matrix.pkg }}", None),
    ("npm run lint", "${{ matrix.dir }}"),
])
def test_a_runtime_expression_in_the_lint_step_has_its_own_gate(tmp_path, run, wd):
    out, withheld, cands = _detect(scan._read_type_aware_lint(_tree(tmp_path)),
                                   wf=_wf_with(run, wd=wd))
    assert out == []
    _held(withheld, cands, "lint_step_uses_runtime_expression")


def test_a_findings_doc_without_the_scan_block_is_held_back_honestly(tmp_path):
    out, withheld, cands = _detect(None)
    assert out == []
    _held(withheld, cands, "type_aware_lint_scan_missing")


@pytest.mark.parametrize("eslint_dep,held", [(None, True), ("^10.0.0", True),
                                             ("^9.12.0", False)])
def test_a_nested_flat_config_is_ambiguous_unless_eslint_is_known_below_10(
        tmp_path, eslint_dep, held):
    root = _tree(tmp_path)
    pkg = json.loads(_PACKAGE_JSON)
    if eslint_dep:
        pkg["devDependencies"] = {"eslint": eslint_dep}
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    (root / "packages" / "web").mkdir(parents=True)
    (root / "packages" / "web" / "eslint.config.mjs").write_text(_OFF_CONFIG,
                                                                 encoding="utf-8")
    out, withheld, cands = _detect(scan._read_type_aware_lint(root))
    if held:
        assert out == []
        _held(withheld, cands, "eslint_config_lookup_ambiguous")
    else:
        assert len(out) == 1, withheld


def test_a_preset_name_in_a_plain_string_is_not_type_aware(tmp_path):
    cfg = ("const s = 'recommendedTypeChecked';\n"
           "const t = 'strict-type-checked';\n"
           "export default [{ rules: { 'no-console': 'warn' } }];\n")
    cfg_read = scan._read_type_aware_lint(_tree(tmp_path, cfg))["configs"][0]
    assert cfg_read["type_aware"] == "off", cfg_read


def test_a_flat_config_that_spreads_a_local_base_reads_the_base(tmp_path):
    root = _tree(tmp_path, "import base from './config/base.mjs';\n"
                           "export default [...base, { rules: "
                           "{ '@typescript-eslint/no-floating-promises': 'error' } }];\n")
    (root / "config").mkdir()
    (root / "config" / "base.mjs").write_text(
        "export default [{ languageOptions: { parserOptions: "
        "{ projectService: true } } }];\n", encoding="utf-8")
    out, withheld, _c = _detect(scan._read_type_aware_lint(root))
    assert len(out) == 1, withheld


def test_an_eslintrc_that_extends_a_local_base_reads_the_base(tmp_path):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / ".eslintrc.js").write_text(
        "module.exports = { extends: ['./eslint/base.js'], rules: "
        "{ '@typescript-eslint/no-floating-promises': 'error' } };\n", encoding="utf-8")
    (root / "eslint").mkdir()
    (root / "eslint" / "base.js").write_text(
        "module.exports = { parserOptions: { project: true } };\n", encoding="utf-8")
    out, withheld, _c = _detect(scan._read_type_aware_lint(root))
    assert len(out) == 1, withheld


@pytest.mark.parametrize("child,fires", [
    ({"rules": {"no-console": "warn"}}, True),                       # inherits ON
    ({"root": True, "rules": {"no-console": "warn"}}, False),        # cascade stops
    ({"parserOptions": {"project": False}}, False),                  # explicit OFF
])
def test_a_legacy_eslintrc_cascades_upward_to_root_true(tmp_path, child, fires):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / ".eslintrc.json").write_text(json.dumps({
        "root": True, "parserOptions": {"project": True},
        "rules": {"@typescript-eslint/no-floating-promises": "error"}}), encoding="utf-8")
    (root / "packages" / "a").mkdir(parents=True)
    (root / "packages" / "a" / ".eslintrc.json").write_text(json.dumps(child),
                                                            encoding="utf-8")
    out, withheld, cands = _detect(scan._read_type_aware_lint(root),
                                   wf=_wf_with("npx eslint .", wd="packages/a"))
    if fires:
        assert len(out) == 1, withheld
    else:
        assert out == [] and withheld.get("type_aware_parsing_off") == 1 and cands == []


def test_a_one_job_lint_workflow_is_not_a_long_pole_by_default(tmp_path):
    """A one-job workflow's only job is trivially its own long pole; that must
    not waive the 60s bar."""
    block = scan._read_type_aware_lint(_tree(tmp_path))
    runs = [[r[0]] for r in _runs(25, 20)]
    crit = cr._critical_path(runs)
    withheld: dict = {}
    out = cr._detect_opt82_type_aware_lint(
        ".github/workflows/lint.yml", runs, crit, _WF, block, 0, withheld=withheld,
        withheld_candidates=[])
    assert out == [] and withheld.get("lint_job_below_cost_threshold") == 1, withheld


def test_a_jsonc_eslintrc_with_trailing_commas_is_read(tmp_path):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / ".eslintrc.json").write_text(
        '{\n  // jsonc\n  "parserOptions": { "project": true, },\n'
        '  "rules": { "@typescript-eslint/no-floating-promises": "error", },\n}\n',
        encoding="utf-8")
    block = scan._read_type_aware_lint(root)
    assert block["unreadable"] == [] and block["configs"][0]["type_aware"] == "on"


def test_project_null_is_a_documented_off_not_unresolved(tmp_path):
    cfg = _FLAT_CONFIG_ON.replace("projectService: true", "project: null")
    assert scan._read_type_aware_lint(_tree(tmp_path, cfg))["configs"][0][
        "type_aware"] == "off"


def test_a_duplicate_rule_row_fails_closed_at_load(tmp_path, monkeypatch):
    bad = tmp_path / "rules.tsv"
    row = "@typescript-eslint/await-thenable\t-\trecommended\n"
    bad.write_text(row + row, encoding="utf-8")
    monkeypatch.setattr(scan, "_TAL_RULES_PATH", bad)
    monkeypatch.setattr(scan, "_TAL_RULES_CACHE", None)
    with pytest.raises(ValueError):
        scan._load_type_aware_rules()


@pytest.mark.parametrize("cfg,on", [
    ("import tseslint from 'typescript-eslint';\n"
     "export default tseslint.config(tseslint.configs.all);\n", True),
    ("import tseslint from 'typescript-eslint';\n"
     "export default [...tseslint.configs.all];\n", True),
    ("import typescriptEslint from '@typescript-eslint/eslint-plugin';\n"
     "export default [typescriptEslint.configs.all];\n", True),
    ("import js from '@eslint/js';\nexport default [js.configs.all];\n", False),
])
def test_the_all_preset_reads_on_only_when_it_is_typescript_eslints(tmp_path, cfg, on):
    c = scan._read_type_aware_lint(_tree(tmp_path, cfg))["configs"][0]
    assert (c["type_aware"] == "on") is on, c
    assert ("all" in c["presets"]) is on, c


def test_the_legacy_all_preset_string_reads_on(tmp_path):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / ".eslintrc.json").write_text(json.dumps(
        {"extends": ["plugin:@typescript-eslint/all"]}), encoding="utf-8")
    c = scan._read_type_aware_lint(root)["configs"][0]
    assert c["type_aware"] == "on" and "all" in c["presets"], c


# --- fail-closed reader branches (each was downgradable to off by a mutant) ----

@pytest.mark.parametrize("parser_options", [
    "parserOptions: sharedOpts",                      # taken from a variable
    "parserOptions: { ...shared, projectService: true }",   # spread inside
    "parserOptions: { project }",                     # shorthand
])
def test_js_parser_options_the_read_cannot_see_are_unresolved(tmp_path, parser_options):
    cfg = ("export default [{ languageOptions: { " + parser_options + " }, rules: "
           "{ '@typescript-eslint/no-floating-promises': 'error' } }];\n")
    root = _tree(tmp_path, cfg)
    assert scan._read_type_aware_lint(root)["configs"][0]["type_aware"] == "unresolved"
    out, withheld, cands = _detect(scan._read_type_aware_lint(root))
    assert out == []
    _held(withheld, cands, "type_aware_setting_unresolvable")


@pytest.mark.parametrize("rc", [
    {"parserOptions": "./tsconfig.json"},             # not a mapping
    {"parserOptions": {"project": 5}},                # not a literal on
    {"parserOptions": {"project": {"a": 1}}},         # object project (only projectService)
    {"parserOptions": {"project": ["./a.json", 1]}},  # mixed list
])
def test_eslintrc_parser_options_the_read_cannot_see_are_unresolved(tmp_path, rc):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / ".eslintrc.json").write_text(json.dumps(rc), encoding="utf-8")
    assert scan._read_type_aware_lint(root)["configs"][0]["type_aware"] == "unresolved"


@pytest.mark.parametrize("value", ["sev", "isCI ? 'error' : 'off'"])
def test_a_rule_severity_only_known_at_run_time_is_held_back(tmp_path, value):
    cfg = ("export default [{ languageOptions: { parserOptions: { projectService: true } },"
           " rules: { '@typescript-eslint/no-floating-promises': " + value + " } }];\n")
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg))
    c = block["configs"][0]
    assert c["rules"] == [] and c["rule_unresolved"] == [
        "@typescript-eslint/no-floating-promises"], c
    out, withheld, cands = _detect(block)
    assert out == []
    _held(withheld, cands, "rule_setting_unresolvable")


@pytest.mark.parametrize("name,text", [(".eslintrc.json", b"[]\n"),
                                       (".eslintrc.json", b"\xff\xfe{\x00}\x00"),
                                       ("eslint.config.mjs", b"\xff\xfe{\x00}\x00")])
def test_an_unreadable_config_is_held_back_never_dropped(tmp_path, name, text):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()    # the unreadable file is the only config
    (root / name).write_bytes(text)
    block = scan._read_type_aware_lint(root)
    assert name in block["unreadable"]
    out, withheld, cands = _detect(block)
    assert out == []
    _held(withheld, cands, "eslint_config_unreadable")


def test_no_config_at_all_is_held_back_never_read_as_off(tmp_path):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    out, withheld, cands = _detect(scan._read_type_aware_lint(root))
    assert out == []
    _held(withheld, cands, "no_eslint_config_found")


def test_one_on_and_one_unresolved_applicable_config_is_held_back(tmp_path):
    root = _tree(tmp_path)
    (root / "eslint.config.mjs").unlink()
    (root / ".eslintrc.json").write_text(json.dumps({
        "root": True, "parserOptions": {"project": True},
        "rules": {"@typescript-eslint/no-floating-promises": "error"}}), encoding="utf-8")
    (root / "packages" / "a").mkdir(parents=True)
    (root / "packages" / "a" / ".eslintrc.json").write_text(json.dumps(
        {"parserOptions": {"project": 5}}), encoding="utf-8")
    out, withheld, cands = _detect(scan._read_type_aware_lint(root),
                                   wf=_wf_with("npx eslint .", wd="packages/a"))
    assert out == []
    _held(withheld, cands, "type_aware_setting_unresolvable")


# --- order and scope of settings (greptile) -------------------------------------

_PO_ON = "{ languageOptions: { parserOptions: { projectService: true } } }"


def test_a_typed_rule_turned_off_later_unscoped_is_never_named(tmp_path):
    cfg = ("export default [" + _PO_ON + ",\n"
           "  { rules: { '@typescript-eslint/no-floating-promises': 'error' } },\n"
           "  { rules: { '@typescript-eslint/no-floating-promises': 'off' } }];\n")
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg))
    assert "@typescript-eslint/no-floating-promises" not in block["configs"][0]["rules"]
    out, withheld, cands = _detect(block)
    assert out == []
    _held(withheld, cands, "rule_setting_unresolvable")


def test_a_typed_rule_turned_off_only_for_some_files_is_still_named(tmp_path):
    cfg = ("export default [" + _PO_ON + ",\n"
           "  { rules: { '@typescript-eslint/no-floating-promises': 'error' } },\n"
           "  { files: ['**/*.test.ts'],\n"
           "    rules: { '@typescript-eslint/no-floating-promises': 'off' } }];\n")
    out, withheld, _c = _detect(scan._read_type_aware_lint(_tree(tmp_path, cfg)))
    assert len(out) == 1, withheld


def test_an_unscoped_disable_type_checked_after_a_preset_is_held_back(tmp_path):
    cfg = ("import tseslint from 'typescript-eslint';\n"
           "export default tseslint.config(...tseslint.configs.recommendedTypeChecked,\n"
           "  tseslint.configs.disableTypeChecked);\n")
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg))
    assert block["configs"][0]["type_aware"] == "unresolved", block["configs"][0]
    out, withheld, cands = _detect(block)
    assert out == []
    _held(withheld, cands, "type_aware_setting_unresolvable")


def test_disable_type_checked_scoped_to_js_files_keeps_the_preset_on(tmp_path):
    cfg = ("import tseslint from 'typescript-eslint';\n"
           "export default tseslint.config(...tseslint.configs.recommendedTypeChecked,\n"
           "  { files: ['**/*.js'], extends: [tseslint.configs.disableTypeChecked] });\n")
    block = scan._read_type_aware_lint(_tree(tmp_path, cfg))
    assert block["configs"][0]["type_aware"] == "on"
    assert len(_detect(block)[0]) == 1


def _rc_typed() -> str:
    return json.dumps({"parserOptions": {"project": True},
                       "rules": {"@typescript-eslint/no-floating-promises": "error"}})


def test_a_flat_config_wins_over_an_old_eslintrc_beside_it(tmp_path):
    root = _tree(tmp_path, _OFF_CONFIG)
    (root / ".eslintrc.json").write_text(_rc_typed(), encoding="utf-8")
    out, withheld, cands = _detect(scan._read_type_aware_lint(root))
    assert out == [] and withheld.get("type_aware_parsing_off") == 1 and cands == []


def test_eslint_use_flat_config_false_selects_the_eslintrc(tmp_path):
    root = _tree(tmp_path, _OFF_CONFIG)
    (root / ".eslintrc.json").write_text(_rc_typed(), encoding="utf-8")
    wf = _wf_with("ESLINT_USE_FLAT_CONFIG=false npx eslint .")
    out, withheld, _c = _detect(scan._read_type_aware_lint(root), wf=wf)
    assert len(out) == 1, withheld
    wf = _wf_with("npx eslint .")
    wf["jobs"]["eslint"]["env"] = {"ESLINT_USE_FLAT_CONFIG": "false"}
    out, withheld, _c = _detect(scan._read_type_aware_lint(root), wf=wf)
    assert len(out) == 1, withheld


def test_a_flat_config_above_beats_a_nearer_eslintrc(tmp_path):
    root = _tree(tmp_path)                               # flat config, type-aware ON
    (root / "packages" / "a").mkdir(parents=True)
    (root / "packages" / "a" / ".eslintrc.json").write_text(
        json.dumps({"rules": {"no-console": "warn"}}), encoding="utf-8")
    out, withheld, _c = _detect(scan._read_type_aware_lint(root),
                                wf=_wf_with("npx eslint .", wd="packages/a"))
    assert len(out) == 1, withheld


def _custom_rule_tree(tmp_path, index: str, rule_key: str):
    cfg = ("import local from './eslint-rules/index.mjs';\n"
           "export default [" + _PO_ON + ",\n"
           "  { plugins: { local }, rules: { '" + rule_key + "': 'error' } }];\n")
    root = _tree(tmp_path, cfg)
    (root / "eslint-rules" / "index.mjs").write_text(index, encoding="utf-8")
    return scan._read_type_aware_lint(root)


def test_a_custom_rule_in_another_namespace_is_not_proven_typed(tmp_path):
    block = _custom_rule_tree(tmp_path, _RULES_INDEX, "other/no-unsafe-enum-access")
    c = block["configs"][0]
    assert c["custom_rules"] == [] and c["rule_unresolved"] == [
        "other/no-unsafe-enum-access"], c
    out, withheld, cands = _detect(block)
    assert out == []
    _held(withheld, cands, "rule_setting_unresolvable")


def test_a_custom_rule_is_matched_by_the_plugins_key_not_the_file_name(tmp_path):
    index = ("import noUnsafeEnumAccess from './no-unsafe-enum-access.mjs';\n"
             "export default { rules: { 'enum-safety': noUnsafeEnumAccess } };\n")
    by_file = _custom_rule_tree(tmp_path / "a", index, "local/no-unsafe-enum-access")
    assert by_file["configs"][0]["custom_rules"] == []
    assert by_file["configs"][0]["rule_unresolved"] == ["local/no-unsafe-enum-access"]
    by_key = _custom_rule_tree(tmp_path / "b", index, "local/enum-safety")
    assert [c["rule"] for c in by_key["configs"][0]["custom_rules"]] == ["local/enum-safety"]
    assert len(_detect(by_key)[0]) == 1


def test_verifier_fails_a_finding_stamped_from_a_walk_that_missed_its_dir(tmp_path):
    f, card = _rendered_card(tmp_path)
    f["type_aware_lint"]["working_directory"] = "packages/a/b/c/d/e"
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({"findings": [f], "type_aware_lint": {
        "truncated": True, "configs": [{"path": "eslint.config.mjs", "dir": ""}],
        "unreadable": [], "error": None}}), encoding="utf-8")
    c = _vr().check_opt82_type_aware_lint_uncredited(card, p)
    assert not c.ok and "truncated" in c.detail, c.detail


# --- the lint-command resolver: cd, quoting, forwarded args, npx flags, runner --

def _lint_wf(run: str):
    wf = json.loads(json.dumps(_WF))
    wf["jobs"]["eslint"]["steps"][2]["run"] = run
    return wf


def test_a_literal_cd_moves_the_lint_and_its_benchmark_to_that_directory(tmp_path):
    """`cd packages/web && eslint .` lints from packages/web: the config is
    looked up from there and the benchmark must run there, not at the root."""
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, _c = _detect(block, wf=_lint_wf("cd packages/web && eslint ."))
    assert len(out) == 1, withheld
    bench = out[0]["type_aware_lint"]["benchmark_commands"]
    assert bench["as_ci_runs_it"].startswith("(cd packages/web && time "), bench
    res = cr._opt82_resolve_lint("cd ./a && cd ../b/c && eslint .", "apps", {})
    assert res[0] == "eslint" and res[3]["wd"] == "apps/b/c", res


@pytest.mark.parametrize("cd", ["cd ${{ matrix.dir }}", "cd $PKG_DIR", "cd /tmp/x",
                                "cd -", "cd", "cd ../..", "cd ~/src"])
def test_an_untraceable_cd_before_the_lint_is_held_back(tmp_path, cd):
    block = scan._read_type_aware_lint(_tree(tmp_path))
    out, withheld, cands = _detect(block, wf=_lint_wf(f"{cd} && eslint ."))
    # A `cd` to a value only known at run time is a runtime expression; any
    # other untraceable `cd` has its own gate. Neither is a "package script".
    gate = ("lint_step_uses_runtime_expression" if "$" in cd
            else "lint_step_cd_untraceable")
    assert out == [] and withheld.get(gate) == 1, withheld
    assert cands[0]["gate"] == gate
    assert gate in bp._OPT82_WITHHOLD_PHRASES


def test_a_quoted_path_stays_one_argument_in_the_benchmark():
    res = cr._opt82_resolve_lint('eslint "src/my file.ts"', "", {})
    assert res[0] == "eslint"
    bench = cr._opt82_benchmark_commands(res[1], "", ["r"])
    assert "'src/my file.ts'" in bench["as_ci_runs_it"], bench


@pytest.mark.parametrize("cmd", [
    "npm run lint -- --config eslint.config.syntax.mjs",
    "pnpm run lint --config eslint.config.syntax.mjs",
    "pnpm lint --config eslint.config.syntax.mjs",
    "yarn lint -- --config eslint.config.syntax.mjs",
    "bun run lint --config eslint.config.syntax.mjs",
])
def test_args_forwarded_to_a_lint_script_reach_the_eslint_command(cmd):
    scripts = {"": {"lint": "eslint ."}}
    res = cr._opt82_resolve_lint(cmd, "", scripts)
    assert res[0] == "eslint", res
    assert cr._opt82_config_flag(res[1]) == "eslint.config.syntax.mjs", res


def test_npm_run_without_a_separator_forwards_nothing():
    res = cr._opt82_resolve_lint("npm run lint --silent", "", {"": {"lint": "eslint ."}})
    assert res[0] == "eslint" and res[1] == "eslint .", res


@pytest.mark.parametrize("cmd", ["npx -p eslint eslint .", "npx --package eslint eslint .",
                                 "npx -y -p eslint@9 eslint .",
                                 "npm exec -p eslint -- eslint .",
                                 "npx -c 'eslint .'", "npx --call 'eslint .'"])
def test_npx_flags_that_take_a_value_never_become_the_lint_command(cmd):
    res = cr._opt82_resolve_lint(cmd, "", {})
    assert res is not None and res[0] == "eslint" and res[1] == "eslint .", res


@pytest.mark.parametrize("cmd,runner", [
    ("npx eslint .", "npx"), ("eslint .", "npx"),
    ("pnpm exec eslint .", "pnpm exec"), ("yarn eslint .", "yarn"),
    ("bunx eslint .", "bunx"), ("pnpm run lint", "pnpm exec"),
    ("yarn lint", "yarn"),
])
def test_the_benchmark_runs_eslint_through_the_runner_ci_used(cmd, runner):
    res = cr._opt82_resolve_lint(cmd, "", {"": {"lint": "eslint ."}})
    assert res[0] == "eslint" and res[3]["runner"] == runner, res
    bench = cr._opt82_benchmark_commands(res[1], "", ["r"], runner=res[3]["runner"])
    assert bench["as_ci_runs_it"] == f"(time {runner} eslint .)", bench


# --- which config a lint run uses ---------------------------------------------
#
# ESLint reads the config passed with `-c`/`--config`; otherwise the nearest
# config at or above the directory the lint runs in, and that directory comes
# from the step, the job's `defaults.run` or the workflow's `defaults.run`. A
# legacy `.eslintrc` below the directory cascades into the run too. Each test
# puts a type-aware config where ESLint WOULD look and a syntax-only one where
# a wrong read would land, so picking the wrong file flips the outcome.

_FLAT_TA_ON = ("export default [{ languageOptions: { parserOptions: "
               "{ projectService: true } }, rules: "
               "{ '@typescript-eslint/no-floating-promises': 'error' } }];\n")
_FLAT_TA_OFF = "export default [{ rules: { 'no-console': 'warn' } }];\n"


def _cfg_block(tmp_path: Path, files: dict[str, str]) -> dict:
    root = tmp_path / "cfgrepo"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return scan._read_type_aware_lint(root)


def _lint_wf(run: str, step_wd: "str | None" = None, job_wd: "str | None" = None,
             wf_wd: "str | None" = None) -> dict:
    wf = json.loads(json.dumps(_WF))
    step = wf["jobs"]["eslint"]["steps"][2]
    step["run"] = run
    if step_wd is not None:
        step["working-directory"] = step_wd
    if job_wd is not None:
        wf["jobs"]["eslint"]["defaults"] = {"run": {"working-directory": job_wd}}
    if wf_wd is not None:
        wf["defaults"] = {"run": {"working-directory": wf_wd}}
    return wf


def _fired_configs(out: list[dict]) -> set[str]:
    assert len(out) == 1, out
    return {r["config"] for r in out[0]["type_aware_lint"]["rules"]}


@pytest.mark.parametrize("run", [
    "npx eslint -c ../lint/eslint.config.mjs .",
    "npx eslint --config=../lint/eslint.config.mjs .",
])
def test_an_explicit_config_flag_is_the_config_the_run_uses(tmp_path, run):
    """`-c`/`--config` (resolved against the step's working directory) beats
    the nearest config: the root config here is syntax-only, the named one is
    type-aware."""
    block = _cfg_block(tmp_path, {"eslint.config.mjs": _FLAT_TA_OFF,
                                  "lint/eslint.config.mjs": _FLAT_TA_ON})
    out, withheld, _c = _detect(block, wf=_lint_wf(run, step_wd="app"))
    assert _fired_configs(out) == {"lint/eslint.config.mjs"}, withheld


@pytest.mark.parametrize("step_wd,norm_wd", [("./packages/web", "packages/web"),
                                             ("packages/web/src/", "packages/web/src")])
def test_the_nearest_config_at_or_above_the_working_dir_wins(tmp_path, step_wd, norm_wd):
    """A package's own flat config, not the repo root's, governs a lint run
    inside that package (from the package dir or a dir under it). The
    benchmark commands `cd` into the same directory the lint ran in."""
    block = _cfg_block(tmp_path, {"eslint.config.mjs": _FLAT_TA_OFF,
                                  "packages/web/eslint.config.mjs": _FLAT_TA_ON})
    out, withheld, _c = _detect(block, wf=_lint_wf("npx eslint .", step_wd=step_wd))
    assert _fired_configs(out) == {"packages/web/eslint.config.mjs"}, withheld
    for cmd in out[0]["type_aware_lint"]["benchmark_commands"].values():
        assert cmd.startswith(f"(cd {norm_wd} && time npx eslint"), cmd


@pytest.mark.parametrize("level", ["job_wd", "wf_wd"])
def test_defaults_run_working_directory_places_the_lint_run(tmp_path, level):
    """A step with no `working-directory` of its own runs in the job's
    `defaults.run.working-directory`, else the workflow's."""
    block = _cfg_block(tmp_path, {"eslint.config.mjs": _FLAT_TA_OFF,
                                  "packages/web/eslint.config.mjs": _FLAT_TA_ON})
    out, withheld, _c = _detect(
        block, wf=_lint_wf("npx eslint .", **{level: "packages/web"}))
    assert _fired_configs(out) == {"packages/web/eslint.config.mjs"}, withheld


def test_a_working_directory_expression_is_held_back_not_read_as_root(tmp_path):
    """`working-directory: ${{ matrix.pkg }}` cannot be evaluated offline, so
    which config applies is unknown: held back and named, never answered from
    the root config (which here would fire)."""
    block = _cfg_block(tmp_path, {"eslint.config.mjs": _FLAT_TA_ON})
    out, _w, cands = _detect(
        block, wf=_lint_wf("npx eslint .", step_wd="${{ matrix.pkg }}"))
    assert out == [], out
    assert len(cands) == 1 and cands[0]["job"] == "eslint", cands
    assert cands[0]["gate"] in cr._OPT82_HELD_BACK_GATES, cands


def test_a_legacy_eslintrc_below_the_working_dir_cascades_into_the_run(tmp_path):
    """Legacy `.eslintrc` files cascade: a lint run from the repo root also
    applies a nested package's `.eslintrc.json`, so its type-aware setting
    counts even though the root one is syntax-only."""
    off = json.dumps({"root": True, "rules": {"no-console": "warn"}})
    on = json.dumps({"parser": "@typescript-eslint/parser",
                     "parserOptions": {"project": "./tsconfig.json"},
                     "rules": {"@typescript-eslint/no-floating-promises": "error"}})
    block = _cfg_block(tmp_path, {".eslintrc.json": off,
                                  "packages/web/.eslintrc.json": on})
    out, withheld, _c = _detect(block, wf=_lint_wf("npx eslint ."))
    assert _fired_configs(out) == {"packages/web/.eslintrc.json"}, withheld


# --- which commands run ESLint --------------------------------------------------

@pytest.mark.parametrize("cmd,scripts,lint_command", [
    ("npx eslint . --max-warnings 0", {}, "eslint . --max-warnings 0"),
    ("npx --no-install eslint src", {}, "eslint src"),
    ("pnpm exec eslint .", {}, "eslint ."),
    ("./node_modules/.bin/eslint src --ext .ts", {}, "eslint src --ext .ts"),
    # a package script that runs ESLint is followed whatever it is called
    ("pnpm run check", {"check": "eslint ."}, "eslint ."),
    ("pnpm check", {"check": "eslint ."}, "eslint ."),
    # four hops of scripts, inside the depth cap
    ("npm run lint", {"lint": "npm run lint:1", "lint:1": "npm run lint:2",
                      "lint:2": "npm run lint:3", "lint:3": "eslint ."}, "eslint ."),
])
def test_eslint_invocation_forms_resolve_to_eslint(cmd, scripts, lint_command):
    res = cr._opt82_resolve_lint(cmd, "", {"": scripts})
    assert res is not None and res[0] == "eslint" and res[1] == lint_command, res


@pytest.mark.parametrize("cmd,scripts", [
    # a `${{ }}` expression in a lint-named segment: what runs is unknown
    ("npm run ${{ matrix.lint-script }}", {"lint": "eslint ."}),
    # `pnpm lint` naming a script this read cannot see
    ("pnpm lint", {}),
    # a script that calls itself: the depth cap stops it
    ("npm run lint", {"lint": "npm run lint"}),
    # scoped to ANOTHER package: never answered from the root's own `lint`
    ("pnpm --filter web lint", {"lint": "eslint ."}),
    ("pnpm --filter=web run lint", {"lint": "eslint ."}),
    ("npm run lint --workspace=packages/web", {"lint": "eslint ."}),
    ("npm run lint --workspace packages/web", {"lint": "eslint ."}),
])
def test_untraceable_lint_commands_are_unresolvable_never_eslint(cmd, scripts):
    res = cr._opt82_resolve_lint(cmd, "", {"": scripts})
    assert res is not None and res[0] == "unresolvable", res


# --- an OPT82 finding on a drilled pole is that pole's catalog cover -----------

_UNKNOWN_LOG = {"pipeline": "nothing any detector knows\n"}


def _opt82_on_the_drilled_pole(tmp_path):
    """The one-pole render fixture, with the lint finding routed to its job: a
    captured log no detector recognises, and no other catalog match."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_blocking_path as tb  # noqa: E402

    f = _detect(scan._read_type_aware_lint(_tree(tmp_path)))[0][0]
    doc = tb._doc_one_pole()
    pole = doc["pr_critical_path"]["poles"][0]
    f = dict(f, workflow_file=pole["workflow_file"], affected_jobs=[pole["job"]])
    doc["findings"] = [f]
    return doc, f


def _render_pole(doc):
    return bp.render(json.loads(json.dumps(doc)), dict(_UNKNOWN_LOG), {},
                     {"pipeline": "https://github.com/o/r/actions/runs/123"},
                     "2026-06-08")


def test_opt82_on_a_drilled_pole_is_its_catalog_cover_not_a_coverage_gap(tmp_path):
    """OPT82 names the pole's cause and targets poles directly ("60s or more,
    or the slowest job"), so the pole it sits on is covered: no coverage-gap
    wording, the waterfall and the prompt point at its card, and the
    maintainer gap loop is never sent to draft a detector for it. Uncredited
    by design, so unlike OPT79 no magnitude gates the cover."""
    doc, f = _opt82_on_the_drilled_pole(tmp_path)
    assert not f.get("wall_clock_p50_s")
    md = _render_pole(doc)
    pole = md.split('<a id="pole-1"></a>', 1)[1].split("\n## ", 2)[1]
    assert "NO CATALOG PATTERN MATCHED" not in pole, pole
    assert "coverage gap" not in pole, pole
    assert ("a measured **catalog pattern** (OPT82, lint builds the whole type graph) "
            "matched this pole - see its card in the **Also noticed** section below"
            in pole), pole
    prompt = pole.split("Prompt for your coding agent", 1)[1]
    assert "OPT82" in prompt and "Also noticed" in prompt, prompt
    assert "uncredited, benchmark first" in md          # the card it points at renders
    assert bp._gap_poles(doc, dict(_UNKNOWN_LOG)) == []
    # an advisory OPT82 makes no claim, so the pole stays a gap
    adv = json.loads(json.dumps(doc))
    adv["findings"][0]["advisory"] = True
    assert len(bp._gap_poles(adv, dict(_UNKNOWN_LOG))) == 1


def test_verifier_fails_an_opt82_pole_that_renders_the_coverage_gap(tmp_path):
    doc, _f = _opt82_on_the_drilled_pole(tmp_path)
    md = _render_pole(doc)
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    vr = _vr()
    c = vr.check_opt82_type_aware_lint_uncredited(md, p)
    assert c.ok, c.detail
    head = md.index('<a id="pole-1"></a>')
    for wording in ("NO CATALOG PATTERN MATCHED",
                    "this is a coverage gap, not a clean job"):
        bad = md[:head] + md[head:].replace("```text", f"{wording}\n\n```text", 1)
        c = vr.check_opt82_type_aware_lint_uncredited(bad, p)
        assert not c.ok and "coverage gap" in c.detail, (wording, c.detail)

"""OPT84 — A Slow Step on the Long Pole Runs a Tool That Has a Native Drop-in.

The report half: the card (`blocking_path._opt84_card`), where it sits in the
report, the held-back Data sources row, and the verifier arm
(`verify_report.check_opt84_tool_swap`), plus the coupling pins between the
renderer's and the verifier's copies. The finding is built by hand to the
engine's finding contract, so these tests do not depend on the detector.
"""
from __future__ import annotations

import copy
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
sys.path.insert(0, str(_SKILL_DIR / "tests"))
import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import verify_report as vr  # noqa: E402

_WF = ".github/workflows/lint.yml"
_STEP = "Run lint and format checks"
_HELD_BACK = {
    "pole_workflow_ambiguous", "pole_step_entries_unreadable",
    "pole_step_timings_unavailable", "workflow_yaml_unavailable",
    "pole_job_not_matched_in_yaml", "measured_step_not_matched_in_yaml",
    "command_has_expression", "command_changes_directory", "script_unresolvable",
    "delegated_script_unresolved", "package_json_unreadable",
    "candidate_is_an_action", "eslint_config_unreadable",
    "typescript_version_unreadable", "working_directory_unknown",
    "type_aware_lint_scan_missing", "type_aware_config_reader_failed",
}
# Decided before any candidate step is chosen: listed with the job, no step.
_POLE_LEVEL = {
    "pole_workflow_ambiguous", "pole_step_entries_unreadable",
    "pole_step_timings_unavailable", "workflow_yaml_unavailable",
    "pole_job_not_matched_in_yaml", "type_aware_lint_scan_missing",
    "type_aware_config_reader_failed",
}
_VERDICTS = {
    "pole_not_file_backed", "no_candidate_step", "tool_not_in_table",
    "target_already_in_use", "tsc_emits_not_in_v1", "eslint_reported_by_opt82",
}


def _tool(row, fit="drop-in", reasons=None):
    leaf, src, tgt, bench = {
        "prettier": ("prettier --check .", "prettier", "oxfmt",
                     ["time npx prettier --check .",
                      "time npx --yes oxfmt@0.47.0 --check ."]),
        "eslint": ("eslint .", "eslint", "oxlint",
                   ["time npx eslint .", "time npx --yes oxlint@1.0.0 ."]),
        "tsc": ("tsc --noEmit", "tsc", "typescript@^7",
                ["time npx tsc --noEmit", "time npx --yes -p typescript@7 tsc --noEmit"]),
    }[row]
    return {"row": row, "leaf_command": leaf, "source_tool": src, "target": tgt,
            "fit": fit, "fit_reasons": list(reasons or []),
            "source_version": "3.3.3", "typescript6_alias_needed": False,
            "benchmark_commands": bench, "docs_url": bp._OPT84_DOCS_URLS[row],
            # Stamped by the engine on every tool, from its own tables.
            "published_range": cr._OPT84_TABLE[row]["published_range"],
            "migration_docs_url": cr._OPT84_DOCS[row]["migration_docs_url"]}


def _tal(**over):
    """The scan's `type_aware_lint` block as a clean read: one flat ESLint config
    at the root with no plugins or local rules, no Prettier plugins, a complete
    walk."""
    b = {"configs": [{"path": "eslint.config.js", "dir": "", "format": "flat",
                      "type_aware": "off", "custom_rules": [], "shared_configs": [],
                      "unresolved_imports": []}],
         "unreadable": [], "truncated": False, "error": None,
         "package_scripts_unreadable": [],
         "eslint_config_facts": {"eslint.config.js": {
             "plugins": [], "local_rules": False, "exhaustive_deps": False}},
         "prettier_plugins": {}}
    b.update(over)
    return b


def _tools_default():
    return [_tool("eslint", "dual-run",
                  ["this config has 2 custom rules oxlint cannot run"]),
            _tool("prettier")]


def _finding(tools=None, fid="f84"):
    tools = _tools_default() if tools is None else tools
    return {
        "id": fid, "pattern": "OPT84",
        "title": "A Slow Step on the Long Pole Runs a Tool That Has a Native Drop-in",
        "fix_strategy": "slow-tool-on-pole-has-native-drop-in",
        "fix_recipe_anchor": "opt84--a-slow-step",
        "pattern_class": "structural", "sizing_basis": "uncredited",
        # The engine's rule: HIGH only for an ESLint row it reports itself
        # (drop-in or dual-run); one deferred to OPT82 does not raise it.
        "severity": "HIGH" if any(t["row"] == "eslint" and t["fit"] != "deferred_to_opt82"
                                  for t in tools) else "MEDIUM",
        "risk": "HIGH" if any(t["row"] == "eslint" and t["fit"] != "deferred_to_opt82"
                              for t in tools) else "MEDIUM",
        "guardrail": "g", "rollout": "r", "workflow_file": _WF,
        "affected_jobs": ["Lint"], "wall_clock_p50_s": None, "runner_min_saving": None,
        "tool_swap": {
            "kind": "opt84_tool_swap", "job": "Lint", "yaml_job": "lint",
            "workflow_file": _WF, "pole_check": "Lint", "job_p50_s": 162.0,
            "step": _STEP, "category": "scan", "step_p50_s": 52.0,
            "share": round(52.0 / 162.0, 3), "working_directory": ".",
            "command": "pnpm lint",
            "resolved_leaves": [t["leaf_command"] for t in tools],
            "tools": tools, "ceiling_s": 52.0,
            "ceiling_basis": "the candidate step's own measured p50",
            "split_unknown": len(tools) > 1, "min_share": 0.0, "min_step_s": 20.0,
        },
    }


def _pole(job="Lint", wf=_WF):
    return {"check": job, "workflow_file": wf, "job": job, "p50_s": 162.0,
            "job_p50_s": 162.0, "dominant_step": "Run tests", "dominant_p50_s": 100.0,
            "steps": [{"step": "Set up job", "category": "setup", "p50_s": 2.0},
                      {"step": _STEP, "category": "scan", "p50_s": 52.0},
                      {"step": "Run tests", "category": "test", "p50_s": 100.0}]}


def _opt82(job="Lint", yaml_job="lint", lint_step=_STEP):
    # The engine joins on OPT82's `type_aware_lint` block (workflow file, YAML
    # job key, lint step), not on the display job: on a matrix lint job OPT82
    # names one matrix run, which need not be the pole's.
    return {"id": "f82", "pattern": "OPT82", "workflow_file": _WF,
            "affected_jobs": [job], "sizing_basis": "uncredited",
            "type_aware_lint": {"kind": "opt82_type_aware_lint", "job": job,
                                "yaml_job": yaml_job, "lint_step": lint_step}}


def _opt83():
    return {"id": "f83", "pattern": "OPT83", "title": "t", "risk": "MEDIUM",
            "workflow_file": _WF, "affected_jobs": ["Lint"],
            "wall_clock_p50_s": None, "runner_min_saving": None,
            "sizing_basis": "uncredited",
            "independent_steps": {"kind": "opt83_independent_steps", "job": "Lint",
                                  "yaml_job": "lint", "ceiling_s": 52.0,
                                  "steps": [{"step": _STEP, "p50_s": 52.0},
                                            {"step": "Run tests", "p50_s": 100.0}]}}


def _doc(findings, poles=None):
    return {"repo": "o/r", "scanned_at": "2026-10-10T00:00:00Z",
            "type_aware_lint": _tal(),
            "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                             "workflows_analyzed": 5},
            "findings": findings,
            "pr_critical_path": {"sampled_pr_count": 20, "sample_target": 20,
                                 "sample_complete": True,
                                 "poles": [_pole()] if poles is None else poles}}


def _card(f=None):
    return "\n".join(bp._opt84_card(f or _finding(), "https://x/catalog.md"))


def _flat(s):
    return " ".join(s.split())


# ---- the card ---------------------------------------------------------------

def test_card_states_step_share_command_tools_ceiling_rail_and_benchmark():
    card = _card()
    text = _flat(card)
    assert card.startswith('<a id="opt84-f84"></a>')
    assert bp._OPT84_SUMMARY == "uncredited, benchmark first"
    assert f"`{_STEP}`" in text and "52s" in text and "32%" in text and "162s" in text
    assert "`pnpm lint`" in text and "`eslint .`" in text and "`prettier --check .`" in text
    for row in ("eslint", "prettier"):
        assert bp._OPT84_DOCS_URLS[row] in text
        assert cr._OPT84_TABLE[row]["published_range"] in text
    assert "run both" in text and "2 custom rules oxlint cannot run" in text
    assert "drop-in" in text
    assert bp._OPT84_SIZING_LABEL in text
    assert bp._OPT84_SPLIT_UNKNOWN in text
    assert bp._OPT84_RAIL in text and bp._OPT84_BENCHMARK in text
    assert "time npx --yes oxfmt@0.47.0 --check ." in text
    assert not re.search(r"\bsaves?\b|min/mo|\*\*Saving", text)
    # Short by owner steer: no migration checklist, no prompt block.
    assert "Prompt for your coding agent" not in card
    assert not hasattr(bp, "_OPT84_MIGRATION_COST")
    assert not hasattr(bp, "_OPT84_TWO_COMMITS")


def test_card_renders_each_tools_stamped_published_range():
    t = _tool("prettier")
    t["published_range"] = "~5-7×"
    text = _flat(_card(_finding([t])))
    assert "published range ~5-7×," in text, text
    assert cr._OPT84_TABLE["prettier"]["published_range"] not in text


def test_card_never_falls_back_to_a_table_range_when_none_is_stamped():
    # `published_range` is a required stamp: the card renders what the engine
    # stamped and nothing else, and the verifier fails a tool without one.
    t = _tool("prettier")
    del t["published_range"]
    text = _flat(_card(_finding([t])))
    assert cr._OPT84_TABLE["prettier"]["published_range"] not in text, text
    assert not hasattr(bp, "_OPT84_PUBLISHED_RANGE")


def test_card_renders_the_migration_guide_beside_the_docs_when_stamped():
    text = _flat(_card(_finding([_tool("eslint"), _tool("prettier"), _tool("tsc")])))
    for row in ("eslint", "prettier"):
        want = (f"docs: {bp._OPT84_DOCS_URLS[row]}; migration guide: "
                f"{cr._OPT84_DOCS[row]['migration_docs_url']}")
        assert want in text, (row, text)
    # tsc has no migration guide: its docs line ends at the docs link.
    tsc_line = next(ln for ln in _card(_finding([_tool("tsc")])).splitlines()
                    if ln.startswith("- **tsc"))
    assert "migration guide" not in tsc_line, tsc_line
    assert tsc_line.endswith(f"docs: {bp._OPT84_DOCS_URLS['tsc']}"), tsc_line


@pytest.mark.parametrize("fit", ["dual-run", "deferred_to_opt82"])
def test_a_row_that_is_not_a_drop_in_never_says_drop_in(fit):
    t = _tool("eslint", fit, ["this config has 2 custom rules oxlint cannot run"]
              if fit == "dual-run" else None)
    line = next(ln for ln in _card(_finding([t])).splitlines()
                if ln.startswith("- **ESLint → oxlint:**"))
    assert "drop-in" not in line, line
    assert "oxlint is the native replacement (published range" in line, line


def test_a_drop_in_row_says_so_in_its_fit_only():
    line = next(ln for ln in _card(_finding([_tool("prettier")])).splitlines()
                if ln.startswith("- **Prettier → oxfmt:**"))
    assert line.count("drop-in") == 1 and "fit: drop-in; docs:" in line, line


@pytest.mark.parametrize("edit,needle", [
    (lambda r: r.replace("oxlint is the native replacement", "oxlint is a native drop-in"),
     "card's 'eslint' row does not name oxlint 'is the native replacement'"),
    (lambda r: "\n".join(ln for ln in r.splitlines()
                         if not ln.startswith("- **Prettier → oxfmt:**")),
     "card has no row for 'prettier'"),
], ids=["drop-in-wording", "row-missing"])
def test_verifier_fails_a_tool_row_that_calls_the_target_a_drop_in(tmp_path, edit, needle):
    doc, report = _doc_and_report()
    c = _check(tmp_path, doc, edit(report))
    assert not c.ok and needle in c.detail, c.detail


@pytest.mark.parametrize("edit,needle", [
    (lambda r: r.replace("fit: run both: this config has 2 custom rules oxlint cannot run;",
                         "fit: drop-in;"),
     "card's 'eslint' fit"),
    (lambda r: r.replace("fit: drop-in;", "fit: run both;"), "card's 'prettier' fit"),
], ids=["dual-run-shown-as-drop-in", "drop-in-shown-as-run-both"])
def test_verifier_fails_a_card_whose_fit_disagrees_with_the_stamp(tmp_path, edit, needle):
    doc, report = _doc_and_report()
    tampered = edit(report)
    assert tampered != report
    c = _check(tmp_path, doc, tampered)
    assert not c.ok and needle in c.detail, c.detail


_FIT_WORD_CASES = [
    {"fit": "drop-in"},
    {"fit": "drop-in", "packages": 2},
    {"fit": "dual-run", "fit_reasons": ["a", "b | c"]},
    {"fit": "dual-run", "fit_reasons": ["x"], "packages": 3},
    {"fit": "deferred_to_opt82"},
    {"fit": "deferred_to_opt82", "packages": 4},
    {"fit": "something new"},
    {"fit": None},
    {"fit": "drop-in", "packages": True},
]


@pytest.mark.parametrize("t", _FIT_WORD_CASES)
def test_verifier_fit_words_equal_the_renderers(t):
    assert vr._vr_opt84_fit_words(t) == bp._opt84_fit_words(t)


def test_one_tool_has_no_split_sentence():
    f = _finding([_tool("prettier")])
    assert bp._OPT84_SPLIT_UNKNOWN not in _flat(_card(f))


def test_deferred_tool_names_opt82_and_alias_adds_one_sentence():
    t = _tool("tsc")
    t["typescript6_alias_needed"] = True
    f = _finding([_tool("eslint", "deferred_to_opt82"), t])
    text = _flat(_card(f))
    assert "see OPT82 first" in text
    assert bp._OPT84_TS6_ALIAS in text
    assert bp._OPT84_TS6_ALIAS not in _flat(_card(_finding([_tool("tsc")])))


def test_string_contracts_carry_the_owner_wording():
    assert bp._OPT84_SIZING_LABEL == (
        "a CEILING, not a forecast: the step's own measured p50 is the most the swap "
        "can remove; the published speedup range is the catalog's, not this repo's "
        "number. Not credited, and not in any total")
    rail = bp._OPT84_RAIL.lower()
    for w in ("rule", "plugin", "file set", "check", "run both"):
        assert w in rail, w
    bench = bp._OPT84_BENCHMARK.lower()
    for w in ("same commit", "runner class", "separate commits"):
        assert w in bench, w


def test_benchmark_line_compares_like_workloads():
    # The target command times the target with its DEFAULT config (oxlint's
    # default rules, oxfmt's defaults) until the repo's config is migrated, so
    # timing it as-is compares unlike workloads and invites an inflated number.
    b = bp._OPT84_BENCHMARK
    assert b.startswith("Benchmark first")
    assert "time both tools on the same commit and the same runner class" in b
    assert "migrated config" in b and "migration guide" in b, b
    assert "defaults" in b, b
    assert "same files" in b and "equivalent rules" in b, b
    assert vr._VR_OPT84_BENCHMARK == b
    assert _flat(b) in _flat(_card())


# ---- placement ----------------------------------------------------------------

def _render(findings, poles=None, monkeypatch=None):
    return bp.render(_doc(findings, poles), {}, {}, {}, "2026-10-10")


def test_card_renders_once_inside_its_pole_section():
    md = _render([_finding()])
    assert md.count('<a id="opt84-f84"></a>') == 1
    secs = [b for w, c, b in vr._pole_header_sections(md) if c == "Lint"]
    assert secs and '<a id="opt84-f84"></a>' in secs[0]
    assert "Tool swaps on other long poles" not in md


def test_card_sits_after_opt83_and_before_opt81(monkeypatch):
    monkeypatch.setattr(bp, "_opt81_findings", lambda fs: [
        f for f in fs if f.get("pattern") == "OPT81"])
    monkeypatch.setattr(bp, "_opt81_for_pole", lambda p, fs: [
        f for f in fs if f.get("pattern") == "OPT81"])
    monkeypatch.setattr(bp, "_opt81_card", lambda f, u, **k: [
        '<a id="opt81-f81"></a>', "", "OPT81 card", ""])
    f81 = {"id": "f81", "pattern": "OPT81", "workflow_file": _WF,
           "affected_jobs": ["Lint"], "advisory": True}
    md = _render([f81, _finding(), _opt83()])
    a83, a84, a81 = (md.index(f'<a id="opt8{n}-f8{n}"></a>') for n in (3, 4, 1))
    assert a83 < a84 < a81, (a83, a84, a81)


def test_unrendered_pole_gets_its_own_section():
    other = _pole(job="Other")
    other["p50_s"] = 400.0
    f = _finding()
    lines = bp._opt84_unrouted_block([f], "https://x/c.md")
    assert "## 🧰 Tool swaps on other long poles" in lines
    assert bp._opt84_unrouted_block([], "u") == []
    assert bp._opt84_for_pole(_pole(), [f]) == [f]
    assert bp._opt84_for_pole(_pole(job="Other"), [f]) == []
    assert bp._opt84_for_pole(_pole(wf=".github/workflows/ci.yml"), [f]) == []
    md = _render([f], poles=[other])
    assert md.count('<a id="opt84-f84"></a>') == 1
    at = md.index('<a id="opt84-f84"></a>')
    head = md[:at].rsplit("\n## ", 1)[1].splitlines()[0]
    assert "Tool swaps on other long poles" in head, head


def test_never_in_also_noticed_tier2_or_totals():
    f = _finding()
    lines, n, _wc = bp._also_noticed_block([f], "https://x/c.md")
    assert (lines, n) == ([], 0)
    assert not bp._is_tier2_finding(f)
    # An OPT82 finding on another job puts an Also noticed section in this
    # render, so the check below reads a real section, never an empty string.
    md = _render([f, _opt82(job="Other")])
    assert '<a id="also-noticed"></a>' in md, md[:2000]
    also = md.split('<a id="also-noticed"></a>', 1)[1]
    head, _sep, body = also.lstrip("\n").partition("\n")
    assert head.startswith("## ") and "Also noticed" in head, head
    also = body.split("\n## ", 1)[0]
    assert "f82" in also or "OPT82" in also, also[:1500]
    # No OPT84 card (OPT82's own prompt names OPT84's table, so the word alone
    # is not the test; the card's anchor and header are).
    assert '<a id="opt84-' not in also and "🧰 OPT84" not in also, also[:1500]


# ---- held-back row -------------------------------------------------------------

def test_held_back_registry_row_and_phrases():
    assert bp._OPT84_WITHHELD_DOC_KEY == vr._VR_OPT84_WITHHELD_DOC_KEY == \
        "opt84_withheld_candidates"
    row = next(r for r in bp._WITHHELD_ROWS if r.doc_key == bp._OPT84_WITHHELD_DOC_KEY)
    assert row.label == "tool swap: held back" and row.entry_shape == "step"
    assert set(bp._OPT84_WITHHOLD_PHRASES) == _HELD_BACK
    assert bp._WITHHELD_PHRASES_BY_KEY[row.doc_key] is bp._OPT84_WITHHOLD_PHRASES
    for g, ph in bp._OPT84_WITHHOLD_PHRASES.items():
        assert ph and "_" not in ph, g


@pytest.mark.parametrize("gate", sorted(_HELD_BACK))
def test_held_back_row_prints_each_gates_phrase_and_the_verifier_agrees(tmp_path, gate):
    key = bp._OPT84_WITHHELD_DOC_KEY
    row = next(r for r in bp._WITHHELD_ROWS if r.doc_key == key)
    on_step = gate not in _POLE_LEVEL
    cand = {"workflow_file": _WF, "job": "Lint", "gate": gate}
    cands = [{**cand, "step": _STEP}] if on_step else [cand]
    line = bp._withheld_candidates_line({key: cands}, key, row.noun)
    named = f"Lint ({_STEP})" if on_step else "Lint"
    assert line == (f"1 {row.noun} held back ({named}): "
                    f"{bp._OPT84_WITHHOLD_PHRASES[gate]}."), line
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({key: cands, "opt84_withheld_by_gate": {gate: 1}}),
                 encoding="utf-8")
    report = f"| {row.label} | {line} | {row.feeds} |\n"
    err, _ = vr._withheld_disclosure_violation(report, p)
    assert err is None, err
    if on_step:
        err, _ = vr._withheld_disclosure_violation(report.replace(f" ({_STEP})", ""), p)
        assert err, "dropping the step from the row must fail"
    # The entry carries `step` exactly when its gate is decided on a step.
    wrong = [cand] if on_step else [{**cand, "step": _STEP}]
    p.write_text(json.dumps({key: wrong, "opt84_withheld_by_gate": {gate: 1}}),
                 encoding="utf-8")
    line = bp._withheld_candidates_line({key: wrong}, key, row.noun)
    err, _ = vr._withheld_disclosure_violation(
        f"| {row.label} | {line} | {row.feeds} |\n", p)
    assert err and "step" in err, (gate, err)


def test_unmatched_pole_job_phrase_names_the_reusable_workflow_case():
    # electron's lint pole is "lint / Lint", produced through a reusable-workflow
    # call, so it is held back here; a matrix job whose `name:` is an expression
    # (curl's windows.yml) fails the same join. The reader is told both shapes.
    want = ("the pole's job could not be tied to one job in its workflow file (a "
            "job produced through a reusable workflow call, or a matrix job whose "
            "name is an expression, for example), so the step's command could not "
            "be read")
    assert bp._OPT84_WITHHOLD_PHRASES["pole_job_not_matched_in_yaml"] == want
    assert vr._VR_OPT84_WITHHOLD_PHRASES["pole_job_not_matched_in_yaml"] == want


def test_held_back_entry_without_a_step_names_the_job():
    key = bp._OPT84_WITHHELD_DOC_KEY
    cands = [{"workflow_file": _WF, "job": "Lint", "gate": "workflow_yaml_unavailable"}]
    line = bp._withheld_candidates_line({key: cands}, key, "n")
    assert line.startswith("1 n held back (Lint): "), line


# ---- coupling ------------------------------------------------------------------

def test_verifier_copies_equal_the_renderers():
    for name in ("SIZING_LABEL", "RAIL", "BENCHMARK", "SUMMARY", "SPLIT_UNKNOWN",
                 "TS6_ALIAS", "DOCS_URLS", "WITHHOLD_PHRASES",
                 "REPLACEMENT", "SOURCE_NAME", "TARGET_NAME"):
        assert getattr(vr, f"_VR_OPT84_{name}") == getattr(bp, f"_OPT84_{name}"), name
    assert vr._VR_OPT84_HELD_BACK_GATES == frozenset(_HELD_BACK)
    assert vr._VR_OPT84_VERDICT_GATES == frozenset(_VERDICTS)
    assert set(vr._VR_OPT84_WITHHOLD_PHRASES) == vr._VR_OPT84_HELD_BACK_GATES
    assert not vr._VR_OPT84_HELD_BACK_GATES & vr._VR_OPT84_VERDICT_GATES
    assert "check_opt84_tool_swap(report, findings_path)," in inspect.getsource(vr)


def test_renderer_and_verifier_url_and_range_tables_equal_the_engines():
    """Both docs keys and the published range: the renderer's tables, the
    verifier's copies and the engine's `_OPT84_DOCS` / `_OPT84_TABLE` agree."""
    engine_docs = {r: d["docs_url"] for r, d in cr._OPT84_DOCS.items()}
    engine_mig = {r: d["migration_docs_url"] for r, d in cr._OPT84_DOCS.items()}
    engine_rng = {r: t["published_range"] for r, t in cr._OPT84_TABLE.items()}
    assert bp._OPT84_DOCS_URLS == vr._VR_OPT84_DOCS_URLS == engine_docs
    assert bp._OPT84_MIGRATION_DOCS_URLS == vr._VR_OPT84_MIGRATION_DOCS_URLS == engine_mig
    assert engine_mig["tsc"] is None
    assert vr._VR_OPT84_PUBLISHED_RANGE == engine_rng


def _engine_fit_vocabulary() -> set[str]:
    """Every string the engine's detector ever assigns to `fit` (a plain
    assignment, the `fit, reasons = ...` pair, or either branch of a ternary),
    read from its source so a new fit word cannot slip past the verifier."""
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(cr._detect_opt84_tool_swap)))
    out: set[str] = set()

    def _strs(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
        elif isinstance(node, ast.IfExp):
            _strs(node.body)
            _strs(node.orelse)

    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign):
            continue
        for tgt in n.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "fit":
                _strs(n.value)
            elif (isinstance(tgt, ast.Tuple) and tgt.elts
                  and isinstance(tgt.elts[0], ast.Name) and tgt.elts[0].id == "fit"
                  and isinstance(n.value, ast.Tuple) and n.value.elts):
                _strs(n.value.elts[0])
    return out


def test_verifier_floors_rows_fits_and_categories_equal_the_engines():
    assert vr._VR_OPT84_MIN_STEP_S == cr._OPT84_MIN_STEP_S
    assert vr._VR_OPT84_MIN_SHARE == cr._OPT84_MIN_SHARE
    assert vr._VR_OPT84_ROWS == set(cr._OPT84_TABLE)
    assert vr._VR_OPT84_CATEGORIES == cr._OPT84_CATEGORIES
    fits = _engine_fit_vocabulary()
    assert fits == {"drop-in", "dual-run", "deferred_to_opt82"}, fits
    assert vr._VR_OPT84_FITS == fits
    assert vr._VR_OPT84_HIGH_FITS == fits - {"deferred_to_opt82"}


def test_engine_copies_equal_the_renderers():
    """A renamed engine constant goes red here (AttributeError), never skipped."""
    assert cr._OPT84_HELD_BACK_GATES == vr._VR_OPT84_HELD_BACK_GATES
    assert cr._OPT84_VERDICT_GATES == vr._VR_OPT84_VERDICT_GATES
    assert cr._OPT84_WITHHELD_DOC_KEY == bp._OPT84_WITHHELD_DOC_KEY
    assert {r: d for r, d in bp._OPT84_DOCS_URLS.items()} == {
        r: (d if isinstance(d, str) else d.get("docs_url"))
        for r, d in cr._OPT84_DOCS.items() if r in bp._OPT84_DOCS_URLS}


def test_grader_seed_classifies_the_check():
    sys.path.insert(0, str(_SKILL_DIR.parents[1] / "maintainers" / "ci-speedup" / "scripts"))
    import grader_seeds as gs
    name = "tool-swap findings re-derive from the pole, stay uncredited and pair with their card"
    assert gs.TRIAGE_ALLOWLIST[name] == gs.AUTO_SEED


# ---- the verifier --------------------------------------------------------------

def _doc_and_report(f=None, extra=None):
    f = f or _finding()
    doc = {"findings": [f, *(extra or [])], "pr_critical_path": {"poles": [_pole()]}}
    head = f"## 🔴 Long pole 1: `lint.yml` ▸ `Lint` — 2m 42s\n\n"
    return doc, head + _card(f) + "\n"


def _check(tmp_path, doc, report):
    # Every doc a real run writes carries the scan's lint-tooling read; a test
    # that is about its absence writes the doc itself (`_check_raw`).
    return _check_raw(tmp_path, {"type_aware_lint": _tal(), **doc}, report)


def _check_raw(tmp_path, doc, report):
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return vr.check_opt84_tool_swap(report, p)


def test_verifier_passes_an_honest_card(tmp_path):
    doc, report = _doc_and_report()
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


def test_verifier_passes_a_deferred_tool_with_its_opt82_finding(tmp_path):
    f = _finding([_tool("eslint", "deferred_to_opt82"), _tool("prettier")])
    doc, report = _doc_and_report(f, extra=[_opt82()])
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


def test_verifier_passes_a_full_render(tmp_path):
    f = _finding()
    md = _render([f])
    c = _check(tmp_path, _doc([f]), md)
    assert c.ok, c.detail


def test_verifier_is_quiet_without_opt84(tmp_path):
    assert _check(tmp_path, {"findings": []}, "# report\n").ok


@pytest.mark.parametrize("edit,needle", [
    (lambda d: d["findings"][0]["tool_swap"].__setitem__("ceiling_s", 80.0),
     "ceiling_s 80.0 != the step's p50 52.0"),
    (lambda d: d["findings"][0]["tool_swap"].__setitem__("step_p50_s", 50.0),
     "!= the pole's measured"),
    (lambda d: d["findings"][0]["tool_swap"]["tools"][1].__setitem__("row", "babel"),
     "row 'babel'"),
    (lambda d: d["findings"][0]["tool_swap"]["tools"][1].__setitem__("fit", "maybe"),
     "fit 'maybe'"),
    (lambda d: d["findings"][0]["tool_swap"]["tools"][1].__setitem__(
        "docs_url", "https://example.test/x"), "docs_url"),
    (lambda d: d["findings"][0]["tool_swap"]["tools"][0].__setitem__(
        "fit", "deferred_to_opt82"), "no OPT82 finding"),
    (lambda d: d["findings"][0]["tool_swap"]["tools"][1].__setitem__(
        "migration_docs_url", "https://example.test/m"),
     "migration_docs_url 'https://example.test/m'"),
    (lambda d: d["findings"][0]["tool_swap"]["tools"][1].__setitem__(
        "migration_docs_url", None), "migration_docs_url None"),
    (lambda d: d["findings"][0]["tool_swap"]["tools"][1].__setitem__(
        "published_range", "~50-90×"), "published_range '~50-90×'"),
    (lambda d: d["findings"][0].__setitem__("wall_clock_p50_s", 52.0), "uncredited"),
    (lambda d: d["findings"][0].__setitem__("sizing_basis", "measured"), "sizing_basis"),
    (lambda d: d["pr_critical_path"].__setitem__("poles", []), "not a drilled long pole"),
    (lambda d: d["findings"][0]["tool_swap"].__setitem__("step", "Nope"),
     "not in the pole's measured steps"),
    (lambda d: d["pr_critical_path"]["poles"][0]["steps"][1].__setitem__("p50_s", 19.0)
     or d["findings"][0]["tool_swap"].update(step_p50_s=19.0, ceiling_s=19.0),
     "does not qualify"),
    (lambda d: d["findings"][0]["tool_swap"].__setitem__("split_unknown", False),
     "split_unknown"),
    (lambda d: d["findings"][0]["tool_swap"].__setitem__("tools", []), "no tool"),
    (lambda d: d.__setitem__("opt84_withheld_by_gate", {"script_unresolvable": 1}),
     "counted 1, listed 0"),
    (lambda d: d.__setitem__("opt84_withheld_candidates", [
        {"workflow_file": _WF, "job": "Lint", "gate": "no_candidate_step"}]),
     "verdict"),
])
def test_verifier_fails_a_tampered_finding(tmp_path, edit, needle):
    doc, report = _doc_and_report()
    edit(doc)
    c = _check(tmp_path, doc, report)
    assert not c.ok and needle in c.detail, c.detail


@pytest.mark.parametrize("edit,needle", [
    (lambda r: r.replace("- **Where:**", "- **Expected:** about 40% faster.\n- **Where:**"),
     "percentage"),
    (lambda r: r.replace("- **Where:**", "- **Expected:** about 45s faster.\n- **Where:**"),
     "duration(s) ['45']"),
    (lambda r: r.replace("- **Where:**", "- **Saving:** a lot\n- **Where:**"),
     "claims a saving"),
    (lambda r: r + r[r.index("<a id"):], "more than once"),
    (lambda r: r.replace(bp._OPT84_SIZING_LABEL, "a guess"), "label"),
    (lambda r: r.replace(bp._OPT84_RAIL, "be careful"), "rail"),
    (lambda r: r.replace(bp._OPT84_BENCHMARK, "go"), "benchmark"),
    (lambda r: r.replace(bp._OPT84_SPLIT_UNKNOWN, ""), "split"),
    (lambda r: r.replace(bp._OPT84_DOCS_URLS["prettier"], "https://example.test/y"),
     "docs link"),
    (lambda r: r.replace("; migration guide: "
                         + cr._OPT84_DOCS["prettier"]["migration_docs_url"], ""),
     "migration guide for 'prettier'"),
    (lambda r: re.sub(r'<a id="opt84-[^"]+"></a>', "", r), "no card"),
    (lambda r: r + '\n<a id="opt84-f99"></a>\n', "no OPT84 finding"),
    (lambda r: r.replace("## 🔴 Long pole 1:", "## 🧹 Also noticed\n\nx\n\n## 🔴 Long pole 1:")
     .replace("`Lint` — 2m 42s\n\n", "`Lint` — 2m 42s\n\n## 🧹 Also noticed\n\n", 1),
     "outside its pole's section"),
])
def test_verifier_fails_a_tampered_card(tmp_path, edit, needle):
    doc, report = _doc_and_report()
    c = _check(tmp_path, doc, edit(report))
    assert not c.ok and needle in c.detail, c.detail


def test_verifier_fails_a_card_in_tier2(tmp_path):
    doc, report = _doc_and_report()
    head = report.split("\n\n", 1)[0]
    moved = report.replace(head, head + "\n\n## 💰 Tier 2: runner-minute savings", 1)
    c = _check(tmp_path, doc, moved)
    assert not c.ok and "outside its pole's section" in c.detail, c.detail


def test_verifier_fails_a_card_after_opt81_on_its_pole(tmp_path):
    doc, report = _doc_and_report()
    at = report.index('<a id="opt84-')
    moved = report[:at] + '<a id="opt81-f81"></a>\n\nOPT81 card\n\n' + report[at:]
    c = _check(tmp_path, doc, moved)
    assert not c.ok and "after its pole's OPT81 card" in c.detail, c.detail


# ---- severity / risk parity with the engine ----------------------------------

_SEV_CASES = [
    ([("eslint", "drop-in")], "HIGH"),
    ([("eslint", "dual-run"), ("prettier", "drop-in")], "HIGH"),
    ([("eslint", "deferred_to_opt82"), ("prettier", "drop-in")], "MEDIUM"),
    ([("prettier", "drop-in")], "MEDIUM"),
    ([("tsc", "drop-in")], "MEDIUM"),
    ([("eslint", "deferred_to_opt82"), ("tsc", "drop-in")], "MEDIUM"),
]


@pytest.mark.parametrize("tools,want", _SEV_CASES)
def test_engine_raises_severity_and_risk_only_for_a_live_eslint_row(tools, want):
    ts = [_tool(r, fit) for r, fit in tools]
    f = cr._opt84_finding(_pole(), _WF, "Lint", "lint", _STEP, "scan", 52.0, 162.0,
                          "", "pnpm lint", [t["leaf_command"] for t in ts], ts, 84)
    assert f["severity"] == f["risk"] == want


@pytest.mark.parametrize("tools,want", _SEV_CASES)
def test_verifier_rederives_the_engines_severity_rule(tmp_path, tools, want):
    # A dual-run carries its reasons, as the engine stamps it.
    f = _finding([_tool(r, fit, ["a custom rule"] if fit == "dual-run" else None)
                  for r, fit in tools])
    f["severity"] = f["risk"] = want
    extra = [_opt82()] if any(fit == "deferred_to_opt82" for _r, fit in tools) else []
    doc, report = _doc_and_report(f, extra=extra)
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail
    other = "MEDIUM" if want == "HIGH" else "HIGH"
    for key in ("severity", "risk"):
        bad = copy.deepcopy(f)
        bad[key] = other
        doc, report = _doc_and_report(bad, extra=extra)
        c = _check(tmp_path, doc, report)
        assert not c.ok and f"{key} {other!r}, but the engine's rule gives {want!r}" in (
            c.detail), (key, c.detail)


def test_verifier_fails_a_card_whose_risk_disagrees_with_the_finding(tmp_path):
    doc, report = _doc_and_report()
    c = _check(tmp_path, doc, report.replace("risk **HIGH**", "risk **MEDIUM**"))
    assert not c.ok and "card states risk" in c.detail, c.detail


def test_verifier_rejects_a_migration_guide_on_the_tsc_row(tmp_path):
    t = _tool("tsc")
    t["migration_docs_url"] = "https://example.test/ts7-migrate"
    doc, report = _doc_and_report(_finding([t]))
    c = _check(tmp_path, doc, report)
    assert not c.ok and "migration_docs_url 'https://example.test/ts7-migrate'" in c.detail, (
        c.detail)


def test_verifier_accepts_names_and_commands_that_look_like_durations(tmp_path):
    f = _finding([_tool("prettier")])
    f["tool_swap"]["command"] = "pnpm run fmt-30s"
    doc, report = _doc_and_report(f)
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


# ---- task-runner expansion: the packages note renders for every fit -----------

_PKG_NOTE = "across 3 workspace packages; task-runner filters not applied"


def _pkg_tool(row, fit, reasons=()):
    # The engine stamps `packages` on a task-runner expansion, whatever the fit;
    # the note is rendered from that count, never carried in `fit_reasons`.
    t = _tool(row, fit, list(reasons))
    t["packages"] = 3
    return t


@pytest.mark.parametrize("tool,extra", [
    (lambda: _pkg_tool("prettier", "drop-in"), []),
    (lambda: _pkg_tool("eslint", "dual-run",
                       ["this config has 2 custom rules oxlint cannot run"]), []),
    (lambda: _pkg_tool("eslint", "deferred_to_opt82"), [_opt82()]),
], ids=["drop-in", "dual-run", "deferred_to_opt82"])
def test_packages_note_renders_once_on_every_fit(tmp_path, tool, extra):
    t = tool()
    f = _finding([t, _tool("tsc")])
    card = _flat(_card(f))
    line = next(x for x in _card(f).splitlines() if x.startswith("- **")
                and "→" in x and t["leaf_command"] in x)
    assert _flat(line).count(_PKG_NOTE) == 1, line
    assert card.count(_PKG_NOTE) == 1, card
    doc, report = _doc_and_report(f, extra=extra)
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


def test_packages_note_is_rendered_from_the_count_when_reasons_lack_it():
    t = _tool("prettier")
    t["packages"] = 1
    line = _flat(_card(_finding([t])))
    assert "across 1 workspace package; task-runner filters not applied" in line


# ---- deferral to OPT82: the engine's join, not the display job ----------------

def test_verifier_passes_a_deferred_row_whose_opt82_names_another_matrix_run(tmp_path):
    # A matrix lint job: OPT82 names the alphabetically first matrix run, the
    # pole is another run of the same YAML job and step. The engine defers on
    # (workflow file, yaml_job, lint_step), so an honest report must pass.
    f = _finding([_tool("eslint", "deferred_to_opt82"), _tool("prettier")])
    doc, report = _doc_and_report(f, extra=[_opt82(job="Lint (node 18)")])
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


def test_verifier_defers_on_the_whitespace_collapsed_step(tmp_path):
    f = _finding([_tool("eslint", "deferred_to_opt82"), _tool("prettier")])
    doc, report = _doc_and_report(f, extra=[_opt82(lint_step=" Run  lint and format checks ")])
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


@pytest.mark.parametrize("o82", [
    lambda: _opt82(yaml_job="other"),
    lambda: _opt82(lint_step="Run something else"),
    lambda: {**_opt82(), "workflow_file": ".github/workflows/ci.yml"},
    lambda: {k: v for k, v in _opt82().items() if k != "type_aware_lint"},
], ids=["other-yaml-job", "other-step", "other-workflow", "no-block"])
def test_verifier_fails_a_deferred_row_whose_opt82_is_on_another_step(tmp_path, o82):
    f = _finding([_tool("eslint", "deferred_to_opt82"), _tool("prettier")])
    doc, report = _doc_and_report(f, extra=[o82()])
    c = _check(tmp_path, doc, report)
    assert not c.ok and "no OPT82 finding" in c.detail, c.detail


# ---- OPT82's prompt points its native-linter option at OPT84 -----------------

def test_opt82_prompt_points_its_native_option_at_opt84_not_the_deleted_opt14_contract():
    # The OPT14 migration-cost contract was removed with OPT84's arrival; the
    # catalog's OPT82 entry now names OPT84's tool-swap table and its
    # no-weakening rail, and the prompt must point where the catalog does.
    m = {"pattern": "OPT82", "workflow_file": _WF, "affected_jobs": ["Lint"],
         "type_aware_lint": {"rules": [{"rule": "@typescript-eslint/no-floating-promises"}],
                             "configs": ["eslint.config.mjs"], "yaml_job": "lint",
                             "lint_step": _STEP}}
    text = _flat("\n".join(bp._opt82_prompt("t", [m], "u", "l")))
    assert "OPT14 migration-cost" not in text and "migration-cost" not in text, text
    opt_c = text.split("(c) ", 1)[1].split("Risk:", 1)[0]
    assert "OPT84's tool-swap table and its no-weakening rail" in opt_c, opt_c


# ---- the step's category: scan or build, and the pole's own -------------------

@pytest.mark.parametrize("ts_cat,pole_cat,needle", [
    ("test", "test", "category 'test' is not one of"),
    ("build", "scan", "category 'build' != the pole step's 'scan'"),
    (None, "scan", "category None is not one of"),
], ids=["not-scan-or-build", "not-the-poles", "missing"])
def test_verifier_fails_a_step_category_the_engine_never_stamps(tmp_path, ts_cat,
                                                                 pole_cat, needle):
    doc, report = _doc_and_report()
    doc["findings"][0]["tool_swap"]["category"] = ts_cat
    doc["pr_critical_path"]["poles"][0]["steps"][1]["category"] = pole_cat
    c = _check(tmp_path, doc, report)
    assert not c.ok and needle in c.detail, c.detail


# ---- negative self-tests: each verifier branch goes red on its own break -------

def _rerendered(edit):
    """A finding edited by `edit`, with its card rendered FROM the edited finding,
    so only the verifier's re-derivation (never a stale card) can catch it."""
    f = _finding()
    edit(f)
    return _doc_and_report(f)


def test_verifier_fails_a_share_that_does_not_re_derive(tmp_path):
    doc, report = _rerendered(lambda f: f["tool_swap"].__setitem__("share", 0.5))
    assert "50% of this 162s job" in _flat(report)
    c = _check(tmp_path, doc, report)
    assert not c.ok and "share 0.5 != the re-derived 0.321" in c.detail, c.detail


def test_verifier_fails_a_job_p50_other_than_the_poles(tmp_path):
    doc, report = _rerendered(lambda f: f["tool_swap"].__setitem__("job_p50_s", 200.0))
    c = _check(tmp_path, doc, report)
    assert not c.ok and "job_p50_s 200.0 != the pole's 162.0" in c.detail, c.detail


def test_verifier_fails_a_card_before_its_poles_opt83_card(tmp_path):
    doc, report = _doc_and_report()
    report += '\n<a id="opt83-f83"></a>\n\nOPT83 card\n'
    c = _check(tmp_path, doc, report)
    assert not c.ok and "card sits before its pole's OPT83 card" in c.detail, c.detail


@pytest.mark.parametrize("edit", [
    lambda r: r.replace(f"published range {cr._OPT84_TABLE['prettier']['published_range']}",
                        "published range ~40-90×"),
    lambda r: r.replace(f"published range {cr._OPT84_TABLE['prettier']['published_range']}, ", ""),
], ids=["altered", "missing"])
def test_verifier_fails_a_card_without_the_rows_published_range(tmp_path, edit):
    doc, report = _doc_and_report()
    c = _check(tmp_path, doc, edit(report))
    assert not c.ok and "missing the published range for 'prettier'" in c.detail, c.detail


def test_verifier_fails_a_finding_naming_one_row_twice(tmp_path):
    f = _finding([_tool("prettier"), _tool("prettier")])
    doc, report = _doc_and_report(f)
    c = _check(tmp_path, doc, report)
    assert not c.ok and "named more than once: ['prettier', 'prettier']" in c.detail, (
        c.detail)


def test_verifier_fails_a_ceiling_line_without_the_stamped_seconds(tmp_path):
    doc, report = _doc_and_report()
    c = _check(tmp_path, doc, report.replace("- **Ceiling:** 52s", "- **Ceiling:** 60s"))
    assert not c.ok and "does not state the 52s ceiling" in c.detail, c.detail


@pytest.mark.parametrize("edit,needle", [
    (lambda d: d.__setitem__("opt84_withheld_by_gate", {"made_up_gate": 1}),
     "counted gate 'made_up_gate' is not an OPT84 gate"),
    (lambda d: d.__setitem__("opt84_withheld_candidates", [
        {"workflow_file": _WF, "job": "Lint", "gate": "made_up_gate"}]),
     "listed gate 'made_up_gate' is not an OPT84 held-back gate"),
], ids=["counted", "listed"])
def test_verifier_fails_a_gate_that_is_not_an_opt84_gate(tmp_path, edit, needle):
    doc, report = _doc_and_report()
    edit(doc)
    c = _check(tmp_path, doc, report)
    assert not c.ok and needle in c.detail, c.detail


def test_verifier_fails_a_runner_minute_saving(tmp_path):
    doc, report = _doc_and_report()
    doc["findings"][0]["runner_min_saving"] = 12.0
    c = _check(tmp_path, doc, report)
    assert not c.ok and "carries runner_min_saving 12.0" in c.detail, c.detail


def test_verifier_fails_a_card_when_the_run_has_no_opt84_finding(tmp_path):
    _doc, report = _doc_and_report()
    c = _check(tmp_path, {"findings": [], "pr_critical_path": {"poles": [_pole()]}}, report)
    assert not c.ok and "the run recorded no OPT84 finding" in c.detail, c.detail


def test_verifier_passes_a_build_step(tmp_path):
    doc, report = _doc_and_report()
    doc["findings"][0]["tool_swap"]["category"] = "build"
    doc["pr_critical_path"]["poles"][0]["steps"][1]["category"] = "build"
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


# ---- the engine's fit contract: what it never emits, the verifier rejects ----

@pytest.mark.parametrize("row", ["prettier", "tsc"])
def test_verifier_rejects_a_deferred_fit_on_a_row_other_than_eslint(tmp_path, row):
    # The engine defers only the ESLint row to OPT82; nothing else is OPT82's.
    f = _finding([_tool("eslint"), _tool(row, "deferred_to_opt82")])
    doc, report = _doc_and_report(f, extra=[_opt82()])
    c = _check(tmp_path, doc, report)
    assert not c.ok and f"tool {row!r} fit 'deferred_to_opt82' on a row other than ESLint" in (
        c.detail), c.detail


def test_verifier_rejects_a_finding_whose_every_tool_is_deferred(tmp_path):
    # All rows deferred is the engine's `eslint_reported_by_opt82` verdict, never
    # a finding.
    f = _finding([_tool("eslint", "deferred_to_opt82")])
    doc, report = _doc_and_report(f, extra=[_opt82()])
    c = _check(tmp_path, doc, report)
    assert not c.ok and "every tool is deferred to OPT82" in c.detail, c.detail


def test_verifier_rejects_a_dual_run_with_no_stamped_reason(tmp_path):
    f = _finding([_tool("eslint", "dual-run", [])])
    doc, report = _doc_and_report(f)
    c = _check(tmp_path, doc, report)
    assert not c.ok and "fit 'dual-run' with no fit_reasons" in c.detail, c.detail


# ---- the TS6 alias sentence follows the tsc row's stamp ----------------------

def _alias_tsc(needed):
    t = _tool("tsc")
    t["typescript6_alias_needed"] = needed
    return t


def test_verifier_passes_a_tsc_row_with_its_alias_sentence(tmp_path):
    doc, report = _doc_and_report(_finding([_alias_tsc(True)]))
    assert vr._VR_OPT84_TS6_ALIAS in _flat(report)
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


def test_verifier_fails_a_tsc_row_missing_its_alias_sentence(tmp_path):
    doc, report = _doc_and_report(_finding([_alias_tsc(True)]))
    c = _check(tmp_path, doc, report.replace(f". {bp._OPT84_TS6_ALIAS}.", ""))
    assert not c.ok and "tsc row's TypeScript 6 alias sentence" in c.detail, c.detail


@pytest.mark.parametrize("needed", [False, None])
def test_verifier_fails_an_alias_sentence_the_stamp_does_not_call_for(tmp_path, needed):
    t = _alias_tsc(needed)
    if needed is None:
        del t["typescript6_alias_needed"]
    doc, report = _doc_and_report(_finding([t]))
    lines = report.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("- **tsc → "))
    lines[i] += f". {bp._OPT84_TS6_ALIAS}."
    c = _check(tmp_path, doc, "\n".join(lines))
    assert not c.ok and "tsc row's TypeScript 6 alias sentence" in c.detail, c.detail


# ---- two matrix legs share a job: the stamped pole_check picks the pole -------

def _leg(check, step_p50, job_p50):
    p = _pole()
    p["check"] = check
    p["p50_s"] = p["job_p50_s"] = job_p50
    p["steps"][1]["p50_s"] = step_p50
    return p


def _leg_finding():
    # Stamped from the SECOND leg: 60s of a 180s job.
    f = _finding([_tool("prettier")])
    f["tool_swap"].update(pole_check="Lint (node 20)", job_p50_s=180.0,
                          step_p50_s=60.0, ceiling_s=60.0,
                          share=round(60.0 / 180.0, 3))
    return f


_LEGS = [_leg("Lint (node 18)", 52.0, 162.0), _leg("Lint (node 20)", 60.0, 180.0)]


def test_verifier_rederives_from_the_stamped_pole_when_legs_share_a_job(tmp_path):
    f = _leg_finding()
    doc = {"findings": [f], "pr_critical_path": {"poles": copy.deepcopy(_LEGS)}}
    report = ("## 🔴 Long pole 1: `lint.yml` ▸ `Lint (node 20)` — 3m 0s\n\n"
              + _card(f) + "\n")
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


def test_verifier_fails_a_card_under_the_other_leg_sharing_its_job(tmp_path):
    f = _leg_finding()
    doc = {"findings": [f], "pr_critical_path": {"poles": copy.deepcopy(_LEGS)}}
    report = ("## 🔴 Long pole 1: `lint.yml` ▸ `Lint (node 18)` — 2m 42s\n\n"
              + _card(f) + "\n"
              "## 🔴 Long pole 2: `lint.yml` ▸ `Lint (node 20)` — 3m 0s\n\n")
    c = _check(tmp_path, doc, report)
    assert not c.ok and "card renders outside its pole's section" in c.detail, c.detail


def test_renderer_joins_a_finding_to_its_stamped_pole_only():
    f = _leg_finding()
    a, b = copy.deepcopy(_LEGS)
    assert bp._opt84_for_pole(a, [f]) == []
    assert bp._opt84_for_pole(b, [f]) == [f]
    # Without a stamped pole_check the join stays the job's.
    g = _finding([_tool("prettier")])
    g["tool_swap"]["pole_check"] = ""
    assert bp._opt84_for_pole(a, [g]) == [g]


# ---- a command that resolves to itself names no "resolves to" clause ---------

def test_card_omits_resolves_to_when_the_leaf_is_the_command_requoted():
    f = _finding([_tool("eslint")])
    f["tool_swap"]["command"] = "eslint '.'"
    f["tool_swap"]["resolved_leaves"] = ["eslint ."]
    text = _flat(_card(f))
    assert "It runs `eslint '.'`." in text, text
    assert "which resolves to" not in text, text


def test_card_keeps_resolves_to_when_the_leaf_differs():
    assert "which resolves to `eslint .`" in _flat(_card(_finding([_tool("eslint")])))


# ---- coupling: the own-section heading the verifier looks for is the renderer's

def test_verifier_own_section_heading_is_the_renderers():
    lines = bp._opt84_unrouted_block([_finding()], "https://x/c.md")
    heading = next(ln for ln in lines if ln.startswith("## "))
    assert vr._VR_OPT84_OWN_SECTION in heading, heading


# ---- the scan's lint-tooling read, required stamps, fit re-derivation ---------

_SCAN_GATE_PHRASES = {
    "type_aware_lint_scan_missing":
        "the scan that wrote this findings file recorded no lint-tooling read, so "
        "the step's tools could not be checked",
    "type_aware_config_reader_failed":
        "the scan's lint-tooling reader failed, so the step's tools could not be "
        "checked",
}


def test_scan_gates_hold_back_in_plain_english_on_both_sides():
    for g, want in _SCAN_GATE_PHRASES.items():
        assert bp._OPT84_WITHHOLD_PHRASES[g] == want, g
        assert vr._VR_OPT84_WITHHOLD_PHRASES[g] == want, g
    assert len(bp._OPT84_WITHHOLD_PHRASES) == len(vr._VR_OPT84_HELD_BACK_GATES) == 17


def test_step_gates_are_every_held_back_gate_but_the_pole_level_ones():
    assert vr._VR_OPT84_STEP_GATES == frozenset(_HELD_BACK - _POLE_LEVEL)
    assert vr._VR_OPT84_STEP_GATES < vr._VR_OPT84_HELD_BACK_GATES


def test_ts6_alias_sentence_says_in_this_repo():
    assert bp._OPT84_TS6_ALIAS.startswith(
        "A type-aware ESLint config in this repo calls the TypeScript compiler API")
    assert vr._VR_OPT84_TS6_ALIAS == bp._OPT84_TS6_ALIAS


def test_packages_note_comes_from_the_count_alone_never_matched_against_reasons():
    # The engine no longer carries the note in `fit_reasons`; the card renders it
    # from `packages` on every fit and does not search the reasons for it.
    t = {"fit": "dual-run", "fit_reasons": ["x", _PKG_NOTE], "packages": 3}
    assert bp._opt84_fit_words(t).count(_PKG_NOTE) == 2
    assert vr._vr_opt84_fit_words(t) == bp._opt84_fit_words(t)


def test_verifier_oxlint_native_plugins_and_major_rule_equal_the_engines():
    assert vr._VR_OPT84_OXLINT_NATIVE_PLUGINS == cr._OPT84_OXLINT_NATIVE_PLUGINS
    for spec in ("^5.6.2", "~7.0", "5.6.2", ">=5", "v6", "6.0.0-beta.1",
                 "npm:@typescript/typescript6@6.0.2", "npm:typescript", "catalog:",
                 "workspace:*", "latest", "", None, "^7.0.2", "5.x"):
        assert vr._vr_opt84_major(spec) == cr._opt84_major(spec), spec
    for p in ("eslint-plugin-react", "@foo/eslint-plugin", "@foo/eslint-plugin-bar",
              "react", " import "):
        assert vr._vr_opt84_plugin_name(p) == cr._opt84_plugin_name(p), p


def _with_tal(**over):
    doc, report = _doc_and_report()
    doc["type_aware_lint"] = _tal(**over)
    return doc, report


def _one(tool, **over):
    """A one-tool finding and its card, with the scan block `_tal(**over)`."""
    f = _finding([tool])
    doc, report = _doc_and_report(f)
    doc["type_aware_lint"] = _tal(**over)
    return doc, report


def test_verifier_fails_a_card_when_the_scan_recorded_no_lint_tooling_read(tmp_path):
    doc, report = _doc_and_report()
    doc.pop("type_aware_lint", None)
    c = _check_raw(tmp_path, doc, report)
    assert not c.ok and "no type_aware_lint block" in c.detail, c.detail


def test_verifier_fails_a_card_stamped_from_a_failed_lint_tooling_read(tmp_path):
    doc, report = _with_tal(error="OSError: boom")
    c = _check_raw(tmp_path, doc, report)
    assert not c.ok and "lint-tooling read that failed" in c.detail, c.detail


@pytest.mark.parametrize("row", ["prettier", "tsc"])
def test_verifier_fails_a_drop_in_stamped_from_a_truncated_walk(tmp_path, row):
    doc, report = _one(_tool(row), truncated=True)
    c = _check_raw(tmp_path, doc, report)
    assert not c.ok and "truncated" in c.detail, c.detail


def test_verifier_passes_a_clean_drop_in_of_each_row(tmp_path):
    for row in ("prettier", "eslint", "tsc"):
        doc, report = _one(_tool(row))
        c = _check_raw(tmp_path, doc, report)
        assert c.ok, (row, c.detail)


def test_verifier_fails_a_tool_without_a_published_range(tmp_path):
    doc, report = _doc_and_report()
    del doc["findings"][0]["tool_swap"]["tools"][1]["published_range"]
    c = _check(tmp_path, doc, report)
    assert not c.ok and "carries no published_range" in c.detail, c.detail


def test_verifier_fails_a_leaf_command_that_is_not_a_resolved_leaf(tmp_path):
    doc, report = _doc_and_report()
    doc["findings"][0]["tool_swap"]["resolved_leaves"] = ["eslint ."]
    c = _check(tmp_path, doc, report)
    assert not c.ok and "leaf_command 'prettier --check .' is not one of" in c.detail, (
        c.detail)


@pytest.mark.parametrize("fit,extra", [("drop-in", []), ("deferred_to_opt82", [_opt82()])])
def test_verifier_fails_fit_reasons_on_a_fit_other_than_dual_run(tmp_path, fit, extra):
    t = _tool("eslint", fit, ["a reason"])
    f = _finding([t, _tool("prettier")])
    doc, report = _doc_and_report(f, extra=extra)
    c = _check(tmp_path, doc, report)
    assert not c.ok and f"fit {fit!r} with fit_reasons" in c.detail, c.detail


@pytest.mark.parametrize("version,ok", [
    ("3.3.3", True), ("^5.6.2", True), ("~6.0", True),
    ("npm:@typescript/typescript6@6.0.2", True),
    ("^7.0.2", False), ("7.1.0", False), ("catalog:", False), ("workspace:*", False),
    (None, False), ("latest", False),
])
def test_verifier_requires_a_readable_pre_7_typescript_version_on_a_tsc_row(
        tmp_path, version, ok):
    t = _tool("tsc")
    t["source_version"] = version
    doc, report = _one(t)
    c = _check_raw(tmp_path, doc, report)
    assert c.ok == ok, (version, c.detail)
    if not ok:
        assert "source_version" in c.detail, c.detail


_CFG = "eslint.config.js"


@pytest.mark.parametrize("over,needle", [
    ({"configs": [{"path": _CFG, "dir": "", "format": "flat", "type_aware": "off",
                   "custom_rules": [{"rule": "local/x", "source": "x.js"}]}]},
     "custom local rules"),
    ({"eslint_config_facts": {_CFG: {"plugins": [], "local_rules": True}}},
     "custom local rules"),
    ({"eslint_config_facts": {_CFG: {"plugins": ["eslint-plugin-react"]}}},
     "plugin react"),
    ({"eslint_config_facts": {_CFG: {"plugins": ["eslint-plugin-foo"]}}},
     "plugin foo"),
    ({"eslint_config_facts": {_CFG: {"plugins": [], "exhaustive_deps": True}}},
     "react-hooks/exhaustive-deps"),
    ({"eslint_config_facts": {}}, "were not read"),
    ({"configs": [{"path": _CFG, "dir": "", "format": "flat", "type_aware": "on"}]},
     "type-aware"),
    ({"configs": [{"path": _CFG, "dir": "", "format": "flat",
                   "type_aware": "unresolved"}]}, "type-aware"),
    ({"configs": []}, "no ESLint config"),
    ({"unreadable": [_CFG], "configs": []}, "could not be read"),
], ids=["custom-rules", "local-rules", "framework-plugin", "unknown-plugin",
        "hooks-rule", "facts-missing", "type-aware-on", "type-aware-unresolved",
        "no-config", "config-unreadable"])
def test_verifier_fails_an_eslint_drop_in_the_stamped_facts_contradict(
        tmp_path, over, needle):
    doc, report = _one(_tool("eslint"), **over)
    c = _check_raw(tmp_path, doc, report)
    assert not c.ok and "ESLint row stamped 'drop-in'" in c.detail \
        and needle in c.detail, c.detail


def test_verifier_passes_an_eslint_drop_in_with_only_native_plugins(tmp_path):
    doc, report = _one(_tool("eslint"), eslint_config_facts={_CFG: {
        "plugins": ["@typescript-eslint", "eslint-plugin-unicorn", "import"],
        "local_rules": False, "exhaustive_deps": False}})
    c = _check_raw(tmp_path, doc, report)
    assert c.ok, c.detail


def test_verifier_reads_the_nearest_flat_config_for_the_leafs_directory(tmp_path):
    # The leaf runs in packages/a (its benchmark pair `cd`s there): the nearest
    # flat config is packages/a's, so the root config's plugin does not apply.
    t = _tool("eslint")
    t["benchmark_commands"] = ["(cd packages/a && time npx eslint .)",
                               "(cd packages/a && time npx --yes oxlint .)"]
    cfgs = [{"path": _CFG, "dir": "", "format": "flat", "type_aware": "off"},
            {"path": "packages/a/eslint.config.js", "dir": "packages/a",
             "format": "flat", "type_aware": "off"}]
    facts = {_CFG: {"plugins": ["eslint-plugin-react"]},
             "packages/a/eslint.config.js": {"plugins": []}}
    doc, report = _one(t, configs=cfgs, eslint_config_facts=facts)
    c = _check_raw(tmp_path, doc, report)
    assert c.ok, c.detail
    facts["packages/a/eslint.config.js"] = {"plugins": ["eslint-plugin-vue"]}
    doc, report = _one(t, configs=cfgs, eslint_config_facts=facts)
    c = _check_raw(tmp_path, doc, report)
    assert not c.ok and "plugin vue" in c.detail, c.detail


@pytest.mark.parametrize("over,needle", [
    ({"prettier_plugins": {"": ["prettier-plugin-tailwindcss"]}},
     "prettier-plugin-tailwindcss"),
    ({"prettier_plugins": None}, "were not read"),
    ({"package_scripts_unreadable": [""]}, "were not read"),
], ids=["plugin-at-root", "plugins-unread", "package-json-unreadable"])
def test_verifier_fails_a_prettier_drop_in_the_stamped_plugins_contradict(
        tmp_path, over, needle):
    doc, report = _one(_tool("prettier"), **over)
    c = _check_raw(tmp_path, doc, report)
    assert not c.ok and "Prettier row stamped 'drop-in'" in c.detail \
        and needle in c.detail, c.detail


@pytest.mark.parametrize("row", ["prettier", "tsc"])
def test_verifier_fails_a_drop_in_from_a_block_with_no_truncated_key(tmp_path, row):
    doc, report = _one(_tool(row))
    doc["type_aware_lint"].pop("truncated")
    c = _check_raw(tmp_path, doc, report)
    assert not c.ok and "truncated" in c.detail, c.detail


def _in_dir(t, wd):
    t["benchmark_commands"] = [f"(cd {wd} && {c})" for c in t["benchmark_commands"]]
    return t


@pytest.mark.parametrize("wd,unread", [
    ("", {"": ["prettier.config.js"]}),
    ("", {"docs": ["docs/.prettierrc.mjs"]}),
    ("web", {"": [".prettierrc.toml"]}),
    ("web", {"web": ["web/prettier.config.ts"]}),
], ids=["root-at-root", "root-run-below", "nested-above", "nested-own"])
def test_verifier_fails_a_prettier_drop_in_over_an_unopened_config(tmp_path, wd, unread):
    t = _tool("prettier")
    doc, report = _one(_in_dir(t, wd) if wd else t, prettier_config_unreadable=unread)
    c = _check_raw(tmp_path, doc, report)
    path = next(iter(unread.values()))[0]
    assert not c.ok and "Prettier row stamped 'drop-in'" in c.detail \
        and path in c.detail, c.detail


def test_verifier_passes_a_nested_prettier_drop_in_beside_a_sibling_unopened_config(
        tmp_path):
    doc, report = _one(_in_dir(_tool("prettier"), "web"),
                       prettier_config_unreadable={"docs": ["docs/.prettierrc.mjs"]})
    c = _check_raw(tmp_path, doc, report)
    assert c.ok, c.detail


def _leaf(t, leaf):
    t["leaf_command"] = leaf
    return t


def test_verifier_fails_a_prettier_drop_in_whose_command_loads_a_plugin(tmp_path):
    t = _leaf(_tool("prettier"), "prettier --check --plugin=prettier-plugin-x .")
    doc, report = _one(t)
    c = _check_raw(tmp_path, doc, report)
    assert not c.ok and "--plugin" in c.detail, c.detail


@pytest.mark.parametrize("flag", ["--rulesdir rules", "--plugin acme", "--rule x:2",
                                  "--parser babel", "--ext .ts",
                                  "--parser-options=project:true"])
def test_verifier_fails_an_eslint_drop_in_whose_command_loads_rules(tmp_path, flag):
    t = _leaf(_tool("eslint"), f"eslint {flag} .")
    doc, report = _one(t)
    c = _check_raw(tmp_path, doc, report)
    name = flag.split("=", 1)[0].split()[0]
    assert not c.ok and "ESLint row stamped 'drop-in'" in c.detail \
        and name in c.detail, c.detail


def test_verifier_eslint_rule_flags_equal_the_engines():
    assert vr._VR_OPT84_ESLINT_RULE_FLAGS == frozenset(cr._OPT84_ESLINT_RULE_FLAGS)


def test_verifier_passes_a_prettier_drop_in_whose_plugins_are_off_its_walk(tmp_path):
    doc, report = _one(_tool("prettier"),
                       prettier_plugins={"packages/b": ["prettier-plugin-svelte"]})
    c = _check_raw(tmp_path, doc, report)
    assert c.ok, c.detail

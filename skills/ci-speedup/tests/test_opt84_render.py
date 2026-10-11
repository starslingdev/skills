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
        assert bp._OPT84_PUBLISHED_RANGE[row] in text
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
    assert bp._OPT84_PUBLISHED_RANGE["prettier"] not in text


def test_card_falls_back_to_the_table_range_when_none_is_stamped():
    t = _tool("prettier")
    del t["published_range"]
    text = _flat(_card(_finding([t])))
    assert f"published range {bp._OPT84_PUBLISHED_RANGE['prettier']}," in text, text


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
    md = _render([f])
    also = md.split("Also noticed", 1)[1] if "Also noticed" in md else ""
    assert "opt84" not in also and "OPT84" not in also


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
    cands = [{"workflow_file": _WF, "job": "Lint", "gate": gate, "step": _STEP}]
    line = bp._withheld_candidates_line({key: cands}, key, row.noun)
    assert line == (f"1 {row.noun} held back (Lint ({_STEP})): "
                    f"{bp._OPT84_WITHHOLD_PHRASES[gate]}."), line
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({key: cands, "opt84_withheld_by_gate": {gate: 1}}),
                 encoding="utf-8")
    report = f"| {row.label} | {line} | {row.feeds} |\n"
    err, _ = vr._withheld_disclosure_violation(report, p)
    assert err is None, err
    err, _ = vr._withheld_disclosure_violation(report.replace(f" ({_STEP})", ""), p)
    assert err, "dropping the step from the row must fail"


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
                 "TS6_ALIAS", "DOCS_URLS", "PUBLISHED_RANGE", "WITHHOLD_PHRASES"):
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
    assert bp._OPT84_PUBLISHED_RANGE == vr._VR_OPT84_PUBLISHED_RANGE == engine_rng


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
    f = _finding([_tool(r, fit) for r, fit in tools])
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
    # The engine stamps `packages` and appends the note to `fit_reasons` on a
    # task-runner expansion, whatever the fit.
    t = _tool(row, fit, [*reasons, _PKG_NOTE])
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


def test_verifier_passes_a_build_step(tmp_path):
    doc, report = _doc_and_report()
    doc["findings"][0]["tool_swap"]["category"] = "build"
    doc["pr_critical_path"]["poles"][0]["steps"][1]["category"] = "build"
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail

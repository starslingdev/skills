"""OPT83 — Independent Steps on the Long Pole Run One After Another.

Unit tests for the detector (`collect_runs._detect_opt83_parallel_steps`), its
card (`blocking_path._opt83_card`) and its verifier check
(`verify_report.check_opt83_parallel_steps`), plus the coupling pins between the
three files. The end-to-end firing case, its withhold twin and the crash
tripwire live in test_offline_pipeline_e2e.py.
"""
from __future__ import annotations

import copy
import inspect
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
sys.path.insert(0, str(_SKILL_DIR / "tests"))
import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import verify_report as vr  # noqa: E402

_WF = ".github/workflows/quality.yml"

_BASE_YAML = """
name: Quality
on: pull_request
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - run: npm ci
      - name: Lint
        run: npx biome check .
      - name: Typecheck
        run: npx tsc --noEmit
      - name: Unit tests
        run: npm test
"""

_STEPS = [("Lint", "scan", 30.0), ("Typecheck", "scan", 25.0),
          ("Unit tests", "test", 25.0), ("Run npm ci", "install", 12.0),
          ("Checkout", "checkout", 3.0), ("Set up job", "setup", 1.0)]


def _pole(steps=None, job="quality", wf=_WF, job_p50=None):
    steps = _STEPS if steps is None else steps
    return {"check": job, "workflow_file": wf, "job": job,
            "job_p50_s": job_p50 if job_p50 is not None else sum(d for _n, _c, d in steps),
            "steps": [{"step": n, "category": c, "p50_s": d} for n, c, d in steps]}


def _run(yaml_text=_BASE_YAML, pole=None, docs=None):
    withheld: dict[str, int] = {}
    cands: list[dict] = []
    wf_docs = docs if docs is not None else {_WF: yaml.safe_load(yaml_text)}
    out = cr._detect_opt83_parallel_steps(
        [pole or _pole()], wf_docs, {_WF: {"job_runner": {"quality": "ubuntu-latest"}}},
        40, withheld=withheld, withheld_candidates=cands)
    return out, withheld, cands


# ---- firing -----------------------------------------------------------------

def test_fires_on_independent_compute_steps_with_an_uncredited_upper_bound():
    out, withheld, cands = _run()
    assert len(out) == 1, withheld
    f = out[0]
    st = f["independent_steps"]
    assert [s["step"] for s in st["steps"]] == ["Lint", "Typecheck", "Unit tests"]
    assert st["ceiling_s"] == 50.0
    assert f["wall_clock_p50_s"] is None and f["runner_min_saving"] is None
    assert f["sizing_basis"] == "uncredited" and f["realization"] == "none"
    assert f["id"] == "f41" and f["pattern"] == "OPT83" and f["risk"] == "MEDIUM"
    assert set(st["independence"]) == set(cr._OPT83_INDEPENDENCE_KEYS)
    assert all(st["independence"].values())
    # Only what a reader uses is stamped: no runner label or runner versions.
    assert not {"runner_label", "self_hosted_min_runner",
                "self_hosted_min_runner_cancel"} & set(st)
    assert set(st["steps"][0]) == {"step", "category", "p50_s", "working_directory"}
    assert withheld == {} and cands == []
    assert "OPT83" not in cr._SIZING
    assert cr._RM_DOOR_OVERRIDES["OPT83"][0] == cr._RM_DOOR_NOT_DERIVABLE


def test_ceiling_is_sum_minus_max():
    assert cr._opt83_ceiling([30.0, 25.0, 25.0]) == 50.0
    assert cr._opt83_ceiling([40.0, 40.0]) == 40.0
    assert cr._opt83_ceiling([40.0]) == 0.0


@pytest.mark.parametrize("p50,ok", [(20.0, True), (19.9, False)])
def test_the_twenty_second_floor_is_inclusive(p50, ok):
    # job p50 100 so the 15% share (15s) never binds.
    assert cr._opt83_step_qualifies("Lint", "scan", p50, 100.0) is ok


@pytest.mark.parametrize("p50,ok", [(30.0, True), (29.9, False)])
def test_the_share_floor_is_inclusive(p50, ok):
    assert cr._opt83_step_qualifies("Lint", "scan", p50, 200.0) is ok


@pytest.mark.parametrize("name,cat", [
    ("Install", "install"), ("Checkout", "checkout"), ("Set up job", "setup"),
    ("Post Run actions/cache@v4", "post"), ("Wait for server", "test"),
    ("sleep 30 then test", "test"), ("wait-all", "other"), ("Docs", "other")])
def test_non_compute_steps_never_qualify(name, cat):
    assert cr._opt83_step_qualifies(name, cat, 60.0, 100.0) is False


@pytest.mark.parametrize("cat", ["build", "test", "scan", "package"])
def test_every_compute_category_qualifies(cat):
    assert cr._opt83_step_qualifies("Work", cat, 60.0, 100.0) is True


def test_one_qualifying_step_is_a_verdict_never_listed():
    out, withheld, cands = _run(pole=_pole([("Unit tests", "test", 140.0),
                                            ("Run npm ci", "install", 40.0)]))
    assert out == [] and withheld == {"fewer_than_two_qualifying_steps": 1}
    assert cands == []


def test_the_largest_run_wins_when_two_runs_qualify():
    y = _BASE_YAML + """      - name: Upload
        uses: actions/upload-artifact@v4
        with: {name: x, path: y}
      - name: Build docs
        run: make docs
      - name: Build site
        run: make site
"""
    steps = list(_STEPS) + [("Upload", "other", 5.0), ("Build docs", "build", 60.0),
                            ("Build site", "build", 60.0)]
    # Separate trees are needed for two builds; they share the root here, so the
    # docs/site run is held back... unless it is not the chosen run. The
    # Lint/Typecheck/Tests run's bound (50s) is below docs/site's (60s), so the
    # docs/site run is chosen and held back for sharing a build tree.
    out, withheld, _ = _run(y, _pole(steps, job_p50=200.0))
    assert out == [] and withheld == {"siblings_share_a_build_tree": 1}
    y2 = y.replace("run: make docs", "run: make docs\n        working-directory: docs"
                   ).replace("run: make site", "run: make site\n        working-directory: site")
    out, withheld, _ = _run(y2, _pole(steps, job_p50=200.0))
    assert [s["step"] for s in out[0]["independent_steps"]["steps"]] == [
        "Build docs", "Build site"], withheld


def test_a_matrix_leg_matches_its_yaml_job():
    y = _BASE_YAML.replace("  quality:\n", "  quality:\n    name: quality\n")
    out, withheld, _ = _run(y, _pole(job="quality (node 20)"))
    assert len(out) == 1, withheld
    st = out[0]["independent_steps"]
    assert (st["job"], st["yaml_job"]) == ("quality (node 20)", "quality")


# ---- adjacency ----------------------------------------------------------------

def test_a_work_step_between_candidates_breaks_the_run():
    y = _BASE_YAML.replace("      - name: Typecheck\n",
                           "      - name: Generate\n        run: npm run gen\n"
                           "      - name: Typecheck\n")
    steps = [s for s in _STEPS if s[0] != "Unit tests"] + [("Generate", "other", 2.0)]
    out, withheld, cands = _run(y, _pole(steps))
    assert out == [] and withheld == {"qualifying_steps_not_adjacent": 1}
    assert cands == []


def test_a_setup_step_between_candidates_breaks_the_run():
    """A setup action changes the toolchain the steps after it run on: grouping
    across it would move the earlier step onto a different setup."""
    y = _BASE_YAML.replace("      - name: Typecheck\n",
                           "      - uses: actions/setup-node@v4\n      - name: Typecheck\n")
    out, withheld, _ = _run(y)
    assert [s["step"] for s in out[0]["independent_steps"]["steps"]] == [
        "Typecheck", "Unit tests"], withheld


def test_a_checkout_between_candidates_breaks_the_run():
    y = _BASE_YAML.replace("      - name: Typecheck\n",
                           "      - name: Checkout again\n        uses: actions/checkout@v4\n"
                           "      - name: Typecheck\n")
    steps = [s for s in _STEPS if s[0] != "Unit tests"]
    out, withheld, _ = _run(y, _pole(steps))
    assert out == [] and withheld == {"qualifying_steps_not_adjacent": 1}


def test_steps_already_in_a_parallel_group_are_never_candidates():
    y = _BASE_YAML.replace(
        "      - name: Lint\n        run: npx biome check .\n"
        "      - name: Typecheck\n        run: npx tsc --noEmit\n",
        "      - parallel:\n"
        "          - name: Lint\n            run: npx biome check .\n"
        "          - name: Typecheck\n            run: npx tsc --noEmit\n")
    steps = [s for s in _STEPS if s[0] != "Unit tests"]
    out, withheld, cands = _run(y, _pole(steps))
    assert out == [] and withheld == {"steps_already_run_in_parallel": 1}
    assert cands == []


def test_a_background_step_is_never_a_candidate():
    y = _BASE_YAML.replace("        run: npx tsc --noEmit\n",
                           "        run: npx tsc --noEmit\n        background: true\n")
    steps = [s for s in _STEPS if s[0] != "Unit tests"]
    out, withheld, _ = _run(y, _pole(steps))
    assert out == [] and withheld == {"steps_already_run_in_parallel": 1}


# ---- independence: every check fails closed -----------------------------------

def _held(y, gate, steps=None):
    out, withheld, cands = _run(y, _pole(steps) if steps else None)
    assert out == [], out
    assert withheld == {gate: 1}, withheld
    # A gate decided on the chosen run also names its steps (checked on its own).
    assert [{k: v for k, v in c.items() if k != "steps"} for c in cands] == [
        {"workflow_file": _WF, "job": "quality", "gate": gate}]


def test_a_sibling_reading_another_siblings_output_is_held_back():
    y = _BASE_YAML.replace("      - name: Lint\n", "      - name: Lint\n        id: lint\n"
                           ).replace("run: npx tsc --noEmit",
                                     "run: npx tsc --noEmit ${{ steps.lint.outputs.x }}")
    _held(y, "sibling_reads_another_siblings_output")


@pytest.mark.parametrize("var", ["GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH"])
def test_a_non_last_sibling_writing_env_output_or_path_is_held_back(var):
    y = _BASE_YAML.replace("run: npx biome check .",
                           f"run: npx biome check . && echo a=b >> \"${var}\"")
    _held(y, "sibling_writes_env_output_or_path")


def test_the_last_sibling_may_write_github_output():
    # Its write is visible after the group's wait, before any later step.
    y = _BASE_YAML.replace("run: npm test", "run: npm test && echo a=b >> \"$GITHUB_OUTPUT\"")
    out, withheld, _ = _run(y)
    assert [s["step"] for s in out[0]["independent_steps"]["steps"]] == [
        "Lint", "Typecheck", "Unit tests"], withheld


def test_a_runtime_expression_in_a_command_is_held_back():
    y = _BASE_YAML.replace("run: npm test", "run: npm test -- --shard ${{ matrix.shard }}")
    _held(y, "step_uses_runtime_expression")


def test_a_runtime_working_directory_is_held_back():
    y = _BASE_YAML.replace("run: npm test",
                           "run: npm test\n        working-directory: ${{ inputs.dir }}")
    _held(y, "step_uses_runtime_expression")


@pytest.mark.parametrize("cmd", ["cd web && npm test", "pushd web; npm test"])
def test_a_directory_change_is_held_back(cmd):
    _held(_BASE_YAML.replace("run: npm test", f"run: {cmd}"), "step_changes_directory")


def test_an_action_candidate_is_held_back():
    y = _BASE_YAML.replace("        run: npx tsc --noEmit\n",
                           "        uses: ./.github/actions/typecheck\n")
    _held(y, "sibling_is_an_action")


def test_two_candidates_on_one_cache_are_held_back():
    y = _BASE_YAML.replace(
        "        run: npx biome check .\n",
        "        uses: actions/cache@v4\n        with: {path: .cache, key: k}\n").replace(
        "        run: npx tsc --noEmit\n",
        "        uses: actions/cache@v4\n        with: {path: .cache, key: k2}\n")
    _held(y, "siblings_share_a_cache_or_artifact")


def test_a_build_and_a_later_step_in_one_tree_are_held_back():
    y = _BASE_YAML.replace("run: npx biome check .", "run: npm run build")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    _held(y, "siblings_share_a_build_tree", steps)


def test_a_build_in_its_own_tree_is_independent():
    # Disjoint trees: the build in `docs`, the other steps in `app` (job default).
    y = _BASE_YAML.replace("run: npx biome check .",
                           "run: npm run build\n        working-directory: docs").replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    defaults:\n      run:\n        working-directory: app\n")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    out, withheld, _ = _run(y, _pole(steps))
    assert len(out) == 1, withheld
    assert [s["working_directory"] for s in out[0]["independent_steps"]["steps"]] == [
        "docs", "app", "app"]


@pytest.mark.parametrize("run,writes", [
    ("npx tsc --noEmit", False), ("npx tsc -p . --noEmit", False), ("npx tsc", True),
    ("npx tsc --noEmit && npm run build", True), ("npm test\nnpm run build", True),
    ("npx biome check .", False)])
def test_build_output_is_read_from_every_command_line(run, writes):
    assert cr._opt83_may_write_build_output("scan", {"run": run}) is writes


def test_a_step_level_condition_is_held_back():
    y = _BASE_YAML.replace("        run: npm test\n",
                           "        if: success()\n        run: npm test\n")
    _held(y, "step_condition_depends_on_order")


def test_a_measured_step_missing_from_the_yaml_is_held_back():
    y = _BASE_YAML.replace("name: Typecheck", "name: Types")
    _held(y, "measured_step_not_matched_in_yaml")


def test_two_yaml_steps_with_one_name_are_held_back():
    y = _BASE_YAML + "      - name: Lint\n        run: npx biome lint .\n"
    _held(y, "measured_step_not_matched_in_yaml")


def test_an_unmatched_job_is_held_back():
    _held(_BASE_YAML.replace("  quality:\n", "  other:\n"), "pole_job_not_matched_in_yaml")


def test_an_unreadable_workflow_is_held_back():
    out, withheld, cands = _run(docs={})
    assert out == [] and withheld == {"workflow_yaml_unavailable": 1}
    assert cands[0]["gate"] == "workflow_yaml_unavailable"


def test_a_pole_without_step_timings_is_held_back():
    p = _pole()
    p.pop("steps")
    out, withheld, cands = _run(pole=p)
    assert out == [] and withheld == {"pole_step_timings_unavailable": 1}
    assert cands[0]["gate"] == "pole_step_timings_unavailable"


def test_a_fileless_pole_is_a_verdict():
    out, withheld, cands = _run(pole={"check": "CodeQL", "p50_s": 300.0})
    assert out == [] and withheld == {"pole_not_file_backed": 1} and cands == []


# ---- registry and coupling pins -----------------------------------------------

def test_every_gate_the_detector_records_is_classified_and_phrased():
    src = inspect.getsource(cr._detect_opt83_parallel_steps)
    gates = set(re.findall(r'_no\("([a-z_]+)"', src))
    assert gates == cr._OPT83_VERDICT_GATES | cr._OPT83_HELD_BACK_GATES, gates
    assert not cr._OPT83_VERDICT_GATES & cr._OPT83_HELD_BACK_GATES
    assert set(bp._OPT83_WITHHOLD_PHRASES) == cr._OPT83_HELD_BACK_GATES
    assert vr._VR_OPT83_WITHHOLD_PHRASES == bp._OPT83_WITHHOLD_PHRASES


def test_doc_key_and_registry_row_are_one_contract():
    assert (cr._OPT83_WITHHELD_DOC_KEY == bp._OPT83_WITHHELD_DOC_KEY
            == vr._VR_OPT83_WITHHELD_DOC_KEY == "opt83_withheld_candidates")
    assert any(r.doc_key == bp._OPT83_WITHHELD_DOC_KEY and r.entry_shape == "job"
               for r in bp._WITHHELD_ROWS)
    assert bp._WITHHELD_PHRASES_BY_KEY[bp._OPT83_WITHHELD_DOC_KEY] is bp._OPT83_WITHHOLD_PHRASES


def test_the_verifier_candidate_rule_is_a_verbatim_copy():
    def body(fn):
        lines = inspect.getsource(fn).splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.rstrip().endswith('"""')
                     and i > 0) + 1
        return "\n".join(lines[start:]).replace("_VR_", "_")
    assert body(cr._opt83_step_qualifies) == body(vr._vr_opt83_step_qualifies)
    assert vr._VR_OPT83_MIN_SHARE == cr._OPT83_MIN_SHARE
    assert vr._VR_OPT83_MIN_STEP_S == cr._OPT83_MIN_STEP_S
    assert vr._VR_OPT83_COMPUTE_CATEGORIES == cr._OPT83_COMPUTE_CATEGORIES
    assert vr._VR_OPT83_NOT_COMPUTE_RE.pattern == cr._OPT83_NOT_COMPUTE_RE.pattern
    assert vr._VR_NON_WORK_STEP_RE.pattern == cr._NON_WORK_STEP_RE.pattern
    assert vr._VR_OPT83_INDEPENDENCE_KEYS == cr._OPT83_INDEPENDENCE_KEYS


def test_card_strings_are_one_contract():
    assert vr._VR_OPT83_SIZING_LABEL == bp._OPT83_SIZING_LABEL
    assert vr._VR_OPT83_RAIL == bp._OPT83_RAIL
    assert vr._VR_OPT83_RUNNER_CAVEAT == bp._OPT83_RUNNER_CAVEAT


def test_scan_reports_opt83_as_routed():
    import scan
    src = inspect.getsource(scan.scan)
    assert '"OPT83"' in src.split("structural_without_detector")[0]


# ---- the card -------------------------------------------------------------------

def _finding():
    return _run()[0][0]


def test_card_carries_the_bound_rail_runner_caveat_and_no_saving():
    lines = bp._opt83_card(_finding(), "https://x/catalog.md")
    text = " ".join("\n".join(lines).split())
    assert '<a id="opt83-f41"></a>' in text
    assert "up to 50s sooner" in text and bp._OPT83_SIZING_LABEL in text
    assert bp._OPT83_RAIL in text and bp._OPT83_RUNNER_CAVEAT in text
    assert "2.335.0" in text and "Enterprise Server" in text
    assert "#### 🤖 Prompt for your coding agent (OPT83)" in "\n".join(lines)
    assert not re.search(r"\bsaves?\b|min/mo", text)


def test_unrouted_findings_get_their_own_section():
    lines = bp._opt83_unrouted_block([_finding()], "https://x/c.md")
    assert "## 🔀 Independent steps on other long poles" in lines
    assert bp._opt83_unrouted_block([], "u") == []


def test_opt83_never_renders_in_also_noticed():
    f = _finding()
    lines, n, _wc = bp._also_noticed_block([f], "https://x/c.md")
    assert lines == [] and n == 0


def test_opt83_joins_only_its_own_pole():
    f = _finding()
    assert bp._opt83_for_pole(_pole(), [f]) == [f]
    assert bp._opt83_for_pole(_pole(job="other"), [f]) == []
    assert bp._opt83_for_pole(_pole(wf=".github/workflows/ci.yml"), [f]) == []


# ---- the verifier ---------------------------------------------------------------

def _head(pole, n=1):
    """A pole heading as the renderer writes it: the card sits in this section."""
    return (f"## 🔴 Long pole {n}: `{Path(pole['workflow_file']).name}` ▸ "
            f"`{pole['check']}` — 1m 35s\n\n")


def _doc_and_report(f=None):
    f = f or _finding()
    doc = {"findings": [f], "pr_critical_path": {"poles": [_pole()]}}
    report = _head(_pole()) + "\n".join(bp._opt83_card(f, "https://x/c.md")) + "\n"
    return doc, report


def _check(tmp_path, doc, report):
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return vr.check_opt83_parallel_steps(report, p)


def test_verifier_passes_an_honest_finding(tmp_path):
    doc, report = _doc_and_report()
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


@pytest.mark.parametrize("edit,needle", [
    (lambda d: d["findings"][0].__setitem__("wall_clock_p50_s", 50.0), "uncredited"),
    (lambda d: d["findings"][0].__setitem__("runner_min_saving", 12.0), "runner_min_saving"),
    (lambda d: d["findings"][0]["independent_steps"].__setitem__("ceiling_s", 80.0),
     "ceiling_s 80.0 != re-derived 50.0"),
    (lambda d: d["findings"][0]["independent_steps"]["steps"][0].__setitem__("p50_s", 31.0),
     "!= the pole's measured"),
    (lambda d: d["findings"][0]["independent_steps"]["independence"].pop("no_step_output_reads"),
     "independence facts"),
    (lambda d: d["pr_critical_path"].__setitem__("poles", []), "not a drilled long pole"),
    (lambda d: d["findings"][0]["independent_steps"]["steps"].append(
        {"step": "Run npm ci", "p50_s": 12.0}), "does not qualify"),
])
def test_verifier_fails_a_tampered_finding(tmp_path, edit, needle):
    doc, report = _doc_and_report()
    edit(doc)
    c = _check(tmp_path, doc, report)
    assert not c.ok and needle in c.detail, c.detail


@pytest.mark.parametrize("edit,needle", [
    (lambda r: r.replace("up to 50s sooner", "up to 80s sooner"), "the upper bound"),
    (lambda r: r.replace("- **Rail:**", "- **Note:**").replace(bp._OPT83_RAIL, "Be careful"),
     "the parallel: rail"),
    (lambda r: r.replace(bp._OPT83_RUNNER_CAVEAT, "Works everywhere"), "runner caveat"),
    (lambda r: r.replace("- **Where:**", "- **Expected:** about 45s faster.\n- **Where:**"),
     "duration(s) ['45']"),
    (lambda r: r.replace("- **Where:**", "- **Saving:** a lot\n- **Where:**"),
     "claims a saving"),
    (lambda r: re.sub(r'<a id="opt83-[^"]+"></a>', "", r), "no card"),
    (lambda r: r + '\n<a id="opt83-f99"></a>\n', "no OPT83 finding"),
])
def test_verifier_fails_a_tampered_card(tmp_path, edit, needle):
    doc, report = _doc_and_report()
    c = _check(tmp_path, doc, edit(report))
    assert not c.ok and needle in c.detail, c.detail


def test_verifier_fails_an_orphan_card_with_no_finding(tmp_path):
    doc, report = _doc_and_report()
    doc["findings"] = []
    c = _check(tmp_path, doc, report)
    assert not c.ok and "no OPT83 finding" in c.detail


def test_verifier_is_skipped_quietly_without_opt83(tmp_path):
    c = _check(tmp_path, {"findings": []}, "# report\n")
    assert c.ok


# ---- review regressions: order barriers, separators, shared trees, repeats ------

def test_a_wait_all_between_candidates_breaks_the_run():
    """A control step runs nothing, but it is an ORDER barrier: a step after a
    `wait-all` must stay after it, so it cannot join a step before it."""
    y = _BASE_YAML.replace(
        "      - name: Lint\n",
        "      - name: Prepare fixtures\n        run: ./prep.sh\n        background: true\n"
        "      - name: Lint\n").replace(
        "      - name: Unit tests\n", "      - wait-all: true\n      - name: Unit tests\n")
    steps = [s for s in _STEPS if s[0] != "Typecheck"]
    y = y.replace("      - name: Typecheck\n        run: npx tsc --noEmit\n", "")
    out, withheld, _ = _run(y, _pole(steps))
    assert out == [] and withheld == {"qualifying_steps_not_adjacent": 1}, withheld


def test_a_parallel_group_between_candidates_breaks_the_run():
    y = _BASE_YAML.replace(
        "      - name: Typecheck\n",
        "      - parallel:\n          - run: ./a.sh\n          - run: ./b.sh\n"
        "      - name: Typecheck\n")
    steps = [s for s in _STEPS if s[0] != "Unit tests"]
    y = y.replace("      - name: Unit tests\n        run: npm test\n", "")
    out, withheld, _ = _run(y, _pole(steps))
    assert out == [] and withheld == {"qualifying_steps_not_adjacent": 1}, withheld


def test_a_setup_named_step_with_effects_between_candidates_breaks_the_run():
    """A step named like setup can still install tools or write GITHUB_ENV that
    the later candidate needs: its effects are unknown, so it is a barrier."""
    y = _BASE_YAML.replace(
        "      - name: Typecheck\n",
        "      - name: Set up test environment\n"
        "        run: echo NODE_OPTIONS=--max-old-space-size=4096 >> \"$GITHUB_ENV\"\n"
        "      - name: Typecheck\n")
    steps = [s for s in _STEPS if s[0] != "Unit tests"]
    y = y.replace("      - name: Unit tests\n        run: npm test\n", "")
    out, withheld, _ = _run(y, _pole(steps))
    assert out == [] and withheld == {"qualifying_steps_not_adjacent": 1}, withheld


def test_a_test_before_a_build_in_one_tree_is_held_back():
    """Serial order hides the race: run side by side, the build writes the tree
    while the earlier test reads it."""
    y = _BASE_YAML.replace("run: npm test", "run: npm run build")
    steps = [s for s in _STEPS if s[0] != "Unit tests"] + [("Unit tests", "build", 25.0)]
    _held(y, "siblings_share_a_build_tree", steps)


@pytest.mark.parametrize("a,b", [("./web", "web"), ("web/", "./web/"), (".", "./"),
                                 ("web/../web", "web"), (".", "web"), ("web", "web/src")])
def test_aliased_or_nested_directories_are_one_build_tree(a, b):
    y = _BASE_YAML.replace(
        "run: npx biome check .", f"run: npm run build\n        working-directory: {a}"
    ).replace("run: npx tsc --noEmit", f"run: npx tsc --noEmit\n        working-directory: {b}")
    y = y.replace("      - name: Unit tests\n        run: npm test\n", "")
    steps = [("Lint", "build", 30.0)] + [s for s in _STEPS[1:] if s[0] != "Unit tests"]
    _held(y, "siblings_share_a_build_tree", steps)


def test_verifier_rejects_a_repeated_candidate_step(tmp_path):
    """Repeating one measured step inflates the upper bound; the pole has it once."""
    f = json.loads(json.dumps(_finding()).replace("50s", "80s"))  # a consistent tamper
    st = f["independent_steps"]
    st["steps"].append(dict(st["steps"][0]))
    st["ceiling_s"] = 80.0
    doc, report = _doc_and_report(f)
    c = _check(tmp_path, doc, report)
    assert not c.ok and "named more than once" in c.detail, c.detail


# ---- review regressions: hidden shared state, expression keys, verifier text ----

@pytest.mark.parametrize("extra", [
    "        env:\n          WEB_HASH: ${{ hashFiles('web/dist/**') }}\n",
    "        shell: ${{ matrix.shell }}\n",
])
def test_a_runtime_expression_anywhere_on_a_step_is_held_back(extra):
    """The card says no step runs a `${{ }}` expression; `env:` and `shell:` are
    evaluated when the step starts, so they count too."""
    y = _BASE_YAML.replace("        run: npm test\n", "        run: npm test\n" + extra)
    _held(y, "step_uses_runtime_expression")


@pytest.mark.parametrize("a,b", [
    ("cargo clippy --all-targets", "cargo test"),            # one target/ and its lock
    ("./mvnw -q verify -DskipTests", "./mvnw -q test"),        # one target/
    ("pytest --cov=app tests/unit", "pytest --cov=app tests/api"),  # one .coverage
])
def test_two_steps_of_one_tool_in_one_tree_are_held_back(a, b):
    y = _BASE_YAML.replace("run: npx biome check .", f"run: {a}").replace(
        "run: npx tsc --noEmit", f"run: {b}")
    _held(y, "siblings_share_tool_state")


def test_two_steps_of_one_tool_in_separate_trees_still_fire():
    y = _BASE_YAML.replace("run: npx biome check .",
                           "run: cargo test\n        working-directory: crates/a").replace(
        "run: npx tsc --noEmit", "run: cargo test\n        working-directory: crates/b").replace(
        "run: npm test", "run: npm test\n        working-directory: web")
    out, withheld, _ = _run(y)
    assert len(out) == 1, withheld
    assert [s["working_directory"] for s in out[0]["independent_steps"]["steps"]] == [
        "crates/a", "crates/b", "web"]


def test_a_job_with_service_containers_is_held_back():
    y = _BASE_YAML.replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    services:\n      db:\n        image: postgres:16\n")
    _held(y, "siblings_share_tool_state")


def test_two_steps_driving_docker_are_held_back():
    # Disjoint trees, so only the shared daemon (one image tag) can hold it back.
    y = _BASE_YAML.replace("run: npx biome check .",
                           "run: docker build -t app:ci .\n        working-directory: img").replace(
        "run: npx tsc --noEmit", "run: docker run --rm app:ci npm run e2e\n"
        "        working-directory: e2e").replace(
        "run: npm test", "run: npm test\n        working-directory: web")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    _held(y, "siblings_share_tool_state", steps)


@pytest.mark.parametrize("cmd", ["npm --prefix ../app run build",
                                 "npx tsc -p ../app --outDir /tmp/out",
                                 "make -C ../app build"])
def test_a_build_aimed_at_another_directory_is_one_tree_with_everything(cmd):
    y = _BASE_YAML.replace("run: npx biome check .",
                           f"run: {cmd}\n        working-directory: tools").replace(
        "run: npx tsc --noEmit", "run: npx tsc --noEmit\n        working-directory: app").replace(
        "run: npm test", "run: npm test\n        working-directory: app")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    _held(y, "siblings_share_a_build_tree", steps)


def test_a_background_expression_is_never_a_candidate():
    y = _BASE_YAML.replace("        run: npx tsc --noEmit\n",
                           "        run: npx tsc --noEmit\n        background: ${{ true }}\n")
    steps = [s for s in _STEPS if s[0] != "Unit tests"]
    _held(y, "background_is_an_expression", steps)


@pytest.mark.parametrize("extra", ["about 2 minutes sooner", "45 seconds faster",
                                   "45 sec faster", "1m faster", "1.5 min faster"])
def test_verifier_rejects_a_duration_in_any_unit(tmp_path, extra):
    doc, report = _doc_and_report()
    report = report.replace("- **Where:**", f"- **Expected:** {extra}.\n- **Where:**")
    c = _check(tmp_path, doc, report)
    assert not c.ok and "other than the candidate steps" in c.detail, c.detail


def test_verifier_rejects_a_card_that_lists_a_different_step(tmp_path):
    doc, report = _doc_and_report()
    report = report.replace("`Lint` 30s", "`Deploy preview` 30s")
    c = _check(tmp_path, doc, report)
    assert not c.ok and "does not list step(s)" in c.detail, c.detail


def test_verifier_accepts_a_step_whose_name_carries_a_duration_flag(tmp_path):
    """GitHub names an unnamed step after its command: `go test -timeout 300s`
    is a measured step name, not a duration claim."""
    name = "Run go test -timeout 300s ./pkg/..."
    f = json.loads(json.dumps(_finding()).replace('"Lint"', json.dumps(name)))
    pole = json.loads(json.dumps(_pole()).replace('"Lint"', json.dumps(name)))
    doc = {"findings": [f], "pr_critical_path": {"poles": [pole]}}
    report = _head(pole) + "\n".join(bp._opt83_card(f, "https://x/c.md")) + "\n"
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


# ---- review regressions: OPT81's last-resort advisory, one finding per pole -----

def _a2_jobs(steps):
    import datetime as dt
    out = []
    for i in range(5):
        t0 = t = dt.datetime(2026, 10, 1, 12, 0, i)
        js = []
        for n, d in steps:
            e = t + dt.timedelta(seconds=d)
            js.append({"name": n, "status": "completed", "conclusion": "success",
                       "started_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "completed_at": e.strftime("%Y-%m-%dT%H:%M:%SZ")})
            t = e
        out.append([{"name": "quality", "status": "completed", "conclusion": "success",
                     "labels": ["ubuntu-latest"],
                     "started_at": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "completed_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "steps": js}])
    return out


def test_opt83_on_the_pole_keeps_the_bigger_runner_advisory_beside_it():
    """Owner decision: both stay visible. OPT83 is uncredited and needs a
    benchmark, so it does not hold the bigger-runner advisory back; the OPT83
    card renders first and the advisory below it."""
    y = ("name: Quality\non: pull_request\njobs:\n  quality:\n    runs-on: ubuntu-latest\n"
         "    steps:\n      - name: Checkout\n        uses: actions/checkout@v4\n"
         "      - run: npm ci\n      - name: Lint\n        run: npx biome check .\n"
         "      - name: Unit tests\n        run: npm test\n")
    steps = [("Set up job", 1), ("Checkout", 3), ("Run npm ci", 12), ("Lint", 40),
             ("Unit tests", 90), ("Complete job", 1)]
    pole = {"check": "quality", "workflow_file": _WF, "job": "quality",
            "job_p50_s": float(sum(d for _n, d in steps)),
            "steps": [{"step": n, "category": cr._step_category(n), "p50_s": float(d)}
                      for n, d in steps]}
    crit = {"long_pole_job": "quality", "job_runner": {"quality": "ubuntu-latest"},
            "job_p50": {"quality": pole["job_p50_s"]}}
    f83 = cr._detect_opt83_parallel_steps([pole], {_WF: yaml.safe_load(y)}, {_WF: crit}, 10)
    assert len(f83) == 1
    w81: dict[str, int] = {}
    a2 = cr._detect_opt81_runner_size_advisory(
        _WF, _a2_jobs(steps), crit, True, [pole], list(f83), [], set(), 20,
        withheld=w81, withheld_candidates=[])
    assert len(a2) == 1, w81
    assert "a2_cheaper_structural_lever_on_the_pole" not in w81, w81
    assert "OPT83" not in cr._OPT81_CHEAPER_STRUCTURAL
    assert vr._VR_OPT81_CHEAPER_STRUCTURAL == cr._OPT81_CHEAPER_STRUCTURAL


def test_a_pole_listed_twice_yields_one_finding_across_calls():
    """collect() calls the detector once per pole; the seen-set must span the calls,
    or a repeated pole yields two findings and only one card renders."""
    seen: set = set()
    first = cr._detect_opt83_parallel_steps(
        [_pole()], {_WF: yaml.safe_load(_BASE_YAML)}, {}, 40, seen=seen)
    second = cr._detect_opt83_parallel_steps(
        [_pole()], {_WF: yaml.safe_load(_BASE_YAML)}, {}, 41, seen=seen)
    assert len(first) == 1 and second == []
    src = inspect.getsource(cr.collect)
    assert "seen=_o83_seen" in src and "_o83_seen: set[tuple[str, str]] = set()" in src, \
        "collect() must share one seen-set across its per-pole calls"


# ---- review regressions: inherited expressions, reads across trees, names ------

@pytest.mark.parametrize("where", ["job", "workflow"])
def test_an_inherited_working_directory_expression_is_held_back(where):
    block = "defaults:\n  run:\n    working-directory: ${{ inputs.dir }}\n"
    if where == "job":
        y = _BASE_YAML.replace("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n"
                               + "".join("    " + ln + "\n" for ln in block.splitlines()))
    else:
        y = _BASE_YAML.replace("on: pull_request\n", "on: pull_request\n" + block)
    _held(y, "step_uses_runtime_expression")


def test_a_step_reading_a_sibling_build_through_a_relative_path_is_held_back():
    y = _BASE_YAML.replace("run: npx biome check .",
                           "run: npm run build\n        working-directory: web").replace(
        "run: npx tsc --noEmit", "run: node ../web/dist/check.js\n        working-directory: api"
    ).replace("      - name: Unit tests\n        run: npm test\n", "")
    steps = [("Lint", "build", 30.0), ("Typecheck", "scan", 25.0)]
    _held(y, "siblings_share_a_build_tree", steps)


def test_a_string_defaults_run_does_not_crash_the_detector():
    y = _BASE_YAML.replace("    runs-on: ubuntu-latest\n",
                           "    runs-on: ubuntu-latest\n    defaults:\n      run: bash\n")
    out, withheld, _ = _run(y)
    assert len(out) == 1, withheld


@pytest.mark.parametrize("bad", [None, "n/a", float("nan")])
def test_unreadable_step_timings_are_held_back_not_a_verdict(bad):
    pole = _pole()
    for s in pole["steps"]:
        if s["step"] == "Lint":
            s["p50_s"] = bad
    out, withheld, cands = _run(pole=pole)
    assert out == [] and withheld == {"pole_step_timings_unavailable": 1}, withheld
    assert cands == [{"workflow_file": _WF, "job": "quality",
                      "gate": "pole_step_timings_unavailable"}]


def test_a_nan_job_p50_is_held_back():
    out, withheld, _ = _run(pole=_pole(job_p50=float("nan")))
    assert out == [] and withheld == {"pole_step_timings_unavailable": 1}, withheld


def test_verifier_accepts_a_step_name_with_a_pipe(tmp_path):
    name = "Run pytest | tee a.log"
    f = json.loads(json.dumps(_finding()).replace('"Unit tests"', json.dumps(name)))
    pole = json.loads(json.dumps(_pole()).replace('"Unit tests"', json.dumps(name)))
    doc = {"findings": [f], "pr_critical_path": {"poles": [pole]}}
    report = _head(pole) + "\n".join(bp._opt83_card(f, "https://x/c.md")) + "\n"
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


@pytest.mark.parametrize("job,wf", [("e2e-30m", _WF), ("quality", ".github/workflows/nightly-24h.yml")])
def test_verifier_accepts_job_and_workflow_names_that_look_like_durations(tmp_path, job, wf):
    f = json.loads(json.dumps(_finding()).replace('"quality"', json.dumps(job)).replace(
        json.dumps(_WF), json.dumps(wf)))
    pole = json.loads(json.dumps(_pole()).replace('"quality"', json.dumps(job)).replace(
        json.dumps(_WF), json.dumps(wf)))
    doc = {"findings": [f], "pr_critical_path": {"poles": [pole]}}
    report = _head(pole) + "\n".join(bp._opt83_card(f, "https://x/c.md")) + "\n"
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


# ---- coverage additions: the prompt, matching, defaults, verifier branches ------

def _prompt_of(card: str) -> str:
    a = card.index("#### 🤖 Prompt for your coding agent (OPT83)")
    b = card.index("```", card.index("```text", a) + 7)
    return card[a:b]


def test_the_agent_prompt_carries_the_safety_text_and_exactly_the_steps():
    f = _finding()
    prompt = " ".join(_prompt_of("\n".join(bp._opt83_card(f, "https://x/c.md"))).split())
    for need in (bp._OPT83_BENCHMARK, bp._OPT83_RAIL, bp._OPT83_RUNNER_CAVEAT,
                 "If any of these fails, stop",
                 "move exactly these steps, unchanged and in the same order",
                 "service container", *bp._NO_WEAKENING_LINES):
        assert " ".join(str(need).split()) in prompt, need
    assert "`Lint`, `Typecheck`, `Unit tests`" in prompt


@pytest.mark.parametrize("names,pole_job,fires", [
    (("Test ${{ matrix.os }}",), "Test ubuntu", True),
    (("Test ${{ matrix.os }}", "Test ${{ matrix.node }}"), "Test ubuntu", False),
    (("${{ matrix.name }}", "quality"), "quality", False),  # both could be it
])
def test_an_expression_job_name_matches_only_one_yaml_job(names, pole_job, fires):
    body = _BASE_YAML.split("jobs:\n", 1)[1]
    job_body = body.split("  quality:\n", 1)[1]
    jobs = "".join(f"  j{i}:\n    name: {n}\n" + job_body for i, n in enumerate(names))
    y = _BASE_YAML.split("jobs:\n", 1)[0] + "jobs:\n" + jobs
    out, withheld, _ = _run(y, _pole(job=pole_job))
    if fires:
        assert len(out) == 1 and out[0]["independent_steps"]["yaml_job"] == "j0", withheld
    else:
        assert out == [] and withheld == {"pole_job_not_matched_in_yaml": 1}, withheld


@pytest.mark.parametrize("build_wd,fires", [("docs", True), ("app/x", False)])
def test_a_workflow_level_default_working_directory_is_read(build_wd, fires):
    y = _BASE_YAML.replace("on: pull_request\n",
                           "on: pull_request\ndefaults:\n  run:\n    working-directory: app\n"
                           ).replace("run: npx biome check .",
                                     f"run: npm run build\n        working-directory: {build_wd}")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    out, withheld, _ = _run(y, _pole(steps))
    if fires:
        assert [s["working_directory"] for s in out[0]["independent_steps"]["steps"]] == [
            "docs", "app", "app"], withheld
    else:
        assert out == [] and withheld == {"siblings_share_a_build_tree": 1}, withheld


@pytest.mark.parametrize("value", ["true", "'true'", "yes"])
def test_a_candidate_allowed_to_fail_is_held_back(value):
    """A step with `continue-on-error` changes what a failing parallel child does
    to the job (the group no longer fails on it), so the pole is held back."""
    y = _BASE_YAML.replace("        run: npm test\n",
                           f"        run: npm test\n        continue-on-error: {value}\n")
    _held(y, "candidate_has_continue_on_error")


def test_an_explicit_continue_on_error_false_still_fires():
    y = _BASE_YAML.replace("        run: npm test\n",
                           "        run: npm test\n        continue-on-error: false\n")
    out, withheld, _ = _run(y)
    assert len(out) == 1, withheld


def test_verifier_fails_a_card_rendered_twice(tmp_path):
    doc, report = _doc_and_report()
    c = _check(tmp_path, doc, report + report)
    assert not c.ok and "rendered more than once" in c.detail, c.detail


def test_verifier_fails_a_finding_with_one_step(tmp_path):
    doc, report = _doc_and_report()
    doc["findings"][0]["independent_steps"]["steps"] = (
        doc["findings"][0]["independent_steps"]["steps"][:1])
    c = _check(tmp_path, doc, report)
    assert not c.ok and "it needs two or more" in c.detail, c.detail


@pytest.mark.parametrize("job_p50", [0.0, "n/a"])
def test_a_zero_or_unreadable_job_p50_is_held_back(job_p50):
    pole = _pole()
    pole["job_p50_s"] = job_p50
    out, withheld, _ = _run(pole=pole)
    assert out == [] and withheld == {"pole_step_timings_unavailable": 1}, withheld


@pytest.mark.parametrize("wd", ["..", "../web", "/srv/web"])
def test_a_working_directory_outside_the_checkout_shares_every_tree(wd):
    """`..` or an absolute path is not a subtree of the checkout: where it
    writes cannot be compared with the other steps' trees."""
    y = _BASE_YAML.replace("run: npx biome check .",
                           f"run: npm run build\n        working-directory: {wd}").replace(
        "run: npx tsc --noEmit", "run: npx tsc --noEmit\n        working-directory: app").replace(
        "run: npm test", "run: npm test\n        working-directory: app")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    _held(y, "siblings_share_a_build_tree", steps)


@pytest.mark.parametrize("a,b", [("npx jest --coverage", "npx vitest run --coverage"),
                                 ("go test -coverprofile=c.out ./a/...",
                                  "go test -coverprofile=c.out ./b/...")])
def test_two_coverage_writers_of_any_common_tool_are_held_back(a, b):
    y = _BASE_YAML.replace("run: npx biome check .", f"run: {a}").replace(
        "run: npx tsc --noEmit", f"run: {b}")
    _held(y, "siblings_share_tool_state")


# ---- fail-open gaps closed (install, build-tool state, containers, YAML keys) ---
# Kept as one block at the end of the file.

_NO_INSTALL_STEPS = [s for s in _STEPS if s[0] != "Run npm ci"]


@pytest.mark.parametrize("cmd", [
    "npm ci && npx biome check .", "pip install -e . && ruff check .",
    "uv sync\nuv run ruff check .", "bundle install; bundle exec rubocop"])
def test_an_install_inside_a_candidate_is_held_back(cmd):
    """An install writes node_modules / site-packages / vendor that every other
    candidate reads: run side by side, they would read a half-written tree."""
    y = _BASE_YAML.replace("      - run: npm ci\n", "").replace(
        "run: npx biome check .", "run: |\n          " + cmd.replace("\n", "\n          "))
    _held(y, "candidate_runs_an_install", _NO_INSTALL_STEPS)


@pytest.mark.parametrize("a,b", [
    ("dotnet test tests/Unit", "dotnet test tests/Integration"),
    ("sbt unit/test", "sbt it/test"),
    ("swift test --filter A", "swift test --filter B"),
    ("mix test test/a", "mix test test/b"),
    ("xcodebuild test -scheme A", "xcodebuild test -scheme B"),
])
def test_two_steps_of_one_build_tool_in_one_tree_are_held_back(a, b):
    """Each of these builds into one shared folder (bin/obj, target/, .build,
    _build, DerivedData) before it tests."""
    y = _BASE_YAML.replace("run: npx biome check .", f"run: {a}").replace(
        "run: npx tsc --noEmit", f"run: {b}")
    _held(y, "siblings_share_tool_state")


_GO_YAML = """
name: Quality
on: pull_request
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Start database
        run: docker compose up -d postgres
      - name: Lint
        run: go test ./a/...
      - name: Typecheck
        run: go test ./b/...
      - name: Unit tests
        run: go test ./c/...
"""


def test_a_container_started_by_an_earlier_step_is_held_back():
    _held(_GO_YAML, "job_starts_a_container_or_background_server", _NO_INSTALL_STEPS)


def test_a_container_action_before_the_candidates_is_held_back():
    y = _GO_YAML.replace("        run: docker compose up -d postgres\n",
                         "        uses: hoverkraft-tech/compose-action@v2\n")
    _held(y, "job_starts_a_container_or_background_server", _NO_INSTALL_STEPS)


def test_a_background_server_before_the_candidates_is_held_back():
    y = _GO_YAML.replace("        run: docker compose up -d postgres\n",
                         "        run: ./bin/server --port 8080\n        background: true\n")
    _held(y, "job_starts_a_container_or_background_server", _NO_INSTALL_STEPS)


def test_go_tests_with_no_container_or_server_still_fire():
    y = _GO_YAML.replace("        run: docker compose up -d postgres\n",
                         "        run: go mod download\n")
    out, withheld, _ = _run(y, _pole(_NO_INSTALL_STEPS))
    assert len(out) == 1, withheld


def test_a_step_with_yaml_boolean_keys_does_not_crash_the_detector():
    """YAML 1.1 reads `on:` / `yes:` as booleans, so a step mapping can carry a
    bool key next to string keys; sorting those keys must not raise."""
    y = _BASE_YAML.replace("        run: npm test\n", "        run: npm test\n        on: x\n")
    doc = yaml.safe_load(y)
    assert True in doc["jobs"]["quality"]["steps"][-1]
    out, withheld, _ = _run(docs={_WF: doc})
    assert len(out) == 1, withheld


# ---- test-review additions: build aimed elsewhere, defaults, tool families -----

@pytest.mark.parametrize("cmd", [
    "make -C docs build",            # -C
    "npm --prefix web run build",    # --prefix
    "pnpm --dir web build",          # --dir
    "yarn --cwd web build",          # --cwd
    "npx tsc --outDir out",          # --outDir
    "npx babel src --out-dir lib",   # --out-dir
    "node /opt/tools/build.js",      # an absolute path
])
def test_each_build_elsewhere_form_alone_shares_every_tree(cmd):
    """One case per alternative of `_OPT83_BUILD_ELSEWHERE_RE`, none with `../`:
    the build sits in `tools`, the others in `app`, so only the regex can make
    them one tree."""
    assert "../" not in cmd
    y = _BASE_YAML.replace("run: npx biome check .",
                           f"run: {cmd}\n        working-directory: tools").replace(
        "run: npx tsc --noEmit", "run: npx tsc --noEmit\n        working-directory: app").replace(
        "run: npm test", "run: npm test\n        working-directory: app")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    _held(y, "siblings_share_a_build_tree", steps)


def test_a_build_with_no_elsewhere_form_in_its_own_tree_fires():
    """The control for the cases above: the same layout, a plain build."""
    y = _BASE_YAML.replace("run: npx biome check .",
                           "run: npm run build\n        working-directory: tools").replace(
        "run: npx tsc --noEmit", "run: npx tsc --noEmit\n        working-directory: app").replace(
        "run: npm test", "run: npm test\n        working-directory: app")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    out, withheld, _ = _run(y, _pole(steps))
    assert len(out) == 1, withheld


def test_sibling_prefix_directories_are_not_one_tree():
    """`web` and `website` share a prefix, not a tree."""
    assert cr._opt83_same_tree("web", "website") is False
    assert cr._opt83_same_tree("website", "web") is False
    y = _BASE_YAML.replace("run: npx biome check .",
                           "run: npm run build\n        working-directory: web").replace(
        "run: npx tsc --noEmit", "run: npx tsc --noEmit\n        working-directory: website"
    ).replace("      - name: Unit tests\n        run: npm test\n", "")
    steps = [("Lint", "build", 30.0), ("Typecheck", "scan", 25.0)]
    out, withheld, _ = _run(y, _pole(steps))
    assert [s["working_directory"] for s in out[0]["independent_steps"]["steps"]] == [
        "web", "website"], withheld


@pytest.mark.parametrize("build_wd,fires", [("app", False), ("web", True)])
def test_the_job_default_working_directory_beats_the_workflow_default(build_wd, fires):
    """Job default `app`, workflow default `web`: the steps with no directory of
    their own run in `app`. A build at `app` shares their tree (held back); a
    build at `web` does not (fires). Reading the workflow default first flips
    both."""
    y = _BASE_YAML.replace(
        "on: pull_request\n",
        "on: pull_request\ndefaults:\n  run:\n    working-directory: web\n").replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    defaults:\n      run:\n        working-directory: app\n"
    ).replace("run: npx biome check .",
              f"run: npm run build\n        working-directory: {build_wd}")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    out, withheld, _ = _run(y, _pole(steps))
    if fires:
        assert [s["working_directory"] for s in out[0]["independent_steps"]["steps"]] == [
            "web", "app", "app"], withheld
    else:
        assert out == [] and withheld == {"siblings_share_a_build_tree": 1}, withheld


@pytest.mark.parametrize("a,b", [
    ("./gradlew test", "./gradlew lint"),
    ("gradle test", "gradle lint"),
    ("npx nyc mocha test/unit", "npx nyc mocha test/api"),
    ("npx c8 node test/unit.js", "npx c8 node test/api.js"),
    ("coverage run -m pytest tests/unit", "coverage run -m pytest tests/api"),
])
def test_each_tool_state_family_in_one_tree_is_held_back(a, b):
    y = _BASE_YAML.replace("run: npx biome check .", f"run: {a}").replace(
        "run: npx tsc --noEmit", f"run: {b}")
    _held(y, "siblings_share_tool_state")


@pytest.mark.parametrize("a,b", [
    ("podman run --rm app:ci npm run lint", "podman run --rm app:ci npm run e2e"),
    ("docker-compose run --rm lint", "docker-compose run --rm e2e"),
])
def test_each_container_driver_twice_is_held_back(a, b):
    # Disjoint trees, so only the shared daemon can hold it back.
    y = _BASE_YAML.replace("run: npx biome check .",
                           f"run: {a}\n        working-directory: img").replace(
        "run: npx tsc --noEmit", f"run: {b}\n        working-directory: e2e").replace(
        "run: npm test", "run: npm test\n        working-directory: web")
    _held(y, "siblings_share_tool_state")


def test_a_lone_docker_candidate_is_not_shared_tool_state():
    """One step driving Docker shares the daemon with no other candidate, so the
    shared-tool-state count (two or more) does not hold it back; the container
    it may start is what does, under its own reason."""
    y = _BASE_YAML.replace("run: npx biome check .",
                           "run: docker build -t app:ci .\n        working-directory: img").replace(
        "run: npx tsc --noEmit", "run: npx tsc --noEmit\n        working-directory: app").replace(
        "run: npm test", "run: npm test\n        working-directory: web")
    steps = [("Lint", "build", 30.0)] + list(_STEPS[1:])
    _held(y, "job_starts_a_container_or_background_server", steps)


def test_a_docker_step_after_the_last_candidate_does_not_hold_the_pole_back():
    y = _BASE_YAML + "      - name: Publish image\n        run: docker build -t app:ci .\n"
    steps = list(_STEPS) + [("Publish image", "package", 5.0)]
    out, withheld, _ = _run(y, _pole(steps))
    assert len(out) == 1, withheld


# ---- test-review additions: the verifier ----------------------------------------

_TWO_JOBS_YAML = _BASE_YAML + _BASE_YAML.split("jobs:\n", 1)[1].replace(
    "  quality:\n", "  quality2:\n", 1)


def test_verifier_passes_two_honest_findings_with_two_cards(tmp_path):
    """Two OPT83 findings in one report: both re-derive and both cards pair.
    A verifier that read only the first finding would call the second card an
    orphan."""
    poles = [_pole(), _pole(job="quality2")]
    out = cr._detect_opt83_parallel_steps(
        poles, {_WF: yaml.safe_load(_TWO_JOBS_YAML)}, {}, 40)
    assert [f["id"] for f in out] == ["f41", "f42"]
    doc = {"findings": out, "pr_critical_path": {"poles": poles}}
    report = "".join(_head(p, n) + "\n".join(bp._opt83_card(f, "https://x/c.md")) + "\n"
                     for n, (p, f) in enumerate(zip(poles, out), 1))
    assert report.count('<a id="opt83-') == 2
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail
    assert "2 OPT83 finding(s), 2 card(s)" in c.detail


@pytest.mark.parametrize("edit,needle", [
    (lambda d: d["findings"][0].__setitem__("sizing_basis", "measured"),
     "not 'uncredited'"),
    (lambda d: d["findings"][0]["independent_steps"].__setitem__("kind", "opt79"),
     "missing its opt83_independent_steps block"),
    (lambda d: d["findings"][0]["independent_steps"]["steps"].__setitem__(
        1, {"step": "Deploy preview", "p50_s": 25.0}),
     "step `Deploy preview` is not in the pole's measured steps"),
])
def test_verifier_fails_a_tampered_record_cleanly(tmp_path, edit, needle):
    doc, report = _doc_and_report()
    edit(doc)
    c = _check(tmp_path, doc, report)
    assert not c.ok and needle in c.detail, c.detail


def test_verifier_fails_a_sizing_line_that_drops_the_upper_bound_label(tmp_path):
    """The SIZING line keeps "up to 50s sooner" but loses its UPPER BOUND label:
    a reader sees a bare number. The prompt's copy of the label does not stand in
    for it."""
    doc, report = _doc_and_report()
    sizing = next(ln for ln in report.splitlines() if ln.startswith("- **SIZING:**"))
    bare = sizing.split(" - ", 1)[0] + "."
    tampered = report.replace(sizing, bare)
    assert "up to 50s sooner" in tampered and tampered != report
    c = _check(tmp_path, doc, tampered)
    assert not c.ok and "its label" in c.detail, c.detail


# ---- test-review additions: render() wiring --------------------------------------

def _render_doc(poles, findings):
    return {
        "repo": "o/r", "scanned_at": "2026-10-09T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 2},
        "pr_critical_path": {"sampled_pr_count": 20, "sample_target": 20,
                             "sample_complete": True, "poles": poles},
        "findings": findings,
    }


def _render_pole(check="quality", job="quality", wf=_WF, p50=96.0):
    p = _pole(job=job, wf=wf)
    p.update({"check": check, "p50_s": p50, "dominant_step": "Lint",
              "dominant_p50_s": 30.0})
    return p


def _o83_heading_at(md, i):
    return md.rfind("\n## ", 0, i)


def test_render_puts_a_finding_on_an_unrendered_pole_in_its_own_section():
    """The finding's pole (`quality`) is not among the rendered poles: the card
    still renders once, in its own section, never dropped."""
    f = _finding()
    other = {"check": "tests-web", "p50_s": 255.0, "job": "tests-web",
             "workflow_file": ".github/workflows/pipeline.yml",
             "dominant_step": "run tests", "dominant_p50_s": 91.0,
             "steps": [{"step": "run tests", "category": "test", "p50_s": 91.0}]}
    md = bp.render(_render_doc([other], [f]))
    anchor = '<a id="opt83-f41"></a>'
    assert "Long pole 1: `pipeline.yml` ▸ `tests-web`" in md
    assert md.count(anchor) == 1, md[-3000:]
    sec = md[_o83_heading_at(md, md.index(anchor)):]
    assert sec.startswith("\n## 🔀 Independent steps on other long poles"), sec[:200]


def test_render_draws_one_card_when_two_rendered_poles_share_the_job():
    """Two rendered poles (two check names) resolve to the one `quality` job:
    the finding joins both, and its card renders once, at the first."""
    f = _finding()
    poles = [_render_pole(check="quality", p50=120.0),
             _render_pole(check="Quality checks", p50=110.0)]
    md = bp.render(_render_doc(poles, [f]))
    assert md.count("Long pole 2:") == 1, "both poles must render"
    anchor = '<a id="opt83-f41"></a>'
    assert md.count(anchor) == 1, md.count(anchor)
    assert "Independent steps on other long poles" not in md
    assert "Long pole 1:" in md[_o83_heading_at(md, md.index(anchor)):][:200]


def test_render_puts_opt83_before_an_opt81_card_on_the_same_pole():
    """On one pole, OPT83 (side by side on the same runner) renders before OPT81
    (a different runner), which stays the last option."""
    import test_opt81_faster_runner as t81
    f81 = t81._a1_finding()
    f81.update({"id": "f50", "workflow_file": _WF, "affected_jobs": ["quality"]})
    md = bp.render(_render_doc([_render_pole(p50=120.0)], [_finding(), f81]))
    a83, a81 = md.find('<a id="opt83-f41"></a>'), md.find('<a id="opt81-f50"></a>')
    assert a83 != -1 and a81 != -1, (a83, a81)
    assert a83 < a81
    assert _o83_heading_at(md, a83) == _o83_heading_at(md, a81), "one pole section"


# ---- comment- and silent-failure-review fixes (2026-10-09) --------------------
# Kept as one separate block at the end of the file: a publish/deploy/upload
# candidate, raw `${{` defaults, `background:` expressions, unreadable step
# entries, ambiguous poles, card placement, the verifier's fail-closed paths,
# the held-back counter cross-check and the held-back row's step names.

@pytest.mark.parametrize("name,cat,run", [
    ("Lint", "scan", "npx biome check . && npm publish"),
    ("Lint", "scan", "npx vercel deploy --prebuilt"),
    ("Lint", "scan", "aws s3 sync dist s3://bucket"),
    ("Lint", "scan", "gh release upload v1 dist/app.tgz"),
    ("Upload coverage", "test", "npx jest"),
    ("Lint", "package", "npx biome check ."),
])
def test_a_candidate_that_publishes_deploys_or_uploads_is_held_back(name, cat, run):
    """A publish, deploy or upload next to the checks that gate it relies on them
    having passed first; inside a group that ordering is gone."""
    y = _BASE_YAML.replace("      - name: Lint\n        run: npx biome check .",
                           f"      - name: {name}\n        run: {run}")
    steps = [(name, cat, 30.0)] + list(_STEPS[1:])
    _held(y, "candidate_publishes_deploys_or_uploads", steps)


def test_the_card_and_prompt_say_no_step_publishes_deploys_or_uploads():
    card = " ".join("\n".join(bp._opt83_card(_finding(), "https://x/c.md")).split())
    assert "none publishes, deploys or uploads" in card
    prompt = " ".join(_prompt_of("\n".join(bp._opt83_card(_finding(), "u"))).split())
    assert ("none publishes, deploys or uploads, or relies on another having "
            "succeeded first") in prompt
    assert "`if: success()`" in prompt


def test_an_inherited_expression_directory_is_read_before_it_is_normalized():
    """`${{ inputs.dir }}/..` normalizes to `.`: the expression must be seen on
    the raw value, or the step reads as running at the checkout root."""
    y = _BASE_YAML.replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    defaults:\n      run:\n"
        "        working-directory: ${{ inputs.dir }}/..\n")
    _held(y, "step_uses_runtime_expression")


def test_a_background_expression_before_the_candidates_is_its_own_reason():
    y = _BASE_YAML.replace(
        "      - name: Lint\n",
        "      - name: Start server\n        run: ./serve.sh\n"
        "        background: ${{ inputs.bg }}\n      - name: Lint\n")
    _held(y, "background_is_an_expression")


def test_a_background_expression_after_the_candidates_does_not_hold_them_back():
    y = _BASE_YAML + ("      - name: Report\n        run: ./report.sh\n"
                      "        background: ${{ inputs.bg }}\n")
    out, withheld, _ = _run(y)
    assert len(out) == 1, withheld


@pytest.mark.parametrize("edit", [
    lambda st: st.append("garbage"),
    lambda st: st.append({"step": "", "category": "test", "p50_s": 5.0}),
    lambda st: st[0].pop("category"),
    lambda st: st[0].__setitem__("category", ""),
    lambda st: st.append(dict(st[0])),
], ids=["non-object", "unnamed", "no-category", "empty-category", "duplicate-name"])
def test_unreadable_pole_step_entries_are_held_back(edit):
    """Dropped quietly, a malformed entry changes which steps qualify; the pole
    must be held back, as an unreadable p50 already is."""
    pole = _pole()
    edit(pole["steps"])
    out, withheld, cands = _run(pole=pole)
    assert out == [] and withheld == {"pole_step_entries_unreadable": 1}, withheld
    assert cands == [{"workflow_file": _WF, "job": "quality",
                      "gate": "pole_step_entries_unreadable"}]


def test_an_ambiguous_pole_is_held_back_not_a_verdict():
    pole = {"check": "quality", "p50_s": 90.0,
            "ambiguous_workflows": [".github/workflows/a.yml", ".github/workflows/b.yml"]}
    out, withheld, cands = _run(pole=pole)
    assert out == [] and withheld == {"pole_workflow_ambiguous": 1}, withheld
    assert cands == [{"workflow_file": "", "job": "quality",
                      "gate": "pole_workflow_ambiguous"}]


def test_a_genuinely_fileless_pole_stays_a_verdict():
    out, withheld, cands = _run(pole={"check": "CodeQL", "p50_s": 300.0,
                                      "ambiguous_workflows": []})
    assert out == [] and withheld == {"pole_not_file_backed": 1} and cands == []


def test_a_held_back_run_names_its_steps():
    y = _BASE_YAML.replace("run: npm test", "run: npm test -- --shard ${{ matrix.shard }}")
    out, withheld, cands = _run(y)
    assert out == [] and cands[0]["steps"] == ["Lint", "Typecheck", "Unit tests"], cands
    key = bp._OPT83_WITHHELD_DOC_KEY
    line = bp._withheld_candidates_line({key: cands}, key, "candidate long pole(s)")
    assert "(quality (Lint + Typecheck + Unit tests))" in line, line


def test_the_verifier_rederives_a_held_back_row_that_names_steps(tmp_path):
    y = _BASE_YAML.replace("run: npm test", "run: npm test -- --shard ${{ matrix.shard }}")
    _out, _withheld, cands = _run(y)
    key = bp._OPT83_WITHHELD_DOC_KEY
    row = next(r for r in bp._WITHHELD_ROWS if r.doc_key == key)
    line = bp._withheld_candidates_line({key: cands}, key, row.noun)
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({key: cands}), encoding="utf-8")
    report = f"| {row.label} | {line} | {row.feeds} |\n"
    err, _note = vr._withheld_disclosure_violation(report, p)
    assert err is None, err
    err, _note = vr._withheld_disclosure_violation(report.replace("Lint + ", ""), p)
    assert err and "not the re-derived line" in err, err


def test_the_bigger_runner_advisory_is_never_held_back_by_opt83():
    """Owner decision: OPT83 is not a cheaper lever that suppresses OPT81 A2;
    both render, OPT83 first. collect() still runs OPT83 before A2."""
    assert "OPT83" not in cr._OPT81_CHEAPER_STRUCTURAL
    assert vr._VR_OPT81_CHEAPER_STRUCTURAL == cr._OPT81_CHEAPER_STRUCTURAL
    src = inspect.getsource(cr.collect)
    assert (src.index("_detect_opt83_parallel_steps(")
            < src.index("_detect_opt81_runner_size_advisory("))


def test_an_opt83_card_on_an_aggregation_gate_pole_renders_in_that_pole():
    from test_blocking_path import _AGG_DEPLOY, _agg_gate_doc, _pole_section
    f = json.loads(json.dumps(_finding()))
    f.update(workflow_file=_AGG_DEPLOY, affected_jobs=["gate"])
    f["independent_steps"]["job"] = "gate"
    doc = _agg_gate_doc()
    doc["findings"] = [f]
    md = bp.render(doc, {}, {}, {}, "2026-07-28T00:00:00Z", {})
    sec = _pole_section(md, "thank you, build")
    anchor = f'<a id="opt83-{f["id"]}"></a>'
    assert anchor in sec and md.count(anchor) == 1, md[-3000:]
    assert "Independent steps on other long poles" not in md


def _unrouted(f):
    return "\n".join(bp._opt83_unrouted_block([f], "https://x/c.md")) + "\n"


def test_verifier_fails_a_card_in_the_unrouted_section_when_its_pole_is_rendered(tmp_path):
    doc, _ = _doc_and_report()
    report = _head(_pole()) + "Pole body.\n\n" + _unrouted(doc["findings"][0])
    c = _check(tmp_path, doc, report)
    assert not c.ok and "outside its pole's section" in c.detail, c.detail


def test_verifier_accepts_the_unrouted_section_when_the_pole_is_not_rendered(tmp_path):
    doc, _ = _doc_and_report()
    report = ("## 🔴 Long pole 1: `ci.yml` ▸ `build` — 2m 00s\n\nPole body.\n\n"
              + _unrouted(doc["findings"][0]))
    c = _check(tmp_path, doc, report)
    assert c.ok, c.detail


@pytest.mark.parametrize("heading", [
    "## 🧹 Also noticed - residual hygiene",
    "## Runner-minute reductions (wall-clock-neutral)",
    "## 🔴 Long pole 2: `ci.yml` ▸ `build` — 2m 00s",
])
def test_verifier_fails_a_card_under_any_other_heading(tmp_path, heading):
    doc, _ = _doc_and_report()
    card = "\n".join(bp._opt83_card(doc["findings"][0], "https://x/c.md"))
    report = _head(_pole()) + "Pole body.\n\n" + heading + "\n\n" + card + "\n"
    c = _check(tmp_path, doc, report)
    assert not c.ok and "outside its pole's section" in c.detail, c.detail


def test_verifier_fails_a_finding_anchor_named_in_another_section(tmp_path):
    doc, report = _doc_and_report()
    fid = doc["findings"][0]["id"]
    report += f"\n## 🧹 Also noticed - residual hygiene\n\n- see [this](#opt83-{fid})\n"
    c = _check(tmp_path, doc, report)
    assert not c.ok and "outside its own section" in c.detail, c.detail


@pytest.mark.parametrize("content", [None, "{not json"], ids=["missing", "malformed"])
def test_verifier_fails_a_rendered_card_when_findings_are_unreadable(tmp_path, content):
    _doc, report = _doc_and_report()
    p = tmp_path / "findings.json"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    c = vr.check_opt83_parallel_steps(report, p)
    assert not c.ok and not c.skipped, c.detail
    c = vr.check_opt83_parallel_steps("# no card\n", p)
    assert c.ok and c.skipped, c.detail


def test_verifier_fails_when_the_held_back_counter_disagrees_with_the_list(tmp_path):
    entry = {"workflow_file": _WF, "job": "quality", "gate": "sibling_is_an_action"}
    doc = {"findings": [], "opt83_withheld_by_gate": {"sibling_is_an_action": 2},
           bp._OPT83_WITHHELD_DOC_KEY: [entry]}
    c = _check(tmp_path, doc, "# report\n")
    assert not c.ok and "sibling_is_an_action" in c.detail, c.detail
    doc["opt83_withheld_by_gate"] = {"sibling_is_an_action": 1,
                                     "fewer_than_two_qualifying_steps": 3}
    c = _check(tmp_path, doc, "# report\n")
    assert c.ok, c.detail


def test_the_detector_counts_every_listed_gate_once():
    """The cross-check on real detector output: every held-back gate's count
    equals the entries listed under it, and verdicts are never listed."""
    withheld: dict[str, int] = {}
    cands: list[dict] = []
    docs = {_WF: yaml.safe_load(_BASE_YAML.replace("run: npm test", "run: npm test ${{ x }}"))}
    poles = [_pole(), _pole(job="other"), {"check": "x", "ambiguous_workflows": ["a", "b"]},
             {"check": "CodeQL"}]
    cr._detect_opt83_parallel_steps(poles, docs, {}, 1, withheld=withheld,
                                    withheld_candidates=cands)
    for g in cr._OPT83_HELD_BACK_GATES:
        assert withheld.get(g, 0) == sum(c["gate"] == g for c in cands), (g, withheld)
    assert sum(withheld.get(g, 0) for g in cr._OPT83_HELD_BACK_GATES) == len(cands) == 3


@pytest.mark.parametrize("claim", ["~40% faster", "40 % sooner", "up to 12.5% off"])
def test_verifier_rejects_a_percentage_claim(tmp_path, claim):
    doc, report = _doc_and_report()
    report = report.replace("- **Where:**", f"- **Expected:** {claim}.\n- **Where:**")
    c = _check(tmp_path, doc, report)
    assert not c.ok and "percentage" in c.detail, c.detail


# ---- a job with an unreadable `parallel:` group (#122 interaction) -----------

@pytest.mark.parametrize("bad", [
    "      - name: Extra\n        parallel: not-a-list\n",
    "      - parallel:\n          - 1\n",
])
def test_a_job_with_an_unreadable_parallel_group_is_held_back(bad):
    """A malformed `parallel:` group's steps were never read, so the
    independence read is incomplete: whatever those steps do (write the build
    tree, start a server, set GITHUB_ENV) is unknown. The pole is held back
    with #122's reason, even though the candidates themselves sit next to each
    other above it, never reported as independent."""
    y = _BASE_YAML + bad
    assert cr.job_walk(yaml.safe_load(y)["jobs"]["quality"]).malformed_groups
    _held(y, "job_has_an_unreadable_parallel_group")


def test_the_unreadable_group_reason_reuses_the_shared_phrase_lead():
    phrase = bp._OPT83_WITHHOLD_PHRASES["job_has_an_unreadable_parallel_group"]
    lead = ("some of the job's steps sit in a parallel step group that could not "
            "be read, so ")
    assert phrase.startswith(lead), phrase
    assert bp._OPT79_HELD_BACK_REASONS["job_has_an_unreadable_parallel_group"].startswith(lead)
    assert vr._VR_OPT83_WITHHOLD_PHRASES["job_has_an_unreadable_parallel_group"] == phrase

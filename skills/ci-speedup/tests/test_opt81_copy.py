"""OPT81 copy and render-routing guards: what the A1 and A2 cards and prompts
tell the reader, and where an A2 advisory may render at all.

Kept apart from test_opt81_faster_runner.py (the detector and verifier suite)
so copy fixes and engine fixes can land in parallel.
"""
from __future__ import annotations

import copy
import json
import re

import blocking_path as bp
import collect_runs as cr
from test_opt81_faster_runner import (_a1_finding, _a2_finding, _doc,
                                      _load_verify_report, _opt81_catalog_entry,
                                      _report_with)

_URL = "https://github.com/starslingdev/skills/blob/main/skills/ci-speedup/references/optimization-patterns.md"


def _card(f, **kw) -> str:
    return "\n".join(bp._opt81_card(f, _URL, **kw))


def _fence(text: str) -> str:
    return text[text.index("```text"):]


def _flat(s: str) -> str:
    return " ".join(s.split())


# ---- Option 1: GitHub defines no Linux/Windows larger-runner label ------------

def test_opt81_a2_option_one_names_no_invented_github_label():
    """GitHub has no `ubuntu-latest-8-cores` label: an organisation admin
    creates a larger runner and the workflow uses that runner's name."""
    catalog_a2 = _flat(_opt81_catalog_entry()).split("Fix recipe (A2)")[-1]
    for where, text in (("card", _card(_a2_finding())), ("catalog", catalog_a2)):
        assert "ubuntu-latest-8-cores" not in text, where
        assert "Nothing else needs to change" not in text, where
        assert "organisation admin creates the larger runner" in text, where
        assert "billed per minute even where standard runners are free" in text, where
        assert "to that runner's name" in text, where
    entry = _flat(_opt81_catalog_entry())
    a1_item = entry[entry.index("1. Runner labels"):entry.index("2. At least")]
    assert "ubuntu-latest-8-cores" not in a1_item
    assert "`ubuntu-24.04-16core`" in a1_item and "`windows-2022-16core`" in a1_item
    assert "chosen by the organisation" in a1_item
    assert "`macos-14-xlarge`" in a1_item


# ---- The last-option line says what gate (d) checked, not more ----------------

def test_opt81_a2_prompt_does_not_overclaim_the_cheaper_lever_search():
    fence = _fence(_card(_a2_finding()))
    assert "not cacheable" not in fence and "not shardable" not in fence
    assert "no cheaper lever was found" not in fence
    assert ("This is the last option the audit can name for this job: none of the "
            "cheaper levers it checks for fired here (") in fence
    assert ("Before benchmarking a different or larger runner class, check whether the dominant step can "
            "be cached or split; this audit did not prove it cannot.") in fence


def test_opt81_a2_prompt_lists_exactly_the_levers_the_finding_stamped():
    """The prompt renders `cheaper_levers_checked` literally: a lever the engine
    adds shows up, and a lever it did not stamp is never claimed."""
    f = copy.deepcopy(_a2_finding())
    f["faster_runner"]["cheaper_levers_checked"] = [
        {"lever": "a speculative lever (OPT77)", "patterns": ["OPT77"], "applies": False,
         "why": "examined only when it credits half the pole's median"}]
    card = _card(f)
    assert "a speculative lever (OPT77)" in _fence(card)
    assert "examined only when it credits half the pole's median" in card
    fired = _fence(card).split("fired here (", 1)[1].split("). Before", 1)[0]
    assert fired == "a speculative lever (OPT77); log-level leaves", fired


# ---- The A2 pattern line carries no measurement claim -------------------------

def test_opt81_a2_card_and_prompt_never_say_measurably_faster():
    card = _card(_a2_finding())
    assert "Measurably Faster" not in card
    assert ("Pattern: OPT81 (advisory) - a different or larger runner class is the "
            "last lever this audit can name; benchmark required.") in card
    assert f"{_URL}#" in card  # the catalog anchor link stays


def test_opt81_a2_framing_is_not_bigger_runner_only():
    card = _card(_a2_finding())
    head = card.splitlines()[2]
    assert "bigger runner" not in head
    assert "a different or larger runner class" in head
    held = _card(_a2_finding(), held_back_by="vitest-isolate-pool")
    assert "bigger runner" not in held


# ---- Option 2 reads as a prerequisite, not a pitch ----------------------------

_OPT2 = ("Option 2, StarSling runners: the benchmark can only be run after installing "
         "the StarSling GitHub app on the repository; the audit has no StarSling "
         "measurement for this job.")


def test_opt81_a2_option_two_states_the_prerequisite_once():
    card = _card(_a2_finding())
    body = card[:card.index("```text")]
    assert body.count("installing the StarSling GitHub app") == 1
    assert "- **Option 2, StarSling runners:** the benchmark can only be run" in body
    assert "- **Option 1, " in body
    # only the lead-in is bold, never the whole sentence
    assert f"**{_OPT2}**" not in body
    assert _OPT2 in _fence(card)
    assert _OPT2.split(": ", 1)[1].lower() in _flat(_opt81_catalog_entry()).lower()


# ---- The log-level entry reads as decided once the card renders ---------------

def test_opt81_a2_card_states_the_log_check_passed_not_pending():
    card = _card(_a2_finding())
    assert "decided when the report is rendered" not in card
    assert "no log-level lever matched this pole's log" in card


# ---- An A2 whose pole is not rendered never renders as an advisory ------------

_UNROUTED_HELD = ("advisory held back: its pole is not one of the long poles this "
                  "report renders")


def test_opt81_unrouted_a2_is_a_held_back_line_and_verifies(tmp_path):
    a1, a2 = _a1_finding(), _a2_finding()
    block = "\n".join(bp._opt81_unrouted_block([a1, a2], _URL))
    assert "Option 1" not in block and "Option 2" not in block
    assert _UNROUTED_HELD in block
    assert "The same job measured on two runner classes" not in block
    vr = _load_verify_report()
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_doc(a1, a2)), encoding="utf-8")
    chk = vr.check_opt81_runner_comparison_rederived(_report_with([block]), path)
    assert chk.ok, chk.detail


# ---- A pole whose log was not read fails CLOSED -------------------------------

def test_opt81_a2_on_a_pole_with_no_log_is_held_back():
    card = _card(_a2_finding(), log_read=False)
    assert ("advisory held back: this pole's log was not read, so the log-level "
            "check did not run") in card
    assert "Option 1" not in card
    assert cr._OPT81_DISCLOSURE in card


def test_opt81_verifier_rejects_an_a2_card_without_the_log_check(tmp_path):
    vr = _load_verify_report()
    a2 = _a2_finding()
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_doc(a2)), encoding="utf-8")
    good = _report_with(bp._opt81_card(a2, "u"))
    assert vr.check_opt81_runner_comparison_rederived(good, path).ok
    bad = good.replace("no log-level lever matched this pole's log", "pending")
    r = vr.check_opt81_runner_comparison_rederived(bad, path)
    assert not r.ok and "log-level" in r.detail, r.detail


# ---- A1 prompt: cost check and the hidden-matrix caveat -----------------------

def test_opt81_a1_prompt_carries_the_cost_check_and_matrix_caveat():
    fence = _flat(_fence(_card(_a1_finding())))
    assert ("Check the cost with whoever pays for CI first: the two classes bill "
            "differently.") in fence
    assert ("If this is a matrix whose legs differ in more than their runner (an "
            "include: leg, a tool version), the gap is not the runner's alone.") in fence


# ---- A credited A1 at its pole is pointed at, never sent to Also noticed ------

def _credited_a1():
    f = _a1_finding()
    f["wall_clock_p50_s"] = 60.0
    return f


def _pole():
    return {"workflow_file": ".github/workflows/bench.yml", "check": "bench",
            "job": "bench", "p50_s": 150.0, "steps": []}


def test_opt81_credited_a1_pole_prompt_points_at_its_card():
    a1 = _credited_a1()
    assert bp._data_driven_for_pole(_pole(), [a1]) == [a1]
    prompt = bp._build_generic_agent_prompt(_pole(), [], None, "o/r", None, 1, 1, None,
                                            data_driven=[a1])
    assert "Also noticed" not in prompt
    assert "dominant step below is where that lever's time is spent" not in prompt
    assert "OPT81" in prompt and "card" in prompt


def test_opt81_credited_a1_pole_waterfall_points_at_its_card():
    lines = "\n".join(bp._pole_waterfall(
        _pole(), None, None, log_present=True, data_driven_present=True,
        data_driven_patterns=("OPT81",)))
    assert "Also noticed" not in lines
    assert "OPT81" in lines
    # a non-OPT81 data-driven match still points at the appendix
    lines = "\n".join(bp._pole_waterfall(
        _pole(), None, None, log_present=True, data_driven_present=True,
        data_driven_patterns=("OPT24",)))
    assert "Also noticed" in lines


# ---- render(): the leaf hold-back is wired through the real renderer ----------

def _a2_on(wf: str, job: str):
    f = copy.deepcopy(_a2_finding())
    f["workflow_file"] = wf
    f["affected_jobs"] = [job]
    f["faster_runner"]["job"] = job
    return f


def _a1_on(wf: str, job: str):
    f = copy.deepcopy(_a1_finding())
    f["workflow_file"] = wf
    f["affected_jobs"] = [job]
    f["faster_runner"]["job"] = job
    return f


def _opt81_block(md: str, fid: str) -> str:
    anchor = f'<a id="opt81-{fid}"></a>'
    assert md.count(anchor) == 1, md.count(anchor)
    rest = md[md.index(anchor) + len(anchor):]
    ends = [i for i in (rest.find('<a id="'), rest.find("\n## ")) if i >= 0]
    return rest[:min(ends)] if ends else rest


def test_opt81_render_holds_an_a2_back_on_a_matched_leaf_and_never_an_a1():
    """Kills R01 (hold-back wiring dropped at the call site): a pole whose own
    log matched a leaf renders the A2 as a held-back line, through render()."""
    from test_blocking_path import _IMPORT_BOUND_LOG, _doc_one_pole
    doc = _doc_one_pole()
    wf = ".github/workflows/pipeline.yml"
    doc["findings"] = [_a2_on(wf, "tests-web")]
    md = bp.render(doc, {"pipeline": _IMPORT_BOUND_LOG}, {}, {}, "2026-06-08")
    block = _opt81_block(md, "f9")
    assert "advisory held back" in block and "vitest-isolate-pool" in block, block
    assert "Option 1" not in block
    # R03: the same matched leaf never holds back a measured A1 card.
    doc["findings"] = [_a1_on(wf, "tests-web")]
    md = bp.render(doc, {"pipeline": _IMPORT_BOUND_LOG}, {}, {}, "2026-06-08")
    block = _opt81_block(md, "f7")
    assert "held back" not in block
    assert "measured from runs this repository already made" in block


def test_opt81_render_holds_an_a2_back_on_an_off_category_leaf():
    """Kills R02 (call site reads only the on-category leaf): an eslint leaf
    demoted off-category on a test-dominant pole still matched the pole's log."""
    from test_blocking_path import _nx_offcategory_doc
    doc, logs = _nx_offcategory_doc()
    doc["findings"] = [_a2_on("ci.yml", "Run Checks")]
    md = bp.render(doc, logs)
    block = _opt81_block(md, "f9")
    assert "advisory held back" in block and "eslint-no-cache" in block, block
    assert "Option 1" not in block


# ---- _opt81_for_pole: matrix base and workflow conflict -----------------------

def test_opt81_for_pole_joins_a_matrix_leg_to_its_unexpanded_base():
    """Kills R09: a finding on job id `bench` belongs to the rendered leg
    `bench (ubuntu, 3.12)` of the same workflow."""
    wf = ".github/workflows/bench.yml"
    f = _a1_on(wf, "bench")
    pole = {"workflow_file": wf, "check": "bench (ubuntu, 3.12)",
            "job": "bench (ubuntu, 3.12)"}
    assert bp._opt81_for_pole(pole, [f]) == [f]


def test_opt81_for_pole_never_joins_across_workflows():
    """Kills R10: the same job name in a different workflow file is not this pole."""
    f = _a1_on(".github/workflows/other.yml", "bench")
    pole = {"workflow_file": ".github/workflows/bench.yml", "check": "bench",
            "job": "bench"}
    assert bp._opt81_for_pole(pole, [f]) == []
    assert bp._opt81_for_pole(dict(pole, workflow_file=".github/workflows/other.yml"),
                              [f]) == [f]


# ---- The A1 card's runner-minute line can only say "unknown" ------------------

def test_opt81_verifier_rejects_an_a1_card_with_a_runner_minute_figure(tmp_path):
    """Kills R13: a made-up runner-minute saving on the A1 card reddens the
    report check; the measured gap in seconds stays allowed on its own line."""
    vr = _load_verify_report()
    a1 = _a1_finding()
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_doc(a1)), encoding="utf-8")
    good = _report_with(bp._opt81_card(a1, "u"))
    assert vr.check_opt81_runner_comparison_rederived(good, path).ok
    bad = good.replace(f"**Runner-minute effect:** {bp._OPT81_RUNNER_MIN_UNKNOWN}.",
                       "**Runner-minute effect:** saves ~60s of runner time.")
    assert bad != good
    r = vr.check_opt81_runner_comparison_rederived(bad, path)
    assert not r.ok and "runner-minute line" in r.detail, r.detail
    assert vr._VR_OPT81_RUNNER_MIN_UNKNOWN == bp._OPT81_RUNNER_MIN_UNKNOWN


def test_opt81_credited_a1_card_states_its_credited_wall_clock():
    """Kills mutant 18 (the credited merge-wait line deleted): a credited A1
    names the credited wall-clock, its value, and never 'not credited'."""
    card = _card(_credited_a1())
    assert ("- **Merge wait:** this job is on the merge-gating path; the credited "
            "wall-clock is 1m 00s after the critical-path floors.") in card
    assert "not credited" not in card


def test_opt81_verifier_rejects_an_a1_card_that_misstates_a_median(tmp_path):
    """Kills mutant 15 (the verifier's `at p50` card check deleted): the card's
    stated medians must be the stamped ones."""
    vr = _load_verify_report()
    a1 = _a1_finding()
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_doc(a1)), encoding="utf-8")
    good = _report_with(bp._opt81_card(a1, "u"))
    assert vr.check_opt81_runner_comparison_rederived(good, path).ok
    bad = good.replace("at p50 90s", "at p50 70s")
    assert bad != good
    r = vr.check_opt81_runner_comparison_rederived(bad, path)
    assert not r.ok and "at p50 90s" in r.detail, r.detail


# ---- No measured pole: an OPT81-only doc still renders its section ------------

def test_opt81_only_doc_with_no_measured_pole_renders_the_runner_class_section(tmp_path):
    """A findings doc with no measured pole whose only findings are OPT81 must
    not collapse to the bare no-critical-path line: the A1 card renders, the A2
    renders only as its held-back one-liner, and the report check passes."""
    a1, a2 = _a1_finding(), _a2_finding()
    doc = {"repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
           "data_sources": {"runs_sampled": 16, "jobs_sampled": 16,
                            "workflows_analyzed": 1},
           "pr_critical_path": {"poles": []},
           "findings": [a1, a2]}
    md = bp.render(doc, {}, {}, {}, "2026-06-08")
    assert md.strip() != "_No measured critical path in this findings JSON._"
    assert "## 🏎️ Runner class comparisons" in md
    assert "measured from runs this repository already made" in _opt81_block(md, "f7")
    held = _opt81_block(md, "f9")
    assert bp._OPT81_A2_HELD_UNROUTED in held
    assert "Option 1" not in held and "Option 2" not in held
    vr = _load_verify_report()
    path = tmp_path / "f.json"
    path.write_text(json.dumps(_doc(a1, a2)), encoding="utf-8")
    chk = vr.check_opt81_runner_comparison_rederived(md, path)
    assert chk.ok, chk.detail

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
    assert ("Before benchmarking a bigger runner, check whether the dominant step can "
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

"""run.py's data-pass summary states the gh API call count, so a run on a shared
API budget sees its cost before rendering (2026-11-03 dogfood: the count reached only
the rendered report, while SKILL.md tells the agent to act on the summary)."""
from __future__ import annotations

from summary import build_summary  # noqa: E402
from test_summary import _doc as _summary_doc  # noqa: E402


def test_summary_states_the_gh_call_count_before_rendering():
    doc = _summary_doc(data_sources={"gh_query_count": 170, "gh_error_count": 0},
                       timings={"scripted_total_s": 41.88})
    s = build_summary(doc)
    assert "gh API cost: 170 call(s) in ~42s." in s, s


def test_summary_names_failed_gh_calls():
    doc = _summary_doc(data_sources={"gh_query_count": 12, "gh_error_count": 3})
    s = build_summary(doc)
    assert "gh API cost: 12 call(s) (3 failed)." in s, s


def test_summary_omits_the_cost_line_when_no_gh_pass_ran():
    assert "gh API cost" not in build_summary(_summary_doc())

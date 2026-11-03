"""The Contents line counts the cost-spine rows the table SHOWS and names the hidden
ones the same way the table does (2026-11-03 dogfood: "a 13-row cost spine" over a
table of 12 rows plus a "+1 more ... hidden" line glued to the table, which GitHub
renders as one more table row)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import blocking_path as bp  # noqa: E402
from test_blocking_path import (  # noqa: E402  (helpers only, read-only)
    _load_verify_report, _render_ready_spine_doc)


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def test_contents_counts_the_shown_spine_rows_and_names_the_hidden_ones(tmp_path):
    doc = _render_ready_spine_doc(row_count=13)
    report = bp.render(doc)

    contents = next(line for line in report.splitlines()
                    if line.startswith("**Runner-minute cost spine**"))
    assert "12 workflow/job rows (+1 more runner-minute row hidden)" in contents, contents
    assert "13 workflow/job rows" not in contents, contents

    vr = _load_verify_report()
    chk = vr.check_cost_spine_contents_count_matches_table(report, _write(tmp_path, doc))
    assert chk.ok and not chk.skipped, chk


def test_hidden_row_line_is_separated_from_the_table(tmp_path):
    # GitHub-flavoured markdown folds a plain line that directly follows a table
    # into the table as one more row; a blank line keeps it a sentence.
    report = bp.render(_render_ready_spine_doc(row_count=13))
    assert re.search(r"^\| Total \|[^\n]*\n\n\+1 more runner-minute row hidden$",
                     report, re.MULTILINE), "hidden-row line is glued to the table"


def test_verifier_rejects_a_contents_count_that_disagrees_with_the_table(tmp_path):
    doc = _render_ready_spine_doc(row_count=13)
    findings_path = _write(tmp_path, doc)
    report = bp.render(doc)
    vr = _load_verify_report()

    overcounted = report.replace(
        "12 workflow/job rows (+1 more runner-minute row hidden)", "13 workflow/job rows")
    assert overcounted != report
    chk = vr.check_cost_spine_contents_count_matches_table(overcounted, findings_path)
    assert not chk.ok, chk

    glued = report.replace("|\n\n+1 more runner-minute row hidden",
                           "|\n+1 more runner-minute row hidden")
    assert glued != report
    chk = vr.check_cost_spine_contents_count_matches_table(glued, findings_path)
    assert not chk.ok, chk


def test_contents_count_without_hidden_rows_is_unchanged(tmp_path):
    doc = _render_ready_spine_doc(row_count=2)
    report = bp.render(doc)
    assert "2 workflow/job rows showing where sampled runner minutes go" in report
    assert "more runner-minute row" not in report
    vr = _load_verify_report()
    chk = vr.check_cost_spine_contents_count_matches_table(report, _write(tmp_path, doc))
    assert chk.ok and not chk.skipped, chk


def test_spine_count_phrase_for_the_runner_minute_reductions_entry():
    assert bp._spine_count_phrase(13, "-row cost spine") == (
        "12-row cost spine (+1 more runner-minute row hidden)")
    assert bp._spine_count_phrase(14, "-row cost spine") == (
        "12-row cost spine (+2 more runner-minute rows hidden)")
    assert bp._spine_count_phrase(5, "-row cost spine") == "5-row cost spine"

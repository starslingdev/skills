"""Structural guards for the transcript loop's summary contract.

The transcript self-improvement loop turns one session's operator steering into a
durable SKILL.md / evals / doc edit. The cross-session recurrence gate
(`scripts/aggregate_lessons.py`) clusters lessons by a stable `signature`, so the
summary schema MUST require that field and pin it to an enforceable template, and
the analysis prompt MUST tell the LLM to emit it (and to record — not encode — a
one-off). These tests fail if either drifts.

Modeled on the other structural guards (test_structural_findings, test_summary):
no jsonschema dependency — the `signature` pattern is exercised directly with `re`.

Run from the repo root:

    pytest -v maintainers/ci-speedup/tests/test_loop_summary_infra.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_LOOP_ROOT = Path(__file__).resolve().parents[1]   # maintainers/ci-speedup — this loop's tree
_REPO_ROOT = _LOOP_ROOT.parents[1]                 # repo root (maintainers/ → root)
_SCHEMA = _LOOP_ROOT / "loops" / "loop-summary.schema.json"
_PROMPT = _LOOP_ROOT / "loops" / "loop-analysis-prompt.md"
_GAP_PROMPT = _LOOP_ROOT / "loops" / "gap-to-catalog-prompt.md"


def _schema() -> dict:
    return json.loads(_SCHEMA.read_text(encoding="utf-8"))


def _steering_item_schema() -> dict:
    return _schema()["properties"]["steering_events"]["items"]


# --------------------------------------------------------------------------- #
# Schema: signature is required + pattern-enforced
# --------------------------------------------------------------------------- #
def test_signature_is_required_on_every_steering_event():
    item = _steering_item_schema()
    assert "signature" in item["required"], (
        "the recurrence gate clusters by signature, so every lesson must carry one"
    )


def test_signature_property_has_a_pattern():
    sig = _steering_item_schema()["properties"]["signature"]
    assert sig["type"] == "string"
    assert sig.get("pattern"), "signature must be pinned to a template, not free prose"


def test_signature_pattern_accepts_well_formed_templates():
    pat = re.compile(_steering_item_schema()["properties"]["signature"]["pattern"])
    good = [
        "gap-fill@SKILL.md:fill-coverage-gap",
        "sizing@references/savings-methodology.md:floor-cap-structural",
        "render@SKILL.md:second-pole",
        "required-scope@SKILL.md:needs-reachability",
        "present@SKILL.md:measure-not-estimate",
        "spine@references/wall-clock-methodology.md:critical-path-floor",
    ]
    for s in good:
        assert pat.match(s), f"valid signature rejected: {s}"


def test_signature_pattern_rejects_free_text_and_off_vocabulary():
    pat = re.compile(_steering_item_schema()["properties"]["signature"]["pattern"])
    bad = [
        "the agent shipped a dead end",            # free prose
        "Gap-Fill@SKILL.md:fill",                  # uppercase area (closed enum, lowercase)
        "made-up-area@SKILL.md:foo",               # area not in the closed enum
        "gap-fill@evals/evals.json:foo",           # file not a contract-doc surface
        "gap-fill@SKILL.md:Fill_Coverage",         # slug not kebab-case
        "gap-fill@SKILL.md:",                       # empty slug
        "gap-fill:SKILL.md@fill",                  # delimiters swapped
        "gap-fill@SKILL.md",                        # missing :rule-slug
    ]
    for s in bad:
        assert not pat.match(s), f"malformed signature accepted: {s}"


# --------------------------------------------------------------------------- #
# Prompt: documents the signature + the recurrence (record-not-encode) rule
# --------------------------------------------------------------------------- #
def test_prompt_documents_the_signature_template():
    body = _PROMPT.read_text(encoding="utf-8")
    assert "signature" in body
    assert "<area>@<file>:<rule-slug>" in body, "prompt must show the fixed template"
    # The closed area vocabulary must be spelled out so the LLM selects, not invents.
    for area in ("spine", "required-scope", "gap-fill", "render", "sizing", "present"):
        assert area in body, f"prompt omits the `{area}` area from the closed set"


def test_prompt_states_the_recurrence_rule():
    body = _PROMPT.read_text(encoding="utf-8").lower()
    # The core discipline, in its own words: a one-off is recorded as feedstock, not
    # encoded. Assert the actual rule phrasing — not a bare "not" that any prose has.
    assert "recorded, not encoded" in body, (
        "prompt must state the record-not-encode rule verbatim"
    )
    assert "pending.jsonl" in body, "prompt must point at the un-promoted feedstock"
    assert "distinct session" in body, "prompt must state the cross-session floor"


# --------------------------------------------------------------------------- #
# Prompt cross-tree references resolve (regression: the #71 maintainers/ relocate
# split the loop tree from the skill tree, dangling links into the skill's own files)
# --------------------------------------------------------------------------- #
def test_loop_analysis_prompt_markdown_links_resolve():
    # Every `../`-relative .md/.json markdown link in the analysis prompt must resolve from the
    # prompt's own directory — a relocate that leaves a link pointing into the (now sibling)
    # maintainers tree where the skill content no longer lives fails here.
    body = _PROMPT.read_text(encoding="utf-8")
    links = re.findall(r"\]\((\.\./[^)]+\.(?:md|json))\)", body)
    assert links, "expected at least one relative markdown link in the analysis prompt"
    for rel in links:
        target = (_PROMPT.parent / rel).resolve()
        assert target.exists(), f"loop-analysis-prompt.md link does not resolve: {rel} → {target}"
    # The two cross-tree skill references specifically must be present (guard against silent drift).
    for must in ("../../../skills/ci-speedup/SKILL.md", "../../../skills/ci-speedup/evals/evals.json"):
        assert must in body, f"loop-analysis-prompt.md no longer references {must}"


def test_gap_to_catalog_prompt_skill_paths_exist():
    # The gap→catalog prompt names skill source files as skill-rooted (repo-root-relative) paths;
    # after the relocate these must still point at real files under skills/ci-speedup/.
    body = _GAP_PROMPT.read_text(encoding="utf-8")
    for rooted in ("skills/ci-speedup/scripts/blocking_path.py",
                   "skills/ci-speedup/references/patterns/INDEX.md",
                   "skills/ci-speedup/tests/test_blocking_path.py"):
        assert rooted in body, f"gap-to-catalog-prompt.md no longer references {rooted}"
        assert (_REPO_ROOT / rooted).exists(), f"gap-to-catalog-prompt.md path missing on disk: {rooted}"

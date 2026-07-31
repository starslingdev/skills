"""Structural validation for `references/ci-score-spec.json` (v0.1-basic).

The OD-CS15 reset: the spec is now a registry of pass/fail CONFIGURATION
FACTS. These tests pin the registry's shape, the band arithmetic that carried
over from the granular design, and the decision record itself — the reset must
stay legible in the artifact, not only in chat history.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SPEC_PATH = _SKILL_DIR / "references" / "ci-score-spec.json"
_METHODOLOGY_PATH = _SKILL_DIR / "references" / "ci-score-methodology.md"
_SKILL_MD_PATH = _SKILL_DIR / "SKILL.md"
_SCRIPTS_DIR = _SKILL_DIR / "scripts"

EXPECTED_CHECK_IDS = [
    "ci.cache.dependency-cache",
    "ci.cache.build-cache",
    "ci.checkout.shallow-clone",
    "ci.parallel.test-sharding",
    "ci.build.change-scoped",
    "ci.trigger.concurrency-groups",
    "ci.trigger.cancel-superseded",
    "ci.trigger.path-filter",
    "ci.hygiene.job-timeouts",
    "ci.security.scoped-id-token",
    "ci.security.pinned-action-shas",
]

# The live /best-practices/github-actions/ pages. Every slug the spec cites
# must be one of these; the one page with no check must be accounted for.
SITE_PRACTICE_SLUGS = {
    "cache-dependencies", "shallow-checkout", "shard-tests", "build-only-affected",
    "path-filter-workflows", "cancel-superseded-runs", "keep-advisory-checks-non-blocking",
    "cut-queue-time", "pin-action-shas", "scope-id-token-per-job",
    # shipped after the v0.1.1 freeze (OD-CS21 accounted them in the note):
    "bound-job-timeouts", "replace-fixed-sleeps-with-polling",
}


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(_SPEC_PATH.read_text())


def test_spec_version(spec):
    assert spec["spec_version"] == "ci-score-v0.1.3"


def test_registry_is_the_eleven_basic_checks(spec):
    # v0.1.2 (OD-CS18) removed ci.trigger.draft-gate: 12 -> 11 checks.
    assert [c["check_id"] for c in spec["checks"]] == EXPECTED_CHECK_IDS
    assert len(spec["checks"]) == 11
    assert "ci.trigger.draft-gate" not in EXPECTED_CHECK_IDS


def test_every_check_states_its_fact(spec):
    """A check IS its fact: the one-sentence thing a maintainer verifies in
    their own YAML. An empty fact is an unreviewable check."""
    for check in spec["checks"]:
        assert check["fact"].strip(), check["check_id"]


def test_practice_slugs_resolve_and_every_page_is_accounted_for(spec):
    cited = {c["practice_slug"] for c in spec["checks"] if c.get("practice_slug")}
    assert cited <= SITE_PRACTICE_SLUGS, cited - SITE_PRACTICE_SLUGS
    # The owner's directive: every published practice is IN the score — except
    # the one that cannot be a config fact, which must be named, not missing.
    unmapped = SITE_PRACTICE_SLUGS - cited
    # Three pages carry no registry practice_slug binding, for three different
    # reasons the note must state (OD-CS21 — the note rotted once when pages
    # shipped after it was written; this census is what keeps it honest):
    # no-check-possible, no-check-in-v0.1.x, and scored-but-null-binding.
    assert unmapped == {"keep-advisory-checks-non-blocking",
                        "replace-fixed-sleeps-with-polling",
                        "bound-job-timeouts"}
    note = spec["practice_coverage_note"]
    for slug in sorted(unmapped):
        assert slug in note, f"unmapped page {slug!r} not accounted for in the note"
    # bound-job-timeouts is the odd one out: scored, null binding — the note
    # must name the scoring check so a reader can confirm it is not a gap.
    assert "ci.hygiene.job-timeouts" in note


def test_the_nothing_to_check_refusals(spec):
    """OD-CS15: config facts need no run history, no branch-protection access,
    no merge-queue calibration — every repo with REAL CI scores on a fresh run.
    The refusals are the two nothing-to-check shapes plus OD-CS20's
    automation-only refusal (workflows that do no build or test)."""
    assert [r["reason_code"] for r in spec["refusals"]] == [
        "no_workflow_yaml", "facts_unavailable", "automation_only"]


def test_score_formula_is_passed_over_applicable(spec):
    f = spec["formula"]
    assert set(f["check_states"]) == {"pass", "fail", "not_applicable"}
    assert "checks_passed / checks_applicable" in f["score"]
    assert "ROUND HALF UP" in f["rounding"]
    assert "self-verify" in f["rules"]["config_facts_only"]
    assert "never changes a state" in f["rules"]["evidence_display_only"]


# --- the band arithmetic carries over unchanged from the granular design ------

def test_bands_are_contiguous_and_cover_zero_to_one_hundred(spec):
    bands = sorted(spec["bands"], key=lambda b: b["min"])
    assert bands[0]["min"] == 0 and bands[-1]["max"] == 100
    for lower, upper in zip(bands, bands[1:]):
        assert upper["min"] == lower["max"] + 1, f"gap/overlap at {lower['grade']}"
    assert [b["grade"] for b in bands] == ["F", "D", "C", "B", "A"]


def test_every_score_maps_to_exactly_one_grade(spec):
    for value in range(0, 101):
        hits = []
        for band in spec["bands"]:
            for suffix, span in (("-", band.get("minus")), ("", band.get("bare")), ("+", band.get("plus"))):
                if span and span[0] <= value <= span[1]:
                    hits.append(band["grade"] + suffix)
        assert len(hits) == 1, f"score {value} maps to {hits}"


# --- the decision record and the punt must stay legible in the artifact -------

def test_od_cs15_records_the_reset_with_the_owner_directives(spec):
    od = next(d for d in spec["decision_log"] if d["id"] == "OD-CS15")
    assert len(od["owner_directives"]) == 4
    assert any("score any repo" in d for d in od["owner_directives"])
    assert any("every best practice" in d for d in od["owner_directives"])
    # The history stays: the superseded decisions' records are not erased.
    ids = {d["id"] for d in spec["decision_log"]}
    assert "OD-CS13" in ids


def test_v2_map_names_the_punted_machinery(spec):
    punted = " ".join(spec["v2_map"]["punted"])
    for phrase in ("measured-magnitude", "occurrence-ratio", "tiers and category weights",
                   "9-refusal", "queue-cause attribution"):
        assert phrase in punted, phrase


def test_stamp_schema_is_the_flat_check_list(spec):
    stamp = spec["stamp_schema"]
    for key in ("spec_version", "scope_statement", "value", "grade", "refusal",
                "checks", "checks_passed", "checks_applicable"):
        assert key in stamp
    check = stamp["checks"][0]
    assert "evidence" in check and "measured_note" in check


def test_changelog_records_the_reset(spec):
    assert any("OD-CS15 basic reset" in e["change"] for e in spec["changelog"])
    # Historical entries keep the version they shipped under; the LAST entry
    # must match the current version (every bump records itself).
    assert spec["changelog"][-1]["version"] == spec["spec_version"]
    for e in spec["changelog"]:
        assert re.fullmatch(r"ci-score-v\d+\.\d+(\.\d+)?", e["version"]), e["version"]


# --- surface consistency -------------------------------------------------------

def test_methodology_and_skill_md_describe_the_basic_rubric():
    m = re.sub(r"\s+", " ", _METHODOLOGY_PATH.read_text())
    assert "pass/fail" in m and "configuration" in m.lower()
    assert "13-check" not in m, "the granular canonical description survived in the methodology"
    sk = re.sub(r"\s+", " ", _SKILL_MD_PATH.read_text())
    assert "13-check" not in sk, "the granular canonical description survived in SKILL.md"


def test_scorer_module_is_basic_sized():
    """The reset's debuggability claim, kept honest: the scorer must stay
    small enough to read in one sitting. (607 lines was the granular design.)"""
    lines = len((_SCRIPTS_DIR / "ci_score.py").read_text().splitlines())
    assert lines < 200, f"ci_score.py is {lines} lines — the basic design is growing back"

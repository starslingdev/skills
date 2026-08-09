"""The why-these-ten doc and the scanner's active catalog cannot drift apart.

The critical-only contract ships its selection reasoning in
references/why-these-ten.md, as the catalog's admission contract requires.
That document is only trustworthy if it describes the catalog that actually
scans — so this census binds them: the doc's numbered vector list, the
catalog's pattern set, and the expected ten are asserted identical. Adding or
removing a pattern without updating the reasoning (or vice versa) fails here.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _scan_import import load_scan  # noqa: E402

load_catalog = load_scan().load_catalog

_CATALOG = _SKILL / "references" / "security-patterns.md"
_WHY = _SKILL / "references" / "why-these-ten.md"

THE_TEN = {
    "P14.10", "P14.9", "P14.7", "P14.11", "P14.14",
    "P14.15", "P14.18", "P14.19", "P14.24", "P14.25",
}


def _doc_patterns() -> set[str]:
    """Pattern ids from the doc's 'The ten' table (| # | Pattern | ... rows)."""
    ids: set[str] = set()
    for line in _WHY.read_text().splitlines():
        m = re.match(r"\|\s*\d+\s*\|\s*(P[\d.]+)\s*\|", line)
        if m:
            ids.add(m.group(1))
    return ids


def test_catalog_is_exactly_the_ten():
    entries = load_catalog(_CATALOG)
    assert {e.pattern for e in entries} == THE_TEN, (
        "the shipped catalog drifted from the approved keep-list — "
        "re-admitting or dropping a vector is a deliberate decision, and "
        "why-these-ten.md must change in the same PR"
    )


def test_doc_lists_exactly_the_ten():
    assert _doc_patterns() == THE_TEN, (
        "why-these-ten.md's vector table drifted from the approved keep-list"
    )


def test_every_entry_has_the_required_prose_sections():
    text = _CATALOG.read_text()
    sections = re.split(r"\n(?=### P)", text)
    seen = set()
    for s in sections:
        m = re.match(r"### (P[\d.]+)", s)
        if not m:
            continue
        pid = m.group(1)
        seen.add(pid)
        # Five markers: the fifth, `**Risk of the change.**`, is what the
        # report renders under the Fix block and passes to the fix agent —
        # a pattern without it ships a fix whose downside nobody stated.
        for marker in ("**TL;DR.**", "**What an attacker can do.**",
                       "**Anti-pattern**:", "**Fix recipe**",
                       "**Risk of the change.**"):
            assert marker in s, f"{pid}: missing required section {marker}"
    assert seen == THE_TEN


def test_reference_incidents_cite_only_live_patterns():
    """why-these-ten.md points readers at the catalog's Reference incidents
    section for "full incident citations", so every pattern id that section
    attributes ("Source for P14.7 and P14.9") must still exist. The descope
    left the section attributing incidents to P14.1 / P14.2 / P14.6 / P5.1 /
    P8.3 / P14.8 — ids a reader can no longer look up anywhere in the shipped
    skill. A census, not an existence check: the whole set is asserted.
    """
    text = _CATALOG.read_text()
    section = text.split("## Reference incidents", 1)
    assert len(section) == 2, "the catalog lost its Reference incidents section"
    cited = set()
    # Pattern ids contain a `.`, so the attribution runs to end-of-line, not
    # to the next period.
    for tail in re.findall(r"Source for (.*)$", section[1], re.M):
        cited.update(re.findall(r"P\d+\.\d+", tail))
    dangling = sorted(cited - THE_TEN)
    assert not dangling, (
        "Reference incidents attributes an incident to pattern(s) no longer in "
        f"the catalog: {dangling} — a reader cannot resolve them"
    )
    assert cited, "no 'Source for …' attributions found — did the format change?"


def test_verify_report_manifest_matches_the_catalog():
    """verify_report.py is standalone (it imports nothing from the skill), so
    it carries its own literal copy of the ten. Two copies of a manifest is
    exactly the shape that drifts — bind them here, or the report self-check
    silently starts validating against a stale set."""
    sys.path.insert(0, str(_SKILL / "tests"))
    import importlib.util as _ilu, sys as _sys
    from pathlib import Path as _P
    _spec = _ilu.spec_from_file_location(
        "ci_secure_verify_report", _P(__file__).resolve().parent / "verify_report.py")
    verify_report = _ilu.module_from_spec(_spec)
    _sys.modules["ci_secure_verify_report"] = verify_report
    _spec.loader.exec_module(verify_report)  # shadow-proof


    assert set(verify_report.THE_TEN) == THE_TEN, (
        "verify_report.py's THE_TEN drifted from the census manifest — the "
        "report self-check would validate against the wrong catalog"
    )


def _catalog_pattern_ids(text: str) -> set[str]:
    return set(re.findall(r"P\d+\.\d+", text))


def test_no_shipped_doc_cites_a_removed_pattern():
    """A whole-catalog + SKILL.md sweep, not a section-scoped one.

    The descope removed 15 patterns. A reader who meets `P8.3` or `P14.22` in
    shipped prose has no way to resolve it — the id exists nowhere in the
    installed skill. why-these-ten.md is exempt: naming the removed ids IS
    its rejection record.
    """
    for doc in (_CATALOG, _SKILL / "SKILL.md"):
        dangling = sorted(_catalog_pattern_ids(doc.read_text()) - THE_TEN)
        assert not dangling, (
            f"{doc.name} cites pattern id(s) that no longer exist anywhere in "
            f"the shipped skill: {dangling}"
        )


def test_every_catalog_anchor_link_resolves():
    """`](#p14…)` links inside the catalog must hit a real heading.

    The descope left at least one live link pointing at a deleted pattern's
    anchor, which renders on GitHub as a link that scrolls nowhere.
    """
    text = _CATALOG.read_text()
    # Slug via the scanner's own implementation — the one that builds the
    # `fix_recipe_anchor` every report links to. Reimplementing it here would
    # let the test pass on links the product then renders broken.
    github_slug = load_scan()._github_slug
    anchors = {
        github_slug(h) for h in re.findall(r"^#+\s+(.+?)\s*$", text, re.M)
    }
    broken = sorted(
        a for a in re.findall(r"\]\(#([^)]+)\)", text) if a not in anchors
    )
    assert not broken, f"catalog anchor link(s) resolve to no heading: {broken}"


# The frozen per-pattern severity manifest. A count-only census (`{HIGH: 8,
# MEDIUM: 2}`) passes a swap that keeps the totals — flip one HIGH to MEDIUM and
# one MEDIUM to HIGH and the counts are unchanged while two vectors are now
# mis-graded. Pinning each id's severity by EQUALITY closes that.
_SEVERITY_BY_PATTERN = {
    "P14.7": "HIGH",
    "P14.9": "HIGH",
    "P14.10": "HIGH",
    "P14.11": "HIGH",
    "P14.14": "HIGH",
    "P14.15": "HIGH",
    "P14.18": "HIGH",
    "P14.19": "HIGH",
    "P14.24": "MEDIUM",
    "P14.25": "MEDIUM",
}


def test_severity_census():
    """Every vector's severity is pinned by id, and any `**Severity**:` prose
    line inside a catalog entry agrees with that entry's METADATA severity.

    A count-only check ("two entries carry MEDIUM") once described a catalog it
    no longer matched, and could not catch a severity SWAP that keeps the
    totals. The per-pattern manifest and the prose/metadata agreement close
    both gaps.
    """
    entries = load_catalog(_CATALOG)
    got = {e.pattern: e.severity for e in entries}
    assert got == _SEVERITY_BY_PATTERN, (
        f"per-pattern severity manifest drifted: {got} — a severity change is "
        f"a deliberate catalog decision; update the manifest and the Severity "
        f"scale prose in the same change"
    )
    # Any `**Severity**: X` prose line in an entry must match its metadata.
    meta = {e.pattern: e.severity for e in entries}
    sev_re = re.compile(r"\*\*Severity\*\*:\s*([A-Z]+)")
    for pid, section in _sections().items():
        m = sev_re.search(section)
        if m:
            assert m.group(1) == meta[pid], (
                f"{pid}: prose says Severity {m.group(1)} but METADATA says "
                f"{meta[pid]} — the two must agree"
            )


def test_exactly_one_network_gated_detector():
    entries = load_catalog(_CATALOG)
    gated = {e.pattern for e in entries if e.detector == "gh-impostor-sha"}
    assert gated == {"P14.11"}, (
        "P14.11 is the one network-gated check; any change to that set is a "
        "contract change (the 'local, deterministic, seconds' claim is scoped "
        "to the other nine)"
    )


_PLATFORM_NOTE_PATTERNS = {"P14.7", "P14.9", "P14.18", "P14.25"}


def _sections() -> dict[str, str]:
    text = _CATALOG.read_text()
    out: dict[str, str] = {}
    for s in re.split(r"\n(?=### P)", text):
        m = re.match(r"### (P[\d.]+)", s)
        if m:
            out[m.group(1)] = s
    return out


def test_platform_notes_are_exactly_where_the_catalog_claims_them():
    """A census, not an existence check.

    A dated platform note is a claim that GitHub narrowed the vector. It must
    appear on exactly the entries a platform change actually touched — an
    extra one softens a vector nothing closed, a missing one leaves a
    github.com maintainer reading an unconditioned claim.
    """
    have = {
        pid for pid, body in _sections().items()
        if re.search(r"(?m)^\*\*Platform change", body)
    }
    assert have == _PLATFORM_NOTE_PATTERNS, (
        f"platform notes present on {sorted(have)}, expected "
        f"{sorted(_PLATFORM_NOTE_PATTERNS)}"
    )


def test_every_platform_note_carries_a_date_and_a_residual():
    """Dated and claim-scoped: a note that says "GitHub fixed this" with no
    date and no residual would retire a live vector in the reader's head."""
    for pid, body in _sections().items():
        m = re.search(r"(?m)^\*\*Platform change[^\n]*(?:\n(?!\s*\n)[^\n]*)*", body)
        if not m:
            continue
        note = " ".join(m.group(0).split())
        assert re.search(r"20\d\d", note), f"{pid}: platform note carries no date"
        assert re.search(
            r"remains live|still fires|intact by default|outside the change|"
            r"does NOT receive|opt-in|residual",
            note,
        ), f"{pid}: platform note states no residual — it reads as a retirement"


def test_the_rejection_record_accounts_for_the_catalog_size_it_claims():
    """The opening line states how big the old catalog was; the rejection
    record below it is the ONLY evidence for that number, and the two disagreed
    three ways — the prose said "~27 patterns", a sibling test said "removed
    19", and the record itself names 15. A claim ABOUT the data has to be
    checked AGAINST the data, so count the record instead of trusting prose.
    """
    text = _WHY.read_text()
    named = _catalog_pattern_ids(text)
    removed = named - THE_TEN
    assert THE_TEN <= named, (
        "why-these-ten.md must name every kept pattern: "
        f"missing {sorted(THE_TEN - named)}")
    assert len(removed) == 15, (
        "the rejection record names a different number of removed patterns "
        f"than the prose claims: {len(removed)} — {sorted(removed)}")
    assert f"not the {len(named)} patterns" in text, (
        f"the opening line must say {len(named)} — ten kept plus the "
        f"{len(removed)} the record accounts for")
    assert f"the {len(removed)} the rejection record below" in text


# Every incident why-these-ten.md names, as the token that identifies it. A
# FROZEN manifest, not a parse of the prose: the point is to catch an incident
# that gets cited in one document and never recorded in the other, and a
# regex over free text would drift with the wording it is supposed to police.
# Adding an incident citation means adding it here AND to the catalog's
# Reference incidents section, in the same change.
_CITED_INCIDENTS = (
    "s1ngularity",
    "elementary-data",
    "Ultralytics",
    "Security Lab",
    "Trivy",
    "TanStack",
    "tj-actions",
    "Chainguard",
    "Codecov",
    "Miasma",
    "July 28 2026",
)


def test_every_incident_why_these_ten_cites_is_recorded_in_the_catalog():
    """The catalog states the invariant — "Every vector in why-these-ten.md
    cites its incidents from this list" — and it was false: the Miasma /
    `@redhat-cloud-services` compromise was cited twice in why-these-ten.md
    and appeared in Reference incidents only inside another entry's prose,
    behind a URL that documents npm's defaults change rather than the
    compromise. A skill that cites an incident it does not record cannot show
    the reader where the claim came from.
    """
    why = _WHY.read_text()
    section = _CATALOG.read_text().split("## Reference incidents", 1)
    assert len(section) == 2, "the catalog lost its Reference incidents section"
    # Match against the ENTRY TITLES (`- **Name (date)** — …`), not the whole
    # section: a passing mention inside another entry's prose is exactly the
    # shape the Miasma gap had, and would satisfy a substring check over the
    # section while leaving the reader with no source for the incident.
    titles = "\n".join(re.findall(r"^- \*\*(.+?)\*\*", section[1], re.M))
    assert titles.count("\n") >= 10, "Reference incidents entry format changed"
    for token in _CITED_INCIDENTS:
        assert token in why, (
            f"{token!r} is in the manifest but why-these-ten.md no longer "
            "cites it — drop it from the manifest in the same change")
        assert token in titles, (
            f"why-these-ten.md cites {token!r}, but no entry in the catalog's "
            "Reference incidents section is ABOUT it — the reader has no source "
            "to follow (a mention inside another entry's prose is not one)")

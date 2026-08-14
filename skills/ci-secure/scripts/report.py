#!/usr/bin/env python3
"""ci-secure report generator.

Reads findings JSON from stdin (produced by scan.py) and writes a
markdown report that's useful as a standalone deliverable — not just a
triage list. For every finding the renderer extracts the catalog's
anti-pattern paragraph, fix-recipe YAML block, and reference incidents,
and inlines them so a reader doesn't need to flip back to the catalog
to understand the finding.

When the findings JSON carries a `repo` and `commit_sha`, every "Source"
line is rendered as a github.com permalink at that commit. Otherwise it
falls back to `path:line`.

Format: the house format its siblings (ci-score, ci-speedup) use — a
question-form title naming the REPO, a label-style provenance table, a
pre-drawn banner the orchestrator copies verbatim to the terminal (never
redrawn by hand), and findings that open with a bold stakes-first line over
a bulleted definition list (never a `| Field | Value |` table — catalog
prose contains hard newlines, which a table cell cannot hold). The chain-status
table lists all ten vectors, clean ones included: a findings table alone
cannot distinguish "checked and clean" from "never checked".

Scope contract: ci-secure checks critical exploit chains only, and the
report says so verbatim in its header. EVERY finding group renders —
there is no priority trim — and a network-gated check that did not run
(`gh_checks`) renders loudly as NOT a pass. Zero findings is a
first-class positive result, never an empty-looking file.

Usage:
    scan.py | report.py > ci-secure-report.md
    report.py --in findings.json --out report.md
    report.py --in findings.json --render-plan   # the render order + dormancy
    report.py --in findings.json --catalog PATH --out report.md

Reads findings from ``--in`` (default: stdin) and writes to ``--out``
(default: stdout). ``--catalog`` overrides the bundled catalog the prose
is extracted from; ``--render-plan`` prints the group list instead of a
report.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from config import (  # noqa: E402
    ACTIVITY_RUN_LIMIT,
    DORMANT_DAYS,
    __version__ as SKILL_VERSION,
    setup_logging,
)

logger = setup_logging(__name__)

_SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "MANUAL": 3}
_VALID_SEVERITIES = frozenset(_SEVERITY_ORDER)
# Keys every well-formed finding must carry (the `Finding` base contract in
# scan.py). A finding missing one of these would render with a silent default
# (severity → MANUAL, line → 0), burying a real issue at the bottom of the
# report — so report.py validates against this set at its JSON-load boundary
# and surfaces violations loudly instead.
_REQUIRED_FINDING_KEYS = (
    "id", "pattern", "severity", "title", "workflow_file", "line",
)
_SEVERITY_EMOJI = {
    "HIGH": "🟥",
    "MEDIUM": "🟧",
    "LOW": "⬜",   # grey/neutral — LOW is a real tier but lowest priority
    "MANUAL": "📄",  # documentation-only entry, not a fixable tier
}

# The one honest thing this report must always say about its own scope:
# ci-secure checks a small set of critical exploit chains, deliberately.
# Rendered verbatim in every report — including the zero-findings one, where
# a reader is most likely to mistake "nothing found" for "nothing to find".
_SCOPE_HONESTY_LINE = (
    "Critical exploit-chain checks only — this is not a comprehensive audit."
)

# Human labels for the network-gated checks reported in the scan's
# `gh_checks` map. Falls back to the bare pattern id when unmapped.
_GH_CHECK_LABELS = {
    "P14.11": "impostor-SHA",
}

# Every finding comes from one source now (the ci-secure catalog). The
# constant survives as the group key's first slot, which keeps the grouping
# code shape-stable; it is NOT part of any emitted payload (there is no
# in-tree caller that consumes a source field).
_SOURCE = "ci-secure"

# The catalog the report links to. By DEFAULT this is the published catalog
# on the public skills repo's main branch — a URL the reader can open from a
# report saved anywhere (tmp, the repo root, a PR body).
#
# Two prior schemes both failed: a permalink pinned to the SKILL'S OWN commit
# 404'd whenever that SHA wasn't a public-repo commit (the report checks only
# that anchors are well formed, not that the host serves them), and a bare
# relative path resolved nowhere, because the report is not written next to
# the catalog. Main-branch links can drift with catalog edits, but the
# anchors are pattern ids, which are stable. `--catalog-url` overrides for a
# caller hosting the catalog elsewhere.
_CATALOG_PUBLIC_URL = ("https://github.com/starslingdev/skills/blob/main/"
                       "skills/ci-secure/references/security-patterns.md")


def _build_catalog_url(skill_commit_sha: str | None) -> str:
    # `skill_commit_sha` is still accepted (and still stamped into the
    # provenance row) so the caller shape is unchanged; the default link does
    # not depend on it — the skill's own commit is not a public-repo SHA.
    return _CATALOG_PUBLIC_URL


# =============================================================================
# Catalog prose extraction
# =============================================================================

_PATTERN_HEADING_RE = re.compile(r"^###\s+(P\d+(?:\.\d+)?)\s+(.*?)\s*$", re.MULTILINE)
# A top-level `## ` heading — exactly two hashes, so a `### P` pattern
# heading never matches.
_NEXT_H2_RE = re.compile(r"^##\s", re.MULTILINE)


def _split_catalog_sections(catalog_text: str) -> dict[str, str]:
    """Return a {pattern_id -> section text} map of the catalog.

    A section ends at whichever comes first: the next ``### P`` pattern
    heading, or the next top-level ``## `` heading. Without the second
    bound the LAST pattern swallows everything after it — for this catalog
    the whole ``## Reference incidents`` list — and its rendered finding
    cites eight incidents belonging to other chains.
    """
    sections: dict[str, str] = {}
    headings = [
        (m.group(1), m.start(), m.end())
        for m in _PATTERN_HEADING_RE.finditer(catalog_text)
    ]
    for i, (pid, start, heading_end) in enumerate(headings):
        end = headings[i + 1][1] if i + 1 < len(headings) else len(catalog_text)
        # Search for the terminating `## ` from AFTER this section's own
        # heading line so a `### ` heading can't terminate itself.
        h2 = _NEXT_H2_RE.search(catalog_text, heading_end, end)
        if h2:
            end = h2.start()
        sections[pid] = catalog_text[start:end]
    return sections


_REF_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_PRIVATE_DOMAINS = ("localhost", "127.0.0.1", "internal.")


class _ExtractedSection:
    """Holds prose snippets pulled from one catalog pattern section."""

    __slots__ = (
        "tldr",
        "attacker_capability",
        "anti_pattern",
        "fix_recipe_summary",
        "fix_recipe_yaml",
        "risk_of_change",
        "platform_note",
        "references",
        "heading_text",
        "missing_markers",
        "fix_surface",
    )

    def __init__(self) -> None:
        self.tldr: str = ""
        self.attacker_capability: str = ""
        self.anti_pattern: str = ""
        self.fix_recipe_summary: str = ""
        self.fix_recipe_yaml: str = ""
        self.risk_of_change: str = ""
        # Optional: a dated paragraph recording a PLATFORM change that
        # narrowed this vector, plus the residuals it leaves open. It is not
        # a required marker (most entries have none) — but where the catalog
        # carries one it MUST reach the rendered finding, or a github.com
        # maintainer reads an unconditioned claim about a chain the platform
        # already partly closed.
        self.platform_note: str = ""
        self.references: list[tuple[str, str]] = []
        self.heading_text: str = ""
        # "yaml" / "non-yaml" from the section's METADATA, or "" when the
        # catalog does not declare it. Declared, never inferred — see
        # `_build_fix_prompt`.
        self.fix_surface: str = ""
        # Catalog markers the extractor could not find in this section.
        # Non-empty means the section is damaged: the renderer says so in
        # words rather than substituting neighbouring prose under the
        # missing marker's label (which reads as authored content).
        self.missing_markers: list[str] = []


_FIX_SURFACE_RE = re.compile(r"(?m)^fix-surface:\s*(yaml|non-yaml)\s*$")

_NEXT_BOLD_MARKER_RE = re.compile(r"\n\s*\*\*[A-Z][^*]+\*\*\s*:?", re.MULTILINE)


def _section_until_next_marker(section: str, marker_re: re.Pattern[str]) -> str:
    """Return everything after a bold marker up to the next bold marker header.

    Preserves blank lines and tables between the marker and the next
    `**SomeHeading**:` — for P14.10 that means the attacker-controllable
    source list survives intact in the rendered report instead of being
    truncated at the first blank line.
    """
    m = marker_re.search(section)
    if not m:
        return ""
    tail = section[m.end():]
    # Strip a leading newline that often sits between `**Anti-pattern**:` and
    # the prose so we don't render a leading blank.
    tail = tail.lstrip("\n")
    end_idx = len(tail)
    next_marker = _NEXT_BOLD_MARKER_RE.search(tail)
    if next_marker:
        end_idx = min(end_idx, next_marker.start())
    next_heading = re.search(r"^##+\s", tail, re.MULTILINE)
    if next_heading:
        end_idx = min(end_idx, next_heading.start())
    return tail[:end_idx].rstrip()


_TLDR_RE = re.compile(r"\*\*TL;DR[.:]?\*\*\s*\.?\s*", re.IGNORECASE)
_ATTACKER_CAPABILITY_RE = re.compile(
    r"\*\*What an attacker can do[.:]?\*\*\s*", re.IGNORECASE
)
_ANTI_PATTERN_RE = re.compile(r"\*\*Anti-pattern\*\*\s*:?\s*", re.IGNORECASE)
_FIX_RECIPE_RE = re.compile(r"\*\*Fix recipe\*\*\s*:?\s*", re.IGNORECASE)
# The honest downside of applying the fix — one authored sentence per catalog
# entry. The sibling skills (ci-score) carry a `Risk of the change` note on
# every recommendation for the same reason: a fix recipe with no stated
# downside reads as free, and a reader who discovers the cost afterwards
# stops trusting the rest of the report.
_RISK_OF_CHANGE_RE = re.compile(
    r"\*\*Risk of the change[.:]?\*\*\s*", re.IGNORECASE
)
_YAML_BLOCK_RE = re.compile(r"```yaml\n(.*?)\n```", re.DOTALL)

# `**Platform change, <date>…**` — the whole paragraph, kept together so the
# date and the residuals travel with the claim. Deliberately NOT a required
# marker: absence means "no platform change to report", which is the normal
# case, and inventing one would be worse than silence.
_PLATFORM_NOTE_RE = re.compile(
    r"(?m)^\*\*Platform change[^\n]*(?:\n(?!\s*\n)[^\n]*)*"
)


def _extract_platform_note(section: str) -> str:
    """The dated platform-mitigation paragraph, or "".

    The leading bold is unwrapped so the renderer can put the paragraph
    behind its own label without printing two bold lead-ins in a row.
    """
    m = _PLATFORM_NOTE_RE.search(section)
    if not m:
        return ""
    para = " ".join(m.group(0).split())
    return re.sub(r"^\*\*(Platform change[^*]*)\*\*\s*", r"\1 ", para).strip()


def _extract_first_yaml_block_after_fix_recipe(section: str) -> str:
    """Return the first ```yaml block that appears after the `**Fix recipe**` marker.

    Falls back to "" when the recipe carries no ```yaml block (e.g.
    P14.11, whose recipe is a ```bash verification sequence).
    """
    fix_match = _FIX_RECIPE_RE.search(section)
    if not fix_match:
        return ""
    tail = section[fix_match.end():]
    block_match = _YAML_BLOCK_RE.search(tail)
    if not block_match:
        return ""
    return block_match.group(1).strip()


_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+\.|[-*])\s+\S")


def _extract_fix_recipe_summary(section: str) -> str:
    """Return the first paragraph right after the `**Fix recipe**:` marker.

    The catalog convention is: `**Fix recipe**:` is followed by a short
    plain-English headline paragraph, then optionally a colon-introduced
    second paragraph and a ```yaml block, then more prose. The headline
    paragraph is what we want for the report's `#### Fix` section —
    short enough to render inline, specific enough to tell the reader
    what the change is. Later paragraphs typically lead into the YAML
    block ("...as follows:") and render awkwardly when shown inline
    without the block. Falls back to "" when no fix-recipe marker is
    present.
    """
    fix_match = _FIX_RECIPE_RE.search(section)
    if not fix_match:
        return ""
    tail = section[fix_match.end():].lstrip("\n")
    blocks = re.split(r"\n\s*\n", tail)
    para = blocks[0].strip()
    # A colon-introduced lead-in ("Two structural options:", "Fix recipe (in
    # order of preference):") is followed in the catalog by the numbered
    # options that ARE the fix. Dropping the separator and stopping there left
    # the report saying "Two structural options. See [catalog]…" — a dangling
    # clause that told the reader nothing. Carry the options through.
    if para.endswith((":", ",", ";")) and len(blocks) > 1:
        options = blocks[1].strip()
        first_line = options.splitlines()[0] if options else ""
        if _LIST_ITEM_RE.match(first_line):
            # The catalog writes P14.7's lead-in as a parenthetical that only
            # reads as a sentence attached to the "Fix recipe" label it
            # follows; restore the label so the block does not open with
            # "(in order of preference):".
            if para.startswith("("):
                para = f"Fix recipe {para}"
            return f"{para}\n\n{options}"
    # No list follows (the lead-in runs into a code block): drop the trailing
    # separator and end the sentence, so the summary and the catalog pointer
    # read as two sentences.
    para = re.sub(r"[,;:]+\s*$", "", para)
    if para and para[-1] not in ".!?":
        para += "."
    return para


def _extract_references(section: str) -> list[tuple[str, str]]:
    """Return (anchor_text, url) tuples for every external link in the section.

    De-duplicates by URL, preserving order. Skips intra-document anchors
    and obvious internal-only hosts.
    """
    seen: dict[str, str] = {}
    for m in _REF_LINK_RE.finditer(section):
        text, url = m.group(1), m.group(2)
        if any(d in url for d in _PRIVATE_DOMAINS):
            continue
        if url in seen:
            continue
        seen[url] = text
    return [(text, url) for url, text in seen.items()][:8]


# The five prose markers every catalog section must carry, in the order the
# report reads them. Label → the marker regex that finds it.
_REQUIRED_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("TL;DR", _TLDR_RE),
    ("What an attacker can do", _ATTACKER_CAPABILITY_RE),
    ("Anti-pattern", _ANTI_PATTERN_RE),
    ("Fix recipe", _FIX_RECIPE_RE),
    ("Risk of the change", _RISK_OF_CHANGE_RE),
)


def unparsed_section_note(pattern: str, marker: str) -> str:
    """The visible stand-in for prose the catalog should have supplied.

    Rendering neighbouring text under the missing marker's label — the old
    ``cat.tldr or cat.anti_pattern`` fallback — presents anti-pattern prose
    as an authored TL;DR, which a reader cannot tell from the real thing.
    Say the section is damaged instead.
    """
    return (
        f"_(catalog section for {pattern}: **{marker}** could not be parsed "
        f"— reinstall the skill)_"
    )


def _extract_section(section_text: str) -> _ExtractedSection:
    out = _ExtractedSection()
    heading = _PATTERN_HEADING_RE.search(section_text)
    if heading:
        # group(2) usually starts with "— Title"; strip leading dash/em-dash/
        # en-dash + spaces so we don't render "P14.10 — — Title".
        title_tail = heading.group(2).lstrip("-—– ").strip()
        out.heading_text = f"{heading.group(1)} — {title_tail}" if title_tail else heading.group(1)
    out.tldr = _section_until_next_marker(section_text, _TLDR_RE)
    out.attacker_capability = _section_until_next_marker(
        section_text, _ATTACKER_CAPABILITY_RE
    )
    out.anti_pattern = _section_until_next_marker(section_text, _ANTI_PATTERN_RE)
    out.platform_note = _extract_platform_note(section_text)
    out.fix_recipe_summary = _extract_fix_recipe_summary(section_text)
    out.fix_recipe_yaml = _extract_first_yaml_block_after_fix_recipe(section_text)
    # One authored sentence, flattened so it renders inside a report line.
    # The marker is the LAST one in a catalog section, so its span runs to the
    # section boundary and would otherwise sweep up the trailing `---` rule
    # ("…accepts the real one. ---"); take the first paragraph only.
    risk_span = _section_until_next_marker(section_text, _RISK_OF_CHANGE_RE)
    out.risk_of_change = " ".join(
        re.split(r"\n\s*\n", risk_span, maxsplit=1)[0].split()
    )
    surface = _FIX_SURFACE_RE.search(section_text)
    out.fix_surface = surface.group(1).lower() if surface else ""
    out.references = _extract_references(section_text)
    out.missing_markers = [
        label for label, marker_re in _REQUIRED_MARKERS
        if not marker_re.search(section_text)
    ]
    return out


# =============================================================================
# Grouping: collapse repeated occurrences of the same underlying rule
# =============================================================================

# Two findings belong to the same group when their pattern (the catalog
# id, e.g. ``P14.10``) matches. The grouping renders one consolidated
# entry per rule, with per-occurrence evidence rows under it — so a repo
# with 10 P14.10 hits across 6 workflows reads as one finding, not ten.
# The key stays a ``(source, pattern)`` tuple with a constant source so the
# grouping and sorting code has one shape to handle.


def _group_key(finding: dict[str, Any]) -> tuple[str, str]:
    return (_SOURCE, finding.get("pattern", ""))


def _group_findings(
    findings: list[dict[str, Any]],
) -> list[tuple[tuple[str, str], list[dict[str, Any]]]]:
    """Collapse findings into (group_key, members) pairs, severity-sorted.

    Within a group, members keep their original order. Across groups,
    sort by severity (HIGH → MANUAL), then pattern id for stable
    tie-breaks.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    insertion_order: list[tuple[str, str]] = []
    for f in findings:
        key = _group_key(f)
        if key not in groups:
            groups[key] = []
            insertion_order.append(key)
        groups[key].append(f)

    def sort_key(item: tuple[tuple[str, str], list[dict[str, Any]]]):
        (_source, pattern), members = item
        return (_SEVERITY_ORDER.get(_group_severity(members), 99), pattern)

    logger.debug(
        "grouped %d findings into %d groups", len(findings), len(groups),
    )
    return sorted(
        ((k, groups[k]) for k in insertion_order),
        key=sort_key,
    )


def _group_severity(members: list[dict[str, Any]]) -> str:
    """Worst (highest-priority) severity across the group's members.

    Findings of one catalog pattern share a catalog-authored severity, so
    in practice this is ``members[0]``. Taking the worst member's severity
    keeps the group's headline honest even if a producer ever patches a
    per-occurrence severity: a group containing any HIGH reads as HIGH,
    and never understates by picking whatever happened to land first. The
    per-occurrence split is surfaced separately (see
    ``_occurrence_severity_breakdown``) so the group label and the
    header's per-occurrence counts reconcile.
    """
    return min(
        (m.get("severity", "MANUAL") for m in members),
        key=lambda s: _SEVERITY_ORDER.get(s, 99),
        default="MANUAL",
    )


def _occurrence_severity_breakdown(members: list[dict[str, Any]]) -> str:
    """Per-severity occurrence split, e.g. ``3 HIGH · 100 MEDIUM · 102 LOW``.

    Returns "" when every occurrence shares one severity (the common case,
    and all ci-secure groups) — the breakdown is only meaningful for a
    mixed group. When non-empty it lets the reader reconcile a group's
    headline severity (the worst member, via ``_group_severity``) and its
    occurrence count against the report header's per-occurrence severity
    totals — otherwise a 205-occurrence group stamped HIGH looks like 205
    HIGH occurrences when only some are.
    """
    counts = Counter(m.get("severity", "MANUAL") for m in members)
    if len(counts) <= 1:
        return ""
    return " · ".join(
        f"{counts[s]} {s}" for s in ("HIGH", "MEDIUM", "LOW", "MANUAL") if counts.get(s)
    )


def _group_attacker_scenario(members: list[dict[str, Any]]) -> str:
    return members[0].get("attacker_scenario") or ""


def _group_is_all_dormant(members: list[dict[str, Any]]) -> bool:
    return all(
        (m.get("workflow_activity") or {}).get("dormant") is True
        for m in members
    )


_CATALOG_HEADING_RE = re.compile(r"^P\d+(?:\.\d+)?\s+—\s+(.+)$")


def _group_short_title(
    members: list[dict[str, Any]],
    catalog_sections: dict[str, _ExtractedSection],
) -> str:
    """The h4 title for a group — a single descriptive sentence-case
    phrase, with no per-file specifics."""
    first = members[0]
    pattern = first.get("pattern", "")
    cat = catalog_sections.get(pattern, _ExtractedSection())
    heading = cat.heading_text or pattern
    # catalog heading shape: "P14.10 — Template Injection in `run:` Blocks"
    m = _CATALOG_HEADING_RE.match(heading)
    return m.group(1) if m else heading


def _group_anchor(ordinal: int) -> str:
    """Stable anchor for a finding group, independent of fix state.

    Emitted as ``<a id="finding-{ordinal}"></a>`` right after each section
    heading, so every link to it survives the orchestrator's heading edits.
    An anchor derived from the heading text (via GitHub's slug algorithm)
    would shift the moment the orchestrator inserts ``FIXED — `` into the
    heading, leaving every reference a dead link by the end of a fix
    dispatch session.
    """
    return f"finding-{ordinal}"


# =============================================================================
# Source citation
# =============================================================================

def _permalink(
    repo: str | None, sha: str | None, path: str, line: int
) -> str | None:
    if not repo or not sha or not path:
        return None
    return f"https://github.com/{repo}/blob/{sha}/{path}#L{line}"


def _source_line(
    repo: str | None, sha: str | None, workflow_file: str, line: int
) -> str:
    """One evidence citation — a permalink when we can build one, else `path:line`.

    No per-bullet ``(commit abc1234)`` suffix: the audited commit is stated
    once in the provenance table (which says in words that file & line
    references are anchored to that tree) and again inside the permalink
    itself, so repeating it on every bullet is restatement, not provenance.
    """
    link = _permalink(repo, sha, workflow_file, line)
    label = f"`{_flatten_scanned(workflow_file)}:{line}`"
    if link:
        return f"[{label}]({link})"
    return label


def _chain_anchor(pattern_id: str) -> str:
    """Stable anchor for a chain's appendix entry (`P14.7` → `chain-p14-7`).

    The vector map's ✅ rows link here: a reader who sees "checked and clean"
    must be one click from what was actually checked, or the clean result is
    an unfalsifiable claim.
    """
    return "chain-" + pattern_id.lower().replace(".", "-")


def _catalog_anchor_url(catalog_url: str, anchor: str) -> str:
    if not catalog_url:
        return f"#{anchor}"
    return f"{catalog_url}#{anchor}"


# =============================================================================
# Per-finding render
# =============================================================================

def _activity_is_unavailable(activity: dict[str, Any]) -> bool:
    """True when activity enrichment was attempted but failed.

    ``fetch_workflow_activity`` returns ``{"status": "unavailable", ...}``
    on a gh API failure (vs. ``{}`` when enrichment was never run). The
    distinction keeps a finding whose activity *couldn't be checked* from
    being read as active — neither active nor dormant, just unknown.
    """
    return isinstance(activity, dict) and activity.get("status") == "unavailable"


def _activity_is_reusable(activity: dict[str, Any]) -> bool:
    """True for the ONE unknown-activity case that is not a failure: a reusable
    (`on: workflow_call`-only) workflow whose runs GitHub books against its
    callers, so its own run history is empty however often it executes."""
    return isinstance(activity, dict) and activity.get("reusable_workflow") is True


def _indent_block(text: str, indent: str = "  ") -> str:
    """Prefix each line of ``text`` with ``indent``.

    Used for fenced code blocks nested inside list items — GFM requires
    the fences and every code line to be indented to the list-item
    content column, otherwise the code spills back out of the bullet.
    """
    return "\n".join(f"{indent}{line}" if line else indent.rstrip() for line in text.splitlines())


def _occurrence_bullet_for_prompt(member: dict[str, Any]) -> str:
    """One bullet per occurrence, formatted for the agent prompt body.

    Same shape as the report's evidence bullets but without permalinks
    or activity counts — the agent only needs file:line + jobs to act.
    """
    wf = _flatten_scanned(member.get("workflow_file", ""))
    line_no = member.get("line", 0)
    jobs = member.get("affected_jobs") or []
    suffix = (
        f" — jobs: {', '.join(_flatten_scanned(j) for j in jobs)}" if jobs else ""
    )
    note = member.get("affected_jobs_note")
    if note:
        suffix += f" ({note})"
    return f"- {wf}:{line_no}{suffix}"


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


_DERIVED_GUTTER_RE = re.compile(r"^\s*(\d+):\s?")


def _derived_evidence_block(evidence: str, indent: str = "  ") -> str:
    """A derived claim as a plain blockquote, labelled, with its line in prose.

    The scanner formats derived evidence with the same `  13: … <-- here`
    gutter the verbatim quoters use, because both go through one evidence
    field. Strip the gutter here and state the line in words instead — the
    claim is about the file, not a quotation from it.
    """
    lines: list[str] = []
    for raw in evidence.splitlines():
        line_no = None
        m = _DERIVED_GUTTER_RE.match(raw)
        text = raw
        if m:
            line_no = m.group(1)
            text = raw[m.end():]
        text = text.replace(" <-- here", "").strip()
        if not text:
            continue
        if line_no and not lines:
            text = f"{text} (line {line_no})"
        lines.append(text)
    if not lines:
        # Everything the stripper touched came out empty — evidence that is
        # entirely gutter, or entirely the `<-- here` marker. Returning ""
        # rendered the occurrence with NO evidence at all, which is the one
        # thing an evidence block must never do. Fall back to the original
        # text, labelled, so the reader sees exactly what the scanner held.
        lines = [" ".join(evidence.split())] if evidence.strip() else []
        if not lines:
            return ""
    body = "\n".join(f"{indent}> {ln}" for ln in lines)
    return f"{indent}> **derived** — assembled by the scanner, not quoted source:\n{body}"


_FALLBACK_SCENARIO_SUFFIX = (
    "_(catalog description — run the full skill for a repo-specific "
    "scenario)_"
)


def _stakes_line(attacker_text: str) -> str:
    """The bold stakes-first one-liner that opens a finding section.

    Derived from the finding's own attacker text (the orchestrator's
    repo-grounded scenario when present, the catalog's capability line
    otherwise) by taking its opening sentence — the part that states who the
    attacker is and what they get. A second sentence is pulled in only when
    the first is too short to stand alone. Nothing is invented: if there is
    no attacker text, there is no stakes line.
    """
    text = " ".join((attacker_text or "").split())
    if not text or text.startswith("_(catalog section"):
        return ""
    parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p]
    if not parts:
        return ""
    stakes = parts[0]
    if len(stakes) < 60 and len(parts) > 1:
        stakes = f"{stakes} {parts[1]}"
    return stakes


def _risk_of_change(pattern: str, cat: _ExtractedSection) -> str:
    """The catalog's authored 'what this fix could break' sentence.

    Never fabricated and never borrowed from a neighbouring marker: a damaged
    section says so, in the same words the other markers use.
    """
    if cat.risk_of_change:
        return cat.risk_of_change
    if "Risk of the change" in cat.missing_markers:
        return unparsed_section_note(pattern, "Risk of the change")
    return ""


def _build_fix_prompt(
    members: list[dict[str, Any]],
    pattern: str,
    severity: str,
    short_title: str,
    catalog_url: str,
    cat: _ExtractedSection,
    findings_path: Path | None = None,
) -> str:
    """Self-contained prompt the user copies into their coding agent.

    Includes enough context for an agent that has never seen this
    repo's report to apply the fix: pattern id + severity + plain-
    English context, full occurrence list, the catalog recipe, and the
    standard ci-secure constraints (touch only listed files, leave
    changes in working tree).
    """
    n = len(members)
    occurrences_block = "\n".join(_occurrence_bullet_for_prompt(m) for m in members)
    # Full path, not `<findings.json>`: the file lives under `$TMPDIR` and a
    # dispatched subagent cannot guess the directory from a placeholder. Shell-
    # quoted because it goes into a command line the agent RUNS — a `$TMPDIR`
    # with a space in it would otherwise split into two arguments.
    # The recheck (the fix oracle) writes to a REPO-SCOPED path derived from the
    # findings path's own slug — NOT a fixed `/tmp/ci-secure-recheck.json`, which
    # two concurrent sessions on different repos would clobber. When there is no
    # findings path (piped input) fall back to the SKILL.md-documented shape.
    if findings_path:
        recheck_path = findings_path.with_name(
            findings_path.name.replace("findings", "recheck")
        )
        recheck_ref = shlex.quote(str(recheck_path))
    else:
        recheck_ref = "${TMPDIR:-/tmp}/ci-secure-recheck-${SLUG}.json"
    # P14.11 is the ONE network-gated vector. Its recheck MUST force
    # `--gh-impostor on` (default `auto` would silently SKIP the impostor check
    # when gh is unauthenticated, and a skipped check re-read as "gone" is a
    # VACUOUS pass) — the exact hole prompts.md was hardened against. Other
    # vectors answer from YAML alone, so forcing `on` there would only make the
    # oracle refuse to run without gh for no security gain.
    is_impostor = pattern == "P14.11"
    gh_flag = " --gh-impostor on" if is_impostor else ""
    impostor_guard = (
        "\n    Because this is the network-gated impostor-SHA vector, read "
        f"`gh_checks[\"P14.11\"]` in {recheck_ref}: only `\"ran\"` verifies the "
        "fix. `skipped` / `partial` / a missing status means the check did NOT "
        "run — treat that as NOT verified, never as a pass (re-run with gh "
        "authenticated)."
        if is_impostor else ""
    )
    risk = _risk_of_change(pattern, cat)
    risk_constraint = f"- Risk of the change: {risk}\n" if risk else ""

    tldr = (cat.tldr or "").strip()
    context_line = tldr if tldr else f"Pattern `{pattern}` from the ci-secure catalog."
    recipe_yaml = (cat.fix_recipe_yaml or "").strip()
    if recipe_yaml:
        recipe_section = (
            f"Catalog reference: {catalog_url}\n\n"
            "Recipe (from the catalog):\n\n"
            "```yaml\n"
            f"{recipe_yaml}\n"
            "```"
        )
    elif "Fix recipe" in cat.missing_markers:
        # Don't tell the agent the fix is a non-YAML org setting when the
        # truth is that the catalog section failed to parse.
        recipe_section = (
            f"Catalog reference: {catalog_url}\n\n"
            f"The local catalog section for {pattern} could not be parsed "
            "(no Fix recipe marker found) — reinstall the skill, or read the "
            "catalog entry above and apply the documented fix."
        )
    elif cat.fix_surface == "non-yaml":
        recipe_section = (
            f"Catalog reference: {catalog_url}\n\n"
            "This pattern's fix is non-YAML (org-level setting, "
            "registry configuration, or manual review). Read the "
            "catalog entry above and apply the documented fix."
        )
    else:
        # A prose-only recipe is not a non-YAML fix. This branch used to fire
        # on "the recipe has no fenced yaml block", so P14.7's and P14.18's
        # workflow restructures were announced to the agent as org-level
        # settings — while the constraints below told it to edit workflow
        # files. The surface is now declared by the catalog (`fix-surface:`),
        # and an undeclared surface says nothing about YAML either way.
        recipe_section = (
            f"Catalog reference: {catalog_url}\n\n"
            "The catalog entry describes this fix in prose rather than a "
            "single YAML snippet — apply the catalog's documented "
            "restructure to the workflow files listed above."
        )

    return (
        f"You are fixing every occurrence of ci-secure finding "
        f"`{pattern}` ({severity}) — {short_title} — in this repository.\n\n"
        f"Context: {context_line}\n\n"
        f"Occurrences ({n}):\n{occurrences_block}\n\n"
        f"{recipe_section}\n\n"
        "Constraints:\n"
        f"{risk_constraint}"
        "- Modify ONLY the workflow files listed above. Do not touch "
        "any other file.\n"
        "- Do not widen the patch beyond what the recipe specifies. If "
        "you spot a sibling issue in the same file, mention it in "
        "your summary but do not fix it here.\n"
        "- Do not commit, push, or open a PR. Leave the changes in "
        "the working tree for human review.\n"
        "- If the recipe is ambiguous for a specific file, stop and "
        "ask before guessing.\n\n"
        "When done, print a 3-line summary: which files changed, what "
        "the shared fix was, and any follow-up the user should verify "
        "manually.\n\n"
        "Verify (the oracle — you are done only when it passes): re-run "
        "the ci-secure scan over this repo — `python3 "
        f"<ci-secure>/scripts/run.py --root .{gh_flag} --out "
        f"{recheck_ref}` — and confirm the chain no longer "
        f"fires: no finding with `\"pattern\": \"{pattern}\"` may remain "
        f"in that JSON.{impostor_guard} If one does, occurrences are left — "
        "fix them and re-run. If you also edited the rendered ci-secure "
        "report, `python3 <ci-secure>/tests/verify_report.py --report "
        "<report.md> --findings <the ci-secure findings JSON from your run "
        "(the Phase 2 --out path)>` must still print `all checks passed`."
    )


def _finding_group_section(
    ordinal: int,
    members: list[dict[str, Any]],
    catalog_sections: dict[str, _ExtractedSection],
    repo: str | None,
    sha: str | None,
    catalog_url: str,
    findings_path: Path | None = None,
) -> str:
    """Render one consolidated finding (one underlying rule, N occurrences).

    Layout, in order:

      the stable ``<a id="finding-N">`` anchor (BEFORE the heading, as in
      ci-speedup, so a jump lands ON the heading rather than under it),
      ``## {sev emoji} Finding N: {short_title} — {n} sites / {m} workflows``
      (a top-level section, like the siblings' recommendation / long-pole
      entries; no checkbox), a bold stakes-first one-liner derived from the
      finding's attacker text, the sibling **bulleted definition list**
      (``- **Pattern:** …``, ``- **TL;DR:** …``, ``- **What an attacker
      could do:** …``, Severity, Workflow activity, Occurrences, Fix
      strategy), an optional all-dormant note, an ``#### Evidence`` h4 with
      sampled occurrence bullets, an ``#### Fix`` h4 carrying the recipe
      summary, the catalog's ``Risk of the change`` sentence and a
      collapsible copy-prompt, an optional ``#### References`` h4, and a
      ``---`` divider.

    The detail body is a definition list, not a ``| Field | Value |``
    table, for the reason both siblings are: a table cell cannot hold a
    hard newline, and several catalog TL;DRs (P14.9's, for one) are
    multi-line — rendered into a table they terminate the row mid-cell and
    the rest of the section spills out as loose prose.

    The orchestrator's fix-tracking contract (SKILL.md Phase 5) inserts
    ``FIXED — `` immediately after ``## `` once every occurrence in
    the group has been dispatched, producing
    ``## FIXED — Finding N: {short_title} — …``.
    """
    first = members[0]
    pattern = first.get("pattern", "")
    severity = _group_severity(members)
    cat = catalog_sections.get(pattern, _ExtractedSection())
    short_title = _group_short_title(members, catalog_sections)

    out: list[str] = []
    # Default heading carries no checkbox — clean to read. The
    # orchestrator's fix-tracking contract (SKILL.md Phase 5) inserts
    # ``FIXED — `` immediately after ``## `` once every occurrence in
    # the group has been dispatched, so a fixed finding renders as
    # ``## FIXED — Finding N: {title} — …``.
    sev_emoji = _SEVERITY_EMOJI.get(severity, "")
    sev_prefix = f"{sev_emoji} " if sev_emoji else ""
    # Magnitude in the heading, sibling-style (ci-speedup: `## 🔴 Long pole N:
    # {name} — {cost}`): a reader scanning headings alone can size each
    # finding without opening it.
    n_members = len(members)
    n_wfs = len({m.get("workflow_file") for m in members if m.get("workflow_file")})
    magnitude = (
        f"{n_members} site{'s' if n_members != 1 else ''} / "
        f"{n_wfs} workflow{'s' if n_wfs != 1 else ''}"
    )
    # Anchor BEFORE the heading (ci-speedup convention): an anchor emitted
    # after the heading scrolls the heading off the top of the viewport, so
    # a `#finding-N` jump lands the reader mid-body with no title in view.
    out.append(f'<a id="{_group_anchor(ordinal)}"></a>')
    out.append("")
    out.append(f"## {sev_prefix}Finding {ordinal}: {short_title} — {magnitude}")
    out.append("")

    # --- Per-workflow accumulation + activity summary ---
    # The full per-occurrence detail (file, line, jobs, activity, evidence)
    # lives in the findings JSON. The report rolls the per-workflow
    # activity into a single summary line and points readers (and the
    # agent-paste prompt below) at the JSON for the rest.
    workflow_files: list[str] = []
    activity_by_file: dict[str, dict[str, Any]] = {}
    for m in members:
        wf = m.get("workflow_file", "")
        if not wf:
            continue
        # Dedupe on `workflow_files` itself, NOT on `activity_by_file`:
        # activity_by_file is only ever populated when the run had `--repo`
        # (activity enrichment), so an offline run deduped nothing and the
        # body counted "2 workflows" for two occurrences in one file while
        # the heading — which counts a set — said "1 workflow".
        if wf not in workflow_files:
            workflow_files.append(wf)
        act = m.get("workflow_activity") or {}
        if act and wf not in activity_by_file:
            activity_by_file[wf] = act

    if activity_by_file:
        n_total = len(workflow_files)
        # Workflows whose activity check failed are unknown, not inactive —
        # exclude them from the active/run counts and surface separately so
        # "we couldn't check" never reads as "active".
        n_unavailable = sum(
            1 for wf in workflow_files
            if _activity_is_unavailable(activity_by_file.get(wf) or {})
        )
        runs_per_wf = [
            (activity_by_file.get(wf) or {}).get("runs_30d")
            for wf in workflow_files
            if not _activity_is_unavailable(activity_by_file.get(wf) or {})
        ]
        runs_ints = [r for r in runs_per_wf if isinstance(r, int)]
        n_active = sum(1 for r in runs_ints if r > 0)
        total_runs = sum(runs_ints)
        any_at_cap = any(r >= ACTIVITY_RUN_LIMIT for r in runs_ints)
        dormant_files = [
            wf for wf in workflow_files
            if (activity_by_file.get(wf) or {}).get("dormant") is True
        ]
        n_dormant = len(dormant_files)
        n_checked = n_total - n_unavailable
        runs_label = f"{total_runs:,}{'+' if any_at_cap else ''}"
        parts = [
            f"{n_active} of {n_checked} active in last 30d",
            f"{runs_label} runs (cap {ACTIVITY_RUN_LIMIT}/wf)",
        ]
        if n_dormant:
            # Name them. A count alone ("2 dormant") over a group whose other
            # workflows are live left the reader unable to tell WHICH sites
            # they could deprioritize — the count was unusable.
            named = ", ".join(
                f"`{wf.rsplit('/', 1)[-1]}`" for wf in dormant_files[:3]
            )
            if n_dormant > 3:
                named += f" and {n_dormant - 3} more"
            parts.append(f"{n_dormant} dormant ({named})")
        # Reusable workflows are unknown-but-not-broken, and saying
        # "activity-unavailable" about them reads as a tooling failure when it
        # is a fact about how GitHub books runs. Count them separately.
        reusable_files = [
            wf for wf in workflow_files
            if _activity_is_reusable(activity_by_file.get(wf) or {})
        ]
        n_reusable = len(reusable_files)
        if n_reusable:
            named_reusable = ", ".join(
                f"`{wf.rsplit('/', 1)[-1]}`" for wf in reusable_files[:3]
            )
            if n_reusable > 3:
                named_reusable += f" and {n_reusable - 3} more"
            parts.append(
                f"{n_reusable} reusable ({named_reusable}) — runs attributed "
                f"to the calling workflow, so activity is unknown, not zero"
            )
        if n_unavailable - n_reusable:
            parts.append(f"{n_unavailable - n_reusable} activity-unavailable")
        activity_value = " · ".join(parts)
    else:
        activity_value = "—"

    # --- TL;DR + attacker-capability prose (now table rows) ---
    # No cross-marker substitution: anti-pattern prose rendered under the
    # TL;DR label is mislabeled catalog content, not a graceful fallback.
    tldr_value = cat.tldr or unparsed_section_note(pattern, "TL;DR")

    # "What an attacker could do" is an LLM-generated, repo-grounded
    # exploitation scenario the orchestrator writes per group in
    # Phase 2.5 (SKILL.md), stored on the finding as `attacker_scenario`.
    # It leads with the access an attacker needs — the barrier to entry —
    # so the reader can calibrate how seriously to take the finding (an
    # anonymous fork PR is very different from "needs a compromised
    # maintainer token"). When the orchestrator hasn't supplied one, we
    # fall back to the catalog's static capability line; when the catalog
    # has none either the row is omitted, never faked.
    attacker_value = _group_attacker_scenario(members)
    attacker_is_fallback = not attacker_value
    if not attacker_value:
        attacker_value = cat.attacker_capability
        if not attacker_value and "What an attacker can do" in cat.missing_markers:
            attacker_value = unparsed_section_note(
                pattern, "What an attacker can do"
            )
    attacker_base = attacker_value
    if attacker_is_fallback and attacker_value and not attacker_value.startswith(
        "_(catalog section"
    ):
        # Say that this is the catalog's generic description of the pattern,
        # not a scenario written against THIS repo. Rendered bare, a reader
        # (and verify_report) could not tell the two apart, so a run that
        # skipped Phase 2.5 looked like one that had done the work.
        attacker_value = f"{attacker_value} {_FALLBACK_SCENARIO_SUFFIX}"

    # --- Stakes-first one-liner (sibling recommendation shape) ---
    # ci-score opens every recommendation with one bold plain-English line
    # saying what is at stake; the field table is the detail underneath it.
    # Here that line is derived from this finding's own attacker text, so it
    # is repo-grounded rather than a generic restatement of the pattern.
    stakes = _stakes_line(attacker_base)
    # A one-sentence capability line becomes its own stakes lead, printing the
    # identical sentence twice in a row. Say it once.
    if stakes and stakes.strip() != " ".join(attacker_base.split()).strip():
        out.append(f"**{stakes}**")
        out.append("")

    # --- Build the definition list (the siblings' `- **Label:** value`) ---
    rows: list[tuple[str, str]] = []
    anchor = first.get("fix_recipe_anchor", "")
    rows.append(
        (
            "Pattern",
            f"[{cat.heading_text or pattern}]({_catalog_anchor_url(catalog_url, anchor)})",
        )
    )
    # TL;DR + attacker capability sit right under Pattern — the reader
    # gets context before the operational metadata (Severity / Affected
    # workflows / Activity / etc.).
    rows.append(("TL;DR", tldr_value))
    if attacker_value:
        rows.append(("What an attacker could do", attacker_value))
    if cat.platform_note:
        # Dated, and directly under the attacker line it qualifies: a reader
        # on github.com defaults must not finish the attacker scenario without
        # learning that the platform narrowed this vector — and which
        # residuals still leave it live for them.
        rows.append(("Platform mitigation", cat.platform_note))
    rows.append(("Severity", f"**{severity}**"))
    rows.append(("Workflow activity", activity_value))
    n = len(members)
    sev_split = _occurrence_severity_breakdown(members)
    occurrences_summary = (
        f"{n} occurrence{'s' if n != 1 else ''} "
        f"across {len(workflow_files)} workflow"
        f"{'s' if len(workflow_files) != 1 else ''}"
        f"{f' ({sev_split})' if sev_split else ''}"
    )
    rows.append(("Occurrences", occurrences_summary))
    fix_strategy = first.get("fix_strategy", "")
    if fix_strategy:
        rows.append(("Fix strategy", f"`{fix_strategy}`"))

    for k, v in rows:
        # Flatten to a single line: several catalog TL;DRs are authored as
        # wrapped multi-line paragraphs, and a bullet whose value carries a
        # raw newline renders its tail as a loose continuation line detached
        # from its label.
        out.append(f"- **{k}:** {' '.join(v.split())}")
    out.append("")

    # Dormancy is informational, never a reason to drop a finding. The
    # per-occurrence flag still renders in the activity row above; when
    # EVERY affected workflow is dormant, say so once here so a triager
    # can deprioritize deliberately rather than by accident.
    all_dormant = _group_is_all_dormant(members)
    if all_dormant and any(m.get("workflow_activity") for m in members):
        out.append(
            f"> **Note.** Every affected workflow is dormant (no runs in the "
            f"last {DORMANT_DAYS} days) — verify before prioritizing. A dormant "
            f"workflow is still exploitable the moment it runs again."
        )
        out.append("")

    # --- #### Evidence: up to N sampled snippets + agent-paste prompt ---
    # Show at most _EVIDENCE_SAMPLE_SIZE occurrences inline with their
    # `evidence` field rendered as a yaml fence. The agent-paste prompt
    # below points at the findings JSON so the reader can dump every
    # occurrence when they need it.
    # Two references, deliberately different, because they are read by two
    # different things:
    #
    #   PROSE keeps the ROLE + BASENAME. The saved report outlives the tmp dir
    #   it was rendered from, so an absolute `/private/tmp/…` in prose points
    #   at a garbage-collected file and leaks the author's local paths on a
    #   shared report. `verify_report.py` makes that a hard invariant.
    #
    #   The FENCED AGENT PROMPTS carry the FULL PATH. The basename alone was
    #   unusable there: the file lives under `$TMPDIR`, so a dispatched
    #   subagent handed `Read the ci-secure findings JSON
    #   (ci-secure-findings.json)` had to guess a directory before it could
    #   read anything. A fenced block is a copy-paste command executed in the
    #   session that rendered the report, when the file still exists.
    findings_ref = (
        f"the ci-secure findings JSON (`{findings_path.name}`)" if findings_path
        else "the ci-secure findings JSON (the file you piped into `report.py`)"
    )
    findings_ref_for_agent = (
        f"the ci-secure findings JSON for this run at `{findings_path}`"
        if findings_path
        else "the ci-secure findings JSON (the file you piped into `report.py`)"
    )
    out.append("#### 🔍 Evidence")
    out.append("")
    out.append(
        f"{n} occurrence{'s' if n != 1 else ''} across "
        f"{len(workflow_files)} workflow"
        f"{'s' if len(workflow_files) != 1 else ''}."
    )
    out.append("")
    samples = members[:_EVIDENCE_SAMPLE_SIZE]
    for m in samples:
        wf = m.get("workflow_file", "")
        line_no = m.get("line", 0)
        jobs = m.get("affected_jobs") or []
        jobs_suffix = (
            f" — jobs: {', '.join(f'`{_flatten_scanned(j)}`' for j in jobs)}"
            if jobs else ""
        )
        # Present only when the job list is a fallback rather than an
        # attribution — "every job, because we could not tell" must not read
        # identically to "every job, because it really is workflow-scope".
        if m.get("affected_jobs_note"):
            jobs_suffix += f" _({m['affected_jobs_note']})_"
        dormant_tag = (
            f" — **dormant** (no runs in {DORMANT_DAYS} days)"
            if (m.get("workflow_activity") or {}).get("dormant") is True
            and not all_dormant
            else ""
        )
        out.append(
            f"- {_source_line(repo, sha, wf, line_no)}{jobs_suffix}{dormant_tag}"
        )
        evidence = (m.get("evidence") or "").rstrip()
        if evidence:
            out.append("")
            if m.get("evidence_kind") == "derived":
                # A correlated chain detector's evidence is a sentence the
                # scanner assembled ("triggers on X AND grants Y"), not lines
                # from the file. Rendering it in a ```yaml fence with a
                # line-number gutter dressed a derived claim as quoted source,
                # and a reader who opened the file looking for that text never
                # found it. Code fences are reserved for verbatim source.
                out.append(_derived_evidence_block(evidence, "  "))
            else:
                out.append("  ```yaml")
                out.append(_indent_block(_fence_safe(evidence), "  "))
                out.append("  ```")
        # A second, always-derived claim under the verbatim quote: the
        # condition elsewhere in the job that makes the quoted line a finding
        # (P14.25's payoff leg). Labelled derived so the split between "this
        # is the line" and "this is what the scanner concluded" survives.
        note = (m.get("derived_note") or "").strip()
        if note:
            out.append("")
            out.append(_derived_evidence_block(note, "  "))
        out.append("")
    # The "render the rest" prompt exists only to reach occurrences the
    # sample left out. When every occurrence is already on the page there is
    # nothing left to render, so the prompt is noise — suppress it.
    if n > len(samples):
        out.append(
            f"_Showing {len(samples)} of {n} occurrences of this one vector "
            f"— the vector itself is not trimmed, only this inline sample is. "
            f"Full list in {findings_ref}._"
        )
        out.append("")
        out.append(
            "To render every occurrence inline, paste this into your coding "
            "agent:"
        )
        out.append("")
        out.append("```text")
        out.append(
            f"Read {findings_ref_for_agent} and list every occurrence of "
            f"Finding {ordinal} (pattern {pattern}) as markdown bullets "
            f"with file:line permalinks and indented `````yaml` code-block "
            f"evidence."
        )
        out.append("```")
        out.append("")

    # --- #### Fix ---
    anchor = first.get("fix_recipe_anchor", "")
    catalog_link = _catalog_anchor_url(catalog_url, anchor)
    fix_prompt = _build_fix_prompt(
        members, pattern, severity, short_title, catalog_link, cat,
        findings_path=findings_path,
    )
    # 🛠️, not 🟢: in ci-speedup 🟢 means "runner-minute saving", and a reader
    # who reads both reports must not see the same glyph mean two things.
    out.append("#### 🛠️ Fix")
    out.append("")
    # The curated imperative first — the one line a reader acts on. (This is
    # what the deleted action-plan section used to carry; it belongs with the
    # fix, not in a duplicate list up top.)
    action = _group_action_summary(members)
    if action:
        out.append(f"**Do this:** {action}")
        out.append("")
    # Summary next, then the copy-prompt collapsible. The summary is
    # what a skimmer reads; the collapsible expands when they want to
    # dispatch the fix. Verbose recipe YAML lives behind the catalog link.
    summary = (cat.fix_recipe_summary or "").strip()
    catalog_ref = f"[catalog §{pattern}]({catalog_link})"
    if summary:
        # Block form (a lead-in plus its numbered options) keeps the catalog
        # pointer on its own line; a one-paragraph summary stays inline.
        if "\n" in summary:
            out.append(summary)
            out.append("")
            out.append(f"See {catalog_ref} for the full recipe and cross-references.")
        else:
            out.append(
                f"{summary} See {catalog_ref} for the full recipe and "
                f"cross-references."
            )
    elif "Fix recipe" in cat.missing_markers:
        # The catalog section is damaged — say that, rather than asserting
        # the fix happens to be a non-YAML org setting.
        out.append(
            f"{unparsed_section_note(pattern, 'Fix recipe')} See {catalog_ref}."
        )
    else:
        out.append(
            f"This pattern's fix is non-YAML (org-level setting, "
            f"registry configuration, or manual review). See "
            f"{catalog_ref}."
        )
    out.append("")

    # What the fix itself could break — stated before the reader dispatches
    # it, never discovered afterwards.
    risk = _risk_of_change(pattern, cat)
    if risk:
        out.append(f"**Risk of the change:** {risk}")
        out.append("")

    out.append("<details>")
    # ci-speedup names this block `🤖 Prompt for your coding agent`; ci-score
    # keeps it collapsed. Do both.
    out.append("<summary>🤖 Prompt for your coding agent</summary>")
    out.append("")
    out.append("````text")
    out.append(fix_prompt)
    out.append("````")
    out.append("")
    out.append("</details>")
    out.append("")

    # --- #### References ---
    if cat.references:
        cited = " · ".join(f"[{text}]({url})" for text, url in cat.references)
        out.append("#### 📚 References")
        out.append("")
        out.append(f"*{cited}*")
        out.append("")

    out.append("---")
    return "\n".join(out)


# =============================================================================
# Header + section blocks (template-aligned)
# =============================================================================


def _header_table(
    findings: list[dict[str, Any]],
    scanned_workflows: int,
    repo: str | None,
    sha: str | None,
    skill_sha: str | None,
    scanned_at: str,
    coverage_complete: bool = True,
    skill_tree_dirty: bool = False,
    repo_root: str | None = None,
    repo_tree_dirty: bool = False,
) -> str:
    """H1-adjacent provenance table — first thing after the title.

    House style, shared with ci-score / ci-speedup: no ``| Field | Value |``
    header row. The FIRST row is the label row (``| Repository | … |``)
    followed by the alignment row, so every subsequent ``| **Label** | value |``
    line reads as a labelled fact rather than a spreadsheet cell. The table
    states provenance only — what was scanned, at which commit, by which
    scanner — plus the counts the report's own invariants are checked against.
    """
    sev_counts: Counter[str] = Counter(f.get("severity", "MANUAL") for f in findings)
    dormant_count = sum(
        1 for f in findings if (f.get("workflow_activity") or {}).get("dormant") is True
    )
    unavailable_count = sum(
        1 for f in findings if _activity_is_unavailable(f.get("workflow_activity") or {})
    )

    # --- Repository: the label row, so no `| Field | Value |` header renders.
    shown_root = _abbreviate_home(repo_root) if repo_root else repo_root
    checkout = f" — local checkout at `{shown_root}`" if repo_root else ""
    if repo:
        repo_cell = f"[`{repo}`](https://github.com/{repo}){checkout}"
    elif repo_root:
        repo_cell = f"`{shown_root}` (local checkout — no linked GitHub remote)"
    else:
        repo_cell = "(unknown — no repository slug or checkout path recorded)"

    # --- Audited commit, with ci-score's dirty-tree caveat when the audited
    # working tree carried uncommitted edits: the scanned bytes then are not
    # the bytes at this commit, and a permalink that pretends otherwise is a
    # false provenance claim.
    # ci-speedup's clause travels with the commit because ci-secure is the
    # only one of the three that emits file:line permalinks — the reader
    # needs to know which tree those line numbers are true of.
    anchored = " — file & line references are anchored to this tree"
    if sha and repo:
        commit_cell = (
            f"[`{sha[:7]}`](https://github.com/{repo}/commit/{sha}){anchored}"
        )
    elif sha:
        commit_cell = f"`{sha[:7]}`{anchored}"
    else:
        commit_cell = "(no commit resolved — the audited tree is not a git checkout)"
    if sha and repo_tree_dirty:
        commit_cell += (
            " — **tree was dirty**: uncommitted or untracked local changes "
            "present, so the scanned bytes may not match this commit"
        )

    rows: list[tuple[str, str]] = []
    rows.append(("Repository", repo_cell))
    rows.append(("Audited commit", commit_cell))
    rows.append((
        "Workflows scanned",
        f"{scanned_workflows} workflow file(s) under `.github/workflows/`",
    ))
    rows.append((
        "Catalog",
        "ten critical attack vectors (critical-only — not a comprehensive "
        "audit)",
    ))
    # No `Scope` row and no `Findings` row. The scope-honesty line already
    # appears in the banner blockquote, the `[!WARNING]` treatment, the
    # Catalog row above and the Methodology table; the finding count already
    # appears in the banner, the headline and the Scanner row. Restating
    # either again here is bulk, not provenance.
    if sev_counts:
        breakdown = " · ".join(
            f"{sev}: {sev_counts[sev]}"
            for sev in ("HIGH", "MEDIUM", "LOW", "MANUAL")
            if sev_counts[sev]
        )
        rows.append(("Severity breakdown (by occurrence)", breakdown))
    rows.append((
        "Coverage",
        "✅ complete — every workflow file was scanned"
        if coverage_complete
        # Deliberately does not say "files": the gap may be an unreadable
        # FILE, or an unanchored `run:` STEP inside a file that parsed
        # perfectly. The banner below is where that distinction is drawn.
        else "⚠️ **PARTIAL** — not every workflow was fully scanned; see the "
        "Incomplete-coverage warning below",
    ))
    if dormant_count:
        rows.append(
            (
                "Dormant",
                f"{dormant_count} finding(s) on workflows with no runs in "
                f"{DORMANT_DAYS} days",
            )
        )
    reusable_count = sum(
        1 for f in findings if _activity_is_reusable(f.get("workflow_activity") or {})
    )
    if reusable_count:
        rows.append(
            (
                "Reusable workflows",
                f"{reusable_count} finding(s) on `workflow_call`-only "
                f"workflows — GitHub attributes their runs to the calling "
                f"workflow, so activity is unknown, not zero",
            )
        )
    if unavailable_count - reusable_count:
        rows.append(
            (
                "Activity unavailable",
                f"{unavailable_count - reusable_count} finding(s) — the "
                "workflow's run history could not be fetched (not known "
                "active or dormant)",
            )
        )
    # Mark the recorded commit `-dirty` when the skill tree had uncommitted
    # edits at scan time, so the report is honest about which code ran (the
    # HEAD sha alone can lag the working-tree files).
    dirty_suffix = "-dirty" if skill_tree_dirty else ""
    # With a git checkout we stamp the commit sha; an INSTALLED skill has no
    # .git, so instead of a bare "(unknown)" (doubled parens, and a provenance
    # the self-check reads as a FAILURE) we stamp the shipped version. Both are
    # valid, single-paren provenance states.
    if skill_sha:
        provenance = f"skill commit `{skill_sha[:7]}{dirty_suffix}`"
    else:
        provenance = f"skill v{SKILL_VERSION} — commit unknown, no git checkout"
    rows.append(("Scanned", f"{scanned_at[:10] if scanned_at else ''} (UTC)"))
    rows.append((
        "Scanner",
        f"ci-secure ({provenance}) — {len(findings)} finding(s)",
    ))

    # First row doubles as the table header (sibling house style), so the
    # alignment row follows it and every other row is a `**Label**` fact.
    first_k, first_v = rows[0]
    lines = [f"| {first_k} | {first_v} |", "| :--- | :--- |"]
    for k, v in rows[1:]:
        lines.append(f"| **{k}** | {v} |")
    return "\n".join(lines)


# =============================================================================
# Pre-drawn terminal banner
# =============================================================================

# The banner is drawn HERE, never by the orchestrator: the sibling convention
# (ci-score's score gauge) is that the agent copies the rendered line verbatim
# into the terminal. A hand-drawn banner mis-counted its blocks once, which is
# exactly the class of error a pre-drawn line cannot make.
#
# The vector DENOMINATOR ("N of {total} vectors hit") is derived from the
# loaded catalog at render time — see `render()`, which passes
# `len(catalog_sections)` in as `n_vectors`. A hardcoded literal here would
# silently drift the moment the catalog gains or loses a pattern: the banner
# would say "of 10" while the vector-map table below listed 11, and
# verify_report's banner==table check would fail against a green suite.


def _impostor_banner_state(gh_checks: dict[str, Any] | None) -> str:
    """The P14.11 word the banner ends with — honest in all four states.

    `ran` / `partial` / `SKIPPED` come straight off the scan's recorded
    status; a scan that recorded no status at all says so rather than
    implying the check ran.
    """
    status = ""
    if isinstance(gh_checks, dict):
        status = str(gh_checks.get("P14.11") or "").strip().lower()
    if not status:
        return "not recorded"
    if status.startswith("skipped"):
        return "SKIPPED"
    if status.startswith("partial"):
        return "partial"
    return "ran"


def _banner_line(
    n_findings: int,
    n_chains_hit: int,
    scanned_workflows: int,
    gh_checks: dict[str, Any] | None,
    n_vectors: int,
) -> str:
    """The one-line headline, pre-drawn for the orchestrator to copy verbatim.

    ``CI Secure   4 critical findings  ▏2 of 10 vectors hit▕  31 workflows ·
    impostor check ran``

    ``n_vectors`` is the catalog size (``len(catalog_sections)``) — the
    denominator, never a literal, so it can't diverge from the vector-map
    table.
    """
    findings_word = "finding" if n_findings == 1 else "findings"
    wf_word = "workflow" if scanned_workflows == 1 else "workflows"
    return (
        f"CI Secure   {n_findings} critical {findings_word}  "
        f"▏{n_chains_hit} of {n_vectors} vectors hit▕  "
        f"{scanned_workflows} {wf_word} · impostor check "
        f"{_impostor_banner_state(gh_checks)}"
    )


# =============================================================================
# Vector-status table — all ten vectors, including the ones that came back clean
# =============================================================================

def _chain_title(pid: str, cat: _ExtractedSection) -> str:
    heading = cat.heading_text or pid
    m = _CATALOG_HEADING_RE.match(heading)
    return m.group(1) if m else heading


def _chain_status_rows(
    groups: list[GroupEntry],
    catalog_sections: dict[str, _ExtractedSection],
    scanned_workflows: int,
    gh_checks: dict[str, Any] | None,
) -> list[tuple[str, str, str]]:
    """(mark, chain cell, evidence cell) for every chain in the catalog.

    A chain that found nothing renders ✅ with its own evidence — "no match in
    N workflows" — because the clean chains ARE the result: a findings table
    alone cannot distinguish "checked and clean" from "never checked". The one
    network-gated chain (P14.11) renders ⚠️ when it did not fully run, and
    says in words that this is not a pass.
    """
    by_pattern = {key[1]: (ordinal, members) for ordinal, key, members in groups}
    gh_status = ""
    if isinstance(gh_checks, dict):
        gh_status = str(gh_checks.get("P14.11") or "").strip()
    gh_detail = gh_status.split(":", 1)[-1].strip() or gh_status
    gh_state = _impostor_banner_state(gh_checks)

    rows: list[tuple[str, str, str]] = []
    wf_word = "workflow" if scanned_workflows == 1 else "workflows"
    for pid, cat in catalog_sections.items():
        title = _chain_title(pid, cat).replace("|", "\\|")
        hit = by_pattern.get(pid)
        if hit:
            ordinal, members = hit
            severity = _group_severity(members)
            mark = _SEVERITY_EMOJI.get(severity, "🟥")
            n = len(members)
            m = len({x.get("workflow_file") for x in members if x.get("workflow_file")})
            evidence = (
                f"{n} site{'s' if n != 1 else ''} across "
                f"{m} workflow{'s' if m != 1 else ''}"
            )
            if pid == "P14.11" and gh_state == "partial":
                evidence += " · PARTIAL — unresolved pins are NOT a pass"
            chain = f"`{pid}` — [{title}](#{_group_anchor(ordinal)})"
            rows.append((mark, chain, evidence))
            continue
        chain = f"`{pid}` — {title}"
        if pid == "P14.11" and gh_state in ("SKIPPED", "partial", "not recorded"):
            if gh_state == "SKIPPED":
                evidence = f"SKIPPED — {gh_detail or 'reason not recorded'}; NOT a pass"
            elif gh_state == "partial":
                evidence = f"PARTIAL — {gh_detail}; NOT a pass"
            else:
                evidence = "status not recorded by the scan; NOT a pass"
            rows.append(("⚠️", chain, evidence))
            continue
        # A clean row links to its appendix entry — "checked and clean" is
        # only meaningful if the reader can reach what was checked.
        rows.append((
            "✅",
            f"`{pid}` — [{title}](#{_chain_anchor(pid)})",
            f"no match in {scanned_workflows} {wf_word}",
        ))
    return rows


def _chain_status_block(
    groups: list[GroupEntry],
    catalog_sections: dict[str, _ExtractedSection],
    scanned_workflows: int,
    gh_checks: dict[str, Any] | None,
) -> str:
    rows = _chain_status_rows(
        groups, catalog_sections, scanned_workflows, gh_checks
    )
    if not rows:
        return ""
    out = [
        "## 🔗 Vector map — all ten",
        "",
        "Every vector ci-secure checks, and what it found. A ✅ row was "
        "evaluated and came back clean; a ⚠️ row did **not** run and is not a "
        "pass. What each vector actually checks is in the appendix.",
        "",
        "| | Vector | Evidence |",
        "|---|---|---|",
    ]
    for mark, chain, evidence in rows:
        out.append(f"| {mark} | {chain} | {evidence} |")
    if any(mark not in ("✅", "⚠️") for mark, _c, _e in rows):
        out.append("")
        out.append(
            "Every hit vector renders in full below — no vector is trimmed, "
            "tiered, or topped up. Findings are grouped by underlying rule: "
            "every occurrence of the same catalog pattern collapses into one "
            "entry, ranked by severity. Within a group, a long list of "
            "occurrences shows a sample inline and says so on the spot; the "
            "complete list is always in the findings JSON."
        )
    return "\n".join(out)


_OUTCOME_MARKS = {"pass": "✅ pass", "fail": "❌ fail", "unmeasured": "⚠️ unmeasured"}


def _abbreviate_home(path: str) -> str:
    """`/Users/me/src/repo` → `~/src/repo` when it sits under $HOME.

    DELIBERATE, and settled rather than left open (it was raised repeatedly
    during development): the audited checkout path is GENUINE PROVENANCE — it records
    which tree the file:line references are true of — so it stays in the
    Repository row rather than being stripped. On a user's own run it is their
    own path, and the report is theirs. Abbreviating $HOME is the one change:
    it keeps the fact ("a local checkout, here") while dropping the account
    name, which is the part that carries nothing for the reader.
    """
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):     # pragma: no cover - defensive
        return path
    if home and home != "/" and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


def _flatten_scanned(value: Any) -> str:
    """Flatten-and-neutralize an ATTACKER-CONTROLLED scanned string before it is
    interpolated into a bullet, an inline code span, or a copy-paste prompt.

    Job names, workflow-file paths, and the like are scanned YAML the repo
    under audit controls. A job name carrying backticks + newlines could forge
    a ``## FIXED —`` heading (a false-clean signal), break out of a ```` ```text ````
    fence into the copy-paste prompt, or corrupt verify_report.py's finding
    count. Collapsing ALL whitespace to single spaces kills newline-based
    heading/fence-break injection; replacing the backtick kills inline-code and
    fence-character breakout. Pipes are escaped too, so the result is also
    table-cell safe.
    """
    if value is None:
        return ""
    flat = " ".join(str(value).split())
    return flat.replace("`", "'").replace("|", "\\|")


def _fence_safe(text: str) -> str:
    """Neutralize backticks in text bound for a ``` ``` ``` code fence.

    Verbatim evidence quotes source lines the audited repo controls. A source
    line that is a run of backticks would close the evidence fence early, so a
    following ``## FIXED`` line would render as a real heading. Replacing the
    backtick guarantees the fence holds; the rest of the source is untouched.
    """
    return text.replace("`", "'")


def _cell(value: Any) -> str:
    """One markdown table cell: single-line, pipes escaped so an evidence
    string carrying `|` cannot split the row into phantom columns.

    ``None`` is the only value that renders empty. ``str(value or "")`` also
    swallowed ``0`` and ``False`` — both meaningful outcomes in a fact table,
    and both silently becoming a blank cell the reader reads as "no data".
    """
    if value is None:
        return ""
    return " ".join(str(value).split()).replace("|", "\\|")


def _security_score_block(security_score: dict[str, Any] | None) -> str:
    """``## 🧰 Config hygiene checks`` — the config facts, pass/fail, NO number.

    **The aggregate is deliberately not rendered anywhere in this report**
    by design. It used to render as `Security score: N/100 —
    X of Y scored facts pass`, both in the report and in the close — and it
    read as a contradiction: "5 of 6 facts pass" sat directly above ten green
    vector rows, so a reader saw a grade that appeared to disagree with the
    scan. The two measure different things. Findings are open doors; these
    facts are armor. A single blended number labelled "Security score"
    overclaims what a handful of config observations can say, and quantification is
    deferred to ci-advisor, where the blend context carries the denominators.

    This exact back-and-forth has already happened once in the other
    direction — a review restored the rendered score on the argument that "a
    score computed but not shown is a number the reader cannot check". That
    argument is answered, not overlooked: the number is not shown because it
    is not shown to the READER at all; it is machine-only, consumed by
    ci-advisor from the findings JSON, whose shape is unchanged — same keys,
    fact ids, outcomes and aggregate; only printed prose moved, so bind to the
    ids and not to the sentences. Do not re-render it here.

    The section renders whenever ``security_score`` is a dict — the FACT TABLE
    is what's gated on there being facts, not the section. Gating the whole
    section on facts made the crash path invisible: when the config-facts layer
    throws, ``scan.py`` hands back ``facts=[]`` and a reason, and the
    "nothing here was checked" headline written for exactly that case could
    never reach the page.
    """
    if not isinstance(security_score, dict):
        return ""
    raw_facts = security_score.get("facts")
    facts = raw_facts if isinstance(raw_facts, list) else []

    # A key that is present but of the wrong type is a DAMAGED block, not an
    # absent one. `or []` / `or {}` turned both into a silent empty and the
    # report rendered as if the producer had simply said "none".
    damaged: list[str] = []
    raw_unmeasured = security_score.get("unmeasured")
    if raw_unmeasured is None or isinstance(raw_unmeasured, list):
        unmeasured = list(raw_unmeasured or [])
    else:
        unmeasured = []
        damaged.append(
            f"`unmeasured` is a {type(raw_unmeasured).__name__}, not a list"
        )
    if raw_facts is not None and not isinstance(raw_facts, list):
        damaged.append(f"`facts` is a {type(raw_facts).__name__}, not a list")

    out = [
        "## 🧰 Config hygiene checks — pass/fail",
        "",
        "Hygiene and armor observations about how these workflows are "
        "configured. They are **not attack vectors** and they are **not "
        "scored, graded, or totalled anywhere in this report** — each row is "
        "an independently fixable fact, and a failing row does not make the "
        "vector scan above less clean (or a passing row make it safer).",
    ]
    if not facts:
        out += [
            "",
            "**Nothing here was checked.** "
            + str(security_score.get("reason")
                  or "the config-facts layer produced no facts on this run")
            + " — that is a coverage gap, not a clean result.",
        ]
    if unmeasured:
        names = ", ".join(str(u) for u in unmeasured)
        out += [
            "",
            f"{len(unmeasured)} check(s) could not be measured ({names}) — a "
            "coverage gap, not a pass.",
        ]
    if damaged:
        out += [
            "",
            "> [!WARNING]",
            "> **The config-facts block handed to the renderer is malformed** "
            + "; ".join(damaged)
            + ". What is shown below is what could be read from it; treat "
            "the rest as missing, not as empty.",
        ]
    if facts:
        out += ["", "| | Check | Evidence |", "|---|---|---|"]
        for f in facts:
            if not isinstance(f, dict):
                continue
            mark = _OUTCOME_MARKS.get(str(f.get("outcome")),
                                      str(f.get("outcome")))
            out.append(
                f"| {mark} | {_cell(f.get('fact'))} | "
                f"{_cell(f.get('evidence'))} |"
            )
    return "\n".join(out)


def _chain_appendix_block(
    catalog_sections: dict[str, _ExtractedSection],
) -> str:
    """``## What each vector checks`` — one line per vector, all ten.

    The analog of ci-score's "What each check means" appendix: a reader who
    sees a ✅ row deserves to know what was checked, or the clean result is an
    unfalsifiable claim. Sourced from each catalog entry's own TL;DR opening
    sentence, so the appendix cannot describe a chain the scanner does not
    run.
    """
    if not catalog_sections:
        return ""
    out = [
        "## 📖 What each vector checks",
        "",
        "One line per vector, taken from the catalog entry each detector is "
        "built from.",
        "",
    ]
    for pid, cat in catalog_sections.items():
        title = _chain_title(pid, cat)
        tldr = " ".join((cat.tldr or "").split())
        first = (
            _SENTENCE_SPLIT_RE.split(tldr)[0] if tldr
            else unparsed_section_note(pid, "TL;DR")
        )
        out.append(
            f'- <a id="{_chain_anchor(pid)}"></a>**`{pid}` — {title}.** {first}'
        )
    return "\n".join(out)


def _gh_checks_block(
    gh_checks: dict[str, Any] | None,
    gh_check_details: dict[str, Any] | None = None,
) -> str:
    """Render the scan's network-gated check statuses under the header.

    ``gh_checks`` maps a pattern id to a status string produced by
    ``scan.py``. Three states, and the distinction between them is the
    whole point:

    - ``ran: …``     — every pin resolved. ✅.
    - ``partial: …`` — some pins could not be resolved (network, rate
      limit, invisible repo). Those pins are NOT verified, so the check is
      warned about alongside a skip and each unresolved pin is named from
      ``gh_check_details[pid]["unverified"]``. Rendering this ✅ would
      assert "verified" of pins nobody checked.
    - ``skipped: …`` — the check never ran at all. ⚠️.

    A skipped or partial network-gated check is the most dangerous thing
    this report can render quietly: the pattern produced no findings, so
    silence reads as a pass. Both go in a ``[!WARNING]`` callout that says,
    in words, that they are NOT a pass.

    Empty / missing → "" (the scan recorded no network-gated checks).
    """
    if not isinstance(gh_checks, dict) or not gh_checks:
        return ""
    details = gh_check_details if isinstance(gh_check_details, dict) else {}

    def _label(pid: str) -> str:
        name = _GH_CHECK_LABELS.get(pid)
        return f"{pid} {name} check" if name else f"{pid} network-gated check"

    def _unverified(pid: str) -> list[str]:
        entry = details.get(pid) or {}
        pins = entry.get("unverified") if isinstance(entry, dict) else None
        return [str(p) for p in pins] if isinstance(pins, list) else []

    skipped: list[tuple[str, str]] = []
    partial: list[tuple[str, str]] = []
    ran: list[tuple[str, str]] = []
    for pid in sorted(gh_checks):
        status = str(gh_checks[pid] or "").strip()
        detail = status.split(":", 1)[-1].strip() or status
        if status.lower().startswith("skipped"):
            skipped.append((pid, detail or "reason not recorded"))
        elif status.lower().startswith("partial"):
            partial.append((pid, detail))
        else:
            ran.append((pid, detail))

    out: list[str] = []
    if skipped or partial:
        out.append("> [!WARNING]")
        out.append(
            "> **A network-gated check did not fully run. Its absence from "
            "the findings below is NOT a pass.**"
        )
        out.append(">")
        for pid, reason in skipped:
            out.append(f"> - **{_label(pid)}: SKIPPED — {reason}; this is NOT a pass.**")
        for pid, reason in partial:
            out.append(
                f"> - **{_label(pid)}: PARTIAL — {reason}. The unverified "
                f"pins below are NOT a pass.**"
            )
            for pin in _unverified(pid):
                out.append(f">   - `{pin}` — UNVERIFIED")
        out.append(">")
        # Only an unauthenticated skip is fixed by logging in; an explicit
        # `--gh-impostor=off` or a rate limit is not.
        if any(
            "auth" in reason.lower() or "gh unavailable" in reason.lower()
            for _pid, reason in skipped
        ):
            out.append(
                "> Re-run with an authenticated `gh` (`gh auth login`) to close "
                "this gap."
            )
        else:
            out.append("> Re-run once the check can complete to close this gap.")
        out.append("")
    # Only the checks that RAN get a plain bullet list. Repeating the
    # skipped/partial bullets here reprinted the `[!WARNING]` callout's own
    # lines byte-for-byte one blank line below it.
    if ran:
        out.append("**Network-gated checks.** These need the GitHub API:")
        out.append("")
        for pid, status in ran:
            out.append(f"- ✅ {_label(pid)}: ran — {status}")
    return "\n".join(out).rstrip("\n")


def _methodology_block(n_vectors: int) -> str:
    return (
        "## ⚙️ Methodology\n\n"
        "| Term | Definition |\n"
        "| --- | --- |\n"
        "| **Scope** | " + _SCOPE_HONESTY_LINE + " ci-secure checks a "
        "deliberately small set of critical exploit chains — the ones that "
        "turn a workflow into remote code execution or credential theft. A "
        "clean ci-secure report does **not** mean the repository is "
        "secure. |\n"
        "| **What is not scanned** | Workflow YAML only. A composite action "
        "the workflow calls (`uses: ./.github/actions/…`, or any third-party "
        "action) is a separate file that this scan does not open, so an "
        "install command, a secret dump, or an untrusted-input expansion "
        "living inside one is invisible here — the workflow line that calls "
        "it looks clean. Steps generated at runtime (a `run:` block that "
        "writes and then executes a script) are outside the scan for the "
        "same reason. |\n"
        "| **Provenance path** | The Repository row names the local "
        "checkout the scan read, so the file:line references below can be "
        "tied to a tree. That is deliberate — it is the audited path, and on "
        "your own run it is your own — with `$HOME` abbreviated to `~`. |\n"
        "| **ci-secure pattern catalog** | Public catalog at "
        f"[`security-patterns.md`]({_CATALOG_PUBLIC_URL}) — the critical "
        "exploit-chain patterns, authored from public CI/CD supply-chain "
        "incidents (tj-actions, Ultralytics, nx/s1ngularity, Trivy, "
        "TanStack, elementary-data). Each pattern carries a TL;DR, an "
        "attacker-capability statement, the anti-pattern definition, and "
        "a fix recipe. |\n"
        "| **Network-gated checks** | Some patterns need the GitHub API to "
        "decide (e.g. whether an action pin points at a commit reachable "
        "from the canonical repo). When `gh` is unavailable they do not "
        "run, and the report says so loudly under the header — a skipped "
        "check is never a pass. |\n"
        "| **Severity** | Criticality is membership in the ten-vector "
        "catalog: every finding here is a complete outsider → compromise "
        "chain, and every one renders. The HIGH / MEDIUM label records the "
        "unfixed attack's potency — it never tiers, truncates, or reorders "
        "what you see. |\n"
        "| **Finding grouping** | Every occurrence of the same underlying "
        "rule (same catalog pattern) collapses into one "
        "`## Finding N` entry — and every group renders, always. The "
        "definition list summarizes the group; "
        "the `#### Evidence` section points at the findings JSON, which "
        "holds the file:line + matched-lines detail for every "
        "occurrence. |\n"
        "| **Workflow activity** | When `--repo owner/repo` is passed, "
        "the GitHub API supplies per-workflow run counts. The row "
        f"summarizes them as `X of Y active in last 30d · Z runs (cap "
        f"{ACTIVITY_RUN_LIMIT}/wf) · K dormant`. The per-workflow cap "
        "means `Z` is a floor when any workflow hit it (a `+` marker is "
        "added to make that explicit). A workflow with zero runs in "
        f"the last {DORMANT_DAYS} days is **dormant**; a group is "
        "dormant only when *every* one of its occurrences is on a "
        "dormant workflow. A **reusable** workflow (`on: workflow_call` "
        "only) is never called dormant on an empty run history: GitHub "
        "attributes its runs to the calling workflow, so that history is "
        "empty however often the file executes — its activity is reported "
        "as unknown. |\n"
        f"| **`N of {n_vectors} vectors hit`** | The denominator is always "
        f"{n_vectors} — the whole catalog — never \"the vectors that ran\". "
        "A vector that could "
        "not be evaluated (a network-gated check with no `gh`) shows ⚠️ in "
        "the vector map and is counted as neither hit nor clean; shrinking "
        "the denominator instead would quietly convert a check that did not "
        "run into one that passed. |\n"
        "| **Workflows scanned** | Counts every workflow FILE read, "
        "including one that is entirely commented out. Such a file defines "
        "no jobs and cannot produce a finding, but it is still part of the "
        "tree that was examined, and excluding it would make the "
        "denominator depend on file contents. |"
    )


def _data_sources_block(
    skill_sha: str | None,
    sha: str | None,
    scanned_at: str,
    repo: str | None,
) -> str:
    skill_part = f" at commit `{skill_sha[:7]}`" if skill_sha else ""
    rows: list[tuple[str, str, str]] = [
        (
            f"ci-secure scanner{skill_part}",
            (
                f"Every `.github/workflows/` file ending `.yml` or `.yaml`, "
                f"dot-prefixed names included, under the audited tree "
                f"({sha[:7] if sha else 'no-sha'})"
            ),
            "Critical exploit-chain pattern detection (see the catalog)",
        ),
    ]
    if repo:
        rows.append(
            (
                f"GitHub API — run activity ({repo})",
                f"Per-workflow run counts + last-run timestamp (last 30 days)",
                "Workflow-activity enrichment (active vs dormant)",
            )
        )
    else:
        rows.append(
            (
                # Scoped to run-activity enrichment ONLY. An unqualified
                # "GitHub API / not queried" contradicted the network-gated
                # status line above it, which can report the P14.11
                # impostor-SHA check as having run against the same API.
                "GitHub API — run activity",
                "not queried (no `--repo`)",
                "Pass `--repo owner/repo` to enrich findings with workflow activity",
            )
        )

    lines = [
        "## 🗄️ Data sources",
        "",
        "| Source | Coverage | Used for |",
        "| --- | --- | --- |",
    ]
    for src, coverage, used_for in rows:
        lines.append(f"| {src} | {coverage} | {used_for} |")
    lines.append("")
    lines.append(
        f"**Data freshness.** Scanner ran at `{scanned_at}`. Workflow YAML "
        f"is read from the audited tree at commit "
        f"`{sha[:7] if sha else 'no-sha'}`. Activity counts (when "
        f"`--repo` is supplied) reflect a rolling 30-day window at "
        f"scan time."
    )
    return "\n".join(lines)


def _headline_block(
    all_groups: list[GroupEntry],
    scanned_workflows: int,
    n_vectors: int,
    coverage_complete: bool = True,
) -> str:
    """The ci-score-style headline: one `## ` line stating the verdict.

    Sibling skeleton (ci-score: header → gauge → `## CI Score: **N/100** — …`;
    ci-speedup: header → `## 🗺️ Long pole map`). There is no executive summary
    and no separate action plan: the ranked finding order IS the action plan,
    and the vector map below carries the per-vector detail. The counting
    sentence — occurrences vs. distinct chains — rides directly under the
    headline, because "17 findings" and "4 chains" are both true of the same
    scan and a reader who conflates them mis-sizes the work.
    """
    if not all_groups:
        return "\n".join([
            "## Critical findings: **0** — no vector matched",
            "",
            "**No critical attack vectors detected.**",
            "",
            (
                f"All {scanned_workflows} workflow file(s) were checked "
                f"against ci-secure's {n_vectors} critical attack-vector "
                f"patterns and none matched."
                if coverage_complete
                else "No pattern matched the workflow files that could be "
                     "scanned — see the incomplete-coverage warning above; "
                     "this is NOT a clean result."
            ),
            "",
            _SCOPE_HONESTY_LINE,
        ])

    sev_counts: Counter[str] = Counter(_group_severity(m) for _o, _k, m in all_groups)
    n_groups_total = len(all_groups)
    n_occurrences_total = sum(len(m) for _o, _k, m in all_groups)
    # The findings' SPREAD is the count of files they actually appear in. The
    # sentence used to read "… across {scanned_workflows} workflow file(s)" —
    # the number of files SCANNED — in a clause whose subject is the findings,
    # so a repo with 3 findings in 1 file was reported as findings "across 12
    # workflow file(s)". Both numbers are now named, each as what it is.
    n_affected_files = len({
        m.get("workflow_file") for _o, _k, members in all_groups
        for m in members if m.get("workflow_file")
    })
    sev_phrases = []
    for sev in ("HIGH", "MEDIUM", "LOW", "MANUAL"):
        if sev_counts.get(sev):
            sev_phrases.append(f"{sev_counts[sev]} {sev}")
    sev_summary = ", ".join(sev_phrases)

    return "\n".join([
        (
            f"## Critical findings: **{n_occurrences_total}** — "
            f"{n_groups_total} of {n_vectors} vectors hit"
        ),
        "",
        (
            f"{n_occurrences_total} occurrence(s) of {n_groups_total} "
            f"distinct attack vector(s) (by vector: {sev_summary}) across "
            f"{n_affected_files} of the {scanned_workflows} workflow file(s) "
            f"scanned. The header's severity breakdown counts occurrences, "
            f"not vectors."
        ),
    ])


# =============================================================================
# Sort + top-level render
# =============================================================================

def _id_sort_key(finding_id: str) -> tuple[int, int, str]:
    # Sort numerically within the `f<N>` id space so f10 follows f9, not f1.
    if finding_id.startswith("f") and finding_id[1:].isdigit():
        return (0, int(finding_id[1:]), finding_id)
    return (1, 0, finding_id)


def _sort_key(finding: dict[str, Any]) -> tuple[int, int, tuple[int, int, str]]:
    sev = _SEVERITY_ORDER.get(finding.get("severity") or "MANUAL", 99)
    dormant = finding.get("workflow_activity", {}).get("dormant") is True
    return (1 if dormant else 0, sev, _id_sort_key(finding.get("id", "")))


# Per-finding evidence sampling. Groups with more than this many
# occurrences render the first N in full and refer the reader to the
# findings JSON for the rest. Keeps a single Finding entry from
# bloating the report body with dozens of near-identical code blocks.
_EVIDENCE_SAMPLE_SIZE = 3

# A group ordinal + its members, paired so the TOC and the body can
# emit matching anchors without re-deriving the ordinal.
GroupEntry = tuple[int, tuple[str, str], list[dict[str, Any]]]


def render_plan_keys(findings_json: dict[str, Any]) -> list[dict[str, Any]]:
    """The groups that WILL render, in render order, with their dormancy.

    Runs the exact same pipeline `render()` uses — `_validate_findings` →
    `_dedupe_findings` → `_group_findings` — and returns EVERY group as
    ``{"pattern": id, "dormant": bool}``. Under the critical-only contract
    every group renders, always, so the plan is the full group list.

    List position IS the group's report ordinal (index 0 → `Finding 1`),
    which is what lets SKILL.md's selection table carry the same numbers as
    the report — building that table from a separately-sorted list is how
    the two drift apart. ``dormant`` matches ``_group_is_all_dormant``:
    true only when EVERY occurrence sits on a dormant workflow, which is
    the definition of "all" the selection prompt uses.
    """
    # Same filtered set as render(): an off-catalog pattern is off-contract and
    # never becomes a render-plan row, so the selection table can't offer a
    # finding the report won't show.
    findings, _malformed = _validate_findings(
        list(findings_json.get("findings") or []),
        catalog_patterns=set(_load_catalog_sections(None)),
    )
    findings, _n_dupes = _dedupe_findings(findings)
    findings.sort(key=_sort_key)
    groups = _report_groups(findings)
    return [
        {"pattern": pattern, "dormant": _group_is_all_dormant(members)}
        for _ordinal, (_source, pattern), members in groups
    ]


def _report_groups(findings: list[dict[str, Any]]) -> list[GroupEntry]:
    """Every group, ordinal-numbered in render order. Nothing is trimmed."""
    raw_groups = _group_findings(findings)
    return [
        (ordinal, key, members)
        for ordinal, (key, members) in enumerate(raw_groups, start=1)
    ]


def _load_catalog_sections(catalog_path: Path | None) -> dict[str, _ExtractedSection]:
    # The catalog is required infrastructure: every ci-secure finding's
    # TL;DR, fix recipe, and reference links are pulled from it. A
    # silent {} fallback would render findings as bare table rows with
    # no actionable context, so we raise instead and let main() turn
    # this into a clear stderr message + non-zero exit.
    if catalog_path is None:
        catalog_path = _THIS_DIR.parent / "references" / "security-patterns.md"
    if not catalog_path.exists():
        raise FileNotFoundError(
            f"catalog not found at {catalog_path}; pass --catalog or reinstall the skill"
        )
    text = catalog_path.read_text(encoding="utf-8")
    sections = {pid: _extract_section(s) for pid, s in _split_catalog_sections(text).items()}
    for pid, sec in sections.items():
        if sec.missing_markers:
            # WARNING, not debug: a missing marker silently empties a whole
            # section of the rendered finding, and the only other signal is
            # the placeholder text in the report itself.
            logger.warning(
                "catalog section %s is missing required marker(s): %s — the "
                "report will render a parse-failure note in their place",
                pid, ", ".join(sec.missing_markers),
            )
    logger.debug("catalog: %d sections loaded from %s", len(sections), catalog_path)
    return sections


# Curated one-line action verbs, keyed off the stable `fix_strategy` slug —
# far cleaner than auto-extracting prose, which grabbed problem-descriptions,
# colon-intros, and admonition markers. One entry per catalog pattern.
#
# There is no separate action-plan section any more (neither sibling has one;
# the severity-ranked finding order IS the plan), so the verb opens each
# finding's Fix block — the imperative a reader acts on, above the catalog's
# fuller recipe sentence.
_ACTION_BY_FIX_STRATEGY = {
    "env-var-indirection": "Move untrusted `${{ }}` values into an `env:` var before using them in `run:`",
    "switch-to-pull-request-or-drop-head-checkout": "Switch to `pull_request`, or stop checking out the PR head in the privileged job",
    "switch-pull-request-target-to-pull-request": "Switch the trigger from `pull_request_target` to `pull_request`",
    "repin-to-reachable-sha": "Repin to a SHA reachable from the canonical repo",
    "scope-secret-per-step": "Pass only the specific secret each step needs (no whole-context dumps)",
    "sanitize-before-export": "Sanitize untrusted input before writing to `$GITHUB_ENV` / `$GITHUB_PATH`",
    "split-trusted-untrusted-workflows": "Split into separate trusted / untrusted workflows",
    "move-credential-outside-cached-path": "Move credential files out of cached / uploaded paths",
    "pin-and-verify-remote-script": "Pin remote code to a full commit SHA, or checksum-verify a downloaded script, before executing it",
    "ignore-install-scripts-in-privileged-job": "Install with `--ignore-scripts` in the job that holds secrets or a write token",
}


def _group_action_summary(members: list[dict[str, Any]]) -> str:
    """Curated one-line action verb for a group, or "" to fall back to the
    short title. Keyed off the catalog's `fix_strategy` slug."""
    return _ACTION_BY_FIX_STRATEGY.get(members[0].get("fix_strategy", ""), "")


def _validate_findings(
    findings: list[dict[str, Any]],
    catalog_patterns: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], list[str]]]]:
    """Partition findings into (well-formed, malformed-with-reasons).

    When ``catalog_patterns`` is supplied, a finding whose ``pattern`` is not
    in the catalog is ALSO malformed: it can never map to a vector-map row, so
    left in it would inflate the headline count while the banner (catalog-
    filtered) and the vector map both omitted it — a silent divergence. Routing
    it here surfaces it in the same loud banner as any other off-contract
    finding, and keeps the headline, banner, and vector map on one filtered set.

    A finding missing a load-bearing key — or carrying a severity outside
    the catalog's tiers — would otherwise be rendered with silent defaults
    (``severity`` → ``MANUAL``, ``line`` → ``0``), sorting a real HIGH issue
    to the bottom of the report as a documentation-tier entry. For a
    security scanner that's a silent downgrade, so off-contract findings are
    pulled out here and surfaced in a loud banner instead of rendered as if
    valid. The dangerous producer is the dynamic one (the orchestrator
    patches `attacker_scenario` and other fields onto findings the
    type-checker never sees), which is exactly why the gate is at runtime,
    not just in the type.
    """
    valid: list[dict[str, Any]] = []
    malformed: list[tuple[dict[str, Any], list[str]]] = []
    for f in findings:
        problems: list[str] = []
        for key in _REQUIRED_FINDING_KEYS:
            # `line` may legitimately be 0; only flag missing/None, and
            # treat empty strings on the str fields as missing too.
            if key not in f or f[key] is None or f[key] == "":
                problems.append(f"missing `{key}`")
        sev = f.get("severity")
        if sev is not None and sev not in _VALID_SEVERITIES:
            problems.append(f"invalid severity {sev!r}")
        if catalog_patterns is not None:
            pat = f.get("pattern")
            if pat not in catalog_patterns:
                problems.append(f"pattern {pat!r} not in the catalog")
        if problems:
            malformed.append((f, problems))
        else:
            valid.append(f)
    return valid, malformed


def _malformed_findings_banner(
    malformed: list[tuple[dict[str, Any], list[str]]],
) -> str:
    """Render a loud banner naming findings dropped as off-contract.

    Empty → "". The whole point mirrors the coverage-gap banner: a finding
    the report couldn't trust must be visible, never silently swallowed or
    rendered as a buried MANUAL placeholder.
    """
    if not malformed:
        return ""
    lines = [
        "> [!CAUTION]",
        f"> **{len(malformed)} finding(s) were dropped as malformed.** They "
        "were missing a required field or carried an invalid severity, so they "
        "are **not** rendered or counted below. A producer (most likely the "
        "orchestrator's runtime enrichment) emitted a finding off-contract — "
        "fix the source and re-run so these aren't lost:",
        ">",
    ]
    for f, problems in malformed:
        fid = f.get("id") or "?"
        pat = f.get("pattern") or "?"
        wf = _flatten_scanned(f.get("workflow_file") or "?")
        lines.append(f"> - `{fid}` ({pat}, {wf}): {'; '.join(problems)}")
    return "\n".join(lines)


def _coverage_is_complete(
    scan_incomplete: list[dict[str, Any]] | None,
    dropped_matches: list[dict[str, Any]] | None = None,
    coverage_notes: list[dict[str, Any]] | None = None,
) -> bool:
    """True when the static scan read every workflow file AND scanned every
    ``run:`` step inside them.

    Three independent holes, all fatal to a claim of completeness:

    - ``scan_incomplete`` — a FILE couldn't be read or YAML-parsed, so the
      detectors never ran on it.
    - ``dropped_matches`` — a file parsed fine, but a ``run:`` step in it
      couldn't be anchored to raw lines and was never scanned for injection
      sinks.
    - ``coverage_notes`` — the step was read, but something in it was not
      knowable from the YAML (a computed ``working-directory:``, a ``ref:``
      chosen at run time, shell that would not parse), so part of it went
      unchecked.

    ``suppressed_findings`` is deliberately NOT here. That list is the
    scanner reaching a finding and choosing not to report it — a fetch pinned
    to a full commit id, which is the fix this catalog recommends. Counting it
    as missing coverage told every repository that followed the fix recipe
    that its report was "not a clean result", which spends the banner's
    credibility exactly where it is least deserved.

    ``None`` means the findings JSON had no such array — i.e. no gaps were
    recorded, the same as an empty list. We normalize explicitly so the
    truthiness check can't be misread as silently turning a stray ``None``
    into a false "complete".
    """
    return not (scan_incomplete or []) and not (dropped_matches or []) \
        and not (coverage_notes or [])


def _coverage_gap_banner(
    scan_incomplete: list[dict[str, Any]] | None = None,
    dropped_matches: list[dict[str, Any]] | None = None,
    coverage_notes: list[dict[str, Any]] | None = None,
) -> str:
    """Render a prominent warning when the static scan left coverage unfinished.

    An unscanned file — or an unscanned step inside a scanned file — must never
    be read as clean, so this banner sits at the top of the report.

    - ``scan_incomplete`` is ``scan.py``'s record of workflow files that
      couldn't be read or YAML-parsed, so the static detectors never ran on
      them (each entry: ``workflow_file``, ``reason``).
    - ``dropped_matches`` is its record of ``run:`` steps that could not be
      anchored to raw lines and were therefore not scanned for injection
      sinks. Those files DID parse. Counting them as unreadable files
      overstated the damage and mis-stated its shape (two drops in one file
      read as "2 workflow files"), so they get their own sentence, counted per
      step AND per file.

    Both empty → returns "" (no banner).
    """
    scan_incomplete = scan_incomplete or []
    dropped_matches = dropped_matches or []
    coverage_notes = coverage_notes or []
    if not scan_incomplete and not dropped_matches and not coverage_notes:
        return ""

    parts: list[str] = []
    if scan_incomplete:
        n_files = len({
            str(e.get("workflow_file", "?")) for e in scan_incomplete
        })
        parts.append(
            f"{n_files} workflow file(s) could not be statically scanned"
        )
    if dropped_matches:
        n_wf = len({str(e.get("workflow_file", "?")) for e in dropped_matches})
        parts.append(
            f"{len(dropped_matches)} run: step(s) in {n_wf} workflow(s) "
            "could not be anchored to raw lines and were NOT scanned for "
            "injection sinks"
        )
    if coverage_notes:
        # Its OWN sentence: these steps were read. What went unchecked is a
        # value the YAML does not contain — where a step ran, which ref a
        # checkout took, a command no shell parser accepts. Saying they "could
        # not be anchored" would describe the wrong defect.
        n_wf = len({str(e.get("workflow_file", "?")) for e in coverage_notes})
        parts.append(
            f"{len(coverage_notes)} step(s) in {n_wf} workflow(s) were read "
            "but carry a value this scan cannot know, so part of each went "
            "unchecked"
        )

    lines = [
        "> [!WARNING]",
        "> **Incomplete coverage — " + "; ".join(parts) + ".** This is "
        "**not** a clean result — fix the cause and re-run before relying on "
        "this report.",
    ]
    if scan_incomplete:
        lines += [">", "> _Static scan could not read/parse:_"]
        for entry in scan_incomplete:
            lines.append(
                f"> - **{_flatten_scanned(entry.get('workflow_file', '?'))}**: "
                f"{_flatten_scanned(entry.get('reason', 'unknown'))}"
            )
    if coverage_notes:
        lines += [">", "> _Read, but something in the step was not knowable:_"]
        for entry in coverage_notes:
            lines.append(
                f"> - **{_flatten_scanned(entry.get('workflow_file', '?'))}**: "
                f"{_flatten_scanned(entry.get('reason', 'unknown'))}"
            )
    if dropped_matches:
        lines += [">", "> _Parsed, but a `run:` step went unscanned:_"]
        for entry in dropped_matches:
            lines.append(
                f"> - **{_flatten_scanned(entry.get('workflow_file', '?'))}**: "
                f"{_flatten_scanned(entry.get('reason', 'unknown'))}"
            )
    return "\n".join(lines)


def _dedupe_findings(
    findings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Collapse exact-duplicate occurrences, preserving order.

    Two findings are the same occurrence when they share
    ``(pattern, workflow_file, line, evidence)``. Duplicates arise
    legitimately upstream — the run-injection detector fires once per
    ``${{ }}`` expression, so a line with two expressions yields two
    identical-evidence findings. Counting each as a separate occurrence
    inflates the headline total, the per-group "N× across M workflows"
    counts, and the action-plan scope (a line shown 14× reads as 14
    problems). Deduping here — before validation results are grouped or
    counted — makes every downstream count honest. Findings whose evidence
    genuinely differs (a different expression on the same line) keep
    distinct evidence and survive.

    Returns (deduped, n_removed).
    """
    seen: set[tuple[str, str, int, str]] = set()
    out: list[dict[str, Any]] = []
    for f in findings:
        key = (
            f.get("pattern", ""),
            f.get("workflow_file", ""),
            f.get("line", 0),
            (f.get("evidence") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out, len(findings) - len(out)


def render(
    findings_json: dict[str, Any],
    catalog_path: Path | None = None,
    catalog_url: str | None = None,
    findings_path: Path | None = None,
) -> str:
    # Load the catalog first: its pattern set is a validation input (an
    # off-catalog pattern is off-contract, surfaced like any malformed finding).
    catalog_sections = _load_catalog_sections(catalog_path)
    # Validate at the boundary: a finding missing a load-bearing key, carrying
    # an invalid severity, or naming a pattern outside the catalog is pulled out
    # and surfaced loudly rather than rendered with silent defaults that would
    # bury it (or, for an off-catalog pattern, diverge the counts).
    findings, malformed_findings = _validate_findings(
        list(findings_json.get("findings") or []),
        catalog_patterns=set(catalog_sections),
    )
    findings, n_duplicates = _dedupe_findings(findings)
    if n_duplicates:
        logger.debug(
            "render: collapsed %d duplicate occurrence(s) (same "
            "pattern/file/line/evidence)", n_duplicates,
        )
    findings.sort(key=_sort_key)
    if malformed_findings:
        logger.warning(
            "render: dropped %d malformed finding(s) (see report banner)",
            len(malformed_findings),
        )

    scanned_at = findings_json.get("scanned_at", "")
    scanned_workflows = findings_json.get("scanned_workflows", 0)
    repo = findings_json.get("repo")
    sha = findings_json.get("commit_sha")
    skill_sha = findings_json.get("skill_commit_sha")
    gh_checks = findings_json.get("gh_checks")
    logger.debug(
        "render: %d findings, scanned %d workflows, repo=%s, gh_checks=%s",
        len(findings), scanned_workflows, repo,
        sorted(gh_checks) if isinstance(gh_checks, dict) else None,
    )

    if catalog_url is None:
        catalog_url = _build_catalog_url(skill_sha)
    # The vector denominator everywhere — banner, headline, methodology — is
    # the loaded catalog's size, never a literal, so the three can't diverge.
    n_vectors = len(catalog_sections)

    # Group findings by pattern. EVERY group renders — the critical-only
    # catalog is small enough that trimming would only hide work. Dormancy
    # is signalled per group, never a reason to drop one.
    groups = _report_groups(findings)

    # The title names the REPO, never the skill. House rule (ci-score's
    # `_render_header`): the slug when there is one, else the basename of the
    # audited checkout, else an explicit unknown marker. Titling a no-remote
    # report "ci-secure — …" made the deliverable look like it was about the
    # tool rather than about the user's repository.
    root = str(findings_json.get("repo_root") or "").rstrip("/")
    repo_label = (
        repo
        or (root.rsplit("/", 1)[-1] or root)
        or "(unknown repository)"
    )

    out: list[str] = []
    out.append(f"# {repo_label} — any critical attack vectors in your CI?")
    out.append("")
    scan_incomplete = findings_json.get("scan_incomplete") or []
    dropped_matches = findings_json.get("dropped_matches") or []
    coverage_notes = findings_json.get("coverage_notes") or []
    coverage_complete = _coverage_is_complete(
        scan_incomplete, dropped_matches, coverage_notes)
    skill_tree_dirty = bool(findings_json.get("skill_tree_dirty"))
    out.append(_header_table(
        findings, scanned_workflows, repo, sha, skill_sha,
        scanned_at, coverage_complete, skill_tree_dirty,
        repo_root=findings_json.get("repo_root"),
        repo_tree_dirty=bool(findings_json.get("repo_tree_dirty")),
    ))
    out.append("")

    # The banner: pre-drawn here, fenced so its spacing survives markdown.
    # The orchestrator copies this line verbatim as the first line of its
    # terminal summary (SKILL.md phase 3) — it never redraws it.
    chains_hit = len({key[1] for _o, key, _m in groups if key[1] in catalog_sections})
    out.append("```")
    out.append(
        _banner_line(len(findings), chains_hit, scanned_workflows, gh_checks,
                     n_vectors)
    )
    out.append("```")
    out.append("")

    # Scope honesty, verbatim and unconditional, as the headline blockquote
    # (sibling style). ci-secure checks a small set of critical exploit chains
    # on purpose; a reader must never take this report for a comprehensive
    # security audit.
    out.append(f"> {_SCOPE_HONESTY_LINE}")
    out.append("")

    # Network-gated check statuses. A skipped check renders loudly here —
    # its pattern produced no findings, and silence would read as a pass.
    gh_block = _gh_checks_block(
        gh_checks, findings_json.get("gh_check_details")
    )
    if gh_block:
        out.append(gh_block)
        out.append("")

    # Malformed-finding banner — findings dropped at the validation boundary
    # are named here so an off-contract finding is loud, never a buried
    # silent default.
    malformed_banner = _malformed_findings_banner(malformed_findings)
    if malformed_banner:
        out.append(malformed_banner)
        out.append("")

    # Coverage-gap banner — if the static scan couldn't read/parse a file,
    # say so loudly and up front. An unscanned file must never be mistaken
    # for a clean one.
    gap_banner = _coverage_gap_banner(
        scan_incomplete, dropped_matches, coverage_notes)
    if gap_banner:
        out.append(gap_banner)
        out.append("")

    # The headline — one `## ` line stating the verdict, ci-score-style. It
    # replaces both the old executive summary and the old action plan: the
    # ranked finding order below IS the plan, exactly as in the siblings.
    # Zero findings is a first-class positive result and folds under the same
    # headline rather than a separate `## Result` section.
    out.append(_headline_block(groups, scanned_workflows, n_vectors,
                               coverage_complete))
    out.append("")

    # The vector map — the analog of ci-score's check table and ci-speedup's
    # long-pole map. It renders in BOTH cases: on a clean report the ten ✅
    # rows are the result, not decoration.
    out.append(_chain_status_block(
        groups, catalog_sections, scanned_workflows, gh_checks,
    ))
    out.append("")

    # The config hygiene facts. They sit right after the vector map because
    # they answer the other half of "how is this repo doing": the vectors are
    # open doors that were found, the facts are armor that is present or
    # absent. Rendered as pass/fail rows only — no aggregate is shown here (the
    # score exists for ci-advisor's blend; verify_report fails a report that
    # renders one).
    score_block = _security_score_block(findings_json.get("security_score"))
    if score_block:
        out.append(score_block)
        out.append("")

    out.append("---")
    out.append("")

    if groups:
        # No `## Security findings` parent — per-finding sections are
        # top-level, like the siblings' recommendation / long-pole entries.
        for ordinal, _key, members in groups:
            out.append(
                _finding_group_section(
                    ordinal, members, catalog_sections, repo, sha,
                    catalog_url, findings_path=findings_path,
                )
            )
            out.append("")

    # Appendices at the end — reference material a reader drops into only
    # when they need to verify how a finding was produced (or what a ✅ vector
    # row actually asserts). The headline, vector map and findings carry the
    # actionable content and stay on top.
    out.append(_chain_appendix_block(catalog_sections))
    out.append("")
    out.append("---")
    out.append("")
    out.append(_methodology_block(n_vectors))
    out.append("")
    out.append("---")
    out.append("")
    out.append(_data_sources_block(skill_sha, sha, scanned_at, repo))
    out.append("")
    out.append("---")
    out.append("")
    out.append("Generated by [StarSling](https://starsling.dev) 💫")
    out.append("")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render scan.py findings JSON into a markdown report.",
    )
    parser.add_argument(
        "--in",
        dest="input_path",
        type=Path,
        default=None,
        help="Path to findings JSON (default: stdin).",
    )
    parser.add_argument(
        "--out",
        dest="output_path",
        type=Path,
        default=None,
        help="Path to write the report to (default: stdout).",
    )
    parser.add_argument(
        "--render-plan",
        dest="render_plan",
        action="store_true",
        help=(
            "Print the JSON array of groups that WOULD render, in render "
            "order ([{\"pattern\": id, \"dormant\": bool}, ...]), and exit 0 "
            "without rendering a report. List position is the group's report "
            "ordinal. Used by Phase 2.5 to scope attacker_scenario writing "
            "and by Phase 4 to build the selection table with the report's "
            "own numbering."
        ),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Path to security-patterns.md (default: bundled catalog).",
    )
    parser.add_argument(
        "--catalog-url",
        type=str,
        default=None,
        help=(
            "Base URL for catalog links — for a report published somewhere "
            "that hosts the catalog. By default the report links to the "
            f"published catalog (`{_CATALOG_PUBLIC_URL}`); a commit-pinned "
            "permalink 404s because the skill's commit is not a public-repo "
            "SHA."
        ),
    )
    args = parser.parse_args(argv)

    if args.input_path:
        raw = args.input_path.read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()

    try:
        findings_json = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid findings JSON: {e}", file=sys.stderr)
        return 1

    if args.render_plan:
        print(json.dumps(render_plan_keys(findings_json)))
        return 0

    findings_path = (
        args.input_path.resolve() if args.input_path else None
    )
    _t0 = time.monotonic()
    try:
        report = render(
            findings_json,
            catalog_path=args.catalog,
            catalog_url=args.catalog_url,
            findings_path=findings_path,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    logger.debug("Timing: report render=%.2fs", time.monotonic() - _t0)

    if args.output_path:
        args.output_path.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)

    # Close the timing loop. report.py is always the last step, so if scan.py
    # stamped `run_start_epoch` into the findings (it always runs first),
    # compute the reliable
    # end-to-end `total_run_s` (and `risk_scenario_s` — the prose phase between
    # the driver finishing and now) and record them back into the findings
    # timings. This replaces the orchestrator-managed stamp that proved fragile.
    timings = findings_json.get("timings")
    if args.input_path and isinstance(timings, dict) and "run_start_epoch" in timings:
        now = time.time()
        scripted_end = timings.get("scripted_end_epoch")
        scripted_total = timings.get("scripted_total_s")
        # Derive the total from its own components when both are known: the
        # driver's start and scan.py's wall-clock anchor are not the same
        # instant, so `now - run_start_epoch` could come out SMALLER than the
        # scripted phase it contains — a total that undercut its own parts.
        #
        # Stamped ONCE. Re-rendering the same findings file — to check a fix,
        # to regenerate after an edit — is not more run time, but `now` moves,
        # so recomputing inflated the run's recorded duration every time
        # anyone looked at it.
        if "total_run_s" not in timings:
            if isinstance(scripted_end, (int, float)) and isinstance(
                scripted_total, (int, float)
            ):
                timings["total_run_s"] = round(
                    scripted_total + (now - scripted_end), 2
                )
            else:
                timings["total_run_s"] = round(
                    now - timings["run_start_epoch"], 2
                )
        # The gap between the last script and now is only the scenario phase
        # if a scenario was actually written. Stamping it unconditionally
        # billed idle wall-clock — an operator reading the report, a crashed
        # session — as time spent on prose that never ran.
        scenario_ran = any(
            (f.get("attacker_scenario") or "").strip()
            for f in (findings_json.get("findings") or [])
            if isinstance(f, dict)
        )
        if not scenario_ran:
            # An earlier render may have stamped this when scenarios were
            # present. Leaving it behind attributes prose time to a run whose
            # findings now carry no prose at all.
            timings.pop("risk_scenario_s", None)
        elif ("risk_scenario_s" not in timings
                and isinstance(scripted_end, (int, float))):
            timings["risk_scenario_s"] = round(now - scripted_end, 2)
        try:
            args.input_path.write_text(
                json.dumps(findings_json, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as e:
            logger.debug("could not write total_run_s back to findings: %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""ci-secure report self-check — turns "did my change keep the report correct?"
into one PASS/FAIL command instead of a manual grep through ~2000 lines.

Parses a rendered ci-secure report (markdown) and asserts the structural
invariants every report must satisfy. Optionally cross-checks against the
findings JSON it came from and the skill's git checkout.

    verify_report.py --report ci-secure-report.md
    verify_report.py --report r.md --findings findings.json --skill-repo ~/Development/skills

Exit 0 if all checks pass, 1 otherwise. Each check prints PASS/FAIL + detail.

Standalone by design: no imports of report.py/scan.py/config (so it has no
PyYAML/config-collision dependency and can run anywhere a report lands —
including the e2e harness and CI).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_SEVERITIES = ("HIGH", "MEDIUM", "LOW", "MANUAL")

# The ten attack vectors ci-secure scans, duplicated here on purpose: this
# file is standalone (no imports of the skill's modules), so the manifest has
# to be literal. `tests/test_census_why_these_ten.py` asserts it equals the
# catalog's own set, which is what stops the two copies from drifting.
THE_TEN = frozenset({
    "P14.7", "P14.9", "P14.10", "P14.11", "P14.14",
    "P14.15", "P14.18", "P14.19", "P14.24", "P14.25",
})


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


# --- small parse helpers -----------------------------------------------------

def _header_value(report: str, field: str) -> str | None:
    """Value cell of a provenance row, or None.

    The provenance table is label-style (house style, shared with ci-score /
    ci-speedup): no ``| Field | Value |`` header row, a first row whose label
    is plain (``| Repository | … |``) doubling as the table header, and every
    later row bolded (``| **Audited commit** | … |``). Both shapes are accepted
    so a check can address any row by its label.
    """
    m = re.search(
        rf"^\|\s*(?:\*\*)?{re.escape(field)}(?:\*\*)?\s*\|\s*(.*?)\s*\|\s*$",
        report, re.MULTILINE,
    )
    return m.group(1) if m else None


_HEADLINE_TOTAL_RE = re.compile(
    r"^##\s+Critical findings:\s*\*\*(\d+)\*\*", re.MULTILINE
)


def _findings_total(report: str) -> int | None:
    """The report's canonical occurrence count.

    Read off the ``## Critical findings: **N**`` headline. It used to be read
    from a ``| **Findings** | N |`` provenance row, but that row was one of
    four places the same number appeared and was cut as restatement — the
    headline is the surviving statement of record, and binding these checks
    to it keeps them checking the number the reader actually sees.
    """
    m = _HEADLINE_TOTAL_RE.search(report)
    return int(m.group(1)) if m else None


def _severity_counts(cell: str) -> dict[str, int]:
    """Parse ``HIGH: 192 · MEDIUM: 320 · LOW: 191`` → {HIGH:192,...}."""
    out: dict[str, int] = {}
    for sev in _SEVERITIES:
        m = re.search(rf"{sev}:\s*([0-9]+)", cell)
        if m:
            out[sev] = int(m.group(1))
    return out


def _split_counts(text: str) -> dict[str, int]:
    """Parse a per-severity split like ``20 HIGH · 6 MEDIUM · 89 LOW``."""
    return {
        sev: int(n)
        for n, sev in re.findall(r"([0-9]+)\s+(HIGH|MEDIUM|LOW|MANUAL)", text)
    }


# --- checks (report-only) ----------------------------------------------------

def check_findings_total_matches_breakdown(report: str) -> Check:
    name = "headline findings total == sum of severity breakdown (by occurrence)"
    total = _findings_total(report)
    breakdown = _header_value(report, "Severity breakdown (by occurrence)")
    if total is None:
        return Check(name, False, "no `## Critical findings: **N**` headline")
    if breakdown is None:
        # report.py omits the breakdown row when there is nothing to break
        # down. Zero findings is a first-class clean result, so demanding the
        # row would fail every clean report; the row is only required once a
        # finding exists.
        if total == 0:
            return Check(name, True, "zero findings, no breakdown row", skipped=True)
        return Check(name, False, "no `Severity breakdown (by occurrence)` header row")
    counts = _severity_counts(breakdown)
    s = sum(counts.values())
    ok = s == total
    return Check(name, ok, f"{total} == {' + '.join(f'{v}' for v in counts.values())} = {s}"
                 if ok else f"Findings={total} but breakdown sums to {s} ({counts})")


def check_group_splits_sum_to_count(report: str) -> Check:
    name = "each group's per-severity split sums to its occurrence count"
    bad: list[str] = []
    n_checked = 0
    # Definition-list shape: `- **Occurrences:** N occurrences across M
    # workflows (20 HIGH · 6 MEDIUM · 89 LOW)`.
    for m in re.finditer(
        r"\*\*Occurrences:\*\*\s*([0-9]+)\s+occurrence.*?\(([^)]*(?:HIGH|MEDIUM|LOW)[^)]*)\)",
        report,
    ):
        count = int(m.group(1))
        split = _split_counts(m.group(2))
        if not split:
            continue
        n_checked += 1
        if sum(split.values()) != count:
            bad.append(f"{count} != {split} (={sum(split.values())})")
    if n_checked == 0:
        return Check(name, True, "no mixed-severity groups with a split", skipped=True)
    return Check(name, not bad, f"{n_checked} split group(s) consistent" if not bad
                 else "; ".join(bad))


def check_finding_anchors_resolve(report: str) -> Check:
    name = "every #finding-N reference resolves to an anchor"
    refs = set(re.findall(r"#(finding-[0-9]+)\b", report))
    anchors = set(re.findall(r'<a\s+id="(finding-[0-9]+)"', report))
    missing = sorted(refs - anchors)
    return Check(name, not missing,
                 f"{len(refs)} refs, all resolve" if not missing
                 else f"referenced but no anchor: {', '.join(missing)}")


def check_chain_anchors_resolve(report: str) -> Check:
    """Every `#chain-*` jump (the vector map's ✅ rows) has an appendix anchor.

    A ✅ row asserts "checked and clean"; the link to what was checked is
    what makes that falsifiable. A dead link quietly removes the receipt.
    """
    name = "every #chain-* reference resolves to an appendix anchor"
    refs = set(re.findall(r"#(chain-[a-z0-9-]+)\)", report))
    anchors = set(re.findall(r'<a\s+id="(chain-[a-z0-9-]+)"', report))
    missing = sorted(refs - anchors)
    return Check(name, not missing,
                 f"{len(refs)} vector ref(s), all resolve" if not missing
                 else f"referenced but no anchor: {', '.join(missing)}")


def check_anchor_precedes_its_heading(report: str) -> Check:
    """`<a id="finding-N">` sits BEFORE its `## Finding N` heading.

    An anchor emitted after the heading scrolls the heading out of view, so
    a `#finding-N` jump lands the reader mid-body with no title on screen.
    """
    name = "every finding anchor precedes its heading (jumps land on the title)"
    bad: list[str] = []
    for m in re.finditer(r'<a\s+id="(finding-\d+)"></a>\s*\n\s*\n(.*)', report):
        anchor_id, next_line = m.group(1), m.group(2)
        if not next_line.startswith("## "):
            bad.append(anchor_id)
    anchors = re.findall(r'<a\s+id="finding-\d+"></a>', report)
    if not anchors:
        return Check(name, True, "no finding anchors", skipped=True)
    return Check(name, not bad,
                 f"all {len(anchors)} anchor(s) precede their heading" if not bad
                 else f"anchor(s) not followed by a `## ` heading: {bad}")


def check_no_broken_definition_rows(report: str) -> Check:
    """No finding-detail bullet spills a bare continuation line.

    The detail body used to be a `| Field | Value |` table. Several catalog
    TL;DRs are multi-line, and a table cell cannot hold a newline: the row
    terminated mid-cell and the rest of the prose fell out of the table as
    loose text. The definition list flattens each value to one line, so any
    line that looks like the tail of a wrapped catalog paragraph — i.e. sits
    directly under a `- **Label:**` bullet without being a bullet, a blank,
    a heading, or a fence — is that bug coming back.
    """
    name = "no finding-detail bullet spills a bare continuation line"
    lines = report.splitlines()
    bad: list[str] = []
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not re.match(r"^- \*\*[^*]+:\*\* ", line):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.strip() and not re.match(r"^(- |\s|#|<|\||>|_|\*)", nxt):
            bad.append(nxt[:60])
    return Check(name, not bad,
                 "every detail bullet is self-contained" if not bad
                 else f"{len(bad)} bare continuation line(s): {bad[:3]}")


# Paths the catalog's own fix recipes recommend to the reader. Not scratch
# pointers — advice that happens to live under /tmp on the runner.
_CATALOG_DOCUMENTED_PATHS = frozenset({"/tmp/.buildx-cache"})


def check_no_absolute_scratch_paths(report: str) -> Check:
    """The saved report never points at a tmp path.

    The report outlives the scratch directory it was rendered from, so a
    `/private/tmp/…/ci-secure-findings-<slug>.json` reference is a pointer at
    something the OS garbage-collects — and, on a shared report, a leak of
    the author's local paths.

    Two exemptions, both because the path is not a pointer at data the
    report depends on:

    - Fenced blocks are copy-paste commands for the reader's own agent — an
      `--out /tmp/…` there names a file the agent creates.
    - The ``Repository`` provenance row records WHERE the audited checkout
      was (the sibling convention: "local checkout at `<root>`"). That is a
      statement about the scan, not a link the reader is meant to follow.
      **This exemption is deliberate and settled** (raised repeatedly during
      development, and decided rather than left open): the checkout path is
      genuine provenance — it says which tree the report's file:line
      references are true of — and on a user's own run it is their own path in
      their own report. ``report.py`` abbreviates ``$HOME`` to ``~`` where it
      applies, which drops the account name and keeps the fact.
    - A path the CATALOG documents as advice (P14.19 tells the reader to
      scope a docker cache to `/tmp/.buildx-cache`) is a recommendation, not
      a pointer at this run's scratch directory. The allowlist is literal and
      short on purpose: everything else stays red.
    """
    name = "no absolute tmp/scratch path referenced in the report's prose"
    hits: list[str] = []
    in_fence = False
    for line in report.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or re.match(r"^\|\s*(?:\*\*)?Repository(?:\*\*)?\s*\|", line):
            continue
        hits += [
            h for h in re.findall(r"(?:/private)?/(?:tmp|var/folders)/[^\s`)]*", line)
            if h not in _CATALOG_DOCUMENTED_PATHS
        ]
    return Check(name, not hits, "clean" if not hits
                 else f"{len(hits)} scratch path(s) referenced: {hits[:3]}")


# Words a fix summary ends on only when it is a lead-in clause whose actual
# content — the numbered options — was dropped. "Two structural options." and
# "(in order of preference)." both passed the dangling-colon guard because the
# colon had been rewritten to a period.
_LEAD_IN_TAIL_RE = re.compile(
    r"\b(options|preference|follows|below|following|steps|remediations|"
    r"file type)\b[)\]]*\.\s+See \[",
    re.IGNORECASE,
)


def check_no_fragment_fix_summary(report: str) -> Check:
    """A fix summary must state the fix, not announce that one exists."""
    name = "no fix summary is a bare lead-in fragment"
    hits = _LEAD_IN_TAIL_RE.findall(report)
    return Check(name, not hits, "clean" if not hits
                 else f"{len(hits)} fix summary/summaries name no actual fix "
                      f"(lead-in clause only): {hits[:3]}")


# A link to `…/security-patterns.md` on github.com. The report used to emit one
# per finding, pinned to the skill checkout's own HEAD sha — a commit that was
# never pushed to the public repo (and, from a dirty tree, may never have
# existed anywhere). github.com resolves the path against a sha it does not
# have, so every link 404s, and nothing caught it because the structural checks
# only ever verified that the ANCHOR was well formed.
_ABSOLUTE_CATALOG_LINK_RE = re.compile(
    r"https?://[^\s)]*security-patterns\.md[^\s)]*"
)

# Every MARKDOWN LINK whose destination is the catalog, in any spelling —
# absolute, or the bare relative path a previous scheme emitted. Matching link
# destinations rather than the bare filename is what keeps the data-sources
# table's backticked `skills/ci-secure/references/security-patterns.md` — prose
# naming the catalog, not a link to it — out of the check.
_CATALOG_LINK_TARGET_RE = re.compile(
    r"\]\(([^)]*security-patterns\.md[^)]*)\)"
)


_CATALOG_PUBLIC_PREFIX = ("https://github.com/starslingdev/skills/blob/main/"
                          "skills/ci-secure/references/security-patterns.md")


def check_catalog_links_are_published(report: str, catalog_url: str | None = None) -> Check:
    """Catalog links use the published catalog URL unless a URL was given.

    A commit-pinned permalink to a SHA the public repo never had is the rot
    this catches: every catalog link must be the main-branch public URL
    (stable path, stable pattern-id anchors) — any OTHER host or path is a
    link the reader cannot open.

    Checked over LINK DESTINATIONS, not just absolute URLs, because both
    schemes this replaced are failures the check has to be able to see: a
    commit-pinned permalink (wrong absolute URL) and a bare relative path
    (resolves nowhere, since the report is not written beside the catalog).
    Matching only `https?://…` would let the relative scheme back in silently.
    """
    name = "catalog links point at the published catalog (or --catalog-url)"
    if catalog_url:
        return Check(name, True, f"--catalog-url was passed ({catalog_url})",
                     skipped=True)
    targets = _CATALOG_LINK_TARGET_RE.findall(report)
    targets += [
        h for h in _ABSOLUTE_CATALOG_LINK_RE.findall(report)
        if not any(h in t for t in targets)
    ]
    bad = [t for t in targets if not t.startswith(_CATALOG_PUBLIC_PREFIX)]
    return Check(name, not bad, "clean" if not bad
                 else f"{len(bad)} catalog link(s) not on the published "
                      f"main-branch path: {bad[:2]}")


_CATALOG_FILE = (Path(__file__).resolve().parent.parent
                 / "references" / "security-patterns.md")


def _heading_slug(text: str) -> str:
    """GitHub's heading-anchor slug: lowercase, drop punctuation, spaces to
    hyphens. `### P14.10 — Template Injection in \\`run:\\` Blocks` becomes
    `p1410--template-injection-in-run-blocks`."""
    s = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return s.replace(" ", "-")


def check_catalog_anchors_are_live(
    report: str, catalog_file: Path | None = None
) -> Check:
    """Every `#anchor` the report emits on a catalog link is a real heading.

    The URL check next door proves the report points at the published FILE.
    It cannot see the other half of the 404: an anchor for a heading that was
    renamed or removed, which lands the reader at the top of a 700-line catalog
    with no idea which entry they were sent to. The catalog ships inside the
    skill, so this costs one local file read and no network.
    """
    name = "every catalog link anchor matches a real heading in the catalog"
    path = catalog_file or _CATALOG_FILE
    anchors = sorted({
        m.group(1)
        for m in re.finditer(r"security-patterns\.md#([A-Za-z0-9_-]+)", report)
    })
    if not anchors:
        return Check(name, True, "no catalog anchors in the report", skipped=True)
    try:
        catalog = path.read_text(encoding="utf-8")
    except OSError as e:
        return Check(name, False, f"could not read the catalog at {path}: {e}")
    live = {
        _heading_slug(m.group(1))
        for m in re.finditer(r"^#{1,6}\s+(.*?)\s*$", catalog, re.MULTILINE)
    }
    dead = [a for a in anchors if a not in live]
    return Check(name, not dead,
                 f"{len(anchors)} anchor(s) all resolve to a catalog heading"
                 if not dead
                 else f"{len(dead)} anchor(s) match no catalog heading: "
                      f"{dead[:3]}")


def check_no_dangling_colon_before_link(report: str) -> Check:
    name = "no dangling colon before 'See [' (fix-summary lead-in bug)"
    hits = re.findall(r":\s+See \[", report)
    return Check(name, not hits, "clean" if not hits
                 else f"{len(hits)} fix summaries end with a dangling colon before 'See ['")


def check_coverage_row_consistent(report: str) -> Check:
    name = "Coverage row present and consistent with the gap banner"
    cov = _header_value(report, "Coverage")
    if cov is None:
        return Check(name, False, "no `Coverage` header row")
    has_banner = "Incomplete coverage" in report
    is_complete = "complete" in cov.lower() and "partial" not in cov.lower()
    if is_complete and has_banner:
        return Check(name, False, "Coverage says complete but an Incomplete-coverage banner is present")
    if not is_complete and not has_banner:
        return Check(name, False, "Coverage says PARTIAL but no Incomplete-coverage banner is present")
    return Check(name, True, f"Coverage={'complete' if is_complete else 'PARTIAL'}, banner={'yes' if has_banner else 'no'}")


def check_scanned_date_present(report: str, report_path: Path | None) -> Check:
    """The report states WHEN it was produced, in the `Scanned` row.

    This check used to key on a date embedded in the filename. The saved
    report now has one stable name (`./ci-secure-report.md`, like both
    siblings) so a re-run overwrites rather than accreting dated copies —
    which leaves the `Scanned` row as the only place the run date lives, and
    therefore the only thing worth checking. A report with no readable date
    can't be told apart from a stale one.
    """
    name = "Scanned (UTC) provenance row carries a well-formed date"
    date_cell = _header_value(report, "Scanned")
    if not date_cell:
        return Check(name, False, "no `Scanned` provenance row")
    got = re.match(r"\s*(\d{4}-\d{2}-\d{2})\b", date_cell)
    if not got:
        return Check(name, False, f"`Scanned` row carries no date: {date_cell!r}")
    # A dated filename is no longer produced, but an archived copy may still
    # carry one — when it does, it must agree with the row.
    if report_path is not None:
        fn = re.search(r"(\d{4}-\d{2}-\d{2})", report_path.name)
        if fn and fn.group(1) != got.group(1):
            return Check(name, False,
                         f"header Scanned {got.group(1)!r} != the date in the "
                         f"filename {fn.group(1)!r}")
    return Check(name, True, got.group(1))


_BANNER_RE = re.compile(
    r"^CI Secure\s+(\d+) critical finding(?:s)?\s+▏(\d+) of (\d+) vectors hit▕\s+"
    r"(\d+) workflows?\s+·\s+impostor check (.+?)\s*$",
    re.MULTILINE,
)
_CHAIN_ROW_RE = re.compile(r"^\|\s*(✅|🟥|🟧|⬜|📄|⚠️)\s*\|\s*`(P[\d.]+)`", re.MULTILINE)


def check_banner_present_and_consistent(report: str) -> Check:
    """The pre-drawn banner is present and every number on it is the report's.

    The banner is the line the orchestrator copies verbatim into the terminal
    (SKILL.md phase 3), so it is the ONE line most readers see. A banner drawn
    by hand — or left stale after a re-render — states a finding count nobody
    can trace, which is exactly the failure the pre-drawn line exists to
    prevent. Three bindings: the finding count IS the one in the
    `## Critical findings: **N**` headline, the vectors-hit count IS the number
    of non-clean rows in the vector-status table, and the impostor word IS one
    of the four honest states.
    """
    name = "banner present and consistent (findings, vectors hit, impostor state)"
    m = _BANNER_RE.search(report)
    if not m:
        return Check(name, False, "no `CI Secure … vectors hit …` banner line")
    shown, hit, total, workflows, impostor = (
        int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)),
        m.group(5),
    )
    problems: list[str] = []
    total_findings = _findings_total(report)
    if total_findings is None:
        problems.append(
            "no `## Critical findings: **N**` headline to compare the banner against")
    elif total_findings != shown:
        problems.append(
            f"banner says {shown} finding(s) but the headline says {total_findings}")
    if total != len(THE_TEN):
        problems.append(f"banner claims {total} vectors, not {len(THE_TEN)}")
    rows = _CHAIN_ROW_RE.findall(report)
    if not rows:
        problems.append("no vector-status table to reconcile the vectors-hit count against")
    else:
        hit_rows = sum(1 for mark, _pid in rows if mark not in ("✅", "⚠️"))
        if hit_rows != hit:
            problems.append(
                f"banner says {hit} vector(s) hit but the vector-status table "
                f"marks {hit_rows}")
        if len(rows) != len(THE_TEN):
            problems.append(
                f"vector-status table lists {len(rows)} vectors, not {len(THE_TEN)}")
    if impostor not in ("ran", "partial", "SKIPPED", "not recorded"):
        problems.append(f"banner impostor state {impostor!r} is not one of the "
                        "four honest states")
    return Check(name, not problems,
                 f"{shown} finding(s), {hit}/{total} vectors hit, {workflows} "
                 f"workflow(s), impostor {impostor}"
                 if not problems else "; ".join(problems))


def check_vector_status_table_covers_the_ten(report: str) -> Check:
    """All ten vectors render a status row — including the clean ones.

    A findings table alone cannot distinguish "this vector was checked and came
    back clean" from "this vector never ran". Listing every vector is what
    makes a silently-missing detector visible to a reader, so the table is an
    invariant, not decoration.
    """
    name = "vector-status table lists all ten vectors (clean ones included)"
    rows = _CHAIN_ROW_RE.findall(report)
    if not rows:
        return Check(name, False, "no vector-status table in the report")
    listed = {pid for _mark, pid in rows}
    missing = sorted(THE_TEN - listed)
    extra = sorted(listed - THE_TEN)
    problems = []
    if missing:
        problems.append(f"vectors with no status row: {missing}")
    if extra:
        problems.append(f"rows for vectors outside the manifest: {extra}")
    return Check(name, not problems,
                 f"all {len(THE_TEN)} vectors have a status row"
                 if not problems else "; ".join(problems))


def check_no_duplicate_occurrences(report: str) -> Check:
    name = "no exact-duplicate occurrences within a finding (dedup intact)"
    bad: list[str] = []
    # Copy-prompt blocks: "Occurrences (N):" then "- file:line — jobs: ..." bullets.
    for m in re.finditer(r"Occurrences \((\d+)\):\n((?:- .*\n?)+)", report):
        declared = int(m.group(1))
        bullets = [b for b in m.group(2).splitlines() if b.startswith("- ")]
        if len(bullets) != len(set(bullets)):
            dupes = sorted({b for b in bullets if bullets.count(b) > 1})
            bad.append(f"duplicate occurrence bullet(s): {dupes[:3]}")
        if declared != len(bullets):
            bad.append(f"declared {declared} occurrences but listed {len(bullets)}")
    return Check(name, not bad, "no duplicate/under-counted occurrence lists" if not bad
                 else "; ".join(bad))


# --- checks (cross-reference, optional) --------------------------------------

def check_skill_commit_provenance(report: str, skill_repo: Path | None) -> Check:
    name = "report's skill commit matches the skill checkout (provenance)"
    m = re.search(r"skill commit `([0-9a-f]+)(-dirty)?`", report)
    if not m:
        # An INSTALLED skill has no .git, so it stamps a version instead of a
        # commit sha. That is a valid provenance state — SKIP, do not FAIL, or
        # every real user's clean report would fail its own self-check.
        vm = re.search(r"skill v([0-9]+\.[0-9]+\.[0-9]+) — commit unknown", report)
        if vm:
            return Check(
                name, True,
                f"installed skill v{vm.group(1)} — no git commit, version-stamped",
                skipped=True,
            )
        return Check(name, False,
                     "no `skill commit` or version stamp recorded in the Scanner row")
    recorded, dirty = m.group(1), bool(m.group(2))
    if skill_repo is None:
        note = "recorded " + recorded + ("-dirty" if dirty else "")
        return Check(name, True, note + " (no --skill-repo to compare)", skipped=True)
    try:
        head = subprocess.run(
            ["git", "-C", str(skill_repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return Check(name, True, "git unavailable", skipped=True)
    ok = head.startswith(recorded) or recorded.startswith(head)
    detail = f"recorded {recorded}{'-dirty' if dirty else ''} vs HEAD {head}"
    if ok and dirty:
        detail += " — recorded with uncommitted changes (provenance flagged, good)"
    return Check(name, ok, detail)


def check_against_findings_json(report: str, findings_path: Path | None) -> Check:
    name = "headline findings total matches the findings JSON occurrence count"
    if findings_path is None:
        return Check(name, True, "no --findings to compare", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Check(name, False, f"could not read findings JSON: {e}")
    raw = data.get("findings") or []
    # Dedup the same way report.py does, so the comparison reflects what the
    # report should show.
    seen, deduped = set(), 0
    for f in raw:
        key = (f.get("source", "ci-secure"), f.get("pattern", ""),
               f.get("workflow_file", ""), f.get("line", 0), (f.get("evidence") or "").strip())
        if key not in seen:
            seen.add(key)
            deduped += 1
    total = _findings_total(report)
    if total is None:
        return Check(name, False, "no `## Critical findings: **N**` headline")
    ok = total == deduped
    return Check(name, ok, f"report {total} == deduped JSON {deduped}" if ok
                 else f"report headline={total} but deduped JSON has {deduped} "
                      f"(raw {len(raw)})")


def _git_modified_files(clone: Path) -> set[str] | None:
    """Workflow files changed vs HEAD in the audited clone (the Phase 5 edits)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(clone), "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def _finding_pattern_map(report: str) -> dict[str, str]:
    """Finding N -> pattern.

    Read off the vector map's hit rows (``| 🟥 | `P14.10` — [title](#finding-1)
    | … |``), which is where the ordinal↔pattern binding lives now that the
    separate Findings table is gone.
    """
    return {
        m.group(2): m.group(1)
        for m in re.finditer(
            r"^\|\s*[^|✅⚠️]*\|\s*`(P[\d.]+)`\s*—\s*\[[^\]]*\]\(#finding-(\d+)\)",
            report, re.MULTILINE,
        )
    }


def check_clone_fixes(
    report: str, findings_path: Path | None, clone: Path | None,
) -> Check:
    """Cross-check the report's recorded fixes against what changed in the clone.

    Catches two silent failures: (a) a finding marked FIXED whose occurrence
    files weren't actually modified (a partial or no-op fix), and (b) workflow
    files changed in the clone that the report doesn't account for as a fix
    (e.g. a trimmed finding fixed in Phase 5 with no `## Fixes applied` entry).
    Needs --clone; uses --findings to map a FIXED finding to its occurrences.
    """
    name = "report's recorded fixes match the files changed in the clone"
    if clone is None:
        return Check(name, True, "no --clone to cross-check", skipped=True)
    modified = _git_modified_files(clone)
    if modified is None:
        return Check(name, True, "clone is not a git repo / git unavailable", skipped=True)

    occ_by_pattern: dict[str, set[str]] = {}
    if findings_path:
        try:
            data = json.loads(findings_path.read_text(encoding="utf-8"))
            for f in data.get("findings") or []:
                occ_by_pattern.setdefault(f.get("pattern", ""), set()).add(
                    f.get("workflow_file", ""))
        except (OSError, ValueError):
            pass

    pat_map = _finding_pattern_map(report)
    accounted: set[str] = set()
    problems: list[str] = []
    for m in re.finditer(
        r"^## (?!#)\s*(PARTIALLY FIXED|FIXED)\s+—.*?Finding\s+(\d+)", report, re.MULTILINE,
    ):
        kind, n = m.group(1), m.group(2)
        files = occ_by_pattern.get(pat_map.get(n, ""), set())
        accounted |= files
        if files and kind == "FIXED" and (files - modified):
            problems.append(
                f"Finding {n} ({pat_map.get(n)}) marked FIXED but these "
                f"occurrence files are unchanged in the clone: {sorted(files - modified)}")
    # Files named in a `## Fixes applied` record also count as accounted-for.
    fa = re.search(r"^##\s+Fixes applied(.*?)(?:\n##\s|\Z)", report, re.S | re.MULTILINE)
    if fa:
        accounted |= set(re.findall(r"\.github/workflows/[\w./-]+\.ya?ml", fa.group(1)))
    unaccounted = {f for f in modified if f.endswith((".yml", ".yaml"))} - accounted
    if unaccounted:
        problems.append(
            f"{len(unaccounted)} workflow file(s) changed in the clone but not "
            f"recorded as a fix in the report (FIXED heading or `## Fixes "
            f"applied`): {sorted(unaccounted)[:6]}")
    return Check(name, not problems,
                 "every FIXED finding's files changed and every changed file is recorded"
                 if not problems else "; ".join(problems))


def check_scope_honesty_line(report: str) -> Check:
    """The critical-only contract: every report carries the verbatim scope line."""
    name = "scope-honesty line present (critical-only, not comprehensive)"
    needle = "Critical exploit-chain checks only"
    ok = needle in report and "not a comprehensive audit" in report
    return Check(name, ok, "present" if ok else f"missing the verbatim scope line ({needle!r} …)")


def check_gh_impostor_status_line(report: str) -> Check:
    """The network-gated check's ran/partial/skipped status must render, and
    an unverified pin must never carry the ✅ that means "checked and clean".

    A skipped check silently absent from the report reads as a pass; a
    partial run rendered ✅ asserts "verified" of pins nobody could resolve,
    which is the same false negative wearing a green tick.
    """
    name = "impostor-SHA (P14.11) ran/partial/skipped status rendered honestly"
    if "P14.11" not in report:
        return Check(name, False, "no P14.11 status anywhere in the report")
    skipped = re.search(r"P14\.11[^\n]*(SKIPPED|skipped)", report)
    ran = re.search(r"P14\.11[^\n]*(ran|verified|PARTIAL|partial)", report)
    if not (skipped or ran):
        return Check(name, False,
                     "P14.11 mentioned but no ran / PARTIAL / SKIPPED state stated")
    # Any line that says a pin was not verified must not be a ✅ line.
    green_unverified = [
        ln.strip() for ln in report.splitlines()
        if "✅" in ln and re.search(r"UNVERIFIED|INCONCLUSIVE", ln)
    ]
    if green_unverified:
        return Check(name, False,
                     "unverified/inconclusive pin rendered with ✅ (reads as "
                     f"checked-and-clean): {green_unverified[:2]}")
    state = "skipped, loudly" if skipped else "ran/partial"
    return Check(name, True, state)


def check_catalog_manifest_complete(findings_path: Path | None) -> Check:
    """Every one of the ten vectors was actually evaluated.

    ``scan.py`` stamps the patterns it loaded into
    ``catalog_patterns_evaluated``. A catalog entry that fails to load
    produces no findings, which is indistinguishable in the report from a
    vector that found nothing — so the report would read clean on a check
    that never ran. Comparing the stamp against the ten-vector manifest is
    what makes a shrunken catalog visible.
    """
    name = "scan evaluated all ten catalog vectors (no silently dropped entry)"
    if findings_path is None:
        return Check(name, True, "no --findings to compare", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Check(name, False, f"could not read findings JSON: {e}")
    evaluated = data.get("catalog_patterns_evaluated")
    if evaluated is None:
        # Findings produced before the stamp existed can't be checked.
        return Check(name, True, "findings JSON carries no "
                     "`catalog_patterns_evaluated`", skipped=True)
    got = set(evaluated)
    missing = sorted(THE_TEN - got)
    extra = sorted(got - THE_TEN)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"never evaluated: {missing}")
        if extra:
            parts.append(f"not in the ten-vector manifest: {extra}")
        return Check(name, False, "; ".join(parts))
    return Check(name, True, f"all {len(THE_TEN)} vectors evaluated")


# Finding sections are top-level (`## `), like the siblings' recommendation /
# long-pole entries. `(?!#)` keeps a deeper heading from matching.
_FINDING_HEADING_RE = re.compile(
    r"^## (?!#)(?:(?:PARTIALLY )?FIXED — )?(?:\S+ )?Finding \d+", re.M)


def _rendered_finding_sections(report: str) -> str:
    """Just the report's `## Finding N` sections, concatenated.

    Pattern ids also appear OUTSIDE the finding sections — most importantly
    the header's impostor-SHA status line, which names `P14.11` in every
    report whether or not P14.11 produced a finding. Searching the WHOLE
    report for a pattern id therefore cannot go red on a trimmed P14.11
    group, which is precisely the bug `check_every_group_rendered` exists to
    catch. Scoping the search to the finding sections is what lets it.
    """
    starts = [m.start() for m in _FINDING_HEADING_RE.finditer(report)]
    if not starts:
        return ""
    bounds = starts[1:] + [len(report)]
    chunks = []
    for start, stop in zip(starts, bounds):
        chunk = report[start:stop]
        # A section ends at the next top-level `## ` heading — the next
        # finding, an appendix, or `## Fixes applied`. Search from the line
        # AFTER the section's own heading, and require exactly two hashes, so
        # the heading can't terminate its own section.
        body_at = chunk.find("\n") + 1
        tail = re.search(r"^## (?!#)", chunk[body_at:], re.M) if body_at else None
        if tail:
            chunk = chunk[: body_at + tail.start()]
        chunks.append(chunk)
    return "\n".join(chunks)


def check_every_group_rendered(report: str, findings_path: Path | None) -> Check:
    """Budget contract: EVERY pattern group in the findings JSON has a rendered
    finding section — no tiering, no topping-up, no trimming."""
    name = "every findings-JSON group has a rendered section (no trimming)"
    if findings_path is None:
        return Check(name, True, "no --findings to compare", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Check(name, False, f"could not read findings JSON: {e}")
    patterns = {f.get("pattern") for f in (data.get("findings") or [])}
    sections = _rendered_finding_sections(report)
    missing = sorted(
        p for p in patterns
        if p and f"`{p}`" not in sections and p not in sections
    )
    return Check(name, not missing,
                 f"all {len(patterns)} group(s) rendered" if not missing
                 else f"groups missing from the report: {missing}")


def check_scenarios_on_rendered_findings(report: str) -> Check:
    """Every rendered finding carries its comprehension mechanism."""
    name = "every rendered finding has a 'What an attacker could do' row"
    # Heading shape: `## 🟥 Finding N: title — N sites / M workflows` (severity
    # emoji varies), with an optional `FIXED — ` / `PARTIALLY FIXED — ` prefix
    # after the `## `.
    n_findings = len(_FINDING_HEADING_RE.findall(report))
    # Strict rows only. The old `len(rows) or len(re.findall(<bare phrase>))`
    # fallback said: when nothing matched the real shape, count the bare phrase
    # instead — turning "the mechanism is missing on every finding" into a
    # pass. Zero strict matches with findings on the page is red, not a cue to
    # count differently.
    rows = re.findall(r"What an attacker could do:\*\*\s*(.*)", report)
    n_scenarios = len(rows)
    if n_findings == 0:
        return Check(name, True, "zero findings (first-class clean report)", skipped=True)
    # An authored, repo-grounded scenario and the catalog's generic capability
    # line are not the same claim. The fallback carries a visible marker, so
    # this check reports the split instead of counting both as "a scenario" —
    # and when EVERY row is the marker, the comprehension layer did not run at
    # all and the report fails rather than reporting a split.
    n_fallback = sum(1 for r in rows if "catalog description" in r)
    if n_scenarios and n_fallback == n_scenarios:
        return Check(name, False,
                     f"all {n_scenarios} scenario row(s) for {n_findings} "
                     "finding(s) are the catalog's generic description — not "
                     "one repo-specific scenario was written")
    ok = n_scenarios >= n_findings
    detail_tail = (f" ({n_fallback} are the catalog's generic description, "
                   f"not repo-specific)" if n_fallback else "")
    return Check(name, ok,
                 f"{n_scenarios} scenario row(s) for {n_findings} finding(s)"
                 f"{detail_tail}" if ok
                 else f"only {n_scenarios} scenario row(s) for {n_findings} finding(s)")


def _cell_text(value: object) -> str:
    return " ".join(str(value or "").split()).replace("|", "\\|")


def check_no_scanner_internal_markers(report: str) -> Check:
    """No scanner-internal stand-in survives into the rendered report.

    The detector rewrites two things it cannot resolve into placeholders — an
    opaque directory (a NUL-prefixed `wd:` key) and an expression token
    (`$EXPRn`) — and both have escaped into `derived_note`, findings.json and
    the markdown at different times. A reader shown a raw control character
    where their own directory belongs cannot check the finding at all, and this
    verifier passed on a report that was leaking. Cheap to assert, and it
    catches any future marker that forgets to be rendered back.
    """
    name = "no scanner-internal marker reaches the report"
    banned = [
        ("\x00", "the opaque-directory sentinel (a NUL byte)"),
        ("\\u0000", "an escaped NUL"),
        ("\x00wd:", "the opaque-directory prefix"),
        ("$SUBST", "the command-substitution stand-in"),
        ("$SELF_REPO", "the self-repository stand-in"),
    ]
    hits = [why for token, why in banned if token in report]
    if re.search(r"\$EXPR\d", report):
        hits.append("an expression stand-in (`$EXPRn`)")
    if hits:
        return Check(name, False, "; ".join(hits))
    return Check(name, True, "clean")


def check_no_rendered_security_score(report: str) -> Check:
    """The report renders NO aggregate score — no number, no ratio, no /100.

    Flipped deliberately, by design. The predecessor of this
    check REQUIRED a rendered `Security score: N/100 — X of Y scored facts
    pass` line, on the argument that a computed-but-unshown number is one the
    reader cannot check. An early run answered it: the line sat above ten green
    vector rows and read as a contradiction, because a hygiene aggregate and a
    vector scan measure different things. The aggregate is now machine-only —
    it stays in the findings JSON for ci-advisor and is never rendered here.
    This check exists so a future review cannot quietly restore it.
    """
    name = "no aggregate security score is rendered in the report"
    banned = [
        ("Security score", "the score line"),
        ("/100", "a score out of 100"),
        ("scored facts pass", "a passed/scored ratio"),
        ("scored config facts", "a scored-facts framing"),
    ]
    hits = [why for token, why in banned if token in report]
    return Check(name, not hits, "clean — no aggregate rendered" if not hits
                 else f"the report renders {', '.join(hits)}")


def check_config_hygiene_facts_rendered(
    report: str, findings_path: Path | None
) -> Check:
    """Every config fact in the JSON is rendered as a pass/fail row.

    The aggregate goes; the FACTS stay. Dropping both would leave the reader
    with no hygiene information at all, which is the opposite of the point.
    """
    name = "every config hygiene fact in the findings JSON is rendered"
    if findings_path is None:
        return Check(name, True, "no --findings to compare", skipped=True)
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Check(name, False, f"could not read findings JSON: {e}")
    if "security_score" not in data:
        return Check(name, True, "no config facts in the JSON", skipped=True)
    score = data.get("security_score")
    if not isinstance(score, dict):
        return Check(name, False,
                     f"`security_score` is a {type(score).__name__}, not an "
                     "object")
    if "## 🧰 Config hygiene checks" not in report:
        return Check(name, False, "the JSON carries config facts but the "
                                  "report has no hygiene-checks section")
    facts = score.get("facts") if isinstance(score.get("facts"), list) else []
    if not facts:
        # No facts means nothing was checked. The report must SAY so — silence
        # reads as "nothing to report", which is how a crashed facts layer
        # once became an implicit pass.
        if "Nothing here was checked" not in report:
            return Check(name, False,
                         "the JSON carries a config-facts block with no facts "
                         "but the report never states that nothing was "
                         "checked")
    missing = [
        f.get("fact_id") for f in facts
        if isinstance(f, dict) and _cell_text(f.get("fact")) not in report
    ]
    if missing:
        return Check(name, False,
                     f"fact(s) not rendered: {', '.join(map(str, missing))}")
    return Check(name, True, f"{len(facts)} hygiene row(s) rendered")


# The complete set of level-2 (`## `) headings the renderer + orchestrator
# legitimately emit. Anything else at `^## ` outside a code fence is a FORGED
# heading — the classic attack is a scanned job name carrying backticks +
# newlines that breaks out of an evidence bullet/fence and forges a
# `## FIXED — …` heading to fake a clean/fixed result.
_ALLOWED_H2 = (
    re.compile(r"^## Critical findings: \*\*\d+\*\*"),
    re.compile(r"^## 🔗 Vector map"),
    re.compile(r"^## 🧰 Config hygiene checks"),
    re.compile(r"^## 📖 What each vector checks"),
    re.compile(r"^## ⚙️ Methodology"),
    re.compile(r"^## 🗄️ Data sources"),
    re.compile(r"^## Fixes applied"),
    # finding groups — optional orchestrator FIXED/PARTIALLY FIXED prefix,
    # then a severity emoji, then `Finding N:`.
    re.compile(
        r"^## (?:FIXED — |PARTIALLY FIXED — )?(?:🟥|🟧|⬜|📄) ?Finding \d+:"
    ),
)


def check_source_fences_quote_only_source(report: str) -> Check:
    """A ```yaml evidence fence must contain nothing but numbered source lines.

    report.py reserves the fenced, line-numbered block for text quoted
    verbatim from the workflow file. A sentence the scanner assembled that
    lands in there is dressed as source, and a reader who opens the file
    looking for it never finds it — the exact defect `_derived_evidence_block`
    was introduced to fix, and nothing enforced it until a later change put
    scanner prose back inside a fence.
    """
    name = "```yaml evidence fences quote only numbered source lines"
    # A blank quoted line renders as a bare `NN:` with nothing after it, so
    # the space is optional. Requiring it failed the self-check on any report
    # quoting a workflow with a blank line in the excerpt — which is most.
    gutter = re.compile(r"\d+:(?: |$)")
    bad: list[str] = []
    fence: list[str] | None = None
    for raw in report.splitlines():
        line = raw.strip()
        if fence is None:
            if line == "```yaml":
                fence = []
            continue
        if line == "```":
            # An EVIDENCE fence is one where ANY line carries the
            # line-number gutter. The catalog's fix recipes are also ```yaml
            # and are legitimately gutter-free, so they are not policed. Keying
            # off the FIRST line instead would let a fence whose excerpt opens
            # on a blank source line smuggle prose through unchecked.
            if any(gutter.match(ln) for ln in fence):
                bad += [ln[:110] for ln in fence if not gutter.match(ln)]
            fence = None
            continue
        if line:
            fence.append(line)
    if bad:
        return Check(
            name, False,
            f"{len(bad)} non-source line(s) inside a yaml evidence fence — "
            f"first: {bad[0]!r}",
        )
    return Check(name, True, "every fenced evidence line carries a gutter")


def check_no_forged_headings(report: str) -> Check:
    """Every `## ` heading (outside code fences) is one the renderer emits.

    A scanned string the audited repo controls (a job name, a workflow path)
    that carries backticks + newlines could break out of its bullet/fence and
    render an arbitrary `## …` heading — e.g. a fake `## FIXED — Finding 1`
    that reads as a false-clean. This invariant rejects any `^## ` line that
    does not match the known-heading manifest.
    """
    name = "no forged `## ` headings outside the known-heading set"
    bad: list[str] = []
    in_fence = False
    for line in report.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## ") and not any(p.match(line) for p in _ALLOWED_H2):
            bad.append(line[:70])
    return Check(name, not bad,
                 "all `## ` headings are renderer-emitted" if not bad
                 else f"{len(bad)} forged heading(s): {bad[:3]}")


def run_checks(
    report: str, report_path: Path | None,
    findings_path: Path | None, skill_repo: Path | None,
    clone: Path | None = None,
    catalog_url: str | None = None,
) -> list[Check]:
    return [
        check_findings_total_matches_breakdown(report),
        check_group_splits_sum_to_count(report),
        check_finding_anchors_resolve(report),
        check_chain_anchors_resolve(report),
        check_anchor_precedes_its_heading(report),
        check_no_broken_definition_rows(report),
        check_no_absolute_scratch_paths(report),
        check_no_dangling_colon_before_link(report),
        check_no_fragment_fix_summary(report),
        check_catalog_links_are_published(report, catalog_url),
        check_catalog_anchors_are_live(report),
        check_coverage_row_consistent(report),
        check_scanned_date_present(report, report_path),
        check_no_duplicate_occurrences(report),
        check_skill_commit_provenance(report, skill_repo),
        check_against_findings_json(report, findings_path),
        check_clone_fixes(report, findings_path, clone),
        check_scope_honesty_line(report),
        check_gh_impostor_status_line(report),
        check_catalog_manifest_complete(findings_path),
        check_every_group_rendered(report, findings_path),
        check_scenarios_on_rendered_findings(report),
        check_banner_present_and_consistent(report),
        check_vector_status_table_covers_the_ten(report),
        check_no_rendered_security_score(report),
        check_no_scanner_internal_markers(report),
        check_config_hygiene_facts_rendered(report, findings_path),
        check_no_forged_headings(report),
        check_source_fences_quote_only_source(report),
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verify a ci-secure report's invariants.")
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--findings", type=Path, help="findings JSON the report came from")
    p.add_argument("--skill-repo", type=Path, help="skill git checkout, to check commit provenance")
    p.add_argument("--clone", type=Path, help="audited repo checkout, to confirm recorded fixes actually changed files")
    p.add_argument("--catalog-url", type=str, default=None,
                   help="the --catalog-url the report was rendered with, if any")
    args = p.parse_args(argv)

    report = args.report.read_text(encoding="utf-8")
    checks = run_checks(report, args.report, args.findings, args.skill_repo,
                        args.clone, args.catalog_url)

    failed = 0
    for c in checks:
        if c.skipped:
            tag = "SKIP"
        elif c.ok:
            tag = "PASS"
        else:
            tag = "FAIL"
            failed += 1
        print(f"{tag}  {c.name}" + (f"  — {c.detail}" if c.detail else ""))
    print()
    if failed:
        print(f"❌ {failed} check(s) failed")
        return 1
    print("✅ all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

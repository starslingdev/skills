#!/usr/bin/env python3
"""Assemble per-skill changelog fragments into each skill's CHANGELOG.md.

Every pull request used to prepend its bullet at the same few lines of a
shared CHANGELOG.md, so any two open PRs conflicted there. Instead, a change
now adds ONE new file under ``skills/<skill>/changelog.d/``:

    skills/<skill>/changelog.d/<slug>.<kind>.md

``<slug>`` is anything unique (lowercase letters, digits, ``-`` and ``_``; a
short description or the PR number works), ``<kind>`` is one of ``added``,
``changed``, ``fixed`` or ``removed``, and the file holds the exact markdown
bullet(s) that would have gone into CHANGELOG.md. Each bullet opens with its
UTC date in bold (``- **YYYY-MM-DD**``) and cites its pull request as
``(#N)`` somewhere in its text. Continuation lines are indented.

A maintainer runs this script with no arguments at release time (or whenever
CHANGELOG.md should catch up). It files every fragment under ``## [Unreleased]``
of its skill's CHANGELOG.md, newest first, and deletes the fragment, in a
dedicated PR whose diff is nothing but that.

    python3 maintainers/scripts/assemble_changelog.py              # assemble all skills
    python3 maintainers/scripts/assemble_changelog.py --skill sling
    python3 maintainers/scripts/assemble_changelog.py --check      # validate, write nothing
    python3 maintainers/scripts/assemble_changelog.py --check --diff-base origin/main

``--diff-base REV`` (CI passes ``HEAD^1``) also refuses a hand edit of any
skill's CHANGELOG.md relative to the merge base of REV and HEAD. The
assembler's own release changes are recognized by content, not by a commit
marker: a CHANGELOG.md change passes only when the same diff deletes
fragments and the new CHANGELOG.md is byte-for-byte what this script produces
from the base CHANGELOG.md plus exactly those deleted fragments.

This file lives outside ``skills/`` on purpose: the ``skills`` CLI installs
``skills/<name>/`` recursively, and the assembler is maintainer tooling.
Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]
SKILLS = ("ci-speedup", "ci-score", "ci-secure", "sling")
KINDS = ("added", "changed", "fixed", "removed")
FRAGMENT_DIR = "changelog.d"
README = "README.md"
UNRELEASED = "## [Unreleased]"

NAME_RE = re.compile(r"^(?P<slug>[a-z0-9][a-z0-9_-]*)\.(?P<kind>[a-z]+)\.md$")
BULLET_START_RE = re.compile(r"^- ")
DATED_BULLET_RE = re.compile(r"^- \*\*(?P<date>\d{4}-\d{2}-\d{2})\*\*")
PR_REF_RE = re.compile(r"\(#\d+\)")

# A CHANGELOG whose `## [Unreleased]` section opens with a bare bullet (no
# `### Kind` heading above it) names each entry's kind inline instead, as
# ci-score's does: `- **2026-11-15** (#357) — **Changed**: ...`. Fragments for
# such a skill are filed bare at the top of the section, and must carry that
# inline `**Kind**` themselves.
MODE_HEADINGS = "headings"
MODE_FLAT = "flat"


class AssembleError(Exception):
    """The CHANGELOG cannot take fragments (no `## [Unreleased]` section)."""


@dataclass(frozen=True)
class Fragment:
    name: str
    kind: str
    bullets: Tuple[str, ...]
    date: str  # the newest date among its bullets

    @property
    def heading(self) -> str:
        return f"### {self.kind.capitalize()}"


def _lines(text: str) -> List[str]:
    return [ln.rstrip() for ln in text.splitlines()]


def changelog_mode(changelog: str) -> str:
    """`flat` when `## [Unreleased]` opens with a bullet, else `headings`."""
    lines = _lines(changelog)
    u = _unreleased_index(lines)
    for ln in lines[u + 1:]:
        if not ln.strip():
            continue
        return MODE_FLAT if BULLET_START_RE.match(ln) else MODE_HEADINGS
    return MODE_HEADINGS


def _unreleased_index(lines: Sequence[str]) -> int:
    for i, ln in enumerate(lines):
        if ln == UNRELEASED:
            return i
    raise AssembleError(f"no `{UNRELEASED}` heading to file fragments under")


def parse_fragment(name: str, text: str, *, mode: str = MODE_HEADINGS,
                   today: Optional[_dt.date] = None) -> Tuple[Optional[Fragment], List[str]]:
    """Validate one fragment. Returns (fragment or None, errors)."""
    errors: List[str] = []
    m = NAME_RE.match(name)
    if not m:
        return None, [f"{name}: fragment names are `<slug>.<kind>.md` with a lowercase "
                      f"slug of letters, digits, `-` or `_`, and kind one of {', '.join(KINDS)}"]
    kind = m.group("kind")
    if kind not in KINDS:
        return None, [f"{name}: unknown kind `{kind}`; use one of {', '.join(KINDS)}"]

    bullets: List[List[str]] = []
    for n, ln in enumerate(_lines(text), 1):
        if not ln.strip():
            if bullets:
                bullets[-1].append("")
            continue
        if BULLET_START_RE.match(ln):
            bullets.append([ln])
        elif ln.startswith("#"):
            errors.append(f"{name}:{n}: no headings in a fragment; the assembler files it "
                          f"under `### {kind.capitalize()}` from its name")
        elif not bullets:
            errors.append(f"{name}:{n}: a fragment starts with a `- **YYYY-MM-DD**` bullet")
        elif ln[0] not in " \t":
            errors.append(f"{name}:{n}: indent continuation lines so they stay in the bullet")
        else:
            bullets[-1].append(ln)
    if not bullets and not errors:
        errors.append(f"{name}: empty fragment; it must hold at least one bullet")

    dates: List[str] = []
    for block in bullets:
        while block and not block[-1]:
            block.pop()
        body = "\n".join(block)
        dm = DATED_BULLET_RE.match(block[0])
        if not dm:
            errors.append(f"{name}: bullet `{block[0][:60]}` must open with its UTC date "
                          f"in bold: `- **YYYY-MM-DD**`")
        else:
            try:
                day = _dt.date.fromisoformat(dm.group("date"))
            except ValueError:
                errors.append(f"{name}: `{dm.group('date')}` is not a calendar date")
            else:
                if today is not None and day > today:
                    errors.append(f"{name}: {day} is in the future (today is {today} UTC)")
                dates.append(dm.group("date"))
        if not PR_REF_RE.search(body):
            errors.append(f"{name}: bullet `{block[0][:60]}` must cite its pull request as `(#N)`")
        if mode == MODE_FLAT and f"**{kind.capitalize()}**" not in body:
            errors.append(f"{name}: this skill's changelog names the kind inline; the bullet "
                          f"must contain `**{kind.capitalize()}**`")
    if errors:
        return None, errors
    return Fragment(name=name, kind=kind,
                    bullets=tuple("\n".join(b) for b in bullets), date=max(dates)), []


def _newest_first(fragments: Iterable[Fragment]) -> List[Fragment]:
    ordered = sorted(fragments, key=lambda f: f.name)
    return sorted(ordered, key=lambda f: f.date, reverse=True)


def _block(fragments: Sequence[Fragment]) -> List[str]:
    out: List[str] = []
    for f in fragments:
        for bullet in f.bullets:
            if out:
                out.append("")
            out.extend(bullet.split("\n"))
    return out


def _first_content(lines: Sequence[str], start: int) -> int:
    i = start
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def assemble(changelog: str, fragments: Iterable[Fragment]) -> str:
    """Return `changelog` with every fragment filed under `## [Unreleased]`.

    Headings mode: one new `### Kind` block per kind at the top of the
    section, in Keep a Changelog order (Added, Changed, Fixed, Removed),
    except that the kind matching the heading the section already opens with
    goes last and is merged into that heading instead of repeating it.
    Flat mode: the bullets go bare at the top of the section. Either way the
    newest date comes first. Pure: the same inputs always give the same text.
    """
    fragments = list(fragments)
    if not fragments:
        return changelog
    mode = changelog_mode(changelog)
    lines = _lines(changelog)
    trailing_newline = changelog.endswith("\n")
    if mode == MODE_FLAT:
        u = _unreleased_index(lines)
        rest = _first_content(lines, u + 1)
        lines = lines[:u + 1] + [""] + _block(_newest_first(fragments)) + [""] + lines[rest:]
    else:
        u = _unreleased_index(lines)
        first = _first_content(lines, u + 1)
        opening = lines[first] if first < len(lines) and lines[first].startswith("### ") else None
        present = [k for k in KINDS if any(f.kind == k for f in fragments)]
        merge = next((k for k in present if f"### {k.capitalize()}" == opening), None)
        new: List[str] = []
        for kind in present:
            if kind == merge:
                continue
            new += [f"### {kind.capitalize()}", ""]
            new += _block(_newest_first(f for f in fragments if f.kind == kind)) + [""]
        if merge:
            new += [opening, ""]
            new += _block(_newest_first(f for f in fragments if f.kind == merge)) + [""]
            tail = lines[_first_content(lines, first + 1):]
        else:
            tail = lines[first:]
        lines = lines[:u + 1] + [""] + new + tail
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def validate_dir(files: Dict[str, str], *, mode: str,
                 today: Optional[_dt.date] = None) -> Tuple[List[Fragment], List[str]]:
    """Validate a changelog.d listing ({file name: text}). README.md is required."""
    errors: List[str] = []
    fragments: List[Fragment] = []
    if README not in files:
        errors.append(f"{FRAGMENT_DIR}/{README} is missing; it keeps the directory (and "
                      f"the format notes) in place when the assembler empties it")
    for name in sorted(files):
        if name == README:
            continue
        frag, errs = parse_fragment(name, files[name], mode=mode, today=today)
        errors.extend(errs)
        if frag:
            fragments.append(frag)
    return fragments, errors


def check_transition(skill: str, base_changelog: Optional[str], head_changelog: str,
                     base_files: Dict[str, str], head_names: Iterable[str]) -> List[str]:
    """Refuse a hand edit of CHANGELOG.md between a base and a head tree.

    `base_files` is the base tree's changelog.d listing ({name: text}),
    `head_names` the file names present there at head. A CHANGELOG.md change
    is accepted only as the assembler's output for exactly the fragments it
    deletes; deleting fragments without filing them is refused too, since it
    drops their entries on the floor.
    """
    path = f"skills/{skill}/CHANGELOG.md"
    head_names = set(head_names)
    deleted = {n: t for n, t in base_files.items() if n != README and n not in head_names}
    if base_changelog is None:  # a new skill creating its changelog is not editing one
        return []
    changed = base_changelog != head_changelog
    if not changed and not deleted:
        return []
    if not deleted:
        return [f"{path} was edited by hand. Add a fragment under skills/{skill}/"
                f"{FRAGMENT_DIR}/ instead (see its {README}); CHANGELOG.md is written "
                f"only by maintainers/scripts/assemble_changelog.py."]
    if not changed:
        return [f"skills/{skill}/{FRAGMENT_DIR}/ fragment(s) {sorted(deleted)} were deleted "
                f"but {path} did not change: their entries would be lost. Run "
                f"maintainers/scripts/assemble_changelog.py instead of deleting them."]
    mode = changelog_mode(base_changelog)
    fragments: List[Fragment] = []
    errors: List[str] = []
    for name, text in sorted(deleted.items()):
        frag, errs = parse_fragment(name, text, mode=mode)
        errors.extend(f"skills/{skill}/{FRAGMENT_DIR}/{e}" for e in errs)
        if frag:
            fragments.append(frag)
    if errors:
        return errors
    expected = assemble(base_changelog, fragments)
    if expected == head_changelog:
        return []
    want, got = expected.splitlines(), head_changelog.splitlines()
    line = next((i for i, (a, b) in enumerate(zip(want, got), 1) if a != b),
                min(len(want), len(got)) + 1)
    return [f"{path} is not the assembler's output for the {len(fragments)} fragment(s) "
            f"this change deletes (first difference at line {line}). Release changes "
            f"commit maintainers/scripts/assemble_changelog.py's output unedited; any "
            f"other change adds a fragment under skills/{skill}/{FRAGMENT_DIR}/."]


# --- filesystem / git adapters ---------------------------------------------------------------

def _read_dir(root: Path, skill: str) -> Dict[str, str]:
    d = root / "skills" / skill / FRAGMENT_DIR
    if not d.is_dir():
        return {}
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(d.iterdir()) if p.is_file()}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _git_show(root: Path, rev: str, path: str) -> Optional[str]:
    proc = _git(root, "show", f"{rev}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def _base_files(root: Path, rev: str, skill: str) -> Dict[str, str]:
    prefix = f"skills/{skill}/{FRAGMENT_DIR}/"
    proc = _git(root, "ls-tree", "--name-only", rev, prefix)
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-tree {rev} {prefix} failed: {proc.stderr.strip()}")
    out: Dict[str, str] = {}
    for path in proc.stdout.splitlines():
        text = _git_show(root, rev, path)
        if text is not None:
            out[path[len(prefix):]] = text
    return out


def _resolve_base(root: Path, rev: str) -> str:
    proc = _git(root, "merge-base", rev, "HEAD")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"cannot find the merge base of {rev!r} and HEAD: "
                           f"{proc.stderr.strip() or 'no common ancestor'}")
    return proc.stdout.strip()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="validate fragments (and, with --diff-base, CHANGELOG.md edits); write nothing")
    ap.add_argument("--diff-base", metavar="REV",
                    help="with --check: refuse hand edits of CHANGELOG.md since merge-base(REV, HEAD)")
    ap.add_argument("--skill", action="append", choices=SKILLS,
                    help="limit to one skill (repeatable); default: every shipped skill")
    ap.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.diff_base and not args.check:
        ap.error("--diff-base is a --check option")
    root: Path = args.root
    skills = args.skill or list(SKILLS)
    today = _dt.datetime.now(_dt.timezone.utc).date()

    errors: List[str] = []
    plan: List[Tuple[str, Path, str, List[Fragment]]] = []
    for skill in skills:
        changelog_path = root / "skills" / skill / "CHANGELOG.md"
        changelog = changelog_path.read_text(encoding="utf-8")
        try:
            mode = changelog_mode(changelog)
        except AssembleError as exc:
            errors.append(f"skills/{skill}/CHANGELOG.md: {exc}")
            continue
        files = _read_dir(root, skill)
        fragments, errs = validate_dir(files, mode=mode, today=today)
        errors.extend(f"skills/{skill}/{FRAGMENT_DIR}/{e}" for e in errs)
        plan.append((skill, changelog_path, changelog, fragments))

    if args.diff_base:
        try:
            base = _resolve_base(root, args.diff_base)
            for skill in skills:
                rel = f"skills/{skill}/CHANGELOG.md"
                head = (root / rel).read_text(encoding="utf-8")
                errors.extend(check_transition(
                    skill, _git_show(root, base, rel), head,
                    _base_files(root, base, skill), _read_dir(root, skill)))
        except RuntimeError as exc:
            print(f"::error::changelog check could not run: {exc}", file=sys.stderr)
            return 2

    if errors:
        for e in errors:
            print(f"::error::{e}", file=sys.stderr)
        print(f"{len(errors)} changelog problem(s); nothing was written.", file=sys.stderr)
        return 1

    if args.check:
        count = sum(len(p[3]) for p in plan)
        print(f"changelog fragments OK: {count} pending across {len(plan)} skill(s)")
        return 0

    for skill, changelog_path, changelog, fragments in plan:
        if not fragments:
            continue
        changelog_path.write_text(assemble(changelog, fragments), encoding="utf-8")
        for f in fragments:
            (root / "skills" / skill / FRAGMENT_DIR / f.name).unlink()
        print(f"{skill}: filed {len(fragments)} fragment(s) under {UNRELEASED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

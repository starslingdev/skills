#!/usr/bin/env python3
"""Reject cross-repository references before writing to a public repository.

The checker intentionally reports only a source label and line number. Echoing
the matched identifier into logs would repeat the disclosure it is preventing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_NAME = r"[A-Za-z0-9_.-]+"
_REPO = rf"{_NAME}/{_NAME}"
# Repository names are matched greedily and then canonicalized, rather than
# bounded by a lookahead: a lookahead list of "allowed next characters" misses
# whatever ordinary prose puts after a link (space, `)`, `,`, backtick, `>`,
# `]`, quotes). Covers https://, scheme-less, www., and SSH `git@github.com:`.
# The negative lookbehind keeps subdomains such as docs.github.com and
# gist.github.com out: they do not name repositories.
_GITHUB_URL = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])(?:https?://)?(?:www\.)?github\.com[/:]"
    rf"(?P<repo>{_REPO})"
)
_GITHUB_API_URL = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])(?:https?://)?api\.github\.com/repos/(?P<repo>{_REPO})"
)
_NUMBERED_REFERENCE = re.compile(
    rf"(?i)(?<![A-Za-z0-9_.-])(?P<repo>{_REPO})[#!](?:pull/|issues/)?\d+\b"
)
_TARGET = re.compile(rf"^{_REPO}$")
# First path segments on github.com that are site pages, not repository
# owners: a two-segment URL under them (github.com/settings/tokens) names no
# repository. Kept to the reserved names that appear in docs and PR text.
_NON_REPO_OWNERS = frozenset({
    "apps", "features", "login", "marketplace", "orgs", "organizations", "settings",
    "sponsors", "topics",
})
# Prose punctuation that can trail a link without being part of the name.
_TRAILING_PUNCTUATION = ".,;:!?)"


@dataclass(frozen=True)
class Finding:
    source: str
    line: int


def _canonical(value: str) -> str:
    """Normalize a matched owner/repo so equal repositories compare equal.

    Strips a trailing slash, prose punctuation, and a `.git` clone suffix,
    so `owner/repo.`, `owner/repo.git`, and `owner/repo` are one repository.
    """
    key = value.casefold()
    while True:
        stripped = key.rstrip("/").rstrip(_TRAILING_PUNCTUATION)
        if stripped.endswith(".git"):
            stripped = stripped[: -len(".git")]
        if stripped == key:
            return key
        key = stripped


def scan_text(
    text: str, *, source: str, target: str, allowed: frozenset[str] = frozenset()
) -> list[Finding]:
    """Return locations of GitHub repository references outside the allowed set.

    The allowed set is the target plus any explicitly approved `allowed`
    repositories (already canonical).
    """
    findings: list[Finding] = []
    allowed_keys = {_canonical(target), *allowed}
    owner = re.escape(target.split("/", 1)[0])
    same_owner = re.compile(rf"(?i)(?<![A-Za-z0-9_.-])(?P<repo>{owner}/{_NAME})")
    patterns = (_GITHUB_URL, _GITHUB_API_URL, _NUMBERED_REFERENCE, same_owner)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if any(
            not (pattern is _GITHUB_URL
                 and match.group("repo").split("/", 1)[0].casefold() in _NON_REPO_OWNERS)
            and _canonical(match.group("repo")) not in allowed_keys
            for pattern in patterns
            for match in pattern.finditer(line)
        ):
            findings.append(Finding(source, line_number))
    return findings


# Added-diff lines under these paths are test fixtures and eval cases: they
# routinely carry synthetic `owner/repo` slugs and upstream documentation
# links that name no private context. They are skipped by the DIFF scan only;
# PR text and commit messages stay strict everywhere.
_FIXTURE_PATH = re.compile(
    r"^(?:tests/|skills/[^/]+/tests/|skills/[^/]+/evals/|maintainers/[^/]+/tests/)"
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a tool; a missing binary is a fail-closed error, not a read error."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise RuntimeError(f"{cmd[0]} not installed") from None


def _git_output(args: list[str]) -> str:
    result = _run(["git", "-c", "core.quotePath=false", *args])
    if result.returncode:
        # Only the subcommand: arguments can carry refs or paths.
        raise RuntimeError(f"git {args[0]} failed")
    return result.stdout


def _verify_public_target(target: str, label: str = "destination repository") -> None:
    result = _run(["gh", "repo", "view", target, "--json", "visibility", "--jq", ".visibility"])
    if result.returncode:
        raise RuntimeError(f"could not verify {label} visibility")
    if result.stdout.strip().upper() != "PUBLIC":
        raise RuntimeError(f"{label} is not verified PUBLIC")


def _header_path(line: str) -> str | None:
    """Return the new-file path from a `+++ ` header, or None for /dev/null."""
    value = line[len("+++ "):].rstrip("\t")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if value == "/dev/null":
        return None
    return value[2:] if value.startswith("b/") else value


def _added_lines(diff: str) -> list[tuple[str | None, int, str]]:
    """Return (path, new-file line, text) for every added line in a patch.

    A `+++ ` line is a file header only in header position (after `diff --git`
    and before the first hunk). Inside a hunk it is added content that begins
    with `++`, and is scanned like any other added line.
    """
    added: list[tuple[str | None, int, str]] = []
    path: str | None = None
    in_header = False
    line_number = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header, path = True, None
            continue
        if in_header:
            if line.startswith("+++ "):
                path = _header_path(line)
                continue
            hunk = _HUNK.match(line)
            if hunk:
                in_header = False
                line_number = int(hunk.group("start"))
            continue
        hunk = _HUNK.match(line)
        if hunk:
            line_number = int(hunk.group("start"))
        elif line.startswith("+"):
            added.append((path, line_number, line[1:]))
            line_number += 1
        elif line.startswith(" "):
            line_number += 1
    return added


def _scan_diff(diff: str, *, label: str, target: str, allowed: frozenset[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path, line_number, text in _added_lines(diff):
        if path is not None and _FIXTURE_PATH.match(path):
            continue
        if scan_text(text, source="", target=target, allowed=allowed):
            findings.append(Finding(f"{label}{path or '?'}", line_number))
    return findings


_DIFF_FLAGS = ["--unified=0", "--no-color", "--no-ext-diff", "--no-renames"]


def _scan_history(base: str, *, target: str, allowed: frozenset[str]) -> list[Finding]:
    """Scan every outgoing commit separately, then the net diff.

    Pushing a branch publishes every commit on it, so a reference added in
    one commit and removed in a later one is still published.
    """
    findings: list[Finding] = []
    commits = _git_output(["rev-list", "--reverse", "--parents", f"{base}..HEAD"])
    for sha, *parents in (entry.split() for entry in commits.splitlines()):
        short = _git_output(["rev-parse", "--short", sha]).strip()
        message = _git_output(["log", "-1", "--format=%B", sha])
        findings += scan_text(
            message, source=f"commit {short} message", target=target, allowed=allowed
        )
        if len(parents) > 1:
            continue  # merge: its content is covered by its parents and the net diff
        patch = _git_output(["show", "--format=", *_DIFF_FLAGS, sha, "--"])
        findings += _scan_diff(patch, label=f"commit {short} ", target=target, allowed=allowed)
    net = _git_output(["diff", *_DIFF_FLAGS, f"{base}...HEAD", "--"])
    findings.extend(_scan_diff(net, label="added diff:", target=target, allowed=allowed))
    return findings


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="public destination owner/repo")
    parser.add_argument(
        "--text-file", action="append", default=[], type=Path,
        help="exact outbound text to scan; repeat for title, body, or readback",
    )
    parser.add_argument(
        "--base", help="base ref: scans each outgoing commit's message and added lines, "
        "and the net added diff",
    )
    parser.add_argument(
        "--allow", action="append", default=[], metavar="OWNER/REPO",
        help="explicitly approved cross-repository reference; must verify PUBLIC",
    )
    return parser.parse_args(argv)


def _report(findings: list[Finding]) -> int:
    if findings:
        lines = [f"  {finding.source}:{finding.line}" for finding in findings]
        print("privacy preflight blocked: cross-repository GitHub reference(s) found",
              *lines, "blocked identifiers are intentionally not echoed",
              sep="\n", file=sys.stderr)
        return 1
    print("privacy preflight passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not _TARGET.fullmatch(args.target):
        print("privacy preflight error: --target must be owner/repo", file=sys.stderr)
        return 2
    for index, repo in enumerate(args.allow, start=1):
        if not _TARGET.fullmatch(repo):
            print(f"privacy preflight error: --allow {index} must be owner/repo", file=sys.stderr)
            return 2
    if not args.text_file and not args.base:
        print("privacy preflight error: provide --text-file or --base", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    try:
        _verify_public_target(args.target)
        for index, repo in enumerate(args.allow, start=1):
            _verify_public_target(repo, label=f"allowed repository {index}")
        allowed = frozenset(_canonical(repo) for repo in args.allow)
        for index, path in enumerate(args.text_file, start=1):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                # The exception text carries the path; never print it.
                raise RuntimeError(f"could not read text file {index}") from None
            findings += scan_text(
                text, source=f"text file {index}", target=args.target, allowed=allowed
            )
        if args.base:
            findings += _scan_history(args.base, target=args.target, allowed=allowed)
    except RuntimeError as exc:
        print(f"privacy preflight error: {exc}", file=sys.stderr)
        return 2

    return _report(findings)


if __name__ == "__main__":
    raise SystemExit(main())

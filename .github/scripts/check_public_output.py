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
    rf"(?<![A-Za-z0-9_.-])(?P<repo>{_REPO})[#!](?:pull/|issues/)?\d+\b",
    re.IGNORECASE,
)
_TARGET = re.compile(rf"^{_REPO}$")
# First path segments on github.com that are site pages, not repository
# owners: a two-segment URL under them (github.com/settings/tokens) names no
# repository. Kept to the reserved names that appear in docs and PR text.
_NON_REPO_OWNERS = frozenset(
    {
        "apps", "features", "login", "marketplace", "orgs", "organizations",
        "settings", "sponsors", "topics",
    }
)
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


def _is_repository(pattern: re.Pattern[str], repo: str) -> bool:
    owner = repo.split("/", 1)[0].casefold()
    return not (pattern is _GITHUB_URL and owner in _NON_REPO_OWNERS)


def scan_text(
    text: str, *, source: str, target: str, allowed: frozenset[str] = frozenset()
) -> list[Finding]:
    """Return locations of GitHub repository references outside the allowed set.

    The allowed set is the target plus any explicitly approved `allowed`
    repositories (already canonical).
    """
    findings: list[Finding] = []
    allowed_keys = {_canonical(target), *allowed}
    owner = target.split("/", 1)[0]
    same_owner_reference = re.compile(
        rf"(?i)(?<![A-Za-z0-9_.-])(?P<repo>{re.escape(owner)}/{_NAME})"
    )
    patterns = (
        _GITHUB_URL, _GITHUB_API_URL, _NUMBERED_REFERENCE, same_owner_reference
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        if any(
            _is_repository(pattern, match.group("repo"))
            and _canonical(match.group("repo")) not in allowed_keys
            for pattern in patterns
            for match in pattern.finditer(line)
        ):
            findings.append(Finding(source, line_number))
    return findings


def _git_output(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed")
    return result.stdout


def _verify_public_target(target: str) -> None:
    result = subprocess.run(
        [
            "gh", "repo", "view", target, "--json", "visibility",
            "--jq", ".visibility",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("could not verify destination repository visibility")
    if result.stdout.strip().upper() != "PUBLIC":
        raise RuntimeError("destination repository is not verified PUBLIC")


def _added_diff(base: str) -> str:
    """Return only added content, excluding diff metadata and removed lines."""
    diff = _git_output(["diff", "--unified=0", f"{base}...HEAD", "--"])
    return "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="public destination owner/repo")
    parser.add_argument(
        "--text-file", action="append", default=[], type=Path,
        help="exact outbound text to scan; repeat for title, body, or readback",
    )
    parser.add_argument(
        "--base", help="base ref used to scan commit messages and added diff lines"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not _TARGET.fullmatch(args.target):
        print("privacy preflight error: --target must be owner/repo", file=sys.stderr)
        return 2
    if not args.text_file and not args.base:
        print("privacy preflight error: provide --text-file or --base", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    try:
        _verify_public_target(args.target)
        for index, path in enumerate(args.text_file, start=1):
            findings.extend(
                scan_text(
                    path.read_text(encoding="utf-8"),
                    source=f"text file {index}",
                    target=args.target,
                )
            )
        if args.base:
            messages = _git_output(["log", "--format=%B", f"{args.base}..HEAD"])
            findings.extend(scan_text(messages, source="commit messages", target=args.target))
            findings.extend(scan_text(_added_diff(args.base), source="added diff", target=args.target))
    except (OSError, UnicodeError):
        print("privacy preflight error: could not read an input file", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"privacy preflight error: {exc}", file=sys.stderr)
        return 2

    if findings:
        print(
            "privacy preflight blocked: cross-repository GitHub reference(s) found",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  {finding.source}:{finding.line}", file=sys.stderr)
        print("blocked identifiers are intentionally not echoed", file=sys.stderr)
        return 1

    print("privacy preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

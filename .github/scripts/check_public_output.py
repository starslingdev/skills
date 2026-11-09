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


_REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_GITHUB_URL = re.compile(
    rf"(?i)https?://(?:www\.)?github\.com/(?P<repo>{_REPO})(?=[/#?]|$)"
)
_GITHUB_API_URL = re.compile(
    rf"(?i)https?://api\.github\.com/repos/(?P<repo>{_REPO})(?=[/#?]|$)"
)
_NUMBERED_REFERENCE = re.compile(
    rf"(?<![A-Za-z0-9_.-])(?P<repo>{_REPO})[#!](?:pull/|issues/)?\d+\b",
    re.IGNORECASE,
)
_TARGET = re.compile(rf"^{_REPO}$")


@dataclass(frozen=True)
class Finding:
    source: str
    line: int


def _canonical(value: str) -> str:
    return value.rstrip("/").casefold()


def scan_text(text: str, *, source: str, target: str) -> list[Finding]:
    """Return locations of non-target GitHub repository references."""
    findings: list[Finding] = []
    target_key = _canonical(target)
    owner = target.split("/", 1)[0]
    same_owner_reference = re.compile(
        rf"(?i)(?<![A-Za-z0-9_.-])(?P<repo>{re.escape(owner)}/[A-Za-z0-9_.-]+)"
        rf"(?=[/#?\s`'\"),.;:!]|$)"
    )
    patterns = (
        _GITHUB_URL, _GITHUB_API_URL, _NUMBERED_REFERENCE, same_owner_reference
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        if any(
            _canonical(match.group("repo")) != target_key
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

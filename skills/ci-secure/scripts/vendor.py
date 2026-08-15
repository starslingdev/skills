#!/usr/bin/env python3
"""Copy ci-secure into a repository, and check later that it is still what was copied.

Installing ci-secure as a CI gate VENDORS it: the engine, the gate and the
licence are copied into the adopter's repository and reviewed in a pull request
like any other code. Nothing is fetched and executed at CI time — a pin can be
moved or deleted in a repository the adopter does not control, and a job that
fetches and runs code is one upstream compromise away from running whatever
arrives. We ship a detector for that shape; we should not be the ones asking
people to run it.

What that buys, and what it costs: the adopter can read every line that will
judge their pull requests, and it never changes underneath them. In exchange
the copy goes stale, so this script also writes VENDORED.json — the skill
version, the source commit, and a hash per file — which turns "is our copy
current, and has anyone edited it?" into a command instead of a visual diff.

    vendor.py --into <repo>     copy in (or refresh) and write the manifest
    vendor.py --verify <dir>    recompute the hashes and compare

What the manifest is NOT is a tamper seal. Anyone who can edit the vendored
gate can edit the manifest in the same commit, and no amount of hashing changes
that. It catches the accident — a local edit made to debug something, still
sitting there six months later, quietly weakening a check nobody re-read — and
the accident is the case that actually happens.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SKILL = _HERE.parent

MANIFEST_NAME = "VENDORED.json"
VENDOR_DIRNAME = "ci-secure"

# The engine's own layout is preserved rather than flattened: scan.py finds its
# pattern catalog at `<root>/references/security-patterns.md`, relative to its
# own directory, so `scripts/` and `references/` have to stay siblings.
#
# Only what the GATE needs. report.py, run.py and record_timing.py belong to the
# interactive skill — a human reading a rendered report — and shipping them
# would enlarge the adopter's supply-chain surface for nothing.
VENDORED_FILES = (
    "scripts/scan.py",
    "scripts/config.py",
    "scripts/config_facts.py",
    "scripts/gh_utils.py",
    "references/security-patterns.md",
    # Travels with the copy so the adopter's own CI can run `--verify` against
    # the manifest. The vendored copy can check itself; it cannot install, since
    # the scaffold and the rest of the skill are not there.
    "scripts/vendor.py",
)

# The gate travels beside the engine, so `config.py` resolves next to the
# resolved engine exactly as it does in this repository.
#
# It is copied from `scaffold/`, not from `.github/scripts/`, because an
# INSTALLED skill is just this directory — there is no `.github/` to read. That
# makes `scaffold/gate.py` a second copy of a security-critical file, which is
# a real cost, so the repository holds the two byte-identical and fails its own
# build if they ever diverge. A vendored gate weaker than the one we run on
# ourselves would be the worst possible thing to ship.
GATE_SOURCE = "scaffold/gate.py"
GATE_DEST = "scripts/gate.py"

# The workflow the adopter gets. One hosted scan job and one always-running
# verdict job; see the file itself for why the verdict job cannot be a copy of
# ours.
WORKFLOW_SOURCE = "scaffold/ci-secure.yml"
WORKFLOW_DEST = ".github/workflows/ci-secure.yml"

LICENSE_DEST = "LICENSE"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_commit() -> str:
    """The commit this copy was taken from, or a marker that says we cannot tell.

    An installed skill has no .git, so this is often genuinely unknowable. It
    says so rather than inventing a value: "unknown" is a worse answer than a
    sha and a much better one than a plausible wrong sha.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(_SKILL), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown (no git available)"
    if out.returncode != 0:
        return "unknown (not a git checkout)"
    sha = out.stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(_SKILL), "status", "--porcelain"],
        capture_output=True, text=True).stdout.strip()
    return f"{sha}-dirty" if dirty else sha


def skill_version() -> str:
    sys.path.insert(0, str(_HERE))
    try:
        import config  # noqa: PLC0415
        return str(config.__version__)
    finally:
        sys.path.pop(0)


def _license_source() -> Path | None:
    for candidate in (_SKILL / "LICENSE", _SKILL.parent.parent / "LICENSE"):
        if candidate.is_file():
            return candidate
    return None


def install(repo: Path) -> Path:
    """Copy the engine, the gate and the licence into `repo/ci-secure/`."""
    if not (_SKILL / GATE_SOURCE).is_file():
        raise SystemExit(
            "this looks like a VENDORED copy of ci-secure, which can verify "
            "itself but cannot install: the scaffold and the rest of the skill "
            "are not here. Run --into from an installed ci-secure skill, or ask "
            "ci-secure to refresh this copy.")
    dest = repo / VENDOR_DIRNAME
    for rel in VENDORED_FILES:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_SKILL / rel, target)

    gate = _SKILL / GATE_SOURCE
    if not gate.is_file():                       # pragma: no cover - defensive
        raise SystemExit(f"gate not found at {gate}")
    shutil.copy2(gate, dest / GATE_DEST)

    workflow = repo / WORKFLOW_DEST
    workflow.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_SKILL / WORKFLOW_SOURCE, workflow)

    # The licence travels with the code or the copy is a licence violation.
    licence = _license_source()
    if licence is None:                          # pragma: no cover - defensive
        raise SystemExit(
            "no LICENSE found to vendor; copying the code without it would be a "
            "licence violation, so this refuses rather than shipping one")
    shutil.copy2(licence, dest / LICENSE_DEST)

    write_manifest(dest)
    return dest


def vendored_paths(dest: Path) -> list[str]:
    return sorted([*VENDORED_FILES, GATE_DEST, LICENSE_DEST])


def write_manifest(dest: Path) -> Path:
    manifest = {
        "skill": "ci-secure",
        "skill_version": skill_version(),
        "source_commit": source_commit(),
        "source_repo": "https://github.com/starslingdev/skills",
        "files": {rel: sha256(dest / rel) for rel in vendored_paths(dest)},
        "note": (
            "Written by ci-secure's install step. `vendor.py --verify` "
            "recomputes these hashes. To update the vendored copy, ask "
            "ci-secure to refresh it; hand edits show up as that pull "
            "request's diff, for you to resolve — they are never silently "
            "overwritten outside a pull request."
        ),
    }
    path = dest / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def verify(dest: Path) -> int:
    """Compare the vendored files against the manifest. 0 if they agree."""
    manifest_path = dest / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"::error::{MANIFEST_NAME} is missing from {dest} - cannot tell "
              "what this copy of ci-secure was supposed to be")
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded = manifest["files"]
    except (ValueError, KeyError) as exc:
        print(f"::error::{MANIFEST_NAME} is unreadable ({exc!r})")
        return 1

    drift = []
    for rel, expected in sorted(recorded.items()):
        path = dest / rel
        if not path.is_file():
            drift.append(f"{rel}: missing")
        elif sha256(path) != expected:
            drift.append(f"{rel}: modified")
    for path in dest.rglob("*"):
        rel = path.relative_to(dest).as_posix()
        if path.is_file() and rel != MANIFEST_NAME and rel not in recorded:
            drift.append(f"{rel}: not in the manifest")

    if not drift:
        print(f"ci-secure vendored copy matches {MANIFEST_NAME} "
              f"(skill v{manifest.get('skill_version', '?')}, "
              f"from {manifest.get('source_commit', '?')})")
        return 0

    for item in drift:
        print(f"::error::vendored ci-secure has drifted - {item}")
    print("::error::the vendored gate is no longer the code that was reviewed. "
          "Ask ci-secure to refresh the vendored copy, which opens a pull "
          "request with the differences, or revert the local edit.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--into", metavar="REPO",
                       help="repository root to vendor ci-secure into")
    group.add_argument("--verify", metavar="DIR",
                       help="vendored ci-secure directory to check")
    args = parser.parse_args(argv)

    if args.verify:
        return verify(Path(args.verify).resolve())

    dest = install(Path(args.into).resolve())
    print(f"vendored ci-secure into {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

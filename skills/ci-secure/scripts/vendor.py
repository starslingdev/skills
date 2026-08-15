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


def _previously_vendored(dest: Path) -> list[str]:
    """What the last install put here, according to its own manifest.

    A refresh needs this so it can REMOVE what this version no longer vendors.
    A dropped file is not inert: `--verify` reds on anything under the vendored
    directory the manifest does not list, so leaving it behind would hand the
    adopter a failing required check for a file they never touched, on a
    refresh that was otherwise correct.
    """
    path = dest / MANIFEST_NAME
    if not path.is_file():
        return []
    try:
        return sorted(json.loads(path.read_text(encoding="utf-8"))["files"])
    except (ValueError, KeyError, TypeError):
        return []


def _refuse_destinations_outside(repo: Path, dest: Path) -> None:
    """Refuse, before writing anything, if any destination leaves the repository.

    A symlink is ordinary repository content: it survives a clone, a pull
    request checkout and a fork, and it costs an attacker one committed file.
    One at `ci-secure/`, at any directory beneath it, or at `.github/` is
    followed by `mkdir` and `copy2` exactly as a real directory would be, so
    without this the engine, the gate and a live workflow land somewhere the
    adopter never looked while this prints success — and the repository it was
    aimed at ends up with no gate at all.

    Every destination is checked, not just the vendored root, because the
    escape can sit at any component of any path, or at a single file. Resolving
    each one and requiring it to stay under the resolved repository covers all
    of those in one test, including `..` inside a path and a symlink loop.
    """
    root = repo.resolve()
    targets = [dest, dest / MANIFEST_NAME, dest / GATE_DEST, dest / LICENSE_DEST,
               repo / WORKFLOW_DEST, (repo / WORKFLOW_DEST).parent]
    targets += [dest / rel for rel in VENDORED_FILES]
    # Directories this will `mkdir` are destinations too, and naming them keeps
    # the refusal pointed at the one symlink to remove rather than at the six
    # files under it.
    targets += [parent for target in list(targets)
                for parent in target.parents if repo in parent.parents]

    escaped = []
    for target in targets:
        try:
            resolved = target.resolve()
        except OSError:                          # symlink loop, unreadable path
            escaped.append(target)
            continue
        if not resolved.is_relative_to(root):
            escaped.append(target)

    if escaped:
        # One symlink at `ci-secure/` escapes every destination beneath it.
        # Naming all ten obscures the single thing the adopter has to fix, so
        # only the shallowest offender on each branch is reported.
        shallowest = [path for path in escaped
                      if not any(other != path and other in path.parents
                                 for other in escaped)]
        listed = "\n  ".join(
            f"{path.relative_to(repo)} -> outside the repository"
            for path in sorted(set(shallowest)))
        raise SystemExit(
            "refusing to install: these destinations resolve outside "
            f"{root}, so installing would write the gate somewhere this "
            "repository's pull requests would never see it:\n  "
            f"{listed}\n"
            "A symlink on one of those paths is almost certainly not what you "
            "want under a security gate. Remove it, then run this again.")


def install(repo: Path) -> Path:
    """Copy the engine, the gate and the licence into `repo/ci-secure/`.

    Everything that can refuse refuses BEFORE anything is written. A partial
    install is not a tidy failure: it is a live workflow in the adopter's
    repository beside a vendored tree with no manifest, whose first CI run reds
    with "cannot tell what this copy was supposed to be".
    """
    gate = _SKILL / GATE_SOURCE
    workflow_source = _SKILL / WORKFLOW_SOURCE
    if not gate.is_file() or not workflow_source.is_file():
        raise SystemExit(
            "this looks like a VENDORED copy of ci-secure, which can verify "
            "itself but cannot install: the scaffold and the rest of the skill "
            "are not here. Run --into from an installed ci-secure skill, or ask "
            "ci-secure to refresh this copy.")

    missing = [rel for rel in VENDORED_FILES if not (_SKILL / rel).is_file()]
    if missing:                                  # pragma: no cover - defensive
        raise SystemExit(f"ci-secure is incomplete, cannot vendor: {missing}")

    # The licence travels with the code or the copy is a licence violation.
    licence = _license_source()
    if licence is None:                          # pragma: no cover - defensive
        raise SystemExit(
            "no LICENSE found to vendor; copying the code without it would be a "
            "licence violation, so this refuses rather than shipping one")

    dest = repo / VENDOR_DIRNAME
    _refuse_destinations_outside(repo, dest)

    first_install = not (dest / MANIFEST_NAME).is_file()
    stale = set(_previously_vendored(dest)) - set(vendored_paths())

    for rel in VENDORED_FILES:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_SKILL / rel, target)
    shutil.copy2(gate, dest / GATE_DEST)
    shutil.copy2(licence, dest / LICENSE_DEST)

    # The manifest is repository content - it arrives with a branch, a pull
    # request checkout or a fork clone - and this loop DELETES. A `files` entry
    # of `../.github/workflows/release.yml` would otherwise be repo-controlled
    # file deletion aimed at the very directory this tool exists to protect, and
    # a merely corrupt manifest would do the same damage by accident. Only paths
    # that stay inside the vendored directory are ever touched.
    resolved_dest = dest.resolve()
    for rel in sorted(stale):
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts:
            print(f"::warning::ignoring manifest entry {rel!r}, which points "
                  "outside the vendored directory")
            continue
        old = dest / rel
        try:
            inside = old.resolve().is_relative_to(resolved_dest)
        except OSError:                          # pragma: no cover - defensive
            inside = False
        if not inside:
            print(f"::warning::ignoring manifest entry {rel!r}, which resolves "
                  "outside the vendored directory")
            continue
        if old.is_file():
            old.unlink()
            print(f"removed {rel}, which this version no longer vendors")

    # The workflow belongs to the adopter. They are invited to change the
    # runner and the triggers, and — the entire point of the ramp — to delete
    # `--advisory` once the first run's findings are burned down. Copying the
    # template back over it on every refresh would quietly return a blocking
    # gate to advisory, and because the workflow is deliberately not
    # checksummed, nothing downstream would catch it. So it is written once and
    # never rewritten.
    # The manifest is written BEFORE the workflow. It is the last thing that
    # can fail - it hashes every file and reads the skill version - and a
    # workflow already on disk when it fails is a live check beside a tree with
    # no manifest, whose first run reds with "cannot tell what this copy was
    # supposed to be".
    write_manifest(dest)

    workflow = repo / WORKFLOW_DEST
    if workflow.exists():
        if first_install:
            print(f"::warning::{WORKFLOW_DEST} already existed and was NOT "
                  "replaced, so nothing here runs the gate yet. Compare it "
                  f"against {WORKFLOW_SOURCE} in the skill and merge what you "
                  "need, or move it aside and run this again.")
        else:
            print(f"left the workflow at {WORKFLOW_DEST} exactly as it is - it "
                  f"is yours to tune. To take up template changes, diff it "
                  f"against {WORKFLOW_SOURCE} in the skill and apply what you "
                  "want.")
    else:
        workflow.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workflow_source, workflow)
        print(f"wrote the workflow to {WORKFLOW_DEST}")

    return dest


def vendored_paths() -> list[str]:
    return sorted([*VENDORED_FILES, GATE_DEST, LICENSE_DEST])


def write_manifest(dest: Path) -> Path:
    manifest = {
        "skill": "ci-secure",
        "skill_version": skill_version(),
        "source_commit": source_commit(),
        "source_repo": "https://github.com/starslingdev/skills",
        "files": {rel: sha256(dest / rel) for rel in vendored_paths()},
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
        if not path.is_file() or rel == MANIFEST_NAME or rel in recorded:
            continue
        # Bytecode gets its OWN message, not an exemption. A `.pyc` written
        # with an unchecked hash is never validated against its source: Python
        # loads it as-is, which is how the gate loads `config.py`. So one
        # planted here can empty the set of outcomes that block while every
        # source file still hashes correctly - a green check over a repository
        # with failing facts, and nothing else would ever see it. The innocent
        # cause is a local run, which the shipped workflow prevents outright by
        # telling Python not to write bytecode at all.
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            drift.append(
                f"{rel}: compiled bytecode, which can override the source file "
                "verified above - delete ci-secure/**/__pycache__, and do not "
                "commit it")
            continue
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

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
version, the source commit, and a hash per file — which turns "has anyone
edited our copy?" into a command instead of a visual diff. It does NOT
answer "is our copy current": `--verify` compares the copy against its own
manifest, never against this skill, so the recorded version is the only
staleness signal and reading it is a human step.

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
import os
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


def esc(value: object) -> str:
    """Neutralise workflow commands in anything read out of the repository.

    `VENDORED.json` arrives with a branch, a pull request checkout or a fork
    clone, and every string in it is printed into a step's log - which is a
    command sink, not a text field. A newline in the recorded version forges a
    `::notice::all clear` on the check run; a newline in a `files` key emits
    `::stop-commands::`, which swallows every drift reason printed after it and
    turns a stated red into an unexplained one. The gate escapes engine output
    for exactly this reason; `--verify` reads from the same trust class and
    runs in the step above it.
    """
    return (str(value).replace("%", "%25")
            .replace("\r", "%0D").replace("\n", "%0A"))


def _git(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run git with the ambient GIT_* variables stripped.

    `GIT_DIR` OVERRIDES `-C`, so `-C <dir> rev-parse` is not actually a question
    about `<dir>` when one is exported - by a hook, a `rebase -x`, or a
    worktree-driven session. Left alone it makes the subdirectory guard read an
    unrelated repository's toplevel and refuse a correct install, telling the
    adopter to vendor a live workflow into that other repository instead: the
    "written somewhere you never looked" outcome the guards exist to prevent,
    arrived at THROUGH a guard. It also makes `source_commit` stamp the
    manifest with a stranger's HEAD - the plausible wrong sha this file says is
    worse than saying nothing.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def source_commit() -> str:
    """The commit this copy was taken from, or a marker that says we cannot tell.

    An installed skill has no .git, so this is often genuinely unknowable. It
    says so rather than inventing a value: "unknown" is a worse answer than a
    sha and a much better one than a plausible wrong sha.
    """
    try:
        out = _git("-C", str(_SKILL), "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return "unknown (no git available)"
    if out.returncode != 0:
        return "unknown (not a git checkout)"
    sha = out.stdout.strip()
    # The dirty test gets the same timeout and the same return-code check as
    # the call above it. Unguarded, ANY failure of `git status` - a held index
    # lock, EACCES, a wedged process - yields empty stdout, which reads as
    # "clean" and stamps the manifest with a bare sha: an assertion that this
    # copy is byte-for-byte the published commit, over a copy that is not. That
    # is the plausible wrong sha, manufactured by the one path that was not
    # checked.
    try:
        status = _git("-C", str(_SKILL), "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return f"{sha} (working tree state unknown)"
    if status.returncode != 0:
        return f"{sha} (working tree state unknown)"
    return f"{sha}-dirty" if status.stdout.strip() else sha


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


def _refuse_a_subdirectory(repo: Path) -> None:
    """Refuse `--into` a subdirectory of a repository rather than its root.

    `services/api/.github/workflows/ci-secure.yml` is a file GitHub never
    reads. The install otherwise prints exactly what a correct one prints, so
    the adopter is told they have a gate and every pull request merges
    unscanned — worse than no install, because it stops them installing again.

    Only a path that IS inside a git work tree but is not its root is refused.
    A directory that is not in a repository at all is left alone: vendoring
    into a tree before `git init` is a legitimate order to do things in, and
    guessing at intent there would refuse work that is fine.
    """
    try:
        out = _git("-C", str(repo), "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):     # no git to ask
        return
    if out.returncode != 0:                           # not in a repository
        return

    root = Path(out.stdout.strip())
    if not root.name or root.resolve() == repo.resolve():
        return
    raise SystemExit(
        f"refusing to install: {repo} is inside the repository at {root}, not "
        "its root. A workflow written here is one GitHub never runs, and the "
        "install would look like it had worked. Run this again with "
        f"--into {root}")


def _refuse_a_destination_in_use(dest: Path) -> None:
    """Refuse a first install into a `ci-secure/` directory someone else owns.

    `ci-secure/` is a plausible name for a directory an adopter already keeps,
    and copying in beside what is there exits 0 while the manifest lists only
    our files — so their own CI reds on `--verify` with "not in the manifest",
    on the first run and every run after it, before the gate is reached.
    Neither documented remedy applies: `--advisory` downgrades failed FACTS,
    and this is not a fact. Refusing while the adopter is still looking at the
    install is the only moment this is cheap to resolve.

    Only a FIRST install is refused. A refresh is expected to find our files
    there, and drift in a copy we did install is what `--verify` is for.
    """
    if not dest.exists():
        return
    if not dest.is_dir():
        raise SystemExit(
            f"refusing to install: {dest} already exists and is not a "
            "directory, so there is nowhere to put the vendored copy. Move it "
            "aside, then run this again.")
    intruders = sorted(path.relative_to(dest).as_posix()
                       for path in dest.rglob("*") if path.is_file())
    if not intruders:
        return
    shown = ", ".join(intruders[:5])
    more = f" (and {len(intruders) - 5} more)" if len(intruders) > 5 else ""
    raise SystemExit(
        f"refusing to install: {dest} already holds files that are not "
        f"ci-secure's - {shown}{more}. The vendored copy has to be the only "
        "thing in that directory, because the workflow re-checks it against a "
        "manifest on every run and would red on anything else it finds. Move "
        "them somewhere else, then run this again.")


def _refuse_redirected_destinations(repo: Path, dest: Path) -> None:
    """Refuse, before writing anything, if any destination has been redirected.

    A symlink is ordinary repository content: it survives a clone, a pull
    request checkout and a fork, and it costs an attacker one committed file.
    One at `ci-secure/`, at any directory beneath it, at a single vendored
    file, or at `.github/` is followed by `mkdir` and `copy2` exactly as a real
    directory would be.

    Two different harms, so two tests. A destination that leaves the repository
    puts the engine, the gate and a live workflow somewhere the adopter never
    looked while this prints success, and the repository it was aimed at comes
    away with no gate. A destination redirected to somewhere else INSIDE the
    repository stays contained and is worse in a different way: a symlink at
    `ci-secure/LICENSE` pointing at `.github/workflows/release.yml` passes any
    containment test and then has the licence text written over the adopter's
    release workflow, on a refresh that reports success. Containment alone is
    not the property wanted; "this path is what it appears to be" is.

    Every destination is checked, not just the vendored root, because the
    redirect can sit at any component of any path. The directories this would
    `mkdir` are destinations too, which also keeps the refusal pointed at the
    one symlink to remove rather than at the six files under it.
    """
    root = repo.resolve()
    targets = [dest, dest / MANIFEST_NAME, dest / GATE_DEST, dest / LICENSE_DEST,
               repo / WORKFLOW_DEST, (repo / WORKFLOW_DEST).parent]
    targets += [dest / rel for rel in VENDORED_FILES]
    targets += [parent for target in list(targets)
                for parent in target.parents if repo in parent.parents]

    bad: dict[Path, str] = {}
    for target in targets:
        if target.is_symlink():
            bad.setdefault(target, "is a symlink, so writing it would write "
                                   "somewhere else")
            continue
        try:
            resolved = target.resolve()
        except OSError:                          # symlink loop, unreadable path
            bad.setdefault(target, "cannot be resolved")
            continue
        if not resolved.is_relative_to(root):
            bad.setdefault(target, "resolves outside the repository")

    if bad:
        # One symlink at `ci-secure/` redirects every destination beneath it.
        # Naming all ten obscures the single thing the adopter has to fix.
        shallowest = sorted(path for path in bad
                            if not any(other in path.parents for other in bad))
        listed = "\n  ".join(f"{path.relative_to(repo)} {bad[path]}"
                             for path in shallowest)
        raise SystemExit(
            "refusing to install: these destinations are not the plain paths "
            "they look like, so installing would write somewhere other than "
            f"where this says it writes:\n  {listed}\n"
            "A symlink on one of those paths is almost certainly not what you "
            "want under a security gate. Remove it, then run this again.")


def install(repo: Path) -> Path:
    """Copy the engine, the gate and the licence into `repo/ci-secure/`.

    Every REFUSAL happens before anything is written. That is not the same as
    "nothing can fail after the first write" — `write_manifest` runs last and
    can still raise — and the ordering below is chosen for exactly that case.
    A partial install is not a tidy failure: a live workflow in the adopter's
    repository beside a vendored tree with no manifest reds their first CI run
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
    _refuse_a_subdirectory(repo)
    _refuse_redirected_destinations(repo, dest)

    first_install = not (dest / MANIFEST_NAME).is_file()
    if first_install:
        _refuse_a_destination_in_use(dest)
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
            print(f"::warning::ignoring manifest entry {esc(rel)!r}, which points "
                  "outside the vendored directory")
            continue
        old = dest / rel
        try:
            inside = old.resolve().is_relative_to(resolved_dest)
        except OSError:                          # pragma: no cover - defensive
            inside = False
        if not inside:
            print(f"::warning::ignoring manifest entry {esc(rel)!r}, which resolves "
                  "outside the vendored directory")
            continue
        if old.is_file():
            old.unlink()
            print(f"removed {esc(rel)}, which this version no longer vendors")

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
    if not first_install:
        # A refresh writes NO workflow, whether or not one is sitting at that
        # path. "Do not overwrite what is there" was a narrower promise than
        # the one made: an adopter who RENAMED the file to fit their
        # conventions, or deleted it while backing the gate out, got the
        # advisory template silently re-added beside their blocking one -
        # reaching the same "quietly back to advisory" outcome by adding
        # rather than overwriting, with two jobs then publishing the required
        # check name.
        print("this is a refresh, so the workflow was not touched - it is "
              "yours, including where it lives. To take up template changes, "
              f"diff yours against {WORKFLOW_SOURCE} in the skill and apply "
              "what you want.")
    elif workflow.exists():
        print(f"::warning::{WORKFLOW_DEST} already existed and was NOT "
              "replaced, so nothing here runs the gate yet. Compare it "
              f"against {WORKFLOW_SOURCE} in the skill and merge what you "
              "need, or move it aside and run this again.")
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
            "Written by the ci-secure setup you ran. `vendor.py --verify` "
            "recomputes these hashes. To update the vendored copy, ask "
            "ci-secure to refresh it. A refresh OVERWRITES the files listed "
            "here, so any hand edit to them is replaced and shows up in "
            "`git diff` — review that before you commit."
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
    # `VENDORED.json` is repository content, so its SHAPE is untrusted too. A
    # `files` that is a list, a string or null used to red with a bare
    # traceback: a security check failing inside someone's pull request with no
    # stated cause, in a tool whose whole argument is that a red says what is
    # wrong and what to do about it.
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded = manifest["files"]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"::error::{MANIFEST_NAME} is unreadable ({esc(repr(exc))})")
        return 1
    if not isinstance(manifest, dict) or not isinstance(recorded, dict):
        print(f"::error::{MANIFEST_NAME} does not have the shape of a "
              "manifest - `files` must be a mapping of path to hash. Ask "
              "ci-secure to refresh the vendored copy, which rewrites it.")
        return 1

    drift = []
    # What the manifest must LIST, before what it happens to list is checked.
    # Every hash agreeing is not the property wanted - the manifest defines its
    # own domain, so a file removed from the copy AND from `files` leaves every
    # remaining hash correct and verifies clean. That is not the acknowledged
    # "anyone who can edit the gate can edit the manifest" caveat: nothing is
    # edited-with-a-matching-hash, the copy is simply made smaller than the
    # thing that was reviewed. It matters most for `config.py`, the rule that
    # says which outcomes block: with the vendored one gone the gate falls back
    # to a path inside the repository being audited, so a pull request that
    # deletes it here and adds it there runs a rule it wrote itself.
    for rel in sorted(set(vendored_paths()) - set(recorded)):
        drift.append(f"{rel}: no longer listed in {MANIFEST_NAME}, so the copy "
                     "cannot be checked against what was reviewed")
    for rel, expected in sorted(recorded.items()):
        path = dest / rel
        if not path.is_file():
            drift.append(f"{rel}: missing")
        elif sha256(path) != expected:
            drift.append(f"{rel}: modified")
    for path in dest.rglob("*"):
        rel = path.relative_to(dest).as_posix()
        # Bytecode gets its OWN message, not an exemption - and the test comes
        # BEFORE the manifest short-circuit below, because the manifest is
        # repository content and cannot be allowed to bless a `.pyc`. A `.pyc`
        # written with an unchecked hash is never validated against its source:
        # Python loads it as-is, which is how the gate loads `config.py`. So
        # one planted here can empty the set of outcomes that block while every
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
        # A symlink is drift whatever it points at. The walk does not descend
        # one, so a `__pycache__` symlinked to a build directory elsewhere in
        # the repository would otherwise hide planted bytecode from the test
        # above without the manifest being touched at all; and a vendored file
        # replaced by a symlink is not the file that was reviewed.
        if path.is_symlink():
            drift.append(
                f"{rel}: a symlink, which a vendored copy never contains - it "
                "makes the verified path and the file that is actually read "
                "two different things")
            continue
        if not path.is_file() or rel == MANIFEST_NAME or rel in recorded:
            continue
        drift.append(f"{rel}: not in the manifest")

    if not drift:
        print(f"ci-secure vendored copy matches {MANIFEST_NAME} "
              f"(skill v{esc(manifest.get('skill_version', '?'))}, "
              f"from {esc(manifest.get('source_commit', '?'))})")
        return 0

    for item in drift:
        print(f"::error::vendored ci-secure has drifted - {esc(item)}")
    print("::error::the vendored gate is no longer the code that was reviewed. "
          "Ask ci-secure to refresh the vendored copy, then review the "
          "resulting diff, or revert the local edit.")
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

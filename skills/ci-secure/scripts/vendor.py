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

    vendor.py --into <repo>       copy in (or refresh), write the manifest, and
                                  PROVE the installed gate can fail
    vendor.py --verify <dir>      recompute the hashes and compare
    vendor.py --self-test <dir>   re-run that proof on an existing copy

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
import re
import shutil
import subprocess
import sys
import tempfile
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


# --------------------------------------------------------------------------
# The self-proof: fire the installed gate once and watch it go red
# --------------------------------------------------------------------------
#
# An install used to hand back a gate nobody had ever seen fail. It ships in
# `--advisory` mode, so its first runs are green by construction, and the
# adopter is then asked to drop that flag and make it a REQUIRED check - to
# trust a blocking check on the strength of having watched it pass. This skill
# says, in its own reports and in the gate's own docstring, that a check which
# did not run is not a check that passed; the same standard has to apply to the
# gate it installs in someone else's repository.
#
# So the install proves it, both ways, before it reports success:
#
#   RED   the gate is pointed at a throwaway workflow that fails a named
#         security fact, and must exit non-zero AND name that fact;
#   GREEN the gate is pointed at the same workflow with the hole closed, and
#         must exit 0 - otherwise "it went red" proves nothing, because a gate
#         wedged red reds on everything.
#
# NOTHING IS WRITTEN INTO THE ADOPTER'S TREE. Both fixtures are generated into
# a temporary directory and deleted with it. Breaking one of their workflows to
# watch a build go red would be the obvious way to do this and is not an
# acceptable price for the demonstration, and a fixture committed into their
# repository is a deliberately vulnerable workflow file sitting under
# `.github/` forever, which their own scanners would then report.
#
# The fixtures are GENERATED here rather than shipped in `scaffold/`, for the
# same reason: a tracked, intentionally-failing workflow file is content a
# registry scanner attributes to this repository as live automation. They are
# also deliberately dull - a workflow missing a `permissions:` block is a real,
# scored security fact and needs no payload, no address and no command to
# demonstrate, so nothing here looks like an attack even out of context.

SELF_TEST_TIMEOUT_S = 300

# The fact the RED fixture is built to fail. Asserting on the id, not merely on
# a non-zero exit, is what stops the proof passing vacuously: the gate exits 1
# for a dozen reasons that are not "a security fact failed" - a missing engine,
# PyYAML absent, a crash - and every one of them would otherwise be read as
# proof that the security check works.
PROOF_FACT = "sec.permissions.workflow-declares"

_PROOF_WORKFLOW_UNSAFE = """\
name: ci-secure self-proof (throwaway fixture, not part of any repository)
on: [pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo self-proof
"""

# The same workflow with the hole closed. `permissions:` declared, and a
# CODEOWNERS entry so the other file-scoped fact passes too.
_PROOF_WORKFLOW_SAFE = """\
name: ci-secure self-proof (throwaway fixture, not part of any repository)
on: [push]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo self-proof
"""

_PROOF_CODEOWNERS = ".github/ @ci-secure-self-proof\n"


def _gate_env(dest: Path, workspace: Path) -> dict:
    """The environment one proof run gets.

    Every ambient `GITHUB_*` and `CI_SECURE_*` is dropped before ours go in. A
    maintainer running this inside Actions would otherwise have the proof
    append its fixture summary to the real job summary, scan `GITHUB_WORKSPACE`
    instead of the fixture, or pick up a `CI_SECURE_ENGINE` aimed elsewhere -
    a proof that quietly examined something other than what it says it did.

    `PYTHONDONTWRITEBYTECODE` is not hygiene here, it is correctness: the gate
    loads `config.py` by file location and the engine imports its neighbours,
    so a proof run would otherwise leave `__pycache__` inside the vendored
    directory - which the adopter's own drift check reds on, on every run, for
    a file the install itself created.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GITHUB_", "CI_SECURE_"))}
    env.update({
        "GITHUB_WORKSPACE": str(workspace),
        "CI_SECURE_ENGINE": str(dest / "scripts" / "scan.py"),
        # Explicitly off: the proof must not need a token, a network, or an
        # authenticated `gh`, and a check that silently did not run must not be
        # part of what is being demonstrated.
        "CI_SECURE_GH_IMPOSTOR": "off",
        "CI_SECURE_GH_STRICT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def _run_gate(dest: Path, workspace: Path):
    """Run the vendored gate against `workspace`. None if it did not finish."""
    try:
        return subprocess.run(
            [sys.executable, str(dest / GATE_DEST)],
            capture_output=True, text=True, errors="replace",
            cwd=str(workspace), env=_gate_env(dest, workspace),
            timeout=SELF_TEST_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _write_proof_tree(root: Path, workflow: str, codeowners: str = "") -> Path:
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows" / "self-proof.yml").write_text(
        workflow, encoding="utf-8")
    if codeowners:
        (root / ".github" / "CODEOWNERS").write_text(codeowners, encoding="utf-8")
    return root


def _headline(output: str) -> str:
    for line in output.splitlines():
        if "facts pass" in line:
            return line.strip()
    return "(the gate printed no headline)"


def self_test(dest: Path) -> int:
    """Prove the installed gate can fail, then say what it makes of this repo.

    Returns 0 proved, 1 NOT proved, 2 could not be run here. The three are kept
    apart on purpose: "this gate cannot go red" and "this machine could not run
    the proof" are different facts about the install, and collapsing them would
    let a missing interpreter read as a broken gate or - far worse - the other
    way round.
    """
    gate = dest / GATE_DEST
    if not gate.is_file():
        print(f"::error::self-proof: no gate at {gate}, so there is nothing to "
              "prove. This copy is not a working install.")
        return 1

    with tempfile.TemporaryDirectory(prefix="ci-secure-self-proof-") as tmp:
        unsafe = _write_proof_tree(Path(tmp) / "unsafe", _PROOF_WORKFLOW_UNSAFE)
        safe = _write_proof_tree(Path(tmp) / "safe", _PROOF_WORKFLOW_SAFE,
                                 _PROOF_CODEOWNERS)
        red = _run_gate(dest, unsafe)
        green = _run_gate(dest, safe)

    if red is None or green is None:
        print("::warning::self-proof COULD NOT RUN: the gate did not finish "
              f"within {SELF_TEST_TIMEOUT_S}s, or could not be started at all. "
              "The gate is installed and UNPROVEN - re-run "
              f"`vendor.py --self-test {dest}` somewhere it can run.")
        return 2

    expected = f"ci-secure fact failed: {PROOF_FACT}"
    if red.returncode != 0 and expected not in red.stdout:
        # Red for a reason that is not the security check: almost always the
        # engine, which needs Python 3.12 and PyYAML while this script needs
        # neither. Reporting that as a successful proof is the exact vacuous
        # pass this whole function exists to prevent.
        for line in (red.stdout + red.stderr).splitlines()[-20:]:
            print(f"gate| {line}")
        print("::warning::self-proof COULD NOT RUN: the gate went red on the "
              "throwaway vulnerable workflow, but not because a security fact "
              f"failed ({PROOF_FACT} was never reported), so it proves nothing "
              "about the check. The usual cause is the engine's own "
              "requirements - Python 3.12 and PyYAML - missing on this "
              "machine; CI installs them. The gate is installed and UNPROVEN: "
              f"re-run `vendor.py --self-test {dest}` where the engine can run.")
        return 2

    if red.returncode == 0:
        for line in red.stdout.splitlines()[-20:]:
            print(f"gate| {line}")
        print("::error::self-proof FAILED: the gate passed a workflow that "
              f"fails `{PROOF_FACT}` - it declares no `permissions:` block. A "
              "gate that cannot go red is not a gate, and making this a "
              "required check would add a green tick and no protection. Do not "
              "rely on this install; the files are on disk, so review them, or "
              "ask ci-secure to refresh the copy.")
        return 1

    if green.returncode != 0:
        for line in green.stdout.splitlines()[-20:]:
            print(f"gate| {line}")
        print("::error::self-proof FAILED: the gate went red on the throwaway "
              "workflow with the hole CLOSED, so its red on the vulnerable one "
              "proves nothing - a gate wedged red reds on everything, and "
              "requiring it would block every pull request. Do not rely on "
              "this install.")
        return 1

    print(f"self-proof PASSED: pointed at a throwaway workflow that fails "
          f"`{PROOF_FACT}`, the installed gate exited {red.returncode} and "
          f"named that fact; pointed at the same workflow with the hole "
          f"closed, it exited 0. The gate can fail, and it does not fail "
          "indiscriminately. Both fixtures were temporary files, now deleted - "
          "nothing was written into this repository.")
    return 0


def report_on_this_repo(dest: Path, repo: Path) -> None:
    """Run the installed gate on the adopter's own tree and say what it found.

    Read-only, and informational only: this NEVER decides whether the install
    succeeded. A repository that has never been scanned usually reds a fact or
    three on its first run, which is what `--advisory` is for. What the adopter
    must not be left guessing at is which of the two they are looking at when
    the check first appears on a pull request.
    """
    run = _run_gate(dest, repo)
    if run is None:
        print("::warning::the gate could not be run against this repository "
              f"within {SELF_TEST_TIMEOUT_S}s, so what it will say about your "
              "code is not known yet.")
        return
    failed = [line.split("::error::", 1)[1] for line in run.stdout.splitlines()
              if line.startswith("::error::")]
    print(f"on THIS repository, blocking (no --advisory): "
          f"{_headline(run.stdout)}")
    for item in failed:
        print(f"  would block: {item}")
    if failed:
        print("  The workflow this install writes passes `--advisory`, so "
              "those are REPORTED and do not block until that flag is "
              "removed.")


# --------------------------------------------------------------------------
# Guard-registration conventions the host repository declares
# --------------------------------------------------------------------------
#
# Some repositories keep a register of their own build-breaking checks - a
# harness that mutates each guard and asserts it fires, a list every new check
# has to be added to - and declare that convention in their contributor docs.
# A gate installed without being added to it is a check that convention was
# meant to cover and does not.
#
# This DETECTS and TELLS, and never edits: registering into an arbitrary repo's
# harness means writing into files this tool knows nothing about, and guessing
# wrong there is worse than saying nothing. It is deliberately a keyword read
# over a handful of well-known doc paths - it will miss conventions written in
# other words, and that is the acceptable direction to be wrong in, because the
# cost of a miss is silence and the cost of a false hit is one sentence the
# adopter can dismiss by reading the line it quotes.

_GUARD_DOCS = ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md",
               ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md")
_GUARD_DOC_BYTES = 400_000
_GUARD_CONVENTION = re.compile(
    r"guard[\w.:-]*\b[^\n]{0,60}\bregist"      # "every guard ... registered"
    r"|regist\w*\b[^\n]{0,60}\bguard\b"        # "register ... guard"
    r"|\bguard:[a-z][\w-]*",                   # a `guard:verify`-style task
    re.IGNORECASE)


def guard_convention_notice(repo: Path) -> list[str]:
    """Lines quoting any guard-registration convention this repo declares."""
    notices = []
    for rel in _GUARD_DOCS:
        path = repo / rel
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[
                :_GUARD_DOC_BYTES]
        except OSError:                          # pragma: no cover - defensive
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if _GUARD_CONVENTION.search(line):
                notices.append(f"{rel}:{number}: {esc(line.strip()[:160])}")
                break                            # one quote per file is enough
    return notices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--into", metavar="REPO",
                       help="repository root to vendor ci-secure into")
    group.add_argument("--verify", metavar="DIR",
                       help="vendored ci-secure directory to check")
    group.add_argument("--self-test", metavar="DIR", dest="self_test",
                       help="prove an installed gate can fail, against "
                            "throwaway fixtures; writes nothing")
    args = parser.parse_args(argv)

    if args.verify:
        return verify(Path(args.verify).resolve())

    if args.self_test:
        dest = Path(args.self_test).resolve()
        outcome = self_test(dest)
        report_on_this_repo(dest, dest.parent)
        return outcome

    repo = Path(args.into).resolve()
    dest = install(repo)
    print(f"vendored ci-secure into {dest}")

    for notice in guard_convention_notice(repo):
        print(f"::warning::this repository documents a guard-registration "
              f"convention and the ci-secure gate has NOT been registered "
              f"with it - {notice}")

    # The proof runs on a refresh too, and there is no flag to skip it. A
    # refresh replaces the engine, the gate and the rule, so it is exactly a
    # moment when a gate can stop being able to fail; and a proof that can be
    # turned off is one that gets turned off in the script that automates the
    # install, which is where it was most needed.
    outcome = self_test(dest)
    report_on_this_repo(dest, repo)
    # Files are written either way, and the exit code says which of the two
    # things happened: 0 the gate was proved able to fail, 1 it was not. An
    # environment that could not run the proof (2 above) is a warning, not a
    # failure of the install - but it is not a proof either, and the caller is
    # told to re-run it somewhere the engine can run.
    return 1 if outcome == 1 else 0


if __name__ == "__main__":
    sys.exit(main())

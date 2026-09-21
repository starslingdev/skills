#!/usr/bin/env python3
"""Phase 4c helper — turn captured catalog gaps into drafted detectors.

SKILL.md phase 4b captures every coverage-gap pole (a drilled job whose log
matched no `_parse_log` detector) to `.ci-speedup-gaps/<repo>__<job>/`. Phase 4c
(maintainer source only) promotes those one-off LLM gap-fills into PERMANENT
deterministic detectors, so the same stack drills measured + auditable next time
(ARCHITECTURE §12.7: extend the catalog first).

Drafting a detector needs an LLM — writing a correct regex from a log is
judgment, not mechanics. But everything AROUND it is deterministic, and this
script owns that half so 4c stops depending on a human noticing the gap:

  list             Which captures are still PENDING (no detector fires on them
                   yet) vs already PROMOTED (a detector now matches — loop closed).
  prepare [SLUG…]  Emit the exact, self-contained drafting TASK for a subagent:
                   the gap→catalog methodology prompt + the specific capture paths
                   + a per-gap summary + the insertion points + the verify command.
                   The orchestrator hands this to a background subagent (the one
                   irreducible LLM step) — nothing is improvised.
  verify [SLUG…]   The deterministic GATE after drafting: assert `_parse_log` now
                   FIRES on each capture's real job log with a `fix_key` that has a
                   `_FIX_META` entry (the gap is genuinely closed), then run the
                   detector test suite. Non-zero exit if a gap isn't closed or a
                   test fails — so the subagent (or you) iterate until it's green.

A capture is "pending" iff `_parse_log` returns None on its job log — the same
ground truth the renderer uses, so state can't drift. Nothing here edits the
catalog or opens a PR: drafting is the subagent's job; the PR is the maintainer's
gate. Maintainer-local: the only run-data it reads is the gitignored
`.ci-speedup-gaps/` (it also reads the committed methodology prompt and runs the
detector test suite), and it never commits the captures.

Exit codes: 0 ok; 1 a gap isn't closed / a test failed / methodology prompt
unreadable; 2 no captures (and argparse usage errors, which argparse owns).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent          # maintainers/ci-speedup/scripts
_LOOP_ROOT = _SCRIPTS.parent                         # maintainers/ci-speedup — this loop's OWN tree
_REPO_ROOT = _LOOP_ROOT.parent.parent                # repo root (maintainers/ci-speedup → maintainers → root)
# The AUDITED skill stays in skills/ci-speedup/ — it is NOT relocated. The detector code/tests this
# script edits anchor on _SKILL_DIR; the methodology prompt (moved alongside this script) on _LOOP_ROOT.
_SKILL_DIR = _REPO_ROOT / "skills" / "ci-speedup"
# Captured gaps root at the REPO ROOT, OUTSIDE skills/<name>/ — the `skills` installer copies the skill
# dir recursively excluding only {.git, __pycache__, __pypackages__} (no dotfile exclusion), so a capture
# dir under the skill would ship to end users. Writer: blocking_path.py `_gaps_root_default()`.
_GAPS_ROOT = _REPO_ROOT / ".ci-speedup-gaps"
_PROMPT = _LOOP_ROOT / "loops" / "gap-to-catalog-prompt.md"   # moved into this loop's loops/ dir
_DETECTOR_TESTS = _SKILL_DIR / "tests" / "test_blocking_path.py"   # the detector test STAYS in the skill

# blocking_path.py stays in the skill's scripts dir (it's runtime skill code), so import it from there
# — NOT from _SCRIPTS, which is now this loop's sibling scripts dir and no longer holds it.
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
import blocking_path as bp  # noqa: E402  (uniquely-named module in the skill's scripts dir)


class Capture:
    """One `.ci-speedup-gaps/<slug>/` gap capture: its job log, the phase-4a
    analysis, and meta. `fires()` re-runs the live `_parse_log` on the raw log —
    None means still a gap (PENDING); a leaf means a detector now matches it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.slug = path.name

    @property
    def log_path(self) -> Path:
        return self.path / "job.log"

    def meta(self) -> dict[str, Any]:
        try:
            data = json.loads((self.path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        # Valid-but-non-dict JSON (top-level null / array / scalar) parses fine but would crash
        # every downstream `.get(...)`; the {} fallback must cover it too, matching the `-> dict`
        # contract and the module's otherwise-paranoid handling of corrupt feedstock.
        return data if isinstance(data, dict) else {}

    def analysis(self) -> dict[str, Any]:
        try:
            data = json.loads((self.path / "analysis.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def fires(self) -> dict[str, Any] | None:
        """The live detector verdict on this capture's raw job log.

        NOTE: config-gated leaves (OPT78 / `vitest-isolate-pool`) also need
        scan's `test_runner_isolation` fact, which a capture dir does not carry
        (`job.log` + `meta.json` + `analysis.json` only), so they always read as
        "pending" here. Harmless today because `_gap_poles` passes the same fact
        and therefore never CAPTURES a pole whose leaf fired — only a suppressed
        one, for which "pending" is the right answer. A capture taken before that
        gating existed can still mislead the loop; stamp the fact into meta.json
        if this ever needs to be exact."""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return bp._parse_log(text)

    def log_sha(self) -> str | None:
        """SHA-256 of the raw job log, or None if unreadable. Used to detect the
        corruption signature where one pole's log was stamped onto several captures."""
        try:
            return hashlib.sha256(self.log_path.read_bytes()).hexdigest()
        except OSError:
            return None

    def is_valid(self) -> bool:
        return self.log_path.exists()


def _dup_log_groups(caps: list[Capture]) -> list[list[Capture]]:
    """Captures that share an IDENTICAL job.log across DIFFERENT job names — the signature
    of a binding bug having stamped one pole's log onto several captures (a Go-vet capture
    holding the bun-test log). Distinct jobs always have distinct logs, so a collision is
    never legitimate: it means the feedstock is corrupt and must not drive detector drafting
    (a detector matching the shared log would 'close' every colliding slug — a false green).
    Two captures of the SAME job (same meta.job) are not flagged. Returns groups of size ≥2."""
    by_sha: dict[str, list[Capture]] = {}
    for c in caps:
        sha = c.log_sha()
        if sha:
            by_sha.setdefault(sha, []).append(c)
    groups = []
    for members in by_sha.values():
        jobs = {str(c.meta().get("job") or c.slug) for c in members}
        if len(members) >= 2 and len(jobs) >= 2:
            groups.append(members)
    return groups


def _unreadable_captures(caps: list[Capture]) -> list[Capture]:
    """Captures whose `job.log` can't be read (`log_sha()` is None). An unreadable log is its
    own corruption signal: it can't be cleared for a dup-log collision (so a corrupt pair with
    one unreadable half would otherwise slip the guard) and can't be drafted from."""
    return [c for c in caps if c.log_sha() is None]


def _warn_dup_logs(selected: list[Capture], universe: list[Capture]) -> bool:
    """Print a loud refusal for any duplicate-log group that includes a SELECTED capture; True when
    such corruption was found.

    Collisions are detected over the FULL `universe`, NOT just `selected`: a collision needs ≥2
    colliding captures, and the colliding sibling may sit OUTSIDE a slug subset
    (`draft_detector prepare <one-slug>`). Building the guard from the slug-filtered set alone is
    subset-blind — the lone selected capture is a one-member group, no collision is seen, and the
    poisoned log gets handed to the drafting subagent (or 'closes' a verify as a false green). So we
    scan the universe and flag any group that includes a selected slug (a collision among
    non-selected captures only doesn't block THIS run)."""
    selected_slugs = {c.slug for c in selected}
    bad = False
    for g in _dup_log_groups(universe):
        if not any(c.slug in selected_slugs for c in g):
            continue
        slugs = ", ".join(c.slug for c in g)
        print(f"  ✗ CORRUPT FEEDSTOCK: {len(g)} differently-named captures share an "
              f"identical job.log — {slugs}. One pole's log was stamped onto the others "
              "(a binding bug). A detector drafted from this would falsely 'close' every "
              "colliding slug. Delete the wrong capture(s) and re-render to re-capture each "
              "pole's own log before drafting.", file=sys.stderr)
        bad = True
    return bad


def _captures(slugs: list[str] | None = None) -> list[Capture]:
    """Valid captures under `.ci-speedup-gaps/`, optionally filtered to `slugs`."""
    if not _GAPS_ROOT.is_dir():
        return []
    caps = [Capture(p) for p in sorted(_GAPS_ROOT.iterdir()) if p.is_dir()]
    caps = [c for c in caps if c.is_valid()]
    if slugs:
        want = set(slugs)
        caps = [c for c in caps if c.slug in want]
    return caps


def cmd_list(_args: argparse.Namespace) -> int:
    caps = _captures()
    if not caps:
        print(f"No gap captures under {_GAPS_ROOT}.")
        return 2
    pending, promoted = [], []
    for c in caps:
        (promoted if c.fires() else pending).append(c)
    print(f"{len(caps)} capture(s) in {_GAPS_ROOT}:\n")
    if pending:
        print(f"PENDING — no detector fires yet ({len(pending)}):")
        for c in pending:
            m = c.meta()
            print(f"  • {c.slug}  [{m.get('dominant_step') or '?'} · "
                  f"{m.get('workflow_file') or '?'}]")
        print("\n  → draft: python3 maintainers/ci-speedup/scripts/draft_detector.py prepare")
    if promoted:
        print(f"\nPROMOTED — a detector now matches ({len(promoted)}):")
        for c in promoted:
            leaf = c.fires() or {}
            print(f"  • {c.slug}  →  fix_key={leaf.get('fix_key')}")
    return 0


def _task_for(caps: list[Capture]) -> str:
    """The self-contained drafting task: the methodology prompt + the concrete
    captures + the target files to edit + the verify gate. This is what the
    orchestrator hands to a background subagent — deterministic so the launch isn't
    improvised. Raises OSError if the methodology prompt is unreadable: a drafting
    task without the methodology isn't a usable task, so the caller must fail loudly
    rather than emit a stub (which could skip the repo-text scrub rules)."""
    prompt = _PROMPT.read_text(encoding="utf-8")
    lines = [
        "# TASK: draft deterministic catalog detector(s) for captured ci-speedup gaps",
        "",
        "You are the gap → catalog drafting subagent (SKILL.md phase 4c). Follow the",
        "METHODOLOGY below. Unlike a pure analysis run, you ARE asked to APPLY the edits",
        "(the maintainer reviews them at the PR gate, not before): for each detector you",
        "propose, write the `_parse_log` branch + `_FIX_META` entry into",
        "`skills/ci-speedup/scripts/blocking_path.py` and a unit test into",
        f"`skills/ci-speedup/tests/{_DETECTOR_TESTS.name}` (grounded in the captured log's SHAPE, scrubbed",
        "of any repo-specific text per the scrub rules). Then run the GATE:",
        "",
        "    python3 maintainers/ci-speedup/scripts/draft_detector.py verify " +
        " ".join(c.slug for c in caps),
        "",
        "and iterate until it passes (every listed gap fires a detector whose `fix_key`",
        "has a `_FIX_META` entry, AND the detector test suite is green). Do NOT open a",
        "PR or commit — hand back to the maintainer for the one-time yes at the PR gate.",
        "",
        "## PENDING CAPTURES TO PROMOTE",
        "",
    ]
    for c in caps:
        m, a = c.meta(), c.analysis()
        cause = str(a.get("cause") or "").strip()
        lines += [
            f"### `{c.slug}`",
            f"- capture dir: `{c.path}`",
            f"- job log (raw evidence): `{c.log_path}`",
            f"- analysis (phase-4a read): `{c.path / 'analysis.json'}`",
            f"- dominant step: {m.get('dominant_step') or '?'}  ·  "
            f"workflow: {m.get('workflow_file') or '?'}",
            f"- phase-4a cause: {cause[:400] or '(none recorded)'}",
            "",
        ]
    lines += ["---", "", "## METHODOLOGY (from maintainers/ci-speedup/loops/gap-to-catalog-prompt.md)",
              "", prompt]
    return "\n".join(lines)


def cmd_prepare(args: argparse.Namespace) -> int:
    caps = _captures(args.slugs or None)
    if not caps:
        where = ", ".join(args.slugs) if args.slugs else str(_GAPS_ROOT)
        print(f"No matching gap captures ({where}).", file=sys.stderr)
        return 2
    # Refuse corrupt feedstock up front, BEFORE handing any log to the drafting subagent:
    # a duplicate-log group (one pole's log stamped onto several captures) or an unreadable
    # job.log (can't be drafted from). Scan the FULL capture universe for dup collisions so a
    # subset invocation can't hide a colliding sibling. `prepare` LAUNCHES the subagent, so it
    # must not pass it a bad log.
    bad = _warn_dup_logs(caps, _captures())
    for c in _unreadable_captures(caps):
        print(f"  ✗ UNREADABLE: {c.slug} has no readable job.log — can't draft a detector "
              "from it. Fix or remove the capture.", file=sys.stderr)
        bad = True
    if bad:
        print("\nRefusing to draft from corrupt feedstock (see above).", file=sys.stderr)
        return 1
    pending = [c for c in caps if not c.fires()]
    if not pending:
        print("All matching captures are already PROMOTED (a detector fires on "
              "each). Nothing to draft.", file=sys.stderr)
        return 0
    try:
        task = _task_for(pending)
    except OSError as e:
        print(f"draft_detector: methodology prompt unreadable ({_PROMPT}): {e} — "
              "cannot assemble a usable drafting task.", file=sys.stderr)
        return 1
    print(task)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    caps = _captures(args.slugs or None)
    if not caps:
        where = ", ".join(args.slugs) if args.slugs else str(_GAPS_ROOT)
        print(f"No matching gap captures ({where}).", file=sys.stderr)
        return 2
    ok = True
    print("Gap-closure check (does a detector now fire on each capture's job log?):")
    # A detector matching a log shared across differently-named captures would 'close'
    # every colliding slug — a false green. Refuse before trusting any per-slug verdict. Scan the
    # FULL universe for dup collisions so a subset (`verify <one-slug>`) can't hide the sibling.
    if _warn_dup_logs(caps, _captures()):
        print("\nCorrupt feedstock — fix the captures before verifying.", file=sys.stderr)
        return 1
    for c in caps:
        # Distinguish an unreadable log from a genuine no-match BEFORE trusting the
        # None sentinel: `fires()` collapses both to None, but they need different
        # fixes — fix the capture vs. draft a detector. Don't send the drafting
        # subagent to write a detector for a log it can't read.
        try:
            c.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  ✗ {c.slug}: job.log unreadable ({e}) — fix the capture, "
                  "not a detector")
            ok = False
            continue
        leaf = c.fires()
        if leaf is None:
            print(f"  ✗ {c.slug}: still UNMATCHED — no detector fires (gap not closed)")
            ok = False
        elif leaf.get("fix_key") not in bp._FIX_META:
            print(f"  ✗ {c.slug}: fires fix_key={leaf.get('fix_key')!r} but it has NO "
                  f"_FIX_META entry (the hand-off is missing)")
            ok = False
        else:
            print(f"  ✓ {c.slug}: closed → fix_key={leaf.get('fix_key')}")
    if not ok:
        print("\nGaps remain open — draft/adjust the detector(s) and re-run verify.",
              file=sys.stderr)
        return 1
    # Deterministic regression gate: the detector suite must be green (the new
    # test exists and nothing else reclassified).
    print(f"\nRunning detector tests ({_DETECTOR_TESTS.name})…")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(_DETECTOR_TESTS), "-q"],
        cwd=str(_SKILL_DIR))
    if r.returncode != 0:
        print("\nDetector tests FAILED — fix before opening a PR.", file=sys.stderr)
        return 1
    print("\n✓ all gaps closed and detector tests pass — ready for the maintainer's "
          "PR gate (branch, run the full suite, open a PR).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase 4c: draft + verify catalog detectors for captured gaps.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show PENDING vs PROMOTED gap captures")
    p_prep = sub.add_parser("prepare", help="emit the subagent drafting task")
    p_prep.add_argument("slugs", nargs="*", help="capture slug(s); default: all pending")
    p_ver = sub.add_parser("verify", help="assert detectors now fire + tests pass")
    p_ver.add_argument("slugs", nargs="*", help="capture slug(s); default: all captures")
    args = ap.parse_args(argv)
    return {"list": cmd_list, "prepare": cmd_prepare, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

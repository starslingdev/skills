"""The scored security config facts and the security score.

ci-secure owns the security component of the CI Score. The ten attack vectors
stay findings-only and never enter this number (several detectors are lexical,
and a public penalty would grade strangers down on unconfirmed matches).
What IS scored are deterministic, self-verifiable configuration facts — the
same "config facts, pass/fail, no judgment calls" shape ci-score uses.

DISJOINTNESS IS A CONSTRAINT TO ENFORCE, NOT ASSUME. Every fact here
must be disjoint from ci-score's shipped registry: one YAML edit must never
move a ci-score check and a fact here at the same time. The census lives in
`references/security-facts.md` and is pinned by test against a frozen manifest
of ci-score's check ids. The one near-collision the review found is handled by
construction: ci-score's `ci.security.scoped-id-token` owns `id-token:` scoping,
so the per-job-scoping fact here covers permissions OTHER than `id-token` only.

THE EIGHT FACTS

  F1 sec.permissions.workflow-declares    every workflow declares `permissions:`
                                          (top level, or on every job)
  F2 sec.permissions.write-scoped         no workflow-level WRITE permission
                                          other than `id-token` (writes belong
                                          on jobs; `write-all` fails)
  F3 sec.codeowners.workflows             a CODEOWNERS entry covers
                                          `.github/workflows/`
  F4 sec.trigger.fork-code-uncleared      no untrusted-trigger workflow checks
                                          out the attacker's head ref
  F5 sec.secrets.no-blanket-inherit       no reusable-workflow call passes
                                          `secrets: inherit`
  F6 sec.checkout.credentials-scoped      on untrusted-trigger workflows, every
                                          checkout sets persist-credentials:
                                          false
  F7 sec.required-checks.skippable        every required status check is
                                          produced by a job that always runs
                                          (API-gated)
  F8 sec.fork-approval.effective          fork-PR CI approval gates more than
                                          accounts new to GitHub (API-gated)

F7 and F8 read the GitHub API, not workflow YAML, so they are TOKEN-GATED: no
repo or no token means UNMEASURED with the reason stated — the same contract
the impostor-SHA vector keeps. They are never a pass for being unreadable and
never a fail for it either.

F4 replaces "has a dangerous trigger", which is true of 84% of the repos
measured during development and so discriminates nobody. The tiering is deliberate and keeps the
findings-only rule intact: a bare untrusted trigger is ignored (too common to
mean anything); a trigger plus a checkout of the attacker's head FAILS THIS FACT
(the fork-code detector cannot clear the trigger as inert); the full chain —
trigger + head checkout + execution — is the P14.9 FINDING, which stays out of
this number. P14.9's own docstring reserves the middle tier for exactly this
fact ("the bare trigger without the head checkout ... belongs to the scored
config checks").

WHAT IS NEVER A SILENT PASS. Facts F1/F2/F4/F5/F6/F7 are universal claims
over
every workflow file, so ANY unscannable workflow (`scan_incomplete`) forces
them to UNMEASURED — no pass, no fail, a stated reason, and they stay in the
applicable count as a visible coverage gap (the same shape ci-speedup's
speed_score uses). F3 reads repo files and F8 a repository setting, not
workflow YAML, and neither is affected.

THE REGISTERED SCORE — MACHINE-ONLY. This aggregate is NEVER rendered in the
report (report.py prohibits it and tests/verify_report.py fails a report that
shows one); it exists so ci-advisor can blend the three engines. Readers get
the pass/fail fact rows instead. `security_score = 100 x passed / scored`, no
weights, no partial credit — registered before any calibration run (never-tune), with the
constants emitted alongside the number so a disputed score is checkable
without reading this source.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

REGISTERED = "2026-08-03"


def _load_sibling(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _scan() -> ModuleType:
    """Load THIS skill's scan module by file location.

    A bare `import scan` resolves against whichever `scan.py` won the
    interpreter's path race — under the repo-wide pytest several skills ship
    colliding module names (the failure `tests/_scan_import.py` documents).
    Loading by location is deterministic everywhere: script, subprocess, test.
    """
    if "ci_secure_scan" in sys.modules:
        return sys.modules["ci_secure_scan"]
    here = Path(__file__).resolve().parent
    saved = {n: sys.modules.get(n) for n in ("config", "gh_utils")}
    try:
        _load_sibling("config", here / "config.py")
        _load_sibling("gh_utils", here / "gh_utils.py")
        return _load_sibling("ci_secure_scan", here / "scan.py")
    finally:
        for n, mod in saved.items():
            if mod is not None:
                sys.modules[n] = mod
            else:
                sys.modules.pop(n, None)


@lru_cache(maxsize=1)
def _gh_utils() -> ModuleType:
    """This skill's `gh_utils`, loaded by location for the same reason `_scan`
    is: the module name collides across the skills in this repository.

    Cached because re-executing the module also resets `check_prereqs`'s own
    cache, so each API-gated fact was spawning its own `gh auth status` and, on
    an unauthenticated run, logging its own ERROR line for a state the caller
    has already handled.
    """
    here = Path(__file__).resolve().parent
    return _load_sibling("gh_utils", here / "gh_utils.py")


# --- CODEOWNERS (F3) ---------------------------------------------------------
# (dropped from the vector catalog in the critical-only descope — a missing
# CODEOWNERS rule is a presence fact, not an attack chain — and restored here
# as a scored config fact, which is the honest weight for it.)
# GitHub's documented precedence: `.github/CODEOWNERS`, then root, then docs/.
_CODEOWNERS_CANDIDATES = (
    Path(".github/CODEOWNERS"),
    Path("CODEOWNERS"),
    Path("docs/CODEOWNERS"),
)
_WORKFLOWS_CODEOWNER_PATTERNS = (
    # `.github/workflows/` must BE the rule (directory form) or carry a glob —
    # `.github/workflows/ @team`, `/.github/workflows/* @team`,
    # `.github/workflows/** @team`. A rule naming ONE file
    # (`.github/workflows/release.yml @team`) protects that file and nothing
    # else; matching on the bare prefix graded such a repo as covered, which is
    # a false clean on the exact claim this fact makes. An extension glob
    # (`*.yml`) is still accepted as directory-wide intent, even though a
    # `.yaml` workflow would slip it — failing a correctly-configured repo over
    # that costs more than it catches.
    #
    # A RESTRICTED glob is the same false clean as the single-file rule, one
    # step removed: `.github/workflows/*release*.yml` owns the release
    # workflows and leaves every other workflow in the directory unowned. So
    # the accepted glob shapes are enumerated rather than left to `\S*` —
    # `*`, `**`, `**/*`, and an extension suffix on any of those — and a glob
    # carrying any other literal text in the filename fails.
    #
    # The trailing slash is OPTIONAL on the directory itself: CODEOWNERS uses
    # gitignore semantics, where a directory pattern with no trailing slash
    # matches the directory AND everything under it. `.github/workflows @team`
    # is exactly as covering as `.github/workflows/ @team`.
    r"^\s*/?\.github/workflows"
    r"(?:/(?:\*{1,2}(?:/\*{1,2})?(?:\.[A-Za-z0-9_-]+)?)?)?(?:\s|$)",
    # `.github/**` recurses and covers workflows; single-star `.github/*` does
    # NOT (gitignore semantics: `*` doesn't cross `/`) and is deliberately
    # absent — including it produced false negatives.
    r"^\s*/?\.github/\*\*(?:\s|$)",
    # The standard recursive DIRECTORY form `.github/ @team` covers everything
    # under .github/, workflows included (CODEOWNERS paths are prefix rules; a
    # trailing slash names the tree). Its absence graded correctly-configured
    # repos down 1/6. The slash is optional here for the same gitignore reason
    # as above, and the alternation ends `(?:\s|$)` so a BARE `.github/` at end
    # of line — the exact ownerless form — matches too. Requiring trailing
    # whitespace let that line fall through every pattern, so an earlier
    # `* @team` was the last match and graded the repo covered.
    r"^\s*/?\.github/?(?:\s|$)",
    # Bare `*` global owner, followed by whitespace or EOL — a bare `^\s*\*`
    # would also match extension rules like `*.go @team`, which do not cover
    # workflow files.
    r"^\s*\*(?:\s|$)",
)

# A matching path is only half a rule. A CODEOWNERS line that names a path and
# NO owner (`.github/workflows/` on its own) does not assign a reviewer — in
# GitHub's semantics an ownerless pattern removes ownership for those paths, so
# it is the opposite of what this row claims. Matching the path alone reported
# such a repo as covered.
#
# An owner is a `@user`, a `@org/team`, or an email address. Anchored on a word
# boundary at each end so a stray `@` inside a path fragment is not read as an
# owner.
_CODEOWNERS_OWNER = re.compile(
    r"(?:^|\s)(?:@[A-Za-z0-9][A-Za-z0-9._/-]*"
    r"|[^@\s]+@[^@\s]+\.[A-Za-z]{2,})(?=\s|$)"
)

# A rule that names a SPECIFIC path UNDER `.github/workflows/` — a single file
# (`.github/workflows/release.yml`) or a restricted glob
# (`.github/workflows/*release*.yml`). These never match a directory-coverage
# pattern above (that is what makes them narrow), so the coverage loop sees them
# only here. They matter for one reason: GitHub applies the LAST matching rule
# PER FILE, so a broad owner (`* @team`) followed by a narrow OWNERLESS rule
# leaves that one workflow with no reviewer, even though the directory as a whole
# still has an owner. The path token is captured so a later broad rule — which is
# the last match for that file and re-owns it — can cancel the exemption.
_WORKFLOWS_NARROW = re.compile(r"^\s*/?\.github/workflows/(\S+)")


def _names_existing_workflow(root: Path, subpath: str) -> bool:
    """True only if a narrow rule fragment under `.github/workflows/` matches a
    workflow file that ACTUALLY EXISTS — a literal filename or a restricted
    glob (`*release*.yml`).

    An ownerless narrow rule removes coverage only from a workflow that is
    really there. A STALE entry for a since-deleted file (a common CODEOWNERS
    residue) matches nothing, so it strips ownership from nothing and must not
    be read as "a workflow merges with no reviewer" — that would fail a repo
    whose every actual workflow is covered.
    """
    wf_dir = root / ".github" / "workflows"
    try:
        if any(ch in subpath for ch in "*?["):
            return any(wf_dir.glob(subpath))
        return (wf_dir / subpath).is_file()
    except (OSError, ValueError):
        return False


def _codeowners_covers_workflows(root: Path) -> tuple[str, str]:
    """(outcome, evidence) where outcome is pass / fail / unmeasured.

    THREE states, not two. "I could not read the file" is not the same claim as
    "this repo has no rule covering workflows", and returning a bare False for
    it scored a repo down for an unreadable directory or a mis-encoded file.
    Anything that stops the check from resolving comes back UNMEASURED — the
    same semantics the workflow-scoped facts use for a scan gap.
    """
    found: Path | None = None
    try:
        # `is_file()` re-raises EACCES on an unreadable parent directory, so
        # the probe belongs INSIDE the guard: one unreadable dir used to
        # escape to scan.py's broad backstop and take all twelve facts down.
        for candidate in _CODEOWNERS_CANDIDATES:
            if (root / candidate).is_file():
                found = root / candidate
                break
    except OSError as exc:
        return "unmeasured", ("unmeasured: could not look for a CODEOWNERS "
                              f"file ({exc})")
    if found is None:
        return "fail", ("no CODEOWNERS file at .github/CODEOWNERS, CODEOWNERS, "
                        "or docs/CODEOWNERS, so workflow changes merge with "
                        "the same approvals as any other change")
    rel = str(found.relative_to(root))
    try:
        # utf-8-SIG: a UTF-8 BOM lands in front of the first line and defeats
        # the `^` anchor, so a repo whose first rule covers workflows was
        # reported as having no entry at all. Decoding is STRICT on purpose —
        # `errors="replace"` laundered an undecodable file into a confident
        # "no entry covering workflows", which is a fabricated fail.
        text = found.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return "unmeasured", (f"unmeasured: `{rel}` could not be read as "
                              f"UTF-8 text ({exc}), so whether it covers "
                              "`.github/workflows/` is unknown")
    # GitHub applies the LAST matching rule, not the first, so the file must be
    # read to the end. Returning on the first match graded
    # `* @team` followed by a bare `.github/workflows/` as covered, when the
    # later ownerless rule is the one GitHub applies and it assigns nobody.
    #
    # TWO things are tracked, because "the directory has a default owner" and
    # "every workflow file has an owner" are different claims. `covered_by_dir`
    # is the last directory-level rule's owner status — the default that applies
    # to any workflow file no more-specific rule names. `exempted` holds workflow
    # files a later NARROW ownerless rule stripped of an owner: `* @team` then
    # `.github/workflows/release.yml` (no owner) leaves release.yml merging with
    # no reviewer, so the directory is not uniformly covered even though its
    # default owner is set. A later directory-level rule is the last match for
    # every file under it, re-owning them, so it clears the exemption set.
    covered_by_dir: bool | None = None
    exempted: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        dir_match = None
        for pat in _WORKFLOWS_CODEOWNER_PATTERNS:
            dir_match = re.match(pat, line)
            if dir_match is not None:
                break
        if dir_match is not None:
            covered_by_dir = bool(_CODEOWNERS_OWNER.search(line[dir_match.end():]))
            exempted.clear()
            continue
        narrow = _WORKFLOWS_NARROW.match(line)
        if narrow is not None:
            path = narrow.group(1)
            if _CODEOWNERS_OWNER.search(line[narrow.end():]):
                exempted.discard(path)
            elif _names_existing_workflow(root, path):
                # Only strip coverage when the rule names a workflow that is
                # really present; a stale rule for a deleted file removes
                # ownership from nothing (see `_names_existing_workflow`).
                exempted.add(path)
    if covered_by_dir and exempted:
        sample = sorted(exempted)[0]
        return "fail", (
            f"`{rel}` names a default owner for `.github/workflows/`, but a "
            f"later ownerless rule strips the owner from "
            f"`.github/workflows/{sample}`, so that workflow merges with no "
            "assigned reviewer")
    if covered_by_dir:
        return "pass", f"`{rel}` covers `.github/workflows/`"
    if covered_by_dir is False:
        # Name the near-miss: "no entry" would send the reader looking in the
        # wrong place when the entry is there and simply owns nothing.
        return "fail", (f"`{rel}` matches `.github/workflows/` but names no "
                        "owner, so the rule assigns no reviewer")
    return "fail", (f"`{rel}` has no entry covering `.github/workflows/`")


# --- workflow-YAML predicates ------------------------------------------------

def _write_scopes_at_workflow_level(doc: dict) -> list[str]:
    """Permission scopes granted WRITE at workflow level, excluding id-token.

    `id-token` is excluded BY CONSTRUCTION: ci-score's
    `ci.security.scoped-id-token` owns that scope, and including it here would
    let one YAML edit move both numbers — the disjointness this module forbids.
    `write-all` fails on its own (it grants every scope, id-token included,
    but the edit that fixes it is not the edit scoped-id-token asks for).
    """
    perms = doc.get("permissions")
    if isinstance(perms, str):
        return ["write-all"] if perms.strip() == "write-all" else []
    if not isinstance(perms, dict):
        return []
    return sorted(
        str(scope) for scope, level in perms.items()
        if str(scope) != "id-token" and str(level).strip() == "write"
    )


def _jobs(doc: dict) -> Iterator[tuple[str, dict]]:
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return
    for name, job in jobs.items():
        if isinstance(job, dict):
            yield str(name), job


def _write_grant_phrase(scopes: list[str]) -> str:
    """How to say a workflow-level write grant in evidence.

    `permissions: write-all` is a shorthand scalar covering every scope, not a
    scope named "write-all" — running it through the per-scope formatter
    printed the nonsense "write-all: write".
    """
    if scopes == ["write-all"]:
        return "`write-all` (every scope)"
    return ", ".join(f"{s}: write" for s in scopes)


def _safe_path(rel: str) -> str:
    """Neutralize an attacker-controlled scanned FILENAME before it is embedded
    in an evidence string.

    Workflow files live at repo-controlled paths, and a filename may legally
    carry backticks or (pathologically) whitespace. These evidence strings land
    in a markdown table cell whose renderer (`report.py:_cell`) collapses
    whitespace and escapes pipes but — by design — leaves backticks alone, since
    the fact-description column uses them for inline code. A raw backtick from a
    filename would unbalance that cell's inline-code spans. Collapsing whitespace
    and swapping the backtick keeps a hostile filename from disturbing the cell,
    the same neutralization the finding bullets apply via `report.py`'s
    `_flatten_scanned`. Structural forgery is already blocked by `_cell`; this
    closes the cosmetic residual so the invariant holds end to end.
    """
    return " ".join(rel.split()).replace("`", "'")


def _capped(items: list[str], cap: int, sep: str = "; ") -> str:
    """Join at most ``cap`` items and say how many were left out.

    A bare "…" told the reader there were more offenders without telling them
    how many — 6 and 60 rendered identically, and a reader cannot size the work
    from an ellipsis.
    """
    shown = sep.join(items[:cap])
    extra = len(items) - cap
    return shown + (f" — and {extra} more" if extra > 0 else "")


def _is_permissions_grant(value: object) -> bool:
    """True only for a value GitHub actually reads as a permissions grant.

    A mapping (empty included — `permissions: {}` explicitly grants nothing,
    which IS a declaration) or one of the two shorthand strings. Everything
    else — most importantly a NULL value, which GitHub treats as omitted so the
    broad default token survives, but equally a typo'd scalar the workflow
    schema rejects outright — is not a declaration and must not earn the fact.
    """
    if isinstance(value, dict):
        return True
    return isinstance(value, str) and value.strip() in ("read-all", "write-all")


def _declares_permissions(doc: dict) -> bool:
    return _permissions_gap(doc) is None


def _permissions_gap(doc: dict) -> str | None:
    """Why this workflow does not declare permissions, or None if it does.

    `permissions:` with a NULL value is treated by GitHub as omitted —
    the workflow keeps the broad default token. Key-presence alone is the naive
    check the skill's own p14_3_null_perms fixture warns about. The rule is the
    same at BOTH levels: a job whose `permissions:` key carries no real grant is
    as undeclared as a workflow's, so presence never suffices.

    The reason is returned rather than a bare False because "no `permissions:`
    block" was printed for files that plainly HAVE one — a reader who opens the
    file sees the key and stops believing the report.
    """
    top = doc.get("permissions")
    if _is_permissions_grant(top):
        return None
    jobs = list(_jobs(doc))
    ungranted = [n for n, job in jobs
                 if not _is_permissions_grant(job.get("permissions"))]
    if jobs and not ungranted:
        return None
    if "permissions" in doc:
        if top is None:
            return "`permissions:` has no value, which GitHub reads as omitted"
        return f"`permissions:` value `{top}` is not a valid grant"
    if not jobs:
        return "no `permissions:` block, and no jobs to carry one"
    if len(ungranted) < len(jobs):
        return ("no workflow-level `permissions:`, and not on every job "
                "(missing on " + _capped(ungranted, 3, ", ") + ")")
    return "no `permissions:` block"


def _blanket_inherit_jobs(doc: dict) -> list[str]:
    return [name for name, job in _jobs(doc)
            if isinstance(job.get("uses"), str)
            and str(job.get("secrets")).strip() == "inherit"]


def _untrusted_triggers_of(scan: ModuleType, doc: dict) -> set[str]:
    on_node = scan._get_on_node(doc)
    names = set(scan._on_trigger_names(on_node)) if on_node is not None else set()
    return names & scan._UNTRUSTED_TRIGGERS


def _jobs_checking_out_attacker_head(scan: ModuleType, doc: dict) -> list[str]:
    """Jobs that check out the attacker's head ref — WITHOUT requiring the
    execution leg. Execution is P14.9's finding; the checkout alone is what
    makes the trigger impossible to clear as inert, which is this fact's tier."""
    out = []
    for name, job in _jobs(doc):
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            if not (isinstance(uses, str) and uses.startswith("actions/checkout")):
                continue
            with_block = step.get("with")
            if isinstance(with_block, dict) and any(
                scan._attacker_head_ref(with_block.get(k))
                for k in ("ref", "repository")
            ):
                out.append(name)
                break
    return out


# Untrusted triggers that carry NO attacker-controllable payload — pure activity
# notifications. `fork`/`watch` fire when someone forks or stars the repo; the
# workflow runs base code in the base context with no attacker text, ref, or
# artifact entering the job. A persisted checkout token therefore cannot be read
# by any attacker-influenced execution (there is none), so persist-credentials is
# not a defense on these events and the credentials-scoped fact must not FAIL on
# them (doing so penalised a config that is not actually exposed, and the reduced
# score propagated into ci-advisor's blend). These stay in the GLOBAL
# `_UNTRUSTED_TRIGGERS` — other checks (e.g. a write-token grant on an untrusted
# trigger) still legitimately care about a fork/watch workflow — so the narrowing
# is local to this one fact. Every other untrusted trigger carries attacker
# content (a PR head, comment/issue/discussion text, a workflow_run artifact, a
# dispatch payload) that, directly or via injection, can reach a persisted token,
# so it stays in scope.
_CREDS_SCOPED_INERT_TRIGGERS = frozenset({"fork", "watch"})


def _unpersisted_checkout_violations(doc: dict) -> list[str]:
    """On an untrusted-trigger workflow, every checkout must set
    persist-credentials: false — GitHub's DEFAULT is to persist the token into
    .git/config, where attacker-influenced later steps can read it."""
    out = []
    for name, job in _jobs(doc):
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            if not (isinstance(uses, str) and uses.startswith("actions/checkout")):
                continue
            with_block = step.get("with")
            persist = (with_block or {}).get("persist-credentials") \
                if isinstance(with_block, dict) else None
            if str(persist).strip().lower() != "false":
                out.append(name)
                break
    return out


# --- F7: required checks that a job can skip ---------------------------------
#
# GitHub counts a SKIPPED required status check as a PASS. So a required check
# produced only by a job that carries an `if:` condition is not a gate at all:
# a pull request that fails to satisfy the condition merges with the check
# green and the suite never run. This repository shipped that bypass and closed
# it with the always-running verdict-job pattern, which is the fix recipe.
#
# TOKEN-GATED, like the impostor-SHA vector. Which checks a branch requires is
# an API question, not a YAML one, so with no repo, no token, or a failed call
# this fact is UNMEASURED with the reason stated — never a pass for being
# unreadable, and never a fail for it either.

# Conditions that cannot skip the job — the WHOLE condition, not a substring
# of it. `always()` runs it in every result state and `!cancelled()` unless the
# run was cancelled, so both keep the check reporting, which is the property
# this fact is about. `success() || failure()` is NOT a third spelling of the
# same thing once the job has `needs:` — see `_NEVER_SKIPS_WITHOUT_NEEDS`.
#
# Matching these as substrings was the defect: `always() && <fork guard>` runs
# in every result state AND only when the guard holds, so it skips exactly like
# the bare guard — which is the bypass this fact exists to catch, with two
# tokens prepended. A condition this list does not recognise is treated as
# skippable, so the failure direction of a mis-parse is a false RED (visible,
# arguable) rather than a false green.
# Conditions that run the job whatever its dependencies did. `always()` and
# `!cancelled()` both still run when a dependency is SKIPPED, which is the
# state that matters here.
_NEVER_SKIPS = {
    "always()",
    "!cancelled()",
    "always()||cancelled()",
}
# These run the job every time ONLY when nothing upstream can skip or fail
# first, so they are never-skipping exactly when the job has no `needs:`.
#
# `success() || failure()` looks like `always()` and is not: if a dependency is
# SKIPPED, neither predicate holds and GitHub skips the job too — and a skipped
# required check is what it reports as passed. Certifying it meant the verdict
# job this fact RECOMMENDS could itself be bypassed, so the recommendation is
# `always()` or `!cancelled()`.
_NEVER_SKIPS_WITHOUT_NEEDS = {
    "success()",
    # A CONSTANT condition is not `always()` either. GitHub skips a dependent
    # when a dependency skips unless the condition is `always()` or
    # `!cancelled()`, and `if: true` is neither — so with `needs:` it is as
    # bypassable as the suite it gates. Without `needs:` it cannot be false,
    # and reding it would be a false RED on a repo that did nothing wrong.
    "true",
    "success()||failure()",
    "failure()||success()",
    "success()||failure()||cancelled()",
}
# `${{ 1 == 1 }}` and friends: a comparison of two identical literals is `true`
# written the long way, and failing it reds a repository that did nothing wrong.
# Deliberately narrow — this evaluates NOTHING, it recognises a constant.
_CONSTANT_TRUE_RE = re.compile(r"^(?:true|(?P<a>'[^']*'|\"[^\"]*\"|\d+)==(?P=a))$")
_EXPRESSION_RE = re.compile(r"\$\{\{")
_EXPRESSION_WRAPPER_RE = re.compile(r"^\$\{\{(.*)\}\}$", re.S)


def _normalise_condition(condition: str) -> str:
    """`condition` with its `${{ }}` wrapper and all whitespace removed.

    `if: ${{ always() }}` is the spelling GitHub's own documentation shows, and
    `success()||failure()` is written both with and without spaces. Comparing
    the raw text made those false REDS against repositories that implemented
    the recommended fix correctly.
    """
    text = condition.strip()
    wrapped = _EXPRESSION_WRAPPER_RE.match(text)
    if wrapped:
        text = wrapped.group(1).strip()
    return "".join(text.split())


def _never_skips(condition: str, has_needs: bool = True) -> bool:
    text = _normalise_condition(condition)
    if text in _NEVER_SKIPS:
        return True
    if has_needs:
        return False
    return text in _NEVER_SKIPS_WITHOUT_NEEDS or bool(
        _CONSTANT_TRUE_RE.match(text))


def _condition_text(job: dict) -> str | None:
    cond = job.get("if")
    if cond is None or isinstance(cond, (dict, list)):
        return None
    # The evidence quotes the reader's own file back at them, so a YAML
    # boolean has to read as YAML: `if: true`, never Python's `True`.
    text = ("true" if cond is True else "false" if cond is False
            else " ".join(str(cond).split()))
    return text or None


def _needs_of(job: dict) -> list[str]:
    needs = job.get("needs")
    if isinstance(needs, str):
        return [needs]
    if isinstance(needs, list):
        return [str(n) for n in needs if isinstance(n, (str, int))]
    return []


def _remember(memo: dict[str, str | None], key: str,
              answer: str | None) -> str | None:
    """Cache an answer that depends only on the job, and return it."""
    memo[key] = answer
    return answer


class _Unknown(str):
    """A skip answer the scan could not determine, carrying its reason.

    A subclass of `str` so it reads like the reason strings beside it, and a
    distinct TYPE so "I don't know" can never be mistaken for either "always
    runs" (`None`) or "can skip" (a plain reason). It used to be the former,
    which is the direction that hides a defect.
    """


def _skip_path(jobs: dict[str, dict], key: str,
               seen: frozenset[str] = frozenset(),
               memo: dict[str, str | None] | None = None) -> str | None:
    """Why this job can be skipped, `None` if it always runs, or `_Unknown`.

    Two ways a job skips: its own `if:` evaluates false, or a job it `needs:`
    skips (GitHub skips the dependents). Both report the check as skipped, and
    a skipped required check is green — so both are the same defect here.

    A never-skipping condition (`always()`, `!cancelled()`) stops the walk in
    BOTH directions: such a job
    runs whatever its dependencies did, which is exactly what makes the
    verdict-job pattern work. Recursion is cycle-guarded (`seen`) because a
    malformed `needs:` cycle must not hang the scan; a cycle is unresolvable
    rather than safe, and says so.

    MEMOIZED, because a branching graph reaches the same job down every path
    and the walk is otherwise exponential in depth: a 12-level graph two jobs
    wide took 8,191 visits where 47 suffice. A cycle answer is NOT cached — it
    depends on the path taken to get there, unlike every other answer, which
    depends only on the job.
    """
    if memo is None:
        memo = {}
    if key in memo:
        return memo[key]
    if key in seen:
        return _Unknown(f"`{key}` is part of a `needs:` cycle")   # never cached
    if key not in jobs:
        return _remember(memo, key, _Unknown(
            f"`{key}` is not a job in this workflow, so whether it runs "
            f"cannot be determined here"))
    job = jobs[key]
    needs = _needs_of(job)
    condition = _condition_text(job)
    if condition and _never_skips(condition, bool(needs)):
        return _remember(memo, key, None)
    if condition:
        return _remember(memo, key, f"`{key}` carries `if: {condition}`")
    # No condition of its own. A job with `needs:` is SKIPPED whenever a job it
    # needs fails or skips — which is precisely what GitHub reports as a passed
    # check. So the walk does not go looking for an always-running ancestor:
    # there is no arrangement of ancestors that makes a bare dependent
    # unskippable, and saying otherwise described the opposite of what it does.
    # The producer has to carry `always()` or `!cancelled()` ITSELF — which is
    # what the fix recipe says. (`success() || failure()` does not qualify
    # here: a skipped dependency makes both predicates false.)
    if needs:
        # Each dependency is walked ONCE. Testing the result's type and then
        # recomputing it doubled the work at every link, so a linear `needs:`
        # chain ending in an unreadable job cost 2^depth — 3.4s at depth 22,
        # a quarter of an hour by depth 30 — which is exactly the hang the
        # cycle guard above promises cannot happen.
        cycle = next(
            (walked for walked in
             (_skip_path(jobs, n, seen | {key}, memo) for n in needs)
             if isinstance(walked, _Unknown)),
            None,
        )
        if cycle is not None:
            return _Unknown(f"`{key}` needs {cycle}")
        return _remember(
            memo, key,
            f"`{key}` needs {_capped([f'`{n}`' for n in needs], 3, ', ')} "
            f"and carries no condition of its own, so it is skipped "
            f"whenever one of them fails or skips")
    return _remember(memo, key, None)


# Which workflows can report a check on a pull request at all.
#
# NOT a skip rule. Path and branch filtering does the OPPOSITE of what this
# fact once assumed: a workflow those filters skip never reports its check, so
# a required check stays PENDING and the pull request cannot merge. Only a
# skipped JOB reports Success — that is the whole bypass, and it is an `if:`.
# Treating a filtered workflow as a bypass failed repositories whose merges
# were already blocked, while GitHub's recommended workaround for exactly that
# situation — an always-succeeding stub job carrying the same name — passed.
# (`pull_request.branches` filters the BASE branch, which is the branch whose
# protection was just read, so every PR this fact gates is inside the filter.)
#
# What the trigger DOES decide is whether a job is a producer of the context at
# all. Producers are matched by display name across every workflow, so a `test`
# job in a tag-only release workflow was accepted as an always-running producer
# and vetoed the real, gated one.
_PR_TRIGGERS = ("pull_request", "pull_request_target")
_TAG_ONLY_KEYS = ("tags", "tags-ignore")


def _on_mapping(doc: dict) -> dict | None:
    """The workflow's `on:` block as a mapping, or None when unreadable.

    `on` is a YAML 1.1 boolean, so the key parses as `True` — read it the way
    the rest of this scanner does rather than by literal name.
    """
    on = _scan()._get_on_node(doc) if isinstance(doc, dict) else None
    if on is None or on is True:
        return None
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {str(k): None for k in on}
    return on if isinstance(on, dict) else None


# Whether a workflow's jobs can report a status check on a pull request is
# THREE-valued, and collapsing it to a boolean cost a verdict in each direction.
#
#   YES      `pull_request` / `pull_request_target`; a plain `push`; a push
#            whose branch filter matches every branch.
#   NO       provably cannot — a push restricted to TAGS. No pull-request
#            branch push matches it.
#   UNKNOWN  a push filtered to specific branches or paths. Whether it runs on
#            a given pull request's head branch is not knowable from the YAML.
#
# UNKNOWN is not NO. Treating it as NO dropped the producer entirely, and when
# it was the only one the fact went UNMEASURED — which scores nothing, while a
# fail scores zero, so a repository with a genuinely bypassable required check
# came out ahead of one that configured protection properly. That is the same
# "unmeasurable beats failing" lever this fact exists to close, moved into the
# producer arm. Treating UNKNOWN as YES is the other error: a `deploy.yml`
# filtered to `branches: [main]` then certified a check it may never report.
#
# So an UNKNOWN producer participates — its skip analysis still counts against
# the check — but it can never be the evidence that a check always reports.
_ABILITY_YES, _ABILITY_NO, _ABILITY_UNKNOWN = "yes", "no", "unknown"
_MATCH_ALL_BRANCH_PATTERNS = {"**", "*", "'**'", '"**"'}


def _matches_every_branch(patterns: Any) -> bool:
    """Does this branch filter match every branch a pull request could use?

    A `!` pattern anywhere in the list means it does not. GitHub does not
    accept `branches` and `branches-ignore` for the same event, so a negated
    pattern INSIDE `branches:` is the documented — and only — way to write
    "everything except", which makes it the shape that actually appears in
    workflows. Reading the `'**'` beside it and ignoring the exclusion
    certified a required check that never reports on an excluded branch.
    """
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not patterns:
        return False
    cleaned = [str(p).strip() for p in patterns]
    if any(p.startswith("!") for p in cleaned):
        return False
    return any(p in _MATCH_ALL_BRANCH_PATTERNS for p in cleaned)


def _pr_reporting_ability(doc: dict) -> str:
    """`yes` / `no` / `unknown` — see the comment above."""
    on = _on_mapping(doc)
    if on is None:
        return _ABILITY_YES          # unreadable `on:`: never silently dropped
    if any(t in on for t in _PR_TRIGGERS):
        return _ABILITY_YES
    if "push" not in on:
        # `workflow_dispatch`, `workflow_call`, `schedule` report nothing on a
        # pull request.
        return _ABILITY_NO
    push = on.get("push")
    if not isinstance(push, dict):
        return _ABILITY_YES          # `on: push` with no filters at all
    branches = push.get("branches")
    # ANY other filter is a reason the push may not run, whatever the branch
    # pattern says — so all of them have to be read before the match-all
    # shortcut answers. Reading `branches: ['**']` first certified a required
    # check that a pull request touching nothing under `paths:` never causes to
    # report, while the real producer skipped: the check greened with neither
    # job having run. A `branches-ignore:` alongside `branches:` is the same
    # problem in the other spelling — the two contradict each other, and
    # "runs on every push" is not a claim this scan can make about the pair.
    also_filtered = bool(push.get("paths") or push.get("paths-ignore")
                         or push.get("branches-ignore"))
    if branches and _matches_every_branch(branches) and not also_filtered:
        return _ABILITY_YES          # `branches: ['**']` runs on every push
    if any(push.get(k) for k in _TAG_ONLY_KEYS) and not (
            branches or push.get("branches-ignore")):
        return _ABILITY_NO           # tags only: provably cannot
    if branches or push.get("paths") or push.get("paths-ignore") \
            or push.get("branches-ignore"):
        return _ABILITY_UNKNOWN
    return _ABILITY_YES


def _display_name(key: str, job: dict) -> str:
    name = job.get("name")
    return " ".join(str(name).split()) if isinstance(name, str) and name.strip() else key


def _context_producers(
    docs: list[tuple[str, dict]], context: str,
) -> list[tuple[str, str, dict, dict[str, dict], dict, str]]:
    """(workflow, job key, job, sibling jobs, workflow doc, PR-reporting
    ability) for every job that could report `context` as a status check.

    A check context is the job's DISPLAY name — its `name:` if it has one, else
    its key — and a matrix job expands to `name (value, value)`. Both spellings
    are matched; a display name built from an expression (`name: test ${{ … }}`)
    is not matched at all, because what it renders to is not knowable here.
    """
    out = []
    for rel, doc in docs:
        ability = _pr_reporting_ability(doc)
        if ability == _ABILITY_NO:
            continue
        jobs = {k: j for k, j in _jobs(doc)}
        for key, job in jobs.items():
            shown = _display_name(key, job)
            if _EXPRESSION_RE.search(shown):
                continue
            if context == shown and not _job_has_matrix(job):
                # A MATRIX job never reports its bare name — GitHub appends the
                # combination, so `test` becomes `test (3.11)`. Matching the
                # exact display name first certified a required context that
                # nothing emits, which read as a measured pass on a branch
                # whose required check can never report.
                out.append((rel, key, job, jobs, doc, ability))
            elif context.startswith(shown + " (") and _matrix_produces(
                    job, context[len(shown) + 2:].rstrip(")")):
                out.append((rel, key, job, jobs, doc, ability))
    return out


_MATRIX_META = {"include", "exclude"}


def _job_has_matrix(job: dict) -> bool:
    """Does this job expand into `name (combination)` check contexts?

    True whenever a matrix is present at all — including one this scan cannot
    enumerate (`include:`, a computed value). Enumerability decides which
    EXPANSIONS match; it does not change the fact that the bare name is not one
    of them.
    """
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    return bool(matrix) if not isinstance(matrix, (str, int, float)) else True


def _matrix_expansions(job: dict) -> set[str] | None:
    """Every `(…)` suffix this job's matrix can actually render, or None when
    the matrix is not knowable from the YAML.

    Only a matrix expands a job into `name (value, …)` contexts; it expands
    into its own values; and it expands into the COMBINATIONS it can really
    run, joined in the order its axes are declared. All three clauses matter,
    and each one was learned from a false green in the same family:

      * offered to every job, an always-running verdict job named `test` stood
        in as the producer of `test (self-hosted)` — this repository's own CI
        shape, where the job added to CLOSE the bypass was what hid it;
      * offered to any matrix, a job named `test` running over `3.11`/`3.12`
        stood in for that same unrelated context;
      * matched against a FLATTENED value set, a job over
        `os: [self-hosted, ubuntu]` stood in for it again — a set says yes to
        any tokens appearing anywhere, including a single value from a
        two-axis matrix, a reordered pair, and a combination `exclude:`
        removes.

    `include:` is not enumerated: it can add combinations, rename axes, and
    extend existing ones, and guessing its rendering is how the three defects
    above happened. An unknowable matrix produces NO match, which leaves the
    context disclosed as not judged; a wrong match is a silent pass.
    """
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict) or not matrix:
        return None
    if matrix.get("include"):
        return None
    axes: list[list[str]] = []
    names: list[str] = []
    for key, node in matrix.items():
        if key in _MATRIX_META:
            continue
        if not isinstance(node, list) or not node:
            return None
        values: list[str] = []
        for entry in node:
            if isinstance(entry, (dict, list)):
                return None                      # nested shape: not knowable
            text = str(entry).strip()
            if _EXPRESSION_RE.search(text):
                return None                      # computed: not knowable
            values.append(text)
        names.append(key)
        axes.append(values)
    if not axes:
        return None

    excluded = []
    for entry in matrix.get("exclude") or []:
        if not isinstance(entry, dict):
            return None
        excluded.append({str(k): str(v).strip() for k, v in entry.items()})

    out: set[str] = set()
    for combo in itertools.product(*axes):
        assignment = dict(zip(names, combo))
        if any(all(assignment.get(k) == v for k, v in rule.items())
               for rule in excluded):
            continue
        out.add(", ".join(combo))
    return out or None


def _matrix_produces(job: dict, suffix: str) -> bool:
    """Could this job's matrix render the `(…)` half of a check context?"""
    expansions = _matrix_expansions(job)
    if expansions is None:
        return False
    return " ".join(suffix.split()) in expansions


# A 403 from the admin-only classic endpoint is ORDINARY — most readers of this
# fact are auditing a repository they do not administer. Any other failure
# (rate limit, timeout, 5xx, malformed body) is not ordinary, and must not be
# laundered into the same "expected" bucket.
_ADMIN_ONLY_403_RE = re.compile(r"\b403\b|must have admin", re.I)


def _is_admin_only_403(message: str) -> bool:
    return bool(_ADMIN_ONLY_403_RE.search(message))


# `404 Branch not protected` from the classic protection endpoint. Matched on
# the status AND the reason — which the comment used to claim while the regex
# was a bare `\b404\b`, so a branch renamed between the two calls, a
# plan-gated endpoint, even a 502 quoting 404 in its body, all read as
# "nothing required here": a measured PASS over a source never read.
_NOT_PROTECTED_404_RE = re.compile(r"\b404\b.*branch not protected", re.I | re.S)


def _is_not_protected_404(exc: Exception) -> bool:
    return bool(_NOT_PROTECTED_404_RE.search(str(exc)))


def _required_contexts_via_gh(repo: str) -> tuple[list[str] | None, str]:
    """(contexts, detail) from the GitHub API, or (None, reason).

    TWO sources, unioned, because either can be the one a repository uses and
    reading only one would report a protected branch as unprotected: the
    rulesets endpoint (readable with repo read access) and the classic branch
    protection endpoint (admin-only).

    The completeness question is therefore per-SOURCE. Each call records its own
    success flag (`rulesets_ok` / `classic_ok`) and appends its own message to
    `errors`, and the guard sits OUTSIDE both calls, on those flags — not inside
    either `except`, where it could only see one source's fate:

      * both sources read — return the union, which may legitimately be empty
        (no required checks configured, which the fact scores as a pass);
      * neither read — return `None` with "could not be read at all", listing
        both errors;
      * exactly one read — return the union ONLY when both of these hold: the
        source that failed did so the ORDINARY way (`_is_admin_only_403` over
        the recorded message: a 403, or "must have admin" — most readers of
        this fact do not administer the repository they are auditing), AND the
        source that answered actually found contexts. Otherwise return `None`
        with the "only part of branch protection ... could be read" reason,
        because a required check configured in the unread source would be
        invisible here.

    Both halves of that last condition are load-bearing. A non-403 failure
    (rate limit, timeout, 5xx, malformed body) is not evidence that the unread
    source is empty, so a partial set must not be returned as complete; and an
    EMPTY answer from the one source that worked establishes nothing at all
    about the other — returning it would score "no required checks" as a pass
    over a source the scan never saw. Anything returned as a pass counts toward
    `passed` and stays out of `unmeasured`, so a consumer blends it as clean AND
    fully measured. That is the "unread is not clean" failure this fact exists
    to prevent, and returning `None` is how it is avoided.
    """
    gh = _gh_utils()
    if not gh.check_prereqs():
        return None, "gh is unavailable or not authenticated (run gh auth login)"
    try:
        branch = json.loads(gh.run_gh_api(f"repos/{repo}")).get("default_branch")
    except Exception as exc:                                   # noqa: BLE001
        return None, f"the repository's default branch could not be read ({exc})"
    if not branch:
        return None, "the repository reported no default branch"

    contexts: set[str] = set()
    errors: list[str] = []
    rulesets_ok = classic_ok = False
    try:
        rules = json.loads(gh.run_gh_api(f"repos/{repo}/rules/branches/{branch}"))
        if not isinstance(rules, list):
            # An error object, a paginated envelope, anything but the documented
            # array. Skipping it and still marking the source READ made an
            # unexpected payload mean "this branch requires nothing" — while the
            # classic arm treats an unexpected shape as unread. Two sources that
            # fail differently let the asymmetry decide the verdict.
            raise ValueError(
                f"the rulesets endpoint returned {type(rules).__name__}, not "
                f"the documented array of rules")
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                continue
            params = rule.get("parameters") or {}
            for check in params.get("required_status_checks") or []:
                if isinstance(check, dict) and check.get("context"):
                    contexts.add(str(check["context"]))
        rulesets_ok = True
    except Exception as exc:                                   # noqa: BLE001
        errors.append(f"rulesets: {exc}")
    try:
        classic = json.loads(gh.run_gh_api(
            f"repos/{repo}/branches/{branch}/protection/required_status_checks",
            quiet_not_found=True))
        for context in classic.get("contexts") or []:
            contexts.add(str(context))
        for check in classic.get("checks") or []:
            if isinstance(check, dict) and check.get("context"):
                contexts.add(str(check["context"]))
        classic_ok = True
    except Exception as exc:                                   # noqa: BLE001
        if _is_not_protected_404(exc):
            # THIS endpoint answers 404 "Branch not protected" when classic
            # protection is not configured — the normal state of every
            # repository that uses rulesets, which is the population this fact
            # was built for. That is an ANSWER ("nothing required here"), not a
            # failure to read, and treating it as unread made the fact
            # unmeasurable for all of them: a repo with a genuinely bypassable
            # check then scored HIGHER than the same repo with classic
            # protection configured empty, because an unmeasured fact scores
            # nothing while a fail scores zero.
            #
            # Scoped to this arm deliberately. A 404 from the repository or
            # rulesets endpoint is a missing repository or a mistyped path, and
            # accepting it here would read every repository as unprotected —
            # `test_the_branch_protection_fetcher_asks_for_the_two_documented
            # _endpoints` asserts the requested paths literally for that reason.
            classic_ok = True
        else:
            errors.append(f"classic branch protection: {exc}")

    if rulesets_ok and classic_ok:
        return sorted(contexts), f"branch `{branch}`"

    detail = "; ".join(errors)
    if not (rulesets_ok or classic_ok):
        return None, (f"branch protection for `{branch}` could not be read "
                      f"at all ({detail})")
    # One source answered. That is enough ONLY when the other failed the
    # ordinary way — the admin-only 403 — and the one that answered found
    # something. An empty answer from one source plus no answer from the other
    # establishes nothing at all.
    if not _is_admin_only_403(errors[-1]) or not contexts:
        return None, (
            f"only part of branch protection for `{branch}` could be read "
            f"({detail}) — a required check configured in the unread "
            f"source would be invisible to this scan, so whether one can "
            f"be bypassed was not established")
    # Measured, but from ONE source. The reader is told which one was not read,
    # because a check configured only there is invisible and the count of
    # required checks the evidence quotes is therefore a floor, not a total.
    # Both halves come from which source actually succeeded. Hardcoding
    # "read from rulesets only" and cutting the unread name out of the error
    # list told the reader, when RULESETS was the arm that 403'd, that rulesets
    # — the source that ANSWERED — could not be read.
    read, unread = ("rulesets", "classic branch protection") if rulesets_ok \
        else ("classic branch protection", "rulesets")
    return sorted(contexts), (
        f"branch `{branch}` (read from {read} only — {unread} could not be "
        f"read, so a check configured only there is not counted here)")


def _matrix_near_miss(docs: list[tuple[str, dict]], context: str) -> str | None:
    """A matrix job whose NAME is this context, or None.

    Reported instead of the generic "no job reports it" so a reader is not sent
    hunting for an external app that is not there: the job is right in front of
    them, and the mismatch is that branch protection names the bare context
    while the job only ever emits expansions of it.
    """
    for rel, doc in docs:
        for key, job in _jobs(doc):
            shown = _display_name(key, job)
            if _EXPRESSION_RE.search(shown):
                # A templated display name is not knowable, which is why the
                # producer match skips it; the near miss has to skip it too or
                # it would name a job as the cause on a guess.
                continue
            if shown == context and _job_has_matrix(job):
                return (f"{rel}: the matrix job `{key}` produces "
                        f"`{context} (…)` expansions, never the bare context, "
                        f"so nothing reports `{context}` — require one of its "
                        f"expansions, or a job without a matrix")
    return None


def _required_checks_skippable(
    docs: list[tuple[str, dict]],
    repo: str | None,
    fetcher,
) -> tuple[str, str]:
    """(outcome, evidence) for `sec.required-checks.skippable`."""
    if not repo:
        return "unmeasured", (
            "unmeasured: which status checks the branch requires is an API "
            "fact, and this scan had no repository to read it from — that "
            "needs `gh` authenticated (`gh auth login`) and a GitHub remote "
            "to derive `owner/name` from")
    try:
        contexts, detail = fetcher(repo)
    except Exception as exc:                                   # noqa: BLE001
        return "unmeasured", (f"unmeasured: branch protection could not be "
                              f"read ({exc})")
    if contexts is None:
        return "unmeasured", f"unmeasured: {detail}"
    if not contexts:
        return "pass", (
            f"{detail} requires no status check, so no required check can be "
            "bypassed by a skipped job (whether the branch SHOULD require one "
            "is a different question, not this fact's)")

    # Three outcomes per context, never two: GATED (some producer always runs),
    # BYPASSABLE (every producer can skip), or UNJUDGED (nothing here produces
    # it, or a producer's skip walk had no answer). Collapsing UNJUDGED into
    # GATED is what let a green row claim "every required check is produced by
    # a job that always runs" about checks it had never looked at.
    bypassable: list[str] = []
    unjudged: list[str] = []
    gated = 0
    for context in contexts:
        producers = _context_producers(docs, context)
        if not producers:
            near = _matrix_near_miss(docs, context)
            unjudged.append(
                f"`{context}` ({near})" if near else
                f"`{context}` (no job in these workflows reports it — an "
                f"external app check, a reusable-workflow job, a templated "
                f"job name, or a stale entry)")
            continue
        skips: list[str] = []
        unknown: list[str] = []
        always_runs = False
        for rel, key, _job, jobs, doc, ability in producers:
            reason = _skip_path(jobs, key)
            if reason is None:
                if ability == _ABILITY_YES:
                    always_runs = True
                    break
                # It never skips, but whether this workflow runs on a pull
                # request at all is not knowable — so it cannot certify the
                # check. Recorded as unknown rather than dropped.
                unknown.append(
                    f"`{context}` ← {rel}: always runs, but its trigger "
                    f"filters may not match a pull request's head branch")
                continue
            if isinstance(reason, _Unknown):
                unknown.append(f"`{context}` ← {rel}: {reason}")
                continue
            skips.append(f"`{context}` ← {rel}: {reason}")
        if always_runs:
            gated += 1
        elif skips:
            # A producer that demonstrably can skip is a finding whatever a
            # second, unreadable producer might have done. Checking `unknown`
            # first downgraded a real fail to "not judged".
            bypassable.extend(skips[:1])
        elif unknown:
            unjudged.extend(unknown[:1])

    tail = ((" Not judged: " + _capped(unjudged, 4, "; ") + ".")
            if unjudged else "")
    if bypassable:
        return "fail", (
            "GitHub reports a SKIPPED required check as passed, and "
            + _capped(bypassable, 3)
            + " — so nothing in these workflows is guaranteed to report it."
            + tail)
    if unjudged:
        # A pass is a claim about EVERY required check. The ordinary shape of a
        # mature repository is a dozen required contexts with most coming from
        # external apps, and returning `pass` off the one that could be traced
        # made the machine outcome say "no required check can be bypassed"
        # about all the others — while counting toward `passed` and staying out
        # of `unmeasured`, so no caveat fired either.
        traced = (f"{gated} of {len(contexts)} required check(s) could be "
                  f"traced to a job in these workflows, and "
                  f"{'that one is' if gated == 1 else 'those are'} gated"
                  if gated else
                  f"none of the {len(contexts)} required check(s) could be "
                  f"traced to a job in these workflows")
        return "unmeasured", (
            f"unmeasured: {traced}, so whether every required check on {detail} "
            f"survives a skipped job was not established." + tail)
    return "pass", (
        f"all {gated} required check(s) on {detail} have a producer that runs "
        f"whatever else happens.")


# --- F8: fork-PR CI approval that gates nobody real --------------------------
#
# GitHub's fork-PR approval policy decides whose pull request can start
# workflows without a maintainer approving the run. The API's documented enum
# (verified against `repos/{owner}/{repo}/actions/permissions/
# fork-pr-contributor-approval`, and against a live repository's response):
#
#   first_time_contributors_new_to_github  approval only for accounts NEW TO
#                                          GITHUB  -> the weakest tier
#   first_time_contributors                approval for anyone who has not
#                                          contributed to THIS repo (default)
#   all_external_contributors              approval for every outside account
#
# Only the weakest tier fails. Requiring approval from first-time contributors
# to the repo is a legitimate trust judgment — a maintainer who chose it is not
# misconfigured — and this fact never dings it. What it does ding is a gate
# that is on while gating nobody real: an attacker registers an account, lets
# it age, and runs workflows on the repository unapproved.
#
# HONEST ABOUT THE STAKES: this is hygiene, not an exploit chain. A fork PR's
# workflows still get a read-only token and no secrets, so the risk is compute
# abuse under the repository's name and quiet iteration — an attacker probing
# the CI surface, or mining on it, without a maintainer ever seeing a run
# waiting for approval. It is not a path to your secrets or your write token.
#
# A value outside the enum is a future GitHub setting, and this fact says so
# rather than inventing a verdict for it.
_FORK_APPROVAL_FAIL = {"first_time_contributors_new_to_github"}
_FORK_APPROVAL_PASS = {"first_time_contributors", "all_external_contributors"}


def _fork_approval_via_gh(repo: str) -> tuple[str | None, str]:
    """(approval_policy, detail) from the API, or (None, reason)."""
    gh = _gh_utils()
    if not gh.check_prereqs():
        return None, "gh is unavailable or not authenticated (run gh auth login)"
    try:
        body = json.loads(gh.run_gh_api(
            f"repos/{repo}/actions/permissions/fork-pr-contributor-approval",
            quiet_not_found=True))
    except Exception as exc:                                   # noqa: BLE001
        return None, f"the fork-PR approval policy could not be read ({exc})"
    policy = body.get("approval_policy") if isinstance(body, dict) else None
    if not policy:
        return None, ("the fork-PR approval endpoint returned no "
                      "`approval_policy` value")
    return str(policy), "the repository's Actions settings"


def _fork_approval_effective(repo: str | None, fetcher) -> tuple[str, str]:
    """(outcome, evidence) for `sec.fork-approval.effective`."""
    if not repo:
        return "unmeasured", (
            "unmeasured: the fork-PR approval policy is a repository setting "
            "read over the API, and this scan had no repository to read it "
            "from — that needs `gh` authenticated (`gh auth login`) and a "
            "GitHub remote to derive `owner/name` from")
    try:
        policy, detail = fetcher(repo)
    except Exception as exc:                                   # noqa: BLE001
        return "unmeasured", (f"unmeasured: the fork-PR approval policy could "
                              f"not be read ({exc})")
    if policy is None:
        return "unmeasured", f"unmeasured: {detail}"
    if policy in _FORK_APPROVAL_FAIL:
        return "fail", (
            f"{detail} set fork-PR workflow approval to "
            f"`{policy}` — approval is required only from accounts new to "
            "GITHUB, so any outside account old enough runs this repository's "
            "workflows with no maintainer approval. Fork runs still carry no "
            "secrets and a read-only token; what an unapproved run buys an "
            "attacker is compute under your repository's name and quiet "
            "iteration against your CI surface")
    if policy in _FORK_APPROVAL_PASS:
        # One sentence per tier: `all_external_contributors` gates EVERY
        # outside account, contributor or not, and describing it with
        # `first_time_contributors`' wording understated the reader's own
        # setting while stating something false about what GitHub does.
        gated = (
            "every outside account, whether or not it has contributed here"
            if policy == "all_external_contributors"
            else "outside accounts that have not contributed here before")
        return "pass", (
            f"fork-PR workflow approval is `{policy}`, which gates {gated}")
    return "unmeasured", (
        f"unmeasured: `{policy}` is not a value this check's enum recognises "
        "(GitHub documents `first_time_contributors_new_to_github`, "
        "`first_time_contributors`, `all_external_contributors`), so whether "
        "it gates anyone is not something this scan can say")


# --- the fact table -----------------------------------------------------------

def compute_config_facts(
    root: Path,
    workflow_files: list[Path],
    scan_incomplete: list[dict[str, str]],
    repo: str | None = None,
    required_contexts_fetcher: Any = None,
    fork_approval_fetcher: Any = None,
) -> dict[str, Any]:
    """Every fact, every time — pass/fail/unmeasured, never silently absent.

    ``repo`` (``owner/name``) enables the TWO facts that need the API —
    ``sec.required-checks.skippable`` (branch protection) and
    ``sec.fork-approval.effective`` (the repository's fork-PR approval setting).
    It is optional, and its absence is disclosed as UNMEASURED rather than
    skipped — a fact that quietly vanishes from the table is a silent pass.

    ``required_contexts_fetcher`` and ``fork_approval_fetcher`` are the seams
    the tests inject a recorded response through, one per API-gated fact,
    defaulting to ``_required_contexts_via_gh`` and ``_fork_approval_via_gh``;
    nothing in the suite touches the network.
    """
    scan = _scan()

    docs: list[tuple[str, dict]] = []
    for wf in workflow_files:
        text = scan._read_text_safe(wf)
        # quiet: the scan pass already reported (and recorded as a coverage
        # gap) every file that will not parse.
        doc = scan._parse_yaml_text(text, wf, quiet=True) if text else None
        if isinstance(doc, dict):
            docs.append((_safe_path(str(wf.relative_to(root))), doc))

    # Universal facts cannot be asserted over a partially readable set: a
    # workflow we could not parse could be the one that fails them.
    gap = bool(scan_incomplete)
    gap_reason = ("unmeasured: %d workflow file(s) could not be scanned "
                  "(%s), and this fact is a claim about every workflow"
                  % (len(scan_incomplete),
                     _capped([_safe_path(g["workflow_file"])
                              for g in scan_incomplete],
                             3, ", "))) if gap else ""

    facts: list[dict[str, Any]] = []

    def add(fact_id: str, fact: str, workflow_scoped: bool,
            passed: bool, evidence: str, outcome: str | None = None) -> None:
        if workflow_scoped and gap:
            facts.append({"fact_id": fact_id, "fact": fact,
                          "outcome": "unmeasured", "evidence": gap_reason})
            return
        # `outcome` lets a fact that computes its OWN three-state result (the
        # CODEOWNERS fact, which can be unmeasured for reasons unrelated to a
        # workflow scan gap) carry it through instead of being flattened to
        # pass/fail here.
        facts.append({"fact_id": fact_id, "fact": fact,
                      "outcome": outcome or ("pass" if passed else "fail"),
                      "evidence": evidence})

    undeclared = [(rel, gap_why) for rel, doc in docs
                  if (gap_why := _permissions_gap(doc)) is not None]
    add("sec.permissions.workflow-declares",
        "every workflow declares `permissions:` (top level, or on every job)",
        True, not undeclared,
        ("all %d workflow(s) declare permissions" % len(docs)) if not undeclared
        # Each file states WHY it failed: a blanket "no permissions: block in"
        # was printed over files that do have one (null value, invalid scalar,
        # or a grant on only some jobs), and a reader who opened the file
        # stopped believing the report.
        else _capped([f"{rel}: {why}" for rel, why in undeclared], 5))

    wide = [(rel, scopes) for rel, doc in docs
            if (scopes := _write_scopes_at_workflow_level(doc))]
    add("sec.permissions.write-scoped",
        "no workflow-level write permission other than id-token "
        "(writes belong on the jobs that need them)",
        True, not wide,
        "no workflow-level write grants (id-token is excluded by "
        "construction; ci-score's scoped-id-token owns that scope)" if not wide
        # `permissions: write-all` is a shorthand SCALAR, not a scope name:
        # rendering it through the scope formatter produced "write-all: write".
        else _capped([f"{rel}: {_write_grant_phrase(s)}" for rel, s in wide], 4))

    co_outcome, co_evidence = _codeowners_covers_workflows(root)
    add("sec.codeowners.workflows",
        "a CODEOWNERS entry covers `.github/workflows/`",
        False, co_outcome == "pass", co_evidence, outcome=co_outcome)

    uncleared = []
    for rel, doc in docs:
        trigs = _untrusted_triggers_of(scan, doc)
        if not trigs:
            continue
        jobs = _jobs_checking_out_attacker_head(scan, doc)
        if jobs:
            uncleared.append(f"{rel} (`{sorted(trigs)[0]}` + head checkout in "
                             f"job(s) {_capped(jobs, 3, ', ')})")
    add("sec.trigger.fork-code-uncleared",
        "no untrusted-trigger workflow checks out the attacker's head ref "
        "(a bare untrusted trigger passes; the full trigger, checkout and "
        "execute chain is reported separately as a fork-code-execution "
        "finding, not as a hygiene check here)",
        True, not uncleared,
        "every untrusted trigger is clear of attacker-head checkouts"
        if not uncleared else _capped(uncleared, 4))

    inherit = [(rel, jobs) for rel, doc in docs
               if (jobs := _blanket_inherit_jobs(doc))]
    add("sec.secrets.no-blanket-inherit",
        "no reusable-workflow call passes `secrets: inherit`",
        True, not inherit,
        "no blanket secret inheritance" if not inherit
        else _capped([f"{rel}: job(s) {_capped(j, 3, ', ')}"
                      for rel, j in inherit], 4))

    persist = []
    for rel, doc in docs:
        # Scope to triggers that can actually expose a persisted token: exclude
        # the payload-less notification events (`fork`/`watch`) that carry no
        # attacker-influenced execution. A workflow with a real untrusted trigger
        # AND fork/watch still applies (the subtraction leaves the real one).
        if not (_untrusted_triggers_of(scan, doc) - _CREDS_SCOPED_INERT_TRIGGERS):
            continue
        jobs = _unpersisted_checkout_violations(doc)
        if jobs:
            persist.append(f"{rel}: job(s) {_capped(jobs, 3, ', ')}")
    # Not computed at all when a workflow could not be scanned: `add` would
    # discard the answer for the gap reason anyway (rightly — the unreadable
    # workflow could be the one holding an always-running producer), and
    # computing it first spent two or three `gh api` round-trips to reach a
    # verdict nothing would read.
    rc_outcome, rc_evidence = (
        ("unmeasured", gap_reason) if gap else _required_checks_skippable(
            docs, repo, required_contexts_fetcher or _required_contexts_via_gh))
    add("sec.required-checks.skippable",
        "every required status check is produced by a job that always runs "
        "(GitHub counts a SKIPPED required check as a pass, so a check only a "
        "conditional job reports can be satisfied without running)",
        True, rc_outcome == "pass", rc_evidence, outcome=rc_outcome)

    fa_outcome, fa_evidence = _fork_approval_effective(
        repo, fork_approval_fetcher or _fork_approval_via_gh)
    add("sec.fork-approval.effective",
        "fork-PR workflow approval gates more than accounts new to GitHub "
        "(the weakest setting lets any aged outside account start CI "
        "unapproved; requiring approval from first-time contributors to this "
        "repo passes)",
        False, fa_outcome == "pass", fa_evidence, outcome=fa_outcome)

    add("sec.checkout.credentials-scoped",
        "on untrusted-trigger workflows, every checkout sets "
        "persist-credentials: false (GitHub's default persists the token into "
        ".git/config where later steps can read it)",
        True, not persist,
        "no untrusted-trigger checkout persists credentials"
        if not persist else _capped(persist, 4))

    return facts_to_score(facts)


def facts_to_score(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """The REGISTERED rule: 100 x passed / scored, no weights.

    Mirrors ci-speedup's speed_score semantics exactly: an unmeasured fact
    scores nothing and STAYS in the applicable count as a visible coverage
    gap; the ratio is over facts that actually resolved. Nothing here is ever
    a silent pass — `scored < applicable` is a gap the profile pipeline can
    gate on, with the gap named.
    """
    scored = [f for f in facts if f["outcome"] in ("pass", "fail")]
    unmeasured = [f["fact_id"] for f in facts if f["outcome"] == "unmeasured"]
    passed = sum(1 for f in scored if f["outcome"] == "pass")
    out: dict[str, Any] = {
        "facts": facts,
        "score": round(100.0 * passed / len(scored), 1) if scored else None,
        "passed": passed,
        "scored_count": len(scored),
        "applicable_count": len(facts),
        "unmeasured": unmeasured,
        "constants": {"rule": "100 * passed / scored; pass/fail only, "
                              "no weights, no partial credit"},
        "registered": REGISTERED,
    }
    if not scored:
        # Reader-visible: report.py prints this in the "Nothing here was
        # checked" headline when `facts` is empty. Score-free wording on
        # purpose — the report renders no aggregate at all by design, so
        # "this is NOT a score of 100" would reintroduce the
        # very framing that was removed. It still says the loud part: an
        # unmeasured block is a coverage gap, never a clean result.
        out["reason"] = ("no config fact could be checked — a coverage gap, "
                         "not a clean result")
    elif unmeasured:
        out["caveat"] = (
            "scored over %d of %d applicable facts: %s could not be "
            "measured; this is a COVERAGE GAP, not a clean result"
            % (len(scored), len(facts), ", ".join(unmeasured)))
    return out

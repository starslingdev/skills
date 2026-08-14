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
# of it. `always()` runs it in every result state, `!cancelled()` unless the
# run was cancelled, and `success() || failure()` is the third spelling of the
# same thing. All three keep the check reporting, which is the property this
# fact is about.
#
# Matching these as substrings was the defect: `always() && <fork guard>` runs
# in every result state AND only when the guard holds, so it skips exactly like
# the bare guard — which is the bypass this fact exists to catch, with two
# tokens prepended. A condition this list does not recognise is treated as
# skippable, so the failure direction of a mis-parse is a false RED (visible,
# arguable) rather than a false green.
_NEVER_SKIPS = {
    "always()",
    "!cancelled()",
    "success() || failure()",
    "failure() || success()",
}
_EXPRESSION_RE = re.compile(r"\$\{\{")
_EXPRESSION_WRAPPER_RE = re.compile(r"^\$\{\{(.*)\}\}$", re.S)


def _never_skips(condition: str) -> bool:
    text = condition.strip()
    wrapped = _EXPRESSION_WRAPPER_RE.match(text)
    if wrapped:
        text = wrapped.group(1).strip()
    return " ".join(text.split()).replace(" (", "(").replace("! ", "!") \
        in _NEVER_SKIPS


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


class _Unknown(str):
    """A skip answer the scan could not determine, carrying its reason.

    A subclass of `str` so it reads like the reason strings beside it, and a
    distinct TYPE so "I don't know" can never be mistaken for either "always
    runs" (`None`) or "can skip" (a plain reason). It used to be the former,
    which is the direction that hides a defect.
    """


def _skip_path(jobs: dict[str, dict], key: str,
               seen: frozenset[str] = frozenset()) -> str | None:
    """Why this job can be skipped, `None` if it always runs, or `_Unknown`.

    Two ways a job skips: its own `if:` evaluates false, or a job it `needs:`
    skips (GitHub skips the dependents). Both report the check as skipped, and
    a skipped required check is green — so both are the same defect here.

    A never-skipping condition (`always()`, `!cancelled()`,
    `success() || failure()`) stops the walk in BOTH directions: such a job
    runs whatever its dependencies did, which is exactly what makes the
    verdict-job pattern work. Recursion is cycle-guarded (`seen`) because a
    malformed `needs:` cycle must not hang the scan; a cycle is unresolvable
    rather than safe, and says so.
    """
    if key in seen:
        return _Unknown(f"`{key}` is part of a `needs:` cycle")
    if key not in jobs:
        return _Unknown(
            f"`{key}` is not a job in this workflow, so whether it runs "
            f"cannot be determined here")
    job = jobs[key]
    condition = _condition_text(job)
    if condition and _never_skips(condition):
        return None
    if condition:
        return f"`{key}` carries `if: {condition}`"
    for need in _needs_of(job):
        upstream = _skip_path(jobs, need, seen | {key})
        if upstream is None:
            continue
        if isinstance(upstream, _Unknown):
            return _Unknown(f"`{key}` needs {upstream}")
        return f"`{key}` needs `{need}`, and {upstream}"
    return None


# Workflow-level filters that keep a job from running at all on a given pull
# request. A required check whose workflow only triggers on `paths:` is the
# textbook form of this bypass — touch nothing matching and GitHub greens the
# check with the suite never run — and it needs no `if:` anywhere.
_TRIGGER_FILTERS = ("paths", "paths-ignore", "branches", "branches-ignore")


def _trigger_skip_reason(rel: str, doc: dict) -> str | None:
    """Why this workflow may not run on a pull request at all, or None."""
    # `on` is a YAML 1.1 boolean, so the key parses as `True` — read it the way
    # the rest of this scanner does rather than by literal name.
    on = _scan()._get_on_node(doc) if isinstance(doc, dict) else None
    if on is True or on is None:
        return None
    if isinstance(on, str):
        on = {on: None}
    if isinstance(on, list):
        on = {str(k): None for k in on}
    if not isinstance(on, dict):
        return None
    pr = on.get("pull_request", on.get("pull_request_target"))
    if not isinstance(pr, dict):
        return None
    present = [f for f in _TRIGGER_FILTERS if pr.get(f)]
    if not present:
        return None
    return (f"{rel} triggers on `pull_request` only for "
            + ", ".join(f"`{f}:`" for f in present)
            + ", so a pull request outside that filter never starts it")


def _display_name(key: str, job: dict) -> str:
    name = job.get("name")
    return " ".join(str(name).split()) if isinstance(name, str) and name.strip() else key


def _context_producers(
    docs: list[tuple[str, dict]], context: str,
) -> list[tuple[str, str, dict, dict[str, dict], dict]]:
    """(workflow, job key, job, sibling jobs, workflow doc) for every job that
    could report `context` as a status check.

    A check context is the job's DISPLAY name — its `name:` if it has one, else
    its key — and a matrix job expands to `name (value, value)`. Both spellings
    are matched; a display name built from an expression (`name: test ${{ … }}`)
    is not matched at all, because what it renders to is not knowable here.
    """
    out = []
    for rel, doc in docs:
        jobs = {k: j for k, j in _jobs(doc)}
        for key, job in jobs.items():
            shown = _display_name(key, job)
            if _EXPRESSION_RE.search(shown):
                continue
            if context == shown:
                out.append((rel, key, job, jobs, doc))
            elif _has_matrix(job) and context.startswith(shown + " ("):
                out.append((rel, key, job, jobs, doc))
    return out


def _has_matrix(job: dict) -> bool:
    """Does this job expand into `name (value, …)` check contexts?

    Only a matrix does. Offering the `name (…)` match to every job let an
    always-running verdict job named `test` be read as a producer of
    `test (self-hosted)` — this repository's own CI shape — and its
    always-runs answer then covered for the suite job that really reports that
    context and really can skip. The bypass hid behind the fix for the bypass.
    """
    strategy = job.get("strategy")
    return isinstance(strategy, dict) and bool(strategy.get("matrix"))


def _required_contexts_via_gh(repo: str) -> tuple[list[str] | None, str]:
    """(contexts, detail) from the GitHub API, or (None, reason).

    Two sources, unioned, because either can be the one a repository uses and
    reading only one would report a protected branch as unprotected: the
    rulesets endpoint (readable with repo read access) and the classic branch
    protection endpoint (admin-only — a 403 there is expected and is not an
    error when the rulesets call succeeded).
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
    reached = False
    try:
        rules = json.loads(gh.run_gh_api(f"repos/{repo}/rules/branches/{branch}"))
        reached = True
        for rule in rules if isinstance(rules, list) else []:
            if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                continue
            params = rule.get("parameters") or {}
            for check in params.get("required_status_checks") or []:
                if isinstance(check, dict) and check.get("context"):
                    contexts.add(str(check["context"]))
    except Exception as exc:                                   # noqa: BLE001
        logger_detail = str(exc)
    else:
        logger_detail = ""
    try:
        classic = json.loads(gh.run_gh_api(
            f"repos/{repo}/branches/{branch}/protection/required_status_checks",
            quiet_not_found=True))
        reached = True
        for context in classic.get("contexts") or []:
            contexts.add(str(context))
        for check in classic.get("checks") or []:
            if isinstance(check, dict) and check.get("context"):
                contexts.add(str(check["context"]))
    except Exception as exc:                                   # noqa: BLE001
        # Admin-only endpoint: a 403/404 here is ordinary — WHEN the other
        # source already told us something. It did not if it never landed, and
        # it did not if it landed EMPTY: a repository can require checks
        # through either mechanism, so "no ruleset requires a check" plus "I
        # may not read classic protection" is unread, not unprotected. That
        # pair is the normal case for the reader this fact is written for —
        # auditing a repository they do not administer — and reading it as a
        # pass turns "I could not see your gate" into "your gate is fine".
        if not reached or not contexts:
            return None, (
                f"branch protection for `{branch}` could not be read in full "
                f"({logger_detail or exc}) — the rulesets endpoint reported "
                f"no required check and classic branch protection is "
                f"admin-only, so a required check configured there would be "
                f"invisible to this token")
    return sorted(contexts), f"branch `{branch}`"


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
            unjudged.append(
                f"`{context}` (no job in these workflows reports it — an "
                f"external app check, a reusable-workflow job, a templated "
                f"job name, or a stale entry)")
            continue
        skips: list[str] = []
        unknown: list[str] = []
        always_runs = False
        for rel, key, _job, jobs, doc in producers:
            reason = _skip_path(jobs, key) or _trigger_skip_reason(rel, doc)
            if reason is None:
                always_runs = True
                break
            if isinstance(reason, _Unknown):
                unknown.append(f"`{context}` ← {rel}: {reason}")
                continue
            skips.append(f"`{context}` ← {rel}: {reason}")
        if always_runs:
            gated += 1
        elif unknown:
            unjudged.extend(unknown[:1])
        else:
            bypassable.extend(skips[:1])

    tail = ((" Not judged: " + _capped(unjudged, 4, "; ") + ".")
            if unjudged else "")
    if bypassable:
        return "fail", (
            "GitHub reports a SKIPPED required check as passed, and "
            + _capped(bypassable, 3)
            + " — so nothing in these workflows is guaranteed to report it."
            + tail)
    if not gated:
        # Everything was unjudged. A green row here would be a claim about an
        # empty set, contradicted by its own next sentence.
        return "unmeasured", (
            f"unmeasured: none of the checks {detail} requires could be traced "
            f"to a job in these workflows, so whether any of them can be "
            f"satisfied by a skipped job was never established." + tail)
    return "pass", (
        f"every required check on {detail} that these workflows produce "
        f"({gated} of {len(contexts)}) has a producer that runs whatever else "
        f"happens." + tail)


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

    ``repo`` (``owner/name``) enables the one fact that needs the API. It is
    optional, and its absence is disclosed as UNMEASURED rather than skipped —
    a fact that quietly vanishes from the table is a silent pass.
    ``required_contexts_fetcher`` is the seam the tests inject a recorded
    response through; nothing in the suite touches the network.
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

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

THE FACTS

  F1 sec.permissions.workflow-declares    every workflow declares `permissions:`
                                          (top level, or on every job)
  F2 sec.permissions.write-scoped         no workflow-level WRITE permission
                                          other than `id-token` (writes belong
                                          on jobs; `write-all` fails)
  F3 sec.codeowners.workflows             a CODEOWNERS entry covers
                                          `.github/workflows/` and the default
                                          branch requires review from Code
                                          Owners (API-gated once covered)
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
  F9 sec.gate.test-failure-fatal          no job that runs the test/lint suite
                                          swallows its own exit code (n/a when
                                          no workflow runs one)
  F10 sec.deps.update-cooldown            Dependabot / Renovate wait >= 3 days
                                          before proposing a fresh release
                                          (n/a when neither is configured)
  F11 sec.environments.branch-restricted  every deployment environment accepts
                                          only refs a non-admin push
                                          credential can neither create nor
                                          move, or requires a reviewer
                                          (API-gated)
  F12 sec.environments.approval-holds     every environment with reviewers
                                          blocks self-review (API-gated)
  F13 sec.secrets.environment-scoped      no repository or organisation
                                          secret is readable from any branch
                                          (API-gated)

F7, F8, F11, F12 and F13 read the GitHub API, not workflow YAML, so they are
TOKEN-GATED: no
repo or no token means UNMEASURED with the reason stated — the same contract
the impostor-SHA vector keeps. They are never a pass for being unreadable and
never a fail for it either. F3 joins them once a CODEOWNERS rule covers
workflows: CODEOWNERS only REQUESTS an owner's review, and whether the default
branch REQUIRES it (a ruleset or classic branch protection) is an API fact
under the same contract. A missing or non-covering CODEOWNERS still fails from
the files alone, with no API call.

F4 replaces "has a dangerous trigger", which is true of 84% of the repos
measured during development and so discriminates nobody. The tiering is deliberate and keeps the
findings-only rule intact: a bare untrusted trigger is ignored (too common to
mean anything); a trigger plus a checkout of the attacker's head FAILS THIS FACT
(the fork-code detector cannot clear the trigger as inert); the full chain —
trigger + head checkout + execution — is the P14.9 FINDING, which stays out of
this number. P14.9's own docstring reserves the middle tier for exactly this
fact ("the bare trigger without the head checkout ... belongs to the scored
config checks").

WHAT IS NEVER A SILENT PASS. Facts F1/F2/F4/F5/F6/F7/F9 are universal claims
over
every workflow file, so ANY unscannable workflow (`scan_incomplete`) forces
them to UNMEASURED — no pass, no fail, a stated reason, and they stay in the
applicable count as a visible coverage gap (the same shape ci-speedup's
speed_score uses). F3 and F10 read repo files and F8 a repository setting,
not workflow YAML, and none of them is affected. F10 has its own unmeasured
state: an update config this scan cannot parse (a Renovate config with
comments, say) is never a pass and never a fail.

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
import logging
import re
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator
from urllib.parse import quote

REGISTERED = "2026-08-03"

# Debug lines carry endpoints and counts only, never a response body or a
# secret name (`gh_utils.run_gh_api` logs each endpoint and its size).
_log = logging.getLogger(__name__)


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
        # escape to scan.py's broad backstop and take all thirteen facts down.
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
        for step in _scan()._job_steps(job):
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
        for step in _scan()._job_steps(job):
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


# --- F9: a verification suite whose failure cannot fail its job --------------
#
# The sibling of `sec.required-checks.skippable`. That fact catches a required
# check that reports green because the job was SKIPPED; this one catches a
# check that RAN and reports green whatever the suite did. Same consequence —
# the merge gate is decorative — and the same fix shape: let the failure reach
# the job's exit status.
#
# ALLOWLIST, NOT A HEURISTIC. Only a `run:` line this list recognises as a
# test, lint, or build-verification suite can fail the fact. Two consequences,
# both deliberate:
#
#   * a `run:` block the list does not recognise (`./scripts/ci.sh || true`) is
#     never failed. It may be swallowing a suite or a cleanup, and the YAML
#     cannot say which — accusing a stranger's repo on a guess is worse than
#     missing a case, especially for a fact that feeds a published score;
#   * `continue-on-error: true` on an UPLOAD, REPORT, or NOTIFICATION step —
#     codecov, upload-artifact, upload-sarif, a Slack curl — is EXEMPT by
#     construction, because no such step runs a suite. Non-fatal reporting
#     steps are a speed and reliability question another engine already owns;
#     reporting them here too would bill one configuration to two engines.
_VERIFICATION_CMDS = (
    r"pytest\b", r"py\.test\b", r"\btox\b", r"\bnox\b", r"pre-commit\s+run\b",
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|typecheck|check)\b",
    r"\bnpx\s+(?:jest|vitest|eslint|playwright|tsc)\b",
    r"\b(?:jest|vitest|mocha|karma)\b",
    r"\bplaywright\s+test\b", r"\bcypress\s+run\b",
    r"\bgo\s+(?:test|vet)\b", r"\bgolangci-lint\s+run\b",
    r"\bcargo\s+(?:test|clippy)\b",
    r"\bmvnw?\b[^|&;]*\s(?:test|verify)\b",
    r"\bgradlew?\b[^|&;]*\s(?:test|check)\b",
    r"\bdotnet\s+test\b", r"\bswift\s+test\b", r"\bctest\b",
    r"\bbazel\s+test\b",
    r"\b(?:bundle\s+exec\s+)?(?:rspec|rubocop)\b",
    r"\brake\s+(?:test|spec)\b",
    r"\bphpunit\b", r"\bcomposer\s+(?:test|lint)\b",
    r"\bmake\s+[^|&;]*\b(?:test|tests|lint|check|checks|verify)\b",
    r"\b(?:ruff|flake8|pylint|mypy|pyright|eslint|stylelint|shellcheck|"
    r"hadolint|actionlint|tflint)\b",
    r"\bblack\b[^|&;]*--check\b",
)
_VERIFICATION_RE = re.compile("|".join(_VERIFICATION_CMDS))

# Commands that MENTION a suite without running one. `pip install pytest || true`
# and `cat tox.ini || true` both carry a name from the list above, and neither
# swallows a test result — the first tolerates a failed install, the second a
# missing file. Reading them as suite runs would fail the fact on a repository
# whose tests are perfectly fatal, which is the false accusation this fact is
# built to avoid. A line that both installs and runs (rare, and usually a
# `&&` chain) is therefore missed rather than guessed at.
_MENTIONS_BUT_DOES_NOT_RUN = re.compile(
    r"\b(?:pip3?|uv|pipx|npm|pnpm|yarn|bun|apt|apt-get|brew|gem|go|cargo|"
    r"dotnet|conda|poetry|asdf)\s+(?:pip\s+)?(?:install|add|get|i)\b"
    r"|\bnpx\s+\S+\s+install\b"
    r"|\b(?:playwright|puppeteer|cypress)\s+install\b"
    r"|\b(?:cat|echo|ls|rm|cp|mv|head|tail|touch|grep|sed|awk|which|"
    r"printf)\b")

# A suite is a COMMAND, not a word. `docker pull ghcr.io/org/mypy:latest`,
# `pkill -f karma`, `tar czf out.tgz .tox`, `gh pr comment --body "eslint
# found 3 issues"` and `git checkout -- jest.config.js` all carry a name from
# the allowlist in an image tag, a process pattern, a directory, a message
# body and a filename respectively. Searching the whole segment read every
# one of them as a test run and failed the fact on repositories whose tests
# are perfectly fatal — the false accusation this fact exists to avoid, and
# the one direction its own scope statement rules out. So the allowlist is
# anchored to the head of the command instead.
#
# What may still precede the command, because it does not change what is
# being run: environment assignments, privilege and timing wrappers, and the
# interpreter runners that are how a suite is normally invoked in the first
# place (`python -m pytest`, `poetry run pytest`, `uv run pytest`).
# Shell KEYWORDS belong on that list too. `if ! pytest -q; then`, `while …`
# and `for m in $MODULES; do pytest` all run the suite; the keyword in front
# of it is grammar, not the command being run. Anchoring the allowlist to the
# head of the segment without them stopped recognising a suite written inside
# a conditional or a loop at all — and a suite the scan cannot see takes the
# whole repository out of this fact's denominator as NOT APPLICABLE, which is
# a silent loss of coverage rather than a miss on one line.
_SHELL_KEYWORDS = (r"if", r"then", r"else", r"elif", r"do", r"while",
                   r"until", r"!")
_COMMAND_PREFIXES = _SHELL_KEYWORDS + (
    r"[A-Za-z_][A-Za-z0-9_]*=\S*",
    r"sudo(?:\s+-\S+)*", r"command", r"exec", r"nice(?:\s+-n\s*-?\d+)?",
    r"env(?:\s+[A-Za-z_][A-Za-z0-9_]*=\S*)*",
    r"time", r"timeout\s+\S+", r"xvfb-run(?:\s+-a)?", r"retry(?:\s+-\S+)*",
    r"(?:python3?|py)\s+-m", r"poetry\s+run", r"pipenv\s+run",
    r"(?:uv|pdm|hatch|rye)\s+run", r"conda\s+run(?:\s+-n\s+\S+)?",
    r"(?:pnpm|npm)\s+exec", r"yarn\s+dlx", r"bun\s+x",
)
_SEGMENT_LEAD = re.compile(r"^\s*(?:(?:%s)\s+)*" % "|".join(_COMMAND_PREFIXES))
# A leading path is part of how the command is spelled, not part of its name:
# `./mvnw test`, `bin/rspec`, `/usr/bin/pytest`.
_PATH_PREFIX = re.compile(r"^(?:\.{0,2}/)?(?:[\w.-]+/)*")


def _command_head(segment: str) -> str:
    """The segment reduced to the command it actually runs."""
    head = segment[_SEGMENT_LEAD.match(segment).end():]
    return head[_PATH_PREFIX.match(head).end():]

# The swallow shapes. Each leaves the shell's last exit status at 0 for a
# command that failed, so the step — and with it the job, and with it the
# required check — reports success.
# `#` terminates the `:` arm alongside `;&|)}`: the form this shape is actually
# written in carries the author's reason on the same line — `pytest || :  #
# tolerate flakes` — and a comment ends the command exactly as those do.
# `|| exit 0` states the discard outright and was the one shape certified as
# a clean bill; `/bin/true` is `true` by absolute path.
_SWALLOW_OR_TRUE = re.compile(
    r"\|\|\s*(?:(?:/(?:usr/)?bin/)?true\b|exit\s+0\b"
    r"|:(?=\s*(?:$|[;&|)}#])))")
_SWALLOW_OR_ECHO = re.compile(r"\|\|\s*echo\b")
_EXIT_ZERO = re.compile(r"(?:^|;)\s*exit\s+0\s*(?:#.*)?$")
_RELAX_ERREXIT = re.compile(r"^set\s+\+[a-z]*e[a-z]*\b")
# What RESCUES a swallow: the block still re-raises a failure afterwards.
# `exit 1`, `exit $rc`, or any read of `$?` means the author kept a path to a
# non-zero exit — a junit report parsed after a tolerated run is the common
# case — and this fact does not second-guess it.
_RERAISES_STATUS = re.compile(
    r"""\$\?|\bexit\s+(?:[1-9]|\$|["']\$)""")
# Restoring `errexit` is a rescue only where it can still act: `set -e`
# changes what happens NEXT, so it rescues a suite it PRECEDES and does
# nothing at all for a failure that already ran and was thrown away.
_RESTORES_ERREXIT = re.compile(r"^set\s+-[a-z]*e[a-z]*\b")


_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|]")


# `echo "exit code was $?"` reports the status; it does not re-raise it.
# Counting a mention as a rescue certifies a job that still exits zero, so a
# `$?` that appears only inside a reporting command does not count.
_REPORTS_ONLY = re.compile(r"^(?:echo|printf)\b")


def _reraises_status(text: str) -> bool:
    """Whether any COMMAND in this text re-raises a failure."""
    return any(_RERAISES_STATUS.search(seg)
               for seg in _SEGMENT_SPLIT.split(text)
               if not _REPORTS_ONLY.match(_command_head(seg)))


def _runs_verification_suite(line: str) -> bool:
    """True when one COMMAND on this line runs a suite.

    Segment by segment, because a line is usually several commands: the
    `echo` in `go test ./... || echo failed` must not clear the `go test`
    beside it, and the `tox.ini` in `cat tox.ini` must not be read as a run
    just because the line also ends in `|| true`.
    """
    return any(_VERIFICATION_RE.match(_command_head(part))
               and not _MENTIONS_BUT_DOES_NOT_RUN.search(part)
               for part in _SEGMENT_SPLIT.split(line))


def _block_runs_verification_suite(run: str) -> bool:
    """Whether ANY executable line of a `run:` block runs a suite.

    Line by line, never over the joined text: the exclusions above are
    line-scoped, so an `echo` on one line would otherwise clear the `pytest`
    on the next.
    """
    return any(_runs_verification_suite(ln) for ln in _code_lines(run))


def _code_lines(run: str) -> list[str]:
    """Executable lines of a `run:` block — comments, blanks and here-doc
    bodies dropped.

    A here-doc body is TEXT ON ITS WAY TO A FILE, not shell the step runs:
    `cat > run.sh <<'EOF' … pytest -q || true … EOF` writes a wrapper script,
    and reading its body as executable failed the fact on a job whose own
    suite is perfectly fatal — the false accusation this fact's scope rules
    out. The opener line itself stays (it is a real command); the body and its
    closing delimiter go. Delimiter recognition is the scan module's, quote
    aware and `<<<`-safe, rather than a second regex here that could drift
    from it.
    """
    delimiter = _scan()._heredoc_delimiter
    lines: list[str] = []
    ends: str | None = None
    for raw in run.splitlines():
        line = raw.strip()
        if ends is not None:
            if line == ends:
                ends = None
            continue
        if not line or line.startswith("#"):
            continue
        lines.append(line)
        ends = delimiter(line)
    return lines


# Shells with no errexit: a trailing `exit 0` after a failed command really
# runs there. Everywhere GitHub's default applies — `bash -e {0}` on Linux and
# macOS runners, `sh -e` where bash is absent, and an explicit `shell: bash`,
# which the runner invokes with `-eo pipefail` — a failing suite aborts the
# step before a trailing `exit 0` is reached: the step already fails and the
# "swallow" is unreachable code. Custom templates and shells this list does
# not name resolve to errexit-bearing, because the failure direction of a
# mis-read here is a false accusation against a fatally wired job, and a miss
# beats that.
_NO_ERREXIT_SHELLS = frozenset({"pwsh", "powershell", "cmd"})


def _defaults_shell(node: dict) -> str | None:
    """The `defaults.run.shell` of a job or workflow mapping, if declared."""
    defaults = node.get("defaults")
    if isinstance(defaults, dict) and isinstance(defaults.get("run"), dict):
        shell = defaults["run"].get("shell")
        if isinstance(shell, str) and shell.strip():
            return shell
    return None


def _exit_zero_can_swallow(step: dict, job: dict, doc: dict) -> bool:
    """Whether this step's effective shell lets a trailing `exit 0` run.

    Resolution order is GitHub's: the step's `shell:`, then the job's
    `defaults.run.shell`, then the workflow's, then the runner default —
    `pwsh` on a `windows-*` runner, `bash -e` everywhere else. An
    expression or an unrecognised value resolves to errexit-bearing
    (see `_NO_ERREXIT_SHELLS` above).
    """
    raw = step.get("shell")
    shell = (raw if isinstance(raw, str) and raw.strip()
             else _defaults_shell(job) or _defaults_shell(doc))
    if shell:
        return shell.strip().split()[0].lower() in _NO_ERREXIT_SHELLS
    runs_on = job.get("runs-on")
    text = " ".join(str(r) for r in runs_on)         if isinstance(runs_on, list) else str(runs_on)
    return "${{" not in text and "windows" in text.lower()


def _restored_before_the_suite_on_one_line(line: str) -> bool:
    """`set -e` sits between the `set +e` and the suite, all on one line."""
    segments = _SEGMENT_SPLIT.split(line)
    suite_at = next(
        (k for k, seg in enumerate(segments)
         if _VERIFICATION_RE.match(_command_head(seg))
         and not _MENTIONS_BUT_DOES_NOT_RUN.search(seg)), None)
    if suite_at is None:
        return False
    return any(_RESTORES_ERREXIT.match(seg.strip())
               for seg in segments[:suite_at])


def _swallow_reason(run: str, exit_zero_live: bool = True) -> str | None:
    """How this block swallows its suite's exit code, or None if it does not.

    Scoped to the SUITE line: `grep … || true` sitting beside `pytest` swallows
    the grep, not the tests, and failing the fact on it would be a false
    accusation about a shape that is usually correct.

    ``exit_zero_live`` is `_exit_zero_can_swallow`'s verdict for the step
    this block came from: the trailing-`exit 0` arm may only fire where the
    shell would actually reach that `exit 0` after a failure.
    """
    code = _code_lines(run)
    suite_at = [i for i, ln in enumerate(code) if _runs_verification_suite(ln)]
    if not suite_at:
        return None

    def rescued_from(index: int) -> bool:
        return any(_reraises_status(ln) for ln in code[index:])

    for i in suite_at:
        line = code[i]
        for pattern, shown in ((_SWALLOW_OR_TRUE, None),
                               (_SWALLOW_OR_ECHO, "|| echo")):
            match = pattern.search(line)
            if not match:
                continue
            # Quote the shape that is actually in the file: a maintainer told
            # their workflow says `|| true` will grep for a string it does not
            # contain.
            shown = shown or re.sub(r"\s+", " ", match.group(0).strip())
            # The rest of the SAME line counts too: `… || { echo …; exit 1; }`
            # reports the failure and still fails the job.
            if _reraises_status(line[match.end():]):
                continue
            if rescued_from(i + 1):
                continue
            return shown

    # `exit 0` as the block's last word: everything before it, suite
    # included, is discarded. "Before" spans the line break AND the semicolon
    # — `npm test; exit 0` is the same discard as the two-line form, and which
    # whitespace separates them is no part of the shape.
    last = code[-1]
    zero = _EXIT_ZERO.search(last) if exit_zero_live else None
    if zero and (suite_at[0] < len(code) - 1
                 or _runs_verification_suite(last[:zero.start()])):
        return "exit 0"

    # `set +e` before the suite, and nothing afterwards that re-raises the
    # status it stopped enforcing.
    for i, line in enumerate(code):
        if not _RELAX_ERREXIT.match(line):
            continue
        # `>= i`, not `> i`: `set +e; pytest -q` puts both on one line and so
        # at one index. `_RELAX_ERREXIT` is anchored to the start of the line,
        # so where the two share an index the relaxation is necessarily the
        # earlier command and the suite runs under it.
        after = [j for j in suite_at if j >= i]
        # A rescue on that shared line counts too — `set +e; pytest; rc=$?;
        # exit $rc` re-raises the status it captured — so the same-line
        # remainder is searched before the lines below it.
        if not after or _reraises_status(line) or rescued_from(i + 1):
            continue
        # Where the relaxation and the suite share a line, the restore between
        # them shares it too, and a slice of LINES between them is empty. Read
        # that line's own segments in order instead: `set +e; set -e; pytest`
        # puts errexit back before the suite runs, and failing it would red a
        # correctly wired suite.
        if after[0] == i and _restored_before_the_suite_on_one_line(line):
            continue
        # `set -e` restored BEFORE the suite runs puts the suite back under
        # `errexit`, so its failure still fails the job. Restored after, it
        # arrives too late to raise anything.
        if any(_RESTORES_ERREXIT.match(ln) for ln in code[i + 1:after[0]]):
            continue
        return "set +e"
    return None


def _continue_on_error_is_literally_true(node: dict) -> bool:
    """A literal `true` only.

    `continue-on-error: ${{ matrix.experimental }}` is true on some matrix legs
    and false on others, and the YAML cannot say which — an expression is
    therefore not judged. GitHub also accepts the quoted string, which parses
    as a str and means the same thing, so that counts.
    """
    value = node.get("continue-on-error")
    return value is True or (isinstance(value, str)
                             and value.strip().lower() == "true")


# `workflow_call` is in the set because a called workflow reports its job
# status into the calling pull-request run: it is where a large repository
# usually keeps its suite. Leaving it out did not merely miss those repos, it
# dropped them out of the denominator entirely — certifying, by silence, the
# exact gate this fact describes.
_GATE_TRIGGERS = frozenset({"pull_request", "pull_request_target", "push",
                            "merge_group", "workflow_call"})


def _can_report_on_a_pull_request(doc: dict) -> bool:
    """Whether this workflow's jobs can report a check on a pull request.

    F9's claim is about a MERGE GATE that reports green whatever the tests
    did, so it only holds where a gate exists. A workflow that runs on a
    schedule, or only when a human dispatches it, reports on no pull request:
    a deliberately tolerant nightly lint or a manual smoke job is a different
    — and usually correct — configuration, and failing it would be exactly
    the false accusation this fact's scope statement rules out.

    An `on:` block this scanner cannot read stays IN scope. Dropping it would
    turn an unreadable trigger into a silent pass, which is the one direction
    this engine never trades away.
    """
    on = _on_mapping(doc)
    if on is None:
        return True
    return bool({str(k) for k in on} & _GATE_TRIGGERS)


_DISCARDS_A_STATUS = re.compile(
    r"\|\|\s*(?:(?:/(?:usr/)?bin/)?true\b|exit\s+0\b"
    r"|:(?=\s*(?:$|[;&|)}]))|echo\b)"
    r"|(?:^|;)\s*exit\s+0\s*(?:#.*)?$|^set\s+\+[a-z]*e[a-z]*\b",
    re.MULTILINE)


def _discards_a_status(run: str) -> bool:
    """Any swallow shape at all, whatever the command is.

    Deliberately NOT scoped to the allowlist. It answers a different question
    from `_swallow_reason`: not "is a suite being swallowed here" but "is
    something being swallowed here that this scan cannot identify" — which is
    what separates a coverage gap from a repository with nothing to check.
    """
    return any(_DISCARDS_A_STATUS.search(ln) for ln in _code_lines(run))


def _suite_failure_swallowed(rel: str,
                             doc: dict) -> tuple[list[str], bool, list[str]]:
    """(offences, whether a suite ran at all, unidentifiable discards).

    The third value is the honest-uncertainty channel. A `run:` line the
    allowlist does not recognise which throws its exit code away — the
    `bash ci/test.sh || true` shape — has two opposite readings, a swallowed
    suite and a tolerated cleanup, and the YAML settles neither. What it is
    NOT is "there is nothing here to check": that verdict asserts no coverage
    gap exists, on the one shape where one demonstrably does.
    """
    offences: list[str] = []
    unidentified: list[str] = []
    saw_suite = False
    if not _can_report_on_a_pull_request(doc):
        return offences, saw_suite, unidentified
    for job_name, job in _jobs(doc):
        job_runs_suite = False
        for job_step in _scan()._iter_job_steps(job):
            step, position = job_step.step, job_step.label
            run = step.get("run")
            if not isinstance(run, str):
                continue
            # `continue-on-error: true` on a `parallel:` group holding the
            # step swallows its failure as surely as its own would.
            group_coe = job_step.group_continue_on_error
            if not _block_runs_verification_suite(run):
                if _discards_a_status(run) or group_coe or \
                        _continue_on_error_is_literally_true(step):
                    unidentified.append(
                        f"{rel}: job `{job_name}` step {position}")
                continue
            job_runs_suite = saw_suite = True
            where = f"{rel}: job `{job_name}` step {position}"
            label = step.get("name")
            if isinstance(label, str) and label.strip():
                where += f" (`{label.strip()}`)"
            if _continue_on_error_is_literally_true(step):
                offences.append(f"{where} — `continue-on-error: true`")
                continue
            if group_coe:
                offences.append(f"{where} — group-level "
                                "`continue-on-error: true`")
                continue
            reason = _swallow_reason(
                run, exit_zero_live=_exit_zero_can_swallow(step, job, doc))
            if reason:
                offences.append(f"{where} — `{reason}`")
        if job_runs_suite and _continue_on_error_is_literally_true(job):
            offences.append(f"{rel}: job `{job_name}` — job-level "
                            "`continue-on-error: true`")
    return offences, saw_suite, unidentified


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
# Only `**` matches EVERY branch. A single `*` is not a synonym for it: in
# GitHub's filter glob `*` matches a run of characters that does NOT include a
# slash, so `feature/x`, `dependabot/npm/…` and every other slashed head branch
# escape it. Treating `*` as match-all certified a required check on a push
# that never fires for those branches — the same over-certification `**`
# alongside an exclusion was already taught to avoid — so `*` routes to UNKNOWN
# with the rest of the specific-branch filters.
_MATCH_ALL_BRANCH_PATTERNS = {"**", "'**'", '"**"'}


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
    branches_ignore = push.get("branches-ignore")
    # A `branches-ignore:` that excludes EVERY branch means the push fires on
    # none of them, so it can never report a check on a pull request — that is
    # NO, not UNKNOWN. Left as UNKNOWN, a sole producer under it that could also
    # skip was counted as a bypassable required check and the fact went RED on a
    # merge that is in fact always blocked (the check stays pending, it does not
    # green). GitHub does not accept `branches` and `branches-ignore` for the
    # same event, so a match-all exclusion stands alone and needs no other test.
    if branches_ignore and _matches_every_branch(branches_ignore):
        return _ABILITY_NO
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
    are matched. A display name built from an expression is matched only when
    the expression is a plain `matrix.<axis>` over legs this scan can enumerate
    from the YAML, and only when nothing makes the rendering a coincidence.
    Those two refusals live in two places, and a reader chasing an unresolved
    check needs both: `_render_templated_name` refuses what the rendering
    itself cannot settle, and `_reusable_caller_claims`, applied here, refuses
    a rendering some reusable call in these files also claims. Everything else
    stays unmatched, because what it renders to is not knowable here.
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
                # A `${{ matrix.* }}` name over ENUMERABLE literal legs renders
                # a knowable set of contexts, and one of them may be the check.
                # See `_templated_name_renderings` for what is refused.
                if context in (_templated_name_renderings(job, shown) or ()) \
                        and not _reusable_caller_claims(docs, context, job):
                    out.append((rel, key, job, jobs, doc, ability))
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


def _leg_text(entry) -> str | None:
    """A matrix leg as GITHUB substitutes it, or None when it is not a scalar.

    `str()` is Python's spelling, not GitHub's, and the difference is not
    cosmetic in either direction. A YAML `true` leg became `True`, so a
    boolean axis matched nothing and the repository kept losing the very fact
    this resolution exists to give it back — the safe direction, but silently
    the whole feature switched off. A `null` leg became `None`, which is worse:
    GitHub substitutes nothing at all there, so `${{ matrix.p }}deploy` really
    does render the bare `deploy` some other job owns — and the empty-leg
    refusal that exists to catch exactly that never fired, because the string
    `"None"` is not empty.
    """
    if isinstance(entry, (dict, list)):
        return None                              # nested shape: not knowable
    if entry is None:
        return ""                                # `null` substitutes nothing
    if isinstance(entry, bool):
        return "true" if entry else "false"      # never Python's `True`
    return str(entry).strip()


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


def _matrix_combinations(job: dict) -> list[dict[str, str]] | None:
    """Every axis assignment this job's matrix can actually run — `[{"shard":
    "1"}, …]` — or None when the matrix is not knowable from the YAML.

    A matrix job runs the COMBINATIONS its axes can really produce, in the
    order those axes are declared, and both readings of its check contexts are
    built from them. Only a matrix expands a job at all; it expands into its
    own values; and it expands into the combinations, not the value set. All
    three clauses matter, and each was learned from a false green in the same
    family:

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

    Both readings of a matrix job's context come from here — the appended
    `(…)` suffix of a literal `name:`, and the rendering of a `${{ matrix.* }}`
    templated one — so the two can never disagree about the same matrix, and a
    refusal added for one is a refusal for both.
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
            text = _leg_text(entry)
            if text is None:
                return None                      # nested shape: not knowable
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
        # An exclude this scan cannot READ is not an exclude it may ignore.
        # Skipping it keeps combinations the run really removes, and EXTRA
        # combinations are the dangerous direction: they manufacture renderings
        # GitHub never emits, any one of which can certify a required check.
        # Three ways a rule went unread, all silent no-ops: a key naming no
        # declared axis, a computed value, and a value that is not a scalar at
        # all — the last one flattened to text like `"[1]"`, which matches no
        # assignment. The values go through the same `_leg_text` as the axes,
        # or the two sides of the comparison disagree about `true` and `null`.
        rule = {}
        for k, v in entry.items():
            text = _leg_text(v)
            if text is None or str(k) not in names or _EXPRESSION_RE.search(text):
                return None
            rule[str(k)] = text
        excluded.append(rule)

    out: list[dict[str, str]] = []
    for combo in itertools.product(*axes):
        assignment = dict(zip(names, combo))
        if any(all(assignment.get(k) == v for k, v in rule.items())
               for rule in excluded):
            continue
        out.append(assignment)
    return out or None


def _matrix_expansions(job: dict) -> set[str] | None:
    """The `(…)` suffixes this job's matrix can render, or None when unknowable.

    The values appear in the order the axes are DECLARED, so joining an
    assignment's values reproduces the suffix GitHub appends. The combinations
    carry their axis NAMES rather than a bare tuple for a different reason:
    the templated-name renderer has to know which value belongs to which axis,
    and both readings have to come from one enumeration or they can disagree
    about the same matrix.
    """
    combos = _matrix_combinations(job)
    if combos is None:
        return None
    return {", ".join(a.values()) for a in combos} or None


def _matrix_produces(job: dict, suffix: str) -> bool:
    """Could this job's matrix render the `(…)` half of a check context?"""
    expansions = _matrix_expansions(job)
    if expansions is None:
        return False
    return " ".join(suffix.split()) in expansions


# A job `name:` may be TEMPLATED over the matrix — `build shard
# ${{ matrix.shard }}/4` over `shard: [1, 2, 3, 4]`. As OBSERVED of the runner,
# GitHub renders such a name and does NOT also append the `(…)` combination
# (it is not written down in GitHub's documentation, and the whole reading
# below rests on it). So the contexts such a job produces are the renderings,
# and when the legs are literals sitting in the YAML they are as enumerable as
# any other expansion.
#
# The rendering is EXACT, never a wildcard match: a rendering is a concrete
# string that either is the required context or is not. Wildcards appear in
# this module only to RULE a template OUT (`_template_could_render`), never to
# rule one in.
#
# One shape neither GitHub's naming nor this reading covers: a templated name
# that omits an axis the matrix declares (`build ${{ matrix.os }}` over
# `os` × `arch`) renders COLLIDING names for several legs. This scan treats
# each rendering as a context the job can report, which stays true of a
# collision — every colliding leg really does report that name.
_PLACEHOLDER_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_MATRIX_REF_RE = re.compile(r"^matrix\.([A-Za-z_][A-Za-z0-9_-]*)$")


def _name_template_is_degenerate(template: str) -> bool:
    """A `name:` carrying no literal WORD to anchor it — `${{ matrix.target }}`,
    and equally `${{ matrix.a }}/${{ matrix.b }}` or `${{ matrix.s }}
    (${{ matrix.l }})`.

    Without anchoring text a rendering that equals a required context is
    indistinguishable from a coincidence: an external app's `Header rules`
    check and a matrix leg spelled the same way are the same string.
    Certifying a required check on that coincidence is a silent pass, and an
    unresolved check is merely unmeasured — so this shape is refused.

    Punctuation is NOT that anchoring text, and reading it as such is how the
    refusal was escaped: `${{ matrix.a }}/${{ matrix.b }}` over `[security]`
    and `[snyk]` renders `security/snyk`, and `${{ matrix.s }}
    (${{ matrix.l }})` renders the `Analyze (javascript)` shape a scanning app
    reports — every discriminating character supplied by a matrix value, the
    `/` and ` ()` supplying none. Nothing in these files competes for an
    EXTERNAL app's name, so the reusable-caller refusal cannot catch these
    either.

    Neither are UNDERSCORES or DIGITS, and spelling the anchor `\\w` said they
    were: `\\w` is `[A-Za-z0-9_]`, so `${{ matrix.os }}_${{ matrix.arch }}`
    over `[ubuntu]` and `[x64]` certified a required `ubuntu_x64` on the same
    coincidence the `/` beside it was refused for — and an underscore between
    two matrix values is a far more ordinary job name than either shape this
    refusal was first written against. A digit is no better: `${{ matrix.t }}
    2` renders `Header rules 2`. The anchor has to be a LETTER, which is the
    only character class a matrix value cannot supply for free.

    One letter is enough, and the rule stays about letters rather than length:
    `v${{ matrix.n }}` is an ordinary release-shard name whose `v` is real
    literal text, and refusing it would report a traceable check as fileless.
    """
    return not re.search(r"[^\W\d_]", _PLACEHOLDER_RE.sub("", template))


def _template_could_render(template: str, context: str) -> bool:
    """Could this template's LITERAL text produce `context`, whatever its
    matrix turns out to hold?

    A template's literal segments survive every substitution, so a template
    whose segments do not appear in `context`, in order, provably does not
    render it. That makes this a way to RULE A TEMPLATE OUT, and it is used
    only where ruling out is the safe direction: narrowing which reusable
    callers compete for a name, and deciding which job a hint may name.

    It is deliberately NOT a way to rule one IN, and must never become one:
    the placeholders are matched with a wildcard, and a wildcard match is the
    coincidence this whole module refuses to certify a check on. Binding stays
    exact — a rendering either is the required context or is not.
    """
    # `_PLACEHOLDER_RE` carries one group, so `split` alternates literal,
    # captured expression, literal — the literals are the even elements.
    pattern = ".*".join(re.escape(part)
                        for part in _PLACEHOLDER_RE.split(template)[::2])
    return re.fullmatch(pattern, context, re.S) is not None


def _reusable_caller_claims(
    docs: list[tuple[str, dict]], context: str, candidate: dict | None = None,
) -> tuple[str, str] | None:
    """The (workflow, job key) of a reusable-workflow CALL in these files that
    claims `context` as its own check or as the `<caller> / <child>` leaf of
    its invocation, or None when none does.

    A caller's own `name:` may itself be TEMPLATED, and comparing a required
    context against unrendered template text never matches — so a matrix
    caller claimed nothing and a foreign `always()` matrix job in another file
    certified a check the real caller produces behind a condition. A templated
    caller is therefore rendered by the same enumeration as any other
    templated name.

    A caller this scan cannot render is a competitor it cannot rule out — but
    only for the names it could actually produce. Claiming EVERY context
    switched the whole matrix-templated resolution off repository-wide for any
    repository holding one `deploy ${{ matrix.env }}` over a computed matrix,
    which is the ordinary deploy-per-environment shape. Its literal text rules
    it out of `build shard 1/4` without guessing at its legs.

    Degeneracy is not an unknowability refusal — a degenerate name enumerates
    fine, and is refused for BINDING because its rendering carries no anchor.
    As a competitor it is knowable, so it is rendered here rather than treated
    as unrenderable.

    `candidate` is the job being decided about, and is skipped: a job carrying
    both `uses:` and a templated `name:` found ITSELF in this walk and claimed
    the name against itself, so it could never resolve — while the same job
    with a literal name resolves. Two readings of one job must not disagree.
    """
    for rel, doc in docs:
        for key, job in _jobs(doc):
            if job is candidate or not isinstance(job.get("uses"), str):
                continue
            shown = _display_name(key, job)
            if _EXPRESSION_RE.search(shown):
                renderings, _reason = _render_templated_name(
                    job, shown, anchored=False)
                if renderings is None:
                    # Two ways this caller could still own the context: as its
                    # own check, or as the `<caller> / <child>` leaf — whose
                    # child half names a job in the CALLED file, which this
                    # scan is not reading, so it is spelled as a placeholder
                    # because that is the wildcard it genuinely is. Written as
                    # a bare `" / "` suffix the pattern demanded the context
                    # END there, and no real leaf ever did, so the leaf half
                    # of this rule never fired.
                    if _template_could_render(shown, context) \
                            or _template_could_render(
                                shown + " / ${{ child }}", context):
                        return rel, key
                    continue
                if any(context == r or context.startswith(r + " / ")
                       for r in renderings):
                    return rel, key
                continue
            if context == shown or context.startswith(shown + " / "):
                return rel, key
    return None


def _templated_name_renderings(job: dict, shown: str) -> set[str] | None:
    """Every display name a templated `name:` can actually render, or None."""
    return _render_templated_name(job, shown)[0]


def _render_templated_name(
    job: dict, shown: str, *, anchored: bool = True,
) -> tuple[set[str] | None, str | None]:
    """(renderings, refusal) for a templated `name:` — renderings, or None and
    the REASON, which the evidence needs so the reader learns the real cause.

    None — the check stays UNTRACED and the fact UNMEASURED — whenever the
    rendering is not knowable from the YAML. An unknown must never come out as
    a known negative, and unmeasured beats a confident wrong answer:

      * `placeholder`: a placeholder that is not a plain `matrix.<axis>`
        reference (`${{ github.ref_name }}`, a function call, `matrix['x']`)
        renders from the run, not the file;
      * `matrix`: a matrix this scan cannot enumerate — `fromJSON()` or any
        other computed value, an `include:`, a nested shape, an unreadable
        `exclude:` — which is the same refusal `_matrix_expansions` already
        makes, reached through the same enumeration so the two cannot
        disagree;
      * `axis`: an axis the name references that the matrix does not declare;
      * `unclosed`: a `${{` with no closing `}}` — not a templated name at
        all, since it has no placeholder to resolve. It reached here anyway
        (the expression test is just `${{`), rendered its raw text unchanged
        for every combination, and the single rendering equalled the bare
        display name — reinstating the one thing the literal branch refuses,
        a MATRIX job standing in for a bare context GitHub never emits for it.

    Two further refusals are about the RENDERING rather than its knowability,
    and each carries its own reason:

      * `degenerate`: no literal text anchors the name, so a rendering that
        equals a required context is indistinguishable from a coincidence.
        Callers deciding COMPETITION rather than binding pass `anchored=False`,
        because a name with no anchor is still perfectly knowable;
      * `whitespace`: a `name:` or a leg whose whitespace this scan normalises
        and GitHub does not, so the compared string is one GitHub never emits.
    """
    if anchored and _name_template_is_degenerate(shown):
        return None, "degenerate"
    # Display names are whitespace-collapsed so they can be compared; GitHub
    # collapses no INTERIOR whitespace, in the template or in the value it
    # substitutes. (Leading and trailing whitespace is stripped from both
    # before comparison, as a YAML plain scalar's already is.) A `name:`
    # carrying interior runs of whitespace therefore renders, after that
    # collapse, a string GitHub never emits — and the required check that does
    # spell it belongs to another producer.
    raw = job.get("name")
    if isinstance(raw, str) and " ".join(raw.split()) != raw.strip():
        return None, "whitespace"
    axes_used = []
    for expr in _PLACEHOLDER_RE.findall(shown):
        ref = _MATRIX_REF_RE.match(expr.strip())
        if ref is None:
            return None, "placeholder"
        axes_used.append(ref.group(1))
    if not axes_used:
        return None, "unclosed"
    combos = _matrix_combinations(job)
    if combos is None:
        return None, "matrix"
    out: set[str] = set()
    for assignment in combos:
        if any(axis not in assignment for axis in axes_used):
            missing = next(a for a in axes_used if a not in assignment)
            return None, f"axis:{missing}"
        # An EMPTY leg renders exactly what GitHub renders, and that is the
        # problem: `${{ matrix.p }}lint` over `p: ['']` collapses to the bare
        # `lint` a different job owns. The anchoring the template was admitted
        # on is supplied by the literal text, and here the discriminating part
        # of the rendering is gone — so the check that survives the anchor
        # test at parse time no longer survives it at render time.
        #
        # A leg whose interior whitespace normalising would REWRITE is the
        # opposite case: there the rendering DIVERGES from GitHub's, so the
        # string compared is one GitHub never emits, whose real producer this
        # scan has not looked at.
        if any(not assignment[a]
               or " ".join(assignment[a].split()) != assignment[a]
               for a in axes_used):
            return None, "whitespace"
        rendered = _PLACEHOLDER_RE.sub(
            lambda m: assignment[_MATRIX_REF_RE.match(
                m.group(1).strip()).group(1)],
            shown)
        out.add(" ".join(rendered.split()))
    return (out, None) if out else (None, "matrix")


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
# "nothing required here": a measured PASS over a source never read. Either
# order: `gh_utils` raises with gh's stderr alone, which puts the reason first
# (`gh: Branch not protected (HTTP 404)`).
_NOT_PROTECTED_404_RE = re.compile(
    r"\b404\b.*branch not protected|branch not protected.*\b404\b",
    re.I | re.S)


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
                # This near miss is about the bare-name-versus-expansion
                # mismatch, which needs a literal name to compare. A templated
                # name has its own note — see `_templated_refusal_note`, which
                # rules a job out by its literal text before naming it, rather
                # than naming one on a guess.
                continue
            if shown == context and _job_has_matrix(job):
                return (f"{rel}: the matrix job `{key}` produces "
                        f"`{context} (…)` expansions, never the bare context, "
                        f"so nothing reports `{context}` — require one of its "
                        f"expansions, or a job without a matrix")
    return None


_TEMPLATE_REFUSALS = {
    "degenerate": ("its name is built only from matrix values, so a rendering "
                   "equal to this context would be a coincidence — this scan "
                   "will not certify a check on one"),
    "whitespace": ("its name or a matrix leg carries interior whitespace, "
                   "which this scan normalises and GitHub does not, so what "
                   "it renders is not the string compared here"),
    "placeholder": ("its name interpolates something that is not a plain "
                    "`matrix.<axis>` value, so it renders from the run, not "
                    "from this file"),
    "matrix": ("its name is templated over a matrix this scan cannot "
               "enumerate"),
    "unclosed": "its name carries a `${{` that is never closed",
}


def _templated_refusal_note(docs: list[tuple[str, dict]], context: str) -> str | None:
    """Why a TEMPLATED job that looks like this context's producer was not
    accepted as one, or None when no such job is in these files.

    Six distinct refusals shared one sentence — "templated over a matrix this
    scan cannot enumerate" — which is false for five of them: in a degenerate
    name, a whitespace rewrite, an undeclared axis and a competing reusable
    call, the matrix enumerates perfectly well. This fact's evidence is the
    surface a reader uses to win back a security grade they lost, and it was
    sending anyone whose axis name held a typo off to hunt for an external app
    that does not exist.

    Only a job whose LITERAL text could produce this context is named. A hint
    pointing at a job that could not have produced it is worse than the
    generic sentence, so ruling out comes first — and ruling out is all this
    match does; nothing here binds a producer.
    """
    for rel, doc in docs:
        for key, job in _jobs(doc):
            shown = _display_name(key, job)
            if not _EXPRESSION_RE.search(shown) \
                    or not _template_could_render(shown, context):
                continue
            renderings, reason = _render_templated_name(job, shown)
            if renderings is not None and context in renderings:
                # It renders this context exactly, so what stopped it is the
                # competing claim — the one refusal applied outside the
                # renderer, and the one a reader is least able to guess.
                claim = _reusable_caller_claims(docs, context, job)
                if claim is None:
                    return None
                return (f"{rel}: the job `{key}` renders `{context}`, but the "
                        f"reusable call `{claim[1]}` in {claim[0]} claims that "
                        f"name too, so which of them reports the check is not "
                        f"settled here")
            if reason is None:
                continue
            if reason.startswith("axis:"):
                return (f"{rel}: the job `{key}` interpolates "
                        f"`matrix.{reason[5:]}`, which its own matrix does not "
                        f"declare, so what its name renders to is not knowable "
                        f"here")
            return f"{rel}: the job `{key}` does not resolve — " \
                   f"{_TEMPLATE_REFUSALS[reason]}"
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
            if near:
                unjudged.append(f"`{context}` ({near})")
                continue
            # The generic sentence lists where an untraceable context USUALLY
            # comes from; the note, when there is one, names the job in these
            # files that nearly produced it and why it did not. It is appended
            # rather than substituted, because the generic list stays true —
            # the note is the part the reader cannot work out for themselves.
            note = _templated_refusal_note(docs, context)
            unjudged.append(
                f"`{context}` (no job in these workflows reports it — an "
                f"external app check, a reusable-workflow job, a templated "
                f"job name, or a stale entry"
                + (f"; {note})" if note else ")"))
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
        # `detail` names the branch and, when only one protection source could
        # be read, carries the partial-read caveat. The pass and unmeasured arms
        # both surface it; the fail arm dropped it, so a reader shown a
        # bypassable check was never told the required-check list they were
        # judged against was itself a floor — a check configured only in the
        # unread source is invisible here.
        return "fail", (
            f"on {detail}, GitHub reports a SKIPPED required check as passed, "
            "and "
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


# --- F3, second half: is the CODEOWNERS rule ENFORCED? -----------------------
#
# A CODEOWNERS rule only REQUESTS a review from its owners. It is binding only
# when the default branch requires review from Code Owners — a `pull_request`
# rule with `require_code_owner_review` in a ruleset (the rules endpoint folds
# in organisation rulesets too), or `require_code_owner_reviews` in classic
# branch protection. With neither, any approver merges a workflow change, and a
# pass here told the reader the opposite. So a covering file is only half the
# fact, and the other half is an API read with F7's contract: no repository or
# no token is UNMEASURED, and so is any answer the scan cannot complete.
#
# Either source saying YES is enough on its own — it is a positive statement
# about what GitHub enforces. NO needs BOTH sources read: the classic endpoint
# is admin-only, so most readers get a 403 (or a bare 404, which GitHub also
# answers to a non-admin) there, and a ruleset that does not require owner
# review says nothing about classic protection that might. The one 404 that IS
# an answer is "Branch not protected" — no classic protection exists — and
# F7's `_is_not_protected_404` recognises exactly that one. When classic is
# unreadable, the public branch endpoint's `protected: false` is the other
# readable "no classic protection" (see `_branch_protected_flag`).

def _branch_segment(branch: str) -> str:
    """`branch` escaped for an API path. A raw `#` or `?` would end the path
    and address another branch (or none); `/` stays, as GitHub's branch paths
    take it as is."""
    return quote(branch, safe="/")


def _branch_protected_flag(gh: Any, repo: str, branch: str) -> bool | None:
    """The public branch endpoint's `protected` flag, or None.

    None covers a failed call, an unexpected shape and a missing or non-bool
    key alike: the caller treats only `False` as an answer.
    """
    try:
        body = json.loads(gh.run_gh_api(
            f"repos/{repo}/branches/{_branch_segment(branch)}"))
    except Exception:                                          # noqa: BLE001
        return None
    flag = body.get("protected") if isinstance(body, dict) else None
    return flag if isinstance(flag, bool) else None


def _code_owner_review_via_gh(repo: str) -> tuple[bool | None, str]:
    """(required, detail) from the API, or (None, reason)."""
    gh = _gh_utils()
    if not gh.check_prereqs():
        return None, "gh is unavailable or not authenticated (run gh auth login)"
    try:
        branch = json.loads(gh.run_gh_api(f"repos/{repo}")).get("default_branch")
    except Exception as exc:                                   # noqa: BLE001
        return None, f"the repository's default branch could not be read ({exc})"
    if not branch:
        return None, "the repository reported no default branch"

    errors: list[str] = []
    rules_require: bool | None = None
    segment = _branch_segment(branch)
    try:
        rules = json.loads(gh.run_gh_api(
            f"repos/{repo}/rules/branches/{segment}"))
        if not isinstance(rules, list):
            raise ValueError(
                f"the rulesets endpoint returned {type(rules).__name__}, not "
                f"the documented array of rules")
        rules_require = any(
            isinstance(rule, dict) and rule.get("type") == "pull_request"
            and isinstance(rule.get("parameters"), dict)
            and rule["parameters"].get("require_code_owner_review") is True
            for rule in rules)
    except Exception as exc:                                   # noqa: BLE001
        errors.append(f"rulesets: {exc}")
    if rules_require:
        return True, f"a ruleset on branch `{branch}`"

    classic_require: bool | None = None
    try:
        classic = json.loads(gh.run_gh_api(
            f"repos/{repo}/branches/{segment}/protection",
            quiet_not_found=True))
        if not isinstance(classic, dict):
            raise ValueError(
                f"the branch protection endpoint returned "
                f"{type(classic).__name__}, not an object")
        reviews = classic.get("required_pull_request_reviews")
        classic_require = (isinstance(reviews, dict)
                           and reviews.get("require_code_owner_reviews") is True)
    except Exception as exc:                                   # noqa: BLE001
        if _is_not_protected_404(exc):
            classic_require = False
        else:
            classic_error = (
                f"classic branch protection: {str(exc).strip()} (only "
                "repository admins can read it; an admin's re-run, or the "
                "branch's settings page, settles it)")
            # The classic endpoint is admin-only, so auditing someone else's
            # repository lands here (403, or a bare 404 GitHub also answers
            # to non-admins). The public branch endpoint's `protected` flag
            # can still settle it — but only in one direction. Verified
            # against live repositories: a branch covered by a ruleset and NO
            # classic protection (classic answers "Branch not protected")
            # reports `protected: true`, so the flag reflects rulesets as well
            # as classic protection. `false` therefore means neither exists:
            # no classic protection, a readable "no". `true` cannot say which
            # source set it, and a failed read says nothing — both leave the
            # classic source unknown. Only asked once the rulesets were read
            # and said no; it stands in for classic protection, never for them.
            flag = (_branch_protected_flag(gh, repo, branch)
                    if rules_require is False else None)
            if flag is False:
                classic_require = False
            elif rules_require is False:
                errors.append(classic_error + (
                    "; the branch reports `protected: true`, which a ruleset "
                    "alone also sets, so that does not settle it"
                    if flag else
                    "; the branch's `protected` flag could not be read either"))
            else:
                errors.append(classic_error)
    if classic_require:
        return True, f"classic branch protection on `{branch}`"
    if rules_require is False and classic_require is False:
        return False, f"branch `{branch}`"
    said = ("its rulesets do not require it, but "
            if rules_require is False else
            "classic branch protection does not require it, but "
            if classic_require is False else "")
    return None, (f"whether branch `{branch}` requires review from Code "
                  f"Owners could not be established — {said}"
                  f"{'; '.join(errors)}")


def _codeowners_enforced(
    root: Path, repo: str | None, fetcher,
) -> tuple[str, str]:
    """(outcome, evidence) for `sec.codeowners.workflows`.

    The file decides every outcome it can: no CODEOWNERS, no covering rule, or
    an unreadable file comes back exactly as `_codeowners_covers_workflows`
    says, with no API call. Only a COVERING file goes on to ask whether the
    default branch enforces it.
    """
    outcome, evidence = _codeowners_covers_workflows(root)
    if outcome != "pass":
        return outcome, evidence
    if not repo:
        return "unmeasured", (
            f"unmeasured: {evidence}, but CODEOWNERS only requests an owner's "
            "review — whether the default branch REQUIRES it is an API fact, "
            "and this scan had no repository to read it from — that needs "
            "`gh` authenticated (`gh auth login`) and a GitHub remote to "
            "derive `owner/name` from")
    try:
        required, detail = fetcher(repo)
    except Exception as exc:                                   # noqa: BLE001
        return "unmeasured", (
            f"unmeasured: {evidence}, but whether the default branch requires "
            f"review from Code Owners could not be read ({exc})")
    if required is None:
        return "unmeasured", f"unmeasured: {evidence}, but {detail}"
    if required:
        return "pass", (
            f"{evidence}, and {detail} requires review from Code Owners, so a "
            "workflow change cannot merge without an owner's approval, except "
            "by an actor the rule lets bypass it")
    return "fail", (
        f"{evidence}, but on {detail} neither a ruleset nor classic branch "
        "protection requires review from Code Owners: CODEOWNERS requests an "
        "owner's review, and the merge rules do not require it, so any "
        "approver can merge a workflow change. Turn on \"Require review from "
        "Code Owners\" in the pull request rule of a ruleset that targets the "
        "branch, or in its branch protection — creating one that requires a "
        "pull request first if the branch has neither")


# --- dependency update cooldown (F10) ----------------------------------------
# The prevention half of P14.25: a hijacked or typosquatted release is usually
# reported and pulled within days, so an update tool that waits before
# proposing a fresh version rarely hands it to CI at all.

# Long enough that most malicious releases are pulled first, short enough to be uncontroversial.
_COOLDOWN_MIN_DAYS = 3

# What Dependabot waits when an `updates` entry sets no `cooldown` (github.com
# since 2026-07-14; GHES from 3.23). An entry with no `cooldown` is judged at
# this value, so it passes while it meets `_COOLDOWN_MIN_DAYS`.
_DEPENDABOT_PLATFORM_DEFAULT_DAYS = 3

_DEPENDABOT_FILES = (".github/dependabot.yml", ".github/dependabot.yaml")
_COOLDOWN_DAY_KEYS = ("default-days", "semver-major-days",
                      "semver-minor-days", "semver-patch-days")
# GitHub documents the `semver-*-days` keys as applying only to package
# managers that support SemVer, and names these as not: they wait
# `default-days`, so a semver key on them changes nothing.
_NON_SEMVER_ECOSYSTEMS = frozenset({"docker", "github-actions", "terraform"})
# Renovate's documented search order (it stops at the first match); the
# `.gitlab/` copies are not read on GitHub. `package.json` is last.
_RENOVATE_FILES = (
    "renovate.json", "renovate.jsonc", "renovate.json5",
    ".github/renovate.json", ".github/renovate.jsonc", ".github/renovate.json5",
    ".renovaterc", ".renovaterc.json", ".renovaterc.jsonc", ".renovaterc.json5",
)
# A filter value that matches every package: Dependabot's `exclude: ["*"]`,
# or a Renovate matcher whose every value is a match-all glob or regex.
_MATCH_ALL = frozenset({"*", "**", "/.*/", ".*", "/.+/", ".+"})


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _matches_everything(value: object) -> bool:
    items = _as_list(value)
    return bool(items) and all(isinstance(v, str) and v.strip() in _MATCH_ALL
                               for v in items)


def _renovate_rule_filters(rule: dict) -> list[str]:
    """The narrowing filters of one `packageRules` entry: every `match*` /
    `exclude*` key (Renovate's matchers, current and deprecated), minus any
    whose value matches every package. Empty means the rule covers all."""
    return [f"{k}: {_capped([str(v) for v in _as_list(val)], 2, ', ')}"
            for k, val in rule.items()
            if (k.startswith("match") or k.startswith("exclude"))
            and not _matches_everything(val)]


_AGE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?|w|weeks?|"
    r"y|yrs?|years?)\s*$", re.I)
_AGE_UNIT_DAYS = {"m": 1 / 1440, "h": 1 / 24, "d": 1, "w": 7, "y": 365}
# Renovate reads months itself before handing the rest to `ms`: a capital
# `M` or `month(s)` is 30 days, while a lower-case `m` stays minutes.
_AGE_MONTHS_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:M|[Mm][Oo][Nn][Tt][Hh][Ss]?)\s*$")


def _renovate_age_days(value: object) -> float | None:
    """Renovate's `minimumReleaseAge` ("3 days", "1 week", "72 hours") in
    days, or None when this scan cannot read the value."""
    if not isinstance(value, str):
        return None
    months = _AGE_MONTHS_RE.match(value)
    if months:
        return float(months.group(1)) * 30
    m = _AGE_RE.match(value)
    if not m:
        return None
    return float(m.group(1)) * _AGE_UNIT_DAYS[m.group(2)[0].lower()]


def _dependabot_cooldown(root: Path) -> tuple[str, str] | None:
    """(outcome, evidence) for Dependabot version updates; None when the repo
    configures none."""
    found = next((rel for rel in _DEPENDABOT_FILES
                  if (root / rel).is_file()), None)
    if found is None:
        return None
    import yaml  # the loader the scan already parses workflows with
    try:
        doc = yaml.safe_load((root / found).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return "unmeasured", (f"unmeasured: {found} could not be read as YAML "
                              f"({type(exc).__name__})")
    updates = doc.get("updates") if isinstance(doc, dict) else None
    if not updates:
        return None
    if not isinstance(updates, list):
        return "unmeasured", f"unmeasured: `updates` in {found} is not a list"
    short: list[str] = []
    defaulted: list[str] = []
    semver_only: list[str] = []
    unreadable: list[str] = []
    exempted: list[str] = []
    for entry in updates:
        if not isinstance(entry, dict):
            unreadable.append("an entry that is not a mapping")
            continue
        where = entry.get("directory") or entry.get("directories") or "?"
        if isinstance(where, list):
            where = ",".join(str(w) for w in where)
        label = f"{entry.get('package-ecosystem', '?')} {where}"
        cooldown = entry.get("cooldown")
        if cooldown is None or cooldown == {}:
            defaulted.append(label)
            if _DEPENDABOT_PLATFORM_DEFAULT_DAYS < _COOLDOWN_MIN_DAYS:
                short.append(f"{label} (no cooldown)")
            continue
        if not isinstance(cooldown, dict):
            unreadable.append(label)
            continue
        exclude = cooldown.get("exclude")
        if exclude:
            excluded = [str(x) for x in _as_list(exclude)]
            if any(x.strip() in _MATCH_ALL for x in excluded):
                short.append(f"{label} (cooldown.exclude: * — every package "
                             "skips the wait)")
                continue
            exempted.append(f"{label} excludes " + _capped(excluded, 3, ", "))
        keys = (("default-days",)
                if entry.get("package-ecosystem") in _NON_SEMVER_ECOSYSTEMS
                else _COOLDOWN_DAY_KEYS)
        days = {k: cooldown[k] for k in keys if k in cooldown}
        if not days:
            defaulted.append(label)
            continue
        if any(isinstance(v, bool) or not isinstance(v, int)
               for v in days.values()):
            unreadable.append(label)
            continue
        if "default-days" not in days:
            # Unlisted semver classes fall back to the platform default.
            days.setdefault("default-days", _DEPENDABOT_PLATFORM_DEFAULT_DAYS)
            if len(days) > 1:
                semver_only.append(label)
        low = [f"{k}: {v}" for k, v in days.items() if v < _COOLDOWN_MIN_DAYS]
        if low:
            short.append(f"{label} ({', '.join(low)})")
    if short:
        return "fail", (
            f"{found}: {_capped(short, 4)} — wait under {_COOLDOWN_MIN_DAYS} "
            "days before proposing a fresh release; set "
            "`cooldown: { default-days: 7 }` on each entry named and raise "
            "(or remove) any `semver-*-days` key under 3 there, keeping its "
            "other `cooldown` keys")
    if unreadable:
        return "unmeasured", (f"unmeasured: {found} has a `cooldown` this scan "
                              f"cannot read ({_capped(unreadable, 3, ', ')})")
    notes = []
    if defaulted:
        notes.append("relying on Dependabot's %d-day platform default "
                     "(github.com, and GHES from 3.23; no cooldown days set): "
                     "%s" % (_DEPENDABOT_PLATFORM_DEFAULT_DAYS,
                             _capped(defaulted, 4, ", ")))
    if semver_only:
        notes.append("semver-specific keys only, unlisted classes on the "
                     "platform default: " + _capped(semver_only, 3, ", "))
    if exempted:
        notes.append("packages exempted from the wait: "
                     + _capped(exempted, 3, "; "))
    return "pass", (f"{found}: every `updates` entry waits at least "
                    f"{_COOLDOWN_MIN_DAYS} days"
                    + ("; " + "; ".join(notes) if notes else ""))


def _renovate_cooldown(root: Path) -> tuple[str, str] | None:
    """(outcome, evidence) for Renovate; None when the repo configures none."""
    config: Any = None
    found = next((rel for rel in _RENOVATE_FILES if (root / rel).is_file()),
                 None)
    if found is not None:
        try:
            config = json.loads((root / found).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return "unmeasured", (
                f"unmeasured: {found} is not plain JSON (comments, trailing "
                "commas or unquoted keys are outside what this scan parses), "
                "so its `minimumReleaseAge` cannot be read; check by hand "
                "that it sets a top-level `minimumReleaseAge` of 3 days or "
                "more")
    elif (root / "package.json").is_file():
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(pkg, dict) or "renovate" not in pkg:
            return None
        found, config = "package.json `renovate`", pkg["renovate"]
    else:
        return None
    if not isinstance(config, dict):
        return "unmeasured", f"unmeasured: {found} is not a JSON object"
    fix = (f"add `\"minimumReleaseAge\": \"7 days\"` at the top level of "
           f"{found}")
    if "minimumReleaseAge" not in config or config["minimumReleaseAge"] is None:
        rules = config.get("packageRules")
        in_rules = isinstance(rules, list) and any(
            isinstance(r, dict) and r.get("minimumReleaseAge") for r in rules)
        presets = config.get("extends")
        return "fail", (f"{found}: no top-level `minimumReleaseAge`"
                        + (" (set only inside `packageRules`, which covers "
                           "only the packages those rules match)"
                           if in_rules else "")
                        + (" (presets in `extends` are not followed; if one "
                           "sets it, set it here too to have it checked)"
                           if presets else "") + f" — {fix}")
    value = config["minimumReleaseAge"]
    rules = config.get("packageRules")
    bypass: list[str] = []
    exempted: list[str] = []
    for i, rule in enumerate(rules if isinstance(rules, list) else []):
        if not isinstance(rule, dict) or "minimumReleaseAge" not in rule:
            continue
        age = rule["minimumReleaseAge"]
        # An explicit null in a rule clears the wait for what it matches.
        rule_days = 0.0 if age is None else _renovate_age_days(age)
        if rule_days is None or rule_days >= _COOLDOWN_MIN_DAYS:
            continue
        filters = _renovate_rule_filters(rule)
        if filters:
            exempted.append(f"rule {i + 1} ({'; '.join(filters)}): {age}")
        else:
            bypass.append(f"rule {i + 1}: {age}")
    if bypass:
        return "fail", (f"{found}: `packageRules` "
                        f"{_capped(bypass, 3, ', ')} lowers "
                        f"`minimumReleaseAge` below {_COOLDOWN_MIN_DAYS} days "
                        "for every package — raise it, or narrow the rule to "
                        "the packages that may skip the wait")
    days = _renovate_age_days(value)
    if days is None:
        return "unmeasured", (f"unmeasured: {found} sets `minimumReleaseAge: "
                              f"{value!r}`, a value this scan cannot convert "
                              "to days")
    if days < _COOLDOWN_MIN_DAYS:
        return "fail", (f"{found}: `minimumReleaseAge: {value}` is under "
                        f"{_COOLDOWN_MIN_DAYS} days — {fix}")
    return "pass", (f"{found}: `minimumReleaseAge: {value}`"
                    + ("; packages exempted from the wait by `packageRules`: "
                       + _capped(exempted, 3, "; ") if exempted else ""))


def _update_cooldown(root: Path) -> tuple[str, str]:
    """(outcome, evidence) for `sec.deps.update-cooldown`. Every configured
    tool must pass; any fail fails; otherwise any unreadable config leaves it
    unmeasured, never passed."""
    try:
        results = [r for r in (_dependabot_cooldown(root),
                               _renovate_cooldown(root)) if r is not None]
    except OSError as exc:
        return "unmeasured", f"unmeasured: could not read update configs ({exc})"
    if not results:
        return "not_applicable", (
            "not applicable: no Dependabot version updates and no Renovate "
            "config, so no update tool here proposes fresh releases")
    outcomes = {o for o, _ in results}
    outcome = ("fail" if "fail" in outcomes else
               "unmeasured" if "unmeasured" in outcomes else "pass")
    return outcome, "; ".join(e for _, e in results)


# --- if a push credential leaks: environments (F11, F12), secrets (F13) ------
#
# A credential that can push workflow files (GitHub documents, among others,
# the `workflow` scope for classic tokens and OAuth apps, and the "Workflows"
# permission for fine-grained tokens and GitHub Apps; see _TOKEN_DOCS) can
# create a throwaway branch, add a workflow that reads secrets, let it run
# and delete the branch. Nothing of it ever reaches the checkout this scan
# reads, and protecting workflow files on the default branch does not stop it.
# What does is GitHub's environment settings: an environment that accepts only
# branches a non-admin credential can neither create nor move, or one that
# needs a reviewer, keeps its secrets and its cloud logins (cloud roles trust
# an environment NAME over OIDC, so an environment with no secrets counts too).
# One exception: GitHub creates `github-pages` and `copilot` itself, so one of
# those that holds no secret is left out of the verdict; one whose secrets
# cannot be read is judged on its settings and, if it would not pass, keeps
# either environment check unmeasured. An admin or owner credential can change these
# settings, so no verdict here covers one.
#
#   F11 sec.environments.branch-restricted  every environment is restricted to
#       "closed" refs, or requires a reviewer
#   F12 sec.environments.approval-holds     every environment with reviewers
#       stops the requester approving their own run
#   F13 sec.secrets.environment-scoped      no repo- or org-level secret is
#       readable from any branch (every secret lives in an environment); the
#       secret-name inventory is its list
#
# A ref is CLOSED when every ref its pattern can match is covered by an active
# ruleset (or classic protection) that blocks creating it AND blocks direct
# updates, with nobody short of an admin able to bypass either. For classic
# protection only the one rule GitHub applies to a branch counts (a rule
# naming it exactly beats every wildcard); a pull request rule blocks updates
# only when it needs at least one approval that a new push dismisses or
# re-asks. An existing
# exact name needs updates blocked and deletion or creation blocked (otherwise
# it can be deleted and pushed again). `evaluate` and `disabled` rulesets count
# for nothing; organisation rulesets count.
#
# The contract is F7's: no repository or no token is UNMEASURED with the reason;
# a source that cannot be read leaves every verdict that depends on it
# unmeasured, never passed and never dropped; no retries.

# PROBE P1 (a private sandbox repository, settings toggled by hand): does a
# branch protected ONLY by a ruleset count as "protected" for an environment
# whose policy is "Protected branches only"? ANSWERED NO (2026-10-24, toggled
# three times): only CLASSIC branch protection rules decide what that policy
# admits. With zero classic rules in the repository it admits every branch and
# every tag; one classic rule, even one matching nothing, makes it restrictive
# and then it admits no tag. So under False the protected set is the classic
# patterns alone, and only classic settings can close them. True restores the
# pre-probe reading (ruleset patterns join the set, rulesets can close).
P1_RULESET_ONLY_BRANCH_COUNTS_AS_PROTECTED = False

# PROBE P6: on which endpoints a 404 is an ANSWER ("nothing configured") rather
# than a failure (wrong repository, wrong path, no access). Only a recording
# moves an endpoint to an answer; every other 404 is unmeasured.
#   * `branch`: recorded 2026-10-24 — a missing branch answers 404 "Branch not
#     found", so that 404 (and only with that reason) means "does not exist".
#   * The deployment-branch-policies endpoint is deliberately NOT an answer:
#     it was recorded answering 404 when the policy is null or
#     protected-branches, but this engine asks it only for custom-policy
#     environments, where a 404 can only mean the read failed. It therefore
#     has no entry here, and `branch_policies()` passes no `kind`.
P6_NOT_FOUND_ANSWERS = {
    "branch": re.compile(r"branch not found", re.I),
}

_MANAGED_ENVIRONMENTS = frozenset({"github-pages", "copilot"})
_BRANCH_FACT = "sec.environments.branch-restricted"
_APPROVAL_FACT = "sec.environments.approval-holds"
_PER_PAGE = "per_page=100"
_GLOB_CHARS = "*?["

# A short, deterministic sort hint for the inventory. It orders the list; it is
# not a severity, and getting it wrong costs only ordering.
_HIGH_VALUE_SECRET = re.compile(
    r"PRIVATE_KEY|APP_ID|PYPI|NPM_TOKEN|PUBLISH|RELEASE|DEPLOY|"
    r"(?:^|_)PAT(?:_|$)|GH_TOKEN|GITHUB_TOKEN")

_SECRETS_FACT = "sec.secrets.environment-scoped"

# Some of the credentials that can add or change workflow files, as GitHub
# documents them. Cited instead of asserted, and not a complete list: the
# report links these rather than listing credential types this skill has not
# tested.
_TOKEN_DOCS = (
    ("the `workflow` scope (classic tokens, OAuth apps)",
     "https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/"
     "scopes-for-oauth-apps"),
    ("the \"Workflows\" permission (GitHub Apps)",
     "https://docs.github.com/en/apps/creating-github-apps/"
     "registering-a-github-app/choosing-permissions-for-a-github-app"),
    ("the \"Workflows\" permission (fine-grained tokens)",
     "https://docs.github.com/en/rest/authentication/"
     "permissions-required-for-fine-grained-personal-access-tokens"),
)
_TOKEN_NOTE = (
    "GitHub documents, among others, these credentials that can add or "
    "change workflow files: "
    + "; ".join(f"[{label}]({url})" for label, url in _TOKEN_DOCS) + ".")

_TIER1_GAP = (
    "Not checked here: a leaked credential that can push code but cannot "
    "push workflow files can still change what this repository's own "
    "workflows run on its branches (build scripts, tests, local actions).")
_CREDENTIAL_NOTE = (
    "A workflow pushed with such a credential can also ask for its own "
    "write-scoped Actions token and an OIDC login token, so any cloud role "
    "that trusts this repository without pinning a branch or an "
    "environment that passes the branch check is exposed too; cloud trust "
    "policies cannot be read from here.")


class _Unread(Exception):
    """A source this scan asked for and could not read; the message is why."""


def _not_found_is_an_answer(kind: str, exc: Exception) -> bool:
    reason = P6_NOT_FOUND_ANSWERS.get(kind)
    text = str(exc)
    return bool(reason and re.search(r"\b404\b", text) and reason.search(text))


def _json_pages(text: str) -> list:
    """Every JSON document `gh api --paginate` printed, in order: one per page
    for an endpoint that answers an object, one merged array for an array."""
    dec = json.JSONDecoder()
    out: list = []
    i, n = 0, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        doc, i = dec.raw_decode(text, i)
        out.append(doc)
    if not out:
        raise ValueError("the response was empty")
    return out


def _paged(text: str, key: str | None) -> list:
    items: list = []
    for page in _json_pages(text):
        got = page if key is None else (
            page.get(key) if isinstance(page, dict) else None)
        if not isinstance(got, list):
            raise ValueError(
                f"a page was {type(page).__name__}, not the documented "
                + ("array" if key is None else f"object with a `{key}` list"))
        items += got
    return items


def _env_segment(name: str) -> str:
    return quote(name, safe="")


_CLASSIC_QUERY = (
    "query($owner: String!, $name: String!) { repository(owner: $owner, "
    "name: $name) { viewerPermission branchProtectionRules(first: 100) { "
    "totalCount nodes { pattern blocksCreations restrictsPushes "
    "requiresApprovingReviews requiredApprovingReviewCount allowsDeletions "
    "dismissesStaleReviews requireLastPushApproval "
    "pushAllowances(first: 1) { "
    "totalCount } bypassPullRequestAllowances(first: 1) { totalCount } } } } }")


class _GhPushAccess:
    """The repository's environment, ruleset and secret settings, read lazily
    over `gh` and memoised, so a run asks only for what a verdict needs.

    Every reader returns the answer or raises `_Unread` with the reason; a
    failure is remembered, so a second verdict over the same source does not
    ask again (there are no retries)."""

    def __init__(self, gh: Any, repo: str):
        self.gh, self.repo = gh, repo
        self._memo: dict[str, tuple[bool, Any]] = {}

    def _once(self, key: str, fn) -> Any:
        if key not in self._memo:
            try:
                self._memo[key] = (True, fn())
            except _Unread as exc:
                self._memo[key] = (False, str(exc))
            except Exception as exc:                           # noqa: BLE001
                # Unmeasured either way, but an engine bug must stay
                # distinguishable from a coverage gap in a debug trace.
                _log.debug("push access reader %s failed", key, exc_info=True)
                self._memo[key] = (False, f"{key}: {str(exc).strip()}")
        ok, value = self._memo[key]
        if not ok:
            raise _Unread(value)
        return value

    def _get(self, path: str, *, paginate: bool = False, kind: str = "",
             key: str | None = None, what: str = "",
             forbidden_expected: bool = False) -> Any:
        try:
            kw = {"quiet_forbidden": True} if forbidden_expected else {}
            text = self.gh.run_gh_api(path, paginate=paginate,
                                      quiet_not_found=True, **kw)
        except Exception as exc:                               # noqa: BLE001
            if kind and _not_found_is_an_answer(kind, exc):
                return None
            raise _Unread(f"{what or path} could not be read "
                          f"({str(exc).strip()})")
        try:
            return _paged(text, key) if paginate else json.loads(text)
        except ValueError as exc:
            raise _Unread(f"{what or path} returned an unexpected body ({exc})")

    def _repository(self) -> dict:
        def read():
            body = self._get(f"repos/{self.repo}", what="the repository")
            if not isinstance(body, dict):
                raise _Unread("the repository endpoint returned no object")
            return body
        return self._once("repository", read)

    def is_admin(self) -> bool:
        """The caller's admin right, from the repository's own
        `permissions.admin`. Only an explicit True counts."""
        perms = self._repository().get("permissions")
        return isinstance(perms, dict) and perms.get("admin") is True

    def default_branch(self) -> str:
        def read():
            body = self._repository()
            branch = body.get("default_branch")
            if not branch:
                raise _Unread("the repository reported no default branch")
            return str(branch)
        return self._once("default_branch", read)

    def environments(self) -> list[dict]:
        def read():
            envs = self._get(f"repos/{self.repo}/environments?{_PER_PAGE}",
                             paginate=True, key="environments",
                             what="the environment list")
            _log.debug("push access: %d environment(s)", len(envs))
            if any(not isinstance(e, dict) or not e.get("name")
                   for e in envs):
                raise _Unread("the environment list held an entry with no "
                              "name")
            return envs
        return self._once("environments", read)

    def branch_policies(self, env: str) -> list[dict]:
        return self._once(f"policies:{env}", lambda: self._get(
            f"repos/{self.repo}/environments/{_env_segment(env)}"
            f"/deployment-branch-policies?{_PER_PAGE}", paginate=True,
            key="branch_policies",
            what=f"the deployment branch policies of `{env}`"))

    def rulesets(self) -> list[dict]:
        def read():
            listed = self._get(
                f"repos/{self.repo}/rulesets?includes_parents=true&{_PER_PAGE}",
                paginate=True, key=None, what="the rulesets")
            details = []
            for item in listed:
                rid = item.get("id") if isinstance(item, dict) else None
                if rid is None:
                    raise _Unread("the rulesets list held an entry with no id")
                body = self._get(
                    f"repos/{self.repo}/rulesets/{rid}?includes_parents=true",
                    what=f"ruleset {rid}")
                if not isinstance(body, dict):
                    raise _Unread(f"ruleset {rid} was not an object")
                details.append(body)
            _log.debug("push access: %d ruleset(s)", len(details))
            return details
        return self._once("rulesets", read)

    def classic_rules(self) -> list[dict]:
        """Classic branch protection RULES (patterns). Only GraphQL lists
        them, and it answers a non-admin with an empty list rather than an
        error, so anything short of an admin's answer is unread, never
        "none"."""
        def read():
            # Recorded: a non-admin is answered with a silent zero, the
            # Actions token with FORBIDDEN. Admin rights are
            # established explicitly first, so a reader never spends the call
            # and a zero is never mistaken for "no rules".
            if not self.is_admin():
                raise _Unread(
                    "classic branch protection rules are readable only by a "
                    "repository admin (GitHub answers anyone else with an "
                    "empty list, which is not the same as none)")
            owner, _, name = self.repo.partition("/")
            try:
                text = self.gh.run_gh_api(
                    "graphql", method="POST",
                    fields={"query": _CLASSIC_QUERY, "owner": owner,
                            "name": name})
                body = json.loads(text)
                repo = body["data"]["repository"]
                rules = repo["branchProtectionRules"]
            except Exception as exc:                           # noqa: BLE001
                raise _Unread(f"classic branch protection rules could not be "
                              f"read ({str(exc).strip()})")
            if repo.get("viewerPermission") != "ADMIN":
                raise _Unread(
                    "classic branch protection rules are readable only by a "
                    "repository admin (GitHub answers anyone else with an "
                    "empty list, which is not the same as none)")
            # A rule the answer cannot show is a rule whose pattern goes
            # unchecked, so an odd shape is unread, never "these are all".
            nodes = rules.get("nodes") if isinstance(rules, dict) else None
            total = rules.get("totalCount") if isinstance(rules, dict) else None
            if (not isinstance(nodes, list) or not isinstance(total, int)
                    or isinstance(total, bool)
                    or any(not isinstance(n, dict) for n in nodes)):
                raise _Unread("classic branch protection rules returned an "
                              "unexpected shape")
            if total > len(nodes):
                raise _Unread("more than 100 classic branch protection rules")
            return nodes
        return self._once("classic", read)

    def protected_branches(self) -> set[str]:
        return self._once("protected", lambda: {
            str(b.get("name")) for b in self._get(
                f"repos/{self.repo}/branches?protected=true&{_PER_PAGE}",
                paginate=True, key=None, what="the protected branch list")
            if isinstance(b, dict)})

    def branch(self, name: str) -> dict | None:
        """The branch, or None when it does not exist (P6: `branch`)."""
        return self._once(f"branch:{name}", lambda: self._get(
            f"repos/{self.repo}/branches/{_branch_segment(name)}",
            kind="branch", what=f"branch `{name}`"))

    def matching_refs(self, prefix: str) -> list[str]:
        def read():
            what = f"the branches starting `{prefix}`"
            refs = self._get(
                f"repos/{self.repo}/git/matching-refs/heads/"
                f"{_branch_segment(prefix)}", paginate=True, key=None,
                what=what)
            # A dropped entry could be the branch that proves a pattern open.
            if any(not isinstance(r, dict) or not str(
                    r.get("ref") or "").startswith("refs/heads/")
                    for r in refs):
                raise _Unread(f"{what} returned an unexpected shape")
            return [str(r["ref"])[len("refs/heads/"):] for r in refs]
        return self._once(f"refs:{prefix}", read)

    def _secret_names(self, key: str, path: str, what: str) -> list[str]:
        def read():
            listed = self._get(path, paginate=True, key="secrets", what=what,
                               forbidden_expected=True)
            # An entry with no name is not "no secret": the list is unread.
            if any(not isinstance(s, dict) or not s.get("name")
                   for s in listed):
                raise _Unread(f"{what} held an entry with no name")
            names = sorted(str(s["name"]) for s in listed)
            _log.debug("push access: %d %s", len(names), key)
            return names
        return self._once(key, read)

    def repo_secrets(self) -> list[str]:
        return self._secret_names(
            "repo secrets", f"repos/{self.repo}/actions/secrets?{_PER_PAGE}",
            "the repository secret list")

    def org_secrets(self) -> list[str]:
        return self._secret_names(
            "org secrets",
            f"repos/{self.repo}/actions/organization-secrets?{_PER_PAGE}",
            "the organisation secret list")

    def environment_secrets(self, env: str) -> list[str]:
        return self._secret_names(
            f"environment secrets:{env}",
            f"repos/{self.repo}/environments/{_env_segment(env)}"
            f"/secrets?{_PER_PAGE}", f"the secrets of `{env}`")


def _push_access_via_gh(repo: str) -> tuple[Any, str]:
    """(source, detail), or (None, reason) when `gh` cannot be used."""
    gh = _gh_utils()
    if not gh.check_prereqs():
        return None, "gh is unavailable or not authenticated (run gh auth login)"
    return _GhPushAccess(gh, repo), "the repository's settings"


# --- ref patterns -------------------------------------------------------------

def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in _GLOB_CHARS)


def _glob_regex(pattern: str, pathname: bool = False) -> re.Pattern:
    """A ref pattern as a regex. `*`, `?` and `[...]` never match `/`.

    `pathname=True` is how GitHub matches branch protection and ruleset
    patterns: Ruby's File.fnmatch with File::FNM_PATHNAME, where only `**/`
    at the start of a path segment matches any number of folders and a bare
    `**` behaves like `*` (`qa/**/*` matches `qa/a/b`; `release/**` does
    not). Docs: "Because GitHub uses the File::FNM_PATHNAME flag for the
    File.fnmatch syntax, the wildcard does not match directory separators"
    (docs.github.com, "Creating rulesets for a repository" and "Managing a
    branch protection rule"). A dot-led segment, which FNM_PATHNAME treats
    apart, cannot occur: git forbids ref components starting with `.`.

    `pathname=False` lets a bare `**` cross `/`. It is used where a wider
    reading is the safe one: the refs an ENVIRONMENT pattern may accept, and
    what a ruleset EXCLUDE may take away."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            at_segment = i == 0 or pattern[i - 1] == "/"
            if pathname and at_segment and pattern.startswith("**/", i):
                out.append("(?:[^/]*/)*")
                i += 3
            else:
                out.append("[^/]*" if pathname else ".*")
                i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                # FNM_PATHNAME matches one segment at a time, so no class,
                # negated or naming `/` itself, ever matches `/`.
                out.append(("(?!/)" if pathname else "") + f"[{body}]")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


_RULESET_ALL = "~ALL"     # a ruleset's own "every ref" condition


def _glob_matches(pattern: str, name: str, pathname: bool = False) -> bool:
    if pattern == _RULESET_ALL:
        return True
    return bool(_glob_regex(pattern, pathname).match(name))


def _glob_prefix(pattern: str) -> str:
    cut = min((pattern.index(c) for c in _GLOB_CHARS if c in pattern),
              default=len(pattern))
    return pattern[:cut]


def _covers(cover: str, pattern: str) -> bool:
    """Does the protection pattern `cover` (read with FNM_PATHNAME, see
    `_glob_regex`) match EVERY ref the accepted `pattern` can match? Exact
    for a literal `pattern`; for a wildcard only the provable shapes (equal,
    match-all, a `**/*` or a trailing `*` over a literal prefix) — anything
    else is "not shown to cover", which can over-fail and never
    under-fail."""
    if cover == _RULESET_ALL:
        return True
    if not _is_glob(pattern):
        return _glob_matches(cover, pattern, pathname=True)
    if cover == pattern:
        return True
    if cover.endswith("**/*"):
        head = cover[:-4]
        if not _is_glob(head) and (not head or head.endswith("/")):
            return pattern.startswith(head) and len(pattern) > len(head)
    head = cover.rstrip("*")
    if len(cover) - len(head) in (1, 2) and not _is_glob(head):
        # `H*` or a bare `H**` (which is `H*`): one segment after `H`. An
        # accepted `**` might cross `/`, so it is not shown to be covered.
        rest = pattern[len(head):]
        return pattern.startswith(head) and "/" not in rest and "**" not in rest
    return False


def _may_overlap(exclude: str, pattern: str) -> bool:
    if not _is_glob(pattern):
        return _glob_matches(exclude, pattern)
    if not _is_glob(exclude):
        return _glob_matches(pattern, exclude)
    return True


def _ruleset_patterns(rs: dict, prefix: str, default_branch: str
                      ) -> tuple[list[str], list[str]]:
    cond = (rs.get("conditions") or {}).get("ref_name") or {}

    def norm(p: str) -> str | None:
        if p == "~ALL":
            return _RULESET_ALL
        if p == "~DEFAULT_BRANCH":
            return default_branch if prefix == "refs/heads/" else None
        return p[len(prefix):] if p.startswith(prefix) else None
    inc = [q for p in cond.get("include") or [] if (q := norm(str(p)))]
    exc = [q for p in cond.get("exclude") or [] if (q := norm(str(p)))]
    return inc, exc


_ADMIN_ACTORS = {("RepositoryRole", 5), ("OrganizationAdmin", None),
                 ("OrganizationAdmin", 1), ("EnterpriseOwner", None)}


def _bypass_state(rs: dict, block: str) -> str:
    """'clean' (nobody short of an admin), 'open' or 'unknown' (the list is
    not served to readers without admin rights) for one block."""
    if "bypass_actors" not in rs or not isinstance(rs["bypass_actors"], list):
        return "unknown"
    for actor in rs["bypass_actors"]:
        if not isinstance(actor, dict):
            return "unknown"
        if actor.get("bypass_mode") == "pull_request" and block != "update":
            # GitHub lets this actor merge a pull request that does not meet
            # the rules. A merge can neither create nor delete a ref, but it
            # does move one, so the actor counts against `update` only.
            continue
        if (actor.get("actor_type"), actor.get("actor_id")) in _ADMIN_ACTORS:
            continue
        return "open"
    return "clean"


_BLOCKS = {"creation": {"creation"}, "update": {"update", "pull_request"},
           "deletion": {"deletion"}}


def _fresh_approval(dismiss: Any, last_push: Any) -> str | None:
    """'held', 'unknown' or None: does an approval stop counting once new
    commits land? Without that, a credential pushes a workflow onto a pull
    request someone already approved and merges it on the old approval."""
    if dismiss is True or last_push is True:
        return "held"
    if dismiss is False and last_push is False:
        return None
    return "unknown"


def _rule_holds(rule: dict) -> str | None:
    """'held', 'unknown' or None for one ruleset rule already of a wanted
    type. A pull_request rule holds updates only when it needs at least one
    approval (with none, a credential merges its own pull request) and that
    approval is dismissed or re-asked after a new push."""
    if rule.get("type") != "pull_request":
        return "held"
    params = rule.get("parameters")
    if not isinstance(params, dict):
        return "unknown"
    count = params.get("required_approving_review_count")
    if not isinstance(count, int) or isinstance(count, bool):
        return "unknown"
    if count < 1:
        return None
    return _fresh_approval(params.get("dismiss_stale_reviews_on_push"),
                           params.get("require_last_push_approval"))


def _requirements(kind: str, pattern: str, exists: bool | None) -> list:
    """Each entry is a set of blocks of which ONE must hold."""
    if kind == "branch" and not _is_glob(pattern) and exists:
        return [{"update"}, {"deletion", "creation"}]
    return [{"creation"}, {"update"}]


def _block_state(block: str, kind: str, pattern: str, rulesets: Any,
                 classic: Any, default_branch: str) -> tuple[str, str]:
    """('held' | 'unknown' | 'absent', why) for one block on one pattern."""
    prefix = "refs/heads/" if kind == "branch" else "refs/tags/"
    target = "branch" if kind == "branch" else "tag"
    unknown: list[str] = []
    if isinstance(rulesets, _Unread):
        unknown.append(f"the rulesets: {rulesets}")
    else:
        for rs in rulesets:
            if rs.get("enforcement") != "active" or rs.get("target") != target:
                continue
            inc, exc = _ruleset_patterns(rs, prefix, default_branch)
            if not any(_covers(q, pattern) for q in inc):
                continue
            if any(_may_overlap(e, pattern) for e in exc):
                continue
            wanted = _BLOCKS[block] - ({"pull_request"} if kind == "tag"
                                       else set())
            holds = {_rule_holds(r) for r in rs.get("rules") or []
                     if isinstance(r, dict) and r.get("type") in wanted}
            if "held" not in holds and "unknown" not in holds:
                continue
            state = _bypass_state(rs, block)
            if state == "open":
                continue
            if state == "unknown":
                unknown.append(
                    f"ruleset `{rs.get('name')}`'s bypass list is not "
                    "readable without the right to edit rulesets (in "
                    "practice, admin)")
            elif "held" in holds:
                return "held", f"ruleset `{rs.get('name')}`"
            else:
                unknown.append(
                    f"ruleset `{rs.get('name')}`'s pull request rule does not "
                    "say how many approvals it needs, or whether they survive "
                    "a new push")
    if kind == "branch":
        if isinstance(classic, _Unread):
            unknown.append(str(classic))
        else:
            state, why = _classic_state(block, pattern, classic)
            if state == "held":
                return "held", why
            if state == "unknown":
                unknown.append(why)
    if unknown:
        return "unknown", "; ".join(dict.fromkeys(unknown))
    return "absent", ""


def _nobody_allowed(rule: dict, key: str) -> bool | None:
    """Does the allowance list `key` name nobody? None when it is unreadable
    (a missing list is unknown, never "nobody")."""
    allowed = rule.get(key)
    count = allowed.get("totalCount") if isinstance(allowed, dict) else None
    if not isinstance(count, int) or isinstance(count, bool):
        return None
    return count == 0


def _classic_holds(rule: dict, block: str) -> str | None:
    """'held', 'unknown' or None: does this one classic rule hold `block`?"""
    if block == "deletion":
        return "held" if rule.get("allowsDeletions") is False else None
    pushes_closed = (_nobody_allowed(rule, "pushAllowances")
                     if rule.get("restrictsPushes") is True else False)
    if block == "creation":
        if rule.get("blocksCreations") is not True:
            return None
        return {True: "held", None: "unknown"}.get(pushes_closed)
    if pushes_closed:
        return "held"
    held = _classic_reviews_hold(rule)
    return "unknown" if held is None and pushes_closed is None else held


def _classic_reviews_hold(rule: dict) -> str | None:
    if rule.get("requiresApprovingReviews") is not True:
        return None
    reviews = _nobody_allowed(rule, "bypassPullRequestAllowances")
    if reviews is None:
        return "unknown"
    if not reviews:
        return None
    count = rule.get("requiredApprovingReviewCount")
    if not isinstance(count, int) or isinstance(count, bool):
        return "unknown"
    if count < 1:
        # With no approval required, a credential merges its own pull request.
        return None
    return _fresh_approval(rule.get("dismissesStaleReviews"),
                           rule.get("requireLastPushApproval"))


def _rules_may_meet(rule_pattern: str, pattern: str) -> bool:
    """Can the classic rule `rule_pattern` match a ref the accepted `pattern`
    accepts? Two wildcards meet unless their literal prefixes or (with no
    `**`) their folder depths rule it out; anything else is "may"."""
    if not _is_glob(pattern):
        return _glob_matches(rule_pattern, pattern, pathname=True)
    if not _is_glob(rule_pattern):
        return _glob_matches(pattern, rule_pattern)
    a, b = _glob_prefix(rule_pattern), _glob_prefix(pattern)
    if not (a.startswith(b) or b.startswith(a)):
        return False
    if "**" not in rule_pattern and "**" not in pattern:
        # A bracket class muddies the count (a `/` inside one is no
        # separator, and the wide reading of an accepted pattern lets a class
        # match `/`), so with one the answer stays "may".
        if "[" in rule_pattern + pattern:
            return True
        return rule_pattern.count("/") == pattern.count("/")
    return True


def _classic_state(block: str, pattern: str, classic: list[dict]
                   ) -> tuple[str, str]:
    """('held' | 'unknown' | 'absent', why) from classic protection alone.

    GitHub applies ONE classic rule to a branch, never a combination: a rule
    naming the branch exactly outranks every wildcard rule, and among
    wildcard rules the oldest wins (docs.github.com, "About protected
    branches"). The rules' order is not read, so where wildcard rules that
    disagree may both apply, the answer is unknown, never held."""
    meeting = [(p, r) for r in classic
               if (p := str(r.get("pattern") or ""))
               and _rules_may_meet(p, pattern)]
    if not _is_glob(pattern):
        exact = [(p, r) for p, r in meeting if not _is_glob(p)]
        applying = exact[:1] or meeting
        covering = applying
    else:
        applying = meeting
        covering = [(p, r) for p, r in meeting if _covers(p, pattern)]
    states = {p: _classic_holds(r, block) for p, r in applying}
    held = [p for p, _ in covering if states[p] == "held"]
    if not held:
        unsure = [p for p, _ in covering if states[p] == "unknown"]
        if unsure:
            return "unknown", (
                f"classic rule `{unsure[0]}` does not say how many approvals "
                "it needs, or whether they survive a new push")
        return "absent", ""
    rivals = [p for p, _ in applying if states[p] != "held"]
    if not rivals:
        return "held", f"classic rule `{held[0]}`"
    if any(not _is_glob(p) and states[p] is None for p in rivals):
        # A rule naming one accepted branch exactly governs it, and it leaves
        # that branch open.
        return "absent", ""
    return "unknown", (
        f"classic rules `{held[0]}` and `{rivals[0]}` both may match; GitHub "
        "applies only one rule to a branch (an exact name first, else the "
        "oldest), and their order is not read here")


_BLOCK_WORDS = {"creation": "creating it", "update": "pushing to it",
                "deletion": "deleting it"}


def _pattern_state(src: Any, kind: str, pattern: str, rulesets: Any,
                   classic: Any, default_branch: str,
                   accepts_unprotected: bool) -> tuple[str, str]:
    """('closed' | 'open' | 'unknown', why) for one accepted ref pattern.

    `accepts_unprotected` is False under "protected branches only", where an
    existing unprotected branch is not an accepted ref at all."""
    label = f"`{pattern}`" + (" (tag)" if kind == "tag" else "")
    exists: bool | None = None
    if kind == "branch" and not _is_glob(pattern):
        try:
            body = src.branch(pattern)
        except _Unread as exc:
            return "unknown", f"{label}: {exc}"
        exists = body is not None
        if exists and accepts_unprotected and \
                isinstance(body, dict) and body.get("protected") is False:
            return "open", (f"{label} exists and nothing protects it, so any "
                            "push credential can push a workflow to it")
    missing: list[str] = []
    unknown: list[str] = []
    for options in _requirements(kind, pattern, exists):
        states = [(b, *_block_state(b, kind, pattern, rulesets, classic,
                                    default_branch)) for b in sorted(options)]
        if any(s == "held" for _, s, _ in states):
            continue
        unk = [why for _, s, why in states if s == "unknown"]
        if unk:
            unknown += unk
        else:
            missing.append(" or ".join(_BLOCK_WORDS[b] for b, _, _ in states))
    if not missing and not unknown:
        return "closed", f"{label} is closed"
    # Rules alone left it undecided (an unreadable source, an unreadable
    # bypass list). One more answer settles it as OPEN without those sources:
    # a branch matching the pattern that already exists with no protection.
    if not missing and kind == "branch" and _is_glob(pattern) \
            and accepts_unprotected:
        prefix = _glob_prefix(pattern)
        if prefix:
            try:
                refs = src.matching_refs(prefix)
                protected = src.protected_branches() if refs else set()
            except _Unread as exc:
                refs, protected = [], set()
                unknown.append(str(exc))
            # Proof of OPEN, so the narrower reading of the accepted pattern.
            hit = next((r for r in refs
                        if _glob_matches(pattern, r, pathname=True)
                        and r not in protected), None)
            if hit:
                return "open", (
                    f"{label} matches `{hit}`, which exists and nothing "
                    "protects, so any credential that can push workflow "
                    "files can push one to it")
    if missing:
        what = "a branch" if kind == "branch" else "a tag"
        if exists is True:
            return "open", (f"{label} exists, and nothing blocks "
                            f"{'; nor '.join(missing)} — any push credential "
                            "can push a workflow to it")
        lead = (" does not exist yet, and" if exists is False else ":")
        return "open", (f"{label}{lead} nothing blocks "
                        f"{'; nor '.join(missing)} — any push credential "
                        f"can make {what} it accepts")
    return "unknown", (f"{label}: whether it is closed could not be "
                       f"established — {'; '.join(dict.fromkeys(unknown))}")


def _source(fn) -> Any:
    try:
        return fn()
    except _Unread as exc:
        return exc


def _environment_branch_verdict(src: Any, env: dict) -> tuple[str, str]:
    """(pass | fail | unmeasured, why) for one in-scope environment."""
    name = env["name"]
    reviewers = _reviewer_count(env)
    if reviewers:
        return "pass", f"`{name}` requires a reviewer"
    policy = env.get("deployment_branch_policy")
    if policy is None:
        return "fail", (f"`{name}` accepts any branch and needs no reviewer")
    if not isinstance(policy, dict):
        return "unmeasured", f"`{name}`'s branch policy is not readable"
    try:
        default_branch = src.default_branch()
    except _Unread as exc:
        return "unmeasured", f"`{name}`: {exc}"
    classic = _source(src.classic_rules)
    if policy.get("protected_branches") is True and \
            not P1_RULESET_ONLY_BRANCH_COUNTS_AS_PROTECTED:
        # Probe P1: only classic rules decide, so only classic settings can
        # close a pattern; rulesets are not consulted at all.
        if isinstance(classic, _Unread):
            return "unmeasured", (
                f"`{name}` accepts \"protected branches\", which only classic "
                f"branch protection rules decide — {classic}")
        patterns = list(dict.fromkeys(
            str(r.get("pattern")) for r in classic if r.get("pattern")))
        if not patterns:
            return "fail", (
                f"`{name}` accepts \"protected branches\", and the repository "
                "has no classic branch protection rule, in which case GitHub "
                "lets every branch and tag deploy to it; it needs no reviewer")
        states = [_pattern_state(src, "branch", p, [], classic,
                                 default_branch, accepts_unprotected=False)
                  for p in patterns]
        opened = [why for s, why in states if s == "open"]
        if opened:
            return "fail", (f"`{name}` accepts protected branches, and "
                            f"{_capped(opened, 2)}")
        unsettled = [why for s, why in states if s == "unknown"]
        if unsettled:
            return "unmeasured", f"`{name}`: {_capped(unsettled, 2)}"
        return "pass", (f"`{name}` accepts only branches matching a classic "
                        "protection rule, and every such rule blocks "
                        "creating and pushing")
    rulesets = _source(src.rulesets)
    if policy.get("protected_branches") is True:
        patterns: list[tuple[str, str]] = []
        unread: list[str] = []
        if P1_RULESET_ONLY_BRANCH_COUNTS_AS_PROTECTED:
            if isinstance(rulesets, _Unread):
                unread.append(str(rulesets))
            else:
                for rs in rulesets:
                    if rs.get("enforcement") == "active" and \
                            rs.get("target") == "branch":
                        inc, _ = _ruleset_patterns(rs, "refs/heads/",
                                                   default_branch)
                        patterns += [("branch", "**" if p == _RULESET_ALL
                                      else p) for p in inc]
        if isinstance(classic, _Unread):
            unread.append(str(classic))
        else:
            patterns += [("branch", str(r.get("pattern")))
                         for r in classic if r.get("pattern")]
        states = [_pattern_state(src, k, p, rulesets, classic, default_branch,
                                 accepts_unprotected=False)
                  for k, p in dict.fromkeys(patterns)]
        opened = [why for s, why in states if s == "open"]
        if opened:
            return "fail", (f"`{name}` accepts protected branches, and "
                            f"{_capped(opened, 2)}")
        if unread:
            return "unmeasured", (
                f"`{name}` accepts protected branches, and which branches "
                f"those are could not be read in full — "
                f"{'; '.join(unread)}")
        unsettled = [why for s, why in states if s == "unknown"]
        if unsettled:
            return "unmeasured", f"`{name}`: {_capped(unsettled, 2)}"
        if not states:
            return "fail", (f"`{name}` accepts \"protected branches\", and "
                            "with none protected GitHub lets every branch "
                            "and tag deploy")
        return "pass", (f"`{name}` accepts only protected branches, and every "
                        "protected pattern is closed")
    if policy.get("custom_branch_policies") is True:
        try:
            entries = src.branch_policies(name)
        except _Unread as exc:
            return "unmeasured", f"`{name}`: {exc}"
        states = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("name"):
                return "unmeasured", (f"`{name}`: a deployment branch policy "
                                      "entry had no name")
            # No type is GitHub's default, a branch; any other is unknown.
            kind = entry.get("type") or "branch"
            if kind not in ("branch", "tag"):
                return "unmeasured", (f"`{name}`: a deployment branch policy "
                                      f"entry has the unknown type `{kind}`")
            states.append(_pattern_state(
                src, kind, str(entry["name"]), rulesets, classic,
                default_branch, accepts_unprotected=True))
        opened = [why for s, why in states if s == "open"]
        if opened:
            return "fail", f"`{name}`: {_capped(opened, 2)}"
        unsettled = [why for s, why in states if s == "unknown"]
        if unsettled:
            return "unmeasured", f"`{name}`: {_capped(unsettled, 2)}"
        return "pass", (f"`{name}` accepts only branches and tags a "
                        "non-admin push credential can neither create nor "
                        "move")
    return "unmeasured", (f"`{name}` has a branch policy this check does not "
                          "recognise")


def _reviewer_rules(env: dict) -> list[dict]:
    """The environment's required-reviewer rules. A rule list or reviewer
    list of an unexpected shape raises `_Unread`: it is not zero reviewers."""
    rules = env.get("protection_rules")
    if not isinstance(rules, list) or any(
            not isinstance(r, dict) for r in rules):
        raise _Unread(f"`{env['name']}`'s protection rules have an "
                      "unexpected shape")
    found = [r for r in rules if r.get("type") == "required_reviewers"]
    if any(not isinstance(r.get("reviewers"), list) for r in found):
        raise _Unread(f"`{env['name']}`'s required reviewer list has an "
                      "unexpected shape")
    return found


def _reviewer_count(env: dict) -> int:
    return sum(len(r["reviewers"]) for r in _reviewer_rules(env))


def _self_review_allowed(env: dict) -> bool:
    return not any(r.get("prevent_self_review") is True
                   for r in _reviewer_rules(env))


def _inventory(src: Any) -> dict[str, Any]:
    repo_s, org_s = _source(src.repo_secrets), _source(src.org_secrets)
    reasons = [str(x) for x in (repo_s, org_s) if isinstance(x, _Unread)]
    repo_names = None if isinstance(repo_s, _Unread) else repo_s
    org_names = None if isinstance(org_s, _Unread) else org_s
    status = ("measured" if not reasons else
              "unmeasured" if len(reasons) == 2 else "partial")
    names = (repo_names or []) + (org_names or [])
    return {"status": status, "repo_secrets": repo_names,
            "org_secrets": org_names,
            "high_value": sorted(n for n in set(names)
                                 if _HIGH_VALUE_SECRET.search(n.upper())),
            "reason": "; ".join(reasons)}


def _not_checked_block(reason: str) -> dict[str, Any]:
    return {"status": "not_checked", "reason": reason, "environments": [],
            "inventory": {"status": "not_checked", "repo_secrets": None,
                          "org_secrets": None, "high_value": [],
                          "reason": reason},
            "tier1_gap": _TIER1_GAP, "credential_note": _CREDENTIAL_NOTE}


def _push_access(repo: str | None, fetcher) -> tuple[
        tuple[str, str], tuple[str, str], dict[str, Any]]:
    """((F11 outcome, evidence), (F12 outcome, evidence), push_access block)."""
    if not repo:
        reason = ("environment and secret settings are read over the API, and "
                  "this scan had no repository to read them from — that needs "
                  "`gh` authenticated (`gh auth login`) and a GitHub remote "
                  "to derive `owner/name` from")
        both = ("unmeasured", f"unmeasured: {reason}")
        return both, both, _not_checked_block(reason)
    try:
        src, detail = fetcher(repo)
    except Exception as exc:                                   # noqa: BLE001
        src, detail = None, f"the settings could not be read ({exc})"
    if src is None:
        both = ("unmeasured", f"unmeasured: {detail}")
        return both, both, _not_checked_block(detail)

    inventory = _inventory(src)
    try:
        envs = src.environments()
    except _Unread as exc:
        both = ("unmeasured", f"unmeasured: {exc}")
        return both, both, {
            "status": "unmeasured", "reason": str(exc), "environments": [],
            "inventory": inventory, "tier1_gap": _TIER1_GAP,
            "credential_note": _CREDENTIAL_NOTE}

    rows: list[dict[str, Any]] = []
    # GitHub-managed environments whose secrets cannot be read, and which
    # would not pass on their settings: each may hold secrets, so each keeps
    # the check from passing (an unread source never yields a pass).
    could_fail: list[str] = []
    approval_could_fail: list[str] = []
    for env in envs:
        name = str(env["name"])
        try:
            _reviewer_rules(env)
        except _Unread as exc:
            rows.append({"name": name,
                         "managed": name in _MANAGED_ENVIRONMENTS,
                         "admins_can_bypass":
                             env.get("can_admins_bypass") is True,
                         "reviewers": None, "has_rules": True,
                         "branch_check": "unmeasured",
                         "branch_check_why": str(exc),
                         "approval_check": "unmeasured",
                         "approval_check_why": ""})
            continue
        row = {"name": name, "managed": name in _MANAGED_ENVIRONMENTS,
               "admins_can_bypass": env.get("can_admins_bypass") is True,
               "reviewers": _reviewer_count(env),
               # Whether there is anything for an admin to skip: a branch
               # policy or required reviewers.
               "has_rules": (env.get("deployment_branch_policy") is not None
                             or _reviewer_count(env) > 0)}
        if row["managed"]:
            try:
                held = src.environment_secrets(name)
            except _Unread as exc:
                # The real reason (a 403, a 502, a rate limit) is kept: not
                # every unread list is a permission gap.
                held, unread = None, str(exc)
            if held is None:
                verdict, why = _environment_branch_verdict(src, env)
                if verdict != "pass":
                    could_fail.append(
                        f"`{name}` is GitHub-managed, its secrets could not "
                        f"be read ({unread}), and {why}")
                if row["reviewers"] and _self_review_allowed(env):
                    approval_could_fail.append(
                        f"`{name}` is GitHub-managed, its secrets could not "
                        f"be read ({unread}), and it lets whoever started a "
                        "run approve it")
            if not held:
                row.update(branch_check="info", approval_check="info", branch_check_why=(
                    f"`{name}` is GitHub-managed; can't tell whether it holds "
                    f"secrets ({unread})" if held is None else
                    f"`{name}` is GitHub-managed and holds no secret"),
                    approval_check_why="")
                rows.append(row)
                continue
        verdict, why = _environment_branch_verdict(src, env)
        row.update(branch_check=verdict, branch_check_why=why)
        if row["reviewers"]:
            allowed = _self_review_allowed(env)
            row.update(approval_check="fail" if allowed else "pass", approval_check_why=(
                f"`{name}` lets whoever started a run approve it" if allowed
                else f"`{name}` blocks self-review"))
        else:
            row.update(approval_check="not_applicable", approval_check_why="")
        rows.append(row)
    block = {"status": "measured", "reason": "", "environments": rows,
             "inventory": inventory, "tier1_gap": _TIER1_GAP,
             "credential_note": _CREDENTIAL_NOTE}

    judged = [r for r in rows if r["branch_check"] != "info"]
    cant_tell = [r["branch_check_why"] for r in rows
                 if r["branch_check"] == "info"
                 and "can't tell" in r["branch_check_why"]]
    if not judged:
        branch = _zero_environments(cant_tell, bool(rows))
    else:
        failed = [r["branch_check_why"] for r in judged if r["branch_check"] == "fail"]
        unsettled = [r["branch_check_why"] for r in judged if r["branch_check"] == "unmeasured"]
        if failed:
            branch = ("fail", (
                f"{len(failed)} of {len(judged)} environment(s) can be "
                "reached from a branch any push credential can make, and "
                "need no reviewer: " + _capped(failed, 6) + ". Restrict each "
                "to \"Selected branches and tags\" naming protected branches "
                "whose rules block creating and pushing, or to \"Protected "
                "branches only\" with a classic branch protection rule on "
                "every protected pattern that turns on \"Restrict who can "
                "push to matching branches\" (nobody listed) and \"Restrict "
                "pushes that create matching branches\" (rulesets do not "
                "count for that option) — or add a required reviewer"))
        elif unsettled or could_fail:
            branch = ("unmeasured", "unmeasured: "
                   + _capped(unsettled + could_fail, 4))
        else:
            branch = ("pass", (
                f"every one of {len(judged)} environment(s) is limited to "
                "branches and tags a non-admin push credential can neither "
                "create nor move, or requires a reviewer"))
    reviewed = [r for r in rows
                if r.get("approval_check") in ("pass", "fail")]
    approval_could_fail += [r["branch_check_why"] for r in rows
                            if r["approval_check"] == "unmeasured"]
    if approval_could_fail and not any(
            r["approval_check"] == "fail" for r in reviewed):
        approval = ("unmeasured", "unmeasured: "
                    + _capped(approval_could_fail, 4))
    elif not reviewed:
        approval = ("not_applicable", (
            "not applicable: no environment requires a reviewer (whether "
            "environments need one is the branch-restriction check's "
            "question)"))
    else:
        selfish = [r["name"] for r in reviewed if r["approval_check"] == "fail"]
        approval = ("fail", (
            f"{len(selfish)} of {len(reviewed)} environment(s) with required "
            "reviewers let whoever started a run approve it: "
            + _capped([f"`{n}`" for n in selfish], 6, ", ")
            + ". A leaked reviewer's credential approves its own run; turn "
            "on \"Prevent self-review\" for each")) if selfish else (
            "pass", (f"every one of {len(reviewed)} environment(s) with "
                     "required reviewers blocks self-review"))
    return branch, approval, block


def _zero_environments(cant_tell: list[str], any_env: bool
                       ) -> tuple[str, str]:
    """No environment to judge: NOT APPLICABLE, whatever the secrets are.

    Readable repository and organisation secrets are
    `sec.secrets.environment-scoped`'s question; failing here too would make
    one setup cost two checks. A GitHub-managed environment whose secrets cannot be read might be in
    scope, so that one case stays unmeasured."""
    if cant_tell:
        return "unmeasured", (
            "unmeasured: the only environments are GitHub-managed ones, "
            "and whether they hold secrets could not be read: "
            + _capped(cant_tell, 2))
    where = ("no environments" if not any_env else
             "no environments other than GitHub-managed ones holding nothing")
    return "not_applicable", (
        f"not applicable: {where}, so there is no environment to restrict "
        "(whether secrets sit outside one is the environment-scoped secrets "
        "check's question)")


def _secret_names_in_order(names: list[str]) -> list[str]:
    """Likely high-value names first (a sort order, not a severity)."""
    return sorted(names, key=lambda n: (
        not _HIGH_VALUE_SECRET.search(n.upper()), n))


_SCOPED_FIX = (
    "To fix each one: pick or create an environment that passes the "
    "branch-restriction check above, re-enter the secret's value there "
    "(GitHub never shows a stored value, so only you can do this step), "
    "point the jobs that use it at that environment, then delete the "
    "repository or organisation copy. What this can break: a job on a pull "
    "request or on a branch the environment does not accept can no longer "
    "read the secret; give that job an environment with a required "
    "reviewer, or stop it needing the secret")
_SCOPED_ORG_FIX = (
    "; for an organisation secret, narrowing its repository access to the "
    "repositories that need it is the alternative")


def _secrets_scoped(inventory: dict[str, Any]) -> tuple[str, str]:
    """(outcome, evidence) for `sec.secrets.environment-scoped`.

    Pass: no repository- or organisation-level secret is readable here (no
    secrets at all included). Fail: at least one is, from any branch a push
    credential creates. Unmeasured when a list cannot be read — unless the
    list that WAS read already proves the fail, which no unread list can
    undo."""
    repo_names = inventory.get("repo_secrets")
    org_names = inventory.get("org_secrets")
    reason = inventory.get("reason") or "the secret lists were not read"
    if inventory.get("status") == "not_checked":
        return "unmeasured", f"unmeasured: {reason}"
    parts = []
    if repo_names:
        parts.append(f"{len(repo_names)} repository secret(s): " + ", ".join(
            f"`{n}`" for n in _secret_names_in_order(repo_names)))
    if org_names:
        parts.append(f"{len(org_names)} organisation secret(s): " + ", ".join(
            f"`{n}`" for n in _secret_names_in_order(org_names)))
    if parts:
        unread = ("" if inventory.get("status") == "measured" else
                  f" (the other list could not be read: {reason}, so this "
                  "may not be all of them)")
        return "fail", (
            "readable from any branch a push credential creates, outside "
            "every environment — " + "; ".join(parts) + unread + ". "
            + _SCOPED_FIX + (_SCOPED_ORG_FIX if org_names else ""))
    if inventory.get("status") == "measured":
        return "pass", ("no repository or organisation secret is readable "
                        "here; every secret this repository uses lives in an "
                        "environment, or there are none")
    return "unmeasured", (
        f"unmeasured: the secret lists could not be read in full ({reason})"
        + _secrets_hint(reason))


def _secrets_hint(reason: str) -> str:
    """What to change so the secret lists can be read, from the 403 itself:
    a token not authorised for single sign-on needs that, not more rights.
    Otherwise GitHub's own answer, quoted in the reason, says what it wants."""
    if "SAML" in reason:
        return ("; authorise the token for the organisation's SAML single "
                "sign-on")
    if "IP allow list" in reason:
        return "; run from an address the organisation's IP allow list permits"
    return ""


# --- the fact table -----------------------------------------------------------

def compute_config_facts(
    root: Path,
    workflow_files: list[Path],
    scan_incomplete: list[dict[str, str]],
    repo: str | None = None,
    required_contexts_fetcher: Any = None,
    fork_approval_fetcher: Any = None,
    codeowner_review_fetcher: Any = None,
    push_access_fetcher: Any = None,
) -> dict[str, Any]:
    """Every fact, every time — pass/fail/unmeasured, never silently absent.

    ``repo`` (``owner/name``) enables the SIX facts that need the API —
    ``sec.required-checks.skippable`` (branch protection),
    ``sec.fork-approval.effective`` (the repository's fork-PR approval
    setting), once a CODEOWNERS rule covers workflows
    ``sec.codeowners.workflows`` (whether the default branch requires review
    from Code Owners), the two environment facts
    ``sec.environments.branch-restricted`` and
    ``sec.environments.approval-holds``, and
    ``sec.secrets.environment-scoped`` (with the per-environment verdicts and
    the secret-name list in the block's ``push_access`` key). It is optional, and
    its absence is disclosed as UNMEASURED rather than skipped — a fact that
    quietly vanishes from the table is a silent pass.

    ``required_contexts_fetcher``, ``fork_approval_fetcher``,
    ``codeowner_review_fetcher`` and ``push_access_fetcher`` are the seams the
    tests inject a recorded response through, defaulting to
    ``_required_contexts_via_gh``, ``_fork_approval_via_gh``,
    ``_code_owner_review_via_gh`` and ``_push_access_via_gh``; nothing in the
    suite touches the network.
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

    co_outcome, co_evidence = _codeowners_enforced(
        root, repo, codeowner_review_fetcher or _code_owner_review_via_gh)
    add("sec.codeowners.workflows",
        "a CODEOWNERS entry covers `.github/workflows/` and the default "
        "branch enforces it (requires review from Code Owners)",
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

    swallowed: list[str] = []
    any_suite = False
    unidentified: list[str] = []
    for rel, doc in docs:
        offences, saw_suite, unknown = _suite_failure_swallowed(rel, doc)
        swallowed += offences
        any_suite = any_suite or saw_suite
        unidentified += unknown
    # Three ways this fact ends without a verdict, and they are not the same
    # claim. A suite ran: pass or fail. No suite ran, but something threw a
    # status away that this scan could not identify: a COVERAGE GAP, named as
    # one. Nothing ran and nothing was discarded: genuinely not applicable.
    gap_outcome = ("unmeasured: no recognised test or lint suite, but %d "
                   "step(s) discard an exit status this scan could not "
                   "identify (%s) — one of them may be a suite, and this "
                   "fact cannot tell; a COVERAGE GAP, not a clean result"
                   % (len(unidentified), _capped(unidentified, 3, "; ")))
    add("sec.gate.test-failure-fatal",
        "no job that runs the test or lint suite swallows its own exit code "
        "(a suite whose failure cannot fail its job leaves the merge gate "
        "green whatever the tests did)",
        True, not swallowed,
        (_capped(swallowed, 4) if swallowed else
         ("no test or lint step this scan recognises discards its exit "
          "status" if any_suite else
          gap_outcome if unidentified else
          "not applicable: no workflow that can report a check on a pull "
          "request runs a test, lint, or build-verification suite this scan "
          "recognises, and no step discards an exit status, so there is no "
          "merge gate here whose result could be thrown away")),
        # Applicability, following the shape ci-score's test-sharding check
        # uses: a repository whose workflows run no recognised suite has
        # nothing to swallow. That is NOT a pass — a free green for having no
        # tests would reward the very absence the fact is about — and it is
        # not unmeasured either, which claims a coverage gap where there is
        # none. It leaves the denominator, exactly as an n/a check does in
        # ci-score.
        outcome=(None if any_suite else
                 "unmeasured" if unidentified else "not_applicable"))

    add("sec.checkout.credentials-scoped",
        "on untrusted-trigger workflows, every checkout sets "
        "persist-credentials: false (GitHub's default persists the token into "
        ".git/config where later steps can read it)",
        True, not persist,
        "no untrusted-trigger checkout persists credentials"
        if not persist else _capped(persist, 4))

    cd_outcome, cd_evidence = _update_cooldown(root)
    add("sec.deps.update-cooldown",
        "dependency update tools wait at least %d days before proposing a "
        "fresh release (Dependabot `cooldown`, Renovate `minimumReleaseAge`), "
        "so a hijacked or typosquatted version is usually pulled before CI "
        "installs it; security updates are not delayed (Dependabot's "
        "cooldown and Renovate's `minimumReleaseAge` both let them through "
        "at once)" % _COOLDOWN_MIN_DAYS,
        False, cd_outcome == "pass", cd_evidence, outcome=cd_outcome)

    # Environment settings, not workflow YAML: a scan gap says nothing about
    # them. The default fetcher is looked up at call time so a test can
    # replace it by name.
    try:
        (env_outcome, env_evidence), (ap_outcome, ap_evidence), push_block = \
            _push_access(repo, push_access_fetcher or _push_access_via_gh)
    except Exception as exc:                                   # noqa: BLE001
        # A field the verdict code cannot handle costs these three checks,
        # with the reason, never the whole fact table.
        _log.warning("push access verdicts failed (%s); reported as "
                     "unmeasured", type(exc).__name__)
        _log.debug("push access verdicts failed", exc_info=True)
        reason = ("the environment and secret settings could not be judged "
                  f"({exc!r})")
        env_outcome = ap_outcome = "unmeasured"
        env_evidence = ap_evidence = f"unmeasured: {reason}"
        push_block = _not_checked_block(reason)
    add(_BRANCH_FACT,
        "every deployment environment either accepts only branches and tags "
        "a non-admin push credential can neither create nor move, or "
        "requires a reviewer (an environment's name is also what cloud "
        "logins trust, so one holding no secrets counts too)",
        False, env_outcome == "pass", env_evidence, outcome=env_outcome)
    add(_APPROVAL_FACT,
        "every deployment environment with required reviewers stops whoever "
        "started a run from approving it themselves",
        False, ap_outcome == "pass", ap_evidence, outcome=ap_outcome)
    sc_outcome, sc_evidence = _secrets_scoped(push_block["inventory"])
    add(_SECRETS_FACT,
        "no repository or organisation secret is readable from any branch: "
        "every secret this repository uses lives in an environment (a "
        "pushed branch can read any secret that is not)",
        False, sc_outcome == "pass", sc_evidence, outcome=sc_outcome)
    push_block["token_note"] = _TOKEN_NOTE

    out = facts_to_score(facts)
    # Unscored: the per-environment verdicts, the secret-name inventory, and
    # the two sentences every run states. Rendered under the report's "If a
    # push credential leaks" sub-heading; never part of the aggregate.
    out["push_access"] = push_block
    return out


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
    # NOT APPLICABLE IS NOT A COVERAGE GAP. An unmeasured fact stays in the
    # applicable count as a named gap ("this could not be checked"); a fact
    # that does not apply to this repository ("there is nothing here to
    # check") leaves the count entirely, the same treatment ci-score gives an
    # n/a check. Collapsing the two would either invent a gap or hand out a
    # free pass, and both distort the aggregate ci-advisor blends.
    not_applicable = [f["fact_id"] for f in facts
                      if f["outcome"] == "not_applicable"]
    applicable = [f for f in facts if f["outcome"] != "not_applicable"]
    passed = sum(1 for f in scored if f["outcome"] == "pass")
    out: dict[str, Any] = {
        "facts": facts,
        "score": round(100.0 * passed / len(scored), 1) if scored else None,
        "passed": passed,
        "scored_count": len(scored),
        "applicable_count": len(applicable),
        "unmeasured": unmeasured,
        "not_applicable": not_applicable,
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
            % (len(scored), len(applicable), ", ".join(unmeasured)))
    return out

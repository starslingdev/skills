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

THE SIX FACTS

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

F4 replaces "has a dangerous trigger", which is true of 84% of corpus repos
and so discriminates nobody. The tiering is deliberate and keeps the
findings-only rule intact: a bare untrusted trigger is ignored (too common to
mean anything); a trigger plus a checkout of the attacker's head FAILS THIS FACT
(the fork-code detector cannot clear the trigger as inert); the full chain —
trigger + head checkout + execution — is the P14.9 FINDING, which stays out of
this number. P14.9's own docstring reserves the middle tier for exactly this
fact ("the bare trigger without the head checkout ... belongs to the scored
config checks").

WHAT IS NEVER A SILENT PASS. Facts F1/F2/F4/F5/F6 are universal claims over
every workflow file, so ANY unscannable workflow (`scan_incomplete`) forces
them to UNMEASURED — no pass, no fail, a stated reason, and they stay in the
applicable count as a visible coverage gap (the same shape ci-speedup's
speed_score uses). F3 reads repo files, not workflow YAML, and is unaffected.

THE REGISTERED SCORE. `security_score = 100 x passed / scored`, no weights, no
partial credit — registered before any calibration run (never-tune), with the
constants emitted alongside the number so a disputed score is checkable
without reading this source.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

REGISTERED = "2026-08-03"

_UNTRUSTED_TRIGGERS: frozenset[str] = frozenset()   # bound from scan at load


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
    # a false clean on the exact claim this fact makes. A partial glob
    # (`*.yml`) is still accepted as directory-wide intent, even though a
    # `.yaml` workflow would slip it — failing a correctly-configured repo over
    # that costs more than it catches.
    r"^\s*/?\.github/workflows/(?:\*\S*)?(?:\s|$)",
    # `.github/**` recurses and covers workflows; single-star `.github/*` does
    # NOT (gitignore semantics: `*` doesn't cross `/`) and is deliberately
    # absent — including it produced false negatives.
    r"^\s*/?\.github/\*\*",
    # GAP-49: the standard recursive DIRECTORY form `.github/ @team` covers
    # everything under .github/, workflows included (CODEOWNERS paths are
    # prefix rules; a trailing slash names the tree). Its absence graded
    # correctly-configured repos down 1/6.
    r"^\s*/?\.github/\s",
    # Bare `*` global owner, followed by whitespace or EOL — a bare `^\s*\*`
    # would also match extension rules like `*.go @team`, which do not cover
    # workflow files.
    r"^\s*\*(?:\s|$)",
)


def _codeowners_covers_workflows(root: Path) -> tuple[bool, str]:
    found: Path | None = None
    for candidate in _CODEOWNERS_CANDIDATES:
        if (root / candidate).is_file():
            found = root / candidate
            break
    if found is None:
        return False, ("no CODEOWNERS file at .github/CODEOWNERS, CODEOWNERS, "
                       "or docs/CODEOWNERS, so workflow changes merge with "
                       "the same approvals as any other change")
    try:
        text = found.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"{found.name} unreadable: {exc}"
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        for pat in _WORKFLOWS_CODEOWNER_PATTERNS:
            if re.match(pat, line):
                rel = str(found.relative_to(root))
                return True, f"`{rel}` covers `.github/workflows/`"
    rel = str(found.relative_to(root))
    return False, (f"`{rel}` has no entry covering `.github/workflows/`")


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

    GAP-48: `permissions:` with a NULL value is treated by GitHub as omitted —
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


# --- the fact table -----------------------------------------------------------

def compute_config_facts(
    root: Path,
    workflow_files: list[Path],
    scan_incomplete: list[dict[str, str]],
) -> dict[str, Any]:
    """Every fact, every time — pass/fail/unmeasured, never silently absent."""
    scan = _scan()

    docs: list[tuple[str, dict]] = []
    for wf in workflow_files:
        text = scan._read_text_safe(wf)
        # quiet: the scan pass already reported (and recorded as a coverage
        # gap) every file that will not parse.
        doc = scan._parse_yaml_text(text, wf, quiet=True) if text else None
        if isinstance(doc, dict):
            docs.append((str(wf.relative_to(root)), doc))

    # Universal facts cannot be asserted over a partially readable set: a
    # workflow we could not parse could be the one that fails them.
    gap = bool(scan_incomplete)
    gap_reason = ("unmeasured: %d workflow file(s) could not be scanned "
                  "(%s), and this fact is a claim about every workflow"
                  % (len(scan_incomplete),
                     _capped([g["workflow_file"] for g in scan_incomplete],
                             3, ", "))) if gap else ""

    facts: list[dict[str, Any]] = []

    def add(fact_id: str, fact: str, workflow_scoped: bool,
            passed: bool, evidence: str) -> None:
        if workflow_scoped and gap:
            facts.append({"fact_id": fact_id, "fact": fact,
                          "outcome": "unmeasured", "evidence": gap_reason})
            return
        facts.append({"fact_id": fact_id, "fact": fact,
                      "outcome": "pass" if passed else "fail",
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

    covered, co_evidence = _codeowners_covers_workflows(root)
    add("sec.codeowners.workflows",
        "a CODEOWNERS entry covers `.github/workflows/`",
        False, covered, co_evidence)

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
        if not _untrusted_triggers_of(scan, doc):
            continue
        jobs = _unpersisted_checkout_violations(doc)
        if jobs:
            persist.append(f"{rel}: job(s) {_capped(jobs, 3, ', ')}")
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

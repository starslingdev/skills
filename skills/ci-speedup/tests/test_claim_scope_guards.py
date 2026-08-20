"""Class-level guard for ci-speedup *scope* claims (the §③(c) claims guard).

`test_evidence_claim_guards.py` invariant 1 already locks one claim class: no
finding's evidence may NAME a trigger absent from the workflow's `on:` block.
This module locks the PR-SCOPE claim class the audit (SUMMARY §③) and the
dogfood fleet (Class B — evidence ≠ claim) kept re-surfacing:

  PR-SCOPE over-claim — a finding says a check runs on "every PR" when the
  workflow only triggers `pull_request` behind a `paths:` filter (so it runs on
  a SUBSET of PRs) or doesn't trigger on PRs at all. Naming the trigger is
  guarded already; over-stating the REACH of a present trigger was not. The three
  PR-scope detectors (OPT33/OPT39/OPT40) each fixed this per-detector by scoping
  the wording with `_on_has_paths_filter` / `_pr_trigger_has_paths`; this guard
  asserts the INVARIANT across EVERY detector so a future one can't regrow a bare
  "every PR".

This is a PROPERTY test: it scans adversarial workflow shapes and inspects EVERY
finding from EVERY detector that fires, so the next detector to regrow the claim
fails in CI instead of on a real repo.

NOT covered here (deliberately deferred): the REQUIRED-STATUS claim class. The
file-only scanner correctly HEDGES required status ("if this workflow is a
required check, …") rather than asserting it, so a static-scanner guard would
fight legitimate hedging. The claims that genuinely assert required / merge-
blocking status are made by the blocking-path RENDERER from the RESOLVED
required-checks data — so that guard belongs with the render-side seam-hole fix
(gap #2: `required_scoped` stamped on a non-merge-blocking pole), where the data
to make it non-fragile exists. Tracked as the follow-up.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL_DIR / "scripts"
_SCAN_SCRIPT = _SCRIPTS / "scan.py"


def _have_yaml() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import yaml"], capture_output=True, text=True,
    ).returncode == 0


def _scan(root: Path) -> dict:
    if not _have_yaml():
        pytest.skip("PyYAML not installed in the test runner")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _claim_text(f: dict) -> str:
    """All prose fields a PR-scope claim could hide in. This guard drives `scan.py`
    directly, whose finding JSON carries `evidence` + `title` (where the dynamic
    scope claim actually lives); the measured-evidence / size-note fields are added
    downstream by collect_runs and are inert here — read defensively so the guard
    still covers them if a future static detector starts emitting them. (Render-
    /measured-stage claims are out of scope here, same family as the deferred
    required-status half — see the module docstring.)"""
    me = f.get("measured_evidence") or {}
    return " ".join(str(x) for x in (
        f.get("evidence") or "",
        me.get("summary") or "", me.get("note") or "",
        f.get("size_note") or "", f.get("title") or "",
    ))


def _write(root: Path, name: str, body: str) -> None:
    wf_dir = root / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / name).write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Invariant A — no finding may claim an unqualified PR scope it doesn't have
# --------------------------------------------------------------------------- #
# A BARE PR-scope claim ("every PR", "all PRs", "on every pull request") that is
# NOT immediately qualified by a scoping connective. The negative lookahead lets
# the correct, scoped wording through ("every PR that changes the filtered paths",
# "every PR which touches …"); an optional adjective ("every single PR", "all of
# the PRs") between the quantifier and the noun is still caught.
_BARE_PR_SCOPE = re.compile(
    r"\b(?:every|all|each)\s+(?:\w+\s+){0,2}(?:PR|pull[\s-]?request)s?\b"
    r"(?!\s+(?:that|which|when|touching|matching|changing)\b)", re.I)

# Three job shapes, one per PR-scope detector, so the guard locks the invariant
# across EVERY detector that makes the claim — not just OPT33:
#   integration-test → OPT33 (matrix + services + test-named, no draft gate)
#   codeql           → OPT39 (language matrix scanner, no paths-filter gate)
#   web-app          → OPT40 (monorepo single-app target, no paths filter)
# (OPT40 also needs the repo to LOOK like a monorepo — the test creates an
# `apps/` dir below so `_is_monorepo` is true.)
_PR_SCOPE_JOBS = (
    "jobs:\n"
    "  integration-test:\n"
    "    runs-on: ubuntu-latest\n"
    "    services:\n"
    "      db:\n"
    "        image: postgres\n"
    "    strategy:\n"
    "      matrix:\n"
    "        python: ['3.11', '3.12']\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - run: pytest\n"
    "  codeql:\n"
    "    runs-on: ubuntu-latest\n"
    "    strategy:\n"
    "      matrix:\n"
    "        language: ['python', 'javascript']\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - uses: github/codeql-action/analyze@v3\n"
    "  web-app:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - run: cd apps/web && pytest\n"
)

# The PR-scope detectors that must stay locked. The unfiltered control proves
# each one actually emits its claim (so a future regression in any of them trips
# this guard, not just OPT33's).
_PR_SCOPE_DETECTORS = {"OPT33", "OPT39", "OPT40"}

# (label, on-block, may a bare "every PR" claim legitimately appear?)
_SCOPE_FIXTURES = {
    # pull_request gated by paths → runs on a SUBSET of PRs; bare "every PR" is a lie.
    "pr-paths-filtered": ("on:\n  pull_request:\n    paths: ['src/**']\n", False),
    # push-only → runs on ZERO PRs; no "every PR" claim may appear at all.
    "push-only": ("on:\n  push:\n    branches: [main]\n", False),
    # bare pull_request → genuinely every PR; the claim is TRUE and expected (the
    # non-vacuity control — every PR-scope detector must emit its claim here).
    "pr-unfiltered": ("on:\n  pull_request:\n", True),
}


@pytest.mark.parametrize("label", sorted(_SCOPE_FIXTURES))
def test_no_finding_overclaims_pr_scope(tmp_path: Path, label: str):
    on_block, bare_ok = _SCOPE_FIXTURES[label]
    _write(tmp_path, "ci.yml", "name: CI\n" + on_block + _PR_SCOPE_JOBS)
    (tmp_path / "apps" / "web").mkdir(parents=True, exist_ok=True)  # → monorepo (OPT40)
    data = _scan(tmp_path)
    bare = [f for f in data["findings"] if _BARE_PR_SCOPE.search(_claim_text(f))]
    bare_hits = [f"{f['pattern']}: {_claim_text(f)[:160]}" for f in bare]
    if bare_ok:
        # Non-vacuity control: on a genuinely-every-PR workflow EVERY PR-scope
        # detector must emit its claim, or this guard is testing nothing (and the
        # "class-wide across OPT33/OPT39/OPT40" promise would be hollow).
        fired = {f["pattern"] for f in bare}
        missing = _PR_SCOPE_DETECTORS - fired
        assert not missing, (
            f"the unfiltered-PR control did not emit a bare 'every PR' claim from "
            f"{sorted(missing)} — those PR-scope detectors didn't fire, so the guard "
            f"isn't actually locking them; refresh _PR_SCOPE_JOBS")
    else:
        assert not bare_hits, (
            f"[{label}] a finding claims an unqualified PR scope on a workflow that "
            f"does NOT run on every PR (paths-filtered or non-PR trigger):\n  "
            + "\n  ".join(bare_hits))


def test_bare_pr_scope_regex_distinguishes_qualified_from_bare():
    """Pin the matcher so a refactor can't make it over- or under-match: bare
    claims trip (including an adjective between the quantifier and the noun), while
    the scoped wording the detectors actually emit — under any of the accepted
    qualifying connectives — does not."""
    for bare in (
        "runs on every PR regardless of which app changed",
        "runs on every pull request including drafts",
        "an advisory check on all PRs",
        "runs for all pull requests",
        "every single PR",          # adjective between quantifier and noun
        "all of the PRs",
    ):
        assert _BARE_PR_SCOPE.search(bare), bare
    for qualified in (
        "every PR that changes the workflow's filtered `paths:`",
        "every PR which touches the filtered paths",
        "every PR matching the path filter",
    ):
        assert not _BARE_PR_SCOPE.search(qualified), qualified


def test_the_methodology_states_its_storage_boundary():
    """A third scope-claim class, held in prose rather than in a detector: what
    the skill's savings are DENOMINATED in. Every finding is sized in runner
    minutes and wall-clock seconds; artifact and cache storage is a separate
    line on the GitHub bill and is deliberately out of scope. That decision
    lives only in `references/savings-methodology.md`, which carries no
    substance floor of its own, so the paragraph can vanish silently and leave
    a reader to infer the boundary from silence — the same silence the
    paragraph was written to end. This pins the claim, not its wording beyond
    the load-bearing phrases.
    """
    text = (_SKILL_DIR / "references" / "savings-methodology.md").read_text(
        encoding="utf-8")
    assert "Storage is deliberately out of scope" in text, (
        "savings-methodology.md no longer states that storage is out of scope "
        "— the two-axis denomination is back to being implied, and a reader "
        "cannot tell an omission from a decision")
    assert "sized in\ngigabytes" in text or "sized in gigabytes" in text, (
        "savings-methodology.md dropped the 'no finding is sized in gigabytes' "
        "half of the storage boundary")
    assert "netted the other way" in text, (
        "savings-methodology.md dropped the direction a user actually asks "
        "about: storage a fix ADDS is not subtracted from the minutes it saves")

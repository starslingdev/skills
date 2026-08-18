"""Freshness/verify gate for the shipped worked examples under ``examples/``.

The two committed worked examples (``examples/<repo>/ci-speedup-findings-report.md``)
are the front-door artifacts a public reader opens first, yet — unlike the
``skills/ci-speedup/reports/`` corpus, which ``test_committed_reports.py`` fresh-renders
and verifies — **nothing verified them** (until this guard). (Confirmed: every corpus guard globs
``skills/ci-speedup/reports/``, which this public repo does not ship, so they all skip;
``examples/`` sat outside every gate.) A hand-edited or stale example could therefore
ship a fabricated / typo'd / squashed-away provenance SHA with no test catching it —
the exact rot a recent by-hand fix (``examples: fix microsoft-playwright provenance sha
to a resolvable on-main commit``) had to repair manually.

This guard closes that gap **cheaply and without re-rendering** (no engine run, no ``gh``
calls): it pins each example's stamped ``ci-speedup skill commit `<sha>` `` provenance to
a real commit **already reachable from the base branch**, and checks the SHA is internally
consistent across the report. CI checks out ``fetch-depth: 0`` precisely so this ancestry
check can resolve historical skill commits; on a shallow clone (where the history isn't
present) the ancestry leg SKIPS LOUDLY rather than false-failing — the well-formedness /
consistency legs still run.

**Base branch, not HEAD** (see ``_mainline_ref``). The ancestry leg originally resolved the
stamp against ``HEAD``, which a PR branch satisfies trivially with a commit of its own — so
the guard was green for exactly as long as the problem existed and could only fail after the
merge that caused it. That is not hypothetical: #60 was squash-merged, its examples stamped
``553baad`` (a commit that lived only on the PR branch), the squash discarded it, ``main``
went red on the next run and #64 had to repair the artifacts by hand. Anchoring to the base
branch moves the failure onto the PR, before the merge. The cost is deliberate: an example
can no longer be generated from an in-flight branch commit and landed in the same PR — when
an engine change and an example change coincide, the example is regenerated in a follow-up
PR once the engine has landed. A branch-commit stamp is precisely what a squash discards,
so forbidding it IS the fix.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

import pytest

_REPO = Path(__file__).resolve().parents[1]
_EXAMPLES = _REPO / "examples"

# The Audit-row provenance stamp: "ci-speedup skill commit [`45479c6`](…)".
_AUDIT_SHA_RE = re.compile(r"ci-speedup skill commit \[`([0-9a-f]{7,40})`\]")
# Archive-era stamp: examples generated BEFORE the repo's public cut-over carry a plain
# (unlinked) SHA marked "(pre-public archive)" — that commit lives in the maintainers'
# private archive, not this repository's history, so the ancestry leg cannot apply.
# The marker is only legal for reports whose Audit `ran` date predates the cut-over
# (_ARCHIVE_CUTOFF); a LATER report claiming it is a stale-example dodge and FAILS.
# Anchored to the Audit ROW (date and stamp parsed from the same line), not the whole
# document — stray archive-marker text elsewhere in a report (a prompt block, quoted
# prose) must not be able to activate the archive branch (#134 review, greptile P1).
_AUDIT_ARCHIVE_ROW_RE = re.compile(
    r"\*\*Audit\*\* \| ran (\d{4}-\d{2}-\d{2}) · "
    r"ci-speedup skill commit `([0-9a-f]{7,40})` \(pre-public archive\)")
_ARCHIVE_CUTOFF = "2026-07-23"  # first public day: no archive-stamped example may postdate it
# Every "skill commit `<sha>`" reference in the body (Audit row + the static-scan table
# row), whether or not it is a markdown link — used for the internal-consistency check.
_ANY_SKILL_SHA_RE = re.compile(r"skill commit \[?`([0-9a-f]{7,40})`")


def _example_reports() -> list[Path]:
    return sorted(_EXAMPLES.glob("*/ci-speedup-findings-report.md"))


def _run_git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(_REPO), *args],
        capture_output=True, text=True,
    )


def _history_available() -> tuple[bool, str]:
    """True only on a git checkout that actually contains the mainline history — so a
    non-resolving SHA means a bad stamp, not a missing object."""
    if _run_git("rev-parse", "--git-dir").returncode != 0:
        return False, "not a git checkout"
    shallow = _run_git("rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0:
        # git couldn't answer (git < 2.15 predates --is-shallow-repository, or a transient
        # error). Reading its stdout unconditionally would fall through to "history available"
        # and risk false-FAILING a valid stamp on a shallow old-git clone — the exact opposite
        # of the docstring's promise. Treat an unanswerable probe as inconclusive → loud skip.
        return False, ("cannot determine shallowness — "
                       f"`git rev-parse --is-shallow-repository` failed: {shallow.stderr.strip()!r}")
    if shallow.stdout.strip() == "true":
        return False, ("shallow checkout — full history (fetch-depth: 0) is needed to "
                       "resolve historical skill commits; CI runs full-depth")
    return True, ""


def _resolves(rev: str) -> bool:
    return _run_git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").returncode == 0


def _mainline_ref() -> tuple[str, str]:
    """The ref an example's stamped engine commit must ALREADY be reachable from.

    This is deliberately NOT ``HEAD``. Resolving against ``HEAD`` is what made the
    ancestry leg unable to protect the merge that broke it: on a PR branch, a commit
    that exists only on that branch IS an ancestor of HEAD, so the guard stayed green
    for the whole life of the PR and only went red on ``main`` after the squash had
    already discarded the stamped commit (#60 → the by-hand repair in #64). Anchoring
    to the BASE BRANCH inverts that timing — the stamp must name a commit that has
    already landed, so the violation surfaces on the PR, pre-merge, with no new
    mechanism and no post-merge cleanup.

    The tip of the base branch is the reference, not the merge base with it: a commit
    that landed on ``main`` after this branch was cut is perfectly legitimate to stamp
    (a squash cannot discard it), and the merge base would reject it.

    Resolution order, first hit wins:
      1. ``GITHUB_BASE_REF`` — the PR's target branch, as the remote-tracking ref or a
         local branch. Set only on ``pull_request`` runs.
      2. ``origin/main`` / ``main`` / ``origin/HEAD`` — a normal clone, a worktree, and
         the ``push``-to-main CI run (actions/checkout writes ``origin/main`` there).
      3. The first parent of HEAD on a GitHub ``pull_request`` run. actions/checkout
         checks out ``refs/pull/N/merge`` and fetches ONLY that ref, so no
         ``origin/<base>`` exists in a PR job however deep the fetch; the merge commit
         GitHub generated for it has the base branch tip as its first parent. Guarded
         on the event being a pull_request AND HEAD actually being a two-parent merge,
         so an ordinary local merge commit can never be mistaken for a base ref.

    Returns ``(ref, "")`` or ``("", reason)``.
    """
    tried: list[str] = []
    base_env = os.environ.get("GITHUB_BASE_REF", "").strip()
    candidates: list[str] = []
    if base_env:
        candidates += [f"refs/remotes/origin/{base_env}", f"refs/heads/{base_env}"]
    candidates += ["refs/remotes/origin/main", "refs/heads/main", "refs/remotes/origin/HEAD"]
    for rev in candidates:
        tried.append(rev)
        if _resolves(rev):
            return rev, ""
    if base_env and os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        tried.append("HEAD^1 (the pull_request merge ref's base parent)")
        parents = _run_git("rev-list", "--parents", "-n", "1", "HEAD")
        if (parents.returncode == 0 and len(parents.stdout.split()) == 3
                and _resolves("HEAD^1")):
            return "HEAD^1", ""
    return "", ("no base-branch ref could be resolved (tried: " + ", ".join(tried) + ")")


def test_examples_corpus_is_present():
    # Never let this whole gate pass vacuously: if the shipped examples disappear, say so
    # LOUDLY (a silently-empty corpus is how a freshness gate rots into a no-op).
    reports = _example_reports()
    assert reports, (
        f"no worked-example reports under {_EXAMPLES} — the examples freshness gate has "
        "nothing to verify; either the examples were removed or the glob drifted"
    )


@pytest.mark.parametrize("report", _example_reports(), ids=lambda p: p.parent.name)
def test_example_provenance_stamp_is_wellformed_and_consistent(report: Path):
    text = report.read_text(encoding="utf-8")
    m = _AUDIT_SHA_RE.search(text)
    arch = _AUDIT_ARCHIVE_ROW_RE.search(text)
    assert m or arch, (
        f"{report.parent.name}: no `ci-speedup skill commit \\`<sha>\\`` provenance stamp "
        "(linked, or Audit-row plain + '(pre-public archive)') found — a worked "
        "example must record the engine commit it was generated from so its freshness "
        "can be audited"
    )
    audit_sha = m.group(1) if m else arch.group(2)
    # Every skill-commit reference in the report must name the SAME commit — a divergent
    # static-scan SHA is exactly the hand-edit drift the recent by-hand fix repaired.
    all_shas = set(_ANY_SKILL_SHA_RE.findall(text))
    assert all_shas == {audit_sha}, (
        f"{report.parent.name}: report cites multiple skill-commit SHAs {sorted(all_shas)} "
        f"but the Audit row stamps {audit_sha!r} — the provenance is internally inconsistent"
    )


@pytest.mark.parametrize("report", _example_reports(), ids=lambda p: p.parent.name)
def test_example_provenance_sha_is_a_real_ancestor_commit(report: Path):
    text = report.read_text(encoding="utf-8")
    arch = _AUDIT_ARCHIVE_ROW_RE.search(text)
    if arch:
        # Archive-era Audit row: the commit predates the public cut-over and lives only
        # in the maintainers' private archive — ancestry is unverifiable here BY DESIGN.
        # The marker keeps its teeth via the date bound, parsed from the SAME row: a
        # report generated on/after the first public day claiming "pre-public archive"
        # is a stale-example dodge. This is a pure text check, so it runs BEFORE the
        # git-history gate — it must fail even on a shallow checkout.
        ran, sha = arch.group(1), arch.group(2)
        assert ran < _ARCHIVE_CUTOFF, (
            f"{report.parent.name}: Audit row claims '(pre-public archive)' but the "
            f"report ran {ran}, on/after the public cut-over ({_ARCHIVE_CUTOFF}) — a "
            "post-cut example must stamp a linked, on-main engine commit; regenerate it"
        )
        pytest.skip(f"{report.parent.name}: archive-era stamp `{sha}` "
                    f"(ran {ran}, pre-cut) — ancestry lives in the private archive")
    m = _AUDIT_SHA_RE.search(text)
    assert m, f"{report.parent.name}: no provenance stamp (see the well-formedness test)"
    # ONE ancestry implementation for every engine family (this leg used to carry its own
    # copy, so the base-ref fix would have had to be made — and kept — in two places).
    # It resolves the stamp against the BASE BRANCH, not HEAD, and distinguishes git's exit
    # codes: rc 0 = reachable, rc 1 = the provenance violation, rc >= 2 = git could not
    # answer at all (bad revision, object pruned mid-run) and skips loudly rather than
    # mislabelling an environment error as "regenerate this example".
    _assert_engine_sha_is_an_ancestor(report, m.group(1))


def test_provenance_ancestry_guard_actually_rejects_a_fabricated_sha(tmp_path, monkeypatch):
    # RED-PROOF (each of this PR's two sibling new guards ships one; this closes the gap that
    # the ancestry/resolvability leg had none). A fabricated-but-well-formed-and-consistent stamp
    # PASSES the well-formedness/consistency leg, and then the two real-commit legs are pinned by
    # SIMULATING the exact git exit codes each is meant to act on — hermetically, no throwaway
    # objects in the real repo. This nails the assertion DIRECTION: a refactor that inverted a
    # return-code check (`== 0` for `!= 0`), dropped the `"commit"` clause, or mislabeled a git
    # environment error as a provenance FAIL would green a bad stamp, and this goes red at once.
    import subprocess
    import sys
    mod = sys.modules[__name__]

    fake = "dead" * 10  # 40 hex chars: well-formed, self-consistent
    d = tmp_path / "fabricated-example"
    d.mkdir()
    report = d / "ci-speedup-findings-report.md"
    report.write_text(
        f"**Audit:** ci-speedup skill commit [`{fake}`](https://example.invalid/x) — and the "
        f"static-scan row cites skill commit `{fake}` too.\n", encoding="utf-8")
    # The fabricated stamp is well-formed + internally consistent, so ONLY the real-commit legs
    # below can reject it (not a malformed-stamp accident).
    test_example_provenance_stamp_is_wellformed_and_consistent(report)

    def _cp(returncode: int, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)

    def _fake_git(cat, mb):
        def run(*args: str):
            if args[0] == "rev-parse" and "--git-dir" in args:
                return _cp(0, ".git\n")
            if args[0] == "rev-parse" and "--is-shallow-repository" in args:
                return _cp(0, "false\n")   # force the ancestry leg to actually run
            if args[0] == "rev-parse" and "--verify" in args:
                # A resolvable base branch, so the legs below reach their real verdict
                # instead of skipping on an unresolvable base.
                return _cp(0, "beef" * 10 + "\n")
            if args[0] == "cat-file":
                return cat
            if args[:2] == ("merge-base", "--is-ancestor"):
                return mb
            return _cp(0, "")
        return run

    # Leg 1 (resolvability): cat-file reports "not a valid object" (rc 128) → loud FAIL.
    monkeypatch.setattr(mod, "_run_git",
                        _fake_git(_cp(128, "", "fatal: Not a valid object name"), _cp(0)))
    with pytest.raises(AssertionError):
        test_example_provenance_sha_is_a_real_ancestor_commit(report)

    # Leg 2 (ancestry verdict): a real commit (cat-file rc 0 'commit') that is genuinely NOT an
    # ancestor (merge-base rc 1) → loud FAIL. This is the provenance violation the leg exists for.
    monkeypatch.setattr(mod, "_run_git", _fake_git(_cp(0, "commit\n"), _cp(1)))
    with pytest.raises(AssertionError):
        test_example_provenance_sha_is_a_real_ancestor_commit(report)

    # Leg 2, environment error (merge-base rc 128) must SKIP loudly, never be mislabeled a FAIL —
    # pins the rc-1-vs-rc>=2 split so a git error can't read as "regenerate from an on-main commit".
    monkeypatch.setattr(mod, "_run_git",
                        _fake_git(_cp(0, "commit\n"), _cp(128, "", "fatal: bad revision")))
    with pytest.raises(pytest.skip.Exception):
        test_example_provenance_sha_is_a_real_ancestor_commit(report)


def test_archive_stamp_is_date_bounded(tmp_path, monkeypatch):
    # RED-PROOF for the archive leg: "(pre-public archive)" is an escape hatch from the
    # ancestry check, so it must be date-bounded or any future stale example could dodge
    # the gate by adding the marker. Pre-cut ran-date → loud SKIP (legit archive era);
    # post-cut ran-date → loud FAIL (regenerate from an on-main commit).
    import sys
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "_history_available", lambda: (True, ""))

    def _write(ran: str) -> Path:
        d = tmp_path / f"archive-{ran}"
        d.mkdir()
        p = d / "ci-speedup-findings-report.md"
        p.write_text(
            f"| **Audit** | ran {ran} · ci-speedup skill commit `3bb6e2e` "
            "(pre-public archive) |\n"
            "| static scan (skill commit `3bb6e2e`, ...) |\n", encoding="utf-8")
        return p

    pre = _write("2026-07-21")
    test_example_provenance_stamp_is_wellformed_and_consistent(pre)  # archive form is well-formed
    with pytest.raises(pytest.skip.Exception):
        test_example_provenance_sha_is_a_real_ancestor_commit(pre)

    post = _write("2026-08-01")
    with pytest.raises(AssertionError):
        test_example_provenance_sha_is_a_real_ancestor_commit(post)

    # Anchoring (#134 review): a stray archive marker in BODY text (a prompt block,
    # quoted prose) beside a normal linked Audit row must NOT activate the archive
    # branch — the row regex only matches the Audit row itself.
    stray = ("| **Audit** | ran 2026-08-01 · ci-speedup skill commit "
             "[`deadbeef`](https://example.invalid/x) |\n"
             "Body prose quoting a stamp: ci-speedup skill commit `3bb6e2e` "
             "(pre-public archive) — not an Audit row.\n")
    assert not _AUDIT_ARCHIVE_ROW_RE.search(stray.splitlines()[1])
    assert _AUDIT_ARCHIVE_ROW_RE.search(stray) is None  # whole-doc: still no row match
    assert _AUDIT_SHA_RE.search(stray)                   # the linked stamp is what parses


# ---------------------------------------------------------------------------
# The other engines' worked examples (ci-secure, ci-score).
#
# The legs above glob ONLY ``*/ci-speedup-findings-report.md``. When the ci-score
# and ci-secure examples landed they fell outside every one of them: no provenance
# pin, no sanitization check, no report-versus-findings cross-check — an example
# nobody would notice drifting (greptile P2 on #60). These legs extend the same
# cheap, no-re-render model to those families, and ``test_every_example_report_
# belongs_to_a_guarded_family`` below makes the omission itself impossible to
# repeat: a NEW example family added with no guard goes red here rather than
# silently slipping through a stale glob.
# ---------------------------------------------------------------------------

# ci-secure stamps its engine commit in the report's Scanner row together with the
# finding count, e.g. "| **Scanner** | ci-secure (skill commit `a43d237`) — 3 finding(s) |".
_SECURE_SCANNER_RE = re.compile(
    r"ci-secure \(skill commit `([0-9a-f]{7,40})`\) [-—] (\d+) finding\(s\)")
_SECURE_AUDITED_COMMIT_RE = re.compile(r"\*\*Audited commit\*\* \| `([0-9a-f]{7,40})`")
# ci-score's report header links the scored commit, and the card restates the score.
_SCORE_COMMIT_RE = re.compile(r"\*\*Scored commit\*\* \| \[`([0-9a-f]{7,40})`\]")
_SCORE_CARD_RE = re.compile(
    r"## CI Score: \*\*(\d+)/100\*\* [-—] (\d+) of (\d+) applicable checks")

# Local-path leakage: the examples' stated sanitization is "only local filesystem
# paths were stripped, replaced with /local/…". Anything that still names a real
# home directory is an un-sanitized artifact.
# ``/home/runner`` is GitHub's own hosted-runner path and legitimately appears inside
# quoted job-log evidence, so it is NOT a leak; a maintainer's own home directory is.
_LOCAL_PATH_RE = re.compile(r"(?:/Users/|/home/(?!runner\b)[a-z]|C:\\Users\\)")


def _rel(path: Path) -> str:
    """Repo-relative path for messages, tolerating the hermetic tmp copies the
    red-proof feeds these legs (which live outside the repo)."""
    try:
        return str(path.relative_to(_REPO))
    except ValueError:
        return str(path)

# Every committed example report must be claimed by exactly one guarded family.
_REPORT_FAMILIES = {
    "ci-speedup": "ci-speedup-findings-report.md",
    "ci-score": "ci-score-report.md",
    "ci-secure": "ci-secure-report-*.md",
}


def _all_example_reports() -> list[Path]:
    return sorted(p for p in _EXAMPLES.glob("*/*.md") if p.name != "README.md")


def _secure_reports() -> list[Path]:
    return sorted(_EXAMPLES.glob("*/ci-secure-report-*.md"))


def _score_reports() -> list[Path]:
    return sorted(_EXAMPLES.glob("*/ci-score-report.md"))


def _load_json(path: Path):
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_example_report_belongs_to_a_guarded_family():
    # The durable fix for the drift that let ci-score/ci-secure in unguarded: a
    # future engine's example (or a renamed report) that no leg below covers fails
    # HERE, naming itself, instead of shipping unverified behind a stale glob.
    import fnmatch
    unguarded = [
        _rel(p) for p in _all_example_reports()
        if not any(fnmatch.fnmatch(p.name, pat) for pat in _REPORT_FAMILIES.values())
    ]
    assert not unguarded, (
        f"example report(s) match no guarded family {sorted(_REPORT_FAMILIES.values())}: "
        f"{unguarded} — they would ship with no provenance, sanitization, or "
        "report-vs-findings check. Add a family + legs to this guard."
    )
    assert _all_example_reports(), (
        f"no example reports under {_EXAMPLES} — this gate has nothing to verify"
    )


@pytest.mark.parametrize("report", _all_example_reports(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_example_artifacts_carry_no_local_filesystem_paths(report: Path):
    # The sanitization the examples' READMEs promise, made a test. Covers the
    # sibling findings JSON too — a raw JSON is the likeliest place for an
    # un-stripped absolute path to survive review.
    for path in [report, *sorted(report.parent.glob("*.json"))]:
        leaked = [
            f"{_rel(path)}:{i}" for i, line in
            enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if _LOCAL_PATH_RE.search(line)
        ]
        assert not leaked, (
            f"un-sanitized local filesystem path(s) at {leaked} — worked examples must "
            "replace the generating machine's paths with `/local/…` before shipping"
        )


@pytest.mark.parametrize("report", _secure_reports(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_ci_secure_example_matches_its_findings_json(report: Path):
    # Report-vs-findings consistency: the headline finding count in the Scanner row is
    # the number a reader quotes, so it must equal the raw `findings` array it claims to
    # summarize. A hand-tidied count (3 -> 2, or 0 -> "clean") goes red here.
    tag = report.stem[len("ci-secure-report-"):]
    findings_json = report.parent / f"ci-secure-findings-{tag}.json"
    assert findings_json.exists(), (
        f"{_rel(report)}: no sibling `{findings_json.name}` — a ci-secure "
        "example ships the raw findings beside the rendered report so the report can be "
        "cross-checked against them"
    )
    data = _load_json(findings_json)
    m = _SECURE_SCANNER_RE.search(report.read_text(encoding="utf-8"))
    assert m, (
        f"{_rel(report)}: no `ci-secure (skill commit \\`<sha>\\`) — N "
        "finding(s)` Scanner row — the example records neither the engine commit it was "
        "generated from nor a checkable headline count"
    )
    stamped_sha, stamped_count = m.group(1), int(m.group(2))
    actual = len(data.get("findings", []))
    assert stamped_count == actual, (
        f"{_rel(report)}: report's Scanner row claims {stamped_count} "
        f"finding(s) but {findings_json.name} carries {actual} — the rendered report and "
        "the raw findings disagree; regenerate the example rather than editing either"
    )
    assert data.get("skill_commit_sha", "").startswith(stamped_sha), (
        f"{_rel(report)}: report stamps skill commit {stamped_sha!r} but "
        f"{findings_json.name} records {data.get('skill_commit_sha')!r} — the report was "
        "not rendered from the run it ships beside"
    )
    # Coverage row vs the raw coverage-gap keys. Counts and SHAs alone would let a
    # report that claims "✅ complete — every workflow file was scanned" ship beside
    # findings whose own JSON records unscanned files (greptile P1 on #60): a stale
    # report rendered before the partial-checkout fix, or a hand-restored row. The
    # predicate mirrors report.py's own: coverage is complete iff no gap channel
    # carries anything.
    text = report.read_text(encoding="utf-8")
    has_gap = any(data.get(k) for k in
                  ("scan_incomplete", "dropped_matches", "coverage_notes"))
    claims_complete = "✅ complete" in text
    claims_partial = "⚠️ **PARTIAL**" in text
    assert claims_complete != claims_partial, (
        f"{_rel(report)}: the Coverage row is neither clearly complete nor clearly "
        "PARTIAL; a reader cannot tell what the scan covered")
    assert claims_complete == (not has_gap), (
        f"{_rel(report)}: the report's Coverage row claims "
        f"{'complete' if claims_complete else 'PARTIAL'} but {findings_json.name} "
        f"records {'a coverage gap' if has_gap else 'no coverage gap'} "
        f"(scan_incomplete={len(data.get('scan_incomplete') or [])}, "
        f"dropped_matches={len(data.get('dropped_matches') or [])}, "
        f"coverage_notes={len(data.get('coverage_notes') or [])}). A report that "
        "claims complete coverage over a partial checkout is a clean bill of health "
        "the scan never established — regenerate it from the current engine."
    )
    audited = _SECURE_AUDITED_COMMIT_RE.search(report.read_text(encoding="utf-8"))
    assert audited, f"{_rel(report)}: no `Audited commit` row"
    assert data.get("commit_sha", "").startswith(audited.group(1)), (
        f"{_rel(report)}: report audits {audited.group(1)!r} but the findings "
        f"record commit {data.get('commit_sha')!r} — mismatched before/after artifacts"
    )
    # Filename tag must name the commit actually scanned: a mislabeled before/after pair
    # would invert the example's entire claim while every other leg still passed.
    assert data.get("commit_sha", "").startswith(tag), (
        f"{_rel(report)}: filename tag {tag!r} does not prefix the scanned "
        f"commit {data.get('commit_sha')!r} — the before/after pair is mislabeled"
    )
    _assert_engine_sha_is_an_ancestor(report, stamped_sha)


@pytest.mark.parametrize("report", _score_reports(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_ci_score_example_matches_its_findings_json(report: Path):
    findings_json = report.parent / "ci-score-findings.json"
    assert findings_json.exists(), (
        f"{_rel(report)}: no sibling `ci-score-findings.json`")
    data = _load_json(findings_json)
    text = report.read_text(encoding="utf-8")
    card = _SCORE_CARD_RE.search(text)
    assert card, (
        f"{_rel(report)}: no `## CI Score: **N/100** — P of A applicable "
        "checks` heading to cross-check against the raw score")
    value, passed, applicable = (int(g) for g in card.groups())
    score = data.get("ci_score", {})
    assert (value, passed, applicable) == (
        score.get("value"), score.get("checks_passed"), score.get("checks_applicable")), (
        f"{_rel(report)}: report renders {value}/100, {passed} of "
        f"{applicable} checks, but ci-score-findings.json computed "
        f"{score.get('value')}/100, {score.get('checks_passed')} of "
        f"{score.get('checks_applicable')} — the headline grade was edited away from the "
        "engine's own output"
    )
    commit = _SCORE_COMMIT_RE.search(text)
    assert commit, f"{_rel(report)}: no linked `Scored commit` row"
    assert data.get("commit_sha", "").startswith(commit.group(1)), (
        f"{_rel(report)}: report scores {commit.group(1)!r} but the findings "
        f"record commit {data.get('commit_sha')!r}"
    )


def _assert_engine_sha_is_an_ancestor(report: Path, sha: str) -> None:
    """THE ancestry contract, shared by every engine family's leg: the engine commit an
    example was generated from must be a real commit that is ALREADY reachable from the
    base branch (see ``_mainline_ref``), not merely from HEAD. An unresolvable history
    SKIPS loudly rather than false-failing."""
    ok, why = _history_available()
    if not ok:
        pytest.skip(f"cannot verify {_rel(report)} ancestry: {why}")
    kind = _run_git("cat-file", "-t", sha)
    assert kind.returncode == 0 and kind.stdout.strip() == "commit", (
        f"{_rel(report)}: stamped engine commit `{sha}` is not a resolvable "
        "commit object — a fabricated, typo'd, or squash-discarded SHA. Regenerate the "
        "example from an on-main engine commit."
    )
    base, why_no_base = _mainline_ref()
    if not base:
        # No base branch to measure against. In CI that is not a tolerable degradation —
        # this gate exists to fire in CI, and a silent skip there is how the leg lost its
        # teeth in the first place — so say it as a FAILURE. Off CI (a vendored copy, a
        # clone with no `main` and no remote) skip loudly instead of false-failing.
        message = (
            f"{_rel(report)}: cannot check the provenance of `{sha}` — {why_no_base}. "
            "The stamp must be reachable from the branch this change merges into; "
            "fetch it (`git fetch origin main`) and re-run."
        )
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(message + " Skipping this in CI would leave the gate toothless.")
        pytest.skip(message)
    anc = _run_git("merge-base", "--is-ancestor", sha, base)
    if anc.returncode >= 2:
        pytest.skip(f"{_rel(report)}: cannot resolve ancestry of `{sha}` against "
                    f"{base} (git merge-base rc={anc.returncode}: {anc.stderr.strip()!r})")
    assert anc.returncode == 0, (
        f"{_rel(report)}: stamped engine commit `{sha}` is a real commit but is NOT "
        f"reachable from {base} — it exists only on this branch (or on no branch at all). "
        "A squash merge DISCARDS branch commits, so this stamp would dangle the moment "
        "the change lands, exactly as #60's did. Land the engine change first, then "
        "regenerate the example from the resulting on-main commit in a follow-up PR. "
        "(If the commit really is on main, this checkout's base ref is stale — "
        "`git fetch origin main`.)"
    )


def _init_pr_shaped_repo(root: Path) -> tuple[str, str]:
    """A throwaway clone-shaped repo in the exact shape a pull request has:
    one commit on ``main`` (mirrored to ``refs/remotes/origin/main``, as a real
    clone would), and one further commit that exists ONLY on the feature branch,
    which is checked out. Returns ``(on_main_sha, branch_only_sha)``.

    Everything stays inside the caller's ``tmp_path``; the repo-wide conftest
    scrubs the ``GIT_*`` addressing env, so these commands cannot escape onto the
    real checkout (2026-07-30 incident).
    """
    def g(*args: str) -> str:
        r = subprocess.run(
            ["git", "-C", str(root),
             "-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture",
             "-c", "commit.gpgsign=false", *args],
            capture_output=True, text=True)
        assert r.returncode == 0, f"fixture git {args}: {r.stderr}"
        return r.stdout.strip()

    init = subprocess.run(["git", "init", "-q", str(root)], capture_output=True, text=True)
    assert init.returncode == 0, init.stderr
    g("checkout", "-q", "-b", "main")
    g("commit", "-q", "--allow-empty", "-m", "engine commit that landed on main")
    on_main = g("rev-parse", "HEAD")
    # A real clone tracks the remote branch; the guard reads that ref, not just the local one.
    g("update-ref", "refs/remotes/origin/main", on_main)
    g("checkout", "-q", "-b", "feature")
    g("commit", "-q", "--allow-empty", "-m", "engine commit that exists only on the PR branch")
    branch_only = g("rev-parse", "HEAD")
    return on_main, branch_only


def test_ancestry_leg_rejects_an_engine_commit_that_exists_only_on_the_pr_branch(
        tmp_path, monkeypatch):
    # POSITIVE CONTROL for the defect this leg was rebuilt to close (PR #60 → #64).
    #
    # #60 was squash-merged. Its examples stamped `553baad`, a commit that existed only
    # on the PR branch; the squash discarded it and `main` went red on the very next run,
    # needing #64 to repair the artifacts by hand. The guard was GREEN throughout #60,
    # because it resolved the stamp against HEAD — and on a PR branch a branch-only commit
    # is trivially an ancestor of HEAD. It could only fail AFTER the merge that broke it.
    #
    # This test builds that exact situation with a real git repo — a commit on `main`,
    # a commit only on a feature branch, the feature branch checked out — and asserts the
    # branch-only stamp is REJECTED while the on-main stamp still passes. Against the
    # pre-fix HEAD-relative guard the rejection assertion does not fire (the guard accepts
    # what it must reject), which is this change's red proof, inverted.
    import sys
    mod = sys.modules[__name__]

    repo = tmp_path / "clone"
    repo.mkdir()
    on_main, branch_only = _init_pr_shaped_repo(repo)
    monkeypatch.setattr(mod, "_REPO", repo)

    def _stamped(name: str, sha: str) -> Path:
        d = tmp_path / name
        d.mkdir()
        p = d / "ci-secure-report-abc1234.md"
        p.write_text(f"| **Scanner** | ci-secure (skill commit `{sha[:7]}`) — 0 finding(s) |\n",
                     encoding="utf-8")
        return p

    # Control: a stamp already reachable from the base branch passes. Without this the
    # test could "pass" by rejecting everything (a guard that fails on valid examples is
    # just as broken, and would be discovered only by whoever regenerates one next).
    _assert_engine_sha_is_an_ancestor(_stamped("on-main", on_main), on_main[:7])

    # Tonight's scenario: the stamp names a commit that lives only on this branch and
    # will not survive the squash. It must be rejected HERE, on the PR, before the merge.
    with pytest.raises(AssertionError, match="only on this branch|not reachable"):
        _assert_engine_sha_is_an_ancestor(
            _stamped("branch-only", branch_only), branch_only[:7])

    # A stamp that landed on main AFTER this branch was cut is legitimate — a squash
    # cannot discard it — so the reference is the base branch TIP, not the merge base.
    subprocess.run(["git", "-C", str(repo),
                    "-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture",
                    "checkout", "-q", "main"], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(repo),
                    "-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture",
                    "-c", "commit.gpgsign=false",
                    "commit", "-q", "--allow-empty", "-m", "later engine commit on main"],
                   capture_output=True, text=True, check=True)
    later = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", later],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(repo),
                    "-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture",
                    "checkout", "-q", "feature"], capture_output=True, text=True, check=True)
    _assert_engine_sha_is_an_ancestor(_stamped("later-on-main", later), later[:7])


def test_the_base_ref_resolves_in_a_github_pull_request_checkout(tmp_path, monkeypatch):
    # The shape EVERY PR run of this suite actually has, and the one no ordinary clone
    # reproduces: actions/checkout checks out `refs/pull/N/merge` and fetches only that
    # ref, so there is no `origin/main` however deep the fetch — HEAD is a detached
    # two-parent merge commit whose FIRST parent is the base branch tip. If that fallback
    # ever stops resolving, every PR either skips this gate (toothless) or fails it
    # (spurious red on a correct example); both are caught here rather than in the wild.
    import sys
    mod = sys.modules[__name__]

    repo = tmp_path / "pr-checkout"
    repo.mkdir()
    on_main, branch_only = _init_pr_shaped_repo(repo)

    def g(*args: str) -> str:
        r = subprocess.run(
            ["git", "-C", str(repo),
             "-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture",
             "-c", "commit.gpgsign=false", *args], capture_output=True, text=True)
        assert r.returncode == 0, f"fixture git {args}: {r.stderr}"
        return r.stdout.strip()

    # GitHub's generated merge commit: base tip first, PR head second. Then drop every
    # trace of the base branch's own refs, exactly as a PR job's checkout has none.
    merge = g("commit-tree", "-p", on_main, "-p", branch_only,
              "-m", "Merge pull request", f"{on_main}^{{tree}}")
    g("checkout", "-q", "--detach", merge)
    g("branch", "-q", "-D", "main")
    g("branch", "-q", "-D", "feature")
    g("update-ref", "-d", "refs/remotes/origin/main")

    monkeypatch.setattr(mod, "_REPO", repo)
    for ref in ("refs/heads/main", "refs/remotes/origin/main"):
        assert not _resolves(ref), f"{ref} must not exist in a PR-shaped checkout"
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    base, why = _mainline_ref()
    assert base == "HEAD^1", f"PR-shaped checkout resolved no base ref: {why}"

    d = tmp_path / "pr-example"
    d.mkdir()
    report = d / "ci-secure-report-abc1234.md"
    report.write_text("stub\n", encoding="utf-8")
    # On-main stamp passes; the branch-only stamp — reachable from HEAD, since HEAD is the
    # merge — is still rejected. THAT is the whole point: pre-merge, on the PR.
    _assert_engine_sha_is_an_ancestor(report, on_main[:7])
    with pytest.raises(AssertionError, match="only on this branch|not reachable"):
        _assert_engine_sha_is_an_ancestor(report, branch_only[:7])


def test_an_unresolvable_base_branch_fails_in_ci_and_skips_only_off_it(tmp_path, monkeypatch):
    # The one way this leg can go quiet: no base-branch ref to measure against. Skipping
    # is right for a vendored copy or a clone with no `main` and no remote — and WRONG in
    # CI, where a silent skip is precisely how a gate rots into a no-op (the failure mode
    # this whole PR exists to remove). Pin both halves so a later refactor cannot turn the
    # CI branch back into a skip without going red here.
    import sys
    mod = sys.modules[__name__]

    orphan = tmp_path / "no-mainline"
    orphan.mkdir()
    subprocess.run(["git", "init", "-q", str(orphan)], capture_output=True, check=True)
    r = subprocess.run(["git", "-C", str(orphan),
                        "-c", "user.email=f@f.invalid", "-c", "user.name=f",
                        "-c", "commit.gpgsign=false",
                        "checkout", "-q", "-b", "detached-work"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    subprocess.run(["git", "-C", str(orphan),
                    "-c", "user.email=f@f.invalid", "-c", "user.name=f",
                    "-c", "commit.gpgsign=false",
                    "commit", "-q", "--allow-empty", "-m", "lone commit, no main, no remote"],
                   capture_output=True, text=True, check=True)
    sha = subprocess.run(["git", "-C", str(orphan), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    monkeypatch.setattr(mod, "_REPO", orphan)
    for var in ("GITHUB_BASE_REF", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(var, raising=False)

    d = tmp_path / "somewhere"
    d.mkdir()
    report = d / "ci-secure-report-abc1234.md"
    report.write_text("stub\n", encoding="utf-8")

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="no base-branch ref"):
        _assert_engine_sha_is_an_ancestor(report, sha[:7])

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(pytest.fail.Exception, match="toothless"):
        _assert_engine_sha_is_an_ancestor(report, sha[:7])


def test_the_other_engines_legs_actually_reject_tampering(tmp_path, monkeypatch):
    # RED-PROOF for every leg added above (this file's standing convention: a guard that
    # cannot be shown to fail is vacuous). Each tamper below is the exact edit the leg
    # exists to catch, applied to a hermetic copy — the real examples are never touched.
    import json
    import sys
    mod = sys.modules[__name__]
    # Stub the ancestry leg to a NO-OP, not to a skip: a skip would abort this whole
    # red-proof at its first call and green it vacuously (caught while writing it).
    # Ancestry has its own red-proof above; what is under test here is everything else.
    monkeypatch.setattr(mod, "_assert_engine_sha_is_an_ancestor", lambda report, sha: None)

    d = tmp_path / "vendor-repo"
    d.mkdir()

    def _write_secure(count: int, findings: int, stamp: str = "a43d237",
                      skill_sha: str = "a43d237d35910b0c7c6c87f287db7a6c6098b729",
                      audited: str = "4a1b8ce", commit: str = "4a1b8cecd65b899540e4324715557d6b080ddeb5",
                      tag: str = "4a1b8ce", body_extra: str = "",
                      coverage: str = "✅ complete", gaps: int = 0) -> Path:
        rep = d / f"ci-secure-report-{tag}.md"
        rep.write_text(
            f"| **Audited commit** | `{audited}` — anchored to this tree |\n"
            f"| **Coverage** | {coverage} |\n"
            f"| **Scanner** | ci-secure (skill commit `{stamp}`) — {count} finding(s) |\n"
            f"{body_extra}", encoding="utf-8")
        (d / f"ci-secure-findings-{tag}.json").write_text(json.dumps({
            "commit_sha": commit, "skill_commit_sha": skill_sha,
            "findings": [{"id": f"f{i}"} for i in range(findings)],
            "scan_incomplete": [{"workflow_file": f"w{i}.yml", "reason": "absent"}
                                for i in range(gaps)],
        }), encoding="utf-8")
        return rep

    # Truthful pair passes.
    test_ci_secure_example_matches_its_findings_json(_write_secure(3, 3))
    # A tidied-down headline count (the "3 findings became 2" hand-edit) must go red.
    with pytest.raises(AssertionError, match="claims 2 finding"):
        test_ci_secure_example_matches_its_findings_json(_write_secure(2, 3))
    # A report rendered from a different engine commit than the run it ships beside.
    with pytest.raises(AssertionError, match="not rendered from the run"):
        test_ci_secure_example_matches_its_findings_json(_write_secure(3, 3, stamp="beefbee"))
    # A mislabeled before/after pair: the "after" file actually holds the "before" scan.
    with pytest.raises(AssertionError, match="mislabeled"):
        test_ci_secure_example_matches_its_findings_json(
            _write_secure(0, 0, audited="1dc7766", commit="1dc7766c5aa4b07da3cf3416e501364d3bc827a0",
                          tag="dead123"))
    # A truthful PARTIAL pair also passes (the shape the sparse examples ship in).
    test_ci_secure_example_matches_its_findings_json(
        _write_secure(3, 3, coverage="⚠️ **PARTIAL** — see the warning below", gaps=7))
    # THE DRIFT greptile P1 names: a report claiming complete coverage beside findings
    # whose own JSON records unscanned files. A stale pre-fix report looks exactly
    # like this, and the count/SHA legs alone wave it through.
    with pytest.raises(AssertionError, match="claims complete"):
        test_ci_secure_example_matches_its_findings_json(_write_secure(3, 3, gaps=7))
    # A Coverage row that states neither verdict (a renamed marker, a reworded row)
    # must fail rather than silently disabling the comparison above.
    with pytest.raises(AssertionError, match="neither clearly complete"):
        test_ci_secure_example_matches_its_findings_json(
            _write_secure(3, 3, coverage="scanned the tree", gaps=0))
    # And the inverse: a PARTIAL row over a JSON with no gap is equally incoherent.
    with pytest.raises(AssertionError, match="claims PARTIAL"):
        test_ci_secure_example_matches_its_findings_json(
            _write_secure(3, 3, coverage="⚠️ **PARTIAL** — see the warning below", gaps=0))
    # An un-sanitized machine path anywhere in the artifact pair.
    leaky = _write_secure(3, 3, body_extra="scanned /Users/someone/checkouts/vendor-repo\n")
    with pytest.raises(AssertionError, match="un-sanitized local filesystem path"):
        test_example_artifacts_carry_no_local_filesystem_paths(leaky)

    s = tmp_path / "scored-repo"
    s.mkdir()

    def _write_score(rendered: int, computed: int) -> Path:
        rep = s / "ci-score-report.md"
        rep.write_text(
            "| **Scored commit** | [`d318b68`](https://example.invalid/c) |\n"
            f"## CI Score: **{rendered}/100** — 8 of 9 applicable checks\n", encoding="utf-8")
        (s / "ci-score-findings.json").write_text(json.dumps({
            "commit_sha": "d318b683471101618febed18996405ad26462110",
            "ci_score": {"value": computed, "checks_passed": 8, "checks_applicable": 9},
        }), encoding="utf-8")
        return rep

    test_ci_score_example_matches_its_findings_json(_write_score(89, 89))
    # A flattered grade: the card says 95 while the engine computed 89.
    with pytest.raises(AssertionError, match="headline grade was edited"):
        test_ci_score_example_matches_its_findings_json(_write_score(95, 89))

    # And the family registry: an example report matching no guarded family is caught.
    monkeypatch.setattr(mod, "_EXAMPLES", tmp_path)
    (tmp_path / "vendor-repo" / "ci-newengine-report.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="match no guarded family"):
        test_every_example_report_belongs_to_a_guarded_family()


def test_ci_workflows_check_out_full_history():
    # The ancestry leg above only has teeth when CI fetches full history: on a shallow
    # (fetch-depth: 1) checkout the historical skill commits aren't present, so `_history_available`
    # returns False and the leg SKIPS — silently degrading the gate to the well-formedness /
    # consistency checks, which a fabricated-but-consistent SHA passes. Pin `fetch-depth: 0` in
    # BOTH CI workflows so that coupling regressing goes red HERE, not silently defanged.
    # Both suite jobs now live in ci.yml (the fork twin moved in so the always-run
    # verdict job can `needs:` it). Derive the pin from every checkout in the file rather
    # than enumerating job ids: a THIRD suite job added later with fetch-depth: 1 would
    # defang the gate while an enumerated check kept passing.
    wf = _REPO / ".github" / "workflows" / "ci.yml"
    assert wf.exists(), (
        "ci.yml is missing — the example-provenance gate's full-history CI coupling can no "
        "longer be verified")
    jobs = yaml.safe_load(wf.read_text(encoding="utf-8"))["jobs"]
    checked = []
    for job_id, job in jobs.items():
        for step in job.get("steps") or []:
            if "checkout" not in str(step.get("uses", "")):
                continue
            checked.append(job_id)
            assert (step.get("with") or {}).get("fetch-depth") == 0, (
                f"ci.yml job {job_id} checks out without `fetch-depth: 0` — the "
                "example-provenance ancestry leg will silently skip in CI (shallow clone), "
                "defanging this gate")
    # The verdict job deliberately checks out nothing, so assert on the suites by name:
    # if they stop checking out code at all, the loop above would pass vacuously.
    assert set(checked) >= {"test-self", "test-fork"}, (
        f"both suite jobs must check out code in ci.yml; found checkouts in {checked}")

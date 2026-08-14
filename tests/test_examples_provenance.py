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
a real commit that is an ancestor of ``HEAD`` (the mainline history), and checks the SHA
is internally consistent across the report. CI checks out ``fetch-depth: 0`` precisely so
this ancestry check can resolve historical skill commits; on a shallow clone (where the
history isn't present) the ancestry leg SKIPS LOUDLY rather than false-failing — the
well-formedness / consistency legs still run.
"""
from __future__ import annotations

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
    ok, why = _history_available()
    if not ok:
        pytest.skip(f"cannot verify example provenance ancestry: {why}")
    m = _AUDIT_SHA_RE.search(text)
    assert m, f"{report.parent.name}: no provenance stamp (see the well-formedness test)"
    sha = m.group(1)
    kind = _run_git("cat-file", "-t", sha)
    assert kind.returncode == 0 and kind.stdout.strip() == "commit", (
        f"{report.parent.name}: stamped skill commit `{sha}` is not a resolvable commit "
        "object in this repo — a fabricated, typo'd, or discarded (squash-merged) SHA. "
        "Regenerate the example from an on-main engine commit and stamp that SHA."
    )
    anc = _run_git("merge-base", "--is-ancestor", sha, "HEAD")
    # git distinguishes its two outcomes by exit code: rc 0 = ancestor, rc 1 = genuinely NOT an
    # ancestor (the provenance violation this leg exists to catch), rc >= 2 (typically 128) = git
    # couldn't answer at all (bad revision, unborn/detached HEAD, object pruned mid-run). Only rc
    # 1 is a real verdict; a rc-128 environment error must NOT be mislabeled "not an ancestor —
    # regenerate", which would send a maintainer down the wrong path. Skip loudly on rc >= 2.
    if anc.returncode >= 2:
        pytest.skip(f"{report.parent.name}: cannot resolve ancestry of `{sha}` "
                    f"(git merge-base rc={anc.returncode}: {anc.stderr.strip()!r})")
    assert anc.returncode == 0, (
        f"{report.parent.name}: stamped skill commit `{sha}` is a real commit but NOT an "
        "ancestor of HEAD — the example was generated from an engine that never landed on "
        "the mainline (a discarded branch/squash commit). Regenerate from an on-main commit."
    )


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

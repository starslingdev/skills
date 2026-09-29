"""Tests for decision_ledger.py — the transcript-loop decision ledger (PR-C, spec §2 Item 3).

Fixture-replay per spec §3 item #3: a fixture `decisions.jsonl` + a pending/promoted set ->
a REJECTED signature is skipped, an ancestry-stale one re-surfaces, a TTL-expired one re-surfaces,
and the two git-ignore DoD oracles hold. No git checkout / wall clock needed — `is_suppressed` and
`apply_decision_ledger` both take injected `now` / `current_skill_sha` / `is_ancestor`, mirroring
`aggregate_lessons.aggregate`'s own discipline.

Run from the repo root:

    pytest -v maintainers/ci-speedup/tests/test_decision_ledger.py
"""
from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path

import aggregate_lessons as al
import decision_ledger as dl

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NOW = _dt.datetime(2026, 7, 1, tzinfo=_dt.timezone.utc)


def _always_ancestor(_anc, _desc):
    return True


def _never_ancestor(_anc, _desc):
    return False


def _decision(sig, disposition, *, reason="", sha="cur", decided_at=None):
    return dl.Decision(
        signature=sig, disposition=disposition, reason=reason,
        skill_commit_sha=sha, decided_at=decided_at or _NOW.isoformat(),
    )


def _cluster(sig, sessions):
    records = [
        al.LessonRecord(
            signature=al.normalize_signature(sig), raw_signature=sig,
            session_id=s, skill_commit_sha="cur", recorded_at=_NOW.isoformat(),
            lesson={"signature": sig},
        )
        for s in sessions
    ]
    return al.Cluster(al.normalize_signature(sig), records)


# --------------------------------------------------------------------------- #
# is_suppressed — the pure core
# --------------------------------------------------------------------------- #
def test_no_decision_is_never_suppressed():
    res = dl.is_suppressed(
        al.normalize_signature("gap-fill@SKILL.md:fill-coverage-gap"), [],
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is False
    assert res.decision is None
    assert res.resurfaced_reason is None


def test_rejected_signature_is_suppressed():
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    decisions = [_decision(sig, "rejected", reason="too narrow, misreads the fixture")]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is True
    assert res.decision.reason == "too narrow, misreads the fixture"
    assert res.resurfaced_reason is None


def test_approved_never_suppresses():
    sig = "sizing@SKILL.md:floor-cap"
    decisions = [_decision(sig, "approved", reason="landed verbatim")]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is False
    assert res.decision.disposition == "approved"   # audit record, surfaced, not a gate


def test_superseded_never_suppresses():
    sig = "render@SKILL.md:second-pole"
    decisions = [_decision(sig, "superseded", reason="landed in edited form")]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is False
    assert res.decision.disposition == "superseded"


def test_rejected_signature_resurfaces_on_ancestry_staleness():
    sig = "spine@SKILL.md:required-scope"
    decisions = [_decision(sig, "rejected", reason="not reproducible", sha="deadbeef")]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_never_ancestor)
    assert res.suppressed is False
    assert res.resurfaced_reason is not None
    assert "ancestry-stale" in res.resurfaced_reason
    assert res.decision.reason == "not reproducible"   # prior reason carried for the loud note


def test_rejected_signature_stays_suppressed_when_still_ancestor():
    sig = "spine@SKILL.md:required-scope"
    decisions = [_decision(sig, "rejected", reason="not reproducible", sha="cur")]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is True


def test_rejected_signature_resurfaces_on_ttl_expiry():
    sig = "present@SKILL.md:measure-not-estimate"
    old = (_NOW - _dt.timedelta(days=120)).isoformat()   # > 90d default TTL
    decisions = [_decision(sig, "rejected", reason="one-off, not a real pattern",
                            sha="cur", decided_at=old)]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is False
    assert res.resurfaced_reason is not None
    assert "TTL-expired" in res.resurfaced_reason


def test_rejected_signature_within_ttl_stays_suppressed():
    sig = "present@SKILL.md:measure-not-estimate"
    recent = (_NOW - _dt.timedelta(days=10)).isoformat()
    decisions = [_decision(sig, "rejected", reason="one-off", sha="cur", decided_at=recent)]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is True


def test_rejected_row_with_materially_future_decided_at_resurfaces():
    # A garbage FUTURE timestamp (the CLI never emits one) makes `now - decided` negative, so the
    # TTL check `age > ttl_days` is forever false — the row would suppress until that date, the same
    # suppress-FOREVER failure the missing-timestamp case guards. Must fail OPEN instead.
    sig = "present@SKILL.md:measure-not-estimate"
    future = (_NOW + _dt.timedelta(days=365 * 900)).isoformat()  # ~year 2926
    decisions = [_decision(sig, "rejected", reason="typo'd the year", sha="cur", decided_at=future)]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is False
    assert res.resurfaced_reason is not None
    assert "future" in res.resurfaced_reason


def test_rejection_with_minor_clock_skew_still_suppresses():
    # A few-seconds/minutes-future stamp (ordinary clock skew) is a fresh, legitimate rejection —
    # the ~1-day grace must NOT resurface it.
    sig = "present@SKILL.md:measure-not-estimate"
    skewed = (_NOW + _dt.timedelta(minutes=5)).isoformat()
    decisions = [_decision(sig, "rejected", reason="one-off", sha="cur", decided_at=skewed)]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is True


def test_a_fresh_rejection_resets_the_clock():
    # An OLD rejection (past TTL) followed by a FRESH rejection for the same signature: the fresh
    # row governs (the newest decision always wins) — so it stays suppressed, not re-surfaced.
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    stale = (_NOW - _dt.timedelta(days=120)).isoformat()
    fresh = (_NOW - _dt.timedelta(days=1)).isoformat()
    decisions = [
        _decision(sig, "rejected", reason="first pass", sha="cur", decided_at=stale),
        _decision(sig, "rejected", reason="re-reviewed, still no", sha="cur", decided_at=fresh),
    ]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is True
    assert res.decision.reason == "re-reviewed, still no"   # the FRESH row, not the stale one


def test_custom_ttl_days_is_honored():
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    old = (_NOW - _dt.timedelta(days=45)).isoformat()   # within default 90d, past a custom 30d
    decisions = [_decision(sig, "rejected", reason="x", sha="cur", decided_at=old)]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor, ttl_days=30)
    assert res.suppressed is False
    assert "TTL-expired" in res.resurfaced_reason


def test_transient_ancestry_failure_keeps_the_suppression():
    # Mirrors aggregate_lessons' identical stance: a transient (non-verdict) ancestry-check
    # failure must not masquerade as "not an ancestor" and silently re-surface a declined lesson.
    sig = "spine@SKILL.md:required-scope"
    decisions = [_decision(sig, "rejected", reason="x", sha="cur")]

    def boom(_anc, _desc):
        raise RuntimeError("git timeout")

    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=boom)
    assert res.suppressed is True


# --------------------------------------------------------------------------- #
# FAIL OPEN, LOUD — a rejected row must never suppress forever (adversarial-review bug)
# --------------------------------------------------------------------------- #
def test_rejected_row_with_missing_decided_at_resurfaces_not_suppresses():
    # THE BUG: a rejected row with no parseable decided_at skipped the TTL block entirely; with no
    # sha it also skipped the ancestry block → permanent, silent suppression. The fix fails OPEN:
    # a governing rejected row we can't age-bound RESURFACES for re-review, loudly.
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    # No decided_at AND no sha — the worst case (both escape hatches disabled).
    decisions = [dl.Decision(signature=sig, disposition="rejected", reason="hand-appended row",
                             skill_commit_sha=None, decided_at="")]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is False
    assert res.resurfaced_reason is not None
    assert "no valid decided_at" in res.resurfaced_reason
    assert res.decision.reason == "hand-appended row"   # prior reason still carried


def test_rejected_row_with_unparseable_decided_at_resurfaces():
    # Same fail-open path via a non-ISO garbage timestamp (distinct from an EMPTY one).
    sig = "spine@SKILL.md:required-scope"
    decisions = [_decision(sig, "rejected", reason="garbage ts", sha="cur", decided_at="not-a-date")]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is False
    assert "no valid decided_at" in res.resurfaced_reason


def test_load_decisions_flags_a_provenanceless_rejected_row(tmp_path):
    # The coverage-signal half of the fix: a well-formed JSON rejected row lacking decided_at/sha
    # must land in `problems` (previously SILENT — valid JSON, so nothing surfaced it).
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"signature": "gap-fill@SKILL.md:fill-coverage-gap", "disposition": "rejected", '
        '"reason": "hand-appended, no provenance"}\n',
        encoding="utf-8",
    )
    decisions, problems = dl.load_decisions(tmp_path)
    assert len(decisions) == 1                      # KEPT, not dropped — it's the newest verdict
    assert decisions[0].disposition == "rejected"
    assert len(problems) == 1
    assert "decided_at" in problems[0] and "skill_commit_sha" in problems[0]
    assert "RESURFACE" in problems[0]


def test_load_decisions_does_not_flag_a_well_provenanced_rejected_row(tmp_path):
    # Control: a rejected row WITH decided_at + sha is not a hazard and must not be flagged.
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"signature": "s@SKILL.md:r", "disposition": "rejected", "reason": "no", '
        '"skill_commit_sha": "cur", "decided_at": "2026-06-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    decisions, problems = dl.load_decisions(tmp_path)
    assert len(decisions) == 1
    assert problems == []


def test_load_decisions_does_not_flag_an_approved_row_missing_provenance(tmp_path):
    # Only SUPPRESSING dispositions are a fail-closed hazard — an approved/superseded row never
    # suppresses, so a missing decided_at/sha on it is not a coverage problem.
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"signature": "s@SKILL.md:r", "disposition": "approved", "reason": "landed"}\n',
        encoding="utf-8",
    )
    decisions, problems = dl.load_decisions(tmp_path)
    assert len(decisions) == 1
    assert problems == []


def test_newest_bad_ts_verdict_governs_over_older_valid_ts(tmp_path):
    # THE SECOND (MEDIUM) BUG: a `decided_at`-sort let a newest row with a garbage/empty ts (sorting
    # to datetime.min) be OUTVOTED by an older valid-ts row. Here the maintainer's NEWEST verdict is
    # `approved` with a bad ts, appended AFTER an older within-TTL `rejected`. "Newest governs" ⇒ the
    # signature must NOT be suppressed (the approval wins), regardless of the unparseable timestamp.
    sig = "render@SKILL.md:second-pole"
    older_rejected = (_NOW - _dt.timedelta(days=10)).isoformat()   # valid ts, within TTL
    decisions = [
        _decision(sig, "rejected", reason="old call", sha="cur", decided_at=older_rejected),
        # newest-APPENDED, but an unparseable ts (which the old `decided_at`-sort put at datetime.min,
        # letting the older rejected outvote it). "not-a-date" is truthy so the _decision helper's
        # `decided_at or _NOW` coercion doesn't silently replace it with a valid now-stamp.
        _decision(sig, "approved", reason="re-reviewed and landed", sha="cur", decided_at="not-a-date"),
    ]
    res = dl.is_suppressed(
        al.normalize_signature(sig), decisions,
        now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert res.suppressed is False
    assert res.decision.disposition == "approved"   # the NEWEST-appended row governs, not max-ts


# --------------------------------------------------------------------------- #
# apply_decision_ledger — the aggregate_lessons wiring
# --------------------------------------------------------------------------- #
def test_apply_ledger_excludes_a_rejected_promoted_cluster():
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    promoted = [_cluster(sig, ["sess-a", "sess-b"])]
    decisions = [_decision(sig, "rejected", reason="misdiagnosed")]
    result = al.apply_decision_ledger(
        promoted, decisions, now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert result.promoted == []
    assert [s["signature"] for s in result.suppressed] == [sig]
    assert result.suppressed[0]["reason"] == "misdiagnosed"
    assert result.resurfaced == []


def test_apply_ledger_keeps_a_clean_promoted_cluster():
    sig = "render@SKILL.md:second-pole"
    promoted = [_cluster(sig, ["sess-a", "sess-b"])]
    result = al.apply_decision_ledger(
        promoted, [], now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert [c.raw_signature for c in result.promoted] == [sig]
    assert result.suppressed == []
    assert result.resurfaced == []


def test_apply_ledger_flags_a_stale_rejection_as_resurfaced_but_keeps_it_promoted():
    sig = "spine@SKILL.md:required-scope"
    promoted = [_cluster(sig, ["sess-a", "sess-b"])]
    decisions = [_decision(sig, "rejected", reason="stale reason", sha="deadbeef")]
    result = al.apply_decision_ledger(
        promoted, decisions, now=_NOW, current_skill_sha="cur", is_ancestor=_never_ancestor)
    assert [c.raw_signature for c in result.promoted] == [sig]   # still promoted
    assert result.suppressed == []
    assert [r["signature"] for r in result.resurfaced] == [sig]
    assert result.resurfaced[0]["prior_reason"] == "stale reason"
    assert "ancestry-stale" in result.resurfaced[0]["note"]


def test_apply_ledger_mixed_promoted_set():
    kept_sig = "render@SKILL.md:second-pole"
    rejected_sig = "gap-fill@SKILL.md:fill-coverage-gap"
    promoted = [_cluster(kept_sig, ["a", "b"]), _cluster(rejected_sig, ["c", "d"])]
    decisions = [_decision(rejected_sig, "rejected", reason="no")]
    result = al.apply_decision_ledger(
        promoted, decisions, now=_NOW, current_skill_sha="cur", is_ancestor=_always_ancestor)
    assert [c.raw_signature for c in result.promoted] == [kept_sig]
    assert [s["signature"] for s in result.suppressed] == [rejected_sig]


# --------------------------------------------------------------------------- #
# main() end-to-end report — the ledger shapes what PROMOTED shows
# --------------------------------------------------------------------------- #
def _summary(loop_dir, run_id, sig):
    d = loop_dir / "transcripts" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "transcript_id": run_id,
        "steering_events": [{"id": "s1", "signature": sig}],
    }), encoding="utf-8")


def test_main_report_excludes_a_rejected_signature(tmp_path, capsys):
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    _summary(tmp_path, "run-1", sig)
    _summary(tmp_path, "run-2", sig)   # 2 sessions -> would promote
    # sha=None: main() ancestry-checks against the REAL repo HEAD, which this synthetic "cur" is
    # not — omitting it isolates the assertion to the rejection itself (TTL not yet elapsed).
    # decided_at is relative to the real wall clock (main() uses datetime.now()), not the fixed
    # _NOW fixture, so this fixture cannot drift past the 90-day TTL as real time passes.
    recent = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=10)).isoformat()
    dl.append_decision(
        tmp_path,
        _decision(sig, "rejected", reason="declined by maintainer", sha=None, decided_at=recent),
    )
    rc = al.main(["--loop-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PROMOTED — recurred across >= 2 distinct sessions (0)" in out
    assert "SUPPRESSED" in out
    assert sig in out
    assert "declined by maintainer" in out


def test_main_report_shows_resurfaced_when_rejection_is_ttl_expired(tmp_path, capsys):
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    _summary(tmp_path, "run-1", sig)
    _summary(tmp_path, "run-2", sig)
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=200)).isoformat()
    dl.append_decision(
        tmp_path, dl.Decision(signature=sig, disposition="rejected", reason="old call",
                               skill_commit_sha="cur", decided_at=old))
    rc = al.main(["--loop-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PROMOTED — recurred across >= 2 distinct sessions (1)" in out   # re-surfaced, so promoted again
    assert "RE-SURFACED" in out
    assert "TTL-expired" in out


# --------------------------------------------------------------------------- #
# I/O round-trip
# --------------------------------------------------------------------------- #
def test_append_and_load_decisions_round_trip(tmp_path):
    d = _decision("sizing@SKILL.md:floor-cap", "approved", reason="landed")
    dl.append_decision(tmp_path, d)
    dl.append_decision(tmp_path, _decision("render@SKILL.md:second-pole", "rejected", reason="no"))
    decisions, problems = dl.load_decisions(tmp_path)
    assert problems == []
    assert [x.signature for x in decisions] == ["sizing@SKILL.md:floor-cap", "render@SKILL.md:second-pole"]
    assert [x.disposition for x in decisions] == ["approved", "rejected"]


def test_load_decisions_preserves_good_rows_and_surfaces_malformed(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"signature": "sizing@SKILL.md:floor-cap", "disposition": "approved", "reason": "x", '
        '"skill_commit_sha": "cur", "decided_at": "2026-01-01T00:00:00+00:00"}\n'
        'not json\n'
        '42\n',
        encoding="utf-8",
    )
    decisions, problems = dl.load_decisions(tmp_path)
    assert len(decisions) == 1
    assert decisions[0].signature == "sizing@SKILL.md:floor-cap"
    assert len(problems) == 2   # the bad-json line + the non-object line, both surfaced loudly


def test_missing_decisions_file_returns_empty():
    decisions, problems = dl.load_decisions(Path("/nonexistent/.ci-speedup-loop"))
    assert decisions == []
    assert problems == []


# --------------------------------------------------------------------------- #
# CLI: --decide
# --------------------------------------------------------------------------- #
def test_cli_decide_appends_a_row(tmp_path):
    rc = dl.main([
        "--loop-dir", str(tmp_path), "--decide",
        "gap-fill@SKILL.md:fill-coverage-gap", "rejected",
        "--reason", "too narrow", "--skill-commit-sha", "deadbeef",
    ])
    assert rc == 0
    decisions, problems = dl.load_decisions(tmp_path)
    assert problems == []
    assert len(decisions) == 1
    assert decisions[0].disposition == "rejected"
    assert decisions[0].reason == "too narrow"
    assert decisions[0].skill_commit_sha == "deadbeef"


def test_cli_decide_rejected_requires_a_reason(tmp_path):
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        dl.main(["--loop-dir", str(tmp_path), "--decide",
                 "gap-fill@SKILL.md:fill-coverage-gap", "rejected"])
    decisions, _ = dl.load_decisions(tmp_path)
    assert decisions == []   # nothing was appended — the bad invocation must not write


def test_cli_decide_rejects_an_unknown_disposition(tmp_path):
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        dl.main(["--loop-dir", str(tmp_path), "--decide",
                 "gap-fill@SKILL.md:fill-coverage-gap", "maybe", "--reason", "x"])


def test_cli_check_reports_suppressed(tmp_path, capsys):
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    # decided_at is relative to the real wall clock (dl.main() uses datetime.now()), not the fixed
    # _NOW fixture, so this fixture cannot drift past the 90-day TTL as real time passes.
    recent = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=10)).isoformat()
    dl.append_decision(
        tmp_path, _decision(sig, "rejected", reason="too narrow", sha=None, decided_at=recent)
    )
    rc = dl.main(["--loop-dir", str(tmp_path), "--check", sig])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SUPPRESSED" in out
    assert "too narrow" in out


# --------------------------------------------------------------------------- #
# DoD oracle #1/#2 — the ledger path is gitignored and untracked (real repo, real git)
# --------------------------------------------------------------------------- #
def test_decisions_jsonl_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", ".ci-speedup-loop/decisions.jsonl"],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f".ci-speedup-loop/decisions.jsonl must be git-ignored; check-ignore exited "
        f"{result.returncode}: {result.stdout!r} {result.stderr!r}"
    )


def test_decisions_jsonl_is_absent_from_git_ls_files():
    result = subprocess.run(
        ["git", "ls-files", "--", ".ci-speedup-loop/"],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    assert "decisions.jsonl" not in "\n".join(tracked)
    assert not any(p.endswith("decisions.jsonl") for p in tracked), tracked

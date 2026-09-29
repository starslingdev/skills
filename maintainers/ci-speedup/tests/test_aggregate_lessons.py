"""The cross-session recurrence gate (scripts/aggregate_lessons.py).

These pin the promotion contract on synthetic summaries — no git, no wall clock
(both injected):

  - two sessions sharing a signature PROMOTE;
  - a lone single-session lesson is HELD (recorded as pending), never promoted;
  - a pending entry past PENDING_TTL_DAYS — or carrying a non-ancestor
    skill_commit_sha — is DISCARDED and does NOT pair with a fresh lesson to cross
    the floor (a fossil can't confirm an evolved contract);
  - below-floor lessons are recorded + surfaced, never silently dropped;
  - phrasing drift (case/whitespace) still clusters.

Run from the repo root:

    pytest -v maintainers/ci-speedup/tests/test_aggregate_lessons.py
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import aggregate_lessons as al  # noqa: E402

_NOW = _dt.datetime(2026, 6, 16, tzinfo=_dt.timezone.utc)


def _lesson(sig: str, sid: str, *, sha: str = "cur", recorded_at: str | None = None):
    return al.LessonRecord(
        signature=al.normalize_signature(sig),
        raw_signature=sig,
        session_id=sid,
        skill_commit_sha=sha,
        recorded_at=recorded_at or _NOW.isoformat(),
        lesson={"id": "s1", "signature": sig},
    )


def _always_ancestor(_anc, _desc):
    return True


def _never_ancestor(_anc, _desc):
    return False


def _agg(fresh, pending, *, is_ancestor=_always_ancestor, **kw):
    return al.aggregate(
        fresh, pending, now=_NOW, current_skill_sha="cur",
        is_ancestor=is_ancestor, **kw,
    )


# --------------------------------------------------------------------------- #
# The promotion gate
# --------------------------------------------------------------------------- #
def test_two_sessions_sharing_a_signature_promote():
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    res = _agg([_lesson(sig, "sess-a"), _lesson(sig, "sess-b")], [])
    assert [c.raw_signature for c in res.promoted] == [sig]
    assert res.promoted[0].distinct_sessions == {"sess-a", "sess-b"}
    assert res.held == []
    # A promoted cluster is NOT re-stashed as pending feedstock.
    assert res.new_pending == []


def test_a_lone_single_session_lesson_is_held_not_promoted():
    sig = "render@SKILL.md:second-pole"
    res = _agg([_lesson(sig, "sess-a")], [])
    assert res.promoted == []
    assert [c.raw_signature for c in res.held] == [sig]
    # Recorded as pending feedstock so it can cross the floor later (no silent drop).
    assert [r["signature"] for r in res.new_pending] == [sig]
    assert res.new_pending[0]["session_id"] == "sess-a"


def test_two_lessons_same_signature_same_session_count_once():
    # A lesson repeated WITHIN one session is one distinct session, not two — it must
    # not self-promote.
    sig = "sizing@SKILL.md:floor-cap"
    res = _agg([_lesson(sig, "sess-a"), _lesson(sig, "sess-a")], [])
    assert res.promoted == []
    assert res.held and res.held[0].distinct_sessions == {"sess-a"}


def test_a_fresh_lesson_pairs_with_a_valid_pending_to_promote():
    # The whole point of pending feedstock: a prior single-session lesson crosses the
    # floor when a SECOND distinct session recurs it.
    sig = "present@SKILL.md:measure-not-estimate"
    pending = [al._record_to_pending(_lesson(sig, "old-sess", recorded_at=_NOW.isoformat()))]
    res = _agg([_lesson(sig, "new-sess")], pending)
    assert [c.raw_signature for c in res.promoted] == [sig]
    assert res.promoted[0].distinct_sessions == {"old-sess", "new-sess"}


# --------------------------------------------------------------------------- #
# Staleness bound — a fossil can't confirm an evolved contract
# --------------------------------------------------------------------------- #
def test_pending_past_ttl_is_discarded_and_does_not_cross_the_floor():
    sig = "gap-fill@SKILL.md:fill-coverage-gap"
    old = (_NOW - _dt.timedelta(days=120)).isoformat()  # > 90d TTL
    pending = [al._record_to_pending(_lesson(sig, "old-sess", recorded_at=old))]
    res = _agg([_lesson(sig, "new-sess")], pending)
    # The fossil is expired (loud, not silent), so the fresh lesson is alone → HELD.
    assert [r.raw_signature for r in res.expired] == [sig]
    assert res.promoted == []
    assert [c.raw_signature for c in res.held] == [sig]
    assert res.held[0].distinct_sessions == {"new-sess"}


def test_pending_with_non_ancestor_sha_is_discarded_and_does_not_cross_the_floor():
    sig = "spine@SKILL.md:required-scope"
    # In-TTL, but its skill_commit_sha is no longer an ancestor of the current SKILL.md
    # (the phase/rule it named has since been rewritten) → discard.
    pending = [al._record_to_pending(_lesson(sig, "old-sess", sha="deadbeef"))]
    res = _agg([_lesson(sig, "new-sess")], pending, is_ancestor=_never_ancestor)
    assert [r.raw_signature for r in res.expired] == [sig]
    assert res.promoted == []
    assert res.held[0].distinct_sessions == {"new-sess"}


def test_in_ttl_ancestor_pending_survives_to_promote():
    # Control for the two expiry tests: a recent, still-ancestor pending entry is NOT
    # expired and DOES pair to promote.
    sig = "spine@SKILL.md:required-scope"
    recent = (_NOW - _dt.timedelta(days=10)).isoformat()
    pending = [al._record_to_pending(_lesson(sig, "old-sess", recorded_at=recent))]
    res = _agg([_lesson(sig, "new-sess")], pending, is_ancestor=_always_ancestor)
    assert res.expired == []
    assert [c.raw_signature for c in res.promoted] == [sig]


# --------------------------------------------------------------------------- #
# Clustering robustness + feedstock hygiene
# --------------------------------------------------------------------------- #
def test_case_and_whitespace_drift_still_clusters():
    res = _agg(
        [_lesson("Gap-Fill@SKILL.md:fill-coverage-gap", "sess-a"),
         _lesson("gap-fill@SKILL.md:fill-coverage-gap ", "sess-b")],
        [],
    )
    assert len(res.promoted) == 1
    assert res.promoted[0].distinct_sessions == {"sess-a", "sess-b"}


def test_held_pending_keeps_earliest_recorded_at_on_rewrite():
    # A re-run must not reset a held entry's TTL clock: when the same (session,
    # signature) shows up again, the EARLIEST recorded_at is retained.
    sig = "render@SKILL.md:second-pole"
    old = (_NOW - _dt.timedelta(days=30)).isoformat()
    pending = [al._record_to_pending(_lesson(sig, "sess-a", recorded_at=old))]
    # Same session re-emits the lesson "now"; it stays below floor and is rewritten.
    res = _agg([_lesson(sig, "sess-a", recorded_at=_NOW.isoformat())], pending)
    assert res.promoted == []
    assert len(res.new_pending) == 1
    assert res.new_pending[0]["recorded_at"] == old  # earliest wins, clock not reset


def test_distinct_lessons_are_not_merged():
    a = _lesson("gap-fill@SKILL.md:fill-coverage-gap", "sess-a")
    b = _lesson("render@SKILL.md:second-pole", "sess-a")
    res = _agg([a, b], [])
    # Two distinct signatures, each single-session → both held, neither merged.
    assert {c.raw_signature for c in res.held} == {
        "gap-fill@SKILL.md:fill-coverage-gap", "render@SKILL.md:second-pole"}
    assert res.promoted == []


# --------------------------------------------------------------------------- #
# Extraction from a summary doc
# --------------------------------------------------------------------------- #
def test_lessons_from_summary_stamps_provenance_and_skips_unsigned():
    summary = {
        "transcript_id": "2026-06-16-run",
        "steering_events": [
            {"id": "s1", "signature": "gap-fill@SKILL.md:fill-coverage-gap"},
            {"id": "s2"},  # unsigned (older summary / malformed) → skipped loudly
        ],
    }
    recs = al.lessons_from_summary(summary, recorded_at=_NOW.isoformat(), skill_commit_sha="abc123")
    assert len(recs) == 1
    assert recs[0].session_id == "2026-06-16-run"
    assert recs[0].skill_commit_sha == "abc123"
    assert recs[0].recorded_at == _NOW.isoformat()


def test_lessons_from_summary_survives_malformed_events():
    # The summary is LLM-written and unvalidated. One off-shape event must NOT raise and abort the
    # whole aggregation (which would lose every other valid session's lessons before any report
    # prints). A non-dict element and a null are skipped loudly; a numeric signature is coerced.
    summary = {
        "transcript_id": "2026-06-16-run",
        "steering_events": [
            {"id": "s1", "signature": "gap-fill@SKILL.md:fill-coverage-gap"},
            "a stray string",                # non-dict element → skipped, not crashed
            None,                            # null element → skipped
            {"id": "s3", "signature": 5},    # numeric signature → coerced to "5", not crashed
        ],
    }
    recs = al.lessons_from_summary(summary, recorded_at=_NOW.isoformat(), skill_commit_sha="abc123")
    sigs = {r.signature for r in recs}
    assert "gap-fill@skill.md:fill-coverage-gap" in sigs   # the good event still lands
    assert "5" in sigs                                     # numeric signature coerced
    assert len(recs) == 2                                  # string + None skipped, two dicts kept


# --------------------------------------------------------------------------- #
# I/O round-trip
# --------------------------------------------------------------------------- #
def test_pending_round_trip_through_disk(tmp_path):
    sig = "sizing@references/savings-methodology.md:floor-cap-structural"
    rows = [al._record_to_pending(_lesson(sig, "sess-a"))]
    al.write_pending(tmp_path, rows)
    assert (tmp_path / "pending.jsonl").is_file()
    back, malformed = al.load_pending(tmp_path)
    assert back == rows
    assert malformed == []


def test_load_summaries_reads_nested_transcript_dirs(tmp_path):
    d = tmp_path / "transcripts" / "run-1"
    d.mkdir(parents=True)
    (d / "summary.json").write_text('{"transcript_id": "run-1"}', encoding="utf-8")
    got, problems = al.load_summaries(tmp_path)
    assert [s["transcript_id"] for s in got] == ["run-1"]
    assert problems == []


def test_load_summaries_surfaces_non_dict_json_as_a_problem(tmp_path):
    # A valid-but-non-dict top-level summary.json (null / array) parses without raising but would
    # crash main()'s summary.get(...) and abort the WHOLE run, losing every other session. It must
    # be surfaced as a problem (like a decode error), and the good summaries still load.
    base = tmp_path / "transcripts"
    (base / "good").mkdir(parents=True)
    (base / "good" / "summary.json").write_text('{"transcript_id": "t1"}', encoding="utf-8")
    (base / "bad").mkdir(parents=True)
    (base / "bad" / "summary.json").write_text("null", encoding="utf-8")
    got, problems = al.load_summaries(tmp_path)
    assert [s["transcript_id"] for s in got] == ["t1"]            # the good one still loads
    assert any("non-object" in p and "bad" in p for p in problems)


def test_load_pending_preserves_non_dict_line(tmp_path):
    # A valid-but-non-dict pending.jsonl line (e.g. a hand-edit leaving `42`) parses cleanly but
    # would crash _pending_to_record's row.get(...). It must be PRESERVED verbatim (the file is
    # unrecoverable), not crash the run, and the good rows still load.
    (tmp_path / "pending.jsonl").write_text(
        '{"signature": "render@SKILL.md:second-pole", "session_id": "s1"}\n42\n', encoding="utf-8")
    rows, malformed = al.load_pending(tmp_path)
    assert [r["signature"] for r in rows] == ["render@SKILL.md:second-pole"]
    assert malformed == ["42"]   # preserved verbatim for rewrite, not dropped


# --------------------------------------------------------------------------- #
# #1 — an unattributable (empty-session) lesson can't self-promote
# --------------------------------------------------------------------------- #
def test_empty_session_id_does_not_count_toward_the_floor():
    # A summary missing transcript_id yields session_id "". It must NOT pair with a real
    # session to cross the floor — else one malformed summary self-promotes.
    sig = "render@SKILL.md:second-pole"
    pending = [al._record_to_pending(_lesson(sig, "real-sess"))]
    res = _agg([_lesson(sig, "")], pending)
    assert res.promoted == []
    assert res.held and res.held[0].distinct_sessions == {"real-sess"}


def test_two_empty_session_lessons_do_not_promote():
    sig = "render@SKILL.md:second-pole"
    res = _agg([_lesson(sig, ""), _lesson(sig, "")], [])
    assert res.promoted == []  # 0 distinct attributable sessions


# --------------------------------------------------------------------------- #
# #3 — a pending entry with no usable provenance is expired, not immortal
# --------------------------------------------------------------------------- #
def test_pending_with_no_provenance_is_expired():
    sig = "spine@SKILL.md:required-scope"
    # A hand-corrupted pending row with NEITHER recorded_at nor sha — it could otherwise
    # never age out (an immortal fossil). Build it directly (the _lesson helper would
    # default recorded_at, masking the no-provenance case).
    pending = [{
        "signature": sig, "session_id": "old-sess",
        "skill_commit_sha": None, "recorded_at": None,
        "lesson": {"id": "s1", "signature": sig},
    }]
    res = _agg([_lesson(sig, "new-sess")], pending)
    assert [r.raw_signature for r in res.expired] == [sig]
    assert res.promoted == []  # the fossil can't pair to cross the floor


# --------------------------------------------------------------------------- #
# #4 — a TRANSIENT ancestry-check failure keeps the entry (does not expire it)
# --------------------------------------------------------------------------- #
def test_transient_ancestry_failure_keeps_entry():
    def _raises(_anc, _desc):
        raise RuntimeError("git timed out")

    sig = "spine@SKILL.md:required-scope"
    recent = (_NOW - _dt.timedelta(days=5)).isoformat()
    pending = [al._record_to_pending(_lesson(sig, "old-sess", recorded_at=recent))]
    res = _agg([_lesson(sig, "new-sess")], pending, is_ancestor=_raises)
    # A git hiccup must NOT destroy valid feedstock: the entry survives and pairs to promote.
    assert res.expired == []
    assert [c.raw_signature for c in res.promoted] == [sig]


# --------------------------------------------------------------------------- #
# #2 carry-through — a malformed pending line is preserved, never destroyed
# --------------------------------------------------------------------------- #
def test_malformed_pending_line_is_preserved_not_dropped(tmp_path):
    good = al._record_to_pending(_lesson("render@SKILL.md:second-pole", "sess-a"))
    body = json.dumps(good) + "\n" + "{not valid json\n"
    (tmp_path / "pending.jsonl").write_text(body, encoding="utf-8")
    rows, malformed = al.load_pending(tmp_path)
    assert rows == [good]
    assert malformed == ["{not valid json"]
    # Rewriting must keep the malformed line verbatim (gitignored + unrecoverable).
    al.write_pending(tmp_path, rows, preserved_raw=malformed)
    rows2, malformed2 = al.load_pending(tmp_path)
    assert rows2 == [good]
    assert malformed2 == ["{not valid json"]


# --------------------------------------------------------------------------- #
# _parse_iso edges
# --------------------------------------------------------------------------- #
def test_parse_iso_edges():
    z = al._parse_iso("2026-06-16T00:00:00Z")
    plus = al._parse_iso("2026-06-16T00:00:00+00:00")
    assert z is not None and z == plus
    naive = al._parse_iso("2026-06-16T00:00:00")
    assert naive is not None and naive.tzinfo is not None  # assumed UTC, never naive
    assert al._parse_iso("garbage") is None
    assert al._parse_iso("") is None
    # A hand-edited pending.jsonl can carry a non-string recorded_at (e.g. an epoch number);
    # treat it as unparseable rather than crashing on `.replace`.
    assert al._parse_iso(1718500000) is None
    assert al._parse_iso(None) is None


# --------------------------------------------------------------------------- #
# main() end-to-end — --commit writes, default observe is non-destructive, scoped
# --------------------------------------------------------------------------- #
def _summary(tmp_path, run_id, sig):
    d = tmp_path / "transcripts" / run_id
    d.mkdir(parents=True)
    (d / "summary.json").write_text(
        json.dumps({"transcript_id": run_id, "steering_events": [{"id": "s1", "signature": sig}]}),
        encoding="utf-8",
    )


def test_main_commit_writes_only_held_to_pending(tmp_path):
    sig_promote = "gap-fill@SKILL.md:fill-coverage-gap"
    sig_held = "render@SKILL.md:second-pole"
    _summary(tmp_path, "run-1", sig_promote)
    _summary(tmp_path, "run-2", sig_promote)  # 2 sessions → promote
    _summary(tmp_path, "run-3", sig_held)     # 1 session → held
    rc = al.main(["--loop-dir", str(tmp_path), "--commit"])
    assert rc == 0
    rows, _ = al.load_pending(tmp_path)
    sigs = [r["signature"] for r in rows]
    assert sig_held in sigs and sig_promote not in sigs  # promoted cleared, held kept


def test_main_without_commit_does_not_mutate_pending(tmp_path, capsys):
    _summary(tmp_path, "run-1", "render@SKILL.md:second-pole")
    # main() judges the seed against the REAL clock (90-day pending TTL) and the REAL repo HEAD
    # (sha ancestry), so a fixed _NOW stamp ages into EXPIRED and a synthetic sha is never an
    # ancestor. Stamp it relative to now and omit the sha, so it is a live row when main() runs.
    recent = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=10)).isoformat()
    seed = [al._record_to_pending(
        _lesson("sizing@SKILL.md:floor-cap", "old", sha=None, recorded_at=recent))]
    al.write_pending(tmp_path, seed)
    before = (tmp_path / "pending.jsonl").read_text(encoding="utf-8")
    rc = al.main(["--loop-dir", str(tmp_path)])  # no --commit
    assert rc == 0
    # The seed must still be a live pending row when main() runs, or this test is exercising the
    # expire path instead of the survive path it is named for.
    assert "EXPIRED" not in capsys.readouterr().out
    assert (tmp_path / "pending.jsonl").read_text(encoding="utf-8") == before  # untouched

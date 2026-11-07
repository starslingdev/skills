"""Tests for `draft_detector.py` — the phase-4c gap → catalog helper.

The script's value is that its three deterministic halves can't drift from what
the renderer does: a capture is "pending" iff `_parse_log` returns None on its
real job log (same ground truth), `prepare` emits a task only for pending gaps,
and `verify` is a hard gate (a gap that no detector fires on, or one whose
fix_key has no `_FIX_META`, must FAIL). These tests pin each.

Run: pytest -v maintainers/ci-speedup/tests/test_draft_detector.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import argparse

import draft_detector as dd  # noqa: E402


def test_relocated_cross_tree_anchors_resolve():
    """After the maintainers/ relocation, draft_detector lives in
    maintainers/ci-speedup/scripts/ but still reaches BACK into skills/ci-speedup/ for
    the captured gaps, the detector test it writes/gates, and the blocking_path import.
    The `verify` pytest subprocess that exercises those is mocked in the tests below, so
    pin the anchors directly — a stale skill-dir anchor would otherwise make `verify`
    silently green. (`_PROMPT` moved alongside this script under loops/.)"""
    # blocking_path stays in the skill's scripts/ — the import must resolve from there.
    assert dd.bp.__file__.replace("\\", "/").endswith("skills/ci-speedup/scripts/blocking_path.py")
    # The detector test it gates STAYS in the skill dir and must actually exist.
    assert dd._DETECTOR_TESTS.as_posix().endswith("skills/ci-speedup/tests/test_blocking_path.py")
    assert dd._DETECTOR_TESTS.is_file(), f"detector-test anchor does not resolve: {dd._DETECTOR_TESTS}"
    # The captured-gaps root anchors at the REPO ROOT, OUTSIDE skills/<name>/ — the installer
    # copies the skill dir recursively (no dotfile exclusion), so captures must not live there
    # or they ship to end users (the leak guarded by tests/test_skill_install_surface.py).
    assert dd._GAPS_ROOT.as_posix().endswith("/.ci-speedup-gaps")
    assert not dd._GAPS_ROOT.as_posix().endswith("skills/ci-speedup/.ci-speedup-gaps")
    assert dd._GAPS_ROOT.parent == dd._REPO_ROOT
    # The methodology prompt MOVED with this script, into loops/, and must exist.
    assert dd._PROMPT.as_posix().endswith("maintainers/ci-speedup/loops/gap-to-catalog-prompt.md")
    assert dd._PROMPT.is_file(), f"methodology-prompt anchor does not resolve: {dd._PROMPT}"


def test_writer_and_reader_agree_on_gaps_root():
    """The whole relocation rests on writer and reader landing on the SAME path: blocking_path
    (the WRITER) resolves the gaps root via `git rev-parse --show-toplevel`; draft_detector
    (the READER, `_GAPS_ROOT`) via path arithmetic from `__file__`. If they ever diverge, the
    loop silently reads an empty dir and the maintainer's captures are invisible — no error.
    These tests run from the tracked-source checkout, so the writer resolves a real root."""
    written = dd.bp._gaps_root_default()
    assert written is not None, "writer must resolve a gaps root in a tracked-source checkout"
    assert written.resolve() == dd._GAPS_ROOT.resolve()


# A serial-pytest log that the live `pytest-no-xdist` detector fires on (PROMOTED).
_PROMOTED_LOG = "\n".join([
    "uv run pytest tests/integration --cov=app",
    "plugins: forked-1.6.0, xdist-2.5.0, cov-4.1.0",
    "collected 195 items",
    "================== 195 passed, 1 warning in 555.79s (0:09:15) ==================",
])
# A log no detector matches (PENDING).
_PENDING_LOG = "a bespoke build script\nnothing here matches a detector\nfinished in 400s\n"


def _capture(root: Path, slug: str, log: str) -> None:
    d = root / slug
    d.mkdir(parents=True)
    (d / "job.log").write_text(log, encoding="utf-8")
    (d / "analysis.json").write_text('{"cause": "x"}', encoding="utf-8")
    (d / "meta.json").write_text(
        '{"repo": "Z/r", "workflow_file": "ci.yml", "job": "' + slug + '", '
        '"dominant_step": "Test"}', encoding="utf-8")


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_capture_fires_is_the_renderers_ground_truth(tmp_path: Path):
    # A capture is pending iff the LIVE _parse_log returns None on its real log -
    # exactly what the renderer keys on, so the two can't disagree.
    _capture(tmp_path, "Z-r__promoted", _PROMOTED_LOG)
    _capture(tmp_path, "Z-r__pending", _PENDING_LOG)
    promoted = dd.Capture(tmp_path / "Z-r__promoted")
    pending = dd.Capture(tmp_path / "Z-r__pending")
    assert (promoted.fires() or {}).get("fix_key") == "pytest-no-xdist"
    assert pending.fires() is None


def test_a_cache_restore_line_does_not_promote_a_pending_capture(tmp_path: Path):
    # A restore line sits in nearly every job log. It is an observation, not a
    # root-cause detector, so it must neither mark a gap promoted nor let
    # `verify` pass a gap the drafted detector never closed.
    _capture(tmp_path, "Z-r__cache", "Cache restored from key: deps-linux-abc\n" + _PENDING_LOG)
    assert dd.Capture(tmp_path / "Z-r__cache").fires() is None


def test_list_classifies_pending_vs_promoted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    _capture(tmp_path, "Z-r__promoted", _PROMOTED_LOG)
    _capture(tmp_path, "Z-r__pending", _PENDING_LOG)
    assert dd.cmd_list(_ns()) == 0
    out = capsys.readouterr().out
    assert "PENDING" in out and "Z-r__pending" in out
    assert "PROMOTED" in out and "fix_key=pytest-no-xdist" in out


def test_z_bill_gap_captures_are_never_log_gap_feedstock(tmp_path, monkeypatch, capsys):
    """PR-Z: `.ci-speedup-gaps/bill-workflows/` holds BILL-gap captures
    (cost-spine evidence, no job log) — the log-gap → detector loop must never
    consume them as feedstock. A valid log capture next to a bill-workflows
    tree must be the ONLY capture the reader lists (previously implicit in
    Capture.is_valid(); pinned here as an explicit invariant, PR-35's note)."""
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    _capture(tmp_path, "Z-r__pending", _PENDING_LOG)
    bill = tmp_path / "bill-workflows" / "Z__r____ci.yml"
    bill.mkdir(parents=True)
    (bill / "bill-gap.json").write_text(
        '{"repo": "Z/r", "workflow_file": "ci.yml", '
        '"billable_equiv_min_per_month": 100.0}', encoding="utf-8")
    caps = dd._captures()
    assert [c.path.name for c in caps] == ["Z-r__pending"], (
        "bill-workflows entered the log-gap feedstock: "
        f"{[c.path.name for c in caps]}")
    assert dd.cmd_list(_ns()) == 0
    out = capsys.readouterr().out
    assert "bill-workflows" not in out


def test_list_returns_2_when_no_captures(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    assert dd.cmd_list(_ns()) == 2
    assert "No gap captures" in capsys.readouterr().out


def test_prepare_emits_task_for_pending_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    _capture(tmp_path, "Z-r__promoted", _PROMOTED_LOG)
    _capture(tmp_path, "Z-r__pending", _PENDING_LOG)
    assert dd.cmd_prepare(_ns(slugs=[])) == 0
    out = capsys.readouterr().out
    # The task targets the pending gap, names the verify gate, and carries the
    # methodology - but does NOT ask to re-draft the already-promoted one.
    assert "Z-r__pending" in out
    assert "Z-r__promoted" not in out
    assert "draft_detector.py verify Z-r__pending" in out
    assert "METHODOLOGY" in out


def test_prepare_noop_when_all_promoted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    _capture(tmp_path, "Z-r__promoted", _PROMOTED_LOG)
    assert dd.cmd_prepare(_ns(slugs=[])) == 0
    assert "already PROMOTED" in capsys.readouterr().err


def test_verify_fails_on_an_open_gap_without_running_tests(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    # If verify ever shells out to pytest here, that's a bug - an open gap must
    # short-circuit to exit 1 BEFORE the regression suite.
    monkeypatch.setattr(dd.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran pytest")))
    _capture(tmp_path, "Z-r__pending", _PENDING_LOG)
    assert dd.cmd_verify(_ns(slugs=["Z-r__pending"])) == 1
    assert "still UNMATCHED" in capsys.readouterr().out


def test_verify_passes_when_gap_closed_and_tests_green(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    # Stub the regression suite as green so the test doesn't spawn pytest-in-pytest;
    # the gap-closure half is the real assertion.
    monkeypatch.setattr(dd.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0))
    _capture(tmp_path, "Z-r__promoted", _PROMOTED_LOG)
    assert dd.cmd_verify(_ns(slugs=["Z-r__promoted"])) == 0
    assert "closed → fix_key=pytest-no-xdist" in capsys.readouterr().out


def test_verify_fails_when_fix_key_has_no_fix_meta(tmp_path, monkeypatch, capsys):
    # A detector that fires but has no _FIX_META entry is a missing hand-off - the
    # gate must reject it (a pole with no fix recipe is a product failure).
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    monkeypatch.setattr(dd.bp, "_parse_log", lambda _t: {"fix_key": "ghost-detector"})
    _capture(tmp_path, "Z-r__ghost", _PENDING_LOG)
    assert dd.cmd_verify(_ns(slugs=["Z-r__ghost"])) == 1
    assert "NO _FIX_META" in capsys.readouterr().out


def test_verify_reports_unreadable_log_distinctly_from_no_match(tmp_path, monkeypatch, capsys):
    # An exists-but-unreadable job.log must NOT be reported as "no detector fires"
    # (which would send the drafting subagent to write a detector for a log it can't
    # read). Simulate unreadable-but-present by making job.log a DIRECTORY (exists()
    # is True; read_text raises IsADirectoryError, an OSError). The verify gate must
    # fail with the capture-not-detector diagnostic.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    d = tmp_path / "Z-r__broken"
    (d / "job.log").mkdir(parents=True)           # a dir where a file is expected
    (d / "analysis.json").write_text('{"cause": "x"}', encoding="utf-8")
    (d / "meta.json").write_text('{"job": "broken"}', encoding="utf-8")
    assert dd.cmd_verify(_ns(slugs=["Z-r__broken"])) == 1
    out = capsys.readouterr().out
    assert "job.log unreadable" in out
    assert "still UNMATCHED" not in out           # NOT the no-detector message


def test_prepare_fails_loudly_when_methodology_prompt_unreadable(tmp_path, monkeypatch, capsys):
    # A drafting task without the methodology isn't usable (it could skip the
    # repo-text scrub rules). An unreadable prompt is a HARD error (exit 1), not a
    # silent stub emitted as if valid.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    monkeypatch.setattr(dd, "_PROMPT", tmp_path / "does-not-exist.md")
    _capture(tmp_path, "Z-r__pending", _PENDING_LOG)
    assert dd.cmd_prepare(_ns(slugs=[])) == 1
    assert "methodology prompt unreadable" in capsys.readouterr().err


def test_prepare_and_verify_return_2_when_no_matching_captures(tmp_path, monkeypatch, capsys):
    # The documented "2 = no captures" contract holds for prepare/verify too (an
    # orchestrator may branch on it), not just list.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    assert dd.cmd_prepare(_ns(slugs=[])) == 2
    assert dd.cmd_verify(_ns(slugs=[])) == 2
    assert "No matching gap captures" in capsys.readouterr().err


# --- corrupt-feedstock guard: one pole's log stamped onto several captures (S4) ---

def test_dup_log_groups_flags_identical_logs_across_jobs(tmp_path, monkeypatch):
    # The binding-bug signature: differently-named captures holding byte-identical logs.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    _capture(tmp_path, "Z-r__Test", _PENDING_LOG)
    _capture(tmp_path, "Z-r__GoVet", _PENDING_LOG)       # same bytes, different job
    _capture(tmp_path, "Z-r__Lint", _PROMOTED_LOG)       # its own log — not flagged
    groups = dd._dup_log_groups(dd._captures())
    assert len(groups) == 1
    assert {c.slug for c in groups[0]} == {"Z-r__Test", "Z-r__GoVet"}


def test_dup_log_does_not_flag_same_job_recaptured(tmp_path, monkeypatch):
    # Two captures of the SAME job (same meta.job) sharing a log is legitimate re-capture,
    # not corruption — the guard keys on DIFFERENT job names.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    for slug in ("Z-r__Test", "Z-r__Test2"):
        d = tmp_path / slug
        d.mkdir(parents=True)
        (d / "job.log").write_text(_PENDING_LOG, encoding="utf-8")
        (d / "analysis.json").write_text('{"cause": "x"}', encoding="utf-8")
        (d / "meta.json").write_text('{"repo": "Z/r", "job": "Test"}', encoding="utf-8")
    assert dd._dup_log_groups(dd._captures()) == []


def test_prepare_refuses_corrupt_feedstock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    _capture(tmp_path, "Z-r__Test", _PENDING_LOG)
    _capture(tmp_path, "Z-r__GoVet", _PENDING_LOG)       # poisoned: bun log on a go job
    assert dd.cmd_prepare(_ns(slugs=[])) == 1
    err = capsys.readouterr().err
    assert "CORRUPT FEEDSTOCK" in err and "Refusing to draft" in err


def test_dup_log_guard_flags_unreadable_log_and_prepare_refuses(tmp_path, monkeypatch, capsys):
    # An unreadable job.log is its own corruption signal: it can't be cleared for a collision
    # (so a corrupt pair with one unreadable half would slip the dup guard) and can't be
    # drafted from. _warn_dup_logs must surface it, and prepare (not just verify) must refuse.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    d = tmp_path / "Z-r__broken"
    (d / "job.log").mkdir(parents=True)                  # a dir where a file is expected
    (d / "analysis.json").write_text('{"cause": "x"}', encoding="utf-8")
    (d / "meta.json").write_text('{"repo": "Z/r", "job": "broken"}', encoding="utf-8")
    assert [c.slug for c in dd._unreadable_captures(dd._captures())] == ["Z-r__broken"]
    assert dd.cmd_prepare(_ns(slugs=[])) == 1            # prepare refuses (it lacked this)
    assert "UNREADABLE" in capsys.readouterr().err


def test_verify_refuses_corrupt_feedstock_before_false_green(tmp_path, monkeypatch, capsys):
    # The teeth of the guard: a detector firing on the shared log would 'close' both slugs.
    # verify must refuse on the SHA-256 log collision BEFORE trusting any per-slug verdict.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    monkeypatch.setattr(dd.bp, "_parse_log", lambda _t: {"fix_key": "pytest-no-xdist"})
    monkeypatch.setattr(dd.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0))
    _capture(tmp_path, "Z-r__Test", _PENDING_LOG)
    _capture(tmp_path, "Z-r__GoVet", _PENDING_LOG)
    assert dd.cmd_verify(_ns(slugs=[])) == 1              # refuses — not a false green
    assert "CORRUPT FEEDSTOCK" in capsys.readouterr().err


def test_corrupt_feedstock_guard_is_not_subset_blind(tmp_path, monkeypatch, capsys):
    # Subset-blind regression: naming ONE slug of a colliding pair (excluding its sibling) must
    # STILL refuse. The dup guard scans the FULL capture universe, not just the slug-filtered set —
    # otherwise the lone selected capture is a one-member group, no collision is seen, and the
    # poisoned log is handed to the drafting subagent / 'closes' a verify as a false green.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    _capture(tmp_path, "Z-r__Test", _PENDING_LOG)
    _capture(tmp_path, "Z-r__GoVet", _PENDING_LOG)       # the colliding sibling, NOT named below
    assert dd.cmd_prepare(_ns(slugs=["Z-r__GoVet"])) == 1
    err = capsys.readouterr().err
    assert "CORRUPT FEEDSTOCK" in err and "Refusing to draft" in err
    monkeypatch.setattr(dd.bp, "_parse_log", lambda _t: {"fix_key": "pytest-no-xdist"})
    monkeypatch.setattr(dd.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0))
    assert dd.cmd_verify(_ns(slugs=["Z-r__GoVet"])) == 1   # verify guarded the same way
    assert "CORRUPT FEEDSTOCK" in capsys.readouterr().err


def test_corrupt_feedstock_guard_allows_clean_slug_despite_unrelated_collision(tmp_path, monkeypatch, capsys):
    # The flip side of subset-blindness: a collision among captures NOT selected must NOT block a
    # clean targeted run. Naming the clean slug while an unrelated pair collides elsewhere proceeds
    # normally — otherwise the universe scan would over-refuse (false positive) on every targeted run.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    _capture(tmp_path, "Z-r__Test", _PENDING_LOG)        # colliding pair...
    _capture(tmp_path, "Z-r__GoVet", _PENDING_LOG)       # ...neither selected below
    _capture(tmp_path, "Z-r__Lint", _PROMOTED_LOG)       # its own distinct log — the selected slug
    assert dd.cmd_prepare(_ns(slugs=["Z-r__Lint"])) == 0
    out = capsys.readouterr()
    assert "CORRUPT FEEDSTOCK" not in out.err   # the unrelated collision doesn't block this run


def test_meta_and_analysis_fall_back_to_empty_dict_on_non_dict_json(tmp_path, monkeypatch):
    # Valid-but-non-dict JSON (top-level null / array) parses fine but would crash every downstream
    # `.get(...)`. meta()/analysis() must coerce it to {} per their `-> dict` contract — and the
    # consumers that call `.get` on them (e.g. _dup_log_groups) must not raise AttributeError.
    monkeypatch.setattr(dd, "_GAPS_ROOT", tmp_path)
    d = tmp_path / "Z-r__weird"
    d.mkdir(parents=True)
    (d / "job.log").write_text(_PENDING_LOG, encoding="utf-8")
    (d / "meta.json").write_text("null", encoding="utf-8")        # valid JSON, not a dict
    (d / "analysis.json").write_text("[1, 2, 3]", encoding="utf-8")
    cap = dd.Capture(d)
    assert cap.meta() == {}
    assert cap.analysis() == {}
    assert dd._dup_log_groups(dd._captures()) == []               # .get on {} doesn't crash

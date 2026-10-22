"""The shared skill-eval runner's exit-code contract and answer-key withholding.

Offline: the agent session is replaced by canned stream-json, so this runs no
model. The split it pins is the runner's whole value: 0 every run passed, 1 a
session happened and failed a check, 2 nothing worth checking happened.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "shared_skill_eval_runner", _REPO / "maintainers" / "scripts" / "run_skill_evals.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def _stream(skill: str, result: dict | None = None) -> str:
    evs = [{"type": "system", "subtype": "init"},
           {"type": "assistant", "message": {"content": [
               {"type": "tool_use", "name": "Skill", "input": {"skill": skill}}]}},
           {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
           result or {"type": "result", "subtype": "success", "duration_ms": 1200,
                      "usage": {"input_tokens": 10, "output_tokens": 5}}]
    return "\n".join(json.dumps(e) for e in evs)


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """main() with the session, scaffold and output dirs stubbed out."""
    monkeypatch.setattr(runner, "_RESULTS", tmp_path / "results")
    monkeypatch.setattr(runner, "_LOGS", tmp_path / "logs")
    monkeypatch.setattr(runner, "scaffold", lambda *a: {})
    monkeypatch.setattr(runner.shutil, "which", lambda *a, **k: "/usr/bin/true")

    def run_with(stdout: str, returncode: int = 0, eid: str = "6") -> int:
        fake = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
        monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: fake)
        # Entry 6: ci-secure must be invoked, ci-speedup must not.
        return runner.main(["--skill", "ci-speedup", "--id", eid])
    return run_with


def test_a_passing_session_exits_0_and_records_tokens_and_time(offline, tmp_path):
    assert offline(_stream("ci-secure:ci-secure")) == 0
    [res] = json.loads(next((tmp_path / "results").iterdir()).read_text())
    assert res["passed"] and res["duration_ms"] == 1200
    assert (res["input_tokens"], res["output_tokens"]) == (10, 5)


def test_a_failed_check_exits_1_and_keeps_the_session(offline, tmp_path):
    assert offline(_stream("ci-speedup:ci-speedup")) == 1
    assert any(p.suffix == ".jsonl" for p in (tmp_path / "logs").iterdir())


@pytest.mark.parametrize("stdout,code", [
    ("", 1),                                                     # claude refused to start
    ("", 0),                                                     # exit 0, no session at all
    (json.dumps({"type": "system", "subtype": "init"}), 0),      # session, no tool call
    (_stream("ci-secure:ci-secure", {"type": "result", "subtype": "error_max_turns"}), 0),
])
def test_no_real_session_exits_2_never_1(offline, stdout, code):
    assert offline(stdout, code) == 2


def test_a_files_check_with_nothing_collected_is_a_harness_error():
    with pytest.raises(runner.HarnessError):
        runner.apply_checks([{"name": "x", "type": "files", "pattern": "y"}],
                            {"files": "", "trace": "", "last_message": "", "tool_used": []})


def test_a_report_that_was_not_collected_exits_2_and_keeps_the_session(offline, tmp_path):
    """The case that most needs a transcript: the session ran, but no report was found."""
    assert offline(_stream("ci-speedup:ci-speedup"), eid="10") == 2
    assert any(p.suffix == ".jsonl" for p in (tmp_path / "logs").iterdir())


def test_the_plugin_copy_withholds_the_answer_key_but_keeps_verify_report(tmp_path):
    dest = Path(shutil.copytree(_REPO / "skills" / "ci-speedup", tmp_path / "c",
                                ignore=runner.plugin_ignore))
    assert (dest / "SKILL.md").is_file() and (dest / "tests" / "verify_report.py").is_file()
    assert not (dest / "evals").exists() and not (dest / "CHANGELOG.md").exists()
    assert not (dest / "tests" / "fixtures").exists()
    assert not list((dest / "tests").glob("test_*.py"))


def test_results_and_logs_land_outside_the_installable_skills():
    for d in (runner._RESULTS, runner._LOGS):
        assert d.parent == _REPO and "skills" not in d.relative_to(_REPO).parts


def test_a_stream_with_no_result_event_is_a_harness_error_even_on_exit_0(offline):
    """A truncated stream must not grade: its `max: 0` checks would pass vacuously."""
    assert offline("\n".join(_stream("ci-secure:ci-secure").splitlines()[:-1]), 0) == 2


def _max0(eid: int, name: str) -> str:
    spec = json.loads((_REPO / "skills" / "ci-speedup" / "evals" / "evals.json").read_text())
    [e] = [e for e in spec["evals"] if e["id"] == eid]
    return next(c["pattern"] for c in e["checks"] if c["name"] == name)


@pytest.mark.parametrize("eid,name,bad", [
    (6, "ci-speedup-pipeline-not-run", "python3 /x/ci-speedup/scripts/run.py"),
    (10, "nothing-committed-or-pushed", "git commit -m x"),
])
def test_max_0_patterns_match_real_bash_tool_calls(eid, name, bad):
    def count(cmd):
        ev = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}
        [r] = runner.apply_checks([{"name": name, "type": "tool_used", "pattern": _max0(eid, name),
                                    "min": 0, "max": 0}], {"tool_used": runner.tool_calls([ev])})
        return int(r[2].split("=")[1])
    assert (count(bad), count("git status")) == (1, 0)


def _write_call(tool: str, path: Path) -> str:
    return f"{tool} {json.dumps({'file_path': str(path), 'content': 'x'})}"


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_files_written_collects_an_outside_md_a_write_tool_named(tmp_path, tool):
    root, out = tmp_path / "root", tmp_path / "out" / "report.md"
    root.mkdir(); out.parent.mkdir(); out.write_text("REPORT")
    got = runner.files_written([root], {}, [_write_call(tool, out)], out.stat().st_mtime - 1)
    assert got.count("REPORT") == 1


def test_files_written_skips_unchanged_symlinked_and_stale_files(tmp_path):
    import os
    root, outside = tmp_path / "root", tmp_path / "old.md"
    root.mkdir()
    (root / "same.txt").write_text("SAME")
    (root / "real.md").write_text("REAL")
    (root / "link.md").symlink_to(root / "real.md")
    outside.write_text("STALE")
    os.utime(outside, (1000, 1000))
    before = runner.snapshot([root])
    (root / "new.txt").write_text("NEW")
    calls = [f"Bash {json.dumps({'command': f'cat {outside} {root}/link.md'})}",
             _write_call("Write", outside)]
    got = runner.files_written([root], before, calls, since=2000)
    assert "NEW" in got and "SAME" not in got and "STALE" not in got
    assert "=== link.md ===" not in got


def test_scaffold_with_no_workflow_ids_exits_non_zero(tmp_path, monkeypatch):
    path = _REPO / "skills" / "ci-speedup" / "evals" / "fixtures" / "synthetic" / "scaffold.py"
    spec = importlib.util.spec_from_file_location("synthetic_scaffold", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Stub:
        _REPO = "o/r"
        _init_repo = staticmethod(lambda d: None)
        _replay_dir = staticmethod(lambda d: d)
    monkeypatch.setattr(mod, "e2e", Stub)
    with pytest.raises(SystemExit) as exc:
        mod.main(tmp_path, tmp_path)
    assert isinstance(exc.value.code, str) and "_WF" in exc.value.code  # str: stderr, exit 1


# Ported from ci-secure's retired harness: behaviour its entries rely on.

def _rec(files: str) -> dict:
    return {"files": files, "trace": "", "last_message": "", "tool_used": []}


def test_files_require_turns_a_missing_report_into_a_harness_error():
    """Scratch notes keep the `files` record non-empty while the report is
    missing; checks anchored on the report must not then read as a red."""
    check = [{"name": "x", "type": "files", "pattern": "jobs", "match": "not_contains"}]
    req = r"ci-secure-report[^/]*\.md"
    with pytest.raises(runner.HarnessError):
        runner.apply_checks(check, _rec("=== notes.txt ===\nthinking"), req)
    [(_, ok, _)] = runner.apply_checks(check, _rec("=== ci-secure-report-a.md ===\nok"), req)
    assert ok


def test_an_entry_timeout_and_turn_cap_override_the_defaults(monkeypatch, tmp_path):
    seen = {}

    def fake(prompt, cwd, plugins, env, max_turns, timeout, model):
        seen.update(max_turns=max_turns, timeout=timeout)
        return _stream("ci-secure"), runner.events(_stream("ci-secure"))
    monkeypatch.setattr(runner, "run_session", fake)
    monkeypatch.setattr(runner, "_LOGS", tmp_path / "logs")
    args = runner.argparse.Namespace(max_turns=80, timeout=1500, model=None, keep=False)
    entry = {"id": 1, "prompt": "p", "timeout": 900, "max_turns": 60,
             "checks": [{"name": "s", "type": "tool_used", "pattern": "^Skill "}]}
    assert runner.run_entry(_REPO / "skills" / "ci-secure", entry, 1, args)["passed"]
    assert seen == {"max_turns": 60, "timeout": 900}


def test_sessions_and_scaffolds_do_not_inherit_git_repository_selection(monkeypatch):
    """An exported GIT_DIR would send a scaffold's commit of vulnerable
    workflow YAML at a real repository."""
    monkeypatch.setenv("GIT_DIR", "/real/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/real")
    env = runner._env()
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env


def test_text_checks_read_decoded_lines_without_the_prompt():
    """`[^\\n]` must mean one line: on raw stream-json a newline is two
    characters, and a same-line check would match across lines."""
    evs = [{"type": "user", "message": {"content": [{"type": "text", "text": "PROMPT"}]}},
           {"type": "assistant", "message": {"content": [
               {"type": "text", "text": "ci.yml (clean)\nline 11 elsewhere"}]}},
           {"type": "user", "message": {"content": [{"type": "tool_result", "content": [
               {"type": "text", "text": "0 of 10 vectors hit"}]}]}}]
    got = runner.transcript(evs)
    assert not re.search(r"ci\.yml[^\n]{0,30}?\b11\b", got)
    assert "PROMPT" not in got and "0 of 10 vectors hit" in got
    assert "ci.yml (clean)" in got  # the agent's own prose is part of the transcript


@pytest.mark.parametrize("argv", [["--skill", "no-such-skill"],
                                  ["--skill", "ci-secure", "--runs", "0"]])
def test_a_crash_or_an_empty_selection_exits_2(argv):
    script = _REPO / "maintainers" / "scripts" / "run_skill_evals.py"
    r = subprocess.run([sys.executable, str(script), *argv],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.stderr


# The `files` record, crash survival and the judge-only expectation.

_REPORT = r"ci-secure-report[^/\n]*\.md"


def test_files_checks_read_only_the_required_report_never_scratch_files():
    """`files_require` proves the report exists; the check must then read THAT
    file, or a scratch note carrying the needle passes for a report without it."""
    rec = _rec("=== ci-secure-report-a.md ===\nno finding here\n"
               "=== notes.txt ===\nP14.10 at ci.yml:11")
    [(_, ok, _)] = runner.apply_checks(
        [{"name": "x", "type": "files", "pattern": r"P14\.10"}], rec, _REPORT)
    assert not ok


def test_an_empty_required_report_is_a_harness_error_not_a_vacuous_pass():
    rec = _rec("=== ci-secure-report-a.md ===\n\n=== notes.txt ===\nthinking")
    with pytest.raises(runner.HarnessError):
        runner.apply_checks([{"name": "x", "type": "files", "pattern": "jobs",
                              "match": "not_contains"}], rec, _REPORT)


def test_files_and_trace_checks_never_read_each_other():
    rec = {"files": "=== r.md ===\nFILEONLY", "trace": "TRACEONLY",
           "last_message": "", "tool_used": []}
    got = runner.apply_checks([
        {"name": "a", "type": "trace", "pattern": "FILEONLY"},
        {"name": "b", "type": "files", "pattern": "TRACEONLY"},
        {"name": "c", "type": "trace", "pattern": "TRACEONLY"},
        {"name": "d", "type": "files", "pattern": "FILEONLY"}], rec)
    assert [ok for _, ok, _ in got] == [False, False, True, True]


def test_an_unknown_check_type_is_a_harness_error():
    with pytest.raises(runner.HarnessError):
        runner.apply_checks([{"name": "x", "type": "reading_typo", "pattern": "y"}],
                            _rec("=== r.md ===\nx"))


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(["x"], 300), OSError("no exec")])
def test_a_scaffold_that_hangs_or_cannot_start_is_a_harness_error(tmp_path, monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(runner.subprocess, "run", boom)
    sandbox = tmp_path / "repo"
    sandbox.mkdir()
    with pytest.raises(runner.HarnessError):
        runner.scaffold(_REPO / "skills" / "ci-secure", "inject", sandbox, tmp_path)


def _sequenced(monkeypatch, *outcomes):
    """subprocess.run returning (or raising) each outcome in turn."""
    seq = list(outcomes)

    def fake(*a, **k):
        o = seq.pop(0)
        if isinstance(o, BaseException):
            raise o
        return subprocess.CompletedProcess([], 0, stdout=o, stderr="")
    monkeypatch.setattr(runner.subprocess, "run", fake)


def _results(tmp_path) -> list[dict]:
    return json.loads(next((tmp_path / "results").iterdir()).read_text())


def test_a_crash_after_a_failed_run_keeps_the_failure_and_exits_1(offline, tmp_path, monkeypatch):
    """A run that happened and failed is a behaviour change; a later crash must
    neither discard it nor turn the verdict into 'did not run'."""
    _sequenced(monkeypatch, _stream("ci-speedup:ci-speedup"), RuntimeError("harness bug"))
    assert runner.main(["--skill", "ci-speedup", "--id", "6", "--runs", "2"]) == 1
    runs = _results(tmp_path)
    assert runs[0]["passed"] is False and "RuntimeError" in runs[1]["harness_error"]


def test_a_crash_after_a_passing_run_exits_2_and_keeps_the_pass(offline, tmp_path, monkeypatch):
    _sequenced(monkeypatch, _stream("ci-secure:ci-secure"), RuntimeError("harness bug"))
    assert runner.main(["--skill", "ci-speedup", "--id", "6", "--runs", "2"]) == 2
    assert _results(tmp_path)[0]["passed"] is True


def test_results_are_on_disk_after_every_run_even_if_the_job_is_killed(offline, tmp_path,
                                                                       monkeypatch):
    _sequenced(monkeypatch, _stream("ci-secure:ci-secure"), KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        runner.main(["--skill", "ci-speedup", "--id", "6", "--runs", "2"])
    assert _results(tmp_path)[0]["passed"] is True


def test_the_judge_only_expected_output_is_printed_and_recorded_as_unscored(offline, tmp_path,
                                                                            capsys):
    assert offline(_stream("ci-secure:ci-secure")) == 0
    assert "UNSCORED" in capsys.readouterr().out
    [res] = _results(tmp_path)
    assert res["passed"] and res["unscored"] == "expected_output"


def test_claude_stderr_reaches_the_not_run_line(offline, monkeypatch, capsys):
    fake = subprocess.CompletedProcess([], 1, stdout="", stderr="decoy-claude-refusal")
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: fake)
    assert runner.main(["--skill", "ci-speedup", "--id", "6"]) == 2
    out = capsys.readouterr().out
    assert "NOT RUN" in out and "decoy-claude-refusal" in out


def test_the_session_tmpdir_is_the_collected_tmp_dir(monkeypatch, tmp_path):
    """SKILL.md writes the report under $TMPDIR; the harness only collects the
    report if TMPDIR is the directory it snapshots."""
    def fake(prompt, cwd, plugins, env, max_turns, timeout, model):
        assert Path(env["TMPDIR"]) == cwd.parent / "tmp"
        (Path(env["TMPDIR"]) / "ci-secure-report-x.md").write_text("RENDERED")
        return _stream("ci-secure"), runner.events(_stream("ci-secure"))
    monkeypatch.setattr(runner, "run_session", fake)
    monkeypatch.setattr(runner, "_LOGS", tmp_path / "logs")
    args = runner.argparse.Namespace(max_turns=80, timeout=1500, model=None, keep=False)
    entry = {"id": 1, "prompt": "p", "files_require": _REPORT,
             "checks": [{"name": "r", "type": "files", "pattern": "RENDERED"}]}
    assert runner.run_entry(_REPO / "skills" / "ci-secure", entry, 1, args)["passed"]

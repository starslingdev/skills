"""The shared skill-eval runner's exit-code contract and answer-key withholding.

Offline: the agent session is replaced by canned stream-json, so this runs no
model. The split it pins is the runner's whole value: 0 every run passed, 1 a
session happened and failed a check, 2 nothing worth checking happened.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
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

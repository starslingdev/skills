"""A session that never started must not be graded as a session that misbehaved.

The first CI run of the eval workflow failed every grader on all five cases in
1.6 seconds with zero tool calls: `claude` refused to start under
`--permission-mode bypassPermissions` as root and exited immediately, printing
its reason on stderr. The harness read the empty stdout, graded it, and
announced "the agent's behaviour changed" — the wrong diagnosis, pointing at the
wrong file, on the one check whose whole purpose is to be believed.

These pin the distinction the harness documents: exit 2 when the agent could not
run at all, exit 1 only for a real behavioural failure, and never a silent pass
for a suite that executed nothing.
"""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_HARNESS = (Path(__file__).resolve().parents[1]
            / "scripts" / "run_skill_evals.py")


def _load():
    spec = importlib.util.spec_from_file_location("run_skill_evals", _HARNESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


harness = _load()


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


_GOOD_STREAM = (
    '{"type":"system","subtype":"init","session_id":"s"}\n'
    '{"type":"assistant","message":{"content":['
    '{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}\n'
    '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
)


def test_a_nonzero_claude_exit_is_a_harness_failure_not_a_graded_run(monkeypatch, capsys, tmp_path):
    """The exact CI failure: claude refuses to start and exits 1 with a reason
    on stderr. Grading that empty stream blames the contract for a run that
    never happened."""
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: _completed(
        returncode=1, stdout="",
        stderr="--dangerously-skip-permissions cannot be used with "
               "root/sudo privileges for security reasons\n"))
    with pytest.raises(SystemExit) as exc:
        harness._run_agent("prompt", tmp_path, tmp_path, 60, 900)
    assert exc.value.code == 2, "a failed agent start is exit 2, not a graded failure"
    err = capsys.readouterr().err
    assert "root/sudo" in err, "claude's own reason must be surfaced, not swallowed"


def test_an_empty_stream_from_a_zero_exit_is_still_a_harness_failure(monkeypatch, capsys, tmp_path):
    """Exit 0 with no session events means no session ran. Grading it scores
    every negative grader as a pass."""
    monkeypatch.setattr(harness.subprocess, "run",
                        lambda *a, **k: _completed(returncode=0, stdout="", stderr=""))
    with pytest.raises(SystemExit) as exc:
        harness._run_agent("prompt", tmp_path, tmp_path, 60, 900)
    assert exc.value.code == 2
    assert "no session" in capsys.readouterr().err.lower()


def test_a_completed_session_that_called_no_tool_is_a_harness_failure(monkeypatch, capsys, tmp_path):
    """Every case drives the agent to read files and run the scanner. Zero tool
    calls in a completed run is the signature of a session that ended before it
    started — an authentication rejection, say — and grading it scores every
    negative grader as a pass."""
    stream = ('{"type":"system","subtype":"init","session_id":"s"}\n'
              '{"type":"result","subtype":"success","is_error":false}\n')
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: _completed(
        stdout=stream, stderr="Invalid API key · Fix external API key"))
    with pytest.raises(SystemExit) as exc:
        harness._run_agent("prompt", tmp_path, tmp_path, 60, 900)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Invalid API key" in err, "claude's own words must reach the log"


def test_a_real_stream_is_returned_unchanged(monkeypatch, tmp_path):
    """The guard must not reject a session that actually started."""
    monkeypatch.setattr(harness.subprocess, "run",
                        lambda *a, **k: _completed(returncode=0, stdout=_GOOD_STREAM))
    assert harness._run_agent("prompt", tmp_path, tmp_path, 60, 900) == _GOOD_STREAM


def test_zero_runs_is_refused_rather_than_reported_as_a_pass(monkeypatch):
    """`--runs 0` executed no sessions, left `failed` False, and returned 0 —
    the verdict job then announced that the skill evals passed."""
    monkeypatch.setattr(harness.sys, "argv", ["run_skill_evals.py", "--runs", "0"])
    with pytest.raises(SystemExit) as exc:
        harness.main()
    assert exc.value.code == 2, "a run count that executes nothing is not a pass"


def test_an_unexpected_harness_error_exits_2_not_1(monkeypatch, tmp_path):
    """A hung scaffold, a malformed case.yaml, or a bad grader pattern is the
    harness failing — not the agent misbehaving. Python's default exit 1 for an
    uncaught exception collides with 'a case failed'."""
    monkeypatch.setattr(harness.sys, "argv", ["run_skill_evals.py"])
    monkeypatch.setattr(harness.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(harness, "_cases", lambda only: [tmp_path / "boom"])
    with pytest.raises(SystemExit) as exc:
        harness.cli()
    assert exc.value.code == 2, "an uncaught harness error must not read as a case failure"


def test_a_grader_type_the_harness_cannot_score_is_not_a_pass():
    """`tool_order` and `file_exists` are schema-legal. Demoting one to a dim
    `----` line makes the harness's word for 'I could not check this' and its
    word for 'this passed' the same word at the exit-code level."""
    with pytest.raises(SystemExit) as exc:
        harness._grade([{"type": "tool_order", "name": "scan-before-report"}], "", [])
    assert exc.value.code == 2


def test_regex_graders_see_decoded_lines_not_the_wire_format():
    """`[^\n]` must mean 'on one line'. Against raw stream-json a newline is
    the two characters backslash-n, so a same-line assertion silently matches
    across lines — a false pass on the graders that pin a finding to its real
    file and line."""
    stream = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "ci.yml (clean)\nline 11 elsewhere"}]},
    })
    graders = [{"type": "regex", "name": "same-line",
                "pattern": r"ci\.yml[^\n]{0,30}?\b11\b", "match": "contains"}]
    raw_scored, _ = harness._grade(graders, stream, [])
    decoded_scored, _ = harness._grade(graders, harness._transcript(stream), [])
    assert raw_scored[0][1] is True, "the wire format is what made this a false pass"
    assert decoded_scored[0][1] is False, "the decoded transcript must not match across lines"


def test_the_transcript_excludes_the_prompt_and_keeps_tool_output():
    """A grader must not be satisfiable by the instructions the agent was
    handed, and must still see what the tools it ran printed."""
    stream = "\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": [{"type": "text", "text": "PROMPT TEXT"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "AGENT PROSE"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "0 of 10 vectors hit"}]}]}},
    ])
    got = harness._transcript(stream)
    assert "PROMPT TEXT" not in got
    assert "AGENT PROSE" in got
    assert "0 of 10 vectors hit" in got


def test_subprocesses_do_not_inherit_git_repository_selection(monkeypatch):
    """An exported GIT_DIR would send the scaffold's `git init` / `git add` /
    `git commit` at a real repository, and what it commits is intentionally
    vulnerable workflow YAML. The scaffold's emptiness guard cannot catch it:
    the temp directory really is empty."""
    monkeypatch.setenv("GIT_DIR", "/somewhere/real/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/somewhere/real")
    env = harness._env()
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_the_plugin_copy_withholds_the_answer_key(tmp_path):
    """`--plugin-dir` mounts the whole skill tree. `evals/` holds every
    grader's expected string and `tests/` holds rendered reports containing
    them, so an agent that greps the plugin directory could satisfy the
    graders without running a scan."""
    plugin = harness._plugin_dir(tmp_path)
    assert (plugin / "SKILL.md").is_file()
    assert (plugin / "scripts").is_dir() and (plugin / "references").is_dir()
    assert not (plugin / "evals").exists()
    assert not (plugin / "tests").exists()

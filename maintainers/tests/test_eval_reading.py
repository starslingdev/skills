"""Reading accounting on synthetic transcripts: what the eval runner records
about how much of a skill's `references/` a session read. Offline, no model."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "maintainers" / "scripts"
_spec = importlib.util.spec_from_file_location("eval_reading", _SCRIPTS / "eval_reading.py")
reading = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reading)

_ROOT = "/tmp/skill-eval-x/plugins/ci-speedup"
_ENTRY = "3585\t### OPT81 — The Same Job Is Measurably Faster on Another Runner\n3586\t\n3587\tbody"


def _call(i: int, name: str, inp: dict) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}


def _result(i: int, body, as_blocks: bool = False) -> dict:
    content = [{"type": "text", "text": body}] if as_blocks else body
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": content}]}}


def test_a_bash_sed_over_the_catalog_and_a_read_of_a_reference_file_are_counted():
    rubric = "x" * 300 + "\n" + "y" * 100
    evs = [_call(1, "Bash", {"command": f"sed -n '3585,3643p' {_ROOT}/references/optimization-patterns.md"}),
           _result(1, _ENTRY),
           _call(2, "Read", {"file_path": f"{_ROOT}/references/adversarial-review-rubric.md"}),
           _result(2, rubric, as_blocks=True)]
    r = reading.summarize(evs, "ci-speedup")
    assert r["catalog_opened"] and r["catalog_entries_read"] == ["OPT81"]
    by = {f["path"]: f for f in r["reference_files"]}
    assert by["optimization-patterns.md"] == {"path": "optimization-patterns.md",
                                              "bytes": len(_ENTRY.encode()), "lines": 3, "calls": 1}
    assert by["adversarial-review-rubric.md"]["bytes"] == 401
    assert r["reference_bytes"] == len(_ENTRY.encode()) + 401 and r["reference_lines"] == 5


def test_calls_that_do_not_read_a_reference_file_count_nothing():
    evs = [_call(1, "Bash", {"command": f"python3 {_ROOT}/scripts/run.py --repo x"}),
           _result(1, "a" * 5000),
           _call(2, "Read", {"file_path": f"{_ROOT}/SKILL.md"}), _result(2, "b" * 900),
           _call(3, "Bash", {"command": f"ls {_ROOT}/references/"}), _result(3, "c" * 50),
           _call(4, "Read", {"file_path": "/x/ci-secure/references/security-patterns.md"}),
           _result(4, "d" * 70)]
    r = reading.summarize(evs, "ci-speedup")
    assert (r["reference_bytes"], r["reference_files"], r["catalog_opened"]) == (0, [], False)


def test_a_glob_expands_against_the_reference_names_and_splits_the_result():
    evs = [_call(1, "Bash", {"command": f"grep -n OPT81 {_ROOT}/references/*.md"}),
           _result(1, "z" * 90)]
    r = reading.summarize(evs, "ci-speedup", ["a.md", "b.md", "optimization-patterns.md", "c.tsv"])
    assert sorted(f["path"] for f in r["reference_files"]) == [
        "a.md", "b.md", "optimization-patterns.md"]
    assert {f["bytes"] for f in r["reference_files"]} == {30} and r["catalog_opened"]


_NAMES = ["adversarial-review-rubric.md", "gap-fill.md", "optimization-patterns.md"]


def test_a_cd_into_references_then_a_bare_file_name_is_counted():
    evs = [_call(1, "Bash", {"command": f"cd {_ROOT}/references; grep -n -i wheel optimization-patterns.md"}),
           _result(1, "4391:### OPT86 — CI Compiles a Dependency That Ships Prebuilt")]
    r = reading.summarize(evs, "ci-speedup", _NAMES)
    assert [f["path"] for f in r["reference_files"]] == ["optimization-patterns.md"]
    assert r["catalog_entries_read"] == ["OPT86"]


def test_a_directory_wide_grep_attributes_each_line_to_its_file():
    body = ("optimization-patterns.md:12:" + "a" * 40 + "\n"
            "gap-fill.md:3:" + "b" * 10)
    evs = [_call(1, "Bash", {"command": f"grep -rn cache {_ROOT}/references/"}), _result(1, body)]
    by = {f["path"]: f["bytes"] for f in reading.summarize(evs, "ci-speedup", _NAMES)["reference_files"]}
    assert by == {"optimization-patterns.md": 69, "gap-fill.md": 25}


def test_another_install_of_the_same_skill_is_not_the_one_under_test():
    """Observed: a session that skipped the Skill tool found the maintainer's
    global install under ~/.agents and read its (stale) catalog."""
    evs = [_call(1, "Bash", {"command": "cd ~/.agents/skills/ci-speedup/references; grep -n x optimization-patterns.md"}),
           _result(1, "y" * 300),
           _call(2, "Read", {"file_path": "/Users/u/.agents/skills/ci-speedup/references/gap-fill.md"}),
           _result(2, "z" * 30),
           _call(3, "Read", {"file_path": f"/private{_ROOT}/references/gap-fill.md"}), _result(3, "w" * 7)]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert [(f["path"], f["bytes"]) for f in r["reference_files"]] == [("gap-fill.md", 7)]


def test_a_heading_seen_twice_in_one_result_is_one_entry():
    evs = [_call(1, "Bash", {"command": f"grep -n -A2 'OPT81 —' {_ROOT}/references/optimization-patterns.md"}),
           _result(1, _ENTRY + "\n" + _ENTRY)]
    assert reading.summarize(evs, "ci-speedup")["catalog_entries_read"] == ["OPT81"]


def test_the_word_references_in_prose_is_not_a_read():
    evs = [_call(1, "Bash", {"command": f"grep -n 'references' {_ROOT}/SKILL.md"}), _result(1, "x" * 400)]
    assert reading.summarize(evs, "ci-speedup", _NAMES)["reference_bytes"] == 0


def test_repeat_reads_sum_per_file_and_a_heading_counts_once():
    evs = []
    for i in (1, 2):
        evs += [_call(i, "Read", {"file_path": f"{_ROOT}/references/optimization-patterns.md",
                                  "offset": 3585, "limit": 60}), _result(i, _ENTRY)]
    [f] = reading.summarize(evs, "ci-speedup")["reference_files"]
    assert f["calls"] == 2 and f["bytes"] == 2 * len(_ENTRY.encode())
    assert reading.summarize(evs, "ci-speedup")["catalog_entries_read"] == ["OPT81"]


@pytest.mark.parametrize("check,ok", [
    ({"type": "reading", "field": "reference_bytes", "min": 1}, True),
    ({"type": "reading", "field": "reference_bytes", "max": 10}, False),
    ({"type": "reading", "field": "catalog_entries_read", "pattern": "^OPT81$"}, True),
    ({"type": "reading", "field": "catalog_entries_read", "pattern": "^OPT9$"}, False),
    ({"type": "reading", "field": "catalog_opened", "min": 0, "max": 0}, False),
])
def test_reading_checks(check, ok):
    evs = [_call(1, "Read", {"file_path": f"{_ROOT}/references/optimization-patterns.md"}),
           _result(1, _ENTRY)]
    assert reading.check(check, reading.summarize(evs, "ci-speedup"))[0] is ok


def test_the_runner_records_reading_and_prints_the_figure(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("runner_r", _SCRIPTS / "run_skill_evals.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, "_RESULTS", tmp_path / "results")
    monkeypatch.setattr(runner, "_LOGS", tmp_path / "logs")
    monkeypatch.setattr(runner, "scaffold", lambda *a: {})
    monkeypatch.setattr(runner.shutil, "which", lambda *a, **k: "/usr/bin/true")
    evs = [{"type": "system", "subtype": "init"},
           _call(1, "Skill", {"skill": "ci-secure:ci-secure"}), _result(1, "ok"),
           # A path relative to the session's mount (where the runner puts it is a temp dir).
           _call(2, "Bash", {"command": "cat ci-speedup/references/gap-fill.md"}), _result(2, "g" * 2048),
           {"type": "result", "subtype": "success", "duration_ms": 1000, "usage": {}}]
    fake = subprocess.CompletedProcess([], 0, stdout="\n".join(map(json.dumps, evs)), stderr="")
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: fake)
    assert runner.main(["--skill", "ci-speedup", "--id", "6"]) == 0
    [res] = json.loads(next((tmp_path / "results").iterdir()).read_text())
    assert res["reading"]["reference_bytes"] == 2048 and not res["reading"]["catalog_opened"]
    assert "refs 2.0 KB in 1 files, catalog not opened; session" in capsys.readouterr().out


def test_session_tokens_come_from_the_cumulative_model_usage_across_resumed_turns():
    """With background subagents the stream carries one result per resumed turn;
    the last one's `modelUsage` is the whole session, subagents included."""
    usage = {"m": {"inputTokens": 5, "cacheReadInputTokens": 900, "cacheCreationInputTokens": 95,
                   "outputTokens": 40}}
    evs = [{"type": "result", "usage": {"input_tokens": 1}, "modelUsage": usage},
           {"type": "result", "usage": {"input_tokens": 2}, "modelUsage": usage,
            "subagent_stats": {"spawned": 2}}]
    assert reading.session_totals(evs, 12.34) == {
        "wall_s": 12.3, "input_tokens": 1000, "output_tokens": 40,
        "result_events": 2, "subagents": 2}

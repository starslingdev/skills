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
           _result(1, "4391:### OPT86 — CI Compiles a Dependency That Ships Prebuilt\n4392-\n4393-Wheel built")]
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


def test_a_heading_without_its_body_is_not_an_entry_read():
    """A heading index (`grep '^### OPT'`) or a bare heading grep lists entries; it reads none."""
    index = "3585:### OPT81 — Faster Runner\n3643:### OPT82 — Other"
    evs = [_call(1, "Bash", {"command": f"grep -n '^### OPT' {_ROOT}/references/optimization-patterns.md"}),
           _result(1, index)]
    assert reading.summarize(evs, "ci-speedup")["catalog_entries_read"] == []


@pytest.mark.parametrize("command", [
    "cd {root} && cat ./references/gap-fill.md",                      # greptile: `./` read as an owner
    "D={root}; sed -n 1,9p $D/references/gap-fill.md",                # shell variable
    'D={root}; cat "${{D}}"/references/gap-fill.md',
    "cd {root} && grep -rn cache references/",                        # relative bare directory
])
def test_relative_and_variable_paths_into_the_mount_are_counted(command):
    evs = [_call(1, "Bash", {"command": command.format(root=_ROOT)}), _result(1, "gap-fill.md:1:" + "g" * 50)]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert [f["path"] for f in r["reference_files"]] == ["gap-fill.md"]


@pytest.mark.parametrize("command", [
    "cd ~/.claude/skills/ci-speedup && sed -n 1,9p references/optimization-patterns.md",
    "curl -s https://raw.githubusercontent.com/o/r/main/skills/ci-speedup/references/optimization-patterns.md | head",
    "gh api repos/o/r/contents/skills/ci-speedup/references/optimization-patterns.md | head",
])
def test_another_copy_reached_by_cd_or_over_the_network_is_not_the_mount(command):
    evs = [_call(1, "Bash", {"command": command}), _result(1, _ENTRY)]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert (r["reference_bytes"], r["catalog_opened"]) == (0, False)


def test_a_failed_or_empty_read_is_not_a_read():
    evs = [_call(1, "Read", {"file_path": f"{_ROOT}/references/optimization-patterns.md"}),
           {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                     "is_error": True, "content": "File too large"}]}},
           _call(2, "Bash", {"command": f"grep -n nomatch {_ROOT}/references/gap-fill.md"}), _result(2, "")]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert (r["reference_bytes"], r["reference_files"], r["catalog_opened"]) == (0, [], False)


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


def test_a_reading_check_on_an_unknown_field_is_a_harness_error_not_a_pass():
    with pytest.raises(ValueError, match="reference_byte"):
        reading.check({"type": "reading", "field": "reference_byte", "min": 0}, {"reference_bytes": 5})


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


_CAT_LINES = (f"{_ROOT}/references/optimization-patterns.md:3585:### OPT81 — Faster Runner\n"
              f"{_ROOT}/references/optimization-patterns.md-3586-unavailable to fork pull requests\n")


@pytest.mark.parametrize("name,inp", [
    ("Bash", {"command": f"grep -rn -A1 OPT81 {_ROOT}"}),
    ("Bash", {"command": f"cd {_ROOT} && grep -rn -A1 OPT81 ."}),
    ("Grep", {"pattern": "OPT81", "path": _ROOT, "output_mode": "content", "-A": 1}),
])
def test_a_search_from_the_skill_root_is_charged_by_each_output_lines_file(name, inp):
    body = _CAT_LINES
    if inp.get("command", "").startswith("cd "):
        body = body.replace(f"{_ROOT}/", "./")
    evs = [_call(1, name, inp), _result(1, body + f"{_ROOT}/SKILL.md:9:OPT81 is listed\n")]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert [f["path"] for f in r["reference_files"]] == ["optimization-patterns.md"]
    assert r["reference_bytes"] == len(body.encode()) and r["catalog_entries_read"] == ["OPT81"]


@pytest.mark.parametrize("name,inp,out", [
    ("Grep", {"pattern": "OPT81", "path": f"{_ROOT}/references"},
     f"Found 1 file\n{_ROOT}/references/optimization-patterns.md"),
    ("Grep", {"pattern": "OPT81", "path": _ROOT, "output_mode": "count"},
     f"{_ROOT}/references/optimization-patterns.md:4"),
    ("Bash", {"command": f"grep -rl OPT81 {_ROOT}/references/"}, f"{_ROOT}/references/optimization-patterns.md"),
    ("Bash", {"command": f"grep -c OPT {_ROOT}/references/optimization-patterns.md"}, "91"),
    ("Bash", {"command": f"ls {_ROOT}/references/ | head"}, "gap-fill.md\noptimization-patterns.md"),
    ("Bash", {"command": f"find {_ROOT}/references -name '*.md'"},
     f"{_ROOT}/references/gap-fill.md\n{_ROOT}/references/optimization-patterns.md"),
    ("Bash", {"command": f"wc -c {_ROOT}/references/gap-fill.md"}, f"4000 {_ROOT}/references/gap-fill.md"),
])
def test_a_listing_of_file_names_reads_nothing(name, inp, out):
    evs = [_call(1, name, inp), _result(1, out)]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert (r["reference_bytes"], r["catalog_opened"]) == (0, False)
    assert r.get("unattributed_calls") == 0


@pytest.mark.parametrize("command", [
    'cat "${{CLAUDE_PLUGIN_ROOT}}/references/gap-fill.md"',
    "sed -n '1,50p' \"$D/references/gap-fill.md\"",
    "nl -ba {root}/references/gap-fill.md",
    "perl -ne 'print if 1..9' {root}/references/gap-fill.md",
    "python3 -c \"print(open('{root}/references/gap-fill.md').read())\"",
    "ls {root}/references/gap-fill.md | xargs cat",
    "find {root}/references -name gap-fill.md -exec cat {{}} \;",
])
def test_variables_and_other_readers_reach_the_mount(command):
    evs = [_call(1, "Bash", {"command": command.format(root=_ROOT)}), _result(1, "g" * 50)]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert [(f["path"], f["bytes"]) for f in r["reference_files"]] == [("gap-fill.md", 50)]


def test_home_is_another_install_not_the_mount():
    evs = [_call(1, "Bash", {"command": "cat $HOME/.agents/skills/ci-speedup/references/gap-fill.md"}),
           _result(1, "g" * 50)]
    assert reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)["reference_bytes"] == 0


def test_a_command_the_tracker_cannot_read_is_surfaced_not_dropped():
    evs = [_call(1, "Bash", {"command": f"cp {_ROOT}/references/gap-fill.md /tmp/g && bat /tmp/g"}),
           _result(1, "g" * 50)]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert (r["reference_bytes"], r.get("unattributed_calls")) == (0, 1)
    assert "1 unattributed" in reading.figure(r)


def test_a_grep_tool_content_search_of_one_reference_file_is_counted():
    evs = [_call(1, "Grep", {"pattern": "wheel", "path": f"{_ROOT}/references/gap-fill.md",
                             "output_mode": "content"}), _result(1, "12:" + "w" * 20)]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert [(f["path"], f["bytes"]) for f in r["reference_files"]] == [("gap-fill.md", 23)]


def test_a_heading_repeated_in_one_result_counts_once():
    body = _ENTRY + "\n" + _ENTRY
    evs = [_call(1, "Read", {"file_path": f"{_ROOT}/references/optimization-patterns.md"}), _result(1, body)]
    assert reading.summarize(evs, "ci-speedup")["catalog_entries_read"] == ["OPT81"]


def test_a_heading_followed_only_by_line_numbers_is_not_an_entry_read():
    body = "3585:### OPT81 — Faster Runner\n3586:\n3587-\n"
    evs = [_call(1, "Bash", {"command": f"grep -n -A2 OPT81 {_ROOT}/references/optimization-patterns.md"}),
           _result(1, body)]
    assert reading.summarize(evs, "ci-speedup")["catalog_entries_read"] == []


def test_unprefixed_lines_of_a_directory_grep_go_only_to_files_it_printed():
    body = "gap-fill.md:3:" + "b" * 10 + "\n--\n" + "c" * 20
    evs = [_call(1, "Bash", {"command": f"grep -rn x {_ROOT}/references/"}), _result(1, body)]
    by = {f["path"]: f["bytes"] for f in reading.summarize(evs, "ci-speedup", _NAMES)["reference_files"]}
    assert by == {"gap-fill.md": len(body.encode()) + 1}


@pytest.mark.parametrize("ev", [
    {"type": "system", "message": "a string"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "9", "name": "Read", "input": "x"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "9", "content": 7}]}},
    {"type": "result", "modelUsage": {"m": None}, "subagent_stats": "n/a"},
    "not an event",
])
def test_malformed_events_never_stop_the_run(ev):
    r = reading.summarize([_call(9, "Read", {"file_path": f"{_ROOT}/references/gap-fill.md"}), ev],
                          "ci-speedup", _NAMES, root=_ROOT)
    assert r["reference_bytes"] == 0


def test_a_reading_check_with_an_unknown_key_is_a_harness_error_not_a_pass():
    with pytest.raises(ValueError, match="mx"):
        reading.check({"type": "reading", "name": "n", "field": "reference_bytes", "mx": 0},
                      {"reference_bytes": 5})


def test_a_skill_without_the_catalog_prints_no_catalog_figure():
    evs = [_call(1, "Read", {"file_path": f"{_ROOT}/references/gap-fill.md"}), _result(1, "g" * 10)]
    r = reading.summarize(evs, "ci-secure", ["gap-fill.md"], root=_ROOT.replace("ci-speedup", "ci-secure"))
    assert r["catalog_opened"] is None and "catalog" not in reading.figure(r)


def _runner(tmp_path, monkeypatch, evs):
    spec = importlib.util.spec_from_file_location("runner_r2", _SCRIPTS / "run_skill_evals.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, "_RESULTS", tmp_path / "results")
    monkeypatch.setattr(runner, "_LOGS", tmp_path / "logs")
    monkeypatch.setattr(runner, "scaffold", lambda *a: {})
    monkeypatch.setattr(runner.shutil, "which", lambda *a, **k: "/usr/bin/true")
    fake = subprocess.CompletedProcess([], 0, stdout="\n".join(map(json.dumps, evs)), stderr="")
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: fake)
    return runner


def test_the_runner_fails_entry_11_when_the_session_never_reads_the_catalog(tmp_path, monkeypatch):
    evs = [{"type": "system", "subtype": "init"},
           _call(1, "Skill", {"skill": "ci-speedup"}), _result(1, "ok"),
           _call(2, "Bash", {"command": "python3 ci-speedup/scripts/run.py --repo o/r"}), _result(2, "done"),
           _call(3, "Bash", {"command": "cat ci-speedup/references/gap-fill.md"}), _result(3, "g" * 64),
           {"type": "assistant", "message": {"content": [{"type": "text", "text":
            "MISSING-FROM-CARD: fork pull requests can't use the faster runner label"}]}},
           {"type": "result", "subtype": "success", "duration_ms": 1000, "usage": {}}]
    runner = _runner(tmp_path, monkeypatch, evs)
    assert runner.main(["--skill", "ci-speedup", "--id", "11"]) == 1
    [res] = json.loads(next((tmp_path / "results").iterdir()).read_text())
    checks = {c["name"]: c["passed"] for c in res["checks"]}
    assert res["passed"] is False and checks["read-the-opt81-catalog-entry"] is False
    assert checks["read-some-reference-material"] and checks["names-the-catalog-only-fork-caveat"]


def test_a_malformed_reading_check_is_a_harness_error(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, [])
    with pytest.raises(runner.HarnessError, match="mx"):
        runner.apply_checks([{"name": "n", "type": "reading", "field": "reference_bytes", "mx": 0}],
                            {"reading": {"reference_bytes": 5}})


def test_a_bash_read_whose_command_exits_non_zero_still_counts_its_output():
    """Seen in a baseline run: `awk` printed the OPT81 entry, a later `cat` in the
    same command failed, and the result came back flagged as an error with the
    entry in it. The agent saw the entry; the `Exit code` line is not the file."""
    out = "Exit code 1\n" + _ENTRY.replace("3585\t", "").replace("3586\t", "").replace("3587\t", "")
    evs = [_call(1, "Bash", {"command": f"awk '/OPT81/,/OPT82/' {_ROOT}/references/optimization-patterns.md; "
                                        "cat missing.yml"}),
           {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                     "is_error": True, "content": out}]}}]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert r["reference_bytes"] == len(out.encode()) - len("Exit code 1\n")
    assert r["catalog_entries_read"] == ["OPT81"]


@pytest.mark.parametrize("command", [
    "cat $(ls {root}/references/gap-fill.md)",
    "ls {root}/references/gap-fill.md | xargs -n 1 cat",
    "for f in {root}/references/gap-fill.md; do cat $f; done",
    "{{ cat {root}/references/gap-fill.md; }}",
    "timeout 5 cat {root}/references/gap-fill.md",
    "if [ -f {root}/references/gap-fill.md ]; then cat {root}/references/gap-fill.md; fi",
    r"grep -n OPT81\|OPT82 {root}/references/gap-fill.md",
])
def test_a_reader_wrapped_in_shell_syntax_still_counts(command):
    """Each shape counted before simple-command splitting and must still count."""
    evs = [_call(1, "Bash", {"command": command.format(root=_ROOT)}), _result(1, "g" * 50)]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert [(f["path"], f["bytes"]) for f in r["reference_files"]] == [("gap-fill.md", 50)]


def _errored(i: int, body: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": True, "content": body}]}}


@pytest.mark.parametrize("command,body", [
    ("rg -n OPT81 {cat}", "Exit code 127\n(eval):1: command not found: rg"),
    ("sed -n '3585,3643p' {cat} | hed -5", "Exit code 127\n(eval):1: command not found: hed"),
    ("sed -n '3585,3643' {cat}", "Exit code 1\nsed: 1: \"3585,3643\": command expected"),
    ("cat {cat}", "Command timed out after 2m 0.0s"),
])
def test_a_shell_read_that_failed_before_printing_counts_nothing(command, body):
    cat = f"{_ROOT}/references/optimization-patterns.md"
    evs = [_call(1, "Bash", {"command": command.format(cat=cat)}), _errored(1, body)]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert (r["reference_bytes"], r["catalog_opened"]) == (0, False)


def test_a_failing_shell_command_counts_what_it_printed_but_not_its_error_line():
    cat = f"{_ROOT}/references/optimization-patterns.md"
    evs = [_call(1, "Bash", {"command": f"awk 'NR>=3585' {cat}; cat {_ROOT}/references/nope.md"}),
           _errored(1, f"Exit code 1\n{_ENTRY}\ncat: {_ROOT}/references/nope.md: No such file or directory")]
    r = reading.summarize(evs, "ci-speedup", _NAMES, root=_ROOT)
    assert [(f["path"], f["bytes"]) for f in r["reference_files"]] == [
        ("optimization-patterns.md", len(_ENTRY.encode()))]


# --- the split catalog: references/patterns/opt<N>-*.md + INDEX.md ---------------

_SPLIT = ["gap-fill.md", "optimization-patterns.md", "patterns/INDEX.md",
          "patterns/opt8-cache-key-granularity-mismatch.md",
          "patterns/opt81-same-job-faster-on-another-runner.md",
          "patterns/opt86-use-prebuilt-dependency-packages.md"]
_OPT81_FILE = "     1\t---\n     2\tpattern: OPT81\n     3\t---\n     4\t\n     5\t# OPT81 — The Same Job\n     6\t\n     7\tbody"


def test_a_read_of_one_pattern_file_is_the_catalog_and_names_its_entry():
    evs = [_call(1, "Read", {"file_path": f"{_ROOT}/references/patterns/opt81-same-job-faster-on-another-runner.md"}),
           _result(1, _OPT81_FILE)]
    r = reading.summarize(evs, "ci-speedup", _SPLIT, root=_ROOT)
    assert r["catalog_opened"] and r["catalog_entries_read"] == ["OPT81"]
    assert [f["path"] for f in r["reference_files"]] == ["patterns/opt81-same-job-faster-on-another-runner.md"]


def test_the_index_alone_opens_the_catalog_but_reads_no_entry():
    body = "- [OPT86 — CI Compiles a Dependency](opt86-use-prebuilt-dependency-packages.md) · HIGH · data-driven · x"
    evs = [_call(1, "Bash", {"command": f"cat {_ROOT}/references/patterns/INDEX.md"}), _result(1, body)]
    r = reading.summarize(evs, "ci-speedup", _SPLIT, root=_ROOT)
    assert r["catalog_opened"] and r["catalog_entries_read"] == []
    assert [f["path"] for f in r["reference_files"]] == ["patterns/INDEX.md"]


def test_a_recursive_grep_over_the_patterns_dir_is_charged_per_file():
    body = (f"{_ROOT}/references/patterns/opt86-use-prebuilt-dependency-packages.md:5:# OPT86 — CI Compiles\n"
            f"{_ROOT}/references/patterns/opt86-use-prebuilt-dependency-packages.md:7:wheel body\n"
            f"{_ROOT}/references/patterns/INDEX.md:40:- [OPT86 — x](y) wheel\n")
    evs = [_call(1, "Bash", {"command": f"grep -rn wheel {_ROOT}/references/patterns/"}), _result(1, body)]
    r = reading.summarize(evs, "ci-speedup", _SPLIT, root=_ROOT)
    paths = sorted(f["path"] for f in r["reference_files"])
    assert paths == ["patterns/INDEX.md", "patterns/opt86-use-prebuilt-dependency-packages.md"]
    assert r["catalog_entries_read"] == ["OPT86"]


def test_a_glob_after_cd_into_the_patterns_dir_names_only_that_file():
    evs = [_call(1, "Bash", {"command": f"cd {_ROOT}/references/patterns && cat opt81-*.md"}),
           _result(1, _OPT81_FILE)]
    r = reading.summarize(evs, "ci-speedup", _SPLIT, root=_ROOT)
    assert [f["path"] for f in r["reference_files"]] == ["patterns/opt81-same-job-faster-on-another-runner.md"]
    assert r["catalog_entries_read"] == ["OPT81"]


def test_a_path_glob_matches_one_pattern_and_not_its_numeric_neighbour():
    evs = [_call(1, "Bash", {"command": f"cat {_ROOT}/references/patterns/opt8-*.md"}),
           _result(1, "# OPT8 — Cache Key Granularity Mismatch\n\nbody")]
    r = reading.summarize(evs, "ci-speedup", _SPLIT, root=_ROOT)
    assert [f["path"] for f in r["reference_files"]] == ["patterns/opt8-cache-key-granularity-mismatch.md"]


def test_a_non_catalog_reference_file_still_does_not_open_the_catalog():
    evs = [_call(1, "Read", {"file_path": f"{_ROOT}/references/gap-fill.md"}), _result(1, "x" * 40)]
    r = reading.summarize(evs, "ci-speedup", _SPLIT, root=_ROOT)
    assert r["catalog_opened"] is False

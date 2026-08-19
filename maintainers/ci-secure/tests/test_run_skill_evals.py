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
import re
import subprocess
import sys
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


def test_no_file_the_plugin_copy_mounts_can_satisfy_a_grader(tmp_path):
    """The name-based exclusion list is not the invariant -- this is.

    `evals/` and `tests/` were excluded because someone noticed they held the
    answer key. Nothing checked the REST of the tree, and the rest of the tree
    grew: `CHANGELOG.md` ships inside the skill, `--plugin-dir` mounts it, and
    it quotes banner strings verbatim while describing the graders that assert
    on them. One `Grep` of the plugin directory then greens a grader with no
    scan behind it -- a false PASS, which is the direction that never gets
    investigated.

    The shipped vacuity rule (`skills/ci-secure/tests/test_evals_cases.py`)
    cannot cover this: it lives in the installed skill and has no idea what
    this maintainer-side harness chooses to mount. So the check belongs here,
    stated as the property rather than as a list of names, and it fails the day
    any new file at the skill root quotes a grader's expected string.
    """
    import yaml

    plugin = harness._plugin_dir(tmp_path)
    mounted = {p: p.read_text(encoding="utf-8", errors="replace")
               for p in sorted(plugin.rglob("*")) if p.is_file()}

    offenders = []
    for case in sorted((harness._SKILL / "evals").glob("*/case.yaml")):
        for g in yaml.safe_load(case.read_text(encoding="utf-8"))["graders"]:
            if g.get("type") != "regex" or g.get("match") == "not_contains":
                continue
            for path, text in mounted.items():
                hit = re.search(g["pattern"], text)
                if hit:
                    offenders.append(
                        f"{case.parent.name}/{g['name']} matches "
                        f"{hit.group(0)!r} in {path.relative_to(plugin)}")
    assert not offenders, (
        "a file the harness mounts satisfies a grader without a scan behind "
        "it -- the agent can read the answer instead of computing it:\n  "
        + "\n  ".join(offenders))


# --- the graded corpus is everything the run produced, not only what it printed
#
# ci-secure writes its report to a file and prints a summary. The graders that
# pin a finding to its real file and line are anchored on `report.py`'s evidence
# bullets — `vulnerable.yml:8 — jobs: bench` — which is deliberate: a model can
# paraphrase its summary a dozen ways, but that bullet is byte-identical every
# run. Grading only the transcript threw that surface away, so two cases failed
# on runs where the skill had behaved correctly and written the right report.


def test_a_files_targeted_regex_reads_what_the_session_wrote(tmp_path):
    """The report is output. A grader anchored on the renderer must be able to
    see it, or it grades whether the agent happened to `cat` the file."""
    report = tmp_path / "ci-secure-report-abc.md"
    report.write_text("- `.github/workflows/vulnerable.yml:8` — jobs: `bench`\n",
                      encoding="utf-8")
    corpus = harness._files_corpus([tmp_path], {})
    graders = [{"type": "regex", "name": "finding-lands-on-the-vulnerable-workflow",
                "target": "files",
                "pattern": r"vulnerable\.yml:(?:8|14)\b[^\n]{0,20}jobs",
                "match": "contains"}]
    scored, _ = harness._grade(graders, "", [], files=corpus)
    assert scored[0][1] is True, (
        "the run rendered the evidence bullet; the grader must see the file")


def test_an_uncollectable_corpus_is_not_scored_as_a_behavioural_failure(tmp_path):
    """A `files` grader with no report to read is a collection failure, exit 2.

    This used to be named for corpus separation, but it never tested it: the
    empty-corpus guard fires before any corpus lookup, so it exits 2 whatever
    `corpora["files"]` would have returned. Separation is now pinned by the test
    below, and this one keeps the guard it actually exercises."""
    corpus = harness._files_corpus([tmp_path], {})
    graders = [{"type": "regex", "name": "report-only", "target": "files",
                "pattern": r"ONLY-IN-THE-TRANSCRIPT", "match": "contains"}]
    with pytest.raises(SystemExit) as exc:
        harness._grade(graders, "ONLY-IN-THE-TRANSCRIPT", [], files=corpus)
    assert exc.value.code == 2, (
        "an empty files corpus under a files-targeted grader is a corpus that "
        "could not be collected — not a behavioural failure")


def test_a_files_targeted_regex_does_not_read_the_transcript(tmp_path):
    """The two corpora stay separate in BOTH directions.

    The trace→files direction was covered; this one was not, and the asymmetry
    was invisible because the test that claimed it only ever reached the
    empty-corpus guard. A `files` grader that silently fell back to — or
    concatenated in — the transcript would pass on the agent's prose, which is
    the model-phrasing dependence these anchors exist to remove.

    So the corpus here is real (it holds a report, clearing the collection
    guard) and simply does not contain the needle, while the transcript does.
    The grader must come back False."""
    report = tmp_path / "ci-secure-report-abc.md"
    report.write_text("a real report that never mentions the needle\n",
                      encoding="utf-8")
    corpus = harness._files_corpus([tmp_path], {})
    graders = [{"type": "regex", "name": "report-only", "target": "files",
                "pattern": r"ONLY-IN-THE-TRANSCRIPT", "match": "contains"}]
    scored, _ = harness._grade(graders, "ONLY-IN-THE-TRANSCRIPT", [], files=corpus)
    assert scored[0][1] is False, (
        "the needle is only in the transcript; a `files` grader that saw it "
        "read the wrong corpus")


def test_a_trace_targeted_regex_does_not_read_the_files(tmp_path):
    """And the other direction: the transcript grader must not be satisfiable
    by a file the agent wrote but never showed."""
    (tmp_path / "report.md").write_text("ONLY-IN-THE-REPORT\n", encoding="utf-8")
    corpus = harness._files_corpus([tmp_path], {})
    graders = [{"type": "regex", "name": "trace-only", "target": "trace",
                "pattern": r"ONLY-IN-THE-REPORT", "match": "contains"}]
    scored, _ = harness._grade(graders, "", [], files=corpus)
    assert scored[0][1] is False


def test_a_corpus_without_the_report_is_a_collection_failure_not_a_red_case(tmp_path):
    """The empty-corpus guard was written for "the report never reached the
    harness", but it only fires when the corpus is COMPLETELY empty -- and a
    session leaves scratch files, a fix branch, a stray note. One of those keeps
    the corpus non-empty while the report is still missing, and then every
    report-anchored grader fails as though the scan had misbehaved.

    That is the exact wrong-diagnosis this PR exists to close, one level in: if
    `claude` stops honouring TMPDIR, or a subprocess resets it, or SKILL.md
    drifts to a literal path, the harness must say it could not collect the
    evidence rather than report a behaviour change.
    """
    (tmp_path / "scratch-note.txt").write_text("thinking out loud\n", encoding="utf-8")
    corpus = harness._files_corpus([tmp_path], {})
    assert corpus, "precondition: the corpus is non-empty but holds no report"
    graders = [{"type": "regex", "name": "finding-lands-on-the-vulnerable-workflow",
                "target": "files",
                "pattern": r"vulnerable\.yml:(?:8|14)\b[^\n]{0,20}jobs",
                "match": "contains"}]
    with pytest.raises(SystemExit) as exc:
        harness._grade(graders, "", [], files=corpus)
    assert exc.value.code == 2, (
        "no report in the corpus means the graders anchored on it had no "
        "evidence to read -- a harness failure, not a skill regression")


def test_a_count_grader_counts_in_the_corpus_its_target_names(tmp_path):
    """`count:` is the third match mode, and it read the transcript no matter
    what `target` said -- the same wrong-corpus bug `contains` was just fixed
    for, surviving in the branch beside it.

    Silent, and wrong in both directions: a `files` grader counting `2` scores
    the occurrences in the agent's prose, so a report that rendered exactly two
    passes only if the session also happened to say it twice. No case uses
    `count:` today, which is why it went unnoticed; the next author to write one
    against `files` would get a verdict computed from the other corpus.
    """
    report = tmp_path / "ci-secure-report-abc.md"
    report.write_text("HIT\nHIT\n", encoding="utf-8")
    corpus = harness._files_corpus([tmp_path], {})
    graders = [{"type": "regex", "name": "two-in-the-report", "target": "files",
                "pattern": "HIT", "match": "count:2"}]
    scored, _ = harness._grade(graders, "HIT", [], files=corpus)
    assert scored[0][1] is True, (
        "the report holds exactly two hits and the grader targets `files`; "
        f"counting the one in the transcript instead gave {scored[0][2]}")


def test_an_unknown_grader_target_is_not_graded_against_the_trace():
    """`target` used to be ignored outright, so `files` silently meant `trace`.
    Whatever the next unimplemented target is, it must stop the harness rather
    than quietly grade the wrong corpus."""
    graders = [{"type": "regex", "name": "bogus", "target": "sandbox",
                "pattern": "x", "match": "contains"}]
    with pytest.raises(SystemExit) as exc:
        harness._grade(graders, "x", [], files="x")
    assert exc.value.code == 2


def test_the_files_corpus_holds_only_what_the_session_created(tmp_path):
    """The fixture the agent was HANDED is not evidence of what it did. The
    scaffold writes intentionally vulnerable workflow YAML naming the very files
    the graders assert on, so grading the whole sandbox would hand several
    graders their answer the way `evals/` and `tests/` would."""
    fixture = tmp_path / "handed.yml"
    fixture.write_text("on: pull_request_target\n", encoding="utf-8")
    edited = tmp_path / "edited.md"
    edited.write_text("before\n", encoding="utf-8")
    before = harness._snapshot([tmp_path])

    (tmp_path / "written-by-the-run.md").write_text("NEW\n", encoding="utf-8")
    edited.write_text("AFTER\n", encoding="utf-8")

    corpus = harness._files_corpus([tmp_path], before)
    assert "NEW" in corpus, "a file the session created belongs in the corpus"
    assert "AFTER" in corpus, "a file the session rewrote belongs in the corpus"
    assert "pull_request_target" not in corpus, (
        "the untouched fixture is what the agent was handed, not what it produced")


def test_the_files_corpus_names_each_file_relative_to_its_root(tmp_path):
    """The corpus carries path headers so a grader can anchor on the file a
    finding landed in. They must be the paths the run used, not the harness's
    temp directory, which changes every run."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "ci-secure-report-abc.md").write_text("x\n", encoding="utf-8")
    corpus = harness._files_corpus([tmp_path], {})
    assert "sub/ci-secure-report-abc.md" in corpus
    assert str(tmp_path) not in corpus


def test_the_agent_writes_its_report_where_the_harness_can_read_it(tmp_path):
    """SKILL.md line 194 renders to `${TMPDIR:-/tmp}/ci-secure-report-<slug>.md`.
    Pointing TMPDIR at a directory the harness owns is what makes collecting the
    report a contract rather than a guess at where it landed."""
    env = harness._env(tmp_path)
    assert env["TMPDIR"] == str(tmp_path)


def test_skill_md_still_renders_its_report_under_tmpdir():
    """The other half of that contract, and the half that can rot silently.

    Collecting the report works only because SKILL.md keys its output paths to
    `${TMPDIR:-/tmp}`. If that ever becomes a fixed `/tmp` or a path under the
    repository, the harness would go on collecting an empty directory — and an
    empty corpus fails every `files` grader as though the skill had regressed.
    """
    skill = (harness._SKILL / "SKILL.md").read_text(encoding="utf-8")
    for artifact in ("ci-secure-findings-", "ci-secure-report-"):
        assert f'"${{TMPDIR:-/tmp}}/{artifact}' in skill, (
            f"SKILL.md no longer writes {artifact}* under TMPDIR; the eval "
            "harness collects the session's output from there")


def test_the_report_the_engine_renders_satisfies_the_graders_anchored_on_it(tmp_path):
    """End to end over everything but the model.

    Scaffold a real fixture, run the real engine the way SKILL.md tells the
    agent to — output under TMPDIR — and check that the corpus the harness
    would build satisfies the case's own `files` grader. This is what failed in
    CI: the assertion was true of the run's output the whole time, and the
    harness was reading somewhere else.
    """
    import subprocess as sp
    import yaml

    case = harness._EVALS / "pwn-request"
    sandbox, artifacts = tmp_path / "repo", tmp_path / "out"
    sandbox.mkdir()
    artifacts.mkdir()
    harness._scaffold(case, sandbox)
    before = harness._snapshot([sandbox, artifacts])

    scripts = harness._SKILL / "scripts"
    findings = artifacts / "ci-secure-findings-deadbeef.json"
    r = sp.run([sys.executable, str(scripts / "scan.py"), "--root", str(sandbox),
                "--gh-impostor", "off"], capture_output=True, text=True,
               env=harness._env(artifacts))
    assert r.returncode == 0, r.stderr[-400:]
    findings.write_text(r.stdout, encoding="utf-8")
    r = sp.run([sys.executable, str(scripts / "report.py"), "--in", str(findings),
                "--out", str(artifacts / "ci-secure-report-deadbeef.md")],
               capture_output=True, text=True, env=harness._env(artifacts))
    assert r.returncode == 0, r.stderr[-400:]

    corpus = harness._files_corpus([sandbox, artifacts], before)
    spec = yaml.safe_load((case / "case.yaml").read_text(encoding="utf-8"))
    graders = [g for g in spec["graders"]
               if g.get("type") == "regex" and g.get("target") == "files"]
    assert graders, "pwn-request no longer has a files-targeted grader to check"
    scored, _ = harness._grade(graders, "", [], files=corpus)
    for name, passed, detail in scored:
        assert passed, f"{name} does not match the report the engine renders ({detail})"

    # The fixture FILES stay out. Their content still appears, quoted back by
    # the engine's own evidence lines — that is the scanner's output, which is
    # exactly what the corpus is for, and it is why the exclusion is checked on
    # the corpus's file headers rather than on the text.
    assert "=== .github/workflows/vulnerable.yml ===" not in corpus, (
        "the fixture the agent was handed must not enter the graded corpus")
    assert "=== ci-secure-report-deadbeef.md ===" in corpus

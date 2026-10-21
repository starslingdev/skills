"""Unit tests for ci-speedup's behavioural eval harness
(`maintainers/ci-speedup/scripts/run_skill_evals.py`).

The harness is a port of ci-secure's; these mirror
`maintainers/ci-secure/tests/test_run_skill_evals.py` for the guards it shares
(a session that never started is exit 2, never a graded failure; the corpora
stay apart; the fixture the agent was handed is not evidence) and add the ones
specific to ci-speedup: the plugin copy keeps `tests/verify_report.py` and
nothing else under `tests/`, the session is pointed at the offline replay
corpus, GitHub auth reaches the gh gate, and a report rendered outside the
harness's roots is still collected. No agent, no network, no tokens.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[1] / "scripts" / "run_skill_evals.py"


def _load():
    spec = importlib.util.spec_from_file_location("ci_speedup_run_skill_evals", _HARNESS)
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


# --- a session that never happened is exit 2 -------------------------------

def test_a_nonzero_claude_exit_is_a_harness_failure(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: _completed(
        returncode=1, stderr="cannot be used with root/sudo privileges\n"))
    with pytest.raises(SystemExit) as exc:
        harness._run_agent("prompt", tmp_path, tmp_path, 60, 900)
    assert exc.value.code == 2
    assert "root/sudo" in capsys.readouterr().err


def test_an_empty_stream_from_a_zero_exit_is_a_harness_failure(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: _completed())
    with pytest.raises(SystemExit) as exc:
        harness._run_agent("prompt", tmp_path, tmp_path, 60, 900)
    assert exc.value.code == 2
    assert "no session" in capsys.readouterr().err.lower()


def test_a_session_that_called_no_tool_is_a_harness_failure(monkeypatch, capsys, tmp_path):
    stream = ('{"type":"system","subtype":"init","session_id":"s"}\n'
              '{"type":"result","subtype":"success","is_error":false}\n')
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: _completed(
        stdout=stream, stderr="Invalid API key"))
    with pytest.raises(SystemExit) as exc:
        harness._run_agent("prompt", tmp_path, tmp_path, 60, 900)
    assert exc.value.code == 2
    assert "Invalid API key" in capsys.readouterr().err


def test_a_turn_cap_hit_is_not_a_graded_run(monkeypatch, tmp_path):
    stream = _GOOD_STREAM + '{"type":"result","subtype":"error_max_turns"}\n'
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: _completed(stdout=stream))
    with pytest.raises(SystemExit) as exc:
        harness._run_agent("prompt", tmp_path, tmp_path, 60, 900)
    assert exc.value.code == 2


def test_a_real_stream_is_returned_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(harness.subprocess, "run",
                        lambda *a, **k: _completed(stdout=_GOOD_STREAM))
    assert harness._run_agent("prompt", tmp_path, tmp_path, 60, 900) == _GOOD_STREAM


def test_zero_runs_is_refused_rather_than_reported_as_a_pass():
    with pytest.raises(SystemExit) as exc:
        harness.main(["--runs", "0"])
    assert exc.value.code == 2


def test_an_unexpected_harness_error_exits_2_not_1(monkeypatch, tmp_path):
    monkeypatch.setattr(harness.sys, "argv", ["run_skill_evals.py"])
    monkeypatch.setattr(harness.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(harness, "_cases", lambda only: [tmp_path / "boom"])
    with pytest.raises(SystemExit) as exc:
        harness.cli()
    assert exc.value.code == 2


def test_a_grader_type_the_harness_cannot_score_is_not_a_pass():
    with pytest.raises(SystemExit) as exc:
        harness._grade([{"type": "tool_order", "name": "x"}], "", [])
    assert exc.value.code == 2


def test_an_unknown_grader_target_stops_the_harness():
    with pytest.raises(SystemExit) as exc:
        harness._grade([{"type": "regex", "name": "x", "target": "sandbox",
                         "pattern": "x"}], "x", [], files="x")
    assert exc.value.code == 2


# --- the session's command line and environment ----------------------------

def test_the_session_mounts_every_plugin_and_loads_no_user_settings(monkeypatch, tmp_path):
    """Without `--setting-sources project,local`, a maintainer laptop offers
    the session the user-installed ci-speedup beside the plugin copy under
    test, and a pass can come from the wrong one."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return _completed(stdout=_GOOD_STREAM)

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    a, b = tmp_path / "a", tmp_path / "b"
    harness._run_agent("p", tmp_path, [a, b], 60, 900, tmp_path, tmp_path / "fx")
    cmd = seen["cmd"]
    dirs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--plugin-dir"]
    assert dirs == [str(a), str(b)]
    i = cmd.index("--setting-sources")
    assert cmd[i + 1] == "project,local"
    assert seen["env"]["CI_SPEEDUP_GH_FIXTURES"] == str(tmp_path / "fx")


def test_the_session_env_is_scrubbed_but_reaches_the_gh_gate(monkeypatch, tmp_path):
    """SKILL.md phase 1 stops unless `gh auth status` passes, so GitHub auth
    must reach the session. Nothing that selects a git repository, and never
    the record switch (a recording session would overwrite its own corpus)."""
    monkeypatch.setenv("GIT_DIR", "/somewhere/real/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/somewhere/real")
    monkeypatch.setenv("CI_SPEEDUP_GH_RECORD", "/somewhere")
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setenv("SOME_SECRET", "nope")
    env = harness._env(tmp_path, tmp_path / "fx")
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert "CI_SPEEDUP_GH_RECORD" not in env and "SOME_SECRET" not in env
    assert env["GH_TOKEN"] == "t0ken"
    assert env["TMPDIR"] == str(tmp_path)
    assert env["CI_SPEEDUP_GH_FIXTURES"] == str(tmp_path / "fx")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_the_scaffold_gets_no_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GIT_DIR", "/somewhere/real/.git")
    env = harness._scaffold_env()
    assert not {"GH_TOKEN", "ANTHROPIC_API_KEY", "GIT_DIR"} & set(env)


def test_the_replay_corpus_covers_the_whole_session_budget(tmp_path):
    """`_monthly_volume`'s endpoint embeds "now minus 30 days" to the second,
    and an agent reaches `run.py` minutes after the corpus is written. A band
    sized for the e2e's seconds would turn the audit into a partial-coverage
    report."""
    fixtures = harness._replay_fixtures(tmp_path, span_s=400)
    helper = harness._load_corpus_helper()
    import datetime as dt
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=300)
    since = (later - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    name = helper.cr._fixture_name(
        f"repos/synthetic/repo/actions/workflows/1001/runs?per_page=1&created=>={since}",
        "json")
    assert (fixtures / name).is_file(), "a call five minutes in would miss its fixture"
    committed = {p.name for p in helper._FIXTURES_DIR.iterdir() if p.is_file()}
    assert committed <= {p.name for p in fixtures.iterdir()}


# --- the plugin copy -------------------------------------------------------

def test_the_plugin_copy_keeps_what_the_contract_runs(tmp_path):
    """SKILL.md phase 5 runs `tests/verify_report.py`, so `tests/` cannot be
    withheld wholesale. It must still run from the copy: it imports only
    `scripts/untrusted_wrap.py`."""
    plugin = harness._plugin_dir(tmp_path)
    assert (plugin / "SKILL.md").is_file()
    assert (plugin / "scripts" / "run.py").is_file()
    assert (plugin / "scripts" / "blocking_path.py").is_file()
    assert sorted(p.name for p in (plugin / "tests").iterdir()) == ["verify_report.py"]
    r = subprocess.run(["python3", str(plugin / "tests" / "verify_report.py"), "--help"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_the_plugin_copy_withholds_the_answer_key(tmp_path):
    """`evals/` holds every grader's string; `tests/test_*.py` assert the
    renderer's exact output; `tests/fixtures/` is the replay corpus itself."""
    plugin = harness._plugin_dir(tmp_path)
    assert not (plugin / "evals").exists()
    assert not (plugin / "CHANGELOG.md").exists()
    assert not (plugin / "tests" / "fixtures").exists()
    assert not list((plugin / "tests").glob("test_*.py"))
    assert not list(plugin.rglob("__pycache__"))


def test_no_file_the_plugin_copy_mounts_can_satisfy_a_grader(tmp_path):
    """The property the exclusion list exists for, stated as a property, so
    the next file added to the skill cannot quietly reopen the hole."""
    import yaml

    roots = [harness._plugin_dir(tmp_path / "a")]
    for case in sorted((harness._SKILL / "evals").glob("*/case.yaml")):
        spec = yaml.safe_load(case.read_text(encoding="utf-8"))
        roots += harness._plugin_dirs(case.parent, spec, tmp_path / case.parent.name)[1:]
    mounted = {p: p.read_text(encoding="utf-8", errors="replace")
               for root in roots for p in sorted(root.rglob("*")) if p.is_file()}
    offenders = []
    for case in sorted((harness._SKILL / "evals").glob("*/case.yaml")):
        for g in yaml.safe_load(case.read_text(encoding="utf-8"))["graders"]:
            if g.get("type") != "regex" or g.get("match") == "not_contains":
                continue
            if g.get("target") == "last_message":
                continue  # the agent's own closing words, never a file it reads
            for path, text in mounted.items():
                hit = re.search(g["pattern"], text)
                if hit:
                    offenders.append(f"{case.parent.name}/{g['name']}: {hit.group(0)!r} "
                                     f"in {path}")
    assert not offenders, "\n".join(offenders)


def test_a_sibling_plugin_is_mounted_without_its_answer_key(tmp_path):
    case = harness._SKILL / "evals" / "security-ask-routes-to-ci-secure"
    spec = {"plugins": ["../..", "../../../ci-secure"]}
    dirs = harness._plugin_dirs(case, spec, tmp_path)
    assert [d.name for d in dirs] == ["ci-speedup", "ci-secure"]
    assert (dirs[1] / "SKILL.md").is_file() and (dirs[1] / "scripts").is_dir()
    for withheld in ("evals", "tests", "CHANGELOG.md"):
        assert not (dirs[1] / withheld).exists(), withheld


def test_a_plugins_entry_that_is_not_a_skill_stops_the_harness(tmp_path):
    case = harness._SKILL / "evals" / "full-run-synthetic"
    with pytest.raises(SystemExit) as exc:
        harness._plugin_dirs(case, {"plugins": ["../..", "../../../../maintainers"]},
                             tmp_path)
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        harness._plugin_dirs(case, {"plugins": ["../../../ci-secure"]}, tmp_path / "x")


def test_session_logs_never_land_inside_the_skill():
    """Anchored on the repo root: a relative path run from inside the skill
    would put a recorded session where the installer ships it."""
    assert harness._LOGS.is_absolute()
    assert harness._SKILL not in harness._LOGS.parents
    assert harness._LOGS == harness._REPO_ROOT / "skill-eval-logs"


# --- transcript decoding and the graded corpora -----------------------------

def test_regex_graders_see_decoded_lines_not_the_wire_format():
    stream = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "ci.yml (clean)\nline 11 elsewhere"}]}})
    graders = [{"type": "regex", "name": "same-line", "target": "trace",
                "pattern": r"ci\.yml[^\n]{0,30}?\b11\b"}]
    assert harness._grade(graders, stream, [])[0][0][1] is True
    assert harness._grade(graders, harness._transcript(stream), [])[0][0][1] is False


def test_the_transcript_excludes_the_prompt_and_keeps_tool_output():
    stream = "\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": [{"type": "text", "text": "PROMPT"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "PROSE"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "\n✅ all checks passed"}]}]}},
    ])
    got = harness._transcript(stream)
    assert "PROMPT" not in got and "PROSE" in got
    assert re.search(r"\n✅ all checks passed", got)


def test_the_last_message_is_the_final_assistant_text():
    stream = "\n".join(json.dumps(e) for e in [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                       "input": {}}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "last"}]}},
    ])
    assert harness._last_message(stream) == "last"


def _report(dirpath: Path, body: str) -> Path:
    p = dirpath / "ci-speedup-findings-report.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_a_files_grader_reads_the_report_and_not_the_transcript(tmp_path):
    _report(tmp_path, "a report without the needle\n")
    corpus = harness._files_corpus([tmp_path], {})
    g = [{"type": "regex", "name": "x", "target": "files", "pattern": "NEEDLE"}]
    assert harness._grade(g, "NEEDLE", [], files=corpus)[0][0][1] is False
    _report(tmp_path, "NEEDLE\n")
    corpus = harness._files_corpus([tmp_path], {})
    assert harness._grade(g, "", [], files=corpus)[0][0][1] is True


def test_a_corpus_without_the_report_is_a_collection_failure(tmp_path):
    (tmp_path / "findings.json").write_text("{}", encoding="utf-8")
    corpus = harness._files_corpus([tmp_path], {})
    assert corpus
    with pytest.raises(SystemExit) as exc:
        harness._grade([{"type": "regex", "name": "x", "target": "files",
                         "pattern": "x"}], "", [], files=corpus)
    assert exc.value.code == 2


def test_the_files_corpus_holds_only_what_the_session_created(tmp_path):
    (tmp_path / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    before = harness._snapshot([tmp_path])
    _report(tmp_path, "NEW\n")
    corpus = harness._files_corpus([tmp_path], before)
    assert "NEW" in corpus and "name: CI" not in corpus
    assert str(tmp_path) not in corpus


# --- a report rendered outside the harness's roots --------------------------

def _bash(command: str) -> tuple[str, str]:
    return ("Bash", json.dumps({"command": command}))


def test_a_report_the_render_command_named_is_collected(tmp_path):
    """SKILL.md leaves the scratch path to the agent; a first real session
    rendered under the system temp dir rather than the harness's TMPDIR.
    The render command names the report, so the harness follows it."""
    roots = [tmp_path / "sandbox", tmp_path / "out"]
    for r in roots:
        r.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    started = time.time() - 5
    report = _report(elsewhere, "the report\n")
    tools = [_bash(f"python3 /p/scripts/blocking_path.py --in {elsewhere}/findings.json "
                   f"--out {report} \\\n  --log ci={elsewhere}/x.log")]
    found = harness._named_reports(tools, roots, started)
    assert found == [report]
    corpus = harness._files_corpus(roots, {}, found)
    assert harness._REPORT_HEADER.search(corpus)
    assert "the report" in corpus


def test_a_stale_report_at_a_named_path_is_not_this_sessions(tmp_path):
    report = _report(tmp_path, "old\n")
    old = time.time() - 3600
    os.utime(report, (old, old))
    assert harness._named_reports([_bash(f"cat {report}")], [], time.time() - 60) == []


def test_named_reports_skip_symlinks_roots_and_other_tools(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inside = _report(root, "x\n")
    real = _report(tmp_path, "y\n")
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    (link_dir / "ci-speedup-findings-report.md").symlink_to(real)
    since = time.time() - 60
    assert harness._named_reports([_bash(f"cat {inside}")], [root], since) == []
    assert harness._named_reports(
        [_bash(f"cat {link_dir}/ci-speedup-findings-report.md")], [], since) == []
    assert harness._named_reports([("Read", json.dumps({"file_path": str(real)}))],
                                  [], since) == []


# --- the gh shim -------------------------------------------------------------
#
# The replay seam covers every GitHub read the skill's scripts make; the shim
# covers the reads the AGENT makes with `gh` itself during phase 1, so the CLI
# and the scripts see the same repository. It must never answer the gate.

@pytest.fixture()
def shim_env(tmp_path):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "repos_synthetic_repo.json").write_text(
        json.dumps({"full_name": "synthetic/repo", "default_branch": "main",
                    "visibility": "public", "private": False}), encoding="utf-8")
    real = tmp_path / "real-gh"
    real.write_text('#!/bin/sh\necho "REAL $*"\n', encoding="utf-8")
    real.chmod(0o755)
    shim_dir = harness._gh_shim(tmp_path / "bin")
    env = {"PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
           "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
           "CI_SPEEDUP_GH_FIXTURES": str(fixtures),
           "CI_SPEEDUP_EVAL_REAL_GH": str(real),
           "CI_SPEEDUP_EVAL_REPLAYED_REPO": "synthetic/repo",
           "CI_SPEEDUP_EVAL_SCRIPTS": str(harness._SKILL / "scripts")}
    repo = tmp_path / "checkout"
    repo.mkdir()
    git = {**env, "GIT_DIR": str(repo / ".git")}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=git)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/synthetic/repo.git"],
                   cwd=str(repo), check=True, env=git)
    return env, repo


def _gh(env, cwd, *args):
    return subprocess.run(["gh", *args], cwd=str(cwd), env=env, capture_output=True,
                          text=True, timeout=60)


def test_the_shim_never_answers_the_auth_gate(shim_env):
    env, repo = shim_env
    r = _gh(env, repo, "auth", "status")
    assert r.stdout.strip() == "REAL auth status", (
        "`gh auth status` is SKILL.md's gate; it must reach the real gh")


def test_the_shim_resolves_the_replayed_checkout(shim_env):
    env, repo = shim_env
    r = _gh(env, repo, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    assert (r.returncode, r.stdout.strip()) == (0, "synthetic/repo")
    r = _gh(env, repo, "repo", "view", "synthetic/repo", "--json", "defaultBranchRef")
    assert json.loads(r.stdout) == {"defaultBranchRef": {"name": "main"}}


def test_the_shim_serves_corpus_endpoints_and_404s_the_rest(shim_env):
    env, repo = shim_env
    r = _gh(env, repo, "api", "repos/synthetic/repo", "--jq", ".default_branch")
    assert (r.returncode, r.stdout.strip()) == (0, "main")
    r = _gh(env, repo, "api", "repos/synthetic/repo/actions/runs?per_page=1")
    assert r.returncode == 1 and "HTTP 404" in r.stderr, (
        "an endpoint the corpus does not hold is a miss, as the replay client treats it")


def test_the_shim_delegates_every_other_repository_and_command(shim_env, tmp_path):
    env, repo = shim_env
    assert _gh(env, repo, "repo", "view", "cli/cli").stdout.startswith("REAL repo view cli/cli")
    assert _gh(env, repo, "api", "repos/cli/cli").stdout.startswith("REAL api")
    assert _gh(env, repo, "api", "-X", "POST",
               "repos/synthetic/repo/issues").stdout.startswith("REAL api")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _gh(env, elsewhere, "repo", "view").stdout.startswith("REAL repo view")


def test_the_shim_is_inert_outside_an_eval(shim_env):
    env, repo = shim_env
    env = {k: v for k, v in env.items() if k != "CI_SPEEDUP_GH_FIXTURES"}
    assert _gh(env, repo, "api", "repos/synthetic/repo").stdout.startswith("REAL api")

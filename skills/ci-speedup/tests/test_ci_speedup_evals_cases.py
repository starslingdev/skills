"""Static validation of ci-speedup's behavioural eval cases under ``evals/``.

A port of ``skills/ci-secure/tests/test_evals_cases.py``; read that module's
docstring for the reasoning behind each rule. The cases themselves cost a real
agent session each, so they run on the maintainer harness
(``maintainers/ci-speedup/scripts/run_skill_evals.py``), never under pytest.
Everything checkable WITHOUT a session is checked here: the schema, that the
scaffolds and the builder they call exist and refuse an unsafe directory, and
the two properties that make a grader worth having:

THE VACUITY RULE (``test_no_trace_or_files_regex_matches_the_skills_own_text``).
A ``trace`` or ``files`` regex that matches text the skill ships passes because
the agent READ the skill, not because it RAN it (and a ``not_contains`` one can
never pass). The corpus is everything the harness's plugin copy mounts: the
prose, the reference files, every script, and ``tests/verify_report.py``, which
the contract runs and which therefore ships in the copy.

THE ENGINE ANCHOR (``test_every_files_grader_holds_on_the_real_offline_render``).
The graders of the full-run case are evaluated against what the real pipeline
renders from the replay corpus today, offline, the same way the session will
produce it. A grader that does not hold on the deterministic output would fail
every good session; one that holds there and also on the skill's own text is
vacuous. Both are caught here, before anyone spends a session on them.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_SKILL = Path(__file__).resolve().parents[1]
_EVALS = _SKILL / "evals"
_SCRIPTS = _SKILL / "scripts"
_BUILDER = _SKILL / "tests" / "fixtures" / "synthetic_corpus.py"
_FULL_RUN = _EVALS / "full-run-synthetic"

_TOP_KEYS = {
    "schema_version", "name", "description", "tags", "plugins",
    "context", "execution", "runs", "graders", "expected_outcome",
}
_CONTEXT_KEYS = {"scaffold_script", "history_file", "add_dirs"}
_EXECUTION_KEYS = {
    "prompt", "max_turns", "timeout_seconds", "model", "allowed_tools",
    "artifact_publish", "growthbook_overrides", "append_system_prompt", "env",
}
_COMMON_GRADER_KEYS = {"type", "name", "weight", "arm"}
_GRADER_KEYS = {
    "regex": _COMMON_GRADER_KEYS | {"target", "pattern", "flags", "match"},
    "tool_order": _COMMON_GRADER_KEYS | {"before", "after"},
    "tool_used": _COMMON_GRADER_KEYS | {"tool", "input_match", "min", "max"},
    "file_exists": _COMMON_GRADER_KEYS | {"path", "exists"},
    "llm": _COMMON_GRADER_KEYS | {"criteria", "focus"},
    "baseline": _COMMON_GRADER_KEYS | {"baseline_file", "criteria"},
}
_TARGET_WORDS = {"trace", "last_message", "files"}
_JS_REGEXP_FLAGS = set("dgimsuvy")
_ENV_KEY = re.compile(r"^EVAL_[A-Z0-9_]*$")
_MATCH_COUNT = re.compile(r"^count:\d+$")


def _case_dirs() -> list[Path]:
    return sorted(p.parent for p in _EVALS.glob("*/case.yaml"))


def _load(case_dir: Path) -> dict:
    return yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))


def _all_cases() -> list[tuple[Path, dict]]:
    return [(d, _load(d)) for d in _case_dirs()]


def _graders(case: dict) -> list[dict]:
    return list(case.get("graders") or [])


def test_the_suite_is_actually_discovered_and_non_empty() -> None:
    """Positive control: every parametrized test below would pass vacuously
    over an empty or moved directory."""
    names = {d.name for d in _case_dirs()}
    assert {"full-run-synthetic", "security-ask-routes-to-ci-secure"} <= names, names
    for d in _case_dirs():
        assert "evals" in d.parts, f"{d} is not under a path segment named 'evals'"


def test_no_case_files_outside_a_case_directory() -> None:
    """Any ``case.yaml`` or ``prompt.md`` below ``evals/`` is picked up as a
    case; one dropped anywhere but ``evals/<case>/`` would run with real spend
    and graders nobody reviewed."""
    strays = sorted(
        p.relative_to(_SKILL).as_posix() for p in _EVALS.rglob("*")
        if p.is_file() and p.name in {"case.yaml", "prompt.md"}
        and p.parent.parent != _EVALS)
    assert not strays, strays


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_case_yaml_parses_and_uses_only_known_keys(case_dir: Path) -> None:
    case = _load(case_dir)
    assert isinstance(case, dict), "case.yaml must be a YAML mapping"
    unknown = sorted(set(case) - _TOP_KEYS)
    assert not unknown, f"unknown top-level key(s): {unknown}"
    assert case.get("schema_version") == "1.1"
    assert case.get("name") == case_dir.name, "name must match the case directory"
    context = case.get("context") or {}
    assert not sorted(set(context) - _CONTEXT_KEYS)
    execution = case.get("execution") or {}
    assert not sorted(set(execution) - _EXECUTION_KEYS)
    assert execution.get("prompt"), "execution.prompt is required"
    assert 0 < execution.get("max_turns", 10) <= 200
    assert 0 < execution.get("timeout_seconds", 300) <= 3600
    assert 0 < case.get("runs", 3) <= 50
    for key in (execution.get("env") or {}):
        assert _ENV_KEY.match(key), key


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_graders_are_well_formed(case_dir: Path) -> None:
    graders = _graders(_load(case_dir))
    assert graders, "at least one grader is required"
    seen: set[str] = set()
    for grader in graders:
        gtype = grader.get("type")
        assert gtype in _GRADER_KEYS, gtype
        name = grader.get("name")
        assert name and name not in seen, f"missing or duplicate grader name {name!r}"
        seen.add(name)
        unknown = sorted(set(grader) - _GRADER_KEYS[gtype])
        assert not unknown, f"grader {name!r} has key(s) the runner rejects: {unknown}"
        if "arm" in grader:
            assert grader["arm"] in {"with-only", "both"}
        for field in ("target", "focus"):
            if field in grader:
                assert grader[field] in _TARGET_WORDS, (name, grader[field])
        if gtype == "regex":
            re.compile(grader["pattern"])
            assert not set(grader.get("flags", "")) - _JS_REGEXP_FLAGS
            match = grader.get("match", "contains")
            assert match in {"contains", "not_contains"} or _MATCH_COUNT.match(match)
            # Mechanical graders only, and an implicit target would exempt the
            # grader from the vacuity rule below.
            assert "target" in grader, f"regex grader {name!r} declares no target"
            assert not re.match(r"^[0-9]", grader["pattern"]), (
                f"{name!r} opens with a bare digit and matches inside larger numbers")
        if gtype == "tool_used":
            assert grader.get("tool")
            if "input_match" in grader:
                re.compile(grader["input_match"])
            low, high = grader.get("min", 1), grader.get("max")
            assert low >= 0 and (high is None or high >= low), name
            if high == 0:
                assert grader.get("min") == 0, (
                    f"{name!r}: max: 0 without min: 0 is unsatisfiable")
            if grader["tool"] == "Skill":
                assert grader.get("arm") == "both", (
                    f"{name!r}: a Skill grader without `arm: both` is silently unscored")
        assert gtype not in {"llm", "baseline"}, (
            f"{name!r}: this suite is mechanical-only; the harness reports llm "
            "graders unscored, so one here would look like coverage and be none")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_cases_declare_the_tools_their_graders_require(case_dir: Path) -> None:
    case = _load(case_dir)
    declared = set((case.get("execution") or {}).get("allowed_tools") or [])
    grantable = {"Read", "Glob", "Grep", "NotebookRead", "Skill", "AskUserQuestion",
                 "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop",
                 "TaskOutput", "Agent", "TodoWrite", "Bash", "Write", "Edit",
                 "WebFetch", "WebSearch"}
    assert not sorted(t for t in declared if t.split("(")[0] not in grantable)
    for grader in _graders(case):
        if grader.get("type") == "tool_used" and grader.get("max") != 0:
            assert grader["tool"] in declared, (
                f"{grader['name']!r} needs {grader['tool']}, not in allowed_tools")


def _agent_readable_sources() -> list[Path]:
    """Everything the harness's plugin copy mounts that an agent can read: the
    prose, the reference files, every script, and the self-check script the
    contract runs. ``evals/*/case.yaml`` is out for ci-secure's reason (every
    pattern is a literal in its own file); the fatal half of that is covered by
    ``test_negative_regexes_do_not_match_the_case_file_that_declares_them``."""
    roots = [
        _SKILL / "SKILL.md",
        _SKILL / "ARCHITECTURE.md",
        *sorted((_SKILL / "references").glob("*.md")),
        *sorted(_SCRIPTS.glob("*.py")),
        _SKILL / "tests" / "verify_report.py",
        *sorted(_EVALS.glob("*.md")),
        *sorted(_EVALS.glob("*.sh")),
        *sorted(_EVALS.glob("*.json")),
    ]
    return [p for p in roots if p.is_file()]


def _agent_readable_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _agent_readable_sources())


def test_the_agent_readable_corpus_spans_more_than_the_prose_files() -> None:
    """Positive control for the corpus: if it stops reaching the renderer, the
    self-check script or the eval docs, the vacuity rule narrows silently."""
    corpus = _agent_readable_text()
    for literal, why in (
        ("chain to finish", "scripts/blocking_path.py"),
        ('print("✅ all checks passed")', "tests/verify_report.py"),
        ("Behavioural evals for ci-speedup", "evals/README.md"),
    ):
        assert literal in corpus, f"the corpus no longer reaches {why}"


def test_no_trace_or_files_regex_matches_the_skills_own_text() -> None:
    prose = _agent_readable_text()
    offenders = [
        f"{d.name}/{g['name']}: {g['pattern']!r}"
        for d, case in _all_cases() for g in _graders(case)
        if g.get("type") == "regex" and g.get("target") in {"trace", "files"}
        and re.search(g["pattern"], prose)
    ]
    assert not offenders, (
        "these regexes match text the skill ships, so they grade the agent "
        "having READ the skill, not RUN it:\n  " + "\n  ".join(offenders))


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_negative_regexes_do_not_match_the_case_file_that_declares_them(
    case_dir: Path,
) -> None:
    text = (case_dir / "case.yaml").read_text(encoding="utf-8")
    for grader in _graders(_load(case_dir)):
        if grader.get("type") == "regex" and grader.get("match") == "not_contains":
            assert not re.search(grader["pattern"], text), grader["name"]


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_plugins_entries_resolve_to_this_skill_then_siblings(case_dir: Path) -> None:
    """The first entry is the skill under test (a bare skill folder is not
    auto-detected); any further entry is another installable skill the case
    mounts beside it, which the harness copies without its answer key."""
    plugins = _load(case_dir).get("plugins")
    assert plugins, "plugins: is required"
    assert (case_dir / plugins[0]).resolve() == _SKILL
    for entry in plugins[1:]:
        sibling = (case_dir / entry).resolve()
        assert sibling.parent == _SKILL.parent and (sibling / "SKILL.md").is_file(), (
            f"plugins entry {entry!r} is not a sibling skill")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_scaffold_and_builder_exist(case_dir: Path) -> None:
    script_name = (_load(case_dir).get("context") or {}).get("scaffold_script")
    assert script_name and (case_dir / script_name).is_file()
    body = (case_dir / script_name).read_text(encoding="utf-8")
    assert "_scaffold_common.sh" in body
    assert (_EVALS / "_scaffold_common.sh").is_file()
    assert _BUILDER.is_file(), "the synthetic repo builder the scaffold calls is gone"


def _run_scaffold(case_dir: Path, cwd: Path):
    """``bash <script>`` with the sandbox as cwd and the harness's minimal env."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "TMPDIR": str(cwd),
           "TERM": "dumb", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["bash", str(case_dir / "scaffold.sh")], cwd=str(cwd),
                          env=env, capture_output=True, text=True, timeout=120)


def _victim(root: Path) -> Path:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "name: real ci\non: push\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=str(root), check=True,
                   env={**os.environ, "GIT_DIR": str(root / ".git")})
    return root / ".github" / "workflows" / "ci.yml"


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_scaffold_refuses_to_run_over_an_existing_checkout(
    case_dir: Path, tmp_path: Path,
) -> None:
    """Run by hand from a real checkout, the scaffold would re-init it, write
    synthetic workflows over its own and commit them to the current branch."""
    live = _victim(tmp_path / "victim")
    done = _run_scaffold(case_dir, tmp_path / "victim")
    assert done.returncode != 0
    assert live.read_text(encoding="utf-8") == "name: real ci\non: push\n"
    count = subprocess.run(
        ["git", "rev-list", "--count", "--all"], cwd=str(tmp_path / "victim"),
        capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_DIR": str(tmp_path / "victim" / ".git")},
    ).stdout.strip()
    assert count == "0", f"the scaffold committed to a repository it did not create: {count}"


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_scaffold_refuses_when_it_cannot_read_the_directory(
    case_dir: Path, tmp_path: Path,
) -> None:
    """Unknown is not empty: an `ls` that errors must not read as a clean dir."""
    victim = tmp_path / "unreadable"
    live = _victim(victim)
    os.chmod(victim, 0o300)
    try:
        done = _run_scaffold(case_dir, victim)
    finally:
        os.chmod(victim, 0o700)
    assert done.returncode != 0, done.stdout + done.stderr
    assert live.read_text(encoding="utf-8") == "name: real ci\non: push\n"


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_scaffold_materialises_the_synthetic_checkout_in_an_empty_sandbox(
    case_dir: Path, tmp_path: Path,
) -> None:
    """The checkout the replay corpus expects: the workflows, one commit on
    `main`, and an `origin` naming `synthetic/repo` (collect_runs reads YAML off
    disk only from a verified clone of `--repo`)."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    done = _run_scaffold(case_dir, sandbox)
    assert done.returncode == 0, done.stdout + done.stderr
    names = sorted(p.name for p in (sandbox / ".github" / "workflows").glob("*.yml"))
    assert "ci.yml" in names and "chained.yml" in names and "style.yml" in names, names
    git_env = {**os.environ, "GIT_DIR": str(sandbox / ".git")}
    origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(sandbox),
                            capture_output=True, text=True, check=True, env=git_env)
    assert origin.stdout.strip() == "https://github.com/synthetic/repo.git"
    branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=str(sandbox),
                            capture_output=True, text=True, check=True, env=git_env)
    assert branch.stdout.strip() == "main"


# ---------------------------------------------------------------------------
# The engine anchor: the full-run case's graders against the real render.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def offline_render(tmp_path_factory) -> dict:
    """Run the pipeline the contract tells the agent to run, offline, on the
    corpus the session will see: scaffold the checkout, build the replay dir,
    `run.py --with-logs`, then the render command run.py PRINTS (rewritten
    only to point `scripts/` at this tree), then `verify_report.py`."""
    tmp = tmp_path_factory.mktemp("offline_render")
    sandbox = tmp / "sandbox"
    sandbox.mkdir()
    done = _run_scaffold(_FULL_RUN, sandbox)
    assert done.returncode == 0, done.stderr

    sys.path.insert(0, str(_BUILDER.parent))
    import synthetic_corpus  # noqa: E402
    fixtures = synthetic_corpus._replay_dir(tmp, span_s=120)

    env = {**os.environ, "CI_SPEEDUP_GH_FIXTURES": str(fixtures)}
    env.pop("CI_SPEEDUP_GH_RECORD", None)
    scratch = tmp / "scratch"
    findings = scratch / "findings.json"
    run = subprocess.run(
        [sys.executable, str(_SCRIPTS / "run.py"), "--root", str(sandbox),
         "--out", str(findings), "--repo", "synthetic/repo", "--with-logs"],
        capture_output=True, text=True, env=env, timeout=180)
    assert run.returncode == 0, run.stdout + run.stderr

    printed = run.stdout[run.stdout.index("python3 scripts/blocking_path.py"):]
    command = printed.split("\n\n")[0].replace("\\\n", " ")
    argv = shlex.split(command)
    argv[0:2] = [sys.executable, str(_SCRIPTS / "blocking_path.py")]
    render = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=180)
    assert render.returncode == 0, render.stderr
    report = scratch / "ci-speedup-findings-report.md"
    assert report.is_file(), "the printed render command no longer targets the report name"

    verify = subprocess.run(
        [sys.executable, str(_SKILL / "tests" / "verify_report.py"),
         "--report", str(report), "--findings", str(findings)],
        capture_output=True, text=True, env=env, timeout=300)
    assert verify.returncode == 0, verify.stdout[-3000:]
    return {
        "files": f"=== scratch/ci-speedup-findings-report.md ===\n"
                 f"{report.read_text(encoding='utf-8')}",
        "trace": run.stdout + "\n" + render.stdout + "\n" + verify.stdout,
        "sandbox": sandbox,
    }


def _full_run_regexes(target: str) -> list[dict]:
    return [g for g in _graders(_load(_FULL_RUN))
            if g.get("type") == "regex" and g.get("target") == target]


def test_every_files_grader_holds_on_the_real_offline_render(offline_render) -> None:
    """Each `files` grader scores PASS on the report the real pipeline renders
    from the replay corpus. A `contains` that misses here fails every good
    session; a `not_contains` that hits here does too."""
    corpus = offline_render["files"]
    wrong = []
    for g in _full_run_regexes("files"):
        found = bool(re.search(g["pattern"], corpus))
        if found != (g.get("match", "contains") != "not_contains"):
            wrong.append(f"{g['name']}: {g['pattern']!r} (found={found})")
    assert not wrong, "graders that would fail a correct session:\n  " + "\n  ".join(wrong)


def test_the_self_check_grader_matches_what_verify_report_prints(offline_render) -> None:
    trace = offline_render["trace"]
    for g in _full_run_regexes("trace"):
        found = bool(re.search(g["pattern"], trace))
        assert found == (g.get("match", "contains") != "not_contains"), g["name"]


def test_the_prompt_less_pole_grader_fires_on_a_stunted_pole(offline_render) -> None:
    """The `not_contains` above holding on a good report proves nothing unless
    it also FIRES on a bad one: drop one pole's hand-off prompt and it must."""
    corpus = offline_render["files"]
    heading = "#### 🤖 Prompt for your coding agent"
    start = corpus.index("Long pole 3: ")
    cut = corpus.index(heading, start)
    stunted = corpus[:cut] + "#### (removed)" + corpus[cut + len(heading):]
    g = next(g for g in _full_run_regexes("files")
             if g["name"] == "no-long-pole-lacks-its-agent-prompt")
    assert not re.search(g["pattern"], corpus)
    assert re.search(g["pattern"], stunted)


def test_every_case_with_a_report_grader_anchors_on_engine_only_text(
    offline_render,
) -> None:
    """At least one positive `files` grader must match the real render AND
    nothing the agent could reach without a working engine: the skill's text,
    the case file, and the synthetic checkout it was handed (raw and as `grep
    -n` prints it). Otherwise an agent that read the workflows and guessed
    scores the same as one whose pipeline ran."""
    reachable = [_agent_readable_text(),
                 (_FULL_RUN / "case.yaml").read_text(encoding="utf-8")]
    sandbox: Path = offline_render["sandbox"]
    for p in sorted(sandbox.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(sandbox).parts:
            try:
                body = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # bytecode the pipeline left behind, not text an agent reads
            rel = p.relative_to(sandbox).as_posix()
            reachable.append(body)
            reachable.extend(f"{rel}:{n}:{line}"
                             for n, line in enumerate(body.splitlines(), 1))
    corpus = "\n".join(reachable)
    anchors = [g["name"] for g in _full_run_regexes("files")
               if g.get("match", "contains") == "contains"
               and re.search(g["pattern"], offline_render["files"])
               and not re.search(g["pattern"], corpus)]
    assert anchors, "no files grader is satisfiable only by a completed render"


def test_the_full_run_targets_the_replayed_repo_by_name() -> None:
    """SKILL.md phase 1 re-confirms only an ambiguous target. The prompt names
    the corpus repository and the checkout it lives in, so the contract proceeds
    without a confirmation round-trip; the harness's gh shim answers
    `gh repo view` for that repository from the replay corpus."""
    prompt = _load(_FULL_RUN)["execution"]["prompt"]
    assert "synthetic/repo" in prompt
    assert "current directory" in prompt


def test_evals_json_points_at_the_runnable_cases() -> None:
    note = json.loads((_EVALS / "evals.json").read_text(encoding="utf-8"))["note"]
    assert "run_skill_evals.py" in note, (
        "evals.json's note must say which scenarios now run, and where")


# ---------------------------------------------------------------------------
# `tool_used` graders against the input the harness actually matches.
# ---------------------------------------------------------------------------
#
# The harness matches `input_match` against `json.dumps` of the tool input, so
# a shell `"` reaches the regex as `\"` and a newline as `\n`. Each row below is
# a Bash command a session could run, encoded exactly that way.

_ROUTING = _EVALS / "security-ask-routes-to-ci-secure"


def _grader(case_dir: Path, name: str) -> dict:
    return next(g for g in _graders(_load(case_dir)) if g["name"] == name)


def _bash_input(command: str) -> str:
    return json.dumps({"command": command})


@pytest.mark.parametrize("case_dir,name,command,hits", [
    # SKILL.md phase 1's gate.
    (_FULL_RUN, "gh-gate-checked", "gh auth status", True),
    (_FULL_RUN, "gh-gate-checked", "gh auth status --hostname github.com 2>&1", True),
    (_FULL_RUN, "gh-gate-checked", "gh repo view synthetic/repo", False),
    # The data pass, however its arguments are quoted or wrapped.
    (_FULL_RUN, "data-pass-ran-with-logs",
     "python3 scripts/run.py --root . --out f.json --repo synthetic/repo --with-logs", True),
    (_FULL_RUN, "data-pass-ran-with-logs",
     'python3 /p/ci-speedup/scripts/run.py --root "$PWD" --repo synthetic/repo --with-logs',
     True),
    (_FULL_RUN, "data-pass-ran-with-logs",
     'python3 scripts/run.py --out "/tmp/a b/findings.json" --with-logs', True),
    (_FULL_RUN, "data-pass-ran-with-logs",
     "python3 scripts/run.py --root . \\\n  --with-logs", True),
    (_FULL_RUN, "data-pass-ran-with-logs", "python3 scripts/run.py --root .", False),
    (_FULL_RUN, "data-pass-ran-with-logs",
     "python3 scripts/run.py --root .\npython3 other.py --with-logs", False),
    (_FULL_RUN, "data-pass-ran-with-logs",
     "python3 scripts/run.py --root . && echo --with-logs", False),
    # Nothing committed or pushed, whatever global options precede the verb.
    (_FULL_RUN, "nothing-was-committed", "git commit -m x", True),
    (_FULL_RUN, "nothing-was-committed", "git --no-pager commit -m x", True),
    (_FULL_RUN, "nothing-was-committed", "git -C /r commit -am x", True),
    (_FULL_RUN, "nothing-was-committed", 'git -C "/a b" commit -m x', True),
    (_FULL_RUN, "nothing-was-committed", "git -c a=b commit -m x", True),
    (_FULL_RUN, "nothing-was-committed", "git -c a=b --no-pager -C /r commit", True),
    (_FULL_RUN, "nothing-was-committed", "/usr/bin/git commit -m x", True),
    (_FULL_RUN, "nothing-was-committed", "git log --grep commit", False),
    (_FULL_RUN, "nothing-was-committed", "git show HEAD:commit.txt", False),
    (_FULL_RUN, "nothing-was-committed", "git status && echo commit", False),
    (_FULL_RUN, "nothing-was-committed", "git commit-tree HEAD^{tree}", False),
    (_FULL_RUN, "nothing-was-pushed", "git push origin HEAD", True),
    (_FULL_RUN, "nothing-was-pushed", "git -C /r --no-pager push", True),
    (_FULL_RUN, "nothing-was-pushed", "git --no-pager push --force", True),
    (_FULL_RUN, "nothing-was-pushed", "git log --grep push", False),
    (_FULL_RUN, "nothing-was-pushed", "git show HEAD:push.txt", False),
    # The routing case: ci-speedup's pipeline, never ci-secure's same-named scripts.
    (_ROUTING, "no-ci-speedup-pipeline-script-ran",
     "python3 /p/ci-speedup/scripts/collect_runs.py --repo o/r", True),
    (_ROUTING, "no-ci-speedup-pipeline-script-ran",
     "python3 /p/ci-speedup/scripts/run.py --root .", True),
    (_ROUTING, "no-ci-speedup-pipeline-script-ran",
     "python3 /p/ci-speedup/tests/verify_report.py --report r.md", True),
    (_ROUTING, "no-ci-speedup-pipeline-script-ran",
     "python3 /p/ci-secure/tests/verify_report.py --report r.md", False),
    (_ROUTING, "no-ci-speedup-pipeline-script-ran",
     "python3 /p/ci-secure/scripts/run.py --root .", False),
])
def test_tool_used_graders_on_the_json_encoded_bash_input(case_dir, name, command, hits):
    grader = _grader(case_dir, name)
    assert grader["type"] == "tool_used" and grader["tool"] == "Bash"
    encoded = _bash_input(command)
    found = bool(re.search(grader["input_match"], encoded))
    assert found == hits, f"{name}: {grader['input_match']!r} on {encoded!r} -> {found}"


def test_the_routing_case_report_grader_fires_on_a_real_report(offline_render) -> None:
    """`no-speed-report-in-the-transcript` holding on a session proves nothing
    unless it FIRES on the report ci-speedup's renderer actually writes: a
    renderer title change would otherwise make it vacuous without a sound."""
    g = _grader(_ROUTING, "no-speed-report-in-the-transcript")
    assert g["match"] == "not_contains"
    assert re.search(g["pattern"], offline_render["files"]), (
        f"{g['pattern']!r} no longer matches the rendered report's title")

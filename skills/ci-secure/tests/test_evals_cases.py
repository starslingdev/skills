"""Static validation of the runnable ``claude plugin eval`` suite under
``skills/ci-secure/evals/``.

WHY THIS FILE EXISTS. The eval runner is in early access and is not enabled on
every machine, so the suite cannot be executed as part of ``pytest``. Everything
here is the verification that IS available without running it: that each
``case.yaml`` parses, that every key is one the harness accepts, that every
grader is well formed against the schema the shipped CLI validates with, that
the fixtures and scaffolds the cases point at exist, and -- the one that is not
obvious -- that no grader is vacuous.

THE VACUITY RULE (``test_no_trace_regex_matches_the_skills_own_prose``). A
grader whose ``target`` is ``trace`` reads the whole session transcript, and the
transcript of a run WITH the skill loaded contains the text of ``SKILL.md`` and
of any reference file the agent opened. So a ``contains`` pattern that already
appears in the skill's own prose passes without the skill ever having done
anything, and a ``not_contains`` pattern that appears there can never pass at
all. Both directions collapse to one rule: the regex must not match the skill's
shipped text. It binds ``files``-targeted graders too, because ``report.py``
inlines catalog prose into the report it renders, so shipped text reaches that
corpus as well. This is not hypothetical -- natural choices for these
graders (``P14.10``, ``did NOT run``, ``Impostor-SHA check (P14.11): ran``) are
quotations from ``SKILL.md``, and each was replaced with a scanner- or
renderer-produced string after this test flagged it.

The corpus the rule reads is defined by ``_agent_readable_sources()`` and is
deliberately wider than the prose files -- ``scripts/*.py`` ships too, and a
``not_contains`` grader anchored on a literal in ``scan.py`` is the silent,
never-passing direction of the same trap.

A third face of the trap has its own test
(``test_negative_trace_regexes_do_not_match_grep_output_over_the_fixtures``) and
is the nastiest, because it fires on a run that did everything right: the
transcript carries the output of every TOOL the agent ran, so a trace-targeted
``not_contains`` shaped like ``<file>:<line>...<word>`` is matched by ``grep``'s
own ``<path>:<line>:<text>`` rendering of the fixture the agent was told to
audit. The more carefully a session inspects the file it is supposed to clear,
the likelier it fails.

Patterns are compiled with Python's ``re`` while the harness uses JavaScript's
``RegExp``. The suite deliberately stays inside the syntax both accept
(character classes, ``\\s \\w \\d \\b``, non-capturing groups, bounded
repetition), so this is a close enough approximation to be worth having; the
flag alphabet is checked against the JavaScript one regardless.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is the scanner's one dependency")

_SKILL = Path(__file__).resolve().parents[1]
_EVALS = _SKILL / "evals"

# Mirrors the schema the shipped CLI validates case.yaml against. Unknown
# top-level / context / execution keys are ignored by the harness for forward
# compatibility, but a typo there silently does nothing -- so they are errors
# here, where a human can still fix them.
_TOP_KEYS = {
    "schema_version", "name", "description", "tags", "plugins",
    "context", "execution", "runs", "graders", "expected_outcome",
}
_CONTEXT_KEYS = {"scaffold_script", "history_file", "add_dirs"}
_EXECUTION_KEYS = {
    "prompt", "max_turns", "timeout_seconds", "model", "allowed_tools",
    "artifact_publish", "growthbook_overrides", "append_system_prompt", "env",
}

# Unknown keys INSIDE a grader are a hard validation error in the harness, so
# these sets are the real contract and not a courtesy.
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
    """Positive control. Every other test in this file iterates the case list,
    so all of them would pass vacuously against an empty or moved directory."""
    dirs = _case_dirs()
    assert len(dirs) >= 5, (
        f"expected at least the five behavioral cases, found {len(dirs)}: "
        f"{[d.name for d in dirs]}")
    # `claude plugin eval` only discovers a case directory when some ancestor
    # path segment is literally named `evals`. That is what makes this layout
    # runnable at all, so it is worth pinning rather than assuming.
    for d in dirs:
        assert "evals" in d.parts, f"{d} is not under a path segment named 'evals'"


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_case_yaml_parses_and_uses_only_known_keys(case_dir: Path) -> None:
    case = _load(case_dir)
    assert isinstance(case, dict), "case.yaml must be a YAML mapping"

    unknown = sorted(set(case) - _TOP_KEYS)
    assert not unknown, f"unknown top-level key(s): {unknown}"

    version = case.get("schema_version")
    assert isinstance(version, str), "schema_version is required and must be a string"
    major = int(version.split(".")[0])
    assert major <= 1, (
        f"schema_version {version!r} declares major {major}; the shipped CLI "
        "supports up to 1.x and would refuse the case")

    assert case.get("name"), "name is required"

    context = case.get("context") or {}
    assert not sorted(set(context) - _CONTEXT_KEYS), (
        f"unknown context key(s): {sorted(set(context) - _CONTEXT_KEYS)}")

    execution = case.get("execution") or {}
    assert not sorted(set(execution) - _EXECUTION_KEYS), (
        f"unknown execution key(s): {sorted(set(execution) - _EXECUTION_KEYS)}")

    # Either a prompt or a history file to resume from; a case with neither is
    # rejected by the harness before it runs.
    assert execution.get("prompt") or context.get("history_file"), (
        "either execution.prompt or context.history_file is required")

    assert 0 < execution.get("max_turns", 10) <= 200, "max_turns must be 1..200"
    assert 0 < execution.get("timeout_seconds", 300) <= 3600, (
        "timeout_seconds must be 1..3600")
    assert 0 < case.get("runs", 3) <= 50, "runs must be 1..50"

    for key in (execution.get("env") or {}):
        assert _ENV_KEY.match(key), (
            f"execution.env key {key!r} does not match ^EVAL_[A-Z0-9_]*$ -- the "
            "harness fails the whole run on any other name")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_graders_are_well_formed(case_dir: Path) -> None:
    graders = _graders(_load(case_dir))
    assert graders, "at least one grader is required"

    seen: set[str] = set()
    for grader in graders:
        gtype = grader.get("type")
        assert gtype in _GRADER_KEYS, (
            f"grader type {gtype!r} is not one of {sorted(_GRADER_KEYS)}")

        name = grader.get("name")
        assert name, f"every grader in a case.yaml needs a name ({gtype})"
        assert name not in seen, f"duplicate grader name {name!r}"
        seen.add(name)

        unknown = sorted(set(grader) - _GRADER_KEYS[gtype])
        assert not unknown, (
            f"grader {name!r} ({gtype}) has key(s) the harness rejects "
            f"outright: {unknown}")

        assert grader.get("weight", 1) > 0, (
            f"grader {name!r}: weight must be positive (there is no weight 0 -- "
            "remove the grader or use `arm` instead)")
        if "arm" in grader:
            assert grader["arm"] in {"with-only", "both"}, (
                f"grader {name!r}: arm must be 'with-only' or 'both'")

        for field in ("target", "focus"):
            if field in grader:
                value = grader[field]
                if isinstance(value, dict):
                    assert value.get("source") == "file" and value.get("path"), (
                        f"grader {name!r}: {field} object must be "
                        "{source: file, path: ...}")
                else:
                    assert value in _TARGET_WORDS, (
                        f"grader {name!r}: {field} must be one of "
                        f"{sorted(_TARGET_WORDS)} or a file object")

        if gtype == "regex":
            assert "pattern" in grader, f"grader {name!r}: pattern is required"
            re.compile(grader["pattern"])  # raises on a malformed pattern
            bad = set(grader.get("flags", "")) - _JS_REGEXP_FLAGS
            assert not bad, (
                f"grader {name!r}: {sorted(bad)} are not JavaScript RegExp "
                "flags (d g i m s u v y); note that inline (?i) is not accepted")
            match = grader.get("match", "contains")
            assert match in {"contains", "not_contains"} or _MATCH_COUNT.match(match), (
                f"grader {name!r}: match must be contains | not_contains | count:N")

        if gtype == "tool_used":
            assert grader.get("tool"), f"grader {name!r}: tool is required"
            if "input_match" in grader:
                re.compile(grader["input_match"])
            low, high = grader.get("min", 1), grader.get("max")
            assert low >= 0, f"grader {name!r}: min must be non-negative"
            if high is not None:
                assert high >= low, (
                    f"grader {name!r}: max {high} is below min {low}, so the "
                    "grader can never pass")

        if gtype == "llm":
            assert (grader.get("criteria") or "").strip(), (
                f"grader {name!r}: an llm grader needs criteria")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_negative_tool_graders_pin_min_zero_as_well_as_max_zero(
    case_dir: Path,
) -> None:
    """``max: 0`` on its own can never pass: ``min`` stays at its default of 1,
    so the grader asserts "between 1 and 0 calls". Every "must not call this"
    grader has to say ``min: 0`` too, and this is the shape most likely to be
    got wrong when a new negative is added later."""
    for grader in _graders(_load(case_dir)):
        if grader.get("type") == "tool_used" and grader.get("max") == 0:
            assert grader.get("min") == 0, (
                f"grader {grader['name']!r}: max: 0 without min: 0 is "
                "unsatisfiable -- min defaults to 1")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_skill_tool_graders_opt_back_into_scoring(case_dir: Path) -> None:
    """A ``tool_used`` grader on the ``Skill`` tool with no explicit ``arm`` is
    auto-promoted to a "the plugin fired" indicator that is EXCLUDED from the
    score in both arms under ablation. Every one of ours is a real assertion
    ("the skill was the thing that answered"), so each must say ``arm: both``
    to stay scored. Without this the suite would look like it was checking
    routing while scoring nothing of the kind."""
    for grader in _graders(_load(case_dir)):
        if grader.get("type") == "tool_used" and grader.get("tool") == "Skill":
            assert grader.get("arm") == "both", (
                f"grader {grader['name']!r} targets the Skill tool without "
                "`arm: both`, so it would be silently unscored")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_cases_declare_the_tools_their_graders_require(case_dir: Path) -> None:
    """Effective tools are the case's ``allowed_tools`` intersected with the
    harness's read-only set, plus the operator's ``--allow-tools``. A grader
    that asserts a Bash call while the case never asked for Bash is a grader
    that cannot pass."""
    case = _load(case_dir)
    declared = set((case.get("execution") or {}).get("allowed_tools") or [])
    for grader in _graders(case):
        if grader.get("type") != "tool_used":
            continue
        # Negative graders assert a tool was NOT used; they hold whether or not
        # the tool was ever available.
        if grader.get("max") == 0:
            continue
        tool = grader["tool"]
        assert tool in declared, (
            f"grader {grader['name']!r} requires the {tool} tool, which is not "
            f"in this case's allowed_tools ({sorted(declared)})")


def _agent_readable_sources() -> list[Path]:
    """The shipped files a run of the skill realistically puts into the trace.

    Wider than ``SKILL.md`` on purpose. The ``skills`` CLI copies
    ``skills/ci-secure/`` recursively, so the engine sources sit on disk beside
    the ``scripts/run.py`` path ``SKILL.md`` directs the agent to resolve and
    execute, and one ``Grep`` over the skill directory pulls any of them into the
    transcript. Checking only the prose files is what let a ``not_contains``
    grader be written against ``ran: no sha-pinned actions found`` -- a literal
    in ``scripts/scan.py`` -- where it can never pass.

    Two shipped surfaces are deliberately OUT of the corpus, because including
    them would forbid the only anchors the suite has rather than improve them:

    * ``tests/`` -- the oracle tests assert the renderer's exact output, so every
      banner string is a literal there by construction. Nothing in ``SKILL.md``
      sends the agent to them and they are not part of any audit it performs.
    * ``evals/files/`` -- the fixture workflows ARE the input under audit. A
      grader that pins a finding to its real ``file:line`` necessarily quotes
      them; that is the assertion, not a leak.
    * ``evals/*/case.yaml`` -- every pattern is a literal in its own case file,
      so including them would flag all fourteen trace regexes against
      themselves and the rule would have to be deleted rather than obeyed.
      This exclusion has a real residual risk, since ``evals/`` ships: an agent
      that greps the eval directory sees the answer key. The fatal HALF of that
      risk is covered separately and unconditionally by
      ``test_negative_regexes_do_not_match_the_case_file_that_declares_them``,
      because a ``not_contains`` that matches its own declaration can never
      pass. The free-pass half is accepted, and the honest fix for it is to
      stop shipping ``evals/`` at all.

    These exclusions are why the ``file:line`` graders are backed by a separate
    renderer-produced anchor in the same case rather than standing alone."""
    roots = [
        _SKILL / "SKILL.md",
        *sorted((_SKILL / "references").glob("*.md")),
        *sorted((_SKILL / "scripts").glob("*.py")),
        *sorted(_EVALS.glob("*.md")),
        *sorted(_EVALS.glob("*.sh")),
    ]
    return [p for p in roots if p.is_file()]


def _agent_readable_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _agent_readable_sources())


def test_the_agent_readable_corpus_spans_more_than_the_prose_files() -> None:
    """Positive control for the corpus above. If it ever stops reaching the
    engine sources or the eval documentation, the vacuity rule silently narrows
    back to the hole this widening was written to close, every grader passes
    again, and nothing anywhere says so."""
    corpus = _agent_readable_text()
    for literal, why in (
        ("ran: no sha-pinned actions found", "scripts/scan.py"),
        ("Critical exploit-chain checks only", "scripts/report.py"),
        ("These cases run, on a harness that is not", "evals/README.md"),
    ):
        assert literal in corpus, (
            f"the agent-readable corpus no longer reaches {why} -- the vacuity "
            "rule is checking a narrower surface than it claims to")


def test_no_trace_regex_matches_the_skills_own_prose() -> None:
    """The vacuity rule -- see the module docstring. A run with the skill loaded
    puts SKILL.md (and any reference file the agent opens) into the transcript,
    so a trace-targeted pattern that matches the skill's own prose asserts
    nothing about behavior: ``contains`` passes for free, ``not_contains`` can
    never pass. Both are silent failures of the suite, which is why this is a
    test and not a review note.

    ``files``-targeted regexes are held to the same rule, and for a reason that
    is easy to miss: ``report.py`` INLINES catalog text into the report it
    renders, so the skill's own shipped prose is inside the file corpus too. A
    ``files`` pattern quoting the catalog would pass on the report having been
    rendered at all, never mind what the scan found."""
    prose = _agent_readable_text()
    offenders = []
    for case_dir, case in _all_cases():
        for grader in _graders(case):
            if (grader.get("type") != "regex"
                    or grader.get("target") not in {"trace", "files"}):
                continue
            pattern = grader["pattern"]
            if re.search(pattern, prose):
                offenders.append(f"{case_dir.name}/{grader['name']}: {pattern!r}")
    assert not offenders, (
        "these regexes match the skill's own shipped prose, so they grade the "
        "agent having READ the skill rather than the agent having RUN it -- "
        "re-anchor each on a string only the scanner or the renderer produces:\n  "
        + "\n  ".join(offenders))


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_scaffold_and_fixtures_referenced_by_the_case_exist(case_dir: Path) -> None:
    """The scaffold is what turns the cloaked ``dot-github/*.yml.fixture``
    sources into a real ``.github/workflows/`` tree in the sandbox. If it points
    at a fixture that has been renamed away, the agent is handed an empty
    repository and the case grades a run that had nothing to scan."""
    case = _load(case_dir)
    script_name = (case.get("context") or {}).get("scaffold_script")
    assert script_name, (
        "every case needs a scaffold_script: the agent under test requires a "
        "real .github/workflows/ tree, and the tracked fixtures are cloaked")

    script = case_dir / script_name
    assert script.is_file(), f"scaffold_script {script_name!r} does not exist"

    body = script.read_text(encoding="utf-8")
    slugs = re.findall(r"^FIXTURE_SLUG=(\S+)", body, flags=re.M)
    assert len(slugs) == 1, (
        f"expected exactly one FIXTURE_SLUG assignment in {script_name}, "
        f"found {slugs}")

    workflows = _EVALS / "files" / slugs[0] / "dot-github" / "workflows"
    assert workflows.is_dir(), f"fixture directory {workflows} does not exist"
    fixtures = sorted(workflows.glob("*.yml.fixture"))
    assert fixtures, f"{workflows} holds no *.yml.fixture files to materialize"

    common = _EVALS / "_scaffold_common.sh"
    assert common.is_file(), "the shared scaffold body is missing"
    assert "_scaffold_common.sh" in body, (
        f"{script_name} does not source the shared scaffold body")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_plugins_entry_resolves_to_this_skill(case_dir: Path) -> None:
    """``skills/ci-secure/`` is a bare skill folder with no ``plugin.json``, so
    the harness cannot auto-detect the plugin under test. Under
    ``--ablation with-without`` a case whose plugin set resolves empty fails up
    front, so the explicit relative path is what keeps the suite runnable at
    all."""
    case = _load(case_dir)
    plugins = case.get("plugins")
    assert plugins, (
        "plugins: is required here -- a bare skill folder is not auto-detected "
        "and the ablation arm would compare nothing to nothing")
    for entry in plugins:
        assert (case_dir / entry).resolve() == _SKILL, (
            f"plugins entry {entry!r} resolves to {(case_dir / entry).resolve()}, "
            f"not to the skill root {_SKILL}")


def _run_scaffold(case_dir: Path, cwd: Path):
    """Invoke a case's ``scaffold.sh`` the way the harness does: ``bash <script>``
    with the sandbox working directory as cwd and the harness's minimal env."""
    import os
    import subprocess

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(cwd),
        "TMPDIR": str(cwd),
        "TERM": "dumb",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["bash", str(case_dir / "scaffold.sh")],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_scaffold_refuses_to_run_over_an_existing_checkout(
    case_dir: Path, tmp_path: Path,
) -> None:
    """The scaffold's whole job is to overwrite ``.github/workflows/`` with
    deliberately vulnerable YAML and ``git commit`` it. That is correct in an
    empty eval sandbox and catastrophic anywhere else: run by hand from a real
    checkout it destroys that repository's live CI workflow, replaces it with a
    ``pull_request_target`` template-injection workflow, and commits the result
    to the current branch -- which is exactly the tracked-vulnerable-workflow
    condition the ``dot-github/*.yml.fixture`` cloak exists to prevent, and which
    ``tests/test_ci_secure_install_surface.py`` cannot see because it only
    inspects paths under ``skills/ci-secure/``.

    ``git init`` on an existing repository is a silent re-init, so nothing in the
    script's own control flow notices. The scaffold must therefore refuse the
    unsafe cwd itself, loudly, before it copies anything."""
    victim = tmp_path / "victim"
    (victim / ".github" / "workflows").mkdir(parents=True)
    live = victim / ".github" / "workflows" / "ci.yml"
    live.write_text("name: real ci\non: push\n", encoding="utf-8")

    import subprocess
    subprocess.run(["git", "init", "-q", "."], cwd=str(victim), check=True)

    done = _run_scaffold(case_dir, victim)

    assert done.returncode != 0, (
        "the scaffold ran to completion inside an existing git checkout; it "
        "must refuse a non-empty working directory instead of overwriting it")
    assert live.read_text(encoding="utf-8") == "name: real ci\non: push\n", (
        "the scaffold overwrote a real workflow file outside the eval sandbox")
    # check=True matters here: this is the assertion that nothing was committed,
    # and a git command that failed to run would otherwise return empty stdout
    # and read as "no commits" on precisely the outcome the guard exists to
    # prevent. `rev-list --count --all` is used rather than `log` because it
    # succeeds with "0" on a repository that has no commits yet, where `git log`
    # exits non-zero and would make the happy path indistinguishable from a
    # broken subprocess.
    count = subprocess.run(
        ["git", "rev-list", "--count", "--all"], cwd=str(victim),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert count == "0", (
        f"the scaffold committed to a repository it did not create: {count} "
        "commit(s) present")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_scaffold_refuses_when_it_cannot_read_the_directory(
    case_dir: Path, tmp_path: Path,
) -> None:
    """The emptiness check must not FAIL OPEN. A listing that errors produces no
    output, and "no output" read as "the directory is empty" turns the one guard
    standing between this script and a destroyed checkout into a no-op in
    exactly the case where it cannot see what it is about to overwrite. Unknown
    is not the same as empty -- the same rule the skill applies to a check that
    could not run."""
    import os
    import subprocess

    victim = tmp_path / "unreadable"
    (victim / ".github" / "workflows").mkdir(parents=True)
    live = victim / ".github" / "workflows" / "ci.yml"
    live.write_text("name: real ci\non: push\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=str(victim), check=True)

    os.chmod(victim, 0o300)  # writable and traversable, NOT listable
    try:
        done = _run_scaffold(case_dir, victim)
    finally:
        os.chmod(victim, 0o700)

    assert done.returncode != 0, (
        "the scaffold treated an unreadable directory as an empty one and ran "
        f"to completion:\n{done.stdout}\n{done.stderr}")
    assert live.read_text(encoding="utf-8") == "name: real ci\non: push\n", (
        "the scaffold overwrote a real workflow file it could not even list")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_scaffold_still_materializes_the_tree_in_an_empty_sandbox(
    case_dir: Path, tmp_path: Path,
) -> None:
    """The other side of the guard above: the refusal must be scoped to unsafe
    working directories, not so broad that the scaffold stops working in the
    empty sandbox every case depends on."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    done = _run_scaffold(case_dir, sandbox)

    assert done.returncode == 0, (
        f"scaffold failed in an empty sandbox:\n{done.stdout}\n{done.stderr}")
    made = sorted(p.name for p in (sandbox / ".github" / "workflows").glob("*.yml"))
    assert made, "scaffold produced no .github/workflows/*.yml in the sandbox"
    assert not any(p.name.endswith(".fixture") for p in
                   (sandbox / ".github" / "workflows").iterdir()), (
        "the cloaked .fixture suffix survived into the materialized tree")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_regex_graders_declare_a_target(case_dir: Path) -> None:
    """``target`` is optional in the harness's schema, and the vacuity rule only
    inspects graders that name one. So omitting the line is a one-token bypass
    of the rule: a verbatim ``SKILL.md`` quotation with no ``target:`` passes
    every test in this file. Requiring it here closes that."""
    for grader in _graders(_load(case_dir)):
        if grader.get("type") != "regex":
            continue
        assert "target" in grader, (
            f"regex grader {grader.get('name')!r} declares no target. The "
            "vacuity rule only reaches trace-targeted regexes, so an implicit "
            "target silently exempts the grader from it -- say `target: trace`")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_count_bearing_regexes_cannot_match_a_larger_count(case_dir: Path) -> None:
    """A pattern that opens with a bare digit matches inside a longer number.
    ``0 critical findings`` is contained in ``10 critical findings`` -- so
    ``clean-repo``, the case whose entire job is to notice that a zero-findings
    run stopped being zero, passes under the single worst regression it exists
    to catch. Same for every ``N of 10 vectors hit`` banner grader.

    The fix is a ``(?<![\\d.])`` guard, which both Python's ``re`` and
    JavaScript's ``RegExp`` support. This test asserts the guard is present
    rather than the absence of one bad match, so it also covers counts nobody
    has thought of yet."""
    for grader in _graders(_load(case_dir)):
        if grader.get("type") != "regex":
            continue
        pattern = grader["pattern"]
        if not re.match(r"^[0-9]", pattern):
            continue
        pytest.fail(
            f"regex grader {grader.get('name')!r} starts with a bare digit: "
            f"{pattern!r}. It will match inside a larger number, so a run "
            "reporting ten of something satisfies a grader asserting one or "
            "zero. Prefix it with (?<![\\d.]).")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_negative_regexes_do_not_match_the_case_file_that_declares_them(
    case_dir: Path,
) -> None:
    """``evals/`` ships inside the installed skill, so a ``case.yaml`` is on disk
    in the sandbox exactly like ``scripts/scan.py`` is -- and a ``not_contains``
    pattern that matches its own declaration can never pass once anything greps
    the eval directory. This is the fatal direction of the vacuity trap, so it
    is checked even though the corpus in ``_agent_readable_sources()``
    deliberately excludes the case files (see the note there).

    The trap is not obvious: a pattern containing ``[^\\n]{0,40}`` matches the
    literal characters ``[^\\n]{0,40}`` sitting in its own source line, because
    every one of them is a non-newline character."""
    text = (case_dir / "case.yaml").read_text(encoding="utf-8")
    for grader in _graders(_load(case_dir)):
        if grader.get("type") != "regex" or grader.get("match") != "not_contains":
            continue
        assert not re.search(grader["pattern"], text), (
            f"not_contains grader {grader.get('name')!r} matches the text of "
            f"{case_dir.name}/case.yaml, which ships inside the skill. One Grep "
            "of the eval directory makes it unpassable on a run that behaved "
            "perfectly -- and a not_contains that can never pass fails silently")


def _grep_shaped_fixture_output(case_dir: Path) -> str:
    """The fixture workflows re-rendered the way a `grep -n` prints them.

    ``grep`` writes one line per hit as ``<path>:<line>:<text>``, and the agent
    reaches the fixtures at two paths depending on where it runs: bare
    bare from inside the workflows directory and prefixed with
    ``.github/workflows/`` from the repository root. Both are rendered, because
    a pattern can be safe against one and not the other.

    Deliberately NARROWER than the reachable-text corpus in
    ``test_every_case_anchors_on_output_the_agent_could_not_have_written``,
    which serves the opposite direction (disqualifying weak POSITIVE anchors)
    and so casts the widest net it can, including a ``<path>:<line> <text>``
    variant. Only grep's true ``<path>:<line>:<text>`` shape belongs here: it is
    a shape tools emit and ``report.py`` never does, which is what makes a match
    proof of contamination rather than of a real finding. Widening this one
    would forbid negatives that legitimately quote the renderer.
    """
    case = _load(case_dir)
    script = case_dir / (case.get("context") or {}).get("scaffold_script", "")
    slugs = re.findall(r"^FIXTURE_SLUG=(\S+)", script.read_text(encoding="utf-8"),
                       flags=re.M)
    workflows = _EVALS / "files" / slugs[0] / "dot-github" / "workflows"
    lines = []
    for fixture in sorted(workflows.glob("*.yml.fixture")):
        name = fixture.name[: -len(".fixture")]
        for n, text in enumerate(fixture.read_text(encoding="utf-8").splitlines(), 1):
            lines.append(f"{name}:{n}:{text}")
            lines.append(f".github/workflows/{name}:{n}:{text}")
    return "\n".join(lines)


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_negative_trace_regexes_do_not_match_grep_output_over_the_fixtures(
    case_dir: Path,
) -> None:
    """The third face of the vacuity trap, and the one that fires on a GOOD run.

    A ``trace``-targeted grader reads ``_transcript``, which folds in the output
    of every tool the agent ran -- not just the agent's own prose. So a
    ``not_contains`` pattern shaped like ``<file>:<line>...<word>`` is matched by
    ``grep``'s own ``<path>:<line>:<text>`` rendering of the fixture the agent
    was told to audit. The case then reports a behavioural regression for a
    session that did exactly the right thing, and the harder the agent looks at
    the file it is supposed to clear, the likelier it fails.

    This is not hypothetical: ``pwn-request``'s clean fixture has the literal
    YAML key ``jobs:`` on line 7, so ``grep -n jobs .github/workflows/*.yml`` --
    which that case's sibling llm grader actively rewards the agent for running
    -- emits ``safe.yml:7:jobs:``.

    Only ``trace`` is held to this rule. The ``files`` corpus introduces each
    file as ``=== <path> ===`` followed by its raw bytes, so a ``<path>:<line>:``
    sequence cannot arise there.
    """
    grep_output = _grep_shaped_fixture_output(case_dir)
    for grader in _graders(_load(case_dir)):
        if (grader.get("type") != "regex"
                or grader.get("match") != "not_contains"
                or grader.get("target") != "trace"):
            continue
        hit = re.search(grader["pattern"], grep_output)
        assert not hit, (
            f"not_contains grader {grader.get('name')!r} matches {hit.group(0)!r} "
            "-- which is what `grep -n` prints for this case's own fixture, not "
            "anything the skill reported. Reading the file under audit is "
            "correct behaviour, so this grader fails correct runs. Anchor it on "
            "the renderer's shape, or move it to `target: files`")


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_allowed_tools_name_tools_the_runner_can_actually_grant(
    case_dir: Path,
) -> None:
    """``allowed_tools`` RESTRICTS rather than grants, and an entry the runner
    does not recognise is dropped with a per-case warning on stderr rather than
    an error. So a plausible-looking name that is not a real tool costs nothing
    at validation time and produces noise on the first run -- which, for a suite
    whose first execution is by definition suite debugging, is the worst moment
    to be reading spurious warnings. ``Task`` was in every case and is not a
    grantable name."""
    grantable = {
        # Auto-allowed (read-only) set.
        "Read", "Glob", "Grep", "NotebookRead", "Skill", "AskUserQuestion",
        "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop",
        "TaskOutput", "Agent", "TodoWrite",
        # Gated, reachable only through an operator --allow-tools grant.
        "Bash", "Write", "Edit", "WebFetch", "WebSearch",
    }
    tools = ((_load(case_dir).get("execution") or {}).get("allowed_tools")) or []
    unknown = sorted(t for t in tools if t.split("(")[0] not in grantable)
    assert not unknown, (
        f"allowed_tools names {unknown}, which the runner cannot grant. Entries "
        "it does not recognise are dropped and reported as an ungranted tool")


def _engine_output_for(case_dir: Path, tmp: Path) -> str:
    """Scaffold the case's fixture and return what the engine actually emits:
    ``run.py``'s stdout followed by the rendered report. Both reach the
    transcript through tool results on the calls ``SKILL.md`` mandates."""
    import subprocess

    sandbox = tmp / case_dir.name
    sandbox.mkdir(parents=True)
    scaffold = _run_scaffold(case_dir, sandbox)
    assert scaffold.returncode == 0, scaffold.stderr

    scripts = _SKILL / "scripts"
    findings = sandbox / "findings.json"
    run = subprocess.run(
        ["python3", str(scripts / "run.py"), "--root", str(sandbox),
         "--out", str(findings), "--gh-impostor", "off"],
        capture_output=True, text=True, cwd=str(sandbox), timeout=120,
    )
    assert run.returncode == 0, run.stderr
    report = subprocess.run(
        ["python3", str(scripts / "report.py"), "--in", str(findings)],
        capture_output=True, text=True, timeout=120,
    )
    assert report.returncode == 0, report.stderr
    return run.stdout + "\n" + report.stdout


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
def test_every_case_anchors_on_output_the_agent_could_not_have_written(
    case_dir: Path, tmp_path: Path,
) -> None:
    """Each case needs at least one grader that only a COMPLETED engine run can
    satisfy -- matching the engine's real output while matching nothing the
    skill ships.

    ``tool_used`` cannot be that grader. It sees a Bash command that mentioned
    ``run.py``; a command that mentioned it and exited 1 looks identical. And a
    banner regex is not automatically it either: the banner's *format* is
    documented in ``SKILL.md``, so on a fixture with one finding an agent that
    eyeballed the YAML can write the count itself. What it cannot invent is the
    engine's own serialization -- ``run.py`` prints its group list as a JSON
    array of string-sorted ids, and the report renders occurrences at lines only
    the scanner computed.

    Without this, a scanner that crashed and an agent that guessed well score
    identically, which is the failure mode the whole suite exists to rule out."""
    produced = _engine_output_for(case_dir, tmp_path)

    # Everything the agent can reach WITHOUT a working engine: the skill's own
    # shipped text, plus the fixture workflows it was handed -- both as raw
    # content and as `grep -n` renders them, since `path:lineno:text` is how a
    # file:line lands in a transcript. This is what disqualifies the
    # occurrence-line graders from counting as completion anchors: on an
    # 11-line fixture, `ci.yml:11` is one grep away and says nothing about
    # whether the scanner ever finished.
    # The case file itself belongs in this corpus even though the vacuity rule
    # excludes it, because `evals/` ships: an agent that greps the eval
    # directory reads the answer key. A grader whose own declaration satisfies
    # it is not evidence the engine ran. Most graders cannot avoid this -- a
    # plain literal always matches its own `pattern:` line -- which is precisely
    # why the COMPLETION anchor must be one that does, and why an anchor must
    # never be a string the surrounding comment quotes in full.
    reachable = [_agent_readable_text(), (case_dir / "case.yaml").read_text(encoding="utf-8")]
    slug = re.search(r"^FIXTURE_SLUG=(\S+)",
                     (case_dir / "scaffold.sh").read_text(encoding="utf-8"),
                     flags=re.M).group(1)
    for fixture in sorted(
        (_EVALS / "files" / slug / "dot-github" / "workflows").glob("*.yml.fixture")
    ):
        name = fixture.name[: -len(".fixture")]
        body = fixture.read_text(encoding="utf-8")
        reachable.append(body)
        for lineno, line in enumerate(body.splitlines(), start=1):
            for path in (name, f".github/workflows/{name}"):
                reachable.append(f"{path}:{lineno}:{line}")
                reachable.append(f"{path}:{lineno} {line}")
    corpus = "\n".join(reachable)

    anchors = [
        g["name"] for g in _graders(_load(case_dir))
        if g.get("type") == "regex"
        and g.get("target") == "trace"
        and g.get("match") != "not_contains"
        and re.search(g["pattern"], produced)
        and not re.search(g["pattern"], corpus)
    ]
    assert anchors, (
        f"{case_dir.name} has no grader that a completed engine run satisfies "
        "and a failed one does not. Every positive trace regex here either "
        "fails to match the engine's real output or is a string the skill "
        "already ships. Anchor one on run.py's printed group list or on a "
        "rendered occurrence line.")


def test_no_stray_case_files_among_the_fixtures() -> None:
    """Any file named ``case.yaml`` or ``prompt.md`` anywhere below ``evals/``
    is picked up as a case. The fixture trees under ``evals/files/`` are shared
    resources, not cases, and a file dropped in there with either name would be
    run silently -- with real API spend and no graders anyone wrote."""
    strays = sorted(
        p.relative_to(_SKILL).as_posix()
        for p in (_EVALS / "files").rglob("*")
        if p.is_file() and p.name in {"case.yaml", "prompt.md"}
    )
    assert not strays, (
        "these files under evals/files/ would be discovered and run as eval "
        f"cases: {strays}")

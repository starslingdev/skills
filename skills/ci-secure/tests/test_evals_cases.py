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
all. Both directions collapse to one rule: a trace-targeted regex must not match
the skill's shipped prose. This is not hypothetical -- half a dozen natural
choices for these graders (``P14.10``, ``did NOT run``, ``No critical attack
vectors``, ``Impostor-SHA check (P14.11): ran``) are all quotations from
``SKILL.md``, and every one of them was replaced with a scanner-produced string
after this test flagged it.

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


def test_no_trace_regex_matches_the_skills_own_prose() -> None:
    """The vacuity rule -- see the module docstring. A run with the skill loaded
    puts SKILL.md (and any reference file the agent opens) into the transcript,
    so a trace-targeted pattern that matches the skill's own prose asserts
    nothing about behavior: ``contains`` passes for free, ``not_contains`` can
    never pass. Both are silent failures of the suite, which is why this is a
    test and not a review note."""
    prose = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [_SKILL / "SKILL.md", *sorted((_SKILL / "references").glob("*.md"))]
    )
    offenders = []
    for case_dir, case in _all_cases():
        for grader in _graders(case):
            if grader.get("type") != "regex" or grader.get("target") != "trace":
                continue
            pattern = grader["pattern"]
            if re.search(pattern, prose):
                offenders.append(f"{case_dir.name}/{grader['name']}: {pattern!r}")
    assert not offenders, (
        "these trace regexes match the skill's own shipped prose, so they grade "
        "the agent having READ the skill rather than the agent having RUN it -- "
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

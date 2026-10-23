"""evals.json stays runnable, and no check can pass by reading the skill.

Entries carrying `checks` are executed as real agent sessions by
maintainers/scripts/run_skill_evals.py. This pins, offline, what that runner
needs from the file, plus the vacuity rule: a `trace` / `files` /
`last_message` regex that matches text the agent can read under the skill
would pass the moment the agent opens that file, without the audit running.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1]
_EVALS = json.loads((_SKILL / "evals" / "evals.json").read_text(encoding="utf-8"))
_RUNNABLE = [e for e in _EVALS["evals"] if e.get("checks")]
_TYPES = {"tool_used", "trace", "files", "last_message", "reading"}
_READING_FIELDS = {"reference_bytes", "reference_lines", "reference_files",
                   "catalog_opened", "catalog_entries_read"}


def _agent_readable() -> list[Path]:
    """What the runner's plugin copy mounts: the skill minus evals/,
    CHANGELOG.md, tests/test_*.py, tests/conftest.py and tests/fixtures/."""
    out = []
    for p in _SKILL.rglob("*"):
        rel = p.relative_to(_SKILL).parts
        if not p.is_file() or "__pycache__" in rel or rel[0] in ("evals", "CHANGELOG.md"):
            continue
        if rel[0] == "tests" and (rel[1] == "fixtures" or rel[-1].startswith("test_")
                                  or rel[-1] == "conftest.py"):
            continue
        out.append(p)
    return out


def test_five_entries_are_runnable_and_ids_are_unique():
    ids = [e["id"] for e in _EVALS["evals"]]
    assert len(ids) == len(set(ids))
    assert {e["name"] for e in _RUNNABLE} == {
        "full-run-synthetic", "security-ask-routes-to-ci-secure", "quality-review-opt81",
        "gap-fill-coverage-lookup", "no-catalog-read-needed"}


@pytest.mark.parametrize("entry", _RUNNABLE, ids=lambda e: e["name"])
def test_every_check_is_well_formed(entry):
    assert entry["prompt"].strip()
    for c in entry["checks"]:
        assert c["type"] in _TYPES, c
        assert c.get("match", "contains") in ("contains", "not_contains"), c
        if c["type"] == "reading":
            assert c["field"] in _READING_FIELDS, c
            assert set(c) <= {"name", "type", "field", "pattern", "min", "max"}, c
        re.compile(c.get("pattern", ""))
    for name in entry.get("plugins", []):
        assert (_SKILL.parent / name / "SKILL.md").is_file(), name


@pytest.mark.parametrize("entry", _RUNNABLE, ids=lambda e: e["name"])
def test_every_fixture_referenced_exists(entry):
    if entry.get("fixture"):
        assert (_SKILL / "evals" / "fixtures" / entry["fixture"] / "scaffold.py").is_file()


def test_no_text_check_matches_anything_the_agent_can_read():
    files = _agent_readable()
    assert any(p.name == "SKILL.md" for p in files)
    assert any(p.name == "verify_report.py" for p in files)
    assert not any(p.name.startswith("test_") for p in files)
    hits = []
    for entry in _RUNNABLE:
        for c in entry["checks"]:
            if c["type"] in ("tool_used", "reading"):
                continue
            pat = re.compile(c["pattern"])
            hits += [(c["name"], p.relative_to(_SKILL).as_posix()) for p in files
                     if pat.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert not hits, f"checks satisfiable by reading the skill: {hits}"


@pytest.mark.parametrize("command,expected", [
    ('git add .\ngit commit -m "save"', 1),
    ("cd /x\ngit push origin HEAD", 1),
    ("git add . && git commit -m x", 1),
    ("git -C /r commit -m z", 1),
    ("python3 scripts/run.py\npython3 tests/verify_report.py r.md", 0),
])
def test_nothing_committed_check_sees_multiline_bash(command, expected):
    """The runner records a tool call as `Name <json.dumps(input)>`, so a newline
    reaches the regex as the two characters `\\n`; a commit on a later line must
    still count."""
    entry = next(e for e in _RUNNABLE if e["name"] == "full-run-synthetic")
    pat = next(c["pattern"] for c in entry["checks"]
               if c["name"] == "nothing-committed-or-pushed")
    call = "Bash " + json.dumps({"command": command})
    assert len(re.findall(pat, call)) == expected, call


@pytest.mark.parametrize("answer,ok", [
    ("MISSING-FROM-CARD: non-standard runner labels are often unavailable to fork pull requests", True),
    ("MISSING-FROM-CARD: The faster runner label isn't available to fork PRs.", True),
    ("MISSING-FROM-CARD: larger runners aren't offered to PRs from forks", True),
    ("MISSING-FROM-CARD: none, the card already says a fork pull request can select the slower label", False),
    ("MISSING-FROM-CARD: fork PRs get no repo secrets and can't reach upstream branch caches", False),
])
def test_the_fork_caveat_check_takes_the_caveat_and_only_the_caveat(answer, ok):
    """The card itself mentions forks, so `fork` plus any negation is not the caveat:
    it must say a runner/label is not available to fork PRs, and `none` never passes."""
    entry = next(e for e in _RUNNABLE if e["name"] == "quality-review-opt81")
    check = next(c for c in entry["checks"] if c["name"] == "names-the-catalog-only-fork-caveat")
    assert bool(re.search(check["pattern"], "Review done.\n" + answer)) is ok


def test_reading_check_patterns_name_real_catalog_entries_and_reference_files():
    """A typo'd id or file name in a reading pattern would make a `max` check
    pass and a `min` check fail for the wrong reason."""
    catalog = "\n".join(p.read_text(encoding="utf-8") for p in
                        sorted((_SKILL / "references" / "patterns").glob("opt*.md")))
    names = {p.relative_to(_SKILL / "references").as_posix()
             for p in (_SKILL / "references").rglob("*") if p.is_file()}
    for entry in _RUNNABLE:
        for c in entry["checks"]:
            if c["type"] != "reading" or "pattern" not in c:
                continue
            pat = c["pattern"].replace("\\.", ".")
            ids = re.findall(r"OPT\d+", pat)
            if c["field"] == "catalog_entries_read":
                assert ids, c
            for i in ids:
                assert re.search(rf"(?m)^#{{1,3}} {i} —", catalog), (c["name"], i)
            for f in re.findall(r"[\w-]+\.(?:md|tsv|json)", pat):
                assert f in names, (c["name"], f)

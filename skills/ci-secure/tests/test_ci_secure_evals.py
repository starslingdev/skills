"""evals/evals.json stays runnable, and no check can pass without the scan.

Every entry runs as a real agent session under the shared runner
(maintainers/scripts/run_skill_evals.py --skill ci-secure), which is not part of
pytest. This pins, offline, what that runner needs from the file and the rules
that keep a check honest:

- the vacuity rule: a `trace` / `files` / `last_message` regex that matches text
  the agent can read in the mounted skill (SKILL.md, references, the engine
  sources, tests/verify_report.py) passes on reading rather than on running, and
  a `not_contains` that matches it can never pass;
- a `not_contains` on `trace` must not match `grep -n` output over the fixture,
  because reading the file under audit is correct behaviour;
- each entry has a positive `trace` anchor that only a completed engine run
  produces, and its `files` checks hold on the report the engine really renders.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1]
_EVALS_DIR = _SKILL / "evals"
_SPEC = json.loads((_EVALS_DIR / "evals.json").read_text(encoding="utf-8"))
_ENTRIES = _SPEC["evals"]
_TYPES = {"tool_used", "trace", "files", "last_message"}
_CHECK_KEYS = {"name", "type", "pattern", "match", "min", "max", "why"}
_ENTRY_KEYS = {"id", "name", "prompt", "expected_output", "fixture", "max_turns",
               "timeout", "files_require", "checks"}
_BY_NAME = {e["name"]: e for e in _ENTRIES}


def _fixture_dir(entry: dict) -> Path:
    return _EVALS_DIR / "fixtures" / entry["fixture"]


def _cloaked(entry: dict) -> list[Path]:
    return sorted((_fixture_dir(entry) / "dot-github" / "workflows").glob("*.yml.fixture"))


def _agent_readable() -> list[Path]:
    """What the runner's plugin copy mounts: the skill minus evals/,
    CHANGELOG.md, tests/test_*.py, tests/conftest.py and tests/fixtures/."""
    out = []
    for p in sorted(_SKILL.rglob("*")):
        rel = p.relative_to(_SKILL).parts
        if not p.is_file() or "__pycache__" in rel or rel[0] in ("evals", "CHANGELOG.md"):
            continue
        if rel[0] == "tests" and (rel[1] == "fixtures" or rel[-1].startswith("test_")
                                  or rel[-1] == "conftest.py"):
            continue
        out.append(p)
    return out


def _agent_readable_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in _agent_readable())


def _text_checks(entry: dict, match: str | None = None) -> list[dict]:
    return [c for c in entry["checks"] if c["type"] != "tool_used"
            and (match is None or c.get("match", "contains") == match)]


def _call(tool: str, payload: dict) -> str:
    """A tool call as the runner records it: `<Tool> <json input>`."""
    return f"{tool} {json.dumps(payload)}"


def test_five_entries_all_runnable_with_unique_ids_and_names():
    assert [e["name"] for e in _ENTRIES] == [
        "template-injection", "clean-repo", "pwn-request", "many-findings",
        "impostor-check-skipped"]
    assert len({e["id"] for e in _ENTRIES}) == len(_ENTRIES)
    assert all(e.get("checks") for e in _ENTRIES), "an entry without checks never runs"


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["name"])
def test_every_entry_and_check_is_well_formed(entry):
    assert set(entry) <= _ENTRY_KEYS, set(entry) - _ENTRY_KEYS
    assert entry["prompt"].strip() and entry["expected_output"].strip()
    if entry.get("files_require"):
        re.compile(entry["files_require"])
    names = [c["name"] for c in entry["checks"]]
    assert len(names) == len(set(names)), names
    for c in entry["checks"]:
        assert set(c) <= _CHECK_KEYS, (c["name"], set(c) - _CHECK_KEYS)
        assert c["type"] in _TYPES, c
        re.compile(c["pattern"])
        if c["type"] == "tool_used":
            lo, hi = c.get("min", 1), c.get("max")
            assert lo >= 0 and (hi is None or hi >= lo), c
            assert re.match(r"\^[A-Z]\w* ", c["pattern"]), (
                "anchor tool_used on the tool name: the runner records `<Tool> <json input>`")
        else:
            assert c.get("match", "contains") in ("contains", "not_contains"), c
    if any(c["type"] == "files" for c in entry["checks"]):
        assert entry.get("files_require"), (
            "a `files` check must name the report it reads, or scratch files the "
            "session wrote stand in for a report that was never collected")


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["name"])
def test_every_fixture_has_a_scaffold_and_cloaked_workflows(entry):
    assert (_fixture_dir(entry) / "scaffold.py").is_file()
    assert _cloaked(entry), f"{_fixture_dir(entry)} holds no *.yml.fixture to materialize"


def test_the_agent_readable_corpus_reaches_the_engine_sources():
    """Positive control: if the corpus stops reaching scripts/, the vacuity rule
    silently narrows to the prose files, which is how a `not_contains` was once
    written against a literal in scan.py where it could never pass."""
    corpus = _agent_readable_text()
    assert "ran: no sha-pinned actions found" in corpus  # scripts/scan.py
    assert "Critical exploit-chain checks only" in corpus  # scripts/report.py
    assert not any(p.name.startswith("test_") for p in _agent_readable())


def test_no_text_check_matches_anything_the_agent_can_read():
    corpus = {p: p.read_text(encoding="utf-8", errors="replace") for p in _agent_readable()}
    hits = [(e["name"], c["name"], p.relative_to(_SKILL).as_posix())
            for e in _ENTRIES for c in _text_checks(e)
            for p, text in corpus.items() if re.search(c["pattern"], text)]
    assert not hits, (
        "these checks are satisfiable by reading the skill rather than running it; "
        f"re-anchor each on a string only the scanner or renderer produces: {hits}")


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["name"])
def test_count_bearing_patterns_cannot_match_a_larger_count(entry):
    """`0 critical findings` is inside `10 critical findings`; a (?<![\\d.])
    guard is required on any pattern that opens with a digit."""
    bare = [c["name"] for c in _text_checks(entry) if re.match(r"[0-9]", c["pattern"])]
    assert not bare, f"prefix these with (?<![\\d.]): {bare}"


def _grep_render(entry: dict) -> str:
    """The fixture as `grep -n` prints it, from the workflows dir and the repo root."""
    lines = []
    for f in _cloaked(entry):
        name = f.name[: -len(".fixture")]
        for n, text in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            lines += [f"{name}:{n}:{text}", f".github/workflows/{name}:{n}:{text}"]
    return "\n".join(lines)


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["name"])
def test_negative_trace_checks_do_not_match_grep_output_over_the_fixture(entry):
    """The transcript folds in tool output, so a trace `not_contains` shaped
    like `<file>:<line>...` fails a session for reading the file under audit."""
    grep = _grep_render(entry)
    hits = [c["name"] for c in _text_checks(entry, "not_contains")
            if c["type"] == "trace" and re.search(c["pattern"], grep)]
    assert not hits, f"these fail correct runs that grep the fixture: {hits}"


@pytest.mark.parametrize("pattern_of,call,hit", [
    ("nothing-was-committed", {"command": 'git add .\ngit commit -m "x"'}, True),
    ("nothing-was-committed", {"command": "git -C /r commit -m z"}, True),
    ("nothing-was-committed", {"command": "git status && git log -1"}, False),
    ("nothing-was-pushed", {"command": "cd /x\ngit push origin HEAD"}, True),
    ("no-pull-request-was-opened", {"command": "gh pr create --fill"}, True),
    ("deterministic-engine-actually-ran", {"command": "python3 /p/scripts/run.py --root ."}, True),
    ("deterministic-engine-actually-ran", {"command": "ls scripts"}, False),
])
def test_tool_used_patterns_match_the_runners_record_of_a_bash_call(pattern_of, call, hit):
    pat = next(c["pattern"] for c in _BY_NAME["template-injection"]["checks"]
               if c["name"] == pattern_of)
    assert bool(re.search(pat, _call("Bash", call))) is hit
    assert not re.search(pat, _call("Read", {"file_path": f"/x/{call['command']}"}))


@pytest.mark.parametrize("skill,hit", [
    ("ci-secure", True), ("ci-secure:ci-secure", True), ("ci-secure-x", False), ("ci-score", False)])
def test_the_skill_check_matches_only_ci_secure(skill, hit):
    pat = _BY_NAME["clean-repo"]["checks"][0]["pattern"]
    assert bool(re.search(pat, _call("Skill", {"skill": skill}))) is hit


def _scaffold(entry: dict, sandbox: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run([sys.executable, str(_fixture_dir(entry) / "scaffold.py"),
                           str(sandbox), str(sandbox.parent)],
                          capture_output=True, text=True, timeout=120, env=env)


@pytest.mark.parametrize("entry", _ENTRIES[:4], ids=lambda e: e["fixture"])
def test_the_scaffold_materializes_and_commits_the_fixture_in_an_empty_sandbox(entry, tmp_path):
    sandbox = tmp_path / "repo"
    sandbox.mkdir()
    done = _scaffold(entry, sandbox)
    assert done.returncode == 0, done.stderr
    assert not [l for l in done.stdout.splitlines() if "=" in l], "no env lines expected"
    made = sorted(p.name for p in (sandbox / ".github" / "workflows").iterdir())
    assert made == [f.name[: -len(".fixture")] for f in _cloaked(entry)]
    count = subprocess.run(["git", "-C", str(sandbox), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert count == "1"


def test_the_scaffold_refuses_a_non_empty_directory(tmp_path):
    """Run by hand from a real checkout it would overwrite and COMMIT that
    repository's workflows with a vulnerable one; it must refuse first."""
    victim = tmp_path / "victim"
    (victim / ".github" / "workflows").mkdir(parents=True)
    live = victim / ".github" / "workflows" / "ci.yml"
    live.write_text("name: real ci\non: push\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(victim)], check=True)
    assert _scaffold(_BY_NAME["template-injection"], victim).returncode != 0
    assert live.read_text(encoding="utf-8") == "name: real ci\non: push\n"
    assert subprocess.run(["git", "-C", str(victim), "rev-list", "--count", "--all"],
                          capture_output=True, text=True, check=True).stdout.strip() == "0"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list a mode-0300 directory")
def test_the_scaffold_refuses_a_directory_it_cannot_list(tmp_path):
    """Unknown is not empty: a listing that errors must not read as empty."""
    victim = tmp_path / "unreadable"
    victim.mkdir()
    (victim / "keep.txt").write_text("x", encoding="utf-8")
    victim.chmod(0o300)
    try:
        done = _scaffold(_BY_NAME["template-injection"], victim)
    finally:
        victim.chmod(0o700)
    assert done.returncode != 0 and not (victim / ".github").exists()


def test_skill_md_still_writes_its_findings_and_report_under_tmpdir():
    """The runner points TMPDIR at a directory it collects; if SKILL.md moved its
    output elsewhere every `files` check would stop at the collection guard."""
    skill = (_SKILL / "SKILL.md").read_text(encoding="utf-8")
    for artifact in ("ci-secure-findings-", "ci-secure-report-"):
        assert f'"${{TMPDIR:-/tmp}}/{artifact}' in skill, artifact


def _engine(entry: dict, tmp: Path) -> tuple[str, str]:
    """(run.py stdout + rendered report, the `files` record the runner would
    build from the report), produced the way SKILL.md tells the agent to."""
    pytest.importorskip("yaml", reason="PyYAML is the scanner's one dependency")
    sandbox, out = tmp / "repo", tmp / "out"
    sandbox.mkdir()
    out.mkdir()
    assert _scaffold(entry, sandbox).returncode == 0
    scripts = _SKILL / "scripts"
    findings, report = out / "ci-secure-findings-x.json", out / "ci-secure-report-x.md"
    run = subprocess.run([sys.executable, str(scripts / "run.py"), "--root", str(sandbox),
                          "--out", str(findings), "--gh-impostor", "off"],
                         capture_output=True, text=True, timeout=120, cwd=str(sandbox))
    assert run.returncode == 0, run.stderr[-800:]
    rep = subprocess.run([sys.executable, str(scripts / "report.py"), "--in", str(findings),
                          "--out", str(report)], capture_output=True, text=True, timeout=120)
    assert rep.returncode == 0, rep.stderr[-800:]
    body = report.read_text(encoding="utf-8")
    return run.stdout + "\n" + rep.stdout + "\n" + body, f"=== {report.name} ===\n{body}"


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: e["name"])
def test_every_entry_anchors_on_output_only_a_completed_engine_run_produces(entry, tmp_path):
    """`tool_used` sees a command that mentioned run.py, which looks the same
    when it exited 1. So each entry needs a positive trace check that the
    engine's real output satisfies and that nothing the agent can reach
    without it (the skill, the fixture, `grep -n` over the fixture) does."""
    produced, files = _engine(entry, tmp_path)
    reachable = "\n".join([_agent_readable_text(), _grep_render(entry),
                           *(f.read_text(encoding="utf-8") for f in _cloaked(entry))])
    anchors = [c["name"] for c in _text_checks(entry, "contains") if c["type"] == "trace"
               and re.search(c["pattern"], produced) and not re.search(c["pattern"], reachable)]
    assert anchors, f"{entry['name']} has no check only a completed engine run satisfies"
    for c in (c for c in entry["checks"] if c["type"] == "files"):
        found = re.search(c["pattern"], files) is not None
        assert found is (c.get("match", "contains") == "contains"), (
            f"{c['name']} disagrees with the report the engine renders")
    if entry.get("files_require"):
        assert re.search(rf"(?m)^=== (?:{entry['files_require']}) ===$", files)

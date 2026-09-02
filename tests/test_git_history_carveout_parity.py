"""The git-history carve-out is duplicated, so pin it two-sided.

`fetch-depth: 0` is load-bearing for a job that walks git history, and BOTH
shipped engines that look at that line must reach the same verdict about it:
the speed engine (`skills/ci-speedup/scripts/scan.py`, the canonical source)
and the best-practices engine (`skills/ci-score/scripts/practice_facts.py`,
which carries a verbatim copy).

A copy is the only option — each skill installs and runs standalone, so
ci-score importing ci-speedup would break every installation of ci-score on
its own, and `skills/ci-score/tests/test_ci_score_contract.py` forbids the
import outright. The price of a copy is drift, and drift here means the two
engines handing one repository contradictory advice about one line of YAML
again.

So this test is the coupling: it loads both modules by file path and fails if
the two predicates disagree — on the pattern text, or on any input in the
battery below. Shape borrowed from the two-sided renderer/verifier pins in
`skills/ci-speedup/tests/test_verify_report_self.py`.

It lives at the repo ROOT, not inside either skill's suite: a cross-skill read
is normal here and forbidden there.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEEDUP = _ROOT / "skills" / "ci-speedup" / "scripts" / "scan.py"
_SCORE = _ROOT / "skills" / "ci-score" / "scripts" / "practice_facts.py"


def _load(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sides():
    pytest.importorskip("yaml")
    speed = _load("_parity_ci_speedup_scan", _SPEEDUP)
    score = _load("_parity_ci_score_practice_facts", _SCORE)
    return speed, score


# Where a `$` or a `HEAD` SITS on the command line decides whether it says
# anything about history, and these rows pin that by VERDICT, not only by
# agreement between the two engines. They carry expected values because the
# agreement arm alone is blind to a shape neither engine has a row for: the
# operand-position rules below were tightened in the canonical engine while
# ci-score's copy still carried the loose original, and no behavioural row in
# this battery could tell — only the character-identity arm caught it.
_OPERAND_POSITION_ROWS = [
    # A positional parameter is a ref operand like any other variable.
    ('git diff "$1" HEAD', True),
    # Past a redirection arrow the `$` names the FILE the diff is written to.
    ("git diff --stat >> $GITHUB_STEP_SUMMARY", False),
    ("git diff > $OUT", False),
    # Past a bare `--` separator the `$` names a pathspec, not a base commit.
    ('git diff --exit-code -- "$FILE"', False),
    # A cat-file anchored at HEAD reads a blob every clone depth already has.
    ("git cat-file -p HEAD:package.json", False),
]


# Every clause of the shared pattern gets at least one row, plus the shapes the
# carve-out exists to protect and the near-misses it must NOT protect.
_TEXT_BATTERY = [
    "",
    "npm test",
    "npm run build && npm run lint",
    "git log --oneline",
    "git describe --tags --abbrev=0",
    "git rev-list --count HEAD",
    "git rev-parse HEAD~1",
    "git fetch --depth=1 origin main",
    "git show HEAD:package.json",
    "git tag -l",
    "git pull --rebase origin main",
    "git rebase origin/main",
    "git merge --no-ff main",
    "git cherry-pick abc1234",
    "git diff origin/main...HEAD",
    "git diff --name-only ${{ github.event.pull_request.base.sha }} HEAD",
    'git diff --name-only "$base" HEAD',
    "git diff --name-only ${BASE} HEAD",
    'git cat-file -e "${base}^{commit}"',
    "git diff --name-only HEAD~1 HEAD",
    "changelog generation",
    "npx auto-changeset",
    "pnpm changeset version",
    "release-please --token=x",
    "nx affected --target=test",
    "lerna run test --since main",
    "actions/checkout@v4",
    "dorny/paths-filter@v3",
    "tj-actions/changed-files@v45",
    "changesets/action@v1",
    # line continuations: the operand that reveals the walk sits on the next line
    "git diff --name-only \\\n  origin/main...HEAD",
    "git \\\n  log --oneline",
    "git diff --name-only \\\n\t\tHEAD~1 HEAD",
    # prose that mentions git without running a history op
    "# we do not use git history here",
    "echo 'git log is not run'",
    "grep -r 'changelog' docs/",
    "curl --tags-are-not-a-flag https://example.test",
    "docker build --tags foo .",
] + [t for t, _expected in _OPERAND_POSITION_ROWS]


def test_has_git_history_op_agrees_on_every_battery_row(sides):
    speed, score = sides
    mismatches = [t for t in _TEXT_BATTERY
                  if speed._has_git_history_op(t) != score._has_git_history_op(t)]
    assert not mismatches, (
        f"the git-history predicate diverged between the two engines on "
        f"{mismatches!r} — re-sync ci-score's copy with the canonical source in "
        f"ci-speedup's scanner; the two engines must not give one repository "
        f"contradictory advice about the same fetch-depth line")


def test_operand_position_rows_get_the_verdict_they_are_pinned_to(sides):
    """Both engines, checked against expected values rather than each other."""
    for engine in sides:
        wrong = [t for t, expected in _OPERAND_POSITION_ROWS
                 if engine._has_git_history_op(t) is not expected]
        assert not wrong, (
            f"{engine.__name__} read the wrong verdict for {wrong!r} — a `$` or a "
            f"`HEAD` only counts where a ref operand can stand")


def test_the_copied_patterns_are_character_identical(sides):
    speed, score = sides
    for name in ("_GIT_HISTORY_RE", "_HISTORY_JOB_NAME_RE", "_LINE_CONTINUATION_RE"):
        a, b = getattr(speed, name), getattr(score, name)
        assert a.pattern == b.pattern, f"{name} pattern text diverged"
        assert a.flags == b.flags, f"{name} flags diverged"


_JOB_BATTERY = [
    # (job_name, job dict)
    ("unit", {"steps": [{"run": "npm test"}]}),
    ("build", {"steps": [{"uses": "actions/checkout@v4"}, {"run": "npm run build"}]}),
    ("changelog", {"steps": [{"run": "npm test"}]}),           # name fallback
    ("release", {"steps": [{"run": "npm publish"}]}),          # name fallback
    ("version", {"steps": []}),                                # name fallback
    ("publish-snapshot", {"steps": [{"run": "echo hi"}]}),     # name fallback
    ("notes", {"steps": [{"run": "git log --oneline"}]}),      # run: op
    ("gate", {"steps": [{"uses": "tj-actions/changed-files@v45"}]}),  # uses: op
    ("gate2", {"steps": [{"uses": "dorny/paths-filter@v3"}]}),
    ("diff", {"steps": [{"run": "git diff --name-only \\\n  origin/main...HEAD"}]}),
    ("empty", {}),
    ("no-steps", {"steps": None}),
    ("junk-steps", {"steps": [None, 42, {"run": "git describe --tags"}]}),
    ("nonstring-run", {"steps": [{"run": {"not": "a string"}}]}),
    ("nonstring-uses", {"steps": [{"uses": ["nope"]}]}),
]


def test_job_needs_git_history_agrees_on_every_battery_row(sides):
    speed, score = sides
    speed._GIT_HISTORY_LOCAL_ACTIONS = set()
    mismatches = [
        name for name, job in _JOB_BATTERY
        if speed._job_needs_git_history(job, name)
        != score._job_needs_git_history(job, name)]
    assert not mismatches, (
        f"the job-needs-history check diverged between the two engines on jobs "
        f"{mismatches!r} — re-sync ci-score's copy with ci-speedup's scanner")


def test_local_composite_action_indexing_agrees(sides, tmp_path: Path):
    """A history op hidden inside a local composite action, and the fail-CLOSED
    branch for an action file that cannot be read: both engines must treat the
    invoking job as a history job."""
    speed, score = sides
    act = tmp_path / ".github" / "actions" / "changed"
    act.mkdir(parents=True)
    act.joinpath("action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - run: git diff origin/main...HEAD\n"
        "      shell: bash\n")
    plain = tmp_path / ".github" / "actions" / "plain"
    plain.mkdir(parents=True)
    plain.joinpath("action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - run: npm ci\n      shell: bash\n")
    doc = {"on": {"pull_request": None}, "jobs": {
        "a": {"steps": [{"uses": "./.github/actions/changed"}]},
        "b": {"steps": [{"uses": "./.github/actions/plain"}]},
        "c": {"steps": [{"uses": "./.github/actions/missing"}]},   # unreadable → fail closed
    }}
    parsed = [(".github/workflows/ci.yml", doc, "")]

    speed_idx = speed._index_local_git_actions(tmp_path, parsed)
    score_idx = score._index_local_git_actions(tmp_path, parsed)
    assert speed_idx == score_idx, "the local composite-action index diverged"
    assert "./.github/actions/changed" in score_idx
    assert "./.github/actions/missing" in score_idx   # fail closed
    assert "./.github/actions/plain" not in score_idx

    speed._GIT_HISTORY_LOCAL_ACTIONS = speed_idx
    for jid, job in doc["jobs"].items():
        assert speed._job_needs_git_history(job, jid) == \
            score._job_needs_git_history(job, jid, score_idx), jid


def test_the_copy_stays_a_copy_and_never_becomes_an_import():
    """The whole point of duplicating the predicate is that ci-score installs
    and runs on its own. No import line in it may reach for ci-speedup."""
    for line in _SCORE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "ci_speedup" not in stripped and "ci-speedup" not in stripped and \
                "scan" not in stripped, stripped

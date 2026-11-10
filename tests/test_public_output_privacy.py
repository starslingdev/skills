from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / ".github" / "scripts"
_TARGET = "/".join(("example", "public-project"))
# Same owner as the target (caught by the bare-slug rule) and a DIFFERENT owner
# (so the same-owner rule cannot mask a URL pattern that fails to match).
_SAME_OWNER = "/".join(("example", "internal-project"))
_FOREIGN = "/".join(("other-owner", "private-project"))
_LINK = f"https://github.com/{_FOREIGN}\n"


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_public_output", _SCRIPTS / "check_public_output.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _lines(text, **kwargs):
    return [f.line for f in checker.scan_text(text, source="t", target=_TARGET, **kwargs)]


# --- scan_text -------------------------------------------------------------------


# `{}` is filled with a foreign-owner repo, then a same-owner one.
_BLOCKED = [
    "See https://github.com/{} for context", "(https://github.com/{})",
    "[context](https://github.com/{})", "`https://github.com/{}`", "<https://github.com/{}>",
    "[https://github.com/{}]", '"https://github.com/{}",', "'https://github.com/{}'",
    "https://github.com/{}, then more", "https://github.com/{}.",
    "https://github.com/{}/pull/91", "github.com/{}", "www.github.com/{}",
    "https://www.github.com/{} here", "git@github.com:{}.git", "git@github.com:{}",
    "https://api.github.com/repos/{} now", "https://api.github.com/repos/{})",
    "https://api.github.com/repos/{}/issues/91", "{}#91", "Derived from {}#91",
]


@pytest.mark.parametrize(
    "line", [form.format(repo) for repo in (_FOREIGN, _SAME_OWNER) for form in _BLOCKED]
    + [f"Provenance: {_SAME_OWNER}"],
)
def test_cross_repository_references_are_blocked(line):
    assert _lines(line) == [1], line


@pytest.mark.parametrize(
    "line",
    [
        f"https://github.com/{_TARGET}/pull/12", f"{_TARGET}#12",
        f"https://api.github.com/repos/{_TARGET}/issues/12", f"Clone https://github.com/{_TARGET}.git to start.",
        f"git@github.com:{_TARGET}.git", f"See {_TARGET}.", f"See {_TARGET}, then run it.",
        f"(https://github.com/{_TARGET}).", f"github.com/{_TARGET}", f"Is it {_TARGET}?",
        # github.com pages that name no repository
        "https://github.com/settings/tokens", "https://github.com/orgs/other-owner/people",
        "https://github.com/features/actions", "https://github.com/marketplace/actions/x",
        "https://github.com/sponsors/someone", "https://github.com/apps/some-bot",
        "https://github.com/topics/python", "https://github.com/login/oauth",
        "https://docs.github.com/en/actions/using-workflows",
        "See skills/ci-speedup after internal design and review.",
    ],
)
def test_destination_and_non_repository_forms_are_allowed(line):
    assert _lines(line) == [], line


def test_allow_only_covers_the_named_repository():
    other = "/".join(("third-owner", "another-project"))
    text = f"{_LINK}https://github.com/{other}\n"
    assert _lines(text, allowed=frozenset({_FOREIGN})) == [2]


def test_added_lines_parser_tracks_paths_hunks_and_double_plus_content():
    diff = "\n".join(
        [
            "diff --git a/notes.md b/notes.md",
            "index 1111111..2222222 100644",
            "--- a/notes.md",
            "+++ b/notes.md",
            "@@ -3,0 +4,2 @@ heading",
            "+++literal content",
            "+--more content",
            "@@ -9 +10,0 @@",
            "-removed",
            "diff --git a/other.md b/other.md",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/other.md",
            "@@ -0,0 +1 @@",
            "+first",
            "\\ No newline at end of file",
        ]
    )
    assert checker._added_lines(diff) == [
        ("notes.md", 4, "++literal content"),
        ("notes.md", 5, "--more content"),
        ("other.md", 1, "first"),
    ]


# --- main(): text files, visibility, fail-closed errors ---------------------------


def _main(*args):
    return checker.main(["--target", _TARGET, *args])


def _fake_gh(monkeypatch, visibility, returncode=0):
    viewed = []

    def run(cmd, **_kwargs):
        viewed.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, visibility(cmd[3]), "")

    monkeypatch.setattr(checker.subprocess, "run", run)
    return viewed


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setattr(checker, "_verify_public_target", lambda *_a, **_k: None)


def test_blocked_text_echoes_neither_identifier_nor_filename(public, capsys, tmp_path):
    payload = tmp_path / "internal-project-body.md"
    payload.write_text(f"Derived from {_SAME_OWNER}#91\n", encoding="utf-8")
    assert _main("--text-file", str(payload)) == 1
    stderr = capsys.readouterr().err
    assert "cross-repository" in stderr and "text file 1:1" in stderr
    assert _SAME_OWNER not in stderr and payload.name not in stderr


def test_unreadable_input_file_is_not_echoed(public, capsys, tmp_path):
    assert _main("--text-file", str(tmp_path / "internal-project-body.md")) == 2
    stderr = capsys.readouterr().err
    assert "could not read text file 1" in stderr
    assert "internal-project" not in stderr and str(tmp_path) not in stderr


@pytest.mark.parametrize(
    "args", [["--target", "not-a-repository", "--base", "main"], ["--target", _TARGET],
             ["--target", _TARGET, "--base", "main", "--allow", "not-a-repository"]],
)
def test_invalid_invocation_fails_closed(args, capsys):
    assert checker.main(args) == 2
    assert "privacy preflight error" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected", "message"),
    [
        ("PUBLIC\n", 0, 0, ""),
        ("PRIVATE\n", 0, 2, "not verified PUBLIC"),
        ("INTERNAL\n", 0, 2, "not verified PUBLIC"),
        ("", 0, 2, "not verified PUBLIC"),
        ("PUBLIC\n", 1, 2, "could not verify destination repository visibility"),
    ],
)
def test_visibility_check_parses_gh_output(
    monkeypatch, capsys, tmp_path, stdout, returncode, expected, message
):
    payload = tmp_path / "body.md"
    payload.write_text("neutral payload\n", encoding="utf-8")
    viewed = _fake_gh(monkeypatch, lambda _repo: stdout, returncode)
    assert _main("--text-file", str(payload)) == expected
    assert viewed[0][:4] == ["gh", "repo", "view", _TARGET]
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(("visibility", "expected"), [("PUBLIC\n", 0), ("PRIVATE\n", 2)])
def test_allowed_repository_must_verify_public(
    monkeypatch, capsys, tmp_path, visibility, expected
):
    payload = tmp_path / "body.md"
    payload.write_text(f"Upstream: {_LINK}", encoding="utf-8")
    viewed = _fake_gh(
        monkeypatch, lambda repo: "PUBLIC\n" if repo == _TARGET else visibility
    )
    assert _main("--text-file", str(payload), "--allow", _FOREIGN) == expected
    assert [cmd[3] for cmd in viewed] == [_TARGET, _FOREIGN]
    stderr = capsys.readouterr().err
    assert _FOREIGN not in stderr
    if expected:
        assert "allowed repository 1 is not verified PUBLIC" in stderr


@pytest.mark.parametrize("tool", ["gh", "git"])
def test_missing_tool_is_named(tool, monkeypatch, capsys, tmp_path):
    payload = tmp_path / "body.md"
    payload.write_text("neutral payload\n", encoding="utf-8")
    if tool == "git":
        monkeypatch.setattr(checker, "_verify_public_target", lambda *_a, **_k: None)

    def missing(cmd, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(checker.subprocess, "run", missing)
    args = ["--text-file", str(payload)] if tool == "gh" else ["--base", "origin/main"]
    assert _main(*args) == 2
    stderr = capsys.readouterr().err
    assert f"{tool} not installed" in stderr and "could not read" not in stderr


# --- --base against real temporary git repositories -------------------------------


def _git(*args):
    return subprocess.run(
        [
            "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args,
        ],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(message, files):
    for name, content in files.items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            path.unlink()
        else:
            path.write_text(content, encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-q", "--allow-empty", "-m", message)
    return _git("rev-parse", "--short", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch, public):
    """A git repo isolated from the caller's git env (hooks export GIT_DIR...) and config."""
    for var in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX",
    ):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    _git("init", "-q", "-b", "main")
    _commit("base", {"README.md": "neutral\n"})
    return root


_FIXTURES = {
    path: _LINK
    for path in (
        "tests/test_x.py", "skills/demo/tests/test_y.py",
        "skills/demo/evals/case.json", "maintainers/demo/tests/test_z.py",
    )
}

# (on-main commits, feature commits, exit code, expected locations, forbidden text).
# Locations use {0}, {1}... for the feature commits' short SHAs.
_HISTORY_CASES = {
    "plus-plus-line-is-scanned": (
        [], [("add notes", {"notes.md": f"++https://github.com/{_FOREIGN}/pull/91\n"})],
        1, ["commit {0} notes.md:1", "added diff:notes.md:1"], [],
    ),
    "added-then-removed-is-blocked-per-commit": (
        [], [("add notes", {"notes.md": f"see {_FOREIGN}#91\n"}),
             ("tidy notes", {"notes.md": "see the upstream report\n"})],
        1, ["commit {0} notes.md:1"], ["added diff:"],
    ),
    "commit-message-is-scanned-per-commit": (
        [], [(f"fix parser\n\nPorted from https://github.com/{_FOREIGN}.", {"a.md": "x\n"})],
        1, ["commit {0} message:3"], [],
    ),
    "fixture-exclusion-does-not-cover-messages": (
        [], [(f"add fixture for {_FOREIGN}#9", {"tests/test_x.py": "x = 1\n"})],
        1, ["commit {0} message:1"], [],
    ),
    "fixture-paths-are-excluded-from-the-diff": (
        [], [("add fixtures", _FIXTURES)], 0, [], [],
    ),
    "non-fixture-path-is-not-excluded": (
        [], [("add fixtures", _FIXTURES), ("add doc", {"docs/guide.md": _LINK})],
        1, ["commit {1} docs/guide.md:1"], ["tests/", "evals/"],
    ),
    "removed-only-reference-passes": (
        [("seed", {"old.md": _LINK})], [("drop old link", {"old.md": None})], 0, [], [],
    ),
}


@pytest.mark.parametrize("case", list(_HISTORY_CASES), ids=list(_HISTORY_CASES))
def test_base_scans_outgoing_history(repo, capsys, case):
    on_main, feature, code, expected, forbidden = _HISTORY_CASES[case]
    for message, files in on_main:
        _commit(message, files)
    _git("checkout", "-q", "-b", "feature")
    shas = [_commit(message, files) for message, files in feature]
    assert _main("--base", "main") == code
    stderr = capsys.readouterr().err
    if code == 0:
        assert stderr == ""
    for location in expected:
        assert location.format(*shas) in stderr
    for text in [_FOREIGN, *forbidden]:
        assert text not in stderr


def test_unknown_base_fails_closed(repo, capsys):
    assert _main("--base", "no-such-ref") == 2
    assert "privacy preflight error: git" in capsys.readouterr().err


# --- docs and CI wiring ------------------------------------------------------------


def _section(name):
    text = (_ROOT / name).read_text(encoding="utf-8")
    heading = "## Public-output privacy gate\n"
    assert text.count(heading) == 1, f"{name} must have exactly one gate section"
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def test_agents_md_holds_the_policy_and_claude_md_points_to_it():
    agents = _section("AGENTS.md")
    assert "python3 .github/scripts/check_public_output.py" in agents
    assert "--base origin/main" in agents and "mktemp" in agents
    assert "--allow" in agents and "PUBLIC" in agents and "explicitly approve" in agents
    assert "Fetch the published artifact back" in agents
    assert "--branch-name" not in agents
    claude = _section("CLAUDE.md")
    assert "AGENTS.md" in claude and '"Public-output privacy gate"' in claude
    assert len(claude.strip().splitlines()) <= 4, "CLAUDE.md keeps only a pointer"


def test_ci_helper_reads_pr_text_only_from_env():
    helper = (_SCRIPTS / "run_public_output_gate.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in helper and "${{" not in helper
    assert helper.count("mktemp") == 2
    assert "\"$PR_TITLE\"" in helper and "\"$PR_BODY\"" in helper
    assert "check_public_output.py" in helper
    for flag in ('--target "$GITHUB_REPOSITORY"', "--text-file", '--base "origin/$BASE_REF"'):
        assert flag in helper


@pytest.mark.parametrize("job_id", ["test-self", "test-fork"])
def test_ci_runs_the_gate_once_per_suite_job(job_id):
    """Inside both suite jobs, the gate gates the required `test` check on
    same-repo and fork PRs alike instead of being a voluntary step."""
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    steps = workflow["jobs"][job_id]["steps"]
    gates = [i for i, s in enumerate(steps) if "public_output" in str(s.get("run", ""))]
    assert len(gates) == 1, f"{job_id} must run the privacy gate exactly once"
    assert gates[0] < len(steps) - 1, "the gate must run before the suite step"
    gate = steps[gates[0]]
    assert str(gate.get("if", "")).strip() == "github.event_name == 'pull_request'"
    # PR text is attacker-controlled: it may reach the shell only via env vars.
    assert gate["run"] == "bash .github/scripts/run_public_output_gate.sh"
    assert gate.get("env") == {
        "GH_TOKEN": "${{ github.token }}",
        "BASE_REF": "${{ github.base_ref }}",
        "PR_TITLE": "${{ github.event.pull_request.title }}",
        "PR_BODY": "${{ github.event.pull_request.body }}",
    }

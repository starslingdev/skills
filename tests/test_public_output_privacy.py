from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / ".github" / "scripts" / "check_public_output.py"
_PUBLIC_TARGET = "/".join(("example", "public-project"))


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_public_output", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def test_target_repository_references_are_allowed():
    text = "\n".join(
        [
            f"https://github.com/{_PUBLIC_TARGET}/pull/12",
            f"https://api.github.com/repos/{_PUBLIC_TARGET}/issues/12",
            f"{_PUBLIC_TARGET}#12",
        ]
    )
    assert checker.scan_text(
        text, source="payload", target=_PUBLIC_TARGET
    ) == []


def test_cross_repository_references_are_blocked_without_echo(
    capsys, monkeypatch, tmp_path
):
    private_reference = "/".join(("example", "internal-project#91"))
    payload = tmp_path / "body.md"
    payload.write_text(f"Derived from {private_reference}\n", encoding="utf-8")

    monkeypatch.setattr(checker, "_verify_public_target", lambda _target: None)
    assert checker.main(
        ["--target", _PUBLIC_TARGET, "--text-file", str(payload)]
    ) == 1
    stderr = capsys.readouterr().err
    assert "cross-repository" in stderr
    assert private_reference not in stderr


def test_sensitive_input_filename_is_not_echoed(capsys, monkeypatch, tmp_path):
    private_slug = "/".join(("example", "internal-project"))
    sensitive_name = "internal-project-body.md"
    payload = tmp_path / sensitive_name
    payload.write_text(private_slug, encoding="utf-8")
    monkeypatch.setattr(checker, "_verify_public_target", lambda _target: None)

    assert checker.main(
        ["--target", _PUBLIC_TARGET, "--text-file", str(payload)]
    ) == 1
    stderr = capsys.readouterr().err
    assert "text file 1:1" in stderr
    assert sensitive_name not in stderr


def test_cross_repository_web_and_api_urls_are_blocked():
    private_slug = "/".join(("example", "internal-project"))
    text = "\n".join(
        [
            f"https://github.com/{private_slug}/pull/91",
            f"https://api.github.com/repos/{private_slug}/issues/91",
        ]
    )
    findings = checker.scan_text(
        text, source="payload", target=_PUBLIC_TARGET
    )
    assert [finding.line for finding in findings] == [1, 2]


# A DIFFERENT owner than the target: the same-owner bare-slug rule cannot mask
# a URL pattern that fails to match.
_FOREIGN = "/".join(("other-owner", "private-project"))


@pytest.mark.parametrize(
    "line",
    [
        f"See https://github.com/{_FOREIGN} for context",
        f"(https://github.com/{_FOREIGN})",
        f"[context](https://github.com/{_FOREIGN})",
        f"`https://github.com/{_FOREIGN}`",
        f"<https://github.com/{_FOREIGN}>",
        f"[https://github.com/{_FOREIGN}]",
        f'"https://github.com/{_FOREIGN}",',
        f"'https://github.com/{_FOREIGN}'",
        f"https://github.com/{_FOREIGN}, then more",
        f"https://github.com/{_FOREIGN}.",
        f"https://github.com/{_FOREIGN}/pull/91",
        f"github.com/{_FOREIGN}",
        f"www.github.com/{_FOREIGN}",
        f"https://www.github.com/{_FOREIGN} here",
        f"git@github.com:{_FOREIGN}.git",
        f"git@github.com:{_FOREIGN}",
        f"https://api.github.com/repos/{_FOREIGN} now",
        f"https://api.github.com/repos/{_FOREIGN})",
        f"{_FOREIGN}#91",
    ],
)
def test_foreign_repository_links_are_blocked_in_ordinary_prose(line):
    findings = checker.scan_text(line, source="payload", target=_PUBLIC_TARGET)
    assert [finding.line for finding in findings] == [1], line


@pytest.mark.parametrize(
    "line",
    [
        f"Clone https://github.com/{_PUBLIC_TARGET}.git to start.",
        f"git@github.com:{_PUBLIC_TARGET}.git",
        f"See {_PUBLIC_TARGET}.",
        f"See {_PUBLIC_TARGET}, then run it.",
        f"(https://github.com/{_PUBLIC_TARGET}).",
        f"github.com/{_PUBLIC_TARGET}",
        f"Is it {_PUBLIC_TARGET}?",
    ],
)
def test_destination_repository_forms_are_allowed(line):
    assert checker.scan_text(line, source="payload", target=_PUBLIC_TARGET) == [], line


@pytest.mark.parametrize(
    "line",
    [
        "https://github.com/settings/tokens",
        "https://github.com/orgs/other-owner/people",
        "https://github.com/features/actions",
        "https://github.com/marketplace/actions/some-action",
        "https://github.com/sponsors/someone",
        "https://github.com/apps/some-bot",
        "https://github.com/topics/python",
        "https://github.com/login/oauth",
        "https://docs.github.com/en/actions/using-workflows",
    ],
)
def test_non_repository_github_pages_are_allowed(line):
    assert checker.scan_text(line, source="payload", target=_PUBLIC_TARGET) == [], line


def test_bare_repository_slug_in_target_organization_is_blocked():
    private_slug = "/".join(("example", "internal-project"))
    findings = checker.scan_text(
        f"Provenance: {private_slug}",
        source="payload",
        target=_PUBLIC_TARGET,
    )
    assert [finding.line for finding in findings] == [1]


def test_neutral_paths_and_generic_provenance_are_allowed():
    text = "See skills/ci-speedup after internal design and review."
    assert checker.scan_text(
        text, source="payload", target=_PUBLIC_TARGET
    ) == []


def test_invalid_or_empty_invocation_fails_closed(capsys):
    assert checker.main(["--target", "not-a-repository", "--base", "main"]) == 2
    assert checker.main(["--target", _PUBLIC_TARGET]) == 2
    assert "privacy preflight error" in capsys.readouterr().err


def test_unverified_visibility_fails_closed(capsys, monkeypatch, tmp_path):
    payload = tmp_path / "body.md"
    payload.write_text("neutral payload\n", encoding="utf-8")

    def reject(_target):
        raise RuntimeError("destination repository is not verified PUBLIC")

    monkeypatch.setattr(checker, "_verify_public_target", reject)
    assert checker.main(
        ["--target", _PUBLIC_TARGET, "--text-file", str(payload)]
    ) == 2
    assert "not verified PUBLIC" in capsys.readouterr().err


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _neutral_payload(tmp_path):
    payload = tmp_path / "body.md"
    payload.write_text("neutral payload\n", encoding="utf-8")
    return payload


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("PUBLIC\n", 0, 0),
        ("PRIVATE\n", 0, 2),
        ("INTERNAL\n", 0, 2),
        ("", 0, 2),
        ("PUBLIC\n", 1, 2),
    ],
)
def test_visibility_check_parses_gh_output(
    monkeypatch, tmp_path, stdout, returncode, expected
):
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _completed(stdout, returncode)

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    assert checker.main(
        ["--target", _PUBLIC_TARGET, "--text-file", str(_neutral_payload(tmp_path))]
    ) == expected
    assert calls[0][:4] == ["gh", "repo", "view", _PUBLIC_TARGET]


def test_missing_gh_reports_gh_not_installed(capsys, monkeypatch, tmp_path):
    def missing(cmd, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(checker.subprocess, "run", missing)
    assert checker.main(
        ["--target", _PUBLIC_TARGET, "--text-file", str(_neutral_payload(tmp_path))]
    ) == 2
    stderr = capsys.readouterr().err
    assert "gh not installed" in stderr
    assert "could not read an input file" not in stderr


def test_missing_git_reports_git_not_installed(capsys, monkeypatch):
    monkeypatch.setattr(checker, "_verify_public_target", lambda _target: None)

    def missing(cmd, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(checker.subprocess, "run", missing)
    assert checker.main(["--target", _PUBLIC_TARGET, "--base", "origin/main"]) == 2
    stderr = capsys.readouterr().err
    assert "git not installed" in stderr
    assert "could not read an input file" not in stderr


def test_unreadable_input_file_is_not_echoed(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(checker, "_verify_public_target", lambda _target: None)
    sensitive_name = "internal-project-body.md"
    assert checker.main(
        ["--target", _PUBLIC_TARGET, "--text-file", str(tmp_path / sensitive_name)]
    ) == 2
    stderr = capsys.readouterr().err
    assert "could not read text file 1" in stderr
    assert sensitive_name not in stderr
    assert str(tmp_path) not in stderr


def test_allowed_public_repository_passes(monkeypatch, tmp_path):
    payload = tmp_path / "body.md"
    payload.write_text(f"Upstream: https://github.com/{_FOREIGN}\n", encoding="utf-8")
    viewed = []

    def fake_run(cmd, **_kwargs):
        viewed.append(cmd[3])
        return _completed("PUBLIC\n")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    assert checker.main(
        [
            "--target", _PUBLIC_TARGET, "--text-file", str(payload),
            "--allow", _FOREIGN,
        ]
    ) == 0
    assert viewed == [_PUBLIC_TARGET, _FOREIGN]


def test_allowed_private_repository_fails_closed(capsys, monkeypatch, tmp_path):
    payload = tmp_path / "body.md"
    payload.write_text(f"Upstream: https://github.com/{_FOREIGN}\n", encoding="utf-8")

    def fake_run(cmd, **_kwargs):
        return _completed("PUBLIC\n" if cmd[3] == _PUBLIC_TARGET else "PRIVATE\n")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    assert checker.main(
        [
            "--target", _PUBLIC_TARGET, "--text-file", str(payload),
            "--allow", _FOREIGN,
        ]
    ) == 2
    stderr = capsys.readouterr().err
    assert "allowed repository 1 is not verified PUBLIC" in stderr
    assert _FOREIGN not in stderr


def test_allow_only_covers_the_named_repository(monkeypatch):
    other = "/".join(("third-owner", "another-project"))
    monkeypatch.setattr(checker, "_verify_public_target", lambda _target: None)
    findings = checker.scan_text(
        f"https://github.com/{_FOREIGN}\nhttps://github.com/{other}\n",
        source="payload",
        target=_PUBLIC_TARGET,
        allowed=frozenset({_FOREIGN}),
    )
    assert [finding.line for finding in findings] == [2]


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


# --- real temporary git repositories ------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An isolated git repository the checker runs inside.

    Isolated from the caller's git environment (pre-commit hooks and worktrees
    export GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE) and from global config.
    """
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
    monkeypatch.setattr(checker, "_verify_public_target", lambda _target: None)
    return root


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


def _run_base(capsys, *extra):
    code = checker.main(["--target", _PUBLIC_TARGET, "--base", "main", *extra])
    return code, capsys.readouterr().err


def test_added_line_starting_with_plus_plus_is_scanned(repo, capsys):
    _git("checkout", "-q", "-b", "feature")
    sha = _commit("add notes", {"notes.md": f"++https://github.com/{_FOREIGN}/pull/91\n"})
    code, stderr = _run_base(capsys)
    assert code == 1
    assert f"commit {sha} notes.md:1" in stderr
    assert "added diff:notes.md:1" in stderr
    assert _FOREIGN not in stderr


def test_reference_added_then_removed_in_a_later_commit_is_blocked(repo, capsys):
    _git("checkout", "-q", "-b", "feature")
    leaked = _commit("add notes", {"notes.md": f"see {_FOREIGN}#91\n"})
    _commit("tidy notes", {"notes.md": "see the upstream report\n"})
    code, stderr = _run_base(capsys)
    assert code == 1
    assert f"commit {leaked} notes.md:1" in stderr
    assert "added diff:" not in stderr


def test_commit_message_reference_is_blocked_per_commit(repo, capsys):
    _git("checkout", "-q", "-b", "feature")
    sha = _commit(f"fix parser\n\nPorted from https://github.com/{_FOREIGN}.", {"a.md": "x\n"})
    code, stderr = _run_base(capsys)
    assert code == 1
    assert f"commit {sha} message:3" in stderr
    assert _FOREIGN not in stderr


def test_removed_only_reference_passes(repo, capsys):
    _commit("seed", {"old.md": f"see https://github.com/{_FOREIGN}\n"})
    _git("checkout", "-q", "-b", "feature")
    _commit("drop old link", {"old.md": None})
    code, stderr = _run_base(capsys)
    assert (code, stderr) == (0, "")


def test_unknown_base_fails_closed(repo, capsys):
    code, stderr = checker.main(
        ["--target", _PUBLIC_TARGET, "--base", "no-such-ref"]
    ), capsys.readouterr().err
    assert code == 2
    assert "privacy preflight error: git" in stderr


def test_git_output_nonzero_exit_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(checker, "_verify_public_target", lambda _target: None)
    monkeypatch.setattr(
        checker.subprocess, "run", lambda cmd, **_kwargs: _completed("", 128)
    )
    assert checker.main(["--target", _PUBLIC_TARGET, "--base", "main"]) == 2
    assert "privacy preflight error: git" in capsys.readouterr().err


def test_fixture_paths_are_excluded_from_the_diff_scan_only(repo, capsys):
    _git("checkout", "-q", "-b", "feature")
    link = f"https://github.com/{_FOREIGN}\n"
    _commit(
        "add fixtures",
        {
            "tests/test_x.py": link,
            "skills/demo/tests/test_y.py": link,
            "skills/demo/evals/case.json": link,
            "maintainers/demo/tests/test_z.py": link,
        },
    )
    assert _run_base(capsys) == (0, "")
    sha = _commit("add doc", {"docs/guide.md": link})
    code, stderr = _run_base(capsys)
    assert code == 1
    assert f"commit {sha} docs/guide.md:1" in stderr
    assert "tests/" not in stderr and "evals/" not in stderr


def test_fixture_exclusion_does_not_cover_commit_messages(repo, capsys):
    _git("checkout", "-q", "-b", "feature")
    sha = _commit(f"add fixture for {_FOREIGN}#9", {"tests/test_x.py": "x = 1\n"})
    code, stderr = _run_base(capsys)
    assert code == 1
    assert f"commit {sha} message:1" in stderr


def test_branch_name_is_scanned_automatically_with_base(repo, capsys):
    _git("checkout", "-q", "-b", "port/" + "/".join(("example", "internal-project")))
    _commit("neutral", {"a.md": "x\n"})
    code, stderr = _run_base(capsys)
    assert code == 1
    assert "branch name:1" in stderr


def test_explicit_branch_name_overrides_detection(repo, capsys):
    _git("checkout", "-q", "-b", "feature")
    _commit("neutral", {"a.md": "x\n"})
    assert _run_base(capsys, "--branch-name", "policy/privacy-gate") == (0, "")
    code, stderr = _run_base(
        capsys, "--branch-name", "port/" + "/".join(("example", "internal-project"))
    )
    assert code == 1
    assert "branch name:1" in stderr


def test_shared_agent_instructions_require_the_gate_and_readback():
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert "check_public_output.py" in text
        assert "explicit" in text
        assert "PUBLIC" in text
        assert "fetch" in text.lower()

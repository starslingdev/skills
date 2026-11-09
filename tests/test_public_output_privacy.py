from __future__ import annotations

import importlib.util
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


def test_base_scans_commit_messages_and_only_added_diff(monkeypatch, tmp_path):
    payload = tmp_path / "body.md"
    payload.write_text("neutral payload\n", encoding="utf-8")
    monkeypatch.setattr(checker, "_verify_public_target", lambda _target: None)

    def fake_git(args):
        if args[0] == "log":
            return "neutral commit message\n"
        return "\n".join(
            [
                "--- a/file.md",
                "+++ b/file.md",
                "-" + "/".join(("old-owner", "old-project#1")),
                "+" + "/".join(("example", "internal-project#91")),
            ]
        )

    monkeypatch.setattr(checker, "_git_output", fake_git)
    assert checker.main(
        [
            "--target", _PUBLIC_TARGET, "--text-file", str(payload),
            "--base", "origin/main",
        ]
    ) == 1


def test_shared_agent_instructions_require_the_gate_and_readback():
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert "check_public_output.py" in text
        assert "explicit" in text
        assert "PUBLIC" in text
        assert "fetch" in text.lower()

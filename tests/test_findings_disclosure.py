"""Disclosure guard for committed ci-secure findings about third-party repos.

A ci-secure report names live security holes. Committing one about a repository
we do not own publishes those holes permanently and searchably, and a maintainer
has to have thought about disclosure BEFORE the file lands. The rule is written
down in `maintainers/ci-secure/MAINTAINERS.md`, but prose in a maintainers
directory enforces nothing: the person who would break it is exactly the person
who has not read it.

This guard makes it mechanical. Every committed ci-secure findings artifact that
reports at least one finding must name its target repository in
`DISCLOSED_TARGETS` below, together with the public disclosure it relies on.
Adding an entry is a deliberate edit that shows up in review, which is the whole
point: the check does not decide whether publishing is acceptable, it forces
somebody to say so on the record.

Scope. It reads the machine-readable findings JSON, not the rendered markdown,
because the renderer is derived from the JSON: a finding cannot reach a `.md`
without appearing here first. It walks the files git has under version control,
so a maintainer's gitignored local scan output (`.ci-speedup-gaps/` and friends,
which may hold third-party findings by design) is never read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Repositories whose committed ci-secure findings are cleared for publication,
# keyed by `owner/name`, each with the disclosure that clears it. A repository we
# own needs no disclosure and records that as its reason instead.
#
# Adding a line here IS the decision. Make it deliberately: the finding must
# already be public, or already fixed, or ours.
DISCLOSED_TARGETS: dict[str, str] = {
    "snowflakedb/snowflake-connector-net": (
        "Template injection in jira_issue.yml, found by Wiz Research, reported "
        "via HackerOne 2026-06-23, fixed by the vendor the same day in 1dc7766. "
        "Public write-up: "
        "https://www.wiz.io/blog/red-agent-snowflake-copilot-cicd-bug"
    ),
    "starslingdev/skills": "Our own repository.",
}

# ci-secure catalog ids look like `P14.10`; ci-speedup's look like `OPT32`.
_CI_SECURE_PATTERN_RE = re.compile(r"^P\d+\.\d+$")


def _cleared_tokens() -> set[str]:
    """Every spelling of a cleared repository the guard may encounter.

    ci-secure scans a local checkout and records `"repo": null`, so for the
    shipped examples the only identifier in the artifact is its directory name —
    the slug with `/` written as `-` (`snowflakedb-snowflake-connector-net`).
    Both spellings are accepted; neither can clear anything a maintainer did not
    write into `DISCLOSED_TARGETS` by hand.
    """
    tokens = set()
    for slug in DISCLOSED_TARGETS:
        tokens.add(slug)
        tokens.add(slug.replace("/", "-"))
    return tokens


def _git_env() -> dict[str, str]:
    """The ambient environment with every GIT_* override stripped.

    A worktree or hook run can export an absolute GIT_DIR / GIT_WORK_TREE, which
    would make a `git` call inside a temporary fixture repo operate on the real
    repository instead.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _tracked_json(root: Path) -> list[Path] | None:
    """Every version-controlled `*.json` under `root`, or None if git can't say."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.json"],
            capture_output=True, env=_git_env(), timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
    return sorted(root / n for n in names)


def _candidate_json(root: Path) -> list[Path]:
    """The artifacts to inspect: tracked JSON, or `examples/` if git can't answer.

    The fallback keeps the guard from silently passing on a tarball export with
    no git metadata. `examples/` is never gitignored, so reading it there cannot
    pull in local scan output.
    """
    tracked = _tracked_json(root)
    if tracked is not None:
        return tracked
    return sorted((root / "examples").rglob("*.json"))


def _load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _security_findings(payload: object) -> list[dict]:
    """The ci-secure findings in a parsed artifact, or [].

    Discrimination matters more than it looks. Keying on a finding carrying
    `pattern` + `severity` — the obvious choice — also matches every ci-speedup
    artifact, whose findings carry both; the guard would then demand an allowlist
    entry for `microsoft/playwright` and `pallets/flask` and the list would stop
    meaning "cleared for disclosure". Two signals identify ci-secure instead, and
    either alone is enough, so an artifact cannot dodge the guard by dropping
    one:

    * a top-level `catalog_patterns_evaluated` key, which ci-secure writes and
      ci-speedup (`catalog_patterns_total` and friends) does not; and
    * catalog ids of the form `P14.10`, which ci-speedup's `OPT32` never match.
    """
    if not isinstance(payload, dict):
        return []
    findings = payload.get("findings")
    if not isinstance(findings, list):
        findings = []
    findings = [f for f in findings if isinstance(f, dict)]
    by_key = "catalog_patterns_evaluated" in payload
    by_pattern = any(
        isinstance(f.get("pattern"), str) and _CI_SECURE_PATTERN_RE.match(f["pattern"])
        for f in findings
    )
    return findings if (by_key or by_pattern) else []


def _declared_target(payload: dict, artifact: Path) -> str:
    """The repository an artifact reports on.

    Prefers the recorded `repo` slug; falls back to the artifact's directory
    name, which is what a local-checkout scan leaves behind. An artifact can
    therefore never dodge the guard by omitting the field — the fallback is
    a name that has to be cleared too.
    """
    repo = payload.get("repo")
    if isinstance(repo, str) and "/" in repo:
        return repo
    return artifact.parent.name


def _offenders(root: Path) -> list[str]:
    """Committed artifacts reporting findings against an uncleared repository."""
    cleared = _cleared_tokens()
    offenders: list[str] = []
    for artifact in _candidate_json(root):
        payload = _load(artifact)
        findings = _security_findings(payload)
        if not findings:
            continue
        target = _declared_target(payload, artifact)
        if target in cleared:
            continue
        patterns = sorted({str(f.get("pattern")) for f in findings})
        try:
            where = artifact.relative_to(root)
        except ValueError:  # pragma: no cover - defensive
            where = artifact
        offenders.append(
            f"{where} reports {len(findings)} finding(s) "
            f"({', '.join(patterns)}) against {target!r}, which is not in "
            f"DISCLOSED_TARGETS"
        )
    return offenders


def test_committed_findings_only_name_disclosed_targets() -> None:
    """No committed artifact reports a security finding about an uncleared repo."""
    offenders = _offenders(REPO_ROOT)
    assert not offenders, (
        "Committed ci-secure findings about a repository with no recorded "
        "disclosure:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither the finding is already public, already fixed, or ours — in "
        "which case add the repository to DISCLOSED_TARGETS with the disclosure "
        "that clears it — or it is not, in which case it must not be committed. "
        "See maintainers/ci-secure/MAINTAINERS.md."
    )


def test_the_shipped_ci_secure_example_is_actually_covered() -> None:
    """The guard sees the real example, so the test above is not vacuously green.

    Without this, deleting the examples, breaking the walk, or breaking the
    discriminator would all read exactly like a clean repository.
    """
    artifact = (
        REPO_ROOT / "examples" / "snowflakedb-snowflake-connector-net"
        / "ci-secure-findings-4a1b8ce.json"
    )
    assert artifact.exists(), f"{artifact} is missing"
    assert artifact in _candidate_json(REPO_ROOT), "the walk does not reach it"
    findings = _security_findings(_load(artifact))
    assert findings, "the discriminator does not recognise a real ci-secure artifact"
    assert _declared_target(_load(artifact), artifact) in _cleared_tokens()


def test_the_guard_can_actually_fail(tmp_path: Path) -> None:
    """Positive control: run the real walk over a repo holding an uncleared artifact.

    A guard that cannot fail is not a guard. This builds a throwaway git
    repository with one committed ci-secure artifact about a repository nobody
    cleared, and asserts the same `_offenders` the test above trusts reports it.
    """
    root = tmp_path / "fixture-repo"
    target_dir = root / "examples" / "someone-else-private-thing"
    target_dir.mkdir(parents=True)
    artifact = target_dir / "ci-secure-findings-deadbee.json"
    artifact.write_text(
        json.dumps({
            "repo": None,
            "catalog_patterns_evaluated": ["P14.10"],
            "findings": [{"pattern": "P14.10", "severity": "HIGH", "line": 24}],
        }),
        encoding="utf-8",
    )
    env = _git_env()
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "add", str(artifact.relative_to(root))],
        check=True, env=env,
    )

    assert _tracked_json(root) == [artifact], "the fixture repo was not set up"
    offenders = _offenders(root)
    assert len(offenders) == 1, offenders
    assert "someone-else-private-thing" in offenders[0]
    assert "P14.10" in offenders[0]


def test_the_guard_fires_on_pattern_ids_alone(tmp_path: Path) -> None:
    """Second control: the `P<n>.<n>` signal works without the top-level key.

    The two signals are an OR precisely so dropping one cannot buy silence.
    """
    payload = {
        "repo": "someone-else/private-thing",
        "findings": [{"pattern": "P4.2", "severity": "MEDIUM"}],
    }
    assert _security_findings(payload), "the pattern-id signal did not fire"
    assert _declared_target(payload, tmp_path / "x.json") not in _cleared_tokens()


def test_the_other_engines_artifacts_are_not_swept_in() -> None:
    """A committed ci-speedup or ci-score artifact is not a security finding.

    Pinned against the real shipped examples, not a synthetic shape, because the
    false positive this prevents was found on exactly these files.
    """
    others = sorted(
        p for p in (REPO_ROOT / "examples").rglob("*.json")
        if "ci-secure" not in p.name
    )
    assert others, "no non-ci-secure example artifacts found to check against"
    swept = [
        str(p.relative_to(REPO_ROOT)) for p in others if _security_findings(_load(p))
    ]
    assert not swept, f"non-ci-secure artifacts treated as security findings: {swept}"


@pytest.mark.parametrize("repo", sorted(DISCLOSED_TARGETS))
def test_every_allowlist_entry_states_its_reason(repo: str) -> None:
    """An entry with no stated reason is an unreviewed entry."""
    assert "/" in repo, f"{repo!r} is not an owner/name slug"
    assert DISCLOSED_TARGETS[repo].strip(), f"{repo} records no reason"

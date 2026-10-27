"""Checks that passed on workflow content the scan never read, and the
workflow lockfile credit on the pinning check.

1. A same-repository reusable workflow (`on: workflow_call`) called from a
   pull-request workflow runs on every pull request, but only workflows
   triggered by `pull_request` were graded on the PR path: a callee that
   checks out full history passed the shallow-checkout check.
2. Local composite actions were read only from `.github/actions/**`, so an
   action at `./ci/setup` or `$/tools/x` was invisible: its cache step did
   not count (a wrong fail) and its unpinned actions were left out of the
   pinning count (a wrong pass).
3. A job-level call to another repository's reusable workflow
   (`jobs.x.uses: org/repo/.github/workflows/ci.yml@main`) was never counted
   in the pinning check.
4. GitHub's workflow lockfile (`.github/workflows/actions.lock`, public
   preview 2026-10-14) records the exact commit each action runs at while
   the YAML keeps tags. A reference with a valid lockfile entry under its
   own workflow now counts as pinned; a malformed lockfile grants nothing.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


cc_mod = _load("ci_score_collect_config_undeserved", "scripts/collect_config.py")

SHA = "8f4b7f84864484a7bf31766abe9204da3cbe65b3"
SHA2 = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SHALLOW = "ci.checkout.shallow-clone"
PINNED = "ci.security.pinned-action-shas"
CACHE = "ci.cache.dependency-cache"
LOCK = ".github/workflows/actions.lock"


def _y(text: str) -> str:
    return textwrap.dedent(text).lstrip()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(root)})


def _collect(tmp_path: Path, files: dict[str, str]) -> dict:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(_y(text))
        _git(root, "add", rel)
    _git(root, "commit", "-qm", "init")
    doc, code = cc_mod.collect(root)
    assert code == 0, doc
    return doc


def _fact(doc: dict, check: str) -> dict:
    return doc["practice_facts"][check]


# ---- 1. local reusable workflows on the PR path ------------------------------

CALLER = """
    on: pull_request
    jobs:
      test:
        uses: ./.github/workflows/test.yml
    """

FULL_HISTORY_CALLEE = f"""
    on: workflow_call
    jobs:
      unit:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@{SHA}
            with: {{fetch-depth: 0}}
          - run: pytest
    """


def test_a_reusable_workflow_called_from_a_pr_workflow_is_graded(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": CALLER,
                              ".github/workflows/test.yml": FULL_HISTORY_CALLEE})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "fail", got
    assert got["files"] == [".github/workflows/test.yml"], got


def test_a_self_repository_call_is_followed_too(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": CALLER.replace("./.github", "$/.github"),
        ".github/workflows/test.yml": FULL_HISTORY_CALLEE})
    assert _fact(doc, SHALLOW)["state"] == "fail"


def test_nested_reusable_calls_are_followed(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": CALLER.replace("test.yml", "mid.yml"),
        ".github/workflows/mid.yml": """
            on: workflow_call
            jobs:
              inner:
                uses: ./.github/workflows/test.yml
            """,
        ".github/workflows/test.yml": FULL_HISTORY_CALLEE})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "fail" and got["files"] == [".github/workflows/test.yml"], got


def test_a_calling_job_that_never_runs_on_a_pull_request_reaches_nothing(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": """
            on: [pull_request, push]
            jobs:
              test:
                if: github.event_name == 'push'
                uses: ./.github/workflows/test.yml
            """,
        ".github/workflows/test.yml": FULL_HISTORY_CALLEE})
    assert _fact(doc, SHALLOW)["state"] == "pass"


def test_a_callee_job_scoped_away_from_pull_requests_is_exempt(tmp_path):
    callee = FULL_HISTORY_CALLEE.replace(
        "runs-on: ubuntu-latest", "if: github.event_name == 'push'\n        runs-on: ubuntu-latest")
    doc = _collect(tmp_path, {".github/workflows/ci.yml": CALLER,
                              ".github/workflows/test.yml": callee})
    assert _fact(doc, SHALLOW)["state"] == "pass"


def test_a_callee_that_walks_history_keeps_the_exemption(tmp_path):
    callee = FULL_HISTORY_CALLEE.replace("run: pytest", "run: git diff origin/main...HEAD")
    doc = _collect(tmp_path, {".github/workflows/ci.yml": CALLER,
                              ".github/workflows/test.yml": callee})
    assert _fact(doc, SHALLOW)["state"] == "pass"


def test_negative_control_a_reusable_workflow_no_pr_workflow_calls_stays_excluded(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": f"""
            on: pull_request
            jobs:
              lint:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@{SHA}
                  - run: ruff check
            """,
        ".github/workflows/release.yml": """
            on: push
            jobs:
              test:
                uses: ./.github/workflows/test.yml
            """,
        ".github/workflows/test.yml": FULL_HISTORY_CALLEE})
    assert _fact(doc, SHALLOW)["state"] == "pass"


def test_a_call_that_escapes_the_repository_is_not_read_and_is_disclosed(tmp_path):
    (tmp_path / "outside.yml").write_text(_y(FULL_HISTORY_CALLEE))
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": CALLER.replace("./.github/workflows/test.yml",
                                                   "$/../outside.yml")})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "pass", got
    assert "could not be read" in got["evidence"] and "$/../outside.yml" in got["evidence"], got


# ---- 2. composite actions outside .github/actions ----------------------------

SETUP_CALLER = f"""
    on: pull_request
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@{SHA}
          - uses: ./ci/setup
          - run: npm test
    """

CACHED_UNPINNED_SETUP = """
    runs:
      using: composite
      steps:
        - uses: actions/setup-node@v4
          with: {cache: npm}
        - run: npm ci
          shell: bash
    """


def test_a_composite_outside_github_actions_counts_for_caching(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n",
                              ".github/workflows/ci.yml": SETUP_CALLER,
                              "ci/setup/action.yml": CACHED_UNPINNED_SETUP})
    got = _fact(doc, CACHE)
    assert got["state"] == "pass", got
    assert got["files"] == ["ci/setup/action.yml"], got


def test_a_composite_outside_github_actions_counts_for_pinning(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n",
                              ".github/workflows/ci.yml": SETUP_CALLER,
                              "ci/setup/action.yml": CACHED_UNPINNED_SETUP})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail", got
    assert got["evidence"].startswith("1 of 2 "), got["evidence"]
    assert got["files"] == ["ci/setup/action.yml"], got


def test_a_self_repository_composite_and_a_nested_one_are_read(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": SETUP_CALLER.replace("./ci/setup", "$/tools/x"),
        "tools/x/action.yaml": """
            runs:
              using: composite
              steps:
                - uses: ./tools/inner
            """,
        "tools/inner/action.yml": """
            runs:
              using: composite
              steps:
                - uses: docker/login-action@v3
            """})
    got = _fact(doc, PINNED)
    assert got["evidence"].startswith("1 of 2 "), got["evidence"]
    assert got["files"] == ["tools/inner/action.yml"], got


def test_negative_control_a_composite_path_that_escapes_the_repo_fails_closed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "action.yml").write_text(_y(CACHED_UNPINNED_SETUP))
    doc = _collect(tmp_path, {"package.json": "{}\n",
                              ".github/workflows/ci.yml":
                                  SETUP_CALLER.replace("./ci/setup", "./../outside")})
    # the file outside the checkout is never read: its cache earns nothing
    assert _fact(doc, CACHE)["state"] == "fail"
    assert _fact(doc, PINNED)["evidence"].startswith("1 of 1 ")
    assert any("./../outside" in e for e in doc["data_sources"]["composite_parse_errors"])


def test_a_missing_composite_is_recorded_not_silently_skipped(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": SETUP_CALLER})
    assert any("./ci/setup" in e for e in doc["data_sources"]["composite_parse_errors"])


def test_an_unread_composite_is_disclosed_in_the_pinning_and_caching_evidence(tmp_path):
    # the unread action may hold unpinned actions or the cache step: a pass
    # or fail that did not see it must say so on the card itself
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": SETUP_CALLER})
    pinned, cache = _fact(doc, PINNED), _fact(doc, CACHE)
    assert pinned["state"] == "pass", pinned
    assert "1 local action(s) could not be read" in pinned["evidence"], pinned
    assert "./ci/setup" in pinned["evidence"], pinned
    assert "1 local action(s) could not be read" in cache["evidence"], cache


# ---- 3. remote reusable workflows in the pinning count -----------------------

def _remote_caller(ref: str) -> str:
    return f"""
        on: pull_request
        jobs:
          lint:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            steps:
              - uses: actions/checkout@{SHA}
              - run: ruff check
          ci:
            uses: org/shared/.github/workflows/ci.yml@{ref}
        """


def test_a_remote_reusable_workflow_counts_in_pinning(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": _remote_caller("main")})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail", got
    assert got["evidence"].startswith("1 of 2 "), got["evidence"]
    assert got["files"] == [".github/workflows/ci.yml"], got


def test_negative_control_a_sha_pinned_remote_reusable_workflow_counts_as_pinned(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": _remote_caller(SHA2)})
    got = _fact(doc, PINNED)
    assert got["state"] == "pass", got
    assert got["evidence"].startswith("2 of 2 "), got["evidence"]


# ---- 4. workflow lockfile credit ---------------------------------------------

TAGGED = """
    on: pull_request
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v7.0.1
          - uses: actions/setup-go@v7.0.0
          - run: go test ./...
    """


def _lockfile(listed: list[str], deps: list[str] | None = None,
              wf: str = ".github/workflows/ci.yml") -> str:
    deps = listed if deps is None else deps
    lines = ["# This file is machine-generated by `gh actions-lock`.",
             "version: 'v0.0.3'", "workflows:", f"    '{wf}':"]
    lines += [f"        - '{ref}'" for ref in listed]
    lines.append("dependencies:")
    for i, ref in enumerate(deps):
        lines += [f"    '{ref}':", f"        ref: '{ref.split('@')[1]}'",
                  f"        commit: 'sha1-{(SHA if i % 2 else SHA2)}'",
                  "        owner_id: 44036562", f"        repo_id: {197814629 + i}"]
    return "\n".join(lines) + "\n"


BOTH = ["actions/checkout@v7.0.1", "actions/setup-go@v7.0.0"]


def test_a_fully_locked_workflow_passes_pinning(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED, LOCK: _lockfile(BOTH)})
    got = _fact(doc, PINNED)
    assert got["state"] == "pass", got
    assert got["evidence"].startswith("2 of 2 remote action references pinned, 2 via lockfile"), got


def test_negative_control_a_malformed_lockfile_grants_nothing(tmp_path):
    bad = _lockfile(BOTH).replace(f"sha1-{SHA2}", "sha1-not-a-commit")
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED, LOCK: bad})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail", got
    assert got["evidence"].startswith("0 of 2 "), got["evidence"]
    assert "grants no credit" in got["evidence"], got["evidence"]


def test_negative_control_an_unparseable_lockfile_grants_nothing(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED,
                              LOCK: "version: 'v0.0.3'\nworkflows: [unclosed\n"})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail" and got["evidence"].startswith("0 of 2 "), got


def test_negative_control_a_reference_the_lockfile_omits_is_not_covered(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED,
                              LOCK: _lockfile(BOTH[:1])})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail", got
    assert got["evidence"].startswith("1 of 2 remote action references pinned, 1 via lockfile"), got


def test_a_dependency_entry_not_listed_under_the_workflow_is_not_covered(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED,
                              LOCK: _lockfile(BOTH[:1], deps=BOTH)})
    assert _fact(doc, PINNED)["evidence"].startswith("1 of 2 ")


def test_lockfile_credit_inside_a_composite_needs_a_self_repository_reference(tmp_path):
    composite = """
        runs:
          using: composite
          steps:
            - uses: actions/setup-go@v7.0.0
        """
    wf = f"""
        on: pull_request
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@{SHA}
              - uses: PREFIX.github/actions/go
        """
    lock = _lockfile(BOTH[1:])
    # `$/` runs at the workflow's own commit and the lockfile covers what it uses
    doc = _collect(tmp_path / "a", {".github/workflows/ci.yml": wf.replace("PREFIX", "$/"),
                                    ".github/actions/go/action.yml": composite, LOCK: lock})
    assert _fact(doc, PINNED)["evidence"].startswith("2 of 2 remote action references pinned, 1 via lockfile")
    # `./` runs whatever is in the workspace: the lockfile cannot cover it
    doc = _collect(tmp_path / "b", {".github/workflows/ci.yml": wf.replace("PREFIX", "./"),
                                    ".github/actions/go/action.yml": composite, LOCK: lock})
    assert _fact(doc, PINNED)["evidence"].startswith("1 of 2 ")


_ENTRY = f"commit: 'sha1-{SHA2}'"


@__import__("pytest").mark.parametrize("name,mutate", [
    ("unknown top-level key", lambda t: t + "extra: 1\n"),
    ("missing owner_id", lambda t: t.replace("        owner_id: 44036562\n", "", 1)),
    ("bad version", lambda t: t.replace("version: 'v0.0.3'", "version: 'v0.0'")),
    ("commit with a trailing newline",
     lambda t: t.replace(_ENTRY, f'commit: "sha1-{SHA2}\\n"')),
    # yaml keeps the LAST of two equal keys; a strict reader refuses the file
    ("duplicate dependency key", lambda t: t.replace(
        "dependencies:\n",
        "dependencies:\n    'actions/checkout@v7.0.1':\n        ref: 'v7.0.1'\n"
        "        commit: 'sha1-not-a-commit'\n        owner_id: 1\n        repo_id: 2\n")),
    ("duplicate workflow key", lambda t: t.replace(
        "workflows:\n", "workflows:\n    '.github/workflows/ci.yml': []\n")),
])
def test_negative_control_every_malformed_lockfile_grants_nothing(tmp_path, name, mutate):
    bad = mutate(_lockfile(BOTH))
    assert bad != _lockfile(BOTH), name
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED, LOCK: bad})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail" and got["evidence"].startswith("0 of 2 "), (name, got)
    assert "grants no credit" in got["evidence"], (name, got)


def test_negative_control_a_lockfile_symlinked_outside_the_repo_grants_nothing(tmp_path):
    import os
    (tmp_path / "outside.lock").write_text(_lockfile(BOTH))
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / ".github/workflows/ci.yml").write_text(_y(TAGGED))
    os.symlink(str(tmp_path / "outside.lock"), str(root / LOCK))
    _git(root, "add", ".github")
    _git(root, "commit", "-qm", "init")
    doc, code = cc_mod.collect(root)
    assert code == 0, doc
    got = _fact(doc, PINNED)
    assert got["evidence"].startswith("0 of 2 ") and "grants no credit" in got["evidence"], got


def test_a_composite_chain_past_the_depth_limit_is_not_read_and_is_disclosed(tmp_path):
    files = {".github/workflows/ci.yml": SETUP_CALLER.replace("./ci/setup", "./a/0")}
    for i in range(18):
        files[f"a/{i}/action.yml"] = f"""
            runs:
              using: composite
              steps:
                - uses: ./a/{i + 1}
            """
    files["a/18/action.yml"] = CACHED_UNPINNED_SETUP
    doc = _collect(tmp_path, files)
    errs = doc["data_sources"]["composite_parse_errors"]
    assert any("nested more than 16 deep" in e for e in errs), errs
    got = _fact(doc, PINNED)
    assert got["evidence"].startswith("1 of 1 ") and "could not be read" in got["evidence"], got


def test_composites_past_the_read_limit_are_not_read_and_are_disclosed(tmp_path):
    steps = "\n".join(f"          - uses: ./a/{i}" for i in range(260))
    files = {".github/workflows/ci.yml": f"""
    on: pull_request
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@{SHA}
{steps}
    """}
    for i in range(260):
        files[f"a/{i}/action.yml"] = "runs: {using: composite, steps: [{uses: x/y@v1}]}\n"
    doc = _collect(tmp_path, files)
    errs = doc["data_sources"]["composite_parse_errors"]
    assert sum("past the 256-action read limit" in e for e in errs) == 4, errs
    assert _fact(doc, PINNED)["evidence"].startswith("1 of 257 "), _fact(doc, PINNED)


def test_no_lockfile_keeps_the_evidence_byte_identical(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED})
    assert _fact(doc, PINNED)["evidence"] == (
        "0 of 2 remote action references SHA-pinned (workflow + local composite "
        "action files; passes at >=95%)")


# ---- 5. review follow-ups ----------------------------------------------------

def test_a_pr_workflow_called_by_another_pr_workflow_gets_the_callers_events(tmp_path):
    # test.yml is itself a pull_request workflow, but ci.yml also calls it on
    # pull_request_target, where github.event_name is the CALLER's event: its
    # pull_request_target-only full-history job runs on pull requests too.
    callee = FULL_HISTORY_CALLEE.replace("on: workflow_call", "on: [pull_request, workflow_call]")
    callee = callee.replace(
        "runs-on: ubuntu-latest",
        "if: github.event_name == 'pull_request_target'\n        runs-on: ubuntu-latest")
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": CALLER.replace("on: pull_request", "on: pull_request_target"),
        ".github/workflows/test.yml": callee})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "fail", got
    assert got["files"] == [".github/workflows/test.yml"], got


def test_a_calling_jobs_if_narrows_the_events_the_callee_sees(tmp_path):
    # the calling job only runs on pull_request, so the callee's
    # pull_request_target-only full-history job never runs through it
    callee = FULL_HISTORY_CALLEE.replace(
        "runs-on: ubuntu-latest",
        "if: github.event_name == 'pull_request_target'\n        runs-on: ubuntu-latest")
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": """
            on: [pull_request, pull_request_target]
            jobs:
              test:
                if: github.event_name == 'pull_request'
                uses: ./.github/workflows/test.yml
            """,
        ".github/workflows/test.yml": callee})
    assert _fact(doc, SHALLOW)["state"] == "pass", _fact(doc, SHALLOW)


def test_lockfile_credit_inside_a_composite_needs_every_reaching_workflow_locked(tmp_path):
    composite = """
        runs:
          using: composite
          steps:
            - uses: actions/setup-go@v7.0.0
        """
    wf = f"""
        on: pull_request
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@{SHA}
              - uses: $/.github/actions/go
        """
    # other.yml reaches the same `$/` action but the lockfile does not cover it
    doc = _collect(tmp_path, {".github/workflows/ci.yml": wf,
                              ".github/workflows/other.yml": wf,
                              ".github/actions/go/action.yml": composite,
                              LOCK: _lockfile(BOTH[1:])})
    got = _fact(doc, PINNED)
    assert got["evidence"].startswith("2 of 3 remote action references pinned, 0 via lockfile"), got
    assert got["files"] == [".github/actions/go/action.yml"], got


def test_a_step_action_directory_named_like_yaml_is_read(tmp_path):
    # a step-level `uses:` is always an action directory, never a workflow
    doc = _collect(tmp_path, {"package.json": "{}\n",
                              ".github/workflows/ci.yml":
                                  SETUP_CALLER.replace("./ci/setup", "./ci/setup.yml"),
                              "ci/setup.yml/action.yml": CACHED_UNPINNED_SETUP})
    got = _fact(doc, CACHE)
    assert got["state"] == "pass", got
    assert got["files"] == ["ci/setup.yml/action.yml"], got


def test_negative_control_a_composite_symlinked_outside_the_repo_fails_closed(tmp_path):
    import os
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "action.yml").write_text(_y(CACHED_UNPINNED_SETUP))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for rel, text in {"package.json": "{}\n", ".github/workflows/ci.yml": SETUP_CALLER}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(_y(text))
        _git(root, "add", rel)
    (root / "ci").mkdir()
    os.symlink(str(outside), str(root / "ci" / "setup"))
    _git(root, "add", "ci/setup")
    _git(root, "commit", "-qm", "init")
    doc, code = cc_mod.collect(root)
    assert code == 0, doc
    # the file the link points at is not this repository's: never read
    assert _fact(doc, CACHE)["state"] == "fail", _fact(doc, CACHE)
    assert "could not be read" in _fact(doc, CACHE)["evidence"], _fact(doc, CACHE)
    assert any("./ci/setup" in e for e in doc["data_sources"]["composite_parse_errors"]), \
        doc["data_sources"]

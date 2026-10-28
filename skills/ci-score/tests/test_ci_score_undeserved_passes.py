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


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(_y(text))
        _git(root, "add", rel)
    _git(root, "commit", "-qm", "init")
    return root


def _collect(tmp_path: Path, files: dict[str, str]) -> dict:
    doc, code = cc_mod.collect(_make_repo(tmp_path, files))
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
    # test.yml has no pull_request trigger: the card names it as what it is
    assert got["evidence"] == ("fetch-depth: 0 on 1 reusable workflow(s) a PR-gating "
                               "workflow calls, e.g. .github/workflows/test.yml"), got


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
    # the file outside the checkout is never read: its cache earns nothing,
    # and a path outside the repository is not an action fetched at run time,
    # so the owner decision's not_applicable does not apply
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./../outside (resolves outside the repository)",
        "files": []}, _fact(doc, CACHE)
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
    assert "not read, so nothing inside was graded: ./ci/setup (no action.yml in the checkout; it may be checked out at run time)" in pinned["evidence"], pinned
    assert "./ci/setup" in pinned["evidence"], pinned
    assert "not read, so nothing inside was graded: ./ci/setup (no action.yml" in cache["evidence"], cache


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


def test_a_tagged_remote_reusable_workflow_earns_no_lockfile_credit(tmp_path):
    # GitHub does not lock another repository's reusable workflow yet, so a
    # hand-written lockfile entry for one enforces nothing: only a SHA counts
    call = "org/shared/.github/workflows/ci.yml@v1"
    doc = _collect(tmp_path, {".github/workflows/ci.yml": _remote_caller("v1"),
                              LOCK: _lockfile([call])})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail", got
    assert got["evidence"].startswith("1 of 2 remote action references pinned, 0 via lockfile"), got


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
    ("owner_id that is a boolean", lambda t: t.replace(
        "owner_id: 44036562", "owner_id: true", 1)),
    ("missing ref", lambda t: t.replace("        ref: 'v7.0.1'\n", "", 1)),
    ("entry uses that is not a list", lambda t: t.replace(
        "        repo_id: 197814629\n", "        repo_id: 197814629\n        uses: 'x/y@v1'\n", 1)),
    ("entry uses item with no @", lambda t: t.replace(
        "        repo_id: 197814629\n", "        repo_id: 197814629\n        uses: ['x-y']\n", 1)),
    ("workflow list item with no @", lambda t: t.replace(
        "        - 'actions/checkout@v7.0.1'\n",
        "        - 'actions/checkout@v7.0.1'\n        - 'nonsense'\n", 1)),
    ("workflow entry that is not a list", lambda t: t.replace(
        "workflows:\n", "workflows:\n    '.github/workflows/other.yml': 'x/y@v1'\n")),
])
def test_negative_control_every_malformed_lockfile_grants_nothing(tmp_path, name, mutate):
    bad = mutate(_lockfile(BOTH))
    assert bad != _lockfile(BOTH), name
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED, LOCK: bad})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail" and got["evidence"].startswith("0 of 2 "), (name, got)
    assert "grants no credit" in got["evidence"], (name, got)


@__import__("pytest").mark.parametrize("version", ["v0.0.2", "v0.0.3"])
def test_the_documented_lockfile_versions_earn_credit(tmp_path, version):
    lock = _lockfile(BOTH).replace("version: 'v0.0.3'", f"version: '{version}'")
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED, LOCK: lock})
    assert _fact(doc, PINNED)["state"] == "pass", _fact(doc, PINNED)


def test_negative_control_an_undocumented_lockfile_version_grants_nothing(tmp_path):
    # a format GitHub has not documented may mean something else entirely
    lock = _lockfile(BOTH).replace("version: 'v0.0.3'", "version: 'v1.0.0'")
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED, LOCK: lock})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail" and got["evidence"].startswith("0 of 2 "), got
    assert "format version v1.0.0 is not one GitHub documents" in got["evidence"], got


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
    assert got["evidence"].startswith("1 of 1 ") and "not read, so nothing inside was graded" in got["evidence"], got


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
    # the file the link points at is not this repository's: never read, and
    # not an action fetched at run time either, so the check fails
    assert _fact(doc, CACHE)["state"] == "fail", _fact(doc, CACHE)
    assert "not read, so nothing inside was graded: ./ci/setup (resolves outside the repository)" in _fact(doc, CACHE)["evidence"], _fact(doc, CACHE)
    assert any("./ci/setup" in e for e in doc["data_sources"]["composite_parse_errors"]), \
        doc["data_sources"]


# ---- 6. second review round: content the scan could not read -----------------

NOT_READ = "not read, so nothing inside was graded: "
NO_FILE_NOTE = " (no action.yml in the checkout; it may be checked out at run time)"
CACHE_FAIL = "no cache action and no setup-* cache input in any workflow or local composite action"
NA_EVIDENCE = ("no readable workflow or local action configures a cache or has a setup-* "
               "step that could; a local action that is not in the checkout runs before "
               "an install, build or test step and may hold the cache")


def _case_insensitive_fs(path: Path) -> bool:
    probe = path / "case-probe"
    probe.write_text("")
    try:
        return (path / "CASE-PROBE").exists()
    finally:
        probe.unlink()


def _repo_with_links(tmp_path: Path, files: dict[str, str],
                     links: dict[str, str]) -> dict:
    import os
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(_y(text))
    for rel, target in links.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, str(root / rel))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    doc, code = cc_mod.collect(root)
    assert code == 0, doc
    return doc


FULL_HISTORY_WITH_LOCAL = f"""
    on: pull_request
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@{SHA}
            with: {{fetch-depth: 0}}
          - uses: ./shared/setup
          - run: npm test
    """


def test_a_job_exempt_only_because_its_action_is_unread_says_so(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": FULL_HISTORY_WITH_LOCAL})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "pass", got
    assert "exempt only because ./shared/setup could not be read" in got["evidence"], got
    assert "walk(s) git history" not in got["evidence"], got
    assert NOT_READ + "./shared/setup" in got["evidence"], got


def test_negative_control_an_action_that_walks_history_is_still_a_proven_exemption(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": FULL_HISTORY_WITH_LOCAL,
                              "shared/setup/action.yml": """
                                  runs:
                                    using: composite
                                    steps:
                                      - run: git log --oneline -5
                                        shell: bash
                                  """})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "pass", got
    assert "1 walk(s) git history" in got["evidence"], got
    assert "exempt only because" not in got["evidence"] and NOT_READ not in got["evidence"], got


BROKEN_CALLEE = """
    on: workflow_call
    jobs:
      b:
        runs-on: ubuntu-latest
        steps:
          - uses: evil/action@main
           - bad: [indent
    """


def test_an_unparseable_workflow_is_named_on_the_caching_pinning_and_shallow_cards(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": f"""
            on: pull_request
            jobs:
              t:
                uses: ./.github/workflows/build.yml
              u:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@{SHA}
            """,
        ".github/workflows/build.yml": BROKEN_CALLEE})
    assert doc["data_sources"]["workflow_parse_errors"] == [".github/workflows/build.yml"]
    for check in (CACHE, PINNED, SHALLOW):
        got = _fact(doc, check)
        assert NOT_READ in got["evidence"] and ".github/workflows/build.yml" in \
            got["evidence"].split(NOT_READ, 1)[1], (check, got)


def test_a_step_list_that_is_not_a_list_is_named_on_the_cards(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": f"""
        on: pull_request
        jobs:
          a:
            runs-on: ubuntu-latest
            steps: {{uses: evil/action@main}}
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@{SHA}
        """})
    got = _fact(doc, PINNED)
    assert NOT_READ in got["evidence"] and "jobs.a.steps" in got["evidence"], got


def test_a_lockfile_in_the_wrong_case_grants_nothing(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED,
                              ".github/workflows/Actions.lock": _lockfile(BOTH)})
    got = _fact(doc, PINNED)
    # unconditional: the lockfile path check folds case itself, so a
    # case-sensitive disk reports the same thing
    assert got == {
        "state": "fail",
        "evidence": ("0 of 2 remote action references SHA-pinned (workflow + local composite "
                     "action files; passes at >=95%); .github/workflows/Actions.lock: path "
                     "case does not match the repository (a runner reads "
                     ".github/workflows/actions.lock), so it grants no credit"),
        "files": [".github/workflows/ci.yml"]}, got


def test_a_uses_path_in_the_wrong_case_earns_nothing(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n",
                              ".github/workflows/ci.yml": SETUP_CALLER.replace("./ci/setup",
                                                                               "./CI/Setup"),
                              "ci/setup/action.yml": CACHED_UNPINNED_SETUP})
    # the same on a case-sensitive and a case-insensitive disk: git's file list
    # decides, and a wrong-case path is not an action fetched at run time
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./CI/Setup (path case does not match the repository)",
        "files": []}, _fact(doc, CACHE)


def test_a_seeded_action_file_symlinked_outside_the_repo_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "action.yml").write_text(_y(CACHED_UNPINNED_SETUP))
    doc = _repo_with_links(tmp_path, {
        "package.json": "{}\n",
        ".github/workflows/ci.yml": SETUP_CALLER.replace("./ci/setup", "./.github/actions/setup")},
        {".github/actions/setup/action.yml": str(outside / "action.yml")})
    got = _fact(doc, CACHE)
    assert got["state"] != "pass", got
    # named once, by the seed walk; the uses: walk does not report it again
    errs = doc["data_sources"]["composite_parse_errors"]
    assert errs == [".github/actions/setup/action.yml (resolves outside the repository)"], errs
    assert NOT_READ + ".github/actions/setup/action.yml (resolves outside" in got["evidence"], got
    assert _fact(doc, PINNED)["evidence"].startswith("1 of 1 "), _fact(doc, PINNED)


def test_a_symlinked_github_actions_directory_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    (outside / "setup").mkdir(parents=True)
    (outside / "setup" / "action.yml").write_text(_y(CACHED_UNPINNED_SETUP))
    doc = _repo_with_links(tmp_path, {
        "package.json": "{}\n",
        ".github/workflows/ci.yml": SETUP_CALLER.replace("./ci/setup", "./.github/actions/setup")},
        {".github/actions": str(outside)})
    got = _fact(doc, CACHE)
    assert got["state"] != "pass", got
    assert "resolves outside the repository" in got["evidence"], got
    assert _fact(doc, PINNED)["evidence"].startswith("1 of 1 "), _fact(doc, PINNED)


INPUT_CALLEE = f"""
    on:
      workflow_call:
        inputs:
          fetch-depth: {{type: number, default: DEFAULT}}
    jobs:
      b:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@{SHA}
            with: {{fetch-depth: '${{{{ inputs.fetch-depth }}}}'}}
          - run: npm test
    """


def _input_caller(with_: str) -> str:
    return f"""
        on: pull_request
        jobs:
          t:
            uses: ./.github/workflows/build.yml
            {with_}
        """


def test_a_callee_depth_from_the_callers_input_is_graded(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": _input_caller("with: {fetch-depth: 0}"),
        ".github/workflows/build.yml": INPUT_CALLEE.replace("DEFAULT", "1")})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "fail", got
    assert got["files"] == [".github/workflows/build.yml"], got


def test_a_callee_depth_from_the_input_default_is_graded(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": _input_caller(""),
        ".github/workflows/build.yml": INPUT_CALLEE.replace("DEFAULT", "0")})
    assert _fact(doc, SHALLOW)["state"] == "fail", _fact(doc, SHALLOW)


def test_negative_control_a_shallow_input_passes(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": _input_caller("with: {fetch-depth: 1}"),
        ".github/workflows/build.yml": INPUT_CALLEE.replace("DEFAULT", "0")})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "pass" and "expression" not in got["evidence"], got


def test_an_unresolvable_callee_depth_is_not_graded_and_is_disclosed(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": _input_caller("with: {fetch-depth: '${{ vars.DEPTH }}'}"),
        ".github/workflows/build.yml": INPUT_CALLEE.replace("DEFAULT", "1")})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "pass", got
    assert ("1 job(s) on the PR path set fetch-depth by an expression that could not be "
            "resolved and were not graded: .github/workflows/build.yml") in got["evidence"], got


@__import__("pytest").mark.parametrize("name,mutate", [
    ("workflow key not in its canonical form", lambda t: t.replace(
        "'.github/workflows/ci.yml':", "'./.github/workflows/ci.yml':")),
    ("workflow key outside .github/workflows", lambda t: t.replace(
        "'.github/workflows/ci.yml':", "'ci.yml':")),
    ("entry ref that contradicts its key", lambda t: t.replace(
        "ref: 'v7.0.1'", "ref: 'v6.0.0'")),
    ("listed reference with no dependency entry", lambda t: t.replace(
        "workflows:\n", "workflows:\n    '.github/workflows/other.yml':\n        - 'x/y@v1'\n")),
    ("workflow key that normalizes to another path", lambda t: t.replace(
        "workflows:\n", "workflows:\n    '.github/workflows/..': []\n")),
    ("entry ref in a different case from its key", lambda t: t.replace(
        "ref: 'v7.0.1'", "ref: 'V7.0.1'")),
])
def test_negative_control_an_inconsistent_lockfile_grants_nothing(tmp_path, name, mutate):
    bad = mutate(_lockfile(BOTH))
    assert bad != _lockfile(BOTH), name
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED, LOCK: bad})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail" and got["evidence"].startswith("0 of 2 "), (name, got)
    assert "grants no credit" in got["evidence"], (name, got)


def test_a_commit_keyed_entry_with_a_tag_ref_stays_valid(tmp_path):
    # GitHub's own lockfile keys a nested dependency by commit, with its tag
    # as `ref`: that shape is valid and keeps the lockfile's credit
    nested = f"actions/attest@{SHA}"
    lock = _lockfile(BOTH).replace(
        "        repo_id: 197814629\n",
        f"        repo_id: 197814629\n        uses:\n            - '{nested}'\n", 1)
    lock += (f"    '{nested}':\n        ref: 'v4.1.0'\n        commit: 'sha1-{SHA}'\n"
             "        owner_id: 44036562\n        repo_id: 760701061\n")
    doc = _collect(tmp_path / "a", {".github/workflows/ci.yml": TAGGED, LOCK: lock})
    assert _fact(doc, PINNED)["state"] == "pass", _fact(doc, PINNED)
    # ...but a commit key whose entry records a different commit is malformed
    bad = lock.replace(f"ref: 'v4.1.0'\n        commit: 'sha1-{SHA}'",
                       f"ref: 'v4.1.0'\n        commit: 'sha1-{SHA2}'")
    assert bad != lock
    doc = _collect(tmp_path / "b", {".github/workflows/ci.yml": TAGGED, LOCK: bad})
    assert "grants no credit" in _fact(doc, PINNED)["evidence"], _fact(doc, PINNED)
    # ...and so is a nested reference with no entry of its own
    orphan = lock[:lock.index(f"    '{nested}':\n        ref")]
    doc = _collect(tmp_path / "c", {".github/workflows/ci.yml": TAGGED, LOCK: orphan})
    assert "grants no credit" in _fact(doc, PINNED)["evidence"], _fact(doc, PINNED)


EXPR_CACHE_SETUP = f"""
    inputs:
      cache: DEFAULT
    runs:
      using: composite
      steps:
        - uses: actions/setup-node@{SHA}
          with: {{cache: '${{{{ inputs.cache }}}}'}}
    """


def test_a_composite_cache_input_whose_default_is_false_earns_nothing(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": SETUP_CALLER,
                              "ci/setup/action.yml":
                                  EXPR_CACHE_SETUP.replace("DEFAULT", "{default: 'false'}")})
    assert _fact(doc, CACHE)["state"] == "fail", _fact(doc, CACHE)


def test_negative_control_a_caller_that_turns_the_cache_on_passes(tmp_path):
    caller = SETUP_CALLER.replace("- uses: ./ci/setup",
                                  "- uses: ./ci/setup\n            with: {cache: npm}")
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": caller,
                              "ci/setup/action.yml":
                                  EXPR_CACHE_SETUP.replace("DEFAULT", "{default: 'false'}")})
    assert _fact(doc, CACHE)["state"] == "pass", _fact(doc, CACHE)


def test_an_unresolvable_composite_cache_input_earns_nothing_and_is_disclosed(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": SETUP_CALLER,
                              "ci/setup/action.yml":
                                  EXPR_CACHE_SETUP.replace("DEFAULT", "{required: true}")})
    got = _fact(doc, CACHE)
    assert got["state"] == "fail", got
    assert ("cache input set by an expression that could not be resolved, so not "
            "credited: ci/setup/action.yml") in got["evidence"], got


def test_a_composite_with_a_duplicate_key_is_not_read_and_is_disclosed(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": SETUP_CALLER,
        "ci/setup/action.yml": """
            runs:
              using: composite
              steps:
                - uses: evil/x@main
            runs:
              using: composite
              steps:
                - run: echo hi
                  shell: bash
            """})
    got = _fact(doc, PINNED)
    assert got["evidence"].endswith(NOT_READ + "ci/setup/action.yml (duplicated key 'runs')"), got
    assert doc["data_sources"]["composite_parse_errors"] == [
        "ci/setup/action.yml (duplicated key 'runs')"], doc["data_sources"]


def test_a_callee_workflow_with_a_duplicate_key_is_not_graded(tmp_path):
    callee = FULL_HISTORY_CALLEE.rstrip() + (
        "\n    jobs:\n      other:\n        runs-on: ubuntu-latest\n        steps: []\n")
    doc = _collect(tmp_path, {".github/workflows/ci.yml": CALLER,
                              ".github/workflows/test.yml": callee})
    got = _fact(doc, SHALLOW)
    assert got["evidence"] == (
        "no PR-gating workflow checks out full history (post-merge automation like "
        "backport/changelog jobs is exempt - it needs history and gates nobody); 1 reusable "
        "workflow call(s) on the PR path could not be read and were not graded: "
        "./.github/workflows/test.yml"), got
    # the callee is not graded for pinning either, and the note there is true
    assert _fact(doc, PINNED) == {
        "state": "not_applicable",
        "evidence": ("no remote actions used in workflow or local composite action files; "
                     + NOT_READ + "./.github/workflows/test.yml (reusable workflow call not "
                     "followed)"),
        "files": []}, _fact(doc, PINNED)


# Owner decision 2026-10-27 (OD-CS24): with nothing readable that shows a
# cache or could carry one, an unread local action that could hold the cache
# step makes the check not_applicable, naming the action. A readable uncached
# setup-* step, or an install that plainly runs uncached, still fails.

def test_an_unread_action_that_could_hold_the_cache_is_not_applicable(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": f"""
        on: pull_request
        jobs:
          t:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@{SHA}
              - uses: ./shared/setup
              - run: npm ci
        """})
    assert _fact(doc, CACHE) == {
        "state": "not_applicable",
        "evidence": NA_EVIDENCE + "; " + NOT_READ + "./shared/setup" + NO_FILE_NOTE,
        "files": []}, _fact(doc, CACHE)


def test_an_unread_action_alone_with_a_manifest_is_not_applicable(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": SETUP_CALLER})
    assert _fact(doc, CACHE)["state"] == "not_applicable", _fact(doc, CACHE)


def test_negative_control_a_readable_uncached_setup_step_still_fails(tmp_path):
    caller = SETUP_CALLER.replace("- uses: ./ci/setup",
                                  f"- uses: actions/setup-node@{SHA}\n          - uses: ./ci/setup")
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": caller})
    got = _fact(doc, CACHE)
    assert got["state"] == "fail", got
    assert NOT_READ + "./ci/setup" in got["evidence"], got


def test_negative_control_a_plain_uncached_install_elsewhere_still_fails(tmp_path):
    caller = SETUP_CALLER.rstrip() + f"""
      lint:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@{SHA}
          - run: npm ci
    """
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": caller})
    assert _fact(doc, CACHE)["state"] == "fail", _fact(doc, CACHE)


# ---- 7. coverage pins from mutation testing ----------------------------------

GO_ONLY = """
    on: pull_request
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/setup-go@v7.0.0
          - run: go test ./...
    """


def test_lockfile_credit_is_per_workflow(tmp_path):
    # other.yml uses the same tagged action, but the lockfile lists it only
    # under ci.yml: pinned in one workflow, an offender in the other
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED,
                              ".github/workflows/other.yml": GO_ONLY,
                              LOCK: _lockfile(BOTH)})
    got = _fact(doc, PINNED)
    assert got["evidence"].startswith("2 of 3 remote action references pinned, 2 via lockfile"), got
    assert got["files"] == [".github/workflows/other.yml"], got


def _chain(tmp_path: Path, first: str, second: str) -> dict:
    return _collect(tmp_path, {
        ".github/workflows/ci.yml": f"""
            on: pull_request
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@{SHA}
                  - uses: {first}ci/outer
            """,
        "ci/outer/action.yml": f"""
            runs:
              using: composite
              steps:
                - uses: {second}ci/inner
            """,
        "ci/inner/action.yml": """
            runs:
              using: composite
              steps:
                - uses: actions/setup-go@v7.0.0
            """,
        LOCK: _lockfile(BOTH[1:])})


def test_a_self_repository_action_reached_through_a_workspace_action_earns_no_lockfile_credit(tmp_path):
    got = _fact(_chain(tmp_path, "./", "$/"), PINNED)
    assert got["evidence"].startswith("1 of 2 remote action references pinned, 0 via lockfile"), got


def test_an_all_self_repository_chain_earns_lockfile_credit(tmp_path):
    got = _fact(_chain(tmp_path, "$/", "$/"), PINNED)
    assert got["evidence"].startswith("2 of 2 remote action references pinned, 1 via lockfile"), got


def test_the_no_ci_refusal_sees_a_composite_outside_github_actions(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/triage.yml": f"""
            on: pull_request
            jobs:
              triage:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@{SHA}
                  - uses: ./ci/check
            """,
        "ci/check/action.yml": """
            runs:
              using: composite
              steps:
                - parallel:
                    - run: pytest
                      shell: bash
            """})
    assert doc["automation_only"] is False
    assert doc["ci_score"]["refusal"] is None, doc["ci_score"]
    # the walk record reads the same composite the checks read
    assert doc["data_sources"]["parallel_steps"]["invalid_groups"] == 1, doc["data_sources"]


def test_a_composite_cycle_is_read_once_and_reports_nothing(tmp_path):
    loop = """
        runs:
          using: composite
          steps:
            - uses: ./ci/OTHER
        """
    doc = _collect(tmp_path, {".github/workflows/ci.yml": SETUP_CALLER.replace("./ci/setup", "./ci/a"),
                              "ci/a/action.yml": loop.replace("OTHER", "b"),
                              "ci/b/action.yml": loop.replace("OTHER", "a")})
    assert "composite_parse_errors" not in doc["data_sources"], doc["data_sources"]
    assert NOT_READ not in _fact(doc, PINNED)["evidence"], _fact(doc, PINNED)


def _call_chain(n: int, files: dict | None = None) -> dict[str, str]:
    """ci.yml -> w1 -> ... -> wn: n hops; wn checks out full history."""
    files = dict(files or {})
    files[".github/workflows/ci.yml"] = CALLER.replace("test.yml", "w1.yml")
    for i in range(1, n):
        files[f".github/workflows/w{i}.yml"] = f"""
            on: workflow_call
            jobs:
              inner:
                uses: ./.github/workflows/w{i + 1}.yml
            """
    files[f".github/workflows/w{n}.yml"] = FULL_HISTORY_CALLEE
    return files


def test_a_reusable_call_cycle_is_followed_once_and_reports_nothing(tmp_path):
    loop = """
        on: workflow_call
        jobs:
          inner:
            uses: ./.github/workflows/OTHER.yml
        """
    doc = _collect(tmp_path, {".github/workflows/ci.yml": CALLER.replace("test.yml", "a.yml"),
                              ".github/workflows/a.yml": loop.replace("OTHER", "b"),
                              ".github/workflows/b.yml": loop.replace("OTHER", "a")})
    assert "could not be read" not in _fact(doc, SHALLOW)["evidence"], _fact(doc, SHALLOW)


def test_sixteen_reusable_hops_are_read_and_seventeen_are_disclosed(tmp_path):
    got = _fact(_collect(tmp_path / "a", _call_chain(16)), SHALLOW)
    assert got["state"] == "fail" and "could not be read" not in got["evidence"], got
    got = _fact(_collect(tmp_path / "b", _call_chain(17)), SHALLOW)
    assert got["state"] == "pass", got
    assert "could not be read and were not graded: ./.github/workflows/w17.yml" in got["evidence"], got


def _action_chain(last: int) -> dict[str, str]:
    """ci.yml -> a/0 -> ... -> a/<last>, which holds a cached unpinned setup."""
    files = {"package.json": "{}\n",
             ".github/workflows/ci.yml": SETUP_CALLER.replace("./ci/setup", "./a/0")}
    for i in range(last):
        files[f"a/{i}/action.yml"] = f"""
            runs:
              using: composite
              steps:
                - uses: ./a/{i + 1}
            """
    files[f"a/{last}/action.yml"] = CACHED_UNPINNED_SETUP
    return files


def test_the_composite_depth_boundary(tmp_path):
    doc = _collect(tmp_path / "a", _action_chain(16))
    assert "composite_parse_errors" not in doc["data_sources"], doc["data_sources"]
    assert _fact(doc, CACHE)["state"] == "pass", _fact(doc, CACHE)
    doc = _collect(tmp_path / "b", _action_chain(17))
    assert doc["data_sources"]["composite_parse_errors"] == [
        "./a/17 (nested more than 16 deep)"], doc["data_sources"]
    assert _fact(doc, CACHE)["state"] != "pass", _fact(doc, CACHE)


def test_a_mixed_case_lockfile_key_credits_the_lowercase_reference(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": TAGGED,
                              LOCK: _lockfile(["Actions/Setup-Go@v7.0.0"])})
    got = _fact(doc, PINNED)
    assert got["evidence"].startswith("1 of 2 remote action references pinned, 1 via lockfile"), got


def test_the_reusable_call_count_is_spelled_out(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": _remote_caller("main")})
    assert _fact(doc, PINNED)["evidence"] == (
        "1 of 2 remote action references SHA-pinned (workflow + local composite "
        "action files, including 1 reusable-workflow call(s); passes at >=95%)")


def test_more_than_three_unread_actions_say_how_many_more(tmp_path):
    steps = "".join(f"\n          - uses: ./missing/{i}" for i in range(4))
    doc = _collect(tmp_path, {".github/workflows/ci.yml": SETUP_CALLER.replace(
        "\n          - uses: ./ci/setup", steps)})
    assert " and 1 more" in _fact(doc, PINNED)["evidence"], _fact(doc, PINNED)


def test_a_repo_whose_only_remote_reference_is_a_reusable_call_now_fails_pinning(tmp_path):
    # before v0.1.6 this pinning check was not_applicable (no remote step);
    # the call is a remote reference, so it now counts and fails at 0 of 1
    doc = _collect(tmp_path, {".github/workflows/ci.yml": """
        on: pull_request
        jobs:
          ci:
            uses: org/shared/.github/workflows/ci.yml@v1
        """})
    got = _fact(doc, PINNED)
    assert got["state"] == "fail", got
    assert got["evidence"] == (
        "0 of 1 remote action references SHA-pinned (workflow + local composite "
        "action files, including 1 reusable-workflow call(s); passes at >=95%)"), got


# ---- 8. the frozen calibration controls: every fact pinned -------------------

_CONTROL_FACTS = Path(__file__).resolve().parent / "fixtures" / "undeserved-passes-control-facts.json"


@__import__("pytest").mark.parametrize("name", ["better-auth", "mastra"])
def test_the_calibration_controls_keep_every_fact(tmp_path, name):
    """Every practice fact (state, evidence, files) on both frozen controls
    equals the snapshot taken with the engine before this change, so "no fact
    moves" is enforced, not only measured."""
    import json
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _fixture_checkouts import materialize
    root = tmp_path / name
    root.mkdir()
    materialize(name, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fixture")
    doc, code = cc_mod.collect(root)
    assert code == 0, doc
    want = json.loads(_CONTROL_FACTS.read_text(encoding="utf-8"))[name]
    assert doc["practice_facts"] == want


# ---- 9. third review round ---------------------------------------------------

import os  # noqa: E402

import pytest  # noqa: E402
import yaml  # noqa: E402

pf_mod = _load("ci_score_practice_facts_undeserved", "scripts/practice_facts.py")


def _job_wf(*steps: str, on: str = "pull_request", job: str = "t") -> str:
    """One job: a SHA-pinned checkout, then `steps` (each a step body; extra
    lines of a step are indented 8 spaces)."""
    body = "".join(f"\n      - {s}" for s in steps)
    return (f"on: {on}\njobs:\n  {job}:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"      - uses: actions/checkout@{SHA}{body}\n")


def _composite(*steps: str) -> str:
    body = "".join(f"\n    - {s}" for s in steps)
    return f"runs:\n  using: composite\n  steps:{body}\n"


SHALLOW_PASS = ("no PR-gating workflow checks out full history (post-merge automation like "
                "backport/changelog jobs is exempt - it needs history and gates nobody)")
UNRESOLVED_CACHE = "; cache input set by an expression that could not be resolved, so not credited: "
CACHE_PASS_ONE = "dependency caching configured in 1 workflow/action file(s)"


# Owner decision (5), as the owner stated it: not_applicable only when an
# unread local action is the ONLY thing that could explain the missing cache.

def test_od5_a_readable_setup_step_outside_the_language_list_still_fails(tmp_path):
    doc = _collect(tmp_path, {"requirements.txt": "x\n", ".github/workflows/ci.yml": _job_wf(
        f"uses: prefix-dev/setup-pixi@{SHA}\n        with: {{cache: false}}",
        "uses: ./.github/actions/missing", "run: pytest")})
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./.github/actions/missing" + NO_FILE_NOTE,
        "files": []}, _fact(doc, CACHE)


def test_od5_an_unread_action_after_the_install_explains_nothing(tmp_path):
    doc = _collect(tmp_path, {"requirements.txt": "x\n", ".github/workflows/ci.yml": _job_wf(
        "run: pip install -r requirements.txt", "run: pytest",
        "uses: ./.github/actions/notify")})
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./.github/actions/notify" + NO_FILE_NOTE,
        "files": []}, _fact(doc, CACHE)


def test_od5_a_wrong_case_path_is_not_an_action_fetched_at_run_time(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n",
                              ".github/workflows/ci.yml": _job_wf("uses: ./ci/setup", "run: npm test"),
                              "ci/Setup/action.yml": _composite("run: echo hi\n      shell: bash")})
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./ci/setup (path case does not match the repository)",
        "files": []}, _fact(doc, CACHE)


def test_od5_an_unread_action_in_a_job_off_the_pr_path_explains_nothing(tmp_path):
    doc = _collect(tmp_path, {
        "Cargo.toml": "[package]\nname = 'x'\n",
        ".github/workflows/ci.yml": _job_wf("run: cargo build", "run: cargo test"),
        ".github/workflows/release.yml": _job_wf("uses: ./.github/actions/notify", on="push")})
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./.github/actions/notify" + NO_FILE_NOTE,
        "files": []}, _fact(doc, CACHE)


@pytest.mark.parametrize("cond", ["false", "${{ false }}"])
def test_od5_an_unread_step_that_never_runs_explains_nothing(tmp_path, cond):
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": _job_wf(
        f"uses: ./nope\n        if: {cond}", "run: npm ci", "run: npm test")})
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./nope" + NO_FILE_NOTE,
        "files": []}, _fact(doc, CACHE)


def test_od5_an_unread_action_reached_through_a_readable_composite_counts(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n",
                              ".github/workflows/ci.yml": _job_wf("uses: ./ci/outer", "run: npm ci"),
                              "ci/outer/action.yml": _composite("uses: ./missing")})
    assert _fact(doc, CACHE) == {
        "state": "not_applicable",
        "evidence": NA_EVIDENCE + "; " + NOT_READ + "./missing" + NO_FILE_NOTE,
        "files": []}, _fact(doc, CACHE)


def test_od5_an_install_inside_a_readable_composite_before_the_unread_action_fails(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n",
                              ".github/workflows/ci.yml": _job_wf(
                                  "uses: ./ci/install", "uses: ./missing", "run: npm test"),
                              "ci/install/action.yml": _composite("run: npm ci\n      shell: bash")})
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./missing" + NO_FILE_NOTE,
        "files": []}, _fact(doc, CACHE)


def test_od5_one_action_under_two_spellings_is_followed_both_ways(tmp_path):
    wf = (_job_wf("uses: ./ci/outer", "run: npm ci", job="a").rstrip("\n") + "\n"
          + _job_wf("uses: $/ci/outer", "run: npm ci", job="b").split("jobs:\n", 1)[1])
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": wf,
                              "ci/outer/action.yml": _composite("uses: ./missing")})
    assert _fact(doc, CACHE) == {
        "state": "not_applicable",
        "evidence": NA_EVIDENCE + "; " + NOT_READ + "./missing" + NO_FILE_NOTE,
        "files": []}, _fact(doc, CACHE)


def test_a_composite_cache_input_any_caller_turns_on_passes(tmp_path):
    wf = (_job_wf("uses: ./ci/setup\n        with: {cache: npm}", "run: npm ci", job="a")
          .rstrip("\n") + "\n"
          + _job_wf("uses: ./ci/setup", "run: npm ci", job="b").split("jobs:\n", 1)[1])
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": wf,
                              "ci/setup/action.yml":
                                  EXPR_CACHE_SETUP.replace("DEFAULT", "{default: 'false'}")})
    assert _fact(doc, CACHE) == {"state": "pass", "evidence": CACHE_PASS_ONE,
                                 "files": ["ci/setup/action.yml"]}, _fact(doc, CACHE)


# Case checks read git's own file list: what a runner checks out.

def test_the_case_check_reads_gits_file_list(tmp_path):
    root = _make_repo(tmp_path, {"ci/setup/action.yml": CACHED_UNPINNED_SETUP})
    assert pf_mod._case_mismatch(root, root / "CI" / "Setup" / "action.yml") is True
    assert pf_mod._case_mismatch(root, root / "ci" / "setup" / "action.yml") is False


def test_the_case_check_reads_the_directory_listing_outside_a_git_work_tree(tmp_path):
    root = tmp_path / "plain"
    (root / "ci" / "setup").mkdir(parents=True)
    (root / "ci" / "setup" / "action.yml").write_text(_y(CACHED_UNPINNED_SETUP))
    assert pf_mod._case_mismatch(root, root / "CI" / "Setup" / "action.yml") is True
    assert pf_mod._case_mismatch(root, root / "ci" / "setup" / "action.yml") is False


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root lists any directory, so the permission error never happens")
def test_a_directory_that_cannot_be_listed_fails_closed(tmp_path):
    root = tmp_path / "plain"
    files = {"requirements.txt": "x\n",
             ".github/workflows/ci.yml": _job_wf("uses: ./.github/actions/setup",
                                                 "run: pip install -r requirements.txt"),
             ".github/actions/Setup/action.yml": _composite(
                 f"uses: actions/cache@{SHA}\n      with: {{path: x, key: k}}")}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    raw = files[".github/workflows/ci.yml"]
    os.chmod(root / ".github" / "actions", 0o111)
    try:
        facts = pf_mod._practice_facts(
            [(".github/workflows/ci.yml", yaml.safe_load(raw), raw)], root, [])
        mismatch = pf_mod._case_mismatch(root, root / ".github/actions/setup/action.yml")
    finally:
        os.chmod(root / ".github" / "actions", 0o755)
    assert mismatch is True
    assert facts[CACHE] == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./.github/actions/setup (directory could not be listed)",
        "files": []}, facts[CACHE]


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="a decomposed name on disk that git stores composed is a macOS behaviour")
def test_a_decomposed_directory_name_is_not_a_case_mismatch(tmp_path):
    nfc, nfd = "café", "café"
    doc = _collect(tmp_path, {"requirements.txt": "x\n",
                              ".github/workflows/ci.yml": _job_wf(
                                  f"uses: ./.github/actions/{nfc}",
                                  "run: pip install -r requirements.txt"),
                              f".github/actions/{nfd}/action.yml": _composite(
                                  f"uses: actions/cache@{SHA}\n      with: {{path: x, key: k}}")})
    assert "composite_parse_errors" not in doc["data_sources"], doc["data_sources"]
    assert _fact(doc, CACHE) == {"state": "pass", "evidence": CACHE_PASS_ONE,
                                 "files": [f".github/actions/{nfc}/action.yml"]}, _fact(doc, CACHE)


def test_an_action_reached_only_by_a_wrong_case_reference_earns_nothing(tmp_path):
    # the seed walk reads every action.yml under .github/actions, but the only
    # reference to this one is in the wrong case: on a Linux runner its cache
    # step never runs, so it earns nothing
    doc = _collect(tmp_path, {"requirements.txt": "x\n",
                              ".github/workflows/ci.yml": _job_wf(
                                  "uses: ./.github/actions/setup",
                                  "run: pip install -r requirements.txt"),
                              ".github/actions/Setup/action.yml": _composite(
                                  f"uses: actions/cache@{SHA}\n      with: {{path: x, key: k}}")})
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + "; " + NOT_READ + "./.github/actions/setup (path case does not match the repository)",
        "files": []}, _fact(doc, CACHE)
    assert _fact(doc, PINNED)["evidence"].startswith("1 of 1 "), _fact(doc, PINNED)


# Unresolved expressions earn nothing, everywhere.

@pytest.mark.parametrize("expr", [
    "${{ env.NODE_CACHE }}", "${{ matrix.cache }}", "${{ inputs.cache || 'npm' }}",
    "${{ github.event_name == 'never' && 'npm' || '' }}"])
def test_a_composite_cache_expression_that_is_not_a_plain_input_earns_nothing(tmp_path, expr):
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": SETUP_CALLER,
                              "ci/setup/action.yml": _composite(
                                  f"uses: actions/setup-node@{SHA}\n      with:\n"
                                  f"        cache: \"{expr}\"")})
    assert _fact(doc, CACHE) == {
        "state": "fail", "evidence": CACHE_FAIL + UNRESOLVED_CACHE + "ci/setup/action.yml",
        "files": []}, _fact(doc, CACHE)


def test_a_workflow_cache_expression_earns_nothing(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": _job_wf(
        f"uses: actions/setup-node@{SHA}\n        with: {{cache: \"${{{{ matrix.pm }}}}\"}}",
        "run: npm ci")})
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": CACHE_FAIL + UNRESOLVED_CACHE + ".github/workflows/ci.yml",
        "files": []}, _fact(doc, CACHE)


CACHE_CALLEE = f"""
    on:
      workflow_call:
        inputs:
          cache: {{type: string, default: DEFAULT}}
    jobs:
      b:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@{SHA}
          - uses: actions/setup-node@{SHA}
            with: {{cache: '${{{{ inputs.cache }}}}'}}
          - run: npm ci
    """


@pytest.mark.parametrize("with_,default,want", [
    ("", "''", "fail"), ("with: {cache: ''}", "npm", "fail"),
    ("with: {cache: npm}", "''", "pass"), ("", "npm", "pass")])
def test_a_called_workflows_cache_input_resolves_against_its_callers(tmp_path, with_, default, want):
    doc = _collect(tmp_path, {"package.json": "{}\n",
                              ".github/workflows/ci.yml": _input_caller(with_),
                              ".github/workflows/build.yml": CACHE_CALLEE.replace("DEFAULT", default)})
    if want == "pass":
        assert _fact(doc, CACHE) == {"state": "pass", "evidence": CACHE_PASS_ONE,
                                     "files": [".github/workflows/build.yml"]}, _fact(doc, CACHE)
    else:
        assert _fact(doc, CACHE) == {"state": "fail", "evidence": CACHE_FAIL,
                                     "files": []}, _fact(doc, CACHE)


def test_negative_control_a_manual_runs_cache_input_default_counts(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": f"""
        on:
          pull_request:
          workflow_dispatch:
            inputs:
              cache: {{default: npm}}
        jobs:
          t:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/setup-node@{SHA}
                with: {{cache: '${{{{ inputs.cache }}}}'}}
              - run: npm ci
        """})
    assert _fact(doc, CACHE) == {"state": "pass", "evidence": CACHE_PASS_ONE,
                                 "files": [".github/workflows/ci.yml"]}, _fact(doc, CACHE)


def test_a_composite_cache_input_default_counts_when_no_caller_passes_one(tmp_path):
    doc = _collect(tmp_path, {"package.json": "{}\n", ".github/workflows/ci.yml": SETUP_CALLER,
                              "ci/setup/action.yml": EXPR_CACHE_SETUP.replace("DEFAULT", "{default: npm}")})
    assert _fact(doc, CACHE) == {"state": "pass", "evidence": CACHE_PASS_ONE,
                                 "files": ["ci/setup/action.yml"]}, _fact(doc, CACHE)


def test_more_than_three_unresolved_cache_inputs_say_how_many_more(tmp_path):
    steps = "".join(f"\n          - uses: ./ci/{n}" for n in "abcd")
    files = {"package.json": "{}\n",
             ".github/workflows/ci.yml": SETUP_CALLER.replace("\n          - uses: ./ci/setup", steps)}
    for n in "abcd":
        files[f"ci/{n}/action.yml"] = EXPR_CACHE_SETUP.replace("DEFAULT", "{required: true}")
    doc = _collect(tmp_path, files)
    assert _fact(doc, CACHE)["evidence"] == (
        CACHE_FAIL + UNRESOLVED_CACHE + "ci/a/action.yml, ci/b/action.yml, ci/c/action.yml "
        "and 1 more"), _fact(doc, CACHE)


def test_a_pr_workflows_own_unresolved_depth_is_disclosed(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": f"""
        on: pull_request
        jobs:
          t:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@{SHA}
                with: {{fetch-depth: '${{{{ vars.DEPTH }}}}'}}
              - run: npm test
        """})
    assert _fact(doc, SHALLOW) == {
        "state": "pass",
        "evidence": SHALLOW_PASS + "; 1 job(s) on the PR path set fetch-depth by an expression "
                    "that could not be resolved and were not graded: .github/workflows/ci.yml",
        "files": []}, _fact(doc, SHALLOW)


def test_any_caller_passing_full_history_fails_the_callee(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/a.yml": _input_caller("with: {fetch-depth: 1}"),
        ".github/workflows/b.yml": _input_caller("with: {fetch-depth: 0}"),
        ".github/workflows/build.yml": INPUT_CALLEE.replace("DEFAULT", "1")})
    assert _fact(doc, SHALLOW) == {
        "state": "fail",
        "evidence": ("fetch-depth: 0 on 1 reusable workflow(s) a PR-gating workflow calls, "
                     "e.g. .github/workflows/build.yml"),
        "files": [".github/workflows/build.yml"]}, _fact(doc, SHALLOW)


DIRECT_AND_CALLED = INPUT_CALLEE.replace(
    "on:\n      workflow_call:", "on:\n      pull_request:\n      workflow_call:").replace(
    "DEFAULT", "0")


def test_a_workflow_triggered_directly_sees_an_empty_input(tmp_path):
    # nobody calls it: on its own pull_request trigger the input is empty, so
    # the default (0) never applies
    doc = _collect(tmp_path, {".github/workflows/build.yml": DIRECT_AND_CALLED})
    assert _fact(doc, SHALLOW) == {"state": "pass", "evidence": SHALLOW_PASS,
                                   "files": []}, _fact(doc, SHALLOW)


def test_a_workflow_triggered_directly_and_called_with_full_history_fails(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/build.yml": DIRECT_AND_CALLED,
                              ".github/workflows/ci.yml": _input_caller("with: {fetch-depth: 0}")})
    got = _fact(doc, SHALLOW)
    assert got["state"] == "fail" and got["files"] == [".github/workflows/build.yml"], got


def test_an_unresolved_depth_off_the_pr_path_is_not_counted(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": f"""
        on: [pull_request, push]
        jobs:
          t:
            if: github.event_name == 'push'
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@{SHA}
                with: {{fetch-depth: '${{{{ vars.DEPTH }}}}'}}
        """})
    assert _fact(doc, SHALLOW)["evidence"] == SHALLOW_PASS, _fact(doc, SHALLOW)


def test_an_unresolved_depth_in_a_history_job_is_not_counted(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": f"""
        on: pull_request
        jobs:
          release:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@{SHA}
                with: {{fetch-depth: '${{{{ vars.DEPTH }}}}'}}
        """})
    assert _fact(doc, SHALLOW)["evidence"] == SHALLOW_PASS, _fact(doc, SHALLOW)


# No self-contradicting cards: a callee the PR path cannot read is graded
# nowhere, so the "not read" note on caching and pinning is true.

def test_a_callee_with_two_jobs_keys_is_not_graded_for_pinning(tmp_path):
    caller = CALLER.rstrip() + f"""
      lint:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@{SHA}
    """
    doc = _collect(tmp_path, {".github/workflows/ci.yml": caller,
                              ".github/workflows/test.yml": """
        on: workflow_call
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: evil/x@main
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/setup-go@v5
        """})
    assert _fact(doc, PINNED) == {
        "state": "pass",
        "evidence": ("1 of 1 remote action references SHA-pinned (workflow + local composite "
                     "action files; passes at >=95%); " + NOT_READ
                     + "./.github/workflows/test.yml (reusable workflow call not followed)"),
        "files": []}, _fact(doc, PINNED)


def test_a_callee_with_two_steps_keys_is_not_graded_for_caching(tmp_path):
    doc = _collect(tmp_path, {"requirements.txt": "x\n", ".github/workflows/ci.yml": CALLER,
                              ".github/workflows/test.yml": f"""
        on: workflow_call
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: evil/x@main
            steps:
              - run: pip install -r requirements.txt
              - uses: actions/cache@{SHA}
                with: {{path: x, key: k}}
        """})
    assert _fact(doc, CACHE) == {
        "state": "fail",
        "evidence": (CACHE_FAIL + "; " + NOT_READ
                     + "./.github/workflows/test.yml (reusable workflow call not followed)"),
        "files": []}, _fact(doc, CACHE)


def test_an_unparseable_callee_is_named_once_on_every_card(tmp_path):
    doc = _collect(tmp_path, {
        ".github/workflows/ci.yml": f"""
            on: pull_request
            jobs:
              t:
                uses: ./.github/workflows/build.yml
              u:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@{SHA}
            """,
        ".github/workflows/build.yml": BROKEN_CALLEE})
    note = "; " + NOT_READ + ".github/workflows/build.yml (does not parse as a workflow)"
    assert _fact(doc, SHALLOW)["evidence"] == SHALLOW_PASS + note, _fact(doc, SHALLOW)
    assert _fact(doc, PINNED)["evidence"] == (
        "1 of 1 remote action references SHA-pinned (workflow + local composite action files; "
        "passes at >=95%)" + note), _fact(doc, PINNED)


def test_a_direct_call_derives_unparseable_workflows_from_disk(tmp_path):
    root = tmp_path / "plain"
    (root / ".github" / "workflows").mkdir(parents=True)
    raw = _job_wf("run: echo")
    (root / ".github/workflows/ci.yml").write_text(raw)
    (root / ".github/workflows/broken.yml").write_text(_y(BROKEN_CALLEE))
    facts = pf_mod._practice_facts([(".github/workflows/ci.yml", yaml.safe_load(raw), raw)], root)
    assert facts[PINNED]["evidence"] == (
        "1 of 1 remote action references SHA-pinned (workflow + local composite action files; "
        "passes at >=95%); " + NOT_READ + ".github/workflows/broken.yml (does not parse as a "
        "workflow)"), facts[PINNED]


# The history exemption for a chain with an unread link: the job is still
# exempt (fail-closed), and the card says the exemption rests on that link.

def _exempt_only(link: str, note: str) -> str:
    return ("1 job(s) in PR-triggered workflow(s) check out full history and are exempt: "
            f"1 exempt only because {link} could not be read; no other PR-gating job takes "
            f"full history; {NOT_READ}{note}")


def test_a_readable_action_calling_a_missing_one_is_exempt_only_because_of_it(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": FULL_HISTORY_WITH_LOCAL,
                              "shared/setup/action.yml": _composite("uses: ./missing/inner")})
    assert _fact(doc, SHALLOW)["evidence"] == _exempt_only(
        "./missing/inner", "./missing/inner" + NO_FILE_NOTE), _fact(doc, SHALLOW)


def test_an_inner_action_linked_outside_the_repo_is_exempt_only_because_of_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "action.yml").write_text(_composite("run: git log -5\n      shell: bash"))
    doc = _repo_with_links(tmp_path, {".github/workflows/ci.yml": FULL_HISTORY_WITH_LOCAL,
                                      "shared/setup/action.yml": _composite("uses: ./ci/inner")},
                           {"ci/inner": str(outside)})
    assert _fact(doc, SHALLOW)["evidence"] == _exempt_only(
        "./ci/inner", "./ci/inner (resolves outside the repository)"), _fact(doc, SHALLOW)


def test_a_wrong_case_inner_action_is_exempt_only_because_of_it(tmp_path):
    doc = _collect(tmp_path, {".github/workflows/ci.yml": FULL_HISTORY_WITH_LOCAL,
                              "shared/setup/action.yml": _composite("uses: ./CI/Inner"),
                              "ci/inner/action.yml": _composite("run: git log -5\n      shell: bash")})
    assert _fact(doc, SHALLOW)["evidence"] == _exempt_only(
        "./CI/Inner", "./CI/Inner (path case does not match the repository)"), _fact(doc, SHALLOW)


def test_an_inner_action_past_the_depth_cap_is_exempt_only_because_of_it(tmp_path):
    files = {".github/workflows/ci.yml": FULL_HISTORY_WITH_LOCAL,
             "shared/setup/action.yml": _composite("uses: ./a/1")}
    for i in range(1, 17):
        files[f"a/{i}/action.yml"] = _composite(f"uses: ./a/{i + 1}")
    files["a/17/action.yml"] = _composite("run: git log -5\n      shell: bash")
    doc = _collect(tmp_path, files)
    assert _fact(doc, SHALLOW)["evidence"] == _exempt_only(
        "./a/17", "./a/17 (nested more than 16 deep)"), _fact(doc, SHALLOW)

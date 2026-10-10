"""GitHub's self-repository `uses:` prefix (shipped 2026-07-30): `uses: $/path`
names an action or reusable workflow in THIS repository at the running commit,
exactly like `uses: ./path`, with no checkout needed.

Every local-action read in the engine matched only `./`, so a `$/` step was
never opened: a history op inside a `$/` composite was invisible to the
shallow-checkout carve-out, the payload read behind a `$/` composite was never
searched, a checkout behind a `$/` composite was not found, and a reusable
workflow called as `$/.github/workflows/x.yml` was dropped from the call graph.
`$/x` resolves to `<repo root>/x`, exactly like `./x`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import collect_runs as cr  # noqa: E402
import scan  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_scan_globals():
    saved = (scan._GIT_HISTORY_LOCAL_ACTIONS, scan._LOCAL_ACTION_TEXT)
    yield
    scan._GIT_HISTORY_LOCAL_ACTIONS, scan._LOCAL_ACTION_TEXT = saved


def _composite(root: Path, name: str, body: str) -> None:
    d = root / ".github" / "actions" / name
    d.mkdir(parents=True)
    d.joinpath("action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n" + body, encoding="utf-8")


def _parsed(jobs: dict) -> list[tuple[str, dict, str]]:
    return [(".github/workflows/ci.yml", {"on": {"pull_request": None}, "jobs": jobs}, "")]


def test_history_index_opens_a_self_repository_composite(tmp_path):
    _composite(tmp_path, "changed",
               "    - run: git diff origin/main...HEAD\n      shell: bash\n")
    job = {"steps": [{"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}},
                     {"uses": "$/.github/actions/changed"}]}
    idx = scan._index_local_git_actions(tmp_path, _parsed({"a": job}))
    assert idx == {"$/.github/actions/changed"}
    scan._GIT_HISTORY_LOCAL_ACTIONS = idx
    assert scan._job_needs_git_history(job, "a") is True


def test_history_index_fails_closed_on_an_unreadable_self_repository_action(tmp_path):
    job = {"steps": [{"uses": "$/.github/actions/missing"}]}
    assert scan._index_local_git_actions(tmp_path, _parsed({"a": job})) == {
        "$/.github/actions/missing"}


def test_composite_walk_follows_self_repository_refs_transitively(tmp_path):
    _composite(tmp_path, "outer", "    - uses: $/.github/actions/inner\n")
    _composite(tmp_path, "inner",
               "    - run: ls vendor/inner-marker\n      shell: bash\n")
    job = {"steps": [{"uses": "$/.github/actions/outer"}]}
    idx = scan._index_local_action_text(tmp_path, _parsed({"a": job}))
    assert "vendor/inner-marker" in (idx.get("$/.github/actions/outer") or ""), idx
    scan._LOCAL_ACTION_TEXT = idx
    assert "vendor/inner-marker" in (scan._job_payload_blob(job) or "")


def test_payload_read_fails_closed_on_an_unreadable_self_repository_action(tmp_path):
    job = {"steps": [{"uses": "$/.github/actions/missing"}]}
    scan._LOCAL_ACTION_TEXT = scan._index_local_action_text(tmp_path, _parsed({"a": job}))
    assert scan._job_payload_blob(job) is None


def test_reusable_workflow_call_graph_reads_the_self_repository_prefix():
    graph = scan._build_workflow_call_graph(
        _parsed({"t": {"uses": "$/.github/workflows/test.yml"}}))
    assert graph == {".github/workflows/ci.yml": [".github/workflows/test.yml"]}


def test_a_self_repository_action_is_not_a_remote_action():
    assert scan._step_uses({"uses": "$/.github/actions/x"}) is None


def test_opt80_finds_a_checkout_behind_a_self_repository_composite(tmp_path):
    _composite(tmp_path, "co", "    - uses: actions/checkout@v4\n")
    got = cr._opt80_checkout_step(
        {"steps": [{"uses": "$/.github/actions/co"}, {"run": "npm test"}]}, tmp_path)
    assert isinstance(got, tuple), got
    assert got[2] == "local composite $/.github/actions/co"


def test_opt80_follows_a_self_repository_ref_nested_in_a_composite(tmp_path):
    _composite(tmp_path, "outer", "    - uses: $/.github/actions/inner\n")
    _composite(tmp_path, "inner", "    - uses: actions/checkout@v4\n")
    got = cr._opt80_checkout_step(
        {"steps": [{"uses": "./.github/actions/outer"}, {"run": "npm test"}]}, tmp_path)
    assert isinstance(got, tuple), got
    assert got[2] == "local composite ./.github/actions/outer"


def test_opt80_reads_the_abort_from_a_self_repository_composite(tmp_path):
    _composite(tmp_path, "net",
               "    - run: git config --global http.lowSpeedTime 30\n"
               "      shell: bash\n")
    co = {"uses": "actions/checkout@v4"}
    job = {"steps": [{"uses": "$/.github/actions/net"}, co, {"run": "npm test"}]}
    assert cr._opt80_retry_already_configured(
        {"jobs": {"build": job}}, job, tmp_path, checkout_step=co) is True

"""GitHub's self-repository `uses:` prefix (shipped 2026-07-30): `uses: $/path`
names an action or reusable workflow in THIS repository at the running commit,
exactly like `uses: ./path`, with no checkout needed.

The engine treated only `./` as local, so a repository written the new way had
every `$/` step counted as an unpinned REMOTE action (a real repository read
"111 of 135 remote action references SHA-pinned" when the truth was 111 of 111),
and the git-history exemption never opened a `$/`-referenced composite action.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

_SKILL_DIR = Path(__file__).resolve().parents[1]


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


pf_mod = _load("ci_score_practice_facts_self_repo", "scripts/practice_facts.py")

SHA = "8f4b7f84864484a7bf31766abe9204da3cbe65b3"
PINNED = "ci.security.pinned-action-shas"
SHALLOW = "ci.checkout.shallow-clone"


def _parsed(steps: list) -> list[tuple[str, dict, str]]:
    jobs: dict = {"b": {"steps": steps}}
    raw = yaml.safe_dump({"on": {"pull_request": None}, "jobs": jobs},
                         sort_keys=False)
    return [("ci.yml", yaml.safe_load(raw), raw)]


def _composite(root: Path, name: str, body: str) -> None:
    d = root / ".github" / "actions" / name
    d.mkdir(parents=True)
    d.joinpath("action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n" + body)


def test_a_self_repository_action_is_local_not_an_unpinned_remote(tmp_path):
    _composite(tmp_path, "pkg-install",
               "    - run: echo install\n      shell: bash\n")
    steps = [{"uses": f"actions/checkout@{SHA}"},
             {"uses": "$/.github/actions/pkg-install"},
             {"uses": "$/.github/actions/pkg-install"}]
    got = pf_mod._practice_facts(_parsed(steps), tmp_path)[PINNED]
    assert got["state"] == "pass", got
    assert got["evidence"].startswith("1 of 1 "), got["evidence"]
    assert got["files"] == [], got["files"]


def test_a_self_repository_ref_inside_a_composite_is_local_too(tmp_path):
    _composite(tmp_path, "outer",
               f"    - uses: actions/setup-node@{SHA}\n"
               "    - uses: $/.github/actions/inner\n")
    _composite(tmp_path, "inner", "    - run: echo hi\n      shell: bash\n")
    got = pf_mod._practice_facts(_parsed([{"uses": "$/.github/actions/outer"}]),
                                 tmp_path)[PINNED]
    assert got["state"] == "pass" and got["evidence"].startswith("1 of 1 "), got


def test_the_history_index_opens_a_self_repository_composite(tmp_path):
    _composite(tmp_path, "changed",
               "    - run: git diff origin/main...HEAD\n      shell: bash\n")
    steps = [{"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}},
             {"uses": "$/.github/actions/changed"}]
    assert pf_mod._index_local_git_actions(tmp_path, _parsed(steps)) == {
        "$/.github/actions/changed"}
    got = pf_mod._practice_facts(_parsed(steps), tmp_path)[SHALLOW]
    assert got["state"] == "pass", got


def test_an_unreadable_self_repository_action_fails_closed(tmp_path):
    steps = [{"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}},
             {"uses": "$/.github/actions/missing"}]
    assert pf_mod._index_local_git_actions(tmp_path, _parsed(steps)) == {
        "$/.github/actions/missing"}

"""Workflow duplicate keys must not erase configuration before grading."""
import pytest

from test_ci_score_undeserved_passes import _make_repo, cc_mod, SHA

VALID = ("on: pull_request\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
         "    timeout-minutes: 10\n    steps:\n"
         f"      - uses: actions/checkout@{SHA}\n      - run: pytest\n")


@pytest.mark.parametrize("key,broken", [
    ("steps", "on: pull_request\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
     "    steps:\n      - uses: example/action@main\n"
     "    steps:\n      - run: pytest\n"),
    ("jobs", "on: pull_request\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
     "    steps:\n      - uses: example/action@main\n"
     "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"),
])
def test_duplicate_workflow_is_unread_and_ungraded(tmp_path, key, broken):
    root = _make_repo(tmp_path, {".github/workflows/ci.yml": VALID,
                                ".github/workflows/broken.yml": broken})
    doc, code = cc_mod.collect(root)
    assert code == 0
    rel = ".github/workflows/broken.yml"
    assert doc["data_sources"].get("workflow_parse_errors") == [rel]
    assert doc["data_sources"]["workflow_parse_error_reasons"][rel] == f"duplicated key {key!r}"
    evidence = doc["practice_facts"]["ci.security.pinned-action-shas"]["evidence"]
    assert "1 of 1" in evidence
    assert f"{rel} (duplicated key {key!r})" in evidence
    assert "not read, so nothing inside was graded" in evidence


def test_valid_workflow_remains_unchanged(tmp_path):
    root = _make_repo(tmp_path, {".github/workflows/ci.yml": VALID})
    doc, code = cc_mod.collect(root)
    assert code == 0
    assert "workflow_parse_errors" not in doc["data_sources"]
    assert doc["practice_facts"]["ci.security.pinned-action-shas"]["state"] == "pass"

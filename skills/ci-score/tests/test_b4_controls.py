"""B4 layer (a): exact-match controls on frozen fixture checkouts.

The calibration positive controls (mastra 91 A, better-auth 82 B+) must
reproduce EXACTLY from the committed fixture trees under
`tests/fixtures/checkouts-src/` — the full collector path (acquire → facts →
score), not just the arithmetic. Live repos drift; these trees cannot, so
an exact-match failure here means the ENGINE changed, never the input.
Offline: each cell materializes the cloaked fixture into a throwaway local git
repo and runs with sockets booby-trapped. (The fixtures ship cloaked — see
``_fixture_checkouts`` — so registry scanners don't attribute the third-party
workflow files to this repo; :func:`materialize` restores the exact original
tree byte-for-byte, so what the collector scores is unchanged.)

Layer (b) (live smoke) and layer (c) (the wide sweep) run outside CI —
their record lives in the B4 sweep notes; this module is the part of B4
that CI re-proves forever.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from _fixture_checkouts import materialize

_SKILL_DIR = Path(__file__).resolve().parents[1]

# The frozen expectations — recomputed under v0.1.2 (OD-CS18 removed
# ci.trigger.draft-gate) by running the collector over the frozen checkout
# trees; never hand-derived. Under v0.1.1 these were mastra 83 B+ and
# better-auth 75 B; removing the draft-gate check moves both to 9/11.
# v0.1.3 (OD-CS19) adds the dependency-cache manifest gate and the
# test-sharding no-test-job gate: both fixtures already pass dependency-cache
# (cache hits present) and pass test-sharding (real test jobs), so neither
# gate fires and both hold at 82 B+ — re-verified by re-running the collector.
# The git-history carve-out on ci.checkout.shallow-clone (2026-09-02) moves the
# first control from 9/11 to 10/11 and leaves the second where it was. Both
# numbers were re-derived by running the collector over the frozen trees and
# then AUDITING every full-history checkout on the PR path by hand:
#  - First control: all nine of its PR-gating jobs that set `fetch-depth: 0`
#    run a real history operation — seven diff the pull request's base SHA
#    against its head SHA (one of them across a line continuation), one
#    fetches the head repository and diffs the base SHA against FETCH_HEAD,
#    and one runs changeset versioning and then diffs the working tree. Eight
#    of the nine are proven from the YAML itself: the base SHA they name is
#    absent from a depth-1 clone, so the command cannot run. The ninth — the
#    peer-dependency check — is a FAIL-CLOSED carve-out, not a proof: its own
#    diffs are working-tree diffs that a shallow clone serves fine, and it is
#    spared because it runs changeset versioning, which the predicate treats
#    as a history operation. So: none of the nine is an offender, and the
#    check passes. 82 B+ -> 91 A.
#  - Second control: eight of its NINE PR-gating full-history jobs run only
#    package install / build / lint / typecheck / test commands and no git
#    operation at all, so they stay offenders; only the changeset-verification
#    job (which diffs `origin/$BASE_REF...HEAD`) is newly spared. The check
#    still fails — on four workflow files instead of five — and the score is
#    unchanged at 82 B+. That the second control did NOT move is the evidence
#    that the carve-out spares history jobs rather than blanket-silencing the
#    check.
_CONTROLS = {
    "mastra": {"value": 91, "grade": "A"},
    "better-auth": {"value": 82, "grade": "B+"},
}

# The per-fixture verdict on the one fact the carve-out moves, audited job by
# job in the note above.
_SHALLOW_CLONE = {
    "mastra": {"state": "pass", "offender_workflows": 0},
    "better-auth": {"state": "fail", "offender_workflows": 4},
}


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


cc_mod = _load("ci_score_collect_config", "scripts/collect_config.py")


@pytest.fixture(autouse=True)
def _no_egress(monkeypatch):
    import socket

    def boom(*_a, **_k):  # pragma: no cover
        raise AssertionError("network egress during a control run")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True,
                   env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(root)})


@pytest.mark.parametrize("name", sorted(_CONTROLS))
def test_control_reproduces_calibration_grade_exactly(name, tmp_path):
    root = tmp_path / name
    root.mkdir()
    materialize(name, root)  # restore the cloaked fixture to its exact tree
    _git(root, "init", "-q")
    _git(root, "add", "-A")  # throwaway fixture repo, not a working tree
    _git(root, "commit", "-qm", "fixture")

    doc, code = cc_mod.collect(root)
    assert code == 0
    stamp = doc["ci_score"]
    expected = _CONTROLS[name]
    assert stamp["refusal"] is None
    assert (stamp["value"], stamp["grade"]) == (expected["value"], expected["grade"]), (
        f"{name}: engine drift — frozen fixture scored "
        f"{stamp['value']} ({stamp['grade']}), calibration row is "
        f"{expected['value']} ({expected['grade']})")
    # clean throwaway repo → clean provenance (the -dirty path is tested
    # elsewhere; here it would mask an unstable fixture build)
    assert not doc["commit_sha"].endswith("-dirty")
    # The grade above is a total; pin the ONE fact the git-history carve-out
    # moves, so a future regression that happens to keep the total intact
    # still shows up here.
    shallow = doc["practice_facts"]["ci.checkout.shallow-clone"]
    assert shallow["state"] == _SHALLOW_CLONE[name]["state"], shallow["evidence"]
    if _SHALLOW_CLONE[name]["offender_workflows"]:
        assert (f"{_SHALLOW_CLONE[name]['offender_workflows']} PR-gating workflow(s)"
                in shallow["evidence"]), shallow["evidence"]
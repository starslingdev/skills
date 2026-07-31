"""B4 layer (a): exact-match controls on frozen fixture checkouts.

The calibration positive controls (mastra 83 B+, better-auth 75 B) must
reproduce EXACTLY from the committed fixture trees under
`tests/fixtures/checkouts/` — the full collector path (acquire → facts →
score), not just the arithmetic. Live repos drift; these trees cannot, so
an exact-match failure here means the ENGINE changed, never the input.
Offline: each cell rebuilds the fixture as a throwaway local git repo and
runs with sockets booby-trapped.

Layer (b) (live smoke) and layer (c) (the wide sweep) run outside CI —
their record lives in the B4 sweep notes; this module is the part of B4
that CI re-proves forever.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_CHECKOUTS = _SKILL_DIR / "tests" / "fixtures" / "checkouts"

# The frozen expectations — recomputed under v0.1.2 (OD-CS18 removed
# ci.trigger.draft-gate) by running the collector over the frozen checkout
# trees; never hand-derived. Under v0.1.1 these were mastra 83 B+ and
# better-auth 75 B; removing the draft-gate check moves both to 9/11.
# v0.1.3 (OD-CS19) adds the dependency-cache manifest gate and the
# test-sharding no-test-job gate: both fixtures already pass dependency-cache
# (cache hits present) and pass test-sharding (real test jobs), so neither
# gate fires and both hold at 82 B+ — re-verified by re-running the collector.
_CONTROLS = {
    "mastra": {"value": 82, "grade": "B+"},
    "better-auth": {"value": 82, "grade": "B+"},
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
    fixture = _CHECKOUTS / name
    assert fixture.is_dir(), f"missing frozen checkout fixture {name}"
    root = tmp_path / name
    shutil.copytree(fixture, root)
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
"""Chain-aware whole-commit projection regression coverage."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import wall_clock as wc


_VERIFY_PATH = Path(__file__).with_name("verify_report.py")


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "chain_projection_verify_report", _VERIFY_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _project_chain(
    *,
    wall_s: float,
    pole_s: float,
    runner_up_s: float,
    chain_s: float,
    speedup: float,
):
    """Exercise the production helper when present.

    The fallback is the pole-only projection used before chain facts reached
    the shared helper. Keeping that old behavior here makes the regression fail
    on its numeric assertion when run against an unchanged checkout.
    """
    project = getattr(wc, "project_commit_wall", None)
    if project is None:
        projected = round(
            wall_s - pole_s + max(pole_s / speedup, runner_up_s), 1
        )
        return SimpleNamespace(
            projected_s=projected,
            multiple=round(wall_s / projected, 2),
        )
    return project(
        wall_s,
        pole_s,
        runner_up_s,
        speedup,
        chain_s=chain_s,
        pole_s=pole_s,
        runner_up_s=runner_up_s,
    )


def test_serial_chain_projection_keeps_other_members_and_parallel_floor():
    # setup 120s -> test 900s -> report 60s. A 3x test speedup leaves the
    # other 180s of serialized work in place: 120 + 300 + 60 = 480s.
    serial_only = _project_chain(
        wall_s=1080.0,
        pole_s=900.0,
        runner_up_s=0.0,
        chain_s=1080.0,
        speedup=3.0,
    )
    assert serial_only.projected_s == 480.0
    assert serial_only.multiple == 2.25

    # A 600s independent path caps the whole commit at 600s. The old pole-only
    # formula adds that floor to the chain's other members and returns 780s.
    parallel_floor = _project_chain(
        wall_s=1080.0,
        pole_s=900.0,
        runner_up_s=600.0,
        chain_s=1080.0,
        speedup=3.0,
    )
    assert parallel_floor.projected_s == 600.0
    assert parallel_floor.multiple == 1.8


def test_chainless_projection_keeps_the_single_job_formula():
    projection = wc.project_commit_wall(1500.0, 1440.0, 900.0, 4.0)
    assert projection.projected_s == 960.0
    assert projection.multiple == 1.56


def test_verifier_rederives_chain_projection_from_facts(tmp_path: Path):
    verifier = _load_verifier()
    doc = {
        "pr_critical_path": {
            "chain_facts": [{
                "sha": "abc123",
                "chain": ["setup", "test", "report"],
                "member_spans_s": {
                    "setup": 120.0,
                    "test": 900.0,
                    "report": 60.0,
                },
                "chain_s": 1080.0,
                "runner_up_s": 600.0,
            }],
            "commit_wall_clock": {
                "facts": [{
                    "sha": "abc123",
                    "partial": False,
                    "wall_s": 1080.0,
                    "slowest_job": {"name": "test", "duration_s": 900.0},
                    "next_longest_job": {"name": "parallel", "duration_s": 600.0},
                    "projection_3x": {
                        "job_speedup": 3.0,
                        "projected_s": 600.0,
                        "multiple": 1.8,
                    },
                }],
            },
        },
    }
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")

    checks = verifier.run_checks("# CI speedup report\n", None, findings, None)
    check = next(
        check for check in checks
        if check.name == "chain projections re-derive from stamped chain facts"
    )
    assert check.ok, check.detail

    doc["pr_critical_path"]["commit_wall_clock"]["facts"][0][
        "projection_3x"
    ]["projected_s"] = 780.0
    findings.write_text(json.dumps(doc), encoding="utf-8")
    mutant = verifier.check_chain_projection_matches_facts(findings)
    assert not mutant.ok
    assert "780.0 != chain-derived 600.0" in mutant.detail

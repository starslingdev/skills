"""The emulated-image fix's runner-minute saving keeps the native compile it still pays."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402

_VR_SPEC = importlib.util.spec_from_file_location("vr_emulated", HERE / "verify_report.py")
vr = importlib.util.module_from_spec(_VR_SPEC)
sys.modules["vr_emulated"] = vr
_VR_SPEC.loader.exec_module(vr)

_POLE = {"workflow_file": "release.yml", "job": "image", "check": "image"}


def _doc(measured: float = 1000.0) -> dict:
    return {"runner_minute_spine": {"rows": [{
        "workflow_file": "release.yml", "job_name": "image",
        "effective_monthly_job_volume": 30,
        "completed_billable_equiv_min_per_month": measured,
    }]}}


def _leaf(emulated: float, native: float, arches: int = 1) -> dict:
    return {"fix_key": "emulated-image-compile",
            "magnitude": {"label": "emulated compile/install seconds", "unit": "s",
                          "value": emulated},
            "native_compile_s": native, "foreign_arches": arches}


def test_saving_subtracts_the_native_compile_each_foreign_arch_still_needs():
    # 120s emulated, 20s native: cross-compiling still costs ~20s per foreign arch,
    # so 100s/run is removed; 100s x 30 runs / 60 = 50 runner-min/month.
    line = bp._emulated_runner_minute_effect_line(_doc(), _POLE, _leaf(120.0, 20.0))
    assert "saving ~50 runner-min/month" in line, line
    assert "capped at the job's measured compute" in line


def test_two_foreign_arches_each_keep_a_native_compile():
    line = bp._emulated_runner_minute_effect_line(_doc(), _POLE, _leaf(120.0, 20.0, 2))
    assert "saving ~40 runner-min/month" in line, line


def test_foreign_only_build_has_no_native_compile_to_subtract_and_stays_unsized():
    line = bp._emulated_runner_minute_effect_line(_doc(), _POLE, _leaf(100.0, 0.0))
    assert "Runner-minute effect: not sized" in line, line
    assert "saving ~" not in line


def test_saving_is_capped_at_measured_compute():
    line = bp._emulated_runner_minute_effect_line(_doc(10.0), _POLE, _leaf(120.0, 20.0))
    assert "saving ~10 runner-min/month" in line, line


def test_verifier_rederives_the_displayed_saving(tmp_path):
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"findings": [], **_doc()}), encoding="utf-8")
    line = bp._emulated_runner_minute_effect_line(_doc(), _POLE, _leaf(120.0, 20.0))
    report = ("<!-- ci-speedup:leaf-crown fix_key=emulated-image-compile -->\n"
              + line + "\n")
    assert vr.check_wall_clock_runner_minute_effects(report, findings).ok
    inflated = report.replace("saving ~50 runner-min/month", "saving ~60 runner-min/month")
    check = vr.check_wall_clock_runner_minute_effects(inflated, findings)
    assert not check.ok, check

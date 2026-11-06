"""OPT9 distinguishes measured tool caches from speculative build state."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL / "scripts"))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
from test_opt81_faster_runner import _CI, _Opt81Client  # noqa: E402


def _collect(monkeypatch) -> dict:
    client = _Opt81Client([("ubuntu-latest", 197)] * 8)
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    doc = {
        "repo": "o/r",
        "findings": [
            {
                "id": "f1",
                "pattern": "OPT9",
                "workflow_file": _CI,
                "affected_jobs": ["typecheck"],
                "title": "TypeScript incremental build state",
                "evidence": (
                    "job `typecheck` runs `tsc` without `--incremental`; the proposed "
                    "cache persists `.tsbuildinfo`"
                ),
            },
            {
                "id": "f2",
                "pattern": "OPT9",
                "workflow_file": _CI,
                "affected_jobs": ["generate-client"],
                "title": "Generated client cache",
                "evidence": (
                    "job `generate-client` regenerates an API client and could cache the "
                    "generated-client output"
                ),
            },
            {
                "id": "f3",
                "pattern": "OPT9",
                "workflow_file": _CI,
                "affected_jobs": ["test"],
                "title": "TypeScript ESLint cache",
                "evidence": (
                    "job `test` runs `eslint` over generated code without its "
                    "`--cache` flag"
                ),
            },
            {
                "id": "f4",
                "pattern": "OPT9",
                "workflow_file": _CI,
                "affected_jobs": ["test"],
                "title": "TypeScript Vitest cache",
                "evidence": (
                    "job `test` runs `vitest` over generated TypeScript fixtures "
                    "without its result cache"
                ),
            },
        ],
    }
    return cr.collect(doc, "o/r", max_runs=8, shallow_runs=8)


def _verify_full(doc: dict, tmp_path: Path) -> str:
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    doc["skill_commit_sha"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=_SKILL, text=True).strip()
    findings_path.write_text(json.dumps(doc), encoding="utf-8")
    assert bp.main(["--in", str(findings_path), "--out", str(report_path)]) == 0
    proc = subprocess.run(
        [
            sys.executable,
            str(_SKILL / "tests" / "verify_report.py"),
            "--report",
            str(report_path),
            "--findings",
            str(findings_path),
        ],
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return report_path.read_text(encoding="utf-8")


def test_build_state_is_uncredited_while_eslint_keeps_existing_sizing(
        monkeypatch, tmp_path):
    doc = _collect(monkeypatch)
    by_id = {finding["id"]: finding for finding in doc["findings"]}

    for finding_id in ("f1", "f2"):
        finding = by_id[finding_id]
        assert finding["wall_clock_p50_s"] is None
        assert finding["runner_min_saving"] is None
        assert finding["tier"] == 2
        assert finding["realization"] == "none"
        assert finding["opt9_measure_before_after"] is True
        assert "measure before and after" in finding["size_note"].lower()

    eslint = by_id["f3"]
    assert {
        key: eslint.get(key)
        for key in (
            "wall_clock_p50_s",
            "runner_min_saving",
            "tier",
            "realization",
            "runner_min_unrealizable_s",
            "runner_min_unrealizable",
            "size_note",
            "runner_min_note",
            "sizing_basis",
        )
    } == {
        "wall_clock_p50_s": 15.0,
        "runner_min_saving": None,
        "tier": 1,
        "realization": "direct",
        "runner_min_unrealizable_s": 75.0,
        "runner_min_unrealizable": True,
        "size_note": (
            "ephemeral-runner caveat — this saving assumes a warm local cache, "
            "which GitHub-hosted/ephemeral runners don't have; verify a "
            "warm-vs-cold step-time delta before claiming wall-clock (OPT8 guardrail)"
        ),
        "runner_min_note": (
            "runner-minute saving is ALSO unproven on ephemeral runners — the "
            "cache writes to a working-dir path the next runner discards; "
            "realizable only with a persisted actions/cache for that dir + a "
            "measured warm-vs-cold delta (none here), so this is config-hygiene, "
            "not a credited bill saving"
        ),
        "sizing_basis": "modeled",
    }

    vitest = by_id["f4"]
    assert {
        key: vitest.get(key)
        for key in (
            "wall_clock_p50_s",
            "runner_min_saving",
            "tier",
            "realization",
            "runner_min_unrealizable_s",
            "runner_min_unrealizable",
            "size_note",
            "runner_min_note",
            "sizing_basis",
        )
    } == {
        key: eslint.get(key)
        for key in (
            "wall_clock_p50_s",
            "runner_min_saving",
            "tier",
            "realization",
            "runner_min_unrealizable_s",
            "runner_min_unrealizable",
            "size_note",
            "runner_min_note",
            "sizing_basis",
        )
    }

    report = _verify_full(doc, tmp_path)
    assert "measure before and after" in report.lower()

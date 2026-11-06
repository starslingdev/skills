"""Measured path-filter hit rate for the missing-trigger-filter finding."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL / "scripts"))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
from test_opt81_faster_runner import _CI, _COLLECT_YAML, _Opt81Client  # noqa: E402


class _PathFilterClient(_Opt81Client):
    def __init__(self, paths_by_pr: dict[int, list[str]], *, yaml: str = _COLLECT_YAML):
        super().__init__([("ubuntu-latest", 197)] * 60, yaml)
        self.paths_by_pr = paths_by_pr
        self.changed_file_calls = 0

    def json(self, endpoint, allow_missing=False):
        response = super().json(endpoint, allow_missing)
        if response is not None:
            if isinstance(response, dict) and "workflow_runs" in response:
                for i, run in enumerate(response["workflow_runs"], 1):
                    run["pull_requests"] = [{"number": i}]
            return response
        match = re.fullmatch(r"repos/o/r/pulls/(\d+)/files\?per_page=100(?:&page=(\d+))?", endpoint)
        if match:
            self.changed_file_calls += 1
            page = int(match.group(2) or 1)
            paths = self.paths_by_pr.get(int(match.group(1)), [])
            start = (page - 1) * 100
            return [{"filename": p} for p in paths[start:start + 100]]
        return None


def _collect(monkeypatch: pytest.MonkeyPatch, client: _PathFilterClient,
             pattern: str = "OPT32") -> dict:
    monkeypatch.setattr(cr, "GhClient", lambda *a, **k: client)
    doc = {"repo": "o/r", "findings": [{
        "id": "f1", "pattern": pattern, "workflow_file": _CI,
        "title": "Add path filters", "evidence": "pull requests run the workflow",
    }]}
    return cr.collect(doc, "o/r", max_runs=60, shallow_runs=60)


def _opt32(doc: dict) -> dict:
    return next(f for f in doc["findings"] if f.get("pattern") == "OPT32")


def _verify_full(doc: dict, tmp_path: Path) -> str:
    findings = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    doc["skill_commit_sha"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=_SKILL, text=True).strip()
    findings.write_text(json.dumps(doc), encoding="utf-8")
    assert bp.main(["--in", str(findings), "--out", str(report_path)]) == 0
    report = report_path.read_text(encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SKILL / "tests" / "verify_report.py"),
         "--report", str(report_path), "--findings", str(findings)],
        text=True, capture_output=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return report


def test_opt32_measures_three_of_sixty_through_collect_and_full_verifier(
        monkeypatch, tmp_path):
    paths = {n: [f"src/file{n}.py"] for n in range(1, 61)}
    paths.update({1: ["README.md"], 2: ["docs/start.md"], 3: ["LICENSE.txt"]})
    client = _PathFilterClient(paths)

    doc = _collect(monkeypatch, client)
    finding = _opt32(doc)

    assert finding["path_filter_measurement"]["would_skip_prs"] == 3
    assert finding["path_filter_measurement"]["sampled_prs"] == 60
    assert finding["path_filter_measurement"]["hit_rate"] == 0.05
    assert "3 of 60 sampled PRs" in finding["size_note"]
    assert "modeled upper bound" in finding["size_note"]
    assert client.changed_file_calls == 60
    report = _verify_full(doc, tmp_path)
    assert "3 of 60 sampled PRs" in report
    assert "modeled upper bound" in report


def test_opt32_zero_hit_is_also_noticed_and_csv_is_not_skipped(monkeypatch, tmp_path):
    paths = {n: (["docs/x.csv"] if n == 1 else [f"src/file{n}.py"])
             for n in range(1, 61)}
    doc = _collect(monkeypatch, _PathFilterClient(paths))
    finding = _opt32(doc)

    assert finding["path_filter_measurement"]["would_skip_prs"] == 0
    report = _verify_full(doc, tmp_path)
    close = report.split('<a id="also-noticed">', 1)[0]
    assert "Add path filters" not in close
    assert "Add path filters" in report.split('<a id="also-noticed">', 1)[1]


def test_opt32_uses_and_names_repository_paths_ignore(monkeypatch, tmp_path):
    yaml = _COLLECT_YAML.replace(
        "  pull_request:\n", "  pull_request:\n    paths-ignore:\n      - 'generated/**'\n")
    paths = {n: [f"src/file{n}.py"] for n in range(1, 61)}
    paths[1] = ["generated/api.json"]
    doc = _collect(monkeypatch, _PathFilterClient(paths, yaml=yaml))
    finding = _opt32(doc)

    assert finding["path_filter_measurement"]["would_skip_prs"] == 1
    assert finding["path_filter_measurement"]["source"] == "repository paths-ignore"
    assert "generated/**" in finding["evidence"]
    _verify_full(doc, tmp_path)


def test_opt32_query_budget_grows_only_by_the_sampled_pr_count(monkeypatch):
    paths = {n: [f"src/file{n}.py"] for n in range(1, 61)}
    baseline_client = _PathFilterClient(paths)
    _collect(monkeypatch, baseline_client, pattern="OPT1")
    baseline_queries = baseline_client.queries
    assert baseline_client.changed_file_calls == 0

    measured_client = _PathFilterClient(paths)
    _collect(monkeypatch, measured_client)
    assert measured_client.queries == baseline_queries + 60
    assert measured_client.changed_file_calls == 60


def test_opt32_changed_files_fetch_is_paginated_and_bounded():
    paths = {1: [f"docs/file-{i}.md" for i in range(201)]}
    client = _PathFilterClient(paths)
    assert len(cr._opt32_changed_files(client, "o/r", 1)) == 201
    assert client.changed_file_calls == 3

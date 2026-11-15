"""Measured path-filter hit rate for the missing-trigger-filter finding."""
from __future__ import annotations

import base64
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
from test_opt81_faster_runner import _CI, _COLLECT_YAML, _Opt81Client, _cts  # noqa: E402


class _PathFilterClient(_Opt81Client):
    def __init__(self, paths_by_pr: dict[int, list[str]], *, yaml: str = _COLLECT_YAML,
                 missing_pr_numbers: set[int] | None = None):
        super().__init__([("ubuntu-latest", 197)] * 60, yaml)
        self.paths_by_pr = paths_by_pr
        self.missing_pr_numbers = missing_pr_numbers or set()
        self.changed_file_calls = 0

    def json(self, endpoint, allow_missing=False):
        response = super().json(endpoint, allow_missing)
        if response is not None:
            if isinstance(response, dict) and "workflow_runs" in response:
                for i, run in enumerate(response["workflow_runs"], 1):
                    run["pull_requests"] = (
                        [] if i in self.missing_pr_numbers else [{"number": i}])
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
        "affected_jobs": ["test"],
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

    assert finding.get("path_filter_measurement", {}).get("would_skip_prs") == 3
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


def test_opt32_changed_files_fetch_rejects_the_api_ceiling_as_incomplete():
    paths = {1: [f"docs/file-{i}.md" for i in range(3000)]}
    client = _PathFilterClient(paths)

    assert cr._opt32_changed_files(client, "o/r", 1) is None
    assert client.changed_file_calls == cr._OPT32_CHANGED_FILES_MAX_PAGES


def test_opt32_unmeasured_ceiling_stays_visible_through_collect_and_verifier(
        monkeypatch, tmp_path):
    paths = {n: [f"src/file{n}.py"] for n in range(1, 61)}
    paths[1] = [f"docs/file-{i}.md" for i in range(3000)]

    doc = _collect(monkeypatch, _PathFilterClient(paths))
    finding = _opt32(doc)

    assert finding.get("path_filter_measurement", {}).get("status") == "unmeasured"
    assert finding["path_filter_measurement"]["sampled_prs"] == 60
    report = _verify_full(doc, tmp_path)
    assert "hit rate not measured (changed-file list unavailable or incomplete)" in report
    assert "Add path filters" in report.split('<a id="also-noticed">', 1)[1]


def test_opt32_without_pr_numbers_stays_visible_through_collect_and_verifier(
        monkeypatch, tmp_path):
    client = _PathFilterClient({}, missing_pr_numbers=set(range(1, 61)))

    doc = _collect(monkeypatch, client)
    measurement = next((
        finding.get("path_filter_measurement")
        for finding in doc["findings"] if finding.get("pattern") == "OPT32"
    ), None)

    assert measurement is not None
    assert measurement["status"] == "unmeasured"
    assert measurement["reason"] == "no PR number on sampled runs"
    report = _verify_full(doc, tmp_path)
    assert "hit rate not measured (no PR number on sampled runs)" in report


def test_opt32_discloses_partial_pr_number_coverage_through_collect_and_verifier(
        monkeypatch, tmp_path):
    paths = {number: [f"src/file{number}.py"] for number in range(1, 31)}
    client = _PathFilterClient(paths, missing_pr_numbers=set(range(31, 61)))

    doc = _collect(monkeypatch, client)
    finding = _opt32(doc)

    assert finding["path_filter_measurement"]["sampled_runs"] == 60
    assert finding["path_filter_measurement"]["runs_with_pr_number"] == 30
    assert "Measured on 30 of 60 sampled runs" in finding["evidence"]
    report = _verify_full(doc, tmp_path)
    assert "Measured on 30 of 60 sampled runs" in report


def test_opt32_unmeasured_disclosure_is_never_cut_by_the_appendix_cap():
    billed = [
        {"id": f"h{i}", "pattern": f"OPT{i + 1}", "title": f"Hygiene {i}",
         "severity": "LOW", "workflow_file": ".github/workflows/ci.yml",
         "line": 10 + i, "affected_jobs": [f"job{i}"], "evidence": "measured",
         "runner_min_saving": 100.0 - i, "wall_clock_p50_s": 0.0}
        for i in range(bp._ALSO_NOTICED_CAP + 1)
    ]
    unmeasured = {
        "id": "path-filter", "pattern": "OPT32", "title": "Add path filters",
        "severity": "LOW", "workflow_file": ".github/workflows/path.yml",
        "line": 7, "affected_jobs": ["test"], "evidence": "pull requests run it",
        "runner_min_saving": None, "wall_clock_p50_s": 0.0,
        "path_filter_measurement": {
            "status": "unmeasured",
            "reason": "changed-file list unavailable or incomplete",
            "sampled_prs": 60,
        },
    }

    lines, _count, _has_wall_clock = bp._also_noticed_block(
        billed + [unmeasured], "https://example.test/catalog")
    report = "\n".join(lines)

    assert "OPT32 - Add path filters" in report
    assert "hit rate not measured (changed-file list unavailable or incomplete)" in report
    assert "more hygiene pattern(s)" in report


def test_opt32_unsupported_negative_glob_fails_closed():
    patterns = ["docs/**", "!docs/[ab]/**"]

    assert not cr._opt32_ignored_by_patterns("docs/a/guide.md", patterns)


def test_opt32_rates_each_workflow_from_its_own_sampled_prs(
        monkeypatch, tmp_path):
    a = ".github/workflows/a.yml"
    b = ".github/workflows/b.yml"

    class TwoWorkflowClient(_PathFilterClient):
        def json(self, endpoint, allow_missing=False):
            self.queries += 1
            if endpoint.startswith("repos/o/r/actions/workflows?"):
                return {"workflows": [
                    {"id": 1, "path": a, "name": "A"},
                    {"id": 2, "path": b, "name": "B"},
                ]}
            match = re.match(r"repos/o/r/actions/workflows/([12])/runs\?(.*)", endpoint)
            if match:
                workflow_id = int(match.group(1))
                if re.search(r"per_page=1(?![0-9])", match.group(2)):
                    return {"total_count": 30}
                offset = 0 if workflow_id == 1 else 10
                return {"workflow_runs": [
                    {"id": workflow_id * 100 + i, "event": "pull_request",
                     "head_sha": f"s{workflow_id}-{i}",
                     "created_at": _cts(i * 1000),
                     "run_started_at": _cts(i * 1000),
                     "updated_at": _cts(i * 1000 + 197),
                     "conclusion": "success", "status": "completed", "run_attempt": 1,
                     "pull_requests": [{"number": offset + i + 1}]}
                    for i in range(10)]}
            match = re.match(r"repos/o/r/actions/runs/([12])(\d\d)/jobs", endpoint)
            if match:
                i = int(match.group(2))
                job = self._job(i)
                job["id"] += int(match.group(1)) * 10000
                job["run_id"] = int(match.group(1)) * 100 + i
                return {"jobs": [job]}
            match = re.match(r"repos/o/r/commits/s([12])-(\d+)/check-runs", endpoint)
            if match:
                job = self._job(int(match.group(2)))
                return {"check_runs": [{"name": "test", "started_at": job["started_at"],
                                         "completed_at": job["completed_at"]}]}
            if endpoint.startswith(f"repos/o/r/contents/{a}") or endpoint.startswith(
                    f"repos/o/r/contents/{b}"):
                return {"content": base64.b64encode(_COLLECT_YAML.encode()).decode()}
            if endpoint == "repos/o/r":
                return {"default_branch": "main"}
            match = re.fullmatch(
                r"repos/o/r/pulls/(\d+)/files\?per_page=100(?:&page=(\d+))?", endpoint)
            if match:
                self.changed_file_calls += 1
                number = int(match.group(1))
                return [{"filename": "README.md" if number <= 10 else "src/app.py"}]
            return None

    client = TwoWorkflowClient({}, yaml=_COLLECT_YAML)
    monkeypatch.setattr(cr, "GhClient", lambda *args, **kwargs: client)
    doc = cr.collect({"repo": "o/r", "findings": [
        {"id": "fa", "pattern": "OPT32", "workflow_file": a,
         "title": "Filter A", "evidence": "A runs", "affected_jobs": ["test"]},
        {"id": "fb", "pattern": "OPT32", "workflow_file": b,
         "title": "Filter B", "evidence": "B runs", "affected_jobs": ["test"]},
    ]}, "o/r", max_runs=10, shallow_runs=10)

    by_workflow = {f["workflow_file"]: f for f in doc["findings"]
                   if f.get("pattern") == "OPT32"}
    assert by_workflow[a]["path_filter_measurement"]["hit_rate"] == 1.0
    assert by_workflow[b]["path_filter_measurement"]["hit_rate"] == 0.0
    assert client.changed_file_calls == 20
    _verify_full(doc, tmp_path)

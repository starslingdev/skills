"""Contracts for the maintainer-only real-repository golden reports."""

from __future__ import annotations

import ast
import gzip
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[1] / "golden"
_REPOSITORIES = {
    "home-assistant-core": (
        "the reviewed Bottom line remains open",
        (
            # Owner-reviewed headline text is handled by the pole-order card.
            "Bottom line does not contain reviewed text",
        ),
    ),
    "vercel-nextjs": (
        "the reviewed Bottom line remains open",
        (
            # Owner-reviewed headline text is handled by the pole-order card.
            "Bottom line does not contain reviewed text",
        ),
    ),
    "ethereum-go-ethereum": (
        "a true lint-to-test predecessor chain is currently reported as redundant setup",
        (
            # The workflow's real dependency edge is still classified as OPT21.
            "forbidden finding present: OPT21",
            # Owner-reviewed headline text waits for that classification fix.
            "Bottom line does not contain reviewed text",
        ),
    ),
    "microsoft-playwright": (
        "the reviewed Bottom line remains open",
        (
            # Owner-reviewed headline text is handled by the pole-order card.
            "Bottom line does not contain reviewed text",
        ),
    ),
}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_only_known_differences(differences: list[str], allowed: tuple[str, ...]) -> None:
    assert all(any(marker in row for marker in allowed) for row in differences), differences
    assert all(any(marker in row for row in differences) for marker in allowed), differences


def test_golden_harness_has_an_independent_oracle_and_recorder() -> None:
    missing = [
        name
        for name in ("oracle.py", "record.py", "compare.py")
        if not (_ROOT / name).is_file()
    ]

    assert not missing, f"missing golden-report tools: {', '.join(missing)}"


def test_recorder_redacts_unrelated_repository_links_from_checkout(tmp_path: Path) -> None:
    recorder = _load("record")
    snapshot = tmp_path / "checkout"
    workflow = snapshot / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "source: https://github.com/public/source\n"
        "dependency: https://github.com/another/public-repository/tree/main\n"
        "issue: another/public-repository#123\n",
        encoding="utf-8",
    )

    recorder._sanitize_checkout_references(snapshot, "public/source")

    sanitized = workflow.read_text(encoding="utf-8")
    assert "https://github.com/public/source" not in sanitized
    assert "another/public-repository" not in sanitized
    assert sanitized.count("<external-public-repository>") == 3
    assert "123" not in sanitized


def test_recorder_rejects_a_workflow_link_outside_the_checkout(tmp_path: Path) -> None:
    recorder = _load("record")
    checkout = tmp_path / "checkout"
    workflows = checkout / ".github" / "workflows"
    workflows.mkdir(parents=True)
    outside = tmp_path / "credential.yml"
    outside.write_text("credential: should-not-be-copied\n", encoding="utf-8")
    (workflows / "ci.yml").symlink_to(outside)

    with pytest.raises(ValueError, match="outside checkout"):
        recorder._reject_external_symlinks(checkout, [workflows])


def test_reference_oracle_does_not_import_the_engine() -> None:
    source = (_ROOT / "oracle.py").read_text(encoding="utf-8")
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        str(node.module or "").split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    )
    assert imported.isdisjoint({"collect_runs", "blocking_path", "scan"})


def test_reference_oracle_uses_month_wide_last_job_facts_and_ignores_bookkeeping() -> None:
    oracle = _load("oracle")
    recording = {
        "schema_version": 1,
        "repository": "public/example",
        "window": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-31T00:00:00Z"},
        "pr_commits": [
            {
                "sha": f"sha{day}",
                "created_at": f"2026-10-0{day}T00:00:00Z",
                "completed": True,
                "jobs": [
                    {"workflow": "CI", "name": "tests", "status": "completed",
                     "started_at": f"2026-10-0{day}T00:00:00Z",
                     "completed_at": f"2026-10-0{day}T00:04:00Z"},
                    {"workflow": "CI", "name": "cleanup", "status": "completed",
                     "started_at": f"2026-10-0{day}T00:04:00Z",
                     "completed_at": f"2026-10-0{day}T00:04:30Z"},
                ],
            }
            for day in (1, 2, 3)
        ] + [{
            "sha": "sha4",
            "created_at": "2026-10-04T00:00:00Z",
            "completed": True,
            "jobs": [
                {"workflow": "Lint", "name": "lint", "status": "completed",
                 "started_at": "2026-10-04T00:00:00Z", "completed_at": "2026-10-04T00:05:00Z"},
            ],
        }],
    }

    facts = oracle.compute(recording, bookkeeping_cutoff_s=60)

    assert facts["sample"]["completed_with_eligible_jobs"] == 4
    assert facts["sample"]["distinct_days"] == 4
    assert facts["gate_order"] == ["CI / tests", "Lint / lint"]
    assert facts["gates"][0]["last_on"] == 3
    assert facts["bookkeeping_jobs"] == [{"check": "CI / cleanup", "excluded_on": 3}]


def test_reference_wait_starts_at_the_earliest_recorded_run() -> None:
    oracle = _load("oracle")
    recording = {
        "schema_version": 1,
        "repository": "public/example",
        "pr_commits": [{
            "created_at": "2026-10-01T00:00:00Z",
            "completed": True,
            "runs": [
                {"created_at": "2026-10-01T00:01:00Z"},
                {"created_at": "2026-10-01T00:04:00Z"},
            ],
            "jobs": [{
                "workflow": "CI", "name": "tests", "status": "completed",
                "started_at": "2026-10-01T00:05:00Z",
                "completed_at": "2026-10-01T00:11:00Z",
            }],
        }],
    }

    facts = oracle.compute(recording)

    assert facts["typical_pr_wait_s"] == 600.0


def test_comparison_rejects_a_small_or_misordered_engine_sample() -> None:
    compare = _load("compare")
    expected = {
        "oracle": {
            "sample": {"completed_with_eligible_jobs": 120},
            "gate_order": ["CI / tests", "Lint / lint"],
        },
        "human_review": {"findings_must_appear": [], "findings_must_not_appear": []},
    }
    findings = {
        "pr_critical_path": {
            "sampled_pr_count": 20,
            "checks": [
                {"name": "Lint / lint", "pole_n": 8, "p50_s": 100},
                {"name": "CI / tests", "pole_n": 2, "p50_s": 200},
            ],
        },
        "findings": [],
    }

    differences = compare.compare(findings, expected)

    assert any("sampled 20" in row for row in differences)
    assert any("gate order differs" in row for row in differences)


def test_recorder_caps_clean_logs_and_deletes_secret_bearing_logs(tmp_path: Path) -> None:
    recorder = _load("record")
    clean = tmp_path / "clean.txt"
    secret = tmp_path / "secret.txt"
    clean.write_text("ordinary public output\n" * 20, encoding="utf-8")
    secret.write_text("Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")

    skipped = recorder._scan_and_cap_logs(tmp_path, 64)

    assert skipped == ["secret.txt"]
    assert not secret.exists()
    assert clean.stat().st_size <= 64


def test_recorder_never_creates_an_expected_file() -> None:
    source = (_ROOT / "record.py").read_text(encoding="utf-8")
    assert "expected.json" not in source


def test_recorder_rewrites_long_api_ids_consistently(tmp_path: Path) -> None:
    recorder = _load("record")
    api = tmp_path / "api"
    api.mkdir()
    run_id = "12345678" + "901"
    job_id = "99887766" + "554"
    first = api / f"actions_runs_{run_id}_jobs.json"
    second = api / "runs.json"
    first.write_text(f'{{"id": {run_id}, "job_id": {job_id}}}', encoding="utf-8")
    second.write_text(f'{{"run_id": {run_id}}}', encoding="utf-8")

    recorder._sanitize_api_identifiers(api)

    joined = "\n".join(path.name + path.read_text(encoding="utf-8") for path in api.iterdir())
    assert run_id not in joined
    assert job_id not in joined
    replacement = next(token for token in joined.replace("{", " ").replace("}", " ").replace(":", " ").replace(",", " ").split() if token.isdigit())
    assert len(replacement) <= 8
    assert joined.count(replacement) >= 2


def test_replay_exposes_month_wide_runs_and_jobs_as_deeper_api_pages(tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    api = recording / "api"
    api.mkdir(parents=True)
    (recording / "manifest.json").write_text(
        json.dumps({"repository": "public/example"}), encoding="utf-8"
    )

    def fixture(endpoint: str, payload: dict[str, object]) -> None:
        with gzip.open(api / (compare._fixture_name(endpoint) + ".gz"), "wt", encoding="utf-8") as stream:
            json.dump(payload, stream)

    fixture(
        "repos/public/example/actions/workflows?per_page=100",
        {"workflows": [{"id": 1234, "name": "CI", "path": ".github/workflows/ci.yml"}]},
    )
    fixture(
        "repos/public/example/actions/workflows/1234/runs?per_page=100",
        {"workflow_runs": [{"head_sha": "newer-live-page"}]},
    )
    month = {
        "pr_commits": [{
            "sha": "commit-000001", "created_at": "2026-11-01T00:00:00Z", "completed": True,
            "jobs": [{"workflow": "CI", "name": f"tests-{index}", "status": "completed",
                      "conclusion": "success", "started_at": "2026-11-01T00:00:00Z",
                      "completed_at": "2026-11-01T00:02:00Z"}
                     for index in range(101)],
        }],
    }
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump(month, stream)

    replay = tmp_path / "replay"
    compare._dynamic_replay_copy(recording, replay)

    page = json.loads((replay / compare._fixture_name(
        "repos/public/example/actions/workflows/1234/runs?per_page=100"
    )).read_text(encoding="utf-8"))
    assert [run["head_sha"] for run in page["workflow_runs"]] == [
        "newer-live-page", "commit-000001"]
    run_id = page["workflow_runs"][1]["id"]
    jobs = json.loads((replay / compare._fixture_name(
        f"repos/public/example/actions/runs/{run_id}/jobs?per_page=100"
    )).read_text(encoding="utf-8"))
    assert len(jobs["jobs"]) == 100
    jobs_page_2 = json.loads((replay / compare._fixture_name(
        f"repos/public/example/actions/runs/{run_id}/jobs?per_page=100&page=2"
    )).read_text(encoding="utf-8"))
    assert [job["name"] for job in jobs_page_2["jobs"]] == ["tests-100"]


def test_replay_records_pull_request_pages_for_every_workflow(tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    api = recording / "api"
    api.mkdir(parents=True)
    (recording / "manifest.json").write_text(
        json.dumps({"repository": "public/example"}), encoding="utf-8"
    )

    def fixture(endpoint: str, payload: dict[str, object]) -> None:
        with gzip.open(api / (compare._fixture_name(endpoint) + ".gz"),
                       "wt", encoding="utf-8") as stream:
            json.dump(payload, stream)

    fixture("repos/public/example/actions/workflows?per_page=100", {
        "workflows": [
            {"id": 11, "name": "CI", "path": ".github/workflows/ci.yml"},
            {"id": 12, "name": "Docs", "path": ".github/workflows/docs.yml"},
        ],
    })
    for workflow_id in (11, 12):
        fixture(f"repos/public/example/actions/workflows/{workflow_id}/runs?per_page=100", {
            "workflow_runs": [],
        })
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump({"pr_commits": []}, stream)

    replay = tmp_path / "replay"
    compare._dynamic_replay_copy(recording, replay)

    for workflow_id in (11, 12):
        assert (replay / compare._fixture_name(
            f"repos/public/example/actions/workflows/{workflow_id}/runs"
            "?per_page=100&event=pull_request"
        )).is_file()


def test_engine_span_oracle_uses_every_recorded_run_without_engine_filtering(
        tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    recording.mkdir()
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump({
            "schema_version": 1,
            "pr_commits": [{
                "sha": "commit-1",
                "created_at": "2026-11-01T00:00:00Z",
                "completed": True,
                "runs": [
                    {"run_key": "docs-run", "workflow": "Docs",
                     "created_at": "2026-11-01T00:00:00Z"},
                    {"run_key": "ci-run", "workflow": "CI",
                     "created_at": "2026-11-01T00:10:00Z"},
                ],
                "jobs": [
                    {"run_key": "docs-run", "workflow": "Docs", "name": "docs",
                     "started_at": "2026-11-01T00:00:00Z",
                     "completed_at": "2026-11-01T00:01:00Z"},
                    {"run_key": "ci-run", "workflow": "CI", "name": "gate",
                     "started_at": "2026-11-01T00:10:00Z",
                     "completed_at": "2026-11-01T00:20:00Z"},
                ],
            }],
        }, stream)
    findings = {"pr_critical_path": {
        "checks": [{"name": "CI / gate", "required_reachable": True}],
        "chain_facts": [{"sha": "commit-1", "chain": ["CI / gate"]}],
    }}

    oracle = compare._oracle_for_engine_span(recording, findings)

    assert oracle["typical_pr_wait_s"] == 1200


def test_engine_span_oracle_derives_population_and_gates_without_chain_facts(
        tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    recording.mkdir()
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump({
            "schema_version": 1,
            "pr_commits": [
                {
                    "sha": "commit-1", "created_at": "2026-11-01T00:00:00Z",
                    "completed": True,
                    "jobs": [{
                        "workflow": "CI", "name": "test", "status": "completed",
                        "started_at": "2026-11-01T00:00:00Z",
                        "completed_at": "2026-11-01T00:10:00Z",
                    }],
                },
                {
                    "sha": "commit-2", "created_at": "2026-11-02T00:00:00Z",
                    "completed": True,
                    "jobs": [{
                        "workflow": "Lint", "name": "lint", "status": "completed",
                        "started_at": "2026-11-02T00:00:00Z",
                        "completed_at": "2026-11-02T00:05:00Z",
                    }],
                },
            ],
        }, stream)
    findings = {"pr_critical_path": {
        "sample_span_first": "2026-11-01T00:00:00Z",
        "sample_span_last": "2026-11-02T00:00:00Z",
        "checks": [{"name": "invented / engine-only", "required_reachable": True}],
        "chain_facts": [{"sha": "commit-1", "chain": ["invented / engine-only"]}],
    }}

    oracle = compare._oracle_for_engine_span(recording, findings)

    assert oracle["sample"]["completed_with_eligible_jobs"] == 2
    assert oracle["gate_order"] == ["CI / test", "Lint / lint"]


def test_engine_span_oracle_matches_the_most_specific_nested_gate_cap(
        tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    recording.mkdir()
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump({
            "schema_version": 1,
            "pr_commits": [{
                "sha": "commit-1", "created_at": "2026-11-01T00:00:00Z",
                "completed": True,
                "jobs": [
                    {"workflow": "CI", "name": "test / build",
                     "status": "completed",
                     "started_at": "2026-11-01T00:00:00Z",
                     "completed_at": "2026-11-01T00:03:20Z"},
                    {"workflow": "CI", "name": "lint",
                     "status": "completed",
                     "started_at": "2026-11-01T00:00:00Z",
                     "completed_at": "2026-11-01T00:02:00Z"},
                ],
            }],
        }, stream)
    findings = {"pr_critical_path": {
        "sample_span_first": "2026-11-01T00:00:00Z",
        "sample_span_last": "2026-11-01T00:00:00Z",
        "checks": [
            {"name": "test / build", "p50_s": 100},
            {"name": "build", "p50_s": 50},
            {"name": "lint", "p50_s": 120},
        ],
    }}

    oracle = compare._oracle_for_engine_span(recording, findings)

    assert oracle["gate_order"][0] == "CI / lint"


def test_engine_span_oracle_rederives_the_representative_required_pr_type(
        tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    recording.mkdir()
    commits = []
    for index in range(4):
        jobs = [
            {"workflow": "CI", "name": "common gate", "status": "completed",
             "started_at": f"2026-11-0{index + 1}T00:00:00Z",
             "completed_at": f"2026-11-0{index + 1}T00:02:00Z"},
        ]
        if index == 3:
            jobs.append({
                "workflow": "Release", "name": "release gate", "status": "completed",
                "started_at": "2026-11-04T00:00:00Z",
                "completed_at": "2026-11-04T00:20:00Z",
            })
        commits.append({
            "sha": f"commit-{index}",
            "created_at": f"2026-11-0{index + 1}T00:00:00Z",
            "completed": True,
            "jobs": jobs,
        })
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "pr_commits": commits}, stream)
    findings = {
        "required_checks": ["common gate", "release gate"],
        "pr_critical_path": {
            "sample_span_first": "2026-11-01T00:00:00Z",
            "sample_span_last": "2026-11-04T00:00:00Z",
            "sampled_pr_count": 4,
            "sample_partial_count": 1,
            "sample_job_list_cap_hit": True,
            "check_present_n_pr": 3,
            "checks": [
                {"name": "common gate", "p50_s": 120},
                {"name": "release gate", "p50_s": 1200},
            ],
        },
    }

    oracle = compare._oracle_for_engine_span(recording, findings)

    assert oracle["sample"]["completed_with_eligible_jobs"] == 3
    assert oracle["gate_order"] == ["CI / common gate"]


def test_replay_preserves_run_identity_timestamps_and_missing_job_lists(
        tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    api = recording / "api"
    api.mkdir(parents=True)
    (recording / "manifest.json").write_text(
        json.dumps({"repository": "public/example"}), encoding="utf-8")

    def fixture(endpoint: str, payload: dict[str, object]) -> None:
        target = api / (compare._fixture_name(endpoint) + ".gz")
        with gzip.open(target, "wt", encoding="utf-8") as stream:
            json.dump(payload, stream)

    fixture(
        "repos/public/example/actions/workflows?per_page=100",
        {"workflows": [
            {"id": 11, "name": "CI", "path": ".github/workflows/ci.yml"},
            {"id": 12, "name": "Docs", "path": ".github/workflows/docs.yml"},
        ]},
    )
    for workflow_id in (11, 12):
        fixture(
            f"repos/public/example/actions/workflows/{workflow_id}/runs?per_page=100",
            {"workflow_runs": [{
                "head_sha": f"newer-{workflow_id}",
                "created_at": "2026-11-02T00:00:00Z",
            }]},
        )
    month = {"pr_commits": [{
        "sha": "commit-000001", "created_at": "2026-11-01T00:00:00Z",
        "completed": True,
        "runs": [
            {"run_key": "run-ci-1", "workflow": "CI", "status": "completed",
             "created_at": "2026-11-01T00:00:00Z",
             "updated_at": "2026-11-01T00:05:00Z", "jobs_recorded": True},
            {"run_key": "run-ci-2", "workflow": "CI", "status": "completed",
             "created_at": "2026-11-01T01:00:00Z",
             "updated_at": "2026-11-01T01:08:00Z", "jobs_recorded": True},
            {"run_key": "run-docs", "workflow": "Docs", "status": "completed",
             "created_at": "2026-11-01T02:00:00Z",
             "updated_at": "2026-11-01T02:01:00Z", "jobs_recorded": False},
        ],
        "jobs": [
            {"run_key": "run-ci-1", "workflow": "CI", "name": "first",
             "status": "completed", "conclusion": "success",
             "started_at": "2026-11-01T00:00:10Z",
             "completed_at": "2026-11-01T00:05:00Z"},
            {"run_key": "run-ci-2", "workflow": "CI", "name": "second",
             "status": "completed", "conclusion": "success",
             "started_at": "2026-11-01T01:00:10Z",
             "completed_at": "2026-11-01T01:08:00Z"},
        ],
    }]}
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump(month, stream)

    replay = tmp_path / "replay"
    compare._dynamic_replay_copy(recording, replay)

    ci_page = json.loads((replay / compare._fixture_name(
        "repos/public/example/actions/workflows/11/runs?per_page=100"
    )).read_text(encoding="utf-8"))["workflow_runs"][1:]
    docs_page = json.loads((replay / compare._fixture_name(
        "repos/public/example/actions/workflows/12/runs?per_page=100"
    )).read_text(encoding="utf-8"))["workflow_runs"][1:]
    assert [row["created_at"] for row in ci_page] == [
        "2026-11-01T01:00:00Z", "2026-11-01T00:00:00Z"]
    assert {row["head_sha"] for row in ci_page + docs_page} == {"commit-000001"}
    first_jobs = json.loads((replay / compare._fixture_name(
        f"repos/public/example/actions/runs/{ci_page[0]['id']}/jobs?per_page=100"
    )).read_text(encoding="utf-8"))["jobs"]
    second_jobs = json.loads((replay / compare._fixture_name(
        f"repos/public/example/actions/runs/{ci_page[1]['id']}/jobs?per_page=100"
    )).read_text(encoding="utf-8"))["jobs"]
    assert [job["name"] for job in first_jobs] == ["second"]
    assert [job["name"] for job in second_jobs] == ["first"]
    assert not (replay / compare._fixture_name(
        f"repos/public/example/actions/runs/{docs_page[0]['id']}/jobs?per_page=100"
    )).exists()


def test_public_month_history_tags_jobs_with_their_run() -> None:
    recorder = _load("record")
    public = recorder._public_history({"pr_commits": [{
        "sha": "private-sha", "created_at": "2026-11-01T00:00:00Z",
        "completed": True,
        "runs": [{"id": 101, "workflow": "CI", "jobs_recorded": True}],
        "jobs": [{"id": 201, "run_id": 101, "workflow": "CI", "name": "test"}],
    }]})

    commit = public["pr_commits"][0]
    assert commit["sha"] == "commit-000001"
    assert commit["runs"][0]["run_key"] == commit["jobs"][0]["run_key"]
    assert commit["runs"][0]["jobs_recorded"] is True


def test_replay_uses_one_commit_identity_on_copied_and_synthetic_pages(
        tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    api = recording / "api"
    api.mkdir(parents=True)
    (recording / "manifest.json").write_text(
        json.dumps({"repository": "public/example"}), encoding="utf-8")

    def fixture(endpoint: str, payload: dict[str, object]) -> None:
        with gzip.open(api / (compare._fixture_name(endpoint) + ".gz"),
                       "wt", encoding="utf-8") as stream:
            json.dump(payload, stream)

    fixture("repos/public/example/actions/workflows?per_page=100", {
        "workflows": [{"id": 11, "name": "CI", "path": ".github/workflows/ci.yml"}],
    })
    fixture("repos/public/example/actions/workflows/11/runs?per_page=100", {
        "workflow_runs": [{
            "id": 99, "head_sha": "real-sha",
            "created_at": "2026-11-01T00:00:00Z",
            "head_commit": {"id": "real-sha"},
        }],
    })
    fixture("repos/public/example/commits/real-sha/check-runs?per_page=100", {
        "check_runs": [],
    })
    fixture("repos/public/example/actions/runs/99/jobs?per_page=100", {
        "total_count": 1,
        "jobs": [{
            "id": 100, "name": "test", "status": "completed",
            "conclusion": "success",
            "started_at": "2026-11-01T00:00:00Z",
            "completed_at": "2026-11-01T00:00:30Z",
            "steps": [{"name": "Run test", "number": 1}],
        }],
    })
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump({"pr_commits": [{
            "sha": "commit-000001", "created_at": "2026-11-01T00:00:00Z",
            "completed": True,
            "runs": [{"workflow": "CI", "status": "completed",
                      "created_at": "2026-11-01T00:00:00Z",
                      "updated_at": "2026-11-01T00:01:00Z",
                      "jobs_recorded": True}],
            "jobs": [{
                "workflow": "CI", "name": "test", "status": "completed",
                "conclusion": "success",
                "started_at": "2026-11-01T00:00:00Z",
                "completed_at": "2026-11-01T00:01:00Z",
            }],
        }]}, stream)

    replay = tmp_path / "replay"
    compare._dynamic_replay_copy(recording, replay)
    page = json.loads((replay / compare._fixture_name(
        "repos/public/example/actions/workflows/11/runs?per_page=100"
    )).read_text(encoding="utf-8"))["workflow_runs"]
    assert page[0]["head_sha"] == page[0]["head_commit"]["id"] == "commit-000001"
    assert (replay / compare._fixture_name(
        "repos/public/example/commits/commit-000001/check-runs?per_page=100"
    )).is_file()
    copied_jobs = json.loads((replay / compare._fixture_name(
        "repos/public/example/actions/runs/99/jobs?per_page=100"
    )).read_text(encoding="utf-8"))["jobs"]
    assert [job["name"] for job in copied_jobs] == ["test"]
    assert copied_jobs[0]["completed_at"] == "2026-11-01T00:01:00Z"
    assert copied_jobs[0]["steps"] == [{"name": "Run test", "number": 1}]


def test_replay_does_not_merge_ambiguous_same_second_commits(tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    api = recording / "api"
    api.mkdir(parents=True)
    (recording / "manifest.json").write_text(
        json.dumps({"repository": "public/example"}), encoding="utf-8")

    def fixture(endpoint: str, payload: dict[str, object]) -> None:
        with gzip.open(api / (compare._fixture_name(endpoint) + ".gz"),
                       "wt", encoding="utf-8") as stream:
            json.dump(payload, stream)

    fixture("repos/public/example/actions/workflows?per_page=100", {
        "workflows": [{"id": 11, "name": "CI", "path": ".github/workflows/ci.yml"}],
    })
    fixture("repos/public/example/actions/workflows/11/runs?per_page=100", {
        "workflow_runs": [
            {"head_sha": "copied-a", "created_at": "2026-11-01T00:00:00Z"},
            {"head_sha": "copied-b", "created_at": "2026-11-01T00:00:00Z"},
        ],
    })
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump({"pr_commits": [
            {"sha": "commit-a", "completed": True, "runs": [{
                "workflow": "CI", "created_at": "2026-11-01T00:00:00Z"}], "jobs": []},
            {"sha": "commit-b", "completed": True, "runs": [{
                "workflow": "CI", "created_at": "2026-11-01T00:00:00Z"}], "jobs": []},
        ]}, stream)

    replay = tmp_path / "replay"
    compare._dynamic_replay_copy(recording, replay)

    page = json.loads((replay / compare._fixture_name(
        "repos/public/example/actions/workflows/11/runs?per_page=100"
    )).read_text(encoding="utf-8"))["workflow_runs"]
    assert [row["head_sha"] for row in page] == [
        "copied-a", "copied-b", "commit-a", "commit-b"]


def test_replay_keeps_distinct_runs_for_one_commit(tmp_path: Path) -> None:
    compare = _load("compare")
    recording = tmp_path / "recording"
    api = recording / "api"
    api.mkdir(parents=True)
    (recording / "manifest.json").write_text(
        json.dumps({"repository": "public/example"}), encoding="utf-8")

    def fixture(endpoint: str, payload: dict[str, object]) -> None:
        with gzip.open(api / (compare._fixture_name(endpoint) + ".gz"),
                       "wt", encoding="utf-8") as stream:
            json.dump(payload, stream)

    fixture("repos/public/example/actions/workflows?per_page=100", {
        "workflows": [{"id": 11, "name": "CI", "path": ".github/workflows/ci.yml"}],
    })
    fixture("repos/public/example/actions/workflows/11/runs?per_page=100", {
        "workflow_runs": [{
            "head_sha": "copied-sha", "created_at": "2026-11-01T02:00:00Z",
        }],
    })
    with gzip.open(recording / "month-wide.json.gz", "wt", encoding="utf-8") as stream:
        json.dump({"pr_commits": [{
            "sha": "commit-1", "completed": True,
            "runs": [
                {"run_key": "new", "workflow": "CI", "status": "completed",
                 "created_at": "2026-11-01T02:00:00Z", "jobs_recorded": True},
                {"run_key": "old", "workflow": "CI", "status": "completed",
                 "created_at": "2026-11-01T01:00:00Z", "jobs_recorded": True},
            ],
            "jobs": [{
                "run_key": "old", "workflow": "CI", "name": "test",
                "status": "completed", "conclusion": "success",
                "started_at": "2026-11-01T01:00:00Z",
                "completed_at": "2026-11-01T01:02:00Z",
            }],
        }]}, stream)

    replay = tmp_path / "replay"
    compare._dynamic_replay_copy(recording, replay)

    page = json.loads((replay / compare._fixture_name(
        "repos/public/example/actions/workflows/11/runs?per_page=100"
    )).read_text(encoding="utf-8"))["workflow_runs"]
    assert [row["created_at"] for row in page] == [
        "2026-11-01T02:00:00Z", "2026-11-01T02:00:00Z",
        "2026-11-01T01:00:00Z"]


def test_month_recording_job_list_reads_are_bounded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _load("record")
    assert recorder.MONTH_JOB_LIST_READ_CAP <= 400

    state_path = tmp_path / "month-wide.json"
    state_path.write_text(json.dumps({
        "run_inventory_complete": True,
        "pr_commits": [{
            "sha": f"sha-{run_id}",
            "created_at": "2026-11-01T00:00:00Z",
            "completed": True,
            "runs": [{
                "id": run_id,
                "workflow": "CI",
                "status": "completed",
                "created_at": "2026-11-01T00:00:00Z",
                "updated_at": "2026-11-01T00:02:00Z",
                "jobs_recorded": False,
            }],
            "jobs": [],
        } for run_id in range(1, 4)],
    }), encoding="utf-8")
    calls: list[int] = []

    def jobs(endpoint: str, key: str) -> list[dict[str, object]]:
        del key
        run_id = int(endpoint.split("/runs/", 1)[1].split("/", 1)[0])
        calls.append(run_id)
        if run_id == 1 and calls.count(1) == 1:
            raise RuntimeError("temporary job-list failure")
        return [{
            "id": run_id + 100,
            "name": "test",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-11-01T00:00:00Z",
            "completed_at": "2026-11-01T00:02:00Z",
        }]

    monkeypatch.setattr(recorder, "MONTH_JOB_LIST_READ_CAP", 3)
    monkeypatch.setattr(recorder, "_pages", jobs)
    recorder._month_history(
        "public/example",
        datetime(2026, 11, 2, tzinfo=timezone.utc),
        state_path,
    )

    assert len(calls) <= recorder.MONTH_JOB_LIST_READ_CAP


def test_every_proposed_expectation_waits_for_owner_signoff() -> None:
    expected_files = sorted((_ROOT / "repositories").glob("*/expected.json"))
    assert len(expected_files) == 4
    for path in expected_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["review_status"] == "awaiting owner sign-off"
        assert payload["repository"]
        assert payload["oracle"]
        oracle = json.loads(path.with_name("oracle.json").read_text(encoding="utf-8"))
        assert payload["oracle"] == oracle
        manifest = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
        sanitization = manifest["checkout_sanitization"]
        assert len(sanitization["snapshot_sha256"].replace("-", "")) == 64
        assert sanitization["repository_links"]
        assert sanitization["long_decimal_runs"]
        assert sanitization["environment_paths"]


def test_comparison_rejects_wrong_month_wide_measurements() -> None:
    compare = _load("compare")
    findings = {
        "pr_critical_path": {
            "sampled_pr_count": 100,
            "commit_wall_clock": {"p50_s": 900},
            "checks": [{"name": "CI / tests", "pole_n": 10, "p50_s": 900}],
        },
        "findings": [],
    }
    expected = {
        "oracle": {
            "sample": {"completed_with_eligible_jobs": 100},
            "typical_pr_wait_s": 100,
            "gate_order": ["CI / tests", "CI / lint"],
            "gates": [
                {"check": "CI / tests", "last_on": 90, "out_of": 100,
                 "share": 0.9, "p50_s": 100},
                {"check": "CI / lint", "last_on": 50, "out_of": 100,
                 "share": 0.5, "p50_s": 80},
            ],
        },
        "human_review": {},
    }

    differences = compare.compare(findings, expected)

    assert any("typical PR wait differs" in row for row in differences)
    assert any("gate share differs" in row for row in differences)
    assert any("gate p50 differs" in row for row in differences)
    assert any("frequent gate absent" in row for row in differences)


def test_comparison_keeps_the_reviewed_120_second_gate_p50_tolerance() -> None:
    compare = _load("compare")
    findings = {
        "pr_critical_path": {
            "sampled_pr_count": 100,
            "checks": [{"name": "CI / tests", "pole_n": 100, "p50_s": 300}],
        },
        "findings": [],
    }
    expected = {"oracle": {
        "sample": {"completed_with_eligible_jobs": 100},
        "gate_order": ["CI / tests"],
        "gates": [{
            "check": "CI / tests", "last_on": 100, "out_of": 100,
            "share": 1.0, "p50_s": 100,
        }],
    }, "human_review": {}}

    differences = compare.compare(findings, expected)

    assert any("gate p50 differs" in row for row in differences)


def test_comparison_does_not_hide_order_changes_below_fifteen_percent() -> None:
    compare = _load("compare")
    findings = {
        "pr_critical_path": {
            "sampled_pr_count": 100,
            "checks": [
                {"name": "CI / second", "pole_n": 9, "p50_s": 90},
                {"name": "CI / first", "pole_n": 10, "p50_s": 100},
            ],
        },
        "findings": [],
    }
    expected = {"oracle": {
        "sample": {"completed_with_eligible_jobs": 100},
        "gate_order": ["CI / second", "CI / first"],
        "gates": [
            {"check": "CI / second", "last_on": 9, "out_of": 100,
             "share": 0.09, "p50_s": 90},
            {"check": "CI / first", "last_on": 10, "out_of": 100,
             "share": 0.10, "p50_s": 100},
        ],
    }, "human_review": {}}

    differences = compare.compare(findings, expected)

    assert any("gate order differs" in row for row in differences)


def test_bottom_line_expectation_does_not_match_a_later_section() -> None:
    compare = _load("compare")
    expected = {
        "oracle": {"sample": {"completed_with_eligible_jobs": 0}},
        "human_review": {"bottom_line_must_contain": "reviewed gate"},
    }
    report = (
        "> **Bottom line.** A different gate controls this sample.\n\n"
        "## Details\n\nThe reviewed gate appears only here.\n"
    )

    differences = compare.compare({"findings": []}, expected, report)

    assert differences == ["Bottom line does not contain reviewed text: 'reviewed gate'"]


def test_known_red_cannot_mask_an_unrelated_regression() -> None:
    with pytest.raises(AssertionError):
        _assert_only_known_differences(
            ["sampled 20 PR commits", "required finding absent: NEW"],
            ("sampled ",),
        )


@pytest.mark.parametrize("recording_name,known_open_bug", _REPOSITORIES.items())
def test_real_repository_report_matches_independent_expectation(
    recording_name: str, known_open_bug: tuple[str, str] | None, tmp_path: Path
) -> None:
    compare = _load("compare")
    recording = _ROOT / "repositories" / recording_name
    expected = json.loads((recording / "expected.json").read_text(encoding="utf-8"))

    findings, report = compare.replay(
        recording,
        tmp_path,
        repo_root=_ROOT.parents[2],
    )
    differences = compare.compare(
        findings, expected, report, recording=recording)

    if known_open_bug and differences:
        reason, allowed_differences = known_open_bug
        _assert_only_known_differences(differences, allowed_differences)
        pytest.xfail(reason + ":\n" + "\n".join(differences))
    if known_open_bug:
        pytest.fail(f"known open bug no longer reproduces: {known_open_bug[0]}")

    assert not differences, "\n".join(differences)


def _golden_files() -> list[Path]:
    return sorted(path for path in (_ROOT / "repositories").rglob("*") if path.is_file())


def _decoded(path: Path) -> str:
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return raw.decode("utf-8", errors="replace")


def test_identity_leak_detector_flags_people_and_spares_bots_and_placeholders() -> None:
    record = _load("record")
    person = {"login": "someone", "type": "User", "id": 1}
    commit = {"author": {"name": "Some One", "email": "someone@example.com"}}
    trailer = "fix\n\nCo-authored-by: Some One <someone@example.com>"
    assert record.identity_leaks(person) == ["$.login"]
    assert record.identity_leaks(commit) == ["$.author.name", "$.author.email (email)"]
    assert record.identity_leaks({"message": trailer}) == ["$.message (email)"]
    assert record.identity_leaks({"login": "dependabot[bot]", "type": "Bot"}) == []
    assert record.identity_leaks({"login": "SomeOrg", "type": "Organization"}) == []
    assert record.identity_leaks({"login": "contributor-0001", "type": "User"}) == []
    assert record.identity_leaks(
        {"name": "contributor", "email": "contributor@example.invalid"}
    ) == []
    assert record.identity_leaks({"ssh_url": "git@github.com:o/r.git"}) == []
    assert record.email_addresses("taskr@1.1.0.patch") == []


def test_golden_recordings_carry_no_contributor_identity() -> None:
    record = _load("record")
    leaks: list[str] = []
    for path in _golden_files():
        text = _decoded(path)
        relative = path.relative_to(_ROOT).as_posix()
        try:
            payload = json.loads(text)
        except ValueError:
            leaks.extend(f"{relative}: {address}" for address in record.email_addresses(text))
            continue
        owner = record.audited_owner(_ROOT / "repositories" / path.relative_to(_ROOT).parts[1])
        keep = frozenset({owner}) if owner else frozenset()
        leaks.extend(f"{relative}: {leak}" for leak in record.identity_leaks(payload, keep=keep))
    assert not leaks, f"{len(leaks)} contributor identities, e.g.:\n" + "\n".join(leaks[:10])


# Dependency manifests Dependabot scans for version and security updates.  A
# golden checkout is a frozen third-party snapshot: a bump would change the
# input its expected report was derived from.
_DEPENDABOT_MANIFESTS = frozenset({
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "pnpm-workspace.yaml", "bun.lockb", "bun.lock",
    "pyproject.toml", "poetry.lock", "Pipfile", "Pipfile.lock", "setup.py", "setup.cfg",
    "uv.lock", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
    "composer.json", "composer.lock", "pom.xml", "build.gradle", "build.gradle.kts",
    "mix.exs", "mix.lock", "pubspec.yaml", "Package.swift", "Dockerfile",
})


def test_golden_checkouts_hold_no_file_dependabot_would_update() -> None:
    offenders = [
        path.relative_to(_ROOT).as_posix()
        for path in _golden_files()
        if "checkout" in path.relative_to(_ROOT).parts
        and (path.name in _DEPENDABOT_MANIFESTS or path.name.startswith("requirements"))
    ]
    assert not offenders, "rename to <name>.golden: " + ", ".join(offenders)


def test_scrub_replaces_people_consistently_and_keeps_bots(tmp_path: Path) -> None:
    record = _load("record")
    run = {
        "actor": {"login": "someone", "type": "User", "html_url": "https://github.com/someone"},
        "triggering_actor": {"login": "dependabot[bot]", "type": "Bot"},
        "head_repository": {"full_name": "someone/project",
                            "owner": {"login": "someone", "type": "User"}},
        "head_commit": {
            "author": {"name": "Some One", "email": "someone@example.com"},
            "committer": {"name": "Some One", "email": "12+someone@users.noreply.github.com"},
            "message": "Merge pull request #1 from someone/fix\n\nSigned-off-by: Some One <someone@example.com>",
        },
        "commit": {"verification": {"payload": "author Some One <someone@example.com>", "signature": "x"}},
        "display_title": "Thanks @someone for the fix",
        "head_branch": "someone/fix-cache",
        "repository": {"full_name": "owner/project", "owner": {"login": "owner", "type": "User"}},
    }
    recording = tmp_path / "recording"
    (recording / "api").mkdir(parents=True)
    path = recording / "api" / "runs.json.gz"
    path.write_bytes(gzip.compress(json.dumps({"workflow_runs": [run]}).encode()))

    (recording / "manifest.json").write_text(json.dumps({"origin": "public-github:owner/project"}))
    (recording / "checkout").mkdir()
    pyproject = recording / "checkout" / "pyproject.toml.golden"
    pyproject.write_text('authors = [{email = "someone@example.com"}]\n', encoding="utf-8")

    assert record.audited_owner(recording) == "owner"
    assert record.scrub_recording(recording) == 2
    scrubbed = json.loads(gzip.decompress(path.read_bytes()))["workflow_runs"][0]

    # The audited repository's owner is a person here, and keeps its login so
    # upstream runs do not turn into forks.
    assert record.identity_leaks(scrubbed) == ["$.repository.owner.login"]
    assert record.identity_leaks(scrubbed, keep=frozenset({"owner"})) == []
    assert scrubbed["repository"]["full_name"] == "owner/project"
    assert scrubbed["display_title"] == "Thanks @contributor-0001 for the fix"
    assert scrubbed["head_branch"] == "contributor-0001/fix-cache"
    assert "someone@example.com" not in pyproject.read_text(encoding="utf-8")
    assert scrubbed["actor"]["login"] == scrubbed["head_repository"]["owner"]["login"] == "contributor-0001"
    assert scrubbed["actor"]["html_url"] == "https://github.com/contributor-0001"
    assert scrubbed["head_repository"]["full_name"] == "contributor-0001/project"
    assert scrubbed["triggering_actor"]["login"] == "dependabot[bot]"
    assert "someone" not in json.dumps(scrubbed) and "Some One" not in json.dumps(scrubbed)
    assert scrubbed["commit"]["verification"] == {"payload": None, "signature": None}
    assert record.scrub_recording(recording) == 0  # idempotent


def test_replay_restores_snapshot_manifest_names(tmp_path: Path) -> None:
    record = _load("record")
    compare = _load("compare")
    assert {name + record.SNAPSHOT_SUFFIX for name in record.SNAPSHOT_MANIFESTS}.isdisjoint(
        _DEPENDABOT_MANIFESTS
    )
    for name in record.SNAPSHOT_MANIFESTS:
        (tmp_path / (name + record.SNAPSHOT_SUFFIX)).write_text(name, encoding="utf-8")
    compare.restore_snapshot_names(tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(record.SNAPSHOT_MANIFESTS)
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == "package.json"

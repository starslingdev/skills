"""Contracts for the maintainer-only real-repository golden reports."""

from __future__ import annotations

import ast
import gzip
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[1] / "golden"
_REPOSITORIES = {
    "home-assistant-core": (
        "the busy PR stream can stop at the configuration-era door",
        (
            "sampled ",
            "full report verifier failed: the fetch plan matches its call sites",
            "full report verifier failed: runner-minute cost spine source block is re-derivable",
            "Bottom line does not contain reviewed text",
        ),
    ),
    "vercel-nextjs": (
        "the current Bottom line does not lead with the independently selected long pole",
        (
            "sampled ",
            "full report verifier failed: the fetch plan matches its call sites",
            "full report verifier failed: no spine-demoted pole carries the typical-gate framing",
            "full report verifier failed: default-branch push lane is isolated",
            "gate order differs:",
            "Bottom line does not contain reviewed text",
        ),
    ),
    "ethereum-go-ethereum": (
        "a true lint-to-test predecessor chain is currently reported as redundant setup",
        (
            "sampled ",
            "full report verifier failed: the fetch plan matches its call sites",
            "forbidden finding present: OPT21",
            "Bottom line does not contain reviewed text",
        ),
    ),
    "microsoft-playwright": (
        "the current Bottom line does not lead with the month-wide longest meaningful gate",
        (
            "sampled ",
            "full report verifier failed: headline reconciles a non-universal slowest check",
            "gate order differs:",
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
        "repos/public/example/actions/workflows/1234/runs?per_page=100&page=2"
    )).read_text(encoding="utf-8"))
    assert [run["head_sha"] for run in page["workflow_runs"]] == ["commit-000001"]
    run_id = page["workflow_runs"][0]["id"]
    jobs = json.loads((replay / compare._fixture_name(
        f"repos/public/example/actions/runs/{run_id}/jobs?per_page=100"
    )).read_text(encoding="utf-8"))
    assert len(jobs["jobs"]) == 100
    jobs_page_2 = json.loads((replay / compare._fixture_name(
        f"repos/public/example/actions/runs/{run_id}/jobs?per_page=100&page=2"
    )).read_text(encoding="utf-8"))
    assert [job["name"] for job in jobs_page_2["jobs"]] == ["tests-100"]


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
    differences = compare.compare(findings, expected, report)

    if known_open_bug and differences:
        reason, allowed_differences = known_open_bug
        _assert_only_known_differences(differences, allowed_differences)
        pytest.xfail(reason + ":\n" + "\n".join(differences))
    if known_open_bug:
        pytest.fail(f"known open bug no longer reproduces: {known_open_bug[0]}")

    assert not differences, "\n".join(differences)

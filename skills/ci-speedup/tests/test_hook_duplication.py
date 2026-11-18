from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import scan as scan_mod  # noqa: E402


_VR_SPEC = importlib.util.spec_from_file_location(
    "hook_duplication_verify_report", Path(__file__).with_name("verify_report.py"))
vr = importlib.util.module_from_spec(_VR_SPEC)
assert _VR_SPEC.loader is not None
sys.modules[_VR_SPEC.name] = vr
_VR_SPEC.loader.exec_module(vr)

_WORKFLOW = ".github/workflows/ci.yml"
_WORKFLOW_TEXT = """\
on: pull_request
jobs:
  checks:
    name: Checks
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Typecheck
        run: pnpm tsc --noEmit
      - name: Lint
        run: pnpm lint
      - name: Test
        run: pnpm test
"""


def _iso(seconds: int) -> str:
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


class _Client:
    gave_up = False

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0

    def available(self) -> bool:
        return True

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{"id": 1, "path": _WORKFLOW,
                                     "name": "ci", "state": "active"}]}
        match = re.match(r"repos/o/r/actions/workflows/1/runs\?(.*)", endpoint)
        if match:
            if re.search(r"per_page=1(?![0-9])", match.group(1)):
                return {"total_count": 2}
            return {"workflow_runs": [{
                "id": run_id, "event": "pull_request",
                "head_branch": f"feature-{run_id}", "head_sha": f"sha-{run_id}",
                "status": "completed", "conclusion": "success",
                "created_at": _iso(run_id - 101),
                "run_started_at": _iso(run_id - 101),
                "updated_at": _iso(300 + run_id - 101),
            } for run_id in (101, 102)]}
        match = re.match(r"repos/o/r/actions/runs/(101|102)/jobs", endpoint)
        if match:
            run_id = int(match.group(1))
            return {"jobs": [{
                "id": run_id * 10 + 1, "run_id": run_id, "name": "Checks",
                "conclusion": "success", "labels": ["ubuntu-latest"],
                "html_url": f"https://github.com/o/r/actions/runs/{run_id}/job/1",
                "started_at": _iso(0), "completed_at": _iso(300),
                "steps": [
                    {"name": "Checkout", "conclusion": "success",
                     "started_at": _iso(0), "completed_at": _iso(20)},
                    {"name": "Typecheck", "conclusion": "success",
                     "started_at": _iso(20), "completed_at": _iso(70)},
                    {"name": "Lint", "conclusion": "success",
                     "started_at": _iso(70), "completed_at": _iso(130)},
                    {"name": "Test", "conclusion": "success",
                     "started_at": _iso(130), "completed_at": _iso(200)},
                ],
            }]}
        if re.match(r"repos/o/r/commits/[^/]+/check-runs", endpoint):
            return {"check_runs": [{"name": "Checks", "started_at": _iso(0),
                                      "completed_at": _iso(300)}]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        if endpoint == "repos/o/r/rulesets?includes_parents=true":
            return []
        if endpoint == "repos/o/r/branches/main/protection/required_status_checks":
            return {"contexts": ["Checks"]}
        if endpoint == f"repos/o/r/contents/{_WORKFLOW}":
            return {"content": base64.b64encode(_WORKFLOW_TEXT.encode()).decode()}
        return None

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


def _collect(monkeypatch, tmp_path: Path, hook: str | None, *,
             client_cls: type[_Client] = _Client,
             package_scripts: dict[str, str] | None = None) -> tuple[dict, str]:
    workflow = tmp_path / _WORKFLOW
    workflow.parent.mkdir(parents=True)
    workflow.write_text(_WORKFLOW_TEXT, encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": package_scripts or {
        "tsc": "tsc", "lint": "eslint .", "test": "vitest run",
    }}), encoding="utf-8")
    if hook is not None:
        husky = tmp_path / ".husky" / "pre-push"
        husky.parent.mkdir()
        husky.write_text("#!/bin/sh\n" + hook + "\n", encoding="utf-8")

    monkeypatch.setattr(cr, "GhClient", client_cls)
    monkeypatch.setattr(cr, "_root_is_clone_of", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cr, "_workflows_are_dirty", lambda *_args, **_kwargs: False)
    doc = scan_mod.scan(tmp_path, SKILL / "references" / "patterns")
    doc["repo"] = "o/r"
    doc["skill_commit_sha"] = "installed:unversioned"
    out = cr.collect(doc, "o/r", max_runs=2, shallow_runs=2, root=tmp_path)
    report = bp.render(out, captured_at="2026-11-18T00:00:00Z")
    findings_path = tmp_path / "findings.json"
    report_path = tmp_path / "report.md"
    findings_path.write_text(json.dumps(out), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    assert bp._LAST_CLAIMS is not None
    report_path.with_name(report_path.name + ".claims.json").write_text(
        json.dumps(bp._LAST_CLAIMS.to_json()), encoding="utf-8")
    failures = [check for check in vr.run_checks(
        report, report_path, findings_path, skill_repo=None)
        if not check.skipped and not check.ok]
    assert failures == [], [(check.name, check.detail) for check in failures]
    return out, report


def test_husky_pre_push_work_repeated_in_pr_ci_is_measured(monkeypatch, tmp_path):
    doc, report = _collect(
        monkeypatch, tmp_path, "pnpm tsc --noEmit && pnpm lint && pnpm test")
    findings = [f for f in doc["findings"] if f.get("pattern") == "OPT105"]
    assert len(findings) == 1, "hook work repeated on the PR path needs a finding"
    finding = findings[0]
    assert finding["wall_clock_p50_s"] == 180.0
    assert finding["hook_duplication"]["job_share"] == pytest.approx(0.6)
    assert finding["hook_duplication"]["steps"] == ["Typecheck", "Lint", "Test"]
    assert finding["runner_min_basis"] == "measured_spine_billable"
    # The safe, input-keyed recipe leads; claim-based skipping is explicitly optional.
    safe = report.index("input-keyed")
    opt_in = report.index("opt-in")
    assert safe < opt_in
    for guard in ("tree hash", "lockfile", "tool versions", "same repository",
                  "fork", "merge queue", "default branch", "written by hand",
                  "--no-verify", "trust decision"):
        assert guard in report


@pytest.mark.parametrize("hook", [None, "pnpm format"])
def test_absent_or_different_hook_stays_quiet(monkeypatch, tmp_path, hook):
    doc, _report = _collect(monkeypatch, tmp_path, hook)
    assert not any(f.get("pattern") == "OPT105" for f in doc["findings"])


def test_every_supported_static_hook_surface_is_read(tmp_path):
    husky = tmp_path / ".husky" / "pre-push"
    husky.parent.mkdir()
    husky.write_text("pnpm lint\n", encoding="utf-8")
    githook = tmp_path / ".githooks" / "pre-commit"
    githook.parent.mkdir()
    githook.write_text("pnpm test\n", encoding="utf-8")
    (tmp_path / "lefthook.yml").write_text(
        "pre-push:\n  commands:\n    types:\n      run: pnpm tsc --noEmit\n",
        encoding="utf-8")
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: lint\n"
        "        entry: pnpm lint\n        pass_filenames: false\n",
        encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({
        "simple-git-hooks": {"pre-push": "pnpm test"},
    }), encoding="utf-8")

    rows = cr._opt105_config_commands(tmp_path)
    sources = {source for source, _command, _working_directory in rows}

    assert sources == {
        ".husky/pre-push", ".githooks/pre-commit", "lefthook.yml",
        ".pre-commit-config.yaml", "package.json#simple-git-hooks",
    }


def test_partial_step_match_does_not_credit_unrelated_work(monkeypatch, tmp_path):
    workflow = _WORKFLOW_TEXT.replace(
        "run: pnpm lint\n      - name: Test\n        run: pnpm test",
        "run: pnpm lint && pnpm test\n      - name: Test\n        run: pnpm format",
    )
    monkeypatch.setattr(sys.modules[__name__], "_WORKFLOW_TEXT", workflow)

    doc, _report = _collect(monkeypatch, tmp_path, "pnpm lint")

    assert not any(f.get("pattern") == "OPT105" for f in doc["findings"]), (
        "a partial command match must not credit the whole CI step")


def test_task_runner_filters_do_not_match_different_packages(monkeypatch, tmp_path):
    workflow = _WORKFLOW_TEXT.replace("pnpm lint", "pnpm --filter app-b lint")
    monkeypatch.setattr(sys.modules[__name__], "_WORKFLOW_TEXT", workflow)
    for package, command in (("app-a", "eslint app-a"), ("app-b", "eslint app-b")):
        folder = tmp_path / "apps" / package
        folder.mkdir(parents=True)
        (folder / "package.json").write_text(
            json.dumps({"name": package, "scripts": {"lint": command}}),
            encoding="utf-8",
        )

    doc, _report = _collect(monkeypatch, tmp_path, "pnpm --filter app-a lint")

    assert not any(f.get("pattern") == "OPT105" for f in doc["findings"]), (
        "different package filters must not collapse to the same command")


def test_typescript_project_flag_is_an_exact_match_not_a_package_filter(
        monkeypatch, tmp_path):
    command = "pnpm exec tsc --project tsconfig.json"
    workflow = _WORKFLOW_TEXT.replace("pnpm tsc --noEmit", command)
    monkeypatch.setattr(sys.modules[__name__], "_WORKFLOW_TEXT", workflow)

    doc, _report = _collect(monkeypatch, tmp_path, command)

    findings = [f for f in doc["findings"] if f.get("pattern") == "OPT105"]
    assert len(findings) == 1, (
        "TypeScript's --project selects a config, not a workspace package")
    assert findings[0]["hook_duplication"]["steps"] == ["Typecheck"]


def test_different_explicit_environments_do_not_match(monkeypatch, tmp_path):
    workflow = _WORKFLOW_TEXT.replace(
        "pnpm lint", "ESLINT_USE_FLAT_CONFIG=true pnpm lint")
    monkeypatch.setattr(sys.modules[__name__], "_WORKFLOW_TEXT", workflow)

    doc, _report = _collect(
        monkeypatch, tmp_path, "ESLINT_USE_FLAT_CONFIG=false pnpm lint")

    assert not any(f.get("pattern") == "OPT105" for f in doc["findings"]), (
        "commands with different explicit environments are not the same work")


def test_inner_environment_assignment_overrides_the_outer_value(monkeypatch, tmp_path):
    workflow = _WORKFLOW_TEXT.replace(
        "pnpm lint", "NODE_ENV=production pnpm lint")
    monkeypatch.setattr(sys.modules[__name__], "_WORKFLOW_TEXT", workflow)

    doc, _report = _collect(
        monkeypatch, tmp_path, "pnpm lint",
        package_scripts={
            "tsc": "tsc", "lint": "NODE_ENV=test eslint .", "test": "vitest run",
        },
    )

    assert any(f.get("pattern") == "OPT105" for f in doc["findings"]), (
        "the script's inner assignment is the final value on both sides")


def test_environment_assignment_order_does_not_change_the_match(monkeypatch, tmp_path):
    workflow = _WORKFLOW_TEXT.replace("pnpm lint", "A=1 B=2 pnpm lint")
    monkeypatch.setattr(sys.modules[__name__], "_WORKFLOW_TEXT", workflow)

    doc, _report = _collect(monkeypatch, tmp_path, "B=2 A=1 pnpm lint")

    assert any(f.get("pattern") == "OPT105" for f in doc["findings"]), (
        "independent environment assignments are compared by final value")


class _DuplicateStepNameClient(_Client):
    def json(self, endpoint: str, allow_missing: bool = False):
        payload = super().json(endpoint, allow_missing)
        if re.match(r"repos/o/r/actions/runs/(101|102)/jobs", endpoint):
            payload["jobs"][0]["steps"] = [
                {"name": "Checkout", "conclusion": "success",
                 "started_at": _iso(0), "completed_at": _iso(20)},
                {"name": "Check", "conclusion": "success",
                 "started_at": _iso(20), "completed_at": _iso(25)},
                {"name": "Check", "conclusion": "success",
                 "started_at": _iso(25), "completed_at": _iso(205)},
            ]
        return payload


def test_duplicate_step_names_cannot_borrow_another_steps_time(monkeypatch, tmp_path):
    workflow = _WORKFLOW_TEXT.replace("name: Lint", "name: Check").replace(
        "name: Test", "name: Check")
    monkeypatch.setattr(sys.modules[__name__], "_WORKFLOW_TEXT", workflow)

    doc, _report = _collect(
        monkeypatch, tmp_path, "pnpm lint", client_cls=_DuplicateStepNameClient)

    assert not any(f.get("pattern") == "OPT105" for f in doc["findings"]), (
        "a duplicate display name must not lend a slow step's time to a hook match")


def test_lefthook_root_is_not_discarded(monkeypatch, tmp_path):
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint apps/web"}}), encoding="utf-8")
    (tmp_path / "lefthook.yml").write_text(
        "pre-push:\n  commands:\n    lint:\n      root: apps/web\n      run: pnpm lint\n",
        encoding="utf-8",
    )

    doc, _report = _collect(monkeypatch, tmp_path, None)

    assert not any(f.get("pattern") == "OPT105" for f in doc["findings"]), (
        "a Lefthook command must resolve from its configured root")


def test_hook_reader_refuses_symlinks_outside_checkout(tmp_path):
    outside = tmp_path.parent / "outside-pre-push"
    outside.write_text("pnpm lint\n", encoding="utf-8")
    husky = tmp_path / ".husky"
    husky.mkdir()
    (husky / "pre-push").symlink_to(outside)

    assert cr._opt105_config_commands(tmp_path) == [], (
        "hook configuration must never be read through an escaping symlink")


def test_hook_directories_ignore_inactive_sample_files(tmp_path):
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    (hooks / "pre-push.sample").write_text("pnpm lint\n", encoding="utf-8")

    assert cr._opt105_config_commands(tmp_path) == [], (
        "a sample file Git never invokes is not a configured hook")


def test_pre_commit_default_filenames_are_not_treated_as_full_suite(tmp_path):
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: tests\n        entry: pytest\n",
        encoding="utf-8",
    )

    assert cr._opt105_config_commands(tmp_path) == [], (
        "pre-commit appends selected filenames unless pass_filenames is false")


def test_pre_commit_explicit_args_are_part_of_the_command(tmp_path):
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: tests\n"
        "        entry: pytest\n        args: [-q, tests/unit]\n"
        "        pass_filenames: false\n",
        encoding="utf-8",
    )

    rows = cr._opt105_config_commands(tmp_path)

    assert rows == [(".pre-commit-config.yaml", "pytest -q tests/unit", "")]


def test_yaml_alias_fanout_has_bounded_work(tmp_path):
    levels = ["seed: &seed [{run: pnpm lint}]"]
    previous = "seed"
    for index in range(12):
        current = f"level{index}"
        levels.append(f"{current}: &{current} [*{previous}, *{previous}]")
        previous = current
    levels.append(f"pre-push: *{previous}")
    (tmp_path / "lefthook.yml").write_text("\n".join(levels) + "\n", encoding="utf-8")

    started = time.monotonic()
    rows = cr._opt105_config_commands(tmp_path)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, "YAML aliases must not multiply traversal work"
    assert rows == [("lefthook.yml", "pnpm lint", "")]

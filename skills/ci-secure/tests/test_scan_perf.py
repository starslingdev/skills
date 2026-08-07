"""Tests for the scan.py performance work: concurrent activity prefetch
(Tier 1) and the per-phase timing emitted in the JSON (Tier 0)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS = str(_SKILL_DIR / "scripts")

# scan.py does `from config import ...`; a sibling skill also ships a `scan`
# module on the shared pythonpath, so evict cached modules and import with
# THIS skill's scripts/ first (mirrors test_report.py).
_saved = {m: sys.modules.pop(m, None) for m in ("config", "scan")}
sys.path.insert(0, _SCRIPTS)
try:
    import scan  # noqa: E402
finally:
    try:
        sys.path.remove(_SCRIPTS)
    except ValueError:
        pass
    for m, mod in _saved.items():
        if mod is not None:
            sys.modules[m] = mod
        else:
            sys.modules.pop(m, None)


def _mk_workflows(tmp_path: Path, names: list[str]) -> list[Path]:
    wfdir = tmp_path / ".github" / "workflows"
    wfdir.mkdir(parents=True, exist_ok=True)
    out = []
    for n in names:
        p = wfdir / n
        p.write_text("on: push\n", encoding="utf-8")
        out.append(p)
    return out


def test_prefetch_activity_enriches_every_workflow(tmp_path, monkeypatch) -> None:
    files = _mk_workflows(tmp_path, ["a.yml", "b.yml", "c.yml"])
    seen: list[str] = []

    def fake(repo: str, basename: str, **_kw) -> dict:
        seen.append(basename)
        return {"runs_30d": len(basename), "dormant": False}

    monkeypatch.setattr(scan, "fetch_workflow_activity", fake)
    cache = scan._prefetch_activity("o/r", files, tmp_path, max_workers=3)

    assert set(cache) == {
        ".github/workflows/a.yml", ".github/workflows/b.yml", ".github/workflows/c.yml",
    }
    assert cache[".github/workflows/a.yml"]["runs_30d"] == len("a.yml")
    assert sorted(seen) == ["a.yml", "b.yml", "c.yml"]  # every workflow fetched


def test_prefetch_activity_records_a_failure_as_unavailable(
    tmp_path, monkeypatch, caplog,
) -> None:
    """One workflow's gh failure must not abort the scan — and must not be
    swallowed into `{}` either.

    `{}` is the scan's encoding for "enrichment never ran" (no `--repo`), and
    report.py reads it as no-data rather than a failed check. Collapsing a
    rate-limited or exploded lookup into `{}` therefore renders a workflow
    nobody could check identically to one that was never checked — and the
    dormancy signal just goes quietly missing. The failure is recorded as
    `{"status": "unavailable", ...}`, which report.py surfaces as its own
    state, and logged at WARNING.
    """
    good, bad = _mk_workflows(tmp_path, ["good.yml", "bad.yml"])

    def fake(repo: str, basename: str, **_kw) -> dict:
        if basename == "bad.yml":
            raise RuntimeError("gh exploded")
        return {"runs_30d": 1}

    monkeypatch.setattr(scan, "fetch_workflow_activity", fake)
    with caplog.at_level("WARNING"):
        cache = scan._prefetch_activity("o/r", [good, bad], tmp_path)
    assert cache[".github/workflows/good.yml"] == {"runs_30d": 1}
    failed = cache[".github/workflows/bad.yml"]
    assert failed != {}, "a failed check must not read as 'never attempted'"
    assert failed["status"] == "unavailable"
    assert "gh exploded" in failed["reason"]
    assert any("bad.yml" in r.getMessage() for r in caplog.records)


def test_prefetch_activity_empty_input() -> None:
    assert scan._prefetch_activity("o/r", [], Path(".")) == {}


def test_scan_json_emits_timings(tmp_path) -> None:
    """The scan JSON carries a `timings` block (Tier-0 instrumentation)."""
    if subprocess.run([sys.executable, "-c", "import yaml"],
                      capture_output=True).returncode != 0:
        pytest.skip("PyYAML not installed in the test runner")
    result = subprocess.run(
        [sys.executable, str(_SKILL_DIR / "scripts" / "scan.py"),
         "--root", str(_SKILL_DIR / "evals" / "files" / "many-findings")],
        capture_output=True, text=True, check=True, timeout=60,
    )
    timings = json.loads(result.stdout).get("timings")
    assert timings is not None
    assert "scan_total_s" in timings and "activity_enrich_s" in timings
    # No --repo → no enrichment → 0s; total is a non-negative number.
    assert timings["activity_enrich_s"] == 0.0
    assert isinstance(timings["scan_total_s"], (int, float)) and timings["scan_total_s"] >= 0
    # scan.py is the always-runs anchor for end-to-end timing: it must stamp a
    # wall-clock `run_start_epoch` so report.py can compute total_run_s even on
    # a direct-scan run (no run.py driver). Without this, total_run_s silently
    # vanishes — the failure that actually happened on a real run.
    assert "run_start_epoch" in timings
    assert isinstance(timings["run_start_epoch"], (int, float)) and timings["run_start_epoch"] > 0


# ---------------------------------------------------------------------------
# Reusable (`workflow_call`-only) workflows: zero REGISTERED runs is unknown
# activity, not dormancy. Exposing repos: vercel/next.js
# (`pr_stack_optimizer.yml`), microsoft/playwright (`tests_docker.yml`).
# ---------------------------------------------------------------------------

def _wf_file(tmp_path: Path, name: str, body: str) -> Path:
    wfdir = tmp_path / ".github" / "workflows"
    wfdir.mkdir(parents=True, exist_ok=True)
    p = wfdir / name
    p.write_text(body, encoding="utf-8")
    return p


_REUSABLE_YAML = """\
name: PR Stack Optimizer
on:
  workflow_call:
    outputs:
      skip:
        value: ${{ jobs.check.outputs.skip }}
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


def test_workflow_call_only_is_recognised(tmp_path) -> None:
    reusable = _wf_file(tmp_path, "pr_stack_optimizer.yml", _REUSABLE_YAML)
    assert scan._is_workflow_call_only(reusable) is True
    # A file that ALSO has a real trigger starts runs of its own.
    both = _wf_file(
        tmp_path, "both.yml",
        "on:\n  push:\n  workflow_call:\njobs: {}\n",
    )
    assert scan._is_workflow_call_only(both) is False
    plain = _wf_file(tmp_path, "plain.yml", "on: push\njobs: {}\n")
    assert scan._is_workflow_call_only(plain) is False


def _stub_gh(monkeypatch, payload: str) -> None:
    """Serve one canned gh response — no network in tests."""
    import types
    fake = types.ModuleType("gh_utils")

    class GitHubAPIError(Exception):
        pass

    fake.GitHubAPIError = GitHubAPIError
    fake.run_gh_api = lambda endpoint, **kw: payload
    monkeypatch.setitem(sys.modules, "gh_utils", fake)


def test_empty_run_history_on_a_reusable_workflow_is_unknown_not_dormant(
    monkeypatch,
) -> None:
    """vercel/next.js's `pr_stack_optimizer.yml` executes on every pull
    request, but `workflow_call` runs are attributed to the CALLER, so its own
    runs endpoint answers 200 with `total_count: 0`. Calling that dormant put a
    "verify before prioritizing" note on a live HIGH finding and dropped the
    group from the `all` fix selection."""
    _stub_gh(monkeypatch, json.dumps({"total_count": 0, "workflow_runs": []}))
    activity = scan.fetch_workflow_activity(
        "vercel/next.js", "pr_stack_optimizer.yml", workflow_call_only=True,
    )
    assert activity.get("dormant") is not True
    assert activity["status"] == "unavailable"
    assert activity["reusable_workflow"] is True
    assert "attributes its runs to the calling workflow" in activity["reason"]


def test_a_reusable_workflow_with_registered_runs_keeps_its_real_data(
    monkeypatch,
) -> None:
    """The exception is narrow: only an EMPTY run list is unknowable. A
    reusable workflow GitHub does register runs for keeps its real numbers,
    dormancy verdict included."""
    _stub_gh(monkeypatch, json.dumps({
        "total_count": 1,
        "workflow_runs": [{"created_at": "2020-01-01T00:00:00Z"}],
    }))
    activity = scan.fetch_workflow_activity(
        "o/r", "reusable.yml", workflow_call_only=True,
    )
    assert activity["dormant"] is True
    assert activity["last_run"] == "2020-01-01T00:00:00Z"
    assert "status" not in activity


def test_a_normal_workflow_with_no_runs_is_still_dormant(monkeypatch) -> None:
    """The counter-guard: a file that CAN start its own runs and has none is
    genuinely dormant, exactly as before."""
    _stub_gh(monkeypatch, json.dumps({"total_count": 0, "workflow_runs": []}))
    activity = scan.fetch_workflow_activity("o/r", "old.yml")
    assert activity["dormant"] is True
    assert activity["last_run"] is None


def test_prefetch_passes_the_reusable_flag_per_workflow(
    tmp_path, monkeypatch,
) -> None:
    """The flag has to be computed from each workflow's own `on:` block — the
    prefetch only ever had the basename to work with."""
    _wf_file(tmp_path, "pr_stack_optimizer.yml", _REUSABLE_YAML)
    _wf_file(tmp_path, "ci.yml", "on: push\njobs: {}\n")
    files = sorted((tmp_path / ".github" / "workflows").iterdir())
    seen: dict[str, bool] = {}

    def fake(repo, basename, workflow_call_only=False, **_kw):
        seen[basename] = workflow_call_only
        return {"runs_30d": 0, "dormant": True}

    monkeypatch.setattr(scan, "fetch_workflow_activity", fake)
    scan._prefetch_activity("o/r", files, tmp_path)
    assert seen == {"pr_stack_optimizer.yml": True, "ci.yml": False}


def _load_report():
    """Import report.py the way test_report.py does (sibling-`config` dance)."""
    saved = sys.modules.pop("config", None)
    sys.modules.pop("report", None)
    sys.path.insert(0, _SCRIPTS)
    try:
        import report
        return report
    finally:
        try:
            sys.path.remove(_SCRIPTS)
        except ValueError:
            pass
        if saved is not None:
            sys.modules["config"] = saved
        else:
            sys.modules.pop("config", None)


def test_reusable_workflow_survives_from_the_gh_call_to_the_all_selection(
    tmp_path, monkeypatch,
) -> None:
    """The whole cascade in one test, because every hop of it was wrong: the
    empty run list on next.js's `pr_stack_optimizer.yml` became `dormant`,
    which printed "Every affected workflow is dormant … verify before
    prioritizing" over a live HIGH finding, and dropped the group from the
    render plan the `all` fix selection reads."""
    report = _load_report()
    reusable = _wf_file(tmp_path, "pr_stack_optimizer.yml", _REUSABLE_YAML)
    _stub_gh(monkeypatch, json.dumps({"total_count": 0, "workflow_runs": []}))
    activity = scan.fetch_workflow_activity(
        "vercel/next.js", reusable.name,
        workflow_call_only=scan._is_workflow_call_only(reusable),
    )
    finding = {
        "id": "f1", "pattern": "P14.10", "severity": "HIGH", "title": "t",
        "workflow_file": ".github/workflows/pr_stack_optimizer.yml",
        "line": 1, "evidence": "1: on:", "workflow_activity": activity,
    }
    payload = {"findings": [finding], "repo": "vercel/next.js",
               "scanned_workflows": 1}

    plan = report.render_plan_keys(dict(payload))
    assert plan == [{"pattern": "P14.10", "dormant": False}]

    md = report.render(dict(payload))
    assert "every affected workflow is dormant" not in md.lower()
    assert "verify before prioritizing" not in md
    assert "attributed to the calling workflow" in md

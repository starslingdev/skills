"""Regression: every rendered long pole is drilled, and no pole's text points at an
Audit link its own section does not render.

Bug (a 2026-11-03 dogfood run on a one-workflow repo with three required checks): the
data pass captured drill logs for TWO poles (`_DRILL_DISTINCT_MATRICES = 2`) while the
renderer draws up to FIVE (`blocking_path._TOP_WORKFLOWS`). Long pole 3 (`ci.yml` ▸
`test`, a required check) therefore rendered with no `🔗 Audit:` line, no captured
timeline and no cross-run check, yet its agent prompt said "open its log (the Audit
link)". The coding agent was sent to a link that does not exist, and `verify_report.py`
passed the report, although SKILL.md 5a requires every gating pole fully drilled.

Fix: the data pass and report share one bounded pole selector, and the prompt only
names "the Audit link" when the pole's section renders one. `verify_report.check_pole_audit_link_reference_resolves` FAILs any
pole whose text cites the Audit link without carrying the `🔗 Audit:` line.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_undrilled_pole_audit_link.py
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL_DIR / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import summary  # noqa: E402


def _load_verify_report():
    # `verify_report` is not a unique module name across skills: load THIS skill's by path.
    path = _SKILL_DIR / "tests" / "verify_report.py"
    name = "ci_speedup_verify_report_audit_link"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # register first: its @dataclass resolves __module__ here
    spec.loader.exec_module(mod)
    return mod


vr = _load_verify_report()

_WF = ".github/workflows/ci.yml"
_AUDIT_LINE = "**🔗 Audit:**"
_AUDIT_REF = "the Audit link"


# --------------------------------------------------------------------------- #
# Fixture: a one-workflow repo whose three jobs are three distinct gating checks
# (the dogfood shape: `browser guards` > `build guards` > `test`).
# --------------------------------------------------------------------------- #

def _iso(offset_s: int) -> str:
    base = _dt.datetime(2026, 10, 20, tzinfo=_dt.timezone.utc)
    return (base + _dt.timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


_JOBS = (("browser guards", 194, "Check rendered layout"),
         ("build guards", 179, "Verify built HTML"),
         ("test", 135, "Test"))


def _job(run_id: int, k: int, name: str, dur: int, dom: str) -> dict:
    jid = 50_000 + run_id * 10 + k
    setup = 10
    return {
        "id": jid, "name": name, "status": "completed", "conclusion": "success",
        "run_id": run_id, "started_at": _iso(0), "completed_at": _iso(dur),
        "labels": ["ubuntu-latest"],
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}/job/{jid}",
        "steps": [
            {"name": "Set up job", "number": 1, "status": "completed",
             "conclusion": "success", "started_at": _iso(0), "completed_at": _iso(setup)},
            {"name": dom, "number": 2, "status": "completed", "conclusion": "success",
             "started_at": _iso(setup), "completed_at": _iso(dur)},
        ]}


class _ThreeGateRepo(cr.GhClient):
    """A real `GhClient` (real prefetch buffer + counters) serving a canned repo: one
    workflow, three jobs, twenty successful pull_request runs, every job log a plain body
    no detector recognizes (so no magnitude probe fans out)."""

    def __init__(self) -> None:
        super().__init__()
        self.text_calls: list[str] = []
        self._lock = threading.Lock()
        self.jobs = {100 + i: [_job(100 + i, k, n, d, s) for k, (n, d, s) in enumerate(_JOBS)]
                     for i in range(20)}

    def available(self) -> bool:
        return True

    def _runs(self) -> list[dict]:
        return [{"id": 100 + i, "event": "pull_request", "head_sha": f"s{i}",
                 "created_at": _iso(0), "run_started_at": _iso(0), "run_attempt": 1,
                 "conclusion": "success", "status": "completed", "updated_at": _iso(200)}
                for i in range(20)]

    def _json_live(self, endpoint: str, allow_missing: bool = False):
        self._bump(query=True)
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [{"id": 1, "path": _WF, "name": "ci"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            if "per_page=1&" in m.group(2) or m.group(2).startswith("per_page=1&"):
                return {"total_count": 40}
            return {"workflow_runs": self._runs()}
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m:
            return {"jobs": self.jobs.get(int(m.group(1)), [])}
        m = re.match(r"repos/o/r/commits/([^/]+)/check-runs", endpoint)
        if m:
            return {"check_runs": [
                {"name": n, "started_at": _iso(0), "completed_at": _iso(d)}
                for n, d, _s in _JOBS]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main", "visibility": "public"}
        return None

    def _text_live(self, endpoint: str, allow_missing: bool = False):
        with self._lock:
            self.text_calls.append(endpoint)
        self._bump(query=True)
        return "2026-10-20T00:00:01Z plain step output\n2026-10-20T00:00:02Z done\n"


def _collect(monkeypatch, data_dir: Path) -> dict:
    client = _ThreeGateRepo()
    monkeypatch.setattr(cr, "GhClient", lambda: client)
    doc = {"findings": [{"id": "f1", "workflow_file": _WF}]}
    return cr.collect(doc, "o/r", max_runs=20, with_logs=True, data_dir=data_dir,
                      shallow_runs=10)


def _render_like_run_py(doc: dict, tmp_path: Path) -> str:
    """Render exactly as a real run does: the `blocking_path.py` command the data-pass
    summary prints (per-pole --log/--steps/--mag bindings from the captured bundle)."""
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "report.md"
    cmd = summary.render_command(doc, str(findings), str(out))
    assert cmd, "the data pass captured no drill logs at all"
    argv = shlex.split(cmd.replace("\\\n", " "), comments=True)
    assert argv[:2] == ["python3", "scripts/blocking_path.py"]
    r = subprocess.run([sys.executable, str(_SCRIPTS / "blocking_path.py"), *argv[2:]],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return out.read_text(encoding="utf-8")


def test_every_rendered_pole_gets_a_captured_log_and_an_audit_line(monkeypatch, tmp_path):
    """End to end (collect with logs -> the printed render command -> verify): the third
    distinct gating job is drilled like the first two, so its section carries the same
    `🔗 Audit:` line and its prompt's Audit-link pointer resolves."""
    doc = _collect(monkeypatch, tmp_path / "findings.data")
    md = _render_like_run_py(doc, tmp_path)
    rendered = [check for _wf, check, _body in vr._pole_header_sections(md)]
    assert len(rendered) == 3, rendered
    drilled = sorted(str(e.get("check")) for e in doc["data_bundle"]["logs"])
    assert drilled == sorted(rendered), (
        f"rendered poles {rendered} but the data pass drilled only {drilled}")
    for _wf, check, body in vr._pole_header_sections(md):
        assert _AUDIT_LINE in body, f"pole `{check}` renders no Audit line"
    chk = vr.check_pole_audit_link_reference_resolves(md)
    assert chk.ok and not chk.skipped, chk.detail


def test_drill_cap_equals_the_render_cap():
    """The structural pin: the data pass drills as many distinct job matrices as the
    renderer can show, so a rendered pole is never left without a captured log because
    of the cap. Raising one without the other reopens the bug."""
    assert cr._DRILL_DISTINCT_MATRICES == bp._TOP_WORKFLOWS


def test_persist_pole_logs_captures_every_distinct_pole(tmp_path):
    """`_persist_pole_logs` itself must not truncate the drill set below the render cap:
    three distinct jobs in one workflow -> three captured logs."""
    class _Client:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            return "plain log body"

    poles = [{"check": n, "workflow_file": _WF, "job": n, "p50_s": float(d)}
             for n, d, _s in _JOBS]
    jpr = {_WF: [[_job(100 + i, k, n, d, s) for k, (n, d, s) in enumerate(_JOBS)]
                 for i in range(4)]}
    manifest = cr._persist_pole_logs(_Client(), "o/r", poles, jpr, tmp_path)
    assert sorted(e["check"] for e in manifest) == sorted(n for n, _d, _s in _JOBS)


# --------------------------------------------------------------------------- #
# Render level: a pole whose section has no Audit line never cites one.
# --------------------------------------------------------------------------- #

def _shallow_pole() -> dict:
    return {"check": "test", "workflow_file": _WF, "job": "test", "p50_s": 135.0,
            "dominant_step": "Test", "dominant_category": "test",
            "dominant_p50_s": 106.0, "dominant_share": 0.77,
            "steps": [{"step": "Test", "category": "test", "p50_s": 106.0}]}


def _structural(check: str) -> dict:
    return {"id": "f17", "pattern": "OPT75", "pattern_class": "structural",
            "structural": True, "workflow_file": _WF, "affected_jobs": [check]}


def test_prompt_without_an_audit_line_never_cites_the_audit_link():
    """No captured timeline -> `_audit_links` renders nothing -> every prompt branch that
    used to say "open its log (the Audit link)" must point somewhere real instead."""
    pole = _shallow_pole()
    assert bp._audit_links(None, pole, None, None) == []
    for kw in ({"structural": [_structural("test")]},
               {"data_driven": [{"id": "f9", "pattern": "OPT24", "workflow_file": _WF}]},
               {}):
        prompt = bp._build_generic_agent_prompt(pole, [], None, "o/r", "abc1234", 3, 4,
                                                timeline=None, **kw)
        assert _AUDIT_REF not in prompt, (kw, prompt)
        assert "does NOT prescribe the fix" in prompt


def test_prompt_with_an_audit_line_still_cites_it():
    """The drilled path keeps its pointer: with a captured timeline + run URL the section
    renders the Audit line, so the prompt may (and does) send the agent to it."""
    pole = _shallow_pole()
    run_url = "https://github.com/o/r/actions/runs/123"
    timeline = {"run_url": run_url, "job_url": run_url + "/job/9",
                "steps": [{"name": "Test", "number": 2, "dur_s": 106.0,
                           "start_s": 10.0, "end_s": 116.0}]}
    assert bp._audit_links(timeline, pole, None, run_url)
    prompt = bp._build_generic_agent_prompt(pole, [], run_url, "o/r", "abc1234", 3, 4,
                                            timeline=timeline,
                                            structural=[_structural("test")])
    assert _AUDIT_REF in prompt


def test_render_of_an_undrilled_third_pole_carries_no_dangling_audit_reference():
    """Full render of the dogfood shape with only two poles in the bundle (a capture that
    failed, or a render with no bundle): pole 3 renders shallow and honest, and the
    report passes the new verify check."""
    poles = [
        {"check": n, "workflow_file": _WF, "job": n, "p50_s": float(d),
         "dominant_step": s, "dominant_category": "test" if n == "test" else "other",
         "dominant_p50_s": float(d) - 30.0, "dominant_share": (d - 30.0) / d,
         "steps": [{"step": s, "category": "test" if n == "test" else "other",
                    "p50_s": float(d) - 30.0}]}
        for n, d, s in _JOBS]
    doc = {
        "repo": "o/r", "scanned_at": "2026-10-20T00:00:00Z",
        "data_sources": {"runs_sampled": 20, "jobs_sampled": 60, "workflows_analyzed": 1},
        "required_checks": [n for n, _d, _s in _JOBS],
        "pr_critical_path": {"sampled_pr_count": 4, "sample_target": 4,
                             "sample_complete": True, "poles": poles},
        "data_bundle": {"logs": [{"check": n, "workflow_file": _WF,
                                  "html_url": f"https://github.com/o/r/actions/runs/{i}"}
                                 for i, (n, _d, _s) in enumerate(_JOBS[:2], 1)]},
        "findings": [{**_structural(n), "id": f"s{i}",
                      "title": f"The long pole's time is one addressable step - `{n}`",
                      "evidence": f"critical-path check `{n}`"}
                     for i, (n, _d, _s) in enumerate(_JOBS)],
    }
    md = bp.render(doc, logs={}, samples={}, captured_at="2026-10-20")
    secs = {check: body for _wf, check, body in vr._pole_header_sections(md)}
    assert "test" in secs, list(secs)
    assert _AUDIT_LINE not in secs["test"]
    assert _AUDIT_REF not in secs["test"], secs["test"]
    chk = vr.check_pole_audit_link_reference_resolves(md)
    assert chk.ok, chk.detail


# --------------------------------------------------------------------------- #
# Verify level: a pole that cites the Audit link must render it.
# --------------------------------------------------------------------------- #

def _report(pole3_body: str) -> str:
    """A trimmed, generic version of the dogfood report: pole 2 drilled (Audit line +
    prompt pointer), pole 3 shallow with whatever body the case supplies."""
    return "\n".join([
        "# o/r - why is the merge slow?", "",
        '<a id="pole-2"></a>', "",
        "## 🟠 Long pole 2: `ci.yml` ▸ `build guards` - 2m 59s", "",
        "**🔗 Audit:** run [41](https://github.com/o/r/actions/runs/41) → "
        "[the `build guards` job](https://github.com/o/r/actions/runs/41/job/7)", "",
        "```text",
        "- The step above is the load-bearing one that lever targets; open its log "
        "(the Audit link) to see exactly what inside it the lever reshapes.",
        "```", "",
        '<a id="pole-3"></a>', "",
        "## 🟠 Long pole 3: `ci.yml` ▸ `test` - 2m 15s", "",
        pole3_body, "",
        "---", "",
        "## Runner-minute cost spine", "",
        "_To verify any number, follow the pole's **🔗 Audit** link to the gating step._",
        ""])


def test_verify_fails_a_pole_citing_an_audit_link_it_does_not_render():
    body = "\n".join([
        "```text",
        "WHERE THE TIME GOES",
        "- The job's time is dominated by the `Test` step: ~1m 46s (77% of the job wall), "
        "from the sampled per-step decomposition (no single-run timeline was captured for "
        "this job).",
        "",
        "STRUCTURAL CATALOG PATTERN MATCHED",
        "- The step above is the load-bearing one that lever targets; open its log (the "
        "Audit link) to see exactly what inside it the lever reshapes.",
        "```"])
    chk = vr.check_pole_audit_link_reference_resolves(_report(body))
    assert not chk.ok, chk.detail
    assert "`ci.yml` ▸ test" in chk.detail
    assert "build guards" not in chk.detail       # the drilled pole is not flagged


def test_verify_passes_when_every_citing_pole_renders_its_audit_line():
    body = "\n".join([
        "**🔗 Audit:** run [42](https://github.com/o/r/actions/runs/42) → "
        "[the `test` job](https://github.com/o/r/actions/runs/42/job/8)", "",
        "```text",
        "- open its log (the Audit link) to see exactly what inside it the lever reshapes.",
        "```"])
    chk = vr.check_pole_audit_link_reference_resolves(_report(body))
    assert chk.ok and not chk.skipped, chk.detail


def test_verify_passes_an_undrilled_pole_that_cites_no_audit_link():
    body = "- open the `Test` step's log in a recent pull-request run of this job."
    chk = vr.check_pole_audit_link_reference_resolves(_report(body))
    assert chk.ok, chk.detail


def test_verify_registers_the_audit_link_check():
    """Wired into the CLI gate, not just defined: `run_checks` must include it."""
    names = [c.name for c in vr.run_checks(_report("plain"), None, None, None)]
    assert vr.check_pole_audit_link_reference_resolves(_report("plain")).name in names


@pytest.mark.parametrize("fake_line", [
    "**🔗 Audit:** unavailable",
    "```text\n**🔗 Audit:** [run](https://github.com/o/r/actions/runs/42)\n```",
])
def test_verify_requires_a_real_audit_link_outside_code_fences(fake_line):
    report = _report(fake_line + "\n\nOpen the Audit link to inspect the job.")
    assert not vr.check_pole_audit_link_reference_resolves(report).ok
    linked = _report("**🔗 Audit:** run [42](https://github.com/o/r/actions/runs/42)"
                     "\n\nOpen the Audit link to inspect the job.")
    assert vr.check_pole_audit_link_reference_resolves(linked).ok


def test_captured_log_without_timeline_does_not_claim_log_is_missing():
    doc = {
        "repo": "o/r", "scanned_at": "2026-10-20T00:00:00Z",
        "data_sources": {"runs_sampled": 20, "jobs_sampled": 20},
        "pr_critical_path": {"poles": [_shallow_pole()]},
        "findings": [],
    }
    report = bp.render(doc, logs={"ci": "plain output from the captured job\n"})
    assert "no run log was captured for it here" not in report
    assert "no direct log link is shown here" in report


def test_capture_selection_matches_rendered_gate_frequency_and_slow_mode(monkeypatch, tmp_path):
    names = ["alpha", "beta", "gamma", "delta", "epsilon", "common"]
    medians = dict(zip(names, [600, 500, 400, 300, 200, 100]))
    choices = [name for name, count in zip(names, [2, 2, 2, 2, 6, 6])
               for _ in range(count)]

    class PartitionedRepo(_ThreeGateRepo):
        def __init__(self):
            super().__init__()
            self.jobs = {
                100 + i: ([_job(100 + i, 0, name, medians[name], "Work")]
                          if name != "common" else [])
                         + [_job(100 + i, 5, "common",
                                 1000 if name == "common" else 100, "Work")]
                for i, name in enumerate(choices)}

        def _json_live(self, endpoint, allow_missing=False):
            match = re.match(r"repos/o/r/commits/s(\d+)/check-runs", endpoint)
            if match:
                return {"check_runs": [
                    {key: job[key] for key in ("name", "started_at", "completed_at")}
                    for job in self.jobs[100 + int(match[1])]]}
            return super()._json_live(endpoint, allow_missing)

    monkeypatch.setattr(cr, "GhClient", PartitionedRepo)
    doc = cr.collect({"findings": [{"id": "f1", "workflow_file": _WF}]}, "o/r",
                     max_runs=20, with_logs=True, data_dir=tmp_path / "bundle",
                     shallow_runs=10)
    report = _render_like_run_py(doc, tmp_path)
    rendered = {name for _, name, _ in vr._pole_header_sections(report)}
    captured = {entry["check"] for entry in doc["data_bundle"]["logs"]}
    assert len(rendered) == 5
    assert rendered == captured

"""A `needs:` verdict job is an aggregation gate, never an addressable long pole.

Reproduced on starslingdev/skills' own CI: the required check `test` is a ~3s verdict
job (`needs: [test-self, test-fork]`, `if: always()`, no checkout, one shell step that
reads `needs.*.result`) whose wait IS the 4m32s `test (self-hosted)` suite it needs.
The report crowned the 3s verdict "Long pole 1", routed an OPT75 "speed up the dominant
step" lever to it, rendered BOTH OPT75 cards under BOTH poles (`test (self-hosted)` reads
as a matrix leg of `test`), and the Bottom line called an 11s check "slower" than a
4m32s chain. The fixture is the real findings.json trimmed to the fields the render
reads (`fixtures/verdict_gate_findings.json`).

Run from the repo root:

    python3 -m pytest -q skills/ci-speedup/tests/test_verdict_aggregation_gate.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
from summary import build_summary  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "verdict_gate_findings.json"
_CI = ".github/workflows/ci.yml"


def _doc() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _render(doc: dict) -> str:
    return bp.render(copy.deepcopy(doc), {}, {}, {}, "2026-10-29T05:21:00Z", {})


def _section(md: str, check: str) -> str:
    i = md.index(f"▸ `{check}`")
    i = md.rindex("\n## ", 0, i)
    j = md.find("\n## ", i + 4)
    return md[i:j if j != -1 else len(md)]


def _bottom_line(md: str) -> str:
    i = md.index("> **Bottom line.**")
    j = md.index("\n## ", i)
    return md[i:j]


# ── Bug 1: the verdict job is an aggregation gate, ranked behind its upstream ─────────

def test_real_time_holder_is_long_pole_one_and_verdict_is_an_aggregation_gate():
    md = _render(_doc())
    # The 4m32s suite leads; the 3s verdict follows it.
    assert "## 🟠 Long pole 1: `ci.yml` ▸ `test (self-hosted)`" in md
    assert "## 🟡 Long pole 2: `ci.yml` ▸ `test`" in md
    toc = md[md.index("## 📋 Contents"):md.index('<a id="long-pole-map"></a>')]
    assert toc.index("[test (self-hosted)](#pole-1)") < toc.index("[test](#pole-2)")
    sec = _section(md, "test")
    assert "**Aggregation gate" in sec
    assert "**➡️ Where the wait actually is:** [Long pole 1](#pole-1)" in sec
    assert "🤖 Prompt for your coding agent" not in sec
    assert "OPT75" not in sec
    # The chain context the headline relies on is kept, not dropped.
    assert "gate chain" in sec


def test_verdict_job_routes_no_structural_lever_in_the_data_layer():
    # The engine must never emit an OPT75 "speed up the dominant step" for a ~3s
    # `needs:`-everything sink: there is nothing inside it to speed up.
    def job(name, steps):
        t, sj = 0, []
        for sn, dur in steps:
            sj.append({"name": sn,
                       "started_at": f"2026-01-01T00:{t // 60:02d}:{t % 60:02d}Z",
                       "completed_at": f"2026-01-01T00:{(t + dur) // 60:02d}:{(t + dur) % 60:02d}Z"})
            t += dur
        return {"name": name, "html_url": "https://github.com/acme/repo/runs/1",
                "steps": sj, "started_at": "2026-01-01T00:00:00Z",
                "completed_at": f"2026-01-01T00:{t // 60:02d}:{t % 60:02d}Z"}

    runs = [[job("test (self-hosted)", [("Checkout", 6), ("Run tests", 257)]),
             job("test", [("Require the suite that should have run to have passed", 1),
                          ("Complete job", 1)])] for _ in range(5)]
    jpr = {_CI: runs}
    crit_by_wf = {_CI: cr._critical_path(runs)}
    out = cr._detect_structural_candidates(
        (("test (self-hosted)", 272.0), ("test", 3.0)), [], crit_by_wf, jpr,
        cr.RequiredChecks(frozenset({"test"}), complete=True),
        {_CI: {"pull_request"}}, {}, 0,
        job_graph=_doc()["workflow_job_graph"])
    routed = {j for f in out for j in (f.get("affected_jobs") or [])}
    assert "test (self-hosted)" in routed          # the real time-holder keeps its lever
    assert "test" not in routed, [f["title"] for f in out]


def test_summary_does_not_call_the_verdict_addressable():
    s = build_summary(_doc())
    block = s[s.index("Addressable long poles"):]
    block = block[:block.index("\n\n")]
    assert "test (self-hosted)" in block
    assert "• test  [" not in block, block


def _addressable_block(s: str) -> str:
    block = s[s.index("Addressable long poles"):]
    return block[:block.index("\n\n")]


def test_summary_without_a_drill_bundle_still_lists_the_suite():
    # No drill logs captured: the fallback collapses matrix siblings by base name. It must
    # not fold the 4m32s `test (self-hosted)` suite into a `test` row and then drop that
    # row as the verdict gate — the digest would report zero fixable checks.
    doc = _doc()
    doc.pop("data_bundle")
    block = _addressable_block(build_summary(doc))
    assert "• test (self-hosted)  [ci.yml]  P50 4m 32s" in block, block
    assert "• test  [" not in block, block


def test_summary_keeps_a_gate_with_a_specific_structural_lever_addressable():
    # The renderer keeps the drill for a sink carrying a SPECIFIC structural lever
    # (OPT70/71/72) — that lever is measured content of its own. The digest must agree
    # and keep the check fixable rather than listing it as an inert aggregation gate.
    doc = _doc()
    lever = copy.deepcopy(doc["findings"][0])
    lever.update(pattern="OPT70", id="f-opt70", affected_jobs=["test"],
                 pr_critical_path_check="test",
                 title="Scope the suite to the changed paths — `test`")
    doc["findings"].append(lever)
    assert "**Aggregation gate" not in _section(_render(doc), "test")
    block = _addressable_block(build_summary(doc))
    assert "• test  [ci.yml]" in block, block
    assert "Aggregation gates" not in build_summary(doc)


# ── Bug 2: a structural card renders only under the pole it names ─────────────────────

def test_structural_card_renders_only_under_its_own_pole():
    md = _render(_doc())
    sec = _section(md, "test (self-hosted)")
    assert sec.count("📐 Structural root-cause") == 1, sec
    assert "path - `test (self-hosted)`**" in sec
    assert "path - `test`**" not in sec


# ── Bug 3: the Bottom line compares against the wait it headlines ─────────────────────

def test_bottom_line_never_calls_a_faster_check_slower_than_the_chain(monkeypatch):
    # With the aggregation recognition disabled the 3s chain sink still heads the poles
    # (it is the required check); the 11s `ci-secure (self-hosted)` is slower than THAT
    # stage but not than the 4m32s chain the Bottom line headlines.
    monkeypatch.setattr(bp, "_agg_gate_shape", lambda *a, **k: None)
    bl = _bottom_line(_render(_doc()))
    assert "4m 32s" in bl
    assert "is slower (~11s)" not in bl, bl


def test_bottom_line_on_the_repro_names_no_false_slower_check():
    bl = _bottom_line(_render(_doc()))
    assert "is slower (~11s)" not in bl, bl


def test_short_terminal_build_keeps_its_specific_structural_lever():
    # A short package build can have the same needs shape as a verdict, but its
    # measured build work is the specific OPT70 exemption the renderer preserves.
    def job(name, duration, step):
        return {"name": name, "html_url": "https://github.com/acme/repo/runs/1",
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": f"2026-01-01T00:00:{duration:02d}Z",
                "steps": [{"name": step, "started_at": "2026-01-01T00:00:00Z",
                           "completed_at": f"2026-01-01T00:00:{duration:02d}Z"}]}

    runs = [[job("a", 5, "Run tests"), job("b", 5, "Run tests"),
             job("package", 20, "Build package")] for _ in range(5)]
    out = cr._detect_structural_candidates(
        (("package", 20.0), ("a", 5.0), ("b", 5.0)), [],
        {_CI: cr._critical_path(runs)}, {_CI: runs},
        cr.RequiredChecks(frozenset({"package"}), complete=True),
        {_CI: {"pull_request"}}, {}, 0,
        job_graph={_CI: {"a": {"needs": []}, "b": {"needs": []},
                         "package": {"needs": ["a", "b"]}}})
    assert any(f["pattern"] == "OPT70" and f["affected_jobs"] == ["package"]
               for f in out)

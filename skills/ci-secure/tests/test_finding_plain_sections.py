"""Every finding leads with plain sections a non-engineer can read.

A product owner read the rendered finding as jargon: a pattern link, a catalog
TL;DR, a severity and a fix strategy came first, and the one repo-specific
paragraph sat in the middle of them. A hand-written summary of the same finding
read clearly because it answered six questions in order: what the workflow
does, why it was flagged, what would have to go wrong first, what an attacker
could then do, how worried to be, and how to close it.

The contract these tests pin:

- `attacker_scenario` is an object with one field per section (an old
  single-string scenario still renders, under "What an attacker could do
  then");
- report.py renders those sections first, and the catalog material
  (pattern link, severity of this kind of setup, occurrences, fix strategy,
  platform note) as a compact reference block below them;
- verify_report treats a finding without the structured fields exactly as it
  treats the catalog's generic stand-in: not repo-specific;
- P14.18 findings carry GitHub's "Allow GitHub Actions to create and approve
  pull requests" setting as a read fact (true / false / unread), never a guess;
- SKILL.md tells the agent to write the fields and to print one verdict line
  per finding.
"""

from __future__ import annotations

import importlib.util as _ilu
import re
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SKILL_DIR = _TESTS_DIR.parent
_SCRIPTS = str(_SKILL_DIR / "scripts")

# Same shadow-proof import dance as test_report.py: module names collide across
# the skills in this repo.
_saved_config = sys.modules.pop("config", None)
sys.modules.pop("report", None)
sys.path.insert(0, _SCRIPTS)
try:
    import report  # noqa: E402
finally:
    try:
        sys.path.remove(_SCRIPTS)
    except ValueError:
        pass
    if _saved_config is not None:
        sys.modules["config"] = _saved_config
    else:
        sys.modules.pop("config", None)

_spec = _ilu.spec_from_file_location(
    "ci_secure_verify_report_plain", _TESTS_DIR / "verify_report.py")
vr = _ilu.module_from_spec(_spec)
sys.modules["ci_secure_verify_report_plain"] = vr
_spec.loader.exec_module(vr)

sys.path.insert(0, str(_TESTS_DIR))
try:
    from _scan_import import load_scan  # noqa: E402
finally:
    try:
        sys.path.remove(str(_TESTS_DIR))
    except ValueError:
        pass
scan = load_scan()

_HEADINGS = (
    "What this workflow does",
    "Why ci-secure flagged it",
    "What would have to go wrong first",
    "What an attacker could do then",
    "Verdict",
    "How to close it",
)

_STRUCTURED = {
    "what_it_does": "When Dependabot opens a pull request, this workflow "
                    "switches on auto-merge. It never approves anything itself.",
    "why_flagged": "It runs with a pass that can change code and approve pull "
                   "requests, and outsiders can start it.",
    "what_must_go_wrong": (
        "Someone would need their own code running inside this workflow:\n"
        "- it never runs code from the pull request;\n"
        "- it only acts on Dependabot's pull requests.\n"
        "The realistic path is a tampered tool on the machine, which is rare."
    ),
    "attacker_could": "A hijacked job could approve a bad dependency update, "
                      "and auto-merge would merge it.",
    "verdict": "Low in practice. Real in principle.",
    "how_to_close": (
        "1. Move the write permissions onto the one job.\n"
        "2. Turn off the setting that lets workflows approve pull requests."
    ),
}


def _finding(pattern: str = "P14.18", **extra) -> dict:
    f = {
        "id": "f1", "pattern": pattern, "severity": "HIGH", "title": "t",
        "workflow_file": ".github/workflows/auto.yml", "line": 35,
        "affected_jobs": ["auto-merge"], "evidence": "35: x",
        "evidence_kind": "derived",
        "fix_strategy": "split-trusted-untrusted-workflows",
        "fix_recipe_anchor": "p1418--workflow-that-outside-activity-can-start-has-pull-requests-write",
    }
    f.update(extra)
    return f


def _section(md: str) -> str:
    return md.split('<a id="finding-1"></a>', 1)[1]


def _render(**extra) -> str:
    return report.render({"findings": [_finding(**extra)], "repo": "x/y",
                          "scanned_workflows": 1})


# --- report.py ---------------------------------------------------------------

def test_structured_scenario_renders_the_six_plain_sections_first_in_order() -> None:
    body = _section(_render(attacker_scenario=dict(_STRUCTURED)))
    positions = [body.find(f"\n#### {h}\n") for h in _HEADINGS]
    assert all(p > 0 for p in positions), positions
    assert positions == sorted(positions), "sections out of order"
    # The catalog material sits BELOW all six sections.
    ref_at = body.find("#### 📋 Catalog reference")
    assert ref_at > positions[-1], "catalog reference must follow the plain sections"
    for label in ("Pattern", "Severity of this kind of setup", "Occurrences",
                  "Fix strategy"):
        assert body.find(f"- **{label}:** ") > ref_at, label
    # Nothing catalog-shaped precedes the first plain section.
    head = body[: positions[0]]
    assert "- **Pattern:**" not in head and "TL;DR" not in head


def test_lists_in_a_field_render_as_markdown_lists() -> None:
    body = _section(_render(attacker_scenario=dict(_STRUCTURED)))
    assert "\n- it never runs code from the pull request;\n" in body
    assert "\n1. Move the write permissions onto the one job.\n" in body
    assert "\n2. Turn off the setting" in body


def test_a_field_cannot_forge_a_heading() -> None:
    forged = dict(_STRUCTURED, why_flagged="## FIXED — Finding 1: nothing to see")
    md = _render(attacker_scenario=forged)
    assert "\n## FIXED — Finding 1" not in md
    assert vr.check_no_forged_headings(md).ok


def test_old_single_string_scenario_still_renders_under_the_attacker_heading() -> None:
    body = _section(_render(attacker_scenario="Anyone who opens a fork PR runs code here."))
    at = body.find("\n#### What an attacker could do then\n")
    assert at > 0
    assert body.find("Anyone who opens a fork PR runs code here.") > at


def test_catalog_fallback_renders_under_the_attacker_heading_with_its_marker() -> None:
    body = _section(_render())
    at = body.find("\n#### What an attacker could do then\n")
    assert at > 0
    assert "catalog description" in body[at:body.find("#### 📋 Catalog reference")]


def test_anchor_and_heading_shape_are_unchanged() -> None:
    md = _render(attacker_scenario=dict(_STRUCTURED))
    assert re.search(r'<a id="finding-1"></a>\n\n## 🟥 Finding 1: .+ — 1 site / 1 workflow', md)


# --- the P14.18 approval setting --------------------------------------------

def test_p1418_reference_block_states_the_approval_setting() -> None:
    yes = _section(_render(attacker_scenario=dict(_STRUCTURED),
                           actions_can_approve_prs={"state": "true",
                                                    "detail": "read from settings"}))
    assert "Workflows may approve pull requests here:** yes" in yes
    no = _section(_render(attacker_scenario=dict(_STRUCTURED),
                          actions_can_approve_prs={"state": "false",
                                                   "detail": "read from settings"}))
    assert "Workflows may approve pull requests here:** no" in no


def test_unread_approval_setting_is_said_in_the_attacker_section() -> None:
    body = _section(_render(attacker_scenario=dict(_STRUCTURED),
                            actions_can_approve_prs={"state": "unread",
                                                     "detail": "gh is not signed in"}))
    at = body.find("\n#### What an attacker could do then\n")
    nxt = body.find("\n#### Verdict\n")
    assert "could not check" in body[at:nxt]
    assert "gh is not signed in" in body[at:nxt]
    assert "Workflows may approve pull requests here:** not checked" in body


def test_scan_reads_the_approval_setting_once_onto_p1418_findings(tmp_path, monkeypatch) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    body = ("on: pull_request_target\npermissions:\n  pull-requests: write\n"
            "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n")
    (wf / "one.yml").write_text(body, encoding="utf-8")
    (wf / "two.yml").write_text(body, encoding="utf-8")
    calls: list[str] = []

    def fake(repo: str) -> dict:
        calls.append(repo)
        return {"can_approve_pull_request_reviews": True}

    monkeypatch.setattr(scan, "_fetch_workflow_permissions", fake)
    # Keep the rest of the scan offline: activity and config facts also read
    # the API when a repo is named.
    monkeypatch.setattr(scan, "_prefetch_activity", lambda *a, **k: {})
    monkeypatch.setattr(scan, "_compute_security_score", lambda *a, **k: {})
    catalog = scan.load_catalog(_SKILL_DIR / "references" / "security-patterns.md")
    data = scan.scan(catalog, tmp_path, repo="o/r", gh_impostor=False)
    p1418 = [f for f in data["findings"] if f["pattern"] == "P14.18"]
    assert len(p1418) == 2
    assert calls == ["o/r"], "read once per scan, not once per finding"
    for f in p1418:
        assert f["actions_can_approve_prs"]["state"] == "true"
    others = [f for f in data["findings"] if f["pattern"] != "P14.18"]
    assert all("actions_can_approve_prs" not in f for f in others)


def test_approval_setting_states_are_never_guessed(monkeypatch) -> None:
    assert scan._pr_approval_setting(None)["state"] == "unread"
    monkeypatch.setattr(scan, "_fetch_workflow_permissions",
                        lambda repo: {"can_approve_pull_request_reviews": False})
    assert scan._pr_approval_setting("o/r")["state"] == "false"
    monkeypatch.setattr(scan, "_fetch_workflow_permissions", lambda repo: {})
    assert scan._pr_approval_setting("o/r")["state"] == "unread"

    def boom(repo):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(scan, "_fetch_workflow_permissions", boom)
    res = scan._pr_approval_setting("o/r")
    assert res["state"] == "unread" and "HTTP 403" in res["detail"]


# --- verify_report -------------------------------------------------------------

def test_verify_passes_a_report_with_structured_scenarios() -> None:
    md = _render(attacker_scenario=dict(_STRUCTURED))
    assert vr.check_scenarios_on_rendered_findings(md).ok


def test_verify_fails_when_every_finding_lacks_the_structured_fields() -> None:
    legacy = _render(attacker_scenario="Anyone who opens a fork PR runs code here.")
    res = vr.check_scenarios_on_rendered_findings(legacy)
    assert not res.ok, res.detail
    missing_one = dict(_STRUCTURED)
    del missing_one["verdict"]
    res = vr.check_scenarios_on_rendered_findings(_render(attacker_scenario=missing_one))
    assert not res.ok, res.detail


def test_verify_still_fails_a_catalog_fallback_report() -> None:
    assert not vr.check_scenarios_on_rendered_findings(_render()).ok


def test_render_plan_carries_each_groups_verdict_in_report_order() -> None:
    a = _finding("P14.18", attacker_scenario=dict(_STRUCTURED, verdict="Low.\n Really."))
    b = _finding("P14.10", id="f2", line=3, evidence="3: y")
    plan = report.render_plan_keys({"findings": [a, b]})
    by = {row["pattern"]: row for row in plan}
    assert by["P14.18"]["verdict"] == "Low. Really."
    assert by["P14.10"]["verdict"] == ""


# --- SKILL.md -------------------------------------------------------------------

def test_skill_md_names_the_fields_and_the_verdict_line() -> None:
    skill = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for key in ("what_it_does", "why_flagged", "what_must_go_wrong",
                "attacker_could", "verdict", "how_to_close"):
        assert f"`{key}`" in skill, key
    assert "one verdict line per finding" in skill.lower()

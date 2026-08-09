"""Tests for verify_report.py — the report self-check.

Unit-tests each invariant check against synthetic report fragments, plus one
end-to-end test that runs the real pipeline (scan.py → report.py →
verify_report) so CI catches any report-integrity regression automatically —
the whole point of the verifier. The e2e is network-free: it scans a local
fixture without zizmor, so report.py never fetches upstream audit docs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SKILL_DIR = _TESTS_DIR.parent
sys.path.insert(0, str(_TESTS_DIR))
import importlib.util as _ilu, sys as _sys
from pathlib import Path as _P
_spec = _ilu.spec_from_file_location(
    "ci_secure_verify_report", _P(__file__).resolve().parent / "verify_report.py")
vr = _ilu.module_from_spec(_spec)
_sys.modules["ci_secure_verify_report"] = vr
_spec.loader.exec_module(vr)
# shadow-proof load (ci-score's tests dir also exports a verify_report)


# The provenance table carries no `Findings` row any more (the count already
# appears in the banner, the headline and the Scanner row) — the
# `## Critical findings: **N**` headline is the count's statement of record,
# so the fixture carries it.
_GOOD_HEADER = (
    "| Repository | [`x/y`](https://github.com/x/y) |\n"
    "| :--- | :--- |\n"
    "| **Severity breakdown (by occurrence)** | HIGH: 2 · MEDIUM: 2 · LOW: 1 |\n"
    "| **Coverage** | ✅ complete — every detector evaluated |\n"
    "| **Scanned** | 2026-05-26 (UTC) |\n"
    "\n"
    "## Critical findings: **5** — 2 of 10 vectors hit\n"
)


def _chain_table(hit: tuple[str, ...] = ()) -> str:
    """A synthetic chain-status table: every chain clean unless named in `hit`."""
    rows = ["| | Chain | Evidence |", "|---|---|---|"]
    for pid in sorted(vr.THE_TEN):
        mark = "🟥" if pid in hit else "✅"
        rows.append(f"| {mark} | `{pid}` — title | evidence |")
    return "\n".join(rows) + "\n"


# --- unit: each check ---------------------------------------------------------

def test_findings_total_matches_breakdown() -> None:
    assert vr.check_findings_total_matches_breakdown(_GOOD_HEADER).ok
    bad = ("## Critical findings: **9** — 2 of 10 vectors hit\n"
           "| **Severity breakdown (by occurrence)** | HIGH: 2 · LOW: 1 |\n")
    assert not vr.check_findings_total_matches_breakdown(bad).ok


def test_group_splits_sum_to_count() -> None:
    good = "- **Occurrences:** 115 occurrences across 17 workflows (20 HIGH · 6 MEDIUM · 89 LOW)"
    assert vr.check_group_splits_sum_to_count(good).ok
    bad = "- **Occurrences:** 100 occurrences across 5 workflows (20 HIGH · 6 MEDIUM · 89 LOW)"
    assert not vr.check_group_splits_sum_to_count(bad).ok
    # uniform group with no split → skipped, treated as ok
    assert vr.check_group_splits_sum_to_count("- **Occurrences:** 3 occurrences across 1 workflow").skipped


def test_finding_anchors_resolve() -> None:
    ok = '[details →](#finding-1)\n<a id="finding-1"></a>'
    assert vr.check_finding_anchors_resolve(ok).ok
    broken = '[details →](#finding-9)\n<a id="finding-1"></a>'
    res = vr.check_finding_anchors_resolve(broken)
    assert not res.ok and "finding-9" in res.detail


def test_no_dangling_colon() -> None:
    assert vr.check_no_dangling_colon_before_link("Replace the token. See [catalog](x).").ok
    assert not vr.check_no_dangling_colon_before_link("Two structural options: See [catalog](x).").ok


def test_coverage_row_consistency() -> None:
    assert vr.check_coverage_row_consistent("| **Coverage** | ✅ complete — x |").ok
    # complete but a gap banner present → inconsistent
    bad1 = "| **Coverage** | ✅ complete |\n> **Incomplete coverage — x.**"
    assert not vr.check_coverage_row_consistent(bad1).ok
    # PARTIAL with the banner → consistent
    good2 = "| **Coverage** | ⚠️ **PARTIAL** — x |\n> **Incomplete coverage — y.**"
    assert vr.check_coverage_row_consistent(good2).ok
    # PARTIAL but no banner → inconsistent
    assert not vr.check_coverage_row_consistent("| **Coverage** | ⚠️ **PARTIAL** — x |").ok
    # missing row entirely → fail
    assert not vr.check_coverage_row_consistent("no coverage here").ok


def test_scanned_date_present() -> None:
    """The saved report has ONE stable name (`./ci-secure-report.md`), so the
    `Scanned` row is the only place the run date lives — and a report that
    can't state when it ran can't be told apart from a stale one."""
    rpt = "| **Scanned** | 2026-05-26 (UTC) |"
    assert vr.check_scanned_date_present(rpt, Path("ci-secure-report.md")).ok
    assert vr.check_scanned_date_present(rpt, None).ok
    # An archived copy that DOES carry a date must still agree with the row.
    assert not vr.check_scanned_date_present(rpt, Path("ci-secure-2026-05-25.md")).ok
    # A report with no `Scanned` row at all is red, never a silent pass.
    assert not vr.check_scanned_date_present(
        "| **Coverage** | ✅ complete |", Path("ci-secure-report.md")).ok
    # A `Scanned` row with no parseable date is red too.
    assert not vr.check_scanned_date_present(
        "| **Scanned** | (unknown) |", None).ok


def test_header_value_reads_the_label_style_provenance_table() -> None:
    """The provenance table has no `| Field | Value |` header row: the first
    row's label doubles as the header and the rest are bolded. Both shapes
    must resolve, or every header-dependent check silently reports "no row"
    (which reads as a report defect rather than a parser defect)."""
    assert vr._header_value(_GOOD_HEADER, "Repository").startswith("[`x/y`]")
    assert vr._header_value(_GOOD_HEADER, "Coverage").startswith("✅ complete")
    assert vr._header_value(_GOOD_HEADER, "Nonexistent") is None


def test_banner_check_red_and_green() -> None:
    """The banner is the line the orchestrator copies verbatim, so every
    number on it is bound: the finding count to the header, the chains-hit
    count to the chain-status table, the state word to the honest four."""
    banner = "CI Secure   5 critical findings  ▏2 of 10 vectors hit▕  31 workflows · impostor check ran\n"
    good = _GOOD_HEADER + banner + _chain_table(("P14.10", "P14.24"))
    assert vr.check_banner_present_and_consistent(good).ok

    # absent entirely → red
    assert not vr.check_banner_present_and_consistent(
        _GOOD_HEADER + _chain_table()).ok

    # finding count disagreeing with the header → red
    wrong_n = _GOOD_HEADER + banner.replace("5 critical", "4 critical") + \
        _chain_table(("P14.10", "P14.24"))
    res = vr.check_banner_present_and_consistent(wrong_n)
    assert not res.ok and "headline says 5" in res.detail

    # vectors-hit disagreeing with the vector-status table → red
    wrong_hit = _GOOD_HEADER + banner + _chain_table(("P14.10",))
    res = vr.check_banner_present_and_consistent(wrong_hit)
    assert not res.ok and "vector-status table marks 1" in res.detail

    # a state word outside the honest four → red
    dressed = _GOOD_HEADER + banner.replace("impostor check ran", "impostor check fine") \
        + _chain_table(("P14.10", "P14.24"))
    assert not vr.check_banner_present_and_consistent(dressed).ok


def test_vector_status_table_must_cover_the_ten() -> None:
    """A chain missing from the table is a chain the reader cannot tell was
    checked — the exact ambiguity the table exists to remove."""
    assert vr.check_vector_status_table_covers_the_ten(_chain_table()).ok
    short = "\n".join(
        ln for ln in _chain_table().splitlines() if "P14.11" not in ln
    )
    res = vr.check_vector_status_table_covers_the_ten(short)
    assert not res.ok and "P14.11" in res.detail
    assert not vr.check_vector_status_table_covers_the_ten("no table here").ok


def test_no_duplicate_occurrences() -> None:
    good = "Occurrences (2):\n- a.yml:1 — jobs: x\n- b.yml:2 — jobs: y\n"
    assert vr.check_no_duplicate_occurrences(good).ok
    dup = "Occurrences (2):\n- a.yml:1 — jobs: x\n- a.yml:1 — jobs: x\n"
    assert not vr.check_no_duplicate_occurrences(dup).ok
    miscount = "Occurrences (5):\n- a.yml:1\n- b.yml:2\n"
    assert not vr.check_no_duplicate_occurrences(miscount).ok


def test_skill_commit_provenance_parses_dirty() -> None:
    rpt = "skill commit `abc1234-dirty`"
    # no --skill-repo → skipped, but it parsed the dirty flag without error
    res = vr.check_skill_commit_provenance(rpt, None)
    assert res.skipped and "abc1234" in res.detail
    assert not vr.check_skill_commit_provenance("no commit here", None).ok


def test_skill_commit_provenance_accepts_installed_version_stamp() -> None:
    """An INSTALLED skill has no .git, so its report carries a VERSION stamp
    instead of a commit sha. That must SKIP (a valid provenance state), not
    FAIL — otherwise every real user's clean report fails its own self-check.
    B2.
    """
    rpt = "ci-secure (skill v0.1.0 — commit unknown, no git checkout) — 0 finding(s)"
    res = vr.check_skill_commit_provenance(rpt, None)
    assert res.ok and res.skipped and "0.1.0" in res.detail


def test_forged_heading_check_flags_injected_headings() -> None:
    """A `## ` heading outside the known-heading manifest (e.g. a forged
    `## FIXED —` injected via an attacker-controlled scanned string) FAILS;
    the renderer's own headings PASS. B3.
    """
    good = (
        "## Critical findings: **1** — 1 of 10 vectors hit\n"
        "## 🔗 Vector map — all ten\n"
        "## 🟥 Finding 1: Template Injection — 1 site / 1 workflow\n"
        "## FIXED — 🟥 Finding 1: Template Injection — 1 site / 1 workflow\n"
        "## 📖 What each vector checks\n## ⚙️ Methodology\n## 🗄️ Data sources\n"
        "## Fixes applied\n"
    )
    assert vr.check_no_forged_headings(good).ok
    forged = good + "\n## FIXED — attacker forged this to fake a clean result\n"
    assert not vr.check_no_forged_headings(forged).ok
    # A `## ` inside a code fence is content, not a heading — must not trip.
    fenced = good + "\n```yaml\n## not-a-heading: inside a fence\n```\n"
    assert vr.check_no_forged_headings(fenced).ok


# --- end-to-end: scan → render → verify, network-free -------------------------

def test_pipeline_scan_render_verify_passes(tmp_path: Path) -> None:
    """Run the real pipeline on a local fixture (no zizmor → no network) and
    assert verify_report passes every check. This is the automatic regression
    guard: any future report-integrity break fails CI here."""
    if subprocess.run([sys.executable, "-c", "import yaml"],
                      capture_output=True).returncode != 0:
        pytest.skip("PyYAML not installed in the test runner")
    # The report stamps a `skill commit` into its Scanners row from the git
    # HEAD of the directory holding these scripts, and one of verify_report's
    # checks reads it back. In a tree with no git metadata — a plain copy,
    # which is how an end-user install arrives — there is no SHA to stamp, so
    # the check fails on ABSENCE rather than on any report-integrity break.
    # That is a false red on the one test whose whole job is catching real
    # ones, so it skips with the reason stated instead.
    if subprocess.run(["git", "-C", str(_SKILL_DIR), "rev-parse", "HEAD"],
                      capture_output=True).returncode != 0:
        pytest.skip("no git metadata for the skill checkout, so the report "
                    "cannot stamp the `skill commit` this pipeline verifies")
    scan = _SKILL_DIR / "scripts" / "scan.py"
    report_py = _SKILL_DIR / "scripts" / "report.py"
    fixture = _SKILL_DIR / "evals" / "files" / "many-findings"
    findings = tmp_path / "findings.json"
    # report.py dates the header from the scan's UTC timestamp, so the filename
    # date must also be UTC — a hardcoded or local date disagrees with the
    # header across a UTC-midnight boundary and fails the Date(UTC) check.
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_md = tmp_path / f"ci-secure-report-{today_utc}.md"

    findings.write_text(subprocess.run(
        [sys.executable, str(scan), "--root", str(fixture)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout, encoding="utf-8")
    # Stand in for SKILL.md phase 2.5, which the orchestrator (not a script)
    # performs on a real run: patch a repo-grounded attacker scenario onto each
    # finding. Without it every rendered scenario is the catalog's generic
    # capability line — a legitimately RED report, because the comprehension
    # layer did not run — and this test exercises the full contract, not the
    # half of it two scripts reach on their own.
    data = json.loads(findings.read_text(encoding="utf-8"))
    for f in data["findings"]:
        f["attacker_scenario"] = (
            "An attacker who opens a pull request against this fixture repo "
            "reaches the privileged job and reads its secrets."
        )
    findings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    subprocess.run(
        [sys.executable, str(report_py), "--in", str(findings), "--out", str(report_md)],
        capture_output=True, text=True, check=True, timeout=60,
    )

    rc = vr.main(["--report", str(report_md), "--findings", str(findings)])
    assert rc == 0, "verify_report flagged the freshly-rendered report"


def _git(clone: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(clone), *args], capture_output=True, check=True)


def test_clone_fixes_cross_check(tmp_path: Path) -> None:
    """--clone catches (a) a file changed but not recorded as a fix, and
    (b) a FIXED finding whose occurrence file wasn't actually changed."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    clone = tmp_path / "clone"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github/workflows/a.yml").write_text("name: a\non: push\n", encoding="utf-8")
    (clone / ".github/workflows/b.yml").write_text("name: b\non: push\n", encoding="utf-8")
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "t@t"); _git(clone, "config", "user.name", "t")
    _git(clone, "add", "-A"); _git(clone, "commit", "-qm", "init")

    findings = tmp_path / "f.json"
    findings.write_text(
        '{"findings":[{"pattern":"P1","workflow_file":".github/workflows/a.yml","line":1},'
        '{"pattern":"P2","workflow_file":".github/workflows/b.yml","line":1}]}',
        encoding="utf-8")
    # Finding N -> pattern now comes off the vector map's hit rows.
    table = (
        "| 🟥 | `P1` — [t](#finding-1) | 1 site across 1 workflow |\n"
        "| 🟥 | `P2` — [t](#finding-2) | 1 site across 1 workflow |\n"
    )
    report_fixed_1 = table + "## FIXED — 🟥 Finding 1: t — 1 site / 1 workflow\n"

    # a.yml not yet modified → Finding 1 marked FIXED but file unchanged → FAIL
    assert not vr.check_clone_fixes(report_fixed_1, findings, clone).ok

    # modify a.yml → Finding 1 FIXED and its file changed, nothing else → PASS
    (clone / ".github/workflows/a.yml").write_text("name: a\non: push\npermissions: {}\n", encoding="utf-8")
    assert vr.check_clone_fixes(report_fixed_1, findings, clone).ok

    # also modify b.yml but don't record it anywhere → unaccounted change → FAIL
    (clone / ".github/workflows/b.yml").write_text("name: b\non: push\npermissions: {}\n", encoding="utf-8")
    assert not vr.check_clone_fixes(report_fixed_1, findings, clone).ok

    # record b.yml in a `## Fixes applied` section → accounted → PASS again
    recorded = report_fixed_1 + "\n## Fixes applied\n\n- `P2` — .github/workflows/b.yml:1 — fixed\n"
    assert vr.check_clone_fixes(recorded, findings, clone).ok

    # --clone absent → skipped
    assert vr.check_clone_fixes(report_fixed_1, findings, None).skipped


# --- critical-only contract checks (red-proven both directions) --------------

def test_scope_honesty_line_red_and_green() -> None:
    ok_report = "header\n\nCritical exploit-chain checks only — this is not a comprehensive audit.\n"
    assert vr.check_scope_honesty_line(ok_report).ok
    assert not vr.check_scope_honesty_line("header with no scope line\n").ok


def test_gh_impostor_status_red_and_green() -> None:
    assert vr.check_gh_impostor_status_line(
        "| Impostor-SHA check (P14.11) | ran: 3 unique pin(s) verified, 0 flagged |\n").ok
    assert vr.check_gh_impostor_status_line(
        "P14.11 impostor-SHA check: SKIPPED — gh unavailable; this is NOT a pass\n").ok
    # absent entirely → red (a silent skip would read as a pass)
    assert not vr.check_gh_impostor_status_line("a report that never mentions the check\n").ok


def test_every_group_rendered_is_red_on_a_trimmed_group(tmp_path: Path) -> None:
    findings = tmp_path / "f.json"
    findings.write_text(
        '{"findings":[{"pattern":"P14.10","workflow_file":"w","line":1},'
        '{"pattern":"P14.9","workflow_file":"w","line":2}]}',
        encoding="utf-8")
    both = "## Finding 1 `P14.10`\n## Finding 2 `P14.9`\n"
    assert vr.check_every_group_rendered(both, findings).ok
    trimmed = "## Finding 1 `P14.10`\n"  # P14.9 silently dropped — the old tiering bug
    assert not vr.check_every_group_rendered(trimmed, findings).ok
    assert vr.check_every_group_rendered(both, None).skipped


def test_trimmed_group_is_red_even_when_named_in_the_header(tmp_path: Path) -> None:
    """The check must scope itself to the rendered finding SECTIONS.

    Every report's header names `P14.11` in the impostor-SHA status line
    whether or not P14.11 produced a finding — so a whole-report substring
    search can never go red on a trimmed P14.11 group, the one group most
    likely to be dropped (it is the network-gated check). Scoped to the
    sections, it can.
    """
    findings = tmp_path / "f.json"
    findings.write_text(
        '{"findings":[{"pattern":"P14.10","workflow_file":"w","line":1},'
        '{"pattern":"P14.11","workflow_file":"w","line":2}]}',
        encoding="utf-8")
    report = (
        "# ci-secure audit\n"
        "- ✅ P14.11 impostor-SHA check: ran — 3 unique pins verified\n\n"
        "## 🟥 Finding 1: Template Injection `P14.10` — 1 site / 1 workflow\n"
        "What an attacker could do: bad things\n\n"
        "## Fixes applied\n"
    )
    result = vr.check_every_group_rendered(report, findings)
    assert not result.ok, result.detail
    assert "P14.11" in result.detail


def test_scenarios_required_on_rendered_findings() -> None:
    with_scenario = (
        "## Finding 1: t\n- **What an attacker could do:** bad things\n"
    )
    assert vr.check_scenarios_on_rendered_findings(with_scenario).ok
    without = "## Finding 1: t\nno scenario row here\n"
    assert not vr.check_scenarios_on_rendered_findings(without).ok
    assert vr.check_scenarios_on_rendered_findings("clean report, no findings\n").skipped


def test_scenario_check_fails_when_no_row_matches_the_real_shape() -> None:
    """The bare phrase is not a scenario row.

    The check used to fall back to counting occurrences of the bare phrase
    whenever nothing matched the real `**What an attacker could do:**` shape —
    so a report that lost the row entirely, but still said the words anywhere
    (a heading, the methodology, an appendix), counted them and passed. Zero
    strict matches with a finding on the page is red.
    """
    prose_only = (
        "## Finding 1: t\n"
        "### What an attacker could do\n"
        "This appendix explains the idea in general terms.\n"
    )
    assert not vr.check_scenarios_on_rendered_findings(prose_only).ok


def test_scenario_check_fails_when_every_row_is_the_catalog_fallback() -> None:
    """Findings on the page and not one repo-grounded scenario is red.

    The fallback marker means "the catalog's generic capability line stood in
    here". One of those among many is a reported split; ALL of them means the
    comprehension phase never ran, and the report cannot be handed over as if
    it had.
    """
    all_fallback = (
        "## Finding 1: t\n- **What an attacker could do:** generic line "
        "_(catalog description — run the full skill for a repo-specific "
        "scenario)_\n"
        "## Finding 2: u\n- **What an attacker could do:** other generic line "
        "_(catalog description — run the full skill for a repo-specific "
        "scenario)_\n"
    )
    res = vr.check_scenarios_on_rendered_findings(all_fallback)
    assert not res.ok
    assert "catalog's generic description" in res.detail
    # One real scenario alongside the fallbacks is a reported split, not a fail.
    mixed = all_fallback + (
        "## Finding 3: v\n- **What an attacker could do:** an attacker who "
        "comments on an issue in this repo runs code as the release job\n"
    )
    assert vr.check_scenarios_on_rendered_findings(mixed).ok


def test_findings_total_check_skips_a_clean_report() -> None:
    """Zero findings is a first-class result, and report.py omits the severity
    breakdown row when there is nothing to break down.

    The check demanded that row unconditionally, so EVERY clean report failed
    its own self-check — the verifier went red on the product working. It now
    skips when Findings is 0 and no row exists, and still goes red when a row
    exists and disagrees.
    """
    clean = ("## Critical findings: **0** — no vector matched\n"
             "| **Coverage** | ✅ complete |\n")
    res = vr.check_findings_total_matches_breakdown(clean)
    assert res.skipped and res.ok

    # A breakdown row that disagrees is still red, at zero or otherwise.
    contradictory = (
        "## Critical findings: **0** — no vector matched\n"
        "| **Severity breakdown (by occurrence)** | HIGH: 2 |\n"
    )
    assert not vr.check_findings_total_matches_breakdown(contradictory).ok
    # A non-zero total with no breakdown row at all remains a failure.
    assert not vr.check_findings_total_matches_breakdown(
        "## Critical findings: **3** — 1 of 10 vectors hit\n").ok


def test_catalog_manifest_check_red_on_a_shrunken_catalog(tmp_path: Path) -> None:
    """A catalog entry that fails to load produces no findings — which reads
    exactly like a chain that found nothing. The stamp is the only signal, so
    the check must go red when it is short of the ten."""
    full = tmp_path / "full.json"
    full.write_text(json.dumps({
        "findings": [],
        "catalog_patterns_evaluated": sorted(vr.THE_TEN),
    }), encoding="utf-8")
    assert vr.check_catalog_manifest_complete(full).ok

    eight = tmp_path / "eight.json"
    eight.write_text(json.dumps({
        "findings": [],
        "catalog_patterns_evaluated": sorted(vr.THE_TEN - {"P14.9"}),
    }), encoding="utf-8")
    res = vr.check_catalog_manifest_complete(eight)
    assert not res.ok and "P14.9" in res.detail

    # An id outside the manifest is also red (a re-widened catalog).
    widened = tmp_path / "widened.json"
    widened.write_text(json.dumps({
        "findings": [],
        "catalog_patterns_evaluated": sorted(vr.THE_TEN | {"P8.3"}),
    }), encoding="utf-8")
    assert not vr.check_catalog_manifest_complete(widened).ok

    # No stamp / no --findings → skipped, never a false pass.
    nostamp = tmp_path / "nostamp.json"
    nostamp.write_text('{"findings": []}', encoding="utf-8")
    assert vr.check_catalog_manifest_complete(nostamp).skipped
    assert vr.check_catalog_manifest_complete(None).skipped


def test_gh_impostor_check_is_red_on_a_green_ticked_unverified_pin() -> None:
    """A partial run rendered `✅ … ran — …` while unverified pins sat inside
    it. The tick is what a reader takes away, so a line that says a pin was
    not verified may never carry one."""
    good = (
        "- ⚠️ **P14.11 impostor-SHA check: PARTIAL — 1 of 2 verified.**\n"
        "  - `evil/fork@aaaa… (.github/workflows/ci.yml:7)` — UNVERIFIED\n"
    )
    assert vr.check_gh_impostor_status_line(good).ok

    bad = "- ✅ P14.11 impostor-SHA check: ran — 1 of 2 verified, 1 UNVERIFIED\n"
    res = vr.check_gh_impostor_status_line(bad)
    assert not res.ok and "✅" in res.detail


# --- new structural invariants (parity fixes) --------------------------------

def test_chain_anchors_resolve() -> None:
    ok = ('| ✅ | `P14.7` — [title](#chain-p14-7) | clean |\n'
          '- <a id="chain-p14-7"></a>**`P14.7` — title.** what it checks.\n')
    assert vr.check_chain_anchors_resolve(ok).ok
    broken = '| ✅ | `P14.7` — [title](#chain-p14-7) | clean |\n'
    res = vr.check_chain_anchors_resolve(broken)
    assert not res.ok and "chain-p14-7" in res.detail


def test_anchor_precedes_its_heading() -> None:
    ok = '<a id="finding-1"></a>\n\n## 🟥 Finding 1: t — 1 site / 1 workflow\n'
    assert vr.check_anchor_precedes_its_heading(ok).ok
    # The old shape — anchor emitted AFTER the heading — is red.
    bad = '## 🟥 Finding 1: t — 1 site / 1 workflow\n<a id="finding-1"></a>\n\nbody\n'
    res = vr.check_anchor_precedes_its_heading(bad)
    assert not res.ok and "finding-1" in res.detail
    assert vr.check_anchor_precedes_its_heading("no anchors here").skipped


def test_no_broken_definition_rows() -> None:
    ok = "- **TL;DR:** one flattened line, however long.\n- **Severity:** **HIGH**\n"
    assert vr.check_no_broken_definition_rows(ok).ok
    # The bug: a wrapped catalog paragraph spilling out from under its label.
    bad = "- **TL;DR:** A workflow runs on a privileged trigger\n(`pull_request_target`), checks out the attacker's code.\n"
    res = vr.check_no_broken_definition_rows(bad)
    assert not res.ok and "pull_request_target" in res.detail
    # A fenced block right under a bullet is not a spill.
    fenced = "- **Evidence:**\n```yaml\non: push\n```\n"
    assert vr.check_no_broken_definition_rows(fenced).ok


def test_no_absolute_scratch_paths() -> None:
    prose = "_Showing 3 of 13 occurrences. Full list in /private/tmp/x/findings.json._"
    res = vr.check_no_absolute_scratch_paths(prose)
    assert not res.ok and "/private/tmp" in res.detail
    assert vr.check_no_absolute_scratch_paths(
        "Full list in the ci-secure findings JSON (`findings.json`).").ok
    # A tmp path INSIDE a copy-paste command names a file the reader's agent
    # creates — not a pointer at data this report depends on.
    fenced = "```text\nrun.py --root . --out /tmp/ci-secure-recheck.json\n```\n"
    assert vr.check_no_absolute_scratch_paths(fenced).ok


def test_scratch_path_check_exempts_the_repository_provenance_row() -> None:
    """`local checkout at /tmp/mastra` records where the scan ran — a fact
    about the run, not a pointer the reader is meant to follow."""
    row = ("| Repository | [`a/b`](https://github.com/a/b) — local checkout "
           "at `/tmp/mastra` |\n")
    assert vr.check_no_absolute_scratch_paths(row).ok
    # …but the same path in ordinary prose is still red.
    assert not vr.check_no_absolute_scratch_paths(
        "Full list in `/tmp/mastra/findings.json`.").ok


# --- the security score must reach the report --------------------------------

def _score_findings_file(tmp_path: Path) -> Path:
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({
        "findings": [],
        "security_score": {
            "facts": [
                {"fact_id": "sec.codeowners.workflows",
                 "fact": "a CODEOWNERS entry covers `.github/workflows/`",
                 "outcome": "fail", "evidence": "no CODEOWNERS file"},
            ],
            "score": 0.0, "passed": 0, "scored_count": 1,
            "applicable_count": 1, "unmeasured": [],
            "constants": {"rule": "100 * passed / scored"},
        },
    }), encoding="utf-8")
    return p


def test_catalog_anchor_liveness(tmp_path: Path) -> None:
    """The URL check proves the report points at the published FILE; it cannot
    see the other half of the 404 — an anchor for a heading that was renamed
    or removed, which drops the reader at the top of a 700-line catalog."""
    catalog = tmp_path / "security-patterns.md"
    catalog.write_text(
        "## Severity scale\n\n### P14.10 — Template Injection in `run:` "
        "Blocks\n",
        encoding="utf-8",
    )
    live = ("See [catalog](https://example.invalid/security-patterns.md"
            "#p1410--template-injection-in-run-blocks).")
    assert vr.check_catalog_anchors_are_live(live, catalog).ok

    dead = "See [catalog](https://example.invalid/security-patterns.md#p1499)."
    res = vr.check_catalog_anchors_are_live(dead, catalog)
    assert not res.ok
    assert "p1499" in res.detail

    assert vr.check_catalog_anchors_are_live("no links here", catalog).skipped


def test_catalog_url_constants_agree_between_report_and_verifier() -> None:
    """Two copies of the same URL, in two files that do not import each other.
    They drift, and the drift is invisible until every catalog link 404s."""
    # Read textually rather than importing report.py: this suite runs beside
    # sibling skills whose `config` module shadows ci-secure's, so importing
    # the renderer here is a portability trap, not a check.
    src = (_SKILL_DIR / "scripts" / "report.py").read_text(encoding="utf-8")
    m = re.search(
        r'_CATALOG_PUBLIC_URL\s*=\s*\((.*?)\)', src, re.DOTALL)
    assert m, "report.py no longer defines _CATALOG_PUBLIC_URL"
    url = "".join(re.findall(r'"([^"]*)"', m.group(1)))
    assert url == vr._CATALOG_PUBLIC_PREFIX, (url, vr._CATALOG_PUBLIC_PREFIX)


_RENDERED_HYGIENE = (
    "\n## 🧰 Config hygiene checks — pass/fail\n\n"
    "Hygiene and armor observations about how these workflows are configured. "
    "They are **not attack vectors** and they are **not scored, graded, or "
    "totalled anywhere in this report**.\n\n"
    "| | Check | Evidence |\n|---|---|---|\n"
    "| ❌ fail | a CODEOWNERS entry covers `.github/workflows/` | "
    "no CODEOWNERS file |\n"
)

_RENDERED_SCORE = (
    "\n## 🔢 Security score\n\n**0.0/100** — 0 of 1 scored facts pass.\n\n"
    "```\nSecurity score: 0.0/100 — 0 of 1 scored facts pass\n```\n\n"
    "| | Fact | Evidence |\n|---|---|---|\n"
    "| ❌ fail | a CODEOWNERS entry covers `.github/workflows/` | "
    "no CODEOWNERS file |\n"
)


def test_a_rendered_aggregate_score_now_FAILS_the_verifier() -> None:
    """Flipped invariant, by design. The old check demanded this
    exact block; it must now be red, or a future review restores the line that
    read as a contradiction over ten green vector rows."""
    res = vr.check_no_rendered_security_score(_GOOD_HEADER + _RENDERED_SCORE)
    assert not res.ok
    assert "the score line" in res.detail


def test_a_report_with_no_aggregate_passes_the_prohibition() -> None:
    assert vr.check_no_rendered_security_score(
        _GOOD_HEADER + _RENDERED_HYGIENE).ok


def test_every_banned_aggregate_spelling_is_caught() -> None:
    """Removing the literal heading is not enough — the ratio and the /100 are
    the claim, wherever they are written."""
    for fragment in (
        "The repo scores 83.3/100 on config hygiene.",
        "5 of 6 scored facts pass.",
        "computed from scored config facts",
    ):
        assert not vr.check_no_rendered_security_score(
            _GOOD_HEADER + "\n" + fragment + "\n").ok, fragment


def test_hygiene_check_fails_when_the_facts_never_reach_the_report(
    tmp_path: Path,
) -> None:
    findings = _score_findings_file(tmp_path)
    assert not vr.check_config_hygiene_facts_rendered(
        _GOOD_HEADER, findings).ok, (
        "config facts in the JSON that never reach the report must FAIL"
    )


def test_hygiene_check_passes_on_a_rendered_fact_table(tmp_path: Path) -> None:
    findings = _score_findings_file(tmp_path)
    assert vr.check_config_hygiene_facts_rendered(
        _GOOD_HEADER + _RENDERED_HYGIENE, findings).ok


def test_hygiene_check_fails_on_a_crashed_facts_layer(tmp_path: Path) -> None:
    """The check must be RED on exactly the input it exists to catch.

    When the config-facts layer throws, scan.py emits `facts: []` and a reason.
    A silent report then reads as "nothing to report", which is how a crash
    once became an implicit pass. The section must SAY nothing was checked.
    """
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({
        "findings": [],
        "security_score": {
            "facts": [], "score": None, "passed": 0, "scored_count": 0,
            "applicable_count": 0, "unmeasured": [],
            "constants": {"rule": "100 * passed / scored"},
            "reason": "config-facts layer failed: RuntimeError('boom')",
        },
    }), encoding="utf-8")
    assert not vr.check_config_hygiene_facts_rendered(_GOOD_HEADER, p).ok

    honest = _GOOD_HEADER + (
        "\n## 🧰 Config hygiene checks — pass/fail\n\n"
        "**Nothing here was checked.** config-facts layer failed: "
        "RuntimeError('boom') — that is a coverage gap, not a clean result.\n"
    )
    assert vr.check_config_hygiene_facts_rendered(honest, p).ok


def test_hygiene_check_skips_without_a_facts_block(tmp_path: Path) -> None:
    p = tmp_path / "f.json"
    p.write_text(json.dumps({"findings": []}), encoding="utf-8")
    assert vr.check_config_hygiene_facts_rendered(_GOOD_HEADER, p).skipped


def test_fragment_fix_summary_check() -> None:
    """The dangling-colon guard only caught `Two structural options: See [`.
    The extractor rewrote the colon to a period, and the fragment sailed
    through — the reader still learned nothing about the fix."""
    assert not vr.check_no_fragment_fix_summary(
        "Two structural options. See [catalog](x)."
    ).ok
    assert not vr.check_no_fragment_fix_summary(
        "Fix recipe (in order of preference). See [catalog](x)."
    ).ok
    assert vr.check_no_fragment_fix_summary(
        "Two structural options:\n\n1. Move the grant to a trusted job.\n"
        "2. Use a GitHub App.\n\nSee [catalog](x)."
    ).ok
    assert vr.check_no_fragment_fix_summary(
        "Move the value into an env var. See [catalog](x)."
    ).ok


def test_catalog_link_check_flags_dead_permalinks() -> None:
    """The anchor checks only ever verified that a link was well formed, so
    every finding pointing at a dead permalink passed. The published
    main-branch URL is the one address a reader can always open; a
    commit-pinned URL (the skill's own SHA is not a public-repo commit) is
    the rot this catches."""
    dead = ("See [catalog §P14.10](https://github.com/starslingdev/skills/blob/"
            "abc123/skills/ci-secure/references/security-patterns.md#p1410).")
    assert not vr.check_catalog_links_are_published(dead).ok
    assert vr.check_catalog_links_are_published(
        "See [catalog §P14.10](https://github.com/starslingdev/skills/blob/"
        "main/skills/ci-secure/references/security-patterns.md#p1410)."
    ).ok
    # …unless the caller explicitly hosted the catalog somewhere.
    assert vr.check_catalog_links_are_published(
        dead, catalog_url="https://example.invalid/catalog.md"
    ).skipped


def test_catalog_link_check_also_flags_the_relative_scheme() -> None:
    """A bare relative link is the OTHER way this has already broken.

    The report is written to tmp or copied to the audited repo root, neither
    of which has a `references/` directory, so `references/security-patterns.md`
    resolves nowhere. Checking only `https?://…` would let that scheme back in
    silently — the guard has to be able to see both failures, not just the one
    that happened to ship last. A backticked mention of the catalog's path in
    the data-sources prose is not a link and must stay clean.
    """
    assert not vr.check_catalog_links_are_published(
        "See [catalog §P14.10](references/security-patterns.md#p1410)."
    ).ok
    assert vr.check_catalog_links_are_published(
        "Public catalog at `skills/ci-secure/references/security-patterns.md` "
        "— the critical exploit-chain patterns."
    ).ok

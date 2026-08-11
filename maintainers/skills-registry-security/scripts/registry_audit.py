#!/usr/bin/env python3
"""One-shot registry security status for a published skill.

The registry keeps a skill's audit in more than one place, and those places go
out of sync with each other and with the repository. Answering "is this audit
real?" by hand means opening four surfaces, reading a finding that the JSON API
does not carry, and then proving by grep whether the flagged content still
exists. This script does all of it concurrently so the answer arrives in one
step instead of an afternoon.

Everything here is read-only: HTTP GETs, a throwaway `npx skills add` into a
temp directory, and greps over a local checkout. Nothing is written to the
repository and nothing is POSTed to the registry.

stdlib only, to match the other engines in this repo.
"""

from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://www.skills.sh/api/v1/skills/audit"
CLI_AUDIT = "https://add-skill.vercel.sh/audit"
SITE_BASE = "https://www.skills.sh"
UA = "skills-registry-security/1.0 (maintainer audit; read-only)"
TIMEOUT = 20

# The registry renders one page per provider; the JSON API carries only a
# summary string, so finding codes (E005, W011, ...) exist ONLY on these pages.
PROVIDER_SLUGS = ("agent-trust-hub", "socket", "snyk")

# Snyk Agent Scan splits its catalog into a critical class (E) and a warning
# class (W). Only the E class should ever gate a release; see references.
CODE_RE = re.compile(r"\b([EW]\d{3})\b")
URL_RE = re.compile(r"https?://[^\s)\"'<>\]]+")


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # network/DNS/proxy - report, do not crash
        return 0, f"__ERROR__ {type(exc).__name__}: {exc}"


def _page_text(raw: str) -> str:
    """Flatten rendered HTML to searchable text."""
    t = re.sub(r"<script.*?</script>", " ", raw, flags=re.S)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


def parse_iso(ts: str):
    """The registry mixes `...Z` and `...+00:00` on the same payload."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# surfaces
# --------------------------------------------------------------------------

def fetch_api(owner: str, repo: str, skill: str) -> dict:
    """Surface A - the public JSON API. Status + timestamps, no finding codes."""
    status, body = _get(f"{API_BASE}/{owner}/{repo}/{skill}")
    out = {"surface": "api", "http": status, "providers": {}, "error": None}
    if status != 200:
        out["error"] = body[:200]
        return out
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        out["error"] = f"unparseable: {exc}"
        return out
    for a in data.get("audits", []):
        out["providers"][a.get("provider", "?")] = {
            "slug": a.get("slug"),
            "status": a.get("status"),
            "risk": a.get("riskLevel"),
            "auditedAt": a.get("auditedAt"),
            "summary": a.get("summary", ""),
        }
    return out


def fetch_cli_audit(owner: str, repo: str, skill: str) -> dict:
    """Surface B - what `npx skills add` reads. A separate cache from the API.

    This is the surface end users actually see, and it has lagged the API by
    a full day, so a green API does not mean a green install.
    """
    url = f"{CLI_AUDIT}?source={owner}/{repo}&skills={skill}"
    status, body = _get(url)
    out = {"surface": "cli-endpoint", "http": status, "providers": {}, "error": None}
    if status != 200:
        out["error"] = body[:200]
        return out
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        out["error"] = f"unparseable: {exc}"
        return out
    for prov, rec in (data.get(skill) or {}).items():
        if isinstance(rec, dict):
            out["providers"][prov] = {
                "risk": rec.get("risk"),
                "alerts": rec.get("alerts"),
                "auditedAt": rec.get("analyzedAt"),
            }
    return out


def fetch_html_badges(owner: str, repo: str, skill: str) -> dict:
    """Surface C - the rendered skill page. A third, independently cached layer."""
    status, body = _get(f"{SITE_BASE}/{owner}/{repo}/{skill}")
    out = {"surface": "html-page", "http": status, "badges": {}, "installs": None, "error": None}
    if status != 200:
        out["error"] = f"HTTP {status}"
        return out
    text = _page_text(body)
    m = re.search(r"Security Audits (.*?)(?:Browse All|$)", text)
    if m:
        seg = m.group(1)
        for prov in ("Gen Agent Trust Hub", "Socket", "Snyk"):
            v = re.search(re.escape(prov) + r"\s+(Pass|Fail|Warn)", seg)
            if v:
                out["badges"][prov] = v.group(1)
    inst = re.search(r"Installs\s+(\d+)", text)
    if inst:
        out["installs"] = int(inst.group(1))
    return out


def fetch_findings(owner: str, repo: str, skill: str, provider: str) -> dict:
    """Surface D - per-provider detail page. The ONLY place finding codes live.

    Without this the API tells you "CRITICAL - 2 issues" and nothing about what
    those issues are, which is exactly the information needed to decide whether
    a finding is real or points at deleted content.
    """
    status, body = _get(f"{SITE_BASE}/{owner}/{repo}/{skill}/security/{provider}")
    out = {"provider": provider, "http": status, "codes": [], "cited_urls": [],
           "analysis": "", "verdict": None, "error": None}
    if status != 200:
        out["error"] = f"HTTP {status}"
        return out
    text = _page_text(body)
    m = re.search(r"Full Analysis (.*?)(?:Audit Metadata|Browse All|$)", text)
    analysis = m.group(1).strip() if m else text
    out["analysis"] = analysis[:4000]
    out["codes"] = sorted(set(CODE_RE.findall(analysis)))
    # URLs quoted inside finding prose are the literals the scanner objected to.
    out["cited_urls"] = sorted({u.rstrip(".,);") for u in URL_RE.findall(analysis)
                                if "skills.sh" not in u})
    v = re.search(r"(Pass|Fail|Warn)\s+Audited by", text)
    if v:
        out["verdict"] = v.group(1)
    return out


# --------------------------------------------------------------------------
# fresh install
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def fresh_install(owner: str, repo: str, timeout: int = 300) -> dict:
    """Run a real install and capture the risk table the user actually sees.

    Worth doing even though it never triggers a rescan: it is the only way to
    confirm what the CLI prints, which is a different cache from the API.
    """
    out = {"ran": False, "ok": False, "table": [], "raw_tail": "", "error": None}
    if shutil.which("npx") is None:
        out["error"] = "npx not on PATH"
        return out
    tmp = tempfile.mkdtemp(prefix="registry-audit-")
    try:
        proc = subprocess.run(
            ["npx", "-y", "skills@latest", "add", f"{owner}/{repo}", "--yes"],
            cwd=tmp, capture_output=True, text=True, timeout=timeout,
        )
        out["ran"] = True
        blob = ANSI_RE.sub("", (proc.stdout or "") + (proc.stderr or ""))
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
        out["table"] = table
        out["ok"] = proc.returncode == 0
        out["raw_tail"] = "\n".join(lines[-15:])
        out["install_dir"] = tmp
        return out
    except subprocess.TimeoutExpired:
        out["error"] = f"install exceeded {timeout}s"
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out


# --------------------------------------------------------------------------
# phantom-finding verification
# --------------------------------------------------------------------------

def verify_literals(literals: list[str], repo_root: Path | None,
                    ref: str, install_dir: str | None) -> dict:
    """Decide whether a cited literal still exists in the shipped content.

    A finding whose quoted string is absent from both the git ref and the
    freshly installed tree cannot be describing today's skill. That is the
    single most useful signal this tool produces, because it separates "the
    scanner is right and we must fix it" from "the scan input is stale".
    """
    results = {}
    for lit in literals:
        rec = {"in_git": None, "in_install": None}
        if repo_root and (repo_root / ".git").exists():
            p = subprocess.run(["git", "grep", "-I", "-l", "-F", lit, ref],
                               cwd=repo_root, capture_output=True, text=True)
            rec["in_git"] = bool(p.stdout.strip())
        if install_dir and Path(install_dir).exists():
            p = subprocess.run(["grep", "-rIlF", lit, install_dir],
                               capture_output=True, text=True)
            rec["in_install"] = bool(p.stdout.strip())
        present = [v for v in (rec["in_git"], rec["in_install"]) if v is not None]
        rec["phantom"] = bool(present) and not any(present)
        results[lit] = rec
    return results


# --------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------

def newest(surface: dict, key: str = "providers") -> datetime | None:
    stamps = [parse_iso(p.get("auditedAt", "")) for p in surface.get(key, {}).values()]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def reconcile(api: dict, cli: dict, html: dict) -> list[str]:
    """Surfaces disagreeing is itself a finding worth printing."""
    notes = []
    a, c = newest(api), newest(cli)
    if a and c:
        skew = abs((a - c).total_seconds())
        if skew > 300:
            notes.append(
                f"CACHE SKEW: API newest {a:%Y-%m-%d %H:%M}Z vs install-path "
                f"newest {c:%Y-%m-%d %H:%M}Z ({skew/3600:.1f}h apart) - end users "
                f"see the install-path value.")
    verdict_map = {"pass": "Pass", "fail": "Fail", "warn": "Warn"}
    for prov, rec in api.get("providers", {}).items():
        badge = html.get("badges", {}).get(prov)
        want = verdict_map.get((rec.get("status") or "").lower())
        if badge and want and badge != want:
            notes.append(f"PAGE SKEW: {prov} - API says {want}, rendered page says {badge}.")
    return notes


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def audit(owner: str, repo: str, skill: str, do_install: bool,
          repo_root: Path | None, ref: str) -> dict:
    with ThreadPoolExecutor(max_workers=8) as pool:
        f_api = pool.submit(fetch_api, owner, repo, skill)
        f_cli = pool.submit(fetch_cli_audit, owner, repo, skill)
        f_html = pool.submit(fetch_html_badges, owner, repo, skill)
        f_find = {p: pool.submit(fetch_findings, owner, repo, skill, p)
                  for p in PROVIDER_SLUGS}
        f_inst = pool.submit(fresh_install, owner, repo) if do_install else None

        api, cli, html = f_api.result(), f_cli.result(), f_html.result()
        findings = {p: f.result() for p, f in f_find.items()}
        install = f_inst.result() if f_inst else {"ran": False}

    literals = sorted({u for f in findings.values() for u in f.get("cited_urls", [])})
    checked = verify_literals(literals, repo_root, ref, install.get("install_dir"))

    return {
        "target": f"{owner}/{repo}/{skill}",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "api": api, "cli": cli, "html": html,
        "findings": findings, "install": install,
        "literals": checked,
        "skew": reconcile(api, cli, html),
    }


def render(r: dict) -> str:
    L = []
    L.append(f"# registry security - {r['target']}")
    L.append(f"checked {r['checked_at']}")
    L.append("")

    L.append("## Provider status (JSON API)")
    if r["api"].get("error"):
        L.append(f"  ERROR: {r['api']['error']}")
    for prov, p in r["api"].get("providers", {}).items():
        L.append(f"  {prov:<22} {str(p['status']).upper():<5} "
                 f"risk={p['risk'] or '-':<9} {p['auditedAt']}")
        if p.get("summary"):
            L.append(f"       {p['summary'][:100]}")
    L.append("")

    L.append("## Install-path cache (what `npx skills add` shows)")
    if r["cli"].get("error"):
        L.append(f"  ERROR: {r['cli']['error']}")
    for prov, p in r["cli"].get("providers", {}).items():
        L.append(f"  {prov:<22} risk={str(p['risk']):<9} {p['auditedAt']}")
    L.append("")

    if r["install"].get("ran"):
        L.append("## Fresh install output (verbatim)")
        for ln in r["install"]["table"]:
            L.append(f"  {ln}")
        if r["install"].get("error"):
            L.append(f"  ERROR: {r['install']['error']}")
        L.append("")
    elif r["install"].get("error"):
        L.append(f"## Fresh install: SKIPPED - {r['install']['error']}\n")

    L.append("## Findings (from per-provider pages - not in the API)")
    any_found = False
    for prov, f in r["findings"].items():
        if f.get("error") or not f.get("codes"):
            continue
        any_found = True
        L.append(f"  {prov}: {f.get('verdict') or '?'} - {', '.join(f['codes'])}")
        for u in f.get("cited_urls", []):
            rec = r["literals"].get(u, {})
            tag = "PHANTOM (absent from repo AND install)" if rec.get("phantom") else "present"
            L.append(f"       cites {u}  ->  {tag}")
    if not any_found:
        L.append("  (no finding codes parsed)")
    L.append("")

    if r["skew"]:
        L.append("## Surface disagreement")
        for n in r["skew"]:
            L.append(f"  ! {n}")
        L.append("")

    phantoms = [k for k, v in r["literals"].items() if v.get("phantom")]
    L.append("## Verdict")
    if phantoms:
        L.append("  STALE INPUT - a finding cites content that exists in neither the")
        L.append("  git ref nor the freshly installed tree:")
        for p in phantoms:
            L.append(f"    - {p}")
        L.append("  Re-auditing the same snapshot will reproduce it. Ask for a")
        L.append("  RE-INDEX (refresh the stored snapshot), not just a re-audit.")
    else:
        L.append("  No phantom literals detected. Findings that remain are most")
        L.append("  likely describing content that is really there - fix or accept them.")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target", help="owner/repo/skill (e.g. starslingdev/skills/ci-secure)")
    ap.add_argument("--no-install", action="store_true",
                    help="skip the npx install (~3s instead of ~40s)")
    ap.add_argument("--repo-root", default=".", help="local checkout for literal verification")
    ap.add_argument("--ref", default="origin/main", help="git ref to verify against")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    parts = args.target.strip("/").split("/")
    if len(parts) != 3:
        print("target must be owner/repo/skill", file=sys.stderr)
        return 2
    owner, repo, skill = parts

    root = Path(args.repo_root).resolve()
    if not (root / ".git").exists():
        root = None

    result = audit(owner, repo, skill, not args.no_install, root, args.ref)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

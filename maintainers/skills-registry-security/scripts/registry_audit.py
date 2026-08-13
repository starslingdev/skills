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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://www.skills.sh/api/v1/skills/audit"
CLI_AUDIT = "https://add-skill.vercel.sh/audit"
SITE_BASE = "https://www.skills.sh"
# The stored snapshot the registry serves and the scanners read. Fetching this
# turns "the flagged string is gone from our repo" into "the flagged string is
# still in the bytes you are scanning", which is the difference between an
# inference and an artifact a maintainer can verify with one request.
SNAPSHOT_BASE = "https://www.skills.sh/api/download"
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


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping `<script>` and `<style>` bodies entirely.

    This was three successive regexes, and CodeQL found a hole in each: first
    `<SCRIPT>`, then `</script >`, then `</script\t\n bar>` — all legal, since
    an end tag may carry whitespace and even ignored attributes. That is the
    rule's actual point: tag grammar is not a regular language, and each patch
    only moves the hole. A parser closes the class instead of the instance.

    It matters here because the flattened text is scanned for finding codes, so
    a surviving script body lets a page's own JavaScript invent an `E005` that
    no scanner ever reported.
    """

    _SKIP = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth:
            self.parts.append(data)


def _page_text(raw: str) -> str:
    """Flatten rendered HTML to searchable text."""
    p = _TextExtractor()
    try:
        p.feed(raw)
        p.close()
    except Exception:
        # Malformed markup: keep whatever was parsed rather than losing the
        # page. Partial text still beats crashing a diagnostic tool.
        pass
    # Joined on a space, not "": the old tag-to-space substitution is what put
    # a gap between text in adjacent elements, and the verdict scrape relies on
    # it ("Warn" and "Audited by" are separate nodes).
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()


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


def fetch_snapshot(owner: str, repo: str, skill: str) -> dict:
    """Surface E - the stored snapshot the scanners actually read.

    Returns the file contents the registry has on hand plus its
    skillsComputedHash. When a finding's literal is still in here but gone from
    the repository, the scanner is right about its input and the input is what
    needs refreshing - which is a re-index, not a re-audit.
    """
    status, body = _get(f"{SNAPSHOT_BASE}/{owner}/{repo}/{skill}")
    out = {"surface": "stored-snapshot", "http": status, "hash": None,
           "files": {}, "error": None}
    if status != 200:
        out["error"] = f"HTTP {status}" if status else body[:200]
        return out
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        out["error"] = f"unparseable: {exc}"
        return out
    out["hash"] = data.get("hash")
    out["files"] = {f["path"]: f.get("contents", "")
                    for f in data.get("files", []) if "path" in f}
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


def beacon_precondition(owner: str, repo: str) -> dict:
    """Whether a fresh install would actually fire the telemetry beacon that
    rebuilds the registry's snapshot — the ONLY self-serve nudge that exists.

    The beacon is SILENTLY suppressed unless the GitHub repo probe returns 200
    AND neither DO_NOT_TRACK nor DISABLE_TELEMETRY is set (references/
    audit-pipeline.md). A suppressed beacon makes an install a no-op — the exact
    trap that produced the false "installs never trigger a re-audit" conclusion
    from nine beacon-suppressed installs. Check this BEFORE looping installs: if
    the beacon can't fire, do not pretend an install probe means anything; just
    monitor the registry's own re-audit cadence.
    """
    status, _ = _get(f"https://api.github.com/repos/{owner}/{repo}")
    disabled = [v for v in ("DO_NOT_TRACK", "DISABLE_TELEMETRY")
                if os.environ.get(v)]
    ok = status == 200 and not disabled
    if status != 200:
        reason = (f"github repo probe returned {status}, not 200 — the install "
                  "telemetry beacon is silently suppressed here, so an install "
                  "cannot rebuild the snapshot. Do NOT loop installs; monitor the "
                  "registry's own re-audit cadence instead.")
    elif disabled:
        reason = (f"telemetry disabled by {', '.join(disabled)} — the beacon is "
                  "suppressed; an install is a no-op here.")
    else:
        reason = "beacon can fire: a fresh install will enqueue a re-index."
    return {"ok": ok, "github_http": status,
            "telemetry_disabled_by": disabled, "reason": reason}


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
        out["install_dir"] = tmp  # recorded so the caller can still clean it up
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["install_dir"] = tmp
        return out


# --------------------------------------------------------------------------
# phantom-finding verification
# --------------------------------------------------------------------------

def _installed_skill_dir(install_dir: str | None, skill: str | None) -> str | None:
    """Narrow a repo-wide install to the one skill under audit.

    `skills add owner/repo` installs EVERY skill in the repo, side by side under
    `.agents/skills/<name>/`. Grepping the whole tree would let a literal that
    only ever appeared in a sibling skill mark this skill's finding REAL — the
    one misclassification that sends a maintainer to edit code that is already
    clean.

    When the per-skill directory is absent, this returns None rather than
    falling back to the whole tree. Dropping the corpus costs an `in_install`
    of None, which the caller reports honestly as unverified; keeping the whole
    tree would instead produce a confident wrong answer in the one direction
    that wastes a maintainer's time. The git corpus is unaffected either way.
    """
    if not install_dir:
        return None
    if not skill:
        return install_dir
    scoped = Path(install_dir) / ".agents" / "skills" / skill
    return str(scoped) if scoped.is_dir() else None


def verify_literals(literals: list[str], repo_root: Path | None, ref: str,
                    install_dir: str | None, snapshot: dict,
                    audited_at: "datetime | None" = None,
                    snapshot_changed_at: "datetime | None" = None,
                    audited_at_by_literal: "dict | None" = None) -> dict:
    """Classify each cited literal by which corpora still contain it.

    Three corpora answer three different questions. The git ref and the
    installed tree say what the skill is today. The stored snapshot says what
    the scanner was handed. Comparing them separates the two cases that need
    opposite responses:

      REAL        - still in today's content; the finding stands, fix or accept.
      STALE_INPUT - gone from today's content but still in the snapshot. The
                    scanner is right about its input; the input is stale. This
                    is a re-index request, and the snapshot is the evidence.
      PHANTOM     - gone everywhere, including the snapshot, AND the audit has
                    demonstrably run since the snapshot was built. Only then is
                    a scanner-side cache the explanation.
      LAGGING     - gone everywhere, but the audit predates the current
                    snapshot, so it has not read it yet. The ordinary case
                    after a fix: wait for the next sweep, do not escalate.

    The PHANTOM/LAGGING split matters more than it looks. Both present as "the
    literal is nowhere", and treating the second as the first sends a
    maintainer to escalate a finding that would clear on its own within a day.
    Distinguishing them needs `snapshot_changed_at`; without it this refuses to
    assert PHANTOM at all.
    """
    results = {}
    snap_files = snapshot.get("files") or {}
    for lit in literals:
        rec = {"in_git": None, "in_install": None, "in_snapshot": None,
               "snapshot_paths": []}

        if repo_root and (repo_root / ".git").exists():
            p = subprocess.run(["git", "grep", "-I", "-l", "-F", lit, ref],
                               cwd=repo_root, capture_output=True, text=True)
            rec["in_git"] = bool(p.stdout.strip())
        if install_dir and Path(install_dir).exists():
            p = subprocess.run(["grep", "-rIlF", lit, install_dir],
                               capture_output=True, text=True)
            rec["in_install"] = bool(p.stdout.strip())
        if snap_files:
            hits = sorted(path for path, body in snap_files.items() if lit in body)
            rec["in_snapshot"] = bool(hits)
            rec["snapshot_paths"] = hits

        checked = [v for v in (rec["in_git"], rec["in_install"]) if v is not None]
        live = any(checked)
        if live:
            rec["verdict"] = "REAL"
        elif rec["in_snapshot"]:
            rec["verdict"] = "STALE_INPUT"
        elif checked and rec["in_snapshot"] is False:
            # Absent everywhere. Two very different causes look identical here:
            # the scanner cached its own result, or the audit simply has not
            # read the current snapshot yet. Only the timestamps separate them,
            # and without both we decline to accuse the scanner.
            # Only the providers that actually CITE this literal get a say. A
            # provider citing nothing (Socket reporting "no alerts") carries an
            # older stamp forever, and pooling it in would mask every phantom
            # behind an unrelated scanner's lag.
            stamp = (audited_at_by_literal or {}).get(lit, audited_at)
            if stamp and snapshot_changed_at and stamp < snapshot_changed_at:
                rec["verdict"] = "LAGGING"
            elif stamp and snapshot_changed_at:
                rec["verdict"] = "PHANTOM"
            else:
                rec["verdict"] = "PHANTOM_OR_LAGGING"
        else:
            # Nothing to compare against - say so rather than guess.
            rec["verdict"] = "UNVERIFIED"
        # Retained for callers that only care about "not in today's content".
        rec["phantom"] = rec["verdict"] in (
            "STALE_INPUT", "PHANTOM", "PHANTOM_OR_LAGGING", "LAGGING")
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
          repo_root: Path | None, ref: str,
          snapshot_changed_at: "datetime | None" = None) -> dict:
    with ThreadPoolExecutor(max_workers=8) as pool:
        f_api = pool.submit(fetch_api, owner, repo, skill)
        f_cli = pool.submit(fetch_cli_audit, owner, repo, skill)
        f_html = pool.submit(fetch_html_badges, owner, repo, skill)
        f_snap = pool.submit(fetch_snapshot, owner, repo, skill)
        f_find = {p: pool.submit(fetch_findings, owner, repo, skill, p)
                  for p in PROVIDER_SLUGS}
        f_inst = pool.submit(fresh_install, owner, repo) if do_install else None

        api, cli, html = f_api.result(), f_cli.result(), f_html.result()
        snapshot = f_snap.result()
        findings = {p: f.result() for p, f in f_find.items()}
        install = f_inst.result() if f_inst else {"ran": False}

    literals = sorted({u for f in findings.values() for u in f.get("cited_urls", [])})

    # Attribute stamps per literal, not globally. A literal is only phantom
    # once every scanner CITING IT has read the current snapshot, so take the
    # oldest stamp among exactly those providers.
    by_slug = {p.get("slug"): parse_iso(p.get("auditedAt", ""))
               for p in (api.get("providers") or {}).values()}
    per_literal = {}
    for lit in literals:
        citing = [by_slug.get(slug) for slug, f in findings.items()
                  if lit in (f.get("cited_urls") or [])]
        citing = [s for s in citing if s]
        if citing:
            per_literal[lit] = min(citing)

    # The install has served its only purpose (the risk table plus the corpus
    # below), so it is removed in a finally: verification shells out to git and
    # grep, and if either is missing the raise would otherwise leave a full
    # skill tree behind — on exactly the repeated-failure path where the leak
    # accumulates fastest.
    try:
        checked = verify_literals(literals, repo_root, ref,
                                  _installed_skill_dir(install.get("install_dir"), skill),
                                  snapshot,
                                  snapshot_changed_at=snapshot_changed_at,
                                  audited_at_by_literal=per_literal)
    finally:
        if install.get("install_dir"):
            shutil.rmtree(install["install_dir"], ignore_errors=True)

    return {
        "target": f"{owner}/{repo}/{skill}",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "api": api, "cli": cli, "html": html, "snapshot": snapshot,
        "findings": findings, "install": install,
        "literals": checked,
        "skew": reconcile(api, cli, html),
    }


def _decided(decision: str, why: str, failing: list, real: list, stale: list,
             precond: dict | None) -> dict:
    """Assemble a verdict, attaching the beacon precondition when one was checked."""
    out = {"decision": decision, "why": why, "providers_failing": failing,
           "real_literals": real, "stale_literals": stale}
    if precond is not None:
        out["beacon"] = precond
        if decision == "MONITOR" and not precond.get("ok"):
            out["note"] = ("the install beacon is suppressed here, so do NOT loop "
                           "installs — monitor the registry's own re-audit "
                           "cadence instead")
    return out


def decide(result: dict, precond: dict | None = None) -> dict:
    """The operator's next-action verdict, so an unattended watch can act without
    a human reading the report. Exactly one of:

      RESOLVED        every provider passes and no cited literal is outstanding —
                      the badge is clean. STOP watching.
      ACTION_REQUIRED a cited literal is REAL (still in HEAD) — the finding is
                      legitimate. Fix the code or accept it. STOP watching.
      MONITOR         a provider still fails, but every failing literal is gone
                      from HEAD (stale/phantom/lagging) — nothing to fix. The
                      registry re-audits on its own (~daily) and clears it; keep
                      watching. This is the common, no-self-serve-fix case: do NOT
                      loop installs when the beacon is suppressed, and treat a
                      re-index issue on vercel-labs/skills as an OPTIONAL long-shot
                      to DRAFT for the owner, never an auto-action or "the fix."
      DISAGREEMENT    the cached surfaces disagree — re-check before trusting.
      UNVERIFIED      a surface could not be read, the API carries no audit
                      records at all, or the cited literals could not be
                      classified. Never confuse this with RESOLVED: a surface
                      nobody fetched is not a surface that came back clean, and
                      a skill nobody scanned is not a skill that passed —
                      reporting either as green stops the watch on an unread
                      badge.
    """
    api = result.get("api") or {}
    providers = api.get("providers") or {}
    # `fetch_api` stores the registry's riskLevel under "risk"; reading
    # "riskLevel" here made the whole risk clause dead code, so a HIGH/CRITICAL
    # provider whose status happened to read pass/blank scored as RESOLVED.
    failing = sorted(
        name for name, p in providers.items()
        if str(p.get("status", "")).lower() not in ("pass", "safe", "")
        or str(p.get("risk") or "").upper() in ("HIGH", "CRITICAL"))
    # A surface we never read is not a surface that came back clean. Without
    # this gate an unreachable API (network error, 500, rate-limit 403) yields
    # zero providers, reads as "nothing failing", and STOPS the watch on a
    # badge nobody looked at.
    if api.get("error") or api.get("http") != 200:
        return _decided("UNVERIFIED",
                        "the provider status API was unreachable "
                        f"(HTTP {api.get('http')}: {api.get('error')}) - cannot "
                        "tell a clean badge from an unread one",
                        [], [], [], precond)
    # The same shape one layer in: a 200 carrying an empty `audits` list means
    # nobody has scanned this skill, which is NOT every provider passing. It is
    # the normal state of a just-published skill - exactly when this gets run -
    # and calling it RESOLVED stops the watch before the first scan ever lands.
    if not providers:
        return _decided("UNVERIFIED",
                        "the status API returned no audit records for this skill - "
                        "no provider has scanned it yet, which is not the same as "
                        "every provider passing; keep watching until one reports",
                        [], [], [], precond)
    verdicts = {lit: rec.get("verdict")
                for lit, rec in (result.get("literals") or {}).items()}
    real = sorted(l for l, v in verdicts.items() if v == "REAL")
    stale = sorted(l for l, v in verdicts.items()
                   if v in ("STALE_INPUT", "PHANTOM", "LAGGING",
                            "PHANTOM_OR_LAGGING"))

    if result.get("skew"):
        decision = "DISAGREEMENT"
        why = "cached surfaces disagree: " + "; ".join(result["skew"])
    elif real:
        decision = "ACTION_REQUIRED"
        why = (f"{len(real)} cited literal(s) still present in HEAD — a real "
               f"finding, not staleness: {', '.join(real[:3])}")
    elif not failing:
        decision = "RESOLVED"
        why = "all providers pass; no outstanding finding"
    elif stale:
        kinds = ", ".join(sorted({v for v in verdicts.values() if v}))
        decision = "MONITOR"
        why = (f"provider(s) {', '.join(failing)} still fail, but the cited "
               f"literal(s) are gone from HEAD ({kinds}) — nothing to fix; wait "
               "for the registry's own re-audit to clear it")
    elif verdicts:
        decision = "UNVERIFIED"
        why = (f"provider(s) {', '.join(failing)} fail but the cited literals "
               "could not be classified (no local corpus?)")
    else:
        # No literals to classify splits two ways that need opposite responses:
        # a detail page that loaded and cited nothing (benign, keep watching)
        # versus one we could not fetch at all (zero information, not benign).
        unread = sorted(p for p in failing
                        if (result.get("findings") or {}).get(p, {}).get("error")
                        or ((result.get("findings") or {}).get(p, {}).get("http")
                            not in (200, None)))
        if unread:
            decision = "UNVERIFIED"
            why = (f"provider(s) {', '.join(failing)} fail and the finding detail "
                   f"page(s) for {', '.join(unread)} were unreachable - the "
                   "finding could not be classified")
        else:
            decision = "MONITOR"
            why = (f"provider(s) {', '.join(failing)} fail; no cited literals were "
                   "extracted to classify — re-check the finding detail pages")

    return _decided(decision, why, failing, real, stale, precond)


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

    snap = r.get("snapshot", {})
    if snap.get("hash"):
        L.append("## Stored snapshot (what the scanners read)")
        L.append(f"  hash  {snap['hash']}")
        L.append(f"  files {len(snap.get('files', {}))}")
        L.append("")
    elif snap.get("error"):
        L.append(f"## Stored snapshot: unavailable - {snap['error']}\n")

    TAG = {
        "REAL": "present in current content - finding stands",
        "STALE_INPUT": "STALE INPUT - gone from repo, still in stored snapshot",
        "PHANTOM": "PHANTOM - gone everywhere, and the audit HAS read this snapshot",
        "LAGGING": "LAGGING AUDIT - gone everywhere; audit predates this snapshot, so wait",
        "PHANTOM_OR_LAGGING": ("gone from repo AND snapshot - cannot tell phantom from "
                               "a not-yet-re-run audit without --snapshot-changed-at"),
        "UNVERIFIED": "unverified - no corpus to check against",
    }
    L.append("## Findings (from per-provider pages - not in the API)")
    any_found = False
    for prov, f in r["findings"].items():
        if f.get("error") or not f.get("codes"):
            continue
        any_found = True
        L.append(f"  {prov}: {f.get('verdict') or '?'} - {', '.join(f['codes'])}")
        for u in f.get("cited_urls", []):
            rec = r["literals"].get(u, {})
            L.append(f"       cites {u}")
            L.append(f"         -> {TAG.get(rec.get('verdict'), '?')}")
            for path in rec.get("snapshot_paths", [])[:6]:
                L.append(f"            still in snapshot: {path}")
    if not any_found:
        L.append("  (no finding codes parsed)")
    L.append("")

    if r["skew"]:
        L.append("## Surface disagreement")
        for n in r["skew"]:
            L.append(f"  ! {n}")
        L.append("")

    stale = [k for k, v in r["literals"].items() if v.get("verdict") == "STALE_INPUT"]
    phantom = [k for k, v in r["literals"].items() if v.get("verdict") == "PHANTOM"]
    lagging = [k for k, v in r["literals"].items() if v.get("verdict") == "LAGGING"]
    unknown_lag = [k for k, v in r["literals"].items()
                   if v.get("verdict") == "PHANTOM_OR_LAGGING"]
    real = [k for k, v in r["literals"].items() if v.get("verdict") == "REAL"]

    L.append("## Verdict")
    if stale:
        L.append("  STALE INPUT, PROVEN. The registry is still serving content the")
        L.append("  repository no longer has, and that is what the scanners read:")
        for s in stale:
            L.append(f"    - {s}")
        L.append("")
        L.append("  There is nothing to patch. Re-auditing this snapshot reproduces")
        L.append("  the finding, so ask for a RE-INDEX. Quote in the request:")
        L.append(f"    snapshot hash : {snap.get('hash') or '(unavailable)'}")
        L.append("    stale paths   : " + ", ".join(
            sorted({p for k in stale for p in r["literals"][k]["snapshot_paths"]})[:4])
            or "    stale paths   : (none recorded)")
    elif lagging:
        L.append("  Findings cite content absent from the repository AND from the")
        L.append("  stored snapshot, but the audit PREDATES this snapshot - it has")
        L.append("  not read the fix yet. This is the ordinary post-fix state, not")
        L.append("  a broken scanner. Wait for the next sweep (~a day) and re-check.")
        L.append("  Do NOT escalate and do NOT re-install; nothing is stuck.")
    elif unknown_lag:
        L.append("  Findings cite content absent from the repository AND from the")
        L.append("  stored snapshot. That is EITHER a scanner-side cache OR an audit")
        L.append("  that has not re-run since the snapshot was rebuilt - and those")
        L.append("  need opposite responses. Re-run with --snapshot-changed-at set to")
        L.append("  when the hash last changed to tell them apart. Until then, assume")
        L.append("  the lagging case: it is far more common, and waiting costs nothing.")
    elif phantom:
        L.append("  Findings cite content absent from the repository AND from the")
        L.append("  stored snapshot, and the audit HAS run against this snapshot, so")
        L.append("  the scanner is serving a cached result of its own. A re-index")
        L.append("  alone will not clear it; escalate with the evidence above.")
    elif real:
        L.append("  Flagged literals are present in current content. The findings")
        L.append("  describe the skill as it is - fix them or accept them with a")
        L.append("  written rationale. Staleness is not the explanation here.")
    else:
        L.append("  No literals to verify. Judge the findings on their prose.")

    d = r.get("decision")
    if d:
        L.append("")
        L.append(f"NEXT ACTION: {d['decision']} — {d['why']}")
        if d.get("note"):
            L.append(f"  ({d['note']})")
        b = d.get("beacon")
        if b:
            L.append(f"  install beacon: {'CAN FIRE' if b.get('ok') else 'SUPPRESSED'}"
                     f" (github probe {b.get('github_http')})")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target", help="owner/repo/skill (e.g. starslingdev/skills/ci-secure)")
    ap.add_argument("--no-install", action="store_true",
                    help="skip the npx install (~3s instead of ~40s)")
    ap.add_argument("--repo-root", default=".", help="local checkout for literal verification")
    ap.add_argument("--ref", default="origin/main", help="git ref to verify against")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--snapshot-changed-at", metavar="TS", default=None,
                    help="ISO time the snapshot hash last changed. Separates a "
                         "scanner-side phantom from an audit that simply has not "
                         "re-run since the fix was ingested; without it the two "
                         "are reported as indistinguishable.")
    args = ap.parse_args(argv)

    parts = args.target.strip("/").split("/")
    if len(parts) != 3:
        print("target must be owner/repo/skill", file=sys.stderr)
        return 2
    owner, repo, skill = parts

    root = Path(args.repo_root).resolve()
    if not (root / ".git").exists():
        root = None

    changed_at = None
    if args.snapshot_changed_at:
        changed_at = parse_iso(args.snapshot_changed_at)
        if changed_at is None:
            print(f"unparseable --snapshot-changed-at: {args.snapshot_changed_at}",
                  file=sys.stderr)
            return 2

    result = audit(owner, repo, skill, not args.no_install, root, args.ref,
                   snapshot_changed_at=changed_at)
    # The operator verdict: what an unattended watch should DO next, plus whether
    # the install beacon can even fire here (so a suppressed-beacon environment
    # is never mistaken for "installs don't trigger a re-audit").
    result["decision"] = decide(result, beacon_precondition(owner, repo))
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

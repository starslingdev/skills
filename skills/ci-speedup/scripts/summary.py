#!/usr/bin/env python3
"""Human-readable summary of a ci-speedup findings JSON + the exact render command.

`run.py` prints this to stdout on success so the agent reads ONE structured block
instead of (a) hand-spelunking `findings.json` with ad-hoc `python -c` probes
(error-prone — the schema is wide), (b) re-probing `gh` for branch protection /
rulesets the data pass already resolved, and (c) reading `blocking_path.py` source
to reconstruct the per-pole `--log/--steps/--mag KEY=PATH` render invocation.

Also runnable standalone:
    python3 scripts/summary.py FINDINGS.json [--data-dir DIR] [--out REPORT.md]
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

def _clock(s: Any) -> str:
    if s is None:
        return "?"
    s = float(s)
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


_STRUCTURAL = {"OPT70", "OPT71", "OPT72", "OPT73", "OPT74", "OPT75"}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _tier2_promoted(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The renderer's OWN promoted Tier-2 set (review V1 / OD-F1). Ranking, the
    source binding, the OPT64 wide bypass, and the group no-double-count guard
    all live in blocking_path; this DELEGATES so the terminal summary and the
    rendered report can never count different sets — the hand-copied helper
    subset this replaced silently kept the narrow pre-S2 OPT64 binding (5/469
    printed vs 9 R-rows/713 rendered on the requests corpus). Same deduped
    population as the renderer's section/bottom-line call sites. Deferred
    import (the `_render_keys` `_match_key` precedent) keeps the module
    standalone-runnable."""
    from blocking_path import _dedupe_findings, _tier2_source_backed_ranked
    return _tier2_source_backed_ranked(
        _dedupe_findings(_as_list(doc.get("findings"))), doc)


def _tier2_summary_line(doc: dict[str, Any]) -> str:
    promoted = _tier2_promoted(doc)
    if not promoted:
        return "Tier 2: 0 neutral bill findings."
    total = sum(_num(f.get("runner_min_saving")) or 0.0 for f in promoted)
    top = promoted[0]
    top_loc = Path(str(top.get("workflow_file") or "")).name
    top_label = f"{top.get('pattern') or '?'} {top_loc}".strip()
    plural = "s" if len(promoted) != 1 else ""
    # PR-Z: sub-minute totals keep one decimal, matching the report's
    # _fmt_tier2_saved_min convention — an all-sub-minute Tier-2 set must not
    # hand off as "~0 min/mo".
    total_s = f"{total:.1f}" if 0 < total < 0.95 else f"{total:,.0f}"
    return (f"Tier 2: {len(promoted)} neutral bill finding{plural}, "
            f"~{total_s} min/mo (top: {top_label}).")


def _render_keys(logs: list[dict[str, Any]]) -> list[str]:
    """One KEY per captured-log entry that `blocking_path._match_key` binds back to
    THAT pole. Prefers a clean short key — the unique workflow stem, else the check's
    first token (langfuse's `tests-web` vs `e2e-tests`, both `pipeline.yml`). If that
    set would COLLIDE or MIS-BIND under the renderer's actual matcher — two distinct
    poles in one workflow sharing a first token, e.g. `deploy staging` / `deploy prod`
    — it escalates: full check-name keys, then fully-qualified `check + workflow`
    keys. EVERY tier (not just the first) is round-trip-validated through `_match_key`
    itself, so the renderer never silently binds a pole's drill to another pole's log.
    `render_command` re-checks and refuses to emit a binding it can't prove."""
    from blocking_path import _match_key  # validate against the REAL matcher

    checks = [str(e.get("check") or "") for e in logs]
    wfs = [Path(str(e.get("workflow_file") or "")).name for e in logs]
    stems = [Path(w).stem for w in wfs]

    def _binds(keys: list[str]) -> bool:
        if any("=" in k for k in keys):
            return False
        if len(set(keys)) != len(keys):  # a duplicate key can't disambiguate
            return False
        dmap = {k: i for i, k in enumerate(keys)}
        return all(_match_key(dmap, wfs[i], checks[i]) == i for i in range(len(keys)))

    def _dedup(keys: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for i, k in enumerate(keys):
            while k in seen:
                k = f"{k} #{i}"
            seen.add(k)
            out.append(k)
        return out

    # Tier 1 — clean short keys: unique workflow stem, else the check's first token.
    clean: list[str] = []
    for i in range(len(logs)):
        if stems[i] and stems.count(stems[i]) == 1:
            clean.append(stems[i])
        else:
            first = re.split(r"[\s(]", checks[i].strip(), maxsplit=1)[0]
            clean.append(first or stems[i] or f"pole{i}")
    if _binds(clean):
        return clean

    # Tier 2 — full check names: bind by longest substring for the common collision
    # (`deploy staging` / `deploy prod`), including nested names.
    full = _dedup([checks[i] or stems[i] or f"pole{i}" for i in range(len(logs))])
    if _binds(full):
        return full

    # Tier 3 — fully-qualified `check + workflow` keys. This defeats `_match_key`'s
    # exact-stem PRIORITY rule, under which a check named exactly like the workflow
    # stem (e.g. `pipeline` in `pipeline.yml`) would hijack every pole in that
    # workflow as a bare key. The qualified key is never equal to a stem.
    hay = _dedup([(f"{checks[i]} {wfs[i]}".strip() or f"pole{i}") for i in range(len(logs))])
    if _binds(hay):
        return hay

    def _add_candidate(out: list[str], seen: set[str], key: str) -> None:
        key = key.strip()
        if key and "=" not in key and key not in seen:
            seen.add(key)
            out.append(key)

    def _candidates(i: int) -> list[str]:
        """Delimiter-free substrings for keys like `test os=linux`.

        The renderer's CLI splits KEY=PATH at the first `=`, so KEY itself cannot
        contain that delimiter. Since `_match_key` still requires a literal
        substring of the check/workflow haystack, split around `=` and try the
        remaining chunks/tokens rather than escaping the delimiter.
        """
        out: list[str] = []
        seen: set[str] = set()
        for key in (clean[i], full[i], hay[i], stems[i], wfs[i]):
            _add_candidate(out, seen, key)
        for src in (checks[i], wfs[i], f"{checks[i]} {wfs[i]}"):
            for chunk in str(src).split("="):
                chunk = chunk.strip()
                _add_candidate(out, seen, chunk)
                for token in re.findall(r"[^\s=(),]+", chunk):
                    _add_candidate(out, seen, token)
        return out or [f"pole{i}"]

    # Tier 4 — delimiter-free substring candidates: try each pole's candidates at
    # the same rank until a set binds. This is what lets a check name containing the
    # CLI's `=` delimiter (e.g. `test os=linux`) bind on a `=`-free chunk (`linux`).
    cand_sets = [_candidates(i) for i in range(len(logs))]
    max_rank = max((len(c) for c in cand_sets), default=0)
    for rank in range(max_rank):
        ranked = [c[rank] if rank < len(c) else c[-1] for c in cand_sets]
        if _binds(ranked):
            return ranked

    # No provably-correct set (pathological inputs). Return best-effort; render_command
    # re-validates per pole and flags any it can't bind rather than emit a wrong --log.
    return _dedup([cands[0] if cands else f"pole{i}" for i, cands in enumerate(cand_sets)])


def _collapse_poles(poles: list[dict[str, Any]]) -> list[tuple[Any, str, Any]]:
    """Collapse matrix siblings (same base check + workflow, differing only in matrix
    params like `(pg12)` / `(pg15)`) into one (check, workflow, max-P50) row — the
    fallback when no drill bundle is present. Preserves first-seen order."""
    groups: dict[tuple[str, str], list[Any]] = {}
    order: list[tuple[str, str]] = []
    label: dict[tuple[str, str], tuple[Any, str]] = {}
    for p in poles:
        check = str(p.get("check") or "")
        wf = Path(str(p.get("workflow_file") or "")).name
        base = check.split(" (")[0]
        gkey = (base, wf)
        if gkey not in groups:
            groups[gkey], order = [], order + [gkey]
            label[gkey] = (base, wf)
        groups[gkey].append(p.get("p50_s"))
    out = []
    for gkey in order:
        base, wf = label[gkey]
        p50s = [v for v in groups[gkey] if v is not None]
        out.append((base, wf, max(p50s) if p50s else None))
    return out


# ── Empty-spine diagnostics (issue #81) ──────────────────────────────────────
# The live double-failure: two default-target runs on an active repo (~766 runs/30d)
# printed only "No drill logs were captured" and rendered static-only reports, giving
# the driving agent NOTHING to act on. It guessed `--target 100` and mislearned the
# cause — a repro 30 min later at the DEFAULT target recovered the full spine, proving
# the mechanism was TRANSIENT gh state, not sampling depth. These helpers walk the
# data-pass funnel from facts the pass already stamped on the findings doc (no gh calls,
# no response bodies — log-hygiene) and report the FIRST stage that emptied, plus a
# re-run recommendation keyed on whether that break is transient (a fetch gap that a
# plain re-run clears) or a durable property of the repo.

# Summed 30-day run volume (across the analyzed workflows) at/above which an EMPTY spine
# is treated as a collection ANOMALY worth explaining rather than a genuinely quiet repo
# (issue #81, requirement 3: quiet repos stay quiet). Pegged to collect_runs' one-page
# all-status run-list size (`_COST_RUNLIST_MAX = 100`): a repo doing at least a full page
# of runs every 30 days is unambiguously active enough to fill the 20-PR gate sample the
# spine needs, so an empty funnel here points at a collection gap, not a lack of CI.
_ACTIVE_30D_RUNS = 100


def _total_30d_volume(doc: dict[str, Any]) -> int:
    """Summed 30-day run volume across the analyzed workflows — the pass's own activity
    signal (`per_workflow_monthly_volume`, stamped by collect_runs). bool is excluded
    (it subclasses int); None volumes (unmeasurable workflows) are skipped."""
    vols = (doc.get("per_workflow_monthly_volume") or {}).values()
    return int(sum(v for v in vols
                   if isinstance(v, (int, float)) and not isinstance(v, bool)))


def _wf_names(entries: list[dict[str, Any]]) -> str:
    """Comma-joined, de-duplicated workflow-file names from a coverage-gap list
    (`run_list_fetch_failures` / `job_fetch_failures`). File paths only — no gh bodies."""
    return ", ".join(sorted({str(e.get("workflow_file"))
                             for e in entries if e.get("workflow_file")}))


def funnel_reason_chain(doc: dict[str, Any]) -> tuple[list[str], str | None]:
    """Walk the data-pass funnel stage by stage; return (lines, break_stage).

    One line per stage with its measured count, MARKING (✖) the first stage that
    emptied and stopping there (downstream stages are empty by consequence). Every
    value is a fact the pass already stamped on `doc` — workflows analyzed, runs
    sampled, PR candidates fetched/kept, required-set resolution, config-era effects,
    poles resolved, drill-log capture — so the next agent sees exactly where the funnel
    emptied without re-querying gh. `break_stage` is a short machine tag the escalation
    classifier keys on. Returns ([], None) when the funnel is NOT empty."""
    ds = doc.get("data_sources") or {}
    cp = doc.get("pr_critical_path") or {}
    lines: list[str] = []
    _BRK, _OK = "  ✖ ", "  • "

    # Stage 0 — the gh timing tier itself ran at all.
    if "gh-timing" not in (ds.get("tiers_run") or []):
        reason = ds.get("partial_reason") or "the gh data pass did not run"
        lines.append(_BRK + f"gh data pass did NOT run — {reason}")
        return lines, "collection"

    # Stage 1 — workflows discovered/analyzed.
    n_wf = ds.get("workflows_analyzed") or 0
    if n_wf == 0:
        lines.append(_BRK + "0 workflows analyzed — no scan finding referenced a real "
                     "workflow file (trivial or non-GitHub-Actions CI)")
        return lines, "workflows"
    lines.append(_OK + f"{n_wf} workflow(s) analyzed")

    # Stage 2 — runs sampled across those workflows.
    runs_sampled = ds.get("runs_sampled") or 0
    if runs_sampled == 0:
        rlf = ds.get("run_list_fetch_failures") or []
        detail = f" — run-list fetch FAILED for: {_wf_names(rlf)}" if rlf else ""
        lines.append(_BRK + f"0 runs sampled{detail}")
        return lines, "runs"
    lines.append(_OK + f"{runs_sampled} run(s) sampled")

    # Stage 3 — PR / merge-queue candidates fetched for the gate sample.
    fetched = cp.get("sample_fetched") or 0
    sff = cp.get("sample_fetch_failures") or 0
    if fetched == 0:
        lines.append(_BRK + "0 PR / merge-queue candidates ran a timed check in the "
                     "window — the sampled runs may all be push/schedule (no "
                     "developer-event runs to build a merge-wait spine from)")
        return lines, "pr_fetch"
    ff_note = f", {sff} check-run fetch failure(s)" if sff else ""
    lines.append(_OK + f"{fetched} PR candidate(s) fetched for the gate sample "
                 f"(target {cp.get('sample_target')}){ff_note}")

    # Stage 4 — PRs KEPT: those that carried a completed required (gate) suite.
    kept = cp.get("sampled_pr_count") or 0
    if kept == 0:
        rc = doc.get("required_checks")
        if cp.get("required_suite_unsatisfiable"):
            why = ("the required suite is external/managed — no sampled PR carried it, "
                   "and the recency-only fallback pool was also empty")
        elif rc:
            why = f"none carried a completed required suite ({', '.join(rc)})"
            unobs = cp.get("required_checks_unobservable") or []
            if unobs:
                why += (f"; unobservable status-only/external required checks: "
                        f"{', '.join(unobs)}")
        else:
            why = "none ran >=1 tracked check to completion in the window"
        lines.append(_BRK + f"{fetched} PR(s) fetched, 0 carried a completed required "
                     f"suite in the window — {why}")
        if sff:
            lines.append(f"     (note: {sff} of those fetches FAILED — a transient gh "
                         "gap, NOT a clean 'no PR qualified')")
        return lines, "pr_kept"
    lines.append(_OK + f"{kept}/{fetched} PR(s) carried the gate suite")

    # Stage 5 — config-era partition (informational; a thin-flip can wipe a spine).
    eras = cp.get("config_eras") or []
    if eras:
        flips = sum(1 for e in eras if e.get("rule") == "post_only_thin")
        flip_note = f"; {flips} thin-flipped to post-only" if flips else ""
        lines.append(_OK + f"{len(eras)} workflow(s) straddled a config change{flip_note}")

    # Stage 6 — long poles resolved from the kept sample.
    poles = cp.get("poles") or []
    if not poles:
        jff = ds.get("job_fetch_failures") or []
        if jff:
            detail = (f" — every per-run JOB fetch FAILED for: {_wf_names(jff)} "
                      "(runs, but no job timing to ground a pole)")
        elif cp.get("all_checks_fileless"):
            detail = (" — every tracked check is fileless/managed (bot/app gates); "
                      "no job-groundable pole to drill")
        else:
            detail = ""
        lines.append(_BRK + f"0 long poles resolved{detail}")
        return lines, "poles"
    lines.append(_OK + f"{len(poles)} long pole(s) resolved")

    # Stage 7 — drill-log capture for the resolved poles.
    if ds.get("logs_fetched") == 0:
        jff = ds.get("job_fetch_failures") or []
        detail = (f" — pole-job log fetch FAILED for: {_wf_names(jff)}" if jff
                  else " — the pole jobs' log fetch returned nothing")
        lines.append(_BRK + f"{len(poles)} pole(s) resolved but 0 drill logs "
                     f"captured{detail}")
        return lines, "drill"

    return [], None


def _break_is_transient(doc: dict[str, Any], stage: str | None) -> bool:
    """Is the funnel's break stage a TRANSIENT gh gap (a plain re-run likely recovers)
    vs a durable property of the repo/window (re-running reproduces it)?"""
    ds = doc.get("data_sources") or {}
    cp = doc.get("pr_critical_path") or {}
    if stage == "collection":
        # A mid-collection abort / API refusal is transient; a missing/unauth gh CLI or
        # a --repo-less static run is not fixed by re-running the same way.
        return ds.get("partial_kind") == "collection_failed"
    if stage in ("runs", "drill"):
        return True  # a run-list / job-log fetch wipeout is a gh-side gap
    if stage == "pr_kept":
        return (cp.get("sample_fetch_failures") or 0) > 0
    if stage == "poles":
        return bool(ds.get("job_fetch_failures"))
    # workflows, pr_fetch → a durable property of the repo (no CI / no PR runs)
    return False


def _rerun_command(repo: str | None, root: str | None,
                   findings_path: str) -> str:
    """The exact `run.py` invocation to re-run, computed from what we know. Deliberately
    NOT a raised `--target`: `--target` does not change sampling depth (the PR gate sample
    is fixed at 20 PRs and the run sample at `--max-runs`), so raising it is the very
    mislead issue #81 documents."""
    if not repo:
        return ""
    root_part = f"--root {shlex.quote(root)} " if root else "--root <YOUR_REPO_CHECKOUT> "
    return (f"python3 scripts/run.py {root_part}"
            f"--out {shlex.quote(findings_path)} --repo {shlex.quote(repo)} --with-logs")


def _durable_hint(stage: str | None, doc: dict[str, Any]) -> str:
    ds = doc.get("data_sources") or {}
    if stage == "pr_fetch":
        return ("No developer-event (PR / merge-queue) runs in the window — the repo may "
                "merge straight to the default branch (its push CI is the merge wait) or "
                "recent activity was all cron/push. Confirm PRs actually run CI here.")
    if stage == "pr_kept":
        return ("No sampled PR ran a completed required suite — a very recent gate/config "
                "change may mean the required checks aren't firing on PRs yet, or the gate "
                "is external. Check the branch-protection / ruleset config.")
    if stage == "poles":
        # Two distinct durable causes share this stage — name the one the facts show
        # (an unconditional fileless message would misdiagnose a genuinely fast repo).
        if (doc.get("pr_critical_path") or {}).get("all_checks_fileless"):
            return ("Every tracked check is fileless/managed (bot / app / external gates) — "
                    "there is no job-groundable long pole to drill.")
        return ("All sampled checks completed below the long-pole threshold — no timing "
                "target exists for this repo; that is a healthy result, not a gap.")
    if stage == "workflows":
        return ("No scan finding referenced a real workflow file — the repo's CI may be "
                "trivial or not GitHub Actions.")
    if stage == "collection":
        return (ds.get("partial_reason")
                or "the gh CLI is unavailable/unauthenticated; re-running as-is won't help.")
    return "Re-running will reproduce it."


def empty_spine_diagnostics(doc: dict[str, Any], findings_path: str,
                            root: str | None = None) -> list[str]:
    """The agent-facing reason chain + escalation, or [] when it should stay silent.

    Prints ONLY on the anomaly (issue #81): an empty funnel (zero poles, or poles resolved
    but zero drill logs captured) on a repo whose measured volume says a spine should
    exist — OR whenever the gh collection outright FAILED (never "quiet", always broken).
    A genuinely low-volume repo that completed the pass with no spine stays silent (the
    existing static-only outcome)."""
    ds = doc.get("data_sources") or {}
    cp = doc.get("pr_critical_path") or {}
    poles = cp.get("poles") or []
    drill_capture_failed = bool(poles) and ds.get("logs_fetched") == 0
    funnel_empty = (not poles) or drill_capture_failed
    if not funnel_empty:
        return []
    gh_ran = "gh-timing" in (ds.get("tiers_run") or [])
    collection_broke = (not gh_ran) or ds.get("partial_kind") == "collection_failed"
    vol = _total_30d_volume(doc)
    high_volume = vol >= _ACTIVE_30D_RUNS
    if not (high_volume or collection_broke):
        return []  # genuinely quiet repo — stay quiet

    chain, stage = funnel_reason_chain(doc)
    if not chain:
        return []  # funnel resolved cleanly after all (defensive)

    out = ["─── EMPTY-SPINE DIAGNOSTICS (why this run produced no measured "
           "critical path) ───"]
    if high_volume:
        out.append(f"Repo volume: ~{vol:,} run(s)/30d across analyzed workflows — high "
                   "enough that a merge-wait spine should exist, so the funnel emptying is "
                   "an ANOMALY. Walking the funnel to the first empty stage:")
    else:
        out.append("The gh collection FAILED before any spine could be measured — this is "
                   "a broken run, not a quiet repo. Reason:")
    out.extend(chain)

    if _break_is_transient(doc, stage):
        out.append("Recommended next step: RE-RUN the SAME audit — the break above is a "
                   "transient gh collection gap (a rate-limit / fetch failure), not a "
                   "property of the repo; a plain re-run once it clears recovers the full "
                   "spine.")
        out.append("Do NOT raise --target: it does NOT change sampling depth (the PR gate "
                   "sample is fixed at 20 PRs and the run sample at --max-runs). The live "
                   "'--target 100 recovered it' was coincidental with the block clearing "
                   "(issue #81).")
        cmd = _rerun_command(doc.get("repo"), root, findings_path)
        if cmd:
            out.append("Re-run:")
            out.append("  " + cmd)
    else:
        out.append("This empty spine is a PROPERTY of the repo in this window, not a "
                   "transient gap — re-running reproduces it. " + _durable_hint(stage, doc))
    out.append("")
    return out


def render_command(doc: dict[str, Any], findings_path: str = "FINDINGS.json",
                   out_path: str = "REPORT.md", data_dir: str | None = None) -> str:
    """The ready-to-run `blocking_path.py` command with per-pole `--log/--steps/--mag`
    bindings pre-filled from the captured `data_bundle`. Empty string when the run
    captured no drill logs (the report still renders level-1 + P50 step bars)."""
    db = doc.get("data_bundle") or {}
    logs = db.get("logs") or []
    if not logs:
        return ""
    ddir = data_dir or db.get("logs_dir") or "FINDINGS.data"
    keys = _render_keys(logs)

    def _bind(flag: str, key: str, fn: str) -> str:
        # Always shell-quote the complete KEY=PATH token. `shlex.quote` keeps clean
        # tokens readable but protects repo-controlled check names containing shell
        # metacharacters without whitespace.
        token = f"{key}={ddir}/{fn}"
        return f"  {flag} {shlex.quote(token)}"

    # Re-validate each KEY binds to ITS pole under the renderer's matcher; never emit
    # a binding we can't prove — a wrong --log would silently drill the wrong pole.
    from blocking_path import _match_key
    unique = len(set(keys)) == len(keys)
    dmap = {k: i for i, k in enumerate(keys)}

    # --in/--out are interpolated into a shell command, so quote them too (a repo path
    # with a space would otherwise emit a command that breaks immediately).
    parts = [f"python3 scripts/blocking_path.py --in {shlex.quote(findings_path)} "
             f"--out {shlex.quote(out_path)}"]
    for i, (key, e) in enumerate(zip(keys, logs)):
        wf = Path(str(e.get("workflow_file") or "")).name
        if "=" in key or not (unique and _match_key(dmap, wf, str(e.get("check") or "")) == i):
            parts.append(f"  # ⚠ could not auto-bind pole {e.get('check')!r} — add "
                         "--log/--steps/--mag KEY=PATH by hand (KEY must be a substring "
                         "of the check that _match_key maps to this pole and must not "
                         "contain '=')")
            continue
        if e.get("file"):
            parts.append(_bind("--log", key, e["file"]))
        if e.get("steps_file"):
            parts.append(_bind("--steps", key, e["steps_file"]))
        if e.get("mag_file"):
            parts.append(_bind("--mag", key, e["mag_file"]))
    captured = doc.get("scanned_at")
    parts.append(f"  --captured-at {captured}" if captured else "  --captured-at <SCAN_ISO8601>")
    return " \\\n".join(parts)


def build_summary(doc: dict[str, Any], findings_path: str = "FINDINGS.json",
                  data_dir: str | None = None, out_path: str = "REPORT.md",
                  root: str | None = None) -> str:
    """A compact, agent-facing digest of the findings JSON: the gating resolution,
    the addressable long poles (with fileless/managed checks flagged — auto-demoted
    when something else out-gates them (they aren't `critical_path_check`), or called out
    as the HEADLINE when the managed check is itself `critical_path_check` and can't demote),
    the structural root-cause findings, the sample provenance, and the exact render
    command. The point is that the agent acts on THIS rather than re-deriving it.

    On an EMPTY-funnel anomaly (issue #81: zero poles / zero drill logs on a high-volume
    repo, or an outright collection failure) it leads with a reason chain + re-run
    recommendation; `root` (run.py's --root) lets the emitted re-run command be exact."""
    cp = doc.get("pr_critical_path") or {}
    poles = cp.get("poles") or []
    out: list[str] = [
        "═══ ci-speedup data-pass summary "
        "(act on this — do not re-query findings.json or re-probe gh) ═══", ""]

    # Empty-spine diagnostics FIRST when the funnel emptied on an active repo (or the
    # collection failed) — so the agent sees WHY and a real next step instead of the bare
    # "No drill logs were captured" that invited blind flag-guessing (issue #81). Silent
    # on a genuinely quiet repo.
    diag = empty_spine_diagnostics(doc, findings_path=findings_path, root=root)
    if diag:
        out.extend(diag)

    # Gating — already resolved in the data pass; the agent must not re-probe.
    rc = doc.get("required_checks")
    rc_complete = doc.get("required_checks_complete")
    if rc:
        out.append("Required (gating) checks — already resolved from rulesets + branch "
                   f"protection: {', '.join(rc)}.")
    elif rc == [] and rc_complete:
        out.append("Required (gating) checks: NONE declared — the repo readably configures "
                   "zero required checks (rulesets + branch protection already read; do NOT "
                   "re-probe gh). Ranking falls back to the slowest checks across sampled PRs.")
    else:
        out.append("Required (gating) checks: NONE readable — the data pass already queried "
                   "rulesets + branch protection without admin access (do NOT re-probe gh). "
                   "Ranking falls back to the slowest checks across sampled PRs.")
    out.append("")

    # PR-FLOOR fallback: no required check resolved to a workflow file (the required
    # suite is external/managed, or no sampled PR carried it). The spine below is the
    # measured PR-floor, NOT the branch-protection gate. State it once so the agent
    # reports the demotion instead of reverse-engineering it from a 0-sample.
    if cp.get("gate_kind") == "pr_floor_fallback":
        out.append("PR-FLOOR FALLBACK: no required check maps to a workflow file in this "
                   "repo — the required suite is external/managed (CLA bot, enterprise CI, "
                   "label-gated e2e, mergeability gate) or no sampled PR carried it. There "
                   "is NO file-backed required-gate critical path. The spine FALLS BACK to "
                   "the measured PR-floor: the file-backed workflows a normal PR actually "
                   "runs, ranked by long pole (already built below — do NOT re-probe gh or "
                   "hand-write it). Report it as the PR-floor, not the branch-protection "
                   "gate; the rendered report's banner already frames this.")
        out.append("")

    # Fileless / managed checks: no workflow file → not a directly-tunable lever. The
    # renderer demotes such a check from the headline ONLY when something else out-gates
    # it — i.e. it isn't `critical_path_check` (usually a file-backed pole takes the
    # headline, but in a multi-managed gate another managed check can). When the managed
    # check is itself `critical_path_check` (the unresolved/external-gate case — nothing
    # to promote in its place) the
    # renderer HEADLINES it as "the slowest check a typical PR waits on", so claiming
    # auto-demotion here would contradict the rendered report (RevenueCat/purchases-ios:
    # "Size Analysis | Emerge" headlines while only its OPT7x structural levers are hidden).
    # Flag the two cases distinctly so the summary's promise matches what the renderer does.
    # The exact-string compare below is correct (NOT fragile): `critical_path_check` and a pole's
    # `check` are the SAME raw data-layer value — both are `pr_checks_tuple[0][0]`, and in the
    # all-managed fallback case `critical_path_check` is assigned straight from `fb_poles[0]["check"]`
    # (collect_runs.py). Label normalization (`_clean_label`/scope-strip) is render-time only, so
    # there is deliberately none here — normalizing would risk merging two genuinely distinct checks.
    headline_check = str(cp.get("critical_path_check") or "")
    # Family framing OUTSIDE the claims layer — deliberate: this string goes to the agent-facing
    # stdout summary ONLY, never into the rendered report, so it needs no `Claim`. If it ever
    # starts feeding the report it must go through the claims layer (plan 007) — this comment is
    # the tripwire, and verify_report's coverage check would catch the rendered output.
    for p in (p for p in poles if not p.get("workflow_file")):
        # Issue #118: a check with no workflow_file but REAL developer job timing that MORE
        # THAN ONE workflow produces under the same name (matrix-leg collision — reth's
        # `test / ethereum`, produced by both unit.yml and integration.yml) is NOT a
        # fileless/external gate to ignore. It is a real CI job the tool can't attribute to
        # one file. The `ambiguous_workflows` stamp (set in collect_runs `_decompose_pole`
        # only when >1 workflow genuinely produces it) is the safe signal — a genuine
        # bot/app check never carries it, so its framing below stays byte-identical.
        if p.get("ambiguous_workflows"):
            _wfs = ", ".join(Path(str(w)).name for w in p.get("ambiguous_workflows"))
            out.append(f"⚠ `{p.get('check')}` is a REAL CI job (P50 {_clock(p.get('p50_s'))}) "
                       f"that more than one workflow ({_wfs}) produces under the same check "
                       f"name — ci-speedup can't attribute it to a single workflow file, so it "
                       f"isn't drilled below. This DOES warrant investigating: rename one job "
                       f"(or its matrix leg) so the check names differ, then re-run to get the "
                       f"per-file step drill.")
        elif headline_check and str(p.get("check") or "") == headline_check:
            out.append(f"⚠ Managed/fileless check HEADLINES the report (no workflow file → "
                       f"NOT a directly-tunable lever, but it IS the slowest check a typical "
                       f"PR waits on, so the renderer keeps it as the headline — there's no "
                       f"file-backed pole to promote in its place; only the structural "
                       f"lever(s) built on it are hidden): {p.get('check')} "
                       f"({_clock(p.get('p50_s'))}). The fix is structural (the work that "
                       f"feeds it), not editing a workflow line in this check.")
        else:
            out.append(f"⚠ Fileless/managed check (no workflow file → NOT a tunable lever; the "
                       f"renderer auto-demotes it from the headline): {p.get('check')} "
                       f"({_clock(p.get('p50_s'))}). Don't investigate its gating manually.")
        out.append("")

    # Addressable poles = the ones the report actually drills. Prefer the captured
    # drill bundle (already matrix-collapsed: one entry per drilled pole); fall back
    # to the file-backed critical-path poles collapsed by base check name, so matrix
    # siblings (tests-web (pg12) / (pg15) / …) read as ONE pole, not five.
    logs = (doc.get("data_bundle") or {}).get("logs") or []
    if logs:
        # The bundle entry's `duration_s` is the nearest-P50 *representative run's*
        # duration, not the percentile — so quote the pole's real `p50_s` (matched by
        # check name) and only fall back to the drilled-run duration if absent.
        p50_by_check = {str(p.get("check")): p.get("p50_s")
                        for p in poles if p.get("p50_s") is not None}
        drilled = [(e.get("check"), Path(str(e.get("workflow_file") or "")).name,
                    p50_by_check.get(str(e.get("check")), e.get("duration_s")))
                   for e in logs]
    else:
        drilled = _collapse_poles([p for p in poles if p.get("workflow_file")])
    out.append(f"Addressable long poles — the fixable critical path ({len(drilled)}):")
    for check, wf, p50 in drilled:
        out.append(f"  • {check}  [{wf or '—'}]  P50 {_clock(p50)}")
    out.append("")

    # Structural root causes deduped to distinct patterns (the catalog routes one per
    # matrix leg, so the raw list repeats); one representative title each.
    structural: dict[str, dict[str, Any]] = {}
    for f in (doc.get("findings") or []):
        if str(f.get("pattern")) in _STRUCTURAL:
            structural.setdefault(str(f.get("pattern")), f)
    if structural:
        out.append(f"Structural root-cause patterns ({len(structural)}):")
        for pat, f in structural.items():
            out.append(f"  • {pat} [{f.get('risk') or '?'} risk] {f.get('title')}")
        out.append("")

    out.append(_tier2_summary_line(doc))
    out.append("")

    # "Also noticed" hygiene — the renderer dedups + ranks these; the raw finding
    # count overstates the appendix, so don't present it as the appendix size.
    out.append("Off-path hygiene findings (runner-minute only) are deduped + ranked "
               "in the report's \"Also noticed\" appendix — they move ~0 merge wait.")

    # Sample provenance.
    spr, tgt = cp.get("sampled_pr_count"), cp.get("sample_target")
    if spr is not None:
        complete = "complete" if cp.get("sample_complete") else "SHORT"
        ff = cp.get("sample_fetch_failures") or 0
        gap = f", {ff} fetch failure(s)" if ff else ""
        # When the required suite was unsatisfiable, the sample is the recency-only
        # fallback (no PR carried the external gate) — say so, not just the count.
        scope = (" — recency-only (external required suite, no PR carried it)"
                 if cp.get("required_suite_unsatisfiable") else "")
        out.append(f"Sample: {spr}/{tgt} PRs ({complete}{gap}){scope}.")
    out.append("")

    # The render command — so the agent never reads blocking_path.py to invoke it.
    cmd = render_command(doc, findings_path=findings_path, out_path=out_path, data_dir=data_dir)
    if cmd:
        out.append("Render the report with (drill logs already captured — run verbatim):")
        out.append(cmd)
    else:
        out.append("No drill logs were captured; render with: "
                   f"python3 scripts/blocking_path.py --in {shlex.quote(findings_path)} "
                   f"--out {shlex.quote(out_path)}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Summarize a ci-speedup findings JSON.")
    ap.add_argument("findings", type=Path, help="Path to findings.json")
    ap.add_argument("--data-dir", default=None, help="Drill-log dir for the render command.")
    ap.add_argument("--out", default="REPORT.md", help="Report path for the render command.")
    args = ap.parse_args(argv)
    try:
        doc = json.loads(args.findings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read findings JSON ({e})", file=sys.stderr)
        return 1
    print(build_summary(doc, findings_path=str(args.findings),
                        data_dir=args.data_dir, out_path=args.out))
    return 0


if __name__ == "__main__":
    from log_level import configure_logging  # STARSLING_LOG_LEVEL opt-in
    configure_logging()
    sys.exit(main())

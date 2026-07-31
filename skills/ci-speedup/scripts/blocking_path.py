#!/usr/bin/env python3
"""Blank-slate, data-first answer to ONE question: why is the merge slow?

For each of the repo's heaviest workflows (which run in parallel on a PR) it
renders an ASCII drill-down waterfall - each level zooms into the slowest bar of
the level above, the connector hanging off that bar - then a PROMPT the user
hands to their own coding agent. ci-speedup does NOT prescribe the fix: it stops
at the measured root cause and points the agent at the tool's official docs.

    long pole  ->  its slowest step  ->  (with --log) the step's internals
    ->  the root cause  ->  a prompt (root cause + doc links) for your agent.

Level 1 (the concurrent checks/jobs) comes from the findings JSON (P50). Level 2
(one job's steps) is drawn as a SEQUENTIAL TIMELINE from one representative run's
real per-step start/end (`--steps`) - steps run one after another, unlike the
level-1 jobs which run at the same time. The deeper levels parse that same run's
raw log (`--log`), so level-2 and the drill stay on one coherent run. Without a
`--steps` timeline the step level falls back to P50 bars sorted by duration.

    python3 blocking_path.py --in FINDINGS.json \
        --log e2e=PRISMA_JOB.log --steps e2e=PRISMA_JOB.steps.json \
        --log ci=CI_TEST_JOB.log --steps ci=CI_TEST_JOB.steps.json [--out REPORT.md]
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import claims  # same-skill module; typed claims layer (increment 1: headline family)

_LBLW = 33
_BARW = 22
_DURW = 7
_PCTW = 4
# Core row width: label · bar · raw-duration · percent. The duration+percent tail
# (7 + 2 + 4 = 13) equals the gantt's "when" field width, so the step timeline and
# the drill levels line up to the SAME marker column - every connector wire aligns.
_CORELEN = 3 + _LBLW + 2 + _BARW + 2 + _DURW + 2 + _PCTW
_MARKCOL = _CORELEN + 2
_TOP_WORKFLOWS = 5  # render the long pole of this many distinct workflows (matches the
# data layer's _STRUCTURAL_TOP_N so every analysed pole renders; the top
# _DRILL_DISTINCT_MATRICES get a raw-log drill, poles below that render shallow from their
# sampled per-step decomposition — see render-depth-2to5 spec / R1–R3 fixes)
_ALSO_NOTICED_CAP = 12  # max residual hygiene patterns shown in the "Also noticed" appendix
_TIER2_CAP = 12  # max promoted runner-minute findings shown before the residual appendix
_RUNNER_MINUTE_SPINE_CAP = 12  # max cost-spine rows rendered before a hidden-row disclosure
# One-line workflow/job/step hierarchy glossary, rendered ONCE under the Long pole
# map heading (issue #96). Presentation-only; ASCII punctuation (no typographic dashes).
_HIERARCHY_GLOSSARY = (
    "A **workflow** is one YAML file under `.github/workflows/`; a run of it executes "
    "its **jobs** in parallel (each on its own runner); each job runs its **steps** in "
    "sequence.")
# Below this many PRs with presence data, the presence fraction is too noisy to demote a
# pole on — keep p50 order. MUST match collect_runs `_RARE_PRESENCE_MIN_PR`, so the renderer
# and the data layer (which sets `critical_path_check` / the summary) never disagree about
# whether a pole is rare on a small sample.
_RARE_PRESENCE_MIN_PR = 6
# A check present on AT MOST this fraction of sampled PRs is rare/opt-in. Legacy signal, kept
# for the backward-compat fallback (findings that predate `pole_n`). MUST match collect_runs
# `_RARE_PRESENCE_FRAC` on that path so the rendered demotion agrees with `critical_path_check`.
_RARE_PRESENCE_FRAC = 0.5
# The recurrence floor on ACTUAL-CRITICAL-PATH frequency (`checks[].pole_n`): a check that is the
# slowest job a PR waits on, on at least this many sampled PRs, is a genuine recurring gate and
# stays TYPICAL; below it, a one-path outlier, demoted. MUST match collect_runs `_POLE_RECUR_FLOOR`
# so the rendered typical/rare split agrees with `critical_path_check` (a present>half cutoff
# wrongly demoted every heavy suite on a path-partitioned monorepo — see collect_runs).
_POLE_RECUR_FLOOR = 2
# A magnitude bracket wider than this (range / median) reads as "wide" - the fix's
# payoff varies run to run. Mirrors collect_runs' escalation threshold so the
# rendered verdict agrees with the sampling decision (kept independent to avoid a
# cross-module import; cosmetic if they ever drift).
_MAG_WIDE_REL = 0.25
# Issue #115: when the measured per-PR wall (makespan p50) materially exceeds the chain-sum
# wait, the chain sum UNDERSTATES what a PR waits — queue gaps between serial `needs:` stages
# stretch the real wall past the sum. Beyond this |divergence| (mirrors the Model-check
# emission threshold, `abs(divergence_pct) > 25`), the "typical PR waits / until all checks
# finish" WALL figure leads with the observed makespan and the chain sum is demoted to its
# attribution role. `divergence_pct = (chain_p50 - makespan_p50) / makespan_p50 * 100`, so the
# wall-understated arm is the NEGATIVE one (`< -25`); the positive arm (sum > wall, re-run
# inflation) is already clamped by `_chain_total`, so the headline never overstates there.
_CHAIN_MAKESPAN_DIVERGENCE_PCT = 25.0

# The report must use a plain ASCII hyphen, never a typographic dash (CI tools and
# some terminals choke on non-ASCII). Em/en/figure/bar dashes leak in from hardcoded
# prose, the runner-min minus glyph, and catalog TL;DRs, so - like report.py - we
# strip them ONCE at the render boundary rather than chase every source. Box-drawing
# glyphs (─ │ ┘ █ ░) are NOT in this set, so the ASCII waterfalls survive. Verified
# by verify_report's no-typographic-dashes check.
_DASH_GLYPHS = ("—", "–", "‒", "―", "−")  # em, en, figure, horizontal bar, minus

# Catalog hygiene (OPT1–69) demotes to the "Also noticed" appendix; the structural
# track (OPT70–75) is already rendered AS the poles above, so it's excluded there.
_SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "MANUAL": 3}
_STRUCTURAL_PATTERNS = frozenset(
    {"OPT70", "OPT71", "OPT72", "OPT73", "OPT74", "OPT75"})
# Pre-start WALL-CLOCK wait (queue time) — the developer waits before any work runs,
# which the critical-path spine (job start → finish) doesn't capture. These are NOT
# runner-minute hygiene, so they get their own "Pre-start wait" section instead of the
# bill-savings appendix (which would wrongly tell the reader they remove ~0 wall-clock).
_WAIT_PATTERNS = frozenset({"OPT43"})


def _is_tier2_finding(f: dict[str, Any]) -> bool:
    """A measured+certified runner-minute candidate for the Tier-2 section.

    This is the single source of truth for measured+certified candidate admission.
    Visible section rows, Contents count, bottom-line totals, and appendix exclusion
    use the stricter source-backed set, so a candidate without matching cost-spine
    rows is kept out of R-numbered savings output but still falls through to the
    residual appendix. Modeled below-floor findings and measured-but-uncertified
    findings also stay in the appendix; widening this predicate would be a
    product/faithfulness change, not a renderer tweak."""
    return (not f.get("advisory")
            and f.get("sizing_basis") == "measured"
            and isinstance(f.get("tier2_neutrality"), dict)
            and bool(f.get("tier2_neutrality"))
            and (_num(f.get("runner_min_saving")) or 0.0) > 0)


def _is_tier2_superseded(f: dict[str, Any]) -> bool:
    return bool(str(f.get("tier2_superseded_by") or "").strip())


def _is_wait_finding(f: dict[str, Any]) -> bool:
    """A non-advisory pre-start-wait (queue) finding WITH addressable wait — what
    `_queue_wait_block` renders AND what `render()` counts for the Contents pointer. SINGLE
    source of truth so the two can't diverge: if they did, the `#pre-start-wait` TOC link
    could point at a section that didn't render (a dead anchor). A queue finding whose
    floor-capped savable wait (`wall_clock_p50_s`) is 0 has nothing for the developer to act
    on here (e.g. a Slack-notify / deploy-staging / scheduled job that no one merge-waits on,
    or a job whose queue overlaps the gate so it adds no wall-clock) — excluding it stops the
    section over-counting (after the floor cascade most emitted OPT43s have zero savable wait;
    only the few with a positive cap belong here). Such a finding doesn't leak into the
    hygiene appendix either — `_also_noticed_block` excludes every `_WAIT_PATTERNS` finding
    unconditionally (not by saving) — and `_queue_wait_block` discloses the dropped count."""
    return (str(f.get("pattern", "")) in _WAIT_PATTERNS and not f.get("advisory")
            and (_num(f.get("wall_clock_p50_s")) or 0) > 0)


def _is_pole_structural(f: dict[str, Any]) -> bool:
    """A structural PER-POLE lever that `_also_noticed_block` must exclude because it
    is already rendered AS one of the long-pole drill-downs above (OPT70/71/72/74/75).
    These carry NO runner-minute axis, so re-listing them in the bill-ranked appendix
    would add an empty row. The ONE structural pattern that is NOT a per-pole
    annotation is the cross-cluster shared-substep floor lever (OPT73): it spans the
    whole cluster, so no single pole represents it, and it is the only structural
    pattern that carries a credited runner-minute saving. Any CREDITED OPT73
    (`runner_min_saving > 0`) is therefore owned by the 'Also noticed' bill appendix —
    where the savings methodology shows the runner-minute axis — and returns False (keep
    it). Wall-clock is deliberately NOT consulted: an on-spine OPT73 (`wall_clock > 0`)
    is still the cross-cluster lever and must stay appendix-owned, so it returns False
    too (the typical bill-only outcome, with wall-clock floored to 0, is just the common
    case — not the gate). A non-credited OPT73 and every other structural pattern
    (OPT70/71/72/74/75) returns True (exclude it; it's rendered AS the pole)."""
    is_struct = (bool(f.get("structural"))
                 or str(f.get("pattern", "")) in _STRUCTURAL_PATTERNS)
    if not is_struct:
        return False
    # Anchor on the PATTERN, not just the bill-only wall-clock heuristic. OPT73 is the
    # cross-cluster shared-substep lever — it spans the whole cluster (no single pole
    # represents it) and is the only structural pattern carrying a runner-minute saving — so
    # a CREDITED OPT73 is owned by the bill appendix whether or not its wall-clock floored to
    # 0. (The old `runner_min_saving>0 AND wall_clock<=0` test mis-classified an on-spine
    # OPT73 (wall_clock>0) as a per-pole annotation, which would render it at a name-matching
    # pole or — with no matching pole — nowhere at all.) Every other structural pattern
    # (OPT70/71/72/74/75) IS a per-pole annotation, already rendered as its own drill-down.
    # #49 — credit on EITHER axis: the render-layer headline consumes wall_clock>0 and points
    # the reader at the Also-noticed OPT73 entry, so a lever credited by wall-clock alone
    # (runner_min_saving None when the workflow's monthly volume couldn't be fetched) must
    # also be appendix-owned — otherwise the headline anchors at a section that doesn't hold
    # it. For the common credited lever (runner_min_saving>0) the `or` short-circuits, so
    # this is byte-identical to the prior behavior.
    if str(f.get("pattern", "")) == "OPT73":
        return not ((_num(f.get("runner_min_saving")) or 0.0) > 0
                    or (_num(f.get("wall_clock_p50_s")) or 0.0) > 0)
    return True


# Catalog link target (the report points at the public catalog, pinned to the skill
# commit so anchors are content-addressable). Mirrors report.py's constants.
_CATALOG_REPO = "starslingdev/skills"
_CATALOG_PATH = "skills/ci-speedup/references/optimization-patterns.md"
_DEFAULT_CATALOG_BRANCH = "main"


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_dict(v: Any) -> dict:
    """`{}` for any non-dict — a defensive coercion so a malformed / absent nested field
    (e.g. a pole's `cache_dist`, or its `pr`/`tail` sub-objects) reads as empty rather than
    raising when the renderer navigates it."""
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _clock(seconds: float | None) -> str:
    s = int(round(seconds or 0))
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _count_noun(n: Any, noun: str) -> str:
    """`1 workflow` / `2 workflows` — pluralize a regular count-noun so provenance
    never prints `across 1 workflows`. A non-int count (e.g. None on a degraded/partial
    doc) compares unequal to 1, so it receives the plural suffix (`None workflows`)."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _mmss(seconds: float | None) -> str:
    """m:ss clock for the timeline axis (0:09, 7:44, 12:38)."""
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def _pct_disp(x: float) -> str:
    """A miss-rate percent for display: whole number when it IS whole, else ONE decimal.
    A median over an even sample lands on a half (e.g. 39.5%); rounding that to a whole
    percent (40%) can cross the churn floor and read as disagreeing with a `mostly-warm`
    verdict re-derived from the true 39.5. Mirrors `_mag_line`'s local `fmtm` so every
    cache-health percent renders on the same rule (kept coupled by a self-test)."""
    return f"{x:.0f}%" if abs(x - round(x)) < 0.05 else f"{x:.1f}%"


def _clean_label(s: str) -> str:
    # Scope-strip THEN `_fence_safe` THEN map EVERY remaining backtick to an apostrophe:
    # `_clean_label` is the canonical normalizer every repo-controlled NAME (check/job/step,
    # in headings, ```text waterfalls, inline code spans, and agent-prompt fences) flows
    # through, so sanitizing here neutralizes the whole class at ONE chokepoint (it can't
    # drift to a missed sink). NAMES drop all backticks (a 1-2-backtick name would otherwise
    # break inline `spans` and desync the heading comparators — #108 review); verbatim
    # EVIDENCE keeps single backticks for fidelity (`_fence_safe` alone defuses only >=3
    # runs). No-op on clean names -> every downstream display AND comparison key is
    # byte-identical for real repos; `verify_report._strip_scope` is a verbatim twin
    # (pinned by test_s1a) and carries the identical return, so the comparators stay aligned.
    return _fence_safe(re.sub(r"^@[^ /]+/", "", s)).replace("`", "'")


# Repo-controlled free text — GitHub check/job/step names and verbatim captured
# job-log / workflow-YAML "evidence" lines — is dropped into the rendered Markdown,
# both inside ```text fences and inside inline code spans / headings. Left raw, a run
# of >=3 backticks CLOSES a fence early: the rest of the report renders as broken
# Markdown on GitHub AND `verify_report` desyncs (it splits the SAME text with
# `re.findall(r"```text\n(.*?)```")`, so the identical stray fence fools the safety
# net). Embedded newlines/CRs and control chars break heading lines, table columns,
# and the fixed-width ASCII bars. Neutralize all of it AT the sinks — the same
# discipline the git-provenance sha already applies to the one field the authors knew
# could break out of its code span (see `_git_provenance`'s `[0-9a-f]{7,40}` guard).
_FENCE_RUN_RE = re.compile(r"`{3,}")
# C0/C1 control chars EXCEPT tab (\x09), LF (\x0a) and CR (\x0d): tab is legitimate
# leading indentation in a verbatim log line and is harmless inside a fence; the
# newlines are handled separately (collapsed, not dropped). NUL/BEL/ESC/backspace/etc.
# never belong in a one-line name or evidence line.
_FENCE_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _defuse_backtick_runs(s: str) -> str:
    """Replace every run of >=3 backticks — the fenced-code-block delimiter, and the only
    backtick run that can terminate a ```text fence — with an equal-length run of ASCII
    apostrophes (visually near-identical, provably unable to close a fence). Runs of 1-2
    backticks are left intact: inside a fence they render literally and cannot close it."""
    return _FENCE_RUN_RE.sub(lambda m: "'" * len(m.group(0)), s)


# Credential masking (#12, skills.sh Snyk W007). The quoted evidence is verbatim
# third-party job-log / workflow-YAML text, and the report is the artifact users commit
# and share. GitHub masks the secrets it KNOWS about (registered ones) in its own log
# output — that stays the first layer — but an accidentally-echoed unregistered token
# reaches the captured log in the clear. So mask credential-SHAPED strings here too,
# deterministically, at the same sink that defuses backtick runs.
#
# SHAPED patterns only, deliberately: no entropy heuristic. A report whose step names,
# durations, run URLs or 40-hex provenance shas came back `[REDACTED:...]` would be
# useless, and a bare sha is not a credential. Each entry masks its WHOLE match.
_SECRET_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"
                                r"|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("docker-token", re.compile(r"\bdckr_pat_[A-Za-z0-9_-]{20,}")),
    # OpenAI / Anthropic style. Anchored on the `sk-` prefix AND a >=20-char alphanumeric
    # tail, so a hyphenated step name can't reach it.
    ("llm-api-key", re.compile(r"\bsk-(?:proj-|ant-[a-z0-9]+-|live-|test-)?[A-Za-z0-9]{20,}")),
)
# The un-shaped catch-all: `<key> = <value>` / `<key>: <value>`. Only the VALUE is masked
# (the key name is half the diagnostic value of the line). The key may carry the usual
# env-var prefix (`NPM_TOKEN=`, `GH_API_KEY:`) — the plain `\btoken\b` form would miss
# every real-world log line, since `_` is a word char. An HTTP auth SCHEME word may sit
# between the separator and the value (`Authorization: Bearer <opaque>` is the single most
# common real form, and the value — not the scheme — is the credential). The value must be
# >=8 chars AND (a digit anywhere OR >=16 chars), so ordinary prose (`token: yes`,
# `authorization: required`) and timing lines are left alone. The lookahead makes the mask
# idempotent.
_ASSIGN_SECRET_RE = re.compile(
    r"(?i)\b(?:[A-Za-z0-9]+[._-])*"
    r"(?:token|secret|password|passwd|api[_-]?key|authorization|bearer)\b\s*[=:]\s*"
    r"(?:(?:bearer|basic|token)\s+)?"
    r"(?!\[REDACTED:)(\S{8,})")
# A value that is a variable REFERENCE, not a value: `${{ secrets.X }}` (the actions
# expression, with or without inner spaces), `${VAR}`, `$VAR`, `%VAR%`. These are exactly
# what a correctly-written workflow YAML line looks like, and they are the lines the
# catalog detectors quote as evidence — masking them would destroy the diagnostic and
# falsely suggest the repo hardcodes a token.
_ASSIGN_VAR_REF_RE = re.compile(r"^(?:\$\{\{?[^}]*\}?\}|\$[A-Za-z_][A-Za-z0-9_]*|%[^%]+%)")


def _redact_secrets(s: str) -> str:
    """Mask credential-shaped substrings in one line of untrusted third-party text,
    replacing each with `[REDACTED:<kind>]` and KEEPING the surrounding words so the
    evidence stays interpretable. Idempotent, and a no-op on text with no credential
    shape in it (the overwhelmingly common case)."""
    for kind, pat in _SECRET_PATTERNS:
        s = pat.sub(f"[REDACTED:{kind}]", s)

    def _assign(m: "re.Match[str]") -> str:
        val = m.group(1)
        if not (len(val) >= 16 or any(c.isdigit() for c in val)):
            return m.group(0)          # `password: changeme` — prose, not a credential
        if _ASSIGN_VAR_REF_RE.match(val):
            return m.group(0)          # `TOKEN: ${{secrets.X}}` — a reference, not a value
        return m.group(0)[: m.start(1) - m.start(0)] + "[REDACTED:credential]"

    return _ASSIGN_SECRET_RE.sub(_assign, s)


def _fence_safe(s: object) -> str:
    """Make one line of repo-controlled free text safe to drop into a ```text fence (a
    waterfall label, a verbatim log/YAML evidence line): defuse >=3-backtick runs, collapse
    any embedded newline/CR run to a single space (a name/step/log line is ONE line — an
    embedded newline could otherwise become its own all-backtick line and close the fence),
    strip dangerous control chars (tab kept), and mask credential-shaped strings (#12).
    BYTE-IDENTICAL for clean single-line input (no >=3-backtick run, no newline, no control
    char, no credential shape) — every normal name/label/evidence line passes through
    unchanged. This is the ONE chokepoint every verbatim log/YAML line, every repo-controlled
    name and every agent-prompt line already flows through (directly, or via `_clean_label` /
    `_safe_span` / `_fence_body`), so the mask covers the whole class by construction instead
    of site by site."""
    s = _FENCE_CTRL_RE.sub("", str(s))
    s = re.sub(r"[\r\n]+", " ", s)
    return _redact_secrets(_defuse_backtick_runs(s))


def _safe_span(s: object) -> str:
    """An inline code span repo text cannot break out of: fence-safe the content, then map
    EVERY remaining backtick to an apostrophe so none survives to close the single-backtick
    delimiter, and wrap. Mirrors `_wf_base`'s `\\`...\\`` style; clean input -> `\\`text\\``."""
    return "`" + _fence_safe(s).replace("`", "'") + "`"


def _fence_body(lines: list[str]) -> str:
    """Join CONTENT lines of a ```text prompt fence with each line fence-safed — so any repo
    name/evidence line assembled into an agent prompt (`check`, the dominant step, verbatim
    log lines) can't emit a stray ``` that closes the fence and desyncs the verifier. Applied
    PER-LINE (never on the joined body, whose own line breaks must survive) and NEVER to the
    ``` delimiters themselves (the callers keep those outside this join). No-op on clean lines."""
    return "\n".join(_fence_safe(l) for l in lines)


def _lbl(s: str) -> str:
    # `_clean_label` already fence-safes (so no repo name can close the ```text fence the
    # waterfall labels live in, or break an inline `\\`label\\`` span); `_lbl` only truncates.
    s = _clean_label(s)
    return s if len(s) <= _LBLW else s[: _LBLW - 1] + "…"


def _matrix_base_raw(name: str) -> str | None:
    """The RAW trailing-paren reduction — the job name BEFORE its TRAILING
    `(matrix params)`, with NO display normalization (no `@scope/` strip, no
    lowercasing). None when the name has no trailing parenthesised params.

    GitHub names a matrix leg `<job> (<param values>)` — the parenthetical is
    always the LAST thing in the name — so this is trailing-anchored (not "up to
    the first paren"): a name with its own embedded, non-trailing parenthetical
    (e.g. `build (x) fast`) is NOT a matrix leg and returns None, while
    `test (a) (b)` peels only the outer trailing group -> `test (a)`.

    This is the CANONICAL reduction shared by both the renderer's display parser
    (`_matrix_base`, which layers scope-strip + lowercase on top) and the
    engine's key builder (`collect_runs._matrix_base_name`, which needs the raw,
    scope- and case-PRESERVING form so required-check matching keys stay
    distinct: `@a/pkg build (18)` and `@b/pkg build (18)` must not collide, and
    `Build` must not fold into `build`)."""
    # The trailing group tolerates ONE level of nested parens (a matrix param VALUE
    # can itself contain parens, e.g. `test (18 (LTS), ubuntu)` -> `test`); deeper
    # nesting is pathological and treated as not-a-leg.
    m = re.match(r"^(.*\S)\s*\((?:[^()]|\([^()]*\))*\)\s*$", name)
    return m.group(1).strip() if m and m.group(1).strip() else None


def _matrix_base(name: str) -> str | None:
    """DISPLAY matrix base: the trailing-paren reduction (`_matrix_base_raw`)
    with display normalization layered on — `@scope/` stripped and lowercased —
    so matrix legs group robustly for the rendered blocking-path even when the
    param VALUES tokenise to different lengths (`tests-web (… mode)` vs
    `tests-web (… mode-redis-cluster)`), which the prefix/suffix token rule below
    can't. Returns None when the name has no trailing parenthesised params.

    NOTE: this is DISPLAY-only. The engine's required-check key builder must NOT
    scope-strip or lowercase — it uses the raw `_matrix_base_raw` directly."""
    base = _matrix_base_raw(_clean_label(name))
    return base.lower() if base is not None else None


def _same_matrix(a: str, b: str,
                 wf_a: str | None = None, wf_b: str | None = None) -> bool:
    """Are these two check names legs of the same matrix? Two cases:
    (1) BOTH are `<base> (params)` - same matrix iff the base matches (robust to
        multi-token param values; a no-params job like `test-docker-build` is NEVER
        folded into a parenthesised matrix).
    (2) otherwise, matrix legs share a long common token PREFIX or SUFFIX (e.g.
        `prisma-adapter Integration Test` / `drizzle-adapter Integration Test`),
        differing in at most one token. The length guard stops a SHORTER distinct
        check sharing a generic tail (`Integration test`) from being mistaken for a
        leg. Erring toward false-negatives is the safe direction.

    A matrix lives in ONE workflow file, so two checks from DIFFERENT workflow files
    are never legs of one matrix — even when their names are identical or share a
    base/prefix. GitHub gives same-named jobs in different workflows IDENTICAL
    check-run names (a `name: Python ${{ matrix.python }}` job in datasets-test.yml
    and another in framework-test.yml both surface as `Python 3.13`), so name
    similarity ALONE over-folds them: the report would draw two workflows' jobs as one
    matrix and claim "the legs share one job config". When BOTH `wf_a`/`wf_b` are known
    and resolve to different files, they are not a shared matrix. An unknown side
    (`None`/"") falls back to name-only matching, so every existing call site that
    passes no workflow keeps its prior behavior."""
    if wf_a and wf_b and _wf_base(wf_a) != _wf_base(wf_b):
        return False
    ta = [t for t in re.split(r"[^a-z0-9]+", _clean_label(a).lower()) if t]
    tb = [t for t in re.split(r"[^a-z0-9]+", _clean_label(b).lower()) if t]
    if not ta or not tb or ta == tb:
        return ta == tb and bool(ta)
    ba, bb = _matrix_base(a), _matrix_base(b)
    if ba is not None and bb is not None:
        return ba == bb
    if abs(len(ta) - len(tb)) > 1:
        return False
    # Same length differing in exactly ONE token (>=3 tokens, so >=2 are shared) is a
    # matrix on a single mid-string dimension - catches `test (22.x)` / `test (24.x)`
    # (shared `test` + `x` but only 1 token each, so the prefix/suffix rule misses it).
    if len(ta) == len(tb) >= 3 and sum(1 for x, y in zip(ta, tb) if x != y) == 1:
        return True
    pre = suf = 0
    for x, y in zip(ta, tb):
        if x == y:
            pre += 1
        else:
            break
    for x, y in zip(reversed(ta), reversed(tb)):
        if x == y:
            suf += 1
        else:
            break
    return pre >= 2 or suf >= 2


# A concurrent sibling counts as a near-universal floor when it ran on at least this fraction
# of the most-present sibling's PRs. Below it the check skips too many PRs to floor the typical
# merge: cutting the pole still drops merge wait on the PRs the minority sibling never gates.
# This GLOBAL-presence test is the LEGACY fallback, used only when no per-PR `populations` are
# available to re-derive co-occurrence (a minimal/static doc, the poles-fallback `src`).
_FLOOR_NEAR_UNIVERSAL_FRAC = 0.8

# When per-PR `populations` ARE available, the floor is re-derived RELATIVE to the pole's OWN
# gating PRs (the PRs where it is the slowest check): a concurrent check floors the merge iff it
# co-occurs with the pole on a strict MAJORITY of those gating PRs. The global test demoted a
# genuinely co-occurring 2nd-slowest sibling that merely ran on slightly fewer PRs than some
# trivial universal check (Infisical: `Run BDD tests` on 13/13 of the pole's gating PRs but
# globally on 15/20 < 0.8×20, so a faster `Lint` was wrongly named the floor and the addressable
# ceiling overstated ~2×). `tests/verify_report.py::check_pole_ceiling_within_cooccurrence` is the
# class invariant that catches the overstatement on every future report; this is its engine fix.
_FLOOR_COOCCUR_MAJORITY_FRAC = 0.5


def _check_name(c: dict[str, Any]) -> str:
    return str(c.get("name") or c.get("check") or "")


def _check_p50(c: dict[str, Any]) -> float:
    return _num(c.get("p50_s")) or 0.0


def _eff_floor_s(c: dict[str, Any]) -> float:
    """A concurrent check's EFFECTIVE wall-clock floor contribution. A check flagged BIMODAL
    HIGH floors the merge at its SLOW mode on the slow-mode PRs even when its blended median is
    lower, so the floor - and the addressable ceiling measured above it - must use that high
    mode, not the blended p50 that under-states it (and so over-states the addressable win)."""
    bi = c.get("bimodal")
    hi = _num(bi.get("high_p50_s")) if isinstance(bi, dict) else None
    return max(_check_p50(c), hi or 0.0)


def _floor_key(s: Any) -> str:
    """The name key co-occurrence is matched on: scope-stripped (`_clean_label`) + lowercased,
    so a populations check name reconciles with a candidate's `name`/`check` regardless of an
    `@scope/` monorepo prefix or case. Shares the same normalization basis (scope-strip + case-fold)
    as the verifier's `_cmp_name`; `_cmp_name` additionally strips `` ` ``/`*` markdown, which is
    irrelevant here because these keys come from data (`populations`/`checks`), not rendered text."""
    return _clean_label(str(s or "")).strip().lower()


def _pole_cooccurrence(cp: dict[str, Any], pole_name: str) -> tuple[dict[str, int], int]:
    """For `pole_name`, how many of its GATING PRs (the PRs where it is the slowest check) each
    concurrent check also ran on, re-derived from the per-PR `populations` ground truth. Returns
    (`{floor_key: cooccur_count}`, n_gating). ({}, 0) when populations are absent or the pole is
    never a per-PR winner — callers then fall back to the legacy GLOBAL-presence floor test. This
    is the data the floor selection uses to count a sibling 'near-universal' relative to the pole
    itself, not relative to the busiest check in the whole repo."""
    target = _floor_key(pole_name)
    cooccur: dict[str, int] = {}
    n_gating = 0
    for entry in cp.get("populations") or []:
        try:
            _share, cks = entry
            pos = [(str(nm), float(p)) for nm, p in cks if float(p) > 0]
        except (TypeError, ValueError):
            continue
        if not pos:
            continue
        winner = max(pos, key=lambda kv: kv[1])[0]
        if _floor_key(winner) != target:
            continue
        n_gating += 1
        for k in {_floor_key(nm) for nm, _p in pos}:
            cooccur[k] = cooccur.get(k, 0) + 1
    return cooccur, n_gating


def _floor_qualifies(c: dict[str, Any], cooccur: dict[str, int] | None, gating_n: int,
                     denom: int) -> bool:
    """Is concurrent check `c` 'near-universal' enough to FLOOR the merge? With per-pole
    co-occurrence (`cooccur`/`gating_n` from `populations`) the test is RELATIVE to the pole's own
    gating PRs: c co-occurs with the pole on a strict MAJORITY of them. Without it (no populations
    — a minimal/static doc or the poles-fallback `src`) it degrades to the LEGACY global-presence
    test (`present_on >= 0.8 × the most-present sibling`), so degraded inputs keep their prior
    behavior unchanged."""
    if cooccur and gating_n:
        return cooccur.get(_floor_key(_check_name(c)), 0) > gating_n * _FLOOR_COOCCUR_MAJORITY_FRAC
    if not denom or c.get("present_on") is None:
        return True
    return int(c.get("present_on") or 0) >= denom * _FLOOR_NEAR_UNIVERSAL_FRAC


def _binding_floor(candidates: list[dict[str, Any]], cooccur: dict[str, int] | None = None,
                   gating_n: int = 0) -> dict[str, Any] | None:
    """The near-every-PR concurrent sibling — INCLUDING managed/external checks (no
    `workflow_file`) — with the highest EFFECTIVE blocking time (bimodal slow mode where
    flagged). This is the check that actually CAPS the addressable win and trips the non-gate
    guard: a universal managed app review slower than the pole genuinely occupies the merge
    wall-clock even though `_floor_candidate` (file-backed) is what the reader is told to act
    toward. 'Near-every-PR' is `_floor_qualifies` — per-pole co-occurrence when populations are
    available, else the legacy global-presence test. None when there's nothing concurrent."""
    if not candidates:
        return None
    denom = max((int(c.get("present_on") or 0) for c in candidates), default=0)
    universal = [c for c in candidates if _floor_qualifies(c, cooccur, gating_n, denom)]
    return max(universal or candidates, key=_eff_floor_s)


def _binding_floor_s(candidates: list[dict[str, Any]], cooccur: dict[str, int] | None = None,
                     gating_n: int = 0) -> float:
    """The effective seconds of `_binding_floor` (0 when nothing concurrent)."""
    b = _binding_floor(candidates, cooccur, gating_n)
    return _eff_floor_s(b) if b else 0.0


def _floor_candidate(candidates: list[dict[str, Any]], cooccur: dict[str, int] | None = None,
                     gating_n: int = 0) -> dict[str, Any] | None:
    """The concurrent sibling that sets the wall-clock FLOOR: the highest EFFECTIVE blocking
    time (bimodal slow mode where flagged) among checks that ACTUALLY floor a typical PR -
    file-backed (a managed/external check the report suppresses from the long-pole list can't be
    named as the floor the reader is told to act down to) AND near-universal by `_floor_qualifies`
    (per-pole co-occurrence on a majority of the pole's gating PRs when populations exist, else the
    legacy global-presence test — a minority sibling doesn't gate the PRs it skips). Falls back to
    the raw set when the doc carries no presence/file/co-occurrence metadata (a minimal/static doc,
    or the poles fallback), so degraded inputs keep their prior floor note. None when there's
    nothing concurrent to floor against."""
    if not candidates:
        return None
    denom = max((int(c.get("present_on") or 0) for c in candidates), default=0)

    def _valid(c: dict[str, Any]) -> bool:
        if not str(c.get("workflow_file") or ""):   # managed/external: not a pole, not a floor
            return False
        return _floor_qualifies(c, cooccur, gating_n, denom)

    valid = [c for c in candidates if _valid(c)]
    if valid:
        return max(valid, key=_eff_floor_s)
    # Nothing file-backed + near-universal to NAME. Only fall back to the raw set when the doc
    # carries NO file/presence metadata at all (a minimal/static doc, or a bundle whose `checks`
    # omit `workflow_file`) — a degraded input keeps its prior floor note. When metadata EXISTS but
    # excluded everything (every sibling is managed or minority-presence), return None: there is no
    # tunable floor to name, and re-admitting the very check `_valid` rejected would contradict the
    # guarantee. Co-occurrence is NOT file metadata (it can't supply a `workflow_file` to name), so
    # it must not gate this fallback — else a populations-rich bundle whose checks lack
    # `workflow_file` (e.g. better-auth) loses its floor note and zeroes the addressable win.
    has_meta = any(c.get("workflow_file") or c.get("present_on") is not None for c in candidates)
    if has_meta:
        return None
    return max(candidates, key=_eff_floor_s)


def _floor_note(pole: dict[str, Any],
                candidates: list[dict[str, Any]]) -> list[str]:
    """How much WALL-CLOCK a fix on this pole actually buys. Cutting the gating job
    helps 1:1 only down to the next concurrent check (the floor); below that the
    gate moves and further savings are runner-minutes, not wall-clock. For a matrix
    pole the next concurrent check is usually a SIBLING LEG, so cutting one leg
    alone buys almost nothing - but a shared-config fix speeds every leg at once and
    drops the whole matrix toward the next NON-leg check. [] when there's nothing
    concurrent to floor against.

    A modal-chain MEMBER pole (render() stashes `_chain_member` — review V2 /
    OD-F2) renders the chain-stage story instead: `needs:` serializes it with its
    chain partners, so the concurrent arithmetic below would frame a serial
    predecessor as a "next leg" and overstate the win (the committed deepgram
    artifact rendered ~28s where the stamped chain headroom is 5.0s). The member's
    win is capped at the stamped chain headroom and its own span — the same bound
    `wall_clock.bound_measured_critical_path` applies to member findings. The
    suppression guards above the branch are unchanged: a pole whose note is
    suppressed today stays suppressed (cell 7), and non-members render
    byte-identically (cells 1/4)."""
    if pole.get("job_timing_unavailable"):
        return []
    pole_name = _clean_label(str(pole.get("check", "")))
    pole_p = _num(pole.get("p50_s")) or 0.0
    # Issue #66 fix 2: the typical merge wait (stashed by render()). A recoverable ceiling
    # rendered below that exceeds it co-renders the slow-mode/minority reconciliation.
    _tw = _num(pole.get("_typical_wait_s")) or 0.0
    # Per-pole co-occurrence (stashed in render() from `populations`): a sibling floors the merge
    # iff it co-occurs with THIS pole on a majority of the pole's own gating PRs, not by global
    # presence. Absent (no populations) → the floor helpers fall back to the legacy global test.
    cooccur, gating_n = pole.get("_cooccur"), int(pole.get("_gating_n") or 0)
    others = [c for c in candidates if _clean_label(_check_name(c)) != pole_name]
    floor_next = _floor_candidate(others, cooccur, gating_n)
    binding = _binding_floor(others, cooccur, gating_n)
    binding_s = _eff_floor_s(binding) if binding else 0.0
    # Non-gate guard + addressable cap. A pole that runs BEHIND a UNIVERSAL concurrent blocker —
    # INCLUDING an untunable managed/external check that `_floor_candidate` won't NAME — buys ~0
    # merge wait until that blocker is cut, which the pole's role line already states. Skip the
    # note rather than over-state a win a slower universal check caps. `_binding_floor` uses the
    # bimodal slow mode where flagged (a sibling whose median is low still floors at its high mode
    # on its slow PRs) and counts managed checks; the addressable win is `pole − binding`, never
    # `pole − the file-backed name` (else a universal managed check between them inflates it).
    # floor_next is None when there's no FILE-BACKED floor to NAME → no coherent note.
    if pole_p <= 0 or binding_s >= pole_p or floor_next is None:
        return []
    ch = pole.get("_chain_member")
    if isinstance(ch, dict):
        win_s = _num(ch.get("win_s")) or 0.0
        win = max(min(win_s, pole_p), 0.0)
        stage, chain_len = int(ch.get("stage") or 0), int(ch.get("len") or 0)
        label = str(ch.get("label") or "gate")
        # Disclose the binding CONCURRENT floor (Class A #6). The chain-headroom story below frames
        # only the SERIAL win (how much shortening this `needs:` stage buys); it says NOTHING about a
        # heavy check running ALONGSIDE the chain. When such a check co-occurs with this pole on a
        # majority of its gating PRs and — by its EFFECTIVE (bimodal slow-mode) time — is the slowest
        # of them, it still caps the merge wall-clock and must be NAMED, else it is a silent spine
        # drop the report's own footnote promise forbids (better-auth `test (22.x)`: ~80s median but
        # ~4m on the crown-gated PRs, co-occurring on 18/18 — invisible to the typical-PR chart and
        # the "Also slower" footnote, both of which rank by the fast-mode median). Re-derive it the
        # SAME way the verifier does: `_floor_candidate` over the concurrent set with the per-pole
        # co-occurrence test, EXCLUDING serial chain partners (they run before/after, not alongside)
        # so a partner is never mis-framed as a parallel cap. The "slowest concurrent check is `X`"
        # phrasing is the form `verify_report._SPINE_FLOOR_NAME_RE` recognizes as a spine disclosure.
        _members = {_clean_label(str(m)) for m in (ch.get("members") or [])}
        # KNOWN ASYMMETRY (review note, PR #228): the verifier's
        # co-occurrence floor does NOT exclude serial chain partners (it only
        # excludes matrix sibling legs), while this pool does. If a repo's
        # binding floor ever lands on an undisclosed SERIAL partner, the
        # verifier fails while this note names a different (concurrent)
        # check — resolve by teaching the verifier the same exclusion, never
        # by widening this pool (a serial partner is not a concurrent floor).
        _conc = [c for c in others if _clean_label(_check_name(c)) not in _members]
        _cfloor = _floor_candidate(_conc, cooccur, gating_n)
        _cfloor_s = _eff_floor_s(_cfloor) if _cfloor else 0.0
        floor_line = ""
        if _cfloor is not None and 0.0 < _cfloor_s < pole_p:
            floor_line = (f" Separately, the slowest concurrent check is "
                          f"`{_clean_label(_check_name(_cfloor))}` (~{_clock(_cfloor_s)} on the PRs "
                          "this chain gates), which runs alongside the chain and independently caps "
                          "the merge wall-clock.")
        if win <= 0:
            # Co-longest chains (`co_longest_n > 1`) net the per-PR headroom to 0
            # via the whole-chain-zeroed runner-up re-walk — the same both-counted
            # rule as the headline win. Say so instead of quoting a positive win.
            return [
                f"> **What a change here can buy (wall-clock):** ~0s for now - this "
                f"check is stage {stage}/{chain_len} of the {label} gate chain "
                "(`needs:` serializes it), but a competing path of comparable length "
                "gates the merge just behind the chain, so time cut here alone buys "
                "~nothing until that path also drops; the saving is runner-minutes, "
                "not wall-clock." + floor_line, ""]
        cap_via = ("the chain's measured headroom before the next-longest competing "
                   "path gates instead" if win_s <= pole_p
                   else "this job's own span (a stage can't give back more time than "
                        "it runs)")
        return [
            f"> **What a change here can buy (wall-clock):** up to **~{_clock(win)}** - "
            f"this check is stage {stage}/{chain_len} of the {label} gate chain and "
            f"`needs:` serializes the members, so time cut here comes 1:1 off the "
            f"chain wait; {cap_via} caps it there, below which further savings are "
            "runner-minutes, not wall-clock."
            + _recoverable_reconciliation(win, _tw) + floor_line, ""]
    addr_leg = max(pole_p - binding_s, 0.0)
    # If the BINDING floor is a check with no workflow file (managed/external) sitting ABOVE the
    # highest file-backed floor we could name, the gate does NOT drop to that file check — it
    # drops to the untunable blocker. Name THAT honestly rather than quoting a faster file floor
    # next to the (correct, smaller) capped number — else the two figures don't reconcile.
    if _eff_floor_s(binding) > _eff_floor_s(floor_next) + 0.5:
        cap_name = _clean_label(_check_name(binding))
        return [
            f"> **What a change here can buy (wall-clock):** up to **~{_clock(addr_leg)}** — a "
            f"concurrent check with no workflow file to speed up here, `{cap_name}` "
            f"({_clock(binding_s)}), caps it there; below that the gate is that check, not this "
            "job." + _recoverable_reconciliation(addr_leg, _tw), ""]
    pole_wf = str(pole.get("workflow_file") or "")
    legs = [c for c in candidates
            if _same_matrix(pole_name, _check_name(c), pole_wf, str(c.get("workflow_file") or ""))]
    non_legs = [c for c in others
                if not _same_matrix(pole_name, _check_name(c), pole_wf,
                                    str(c.get("workflow_file") or ""))]
    floor_m = _floor_candidate(non_legs, cooccur, gating_n)
    if len(legs) >= 2 and floor_m is not None:
        addr_m = max(pole_p - _binding_floor_s(non_legs, cooccur, gating_n), 0.0)   # cap by managed/universal non-legs too
        return [
            f"> **What a change here can buy (wall-clock):** this job's matrix legs run "
            f"in parallel, so speeding **this one leg** saves only ~{_clock(addr_leg)} "
            f"(the next leg, `{_clean_label(_check_name(floor_next))}`, is "
            f"{_clock(_eff_floor_s(floor_next))}). Because the legs share one job config, a "
            f"change that speeds *every* leg at once drops the whole matrix toward the next "
            f"check, `{_clean_label(_check_name(floor_m))}` ({_clock(_eff_floor_s(floor_m))}), "
            f"for up to **~{_clock(addr_m)}** of merge wait."
            + _recoverable_reconciliation(addr_m, _tw), ""]
    return [
        f"> **What a change here can buy (wall-clock):** up to **~{_clock(addr_leg)}** — "
        f"it gates until it drops to the next concurrent check, "
        f"`{_clean_label(_check_name(floor_next))}` ({_clock(_eff_floor_s(floor_next))}); below "
        "that the gate moves and further savings are runner-minutes, not wall-clock."
        + _recoverable_reconciliation(addr_leg, _tw), ""]


# ── Aggregation gate (the degenerate chain SINK) ─────────────────────────────────────
# A "success-aggregation gate" is the trivial job whose ONLY purpose is to `needs:` a set
# of real jobs so that ONE check can be the single required status check — vercel/next.js'
# `thank you, build` (job `buildPassed`: `needs: [deploy-target, build, build-wasm,
# build-native]`, body `run: exit 1` behind an `if:`, P50 3s, required). Crowning it is
# CORRECT data (it literally gates every PR), but drilling it and handing the reader a
# "capture timing, then optimize this step" prompt is INERT advice: there is nothing inside
# a 3-second no-op to speed, and moving it off the PR path defeats the reason it exists.
# Its wait IS its `needs:` upstream, so the honest lever is the slowest upstream member.
#
# THRESHOLD. `_AGG_GATE_TRIVIAL_S` = 30s: a hosted-runner job that does any real work
# (checkout + toolchain setup alone) clears 30s comfortably, so a check whose P50 sits under
# it did nothing but wait on `needs:`. Duration ALONE never matches — it is one necessary
# condition of a STRUCTURAL test (terminal node + covers every non-terminal job in its
# workflow); a genuinely fast real job (a 3s lint) fails the structure test and keeps
# today's rendering.
_AGG_GATE_TRIVIAL_S = 30.0


def _agg_job_produces_check(job_name: str, is_matrix: bool, check: str) -> bool:
    """True iff a workflow job whose display `name:` is `job_name` (matrix-flagged iff
    `is_matrix`) produces the check-run named `check`. Mirrors the scanned check->job
    matching used elsewhere in the pipeline (exact display name; a matrix `name:` template
    with `${{ … }}` holes compiled to `.+?`; a static matrix job's `Name (leg…)` legs). An
    ENTIRELY-placeholder template is refused — a match-anything `^.+?$` would bind any
    check-run and let this gate name an unrelated check as "the slowest upstream member"."""
    if not job_name:
        return False
    if job_name == check:
        return True
    if "${{" in job_name:
        parts = re.split(r"\$\{\{.*?\}\}", job_name)
        if not any(p.strip() for p in parts):
            return False  # degenerate all-placeholder template
        return bool(re.match("^" + ".+?".join(re.escape(p) for p in parts) + "$", check))
    if is_matrix:
        return bool(re.match("^" + re.escape(job_name) + r"(?: \(.*\))?$", check))
    return False


def _agg_gate_shape(pole: dict[str, Any], job_graph: dict[str, Any] | None,
                    checks: list[dict[str, Any]],
                    timeline: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The aggregation-gate shape for `pole`, or None when it doesn't match.

    ALL of the following must hold (structure first — duration alone never matches):

    (a) TRIVIAL — the check's headline P50 is <= `_AGG_GATE_TRIVIAL_S` (see above).
    (b) TERMINAL SINK COVERING ITS WORKFLOW — the pole's job is a terminal node (no job in
        the workflow `needs:` it) and its TRANSITIVE `needs:` closure contains every
        NON-terminal job in that workflow. Uncovered siblings must themselves be terminal,
        which is exactly the shape of an `if:`-conditional peer (next.js' `publishRelease`,
        `deploy-tarball`): a conditional peer sink is never something the gate could
        `needs:`, so "effectively all the other jobs" is expressed STRUCTURALLY rather than
        by reading `if:` (which the scanned graph does not carry). The closure must hold
        >= 2 jobs, so a single-parent `needs: [build]` stage — a chain member, not an
        aggregation sink — never matches.
    (c) NO SUBSTANTIVE WORK OF ITS OWN — step data is used only as a DISQUALIFIER, because
        it usually does not exist for a check like this (a 3s gate is never drilled). When
        per-step data IS present and any step exceeds the trivial threshold, the job does
        real work and the shape is refused. With no step data, (a)+(b) stand on their own
        and the rendered role line discloses that.
    (d) A NAMED, MEASURED UPSTREAM — at least one member of the `needs:` closure resolves to
        a measured check. Without one there is no honest "the real lever is over there" to
        point at, so the pole keeps today's rendering rather than trading an inert prompt
        for a contentless role line.

    Returns `{"job_id", "upstream" (job ids), "slowest" (the measured check dict),
    "unmeasured" (upstream display names with no measured check), "steps_known"}`."""
    head_s, _ = _pole_headline(pole)
    if head_s > _AGG_GATE_TRIVIAL_S:
        return None
    wf = str(pole.get("workflow_file") or "")
    jobs = _as_dict(_as_dict(job_graph).get(wf))
    if not jobs:
        return None  # no scanned graph for this workflow — the shape is unknowable
    check = str(pole.get("check") or "")
    jid = str(pole.get("job") or "")
    if jid not in jobs:
        # The pole's `job` may be a DISPLAY name (or absent); resolve it back to the YAML key.
        cands = [k for k, meta in jobs.items()
                 if _agg_job_produces_check(str(_as_dict(meta).get("name") or k),
                                            bool(_as_dict(meta).get("matrix")), check)]
        if len(cands) != 1:
            return None  # unresolvable or ambiguous identity — never guess
        jid = cands[0]
    needs_of = {k: [str(n) for n in _as_list(_as_dict(m).get("needs"))]
                for k, m in jobs.items()}
    depended_on = {n for ns in needs_of.values() for n in ns}
    if jid in depended_on:
        return None  # something `needs:` it — a chain stage, not a terminal sink
    closure: set[str] = set()
    frontier = list(needs_of.get(jid) or [])
    while frontier:
        n = frontier.pop()
        if n in closure or n not in jobs:
            continue
        closure.add(n)
        frontier.extend(needs_of.get(n) or [])
    if len(closure) < 2:
        return None
    non_terminal = {k for k in jobs if k in depended_on}
    if not non_terminal <= closure:
        return None
    _step_durs = [_num(_as_dict(s).get("dur_s"))
                  for s in _as_list(_as_dict(timeline).get("steps"))]
    _step_durs += [_num(_as_dict(s).get("p50_s")) for s in _as_list(pole.get("steps"))]
    if any((d or 0.0) > _AGG_GATE_TRIVIAL_S for d in _step_durs):
        return None  # it does substantive work of its own
    # Resolve each upstream job to its measured check(s) IN THE SAME WORKFLOW — a
    # cross-workflow same-name check is a different job entirely.
    slowest: dict[str, Any] | None = None
    unmeasured: list[str] = []
    for j in sorted(closure):
        meta = _as_dict(jobs.get(j))
        nm = str(meta.get("name") or j)
        matched = [_as_dict(c) for c in checks
                   if str(_as_dict(c).get("workflow_file") or "") == wf
                   and _agg_job_produces_check(nm, bool(meta.get("matrix")),
                                               _check_name(_as_dict(c)))]
        if not matched:
            unmeasured.append(nm)
            continue
        top = max(matched, key=lambda c: _num(c.get("p50_s")) or 0.0)
        if slowest is None or ((_num(top.get("p50_s")) or 0.0)
                               > (_num(slowest.get("p50_s")) or 0.0)):
            slowest = top
    if slowest is None:
        return None
    return {"job_id": jid, "upstream": sorted(closure), "slowest": slowest,
            "unmeasured": unmeasured,
            "steps_known": bool(_as_list(_as_dict(timeline).get("steps"))
                                or _as_list(pole.get("steps")))}


# Wall-clock severity dot for a long-pole title, by the gate's measured P50 wait:
# 🔴 >= 5m, 🟠 2-5m, 🟡 < 2m. Makes each finding's impact tier scannable in its title.
_SEV_HIGH_S = 300.0
_SEV_MED_S = 120.0


def _severity_dot(p50_s: Any) -> str:
    s = _num(p50_s) or 0.0
    if s >= _SEV_HIGH_S:
        return "🔴"
    if s >= _SEV_MED_S:
        return "🟠"
    return "🟡"


def _pole_headline(pole: dict[str, Any]) -> tuple[float, str]:
    """The duration a pole HEADER should show, plus a one-line caveat. For a BIMODAL
    pole the drill (timeline + verbatim) is captured from a representative SLOW run -
    `collect_runs` targets the slow mode so the drill shows WHY it's slow, not the
    fast median run. So the headline must be the slow-mode time: a `2m25s` header (the
    P50, which sits on the fast mode) over a 5m50s drill reads as a contradiction.
    The override (and its "P50 sits on the fast mode" caveat) fires ONLY when the median
    truly sits on the fast cluster (`p50 <= (lo + hi) / 2`); a job whose median already
    sits in the SLOW cluster (a strict majority of runs slow) keeps its honest p50 header
    and no caveat, so the caveat never contradicts the split it renders.
    Returns (seconds_for_header_and_severity, caveat_line) - caveat is '' for a normal
    pole whose median already reflects its one mode."""
    p50 = _num(pole.get("p50_s")) or 0.0
    bi = pole.get("bimodal")
    if isinstance(bi, dict):
        hi, lo, frac = (_num(bi.get("high_p50_s")), _num(bi.get("low_p50_s")),
                        _num(bi.get("slow_frac")))
        # Only override when there's a genuine, materially-slower second mode the
        # median understates AND the median actually sits on the FAST cluster - nearer
        # the low mode, `p50 <= (lo + hi) / 2`, the SAME midpoint predicate `_bimodal_note`
        # uses to decide a median is on the fast mode. `hi > p50 * 1.15` alone is NOT
        # enough: when a strict majority of runs are slow the median sits IN the slow
        # cluster by construction (nrwl/nx: p50 46m33s, low 13m41s, high 54m14s, 59% slow -
        # 3254 > 2793*1.15=3212 so the ratio test fired), yet the p50 header already
        # reflects the slow mode, so a "P50 sits on the fast mode" caveat CONTRADICTS the
        # very split it renders. A job whose median is already the slow mode needs no
        # caveat - its header is already honest.
        if hi and lo and frac and hi > p50 * 1.15 and p50 <= (lo + hi) / 2:
            caveat = (f"_Bimodal: **~{_clock(hi)}** on ~{round(frac * 100)}% of runs "
                      f"(shown here - the drill is one of those), **~{_clock(lo)}** on "
                      f"the rest. The P50 ({_clock(p50)}) sits on the fast mode, so its "
                      f"P50 ranking under-states this gate._")
            return hi, caveat
    return p50, ""


def _tl_name(s: dict[str, Any]) -> str:
    """The display name of one step entry, from EITHER of the two step shapes the
    collision scan draws from — the captured representative-run TIMELINE (whose entries
    are keyed `name`, the schema `collect_runs._step_timeline` writes: `name`/`number`/
    `start_s`/`dur_s`) or the P50 STEP LIST on the pole (`pole["steps"]`, keyed `step`).
    Reads `name` first (the writer's real timeline key) then `step` (the P50 list). This
    single accessor is why `_check_step_collision` sees the real names with a captured
    timeline present (issue #92: the helper used to read only `step`, so a captured
    timeline — every drilled pole has one — scanned all-empty names and the collision
    clause never fired on a real pole). Robust to either key, so a writer-side rename of
    one can't silently blank the scan; `tests/test_blocking_path.py`'s schema-parity pin
    fails loudly if the writer's timeline key drifts out from under this reader."""
    return str(s.get("name") or s.get("step") or "")


def _check_step_collision(pole: dict[str, Any],
                          timeline: dict[str, Any] | None = None) -> tuple[str, str]:
    """Owner UX edit (2026-07-19): detect a NAME COLLISION between the pole's CHECK name
    and a rendered STEP name inside it. Example (live dogfood repo): the check `test` IS
    the 8m58s gate, yet the drill shows a small step named `Test` (31s / 6%) — read
    together they contradict ("test is the bottleneck" vs a 31s `Test` step), when the
    real dominant step is `Verify the guards…`. Returns
    `(colliding_step_display, dominant_step_display)` when some rendered step's name
    matches the check name (case-insensitive) AND that step is NOT the dominant step (so
    a disambiguation is genuinely owed); `('', '')` otherwise — no collision, or the
    shared name IS the dominant step (a legitimately name-matched bottleneck needs no
    warning). Presentation-only: nothing binds to it. The rendered step names are taken
    from the same source the waterfall prints — the representative-run timeline when one
    is captured, else the P50 step list."""
    check = _clean_label(str(pole.get("check", ""))).strip()
    if not check:
        return "", ""
    dom = _clean_label(str(pole.get("dominant_step") or "")).strip()
    # No known dominant step → we can't name what the bottleneck IS, so no disambiguation
    # is owed. Bail before the loop: without this, `dom == ""` makes the guard below
    # (`nm.casefold() != dom.casefold()`) always true, and the fallback `(dom or nm)`
    # returns `(nm, nm)` — rendering the self-contradiction "its small `Test` step is not
    # the bottleneck — the dominant step is `Test`" (Greptile P2, PR #75).
    if not dom:
        return "", ""
    tl_steps = (timeline or {}).get("steps") or []
    # Both name-sources funnel through the ONE `_tl_name` accessor so the scan reads the
    # writer's real key on a captured timeline (`name`) and the P50 list's key (`step`)
    # from a single boundary — the fix for issue #92, where reading only `step` blanked
    # every drilled pole's timeline scan. Every OTHER timeline consumer (`_dom_index`,
    # `_emit_gantt`, `_dominant_step_from_timeline`, `_audit_links`) already reads `name`
    # directly and is intentionally left untouched.
    raw_names = ([_tl_name(s) for s in tl_steps] if tl_steps
                 else [_tl_name(s) for s in (pole.get("steps") or [])])
    cl = check.casefold()
    for raw in raw_names:
        nm = _clean_label(str(raw)).strip()
        if nm and nm.casefold() == cl and nm.casefold() != dom.casefold():
            return nm, (dom or nm)
    return "", ""


def _pole_addressable(pole: dict[str, Any],
                      candidates: list[dict[str, Any]]) -> float:
    """The biggest WALL-CLOCK win the executive-summary headline credits to fixing this
    pole, in seconds. The headline names ONE check ("the slowest fixable check"), so the
    figure is the CONSERVATIVE single-check floor — the headroom down to the actual
    2nd-slowest CONCURRENT check (a sibling matrix leg included). It must NOT use the
    optimistic matrix shared-config best-case (drop every leg to the next NON-leg), which
    over-promised past a concurrent sibling leg the report's own chart shows still gating
    (a 175s `main-linux py310` beside a 517s `main-windows py310`). 0.0 when nothing floors
    it, OR when the pole isn't the gate (a slower concurrent check exists) — mirroring
    `_floor_note`'s non-gate guard so the headline and the per-pole prose never disagree.
    Uses the SAME `_floor_candidate` selection (effective bimodal-high floor, file-backed +
    near-universal siblings only) as `_floor_note`, so the bottom-line win and the pole's own
    floor note quote the identical addressable ceiling. A modal-chain MEMBER pole
    (`_chain_member` stash, review V2 / OD-F2) mirrors `_floor_note`'s chain branch: the win
    is capped at the stamped chain headroom and the member's own span."""
    if pole.get("job_timing_unavailable"):
        return 0.0
    pole_name = _clean_label(str(pole.get("check", "")))
    pole_p = _num(pole.get("p50_s")) or 0.0
    cooccur, gating_n = pole.get("_cooccur"), int(pole.get("_gating_n") or 0)
    others = [c for c in candidates if _clean_label(_check_name(c)) != pole_name]
    binding_s = _binding_floor_s(others, cooccur, gating_n)
    # Mirror `_floor_note` EXACTLY so the bottom-line win and the per-pole note never disagree:
    # 0 when the pole isn't the gate (a universal blocker — managed/external INCLUDED — already
    # floors at >= the pole), or when there's no file-backed floor to name. The addressable win
    # is `pole − binding` (managed checks count toward the cap), never `pole − the file name`.
    if pole_p <= 0 or binding_s >= pole_p or _floor_candidate(others, cooccur, gating_n) is None:
        return 0.0
    ch = pole.get("_chain_member")
    if isinstance(ch, dict):
        # Mirror `_floor_note`'s chain branch (review V2 / OD-F2, the same
        # never-disagree rule): a modal-chain member's win is capped at the
        # stamped chain headroom and its own span, never the chain-blind
        # `pole − binding` arithmetic below.
        return max(min(_num(ch.get("win_s")) or 0.0, pole_p), 0.0)
    return max(pole_p - binding_s, 0.0)


def _is_credited_cluster_lever(f: dict[str, Any]) -> bool:
    """A STAMPED, credited developer-facing cluster-floor lever (#49). Keyed on the
    PERSISTED `cluster_floor_lever` marker (collect_runs stamps it on every OPT73
    cluster construction) AND a positive `wall_clock_p50_s` — i.e. the shared-step fix
    survived the whole cascade as REAL merge-wait, not a bill-only / off-path zero. A
    legacy artifact predating the persisted flag has no marker, so this is False and the
    bottom line renders byte-identically (the guard SKIPs loud on those).

    Issue #114: crown eligibility binds to the CLUSTER's OWN presence on the gating spine,
    NOT to its workflow hosting some required check. A cluster whose jobs were DROPPED from
    the merge-gating spine carries `off_spine=True` (stamped in collect_runs by
    `_stamp_off_spine_findings` from the spine's dropped/kept check sets) — its `Run pytest`
    matrix can be the workflow's heaviest step yet never gate the merge, because the required
    checks in that same workflow are the fast hassfest/requirements/collect-info jobs
    (home-assistant/core: `ci.yaml`'s 10-leg pytest cluster is off-spine while `Check hassfest`
    is the required gate). Such a cluster is bill/throughput, not merge-wait, so it can never
    crown the typical-PR headline — mirrors the same `off_spine` exclusion the credited
    long-pole selection already applies (`verify_report`'s `credited_wc`)."""
    return (isinstance(f, dict)
            and f.get("cluster_floor_lever") is True
            and not f.get("off_spine")
            and (_num(f.get("wall_clock_p50_s")) or 0.0) > 0.0)


def _headline_cluster_lever(
        findings: list[dict[str, Any]]) -> tuple[float, dict[str, Any] | None]:
    """The biggest STAMPED credited cluster-floor lever ceiling the bottom line must not
    bury (#49), as (seconds, finding) — or (0.0, None) when none exists. A cluster-floor
    lever (OPT73) cuts a step shared across sibling legs, so its fix saves its full stamped
    `wall_clock_p50_s` of merge wait — for a concurrent matrix cluster the whole cluster
    drops in lockstep; for a `needs:`-chained SEQUENTIAL cluster the per-stage savings
    compound serially (`_cluster_headline_bottom_line` phrases each honestly). Either way
    the per-pole `_pole_addressable` / chain-headroom arithmetic can't see it — it caps at
    the next SIBLING leg (mastodon: ~36s per-leg vs a stamped 627s cluster win; electron:
    ~5m37s vs 2635s). This RE-DERIVES the same selection from the ceiling collect_runs
    already sized; it never re-computes headroom."""
    best_s, best = 0.0, None
    for f in findings:
        if not _is_credited_cluster_lever(f):
            continue
        wc = _num(f.get("wall_clock_p50_s")) or 0.0
        if wc > best_s:
            best_s, best = wc, f
    return best_s, best


_CLUSTER_STEP_RE = re.compile(r"`([^`]+)`")


def _cluster_headline_bottom_line(
        wait_dur: str, secs: float, f: dict[str, Any]) -> list[str]:
    """The Bottom-line sentence when a stamped cluster-floor lever is the biggest single
    measured win (#49). Names the shared step (from the finding's own evidence) and the
    cluster's leg count so the headline magnitude is self-justifying, and points the
    reader at the OPT73 appendix entry for the drill. Mirrors the phrasing of the
    top-fixable branch below (a plain framing sentence, not a claim — same as the
    existing bottom-line forms). `wait_dur` is the merge-wait WALL (issue #115: makespan
    when the chain diverges, else the clamped chain total) rendered WITHOUT a leading `~`,
    so the cluster win's `**~<secs>**` stays the first tilde-clock the verifier reads."""
    wf = _wf_base(str(f.get("workflow_file") or ""))
    n_legs = len([j for j in (f.get("affected_jobs") or []) if str(j).strip()])
    m = _CLUSTER_STEP_RE.search(str(f.get("evidence") or ""))
    step = f"the `{m.group(1)}` step" if m else "a shared step"
    # Honesty: a `needs:`-chained cluster runs SEQUENTIALLY, so "concurrent legs … in
    # lockstep … a sibling leg gates" is false for it (the deepgram f19 mislabel the
    # appendix already avoids). collect_runs persists the concurrency nature; default a
    # missing marker to concurrent (the matrix-leg case, and the legacy default).
    concurrent = f.get("cluster_legs_concurrent") is not False
    if concurrent:
        legs = f"{n_legs} concurrent legs" if n_legs >= 2 else "concurrent legs"
        mech = (f"recurs across the {legs} of `{wf}`, so one shared-config fix lowers the "
                "whole matrix cluster in lockstep (the per-leg headroom is far smaller "
                "because a sibling leg otherwise gates)")
    else:
        stages = (f"{n_legs} sequential (`needs:`-chained) stages" if n_legs >= 2
                  else "sequential (`needs:`-chained) stages")
        mech = (f"recurs across the {stages} of `{wf}`, so one shared-config fix lowers "
                "every stage; because they run serially the per-stage savings compound on "
                "the critical path")
    return [
        f"> **Bottom line.** A typical PR waits **{wait_dur}** for all checks to finish. "
        f"The biggest single measured win is **~{_clock(secs)}** — {step} {mech}. "
        "See the **OPT73** entry under [Also noticed](#also-noticed) for the "
        "measured step, evidence, and fix recipe.", ""]


def _bar(secs: float | None, mx: float) -> str:
    secs = secs or 0.0
    if mx <= 0 or secs <= 0:
        return " " * _BARW
    n = max(1, min(_BARW, round(secs / mx * _BARW)))
    return "█" * n + " " * (_BARW - n)


def _core(label: str, secs: float | None, mx: float, dur_str: str | None,
          pct_str: str = "") -> str:
    if dur_str is not None:
        dur = dur_str
    elif isinstance(secs, (int, float)):
        dur = _clock(secs)
    else:
        dur = ""
    return (f"   {_lbl(label):<{_LBLW}}  {_bar(secs, mx)}  "
            f"{dur:>{_DURW}}  {pct_str:>{_PCTW}}")


def _emit_level(out: list[str], rows: list[tuple[str, float | None, str | None]],
                header_below: str | None, blocker_note: str = "",
                mark: bool = True, pct_of: str = "",
                scale_to: float | None = None, mark_idx: int = 0,
                pct_denom: float | None = None) -> None:
    """Render one level's bars. Each row shows its RAW duration AND, when `pct_of`
    is set, its share: `sum` (serial parts that add up to the parent) or `max`
    (parallel parts where you wait for the slowest, so the share is of the wait).
    Showing both, every level, avoids switching between raw-only and percent-only.

    `scale_to` handles levels whose measured values are SUMMED-across-workers (e.g.
    vitest transform/import, which exceed wall because files run on parallel threads):
    the displayed duration becomes share × `scale_to` (the step/project wall), so the
    parts sum to the wall and never exceed it. The % and bar (both share-based) are
    unchanged.

    `mark_idx` is the row that carries the ◀ "addressable lever" marker (and, when a
    drill follows, the connector wire down to the next level). It defaults to the
    first/longest row, but a caller passes the row of the DOMINANT CATEGORY lead so the
    chart's marker agrees with the category-aware crown the root-cause section + agent
    prompt point at — not the single longest step, which lives in a NON-dominant
    category when a multi-step phase out-aggregates it (the dominant_step-disagreement
    class). The wire hangs from `mark_idx` down; rows above it stay clean.

    `pct_denom` overrides the percentage denominator (default None keeps the exact prior
    behaviour: the denom is derived from `pct_of` — `sum` of the rows or their `max`). The
    Long pole map's level 2 passes the CHECK's own p50 (the job wall the reader just saw on
    level 1) so each step's % is its share of the check, not of the shown rows."""
    vals = [s for _l, s, _d in rows if isinstance(s, (int, float))]
    mx = max(vals, default=0.0)
    denom = (sum(vals) if pct_of == "sum" else mx) if pct_of else 0.0
    if pct_denom is not None:
        denom = pct_denom
    has_next = header_below is not None
    if not 0 <= mark_idx < len(rows):
        mark_idx = 0
    for i, (label, secs, dur_str) in enumerate(rows):
        pct_str = (f"{round(100 * secs / denom)}%"
                   if denom and isinstance(secs, (int, float)) else "")
        if scale_to is not None and denom and isinstance(secs, (int, float)):
            dur_str = _clock(secs / denom * scale_to)
        line = _core(label, secs, mx, dur_str, pct_str)
        if i == mark_idx and mark:
            line += " ◀┐" if has_next else (f" ◀  {blocker_note}" if blocker_note else " ◀")
        elif has_next and i > mark_idx:
            line += "  │"
        out.append(line if (has_next and i >= mark_idx) else line.rstrip())
    if has_next:
        out.append("   ┌" + "─" * (_MARKCOL - 4) + "┘")
        out.append("")
        out.append(f"   ▼ {header_below}")
        out.append("")


# --------------------------------------------------------------------------- #
# Log parsing — detect the leaf root cause (per-test-file migrations, or
# coverage-instrumented compile/load) and return the two deeper levels + fix key.
# --------------------------------------------------------------------------- #

def _clean_log(text: str) -> list[str]:
    return [re.sub(r"\x1b\[[0-9;]*m", "", re.sub(r"^\S+Z ", "", l))
            for l in text.splitlines()]


def _gradle_build_secs(joined: str) -> float | None:
    """Total seconds from a Gradle `BUILD SUCCESSFUL in <Xh Ym Zs>` summary, or None when
    absent. Gradle prints this once, wrapping the whole invocation, so it bounds the build
    wall a serial test step gates on. Handles every unit Gradle emits (`1h 2m 3s`,
    `17m 6s`, `45s`, `800ms`); `ms` is matched before the bare `m`/`s` so a millisecond
    tail isn't misread as minutes/seconds."""
    m = re.search(r"^.*BUILD SUCCESSFUL in (.+?)\s*$", joined, re.MULTILINE)
    if not m:
        return None
    total, found = 0.0, False
    for val, unit in re.findall(r"([\d.]+)\s*(ms|h|m|s)", m.group(1)):
        found = True
        total += {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}[unit] * float(val)
    return total if found else None


def _turbo_time_secs(s: str) -> float | None:
    """Seconds from a turbo run-summary `Time:` value (`9m48.756s`, `1m20.171s`,
    `3.013s`, `1h2m3s`). Same unit set as Gradle; `ms` is matched before the bare
    `m`/`s` so a millisecond tail isn't misread as minutes/seconds. None when nothing
    parses (so the caller can fall back to a non-time heuristic)."""
    total, found = 0.0, False
    for val, unit in re.findall(r"([\d.]+)\s*(ms|h|m|s)", s):
        found = True
        total += {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}[unit] * float(val)
    return total if found else None


# A turbo build that's NOT fully cold (some packages cached) but rebuilds at least
# this share every run is treated as cache-key churn, not normal per-PR change: most
# PRs touch few packages, so rebuilding ~half+ of them repeatedly points at an
# unstable cache key. Hedged in the finding's framing - some misses are legitimate.
_PARTIAL_MISS_FLOOR_PCT = 40.0

# A `docker buildx build` with NO persistent BuildKit cache (`--cache-from`/`--cache-to`)
# only counts as a finding when the build is heavy enough that a warm cache would have
# mattered: a one-layer/few-second image isn't worth a cache backend.
# Floor on the cold-layer wall (sum of the RUN/FROM/COPY/ADD `#NN DONE Xs` steps), in
# seconds, and on the number of work-bearing (RUN/FROM/COPY/ADD) steps that ran cold.
_BUILDX_COLD_WALL_FLOOR_S = 60.0
_BUILDX_MIN_WORK_STEPS = 2

# A pytest run with `pytest-xdist` INSTALLED (in the plugin banner) but NO `-n`/
# `--numprocesses` on the invocation runs every test serially on one worker — the
# parallelism dependency is present and simply not switched on. Only a finding when the
# run is heavy enough that workers would matter: a wall floor (below it, parallelising
# saves little merge-wait) and a collected-item floor (too few tests and a worker pool
# is pure overhead — a 3-test run is a matrix-leg problem, not an xdist one).
_PYTEST_SERIAL_WALL_FLOOR_S = 120.0
_PYTEST_MIN_ITEMS = 8

# A serial `cargo test` run (the standard libtest harness, NOT cargo-nextest) only counts
# as a shardable finding when the suite is genuinely large — enough separate test binaries
# that splitting them across runners helps — AND the summed serial wall is worth the split
# (below it, sharding saves little merge-wait and just adds per-job startup overhead).
_CARGO_MIN_BINARIES = 4
_CARGO_SERIAL_WALL_FLOOR_S = 120.0

# A benchmark step invoked with a repeated-iterations flag (`--runs N`) only counts as a
# finding when the rerun count is high enough that the warm reruns are a material, reducible
# cost: a 1-3 sample smoke benchmark needs its repetition for a stable median and isn't worth
# flagging. Hedged in the hand-off — lowering N is a measurement-coverage decision.
_BENCH_RERUNS_FLOOR = 5

# An Android instrumentation suite (`./gradlew connectedCheck` / `connectedAndroidTest`)
# runs each module's androidTest variant serially on a SINGLE emulator: Gradle prints one
# `> Task :<module>:connected[<Variant>]AndroidTest` per module, the per-module
# assemble+install+run fanning in to one device. Sharding across a matrix of emulators
# (android-emulator-runner) only pays off when there are several modules to spread AND the
# build is long enough that the per-shard AVD-boot overhead is worth it - so fire only above
# both a module-count floor and a Gradle build-wall floor.
_ANDROID_MIN_MODULES = 3
_ANDROID_CONNECTED_WALL_FLOOR_S = 120.0

# A single un-sharded JVM Gradle `:test` task (`./gradlew <module>:test`) runs the whole
# module's test suite serially in one task - `maxParallelForks`/`forkEvery` (in-task fork
# parallelism) or splitting the suite across CI jobs would overlap it. Only a finding when
# the Gradle build wall is long enough that the serial test task is a material gate (a quick
# unit-test build isn't worth forking).
_GRADLE_TEST_WALL_FLOOR_S = 300.0

# An `eslint` invocation at a COMMAND position (detector G). Every alternative is a
# REQUIRED, LINE-ANCHORED command marker (not `.search()`-anywhere) so prose / tool output
# can't false-fire: a shell echo (`$`/`>`), a GHA `[command]` line, or a line-start JS
# runner (`npx`/`pnpm exec`/`bun run` …) — each allowing a `path/` prefix before `eslint`.
# The trailing `(?=\s|$)` requires a real command boundary right after the token: it rejects
# dependency lines (`+ eslint@…`, `eslint-plugin-…`, `typescript-eslint@…`), config files
# (`eslint.config.js`), AND script names (`eslint:fix`) in one assertion — anything other
# than whitespace-or-EOL after `eslint` is not the bare binary. There is deliberately NO
# bare-word or standalone-path alternative: a line like `eslint reported 2 warnings` (eslint's
# own output) or `/usr/bin/eslint` (a `which eslint` line) must NOT be read as an invocation.
# group(1) captures the flags so the caller can test them for the `--cache` enable flag.
_ESLINT_CMD = re.compile(
    r"(?:"
    r"^\s*[$>]\s*(?:\S*/)?"                                       # `$ eslint`, `> path/eslint`
    r"|^\s*\[[Cc]ommand\](?:\S*/)?"                               # `[command]/usr/bin/eslint …`
    r"|^\s*(?:npx|bunx|pnpm|yarn|bun)\s+(?:exec\s+|run\s+|x\s+|dlx\s+)?(?:\S*/)?"  # JS runner
    r")"
    r"eslint(?=\s|$)(.*)$"
)


# --- install-lifecycle build (a build runs DURING dependency install) -------------
# A root `package.json` `"prepare": "turbo build"` (or a `postinstall`) makes
# `pnpm install --frozen-lockfile` run a full build as part of dependency installation,
# BEFORE the explicit package checks. That build gates the install step even when the
# cache mostly hits — a "work runs during install" problem, categorically distinct from
# a cache MISS problem (unstable key). Fire only when the in-install build is a material
# gate (below this floor, restructuring install semantics isn't worth the `--ignore-scripts`
# blast radius). Consistent with the sibling detector floors (buildx 60s, pytest 120s).
_LIFECYCLE_BUILD_FLOOR_S = 30.0
# An install step's Run command: a package-manager install/ci at a command position.
# Matched against the `##[group]Run <cmd>` header line (line-anchored so prose can't
# false-fire). `--ignore-scripts` is checked separately (its presence means lifecycle
# scripts are already disabled — nothing to fix).
_INSTALL_STEP_RE = re.compile(r"\b(pnpm|npm|yarn|bun)\s+(?:install|ci|i)\b")
# A lifecycle-script marker line inside the install output (pnpm `<pkg>@ver prepare$ …`
# / npm `> pkg@ver prepare`). Corroborating evidence, not required to fire (the load-
# bearing signal is a timed turbo build INSIDE the install step's log section).
_LIFECYCLE_MARK_RE = re.compile(
    r"(?:\b(?:prepare|postinstall)\$|^\s*[.>]\s+\S+@\S+\s+(?:prepare|postinstall)\b)")
# An EXPLICIT build command (not a lifecycle script). Matched against the echoed command block of a
# `run:` step (header → `##[endgroup]`), so a multi-line `run: |` that does `pnpm install` then
# `pnpm build` is recognized as an explicit build and NOT mislabeled a lifecycle build — the
# single-line `&&`/`;` form is caught separately by the header-chain guard. Scanned only over the
# echoed COMMAND lines, never turbo's own output, so a real lifecycle build (whose only "build"
# token is in post-endgroup turbo output) still fires.
_EXPLICIT_BUILD_RE = re.compile(
    r"\b(?:turbo(?:\s+run)?\s+build|(?:pnpm|yarn|bun)(?:\s+run)?\s+build|npm\s+run\s+build)\b")

# The leaf fix_keys whose magnitude is a cache miss (grounded by collect_runs' cache_dist
# distribution). Mirror of `collect_runs._CACHE_LEAF_KEYS` (kept coupled by a self-test).
_CACHE_LEAF_KEYS = frozenset({
    "turbo-remote-cache", "turbo-partial-cache", "buildx-no-cache",
    "install-lifecycle-build",
})


def _log_step_sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """`[(run_command, start_idx, end_idx), …]` for each `##[group]Run <cmd>` step in a
    cleaned log, the section running to the NEXT `##[group]` marker (so the step's output
    — where a turbo `Cached:`/`Time:` summary lands — is inside its own range). Used to
    tell a build that runs DURING `pnpm install` (install-lifecycle) from a build that
    runs as its own later step (plain turbo D/D2)."""
    marks = [i for i, l in enumerate(lines) if l.startswith("##[group]")]
    out: list[tuple[str, int, int]] = []
    for k, i in enumerate(marks):
        m = re.match(r"##\[group\]Run (.+)", lines[i])
        if not m:
            continue
        end = marks[k + 1] if k + 1 < len(marks) else len(lines)
        out.append((m.group(1).strip(), i, end))
    return out


def _install_build_section(lines: list[str], min_secs: float = _LIFECYCLE_BUILD_FLOOR_S):
    """The `<pm> install` step section with the LARGEST lifecycle build (a timed turbo block), when
    that build is >= `min_secs`, or None. Returns `(cmd, s, e, hit, total, secs, cidx)` — the exact
    section + gating block. The SINGLE source of truth for both the `install-lifecycle` detector
    (which builds the leaf from it, at the `_LIFECYCLE_BUILD_FLOOR_S` firing floor) and
    `_cache_state_of_log` (which reads the per-run miss from the SAME section, `min_secs=0` so a
    WARM sibling run's small in-install build is still measured — otherwise warm runs would drop
    out and bias the distribution toward churn).

    Both callers must land on the SAME section so the per-run miss and the leaf can never diverge.
    We therefore pick the section with the MAX build time ACROSS all qualifying install steps and
    then apply the floor to that winner — NOT the first section that clears the floor. Picking
    first-over-floor let a job with two install steps diverge: the detector (floor 30s) skipped a
    sub-30s earlier step and fired on a later >=30s one, while the state reader (floor 0) returned
    the earlier step — so one run reported a well-cached gating build in its leaf yet contributed
    the earlier step's higher miss to the cross-run distribution. Argmax-then-floor makes both
    callers select the same (largest) build; they differ only on whether it clears the firing floor.

    Guards (all required): a bare `<pm> install` command (no `--ignore-scripts`); no explicit build
    command — neither a single-line `&&`/`;` chain in the header NOR (a `run: |` multi-line block)
    an explicit `pnpm build`/`turbo build` among the echoed command lines, since an explicit build
    is the author's own command, not a lifecycle script; and a real lifecycle-script marker in the
    section."""
    best: tuple[float, str, int, int, int, int, int] | None = None  # (secs, cmd, s, e, hit, total, cidx)
    for _cmd, _s, _e in _log_step_sections(lines):
        if not _INSTALL_STEP_RE.search(_cmd) or "--ignore-scripts" in _cmd:
            continue
        if re.search(r"&&|\|\||;|\|\s*\S", _cmd):
            continue
        # The echoed command block (header → `##[endgroup]`) — a `run: |` multi-line step echoes
        # each command here. Reject if it holds an EXPLICIT build; scan only the command lines, not
        # the turbo OUTPUT that follows `##[endgroup]`, so a genuine lifecycle build still fires.
        _eg = next((k for k in range(_s + 1, _e) if lines[k].startswith("##[endgroup]")), _e)
        if _EXPLICIT_BUILD_RE.search("\n".join(lines[_s:_eg])):
            continue
        if not any(_LIFECYCLE_MARK_RE.search(lines[_i]) for _i in range(_s, _e)):
            continue
        _blocks: list[tuple[int, int, float, int]] = []
        for _i in range(_s, _e):
            _m = re.search(r"Cached:\s+(\d+) cached, (\d+) total", lines[_i])
            if not _m:
                continue
            for _nxt in lines[_i + 1:min(_i + 4, _e)]:
                _tm = re.search(r"\bTime:\s+(\S+)", _nxt)
                if _tm:
                    _t = _turbo_time_secs(_tm.group(1))
                    if _t is not None:
                        _blocks.append((int(_m.group(1)), int(_m.group(2)), _t, _i))
                    break
        if not _blocks:
            continue
        _hit, _total, _secs, _cidx = max(_blocks, key=lambda b: b[2])
        if best is None or _secs > best[0]:
            best = (_secs, _cmd, _s, _e, _hit, _total, _cidx)
    if best is None or best[0] < min_secs:
        return None
    _secs, _cmd, _s, _e, _hit, _total, _cidx = best
    return (_cmd, _s, _e, _hit, _total, _secs, _cidx)


def _cache_state_of_log(text: str | None, fix_key: str | None) -> dict[str, Any] | None:
    """Re-derive one run's cache state — `{miss_pct, cold, remote_off}` — from its log,
    for the given cache leaf. The single source of truth for the per-run miss rate the
    cross-run `cache_dist` distribution is built from (collect_runs passes this as
    `state_fn`), so every sampled run's miss is read the SAME way the drilled run's is.
    None when the log carries no parseable cache signal (that run drops out of the
    distribution, disclosed by count)."""
    if not text or not fix_key:
        return None
    lines = _clean_log(text)
    joined = "\n".join(lines)
    if fix_key in ("turbo-remote-cache", "turbo-partial-cache", "install-lifecycle-build"):
        # For the install-lifecycle leaf, measure the miss rate from the SAME install-step section
        # `_parse_log`'s detector fired on — NOT the whole log. Otherwise a later explicit
        # `turbo build` step (higher miss) would set this run's miss_pct, and its "churn" verdict
        # would wrongly append "BIGGEST LEVER" to a structural "build runs during install" finding
        # (an inconsistency verify_report can't see, since stamped == re-derived). None when no
        # qualifying install section exists (the run drops from the distribution, disclosed).
        if fix_key == "install-lifecycle-build":
            # Read the gating block of the SAME install section the detector fired on (identical
            # hit/total), so the per-run miss and the leaf can never diverge. `min_secs=0`: measure
            # ANY in-install build, even a warm run's small one, so warm runs stay in the
            # distribution (a floor here would drop them and bias the verdict toward churn).
            sec = _install_build_section(lines, min_secs=0.0)
            if sec is None:
                return None
            _cmd, _s, _e, _hit, _total, _secs, _cidx = sec
            _miss = (100.0 * (_total - _hit) / _total) if _total else 0.0
            _ro = "Remote caching disabled" in "\n".join(lines[_s:_e])
            return {"miss_pct": round(_miss, 2),
                    "cold": _ro or (_total > 0 and _hit == 0), "remote_off": _ro}
        lo, hi = 0, len(lines)
        blocks: list[tuple[int, int, float | None]] = []
        for _i in range(lo, hi):
            _m = re.search(r"Cached:\s+(\d+) cached, (\d+) total", lines[_i])
            if not _m:
                continue
            _secs: float | None = None
            for _nxt in lines[_i + 1:min(_i + 4, hi)]:
                _tm = re.search(r"\bTime:\s+(\S+)", _nxt)
                if _tm:
                    _secs = _turbo_time_secs(_tm.group(1))
                    break
            blocks.append((int(_m.group(1)), int(_m.group(2)), _secs))
        remote_off = "Remote caching disabled" in "\n".join(lines[lo:hi])
        if not blocks:
            activity = (joined.count("cache miss, executing")
                        + joined.count("cache bypass, force executing"))
            # Mirror detector D's fire condition (`remote_off or activity > 5`): a LONE stray
            # "cache miss" line is not evidence of a cold run — without this floor a single miss
            # on a sampled run would stamp it 100% cold and drag the cross-run median toward
            # churn/cold. Only a summary-less log with a real burst of misses counts as cold here.
            if remote_off or activity > 5:
                return {"miss_pct": 100.0, "cold": True, "remote_off": remote_off}
            return None
        timed = [b for b in blocks if b[2] is not None]
        hit, total, _ = (max(timed, key=lambda b: b[2]) if timed
                         else max(blocks, key=lambda b: b[1] - b[0]))
        rebuilt = total - hit
        miss_pct = (100.0 * rebuilt / total) if total else 0.0
        return {"miss_pct": round(miss_pct, 2),
                "cold": remote_off or (total > 0 and hit == 0),
                "remote_off": remote_off}
    if fix_key == "buildx-no-cache":
        work = len(re.findall(r"#\d+ \[[^\]]+\] (?:RUN|FROM|COPY|ADD) ", joined))
        if work == 0:
            return None
        cached = len(re.findall(r"^\s*#\d+ CACHED\b", joined, re.MULTILINE))
        return {"miss_pct": round(100.0 * max(work - cached, 0) / work, 2),
                "cold": cached == 0, "remote_off": False}
    return None


def _parse_log(text: str) -> dict[str, Any] | None:
    """Detect the leaf root cause in a captured job log. Returns a leaf dict:
        {fix_key, unit_label, deeper: [ {rows, blocker_note, header?}, … ]}
    `deeper` is the list of drill-down levels below the dominant step (the first
    one's header is built by the renderer from `unit_label`; later ones carry
    their own `header`). `deeper == []` means "root cause is at the step level,
    no further drill — just attach the fix." None = nothing recognized."""
    lines = _clean_log(text)
    joined = "\n".join(lines)

    # --- A: per-DB-engine test files + repeated schema migrations (Prisma) ---
    # Track LINE INDICES so migrations can be attributed to a specific file's log
    # section. Each file prints its per-group migration re-runs then a per-file
    # "Total Migration Time" (= the sum of those re-runs); files are printed one
    # after another, each ending in its `✓ <file> (N tests) <ms>` summary.
    files = [(i, m.group(1), float(m.group(3)) / 1000.0)
             for i, l in enumerate(lines)
             if (m := re.search(r"[✓✗❯]\s+(\S+\.(?:test|spec)\.[tj]sx?)\s+\((\d+)"
                                r"\s+tests?[^)]*\)\s+([\d.]+)ms", l))]
    migs = [(i, float(m.group(1)) / 1000.0) for i, l in enumerate(lines)
            if (m := re.search(r"Total Migration Time:\s*([\d.]+)ms", l))]
    # Sentinels so the names referenced in the `if mper:` block below are always
    # defined (mper > 0 can only be set inside the guard, but making the scope
    # explicit keeps linters quiet and survives a future refactor).
    mper = 0.0
    prev_idx = slow_idx = -1
    slow_name, slow_secs = "", 0.0
    if files and migs:
        files.sort(key=lambda f: -f[2])  # slowest first
        slow_idx, slow_name, slow_secs = files[0]
        # The slowest file's OWN migration time: the largest "Total Migration Time"
        # in ITS section (between the previous file's summary and this one) — i.e.
        # this file's per-file total — NOT an average across the other engines.
        prev_idx = max((i for i, _n, _s in files if i < slow_idx), default=-1)
        own_migs = [s for i, s in migs if prev_idx < i <= slow_idx]
        cand = max(own_migs) if own_migs else 0.0
        # Only a real finding when this file's own migrations are a large, redundant
        # chunk of its wall time (guards against a stray small migration line).
        if cand >= 60 and not (slow_secs and cand / slow_secs < 0.15):
            mper = cand
    if mper:
        tests = max(slow_secs - mper, 0.0)
        # Evidence from the slowest file's own section (verbatim, searchable).
        sect = [l.strip() for i, l in enumerate(lines) if prev_idx < i <= slow_idx]
        ev = [l for l in sect if "MIGRATIONS completed successfully" in l][:2]
        ev += [l for l in sect if "Total Migration Time:" in l
               and (m := re.search(r"([\d.]+)ms", l)) and float(m.group(1)) > 60_000][:2]
        return {
            "fix_key": "prisma-migrate-once",
            "unit_label": f"{len(files)} test files run in parallel "
                          "(you wait for the slowest)",
            "evidence": ev,
            # Verbatim log strings behind the plain-English bars, so the reader can
            # Ctrl-F them in the step log (the bars paraphrase these).
            "search": [slow_name, "Total Migration Time:"],
            # The load-bearing number the fix rests on: migrations are HALF the file.
            # 2 decimals, NOT 1: a value like 51.49% pre-rounded to 51.5 then shown as
            # an integer double-rounds UP to 52, disagreeing with the level bars (which
            # round the raw 51.49 -> 51). Keep enough precision that the single display
            # round matches the bars.
            "magnitude": {"label": "DB-migration share of the slowest test file",
                          "value": round(100 * mper / slow_secs, 2) if slow_secs else 0.0,
                          "unit": "%"},
            "deeper": [
                # The test files run in PARALLEL - you wait for the slowest - so the
                # share is of that wait (max), not a sum.
                {"rows": [(n, s, None) for _i, n, s in files], "blocker_note": "",
                 "pct_of": "max"},
                # Within the slowest file, migrations then tests run in SEQUENCE, so the
                # bigger bar is the bigger LEVER (not "the root cause" - both are real
                # work; cutting the redundant migration re-runs is just the most you can
                # remove at this serial level).
                {"header": f"Level 4 — inside `{slow_name}`: migrations vs test work "
                           "(these run in sequence)",
                 "rows": [("DB migrations  (re-run per test group)", mper, None),
                          ("actual test work", tests, None)],
                 "blocker_note": "BIGGEST LEVER (serial - runs before the tests)",
                 "pct_of": "sum"},
            ],
        }

    # --- B: many package suites under istanbul coverage; compile/load dominates ---
    pkgs: list[tuple[str, float]] = []
    cur: str | None = None
    tr = im = te = 0.0
    for l in lines:
        if (mr := re.search(r"RUN  v\S+ .*/packages/([\w.-]+)", l)):
            cur = mr.group(1)
        if (md := re.search(r"Duration +([\d.]+)s \(transform ([\d.]+)s, setup "
                            r"[\d.]+m?s, import ([\d.]+)s, tests ([\d.]+)s", l)):
            tr += float(md.group(2)); im += float(md.group(3)); te += float(md.group(4))
            pkgs.append((cur or "?", float(md.group(1))))
            cur = None
    istanbul = "coverage.provider=istanbul" in joined or "coverage-istanbul" in joined
    compile_load = tr + im
    if pkgs and istanbul:
        pkgs.sort(key=lambda p: -p[1])
        rows = [(n, s, None) for n, s in pkgs[:7]]
        if len(pkgs) > 7:
            rows.append((f"…{len(pkgs) - 7} more packages", None, ""))
        total = compile_load + te
        ev = [l.strip() for l in lines if "coverage.provider=istanbul" in l][:1]
        ev += [l.strip() for l in lines if "Coverage enabled with istanbul" in l][:1]
        ev += [l.strip() for l in lines
               if re.search(r"Duration .*transform .*import .*tests", l)][:2]
        return {
            "fix_key": "vitest-v8-coverage",
            "unit_label": f"{len(pkgs)} packages ran vitest+istanbul coverage this run "
                          "(others were turbo-cached; slowest shown)",
            "evidence": ev,
            "search": [pkgs[0][0], "Coverage enabled with istanbul"],
            "magnitude": {"label": "compile+instrument share of test work",
                          "value": round(100 * compile_load / total, 2) if total else 0.0,
                          "unit": "%"},
            "deeper": [
                # Packages run concurrently (turbo), so you wait for the slowest -
                # share is of the wait (max), and each row's Duration is real wall.
                {"rows": rows, "blocker_note": "", "pct_of": "max"},
                # transform/import/tests are SUMMED across parallel workers (they
                # exceed the step's wall), so scale the displayed durations to the
                # step's wall - the bars/%, are the share of that summed work.
                {"header": "Level 4 — the step's wall split by each phase's share of "
                           "the packages' SUMMED worker time (transform/import exceed "
                           "wall because files run on parallel threads, so these "
                           "seconds are the wall apportioned by share, not measured "
                           "directly)",
                 "rows": [("transform + import (compile/instrument/load)", compile_load,
                           None),
                          ("actual test assertions", te, None)],
                 "blocker_note": "BIGGEST LEVER - caused by istanbul coverage "
                                 "instrumenting every imported file",
                 "pct_of": "sum", "scale_to_step": True},
            ],
        }

    # --- B2: vitest where IMPORT/transform dominates the tests (no coverage) ---
    # The dominant vitest invocation spends more on loading the module graph per
    # test file than on assertions - per-file isolation re-pays the import cost.
    vd = sorted(
        ((float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4)))
         for l in lines
         if (m := re.search(r"Duration +([\d.]+)s \(transform ([\d.]+)s, setup "
                            r"[\d.]+m?s, import ([\d.]+)s, tests ([\d.]+)s", l))),
        key=lambda x: -x[0])
    if vd and not istanbul:
        wall, tr, im, te = vd[0]
        if (im + tr) > te and im > 30 and te > 0:
            total = tr + im + te
            ev = [l.strip() for l in lines if re.search(r"Test Files +[0-9]+ passed", l)][:1]
            ev += [l.strip() for l in lines
                   if re.search(r"Duration .*transform .*import .*tests", l)][:2]
            return {
                "fix_key": "vitest-isolate-pool",
                "unit_label": f"the slowest vitest project ({_clock(wall)} wall) - "
                              "where that wall goes (import vs tests vs transform)",
                "evidence": ev,
                "search": ["Test Files ", "(transform "],
                "magnitude": {"label": "import share of the vitest run",
                              "value": round(100 * im / total, 2) if total else 0.0,
                              "unit": "%"},
                # import/tests/transform are SUMMED across workers (they exceed the
                # project's wall), so scale the displayed durations to that wall.
                "deeper": [
                    {"rows": [("import (load module graph per file)", im, None),
                              ("tests (run assertions)", te, None),
                              ("transform (compile)", tr, None)],
                     "blocker_note": "BIGGEST LEVER - caused by per-file isolation "
                                     "re-importing the app for each test file",
                     "pct_of": "sum", "scale_to_secs": wall},
                ],
            }

    # --- C: e2e specs run as >=2 SEPARATE `playwright test <spec>` invocations instead ---
    # of one run that could parallelize across every spec. This matches invocation lines
    # ANYWHERE in the joined log — it has NO view of the rendered step timeline and no
    # sequencing/scheduling signal, so it establishes only the SPLIT, never that the
    # invocations run serially (see `_FIX_META['playwright-parallel']`).
    # `.spec.tsx`/`.spec.jsx` are valid Playwright specs too (TS/React repos), so
    # allow the trailing `x?` — same as detector A's test/spec extension match.
    pw = re.findall(r"playwright test (\S+\.spec\.[tj]sx?)", joined)
    if len(pw) >= 2:
        ev = [l.strip() for l in lines
              if re.search(r"playwright test \S+\.spec\.[tj]sx?", l)][:4]
        # No scalar magnitude - the finding is the SPLIT (>=2 separate invocations rather
        # than one parallelizable run), not a single percentage. Whether they run serially
        # or overlap (e.g. separate `nx e2e` targets a task runner may run concurrently)
        # isn't determinable from these lines; the cause hands that to the agent. The
        # cross-run check is "every run shows >=2 separate invocations", which the
        # categorical detector already covers.
        return {"fix_key": "playwright-parallel", "unit_label": "", "deeper": [],
                "magnitude": None, "evidence": ev, "search": ["playwright test"]}

    # --- install-lifecycle-build: a build runs DURING dependency install. When a root
    # `prepare`/`postinstall` lifecycle script runs a build tool, `pnpm install` executes
    # that build as part of installing dependencies — so the install step is a build gate
    # even when the cache mostly hits. This is a DIFFERENT mechanism than a cache miss
    # (D/D2): the fix is to move the build out of install (`--ignore-scripts` + an explicit
    # cached build step), not to stabilize a cache key. Detected by a TIMED turbo build
    # inside the install step's own log section (`_log_step_sections`), so it wins the
    # framing over D/D2 only when the build genuinely runs under `<pm> install`. Placed
    # before D so a plain later-step turbo build still falls through to D/D2 unchanged.
    _sec = _install_build_section(lines)
    if _sec is not None:
        _cmd, _s, _e, _hit, _total, _secs, _cidx = _sec
        _pm = _INSTALL_STEP_RE.search(_cmd).group(1)
        _rebuilt = _total - _hit
        ev = [lines[_s].strip()]
        ev += [lines[_i].strip() for _i in range(_s, _e)
               if _LIFECYCLE_MARK_RE.search(lines[_i])][:1]
        ev += [lines[_i].strip() for _i in range(max(_s, _cidx - 1), min(_e, _cidx + 4))
               if re.match(r"\s*(Tasks|Cached|Time):\s", lines[_i])][:3]
        note = (f"{_clock(_secs)} of turbo build runs INSIDE `{_pm} install` via a "
                f"lifecycle script ({_hit}/{_total} packages cached) - build work runs "
                "during dependency install")
        return {
            "fix_key": "install-lifecycle-build",
            "unit_label": f"dependency install runs a build via a lifecycle script - "
                          f"{_clock(_secs)} of build inside `{_pm} install`",
            "magnitude": {"label": "build run by lifecycle scripts during dependency install",
                          "value": round(_secs, 1), "unit": "s"},
            "deeper": [
                {"rows": [("lifecycle build (inside install)", _secs, _clock(_secs)),
                          ("restored from cache", _hit, str(_hit)),
                          ("rebuilt packages", _rebuilt, str(_rebuilt))],
                 "blocker_note": note, "pct_of": None},
            ],
            "evidence": ev,
            "search": ["prepare", "Cached:", "--ignore-scripts"],
        }

    # --- D / D2: turbo build cache health. A job can run SEVERAL turbo invocations
    # (db:migrate, build, lint, …), each printing its own "Cached: N cached, M total"
    # followed by its own "Time: …". The invocation that GATES the step is the SLOWEST
    # one (largest `Time:`), so key off that summary, NOT the one that happens to rebuild
    # the most packages: a fast side-invocation (e.g. a separate lint pass that rebuilds
    # all of its few packages in seconds) would otherwise out-rank the dominant build,
    # overstating the miss rate and contradicting the evidence block below (which quotes
    # the slow build). Fall back to most-rebuilt only when no `Time:` line is parseable.
    # Rebuild activity counts BOTH "cache miss, executing" AND "cache bypass, force
    # executing" (turbo versions / `--force` / `cache:false` print one or the other), and
    # the run-summary is authoritative when present - some turbo versions print NO
    # per-task line at all (the langfuse case: a `cache bypass` build whose only signal is
    # the summary).
    # Each block = (hit, total, secs|None, line_idx of its `Cached:` line).
    turbo_blocks: list[tuple[int, int, float | None, int]] = []
    for _i, _l in enumerate(lines):
        _m = re.search(r"Cached:\s+(\d+) cached, (\d+) total", _l)
        if not _m:
            continue
        _secs: float | None = None
        for _nxt in lines[_i + 1:_i + 4]:               # the block's own `Time:` line
            _tm = re.search(r"\bTime:\s+(\S+)", _nxt)
            if _tm:
                _secs = _turbo_time_secs(_tm.group(1))
                break
        turbo_blocks.append((int(_m.group(1)), int(_m.group(2)), _secs, _i))
    summaries = [(h, t) for h, t, _s, _idx in turbo_blocks]
    remote_off = "Remote caching disabled" in joined
    activity = (joined.count("cache miss, executing")
                + joined.count("cache bypass, force executing"))
    chosen_idx: int | None = None
    if turbo_blocks:
        timed = [b for b in turbo_blocks if b[2] is not None]
        hit, total, _secs, chosen_idx = (
            max(timed, key=lambda b: b[2])              # the slowest (gating) invocation
            if timed else
            max(turbo_blocks, key=lambda b: b[1] - b[0]))  # fallback: most rebuilt
    else:
        hit, total = 0, activity
    rebuilt = total - hit

    def _turbo_ev() -> list[str]:
        ev = [l.strip() for l in lines if "Remote caching disabled" in l][:1]
        ev += [l.strip() for l in lines if "cache miss, executing" in l
               or "cache bypass, force executing" in l][:3]
        # Quote the GATING invocation's OWN summary block (the one the magnitude is
        # computed from), not the first summary in the job - else the evidence can cite a
        # different turbo run than the bar above it (the in-report contradiction).
        if chosen_idx is not None:
            ev += [l.strip() for l in lines[max(0, chosen_idx - 1):chosen_idx + 4]
                   if re.match(r"\s*(Tasks|Cached|Time):\s", l)][:3]
        else:
            ev += [l.strip() for l in lines
                   if re.match(r"\s*(Tasks|Cached|Time):\s", l)][:3]
        return ev

    # D: COLD - nothing restored (`hit == 0`) and a real rebuild happened. Fires when
    # remote caching is disabled (no restore possible) OR there are >5 explicit rebuild
    # lines. Summary-driven, so a turbo build that prints "cache bypass, force
    # executing" instead of per-task "cache miss" lines is still caught.
    if hit == 0 and rebuilt >= 2 and (remote_off or activity > 5):
        note = f"{hit}/{total} cached" + (" · Remote caching DISABLED" if remote_off
                                          else "") + " - BIGGEST LEVER"
        return {
            "fix_key": "turbo-remote-cache",
            "unit_label": f"turbo builds {total} packages - cache status",
            "magnitude": {"label": "packages rebuilt from scratch (cache-miss)",
                          "value": round(100 * rebuilt / total, 2) if total else 0.0,
                          "unit": "%"},
            "deeper": [
                {"rows": [("rebuilt (cache miss)", rebuilt, str(rebuilt)),
                          ("restored from cache", hit, str(hit))],
                 "blocker_note": note, "pct_of": "sum"},
            ],
            "evidence": _turbo_ev(),
            "search": ["cache miss, executing", "cache bypass, force executing", "Cached:"],
        }
    # D2: PARTIAL - caching is ON and some packages hit, but >= _PARTIAL_MISS_FLOOR_PCT
    # rebuild every run = an UNSTABLE cache key (a lockfile hash, timestamp, or per-run
    # env var in the key invalidating packages that didn't change). Hedged: some misses
    # are legitimately-changed packages (the constraint + cross-run check say so).
    if summaries and hit > 0 and rebuilt > 0:
        miss_pct = (100 * rebuilt / total) if total else 0.0
        if miss_pct >= _PARTIAL_MISS_FLOOR_PCT:
            note = (f"{hit}/{total} cached - {miss_pct:.0f}% rebuilt despite caching ON "
                    "(cache-key churn?) - BIGGEST LEVER")
            return {
                "fix_key": "turbo-partial-cache",
                "unit_label": f"turbo builds {total} packages - {hit} cached, "
                              f"{rebuilt} rebuilt",
                "magnitude": {"label": "packages rebuilt despite caching ON (cache-miss)",
                              "value": round(miss_pct, 2), "unit": "%"},
                "deeper": [
                    {"rows": [("rebuilt (cache miss)", rebuilt, str(rebuilt)),
                              ("restored from cache", hit, str(hit))],
                     "blocker_note": note, "pct_of": "sum"},
                ],
                "evidence": _turbo_ev(),
                "search": ["cache miss, executing", "Cached:"],
            }

    # --- E: docker buildx build with NO persistent BuildKit cache RESTORE ------
    # A `docker/build-push-action` / `docker buildx build` step that recompiles every
    # run because nothing is RESTORED: the command imports no cache (no `--cache-from`
    # and no `cache-from:` action input), so even if it exports one (`--cache-to`), the
    # next run reads nothing back - the BuildKit layer cache and any in-Dockerfile
    # `RUN --mount=type=cache` survive only for the life of the build. (Keying on the
    # import path, not "any cache flag", is deliberate: an export-only config is the
    # textbook cold-rebuild bug - it writes a cache it never reads.) BuildKit prints one
    # `#NN [stage] RUN|FROM|COPY|ADD …` header per layer + a matching `#NN DONE <secs>s`;
    # a layer served from cache prints `#NN CACHED` INSTEAD of doing the work. So a build
    # with zero `CACHED` lines and a real wall is provably cold. The load-bearing
    # magnitude is the slowest cold layer's share of the cold-build wall - the single
    # biggest chunk a warm cache would skip.
    buildx_cmd = bool(re.search(r"docker buildx build\b", joined)
                      or re.search(r"build-push-action", joined))
    cache_restore = bool(re.search(r"--cache-from\b", joined)
                         or re.search(r"^\s*cache-from:\s*\S", joined, re.MULTILINE))
    cache_export = bool(re.search(r"--cache-to\b", joined)
                        or re.search(r"^\s*cache-to:\s*\S", joined, re.MULTILINE))
    if buildx_cmd and not cache_restore:
        # Pair each BuildKit layer's `#NN DONE <secs>s` with its `#NN [stage] RUN|…`
        # header, so a layer's wall can be attributed to the work it ran. `cache-binary`
        # (the `docker/setup-buildx-action` default) caches the buildx BINARY across runs,
        # NOT the build layers - it is not a `--cache-from` import, so it doesn't disqualify.
        # `[\d.]+` can capture a malformed run (`1.2.3s` from an interleaved/corrupt line);
        # skip the unparseable layer (treat as un-timed) rather than let one bad line raise
        # ValueError and abort the whole report's rendering.
        done_s: dict[str, float] = {}
        for n, s in re.findall(r"#(\d+) DONE ([\d.]+)s", joined):
            try:
                done_s[n] = float(s)
            except ValueError:
                continue
        # RUN/FROM/COPY/ADD all produce CACHEABLE layers (a warm `--cache-from` skips
        # them as `CACHED`) - so an expensive `COPY` of a big context or `COPY --from`
        # artifact counts toward the cold wall too. `[internal] load …` steps (context
        # transfer, metadata) are BuildKit overhead, not cacheable layers, and stay out.
        work_headers = re.findall(
            r"#(\d+) \[([^\]]+)\] (RUN|FROM|COPY|ADD) (.*)", joined)
        work_steps = [(done_s[n], stage.strip(), kind, n)
                      for n, stage, kind, _rest in work_headers if done_s.get(n)]
        # A work-layer header with no matching `#NN DONE` line is an UNPAIRED layer -
        # a truncated/mangled log (job timeout, ANSI redraw eating a line). It drops out
        # of the cold wall, so the wall is an UNDERCOUNT; surface that rather than let a
        # heavy build silently fall below the floor or carry a wrong magnitude.
        unpaired = sum(1 for n, _stage, _kind, _rest in work_headers if not done_s.get(n))
        # Count ONLY genuine BuildKit cached-layer lines (`#NN CACHED`), anchored to the
        # same `#\d+` layer grammar as the DONE/header signals. A bare `joined.count(" CACHED")`
        # would also match log chatter - a `RUN … echo CACHED`, a test printing `Status: CACHED`,
        # a filename - and a single stray token would zero out a provably-cold build, silently
        # suppressing the finding (a false "clean"). Keep the count on the layer grammar.
        cached = len(re.findall(r"^\s*#\d+ CACHED\b", joined, re.MULTILINE))
        # Denominator is the COLD-LAYER wall (the summed cacheable-layer work a warm cache
        # would skip), used for the floor, the magnitude, AND the drill bars - one number
        # everywhere, so the headline % and the bars can never disagree.
        cold_wall = sum(secs for secs, _stage, _kind, _n in work_steps)
        if (cached == 0 and len(work_steps) >= _BUILDX_MIN_WORK_STEPS
                and cold_wall >= _BUILDX_COLD_WALL_FLOOR_S):
            work_steps.sort(key=lambda w: -w[0])
            slow_secs, slow_stage, _slow_kind, slow_n = work_steps[0]
            # ALWAYS quote the slowest cold layer's header + DONE - the single pair the
            # BIGGEST-LEVER magnitude (slow_secs / cold_wall) is computed from - adjacent
            # and verbatim, so the evidence substantiates the headline %. A blind
            # "first-2 headers + last-3 DONE" window can structurally exclude the slowest
            # layer (its header isn't in the first two, its DONE isn't in the last three)
            # and juxtapose an unrelated header against an unrelated DONE, inviting a false
            # reading. Anchor on the layer number with a trailing space so `#7` can't match
            # `#70`.
            ev = [l.strip() for l in lines
                  if re.search(r"docker buildx build\b", l)][:1]
            ev += [l.strip() for l in lines
                   if re.search(rf"#{slow_n} \[[^\]]+\] (RUN|FROM|COPY|ADD) ", l)][:1]
            ev += [l.strip() for l in lines
                   if re.search(rf"#{slow_n} DONE [\d.]+s", l)][:1]
            # A couple more DONE lines for surrounding context, skipping any already shown
            # so the slowest layer's pair stays unduplicated.
            ev += [l.strip() for l in lines
                   if re.search(r"#\d+ DONE [\d.]+s", l) and l.strip() not in ev][-2:]
            if unpaired:
                ev.append(f"⚠ {unpaired} build layer(s) had no DONE line (truncated/"
                          "mangled log) - cold wall may be undercounted")
            rows = [(f"{stage}  ({kind.lower()})", secs, None)
                    for secs, stage, kind, _n in work_steps[:6]]
            if len(work_steps) > 6:
                # Fold the un-shown cold layers into ONE valued row so the `pct_of: "sum"`
                # bars still total the cold wall. A None-valued tail would drop out of the
                # denominator and inflate every shown share (disagreeing with magnitude).
                tail = sum(secs for secs, _s, _k, _n in work_steps[6:])
                rows.append((f"…{len(work_steps) - 6} more cold layers", tail, None))
            return {
                "fix_key": "buildx-no-cache",
                "unit_label": f"docker buildx rebuilds {len(work_steps)} layers from "
                              "scratch every run "
                              + ("- cache is EXPORTED (`--cache-to`) but never imported "
                                 "(`--cache-from`), so nothing restores"
                                 if cache_export else
                                 "(no persistent cache - all cold)"),
                "magnitude": {"label": "slowest cold layer's share of the cold-build wall",
                              "value": round(100 * slow_secs / cold_wall, 2)
                              if cold_wall else 0.0, "unit": "%"},
                "deeper": [
                    # The layers build in SEQUENCE (BuildKit DAG, but each gates the
                    # next here), so the share is of the summed cold wall - the biggest
                    # bar is the biggest cold chunk a warm cache would skip or shrink.
                    {"rows": rows,
                     "blocker_note": f"{slow_stage} is {round(100 * slow_secs / cold_wall)}% "
                                     "of the cold build - BIGGEST LEVER",
                     "pct_of": "sum"},
                ],
                "evidence": ev,
                "search": ["docker buildx build", "DONE", "--mount=type=cache"],
            }

    # --- F: serial pytest with pytest-xdist INSTALLED but not switched on -------
    # pytest prints its loaded plugins (`plugins: …, xdist-2.5.0, …`), the collected
    # count (`collected N items`), and a summary (`N passed … in WALLs`). When xdist is
    # in that banner but the run is still serial, every independent test ran one-by-one
    # on a single worker while the dependency to parallelise was already installed — the
    # textbook "switch it on" win. Keyed on installed-but-unused (not "any slow pytest")
    # so the fix is unambiguous: add `-n auto`, the dep is already there.
    #
    # "xdist is actually running" is proven by its OWN output: the startup line
    # `N workers [M items]` / `created: N/M workers`, per-test worker tags `[gw0]`/
    # `[gw1]` (these appear under `-v`), or an explicit `-n`/`--numprocesses` ON the
    # pytest command line. Their ABSENCE (with the banner present) is provably serial.
    # The startup line is the authoritative signal — xdist prints it whenever it runs,
    # verbose or not; the `[gw0]` tags cover the verbose case and the `-n`-on-a-pytest-
    # line check is a belt-and-suspenders that won't fire on a stray `-n` elsewhere in
    # the log (e.g. `echo -n`) — keeping a real serial finding from being silently
    # suppressed.
    xdist_installed = bool(re.search(r"\bxdist-\d", joined)
                           or re.search(r"\bpytest-xdist\b", joined))
    xdist_active = bool(
        re.search(r"\[gw\d+\]", joined)
        or re.search(r"\bcreated:\s*\d+/\d+\s+workers?\b", joined)
        or re.search(r"(?m)^\s*\d+\s+workers?\s+\[\d+\s+item", joined)
        or re.search(r"(?m)^.*\bpytest\b.*\s-n[ =]?(?:auto|\d+)\b", joined)
        or re.search(r"(?m)^.*\bpytest\b.*--numprocesses\b", joined))
    if xdist_installed and not xdist_active:
        m_items = re.search(r"collected (\d+) items?", joined)
        m_sum = re.search(r"(\d+) passed[^\n]*? in ([\d.]+)s", joined)
        if m_items and m_sum:
            n_items, wall = int(m_items.group(1)), float(m_sum.group(2))
            if n_items >= _PYTEST_MIN_ITEMS and wall >= _PYTEST_SERIAL_WALL_FLOOR_S:
                ev = [l.strip() for l in lines if re.search(r"\bxdist-\d", l)][:1]
                ev += [l.strip() for l in lines
                       if re.search(r"collected \d+ items?", l)][:1]
                ev += [l.strip() for l in lines
                       if re.search(r"\d+ passed[^\n]*? in [\d.]+s", l)][:1]
                # Flag-style like playwright-parallel: the finding is the SERIALITY (one
                # worker, xdist off), not a single percentage — no scalar magnitude / drill.
                return {"fix_key": "pytest-no-xdist", "unit_label": "", "deeper": [],
                        "magnitude": None, "evidence": ev,
                        "search": ["xdist", "collected", "passed", "-n auto"]}

    # --- G: eslint run over the whole tree with NO persisted cache (`--cache`) ----
    # The dominant Lint pole shells to an `eslint` invocation that omits `--cache`, so
    # eslint re-analyses the entire file tree every run with nothing carried between runs
    # — the textbook "tool invoked without a cache flag" miss (the same shape as
    # buildx-no-cache, but for the linter). `--cache` is an ESLint CLI-only flag (there is
    # no config-file equivalent), so its presence/absence on the invocation line is the
    # authoritative signal. eslint prints no duration of its own (the wall is only in the
    # stripped GHA timestamps), so this is a flag-style finding like playwright-parallel /
    # pytest-no-xdist: no scalar magnitude, the step wall comes from the router's context.
    #
    # `_ESLINT_CMD` matches eslint at a COMMAND position only (see its definition). group(1)
    # is the invocation's flags, tested for the `--cache` ENABLE flag. Only the boolean
    # `--cache` turns caching on; `--cache-location`/`--cache-strategy` alone do NOT (ESLint
    # ignores a cache location with caching off), so `--cache(?![\w-])` requires `--cache` to
    # stand on its own — a tail with only `--cache-location` is still a cache miss and fires.
    # The decision is PER-INVOCATION: a repo that caches one eslint run but not another still
    # has a real miss, so we fire on (and quote as evidence) the UNCACHED invocations — never
    # let one cached sibling silence a genuine miss (a false "clean"). An invocation whose tail
    # ends in a line-continuation `\` is skipped: the `--cache` flag may live on a wrapped line
    # the single-line match can't see, so its cache state is unknown — fall through rather than
    # assert a miss.
    def _cached(tail: str) -> bool:
        return bool(re.search(r"--cache(?![\w-])", tail))
    hits = [(l, m.group(1)) for l in lines if (m := _ESLINT_CMD.search(l))]
    uncached = [l for l, tail in hits
                if not _cached(tail) and not tail.rstrip().endswith("\\")]
    if uncached:
        return {"fix_key": "eslint-no-cache", "unit_label": "", "deeper": [],
                "magnitude": None, "evidence": [l.strip() for l in uncached[:2]],
                "search": ["eslint", "--cache", ".eslintcache"]}

    # --- H: long serial `cargo test` suite where a few test binaries dominate -----
    # The standard Rust libtest harness (`cargo test`, NOT cargo-nextest) runs each test
    # BINARY one after another and prints, per binary, a `Running <name> (target/…/deps/
    # <name>-<hash>)` header followed by `test result: ok. N passed; … finished in
    # <secs>s`. `--jobs`/`-j` only parallelises the BUILD and `--test-threads` only raises
    # in-binary thread count — across binaries the run is serial, so on a single job the
    # wall is the SUM of the per-binary `finished in` times and a few heavy binaries
    # dominate. Keyed on the libtest result-line shape: cargo-nextest prints a different
    # summary, so a repo already partitioning with nextest won't match (its output isn't
    # this format). The load-bearing magnitude is the slowest binary's share of that
    # serial test wall — the biggest single chunk that sharding across runners (or
    # splitting the heavy binary) would move off the critical path. Fires only when the
    # suite is genuinely large (enough binaries) AND the serial wall is worth sharding
    # (floor), so a small crate's quick test run stays clean.
    cargo_bins: list[tuple[str, float]] = []
    cur_bin: str | None = None
    for l in lines:
        if (mh := re.search(r"^\s*Running\s+(.+?)\s+\(target/", l)):
            cur_bin = mh.group(1).strip()
        elif (md := re.search(r"^\s*Doc-tests\s+(\S+)", l)):
            cur_bin = f"Doc-tests {md.group(1)}"
        elif (mt := re.search(r"test result: ok\.\s+\d+ passed;.*?finished in "
                              r"([\d.]+)s", l)):
            secs = float(mt.group(1))
            if secs > 0:  # the `0 passed … 0.00s` empty-binary line is not real work
                cargo_bins.append((cur_bin or "?", secs))
            cur_bin = None
    if len(cargo_bins) >= _CARGO_MIN_BINARIES:
        cargo_total = sum(s for _n, s in cargo_bins)
        if cargo_total >= _CARGO_SERIAL_WALL_FLOOR_S:
            cargo_bins.sort(key=lambda b: -b[1])
            slow_name, slow_secs = cargo_bins[0]
            rows = [(n, s, None) for n, s in cargo_bins[:6]]
            if len(cargo_bins) > 6:
                # Fold the un-shown binaries into ONE valued row so the `pct_of: "sum"`
                # bars still total the serial wall (a None tail would drop out of the
                # denominator and inflate every shown share, disagreeing with magnitude).
                tail = sum(s for _n, s in cargo_bins[6:])
                rows.append((f"…{len(cargo_bins) - 6} more test binaries", tail, None))
            ev = [l.strip() for l in lines
                  if re.search(r"^\s*Running\s+.+\(target/", l)][:1]
            ev += [l.strip() for l in lines
                   if re.search(r"test result: ok\..*finished in [\d.]+s", l)][:2]
            return {
                "fix_key": "cargo-test-shard",
                "unit_label": f"{len(cargo_bins)} `cargo test` binaries run serially in "
                              "one job (slowest first)",
                "magnitude": {"label": "slowest test binary's share of the serial test wall",
                              "value": round(100 * slow_secs / cargo_total, 2)
                              if cargo_total else 0.0, "unit": "%"},
                "deeper": [
                    # The binaries run in SEQUENCE (libtest runs them one at a time), so
                    # the share is of the summed serial wall - the biggest bar is the
                    # biggest chunk sharding across runners would move off the critical path.
                    {"rows": rows,
                     "blocker_note": f"{_lbl(slow_name)} is "
                                     f"{round(100 * slow_secs / cargo_total)}% of the "
                                     "serial test wall - BIGGEST LEVER",
                     "pct_of": "sum"},
                ],
                "evidence": ev,
                "search": ["test result: ok.", "finished in", "Running"],
            }

    # --- I: a benchmark/perf step that reruns its whole op suite N times serially --
    # A benchmark harness invoked with a repeated-iterations flag (`--runs N`, plus the
    # common synonyms `--iterations`/`--samples`/`--repeat`) executes EVERY query/op in
    # its suite N times in sequence to get a stable median. When N is large the warm
    # reruns dominate the step: the suite already runs once, and the extra N-1 passes are
    # pure repetition. Keyed on a benchmark indicator (`bench`/`benchmark`) AND the runs
    # flag on the SAME invocation line, so a stray `--runs` on an unrelated command can't
    # fire. The load-bearing magnitude is N itself — the serial-rerun multiplier the owner
    # can dial down (or parallelise across runners). Hedged in the hand-off: the reruns
    # exist to produce stable medians for a regression gate, so dropping N is a
    # measurement-coverage decision, not a free win. Fires only above a floor, so a 1-3
    # sample smoke benchmark stays clean.
    bench_runs = [int(m.group(1)) for l in lines
                  if re.search(r"\bbench", l, re.IGNORECASE)
                  and (m := re.search(r"--(?:runs|iterations|samples|repeat)[ =](\d+)", l))]
    if bench_runs:
        n_runs = max(bench_runs)
        if n_runs >= _BENCH_RERUNS_FLOOR:
            ev = [l.strip() for l in lines
                  if re.search(r"\bbench", l, re.IGNORECASE)
                  and re.search(r"--(?:runs|iterations|samples|repeat)[ =]\d+", l)][:2]
            return {
                "fix_key": "benchmark-serial-reruns",
                "unit_label": f"the benchmark reruns its whole op suite {n_runs}x in "
                              "sequence (one cold run + warm reruns per op)",
                "magnitude": {"label": "serial reruns of every benchmarked op (--runs)",
                              "value": n_runs, "unit": "x"},
                "deeper": [],
                "evidence": ev,
                "search": ["--runs", "benchmark"],
            }

    # --- J: Android instrumentation suite run serially on a SINGLE emulator --------
    # `./gradlew connectedCheck` / `connectedAndroidTest` assembles, installs, and runs each
    # module's androidTest variant ONE AT A TIME on a single emulator: Gradle prints
    # `> Task :<module>:connected[<Variant>]AndroidTest` per module, so the count of those
    # per-module task lines is the suite's serial fan-in width. With no device-level
    # parallelism (an android-emulator-runner job matrix / Gradle Managed Devices) the
    # per-module work all lands on one device and gates the merge. Keyed on the PER-MODULE
    # task shape (the umbrella `connectedCheck`/`connectedAndroidTest` aggregate ends in
    # `Check`, not `AndroidTest`, and a module's own `connectedAndroidTest` aggregate is
    # deduped by module). A flag-style finding like playwright-parallel: the cost is the
    # serialization, not a single percentage. Fires only when there are several modules AND
    # the Gradle build wall is worth the per-shard AVD-boot overhead, so a single-module
    # quick run stays clean. Gradle build wall is reused by detector K below.
    android_modules = sorted({
        mm.group(1) for l in lines
        if (mm := re.match(r"> Task (:[\w.\-]+(?::[\w.\-]+)*):connected\w*AndroidTest\b", l))})
    gradle_wall = _gradle_build_secs(joined)
    if (len(android_modules) >= _ANDROID_MIN_MODULES and gradle_wall
            and gradle_wall >= _ANDROID_CONNECTED_WALL_FLOOR_S):
        ev = [l.strip() for l in lines if re.search(r"gradlew\b.*connected", l)][:1]
        ev += [l.strip() for l in lines
               if re.match(r"> Task :[\w.\-]+(?::[\w.\-]+)*:connected\w*AndroidTest\b", l)][:2]
        ev += [l.strip() for l in lines if "BUILD SUCCESSFUL in" in l][:1]
        return {
            "fix_key": "android-emulator-shard",
            "unit_label": f"{len(android_modules)} modules run instrumentation tests "
                          "serially on one emulator (no device-level parallelism)",
            "magnitude": None,
            "deeper": [],
            "evidence": ev,
            "search": ["connectedCheck", "connectedAndroidTest", "BUILD SUCCESSFUL in"],
        }

    # --- K: a single un-sharded JVM Gradle `:test` task gates the build ------------
    # `./gradlew <module>:test` runs a module's whole JVM test suite as ONE serial Gradle
    # task (Gradle prints `> Task :<module>:test`, or `:test<Variant>UnitTest` for Android
    # unit tests); with no `maxParallelForks`/`forkEvery` (in-task fork parallelism) and no
    # split across CI jobs, a heavy suite (e.g. TestKit-style plugin integration tests that
    # each spin up a real Gradle build) runs start to finish in that one task and gates the
    # merge. Distinct from detector J - those are on-emulator `connectedAndroidTest`
    # instrumentation tasks; this is the JVM `:test` task, so it fires only when there are NO
    # instrumentation tasks (`not android_modules`). Gated on the Gradle build wall so a
    # quick unit-test build stays clean. Flag-style: the finding is the serial, un-sharded
    # task, not a single percentage.
    jvm_test_modules = sorted({
        mm.group(1) for l in lines
        if (mm := re.match(r"> Task (:[\w.\-]+(?::[\w.\-]+)*):test(?:\w*UnitTest)?\b", l))})
    if (jvm_test_modules and not android_modules and gradle_wall
            and gradle_wall >= _GRADLE_TEST_WALL_FLOOR_S):
        ev = [l.strip() for l in lines if re.search(r"gradlew\b.*:test\b", l)][:1]
        ev += [l.strip() for l in lines
               if re.match(r"> Task :[\w.\-]+(?::[\w.\-]+)*:test(?:\w*UnitTest)?\b", l)][:2]
        ev += [l.strip() for l in lines if "BUILD SUCCESSFUL in" in l][:1]
        return {
            "fix_key": "gradle-test-parallelism",
            "unit_label": "the JVM test suite runs as serial, un-sharded Gradle `:test` "
                          "task(s) (no fork/shard parallelism)",
            "magnitude": None,
            "deeper": [],
            "evidence": ev,
            "search": [":test", "BUILD SUCCESSFUL in", "maxParallelForks"],
        }
    return None


# --------------------------------------------------------------------------- #
# Agent prompts — starslingdev/ci-speedup does NOT prescribe the fix. Each pole's
# prompt is BUILT from the measured context (gate, drill, cause, addressable
# ceiling, evidence, run link) plus a per-cause static block (cause/look/
# constraints/docs/deliver), so the agent gets the full context to design a fix
# from the prompt alone - without prescribing one.
# --------------------------------------------------------------------------- #

_FIX_META: dict[str, dict[str, Any]] = {
    "prisma-migrate-once": {
        "cause": "Most of each test file's wall time is repeated schema rebuilds, not "
                 "test work: the job re-applies the full Prisma schema once per test "
                 "group (typically `prisma migrate reset` or `db push --force-reset`), "
                 "which is what the per-file `Total Migration Time` measures.",
        "look": "`{wf}` (the job's steps) and the test harness that rebuilds the schema "
                "- grep the log for `Total Migration Time`/`MIGRATIONS completed` to "
                "confirm the per-group repeats, then the source for `migrate reset`, "
                "`db push`, `--force-reset`, and per-test `beforeEach`/`beforeAll` "
                "DB-reset hooks. Recover the intent from git history before changing it.",
        "constraints": "Per-test isolation must hold: tests must not see each other's "
                       "DB state. If you migrate once and reset *data* between tests "
                       "instead of rebuilding the schema, prove no cross-test leakage "
                       "(transaction rollback / truncate per test).",
        "docs": ["Prisma CLI reference (db push --force-reset): "
                 "https://www.prisma.io/docs/orm/reference/prisma-cli-reference",
                 "Prisma integration testing (resetting state between tests): "
                 "https://www.prisma.io/docs/orm/prisma-client/testing/integration-testing"],
        "deliver": "Remove the redundant per-group schema rebuilds while keeping "
                   "per-test isolation. Confirm migrations run once (or once per "
                   "suite), tests still pass in isolation, and re-measure the dominant "
                   "step to confirm the drop.",
    },
    "vitest-v8-coverage": {
        "cause": "The gating `test` job runs vitest with istanbul coverage across "
                 "every package, and the compile+instrument phase dominates the run, "
                 "not the assertions - istanbul instruments every imported file.",
        "look": "the vitest config / coverage setup and the package test scripts - "
                "grep for `coverage`, `provider: 'istanbul'`, `--coverage`. Check what "
                "actually consumes the coverage output (a PR gate? a report upload?).",
        "constraints": "If coverage must gate PRs, switching provider or scoping it "
                       "has to keep whatever consumes the output working. The v8 "
                       "provider is much cheaper than istanbul but reports slightly "
                       "differently - verify the consumer.",
        "docs": ["Vitest coverage docs (v8 vs istanbul providers): "
                 "https://vitest.dev/guide/coverage"],
        "deliver": "Decide whether coverage needs to gate PRs at all, and if so which "
                   "provider fits; apply the change to the vitest config / scripts, "
                   "verify the coverage consumer still works, and re-measure the step.",
    },
    "vitest-isolate-pool": {
        "cause": "The gating vitest run spends as much time in `import` (re-loading "
                 "the module graph per test file under per-file isolation) as in the "
                 "tests themselves.",
        "look": "the vitest config - `test.isolate`, `pool`, `poolOptions`, and "
                "whether tests rely on per-file isolation (global/DB state set up per "
                "file).",
        "constraints": "State the failure mode (cross-file state leakage) and how you "
                       "verified it's safe. Disabling isolation or sharing a pool only "
                       "works if tests clean up their own global/DB state.",
        "docs": ["Vitest performance guide (isolation and pools): "
                 "https://vitest.dev/guide/improving-performance"],
        "deliver": "Tune isolation/pool in the vitest config if the tests can safely "
                   "share context; prove no cross-file leakage and re-measure the run.",
    },
    "turbo-remote-cache": {
        "cause": "`turbo build` rebuilds every package from scratch each run - the "
                 "summary shows remote caching disabled / 0 cached, so unchanged "
                 "packages are not restored.",
        "look": "the turbo config (`turbo.json`) and the CI workflow - how `turbo` is "
                "invoked and whether CI has credentials for a shared cache (managed "
                "Remote Cache or self-hosted).",
        "constraints": "Confirm cache hits on a follow-up run and check no credentials "
                       "leak into logs. A misconfigured cache key can serve stale "
                       "artifacts - verify task outputs are correct.",
        "docs": ["Turborepo Remote Caching: "
                 "https://turborepo.dev/docs/core-concepts/remote-caching"],
        "deliver": "Give CI access to a shared remote cache so unchanged packages "
                   "restore instead of rebuilding; confirm cache hits on a second run "
                   "and re-measure.",
    },
    "turbo-partial-cache": {
        "cause": "`turbo build` caching is ON and some packages restore from cache, but "
                 "the build still rebuilds most of them every run. A high miss rate on "
                 "a CI build usually means the cache KEY is unstable - a lockfile hash, "
                 "a timestamp, or a per-run env var in the key (or in a task's "
                 "`inputs`) - invalidating packages whose source did not change, rather "
                 "than the code genuinely changing.",
        "look": "`turbo.json` - each task's `inputs`, `outputs`, and "
                "`globalDependencies` - and the CI cache step that feeds turbo's key. "
                "Grep the log for `cache miss, executing` and compare the missing "
                "packages + their hashes across two runs of the SAME PR: packages that "
                "miss every run despite unchanged source are the churn.",
        "constraints": "Some misses are legitimate - the packages a PR actually "
                       "changed MUST rebuild. Before treating misses as waste, confirm "
                       "the missing packages are unchanged across the sampled runs "
                       "(the cross-run check shows whether the miss rate is stably "
                       "high). Do not suppress rebuilds of genuinely-changed code.",
        "docs": ["Turborepo caching (what goes into the hash): "
                 "https://turborepo.dev/docs/core-concepts/caching",
                 "Turborepo Remote Caching: "
                 "https://turborepo.dev/docs/core-concepts/remote-caching"],
        "deliver": "Stabilize the cache key (pin/normalize what feeds it; scope task "
                   "`inputs` to real sources) so unchanged packages hit the cache; "
                   "confirm the miss rate drops on a second run of the same PR and "
                   "re-measure the step.",
    },
    "buildx-no-cache": {
        "cause": "The `docker buildx build` step recompiles the image from scratch every "
                 "run because nothing is RESTORED: the command imports no cache - no "
                 "`--cache-from` and no `cache-from:` action input - so even if it EXPORTS "
                 "one (`--cache-to`, written but never read back), the BuildKit layer "
                 "cache and any in-Dockerfile `RUN --mount=type=cache` mount (e.g. a "
                 "ccache/apt/pip cache) start cold each run. The log shows every cacheable "
                 "layer (RUN/FROM/COPY/ADD) running cold (no `CACHED` lines), and the "
                 "slowest layer dominates that wall.",
        "look": "`{wf}` - the `docker/build-push-action` (or raw `docker buildx build`) "
                "step and whether it sets a `cache-from` IMPORT (an export-only "
                "`cache-to` config still rebuilds cold). Grep the build log for "
                "`#`-prefixed `DONE`/`CACHED` lines to see which layers are cold, and "
                "read the Dockerfile's layer ordering + any `--mount=type=cache` mounts. "
                "Note `cache-binary: true` is the `docker/setup-buildx-action` default - "
                "it caches the buildx BINARY, NOT the build layers, so it is not a cache "
                "import.",
        "constraints": "Some cold work is legitimate - a layer whose inputs genuinely "
                       "changed MUST rebuild, and an update/bump workflow may rebuild "
                       "from scratch ON PURPOSE (to validate a clean build with no cache "
                       "contamination). Confirm that intent from git history before "
                       "adding a cache; if it's deliberate, flag it rather than silently "
                       "caching. A stale layer can also mask a real build break - key "
                       "the cache on the Dockerfile + the pinned tool/base versions and "
                       "verify outputs are correct.",
        "docs": ["Docker build cache backends (gha / registry / local): "
                 "https://docs.docker.com/build/cache/backends/",
                 "docker/build-push-action cache inputs: "
                 "https://github.com/docker/build-push-action#cache"],
        "deliver": "Persist the BuildKit layer cache (and the `--mount=type=cache` "
                   "mounts) across runs by wiring BOTH a `--cache-from` import and a "
                   "`--cache-to` export (gha / registry / local) - so unchanged layers "
                   "restore instead of rebuilding cold; if a `--cache-to` is already "
                   "present, the fix is just the missing `--cache-from`. Confirm those "
                   "layers log as `CACHED` (or the mount warm-starts) on a second run and "
                   "re-measure the step. Don't serve stale artifacts: scope the cache key "
                   "to the Dockerfile + pinned versions.",
    },
    "install-lifecycle-build": {
        "cause": "The dependency-install step runs a BUILD, because the root "
                 "`package.json` declares a `prepare` (or `postinstall`) lifecycle "
                 "script that invokes a build tool (e.g. `\"prepare\": \"turbo build\"`). "
                 "`pnpm install --frozen-lockfile` executes that script as part of "
                 "installing dependencies, so a full build runs during install, BEFORE "
                 "the explicit package checks. The install step is slow even when the "
                 "build cache mostly HITS - this is \"work runs during install\", not a "
                 "cache-key problem, so stabilizing a cache key would not fix it.",
        "look": "the root `package.json` `scripts.prepare` / `scripts.postinstall`, the "
                "install step in `{wf}`, and the turbo (or other build) summary that "
                "appears INSIDE the install step's log section. Check whether any "
                "DEPENDENCY's own `postinstall` (not the root's) is load-bearing - "
                "esbuild/sharp/puppeteer and similar rely on it to fetch/compile a "
                "native binary.",
        "constraints": "`--ignore-scripts` disables EVERY package's lifecycle scripts, "
                       "not just the root's - so packages that rely on `postinstall` to "
                       "function will break. If you use it, the root build must become an "
                       "explicit, cached CI step, and each dependency's postinstall must "
                       "be verified unnecessary or re-run selectively. NEVER benchmark "
                       "this change from a fork or a cold-cache clone - a fork cannot read "
                       "the production remote cache, so it shows a worst-case cold build, "
                       "not the warm-cache reality; measure on an upstream PR with the "
                       "cache warm.",
        "docs": ["pnpm `--ignore-scripts` / lifecycle scripts: "
                 "https://pnpm.io/cli/install#--ignore-scripts",
                 "npm scripts (prepare / postinstall): "
                 "https://docs.npmjs.com/cli/using-npm/scripts",
                 "Turborepo caching: "
                 "https://turborepo.dev/docs/core-concepts/caching"],
        "deliver": "Move the build out of dependency install (install with "
                   "`--ignore-scripts`, then run the build as an explicit, cached step) "
                   "once you've confirmed no dependency's postinstall is load-bearing; "
                   "re-measure the install step's median AND P90 across upstream PRs "
                   "(not a fork) to confirm the build cost left install.",
    },
    "playwright-parallel": {
        # State ONLY what the log the detector read establishes: >=2 SEPARATE `playwright
        # test` invocation lines, each naming a `.spec` file (the detector counts matching
        # lines and does not check the specs are distinct), in place of one run that could
        # parallelize across all specs. The detector matches invocation lines ANYWHERE in
        # the joined log — it has no view of the rendered per-step timeline and no
        # sequencing/scheduling signal — so the cause must NOT assert the invocations are
        # "sequential steps in the timeline above" (a single-step job renders one bar) or
        # that they "don't share a worker pool" (in an nx monorepo they are separate `nx
        # e2e` targets nx may run concurrently). Hand those questions to the agent instead.
        "cause": "The job's log runs two or more SEPARATE `playwright test` invocation "
                 "lines, each naming a `.spec` file, instead of a single "
                 "`playwright test` run that could parallelize across every spec. Whether "
                 "those invocations overlap (e.g. separate `nx e2e` targets a task runner "
                 "may execute concurrently) or run one after another - and whether they "
                 "share a worker pool - isn't determinable from the invocation lines "
                 "alone; confirm how they're scheduled before assuming they run serially.",
        "look": "the workflow steps that invoke `playwright test` and the Playwright "
                "config (`playwright.config.*`) - projects, `workers`, "
                "`fullyParallel`, and how specs are split across steps.",
        "constraints": "If the invocations do run serially, restructure so specs run "
                       "concurrently without dropping any project coverage or overrunning "
                       "the runner. Note CI's default worker count (often = CPU cores). If "
                       "you shard across separate CI jobs (rather than one parallel "
                       "invocation) and this step is a required status check, add the new "
                       "shard jobs to branch protection as required checks (or the ruleset "
                       "equivalent), or the split-out specs silently stop gating merges "
                       "(everything stays green) until that admin-only re-gating step is done.",
        "docs": ["Playwright parallelism: https://playwright.dev/docs/test-parallel"],
        "deliver": "Once you've confirmed the invocations run serially, run the specs in "
                   "one parallel invocation (or shard across jobs) without losing project "
                   "coverage; re-measure the e2e step.",
    },
    "pytest-no-xdist": {
        "cause": "The step runs the whole pytest suite in ONE process — the plugin "
                 "banner shows `pytest-xdist` is installed, but the invocation passes no "
                 "`-n`/`--numprocesses`, so every collected test runs one-by-one on a "
                 "single worker. The dependency for parallelism is already present and "
                 "simply isn't switched on (no `[gw0]` worker tags in the log).",
        "look": "`{wf}` — the step's `pytest` command (does it pass `-n auto`/`-n <N>`?), "
                "the plugin banner line (`xdist-<ver>` confirms the dep is installed), and "
                "`pytest.ini`/`pyproject.toml`/`setup.cfg` `addopts` (a project-wide `-n` "
                "may be set but overridden here). Check whether the tests are STATEFUL "
                "(bind fixed ports, share one DB / server / temp dir, depend on ordering) "
                "— the usual reason xdist is installed but left off.",
        "constraints": "These may be integration tests kept serial ON PURPOSE: parallel "
                       "workers can collide on fixed ports or a shared sqlite/Postgres DB, "
                       "or interleave stdout under `-s`. Recover the intent from git "
                       "history before flipping it on. Before adding `-n auto`, prove "
                       "per-worker isolation (a unique port/DB/tmp per `gw` worker); if "
                       "coverage is collected (`--cov`), merge it across workers "
                       "(`coverage combine`) or the report goes partial. If in-process "
                       "isolation isn't feasible, shard by directory across matrix jobs "
                       "instead — but if you take that split-across-jobs path and this step "
                       "is a required status check, the new matrix jobs must be added to "
                       "branch protection as required checks (or the ruleset equivalent), or "
                       "the split-out tests silently stop gating merges (everything stays "
                       "green) until that admin-only re-gating step is done.",
        "docs": ["pytest-xdist — distributing tests: "
                 "https://pytest-xdist.readthedocs.io/en/stable/distribution.html",
                 "pytest-xdist — known limitations (shared state): "
                 "https://pytest-xdist.readthedocs.io/en/stable/known-limitations.html"],
        "deliver": "Run the suite across workers — add `-n auto` (xdist is already a dep) "
                   "once per-worker isolation is proven, or shard the suite across matrix "
                   "jobs by directory. Keep the full test count and (if present) a "
                   "complete merged coverage report; re-measure the step.",
    },
    "eslint-no-cache": {
        "cause": "The lint step runs `eslint` over the whole file tree with NO `--cache` "
                 "flag on the invocation, so eslint re-analyses every file every run and "
                 "persists nothing between runs — the full lint cost is paid cold each "
                 "time. `--cache` is an ESLint CLI-only flag (there is no config-file "
                 "equivalent), so its absence on the command line is the whole signal.",
        "look": "`{wf}` — the lint step's `eslint` command and the `lint` script in "
                "`package.json` (the `$ eslint` echo shows the flags actually passed). "
                "Confirm `--cache` is genuinely absent, then check git history for whether "
                "it was dropped ON PURPOSE — some teams omit it to avoid a stale cache "
                "masking a newly-introduced rule violation. Note any other whole-graph "
                "pass in the same job (e.g. `knip`, `madge`) is inherently global and "
                "less reducible than the per-file lint.",
        "constraints": "A persisted `.eslintcache` can mask a real lint failure if it "
                       "isn't invalidated when the rules change: a warm entry is reused "
                       "for an unchanged file even after an eslint/plugin/config bump that "
                       "would now flag it. Key the CI cache on the lockfile + eslint config "
                       "+ plugin versions (with a `restore-keys` fallback) so a rule change "
                       "busts it, and keep `--cache-strategy content` if mtimes are "
                       "unreliable in CI. Don't weaken coverage: every file eslint lints "
                       "today must still be linted.",
        "docs": ["ESLint CLI — caching (`--cache`, `--cache-location`, "
                 "`--cache-strategy`): https://eslint.org/docs/latest/use/command-line-interface#caching",
                 "actions/cache — persisting a directory across runs: "
                 "https://github.com/actions/cache"],
        "deliver": "Enable `--cache` with a stable `--cache-location` and persist the "
                   "`.eslintcache` across CI runs (actions/cache keyed to bust on "
                   "dependency/eslint-config changes), so warm runs only re-lint changed "
                   "files. Verify a deliberately-introduced lint error still fails CI (the "
                   "cache must not hide it) and re-measure the lint step.",
    },
    "cargo-test-shard": {
        "cause": "The job runs the whole test suite through the standard `cargo test` "
                 "(libtest) harness in ONE job, and the harness runs each test BINARY one "
                 "after another - `--jobs`/`-j` only parallelises the build and "
                 "`--test-threads` only raises in-binary thread count, so across binaries "
                 "the run is serial. The step's wall is the SUM of the per-binary "
                 "`finished in` times and a few heavy binaries dominate it.",
        "look": "`{wf}` - the test step's `cargo test` command (is it already split by "
                "`--package`/`--test`, and could it move to `cargo nextest` "
                "partitioning?) and the project's test layout. Grep the log for "
                "`test result: ok. … finished in <secs>s` to see which binaries are heavy, "
                "and check whether the tests share one external resource (a single DB / "
                "server / fixed port) - the usual reason the suite is kept serial.",
        "constraints": "Tests sharded across runners must stay ISOLATED: if the heavy "
                       "binaries share one stateful backend (a single Postgres/DB, a "
                       "fixed port, a temp dir), each shard needs its OWN fresh instance "
                       "or they'll bleed state and flake (or false-pass). Recover why the "
                       "suite is serial from git history before changing it, keep the full "
                       "test count, and roll the sharded layout out alongside the existing "
                       "job until pass/fail parity is confirmed across several runs. "
                       "Re-establish gating: if this job is a required status check, the "
                       "new shard jobs must be added to branch protection as required "
                       "checks (or the ruleset equivalent), or the split-out tests silently "
                       "stop gating merges — everything stays green while the gate no longer "
                       "runs them. The fix isn't complete until the new jobs gate the merge, "
                       "usually an admin-only step.",
        "docs": ["cargo-nextest - partitioning tests across CI machines: "
                 "https://nexte.st/docs/ci-features/partitioning/",
                 "cargo test (libtest harness options): "
                 "https://doc.rust-lang.org/cargo/commands/cargo-test.html"],
        "deliver": "Shard the suite across parallel jobs (cargo-nextest `--partition`, or "
                   "split by `--package`/`--test` so the heavy binaries land on different "
                   "runners), giving each shard an isolated backend; keep a full-suite "
                   "fallback, confirm pass/fail parity with the single-job run, and "
                   "re-measure the step.",
    },
    "benchmark-serial-reruns": {
        "cause": "The benchmark step reruns its ENTIRE operation suite N times in "
                 "sequence (a `--runs N`-style flag) to get a stable median per op. The "
                 "suite already runs once; the extra N-1 warm passes are repetition, and "
                 "with a large suite and a high N they dominate the step's wall.",
        "look": "`{wf}` and the benchmark harness - the `--runs`/`--iterations` value and, "
                "CRITICALLY, what consumes the results: how many samples the regression "
                "gate's statistics actually need, and whether this job even gates merge (a "
                "label-gated / opt-in benchmark is a throughput/cost lever, not developer "
                "merge-wait). Recover the chosen N from git history before changing it.",
        "constraints": "The reruns exist to produce stable medians - dropping N or "
                       "skipping warm passes REDUCES measurement coverage and can let a "
                       "real perf regression slip through. This is a measurement-policy "
                       "change needing the owner's sign-off, not a free win: keep a "
                       "full-sample fallback and, before lowering N, confirm the gate's "
                       "medians still match (run old and new side by side). If you take the "
                       "parallelise-across-runners path (new jobs, not one serial pass) and "
                       "this benchmark step is a required status check, add the new jobs to "
                       "branch protection as required checks (or the ruleset equivalent), or "
                       "the split-out benchmark work silently stops gating merges (everything "
                       "stays green) until that admin-only re-gating step is done.",
        "docs": ["github-action-benchmark - tracking benchmark results / catching "
                 "regressions: https://github.com/benchmark-action/github-action-benchmark"],
        "deliver": "Cut the warm-rerun cost without weakening the regression signal: lower "
                   "N only to what the gate's statistics need, and/or parallelise the suite "
                   "(independent ops / dataset tiers across runners) instead of one serial "
                   "pass. Verify the medians match the old layout and re-measure the step.",
    },
    "android-emulator-shard": {
        "cause": "The merge-gating step runs the WHOLE multi-module Android instrumentation "
                 "suite (`./gradlew connectedCheck` / `connectedAndroidTest`) on a SINGLE "
                 "emulator: Gradle prints one `> Task :<module>:connectedAndroidTest` per "
                 "module and assembles, installs, and runs each module's androidTest variant "
                 "one after another on that one device. With no device-level parallelism the "
                 "per-module work all fans in to one emulator and gates the merge; the "
                 "on-device run per module is usually small, so the cost is the SERIALIZATION "
                 "across modules plus a one-time AVD boot, not test volume.",
        "look": "`{wf}` - the emulator step's gradle command and the android-emulator-runner "
                "config (is there a job matrix / Gradle Managed Devices, or just one AVD?). "
                "Grep the log for `> Task :<module>:connectedAndroidTest` to see the module "
                "fan-in width and `BUILD SUCCESSFUL in <wall>` for the suite wall, and check "
                "whether the modules' tests share device state (one app/account, fixed "
                "on-device paths). Recover from git history why it's one emulator before "
                "changing it.",
        "constraints": "Instrumentation tests sharded across emulators must stay ISOLATED: "
                       "tests that assume one device's state (shared app data, a fixed "
                       "account, on-device files) bleed across shards and flake or "
                       "false-pass. Each shard needs its OWN fresh AVD/snapshot, the "
                       "per-shard AVD-boot overhead must be paid back by the parallelism, and "
                       "the full module/test count must be preserved. Roll the sharded matrix "
                       "out alongside the existing single-emulator job until pass/fail parity "
                       "holds across several runs. Re-establish gating: if this job is a "
                       "required status check, the new emulator-shard jobs must be added to "
                       "branch protection as required checks (or the ruleset equivalent), or "
                       "the split-out tests silently stop gating merges — everything stays "
                       "green while the gate no longer runs them; that admin-only step is part "
                       "of the fix.",
        "docs": ["reactivecircus/android-emulator-runner - matrix runs + AVD snapshot "
                 "caching: https://github.com/ReactiveCircus/android-emulator-runner",
                 "Gradle-managed devices for instrumented tests: "
                 "https://developer.android.com/studio/test/gradle-managed-devices"],
        "deliver": "Spread the per-module instrumentation suites across parallel emulators "
                   "(an android-emulator-runner job matrix or Gradle Managed Devices), giving "
                   "each shard a fresh device; keep a full single-emulator fallback, confirm "
                   "pass/fail parity with the single-device run, and re-measure the step.",
    },
    "gradle-test-parallelism": {
        "cause": "The merge-gating step runs a module's whole JVM test suite as ONE serial "
                 "Gradle `:test` task (`> Task :<module>:test`, or `:test<Variant>UnitTest` "
                 "for Android unit tests). With no in-task fork parallelism "
                 "(`maxParallelForks`/`forkEvery`) and no split across CI jobs, a heavy suite "
                 "- e.g. TestKit-style plugin integration tests that each spin up a real "
                 "Gradle build - runs start to finish in that one task; the task's wall is "
                 "essentially the whole suite run serially, and it gates the merge.",
        "look": "`{wf}` and the module's `build.gradle(.kts)` test config - is "
                "`maxParallelForks`/`forkEvery` set, and could the suite split across CI jobs "
                "by tag/filter? Grep the log for `> Task :<module>:test` and "
                "`BUILD SUCCESSFUL in <wall>` for the task/build wall, and check whether the "
                "tests share one external resource (a single TestKit working dir, a DB, a "
                "fixed port) - the usual reason the suite is kept serial. Recover from git "
                "history why it's one task before changing it.",
        "constraints": "Tests run in parallel forks or sharded across jobs must stay "
                       "ISOLATED: if they share one mutable working dir (a single TestKit "
                       "project dir), DB, or fixed port, forks/shards collide and flake or "
                       "false-pass. Each fork/shard needs its OWN fresh fixture, the full "
                       "test count must be preserved, and this is a test-infra change needing "
                       "the owner's sign-off - roll it out alongside the existing serial task "
                       "until pass/fail parity holds across several runs. If you take the "
                       "split-across-jobs path (not in-task `maxParallelForks`) and this task "
                       "is a required status check, add the new jobs to branch protection as "
                       "required checks (or the ruleset equivalent), or the split-out tests "
                       "silently stop gating merges — everything stays green while the gate no "
                       "longer runs them; that admin-only re-gating is part of the fix.",
        "docs": ["Gradle - parallel test execution (`maxParallelForks`/`forkEvery`): "
                 "https://docs.gradle.org/current/userguide/performance.html#execute_tests_in_parallel",
                 "Gradle Test task reference: "
                 "https://docs.gradle.org/current/dsl/org.gradle.api.tasks.testing.Test.html"],
        "deliver": "Parallelise the suite - set `maxParallelForks` for in-task fork "
                   "parallelism and/or split the suite across CI jobs by test filter/tag so "
                   "independent tests land on different runners, each with an isolated "
                   "fixture; keep a full-suite fallback, confirm pass/fail parity, and "
                   "re-measure the step.",
    },
}


def _addressable_plain(pole: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    """The floor note's addressable-ceiling sentence as plain text (no blockquote /
    bold), for embedding in the agent prompt. "" when there's nothing to floor."""
    fn = _floor_note(pole, candidates)
    if not fn:
        return ""
    return (fn[0].replace("> **What a change here can buy (wall-clock):** ", "")
            .replace("**", ""))


# Setup/teardown step names that are NOT the load-bearing work, so they're skipped
# when picking a pole's "dominant step" for the generic hand-off (mirrors the same
# constant in collect_runs - kept in sync, not cross-imported, per the skill layout).
_NON_WORK_STEP_RE = re.compile(
    r"^(set up job|complete job|post\b|checkout\b|set up |setup [a-z]*node)",
    re.IGNORECASE)


def _dominant_step_from_timeline(timeline: dict[str, Any] | None,
                                 cat_by_name: dict[str, str] | None = None,
                                 ) -> tuple[str, float, float] | None:
    """(name, dur_s, share) of the dominant non-setup step in a timeline, or None.
    Mirrors the dominant step the collector crowns + cross-run-checks, so an undetected
    pole's agent prompt focuses on the SAME lever the waterfall + check highlight.

    With `cat_by_name` (a step-name → category map, from the pole's own decomposition),
    the dominant step is the LEAD step of the dominant CATEGORY — the category with the
    largest aggregate duration, then its slowest step — exactly as
    `collect_runs._decompose_job_steps` / `_dominant_category_lead` crown it. Without the
    map (older/degraded doc), it falls back to the single longest step. The category rule
    prevents naming a lone big step in a non-dominant category when a multi-step phase
    out-aggregates it (the dominant_step-disagreement class)."""
    if not isinstance(timeline, dict):
        return None
    job_dur = _num(timeline.get("job_dur_s")) or 0.0
    cand = [s for s in timeline.get("steps", [])
            if _num(s.get("dur_s")) and not _NON_WORK_STEP_RE.match(str(s.get("name", "")))]
    if not cand or job_dur <= 0:
        return None
    if cat_by_name:
        def _cat(s: dict[str, Any]) -> str:
            return cat_by_name.get(str(s.get("name", "")), "other")
        agg: dict[str, float] = {}
        for s in cand:
            agg[_cat(s)] = agg.get(_cat(s), 0.0) + (_num(s.get("dur_s")) or 0.0)
        dom_cat = _cat(max(cand, key=lambda s: (agg[_cat(s)], _num(s.get("dur_s")) or 0.0)))
        dom = max((s for s in cand if _cat(s) == dom_cat),
                  key=lambda s: _num(s.get("dur_s")) or 0.0)
    else:
        dom = max(cand, key=lambda s: _num(s.get("dur_s")) or 0.0)
    d = _num(dom.get("dur_s")) or 0.0
    return str(dom.get("name", "")), d, d / job_dur


def _pole_gate_prompt_claim(check: str, wf: str, dur: str, gate_count: int, npop: int,
                            pole: dict[str, Any],
                            cs: "claims.ClaimSet | None") -> str:
    """The agent-prompt "THE GATE" line (kind=pole_gate_prompt), framed to MATCH the pole's OWN
    header so a DEMOTED pole's prompt can't re-assert the typical-gate framing its header disowns. A
    pole the header framed as a gate (the blocker / a chain member / a typical pole — `_demoted_gate_
    framing` absent or False) keeps the typical-gate line ("… P50 {dur}; its workflow gates N/npop").
    A pole the header DEMOTED (`_demoted_gate_framing` True — the caddy goreleaser-check class: pole_n
    0, present on a majority, a slower concurrent check gates ahead every PR, so the header reads
    "Rarely the merge gate") instead gets the "Rarely the merge pole" line and DROPS the workflow
    gate-count — that count is a SIBLING check's frequency (ci.yml's 20/20 is driven by the required
    `test` matrix), never this pole's, so attributing it next to a pole that's the slowest on 0/npop
    is the contradiction the header already narrates. The demoted wording stays NEUTRAL on
    throughput-vs-required (the header owns that split) so it fits every demotion variant. Both
    framing literals live lexically inside a `Claim(...)` (the source lint's requirement); only the
    selected one is registered. `cs` is None on the drill-parity direct calls, which pass no stamp and
    so take the gate branch (byte-identical to the pre-helper output). The em-dash strip mirrors
    render()'s report-wide strip so manifest<->prose stays byte-identical."""
    demoted = bool(pole.get("_demoted_gate_framing", False))
    # `wf` is a repo-controlled workflow FILENAME and this claim's `rendered` is emitted into
    # the agent prompt through `_fence_body` (per-line `_fence_safe`). A `Claim.rendered` must
    # be BYTE-IDENTICAL to the report text (`check_claims_cover_framing_vocabulary` binds by
    # exact-span containment), so the manifest must carry the SAME `_fence_safe(wf)` the fence
    # emission produces — otherwise a >=3-backtick filename defuses on emit but not in the
    # manifest, and the framing-coverage guard false-fails. No-op on a clean filename.
    wf = _fence_safe(wf)
    _pn = _num(pole.get("_pole_n"))
    _pres = _num(pole.get("_present"))
    _freq = f", on only {int(_pn)}/{npop} sampled PRs" if (_pn is not None and npop) else ""
    _prestxt = f" (present on {int(_pres)}/{npop})" if (_pres is not None and npop) else ""
    _gate_claim = claims.Claim(
        kind="pole_gate_prompt", subject=check,
        fields={"dur": dur, "gate_count": gate_count, "npop": npop, "demoted": False},
        rendered=_strip_emdashes(
            f"Slowest check a typical PR waits on: P50 {dur}"
            + (f"; its workflow `{wf}` gates {gate_count}/{npop} sampled PRs."
               if gate_count and npop else ".")))
    _demoted_claim = claims.Claim(
        kind="pole_gate_prompt", subject=check,
        fields={"dur": dur, "gate_count": gate_count, "npop": npop, "demoted": True},
        rendered=_strip_emdashes(
            f"Rarely the merge pole - the actual slowest check a PR waits on{_freq}{_prestxt}: "
            f"P50 {dur}. A slower concurrent check usually gates ahead, so speeding it helps only "
            "the PRs where it IS the pole, not typical merge-wait."))
    # A modal-CHAIN member (issue #112) is on-spine but is NOT "the slowest check a typical PR
    # waits on" — it is one serial `needs:` STAGE whose time ADDS on the gate path (the pole's
    # role line already says "Stage N/M … its {dur} ADDS"). Frame the prompt gate line the same
    # way: name the stage and the CHAIN total (`merge_dur`), then this stage's own P50 — so a
    # mid-chain stage never re-asserts the typical-single-gate framing. A chain member is never
    # `_demoted_gate_framing` (the stamp excludes chain members), so these are mutually exclusive.
    _chain = pole.get("_chain_member") if isinstance(pole.get("_chain_member"), dict) else None
    _chain_claim = None
    if _chain and _chain.get("merge_dur"):
        _chain_claim = claims.Claim(
            kind="pole_gate_prompt", subject=check,
            fields={"dur": dur, "stage": int(_chain.get("stage") or 0),
                    "chain_len": int(_chain.get("len") or 0),
                    "merge_dur": str(_chain.get("merge_dur")), "npop": npop},
            rendered=_strip_emdashes(
                f"Stage {int(_chain.get('stage') or 0)}/{int(_chain.get('len') or 0)} of the "
                f"`needs:` gate chain (chain P50 {_chain.get('merge_dur')}); this stage: P50 {dur}. "
                "Its time ADDS on the gate path - the chain, not this single stage, is what a "
                "typical PR waits on."))
    _claim = _chain_claim or (_demoted_claim if demoted else _gate_claim)
    return cs.add(_claim) if cs is not None else _claim.rendered


def _build_agent_prompt(leaf: dict[str, Any] | None, pole: dict[str, Any],
                        candidates: list[dict[str, Any]], run_url: str | None,
                        repo: str, sha: str | None, gate_count: int,
                        npop: int, timeline: dict[str, Any] | None = None,
                        structural: list[dict[str, Any]] | None = None,
                        data_driven: list[dict[str, Any]] | None = None,
                        *, cs: "claims.ClaimSet | None" = None,
                        cross_run_rendered: bool = False) -> str:
    """Assemble the per-pole agent prompt from the MEASURED context + the per-cause
    static block. Self-contained: pasted alone it gives the agent the gate, the
    drill, the cause + verbatim evidence, the addressable wall-clock ceiling, where
    to look, the failure mode to guard, the docs, and what to deliver - without
    prescribing the fix. When no catalog pattern matched the job's log (`leaf` is None
    / unknown fix_key), it still hands off a GENERIC prompt focused on the dominant
    step from the timeline - so every pole gets an actionable prompt, not a dead end.
    A pole can have no log `leaf` yet still match a STRUCTURAL catalog pattern
    (OPT70–75, rendered AS the pole) or a DATA-DRIVEN catalog pattern (e.g. OPT24,
    rendered in the 'Also noticed' appendix) - pass those finding lists through so the
    generic prompt names the matched pattern instead of falsely claiming no catalog match."""
    meta = _FIX_META.get(str((leaf or {}).get("fix_key", "")))
    if not meta:
        return _build_generic_agent_prompt(
            pole, candidates, run_url, repo, sha, gate_count, npop, timeline,
            structural, data_driven, cs=cs, cross_run_rendered=cross_run_rendered)
    wf = _wf_base(pole.get("workflow_file", ""))
    check = _clean_label(str(pole.get("check", "")))
    dur = _clock(_num(pole.get("p50_s")))
    sha7 = (sha or "")[:7]
    rid = run_url.rstrip("/").rsplit("/", 1)[-1] if run_url else ""
    # `gate_count` is the WORKFLOW's gate frequency (summed over its matrix legs), so
    # attribute it to the workflow, not this one leg - the named leg may be the literal
    # pole on fewer PRs than its workflow gates. The gate LINE itself (typical vs demoted
    # framing) is built by `_pole_gate_prompt_claim` off the pole's `_demoted_gate_framing` stamp,
    # so a DEMOTED pole's prompt matches its "Rarely the merge gate" header instead of re-asserting
    # the typical-gate claim (the caddy goreleaser-check contradiction).
    gate = _pole_gate_prompt_claim(check, wf, dur, gate_count, npop, pole, cs)

    out = [f"starslingdev/ci-speedup measured where the time goes below but does NOT "
           "prescribe the fix - investigate it in the repo and apply a safe change.",
           "",
           f"REPO: {repo}" + (f" (audited at commit {sha7})" if sha7 else ""),
           "",
           "THE GATE",
           f"- Workflow `{wf}`, job `{check}`.",
           f"- {gate}",
           ""]

    _bi = pole.get("bimodal")
    _slow_drill = isinstance(_bi, dict) and _num(_bi.get("high_p50_s"))
    _rep = ("representative of the slow mode" if _slow_drill else "closest to the P50")
    wtg = ["WHERE THE TIME GOES"
           + (f" (representative run {rid}, {_rep})" if rid else "")]
    if leaf.get("unit_label"):
        wtg.append(f"- {leaf['unit_label']}.")
    mg = leaf.get("magnitude")
    if isinstance(mg, dict) and mg.get("value") is not None:
        unit = str(mg.get("unit", ""))
        v = mg["value"]
        vs = f"{v:.0f}%" if unit == "%" else f"{v}{unit}"
        # Only claim cross-run validation when the "🔬 Cross-run check" section actually
        # rendered (`cross_run_rendered`). A singleton magnitude sample suppresses that
        # section (`_mag_line` returns [] on <2 values), so asserting "validated across
        # runs in the cross-run check above" would dangle at a section that isn't there.
        _val = ("(validated across runs in the cross-run check above)"
                if cross_run_rendered else "(measured in the drilled run)")
        wtg.append(f"- Load-bearing magnitude - {mg.get('label', '')}: ~{vs} {_val}.")
    out += wtg + [""]

    out += ["THE MEASURED CAUSE", f"- {meta['cause']}"]
    ev = (leaf.get("evidence") or [])[:2]
    if ev:
        out += ["  Verbatim from the run:"] + [f"    {_fence_safe(e)}" for e in ev]
    out += [""]

    addr = _addressable_plain(pole, candidates)
    if addr:
        out += ["WHAT'S ADDRESSABLE (wall-clock ceiling - don't over-promise)",
                f"- {addr}", ""]

    constraints = [f"- {meta['constraints']}"]
    # A cache pole whose measured distribution demotes it (miss-tail / mostly-warm) carries
    # the context the agent must not re-derive wrong: the cache mostly hits, so the win is on
    # the miss-heavy tail, and a fork/cold benchmark would overstate it.
    _cd = _as_dict(pole.get("cache_dist"))
    if _cd.get("verdict") in ("miss-tail", "mostly-warm"):
        _med = _num(_as_dict(_cd.get("pr")).get("upstream_median"))
        _mtxt = f" (upstream median miss ~{_pct_disp(_med)})" if _med is not None else ""
        constraints.append(
            f"- Cache context: across sampled PRs the cache mostly HITS{_mtxt}; the drilled "
            "run is a cache-miss-heavy minority. Size the win on the miss-heavy tail, not the "
            "whole job, and benchmark on an upstream PR with the cache warm — NEVER a fork or "
            "cold clone (a fork cannot read the repo cache and shows a worst-case cold build).")
    out += ["WHERE TO LOOK", f"- {meta['look'].format(wf=wf)}", "",
            "CONSTRAINTS / FAILURE MODE TO GUARD"] + constraints + [
            "", "READ FIRST"] + [f"- {d}" for d in meta["docs"]] + [
            "", "DELIVER & VERIFY", f"- {meta['deliver']}"]
    return ("#### 🤖 Prompt for your coding agent\n\n```text\n"
            + _fence_body(out) + "\n```\n")


def _build_generic_agent_prompt(pole: dict[str, Any],
                                candidates: list[dict[str, Any]], run_url: str | None,
                                repo: str, sha: str | None, gate_count: int,
                                npop: int, timeline: dict[str, Any] | None,
                                structural: list[dict[str, Any]] | None = None,
                                data_driven: list[dict[str, Any]] | None = None,
                                *, cs: "claims.ClaimSet | None" = None,
                                cross_run_rendered: bool = False) -> str:
    """The hand-off for a pole with no log-level catalog detector match: ci-speedup
    measured WHERE the time goes (the gate, the dominant step + its share) but no log
    detector named the sub-cause, so it points the agent at that step to investigate -
    still a complete, self-contained prompt, just without a catalog-specific cause/docs.
    EXCEPTION: a pole can match a non-log catalog pattern without any log `leaf` — a
    STRUCTURAL one (OPT70–75, rendered AS the pole above) or a DATA-DRIVEN one (e.g. OPT24,
    rendered in the 'Also noticed' appendix). When `structural` OR `data_driven` is non-empty
    the prompt names that matched pattern and points at where it renders, instead of falsely
    asserting `NO CATALOG PATTERN MATCHED` while that pattern renders alongside — the same
    contradiction the spine waterfall (`data_driven_present`) already suppresses."""
    struct_pats = sorted({str(f.get("pattern", "")) for f in (structural or [])
                          if f.get("pattern")})
    dd_pats = sorted({str(f.get("pattern", "")) for f in (data_driven or [])
                      if f.get("pattern")})
    wf = _wf_base(pole.get("workflow_file", ""))
    check = _clean_label(str(pole.get("check", "")))
    dur = _clock(_num(pole.get("p50_s")))
    sha7 = (sha or "")[:7]
    rid = run_url.rstrip("/").rsplit("/", 1)[-1] if run_url else ""
    # The pole's decomposition carries each step's category; pass that map so the prompt
    # crowns the SAME dominant CATEGORY lead the waterfall + cross-run check do (the
    # timeline itself has no category), instead of the single longest step.
    _cat_by_name = {str(s.get("step", "")): str(s.get("category", ""))
                    for s in (pole.get("steps") or []) if s.get("step")}
    dom = _dominant_step_from_timeline(timeline, _cat_by_name or None)
    # Claims layer (pole_gate_prompt): the agent-prompt gate line, built by
    # `_pole_gate_prompt_claim` off the pole's `_demoted_gate_framing` stamp so a DEMOTED generic
    # pole's prompt matches its "Rarely the merge gate" header instead of re-asserting the typical-
    # gate framing. Same helper as `_build_agent_prompt`, so the two prompt paths can't diverge.
    gate = _pole_gate_prompt_claim(check, wf, dur, gate_count, npop, pole, cs)
    if pole.get("job_timing_unavailable"):
        reason = str(pole.get("job_timing_unavailable") or "").strip()
        out = [
            "starslingdev/ci-speedup measured this gate from sampled PR check-runs but "
            "does NOT prescribe the fix - no developer-facing workflow job timing was "
            "available, so step timing is intentionally withheld.",
            "",
            f"REPO: {repo}" + (f" (audited at commit {sha7})" if sha7 else ""),
            "",
            "THE GATE",
            f"- Workflow `{wf}`, check `{check}`.",
            f"- {gate}",
            "",
            "WHAT IS MISSING",
            f"- {reason}" if reason else (
                "- No sampled workflow job for this check ran on a developer-facing "
                "event, so the report did not borrow push/schedule step timings."),
            "",
            "NEXT STEP",
            "- Capture this workflow on a pull_request, pull_request_target, or "
            "merge_group run and rerun ci-speedup before changing workflow steps. "
            "Once developer-event job timing exists, optimize the measured dominant "
            "step from the refreshed report."
        ]
        return ("#### 🤖 Prompt for your coding agent\n\n```text\n"
                + _fence_body(out) + "\n```\n")
    if struct_pats:
        lead = (f"starslingdev/ci-speedup measured where the time goes below but does NOT "
                f"prescribe the fix - a structural catalog pattern "
                f"({', '.join(struct_pats)}) matched this pole (see the **structural "
                f"root-cause** section above for the measured lever + its risk axis); the "
                f"dominant step below is where that lever's time is spent.")
    elif dd_pats:
        lead = (f"starslingdev/ci-speedup measured where the time goes below but does NOT "
                f"prescribe the fix - a data-driven catalog pattern "
                f"({', '.join(dd_pats)}) matched this pole (see the **Also noticed** "
                f"section below for the measured lever + its fix recipe); the dominant "
                f"step below is where that lever's time is spent.")
    else:
        lead = ("starslingdev/ci-speedup measured where the time goes below but does NOT "
                "prescribe the fix - and for this job its detectors found no known "
                "root-cause pattern, so investigate the dominant step in the repo.")
    out = [lead,
           "",
           f"REPO: {repo}" + (f" (audited at commit {sha7})" if sha7 else ""),
           "",
           "THE GATE",
           f"- Workflow `{wf}`, job `{check}`.",
           f"- {gate}",
           ""]
    wtg = ["WHERE THE TIME GOES" + (f" (representative run {rid})" if rid else "")]
    pole_dom = str(pole.get("dominant_step") or "")
    pole_dom_d = _num(pole.get("dominant_p50_s"))
    pole_dom_share = _num(pole.get("dominant_share"))
    if dom:
        name, d, share = dom
        # The cross-run validation clause is true ONLY when the "🔬 Cross-run check"
        # section actually rendered (`cross_run_rendered`). A singleton magnitude sample
        # (just the drilled run) suppresses that section (`_mag_line` returns [] on <2
        # values), so pointing at it would dangle — say "measured in the drilled run"
        # instead, matching the timeline the reader can actually see.
        _val = (", validated across runs in the cross-run check above"
                if cross_run_rendered else ", measured in the drilled run")
        wtg.append(f"- The job's time is dominated by the `{name}` step: "
                   f"~{_clock(d)} ({round(share * 100)}% of the job wall{_val}).")
    elif pole_dom and pole_dom_d:
        # Shallow pole (rendered from the sampled decomposition, no single-run timeline
        # was drilled for it) — name the dominant step from the SAMPLED per-step P50s, and
        # say so. NEVER point at a "step timeline above" that this pole doesn't render (R2).
        share_txt = (f" ({round(pole_dom_share * 100)}% of the job wall)"
                     if pole_dom_share else "")
        wtg.append(f"- The job's time is dominated by the `{pole_dom}` step: "
                   f"~{_clock(pole_dom_d)}{share_txt}, from the sampled per-step "
                   "decomposition (no single-run timeline was captured for this job).")
    else:
        wtg.append("- No per-step breakdown was captured for this job; profile its "
                   "slowest step in the repo.")
    out += wtg + [""]
    if struct_pats:
        out += ["STRUCTURAL CATALOG PATTERN MATCHED",
                f"- A structural catalog pattern ({', '.join(struct_pats)}) matched this "
                "pole - see the **structural root-cause** section above for the measured "
                "lever, its risk / guardrail / rollout, and the catalog fix recipe. The "
                "step above is the load-bearing one that lever targets; open its log (the "
                "Audit link) to see exactly what inside it the lever reshapes.", ""]
    elif dd_pats:
        out += ["DATA-DRIVEN CATALOG PATTERN MATCHED",
                f"- A data-driven catalog pattern ({', '.join(dd_pats)}) matched this "
                "pole - see the **Also noticed** section below for the measured lever and "
                "its catalog fix recipe (it sits ON the critical path, so it shrinks this "
                "pole's wall time). The step above is the load-bearing one that lever "
                "targets; open its log (the Audit link) to see what inside it the lever "
                "reshapes.", ""]
    else:
        out += ["NO CATALOG PATTERN MATCHED",
                "- ci-speedup's detector set didn't recognize this job's stack, so there "
                "is no named sub-cause. The step above is the load-bearing one; open its "
                "log (the Audit link) and determine what inside it is slow (e.g. an "
                "uncached build, a serial test phase, a large install).", ""]
    addr = _addressable_plain(pole, candidates)
    if addr:
        out += ["WHAT'S ADDRESSABLE (wall-clock ceiling - don't over-promise)",
                f"- {addr}", ""]
    look_step = f"the `{dom[0]}` step" if dom else "the dominant step"
    out += ["WHERE TO LOOK",
            f"- The `{wf}` workflow definition for {look_step}, and the tool/config it "
            "invokes (build tool, test runner, or install) - that's where its time is "
            "spent.", "",
            "DELIVER & VERIFY",
            f"- A change that cuts {look_step}'s wall time without dropping coverage; "
            "re-measure the step on a PR run to confirm the reduction."]
    return ("#### 🤖 Prompt for your coding agent\n\n```text\n"
            + _fence_body(out) + "\n```\n")


def _llm_analysis_block(a: dict[str, Any], cross_run_rendered: bool = False) -> list[str]:
    """Render the LLM gap-fill for a pole with NO catalog detector match: a clearly
    LABELLED, log-grounded root-cause reading produced by the agent running the skill
    (see SKILL.md's gap-fill phase). The label + the cited verbatim lines keep
    provenance honest - this is the skill's LLM reading the captured log, NOT a
    measured catalog detector, so it's framed as a lead to verify. [] when empty.

    `cross_run_rendered` says whether this pole's "🔬 Cross-run check" section actually
    rendered; only then does the provenance line cite it (a singleton magnitude sample
    suppresses that section, so the citation must be suppressed too, or it dangles)."""
    if not isinstance(a, dict):
        return []
    # `cause`/`breakdown` are LLM prose ABOUT the log, so they can echo a credential the
    # log carried. They render as markdown (not in a fence), so they don't pass through
    # `_fence_safe` — mask them here. The `evidence` lines and the `prompt` do go through
    # `_fence_safe`/`_fence_body`; these two fields are the only untrusted-derived text
    # that bypasses it, which makes this the second (and last) masking chokepoint.
    cause = _redact_secrets(str(a.get("cause", "")).strip())
    breakdown = a.get("breakdown") or []
    evidence = a.get("evidence") or []
    if not (cause or breakdown or evidence):
        return []
    _measured = ("the timeline + cross-run check above are measured"
                 if cross_run_rendered else "the timeline above is measured")
    out = [f"**🤖 LLM root-cause analysis** — no catalog pattern matched this job, so "
           "the analysis below is the skill's LLM reading the captured job log, "
           "**grounded in the lines quoted below**. It is a strong lead to verify, "
           f"**not** a measured catalog detector - {_measured}; this cause is inferred.",
           ""]
    if cause:
        out += [cause, ""]
    if breakdown:
        out += ["**Where the time likely goes (LLM reading of the log):**", ""]
        for row in breakdown:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                out.append(_redact_secrets(f"- {row[0]} - {row[1]}"))
            else:
                out.append(_redact_secrets(f"- {row}"))
        out.append("")
    if evidence:
        out += ["**Evidence — verbatim from the captured job log:**", "",
                "```text", *[_fence_safe(e) for e in evidence], "```", ""]
    return out


def _llm_agent_prompt(body: str) -> str:
    """Wrap an LLM-authored, log-tailored agent prompt (from the analysis JSON) in the
    same copy-paste block matched/generic prompts use, **prepending the standard
    no-prescription disclaimer** so a gap-fill pole hands off on the same no-prescription
    contract as a catalog pole (the load-bearing `does NOT prescribe the fix` substring;
    the lead clause is a deliberate variant, see the inline note below) — and so the
    report satisfies `verify_report`'s one-disclaimer-per-prompt
    invariant. The agent writes only the log-grounded body (SKILL.md 4a); the renderer
    owns the disclaimer, exactly as `_build_agent_prompt`/`_hygiene_prompt` embed it in
    their templates. Idempotent: a body that already carries the disclaimer (a
    hand-written prompt that included it) is not doubled, so the per-prompt count stays 1."""
    body = str(body).strip()
    if "does NOT prescribe the fix" not in body:
        # No "below": unlike the catalog/generic prompts (whose structured content
        # follows in-fence), the gap-fill's log evidence renders in the `🤖 LLM
        # root-cause analysis` block ABOVE this one, and the agent's prompt is what
        # follows — so a locator word here would point at the wrong thing.
        body = ("ci-speedup read the captured job log but does NOT prescribe the "
                "fix - investigate it in the repo and apply a safe change.\n\n" + body)
    # The LLM-authored body is model output grounded in the repo log — it can echo a repo
    # name / log line carrying a ``` run. Fence-safe it PER-LINE (its own line breaks survive)
    # so a model-emitted stray fence can't close this ```text block.
    return ("#### 🤖 Prompt for your coding agent\n\n```text\n"
            + _fence_body(body.split("\n")) + "\n```\n")


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #

def _wf_base(wf: str) -> str:
    return str(wf).rsplit("/", 1)[-1]


def _job_base(name: str) -> str:
    """A job/check name with a trailing matrix `(variant)` stripped, scope-cleaned, and lowercased,
    so a finding's job (`pytest-torch`) matches a drilled pole's matrix leg
    (`pytest-torch (ubuntu-latest, 3.10)`). Used to exclude an off-path appendix finding that sits on
    a job already rendered AS a long-pole drill-down (Class A #5)."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", _clean_label(str(name or ""))).strip().lower()


def _dom_index(steps: list[dict[str, Any]], dom_name: str) -> int:
    """Which step is the one we drill into - by name (exact, then case-insensitive
    contains), else the longest-running NON-BOILERPLATE step.

    The fallback excludes setup/cleanup boilerplate so an AGGREGATE dominant label
    (e.g. `Build + 2 more`, which matches no single step name) doesn't fall through
    to a long `checkout`/`Set up job`/`Post` step — that would mark a different step
    than the category-aware `dominant_step` the producer crowned. Falls back to the
    full step set only when EVERY step is boilerplate (mirrors
    collect_runs._decompose_job_steps)."""
    clean = _clean_label(dom_name).lower()
    for i, s in enumerate(steps):
        if _clean_label(str(s.get("name", ""))).lower() == clean:
            return i
    for i, s in enumerate(steps):
        if clean and clean in _clean_label(str(s.get("name", ""))).lower():
            return i
    if not steps:
        return -1
    work = [i for i, s in enumerate(steps)
            if not _NON_WORK_STEP_RE.match(_clean_label(str(s.get("name", ""))))]
    pool = work or list(range(len(steps)))
    return max(pool, key=lambda i: _num(steps[i].get("dur_s")) or 0.0)


def _dom_lead_idx(steps: list[dict[str, Any]], dom_cat: str) -> int:
    """Index (within `steps`, already duration-sorted) of the DOMINANT CATEGORY lead —
    the slowest NON-BOILERPLATE step whose `category` is `dom_cat`, i.e. the SAME step
    `collect_runs._decompose_job_steps` / `_dominant_category_lead` crowns on the pole.
    The per-step chart marks THIS row, not the single longest step (index 0), so its ◀
    "addressable lever" agrees with the root-cause section + agent prompt: when a
    multi-step phase out-aggregates one big step, the big step sits in a NON-dominant
    category and is the wrong lever (the dominant_step-disagreement class).

    Excludes boilerplate (`_NON_WORK_STEP_RE`) EXACTLY as the crown does (collect_runs
    builds its category lead over non-boilerplate steps only): without this filter a slow
    boilerplate step that happens to share `dom_cat` — e.g. "Set up job" (category
    `setup`) or "Complete job" (`other`) outranking the real work lead — would be marked
    here while the prose/prompt named the work step, re-opening the very disagreement this
    closes. Falls back to 0 (the longest step) when there's no category info or no
    non-boilerplate row matches — degrading to the old behaviour rather than marking
    nothing."""
    if not dom_cat:
        return 0
    for i, s in enumerate(steps):
        # Pole steps key the label as "step"; some callers use "name" — accept either.
        label = _clean_label(str(s.get("step") or s.get("name") or ""))
        if (str(s.get("category", "")) == dom_cat
                and not _NON_WORK_STEP_RE.match(label)):
            return i
    return 0


def _collapse_timeline(steps: list[dict[str, Any]], dom_idx: int,
                       job_dur: float) -> tuple[list[dict[str, Any]], int, int]:
    """Keep the steps worth a bar (>= ~1.5% of the job, or the dominant one) and
    count the rest. A real run has a dozen ~0s post/cleanup steps; drawing each as a
    sliver buries the shape. Returns (kept_steps, kept_dom_idx, n_collapsed)."""
    thresh = max(2.0, 0.015 * job_dur)
    kept: list[dict[str, Any]] = []
    kept_dom = -1
    collapsed = 0
    for i, s in enumerate(steps):
        if i == dom_idx or (_num(s.get("dur_s")) or 0.0) >= thresh:
            if i == dom_idx:
                kept_dom = len(kept)
            kept.append(s)
        else:
            collapsed += 1
    return kept, kept_dom, collapsed


def _gantt_bar(start: float, dur: float, total: float) -> str:
    """One timeline bar: `░` for the time already elapsed before the step starts,
    `█` for the step's own span, blanks after. The bar field is _BARW columns wide
    and represents [0, total] (the whole job), so a step's bar sits at the point in
    the run when it actually runs."""
    if total <= 0:
        return " " * _BARW
    lead = int(round(start / total * _BARW))
    lead = max(0, min(lead, _BARW - 1))
    blen = max(1, int(round(dur / total * _BARW)))
    blen = min(blen, _BARW - lead)
    return "░" * lead + "█" * blen + " " * (_BARW - lead - blen)


def _emit_gantt(out: list[str], steps: list[dict[str, Any]], total: float,
                dom_idx: int, has_next: bool = False) -> None:
    """Render the step timeline (execution order, offset bars). The OFFSET BAR shows
    WHEN each step runs (`░` = elapsed before it starts); the number column shows how
    long it took - its DURATION and its % of the job - consistent with the drill
    levels below (never a raw timestamp). When a drill follows (`has_next`), the
    dominant step gets the SAME connector wire the deeper levels use - `◀┐` on its
    row, `│` down the rows below it - so the arrow to the next level is drawn
    literally, exactly like level-to-level below."""
    for i, s in enumerate(steps):
        start = _num(s.get("start_s")) or 0.0
        dur = _num(s.get("dur_s")) or 0.0
        bar = _gantt_bar(start, dur, total)
        pct = f"{round(100 * dur / total)}%" if total else ""
        core = (f"   {_lbl(str(s.get('name', ''))):<{_LBLW}}  {bar}  "
                f"{_clock(dur):>{_DURW}}  {pct:>{_PCTW}}")
        if has_next and i == dom_idx:
            out.append(core + " ◀┐")
        elif has_next and i > dom_idx:
            out.append(f"{core}  │")
        elif i == dom_idx:
            out.append((core + "  ◀ the slow step (drilled into below)").rstrip())
        else:
            out.append(core.rstrip())


def _pole_waterfall(pole: dict[str, Any], leaf: dict[str, Any] | None,
                    timeline: dict[str, Any] | None = None,
                    log_present: bool = False,
                    analysis_present: bool = False,
                    structural_present: bool = False,
                    data_driven_present: bool = False,
                    data_driven_on_path: bool = True) -> list[str]:
    """The ASCII waterfall for one pole (no code fence): the blocking job's steps,
    then - when a log was captured - the dominant step's internals down to the
    root cause.

    With a captured `timeline` (one representative run's real per-step start/end),
    the step level is drawn as a sequential TIMELINE - steps run one after another,
    each bar offset to when it actually starts - making explicit that steps (unlike
    the concurrent jobs in level 1) do not overlap. Without one it falls back to
    P50 bars sorted by duration.

    `data_driven_on_path` mirrors the appendix: a data-driven catalog match on a pole the spine
    DEMOTES as opt-in/rare (`spine_rare`) is still catalog coverage (never a gap), but the
    "no gap — see Also noticed" pointer must NOT claim the pole "sits ON the critical path"
    while the spine footnote demotes it (the paradedb `Test pg_search` double-framing)."""
    # The pointer tail for a data-driven catalog match — on-path for a typical pole, opt-in/rare
    # for a `spine_rare` pole. Never a coverage gap either way (a measured catalog lever matched).
    _dd_tail = ("flagged as sitting ON the critical path.)" if data_driven_on_path
                else "flagged as opt-in / rare — its fix helps the minority of PRs that run "
                     "it, not the typical merge path.)")
    wf_base = _wf_base(pole.get("workflow_file", ""))
    steps = sorted((pole.get("steps") or []),
                   key=lambda s: -(_num(s.get("p50_s")) or 0.0))
    step_total = sum(_num(s.get("p50_s")) or 0.0 for s in steps)
    job_total = _num(pole.get("p50_s")) or step_total
    if pole.get("job_timing_unavailable"):
        reason = str(pole.get("job_timing_unavailable") or "").strip()
        return [
            "Level 2 - workflow-job drill withheld for this PR check-run gate.",
            "",
            f"- The check-run P50 is {_clock(job_total)} across sampled PRs.",
            "- Developer-facing workflow job timing was unavailable, so ci-speedup did "
            "not borrow push/schedule step timings for this pole.",
            *( [f"- {reason}"] if reason else [] ),
            "- Re-run after a pull_request, pull_request_target, or merge_group job "
            "sample exists to capture the dominant step before optimizing."
        ]
    dom_p50 = _num(pole.get("dominant_p50_s")) or (
        _num(steps[0].get("p50_s")) if steps else 0.0) or 0.0
    dom = _fence_safe(str(pole.get("dominant_step")
                          or (steps[0].get("step", "step") if steps else "step")))
    dom_dur = _clock(dom_p50)
    dom_pct = round(100 * dom_p50 / step_total) if step_total else 0
    deeper = leaf["deeper"] if leaf else None

    # --- Timeline path: one representative run's real step start/end. Steps run in
    # succession (not concurrently like the level-1 jobs), so draw them as an
    # offset Gantt instead of left-aligned bars. The run's steps sum exactly to its
    # job wall time, so there's no P50-doesn't-sum caveat here. ---
    tl_steps = (timeline or {}).get("steps") or []
    if tl_steps:
        tl_dur = _num(timeline.get("job_dur_s")) or sum(
            (_num(s.get("start_s")) or 0.0) + (_num(s.get("dur_s")) or 0.0)
            for s in tl_steps[-1:]) or 1.0
        # Only crown a single "slow step" when there's a real drill below it. For a
        # step-level root cause (e.g. several test steps run back-to-back), there's
        # no single step to zoom into - the timeline itself IS the finding, so don't
        # mark one bar and promise a drill that isn't coming; the fix explains it.
        has_drill = bool(deeper)
        dom_idx = _dom_index(tl_steps, dom) if has_drill else -1
        kept, kept_dom, collapsed = _collapse_timeline(tl_steps, dom_idx, tl_dur)
        # These steps run in SEQUENCE, so the waterfall sums to the job's wall time -
        # which means time shaved off any step comes straight off the job (1:1), and
        # off the merge wait down to the next concurrent check. Say so up front.
        _bi = pole.get("bimodal")
        _rep2 = ("a representative of its slow mode" if (isinstance(_bi, dict)
                 and _num(_bi.get("high_p50_s"))) else "the run closest to the typical "
                 "(P50) time")
        # Collision-triggered emphasis (owner UX edit 2026-07-19): when the check name
        # collides with a step name inside it, spell out that these are its **steps** (not
        # checks) so the drill can't be misread as ranking checks. Plain phrasing otherwise.
        _steps_phrase = ("its **steps** (not checks) run one after another"
                         if _check_step_collision(pole, timeline)[0]
                         else "its steps run **one after another**")
        lines = [
            f"Level 2 — inside that one job, {_steps_phrase} "
            f"(← {_mmss(0)} job start … {_mmss(tl_dur)} → ; `░` = time already elapsed, "
            f"`█` = the step running) and sum to the job's **{_clock(tl_dur)}** wall "
            f"time on this run - {_rep2}. Because "
            "they're sequential, time cut from any step comes straight off the job's "
            "wall-clock (and off the merge wait, down to the next concurrent check):", ""]
        # Reconcile with the check-gate clock (the Contents critical-path value, which
        # excludes the runner setup/teardown steps) up front, before the bars, so it
        # doesn't break the connector wire that hangs off the dominant step.
        check_p50 = _num(pole.get("p50_s")) or 0.0
        if check_p50 and tl_dur - check_p50 > max(20.0, 0.08 * check_p50):
            lines += [f"({_clock(check_p50)} is this check's **P50 across "
                      f"PRs** (the check-gate clock); this is **one representative run** whose job wall is "
                      f"{_clock(tl_dur)} — they differ from run-to-run variance and "
                      "because the job clock includes setup/teardown the check-gate "
                      "excludes.)", ""]
        _emit_gantt(lines, kept, tl_dur, kept_dom, has_next=has_drill)
        if collapsed:
            # The collapse threshold is max(2s, 1.5% of the job), so on a long job a
            # hidden step can be several seconds - name the real threshold. "or less"
            # (inclusive), since a step AT the threshold is collapsed too - "under Ns"
            # mislabels a hidden step that is exactly Ns.
            thr = max(2.0, 0.015 * (_num(tl_dur) or 0.0))
            note = (f"   (+{collapsed} setup/cleanup steps of {_clock(thr)} or less "
                    "not shown)")
            lines.append(f"{note:<{_CORELEN}}  │" if has_drill else note)
        if has_drill and 0 <= dom_idx < len(tl_steps):
            lines.append("   ┌" + "─" * (_MARKCOL - 4) + "┘")
            dom_real = _num(tl_steps[dom_idx].get("dur_s")) or 0.0
            lines += ["", f"   ▼ Level 3 — inside `{_lbl(dom)}`: {leaf['unit_label']}",
                      ""]
            for i, lvl in enumerate(deeper):
                last = i == len(deeper) - 1
                scale = lvl.get("scale_to_secs")
                if scale is None and lvl.get("scale_to_step"):
                    scale = dom_real
                _emit_level(lines, lvl["rows"],
                            header_below=None if last else deeper[i + 1]["header"],
                            blocker_note=lvl["blocker_note"] if last else "",
                            pct_of=lvl.get("pct_of", ""), scale_to=scale)
        elif log_present and leaf is None and structural_present:
            # A STRUCTURAL catalog pattern (OPT70–75) routed to this pole — the log
            # detector didn't fire, but a measured catalog lever DID, so this is NOT a
            # coverage gap. Point at the structural root-cause block rendered below
            # (ARCHITECTURE §11: the structural track is rendered AS the pole).
            lines += ["", "   (no log-level detector fired, but a **structural catalog "
                      "pattern** matched this pole - see the **structural root-cause** "
                      "below; the dominant step is the addressable lever.)"]
        elif log_present and leaf is None and data_driven_present:
            # A DATA-DRIVEN catalog pattern (e.g. OPT24) fired ON this pole with a credited
            # wall-clock saving — the log detector didn't fire, but a measured catalog lever
            # DID, so this is NOT a coverage gap. It's sized in the 'Also noticed' appendix, so
            # point there rather than re-render; `_dd_tail` matches the appendix's on-path vs
            # opt-in/rare framing so the pointer can't contradict the spine footnote.
            lines += ["", "   (no log-level detector fired, but a measured **catalog "
                      "pattern** matched this pole - see its entry in the **Also noticed** "
                      "section below, " + _dd_tail]
        elif log_present and leaf is None:
            # A log WAS captured but no detector recognized it - surface that loudly
            # rather than silently showing the timeline with no drill (a missed root
            # cause must read as a coverage gap, never as a clean job).
            if analysis_present:
                lines += ["", "   (no catalog pattern matched this job's log - see the "
                          "**LLM root-cause analysis** below, which reads the captured "
                          "log directly.)"]
            else:
                lines += ["", "   (captured this job's log but matched no known "
                          "root-cause pattern — no drill-down available; this is a "
                          "coverage gap, not a clean job. The detector set may need "
                          "extending for this stack.)"]
        return lines

    # Show the top steps, then ROLL UP the rest into one row so every second is
    # accounted for - the bars must sum to the job, or "where did the time go?"
    # has no answer. (Per-step times are P50, so they only roughly sum.)
    TOP = 6
    rows2: list[tuple[str, float | None, str | None]] = [
        (str(s.get("step", "")), _num(s.get("p50_s")), None) for s in steps[:TOP]]
    tail = steps[TOP:]
    if tail:
        tail_sum = sum(_num(s.get("p50_s")) or 0.0 for s in tail)
        rows2.append((f"…{len(tail)} smaller steps (setup, cache, post, …)", tail_sum,
                      f"~{_clock(tail_sum)}"))
    # The per-step P50s are each measured independently, so they sum to step_total,
    # which rarely equals the job's own P50 (no single run sits at the median of
    # every step at once). Say so rather than claiming a clean "adds up to the job".
    gap = step_total and abs(step_total - job_total) > max(3.0, 0.04 * job_total)
    recon = (f" — each step's P50 is measured on its own, so they sum to "
             f"~{_clock(step_total)} vs the job's own {_clock(job_total)} P50; "
             "read the bars as proportions, not an exact sum" if gap else
             "; they run in sequence and roughly add up to the job")
    lines = [f"Where the job's ~{_clock(job_total)} goes - every step, slowest "
             f"first{recon}:", ""]
    if not deeper:
        # No drill: no captured log, a captured-but-unrecognized log, or a step-level
        # root cause (categorical leaf - the fix follows below). Show the steps
        # without a connector, and distinguish "no log" from "log present but no
        # detector matched" so the latter reads as a coverage gap, not a clean job.
        # The ◀ "addressable lever" marker lands on the DOMINANT CATEGORY lead (the
        # step the root-cause/prompt crown names), not the single longest step - which
        # sits in a non-dominant category when a multi-step phase out-aggregates it
        # (the dominant_step-disagreement class). rows2 mirrors steps[:TOP], so the
        # lead's index in `steps` is its row; fall back to 0 if it's rolled into the
        # tail, mirroring the old single-longest-step behaviour.
        _emit_level(lines, rows2, header_below=None,
                    mark_idx=_dom_lead_idx(steps[:TOP],
                                           str(pole.get("dominant_category", ""))))
        if structural_present and leaf is None:
            # A structural catalog pattern matched this pole even though no log-level
            # detector fired — not a coverage gap; the structural root-cause renders below.
            lines += ["", "(no log-level detector fired, but a **structural catalog "
                      "pattern** matched this pole - see the **structural root-cause** "
                      "below; the dominant step is the addressable lever.)"]
        elif data_driven_present and leaf is None:
            # A data-driven catalog pattern (e.g. OPT24) fired ON this pole — not a coverage
            # gap; it's sized in the 'Also noticed' appendix. `_dd_tail` matches the appendix's
            # on-path vs opt-in/rare framing so the pointer can't contradict the spine footnote.
            lines += ["", "(no log-level detector fired, but a measured **catalog "
                      "pattern** matched this pole - see its entry in the **Also noticed** "
                      "section below, " + _dd_tail]
        elif not log_present:
            lines += ["", f"(no captured log for this job — run with `--log "
                      f"{wf_base.split('.')[0]}=<job log>` to drill into `{dom}`.)"]
        elif leaf is None and analysis_present:
            lines += ["", "(no catalog pattern matched this job's log - see the **LLM "
                      "root-cause analysis** below, which reads the captured log "
                      "directly.)"]
        elif leaf is None:
            lines += ["", "(captured this job's log but matched no known root-cause "
                      "pattern — no drill-down available; this is a coverage gap, not a "
                      "clean job. The detector set may need extending for this stack.)"]
        return lines
    # Drill into the BIGGEST step (it's only part of the job - the other steps are
    # above it in the bars).
    deeper[0]["header"] = f"Level 3 — inside `{_lbl(dom)}`: {leaf['unit_label']}"
    _emit_level(lines, rows2, header_below=deeper[0]["header"])
    for i, lvl in enumerate(deeper):
        last = i == len(deeper) - 1
        scale = lvl.get("scale_to_secs")
        if scale is None and lvl.get("scale_to_step"):
            scale = dom_p50
        _emit_level(lines, lvl["rows"],
                    header_below=None if last else deeper[i + 1]["header"],
                    blocker_note=lvl["blocker_note"] if last else "",
                    pct_of=lvl.get("pct_of", ""), scale_to=scale)
    return lines


def _audit_links(timeline: dict[str, Any] | None, pole: dict[str, Any],
                 leaf: dict[str, Any] | None, run_url: str | None) -> list[str]:
    """A deep-link audit trail so the drill is verifiable: representative run → its
    job → the dominant step's log. GitHub only anchors to the STEP (not a log line),
    and that step's log is long, so the note tells the reader to search it for the
    verbatim evidence (shown below) rather than implying it's at the top. [] when
    there's no job URL to anchor to."""
    if not isinstance(timeline, dict):
        return []
    job_url = str(timeline.get("job_url") or "")
    run = str(run_url or timeline.get("run_url") or "")
    if not job_url and not run:
        return []
    rid = run.rstrip("/").rsplit("/", 1)[-1] if run else ""
    check = _clean_label(str(pole.get("check", "")))
    dom = str(pole.get("dominant_step") or "")
    cl = _clean_label(dom).lower()
    num = None
    for s in timeline.get("steps") or []:
        nm = _clean_label(str(s.get("name", ""))).lower()
        if cl and (nm == cl or cl in nm) and s.get("number") is not None:
            num = s.get("number")
            break
    parts: list[str] = []
    if rid and run:
        parts.append(f"run [{rid}]({run})")
    if job_url:
        parts.append(f"[the `{_lbl(check)}` job]({job_url})")
    if job_url and num is not None:
        parts.append(f"[the `{_lbl(dom)}` step]({job_url}#step:{num}:1)")
    if not parts:
        return []
    # The bars above are plain-English labels; give the VERBATIM log strings to
    # Ctrl-F so the reader can actually find each callout on the page (e.g. the
    # `DB migrations` bar is logged as `Total Migration Time:`).
    terms = [f"`{t}`" for t in ((leaf or {}).get("search") or []) if t]
    if terms:
        how = (" — GitHub anchors to the step, not a log line, so **expand the step** "
               "and search (Ctrl-F) for " + " and ".join(terms)
               + " (the bars above are plain-English labels for these log lines).")
    elif leaf is not None:
        how = " — open the step and search for the **🔬 Evidence** lines below."
    else:
        # No detector matched, so there are no Evidence lines / search terms to point
        # at - just send the reader to the step's own log.
        how = (" — open the step to inspect its log directly (no known root-cause "
               "pattern matched, so there is no specific callout to search for).")
    return ["**🔗 Audit:** " + " → ".join(parts) + how, ""]


def _detectors_skipped_lines(doc: dict[str, Any]) -> list[str]:
    """Name every workflow whose detectors could NOT be evaluated, and say so as
    UNKNOWN — never as clean.

    A detector that was skipped emits no finding, and a finding that never appears
    reads exactly like a finding that looked and found nothing. `collect_runs` refuses
    to size the run-elimination family against a laundered empty run list (that would
    report "0 superseded runs of 0 runs sampled" off a gh timeout), but the refusal is
    only honest if the reader is TOLD. The generic gh-failure note cannot do this job:
    it says a few runs are absent and the P50s are marginally thinner, which is a
    different — and here simply false — failure. So this is its own named bullet, keyed
    off `data_sources.detectors_skipped`, listing the workflow and the detectors.

    Deliberately ADDITIVE (a separate bullet, its own data key, no shared state with
    the gh-error note) so it composes with, rather than fights, the coverage-note
    rewrite in PR #212."""
    ds = doc.get("data_sources") or {}
    skipped = ds.get("detectors_skipped")
    if not isinstance(skipped, list) or not skipped:
        return []
    out: list[str] = []
    for entry in skipped:
        if not isinstance(entry, dict):
            continue
        wf = str(entry.get("workflow") or "")
        dets = [str(d) for d in (entry.get("detectors") or [])]
        if not wf or not dets:
            continue
        reason = str(entry.get("reason") or "its run list could not be fetched")
        out.append(
            f"> - ⚠️ **`{_wf_base(wf)}`: {'/'.join(dets)} did not run.** These detectors "
            f"were NOT evaluated for this workflow — {reason}. Their absence from this "
            "report is **UNKNOWN, not clean**: re-run waste, superseded runs, "
            "double-triggers and schedule burn on this workflow are unmeasured here, "
            "not measured-at-zero.")
    return out


def _yaml_skew_lines(doc: dict[str, Any]) -> list[str]:
    """Name the workflow-YAML branch skew, when the checkout the detectors parsed is on
    a different commit line than the branch the sampled runs came from.

    The dirty check (`<sha>-dirty`) catches UNCOMMITTED edits only. A clean checkout on
    a feature branch, or a `main` that hasn't been pulled in weeks, is not dirty — the
    stamped sha is true — and yet every `on:`/matrix/timeout signal was read from YAML
    that produced none of the timings. That is a fact about the report."""
    ds = doc.get("data_sources") or {}
    skew = ds.get("workflow_yaml_skew")
    if not isinstance(skew, dict) or not skew.get("reason"):
        return []
    return [f"> - ⚠️ **Workflow YAML / run-sample branch skew:** {skew['reason']}. The "
            "detectors parsed the checkout's YAML, while every timing comes from runs "
            "of the default branch — so a trigger, matrix or timeout changed since this "
            "checkout is measured but not parsed (or parsed but not measured). Re-run "
            "from a current default-branch checkout if that matters here."]


def _provenance_block(doc: dict[str, Any], repo: str, captured_at: str) -> list[str]:
    """Spell out WHERE the numbers come from: levels 1–2 are P50 from the committed
    ci-speedup audit (its own sample size + window + gate-sample completeness);
    the deeper levels + cross-run samples are job logs fetched separately."""
    ds = doc.get("data_sources") or {}
    cp = doc.get("pr_critical_path") or {}
    # ENG-1 PR-N2: when the headline gate is a `needs:` chain, the block that
    # defines the word "gate" for the reader must say so — the per-check
    # orderings below rank INDIVIDUAL checks; chain members' times ADD.
    _pv_chs = cp.get("chain_summary") or {}
    _pv_modal = [str(m) for m in (_pv_chs.get("modal_chain") or [])]
    scanned = str(doc.get("scanned_at") or "")[:10]
    runs, jobs, wfs = ds.get("runs_sampled"), ds.get("jobs_sampled"), ds.get("workflows_analyzed")
    cutoff = ds.get("sampled_runs_created_before")
    spr, tgt = cp.get("sampled_pr_count"), cp.get("sample_target")
    complete = cp.get("sample_complete", True)
    fetched = cp.get("sample_fetched")
    fetch_failures = cp.get("sample_fetch_failures") or 0
    # PRs the walk actually got a verdict on (passed or partial-suite). Failed
    # fetches are disclosed in their own coverage-gap bullet below, so excluding
    # them here keeps the deep-scan note's "scanned N to find M full-suite PRs"
    # count from conflating fetch failures with partial-suite rejections.
    evaluated = fetched - fetch_failures if fetched else fetched
    out = ["> **Where this data comes from**", ">"]
    win = f", created ≤ {str(cutoff)[:10]}" if cutoff else " (latest runs at scan time)"
    out.append(f"> - **Critical path + step P50:** the committed ci-speedup "
               f"audit of `{repo}`, scanned **{scanned}** — P50 over "
               f"**{_count_noun(runs, 'run')} / {_count_noun(jobs, 'job')}** across "
               f"{_count_noun(wfs, 'workflow')}{win}.")

    def _ds_count(count_key: str, list_key: str) -> int:
        value = ds.get(count_key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        items = ds.get(list_key)
        return len(items) if isinstance(items, list) else 0

    # Make the data-collection COST visible — the answer to "why did this take a
    # while" lives in the artifact, not a guess. Adaptive 2-pass: a shallow sample of
    # every workflow, then the top pole candidates deepened to full depth (the floor a
    # runner-scoped P50 + bimodal split needs), so the call count tracks the workflows
    # plus a handful of deepened poles, not full depth × every workflow.
    calls = ds.get("gh_query_count")
    cost_s = (doc.get("timings") or {}).get("scripted_total_s")
    if calls:
        cost = f"**{calls} gh API call(s)**"
        if cost_s:
            cost += f" in ~{_clock(cost_s)}"
        errs = ds.get("gh_error_count") or 0
        if errs:
            cost += f" ({errs} failed — see the coverage note)"
        # Four honest states (never launder reduced coverage as full, never call an
        # exact gate shallow): a genuine full pass; adaptive with deepening; pole
        # candidates that already fit within the shallow depth (so they're exact and
        # nothing needed deepening) while other workflows stayed shallow; or no PR-gating
        # pole at all (lowest coverage). Keyed off `capped` + candidate/deepen counts.
        shallow = ds.get("shallow_runs")
        deep = _ds_count("deepened_workflows", "full_depth_workflows")
        cost_deep = _ds_count("cost_deepened_workflow_count", "cost_deepened_workflows")
        mx, capped = ds.get("max_runs"), ds.get("shallow_capped")
        cand = ds.get("pole_candidates") or 0
        converged = ds.get("deepen_converged") is not False
        if capped and (deep or cost_deep):
            how = f"adaptive sampling — a {shallow}-run shallow pass over every workflow"
            if deep:
                how += (f", then {deep} of {cand} PR-gating pole candidate(s) deepened "
                        f"to {mx} runs")
            elif cand:
                how += (f"; the rendered PR-gating poles fit within {shallow} runs "
                        "(full-depth, nothing to deepen)")
            else:
                how += "; no PR-gating pole workflow was deepened"
            if cost_deep:
                how += (f", plus {cost_deep} bill-pole workflow candidate(s) deepened "
                        f"to {mx} runs for the runner-minute source block")
            if deep and converged:
                how += (" (the gate, drill-set, and floor are full-depth; other "
                        "finding-level values may still rest on the shallow sample)")
            elif deep:
                how += (" (some PR-gating workflows were deepened, but convergence did "
                        "not prove the gate/floor full-depth)")
            else:
                how += " (wall-clock finding values still follow the shallow/deepened split)"
            if not converged:
                how += " — ⚠️ deepen did not fully converge; treat the ranking as a floor"
        elif capped and cand:
            how = (f"adaptive sampling — a {shallow}-run shallow pass; the rendered PR-gating "
                   f"poles fit within {shallow} runs (full-depth, nothing to deepen), while "
                   "other workflows stayed at the shallow sample")
        elif capped:
            how = (f"⚠️ a {shallow}-run shallow pass over every workflow; **none** ranked as "
                   f"a PR-gating pole, so nothing was deepened to {mx} — every P50 rests on "
                   "the shallow sample")
        else:
            how = "one jobs fetch per sampled run (fetched concurrently)"
        out.append(f"> - **Data-collection cost:** {cost} — {how}.")
    # Run-list triage disclosure — OUTSIDE the `if calls:` cost block so it renders even on
    # a re-render whose doc lacks `gh_query_count` (no-silent-drop must not hinge on the
    # cost line). Fast workflows whose per-run job fetch was skipped because their slowest
    # sampled run finished under the pole-scale floor: gate/pole unaffected (those live in
    # the slow workflows), and the cross-workflow floor still counts them via their run-list
    # wall — only their job-level hygiene/queue are run-list-only.
    # Prefetch drift — a plan that fetched responses no call site consumed. Zero on a
    # healthy run (the normal case, so nothing renders); non-zero means the parallel fetch
    # pass paid for gh calls the serial path would never have made. It cannot corrupt the
    # sampled data (a prefetch changes WHEN a call is issued, never which), but it is wasted
    # rate-limit budget and it is the signature of a plan/call-site drift bug — so it is
    # disclosed in the artifact rather than left in a stderr line nobody kept.
    # `verify_report.py` fails on it; this renders it for a human.
    _unconsumed = ds.get("prefetch_unconsumed") or 0
    if _unconsumed:
        out.append(f"> - **⚠️ Prefetch drift:** {_unconsumed} gh response(s) were fetched "
                   "but never consumed — a fetch plan disagreed with its call site. The "
                   "measured data below is unaffected (the same calls were still made and "
                   "read), but the run paid for extra API calls; please report this.")
    _triaged = ds.get("triaged_fast_workflows") or []
    if _triaged:
        _shown = ", ".join(f"`{_wf_base(w)}`" for w in _triaged[:4])
        _more = f" +{len(_triaged) - 4} more" if len(_triaged) > 4 else ""
        out.append(f"> - **Fast workflows triaged (no job fetch):** {len(_triaged)} "
                   f"workflow(s) whose slowest sampled run finished under the long-pole "
                   f"floor — {_shown}{_more}. They can't hold the merge pole, so their "
                   "jobs weren't fetched (saving calls); their off-path hygiene/queue "
                   "figures are run-list-only, not job-level.")
    gate = (f"> - **Which checks gate (the critical-path ordering):** measured from **{spr}/{tgt} "
            "sampled PRs**")
    if not complete:
        gate += (" — ⚠️ **short sample** (fewer recent PRs ran the full required suite), "
                 "so the gate ordering here is a floor, not the full picture")
    elif (evaluated and spr and evaluated >= spr * 3
          and not cp.get("required_suite_unsatisfiable")):
        # A complete sample can still require evaluating far more recent PRs than it
        # kept, when most ran only a partial required suite. Count only PRs we got a
        # verdict on (failed fetches are surfaced separately), state the span
        # factually — we know the scan count, not the wall-clock age — and reserve
        # the note for a genuinely deep scan (>=3x kept) so a routine ~50%
        # partial-suite rate doesn't trip it on every active repo. Gated on NOT
        # `required_suite_unsatisfiable`: when the required suite ran on ZERO sampled
        # PRs (recency-only promotion), "found {spr} that ran the full required suite"
        # is false — zero did — so the claim must not render (it would contradict the
        # PR-floor demotion note ~20 lines below).
        gate += (f" — scanned **{evaluated} recent PRs** to find {spr} that ran the "
                 "full required suite, so it spans more PRs than the kept count alone "
                 "suggests")
    out.append(gate + ".")
    if len(_pv_modal) >= 2:
        _pv_label = " → ".join(f"`{m}`" for m in _pv_modal)
        out.append(f"> - **Gate chain:** the headline gate is the {_pv_label} "
                   "chain (`needs:`-serialized — members' times ADD); the "
                   "report's per-check orderings rank individual checks by their "
                   "own time.")
    if fetch_failures:
        out.append(f"> - ⚠️ **{fetch_failures} PR check-run fetch(es) failed** "
                   "(coverage gap — a gh error/timeout or rate-limiting, more likely "
                   "on the deeper walk); those PRs are excluded from the gate sample, "
                   "not laundered into 'ran nothing / clean'.")
    jobs_ff = ds.get("jobs_fetch_failures") or 0
    if jobs_ff:
        out.append(f"> - ⚠️ **{jobs_ff} run job-timing fetch(es) failed** "
                   "(coverage gap — a gh error/timeout, more likely under the "
                   "concurrent per-run jobs sampling); those runs are excluded from "
                   "the level-1–2 P50 sample, not laundered into 'ran no jobs / clean'.")
    out += _detectors_skipped_lines(doc)
    out += _yaml_skew_lines(doc)
    # `required_suite_scoped is False` has TWO causes, and they need different prose:
    # the required suite was read but is external/managed and ran on no sampled PR
    # (the PR-floor fallback), vs. it couldn't be read at all (no admin / 404). Branch
    # on `required_suite_unsatisfiable` so an external-but-readable gate isn't reported
    # as a permissions failure.
    if cp.get("required_suite_unsatisfiable"):
        out.append("> - ⚠️ **No file-backed required gate** — the required checks were "
                   "read, but they're external/managed (a CLA bot, enterprise CI, "
                   "label-gated e2e, a mergeability gate) and ran on **none** of the "
                   "sampled PRs, so there's no required-check critical path. The spine "
                   "is the measured **PR-floor** — the file-backed work a normal PR "
                   "runs — not the branch-protection gate.")
    elif cp.get("required_suite_scoped") is False:
        out.append("> - ⚠️ **Required checks were unreadable** (no admin / branch "
                   "protection 404), so 'gate' here means the **slowest check on a "
                   "typical PR** (observed), not a *confirmed required* check. Slow "
                   "checks that run on only a minority of PRs are shown as a footnote, "
                   "not the headline.")
    dropped = cp.get("dropped_non_pr_checks") or []
    if dropped:
        shown = ", ".join(f"`{_clean_label(n)}`" for n in dropped[:4])
        more = f" +{len(dropped) - 4} more" if len(dropped) > 4 else ""
        out.append(f"> - **Excluded from the gate:** {len(dropped)} check(s) that run "
                   f"only on push/schedule (not PRs) - {shown}{more}. Their check-runs "
                   "ride along on sampled commits but the developer never waits on them "
                   "to merge.")
    # Required-scope narrowing — same "visible, never silent" bar as the non-PR drop
    # above: when the spine was scoped to the merge-blocking (required-reachable) checks,
    # name what was set aside so a slow-but-non-gating check isn't silently absent.
    dropped_nr = cp.get("dropped_non_required_checks") or []
    if dropped_nr:
        shown = ", ".join(f"`{_clean_label(n)}`" for n in dropped_nr[:4])
        more = f" +{len(dropped_nr) - 4} more" if len(dropped_nr) > 4 else ""
        out.append(f"> - **Narrowed to merge-blocking checks:** {len(dropped_nr)} "
                   f"non-required check(s) excluded from the critical path - {shown}"
                   f"{more}. They run on PRs but aren't required and nothing required "
                   "`needs:` them, so speeding them moves no time-to-merge (they remain "
                   "in the findings JSON + any off-path hygiene).")
    # No silent drop (sibling to the narrowing footnote above): a REQUIRED check that never
    # appeared as a check-run in the sampled window was excluded from the per-PR suite test
    # (a status-only / external gate can't be sampled). Name it so the reader sees the gate
    # was measured on the OBSERVABLE required subset — the excluded check is NOT confirmed
    # satisfied here, just unmeasurable — instead of it being silently absent.
    unobs = cp.get("required_checks_unobservable") or []
    if unobs:
        shown = ", ".join(f"`{_clean_label(n)}`" for n in unobs[:4])
        more = f" +{len(unobs) - 4} more" if len(unobs) > 4 else ""
        out.append(f"> - ⚠️ **Status-only required check(s) excluded from the suite test:** "
                   f"{shown}{more} — never posted as a check-run in the sampled window (a "
                   "GitHub-App commit status / external gate), so they can't be timed. The "
                   "gate was measured on the **observable** required subset; these aren't on "
                   "the critical path and aren't confirmed satisfied here, just unmeasurable.")
    if captured_at:
        out.append(f"> - **Step internals + cross-run checks (the per-pole drill-downs):** the "
                   f"pole jobs' raw logs, fetched **{captured_at}** (newer than the "
                   "critical-path audit above). Each drill-down is **one representative "
                   "run** of that job - the one closest to its typical time (for a "
                   "bimodal job, a representative of the slow mode the drill explains), "
                   "linked + labelled per pole - and the **Cross-run check** validates "
                   "the load-bearing magnitude (median + range) across several runs.")
    out.append("")
    return out


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _iqr(xs: list[float]) -> float:
    """Inter-quartile range = median of the upper half − median of the lower half.
    Robust to a single outlier, unlike max−min — which is the whole point once a
    widened sample is in hand (a tight cluster + one stray must not read as 'wide')."""
    s = sorted(xs)
    n = len(s)
    if n < 4:
        return max(s) - min(s) if s else 0.0
    return _median(s[(n + 1) // 2:]) - _median(s[: n // 2])


def _bimodal_note(checks: list[dict[str, Any]], gate_p50: float | None = None) -> list[str]:
    """Surface checks whose duration is BIMODAL and whose median landed on the FAST
    mode — so the level-1 bar (a P50) under-states them and the P50 ranking can drop
    them below the spine even though they are a long gate on a large share of PRs.
    `collect_runs` attaches `bimodal: {low_p50_s, high_p50_s, slow_frac, ...}` to a
    check from its uncapped job sample. [] when no shown check is fast-median bimodal.

    A `gate_p50` materiality threshold keeps the note to checks whose SLOW mode is a
    genuinely long gate (>= half the headline gate) — not a 2m job that's bimodal
    only between 70s and 135s."""
    material = 0.5 * gate_p50 if gate_p50 else 0.0
    rows: list[tuple[str, float, float, float, float]] = []
    for c in checks:
        bi = c.get("bimodal")
        if not isinstance(bi, dict):
            continue
        lo, hi, frac = _num(bi.get("low_p50_s")), _num(bi.get("high_p50_s")), _num(bi.get("slow_frac"))
        p50 = _num(c.get("p50_s"))
        if not lo or not hi or not frac or p50 is None:
            continue
        # Only flag when the MEDIAN sits on the fast mode (p50 nearer the low cluster):
        # then the bar hides the slow mode. A check whose median is already the slow
        # mode is shown at its slow time, so there's nothing hidden to surface.
        if p50 > (lo + hi) / 2:
            continue
        # ...and only when the slow mode is a materially long gate (would rank near the
        # spine on its slow PRs), so trivial fast-job bimodality isn't surfaced.
        if hi < material:
            continue
        rows.append((_clean_label(str(c.get("name", ""))), p50, lo, hi, frac))
    if not rows:
        return []
    out = ["> **⚠️ Bimodal gates (P50 hides a frequent slow mode).** These checks rank "
           "low by median — so the P50 spine puts them below the gate, and some aren't "
           "in the Contents critical-path list above — but each is a long gate on a "
           "large share of PRs:"]
    for name, p50, _lo, hi, frac in rows:
        out.append(f"> - `{name}` — P50 only **{_clock(p50)}**, but **~{_clock(hi)}** on "
                   f"~{round(frac * 100)}% of PRs; on those it is among the slowest gates "
                   "(median-ranked low, so easy to miss).")
    out.append("")
    return out


def _match_key(d: dict[str, Any], wf_base: str, check: str = "") -> Any:
    """Match a per-pole --log/--steps/--mag KEY to a pole. An EXACT workflow-stem
    match wins (key `ci` binds `ci.yml`, never `lint-ci.yml`); otherwise the longest
    substring key that appears in the pole's JOB/check name OR its workflow filename.
    Matching the JOB name (not just the workflow) is what lets two poles in the SAME
    workflow get distinct logs - e.g. keys `tests-web` and `docker` each bind their own
    `pipeline.yml` job. Order-independent; None when nothing matches."""
    wf_stem = wf_base.split(".")[0]
    exact = [v for k, v in d.items() if k == wf_stem]
    if exact:
        return exact[0]
    hay = (check + " " + wf_base).lower()
    cands = [(len(k), v) for k, v in d.items() if k and k.lower() in hay]
    return max(cands, key=lambda kv: kv[0])[1] if cands else None


# The render literals a cache pole's framing turns on, hoisted so the coupling test can
# pin them against verify_report's `_VR_*` copies (Lesson L7: a reword must break a
# coupling test, not silently unhook the guard). `_BIGGEST_LEVER` / `_CHURN_HINT` are the
# substrings `_apply_cache_dist` strips when the measured distribution says the cache is
# not the lever; `_CACHE_CONTEXT_MARKER` is the invisible marker verify_report requires
# on a demoted (miss-tail / mostly-warm) cache pole (mirrors the `_STATIC_ONLY_MARKER`
# machine-marker pattern).
_BIGGEST_LEVER = " - BIGGEST LEVER"
_CHURN_HINT = " (cache-key churn?)"
_CACHE_CONTEXT_MARKER = "<!-- ci-speedup:cache-context -->"

# The one-line verdict sentence rendered under the cache-health block, per verdict.
_CACHE_VERDICT_SENTENCE = {
    "cold": "The cache is cold across sampled runs — every run rebuilds, so this is a "
            "broad wall-clock lever.",
    "churn": "The miss rate is high across sampled runs, not just the drilled one — the "
             "cache key looks unstable (churn).",
    "miss-tail": "Most sampled PRs hit the cache; the miss cost falls on a cache-miss-heavy "
                 "minority — this helps those runs, not the typical warm run.",
    "mostly-warm": "The cache mostly HITS across sampled PRs — the drilled run is a "
                   "minority slow mode, so the cache is not the dominant lever here.",
    "insufficient": "Too few sampled runs exposed a cache summary to judge the hit rate "
                    "across runs.",
}
# Rendered when a cache pole's savings are cache-context-dependent (miss-tail / mostly-warm),
# so a reader never sizes a fix off the cold/fork drilled run.
_CACHE_CAVEAT = (
    "⚠ **Cache-context caveat:** this saving applies to cache-miss-heavy runs, not the "
    "typical warm-cache run — size and benchmark any fix on an upstream PR with the "
    "production cache warm, never from a fork or a cold clone.")


def _apply_cache_dist(leaf: dict[str, Any] | None,
                      cache_dist: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reframe a cache leaf IN PLACE to match its measured cross-run distribution
    (`cache_dist.verdict`), so a mostly-warm cache can't render as a top miss/churn lever
    off the single (slow-mode) drilled run. Keyed ONLY off the re-derivable verdict:

      cold / churn  → leaf untouched (the drilled miss IS the typical case); the
                      lifecycle leaf, which hard-codes no lever, gains "BIGGEST LEVER".
      miss-tail     → strip the "BIGGEST LEVER" label + the "(cache-key churn?)" hint,
                      disclose the drilled run is the miss-heavy minority.
      mostly-warm   → strip both + reframe the note around the warm median.
      insufficient  → strip both + disclose the single-run basis (fewer than 2 upstream runs
                      exposed a cache summary, so the drilled run's native churn/lever framing is
                      NOT cross-run grounded and must not ship as if it were).
      absent (no `cache_dist` field, or a dict with no `verdict`) → no-op (backward compatible
                      with pre-cache_dist findings).

    The caveat + machine marker are emitted by `_cache_health_block`, not here."""
    if not leaf or not isinstance(cache_dist, dict):
        return leaf
    if leaf.get("fix_key") not in _CACHE_LEAF_KEYS:
        return leaf
    verdict = cache_dist.get("verdict")
    deeper = leaf.get("deeper") or []
    if verdict in ("cold", "churn"):
        if leaf.get("fix_key") == "install-lifecycle-build":
            for d in deeper:
                note = d.get("blocker_note")
                if note and _BIGGEST_LEVER.strip() not in note:
                    d["blocker_note"] = note + _BIGGEST_LEVER
        return leaf
    if verdict == "insufficient":
        # Too few upstream (non-fork) runs exposed a cache summary to establish a cross-run hit
        # rate. The drilled run's native "BIGGEST LEVER" / "(cache-key churn?)" framing is a
        # single-run guess, not a grounded claim — strip the over-claim and disclose the basis so
        # a reader never sizes a fix off one deliberately-slow run. (A dict `cache_dist` with no
        # `verdict` key is malformed/pre-schema and falls through to the no-op return below.)
        for d in deeper:
            note = d.get("blocker_note")
            if not note:
                continue
            note = note.replace(_BIGGEST_LEVER, "").replace(_CHURN_HINT, "")
            note += (" - basis: a single sampled run's log; too few runs exposed a cache summary "
                     "to establish the cross-run hit rate, so this may not be the typical run")
            d["blocker_note"] = note
        return leaf
    if verdict in ("miss-tail", "mostly-warm"):
        med = _num(_as_dict(cache_dist.get("pr")).get("upstream_median"))
        for d in deeper:
            note = d.get("blocker_note")
            if not note:
                continue
            note = note.replace(_BIGGEST_LEVER, "").replace(_CHURN_HINT, "")
            if verdict == "mostly-warm":
                # The cache mostly HITS across sampled PRs regardless of whether a numeric median
                # is available — never fall through to the miss-tail wording (which would tell the
                # reader the drilled run is a miss-heavy minority, contradicting "mostly warm").
                note += " - but the cache mostly HITS across sampled PRs"
                if med is not None:
                    note += f" (median miss {_pct_disp(med)})"
            else:
                note += " - drilled run is from the miss-heavy minority (see cache health below)"
            d["blocker_note"] = note
    return leaf


def _cache_health_block(cache_dist: dict[str, Any] | None) -> list[str]:
    """Render the "Cache health across runs/events" block from a pole's stamped
    `cache_dist`: the PR-bucket miss median (fork runs excluded + labelled), the push
    (default-branch) miss median or a "no push runs" note, the miss-heavy tail
    prevalence, and the one-line verdict. For a demoted verdict (miss-tail / mostly-warm)
    it also emits the cache-context caveat + the machine marker verify_report requires.
    [] when the pole carries no cache_dist (backward compatible) OR the verdict is `insufficient`
    (too few runs exposed a summary to judge — the leaf framing was left unchanged, so there is
    nothing measured to disclose here)."""
    if not isinstance(cache_dist, dict):
        return []
    verdict = cache_dist.get("verdict")
    if verdict in (None, "insufficient"):
        return []   # nothing measured to disclose; leaf framing was left unchanged
    pr = _as_dict(cache_dist.get("pr"))
    out = ["**🗃️ Cache health across runs/events**", ""]
    med = _num(pr.get("upstream_median"))
    rng = pr.get("upstream_range")
    # The median is over the runs that exposed a cache summary (`upstream_n`), NOT every sampled
    # run (`n`): a run whose log had no parseable summary isn't behind the median, so counting it
    # would overstate the agreement. Disclose those separately (`no_summary_n`).
    up_n = pr.get("upstream_n")
    up_n = up_n if isinstance(up_n, int) else pr.get("n")
    no_summary_n = pr.get("no_summary_n") or 0
    fork_n = pr.get("fork_n") or 0
    pr_line = "- Pull requests: "
    if med is not None:
        pr_line += f"median miss **{_pct_disp(med)}**"
        if isinstance(rng, list) and len(rng) == 2:
            pr_line += f" (range {_pct_disp(_num(rng[0]) or 0.0)}–{_pct_disp(_num(rng[1]) or 0.0)})"
        pr_line += f" across {up_n} sampled run(s)"
    else:
        pr_line += "no upstream run exposed a cache summary"
    if fork_n:
        pr_line += (f"; {fork_n} fork-PR run(s) excluded from the median "
                    "(a fork PR cannot read the repo cache)")
    if no_summary_n:
        pr_line += f"; {no_summary_n} run(s) exposed no cache summary"
    out.append(pr_line)
    push = cache_dist.get("push")
    if isinstance(push, dict):
        pmed = _num(push.get("median"))
        p_err = push.get("errors") or 0
        if pmed is not None:
            out.append(f"- Default branch (push): median miss **{_pct_disp(pmed)}** across "
                       f"{push.get('n')} probed run(s)")
        elif p_err:
            out.append(f"- Default branch (push): unknown — {p_err} log fetch(es) failed "
                       "(cache-on-main not measured)")
        else:
            out.append("- Default branch (push): probed runs exposed no cache summary")
    else:
        out.append(f"- Default branch (push): "
                   f"{cache_dist.get('push_reason') or 'no push runs sampled'}")
    tail = _as_dict(cache_dist.get("tail"))
    prev = _num(tail.get("prevalence_max"))
    if prev:
        out.append(f"- Miss-heavy runs are at most ~{prev * 100:.0f}% of qualifying PR "
                   "runs (duration-bound estimate)")
    out += ["", _CACHE_VERDICT_SENTENCE.get(verdict, "")]
    if verdict in ("miss-tail", "mostly-warm"):
        out += ["", _CACHE_CAVEAT, "", _CACHE_CONTEXT_MARKER]
    out.append("")
    return out


# --- The off-category leaf-hijack class (issue #16) ---------------------------------
# A whole-log `_parse_log` leaf fires on a TOOL MARKER anywhere in the joined job log,
# with no check that the tool's work is the pole's DOMINANT measured step. When it is not
# — nrwl/nx's `eslint-no-cache` leaf firing on a TEST-dominated pole whose one combined
# `Run Checks/Lint/Test/Build` step bins as `test`, or sveltejs/svelte's eslint leaf on a
# type-check-dominated Lint pole — the leaf would crown a LINT cache fix as the pole's
# MEASURED CAUSE and pin the pole's full wall-clock ceiling on it, though lint is a
# MINORITY of the measured time. The rule: a leaf may crown the cause / claim the ceiling
# only when its target step-category AGREES with the pole's measured `dominant_category`
# (the crown `collect_runs._decompose_job_steps` computed over the SAME `_step_category`
# taxonomy) AND — for a leaf that shares a coarse category with a distinct sibling tool
# (eslint vs. type-check, both bin `scan`) — the dominant STEP is one the leaf actually
# addresses. Otherwise the leaf is DEMOTED to a secondary observation (never a silent drop)
# and the pole falls back to its generic dominant-step hand-off, which points at the
# measured dominant step. We DEMOTE rather than credit a fractional ceiling: inside a single
# combined step the lint sub-share is unmeasurable from step timing, so any partial ceiling
# would be invented — the honest ceiling is the generic dominant-step wall (which IS
# measured). Mirrors the `_apply_cache_dist` precedent (reframe a leaf off a re-derivable
# measured fact); verify_report re-derives the SAME category agreement from findings.json.

# fix_key -> the `collect_runs._step_category` bin the leaf's fix addresses. Kept coupled to
# the engine taxonomy AND verify_report's mirror by test_verify_report_self.
_LEAF_STEP_CATEGORY: dict[str, str] = {
    "prisma-migrate-once": "test",
    "vitest-v8-coverage": "test",
    "vitest-isolate-pool": "test",
    "playwright-parallel": "test",
    "pytest-no-xdist": "test",
    "cargo-test-shard": "test",
    "benchmark-serial-reruns": "test",
    "android-emulator-shard": "test",
    "gradle-test-parallelism": "test",
    "eslint-no-cache": "scan",
    "turbo-remote-cache": "build",
    "turbo-partial-cache": "build",
    "buildx-no-cache": "build",
    "install-lifecycle-build": "install",
}
# A leaf whose coarse category (`scan`) is SHARED with a distinct sibling tool must, on a
# pole where that category IS dominant, also have the dominant step carry the leaf's own
# token — else it is demoted. This separates eslint (lint) from a type-check step (both
# bin `scan`), catching the sveltejs/svelte instance where scan is dominant but the
# dominant step is the type-check, not the lint.
_LEAF_DOMINANT_STEP_TOKEN: dict[str, "re.Pattern[str]"] = {
    "eslint-no-cache": re.compile(r"lint", re.IGNORECASE),
}
# Machine markers (HTML comments, like `_CACHE_CONTEXT_MARKER`): the crown marker records
# the fix_key of the leaf that crowned a pole's MEASURED CAUSE so verify_report can
# re-derive its category and check it against the pole's measured dominant_category; the
# off-category marker tags the demoted secondary-observation block.
_LEAF_CROWN_MARKER = "<!-- ci-speedup:leaf-crown fix_key={fk} -->"
_OFFCATEGORY_MARKER = "<!-- ci-speedup:offcategory-leaf -->"


def _offcategory_leaf(leaf: dict[str, Any] | None, pole: dict[str, Any]) -> bool:
    """True when `leaf`'s target step-category CONTRADICTS the pole's measured
    `dominant_category` (or the dominant step is a sibling tool the leaf does not address),
    so the leaf must not crown the pole's cause or pin its ceiling. Fails OPEN (False) when
    the leaf's category is unknown or the pole carries no decomposition — never demote
    without a measured contradiction to point at."""
    if not leaf:
        return False
    fk = str(leaf.get("fix_key", ""))
    cat = _LEAF_STEP_CATEGORY.get(fk)
    if cat is None:
        return False
    dom_cat = pole.get("dominant_category")
    if not isinstance(dom_cat, str) or not dom_cat:
        return False
    if cat != dom_cat:
        return True
    tok = _LEAF_DOMINANT_STEP_TOKEN.get(fk)
    if tok is not None:
        dom_step = str(pole.get("dominant_step") or "")
        if dom_step and not tok.search(dom_step):
            return True
    return False


def _demote_offcategory_leaf(
    leaf: dict[str, Any] | None, pole: dict[str, Any],
) -> "tuple[dict[str, Any] | None, dict[str, Any] | None]":
    """Split a crowning leaf from a DEMOTED one. `(leaf, None)` keeps the crown; `(None,
    leaf)` when `_offcategory_leaf` fires — the caller renders the second element as a
    secondary observation instead of the pole's MEASURED CAUSE."""
    if leaf is not None and _offcategory_leaf(leaf, pole):
        return None, leaf
    return leaf, None


def _derive_pole_leaf(
    pole: dict[str, Any], owner_key: str | None, logs: dict[str, str],
) -> "tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]":
    """The ONE leaf-derivation pipeline a pole's log runs through — captured here so the
    Long pole map (which drills the descent pole = pole 1 up top) and the per-pole loop
    below share the IDENTICAL `(log_text, leaf, offcat_leaf)` result, never two forks that
    could disagree. Mirrors the loop verbatim: bind by the pole's OWN owner key (an undrilled
    pole owns none → no log), parse, reconcile the cache-hit distribution, then split off an
    off-category leaf. Pure over its inputs, so calling it twice on the same pole is exact."""
    log_text = logs.get(owner_key) if owner_key is not None else None
    leaf = _parse_log(log_text) if log_text else None
    leaf = _apply_cache_dist(leaf, pole.get("cache_dist"))
    leaf, offcat_leaf = _demote_offcategory_leaf(leaf, pole)
    return log_text, leaf, offcat_leaf


def _offcategory_note_block(leaf: dict[str, Any], pole: dict[str, Any]) -> list[str]:
    """Render a demoted off-category leaf as a labelled secondary observation (never a
    silent drop): what matched, why it is NOT the measured cause (the pole's dominant
    measured step is different work the leaf's fix does not touch), and the verbatim
    evidence. Carries `_OFFCATEGORY_MARKER` so verify_report can recognize the demotion."""
    dom_step = _fence_safe(str(pole.get("dominant_step") or "the dominant step"))
    dom_cat = str(pole.get("dominant_category") or "")
    fk = str(leaf.get("fix_key", ""))
    cat = _LEAF_STEP_CATEGORY.get(fk, "")
    out = [_OFFCATEGORY_MARKER, "",
           "**🔎 Secondary observation — not the measured cause.** A log detector matched "
           f"`{fk}` (a `{cat}` fix) in this job's log, but the pole's measured dominant work "
           f"is the `{dom_step}` step (`{dom_cat}`), which this fix does not address — so it "
           "is not crowned as the cause and is not credited the pole's wall-clock ceiling. "
           "Address the dominant step above first, then treat this as a smaller, separate "
           "cleanup."]
    ev = leaf.get("evidence") or []
    if ev:
        out += ["", "```text", *[_fence_safe(e) for e in ev], "```"]
    out.append("")
    return out


def _mag_line(mag: dict[str, Any] | None, leaf: dict[str, Any] | None) -> list[str]:
    """A one-line cross-run check on the load-bearing magnitude: the typical value +
    its spread across the sampled runs, so the single drilled run's number isn't
    taken on faith. The CATEGORICAL root cause is stable run-to-run; this is how much
    the quantity moves. [] when there's no scalar magnitude (e.g. a sequencing
    finding) or fewer than 2 runs to compare."""
    if not mag or not isinstance(mag, dict):
        return []
    entries = [x for x in (mag.get("values") or []) if _num(x.get("value")) is not None]
    vals = [_num(x.get("value")) for x in entries]
    if len(vals) < 2:
        return []
    unit = str(mag.get("unit", ""))
    fmt = (lambda x: f"{x:.0f}%") if unit == "%" else (lambda x: _clock(x))
    # The median can land on a half (even sample -> mean of the two middles, e.g.
    # 51.5); rounding it to a whole percent (52) reads as disagreeing with runs that
    # cluster at 51, so show one decimal when it isn't a whole number.
    fmtm = ((lambda x: f"{x:.0f}%" if abs(x - round(x)) < 0.05 else f"{x:.1f}%")
            if unit == "%" else (lambda x: _clock(x)))
    n = len(vals)
    med, lo, hi = _median(vals), min(vals), max(vals)
    this = _num(mag.get("this_run"))
    label = str(mag.get("label", "the key magnitude"))
    # Honest labelling: a 3-run probe isn't a real "median" - report the drilled
    # value + the bracket. Only call it a median once the sample is wide enough.
    centre = (f"median **{fmtm(med)}**" if n >= 5
              else f"**{fmt(this if this is not None else med)}** in the drilled run")
    # Verdict the reader acts on. With a widened sample, judge spread by IQR (robust
    # to one stray run); the raw range only labels a small probe. A tight cluster
    # with a lone outlier is "stable", NOT "varies run to run".
    outliers = sum(1 for v in vals if med and abs(v - med) > _MAG_WIDE_REL * med)
    # Degenerate sample: every sampled value collapsed to ~0 while the drilled value is
    # clearly non-zero. The per-run extraction disagrees with the headline (e.g. a
    # duplicate-named step that resolved to the wrong occurrence), so this magnitude is
    # NOT cross-run-validated — never call it "stable across runs". Defensive: the
    # collector now resolves the dominant occurrence, but a future extraction mismatch
    # must surface as unverified rather than assert a false stability. Uses a small
    # RELATIVE floor (not exactly 0) so a near-zero collapse from timestamp rounding (med
    # ~0.001s while this is minutes) is caught too, not just an exact 0.0 median.
    if this is not None and this > 0 and med <= this * 0.01:
        verdict = ("the per-run samples did not line up with the drilled value, so this "
                   "magnitude is NOT cross-run validated")
        tail = ("⚠ Per-run samples below disagree with the drilled value — treat the "
                "magnitude as single-run until this resolves:")
        out = [f"**🔬 Cross-run check** — {label}: {centre}, {n} runs sampled, range "
               f"{fmt(lo)}–{fmt(hi)} ({verdict}). {tail}", ""]
        for x in entries:
            url = str(x.get("run_url", ""))
            rid = url.rstrip("/").rsplit("/", 1)[-1]
            tag = " — drilled above" if x.get("drilled") else ""
            if x.get("fork"):
                tag += " — fork PR (repo cache unavailable)"
            out.append(f"- [run {rid}]({url}) — {fmt(_num(x.get('value')))}{tag}")
        out.append("")
        return out
    if n >= 5:
        wide = (_iqr(vals) / med > _MAG_WIDE_REL) if med else False
        if wide:
            verdict = "a genuinely wide spread — the payoff varies run to run"
        elif outliers:
            plural = "s" if outliers != 1 else ""
            verdict = (f"{n - outliers} of {n} runs cluster near {fmtm(med)}; the "
                       f"{fmt(lo)}–{fmt(hi)} range comes from {outliers} outlier "
                       f"run{plural}, so the number is effectively stable")
        else:
            verdict = "a tight spread, so the number is stable across runs"
    else:
        wide = ((hi - lo) / med > _MAG_WIDE_REL) if med else False
        verdict = ("a tight spread, so the number is stable across runs" if not wide
                   else "a wide spread, so the payoff varies run to run")
    # A generic step-wall magnitude (an undetected pole's dominant step) has no
    # "categorical cause" to assert is stable - it IS the step's measured wall time, so
    # "the payoff varies" (a fix-magnitude phrasing) reads wrong - it's the step's own
    # wall time that varies (a wide spread here usually means the step is cache- or
    # retry-sensitive, which is itself the lead).
    if mag.get("kind") == "step-wall":
        verdict = verdict.replace("the payoff varies run to run",
                                  "the step's wall time varies run to run (often "
                                  "cache- or retry-sensitive)")
        tail = "This is the dominant step's own wall time, measured per run:"
    else:
        tail = "The categorical cause is the same in every run:"
    out = [f"**🔬 Cross-run check** — {label}: {centre}, {n} runs sampled, range "
           f"{fmt(lo)}–{fmt(hi)} ({verdict}). {tail}", ""]
    for x in entries:
        url = str(x.get("run_url", ""))
        rid = url.rstrip("/").rsplit("/", 1)[-1]
        tag = " — drilled above" if x.get("drilled") else ""
        # Same fork disclosure as the NOT-cross-run-validated branch above: a fork PR runs
        # cache-cold, so its miss is a worst-case cold build, not an ordinary upstream point —
        # annotate it so a reader doesn't size the fix off it (the value is still listed, but the
        # cross-run median/verdict already exclude forks). Applying it in BOTH per-run loops.
        if x.get("fork"):
            tag += " — fork PR (repo cache unavailable)"
        out.append(f"- [run {rid}]({url}) — {fmt(_num(x.get('value')))}{tag}")
    out.append("")
    return out


def _gate_counts(cp: dict[str, Any]) -> tuple[dict[str, int], dict[str, int], int]:
    """From the per-PR check populations: how often each check is the SLOWEST one
    (the actual pole) and how often it is PRESENT at all, across the sampled PRs.
    Returns (pole_count, present_count, n_populations). A slow check that only
    runs on a few PRs has a low pole_count and must NOT be crowned over a check
    that gates almost every PR. ({}, {}, 0) when populations weren't recorded."""
    pole_count: dict[str, int] = {}
    present: dict[str, int] = {}
    n = 0
    for entry in cp.get("populations") or []:
        try:
            _share, cks = entry
            pos = [(str(nm), float(p)) for nm, p in cks if float(p) > 0]
        except (TypeError, ValueError):
            continue
        if not pos:
            continue
        n += 1
        for nm, _p in pos:
            present[nm] = present.get(nm, 0) + 1
        winner = max(pos, key=lambda kv: kv[1])[0]
        pole_count[winner] = pole_count.get(winner, 0) + 1
    return pole_count, present, n


def _population_typical_floor(cp: dict[str, Any]) -> float | None:
    """The POPULATION-WEIGHTED typical merge wait: the median over sampled PRs of each
    PR's critical-path MAXIMUM (the slowest check that PR actually ran). When a slow check
    runs on only a FRACTION of PRs, its full conditional p50 overstates what a TYPICAL
    (median) PR waits for "all checks to finish" — the PRs that don't run it finish sooner.
    The median of the per-PR maxima recorded in `populations` is the faithful universal
    floor for that case. Returns None when too few per-PR populations were recorded to form
    a stable median (mirrors the `_RARE_PRESENCE_MIN_PR` presence floor used elsewhere), so
    a repo with no per-PR populations keeps the slowest concurrent check's p50 as the floor."""
    maxima: list[float] = []
    for entry in cp.get("populations") or []:
        try:
            _share, cks = entry
            vals = [float(p) for _nm, p in cks if float(p) > 0]
        except (TypeError, ValueError):
            continue
        if vals:
            maxima.append(max(vals))
    if len(maxima) < _RARE_PRESENCE_MIN_PR:
        return None
    return _median(maxima)


def _toc_block(pole_wfs: list[dict[str, Any]], wf_gate: dict[str, int],
               npop: int, also_count: int | None = None,
               pr_floor: bool = False, queue_count: int = 0,
               also_on_path: bool = False,
               runner_spine_count: int = 0,
               tier2_toc: "dict[str, Any] | None" = None,
               top_is_gate: bool = False) -> list[str]:
    """The report's Contents: the merge-gating long poles (each linking to its
    `#pole-N` drill-down), then a one-line "Pre-start wait" pointer when `queue_count`
    > 0, then a one-line pointer to the off-path "Also noticed" hygiene. `also_count`
    is None until the hygiene appendix is wired in (Phase 2); a 0 renders no hygiene
    line. `queue_count` is the number of pre-start-wait findings (0 renders no wait
    line). `pr_floor` swaps the "gate your merge" framing for the PR-floor one so the
    TOC doesn't contradict the demotion banner. [] when there's nothing to list."""
    if not pole_wfs:
        return []
    intro = ("the file-backed workflows that set your PR-floor (no file-backed required "
             "gate exists)" if pr_floor else "the checks that gate your merge")
    lines = ["## 📋 Contents", "",
             f"**🐌 Critical path** — {intro}, each linking to its long-pole drill-down "
             "(waterfall → biggest lever → agent prompt):", ""]
    for i, p in enumerate(pole_wfs, 1):
        check = _lbl(_clean_label(str(p.get("check", ""))))
        # Use the headline duration (slow mode for a bimodal pole), so the TOC's dot +
        # time match the pole's own header instead of disagreeing (median 🟠 vs slow 🔴).
        head_s, _ = _pole_headline(p)
        dur = _clock(head_s)
        gc = wf_gate.get(str(p.get("workflow_file", "")), 0)
        # The count is a per-WORKFLOW sum (over its matrix legs/jobs), so attribute it
        # to the workflow file, not the single representative check name - a matrix leg
        # like `tests-web (... pg15 ...)` may itself be the literal pole on fewer PRs
        # than its workflow gates, and "<check> gates N" would read as the leg's own
        # count and mismatch a populations recompute.
        wf_base = _wf_base(p.get("workflow_file", ""))
        # Attribute the workflow gate-count ONLY next to a pole the header frames as a gate. For a
        # DEMOTED pole (`_demoted_gate_framing` — the header reads "Rarely the merge gate"), that
        # count is a sibling check's frequency, so printing "`wf` gates N/npop PRs" beside it
        # contradicts the pole's own header (the caddy goreleaser-check case); tag it "rarely the
        # merge pole" instead. Absent stamp (direct `_toc_block` calls) → the gate tail, as before.
        if p.get("_demoted_gate_framing"):
            gates = " · rarely the merge pole"
        elif gc and npop:
            gates = f" · `{wf_base}` gates {gc}/{npop} PRs"
        else:
            gates = ""
        # Plain-text anchor label (owner UX edit 2026-07-19): a backticked `check`
        # inside the link rendered as a code chip that didn't read as a link — align it
        # with the plain-text runner rows. In-body code references stay backticked.
        # Gate signal (owner UX edit 2026-07-19): the removed Level-1 chart marked its
        # top bar with ◀ and captioned it "the gate" only when the frequency gate WAS the
        # slowest single check (never on a `needs:` chain, where the top bar is the
        # slowest single check, not the gate). Carry that exact condition here as a
        # " (the gate)" tag on the first critical-path row.
        _gate_tag = " (the gate)" if (top_is_gate and i == 1) else ""
        lines.append(f"{i}. {_severity_dot(head_s)} [{check}]"
                     f"(#pole-{i}) — {dur}{_gate_tag}{gates}")
    lines.append("")
    if queue_count:
        qp = "s" if queue_count != 1 else ""
        lines += [f"**⏳ Pre-start wait** — {queue_count} job{qp} wait{'' if qp else 's'} "
                  "in queue before starting (developer wall-clock the spine above doesn't "
                  "capture): [see below](#pre-start-wait).", ""]
    if tier2_toc and tier2_toc.get("rows"):
        # First-class entry (owner request): the money section gets the same
        # visual weight as the poles — marker, the de-overlapped total up
        # front, and enumerated rows linking to their `#r-N` anchors. The
        # totals/rows mirror the section's own strings (same helpers, same
        # ranked list), and `check_tier2_total_deoverlapped` re-derives both
        # renderings from findings.json so they can never drift apart.
        spine_part = (f", backed by a {runner_spine_count}-row cost spine"
                      if runner_spine_count else "")
        lines += [f"**💸 Runner-minute reductions** — ~{tier2_toc['total_min']} of "
                  f"measured, merge-safe runner-minute savings{spine_part}: "
                  "[section](#runner-minute-reductions).", ""]
        # Real ordered-list markers, mirroring the Critical path list exactly —
        # "R1."-prefixed plain lines are NOT Markdown list items and GitHub
        # merges consecutive plain lines into one run-on paragraph (shipped
        # once; owner caught it on the rendered page). The list number IS the
        # R-number: item k links to #r-k (the section's "### Rk" header).
        # 🟢 in the pole rows' severity-dot slot (OD12-rev1): every admitted row
        # is certificate-proven merge-safe, so the dot is constant green by
        # design - a wall-clock severity color here would be a lie.
        for idx, title, min_str in tier2_toc["rows"]:
            lines.append(f"{idx}. 🟢 [{title}](#r-{idx}) — {min_str}")
        if tier2_toc.get("overflow"):
            lines += ["", f"*… +{tier2_toc['overflow']} more (not shown; "
                          "kept in the findings JSON)*"]
        lines.append("")
    elif runner_spine_count:
        row_plural = "s" if runner_spine_count != 1 else ""
        lines += [f"**Runner-minute cost spine** — {runner_spine_count} workflow/job "
                  f"row{row_plural} showing where sampled runner minutes go: "
                  "[see below](#runner-minute-reductions).", ""]
    if also_count:
        plural = "s" if also_count != 1 else ""
        if also_on_path:
            # One or more "Also noticed" findings is a credited wall-clock lever that sits ON the
            # critical path, so the pointer must NOT blanket-label the whole section off-path / ~0
            # wall-clock / below the path — that contradicts the inline on-path row (Class A #7).
            lines += [f"**🧹 Also noticed** — {also_count} finding{plural} (mostly off-path "
                      "runner-minute savings; **one or more flagged DO sit on the critical path** "
                      "and cut wall-clock): [see below](#also-noticed).", ""]
        else:
            lines += [f"**🧹 Also noticed** — {also_count} additional hygiene finding{plural} "
                      "kept outside the neutral runner-minute section (modeled/uncertified, "
                      "mostly ~0 wall-clock), below the critical path: "
                      "[see below](#also-noticed).", ""]
    return lines


# The severity keys `collect_runs` stamps on `data_sources.partial_kind`. Kept as
# literals (not imported) so the renderer stays decoupled from the 11k-line collector;
# `test_disclosure_reaches_the_artifact.py` asserts the two definitions agree, so a
# rename can't drift.
#
# `sample_thinned` is the only MINOR kind — the only one that may carry the "a few
# runs/jobs are absent" suffix. `workflow_missing` / `collection_failed` are HOLES in
# the data and render loud. `static_only` / `gh_unavailable` are neither: the gh pass
# never ran, which the report already says at the top, so the note states the reason
# plainly and adds nothing.
_PARTIAL_MINOR_KINDS = frozenset({"sample_thinned"})
_PARTIAL_SEVERE_KINDS = frozenset({"workflow_missing", "collection_failed"})
# The kinds that mean "the gh pass never ran" — the ONLY ones for which an empty
# critical path may be reported as a genuinely quiet repo. Anything else (including a
# kind this renderer has never heard of) is a broken fetch until proven otherwise; see
# `_measurement_is_broken`.
_PARTIAL_NOT_MEASURED_KINDS = frozenset({"static_only", "gh_unavailable"})
# The two lists in `data_sources` naming workflows that left the MEASURED sample: the
# run-list fetch died (no runs), or every per-run job fetch died (runs, no job timing).
# Both are the same news to the reader — the workflow is absent, and the critical path
# was computed from the survivors — so both are rendered, and named, identically.
_SAMPLE_GAP_KEYS = ("run_list_fetch_failures", "job_fetch_failures")


def _sample_gap_workflows(ds: dict[str, Any]) -> list[str]:
    """Every workflow named in either gap list, deduped and sorted."""
    out: set[str] = set()
    for k in _SAMPLE_GAP_KEYS:
        gaps = ds.get(k)
        if not isinstance(gaps, list):
            continue
        for g in gaps:
            if isinstance(g, dict) and g.get("workflow_file"):
                out.add(str(g["workflow_file"]))
    return sorted(out)


def _derived_partial_kind(ds: dict[str, Any]) -> str:
    """The effective severity key for a `data_sources` dict — the stamped
    `partial_kind` when there is one, else derived from the DATA by the same rule the
    collector uses (never guessed from the reason's prose). "" means coverage is clean.

    The derivation exists only for docs that predate the stamp (the committed worked
    examples); `collect()` now stamps the key on every exit."""
    kind = ds.get("partial_kind")
    if kind:
        return str(kind)
    if _sample_gap_workflows(ds):
        return "workflow_missing"
    errs = ds.get("gh_error_count")
    if isinstance(errs, int) and not isinstance(errs, bool) and errs:
        return "sample_thinned"
    return ""


def _measurement_is_broken(ds: dict[str, Any]) -> bool:
    """True when an EMPTY critical path is a hole in the data, not a quiet repo.

    THE cardinal rule of this disclosure, and the one that was violated: a report with
    no measured spine must never tell a reader whose collection just FAILED that they
    have "an archived, brand-new, or low-activity repo". That is a confident WRONG
    diagnosis of a broken fetch, and it invites exactly the wrong conclusion ("my CI is
    quiet") — the honest note lives 50 lines further down in the Data Sources footer,
    which is not where the headline takeaway is formed.

    Reachable without anything exotic: sustained 5xx / timeouts on the run-list
    endpoints wipe every workflow out of the sample and stamp `workflow_missing`, NOT
    `collection_failed`. Branching the banner on `collection_failed` alone (as it once
    did) closed this for exactly ONE of the five kinds.

    So the test is INVERTED — anything that is not explicitly "the gh pass never ran"
    (`static_only` / `gh_unavailable`, which the report already announces up top) counts
    as a broken measurement, INCLUDING a merely thinned sample: with no spine at all,
    even one failed call means we cannot tell a quiet repo from a fetch that lost the
    runs. A new severity key added to the collector therefore defaults to the LOUD
    banner, which is the safe direction to be wrong in."""
    kind = _derived_partial_kind(ds)
    return bool(kind) and kind not in _PARTIAL_NOT_MEASURED_KINDS


def _coverage_note(ds: dict[str, Any]) -> str:
    """The report's partial-coverage sentence, rendered at the severity the DATA says.

    Three things this must not do, each of which it used to do:

    1. Staple a MINIMIZING suffix onto every reason. "…so a few runs/jobs are absent
       from the sample - the P50s are over marginally fewer runs" is true of a handful
       of failed calls and FALSE of a total collection failure, whose own reason says
       "NO workflow could be measured". The report contradicted its own disclosure and
       downgraded a whole-repo gap to a rounding error. Severity now comes from
       `partial_kind` (data), never from string-matching the reason (prose).
    2. Render NOTHING when `gh_error_count` is 0 but a reason is set — reachable
       whenever the gap isn't a failed call (an aborted collection, a malformed body).
       The note is now keyed on the REASON's presence.
    3. Leave the sample-gap lists unrendered. A workflow whose run-list fetch failed —
       or whose per-run JOB fetches ALL failed — is MISSING from the sample, and the
       critical path is then computed from the survivors, so if the vanished one was
       the merge gate the headline is confidently WRONG. Those workflows are NAMED
       here, always. `verify_report`'s `check_run_list_gaps_named` fails any report
       that doesn't."""
    reason = str(ds.get("partial_reason") or "").strip()
    errs = ds.get("gh_error_count")
    errs = errs if isinstance(errs, int) and not isinstance(errs, bool) else 0
    missing = _sample_gap_workflows(ds)
    if not reason and errs:
        # A doc predating `partial_reason` (or a hand-built one): the bare count is
        # all there is, so state it.
        reason = f"{errs} gh API call(s) failed during collection"
    if not reason:
        return ""
    # A doc predating `partial_kind` (the committed worked examples) has its severity
    # DERIVED from the data, by the same rule the collector uses — never guessed from
    # the reason's prose. A pre-stamp artifact with a bare failed-call count therefore
    # keeps reading exactly as it did before the severity key existed.
    kind = _derived_partial_kind(ds)
    # Name every workflow that dropped out of the sample: it is MISSING, not empty, and
    # the critical path above was computed from the survivors. `partial_reason` already
    # names them when collect_runs wrote it — re-derive from the stamped list anyway, so
    # the guarantee holds for any doc however its reason was phrased.
    unnamed = [w for w in missing if w not in reason]
    named_part = ""
    if unnamed:
        named_part = (" Workflows MISSING from the sample (their run-list fetch failed, "
                      "so they are absent, not empty): "
                      + ", ".join(f"`{w}`" for w in unnamed) + ".")
    if kind in _PARTIAL_MINOR_KINDS:
        return (f" **Note:** {reason}, so a few runs/jobs are absent from the sample - "
                "the P50s are over marginally fewer runs than the totals above."
                + named_part)
    if kind in _PARTIAL_SEVERE_KINDS:
        # A HOLE in the data, not a caveat: say so without softening it, and never imply
        # the numbers above are merely a bit thinner than stated.
        return (f" **⚠️ Partial coverage - this audit is INCOMPLETE.** {reason}."
                f"{named_part} Anything the sections above do not mention was not "
                "necessarily absent - it may simply never have been measured.")
    # static_only / gh_unavailable: the gh pass never ran at all, which the report
    # already says up top. State the reason plainly; neither minimize nor alarm.
    return f" **Note:** {reason}.{named_part}"


def _data_sources_footer(doc: dict[str, Any], repo: str,
                         lead: "list[str] | None" = None) -> list[str]:
    """A structured Data Sources table at the foot of the report - which tiers ran,
    their coverage, and what each fed. `lead` (owner UX edit 2026-07-19) is the prose
    provenance block ("Where this data comes from"), now consolidated HERE as the
    section's lead block instead of rendering after the Contents; it explains the
    level-by-level scopes above the at-a-glance audit table. Ported from report.py's
    `_data_sources_block`."""
    ds = doc.get("data_sources") or {}
    sha = doc.get("commit_sha")
    skill_sha = doc.get("skill_commit_sha")
    scanned_at = str(doc.get("scanned_at") or "")
    # `-dirty` when the orchestrator flagged uncommitted skill edits (the HEAD sha
    # alone can lag working-tree changes); verify_report parses this token.
    dirty = "-dirty" if doc.get("skill_tree_dirty") else ""
    # The commit is COLLECT-time provenance (carried in findings.json) and can be
    # squashed out of history; the scripts tree is RENDER-time provenance and survives
    # a squash-merge. verify_report parses both tokens; see _skill_scripts_tree_sha.
    if isinstance(skill_sha, str) and skill_sha.startswith("installed:"):
        # An INSTALLED skill copy (no git repo) stamps a content-hash provenance
        # form (run.py `_skill_lock_provenance`, issue #2). It is NOT a commit —
        # render it as a plain identity string with NO scripts-tree token and (see
        # `_build_catalog_url` / `_metadata_table`) no fabricated commit URL, since
        # there is no resolvable sha to link. verify_report accepts this arm.
        skill_part = f" (skill build `{skill_sha}`)"
    else:
        _tree = _skill_scripts_tree_sha()
        _prov = f"skill commit `{skill_sha[:7]}{dirty}`" if skill_sha else ""
        if skill_sha and _tree:
            _prov += f", scripts tree `{_tree}`"
        skill_part = f" ({_prov})" if _prov else ""
    short_sha = sha[:7] if sha else "no-sha"
    tiers = ds.get("tiers_run") or []
    rows: list[tuple[str, str, str]] = [
        (f"ci-speedup static scan{skill_part}",
         f"All `.github/workflows/*.yml` under the analyzed tree ({short_sha})",
         "Static pattern detection (OPT1–OPT69 catalog)")]
    if "gh-timing" in tiers:
        runs, jobs = ds.get("runs_sampled"), ds.get("jobs_sampled")
        cov = (f"{_count_noun(runs, 'run') if isinstance(runs, int) else '? runs'} / "
               f"{_count_noun(jobs, 'job') if isinstance(jobs, int) else '? jobs'} sampled")
        rows.append(("gh runs/jobs API (timestamps)", cov,
                     "Critical-path + per-step P50"))
    else:
        rows.append(("gh runs/jobs API (timestamps)", "not run",
                     "Pass `--repo owner/repo` (with `gh`) to sample run/job timing"))
    if "job-logs" in tiers or "logs" in tiers:
        # collect_runs names this tier "job-logs"; accept the legacy "logs" too. Count
        # falls back to the persisted bundle manifest when `logs_fetched` is absent, so
        # the cell never reads "not run" while a drill is rendered from those logs.
        logs_n = ds.get("logs_fetched")
        if not isinstance(logs_n, int):
            logs_n = len((doc.get("data_bundle") or {}).get("logs") or [])
        # A genuine ZERO (`logs_fetched == 0`, a no-run-history / nothing-drilled repo) must read
        # "none" - NOT the bare "fetched", which asserts a fetch that never happened. `0` is a valid
        # int so the isinstance fallback above does not fire; the falsy-int branch used to collapse it
        # into "fetched" and overstate the provenance table. Render the count faithfully, zero included.
        rows.append(("job logs",
                     f"{logs_n} job log(s) sampled" if logs_n else "none",
                     "Step internals + cross-run magnitude (deeper levels)"))
    else:
        rows.append(("job logs", "not run",
                     "Sampled only for a slow pole worth log-level inspection"))
    # WHICH workflow YAML fed the detectors. `collect_runs` stamps this, and until now
    # nothing rendered it — so the reader could not tell whether the `on:`/matrix/timeout
    # signals came off the audited checkout or off the default branch's HEAD (the two
    # disagree exactly when the checkout does). A stamped fact nobody can see is not a
    # disclosure.
    _yaml_src = ds.get("workflow_yaml_source")
    if isinstance(_yaml_src, dict) and (_yaml_src.get("checkout") or _yaml_src.get("api")):
        _co = int(_yaml_src.get("checkout") or 0)
        _api = int(_yaml_src.get("api") or 0)
        _parts = []
        if _co:
            _parts.append(f"{_co} from the analyzed checkout")
        if _api:
            _parts.append(f"{_api} from the gh contents API (default branch HEAD)")
        rows.append(("workflow YAML", " / ".join(_parts),
                     "`on:` triggers, matrix/shard axes, job timeouts (detector inputs)"))
    out = ["## 🗄️ Data sources", ""]
    if lead:
        out += lead
    out += ["| Source | Coverage | Used for |", "| --- | --- | --- |"]
    for src, cov, used in rows:
        out.append(f"| {src} | {cov} | {used} |")
    out.append("")
    queries = ds.get("gh_query_count")
    qpart = (f" {queries} gh API queries were made."
             if isinstance(queries, int) and queries else "")
    epart = _coverage_note(ds)
    cutoff = ds.get("sampled_runs_created_before")
    freshness = (f", created ≤ {str(cutoff)[:10]}" if cutoff
                 else " over a rolling 30-day window at scan time")
    out.append(f"**Data freshness.** Analyzer ran at `{scanned_at}`; workflow YAML is "
               f"read from the analyzed tree at commit `{short_sha}`. Timing and "
               f"activity counts reflect the sampled runs{freshness}.{qpart}{epart}")
    out.append("")
    # The config-era bill-scope note (owner UX edit 2026-07-19): moved out of the top-matter
    # era blockquote down to here — it's methodology qualifying these cost-spine figures.
    # Only renders when a workflow's sample straddled a config change.
    out += _bill_scope_era_note(doc)
    return out


def _strip_emdashes(report: str) -> str:
    """Flatten every typographic dash to an ASCII hyphen at the render boundary,
    preserving spacing (' — ' -> ' - ')."""
    for glyph in _DASH_GLYPHS:
        report = report.replace(glyph, "-")
    return report


def _build_catalog_url(skill_sha: str | None) -> str:
    # An `installed:` provenance form (issue #2) is a content hash, not a git ref —
    # linking it as a blob ref would 404, so fall back to the default branch.
    if not skill_sha or skill_sha.startswith("installed:"):
        ref = _DEFAULT_CATALOG_BRANCH
    else:
        ref = skill_sha
    return f"https://github.com/{_CATALOG_REPO}/blob/{ref}/{_CATALOG_PATH}"


def _starsling_footer() -> list[str]:
    """The publisher attribution that closes every report (matches the legacy
    report.py reports)."""
    return ["", "---", "", "Generated by [StarSling](https://starsling.dev) 💫"]


def _run_window(end_iso: str) -> str:
    """The 30-day sampling window as `start → end`, derived from the window's end
    date (the pin, else scan time). Falls back to a plain label if unparseable."""
    end = (end_iso or "")[:10]
    try:
        start = date.fromisoformat(end) - timedelta(days=30)
        return f"{start.isoformat()} → {end} (30-day window)"
    except ValueError:
        return "rolling 30-day window at scan time"


def _metadata_table(doc: dict[str, Any], repo: str) -> list[str]:
    """A compact, scannable header table of what the audit is anchored on: the
    audited commit (so file/line refs have a home), the runs analyzed + their
    window, the PR gate sample, and the audit's own provenance."""
    ds = doc.get("data_sources") or {}
    cp = doc.get("pr_critical_path") or {}
    sha = str(doc.get("commit_sha") or "")
    skill = str(doc.get("skill_commit_sha") or "")
    dirty = "-dirty" if doc.get("skill_tree_dirty") else ""
    scanned = str(doc.get("scanned_at") or "")[:10]
    runs, jobs = ds.get("runs_sampled"), ds.get("jobs_sampled")
    wfs = ds.get("workflows_analyzed")
    cutoff = ds.get("sampled_runs_created_before")
    spr, tgt = cp.get("sampled_pr_count"), cp.get("sample_target")

    # `-dirty` when the checkout's `.github/workflows` had UNCOMMITTED edits at collect
    # time (collect_runs stamps it). It matters to a reader: the detectors parsed the
    # WORKING TREE, while every timing below came from runs of the COMMITTED branch — so
    # a finding already fixed locally can render as clean against timings that still
    # contain it. The marker goes on the DISPLAYED sha only; the permalink keeps the real
    # sha, or it would 404.
    wf_dirty = "-dirty" if doc.get("workflows_tree_dirty") else ""
    commit = (f"[`{sha[:7]}{wf_dirty}`](https://github.com/{repo}/commit/{sha}) — file & "
              "line references are anchored to this tree" if sha else "—")
    if sha and wf_dirty:
        commit += (" — **uncommitted workflow edits were present**: the YAML analyzed is "
                   "the working tree, not this commit, while the timings are this "
                   "branch's runs")
    rows = [("Audited commit", commit)]
    if isinstance(runs, int) and isinstance(jobs, int):
        wf_part = f" across {_count_noun(wfs, 'workflow')}" if isinstance(wfs, int) else ""
        rows.append(("Runs analyzed",
                     f"{_count_noun(runs, 'run')} / {_count_noun(jobs, 'job')}{wf_part}"))
    rows.append(("Runs window", _run_window(str(cutoff) if cutoff else scanned)))
    if isinstance(spr, int) and isinstance(tgt, int):
        rows.append(("PR gate sample", f"{spr} / {tgt} PRs"))
    if skill.startswith("installed:"):
        # Content-hash provenance for an installed skill copy (issue #2): not a
        # commit, so a plain identity string with NO commit permalink to fabricate.
        skill_part = f" · ci-speedup skill build `{skill}`"
    elif skill:
        link = f"[`{skill[:7]}{dirty}`](https://github.com/{_CATALOG_REPO}/commit/{skill})"
        skill_part = f" · ci-speedup skill commit {link}"
    else:
        skill_part = ""
    rows.append(("Audit", f"ran {scanned}{skill_part}" if scanned else "—"))

    # The repository is the table's header row (no empty starting row); the rest are
    # the metadata pairs. `:---` forces left alignment in every renderer.
    out = [f"| Repository | `{repo}` |", "| :--- | :--- |"]
    out += [f"| **{k}** | {v} |" for k, v in rows]
    out.append("")
    return out


def _flatten_cell(text: str) -> str:
    """A markdown table cell can't contain a raw newline or an unescaped pipe — and a
    >=3-backtick run in the repo evidence text that flows through here (structural /
    workflow-YAML `evidence`) would break out of a ```text fence elsewhere in the report
    AND desync `verify_report`'s fence split, so defuse it here too. No-op on clean cells.

    This is the THIRD untrusted-text sink (with `_fence_safe` and `_llm_analysis_block`):
    the appendix `**Evidence:**` lines and the Tier-2 / structural rows quote workflow-YAML
    verbatim through here WITHOUT going through `_fence_safe`, and a hardcoded token in a
    workflow file is the most likely place a credential is quoted from — so mask here too
    (#12). Masking is a no-op on clean cells, so the byte-identity contract holds."""
    return _redact_secrets(_defuse_backtick_runs(
        re.sub(r"\s+", " ", str(text)).replace("|", "\\|").strip()))


def _dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse exact-duplicate occurrences (same source/pattern/file/line/evidence),
    preserving order, so the appendix's occurrence counts aren't inflated by two
    detectors firing on the same line. Ported from report.py."""
    seen: set[tuple[str, str, str, Any, str]] = set()
    out: list[dict[str, Any]] = []
    for f in findings:
        key = (f.get("source", "ci-speedup"), f.get("pattern", ""),
               f.get("workflow_file", ""), f.get("line", 0),
               (f.get("evidence") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _coverage_gap_banner(scan_incomplete: list[dict[str, Any]] | None) -> list[str]:
    """A prominent warning when the static scan couldn't read/parse some workflow
    file - an unscanned file must never read as clean. [] when coverage is complete.
    Ported from report.py."""
    scan_incomplete = scan_incomplete or []
    if not scan_incomplete:
        return []
    lines = ["> [!WARNING]",
             f"> **Incomplete coverage - {len(scan_incomplete)} workflow file(s) could "
             "not be statically scanned.** These files are **not** known to be clean - "
             "fix the cause and re-run before relying on this report.", ">",
             "> _Static scan could not read/parse:_"]
    for entry in scan_incomplete:
        wf = entry.get("path") or entry.get("workflow_file") or "?"
        lines.append(f"> - **{wf}**: {entry.get('reason', 'unknown')}")
    lines.append("")
    return lines


def _pr_floor_fallback_banner(doc: dict[str, Any], cp: dict[str, Any]) -> list[str]:
    """A prominent demotion banner when the spine is the PR-FLOOR fallback, not a
    file-backed required gate. Fires whenever collect_runs set `gate_kind ==
    "pr_floor_fallback"`, which happens two ways: (1) the required suite was readable
    but external/managed and ran on no sampled PR, so the recency-sampled file-backed
    poles are demoted IN PLACE; or (2) no file-backed pole exists at all, so the spine
    is SYNTHESIZED from per-workflow timing. Makes the demotion unmistakable so the
    PR-floor is never misread as the branch-protection gate. [] for a normal
    gate-scoped report."""
    if cp.get("gate_kind") != "pr_floor_fallback":
        return []
    rc = [str(c) for c in (doc.get("required_checks") or [])]
    # Only claim "ran on none of the sampled PRs" when the sampler actually proved it
    # (the external suite was read but absent) — not for the all-fileless-gate case,
    # where the required checks DID run, just without a workflow file to drill.
    unsat = bool(cp.get("required_suite_unsatisfiable"))
    # The "none ran on the sampled PRs" proof is only as strong as the sample. On a
    # SHORT or fetch-degraded sample, "no PR carried the required suite" may be a
    # COVERAGE gap (we sampled too few / failed fetches), not proof the gate is external.
    # Soften the categorical claim to track the sample's quality rather than overstate it.
    degraded = unsat and (not cp.get("sample_complete")
                          or bool(cp.get("sample_fetch_failures")))
    if rc:
        shown = ", ".join(f"`{c}`" for c in rc[:5])
        more = f" (+{len(rc) - 5} more)" if len(rc) > 5 else ""
        if degraded:
            ff = cp.get("sample_fetch_failures") or 0
            why = (f"{ff} fetch failure(s)" if ff else "a short sample")
            gate_clause = (f"This repo's {len(rc)} branch-protection required check(s) — "
                           f"{shown}{more} — did not appear on any sampled PR, but the "
                           f"sample was degraded ({why}), so this may be a coverage gap "
                           "rather than a confirmed external/managed gate — re-run on a "
                           "fuller window before treating the gate as external.")
        else:
            ran_clause = ", and none ran on the sampled PRs" if unsat else ""
            gate_clause = (f"This repo's {len(rc)} branch-protection required check(s) — "
                           f"{shown}{more} — are external/managed: none maps to a workflow "
                           f"file here{ran_clause}.")
    else:
        gate_clause = ("No file-backed required check could be resolved (the required "
                       "suite is external/managed or was unreadable).")
    return ["> [!IMPORTANT]",
            "> **No file-backed required gate — the spine below is the measured "
            f"PR-floor.** {gate_clause} So there is no required-check critical path to "
            "drill. Instead, the long poles below are the **file-backed workflows that "
            "actually run on a normal PR** — the work that sets the developer's merge "
            "wait — ranked by their long-pole job. This is the PR-floor, **not** the "
            "branch-protection gate (which runs outside this repo's workflows); treat "
            "the figures as the PR-floor accordingly.", ""]


def _dropped_unprovable_banner(dropped: list[dict[str, Any]] | None) -> list[str]:
    """A note naming cache findings the `--with-logs` admission gate removed (the
    logs couldn't prove the cacheable work runs). Kept VISIBLE so the drop is
    re-checkable, never silent. [] when none. Ported from report.py."""
    dropped = dropped or []
    if not dropped:
        return []
    lines = ["> [!NOTE]",
             f"> **{len(dropped)} cache finding(s) were dropped after log review.** The "
             "static scan flagged these, but `--with-logs` could not prove the cacheable "
             "work actually runs (no cache line and no install/build activity in the "
             "sampled job logs), so they are **not** counted below - listed here so the "
             "drop is visible and re-checkable, not silent:", ">"]
    for d in dropped:
        jobs = d.get("affected_jobs") or []
        job_str = f" [{', '.join(str(j) for j in jobs)}]" if jobs else ""
        lines.append(f"> - `{d.get('id', '?')}` ({d.get('pattern', '?')}){job_str}: "
                     f"{d.get('reason', 'unprovable from logs')}")
    lines.append("")
    return lines


def _fmt_runner_min(value: float | None) -> str:
    # PR-Z: unified with the Tier-2 label convention (PR-38) — recoverable
    # minutes render as a positive saving, never a signed "−N" reduction. The
    # appendix and the R-rows must not tell the same number with two signs.
    if value is None or value == 0:
        return "—"
    return _fmt_tier2_saved_min(value)


def _fmt_tier2_saved_min(value: float | None) -> str:
    # Twin of tests/verify_report.py:_fmt_tier2_saved_min (identical nonzero
    # output). The zero/None sentinels intentionally differ: this "—" is
    # display-only and _strip_emdashes normalizes it to ASCII "-" at the render
    # boundary, while the verifier's "-" is a skip-sentinel never compared
    # against rendered bytes. Unreachable on visible R-rows (admission requires
    # a positive saving); same convention as the _fmt_runner_min pair above.
    if value is None or value == 0:
        return "—"
    v = abs(value)
    if v < 0.95:
        # Sub-minute savings keep one decimal (PR-Z): a real 0.2 min/mo row
        # must never render "0 min/mo" — display precision, not an admission
        # floor (D3 untouched). Detector savings round to 0.1, so the smallest
        # positive value is 0.1, never "0.0".
        return f"{v:.1f} min/mo"
    return f"{v:,.0f} min/mo"


def _rmin_of(ms: list[dict[str, Any]]) -> float:
    return sum(_num(m.get("runner_min_saving")) or 0.0 for m in ms)


def _sev_of(ms: list[dict[str, Any]]) -> str:
    return min((m.get("severity", "MANUAL") for m in ms),
               key=lambda s: _SEVERITY_ORDER.get(s, 99), default="MANUAL")


def _group_by_pattern_ranked(
        findings: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Collapse findings into (pattern, members) pairs. A group carrying a CREDITED
    wall-clock saving (OPT24 sharding the long pole) sorts FIRST — it is a critical-path
    speed lever, not bill-only hygiene, and such a group has `rm=0`, which would otherwise
    sink it to the bottom of the bill ranking and (on a repo with many hygiene patterns)
    past `_ALSO_NOTICED_CAP` into the hidden "+N more" tail. Sorting it first means it leads
    the section and — as long as wall-clock levers stay far fewer than `_ALSO_NOTICED_CAP`
    (12), which holds today — is never the row suppressed by the cap. The rest are ranked by
    cloud-bill saving desc (then severity, then pattern id). Used by the off-path appendix.

    Grouping is by pattern id EXCEPT for OPT73 (the cross-cluster shared-substep floor
    lever): each OPT73 finding is a DISTINCT lever — its own shared step, its own cluster
    of jobs in its own workflow, its own evidence and magnitude — not a fungible occurrence
    of one fix recipe applied at N spots. Folding them by pattern would render one row whose
    evidence is only the first leg's and whose size is the MAX leg's wall-clock, hiding the
    smaller legs' evidence and over-sizing them. So OPT73 is keyed by its cluster identity
    (workflow + jobs), so distinct levers render as their own rows; identical clusters (same
    workflow + same jobs) still fold. The displayed `pat` stays the bare pattern id."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    display: dict[Any, str] = {}
    order: list[Any] = []
    for f in findings:
        pat = str(f.get("pattern", "") or "?")
        # OPT73 levers are distinct per cluster, not fungible occurrences — see docstring.
        key: Any = pat
        if pat == "OPT73":
            key = (pat, str(f.get("workflow_file", "")),
                   tuple(f.get("affected_jobs") or ()))
        if key not in groups:
            groups[key] = []
            display[key] = pat
            order.append(key)
        groups[key].append(f)
    return sorted(((display[k], groups[k]) for k in order),
                  key=lambda kv: (0 if _group_saves_wall_clock(kv[1]) else 1,
                                  -_rmin_of(kv[1]),
                                  _SEVERITY_ORDER.get(_sev_of(kv[1]), 99), kv[0]))


def _catalog_anchor(catalog_url: str, members: list[dict[str, Any]]) -> str:
    """Deep link into the catalog at the pattern's exact section. `fix_recipe_anchor`
    (e.g. `opt33--no-draft-pr-gating-on-expensive-jobs`) IS the GitHub heading anchor,
    so this lands on the background + fix recipe, not the 1,400-line file's top."""
    anchor = next((m.get("fix_recipe_anchor") for m in members
                   if m.get("fix_recipe_anchor")), "")
    return f"{catalog_url}#{anchor}" if anchor else catalog_url


def _occ_loc(m: dict[str, Any], *, code: bool = True, lead_job: str | None = None) -> str:
    """One occurrence's location: `workflow.yml:line (job)`. `code=False` for inside
    a code fence (the agent prompt), where backticks would render literally.

    `lead_job` overrides the displayed job (default `affected_jobs[0]`). The appendix passes
    the ON-PATH (non-spine-rare) leg for a finding it frames "sits ON the critical path", so a
    multi-leg cluster finding never LEADS its `**Where:**` with a spine-rare-demoted sibling leg
    that the footnote calls opt-in — which the reader would read as that leg being on-path (the
    tauri `test (macos-latest)` double-framing). It must be one of the finding's own jobs."""
    wf = _wf_base(m.get("workflow_file", ""))
    line = m.get("line")
    jobs = m.get("affected_jobs") or []
    lead = lead_job if lead_job else (jobs[0] if jobs else None)
    job = f" ({lead})" if lead else ""
    loc = f"{wf}:{line}" if line else wf
    return f"`{loc}`{job}" if code else f"{loc}{job}"


def _wall_clock_driver(members: list[dict[str, Any]]) -> dict[str, Any]:
    """The member whose `wall_clock_p50_s` IS the group's displayed `~max wall-clock`
    magnitude (argmax). The displayed magnitude and the evidence quoted beside it MUST be
    sourced from this same member — otherwise a group's headline (one on-path job's
    ~3m09s) and its evidence (a different, possibly 0s, off-path job that merely happened
    to be listed first) describe two jobs that don't reconcile, and the row reads as
    self-contradictory. `members` is non-empty at every call site."""
    return max(members, key=lambda m: _num(m.get("wall_clock_p50_s")) or 0.0)


def _group_evidence(members: list[dict[str, Any]], *,
                    prefer: dict[str, Any] | None = None,
                    compose: bool = False) -> str:
    """The evidence string to display for a pattern group. When `prefer` is given (the
    member that drives the displayed magnitude — e.g. the max-wall-clock job for a
    wall-clock lever), quote ITS evidence so the headline magnitude and the evidence
    describe the SAME job. Fall back to the first member carrying an evidence string only
    when the driver itself has none (so a driver without evidence still shows the group's
    available evidence rather than nothing).

    With `compose=True` (and no usable `prefer`), COMPOSE the evidence from the DISTINCT
    per-member evidence of the members shown in "Where" (the first 8, matching the render
    cap), joined in display order. This is for BILL-ONLY groups, whose displayed magnitude
    is an aggregate over ALL members and whose "Where" lists EVERY member's job: the old
    single-member 'first with evidence' fallback named only the FIRST member's jobs, so a
    multi-member bill-only group left the other members' jobs in "Where" unexplained AND
    dropped their evidence (e.g. an OPT12 row aggregating two distinct preamble clusters
    showed only the first cluster's). Composing keeps evidence and the listed locations in
    agreement — the bill-only analogue of the `prefer` guard for wall-clock levers.

    With neither `prefer` nor `compose`, this is the historical 'first member with
    evidence' behaviour (a single-member or fully-fungible group where any member's
    evidence is representative)."""
    # Every arm returns repo-controlled `evidence` text that a caller drops into a ```text
    # prompt fence — fence-safe it (no-op on clean single-line evidence) so a >=3-backtick run
    # can't close the fence / desync verify_report.
    if prefer is not None:
        pe = str(prefer.get("evidence") or "").strip()
        if pe:
            return _fence_safe(pe)
    if compose:
        seen: set[str] = set()
        parts: list[str] = []
        for m in members[:8]:  # mirror the 8-member "Where" cap so evidence ⇔ locations
            e = str(m.get("evidence") or "").strip()
            if e and e not in seen:
                seen.add(e)
                parts.append(e)
        return _fence_safe("; ".join(parts))
    return _fence_safe(
        next((str(m.get("evidence")).strip() for m in members if m.get("evidence")), ""))


def _tier2_ranked(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((f for f in findings if _is_tier2_finding(f)),
                  key=lambda f: (-(_num(f.get("runner_min_saving")) or 0.0),
                                 str(f.get("pattern", "")),
                                 str(f.get("id", ""))))


def _tier2_id(f: dict[str, Any], fallback: int) -> str:
    raw = str(f.get("id") or f"{f.get('pattern', 'tier2')}-{fallback}")
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw)


_TIER2_SHORT_TITLE_MAX = 44


def _tier2_short_title(f: dict[str, Any]) -> str:
    """Compact per-row label for the glanceable Contents rows (OD12-rev1): the
    catalog title minus its trailing parenthetical qualifier, capped at a word
    boundary. Display-only - the FULL catalog title still renders on the row's
    'Bill root-cause' body line, and nothing re-derives from this label."""
    t = _flatten_cell(str(f.get("title") or f.get("pattern") or ""))
    t = re.sub(r"\s*\([^()]*\)\s*$", "", t).strip() or t
    if len(t) <= _TIER2_SHORT_TITLE_MAX:
        return t
    cut = t[:_TIER2_SHORT_TITLE_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or t[:_TIER2_SHORT_TITLE_MAX]) + "…"


def _tier2_cert_summary(f: dict[str, Any]) -> str:
    cert = f.get("tier2_neutrality") if isinstance(f.get("tier2_neutrality"), dict) else {}
    proof = str(cert.get("proof") or "unknown")
    ref = str(cert.get("ref") or "").strip()
    margin = _num(cert.get("margin_s"))
    if proof == "below_cluster_floor" and margin is not None:
        msg = f"`below_cluster_floor` with {_clock(margin)} margin"
    elif proof == "post_completion_waste":
        msg = "`post_completion_waste` - compute burned after the run signal is already decided"
    elif proof == "non_pr_event":
        events = f.get("tier2_run_subset_events")
        ev = ", ".join(str(e) for e in events) if isinstance(events, list) else "non-PR"
        msg = f"`non_pr_event` - `{ev}` runs do not gate a PR merge"
    else:
        msg = f"`{proof}`"
    if ref:
        msg += f" ({_flatten_cell(ref)})"
    return msg


def _tier2_unpromoted_accounting(findings: list[dict[str, Any]],
                                 promoted: list[dict[str, Any]]) -> dict[str, int]:
    """Classify every positive-saving finding the lead's tail must account for.

    Replaces `_tier2_unpromoted_runner_min_count` (PR-P1, closes G1's labeling half).
    That counter called everything "unmeasured" — including OPT47, the demo repo's
    largest MEASURED lever — and counted measured+certified-but-source-unbacked
    findings (the OPT64 class) NOWHERE, because it excluded `_is_tier2_finding`
    candidates wholesale. The lead must account for 100% of positive-saving findings
    by basis and reason (the remediation plan's §5 exit criterion, item 1).

    Buckets, per eligible finding (not advisory / tier2-superseded / wait-pattern /
    per-pole structural annotation, positive `runner_min_saving`), minus the
    promoted set:
      no_source     — measured + certified, but no matching render-ready
                      `runner_minute_spine` rows (kept in the appendix by the
                      PR-34 source-backing gate; D3-rev1's accounting condition)
      cert_deferred — measured, but no neutrality certificate (OPT47 today)
      modeled       — heuristic-sized; never promotes (§5.2)
      structural    — OPT73, the ONE structural pattern that carries a credited
                      runner-minute saving (see `_is_pole_structural`); sized by
                      the shared-step methodology, not the Tier-2 stamp
      other         — anything unclassifiable. Rendered VISIBLY when nonzero, and
                      `verify_report` fails the artifact: an unaccountable finding
                      is the bug class this function exists to end, never a
                      silent drop."""
    promoted_ids = {id(f) for f in promoted}
    out = {"no_source": 0, "cert_deferred": 0, "modeled": 0, "structural": 0, "other": 0}
    for f in findings:
        if (f.get("advisory") or _is_tier2_superseded(f)
                or str(f.get("pattern", "")) in _WAIT_PATTERNS
                # The PATTERN set, not _is_pole_structural: the flag half of that
                # predicate would silently EXCLUDE a flag-carrying finding of any
                # other pattern, where the contract says bucket it — and the
                # verifier's accounting (which keys on the pattern set) would
                # correctly fail the render. Keep the two derivations identical.
                or str(f.get("pattern", "")) in _PER_POLE_STRUCTURAL_PATTERNS
                or (_num(f.get("runner_min_saving")) or 0.0) <= 0
                or id(f) in promoted_ids):
            continue
        basis = f.get("sizing_basis")
        if _is_tier2_finding(f):
            out["no_source"] += 1
        elif basis == "measured":
            out["cert_deferred"] += 1
        elif basis == "modeled":
            out["modeled"] += 1
        elif str(f.get("pattern", "")) == "OPT73":
            out["structural"] += 1
        else:
            out["other"] += 1
    return out


def _tier2_unpromoted_tail(acct: dict[str, int]) -> str:
    """The lead's rendered accounting tail; segments render only when nonzero."""
    measured = acct["no_source"] + acct["cert_deferred"]
    segs = []
    if measured:
        reasons = []
        if acct["no_source"]:
            reasons.append(f"{acct['no_source']} without source rows")
        if acct["cert_deferred"]:
            reasons.append(f"{acct['cert_deferred']} certificate-deferred")
        segs.append(f"{measured} measured item(s) ({', '.join(reasons)})")
    if acct["modeled"]:
        segs.append(f"{acct['modeled']} modeled item(s)")
    if acct["structural"]:
        segs.append(f"{acct['structural']} structural shared-step item(s)")
    if acct["other"]:
        segs.append(f"{acct['other']} other item(s)")
    if not segs:
        return ""
    return "; not promoted: " + " · ".join(segs) + "; see Also noticed"


def _renderable_runner_minute_spine(doc: dict[str, Any]) -> dict[str, Any] | None:
    spine = doc.get("runner_minute_spine")
    if not isinstance(spine, dict) or spine.get("render_ready") is not True:
        return None
    rows = spine.get("rows")
    totals = spine.get("totals")
    if not isinstance(rows, list) or not rows or not isinstance(totals, dict):
        return None
    return spine


def _runner_minute_spine_row_count(doc: dict[str, Any]) -> int:
    spine = _renderable_runner_minute_spine(doc)
    rows = spine.get("rows") if spine else None
    return len(rows) if isinstance(rows, list) else 0


def _spine_identity_cell(value: object) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def _spine_text_cell(value: object) -> str:
    return _flatten_cell(str(value or ""))


def _spine_num_cell(value: object) -> str:
    if isinstance(value, bool):
        return "-"
    val = _num(value)
    return "-" if val is None else f"{val:.3f}"


def _spine_share_cell(value: object) -> str:
    if isinstance(value, bool):
        return "-"
    val = _num(value)
    return "-" if val is None else f"{val * 100.0:.3f}%"


def _runner_minute_spine_sort_key(row: dict[str, Any]) -> tuple:
    row = _as_dict(row)
    billable = _num(row.get("billable_equiv_min_per_month")) or 0.0
    raw = _num(row.get("raw_compute_runner_min_per_month")) or 0.0
    identity = (
        str(row.get("workflow_file") or ""),
        str(row.get("job_name") or ""),
        str(row.get("runner_label") or ""),
        str(row.get("event_scope") or ""),
        str(row.get("status_filter") or ""),
        str(row.get("attempt_filter") or ""),
        str(row.get("volume_filter") or ""),
    )
    return (-billable, -raw, identity)


def _runner_minute_spine_table_row(row: dict[str, Any]) -> str:
    cells = [
        _spine_identity_cell(row.get("workflow_file")),
        _spine_identity_cell(row.get("job_name")),
        _spine_identity_cell(row.get("runner_label")),
        _spine_text_cell(row.get("event_scope")),
        _spine_text_cell(row.get("status_filter")),
        _spine_text_cell(row.get("attempt_filter")),
        _spine_text_cell(row.get("volume_filter")),
        _spine_num_cell(row.get("raw_compute_runner_min_per_month")),
        _spine_num_cell(row.get("billable_equiv_min_per_month")),
        _spine_share_cell(row.get("share_of_all_row_total")),
    ]
    return "| " + " | ".join(cells) + " |"


def _runner_minute_spine_block(doc: dict[str, Any]) -> list[str]:
    spine = _renderable_runner_minute_spine(doc)
    if spine is None:
        return []
    rows = sorted((_as_dict(row) for row in spine.get("rows") or []),
                  key=_runner_minute_spine_sort_key)
    totals = _as_dict(spine.get("totals"))
    shown, hidden = rows[:_RUNNER_MINUTE_SPINE_CAP], rows[_RUNNER_MINUTE_SPINE_CAP:]
    lines = [
        "<!-- ci-speedup:runner-minute-spine -->",
        "### Cost spine: where runner minutes go",
        "",
        ("All figures are runner-minutes; multiply by your runner's per-minute "
         "rate to get dollars."),
        "",
        "| Workflow | Job | Runner | Event | Status | Attempt | Volume | Raw min/mo | Billable min/mo | Share |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    lines += [_runner_minute_spine_table_row(row) for row in shown]
    lines.append(
        "| Total |  |  |  |  |  |  | "
        f"{_spine_num_cell(totals.get('raw_compute_runner_min_per_month'))} | "
        f"{_spine_num_cell(totals.get('billable_equiv_min_per_month'))} | 100.000% |")
    if hidden:
        plural = "s" if len(hidden) != 1 else ""
        lines.append(f"+{len(hidden)} more runner-minute row{plural} hidden")
    lines.append("")
    return lines


def _tier2_source_job_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _tier2_source_rows_matching_jobs(rows: list[dict[str, Any]],
                                     jobs: list[str]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    seen: set[int] = set()
    for job in jobs:
        affected = _tier2_source_job_name(job)
        if not affected:
            continue
        exact = [
            row for row in rows
            if _tier2_source_job_name(row.get("job_name")) == affected
        ]
        candidates = exact or [
            row for row in rows
            if _matrix_base_raw(_tier2_source_job_name(row.get("job_name"))) == affected
        ]
        for row in candidates:
            ident = id(row)
            if ident not in seen:
                seen.add(ident)
                matched.append(row)
    return matched


def _tier2_source_job_candidates(f: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        name = _tier2_source_job_name(value)
        if name and name not in seen:
            seen.add(name)
            out.append(name)

    for job in _as_list(f.get("affected_jobs")):
        add(job)
    add(f.get("rerun_dominant_job"))
    burn = _as_dict(f.get("timeout_default_burn"))
    add(burn.get("job_template"))
    add(burn.get("job_key"))
    for sample in _as_list(burn.get("samples")):
        add(_as_dict(sample).get("job_name"))
    return out


def _tier2_required_source_filters(f: dict[str, Any]) -> dict[str, str] | None:
    filters: dict[str, str] = {
        "status_filter": "success",
        "attempt_filter": "latest",
        "volume_filter": "all-status",
    }
    pat = str(f.get("pattern") or "")
    if pat == "OPT64" or f.get("rerun_dominant_job"):
        filters.update({
            "status_filter": "all-status",
            "attempt_filter": "prior",
            "volume_filter": "all-status",
        })
    explicit = _as_dict(f.get("runner_minute_source_filter"))
    for key in ("event_scope", "status_filter", "attempt_filter", "volume_filter"):
        value = str(explicit.get(key) or f.get(key) or "").strip()
        if value:
            if key in filters and filters[key] != value:
                return None
            filters[key] = value
    return filters


def _tier2_row_matches_required_filters(row: dict[str, Any],
                                        filters: dict[str, str]) -> bool:
    return all(str(row.get(key) or "").strip() == expected
               for key, expected in filters.items())


def _tier2_spine_source_rows(f: dict[str, Any], doc: dict[str, Any]) -> list[dict[str, Any]]:
    spine = _renderable_runner_minute_spine(doc)
    if spine is None:
        return []
    wf = str(f.get("workflow_file") or "").strip()
    if not wf:
        return []
    wide_opt64 = str(f.get("pattern") or "") == "OPT64"
    rows = [
        _as_dict(row)
        for row in _as_list(spine.get("rows"))
        if str(_as_dict(row).get("workflow_file") or "").strip() == wf
    ]
    filters = _tier2_required_source_filters(f)
    if filters is None:
        return []
    if filters:
        rows = [row for row in rows if _tier2_row_matches_required_filters(row, filters)]
    jobs = _tier2_source_job_candidates(f)
    if wide_opt64:
        # R1 (WIDE): an OPT64 finding credits the whole retried run's
        # prior-attempt compute across every runner, so it binds to every
        # prior-attempt row of its workflow — narrowed by neither runner nor
        # job name. The DOMINANT FAILING JOB itself (`rerun_dominant_job`,
        # never a looser candidate like a stale affected_jobs entry) must be
        # visible among those rows — its retries are what caused the
        # credited runs — else no binding at all. Sibling no-double-count:
        # _tier2_opt64_group_cover_ok.
        dominant = _tier2_source_job_name(f.get("rerun_dominant_job"))
        if not dominant or not _tier2_source_rows_matching_jobs(rows, [dominant]):
            return []
        return sorted(rows, key=_runner_minute_spine_sort_key)
    if jobs:
        rows = _tier2_source_rows_matching_jobs(rows, jobs)
    return sorted(rows, key=_runner_minute_spine_sort_key)


def _tier2_opt64_group_cover_ok(f: dict[str, Any], findings: list[dict[str, Any]],
                                doc: dict[str, Any]) -> bool:
    """R1's no-double-count guard: sibling OPT64 findings on one workflow all
    bind to the SAME wide prior-attempt row set, so covering each sibling
    individually is not enough — their combined claim must fit the shared
    cover once. If it does not, NONE of the siblings is source-backed
    (order-independent fail-close: no sibling is cherry-picked)."""
    if str(f.get("pattern") or "") != "OPT64":
        return True
    rows = _tier2_spine_source_rows(f, doc)
    if not rows:
        return False
    wf = str(f.get("workflow_file") or "").strip()
    sibs = [g for g in findings
            if isinstance(g, dict)
            and str(g.get("pattern") or "") == "OPT64"
            and str(g.get("workflow_file") or "").strip() == wf
            and not g.get("advisory")
            and g.get("sizing_basis") == "measured"
            and isinstance(g.get("tier2_neutrality"), dict)
            and bool(g.get("tier2_neutrality"))
            and (_num(g.get("runner_min_saving")) or 0.0) > 0]
    claimed = sum(_num(g.get("runner_min_saving")) or 0.0 for g in sibs)
    # Each sibling's saving is rounded to 0.1 by the detector (≤0.05 error).
    tol = 0.011 + 0.05 * len(sibs)
    raw_total = sum(_num(row.get("raw_compute_runner_min_per_month")) or 0.0
                    for row in rows)
    if claimed > raw_total + tol:
        return False
    return True


def _tier2_source_rows_cover_saving(f: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    saving = _num(f.get("runner_min_saving")) or 0.0
    if saving <= 0 or not rows:
        return False
    if str(f.get("pattern") or "") == "OPT65":
        source_billable = sum(
            _num(row.get("billable_equiv_min_per_month")) or 0.0 for row in rows)
        if saving > source_billable + 0.011:
            return False
    else:
        source_raw = sum(_num(row.get("raw_compute_runner_min_per_month")) or 0.0 for row in rows)
        if saving > source_raw + 0.011:
            return False
    return True


def _tier2_source_backed_ranked(findings: list[dict[str, Any]],
                                doc: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for f in _tier2_ranked(findings):
        rows = _tier2_spine_source_rows(f, doc)
        if (_tier2_source_rows_cover_saving(f, rows)
                and _tier2_opt64_group_cover_ok(f, findings, doc)):
            out.append(f)
    return out


def _is_tier2_source_backed_finding(f: dict[str, Any], doc: dict[str, Any]) -> bool:
    if not _is_tier2_finding(f):
        return False
    # PR-Z (S2-4): the group guard sees the SAME deduped population here as in
    # the section path (_tier2_source_backed_ranked's call sites pass
    # _dedupe_findings output) — two populations could disagree on exact-
    # duplicate OPT64 entries.
    return (_tier2_source_rows_cover_saving(f, _tier2_spine_source_rows(f, doc))
            and _tier2_opt64_group_cover_ok(
                f, _dedupe_findings(list(doc.get("findings") or [])), doc))


def _tier2_source_line(f: dict[str, Any], doc: dict[str, Any]) -> str:
    rows = _tier2_spine_source_rows(f, doc)
    wf = str(f.get("workflow_file") or "").strip() or "unknown workflow"
    if not rows:
        return ("**Source block:** no matching render-ready `runner_minute_spine` row for "
                f"`{_flatten_cell(wf)}`; re-run with complete "
                "cost-spine coverage before treating this savings row as source-backed.")
    raw = sum(_num(row.get("raw_compute_runner_min_per_month")) or 0.0 for row in rows)
    billable = sum(_num(row.get("billable_equiv_min_per_month")) or 0.0 for row in rows)
    plural = "s" if len(rows) != 1 else ""
    if str(f.get("pattern") or "") == "OPT64":
        # R1 (WIDE): the binding spans every prior-attempt row of the workflow
        # across runners, so the line names the attempt population.
        head = (f"matched {len(rows)} prior-attempt row{plural} for "
                f"`{_flatten_cell(wf)}`")
    else:
        head = (f"matched {len(rows)} row{plural} for `{_flatten_cell(wf)}`")
    return (f"**Source block:** `runner_minute_spine` {head}; "
            "current measured cost spine for those rows is "
            f"{raw:.3f} raw min/mo, {billable:.3f} billable min/mo.")


def _has_tier2_stamp_surface(findings: list[dict[str, Any]]) -> bool:
    return any("sizing_basis" in f or "tier2_neutrality" in f for f in findings)


def _tier2_has_modeled_value(findings: list[dict[str, Any]]) -> bool:
    return _has_tier2_stamp_surface(findings) and any(not f.get("advisory")
               and not _is_tier2_superseded(f)
               and not _is_tier2_finding(f)
               and f.get("sizing_basis") != "measured"
               and (_num(f.get("runner_min_saving")) or 0.0) > 0
               for f in findings)


def _tier2_measured_table(f: dict[str, Any]) -> list[str]:
    # Table + provenance note only. The prose summary is NOT emitted here: it renders
    # once, as the row's "What ci-speedup measured" bullet (`_tier2_measured_summary`).
    # The old shape emitted it twice - once as "Waste mechanism", once as "Evidence",
    # word-for-word identical - which is exactly the jumble the format-parity pass
    # (owner directive, 2026-07-09) removed.
    me = f.get("measured_evidence") if isinstance(f.get("measured_evidence"), dict) else {}
    out: list[str] = []
    table = me.get("table") if isinstance(me.get("table"), dict) else {}
    headers = [str(h) for h in (table.get("headers") or [])]
    rows = table.get("rows") if isinstance(table.get("rows"), list) else []
    if headers and rows:
        out += ["| " + " | ".join(_flatten_cell(h) for h in headers) + " |",
                "| " + " | ".join("---" for _ in headers) + " |"]
        for row in rows:
            cells = list(row) if isinstance(row, list) else [row]
            cells = (cells + [""] * len(headers))[:len(headers)]
            out.append("| " + " | ".join(_flatten_cell(str(c)) for c in cells) + " |")
    note = str(me.get("note") or "").strip()
    if note:
        if out:
            out.append("")
        out.append(f"_{_flatten_cell(note)}_")
    return out


def _tier2_measured_summary(f: dict[str, Any]) -> str:
    """The one measured-mechanism sentence for a promoted R-row - the single source
    for what the old card said twice (identical "Waste mechanism" and "Evidence"
    fields)."""
    me = f.get("measured_evidence") if isinstance(f.get("measured_evidence"), dict) else {}
    return _flatten_cell(str(me.get("summary") or f.get("evidence")
                             or f.get("title") or f.get("pattern") or "").strip())


def _tier2_section_lead(
        promoted: list[dict[str, Any]],
        acct: dict[str, int],
        cs: "claims.ClaimSet") -> list[str]:
    total_min = sum(_num(f.get("runner_min_saving")) or 0.0 for f in promoted)
    naive_min = total_min + sum(_num(f.get("runner_min_overlap_s")) or 0.0 for f in promoted)
    count = len(promoted)
    extra = _tier2_unpromoted_tail(acct)
    first = promoted[0]
    sentence = cs.add(claims.Claim(
        kind="tier2_section_lead",
        subject=_tier2_id(first, 1),
        fields={
            "raw_min": round(total_min, 3),
            "naive_min": round(naive_min, 3),
            "count": count,
            "not_promoted_measured_no_source": acct["no_source"],
            "not_promoted_measured_cert_deferred": acct["cert_deferred"],
            "not_promoted_modeled": acct["modeled"],
            "not_promoted_structural": acct["structural"],
            "not_promoted_other": acct["other"],
            "derivation_basis": "jobs_api_timestamps",
        },
        rendered=(
            "These findings cut wall-clock-neutral runner spend without touching your "
            "merge gate; each R-numbered finding carries a machine-derived proof it "
            "cannot slow a PR.")))
    totals = (f"**{_fmt_tier2_saved_min(total_min)} credited after de-overlap** "
              f"(naive sum {_fmt_tier2_saved_min(naive_min)}; {count} neutral "
              f"finding{'s' if count != 1 else ''}{extra}). All figures are "
              "runner-minutes; multiply by your runner's per-minute rate to get dollars.")
    return [f"> {sentence}", f"> {totals}", ""]


def _tier2_block(findings: list[dict[str, Any]], catalog_url: str,
                 cs: "claims.ClaimSet", doc: dict[str, Any] | None = None) -> list[str]:
    spine_lines = _runner_minute_spine_block(doc or {})
    promoted_all = _tier2_source_backed_ranked(findings, doc or {})
    if not promoted_all and not spine_lines:
        return []
    shown, rest = promoted_all[:_TIER2_CAP], promoted_all[_TIER2_CAP:]
    acct = _tier2_unpromoted_accounting(findings, promoted_all)
    heading = (
        "## Runner-minute reductions (wall-clock-neutral)"
        if promoted_all else "## Runner-minute cost spine")
    lines = ['<a id="runner-minute-reductions"></a>', "", heading, ""]
    lines += spine_lines
    if not promoted_all:
        return lines
    lines += _tier2_section_lead(promoted_all, acct, cs)
    # Each promoted R-row mirrors the Long-pole skeleton (owner directive,
    # 2026-07-09: the two sections must not look different aside from the data):
    # same `##` header shape (marker ▸ where - magnitude), a bold role lead, an
    # OPEN body (no <details> wrapper), the pole's labeled-bullet block, and the
    # agent prompt. 🟢 sits in the severity dot's slot (OD12-rev1): every admitted
    # row is certificate-proven merge-safe, so the dot is constant green by design
    # - a wall-clock severity color here would be a lie.
    # The `r-{idx}` anchor stays exactly as before: every TOC link and verifier
    # backreference keys on it, independent of the header text.
    for idx, f in enumerate(shown, 1):
        pat = str(f.get("pattern") or "?")
        title = _flatten_cell(str(f.get("title") or pat))
        rmin = _num(f.get("runner_min_saving")) or 0.0
        rng = f.get("runner_min_range_s")
        range_s = ""
        if isinstance(rng, list) and len(rng) == 2:
            lo, hi = _num(rng[0]), _num(rng[1])
            if lo is not None and hi is not None and hi != lo:
                range_s = (f" (sensitivity range: {_fmt_tier2_saved_min(lo)} to "
                           f"{_fmt_tier2_saved_min(hi)})")
        fid = _tier2_id(f, idx)
        url = _catalog_anchor(catalog_url, [f])
        loc = _occ_loc(f)
        cert_line = cs.add(claims.Claim(
            kind="tier2_neutrality_line",
            subject=fid,
            fields={
                "proof": (f.get("tier2_neutrality") or {}).get("proof"),
                "margin_s": (f.get("tier2_neutrality") or {}).get("margin_s"),
                "derivation_basis": "jobs_api_timestamps",
                "pattern": pat,
            },
            rendered=f"machine-derived proof: {_tier2_cert_summary(f)}."))
        # The role lead, mirroring the pole's ("The slowest check a typical PR
        # waits on."). Rank facts only - the ranking is re-derived by
        # check_tier2_neutrality_derived's id<->rank binding, and "merge-safe" is
        # backed by the minted certificate claim below.
        role = ("**The largest merge-safe runner-minute saving measured on this "
                "repo.**" if idx == 1 else
                f"**The #{idx} merge-safe runner-minute saving measured on this "
                "repo, by size.**")
        lines += [f"<!-- ci-speedup:tier2-finding id={fid} pattern={pat} -->",
                  f'<a id="r-{idx}"></a>',
                  "",
                  f"## 🟢 Runner saving {idx}: {loc} - "
                  f"{_fmt_tier2_saved_min(rmin)}",
                  "",
                  role,
                  ""]
        evidence_table = _tier2_measured_table(f)
        if evidence_table:
            lines += [*evidence_table, ""]
        lines += [f"**💸 Bill root-cause - {pat} · {title}** - risk **{_sev_of([f])}**",
                  "",
                  f"- **What ci-speedup measured:** {_tier2_measured_summary(f)}{range_s}",
                  f"- **Why this can't slow your merge:** {cert_line}",
                  f"- {_tier2_source_line(f, doc or {})}"]
        overlap_s = _num(f.get("runner_min_overlap_s")) or 0.0
        if overlap_s > 0:
            note = str(f.get("tier2_overlap_note") or "duplicate sampled run credit displaced")
            lines.append(f"- **De-overlap:** {_fmt_runner_min(overlap_s)} moved out of this "
                         f"row before totals. {_flatten_cell(note)}")
        for label, key in (("Risk", "risk"), ("Guardrail", "guardrail"),
                           ("Rollout", "rollout")):
            val = str(f.get(key) or "").strip()
            if val:
                lines.append(f"- **{label}:** {_flatten_cell(val)}")
        lines += [f"- **Catalog (background + fix recipe):** {url}", "",
                  *_hygiene_prompt(pat, title, [f], url, tier2=True),
                  ""]
    if rest:
        lines += ["> [!TIP]",
                  f"> **+{len(rest)} more wall-clock-neutral runner-minute finding(s) not "
                  "shown** - lower ranked, kept in the findings JSON.", ""]
    return lines


def _tier2_bottom_line(findings: list[dict[str, Any]],
                       cs: "claims.ClaimSet",
                       doc: dict[str, Any] | None = None) -> list[str]:
    promoted = _tier2_source_backed_ranked(findings, doc or {})
    if promoted:
        total_min = sum(_num(f.get("runner_min_saving")) or 0.0 for f in promoted)
        count = len(promoted)
        top = promoted[0]
        value = f"{_fmt_tier2_saved_min(total_min)} of wall-clock-neutral runner minutes"
        return [cs.add(claims.Claim(
            kind="tier2_headline",
            subject=_tier2_id(top, 1),
            fields={"raw_min": round(total_min, 3),
                    "count": count, "derivation_basis": "jobs_api_timestamps"},
            rendered=_strip_emdashes(
                f"> **After the gate.** {value} is recoverable "
                f"({count} neutral finding{'s' if count != 1 else ''}; none can slow "
                "a merge)."))), ""]
    if _tier2_has_modeled_value(findings):
        return [cs.add(claims.Claim(
            kind="tier2_headline",
            subject="unpromoted-modeled",
            fields={"promoted_count": 0, "derivation_basis": "jobs_api_timestamps"},
            rendered=_strip_emdashes(
                "> **After the gate.** modeled bill opportunities remain in Also "
                "noticed - not promoted: unmeasured."))), ""]
    return []


# The skip-family / trigger-scope patterns whose catalog fixes add
# paths:/branches:/types: filters or skip conditions — exactly the shapes that
# can leave a REQUIRED status check "Pending" forever (§8.1 landmine 1). Their
# agent prompts MUST carry _PENDING_CAVEAT_LINES; verify_report re-checks the
# rendered prompts (check_skip_family_prompts_carry_pending_caveat) via its
# own verbatim marker copy, so this set and that check move together.
_PENDING_CAVEAT_PATTERNS = frozenset(
    {"OPT32", "OPT33", "OPT34", "OPT39", "OPT40", "OPT47"})

# The §8.1 required-check "Pending" landmine, as mandatory prompt text. Every
# sentence is load-bearing: the Pending mechanism, the documented-safe shape
# (job-level `if:` — skipped jobs report Success), the twin-workflow trick
# labeled community-workaround-NOT-docs, and the OPT71 UNKNOWN-is-required
# rule extended to every skip lever.
_PENDING_CAVEAT_LINES = [
    "CAVEAT - the required-status 'Pending' landmine: if ANY check this",
    "workflow produces is a required status check, do NOT skip it via",
    "paths:/branches: filters, [skip ci], or by removing/narrowing a trigger",
    "event - a workflow that no longer fires leaves its",
    "required check 'Pending' and the PR can never merge (official guidance:",
    "do not use path/branch filtering on required workflows). The",
    "documented-safe shape is a job-level `if:` - a skipped job reports",
    "Success and satisfies the gate. The no-op twin-workflow trick (same",
    "workflow AND job name, inverse filter) is a community-known workaround,",
    "NOT in current GitHub docs. Treat required-status UNKNOWN as required:",
    "if branch protection/rulesets are not readable, assume every check this",
    "workflow produces may be required.",
]


def _tier2_guardrail_sentence(f: dict[str, Any]) -> str:
    """The per-finding GUARDRAIL sentence from the detector's measured-evidence
    note (e.g. OPT46's "verify this is NOT a deploy/release/publish workflow"),
    verbatim from "GUARDRAIL" to the note's end — detector-authored fact, never
    renderer-invented prose. Empty when the note carries none."""
    note = str(_as_dict(f.get("measured_evidence")).get("note") or "")
    idx = note.find("GUARDRAIL")
    return note[idx:].strip() if idx >= 0 else ""


def _hygiene_prompt(pat: str, title: str, members: list[dict[str, Any]],
                    url: str, *, wait: bool = False, saves_wc: bool = False,
                    tier2: bool = False,
                    lead_job: "Callable[[dict[str, Any]], str | None] | None" = None
                    ) -> list[str]:
    """A per-pattern, copy-paste agent prompt for an off-path hygiene finding - same
    RCA-hands-off contract as the spine poles (measured cause + locations + the
    catalog recipe link; never a prescribed diff).

    `wait=True` reframes the saving line for the pre-start-wait family (OPT43): the
    cost is developer WALL-CLOCK wait-to-start, NOT a runner-bill cut - so the embedded
    prompt matches its "Pre-start wait" section instead of re-asserting the off-path
    "~0 wall-clock" framing the section exists to correct.

    `saves_wc=True` reframes for a finding that carries a CREDITED wall-clock saving
    (`wall_clock_p50_s > 0` survived the cross-workflow cascade, so the named job IS the
    slowest concurrent check — on the merge-gating critical path; OPT24 'Long Test Job
    Without Sharding' is the canonical case, but OPT28 / a credited OPT73 reach here too).
    Its catalog fix CUTS developer wall-clock — the opposite of the bill-only '~0
    wall-clock' framing — so the prompt says so WITHOUT naming a pattern-specific remedy
    (sharding is OPT24's; naming it would mis-prescribe OPT28/OPT73 and break the
    no-prescription rule). When the saving line quotes ONE member's wall-clock, the "What
    ci-speedup saw" evidence must come from that SAME member, or the prompt's magnitude and
    its evidence describe two different jobs."""
    locs = "; ".join(_occ_loc(m, code=False,
                              lead_job=lead_job(m) if lead_job else None)
                     for m in members[:8])
    if len(members) > 8:
        locs += f"; +{len(members) - 8} more (see findings JSON)"
    prefer: dict[str, Any] | None = None
    if wait:
        saving = ("Cost: developer WALL-CLOCK wait before the job starts (queue / "
                  "wait-to-start) - NOT a runner-bill saving and NOT off the critical "
                  "path. For a `needs:`-gated job this span includes the gating job's "
                  "own run time, so the savable part is bounded by the gating job's own "
                  "fix.")
    elif tier2:
        rmin = _rmin_of(members)
        cert = _tier2_cert_summary(members[0])
        saving = (f"Saving: {_fmt_tier2_saved_min(rmin)} of runner capacity"
                  " - a bill/capacity reduction, not a merge-wait cut. "
                  f"Neutrality certificate: {cert}.")
        # PR-Z (§6 card-shape contract: "certificate + guardrail embedded"):
        # a finding whose measured-evidence note carries a GUARDRAIL sentence
        # ships it inside the prompt too — the agent must not need to leave
        # the prompt to learn the fix's failure mode.
        guardrail = _tier2_guardrail_sentence(members[0])
        if guardrail:
            saving += f"\n{guardrail}"
    elif saves_wc:
        prefer = _wall_clock_driver(members)
        wc = _num(prefer.get("wall_clock_p50_s")) or 0.0
        wc_str = f"~{_clock(wc)}" if wc else "measurable"
        saving = (f"Saving: developer WALL-CLOCK ({wc_str}) - this job is a long pole ON "
                  "the merge-gating critical path, so its catalog fix shortens the merge "
                  "wait. NOT a runner-bill cut, and NOT off the critical path.")
    else:
        rmin = _rmin_of(members)
        bill = (f"~{rmin:,.0f} runner-min/mo" if rmin
                else "no measured runner-min saving")
        wc = max((_num(m.get("wall_clock_p50_s")) or 0.0) for m in members)
        if 0 < wc < _WALL_CLOCK_LONG_POLE_FLOOR_S:
            # A POSITIVE but sub-threshold wall-clock saving (the cascade kept a small
            # value — a near-tie with the next concurrent check, or a rare/opt-in job
            # the spine demotes). `_saves_wall_clock`'s floor correctly declines to call
            # it a long pole, but the blanket "off the critical path, ~0 wall-clock, a
            # cloud-bill cut" framing then CONTRADICTS the measured positive value (and
            # claims a bill cut a rm=0 finding doesn't have). State the measured fact
            # instead: it is below the long-pole threshold, so not a credited merge-wait
            # win, without asserting it is off-path or a bill cut.
            saving = (f"Saving: {bill}; the measured developer wall-clock saving "
                      f"(~{_clock(wc)}) is below the long-pole threshold, so it is NOT "
                      "credited as a merge-gating long pole - treat it as a minor cleanup, "
                      "not a merge-wait win.")
        else:
            saving = (f"Saving: {bill} - off the merge-gating critical path, so ~0 developer "
                      "wall-clock (a cloud-bill cut, not a merge-wait cut).")
    # Bill-only prompts (not wait, not saves_wc/tier2) compose evidence across the listed members
    # so the prompt's "What ci-speedup saw" matches its own multi-job "Where" — the same
    # guard the Evidence line uses. wait/saves_wc keep their single-member `prefer` source.
    ev = _group_evidence(members, prefer=prefer,
                         compose=(not wait and not saves_wc and not tier2))
    # Build the fence CONTENT separately from the ``` delimiters so it can be fence-safed
    # per-line (repo text — `title`, `locs` — assembled here must not close the fence), while the
    # delimiters are emitted structurally, never through the sanitizer.
    body = ["ci-speedup measured the pattern below but does NOT prescribe the fix -",
            "investigate it in the repo and apply a safe change.", "",
            f"Pattern: {pat} - {title}.",
            f"Where: {locs}."]
    if ev:
        body.append(f"What ci-speedup saw: {ev}")
    body += [saving, "",
             "Read the catalog entry (background, fix recipe, and guardrail):",
             f"  {url}", ""]
    if pat in _PENDING_CAVEAT_PATTERNS:
        # §8.1 landmine 1 — mandatory on every skip-family / trigger-scope
        # prompt, whichever framing branch built the saving line above.
        body += _PENDING_CAVEAT_LINES + [""]
    body += [
        "Do: confirm the pattern at each location above, recover the intent from git",
        "history, and apply the catalog's fix recipe where it is safe. State the",
        "failure mode and how you have guarded it before shipping.",
    ]
    return ["#### 🤖 Prompt for your coding agent", "", "```text",
            *[_fence_safe(l) for l in body], "```"]


def _queue_wait_block(findings: list[dict[str, Any]], catalog_url: str,
                      shallow_note: str = "") -> list[str]:
    """Pre-start WALL-CLOCK wait (queue time). A PR waits in queue before its jobs
    start running — developer wall-clock the critical-path spine (job start → finish)
    doesn't capture, and NOT a runner-minute saving — so it gets its own section rather
    than the bill-savings appendix. Grouped by pattern; each a `<details>` with the
    worst-queued jobs ranked by P90 wait, evidence, the catalog fix recipe, and an agent
    prompt. [] when there's no queue-wait finding."""
    wait = [f for f in findings if _is_wait_finding(f)]
    if not wait:
        return []
    # Disclosure parity with the hygiene appendix's "+N more … kept in the findings JSON":
    # `_is_wait_finding` drops queue findings whose floor-capped savable wait is 0 (queue
    # overlaps the gate / a job no one merge-waits on). That's the right call, but say so —
    # a silent drop here would read as "no queue issue" if the floor cascade ever wrongly
    # zeroed a real wait. The findings survive in findings.json (the filter is render-only).
    dropped = sum(1 for f in findings
                  if str(f.get("pattern", "")) in _WAIT_PATTERNS and not f.get("advisory")
                  and not ((_num(f.get("wall_clock_p50_s")) or 0) > 0))
    # NB: the wait family stuffs its queue value into the generic `wall_clock_p50_s` field
    # (see collect_runs `_detect_opt43_queue_time`). After the floor cascade this value is the
    # FLOOR-CAPPED SAVABLE wait, NOT the raw P90 — when something capped it the raw P90 lives
    # in `wall_clock_uncapped_p50_s` (set only when `derivation` is non-empty; absent when
    # nothing capped, where savable == raw P90), and the per-finding `evidence` line quotes
    # the raw P90 ("P90 queue 295s"). So rank/display by the savable value but LABEL it
    # "savable wait", not "P90"; the evidence line carries the true P90. (Earlier this field
    # was the raw P90 and the labels said so — the cascade changed that, labels corrected.)
    by_pat: dict[str, list[dict[str, Any]]] = {}
    for f in wait:
        by_pat.setdefault(str(f.get("pattern", "")), []).append(f)
    groups = sorted(by_pat.items(),
                    key=lambda kv: -max((_num(m.get("wall_clock_p50_s")) or 0)
                                        for m in kv[1]))
    lines = ['<a id="pre-start-wait"></a>', "",
             "## ⏳ Pre-start wait (queue time)", "",
             "> Time a PR waits in queue **before its jobs start running** — developer "
             "wall-clock the critical-path spine above does **not** capture (the spine "
             "measures each job from start to finish). Usually runner-pool saturation or "
             "a restrictive concurrency group. Ranked by savable wait (the floor-capped "
             "addressable part; each finding's evidence quotes the raw P90 queue). **Note:** "
             "for a `needs:`-gated job this is *wait-to-start* — it includes the gating job's "
             "run time, so the savable part is bounded by the gating job's own fix.", ""]
    if shallow_note:
        lines += [f"> ⚠️ _{shallow_note}_", ""]
    if dropped:
        plural = "s" if dropped != 1 else ""
        lines += [f"> _{dropped} more queue finding{plural} ha{'ve' if dropped != 1 else 's'} "
                  "no addressable wait (queue overlaps the gate, or a job no one merge-waits "
                  "on) — kept in the findings JSON, omitted here._", ""]
    for pat, ms in groups:
        ms = sorted(ms, key=lambda m: -(_num(m.get("wall_clock_p50_s")) or 0))
        title = _flatten_cell(ms[0].get("title") or pat)
        worst = _num(ms[0].get("wall_clock_p50_s")) or 0
        n = len(ms)
        wfs = len({m.get("workflow_file", "") for m in ms if m.get("workflow_file")})
        occ = f"{n} across {wfs} wf" if wfs else f"{n} occurrence"
        url = _catalog_anchor(catalog_url, ms)
        ev = next((str(m.get("evidence")).strip() for m in ms if m.get("evidence")), "")
        locs = ", ".join(_occ_loc(m) for m in ms[:8])
        if len(ms) > 8:
            locs += f", +{len(ms) - 8} more"
        lines += ["<details>",
                  f"<summary><strong>{pat} - {title}</strong> · worst savable wait "
                  f"{_clock(worst)} · {_sev_of(ms)} · {occ}</summary>", "",
                  f"**Where:** {locs}"]
        if ev:
            lines.append(f"**Evidence:** {_flatten_cell(ev)}")
        lines += [f"**Catalog (background + fix recipe):** {url}", "",
                  *_hygiene_prompt(pat, title, ms, url, wait=True), "", "</details>", ""]
    return lines


def _also_noticed_block(findings: list[dict[str, Any]],
                        catalog_url: str,
                        shallow_note: str = "",
                        pole_jobs: set[tuple[str, str]] | None = None,
                        doc: dict[str, Any] | None = None,
                        spine_rare_names: set[str] | None = None) -> tuple[list[str], int]:
    """The residual hygiene appendix: modeled/uncertified catalog hygiene + bill-only
    cluster-floor findings that are not promoted to the measured spine or Tier 2,
    grouped by pattern and ranked by cloud-bill saving. Each is a COLLAPSED `<details>` block carrying its locations,
    evidence, a deep link to the catalog fix recipe, and its own copy-paste agent
    prompt - actionable like the spine poles, but folded so the section stays
    scannable. Advisory findings, pre-start-wait findings (their own §), and the
    structural PER-POLE levers (OPT70/71/72/74/75 — rendered AS the poles above) are
    excluded; the cross-cluster floor lever (OPT73) is NOT a per-pole annotation and,
    when bill-only, carries the runner-minute axis the methodology shows here, so it
    is kept (see `_is_pole_structural`).
    A VALUELESS finding (no bill AND no wall-clock saving — the OPT24 "~0 everything" profile) whose
    jobs are ALL drilled long poles is also excluded (Class A #5): the pole headlines that job as the
    single biggest lever, so re-listing it as an "Also noticed · minor cleanup, ~0 wall-clock" row
    directly contradicts the headline. The exclusion is NARROW on purpose — a finding carrying a real
    saving on EITHER axis (a credited OPT73 cross-cluster floor, an OPT33/OPT45 bill lever) is a
    legitimate appendix entry on a different axis from the pole's wall-clock and is KEPT, and a finding
    that also touches a NON-pole job is kept so its other jobs stay disclosed.
    Source-backed Tier-2 findings are excluded because their own section owns
    them; source-unbacked measured+certified candidates fall through here with a
    note instead of silently disappearing.
    Returns (lines, n_groups, any_wc_lever) — the 3rd flag is True when a shown finding is a
    credited wall-clock lever that sits ON the critical path (so the TOC pointer must not blanket-
    label the section off-path, Class A #7); ([], 0, False) when there's nothing off-path."""
    pole_jobs = pole_jobs or set()
    tier2_doc = doc
    doc = doc or {}
    rare_names = spine_rare_names or set()

    def _job_is_spine_rare(job: str) -> bool:
        # A job NAME the footnote demotes as opt-in / rare — exact, or unexpanded matrix
        # base<->rendered leg (mirrors `_stamp_spine_rare`'s NAME-level join, no sibling fold).
        return any(job == n or _matrix_base(n) == job or _matrix_base(job) == n
                   for n in rare_names)

    def _lead_job(m: dict[str, Any], on_path: bool) -> str | None:
        # The job to DISPLAY in `**Where:**` for one member. For an ON-PATH finding, lead with
        # the first affected job that is NOT spine-rare-demoted (the leg the on-path claim rests
        # on) so the line never frames a demoted name "sits ON the critical path" — the tauri
        # OPT73 `test (macos-latest)`-vs-`test (windows-latest)` double-framing. Off-path (or
        # when every leg is demoted, which for on-path can't happen — the finding would be
        # stamped `spine_rare`), keep the default first affected job.
        jobs = [str(j) for j in (m.get("affected_jobs") or []) if str(j)]
        if not jobs:
            return None
        if on_path and rare_names:
            for j in jobs:
                if not _job_is_spine_rare(j):
                    return j
        return jobs[0]

    def _tier2_owned_here(f: dict[str, Any]) -> bool:
        if tier2_doc is None:
            return _is_tier2_finding(f)
        return _is_tier2_source_backed_finding(f, doc)

    def _source_unbacked_tier2_here(f: dict[str, Any]) -> bool:
        return (tier2_doc is not None
                and _is_tier2_finding(f)
                and not _is_tier2_source_backed_finding(f, doc))

    def _on_pole_job(f: dict[str, Any]) -> bool:
        # Only the VALUELESS profile contradicts the headline: no bill saving AND not a CREDITED
        # wall-clock lever (`_saves_wall_clock` — `wall_clock_p50_s` at/above the long-pole floor;
        # OPT24's sub-second 0.5s is NOT credited). A finding with a real saving on either axis (a
        # credited OPT73 bill lever, a credited wall-clock lever flagged inline) is a legitimate
        # appendix entry and stays. A valueless finding whose every job is a drilled pole would
        # otherwise render an "Also noticed · ~0 wall-clock minor cleanup" row that directly
        # contradicts the headline crowning that same job the biggest lever; falling through to
        # the valueless + all-pole test below excludes exactly those, mirroring
        # `check_pole_not_reframed_as_hygiene`. A finding that also touches a NON-pole job still
        # renders (the all-pole guard keeps it), so its signal is not lost off-path.
        if (_num(f.get("runner_min_saving")) or 0.0) > 0 or _saves_wall_clock(f):
            return False
        jobs = f.get("affected_jobs") or ([f.get("job")] if f.get("job") else [])
        if not jobs:
            return False
        wfb = _wf_base(str(f.get("workflow_file") or ""))
        # ALL jobs must be drilled poles — else a co-affected non-pole job would lose its disclosure.
        return all((wfb, _job_base(str(j))) in pole_jobs for j in jobs)

    elig = [f for f in findings
            if not f.get("advisory") and not _is_pole_structural(f)
            and not _is_tier2_superseded(f)
            and not _tier2_owned_here(f)                          # Tier-2-owned → own section
            and str(f.get("pattern", "")) not in _WAIT_PATTERNS  # → its own §
            and not _on_pole_job(f)]                              # valueless + all-pole-job → already AS a pole (#5)
    if not elig:
        return [], 0, False
    ranked = _group_by_pattern_ranked(elig)
    # Hard cut at the cap. Wall-clock levers are sorted to the front by
    # `_group_by_pattern_ranked`, so they survive this slice (they'd only be cut if there
    # were >_ALSO_NOTICED_CAP of them — not a case that arises today; see that docstring).
    shown, rest = ranked[:_ALSO_NOTICED_CAP], ranked[_ALSO_NOTICED_CAP:]
    # A credited wall-clock lever (OPT24) can land in this appendix; it sorts first and
    # carries a per-row correction below. Qualify the blanket "off-path / ~0 wall-clock"
    # blurb when one is present, so the section header doesn't contradict that row.
    # `_group_frames_on_path` (NOT `_group_saves_wall_clock`): a `spine_rare` lever keeps its
    # wall-clock magnitude for display but is NOT framed on-path, so the "one or more DO sit on
    # the critical path" blurb variant fires only for a genuinely on-path (non-rare) lever.
    any_wc_lever = any(_group_frames_on_path(ms) for _pat, ms in shown)
    promoted_patterns = {
        str(f.get("pattern", ""))
        for f in findings
        if _tier2_owned_here(f)
    }
    blurb = ("> These findings stay outside the wall-clock-neutral runner-minute section "
             "because they are modeled, uncertified, advisory-by-shape, missing source-spine "
             "backing, or below that section's measured admission gate. Most do **not** sit on the merge-gating "
             "critical path above, so fixing them removes little or no developer wall-clock "
             "- but they can still cut runner-minutes. **Expand any finding** for its "
             "locations, evidence, the catalog fix recipe, and a copy-paste agent prompt; "
             "exact per-occurrence lines + evidence also live in the findings JSON.")
    if any_wc_lever:
        blurb = ("> Most of these stay outside the wall-clock-neutral runner-minute section "
                 "and do **not** sit on the merge-gating critical path above, so fixing them "
                 "removes little or no developer wall-clock. **One or more exceptions are "
                 "flagged inline** with a **Wall-clock** note: those DO sit on the critical "
                 "path and their fix cuts developer wall-clock (shown first). **Expand any "
                 "finding** for its locations, evidence, the catalog fix recipe, and a "
                 "copy-paste agent prompt; exact per-occurrence lines + evidence also live "
                 "in the findings JSON.")
    lines = ['<a id="also-noticed"></a>', "",
             "## 🧹 Also noticed - residual hygiene", "",
             blurb, ""]
    if shallow_note:
        # Adaptive sampling deepened the critical-path workflows to full depth, but
        # these off-path findings aggregate across ALL workflows — so for workflows
        # outside the deepened spine their runner-minute and queue-time figures rest on
        # the shallow sample and are APPROXIMATE (queue-time p90 especially can swing).
        # Disclosed, not silent; the exact figures need a full-depth re-run.
        lines += [f"> ⚠️ _{shallow_note}_", ""]
    for pat, ms in shown:
        title = _flatten_cell(ms[0].get("title") or pat)
        rmin = _rmin_of(ms)
        bill = _fmt_runner_min(rmin) if rmin else "no bill saving"
        n = len(ms)
        wfs = len({m.get("workflow_file", "") for m in ms if m.get("workflow_file")})
        occ = f"{n} across {wfs} wf" if wfs else f"{n} occurrence"
        url = _catalog_anchor(catalog_url, ms)
        # A finding with a CREDITED wall-clock saving (OPT24 sharding the long pole) is a
        # wall-clock lever, not bill-only hygiene — the appendix's blanket "off-path / ~0
        # wall-clock" framing is false for it, so flag the summary by its wall-clock saving
        # instead of its (zero) bill, and reframe its prompt. `saves_wc` (magnitude) drives the
        # DISPLAY; `on_path` (magnitude AND not `spine_rare`) drives the "sits ON the critical
        # path" CLAIM. They diverge only for a presence-demoted (opt-in/rare) job: it still shows
        # its real wall-clock magnitude (never a valueless "~0" row that would contradict its
        # pole headline), but its note says it only cuts wall-clock on the minority of PRs that
        # run it — a typical PR doesn't wait on it — never "sits ON the merge-gating critical path".
        saves_wc = _group_saves_wall_clock(ms)
        on_path = _group_frames_on_path(ms)
        # `on_path` known → a member's `**Where:**` leads with its ON-PATH (non-spine-rare) leg,
        # so a multi-leg cluster finding never double-frames a demoted sibling leg on-path.
        locs = ", ".join(_occ_loc(m, lead_job=_lead_job(m, on_path)) for m in ms[:8])
        if len(ms) > 8:
            locs += f", +{len(ms) - 8} more"
        if saves_wc:
            # The displayed magnitude is ONE member's wall-clock (the max). Source the
            # evidence from that SAME member so the headline (~max wall-clock) and the
            # evidence describe one job — not a 3m09s on-path job headline beside a 0s
            # off-path member's evidence that merely happened to be listed first.
            driver = _wall_clock_driver(ms)
            wc = _num(driver.get("wall_clock_p50_s")) or 0.0
            summary_metric = f"~{_clock(wc)} wall-clock"
            ev = _group_evidence(ms, prefer=driver)
        else:
            summary_metric = bill
            # The displayed magnitude is an aggregate over ALL members and "Where" lists
            # every member's job, so COMPOSE the evidence across the listed members — the
            # bill-only analogue of the saves_wc `prefer` guard. Sourcing only the first
            # member (the old behaviour) left other members' jobs in "Where" unexplained
            # and dropped their evidence, so evidence and locations disagreed.
            ev = _group_evidence(ms, compose=True)
        lines += ["<details>",
                  f"<summary><strong>{pat} - {title}</strong> · {summary_metric} · "
                  f"{_sev_of(ms)} · {occ}</summary>", "",
                  f"**Where:** {locs}"]
        has_source_unbacked_tier2 = any(_source_unbacked_tier2_here(m) for m in ms)
        # PR-P1 (D3-rev1's accounting condition): a demoted MEASURED finding names its
        # reason class here, and certificate-carrying ones render their proof class —
        # the lead's "not promoted:" tail points at these notes.
        cert_classes = sorted({str((m.get("tier2_neutrality") or {}).get("proof") or "")
                               for m in ms if _source_unbacked_tier2_here(m)} - {""})
        cert_line = (f" Their computed neutrality certificate(s) "
                     f"(`{'`, `'.join(cert_classes)}`) are stamped in findings.json "
                     "and re-derived by verify_report." if cert_classes else "")
        has_cert_deferred_tier2 = any(
            tier2_doc is not None
            and m.get("sizing_basis") == "measured"
            and not isinstance(m.get("tier2_neutrality"), dict)
            and not m.get("advisory")
            and (_num(m.get("runner_min_saving")) or 0.0) > 0 for m in ms)
        if pat in promoted_patterns:
            residual = ("modeled, uncertified, or source-unbacked"
                        if has_source_unbacked_tier2 else "modeled or uncertified")
            lines.append("**Tier-2 note:** measured wall-clock-neutral instances of this "
                         "same pattern are promoted above; this appendix row shows only "
                         f"the remaining {residual} instance(s).")
        elif has_source_unbacked_tier2:
            lines.append("**Tier-2 note:** measured wall-clock-neutral instance(s) of "
                         "this pattern did not have matching render-ready "
                         "`runner_minute_spine` source rows, so they are kept here "
                         "instead of rendered as source-backed savings cards."
                         + cert_line)
        elif has_cert_deferred_tier2:
            lines.append("**Tier-2 note:** measured, but its wall-clock-neutrality "
                         "certificate is deferred (no proof class covers it yet), so "
                         "it is not promotion-eligible; the sizing itself comes from "
                         "sampled run history, not a heuristic.")
        if saves_wc and on_path:
            # Correct the section header for this row: unlike the bill-only findings here,
            # it IS on the merge-gating critical path and its catalog fix cuts wall-clock.
            # Stay remedy-agnostic — sharding is OPT24's fix, but OPT28 (fetch-depth) and a
            # credited OPT73 (extract/cache the shared step) reach here too; naming sharding
            # would mis-prescribe them and break the "does NOT prescribe the fix" invariant.
            lines.append("**Wall-clock:** unlike the other findings in this section, this "
                         "one **sits ON the merge-gating critical path** (a long pole) — "
                         "its catalog fix **cuts developer wall-clock**, it is not a "
                         "bill-only cleanup. See the spine above and this pattern's "
                         "catalog recipe below for the remedy.")
        elif saves_wc:
            # `spine_rare`: the job carries a real multi-minute wall-clock saving, but the spine
            # DEMOTES it as opt-in/rare (present on only a minority of PRs). So its fix cuts
            # wall-clock ONLY on the PRs that run it — a typical PR doesn't wait on it. This note
            # is the exact complement of the level-1 footnote's demotion; it must NEVER claim the
            # job "sits ON the merge-gating critical path" (the paradedb double-framing).
            lines.append("**Wall-clock:** this job carries a real wall-clock saving, but the "
                         "spine demotes it as **opt-in / rare** — it runs on only a minority "
                         "of sampled PRs, so a typical PR doesn't wait on it (see the spine's "
                         "opt-in footnote above). Its fix cuts wall-clock on the PRs that DO "
                         "run it, not on the typical merge path.")
        # Disclose wall-clock-negative hygiene: it saves runner-minutes but its usual
        # fix (build-once then fan-out) adds a serial gate, so it makes the merge
        # SLOWER. Surfacing it keeps the bill saving honest about its wall-clock cost.
        # This is an `elif`: a finding cannot be BOTH a credited wall-clock saver and
        # wall-clock-negative — `saves_wc` requires post-cascade `wall_clock_p50_s > 0`, but a
        # wall-clock-negative finding's sizing model sets `wall_clock_p50_s = 0.0` by design (its
        # fix ADDS wall-clock), unconditionally and independent of critical-path position. So the
        # two are disjoint and the saving note never masks this honesty disclosure.
        elif _increases_wall_clock(ms[0]):
            lines.append("**Wall-clock:** this saves runner-minutes but its fix is "
                         "**wall-clock-negative** (build-once-then-fan-out adds a serial "
                         "gate), so it lengthens the merge wait. Treat it as a bill "
                         "saving, not a speed win.")
        if ev:
            lines.append(f"**Evidence:** {_flatten_cell(ev)}")
        lines += [f"**Catalog (background + fix recipe):** {url}", "",
                  *_hygiene_prompt(pat, title, ms, url, saves_wc=saves_wc,
                                   lead_job=lambda m: _lead_job(m, on_path)),
                  "", "</details>", ""]
    if rest:
        n_rest = sum(len(ms) for _p, ms in rest)
        lines += ["> [!TIP]",
                  f"> **+{len(rest)} more hygiene pattern(s) ({n_rest} occurrence(s)) "
                  "not shown** - lower bill saving, kept in the findings JSON so nothing "
                  "is dropped.", ""]
    return lines, len(ranked), any_wc_lever


# A credited wall-clock saving below this many seconds is NOT a "long pole". This floor
# rejects sub-floor ROUNDING ARTIFACTS only (a 0.3s OPT24 that renders "~0s wall-clock", or a
# 2.2s OPT28) — it does NOT, and never did, guard against the spine's typical/rare (presence /
# pole-frequency) demotion. The concurrency cascade floors an OFF-path workflow's saving to 0,
# but it bounds against CONCURRENT checks only, so a rare / opt-in / path-conditional job that
# is the slowest check on the MINORITY of PRs it runs keeps its FULL positive `wall_clock_p50_s`
# — multi-minute, far above this floor (paradedb `Test pg_search …` ~22m31s on 8/20 PRs). Such a
# rare job IS still catalog-covered (a real OPT24 fired on it), so the presence demotion is NOT
# expressed here (nor in `_saves_wall_clock`, which this floor gates): the "opt-in / conditional
# ... a typical PR doesn't wait on it" footnote is a RENDER-only framing judgment enforced by
# `_frames_on_path` (the appendix on-path gate) via the `spine_rare` stamp — kept decoupled from
# this coverage gate so demoting a rare job can never manufacture a false coverage gap. A real
# sharding/structural long-pole saving runs to minutes and is untouched by this floor.
_WALL_CLOCK_LONG_POLE_FLOOR_S = 30.0


def _saves_wall_clock(f: dict[str, Any]) -> bool:
    """True when the finding carries a CREDITED wall-clock saving large enough to be a
    long pole — `wall_clock_p50_s` survived the cross-workflow cascade as a value at or
    above `_WALL_CLOCK_LONG_POLE_FLOOR_S`, which only happens when the named job is the
    slowest concurrent check (i.e. it sits ON the merge-gating critical path; the cascade
    floors an off-path workflow's saving to 0). OPT24 ('Long Test Job Without Sharding')
    is the canonical case: sharding the long pole cuts wall-clock with no runner-minute
    saving, so it lands in the bill-ranked appendix with rm=0 — but it is emphatically NOT
    off-path / ~0 wall-clock, and the appendix must not claim so. The post-cascade value
    is the gate (NOT `wall_clock_uncapped_p50_s`, which is also set on a finding the
    cascade floored to 0 precisely BECAUSE it is off the critical path).

    The magnitude floor matters because the cascade bounds against concurrent checks, not
    against the spine's rare/conditional-presence demotion: a rare or path-conditional job
    can keep a small positive saving while the spine demotes it as opt-in. A sub-floor
    saving (a sub-second rounding artifact, or a few seconds on such a demoted job) is NOT
    a long pole and must not be labeled as sitting ON the critical path — that would
    contradict the report's own spine footnote.

    `off_spine` (stamped in `collect_runs` from the spine's dropped-check sets, by
    `(workflow_file, job)` identity — encord §6 Cause 2) is the harder gate the magnitude
    floor can't enforce: a non-required job that is its OWN workflow's long pole survives the
    concurrency cascade with a large `wall_clock_p50_s` while being excluded from a
    required-scoped spine (512s ≫ 30s). Such a finding is never a long pole on the
    merge-gating path, so decline it regardless of magnitude.

    This is the CREDITED-MAGNITUDE / catalog-COVERAGE predicate: `_data_driven_for_pole`
    and `_catalog_covers` gate on it to decide whether a catalog detector already covers a
    pole (so it's NOT a coverage gap and must not trigger the phase-4a gap-fill / gap-capture /
    detector-draft loop). It therefore does NOT — and must not — consult `spine_rare`: a
    presence-demoted (opt-in/rare) job that ran on only a MINORITY of sampled PRs keeps its full
    multi-minute `wall_clock_p50_s` (paradedb `Test pg_search …` ~22m31s on 8/20 PRs) and IS
    genuinely catalog-covered — declining it here would manufacture a false coverage gap. The
    separate `spine_rare` demotion — the spine footnote's "a typical PR doesn't wait on it" — is
    a RENDER-only FRAMING judgment; it's enforced by `_frames_on_path`, the appendix's on-path
    gate, so the appendix never claims a rare job "sits ON the merge-gating critical path" while
    the footnote demotes it, WITHOUT touching this coverage gate."""
    if f.get("off_spine"):
        return False
    return (_num(f.get("wall_clock_p50_s")) or 0.0) >= _WALL_CLOCK_LONG_POLE_FLOOR_S


def _group_saves_wall_clock(ms: list[dict[str, Any]]) -> bool:
    """True when ANY member of a pattern group carries a credited wall-clock saving.
    Group members can arrive in any order, and the summary metric is `max(... for m in ms)`,
    so the wall-clock framing (ranking, blurb, per-row note) must inspect every member —
    not just `ms[0]` — to stay consistent with that max."""
    return any(_saves_wall_clock(m) for m in ms)


def _frames_on_path(f: dict[str, Any]) -> bool:
    """Whether the 'Also noticed' appendix may frame this finding as sitting ON the
    merge-gating critical path: it carries a credited wall-clock magnitude (`_saves_wall_clock`)
    AND is NOT `spine_rare`. This is the ONLY place the `spine_rare` demotion is enforced —
    deliberately DECOUPLED from `_saves_wall_clock` (the credited-magnitude / catalog-COVERAGE
    gate). A presence-demoted (opt-in/rare) job keeps its full multi-minute `wall_clock_p50_s`
    and stays catalog-covered (no false coverage gap, no wasted gap-fill / gap-capture /
    detector-draft), but the spine footnote demotes it as "a typical PR doesn't wait on it", so
    the appendix must not ALSO claim it "sits ON the merge-gating critical path" (the paradedb
    `Test pg_search` double-framing). For a NON-rare finding `_frames_on_path == _saves_wall_clock`,
    so existing appendix behavior is unchanged."""
    return _saves_wall_clock(f) and not f.get("spine_rare")


def _group_frames_on_path(ms: list[dict[str, Any]]) -> bool:
    """`_frames_on_path` over a pattern group — see `_group_saves_wall_clock` for why every
    member is inspected, not just `ms[0]`."""
    return any(_frames_on_path(m) for m in ms)


def _stamp_spine_rare(findings: list[dict[str, Any]],
                      checks: list[dict[str, Any]],
                      is_opt_in_rare: Callable[[str], bool]) -> None:
    """Stamp `spine_rare=True` on each finding whose job maps ONLY to spine checks the spine
    demotes as OPT-IN / RARE — the class whose level-1 footnote reads "a typical PR doesn't wait
    on it". The appendix on-path gate `_frames_on_path` then declines them, so the "Also noticed"
    appendix never frames such a job "sits ON the merge-gating critical path" while the spine
    demotes the SAME job as opt-in (the paradedb `Test pg_search` double-framing). The stamp is
    read ONLY by `_frames_on_path` (render-time framing), never by `_saves_wall_clock` — so a
    rare job with a real OPT24 stays catalog-covered and never manufactures a false coverage gap.

    `is_opt_in_rare(check_name)` is the renderer's OWN opt-in test — `not _typical_check(name)`
    AND present on a MINORITY of sampled PRs — so the stamp is the exact complement of the
    demotion the spine renders and can never disagree. The PRESENCE-minority clause is what
    keeps a FREQUENCY-demoted matrix leg that is present on EVERY PR (requests `build (…)`,
    `pole_n` 0 but on 11/11 PRs) OFF this stamp: its matrix IS on the critical path every PR, so
    an OPT73/OPT24 across it legitimately cuts wall-clock (a TRUE on-path claim). Opt-in demotion
    is a RENDER-time judgment over `pr_critical_path`, which is why this is stamped here
    (mirroring how `collect_runs._stamp_off_spine_findings` stamps `off_spine` where the
    dropped/kept sets are known) rather than in the data layer.

    KEPT-GUARD (mirrors `_stamp_off_spine_findings`): a finding is stamped only when its job
    matches an OPT-IN/RARE check AND matches NO non-opt-in check — a job that also maps to a
    typical/present check is genuinely on the typical path, so demoting it would wrongly strip
    the legitimate "sits ON the critical path" framing from a finding whose matrix DOES gate a
    typical PR.

    Matching is by NAME — exact, or unexpanded matrix base<->rendered leg — NOT by
    (workflow, job) identity, because the spine footnote demotes a check NAME ("`X` … a typical
    PR doesn't wait on it") and the reader can't distinguish which workflow's `X` a later
    "sits ON the critical path" note is about. So it must NOT use `_same_matrix` (which folds
    two DISTINCT sibling legs — `test (macos-latest)` vs `test (windows-latest)` — under one
    matrix base: a finding on the exact RARE leg would then wrongly match its TYPICAL sibling
    and the KEPT-GUARD would decline the stamp, leaving the rare leg framed on-path — the
    tauri `test (macos-latest)` contradiction). And it must NOT skip a `_wf_conflict`: a finding
    on `test (macos-latest)` in `test-cli-js.yml` collides by NAME with the rare
    `test (macos-latest)` the footnote demotes (recorded against `test-android.yml`), so it too
    must be demoted or the appendix double-frames the same NAME the footnote calls opt-in.
    Base<->leg matching still lets an UNEXPANDED-base finding (`test`) match BOTH legs, so the
    KEPT-GUARD correctly keeps it on-path when a sibling leg is typical. Over-stamping under this
    NAME rule can only make the appendix MORE conservative (drop an on-path claim for a name the
    footnote already demotes) — it can never manufacture the opposite contradiction — which is
    exactly the reader-facing consistency the `check_dropped_check_not_framed_on_path` invariant
    enforces."""
    named = [(str(c.get("name", "")), str(c.get("workflow_file") or ""))
             for c in checks if str(c.get("name", ""))]
    if not named:
        return

    def _hits(f: dict[str, Any]) -> list[str]:
        jobs = [str(j) for j in (f.get("affected_jobs") or []) if str(j)]
        if f.get("job"):
            jobs.append(str(f.get("job")))
        if not jobs:
            return []
        out: list[str] = []
        for cname, _cwf in named:
            # NAME-level match, NOT identity: exact, OR base<->leg (the finding names the
            # unexpanded matrix base of which cname is a rendered leg, or vice-versa).
            # Deliberately NO `_same_matrix` (which folds DISTINCT sibling legs) and NO
            # `_wf_conflict` skip — both are wrong for a demotion whose scope is the NAME the
            # footnote demotes, not a (workflow, job) identity. See the KEPT-GUARD note below.
            if any(j == cname or _matrix_base(cname) == j or _matrix_base(j) == cname
                   for j in jobs):
                out.append(cname)
        return out

    for f in findings:
        hits = _hits(f)
        if hits and all(is_opt_in_rare(n) for n in hits):
            f["spine_rare"] = True


def _increases_wall_clock(f: dict[str, Any]) -> bool:
    """True when the fix ADDS developer wait (serial-gate / build-once-then-fan-out
    rewrites) - it saves runner-minutes but is wall-clock-NEGATIVE. The 'Also noticed'
    appendix flags this so a bill saving isn't mistaken for a speed win. Ported from
    report.py."""
    if f.get("wall_clock_negative"):
        return True
    return "wall-clock-negative" in (f.get("size_note") or "").lower()


def _wf_conflict(wf_a: str, wf_b: str) -> bool:
    """True when both workflow files are KNOWN and resolve to DIFFERENT files.

    A finding from one workflow must not anchor a pole in another even when their
    check names collide — GitHub gives same-named jobs in different workflows
    IDENTICAL check-run names (a `name: Python ${{ matrix.python }}` job in both
    datasets-test.yml and framework-test.yml both surface as `Python 3.13`), so a
    per-pole join keyed on the name alone (`j == t`, `_same_matrix`, base↔leg) would
    attach datasets-test's finding to framework-test's pole. The poles no longer
    fold (`_same_matrix` takes the workflow file), so without this guard BOTH poles
    render and the finding mis-attributes to the wrong one. An unknown side
    (`""`/None) returns False, so the join falls back to name-only matching —
    preserving reusable-workflow behaviour where a finding's file may be the caller,
    not the producer."""
    return bool(wf_a and wf_b and _wf_base(wf_a) != _wf_base(wf_b))


def _structural_for_pole(pole: dict[str, Any],
                         findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The structural-track findings (OPT70–75, `structural: true`) routed to THIS pole.
    The structural track is rendered AS the pole (ARCHITECTURE §11) and is excluded from
    the off-path 'Also noticed' appendix precisely because the pole represents it — so
    the renderer MUST join it back here, or the finding (its risk + guardrail + rollout)
    renders nowhere AND the pole falsely reads as a coverage gap. The join is on the
    finding's `affected_jobs` against the pole's check/job — exact, OR base↔leg expansion
    (the finding names the unexpanded matrix base of which this pole is a rendered leg, or
    vice-versa). It deliberately does NOT fold a DISTINCT sibling matrix leg (a different
    rendered check with its own bar/dominant step): that produced a false "it IS this pole"
    render (lancedb/lancedb), so it mirrors how collect_runs routes a structural finding to
    its critical-path check rather than name-folding across the whole matrix base. A finding
    from a DIFFERENT workflow file never joins (`_wf_conflict`), so a cross-workflow
    check-name collision (`Python 3.13` in two workflows) can't mis-attribute either."""
    targets = [t for t in (str(pole.get("check", "")), str(pole.get("job", ""))) if t]
    if not targets:
        return []
    pole_wf = str(pole.get("workflow_file") or "")
    out: list[dict[str, Any]] = []
    for f in findings:
        if not (f.get("structural")
                or str(f.get("pattern", "")) in _STRUCTURAL_PATTERNS):
            continue
        if _wf_conflict(pole_wf, str(f.get("workflow_file") or "")):
            continue  # a different workflow's finding can't anchor this pole (name collision)
        # Mirror the appendix predicate so the two never both claim the same finding:
        # a bill-only cross-cluster lever (OPT73) is OWNED by "Also noticed" (it spans the
        # cluster, no single pole represents it), so it must NOT also render at a pole its
        # `affected_jobs` happen to name-match — otherwise the bill saving renders twice.
        if not _is_pole_structural(f):
            continue
        jobs = [str(j) for j in (f.get("affected_jobs") or []) if str(j)]
        if not jobs:
            continue  # an unrouted structural finding can't anchor to a pole
        # FAITHFUL per-pole routing: join only when affected_jobs names THIS pole's exact
        # check/job, OR the unexpanded matrix BASE of which this pole is a rendered leg (or
        # vice-versa). A DISTINCT sibling leg (`windows (x86_64-...)` vs the pole's
        # `windows (aarch64-...)`) is its OWN check with its own bar and dominant step — a
        # shared-matrix-base fold (`_same_matrix`) would render it under this pole with a
        # false "it IS this pole" claim (lancedb/lancedb). Mirrors `_data_driven_for_pole`'s
        # `_job_targets_pole` minus that `_same_matrix` sibling fold.
        if any(j == t or _matrix_base(t) == j or _matrix_base(j) == t
               for j in jobs for t in targets):
            out.append(f)
    return out


def _collapsed_sibling_structural(
        rep: dict[str, Any],
        rendered_poles: list[dict[str, Any]],
        file_poles: list[dict[str, Any]],
        findings: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Structural levers (OPT70/71/72/74/75) carried by a matrix SIBLING leg that
    `by_matrix` COLLAPSED into `rep`. One representative pole is rendered per matrix
    (the slowest leg); the faster legs are dropped from `rendered_poles`. Because
    `_structural_for_pole` deliberately does NOT fold a distinct sibling leg (it would
    render a false "it IS this pole") AND `_also_noticed_block` excludes every per-pole
    structural lever (`_is_pole_structural`), a finding that names ONLY a collapsed leg
    routes to NEITHER the rendered pole NOR the appendix — it survives only in
    findings.json. That is a silent drop. This returns `(leg_name, finding)` pairs so the
    representative pole can ANNOTATE them (carrying the leg's OWN already-computed
    numbers), never inventing a saving. Faithful guards: the leg must genuinely share
    `rep`'s matrix base + workflow (`_same_matrix`), must have been collapsed out (not
    rendered as its own pole), and the finding must not already render at any pole."""
    rep_check = str(rep.get("check", ""))
    rep_wf = str(rep.get("workflow_file", ""))
    rep_targets = {t for t in (rep_check, str(rep.get("job", ""))) if t}
    # Structural findings already rendered AT some pole — never re-annotate those (a
    # finding whose `affected_jobs` names the matrix BASE joins `rep` via base↔leg
    # expansion and is shown there already).
    already = {id(f) for q in rendered_poles for f in _structural_for_pole(q, findings)}
    rendered_checks = {str(q.get("check", "")) for q in rendered_poles}
    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[int] = set()
    for leg in file_poles:
        leg_check = str(leg.get("check", ""))
        if leg_check in rep_targets or leg_check in rendered_checks:
            continue  # the rep itself, or a leg that survived as its own rendered pole
        if not _same_matrix(leg_check, rep_check,
                            str(leg.get("workflow_file", "")), rep_wf):
            continue  # not a sibling of this representative's matrix
        for f in _structural_for_pole(leg, findings):
            if id(f) in already or id(f) in seen:
                continue
            seen.add(id(f))
            out.append((_clean_label(leg_check), f))
    return out


# The per-pole structural patterns (OPT70/71/72/74/75) — each a lever on ONE pole, rendered
# AS that pole. OPT73 is excluded: it is the cross-cluster floor lever (no single pole
# represents it) with its own appendix/known-gap story, so it is not part of this
# per-pole-drop accounting.
_PER_POLE_STRUCTURAL_PATTERNS = _STRUCTURAL_PATTERNS - {"OPT73"}


def _undisclosed_pole_structural(
        findings: list[dict[str, Any]],
        rendered_poles: list[dict[str, Any]],
        file_poles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-pole structural levers (OPT70/71/72/74/75) that render NOWHERE in the markdown.

    A per-pole structural finding is rendered AS its home pole (`_structural_for_pole`) or
    annotated at a representative when a sibling leg collapsed into it
    (`_collapsed_sibling_structural`); it is deliberately excluded from the off-path appendix
    (`_is_pole_structural`). But collect_runs routes the top `_STRUCTURAL_TOP_N` (5)
    critical-path checks into structural candidates while the renderer shows only the top
    `_TOP_WORKFLOWS` (2) poles — so a finding on a check ranked 3rd–5th, in a workflow with NO
    rendered pole, joins neither path and survives only in findings.json. That is a silent
    drop, which this skill forbids. Returning those findings lets the renderer DISCLOSE a
    count (mirroring the appendix's "+N more … kept in the findings JSON"), converting a
    silent drop into a visible one. This also covers the cross-workflow-collision case
    `_wf_conflict` now (correctly) excludes from a foreign pole — previously that finding
    rendered under the WRONG pole; now it is disclosed honestly instead.

    The renderer PARTITIONS the returned levers for disclosure: a FILE-BACKED one (real
    `.yml` workflow_file) is disclosed as "below the rendered poles"; one with a name-stub
    workflow_file (a managed/fileless check like Greptile/Socket, or an unresolved file —
    collect_runs' `check_name.split(' ')[0]` fallback) is disclosed separately as
    "managed/unresolved" so it is NEVER silently dropped from the count. Rendered poles are
    excluded by the workflow-aware finding-IDENTITY set (`_structural_for_pole` /
    `_collapsed_sibling_structural`) — NOT by a name match, which could drop a real
    file-backed lever whose check name collides with a rendered pole in a DIFFERENT workflow."""
    rendered: set[int] = set()
    for p in rendered_poles:
        for f in _structural_for_pole(p, findings):
            rendered.add(id(f))
        for _leg, f in _collapsed_sibling_structural(
                p, rendered_poles, file_poles, findings):
            rendered.add(id(f))
    out: list[dict[str, Any]] = []
    for f in findings:
        if id(f) in rendered:
            continue
        if str(f.get("pattern", "")) not in _PER_POLE_STRUCTURAL_PATTERNS:
            continue  # not a per-pole lever (appendix-owned, OPT73, or non-structural)
        if not [j for j in (f.get("affected_jobs") or []) if str(j)]:
            continue  # unrouted — can't anchor a pole; not this disclosure's concern
        out.append(f)
    return out


def _is_real_workflow_path(wf: str) -> bool:
    """True for a genuine repo-rooted workflow path (`.github/workflows/x.yml`), False for
    the name-stub a structural finding gets when its file couldn't be resolved
    (`check_name.split(' ')[0]` — a managed/fileless or cross-file-ambiguous check)."""
    wf = (wf or "").strip()
    return ("/" in wf) and wf.endswith((".yml", ".yaml"))


def _data_driven_for_pole(pole: dict[str, Any],
                          findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The DATA-DRIVEN catalog finding(s) (e.g. OPT24 'Long Test Job Without Sharding')
    routed to THIS pole that carry a CREDITED wall-clock saving — i.e. the cross-workflow
    cascade kept `wall_clock_p50_s > 0`, which only happens when the named job is the
    slowest concurrent check, so the finding sits ON the merge-gating critical path. Such a
    finding renders in 'Also noticed' with a 'this one sits ON the critical path … See the
    spine above' note (`_saves_wall_clock`), so the SPINE must acknowledge it back — without
    this join the pole falsely reads as a coverage gap ('no catalog pattern matched') and
    gets routed into the phase-4a LLM gap-fill + the gap-capture loop, even though a
    deterministic catalog detector fired squarely on it. Unlike the structural track these
    findings are NOT re-rendered AS the pole (they live in the appendix), so this join only
    suppresses the false gap; it never duplicates their body. A bill-only / off-path
    data-driven finding (wall-clock floored to 0) makes no spine claim and is NOT joined.
    Also excluded: `advisory` findings and pre-start-wait (`_WAIT_PATTERNS`, own section)
    findings, neither of which makes a spine claim. Structural findings have their own join
    (`_structural_for_pole`) and are excluded here.
    KNOWN GAP (follow-up): a *credited on-spine* OPT73 (`wall_clock > 0`) is appendix-owned
    by `_is_pole_structural` (returns False) yet is still excluded here by the blanket
    `_STRUCTURAL_PATTERNS` skip, so it has neither per-pole join. If such a finding routes to
    a single no-`leaf` pole it can still read as a coverage gap — narrow (OPT73 conceptually
    spans a cluster, and the typical bill-only OPT73 floors wall-clock to 0 and is correctly
    not joined), tracked separately rather than widened here to avoid mislabeling OPT73 as
    'data-driven' in the prompt.
    The join is on `affected_jobs` vs the pole's check/job — exact, same-matrix leg, OR the
    unexpanded matrix base (a finding routed to job id `pytest-torch` covers the rendered
    leg `pytest-torch (ubuntu-latest, 3.10)`). A finding from a DIFFERENT workflow file
    never joins (`_wf_conflict`), so a cross-workflow check-name collision (`Python 3.13` in
    two workflows) can't mis-attribute a credited finding to the wrong pole."""
    targets = [t for t in (str(pole.get("check", "")), str(pole.get("job", ""))) if t]
    if not targets:
        return []
    pole_wf = str(pole.get("workflow_file") or "")

    def _job_targets_pole(j: str, t: str) -> bool:
        return (j == t or _same_matrix(j, t)
                or _matrix_base(t) == j or _matrix_base(j) == t)

    out: list[dict[str, Any]] = []
    for f in findings:
        if f.get("advisory"):
            continue
        if _wf_conflict(pole_wf, str(f.get("workflow_file") or "")):
            continue  # a different workflow's finding can't anchor this pole (name collision)
        if (f.get("structural")
                or str(f.get("pattern", "")) in _STRUCTURAL_PATTERNS):
            continue  # the structural track has its own join (_structural_for_pole)
        if str(f.get("pattern", "")) in _WAIT_PATTERNS:
            continue  # pre-start wait has its own section
        if not _saves_wall_clock(f):
            continue  # only a credited-wall-clock finding claims the spine
        jobs = [str(j) for j in (f.get("affected_jobs") or []) if str(j)]
        if not jobs:
            continue  # an unrouted finding can't anchor to a pole
        if any(_job_targets_pole(j, t) for j in jobs for t in targets):
            out.append(f)
    return out


def _structural_block(findings: list[dict[str, Any]],
                      catalog_url: str) -> list[str]:
    """Render the structural-track finding(s) (OPT70–75) routed to this pole - LOUD, as
    SKILL.md / the OPT75 catalog TL;DR require. Because the structural track IS the pole,
    these findings are excluded from the hygiene appendix; if they didn't render here
    they'd render nowhere. Each carries the non-negotiable risk axis (risk + guardrail +
    rollout + failure mode) so the lever is never handed over as a safe quick win."""
    out: list[str] = []
    for f in findings:
        pat = str(f.get("pattern", ""))
        title = _flatten_cell(f.get("title") or pat)
        risk = str(f.get("risk", "")).strip().upper()
        risk_s = f" - risk **{risk}**" if risk else ""
        out += [f"**📐 Structural root-cause — {pat} · {title}**{risk_s}", "",
                "A measured **structural** lever on the critical path (it IS this pole, so "
                "it's not repeated in the off-path appendix). It carries a risk profile - "
                "review the guardrail and rollout before shipping:", ""]
        ev = str(f.get("evidence") or "").strip()
        if ev:
            out.append(f"- **What ci-speedup measured:** {_flatten_cell(ev)}")
        if f.get("guardrail"):
            out.append(f"- **Guardrail:** {_flatten_cell(str(f['guardrail']))}")
        if f.get("rollout"):
            out.append(f"- **Rollout:** {_flatten_cell(str(f['rollout']))}")
        if f.get("failure_mode"):
            out.append(f"- **Failure mode:** {_flatten_cell(str(f['failure_mode']))}")
        anchor = f.get("fix_recipe_anchor")
        url = f"{catalog_url}#{anchor}" if anchor else catalog_url
        out += [f"- **Catalog (background + fix recipe):** {url}", ""]
    return out


def _sibling_structural_annotation(
        pairs: list[tuple[str, dict[str, Any]]],
        catalog_url: str) -> list[str]:
    """Annotate this representative pole with structural levers carried by matrix sibling
    legs that COLLAPSED into it (`_collapsed_sibling_structural`). The pole renders only
    the slowest leg of its matrix; a faster sibling's lever — excluded from both the
    per-pole render and the off-path appendix — would otherwise vanish from the markdown
    (it survives only in findings.json). The lever is NOT re-derived: it carries the
    sibling finding's OWN measured numbers/labels, clearly attributed to that leg so the
    reader never mistakes it for this pole's own cause."""
    out: list[str] = []
    for leg, f in pairs:
        pat = str(f.get("pattern", ""))
        title = _flatten_cell(f.get("title") or pat)
        risk = str(f.get("risk", "")).strip().upper()
        risk_s = f" - risk **{risk}**" if risk else ""
        out += [f"**📐 Sibling matrix leg `{leg}` also carries a structural lever — "
                f"{pat} · {title}**{risk_s}", "",
                f"This pole renders the slowest leg of its matrix; the faster sibling leg "
                f"`{leg}` carries its OWN structural lever, surfaced here so it isn't "
                f"dropped (the measured numbers below are that leg's, not this pole's). "
                f"It carries a risk profile - review the guardrail and rollout before "
                f"shipping:", ""]
        ev = str(f.get("evidence") or "").strip()
        if ev:
            out.append(f"- **What ci-speedup measured (sibling leg):** {_flatten_cell(ev)}")
        if f.get("guardrail"):
            out.append(f"- **Guardrail:** {_flatten_cell(str(f['guardrail']))}")
        if f.get("rollout"):
            out.append(f"- **Rollout:** {_flatten_cell(str(f['rollout']))}")
        if f.get("failure_mode"):
            out.append(f"- **Failure mode:** {_flatten_cell(str(f['failure_mode']))}")
        anchor = f.get("fix_recipe_anchor")
        url = f"{catalog_url}#{anchor}" if anchor else catalog_url
        out += [f"- **Catalog (background + fix recipe):** {url}", ""]
    return out


# The category-aggregation suffix `_decompose_job_steps` appends when the dominant step
# is really a group of same-category steps (e.g. "Verify … (mutation registry) + 1 more
# other step", "… + 2 more install steps"). Two matrix legs of the same job routinely
# differ ONLY by this suffix (leg 1/4's decomposition names one step, 3/4's names two), so
# collapsing must compare the BASE step name with the suffix stripped — otherwise identical
# levers would fail the identity test on a cosmetic count difference (issue #53). Anchored to
# the EXACT generated shape ` + N more <category> step(s)` at end-of-string (category is one
# token, always followed by `step`/`steps`) so a real step name that merely contains
# `+ N more <noun>` — e.g. `Deploy + 2 more regions` — is NOT clipped to its head.
_MORE_STEPS_SUFFIX_RE = re.compile(r"\s+\+\s+\d+\s+more\s+\S+\s+steps?\s*$")
# The displayed check duration `(158s)` and job share `71% of job` inside a structural
# finding's `evidence` string (built once in collect_runs `_detect_structural_candidates`:
# `critical-path check \`<check>\` (<N>s): dominant step \`<step>\` (<cat>, <M>% of job …)`).
# The collapsed sibling line reuses these EXACT displayed numbers so no per-leg measurement
# the full block carried is lost — the check p50 the evidence shows is not a separate field
# on the finding, so this is the faithful source (fallback to `decomposition` below).
# Anchored to the `s):` that ALWAYS closes the check duration in the evidence grammar
# (`… (<N>s): dominant step …`), so a check NAME that itself carries a parenthesized
# `(Ns)` token — a timeout-matrix leg like `test (3s)` — can't shadow the real duration.
_CRIT_DUR_RE = re.compile(r"\((\d+)s\):")
_CRIT_SHARE_RE = re.compile(r"(\d+)%\s+of job `")


def _dominant_step_base(step: str) -> str:
    """The dominant-step name with the `+ N more <category> step(s)` category-aggregation
    suffix stripped, so two legs whose decompositions aggregate a different NUMBER of
    same-category steps still compare equal on the underlying step (issue #53)."""
    return _MORE_STEPS_SUFFIX_RE.sub("", str(step)).strip()


def _struct_identity(f: dict[str, Any]) -> "tuple[str, str, str] | None":
    """The collapse identity of a structural finding: (routed pattern id, dominant-step BASE
    name, dominant category). Two OPT75 siblings that carry the SAME lever on the SAME
    dominant step of the SAME category are duplicate boilerplate — the reader learns nothing
    new from a second full block (issue #53). Dominant CATEGORY is part of the identity by
    design: the base step name almost always fixes the category, so including it can never
    create a FALSE collapse — it only refuses to merge the pathological case where the same
    step name routed to a different cost category (genuinely different remedy framing), which
    should keep its own block. Returns None when the finding lacks a `decomposition` (no
    measured step to key on) so it can never collapse — the anti-drop full block is kept."""
    decomp = f.get("decomposition")
    if not isinstance(decomp, dict):
        return None
    step = decomp.get("dominant_step")
    if not step:
        return None
    return (str(f.get("pattern", "")),
            _dominant_step_base(str(step)),
            str(decomp.get("dominant_category") or ""))


def _leg_measure(f: dict[str, Any]) -> "tuple[str, str]":
    """The `(duration, share)` a collapsed sibling leg contributes to the compact line, as
    the SAME strings its full block's evidence displayed (`158s`, `71%`). Parses the finding's
    `evidence` first (the check p50 shown there is not a standalone field); falls back to the
    structured `decomposition` if the evidence text ever changes shape."""
    ev = str(f.get("evidence") or "")
    dm = _CRIT_DUR_RE.search(ev)
    # The share is anchored to the grammar's closing ``of job `<leg>` `` (the backtick
    # is what a step NAME like "Run 90% of job tests" can't produce) and scoped to the
    # text AFTER the duration match — either alone defeats most shadowing, together they
    # defeat the adversarial cases (same class as the duration anchoring above; #90 bot
    # review, strengthened: the review's scope-only suggestion still matched its own
    # example, since the step name precedes the real share in the post-duration text).
    sm = _CRIT_SHARE_RE.search(ev[dm.end():] if dm else ev)
    dur = f"{dm.group(1)}s" if dm else ""
    share = f"{sm.group(1)}%" if sm else ""
    decomp = f.get("decomposition")
    if isinstance(decomp, dict):
        jp = _num(decomp.get("job_p50_s"))
        sh = _num(decomp.get("dominant_share"))
        if not dur and jp:
            dur = f"{jp:.0f}s"
        if not share and sh is not None:
            share = f"{sh * 100:.0f}%"
    return dur, share


def _collapsed_sibling_line(pairs: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """ONE compact line for the sibling legs whose lever is IDENTICAL to the pole's own
    (same routed pattern + dominant-step base + category). The pole's own structural block
    carries the guardrail/rollout/failure-mode/catalog boilerplate exactly once; repeating it
    per leg said nothing new (issue #53). Each leg still appears by name with its OWN measured
    p50 + share, so no per-leg evidence is lost — only the duplicated boilerplate is dropped."""
    parts: list[str] = []
    for leg, f in pairs:
        dur, share = _leg_measure(f)
        seg = f"`{leg}`"
        if dur and share:
            seg += f" {dur} · {share}"
        elif dur:
            seg += f" {dur}"
        elif share:
            seg += f" {share}"
        parts.append(seg)
    return ["**Sibling legs carry the same lever on the same step** — "
            + ", ".join(parts)
            + "; one fix reshapes all legs (each leg's own p50 · share shown; the "
              "guardrail, rollout, and failure mode above apply unchanged).", ""]


# Invisible (HTML-comment) marker the static-only renderer stamps on its report so
# `verify_report._is_static_only` can recognize it WITHOUT substring-matching human/LLM
# prose. Keep this literal in sync with the copy in `tests/verify_report.py`.
_STATIC_ONLY_MARKER = "<!-- ci-speedup:static-only -->"

# PR-developer trigger events — a workflow gates a PR only if its static `on:` parse
# fires on one of these. Mirrors `collect_runs._DEVELOPER_EVENTS` /
# `verify_report._VR_PR_VOLUME_EVENTS` verbatim (no cross-module import; keep in sync).
_PR_GATING_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})


# ── Config-era disclosure + recoverable-within-wait reconciliation (issue #66) ──
# The disclosure marker the disclosed-pre blockquote carries — `verify_report`'s
# check_config_era_boundary binds the stamped `kept_era == "pre"` fact to this literal.
_CONFIG_ERA_DISCLOSED_MARKER = "measures the previous configuration"
# The marker the post_only (narrowed-to-current) note carries — `verify_report` binds a stamped
# `post_only` era to this literal so a dropped narrowed note FAILs too (symmetric with the pre marker).
_CONFIG_ERA_NARROWED_MARKER = "narrowed to the current configuration"
# The marker the post_only_thin note carries (issue #74). When a straddle's kept (pre) era carries no
# gate-bearing check in the sample, the audit measures the NEW config from a thin post-change sample;
# `verify_report` binds a stamped `post_only_thin` era to this literal so a dropped provisional note
# FAILs too, and the direct-contradiction guard uses it to prove a pre-only disclosure never renders
# over a post_only_thin (all-post) measurement.
_CONFIG_ERA_THIN_MARKER = "treat these numbers as provisional"
# The marker the enumeration-binding clause carries when the OTHER configuration adds/removes
# checks that this single-era report does not enumerate (issue #69). `verify_report`'s
# check_era_enumeration_bound re-derives the bind from the stamped `other_era_checks`; this literal
# is the reader-facing half, coupled to that guard by the #69 fixtures.
_CONFIG_ERA_OTHER_CHECKS_MARKER = "not measured here"
# The reconciliation marker a recoverable ceiling above the typical wait must carry.
_RECOVERABLE_RECONCILE_MARKER = "slow-mode/worst-case figure on the PRs where"
# The renderer emits the reconciliation for any ceiling more than this above the wait. Kept
# DELIBERATELY TIGHTER than verify_report's `_RECOVERABLE_WAIT_TOL_S` (30.0): because the renderer
# fires at a lower excess than the guard demands, the marker is ALWAYS present by the time the guard
# checks (guard threshold 30 > this 1.5), so a coherent report never false-FAILs. The direction of
# the asymmetry is load-bearing — do NOT raise this above the guard's tolerance (that would open a
# band where the guard demands a marker the renderer didn't emit). Both are one-sided (excess ABOVE
# the wait only); this is clock-rounding slack, not a mirror of the guard's magnitude.
_RECONCILE_TOL_S = 1.5


def _iso_age_phrase(iso_from: str, iso_to: str = "") -> str:
    """A human '~N days/hours ago' between two ISO 8601 instants. `iso_to` defaults to the
    audit capture time (or now). 'recently' when the FROM instant can't be parsed or the delta
    is negative; an unparseable/absent TO instant falls back to render-time now (a real age
    phrase, not 'recently') — the disclosure stays honest without depending on a clean parse."""
    def _p(s: str) -> "datetime | None":
        try:
            return datetime.fromisoformat(
                str(s).replace("Z", "+00:00").replace("z", "+00:00"))
        except (ValueError, TypeError):
            return None
    a = _p(iso_from)
    if a is None:
        return "recently"
    b = _p(iso_to) if iso_to else None
    if b is None:
        b = datetime.now(timezone.utc)
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    secs = (b - a).total_seconds()
    if secs < 0:
        return "recently"
    days = int(secs // 86400)
    if days >= 1:
        return f"~{days} day{'s' if days != 1 else ''} ago"
    hours = int(secs // 3600)
    if hours >= 1:
        return f"~{hours} hour{'s' if hours != 1 else ''} ago"
    return "less than an hour ago"


def _era_other_checks_clause(e: dict[str, Any], lead: str, tail: str) -> str:
    """Issue #69: the sentence naming the checks the OTHER configuration adds/removes — the checks
    `collect_runs._era_scope_enumeration` bound OUT of this single-era report (stamped as
    `other_era_checks` on the straddle fact). `lead` frames the direction (adds, for disclosed_pre;
    ran, for post_only) and carries `_CONFIG_ERA_OTHER_CHECKS_MARKER`; `tail` is the re-run / dropped
    coda. Returns a leading-space-prefixed clause, or "" when the fact stamps no other-era checks
    (nothing to name) or predates the #69 stamps (legacy artifact — the caller's era note still
    renders, only the naming clause is absent)."""
    names = [str(n) for n in (e.get("other_era_checks") or []) if str(n)]
    if not names:
        return ""
    listed = ", ".join(f"`{_clean_label(n)}`" for n in names)
    return f" {lead}: {listed} — {tail}."


def _era_fact_spine_relevant(e: dict[str, Any]) -> bool:
    """Issue #116: may this straddle fact carry the GLOBAL "the headline reflects the old/thin
    config" caveat — i.e. does it actually touch the PR-gating spine?

    A straddle whose workflow never gates a PR (neither a developer-timed spine check nor a canonical
    `_PR_TRIGGER_EVENTS` trigger — `pull_request` / `pull_request_target` / `merge_group`) or that
    contributed NO check to the bound enumeration (`kept_checks` + `other_era_checks` both empty)
    cannot affect the headline pole the global caveat impugns: a config change in a push-only /
    cron-only workflow measures stale runner-minutes (the bill-scope note in Data sources still
    discloses that) but does NOT make the merge-wait headline reflect the previous config.

    `collect_runs._era_stamp_spine_relevance` stamps `spine_relevant` once the enumeration is bound
    (folding a developer-timed spine check with the workflow's triggers) — trust it when present. A
    LEGACY artifact predating that stamp is
    re-derived from the enumeration sets ALREADY on the fact (issue #69+): both empty ⇒ spine-
    irrelevant ⇒ suppress. A truly pre-enumeration fact (neither set stamped) is not re-derivable, so
    default to rendering — byte-identical to pre-#116 and matching verify_report's own narrow skip."""
    sr = e.get("spine_relevant")
    if isinstance(sr, bool):
        return sr
    if "kept_checks" in e or "other_era_checks" in e:
        return bool(e.get("kept_checks") or e.get("other_era_checks"))
    return True


def _config_era_disclosure_lines(cp: dict[str, Any], captured_at: str = "") -> list[str]:
    """Issue #66: the config-era disclosure blockquote(s), rendered PROMINENTLY near the
    headline. When a workflow's sample straddled its last-change commit, the engine kept
    ONE era (never both) and stamped the fact in `pr_critical_path.config_eras`; this
    surfaces it so the reader never mistakes a retired-config measurement for the current
    one, nor a narrowed window for a full sample. Returns [] when nothing straddled (the
    byte-identical no-op). Two shapes:
      * `disclosed_pre` — the post-change sample was too thin to drill AND the pre era carried a
        gate-bearing check, so the audit measures the PREVIOUS config: a loud ⚠️ line carrying
        `_CONFIG_ERA_DISCLOSED_MARKER`.
      * `post_only_thin` — the post-change sample was too thin AND the pre era carried NO
        gate-bearing check in the sample (the gate PRs are all post-change, issue #74), so measuring
        the old config is unavailable: the audit measures the NEW config from the thin post sample,
        a loud ⚠️ line carrying `_CONFIG_ERA_THIN_MARKER` (provisional numbers).
      * `post_only` — the audit narrowed to the current config: a lighter note that the
        earlier (retired-config) runs were excluded so no drill-down blends the two."""
    eras = [e for e in (cp.get("config_eras") or [])
            if isinstance(e, dict) and e.get("workflow_file")]
    if not eras:
        return []
    out: list[str] = []
    for e in eras:
        if e.get("rule") != "disclosed_pre":
            continue
        # Issue #116: a non-gating (push/cron-only) or check-neutral straddle can't touch the
        # headline this global caveat impugns — its bill-side staleness stays in the Data-sources
        # bill-scope note, but it must NOT globalize "the headline reflects the OLD config".
        if not _era_fact_spine_relevant(e):
            continue
        wf = _clean_label(str(e.get("workflow_file")))
        ago = _iso_age_phrase(str(e.get("boundary") or ""), captured_at)
        post_n = int(_num(e.get("post_count")) or 0)
        need = int(_num(e.get("sufficiency_min")) or 0)
        # Multi-boundary (issue #66): the workflow also changed EARLIER in the window, so the
        # kept pre-side was narrowed to the single era immediately before the latest change —
        # say so, so "the configuration BEFORE it" is unambiguous (not a blend of older configs).
        multi = " (it also changed earlier in the window; this reflects the configuration in " \
                "effect just before the latest change, not a blend)" if e.get("multi_change") else ""
        # Issue #69: the enumeration is bound to the KEPT (pre) era, so the NEW config's checks
        # (observed only on the excluded post-change PRs) are NOT rendered as poles/bars — name them
        # here so the reader knows the other configuration adds checks this report doesn't measure.
        # The enclosing sentence already ends with the "Re-run once post-change history
        # accumulates…" call-to-action, so the naming clause's tail must NOT repeat it (Greptile
        # P2) — it states why the added checks are unmeasured (the thin post-change sample that IS
        # the disclosed_pre condition) and leaves the single re-run CTA to the coda below.
        adds = _era_other_checks_clause(
            e, f"The new configuration adds checks {_CONFIG_ERA_OTHER_CHECKS_MARKER}",
            "too few post-change runs to measure them yet")
        out += [
            f"> **⚠️ `{wf}` changed {ago} — this audit {_CONFIG_ERA_DISCLOSED_MARKER}.** "
            f"Only {post_n} sampled run{'s' if post_n != 1 else ''} have run since that "
            f"change (fewer than the {need} needed to measure the new config), so the "
            f"headline and every drill-down below reflect the configuration BEFORE it{multi}.{adds} "
            f"Re-run once post-change history accumulates for the current config's numbers.",
            ""]
    for e in eras:
        if e.get("rule") != "post_only_thin":
            continue
        # Issue #116: same spine-relevance gate as disclosed_pre — a thin-flip on a straddle that
        # touches no PR-gating spine check can't caveat the headline (its bill-side note is enough).
        if not _era_fact_spine_relevant(e):
            continue
        # Issue #74: the kept (pre) era carried no gate-bearing check in the sample, so the audit
        # measures the NEW config from a thin post-change sample. Loud ⚠️ (like disclosed_pre), but
        # the honest direction — the numbers ARE the current config, only provisional.
        wf = _clean_label(str(e.get("workflow_file")))
        ago = _iso_age_phrase(str(e.get("boundary") or ""), captured_at)
        post_n = int(_num(e.get("post_count")) or 0)
        # Converse of disclosed_pre's naming clause: name any check the RETIRED config ran but the
        # current one dropped (observed only on the excluded pre-change PRs). In the pure #74 shape
        # the pre side is check-empty, so this is usually absent — but keep it for the rare mixed run.
        removes = _era_other_checks_clause(
            e, f"The previous configuration ran checks {_CONFIG_ERA_OTHER_CHECKS_MARKER}",
            "the current configuration no longer runs them")
        out += [
            f"> **⚠️ `{wf}` changed {ago} — this audit measures ONLY the new configuration on a "
            f"thin sample.** Only {post_n} sampled run{'s' if post_n != 1 else ''} have run on the "
            f"new configuration (the gate-bearing PRs are all post-change, so there is no pre-change "
            f"gate run to measure the old config) — {_CONFIG_ERA_THIN_MARKER}; re-run as post-change "
            f"history accumulates for stable numbers.{removes}",
            ""]
    for e in eras:
        if e.get("rule") != "post_only":
            continue
        wf = _clean_label(str(e.get("workflow_file")))
        ago = _iso_age_phrase(str(e.get("boundary") or ""), captured_at)
        post_n = int(_num(e.get("post_count")) or 0)
        pre_n = int(_num(e.get("pre_count")) or 0)
        # Issue #69 (converse): the enumeration is bound to the kept (post) era, so any check the
        # RETIRED config ran but the current one dropped (observed only on the excluded pre-change
        # PRs) is named here rather than silently vanishing.
        removes = _era_other_checks_clause(
            e, f"The previous configuration ran checks {_CONFIG_ERA_OTHER_CHECKS_MARKER}",
            "the current configuration no longer runs them")
        out += [
            f"> **`{wf}` changed {ago} — {_CONFIG_ERA_NARROWED_MARKER}.** This audit "
            f"measures only the {post_n} run{'s' if post_n != 1 else ''} since that change; "
            f"the {pre_n} earlier run{'s' if pre_n != 1 else ''} measured the retired "
            f"configuration and were excluded so no drill-down blends the two.{removes}",
            ""]
    # Bill-scope honesty note MOVED OUT (owner UX edit 2026-07-19): it's methodology (it
    # qualifies the runner-minute / cost-spine figures, not the headline), so it now renders
    # in the 🗄️ Data sources section via `_bill_scope_era_note`, not here in the top matter.
    return out


def _bill_scope_era_note(doc: dict[str, Any]) -> list[str]:
    """Owner UX edit (2026-07-19): the config-era bill-scope methodology note. When a
    workflow's sample straddled its last-change commit, the engine partitions ONLY the PR
    critical-path spine + drill to one era; the runner-minute / cost-spine figures keep the
    FULL sample (they size total compute, not the merge wait), so they still include the
    earlier configuration and a duration-/structure-changing edit blends both layouts.
    Previously folded into the top-matter era blockquote; moved DOWN into 🗄️ Data sources
    (it's methodology, not headline). Rendered only when a straddle is stamped
    (`pr_critical_path.config_eras` non-empty); [] otherwise (byte-identical no-op)."""
    cp = doc.get("pr_critical_path") or {}
    eras = [e for e in (cp.get("config_eras") or [])
            if isinstance(e, dict) and e.get("workflow_file")]
    if not eras:
        return []
    return [
        "> _The runner-minute / cost-spine figures in this report keep the full sample by "
        "design (they size total compute, not the critical path), so they still include the "
        "earlier configuration; a duration- or structure-changing edit (e.g. a shard split) "
        "blends both layouts._",
        ""]


def _fold_bottom_line(out: list[str], *blocks: list[str]) -> None:
    """Owner UX edit (2026-07-19): the top matter is metadata table → ONE Bottom-line
    blockquote → Contents, and nothing else. Every disclosure that used to render as its
    OWN blockquote between the headline and the Contents (the folded headline claim, the
    config-era caveat, the fileless/managed disclosure, the chain model-check, the "After
    the gate" runner-minute line) folds into the single Bottom-line blockquote that `out`
    already ends with. Each `block` is a list of already-`>`-prefixed markdown lines
    (its internal `""` paragraph breaks become `">"` continuations so no blank line
    splits the quote); empty/whitespace-only blocks are skipped. The Bottom-line block's
    trailing `""` terminator is popped, every block joined with a `">"` separator, and one
    `""` re-added to close the whole quote. Line TEXT is preserved byte-for-byte — the
    claims manifest matches each claim's `rendered` by exact substring and the era guards
    are text-anchored (position-independent), so every claim + verify check stays green."""
    if out and out[-1] == "":
        out.pop()
    for block in blocks:
        lines = list(block)
        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()
        if not lines:
            continue
        out.append(">")  # blank-line-free separator: keeps it ONE blockquote
        for ln in lines:
            out.append(ln if ln != "" else ">")
    out.append("")


def _recoverable_reconciliation(ceiling_s: Any, typical_wait_s: Any) -> str:
    """Issue #66 fix 2: a recoverable 'up to ~X' ceiling that EXCEEDS the headline typical
    merge wait is incoherent read alone — a fix cannot give back more than the whole wait a
    TYPICAL PR sees. Reconcile in place: the ceiling is the check's conditional/worst-case
    (slow-mode) figure on the PRs where it IS the pole, which exceeds the typical wait
    because the check runs this long on only a minority of PRs. Returns '' when the ceiling
    is within the wait (no incoherence to reconcile), or when no typical wait is known
    (`w <= 0`) — so a well-behaved report, or one with no rendered wall, is unchanged."""
    c = _num(ceiling_s) or 0.0
    w = _num(typical_wait_s) or 0.0
    if w <= 0 or c <= w + _RECONCILE_TOL_S:
        return ""
    return (f" (This is the {_RECOVERABLE_RECONCILE_MARKER} this check is the pole; it "
            f"exceeds the ~{_clock(w)} typical merge wait because the check runs this long "
            f"on only a minority of PRs — recovering it speeds those PRs, not the median.)")


def _fileless_disclosure_lines(cp: dict[str, Any]) -> list[str]:
    """Issue #12: the fileless/managed status-check disclosure blockquote, framed as PR-lifetime
    status-gating latency (NOT CI compute). Returns [] when nothing is stamped.

    Shared by EVERY render site that can carry the stamp — the measured report, the degenerate
    early-return, AND the static-only body — so the marker phrase `verify_report` binds to and the
    slowest-span format are emitted from ONE place, and no path can silently drop the disclosure
    the engine stamped (the static-only short-circuit used to). The all-fileless flag renders the
    honest degenerate line; otherwise the per-gate "disclosed, not headlined" line."""
    checks = [c for c in (cp.get("fileless_status_checks") or [])
              if isinstance(c, dict) and c.get("name")]
    if not checks:
        return []
    fl = checks[0]
    name = _lbl(_clean_label(str(fl.get("name", ""))))
    span = _clock(_num(fl.get("span_s")))
    n_more = len(checks) - 1
    more = (f" (+{n_more} more fileless status check{'s' if n_more != 1 else ''})"
            if n_more > 0 else "")
    if cp.get("all_checks_fileless"):
        return ["> **Every gating check here is fileless.** On these PRs every tracked check is a "
                "fileless/managed status check (a bot gate, a label gate, or an external app) that "
                "produces no CI workflow job — there is no CI compute to headline as the merge "
                f"wait. The slowest is `{name}` at ~{span}{more}, but that is PR-lifetime "
                "status-gating latency (how long the gate sat open on the PR), not CI wall-clock. "
                "No job-groundable check exists to crown.", ""]
    return [f"> **Fileless status gate (disclosed, not headlined).** `{name}` shows ~{span}{more}, "
            "but that span is PR-lifetime status-gating latency — how long a bot/label/external-app "
            "check sat open on the PR, not CI compute — so it is excluded from the merge-wait "
            "headline above (which measures what CI makes a typical PR wait). It is disclosed here "
            "so the wait is never hidden.", ""]


def _render_static_only(doc: dict[str, Any], captured_at: str = "",
                        cs: "claims.ClaimSet | None" = None) -> str:
    """Report body for a repo with NO measured critical path (no sampled run timing —
    an archived / brand-new / low-activity repo whose Actions history aged out, so
    `pr_critical_path.poles` is empty) but with static-scan findings present.

    Without this, `render` returned a one-line "_No measured critical path_" note and
    silently dropped every static hygiene finding (and shipped a verify-failing
    dead-end). This renders those findings honestly: a "no run history to measure"
    banner, the pre-start queue-wait section, the off-path hygiene appendix, and the
    full provenance + data-sources footer — the same building blocks the measured
    report uses, minus the (unmeasurable) spine.

    Returns "" when there is genuinely nothing static to report, so the caller can fall
    back to the one-line no-critical-path note."""
    repo = doc.get("repo", "repo")
    catalog_url = _build_catalog_url(doc.get("skill_commit_sha"))
    all_findings = _dedupe_findings(list(doc.get("findings") or []))
    cs = cs or claims.ClaimSet()
    tier2_lines = _tier2_block(all_findings, catalog_url, cs, doc)
    also_lines, also_count, _also_on_path = _also_noticed_block(
        all_findings, catalog_url, doc=doc)
    queue_lines = _queue_wait_block(all_findings, catalog_url)
    # `scan_incomplete` means a workflow file could NOT be scanned — an unscanned
    # file must never read as clean, so a populated coverage gap is itself something
    # to report even with zero findings. Only the truly-empty case (no findings AND
    # complete coverage) falls back to the caller's one-line note.
    incomplete = doc.get("scan_incomplete")
    cp = doc.get("pr_critical_path") or {}
    ds = doc.get("data_sources") or {}
    # A BROKEN measurement is itself the news, even with zero static findings. Without
    # this, a run whose every run-list fetch failed and which happens to have no static
    # findings rendered as the bare one-line "_No measured critical path in this
    # findings JSON._" — no banner, no coverage note, no data-sources footer: the
    # loudest failure in the collector, rendered as a shrug.
    broken = _measurement_is_broken(ds)
    if (not tier2_lines and not also_lines and not queue_lines
            and not incomplete and not broken):
        return ""  # nothing static to say — caller keeps the one-line note

    sampled = cp.get("sampled_pr_count")
    target = cp.get("sample_target")
    runs_n = ds.get("runs_sampled")
    timed_n = runs_n if isinstance(runs_n, int) and runs_n > 0 else 0
    # "no run timing" must stay literally true: when non-PR (schedule/push) runs WERE
    # timed — they price the runner-minute findings below — the missing thing is PR
    # run timing specifically, and saying otherwise contradicts the report's own body.
    _no_timing = "no PR run timing" if timed_n else "no run timing"
    if isinstance(sampled, int) and isinstance(target, int) and target:
        scope = f"sampled {sampled} of {target} PRs but found {_no_timing}"
    elif isinstance(runs_n, int):
        scope = f"sampled {runs_n} runs"
    else:
        scope = "found no sampled run timing"
    # Which of the repo's CURRENT workflows can gate a PR at all? `workflow_triggers`
    # is the collector's static `on:` parse of every scanned workflow. When it is
    # populated and NO workflow fires on a PR-developer event, the empty spine is a
    # property of the repo's CI SHAPE — schedule/push/tag/release-only CI (git
    # scrapers, data-refresh, mirrors, release-only projects) — not of its activity
    # level, and the dormant-repo hedge ("archived, brand-new, or low-activity …
    # aged out") is a confident wrong diagnosis of an actively-running repo.
    _wt = doc.get("workflow_triggers")
    _wt = _wt if isinstance(_wt, dict) else {}
    _pr_gating_wfs = [str(w) for w, evs in _wt.items()
                      if isinstance(evs, list) and _PR_GATING_EVENTS.intersection(
                          str(e) for e in evs)]
    no_pr_gating = bool(_wt) and not _pr_gating_wfs

    # Machine-readable static-only marker (an HTML comment, invisible in rendered
    # markdown). `verify_report._is_static_only` keys off THIS, not the human banner
    # prose below — so a MEASURED report whose LLM gap-fill / evidence text happens to
    # quote the banner phrase can't be misclassified static-only (which would fail the
    # spine invariants open). Emitted only here, by the static-only renderer.
    out: list[str] = [_STATIC_ONLY_MARKER, "",
                      f"# {repo} — why is the merge slow?", ""]
    out += _metadata_table(doc, repo)
    # An unscanned file must never read as clean — keep the coverage warning on top.
    out += _coverage_gap_banner(doc.get("scan_incomplete"))
    # WHY there is no measured spine decides what this banner may claim. A BROKEN FETCH
    # (the workflow-list fetch died; the breaker aborted the gh pass; every run-list
    # fetch 5xx'd) produces the same empty `pr_critical_path` as a genuinely dormant
    # repo — and the dormant-repo banner ("an archived, brand-new, or low-activity repo
    # whose run history aged out") is then a confident, WRONG diagnosis that invites the
    # reader to conclude their CI is quiet.
    #
    # Branch on the DATA, and on the INVERTED test (`_measurement_is_broken`): only
    # "the gh pass never ran" may render the quiet-repo banner. This once branched on
    # `partial_kind == "collection_failed"` alone — which closed the hole for ONE of the
    # five kinds and left `workflow_missing` (the kind a total run-list wipeout actually
    # stamps) rendering "an archived, brand-new, or low-activity repo" under a passing
    # verify gate. The honest note was 50 lines below in the footer; the headline
    # takeaway was "my CI is quiet".
    if broken:
        _why = str(ds.get("partial_reason") or "the gh API pass failed").rstrip(".")
        _severe = _derived_partial_kind(ds) not in _PARTIAL_MINOR_KINDS
        _head = ("**Collection FAILED — this is not a quiet repo.**" if _severe else
                 "**Collection was INCOMPLETE — this may not be a quiet repo.**")
        # Name every vanished workflow — but only those the reason didn't already name,
        # so the banner doesn't list them twice (`partial_reason` names them when
        # collect_runs wrote it; a hand-built or legacy doc may not).
        _unnamed = [w for w in _sample_gap_workflows(ds) if w not in _why]
        _named = ""
        if _unnamed:
            _named = (" Workflows MISSING from the sample (their fetch failed, so they "
                      "are absent, not empty): "
                      + ", ".join(f"`{w}`" for w in _unnamed) + ".")
        out += ["> [!IMPORTANT]",
                f"> {_head} {_why}.{_named} ci-speedup "
                "could NOT measure the merge critical path because it could not READ the "
                "run history, so the absence of a spine below is a hole in the data, "
                "**not** evidence that your CI is fast or idle. What follows is "
                "**static-only**: workflow hygiene from pattern analysis, scoped to the "
                "**cloud bill** (runner-minutes), not measured developer wall-clock. "
                "Re-run when the GitHub API is reachable again (e.g. once a rate limit "
                "has reset) to get the measured wall-clock critical path.", ""]
    elif no_pr_gating:
        # Third shape (issue: false dormant banner on live shapes) — the repo may be
        # perfectly active; its CI just never gates a PR. Say THAT, own any timing we
        # did measure, and never speculate about archived/aged-out history.
        _timed_note = (f" It did sample **{timed_n} timed run(s)** from those non-PR "
                       "workflows — they price the runner-minute findings below."
                       if timed_n else "")
        out += ["> [!IMPORTANT]",
                "> **No PR-gating CI to measure.** None of this repo's current "
                "workflows run on `pull_request` / `pull_request_target` / "
                "`merge_group` — CI here is schedule/push/tag/release-triggered — so "
                "there is no merge critical path to rank: PRs do not wait on these "
                f"workflows.{_timed_note} What follows is **static-only**: workflow "
                "hygiene from pattern analysis, scoped to the **cloud bill** "
                "(runner-minutes), not measured developer wall-clock. If PR-triggered "
                "CI is added later, re-run to get the measured wall-clock critical "
                "path.", ""]
    else:
        # A PR-gating workflow EXISTS but produced no sampled PR run timing. When
        # non-PR runs WERE timed, the repo is demonstrably not dormant — say what is
        # actually missing instead of hedging about archived/aged-out history. The
        # since-deleted-workflows clause covers the real shape where gh shows recent
        # PR runs but every one belongs to a workflow file that no longer exists
        # (ci-speedup excludes those: their YAML can't be audited).
        _why_empty = (
            f"the repo has recent Actions runs — {timed_n} sampled — but none from a "
            "PR-gating workflow in the sample window"
            if timed_n else
            "an archived, brand-new, or low-activity repo whose GitHub Actions run "
            "history aged out, or one whose recent PR runs belong to since-deleted "
            "workflows, which ci-speedup excludes")
        out += ["> [!IMPORTANT]",
                f"> **No run history to measure.** ci-speedup {scope} ({_why_empty}), "
                "so it could NOT measure the merge critical path — there is no ranked "
                "spine of what your PRs wait on, and the PR-floor fallback can't synthesize "
                "one without run timing. What follows is **static-only**: workflow hygiene "
                "from pattern analysis, scoped to the **cloud bill** (runner-minutes), not "
                "measured developer wall-clock. Re-run once the repo has recent Actions runs "
                "to get the measured wall-clock critical path.", ""]
    # Mention the hygiene count only when there ARE hygiene findings: a queue-only
    # repo has 0 hygiene groups (`_also_noticed_block` excludes wait patterns), and
    # printing "0 static hygiene findings" beside a populated Pre-start wait section
    # would read as a bug. Call out the queue section on its own axis — it IS
    # pre-start developer wall-clock, not a bill-only cleanup, so it must not inherit
    # the hygiene "won't change wall-clock" framing.
    # The bottom line must not undo the banner. "No run history was available" is a
    # statement about the REPO; when the fetch broke, the truth is a statement about the
    # AUDIT — the run history may be perfectly healthy and we simply failed to read it.
    if broken:
        bottom = ["> **Bottom line.** The run history could NOT be read (the gh API "
                  "pass failed), so this report can't rank what a PR waits on before "
                  "the merge — and that silence is a hole in the data, not a verdict "
                  "on your CI."]
    elif no_pr_gating:
        bottom = ["> **Bottom line.** This repo's current workflows never run on PRs, "
                  "so there is no merge wait to rank — the findings below are about "
                  "the cloud bill (runner-minutes), not developer wall-clock."]
    else:
        bottom = ["> **Bottom line.** No PR run timing was available, so this report "
                  "can't rank what a PR waits on before the merge."]
    if also_count:
        plural = "s" if also_count != 1 else ""
        # The no-PR-gating shape has no merge wall-clock to unlock EVER (no workflow
        # gates a PR), so "until there is run timing" would wrongly promise one.
        _tail = ("fixing them won't change a merge wait — no workflow here gates PRs."
                 if no_pr_gating else
                 "fixing them won't measurably change merge wall-clock until there "
                 "is run timing to measure.")
        bottom.append(f" It does carry **{also_count} static hygiene finding{plural}** "
                      "below — workflow issues found by static analysis that cut the "
                      f"cloud bill (runner-minutes); {_tail}")
    if queue_lines:
        bottom.append(" A **pre-start wait (queue-time)** section is shown below too — "
                      "that IS developer wall-clock (time a PR waits before its jobs "
                      "start), measured independently of the missing critical-path spine.")
    out += ["".join(bottom), ""]
    out += _tier2_bottom_line(all_findings, cs, doc)
    if queue_lines:
        out += ["---", "", *queue_lines]
    if tier2_lines:
        out += ["---", "", *tier2_lines]
    if also_lines:
        out += ["---", "", *also_lines]
    out += _dropped_unprovable_banner(cp.get("dropped_unprovable")
                                      or doc.get("dropped_unprovable"))
    # Issue #12: a static-only report (no measured pole to crown) can still carry a stamped
    # fileless/managed status check — an all-fileless repo that ALSO has static hygiene findings
    # renders HERE, not through `render`'s degenerate arm. Surface the disclosure so the excluded
    # gate is never silently dropped just because the static path won the short-circuit above.
    _fileless_lines = _fileless_disclosure_lines(cp)
    if _fileless_lines:
        out += ["---", "", *_fileless_lines]
    # Issue #66: a config-era boundary can be stamped even on a static-only report; surface it
    # so a straddle is never silently dropped just because the static path won the short-circuit.
    _era_lines = _config_era_disclosure_lines(cp, captured_at)
    if _era_lines:
        out += ["---", "", *_era_lines]
    out += _data_sources_footer(doc, repo)
    out += _starsling_footer()
    return _strip_emdashes("\n".join(out))


# The `ClaimSet` built by the MOST RECENT `render()` call. `render()`'s signature
# (and its str-only return) is depended on directly by ~30 call sites in
# `test_blocking_path.py` and other same-skill modules — changing it to also
# return the claims would be a much larger, riskier diff than this increment's
# scope. Stashing the set here lets `main()` (same module, called right after
# `render()`) pick it up to write the `<report>.claims.json` manifest, without
# touching `render()`'s public contract. Reset at the top of every `render()`
# call so a stale set from a prior render can never leak into a new manifest.
_LAST_CLAIMS: claims.ClaimSet | None = None


def render(doc: dict[str, Any], logs: dict[str, str] | None = None,
           samples: dict[str, dict[str, Any]] | None = None,
           log_runs: dict[str, str] | None = None,
           captured_at: str = "",
           steps: dict[str, dict[str, Any]] | None = None,
           mags: dict[str, dict[str, Any]] | None = None,
           analyses: dict[str, dict[str, Any]] | None = None) -> str:
    global _LAST_CLAIMS
    logs = logs or {}
    samples = samples or {}
    log_runs = log_runs or {}
    steps = steps or {}
    mags = mags or {}
    analyses = analyses or {}
    cs = claims.ClaimSet()
    _LAST_CLAIMS = cs
    cp = doc.get("pr_critical_path") or {}
    poles = sorted((p for p in (cp.get("poles") or []) if p.get("check")),
                   key=lambda p: -(_num(p.get("p50_s")) or 0.0))
    if not poles:
        # No measured critical path (an archived / brand-new / low-activity repo whose
        # Actions run history aged out, so collect_runs sampled 0 runs and
        # per_workflow_timing is all-zero -> no poles). The PR-floor fallback can't help
        # either: it synthesizes a spine from per_workflow_timing, which is all-zero when
        # zero runs are sampled. But the STATIC scan may still have found real workflow
        # hygiene findings — rendering only the one-line note would silently drop them and
        # ship a verify-failing dead-end. Render the static findings honestly instead;
        # fall back to the one-line note only when there is genuinely nothing static.
        _static = _render_static_only(doc, captured_at, cs)
        if _static:
            return _static
        # Issue #12 degenerate case: no job-groundable pole to crown, but the sampled PRs DID
        # carry fileless/managed status checks (all-fileless repo, or every gating check is a
        # bot/label/app with no sampled workflow job). Say so honestly — name the slowest gate and
        # frame it as PR-lifetime status-gating latency — rather than the bare no-critical-path
        # note that would silently drop the disclosure the engine stamped.
        _deg_fileless_lines = _fileless_disclosure_lines(cp)
        # Issue #66: a config-era straddle is stamped in collect() independent of whether any pole is
        # crownable, so `config_eras` can be non-empty here while `poles` is empty. Surface it on
        # BOTH degenerate arms (mirroring the static-only guard above and the fileless handling), so
        # a straddle is never silently dropped just because a shorter render path won the
        # short-circuit — else a post_only sample looks full / a disclosed_pre sample looks current.
        _deg_era_lines = _config_era_disclosure_lines(cp, captured_at)
        if _deg_fileless_lines or _deg_era_lines:
            # `_strip_emdashes` at this early-return boundary mirrors the main render exit:
            # this path bypasses that terminal scrub, so without it the typographic dashes in the
            # shared disclosure prose would survive and trip verify_report's ASCII-hyphens-only
            # invariant on a real all-fileless degenerate repo.
            return _strip_emdashes("\n".join([
                f"# {doc.get('repo', 'repo')} — why is the merge slow?", "",
                *_deg_era_lines, *_deg_fileless_lines]))
        return "_No measured critical path in this findings JSON._"
    repo = doc.get("repo", "repo")
    catalog_url = _build_catalog_url(doc.get("skill_commit_sha"))
    all_findings = _dedupe_findings(list(doc.get("findings") or []))
    # When adaptive sampling capped some workflows at the shallow depth (and didn't
    # deepen every candidate), the off-path findings aggregating those workflows are
    # approximate — flag it on the appendix so the figures aren't read as exact.
    _ds = doc.get("data_sources") or {}
    shallow_note = ""
    # Fire when a CAPPED workflow was left at shallow depth (not just when a PR pole
    # went un-deepened) — a shallow cron/push workflow feeding off-path hygiene figures
    # must be disclosed even if every PR pole was deepened.
    _wall_shallow = _ds.get("shallow_remaining_workflow_count")
    if _wall_shallow is None:
        _wall_shallow = max(
            0, int(_ds.get("capped_workflows") or 0)
            - int(_ds.get("deepened_workflows") or 0))
    _cost_shallow = _ds.get("cost_spine_shallow_workflow_count")
    if _cost_shallow is None:
        _cost_shallow = 0
    if (_wall_shallow or 0) > 0 or (_cost_shallow or 0) > 0:
        parts = []
        if (_wall_shallow or 0) > 0:
            parts.append(
                f"{_wall_shallow} capped workflow(s) still use the shallow "
                f"{_ds.get('shallow_runs')}-run job sample for finding/queue values")
        if (_cost_shallow or 0) > 0:
            parts.append(
                f"{_cost_shallow} runner-minute source workflow(s) still use a shallow "
                f"{_ds.get('shallow_runs')}-run cost-spine sample")
        shallow_note = (
            "Approximate: computed across all workflows, but " + "; ".join(parts)
            + f". Figures can shift run-to-run; re-run with `--shallow-runs "
            f"{_ds.get('max_runs')}` to confirm exact values.")
    # Pre-start wall-clock wait (queue time) — its own section, ABOVE the runner-minute
    # hygiene appendix, because it IS developer wall-clock (the spine just doesn't see
    # the pre-start portion).
    queue_lines = _queue_wait_block(all_findings, catalog_url, shallow_note)
    runner_spine_count = _runner_minute_spine_row_count(doc)
    tier2_lines = _tier2_block(all_findings, catalog_url, cs, doc)

    # How often each check ACTUALLY gates (is the pole) across the sampled PRs. A
    # slow-but-rare check (e.g. an AI review bot on maintainer PRs only) must not
    # be crowned over a check that gates almost every PR.
    pole_count, present, npop = _gate_counts(cp)
    if not npop:
        # M2 emitted no populations (no bimodal check) — fall back to the data layer's
        # per-check presence (always computed from the per-PR maps) so rare-pole demotion
        # still works. `pole_count` stays empty (it needs the populations), which only
        # softens the workflow-vs-fileless frequency tie-break, not the typical/rare split.
        # Use `check_present_n_pr` as the denominator, NOT `sampled_pr_count`: a 0 there
        # means presence is UNKNOWN (don't demote), whereas sampled_pr_count>0 with all
        # present_on==0 would wrongly mark every pole rare.
        _checks = cp.get("checks") or []
        _pres = {str(c.get("name", "")): int(c.get("present_on") or 0) for c in _checks}
        _denom = int(cp.get("check_present_n_pr") or 0)
        # Only demote on a COMPLETE presence map. If ANY check lacks `present_on` the doc is
        # partially populated (a pre-format / hand-built doc), so its presence is unreliable —
        # treat that as UNKNOWN and skip the fallback (nothing demoted), rather than reading a
        # missing key as present_on==0 and silently marking those checks rare. Same
        # "unknown → don't demote" stance as the 0-denominator guard.
        _complete = all(c.get("present_on") is not None for c in _checks)
        if _pres and _denom and _complete:
            present, npop = _pres, _denom

    def _is_file_pole(p: dict[str, Any]) -> bool:
        wf = str(p.get("workflow_file") or "")
        return wf.endswith((".yml", ".yaml")) and bool(p.get("job"))

    def _gatef(p: dict[str, Any]) -> int:
        return pole_count.get(str(p.get("check", "")), 0)

    # Gate frequency per WORKFLOW = how often ANY check in that workflow is the per-PR slowest,
    # summed over ALL of the workflow's checks (Class A #2). Summing over the representative `poles`
    # only dropped a non-representative matrix sibling leg's `pole_count` — so a workflow whose pole
    # is held by different legs on different PRs undercounted (razorpay/blade `blade-validate` rendered
    # "gates 4/20" vs the true 5/20). `pole_count` (from `_gate_counts`) is keyed per check, and only
    # one check is the slowest on any given PR, so summing a workflow's checks never double-counts a PR.
    wf_gate: dict[str, int] = {}
    _gate_checks = cp.get("checks") or []
    if _gate_checks:
        for c in _gate_checks:
            wf = str(c.get("workflow_file") or "")
            if wf.endswith((".yml", ".yaml")):
                wf_gate[wf] = wf_gate.get(wf, 0) + pole_count.get(str(c.get("name", "")), 0)
    else:
        # Minimal/synthetic doc with no per-check list: fall back to summing the representative
        # poles (the prior behavior) so a doc that carries no `checks[]` still reports a count.
        for p in poles:
            if _is_file_pole(p):
                wf = str(p.get("workflow_file", ""))
                wf_gate[wf] = wf_gate.get(wf, 0) + _gatef(p)

    # One representative pole per distinct JOB MATRIX (sibling legs collapse via
    # `_same_matrix`). POLE 1 is the typical gate (slowest by median); POLE 2+ are the
    # next distinct matrices by IMPACT = max(median, bimodal slow mode) - so the second
    # finding is the next-biggest LEVER (a bimodal `test` job that's a long gate on a
    # large share of PRs), not a higher-median but trivial check. Grouping by matrix,
    # not workflow, lets two jobs in ONE workflow both surface (langfuse).
    def _pole_impact(p: dict[str, Any]) -> float:
        bi = p.get("bimodal") or {}
        return max(_num(p.get("p50_s")) or 0.0, _num(bi.get("high_p50_s")) or 0.0)

    # A pole is part of the TYPICAL merge path only if it ran on a MAJORITY of sampled
    # PRs. A slow but rarely-run FILE pole — a label-gated / opt-in benchmark that ran on
    # a handful of PRs — must not headline "why is the merge slow?" just because it's long
    # when it does run; its cost is throughput, not merge-wait. On too small a sample
    # (npop < _RARE_PRESENCE_MIN_PR — matching the data layer) the fraction is noise, so
    # every pole counts as typical (prior behavior); a required check gates by definition
    # and is never demoted. Keeping the SAME threshold as collect_runs keeps the rendered
    # order/labels in agreement with `critical_path_check` and the data-pass summary.
    _required_names = {str(c) for c in (doc.get("required_checks") or [])}
    # Pole FREQUENCY (times a check is the ACTUAL critical path) drives the typical/rare split,
    # keyed off the engine's stamped `checks[].pole_n` — the same signal `_rank_spine_present_first`
    # ranks by, so the rendered demotion can't disagree with `critical_path_check`. Presence>half
    # (the legacy rule) demoted every heavy suite on a path-partitioned monorepo; pole-frequency
    # fixes that. `check_present_n_pr` is the sample-size floor (mirrors the data layer's `n_pr`).
    _pole_n = {str(c.get("name", "")): c.get("pole_n") for c in (cp.get("checks") or [])}
    _have_pole_n = bool(_pole_n) and all(v is not None for v in _pole_n.values())
    # A PARTIALLY-populated `pole_n` (some checks have it, some don't) is a malformed new-schema
    # doc, not a legacy one — the fresh engine always stamps `pole_n` on every check. Falling back
    # to the legacy presence rule there would silently re-introduce the phantom-gate bug, so make
    # it loud rather than degrading in silence (a hand-edited / merged findings file, or a field
    # rename). A fully-absent `pole_n` is a genuine pre-`pole_n` doc and uses the fallback quietly.
    if _pole_n and not _have_pole_n and any(v is not None for v in _pole_n.values()):
        print("⚠ pr_critical_path.checks has pole_n on only SOME checks — the typical/rare split "
              "is falling back to the legacy presence rule, which can disagree with "
              "critical_path_check. Regenerate the findings with the current engine.",
              file=sys.stderr)
    _nfloor = int(cp.get("check_present_n_pr") or 0)

    def _typical_check(name: str) -> bool:
        # ONE predicate for both the pole ordering AND the typical/minority label split, so a
        # demoted pole and its prose can't disagree. A required check gates by definition. On a
        # too-small sample the frequency is noise (everything typical). New findings use the
        # pole-frequency floor; findings that PREDATE `pole_n` fall back to the legacy presence
        # rule (unchanged) so old committed reports render identically.
        if name in _required_names:
            return True
        if _have_pole_n:
            if _nfloor < _RARE_PRESENCE_MIN_PR:
                return True
            return int(_pole_n.get(name) or 0) >= _POLE_RECUR_FLOOR
        if npop < _RARE_PRESENCE_MIN_PR:
            return True
        return present.get(name, 0) > npop * _RARE_PRESENCE_FRAC

    def _pole_typical(p: dict[str, Any]) -> bool:
        return _typical_check(str(p.get("check", "")))

    def _opt_in_rare_check(name: str) -> bool:
        # The OPT-IN / RARE class — the one the level-1 footnote frames "a typical PR doesn't wait
        # on it": demoted off the typical set (`not _typical_check`) AND present on a MINORITY of
        # sampled PRs. The presence clause (mirrors `_ms_freq_demoted`'s complement) is what keeps
        # a frequency-demoted leg present on EVERY PR (requests `build (…)`) OFF the stamp — its
        # matrix is genuinely on-path, so its OPT73/OPT24 wall-clock claim is TRUE, not a
        # contradiction. On too small a sample presence is noise, so nothing is opt-in-demoted.
        if _typical_check(name):
            return False
        return npop >= _RARE_PRESENCE_MIN_PR and present.get(name, 0) <= npop * _RARE_PRESENCE_FRAC

    # Stamp `spine_rare` on findings whose job maps ONLY to opt-in/rare spine checks, using the
    # SAME typical/presence test the spine footnote renders — so `_saves_wall_clock` can't frame a
    # presence-demoted (opt-in) job "sits ON the merge-gating critical path" in the appendix while
    # the spine demotes it as opt-in (the paradedb `Test pg_search` double-framing). Done here,
    # once `_typical_check` + `present`/`npop` exist and before the appendix (`_also_noticed_block`)
    # and the per-pole `_data_driven_for_pole` join both read `_saves_wall_clock`.
    _stamp_spine_rare(all_findings, cp.get("checks") or [], _opt_in_rare_check)
    # The set of spine check NAMES the footnote demotes as opt-in / rare — passed to the
    # appendix so an on-path Where line never LEADS with a demoted leg (a multi-leg OPT73
    # cluster finding is on-path via its TYPICAL leg, but `affected_jobs[0]` may be a
    # demoted sibling: display the typical one instead of double-framing the demoted name).
    _spine_rare_names = {str(c.get("name", ""))
                         for c in (cp.get("checks") or [])
                         if str(c.get("name", "")) and _opt_in_rare_check(str(c.get("name", "")))}

    def _by_p50(ps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(ps, key=lambda p: -(_num(p.get("p50_s")) or 0.0))

    # Among TYPICAL poles, order by how often each is the ACTUAL per-PR gate (its pole_count),
    # tie-broken by p50. Presence (`_pole_typical`) only proves a check RAN on most PRs — not
    # that it's the one most PRs wait on. A check present on every PR but slower than the real
    # gate on only a handful of them (flwrlabs/flower: `datasets-test` is the slowest on 3/20
    # while `framework-test` gates 17/20) must NOT head the list just because its global p50 is
    # highest. A REQUIRED check gates every PR by definition (must pass to merge), so it ranks
    # above the frequency-counted ones regardless of how often the sample shows it slowest —
    # mirroring the `_pole_typical` required exemption so a required gate still headlines. When
    # pole_count is unavailable (no populations) `_gatef` is 0 for all, so this reduces to the
    # prior pure-p50 order — and a single typical pole is unaffected either way.
    def _gate_rank(p: dict[str, Any]) -> int:
        if str(p.get("check", "")) in _required_names:
            return npop + 1
        return _gatef(p)

    def _by_gate_then_p50(ps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(ps, key=lambda p: (-_gate_rank(p), -(_num(p.get("p50_s")) or 0.0)))

    # Typical poles first, rare ones demoted below — so POLE 1 is the most-gating TYPICAL
    # gate, not the slowest job overall (which can be a rare opt-in benchmark).
    _file_all = [p for p in poles if _is_file_pole(p)]
    file_poles = (_by_gate_then_p50([p for p in _file_all if _pole_typical(p)])
                  + _by_p50([p for p in _file_all if not _pole_typical(p)]))
    by_matrix: list[dict[str, Any]] = []
    for p in file_poles:
        if not any(_same_matrix(str(p.get("check", "")), str(q.get("check", "")),
                                str(p.get("workflow_file", "")), str(q.get("workflow_file", "")))
                   for q in by_matrix):
            by_matrix.append(p)
    if by_matrix:
        # gate_rep = the most-gating typical pole (head of the typical-first order). The rest
        # keep the tiering — remaining typical poles (by impact) before demoted rare ones —
        # so a rare giant never outranks a real common gate even as POLE 2+.
        gate_rep = by_matrix[0]
        _rest_typ = sorted((p for p in by_matrix[1:] if _pole_typical(p)),
                           key=lambda p: -_pole_impact(p))
        _rest_rare = sorted((p for p in by_matrix[1:] if not _pole_typical(p)),
                            key=lambda p: -_pole_impact(p))
        pole_wfs = [gate_rep, *_rest_typ, *_rest_rare][:_TOP_WORKFLOWS]
    else:
        pole_wfs = []
    # Stash each pole's per-PR co-occurrence (from `populations`) on the pole dict, so the floor
    # helpers (`_floor_note`/`_pole_addressable` → `_binding_floor`/`_floor_candidate`) can size the
    # addressable ceiling against the slowest sibling that co-occurs on a MAJORITY of THIS pole's
    # own gating PRs — not the legacy global-presence cutoff that demoted a co-occurring 2nd-slowest
    # sibling and overstated the ceiling. These same dicts thread through the TOC, the bottom-line
    # (`pole_wfs[0]`), the per-pole loop, and the prompt builders, so the floor is consistent across
    # every render site. Empty when no populations → the helpers fall back to the global test.
    for p in pole_wfs:
        cooccur, gating_n = _pole_cooccurrence(cp, str(p.get("check", "")))
        p["_cooccur"], p["_gating_n"] = cooccur, gating_n
        # Stash the pole's OWN gate/presence counts, so the agent-prompt gate line can state a
        # DEMOTED pole's real (low) gate frequency instead of borrowing the workflow's typical-gate
        # count (the caddy goreleaser-check contradiction). The demotion FLAG itself is stamped
        # below, once `blocker`/`chain` are known (a demoted pole is exactly one the header frames
        # "Rarely the merge gate" — a non-blocker, non-chain, non-typical pole).
        _pnm = str(p.get("check", ""))
        p["_pole_n"] = _pole_n.get(_pnm)
        p["_present"] = present.get(_pnm)
    # The jobs already rendered AS drilled long poles — a finding on one of these must NOT also appear
    # in the off-path appendix as a "minor cleanup" (Class A #5). `_job_base` strips the matrix
    # `(variant)` so a finding's bare job (`pytest-torch`) matches the pole's leg. Computed here,
    # after `pole_wfs` is finalized, so `_also_noticed_block` can exclude those jobs.
    pole_jobs = {(_wf_base(str(p.get("workflow_file") or "")),
                  _job_base(str(p.get("job") or p.get("check") or "")))
                 for p in pole_wfs}
    also_lines, also_count, also_on_path = _also_noticed_block(
        all_findings, catalog_url, shallow_note, pole_jobs, doc=doc,
        spine_rare_names=_spine_rare_names)
    fileless = sorted((p for p in poles if not _is_file_pole(p)),
                      key=lambda p: -_gatef(p))

    # The HEADLINE gate = what most PRs actually wait on. Compare the most-gating
    # workflow against the most-gating fileless check by FREQUENCY, not duration.
    top_wf_freq = wf_gate.get(str(pole_wfs[0].get("workflow_file", "")), 0) if pole_wfs else 0
    top_fl_freq = _gatef(fileless[0]) if fileless else 0
    if npop and pole_wfs and top_wf_freq >= top_fl_freq:
        blocker = pole_wfs[0]
    elif npop and fileless and top_fl_freq > top_wf_freq:
        blocker = fileless[0]
    else:
        blocker = poles[0]  # no frequency data → slowest, as before
    blocker_is_file = _is_file_pole(blocker)
    # `merge_dur` ("X until all checks finish") is the wall-clock FLOOR = max p50 over the
    # concurrent set, NOT the frequency-selected gate's own p50. Computed below, once `src`
    # (the concurrent set) is built, so the headline number is when the SLOWEST check
    # finishes — never an understatement when the gate isn't the slowest.

    # Split checks into the TYPICAL PR's set (present on the majority of sampled
    # PRs) and slow MINORITY checks (e.g. an AI review bot on maintainer PRs only).
    # The visualization shows the typical PR; the minority giants are a footnote so
    # they never read as "what the merge always waits on".
    checks = cp.get("checks") or []

    def _present(name: str) -> int:
        return present.get(name, 0)

    # Same `_typical_check` predicate as the pole ordering (with the min-PR floor + required
    # exemption), so the "opt-in / rare" labels and the typical-PR visualization can't
    # disagree with which pole headlines — especially on a small-sample fallback where a bare
    # `present > npop*0.5` would demote a check the data layer (and `_pole_typical`) kept.
    if npop >= _RARE_PRESENCE_MIN_PR:
        typical = [c for c in checks if _typical_check(str(c.get("name", "")))]
        minority_slow = [c for c in checks
                         if not _typical_check(str(c.get("name", "")))
                         and (_num(c.get("p50_s")) or 0) > (_num(blocker.get("p50_s")) or 0)]
    else:
        typical, minority_slow = list(checks), []
    typical.sort(key=lambda c: -(_num(c.get("p50_s")) or 0.0))
    minority_slow.sort(key=lambda c: -(_num(c.get("p50_s")) or 0.0))
    # Distinguish a rare FILE pole (a label-gated / conditional workflow job — fixable, just
    # not on the typical PR) from a genuinely external/managed check (an AI review bot with
    # no workflow file). Both are minority, but the remedy differs, so they must not be
    # described identically (a rare benchmark is NOT an "external review check").
    _file_check_names = {str(p.get("check", "")) for p in _file_all}

    def _ms_is_file(c: dict[str, Any]) -> bool:
        # A check is file-backed if it carries its own `workflow_file` (emitted per check by
        # collect_runs) OR matches a drilled file pole's name. The `workflow_file` arm is the
        # robust one: a rare file check BELOW the top drilled poles still reads as a workflow
        # job (opt-in/conditional), not mislabeled an "external review check".
        return bool(c.get("workflow_file")) or str(c.get("name", "")) in _file_check_names

    def _ms_freq_demoted(c: dict[str, Any]) -> bool:
        # A `minority_slow` member demoted by pole FREQUENCY while still PRESENT on a majority of
        # sampled PRs — it runs on most PRs but a heavier concurrent check almost always gates
        # ahead of it, so it's rarely the actual slowest. This is a DIFFERENT case than a check
        # that simply ran on a minority (opt-in / path-gated), and the "ran on only N/npop —
        # opt-in / label-gated / minority" prose is self-contradictory for it (the expo/expo
        # class, in the minority_slow render sites). Only meaningful with `pole_n` present; the
        # legacy presence rule only demotes genuine minorities, so this is always False there and
        # the original presence wording (correct for legacy) is kept.
        return _have_pole_n and _present(str(c.get("name", ""))) > npop * _RARE_PRESENCE_FRAC

    # When the spine was REQUIRED-SCOPED — `_scope_spine_to_required` ACTUALLY narrowed it to
    # required-reachable checks (the data layer sets `spine_required_scoped`) — a demoted
    # minority FILE check here isn't an opt-in benchmark, it's a *required* gate that's merely
    # path-conditional (path-filtered: it runs only on the PRs whose paths trigger it, where it
    # IS the merge pole). The "opt-in / throughput, not merge-wait" framing is wrong for it;
    # reframe as required · path-conditional. Key off `spine_required_scoped`, NOT the
    # sampling-level `required_suite_scoped`: that flag is True whenever a required set was
    # satisfiable, even on a partial / anchorless read where the spine was left UNSCOPED and
    # still holds non-required checks — there a demoted check really could be a non-required
    # opt-in, so the throughput framing is correct. (Likewise False on a PR-floor / recency
    # spine.)
    _req_scoped = cp.get("spine_required_scoped") is True

    # The TYPICAL-PR check set: what the level-1 waterfall draws and what "merge wait"
    # (the slowest typical check) is measured over. Typical first, else all checks, else
    # the poles. This is the VISUALIZATION set — it must stay the typical PR's checks.
    if typical:
        src: list[dict[str, Any]] = typical
    elif checks:
        src = sorted(checks, key=lambda c: -(_num(c.get("p50_s")) or 0.0))
    else:  # no per-check list (minimal/static doc) - fall back to the poles
        src = [{"name": p.get("check"), "p50_s": p.get("p50_s")} for p in poles]

    # The FLOOR-candidate pool — DISTINCT from `src` (Class A #6). A pole's addressable ceiling
    # floors against the slowest OTHER check that co-occurs with it on a majority of its OWN gating
    # PRs (the co-occurrence test in `_floor_qualifies`, Class A #4). That floor can be a check that
    # is heavy but runs on a MINORITY of PRs (so it's not in the typical-PR `src`) — e.g. lightdash
    # `E2E: API (Vitest)` (13m, on ~50% of PRs) genuinely caps `Deploy Preview`. Restricting the
    # floor pool to `src` (typical) hid such a check from the floor math: the ceiling overstated AND
    # the heavy check was disclosed nowhere on the spine. So the floor pool is the FULL concurrent
    # set; the co-occurrence filter still gates which qualify, so a rare non-co-occurring check can't
    # inflate the floor. `src` stays the typical-PR chart; the two roles are no longer conflated.
    floor_pool = sorted(checks, key=lambda c: -(_num(c.get("p50_s")) or 0.0)) if checks else src

    gate_check = _clean_label(str(blocker.get("check", "")))
    nwf = len(pole_wfs)
    # The wall-clock FLOOR — when the slowest concurrent check finishes. `src` is sorted
    # p50-desc, so `src[0]` is the slowest; its name is the genuinely-slowest check and
    # `slowest_p50` is that check's OWN conditional p50. The frequency-selected
    # `blocker`/`gate_check` (what MOST PRs gate on) may be a DIFFERENT, faster check — so only
    # call the gate "the slowest" when it actually is; otherwise name the real slowest separately
    # (the langfuse class: a frequent bot headlined as "slowest" while a slower file check set
    # the real floor).
    slowest_p50 = _num(src[0].get("p50_s")) if src else _num(blocker.get("p50_s"))
    floor_name = _clean_label(str(src[0].get("name", ""))) if src else gate_check
    # `merge_dur` ("X until all checks finish") is the floor a TYPICAL (median) PR waits. When
    # the slowest typical check ran on only a FRACTION of sampled PRs, its full conditional p50
    # OVERSTATES that floor — a median PR that doesn't run it finishes sooner — so use the
    # POPULATION-WEIGHTED typical (median of the per-PR critical-path maxima) instead. Only
    # LOWER, and only for a non-universal slowest check: a near-universal slowest check keeps
    # its exact p50 floor (unchanged), as does any repo with no per-PR populations.
    floor_p50 = slowest_p50
    if src and slowest_p50 is not None and npop:
        _spresent = present.get(str(src[0].get("name", "")), 0)
        if 0 < _spresent < npop:
            _pop_floor = _population_typical_floor(cp)
            if _pop_floor is not None and _pop_floor < slowest_p50:
                floor_p50 = _pop_floor
    # PHYSICAL BOUND (issue #24): "X until all checks finish" is a WALL — it can never exceed the
    # MEASURED makespan p50 (the median per-PR max(end)-min(start) over the spine's checks, each
    # span-CAPPED). The population floor above (median of per-PR maxima) is taken over the per-PR
    # CHECK p50s, which carry re-run-inflated check-run clocks the makespan's caps defeat — so a
    # single re-run-bloated gate (nx `main-linux`: a ~46m conditional check p50 over a wider
    # run-sample lowers to a ~15m08 population floor, vs an ~11m measured makespan) can leave the
    # floor ABOVE the actual wall. Cap the floor at the measured makespan so the headline derives
    # from the same span-capped basis chain_summary stamps. It only LOWERS, and only when a
    # makespan was measured; a well-behaved report keeps its floor unchanged (per-PR, the span is
    # >= that PR's max check, so where the pop-floor and the makespan are drawn from the same PR
    # sample pop-floor <= makespan — the two are re-derived from different sources, `populations`
    # vs `chain_facts`, so the cap can nudge a legitimate floor down only in the conservative,
    # toward-the-measured-wall direction).
    _makespan_p50 = _num((cp.get("chain_summary") or {}).get("makespan_p50_s")) or 0.0
    if _makespan_p50 > 0 and floor_p50 is not None and floor_p50 > _makespan_p50:
        floor_p50 = _makespan_p50
    merge_dur = _clock(floor_p50)
    slowest_dur = _clock(slowest_p50)
    # Issue #66 fix 2: stash the typical merge wait on every pole so `_floor_note` can
    # reconcile a per-pole recoverable ceiling that exceeds it (a fix cannot give back more
    # than a TYPICAL PR waits — the excess is the pole's slow-mode/minority figure). Set here,
    # once floor_p50 is final, on the same pole dicts the per-pole loop renders below.
    for _p in pole_wfs:
        _p["_typical_wait_s"] = floor_p50
    floor_lowered = floor_p50 != slowest_p50
    gate_is_slowest = (not src) or floor_name == gate_check
    # ENG-1 PR-N2: when the per-PR chain facts show the typical gate is a
    # `needs:` CHAIN (modal chain has >= 2 members), the slowest-single-check
    # floor UNDERSTATES the wait (the deepgram class: compile -> test is
    # ~1m45s, not test's 1m06s) — the honest typical wait is the chain p50.
    # Everything here is stamped data (`pr_critical_path.chain_summary`,
    # reduced from `chain_facts`); the verifier re-derives it. Chainless /
    # legacy artifacts leave `chain_active` False and render byte-identically.
    _chs = cp.get("chain_summary") or {}
    _chain_modal = [str(m) for m in (_chs.get("modal_chain") or [])]
    _chain_p50 = _num(_chs.get("chain_p50_s")) or 0.0
    chain_active = len(_chain_modal) >= 2 and _chain_p50 > 0.0
    _chain_label = " → ".join(f"`{_lbl(_clean_label(m))}`" for m in _chain_modal)
    # PHYSICAL BOUND (issue #22): the chain sum `chain_p50` is a median of the per-PR SUMMED member
    # spans, taken over ALL sampled PRs — so fast PRs DILUTE it, and it can render a chain "total"
    # BELOW a single member's own p50 (tokio miri-test's 18m36 inside a 17m18 total) or ABOVE the
    # measured wall (re-run-inflated spans). The rendered "until all checks finish" figure has to
    # stay physically coherent: a serial `needs:` chain can't finish faster than its longest single
    # stage (invariant a: total >= the largest modal member's p50), and it can't take longer than the
    # MEASURED makespan wall (invariant b). So CLAMP the sum: cap it at the measured makespan first,
    # then floor it to the largest member (the floor wins if a member exceeds the wall — the member
    # is a hard MEASURED lower bound). A chain whose sum is already within [member, makespan] renders
    # byte-identically. `_chain_total` (not `_chain_p50`) is what the headline claims as the wait.
    _chain_member_p50s = [_num(c.get("p50_s")) or 0.0
                          for c in (cp.get("checks") or [])
                          if str(c.get("name", "")) in set(_chain_modal)]
    _chain_largest_member = max(_chain_member_p50s, default=0.0)
    _chain_total = _chain_p50
    if _makespan_p50 > 0 and _chain_total > _makespan_p50:
        _chain_total = _makespan_p50
    if _chain_largest_member > _chain_total:
        _chain_total = _chain_largest_member
    # Issue #115: does the measured per-PR wall (makespan) materially exceed the chain-sum wait?
    # A serial `needs:` chain finishes when its last stage ends, but the OBSERVED wall also carries
    # the queue gaps BETWEEN stages (a stage waiting for a runner), so the sum of stage spans
    # UNDERSTATES the real wait (withastro/astro: a 16m18s chain sum vs a ~69m04s measured wall,
    # divergence -76%). Beyond the Model-check threshold this arm leads the headline WALL with the
    # observed makespan and demotes the chain sum to attribution. `_chain_total` still clamps the
    # OVER-statement direction (sum > wall), so `_chain_diverges` only fires when the wall is the
    # bigger, honest number. `_wall_dur` is the figure the "typical PR waits / until all checks
    # finish" WALL renders — makespan when diverging, else the clamped chain total (byte-identical
    # on every well-behaved report). Rendered WITHOUT a leading `~` (like every other wait figure)
    # so a cluster-crown line's `**~<win>**` stays the first tilde-clock the verifier parses.
    # `_num()` (not a bare `float()`): a persisted / externally-supplied findings.json can carry a
    # non-numeric `divergence_pct`, and the codebase parses every other `chain_summary` field this
    # way — a malformed value reads as None (non-divergent fallback), never crashes the render.
    _dvg = _num(_chs.get("divergence_pct"))
    _chain_diverges = bool(
        chain_active and _dvg is not None and _makespan_p50 > 0
        and _dvg < -_CHAIN_MAKESPAN_DIVERGENCE_PCT
        and _makespan_p50 > _chain_total + 0.5)
    _wall_dur = _clock(_makespan_p50) if _chain_diverges else _clock(_chain_total)
    if chain_active:
        merge_dur = _clock(_chain_total)
        # Review V2 (OD-F2): stash the chain story on each modal-MEMBER pole so
        # the per-pole floor helpers (`_floor_note` / `_pole_addressable`) can
        # render the chain-stage framing with the win capped at the stamped
        # chain headroom (`chain_win_p50_s` — the same bound the N3 cascade
        # applies to member findings). Same threading pattern as the `_cooccur`
        # stash above; non-members carry no stash and render byte-identically.
        for p in pole_wfs:
            _rc = str(p.get("check", ""))
            if _rc in set(_chain_modal):
                p["_chain_member"] = {
                    "win_s": _num(_chs.get("chain_win_p50_s")) or 0.0,
                    "stage": _chain_modal.index(_rc) + 1,
                    "len": len(_chain_modal),
                    "label": _chain_label,
                    # The physically-coherent chain TOTAL (issue #112) — the SAME `merge_dur` the
                    # headline claims as the wait, so the per-pole agent-prompt gate line frames this
                    # stage against the chain total instead of mis-labelling a mid-chain `needs:`
                    # stage "the slowest check a typical PR waits on" (it is not — the CHAIN is).
                    "merge_dur": merge_dur,
                    # The chain's OTHER members (raw names) so the floor note can exclude serial
                    # `needs:` partners when it names the slowest CONCURRENT floor (Class A #6): a
                    # partner runs before/after this stage, not alongside it, so it must never be
                    # mis-framed as a parallel cap.
                    "members": list(_chain_modal),
                }
    withheld_poles = [p for p in pole_wfs if p.get("job_timing_unavailable")]
    all_poles_withheld = bool(pole_wfs) and len(withheld_poles) == len(pole_wfs)
    if chain_active:
        # Claims layer (headline family, chain form — ENG-1 PR-N2). The gate is a
        # `needs:` chain, so the parallel framing of every other form would be
        # false here. `subject` is the joined modal-chain member names — the
        # tuple `verify_report.check_headline_chain_matches_stamp` re-derives
        # from `chain_facts`.
        lead = cs.add(claims.Claim(
            kind="headline_chain", subject=" → ".join(_chain_modal),
            fields={"merge_dur": merge_dur, "chain_p50_s": _chain_p50,
                    # `chain_wait_p50_s` is the physically-coherent rendered wait: the sum
                    # `chain_p50_s` clamped into [largest member p50, measured makespan] (issue #22).
                    # `merge_dur` renders from THIS, not the raw sum.
                    "chain_wait_p50_s": round(_chain_total, 3),
                    "makespan_p50_s": round(_makespan_p50, 3) if _makespan_p50 else None,
                    "largest_member_p50_s": round(_chain_largest_member, 3),
                    "modal_n": int(_chs.get("modal_n") or 0),
                    "n": int(_chs.get("n") or 0)},
            rendered=_strip_emdashes(
                # Issue #115 divergent arm: the WALL (makespan) leads; the chain sum is demoted
                # to serial-work attribution. `merge_dur` (the chain sum) still appears, and stays
                # the claim's `merge_dur` FIELD, so the #22/#24 physical bounds re-derive unchanged.
                (f"**{_wall_dur} until all checks finish** — the {_chain_label} "
                 f"`needs:` chain sums to {merge_dur} of serial work, but the observed "
                 "per-PR wall is longer: queue gaps between the stages stretch it past the "
                 f"sum (the longest path on {int(_chs.get('modal_n') or 0)}/"
                 f"{int(_chs.get('n') or 0)} sampled PRs). Budget on the "
                 f"{_wall_dur} wall; the chain sum is for attribution. ")
                if _chain_diverges else
                (f"**{merge_dur} until all checks finish** — the gate is the "
                 f"{_chain_label} chain: `needs:` runs these checks one after "
                 f"another, so their times add up (the longest path on "
                 f"{int(_chs.get('modal_n') or 0)}/{int(_chs.get('n') or 0)} "
                 "sampled PRs). "))))
    elif not npop:
        # Framing-family exclusion (plan 007): a generic mechanics sentence ("waits on the
        # slowest concurrent check") that names no specific check — there is no check-name
        # subject to stamp/verify, and no FRAMING_VOCABULARY template matches its word order.
        # Deliberately NOT a claim.
        lead = (f"**{merge_dur} until all checks finish.** A PR waits on the slowest "
                "concurrent check. ")
    elif gate_is_slowest and not floor_lowered:
        # Claims layer (headline family, form 1: gate-is-slowest, name-first). `subject`
        # is the check named in the slowest slot (`gate_check`, which here equals
        # `floor_name`) — the same field `verify_report.check_headline_slowest_matches_stamp`
        # compares against the data layer's stamped `critical_path_check`.
        lead = cs.add(claims.Claim(
            kind="headline_slowest", subject=gate_check,
            fields={"merge_dur": merge_dur, "slowest_dur": slowest_dur,
                    "gate_check": gate_check, "floor_lowered": floor_lowered},
            rendered=_strip_emdashes(
                f"**{merge_dur} until all checks finish** — `{gate_check}` is the "
                "slowest check a typical PR waits on. ")))
    elif gate_is_slowest:
        # The slowest typical check is non-universal, so its full conditional time isn't the
        # universal floor. WHY the population-weighted typical wait is LOWER than that check's
        # own conditional p50 depends on how OFTEN the check runs — and the two causes are not
        # interchangeable:
        #  - MINORITY presence (present <= npop*_RARE_PRESENCE_FRAC): a typical (median) PR
        #    genuinely SKIPS the check, so its ABSENCE on most PRs is what pulls the median
        #    wait below its conditional time. The "ran on only N/npop sampled PRs, so a typical
        #    PR finishes in {merge_dur}" presence-causal framing is faithful here.
        #  - MAJORITY presence (near-universal, e.g. nx `main-linux` at 19/20): a typical PR
        #    RUNS the check, so presence CANNOT lower the median — 95% presence does not drop a
        #    46m wait to 11m. The gap is a DURATION / population skew: the conditional p50
        #    (measured over a wider run-sample) overstates what a median PR waits, and the
        #    population-weighted median of the per-PR maxima ({merge_dur}) is the honest floor.
        #    Naming presence as the cause there is a non-sequitur the populations contradict, so
        #    frame the drop as conditional-p50-overstatement instead. (verify_report's
        #    `check_headline_presence_causal_only_when_minority` re-derives present/npop from
        #    `populations` and FAILS a presence-causal headline whose check is majority-present.)
        # Claims layer (headline family, form 1: gate-is-slowest, non-universal disclosure).
        # Both framing literals stay INSIDE this `Claim(...)` statement (the ternary on `rendered`)
        # so the source-lint `test_framing_source_lint_every_family_literal_is_a_claim` still sees
        # every "slowest check ... waits on" literal wrapped in a Claim.
        _spres = present.get(str(src[0].get("name", "")), 0)
        _pres_minority = bool(npop) and _spres <= npop * _RARE_PRESENCE_FRAC
        lead = cs.add(claims.Claim(
            kind="headline_slowest", subject=gate_check,
            fields={"merge_dur": merge_dur, "slowest_dur": slowest_dur,
                    "gate_check": gate_check, "floor_lowered": floor_lowered,
                    "present": _spres, "npop": npop},
            rendered=(
                # MINORITY presence: a typical PR SKIPS the check, so its absence is what lowers the
                # median wait — the presence-causal "ran on only N/npop, so" clause is faithful.
                _strip_emdashes(
                    f"**{merge_dur} until all checks finish** — `{gate_check}` is the slowest "
                    f"check a typical PR waits on (~{slowest_dur}), but it ran on only "
                    f"{_spres}/{npop} sampled PRs, so a typical PR finishes in {merge_dur}. ")
                if _pres_minority else
                # MAJORITY presence: a typical PR RUNS the check, so presence CANNOT lower the
                # median. The drop is a duration/population skew — the conditional p50 overstates
                # the typical wait; state the measured population-weighted median instead (naming
                # presence as the cause here is the nx `main-linux` non-sequitur).
                _strip_emdashes(
                    f"**{merge_dur} until all checks finish** — `{gate_check}` is the slowest "
                    f"check a typical PR waits on, but its ~{slowest_dur} is a conditional p50 that "
                    f"overstates the typical wait; across sampled PRs the median PR's slowest check "
                    f"finishes in {merge_dur}. "))))
    elif floor_lowered:
        # Form 2, floor-LOWERED: the slowest TYPICAL check (`floor_name`) is BOTH not the frequency
        # gate AND non-universal, so its conditional `slowest_dur` OVERSTATES the typical wait — name
        # both checks AND reconcile presence exactly as form 1 (the `gate_is_slowest` floor-lowered
        # branch) does. Without the reconciliation the sentence labels a check "a typical PR waits on"
        # (~slowest_dur) beside a strictly-lower `merge_dur` all-checks-finish floor with no caveat —
        # the tauri `test (windows-latest)` shape (5/20 path-filtered, ~28m56s, headlined against a
        # ~7m16s floor while its identical matrix siblings carry the opt-in footnote). Claims layer
        # (headline family, form 2, name-after); `subject` is `floor_name` (= `src[0].name` =
        # `critical_path_check`). `verify_report.check_headline_floor_presence_reconciled` re-derives
        # the lowering from `populations` and REQUIRES this presence clause.
        lead = cs.add(claims.Claim(
            kind="headline_slowest", subject=floor_name,
            fields={"merge_dur": merge_dur, "slowest_dur": slowest_dur,
                    "floor_name": floor_name, "gate_check": gate_check,
                    "present": present.get(str(src[0].get("name", "")), 0), "npop": npop},
            rendered=_strip_emdashes(
                f"**{merge_dur} until all checks finish** — the slowest check a typical PR "
                f"waits on is `{floor_name}` (~{slowest_dur}), but it ran on only "
                f"{present.get(str(src[0].get('name', '')), 0)}/{npop} sampled PRs, so a typical "
                f"PR finishes in {merge_dur}; `{gate_check}` is the check most PRs gate on "
                "(drilled below). ")))
    else:
        # The most-gating check isn't the slowest — say both, truthfully, so the floor
        # number and the "slowest" label can't contradict the Level-1 chart. `slowest_dur`
        # is the slowest check's OWN time (equals `merge_dur` here because the floor was NOT
        # lowered — the slowest check is universal, OR the population floor isn't strictly below
        # its p50, OR there are too few per-PR populations (< _RARE_PRESENCE_MIN_PR) to derive one;
        # the floor-lowered case is the branch above).
        # Claims layer (headline family, form 2: floor != frequency-gate, name-after). `subject`
        # is `floor_name` (= `src[0].name`) — the slowest TYPICAL check the data layer stamps as
        # `critical_path_check`, NOT `gate_check` (the frequency gate, drilled below instead).
        lead = cs.add(claims.Claim(
            kind="headline_slowest", subject=floor_name,
            fields={"merge_dur": merge_dur, "slowest_dur": slowest_dur,
                    "floor_name": floor_name, "gate_check": gate_check},
            rendered=_strip_emdashes(
                f"**{merge_dur} until all checks finish** — the slowest check a typical PR "
                f"waits on is `{floor_name}` (~{slowest_dur}); `{gate_check}` is the check "
                "most PRs gate on (drilled below). ")))
    if minority_slow:
        ms = minority_slow[0]
        ms_name = str(ms.get("name", ""))
        ms_slow = _clock(_num(ms.get("p50_s")))
        if _ms_is_file(ms):
            _seebelow = (" See its long pole below."
                         if any(str(p.get("check", "")) == ms_name for p in pole_wfs) else "")
            if _ms_freq_demoted(ms):
                # Present on a majority but rarely the actual slowest — state the frequency
                # reason, not a presence/opt-in one (which would contradict its own presence).
                # Claims layer (minority_slow_note): the frequency-demoted minority-slow
                # fragment. `subject` is the minority check `ms_name`. This fragment is
                # concatenated onto a line that already carries a headline claim, so the
                # coverage check binds by phrase containment, not line equality. Em-dash
                # source -> strip at construction (idempotent report-wide strip keeps it
                # byte-identical).
                lead += cs.add(claims.Claim(
                    kind="minority_slow_note", subject=ms_name,
                    fields={"ms_slow": ms_slow, "present": present.get(ms_name, 0), "npop": npop},
                    rendered=_strip_emdashes(
                        f"(`{_clean_label(ms_name)}` is slower (~{ms_slow}) and runs on "
                        f"{present.get(ms_name, 0)}/{npop} sampled PRs, but it's rarely the "
                        "actual slowest check a PR waits on — a heavier concurrent check gates "
                        "ahead of it — so its time is throughput/cost, not merge-wait; unless "
                        f"it's a *required* status check it isn't the gate here.{_seebelow}) ")))
            elif _req_scoped:
                lead += (f"(`{_clean_label(ms_name)}` is slower (~{ms_slow}) and is a "
                         f"*required* gate, but it ran on only {present.get(ms_name, 0)}/{npop} "
                         "sampled PRs — it's path-conditional (path-filtered), so a typical PR "
                         "doesn't wait on it, though it IS the merge pole on the PRs that do "
                         f"run it.{_seebelow}) ")
            else:
                lead += (f"(`{_clean_label(ms_name)}` is slower (~{ms_slow}) but it ran on only "
                         f"{present.get(ms_name, 0)}/{npop} sampled PRs — it looks opt-in / "
                         "conditional (e.g. label-gated), so a typical PR doesn't wait on it and "
                         "its time is throughput/cost, not merge-wait; unless it's a *required* "
                         f"status check it isn't the gate here.{_seebelow}) ")
        else:
            lead += (f"(`{_clean_label(ms_name)}` is slower (~{ms_slow}) but it's an external "
                     "review check — not something a workflow change speeds up — and didn't "
                     "run on the typical sampled PR; unless it's a *required* status check it "
                     "doesn't hold up the merge, so it isn't treated as the gate here; see the "
                     "note below.) ")
    # Owner UX edit (2026-07-19): the standalone headline paragraph is GONE — its
    # claim sentence(s) (`lead`, built above: the headline claim + any minority-slow
    # fragments, all registered claims) FOLD into the Bottom-line blockquote below as
    # its continuation; the ORIENTATION TAIL that used to close this paragraph ("the N
    # heaviest checks run in parallel … an ASCII drill-down … a prompt to hand your
    # coding agent … does not prescribe the fix", plus the fileless / withheld variants)
    # is DROPPED. The substantive special-case info it carried (no workflow to drill;
    # step timing withheld) is already stated in the Bottom-line branches themselves, so
    # nothing load-bearing is lost.
    # The PR-floor fallback isn't the merge gate, so the title asks the honest
    # question for that spine ("why is CI slow on a PR?") instead of "why is the merge
    # slow?" — the banner just below spells out why.
    is_pr_floor = cp.get("gate_kind") == "pr_floor_fallback"
    title = (f"# {repo} — why is CI slow on a PR?" if is_pr_floor
             else f"# {repo} — why is the merge slow?")
    out: list[str] = [title, ""]
    out += _metadata_table(doc, repo)

    # Incomplete coverage is a "read me first" warning - an unscanned file must never
    # read as clean - so it sits above everything. The dropped-finding NOTE is lower-
    # stakes transparency (a flagged-then-gated finding) and renders as bottom matter.
    out += _coverage_gap_banner(doc.get("scan_incomplete"))
    # The PR-floor demotion banner sits right under the coverage warning: it reframes
    # everything below as the PR-floor, so it must be read before the bottom line.
    out += _pr_floor_fallback_banner(doc, cp)

    # Executive summary - the one number a reader wants (total merge wait) and the
    # single biggest measured win, scannable above the framing paragraph. The win is
    # the addressable wall-clock on the slowest FIXABLE pole (pole_wfs[0]); when
    # nothing is fixable or floored, the bottom line is just the wait.
    top_fixable = pole_wfs[0] if pole_wfs else None
    win = _pole_addressable(top_fixable, floor_pool) if top_fixable else 0.0
    # ENG-1 PR-N2: chain-aware executive summary. The win is chain arithmetic —
    # the chain's headroom down to the next-longest COMPETING path
    # (`runner_up_p50_s`); shortening any chain member comes 1:1 off the wait
    # until that bound. Verifier-re-derived from `chain_facts`.
    _chain_win = (_num(_chs.get("chain_win_p50_s")) or 0.0) if chain_active else 0.0
    # #49 — RENDER-LAYER SINGLE DOOR. The headline lever consumes the STAMPED per-finding
    # cluster ceilings collect_runs sized, never a re-computed sibling-capped headroom. A
    # credited cluster-floor lever (OPT73) saves its full stamped ceiling (concurrent legs
    # drop in lockstep; sequential `needs:`-chained stages compound) — a win neither the
    # chain-headroom nor `_pole_addressable` arithmetic below can reach (both cap at the
    # next SIBLING leg). When such a stamped ceiling beats the win the branches below would
    # headline, the bottom line LEADS with it instead of burying it in "Also noticed"
    # (mastodon: 627s cluster win vs a 36s per-leg headline; electron: 2635s vs 5m37s).
    # Non-cluster repos and legacy artifacts (no stamped flag) resolve `_cluster_s == 0`,
    # so the branch never fires and the Bottom-line block renders byte-identically.
    _existing_win = (_chain_win if (chain_active and _chain_win > 0)
                     else (win if (top_fixable is not None and win > 0) else 0.0))
    _cluster_s, _cluster_f = _headline_cluster_lever(all_findings)
    if _cluster_f is not None and _cluster_s > _existing_win + 0.5:
        # Issue #115: the cluster crown's wait figure is the WALL (`_wall_dur` = makespan when
        # the chain diverges, else the clamped chain total) — the cluster win itself is unchanged.
        out += _cluster_headline_bottom_line(_wall_dur, _cluster_s, _cluster_f)
    elif _chain_diverges:
        # Issue #115: makespan materially exceeds the chain sum — the chain sum UNDERSTATES the
        # wait, so lead with the observed wall and demote the sum to attribution (the honest arm
        # the biome slowest-check lead already takes, unified onto the chained-gate shape). No
        # "waits **X** for the … chain" phrasing here, so the chain cell-7 re-derivation binds only
        # the non-divergent branch below (where the rendered wait IS the chain total).
        _cw = (f" Fixing the whole chain is worth up to **~{_clock(_chain_win)}** of that wait "
               "(median of the per-PR headroom; see the drill-downs below)."
               if _chain_win > 0 else "")
        out += [f"> **Bottom line.** A typical PR waits **{_wall_dur}** — the observed per-PR "
                f"wall (p50). The {_chain_label} `needs:` chain sums to only {merge_dur} of "
                "serial work, but queue gaps between its stages stretch the real wait well past "
                f"the sum, so budget on the {_wall_dur} wall and use the chain for attribution."
                + _cw, ""]
    elif chain_active and _chain_win > 0:
        # The competing-path clause is honest only when a competing path
        # exists; with runner_up 0 the chain is the only measured path.
        _bound = ("before the next-longest competing path gates instead "
                  if (_num(_chs.get("runner_up_p50_s")) or 0.0) > 0.0
                  else "— it is the only measured path on these PRs ")
        out += [f"> **Bottom line.** A typical PR waits **{merge_dur}** for the "
                f"{_chain_label} chain to finish — the members run in sequence, so "
                "fixing the whole chain is worth up to "
                f"**~{_clock(_chain_win)}** of merge wait {_bound}"
                "(median of the per-PR headroom; per-member headroom varies — "
                "see the drill-downs below).", ""]
    elif chain_active:
        out += [f"> **Bottom line.** A typical PR waits **{merge_dur}** for the "
                f"{_chain_label} chain to finish; a competing path of comparable "
                "length gates the merge just behind it, so shortening the chain "
                "alone buys little — the drill-downs below cover both paths.", ""]
    elif top_fixable is not None and win > 0:
        win_check = _lbl(_clean_label(str(top_fixable.get("check", ""))))
        # Issue #66 fix 2: a headline win above the typical wait co-renders the
        # slow-mode/minority reconciliation (else "~6m28s recoverable" beside a "~2m46s"
        # typical wait reads as saving more than the whole wait).
        out += [f"> **Bottom line.** A typical PR waits **{merge_dur}** for all checks "
                f"to finish. The biggest single measured win is **~{_clock(win)}** off "
                f"the slowest fixable check, `{win_check}` — see [Long pole 1]"
                "(#pole-1) for the drill-down to the biggest lever."
                + _recoverable_reconciliation(win, floor_p50), ""]
    elif nwf == 0:
        # All-managed/external gate: there are no file-backed poles, so the report
        # renders ZERO "## Long pole" drill-downs. Don't point the reader at drill-downs
        # that don't exist — say plainly there's no workflow to drill (mirrors the lead).
        out += [f"> **Bottom line.** A typical PR waits **{merge_dur}** for all checks "
                "to finish, but every gating check is a managed/external check with no "
                "editable workflow file — there is no workflow to drill or diff.", ""]
    elif all_poles_withheld:
        out += [f"> **Bottom line.** A typical PR waits **{merge_dur}** for all checks "
                "to finish; the pole sections below keep the PR check-run gates visible "
                "and explain why step timing is withheld until a developer-event job "
                "sample exists.", ""]
    else:
        out += [f"> **Bottom line.** A typical PR waits **{merge_dur}** for all checks "
                "to finish; the per-pole drill-downs below trace where that time goes.",
                ""]
    # Owner UX edit (2026-07-19): TOP MATTER = metadata table → ONE Bottom-line blockquote
    # → Contents, nothing else. Everything that used to render as its own blockquote between
    # the headline and the Contents FOLDS into the single Bottom-line quote (via
    # `_fold_bottom_line`), so nothing renders between the block and `## 📋 Contents`:
    #   • the folded headline claim (`lead`) — the registered framing text, kept byte-exact
    #     (its trailing space included) so the claims manifest's `report.count(rendered)==1`
    #     and verify_report's coverage/headline parsers (position-independent) stay green;
    #   • the config-era caveat — it QUALIFIES the headline number, so it belongs IN the
    #     bottom line; the era guards are text-anchored (marker-in-report), not position-
    #     anchored, so folding keeps them green (proven by a straddle fixture render→verify);
    #   • the fileless/managed disclosure and the chain model-check (also text-anchored);
    #   • the "After the gate" runner-minute line (`_tier2_bottom_line`).
    # The bill-scope methodology note was MOVED down into 🗄️ Data sources (see
    # `_bill_scope_era_note`) — it sizes total compute, not the headline.
    _lead_block = ["> " + lead] if lead.strip() else []
    _model_check = []
    # Reuse the already-`_num`-parsed `_dvg` (never a bare `float()` on the raw field: a persisted /
    # externally-supplied summary can carry a non-numeric `divergence_pct` that would crash the render).
    if chain_active and _dvg is not None and abs(_dvg) > 25.0:
        _model_check = ["> *Model check:* the chain-sum wait above differs from the observed "
                        "per-PR wall of the gating checks (p50 "
                        f"~{_clock(_num(_chs.get('makespan_p50_s')))}) by "
                        f"{_dvg:+.0f}% — queue gaps between chain "
                        "stages push the wall above the sum; re-run-inflated member spans "
                        "push the sum above the wall. Budget on the observed wall; use the "
                        "chain for attribution."]
    _fold_bottom_line(
        out,
        _lead_block,
        _model_check,
        # Config-era caveat: qualifies the headline number (folded, not a separate quote).
        _config_era_disclosure_lines(cp, captured_at),
        # Fileless/managed status-check disclosure (PR-lifetime status-gating latency).
        _fileless_disclosure_lines(cp),
        # "After the gate" runner-minute line.
        _tier2_bottom_line(all_findings, cs, doc),
    )

    # (headline claim `lead` and every top-matter disclosure were folded into the single
    # Bottom-line blockquote above — owner UX edit 2026-07-19; nothing renders between it
    # and the Contents.)
    queue_count = sum(1 for f in all_findings if _is_wait_finding(f))
    _t2_ranked = _tier2_source_backed_ranked(all_findings, doc)
    tier2_count = len(_t2_ranked)
    tier2_toc = None
    if _t2_ranked:
        _t2_total = sum(_num(f.get("runner_min_saving")) or 0.0 for f in _t2_ranked)
        _t2_rows = []
        for _i, _f in enumerate(_t2_ranked[:_TIER2_CAP], 1):
            # Short label (OD12-rev1): the full catalog title made the row
            # unglanceable - the savings drowned at the end of a 2-line title.
            _rmin = _num(_f.get("runner_min_saving")) or 0.0
            _t2_rows.append((_i, _tier2_short_title(_f), _fmt_tier2_saved_min(_rmin)))
        _t2_overflow = max(tier2_count - _TIER2_CAP, 0)
        tier2_toc = {
            "total_min": _fmt_tier2_saved_min(_t2_total),
            "rows": _t2_rows,
            "overflow": _t2_overflow,
        }
    # Stamp `_demoted_gate_framing` = "the header framed this pole 'Rarely the merge gate' /
    # 'Opt-in / rare' / 'Required · path-conditional'" — EXACTLY the per-pole header's demotion
    # branch (npop present, NOT typical, NOT the blocker, NOT a chain member). The TOC row and the
    # agent-prompt gate line read this stamp so a demoted pole isn't handed the typical-gate framing
    # its own header disowns (caddy goreleaser-check). Stamped HERE, after `blocker`/`chain` exist
    # and before both the TOC and the per-pole loop, so all three render sites agree.
    _chain_set = set(_chain_modal) if chain_active else set()
    for p in pole_wfs:
        p["_demoted_gate_framing"] = bool(
            npop and not _pole_typical(p) and p is not blocker
            and str(p.get("check", "")) not in _chain_set)
    # `top_is_gate` carries the removed Level-1 chart's ◀-is-the-gate condition to the
    # Contents' first row: the frequency gate is the slowest single check AND the gate is
    # not a `needs:` chain (a chain's slowest single check is not the gate).
    out += _toc_block(pole_wfs, wf_gate, npop, also_count, pr_floor=is_pr_floor,
                      queue_count=queue_count, also_on_path=also_on_path,
                      runner_spine_count=runner_spine_count,
                      tier2_toc=tier2_toc,
                      top_is_gate=(gate_is_slowest and not chain_active))
    # The prose provenance ("Where this data comes from") is consolidated into the
    # 🗄️ Data sources section at the foot (owner UX edit 2026-07-19) — no longer emitted
    # here after the Contents.

    # Identity-resolved per-pole log binding (R1): which single KEY each rendered pole owns,
    # so a pole never inherits a workflow-sibling's drill. Bound via the drill bundle when
    # present, else the uniqueness-guarded sole-owner rule over the supplied keys. Hoisted
    # ABOVE the Long pole map (owner UX edit 2026-07-19) so the map's Level-3 drill of the
    # descent pole and the per-pole loop below share ONE `_derive_pole_leaf` result per pole.
    _supplied_keys = (set(logs) | set(samples) | set(log_runs) | set(steps)
                      | set(mags) | set(analyses))
    pole_owner_keys = _pole_owner_keys(doc, poles, _supplied_keys)
    # (log_text, leaf, offcat_leaf) for every rendered pole, derived ONCE. The map reads the
    # descent pole's entry; the loop reads its own pole's — the SAME object, so the map's
    # Level 3 and the pole's Level 3 can never disagree.
    _pole_leaves: dict[int, tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]] = {
        id(p): _derive_pole_leaf(p, pole_owner_keys.get(id(p)), logs) for p in pole_wfs}

    # ── Long pole map (owner UX edit 2026-07-19) ─────────────────────────────────────────
    # The FULL blocker cascade, RESTORED up top (PR #73 flattened it to level 1; the trimmed
    # levels no longer taught the cascade). Checks-first (the physically true model — GitHub
    # gates on CHECKS, which all race concurrently): level 1 is the flat race of merge-gating
    # checks, each bar labelled `{check} · {workflow file}`; the ◀┐ marks the check the drill
    # descends into (pole 1 = pole_wfs[0]); level 2 breaks that check into its stamped STEPS
    # (share of the check's own p50); level 3 opens the dominant step's internals WHEN the
    # drill actually has them. Degenerate levels collapse (never a one-bar level); numbering
    # stays semantic (checks=1, steps=2, internals=3). PRESENTATION-ONLY: no claim / verify
    # check binds to it — the Contents list + the stamped fields stay the single source of
    # truth, and `src` (level 1) is the SAME typical-PR set the Contents draws. Skipped where
    # today's `if src:` guard skips it.
    if src:
        # Level-1 lead (in-fence); chain repos keep the #75 chain-variant wording.
        if chain_active:
            _l1_lead = (f"Level 1 — checks racing on every PR — except {_chain_label}, which "
                        "run in sequence (`needs:`), so their times ADD on the gate path; the "
                        "merge waits for the slowest:")
        else:
            _l1_lead = "Level 1 — checks racing on every PR; the merge waits for the slowest:"

        def _l1_label(c: dict[str, Any]) -> str:
            # `{clean check} · {workflow file}` — the file comes from the check's own
            # `workflow_file` (emitted per check by collect_runs), else a same-named pole's;
            # a fileless / external check renders with no suffix.
            nm = _clean_label(str(c.get("name", "")))
            wf = str(c.get("workflow_file") or "")
            if not wf:
                for _p in pole_wfs:
                    if (_clean_label(str(_p.get("check", ""))) == nm
                            and _p.get("workflow_file")):
                        wf = str(_p.get("workflow_file"))
                        break
            base = _wf_base(wf) if wf else ""
            return f"{nm} · {base}" if base else nm

        _l1_src = src[:9]
        # Minority-presence marking (Class-A honesty). A rendered level-1 row can be "typical"
        # by pole FREQUENCY (it was the actual per-PR gate on >= _POLE_RECUR_FLOOR PRs, or a
        # required gate) yet have run on only a MINORITY of sampled PRs — a path-conditional job
        # kept in `src` because it gates the PRs it DOES run on. Under the "every PR" lead a reader
        # would misread its conditional time as the normal blocker (the playwright `Windows
        # (firefox)`, 2/20, defect). Mark such rows' DISPLAY label with a trailing ` †`, reframe
        # the lead, and add a legend with each row's real fraction. PRESENTATION-ONLY: the ` †` is
        # appended to the rendered label here, never to `present`/pole keys or any match key
        # (`_l1_names` below stays unmarked, so the descent-pole lookup is untouched). The minority
        # test mirrors the presence filter's threshold (`present <= npop*_RARE_PRESENCE_FRAC`) and
        # its small-sample guard (`npop >= _RARE_PRESENCE_MIN_PR`): below the floor presence is
        # noise, nothing is marked, and small-sample repos render byte-identically to today.
        def _row_minority(c: dict[str, Any]) -> bool:
            nm = str(c.get("name", ""))
            return (npop >= _RARE_PRESENCE_MIN_PR
                    and 0 < present.get(nm, 0) <= npop * _RARE_PRESENCE_FRAC)
        _l1_min = [_row_minority(c) for c in _l1_src]

        def _mark_minority(lbl: str) -> str:
            # Append ` †` to the DISPLAY label, reserving room so the marker survives `_lbl`'s
            # truncation to `_LBLW` — the field still pads to exactly `_LBLW`, so the bar and
            # connector columns stay aligned (a bare append to a >_LBLW label would be truncated
            # straight back off). No-op-safe: `†` passes `_clean_label`/`_fence_safe` unchanged.
            clean = _clean_label(lbl)
            if len(clean) + 2 > _LBLW:
                clean = clean[: _LBLW - 3] + "…"
            return clean + " †"

        _rows1 = [((_mark_minority(_l1_label(c)) if m else _l1_label(c)),
                   _num(c.get("p50_s")), None)
                  for c, m in zip(_l1_src, _l1_min)]
        _l1_names = [_clean_label(str(c.get("name", ""))) for c in _l1_src]

        # When any drawn level-1 row is a minority, reframe the "every PR" lead to "a typical PR"
        # and disclose the † convention inline (the legend below names each marked row's fraction).
        # Only USED when level 1 is actually drawn (below); harmless to compute otherwise.
        if any(_l1_min):
            _min_clause = (" — rows marked † ran on a minority of sampled PRs (path-conditional "
                           "— they gate only the PRs that trigger them)")
            if chain_active:
                _l1_lead = (f"Level 1 — checks racing on a typical PR — except {_chain_label}, "
                            "which run in sequence (`needs:`), so their times ADD on the gate "
                            f"path; the merge waits for the slowest{_min_clause}:")
            else:
                _l1_lead = ("Level 1 — checks racing on a typical PR; the merge waits for the "
                            f"slowest{_min_clause}:")

        def _minority_legend() -> str:
            # ONE prose line (OUTSIDE the ```text fence) naming each †-marked level-1 row with its
            # real sampled-PR fraction, derived from `present`/`npop`. Empty when no row is marked.
            parts = [f"`{_clean_label(str(c.get('name', '')))}` ran on "
                     f"{present.get(str(c.get('name', '')), 0)}/{npop}"
                     for c, m in zip(_l1_src, _l1_min) if m]
            return ("† " + "; ".join(parts) + " sampled PRs.") if parts else ""

        # The descent pole = pole 1 (the check the drill opens). Its stamped steps are level 2;
        # its shared leaf drives level 3. NB `src` (the checks) and `pole["steps"]` (the steps)
        # are DISTINCT axes — the map descends from a check into its steps, never re-ranking
        # checks as steps.
        _descent = pole_wfs[0] if pole_wfs else None
        _dcheck = _clean_label(str(_descent.get("check", ""))) if _descent else ""
        _d_idx = _l1_names.index(_dcheck) if _dcheck in _l1_names else None

        # Level 2 = the descent pole's STAMPED steps (p50-desc), top 6, NO roll-up row (the map
        # is orientation; the pole section keeps the full accounting). % is share of the CHECK's
        # own p50 (the job wall on level 1). ◀ lands on the dominant-category lead (agreeing with
        # the crown), falling back to row 0.
        _l2_steps = sorted((_descent.get("steps") or []) if _descent else [],
                           key=lambda s: -(_num(s.get("p50_s")) or 0.0))[:6]
        _l2_rows = [(str(s.get("step", "")), _num(s.get("p50_s")), None) for s in _l2_steps]
        _check_p50 = (_num(_descent.get("p50_s")) if _descent else None) or 0.0
        _l2_mark = _dom_lead_idx(_l2_steps, str(_descent.get("dominant_category", ""))
                                 if _descent else "")
        _l2_ok = _descent is not None and len(_l2_rows) >= 2

        # Level 3 = the dominant step's internals — ONLY when the descent pole's shared leaf has
        # a `deeper` first level of >= 2 rows (else the level would be degenerate / absent). We
        # render deeper[0] ONLY (the map is orientation; the pole section renders the full stack).
        _l3 = None
        if _descent is not None:
            _dleaf = _pole_leaves.get(id(_descent), (None, None, None))[1]
            _deeper = _dleaf.get("deeper") if _dleaf else None
            if _deeper and len(_deeper[0].get("rows") or []) >= 2:
                _d0 = _deeper[0]
                _dom = str(_descent.get("dominant_step")
                           or (_l2_steps[0].get("step", "step") if _l2_steps else "step"))
                _l3_scale = _d0.get("scale_to_secs")
                if _l3_scale is None and _d0.get("scale_to_step"):
                    _l3_scale = _num(_descent.get("dominant_p50_s")) or 0.0
                _l3 = {
                    "header": f"Level 3 — inside `{_lbl(_dom)}`: {_dleaf['unit_label']}",
                    "rows": _d0["rows"],
                    # blocker_note ONLY when deeper is a single level (the cache-status shape);
                    # a multi-level deeper's note belongs to its LAST level, which the map omits.
                    "note": _d0.get("blocker_note", "") if len(_deeper) == 1 else "",
                    "pct_of": _d0.get("pct_of", "") or "",
                    "scale_to": _l3_scale,
                }

        # Cascade when the descent pole has a >=2-row level 2 AND either its check is a shown
        # level-1 row or level 1 itself is too thin to draw (start the fence at level 2 then).
        _cascade = _l2_ok and (_d_idx is not None or len(_rows1) < 2)
        # Hierarchy glossary, rendered ONCE at the report's first structural use of
        # the workflow/job/step vocabulary (the Long pole map is the first place all
        # three collide). Presentation-only; no claim binds it.
        _map: list[str] = ['<a id="long-pole-map"></a>', "", "## 🗺️ Long pole map", "",
                           _HIERARCHY_GLOSSARY, "",
                           "```text"]
        if _cascade:
            _l2_lead = f"Level 2 — inside {_dcheck}, steps run one after another:"
            _fence: list[str] = []
            if len(_rows1) >= 2 and _d_idx is not None:
                _fence += [_l1_lead, ""]
                _emit_level(_fence, _rows1, header_below=_l2_lead, mark=True, mark_idx=_d_idx)
            else:
                # Level 1 too thin to draw a race — open the fence at level 2.
                _fence += [_l2_lead, ""]
            _emit_level(_fence, _l2_rows,
                        header_below=(_l3["header"] if _l3 else None),
                        mark=True, mark_idx=_l2_mark, pct_denom=_check_p50)
            if _l3:
                _emit_level(_fence, _l3["rows"], header_below=None, mark=True,
                            blocker_note=_l3["note"], pct_of=_l3["pct_of"],
                            scale_to=_l3["scale_to"])
            # The minority legend is emitted only when level 1 was actually drawn (its rows carry
            # the † markers); a level-2-opened fence has no marked rows, so no legend.
            _l1_drawn = len(_rows1) >= 2 and _d_idx is not None
            _lg = _minority_legend() if _l1_drawn else ""
            _map += [*_fence, "```", "",
                     "Each ◀ marks the blocker the next level opens. Long pole 1 below drills "
                     "the marked step to its root cause and hand-off prompt."]
            _map += (["", _lg] if _lg else []) + [""]
            out += _map
        elif len(_rows1) >= 2:
            # Level-1-only fallback (data-poor: no descent pole, its check off the chart, or no
            # usable steps) — renders exactly as #75 did, keeping the original closing line.
            _emit_level(_map, _rows1, header_below=None, mark=True)
            _lg = _minority_legend()
            _map += ["```", "",
                     "Inside the gate, its **steps** run in sequence — each **Long pole** "
                     "finding below drills one check's steps down to the addressable lever."]
            _map += (["", _lg] if _lg else []) + [""]
            out += _map
        # else: a single-check / single-step doc — never render a one-bar level; skip entirely.

    if minority_slow:
        def _fmt_ms(cs: list[dict[str, Any]]) -> str:
            return ", ".join(
                f"`{_clean_label(str(c.get('name','')))}` (~{_clock(_num(c.get('p50_s')))})"
                for c in cs)
        _file_ms = [c for c in minority_slow[:3] if _ms_is_file(c)]
        _ext_ms = [c for c in minority_slow[:3] if not _ms_is_file(c)]
        # Split the file checks by WHY they were demoted: present on a majority but rarely the
        # actual slowest (frequency-demoted — the expo/expo class) vs genuinely ran on a minority
        # (opt-in / path-gated). The "ran on a minority" wording is false for the former.
        _file_ms_freq = [c for c in _file_ms if _ms_freq_demoted(c)]
        _file_ms_pres = [c for c in _file_ms if not _ms_freq_demoted(c)]
        notes = []
        if _file_ms_freq:
            # Claims layer (minority_slow_note): the PLURAL frequency-demoted twin of the
            # singular `_ms_freq_demoted` minority_slow_note fragment above. It omits the word
            # "check", so its family phrase is "slowest a PR waits on" (its own
            # FRAMING_VOCABULARY template). `subject` is the joined minority check names. The
            # STATIC text has no em dash, but `_fmt_ms` interpolates check NAMES that can carry
            # dash glyphs — so strip at construction for manifest<->report parity (the report
            # gets the report-wide strip; without this the manifest keeps the glyph and the
            # coverage check false-fails). Idempotent, so byte-identical.
            notes.append(cs.add(claims.Claim(
                kind="minority_slow_note",
                subject=", ".join(str(c.get("name", "")) for c in _file_ms_freq),
                fields={"n": len(_file_ms_freq)},
                rendered=_strip_emdashes(
                    "workflow check(s) present on most sampled PRs but rarely the actual "
                    "slowest a PR waits on (a heavier concurrent check gates ahead of "
                    f"them): {_fmt_ms(_file_ms_freq)}"))))
        if _file_ms_pres:
            if _req_scoped:
                notes.append("*required* check(s) that ran on a minority of sampled PRs "
                             "(path-conditional — they gate only the PRs whose paths trigger "
                             f"them, where they ARE the merge pole): {_fmt_ms(_file_ms_pres)}")
            else:
                notes.append("opt-in / conditional workflow check(s) that ran on a minority of "
                             "sampled PRs (label-gated or path-filtered — a typical PR doesn't "
                             f"wait on them): {_fmt_ms(_file_ms_pres)}")
        if _ext_ms:
            notes.append("external / AI review check(s) with no workflow file to fix: "
                         f"{_fmt_ms(_ext_ms)}")
        # The tail must address each set on its own terms: on a required-scoped spine the FILE
        # check(s) ARE required gates (speed them), while an external check never has a workflow
        # to fix. A blanket "treat it as throughput/cost (opt-in)" tail would contradict the
        # required framing in the file note above when BOTH are present, so compose it.
        if _req_scoped and _file_ms:
            _tail = ("The *required* check(s) gate the merge on the PRs that run them — speed "
                     "them to help those PRs.")
            if _ext_ms:
                _tail += (" The external / AI review check(s) have no workflow file to fix — "
                          "make them non-blocking rather than the long pole.")
        else:
            _tail = ("Unless one is a *required* status check it does not gate the merge — treat "
                     "it as throughput/cost (an opt-in job) or make an external check "
                     "non-blocking, rather than the long pole.")
        out += [f"> Also slower on **some** of the sampled PRs (not the typical path, not "
                f"in the Contents critical path): {'; '.join(notes)}. {_tail}", ""]

    # Scan ALL typical checks (not just the poles listed in Contents): the whole point is
    # a check whose SLOW mode would rank it as a top gate but whose median ranks it low and
    # out of the critical-path list (e.g. a turbo-cache-dependent job fast on cache hits).
    out += _bimodal_note(src, _num(blocker.get("p50_s")))

    for i, p in enumerate(pole_wfs, 1):
        check = _clean_label(str(p.get("check", "")))
        wf_base = _wf_base(p.get("workflow_file", ""))
        head_s, bi_caveat = _pole_headline(p)
        dur = _clock(head_s)
        _raw_check = str(p.get("check", ""))
        # AGGREGATION-GATE (success-sink) detection — issue #1, see `_agg_gate_shape`. Three
        # deliberate carve-outs: a modal-chain MEMBER keeps the chain-stage framing (the chain
        # rendering wins; never double-frame), and a pole carrying real measured content of its
        # own — a matched log-detector leaf, or a structural lever routed to it — keeps today's
        # rendering, because there the drill/prompt is NOT inert and suppressing it would
        # silently drop a measured lever that renders nowhere else.
        _agg_key = pole_owner_keys.get(id(p))
        _agg = None
        if (not (chain_active and _raw_check in set(_chain_modal))
                and _pole_leaves[id(p)][1] is None
                and not _structural_for_pole(p, all_findings)):
            _agg = _agg_gate_shape(
                p, doc.get("workflow_job_graph"), list(checks or src),
                steps.get(_agg_key) if _agg_key is not None else None)
        if chain_active and _raw_check in set(_chain_modal):
            # ENG-1 PR-N2: a chain MEMBER must never carry the concurrent
            # framing — `needs:` serializes it; its time ADDS to the headline
            # chain wait instead of overlapping. Claims layer (pole_role_line).
            _stage = _chain_modal.index(_raw_check) + 1
            role = cs.add(claims.Claim(
                kind="pole_role_line", subject=check,
                fields={"stage": _stage, "chain_len": len(_chain_modal),
                        "dur": dur},
                rendered=_strip_emdashes(
                    f"**Stage {_stage}/{len(_chain_modal)} of the {_chain_label} "
                    f"gate chain.** `needs:` serializes it, so its {dur} ADDS to "
                    "the chain wait in the headline rather than overlapping it; "
                    "time cut here helps until the next-longest competing path "
                    "gates instead.")))
        elif _agg:
            # AGGREGATION GATE (issue #1). Crowning it is correct — it really is the check
            # most PRs gate on — but its wait is NOT its own: it is the `needs:` upstream it
            # aggregates. Say that, name the slowest measured upstream member, and (below)
            # render NO drill and NO "optimize this step" prompt for the sink itself.
            _ag_slow = _clean_label(_check_name(_agg["slowest"]))
            _ag_dur = _clock(_num(_agg["slowest"].get("p50_s")))
            # BOTH caveats compose — they are independent, and neither may silently drop the
            # other. "Runs no work of its own" rests on the measured P50 plus the `needs:`
            # structure whenever no per-step data was captured, so that basis is disclosed on
            # its own terms even when an unmeasured upstream member is ALSO disclosed (an
            # `elif` hid the weaker basis exactly when the sample was thinnest — greptile P1).
            _ag_tail = ""
            if _agg["unmeasured"]:
                _ag_tail += (f" ({_count_noun(len(_agg['unmeasured']), 'upstream job')} had no "
                             "measured check timing in this sample, so the slowest upstream "
                             "member could be a heavier one.)")
            if not _agg["steps_known"]:
                _ag_tail += (" (No per-step data was captured for this check; the shape is read "
                             "from its `needs:` structure and its measured P50.)")
            role = cs.add(claims.Claim(
                kind="pole_role_line", subject=check,
                fields={"dur": dur, "job_id": str(_agg["job_id"]),
                        "upstream_n": len(_agg["upstream"]),
                        "upstream_slowest": _ag_slow, "upstream_dur": _ag_dur},
                rendered=_strip_emdashes(
                    f"**Aggregation gate — it exists to be the single required check.** Its "
                    f"job (`{_agg['job_id']}`) runs no work of its own ({dur}); it `needs:` "
                    f"{_count_noun(len(_agg['upstream']), 'upstream job')} so one check can "
                    f"stand for all of them. So its {dur} is not the wait — the wait IS that "
                    f"`needs:` upstream, whose slowest measured member is `{_ag_slow}` "
                    f"(~{_ag_dur}). That member, not this check, is the lever."
                    + _ag_tail)))
        elif p is blocker:
            if not npop:
                # Claims layer (pole_role_line): the blocker pole's role label.
                # `subject` is this pole's own check name; no gate comparator applies
                # to this kind (the coverage check is its bind). No em dash here.
                role = cs.add(claims.Claim(
                    kind="pole_role_line", subject=check,
                    fields={"dur": dur, "npop": npop},
                    rendered="**The slowest check a PR waits on.**"))
            elif gate_is_slowest:
                role = cs.add(claims.Claim(
                    kind="pole_role_line", subject=check,
                    fields={"dur": dur, "npop": npop},
                    rendered="**The slowest check a typical PR waits on.**"))
            else:
                # The gate is the check MOST PRs wait on (highest pole_count), but a slower
                # concurrent check (`floor_name`) is the genuine slowest — so don't call this
                # one "the slowest"; that would contradict the headline's truthful split.
                # Quote `floor_name`'s OWN time (`slowest_dur`); it only "sets the wall-clock
                # floor" when it isn't a non-universal check (else a median PR finishes sooner).
                _sets_floor = "" if floor_lowered else ", which sets the wall-clock floor"
                role = (f"**The check most PRs gate on.** A typical PR waits on this most "
                        f"often; the slowest concurrent check is `{floor_name}` "
                        f"(~{slowest_dur}){_sets_floor}.")
        elif npop and not _pole_typical(p):
            _pc = present.get(str(p.get("check", "")), 0)
            if _req_scoped:
                role = (f"**Required · path-conditional — ran on {_pc}/{npop} sampled PRs.** A "
                        f"*required* gate, but only on PRs whose paths trigger it (where its "
                        f"{dur} IS the merge pole); a typical PR doesn't run it, so it's "
                        "demoted below the common gate. Speeding it helps the PRs that run it.")
            elif _have_pole_n:
                # Pole-frequency demotion (the new ranking rule): the check was demoted for being
                # RARELY the actual per-PR gate, NOT for low presence — so the role text must state
                # the frequency reason. A check present on every PR but never the slowest (`pole_n`
                # low) is the exact expo/expo `check-packages` case; narrating "opt-in / rare — ran
                # on 20/20" here would contradict its own presence count. Speeding it helps only the
                # PRs where it IS the pole; it doesn't move typical merge-wait (a concurrent check
                # gates ahead of it).
                _pn = int(_pole_n.get(str(p.get("check", ""))) or 0)
                # Claims layer (pole_role_line): the rare/minority role. `subject` is this
                # pole's OWN check name — deliberately NOT the stamped gate (this pole is
                # rarely the gate), which is why it must NOT be kind="headline_slowest"
                # (that kind's comparator enforces subject==stamp). Em-dash source, so strip
                # at construction for manifest<->prose parity (the report-wide strip is
                # idempotent, so this stays byte-identical).
                role = cs.add(claims.Claim(
                    kind="pole_role_line", subject=check,
                    fields={"pole_n": _pn, "present": _pc, "npop": npop, "dur": dur},
                    rendered=_strip_emdashes(
                        f"**Rarely the merge gate — the actual slowest check a PR waits on, on "
                        f"only {_pn}/{npop} sampled PRs.** Present on {_pc}/{npop} PRs, but a "
                        f"slower concurrent check almost always gates ahead of it, so its {dur} is "
                        "throughput/cost, not merge-wait. Speeding it helps only the PRs where it "
                        "IS the pole — it won't move typical merge-wait.")))
            else:
                role = (f"**Opt-in / rare — ran on only {_pc}/{npop} sampled PRs.** Not a check "
                        f"a typical PR waits on, so its {dur} is throughput/cost, not merge-wait. "
                        "Shown for completeness; confirm whether it's label-gated or conditional "
                        "before treating it as merge-blocking.")
        elif i == 1 and not blocker_is_file:
            bn = _clean_label(str(blocker.get("check", "")))
            # Framing-family exclusion (plan 007): "slowest **fixable** check on a typical
            # PR" is a DIFFERENT family (fixable-qualified, no "waits on") — no
            # FRAMING_VOCABULARY template matches it. A candidate future family, not this one.
            role = (f"The slowest **fixable** check on a typical PR. The single slowest "
                    f"check overall is `{bn}` ({_clock(_num(blocker.get('p50_s')))}), a "
                    "managed check with no file to fix (see note above). Because that "
                    "managed check runs concurrently and is slower, cutting this pole "
                    "reduces merge wait only on the PRs where it - not the managed "
                    "check - is the actual gate; on the rest the managed check gates "
                    "and must drop first.")
        else:
            # Name the ACTUAL slowest concurrent check above this pole (on the same
            # headline basis as `dur`), not just the previous pole - there can be an
            # intervening check that isn't itself a drilled pole (e.g. a matrix shard),
            # and "becomes the gate once <prev pole> drops" would skip it. "every slower
            # concurrent check" then honestly covers those in-between.
            def _cn(c: dict[str, Any]) -> str:
                return _clean_label(str(c.get("name") or c.get("check") or ""))
            _pole_wf = str(p.get("workflow_file", ""))
            # Rank the concurrent checks above this pole by their EFFECTIVE floor
            # (`_eff_floor_s`, bimodal-aware `max(p50, high_p50_s)`) — the SAME notion
            # every other floor path and verify_report's spine-drop check use — NOT the
            # bare p50. Selecting by p50 named the p50-slowest sibling of a matrix while
            # the check that actually CAPS the wait was a DIFFERENT leg whose bimodal SLOW
            # mode is the effective ceiling (e.g. `guard shard 4/4`: blended p50 below
            # `1/4` but slow-mode above `1/4`'s). The report then named a shard that isn't the
            # binding floor, so the floor went undisclosed and verify_report FAILed.
            above = sorted(
                (c for c in src
                 if _eff_floor_s(c) > head_s and _cn(c) != check
                 and not _same_matrix(check, _cn(c),
                                      _pole_wf, str(c.get("workflow_file", "")))),
                key=lambda c: -_eff_floor_s(c))
            if above:
                lead = above[0]
                _lead_floor = _clock(_eff_floor_s(lead))
                # Claims layer (pole_role_line family): `subject` is the interpolated
                # lead-check name (the concurrent check this pole runs behind), valued at
                # its effective floor. The field key is `lead_floor` (NOT `lead_p50`): the
                # value is `_eff_floor_s(lead)` — the bimodal-aware `max(p50, high_p50_s)`,
                # which can be the slow mode, not the median — so a `p50` key would misname
                # it and invite a consumer to read a floor as a median. Wrap in
                # `_strip_emdashes` for parity with the headline claims, so the manifest's
                # `rendered` matches the em-dash-stripped report text; the report-wide strip
                # is idempotent, so this stays byte-identical.
                role = cs.add(claims.Claim(
                    kind="pole_role_line", subject=_cn(lead),
                    fields={"lead_floor": _lead_floor, "dur": dur},
                    rendered=_strip_emdashes(
                        f"Runs concurrently behind `{_cn(lead)}` "
                        f"({_lead_floor}); it becomes the gate only "
                        f"once every slower concurrent check drops below {dur}.")))
            else:
                role = (f"Runs concurrently — becomes the gate once the slower checks "
                        f"above it drop below {dur}.")
        if chain_active and _raw_check not in set(_chain_modal):
            # ENG-1 PR-N2: on a chain-gated repo a non-member pole's own
            # wall-clock matters only once the chain drops below it — say so,
            # or this section contradicts the headline (pass-B finding 1).
            role += (f" Note: the {_chain_label} chain ({merge_dur}) gates the "
                     "merge ahead of this check — its wall-clock figure applies "
                     "once the chain drops below it.")
        owner_key = pole_owner_keys.get(id(p))

        def _match(d: dict[str, Any], _k: Any = owner_key) -> Any:
            # Bind by the pole's OWN identity-resolved key (R1) — never a workflow-stem
            # borrow. An undrilled pole sharing a workflow with a drilled one owns no key
            # and falls through to the honest "(no captured log for this job)" path.
            return d.get(_k) if _k is not None else None

        # The pole's (log_text, leaf, offcat_leaf) — the SAME `_derive_pole_leaf` result the
        # Long pole map read for the descent pole (pole 1), so the map's Level 3 and this
        # pole's Level 3 can never disagree. The derivation itself is unchanged: bind by the
        # pole's OWN key, `_parse_log`, `_apply_cache_dist` (reframe a cache leaf to its
        # measured hit-rate distribution), then off-category demotion (issue #16 — a whole-log
        # leaf whose category contradicts the pole's dominant work is kept as a secondary note,
        # never crowned).
        log_text, leaf, offcat_leaf = _pole_leaves[id(p)]
        sample = _match(samples)
        run_url = _match(log_runs)
        timeline = _match(steps)
        # The timeline carries its own run URL; prefer the explicit --log-run, fall
        # back to the timeline's so the rendered run link always matches the run the
        # timeline + drill came from.
        if not run_url and isinstance(timeline, dict):
            run_url = timeline.get("run_url") or None
        # Check/step name-collision disambiguation (owner UX edit 2026-07-19): when this
        # pole's CHECK name collides with a small STEP name rendered inside it (e.g. the
        # check `test` IS the 8m58s gate while a 31s `Test` step reads as "so test isn't the
        # bottleneck"), append ONE clarifying clause naming the collision and the real
        # dominant step. Collision-triggered (never boilerplate); appended OUTSIDE any
        # claim's `rendered` span, so the claims manifest's exact-substring match is intact.
        _coll_step, _coll_dom = _check_step_collision(p, timeline)
        if _coll_step:
            if len(_coll_dom) <= 40:
                _dom_disp = _coll_dom
            else:  # break on a word boundary so the ellipsis never cuts mid-word
                _dom_disp = (_coll_dom[:40].rsplit(" ", 1)[0] or _coll_dom[:39]) + "…"
            role += (f" (the check named `{check}`; its small `{_coll_step}` step below is "
                     f"not the bottleneck — the dominant step is `{_dom_disp}`)")
        mag = _match(mags)
        # Structural-track findings (OPT70–75) routed to THIS pole. The structural track
        # is rendered AS the pole, so join it back here — without this the finding (its
        # risk/guardrail/rollout) renders nowhere and the pole falsely reads as a
        # coverage gap (it matched a measured catalog lever, just not a log detector).
        structural = _structural_for_pole(p, all_findings)
        # Data-driven catalog finding(s) (e.g. OPT24) that fired ON this pole with a
        # credited wall-clock saving. They render in 'Also noticed' (not AS the pole), but
        # they ARE a catalog match — so they must suppress the false "coverage gap" read +
        # the gap-fill exactly as a structural match does, or the spine self-contradicts
        # the appendix's "sits ON the critical path … See the spine above" note.
        data_driven = _data_driven_for_pole(p, all_findings)
        # An LLM gap-fill analysis only applies to a pole that matched NO catalog
        # detector (SKILL.md phase 4a). A structural- OR data-driven-track match IS a
        # catalog match, so it suppresses the gap-fill exactly as a log-detector match
        # (`leaf`) does.
        # A pole whose leaf was DEMOTED off-category (`offcat_leaf`) is NOT a coverage gap —
        # a detector DID match its log, it just isn't the pole's dominant work — so it must
        # not pull an LLM gap-fill analysis (that path is for poles no detector recognized).
        analysis = (_match(analyses)
                    if (leaf is None and offcat_leaf is None
                        and not structural and not data_driven) else None)
        out.append(f'<a id="pole-{i}"></a>')
        out.append("")
        # `wf_base` (a workflow FILENAME) and `check` are BOTH arbitrary repo text dropped
        # into a `## ` heading — bare, a `*`/`_`/backtick renders as formatting or opens a
        # stray span (a single backtick in the filename `` `c`i.yml` `` closes the inline
        # span early), and a >=3-backtick run would break out entirely. `_safe_span` maps
        # EVERY backtick to an apostrophe and wraps as one inline-code token, so neither can
        # break the heading. Byte-identical for a clean filename/name (`` `ci.yml` `` /
        # `` `build` ``). The `<a id="pole-N">` anchor and every `#pole-N` TOC link key off
        # the integer `i`, never this text, so wrapping the display names changes nothing
        # about anchor/TOC resolution.
        out.append(f"## {_severity_dot(head_s)} Long pole {i}: "
                   f"{_safe_span(wf_base)} ▸ {_safe_span(check)} — {dur}")
        out.append("")
        out.append(f"_{role}_" if not role.startswith("**") else role)
        out.append("")
        # Machine marker: which log-detector leaf crowned this pole's MEASURED CAUSE. Emitted
        # only for a leaf that survived off-category demotion, so verify_report re-derives the
        # crowned leaf's category and asserts it agrees with the pole's dominant_category.
        if leaf is not None:
            out += [_LEAF_CROWN_MARKER.format(fk=str(leaf.get("fix_key", ""))), ""]
        if bi_caveat:
            out += [bi_caveat, ""]
        if _agg:
            # AGGREGATION GATE (issue #1): the section ENDS at the honest pointer. No
            # per-step drill and no "capture timing, then optimize this step" agent prompt
            # for the sink itself — both are inert over a job that runs no work, and the
            # prompt actively misdirects (there is nothing to speed, and moving it off the
            # PR path defeats the single-required-check job it does). The `_floor_note`
            # ("what a change here can buy") is skipped for the same reason: a change HERE
            # buys nothing, which is exactly what the role line just said. The reader is
            # pointed at the pole that drills the slowest upstream member, or told which
            # check it is when it isn't among the rendered poles (never a dead link).
            # IDENTITY, not presentation: the pole this links to is matched on the RAW check
            # name + workflow file — the same (name, file) identity the spine carries. Matching
            # on the CLEANED display label instead would fold two distinct checks whose labels
            # normalize together (`@scope/lint` and `lint` both clean to `lint`) and link the
            # gate's pointer at an unrelated pole's drill (greptile P2; the #13 name-join
            # lesson). `_ag_slow` stays the DISPLAY label only.
            _ag_wf = str(_agg["slowest"].get("workflow_file") or p.get("workflow_file") or "")
            _ag_raw = _check_name(_agg["slowest"])
            _ag_pole_i = next(
                (j for j, pw in enumerate(pole_wfs, 1)
                 if _check_name(pw) == _ag_raw
                 and str(pw.get("workflow_file") or "") == _ag_wf), None)
            if _ag_pole_i is not None:
                out += [f"**➡️ Where the wait actually is:** [Long pole {_ag_pole_i}]"
                        f"(#pole-{_ag_pole_i}) drills `{_ag_slow}` ({_ag_dur}) - the slowest "
                        "measured member of this gate's `needs:` upstream. Attack it there; "
                        "this check follows it down for free.", ""]
            else:
                out += [f"**➡️ Where the wait actually is:** `{_ag_slow}` ({_ag_dur}), the "
                        "slowest measured member of this gate's `needs:` upstream. It is not "
                        "among the long poles drilled in this report (it ranks below them, or "
                        "its own job timing wasn't sampled), so there is no step-level drill "
                        "for it here - a re-run that samples it will drill it. Attack it "
                        "there; this check follows it down for free.", ""]
            continue
        out += _floor_note(p, floor_pool)
        # A data-driven match is on-path only if ANY joined finding is NOT `spine_rare`; a match
        # made up solely of presence-demoted (opt-in/rare) findings is still coverage, but the
        # waterfall pointer must say "opt-in / rare", not "sits ON the critical path" (mirrors the
        # appendix's `_frames_on_path` split — the paradedb `Test pg_search` double-framing).
        dd_on_path = any(not f.get("spine_rare") for f in data_driven)
        out += ["```text", *_pole_waterfall(p, leaf, timeline,
                                            log_present=log_text is not None,
                                            analysis_present=bool(analysis),
                                            structural_present=bool(structural),
                                            data_driven_present=bool(data_driven),
                                            data_driven_on_path=dd_on_path),
                "```", ""]
        # The cross-run magnitude check (rendered below) - compute now so the footer
        # only promises it when it actually appears (a categorical finding has none).
        mag_block = _mag_line(mag, leaf)
        # Whether the "🔬 Cross-run check" section actually renders for this pole. The
        # prompt builders + the LLM-analysis block gate their "validated across runs in
        # the cross-run check above" claim on this, so a singleton magnitude sample (which
        # suppresses the section) can't leave a dangling reference to a section that isn't
        # there (verify_report re-derives this per pole in check_rca_hands_off_never_prescribes).
        cross_run_rendered = bool(mag_block)
        if (leaf is not None and leaf.get("deeper") or timeline) and run_url:
            rid = run_url.rstrip("/").rsplit("/", 1)[-1]
            if mag_block and leaf is not None:
                tail = (" The **cause** below is the same across runs; the "
                        "**magnitudes** are this run's (see the cross-run check).")
            elif mag_block:
                # Undetected pole: no named cause, but the dominant step's wall time is
                # validated across runs in the cross-run check below.
                tail = (" The dominant step's wall time is validated across runs in the "
                        "cross-run check below.")
            elif leaf is not None:
                tail = (" The **cause** below (how the steps are structured) is the "
                        "same across runs.")
            else:
                tail = ""
            # For a BIMODAL pole `collect_runs` drills a SLOW-mode representative (so
            # the drill shows WHY it's slow), not the overall-P50 run — say so, else
            # the caption claims "closest to P50" over a run well above the median.
            _bi = p.get("bimodal")
            _slow_drill = isinstance(_bi, dict) and _num(_bi.get("high_p50_s"))
            if _slow_drill:
                _frac = _num(_bi.get("slow_frac"))
                _share = f" (~{round(_frac * 100)}% of runs)" if _frac else ""
                which = (f"the one representative of its **slow mode**{_share} — "
                         f"the mode this drill exists to explain")
            else:
                which = "the one whose duration is closest to the typical (P50) time"
            out += [f"_The timeline and the per-step times above are from **one "
                    f"representative run** — {which}, [run {rid}]({run_url})."
                    + tail + "_", ""]
        out += _audit_links(timeline, p, leaf, run_url)
        # The detector-specific evidence (the cross-run sample list + the verbatim
        # cause lines) only exists when a catalog pattern matched. The cross-run check
        # and the agent prompt, by contrast, render for EVERY pole - so an undetected
        # pole (e.g. a build the detector set doesn't recognize) is still a complete
        # finding (timeline -> cross-run check on its dominant step -> hand-off), not a
        # bare timeline that dead-ends.
        if leaf is not None and sample and sample.get("runs"):
            runs = sample["runs"]
            out.append(f"**🔬 Evidence — {sample.get('summary', '')}**")
            out.append("")
            for r in runs:
                rid = str(r.get("url", "")).rstrip("/").rsplit("/", 1)[-1]
                note = r.get("note", "")
                out.append(f"- [run {rid}]({r.get('url','')})"
                           + (f" — `{note}`" if note else ""))
            out.append("")
        # Cross-run check on the load-bearing magnitude (matched cause) OR, for an
        # undetected pole, on the dominant step's wall time - median + range across a
        # few runs, so the single drilled run's number isn't taken on faith.
        out += mag_block
        # Cache health across runs/events (only for a cache pole carrying cache_dist):
        # the PR-vs-push, fork-aware miss distribution + verdict + (when demoted) the
        # cache-context caveat and its machine marker.
        out += _cache_health_block(p.get("cache_dist"))
        if leaf is not None:
            ev = leaf.get("evidence") or []
            if ev:
                lead = ("Verbatim from one of those runs' log:" if sample
                        else "**🔬 Evidence** — verbatim from the captured job log:")
                out += [lead, "", "```text", *[_fence_safe(e) for e in ev], "```", ""]
        elif analysis:
            # LLM gap-fill: no catalog match, so the agent's grounded reading of the
            # captured log stands in for the measured cause (clearly labelled).
            out += _llm_analysis_block(analysis, cross_run_rendered=cross_run_rendered)
        # The structural-track finding(s) routed to this pole render LOUD here — the
        # pole IS the structural lever, and they're excluded from the hygiene appendix,
        # so this is the only place their risk/guardrail/rollout appears.
        if structural:
            out += _structural_block(structural, catalog_url)
        # A matrix sibling leg that collapsed into THIS representative pole can carry its
        # own structural lever that — since `_structural_for_pole` no longer folds a
        # distinct sibling AND `_also_noticed_block` excludes per-pole structural levers —
        # would otherwise render nowhere (a silent drop). Surface it here, carrying the
        # sibling's own measured numbers; never let the lever vanish from the markdown.
        sibling_struct = _collapsed_sibling_structural(p, pole_wfs, file_poles,
                                                       all_findings)
        if sibling_struct:
            # Collapse the DUPLICATE case (issue #53): a sibling whose lever is IDENTICAL to
            # the pole's own (same routed pattern + dominant-step base + category) adds one
            # compact per-leg measurement line under the pole's block instead of a second
            # full copy of the same guardrail/rollout/failure-mode boilerplate. A sibling
            # carrying a genuinely DIFFERENT lever (different pattern or dominant step) keeps
            # its own full block. Either way every leg's name + measured numbers still render,
            # so the anti-drop rule holds — only the repeated boilerplate is dropped. When the
            # pole has NO own structural block (`structural` empty), there is nothing to
            # collapse into, so every sibling keeps its full block (unchanged behaviour).
            pole_ids = {i for i in (_struct_identity(pf) for pf in structural)
                        if i is not None}
            collapsible, distinct = [], []
            for leg, f in sibling_struct:
                ident = _struct_identity(f)
                (collapsible if (ident is not None and ident in pole_ids)
                 else distinct).append((leg, f))
            if collapsible:
                out += _collapsed_sibling_line(collapsible)
            if distinct:
                out += _sibling_structural_annotation(distinct, catalog_url)
        # Prompt: an LLM-authored, log-tailored one when the gap was filled; otherwise
        # the matched-cause prompt, or the generic dominant-step prompt.
        if analysis and str(analysis.get("prompt", "")).strip():
            out += [_llm_agent_prompt(analysis["prompt"]), ""]
        else:
            out += [_build_agent_prompt(
                leaf, p, floor_pool, run_url, repo, doc.get("commit_sha"),
                wf_gate.get(str(p.get("workflow_file", "")), 0), npop, timeline,
                structural=structural, data_driven=data_driven, cs=cs,
                cross_run_rendered=cross_run_rendered), ""]
        # A leaf demoted off-category (issue #16) is kept as a labelled secondary
        # observation below the prompt — never a silent drop of a real (if minority) finding.
        if offcat_leaf is not None:
            out += _offcategory_note_block(offcat_leaf, p)

    # Disclose per-pole structural levers (OPT70/71/72/74/75) on checks ranked below the
    # top-N spine: collect_runs analyses the top 5 critical-path checks but the spine shows
    # the top 2, and a per-pole structural lever is excluded from the off-path appendix
    # (`_is_pole_structural`), so one on a non-rendered pole would otherwise survive only in
    # findings.json. Surface a COUNT (not a silent drop), mirroring the appendix's "+N more …
    # kept in the findings JSON". Never invents numbers — only states that they exist.
    undisclosed = _undisclosed_pole_structural(all_findings, pole_wfs, file_poles)
    # Partition for an honest disclosure (R3): file-backed levers rank below the rendered
    # poles; name-stub ones are on managed/unresolved checks (no single workflow file). Both
    # are disclosed so NEITHER is silently dropped from the count, and a managed check is
    # never mis-claimed as "ranks below the rendered poles".
    _below = [f for f in undisclosed
              if _is_real_workflow_path(str(f.get("workflow_file", "")))]
    _unresolved = [f for f in undisclosed
                   if not _is_real_workflow_path(str(f.get("workflow_file", "")))]
    if _below:
        _pats = ", ".join(sorted({str(f.get("pattern", "")) for f in _below}))
        out += ["> [!NOTE]",
                f"> **+{len(_below)} structural lever(s) on lower-ranked poles "
                f"({_pats}) not shown** - their home check ranks below the "
                f"{len(pole_wfs)} pole(s) rendered above, so the lever is kept in the "
                "findings JSON (with its risk/guardrail/rollout) rather than dropped.", ""]
    if _unresolved:
        _pats = ", ".join(sorted({str(f.get("pattern", "")) for f in _unresolved}))
        out += ["> [!NOTE]",
                f"> **+{len(_unresolved)} structural lever(s) on managed/unresolved checks "
                f"({_pats}) not shown** - their check has no single editable workflow file "
                "(a managed/app check, or a job defined across multiple workflows), so it "
                "isn't rendered as a pole; the lever is kept in the findings JSON.", ""]
    # Below the spine: first the pre-start wall-clock wait (its own section — wall-clock
    # the spine doesn't capture, NOT hygiene), then the promoted runner-minute tier, then
    # the off-path / set-aside material — the residual hygiene appendix, advisory signals, and the
    # judgment-needed checklist.
    if queue_lines:
        out += ["---", "", *queue_lines]
    if tier2_lines:
        out += ["---", "", *tier2_lines]
    if also_lines:
        out += ["---", "", *also_lines]
    if shallow_note and not queue_lines and not also_lines:
        # The report-level shallow-sample disclosure normally rides the Pre-start-wait
        # or Also-noticed sections (its two carriers above). When NEITHER renders, it
        # must still appear — a shallow sample read as exact is a silent coverage gap
        # (§5.5/G15; `check_cost_spine_shallow_disclosed` re-derives it from
        # `data_sources`, so dropping this line is a verify FAIL, not a style choice).
        out += ["---", "", f"> ⚠️ _{shallow_note}_", ""]
    out += _dropped_unprovable_banner(cp.get("dropped_unprovable")
                                      or doc.get("dropped_unprovable"))
    # The prose provenance block leads the Data sources section (owner UX edit
    # 2026-07-19) instead of rendering after the Contents.
    out += _data_sources_footer(doc, repo,
                                lead=_provenance_block(doc, repo, captured_at))
    _any_bimodal = any(isinstance(p.get("bimodal"), dict)
                       and _num(p.get("bimodal", {}).get("high_p50_s"))
                       for p in pole_wfs)
    _rep_clause = ("the one closest to the P50 time (for a **bimodal** pole, a "
                   "representative of its slow mode — the mode the drill explains)"
                   if _any_bimodal else "the one closest to the P50 time")
    out.append(f"_The concurrent checks (the Contents critical path) are P50 across "
               f"sampled PRs. The per-step timeline + the drill are **one representative "
               f"run** — {_rep_clause} — so they are absolute for that run, not P50. "
               "The **categorical cause** is stable across runs; where a "
               "**Cross-run check** is shown it gives the magnitude's median + range "
               "across several runs, so the single run's number isn't taken on faith. "
               "Per-step bars are scaled within each drill._")
    out.append("")
    out.append("_The drill bars are plain-English labels for what's in the job log "
               "(e.g. a `DB migrations` bar is logged as `Total Migration Time:`). To "
               "verify any number, follow the pole's **🔗 Audit** link to the gating "
               "step, expand it, and search (Ctrl-F) for the verbatim strings the "
               "Audit line lists — GitHub anchors to the step, not an exact log line._")
    out += _starsling_footer()
    return _strip_emdashes("\n".join(out))


# --------------------------------------------------------------------------- #
# Gap → catalog loop, phase 4b/4c plumbing (SKILL.md). 4b (capture the gap) used
# to be PROSE the orchestrating model had to remember to run AFTER it already had
# a satisfying report — so a model that finished the gap-fill (4a) and rendered
# would skip it, and the feedstock the whole loop needs never got written. These
# helpers move 4b into deterministic CODE that rides on the gap-fill RE-RENDER the
# agent always runs (render with `--analysis`), and emit a loud, machine-readable
# signal whether or not an analysis is attached so the gap is impossible to miss.
# --------------------------------------------------------------------------- #


def _skill_root() -> Path:
    """The skill's own directory (scripts/..); the `git -C` anchor for the maintainer-source
    and gaps-root probes. Captures no longer live here — they root at the repo root via
    `_gaps_root_default()`, kept out of the install surface."""
    return Path(__file__).resolve().parent.parent


def _skill_scripts_tree_sha(scripts_dir: Path | str | None = None) -> str | None:
    """Short git tree sha of `scripts/` at HEAD — the RENDER-time provenance token.

    Why a tree and not the commit: this repo squash-merges, which discards the branch
    commit a report was rendered from. A report generated on a branch therefore records
    a sha that vanishes at merge, and `check_skill_commit_provenance` can never confirm
    it. A squash PRESERVES the tree, so the tree sha survives the merge intact.

    Why `scripts/` and not the whole skill dir: the skill dir contains `reports/`, so
    committing a report would change the very tree that report claims to be produced
    under — a self-referential stamp that can never match.

    None when this isn't a git checkout (an installed skill) — the token is then
    omitted and the verifier falls back to commit-only provenance.

    Scope, stated plainly: this token is provenance for the MAINTAINER's committed
    worked examples. If an end user vendors this skill into their own git repo, it
    records a tree sha from THEIR repo — exactly as the existing `skill commit` token
    already records their repo's HEAD (`run.py:_git_short_sha`). Harmless: nothing
    verifies an end-user report with `--skill-repo`. Not worth a maintainer-source
    probe, which cannot tell the two apart anyway (`git ls-files` succeeds on a
    vendored copy the user committed).

    Suffixed `-dirty` when `scripts/` has uncommitted edits. This matters: `git
    rev-parse HEAD:scripts` reads the COMMITTED tree, so a report rendered from a
    dirty working tree would otherwise stamp a clean tree it was not produced by.
    The verifier refuses a dirty tree as provenance — commit `scripts/` first, then
    re-render.

    `scripts_dir` exists only so tests can drive this against a throwaway repo.

    NEVER raises. This runs on every render, in an end user's repo, and a crashed
    render is far worse than a missing provenance token. `OSError` is the catch that
    matters: `subprocess.run` raises `PermissionError` (an OSError, NOT a
    `SubprocessError`) when a `git` on PATH exists but is not executable, plus
    `NotADirectoryError` / `ENOEXEC` for a broken PATH entry."""
    scripts = Path(scripts_dir) if scripts_dir else Path(__file__).resolve().parent
    # ONE budget across all three calls, not `timeout=10` each. A `git` that answers in
    # 9s per call (a huge working tree, a network filesystem, a corporate wrapper) stays
    # under a per-call cap while adding ~27s to every render. Measured.
    deadline = time.monotonic() + 6.0

    def _git(*args: str) -> subprocess.CompletedProcess | None:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        return subprocess.run(["git", "-C", str(scripts), *args],
                              capture_output=True, text=True, timeout=left)

    try:
        pre = _git("rev-parse", "--show-prefix")
        if pre is None or pre.returncode != 0 or not pre.stdout.strip():
            return None
        prefix = pre.stdout.strip()
        r = _git("rev-parse", "--short", f"HEAD:{prefix}")
        st = _git("status", "--porcelain", "--", ".")
    except (OSError, subprocess.SubprocessError):
        return None
    # `st.returncode != 0` matters as much as `r`'s: if `git status` fails (broken
    # index, hostile hook), dirtiness is UNKNOWN — stamping a clean sha there would be
    # false provenance. Unknown dirtiness = no token, same as every other can't-tell case.
    if r is None or st is None or r.returncode != 0 or st.returncode != 0:
        return None
    sha = r.stdout.strip()
    # VALIDATE. `sha` is whatever `git` printed. A wrapper/shim `git` that echoes an
    # advisory line, or any non-git binary first on PATH, would otherwise be stamped
    # verbatim into the report — breaking out of its markdown code span (stray
    # backticks/pipes/newlines corrupt the Data-sources table) and asserting provenance
    # that describes nothing. Reject anything that is not a bare short-or-full sha.
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return None
    dirty = bool(st.stdout.strip())
    return f"{sha}-dirty" if dirty else sha


def _is_maintainer_source() -> bool:
    """True when this skill IS the tracked monorepo source (a maintainer checkout),
    not an installed/vendored copy — `git ls-files --error-unmatch` succeeds on a
    tracked path. Mirrors SKILL.md 4c's probe so the gap signal can tell a maintainer
    to draft a detector. A symlinked install (~/.claude/skills/... → the source tree)
    resolves into the git tree and correctly reads as source."""
    try:
        r = subprocess.run(
            ["git", "-C", str(_skill_root()), "ls-files", "--error-unmatch",
             "scripts/blocking_path.py"],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _gaps_root_default() -> Path | None:
    """Where gap captures root by DEFAULT — never under the installable skill dir.

    Returns None on an installed/vendored copy, so capture is SKIPPED there: end users never
    run the gap → catalog loop, and the `skills` installer copies `skills/<name>/` recursively
    excluding only {.git, __pycache__, __pypackages__} (vercel-labs/skills `src/installer.ts`) —
    no general dotfile exclusion — so a `.ci-speedup-gaps/` under the skill would ship MBs of
    third-party job logs to every user. In a tracked-source checkout it roots at the REPO ROOT
    (`git rev-parse --show-toplevel`), OUTSIDE `skills/<name>/`, kept out of the install surface
    and gitignored. The reader (`draft_detector.py` `_GAPS_ROOT`) anchors the same repo-root path."""
    if not _is_maintainer_source():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(_skill_root()), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()) / ".ci-speedup-gaps"
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    # Reached only when this IS a maintainer source checkout but the repo root didn't resolve
    # (git race / timeout / non-zero rc). Say so loudly — otherwise a maintainer's feedstock is
    # dropped indistinguishably from the intentional, silent installed-copy skip above.
    print("ci-speedup: gap capture skipped — maintainer source detected but could not resolve "
          "the repo root (`git rev-parse --show-toplevel`).", file=sys.stderr)
    return None


def _pole_for_entry(poles: list[dict[str, Any]],
                    entry: dict[str, Any]) -> dict[str, Any] | None:
    """The critical-path pole that a drill-bundle log entry belongs to, matched by the
    EXACT (check, workflow_file) the entry carries — never a substring borrow. Falls back
    to (check, workflow basename) when the pole stores a bare filename. None if no pole
    owns the entry (then the caller uses the entry itself)."""
    ck = str(entry.get("check") or "")
    wf = str(entry.get("workflow_file") or "")
    for p in poles:
        if str(p.get("check") or "") == ck and str(p.get("workflow_file") or "") == wf:
            return p
    wb = _wf_base(wf)
    for p in poles:
        if str(p.get("check") or "") == ck and _wf_base(str(p.get("workflow_file") or "")) == wb:
            return p
    return None


def _sole_owner_pole(key: str, poles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The single pole a `--log` KEY uniquely owns, mirroring `_match_key`'s exact-stem-
    then-substring rule but REFUSING an ambiguous borrow: a bare key like `Test` that is
    a substring of several pole names (`Test`, `Unit Tests`, `… vet, test …`) owns none of
    them. Used only in the no-`data_bundle` fallback; the primary path binds by drill-bundle
    entry so it never reaches this. None when the key matches no pole or borrows across
    several without an exact-stem tiebreak."""
    kl = key.lower()
    matches = [p for p in poles
               if kl in (str(p.get("check", "")) + " "
                         + _wf_base(str(p.get("workflow_file", "")))).lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        stem = [p for p in matches
                if key == _wf_base(str(p.get("workflow_file", ""))).split(".")[0]]
        return stem[0] if len(stem) == 1 else None
    return None


def _pole_owner_keys(doc: dict[str, Any], poles: list[dict[str, Any]],
                     supplied_keys: "set[str]") -> dict[int, str]:
    """Map `id(pole)` → the single `--log/--steps/--mag` KEY that pole OWNS, so the
    renderer binds each pole to ITS OWN drill — never a workflow-stem borrow.

    R1 fix: `_match_key`'s exact-stem rule made an UNDRILLED pole sharing a workflow with a
    drilled one (e.g. `test` beside a drilled `build`, both `ci.yml`) inherit the sibling's
    log/waterfall/evidence/prompt — a hard faithfulness violation that only surfaces once
    render depth exceeds the drill depth. Binding by IDENTITY (the drill-bundle entry's own
    `(check, workflow_file)` via `_pole_for_entry`, keyed exactly as `summary._render_keys`
    emits) means a pole that wasn't drilled owns no key → it renders honestly with no
    captured log instead of borrowing. With no drill bundle (a hand-run `--log` render) each
    supplied key binds to the pole it UNIQUELY owns (`_sole_owner_pole`), which likewise
    refuses an ambiguous cross-pole borrow."""
    out: dict[int, str] = {}
    entries = (doc.get("data_bundle") or {}).get("logs") or []
    if entries:
        try:
            from summary import _render_keys  # the SAME keys the render command emitted
            keys = _render_keys(entries)
        except Exception as e:  # noqa: BLE001 — degrade to the sole-owner fallback, but loudly
            keys = []
            print(f"ci-speedup: pole-key derivation failed ({type(e).__name__}: {e}); "
                  "falling back to the uniqueness-guarded sole-owner binding for pole logs.",
                  file=sys.stderr)
        if len(keys) == len(entries):
            for key, entry in zip(keys, entries):
                if "=" in key:
                    continue  # an un-bindable key (summary flags it) — never borrow
                p = _pole_for_entry(poles, entry)
                if p is not None:
                    out[id(p)] = key
            return out
        # Import succeeded but the key count didn't match the entries — degrade to the
        # uniqueness-guarded fallback, but say so (parity with the exception branch + _gap_poles;
        # the fallback is safe — it never mis-binds — but a silent drop to it shouldn't hide a
        # real derivation skew). S2.
        print(f"ci-speedup: pole-key count mismatch ({len(keys)} keys vs {len(entries)} "
              "drill entries); falling back to uniqueness-guarded sole-owner binding.",
              file=sys.stderr)
    # Fallback (no drill bundle, or the derivation above didn't bind): map each supplied key
    # to the pole it UNIQUELY owns. NOTE (S1): this binds ONE key per pole and reuses it for
    # every map (logs/steps/mags/analyses); on the generated `render_command` each pole has a
    # single consistent key so this is exact, but a hand-run that keys a pole's maps
    # differently gets only the first-resolved key (others render shallow) — a safe
    # degradation, never a cross-pole borrow. `supplied_keys` is a set, so on the rare
    # ambiguous-key case which key wins isn't ordered; `_sole_owner_pole` still refuses a
    # genuinely ambiguous (multi-pole) key, so it can never bind the WRONG pole.
    for key in supplied_keys:
        p = _sole_owner_pole(key, poles)
        if p is not None:
            out.setdefault(id(p), key)
    return out


def _gap_poles(doc: dict[str, Any],
               logs: dict[str, str]
               ) -> list[tuple[dict[str, Any], str, str, str]]:
    """The DRILLED poles whose OWN captured log matched NO catalog detector — the coverage
    gaps. Drives off the drill bundle (`data_bundle.logs`), i.e. exactly the poles the
    report renders, binding each pole to ITS OWN log by the collision-proof owner key that
    `summary._render_keys` emits (the same keys the render command used) — never a substring
    borrow. This is what stops a pole in another workflow from being flagged a gap just
    because it shares a token (`test`) with a drilled pole's log, and stops one log/analysis
    from being stamped onto several poles. Returns `[(pole, workflow_base, log_text, key), …]`
    where `key` is the pole's owning `--log/--analysis` key. The owner key is the one the
    render command (`summary._render_keys`) emits; on the SKILL.md happy path the agent runs
    that emitted command, so a pole the report shows as a gap binds here too. Falls back to a
    uniqueness-guarded `cp.poles` scan when there is no drill bundle (a hand-run render) OR
    when the render keys can't be reproduced 1:1 for the drill entries (import/derivation
    failure — surfaced loudly, never a silent under-count)."""
    cp = doc.get("pr_critical_path") or {}
    poles = list(cp.get("poles") or [])
    # A pole that a catalog detector already covers is NOT a coverage gap, even when its
    # OWN log matched no log-level (`_parse_log`) detector: a structural lever (OPT70–75)
    # OR a data-driven finding (e.g. OPT24) routed to it with a credited wall-clock saving
    # both mean the report already names a measured cause for it. Capturing such a pole and
    # telling a maintainer to "draft a NEW detector" (phase 4c) is the bug this guards
    # against — the render path suppresses the gap for these poles, so the capture must too.
    findings = _dedupe_findings(list(doc.get("findings") or []))

    def _catalog_covers(pole: dict[str, Any]) -> bool:
        return bool(_structural_for_pole(pole, findings)
                    or _data_driven_for_pole(pole, findings))

    out: list[tuple[dict[str, Any], str, str, str]] = []
    entries = (doc.get("data_bundle") or {}).get("logs") or []
    if entries:
        try:
            from summary import _render_keys  # the SAME keys the render command emitted
            keys = _render_keys(entries)
        except Exception as e:  # noqa: BLE001 — degrade to the fallback, but NEVER silently:
            # a swallowed failure here would quietly drop to the weaker substring matcher and
            # could under-report gaps (a false "clean") on the very pipeline this binding fix
            # exists to make trustworthy. Warn loudly so a real import/derivation break is seen.
            keys = []
            print(f"ci-speedup: gap-key derivation failed ({type(e).__name__}: {e}); "
                  "falling back to the uniqueness-guarded pole scan — ambiguous-key gaps may "
                  "be UNDER-REPORTED. Re-run with summary.py importable to restore "
                  "collision-proof binding.", file=sys.stderr)
        if len(keys) == len(entries):
            for key, entry in zip(keys, entries):
                if "=" in key:
                    continue  # an un-bindable key (summary flags it) — never borrow
                log_text = logs.get(key)
                if not log_text or _parse_log(log_text) is not None:
                    continue
                base = _pole_for_entry(poles, entry) or dict(entry)
                if _catalog_covers(base):
                    continue  # a catalog detector already covers it — not a gap
                pole = dict(base)
                if not pole.get("run_url") and entry.get("html_url"):
                    pole["run_url"] = entry["html_url"]
                wf_base = _wf_base(entry.get("workflow_file") or pole.get("workflow_file") or "")
                out.append((pole, wf_base, log_text, key))
            return out
    # Fallback (no drill bundle): bind each agent-supplied --log key to the pole it
    # UNIQUELY owns, so one log can't borrow across poles.
    named = [p for p in poles if p.get("check")]
    for key, log_text in logs.items():
        pole = _sole_owner_pole(key, named)
        if pole is None or not log_text or _parse_log(log_text) is not None:
            continue
        if _catalog_covers(pole):
            continue  # a catalog detector already covers it — not a gap
        wf_base = _wf_base(str(pole.get("workflow_file", "")))
        out.append((pole, wf_base, log_text, key))
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^\w.-]+", "-", str(s)).strip("-") or "x"


def _capture_gap(repo: str, pole: dict[str, Any], log_text: str,
                 analysis: dict[str, Any], doc: dict[str, Any],
                 gaps_root: Path) -> Path:
    """Persist one gap to `<gaps_root>/<repo-slug>__<job-slug>/`: the captured job log,
    the gap-fill analysis JSON, and a meta.json. This is the feedstock for the gap →
    catalog loop. Local-only by contract (job logs may carry repo internals/tokens —
    `.ci-speedup-gaps/` is gitignored and never committed)."""
    job = str(pole.get("job") or pole.get("check") or "job")
    dest = gaps_root / f"{_slug(repo)}__{_slug(job)}"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "job.log").write_text(log_text, encoding="utf-8")
    (dest / "analysis.json").write_text(
        json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ds = doc.get("data_sources") or {}
    meta = {
        "repo": repo,
        "workflow_file": pole.get("workflow_file"),
        "job": job,
        "dominant_step": pole.get("dominant_step"),
        "skill_commit_sha": ds.get("skill_commit_sha") or doc.get("skill_commit_sha"),
        # The fetch timestamp lives at top-level `scanned_at`; `captured_at`/`ds.scanned_at`
        # are usually unset (don't leave the feedstock's provenance NULL when it's known).
        "scanned_at": (doc.get("captured_at") or ds.get("scanned_at")
                       or doc.get("scanned_at")),
        # `pole.run_url` is threaded from the drill-bundle entry's `html_url` in `_gap_poles`.
        "run_url": pole.get("run_url") or ds.get("sampled_runs_created_before"),
    }
    (dest / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dest


def _emit_gap_signal(doc: dict[str, Any], logs: dict[str, str],
                     analyses: dict[str, dict[str, Any]],
                     gaps_root: Path | None = None) -> None:
    """Phase 4b + the loud signal. For every coverage-gap pole: when its gap-fill
    `--analysis` is attached (the re-render), capture it to the repo-root `.ci-speedup-gaps/`
    (resolved by `_gaps_root_default()` — None on an installed copy, which skips capture
    entirely); then print a machine-readable stderr banner naming the gaps,
    what was captured, and — in maintainer source context — the draft-a-detector
    pointer (4c). No-op when there are no gaps. Best-effort: a capture I/O error
    degrades to a warning, never fails the render."""
    gaps = _gap_poles(doc, logs)
    if not gaps:
        return
    if gaps_root is None:
        gaps_root = _gaps_root_default()
    if gaps_root is None:
        # Installed/vendored copy (not a tracked-source checkout): do NOT capture. End users
        # never run the gap → catalog loop, and rooting the capture under the skill would ship
        # it (the installer has no dotfile exclusion). The gap is still shown in the rendered
        # report's phase-4a analysis; only the maintainer-feedstock capture + stderr banner are
        # skipped here (that banner is loop machinery, never end-user output).
        return
    repo = str(doc.get("repo") or "repo")
    captured: list[Path] = []
    any_analysis = False   # was a gap-fill analysis attached (by its EXACT key) for ANY gap?
    capture_failed = False  # did an attached analysis fail to persist (OSError)?
    no_provenance: list[str] = []  # captured gaps whose meta has no scanned_at/run_url
    # A gap whose --analysis was attached under a LOOSE key that only fuzzy-matches it
    # (the historical footgun: keying `--analysis Test=…` stamps the bun analysis onto
    # every pole whose name contains `test`). Bind by the EXACT owner key only; collect
    # the mis-keyed ones to tell the agent the right key rather than silently mis-binding.
    mis_keyed: list[tuple[dict[str, Any], str]] = []
    for pole, wf_base, log_text, key in gaps:
        a = analyses.get(key)
        if not isinstance(a, dict):
            if isinstance(_match_key(analyses, wf_base, str(pole.get("check", ""))), dict):
                mis_keyed.append((pole, key))
            continue
        any_analysis = True
        try:
            dest = _capture_gap(repo, pole, log_text, a, doc, gaps_root)
            captured.append(dest)
            if not pole.get("run_url") and not (doc.get("captured_at")
                    or (doc.get("data_sources") or {}).get("scanned_at")
                    or doc.get("scanned_at")):
                no_provenance.append(str(pole.get("job") or pole.get("check")))
        except OSError as e:
            capture_failed = True
            print(f"ci-speedup: gap capture failed for "
                  f"{pole.get('job') or pole.get('check')}: {e}", file=sys.stderr)
    jobs = ", ".join(str(p.get("job") or p.get("check")) for p, _, _, _ in gaps)
    print(f"\n⚠ ci-speedup CATALOG GAP: {len(gaps)} drilled pole(s) matched no "
          f"detector: {jobs}.", file=sys.stderr)
    if captured:
        print(f"  captured {len(captured)} gap(s) for the gap → catalog loop:",
              file=sys.stderr)
        for d in captured:
            print(f"    {d}", file=sys.stderr)
        if no_provenance:
            print("  ⚠ no scanned_at/run_url provenance for: "
                  f"{', '.join(no_provenance)} (meta.json fields are null).",
                  file=sys.stderr)
    elif any_analysis and capture_failed:
        # The analysis WAS attached — the failure is the I/O above, not a missing
        # flag. Don't tell the maintainer to attach what they already attached.
        print("  the gap-fill analysis was attached but capture to "
              "`.ci-speedup-gaps/` failed (see the error(s) above) — fix the path/"
              "permissions and re-render to capture for the catalog loop.",
              file=sys.stderr)
    else:
        print("  attach each gap pole's gap-fill analysis on re-render, keyed EXACTLY "
              "to the pole (SKILL.md phase 4a/4b):", file=sys.stderr)
        for pole, _wf, _log, key in gaps:
            print(f"    --analysis {shlex.quote(key + '=PATH')}"
                  f"   # {pole.get('job') or pole.get('check')}", file=sys.stderr)
    if mis_keyed:
        # Loud, never silent: a fuzzy-keyed analysis would bind to the wrong pole(s).
        print(f"  ⚠ skipped {len(mis_keyed)} gap(s): the --analysis KEY did not EXACTLY "
              "own the pole (a loose substring can bind one analysis to several poles). "
              "Re-attach with the exact owner key:", file=sys.stderr)
        for pole, key in mis_keyed:
            print(f"    --analysis {shlex.quote(key + '=PATH')}"
                  f"   # {pole.get('job') or pole.get('check')}", file=sys.stderr)
    if _is_maintainer_source():
        if captured:
            # Imperative + the exact command incl. the captured slugs, so 4c is a concrete
            # next step the agent runs — not a passive pointer it reads past (SKILL.md 4c).
            slugs = " ".join(shlex.quote(d.name) for d in captured)
            print("  MAINTAINER (tracked source) — REQUIRED NEXT ACTION (SKILL.md phase 4c): "
                  "promote these gaps to deterministic catalog detectors before closing. Run:",
                  file=sys.stderr)
            print(f"    python3 maintainers/ci-speedup/scripts/draft_detector.py prepare {slugs}", file=sys.stderr)
            print("  hand its output to a background subagent to draft a `_parse_log` "
                  "detector + `_FIX_META` + test, then `draft_detector.py verify <slugs>` "
                  "gates it, then ask the maintainer once (maintainers/ci-speedup/MAINTAINERS.md).", file=sys.stderr)
        else:
            print("  MAINTAINER (tracked source): once the gap-fill analyses are attached and "
                  "captured (above), promote them to detectors — `python3 "
                  "maintainers/ci-speedup/scripts/draft_detector.py prepare` "
                  "(SKILL.md phase 4c / maintainers/ci-speedup/MAINTAINERS.md).",
                  file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="in_path", required=True, type=Path)
    ap.add_argument("--out", dest="out_path", type=Path)
    ap.add_argument("--log", dest="logs", action="append", default=[],
                    metavar="KEY=PATH",
                    help="Captured raw log of a workflow's blocking job, keyed by a "
                         "substring of the workflow filename (e.g. e2e=prisma.log).")
    ap.add_argument("--sample", dest="samples", action="append", default=[],
                    metavar="KEY=PATH",
                    help="JSON {summary, runs:[{url,note}]} cross-run sample proving "
                         "the root cause is representative, keyed like --log.")
    ap.add_argument("--log-run", dest="log_runs", action="append", default=[],
                    metavar="KEY=URL",
                    help="The GitHub run URL the --log for KEY came from (the median "
                         "representative run), shown as the drill-down's source.")
    ap.add_argument("--steps", dest="steps", action="append", default=[],
                    metavar="KEY=PATH",
                    help="JSON {job, run_url, job_dur_s, steps:[{name,start_s,dur_s}]} "
                         "of the representative run's per-step timeline (execution "
                         "order + offsets), keyed like --log. Renders the step level "
                         "as a sequential timeline instead of P50 bars.")
    ap.add_argument("--mag", dest="mags", action="append", default=[],
                    metavar="KEY=PATH",
                    help="JSON {label, unit, this_run, values:[{run_url,value}]} - the "
                         "load-bearing magnitude (e.g. migration share) sampled across "
                         "several runs, keyed like --log. Renders the Cross-run check "
                         "(median + range) so the single drilled run isn't trusted alone.")
    ap.add_argument("--analysis", dest="analyses", action="append", default=[],
                    metavar="KEY=PATH",
                    help="JSON {cause, breakdown:[[label,detail]], evidence:[log lines], "
                         "prompt} - an LLM gap-fill for a pole with NO catalog match, "
                         "keyed like --log. The agent running the skill writes this from "
                         "the captured log (grounded in cited lines); it renders as a "
                         "clearly-labelled LLM analysis + tailored prompt instead of a "
                         "coverage-gap dead-end. See SKILL.md's gap-fill phase.")
    ap.add_argument("--captured-at", dest="captured_at", default="",
                    help="Date the --log/--sample data was fetched (provenance note).")
    args = ap.parse_args(argv)

    def _split(spec: str, flag: str) -> tuple[str, str] | None:
        # A malformed spec (missing KEY=) silently producing an empty report is a
        # footgun - warn loudly instead of ignoring it.
        if "=" not in spec:
            print(f"{flag}: ignoring malformed spec {spec!r} (expected KEY=...)",
                  file=sys.stderr)
            return None
        k, v = spec.split("=", 1)
        return k, v

    try:
        doc = json.loads(args.in_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"--in: cannot read findings JSON {args.in_path}: {e}", file=sys.stderr)
        return 1
    logs: dict[str, str] = {}
    for spec in args.logs:
        kv = _split(spec, "--log")
        if kv is None:
            continue
        try:
            logs[kv[0]] = Path(kv[1]).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"--log: cannot read {kv[1]}: {e}", file=sys.stderr)
            return 1
    samples: dict[str, dict[str, Any]] = {}
    for spec in args.samples:
        kv = _split(spec, "--sample")
        if kv is None:
            continue
        try:
            samples[kv[0]] = json.loads(Path(kv[1]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"--sample: cannot read/parse {kv[1]}: {e}", file=sys.stderr)
            return 1
    log_runs: dict[str, str] = {}
    for spec in args.log_runs:
        kv = _split(spec, "--log-run")
        if kv is not None:
            log_runs[kv[0]] = kv[1]
    steps: dict[str, dict[str, Any]] = {}
    for spec in args.steps:
        kv = _split(spec, "--steps")
        if kv is None:
            continue
        try:
            steps[kv[0]] = json.loads(Path(kv[1]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"--steps: cannot read/parse {kv[1]}: {e}", file=sys.stderr)
            return 1
    mags: dict[str, dict[str, Any]] = {}
    for spec in args.mags:
        kv = _split(spec, "--mag")
        if kv is None:
            continue
        try:
            mags[kv[0]] = json.loads(Path(kv[1]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"--mag: cannot read/parse {kv[1]}: {e}", file=sys.stderr)
            return 1
    analyses: dict[str, dict[str, Any]] = {}
    for spec in args.analyses:
        kv = _split(spec, "--analysis")
        if kv is None:
            continue
        try:
            analyses[kv[0]] = json.loads(Path(kv[1]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"--analysis: cannot read/parse {kv[1]}: {e}", file=sys.stderr)
            return 1
    md = render(doc, logs, samples, log_runs, args.captured_at, steps, mags,
                analyses=analyses)
    if args.out_path:
        args.out_path.write_text(md + "\n", encoding="utf-8")
        # Claims layer manifest, written alongside the report (never in place of it - the
        # report is the artifact; the manifest is a machine-readable companion the gate can
        # compare fields against instead of parsing prose). Always written on a new render
        # with a known output path; its absence is how the gate recognizes an OLD artifact
        # (rendered before this manifest existed) and falls back to text parsing for it.
        _claims_path = args.out_path.parent / (args.out_path.name + ".claims.json")
        _claims_path.write_text(
            json.dumps(_LAST_CLAIMS.to_json() if _LAST_CLAIMS else
                       {"claims": [], "families_migrated": []}, indent=2) + "\n",
            encoding="utf-8")
    else:
        sys.stdout.write(md + "\n")
    # Phase 4b/4c: deterministically capture any coverage-gap pole (when its --analysis
    # gap-fill is attached) and emit the loud catalog-gap signal. Rides on the render the
    # agent always runs, so the capture can't be skipped the way the old prose step was.
    # Strictly best-effort: the report is already written above, so a signal/capture
    # failure degrades to a warning rather than failing the render (its contract).
    try:
        _emit_gap_signal(doc, logs, analyses)
    except Exception as e:  # noqa: BLE001 — never let the gap signal fail a written report
        print(f"ci-speedup: gap signal failed (report already written): {e}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

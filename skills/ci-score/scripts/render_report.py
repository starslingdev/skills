"""ci-score report renderer (B2, OD-L5) — card → RANKED recommendations →
kickoff handoff prompts.

Renders the full report from a collected findings.json: the score card
(`render_card._render_score_card`, stamp-only), the adherence-not-speed
disclosure beside it, then one recommendation per FAILED check, **ranked by
impact × risk** from `_FIX_TABLE` below, each carrying a concrete fix recipe,
the matching best-practices page, and a ready-to-paste agent handoff prompt
grounded in the findings document (capture-once: evidence, files, and commit
are quoted so the fixing agent re-derives nothing).

THE RANKING LIVES HERE, NOT IN THE REGISTRY (OD-L5): impact/risk tiers are
presentation of fixes, not scoring — the frozen `ci-score-spec.json` is never
edited and no number on the card is affected by anything in this module.
Practice-page links come FROM the registry's `practice_slug` binding; the fix
table may supply a slug only where the registry's is null because the page
shipped after the v0.1.1 freeze (exactly one today: bound-job-timeouts).

Ranking rule (deterministic): impact high→medium, then risk low→medium→high
(cheapest-safest first within an impact tier), then registry order.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_SPEC = _SCRIPT_DIR.parent / "references" / "ci-score-spec.json"
_PRACTICE_BASE = "https://starsling.dev/best-practices/github-actions"

# The one sentence that must sit beside the card on EVERY scored surface
# (same requirement as the website profile pages). verify_report asserts its
# literal presence; edit it only in lockstep there.
DISCLOSURE = ("This grade measures configuration adherence to CI best "
              "practices; it does not predict CI speed: in our measured "
              "calibration, faster repos can hold lower grades.")

_IMPACT_ORDER = {"high": 0, "medium": 1}
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Per-check fix metadata: impact tier (what it saves/protects), risk tier
# (what the CHANGE could break — stated honestly in risk_note), a concrete
# recipe, and optionally a slug for registry-null checks whose page shipped
# after the freeze. Never consulted for scoring.
_FIX_TABLE: dict[str, dict[str, str]] = {
    "ci.trigger.cancel-superseded": {
        "tldr": "When you push again, the old run keeps burning paid minutes on code that no longer exists - this kills it the moment it's obsolete.",
        "impact": "high", "risk": "low",
        "impact_note": "superseded runs burn runner minutes on every rapid push - impact scales with push frequency; on a low-traffic repo this is cheap hygiene, not meaningful savings",
        "risk_note": "trivial: cancelling an obsolete run cannot break a fresh one",
        "recipe": ("add to each PR workflow:\n"
                   "concurrency:\n"
                   "  group: ${{ github.workflow }}-${{ github.ref }}\n"
                   "  cancel-in-progress: true"),
    },
    "ci.trigger.concurrency-groups": {
        "tldr": "Without a group, every push while CI is busy just stacks another full run on the pile - you wait longer and pay for runs nobody will read.",
        "impact": "high", "risk": "low",
        "impact_note": "bounds self-inflicted queue time when pushes stack up - impact scales with push frequency; on a low-traffic repo this is cheap hygiene, not meaningful savings",
        "risk_note": "trivial: a group alone changes scheduling, never results",
        "recipe": ("add to each PR workflow:\n"
                   "concurrency:\n"
                   "  group: ${{ github.workflow }}-${{ github.ref }}"),
    },
    "ci.cache.dependency-cache": {
        "tldr": "Every run re-downloads all your dependencies from scratch - caching reuses the last install instead of fetching them again.",
        "impact": "high", "risk": "low",
        "impact_note": "dependency installs re-download on every run without it",
        "risk_note": "low: a stale cache is keyed by lockfile hash and self-heals",
        "recipe": ("this check is not_applicable, never a fail, only when the\n"
                   "repo installs no dependencies at all - so a fail means it\n"
                   "DOES install them and simply caches none. If a manifest/\n"
                   "lockfile exists (package-lock.json, pnpm-lock.yaml,\n"
                   "requirements*.txt, poetry.lock, ...), wire the cache to it:\n"
                   "enable the setup action's cache input (e.g.\n"
                   "- uses: actions/setup-node@<sha>\n"
                   "  with: {cache: npm}\n"
                   ") or add an actions/cache step keyed on the lockfile. If the\n"
                   "deps are installed inline with no manifest, add the manifest\n"
                   "first, then key the cache on it"),
    },
    "ci.security.pinned-action-shas": {
        "tldr": "A tag like @v4 can be silently repointed by whoever owns the action - pinning the exact commit means CI only ever runs code you chose.",
        "impact": "high", "risk": "low",
        "impact_note": "unpinned actions are a supply-chain door: a moved tag runs someone else's code with your secrets",
        "risk_note": "low and mechanical: pin refs to full commit SHAs; Renovate/Dependabot keep them fresh",
        "recipe": ("replace every `uses: owner/action@vN` with the full 40-char\n"
                   "commit SHA (keep a `# vN` comment), workflows AND local\n"
                   "composite actions. Resolving a tag to its SHA needs a\n"
                   "network lookup (`git ls-remote <action-repo> <tag>`) -\n"
                   "NEVER guess or fabricate a SHA; without network access,\n"
                   "stop and hand the lookup to the user. Enabling\n"
                   "Renovate/Dependabot is a follow-up suggestion, not part\n"
                   "of this edit"),
    },
    "ci.security.scoped-id-token": {
        "tldr": "A workflow-wide cloud credential hands every job the keys - scoping gives them only to the one job that actually deploys.",
        "impact": "high", "risk": "low",
        "impact_note": "a workflow-wide id-token grant hands OIDC credentials to every job, not just the one that needs them",
        "risk_note": "low: moving the grant to the one job that uses it changes nothing else",
        "recipe": ("delete workflow-level `permissions: id-token: write` (or\n"
                   "write-all) and grant it on the specific job:\n"
                   "jobs:\n  deploy:\n    permissions:\n      id-token: write"),
    },
    "ci.cache.build-cache": {
        "tldr": "CI rebuilds work that didn't change - a build cache reuses the previous build instead of redoing it.",
        "impact": "high", "risk": "medium",
        "impact_note": "rebuilding unchanged targets is usually the largest avoidable build cost - pays off when builds are long; trivial builds gain nothing",
        "risk_note": "medium: cache keys and remote-cache auth need care; a wrong key serves stale artifacts",
        "recipe": ("wire the build tool's own cache (turbo: TURBO_TOKEN remote\n"
                   "cache; nx: nx-cloud; gradle: setup-gradle's cache; bazel:\n"
                   "a remote cache) into the CI workflow. Worth wiring only when\n"
                   "the build is slow enough to notice - a trivial build gains\n"
                   "nothing from a cache"),
    },
    "ci.parallel.test-sharding": {
        "tldr": "One long test job sets the floor for every PR - splitting it across N runners runs the slices in parallel instead of one after another.",
        "impact": "high", "risk": "medium",
        "impact_note": "the test job's wall-clock divides by the shard count - but this pays off when the test job runs long (the measured catalog's threshold was five-plus minutes); on a quick suite the per-shard setup overhead makes CI slower and costs more - skip it unless tests are what you wait on",
        "risk_note": "medium: sharding surfaces hidden test inter-dependencies and needs a result-merge step",
        "recipe": ("ONLY IF the test runner supports sharding (verify the\n"
                   "flag exists first; if it doesn't, this fix does not\n"
                   "apply), and ONLY IF tests aren't already distributed by\n"
                   "a mechanism this check cannot see (Nx Cloud agents,\n"
                   "external CI) - adding a shard matrix on top of those is\n"
                   "wrong, and ONLY IF the suite is long enough to beat the\n"
                   "per-shard setup overhead (the measured catalog used a\n"
                   "five-plus-minute test job as the bar) - on a quick suite\n"
                   "sharding makes CI slower and costs more: run the test job\n"
                   "as a matrix over shards:\n"
                   "strategy:\n  matrix:\n    shard: [1, 2, 3, 4]\n"
                   "and pass the shard to the runner\n"
                   "(e.g. `--shard=${{ matrix.shard }}/4`). A passing\n"
                   "re-score proves the config shape, not that the runner\n"
                   "accepts the flag - never ship a flag CI can't run"),
    },
    "ci.build.change-scoped": {
        "tldr": "A docs typo shouldn't rebuild and retest the world - scope CI to what the change actually touched.",
        "impact": "high", "risk": "medium",
        "impact_note": "unscoped CI rebuilds and retests the whole graph for every change - pays off in large graphs; small repos rebuild everything quickly anyway",
        "risk_note": "medium: a wrong filter can SKIP work that should run; verify against a base-branch diff",
        "recipe": ("use the task graph's affected mode on PRs (turbo:\n"
                   "`--filter=...[origin/main]`; nx: `nx affected`) or gate\n"
                   "jobs on a changed-files step. Affected mode needs a\n"
                   "merge-base with the base branch: keep enough fetch depth\n"
                   "on these jobs - do not combine with the shallow-clone\n"
                   "fix on the same workflow. Worth it in a large task graph;\n"
                   "a small repo rebuilds everything quickly anyway"),
    },
    "ci.checkout.shallow-clone": {
        "tldr": "CI downloads your repo's entire history when it only needs today's code.",
        "impact": "medium", "risk": "low",
        "impact_note": "full-history clones tax every PR checkout; big repos pay minutes",
        "risk_note": "low: only jobs that genuinely read history (changelogs, blame) need depth — keep it on those jobs only",
        "recipe": ("remove `fetch-depth: 0` from checkout steps on PR-gating\n"
                   "workflows (the default depth is 1); keep full depth only\n"
                   "on jobs that need history"),
    },
    "ci.hygiene.job-timeouts": {
        "tldr": "A hung job bills the full 6-hour GitHub default before dying - a timeout caps the damage at minutes.",
        "impact": "medium", "risk": "low",
        "impact_note": "without timeout-minutes a hung job bills the default 360 minutes",
        "risk_note": "low: set generously (2-3x normal runtime); too tight kills legitimate slow runs",
        "recipe": ("set on every job:\n"
                   "jobs:\n  test:\n    timeout-minutes: 20\n"
                   "(pick 2-3x the job's normal runtime)"),
        "slug": "bound-job-timeouts",  # page shipped after the v0.1.1 freeze
    },
    "ci.trigger.path-filter": {
        "tldr": "Workflows run even for changes that can't possibly affect them - filters skip CI that has nothing to check.",
        "impact": "medium", "risk": "medium",
        "impact_note": "docs-only and unrelated changes run the full pipeline without filters",
        "risk_note": "medium: a wrong filter can skip CI that SHOULD run, and a skipped required check blocks merges — mind required-check interplay",
        "recipe": ("scope PR workflows with paths/paths-ignore:\n"
                   "on:\n  pull_request:\n    paths-ignore:\n"
                   "      - '**.md'\n      - 'docs/**'"),
    },
}


# Per-check external SOURCES — the SINGLE source of truth for the vendor
# documentation links (check_id -> [(label, url), ...]). Both the report's
# "What each check means" appendix AND the methodology explainers render from
# THIS dict (a census test asserts the methodology's Sources lines match it),
# so the two surfaces can never diverge. Every URL is curl-verified live
# (HTTP 200 after redirects) before commit; an unverifiable link is omitted,
# never shipped.
_CHECK_SOURCES: dict[str, list[tuple[str, str]]] = {
    "ci.cache.dependency-cache": [
        ("GitHub Actions — Caching dependencies",
         "https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching"),
        ("actions/cache", "https://github.com/actions/cache"),
    ],
    "ci.cache.build-cache": [
        ("Turborepo — Remote caching", "https://turbo.build/repo/docs/core-concepts/remote-caching"),
        ("Nx — Remote cache", "https://nx.dev/ci/features/remote-cache"),
        ("Gradle — Build cache", "https://docs.gradle.org/current/userguide/build_cache.html"),
    ],
    "ci.checkout.shallow-clone": [
        ("actions/checkout — fetch-depth", "https://github.com/actions/checkout"),
    ],
    "ci.parallel.test-sharding": [
        ("Playwright — Sharding", "https://playwright.dev/docs/test-sharding"),
        ("Jest — --shard", "https://jestjs.io/docs/cli#--shard"),
        ("pytest-xdist — Distribution modes", "https://pytest-xdist.readthedocs.io/en/stable/distribution.html"),
    ],
    "ci.build.change-scoped": [
        ("Nx — Affected", "https://nx.dev/ci/features/affected"),
        ("Turborepo — Running tasks", "https://turbo.build/repo/docs/crafting-your-repository/running-tasks"),
    ],
    "ci.trigger.concurrency-groups": [
        ("GitHub Actions — Control workflow concurrency",
         "https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency"),
    ],
    "ci.trigger.cancel-superseded": [
        ("GitHub Actions — Control workflow concurrency",
         "https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency"),
    ],
    "ci.trigger.path-filter": [
        ("GitHub Actions — Workflow syntax (`paths` / `paths-ignore`)",
         "https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax"),
    ],
    "ci.hygiene.job-timeouts": [
        ("GitHub Actions — Workflow syntax (`jobs.<job_id>.timeout-minutes`)",
         "https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax"),
    ],
    "ci.security.scoped-id-token": [
        ("GitHub Actions — About security hardening with OpenID Connect",
         "https://docs.github.com/en/actions/concepts/security/openid-connect"),
        ("GitHub Actions — Secure use reference",
         "https://docs.github.com/en/actions/reference/security/secure-use"),
    ],
    "ci.security.pinned-action-shas": [
        ("GitHub Actions — Secure use reference (using third-party actions)",
         "https://docs.github.com/en/actions/reference/security/secure-use"),
    ],
}


def render_sources_line(check_id: str) -> str:
    """The `**Sources:** ...` line for a check, rendered from `_CHECK_SOURCES`
    (the single source of truth shared by the report appendix and the
    methodology explainers). Empty string when the check has no sources."""
    srcs = _CHECK_SOURCES.get(check_id) or []
    if not srcs:
        return ""
    return "**Sources:** " + ", ".join(f"[{label}]({url})" for label, url in srcs)


def _load_sibling(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _render_appendix(registry: dict[str, Any]) -> list[str]:
    """The self-contained "What each check means" appendix — one subsection per
    registry check, generated from the registry + fix table + `_CHECK_SOURCES`
    at render time (never hand-authored per report). The card's check-name
    links are IN-DOCUMENT anchors that resolve to these `### <label>` headings,
    so the report depends on no external file (owner, 2026-07-28: the earlier
    methodology-file links broke in common viewers). The heading text is the
    registry label; GitHub derives the same slug the card's _anchor() emits."""
    lines: list[str] = ["## What each check means", ""]
    for check in registry["checks"]:
        cid = check["check_id"]
        meta = _FIX_TABLE.get(cid, {})
        lines += [f"### {check.get('label') or cid}", ""]
        lines += [f"**The check:** {check.get('fact', '').strip()}", ""]
        na = str(check.get("not_applicable_when") or "").strip()
        if na:
            lines += [f"**Not applicable when:** {na}", ""]
        if meta.get("tldr"):
            lines += [f"**Why it matters:** {meta['tldr']}", ""]
        url = _practice_url(cid, registry)
        # a guide link only where a practice page actually backs the check
        reg_slug = next((c.get("practice_slug") for c in registry["checks"]
                         if c["check_id"] == cid), None)
        if reg_slug or _FIX_TABLE.get(cid, {}).get("slug"):
            lines += [f"**Guide:** {url}", ""]
        src = render_sources_line(cid)
        if src:
            lines += [src, ""]
    return lines


def _practice_url(check_id: str, registry: dict) -> str:
    """The registry's `practice_slug` binding governs; the fix table may
    supply one ONLY where the registry's is null (page postdates the freeze);
    otherwise the hub."""
    reg_slug = next((c.get("practice_slug") for c in registry["checks"]
                     if c["check_id"] == check_id), None)
    slug = reg_slug or _FIX_TABLE.get(check_id, {}).get("slug")
    return f"{_PRACTICE_BASE}/{slug}" if slug else _PRACTICE_BASE


# Tie-break among equal impact+risk: the stake KIND decides — security,
# then reliability (hygiene), then speed/cost — instead of arbitrary
# registry position (owner, 2026-07-30: two high/low findings tied and the
# security one ranked below queue hygiene purely by registry order).
# Impact and risk still dominate; this is presentation (OD-L5), never scoring.
_FAMILY_ORDER = {"security": 0, "hygiene": 1}


def _rank_key(check_id: str, registry_order: dict[str, int]):
    meta = _FIX_TABLE.get(check_id, {})
    family = check_id.split(".")[1] if check_id.count(".") >= 2 else ""
    return (_IMPACT_ORDER.get(meta.get("impact"), 9),
            _RISK_ORDER.get(meta.get("risk"), 9),
            _FAMILY_ORDER.get(family, 2),
            registry_order.get(check_id, 99))


def _handoff_prompt(rec_no: int, chk: dict, meta: dict, url: str,
                    doc: dict) -> list[str]:
    """The paste-able kickoff prompt — grounded in THIS findings document
    (capture-once): evidence, offender files, and commit are quoted so the
    fixing agent re-derives nothing. The offender list is capped at three
    examples (`_practice_facts` truncates `files`), so the re-scored check —
    not the list — is the completion oracle: done only when it reads pass."""
    files = chk.get("files") or []
    files_line = ", ".join(files) if files else "(locate via .github/workflows/)"
    return [
        "```text",
        f"Fix one CI best-practice gap in this repository (ci-score "
        f"recommendation #{rec_no}: {chk.get('label')}).",
        f"Repo state when scored: commit {doc.get('commit_sha', '?')}.",
        f"Finding: {chk.get('evidence')}",
        f"Example files (up to three; the Finding above states the full "
        f"scope): {files_line}",
        f"Task: {meta.get('recipe', '').strip()}",
        f"Reference: {url}",
        "Constraints: apply the fix everywhere the practice is missing — the "
        "files listed are up to three examples, so more offenders may exist; "
        "the Finding above states the full scope. Change nothing else; "
        "preserve workflow behavior apart from the practice being added; do "
        "not reformat unrelated YAML. Then re-run "
        "`python3 <ci-score>/scripts/collect_config.py --repo . --out "
        "/tmp/rescore.json`: the re-scored check is the oracle — you are done "
        "only when it reads pass (if it still fails, offenders remain — fix "
        "them and re-run).",
        "```",
    ]


def _render_header(doc: dict[str, Any]) -> list[str]:
    """Title + provenance table, ci-speedup-house-style: `# <repo> — how does
    your CI configuration score?` over a metadata table naming exactly what
    was scored (repo, commit incl. any -dirty marker, workflow count, rubric
    version, run date). Every cell is read off the findings document — the
    header states provenance, it never computes or restates the score (the
    gauge and card own that, one line below)."""
    slug = doc.get("repo_slug")
    root = str(doc.get("repo_root", "?"))
    name = slug or (root.rstrip("/").rsplit("/", 1)[-1] or root)
    commit = str(doc.get("commit_sha", "?"))
    dirty = commit.endswith("-dirty")
    base_sha = commit[: -len("-dirty")] if dirty else commit
    short = base_sha[:7]
    real_sha = bool(base_sha) and base_sha != "?"

    if slug:
        repo_cell = f"[`{slug}`](https://github.com/{slug}) — local checkout at `{root}`"
        # Link the commit only when there is a real SHA to link — a checkout
        # with a remote but no resolvable HEAD would otherwise emit a broken
        # `/commit/?` URL presented as real provenance.
        commit_cell = (f"[`{short}`](https://github.com/{slug}/commit/{base_sha})"
                       if real_sha else f"`{short}`")
    else:
        # No linkable slug — could be no remote, or a remote whose URL form
        # wasn't recognised. Don't assert a remote is absent; just say we
        # aren't linking.
        repo_cell = f"`{root}` (local checkout — no linked GitHub remote)"
        commit_cell = f"`{short}`"
    if dirty:
        commit_cell += (" — **tree was dirty**: uncommitted or untracked local "
                        "changes present, so the scored bytes may not match this commit")

    lines = [f"# {name} — how does your CI configuration score?", "",
             f"| Repository | {repo_cell} |",
             "| :--- | :--- |",
             f"| **Scored commit** | {commit_cell} |"]
    if "scanned_workflows" in doc:
        n = doc.get("scanned_workflows")
        lines += [f"| **Workflows scanned** | {n} workflow file(s) under `.github/workflows/` |"]
    stamp = doc.get("ci_score")
    if isinstance(stamp, dict) and stamp.get("spec_version"):
        n_checks = len(stamp.get("checks") or [])
        rubric = f"`{stamp['spec_version']}`"
        if n_checks:
            rubric += f" · {n_checks} pass/fail configuration checks"
        lines += [f"| **Rubric** | {rubric} |"]
    generated = str(doc.get("generated_at", ""))
    if generated:
        lines += [f"| **Scored** | {generated[:10]} (UTC) · local checkout only — no CI runs fetched |"]
    return lines + [""]


def render_report(doc: dict[str, Any], registry: dict[str, Any]) -> str:
    """The full report, from the findings document ONLY (single-stamp rule:
    the card renders the stamp; recommendations rank the stamp's FAILs;
    nothing recomputes a number)."""
    rc_mod = _load_sibling("ci_score_render_card", "render_card.py")
    lines: list[str] = []
    lines += _render_header(doc)

    refusal_only = "collection_refusal" in doc
    if refusal_only:
        lines += [f"**Not scored:** {doc['collection_refusal']['human_reason']}", ""]
        return "\n".join(lines) + "\n"

    lines += rc_mod._render_score_card(doc)
    stamp = doc.get("ci_score")
    if isinstance(stamp, dict) and not stamp.get("refusal"):
        lines += [f"> {DISCLOSURE}", ""]

    checks = (stamp or {}).get("checks") if isinstance(stamp, dict) else None
    fails = [c for c in (checks or []) if isinstance(c, dict)
             and c.get("state") == "fail"]
    if not isinstance(stamp, dict) or stamp.get("refusal") or checks is None:
        # a refusal card still links every check name → append the appendix so
        # those in-document anchors resolve; an error/no-checks card links none.
        if isinstance(checks, list) and checks:
            lines += _render_appendix(registry)
        return "\n".join(lines) + "\n"

    if not fails:
        lines += ["## Recommendations", "",
                  "Every applicable check passes — nothing to recommend. "
                  "(Re-run after CI changes; the practices above are the "
                  "full v0.1.3 rubric.)", ""]
        lines += _render_appendix(registry)
        return "\n".join(lines) + "\n"

    registry_order = {c["check_id"]: i for i, c in enumerate(registry["checks"])}
    ranked = sorted(fails, key=lambda c: _rank_key(str(c.get("check_id")), registry_order))

    lines += ["## Recommendations — ranked by impact × risk", "",
              "Highest-impact, lowest-risk first. Each carries a concrete fix "
              "and a paste-able agent prompt; risk notes state plainly what "
              "the change could break.", "",
              "> If this repo's primary CI runs outside GitHub Actions (e.g. "
              "Buildkite, Jenkins) or distributes work via Nx Cloud agents, "
              "some failing checks may reflect what this scan cannot see — "
              "weigh each recommendation against mechanisms that live outside "
              "the workflow YAML.", ""]
    for i, chk in enumerate(ranked, 1):
        cid = str(chk.get("check_id"))
        meta = _FIX_TABLE.get(cid, {})
        url = _practice_url(cid, registry)
        note = chk.get("measured_note")
        lines += [f"### {i}. {chk.get('label')} — impact: "
                  f"{meta.get('impact', '?')}, risk: {meta.get('risk', '?')}", ""]
        if meta.get("tldr"):
            lines += [f"**{meta['tldr']}**", ""]
        lines += [f"- **Why:** {meta.get('impact_note', '')}"
                  + (f" ({note})" if note else "")]
        lines += [f"- **Risk of the change:** {meta.get('risk_note', '')}"]
        lines += [f"- **Finding:** {chk.get('evidence')}"]
        files = chk.get("files") or []
        if files:
            lines += ["- **Files:** " + ", ".join(f"`{f}`" for f in files)]
        lines += [f"- **Guide:** {url}", "", "**Fix:**", "", "```yaml"]
        lines += [meta.get("recipe", "(no recipe)")]
        lines += ["```", "", "<details><summary>Agent handoff prompt</summary>", ""]
        lines += _handoff_prompt(i, chk, meta, url, doc)
        lines += ["", "</details>", ""]
    lines += _render_appendix(registry)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ci-score: render the report")
    parser.add_argument("--findings", default="findings.json")
    parser.add_argument("--out", default="report.md")
    parser.add_argument("--spec", default=str(_DEFAULT_SPEC), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    doc = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    registry = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    Path(args.out).write_text(render_report(doc, registry), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

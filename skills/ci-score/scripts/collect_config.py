"""ci-score entry point (B1) — collect a repo's CI configuration and stamp
the CI Score, offline, from a local checkout only.

Flow: verify the target is a git checkout → read + parse
`.github/workflows/*.yml|yaml` → `_practice_facts(parsed, root)` (which itself
walks local composite actions and the repo-root build-tool config probes) →
`compute_ci_score` → write `findings.json` carrying the `ci_score` stamp.

LOCAL-CHECKOUT-ONLY (OD-L2, owner-decided): the scorer's input surface is
three classes — workflow YAML, `.github/actions/**` composite actions, and
repo-ROOT build-tool configs. A partial fetch (e.g. workflows-only via an API)
silently turns failing checks `not_applicable` and INFLATES the grade, so this
collector refuses politely outside a full checkout rather than guessing. No
network, ever: everything is read from the local tree; the offline contract
cell runs this module with sockets booby-trapped.

Honest outcomes, all stamped (never a fabricated grade):
- no workflow files      → the stamp itself carries the spec's `no_workflow_yaml`
                           refusal (a refusal is a *result*, exit 0).
- not a git checkout     → `collection_refusal` written to the output document,
                           no `ci_score` stamp, exit 2.
- files but none parse   → `collection_refusal` (reason `no_parseable_workflows`),
                           no `ci_score` stamp, exit 2. Computing facts from zero
                           readable documents would assert false "absent" facts
                           (e.g. "no job sets timeout-minutes" when we merely
                           couldn't read the file) — a deflated grade, refused.
- scoring raised         → `data_sources.ci_score_error` recorded, no partial
                           stamp, exit 3.

Exit 2 is the "collection refusal" class: no scoreable input was obtained, so no
stamp is written; the `collection_refusal.reason_code` disambiguates which gate
refused (`not_a_git_checkout` vs `no_parseable_workflows`).

Provenance records the FULL-REPO head commit SHA (the score reads files
outside the workflow tree — root configs, composite actions), suffixed
`-dirty` when the working tree differs from HEAD. Published profiles forbid
`-dirty` (Wave-1 rule); this collector records it honestly either way.

DEBUG logging follows the `STARSLING_LOG_LEVEL` convention. Debug lines record
counts, file *names*, and states — never file contents.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover — surfaced loudly if missing
    print("ERROR: PyYAML is required (`pip install pyyaml`)", file=sys.stderr)
    sys.exit(1)

logger = logging.getLogger("ci-score.collect")

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_SPEC = _SCRIPT_DIR.parent / "references" / "ci-score-spec.json"


def _load_sibling(mod_name: str, filename: str):
    """Load a sibling script by FILE PATH: sibling skills also ship
    modules with generic names on the shared pythonpath, so a bare `import`
    could bind the wrong skill's module. File-path loading pins to this one."""
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(root: Path, *args: str) -> str | None:
    """One local git plumbing call; None on any failure (git absent, not a
    repo). Local-only — git never touches the network for these."""
    try:
        out = subprocess.run(["git", "-C", str(root), *args],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _repo_slug(root: Path) -> str | None:
    """`owner/repo` parsed from the local `remote.origin.url` config, or None
    when there is no origin or the URL is not recognisably a github.com remote.
    Read locally (git config) — never a network call. Display-only provenance:
    the report header uses it to name and link the repository; nothing scores
    off it. Recognises every common GitHub URL shape — https/ssh/git schemes,
    the scp `user@github.com:owner/repo` form with any user (not just `git`),
    an optional port, an optional `.git`/trailing slash — case-insensitively.
    A non-github host (gitlab, a `github.com.evil.com` look-alike, an
    unrecognised form) yields None, so the header never fabricates a link."""
    url = _git(root, "config", "--get", "remote.origin.url")
    if not url:
        return None
    m = re.match(
        r"(?:[^/@]+@github\.com:|(?:https?|ssh|git)://(?:[^/@]+@)?github\.com(?::\d+)?/)"
        r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url, re.IGNORECASE)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _provenance(root: Path) -> str | None:
    """`<full-repo-head-sha>` or `<sha>-dirty`; None when not a git checkout.
    The SHA is the whole repo's HEAD, not the workflow tree's — the score
    reads files outside `.github/workflows/` (root configs, composites).

    "dirty" = `git status --porcelain` is non-empty, which INCLUDES untracked
    files (the honest, conservative choice: any working-tree difference from
    HEAD means the scored bytes may not match this SHA). Note this also flags
    the collector's own default `findings.json` output if it is written into
    the target repo and left untracked — gitignore it (or write --out
    elsewhere) when the -dirty-forbidding published profile matters."""
    sha = _git(root, "rev-parse", "HEAD")
    if sha is None:
        return None
    status = _git(root, "status", "--porcelain")
    # `""` = verified clean; non-empty = dirty; `None` = git status failed/timed
    # out so the tree state is UNKNOWABLE — never certify clean from ignorance
    # (a published profile that forbids -dirty must reject an unverifiable tree,
    # not accept it as clean). Unknown errs to -dirty, the safe direction.
    dirty = (status != "")
    return f"{sha}-dirty" if dirty else sha


def _read_workflows(root: Path) -> tuple[list[tuple[str, dict, str]], list[str], list[str]]:
    """(parsed, workflow_files, parse_errors). `workflow_files` counts every
    on-disk workflow file (that count drives the no-workflows refusal
    honestly); `parsed` carries only the mapping-shaped docs the facts can
    walk; unparseable files land in `parse_errors` by name, never silently."""
    wf_dir = root / ".github" / "workflows"
    files: list[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        files.extend(wf_dir.glob(pattern))
    files = sorted(files)
    parsed: list[tuple[str, dict, str]] = []
    errors: list[str] = []
    for path in files:
        rel = str(path.relative_to(root))
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            doc = yaml.safe_load(raw)
        except (OSError, yaml.YAMLError) as exc:
            logger.debug("workflow parse failed: %s (%s)", rel, type(exc).__name__)
            errors.append(rel)
            continue
        if isinstance(doc, dict):
            parsed.append((rel, doc, raw))
        else:
            logger.debug("workflow not a mapping, skipped: %s", rel)
            errors.append(rel)
    logger.debug("workflows: %d file(s), %d parsed, %d errored",
                 len(files), len(parsed), len(errors))
    return parsed, [str(p.relative_to(root)) for p in files], errors


def collect(root: Path, spec_path: Path = _DEFAULT_SPEC) -> tuple[dict[str, Any], int]:
    """Collect + score one checkout. Returns (document, exit_code). Pure with
    respect to the network; reads only the local tree and the spec file."""
    root = root.resolve()
    # Normalize to the repo's TOP LEVEL. The score reads the full-repo surface
    # (root build-tool configs, `.github/workflows`, composite actions). If
    # --repo points at a SUBDIRECTORY of a checkout, git plumbing still succeeds
    # there but a subdir-relative scan sees a PARTIAL view and inflates the
    # grade — the exact OD-L2 failure mode this collector exists to refuse.
    # Anchoring on the top level closes that hole; None ⇒ not a checkout, which
    # the provenance gate below turns into the honest refusal.
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if toplevel is not None:
        root = Path(toplevel).resolve()

    doc: dict[str, Any] = {
        "generator": "ci-score/collect_config.py",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_root": str(root),
        "data_sources": {"mode": "local-checkout",
                         "spec_path": str(spec_path)},
    }

    # Sparse checkouts are partial views WITH a clean status: git's sparse
    # mode deliberately hides the excluded files, so neither the -dirty flag
    # nor any file probe can tell absence-in-repo from absence-in-view. A
    # missing root config or composite dir would silently inflate the grade
    # (checks fall to not_applicable) — the exact OD-L2 failure class. Refuse.
    # --type=bool: git normalizes every truthy spelling (true/yes/on/1) to
    # "true", so a hand-set non-canonical value can't slip past this literal.
    if _git(root, "config", "--type=bool", "--get", "core.sparseCheckout") == "true":
        doc["collection_refusal"] = {
            "reason_code": "sparse_checkout",
            "human_reason": ("Not scored: this checkout uses git sparse-checkout, "
                             "so parts of the tree are hidden and the score's "
                             "input surface cannot be verified complete. Run "
                             "`git sparse-checkout disable` (or use a full "
                             "clone) and re-run."),
        }
        logger.debug("refused: sparse checkout at %s", root)
        return doc, 2

    commit = _provenance(root)
    slug = _repo_slug(root)
    if slug is not None:
        doc["repo_slug"] = slug
    if commit is None:
        # Not a git checkout: refuse politely (OD-L2), stamp what happened,
        # and DON'T guess — a partial or checkout-less read inflates grades.
        doc["collection_refusal"] = {
            "reason_code": "not_a_git_checkout",
            "human_reason": ("Not scored: this path is not a git checkout. "
                             "ci-score scores only full local checkouts — "
                             "clone the repository and run it from inside "
                             "(a partial view silently inflates the grade)."),
        }
        logger.debug("refused: not a git checkout: %s", root)
        return doc, 2
    doc["commit_sha"] = commit

    parsed, workflow_files, parse_errors = _read_workflows(root)
    doc["scanned_workflows"] = len(workflow_files)
    doc["workflow_files"] = workflow_files
    if parse_errors:
        doc["data_sources"]["workflow_parse_errors"] = parse_errors

    if workflow_files and not parsed:
        # Files exist but NONE parsed: every practice fact would be computed
        # from zero readable documents and assert a false "absent" (e.g. "no
        # job sets timeout-minutes" when we simply couldn't read the file),
        # producing a deflated grade with fabricated evidence — this design's
        # worst outcome. Refuse like the not-a-checkout gate: no facts, no
        # stamp, no guessed grade. (scanned_workflows > 0, so the spec's
        # no_workflow_yaml refusal cannot fire and must not be borrowed.)
        doc["collection_refusal"] = {
            "reason_code": "no_parseable_workflows",
            "human_reason": (
                f"Not scored: found {len(workflow_files)} workflow file(s) but "
                "none could be parsed as YAML (see "
                "data_sources.workflow_parse_errors). Scoring from zero readable "
                "workflows would assert false 'absent' facts, so ci-score "
                "refuses rather than publish a deflated grade."),
        }
        logger.debug("refused: %d workflow file(s), none parseable",
                     len(workflow_files))
        return doc, 2

    pf_mod = _load_sibling("ci_score_practice_facts", "practice_facts.py")
    cs_mod = _load_sibling("ci_score_ci_score", "ci_score.py")

    doc["practice_facts"] = pf_mod._practice_facts(parsed, root)
    if logger.isEnabledFor(logging.DEBUG):
        states = {k: v.get("state") for k, v in doc["practice_facts"].items()}
        logger.debug("practice facts: %s", states)

    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        # OD-CS20: a repo whose workflows do NO project build or test (only
        # bots, releases, triage) is refused rather than given an absurd-but-
        # honest number. Computed INSIDE the scoring try (from the parsed
        # workflows), so any failure in the predicate becomes an honest
        # ci_score_error marker, never a silently-dropped signal; compute_ci_score
        # turns the flag into the refusal (a refusal is a result, exit 0).
        doc["automation_only"] = pf_mod._automation_only(parsed, root)
        logger.debug("automation_only=%s", doc["automation_only"])
        stamp = cs_mod.compute_ci_score(doc, spec)
    except Exception as exc:  # scoring failure → marker, never a partial stamp
        doc["data_sources"]["ci_score_error"] = f"{type(exc).__name__}: {exc}"
        logger.debug("scoring failed: %s", doc["data_sources"]["ci_score_error"])
        return doc, 3
    doc["ci_score"] = stamp
    logger.debug("stamped: value=%s grade=%s refusal=%s",
                 stamp.get("value"), stamp.get("grade"),
                 (stamp.get("refusal") or {}).get("reason_code"))
    return doc, 0


def _summary_line(doc: dict[str, Any]) -> str:
    """One honest line for stdout — mirrors the close contract: grade from the
    stamp, refusal reason verbatim, error stated; never a fabricated grade."""
    if "collection_refusal" in doc:
        return doc["collection_refusal"]["human_reason"]
    err = doc.get("data_sources", {}).get("ci_score_error")
    if err:
        return f"CI Score unavailable this run (scoring failed: {err})"
    stamp = doc.get("ci_score") or {}
    refusal = stamp.get("refusal")
    if refusal:
        return refusal["human_reason"]
    commit = doc.get("commit_sha", "?")
    # Truncate the hex, never the honesty: the -dirty suffix must survive.
    short = (commit[:12] + "-dirty") if commit.endswith("-dirty") else commit[:12]
    return (f"CI Score: {stamp.get('value')}/100 — "
            f"{stamp.get('checks_passed')}/{stamp.get('checks_applicable')} "
            f"applicable checks passed @ {short}")


def _banner_lines(doc: dict[str, Any]) -> list[str]:
    """The close-banner box, PRE-DRAWN from the stamp so the agent never draws
    it freehand (a live dogfood mis-drew the 30-block bar by hand, 2026-07-29).
    The bar's filled count uses the SAME integer round-half-up as the card
    gauge — `(value * 30 + 50) // 100` — over 30 blocks. Every border row is
    the same width (the box widens to fit a long slug). Returns [] for any
    non-scored document (collection refusal, scoring error, or stamp refusal):
    those keep their plain-sentence close, no banner."""
    if "collection_refusal" in doc or (doc.get("data_sources") or {}).get("ci_score_error"):
        return []
    stamp = doc.get("ci_score")
    if not isinstance(stamp, dict) or stamp.get("refusal"):
        return []
    value = stamp.get("value")
    if not isinstance(value, int):
        return []
    passed = stamp.get("checks_passed")
    applicable = stamp.get("checks_applicable")
    if not isinstance(passed, int) or not isinstance(applicable, int):
        # A malformed stamp (value present, tallies missing) must not print a
        # contradictory "0 pass · 0 fail" box under a filled bar — no banner.
        return []
    total = len(stamp.get("checks") or [])
    fails = applicable - passed
    na = total - applicable

    filled = (value * 30 + 50) // 100  # round-half-up, same math as the card gauge
    bar = "█" * filled + "░" * (30 - filled)

    commit = str(doc.get("commit_sha", "?"))
    dirty = commit.endswith("-dirty")
    base = commit[: -len("-dirty")] if dirty else commit
    short = base[:7] + ("-dirty" if dirty else "")  # keep the -dirty honesty
    slug = doc.get("repo_slug")
    root = str(doc.get("repo_root", "?"))
    name = slug or (root.rstrip("/").rsplit("/", 1)[-1] or root)

    vstr = f"{value} / 100"
    left_rows = [f"  {bar}",
                 f"  {passed} pass · {fails} fail · {na} not applicable",
                 f"  {name} @ {short}"]
    # Interior width: at least the SKILL.md example's 44, and always wide enough
    # for the widest content row and the "CI SCORE … {value} / 100" line —
    # every content row keeps a 2-space right margin (a long slug widens the
    # box rather than touching the border).
    w = max([44, len("  CI SCORE") + 2 + len(vstr) + 2] + [len(r) + 2 for r in left_rows])

    def row(s: str) -> str:
        return "│" + s + " " * (w - len(s)) + "│"

    gap = w - len("  CI SCORE") - len(vstr) - 2  # 2 trailing spaces before the border
    value_row = "│" + "  CI SCORE" + " " * gap + vstr + "  " + "│"
    return (["┌" + "─" * w + "┐", value_row]
            + [row(r) for r in left_rows]
            + ["└" + "─" * w + "┘"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ci-score: score a local checkout's CI configuration")
    parser.add_argument("--repo", default=".",
                        help="path to the target repo's local checkout (default: .)")
    parser.add_argument("--out", default="findings.json",
                        help="output document path (default: ./findings.json)")
    parser.add_argument("--spec", default=str(_DEFAULT_SPEC),
                        help=argparse.SUPPRESS)  # test hook: alternate registry
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("STARSLING_LOG_LEVEL", "WARNING").upper(),
        format="%(levelname)s %(name)s: %(message)s")

    doc, code = collect(Path(args.repo), Path(args.spec))
    out = Path(args.out)
    out.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n",
                   encoding="utf-8")
    print(_summary_line(doc))
    for line in _banner_lines(doc):  # pre-drawn close banner, copied verbatim
        print(line)
    print(f"wrote {out}")
    return code


if __name__ == "__main__":
    sys.exit(main())

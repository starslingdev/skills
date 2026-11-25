#!/usr/bin/env python3
"""One-shot driver for ci-speedup's deterministic phases.

Runs the scripted phases — scan → gh data (collect_runs) — in a SINGLE process
and writes ONE findings JSON. The orchestrator used to run these as separate
steps in separate shells; that leaked a large amount of
between-step overhead (the agent re-reads context and "thinks" between each
tool call) and a fistful of scratch / pointer / per-script-stderr files just to
carry state across the shells. Collapsing them here removes both.

It also makes end-to-end timing reliable WITHOUT an orchestrator-managed stamp
file (which proved fragile — a run skipped it). `scan.py` (this driver's first
step) records the run's start epoch into the findings `timings` block; this
driver adds its own `scripted_*` spans; the orchestrator closes the loop with
`record_timing.py` once rendering is done. The scripts own the timing, not the
agent's memory.

ci-speedup does NOT generate fixes: detection + measured root-cause analysis is
fully deterministic (these phases), and `blocking_path.py` renders each pole with
a ready-to-paste agent prompt the user hands to their own coding agent. There is
no fix-subagent phase. After this driver, the orchestrator just renders with
`blocking_path.py`.

Usage:
    run.py --root REPO --out FINDINGS.json [--repo owner/repo]
           [--target N] [--with-logs] [--catalog PATH]
           [--accept-request-estimate]
           [--account-weekly-series PATH]
           [--skill-commit-sha SHA] [--commit-sha SHA]
    # writes FINDINGS.json; then render it with `blocking_path.py --in FINDINGS.json`.
    # scan stderr flows straight through, so coverage warnings are visible.
    # Provenance (skill/commit sha) is auto-derived from git when not passed, so
    # the findings JSON never records a NULL sha that blanks the report's
    # `Audited commit` row or its skill-commit footer. For an INSTALLED skill copy
    # (no git repo) the skill provenance falls back to the skills-CLI lockfile's
    # content hash as a distinct `installed:<hash12>` form (issue #2) — never NULL,
    # never a guessed remote sha.

Exit codes: 0 ok; 2 invalid --repo value (scan.py's code); 1 any other failure
— scan non-zero (including no .github/workflows dir), unparseable/empty
findings, or a sub-step failed — a coverage failure the orchestrator must
surface and stop on, never render over.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DIR))
from summary import build_summary  # noqa: E402  (sibling module; needs _DIR on path)
from before_after import (  # noqa: E402
    advance_verification,
    GhVerificationClient,
    summarize_account_weekly,
)
from pr_sample_contract import pr_sample_goal_text  # noqa: E402
from installed_skill import has_extra_files, latest_release  # noqa: E402


def _git_short_sha(repo_dir: Path, short: bool = True) -> str | None:
    """HEAD sha of a git tree, or None if `repo_dir` isn't a git checkout / git is
    unavailable. Used to derive provenance so a run never silently records a NULL
    skill/commit sha (the gap that left a worked example with a blank `Audited
    commit` and an unverifiable skill-commit footer). `short=False` returns the full
    40-char sha for the analyzed-commit permalink (the report truncates it for
    display but links the full sha)."""
    args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
    try:
        r = subprocess.run(["git", "-C", str(repo_dir), *args],
                           capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and sha else None


def _skill_checkout_sha(scripts_dir: Path) -> str | None:
    """Use a commit only for the source checkout's tracked skill entry point."""
    try:
        tracked = subprocess.run(
            ["git", "-C", str(scripts_dir), "ls-files", "--full-name",
             "--error-unmatch", "--", "run.py"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if tracked.returncode == 0 and tracked.stdout.strip() == "skills/ci-speedup/scripts/run.py":
        return _git_short_sha(scripts_dir)
    return None


# The terminal provenance form: an installed skill copy with no git repo AND no
# resolvable lockfile entry. Honest and distinct — NOT a NULL, NOT a guessed sha.
_INSTALLED_UNVERSIONED = "installed:unversioned"


def _skill_lock_provenance(skill_root: Path) -> str:
    """Provenance for an INSTALLED skill copy (no git repo).

    The skills CLI installs `skills/<name>/` as a recursive copy with NO `.git`,
    so `_git_short_sha` returns None. Recording that NULL blanks the report's
    skill-commit footer and FAILS `verify_report` (`check_skill_commit_provenance`:
    "no `skill commit` recorded"), which historically pushed the driving agent to
    re-run the entire gh data pass just to pass a GUESSED `--skill-commit-sha`
    (issue #2). The installer's lockfile (`.skill-lock.json`, a sibling of the
    installed skill dirs) records a `skillFolderHash` (content hash) but NO commit
    sha, so the honest provenance is that folder hash stamped as a DISTINCT
    `installed:` form. Returns `installed:<hash12>` when the lockfile carries this
    skill's entry, else the terminal `installed:unversioned` — never a NULL, never
    a remote-tip guess. NEVER raises (a provenance probe must not fail the run)."""
    name = skill_root.name  # the installed dir basename, e.g. "ci-speedup"
    # The skills CLI has moved the lockfile between versions: earlier CLIs wrote it
    # as a SIBLING of the installed skill dirs (`<skill_root>/../.skill-lock.json`),
    # the current CLI writes it one level higher, at the skill root's GRANDPARENT
    # (`<skill_root>/../../.skill-lock.json`, e.g. `~/.agents/.skill-lock.json` for
    # `~/.agents/skills/ci-speedup`) — issue #91. So probe a BOUNDED upward walk of
    # three levels, NEAREST first: the skill root itself (defensive), then parent,
    # then grandparent. Stop at the FIRST lockfile that both parses AND carries a
    # matching entry; a parseable lockfile with NO matching entry does NOT stop the
    # walk (a higher-level lockfile may hold the entry). NEVER walk above the third
    # level. Nearest-first means the closest lockfile to the skill wins a tie.
    for lock in (skill_root / ".skill-lock.json",
                 skill_root.parent / ".skill-lock.json",
                 skill_root.parent.parent / ".skill-lock.json"):
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        # ValueError covers JSONDecodeError AND a non-UTF-8 lockfile's
        # UnicodeDecodeError (both subclass ValueError); OSError covers a
        # missing/unreadable/dir path. A corrupt lockfile must degrade to
        # `unversioned`, never raise out and fail the run (issue #2 premise).
        except (OSError, ValueError):
            continue
        skills = data.get("skills") if isinstance(data, dict) else None
        if not isinstance(skills, dict):
            continue
        entry = skills.get(name)
        # Fall back to the sole entry when the install dir was renamed and so no
        # longer keys it; an ambiguous multi-entry miss stays unversioned.
        if not isinstance(entry, dict) and len(skills) == 1:
            entry = next(iter(skills.values()))
        if isinstance(entry, dict):
            h = str(entry.get("skillFolderHash") or "")
            hex12 = "".join(c for c in h.lower() if c in "0123456789abcdef")[:12]
            if len(hex12) == 12:
                return f"installed:{hex12}"
        # No matching/valid entry at this level — keep walking upward: a
        # higher-level lockfile may still carry this skill's entry (issue #91).
    return _INSTALLED_UNVERSIONED


# A hung child (e.g. a wedged `gh` process inside collect_runs that outlives its
# own per-call timeout) must not block the orchestrator forever. Each step gets a
# generous wall-clock ceiling; on timeout we synthesize a non-zero result so the
# caller's `returncode != 0` guard trips and we stop cleanly instead of hanging.
_STEP_TIMEOUT_S = 900


class _RequestEstimate(NamedTuple):
    """Cheap preflight estimate, kept separate from the measured final count.

    `request_*` are core-quota JSON requests (the preflight probes included),
    `log_*` core-quota job-log downloads. `*_ceiling` is every conditional detector
    read firing on every workflow (see `request_estimate.py`)."""

    request_low: int
    request_high: int
    log_low: int
    log_high: int
    remaining: int
    reset_epoch: int
    probe_requests: int
    request_ceiling: int = 0
    log_ceiling: int = 0
    limit: int = 0
    graphql_high: int = 0
    graphql_remaining: int | None = None
    search_high: int = 0


class _RequestProbe:
    """Count every attempted preflight request, including a failing final probe."""

    def __init__(self) -> None:
        self.requests = 0

    def json(self, endpoint: str) -> dict:
        self.requests += 1
        return _gh_json(endpoint)


def _gh_json(endpoint: str) -> dict:
    """Read one audited-repository endpoint through the authenticated gh client."""
    result = subprocess.run(
        ["gh", "api", endpoint], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh api failed for {endpoint}")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError(f"gh api returned non-object JSON for {endpoint}")
    return value


def _estimate_data_pass(
        repo: str, *, with_logs: bool, target: int = 10,
        probe: _RequestProbe | None = None,
        root: Path | None = None) -> _RequestEstimate:
    """Estimate the bulk pass from four cheap, bounded probes.

    Every request family the collector issues is priced in `request_estimate.py`;
    job logs count even without `--with-logs`, because several detectors read a
    bounded number of logs on every run."""
    import request_estimate  # local: imports the collector's constants
    probe = probe or _RequestProbe()
    workflows = probe.json(f"repos/{repo}/actions/workflows?per_page=100")
    runs = probe.json(f"repos/{repo}/actions/runs?per_page=10")
    recent = runs.get("workflow_runs") if isinstance(runs.get("workflow_runs"), list) else []
    run_id = next((r.get("id") for r in recent if isinstance(r, dict) and r.get("id")), None)
    jobs = (probe.json(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
            if run_id is not None else {"total_count": 0, "jobs": []})
    rate = probe.json("rate_limit")

    facts = request_estimate.facts_from_probe(
        workflows, runs, jobs, root=root, with_logs=with_logs)
    totals = request_estimate.estimate(facts)
    probe_requests = probe.requests
    resources = rate.get("resources") or {}
    core = resources.get("core") or {}
    graphql = resources.get("graphql") or {}
    graphql_remaining = graphql.get("remaining")
    return _RequestEstimate(
        request_low=probe_requests + totals.json_low,
        request_high=probe_requests + totals.json_high,
        log_low=totals.log_low, log_high=totals.log_high,
        remaining=max(0, int(core.get("remaining") or 0)),
        reset_epoch=max(0, int(core.get("reset") or 0)),
        probe_requests=probe_requests,
        request_ceiling=probe_requests + totals.json_ceiling,
        log_ceiling=totals.log_ceiling,
        limit=max(0, int(core.get("limit") or 0)),
        graphql_high=totals.graphql_high,
        graphql_remaining=(int(graphql_remaining)
                           if isinstance(graphql_remaining, int) else None),
        search_high=totals.search_high,
    )


def _confirm_request_budget(
        estimate: _RequestEstimate, *, explicitly_accepted: bool = False) -> bool:
    """Require explicit consent before a pass may spend over half the quota.

    Gates on the expected upper figure only. The ceiling (every conditional read
    firing on every workflow) is printed and recorded, never gated on: it is not
    a realistic figure, and stopping on it would stop most mid-size scans."""
    quota_high = estimate.request_high + estimate.log_high
    if quota_high <= estimate.remaining / 2 or explicitly_accepted:
        return True
    # An agent or redirected run must never hang on input(). Its rerun command
    # carries the explicit acceptance flag instead.
    if not sys.stdin.isatty():
        return False
    relation = ("exceeds the remaining quota" if quota_high > estimate.remaining
                else "exceeds half the remaining quota")
    try:
        answer = input(
            f"The estimated data pass (up to {quota_high} API calls: "
            f"{estimate.request_low}-{estimate.request_high} JSON requests and "
            f"{estimate.log_low}-{estimate.log_high} log downloads) "
            f"{relation} ({estimate.remaining} remain). Continue? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _estimate_line(e: _RequestEstimate) -> str:
    """The one pre-collection line: estimate, ceiling, and what is left."""
    reset = (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(e.reset_epoch))
             if e.reset_epoch else "unknown")
    left = (f"{e.remaining} of {e.limit}" if e.limit else f"{e.remaining}")
    line = (
        "Data-pass request estimate: "
        f"{e.request_low}-{e.request_high} GitHub API requests; "
        f"{e.log_low}-{e.log_high} log downloads "
        f"(ceiling {e.request_ceiling + e.log_ceiling} if every conditional read fires); "
        f"{left} requests remain (reset {reset}).")
    if e.graphql_high or e.search_high:
        gql_left = (f", {e.graphql_remaining} remain"
                    if e.graphql_remaining is not None else "")
        line += (f" Separate quotas: up to {e.graphql_high} GraphQL "
                 f"quer{'y' if e.graphql_high == 1 else 'ies'}{gql_left}; "
                 f"up to {e.search_high} search request(s).")
    return line


def _accepted_estimate_command(argv: list[str] | None) -> str:
    """Exact command that repeats this run with the quota estimate accepted."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--accept-request-estimate" not in raw:
        raw.append("--accept-request-estimate")
    return shlex.join([sys.executable, str(_DIR / "run.py"), *raw])


def _step(cmd: list[str], *, capture_stdout: bool) -> subprocess.CompletedProcess[str]:
    """Run a sub-step. stderr always inherits (flows to our stderr → the
    orchestrator sees scan warnings etc. inline, so no per-script stderr file is
    needed). Returns the completed process; caller checks returncode. A step that
    exceeds `_STEP_TIMEOUT_S` is reported as a non-zero exit, not a traceback."""
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE if capture_stdout else None,
            text=True,
            timeout=_STEP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        print(
            f"ERROR: step timed out after {_STEP_TIMEOUT_S}s: {' '.join(cmd)}",
            file=sys.stderr,
        )
        return subprocess.CompletedProcess(cmd, returncode=124,
                                           stdout=e.stdout or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run ci-speedup's scan → gh data phases "
        "in one process and write one findings JSON.",
    )
    parser.add_argument("--root", required=True, help="Repo root to scan.")
    parser.add_argument("--out", required=True, type=Path, help="Findings JSON path.")
    parser.add_argument(
        "--report-out", type=Path, default=None,
        help="Where the printed render command writes the SANITIZED report .md. "
             "Defaults to an INTERNAL/session path beside --out "
             "(ci-speedup-findings-report.md in the scratch --out dir) — NOT the "
             "working tree. The report is rendered and verify-gated there on EVERY "
             "run; it is surfaced into the user's working directory only when they "
             "opt in at the phase-6 close ('save the full report', issue #18). The "
             "raw findings JSON + .data bundle also stay at --out (scratch). Pass an "
             "explicit path to override.")
    parser.add_argument("--repo", default=None, help="owner/repo for gh data-driven findings.")
    parser.add_argument("--target", type=int, default=10,
                        help="Render target (orchestration knob). NOTE: does NOT change "
                             "sampling DEPTH — the gh pass always runs, the PR gate sample "
                             f"aims for {pr_sample_goal_text()} and the "
                             "run sample stays at collect_runs' "
                             "--max-runs. Raising it will not recover an empty spine "
                             "(issue #81).")
    parser.add_argument("--with-logs", action="store_true",
                        help="Also run collect_runs' Tier-3 logs deep dive.")
    parser.add_argument(
        "--accept-request-estimate", action="store_true",
        help="Proceed when the printed upper request estimate exceeds half of "
             "the remaining GitHub quota (required for non-interactive runs). "
             "The printed ceiling is context only and never stops a run.")
    parser.add_argument("--bmf", action="store_true",
                        help="Show wall-clock-only recommendations that add runner "
                             "minutes or use an unpriced larger runner.")
    parser.add_argument("--minute-budget", type=float, default=5.0,
                        help="Maximum added runner-minutes as a percentage of measured "
                             "monthly runner minutes (default: 5; 0 restores strict "
                             "holdback).")
    parser.add_argument("--catalog", default=None, help="Override catalog path.")
    parser.add_argument(
        "--account-weekly-series", type=Path, default=None,
        help="Optional JSON account export with weekly runner-hours, job counts, "
             "merged-PR counts, workload-change notes, and explicit paired before/"
             "after run minutes (at least 5 per side for a comparison). The skill normalizes and renders it; it never "
             "extrapolates one repository into an account total.")
    parser.add_argument("--skill-commit-sha", default=None,
                        help="Skill provenance sha for the report footer/catalog "
                             "links; auto-derived from this skill's own git HEAD "
                             "when omitted, or from the skills-CLI lockfile "
                             "(`installed:<hash12>`) for an installed copy.")
    parser.add_argument("--commit-sha", default=None,
                        help="Analyzed-tree sha for the report's `Audited commit` "
                             "row; auto-derived from --root's git HEAD when omitted.")
    parser.add_argument(
        "--verify-branch", default=None,
        help="After an authorized push, resume post-fix verification for this "
             "remote branch. The branch is resolved to an exact head SHA.")
    parser.add_argument(
        "--verify-context", type=Path, default=None,
        help="Scratch context for resumable post-fix verification. Defaults beside --out.")
    parser.add_argument(
        "--verify-changed-file", action="append", default=[],
        help="A file changed by the fix; repeat for every changed path.")
    parser.add_argument(
        "--verify-cache-sensitive", action="store_true",
        help="Use three distinct default-branch pushes instead of warm reruns.")
    args = parser.parse_args(argv)
    if not math.isfinite(args.minute_budget) or args.minute_budget < 0:
        parser.error("--minute-budget must be a finite non-negative percentage")

    skill_root = _DIR.parent
    skill_checkout_sha = _skill_checkout_sha(_DIR)
    if skill_checkout_sha is None and has_extra_files(skill_root):
        print("WARNING: files from an older version of the skill are present; "
              "remove and re-add the skill to upgrade", file=sys.stderr)

    # Create the output directory up front. Without this, a fresh `--out
    # /tmp/run/findings.json` whose parent doesn't exist tracebacks on the first
    # write (the partial-file write below) instead of just working — a papercut that
    # forces the caller to mkdir and retry.
    args.out.parent.mkdir(parents=True, exist_ok=True)

    # Validate the optional account export before any scan, gh collection or
    # verification rerun, so a malformed file never costs API reads or CI runs
    # and never discards a verification result that already started.
    account_weekly = None
    if args.account_weekly_series is not None:
        try:
            raw_weekly = json.loads(
                args.account_weekly_series.read_text(encoding="utf-8"))
            account_weekly = summarize_account_weekly(raw_weekly)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(f"ERROR: account weekly series is invalid ({e}); stopping.",
                  file=sys.stderr)
            return 1

    # Price the bulk data pass only after every local input is valid, so a
    # malformed account export never costs the user preflight API reads.
    request_estimate: _RequestEstimate | None = None
    request_probe = _RequestProbe()
    offline_replay = bool(os.environ.get("CI_SPEEDUP_GH_FIXTURES"))
    if args.repo and not args.verify_branch and not offline_replay:
        try:
            request_estimate = _estimate_data_pass(
                args.repo, with_logs=args.with_logs, target=args.target,
                probe=request_probe, root=Path(args.root))
        except (OSError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
            # A failed cheap probe must not turn an otherwise usable collector into a
            # static-only run. Collection keeps its existing loud partial-state path.
            print(f"Data-pass request estimate unavailable ({e}).", file=sys.stderr)
        else:
            print(_estimate_line(request_estimate), flush=True)
            if not _confirm_request_budget(
                    request_estimate,
                    explicitly_accepted=args.accept_request_estimate):
                print("Data pass not started. Omit --with-logs or wait for the "
                      "quota reset, or explicitly accept this estimate.",
                      file=sys.stderr)
                print("Rerun exactly: " + _accepted_estimate_command(argv),
                      file=sys.stderr)
                return 1

    # Verification resumes the artifact produced before the fix.  Do this
    # before scan/collect so the historical baseline is never replaced with a
    # fresh post-push collection.
    if args.verify_branch:
        if not args.repo:
            print("ERROR: --verify-branch requires --repo.", file=sys.stderr)
            return 2
        if not args.out.is_file():
            print("ERROR: --verify-branch requires the original findings file "
                  f"at --out ({args.out}).", file=sys.stderr)
            return 2
        context_path = (args.verify_context
                        or args.out.with_name(args.out.stem + ".verification.json"))
        result = advance_verification(
            GhVerificationClient(),
            repo=args.repo,
            branch=args.verify_branch,
            baseline_artifact=str(args.out),
            context_path=context_path,
            changed_files=list(args.verify_changed_file),
            cache_sensitive=bool(args.verify_cache_sensitive),
        )
        if account_weekly is not None:
            result["account_weekly"] = account_weekly
        print("Post-fix verification: "
              + json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0

    # Provenance: forward explicit values, else derive from git so a run never
    # records a NULL skill/commit sha. scan.py only stamps these when non-empty.
    # Skill provenance is a short sha (matches the catalog-permalink convention); the
    # analyzed-commit is the FULL sha so the report's `Audited commit` link is a stable
    # permalink (the renderer truncates it to 7 chars for display).
    # Skill provenance: explicit wins; else the skill's own git HEAD; else — for an
    # INSTALLED copy with no git repo — the skills-CLI lockfile's content hash as a
    # distinct `installed:` form. Never NULL, never a guessed remote sha (issue #2).
    # `_DIR` is `<skill_root>/scripts`, so `_DIR.parent` is the installed skill root.
    skill_commit_sha = (args.skill_commit_sha or skill_checkout_sha
                        or _skill_lock_provenance(skill_root))
    skill_release = latest_release(skill_root) if skill_commit_sha.startswith("installed:") else None
    commit_sha = args.commit_sha or _git_short_sha(Path(args.root), short=False)
    # NB the audited commit stays a CLEAN sha here. Uncommitted workflow edits are a real
    # provenance skew (collect_runs parses the working tree; the timings come from runs of
    # the committed branch) — but they are disclosed by the `workflows_tree_dirty` flag
    # collect_runs stamps, NOT by mangling the sha. Appending `-dirty` to the sha itself
    # would silently 404 the report's `Audited commit` permalink, and the renderer
    # truncates the sha to 7 chars for display, so the marker would never be seen anyway.

    run_start = time.time()

    # 1. Scan. stdout = findings JSON; stderr flows through. A non-zero exit is
    #    a coverage failure — propagate it, never write a partial findings file.
    scan_cmd = [sys.executable, str(_DIR / "scan.py"), "--root", args.root]
    if args.repo:
        scan_cmd += ["--repo", args.repo]
    if args.catalog:
        scan_cmd += ["--catalog", args.catalog]
    if skill_commit_sha:
        scan_cmd += ["--skill-commit-sha", skill_commit_sha]
    if commit_sha:
        scan_cmd += ["--commit-sha", commit_sha]
    scan = _step(scan_cmd, capture_stdout=True)
    if scan.returncode != 0:
        print(
            f"ERROR: scan.py exited {scan.returncode} — coverage failure, not a "
            f"clean repo. Surface the stderr above and stop; do not render.",
            file=sys.stderr,
        )
        return scan.returncode
    out_text = scan.stdout or ""
    try:
        data = json.loads(out_text)
        if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
            raise ValueError("missing `findings` array")
    except (json.JSONDecodeError, ValueError) as e:
        print(
            f"ERROR: scan.py exited 0 but produced unparseable/empty findings "
            f"({e}) — coverage failure, not a clean repo. Stop; do not render.",
            file=sys.stderr,
        )
        return 1

    # Build everything in a temp file and atomically move it into --out only
    # after every step succeeds. A post-scan failure (collect_runs)
    # then never leaves a findings file *with* findings at the real path for the
    # orchestrator to render over — the coverage-failure contract is "stop,
    # leave nothing renderable".
    tmp = args.out.with_name(args.out.name + ".partial")
    try:
        tmp.write_text(out_text, encoding="utf-8")

        # 2. Data-driven gh phase. collect_runs gates on gh availability (the gh
        #    pass ALWAYS runs when gh is reachable — `--target` is inert here, it
        #    does NOT gate the pass or the sampling depth; see its help + issue #81),
        #    appends data-driven findings, and ALWAYS writes a `data_sources` block
        #    (PARTIAL when gh is unavailable — never a false "complete"). A non-zero
        #    exit is a real failure (bad JSON / catalog); gh flakiness degrades
        #    gracefully inside collect_runs.
        collect_cmd = [
            sys.executable, str(_DIR / "collect_runs.py"),
            "--in", str(tmp), "--out", str(tmp), "--target", str(args.target),
            # The checkout scan.py just walked. collect_runs reads each workflow's
            # YAML from it rather than re-fetching it over the gh contents API —
            # cheaper, and it parses the same commit the report stamps as audited
            # (the API serves the default branch's HEAD, which can differ).
            "--root", args.root,
        ]
        if args.repo:
            collect_cmd += ["--repo", args.repo]
        if args.with_logs:
            collect_cmd.append("--with-logs")
            # Save the long-pole job logs alongside the FINAL findings JSON (not the
            # .partial), in a sibling `<name>.data/` dir, so the report and the
            # fix-agent can read the step's internal timing without re-downloading.
            data_dir = args.out.with_name(args.out.stem + ".data")
            collect_cmd += ["--data-dir", str(data_dir)]
        if args.catalog:
            collect_cmd += ["--catalog", args.catalog]
        collected = _step(collect_cmd, capture_stdout=False)
        if collected.returncode != 0:
            print(f"ERROR: collect_runs exited {collected.returncode}; stopping.", file=sys.stderr)
            return 1

        # 3. Stamp timing epochs so the orchestrator can close the loop. If collect_runs
        # exited 0 but left corrupt JSON, fail with the same clean "stop, leave
        # nothing renderable" message the scan-parse guard uses — not a bare
        # traceback. (The finally below unlinks the partial; args.out is untouched.)
        try:
            data = json.loads(tmp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: post-collect findings JSON is unreadable ({e}); stopping.",
                  file=sys.stderr)
            return 1
        ds = data.get("data_sources")
        if isinstance(ds, dict) and request_probe.requests:
            measured = ds.get("gh_query_count")
            if isinstance(measured, int) and not isinstance(measured, bool):
                ds["gh_query_count"] = measured + request_probe.requests
        if request_estimate is not None and isinstance(ds, dict):
            ds["request_estimate"] = {
                "request_low": request_estimate.request_low,
                "request_high": request_estimate.request_high,
                "log_low": request_estimate.log_low,
                "log_high": request_estimate.log_high,
                "remaining_before_data_pass": request_estimate.remaining,
                "probe_requests": request_estimate.probe_requests,
                "request_ceiling": request_estimate.request_ceiling,
                "log_ceiling": request_estimate.log_ceiling,
                "graphql_high": request_estimate.graphql_high,
                "search_high": request_estimate.search_high,
            }
        if account_weekly is not None:
            data["account_weekly"] = account_weekly
        if skill_release is not None:
            data["skill_release"] = skill_release
        timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
        # `run_start_epoch` is set by scan.py (the always-runs anchor); don't
        # override it here. The driver only adds its own scripted-phase span.
        # Capture the end once so the epoch and the duration are consistent.
        _scripted_end = time.time()
        timings["scripted_end_epoch"] = round(_scripted_end, 3)
        timings["scripted_total_s"] = round(_scripted_end - run_start, 2)
        data["timings"] = timings
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        tmp.replace(args.out)  # all steps succeeded — atomically publish

        # Print the agent-facing summary + the exact render command to stdout, so the
        # orchestrator acts on ONE structured block instead of hand-spelunking the
        # findings JSON, re-probing gh for gating the data pass already resolved, or
        # reading blocking_path.py source to reconstruct the render invocation.
        # The sanitized report .md renders to an INTERNAL/session path beside --out
        # (the scratch dir), NOT the working tree. Every run renders + verify-gates it
        # there unconditionally — the honesty gate (verify_report.py) always runs. It is
        # copied into the user's working directory only when they opt in at the phase-6
        # close ('save the full report'); issue #18 made the artifact opt-in so the
        # default blast radius for prose-vs-data defects is the close, not a file the
        # user didn't ask for. (--out's findings.json + .data bundle also hold raw
        # third-party job logs and stay in this scratch path.)
        report_path = str(args.report_out
                          or args.out.with_name("ci-speedup-findings-report.md"))
        # data_dir is left to default: the render command reuses the bundle's
        # already-absolute `logs_dir`, so the emitted paths are valid regardless of
        # whether --out was relative.
        try:
            print(build_summary(data, findings_path=str(args.out), out_path=report_path,
                                root=args.root, bmf=args.bmf,
                                minute_budget=args.minute_budget))
        except Exception as e:  # a summary failure must never fail the run
            print(f"(summary unavailable: {e}; findings at {args.out})", file=sys.stderr)

    finally:
        if tmp.exists():  # any failure path: don't leave a stray partial file
            try:
                tmp.unlink()
            except OSError:
                pass

    return 0


if __name__ == "__main__":
    from log_level import configure_logging  # STARSLING_LOG_LEVEL opt-in
    configure_logging()
    sys.exit(main())

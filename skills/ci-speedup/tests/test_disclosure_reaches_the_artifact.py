"""INTEGRATION tests for the coverage disclosure: data -> renderer -> verifier.

Why this file exists, in one sentence: a green helper-level suite proved NOTHING
about the artifact.

The bug that motivated it. `collect()` builds `data_sources.partial_reason` through
`_partial_reason(...)`, which NAMES every workflow whose run-list fetch failed — a
workflow that vanished from the sample, so the critical path was recomputed from the
SURVIVORS. Two hundred lines later, an unconditional re-stamp at the end of the SAME
function overwrote it with a bare count:

    "1 gh API call(s) failed during collection"

which reads as a rounding error. The rate-limited merge gate was gone from the audit,
the report headlined a confident WRONG gate off whatever else survived, and every
existing guard passed — including the unit test of `_partial_reason` itself, which
tested a function whose output the shipped pipeline never used.

So these tests drive the REAL `collect()` and the REAL renderer, and assert on the
REPORT TEXT a user would read:

  1. `collect()` with a failing run list           -> partial_reason NAMES the workflow
  2. `collect()` + render                          -> the REPORT NAMES the workflow
  3. `collect()` with a failing workflow list      -> aborts, and the report does not
                                                      read as a normal (quiet-repo) audit
  4. EVERY run list fails                          -> the report must NOT say "an
                                                      archived, brand-new, or
                                                      low-activity repo"
  5. every JOB fetch for a workflow fails          -> named, and barred from headlining
                                                      off a queue-inflated check-run span
  6. `verify_report`'s invariant                   -> a report that hides a gap FAILS
  7. a permanently broken API (rate limit / 5xx /
     timeout) and a malformed body                 -> terminate, with a disclosed gap

Round 3 had to re-learn the lesson at this file's own expense: five of its
"integration" tests called `blocking_path._coverage_note` DIRECTLY with hand-built
dicts and never rendered a report — the exact anti-pattern the paragraphs above
indict. They passed while the RENDERER, 2000 lines away, went on printing "an
archived, brand-new, or low-activity repo" over a total run-list wipeout. They are
artifact-level now.

Rules for anything added here:
  - drive `collect()` and/or the real `render()`, and assert on the RENDERED REPORT;
  - never assert on a helper the shipped artifact does not call;
  - no tautologies (an `... or True` assertion was live in this file for a round).
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL_DIR / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)
import collect_runs as cr  # noqa: E402


def _load_verify_report():
    # ci-secure ships a verify_report too, so bind THIS skill's by path.
    name = "ci_speedup_verify_report_disclosure"
    spec = importlib.util.spec_from_file_location(
        name, _SKILL_DIR / "tests" / "verify_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


vr = _load_verify_report()

_GATE = ".github/workflows/test.yml"
_VANISHED = ".github/workflows/benchmark.yml"


def _iso(offset_s: int) -> str:
    return f"2026-01-0{1 + offset_s // 86400}T00:{offset_s % 3600 // 60:02d}:00Z"


def _job(name: str, dur: int) -> dict:
    return {"name": name, "started_at": "2026-01-01T00:00:00Z",
            "completed_at": f"2026-01-01T00:{dur // 60:02d}:{dur % 60:02d}Z",
            "id": abs(hash(name)) % 9000 + 1000, "conclusion": "success",
            "runner_name": "ubuntu-latest", "labels": ["ubuntu-latest"],
            "steps": []}


class _TwoWorkflowClient:
    """A repo `o/r` with two workflows: `test.yml` (the typical gate) and
    `benchmark.yml`. Every endpoint resolves. Subclasses break exactly one of them,
    so what changes in the artifact is attributable to that one failure."""

    gave_up = False

    def __init__(self) -> None:
        self.queries = 0
        self.errors = 0
        self._shas = [f"s{i}" for i in range(1, 9)]

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        self.queries += int(query)
        self.errors += int(error)

    def available(self) -> bool:
        return True

    def _runs(self, wf_id: int) -> list[dict]:
        wall = 1410 if wf_id == 1 else 9010
        # `conclusion`/`status` are REQUIRED post-#213: the merged sampling loop derives
        # the success sample from the all-status page via `_success_runs_from_all_status`,
        # which keeps only runs with `conclusion == "success"` and `status == "completed"`.
        # Under main the fake was hit by an explicit `status=success` query and returned
        # every run regardless, so these keys were never needed; without them the derived
        # sample is empty and no spine is ever measured.
        return [{"id": (100 if wf_id == 1 else 200) + i, "event": "pull_request",
                 "head_sha": sha, "created_at": _iso(0), "run_started_at": _iso(0),
                 "updated_at": _iso(wall),
                 "conclusion": "success", "status": "completed"}
                for i, sha in enumerate(self._shas)]

    def _jobs(self, run_id: int) -> list[dict]:
        return ([_job("Test suite", 1400)] if run_id < 200
                else [_job("Run Benchmark Jobs", 9000)])

    def json(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            return {"workflows": [
                {"id": 1, "path": _GATE, "name": "test"},
                {"id": 2, "path": _VANISHED, "name": "benchmark"}]}
        m = re.match(r"repos/o/r/actions/workflows/(\d+)/runs\?(.*)", endpoint)
        if m:
            wf_id, qs = int(m.group(1)), m.group(2)
            if re.search(r"per_page=1(?![0-9])", qs):      # monthly volume
                return {"total_count": 30}
            return {"workflow_runs": self._runs(wf_id)}
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m:
            return {"jobs": self._jobs(int(m.group(1)))}
        m = re.match(r"repos/o/r/commits/([^/]+)/check-runs", endpoint)
        if m:
            return {"check_runs": [
                {"name": "Test suite", "started_at": _iso(0), "completed_at": _iso(1400)},
                {"name": "Run Benchmark Jobs", "started_at": _iso(0),
                 "completed_at": _iso(9000)}]}
        if endpoint == "repos/o/r":
            return {"default_branch": "main"}
        return None            # rulesets / protection / contents — allow_missing

    def text(self, endpoint: str, allow_missing: bool = False):
        self.queries += 1
        return ""


class _RunListFailsForBenchmark(_TwoWorkflowClient):
    """`benchmark.yml`'s run list is unfetchable (a rate-limit exhaustion). The
    workflow drops out of the sample entirely — it is MISSING, not empty.

    Post-#213 the sampling loop derives the success sample from the ALL-STATUS run
    page and issues the explicit `status=success` query only as a truncation fallback,
    so failing ONLY that query would never be reached. To express "benchmark.yml's run
    list is unfetchable" the fake must fail the WHOLE `workflows/2/runs?...` run-list
    family — both the all-status page (`_all_status_runs`, no status filter) and the
    success query (`_sample_runs`) — while leaving the `per_page=1` monthly-volume probe
    working (that is a count, not a run list, and the workflow is known to exist)."""

    def json(self, endpoint: str, allow_missing: bool = False):
        m = re.match(r"repos/o/r/actions/workflows/2/runs\?(.*)", endpoint)
        if m and not re.search(r"per_page=1(?![0-9])", m.group(1)):
            self.queries += 1
            self.errors += 1          # what GhClient._invoke does on a give-up
            return None
        return super().json(endpoint, allow_missing)


class _WorkflowListFails(_TwoWorkflowClient):
    """The workflow-LIST fetch fails: nothing about the repo can be measured."""

    def json(self, endpoint: str, allow_missing: bool = False):
        if endpoint.startswith("repos/o/r/actions/workflows?"):
            self.queries += 1
            self.errors += 1
            return None
        return super().json(endpoint, allow_missing)


class _GhAuthProbeBlocked(_TwoWorkflowClient):
    """gh is installed AND a token is stored, but the `available()` auth probe fails
    because the GitHub API REFUSES the credential — a secondary-rate-limit 403, or a dead
    (expired / revoked) token, on the verify call. The OFFLINE diagnosis therefore reports
    `api_blocked`, not `absent`: the pass could not run because the API declined it, not
    because gh is missing."""

    def available(self) -> bool:
        return False

    def diagnose_unavailability(self) -> str:
        return "api_blocked"


class _GhGenuinelyAbsent(_TwoWorkflowClient):
    """gh is missing or has no token: `available()` fails and the offline diagnosis
    confirms `absent`. A static-only report is the honest fallback here — not a hole."""

    def available(self) -> bool:
        return False

    def diagnose_unavailability(self) -> str:
        return "absent"


def _doc() -> dict:
    return {"repo": "o/r", "findings": [
        {"id": "f1", "workflow_file": _GATE},
        {"id": "f2", "workflow_file": _VANISHED}]}


# ---------------------------------------------------------------------------
# 1. The DATA: the by-name disclosure must survive to findings.json.
# ---------------------------------------------------------------------------

def test_a_failed_run_list_names_the_vanished_workflow_in_findings(monkeypatch):
    """THE regression test. Before the fix this failed on the last assertion: the
    end-of-collect() re-stamp had already overwritten the by-name sentence with the
    bare count, so the artifact said "1 gh API call(s) failed during collection" and
    the reader had no way to know the benchmark workflow was never measured."""
    monkeypatch.setattr(cr, "GhClient", _RunListFailsForBenchmark)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]

    # The failed fetch counts as a coverage gap — `gh_error_count` is the channel the
    # partial-coverage banner keys off, and a gap that isn't counted can't fire it. The
    # INVARIANT is "the gap is counted", not an exact number: post-#213 an unfetchable
    # run list fails BOTH the all-status page and the success fallback (and the detector
    # loop's retry may add more), so the count is >= 1, not exactly 1.
    assert ds["gh_error_count"] >= 1
    # The structured stamp: WHICH workflow vanished. The same workflow can be stamped
    # under more than one fetch ("success run sample", "all-status run list"), so assert
    # on the SET of vanished workflows, not a positional list.
    assert ds["run_list_fetch_failures"], "the run-list gap must be stamped"
    assert {g["workflow_file"] for g in ds["run_list_fetch_failures"]} == {_VANISHED}

    # The human sentence must NAME it — a bare count reads as a rounding error when
    # what actually happened is that a whole workflow left the sample.
    reason = ds["partial_reason"]
    assert reason, "a coverage gap must produce a partial_reason"
    assert _VANISHED in reason, (
        f"FINAL partial_reason does NOT name the vanished workflow: {reason!r}")
    assert "MISSING from the sample" in reason
    # And the severity must be carried as DATA, not left for a renderer to infer.
    assert ds["partial_kind"] == cr._PARTIAL_WORKFLOW_MISSING


def test_a_clean_collection_discloses_nothing(monkeypatch):
    """The disclosure must stay silent when coverage is clean — otherwise a
    permanent banner trains the reader to ignore it."""
    monkeypatch.setattr(cr, "GhClient", _TwoWorkflowClient)
    ds = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)["data_sources"]
    assert ds["gh_error_count"] == 0
    assert ds["run_list_fetch_failures"] == []
    assert ds["partial_reason"] is None
    assert ds["partial_kind"] is None


# ---------------------------------------------------------------------------
# 2. The ARTIFACT: the rendered report must name it. This is the property that
#    actually matters — findings.json is not what the user reads.
# ---------------------------------------------------------------------------

def test_the_rendered_report_names_the_vanished_workflow(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _RunListFailsForBenchmark)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    # the gap is counted — the invariant is ">= 1", not an exact number (post-#213 an
    # unfetchable run list fails both the all-status page and the success fallback).
    assert out["data_sources"]["gh_error_count"] >= 1
    report = bp.render(out)

    assert _VANISHED in report, (
        "the report does not name the workflow that vanished from the sample — its "
        "critical path is computed from the survivors, so a vanished merge gate "
        "would be headlined as a confident, wrong gate")
    assert "MISSING from the sample" in report
    # ...and it must NOT be dressed up as a rounding error.
    assert "marginally fewer runs" not in report


# ---------------------------------------------------------------------------
# 3. The ABORT: a total collection failure must not read as a normal audit.
# ---------------------------------------------------------------------------

def test_a_failed_workflow_list_aborts_and_does_not_read_as_a_quiet_repo(monkeypatch):
    """The workflow-list fetch failing means we have NO idea what the repo runs. The
    collector already aborted with an honest sentence ("NO workflow could be
    measured — this is a collection failure, not a repo with no workflows") — but the
    RENDERER then stapled "so a few runs/jobs are absent from the sample - the P50s
    are over marginally fewer runs than the totals above" onto it, and headlined the
    repo as "an archived, brand-new, or low-activity repo whose run history aged
    out". The report contradicted its own disclosure and misdiagnosed a broken fetch
    as a quiet repo."""
    monkeypatch.setattr(cr, "GhClient", _WorkflowListFails)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]
    assert ds["tiers_run"] == []
    assert ds["gh_error_count"] == 1
    assert ds["partial_kind"] == cr._PARTIAL_COLLECTION_FAILED
    assert "NO workflow could be measured" in ds["partial_reason"]

    report = bp.render(out)
    # The minimizing suffix must NOT be applied to a whole-repo gap.
    assert "marginally fewer runs" not in report
    assert "a few runs/jobs are absent from the sample" not in report
    # ...and the repo must not be diagnosed as merely quiet.
    assert "low-activity repo whose GitHub Actions run history aged out" not in report
    assert "Collection FAILED" in report
    assert "not a quiet repo" in report


def test_an_api_blocked_auth_probe_reads_as_a_collection_failure_not_gh_unavailable(monkeypatch):
    """THE up-front silent-drop bug. A refused credential (rate-limited, or expired /
    revoked) makes `gh auth status` (the `available()` probe) return non-zero even though
    gh is installed and a token is stored — the API declines the verify call. Before the
    fix `available() is False` was labeled `gh_unavailable` (a NOT-MEASURED kind), so the
    run shipped a static-only report that read as a complete audit of a quiet repo while
    the collection had actually been refused. It must now be the LOUD `collection_failed`
    kind — the same severity the mid-collection abort already uses — with `gh_available`
    True (gh IS available; the API refused it), the full disclosure stamp, and a reason
    that names BOTH remedies (wait out a rate limit, or re-authenticate)."""
    monkeypatch.setattr(cr, "GhClient", _GhAuthProbeBlocked)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]
    assert ds["partial_kind"] == cr._PARTIAL_COLLECTION_FAILED
    assert ds["gh_available"] is True
    # The disclosure keys must ALL be stamped, even empty — a missing key makes the verify
    # invariant SKIP (see check_run_list_gaps_named / ARCHITECTURE §12).
    assert ds["run_list_fetch_failures"] == []
    assert ds["job_fetch_failures"] == []
    # The reason must name the rate limit AND the invalid-token cause — not assert
    # "authenticated" or prescribe waiting out a rate limit as the only remedy (a dead
    # token never clears; the old prose sent that user into a loop).
    assert "rate-limit" in ds["partial_reason"]
    assert "expired" in ds["partial_reason"]
    assert "re-authenticate" in ds["partial_reason"]
    assert "NOT a trustworthy CI audit" in ds["partial_reason"]

    report = bp.render(out)
    # The loud severe machinery must fire, exactly as for a workflow-list abort.
    assert "Collection FAILED" in report
    assert "GitHub API REFUSED" in report
    # Both remedies reach the reader.
    assert "wait out a rate limit" in report
    assert "re-authenticate" in report
    # ...and it must NOT be dressed up as a quiet repo or a rounding error.
    assert "low-activity repo whose GitHub Actions run history aged out" not in report
    assert "marginally fewer runs" not in report


def test_a_genuinely_absent_gh_still_reads_as_a_quiet_static_only_run(monkeypatch):
    """The other side of the split must be preserved: when gh is truly missing or
    unauthenticated the diagnosis is `absent`, and the honest `gh_unavailable` fallback
    stays — a static-only report there is NOT a hole and must not be alarmed as a
    collection failure."""
    monkeypatch.setattr(cr, "GhClient", _GhGenuinelyAbsent)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]
    assert ds["partial_kind"] == cr._PARTIAL_GH_UNAVAILABLE
    assert ds["gh_available"] is False

    report = bp.render(out)
    assert "Collection FAILED" not in report
    assert "gh data pass could not start" in report


def test_a_failed_gh_probe_names_its_stderr_and_the_sandbox_cause(monkeypatch):
    """A sandboxed / no-network shell fails `gh auth status` (and, with no keychain, the
    offline `gh auth token` probe too) even when gh works fine in a normal terminal. The
    reason used to be a bare "gh CLI not available or not authenticated" — blaming auth
    and hiding the probe's own error. It must quote the probe's first stderr line and
    name the sandbox / no-network cause with its remedy (retry with network access)."""
    def _run(argv, *a, **k):
        if argv[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(
                argv, 1, "", "\nerror connecting to api.github.com\ncheck your internet\n")
        if argv[1:3] == ["auth", "token"]:
            return subprocess.CompletedProcess(argv, 1, "", "no oauth token\n")
        return subprocess.CompletedProcess(argv, 0, "gh version 2.60.0\n", "")
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    monkeypatch.setattr(cr.subprocess, "run", _run)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]
    assert ds["partial_kind"] == cr._PARTIAL_GH_UNAVAILABLE
    reason = ds["partial_reason"]
    assert "error connecting to api.github.com" in reason
    assert "check your internet" not in reason      # first non-blank line only
    assert "sandbox" in reason and "network" in reason
    assert "gh CLI not available or not authenticated" not in reason


# Real `gh auth status` failure output (gh 2.100): a bare hostname line, then the
# indented, X-marked error. Taking the first non-blank line quoted "github.com".
_REAL_GH_AUTH_STATUS_FAILURE = (
    "github.com\n"
    "  X Failed to log in to github.com using token (GH_TOKEN)\n"
    "  - Active account: true\n"
    "  - The token in GH_TOKEN is invalid.\n")


def _real_gh_probe(status_stderr: str, token_rc: int):
    def _run(argv, *a, **k):
        if argv[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(argv, 1, "", status_stderr)
        if argv[1:3] == ["auth", "token"]:
            return subprocess.CompletedProcess(argv, token_rc, "", "")
        return subprocess.CompletedProcess(argv, 0, "gh version 2.100.0\n", "")
    return _run


def test_a_real_gh_probe_failure_quotes_the_error_not_the_hostname(monkeypatch):
    """gh prints the host name on its own line before the error, so "first non-blank
    line" quoted `github.com` — the reason never carried gh's own words."""
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    monkeypatch.setattr(cr.subprocess, "run", _real_gh_probe(_REAL_GH_AUTH_STATUS_FAILURE, 1))
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    reason = out["data_sources"]["partial_reason"]
    assert "Failed to log in to github.com using token (GH_TOKEN)" in reason
    assert "exited 1: github.com." not in reason
    assert "X Failed" not in reason                 # gh's status glyph is dropped


def test_a_missing_gh_binary_says_the_probe_could_not_run(monkeypatch):
    def _run(argv, *a, **k):
        raise FileNotFoundError(2, "No such file or directory", "gh")
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    monkeypatch.setattr(cr.subprocess, "run", _run)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]
    assert ds["partial_kind"] == cr._PARTIAL_GH_UNAVAILABLE
    assert "`gh auth status` could not run" in ds["partial_reason"]


def test_an_api_blocked_probe_names_the_sandbox_cause_and_gh_words(monkeypatch):
    """A sandboxed shell that can still read gh's stored token lands on `api_blocked`
    (the loud collection-failure path). Its reason blamed only a rate limit or the
    token; the sandbox / no-network cause and gh's own error must be named there too."""
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    monkeypatch.setattr(cr.subprocess, "run", _real_gh_probe(_REAL_GH_AUTH_STATUS_FAILURE, 0))
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]
    assert ds["partial_kind"] == cr._PARTIAL_COLLECTION_FAILED
    reason = ds["partial_reason"]
    assert "Failed to log in to github.com using token (GH_TOKEN)" in reason
    assert "sandboxed" in reason and "network / host access" in reason
    assert "rate-limit" in reason and "re-authenticate" in reason   # old causes kept


def test_a_gh_pass_that_never_started_is_not_rendered_as_a_dormant_repo(monkeypatch):
    """The summary stopped calling a never-started gh pass a property of the repo; the
    REPORT must agree. It rendered the dormant-repo banner ("an archived, brand-new, or
    low-activity repo ... Re-run once the repo has recent Actions runs") and told the
    reader to pass `--repo` they had already passed."""
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    monkeypatch.setattr(cr.subprocess, "run", _real_gh_probe(_REAL_GH_AUTH_STATUS_FAILURE, 1))
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    assert out["data_sources"]["partial_kind"] == cr._PARTIAL_GH_UNAVAILABLE
    report = bp.render(out)
    for lie in ("an archived, brand-new, or low-activity repo",
                "No run history to measure",
                "Re-run once the repo has recent Actions runs",
                "Pass `--repo owner/repo`"):
        assert lie not in report, lie
    assert "says nothing about whether this repo's CI is quiet" in report
    assert "network / host access" in report
    assert "access.." not in report                  # no doubled period after the reason


# ---------------------------------------------------------------------------
# 3b. The CLASSIFIER itself. The two tests above monkeypatch `diagnose_unavailability`
# on the fake client, so they prove `collect()` ROUTES a given label correctly but never
# run the real offline-probe heuristic — the substance of the fix. These exercise the
# shipped `GhClient.diagnose_unavailability()` directly, pinning every branch, so an
# inverted condition or a mis-mapped probe result (which would silently re-open the
# original silent-drop bug) turns a test red.
# ---------------------------------------------------------------------------

def _fake_run(exit_by_argv):
    """Build a `subprocess.run` stand-in: `exit_by_argv` maps the gh subcommand (argv[1])
    to either an int return code or an exception instance to raise."""
    def _run(argv, *a, **k):
        outcome = exit_by_argv[argv[1]]
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(argv, outcome, "", "")
    return _run


def _real_client(monkeypatch):
    # A real GhClient with replay mode OFF, so the offline probes actually branch.
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    return cr.GhClient()


def test_diagnose_unavailability_binary_and_token_present_is_api_blocked(monkeypatch):
    """gh present + a token stored, but `available()` already failed → the API refused the
    credential (rate limit OR dead token). The LOUD side."""
    monkeypatch.setattr(cr.subprocess, "run",
                        _fake_run({"--version": 0, "auth": 0}))
    assert _real_client(monkeypatch).diagnose_unavailability() == "api_blocked"


def test_diagnose_unavailability_no_token_is_absent(monkeypatch):
    """gh present but NO token configured (`gh auth token` non-zero) → genuinely can't
    run; the honest quiet `absent` fallback."""
    monkeypatch.setattr(cr.subprocess, "run",
                        _fake_run({"--version": 0, "auth": 1}))
    assert _real_client(monkeypatch).diagnose_unavailability() == "absent"


def test_diagnose_unavailability_missing_binary_is_absent(monkeypatch):
    """gh not on PATH (`gh --version` raises FileNotFoundError) → truly absent."""
    monkeypatch.setattr(cr.subprocess, "run",
                        _fake_run({"--version": FileNotFoundError()}))
    assert _real_client(monkeypatch).diagnose_unavailability() == "absent"


def test_diagnose_unavailability_hung_probe_defaults_loud_not_quiet(monkeypatch):
    """The regression guard for the reintroduced silent drop: gh IS present (available()
    already spawned it), but the offline `gh auth token` probe HANGS (`TimeoutExpired`, a
    locked keyring). A timeout is NOT absence — routing it to the quiet `absent` path
    would re-open the exact static-only silent drop this PR exists to kill. An ambiguous
    probe failure must default to the LOUD `api_blocked` side."""
    monkeypatch.setattr(cr.subprocess, "run", _fake_run(
        {"--version": 0, "auth": subprocess.TimeoutExpired(["gh", "auth", "token"], 10)}))
    assert _real_client(monkeypatch).diagnose_unavailability() == "api_blocked"


def test_diagnose_unavailability_replay_mode_is_absent(monkeypatch):
    """Defensive fixtures guard: in replay mode the offline probes must never spawn gh or
    claim `api_blocked` (unreachable via collect() today, but kept honest in isolation)."""
    monkeypatch.setenv("CI_SPEEDUP_GH_FIXTURES", "/tmp/does-not-matter")

    def _boom(*a, **k):  # spawning gh in replay mode is a bug
        raise AssertionError("diagnose_unavailability spawned gh in replay mode")

    monkeypatch.setattr(cr.subprocess, "run", _boom)
    assert cr.GhClient().diagnose_unavailability() == "absent"


# ---------------------------------------------------------------------------
# 4. The VERIFIER: the invariant that makes the whole class uncatchable-again.
# ---------------------------------------------------------------------------

def test_verify_report_fails_a_report_that_hides_a_run_list_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _RunListFailsForBenchmark)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(out), encoding="utf-8")

    good = bp.render(out)
    chk = vr.check_run_list_gaps_named(good, findings)
    assert chk.ok and not chk.skipped, chk.detail

    # The exact artifact the bug produced: the same findings, but a report that never
    # names the workflow. It satisfies the pre-existing `_gh_errors_disclosure_violation`
    # guard (count + "gh api" + "failed") — which is precisely why that guard could not
    # catch this.
    hidden = good.replace(_VANISHED, "an unnamed workflow")
    chk = vr.check_run_list_gaps_named(hidden, findings)
    assert not chk.ok, "a report that does not name the vanished workflow must FAIL"
    assert _VANISHED in chk.detail

    # A clean run passes — and RUNS. Not `skipped`: a guard that skips on every clean
    # artifact is a guard nobody is exercising, which is how it stayed vacuous across
    # the whole committed corpus for two rounds.
    monkeypatch.setattr(cr, "GhClient", _TwoWorkflowClient)
    clean = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    clean_path = tmp_path / "clean.json"
    clean_path.write_text(json.dumps(clean), encoding="utf-8")
    chk = vr.check_run_list_gaps_named(bp.render(clean), clean_path)
    assert chk.ok and not chk.skipped


def test_verify_report_fails_closed_on_a_malformed_stamp(tmp_path):
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(
        {"data_sources": {"gh_available": True, "job_fetch_failures": [],
                          "run_list_fetch_failures": "benchmark.yml"}}), encoding="utf-8")
    chk = vr.check_run_list_gaps_named("## 🗄️ Data sources", findings)
    assert not chk.ok and "not a list" in chk.detail


def test_verify_report_fails_CLOSED_when_a_gh_run_omits_the_stamps(tmp_path):
    """The guard used to fail closed on a malformed stamp but OPEN on a MISSING one —
    and the three early-return `data_sources` dicts omitted the key, as did all six
    committed worked examples. So the invariant SKIPPED on the entire corpus and ran
    only in its own unit tests: a guard that skips is a guard that isn't there."""
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(
        {"data_sources": {"gh_available": True, "gh_error_count": 1}}), encoding="utf-8")
    chk = vr.check_run_list_gaps_named("a report", findings)
    assert not chk.ok, "a gh-tier run with no sample-gap stamps must FAIL, not skip"
    assert "run_list_fetch_failures" in chk.detail

    # A run where the gh tier never ran has no sample to have gaps in — that one skips.
    static = tmp_path / "static.json"
    static.write_text(json.dumps(
        {"data_sources": {"gh_available": False}}), encoding="utf-8")
    assert vr.check_run_list_gaps_named("a report", static).skipped


def test_every_committed_worked_example_actually_exercises_the_gap_invariant():
    """...and the corpus must carry the stamps, or the guard above is decorative. This
    is the test that would have caught the invariant being vacuous on all six."""
    reports = _SKILL_DIR / "reports"
    findings = sorted(reports.glob("*/findings.json"))
    if not findings:
        # No committed worked-example corpus in this public repo — skip LOUDLY (never a
        # silent vacuous pass). The guard runs again the moment a corpus reappears
        # (a generated examples/ report, or in the internal development repo).
        pytest.skip("no committed report corpora in this repo — corpus guards run "
                    "against generated reports / in the internal development repo")
    for f in findings:
        ds = json.loads(f.read_text(encoding="utf-8"))["data_sources"]
        for key in ("run_list_fetch_failures", "job_fetch_failures", "partial_kind"):
            assert key in ds, f"{f.parent.name}: data_sources is missing {key!r}"
        report = next(f.parent.glob("*.md"))
        chk = vr.check_run_list_gaps_named(report.read_text(encoding="utf-8"), f)
        assert chk.ok and not chk.skipped, (
            f"{f.parent.name}: the gap invariant SKIPPED — it is not running on the "
            f"committed corpus at all ({chk.detail})")


# ---------------------------------------------------------------------------
# 5. The BREAKER: a sustained block must END the run, not hang it.
# ---------------------------------------------------------------------------

def test_a_permanently_rate_limited_client_terminates_with_a_disclosed_gap(monkeypatch):
    """The per-call attempt budget bounds ONE call. Without a global breaker, a
    sustained block (a primary limit whose reset is 40 minutes out) makes every
    remaining call sleep its cap, retry into the same block, give up — and the next
    call start again at attempt 1. On a repo with hundreds of calls left that is
    HOURS of wall-clock with no terminal condition: the audit trades a fast wrong
    answer for a hang. The breaker turns it into a fast, LOUD, honest failure."""
    slept: list[float] = []
    monkeypatch.setattr(cr.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(cr.random, "uniform", lambda a, b: 0.0)

    calls: list[list[str]] = []

    def _always_rate_limited(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=1,
            stdout="HTTP/2.0 403 Forbidden\r\nx-ratelimit-remaining: 0\r\n\r\n",
            stderr="gh: You have exceeded a secondary rate limit.")

    monkeypatch.setattr(cr.subprocess, "run", _always_rate_limited)

    client = cr.GhClient()
    # Enough endpoints that an unbounded client would grind through all of them.
    for i in range(60):
        client.json(f"repos/o/r/actions/workflows/{i}/runs?per_page=20")

    assert client.gave_up, "a sustained rate-limit block must trip the global breaker"
    # The breaker is what bounds the run: once tripped, later calls cost NOTHING.
    assert len(calls) <= cr._GH_MAX_ATTEMPTS * cr._GH_MAX_GIVEUPS, (
        f"{len(calls)} subprocesses spawned — the breaker did not short-circuit")
    assert sum(slept) <= cr._GH_TOTAL_BACKOFF_BUDGET_S + cr._GH_MAX_BACKOFF_S
    assert client.errors >= 60 - cr._GH_MAX_GIVEUPS, (
        "every blocked call is a coverage gap and must be counted")


def test_collect_aborts_with_a_disclosed_gap_when_the_breaker_trips(monkeypatch):
    """...and the abort travels all the way to the artifact: `collect()` must bail out
    through the SAME disclosed-coverage-gap path the workflow-list failure uses, not
    render a confident critical path off the handful of workflows that got in first."""

    class _AllRateLimited(_TwoWorkflowClient):
        gave_up = False

        def json(self, endpoint: str, allow_missing: bool = False):
            self.queries += 1
            if endpoint.startswith("repos/o/r/actions/workflows?"):
                return {"workflows": [{"id": 1, "path": _GATE, "name": "test"},
                                      {"id": 2, "path": _VANISHED, "name": "benchmark"}]}
            self.errors += 1
            self.gave_up = True         # the breaker tripped inside the client
            return None

    monkeypatch.setattr(cr, "GhClient", _AllRateLimited)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]
    assert ds["tiers_run"] == []
    assert ds["partial_kind"] == cr._PARTIAL_COLLECTION_FAILED
    assert "ABORTED" in ds["partial_reason"]
    report = bp.render(out)
    assert "Collection FAILED" in report
    assert "marginally fewer runs" not in report


# ---------------------------------------------------------------------------
# 6. THE CARDINAL RULE: a broken fetch must never render as a quiet repo.
#
# This is what two review rounds and a green suite missed. The static-only banner
# branched on `partial_kind == "collection_failed"` ONLY — but a total RUN-LIST wipeout
# stamps `workflow_missing`, so it took the `else` branch and rendered:
#
#     > **No run history to measure.** ci-speedup sampled 0 runs (an archived,
#     > brand-new, or low-activity repo whose GitHub Actions run history aged out) ...
#     > **Bottom line.** No run history was available ...
#
# ...while the verify gate PASSED (the honest note was in the Data Sources footer, 50
# lines below the headline the reader actually forms their takeaway from). The reader's
# conclusion: "my CI is quiet." The truth: GitHub 5xx'd every run-list call.
#
# Not exotic: the breaker aside, sustained 5xx / timeouts on the run-list endpoints
# produce `workflow_missing`, never `collection_failed`.
# ---------------------------------------------------------------------------

class _AllRunListsFail(_TwoWorkflowClient):
    """Sustained 5xx on the RUN-LIST endpoints (a GitHub incident): the workflow list
    still resolves, so we know WHAT the repo runs — we just can't read a single run of
    it. Every workflow drops out of the sample; there are no poles; the report is
    static-only."""

    def json(self, endpoint: str, allow_missing: bool = False):
        if re.match(r"repos/o/r/actions/workflows/\d+/runs\?", endpoint):
            self.queries += 1
            self.errors += 1
            return None
        return super().json(endpoint, allow_missing)


def test_a_total_run_list_wipeout_is_NOT_rendered_as_an_archived_or_quiet_repo(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _AllRunListsFail)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]
    # Precondition: this really is the kind that used to slip through.
    assert ds["partial_kind"] == cr._PARTIAL_WORKFLOW_MISSING
    assert not (out.get("pr_critical_path") or {}).get("poles"), (
        "the fixture must produce NO measured spine — that is the branch under test")

    report = bp.render(out)

    # THE regression. Every one of these phrases is the dormant-repo diagnosis.
    for lie in ("an archived, brand-new, or low-activity repo",
                "run history aged out",
                "No run history to measure",
                "No run history was available"):
        assert lie not in report, (
            f"the report tells a reader whose fetch just FAILED that they have "
            f"{lie!r} — a confident WRONG diagnosis that invites them to conclude "
            f"their CI is quiet")
    # ...and it must say what actually happened, by name, in the BANNER.
    assert "Collection FAILED" in report
    assert "not a quiet repo" in report
    for wf in (_GATE, _VANISHED):
        assert wf in report, f"{wf} vanished from the sample but is not named"
    assert "marginally fewer runs" not in report


def test_a_total_run_list_wipeout_with_NO_static_findings_still_renders_the_banner(
        monkeypatch):
    """The sub-case: with nothing static to say, `_render_static_only` returned "" and
    the WHOLE report collapsed to `_No measured critical path in this findings JSON._`
    — no banner, no coverage note, no data-sources footer. The loudest failure in the
    collector, rendered as a shrug."""
    monkeypatch.setattr(cr, "GhClient", _AllRunListsFail)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    # The findings SEED the workflow sample (that is how collect knows what to fetch),
    # so the artifact under test is that same broken collection with nothing left to say
    # statically: a repo whose workflows are clean and whose run history we could not
    # read.
    out["findings"] = []
    assert out["data_sources"]["run_list_fetch_failures"], "the gaps must survive"
    report = bp.render(out)

    assert report.strip() != "_No measured critical path in this findings JSON._", (
        "the loudest failure in the collector rendered as a one-line shrug: no banner, "
        "no coverage note, no data-sources footer")
    assert "Collection FAILED" in report
    assert "an archived, brand-new, or low-activity repo" not in report
    for wf in (_GATE, _VANISHED):
        assert wf in report


def test_the_verify_gate_catches_the_wipeout_the_renderer_used_to_hide(monkeypatch, tmp_path):
    """Belt and braces at the artifact level: the SAME findings rendered by a renderer
    that hides the names must FAIL verify. (It passed before, because the gap list was
    only reachable via a footer the check never required.)"""
    monkeypatch.setattr(cr, "GhClient", _AllRunListsFail)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps(out), encoding="utf-8")

    good = bp.render(out)
    assert vr.check_run_list_gaps_named(good, findings).ok
    hidden = good.replace(_GATE, "x").replace(_VANISHED, "y")
    assert not vr.check_run_list_gaps_named(hidden, findings).ok


# ---------------------------------------------------------------------------
# 7. A JOBS-fetch wipeout: named, and barred from headlining.
#
# Only RUN-LIST failures used to be named. If one workflow's per-run JOB fetches all
# failed, its `crit` was empty, `_map_check_to_job(..., require_developer_timing=True)`
# found nothing, and its checks fell back to CHECK-RUN SPAN timing — which is
# queue-inflated. That inflated number could outrank the true gate and HEADLINE the
# report, disclosed only as a bare count plus the MINOR "marginally fewer runs" note.
# ---------------------------------------------------------------------------

class _JobFetchWipeoutForBenchmark(_TwoWorkflowClient):
    """`benchmark.yml`'s run list resolves, but EVERY one of its runs' job fetches
    fails. Its check-run span reads 9000s (mostly queue) against the true gate's 1400s
    — so if the fallback is allowed to stand in for the missing job timing, the
    benchmark check headlines the report off a number we could not measure."""

    def json(self, endpoint: str, allow_missing: bool = False):
        m = re.match(r"repos/o/r/actions/runs/(\d+)/jobs", endpoint)
        if m and int(m.group(1)) >= 200:          # benchmark.yml's run ids
            self.queries += 1
            self.errors += 1
            return None
        return super().json(endpoint, allow_missing)


def test_a_jobs_fetch_wipeout_is_named_and_cannot_headline_the_report(monkeypatch):
    monkeypatch.setattr(cr, "GhClient", _JobFetchWipeoutForBenchmark)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]

    # 1. It is STAMPED, by name, as a sample gap — not just added to a bare count.
    assert [g["workflow_file"] for g in ds["job_fetch_failures"]] == [_VANISHED]
    # 2. At the SEVERE severity, so it can never render as "marginally fewer runs".
    assert ds["partial_kind"] == cr._PARTIAL_WORKFLOW_MISSING

    # 3. The queue-inflated check-run span must NOT be the headline. The benchmark
    #    check's span is 9000s vs the true gate's 1400s, so before the fix it won.
    poles = (out.get("pr_critical_path") or {}).get("poles") or []
    headline = poles[0]["check"] if poles else ""
    assert headline != "Run Benchmark Jobs", (
        "a workflow whose job timing we FAILED TO FETCH headlined the report off its "
        "queue-inflated check-run span — the number is mostly queue, and it outranked "
        "the gate we actually measured")
    for p in poles:
        assert p.get("timing_source") != "pr_check_runs", (
            "no pole may be timed by a check-run span while a job-fetch wipeout is in "
            "play: we cannot tell whose span it is")

    # 4. And the artifact SAYS so, by name.
    report = bp.render(out)
    assert _VANISHED in report
    assert "MISSING from the sample" in report
    assert "marginally fewer runs" not in report


# ---------------------------------------------------------------------------
# 8. Severity -> RENDERED PROSE. (Was five helper-level tests that called
#    `_coverage_note` directly with hand-built dicts and never rendered a report.)
# ---------------------------------------------------------------------------

def _measured_doc(monkeypatch, **ds_overrides) -> dict:
    """A real, clean `collect()` artifact (poles and all) whose `data_sources` is then
    set to the coverage state under test. The report is rendered by the REAL renderer,
    so what these assert on is what a user would read."""
    monkeypatch.setattr(cr, "GhClient", _TwoWorkflowClient)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    assert (out.get("pr_critical_path") or {}).get("poles"), "need a measured spine"
    out["data_sources"].update(ds_overrides)
    return out


def test_a_severe_kind_renders_LOUD_in_the_report_and_a_thinned_one_does_not(monkeypatch):
    """The severity comes from the DATA (`partial_kind`), never from string-matching the
    reason's prose — one reword away from stapling "so a few runs/jobs are absent from
    the sample" onto "NO workflow could be measured"."""
    for kind in (cr._PARTIAL_COLLECTION_FAILED, cr._PARTIAL_WORKFLOW_MISSING):
        report = bp.render(_measured_doc(
            monkeypatch, partial_kind=kind, gh_error_count=0,
            partial_reason="the sky fell"))
        assert "the sky fell" in report
        assert "this audit is INCOMPLETE" in report, f"{kind} must render LOUD"
        assert "marginally fewer runs" not in report, (
            f"{kind} is a HOLE in the data; it must never render as a minor caveat")

    thinned = bp.render(_measured_doc(
        monkeypatch, partial_kind=cr._PARTIAL_SAMPLE_THINNED, gh_error_count=3,
        partial_reason="3 gh API call(s) failed during collection"))
    assert "3 gh API call(s) failed" in thinned
    assert "marginally fewer runs" in thinned
    assert "this audit is INCOMPLETE" not in thinned


def test_the_note_reaches_the_report_even_when_no_CALL_failed(monkeypatch):
    """The note used to be gated on `gh_error_count` being truthy, so a reason with zero
    failed CALLS (an aborted collection, a malformed body) rendered NOTHING at all."""
    report = bp.render(_measured_doc(
        monkeypatch, gh_error_count=0, partial_kind=cr._PARTIAL_COLLECTION_FAILED,
        partial_reason="the workflow-list fetch failed"))
    assert "the workflow-list fetch failed" in report
    assert "this audit is INCOMPLETE" in report

    clean = bp.render(_measured_doc(
        monkeypatch, gh_error_count=0, partial_kind=None, partial_reason=None))
    assert "INCOMPLETE" not in clean and "marginally fewer runs" not in clean


def test_the_report_names_gap_workflows_even_if_the_reason_forgot(monkeypatch):
    """Belt and braces: the by-name guarantee is re-derived from the STAMPED lists, so it
    holds for any findings doc however its `partial_reason` was phrased."""
    report = bp.render(_measured_doc(
        monkeypatch, gh_error_count=1, partial_kind=cr._PARTIAL_WORKFLOW_MISSING,
        partial_reason="1 gh API call(s) failed during collection",
        run_list_fetch_failures=[{"workflow_file": _VANISHED,
                                  "fetch": "success run sample"}]))
    assert _VANISHED in report


def test_a_pre_partial_kind_artifact_keeps_its_legacy_severity_in_the_report(monkeypatch):
    """The committed worked examples predate `partial_kind` (they now carry it, but an
    artifact from an older skill checkout won't). Severity is DERIVED from the data by
    the collector's own rule, so a legacy doc with a bare failed-call count still reads
    as a minor caveat, while one carrying a run-list gap reads LOUD."""
    doc = _measured_doc(monkeypatch, gh_error_count=4,
                        partial_reason="4 gh API call(s) failed during collection")
    doc["data_sources"].pop("partial_kind")
    thinned = bp.render(doc)
    assert "marginally fewer runs" in thinned and "INCOMPLETE" not in thinned

    doc = _measured_doc(monkeypatch, gh_error_count=1,
                        partial_reason="1 gh API call(s) failed during collection",
                        run_list_fetch_failures=[{"workflow_file": _VANISHED,
                                                  "fetch": "success run sample"}])
    doc["data_sources"].pop("partial_kind")
    loud = bp.render(doc)
    assert "this audit is INCOMPLETE" in loud and _VANISHED in loud
    assert "marginally fewer runs" not in loud


def test_a_static_only_run_is_neither_minimized_nor_alarmed(monkeypatch):
    """`static_only` / `gh_unavailable` are not holes in a measured audit — the gh pass
    never ran, which the report already says up top. State the reason; add nothing. And
    an empty spine there IS allowed to read as a quiet repo, because nothing broke."""
    monkeypatch.setattr(cr, "GhClient", _TwoWorkflowClient)
    out = cr.collect({"repo": "o/r", "findings": [{"id": "f1", "workflow_file": _GATE}]},
                     None, max_runs=8, shallow_runs=8)
    assert out["data_sources"]["partial_kind"] == cr._PARTIAL_STATIC_ONLY
    report = bp.render(out)
    assert "no --repo supplied; static-only run" in report
    assert "INCOMPLETE" not in report
    assert "marginally fewer runs" not in report
    assert "Collection FAILED" not in report


# ---------------------------------------------------------------------------
# 9. A MALFORMED body must not read as an empty repo.
#
# `_paginate`'s page-1 empty exception tested only that the list KEY was PRESENT, not
# that its value was an empty LIST. So `{"workflows": "oops"}` / `{"workflows": null}`
# returned `[]` with errors == 0 — via `_list_workflows` that means "this repo runs
# NOTHING", and the audit rendered static-only with NO coverage note at all.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", ['{"workflows": "oops"}',
                                  '{"workflows": {"1": "x"}}',
                                  '{"workflows": null}',
                                  '{"workflows": null, "total_count": 103}',
                                  '{"foo": 1}'])
def test_a_malformed_workflow_list_aborts_instead_of_reading_as_an_empty_repo(
        monkeypatch, body):
    """Drives the REAL `GhClient` (stubbed subprocess), so `_paginate` is the code under
    test, and asserts on the RENDERED report."""
    def _fake_run(cmd, *a, **kw):
        if cmd[:2] == ["gh", "auth"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"HTTP/2.0 200 OK\r\nServer: github.com\r\n\r\n{body}",
            stderr="")

    monkeypatch.setattr(cr.subprocess, "run", _fake_run)
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    ds = out["data_sources"]

    assert ds["gh_error_count"] >= 1, (
        "a malformed page-1 body was laundered into an empty list with ZERO errors — "
        "no coverage note, no banner, a clean-looking report built on nothing")
    assert ds["partial_kind"] == cr._PARTIAL_COLLECTION_FAILED
    report = bp.render(out)
    assert "Collection FAILED" in report
    assert "an archived, brand-new, or low-activity repo" not in report


def test_a_genuinely_empty_collection_still_returns_empty(monkeypatch):
    """The other side of the fix: a REAL empty list, and an explicit `total_count: 0`,
    must still be read as "nothing ran" — not turned into a false coverage gap."""
    for body in ('{"workflows": []}', '{"total_count": 0}'):
        def _fake_run(cmd, *a, **kw):
            if cmd[:2] == ["gh", "auth"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"HTTP/2.0 200 OK\r\n\r\n{body}", stderr="")

        monkeypatch.setattr(cr.subprocess, "run", _fake_run)
        client = cr.GhClient()
        assert cr._paginate(client, "repos/o/r/actions/workflows", "workflows") == []
        assert client.errors == 0, f"{body} is genuinely empty, not a coverage gap"


def test_failed_gh_probe_skips_successful_accounts(monkeypatch):
    status = (
        "github.example.com\n"
        "  ✓ Logged in to github.example.com account example (keyring)\n"
        "  - Active account: true\n"
        "github.com\n"
        "  X Failed to log in to github.com using token (GH_TOKEN)\n"
        "  - The token in GH_TOKEN is invalid.\n"
    )
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)
    monkeypatch.setattr(cr.subprocess, "run", _real_gh_probe(status, 1))
    out = cr.collect(_doc(), "o/r", max_runs=8, shallow_runs=8)
    report = bp.render(out)
    assert "Failed to log in to github.com using token (GH_TOKEN)" in report
    assert "Logged in to github.example.com" not in report

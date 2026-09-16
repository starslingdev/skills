"""Unit tests for ``GhClient`` in collect_runs.py.

``GhClient`` is the wrapper every GitHub API byte flows through, and its
error-classification logic — the ``allow_missing`` rule deciding whether a
failure counts as "data unavailable" (fine) or a collection error (trips the
report's partial-coverage banner) — had zero direct tests before this file.
These pin the live-path semantics (no ``CI_SPEEDUP_GH_FIXTURES`` replay branch
in this checkout) by monkeypatching ``collect_runs.subprocess.run`` with a
canned ``subprocess.CompletedProcess``.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_gh_client.py
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time

import pytest

import collect_runs
from collect_runs import GhClient


@pytest.fixture(autouse=True)
def _no_fixture_replay(monkeypatch):
    # Harmless if plan 001 (fixture-replay branch) hasn't landed in this
    # checkout — guards these tests against that env var if it has.
    monkeypatch.delenv("CI_SPEEDUP_GH_FIXTURES", raising=False)


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["gh", "api", "whatever"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# A REALISTIC `gh api -i` stdout block, transcribed from a captured live failure
# (`gh api -i repos/o/r/actions/jobs/<bogus>/logs`): gh prints the status line
# terminated by a bare \n, then CRLF-terminated headers, then a blank CRLF line,
# then the body — on SUCCESS AND ON FAILURE alike. That is the whole reason
# `_invoke` runs with `-i`: the server's own retry guidance arrives WITH the
# failure, so no second request has to be fired at the endpoint that just
# rate-limited us.
_REAL_HEADER_TAIL = (
    "Access-Control-Allow-Origin: *\r\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "Server: github.com\r\n"
    "X-Github-Media-Type: github.v3; format=json\r\n"
)


def _gh_i(status_line: str, *, headers: str = "", body: str = "") -> str:
    return f"{status_line}\n{_REAL_HEADER_TAIL}{headers}\r\n{body}"


def _ok(body: str) -> str:
    return _gh_i("HTTP/2.0 200 OK", body=body)


def _patch_run(monkeypatch, fake):
    """Install `fake` (callable or exception instance/class) as collect_runs.subprocess.run."""
    if isinstance(fake, BaseException) or (isinstance(fake, type) and issubclass(fake, BaseException)):
        def _raiser(*_a, **_kw):
            raise fake
        monkeypatch.setattr(collect_runs.subprocess, "run", _raiser)
    else:
        monkeypatch.setattr(collect_runs.subprocess, "run", fake)


# -----------------------------------------------------------------------
# json()
# -----------------------------------------------------------------------

def test_json_happy_path(monkeypatch):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=0, stdout='{"a": 1}'))
    client = GhClient()
    result = client.json("repos/o/r")
    assert result == {"a": 1}
    assert client.queries == 1
    assert client.errors == 0


def test_json_nonzero_rc_allow_missing_false(monkeypatch):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=1, stderr="404 Not Found"))
    client = GhClient()
    result = client.json("repos/o/r/branches/main/protection", allow_missing=False)
    assert result is None
    assert client.queries == 1
    assert client.errors == 1


def test_json_nonzero_rc_allow_missing_true(monkeypatch):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=1, stderr="404 Not Found"))
    client = GhClient()
    result = client.json("repos/o/r/branches/main/protection", allow_missing=True)
    assert result is None
    assert client.queries == 1
    assert client.errors == 0


def test_json_non_json_body_allow_missing_false(monkeypatch):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=0, stdout="not json"))
    client = GhClient()
    result = client.json("repos/o/r", allow_missing=False)
    assert result is None
    assert client.queries == 1
    assert client.errors == 1


def test_json_non_json_body_allow_missing_true(monkeypatch):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=0, stdout="not json"))
    client = GhClient()
    result = client.json("repos/o/r/rulesets", allow_missing=True)
    assert result is None
    assert client.queries == 1
    assert client.errors == 0


@pytest.mark.parametrize("allow_missing", [False, True])
def test_json_missing_binary_is_terminal(monkeypatch, allow_missing):
    """gh isn't installed: retrying can't help, and `allow_missing` still applies
    (a rulesets probe on a machine with no gh is "data unavailable")."""
    _patch_run(monkeypatch, FileNotFoundError("gh not found"))
    client = GhClient()
    result = client.json("repos/o/r", allow_missing=allow_missing)
    assert result is None
    assert client.queries == 1
    assert client.errors == (0 if allow_missing else 1)


@pytest.mark.parametrize("exc", [
    OSError("EMFILE: too many open files"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
])
@pytest.mark.parametrize("allow_missing", [False, True])
def test_json_oserror_or_undecodable_body_is_counted_not_raised(monkeypatch, exc, allow_missing):
    """WAVE-SAFETY: the parallel pass runs `_invoke` on the shared pool's threads, so a
    failure to SPAWN gh (EMFILE/ENOMEM forking the 8th concurrent process — an `OSError`
    that is NOT `FileNotFoundError`) or to DECODE its `text=True` stdout (a non-UTF8 job
    log → `UnicodeDecodeError`) must be caught at the choke point, never left to escape
    the pool's `map()` and discard the whole wave. It is a real collection error — never
    the expected-absent 404 `allow_missing` declares — so it is ALWAYS counted, and never
    retried (an immediate re-run of the same call won't clear a resource/decoding fault)."""
    _patch_run(monkeypatch, exc)
    client = GhClient()
    result = client.json("repos/o/r", allow_missing=allow_missing)
    assert result is None
    assert client.queries == 1
    assert client.errors == 1


@pytest.mark.parametrize("allow_missing", [False, True])
def test_json_timeout_is_transient_so_it_is_retried_and_counted(monkeypatch, no_sleep, allow_missing):
    """A timeout means the data EXISTS and we didn't get it — a coverage gap, the
    same as a 5xx. It counts regardless of `allow_missing` (which only ever meant
    "a 404 here is expected")."""
    _patch_run(monkeypatch, subprocess.TimeoutExpired(cmd="gh", timeout=60))
    client = GhClient()
    result = client.json("repos/o/r", allow_missing=allow_missing)
    assert result is None
    assert client.queries == 1
    assert client.errors == 1


# -----------------------------------------------------------------------
# text()
# -----------------------------------------------------------------------

def test_text_happy_path(monkeypatch):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=0, stdout="log body"))
    client = GhClient()
    result = client.text("repos/o/r/actions/jobs/1/logs")
    assert result == "log body"
    assert client.queries == 1
    assert client.errors == 0


def test_text_nonzero_rc(monkeypatch):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=1, stderr="boom"))
    client = GhClient()
    result = client.text("repos/o/r/actions/jobs/1/logs")
    assert result is None
    assert client.queries == 1
    assert client.errors == 1


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(cmd="gh", timeout=90),
    FileNotFoundError("gh not found"),
])
def test_text_raises(monkeypatch, no_sleep, exc):
    _patch_run(monkeypatch, exc)
    client = GhClient()
    result = client.text("repos/o/r/actions/jobs/1/logs")
    assert result is None
    assert client.queries == 1
    assert client.errors == 1


# -----------------------------------------------------------------------
# available()
# -----------------------------------------------------------------------

def test_available_true(monkeypatch):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=0))
    client = GhClient()
    assert client.available() is True
    # available() deliberately never _bump()s — it is a probe, not a data query.
    assert client.queries == 0
    assert client.errors == 0


def test_available_false_nonzero_rc(monkeypatch):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=1))
    client = GhClient()
    assert client.available() is False
    assert client.queries == 0
    assert client.errors == 0


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(cmd="gh", timeout=10),
    FileNotFoundError("gh not found"),
])
def test_available_false_raises(monkeypatch, exc):
    _patch_run(monkeypatch, exc)
    client = GhClient()
    assert client.available() is False
    assert client.queries == 0
    assert client.errors == 0


# -----------------------------------------------------------------------
# counter arithmetic under concurrent calls
# -----------------------------------------------------------------------

def test_bump_counts_are_exact_under_concurrent_calls():
    """Verify `_bump`'s counter arithmetic is exact when called concurrently.

    NOTE: this does NOT prove the `self._lock` is load-bearing. Under CPython's
    GIL a 20-thread trivial-body increment does not manifest the read-modify-write
    race the lock exists to prevent (mutation testing confirms removing the lock
    still passes this). It only pins that concurrent `_bump` calls produce the
    expected totals (queries==20, errors==0) — arithmetic, not lock correctness.
    """
    client = GhClient()
    threads = [threading.Thread(target=lambda: client._bump(query=True)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert client.queries == 20
    assert client.errors == 0


# =======================================================================
# BUG 1 — silent pagination truncation
#
# The list endpoints used to request `per_page=100` and keep whatever page 1
# held. Measured on better-auth/better-auth @ 6f20f44: the check-runs endpoint
# reported `total_count = 103` and returned 100 — three checks silently dropped
# from the critical-path sample. If one of them were the merge pole, the computed
# gate would be WRONG. These pin full pagination, and pin that a mid-walk failure
# is a LOUD gap (None), never a short-but-plausible list.
# =======================================================================

class _PagingClient:
    """A GhClient stand-in that serves canned pages and records every endpoint.

    `json()` AND `_bump()`: `_paginate` composes over both — `json()` counts the
    failures IT sees, while the malformed-page and page-ceiling paths are gaps
    `_paginate` itself discovers and must therefore tally."""

    def __init__(self, pages: dict[str, object]) -> None:
        self.pages = pages
        self.calls: list[str] = []
        self.queries = 0
        self.errors = 0

    def _bump(self, *, query: bool = False, error: bool = False) -> None:
        self.queries += int(query)
        self.errors += int(error)

    def json(self, endpoint: str, allow_missing: bool = False):
        self.calls.append(endpoint)
        self.queries += 1
        doc = self.pages.get(endpoint)
        if doc is None:
            if not allow_missing:
                self.errors += 1
            return None
        return doc


def _check_page(items: list[dict], total: int) -> dict:
    return {"total_count": total, "check_runs": items}


def _checks(lo: int, hi: int) -> list[dict]:
    return [{"id": i, "name": f"check-{i}"} for i in range(lo, hi)]


def test_check_runs_paginates_past_100_the_measured_103_case():
    """The better-auth case: total_count=103, page 1 holds 100. All 103 must land."""
    client = _PagingClient({
        "repos/o/r/commits/sha1/check-runs?per_page=100": _check_page(_checks(0, 100), 103),
        "repos/o/r/commits/sha1/check-runs?per_page=100&page=2": _check_page(_checks(100, 103), 103),
    })
    got = collect_runs._fetch_check_runs(client, "o/r", "sha1")
    assert got is not None
    assert len(got) == 103, "check-runs truncated at the page boundary — a dropped check could be the merge pole"
    assert [c["id"] for c in got] == list(range(103))
    assert len(client.calls) == 2
    assert client.errors == 0


def test_run_jobs_paginates_past_100():
    """Same latent bug on the jobs endpoint — a big matrix run exceeds 100 jobs."""
    jobs = lambda lo, hi: [{"id": i, "name": f"job-{i}"} for i in range(lo, hi)]  # noqa: E731
    client = _PagingClient({
        "repos/o/r/actions/runs/7/jobs?per_page=100": {"total_count": 142, "jobs": jobs(0, 100)},
        "repos/o/r/actions/runs/7/jobs?per_page=100&page=2": {"total_count": 142, "jobs": jobs(100, 142)},
    })
    got = collect_runs._fetch_run_jobs(client, "o/r", 7)
    assert got is not None and len(got) == 142


def test_run_jobs_filtered_paginates_and_keeps_the_filter_param():
    jobs = lambda lo, hi: [{"id": i} for i in range(lo, hi)]  # noqa: E731
    client = _PagingClient({
        "repos/o/r/actions/runs/7/jobs?per_page=100&filter=all": {"total_count": 120, "jobs": jobs(0, 100)},
        "repos/o/r/actions/runs/7/jobs?per_page=100&filter=all&page=2": {"total_count": 120, "jobs": jobs(100, 120)},
    })
    got = collect_runs._fetch_run_jobs_all_attempts(client, "o/r", 7)
    assert got is not None and len(got) == 120


def test_paginate_first_page_endpoint_is_byte_identical_to_the_legacy_string():
    """The record/replay fixture names key off the endpoint string, so page 1 MUST
    keep the exact pre-pagination spelling or the committed corpus stops replaying."""
    client = _PagingClient({
        "repos/o/r/actions/runs/7/jobs?per_page=100": {"total_count": 2, "jobs": [{"id": 1}, {"id": 2}]},
    })
    collect_runs._fetch_run_jobs(client, "o/r", 7)
    assert client.calls == ["repos/o/r/actions/runs/7/jobs?per_page=100"]


def test_paginate_mid_walk_failure_is_a_loud_gap_not_a_short_list(caplog):
    """Page 2 fails. Returning the 100 items from page 1 would hand the caller a
    truncated sample that LOOKS complete — the silent-drop failure class. The whole
    fetch must fail (None), which the caller already surfaces as a coverage gap."""
    client = _PagingClient({
        "repos/o/r/commits/sha1/check-runs?per_page=100": _check_page(_checks(0, 100), 103),
        # page 2 absent -> json() returns None
    })
    with caplog.at_level(logging.WARNING, logger="collect_runs"):
        got = collect_runs._fetch_check_runs(client, "o/r", "sha1")
    assert got is None, "a partial pagination walk must never be returned as a complete list"
    assert client.errors == 1
    assert any("abandoning the whole fetch" in r.getMessage() for r in caplog.records)


# =======================================================================
# BUG 2 — the phantom partial-coverage banner
#
# GitHub 404s the logs of a job that never ran. The log sites fetched them
# anyway, and `text()` had no `allow_missing`, so 4 EXPECTED 404s on a
# fully-successful better-auth run became `gh_error_count: 4` →
# `partial_reason: "4 gh API call(s) failed during collection"` → a
# partial-coverage banner on a clean run.
# =======================================================================

@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "SKIPPED"])
def test_skipped_job_log_is_never_fetched_and_is_not_an_error(monkeypatch, conclusion):
    def _boom(*_a, **_kw):
        raise AssertionError("must not spawn gh for a job that never ran")
    _patch_run(monkeypatch, _boom)
    client = GhClient()
    assert collect_runs._fetch_job_log(client, "o/r", {"id": 5, "conclusion": conclusion}) is None
    assert client.queries == 0
    assert client.errors == 0


def test_four_skipped_jobs_do_not_trip_the_partial_coverage_banner(monkeypatch):
    """The measured better-auth shape: 4 skipped jobs in the drilled set. Pre-fix
    this produced errors==4 and a phantom banner; the banner is keyed off
    `client.errors` (see collect()'s `partial_reason`), so errors must be 0."""
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=0, stdout="log body"))
    client = GhClient()
    jobs = [{"id": i, "conclusion": "skipped"} for i in range(4)]
    jobs.append({"id": 99, "conclusion": "success"})
    logs = [collect_runs._fetch_job_log(client, "o/r", j) for j in jobs]
    assert logs == [None, None, None, None, "log body"]
    assert client.errors == 0
    partial_reason = None if client.errors == 0 else f"{client.errors} gh API call(s) failed"
    assert partial_reason is None


def test_text_allow_missing_suppresses_the_expected_404(monkeypatch):
    """`text()` now mirrors `json()`'s allow_missing contract."""
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=1, stderr="gh: Not Found (HTTP 404)"))
    client = GhClient()
    assert client.text("repos/o/r/actions/jobs/1/logs", allow_missing=True) is None
    assert client.queries == 1
    assert client.errors == 0


# =======================================================================
# BUG 3 — silent rate-limit drops
#
# There was no retry, no backoff and no 403/429 detection: a GitHub secondary
# rate-limit block was indistinguishable from a 404 and logged at DEBUG
# (invisible at the default INFO level). A blocked run quietly shrank its own
# sample and still rendered a confident, plausible, WRONG report.
# =======================================================================

_SECONDARY = ("gh: You have exceeded a secondary rate limit and have been blocked "
              "from content creation. Please retry your request again later. (HTTP 403)")
_PRIMARY = "gh: API rate limit exceeded for user ID 1. (HTTP 403)"
_TOO_MANY = "gh: Too Many Requests (HTTP 429)"
# The same secondary-limit message with NO parseable HTTP status — gh wrapping the
# error, a proxy rewriting it, or gh changing its format. The KEYWORD is the signal.
_SECONDARY_NO_STATUS = ("gh: You have exceeded a secondary rate limit. "
                        "Please wait a few minutes before you try again.")

_RATE_LIMITED = _gh_i("HTTP/2.0 403 Forbidden",
                      body='{"message":"You have exceeded a secondary rate limit."}')


@pytest.fixture
def no_sleep(monkeypatch):
    """Capture backoff sleeps instead of serving them."""
    slept: list[float] = []
    monkeypatch.setattr(collect_runs.time, "sleep", lambda s: slept.append(s))
    return slept


def _script(responses):
    """subprocess.run stub walking `responses` in order (the last one repeats)."""
    seq = list(responses)

    def _run(cmd, *a, **kw):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return _run


def test_every_live_call_asks_for_headers_so_no_second_probe_is_ever_fired(monkeypatch):
    """`_invoke` runs `gh api -i` — ONE request that carries both the body and the
    server's retry guidance. The alternative (probe the endpoint again with `-i`
    after it rate-limits you, from every blocked worker) is precisely what GitHub's
    secondary-limit guidance says not to do, and it was uncounted in `queries`."""
    cmds = []

    def _run(cmd, *a, **kw):
        cmds.append(list(cmd))
        return _completed(returncode=0, stdout=_ok('{"a": 1}'))
    _patch_run(monkeypatch, _run)
    client = GhClient()
    assert client.json("repos/o/r") == {"a": 1}
    assert cmds == [["gh", "api", "-i", "--allow-escape-sequences",
                     "repos/o/r"]], (
        "one call, carrying both `-i` (the server's own retry guidance, so a "
        "blocked worker never re-probes) and the escape-sequence flag (without "
        "which gh refuses to print a coloured job log at all)")
    assert client.queries == 1


def test_the_header_split_returns_the_body_untouched():
    """A job log is parsed downstream; a botched header/body split would corrupt it
    silently. Pin the exact captured wire shape — and pin that stdout with NO header
    block at all degrades to the raw body rather than losing it."""
    body = "2026-07-11T19:13:19.5Z Current runner version: '2.335.1'\n\nsecond para\n"
    status, headers, got = collect_runs._split_headers_body(
        _gh_i("HTTP/1.1 200 OK", headers="X-Ratelimit-Reset: 1783800312\r\n", body=body))
    assert status == 200
    assert headers["x-ratelimit-reset"] == "1783800312"
    assert got == body, "the body must survive the split byte-for-byte"
    assert collect_runs._split_headers_body("raw body, no headers") == (
        None, {}, "raw body, no headers")


def test_the_header_split_on_a_real_job_log_success_response():
    """The 404 path was the only one with wire evidence. The SUCCESS path for job logs
    (`/jobs/{id}/logs` -> 302 -> blob storage) carries EVERY log byte through this
    seam, so pin it too: gh's Go client follows the redirect and prints only the FINAL
    response, so there is exactly ONE header block — and the log (which contains blank
    lines, colons and `Key: value`-shaped lines of its own) must come back whole."""
    log = (
        "2026-07-11T19:13:19.5Z ##[group]Run actions/checkout@v4\n"
        "2026-07-11T19:13:19.5Z with:\n"
        "2026-07-11T19:13:19.5Z   fetch-depth: 0\n"
        "\n"
        "2026-07-11T19:13:21.0Z Cache-Control: no-store\n"     # a header-SHAPED log line
        "2026-07-11T19:13:22.7Z ##[endgroup]\n")
    stdout = (
        "HTTP/2.0 200 OK\r\n"
        "content-type: text/plain; charset=utf-8\r\n"
        "x-ratelimit-remaining: 4987\r\n"
        "\r\n"
        + log)
    status, headers, body = collect_runs._split_headers_body(stdout)
    assert status == 200
    assert headers["x-ratelimit-remaining"] == "4987"
    assert body == log, "a job log must cross the seam whole"
    # The split point is the FIRST blank line after the status line — a blank line
    # INSIDE the log (and the `Cache-Control:` line after it) can never move it.
    assert "Cache-Control: no-store" in body
    assert "cache-control" not in headers


def test_an_unterminated_header_block_returns_the_RAW_stdout_not_an_empty_body():
    """The truncation case the docstring promised but the code did not deliver: a
    status line + headers with NO terminating blank line fell out of the parse loop
    with `body = ""`. On a SUCCESSFUL (`rc == 0`) call that hands back an EMPTY body —
    a job log reading as empty rather than absent, so `_persist_pole_logs` records a
    fetched-but-empty log instead of a gap. Degrade to the RAW stdout instead: nothing
    is ever silently dropped."""
    stdout = "HTTP/2.0 200 OK\nx-ratelimit-remaining: 42\nthe body, unterminated"
    status, headers, body = collect_runs._split_headers_body(stdout)
    assert body == stdout, "an unterminated header block must not eat the body"
    assert (status, headers) == (None, {})


def test_the_header_split_does_not_materialize_every_line_of_a_big_body():
    """PERF (it matters on exactly the endpoint whose bodies are large): the split
    scans for the terminator, it does not line-split the whole payload. Pinned
    behaviorally — a 4MB log must come back identical, and fast."""
    log = "x" * (4 * 1024 * 1024) + "\n\ntrailing\n"
    stdout = "HTTP/2.0 200 OK\r\ncontent-type: text/plain\r\n\r\n" + log
    t0 = time.perf_counter()
    status, _headers, body = collect_runs._split_headers_body(stdout)
    elapsed = time.perf_counter() - t0
    assert status == 200 and body == log
    assert elapsed < 0.5, f"the split took {elapsed:.3f}s on a 4MB body"


@pytest.mark.parametrize("stderr", [_SECONDARY, _PRIMARY, _TOO_MANY])
def test_rate_limit_is_classified_not_confused_with_a_404(stderr):
    assert collect_runs._classify_gh_failure(stderr) == "rate_limit"
    assert collect_runs._classify_gh_failure("gh: Not Found (HTTP 404)") == "not_found"
    # A plain 403 (no admin on the audited repo) is NOT a rate limit.
    assert collect_runs._classify_gh_failure(
        "gh: Must have admin rights to Repository. (HTTP 403)") == "forbidden"
    assert collect_runs._classify_gh_failure("gh: Bad gateway (HTTP 502)") == "server_error"


def test_a_rate_limit_with_no_parseable_status_is_still_a_rate_limit():
    """The classifier used to compute `limited` and then IGNORE it unless the HTTP
    status parsed (`status == 403 and limited`). A stderr that literally says
    "exceeded a secondary rate limit" then fell through to `other`: no retry, and on
    an `allow_missing` call not even counted — the exact silent-truncation bug this
    PR exists to fix, reachable through the one line that decides."""
    assert collect_runs._classify_gh_failure(_SECONDARY_NO_STATUS) == "rate_limit"
    assert collect_runs._classify_gh_failure("You have triggered an abuse detection "
                                             "mechanism.") == "rate_limit"


def test_a_status_less_rate_limit_is_retried_and_counted(monkeypatch, no_sleep):
    """…and the classification must actually reach the retry/count path."""
    calls = []

    def _run(cmd, *a, **kw):
        calls.append(cmd)
        return _completed(returncode=1, stdout="", stderr=_SECONDARY_NO_STATUS)
    _patch_run(monkeypatch, _run)
    client = GhClient()
    assert client.json("repos/o/r/rulesets", allow_missing=True) is None
    assert len(calls) == collect_runs._GH_MAX_ATTEMPTS, "a rate limit must be retried"
    assert client.errors == 1, "a rate-limit block is a coverage gap, even allow_missing"


def test_rate_limit_is_retried_logged_at_warning_and_then_succeeds(monkeypatch, no_sleep, caplog):
    _patch_run(monkeypatch, _script([
        _completed(returncode=1, stdout=_RATE_LIMITED, stderr=_SECONDARY),
        _completed(returncode=0, stdout=_ok('{"ok": 1}')),
    ]))
    client = GhClient()
    with caplog.at_level(logging.WARNING, logger="collect_runs"):
        got = client.json("repos/o/r/actions/runs/1/jobs?per_page=100")
    assert got == {"ok": 1}
    assert client.errors == 0                       # it eventually succeeded
    assert no_sleep, "a secondary rate limit must be backed off, not hammered"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a rate-limit block must be WARNING — at DEBUG it is invisible by default"
    assert "rate limit" in warnings[0].getMessage().lower()


def test_rate_limit_honors_retry_after_header(monkeypatch, no_sleep):
    """`retry-after` IS the server's answer for THIS block — taken as given (plus
    jitter), read off the failed response itself, with no extra request."""
    _patch_run(monkeypatch, _script([
        _completed(returncode=1, stderr=_SECONDARY,
                   stdout=_gh_i("HTTP/2.0 403 Forbidden", headers="Retry-After: 42\r\n")),
    ]))
    client = GhClient()
    assert client.json("repos/o/r") is None
    assert 42.0 <= no_sleep[0] <= 42.0 + collect_runs._GH_JITTER_S
    # Every attempt is blocked, so the retries exhaust: a REAL coverage gap. `errors` is
    # the only channel to the partial-coverage banner, so every failure-to-exhaustion
    # test asserts it — a test that checks only the None return is half a test.
    assert client.errors == 1


def test_rate_limit_honors_x_ratelimit_reset(monkeypatch, no_sleep):
    reset = time.time() + 120
    _patch_run(monkeypatch, _script([
        _completed(returncode=1, stderr=_SECONDARY,
                   stdout=_gh_i("HTTP/2.0 403 Forbidden",
                                headers=f"X-Ratelimit-Reset: {int(reset)}\r\n")),
    ]))
    client = GhClient()
    assert client.json("repos/o/r") is None
    assert 110 <= no_sleep[0] <= 130
    assert client.errors == 1, "an exhausted rate limit is a counted coverage gap"


def test_an_almost_expired_reset_still_waits_githubs_one_minute_floor(monkeypatch, no_sleep):
    """A SECONDARY limit returns headers for the PRIMARY hourly bucket. Honouring a
    3-seconds-from-rollover `x-ratelimit-reset` literally would sleep 3s, retry into
    the same (≥60s) secondary block, and burn all 3 attempts in ~3 seconds. The
    header-derived wait is floored at GitHub's documented one minute."""
    _patch_run(monkeypatch, _script([
        _completed(returncode=1, stderr=_SECONDARY,
                   stdout=_gh_i("HTTP/2.0 403 Forbidden",
                                headers=f"X-Ratelimit-Reset: {int(time.time()) + 3}\r\n")),
    ]))
    client = GhClient()
    assert client.json("repos/o/r") is None
    assert no_sleep[0] >= collect_runs._GH_RATE_LIMIT_FLOOR_S
    assert client.errors == 1, "an exhausted rate limit is a counted coverage gap"


def test_a_huge_reset_is_clamped_to_the_max_backoff(monkeypatch, no_sleep):
    """A PRIMARY-limit reset can be an hour out. Hanging the whole audit on it is
    worse than spending the attempt budget and surfacing a loud gap."""
    _patch_run(monkeypatch, _script([
        _completed(returncode=1, stderr=_SECONDARY,
                   stdout=_gh_i("HTTP/2.0 403 Forbidden",
                                headers=f"X-Ratelimit-Reset: {int(time.time()) + 3600}\r\n")),
    ]))
    client = GhClient()
    assert client.json("repos/o/r") is None
    assert all(s <= collect_runs._GH_MAX_BACKOFF_S for s in no_sleep)
    assert no_sleep[0] == pytest.approx(collect_runs._GH_MAX_BACKOFF_S)
    assert client.errors == 1, "an exhausted rate limit is a counted coverage gap"


def test_rate_limit_without_headers_waits_githubs_documented_one_minute_floor(monkeypatch, no_sleep):
    _patch_run(monkeypatch, _script([_completed(returncode=1, stderr=_SECONDARY)]))
    client = GhClient()
    assert client.json("repos/o/r") is None
    assert no_sleep[0] >= collect_runs._GH_RATE_LIMIT_FLOOR_S
    assert client.errors == 1, "an exhausted rate limit is a counted coverage gap"


def test_a_rate_limit_block_pauses_every_worker_not_just_the_one_that_hit_it(monkeypatch):
    """The fetch pool shares ONE client. Without a shared deadline, 8 threads each
    discover the same block independently, each hammers the endpoint GitHub just told
    us to back off from, and the audit ends with 8 coverage gaps instead of one pause.
    `_record_block` publishes it; `_sleep_until_unblocked` makes the others wait."""
    slept: list[float] = []
    monkeypatch.setattr(collect_runs.time, "sleep", lambda s: slept.append(s))
    client = GhClient()
    client._record_block(60.0)               # worker A hits the limit
    client._sleep_until_unblocked()          # worker B, mid-flight, must wait it out
    assert slept and slept[0] == pytest.approx(60.0, abs=1.0)


def test_a_worker_that_wakes_into_an_EXTENDED_block_sleeps_again(monkeypatch):
    """`_sleep_until_unblocked` used to read the deadline once, sleep, and return. If
    another worker EXTENDED the block while this thread slept, it woke straight back
    into a live block and burned an attempt on a call GitHub was still refusing.
    Re-check after waking — but serve each DISTINCT deadline only once, so a
    `time.sleep` that returns early (a signal, or a test stub) can't spin."""
    slept: list[float] = []
    client = GhClient()

    def _sleep(s):
        slept.append(s)
        if len(slept) == 1:                  # another worker extends the block
            client._record_block(120.0)      # while this one is asleep

    monkeypatch.setattr(collect_runs.time, "sleep", _sleep)
    client._record_block(30.0)
    client._sleep_until_unblocked()
    assert len(slept) == 2, "a block extended mid-sleep must be waited out too"
    assert slept[1] > slept[0]


def test_rate_limit_backoff_is_jittered_so_workers_do_not_wake_in_lockstep(monkeypatch):
    """Identical waits across N blocked workers = a thundering herd back into the
    limit. Two computations of the same block must not be byte-identical."""
    client = GhClient()
    headers = {"retry-after": "30"}
    waits = {client._rate_limit_wait(headers, 1) for _ in range(20)}
    assert len(waits) > 1, "no jitter on the rate-limit path — 8 threads wake together"
    assert all(30.0 <= w <= 30.0 + collect_runs._GH_JITTER_S for w in waits)


def test_exhausted_rate_limit_is_a_loud_coverage_gap_not_a_silent_none(monkeypatch, no_sleep, caplog):
    """The headline of bug 3: after the retries are spent, the call must be counted
    as a REAL error (tripping the report's partial-coverage disclosure) and shouted
    at WARNING — never a quiet None that shrinks the sample behind a clean report."""
    calls = []

    def _run(cmd, *a, **kw):
        calls.append(cmd)
        return _completed(returncode=1, stdout=_RATE_LIMITED, stderr=_SECONDARY)
    _patch_run(monkeypatch, _run)
    client = GhClient()
    with caplog.at_level(logging.WARNING, logger="collect_runs"):
        got = client.json("repos/o/r/commits/sha/check-runs?per_page=100")
    assert got is None
    assert len(calls) == collect_runs._GH_MAX_ATTEMPTS
    assert client.errors == 1, "a rate-limit block is a coverage gap, not an empty result"
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("GAVE UP" in m and "COVERAGE GAP" in m for m in loud)


def test_rate_limit_counts_as_an_error_even_on_an_allow_missing_endpoint(monkeypatch, no_sleep):
    """`allow_missing` means 'a 404 here is EXPECTED' — not 'swallow anything'. A
    rate-limit block on the rulesets endpoint is still a real gap."""
    _patch_run(monkeypatch, _script([_completed(returncode=1, stderr=_SECONDARY)]))
    client = GhClient()
    assert client.json("repos/o/r/rulesets", allow_missing=True) is None
    assert client.errors == 1


def test_transient_5xx_is_retried_with_backoff_then_succeeds(monkeypatch, no_sleep):
    _patch_run(monkeypatch, _script([
        _completed(returncode=1, stderr="gh: Bad gateway (HTTP 502)"),
        _completed(returncode=0, stdout=_ok("log body")),
    ]))
    client = GhClient()
    assert client.text("repos/o/r/actions/jobs/1/logs") == "log body"
    assert client.errors == 0
    assert 1.0 <= no_sleep[0] <= 1.5           # 1s base + jitter


def test_exhausted_5xx_is_retried_exactly_max_attempts_times_and_counted(monkeypatch, no_sleep):
    """`errors == 1` alone is TAUTOLOGICAL — it held before this PR too (any failure
    bumped it). What must be pinned is that the 5xx was RETRIED to the budget."""
    calls = []

    def _run(cmd, *a, **kw):
        calls.append(cmd)
        return _completed(returncode=1, stderr="gh: Server Error (HTTP 500)")
    _patch_run(monkeypatch, _run)
    client = GhClient()
    assert client.text("repos/o/r/actions/jobs/1/logs") is None
    assert len(calls) == collect_runs._GH_MAX_ATTEMPTS
    assert len(no_sleep) == collect_runs._GH_MAX_ATTEMPTS - 1
    assert client.errors == 1


def test_a_timeout_is_retried_and_counted_even_when_allow_missing(monkeypatch, no_sleep):
    """A timeout is TRANSIENT — the data exists, we just didn't get it (the same
    meaning as a 5xx). It used to be a fully silent drop on an `allow_missing` call:
    no retry, no bump, no print, no log at any level."""
    calls = []

    def _run(cmd, *a, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd="gh", timeout=90)
    _patch_run(monkeypatch, _run)
    client = GhClient()
    assert client.text("repos/o/r/actions/jobs/1/logs", allow_missing=True) is None
    assert len(calls) == collect_runs._GH_MAX_ATTEMPTS, "a timeout must be retried"
    assert client.errors == 1, "an exhausted timeout is a coverage gap, not an absence"


def test_gh_not_installed_is_terminal_and_never_retried(monkeypatch, no_sleep):
    calls = []

    def _run(cmd, *a, **kw):
        calls.append(cmd)
        raise FileNotFoundError("gh not found")
    _patch_run(monkeypatch, _run)
    client = GhClient()
    assert client.json("repos/o/r") is None
    assert len(calls) == 1, "retrying a missing binary can only waste time"
    assert client.errors == 1


def test_a_404_is_never_retried(monkeypatch, no_sleep):
    calls = []

    def _run(cmd, *a, **kw):
        calls.append(cmd)
        return _completed(returncode=1, stderr="gh: Not Found (HTTP 404)")
    _patch_run(monkeypatch, _run)
    client = GhClient()
    assert client.json("repos/o/r/branches/main/protection", allow_missing=True) is None
    assert len(calls) == 1
    assert client.errors == 0
    assert no_sleep == []


def test_text_rate_limit_is_retried_too(monkeypatch, no_sleep, caplog):
    """The policy lives at the single choke point, so `text()` inherits it."""
    _patch_run(monkeypatch, _script([
        _completed(returncode=1, stderr=_TOO_MANY),
        _completed(returncode=0, stdout=_ok("log body")),
    ]))
    client = GhClient()
    with caplog.at_level(logging.WARNING, logger="collect_runs"):
        assert client.text("repos/o/r/actions/jobs/1/logs") == "log body"
    assert no_sleep
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    # A block we successfully waited out is NOT a coverage gap — we got the data. Only
    # EXHAUSTING the retries counts, so the banner keeps meaning something.
    assert client.errors == 0


# =======================================================================
# BUG 4 — the fixes' own silent drops (this PR's review round)
#
# The first pass at bugs 1–3 left the SAME failure class open in five more
# places: the pagination ceiling, a malformed page, the conclusion-based log
# skip, and the `None -> []` launderers between the client and the audit.
# =======================================================================

def _run_page(items: list[dict], total: int | None = None) -> dict:
    doc: dict = {"workflow_runs": items}
    if total is not None:
        doc["total_count"] = total
    return doc


def test_the_page_ceiling_is_a_failure_not_a_short_list(caplog):
    """A client that serves a full page FOREVER. Hitting `_GH_MAX_PAGES` means the
    stop condition is broken, so the accumulated items are of unknown completeness.
    Returning them (with `errors == 0`, as this did) hands back a truncated list that
    LOOKS complete — the very bug `_paginate`'s own docstring forbids."""
    class _Endless:
        queries = 0

        def __init__(self):
            self.errors = 0
            self.calls = 0

        def _bump(self, *, query=False, error=False):
            self.errors += int(error)

        def json(self, endpoint, allow_missing=False):
            self.calls += 1
            return {"check_runs": _checks(0, 100)}      # a full page, every time

    client = _Endless()
    with caplog.at_level(logging.WARNING, logger="collect_runs"):
        got = collect_runs._fetch_check_runs(client, "o/r", "sha1")
    assert got is None, "the ceiling must FAIL the fetch, not return a short list"
    assert client.errors == 1, "errors is the ONLY channel to the partial-coverage banner"
    assert client.calls == collect_runs._GH_MAX_PAGES
    assert any("ceiling" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad_page2", [
    ["not", "a", "dict"],                              # a non-object body
    {"total_count": 250, "check_runs": None},          # the list key is null
    {"total_count": 250},                              # the list key is absent
])
def test_a_malformed_page_two_is_a_loud_gap_not_a_short_list(bad_page2, caplog):
    """Probed on the pre-fix code: total_count=250, a bad page 2 → 100 of 250 items
    returned AS COMPLETE with `errors == 0`. Same silent drop as a failed page."""
    client = _PagingClient({
        "repos/o/r/commits/sha1/check-runs?per_page=100": _check_page(_checks(0, 100), 250),
        "repos/o/r/commits/sha1/check-runs?per_page=100&page=2": bad_page2,
    })
    with caplog.at_level(logging.WARNING, logger="collect_runs"):
        got = collect_runs._fetch_check_runs(client, "o/r", "sha1")
    assert got is None
    assert client.errors == 1
    assert any("truncated" in r.getMessage() for r in caplog.records)


def test_a_genuinely_empty_first_page_is_still_an_empty_list():
    """The other direction: `[]` (this commit ran no checks) must NOT become a gap —
    every caller reads `[]` as "nothing ran", and turning that into a disclosed
    failure would cry wolf on every quiet commit."""
    client = _PagingClient({
        "repos/o/r/commits/sha1/check-runs?per_page=100": {"total_count": 0, "check_runs": []},
    })
    assert collect_runs._fetch_check_runs(client, "o/r", "sha1") == []
    assert client.errors == 0


def test_an_empty_first_page_must_be_EXPLICITLY_empty_to_count_as_empty():
    """The page-1 "genuinely empty" exception is only safe if emptiness is EXPLICIT
    (`total_count == 0`, or the list key present-and-empty). "No list key and no
    total_count" was abusable through the acknowledged `_fixture_name` collision: a
    check-runs fixture overwritten by some other endpoint's body (say
    `{"default_branch": "main"}`) is a valid dict with no `check_runs` key and no
    `total_count` — which used to return `[]` with `errors == 0`, i.e. "this commit
    ran no checks", and a clean critical-path sample built on nothing."""
    client = _PagingClient({
        "repos/o/r/commits/sha1/check-runs?per_page=100": {"default_branch": "main"},
    })
    assert collect_runs._fetch_check_runs(client, "o/r", "sha1") is None
    assert client.errors == 1, "a wrong-body page is a coverage gap, not an empty commit"
    # The key present-and-empty IS explicit, and stays an empty list.
    ok = _PagingClient({
        "repos/o/r/commits/sha2/check-runs?per_page=100": {"check_runs": []},
    })
    assert collect_runs._fetch_check_runs(ok, "o/r", "sha2") == []
    assert ok.errors == 0


def test_the_short_page_is_the_stop_not_total_count():
    """`total_count` is a field we don't control. If an endpoint ever UNDER-reports it
    (the `filter=all` jobs view is the one to worry about), stopping on
    `len(items) >= total` would silently drop pages 2+ — reintroducing the exact
    truncation pagination exists to prevent. The SHORT PAGE is authoritative;
    total_count is a warn-only cross-check."""
    client = _PagingClient({
        # total_count LIES (says 100); there are really 137 items across 2 pages.
        "repos/o/r/commits/sha1/check-runs?per_page=100": _check_page(_checks(0, 100), 100),
        "repos/o/r/commits/sha1/check-runs?per_page=100&page=2": _check_page(_checks(100, 137), 100),
    })
    got = collect_runs._fetch_check_runs(client, "o/r", "sha1")
    assert got is not None and len(got) == 137, (
        "an under-reported total_count must not truncate the walk")
    assert len(client.calls) == 2
    assert client.errors == 0


def test_an_over_reported_total_count_warns_but_keeps_what_was_served(caplog):
    """The other direction: GitHub claims more than it serves. Report what actually
    came back (and say so) rather than failing a walk that ended honestly."""
    client = _PagingClient({
        "repos/o/r/commits/sha1/check-runs?per_page=100": _check_page(_checks(0, 7), 99),
    })
    with caplog.at_level(logging.WARNING, logger="collect_runs"):
        got = collect_runs._fetch_check_runs(client, "o/r", "sha1")
    assert got is not None and len(got) == 7
    assert client.errors == 0
    assert any("disagrees" in r.getMessage() for r in caplog.records)


def test_a_run_list_with_no_workflow_runs_key_is_a_FAILURE_not_an_empty_workflow():
    """`_run_list`'s docstring promised None "when the fetch failed or the body was
    malformed"; the code did the opposite for the malformed-KEY case, laundering it
    into `[]` = "this workflow has no runs" with `errors` unbumped and no gap noted —
    the workflow silently deleted from the audit."""
    class _C:
        def __init__(self, doc):
            self.doc, self.errors, self.queries = doc, 0, 0

        def _bump(self, *, query: bool = False, error: bool = False) -> None:
            self.errors += int(error)

        def json(self, endpoint, allow_missing=False):
            return self.doc

    bad = _C({"default_branch": "main"})           # a valid dict, wrong shape
    assert collect_runs._run_list(bad, "repos/o/r/actions/runs") is None
    assert bad.errors == 1
    null_key = _C({"workflow_runs": None})
    assert collect_runs._run_list(null_key, "repos/o/r/actions/runs") is None
    assert null_key.errors == 1
    # A genuinely empty run list is still an empty list, and still not an error.
    empty = _C({"workflow_runs": []})
    assert collect_runs._run_list(empty, "repos/o/r/actions/runs") == []
    assert empty.errors == 0


def test_paginate_stops_on_a_short_page_without_total_count():
    """Page 1 FULL, page 2 short — so "stopped correctly after page 2" is
    distinguishable from "never paginated at all" (a short page 1 proves neither)."""
    client = _PagingClient({
        "repos/o/r/commits/sha1/check-runs?per_page=100": {"check_runs": _checks(0, 100)},
        "repos/o/r/commits/sha1/check-runs?per_page=100&page=2": {"check_runs": _checks(100, 107)},
    })
    got = collect_runs._fetch_check_runs(client, "o/r", "sha1")
    assert got is not None and len(got) == 107
    assert len(client.calls) == 2, "a short page ends the walk — no third request"


def test_list_workflows_paginates_past_100():
    """A monorepo can register >100 workflows; page 1 alone silently loses the rest."""
    wf = lambda lo, hi: [{"id": i, "path": f".github/workflows/w{i}.yml"} for i in range(lo, hi)]  # noqa: E731
    client = _PagingClient({
        "repos/o/r/actions/workflows?per_page=100": {"total_count": 137, "workflows": wf(0, 100)},
        "repos/o/r/actions/workflows?per_page=100&page=2": {"total_count": 137, "workflows": wf(100, 137)},
    })
    got = collect_runs._list_workflows(client, "o/r")
    assert got is not None and len(got) == 137


def test_a_failed_workflow_list_is_not_a_repo_with_zero_workflows():
    """The worst launderer: `[]` here means the audit believes the repo runs NOTHING,
    and every later phase is scoped by this list."""
    client = _PagingClient({})                     # page 1 fails
    assert collect_runs._list_workflows(client, "o/r") is None
    assert client.errors == 1


@pytest.mark.parametrize("fn,extra", [
    (collect_runs._sample_runs, ()),
    (collect_runs._all_status_runs, ()),
    (collect_runs._sample_event_runs, ("push",)),
    (collect_runs._all_status_event_runs, ("push",)),
])
def test_a_failed_run_list_fetch_does_not_read_as_this_workflow_has_no_runs(fn, extra):
    """These five `None -> []` launderers turned a FAILED fetch — including a
    rate-limit block that just exhausted its retries — into "this workflow has no
    runs", and the workflow silently vanished from the audit while the report still
    rendered as though it had been measured."""
    client = _PagingClient({})                     # every fetch fails
    args = ("o/r", 42) + extra + (10,)
    assert fn(client, *args) is None, "a failed fetch must stay distinguishable from empty"
    assert client.errors == 1


def test_a_genuinely_empty_run_list_is_still_an_empty_list():
    client = _PagingClient({
        "repos/o/r/actions/workflows/42/runs?per_page=10&status=success":
            _run_page([]),
    })
    assert collect_runs._sample_runs(client, "o/r", 42, 10) == []
    assert client.errors == 0


def test_the_partial_reason_names_the_workflow_that_vanished():
    """`errors` bumps, so the banner fires — but "4 gh API call(s) failed" reads as a
    rounding error. What the reader needs to know is that the MERGE GATE was dropped
    from the sample entirely."""
    reason = collect_runs._partial_reason(
        4, [{"workflow_file": ".github/workflows/ci.yml", "fetch": "success run sample"}])
    assert "ci.yml" in reason
    assert "MISSING from the sample, not empty" in reason
    assert collect_runs._partial_reason(0, []) is None


# --- the job-log skip, wrong in BOTH directions ------------------------------

def _cancelled(*, started: bool) -> dict:
    j = {"id": 7, "conclusion": "cancelled"}
    if started:
        j["started_at"] = "2026-07-01T10:00:00Z"
    return j


def test_a_cancelled_job_that_STARTED_still_has_a_log_and_must_be_fetched(monkeypatch):
    """A job cancelled MID-RUN (superseded push, `cancel-in-progress`, a manual
    cancel) has a real partial log that GitHub serves 200 — and since
    `_persist_pole_logs` picks the representative job by DURATION and never filters on
    conclusion, a long cancelled job can BE the drilled pole. Skipping it outright
    deleted that pole's drill-down: a coverage REGRESSION shipped inside a coverage
    fix."""
    _patch_run(monkeypatch, lambda *a, **kw: _completed(returncode=0, stdout=_ok("partial log")))
    client = GhClient()
    assert collect_runs._fetch_job_log(client, "o/r", _cancelled(started=True)) == "partial log"
    assert client.queries == 1


def test_a_cancelled_job_that_never_started_has_no_log_and_is_skipped(monkeypatch):
    """Cancelled while still QUEUED — the only cancellation that really 404s."""
    def _boom(*_a, **_kw):
        raise AssertionError("must not spawn gh for a job that never started")
    _patch_run(monkeypatch, _boom)
    client = GhClient()
    assert collect_runs._fetch_job_log(client, "o/r", _cancelled(started=False)) is None
    assert client.queries == 0 and client.errors == 0


def test_a_queued_job_conclusion_null_is_never_fetched(monkeypatch):
    """In-flight runs ARE sampled (`_all_status_runs` applies no `status=completed`
    filter). `conclusion: null` fell through the skip set, so we fetched a guaranteed
    404 and bumped `errors` — bug 2's phantom banner, straight back, on any repo
    audited mid-CI-storm."""
    def _boom(*_a, **_kw):
        raise AssertionError("must not spawn gh for a queued / in-progress job")
    _patch_run(monkeypatch, _boom)
    client = GhClient()
    for job in ({"id": 1, "conclusion": None, "status": "queued"},
                {"id": 2, "status": "in_progress"}):
        assert collect_runs._fetch_job_log(client, "o/r", job) is None
    assert client.queries == 0 and client.errors == 0


def test_a_retention_expired_log_404_is_not_a_collection_error(monkeypatch):
    """GitHub deletes job logs after `retention-days` (default 90) and this skill
    supports pinned windows (`--created-before`). Auditing a window older than
    retention 404s EVERY log — which fired the phantom banner at full strength,
    because the one `.text()` call site never passed `allow_missing`."""
    _patch_run(monkeypatch, lambda *a, **kw: _completed(
        returncode=1, stderr="gh: Not Found (HTTP 404)"))
    client = GhClient()
    job = {"id": 9, "conclusion": "success", "started_at": "2025-01-01T00:00:00Z"}
    assert collect_runs._fetch_job_log(client, "o/r", job) is None
    assert client.queries == 1
    assert client.errors == 0, "an expired log is an EXPECTED absence, not a failed collection"


def test_a_blocked_log_fetch_on_a_job_that_ran_still_counts(monkeypatch, no_sleep):
    """The `allow_missing=True` above must not launder the REAL failures: being
    RATE-LIMITED off a log is a coverage gap, and `_invoke` counts it regardless."""
    _patch_run(monkeypatch, _script([_completed(returncode=1, stderr=_SECONDARY)]))
    client = GhClient()
    job = {"id": 9, "conclusion": "success", "started_at": "2026-07-01T00:00:00Z"}
    assert collect_runs._fetch_job_log(client, "o/r", job) is None
    assert client.errors == 1


# =======================================================================
# The GLOBAL circuit breaker.
#
# The attempt budget is PER CALL. A sustained block (a primary 5000/hr bucket
# exhausted mid-audit, reset 40 minutes out) is unbounded in AGGREGATE without a
# global terminal condition: every remaining call clamps its wait to the cap,
# sleeps, retries into the same block, gives up — and the NEXT call starts fresh at
# attempt 1. Several hundred queued calls is HOURS with no end. Correct-but-hanging
# is its own product failure, so "better to fail loudly than hang" is applied at the
# CLIENT, not just per call.
# =======================================================================

def test_the_breaker_trips_after_consecutive_rate_limit_giveups(monkeypatch, no_sleep):
    _patch_run(monkeypatch, lambda *a, **kw: _completed(
        returncode=1, stderr=_SECONDARY))
    client = GhClient()
    for _ in range(collect_runs._GH_MAX_GIVEUPS):
        assert client.json("repos/o/r/actions/runs") is None
    assert client.gave_up
    assert client.errors == collect_runs._GH_MAX_GIVEUPS, (
        "every exhausted call is a coverage gap and must count toward the banner")


def test_a_tripped_breaker_short_circuits_WITHOUT_sleeping_or_spawning_gh(monkeypatch):
    slept: list[float] = []
    calls: list[list[str]] = []
    monkeypatch.setattr(collect_runs.time, "sleep", lambda s: slept.append(s))
    _patch_run(monkeypatch, lambda cmd, *a, **kw: calls.append(cmd) or _completed(returncode=0, stdout=_ok("{}")))
    client = GhClient()
    client._trip_breaker("test")
    client._record_block(300.0)              # a live block the call must NOT wait out

    assert client.json("repos/o/r") is None
    assert calls == [], "a gave-up client must not spawn gh"
    assert slept == [], "a gave-up client must not sleep — that is the hang we are killing"
    assert client.errors == 1, "the skipped call is still a coverage gap and must count"


def test_a_transient_block_that_clears_does_NOT_trip_the_breaker(monkeypatch, no_sleep):
    """The breaker must fire on a SUSTAINED block, not on two unlucky calls spread
    across an otherwise healthy audit — otherwise it aborts a run that would have
    finished. A success resets the consecutive-give-up count."""
    script = [_completed(returncode=1, stderr=_SECONDARY)] * collect_runs._GH_MAX_ATTEMPTS
    script += [_completed(returncode=0, stdout=_ok('{"ok": true}'))]
    script += [_completed(returncode=1, stderr=_SECONDARY)] * collect_runs._GH_MAX_ATTEMPTS
    _patch_run(monkeypatch, _script(script))
    client = GhClient()
    assert client.json("repos/o/r/a") is None        # give-up #1
    assert client.json("repos/o/r/b") == {"ok": True}  # ...cleared
    assert client.json("repos/o/r/c") is None        # give-up #1 again, not #2
    assert not client.gave_up
    assert client.errors == 2, "both exhausted calls are still counted coverage gaps"


def test_the_cumulative_backoff_budget_also_trips_the_breaker(monkeypatch):
    """Even without consecutive give-ups, an audit that has already spent its whole
    backoff budget waiting is not going to finish inside a useful wall-clock."""
    monkeypatch.setattr(collect_runs.time, "sleep", lambda s: None)
    client = GhClient()
    client._spend_backoff(collect_runs._GH_TOTAL_BACKOFF_BUDGET_S + 1)
    assert client.gave_up


# =======================================================================
# The breaker must bound EVERY sustained failure class, not just rate limits.
#
# Round-3 finding: `_note_rate_limit_giveup()` was called only under
# `if kind == "rate_limit"`, and the transient path's sleep was charged to nothing.
# So a 5xx / timeout outage hit the breaker NEVER. Measured against a stubbed
# subprocess over 200 endpoints, before the fix:
#
#     failure     | subprocess calls | errors | gave_up
#     rate limit  |   6 (2 endpoints)|    2   |  True   <- bounded
#     5xx         | 600 (all 200)    |  200   |  False  <- unbounded
#     timeout     | 600 (all 200)    |  200   |  False  <- unbounded
#
# The timeout row is the real hang: `json()` uses `timeout=60`, so each endpoint
# costs 3 x 60s = 180s of REAL time and a few-hundred-call audit is still the
# multi-hour hang the breaker exists to kill. "Aborts loudly instead of hanging for
# hours" was true only for rate limits. These tests pin all three classes.
# =======================================================================

def _rate_limited(cmd, *a, **kw):
    return _completed(returncode=1, stderr=_SECONDARY,
                      stdout=_gh_i("HTTP/2.0 403 Forbidden",
                                   headers="x-ratelimit-remaining: 0\r\n"))


def _server_error(cmd, *a, **kw):
    return _completed(returncode=1, stderr="gh: Server Error (HTTP 502)",
                      stdout=_gh_i("HTTP/2.0 502 Bad Gateway"))


def _timeout(cmd, *a, **kw):
    raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)


@pytest.mark.parametrize("fake,label", [(_rate_limited, "rate limit"),
                                        (_server_error, "5xx"),
                                        (_timeout, "timeout")])
def test_every_sustained_failure_class_trips_the_breaker_in_bounded_time(
        monkeypatch, no_sleep, fake, label):
    """200 endpoints against an API that is permanently broken in ONE of the three
    retryable ways. Each must abort the client after a BOUNDED number of subprocess
    calls, with the gap COUNTED — `errors` is the only channel to the report's
    partial-coverage banner, so an uncounted give-up is a silent drop."""
    calls: list[list[str]] = []

    def _spy(cmd, *a, **kw):
        calls.append(cmd)
        return fake(cmd, *a, **kw)

    _patch_run(monkeypatch, _spy)
    client = GhClient()
    for i in range(200):
        assert client.json(f"repos/o/r/actions/runs/{i}/jobs") is None

    assert client.gave_up, f"a sustained {label} must trip the global breaker"
    # The bound: at most one full attempt budget per give-up allowed before the trip.
    # Everything after short-circuits with NO subprocess and NO sleep.
    budget = collect_runs._GH_MAX_ATTEMPTS * collect_runs._GH_MAX_GIVEUPS
    assert len(calls) <= budget, (
        f"{len(calls)} subprocesses spawned for a sustained {label} — the breaker did "
        f"not bound it (budget {budget})")
    assert sum(no_sleep) <= (collect_runs._GH_TOTAL_BACKOFF_BUDGET_S
                             + collect_runs._GH_MAX_BACKOFF_S)
    # Every blocked call — including the ones short-circuited by the breaker — is a
    # coverage gap the banner must see.
    assert client.errors == 200


def test_a_transient_5xx_that_clears_does_NOT_trip_the_breaker(monkeypatch, no_sleep):
    """The breaker fires on a SUSTAINED failure. A 5xx burst that clears must not abort
    an audit that would have finished — the same rule the rate-limit path already had."""
    script = [_completed(returncode=1, stderr="gh: Server Error (HTTP 502)")] \
        * collect_runs._GH_MAX_ATTEMPTS
    script += [_completed(returncode=0, stdout=_ok('{"ok": true}'))]
    script += [_completed(returncode=1, stderr="gh: Server Error (HTTP 502)")] \
        * collect_runs._GH_MAX_ATTEMPTS
    _patch_run(monkeypatch, _script(script))
    client = GhClient()
    assert client.json("repos/o/r/a") is None          # give-up #1
    assert client.json("repos/o/r/b") == {"ok": True}  # ...cleared; count resets
    assert client.json("repos/o/r/c") is None          # give-up #1 again, not #2
    assert not client.gave_up
    assert client.errors == 2


def test_transient_backoff_is_charged_to_the_run_wide_budget(monkeypatch, no_sleep):
    """The transient path used to `time.sleep(wait)` and charge it to nothing, so the
    cumulative-backoff budget was blind to a 5xx storm."""
    _patch_run(monkeypatch, _server_error)
    client = GhClient()
    client.json("repos/o/r/a")
    assert client._backoff_spent > 0, (
        "a transient backoff must count against the run-wide budget, or the budget "
        "cannot bound a 5xx storm")
    assert client._backoff_spent == pytest.approx(sum(no_sleep))


# -----------------------------------------------------------------------
# The classifier must READ THE HEADERS `-i` already put in its hand.
# -----------------------------------------------------------------------

def test_a_403_with_an_exhausted_bucket_is_a_rate_limit_even_without_the_keywords(
        monkeypatch, no_sleep):
    """`_classify_gh_failure` never saw the headers — so a 403 whose message gh does
    not render with the rate-limit keywords classified as `forbidden`: no retry, and on
    an `allow_missing` endpoint (which is EVERY job log) not even a COUNT. Measured
    before the fix: 200 rate-limited job-log fetches, 0 errors, silent. The response's
    own `x-ratelimit-remaining: 0` was in hand the whole time."""
    bland = _gh_i("HTTP/2.0 403 Forbidden", headers="x-ratelimit-remaining: 0\r\n")
    assert collect_runs._split_headers_body(bland)[1]["x-ratelimit-remaining"] == "0"
    assert collect_runs._classify_gh_failure(
        "gh: Forbidden (HTTP 403)", 403,
        {"x-ratelimit-remaining": "0"}) == "rate_limit"
    # `retry-after` is the same evidence by another name.
    assert collect_runs._classify_gh_failure(
        "gh: Forbidden (HTTP 403)", 403, {"retry-after": "60"}) == "rate_limit"
    # A genuine permission 403 (a fresh bucket, no retry-after) stays `forbidden`.
    assert collect_runs._classify_gh_failure(
        "gh: Must have admin rights (HTTP 403)", 403,
        {"x-ratelimit-remaining": "4998"}) == "forbidden"
    # A 404 served while the bucket happens to read 0 is still a 404.
    assert collect_runs._classify_gh_failure(
        "gh: Not Found (HTTP 404)", 404, {"x-ratelimit-remaining": "0"}) == "not_found"

    # ...and end to end: the bland 403 is now RETRIED and COUNTED on an
    # `allow_missing` endpoint, instead of vanishing.
    _patch_run(monkeypatch, lambda *a, **kw: _completed(
        returncode=1, stderr="gh: Forbidden (HTTP 403)", stdout=bland))
    client = GhClient()
    assert client.text("repos/o/r/actions/jobs/1/logs", allow_missing=True) is None
    assert client.errors == 1, (
        "a rate-limited job log must count as a coverage gap, not vanish because the "
        "endpoint tolerates 404s")


# =============================================================================
# A COLOURED RESPONSE BODY  (gh's escape-sequence refusal)
# =============================================================================

_COLOURED = "2026-09-15T00:00:00Z \x1b[31mFAIL\x1b[0m tests/test_api.py::test_login\n"
_OK_HEADERS = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n"
_REFUSAL = ("the response contains terminal escape sequences; pass "
            "--allow-escape-sequences to output it anyway\n")


class _FakeGh:
    """The `gh` binary's behaviour around coloured bodies, in both vintages."""

    def __init__(self, *, supports_flag: bool = True) -> None:
        self.supports_flag = supports_flag
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        has_flag = "--allow-escape-sequences" in argv
        if has_flag and not self.supports_flag:
            return _completed(returncode=2,
                              stderr="unknown flag: --allow-escape-sequences\n")
        if has_flag or not self.supports_flag:
            return _completed(returncode=0, stdout=_OK_HEADERS + _COLOURED)
        # Modern gh, asked without the flag, refuses to print the body.
        return _completed(returncode=1, stdout=_OK_HEADERS, stderr=_REFUSAL)


def test_a_coloured_job_log_is_read_not_silently_dropped(monkeypatch):
    """`gh api` exits 1 on a body containing escape sequences unless told
    otherwise — while the HTTP status is 200. CI logs are coloured, so without
    the flag essentially every log fetch fails, and it fails invisibly: logs are
    fetched `allow_missing=True`, so the failure is not even counted. Measured
    2026-09-15 on a live repo: 13 of 13 reds reported as unreadable, all 13 logs
    served 200 at ~450KB.
    """
    fake = _FakeGh()
    monkeypatch.setattr(collect_runs.subprocess, "run", fake)
    monkeypatch.setattr(collect_runs, "_ESCAPE_FLAG_SUPPORTED", True)

    body = GhClient().text("repos/o/r/actions/jobs/1/logs", allow_missing=True)

    assert body is not None and "test_login" in body
    assert any("--allow-escape-sequences" in argv for argv in fake.calls)


def test_the_escape_sequences_are_stripped_from_what_callers_see(monkeypatch):
    """gh withholds that output to protect the terminal; asking for it anyway
    moves the sanitising duty here. A job log is attacker-influenced content and
    is quoted into a rendered report."""
    monkeypatch.setattr(collect_runs.subprocess, "run", _FakeGh())
    monkeypatch.setattr(collect_runs, "_ESCAPE_FLAG_SUPPORTED", True)

    body = GhClient().text("repos/o/r/actions/jobs/1/logs", allow_missing=True)

    assert "\x1b" not in body
    assert "FAIL" in body


def test_a_gh_too_old_for_the_flag_still_returns_its_log(monkeypatch):
    fake = _FakeGh(supports_flag=False)
    monkeypatch.setattr(collect_runs.subprocess, "run", fake)
    monkeypatch.setattr(collect_runs, "_ESCAPE_FLAG_SUPPORTED", True)
    client = GhClient()

    body = client.text("repos/o/r/actions/jobs/1/logs", allow_missing=True)
    assert body is not None and "test_login" in body

    # And the rejection is remembered, so the next log costs one call.
    before = len(fake.calls)
    client.text("repos/o/r/actions/jobs/2/logs", allow_missing=True)
    assert len(fake.calls) - before == 1


def test_a_coloured_log_is_returned_stripped_whatever_the_client_calls_its_memo(monkeypatch):
    """The BEHAVIOURAL contract, stated without reference to any implementation
    symbol: a modern gh that refuses a coloured body unless asked must still yield
    the log, and the log must arrive without its colour codes. A client that never
    asks gets the refusal, returns None, and this fails on the assertion — not on
    a missing attribute."""
    monkeypatch.setattr(collect_runs, "_ESCAPE_FLAG_SUPPORTED", True, raising=False)
    calls: list[list[str]] = []

    def _modern_gh(argv, **kwargs):
        calls.append(list(argv))
        if "--allow-escape-sequences" in argv:
            return _completed(returncode=0, stdout=_OK_HEADERS + _COLOURED)
        return _completed(returncode=1, stdout=_OK_HEADERS, stderr=_REFUSAL)
    monkeypatch.setattr(collect_runs.subprocess, "run", _modern_gh)

    body = GhClient().text("repos/o/r/actions/jobs/1/logs", allow_missing=True)

    assert body == "2026-09-15T00:00:00Z FAIL tests/test_api.py::test_login\n", (
        f"expected the stripped log back, got {body!r} after {calls}")


class _SpyGovernor:
    """Stand-in for the token-wide REST governor: records every admission."""

    def __init__(self) -> None:
        self.routes: list[str] = []

    def acquire(self, route: str = "") -> None:
        self.routes.append(route)


def test_the_fallback_reissue_is_paced_and_counted_like_any_other_live_call(monkeypatch):
    """When an old gh rejects the flag, the plain re-issue is a SECOND real HTTP
    call. It must take its own token from the token-wide governor and be counted
    in `queries` — under the prefetch pool every in-flight worker pays its own
    rejection at once, so an unmetered re-issue is a burst of up to pool-width
    calls that neither the pacing nor the accounting ever saw."""
    fake = _FakeGh(supports_flag=False)
    monkeypatch.setattr(collect_runs.subprocess, "run", fake)
    monkeypatch.setattr(collect_runs, "_ESCAPE_FLAG_SUPPORTED", True)
    client = GhClient()
    spy = _SpyGovernor()
    client._governor = spy

    body = client.text("repos/o/r/actions/jobs/1/logs", allow_missing=True)

    assert body is not None and "test_login" in body
    assert len(fake.calls) == 2, "one rejected call with the flag, one plain re-issue"
    assert len(spy.routes) == 2, (
        f"the re-issue must acquire the governor too; acquired {len(spy.routes)}x")
    assert client.queries == 2, (
        f"the re-issue is a real call and must be counted; queries == {client.queries}")


@pytest.mark.parametrize("stderr", [
    "gh: Bad Gateway (HTTP 502)\n",
    "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com\n",
    # The refusal itself NAMES the flag — that is the modern gh telling us to pass
    # it, not an old gh rejecting it.
    _REFUSAL,
])
def test_a_failure_that_is_not_a_flag_rejection_does_not_poison_the_memo(
        monkeypatch, no_sleep, stderr):
    """`_flag_was_rejected` must key on gh's "unknown flag" wording, not on the
    flag's name appearing in stderr. A 5xx, a network error, or the refusal
    message itself must leave the memo True — otherwise one transient failure
    would silently downgrade every later log fetch to the plain call that gh
    refuses on coloured bodies."""
    assert collect_runs._flag_was_rejected(stderr) is False

    monkeypatch.setattr(collect_runs, "_ESCAPE_FLAG_SUPPORTED", True)
    calls: list[list[str]] = []
    seq = [
        _completed(returncode=1, stdout="HTTP/1.1 502 Bad Gateway\r\n\r\n", stderr=stderr),
        _completed(returncode=0, stdout=_OK_HEADERS + _COLOURED),
    ]

    def _flaky_gh(argv, **kwargs):
        calls.append(list(argv))
        return seq.pop(0) if len(seq) > 1 else seq[0]
    monkeypatch.setattr(collect_runs.subprocess, "run", _flaky_gh)

    body = GhClient().text("repos/o/r/actions/jobs/1/logs", allow_missing=True)

    assert body is not None and "test_login" in body
    assert collect_runs._ESCAPE_FLAG_SUPPORTED is True, "the memo must not flip on a non-flag failure"
    assert all("--allow-escape-sequences" in argv for argv in calls), (
        f"every attempt must keep asking for the coloured body: {calls}")


def test_strip_keeps_the_whitespace_a_log_is_made_of_and_drops_the_rest():
    strip = collect_runs._strip_terminal_escapes
    # Tab, newline and carriage return are ordinary log text (CRLF logs, progress
    # bars redrawn with \r, tab-indented stack traces) and must survive.
    assert strip("a\tb\r\nc\rd\n") == "a\tb\r\nc\rd\n"
    # CSI (colour, cursor, private modes), OSC (hyperlinks / titles, both
    # terminators) and the two-character escapes are removed whole.
    assert strip("\x1b[1;31mFAIL\x1b[0m") == "FAIL"
    assert strip("\x1b[2K\x1b[1A\x1b[?25lstep\x1b[?25h") == "step"
    assert strip("\x1b]8;;https://x.test\x07link\x1b]8;;\x07") == "link"
    assert strip("\x1b]0;title\x1b\\body") == "body"
    assert strip("\x1b(Bplain\x1b=\x1b>") == "plain"
    # Stray C0 controls (a BEL, a NUL) go; the log text between them stays.
    assert strip("ding\x07\x00dong") == "dingdong"
    # Mixed: a real coloured pytest line keeps its \n and its text.
    assert strip(_COLOURED) == "2026-09-15T00:00:00Z FAIL tests/test_api.py::test_login\n"


def test_strip_removes_csi_sequences_with_private_parameter_bytes():
    """ECMA-48 lets a CSI parameter string open with any of `<=>?`, not just `?`
    — xterm's SGR-mouse reports (`ESC[<…M`), `ESC[=…h` screen modes and `ESC[>c`
    device attributes all appear in captured terminal output. Leaving them in
    would hand a rendered report a bare ESC followed by junk."""
    strip = collect_runs._strip_terminal_escapes
    assert strip("\x1b[<35;10;5Mclick") == "click"
    assert strip("\x1b[=3hmode") == "mode"
    assert strip("\x1b[>cattrs") == "attrs"
    assert strip("\x1b[?1049hscreen\x1b[?1049l") == "screen"


def test_four_workers_rejected_at_once_flip_the_memo_once_and_reissue_once_each(monkeypatch):
    """The memo flip under the prefetch pool: every in-flight worker sees its own
    rejection in the same instant. Each must re-issue its own call exactly once —
    never twice (a worker that re-reads the memo and re-tries the flag), never
    zero times (a worker that trusts another worker's re-issue for its own
    endpoint) — and the memo must end False."""
    monkeypatch.setattr(collect_runs, "_ESCAPE_FLAG_SUPPORTED", True)
    n = 4
    barrier = threading.Barrier(n, timeout=5)
    lock = threading.Lock()
    calls: list[list[str]] = []

    def _old_gh(argv, **kwargs):
        with lock:
            calls.append(list(argv))
        if "--allow-escape-sequences" in argv:
            barrier.wait()          # all four rejections land together
            return _completed(returncode=2,
                              stderr="unknown flag: --allow-escape-sequences\n")
        return _completed(returncode=0, stdout=_OK_HEADERS + _COLOURED)
    monkeypatch.setattr(collect_runs.subprocess, "run", _old_gh)

    client = GhClient()
    endpoints = [f"repos/o/r/actions/jobs/{i}/logs" for i in range(n)]
    results: dict[str, str | None] = {}

    def _worker(ep):
        results[ep] = client.text(ep, allow_missing=True)
    threads = [threading.Thread(target=_worker, args=(ep,)) for ep in endpoints]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert collect_runs._ESCAPE_FLAG_SUPPORTED is False
    assert all(results[ep] and "test_login" in results[ep] for ep in endpoints), results
    for ep in endpoints:
        flagged = [c for c in calls if c[-1] == ep and "--allow-escape-sequences" in c]
        plain = [c for c in calls if c[-1] == ep and "--allow-escape-sequences" not in c]
        assert len(flagged) == 1 and len(plain) == 1, (
            f"{ep}: {len(flagged)} flagged, {len(plain)} plain re-issues")
    assert len(calls) == 2 * n

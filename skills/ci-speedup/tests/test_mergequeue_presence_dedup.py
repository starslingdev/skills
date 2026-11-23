"""Issue #58 — merge-queue runs must not inflate the presence denominator.

A `merge_group` (merge-queue) run executes on a GitHub-generated temporary branch
`gh-readonly-queue/<base>/pr-<N>-<sha>`, i.e. a DISTINCT head_sha per PR. Keying the
per-PR presence population by raw head_sha therefore counts each queue run as a
separate "PR" in the denominator. On a repo that runs its heavy suite only in the
merge queue (heavy suite on `merge_group`, light checks on `pull_request`), the heavy
suite then reads as present on a MINORITY of the sampled "PRs", and the
presence-weighted machinery (#26/#27/#57 — `_workflow_gates_minority`) demotes the
REAL merge gate to a runner-minute-only "minority slow mode", crowning a lighter check.

The fix (`_group_dev_shas_by_pr`) collapses the population denominator to PR IDENTITY
BEFORE sampling: a queue run is folded onto its PR's row (via the queue branch's PR
number) so the heavy suite is present on that PR; unlinkable queue runs collapse onto a
single class that cannot dilute PR presence. Every downstream presence / populations /
chain_facts consumer re-derives from the deduped `per_sha_checks`, so no verifier mirror
moves.

These tests pin:
  * the queue-branch PR-number parser and the per-run PR-number derivation;
  * the grouping rule (fold queued PR runs + queue runs onto one row, single-class
    orphans, and — the no-regression guarantee — an IDENTITY map when no `merge_group`
    run is present);
  * the RED-PROOF: a merge-queue-shaped sample demotes the heavy gate BEFORE dedup
    (`_workflow_gates_minority` fires on the inflated 2N denominator) and keeps it AFTER
    (N denominator), with `_rank_spine_present_first` crowning the heavy check post-dedup.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_mergequeue_presence_dedup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from collect_runs import (  # noqa: E402
    _check_presence,
    _group_dev_shas_by_pr,
    _merge_group_pr_number,
    _POLE_RECUR_FLOOR,
    _RARE_PRESENCE_MIN_PR,
    _rank_spine_present_first,
    _run_pr_number,
    _QUEUE_ORPHAN_KEY,
    _union_member_checks,
    _workflow_gates_minority,
)

# --- A merge-queue-shaped synthetic repo -----------------------------------------
# N PRs, each with ONE pull_request run (light checks) and ONE merge_group (queue)
# run (the heavy suite). The heavy suite runs ONLY in the queue; the light checks run
# ONLY on the pull_request event — the exact shape issue #58 describes.
_N = 8  # >= _RARE_PRESENCE_MIN_PR so the minority test is active, not inert
_HEAVY = "integration-suite"   # ~900s, runs on merge_group only
_LIGHT = "lint"                # ~60s, runs on pull_request only
_HEAVY_S = 900.0
_LIGHT_S = 60.0


def _mergequeue_sample():
    """Return (sha_meta, sha_checks) for the synthetic merge-queue repo.

    `sha_meta` is what the collector accumulates per head_sha (event + derived PR
    number + timestamp); `sha_checks` maps each head_sha to its `{check: dur}` map —
    the check-runs the sampler would fetch for that sha."""
    sha_meta: dict[str, dict] = {}
    sha_checks: dict[str, dict[str, float]] = {}
    for i in range(1, _N + 1):
        pr_sha = f"prsha{i:039d}"
        q_sha = f"qsha{i:040d}"
        # pull_request run: PR number comes from the `pull_requests` array.
        sha_meta[pr_sha] = {
            "event": "pull_request",
            "pr_num": _run_pr_number(
                {"event": "pull_request", "pull_requests": [{"number": i}]}),
            "ts": f"2026-07-1{i:02d}T00:00:00Z",
        }
        sha_checks[pr_sha] = {_LIGHT: _LIGHT_S}
        # merge_group run: PR number comes from the queue branch. Newer ts than the PR
        # event (the queue runs after review), so it is the group's representative.
        q_branch = f"gh-readonly-queue/main/pr-{i}-{'a' * 40}"
        sha_meta[q_sha] = {
            "event": "merge_group",
            "pr_num": _run_pr_number(
                {"event": "merge_group", "head_branch": q_branch}),
            "ts": f"2026-07-1{i:02d}T12:00:00Z",
        }
        sha_checks[q_sha] = {_HEAVY: _HEAVY_S}
    return sha_meta, sha_checks


def _per_sha_checks_ungrouped(sha_meta, sha_checks):
    """Pre-#58: one population row per raw head_sha (2N rows)."""
    return [sha_checks[s] for s in sha_meta]


def _per_sha_checks_grouped(sha_meta, sha_checks):
    """Post-#58: one row per PR identity, UNIONing every member sha's checks."""
    _rep_ts, rep_members = _group_dev_shas_by_pr(sha_meta)
    rows = []
    for rep in _rep_ts:
        u: dict[str, float] = {}
        for member in rep_members[rep]:
            for name, dur in sha_checks[member].items():
                u[name] = max(u.get(name, 0.0), dur)
        rows.append(u)
    return rows


# --- Parser + per-run PR-number derivation ---------------------------------------

def test_merge_group_pr_number_parses_queue_branch():
    assert _merge_group_pr_number(
        "gh-readonly-queue/main/pr-123-0123456789abcdef0123456789abcdef01234567") == 123
    # A non-default base branch is still parsed.
    assert _merge_group_pr_number(
        "gh-readonly-queue/release-2.0/pr-7-deadbeefdeadbeefdeadbeefdeadbeefdeadbeef") == 7
    # A base branch containing SLASHES (`release/1.x`, `feature/foo`) — the merge
    # queue keeps the base's slashes in the temp branch, so the parser must match
    # `<base>` greedily to the LAST `/pr-<N>-<hex>` marker, not as one path segment.
    assert _merge_group_pr_number(
        "gh-readonly-queue/release/1.x/pr-123-" + "a" * 40) == 123
    assert _merge_group_pr_number(
        "gh-readonly-queue/feature/foo/pr-7-" + "b" * 40) == 7
    # Pathological: a base branch whose OWN name contains a `pr-<n>-<hex>`-shaped
    # segment. Greedy `.+` still resolves to the real (last) PR marker.
    assert _merge_group_pr_number(
        "gh-readonly-queue/fix/pr-5-oldhex/pr-99-" + "c" * 40) == 99


def test_merge_group_pr_number_rejects_non_queue_branches():
    assert _merge_group_pr_number("feature/my-branch") is None
    assert _merge_group_pr_number("main") is None
    assert _merge_group_pr_number("") is None
    assert _merge_group_pr_number(None) is None
    # Missing the PR segment entirely.
    assert _merge_group_pr_number("gh-readonly-queue/main/abcdef") is None


def test_run_pr_number_sources():
    # merge_group -> from the queue branch.
    assert _run_pr_number(
        {"event": "merge_group",
         "head_branch": "gh-readonly-queue/main/pr-42-" + "f" * 40}) == 42
    # pull_request -> from pull_requests[0].number.
    assert _run_pr_number(
        {"event": "pull_request", "pull_requests": [{"number": 99}]}) == 99
    # fork PR with no pull_requests -> non-derivable.
    assert _run_pr_number({"event": "pull_request", "pull_requests": []}) is None
    assert _run_pr_number({"event": "pull_request"}) is None


# --- Grouping rule ---------------------------------------------------------------

def test_group_folds_queue_run_onto_its_pr():
    sha_meta, _ = _mergequeue_sample()
    rep_ts, rep_members = _group_dev_shas_by_pr(sha_meta)
    # N PRs -> N population rows (not 2N), each with its PR-event sha + queue sha.
    assert len(rep_ts) == _N
    for rep, members in rep_members.items():
        assert len(members) == 2, "each PR folds its pull_request + merge_group sha"
        # The representative is the NEWEST member (the queue run, ts 12:00 > 00:00).
        assert rep.startswith("qsha"), "queue run is newest -> representative"


def test_group_is_identity_without_merge_group_runs():
    """No-regression: a repo with NO merge_group runs groups every sha as its own
    row (byte-for-byte the pre-#58 per-sha behaviour), even when pull_requests[]
    carries a PR number that would otherwise fold multiple commits together."""
    sha_meta = {
        "cA" + "0" * 38: {"event": "pull_request", "pr_num": 5,
                          "ts": "2026-07-10T00:00:00Z"},
        "cB" + "0" * 38: {"event": "pull_request", "pr_num": 5,  # same PR, later commit
                          "ts": "2026-07-11T00:00:00Z"},
        "cC" + "0" * 38: {"event": "pull_request", "pr_num": 6,
                          "ts": "2026-07-12T00:00:00Z"},
    }
    rep_ts, rep_members = _group_dev_shas_by_pr(sha_meta)
    # Every sha is its own group — commits of PR #5 are NOT collapsed (no queue in play).
    assert set(rep_ts) == set(sha_meta)
    assert all(members == [rep] for rep, members in rep_members.items())
    # The ts map matches the input exactly -> identical newest-first walk order.
    assert rep_ts == {s: m["ts"] for s, m in sha_meta.items()}


def test_unlinkable_queue_runs_collapse_to_single_class():
    """A merge_group run whose branch is off the naming scheme (no derivable PR)
    collapses onto ONE orphan row — it cannot dilute PR presence, and is not dropped."""
    sha_meta = {
        "pr1" + "0" * 37: {"event": "pull_request", "pr_num": 1,
                           "ts": "2026-07-10T00:00:00Z"},
        "pr2" + "0" * 37: {"event": "pull_request", "pr_num": 2,
                           "ts": "2026-07-11T00:00:00Z"},
        # Two queue runs with UNPARSEABLE branches -> pr_num None -> single class.
        "orphanA" + "0" * 33: {"event": "merge_group", "pr_num": None,
                               "ts": "2026-07-12T00:00:00Z"},
        "orphanB" + "0" * 33: {"event": "merge_group", "pr_num": None,
                               "ts": "2026-07-13T00:00:00Z"},
    }
    rep_ts, rep_members = _group_dev_shas_by_pr(sha_meta)
    # 2 PR rows + 1 orphan class row = 3 (not 4): the two orphan queue runs add ONE.
    assert len(rep_ts) == 3
    orphan_reps = [rep for rep, members in rep_members.items()
                   if any(s.startswith("orphan") for s in members)]
    assert len(orphan_reps) == 1
    assert sorted(rep_members[orphan_reps[0]]) == sorted(
        ["orphanA" + "0" * 33, "orphanB" + "0" * 33])
    # The orphan sentinel is never emitted as a representative head_sha.
    assert _QUEUE_ORPHAN_KEY not in rep_ts


# --- Mixed derivable + orphan queue runs in one sample ---------------------------

def test_group_mixes_derivable_fold_and_orphan_class():
    """A derivable queue fold and an unlinkable orphan queue run COEXIST correctly
    under `has_merge_group=True`: PR #1's pull_request + queue members fold to one
    `("pr", 1)` row, while an off-scheme queue run forms the single orphan class —
    3 members total across exactly 2 rows."""
    sha_meta = {
        "pr1sha" + "0" * 34: {"event": "pull_request", "pr_num": 1,
                              "ts": "2026-07-10T00:00:00Z"},
        "q1sha" + "0" * 35: {"event": "merge_group", "pr_num": 1,
                             "ts": "2026-07-10T12:00:00Z"},
        "orphan" + "0" * 34: {"event": "merge_group", "pr_num": None,
                              "ts": "2026-07-11T00:00:00Z"},
    }
    rep_ts, rep_members = _group_dev_shas_by_pr(sha_meta)
    assert len(rep_ts) == 2, "one folded PR row + one orphan-class row"
    folded = [m for m in rep_members.values() if len(m) == 2]
    assert len(folded) == 1 and sorted(folded[0]) == sorted(
        ["pr1sha" + "0" * 34, "q1sha" + "0" * 35])
    assert _QUEUE_ORPHAN_KEY not in rep_ts


def test_group_queue_only_pr_forms_single_member_row():
    """A PR whose `pull_request` run fell outside the recency window but whose queue
    run WAS sampled still forms a valid single-member `("pr", N)` row carrying the
    heavy suite — presence-correct, not dropped."""
    sha_meta = {
        "q7sha" + "0" * 35: {"event": "merge_group", "pr_num": 7,
                             "ts": "2026-07-12T00:00:00Z"},
    }
    rep_ts, rep_members = _group_dev_shas_by_pr(sha_meta)
    assert list(rep_ts) == ["q7sha" + "0" * 35]
    assert rep_members["q7sha" + "0" * 35] == ["q7sha" + "0" * 35]


def test_group_representative_tiebreak_on_equal_ts_is_deterministic():
    """When two members carry an IDENTICAL timestamp, the representative is chosen by
    head_sha descending (a stable tiebreak), so the stamped `chain_facts.sha` and the
    interval lookups are reproducible run-to-run."""
    a = "aaa" + "0" * 37
    b = "bbb" + "0" * 37
    sha_meta = {
        a: {"event": "pull_request", "pr_num": 3, "ts": "2026-07-10T00:00:00Z"},
        b: {"event": "merge_group", "pr_num": 3, "ts": "2026-07-10T00:00:00Z"},
    }
    rep_ts, rep_members = _group_dev_shas_by_pr(sha_meta)
    assert list(rep_ts) == [b], "sha 'bbb...' > 'aaa...' wins the equal-ts tiebreak"
    assert sorted(rep_members[b]) == sorted([a, b])


# --- _union_member_checks: partial-fetch coverage-gap semantics -------------------

def test_union_member_checks_unions_across_members():
    """The happy path: two members union to one row, max duration per check."""
    store = {
        "m1": [{"name": "lint", "started_at": "2026-07-10T00:00:00Z",
                "completed_at": "2026-07-10T00:01:00Z"}],           # 60s
        "m2": [{"name": "heavy", "started_at": "2026-07-10T00:00:00Z",
                "completed_at": "2026-07-10T00:15:00Z"}],           # 900s
    }
    res = _union_member_checks(["m1", "m2"], lambda s: store.get(s))
    assert res is not None
    m, iv = res
    assert m == {"lint": 60.0, "heavy": 900.0}
    assert set(iv) == {"lint", "heavy"}


def test_union_member_checks_partial_failure_is_a_coverage_gap():
    """The #58 corollary: if the heavy queue member's fetch FAILS while the light
    member succeeds, the whole row is a coverage gap (None) — NOT a laundered
    light-only row that would silently drop the heavy gate and reintroduce the
    presence dilution this fix removes."""
    def fetch(member):
        if member == "queue":
            return None  # gh error/timeout on the heavy member
        return [{"name": "lint", "started_at": "2026-07-10T00:00:00Z",
                 "completed_at": "2026-07-10T00:01:00Z"}]
    assert _union_member_checks(["head", "queue"], fetch) is None
    # Order-independent: the failure is caught whichever member fails first.
    assert _union_member_checks(["queue", "head"], fetch) is None


def test_union_member_checks_all_fail_is_a_gap_and_empty_is_not():
    # Every member fails -> coverage gap.
    assert _union_member_checks(["a", "b"], lambda s: None) is None
    # A member that genuinely ran no timed check contributes {} and is NOT a failure.
    res = _union_member_checks(["a"], lambda s: [])
    assert res == ({}, {})


def test_union_member_checks_single_member_matches_pre_fix_behaviour():
    """On a no-queue repo every group is single-member: fetch None -> None (gap),
    a populated fetch -> the sha's own map — byte-for-byte the pre-#58 path."""
    assert _union_member_checks(["only"], lambda s: None) is None
    res = _union_member_checks(
        ["only"], lambda s: [{"name": "test", "started_at": "2026-07-10T00:00:00Z",
                              "completed_at": "2026-07-10T00:02:00Z"}])
    assert res is not None and res[0] == {"test": 120.0}


# --- RED-PROOF: the demotion, and its repair -------------------------------------

def test_queue_inflation_demotes_the_heavy_gate_and_dedup_fixes_it():
    """The headline invariant of issue #58.

    BEFORE dedup: the presence denominator is 2N (each queue run its own "PR"), so the
    heavy workflow gates N of 2N -> `_workflow_gates_minority` fires (a MINORITY), and
    the heavy gate is demoted to a runner-minute-only mode with a lighter check crowned.

    AFTER dedup: the denominator is N (one row per PR), the heavy suite is present on
    every PR, the minority test does NOT fire, and `_rank_spine_present_first` crowns
    the heavy check as the merge gate."""
    sha_meta, sha_checks = _mergequeue_sample()

    # --- pre-fix (ungrouped) ---
    pre_rows = _per_sha_checks_ungrouped(sha_meta, sha_checks)
    pre_present, pre_npr = _check_presence(pre_rows, {_HEAVY, _LIGHT})
    assert pre_npr == 2 * _N, "each raw head_sha counts as a PR -> inflated denominator"
    assert pre_present[_HEAVY] == _N
    # The heavy WORKFLOW gates N of 2N sampled 'PRs' -> minority -> demoted.
    assert _workflow_gates_minority(wf_gate_freq=_N, present_n_pr=pre_npr) is True

    # --- post-fix (grouped to PR identity) ---
    post_rows = _per_sha_checks_grouped(sha_meta, sha_checks)
    post_present, post_npr = _check_presence(post_rows, {_HEAVY, _LIGHT})
    assert post_npr == _N, "one population row per PR -> deduped denominator"
    assert post_present[_HEAVY] == _N, "heavy suite present on EVERY PR"
    assert post_present[_LIGHT] == _N, "light checks still present on every PR (unioned)"
    # The heavy workflow now gates N of N -> NOT a minority -> kept as the real gate.
    assert _workflow_gates_minority(wf_gate_freq=_N, present_n_pr=post_npr) is False

    # And the spine crowns the heavy check as the headline merge gate.
    pr_check_p50 = {_HEAVY: _HEAVY_S, _LIGHT: _LIGHT_S}
    order, present, n_pr, pole_freq = _rank_spine_present_first(
        pr_check_p50, post_rows, req_names=frozenset())
    assert order[0][0] == _HEAVY, "the heavy suite is crowned the merge gate post-dedup"
    assert n_pr == _N
    # The heavy check is the actual pole on every PR (a recurring gate, not rare).
    assert pole_freq[_HEAVY] >= _POLE_RECUR_FLOOR
    assert n_pr >= _RARE_PRESENCE_MIN_PR

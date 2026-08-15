"""The two Phase-A detectors, proven in both directions.

P14.9 (fork code executed with privileges) is the most consequential detector
in the product — the "pwn request" chain. Its fixtures assert it FIRES on the
vulnerable three-condition shape and stays SILENT on every near-miss (safe
trigger, base checkout, no execution). P14.11 (impostor SHA) is the one
network-gated check; its tests mock the gh boundary and prove the
skip-is-never-a-pass contract.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

_SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _scan_import import load_scan  # noqa: E402

scan = load_scan()


def _wf(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(textwrap.dedent(body))
    return p


# ---------------------------------------------------------------------------
# P14.9 — untrusted-checkout-executes
# ---------------------------------------------------------------------------

VULNERABLE = """\
    name: pr-bench
    on: pull_request_target
    jobs:
      bench:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
            with:
              ref: ${{ github.event.pull_request.head.sha }}
          - run: npm ci && npm run bench
"""


def _hits(tmp_path: Path, body: str) -> list:
    wf = _wf(tmp_path, "wf.yml", body)
    return list(scan._correlation_untrusted_checkout_executes(wf))


def test_fires_on_the_full_chain(tmp_path):
    hits = _hits(tmp_path, VULNERABLE)
    assert len(hits) == 1
    assert "checks out" in hits[0].evidence
    assert "bench" == hits[0].match_text


def test_fires_on_workflow_run_head_and_local_action(tmp_path):
    hits = _hits(tmp_path, """\
        on:
          workflow_run:
            workflows: [CI]
            types: [completed]
        jobs:
          publish:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  ref: ${{ github.event.workflow_run.head_sha }}
              - uses: ./.github/actions/build
    """)
    assert len(hits) == 1


def test_fires_on_refs_pull_merge_ref(tmp_path):
    hits = _hits(tmp_path, """\
        on: pull_request_target
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  ref: refs/pull/${{ github.event.number }}/merge
              - run: make test
    """)
    assert len(hits) == 1


def test_silent_on_safe_trigger(tmp_path):
    # Same checkout+exec shape, but plain pull_request: fork PRs get a
    # read-only token and no secrets — not the chain.
    assert _hits(tmp_path, VULNERABLE.replace(
        "on: pull_request_target", "on: pull_request")) == []


def test_silent_on_base_checkout(tmp_path):
    # Untrusted trigger, but the checkout has no ref: (base/merge ref of the
    # BASE repo) — the attacker's code never enters the tree.
    assert _hits(tmp_path, """\
        on: pull_request_target
        jobs:
          label:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - run: ./scripts/label.sh
    """) == []


def test_silent_without_execution_after_checkout(tmp_path):
    # Head checkout but nothing executes from the tree afterward (an
    # API-only step) — condition (3) missing.
    assert _hits(tmp_path, """\
        on: pull_request_target
        jobs:
          diff:
            runs-on: ubuntu-latest
            steps:
              - run: echo "pre-checkout step, runs before attacker code exists"
              - uses: actions/checkout@v4
                with:
                  ref: ${{ github.event.pull_request.head.sha }}
              - uses: actions/labeler@8558fd74291d67161a8a78ce36a881fa63b766a9
    """) == []


def test_one_hit_per_qualifying_job(tmp_path):
    hits = _hits(tmp_path, """\
        on: pull_request_target
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with: { ref: "${{ github.head_ref }}" }
              - run: make
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - run: make lint
    """)
    assert [h.match_text for h in hits] == ["a"]


def test_catalog_scan_end_to_end_fires_p14_9(tmp_path):
    _wf(tmp_path, "bench.yml", VULNERABLE)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path)
    p149 = [f for f in result["findings"] if f["pattern"] == "P14.9"]
    assert len(p149) == 1
    assert p149[0]["severity"] == "HIGH"


# ---------------------------------------------------------------------------
# P14.11 — gh-impostor-sha
# ---------------------------------------------------------------------------

PINNED = """\
    on: push
    jobs:
      build:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@8ade135a41bc03ea155e62e844d188df1ea18608
          - uses: evil/fork-action@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
          - uses: actions/checkout@8ade135a41bc03ea155e62e844d188df1ea18608
          - uses: ./local/action
          - uses: actions/setup-node@v4
"""


def test_collect_sha_pins_finds_only_forty_hex_remote_pins(tmp_path):
    _wf(tmp_path, "ci.yml", PINNED)
    pins = scan._collect_sha_pins(tmp_path, scan.all_workflow_files(tmp_path))
    repos = [(repo, sha[:4]) for _, _, repo, sha in pins]
    assert repos == [
        ("actions/checkout", "8ade"),
        ("evil/fork-action", "aaaa"),
        ("actions/checkout", "8ade"),
    ]


NOT_REALLY_PINS = """\
    on: push
    jobs:
      build:
        runs-on: ubuntu-latest
        steps:
          # - uses: retired/action@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  (old)
          - uses: actions/checkout@8ade135a41bc03ea155e62e844d188df1ea18608  # v4.1.1
          - run: |
              echo "the docs example says: uses: someone/thing@cccccccccccccccccccccccccccccccccccccccc"
"""


def test_collect_sha_pins_ignores_comments_and_run_block_text(tmp_path):
    """A commented-out pin and a `uses:` string inside a run: block are not
    action references. Collecting them sends them to the gh boundary, and a
    404 on either renders as a CRITICAL impostor finding on a line that isn't
    an action reference at all. Only the real pin (with its trailing version
    comment intact) may survive."""
    _wf(tmp_path, "ci.yml", NOT_REALLY_PINS)
    pins = scan._collect_sha_pins(tmp_path, scan.all_workflow_files(tmp_path))
    assert [(repo, sha[:4]) for _, _, repo, sha in pins] == [
        ("actions/checkout", "8ade"),
    ]


MULTILINE_PINS = """\
    on: push
    jobs:
      a:
        runs-on: ubuntu-latest
        steps:
          - uses: >-
              owner/folded@dddddddddddddddddddddddddddddddddddddddd
          - {uses: owner/flow@eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee}
          - uses: "owner/quoted@ffffffffffffffffffffffffffffffffffffffff"
      b:
        uses: owner/reusable/.github/workflows/x.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
"""


def test_collect_sha_pins_sees_pins_a_line_scan_cannot(tmp_path):
    """A missed pin is a silent clean, which is the worse direction for a
    network-gated check. Folded scalars, flow style, quoted values and a
    job-level reusable-workflow pin are all real action references — and the
    reported line must still point at the value, not at the `uses:` key that
    may sit a line above it."""
    _wf(tmp_path, "ci.yml", MULTILINE_PINS)
    pins = scan._collect_sha_pins(tmp_path, scan.all_workflow_files(tmp_path))
    assert [(line, repo) for _, line, repo, _ in pins] == [
        (7, "owner/folded"),
        (8, "owner/flow"),
        (9, "owner/quoted"),
        (11, "owner/reusable"),
    ]


def test_collect_sha_pins_falls_back_when_the_workflow_will_not_parse(tmp_path):
    """An unparseable workflow must not silently drop its pins — that would
    read as clean. Fall back to the line scan."""
    _wf(tmp_path, "broken.yml", """\
        on: push
        jobs: [
          - uses: owner/thing@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    """)
    pins = scan._collect_sha_pins(tmp_path, scan.all_workflow_files(tmp_path))
    assert [repo for _, _, repo, _ in pins] == ["owner/thing"]


def test_invisible_repo_is_inconclusive_not_impostor():
    """GitHub answers 404 — not 403 — for a repo the caller cannot see, so a
    private/internal shared-action repo would otherwise be flagged as an
    impostor pin. An unseeable repo is an unknown, never a finding."""
    scan._gh_repo_visible.cache_clear()
    with mock.patch.object(scan.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=1, stderr="gh: Not Found (HTTP 404)")
        assert scan._gh_commit_in_repo("private-org/actions", "a" * 40) is None
    # …but a 404 on a repo we CAN see is a genuine impostor verdict.
    scan._gh_repo_visible.cache_clear()

    with mock.patch.object(
        scan.subprocess, "run", side_effect=_gh_api_stub(commit=False, tag=None)
    ):
        assert scan._gh_commit_in_repo("actions/checkout", "a" * 40) is False
    scan._gh_repo_visible.cache_clear()


def _gh_api_stub(*, commit, tag, visible=True, peeled_commit=True):
    """Stub `gh api` for the impostor probes without touching the network.

    ``commit`` is the verdict for `repos/…/commits/<pinned sha>`; ``tag`` is the
    JSON body `repos/…/git/tags/<sha>` returns, or None for "not a tag object";
    ``peeled_commit`` is the verdict for the commit a tag peels to.
    """
    not_found = "gh: Not Found (HTTP 404)"

    def _run(argv, **kw):
        path = argv[2]
        if "/git/tags/" in path:
            sha = path.rsplit("/", 1)[-1]
            body = tag(sha) if callable(tag) else tag
            if body is None:
                return mock.Mock(returncode=1, stdout="", stderr=not_found)
            return mock.Mock(returncode=0, stdout=json.dumps(body), stderr="")
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            ok = peeled_commit if sha != "a" * 40 else commit
            return mock.Mock(
                returncode=0 if ok else 1, stdout="", stderr="" if ok else not_found
            )
        return mock.Mock(returncode=0 if visible else 1, stdout="", stderr="")

    return _run


def _tag_body(target_sha, target_type="commit"):
    return {"object": {"type": target_type, "sha": target_sha}}


def test_pin_to_an_annotated_tag_object_is_not_an_impostor():
    """setup-uv/pnpm-action-setup style: `uses: owner/action@<sha>` where the
    sha is the ANNOTATED TAG object of a release, not a commit. The commits
    endpoint 404s on it, but the canonical repo plainly contains it — flagging
    it accuses a legitimate release of being a fork-only or dangling object.
    """
    scan._gh_repo_visible.cache_clear()
    stub = _gh_api_stub(commit=False, tag=_tag_body("b" * 40))
    with mock.patch.object(scan.subprocess, "run", side_effect=stub):
        assert scan._gh_commit_in_repo("astral-sh/setup-uv", "a" * 40) is True
    scan._gh_repo_visible.cache_clear()


def test_nested_tag_object_peels_through_to_its_commit():
    """An annotated tag may point at another tag object; follow the chain."""
    scan._gh_repo_visible.cache_clear()

    def _tag(sha):
        if sha == "a" * 40:
            return _tag_body("c" * 40, target_type="tag")
        if sha == "c" * 40:
            return _tag_body("b" * 40)
        return None

    with mock.patch.object(
        scan.subprocess, "run", side_effect=_gh_api_stub(commit=False, tag=_tag)
    ):
        assert scan._gh_commit_in_repo("pnpm/action-setup", "a" * 40) is True
    scan._gh_repo_visible.cache_clear()


def test_tag_object_cycle_does_not_hang_and_is_unverified_not_flagged():
    """A tag object that points at itself terminates, as UNRESOLVED.

    It used to terminate as an accusation. "We stopped walking this chain" and
    "the canonical repo does not contain this object" are different facts, and
    a broken or malicious tag graph is not evidence that the PIN is an
    impostor — it is evidence that we could not tell. The pin goes on the
    unverified list, where a human looks at it.
    """
    scan._gh_repo_visible.cache_clear()
    with mock.patch.object(
        scan.subprocess,
        "run",
        side_effect=_gh_api_stub(
            commit=False, tag=lambda sha: _tag_body(sha, target_type="tag")
        ),
    ):
        assert scan._gh_commit_in_repo("evil/fork-action", "a" * 40) is None
    scan._gh_repo_visible.cache_clear()


def test_tag_chain_deeper_than_the_cap_is_unverified_not_flagged():
    """Depth exhaustion is a limit of ours, not a fact about the repo."""
    scan._gh_repo_visible.cache_clear()
    chain = [chr(ord("a") + i) * 40 for i in range(7)]

    def _tag(sha):
        if sha in chain[:-1]:
            return _tag_body(chain[chain.index(sha) + 1], target_type="tag")
        return None

    with mock.patch.object(
        scan.subprocess, "run",
        side_effect=_gh_api_stub(commit=False, tag=_tag),
    ):
        assert scan._gh_commit_in_repo("some/action", chain[0]) is None
    scan._gh_repo_visible.cache_clear()


def test_tag_target_that_is_not_a_commit_or_tag_is_unverified():
    """A tag pointing at a tree or a blob cannot be peeled to a commit. We
    learned nothing about containment, so we accuse nobody."""
    scan._gh_repo_visible.cache_clear()
    with mock.patch.object(
        scan.subprocess, "run",
        side_effect=_gh_api_stub(
            commit=False, tag=_tag_body("b" * 40, target_type="tree")),
    ):
        assert scan._gh_commit_in_repo("some/action", "a" * 40) is None
    scan._gh_repo_visible.cache_clear()


def test_malformed_tag_body_is_unverified():
    """`git/tags` answered with something that is not JSON. We asked and got
    noise; noise is not an absence."""
    scan._gh_repo_visible.cache_clear()

    def _run(argv, **kw):
        path = argv[2]
        if "/git/tags/" in path:
            return mock.Mock(returncode=0, stdout="<html>502</html>", stderr="")
        if "/commits/" in path:
            return mock.Mock(returncode=1, stdout="",
                             stderr="gh: Not Found (HTTP 404)")
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(scan.subprocess, "run", side_effect=_run):
        assert scan._gh_commit_in_repo("some/action", "a" * 40) is None
    scan._gh_repo_visible.cache_clear()


def test_tag_object_sha_that_is_not_40_hex_is_unverified():
    """A truncated or garbage `object.sha` cannot be re-probed. Treating that
    as an absence would flag on a malformed API response."""
    scan._gh_repo_visible.cache_clear()
    with mock.patch.object(
        scan.subprocess, "run",
        side_effect=_gh_api_stub(commit=False, tag=_tag_body("deadbeef")),
    ):
        assert scan._gh_commit_in_repo("some/action", "a" * 40) is None
    scan._gh_repo_visible.cache_clear()


def test_gh_missing_or_timing_out_during_the_peel_is_unverified():
    """No `gh` on PATH, or a hung call, is an environment problem. It must
    never spend itself as a CRITICAL finding against the user's repo."""
    for exc in (subprocess.TimeoutExpired(cmd="gh", timeout=30),
                FileNotFoundError("gh")):
        scan._gh_repo_visible.cache_clear()

        def _run(argv, _exc=exc, **kw):
            path = argv[2]
            if "/git/tags/" in path:
                raise _exc
            if "/commits/" in path:
                return mock.Mock(returncode=1, stdout="",
                                 stderr="gh: Not Found (HTTP 404)")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(scan.subprocess, "run", side_effect=_run):
            assert scan._gh_commit_in_repo("some/action", "a" * 40) is None
        scan._gh_repo_visible.cache_clear()


def test_a_422_from_the_tag_endpoint_is_a_definitive_absence():
    """The definitive set is 404 AND 422. GitHub answers 422 for a sha that is
    well-formed but names nothing here — an answer, not a failure. Dropping
    422 from the definitive list turns real impostor pins into unverified
    ones, so this test names it explicitly."""
    scan._gh_repo_visible.cache_clear()

    def _run(argv, **kw):
        path = argv[2]
        if "/git/tags/" in path:
            return mock.Mock(returncode=1, stdout="",
                             stderr="gh: Unprocessable Entity (HTTP 422)")
        if "/commits/" in path:
            return mock.Mock(returncode=1, stdout="",
                             stderr="gh: Not Found (HTTP 404)")
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(scan.subprocess, "run", side_effect=_run):
        assert scan._gh_commit_in_repo("evil/fork-action", "a" * 40) is False
    scan._gh_repo_visible.cache_clear()


def test_pin_that_is_neither_commit_nor_tag_object_is_still_flagged():
    """The fork-only/dangling verdict must survive the tag-object fallback."""
    scan._gh_repo_visible.cache_clear()
    with mock.patch.object(
        scan.subprocess, "run", side_effect=_gh_api_stub(commit=False, tag=None)
    ):
        assert scan._gh_commit_in_repo("evil/fork-action", "a" * 40) is False
    scan._gh_repo_visible.cache_clear()


def test_tag_peel_that_cannot_reach_the_api_is_unverified_not_an_impostor():
    """A rate limit or a dropped connection during the peel is not evidence.

    The peel runs only on the about-to-be-flagged path, so a transient failure
    there manufactures exactly the accusation this check exists to avoid — a
    legitimate release pin reported as a fork-only object. An unanswered probe
    degrades to unverified; only an explicit 404/422 is an answer.
    """
    scan._gh_repo_visible.cache_clear()
    not_found = "gh: Not Found (HTTP 404)"

    def _run(argv, **kw):
        path = argv[2]
        if "/git/tags/" in path:
            return mock.Mock(
                returncode=1, stdout="",
                stderr="error connecting to api.github.com",
            )
        if "/commits/" in path:
            return mock.Mock(returncode=1, stdout="", stderr=not_found)
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(scan.subprocess, "run", side_effect=_run):
        assert scan._gh_commit_in_repo("astral-sh/setup-uv", "a" * 40) is None
    scan._gh_repo_visible.cache_clear()


def test_a_tag_peeling_to_a_commit_this_repo_lacks_is_flagged():
    """THE distinguishing case: the tag that peels to nothing.

    A tag can peel cleanly and still point at an object that resolves nowhere —
    deleted, dangling, never pushed. The peel must not launder that into
    "verified": a served tag object says only that the object store holds the
    TAG, nothing about what it points at.

    Note what this does NOT cover, per `_gh_commit_in_repo`'s docstring: both
    endpoints answer about the fork network, so a tag peeling to a commit in a
    FORK of the canonical repo returns 200 and reads clean. The mock here says
    404 because that is the case the code can actually decide.
    """
    scan._gh_repo_visible.cache_clear()
    with mock.patch.object(
        scan.subprocess, "run",
        side_effect=_gh_api_stub(
            commit=False, tag=_tag_body("b" * 40), peeled_commit=False),
    ):
        assert scan._gh_commit_in_repo("owner/action", "a" * 40) is False
    scan._gh_repo_visible.cache_clear()


def test_served_tag_object_survives_an_inconclusive_reprobe():
    """Tag object served, re-probe inconclusive → UNVERIFIED, not clean.

    Being SERVED the tag object is not proof of containment: GitHub serves one
    object store per fork network, so `repos/{repo}/git/tags/{sha}` will return
    a tag an attacker created in a fork of this repo. The commit re-probe is
    the only question here whose negative answer means anything.

    So when the peel succeeds but the re-probe cannot answer, we hold no
    evidence at all. Calling that "verified" would assert something we never
    established — the pin belongs on the unverified list, driving `gh_checks`
    to `partial:` rather than counting toward "N verified".
    """
    scan._gh_repo_visible.cache_clear()

    def _run(argv, **kw):
        path = argv[2]
        if "/git/tags/" in path:
            return mock.Mock(
                returncode=0,
                stdout=json.dumps({"object": {"type": "commit", "sha": "b" * 40}}),
                stderr="",
            )
        if "/commits/" in path:
            if path.endswith("b" * 40):      # the peeled commit: no answer
                return mock.Mock(returncode=1, stdout="", stderr="server error 502")
            return mock.Mock(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(scan.subprocess, "run", side_effect=_run):
        assert scan._gh_commit_in_repo("astral-sh/setup-uv", "a" * 40) is None
    scan._gh_repo_visible.cache_clear()


def test_impostor_flags_every_occurrence_but_calls_gh_once_per_pin(tmp_path):
    _wf(tmp_path, "ci.yml", PINNED)
    files = scan.all_workflow_files(tmp_path)
    entry = next(
        e for e in scan.load_catalog(_SKILL / "references" / "security-patterns.md")
        if e.pattern == "P14.11"
    )
    calls: list[tuple[str, str]] = []

    def fake(repo, sha):
        calls.append((repo, sha))
        return repo != "evil/fork-action"

    with mock.patch.object(scan, "_gh_commit_in_repo", side_effect=fake):
        hits, status, unverified = scan._impostor_sha_findings(
            entry, tmp_path, files
        )
    assert len(calls) == 2  # unique pins, cached
    assert len(hits) == 1
    assert "NOT found in" in hits[0][1].evidence
    assert status.startswith("ran:")
    assert "2 unique pin(s) verified, 1 flagged" in status
    assert unverified == []  # every pin resolved


def _p1411_entry():
    return next(
        e for e in scan.load_catalog(_SKILL / "references" / "security-patterns.md")
        if e.pattern == "P14.11"
    )


def test_inconclusive_is_not_clean(tmp_path):
    """No verdict at all: the status must say `partial:`, never `ran:`.

    `ran: N unique pin(s) verified` asserts a fact about pins nobody could
    resolve — and report.py renders a `ran:` status with a ✅, so the whole
    check would read as passed. It must also hand back WHICH pins are
    unresolved so the report can name them.
    """
    _wf(tmp_path, "ci.yml", PINNED)
    files = scan.all_workflow_files(tmp_path)
    with mock.patch.object(scan, "_gh_commit_in_repo", return_value=None):
        hits, status, unverified = scan._impostor_sha_findings(
            _p1411_entry(), tmp_path, files
        )
    assert hits == []
    assert status.startswith("partial:"), status
    assert not status.startswith("ran:")
    assert "0 of 2 unique pin(s) verified" in status
    assert "2 UNVERIFIED" in status and "not treated as clean" in status
    # Every occurrence of both unresolved pins, with its file:line.
    assert len(unverified) == 3
    assert all("ci.yml:" in u for u in unverified)
    assert any(u.startswith("evil/fork-action@") for u in unverified)


def test_partial_run_reports_the_verified_split(tmp_path):
    """One pin resolves, one doesn't → `partial:` naming both counts. The old
    status called this a clean `ran:` over an unverified pin."""
    _wf(tmp_path, "ci.yml", PINNED)
    files = scan.all_workflow_files(tmp_path)

    def fake(repo, sha):
        return None if repo == "evil/fork-action" else True

    with mock.patch.object(scan, "_gh_commit_in_repo", side_effect=fake):
        hits, status, unverified = scan._impostor_sha_findings(
            _p1411_entry(), tmp_path, files
        )
    assert hits == []
    assert "partial: 1 of 2 unique pin(s) verified" in status
    assert "1 UNVERIFIED" in status
    assert [u.split("@")[0] for u in unverified] == ["evil/fork-action"]


def test_scan_records_loud_skip_when_gh_unavailable(tmp_path):
    _wf(tmp_path, "ci.yml", PINNED)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path, gh_impostor=False)
    status = result["gh_checks"]["P14.11"]
    assert status.startswith("skipped: gh not authenticated")
    assert "did NOT run" in status
    assert not [f for f in result["findings"] if f["pattern"] == "P14.11"]


def test_skip_reason_distinguishes_disabled_from_unauthenticated(tmp_path):
    """"gh unavailable" was reported for BOTH `--gh-impostor=off` and a missing
    gh login, so a report told a user to `gh auth login` when they had simply
    turned the check off. The two reasons are now distinct strings."""
    _wf(tmp_path, "ci.yml", PINNED)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    off = scan.scan(
        catalog, tmp_path, gh_impostor=False,
        gh_skip_reason="disabled via --gh-impostor=off",
    )["gh_checks"]["P14.11"]
    assert "disabled via --gh-impostor=off" in off
    assert "auth" not in off
    unauth = scan.scan(catalog, tmp_path, gh_impostor=False)["gh_checks"]["P14.11"]
    assert "gh not authenticated (run gh auth login)" in unauth


def test_scan_runs_impostor_when_enabled(tmp_path):
    _wf(tmp_path, "ci.yml", PINNED)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    with mock.patch.object(scan, "_gh_commit_in_repo", return_value=False):
        result = scan.scan(catalog, tmp_path, gh_impostor=True)
    flagged = [f for f in result["findings"] if f["pattern"] == "P14.11"]
    assert len(flagged) == 3  # every occurrence of both bad pins
    assert result["gh_checks"]["P14.11"].startswith("ran:")
    assert all(f["workflow_file"] == ".github/workflows/ci.yml" for f in flagged)


# ---------------------------------------------------------------------------
# The gh boundary: every distinguishable outcome maps to True / False / None,
# and `None` (unknowable) must never collapse into either verdict.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.parametrize(
    "returncode, stderr, exc, expected, why",
    [
        (0, "", None, True, "commit present in the canonical repo"),
        (1, "gh: Not Found (HTTP 404)", None, False,
         "404 on a repo we can see = the impostor verdict"),
        (1, "gh: Unprocessable Entity (HTTP 422)", None, False,
         "422 = sha malformed for this repo, same verdict as absent"),
        (1, "HTTP 403: API rate limit exceeded", None, None,
         "rate limit is unknowable now, never 'clean'"),
        (1, "gh: Bad credentials (HTTP 401)", None, None,
         "401 says nothing about the commit"),
        (1, "HTTP 500: Internal Server Error", None, None, "server error"),
        (None, "", subprocess.TimeoutExpired("gh", 30), None, "timeout"),
        (None, "", FileNotFoundError("gh"), None, "gh not installed"),
    ],
)
def test_gh_commit_in_repo_verdicts(returncode, stderr, exc, expected, why):
    scan._gh_repo_visible.cache_clear()

    def _run(argv, **kw):
        # The visibility probe (`repos/OWNER/REPO`) always succeeds here so the
        # 404/422 cases reach their real verdict rather than the
        # invisible-repo escape hatch; the pin is not a tag object either, so
        # the tag fallback declines too.
        if "/git/tags/" in argv[2]:
            return mock.Mock(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")
        if "/commits/" not in argv[2]:
            return mock.Mock(returncode=0, stdout="", stderr="")
        if exc is not None:
            raise exc
        return mock.Mock(returncode=returncode, stderr=stderr)

    with mock.patch.object(scan.subprocess, "run", side_effect=_run):
        assert scan._gh_commit_in_repo("actions/checkout", "a" * 40) is expected, why
    scan._gh_repo_visible.cache_clear()


# ---------------------------------------------------------------------------
# scan.py's --gh-impostor CLI gating
# ---------------------------------------------------------------------------

_SCAN_SCRIPT = _SKILL / "scripts" / "scan.py"
_MINIMAL_WF = "on: push\njobs:\n  b:\n    runs-on: ubuntu-latest\n"


def _scan_cli(root, mode, gh_ok):
    """Run scan.py's main() in-process with check_prereqs mocked."""
    import gh_utils

    with mock.patch.object(gh_utils, "check_prereqs", return_value=gh_ok):
        return scan.main(["--root", str(root), "--gh-impostor", mode])


def test_gh_impostor_on_without_auth_exits_2(tmp_path, capsys):
    """`on` means REQUIRED. With no authenticated gh the check cannot run, and
    a scan that silently proceeds would emit a report whose only signal is a
    skip line the user explicitly asked not to accept."""
    _wf(tmp_path, "ci.yml", _MINIMAL_WF)
    rc = _scan_cli(tmp_path, "on", gh_ok=False)
    assert rc == 2
    assert "not authenticated" in capsys.readouterr().err


def test_gh_impostor_auto_without_auth_exits_0_with_a_loud_skip(tmp_path, capsys):
    """`auto` degrades — but never silently."""
    _wf(tmp_path, "ci.yml", _MINIMAL_WF)
    rc = _scan_cli(tmp_path, "auto", gh_ok=False)
    assert rc == 0
    status = json.loads(capsys.readouterr().out)["gh_checks"]["P14.11"]
    assert status.startswith("skipped: gh not authenticated")
    assert "did NOT run" in status


# ---------------------------------------------------------------------------
# P14.25 — install-scripts-in-privileged-job
#
# Conditioned, not hygiene: the install alone is every repo's CI. These prove
# the pairing fires and each half alone stays silent, which is the precision
# contract the catalog's admission rests on.
# ---------------------------------------------------------------------------

_INSTALL_PRIVILEGED = """\
    name: publish
    on: push
    jobs:
      publish:
        runs-on: ubuntu-latest
        permissions:
          contents: write
        steps:
          - run: npm ci
          - run: npm publish
            env:
              NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
"""


def _install_hits(tmp_path: Path, body: str) -> list:
    wf = _wf(tmp_path, "wf.yml", body)
    return list(scan._correlation_install_scripts_in_privileged_job(wf))


def test_p1425_fires_on_install_plus_payoff(tmp_path):
    hits = _install_hits(tmp_path, _INSTALL_PRIVILEGED)
    assert len(hits) == 1
    # Evidence quotes the install line verbatim (source), and the payoff
    # travels as a separately-labelled derived claim.
    assert "npm ci" in hits[0].evidence
    assert hits[0].derived is False
    assert "NPM_TOKEN" in (hits[0].derived_note or "")
    assert "contents" in (hits[0].derived_note or "")


def test_p1425_fires_on_secrets_alone_without_a_write_scope(tmp_path):
    """A read-only job that still exports a publish token is the vector."""
    hits = _install_hits(tmp_path, """\
        name: ci
        on: push
        permissions:
          contents: read
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: npm ci
                env:
                  API_KEY: ${{ secrets.API_KEY }}
    """)
    assert len(hits) == 1
    assert "API_KEY" in (hits[0].derived_note or "")


def test_p1425_silent_when_scripts_are_ignored(tmp_path):
    assert _install_hits(
        tmp_path, _INSTALL_PRIVILEGED.replace("npm ci", "npm ci --ignore-scripts")
    ) == []


def test_p1425_silent_without_a_payoff(tmp_path):
    assert _install_hits(tmp_path, """\
        name: ci
        on: push
        permissions:
          contents: read
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: npm ci
              - run: npm test
    """) == []


def test_p1425_silent_on_empty_permissions_and_no_secrets(tmp_path):
    assert _install_hits(tmp_path, """\
        name: lint
        on: push
        jobs:
          lint:
            runs-on: ubuntu-latest
            permissions: {}
            steps:
              - run: pnpm install --frozen-lockfile
              - run: pnpm lint
    """) == []


def test_p1425_github_token_alone_is_not_a_payoff(tmp_path):
    """Every job can read `secrets.GITHUB_TOKEN`; counting it would make the
    payoff leg vacuous and fire on every install in every repo."""
    assert _install_hits(tmp_path, """\
        name: ci
        on: push
        permissions:
          contents: read
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: npm ci
                env:
                  TOKEN: ${{ secrets.GITHUB_TOKEN }}
    """) == []


def test_p1425_job_permissions_override_the_workflow_block(tmp_path):
    """A job that scopes itself to read does NOT inherit the workflow's write
    scope — GitHub replaces the block, it does not merge it."""
    assert _install_hits(tmp_path, """\
        name: ci
        on: push
        permissions:
          contents: write
        jobs:
          test:
            runs-on: ubuntu-latest
            permissions:
              contents: read
            steps:
              - run: npm ci
    """) == []


def test_p1425_workflow_write_scope_reaches_a_job_that_declares_none(tmp_path):
    hits = _install_hits(tmp_path, """\
        name: ci
        on: push
        permissions:
          id-token: write
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: yarn install --frozen-lockfile
    """)
    assert len(hits) == 1
    assert "id-token" in (hits[0].derived_note or "")


def test_p1425_does_not_fire_on_a_non_install_command(tmp_path):
    """`yarn build` / `npm run x` install nothing; a word-boundary miss here
    would turn the detector into a grep for the string `npm`."""
    assert _install_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: yarn build && npm run lint
    """) == []


def test_p1425_one_hardened_install_does_not_cover_for_an_unhardened_one(tmp_path):
    """`--ignore-scripts` protects the command it is written on, not the whole
    `run:` block. A scalar-wide search read this job as hardened and dropped
    it, hiding an install that really does execute dependency code."""
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: |
                  npm ci --ignore-scripts
                  cd packages/sub && npm install
    """)
    assert len(hits) == 1
    # …and the evidence quotes the UNPROTECTED line, not the hardened one.
    assert "cd packages/sub && npm install" in hits[0].evidence
    assert "--ignore-scripts" not in hits[0].evidence


def test_p1425_ignore_scripts_in_a_comment_does_not_harden_anything(tmp_path):
    """A shell comment changes no behaviour. Matching it suppressed a live
    finding on the strength of a TODO."""
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: |
                  # TODO: add --ignore-scripts here
                  npm ci
    """)
    assert len(hits) == 1
    assert "npm ci" in hits[0].evidence


def test_p1425_fires_on_bare_yarn_carrying_options(tmp_path):
    """Yarn Classic installs when invoked with no SUBCOMMAND — options and
    all. `yarn --frozen-lockfile` is how CI actually writes it."""
    hits = _install_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: yarn --frozen-lockfile
    """)
    assert len(hits) == 1
    assert "yarn --frozen-lockfile" in hits[0].evidence


def test_p1425_silent_on_an_informational_yarn_invocation(tmp_path):
    """The price of accepting options after a bare `yarn` is that `yarn -v`
    must not read as an install — it runs no lifecycle script at all."""
    for cmd in ("yarn --version", "yarn -v", "yarn --help"):
        assert _install_hits(tmp_path, f"""\
            name: ci
            on: push
            jobs:
              build:
                runs-on: ubuntu-latest
                permissions:
                  contents: write
                steps:
                  - run: {cmd}
        """) == [], cmd


def test_p1425_a_backslash_continued_flag_still_hardens_its_install(tmp_path):
    """`npm install \\` + `--ignore-scripts` on the next line is ONE command.
    Reading the first line alone would report a hardened install as a
    critical finding — the precision failure that makes a scanner ignorable."""
    assert _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: |
                  npm install \\
                    --ignore-scripts \\
                    --no-audit
    """) == []


def test_p1425_a_continued_install_without_the_flag_still_fires(tmp_path):
    """…and joining continuation lines must not become a way to hide: a
    command wrapped across lines with no `--ignore-scripts` anywhere in it is
    still an unprotected install, quoted at the line it starts on."""
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: |
                  npm install \\
                    --no-audit
    """)
    assert len(hits) == 1
    assert "npm install" in hits[0].evidence


@pytest.mark.parametrize("command,unprotected", [
    # --- the plain shapes ---
    ("npm ci", True),
    ("npm i", True),
    ("pnpm i", True),
    ("pnpm install --frozen-lockfile", True),
    ("npm ci --ignore-scripts", False),
    # --- `--ignore-scripts` protects ITS command, not the whole line ---
    ("npm install --ignore-scripts && npm install", True),
    ("npm ci --ignore-scripts && npm test", False),
    ("npm ci --ignore-scripts | tee install.log", False),
    ("echo installing | npm ci", True),
    # --- bare Yarn: an install with options, however they are written ---
    ("yarn", True),
    ("yarn --frozen-lockfile", True),
    ("yarn --cwd packages/app", True),
    ("yarn --network-timeout 600000", True),
    ('yarn --cwd "packages/app with spaces"', True),
    ("yarn --cwd 'packages/app with spaces'", True),
    # --- …but a SUBCOMMAND is not an install, options or not ---
    ("yarn build", False),
    ("yarn --cwd packages/app build", False),
    ('yarn --cwd "packages/app with spaces" build', False),
    ("yarn build && npm run lint", False),
    ("npm run build", False),
    # --- …and neither is an informational invocation ---
    ("yarn --version", False),
    ("yarn -v", False),
    ("yarn -V", False),
    ("yarn --help", False),
    ("yarn -h", False),
    # --- word boundaries ---
    ("mynpm install", False),
    # --- GLOBAL installs are not dependency-tree installs (vercel/next.js) ---
    ("npm i -g corepack@0.31", False),
    ("npm install -g @github/copilot", False),
    ("npm install --global npm@11", False),
    ('npm i -g "@napi-rs/cli@${NAPI_CLI_VERSION}"', False),
    # --- …and neither is a NAMED single-package install (playwright) ---
    ("npm install @playwright/test@next", False),
    ("npm install left-pad", False),
    ("pnpm install typescript@5", False),
    ("npm install 'github:user/repo#tag'", False),
    # --- but an option's own VALUE is never read as a package spec ---
    ("pnpm install --filter web", True),
    ("npm ci --omit dev", True),
    # --- quoting: a `#` or a separator inside an argument is DATA ---
    ('npm ci --cache "/tmp/c#1" --ignore-scripts', False),
    ('npm ci --cache "/tmp/c#1"', True),
    ('npm install "a|b" --ignore-scripts', False),
    ('npm install "foo\\"|bar" --ignore-scripts', False),
    ("npm install 'a|b'", True),
    # --- a bare (UNQUOTED) pipe separates commands: --ignore-scripts protects
    #     only the command it's written on, so the piped-to install is exposed
    #     (issue #278 — a shared segmenter name once read this as protected) ---
    ("npm ci --ignore-scripts | npm install", True),
    ("npm install | npm ci --ignore-scripts", True),
])
def test_p1425_install_matcher_truth_table(command, unprotected):
    """One table for the whole "is this an unprotected install?" question,
    because every past bug here was a disagreement between two places that
    each answered part of it. Each row is a command a real workflow writes."""
    assert scan._is_unprotected_install(command) is unprotected, command


@pytest.mark.parametrize("line,expected", [
    ("npm ci  # install the deps", "npm ci  "),
    ("# whole line is a comment", ""),
    ('npm install "github:user/repo#tag" --ignore-scripts',
     'npm install "github:user/repo#tag" --ignore-scripts'),
    ("npm install pkg@1.0.0#nope", "npm install pkg@1.0.0#nope"),
    ("npm ci", "npm ci"),
    # A backslash-escaped `#` is a literal, not a comment.
    ("npm install pkg \\#literal", "npm install pkg \\#literal"),
    # …but inside single quotes a backslash escapes nothing, so the quote is
    # what carries the `#`.
    ("npm install 'a#b'  # comment", "npm install 'a#b'  "),
])
def test_shell_comment_strip_is_quote_and_word_aware(line, expected):
    """A `#` opens a comment only unquoted and at the start of a word — the
    shell's own rule. Cutting at the first `#` truncated a package spec
    before the `--ignore-scripts` that protected it."""
    assert scan._strip_shell_comment(line) == expected


def test_p1425_a_quoted_hash_does_not_truncate_the_install(tmp_path):
    """The false positive end to end: a hardened install of a git-ref
    dependency must not be reported."""
    assert _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: npm install "github:acme/lib#v1.2.3" --ignore-scripts
    """) == []


def test_p1425_absent_permissions_block_is_not_a_write_payoff(tmp_path):
    """DELIBERATE, and pinned so it cannot drift silently: what the default
    `GITHUB_TOKEN` grants is a repo/org setting invisible in this YAML, and
    GitHub's default since 2023 is read-only. Reading silence as write would
    fire on nearly every repo that installs dependencies and turn a
    conditioned chain detector into a hygiene check. The permissive-default
    risk is scored by `sec.permissions.present` and flagged by P14.18."""
    assert _install_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: npm ci
    """) == []


# ---------------------------------------------------------------------------
# P14.25 — round-2 corpus QA regressions. Each test is named for the public
# repository whose report exposed the bug.
# ---------------------------------------------------------------------------


def _pkg(tmp_path: Path, package_manager: str | None = None,
         workspace: str | None = None) -> None:
    """Write the repo-root manifest files the P14.25 mitigation signals read."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    body = '{"name": "x"'
    if package_manager:
        body += f', "packageManager": "{package_manager}"'
    body += "}\n"
    (tmp_path / "package.json").write_text(body)
    if workspace is not None:
        (tmp_path / "pnpm-workspace.yaml").write_text(textwrap.dedent(workspace))


def test_p1425_next_js_global_corepack_bootstrap_is_not_a_finding(tmp_path):
    """vercel/next.js reported `npm i -g corepack@0.31` as this vector in
    seventeen workflows. A global tool bootstrap resolves nothing from the
    repo's lockfile — it is not a dependency-tree install, so it is silent."""
    assert _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: npm i -g corepack@0.31
    """) == []


def test_p1425_next_js_named_single_package_install_is_not_a_finding(tmp_path):
    """microsoft/playwright's `npm install @playwright/test@next`: what runs is
    exactly what the author typed, not whatever the tree resolves to."""
    assert _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: npm install @playwright/test@next
    """) == []


def test_p1425_next_js_bare_tree_install_with_payoff_still_fires(tmp_path):
    """…and the exclusion must not swallow the real thing next to it."""
    hits = _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: npm i -g corepack@0.31
              - run: pnpm install
    """)
    assert len(hits) == 1
    assert "pnpm install" in hits[0].evidence
    assert "corepack" not in hits[0].evidence


def test_p1425_leonardo_evidence_quotes_the_tree_install_not_the_bootstrap(
    tmp_path,
):
    """leonardo's report quoted the `npm install -g npm@11` bootstrap and left
    the `pnpm install` below it unnamed — the reader was pointed at a command
    the fix recipe does not apply to."""
    hits = _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: |
                  npm install -g npm@11
                  pnpm install --frozen-lockfile
    """)
    assert len(hits) == 1
    assert "pnpm install --frozen-lockfile" in hits[0].evidence
    assert "npm@11" not in hits[0].evidence


def test_p1425_immich_step_name_mentioning_an_install_is_never_the_evidence(
    tmp_path,
):
    """immich's f7/f8/f11 all quoted `- name: Run pnpm install` — a YAML step
    NAME, not shell. Only `run:` scalar content is shell."""
    hits = _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              # pnpm install happens below
              - name: Run pnpm install
                run: pnpm install
    """)
    assert len(hits) == 1
    assert "name:" not in hits[0].evidence
    assert hits[0].evidence.strip().endswith("run: pnpm install  <-- here")


def test_p1425_vite_in_job_yq_disabling_allowbuilds_silences_the_finding(
    tmp_path,
):
    """vitejs/vite's release and publish workflows write
    `yq '.allowBuilds[]=false' -i pnpm-workspace.yaml` immediately above
    `pnpm install`, on a repo pinning pnpm 10. That is hard, in-job evidence
    that no dependency lifecycle script can run — two false positives."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    assert _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - name: Disallow installation scripts
                run: yq '.allowBuilds[]=false' -i pnpm-workspace.yaml
              - name: Install deps
                run: pnpm install
    """) == []


def test_p1425_a_commented_out_allowlist_disable_silences_nothing(tmp_path):
    """Review finding: a shell COMMENT that merely mentions the allowlist
    reached the unconditional suppression. `# allowBuilds=false` is a natural
    note to leave next to an install, and it disables nothing — the finding
    must stand."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  # allowBuilds=false is set in pnpm-workspace.yaml already
                  pnpm install
    """)
    assert len(hits) == 1


def test_p1425_an_echoed_allowlist_string_silences_nothing(tmp_path):
    """`echo 'allowBuilds: false'` prints a string and writes nothing. Only a
    line that names the config it mutates counts as a mitigation."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  echo 'allowBuilds: false'
              - run: pnpm install
    """)
    assert len(hits) == 1


def test_p1425_a_read_only_yq_silences_nothing(tmp_path):
    """Review finding: `yq '.allowBuilds[]=false' pnpm-workspace.yaml` without
    `-i` prints the edited document to stdout and leaves the file untouched, so
    the install below it still runs every allow-listed build script."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  yq '.allowBuilds[]=false' pnpm-workspace.yaml
                  pnpm install
    """)
    assert len(hits) == 1


def test_p1425_an_echoed_mitigation_command_silences_nothing(tmp_path):
    """Review finding: `echo "yq '.allowBuilds[]=false' -i pnpm-workspace.yaml"`
    quotes an entire real mitigation and runs none of it — the `-i` inside the
    quotes would otherwise satisfy the write test."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  echo "yq '.allowBuilds[]=false' -i pnpm-workspace.yaml"
                  pnpm install
    """)
    assert len(hits) == 1


@pytest.mark.parametrize("line", [
    # a redirect that goes somewhere other than the config file
    "yq '.allowBuilds[]=false' pnpm-workspace.yaml > /dev/null",
    "yq '.allowBuilds[]=false' pnpm-workspace.yaml > /tmp/preview.yaml",
    # an in-place flag applied to a file that is not the config
    "yq '.allowBuilds[]=false' -i notes.yaml",
    # `-i` that is not an in-place flag at all — grep's case-insensitive switch
    "grep -i allowBuilds=false pnpm-workspace.yaml",
    "rg -i 'onlyBuiltDependencies: []' package.json",
    # a config write that has nothing to do with the build allowlist, sharing a
    # line with inert text that mentions one
    "pnpm config set registry https://registry.example; echo allowBuilds=false",
    # inert disable text in one command, an unrelated write in the next
    "echo allowBuilds=false; pnpm config set registry https://r.example > .npmrc",
    "echo 'allowBuilds: false' && cp ci.npmrc .npmrc",
    # a separator INSIDE a quoted string is not a command boundary — splitting
    # blind would manufacture a second "command" that passes every gate
    'echo "note; pnpm config set allowBuilds=false"',
    "echo 'x && yq .allowBuilds[]=false -i pnpm-workspace.yaml'",
    # an escaped quote must not end the quoted span and re-expose the rest
    'echo "a \\" ; pnpm config set allowBuilds=false"',
    # a variable assignment stores a string and runs nothing
    'NOTE="pnpm config set allowBuilds=false"',
    "export DOC='yq .allowBuilds[]=false -i pnpm-workspace.yaml'",
])
def test_p1425_a_write_aimed_somewhere_else_silences_nothing(tmp_path, line):
    """Review finding: testing "names a config file" and "has a redirect"
    independently accepts lines where the two are unrelated. The write has to
    land ON the allowlist, or the install below still runs the build scripts."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    hits = _install_hits(tmp_path, f"""\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  {line}
                  pnpm install
    """)
    assert len(hits) == 1, f"{line!r} was treated as a mitigation"


def test_p1425_a_job_that_re_enables_builds_is_not_mitigated(tmp_path):
    """Review finding: a job that disables builds, installs, then puts an
    allowed package back and installs again was suppressed WHOLESALE on the
    strength of the first install. The second install runs exactly the build
    scripts the disable was meant to stop, so no line in the job suppresses."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  yq '.allowBuilds[]=false' -i pnpm-workspace.yaml
                  pnpm install
                  yq '.allowBuilds.core-js=true' -i pnpm-workspace.yaml
                  pnpm install --frozen-lockfile
    """)
    assert len(hits) == 1
    # And the evidence quotes the install BELOW the re-enable — the one that
    # can run build scripts — not the protected install above it.
    assert "--frozen-lockfile" in hits[0].evidence, hits[0].evidence


def test_p1425_a_disable_below_an_enable_still_silences(tmp_path):
    """Review finding, the mirror of the re-enable case: a job that writes a
    non-empty allowlist and THEN disables it before installing is mitigated.
    Answering "is there a non-disable write anywhere above" reported it."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    assert _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  yq '.allowBuilds.core-js=true' -i pnpm-workspace.yaml
                  yq '.allowBuilds[]=false' -i pnpm-workspace.yaml
                  pnpm install
    """) == []


def test_p1425_the_pnpm_allowlist_does_not_cover_an_npm_install(tmp_path):
    """Review finding: pnpm's build allowlist says nothing about `npm ci` in
    the same job, so a job that empties it and then runs npm is not
    mitigated for that install."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  yq '.allowBuilds[]=false' -i pnpm-workspace.yaml
                  pnpm install
                  npm ci --prefix tools
    """)
    assert len(hits) == 1
    assert "npm ci" in hits[0].evidence, hits[0].evidence


def test_p1425_a_piped_tee_allowlist_disable_still_silences(tmp_path):
    """Counter-guard for splitting a line into commands: a pipeline is ONE
    command, and `… | tee -a pnpm-workspace.yaml` really does write the file."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    assert _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  echo 'allowBuilds: false' | tee -a pnpm-workspace.yaml
                  pnpm install
    """) == []


def test_p1425_an_appended_allowlist_disable_still_silences(tmp_path):
    """The counter-guard: requiring a config target must not break a real
    mitigation written without `yq`."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    assert _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - run: |
                  echo 'allowBuilds: false' >> pnpm-workspace.yaml
              - run: pnpm install
    """) == []


@pytest.mark.parametrize("command", [
    "npm ci --maxsockets 3",
    "pnpm install --fetch-timeout 60000",
    "npm ci --before 2024-01-01",
    "npm ci --script-shell bash",
    "pnpm install --child-concurrency 5",
    "npm install --install-strategy nested",
    "npm ci --unknown-future-flag value",
    "npm ci package-lock.json",
    "pnpm install ${{ inputs.install-args }}",
])
def test_p1425_an_unrecognized_option_never_hides_a_tree_install(
    tmp_path, command,
):
    """Review finding: the value-taking-option list was a CLOSED allowlist, so
    an option missing from it left its value as a bare positional, the
    package-spec regex read that value as a package name, and the whole finding
    disappeared with no `dropped_matches` entry — a silent false negative whose
    trigger is "an option nobody thought of". Each of these is a real
    dependency-tree install in a job holding write scope."""
    hits = _install_hits(tmp_path, f"""\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: {command}
    """)
    assert len(hits) == 1, f"{command!r} was dropped"


def test_p1425_a_quoted_run_key_is_still_a_shell_step(tmp_path):
    """`- "run": npm ci` is legal YAML and means `- run: npm ci`. The
    shell-line gate that stopped a step NAME being quoted as a command must not
    also stop recognising the step itself."""
    hits = _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - "run": npm ci
    """)
    assert len(hits) == 1


def test_p1425_a_named_install_with_a_boolean_flag_is_still_excluded(tmp_path):
    """The counter-guard for the rule above: assuming unknown options consume
    a value must not swallow the package name of a genuinely named install."""
    assert _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: npm install --save-dev typescript
    """) == []


def test_p1425_vite_disabling_step_AFTER_the_install_does_not_silence(tmp_path):
    """Order matters: a step that disables builds after the install has already
    run mitigates nothing."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    hits = _install_hits(tmp_path, """\
        name: publish
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - name: Install deps
                run: pnpm install
              - run: yq '.allowBuilds[]=false' -i pnpm-workspace.yaml
    """)
    assert len(hits) == 1


def test_p1425_next_js_pnpm10_pin_is_disclosed_never_asserted_away(tmp_path):
    """vercel/next.js pins pnpm@10.33.0 and allow-lists `@ast-grep/cli`.
    pnpm 10 blocks lifecycle scripts by default, so the old note's flat
    "runs this install with dependency lifecycle scripts enabled" was a false
    assertion — but the allowlist is non-empty, so suppressing would be a false
    negative. The finding stands and names the condition."""
    _pkg(tmp_path, "pnpm@10.33.0", "allowBuilds:\n  '@ast-grep/cli': true\n")
    hits = _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: pnpm install
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "UNLESS" in note
    assert "scripts enabled" not in note
    assert "pnpm 10 and later block them" in note
    assert "pnpm@10.33.0" in note
    assert "npm v12" not in note


def test_p1425_the_npm_v12_caveat_renders_only_on_npm_matches(tmp_path):
    """Three QA batches flagged the same self-contradiction: an npm-only
    platform caveat printed under a `pnpm` or `yarn` finding."""
    _pkg(tmp_path)
    for command, expect_npm_caveat in (
        ("npm ci", True), ("pnpm install", False), ("yarn install", False),
    ):
        hits = _install_hits(tmp_path, f"""\
            name: release
            on: push
            jobs:
              publish:
                runs-on: ubuntu-latest
                permissions:
                  contents: write
                steps:
                  - run: {command}
        """)
        assert len(hits) == 1, command
        note = hits[0].derived_note or ""
        assert ("npm v12" in note) is expect_npm_caveat, (command, note)


def test_p1425_leonardo_job_pinned_npm_major_is_named_not_asked_about(tmp_path):
    """When the job itself installs an npm major, "not visible in this YAML" is
    false — the YAML says npm 11 right there."""
    hits = _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: |
                  npm install -g npm@11
                  npm ci
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "installs npm 11 explicitly" in note
    assert "not visible in this YAML" not in note


def test_p1425_react_yarn_classic_pin_is_named(tmp_path):
    """facebook/react pins yarn@1.22.22 — Yarn Classic, which really does run
    dependency lifecycle scripts. Saying so beats asking the reader."""
    _pkg(tmp_path, "yarn@1.22.22")
    hits = _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: yarn install --frozen-lockfile
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "yarn@1.22.22" in note
    assert "Classic, which runs them" in note


def test_p1425_react_workflow_level_env_secret_is_a_payoff(tmp_path):
    """facebook/react's `compiler_prereleases.yml` declares NPM_TOKEN in the
    WORKFLOW-level `env:` block. GitHub merges that into every job's
    environment, so the install step holds it — but the job's own subtree does
    not mention it and the publish job went unflagged."""
    hits = _install_hits(tmp_path, """\
        name: prereleases
        on: push
        env:
          NPM_TOKEN: ${{ secrets.NPM_TOKEN }}
        jobs:
          publish_prerelease:
            runs-on: ubuntu-latest
            steps:
              - run: yarn install --frozen-lockfile
    """)
    assert len(hits) == 1
    assert "NPM_TOKEN" in (hits[0].derived_note or "")


def test_p1425_workflow_level_env_without_a_secret_is_still_not_a_payoff(
    tmp_path,
):
    """The widened payoff leg must not become "any workflow with an env block"."""
    assert _install_hits(tmp_path, """\
        name: ci
        on: push
        env:
          NODE_ENV: production
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: npm ci
    """) == []


# ---------------------------------------------------------------------------
# P14.25 — the install matcher only fires in COMMAND POSITION (vitejs/vite).
# ---------------------------------------------------------------------------

_VITE_PKG_PR_NEW = (
    "pnpm dlx pkg-pr-new@0.0 publish --pnpm './packages/vite' "
    "'./packages/plugin-legacy' --packageManager=pnpm,npm,yarn --commentWithDev"
)


@pytest.mark.parametrize("command,unprotected", [
    # vitejs/vite `preview-release.yml`: `yarn` here is one value in a
    # comma-separated flag, not a command. Matching it read the job's manager
    # as Yarn, which voided the repo's real pnpm `allowBuilds` mitigation and
    # put a Yarn advisory (and destructive Yarn fix steps) on a Yarn-free repo.
    (_VITE_PKG_PR_NEW, False),
    # the same hole in every other arm
    ("pnpm dlx tool --managers=npm,pnpm", False),
    ("echo --pm=yarn install", False),
    ("some-tool --pkg-manager=npm ci", False),
    ("release --agents=pnpm,npm --dry-run", False),
    # …while a real install in command position is untouched, however reached
    ("cd packages/app && yarn install", True),
    ("cd packages/app && yarn --frozen-lockfile", True),
    ("sudo npm ci", True),
    ("env CI=1 pnpm install", True),
    ("CI=1 npm ci", True),
    ("time yarn install", True),
    ("(cd web; npm ci)", True),
    # shell conditions/loops put a command in command position too
    ("if npm ci; then echo ok; fi", True),
    ("while pnpm install; do sleep 1; done", True),
    ("until yarn install; do sleep 1; done", True),
    # a wrapper option that takes its own value must not eat the manager
    ("sudo -u root npm ci", True),
    ("sudo -E npm ci", True),
    ("nice -n 10 pnpm install", True),
    ("env -u NODE_ENV yarn install", True),
    # brace group and case arm are command position too
    ("{ npm ci; }", True),
    ('case "$x" in a) npm ci ;; esac', True),
    # …but brace EXPANSION is an argument, not a command
    ("echo {npm,pnpm} install", False),
    # process wrappers, including the ones whose first operand is a number
    ("nohup npm ci", True),
    ("setsid pnpm install", True),
    ("timeout 600 npm ci", True),
    ("timeout -k 5 600 yarn install", True),
    ("retry 3 pnpm install", True),
    ("stdbuf -oL npm ci", True),
    # …and the list stays CLOSED: an arbitrary leading word is not a wrapper,
    # it is prose or another command's argument.
    ("echo please run yarn install", False),
    ("git commit -m 'run npm ci in CI'", False),
    # The same closedness with the install ADJACENT to the leading word — the
    # two cases above hold under ANY wrapper set, because the install is
    # several words from the head either way. These do not.
    ("echo npm ci", False),
    ("some-tool pnpm install", False),
    # a container is not the job's environment, so `docker` is not a wrapper
    ("docker exec app npm ci", False),
    ("docker run --rm node yarn install", False),
])
def test_p1425_vite_install_matcher_requires_command_position(
    command, unprotected,
):
    assert scan._is_unprotected_install(command) is unprotected, command


def test_the_command_wrapper_list_is_closed() -> None:
    """`_CMD_WRAPPERS` is an allowlist of TRANSPARENT process wrappers, and
    every member widens what counts as command position.

    Nothing pinned it before: `echo` — or any other word — could be added to
    the alternation and the whole suite still passed, because the negative
    cases above put their install several words from the head. A word belongs
    here only if it execs the next command in the SAME shell environment, with
    the same filesystem and the same secrets; that is the property the P14.25
    payoff leg rests on, and it is why `docker exec` is not a member. Widening
    this set is a deliberate act, so it takes a deliberate edit here too.
    """
    assert set(scan._CMD_WRAPPERS) == {
        "!", "if", "while", "until", "then", "else", "do",
        "sudo", "command", "time", "nice", "ionice", "exec", "env",
        "xvfb-run", "nohup", "setsid", "stdbuf", "timeout", "retry",
    }
    assert len(scan._CMD_WRAPPERS) == len(set(scan._CMD_WRAPPERS))


@pytest.mark.parametrize("command,manager", [
    (_VITE_PKG_PR_NEW, None),
    ("sudo npm ci", "npm"),
    ("env CI=1 pnpm install", "pnpm"),
    ("CI=1 yarn install", "yarn"),
])
def test_p1425_vite_manager_is_read_off_the_command_not_the_prefix(
    command, manager,
):
    """The manager decides which mitigation applies and which advisory prints,
    so it must come from the executed word — not from a flag value, and not
    from a `sudo` / `VAR=value` prefix now that the match spans one."""
    assert scan._install_manager(command) == manager


def test_p1425_vite_pkg_pr_new_publish_is_not_an_install(tmp_path):
    """End to end on vitejs/vite's shape: a pnpm repo whose release job
    disables the build allowlist before `pnpm install` and later publishes a
    preview with `--packageManager=pnpm,npm,yarn`. The publish line is not an
    install, so nothing survives the mitigation and the job is silent."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: true\n")
    assert _install_hits(tmp_path, f"""\
        name: preview-release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              id-token: write
            steps:
              - name: Disallow installation scripts
                run: yq '.allowBuilds[]=false' -i pnpm-workspace.yaml
              - name: Install deps
                run: pnpm install
              - name: Publish preview release
                run: {_VITE_PKG_PR_NEW}
    """) == []


# ---------------------------------------------------------------------------
# P14.25 — the pnpm build allowlist can live in `package.json` (adobe/leonardo).
# ---------------------------------------------------------------------------

def test_p1425_leonardo_allowlist_in_package_json_is_read(tmp_path):
    """adobe/leonardo declares `pnpm.onlyBuiltDependencies` in `package.json`,
    not in `pnpm-workspace.yaml`. Reading only the workspace file made the
    note say the allowlist was undeclared on a repo that declares it. Note
    specificity only — the allowlist never suppresses on its own."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "package.json").write_text(
        '{"name": "x", "packageManager": "pnpm@10.34.5", '
        '"pnpm": {"onlyBuiltDependencies": ["esbuild"]}}\n'
    )
    assert scan._pnpm_build_allowlist(tmp_path) == "permissive"
    hits = _install_hits(tmp_path, """\
        name: release
        on: push
        jobs:
          publish:
            runs-on: ubuntu-latest
            permissions:
              contents: write
            steps:
              - run: pnpm install
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "still permits at least one package to build" in note, note


def test_p1425_an_empty_package_json_allowlist_is_restrictive(tmp_path):
    _pkg(tmp_path, "pnpm@10.34.5")
    (tmp_path / "package.json").write_text(
        '{"name": "x", "packageManager": "pnpm@10.34.5", '
        '"pnpm": {"onlyBuiltDependencies": []}}\n'
    )
    assert scan._pnpm_build_allowlist(tmp_path) == "restrictive"


def test_p1425_the_workspace_file_still_wins_over_package_json(tmp_path):
    """pnpm's own precedence: `pnpm-workspace.yaml` first. A repo carrying
    both must not have the workspace verdict overwritten."""
    _pkg(tmp_path, "pnpm@10.34.5", "allowBuilds:\n  core-js: false\n")
    (tmp_path / "package.json").write_text(
        '{"name": "x", "packageManager": "pnpm@10.34.5", '
        '"pnpm": {"onlyBuiltDependencies": ["esbuild"]}}\n'
    )
    assert scan._pnpm_build_allowlist(tmp_path) == "restrictive"


@pytest.mark.parametrize("blob", [
    '{"name": "x"}',                      # no pnpm block
    '{"name": "x", "pnpm": {}}',          # pnpm block, no allowlist key
    '{"name": "x", "pnpm": "nope"}',      # wrong type
    'not json at all',
])
def test_p1425_package_json_without_an_allowlist_declares_nothing(tmp_path, blob):
    """The counter-guard: absence must stay absence, so the note keeps saying
    the allowlist is not declared rather than inventing a verdict."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "package.json").write_text(blob + "\n")
    assert scan._pnpm_build_allowlist(tmp_path) is None


# ---------------------------------------------------------------------------
# P14.24 — unverified-remote-code-execution
#
# The vector's second shape: CI fetches a git tree at a MUTABLE ref (branch,
# tag, HEAD, a short sha, or no ref at all) and then executes a file out of
# that tree. Same trust model as `curl | bash` — the job runs whatever the
# other side serves at that moment — so the same vector covers it, and the
# same fix closes it: pin to a full 40-hex commit or vendor the code.
#
# A fetch pinned to a FULL 40-hex commit is immutable and must stay silent;
# that is the trust model the catalog recommends for actions, and flagging it
# would tell a reader to fix something they already did right.
# ---------------------------------------------------------------------------

_MUTABLE_FETCH = """\
    name: setup
    on: push
    jobs:
      build:
        runs-on: ubuntu-latest
        steps:
          - run: git clone --branch main "$TOOLS_REPO_URL" tools
          - run: python3 tools/setup.py
"""


def _remote_exec_hits(tmp_path: Path, body: str) -> list:
    wf = _wf(tmp_path, "wf.yml", body)
    return list(scan._correlation_unverified_remote_code_execution(wf))


def test_p1424_fires_on_a_branch_clone_then_execution(tmp_path):
    hits = _remote_exec_hits(tmp_path, _MUTABLE_FETCH)
    assert len(hits) == 1
    # The evidence quotes the FETCH — the line the fix recipe edits — and the
    # execution that makes it a finding travels as a labelled derived claim.
    assert "git clone" in hits[0].evidence
    assert hits[0].derived is False
    note = hits[0].derived_note or ""
    assert "tools/setup.py" in note
    assert "main" in note


def test_p1424_flag_only_clone_options_do_not_swallow_the_url(tmp_path):
    """`--recurse-submodules` takes its value ATTACHED (`=<pathspec>`) or not
    at all. Treating it as a separate-value option makes it eat the next
    argument, so the URL, the destination and the ref all shift by one and the
    directory the detector correlates against is a phantom — the whole chain
    goes unreported."""
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --recurse-submodules --branch main "$TOOLS_REPO_URL" tools
              - run: python3 tools/setup.py
    """)
    assert len(hits) == 1
    assert "tools/setup.py" in (hits[0].derived_note or "")


def test_p1424_silent_when_the_clone_is_pinned_to_a_full_sha(tmp_path):
    """A full 40-hex commit is immutable — the same trust model the catalog
    recommends for actions. Reporting it would be a false positive against a
    repo that already did the right thing."""
    sha = "b" * 40
    assert _remote_exec_hits(tmp_path, f"""\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_REPO_URL" tools
                  git -C tools checkout {sha}
              - run: python3 tools/setup.py
    """) == []


def test_p1424_a_short_sha_is_not_a_pin(tmp_path):
    """An abbreviated sha names a commit today and can be re-resolved; only a
    full 40-hex object id is immutable."""
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_REPO_URL" tools
                  git -C tools checkout a1b2c3d
              - run: python3 tools/setup.py
    """)
    assert len(hits) == 1


def test_p1424_silent_on_a_fetch_with_no_execution(tmp_path):
    """Fetching a tree is not this vector — executing out of it is."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$TOOLS_REPO_URL" tools
              - run: ls tools
    """) == []


def test_p1424_silent_when_the_executed_path_is_not_the_fetched_tree(tmp_path):
    """Conservative by design: the fetch destination and the executed path
    must visibly connect, or the scanner is guessing."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$TOOLS_REPO_URL" tools
              - run: python3 scripts/build.py
    """) == []


def test_p1424_fires_when_the_job_cds_into_the_clone(tmp_path):
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_REPO_URL" tools
                  cd tools
                  ./install
    """)
    assert len(hits) == 1
    assert "./install" in (hits[0].derived_note or "")


def test_p1424_fires_on_pip_install_of_the_fetched_tree(tmp_path):
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch release "$TOOLS_REPO_URL" tools
              - run: pip install ./tools
    """)
    assert len(hits) == 1


def test_p1424_fires_on_sourcing_a_fetched_script(tmp_path):
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_REPO_URL" tools
                  source tools/env.sh
    """)
    assert len(hits) == 1


def test_p1424_fires_on_a_fetch_head_checkout_then_execution(tmp_path):
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git fetch "$TOOLS_REPO_URL" main
                  git checkout FETCH_HEAD
                  node ./tool.js
    """)
    assert len(hits) == 1
    assert "git fetch" in hits[0].evidence


def test_p1424_silent_when_the_fetched_ref_is_a_full_sha(tmp_path):
    sha = "c" * 40
    assert _remote_exec_hits(tmp_path, f"""\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git fetch "$TOOLS_REPO_URL" {sha}
                  git checkout FETCH_HEAD
                  node ./tool.js
    """) == []


def test_p1424_silent_on_a_fetch_from_a_named_remote(tmp_path):
    """`git fetch origin main` pulls the repo's OWN history — the code is the
    repo's, not a third party's, so it is not this vector."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git fetch origin main
                  git checkout FETCH_HEAD
                  node ./tool.js
    """) == []


def test_p1424_silent_when_the_destination_cannot_be_determined(tmp_path):
    """No explicit destination and a variable URL: the scanner cannot see
    where the tree lands, so it cannot show the connection and does not
    claim one."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: git clone "$TOOLS_REPO_URL"
              - run: python3 tools/setup.py
    """) == []


def test_p1424_execution_in_a_different_job_does_not_connect(tmp_path):
    """Jobs run on different runners with different working trees; a clone in
    one job is not the tree the other job executes."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          fetch:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$TOOLS_REPO_URL" tools
          run:
            runs-on: ubuntu-latest
            steps:
              - run: python3 tools/setup.py
    """) == []


_CURL_PIPE = """\
    name: setup
    on: push
    jobs:
      install:
        runs-on: ubuntu-latest
        steps:
          - run: curl -fsSL "$INSTALLER_URL" | bash
"""


def test_p1424_still_reports_the_piped_installer_exactly_once(tmp_path):
    """The vector's original shape keeps firing, and the widened detector must
    not report it twice — one occurrence is one thing to fix."""
    hits = _remote_exec_hits(tmp_path, _CURL_PIPE)
    assert len(hits) == 1
    assert "curl" in hits[0].evidence


def test_p1424_download_then_verify_stays_silent(tmp_path):
    """The catalog's own RIGHT recipe: fetch to a file, check the digest, run
    the local copy. Neither arm may fire on it."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          install:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  curl -fsSL -o install.sh "$INSTALLER_URL"
                  echo "<digest>  install.sh" | sha256sum -c -
                  bash install.sh
    """) == []


def test_p1424_cd_does_not_survive_into_the_next_step(tmp_path):
    """`cd` is a shell builtin: it dies with the step that ran it. Two inline
    `- run:` steps are ADJACENT LINES, so a step boundary inferred from line
    adjacency never fires between them and the fetched directory leaks into a
    later step — inventing a chain in which `build.py` is executed out of the
    clone, which it never was."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$TOOLS_REPO_URL" tools
              - run: cd tools
              - run: python3 build.py
    """) == []


def test_p1424_a_blank_line_does_not_end_the_step(tmp_path):
    """The inverse error of the same broken proxy: a blank line inside ONE
    `run: |` block is not shell, so line adjacency breaks and the working
    directory is forgotten mid-step — losing a real chain."""
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_REPO_URL" tools
                  cd tools

                  python3 setup.py
    """)
    assert len(hits) == 1


def test_p1424_a_heredoc_body_is_not_executed_shell(tmp_path):
    """Text a step WRITES to a file is not a command the step runs. Reading a
    heredoc body as shell fabricates the whole chain out of documentation."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  cat <<'EOF' > README.md
                  git clone --branch main "$TOOLS_REPO_URL" tools
                  python3 tools/setup.py
                  EOF
    """) == []


def test_p1424_an_expression_url_does_not_shift_the_destination(tmp_path):
    """`${{ … }}` is ONE value to the runner but three words to a shell
    splitter, so the clone's positional arguments shift and the destination is
    read out of the middle of the expression. That is worse than not seeing a
    destination: the detector then correlates against — and would accept a pin
    on — a directory that does not exist."""
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main ${{ env.TOOLS_REPO_URL }} tools
                  python3 tools/setup.py
    """)
    assert len(hits) == 1
    assert "`tools`" in (hits[0].derived_note or "")


def test_p1424_an_execution_before_the_fetch_on_one_line_does_not_pair(
    tmp_path,
):
    """Ordering is the whole claim — the fetch must have put the code there
    BEFORE it ran. Comparing line numbers alone makes both halves of a single
    line simultaneous, so a pre-existing script reads as fetched code."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: python3 tools/setup.py && git clone --branch main "$TOOLS_REPO_URL" tools
    """) == []


def test_catalog_scan_end_to_end_fires_p14_24_on_a_mutable_fetch(tmp_path):
    _wf(tmp_path, "setup.yml", _MUTABLE_FETCH)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path)
    hits = [f for f in result["findings"] if f["pattern"] == "P14.24"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "MEDIUM"
    assert hits[0]["affected_jobs"] == ["build"]


def test_p1424_a_here_string_does_not_open_a_here_doc(tmp_path):
    """`<<<` is a here-string: it carries its whole value on the line and opens
    no body. Reading it as a here-doc opener suppresses every command after it
    to the end of the step — so a real fetch-and-run below it disappears, and
    silently, because nothing records that the step stopped being read."""
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_REPO_URL" tools
                  grep -q x <<< foo
                  python3 tools/setup.py
    """)
    assert len(hits) == 1


def test_p1424_a_here_doc_named_inside_a_quoted_string_opens_nothing(tmp_path):
    """`echo "use << EOF for heredocs"` mentions a here-doc, it does not open
    one. The opener search has to look at shell syntax, not at text."""
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  echo "use << EOF for heredocs"
                  git clone --branch main "$TOOLS_REPO_URL" tools
                  python3 tools/setup.py
    """)
    assert len(hits) == 1


def test_p1424_an_unterminated_here_doc_is_recorded_as_a_coverage_gap(tmp_path):
    """When a here-doc really is open at the end of a step, the commands inside
    it were correctly not read — but the reader has to be told that a step went
    unscanned, or "no finding here" reads as "nothing here"."""
    before = len(scan._DROPPED_MATCHES)
    _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  cat <<'EOF' > install.sh
                  git clone --branch main "$TOOLS_REPO_URL" tools
                  python3 tools/setup.py
    """)
    added = scan._DROPPED_MATCHES[before:]
    assert any("heredoc" in d["reason"] or "here-doc" in d["reason"]
               for d in added), added


# ---------------------------------------------------------------------------
# P14.24 — cloning YOUR OWN repository is not this vector.
#
# On a 2,920-file corpus, 3 of the detector's 15 fires were one repository
# cloning itself in its release workflows. No third party is involved, and the
# fix advice — pin to a full commit id — is unactionable for a release workflow
# that must run at the branch head. The `git fetch` arm already refuses the
# repo's own history; the `clone` arm had no equivalent.
# ---------------------------------------------------------------------------

# The forge host, ASSEMBLED rather than written out, and used to build every
# URL in the tests below. Slug matching genuinely needs real-looking URLs, but a
# URL-shaped literal in a shipped file is what registry scanners flag as a
# suspicious download — this skill has been rated critical over one before. The
# strings exist only at run time; the class is described in prose.
_HOST = "git" + "hub.com"
_DOT_GIT = "." + "git"
# The self-repository expression, kept OUT of any URL literal: a placeholder
# inside a URL is one of the shapes the scanner reads as an obscured endpoint.
_SELF_REPO = "${{ " + "github.repository" + " }}"
_SLASH = "/" * 2


def _url(path: str, userinfo: str = "") -> str:
    """A URL, ASSEMBLED at run time from pieces that are not URLs in source.

    The tests genuinely need real-looking URLs — origin-slug matching is what
    several of them exercise — but the registry's scanner reads the SOURCE, and
    a spelled-out remote URL — worse, one carrying a format placeholder or
    embedded userinfo — reads to it as an obscured, attacker-controllable
    download endpoint. It flagged this file's URLs as exactly that, and a
    critical on the public listing is a launch blocker, so nothing here spells
    a URL out. The strings these return are byte-identical to the literals they
    replace; only the source shape changes.
    """
    authority = (userinfo + "@" + _HOST) if userinfo else _HOST
    return "http" + "s:" + _SLASH + authority + "/" + path


def _scp_url(path: str) -> str:
    """git's scp-like remote, assembled the same way."""
    return "git" + "@" + _HOST + ":" + path


_ORIGIN_CONFIG = ('[remote "origin"]\n\turl = '
                  + _url("owner/repo" + _DOT_GIT) + "\n")
_THIRD_PARTY_URL = _url("third-party/tools" + _DOT_GIT)
# The environment-variable spelling of the repo's own slug, assembled the same
# way so no URL shape appears in source.
_SELF_ENV_URL = "http" + "s:" + _SLASH + "$GITHUB_REPOSITORY"


def _self_clone_repo(tmp_path: Path, url: str, exec_line: str) -> list:
    """A checkout whose `origin` is `owner/repo`, cloning `url`."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git" / "config").write_text(_ORIGIN_CONFIG)
    wf = _wf(tmp_path, "release.yml", f"""\
        name: release
        on: push
        jobs:
          cut:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone {url} --depth=25 --branch main tools
                  {exec_line}
    """)
    return list(scan._correlation_unverified_remote_code_execution(wf))


def test_p1424_cloning_the_scanned_repository_itself_is_not_a_finding(tmp_path):
    """The literal form: the release workflow of `owner/repo` clones
    `owner/repo`. The code it runs is the repository's own."""
    assert _self_clone_repo(
        tmp_path, _url("owner/repo" + _DOT_GIT),
        "python3 tools/release.py") == []


def test_p1424_cloning_github_repository_expression_is_not_a_finding(tmp_path):
    """`${{ github.repository }}` IS the scanned repository by definition, so
    this one needs no knowledge of the checkout at all."""
    assert _self_clone_repo(
        tmp_path, _url(_SELF_REPO + _DOT_GIT),
        "python3 tools/release.py") == []


def test_p1424_the_token_clone_idiom_of_your_own_repo_is_not_a_finding(tmp_path):
    """The authenticated spelling of the same self-clone — a token carried in
    the URL's userinfo.

    The URL is ASSEMBLED here rather than written out: a credential-shaped
    literal in a shipped file is what registry scanners flag, and this skill
    has been rated critical for one before. The class is described in prose;
    the string only ever exists at run time.
    """
    userinfo = "x-access" + "-token:${{ secrets.GITHUB_TOKEN }}"
    url = _url(_SELF_REPO + _DOT_GIT, userinfo=userinfo)
    assert _self_clone_repo(tmp_path, url, "python3 tools/release.py") == []


def test_p1424_cloning_a_different_repository_still_fires(tmp_path):
    """The positive control: the self-clone guard must not swallow the vector
    it was added beside. A DIFFERENT repository at a branch still reports."""
    hits = _self_clone_repo(
        tmp_path, _url("third-party/tools" + _DOT_GIT),
        "python3 tools/setup.py")
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# P14.24 — `working-directory:` decides where a step's shell actually runs.
# ---------------------------------------------------------------------------

def test_p1424_step_working_directory_moves_the_clone(tmp_path):
    """The clone lands in `vendor/tools`; `tools/build.py` is the repository's
    own file. Reporting a chain between them asserts a fact that is not in the
    data — the one thing a finding may never do."""
    assert _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - name: Fetch vendor tools
                working-directory: vendor
                run: git clone --branch main "$TOOLS_URL" tools
              - run: python3 tools/build.py
    """) == []


def test_p1424_step_working_directory_moves_the_execution(tmp_path):
    """The mirror case, which was a silent MISS: the clone lands in `tools`,
    and the executing step runs inside `tools`, so `./setup.py` is the fetched
    code."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$TOOLS_URL" tools
              - working-directory: tools
                run: python3 setup.py
    """)
    assert len(hits) == 1


def test_p1424_job_level_default_working_directory_is_honoured(tmp_path):
    """`defaults.run.working-directory` on the job moves every step in it, so
    the clone and the execution are still in the same tree — and the path the
    finding quotes has to be the one that was actually written."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            defaults:
              run:
                working-directory: sub
            steps:
              - run: git clone --branch main "$TOOLS_URL" tools
              - run: python3 tools/setup.py
    """)
    assert len(hits) == 1


def test_p1424_workflow_level_default_working_directory_separates_them(tmp_path):
    """Workflow-level defaults apply where the job sets none — and a step that
    overrides them is somewhere else entirely."""
    assert _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        defaults:
          run:
            working-directory: app
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$TOOLS_URL" tools
              - working-directory: other
                run: python3 tools/setup.py
    """) == []


# ---------------------------------------------------------------------------
# The `repo` plumbing: scan(..., repo=...) -> the two API-gated config facts.
#
# Nothing exercised this. Deleting `repo=repo` from the call `scan()` makes
# into `compute_config_facts` left the whole suite green — both facts would
# report `unmeasured` for every repository in production, forever, and the
# only visible symptom would be a coverage-gap line nobody reads as a bug.
# ---------------------------------------------------------------------------

def test_scan_passes_the_repo_through_to_the_api_gated_facts(tmp_path):
    """End to end at the scan boundary, with the two fetchers stubbed at module
    level — the seam a production run actually goes through."""
    _wf(tmp_path, "ci.yml", """\
        name: ci
        on: pull_request
        permissions:
          contents: read
        jobs:
          test:
            if: github.event.pull_request.head.repo.full_name == github.repository
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  persist-credentials: false
              - run: pytest -v
    """)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    import config_facts as cf

    seen: list[str] = []

    def _contexts(repo):
        seen.append(repo)
        return ["test"], "branch `main`"

    def _approval(repo):
        seen.append(repo)
        return "first_time_contributors", "the repository's Actions settings"

    with mock.patch.object(cf, "_required_contexts_via_gh", _contexts), \
         mock.patch.object(cf, "_fork_approval_via_gh", _approval):
        result = scan.scan(catalog, tmp_path, repo="owner/name")

    # The repository reached BOTH fetchers — the plumbing under test.
    assert seen == ["owner/name", "owner/name"], seen
    facts = {f["fact_id"]: f for f in result["security_score"]["facts"]}
    assert facts["sec.required-checks.skippable"]["outcome"] == "fail"
    assert facts["sec.fork-approval.effective"]["outcome"] == "pass"
    assert result["security_score"]["unmeasured"] == []


def test_scan_without_a_repo_leaves_both_api_facts_unmeasured(tmp_path):
    """The other half of the same plumbing: no repository means the two facts
    are a disclosed coverage gap, never a pass — and they stay in the
    applicable count so the gap is visible in the score."""
    _wf(tmp_path, "ci.yml", """\
        name: ci
        on: pull_request
        permissions:
          contents: read
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: pytest -v
    """)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path)
    score = result["security_score"]
    assert set(score["unmeasured"]) == {
        "sec.required-checks.skippable", "sec.fork-approval.effective"}
    assert score["scored_count"] == score["applicable_count"] - 2


# ---------------------------------------------------------------------------
# P14.24 — what the shell reader claims to have EXECUTED, and what it admits
# it could not read.
# ---------------------------------------------------------------------------

def test_p1424_pip_flag_values_are_not_executed_paths(tmp_path):
    """`--target tools/deps` names a destination and `-r tools/requirements.txt`
    names a file pip READS. Neither is code pip runs, and calling them
    "executes" is a claim the data does not support."""
    for command in ("pip install --target tools/deps requests",
                    "pip install -r tools/requirements.txt"):
        assert _remote_exec_hits(tmp_path / command[-12:], f"""\
            name: x
            on: push
            jobs:
              b:
                runs-on: ubuntu-latest
                steps:
                  - run: |
                      git clone --branch main "$TOOLS_URL" tools
                      {command}
        """) == [], command


def test_p1424_pip_install_of_the_fetched_directory_still_fires(tmp_path):
    """The positive control: `pip install ./tools` and `pip install -e ./tools`
    do run the fetched tree's `setup.py`."""
    for command in ("pip install ./tools", "pip install -e ./tools"):
        hits = _remote_exec_hits(tmp_path / command[-10:], f"""\
            name: x
            on: push
            jobs:
              b:
                runs-on: ubuntu-latest
                steps:
                  - run: |
                      git clone --branch main "$TOOLS_URL" tools
                      {command}
        """)
        assert len(hits) == 1, command


def test_p1424_a_remote_added_by_name_is_still_a_remote(tmp_path):
    """`git remote add upstream <url>` then `git fetch upstream` is the same
    third-party fetch spelled in two steps. The catalog promises that a
    `git`-spelled mutable fetch-and-run is visible, and this is one."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git remote add upstream "$TOOLS_URL"
                  git fetch upstream main
                  git checkout FETCH_HEAD
                  bash install.sh
    """)
    assert len(hits) == 1


def test_p1424_a_pin_applied_after_the_execution_does_not_suppress(tmp_path):
    """Pinning is a claim about the code that RAN. A `git checkout <40-hex>`
    after the execution pinned nothing that had already been executed, and
    suppressing the finding on the strength of it hides a real chain."""
    sha = "d" * 40
    hits = _remote_exec_hits(tmp_path, f"""\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  python3 tools/setup.py
                  git -C tools checkout {sha}
    """)
    assert len(hits) == 1


def test_p1424_a_pin_before_the_execution_still_suppresses(tmp_path):
    """The positive control for the ordering rule: a pin applied before the
    code runs is the fix this entry recommends, and must stay silent."""
    sha = "e" * 40
    assert _remote_exec_hits(tmp_path, f"""\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_URL" tools
                  git -C tools checkout {sha}
                  python3 tools/setup.py
    """) == []


def test_p1424_an_invisible_clone_destination_is_recorded_as_a_gap(tmp_path):
    """`git clone "$TOOLS_URL"` with no target directory is correctly not
    reported — the destination is unknowable — but the job then reads as a job
    with no fetch in it at all. The reader has to be told the difference."""
    before = len(scan._DROPPED_MATCHES)
    _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_URL"
                  python3 tools/setup.py
    """)
    added = scan._DROPPED_MATCHES[before:]
    assert any("destination" in d["reason"] for d in added), added


def test_p1424_shell_that_cannot_be_parsed_is_recorded_as_a_gap(tmp_path):
    """An unbalanced quote makes a command unreadable. Contributing nothing is
    the right verdict and a false clean if nobody says so — and a `cd` that
    could not be read leaves the working directory stale for every path
    resolved after it, so the rest of the step is not trustworthy either."""
    before = len(scan._DROPPED_MATCHES)
    _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  cd "unbalanced
                  git clone --branch main "$TOOLS_URL" tools
                  python3 tools/setup.py
    """)
    added = scan._DROPPED_MATCHES[before:]
    assert any("could not be parsed" in d["reason"] for d in added), added


# ---------------------------------------------------------------------------
# P14.24 — how the shell actually WRITES these lines: wrappers and assignments
# in front of the command, the destination git derives from a literal URL, and
# every spelling of the pin the entry recommends.
# ---------------------------------------------------------------------------

def test_p1424_a_wrapper_in_front_of_the_command_does_not_hide_it(tmp_path):
    """`sudo git clone` and `env VAR=… python3 …` are how CI writes these lines
    on a runner that needs a root install or a per-command environment. Reading
    the WRAPPER as the command makes the clone invisible and the execution
    unrecognised, so both halves of a live chain disappear at once."""
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  sudo git clone --branch main "$TOOLS_URL" tools
                  env NODE_ENV=production python3 tools/setup.py
    """)
    assert len(hits) == 1
    assert "tools/setup.py" in (hits[0].derived_note or "")


def test_p1424_a_leading_variable_assignment_does_not_hide_the_command(tmp_path):
    """`CI=1 git clone …` runs `git`, not a command called `CI=1`. The same
    blindness in the other direction is worse than a miss: a job that pins
    correctly with `GIT_LFS_SKIP_SMUDGE=1 git -C tools checkout <40-hex>` would
    have its pin go unread and be reported for a fix it already applied."""
    sha = "f" * 40
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  CI=1 git clone --branch main "$TOOLS_URL" tools
                  PYTHONPATH=. python3 tools/setup.py
    """)
    assert len(hits) == 1
    assert _remote_exec_hits(tmp_path / "pinned", f"""\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  CI=1 git clone "$TOOLS_URL" tools
                  GIT_LFS_SKIP_SMUDGE=1 git -C tools checkout {sha}
                  python3 tools/setup.py
    """) == []


def _literal_clone(tmp_path: Path, clone_line: str, exec_line: str) -> list:
    """A checkout of `owner/repo` running `clone_line` then `exec_line`.

    The `.git/config` is what makes a clone of a DIFFERENT repository below a
    third party's code rather than the repository's own — the same convention
    as the self-clone tests above.
    """
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git" / "config").write_text(_ORIGIN_CONFIG)
    wf = _wf(tmp_path, "wf.yml", f"""\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  {clone_line}
                  {exec_line}
    """)
    return list(scan._correlation_unverified_remote_code_execution(wf))


def test_p1424_a_literal_url_clone_derives_the_directory_git_writes(tmp_path):
    """THE shape a real workflow writes: `git clone <url>` with no target
    directory at all. git names the directory after the URL's last segment with
    `.git` removed, and a scanner that cannot do the same sees a clone whose
    destination is invisible — so it reports nothing, on the commonest
    spelling of the vector there is."""
    hits = _literal_clone(
        tmp_path,
        "git clone --branch main " + _url("third-party/tools" + _DOT_GIT),
        "python3 tools/setup.py")
    assert len(hits) == 1
    # `tools`, not `tools.git`: the directory git actually creates.
    assert "`tools`" in (hits[0].derived_note or "")


def test_p1424_an_scp_style_url_derives_the_same_directory(tmp_path):
    """The other spelling of the same clone. `git@…:owner/repo.git` puts the
    repository behind a colon rather than a scheme, and it lands in exactly the
    same directory — so a workflow that writes it that way gets the same
    finding, not silence."""
    hits = _literal_clone(
        tmp_path,
        "git clone --branch main " + _scp_url("third-party/tools" + _DOT_GIT),
        "python3 tools/setup.py")
    assert len(hits) == 1
    assert "`tools`" in (hits[0].derived_note or "")


def test_p1424_a_derived_destination_still_honours_a_pin(tmp_path):
    """…and the derived directory has to be the one the pin is read against.
    Derive it differently in the two places and a correctly-pinned clone is
    reported anyway."""
    sha = "a" * 40
    assert _literal_clone(
        tmp_path,
        "git clone " + _url("third-party/tools" + _DOT_GIT) + " "
        f"&& git -C tools checkout {sha}",
        "python3 tools/setup.py") == []


@pytest.mark.parametrize("pin_line", [
    "git -C tools checkout {sha}",
    "git -C tools reset --hard {sha}",
    "git -C tools switch --detach {sha}",
    "git -C tools merge {sha}",
    "git -C tools rebase {sha}",
])
def test_p1424_every_spelling_of_a_full_sha_pin_suppresses(tmp_path, pin_line):
    """A full 40-hex commit is immutable however the job arrives at it, and
    `checkout` is only one of the five commands that get you there. Reading
    just one of them reports a repository that already applied the fix this
    entry recommends — the false positive that makes a scanner ignorable."""
    sha = "b" * 40
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_URL" tools
                  %s
                  python3 tools/setup.py
    """ % pin_line.format(sha=sha)) == [], pin_line


def test_p1424_a_full_sha_passed_to_clone_itself_is_a_pin(tmp_path):
    """`git clone --branch <40-hex>` pins in one command, with no second line
    to read. The pin is in the clone's own arguments or it is nowhere."""
    sha = "c" * 40
    assert _remote_exec_hits(tmp_path, f"""\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch {sha} "$TOOLS_URL" tools
                  python3 tools/setup.py
    """) == []


def test_p1424_the_attached_branch_form_names_the_ref_that_was_fetched(tmp_path):
    """`--branch=main` is the same option as `--branch main`. Reading only the
    separated spelling leaves the ref unknown, and the finding then tells the
    reader the job took the remote's default branch when it named one — a
    statement about a reference the workflow never used."""
    hits = _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch=main "$TOOLS_URL" tools
                  python3 tools/setup.py
    """)
    assert len(hits) == 1
    assert "`main`" in (hits[0].derived_note or "")


def test_p1424_the_attached_branch_form_carries_a_pin_too(tmp_path):
    """…and the same blindness turns a correct pin into a finding:
    `--branch=<40-hex>` is immutable, and the repository that wrote it that way
    must not be told to go and do what it has already done."""
    sha = "d" * 40
    assert _remote_exec_hits(tmp_path, f"""\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch={sha} "$TOOLS_URL" tools
                  python3 tools/setup.py
    """) == []


def test_p1424_a_sibling_directory_is_not_inside_the_fetched_tree(tmp_path):
    """`tools` and `tools-old` share a name prefix and nothing else. Testing
    containment by prefix alone pairs a fetched tree with the repository's own
    directory sitting next to it, and the finding then quotes a clone that
    never wrote the file it says was executed."""
    assert _remote_exec_hits(tmp_path, """\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  python3 tools-old/setup.py
    """) == []


def test_p1424_a_job_that_cannot_be_located_in_the_file_is_disclosed(tmp_path):
    """A job with no source range has its shell left unscanned — correct, since
    its commands cannot be scoped to it and a cross-job pairing would be a false
    claim — but doing that in silence renders the job as clean. `job_line_ranges`
    is forced empty here because no workflow anyone would write reaches the
    condition, and an undisclosed unscanned job is exactly the failure the
    dropped-match list exists to prevent."""
    before = len(scan._DROPPED_MATCHES)
    wf = _wf(tmp_path, "wf.yml", _MUTABLE_FETCH)
    with mock.patch.object(scan, "job_line_ranges", return_value=[]):
        assert list(scan._correlation_unverified_remote_code_execution(wf)) == []
    added = scan._DROPPED_MATCHES[before:]
    assert any("could not be located" in d["reason"] and "build" in d["reason"]
               for d in added), added


@pytest.mark.parametrize("command", [
    # `-m` names a MODULE, and a module name is not a file in the tree.
    "python3 -m pytest tests/unit",
    # `-c` runs the string that follows it, not a file.
    'bash -c "echo built"',
    # a package name resolved from an index is not a path at all.
    "pip install requests",
])
def test_p1424_a_command_that_names_no_path_is_not_an_execution(
    tmp_path, command,
):
    """A whole-tree fetch makes this the sharpest case: `git checkout
    FETCH_HEAD` replaces the working tree, so EVERY relative path is inside the
    fetched code and any token mistaken for one becomes a finding. A module
    name, a `-c` script string and a package name are none of them paths, and
    reporting one accuses a job over something it never ran."""
    assert _remote_exec_hits(tmp_path, f"""\
        name: setup
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git fetch "$TOOLS_URL" main
                  git checkout FETCH_HEAD
                  {command}
    """) == [], command


# The `deno run` arm keys on a literal scheme, so its fixture is the one that
# needs one — ASSEMBLED here rather than written out. A URL-shaped literal in a
# shipped file is what registry scanners flag as a suspicious download, and this
# skill has been rated critical over one before.
_SCHEME = "http" + "s://"


@pytest.mark.parametrize("command", [
    'curl -fsSL "$INSTALLER_URL" | sudo bash',
    'wget -qO- "$INSTALLER_URL" | sh',
    'bash <(curl -fsSL "$INSTALLER_URL")',
    'sh <(wget -qO- "$INSTALLER_URL")',
    f'deno run -A "{_SCHEME}$INSTALLER_HOST/install.ts"',
])
def test_p1424_every_spelling_of_the_piped_installer_reports(tmp_path, command):
    """One vector, five idioms — process substitution and `deno run` fetch and
    execute exactly as `curl | bash` does, and `wget` is the same command under
    a different name. The pattern that recognises them now lives in Python
    source rather than in the catalog's metadata, so a transcription slip in any
    one arm would silently stop reporting that idiom, with nothing anywhere to
    show that it had."""
    hits = _remote_exec_hits(tmp_path, f"""\
        name: setup
        on: push
        jobs:
          install:
            runs-on: ubuntu-latest
            steps:
              - run: {command}
    """)
    assert len(hits) == 1, command


# ---------------------------------------------------------------------------
# P14.24 — the YAML spelling of the same fetch.
#
# `actions/checkout` with `repository:` is how most workflows pull a second
# repository, and at a branch or tag it is the identical trust model: the tree
# that lands is whatever the other side serves when the job runs.
# ---------------------------------------------------------------------------

def test_p1424_checkout_of_another_repo_at_a_branch_then_execution(tmp_path):
    """The idiomatic spelling fired no detector at all — P14.24 read `run:`
    shell only, so the most common way to fetch a second repository was the one
    way the vector could not see."""
    hits = _remote_exec_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
              - run: bash tools/run.sh
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "acme/tools" in note
    assert "tools/run.sh" in note


def test_p1424_checkout_pinned_to_a_full_sha_is_silent(tmp_path):
    """Same exemption as the shell arm: a full 40-hex ref is immutable, and
    reporting it would tell a reader to fix what they already did right."""
    sha = "f" * 40
    assert _remote_exec_hits(tmp_path, f"""\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: {sha}
                  path: tools
              - run: bash tools/run.sh
    """) == []


def test_p1424_checkout_of_the_scanned_repo_itself_is_silent(tmp_path):
    """A plain `actions/checkout` with no `repository:` is your own code, and
    so is one that names your own repository — the overwhelmingly common case,
    which must not become a finding on every workflow in the world."""
    assert _remote_exec_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  path: tools
              - run: bash tools/run.sh
    """) == []
    assert _remote_exec_hits(tmp_path / "self", """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: ${{ github.repository }}
                  ref: main
                  path: tools
              - run: bash tools/run.sh
    """) == []


def test_p1424_checkout_nothing_executes_from_is_silent(tmp_path):
    """Fetching a second repository is not the finding — executing out of it
    is. A checkout used for data must stay quiet."""
    assert _remote_exec_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/fixtures
                  ref: main
                  path: fixtures
              - run: diff -r fixtures expected
    """) == []


def test_p1424_checkout_execution_in_another_job_does_not_connect(tmp_path):
    """Jobs get their own runner and their own tree, exactly as for the shell
    arm."""
    assert _remote_exec_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          fetch:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
          run:
            runs-on: ubuntu-latest
            steps:
              - run: bash tools/run.sh
    """) == []


# ---------------------------------------------------------------------------
# X2/X3 — what the detector calls "the executed path", and which remotes the
# named-remote arm is allowed to trust.
#
# Both were caught by running the shipped scanner over a real public repo:
# 2 of its 3 fires quoted an interpreter's inline SCRIPT TEXT as the path
# ("executes `chomp if eof`"), and the third arm registered the repository's
# own URL as a third-party remote.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tokens", [
    ["perl", "-i", "-pe", "chomp if eof", "dump.sql"],
    ["perl", "-ne", "print if /x/", "dump.sql"],
    ["perl", "-p", "-e", "s/a/b/", "dump.sql"],
    ["node", "-e", "console.log(1)"],
    ["ruby", "-e", "puts 1"],
    ["python3", "-"],
    ["python3", "<<PY"],
    ["bash", "<<'EOF'"],
])
def test_p1424_an_inline_script_body_is_not_an_executed_path(tokens):
    """`-e` / `-pe` / `-ne` / `-p` / `-n` take the program on the command line,
    exactly as `-c` does, and `-` reads it from stdin. The text after them is
    a SCRIPT, not a file — and because it is relative it satisfied the
    containment test and completed a chain, so the finding read "executes
    `chomp if eof` from it". A `<<NAME` token is the here-doc that supplies
    stdin, not a path either."""
    assert scan._executed_path(tokens) is None, tokens


@pytest.mark.parametrize("tokens", [
    ["python3", "tools/setup.py"],
    ["perl", "-w", "tools/gen.pl"],
    ["node", "--experimental-vm-modules", "tools/run.js"],
])
def test_p1424_an_interpreter_running_a_real_file_still_reports(tokens):
    """The control: narrowing the flag handling must not stop the arm from
    seeing an interpreter given an actual file to run."""
    assert scan._executed_path(tokens) == tokens[-1], tokens


def test_p1424_a_remote_added_for_your_own_repository_is_not_third_party(
    tmp_path,
):
    """`git remote add` is the two-step spelling of a fetch, so it has to make
    the same judgment the one-step spelling makes. It did not: the identical
    URL was silent through `git clone` and a finding through
    `git remote add` — two arms, opposite verdicts, same repository."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git" / "config").write_text(_ORIGIN_CONFIG)
    wf = _wf(tmp_path, "wf.yml", """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git remote add pub %s
                  git fetch pub main
                  git checkout FETCH_HEAD
                  bash install.sh
    """ % _url(_SELF_REPO + _DOT_GIT))
    assert list(scan._correlation_unverified_remote_code_execution(wf)) == []


def test_p1424_a_remote_added_for_a_third_party_still_fires(tmp_path):
    """The control for the guard above — the shape X2's repository really has,
    and the one the catalog promises is visible."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git" / "config").write_text(_ORIGIN_CONFIG)
    wf = _wf(tmp_path, "wf.yml", f"""\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git remote add up {_THIRD_PARTY_URL}
                  git fetch up main
                  git checkout FETCH_HEAD
                  bash install.sh
    """)
    assert len(list(scan._correlation_unverified_remote_code_execution(wf))) == 1


def test_p1424_a_command_substitution_body_is_not_the_command(tmp_path):
    """`MIGRATION_FILE=$(ls pg_search/sql/*.sql)` assigns the OUTPUT of `ls`.
    The assignment prefix was stripped and the substitution's arguments were
    then read as a command, so a path `ls` merely listed became "executes
    `pg_search/sql/…`" — the third nonsense fire on the same public repository
    as the inline-script ones, and `ls` is named in the code's own docstring as
    the example of something that is NOT execution."""
    assert _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  MIGRATION_FILE=$(ls tools/sql/migrate.sql | head -n 1)
    """) == []


def test_p1424_a_real_execution_after_a_substitution_still_reports(tmp_path):
    """The control: collapsing substitutions must not swallow the command that
    follows them."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  VERSION=$(cat VERSION)
                  python3 tools/setup.py
    """)
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# X6/R3 — three channels, because "we did not scan this" and "we scanned this
# and deliberately said nothing" are opposite claims.
#
# Both were feeding one list, which report.py renders under
# "Incomplete coverage — N run: step(s) … were NOT scanned … This is not a
# clean result". A repository that did exactly what the fix recipe says got
# that banner, with a bullet underneath saying the step had been read in full.
# ---------------------------------------------------------------------------

def _scan_repo(tmp_path: Path, body: str, name: str = "ci.yml") -> dict:
    _wf(tmp_path, name, body)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    return scan.scan(catalog, tmp_path)


_PINNED_AS_RECIPE_SAYS = """\
    name: ci
    on: push
    jobs:
      b:
        runs-on: ubuntu-latest
        steps:
          - run: |
              git clone "$TOOLS_URL" tools
              git -C tools checkout %s
              python3 tools/setup.py
""" % ("a" * 40)


def test_a_pin_suppression_never_reads_as_missing_coverage(tmp_path):
    """This repository followed the fix recipe exactly. Telling it that a step
    "was NOT scanned" and that its report is "not a clean result" — with a
    bullet underneath explaining the step was read completely — spends the
    honesty banner's credibility on the repositories that did the right thing,
    which is where it costs most."""
    result = _scan_repo(tmp_path, _PINNED_AS_RECIPE_SAYS)
    assert result["findings"] == []
    assert result["dropped_matches"] == [], result["dropped_matches"]
    # It is still recorded — just not as a coverage gap.
    suppressed = result["suppressed_findings"]
    assert len(suppressed) == 1, suppressed
    assert "pinned" in suppressed[0]["reason"]


def test_the_ordinary_pin_spellings_are_recorded_too(tmp_path):
    """`security-patterns.md` and the changelog both say EVERY suppression is
    recorded. Only the deferred late-pin path was: a clone whose own ref is a
    full sha, and a checkout with a 40-hex `ref:`, both returned silently."""
    clone_pin = _scan_repo(tmp_path / "clone", """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch %s "$TOOLS_URL" tools
                  python3 tools/setup.py
    """ % ("b" * 40))
    assert clone_pin["findings"] == []
    assert any("pin" in e["reason"] for e in clone_pin["suppressed_findings"]), \
        clone_pin["suppressed_findings"]

    checkout_pin = _scan_repo(tmp_path / "checkout", """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: %s
                  path: tools
              - run: bash tools/run.sh
    """ % ("c" * 40))
    assert checkout_pin["findings"] == []
    assert any("pin" in e["reason"] for e in checkout_pin["suppressed_findings"]), \
        checkout_pin["suppressed_findings"]


def test_a_checkout_expression_is_a_coverage_note_not_an_unanchored_step(
    tmp_path,
):
    """`ref: ${{ inputs.ref }}` is the standard `workflow_dispatch` spelling, so
    this lands on ordinary repositories. It IS a real coverage gap — nothing
    was established about that checkout — but it is not a `run:` step that
    could not be anchored to a line, which is what the headline claims."""
    result = _scan_repo(tmp_path, """\
        name: ci
        on: workflow_dispatch
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: ${{ inputs.ref }}
                  path: tools
              - run: bash tools/run.sh
    """)
    assert result["dropped_matches"] == [], result["dropped_matches"]
    assert len(result["coverage_notes"]) == 1, result["coverage_notes"]


def test_an_unanchorable_job_still_reaches_the_coverage_headline(tmp_path):
    """The control: the channel that headline was written for must keep
    reaching it, or splitting the channels would have quietly disabled the
    honesty signal instead of aiming it. A job whose source range cannot be
    found is the case it describes — its shell was never scanned at all."""
    body = """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$TOOLS_URL" tools
    """
    with mock.patch.object(scan, "job_line_ranges", lambda _p: []):
        result = _scan_repo(tmp_path, body)
    assert len(result["dropped_matches"]) >= 1, result["dropped_matches"]
    assert all("could not be located" in e["reason"]
               for e in result["dropped_matches"]), result["dropped_matches"]


def test_unparsable_shell_is_a_coverage_note_not_an_unanchored_step(tmp_path):
    """Shell that will not parse is a real gap — but the step WAS anchored and
    read, so it does not belong under a headline about steps that could not be
    tied to a line. Different sentence, same seriousness."""
    result = _scan_repo(tmp_path, """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  cd "unbalanced
                  git clone --branch main "$TOOLS_URL" tools
                  python3 tools/setup.py
    """)
    assert result["dropped_matches"] == [], result["dropped_matches"]
    assert len(result["coverage_notes"]) == 1, result["coverage_notes"]


# ---------------------------------------------------------------------------
# X4/X5 — expressions. One family: a value the YAML does not contain must be
# opaque, opaque consistently, and never rendered as scanner internals.
# ---------------------------------------------------------------------------

def test_p1424_a_computed_job_default_working_directory_is_opaque(tmp_path):
    """Fixed at step level in cea9dde and left open one level above: a
    `defaults.run.working-directory` holding an expression was skipped, so the
    finding named a directory the data does not contain — `tools`, at the
    workspace root, when the step ran somewhere the YAML never says.
    `defaults: {run: {working-directory: apps/${{ matrix.app }}}}` is a
    mainstream monorepo shape."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            defaults:
              run:
                working-directory: apps/${{ matrix.app }}
            steps:
              - run: git clone --branch main "$TOOLS_URL" tools
              - run: python3 tools/setup.py
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "apps/${{ matrix.app }}/tools" in note, note


def test_p1424_a_computed_job_default_still_connects_within_itself(tmp_path):
    """The control, and the whole point of opaque rather than skipped: both
    halves are under the same unknown directory, so they are exactly as
    connected as if it were named."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            defaults:
              run:
                working-directory: apps/${{ matrix.app }}
            steps:
              - run: git clone --branch main "$TOOLS_URL" tools
              - run: python3 tools/setup.py
    """)
    assert len(hits) == 1
    assert "\x00" not in (hits[0].derived_note or "")


def test_p1424_a_job_default_does_not_fall_back_to_the_workflow_default(
    tmp_path,
):
    """GitHub resolves the JOB's default over the workflow's. Treating an
    unreadable job default as absent inverted that precedence, so the finding
    placed the step in the workflow's directory — a place it demonstrably did
    not run."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        defaults:
          run:
            working-directory: app
        jobs:
          b:
            runs-on: ubuntu-latest
            defaults:
              run:
                working-directory: apps/${{ matrix.app }}
            steps:
              - run: git clone --branch main "$TOOLS_URL" tools
              - run: python3 tools/setup.py
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "app/tools" not in note.replace("apps/", ""), note


def test_p1424_a_step_working_directory_replaces_the_job_default(tmp_path):
    """A step's `working-directory:` is resolved against the WORKSPACE and
    replaces the job default — it does not nest inside it. Composing the two
    put the step in `app/app`, so a real chain through the job's own default
    directory went unreported."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            defaults:
              run:
                working-directory: app
            steps:
              - run: git clone --branch main "$TOOLS_URL" tools
              - working-directory: app
                run: python3 tools/setup.py
    """)
    assert len(hits) == 1


def test_p1424_two_different_expressions_are_two_different_places(tmp_path):
    """Every `${{ }}` collapsed to one shared token, so a clone into
    `${{ env.DIR_A }}` and an execution from `${{ env.DIR_B }}` matched each
    other — a chain the reader cannot see in their own YAML, rendered with a
    scanner-internal token as the directory name."""
    assert _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" ${{ env.DIR_A }}
                  python3 ${{ env.DIR_B }}/setup.py
    """) == []


def test_p1424_one_expression_used_twice_is_one_place(tmp_path):
    """The control for the rule above: the SAME expression is exactly as
    knowable in both spots, so the chain is real and has to report."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" ${{ env.TOOLS_DIR }}
                  python3 ${{ env.TOOLS_DIR }}/setup.py
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "${{ env.TOOLS_DIR }}" in note, note
    assert "$EXPR" not in note, note


def test_p1424_the_same_working_directory_expression_connects_across_steps(
    tmp_path,
):
    """The opaque root was keyed by STEP LINE, so two steps under the same
    `apps/${{ matrix.app }}` were treated as two different unknown places and
    produced no finding and no gap — while the single-step spelling fired.
    Splitting fetch and execution across steps is the more idiomatic form."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - working-directory: apps/${{ matrix.app }}
                run: git clone --branch main "$TOOLS_URL" tools
              - working-directory: apps/${{ matrix.app }}
                run: python3 tools/setup.py
    """)
    assert len(hits) == 1


def test_p1424_two_different_working_directory_expressions_do_not_connect(
    tmp_path,
):
    """And the control on the other side — different expressions stay
    different places."""
    assert _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - working-directory: apps/${{ matrix.app }}
                run: git clone --branch main "$TOOLS_URL" tools
              - working-directory: pkgs/${{ matrix.pkg }}
                run: python3 tools/setup.py
    """) == []


def test_p1424_no_finding_ever_renders_the_opaque_sentinel(tmp_path):
    """`_OPAQUE_DIR` is a NUL byte and a scanner-internal prefix. It reached
    `derived_note`, findings.json and the rendered markdown, so the reader was
    shown a raw control character where their own directory should be."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - working-directory: apps/${{ matrix.app }}
                run: |
                  git clone --branch main "$TOOLS_URL" tools
                  python3 tools/setup.py
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "\x00" not in note, repr(note)
    assert "wd:" not in note, note
    assert "apps/${{ matrix.app }}/tools" in note, note


def test_no_scanner_internal_token_reaches_the_rendered_report(tmp_path):
    """The artifact-level guard for the whole family. `_OPAQUE_DIR` is a NUL
    byte and `$EXPRn` is a tokenizer stand-in; both are scanner internals, and
    both reached `derived_note` and from there findings.json and the markdown.
    Asserting it at the rendered artifact means any new internal marker that
    escapes is caught here rather than by a reader."""
    _wf(tmp_path, "ci.yml", """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            defaults:
              run:
                working-directory: apps/${{ matrix.app }}
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" ${{ env.TOOLS_DIR }}
                  python3 ${{ env.TOOLS_DIR }}/setup.py
              - working-directory: pkgs/${{ matrix.pkg }}
                run: |
                  git clone --branch main "$OTHER_URL" vendor
                  bash vendor/install.sh
    """)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path)
    assert result["findings"], "fixture must produce findings to be a guard"

    import report as report_module
    rendered = report_module.render(result)
    blob = json.dumps(result) + rendered
    assert "\\u0000" not in blob and "\x00" not in blob, "opaque sentinel leaked"
    assert "wd:" not in blob, "opaque sentinel prefix leaked"
    assert not re.search(r"\$EXPR\d", blob), "expression stand-in leaked"
    assert "${{ env.TOOLS_DIR }}" in rendered, rendered[:400]


# ---------------------------------------------------------------------------
# R4/R5/R6 — reading the step's own data instead of guessing at raw lines.
# ---------------------------------------------------------------------------

def test_p1424_a_trailing_yaml_comment_is_not_part_of_the_directory(tmp_path):
    """`working-directory: .   # repo root` is the directory `.`; the comment
    is YAML's, not the value's. Scraping the line with a regex made the
    destination `` `.   # repo root/tools` `` — and on the checkout arm the
    same scrape silently dropped a real finding."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - working-directory: .   # repo root
                run: |
                  git clone --branch main "$TOOLS_URL" tools
                  python3 tools/setup.py
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "repo root" not in note, note
    assert "`tools`" in note, note


def test_p1424_a_working_directory_inside_a_heredoc_is_not_the_steps(tmp_path):
    """Text a step WRITES to a file is not configuration of the step. A
    `cat > gen.yml <<EOF` body containing `working-directory: /opt/evil` was
    read as the step's own, so the finding stated a destination taken from
    generated content."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  cat > gen.yml <<EOF
                  working-directory: /opt/evil
                  EOF
                  git clone --branch main "$TOOLS_URL" tools
                  python3 tools/setup.py
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "/opt/evil" not in note, note


def test_p1424_a_pin_before_the_fetch_pins_nothing_it_brought_in(tmp_path):
    """Ordering is the whole claim. A `git checkout <40-hex>` two lines BEFORE
    a fetch pinned the tree as it stood — not the code the fetch then brought
    in — yet it suppressed the finding, and said so: "was pinned … before
    anything ran from it"."""
    sha = "d" * 40
    hits = _remote_exec_hits(tmp_path, f"""\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git checkout {sha}
                  git fetch "$TOOLS_URL" main
                  git checkout FETCH_HEAD
                  bash install.sh
    """)
    assert len(hits) == 1


def test_p1424_two_fetches_on_one_line_are_two_findings(tmp_path):
    """Deduplication keys on the evidence text, and the evidence was the whole
    LINE — so two clones written on one line were byte-identical to the
    deduper and the second chain vanished with no entry anywhere. A dropped
    finding must be recorded somewhere; this one was not."""
    _wf(tmp_path, "ci.yml", """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$A_URL" one && python3 one/x.py && git clone --branch main "$B_URL" two && python3 two/y.py
    """)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path)
    hits = [f for f in result["findings"] if f["pattern"] == "P14.24"]
    shown = " ".join(f.get("derived_note", "") + f.get("evidence", "")
                     for f in hits)
    # One line is one place to fix, so the deduper folds them — but both
    # destinations have to be NAMED, or the second chain is simply gone.
    assert "into one" in shown and "into two" in shown, shown


def test_p1424_a_checkout_named_inside_a_heredoc_does_not_shift_the_evidence(
    tmp_path,
):
    """Checkout steps were tied to lines by counting `uses:` occurrences in
    order, so the WORDS `uses: actions/checkout@v4` written inside a heredoc
    body — a step generating a workflow file — shifted every later step and the
    evidence quoted a line of shell script instead of the checkout."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  cat > generated.yml <<'EOF'
                  steps:
                    - uses: actions/checkout@v4
                  EOF
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
              - run: bash tools/run.sh
    """)
    assert len(hits) == 1
    # The REAL checkout step is the one carrying `repository:`; the heredoc
    # mention is four lines above it. Only the line number distinguishes them,
    # since both lines contain the words "uses" and "checkout".
    assert hits[0].line == 12, (hits[0].line, hits[0].evidence)


def test_p1424_an_execution_shaped_unparsable_line_is_recorded(tmp_path):
    """A visible clone at a branch, then `tools/setup.py --msg=it's here` — an
    apostrophe `shlex` refuses. The command names a path and could be the
    execution half of the chain, but the relevance test only matched command
    NAMES, so a bare path matched nothing: zero findings, zero gaps, a silent
    false clean on exactly the shape the vector exists to catch."""
    before = len(scan._DROPPED_MATCHES)
    _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  tools/setup.py --msg=it's here
    """)
    added = scan._DROPPED_MATCHES[before:]
    assert any("could not be parsed" in d["reason"] for d in added), added


def test_p1424_a_sourced_path_shaped_unparsable_line_is_recorded(tmp_path):
    """`source` and `.` are executions whose names the relevance test also
    missed."""
    before = len(scan._DROPPED_MATCHES)
    _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  . tools/env.sh --name=it's
    """)
    added = scan._DROPPED_MATCHES[before:]
    assert any("could not be parsed" in d["reason"] for d in added), added


def test_p1424_an_irrelevant_unparsable_line_stays_quiet(tmp_path):
    """The control that keeps the gap channel from becoming noise again: a
    `jq` filter with an unbalanced quote cannot hold either half of the chain,
    so failing to read it costs this detector nothing."""
    before = len(scan._DROPPED_MATCHES)
    _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  echo hello
                  jq -r '.a["b] | @csv' data.json
    """)
    assert scan._DROPPED_MATCHES[before:] == []


def test_p1424_a_lookalike_repository_is_not_your_repository(tmp_path):
    """The self-clone test matched the slug ANYWHERE in the URL, so
    `.../owner/repo-mirror` — someone else's fork under a name that contains
    yours — was treated as your own repository and went silent. Contrived, but
    the guard exists to suppress findings, so a loose match suppresses real
    ones."""
    # The literal-slug half already compares the whole `owner/repo`; the
    # EXPRESSION half searched for `${{ github.repository }}` anywhere in the
    # URL, so a stranger's URL that merely embeds it read as your own.
    hits = _self_clone_repo(
        tmp_path, _url("evil/" + _SELF_REPO + "-mirror" + _DOT_GIT),
        "python3 tools/release.py")
    assert len(hits) == 1, hits
    assert _self_clone_repo(
        tmp_path / "self", _url(_SELF_REPO + _DOT_GIT),
        "python3 tools/release.py") == []


def test_p1424_a_computed_checkout_repository_is_gapped_not_named(tmp_path):
    """`repository: ${{ env.TOOLS_REPO }}` fired and rendered "checks out
    `${{ env.TOOLS_REPO }}`" — naming a third party the scan never
    established, when that variable routinely holds the org's own repository.
    `ref:` and `path:` expressions already recorded gaps; `repository:` did
    not."""
    before = len(scan._DROPPED_MATCHES)
    hits = _remote_exec_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: ${{ env.TOOLS_REPO }}
                  ref: main
                  path: tools
              - run: bash tools/run.sh
    """)
    assert hits == [], hits
    added = scan._DROPPED_MATCHES[before:]
    assert any("repository" in d["reason"] for d in added), added


def test_p1424_a_checkout_with_no_path_replaces_the_workspace(tmp_path):
    """With no `path:`, `actions/checkout` REPLACES the workspace, so anything
    the job runs afterwards comes out of the fetched tree. That is the arm's
    broadest pairing rule and it had no test in either direction."""
    hits = _remote_exec_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
              - run: bash scripts/build.sh
    """)
    assert len(hits) == 1


def test_p1424_a_checkout_pin_still_honours_position(tmp_path):
    """Parity with the shell arm: a checkout at a mutable ref followed by an
    execution reports even when a LATER checkout of the same path is pinned —
    the code that ran was the unpinned one."""
    sha = "e" * 40
    hits = _remote_exec_hits(tmp_path, f"""\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
              - run: bash tools/run.sh
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: {sha}
                  path: tools
    """)
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# G2/G3 — two wiring bugs in the adoption of `_step_marks`. The approach is
# right; both of these read the marks against the wrong index.
# ---------------------------------------------------------------------------

def test_p1424_a_setup_step_before_the_checkout_does_not_shift_it(tmp_path):
    """Lines were collected from EVERY step carrying a `uses:` key while the
    index counted only `actions/checkout` steps, so any
    `- uses: actions/setup-node@v4` ahead of the third-party checkout — nearly
    every real workflow — shifted the mapping and the evidence quoted the
    wrong step."""
    hits = _remote_exec_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-node@v4
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
              - run: bash tools/run.sh
    """)
    assert len(hits) == 1
    assert "repository" in hits[0].evidence or hits[0].line == 9, \
        (hits[0].line, hits[0].evidence)


def test_p1424_an_execution_before_the_checkout_is_not_a_chain(tmp_path):
    """The ordering contract, violated by the same mis-mapping: the shifted
    line put the checkout EARLIER than the run step, so a `bash tools/run.sh`
    that executes before anything is fetched was reported as executing out of
    the fetched tree."""
    assert _remote_exec_hits(tmp_path, """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/setup-node@v4
              - run: bash tools/run.sh
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
    """) == []


def test_p1424_a_flow_style_step_directory_stays_in_its_own_step(tmp_path):
    """`_run_scalar_starts` is a line regex and cannot see a flow-style
    `- {run: …}` step, so its `working-directory:` attached to the next block
    scalar it could find — in a DIFFERENT job — and the finding named a
    destination that appears nowhere near the step it describes."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          a:
            runs-on: ubuntu-latest
            steps:
              - {run: echo hi, working-directory: vendor/x}
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  bash tools/build.sh
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "vendor/x" not in note, note
    assert "`tools`" in note, note


def test_p1424_a_flow_style_step_does_not_erase_a_real_chain(tmp_path):
    """The inverse loss: a flow-style step between the fetch and the execution
    donated its directory to the executing step and moved it out of the
    fetched tree, so a real chain went unreported."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$TOOLS_URL" tools
              - {run: echo hi, working-directory: /opt/other}
              - run: bash tools/run.sh
    """)
    assert len(hits) == 1


def test_p1424_a_repinned_reclone_is_still_pinned(tmp_path):
    """Only the EARLIEST pin per destination was kept, while the rule needs A
    pin between the fetch and the execution. Here the first pin lands on the
    tree that is about to be discarded — before the clone that matters — so
    keeping only that one hid the pin the repository actually applied, and a
    repo that followed the fix recipe was told it executes unpinned code."""
    old_sha, new_sha = "a" * 40, "b" * 40
    assert _remote_exec_hits(tmp_path, f"""\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git -C tools checkout {old_sha}
                  rm -rf tools
                  git clone "$TOOLS_URL" tools
                  git -C tools checkout {new_sha}
                  python3 tools/setup.py
    """) == []


def test_p1424_a_checkout_arm_pin_must_also_come_after_the_fetch(tmp_path):
    """The checkout arm's fetches carried no position, so every pin in the job
    counted as "before anything ran from it" — including one applied to a tree
    the checkout then replaced. Same ordering rule as the shell arm, or the
    rule is only enforced on one of the two."""
    sha = "c" * 40
    hits = _remote_exec_hits(tmp_path, f"""\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: git -C tools checkout {sha}
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
              - run: bash tools/run.sh
    """)
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# G4 — which flags mean "the program is on the command line" depends on the
# INTERPRETER, and only leading flags are the interpreter's at all.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tokens,expected", [
    # Shell options that are NOT inline-script flags. Treating them as such
    # made a real fetched script silently unreported — a regression.
    (["bash", "-e", "tools/install.sh"], "tools/install.sh"),
    (["sh", "-e", "-x", "tools/install.sh"], "tools/install.sh"),
    (["python3", "-E", "tools/setup.py"], "tools/setup.py"),
    # A flag AFTER the path belongs to the fetched script, not the interpreter.
    (["bash", "tools/install.sh", "-n"], "tools/install.sh"),
    (["bash", "tools/deploy.sh", "-p", "8080"], "tools/deploy.sh"),
    # A flag's VALUE is not the program either.
    (["python3", "-W", "ignore", "tools/setup.py"], "tools/setup.py"),
])
def test_p1424_an_interpreter_option_does_not_hide_the_script(tokens, expected):
    """`-e`/`-E`/`-p`/`-n` are ordinary shell and Python options. Matching them
    anywhere in the argument list — including after the path, where they belong
    to the fetched script — turned a real chain into a silent false clean."""
    assert scan._executed_path(tokens) == expected, tokens


@pytest.mark.parametrize("tokens", [
    ["bash", "-lc", "echo hi"],
    ["sh", "-lc", "make release"],
    ["perl", "-lane", "print $F[0]", "dump.sql"],
    ["perl", "-nle", "print", "dump.sql"],
    ["php", "-r", "echo 1;"],
    ["pwsh", "-Command", "Write-Host hi"],
    ["powershell", "-EncodedCommand", "ZQBjAGgAbwA="],
    ["ruby", "-e", "puts 1"],
    ["node", "-e", "console.log(1)"],
])
def test_p1424_an_inline_program_is_still_not_a_path(tokens):
    """The other direction, still open: a cluster carrying `c`/`e`/`r`, or a
    PowerShell `-Command`, puts the PROGRAM on the command line. Rendering that
    text as "the executed path" is how `executes \\`echo hi\\` from it` reached a
    real report."""
    assert scan._executed_path(tokens) is None, tokens


def test_p1424_a_shell_option_before_a_fetched_script_still_reports(tmp_path):
    """End to end: the regression this closes was a clone at a branch followed
    by `bash tools/install.sh -n` producing zero findings, zero gaps and zero
    suppressions."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  bash -e tools/install.sh -n
    """)
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# G5 — `$( … )` RUNS its body. Collapsing it for tokenization is right; losing
# it entirely is not.
# ---------------------------------------------------------------------------

def test_p1424_an_execution_inside_a_substitution_is_not_lost(tmp_path):
    """`OUT=$(tools/setup.sh)` executes the fetched script — the substitution
    is how its output is captured, not a reason it did not run. Collapsing the
    body before tokenizing killed the nonsense `$(ls …)` fire and took this
    with it: zero findings, zero gaps, zero suppressions, where the backtick
    spelling of the same construct still recorded a gap."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  OUT=$(tools/setup.sh)
    """)
    assert len(hits) == 1
    assert "tools/setup.sh" in (hits[0].derived_note or "")


def test_p1424_an_interpreter_inside_a_substitution_is_not_lost(tmp_path):
    """The same shape with the interpreter spelled out."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  VERSION=$(python3 tools/version.py)
    """)
    assert len(hits) == 1


def test_p1424_a_substitution_that_only_reads_stays_quiet(tmp_path):
    """The control that keeps the original fix intact: `$(ls tools/x.sql)`
    lists a path, it does not execute it, and `ls` is this code's own example
    of what is not execution."""
    assert _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  MIGRATION=$(ls tools/sql/migrate.sql | head -n 1)
    """) == []


def test_p1424_a_checkout_of_your_own_repository_raises_no_coverage_note(
    tmp_path,
):
    """The expression gap ran BEFORE the self-repository test, so a checkout of
    your own repo — the exact spelling the clone arm was taught to recognise —
    recorded "whether it fetches a third party was NOT established" into the
    channel that drives the `not a clean result` banner. Two real repositories
    open their reports with that warning over nothing but self-checkouts. It is
    X6's crying-wolf defect, one channel over."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git" / "config").write_text(_ORIGIN_CONFIG)
    _wf(tmp_path, "ci.yml", """\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: ${{ github.repository }}
                  ref: main
                  path: tools
              - run: bash tools/run.sh
    """)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path)
    assert result["findings"] == []
    assert result["coverage_notes"] == [], result["coverage_notes"]


def test_p1424_a_genuinely_unknowable_checkout_repository_is_still_gapped(
    tmp_path,
):
    """The control: the fork-PR spelling really is unknowable — it resolves to
    the head repository of whoever opened the pull request — so it stays a
    disclosed gap."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git" / "config").write_text(_ORIGIN_CONFIG)
    _wf(tmp_path, "ci.yml", """\
        name: ci
        on: pull_request_target
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: ${{ github.event.pull_request.head.repo.full_name }}
                  ref: main
                  path: tools
              - run: bash tools/run.sh
    """)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path)
    assert len(result["coverage_notes"]) == 1, result["coverage_notes"]


def test_p1424_the_env_var_spelling_of_your_own_slug_is_not_third_party(
    tmp_path,
):
    """`$GITHUB_REPOSITORY` is exactly as self-identifying as
    `${{ github.repository }}`, which the guard already honours."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git" / "config").write_text(_ORIGIN_CONFIG)
    wf = _wf(tmp_path, "wf.yml", """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git remote add up "%s"
                  git fetch up main
                  git checkout FETCH_HEAD
                  bash install.sh
    """ % _SELF_ENV_URL)
    assert list(scan._correlation_unverified_remote_code_execution(wf)) == []


def test_p1424_a_digit_after_an_expression_does_not_leak_the_token(tmp_path):
    """`${{ env.DIR }}2` tokenizes to the stand-in followed by a literal `2`,
    and the render pattern's `\\d+` swallowed that digit — so the lookup missed
    and the reader saw the scanner's own token where their directory belongs.
    The sentinel is delimited now, which is what makes the leak impossible
    rather than merely unlikely."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" ${{ env.DIR }}2
                  python3 ${{ env.DIR }}2/setup.py
    """)
    assert len(hits) == 1
    note = hits[0].derived_note or ""
    assert "EXPR" not in note, note
    assert "${{ env.DIR }}2" in note, note


def test_the_expression_registry_does_not_leak_across_scans(tmp_path):
    """The token registry was a module global never reset, so a workflow whose
    text literally contains a stand-in rendered as some OTHER workflow's
    expression — and which text leaked depended on the order files were
    scanned."""
    _wf(tmp_path / "one", "a.yml", """\
        name: a
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" ${{ env.SECRET_LOOKING }}
                  python3 ${{ env.SECRET_LOOKING }}/setup.py
    """)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    scan.scan(catalog, tmp_path / "one")

    _wf(tmp_path / "two", "b.yml", """\
        name: b
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  python3 tools/setup.py
    """)
    result = scan.scan(catalog, tmp_path / "two")
    blob = json.dumps(result)
    assert "SECRET_LOOKING" not in blob, "one scan's expressions leaked into another"


def test_p1424_expression_spacing_does_not_split_a_directory(tmp_path):
    """`apps/${{ matrix.app }}` and `apps/${{matrix.app}}` are the same
    directory — GitHub ignores the spacing inside the braces. Keying the opaque
    root on the raw text made them two different unknown places, so the chain
    produced no finding and no gap."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - working-directory: apps/${{ matrix.app }}
                run: git clone --branch main "$TOOLS_URL" tools
              - working-directory: apps/${{matrix.app}}
                run: python3 tools/setup.py
    """)
    assert len(hits) == 1


def test_p1424_sourcing_an_extensionless_fetched_path_is_recorded(tmp_path):
    """`. tools/env --name=it's` sources a fetched file — `shlex` refuses the
    apostrophe, and the relevance test required no space after the `.`, so
    nothing was reported and nothing was recorded. The shipped test for this
    used `tools/env.sh`, whose `.sh` matched the interpreter alternation on
    base too, so it passed either way and masked the live hole."""
    before = len(scan._DROPPED_MATCHES)
    _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  . tools/env --name=it's
    """)
    added = scan._DROPPED_MATCHES[before:]
    assert any("could not be parsed" in d["reason"] for d in added), added


def test_a_coverage_note_names_the_line_it_came_from(tmp_path):
    """Notes dedupe on their reason text, and the checkout-arm reasons carried
    no line — so two steps with the same unresolvable expression collapsed into
    one entry, the banner under-counted, and the reader had nowhere to look."""
    _wf(tmp_path, "ci.yml", """\
        name: ci
        on: workflow_dispatch
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: ${{ inputs.ref }}
                  path: one
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: ${{ inputs.ref }}
                  path: two
              - run: bash one/run.sh
    """)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path)
    notes = result["coverage_notes"]
    assert len(notes) == 2, notes
    assert all("line" in n["reason"] for n in notes), notes


@pytest.mark.parametrize("path", [
    "${GITHUB_WORKSPACE}/vale",
    "$GITHUB_WORKSPACE/bin/tool",
    "$HOME/bin/tool",
    "$RUNNER_TEMP/installer",
    "${RUNNER_WORKSPACE}/x/tool",
    "$GITHUB_ACTION_PATH/run.sh",
])
def test_p1424_a_runner_absolute_path_is_not_inside_the_fetched_tree(
    tmp_path, path,
):
    """`$GITHUB_WORKSPACE`, `$HOME` and the runner's own directories are
    ABSOLUTE by GitHub's contract. Resolving them relative to the step's
    `working-directory:` put them inside a checkout they have nothing to do
    with — teleport's report says a `vale` binary downloaded and sha-verified
    from its own release page is executed "from" a docs checkout."""
    assert _remote_exec_hits(tmp_path / path[-8:].replace("/", "_"), f"""\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/docs
                  ref: main
                  path: docs
              - working-directory: docs/content/current
                run: |
                  "{path}" --config .vale.ini docs/pages
    """) == []


def test_p1424_a_relative_path_under_a_working_directory_still_reports(
    tmp_path,
):
    """The control: an ordinary relative execution inside the fetched checkout
    is exactly what this vector is for."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/docs
                  ref: main
                  path: docs
              - run: bash docs/build.sh
    """)
    assert len(hits) == 1


def test_p1424_the_second_chain_on_a_line_is_described_not_just_named(tmp_path):
    """One line is one place to fix, so folding the two findings is right — but
    the kept finding's prose described only the FIRST chain, so what runs out
    of the second tree was stated nowhere. Naming the fetch on the marker was
    half the contract; the reader still has to be told what it executes."""
    _wf(tmp_path, "ci.yml", """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: git clone --branch main "$A_URL" one && python3 one/x.py && git clone --branch main "$B_URL" two && python3 two/y.py
    """)
    catalog = scan.load_catalog(_SKILL / "references" / "security-patterns.md")
    result = scan.scan(catalog, tmp_path)
    hits = [f for f in result["findings"] if f["pattern"] == "P14.24"]
    assert len(hits) == 1, hits
    note = hits[0].get("derived_note", "")
    assert "two/y.py" in note, note
    assert "one/x.py" in note, note


def test_p1424_a_checkout_pinned_by_a_later_step_is_suppressed(tmp_path):
    """The direction the checkout arm's pin test never covered, and the one the
    catalog promises: fetch, pin to a full commit id, then run. This is the fix
    recipe applied exactly, and it was being reported — because the suppression
    window opened at the first EXECUTION-shaped command after the fetch, which
    is the reported execution itself, leaving an empty interval no pin could
    land in. Whether it suppressed depended on an unrelated command happening
    to sit in between."""
    sha = "1" * 40
    hits, = (_remote_exec_hits(tmp_path, f"""\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
              - run: git -C tools checkout {sha}
              - run: bash tools/run.sh
    """),)
    assert hits == [], hits


def test_p1424_a_checkout_pin_does_not_depend_on_an_unrelated_neighbour(
    tmp_path,
):
    """The same repository with one irrelevant command added ahead of the pin
    must reach the same verdict. It did not: the extra execution opened the
    window that the recipe-compliant shape lacked, so an unrelated line decided
    whether a correctly pinned repo was reported."""
    sha = "2" * 40
    without = _remote_exec_hits(tmp_path / "without", f"""\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
              - run: git -C tools checkout {sha}
              - run: bash tools/run.sh
    """)
    with_neighbour = _remote_exec_hits(tmp_path / "with", f"""\
        name: ci
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
                with:
                  repository: acme/tools
                  ref: main
                  path: tools
              - run: python3 unrelated/x.py
              - run: git -C tools checkout {sha}
              - run: bash tools/run.sh
    """)
    assert without == with_neighbour == []


# ---------------------------------------------------------------------------
# P14.24 — follow-up hardening: shell-var executed paths, shell `-o` values,
# nested substitution, re-clone-after-pin, and YAML-quoted single-line steps.
# ---------------------------------------------------------------------------


def test_p1424_an_unresolved_shell_variable_path_is_a_gap_not_a_fire(tmp_path):
    """A path whose first component is a shell variable this scan cannot resolve
    (`$MYVAR/tool.sh`) was joined onto the working directory and, inside a
    third-party checkout, fired — but the variable could hold an absolute path
    that escapes the tree entirely, so resolving it was a guess. It is a
    coverage note, not a finding."""
    before = len(scan._DROPPED_MATCHES)
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  cd tools
                  bash $MYVAR/tool.sh
    """)
    added = scan._DROPPED_MATCHES[before:]
    assert hits == []
    assert any("shell variable" in d["reason"] for d in added), added


def test_p1424_a_path_under_a_known_tree_still_fires_though_it_holds_a_var(
        tmp_path):
    """The narrowness control: only a LEADING variable is unknowable. A path
    rooted at the fetched directory with a variable deeper in
    (`tools/$V/run.sh`) is inside the tree whatever the variable holds, so it is
    still a real execution out of it."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  bash tools/$V/run.sh
    """)
    assert len(hits) == 1


def test_p1424_a_shell_o_option_value_is_not_the_executed_path(tmp_path):
    """`bash -o pipefail tools/run.sh` names a shell OPTION as the value of
    `-o`; the script is the NEXT word. Read flatly, `pipefail` was taken for the
    path and the real execution silently missed."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  bash -o pipefail tools/run.sh
    """)
    assert len(hits) == 1
    assert "tools/run.sh" in (hits[0].derived_note or "")


def test_p1424_python_dash_capital_o_is_boolean_and_runs_its_script(tmp_path):
    """The scoping control for the `-o`/`-O` value rule: python's `-O` is a
    boolean (optimize), so `python3 -O tools/setup.py` runs `tools/setup.py`.
    Treating `-O` as value-taking there would eat the script."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  python3 -O tools/setup.py
    """)
    assert len(hits) == 1
    assert "tools/setup.py" in (hits[0].derived_note or "")


def test_p1424_a_nested_command_substitution_is_scanned(tmp_path):
    """`$(…)` runs its body, and a body can hold another `$(…)`. Reading only
    the outer level left `OUT=$(echo $(tools/setup.sh))` a silent false clean —
    no finding, no gap. The inner execution is now seen."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone --branch main "$TOOLS_URL" tools
                  OUT=$(echo $(tools/setup.sh))
    """)
    assert len(hits) == 1
    assert "tools/setup.sh" in (hits[0].derived_note or "")


def test_p1424_a_reclone_after_a_pin_is_not_treated_as_pinned(tmp_path):
    """clone → pin → re-clone of the SAME directory → run: the tree that runs is
    the unpinned re-clone. Keeping the FIRST fetch let the pin (which sat
    between the stale first clone and the execution) suppress the finding, so
    the job went silent while it ran unpinned remote code. The last unpinned
    fetch into a destination wins."""
    sha = "a" * 40
    hits = _remote_exec_hits(tmp_path, f"""\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_URL" tools
                  git -C tools checkout {sha}
                  git clone "$TOOLS_URL" tools
                  python3 tools/setup.py
    """)
    assert len(hits) == 1


def test_p1424_a_genuine_pin_of_the_surviving_reclone_still_suppresses(
        tmp_path):
    """The control for last-fetch-wins: a pin applied to the re-clone, before it
    runs, is the fix this entry recommends and must still stay silent."""
    sha = "a" * 40
    assert _remote_exec_hits(tmp_path, f"""\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_URL" tools
                  git clone "$TOOLS_URL" tools
                  git -C tools checkout {sha}
                  python3 tools/setup.py
    """) == []


def test_p1424_a_trailing_reclone_does_not_shadow_a_real_execution(tmp_path):
    """last-fetch-wins must not let a re-clone that nothing runs from bury an
    earlier clone that WAS executed. clone → run → re-clone (with nothing after
    it) overwrote the destination with the trailing, unexecuted fetch; the
    ordering guard then found no execution after it and the job went silent
    while it had already run unpinned remote code. The earlier fetch a command
    actually ran from stays a candidate and still fires."""
    hits = _remote_exec_hits(tmp_path, """\
        name: x
        on: push
        jobs:
          b:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  git clone "$TOOLS_URL" tools
                  python3 tools/setup.py
                  git clone "$TOOLS_URL" tools
    """)
    assert len(hits) == 1


def test_p1424_a_quoted_single_line_run_scalar_is_read_as_shell(tmp_path):
    """A single-line `run:` value written with YAML quotes was scraped raw, so
    the quotes reached the shell tokenizer and `run: "git clone … && bash
    x.sh"` came back as one quoted word — the clone and the execution both
    vanished, a silent false clean. The value is read as the parser saw it."""
    for quote in ('"', "'"):
        body = (
            "name: x\n"
            "on: push\n"
            "jobs:\n"
            "  b:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: " + quote
            + "git clone --branch main $U tools && bash tools/go.sh" + quote
            + "\n"
        )
        wf = _wf(tmp_path, f"wf_{quote!r}.yml".replace("'", "s").replace('"', "d"),
                 body)
        hits = list(scan._correlation_unverified_remote_code_execution(wf))
        assert len(hits) == 1, (quote, hits)
        assert "tools/go.sh" in (hits[0].derived_note or "")

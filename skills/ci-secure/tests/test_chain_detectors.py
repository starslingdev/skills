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

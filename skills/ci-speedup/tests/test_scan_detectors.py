"""Oracle tests for the ci-speedup static scanner.

Each test writes a minimal workflow (or repo tree) to a tmp dir and runs
``scripts/scan.py`` as a subprocess — mirroring real invocation — then asserts
which catalog patterns fired. Every detector gets self-contained positive AND
negative coverage so a regression that breaks a branch is caught by ``pytest``,
not the next worked-example run.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_scan.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCAN_SCRIPT = _SKILL_DIR / "scripts" / "scan.py"


def _have_yaml() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import yaml"], capture_output=True, text=True,
    ).returncode == 0


def _scan(root: Path) -> dict:
    if not _have_yaml():
        pytest.skip("PyYAML not installed in the test runner")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _write_workflow(root: Path, name: str, content: str) -> None:
    wf_dir = root / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / name).write_text(content, encoding="utf-8")


def _patterns(data: dict) -> set[str]:
    return {f["pattern"] for f in data["findings"]}


def _scan_one(tmp_path: Path, content: str, name: str = "ci.yml") -> set[str]:
    _write_workflow(tmp_path, name, content)
    return _patterns(_scan(tmp_path))


# =============================================================================
# Phase 2b — job-correlated detectors
# =============================================================================

def test_opt1_unnecessary_playwright_install(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright install --with-deps
      - run: vitest run
"""
    assert "OPT1" in _scan_one(tmp_path, pos)


def test_opt1_negative_when_playwright_test_runs(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright install --with-deps
      - run: npx playwright test
"""
    assert "OPT1" not in _scan_one(tmp_path, neg)


def test_opt2_uncached_playwright(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npx playwright install
      - run: npx playwright test
"""
    fired = _scan_one(tmp_path, pos)
    assert "OPT2" in fired


def test_opt2_negative_with_preceding_cache(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with:
          path: ~/.cache/ms-playwright
          key: pw-${{ hashFiles('pnpm-lock.yaml') }}
      - run: npx playwright install
      - run: npx playwright test
"""
    assert "OPT2" not in _scan_one(tmp_path, neg)


def test_opt1_opt2_same_step_reconciled_not_double_counted(tmp_path: Path):
    """A job that runs `playwright install`, never USES Playwright, and has no
    preceding cache trips BOTH OPT1 (remove the unused install) AND OPT2 (cache
    the same install) on the IDENTICAL step. Their remedies are mutually
    exclusive — OPT1's premise (unused) negates OPT2's remedy (cache for reuse) —
    and each independently credits the same install seconds / runner-minutes, so
    rendering both double-counts the saving. The scanner must reconcile to the
    decisive removal (OPT1) and drop OPT2 on that one step, crediting it once."""
    pos = """name: CI
on: [pull_request]
jobs:
  server:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npx playwright install --with-deps
      - run: vitest run
"""
    _write_workflow(tmp_path, "engine_pr_test.yml", pos)
    findings = _scan(tmp_path)["findings"]
    opt1 = [f for f in findings if f["pattern"] == "OPT1"]
    opt2 = [f for f in findings if f["pattern"] == "OPT2"]
    assert len(opt1) == 1, "OPT1 (the decisive removal) must survive"
    # The contradictory OPT2 win on the SAME physical step must be reconciled away
    # so the same install cost is not credited twice.
    same_step = [
        f for f in opt2
        if (f["workflow_file"], f["line"], tuple(f["affected_jobs"]))
        == (opt1[0]["workflow_file"], opt1[0]["line"], tuple(opt1[0]["affected_jobs"]))
    ]
    assert same_step == [], (
        "OPT2 must not double-count the same `playwright install` step OPT1 already "
        f"owns; got OPT2 findings on the identical step: {same_step}")
    # The reconciliation is recorded on the keeper, not silently dropped.
    assert "OPT2" in (opt1[0].get("reconciled_with") or [])


def test_opt2_distinct_step_survives_when_opt1_fires_elsewhere(tmp_path: Path):
    """Reconciliation is keyed on the exact step: an OPT2 on a job that genuinely
    USES Playwright (so OPT1 never fires there) must NOT be dropped just because
    OPT1 fired on a different job/step in the same repo."""
    pos = """name: CI
on: [pull_request]
jobs:
  unused:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright install --with-deps
      - run: vitest run
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npx playwright install
      - run: npx playwright test
"""
    _write_workflow(tmp_path, "ci.yml", pos)
    findings = _scan(tmp_path)["findings"]
    opt2_e2e = [f for f in findings
                if f["pattern"] == "OPT2" and f["affected_jobs"] == ["e2e"]]
    assert opt2_e2e, "OPT2 on the genuinely-using `e2e` job must survive reconciliation"
    assert any(f["pattern"] == "OPT1" and f["affected_jobs"] == ["unused"]
               for f in findings), "OPT1 should still fire on the unused-install job"


def test_opt5_setup_node_no_pnpm_cache(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: pnpm install
"""
    assert "OPT5" in _scan_one(tmp_path, pos)


def test_opt5_wrong_order(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: pnpm
      - uses: pnpm/action-setup@v4
      - run: pnpm install
"""
    assert "OPT5" in _scan_one(tmp_path, pos)


def test_opt5_negative_well_configured(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: pnpm
      - run: pnpm install
"""
    assert "OPT5" not in _scan_one(tmp_path, neg)


def test_opt9_eslint_no_cache(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: npx eslint .
"""
    assert "OPT9" in _scan_one(tmp_path, pos)


def test_opt9_negative_with_cache_flag(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: npx eslint . --cache --cache-location ./node_modules/.cache/eslint
"""
    assert "OPT9" not in _scan_one(tmp_path, neg)


def test_opt14_repeated_setup_no_artifact(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm install
      - run: pnpm lint
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm install
      - run: pnpm test
"""
    assert "OPT14" in _scan_one(tmp_path, pos)


def test_opt14_negative_with_real_handoff(tmp_path: Path):
    """A real handoff = upload in the producer AND download in the consumer."""
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm install
      - run: pnpm build
      - uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm install
      - uses: actions/download-artifact@v4
        with:
          name: dist
      - run: pnpm test
"""
    assert "OPT14" not in _scan_one(tmp_path, neg)


def test_opt14_upload_only_does_not_suppress(tmp_path: Path):
    """An upload-only step (e.g. a coverage report) is NOT a setup handoff —
    the two setup jobs still duplicate install, so OPT14 must still fire."""
    pos = """name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm install
      - run: pnpm lint
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm install
      - run: pnpm test
      - uses: actions/upload-artifact@v4
        with:
          name: coverage
          path: coverage
"""
    assert "OPT14" in _scan_one(tmp_path, pos)


def test_opt16_within_job_duplicate(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm run build:packages
      - run: pnpm run build:packages
"""
    assert "OPT16" in _scan_one(tmp_path, pos)


def test_opt16_negative_distinct_commands(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm run build:packages
      - run: pnpm run test:packages
"""
    assert "OPT16" not in _scan_one(tmp_path, neg)


def test_opt18_not_auto_emitted(tmp_path: Path):
    """OPT18 is NOT auto-emitted as a finding: which services a job needs can't
    be determined from the workflow YAML (it needs docker-compose.yml + the test
    code), so a naive auto-fix could drop a service the tests require. It is
    surfaced as a manual-review checklist item instead — never a scan finding."""
    pos = """name: CI
on: push
jobs:
  it:
    runs-on: ubuntu-latest
    steps:
      - run: docker compose up -d --wait --wait-timeout 60
      - run: pnpm test:integration
"""
    assert "OPT18" not in _scan_one(tmp_path, pos)


def test_opt21_unnecessary_needs(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo build
  test:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
"""
    assert "OPT21" in _scan_one(tmp_path, pos)


def test_opt21_negative_uses_outputs(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      ver: ${{ steps.v.outputs.ver }}
    steps:
      - id: v
        run: echo "ver=1" >> $GITHUB_OUTPUT
  test:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: echo ${{ needs.build.outputs.ver }}
"""
    assert "OPT21" not in _scan_one(tmp_path, neg)


def test_opt21_negative_bracket_indexed_needs(tmp_path: Path):
    # `needs['issue-link-nudge'].outputs` (bracket form, required for hyphenated
    # job keys) IS a needs reference — OPT21 must not flag it (mastra complexity
    # regression).
    neg = """name: triage
on: pull_request
jobs:
  issue-link-nudge:
    runs-on: ubuntu-latest
    outputs:
      needs_issue: ${{ steps.x.outputs.needs_issue }}
    steps:
      - id: x
        run: echo "needs_issue=false" >> $GITHUB_OUTPUT
  complexity:
    needs: issue-link-nudge
    if: ${{ needs['issue-link-nudge'].outputs.needs_issue == 'false' }}
    runs-on: ubuntu-latest
    steps:
      - run: echo score
"""
    assert "OPT21" not in _scan_one(tmp_path, neg)


def test_opt27_duplicate_setup_node(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: 18
      - uses: actions/setup-node@v4
        with:
          node-version: 20
"""
    assert "OPT27" in _scan_one(tmp_path, pos)


def test_opt27_negative_single(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
"""
    assert "OPT27" not in _scan_one(tmp_path, neg)


def test_opt29_merge_group_step_skip(tmp_path: Path):
    pos = """name: CI
on:
  pull_request:
  merge_group:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - if: github.event_name != 'merge_group'
        run: pnpm test
"""
    assert "OPT29" in _scan_one(tmp_path, pos)


def test_opt29_negative_job_level_if(tmp_path: Path):
    neg = """name: CI
on:
  merge_group:
jobs:
  test:
    if: github.event_name != 'merge_group'
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
"""
    assert "OPT29" not in _scan_one(tmp_path, neg)


def test_opt31_conditional_setup(tmp_path: Path):
    pos = """name: CI
on: pull_request
jobs:
  web:
    runs-on: ubuntu-latest
    steps:
      - run: npx playwright install --with-deps chromium
      - if: env.CLERK_SECRET_KEY != ''
        run: npx playwright test smoke
"""
    assert "OPT31" in _scan_one(tmp_path, pos)


def test_opt31_negative_install_also_gated(tmp_path: Path):
    neg = """name: CI
on: pull_request
jobs:
  web:
    runs-on: ubuntu-latest
    steps:
      - if: env.CLERK_SECRET_KEY != ''
        run: npx playwright install --with-deps chromium
      - if: env.CLERK_SECRET_KEY != ''
        run: npx playwright test smoke
"""
    assert "OPT31" not in _scan_one(tmp_path, neg)


def test_opt33_no_draft_gate_on_matrix(tmp_path: Path):
    pos = """name: CI
on: pull_request
jobs:
  adapters:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        adapter: [prisma, drizzle, kysely]
    steps:
      - run: pnpm test
"""
    assert "OPT33" in _scan_one(tmp_path, pos)


def test_opt33_line_anchors_on_the_jobs_own_header(tmp_path: Path):
    """OPT33's `line` must point at the flagged job's OWN `job_name:` header, not
    the file-global first substring match. A `test` job preceded by another job
    whose `runs-on: ubuntu-latest` contains the substring `test` ('la-test')
    must not be mis-anchored to that earlier line (s2-streamstore/cachey)."""
    content = """name: CI
on: pull_request
jobs:
  conventional-commit:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        go: ['1.21', '1.22']
    steps:
      - run: go test ./...
"""
    _write_workflow(tmp_path, "ci.yml", content)
    data = _scan(tmp_path)
    f = [x for x in data["findings"] if x["pattern"] == "OPT33"]
    assert f, "OPT33 should fire on the matrix `test` job"
    lines = content.splitlines()
    # The flagged job is `test` — its header `  test:` is on this 1-based line.
    test_header = next(i for i, ln in enumerate(lines, 1)
                       if ln.strip() == "test:")
    assert f[0]["line"] == test_header, (
        f"OPT33 line {f[0]['line']} should anchor on the `test:` header "
        f"(line {test_header}), not the earlier `ubuntu-latest` line")


def _line_containing(content: str, needle: str, after: int = 0) -> int:
    """1-based line number of the first line containing `needle` after line
    `after` (exclusive). Helper for the wrong-job line-anchor regressions."""
    return next(i for i, ln in enumerate(content.splitlines(), 1)
                if i > after and needle in ln)


def _header_line(content: str, job: str) -> int:
    """1-based line of the `<job>:` block header — an EXACT stripped match, so
    `scan` doesn't collide with `gated-scan` (substring traps)."""
    return next(i for i, ln in enumerate(content.splitlines(), 1)
                if ln.strip() == f"{job}:")


def test_opt21_needs_line_anchors_on_the_flagged_job(tmp_path: Path):
    """OPT21 (orphan `needs:`) must anchor on the FLAGGED job's own `needs:`, not
    the file-global first `needs:` line. A multi-job workflow has many; pointing
    at the wrong job is the OPT33/OPT29 wrong-job hazard in another detector."""
    content = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      x: ${{ steps.s.outputs.x }}
    steps:
      - id: s
        run: echo "x=1" >> "$GITHUB_OUTPUT"
  consume:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      - run: echo "${{ needs.build.outputs.x }}"
  orphan:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      - run: echo done
"""
    _write_workflow(tmp_path, "ci.yml", content)
    f = _finding(_scan(tmp_path), "OPT21")
    assert f and f["affected_jobs"] == ["orphan"], "OPT21 should flag only `orphan`"
    first_needs = _line_containing(content, "needs:")            # consume's (file-global)
    orphan_at = _header_line(content, "orphan")
    orphan_needs = _line_containing(content, "needs:", after=orphan_at)
    assert f["line"] == orphan_needs, (
        f"OPT21 line {f['line']} should anchor on orphan's `needs:` (line "
        f"{orphan_needs}), not the file-global first (line {first_needs})")
    assert f["line"] != first_needs


def test_opt27_setup_node_line_anchors_on_the_flagged_job(tmp_path: Path):
    """OPT27 (duplicate `setup-node`) must anchor on the flagged job's own
    `setup-node`, not an earlier job's single use."""
    content = """name: CI
on: pull_request
jobs:
  early:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
      - run: npm ci
  dup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
      - uses: actions/setup-node@v4
      - run: npm ci
"""
    _write_workflow(tmp_path, "ci.yml", content)
    f = _finding(_scan(tmp_path), "OPT27")
    assert f and f["affected_jobs"] == ["dup"], "OPT27 should flag only `dup`"
    first_setup = _line_containing(content, "actions/setup-node")   # early's
    dup_at = _header_line(content, "dup")
    dup_setup = _line_containing(content, "actions/setup-node", after=dup_at)
    assert f["line"] == dup_setup and f["line"] != first_setup, (
        f"OPT27 line {f['line']} should anchor on dup's setup-node (line "
        f"{dup_setup}), not early's (line {first_setup})")


def test_opt39_language_line_anchors_on_the_flagged_job(tmp_path: Path):
    """OPT39 (ungated `language` matrix scanner) must anchor on the flagged job's
    own `language:` line, not an earlier, gated scanner job's."""
    content = """name: CI
on: pull_request
jobs:
  gated-scan:
    runs-on: ubuntu-latest
    if: ${{ matrix.language != 'go' }}
    strategy:
      matrix:
        language: [python]
    steps:
      - uses: github/codeql-action/analyze@v3
  scan:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        language: [python, javascript]
    steps:
      - uses: github/codeql-action/analyze@v3
"""
    _write_workflow(tmp_path, "ci.yml", content)
    f = _finding(_scan(tmp_path), "OPT39")
    assert f and f["affected_jobs"] == ["scan"], "OPT39 should flag only `scan`"
    # The detector's needle is `language` (no colon); under the pre-fix scanner
    # the file-global match was gated-scan's `if: matrix.language` line (earlier
    # still). The load-bearing assertion is that the anchor lands on scan's own
    # `language:` line — that alone is red against the bug.
    scan_at = _header_line(content, "scan")
    scan_language = _line_containing(content, "language:", after=scan_at)
    bug_anchor = _line_containing(content, "language")               # gated-scan's `if:`
    assert f["line"] == scan_language and f["line"] != bug_anchor, (
        f"OPT39 line {f['line']} should anchor on scan's `language:` (line "
        f"{scan_language}), not a gated-scan line (file-global was {bug_anchor})")


def test_opt39_push_only_workflow_does_not_claim_every_pr(tmp_path: Path):
    """A `language` matrix scanner on a PUSH-only / schedule-only workflow runs on ZERO
    PRs — OPT39's "every language leg runs on every PR" claim is false, so it must not
    fire (mirrors the OPT33 `on:`-gate; the claim was previously emitted blindly)."""
    content = """name: CodeQL
on:
  push:
    branches: [main]
  schedule:
    - cron: '0 0 * * 1'
jobs:
  scan:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        language: [python, javascript]
    steps:
      - uses: github/codeql-action/analyze@v3
"""
    _write_workflow(tmp_path, "codeql.yml", content)
    assert _finding(_scan(tmp_path), "OPT39") is None, (
        "OPT39 must not fire on a push/schedule-only workflow (runs on no PR)")


def test_opt39_paths_filtered_pr_scopes_the_every_pr_wording(tmp_path: Path):
    """A `pull_request` with a `paths:` filter does not run on EVERY PR — OPT39 must
    scope its wording to the filtered paths, never bare "every PR"."""
    content = """name: CodeQL
on:
  pull_request:
    paths: ['src/**']
jobs:
  scan:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        language: [python, javascript]
    steps:
      - uses: github/codeql-action/analyze@v3
"""
    _write_workflow(tmp_path, "codeql.yml", content)
    f = _finding(_scan(tmp_path), "OPT39")
    assert f, "OPT39 still fires on a PR-triggered ungated language matrix"
    assert "filtered `paths:`" in f["evidence"]
    assert "on every PR regardless" not in f["evidence"]


def test_line_anchor_handles_four_space_job_indent(tmp_path: Path):
    """`_line_of_in_job` detects the job-header indent rather than assuming two
    spaces — a four-space-indented workflow (valid YAML) must still anchor a
    per-job finding inside its own block, not fall back to the file-global match.
    Here `orphan`'s OPT21 `needs:` must land in orphan's block, not on the first
    `needs:` in the file (`consume`'s)."""
    content = """name: CI
on: pull_request
jobs:
    build:
        runs-on: ubuntu-latest
        outputs:
            x: ${{ steps.s.outputs.x }}
        steps:
            - id: s
              run: echo x=1 >> "$GITHUB_OUTPUT"
    consume:
        needs: [build]
        runs-on: ubuntu-latest
        steps:
            - run: echo "${{ needs.build.outputs.x }}"
    orphan:
        needs: [build]
        runs-on: ubuntu-latest
        steps:
            - run: echo done
"""
    _write_workflow(tmp_path, "ci.yml", content)
    f = _finding(_scan(tmp_path), "OPT21")
    assert f and f["affected_jobs"] == ["orphan"], "OPT21 should flag only `orphan`"
    first_needs = _line_containing(content, "needs:")               # consume's (file-global)
    orphan_at = _header_line(content, "orphan")
    orphan_needs = _line_containing(content, "needs:", after=orphan_at)
    assert f["line"] == orphan_needs and f["line"] != first_needs, (
        f"OPT21 line {f['line']} should anchor on orphan's `needs:` (line "
        f"{orphan_needs}) under 4-space indent, not the file-global first "
        f"(line {first_needs})")


def test_line_of_in_job_falls_back_to_zero_not_another_job(tmp_path: Path):
    """When the needle is NOT inside the flagged job's block — a quoted/differently
    spaced variant, or a job key the header regex can't match — `_line_of_in_job`
    must return 0 (renders filename-only, no snippet), NEVER the file-global first
    match, which would cite a DIFFERENT job and paste its YAML as the evidence
    snippet. (PR #88 review.)"""
    import importlib.util
    name = "ci_speedup_scan_anchor"
    spec = importlib.util.spec_from_file_location(name, _SCAN_SCRIPT)
    scan = importlib.util.module_from_spec(spec)
    sys.modules[name] = scan  # register first: scan.py's @dataclass resolves __module__ here
    spec.loader.exec_module(scan)
    raw = """name: CI
on: pull_request
jobs:
  other:
    runs-on: ubuntu-latest
    steps:
      - run: echo "needs: in a string"
  flagged:
    runs-on: ubuntu-latest
    steps:
      - run: echo done
"""
    # `needs:` appears only in `other` — the file-global search WOULD find it...
    assert scan._line_of(raw, "needs:") != 0
    # ...but it is not in `flagged`'s block, so we refuse to cite another job.
    assert scan._line_of_in_job(raw, "flagged", "needs:") == 0
    # An unmatchable job key (header regex rejects it → `start is None`) also → 0.
    assert scan._line_of_in_job(raw, "no such job", "needs:") == 0
    # In-block resolution is unaffected.
    assert scan._line_of_in_job(raw, "other", "needs:") == _line_containing(raw, "needs:")


def test_opt33_negative_with_draft_gate(tmp_path: Path):
    neg = """name: CI
on: pull_request
jobs:
  adapters:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    strategy:
      matrix:
        adapter: [prisma, drizzle]
    steps:
      - run: pnpm test
"""
    assert "OPT33" not in _scan_one(tmp_path, neg)


def test_opt36_mn_step_not_false_positive(tmp_path: Path):
    """`30/30` fires only at minute 30 = once/hour → OPT36 must NOT fire
    (regression for the off-by-one in the M/N frequency formula)."""
    neg = """name: cron
on:
  schedule:
    - cron: '30/30 * * * *'
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
    assert "OPT36" not in _scan_one(tmp_path, neg, name="cron.yml")


def test_opt36_truly_frequent_fires(tmp_path: Path):
    pos = """name: cron
on:
  schedule:
    - cron: '*/5 * * * *'
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
    assert "OPT36" in _scan_one(tmp_path, pos, name="cron.yml")


def test_opt39_multilang_matrix_no_filter(tmp_path: Path):
    pos = """name: CodeQL
on: pull_request
jobs:
  analyze:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        language: [javascript, python]
    steps:
      - uses: github/codeql-action/init@v3
        with:
          languages: ${{ matrix.language }}
      - uses: github/codeql-action/analyze@v3
"""
    assert "OPT39" in _scan_one(tmp_path, pos, name="codeql.yml")


def test_opt39_negative_with_paths_filter(tmp_path: Path):
    neg = """name: CodeQL
on: pull_request
jobs:
  changes:
    runs-on: ubuntu-latest
    steps:
      - uses: dorny/paths-filter@v3
  analyze:
    needs: changes
    runs-on: ubuntu-latest
    strategy:
      matrix:
        language: [javascript, python]
    steps:
      - uses: github/codeql-action/init@v3
        with:
          languages: ${{ matrix.language }}
"""
    assert "OPT39" not in _scan_one(tmp_path, neg, name="codeql.yml")


def test_opt62_clean_on_self_hosted(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  build:
    runs-on: [self-hosted, linux]
    steps:
      - run: rm -rf target
      - run: cargo build
"""
    assert "OPT62" in _scan_one(tmp_path, pos)


def test_opt62_negative_github_hosted(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: rm -rf target
      - run: cargo build
"""
    assert "OPT62" not in _scan_one(tmp_path, neg)


def test_opt63_no_cache_flag_self_hosted(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  build:
    runs-on: self-hosted
    steps:
      - run: pip install --no-cache-dir -r requirements.txt
"""
    assert "OPT63" in _scan_one(tmp_path, pos)


def test_opt63_negative_github_hosted(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: pip install --no-cache-dir -r requirements.txt
"""
    assert "OPT63" not in _scan_one(tmp_path, neg)


# =============================================================================
# Per-instance precondition guards (adversarial-audit regressions): a finding's
# remedy must be valid for THIS instance, not just structurally matched.
# =============================================================================

def test_opt28_suppressed_when_job_needs_git_history(tmp_path: Path):
    """`fetch-depth: 0` on a changeset/release job is load-bearing — shallowing
    it breaks changelogs/versioning, so it must NOT be flagged."""
    neg = """name: Release
on: push
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: pnpm changeset version
      - run: pnpm changeset publish
"""
    assert "OPT28" not in _scan_one(tmp_path, neg, name="release.yml")


def test_opt28_suppressed_on_two_sha_diff(tmp_path: Path):
    """`git diff base.sha head.sha` (PR change detection) needs full history —
    a shallow checkout doesn't contain base.sha — so `fetch-depth: 0` is
    load-bearing and OPT28 must NOT flag it (mastra lint-docs/lint/prebuild)."""
    neg = """name: lint
on: pull_request
jobs:
  changes:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - run: |
          ALL=$(git diff --name-only ${{ github.event.pull_request.base.sha }} ${{ github.event.pull_request.head.sha }})
"""
    assert "OPT28" not in _scan_one(tmp_path, neg, name="lint.yml")


def test_opt28_suppressed_on_bot_commit_back_rebase(tmp_path: Path):
    """A bot commit-back job running `git pull --rebase` + `git push` needs base
    history — `fetch-depth: 0` is load-bearing (mastra regenerate-provider)."""
    neg = """name: regenerate
on: workflow_dispatch
jobs:
  regenerate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - run: |
          git add -A
          git commit -m "chore: regenerate"
          git pull --rebase
          git push
"""
    assert "OPT28" not in _scan_one(tmp_path, neg, name="regenerate.yml")


def test_opt28_suppressed_on_backslash_continued_history_command(tmp_path: Path):
    """A `run:` block that continues a git command onto the next line with a
    trailing backslash is ONE shell command. The merge-base diff below needs
    full history, so `fetch-depth: 0` is load-bearing — OPT28 must not
    recommend shallowing the job just because the operand sits on line two."""
    neg = """name: prebuild
on: pull_request
jobs:
  affected-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: |
          git diff --name-only \\
            "${{ github.event.pull_request.base.sha }}...${{ github.event.pull_request.head.sha }}" \\
            > /tmp/changed-files.txt
"""
    assert "OPT28" not in _scan_one(tmp_path, neg, name="prebuild.yml")


def test_opt28_suppressed_on_diff_against_a_variable_base_ref(tmp_path: Path):
    """A `git diff` whose base operand is a shell variable is still a diff
    against a base commit — a shallow clone does not contain it — so
    `fetch-depth: 0` is load-bearing and OPT28 must stay silent."""
    neg = """name: CI
on: pull_request
jobs:
  changed:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: |
          base=$(cat base.txt)
          git diff --no-renames --name-only "$base" HEAD
"""
    assert "OPT28" not in _scan_one(tmp_path, neg, name="changed.yml")


def test_opt28_suppressed_on_cat_file_reachability_probe(tmp_path: Path):
    """`git cat-file -e <sha>^{commit}` probes whether an object is present in
    the clone — it only succeeds with the history fetched, so the job needs
    `fetch-depth: 0` and OPT28 must not flag it."""
    neg = """name: CI
on: pull_request
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: |
          base="${{ github.event.pull_request.base.sha }}"
          git cat-file -e "${base}^{commit}"
"""
    assert "OPT28" not in _scan_one(tmp_path, neg, name="probe.yml")


def test_opt28_suppressed_by_a_documented_history_justification_comment(tmp_path: Path):
    """A YAML comment above `fetch-depth: 0` naming a history command is the one
    artifact that settles whether the depth is load-bearing — and comments are
    dropped at parse time, so the detector never saw it. It must now stay
    silent rather than recommend a change that breaks the job."""
    neg = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      # full history is required: the version stamp comes from `git describe`
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: make build
"""
    assert "OPT28" not in _scan_one(tmp_path, neg, name="build.yml")


def test_opt28_still_fires_when_a_comment_denies_the_depth_is_needed(tmp_path: Path):
    """The justification suppressor must read the comment, not merely notice one.
    A comment saying the depth is unnecessary is not a justification, and the
    word `depth` alone never suppresses."""
    pos = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      # fetch-depth: 0 is unnecessary here, left over from an old job
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: make build
"""
    assert "OPT28" in _scan_one(tmp_path, pos, name="build.yml")


def test_opt28_ignores_a_history_comment_too_far_above_the_step(tmp_path: Path):
    """The suppressor reads a NEARBY comment block. A history comment attached to
    an earlier step is not a justification for this one, and must not silence it."""
    pos = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      # this earlier step is the one that needs `git log`
      - run: echo one
      - run: echo two
      - run: echo three
      - run: echo four
      - run: echo five
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: make build
"""
    assert "OPT28" in _scan_one(tmp_path, pos, name="build.yml")


def test_opt28_line_points_at_the_flagged_job_not_the_first_match(tmp_path: Path):
    """A per-job OPT28 hit must record ITS OWN `fetch-depth: 0` line, not the
    file-global first match. prebuild.yml has depth:0 in the `changes` job (two-SHA
    diff, load-bearing, correctly excluded) AND in a plain `build` job (flaggable).
    The file-global `_line_of` returned the `changes` line for every hit, so the
    generated diff edited the one job that must keep depth:0."""
    wf = """name: prebuild
on: pull_request
jobs:
  changes:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - run: |
          CHANGED=$(git diff --name-only ${{ github.event.pull_request.base.sha }} ${{ github.event.pull_request.head.sha }})
  build:
    needs: changes
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - run: pnpm install && pnpm build
"""
    _write_workflow(tmp_path, "prebuild.yml", wf)
    opt28 = [f for f in _scan(tmp_path)["findings"] if f["pattern"] == "OPT28"]
    # Only the plain `build` job is flagged; the two-SHA `changes` job is excluded.
    assert [f["affected_jobs"] for f in opt28] == [["build"]]
    # The recorded line is the `build` job's own depth:0 (the SECOND occurrence
    # in the file), NOT the file-global first match in the `changes` job.
    raw = wf.splitlines()
    depth_lines = [i + 1 for i, ln in enumerate(raw)
                   if ln.strip() == "fetch-depth: 0"]
    assert opt28[0]["line"] == depth_lines[1]  # build's, not changes' (==[0])


def test_opt28_excludes_paths_filter_change_detection_job(tmp_path: Path):
    """A job that runs `dorny/paths-filter` with `base:` (or `tj-actions/changed-
    files`) diffs the head against the base branch — that needs base history, so
    `fetch-depth: 0` is LOAD-BEARING and OPT28 must NOT recommend shallowing it.
    Regression from the mastra adversarial review: prebuild `*-check-changes` jobs
    were wrongly flagged with 'no git-history operation found in the job'."""
    wf = """name: prebuild
on: pull_request
jobs:
  e2e-check-changes:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: dorny/paths-filter@v3
        id: changes
        with:
          base: ${{ github.event.pull_request.base.ref }}
          ref: ${{ github.event.pull_request.head.sha }}
          filters: |
            e2e:
              - 'packages/**'
"""
    _write_workflow(tmp_path, "prebuild.yml", wf)
    opt28 = [f for f in _scan(tmp_path)["findings"] if f["pattern"] == "OPT28"]
    assert opt28 == []  # paths-filter needs base history — never an OPT28 finding


def test_opt33_suppressed_on_alls_green_aggregator(tmp_path: Path):
    # The `e2e` job is a status aggregator (alls-green) — gating it on draft is
    # wrong and it does no test work despite the test-y name.
    neg = """name: e2e
on: pull_request
jobs:
  integration:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        adapter: [prisma, drizzle]
    steps:
      - run: pnpm e2e:smoke
  e2e:
    needs: [integration]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - uses: re-actors/alls-green@release/v1
        with:
          jobs: ${{ toJSON(needs) }}
"""
    # OPT33 fires on the real `integration` matrix job, but NOT on the `e2e`
    # alls-green aggregator.
    _write_workflow(tmp_path, "e2e.yml", neg)
    data = _scan(tmp_path)
    opt33_jobs = [j for f in data["findings"] if f["pattern"] == "OPT33"
                  for j in f["affected_jobs"]]
    assert "e2e" not in opt33_jobs
    assert "integration" in opt33_jobs


def test_opt33_suppressed_on_change_detection_job(tmp_path: Path):
    # `e2e-check-changes` runs only dorny/paths-filter — a seconds-long gate, not
    # an expensive test job, even though its name contains `e2e`.
    neg = """name: prebuild
on: pull_request
jobs:
  e2e-check-changes:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: f
        with:
          filters: |
            e2e: ['packages/**']
"""
    _write_workflow(tmp_path, "prebuild.yml", neg)
    data = _scan(tmp_path)
    opt33_jobs = [j for f in data["findings"] if f["pattern"] == "OPT33"
                  for j in f["affected_jobs"]]
    assert "e2e-check-changes" not in opt33_jobs


def test_opt28_suppressed_when_local_action_needs_history(tmp_path: Path):
    """A job whose git op lives in an invoked local composite action
    (`uses: ./.github/actions/turbo-changed` running `git checkout origin/main`)
    still needs full history — OPT28 must resolve the action file and suppress
    (mastra memory-check-changes regression)."""
    action_dir = tmp_path / ".github" / "actions" / "turbo-changed"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "action.yml").write_text(
        "name: turbo-changed\nruns:\n  using: composite\n  steps:\n"
        "    - shell: bash\n      run: |\n        git rev-parse HEAD\n"
        "        git checkout origin/main\n", encoding="utf-8")
    neg = """name: prebuild
on: pull_request
jobs:
  memory-check-changes:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: ./.github/actions/turbo-changed
"""
    assert "OPT28" not in _scan_one(tmp_path, neg, name="prebuild.yml")


def test_opt28_fails_closed_on_unreadable_local_action(tmp_path: Path):
    """If a job invokes a local composite action whose file is missing/unreadable,
    we can't prove it's history-free — fail CLOSED (suppress OPT28) rather than
    recommend a fix that might break the job."""
    # Note: no action file is created for `./.github/actions/mystery`.
    neg = """name: prebuild
on: pull_request
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: ./.github/actions/mystery
"""
    assert "OPT28" not in _scan_one(tmp_path, neg, name="prebuild.yml")


def test_opt28_fires_when_no_history_needed(tmp_path: Path):
    pos = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: pnpm test
"""
    assert "OPT28" in _scan_one(tmp_path, pos)


def test_opt35_suppressed_on_diagnostic_matrix(tmp_path: Path):
    """A node-version / named-adapter matrix with fail-fast:false is correct —
    you want each variant's result — so OPT35 must NOT fire."""
    neg = """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        adapter: [prisma, drizzle, mongo, memory]
    steps:
      - run: pnpm test --filter ${{ matrix.adapter }}
"""
    assert "OPT35" not in _scan_one(tmp_path, neg)


def test_opt35_fires_on_shard_indexed_matrix(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        shard: [1, 2, 3, 4]
    steps:
      - run: pnpm test --shard ${{ matrix.shard }}/4
"""
    assert "OPT35" in _scan_one(tmp_path, pos)


def test_opt35_shard_axis_token_set(tmp_path: Path):
    """Pins _SHARD_AXIS_RE's exact token set (shard|chunk|split|partition).
    The score extraction inlined the winning of two module-level bindings
    (the deleted score block's later rebinding won at import time); these
    axes prove the runtime behavior carried over: split/chunk/partition
    fire, and group_index — a token only the shadowed dead first binding
    matched — never fired at runtime before and still doesn't."""
    def wf(axis: str) -> str:
        return (
            "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
            "    strategy:\n      fail-fast: false\n      matrix:\n"
            f"        {axis}: [1, 2, 3, 4]\n"
            "    steps:\n"
            f"      - run: pnpm test --pick ${{{{ matrix.{axis} }}}}/4\n"
        )

    for axis in ("split", "chunk", "partition"):
        assert "OPT35" in _scan_one(tmp_path, wf(axis)), axis
    assert "OPT35" not in _scan_one(tmp_path, wf("group_index"))


def test_opt45_suppressed_on_release_workflow(tmp_path: Path):
    """cancel-in-progress on a publish workflow is unsafe (half-applied
    version bump) — OPT45 must not recommend it."""
    neg = """name: Release
on:
  push:
    branches: [main]
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm changeset publish
"""
    assert "OPT45" not in _scan_one(tmp_path, neg, name="release.yml")


def test_opt45_suppressed_when_jobs_have_concurrency(tmp_path: Path):
    neg = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    concurrency:
      group: ${{ github.workflow }}-test-${{ github.ref }}
      cancel-in-progress: true
    steps:
      - run: pnpm test
"""
    assert "OPT45" not in _scan_one(tmp_path, neg)


def test_opt45_fires_when_truly_unprotected(tmp_path: Path):
    pos = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
"""
    assert "OPT45" in _scan_one(tmp_path, pos)


def test_opt5_suppressed_when_pnpm_action_setup_caches(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: pnpm/action-setup@v4
        with:
          cache: true
      - run: pnpm install
"""
    assert "OPT5" not in _scan_one(tmp_path, neg)


def test_opt52_58_skip_uncacheable_and_explicit(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml",
                    "name: x\non: push\njobs:\n  run:\n    runs-on: ubuntu-latest\n"
                    "    steps:\n      - run: turbo run clean lint build\n")
    _write_turbo(tmp_path, """{
  "tasks": {
    "clean": { "cache": false },
    "lint": { "outputs": [] },
    "build": { "cache": true }
  }
}""")
    fired = _patterns(_scan(tmp_path))
    # clean (cache:false) and lint (explicit outputs:[]) are NOT flagged;
    # build (cacheable, no outputs/inputs key) IS.
    data = _scan(tmp_path)
    opt52 = _finding(data, "OPT52")
    assert opt52 and "build" in opt52["evidence"]
    assert "clean" not in opt52["evidence"] and "lint" not in opt52["evidence"]


def test_opt16_different_working_directory_not_dup(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  demo:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm install
      - run: pnpm install
        working-directory: demo/app
"""
    assert "OPT16" not in _scan_one(tmp_path, neg)


def test_opt16_reinstall_after_changeset_version_not_dup(tmp_path: Path):
    neg = """name: Release
on: push
jobs:
  stable:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm install
      - run: pnpm changeset version
      - run: pnpm install
"""
    assert "OPT16" not in _scan_one(tmp_path, neg, name="release.yml")


def test_opt16_reinstall_in_same_step_as_version_not_dup(tmp_path: Path):
    """The re-build often shares a multi-line step with the version bump
    (changeset version + build) — must still be recognized as a re-sync."""
    neg = """name: Release
on: push
jobs:
  stable:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm build
      - run: |
          pnpm changeset version
          pnpm install
          pnpm build
"""
    assert "OPT16" not in _scan_one(tmp_path, neg, name="release.yml")


def test_opt1_suppressed_when_job_runs_package_script(tmp_path: Path):
    neg = """name: e2e-docs
on: pull_request
jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm exec playwright install --with-deps chromium
      - run: pnpm test:smoke
"""
    assert "OPT1" not in _scan_one(tmp_path, neg, name="e2e-docs.yml")


# =============================================================================
# Phase 2c — cross-workflow / repo-context detectors
# =============================================================================

def _findings(data: dict) -> list[dict]:
    return data["findings"]


def test_opt7_pnpm_version_drift(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: pnpm/action-setup@v4
        with:
          version: 9.1.0
""")
    _write_workflow(tmp_path, "release.yml", """name: Release
on: push
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: pnpm/action-setup@v4
        with:
          version: 8.15.0
""")
    assert "OPT7" in _patterns(_scan(tmp_path))


def test_opt7_negative_consistent_versions(tmp_path: Path):
    for name in ("ci.yml", "release.yml"):
        _write_workflow(tmp_path, name, """name: X
on: push
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - uses: pnpm/action-setup@v4
        with:
          version: 9.1.0
""")
    assert "OPT7" not in _patterns(_scan(tmp_path))


def test_opt12_duplicated_setup(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
      - run: pnpm install
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
      - run: pnpm install
"""
    assert "OPT12" in _scan_one(tmp_path, pos)


def test_opt12_negative_checkout_only_status_jobs(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  ok-a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo a
  ok-b:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo b
"""
    assert "OPT12" not in _scan_one(tmp_path, neg)


def test_opt37_cache_race_candidate_is_low_severity(tmp_path: Path):
    _write_workflow(tmp_path, "prebuild.yml", """name: Prebuild
on: pull_request
env:
  TURBO_CACHE: remote:rw
jobs:
  prebuild:
    runs-on: ubuntu-latest
    steps:
      - run: turbo run build
""")
    _write_workflow(tmp_path, "lint.yml", """name: Lint
on: pull_request
env:
  TURBO_CACHE: remote:ro
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - run: turbo run build
""")
    data = _scan(tmp_path)
    opt37 = [f for f in _findings(data) if f["pattern"] == "OPT37"]
    assert opt37, "OPT37 candidate should fire"
    f = opt37[0]
    assert f["severity"] == "LOW", "structural candidate must be LOW, not HIGH"
    assert f.get("needs_log_confirmation") is True
    assert "STRUCTURAL CANDIDATE" in f["evidence"]


def test_opt37_negative_workflow_run_linked(tmp_path: Path):
    _write_workflow(tmp_path, "prebuild.yml", """name: Prebuild
on: pull_request
env:
  TURBO_CACHE: remote:rw
jobs:
  prebuild:
    runs-on: ubuntu-latest
    steps:
      - run: turbo run build
""")
    _write_workflow(tmp_path, "lint.yml", """name: Lint
on:
  workflow_run:
    workflows: [Prebuild]
    types: [completed]
env:
  TURBO_CACHE: remote:ro
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - run: turbo run build
""")
    assert "OPT37" not in _patterns(_scan(tmp_path))


def test_opt40_monorepo_single_app_no_filter(tmp_path: Path):
    (tmp_path / "apps").mkdir()
    (tmp_path / "turbo.json").write_text("{}", encoding="utf-8")
    pos = """name: CI
on: pull_request
jobs:
  web-quality:
    runs-on: ubuntu-latest
    steps:
      - run: cd apps/web && pnpm playwright test
"""
    assert "OPT40" in _scan_one(tmp_path, pos)


def test_opt40_negative_with_paths_filter(tmp_path: Path):
    (tmp_path / "apps").mkdir()
    (tmp_path / "turbo.json").write_text("{}", encoding="utf-8")
    pos = """name: CI
on:
  pull_request:
    paths: ['apps/web/**']
jobs:
  web-quality:
    runs-on: ubuntu-latest
    steps:
      - run: cd apps/web && pnpm playwright test
"""
    assert "OPT40" not in _scan_one(tmp_path, pos)


def test_opt40_negative_pr_filtered_push_unfiltered(tmp_path: Path):
    # OPT40 is PR-scoped: its evidence claims "every PR". When `pull_request`
    # carries a `paths:` filter, PRs are gated even if a sibling bare `push`
    # trigger is not — so OPT40 must NOT fire (the PR claim would be false).
    # The all-triggers `_on_has_paths_filter` would wrongly keep firing here.
    (tmp_path / "apps").mkdir()
    (tmp_path / "turbo.json").write_text("{}", encoding="utf-8")
    neg = """name: CI
on:
  pull_request:
    paths: ['apps/web/**']
  push:
jobs:
  web-quality:
    runs-on: ubuntu-latest
    steps:
      - run: cd apps/web && pnpm playwright test
"""
    assert "OPT40" not in _scan_one(tmp_path, neg)


def test_opt40_negative_shared_package_target(tmp_path: Path):
    # Per-instance precondition: a target under `packages/` is a SHARED library,
    # not an app — most PRs legitimately touch it, so OPT40 ("affected APP") must
    # NOT fire (mastra `--filter ./packages/server` peerdeps-check regression).
    (tmp_path / "packages").mkdir()
    (tmp_path / "turbo.json").write_text("{}", encoding="utf-8")
    neg = """name: CI
on: pull_request
jobs:
  peerdeps-check:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm --filter ./packages/server check
"""
    assert "OPT40" not in _scan_one(tmp_path, neg)


# =============================================================================
# Phase 2d — repo-file (turbo.json) detectors + OPT3
# =============================================================================

def test_opt3_flags_read_only_cache_not_read_write(tmp_path: Path):
    # `remote:rw` is the CORRECT config for ephemeral CI runners (local cache never
    # persists), so it is NOT flagged. Only `remote:ro` (reads but never writes the
    # cache) is — that's the genuinely-suspect case.
    rw = """name: CI
on: push
env:
  TURBO_CACHE: remote:rw
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: turbo run build
"""
    assert "OPT3" not in _scan_one(tmp_path, rw, name="ci.yml")
    ro = rw.replace("remote:rw", "remote:ro")
    assert "OPT3" in _scan_one(tmp_path, ro, name="ci.yml")


def test_opt3_excludes_release_workflow(tmp_path: Path):
    """In a release workflow remote:rw is OPT42's territory, not OPT3's."""
    pos = """name: Release
on: push
env:
  TURBO_CACHE: remote:rw
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - run: turbo run build
"""
    fired = _scan_one(tmp_path, pos, name="release.yml")
    assert "OPT3" not in fired
    assert "OPT42" in fired


def _write_turbo(tmp_path: Path, obj: str) -> None:
    (tmp_path / "turbo.json").write_text(obj, encoding="utf-8")


def test_opt52_58_60_missing_outputs_inputs_ciconfig(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: turbo run build\n")
    _write_turbo(tmp_path, """{
  "tasks": {
    "build": { "cache": true }
  }
}""")
    fired = _patterns(_scan(tmp_path))
    assert "OPT52" in fired   # build has no outputs
    assert "OPT58" in fired   # build has no inputs
    assert "OPT60" in fired   # no ui / futureFlags / outputLogs


def test_opt53_unstable_env(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: turbo run build\n")
    _write_turbo(tmp_path, """{
  "globalEnv": ["GITHUB_RUN_ID"],
  "tasks": { "build": { "outputs": ["dist/**"], "inputs": ["src/**"] } }
}""")
    assert "OPT53" in _patterns(_scan(tmp_path))


def test_opt59_runtime_only_secret(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: turbo run build\n")
    _write_turbo(tmp_path, """{
  "globalEnv": ["ANTHROPIC_API_KEY", "NEXT_PUBLIC_URL"],
  "tasks": { "build": { "outputs": ["dist/**"], "inputs": ["src/**"] } }
}""")
    fired = _patterns(_scan(tmp_path))
    assert "OPT59" in fired


def test_turbo_negative_well_configured(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: turbo run build\n")
    _write_turbo(tmp_path, """{
  "ui": "stream",
  "futureFlags": { "affectedUsingTaskInputs": true },
  "tasks": {
    "build": {
      "outputs": ["dist/**"],
      "inputs": ["src/**"],
      "outputLogs": "new-only"
    }
  }
}""")
    fired = _patterns(_scan(tmp_path))
    for pat in ("OPT52", "OPT53", "OPT58", "OPT59", "OPT60"):
        assert pat not in fired, f"{pat} should not fire on a well-configured turbo.json"


def test_turbo_unparseable_is_a_coverage_gap_not_clean(tmp_path: Path):
    """A present-but-unparseable turbo.json must surface in scan_incomplete —
    never be silently treated as 'no turbo findings'."""
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    _write_turbo(tmp_path, "{ this is : not json , , }")
    data = _scan(tmp_path)
    reasons = " ".join(r.get("reason", "") for r in data["scan_incomplete"])
    assert "turbo.json" in reasons and "NOT evaluated" in reasons
    assert "OPT52" not in _patterns(data)  # and no fabricated findings


# =============================================================================
# Declarative engine + wf-name routing (regression coverage)
# =============================================================================

def test_opt6_declarative_match_positive_and_negative(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          key: build-${{ github.sha }}
          path: dist
"""
    assert "OPT6" in _scan_one(tmp_path, pos)
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          key: build-${{ hashFiles('pnpm-lock.yaml') }}
          path: dist
"""
    assert "OPT6" not in _scan_one(tmp_path, neg)


def test_opt6_suppressed_when_restore_keys_present(tmp_path: Path):
    """A per-run primary key WITH a restore-keys prefix still restores content
    via fallback — not a defeated cache (OPT8 guardrail)."""
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          key: ${{ runner.os }}-turbo-${{ github.sha }}
          restore-keys: |
            ${{ runner.os }}-turbo-
          path: .turbo
"""
    assert "OPT6" not in _scan_one(tmp_path, neg)


def test_opt42_wf_name_filter_does_not_fire_outside_release(tmp_path: Path):
    """OPT42 (release-only) must NOT fire on ci.yml; OPT3 owns non-release (the
    read-only `remote:ro` case — `remote:rw` is correct and flagged by neither)."""
    content = """name: CI
on: push
env:
  TURBO_CACHE: remote:ro
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: turbo run build
"""
    fired = _scan_one(tmp_path, content, name="ci.yml")
    assert "OPT42" not in fired
    assert "OPT3" in fired


def test_opt9_tsc_without_then_with_build_flag(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  types:
    runs-on: ubuntu-latest
    steps:
      - run: npx tsc --noEmit
"""
    assert "OPT9" in _scan_one(tmp_path, pos)
    neg = """name: CI
on: push
jobs:
  types:
    runs-on: ubuntu-latest
    steps:
      - run: npx tsc --build
"""
    assert "OPT9" not in _scan_one(tmp_path, neg)


# =============================================================================
# Phase 2e — OPT19 source-file sleep grep
# =============================================================================

def test_opt19_source_sleep_dominance(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    e2e = tmp_path / "packages" / "app" / "e2e" / "tests"
    e2e.mkdir(parents=True)
    (e2e / "flow.spec.ts").write_text(
        "test('x', async ({ page }) => {\n"
        "  await page.waitForTimeout(3000);\n"
        "  await sleep(2000);\n"
        "  await new Promise(r => setTimeout(r, 5000));\n"
        "});\n", encoding="utf-8")
    data = _scan(tmp_path)
    opt19 = [f for f in data["findings"] if f["pattern"] == "OPT19"]
    assert opt19, "OPT19 should fire on test-source sleeps"
    f = opt19[0]
    # 3000 + 2000 + 5000 = 10000ms = 10s
    assert f["wall_clock_p50_s"] == 10.0, f["wall_clock_p50_s"]
    assert f.get("measured_signal")
    # OPT19's remedy edits TEST SOURCE (not CI config), so it's advisory — a
    # reliability/hygiene signal, never a ranked optimization (same as OPT48).
    assert f.get("advisory") is True


def test_opt19_negative_timeout_guard_not_counted(tmp_path: Path):
    """A `setTimeout(() => …, N)` timeout-guard never blocks and must not count."""
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    t = tmp_path / "tests"
    t.mkdir()
    (t / "guard.test.ts").write_text(
        "it('x', () => {\n"
        "  const h = setTimeout(() => reject(new Error('timeout')), 30000);\n"
        "});\n", encoding="utf-8")
    assert "OPT19" not in _patterns(_scan(tmp_path))


def test_opt19_python_time_sleep_seconds_to_ms(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_slow.py").write_text(
        "def test_x():\n    import time\n    time.sleep(2.5)\n", encoding="utf-8")
    data = _scan(tmp_path)
    opt19 = [f for f in data["findings"] if f["pattern"] == "OPT19"]
    assert opt19, "OPT19 should fire on Python time.sleep"
    # 2.5s → 2.5 (seconds × 1000ms / 1000 = 2.5s wall-clock)
    assert opt19[0]["wall_clock_p50_s"] == 2.5


def test_opt19_ellipsis_stub_does_not_crash(tmp_path: Path):
    """`time.sleep(...)` is the common Ellipsis stub for an unimplemented body.

    Its non-numeric capture must not raise ValueError out of the scanner (a
    crash there is fatal — run.py aborts the whole audit) and must not count
    as an OPT19 occurrence, since there is no real numeric delay to sum."""
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_stub.py").write_text(
        "def test_x():\n    import time\n    time.sleep(...)\n", encoding="utf-8")
    data = _scan(tmp_path)  # must not raise (subprocess check=True would surface a crash)
    assert "OPT19" not in _patterns(data)


def test_opt19_malformed_numeric_does_not_crash(tmp_path: Path):
    """`time.sleep(1.2.3)` is a malformed multi-dot literal (invalid Python).
    Scanning must not crash; the greedy float grammar captures the leading
    valid prefix (1.2) and OPT19 fires on it. Assert that DEFINED behavior so
    the malformed case isn't left silent (there is no producible real code with
    a multi-dot sleep arg, so best-effort prefix capture is acceptable)."""
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_malformed.py").write_text(
        "def test_x():\n    import time\n    time.sleep(1.2.3)\n", encoding="utf-8")
    data = _scan(tmp_path)  # must not raise
    assert isinstance(data, dict)
    opt19 = [f for f in data["findings"] if f["pattern"] == "OPT19"]
    assert opt19, "the leading 1.2 prefix of 1.2.3 is captured (greedy), so OPT19 fires"
    assert opt19[0]["wall_clock_p50_s"] == 1.2


def test_opt19_python_time_sleep_still_counts_with_tightened_grammar(tmp_path: Path):
    """Tightening the numeric capture must not regress the real detector."""
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_slow.py").write_text(
        "def test_x():\n    import time\n    time.sleep(1.5)\n", encoding="utf-8")
    data = _scan(tmp_path)
    opt19 = [f for f in data["findings"] if f["pattern"] == "OPT19"]
    assert opt19, "OPT19 should still fire on time.sleep(1.5)"
    assert opt19[0]["wall_clock_p50_s"] == 1.5


def test_opt19_python_time_sleep_leading_dot_float(tmp_path: Path):
    """`time.sleep(.5)` is a valid 0.5s sleep with no leading digit — the
    tightened grammar must still capture it (500ms → 0.5s wall-clock), not
    over-narrow to require a leading digit and miss it."""
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_slow.py").write_text(
        "def test_x():\n    import time\n    time.sleep(.5)\n", encoding="utf-8")
    data = _scan(tmp_path)
    opt19 = [f for f in data["findings"] if f["pattern"] == "OPT19"]
    assert opt19, "OPT19 should fire on time.sleep(.5)"
    # .5s → 0.5 * 1000ms / 1000 = 0.5s wall-clock
    assert opt19[0]["wall_clock_p50_s"] == 0.5


# =============================================================================
# Evidence quality — findings must carry the VERBATIM matched code, not prose
# (regression for the "evidence doesn't let a human verify the claim" audit).
# =============================================================================

def _finding(data: dict, pattern: str) -> dict | None:
    return next((f for f in data["findings"] if f["pattern"] == pattern), None)


def test_static_finding_carries_verbatim_snippet(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: pnpm build
"""
    data = _scan_dir_with(tmp_path, pos)
    f = _finding(data, "OPT28")
    assert f and "fetch-depth: 0" in (f.get("evidence_snippet") or "")
    assert f["line"] > 1  # precise line, not the top of the file


def test_absence_finding_shows_on_block(tmp_path: Path):
    pos = """name: CI
on:
  pull_request:
    types: [opened, synchronize]
  push:
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        n: [1, 2]
    steps:
      - run: pnpm test
"""
    data = _scan_dir_with(tmp_path, pos)
    f = _finding(data, "OPT32")  # missing paths filter — an absence
    assert f, "OPT32 should fire"
    snip = f.get("evidence_snippet") or ""
    assert "on:" in snip and "pull_request" in snip  # the block proving the absence


def test_opt32_push_only_evidence_omits_pr_claim_and_note(tmp_path: Path):
    # A push-only workflow never runs on PRs, so its OPT32 evidence must not
    # claim it "triggers on pull_request/push", and must drop the REQUIRED
    # status-check NOTE (which only makes sense for PR-triggered workflows).
    pos = """name: release-plz
on:
  push:
    branches: [main]
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - run: cargo build
"""
    data = _scan_dir_with(tmp_path, pos)
    f = _finding(data, "OPT32")
    assert f, "OPT32 should fire on a push-only workflow with no paths filter"
    ev = f.get("evidence") or ""
    assert "pull_request" not in ev, ev  # false for a push-only workflow
    assert "triggers on push" in ev, ev  # the true trigger list
    assert "REQUIRED status check" not in ev, ev  # NOTE is meaningless here


def test_opt32_pr_workflow_keeps_required_check_note(tmp_path: Path):
    # A PR-triggered workflow keeps the REQUIRED status-check caveat.
    pos = """name: CI
on:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
"""
    data = _scan_dir_with(tmp_path, pos)
    f = _finding(data, "OPT32")
    assert f, "OPT32 should fire"
    ev = f.get("evidence") or ""
    assert "triggers on pull_request" in ev, ev
    assert "REQUIRED status check" in ev, ev


def test_opt32_suppressed_on_types_labeled_only_trigger(tmp_path: Path):
    # CLASS-fix routing guard for OPT32 (missing paths filter): a `pull_request`
    # trigger gated to a NON-lifecycle activity (labeled / unlabeled / …) reacts
    # to PR metadata, not code pushes — those events carry no file diff, so a
    # `paths:` filter is irrelevant and OPT32 must NOT fire. Routed through the
    # shared `_pr_trigger_runs_every_pr` predicate (same drain as OPT39/40/33).
    label_only = """name: rerun-danger-on-label
# Re-run Danger whenever the label is (un)applied, regardless of files changed.
on:
  pull_request:
    types: [labeled, unlabeled]
jobs:
  danger:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        n: [1, 2]
    steps:
      - run: pnpm danger ci
"""
    assert "OPT32" not in _scan_one(tmp_path, label_only)


def test_opt32_fires_on_labeled_plus_lifecycle_trigger(tmp_path: Path):
    # Counterpart: a `pull_request` trigger whose `types:` INCLUDES a lifecycle
    # activity (synchronize) still runs on real code pushes, so the missing
    # `paths:` filter IS actionable and OPT32 must still fire.
    mixed = """name: ci
on:
  pull_request:
    types: [labeled, synchronize]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
"""
    assert "OPT32" in _scan_one(tmp_path, mixed)


def test_turbo_finding_cites_per_task_lines(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: turbo run build lint\n")
    _write_turbo(tmp_path, """{
  "tasks": {
    "build": { "cache": true },
    "lint": { "cache": true }
  }
}""")
    f = _finding(_scan(tmp_path), "OPT52")
    assert f and "(L" in f["evidence"]      # task names carry turbo.json line nums
    assert f["line"] > 1                     # anchored at the first offender


def test_opt19_table_has_sleep_line_numbers(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    t = tmp_path / "e2e"
    t.mkdir()
    (t / "a.spec.ts").write_text(
        "test('x', async ({page}) => {\n  await page.waitForTimeout(3000);\n});\n",
        encoding="utf-8")
    f = _finding(_scan(tmp_path), "OPT19")
    headers = f["measured_evidence"]["table"]["headers"]
    assert any("line" in h.lower() for h in headers)
    rows = f["measured_evidence"]["table"]["rows"]
    assert any("L2" in cell for row in rows for cell in row)  # waitForTimeout on line 2


def _scan_dir_with(tmp_path: Path, content: str, name: str = "ci.yml") -> dict:
    _write_workflow(tmp_path, name, content)
    return _scan(tmp_path)


# =============================================================================
# Coverage bookkeeping — the new detectors must register, dropping the
# without-detector count.
# =============================================================================

def test_phase2_patterns_have_detectors(tmp_path: Path):
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    data = _scan(tmp_path)
    without = set(data["catalog_patterns_without_detector"])
    # OPT18 is intentionally NOT auto-detected (manual-review only — service
    # mapping needs docker-compose.yml), so it is excluded here.
    for pat in ("OPT1", "OPT2", "OPT3", "OPT5", "OPT9", "OPT12", "OPT14",
                "OPT16", "OPT19", "OPT21", "OPT27", "OPT29", "OPT31",
                "OPT33", "OPT39", "OPT62", "OPT63", "OPT7", "OPT37", "OPT40",
                "OPT52", "OPT53", "OPT58", "OPT59", "OPT60"):
        assert pat not in without, f"{pat} should have a registered detector"


def test_structural_pattern_without_router_is_reported_not_hidden(tmp_path: Path):
    """A `structural` catalog entry with no critical-path router must be reported
    in catalog_structural_patterns_without_detector, never silently treated as
    covered. OPT74 (trust-boundary cache split) is catalogued for human
    application but has no auto-detector — so it must appear here, while the
    detected structural patterns (OPT70-73, 75) must NOT."""
    _write_workflow(tmp_path, "ci.yml", "name: x\non: push\njobs: {}\n")
    data = _scan(tmp_path)
    without = set(data["catalog_structural_patterns_without_detector"])
    assert "OPT74" in without, "OPT74 has no router and must be reported as uncovered"
    for pat in ("OPT70", "OPT71", "OPT72", "OPT73", "OPT75"):
        assert pat not in without, f"{pat} has a router and must not be listed"


def _catalog_section(pat: str) -> str:
    """The catalog body of one `### <PAT> — ...` section, up to the next `###`
    heading (or EOF — the last section in the file must not blow up the lint)."""
    catalog = (_SKILL_DIR / "references" / "optimization-patterns.md").read_text()
    start = catalog.index(f"### {pat} —")
    nxt = catalog.find("\n### ", start + 1)
    return catalog[start:] if nxt < 0 else catalog[start:nxt]


def _yaml_blocks(body: str) -> list[str]:
    blocks, cur = [], None
    for line in body.splitlines():
        if line.startswith("```yaml"):
            cur = []
        elif line.startswith("```") and cur is not None:
            blocks.append("\n".join(cur))
            cur = None
        elif cur is not None:
            cur.append(line)
    return blocks


def test_opt45_opt46_fix_recipes_never_prescribe_bare_cancel_in_progress():
    """Catalog lint: OPT45/OPT46 fire on `push`-triggered workflows, so their fix
    recipes must NEVER contain a bare `cancel-in-progress: true` — on a push to
    `main` or a release tag that cancels the in-flight run (a deploy, publish, or
    migration killed halfway). The cancellation must be an expression."""
    for pat in ("OPT45", "OPT46"):
        body = _catalog_section(pat)
        for line in body.splitlines():
            stripped = line.strip()
            # The prose may *name* the unsafe form (that's the warning); only a
            # YAML key assignment is a prescription.
            if stripped.startswith("cancel-in-progress:"):
                value = stripped.split(":", 1)[1].strip()
                assert value != "true", (
                    f"{pat} prescribes a bare `cancel-in-progress: true` — unsafe on "
                    f"the push/main/tag runs this pattern fires on. Use an expression."
                )
                assert value.startswith("${{") or value.startswith(">-"), (
                    f"{pat}'s cancel-in-progress must be an expression, got {value!r}"
                )
        assert "github.event_name" in body, (
            f"{pat}'s fix recipe must scope cancellation by event"
        )


def test_opt45_opt46_recipe_yaml_parses_with_a_single_line_expression():
    """Every `concurrency:` snippet in OPT45/OPT46 must load under a plain YAML
    parser AND yield a one-line `${{ ... }}` expression. A folded (`>-`) scalar
    whose continuation lines are MORE-indented than the first keeps its newlines;
    an expression carrying literal newlines depends on undocumented GHA lexer
    behavior, and if it were ever read as a plain string, a non-empty string is
    truthy → cancels on every event (the deploy-killing shape this recipe exists
    to prevent)."""
    yaml = pytest.importorskip("yaml")
    seen = 0
    for pat in ("OPT45", "OPT46"):
        for block in _yaml_blocks(_catalog_section(pat)):
            doc = yaml.safe_load(block)
            conc = (doc or {}).get("concurrency")
            if not isinstance(conc, dict) or "cancel-in-progress" not in conc:
                continue
            seen += 1
            cip = conc["cancel-in-progress"]
            assert isinstance(cip, str), (
                f"{pat}: cancel-in-progress loaded as {cip!r} ({type(cip).__name__}), "
                f"not an expression string")
            assert "\n" not in cip, (
                f"{pat}: folded scalar kept literal newlines inside the expression: {cip!r}")
            assert cip.startswith("${{") and cip.endswith("}}"), cip
            group = conc.get("group", "")
            assert "\n" not in str(group)
            # F2: the group key must unify push + pull_request on the SAME branch.
            # `head_ref` is the bare branch name; on `push` only `ref_name` is the
            # bare name (`github.ref` is `refs/heads/...` → a different group).
            if "github.head_ref" in str(group):
                assert "github.ref_name" in str(group), (
                    f"{pat}: `github.head_ref || github.ref` does NOT group a branch's "
                    f"push and PR runs together — use `github.ref_name`. Got: {group!r}")
    assert seen >= 3, f"expected OPT45's + OPT46's concurrency recipes, found {seen}"


def test_opt45_recipe_routes_push_only_workflows_to_the_widened_form():
    """F3: `_detect_opt45` fires on `pull_request` OR `push`. On a push-only
    workflow `${{ github.event_name == 'pull_request' }}` is never true — the
    block cancels nothing and the runner-minute saving is zero. OPT45's fix must
    say so mechanically (keyed on the trigger set), not leave it to judgment."""
    body = _catalog_section("OPT45")
    assert "no `pull_request` trigger" in body, (
        "OPT45's fix recipe must route push-only workflows to OPT46's widened form")
    assert "widened" in body and "OPT46" in body


# --- The cancel predicate, evaluated the way GitHub evaluates it ---------------
#
# A tiny evaluator for the GHA-expression subset the OPT45/OPT46 recipes use
# (`==`, `!=`, `&&`, `!`, `startsWith()`, `format()`, single-quoted literals,
# dotted context lookups). It lets the catalog's recipe be executed against real
# event contexts instead of only string-matched — so a predicate that would
# cancel a default-branch run FAILS the suite rather than passing a grep.

def _gha_resolve(tok: str, ctx: dict):
    tok = tok.strip()
    if tok.startswith("'") and tok.endswith("'"):
        return tok[1:-1]
    m = re.fullmatch(r"format\(\s*'([^']*)'\s*,\s*(.+?)\s*\)", tok)
    if m:
        return m.group(1).replace("{0}", str(_gha_resolve(m.group(2), ctx)))
    cur: object = ctx
    for part in tok.split("."):
        cur = cur.get(part, "") if isinstance(cur, dict) else ""
    return cur


def _gha_term(term: str, ctx: dict) -> bool:
    term = term.strip()
    neg = term.startswith("!") and not term.startswith("!=")
    if neg:
        term = term[1:].strip()
    m = re.fullmatch(r"startsWith\(\s*(.+?)\s*,\s*(.+?)\s*\)", term)
    if m:
        val = str(_gha_resolve(m.group(1), ctx)).startswith(
            str(_gha_resolve(m.group(2), ctx)))
    elif "!=" in term:
        a, b = term.split("!=", 1)
        val = _gha_resolve(a, ctx) != _gha_resolve(b, ctx)
    elif "==" in term:
        a, b = term.split("==", 1)
        val = _gha_resolve(a, ctx) == _gha_resolve(b, ctx)
    else:
        raise AssertionError(f"unsupported term in catalog recipe: {term!r}")
    return (not val) if neg else val


def _gha_eval(expr: str, ctx: dict) -> bool:
    body = expr.strip()
    assert body.startswith("${{") and body.endswith("}}"), expr
    body = body[3:-2]
    return all(_gha_term(t, ctx) for t in body.split("&&"))


def _ctx(event_name, *, ref, head_ref="", ref_name="", default_branch="main"):
    return {"github": {
        "event_name": event_name, "ref": ref, "head_ref": head_ref,
        "ref_name": ref_name or ref.rsplit("/", 1)[-1],
        "event": {"repository": {"default_branch": default_branch}},
    }}


def _cancel_predicates() -> dict[str, str]:
    """{label: expression} for every `concurrency:` recipe in OPT45/OPT46."""
    yaml = pytest.importorskip("yaml")
    out = {}
    for pat in ("OPT45", "OPT46"):
        for i, block in enumerate(_yaml_blocks(_catalog_section(pat))):
            conc = (yaml.safe_load(block) or {}).get("concurrency")
            if isinstance(conc, dict) and "cancel-in-progress" in conc:
                out[f"{pat}#{i}"] = str(conc["cancel-in-progress"])
    return out


def test_catalog_cancel_predicate_never_cancels_a_default_branch_run():
    """THE fork-PR hazard the unified group key opens up.

    The group key deliberately unifies a branch's `push` and `pull_request` runs
    (`head_ref || ref_name`, both BARE branch names) — that unification is what
    buys the OPT47 push+PR dedup. It also means a fork contributor who commits on
    **their fork's `main`** and opens a PR lands in group `<wf>-main`, the SAME
    group as the upstream repo's own `push: [main]` run. GitHub decides
    cancellation from the INCOMING run's `cancel-in-progress` and kills every
    in-progress run in the group regardless of THEIR settings — so an unguarded
    `github.event_name == 'pull_request'` predicate would cancel the in-flight
    push-to-`main` run. Same for a gitflow `main → develop` back-merge PR.

    Every catalog predicate is executed here against the hostile contexts. Drop
    the `github.head_ref != github.event.repository.default_branch` term from
    either recipe and this test fails."""
    preds = _cancel_predicates()
    assert len(preds) >= 3, preds
    must_not_cancel = {
        # The F1 hazard: a PR whose HEAD branch is the default branch.
        "fork PR opened from the fork's own `main`":
            _ctx("pull_request", ref="refs/pull/7/merge", head_ref="main"),
        "gitflow back-merge PR (`main` → `develop`)":
            _ctx("pull_request", ref="refs/pull/9/merge", head_ref="main"),
        "push to the default branch":
            _ctx("push", ref="refs/heads/main", ref_name="main"),
        # F2: same, on a repo whose default branch is NOT `main` — a hardcoded
        # 'refs/heads/main' term would let this through.
        "push to a `master` default branch":
            _ctx("push", ref="refs/heads/master", ref_name="master",
                 default_branch="master"),
        "push to a `develop` default branch":
            _ctx("push", ref="refs/heads/develop", ref_name="develop",
                 default_branch="develop"),
        "release tag build":
            _ctx("push", ref="refs/tags/v1.2.3", ref_name="v1.2.3"),
        "merge-queue run (cancelling it ejects the PR from the queue)":
            _ctx("merge_group", ref="refs/heads/gh-readonly-queue/main/pr-9-abc",
                 ref_name="gh-readonly-queue/main/pr-9-abc"),
    }
    for label, expr in preds.items():
        for what, ctx in must_not_cancel.items():
            assert _gha_eval(expr, ctx) is False, (
                f"{label} cancels an in-flight run for: {what}. Predicate: {expr}")


def test_catalog_cancel_predicate_still_cancels_what_it_is_supposed_to():
    """The other half of the guard: scoping the predicate must not zero out the
    saving. The DEFAULT (PR-scoped) form still cancels an ordinary superseded PR
    run; the WIDENED form still cancels superseded feature-branch pushes (the
    Flavor A case it exists for)."""
    preds = _cancel_predicates()
    pr_run = _ctx("pull_request", ref="refs/pull/3/merge", head_ref="feature/x")
    branch_push = _ctx("push", ref="refs/heads/feature/x", ref_name="feature/x")
    default_form = [e for e in preds.values() if "== 'pull_request'" in e]
    widened_form = [e for e in preds.values() if "refs/tags/" in e]
    assert default_form and widened_form, preds
    for expr in default_form:
        assert _gha_eval(expr, pr_run) is True, expr
    for expr in widened_form:
        assert _gha_eval(expr, branch_push) is True, expr
        assert _gha_eval(expr, pr_run) is True, expr


def test_opt46_widened_recipe_is_substitution_free():
    """F2: the widened predicate must not hardcode `refs/heads/main`. A repo whose
    default branch is `master`/`develop` copy-pastes the recipe verbatim (we hand
    it to an agent and tell it to 'apply the catalog's fix recipe'), and a
    hardcoded term would leave `cancel-in-progress: true` in all but name on that
    repo's OWN default branch — the original bug, restored. Derive the branch from
    `github.event.repository.default_branch` instead.

    Also: `github.ref_protected` must not be offered as a recipe. It is wrong in
    BOTH directions — an unprotected default branch reads `false` (→ cancelled),
    and repo rulesets targeting `~ALL` branches read `true` on feature branches
    (→ the predicate never fires and the saving is zero)."""
    for expr in _cancel_predicates().values():
        assert "'refs/heads/main'" not in expr, (
            f"hardcoded default branch in a catalog recipe: {expr}")
        assert "ref_protected" not in expr, (
            f"`github.ref_protected` is not a safe recipe (see docstring): {expr}")
    widened = [e for e in _cancel_predicates().values() if "refs/tags/" in e]
    assert widened, "OPT46's widened recipe disappeared"
    for expr in widened:
        assert "format('refs/heads/{0}', github.event.repository.default_branch)" in expr, expr
    body = _catalog_section("OPT46")
    assert "ruleset" in body.lower(), (
        "the catalog must flag the ref_protected inverse footgun (rulesets → "
        "ref_protected true on feature branches → zero saving)")


def test_opt43_queue_time_fix_cross_links_the_scoped_cancel_recipe():
    """F3: OPT43 (Excessive Queue Time) offers `cancel-in-progress` as one remedy for
    an over-restrictive concurrency group. It is `detector: manual`, so it rarely
    reaches a prompt — but it was the last place in the catalog from which an agent
    could infer the bare form. Its Fix must forbid the bare form and point at
    OPT45/OPT46's scoped recipes."""
    body = _catalog_section("OPT43")
    fix = body[body.index("**Fix**"):]
    assert "cancel-in-progress" in fix, "OPT43's fix no longer mentions cancellation"
    assert "OPT45" in fix and "OPT46" in fix, (
        "OPT43's fix must cross-link OPT45/OPT46's scoped recipes")
    assert "Never a bare `cancel-in-progress: true`" in fix, (
        "OPT43's fix must name the bare form as forbidden")


def test_opt45_opt46_name_the_group_key_vs_cancel_predicate_tension():
    """The two halves of the recipe pull against each other, and a future editor
    who only sees one half will break the other: unifying `push` + `pull_request`
    into ONE group is what buys the OPT47 dedup, and it is the SAME unification
    that lets a PR whose head branch is the default branch reach a default-branch
    run. Both sections must state the tension in prose — the executable guard
    above cannot tell an editor WHY the guard is there."""
    for pat in ("OPT45", "OPT46"):
        body = _catalog_section(pat)
        assert "OPT47" in body, (
            f"{pat} must say the unified group key is what buys the OPT47 dedup")
        assert "fork" in body.lower(), (
            f"{pat} must name the fork-PR-from-`main` hazard the unification opens")
        assert "github.head_ref != github.event.repository.default_branch" in body, (
            f"{pat} must name the term that closes it")




def test_coverage_honest_with_zero_parseable_workflows(tmp_path: Path):
    """No workflows at all must NOT report full coverage — the uncovered set
    (incl. the OPT13/OPT15 judgment pair) is a property of the catalog, not of
    what parsed."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    data = _scan(tmp_path)
    without = set(data["catalog_patterns_without_detector"])
    assert "OPT13" in without and "OPT15" in without
    assert without, "uncovered set must not be falsely empty on an empty repo"


def test_turbo_task_not_invoked_in_ci_is_not_flagged(tmp_path: Path):
    """A turbo task whose config nothing in CI runs through turbo (here `lint`,
    while CI only runs `turbo run build`) must NOT be flagged — its turbo.json
    config can't cost CI time (better-auth OPT58-on-lint false positive)."""
    _write_workflow(tmp_path, "ci.yml",
                    "name: x\non: push\njobs:\n  run:\n    runs-on: ubuntu-latest\n"
                    "    steps:\n      - run: turbo run build\n")
    _write_turbo(tmp_path, """{
  "tasks": {
    "build": { "cache": true, "outputs": ["dist/**"], "inputs": ["src/**"] },
    "lint": { "cache": true }
  }
}""")
    fired = _patterns(_scan(tmp_path))
    assert "OPT58" not in fired   # lint isn't run via turbo in CI → not flagged
    assert "OPT52" not in fired   # build is well-configured


# =============================================================================
# Declarative (regex `match:`) detectors — OPT20/26/41/55. These four are
# catalog-param-driven (no bespoke handler), so they exercise the shared
# _declarative_hits regex path. One positive + one negative each, so a broken
# catalog regex or a regression in the declarative dispatch is caught here.
# =============================================================================

def test_opt20_image_latest_tag(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      db:
        image: postgres:latest
    steps:
      - run: pytest
"""
    assert "OPT20" in _scan_one(tmp_path, pos)


def test_opt20_negative_pinned_image(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      db:
        image: postgres:16
    steps:
      - run: pytest
"""
    assert "OPT20" not in _scan_one(tmp_path, neg)


def test_opt26_outdated_action_major(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: pytest
"""
    assert "OPT26" in _scan_one(tmp_path, pos)


def test_opt26_negative_current_action_major(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest
"""
    assert "OPT26" not in _scan_one(tmp_path, neg)


def test_opt41_turbo_force_true(tmp_path: Path):
    pos = """name: CI
on: push
env:
  TURBO_FORCE: true
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: turbo run build
"""
    assert "OPT41" in _scan_one(tmp_path, pos)


def test_opt41_negative_no_turbo_force(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: turbo run build
"""
    assert "OPT41" not in _scan_one(tmp_path, neg)


def test_opt55_vitest_watch_in_ci(tmp_path: Path):
    pos = """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: vitest --watch
"""
    assert "OPT55" in _scan_one(tmp_path, pos)


def test_opt55_negative_vitest_run(tmp_path: Path):
    neg = """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: vitest run
"""
    assert "OPT55" not in _scan_one(tmp_path, neg)


def test_opt28_skips_dispatch_only_workflow(tmp_path: Path):
    """A workflow_dispatch-only helper (mastra vitest-all) isn't dev-facing CI —
    it runs ~0×/mo, so its fetch-depth:0 is not a ranked OPT28 finding (M8)."""
    wf = """name: vitest-all
on:
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - run: vitest run
"""
    assert "OPT28" not in _scan_one(tmp_path, wf, name="vitest-all.yml")


def test_opt28_still_fires_on_workflow_call_child(tmp_path: Path):
    """A workflow_call child runs on every PR via its caller — OPT28 still applies."""
    wf = """name: e2e
on:
  workflow_call:
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - run: pnpm e2e
"""
    assert "OPT28" in _scan_one(tmp_path, wf, name="e2e-tests.yml")


def test_opt33_evidence_is_paths_aware(tmp_path: Path):
    """A `paths:`-filtered workflow does NOT run on every PR — OPT33's evidence
    must say so, not claim "every PR" (M7: e2e-docs is `paths: ['docs/**']`)."""
    wf = """name: e2e-docs
on:
  pull_request:
    paths: ['docs/**']
jobs:
  e2e-docs:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test:docs
"""
    _write_workflow(tmp_path, "e2e-docs.yml", wf)
    f = [x for x in _scan(tmp_path)["findings"] if x["pattern"] == "OPT33"]
    assert f and "filtered `paths:`" in f[0]["evidence"]
    assert "runs on every PR including drafts" not in f[0]["evidence"]


def test_workflow_call_graph_maps_caller_to_reusable_children(tmp_path: Path):
    """The scan emits a workflow_call graph: a job-level
    `uses: ./.github/workflows/X.yml` records caller→child, so downstream sizing
    can attribute a reusable child's frequency to its caller (mastra: prebuild →
    e2e-tests/test-suite/…). A step-level `uses:` (a composite action) is NOT a
    reusable-workflow call and must not appear."""
    caller = """name: prebuild
on:
  pull_request:
jobs:
  changes:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/setup    # composite action — NOT a wf call
  e2e:
    needs: changes
    uses: ./.github/workflows/e2e-tests.yml
  unit:
    uses: ./.github/workflows/test-suite.yml@main
"""
    child = """name: e2e-tests
on:
  workflow_call:
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm e2e
"""
    _write_workflow(tmp_path, "prebuild.yml", caller)
    _write_workflow(tmp_path, "e2e-tests.yml", child)
    _write_workflow(tmp_path, "test-suite.yml", child)
    graph = _scan(tmp_path).get("workflow_call_graph", {})
    assert graph.get(".github/workflows/prebuild.yml") == [
        ".github/workflows/e2e-tests.yml",
        ".github/workflows/test-suite.yml",
    ]
    # A reusable child doesn't call anything → not a caller key.
    assert ".github/workflows/e2e-tests.yml" not in graph


def test_workflow_job_graph_normalizes_needs_name_and_reusable(tmp_path: Path):
    """`_build_workflow_job_graph` feeds collect_runs' required-reachability filter, so a
    wrong graph silently mis-scopes the critical-path pole. Pin every normalization branch
    end-to-end (through real scan + YAML parse): `needs:` as a bare string vs a list vs
    missing/null, a missing `name:` falling back to the job id, and `reusable` detection
    from a job-level `uses:`. An empty-jobs workflow contributes no graph entry."""
    wf = """name: CI
on:
  pull_request:
jobs:
  changes:
    runs-on: ubuntu-latest
    steps:
      - run: echo detect
  build:
    name: Build
    needs: changes                       # bare string -> ["changes"]
    timeout-minutes: 30
    runs-on: ubuntu-latest
    steps:
      - run: echo build
  test:
    name: UNIT Test (Shard ${{ matrix.shard }})
    needs: [changes, build]              # list -> kept as-is
    strategy:
      matrix:
        shard: [1, 2]                    # strategy.matrix -> matrix: True
    runs-on: ubuntu-latest
    steps:
      - run: echo test
  lint:                                  # no name: -> falls back to job id "lint"
    runs-on: ubuntu-latest
    steps:
      - run: echo lint
  suite:
    name: Suite
    needs: build
    uses: ./.github/workflows/suite.yml  # job-level uses -> reusable: True
"""
    child = """name: suite
on:
  workflow_call:
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: echo suite
"""
    empty = """name: empty
on:
  pull_request:
jobs: {}
"""
    _write_workflow(tmp_path, "ci.yml", wf)
    _write_workflow(tmp_path, "suite.yml", child)
    _write_workflow(tmp_path, "empty.yml", empty)
    graph = _scan(tmp_path).get("workflow_job_graph", {})
    ci = graph.get(".github/workflows/ci.yml")
    assert ci is not None
    assert ci["changes"] == {"name": "changes", "needs": [], "reusable": False, "matrix": False, "timeout_minutes": False}  # missing needs -> []; no name -> job id; no strategy.matrix
    assert ci["build"] == {"name": "Build", "needs": ["changes"], "reusable": False, "matrix": False, "timeout_minutes": True}  # bare string normalized
    assert ci["test"]["needs"] == ["changes", "build"]                                    # list preserved
    assert ci["test"]["name"] == "UNIT Test (Shard ${{ matrix.shard }})"                  # matrix placeholder kept intact
    assert ci["test"]["matrix"] is True                                                   # strategy.matrix -> matrix flag
    assert ci["test"]["timeout_minutes"] is False                                         # no timeout-minutes -> false
    assert ci["lint"]["name"] == "lint"                                                   # name falls back to job id
    assert ci["lint"]["matrix"] is False                                                  # no strategy block -> not matrix
    assert ci["lint"]["timeout_minutes"] is False                                         # no timeout-minutes -> false
    assert ci["suite"]["reusable"] is True and ci["suite"]["needs"] == ["build"]          # job-level uses -> reusable
    assert ".github/workflows/empty.yml" not in graph                                     # empty-jobs file omitted


# --- Activation-fidelity class (OPT33/39/40 "runs on every PR" claims) ------------------------------
def _import_scan_module():
    import importlib.util
    name = "ci_speedup_scan_activation"
    spec = importlib.util.spec_from_file_location(name, _SCAN_SCRIPT)
    scan = importlib.util.module_from_spec(spec)
    sys.modules[name] = scan  # register first: scan.py's @dataclass resolves __module__ here
    spec.loader.exec_module(scan)
    return scan


def test_opt33_not_fired_on_types_labeled_gated_job(tmp_path: Path):
    # CLASS bug (razorpay/blade `interaction-tests`): a matrix job that runs ONLY when a label is added
    # (trigger `pull_request: types: [labeled]`), NOT on a normal PR open/update. OPT33 claiming it
    # "runs on every PR including drafts" is a factual error — the activation-fidelity class.
    types_gated = """name: Interaction Tests
on:
  pull_request:
    types: [labeled]
jobs:
  interaction-tests:
    runs-on: ubuntu-latest
    if: github.event.label.name == 'Run Interaction Tests'
    strategy:
      matrix:
        shard: [1, 2, 3]
    steps:
      - run: pnpm test:interaction
"""
    assert "OPT33" not in _scan_one(tmp_path, types_gated)


def test_opt33_not_fired_on_if_label_gate_even_with_lifecycle_types(tmp_path: Path):
    # The job's own `if:` activity gate suppresses even when the trigger admits lifecycle types.
    if_gated = """name: CI
on:
  pull_request:
    types: [opened, synchronize, labeled]
jobs:
  test:
    runs-on: ubuntu-latest
    if: github.event.action == 'labeled'
    strategy:
      matrix:
        go: ['1.21', '1.22']
    steps:
      - run: go test ./...
"""
    assert "OPT33" not in _scan_one(tmp_path, if_gated)


def test_opt33_still_fires_on_normal_pr_matrix(tmp_path: Path):
    # Positive control: a normal PR matrix (default lifecycle types, no activity `if:`) STILL fires —
    # the class fix must not over-suppress.
    normal = """name: CI
on:
  pull_request:
    types: [opened, synchronize, reopened]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        go: ['1.21', '1.22']
    steps:
      - run: go test ./...
"""
    assert "OPT33" in _scan_one(tmp_path, normal)


def test_pr_activation_fidelity_shared_predicate():
    # The shared predicate every "every PR" detector (OPT33/39/40) gates on — the CLASS guard. Pins the
    # workflow-level `types:` logic AND the per-job `if:` activity gate in one place.
    scan = _import_scan_module()
    every, job = scan._pr_trigger_runs_every_pr, scan._job_runs_on_every_pr
    # Workflow-level: absent/lifecycle types → every PR; an activity-only `types:` → NOT every PR.
    assert every("pull_request") is True
    assert every(["pull_request", "push"]) is True
    assert every({"pull_request": None}) is True                                  # bare `pull_request:`
    assert every({"pull_request": {"types": ["opened", "synchronize"]}}) is True
    assert every({"pull_request": {"types": ["labeled", "synchronize"]}}) is True  # has a lifecycle type
    assert every({"pull_request": {"types": ["labeled"]}}) is False
    assert every({"pull_request": {"types": "labeled"}}) is False                 # scalar form
    assert every({"push": {}}) is False                                          # no pull_request at all
    # GitHub's default lifecycle is EXACTLY {opened, synchronize, reopened} — `ready_for_review` / `edited`
    # do NOT fire on a normal commit-push, so a workflow gated to only those does NOT run on every PR.
    assert every({"pull_request": {"types": ["ready_for_review"]}}) is False
    assert every({"pull_request": {"types": ["edited"]}}) is False
    # Per-job: the job's own `if:` activity gate suppresses on top of the trigger; a DRAFT gate does not.
    on = {"pull_request": {"types": ["opened", "synchronize"]}}
    assert job(on, {}) is True
    assert job(on, {"if": "github.event.label.name == 'Run X'"}) is False
    assert job(on, {"if": "github.event.action == 'labeled'"}) is False
    assert job(on, {"if": "github.event.pull_request.draft == false"}) is True   # draft gate is NOT an activity gate
    assert job({"pull_request": {"types": ["labeled"]}}, {}) is False            # trigger gate alone suppresses
    # ANY non-lifecycle `github.event.action ==` gate suppresses (not just `labeled`): assigned,
    # review_requested, ready_for_review — while a LIFECYCLE action (synchronize) does NOT.
    assert job(on, {"if": "github.event.action == 'assigned'"}) is False
    assert job(on, {"if": "github.event.action == 'review_requested'"}) is False
    assert job(on, {"if": "github.event.action == 'ready_for_review'"}) is False
    assert job(on, {"if": "github.event.action == 'synchronize'"}) is True       # a lifecycle action ≠ activity gate
    # The common label opt-in idiom `contains(github.event.pull_request.labels.*.name, 'ci')` gates the
    # job to labeled PRs → not every PR.
    assert job(on, {"if": "contains(github.event.pull_request.labels.*.name, 'ci')"}) is False
    # Reversed operand order is also caught.
    assert job(on, {"if": "'labeled' == github.event.action"}) is False
    # `!=` is intentionally NOT an activity gate — `action != 'closed'` still runs on normal PRs.
    assert job(on, {"if": "github.event.action != 'closed'"}) is True


def test_opt39_suppressed_on_types_labeled_trigger(tmp_path: Path):
    # CLASS-fix routing guard: OPT39 (ungated `language` matrix) is routed through the shared
    # activation-fidelity predicate, so a `pull_request: types: [labeled]` workflow — whose legs do NOT
    # run on every PR — must NOT fire OPT39. (Without the routing, mutation testing showed the old gate
    # let it through; this pins the wire.)
    types_gated = """name: CodeQL
on:
  pull_request:
    types: [labeled]
jobs:
  scan:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        language: [python, javascript]
    steps:
      - uses: github/codeql-action/analyze@v3
"""
    assert "OPT39" not in _scan_one(tmp_path, types_gated)


def test_opt40_suppressed_on_types_labeled_trigger(tmp_path: Path):
    # CLASS-fix routing guard for OPT40 (monorepo single-app job): a `types: [labeled]` trigger does not
    # run on every PR, so the "targets every PR" claim is false and OPT40 must NOT fire.
    (tmp_path / "apps").mkdir()
    (tmp_path / "turbo.json").write_text("{}", encoding="utf-8")
    types_gated = """name: CI
on:
  pull_request:
    types: [labeled]
jobs:
  web-quality:
    runs-on: ubuntu-latest
    steps:
      - run: cd apps/web && pnpm playwright test
"""
    assert "OPT40" not in _scan_one(tmp_path, types_gated)


# =============================================================================
# Catalog removal discipline — a cut/removed pattern keeps a retired-id stub;
# the shipped "N-pattern" doc claims stay reconciled with the real count.
# =============================================================================

_CATALOG_PATH = _SKILL_DIR / "references" / "optimization-patterns.md"


def test_opt66_stays_retired_not_silently_deleted():
    """OPT66 (SKU-arbitrage ceiling) was a dollar-only pattern removed by the
    2026-07-20 pricing excision. The catalog's retired-id rule forbids silently
    dropping (or reusing) an id — a removed pattern must keep a stub, like the
    OPT49/50/51 CUTs — so historical reports/evals/fix-strategy strings never
    collide. This guards against OPT66 vanishing (which also silently staled
    every '74-pattern' doc claim, see the count test below)."""
    import sys as _sys
    _sys.path.insert(0, str(_SKILL_DIR / "scripts"))
    import scan  # noqa: E402

    text = _CATALOG_PATH.read_text(encoding="utf-8")
    assert "### OPT66" in text, (
        "OPT66 must keep a retired-id stub in the catalog, not be silently deleted")
    stub = re.search(r"### OPT66.*?(?=\n### OPT|\Z)", text, re.S)
    assert stub and "REMOVED" in stub.group(0), (
        "the OPT66 stub must carry a REMOVED/CUT marker naming the pricing excision")
    ents = {e.pattern: e for e in scan.load_catalog(_CATALOG_PATH)}
    assert "OPT66" in ents, "OPT66 must parse as a catalog entry (keeps the id in the count)"
    assert ents["OPT66"].detector == "manual", (
        "the removed OPT66 must not re-declare a live detector — its detector was "
        "deleted with billing.py")


def test_catalog_pattern_count_matches_doc_claims():
    """Every '<N>-pattern' / 'all <N> patterns' claim in the shipped docs must use
    the REAL current catalog count. OPT66's removal-as-a-retired-stub keeps that
    count at 74; a silent delete drops it to 73 and staled five claims at once
    (SKILL.md, ARCHITECTURE.md, evals.json). This is the guard that was missing."""
    import sys as _sys
    _sys.path.insert(0, str(_SKILL_DIR / "scripts"))
    import scan  # noqa: E402

    count = len(scan.load_catalog(_CATALOG_PATH))
    for rel in ("SKILL.md", "ARCHITECTURE.md", "evals/evals.json"):
        text = (_SKILL_DIR / rel).read_text(encoding="utf-8")
        for claimed in re.findall(r"(\d+)[- ]pattern", text):
            assert int(claimed) == count, (
                f"{rel} claims a {claimed}-pattern catalog but the real count is "
                f"{count} — reconcile the doc with optimization-patterns.md")


def test_catalog_pattern_count_breakdown_sums_to_the_total():
    """The headline count is followed, in both shipped docs, by a hygiene +
    structural breakdown. Bumping only the total leaves the very sentence a
    reader uses to check the number contradicting itself — which is exactly what
    the total-only guard above cannot see."""
    import sys as _sys
    _sys.path.insert(0, str(_SKILL_DIR / "scripts"))
    import scan  # noqa: E402

    count = len(scan.load_catalog(_CATALOG_PATH))
    breakdown = re.compile(
        r"(\d+)[- ]pattern\s*\n?catalog\s*[—-]\s*(\d+)\s+\*\*hygiene/data-driven\*\*"
        r".*?plus\s+(\d+)\s+\*\*structural",
        re.S)
    for rel in ("SKILL.md", "ARCHITECTURE.md"):
        text = (_SKILL_DIR / rel).read_text(encoding="utf-8")
        m = breakdown.search(text)
        assert m, f"{rel} no longer states a hygiene + structural breakdown to check"
        total, hygiene, structural = (int(g) for g in m.groups())
        assert total == count, f"{rel} headline count {total} != real count {count}"
        assert hygiene + structural == total, (
            f"{rel} breaks down its {total}-pattern catalog as {hygiene} hygiene + "
            f"{structural} structural = {hygiene + structural} — the breakdown must "
            f"sum to the total")


# =============================================================================
# OPT76 — Submodule / Git LFS Checkout Payload
# =============================================================================

_GITMODULES = """[submodule "vendor/protos"]
\tpath = vendor/protos
\turl = https://github.com/example/protos.git
"""

_GITATTRIBUTES = "*.psd filter=lfs diff=lfs merge=lfs -text\n"


def _write_repo_file(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8")


def test_opt76_fires_on_submodule_checkout_no_step_reads_it(tmp_path: Path):
    """A PR-gating job that clones every submodule but never references the
    submodule path pays the clone on every run."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    pos = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
      - run: pnpm install && pnpm test
"""
    assert "OPT76" in _scan_one(tmp_path, pos)


def test_opt76_suppressed_when_a_step_builds_from_the_submodule(tmp_path: Path):
    """The submodule payload is LOAD-BEARING when a step reads it — dropping
    `submodules:` would break the job, so OPT76 must NOT fire."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    neg = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true
      - run: make -C vendor/protos generate && pnpm build
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_suppressed_when_no_gitmodules_declares_a_path(tmp_path: Path):
    """With no `.gitmodules` in the checkout we can't name a submodule the job
    fails to read — fail CLOSED rather than assert unread payload we never saw."""
    neg = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
      - run: pnpm test
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_fails_closed_on_unreadable_local_action(tmp_path: Path):
    """A local composite action whose file can't be read may itself read the
    submodule — suppress rather than recommend a payload removal that breaks it."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    neg = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
      - uses: ./.github/actions/mystery
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_sees_submodule_use_inside_a_local_composite_action(tmp_path: Path):
    """The step that reads the submodule can live in a local composite action —
    resolve it, and suppress the finding just as if it were in the workflow."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    act = tmp_path / ".github" / "actions" / "gen"
    act.mkdir(parents=True, exist_ok=True)
    (act / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - run: make -C vendor/protos generate\n"
        "      shell: bash\n", encoding="utf-8")
    neg = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
      - uses: ./.github/actions/gen
      - run: pnpm test
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_fires_on_lfs_checkout_no_step_reads_a_tracked_path(tmp_path: Path):
    _write_repo_file(tmp_path, ".gitattributes", _GITATTRIBUTES)
    pos = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          lfs: true
      - run: pnpm test
"""
    assert "OPT76" in _scan_one(tmp_path, pos)


def test_opt76_fires_on_git_lfs_pull_in_a_run_block(tmp_path: Path):
    _write_repo_file(tmp_path, ".gitattributes", _GITATTRIBUTES)
    pos = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: git lfs pull
      - run: pnpm test
"""
    assert "OPT76" in _scan_one(tmp_path, pos)


def test_opt76_suppressed_when_a_step_reads_an_lfs_tracked_path(tmp_path: Path):
    _write_repo_file(tmp_path, ".gitattributes", _GITATTRIBUTES)
    neg = """name: CI
on: pull_request
jobs:
  render:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          lfs: true
      - run: node scripts/render.js assets/logo.psd
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_ignores_dispatch_only_helper_workflows(tmp_path: Path):
    """A `workflow_dispatch`-only helper isn't dev-facing CI (runs ~0x/mo), so
    its checkout payload is noise, not a ranked optimization (OPT28's scope)."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    neg = """name: helper
on: workflow_dispatch
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
      - run: pnpm test
"""
    assert "OPT76" not in _scan_one(tmp_path, neg, name="helper.yml")


def test_opt76_evidence_names_the_declared_payload_and_anchors_its_job(tmp_path: Path):
    """The finding must cite the submodule path it read from `.gitmodules` and
    anchor on the flagged job's OWN `submodules:` line, not a file-global match."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    wf = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
      - run: make -C vendor/protos generate
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
      - run: pnpm test
"""
    _write_workflow(tmp_path, "ci.yml", wf)
    hits = [f for f in _scan(tmp_path)["findings"] if f["pattern"] == "OPT76"]
    # Only `test` is flagged; `build` genuinely reads the submodule.
    assert [f["affected_jobs"] for f in hits] == [["test"]]
    assert "vendor/protos" in hits[0]["evidence"]
    sub_lines = [i + 1 for i, ln in enumerate(wf.splitlines())
                 if ln.strip() == "submodules: recursive"]
    assert hits[0]["line"] == sub_lines[1]  # test's line, not build's


# --- OPT76 regressions: the evidence must match what was actually checked ----


def test_opt76_does_not_call_git_lfs_checkout_a_download(tmp_path: Path):
    """`git lfs checkout` populates the working tree from objects ALREADY local
    — it downloads nothing. Flagging it would assert a network payload the
    detector never established, and the catalog's own recipe greps only for
    `git lfs pull` / `git lfs fetch`."""
    _write_repo_file(tmp_path, ".gitattributes", _GITATTRIBUTES)
    neg = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: git lfs checkout
      - run: pnpm test
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_resolves_a_local_action_nested_inside_a_local_action(tmp_path: Path):
    """A composite action may invoke ANOTHER local action, and that inner one
    may be the step that reads the payload. Following only one level makes the
    job look clean and recommends a removal that breaks the build."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    outer = tmp_path / ".github" / "actions" / "build"
    outer.mkdir(parents=True, exist_ok=True)
    (outer / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: ./.github/actions/inner\n",
        encoding="utf-8")
    inner = tmp_path / ".github" / "actions" / "inner"
    inner.mkdir(parents=True, exist_ok=True)
    (inner / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n"
        "    - run: make -C vendor/protos generate\n      shell: bash\n",
        encoding="utf-8")
    neg = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true
      - uses: ./.github/actions/build
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_fails_closed_when_a_nested_local_action_is_unreadable(tmp_path: Path):
    """The fail-closed stance has to survive one level down too: an inner action
    we cannot read may be the payload's reader."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    outer = tmp_path / ".github" / "actions" / "build"
    outer.mkdir(parents=True, exist_ok=True)
    (outer / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: ./.github/actions/mystery\n",
        encoding="utf-8")
    neg = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true
      - uses: ./.github/actions/build
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_ignores_a_checkout_of_a_different_repository(tmp_path: Path):
    """`repository:` clones SOMEONE ELSE's tree, whose submodules this repo's
    `.gitmodules` says nothing about. Naming our declared paths as the unread
    payload would be a claim about data the scanner never saw."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    neg = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          repository: other/other-repo
          submodules: recursive
          path: other
      - run: make -C other all
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_ignores_commented_and_negated_gitattributes_lines(tmp_path: Path):
    """A commented `.gitattributes` line must not become the hint `#`, which
    appears in almost every run block and would silently switch the whole LFS
    half of the pattern off. A `-filter=lfs` unset is not a declaration either."""
    _write_repo_file(
        tmp_path, ".gitattributes",
        "# *.bin filter=lfs diff=lfs -text\n"
        "*.log -filter=lfs\n"
        "*.psd filter=lfs diff=lfs merge=lfs -text\n")
    pos = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          lfs: true
      - run: |
          # build the thing
          pnpm test
"""
    assert "OPT76" in _scan_one(tmp_path, pos)


def test_opt76_reads_quoted_and_dot_prefixed_gitmodules_paths(tmp_path: Path):
    """A path with a space is quoted in `.gitmodules`, and `./`-prefixed paths
    are legal. Dropping them silently shrinks the declared payload, so the
    evidence enumerates an incomplete declaration and fires on a job that does
    read the submodule."""
    _write_repo_file(
        tmp_path, ".gitmodules",
        '[submodule "assets"]\n\tpath = "assets/big data"\n'
        '\turl = https://example.invalid/a.git\n'
        '[submodule "vendor"]\n\tpath = ./vendor/protos\n'
        '\turl = https://example.invalid/b.git\n')
    neg = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true
      - run: make -C "assets/big data" all
      - run: make -C vendor/protos generate
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_anchors_on_the_checkout_that_actually_pulls_the_payload(tmp_path: Path):
    """Two checkouts in one job: the snippet the report renders as verbatim
    proof must be the `submodules: true` line, never the `submodules: false`
    line that happens to come first."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    wf = """name: CI
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: false
      - uses: actions/checkout@v4
        with:
          submodules: true
      - run: pnpm test
"""
    _write_workflow(tmp_path, "ci.yml", wf)
    hits = [f for f in _scan(tmp_path)["findings"] if f["pattern"] == "OPT76"]
    assert len(hits) == 1
    assert "submodules: true" in hits[0]["evidence_snippet"]
    assert "false" not in hits[0]["evidence_snippet"]


def test_opt76_does_not_fire_on_yaml_truthy_submodules_yes(tmp_path: Path):
    """PyYAML resolves `yes` to True; the runner does not — actions/checkout
    enables submodules only for `TRUE`/`RECURSIVE`, so `submodules: yes` clones
    nothing. Firing would flag a payload that is never pulled, and quote a
    `submodules: true` that is not in the file."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    neg = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: yes
      - run: pnpm test
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_sees_paths_named_in_job_defaults_matrix_and_step_if(tmp_path: Path):
    """These references are in the job's own YAML — the very text the evidence
    claims to have searched. Missing them recommends dropping a payload the job
    demonstrably uses."""
    _write_repo_file(tmp_path, ".gitmodules", _GITMODULES)
    for wf in (
        """name: CI
on: pull_request
jobs:
  a:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: vendor/protos
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true
      - run: make all
""",
        """name: CI
on: pull_request
jobs:
  b:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        pkg: [vendor/protos]
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true
      - run: make -C ${{ matrix.pkg }} generate
""",
        """name: CI
on: pull_request
jobs:
  c:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true
      - name: build vendor/protos
        if: hashFiles('vendor/protos/**') != ''
        run: make all
""",
    ):
        assert "OPT76" not in _scan_one(tmp_path, wf)


def test_opt76_reports_one_finding_per_job_for_one_lfs_payload(tmp_path: Path):
    """`lfs: true` and `git lfs pull` in the same job download the SAME objects
    once. Two findings would double-count one payload in the ranked list."""
    _write_repo_file(tmp_path, ".gitattributes", _GITATTRIBUTES)
    wf = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          lfs: true
      - run: git lfs pull
      - run: pnpm test
"""
    _write_workflow(tmp_path, "ci.yml", wf)
    hits = [f for f in _scan(tmp_path)["findings"] if f["pattern"] == "OPT76"]
    assert len(hits) == 1


def test_opt76_matches_declared_paths_case_insensitively(tmp_path: Path):
    """Git path matching is effectively case-insensitive on the macOS/Windows
    checkouts these workflows run against, so a step naming `assets/LOGO.PSD`
    reads the `*.psd` payload."""
    _write_repo_file(tmp_path, ".gitattributes", _GITATTRIBUTES)
    neg = """name: CI
on: pull_request
jobs:
  render:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          lfs: true
      - run: node scripts/render.js assets/LOGO.PSD
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_suppressed_when_a_git_lfs_run_step_job_reads_a_tracked_path(tmp_path: Path):
    """The run-block branch needs its OWN suppression case: the `lfs: true`
    negatives all go through the `with:`-key path, so a mutant that drops the
    "no step reads a tracked path" condition from the `git lfs pull` branch
    alone leaves the suite green while the detector fires on a job whose whole
    purpose is reading the payload it just pulled."""
    _write_repo_file(tmp_path, ".gitattributes", _GITATTRIBUTES)
    neg = """name: CI
on: pull_request
jobs:
  render:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: git lfs pull
      - run: node scripts/render.js assets/logo.psd
"""
    assert "OPT76" not in _scan_one(tmp_path, neg)


def test_opt76_run_block_anchors_on_the_downloading_git_lfs_command(tmp_path: Path):
    """`git lfs install` (and `git lfs checkout`) download nothing — that is why
    the run-block branch only fires on `pull`/`fetch`. Anchoring the finding on
    the first `git lfs` line in the job pastes a non-downloading setup command as
    the verbatim proof of a network payload, so the snippet must be the
    `pull`/`fetch` line the detector actually matched."""
    _write_repo_file(tmp_path, ".gitattributes", _GITATTRIBUTES)
    wf = """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: git lfs install
      - run: git lfs checkout
      - run: git lfs pull
      - run: pnpm test
"""
    _write_workflow(tmp_path, "ci.yml", wf)
    hits = [f for f in _scan(tmp_path)["findings"] if f["pattern"] == "OPT76"]
    assert len(hits) == 1
    assert "git lfs pull" in hits[0]["evidence_snippet"]
    assert "install" not in hits[0]["evidence_snippet"]

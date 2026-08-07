"""Scanner-substrate guards for the behavioral eval fixtures added by the
ci-secure self-improvement loop.

``evals/evals.json`` cases 2-4 are graded by the behavioral eval harness
(an agent runs the whole skill), which ``pytest`` does not execute. These
tests pin the *deterministic* scanner output each of those cases depends on,
so a detector regression that would invalidate an eval is caught by
``pytest -v`` in CI — not silently discovered the next time the harness runs.

Each test runs ``scripts/scan.py`` as a subprocess (mirroring real
invocation) against the matching ``evals/files/<slug>/`` fixture.

Run from the skill root:

    python -m pytest skills/ci-secure/tests/test_loop_evals.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCAN_SCRIPT = _SKILL_DIR / "scripts" / "scan.py"
_EVAL_FILES = _SKILL_DIR / "evals" / "files"


def _scan(slug: str) -> dict:
    """Run scan.py against evals/files/<slug>/ and return the parsed JSON.

    Skips if PyYAML is absent, matching test_scan.py's behavior on a runner
    without the scanner's one third-party dependency installed. (scan.py and
    its helpers carry ``from __future__ import annotations``, so they run on
    the same interpreters test_scan.py supports — no separate version gate.)
    """
    if subprocess.run(
        [sys.executable, "-c", "import yaml"],
        capture_output=True, text=True, timeout=60,
    ).returncode != 0:
        pytest.skip("PyYAML not installed in the test runner")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(_EVAL_FILES / slug),
         "--gh-impostor", "off"],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return json.loads(result.stdout)


def _patterns_for_file(data: dict, basename: str) -> set[str]:
    """Patterns that fired on a specific workflow file (excludes repo-wide)."""
    return {
        f["pattern"] for f in data["findings"]
        if f.get("workflow_file", "").endswith(basename)
    }


# --- eval 3: the P14.9 pair must discriminate ------------------------------

def test_pwn_request_p14_9_fires_on_vulnerable_only() -> None:
    """P14.9 must fire on the pull_request_target + head-checkout + exec
    chain (vulnerable.yml) and NOT on the identical build under plain
    pull_request (safe.yml).

    Backs evals.json case 3. If the detector relaxes to trigger-presence, the
    safe file starts firing and the eval's negative is invalidated; if it
    breaks, the vulnerable file goes silent. Both directions pin here.
    """
    data = _scan("pwn-request")
    assert "P14.9" in _patterns_for_file(data, "vulnerable.yml"), (
        "P14.9 should fire on the untrusted-trigger + head-checkout + exec chain"
    )
    assert _patterns_for_file(data, "safe.yml") == set(), (
        "safe.yml (plain pull_request) must produce no findings"
    )


def test_clean_fixture_produces_zero_findings() -> None:
    """Backs evals.json case 2: zero findings is a first-class result, and
    the clean fixture must actually be clean under the ten-vector catalog —
    the loud gh_checks skip note must still be present (a skipped
    network-gated check is never silently dropped from the output)."""
    data = _scan("clean")
    assert data["findings"] == []
    assert data["gh_checks"]["P14.11"].startswith("skipped:")


# --- eval 5: many distinct findings exceed a fixed-option prompt's cap -------

def test_many_findings_exceeds_capped_prompt() -> None:
    """The many-findings fixture must trip well past four distinct pattern
    groups, so the selection step cannot be represented by a 4-option widget.

    Backs evals.json case 4. The behavioral eval asserts the skill presents a
    full numbered free-text table rather than a capped multiple-choice prompt;
    that assertion is only meaningful while the fixture genuinely produces
    more groups than the cap. This pins the >= 6 substrate.

    Note: ci-secure groups findings by pattern (one ``### Finding N`` per
    pattern), so distinct-pattern count is the group count the selection menu
    uses. If grouping ever stops being per-pattern, revisit this proxy.
    """
    data = _scan("many-findings")
    distinct = {f["pattern"] for f in data["findings"]}
    assert len(distinct) >= 6, (
        f"many-findings fixture should produce >= 6 distinct pattern groups, "
        f"got {len(distinct)}: {sorted(distinct)}"
    )


# --- the false-clean path: a failed scan must NOT look like an empty-clean one

def test_scanner_signals_unscannable_root_via_nonzero_exit(tmp_path: Path) -> None:
    """scan.py must FAIL loudly on a root it can't scan — non-zero exit and no
    findings document on stdout — rather than emitting empty-but-valid output
    that a caller could read as "clean".

    This is the deterministic half of the Phase 2 / NEVER-rule contract that
    "empty or failed scanner output is a coverage failure, not a clean
    result." A regression that made scan.py print ``{"findings": []}`` and
    exit 0 on an unscannable root would let a false-clean slip past the
    orchestrator's "non-zero exit => stop" check; this catches it at the
    scanner layer. tmp_path has no ``.github/workflows`` directory.
    """
    if subprocess.run(
        [sys.executable, "-c", "import yaml"],
        capture_output=True, text=True, timeout=60,
    ).returncode != 0:
        pytest.skip("PyYAML not installed in the test runner")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0, (
        "scan.py must exit non-zero on an unscannable root, not exit 0 with "
        f"empty findings (got exit {result.returncode}, stdout={result.stdout!r})"
    )
    try:
        parsed = json.loads(result.stdout)
        emitted_findings_doc = isinstance(parsed, dict) and "findings" in parsed
    except json.JSONDecodeError:
        emitted_findings_doc = False
    assert not emitted_findings_doc, (
        "scan.py emitted a findings document on stdout despite failing — a "
        "caller could mistake it for a completed, clean scan"
    )

"""The hand-off must never let an agent buy speed by checking less.

ci-speedup hands a downstream coding agent an RCA plus a prompt instead of an
applied fix, so the prompt IS the whole contract. The cheapest way to make CI
faster is to make it verify less — deleted matrix legs, `continue-on-error`,
a required check that stops reporting, tests skipped behind a path filter — and
every one of those reads as a win in the numbers ci-speedup measures. So every
prompt-emitting surface carries the same rail, and this pins it:

  - the catalog prompt (a matched `_FIX_META` cause);
  - the generic prompt (no catalog match) and its no-job-timing variant;
  - the LLM gap-fill prompt, whose body the agent authors;
  - the per-pattern hygiene / Tier-2 / queue-wait prompt.

It also pins the eval case that fails a hand-off accepting such an edit.

Run from the repo root:

    pytest -v skills/ci-speedup/tests/test_no_weakening_rail.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)

# The forbidden edits, as substrings that must appear in every prompt's rail.
# Kept here verbatim (not imported from the renderer) so a rail that is quietly
# weakened in blocking_path.py fails here instead of moving with it.
_FORBIDDEN_MARKERS = (
    "matrix leg",            # deleting / narrowing matrix legs
    "continue-on-error",     # exit-code suppression
    "|| true",
    "required status check",  # narrowing / silencing a required check
    "path/branch filter",    # skipping tests by filter
    "retries",               # weakening the signal rather than the cost
)

_RAIL_HEADING = "NEVER BUY SPEED BY CHECKING LESS"


def _assert_rail(prompt: str, where: str) -> None:
    assert _RAIL_HEADING in prompt, f"{where}: no no-weakening rail in the prompt"
    for marker in _FORBIDDEN_MARKERS:
        assert marker in prompt, f"{where}: rail does not forbid `{marker}`"
    # The rail is an exception rule, not a blanket ban: an RCA that NAMED the
    # reduction as the finding must still be fixable.
    assert "unless" in prompt.lower(), (
        f"{where}: rail states no exception for an RCA that named the reduction")


_POLE = {
    "workflow_file": ".github/workflows/ci.yml",
    "check": "test",
    "p50_s": 900.0,
    "dominant_step": "Build",
    "dominant_p50_s": 600.0,
    "dominant_share": 0.66,
}


def test_catalog_prompt_carries_the_rail() -> None:
    leaf = {"fix_key": "turbo-remote-cache", "unit_label": "turbo rebuilds everything",
            "evidence": ["cache miss, executing 1a2b3c"]}
    out = bp._build_agent_prompt(leaf, dict(_POLE), [], None, "acme/app", "deadbeef1234",
                                 3, 20)
    _assert_rail(out, "catalog prompt")


def test_generic_prompt_carries_the_rail() -> None:
    out = bp._build_generic_agent_prompt(
        dict(_POLE), [], None, "acme/app", "deadbeef1234", 3, 20, None)
    _assert_rail(out, "generic prompt")


def test_no_job_timing_prompt_carries_the_rail() -> None:
    pole = dict(_POLE)
    pole["job_timing_unavailable"] = "no developer-facing run sampled"
    out = bp._build_generic_agent_prompt(
        pole, [], None, "acme/app", "deadbeef1234", 3, 20, None)
    _assert_rail(out, "no-job-timing prompt")


def test_llm_gap_fill_prompt_carries_the_rail() -> None:
    out = bp._llm_agent_prompt("The install step re-resolves the lockfile every run.")
    _assert_rail(out, "LLM gap-fill prompt")


def test_llm_gap_fill_rail_is_not_doubled() -> None:
    """The renderer owns the rail; a body that already carries it isn't doubled."""
    once = bp._llm_agent_prompt("body")
    twice = bp._llm_agent_prompt(once.split("```text\n", 1)[1].rsplit("\n```", 1)[0])
    assert twice.count(_RAIL_HEADING) == 1


def test_llm_gap_fill_partial_rail_does_not_suppress_the_canonical_block() -> None:
    """A model-authored body that merely ECHOES the rail's heading must not
    suppress the renderer-owned block. `gap-fill.md` names the rail to the model
    writing that body, so an echoed heading is a realistic body; if the heading
    alone counted as "already carries it", the hand-off would ship a rail
    naming none of the forbidden edits."""
    out = bp._llm_agent_prompt(
        "The install step re-resolves the lockfile every run.\n\n"
        + _RAIL_HEADING + "\n- keep an eye on coverage.")
    _assert_rail(out, "gap-fill prompt with a partial model-authored rail")


def test_hygiene_prompt_carries_the_rail() -> None:
    members = [{"pattern": "OPT24", "title": "Long test job without sharding",
                "workflow_file": ".github/workflows/ci.yml", "job": "test",
                "runner_minutes_per_month": 400.0}]
    out = "\n".join(bp._hygiene_prompt(
        "OPT24", "Long test job without sharding", members,
        "https://example.invalid/catalog#opt24"))
    _assert_rail(out, "hygiene prompt")


def test_rail_does_not_disturb_the_one_disclaimer_per_prompt_invariant() -> None:
    """verify_report counts `does NOT prescribe the fix` once per prompt — the
    rail must not add a second copy of that load-bearing substring."""
    out = bp._build_generic_agent_prompt(
        dict(_POLE), [], None, "acme/app", "deadbeef1234", 3, 20, None)
    assert out.count("does NOT prescribe the fix") == 1


def test_eval_case_pins_the_rail() -> None:
    """An eval case must fail a hand-off that would accept a coverage-reducing edit."""
    cases = json.loads((_SKILL_DIR / "evals" / "evals.json").read_text())["evals"]
    hits = [c for c in cases
            if "checking less" in json.dumps(c).lower()
            or "continue-on-error" in json.dumps(c).lower()]
    assert hits, "evals.json has no case for the no-weakening rail"
    blob = json.dumps(hits).lower()
    for marker in ("matrix leg", "continue-on-error", "required status check",
                   "path/branch filter"):
        assert marker.lower() in blob, f"eval case does not cover `{marker}`"
    for case in hits:
        assert case.get("expectations"), "eval case has no expectations list"

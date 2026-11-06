"""Every coding-agent handoff proves the change against the repository's CI gate."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import blocking_path as bp  # noqa: E402


_POLE = {
    "workflow_file": ".github/workflows/ci.yml",
    "check": "test",
    "p50_s": 900.0,
    "dominant_step": "Build",
    "dominant_p50_s": 600.0,
    "dominant_share": 0.66,
    "timing_spread": {"min_s": 840.0, "max_s": 960.0, "n": 8},
}

_BAR_MARKERS = (
    "VERIFICATION BAR",
    "A scoped local run is pre-flight only",
    "repository's own CI gate passes",
    "observed range beside the predicted saving",
    "Do not open a pull request when the predicted saving is smaller",
)


def _prompts() -> dict[str, str]:
    leaf = {
        "fix_key": "turbo-remote-cache",
        "unit_label": "turbo rebuilds everything",
        "evidence": ["cache miss, executing 1a2b3c"],
    }
    no_timing = dict(_POLE)
    no_timing["job_timing_unavailable"] = "no developer-facing run sampled"
    return {
        "catalog": bp._build_agent_prompt(
            leaf, dict(_POLE), [], None, "acme/app", "deadbeef", 3, 20
        ),
        "generic": bp._build_generic_agent_prompt(
            dict(_POLE), [], None, "acme/app", "deadbeef", 3, 20, None
        ),
        "generic without job timing": bp._build_generic_agent_prompt(
            no_timing, [], None, "acme/app", "deadbeef", 3, 20, None
        ),
        "gap-fill": bp._llm_agent_prompt(
            "The build step compiles the same generated client on every run.",
            dict(_POLE),
        ),
    }


def test_catalog_generic_and_gap_fill_prompts_carry_verification_bar() -> None:
    for name, prompt in _prompts().items():
        for marker in _BAR_MARKERS:
            assert marker in prompt, f"{name} prompt is missing {marker!r}"


def test_verification_language_in_rendered_prompts_is_closed_world() -> None:
    """A new verification sentence must be part of the bar or a named old rail.

    The allowlist is deliberately short: the existing no-weakening rail, the
    catalog fixture's output check, and the established section heading.
    """
    allowed = (
        "verification bar",
        "call the change verified only",
        "deliver & verify",
        "verified by exactly what verifies",
        "still verified by every check",
        "pipeline that finishes sooner because it verifies less",
        "verify task outputs are correct",
    )
    for name, prompt in _prompts().items():
        for line in prompt.splitlines():
            if "verif" not in line.lower():
                continue
            assert any(phrase in line.lower() for phrase in allowed), (
                f"{name} prompt has verification language outside the bar/allowlist: "
                f"{line!r}"
            )


def test_gap_fill_keeps_one_renderer_owned_verification_bar() -> None:
    once = bp._llm_agent_prompt("body")
    body = once.split("```text\n", 1)[1].rsplit("\n```", 1)[0]
    twice = bp._llm_agent_prompt(body)
    assert twice.count("VERIFICATION BAR") == 1


def test_every_prompt_builder_carries_both_shared_bars() -> None:
    """Pin the prompt surface as a closed set beside the existing shared rail."""
    source = (_SKILL_DIR / "scripts" / "blocking_path.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    prompt_builders = {
        "_build_agent_prompt",
        "_build_generic_agent_prompt",
        "_llm_agent_prompt",
        "_offcategory_note_block",
        "_hygiene_prompt",
        "_opt82_prompt",
        "_opt81_prompt",
        "_opt83_prompt",
    }
    for name in sorted(prompt_builders):
        loaded = {
            node.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        assert "_NO_WEAKENING_LINES" in loaded, f"{name} lost the no-weakening rail"
        assert "_VERIFICATION_BAR_LINES" in loaded, f"{name} lost the verification bar"


def test_skill_body_line_count_stays_net_zero() -> None:
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    frontmatter_end = lines.index("---", 1)
    assert len(lines[frontmatter_end + 1 :]) == 499

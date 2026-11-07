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
    prompts = {}
    for fix_key in bp._FIX_META:
        leaf = {
            "fix_key": fix_key,
            "unit_label": "measured unit",
            "evidence": ["measured evidence"],
        }
        prompts[f"catalog {fix_key}"] = bp._build_agent_prompt(
            leaf, dict(_POLE), [], None, "acme/app", "deadbeef", 3, 20
        )
    no_timing = dict(_POLE)
    no_timing["job_timing_unavailable"] = "no developer-facing run sampled"
    prompts.update({
        "generic": bp._build_generic_agent_prompt(
            dict(_POLE), [], None, "acme/app", "deadbeef", 3, 20, None
        ),
        "generic without job timing": bp._build_generic_agent_prompt(
            no_timing, [], None, "acme/app", "deadbeef", 3, 20, None
        ),
        "gap-fill": bp._llm_agent_prompt(
            "The build step compiles the same generated client on every run.\n"
            "A scoped local test verifies the fix before CI runs.",
            dict(_POLE),
        ),
        "off-category cache path": "\n".join(bp._offcategory_note_block(
            {
                "fix_key": "cache-effective-path-mismatch",
                "config_fact": "the configured cache path was absent",
                "evidence": ["cache path does not exist"],
            },
            {**_POLE, "job": "test", "dominant_category": "build"},
        )),
        "hygiene": "\n".join(bp._hygiene_prompt(
            "OPT24", "Long test job without sharding",
            [{
                "pattern": "OPT24",
                "title": "Long test job without sharding",
                "workflow_file": ".github/workflows/ci.yml",
                "job": "test",
                "runner_minutes_per_month": 400.0,
            }],
            "https://example.invalid/catalog#opt24", saves_wc=True,
        )),
        "OPT82": "\n".join(bp._opt82_prompt(
            "Type-aware lint",
            [{
                "pattern": "OPT82",
                "workflow_file": ".github/workflows/ci.yml",
                "affected_jobs": ["lint"],
                "evidence": "type-aware lint dominates",
                "type_aware_lint": {
                    "rules": [{"rule": "@typescript-eslint/no-floating-promises"}],
                    "configs": ["eslint.config.mjs"],
                    "ceiling_s": 120.0,
                    "ceiling_basis": "lint_step",
                },
            }],
            "https://example.invalid/catalog#opt82", "ci.yml (lint)",
        )),
        "OPT81": "\n".join(bp._opt81_prompt(
            {
                "pattern": "OPT81",
                "title": "Faster runner",
                "workflow_file": ".github/workflows/ci.yml",
                "evidence": "the same job uses two runner classes",
                "faster_runner": {"half": "A1", "job": "test"},
            },
            "https://example.invalid/catalog#opt81",
        )),
        "OPT83": "\n".join(bp._opt83_prompt(
            {
                "pattern": "OPT83",
                "title": "Independent steps",
                "workflow_file": ".github/workflows/ci.yml",
                "evidence": "the steps share no declared outputs",
                "independent_steps": {
                    "job": "test",
                    "yaml_job": "test",
                    "steps": [{"step": "lint"}, {"step": "unit tests"}],
                    "ceiling_s": 90.0,
                },
            },
            "https://example.invalid/catalog#opt83",
        )),
    })
    return prompts


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
        "verified unnecessary or re-run selectively",
        "verify task outputs are correct",
        "verify the medians match",
        "verify the coverage consumer",
        "verify the consumer",
        "verify a deliberately-introduced lint error",
        "verify the same test count and results",
        "verify a fresh pr restores",
        "verify outputs are correct",
        "does real verification",
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


def test_gap_fill_discards_model_authored_verification_claims() -> None:
    contradiction = "A scoped local test verifies the fix before CI runs."
    prompt = bp._llm_agent_prompt(
        f"VERIFICATION BAR\n- {contradiction}\n\nKeep the measured cause."
    )
    assert contradiction not in prompt
    assert "Keep the measured cause." in prompt
    assert prompt.count("VERIFICATION BAR") == 1


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


def _hygiene_member() -> dict:
    return {
        "pattern": "OPT61",
        "title": "Missing dependency cache",
        "workflow_file": ".github/workflows/ci.yml",
        "job": "lint",
        "runner_minutes_per_month": 400.0,
    }


def test_bill_only_hygiene_prompts_keep_ci_bar_without_pole_range_rule() -> None:
    """A runner-minute saving is not comparable with a pole's timing range.

    Bill-only, Tier-2 and queue-wait prompts still require CI-backed verification,
    but the pole-range rule belongs only to wall-clock hand-offs.
    """
    variants = {
        "also noticed": {},
        "tier2": {"tier2": True},
        "queue wait": {"wait": True},
    }
    for name, kwargs in variants.items():
        prompt = "\n".join(bp._hygiene_prompt(
            "OPT61", "Missing dependency cache", [_hygiene_member()],
            "https://example.invalid/catalog#opt61", **kwargs,
        ))
        assert "A scoped local run is pre-flight only" in prompt, name
        assert "repository's own CI gate passes" in prompt, name
        assert "observed range beside the predicted saving" not in prompt, name


def test_wall_clock_hygiene_prompt_keeps_pole_range_rule() -> None:
    prompt = "\n".join(bp._hygiene_prompt(
        "OPT61", "Missing dependency cache", [_hygiene_member()],
        "https://example.invalid/catalog#opt61", saves_wc=True,
    ))
    assert "observed range beside the predicted saving" in prompt


def test_close_still_runs_the_pole_prompt_verbatim() -> None:
    """The pick contract hands the agent the rendered prompt unchanged."""
    import re

    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    close = re.sub(r"\s+", " ", text)
    assert "run that pole's agent prompt verbatim" in close

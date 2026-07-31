"""Guards for the OD-L4 prompt-routing contract (B3).

The routing itself is LLM-driven (skill descriptions), so the committed
artifact + the SKILL.md description are the only guardable surfaces. These
cells pin: the artifact exists and is well-shaped; OD-L4's required routings
hold (ambiguous middle → ci-speedup; explicit grade/score/hygiene →
ci-score); and the SKILL.md description actually carries the do-not-trigger
routing so the artifact can't silently diverge from what the model reads.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
_ARTIFACT = _SKILL_DIR / "evals" / "prompt-routing.json"
_SKILL_MD = (_SKILL_DIR / "SKILL.md").read_text()

_AMBIGUOUS_MIDDLE = {"audit my CI", "check my CI", "review my CI setup"}
_REQUIRED_ADVISOR = {"grade my CI", "is my CI healthy",
                     "how healthy is my CI config"}


def _routes() -> list[dict]:
    return json.loads(_ARTIFACT.read_text())["routes"]


def test_artifact_shape():
    routes = _routes()
    assert len(routes) >= 10
    for r in routes:
        assert set(r) == {"prompt", "expected", "why"}
        assert r["expected"] in {"ci-speedup", "ci-score", "none"}
    # no duplicate prompts — a contract with two answers is no contract
    prompts = [r["prompt"] for r in routes]
    assert len(prompts) == len(set(prompts))


def test_od_l4_required_routings_hold():
    by_prompt = {r["prompt"]: r["expected"] for r in _routes()}
    for p in _AMBIGUOUS_MIDDLE:
        assert by_prompt.get(p) == "ci-speedup", (
            f"OD-L4: bare-ambiguous {p!r} must route to ci-speedup")
    for p in _REQUIRED_ADVISOR:
        assert by_prompt.get(p) == "ci-score", (
            f"OD-L4: {p!r} must route to ci-score")
    # speed/cost language never lands here
    for r in _routes():
        if re.search(r"slow|cost|optimiz", r["prompt"], re.I):
            assert r["expected"] == "ci-speedup", r["prompt"]
    # OD-L6: ci-score is not a security audit — the security prompt routes to
    # "none" (do-not-trigger), never to a sibling skill. This positively pins
    # the one route this decision changed, so a future edit can't silently
    # flip it back to a sibling without failing the suite.
    assert by_prompt.get("review our workflows for security issues") == "none", (
        "OD-L6: security prompts must route to 'none' (not-a-security-audit framing)")


def test_skill_description_carries_the_routing():
    """The model routes on the frontmatter description, not the artifact —
    the description must name the do-not-trigger surfaces so the two can't
    diverge silently."""
    fm = _SKILL_MD.split("---")[1]
    assert "ci-speedup" in fm and "not a security audit" in fm
    assert re.search(r"why is CI slow", fm)
    assert "grade" in fm.lower()
    # the placeholder wall is gone — this is the live description
    assert "NOT LAUNCHED" not in _SKILL_MD

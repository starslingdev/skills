"""Guards for the one thing this skill is: a routing contract.

`sling-cli` ships no detectors and no report pipeline — its whole product is
prose that tells an agent which CLI answers which ask. Two ways that prose
can be wrong while every other check in the repo stays green:

1. **It names a `sling` subcommand that does not exist.** This is the
   failure the skill was written to prevent (an agent inventing `sling
   rerun`), so shipping it in our own routing table would be the joke
   writing itself. It is not hypothetical: `sling doctor` on v0.1.2 emits
   `fix_command: "sling update"`, and `sling update` is rejected by the
   binary as unknown — a wrong command name survives being printed by the
   tool itself.
2. **The read/write split drifts.** Every routing rule reduces to "does
   this change CI state?" If a mutating verb ever routes to `sling`, or a
   mutating command appears in the surface while the skill still says
   read-only, the contract is broken in the direction that costs the user
   runner-minutes or a cancelled deploy.

`references/command-surface.json` is the pinned capture of what the CLI
actually offers, so these tests are deterministic and need no network.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

_SKILL = Path(__file__).resolve().parents[1]
_SURFACE = json.loads((_SKILL / "references" / "command-surface.json").read_text())
_ROUTING = json.loads((_SKILL / "evals" / "prompt-routing.json").read_text())
_SKILL_MD = (_SKILL / "SKILL.md").read_text()
_FRONT = yaml.safe_load(_SKILL_MD[4:_SKILL_MD.index("\n---", 4)])
_BODY = _SKILL_MD[_SKILL_MD.index("\n---", 4):]

_COMMANDS = {c for group in _SURFACE["groups"].values() for c in group}

# `sling` followed by one or two bare words: the shape of a command mention.
# Flags (`--x`), placeholders (`<id>`) and prose (`` `sling` cannot ``) do not
# match, which is what keeps this from flagging every sentence.
_MENTION = re.compile(r"\bsling\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?")

# Words that follow `sling` in ordinary prose and are not command attempts.
_PROSE = {"is", "cannot", "has", "shows", "reads", "says", "and", "was", "in",
          "on", "over", "with", "for", "to", "the", "as", "a", "it", "does",
          "command", "commands", "release", "v", "credential", "credentials"}


def _mentions(text: str) -> set[str]:
    """Every `sling <cmd>` a reader could take as a command, longest match."""
    found = set()
    for first, second in _MENTION.findall(text):
        if first in _PROSE:
            continue
        two = f"{first} {second}" if second else None
        found.add(two if two in _COMMANDS else first)
    return found


def test_surface_capture_is_not_empty():
    """Positive control: an empty or renamed `groups` block would make every
    membership assertion below pass vacuously."""
    assert len(_COMMANDS) >= 15, f"surface capture holds only {_COMMANDS}"
    assert {"why", "time", "runs list", "logs"} <= _COMMANDS


def test_detector_recognises_a_real_and_a_fake_command():
    """Teeth check for `_mentions`: it must find both a good and a bad name,
    or the scan below cannot fail on a hallucinated command."""
    found = _mentions("Run `sling why <job>` and then `sling rerun 123`.")
    assert "why" in found and "rerun" in found
    assert _mentions("`sling` cannot do it in this release.") == set()


def test_skill_md_names_only_commands_that_exist():
    """The routing table must not invent a subcommand. `sling update` is the
    live example: printed by the tool, rejected by the tool."""
    unknown = _mentions(_BODY) - _COMMANDS
    assert not unknown, (
        f"SKILL.md routes to sling subcommand(s) that do not exist: "
        f"{sorted(unknown)}. Check them against `sling --help`, and if a new "
        f"release added one, update references/command-surface.json.")


def test_reference_names_only_commands_that_exist_or_documents_the_exception():
    ref = (_SKILL / "references" / "command-reference.md").read_text()
    unknown = _mentions(ref) - _COMMANDS
    assert unknown <= {"update"}, f"command reference invents {sorted(unknown - {'update'})}"
    if "update" in unknown:
        assert "does not\nexist" in ref or "does not exist" in ref, (
            "the reference mentions `sling update` without saying it is not a "
            "real subcommand — that is the exact trap it was written to warn about")


@pytest.mark.parametrize("verb", ["rerun", "cancel", "trigger", "enable",
                                  "disable", "download"])
def test_every_mutating_verb_is_routed_to_gh(verb: str):
    """Each state-changing ask must appear in the routing table on a `gh` row.
    A verb that falls out of the table is a verb an agent will improvise."""
    rows = [ln for ln in _BODY.splitlines()
            if ln.startswith("|") and verb in ln.lower()]
    assert rows, f"the routing table no longer covers {verb!r}"
    assert any("`gh" in ln for ln in rows), (
        f"{verb!r} appears in the routing table but not on a `gh` row")


def test_read_only_claim_matches_the_captured_surface():
    """If a sling release ships a write command, the capture changes and this
    fails — before the skill can keep telling users sling cannot change
    anything."""
    assert _SURFACE["read_only"] is True
    assert _SURFACE["mutating_commands"] == []
    assert "read-only" in _FRONT["description"]


def test_routing_evals_agree_with_the_surface_and_the_split():
    for r in _ROUTING["routings"]:
        route, prompt = r["route"], r["prompt"]
        assert route in {"sling", "gh", "ci-score", "ci-speedup",
                         "ci-secure", "file-edit", "none"}, prompt
        if route == "sling":
            cmd = r["command"].removeprefix("sling ")
            assert cmd in _COMMANDS, f"{prompt!r} routes to unknown `sling {cmd}`"
        if route == "gh":
            assert r["command"].startswith("gh "), prompt


def test_no_mutating_ask_routes_to_sling():
    """The one-line version of the whole contract."""
    verbs = ("re-run", "rerun", "cancel", "kick off", "disable", "enable",
             "download", "approve")
    for r in _ROUTING["routings"]:
        if any(v in r["prompt"].lower() for v in verbs):
            assert r["route"] != "sling", (
                f"{r['prompt']!r} routes a state change to a read-only CLI")


def test_repo_wide_asks_leave_this_skill_for_the_owning_audit_skill():
    """The boundary this skill has to hold from its own side: an ask about the
    repo's workflow CONFIGURATION is never answered from run data. `sling`
    cannot read a workflow file at all, so answering one here means guessing."""
    owners = {"grade my CI": "ci-score",
              "is my CI secure": "ci-secure",
              "why is CI slow": "ci-speedup"}
    routed = {r["prompt"]: r["route"] for r in _ROUTING["routings"]}
    for prompt, skill in owners.items():
        assert routed.get(prompt) == skill, (
            f"{prompt!r} must hand off to {skill}, not be answered here")
    assert not any(r["route"] == "sling" for r in _ROUTING["routings"]
                   if r["prompt"] in owners)


def test_frontmatter_carries_the_handoff_contract():
    """The description is the only part a router reads before deciding. If it
    loses the handoff clause, this skill starts answering repo-wide audits."""
    low = _FRONT["description"].lower()
    for engine in ("ci-score", "ci-speedup", "ci-secure"):
        assert engine in low, f"description no longer names {engine}"
    assert "do not trigger" in low, "description lost its negative-trigger clause"


def test_agent_flag_is_the_documented_default():
    """Every shape in the reference was captured under `--agent`. If the body
    stops telling the agent to pass it, stdout becomes human chrome and every
    documented parse breaks."""
    assert "--agent" in _FRONT["description"] or "--agent" in _BODY
    assert "Always pass `--agent`" in _BODY
    assert "--agent" in _SURFACE["global_flags"]


def test_exit_code_table_matches_the_capture():
    """A code documented in one place and missing in the other is how an agent
    learns to treat exit 10 as a crash."""
    for code in _SURFACE["exit_codes"]:
        assert re.search(rf"^\| `{code}` \|", _BODY, re.M), (
            f"SKILL.md's exit-code table is missing `{code}`")


def test_skill_ships_no_installer_literal():
    """Repo-level guard covers the whole tree; this states the intent locally,
    because this is the one skill whose subject matter invites the literal."""
    assert not re.search(r"curl[^|\n]*https?://[\w-][^|\n]*\|\s*(?:sudo\s+)?(?:ba)?sh",
                         _SKILL_MD + (_SKILL / "references" / "command-reference.md").read_text())
    assert "docs.starsling.dev/sling-cli/installation" in _SKILL_MD, (
        "the install path must still be reachable — point at the installation page")

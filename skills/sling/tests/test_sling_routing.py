"""Guards for the one thing this skill is: a routing contract.

This skill ships no detectors and no report pipeline — its whole product is
prose that tells an agent which CLI answers which ask. (It is named after the
CLI it drives, so `sling` below always means the binary.) Two ways that prose
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
_EVALS = json.loads((_SKILL / "evals" / "evals.json").read_text())
_SKILL_MD = (_SKILL / "SKILL.md").read_text()
_FRONT = yaml.safe_load(_SKILL_MD[4:_SKILL_MD.index("\n---", 4)])
_BODY = _SKILL_MD[_SKILL_MD.index("\n---", 4):]

_COMMANDS = {c for group in _SURFACE["groups"].values() for c in group}

# Command mentions are read out of CODE SPANS — inline backticks and fenced
# blocks — not out of running prose. Two reasons, both learned the hard way:
# prose like "`sling logs` is a filter" put a word after a command name and had
# to be special-cased, and a prose-scoped scan could not see the frontmatter
# description at all, which is where an unreal `sling runs` was in fact shipped.
_CODE = re.compile(r"`([^`\n]+)`|```[a-z]*\n(.*?)```", re.S)
_GROUPS = {c.split()[0] for c in _COMMANDS if " " in c}


def _mentions(text: str) -> set[str]:
    """Every `sling <cmd>` a reader could act on, as the longest real match.

    Tokens are taken until something that cannot be a subcommand — a flag, a
    `<placeholder>`, an id, a `/` — which is what stops `sling why <job-id>`
    from reading as a two-word command. The pair is reported whenever it is
    not a known command, so an invented subcommand under a REAL command
    (`sling bill export`, `sling logs tail`) is caught. Reporting only the
    first word there was the original hole: `bill` is real, so nothing fired.
    """
    found: set[str] = set()
    for inline, fenced in _CODE.findall(text):
        span = inline or fenced
        for m in re.finditer(r"\bsling\s+(.*)", span):
            toks: list[str] = []
            for raw in m.group(1).split():
                if not re.fullmatch(r"[a-z][a-z-]*", raw) or len(toks) == 2:
                    break
                toks.append(raw)
            if not toks:
                continue
            two = " ".join(toks) if len(toks) == 2 else None
            if two and two in _COMMANDS:
                found.add(two)
            elif two and (toks[0] in _GROUPS or toks[0] in _COMMANDS):
                found.add(two)          # invented subcommand under a real one
            else:
                found.add(toks[0])


    return found


def test_surface_capture_is_not_empty():
    """Positive control: an empty or renamed `groups` block would make every
    membership assertion below pass vacuously."""
    assert len(_COMMANDS) >= 15, f"surface capture holds only {_COMMANDS}"
    assert {"why", "time", "runs list", "logs"} <= _COMMANDS


def test_detector_recognises_a_real_and_a_fake_command():
    """Teeth check for `_mentions`. The sub-subcommand cases are the ones a
    first version got wrong: `bill` and `logs` are real, so a fallback that
    reported only the first word waved `sling bill export` straight through."""
    found = _mentions("Run `sling why <job>` and then `sling rerun 123`.")
    assert "why" in found and "rerun" in found
    for invented in ("sling bill export", "sling logs tail", "sling why explain"):
        assert invented.removeprefix("sling ") in _mentions(f"`{invented}`"), invented
    for real in ("sling bill history", "sling runs list", "sling org switch"):
        assert _mentions(f"`{real} --agent`") <= _COMMANDS, real
    # Prose is not a command mention, and an id or flag does not read as one.
    assert _mentions("sling cannot do it in this release.") == set()
    assert _mentions("`sling logs 97912608061 --agent --limit 60`") == {"logs"}


def _assert_only_real_commands(text: str, where: str) -> None:
    """A `sling <cmd>` a reader could act on must exist — with one allowance:
    naming a non-command in order to WARN about it is the opposite of the
    failure, so it passes only when the same file says the command is not real.
    `sling update` is the live case: `sling doctor` recommends it and the
    binary rejects it, so warning about it by name is the useful thing to do."""
    unknown = _mentions(text) - _COMMANDS
    warned = {c for c in unknown
              if re.search(rf"`sling {c}`[^.]{{0,120}}?does not\s+exist", text, re.S)}
    invented = unknown - warned
    assert not invented, (
        f"{where} routes to sling subcommand(s) that do not exist: "
        f"{sorted(invented)}. Check them against `sling --help`, and if a new "
        f"release added one, update references/command-surface.json.")


def test_skill_md_names_only_commands_that_exist():
    _assert_only_real_commands(_BODY, "SKILL.md")


def test_the_frontmatter_description_names_only_commands_that_exist():
    """The description is the one string a router reads for EVERY ask, so it is
    the worst place to name a command that does not exist — and it shipped one
    (`sling runs`, which is only ever `runs list` / `runs show`) while the body
    was guarded and it was not."""
    _assert_only_real_commands(_FRONT["description"], "the frontmatter description")


@pytest.mark.parametrize(
    "ref", sorted((_SKILL / "references").glob("*.md")), ids=lambda p: p.name)
def test_every_shipped_reference_names_only_commands_that_exist(ref):
    """Every reference, not just the big one: they are all linked from SKILL.md
    and all install, so a hallucination in any of them reaches a user."""
    _assert_only_real_commands(ref.read_text(), f"references/{ref.name}")


def test_the_warning_allowance_does_not_swallow_a_plain_invention():
    """Teeth check on the allowance itself: a made-up command with no warning
    beside it must still fail, or the exemption quietly disables the guard."""
    with pytest.raises(AssertionError):
        _assert_only_real_commands("Just run `sling rerun 123` to retry.", "sample")


# The canonical state-changing actions, as the routing table names them.
_MUTATING_ACTIONS = ("rerun", "cancel", "trigger", "enable", "disable",
                     "download", "delete")
# The same actions as a USER would phrase them, for scanning eval prompts.
# Kept as one derived set so the two guards can never disagree about what
# counts as mutating — they did, and `trigger` fell through the gap.
_MUTATING_VERBS = _MUTATING_ACTIONS + ("re-run", "kick off", "approve")


@pytest.mark.parametrize("verb", _MUTATING_ACTIONS)
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
    for r in _ROUTING["routings"]:
        if any(v in r["prompt"].lower() for v in _MUTATING_VERBS):
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


def test_evals_cover_both_polarities():
    """Skill evals are how a later change gets caught, and how we notice if the
    base model catches up and the skill stops earning its context. Coverage has
    to include the asks this skill must DECLINE, or it only ever proves the
    happy path."""
    cases = _EVALS["evals"]
    assert len(cases) >= 3, "fewer than three eval scenarios"
    assert any(c["should_trigger"] for c in cases), "no should-trigger case"
    assert any(not c["should_trigger"] for c in cases), "no should-NOT-trigger case"
    for c in cases:
        assert c.get("assertions"), f"eval {c['id']} has no assertions to grade"
        assert c.get("expected_output", "").strip(), f"eval {c['id']} has no expected output"


def test_reference_over_100_lines_carries_a_contents_block():
    """Claude previews long reference files with partial reads, so a file that
    does not state its own scope up top can be acted on half-read."""
    for ref in sorted((_SKILL / "references").glob("*.md")):
        text = ref.read_text()
        if len(text.splitlines()) > 100:
            head = "\n".join(text.splitlines()[:25]).lower()
            assert "contents" in head, f"{ref.name} is long but has no contents block"


def _shipped_text() -> list[tuple[str, str]]:
    out = [("SKILL.md", (_SKILL / "SKILL.md").read_text())]
    out += [(f"references/{r.name}", r.read_text())
            for r in sorted((_SKILL / "references").glob("*.md"))]
    return out


def test_login_is_never_shown_with_the_machine_mode_flag():
    """`sling login` needs a human at a browser to approve a device code, and
    `--agent` carries `--no-input` — so the flag turns the one command that
    requires a person into exit `2`. Any shipped example pairing them teaches
    an agent to hang the session on a prompt it has already refused."""
    for where, text in _shipped_text():
        for line in text.splitlines():
            if "sling login" in line:
                assert "--agent" not in line or "never" in line.lower() or "not" in line.lower(), (
                    f"{where}: `sling login` shown with --agent — {line.strip()[:90]}")


def test_the_skill_says_the_user_runs_login():
    """The recovery for exit `4` has to hand the browser step back to a person.
    An agent that runs `sling login` itself blocks until timeout and never
    shows the code, leaving the user signed out and the session stuck."""
    body = _BODY.lower()
    assert "ask the user to run `sling login`" in body, (
        "the preflight no longer hands the sign-in to the user")
    assert "do not run it yourself" in body, (
        "the preflight no longer tells the agent to keep its hands off login")

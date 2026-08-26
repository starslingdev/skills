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


def test_exit_four_is_not_treated_as_a_login_problem_unconditionally():
    """`sling` reports CI that the StarSling GitHub App collected, so an org the
    app was never installed on fails with exit `4` — the same code as a stale
    credential, and a message that ends "Run `sling login`". Following that
    advice cannot work: the login was never the problem. If the skill ever
    collapses exit 4 back into "run login and retry", it sends users into the
    same loop the CLI's own message does."""
    row = next((ln for ln in _BODY.splitlines()
                if ln.startswith("| `4` |")), None)
    assert row, "the exit-code table lost its row for 4"
    low = row.lower()
    assert "org" in low, "exit 4 no longer mentions the org/installation case"
    assert "do not retry login" in low or "not retry" in low, (
        "exit 4 no longer warns against retrying login for the org case")


def test_the_app_precondition_is_stated_somewhere_a_reader_will_hit_it():
    """A valid login sees nothing for an org without the app installed. That is
    a precondition, not an error case, and nothing in `sling doctor` checks it —
    so the skill is the only place a user can learn it."""
    body_and_refs = _BODY + "".join(
        r.read_text() for r in sorted((_SKILL / "references").glob("*.md")))
    assert "github.com/apps/starslingdev" in body_and_refs, (
        "the skill no longer says where to install the GitHub App")
    assert "personal" in body_and_refs.lower(), (
        "the skill no longer notes that personal repositories are unsupported")


def test_an_empty_result_is_not_documented_as_proof_of_no_ci():
    """`{"runs": []}` with exit 0 means either "no runs in the window" or
    "StarSling is not watching this repo". Reporting the first as fact is how a
    missing installation gets rendered as a finding about the user's CI."""
    low = _BODY.lower()
    assert "coverage hole" in low and "did not run" in low, (
        "the skill lost the warning that an empty listing is a coverage hole "
        "rather than a finding — the same rule ci-secure states for a check "
        "that could not run")


def test_the_gh_gate_matches_the_house_pattern():
    """`gh` is half this skill's routing table, and it authenticates separately
    from `sling` — a working `sling` says nothing about whether `gh` is signed
    in. ci-speedup's gh gate already encodes what this costs to get wrong,
    including a live miss: a sandboxed agent shell cannot reach keyring
    credentials, so a single failed probe is not proof of anything. Re-deriving
    a thinner version of that gate is how the lesson gets lost."""
    # Whitespace-normalised: this prose is hard-wrapped, so `gh auth status`
    # legitimately spans a line break and a naive substring test would report a
    # missing gate that is right there.
    text = _BODY + (_SKILL / "references" / "gh-fallback.md").read_text()
    low = " ".join(text.split()).lower()
    assert "isn't installed" in low or "is installed" in low, (
        "the gh gate no longer checks whether gh is INSTALLED, only whether it "
        "is authenticated — two different failures with the same consequence")
    assert "gh auth status" in low, "the gh gate lost its auth probe"
    # A bounded match, not a substring test. CodeQL's incomplete-URL-sanitization
    # rule fires on any `in` check against a host — correctly in general, since
    # `"cli.github.com" in text` is satisfied by `evil-cli.github.com.example`, the
    # look-alike shape this repo has shipped once and been rated CRITICAL for. Here the
    # subject is our own documentation rather than an untrusted URL, but the weakness is
    # the same either way, so the check is anchored: scheme in front, and nothing that
    # could extend the host behind.
    assert re.search(r"https://cli\.github\.com(?![\w.-])", low), (
        "the gh gate no longer gives the install path")
    assert "sandbox" in low, (
        "the gh gate lost the sandboxed-shell caution ci-speedup recorded from "
        "a live miss — without it a Codex shell reports a false auth failure")


def test_both_shapes_of_a_missing_installation_are_recognised():
    """`sling` sees nothing until the StarSling GitHub App is installed, and that
    arrives two ways: exit 4 for an org you cannot reach, and exit 2 with "you
    don't belong to any orgs yet" for the brand-new user who signed in first.
    Only the first was handled, so the most ordinary first-run state in the
    product fell through to whatever the exit-code table happened to say."""
    low = " ".join(_BODY.split()).lower()
    assert "don't have access to org" in low, "lost the unreachable-org trigger"
    assert "don't belong to any orgs" in low, (
        "lost the zero-org trigger — a new user who has not installed the app "
        "hits a message the skill does not recognise")


def test_the_no_orgs_case_is_not_routed_to_org_switch():
    """There is no slug to switch to. `doctor`'s org check fails for BOTH "pick
    one of several" and "you have none", so a step keyed on the check alone
    sends a brand-new user to choose from an empty list."""
    low = " ".join(_BODY.split()).lower()
    assert "there is nothing to switch to" in low or "no slug to switch to" in low, (
        "the skill no longer separates having no orgs from having several")
    assert "never a flag problem" in low, (
        "exit 2 no longer warns that the zero-org case is not a bad invocation")


def test_the_auth_step_does_not_swallow_the_missing_app_shape_of_exit_four():
    """Exit `4` has two causes with opposite recoveries: a stale credential
    (`sling login`) and an org the StarSling App was never installed on
    (install the app — logging in again is a verified no-op). The preflight's
    auth step is reached BEFORE the app gate, so an unconditional "any command
    exits 4 -> sling login" is the branch a live session hits first, and the
    user is sent round a login loop that cannot change the outcome. The
    exit-code table and the app gate already read stderr first; the auth step
    has to as well, or the contract contradicts itself in reading order."""
    low = " ".join(_BODY.split()).lower()
    auth = low.index("**not authenticated**")
    gate = low.index("starsling gate")
    assert auth < gate, (
        "the preflight reordered: this guard assumes the auth step is reached "
        "before the app gate, which is why the auth step must self-limit")
    step = low[auth:gate]
    assert "exits `4`" not in step or "does not name an org" in step, (
        "the preflight's auth step routes exit 4 to `sling login` without "
        "excluding the missing-installation shape, so a user whose app is not "
        "installed is sent to log in again before the app gate is reached")


def test_a_non_zero_exit_is_not_documented_as_an_empty_stdout():
    """Verified against v0.1.2: `sling logs` on a job that stores no logs exits
    `3` and still prints `{"lines": [], ...}`, and `sling resolve` on an
    ambiguous id exits `2` and still prints `{"candidates": [...]}`. A contract
    that says errors write nothing to stdout tells an agent to read that empty
    `lines` array as "no log lines" — the silent false negative this skill
    exists to prevent. `references/command-reference.md` already says "branch
    on the exit code, not on whether stdout looks empty"; SKILL.md must not
    say the opposite."""
    low = " ".join(_BODY.split()).lower()
    assert "writes nothing to stdout" not in low, (
        "SKILL.md still claims a genuine error writes nothing to stdout — "
        "`logs` (exit 3) and `resolve` (exit 2) both emit a full JSON body")
    assert "fall back to stderr only when stdout is empty" not in low, (
        "SKILL.md still tells the agent to key its stderr fallback on an "
        "empty stdout rather than on the exit code")
    assert "branch on the exit code" in low, (
        "SKILL.md lost the rule the reference states — branch on the exit "
        "code, never on whether stdout looks empty")


def test_exit_one_covers_a_subcommand_that_does_not_exist():
    """`sling update` — this skill's own headline gotcha — exits `1`, not `2`.
    An exit-1 row that says only "a crash ... this is a bug report, not a
    routing decision" sends an agent that typo'd a subcommand off to file a bug
    instead of reading the `unknown command` list the binary just printed."""
    low = " ".join(_BODY.split()).lower()
    row = [ln for ln in low.split("|") if "unexpected internal error" in ln]
    assert row, "the exit-1 row is gone or reworded past recognition"
    ctx = low[low.index("unexpected internal error"):][:600]
    assert "unknown command" in ctx, (
        "the exit-1 row does not mention that a nonexistent subcommand exits "
        "1 — the skill's own `sling update` gotcha lands here and the row "
        "tells the agent to treat it as a crash")


def test_the_scope_flag_gotcha_reports_the_exit_code_the_binary_returns():
    """A scope flag with no value is REJECTED, not swallowed: `sling usage
    --org --window 7d` exits `2` with `--org needs a slug (got an empty
    value).` The shipped text claimed it exits `0` and returns a wrongly-scoped
    answer that looks fine — the opposite failure mode, in the one file whose
    stated premise is that every shape was read off the binary."""
    text = " ".join(
        " ".join(t for _, t in _shipped_text()).split()).lower()
    if "--org --window" not in text:
        pytest.skip("the scope-flag gotcha is no longer documented")
    ctx = text[text.index("--org --window") - 200:][:600]
    assert "exits `0`" not in ctx, (
        "the scope-flag gotcha still says a valueless `--org` exits 0 and "
        "silently swallows the next flag; the binary exits 2 and says so")

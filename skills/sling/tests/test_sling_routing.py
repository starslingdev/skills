"""Guards for the one thing this skill is: a routing contract.

This skill ships no detectors and no report pipeline — its whole product is
prose that tells an agent which CLI answers which ask. (It is named after the
CLI it drives, so `sling` below always means the binary.) Two ways that prose
can be wrong while every other check in the repo stays green:

1. **It names a `sling` subcommand that does not exist.** This is the
   failure the skill was written to prevent (an agent inventing `sling
   rerun`), so shipping it in our own routing table would be the joke
   writing itself. It is not hypothetical: `sling doctor` on v0.1.2 emitted
   `fix_command: "sling update"` long before `sling update` existed — a
   wrong command name survives being printed by the tool itself.
2. **The read/write split drifts.** Every routing rule reduces to "does
   this change GitHub or CI state?" If a mutating verb ever routes to
   `sling`, or a GitHub-state-changing command appears in the surface while
   the skill still says no `sling` command changes GitHub, the contract is
   broken in the direction that costs the user runner-minutes or a
   cancelled deploy. A few `sling` commands change the user's own machine
   (`login`/`logout` the credential, `org switch` the default org, `update`
   the installed binary) and none changes GitHub; `login`, `logout` and
   `update` are routed to the user, never run by the agent.

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
_REFERENCES = sorted((_SKILL / "references").glob("*.md"))
_COMMAND_REF = (_SKILL / "references" / "command-reference.md").read_text()

_COMMANDS = {c for group in _SURFACE["groups"].values() for c in group}
_USER_RUN = set(_SURFACE["local_only_user_runs"]["commands"])


def _flat(text: str) -> str:
    """Whitespace collapsed to single spaces and lowercased, so a phrase check
    does not care where the prose wraps."""
    return " ".join(text.split()).lower()


_BODY_FLAT = _flat(_BODY)


def _user_run_command(row: dict) -> str | None:
    """The user-run local command (`login`, `logout`, `update`) an eval row
    names, or None when its command is anything else."""
    command = row.get("command", "")
    head = " ".join(command.split()[:2]).removeprefix("sling ")
    return head if command.startswith("sling ") and head in _USER_RUN else None

# Command mentions are read out of CODE SPANS — inline backticks and fenced
# blocks — not out of running prose. Two reasons, both learned the hard way:
# prose like "`sling logs` is a filter" put a word after a command name and had
# to be special-cased, and a prose-scoped scan could not see the frontmatter
# description at all, which is where an unreal `sling runs` was in fact shipped.
_CODE = re.compile(r"`([^`\n]+)`|```[a-z]*\n(.*?)```", re.S)
_GROUPS = {c.split()[0] for c in _COMMANDS if " " in c}


def _code_lines(text: str) -> list[str]:
    """Every inline code span, and every LINE of every fenced block — the unit
    an agent copies and runs."""
    out = []
    for inline, fenced in _CODE.findall(text):
        out += [inline] if inline else fenced.splitlines()
    return out


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
            # A real pair, or an invented subcommand under a real command.
            if len(toks) == 2 and (" ".join(toks) in _COMMANDS
                                   or toks[0] in _GROUPS or toks[0] in _COMMANDS):
                found.add(" ".join(toks))
            else:
                found.add(toks[0])
    return found


def _preflight_step(n: int) -> str:
    """The text of one numbered preflight step, and nothing else.

    Every guard below was originally written against the whole of `_BODY`, which is why
    they were all vacuous: the phrases they look for also occur in the exit-code table
    and in the references, so DELETING THE ENTIRE APP GATE left all of them green.
    A guard for a step has to read that step.
    """
    start = _BODY.index(f"\n{n}. **")
    try:
        end = _BODY.index(f"\n{n + 1}. **", start)
    except ValueError:
        end = _BODY.index("\n## ", start)
    return _BODY[start:end]


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


# Words that follow `sling` in output the binary PRINTS, where they are plain
# English rather than a subcommand. Keep this list one word long if you can:
# every entry is a word the guard can no longer catch anywhere.
_QUOTED_OUTPUT_NOUNS = {"skill"}


def _assert_only_real_commands(text: str, where: str) -> None:
    """A `sling <cmd>` a reader could act on must exist — with one allowance:
    naming a non-command in order to WARN about it is the opposite of the
    failure, so it passes only when the same file says the command is not real.
    (`sling update` was the original case, before a release added it.)

    One WORD is waved through rather than one field: `doctor`'s `agent_skill`
    row prints "sling skill installed for …", and `skill` is a noun there, not
    a subcommand a reader could route to. Exempting the whole `"detail"` value
    instead would blind the guard in the highest-risk place it has — details
    are where the binary prints commands (`fix_command: "sling update"` is
    this skill's founding anecdote) and step 2 tells the agent to read them —
    so an invented `sling rerun` inside a detail must still fail."""
    unknown = _mentions(text) - _COMMANDS - _QUOTED_OUTPUT_NOUNS
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


@pytest.mark.parametrize("ref", _REFERENCES, ids=lambda p: p.name)
def test_every_shipped_reference_names_only_commands_that_exist(ref):
    """Every reference, not just the big one: they are all linked from SKILL.md
    and all install, so a hallucination in any of them reaches a user."""
    _assert_only_real_commands(ref.read_text(), f"references/{ref.name}")


def test_the_warning_allowance_does_not_swallow_a_plain_invention():
    """Teeth check on the allowance itself: a made-up command with no warning
    beside it must still fail, or the exemption quietly disables the guard."""
    with pytest.raises(AssertionError):
        _assert_only_real_commands("Just run `sling rerun 123` to retry.", "sample")


# A `"detail"` value quoting the binary is the shape the quoted-output
# allowance exists for; anything else inside one is still routing a reader.
_DETAIL_BLOCK = (
    '```json\n'
    '{"checks": [{"key": "agent_skill", "ok": true,\n'
    '             "detail": "%s"}]}\n'
    '```\n'
)


@pytest.mark.parametrize("invention", ["frobnicate", "rerun"])
def test_a_detail_string_does_not_launder_an_invented_command(invention):
    """Teeth check on the quoted-output allowance. `doctor` details are the
    HIGHEST-risk place for a command that does not exist, not the lowest: the
    skill's founding anecdote is the binary itself printing `sling update`,
    and step 2 tells the agent to read `detail` and act on it. So exempting a
    whole `"detail"` value would blind the guard exactly where it is needed —
    `rerun` is the canonical case, the invention this whole file exists to
    stop. Only the phrase the binary really prints may be waved through."""
    with pytest.raises(AssertionError):
        _assert_only_real_commands(
            _DETAIL_BLOCK % f"run `sling {invention} 123` to fix", "sample")


def test_the_quoted_output_allowance_covers_the_detail_the_binary_prints():
    """The other direction: `doctor`'s `agent_skill` row prints "sling skill
    installed for …", so documenting that row verbatim must NOT trip the
    guard. Without this the reference cannot quote the binary at all."""
    _assert_only_real_commands(
        _DETAIL_BLOCK % "sling skill installed for Claude Code", "sample")
    _assert_only_real_commands(
        _DETAIL_BLOCK % "not installed \u2014 the sling skill lets your coding "
                        "agent run sling for you, to install:", "sample")


# The canonical state-changing actions, as the routing table names them.
_MUTATING_ACTIONS = ("rerun", "cancel", "trigger", "enable", "disable",
                     "download", "delete")
# The same actions as a USER would phrase them, for scanning eval prompts and
# the routing table's ask cells. Derived from the list above so the guards can
# never disagree about what counts as mutating, and wide on purpose: every
# synonym left out ("stop that run") is an ask that can route to `sling` unseen.
_MUTATING_VERBS = _MUTATING_ACTIONS + (
    "re-run", "kick off", "approve", "reject", "retry", "restart", "dispatch",
    "stop", "kill", "abort", "terminate", "halt", "start", "requeue", "replay",
    "resume", "pause", "retrigger", "re-trigger", "re-execute", "reexecute")
# Words a CLI command could be NAMED with if it changed GitHub or CI state —
# wider than the routing verbs, so `secrets set` and `workflow run` count. The
# name detector is only a backstop: every captured command must also carry a
# reviewed `classification` in the surface (see the no-GitHub-state test).
_MUTATING_NAME_WORDS = frozenset(_MUTATING_VERBS + (
    "set", "create", "write", "deploy", "merge", "push", "run"))


def _named_for_a_state_change(name: str) -> bool:
    """True when a command name carries a state-changing word: as a token, as
    its hyphen-joined form (`re-run` -> `rerun`), or as a plural (`reruns`).
    `runs` is a noun here (`runs list`), never the verb `run`."""
    low = name.lower()
    tokens = set(low.split()) | set(low.replace("-", " ").split()) | {
        t.replace("-", "") for t in low.split()} | {low}
    return any(t in _MUTATING_NAME_WORDS
               or (t.endswith("s") and t[:-1] != "run" and t[:-1] in _MUTATING_NAME_WORDS)
               for t in tokens)


def _inflections(verb: str) -> set[str]:
    """The verb as users write it: `cancel`, `cancels`, `cancelled`,
    `cancelling`, `retries`, `re-running`. Only the first word inflects
    (`kicking off`)."""
    head, _, tail = verb.partition(" ")
    forms = {head, head + "s", head + "ed", head + "ing"}
    if head.endswith("e"):
        forms |= {head + "d", head[:-1] + "ing"}
    if head.endswith("y"):
        forms |= {head[:-1] + "ies", head[:-1] + "ied"}
    if re.search(r"[^aeiou][aeiou][bdglmnprt]$", head):      # stop, cancel, rerun
        forms |= {head + head[-1] + "ed", head + head[-1] + "ing"}
    return {f + (" " + tail if tail else "") for f in forms}


# A verb whose object is `sling` itself ("stop sling from updating itself") is
# about the user's own machine, not GitHub or CI state, so it does not count —
# but only when `sling` is followed by a word about the binary, or ends the ask.
# `sling's` ("cancel sling's last run") names a run, and still counts.
_VERB_RE = re.compile(
    r"(?<![\w-])("
    + "|".join(sorted({re.escape(f) for v in _MUTATING_VERBS for f in _inflections(v)},
                      key=len, reverse=True))
    + r")(?![\w-])"
    r"(?!\s+sling(?:\s+(?:from|itself|auto-update|updating|background)\b|\s*$))")
# A clause that opens as a question asks ABOUT a state change ("why did the job
# abort", "when did the run start"); it does not ask for one.
_QUESTION_OPENER = re.compile(
    r"^(?:did|does|do|was|were|is|are|has|have|had|why|when|how|what|which|where|who)\b")
_ASKED_FOR = re.compile(
    r"(?:\bplease|\bcan you|\bcould you|\bwould you|\bi want to|\bi'd like to|\bi need to)\s+$")
_CLAUSE_LEAD = re.compile(r"^(?:(?:and|then|also|now|so|just)\s+)*")
# Asking HOW to perform an action ("how do I cancel a run", "how to trigger
# the deploy") is asking to change state, even though it opens as a question.
_HOW_TO = re.compile(r"^how\s+(?:(?:do|can|could|should|would)\s+(?:i|we)|to)\s+$")


def _is_mutating_ask(prompt: str) -> bool:
    """True when an action word is used as an INSTRUCTION: clause-initial
    (imperative), or right after please / can you / I want to / to. In a
    clause that opens as a question ("why did the job abort", "why is my job
    slow to start") the word is described, not requested, so only the
    please / can you form and how-do-I / how-to count there."""
    low = _flat(prompt.replace("`", "").replace("*", ""))
    for clause in re.split(r"[.?!;,:()—]+", low):
        clause = _CLAUSE_LEAD.sub("", clause.strip())
        question = bool(_QUESTION_OPENER.match(clause))
        for m in _VERB_RE.finditer(clause):
            before = clause[:m.start()]
            if _ASKED_FOR.search(before) or _HOW_TO.match(before):
                return True
            if not question and (not before.strip() or re.search(r"\bto\s+$", before)):
                return True
    return False


def test_state_change_name_detector_catches_known_shapes():
    """Self-test of the detectors the state-change guards lean on. Most names
    below are hyphenated, plural or synonym forms that an exact match against
    the seven routing-table actions (`rerun`, `cancel`, ...) would miss. The
    asks are user phrasings a narrower verb list let route to `sling` or to
    nothing with the suite green; the negatives are reads and asks about
    `sling` itself."""
    for bad in ("rerun", "runs re-run", "re-run", "retry", "runs retry", "dispatch",
                "approve", "secrets set", "workflow run", "reruns", "runs dispatch",
                "cancel-run", "runs delete", "restart", "runs requeue", "replay",
                "runs resume", "pause", "runs stop", "kill"):
        assert _named_for_a_state_change(bad), bad
    for ok in sorted(_COMMANDS):
        assert not _named_for_a_state_change(ok), f"`{ok}` misread as state-changing"
    for ask in ("stop that run", "abort the deploy run", "kill the job",
                "halt the nightly workflow", "pause that workflow",
                # inflections and synonyms a bare-verb list let through
                "cancelling the deploy run now", "retrigger the failed build",
                "re-execute the failed jobs", "re-running the failed jobs",
                "can you terminate the deploy", "i want to restart the build",
                # `sling's` names a run, not the binary
                "cancel sling's last run", "stop sling's nightly deploy run",
                # asking HOW to change state is still a state-change ask
                "how do I cancel a run", "how can I re-run the failed jobs",
                "how to trigger the deploy workflow"):
        assert _is_mutating_ask(ask), ask
    for ask in ("stop sling from updating itself", "what triggers does this workflow have",
                "why did this job fail",
                # read-only diagnosis: the action word is described, not requested
                "why is my job slow to start", "why did the job abort",
                "why did my job stop at step 3", "how many times did this job retry",
                "when did the run start", "how long did the job take to start",
                "how many times did the deploy restart"):
        assert not _is_mutating_ask(ask), ask


@pytest.mark.parametrize("verb", _MUTATING_ACTIONS)
def test_every_mutating_verb_is_routed_to_gh(verb: str):
    """Each state-changing ask must appear in the routing table on a `gh` row.
    A verb that falls out of the table is a verb an agent will improvise."""
    rows = [ln for ln in _BODY.splitlines()
            if ln.startswith("|") and verb in ln.lower()]
    assert rows, f"the routing table no longer covers {verb!r}"
    assert any("`gh" in ln for ln in rows), (
        f"{verb!r} appears in the routing table but not on a `gh` row")


def _routing_section() -> str:
    i = _BODY.index("## The routing table")
    return _BODY[i:_BODY.index("**Ambiguous asks.**", i)]


def _routing_table_rows() -> list[list[str]]:
    """The routing table's data rows, split into cells on unescaped pipes."""
    rows = []
    for ln in _routing_section().splitlines():
        if ln.startswith("|") and not ln.startswith("|---"):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", ln)[1:-1]]
            if cells and cells[0] != "User is asking about...":
                rows.append(cells)
    return rows


def test_every_state_change_row_in_the_routing_table_routes_to_gh():
    """Per row, not per verb. The per-verb check above is satisfied by ANY `gh`
    row naming the verb, so a second "Cancel a stuck run quickly" row routed to
    `sling` sat beside the real one with the suite green."""
    rows = _routing_table_rows()
    assert len(rows) >= 20, f"the routing table parsed to only {len(rows)} rows"
    hits = [r for r in rows if _is_mutating_ask(r[0].replace("*", ""))]
    assert len(hits) >= 6, f"only {len(hits)} state-change rows found: {hits}"
    for ask, route, command in (r[:3] for r in hits):
        assert route == "`gh`" and command.startswith("`gh "), (
            f"routing-table row {ask!r} is a state change routed to {route} / "
            f"{command[:40]!r}; state changes route to `gh`")


def test_the_change_question_sends_state_changes_to_gh_directly():
    """Decision step 1 is the sentence an agent reads before the table. If it
    softens to "try `sling` first, then `gh`", every state change costs a
    failed or invented `sling` call first."""
    sec = _routing_section()
    step1 = _flat(sec[sec.index("**1."):sec.index("**2.")])
    assert ("use `gh` directly; do not attempt a `sling` subcommand for these "
            "under any name") in step1, (
        "decision step 1 no longer sends state changes straight to `gh`")
    assert not re.search(r"\b(one|only) local change", _BODY_FLAT), (
        "the body calls `sling update` the one (or only) local change; `login`, "
        "`logout` and `org switch` change local state too")
    assert _mentions(step1) <= _USER_RUN, (
        f"decision step 1 names `sling` command(s) {sorted(_mentions(step1) - _USER_RUN)}; "
        f"it names only the user-run local commands, never a sling route for a state change")


def test_no_github_state_change_claim_matches_the_captured_surface():
    """If a sling release ships a command that changes GitHub or CI state, the
    capture changes and this fails — before the skill can keep telling users
    no `sling` command changes GitHub.

    The claim is "nothing in `sling` touches GitHub", not "sling changes
    nothing": `sling update` reinstalls sling on the user's machine. The
    local-only commands are pinned separately, as user-run, by the next test."""
    assert _SURFACE["changes_github_state"] is False
    assert _SURFACE["github_state_changing_commands"] == []
    # Teeth that do not depend on the hand-kept list above: no captured command
    # may be named for a GitHub state change (`run cancel`, `rerun`, ...). A
    # release that adds one must be re-classified on purpose, not slip through.
    named_for_a_state_change = sorted(
        c for c in _COMMANDS if _named_for_a_state_change(c))
    assert not named_for_a_state_change, (
        f"the surface captures GitHub-state-changing command(s) "
        f"{named_for_a_state_change} while the skill says no sling command changes "
        f"GitHub. Route them to the user or to gh, and reshape this contract.")
    assert not _USER_RUN & set(_SURFACE["github_state_changing_commands"]), (
        "a command is classed as both local-only and GitHub-state-changing")
    # The name detector only catches names that LOOK like a state change. The
    # real gate is a reviewed class per captured command: a new command fails
    # here until someone decides, on purpose, what it changes.
    cls = {k: v for k, v in _SURFACE["classification"].items() if not k.startswith("_")}
    assert set(cls) == _COMMANDS, (
        f"command(s) with no reviewed classification: {sorted(_COMMANDS - set(cls))}; "
        f"classified but not captured: {sorted(set(cls) - _COMMANDS)}")
    allowed = {"read_only", "local_only", "local_only_user_runs"}
    assert set(cls.values()) <= allowed, (
        f"unknown classification(s) {sorted(set(cls.values()) - allowed)}; a GitHub "
        f"state change has no class here because the contract would change with it")
    assert {c for c, v in cls.items() if v == "local_only_user_runs"} == _USER_RUN, (
        "classification and local_only_user_runs disagree about who runs what")
    # The description carries the CONSEQUENCE (state changes route to gh); the
    # mechanism lives in the body. It must be the backticked `gh`, in the same
    # clause: a bare "gh" also matches inside "GitHub".
    assert re.search(r"state changes?[^.]{0,60}`gh`", _FRONT["description"]), (
        "the description no longer says state changes route to `gh` in one clause")
    assert "no `sling` command changes github" in _BODY_FLAT, (
        "the body no longer states that no sling command changes GitHub")
    assert "no `sling` command that changes anything" not in _BODY.lower(), (
        "the body claims sling changes nothing at all — `sling update` does, locally")


def test_local_only_commands_are_routed_to_the_user():
    """`login`, `logout` and `update` change only the user's machine — a
    credential, an installed binary — and the user runs all three. If the skill
    ever stops saying so, or an eval routes one to the agent, an agent reinstalls
    sling, signs the user out (only they can sign back in), or hangs on a device
    code on the user's behalf. `logout` was once classed agent-runnable; the
    classification check in the previous test pins it here."""
    assert {"login", "logout", "update"} <= _USER_RUN, (
        f"local-only group lost a command: {sorted(_USER_RUN)}")
    assert "the user runs `login`, `logout` and `update`, never you" in " ".join(
        _BODY.split()), "the intro no longer hands login, logout and update to the user"
    for cmd in _USER_RUN:
        assert cmd in _COMMANDS, f"local-only `{cmd}` is not in the captured surface"
        assert f"ask the user to run `sling {cmd}`" in _BODY_FLAT, (
            f"SKILL.md no longer hands `sling {cmd}` to the user")
    for row in _ROUTING["routings"]:
        head = _user_run_command(row)
        if head:
            assert row["route"] == "user", (
                f"{row['prompt']!r} routes the local-only `sling {head}` to "
                f"{row['route']!r}; the user runs it, never the agent")
    table = [ln for ln in _BODY.splitlines()
             if ln.startswith("|") and "`sling update" in ln]
    assert table, "the routing table no longer covers `sling update`"
    assert all(ln.split("|")[2].strip() == "the user" for ln in table), (
        "a routing-table row puts `sling update` in someone else's hands than the user's")


def test_scope_flag_rules_match_the_captured_surface():
    """`--org`/`--repo` are per-command, and three rules were wrong on both
    0.1.8 and 0.1.124 before this guard: `time` rejects `--org` outright,
    four list/cost commands reject the pair together, and no command reads
    `--repo` from the git remote. The first two are pinned to the capture's
    `scope_flags` lists, in the step that tells an agent which flag to reach
    for and in the reference. The git-remote rule has no capture field: it is
    pinned as prose, and the next test bans its inversion everywhere."""
    sf = _SURFACE["scope_flags"]
    assert "time" in sf["rejects_org"], (
        "the 0.1.124 capture showed `sling time --org` exits 2; re-verify on the "
        "binary before removing it")
    assert "logs" in sf["rejects_org_and_repo"]
    step4 = _flat(_preflight_step(4))
    glob = _COMMAND_REF[_COMMAND_REF.index("## Global flags"):
                        _COMMAND_REF.index("## The precondition")]
    for cmd in sf["rejects_org"]:
        assert f"`sling {cmd}` rejects `--org`" in step4, (
            f"step 4 no longer says `sling {cmd}` rejects --org")
        rows = [ln for ln in glob.splitlines() if ln.startswith(f"| `sling {cmd}`")]
        assert rows and "rejected" in rows[0].split("|")[2], (
            f"the reference's scope-flag table no longer says `sling {cmd}` rejects --org")
        heads = [ln for ln in _COMMAND_REF.splitlines() if ln.startswith(f"### `sling {cmd}")]
        assert heads and not any("--org" in h for h in heads), (
            f"a `sling {cmd}` heading in the reference offers --org, which the binary "
            f"rejects with exit 2")
        assert f"`{cmd}` rejects --org" in _SURFACE["global_flags_note"]
    assert {"runs list", "jobs list", "usage", "top"} <= set(sf["rejects_org_with_repo"]), (
        "the capture lost a not-both command; the 0.1.124 binary rejects --org with "
        "--repo on runs list, jobs list, usage and top")
    for cmd in sf["rejects_org_with_repo"]:
        assert f"`{cmd}`" in step4, f"step 4 no longer names `{cmd}` among the not-both commands"
        assert any(cmd in ln and "not both" in ln for ln in glob.splitlines()), (
            f"the reference's scope-flag table no longer says `{cmd}` rejects --org with --repo")
    # The rejection itself, quoted, beside the command list — not just the words
    # "not both", which "the old not both error is gone" also contains.
    not_both = "reject both together (`use --org or --repo, not both.`)"
    assert not_both in step4, (
        "step 4 lost the quoted not-both rejection next to its command list")
    rest = step4[step4.index(not_both) + len(not_both):]
    rest = rest[:rest.index(".")] if "." in rest else rest
    assert not re.search(r"\b(except|accepts?)\b", rest), (
        f"step 4's not-both sentence carves out an exception: {rest.strip()!r}")
    assert "no command fills in `--repo` from the git remote" in step4, (
        "step 4 no longer says --repo is never read from the git remote")
    assert "for \"my\" or \"this repo\" asks, pass `--repo` from the checkout's `origin`" \
        in step4, "step 4 lost the instruction to pass --repo from the checkout's origin"
    # `doctor`'s `git_remote` check DOES read the remote, so the absolute form
    # of the claim is false and must not come back.
    assert "no command reads the git remote" not in step4, (
        "step 4 claims no command reads the git remote; `doctor`'s git_remote does")


def test_the_scope_flag_paragraph_parses_into_real_code_spans():
    """Inline spans are matched within one line, so a span wrapped across a line
    break (`` `sling\\n   time` ``) shifts every backtick after it: the parser
    reads " rejects " as code and `--org` as prose, and a `sling time --org`
    example on that line escapes the scope-flag ban. Feed the real step-4 text
    through the parser and require real spans."""
    step4 = _preflight_step(4)
    spans = [inline or fenced for inline, fenced in _CODE.findall(step4)]
    assert spans, "step 4 parsed to no code spans at all"
    edged = [s for s in spans if s != s.strip()]
    assert not edged, f"step 4 parses prose as code (a span is wrapped or unbalanced): {edged}"
    assert "rejects" not in {s.strip() for s in spans}, (
        "step 4 parses the word `rejects` as a code span")
    flat = " ".join(step4.split())
    assert len(_sling_spans(step4, "time")) == flat.count("`sling time"), (
        "a `sling time` span in step 4 is not parsed as code, so the scope-flag "
        "ban cannot see it")


# A claim that the CLI fills --repo in from the checkout. Only wording where the
# CLI supplies the value is banned — "falls back / defaults / inferred /
# auto-detect / picks up / uses … origin|remote", or "optional" beside the
# remote. "The `--repo` you pass is taken from the checkout's `origin`" is the
# true rule (the AGENT reads it there), so "read from" / "taken from" are not
# triggers. A negation counts only directly before the verb: "If `--repo` is not
# given, it falls back to `origin`" is a claim, whatever "not" sits elsewhere.
_REPO_FILL = re.compile(
    r"\b(?:falls?\s+back|fell\s+back|defaults?|defaulted|infers?|inferred|"
    r"auto-?detect(?:s|ed)?|picks?\s+up|picked\s+up|uses?|used)\b"
    r"[^.]{0,60}?(?:git remote|\borigin\b|\bremote\b)")
_NEGATED_BEFORE = re.compile(r"(?:\b(?:never|not|no|nothing)|n't)\s+$")


def _repo_from_git_claims(text: str) -> list[str]:
    low = _flat(text).replace("`", "")
    hits = []
    for sentence in re.split(r"(?<=[.!?])\s+", low):
        hits += [m.group(0) for m in _REPO_FILL.finditer(sentence)
                 if not _NEGATED_BEFORE.search(sentence[:m.start()])]
        if re.search(r"\boptional\b", sentence) and re.search(r"\b(?:origin|remote)\b", sentence):
            hits.append(sentence)
    return hits


def test_no_shipped_text_says_repo_comes_from_the_git_remote():
    """A phrase ban caught one wording ("defaults to the git remote") and let
    "omit `--repo`: it falls back to the checkout's `origin`" through. Without
    `--repo`, `runs list`, `usage` and `top` widen to the whole org, while
    `jobs list` (with no `--run` or window) and `time`'s repo view exit 2 — so
    any such claim gives a wrongly-scoped answer that looks fine, or a usage
    error the agent was told could not happen. Checked across SKILL.md
    (frontmatter included), every reference and every eval file."""
    for claim in ("omit `--repo`: it falls back to the checkout's `origin`.",
                  "If `--repo` is not given, it falls back to the checkout's `origin`.",
                  "Inside a checkout `--repo` is optional: sling uses the checkout's `origin`.",
                  "Inside a checkout, sling infers the repo from `origin`, so `--repo` is optional.",
                  "read; --repo defaults to the git remote"):
        assert _repo_from_git_claims(claim), claim
    for true in ("`--repo` is never read from the git remote.",
                 "Inside a checkout, the `--repo` you pass is taken from the checkout's `origin`.",
                 "For \"my\" or \"this repo\" asks, pass `--repo` from the checkout's `origin`.",
                 "`--repo` never defaults to the git remote."):
        assert not _repo_from_git_claims(true), true
    for where, text in _shipped_text() + _eval_texts():
        hits = _repo_from_git_claims(text)
        assert not hits, f"{where} says --repo comes from the git remote: {hits}"


def _invocations(span: str, cmd: str) -> list[str]:
    """Each place one span invokes `sling <cmd>`, from the command name to the
    end of the span."""
    return [span[m.start():]
            for m in re.finditer(rf"\bsling {re.escape(cmd)}(?![\w-])", span)]


def _sling_spans(text: str, cmd: str) -> list[str]:
    """`_invocations` across every code span in a document."""
    return [tail for span in _code_lines(text) for tail in _invocations(span, cmd)]


# `--org` and `--repo` together, with placeholders or real values
# (`--org acme --repo acme/web`). Every command that takes both rejects the pair.
_BOTH_SCOPE_FLAGS = re.compile(
    r"--org\s+(?!(?:or|and|\|)\s)\S+\s+--repo|--repo\s+(?!(?:or|and|\|)\s)\S+\s+--org")


def _scope_flag_violations(span: str) -> list[str]:
    """What one runnable command string gets wrong about the scope flags: a
    flag its command rejects (`sling time --org`, `sling logs --repo`), or
    `--org` beside `--repo`. Shared by the doc check and the eval check, so the
    two can never disagree about what is banned."""
    sf = _SURFACE["scope_flags"]
    banned = {c: ("--org",) for c in sf["rejects_org"]}
    for c in sf["rejects_org_and_repo"]:
        banned[c] = ("--org", "--repo")
    out = []
    for cmd, flags in banned.items():
        for tail in _invocations(span, cmd):
            out += [f"`{tail[:70]}` passes {flag}, which `sling {cmd}` rejects with exit 2"
                    for flag in flags if flag in tail]
    if _BOTH_SCOPE_FLAGS.search(span):
        out.append(f"`{span[:70]}` passes --org together with --repo, which every "
                   f"command rejects")
    return out


def test_no_example_passes_a_scope_flag_the_command_rejects():
    """Prose said `time` rejects `--org` while the table could still show
    `sling time --org <slug>`, and a logs row could gain `--org`, with the
    suite green. The examples are what an agent copies, so ban the flag in
    every code span that runs those commands, and in every eval row's
    `command` + `args`. The both-flags pattern bans `--org` and `--repo` one
    value apart, in either order, placeholder or real (`--org acme --repo
    acme/web`); a pair split across two spans is not caught. A routing-table
    row that names a command rejecting `--org` may not mention `--org` in its
    Command cell at all, since that cell is what an agent copies."""
    sf = _SURFACE["scope_flags"]
    assert sf["rejects_org"] and sf["rejects_org_and_repo"], (
        "no scope-flag rejections captured; the guard would be vacuous")
    assert _scope_flag_violations("sling usage --org acme --repo acme/web")
    assert _scope_flag_violations("sling time --repo <owner/name> --org <slug>")
    assert not _scope_flag_violations("sling usage [--org <slug> | --repo <owner/name>]")
    assert not _scope_flag_violations("Use --org or --repo, not both.")
    for where, text in _shipped_text():
        for span in _code_lines(text):
            bad = _scope_flag_violations(span)
            assert not bad, f"{where}: {bad}"
    for r in _ROUTING["routings"]:
        if r.get("command", "").startswith("sling "):
            bad = _scope_flag_violations(f"{r['command']} {r.get('args', '')}".strip())
            assert not bad, f"evals/prompt-routing.json {r['prompt']!r}: {bad}"
    for ask, _route, command in (row[:3] for row in _routing_table_rows()):
        for cmd in sf["rejects_org"]:
            if re.search(rf"`sling {re.escape(cmd)}(?![\w-])", command):
                assert "--org" not in command, (
                    f"routing-table row {ask!r} mentions --org beside `sling {cmd}`, "
                    f"which rejects it with exit 2: {command[:90]!r}")


def test_routing_evals_agree_with_the_surface_and_the_split():
    for r in _ROUTING["routings"]:
        route, prompt = r["route"], r["prompt"]
        assert route in {"sling", "gh", "user", "ci-score", "ci-speedup",
                         "ci-secure", "file-edit", "none"}, prompt
        if route == "sling":
            cmd = r["command"].removeprefix("sling ")
            assert cmd in _COMMANDS, f"{prompt!r} routes to unknown `sling {cmd}`"
            assert cmd not in _USER_RUN, (
                f"{prompt!r} has the agent run the user-run `sling {cmd}`")
        if route == "user":
            assert _user_run_command(r), (
                f"{prompt!r} routes to the user a command that is not local-only")
        if route == "gh":
            assert r["command"].startswith("gh "), prompt
        # Optional flags for the command, kept apart from it so `command` is
        # always a bare command the membership checks above can test.
        if "args" in r:
            assert route in {"sling", "user"} and isinstance(r["args"], str), prompt
            assert r["args"].startswith("--"), (
                f"{prompt!r}: `args` holds flags only; the command goes in `command`")


def test_no_mutating_ask_routes_to_sling():
    """The one-line version of the whole contract. A state-change ask goes to
    `gh` and nowhere else: `!= "sling"` alone let one be handed to the user as
    "run `sling update`", or dropped as `none`, with the suite green."""
    hits = [r for r in _ROUTING["routings"] if _is_mutating_ask(r["prompt"])]
    assert len(hits) >= 5, f"only {len(hits)} state-change asks in the evals"
    for r in hits:
        assert r["route"] == "gh" and r.get("command", "").startswith("gh "), (
            f"{r['prompt']!r} routes a GitHub state change to {r['route']!r}; "
            f"state changes route to `gh`")


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
    assert any(k in low for k in ("do not trigger", "do not use", "not for")), (
        "description lost its negative-trigger clause")


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
    for ref in _REFERENCES:
        text = ref.read_text()
        if len(text.splitlines()) > 100:
            head = "\n".join(text.splitlines()[:25]).lower()
            assert "contents" in head, f"{ref.name} is long but has no contents block"


def _shipped_text() -> list[tuple[str, str]]:
    """SKILL.md (frontmatter included) and every reference, by relative path."""
    return [("SKILL.md", _SKILL_MD)] + [
        (f"references/{r.name}", r.read_text()) for r in _REFERENCES]


def _eval_texts() -> list[tuple[str, str]]:
    """The free text of every eval row (`prompt`, `args`, `why`) in every eval
    file. Evals ship with the skill and are read as examples, so the shipped-text
    bans apply to them too."""
    out = []
    for p in sorted((_SKILL / "evals").glob("*.json")):
        data = json.loads(p.read_text())
        for row in data.get("routings", []) + data.get("evals", []):
            for key in ("prompt", "args", "why"):
                if isinstance(row.get(key), str):
                    out.append((f"evals/{p.name} {row.get('prompt', '')[:40]!r} {key}", row[key]))
    return out


def test_login_is_never_shown_with_the_machine_mode_flag():
    """`sling login` needs a human at a browser to approve a device code, and
    `--agent` carries `--no-input` — so the flag turns the one command that
    requires a person into exit `2`. Any shipped example pairing them teaches
    an agent to hang the session on a prompt it has already refused. The evals
    count: no user-run command's eval row carries `--agent` in its `args`, and
    its `why` may name `--agent` only as a prohibition ("never with --agent")."""
    for r in _ROUTING["routings"]:
        head = _user_run_command(r)
        if not head:
            continue
        assert "--agent" not in f"{r['command']} {r.get('args', '')}", (
            f"{r['prompt']!r} gives the user-run `sling {head}` the --agent flag")
        for key in ("prompt", "why"):
            text = r.get(key, "").lower()
            for m in re.finditer(r"--agent", text):
                assert re.search(r"\b(never|not|no|without)\b(?:\s+\S+){0,2}\s+$",
                                 text[:m.start()]), (
                    f"{r['prompt']!r} {key} pairs `sling {head}` with --agent: {text!r}")
    for where, text in _shipped_text() + _eval_texts():
        for line in text.splitlines():
            # The dangerous shape is the RUNNABLE literal: `sling login --agent` as a
            # contiguous invocation an agent can copy. Warnings phrase the pairing the
            # other way around ("never pass `--agent` to `sling login`"), so the
            # literal is banned outright, with no negation exemption to hide behind.
            assert "sling login --agent" not in line, (
                f"{where}: a runnable `sling login --agent` ships — the exit-2 hang "
                f"this guard exists to block — {line.strip()[:90]}")


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


def test_the_app_gate_survives_in_the_step_that_owns_it():
    """Scoped to step 5, because a global search could not see it disappear.

    Deleting the entire gate — both failure shapes, the STOP, the install path, the
    organizations-only rule — left all 36 guards green, because every phrase they
    looked for also occurs in the exit-code table or a reference. A guard for the
    centrepiece of this skill has to read the centrepiece.
    """
    step = _flat(_preflight_step(5))
    assert "github.com/apps/starslingdev" in step, (
        "step 5 no longer gives the app install path")
    assert "stop" in step, "step 5 no longer tells the agent to stop and explain"
    assert "organizations only" in step or "organisations only" in step, (
        "step 5 no longer says the app does not work on personal repositories")
    assert "don't have access to org" in step and "don't belong to any orgs" in step, (
        "step 5 no longer recognises both shapes of a missing installation")


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
    """Scoped per file to the gh gate itself, not to the document.

    `gh` authenticates separately from `sling`, and ci-speedup's gate already encodes
    what this costs to get wrong — including a live miss: a sandboxed agent shell
    cannot reach keyring credentials, so one failed probe proves nothing.

    Every assertion here used to search SKILL.md and the reference as one blob, which
    made three of them vacuous: "is installed" was satisfied by the StarSling App's own
    prose, and "sandbox" by step 5's caution about sling's auth probe. Both gh gates
    could lose their install check and their sandbox caution with the suite green.
    """
    gh_body = _BODY[_BODY.index("**gh gate,"):]
    gh_body = gh_body[:gh_body.index("\n## ")] if "\n## " in gh_body else gh_body
    ref = (_SKILL / "references" / "gh-fallback.md").read_text()
    ref_gate = ref[ref.index("Before the first `gh` call"):ref.index("## State changes")]

    for where, text in (("SKILL.md's gh gate", gh_body), ("gh-fallback.md's gate", ref_gate)):
        low = _flat(text)
        assert "installed" in low, (
            f"{where} no longer checks whether gh is INSTALLED — a different failure "
            "from being signed out, with the same consequence")
        assert "gh auth status" in low, f"{where} lost its auth probe"
        assert "sandbox" in low, (
            f"{where} lost the sandboxed-shell caution ci-speedup recorded from a live "
            "miss — without it a Codex shell reports a false auth failure")
    # The install path is given once, in the gate the agent reads first.
    assert re.search(r"https://cli\.github\.com(?![\w.@:-])", _flat(gh_body)), (
        "SKILL.md's gh gate no longer gives the install path, bounded against a host "
        "that could be extended — including the `user@host` form, where everything "
        "before the @ is userinfo and the real host is whatever follows")


def test_the_empty_listing_rule_survives_in_the_gotcha_that_states_it():
    """Scoped to the bullet, and asserting the semantic clause.

    A global search for "coverage hole" and "did not run" was satisfied by a
    parenthetical, so the gotcha could be replaced with its INVERSION — "report it as:
    this repo had no CI runs" — and stay green. That is the exact false finding the
    rule exists to stop.
    """
    low = _BODY_FLAT
    i = low.index("empty listing")
    bullet = low[i:i + 700]
    assert "coverage hole" in bullet, "the empty-listing rule lost its coverage-hole framing"
    assert "never a finding" in bullet or "not a finding" in bullet, (
        "the empty-listing rule no longer says an empty result is not a finding")
    assert "did not run" in bullet, "the empty-listing rule no longer says the check did NOT run"


def test_the_no_orgs_case_is_not_routed_to_org_switch():
    """There is no slug to switch to. `doctor`'s org check fails for BOTH "pick
    one of several" and "you have none", so a step keyed on the check alone
    sends a brand-new user to choose from an empty list."""
    low = _BODY_FLAT
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
    low = _BODY_FLAT
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
    low = _BODY_FLAT
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
    """A subcommand that does not exist (`sling nosuchcmd`) exits `1`, not `2`.
    An exit-1 row that says only "a crash ... this is a bug report, not a
    routing decision" sends an agent that typo'd a subcommand off to file a bug
    instead of reading the `unknown command` list the binary just printed."""
    low = _BODY_FLAT
    row = [ln for ln in low.split("|") if "unexpected internal error" in ln]
    assert row, "the exit-1 row is gone or reworded past recognition"
    ctx = low[low.index("unexpected internal error"):][:600]
    assert "unknown command" in ctx, (
        "the exit-1 row does not mention that a nonexistent subcommand exits "
        "1 — an invented name lands here and the row tells the agent to treat "
        "it as a crash")


def test_the_scope_flag_gotcha_reports_the_exit_code_the_binary_returns():
    """A scope flag with no value is REJECTED, not swallowed: `sling usage
    --org --window 7d` exits `2` with `--org needs a slug (got an empty
    value).` The shipped text claimed it exits `0` and returns a wrongly-scoped
    answer that looks fine — the opposite failure mode, in the one file whose
    stated premise is that every shape was read off the binary."""
    text = " ".join(
        " ".join(t for _, t in _shipped_text()).split()).lower()
    if "--org --window" not in text:
        pytest.fail(
            "the scope-flag gotcha is gone — it documents a verified binary fact "
            "(a valueless --org is rejected with exit 2), and deleting it is a "
            "strictly easier way to reintroduce the old exits-0 claim than editing it")
    ctx = text[text.index("--org --window") - 200:][:600]
    assert "exits `0`" not in ctx, (
        "the scope-flag gotcha still says a valueless `--org` exits 0 and "
        "silently swallows the next flag; the binary exits 2 and says so")


def test_exit_four_is_disambiguated_where_the_agent_first_meets_it():
    """The carve-out has to live in the AUTH step, not only in the app step.

    Exit 4 arrives two ways — a stale credential and an org the StarSling app was
    never installed on — and the preflight reaches authentication (step 3) before
    it reaches the app gate (step 5). Without a carve-out at step 3, the first
    step to match claims every exit 4 and sends the second case round a login loop
    that cannot change the outcome. That was greptile's P1 on this PR, and nothing
    guarded the fix: deleting the sentence left all 36 tests green.
    """
    auth_step = _BODY[_BODY.index("3. **Not authenticated"):_BODY.index("4. **Wrong org")]
    low = _flat(auth_step)
    assert "exit `4`" in low or "exit 4" in low, "step 3 no longer names the code it shares"
    assert "step 5" in low, (
        "the auth step no longer hands the missing-installation case to the app gate, "
        "so it claims every exit 4 and loops the user on login")


def test_the_wrong_org_step_hands_off_the_empty_case():
    """`doctor`'s org check fails for BOTH 'pick one of several' and 'you have none'.

    A step keyed on that check alone answers a brand-new user with
    `sling org switch <slug>` — choosing from an empty list. The hand-off is the
    only thing separating them, and deleting it left every test green.
    """
    org_step = _BODY[_BODY.index("4. **Wrong org"):_BODY.index("5. **StarSling gate")]
    low = _flat(org_step)
    assert "step 5" in low, (
        "the wrong-org step no longer hands the no-orgs case to the app gate, so a "
        "user with zero orgs is told to switch to one of them")


def test_the_app_install_url_is_right_in_every_file_that_gives_it():
    """One correct occurrence must not vouch for a wrong one elsewhere.

    The earlier guard concatenated SKILL.md and the references and asked whether the
    URL appeared anywhere, so SKILL.md could send users to the wrong host while a
    reference kept the right one — and the suite stayed green. Each file that names
    an install URL for the app has to name the right one.
    """
    expected = "https://github.com/apps/starslingdev"
    files = [("SKILL.md", _BODY)] + [(r.name, r.read_text()) for r in _REFERENCES]
    named = [(name, text) for name, text in files if "github.com/apps/" in text]
    assert named, "no file gives an install URL for the StarSling GitHub App"
    wrong = [name for name, text in named if expected not in text]
    assert not wrong, (
        f"{', '.join(wrong)} names a GitHub App install URL that is not {expected}")


def test_the_unreachable_org_case_rules_out_a_typo_first():
    """Exit 4 with that message does not prove the org exists.

    A slug that could never exist returns byte-identically to one whose org simply
    lacks the app — verified against the binary. Reading it only as "the app is not
    installed" sends someone to install a GitHub App on an organization they
    mistyped, and `sling org switch <slug>` settles it for free by printing the
    orgs they actually have.
    """
    step = _flat(_preflight_step(5))
    assert "typo" in step, (
        "step 5 no longer tells the agent to rule out a mistyped slug before "
        "reading exit 4 as a missing installation")
    assert "org switch" in step, (
        "step 5 no longer names the disambiguator that lists the user's real orgs")


def test_the_scope_flags_are_not_claimed_to_be_universal():
    """`sling logs` rejects `--org` and `--repo` with exit 2, and it is the command
    the routing table reaches for most. Claiming they are global on every subcommand
    turns a working logs read into a usage error on the recovery path."""
    low = _BODY_FLAT
    assert "global flags on every subcommand" not in low, (
        "the scope flags are documented as universal again — `sling logs` rejects them")
    assert "not by `sling logs`" in low, (
        "the strict-parser exception for `sling logs` is gone")


def test_agent_is_not_described_as_an_alias_for_flags_it_does_not_have():
    """The reference's stated premise is that it was read off the binary.

    `--compact` does not exist in v0.1.8 — the published docs call `--agent`
    "exactly equivalent to --json --compact --no-input --no-color --yes", and
    repeating that attributes to the tool a contract no help page states.
    """
    low = _BODY_FLAT
    i = low.index("`--agent` is the machine-mode flag")
    section = low[i:i + 900]
    assert "do not describe it as an alias" in section, (
        "the caution against restating the docs' flag-alias claim is gone")


def test_the_description_keeps_its_measured_trigger_properties():
    """This description was chosen by measurement, not judgement — keep what won.

    The documented gentle shape (capability clause + short "Use when") measured 0%
    on real prompts: "why did this job fail?" with an Actions URL was answered by
    grepping a whole job log with `gh`, in two live dogfoods and in direct
    `claude -p` probes. The same probes showed the channel works — an imperative
    claim over the default path fired 4/4 with `Skill:sling` as the FIRST tool
    call, while both near-miss negatives (a repo-wide grade, a YAML question)
    still routed away correctly.

    So the properties pinned here are the measured winners: the imperative
    invoke-before-`gh` claim, the trigger terms, the pasted-URL cue, and the
    negative clause. Softening these back toward the documented shape is the
    regression the measurements exist to prevent — re-measure before changing
    them (direct `claude -p` probes; the eval harness's positive control failed,
    so its numbers are void).
    """
    d = _FRONT["description"]
    low = d.lower()
    # Probed vacuous as a bare substring: "Consider using it, before or after `gh`"
    # kept the suite green while inverting the measured imperative. Require the shape.
    assert re.search(r"\b[Ii]nvoke this BEFORE[^.]{0,40}`gh`", d), (
        "the description lost its invoke-BEFORE-`gh` imperative — the single feature "
        "that took the trigger rate from 0% to 4/4")
    for term in ("failed", "runner minutes", "GitHub Actions URL"):
        assert term in d, f"the description lost the trigger term {term!r}"
    assert "re-run" in low or "rerun" in low, "the state-change cue is gone"


def test_no_hyphenated_term_is_split_by_the_yaml_fold():
    """A `>-` block folds newlines into spaces, so a term wrapped across the fold
    silently becomes two words in the value the router actually reads.

    `read-only` split this way and turned into `read- only`, which a guard caught only
    because it happened to assert on that exact term. Anything not asserted on would
    have shipped mangled and invisible.
    """
    raw = (_SKILL / "SKILL.md").read_text()
    front_lines = raw[:raw.index("\n---", 4)].splitlines()
    desc_lines = []
    inside = False
    for line in front_lines:
        if line.startswith("description:"):
            inside = True
            continue
        if inside:
            if line and not line.startswith("  "):
                break
            desc_lines.append(line)
    broken = [ln for ln in desc_lines if ln.rstrip().endswith("-")]
    assert not broken, (
        "description line(s) end in a hyphen, so the fold will join them into two "
        f"words: {[ln.strip()[-30:] for ln in broken]}")


def test_a_foreign_org_url_is_not_answered_with_the_app_install_remedy():
    """A pasted URL from an org the user does not belong to fails with exit 3, not 4.

    The resolver searches only the user's own orgs, so a foreign Actions URL reports
    not-found — verified live on a mastra-ai run URL (`No run/job/attempt matches that
    id in an org you can access`, exit 3). The app-install remedy is only correct for
    an org the user belongs to; suggesting it for a third party tells them to install
    a GitHub App on someone else's organization. The right move is the gh read
    fallback, stated as such.
    """
    low = _BODY_FLAT
    row = low[low.index("| `3` |"):low.index("| `4` |")]
    assert "third-party" in row or "third party" in row, (
        "the exit-3 row no longer names the pasted-foreign-URL shape")
    assert "gh" in row, (
        "the exit-3 row no longer routes the foreign-org case to the gh read fallback")
    step5 = _flat(_preflight_step(5))
    assert "exit `3`" in step5 or "exit 3" in step5, (
        "the app gate no longer warns that a foreign URL bypasses it via exit 3")


# ---------------------------------------------------------------------------
# Guards added after a pr-review-toolkit mutation sweep (2026-08-26) found that
# 13 of 20 probes passed green: the sections earlier review rounds hardened had
# teeth, and everything else was unguarded prose. Each guard below is scoped to
# the section it protects, per the pattern _preflight_step established.
# ---------------------------------------------------------------------------


def test_the_doctor_step_survives_with_its_exit_contract():
    """Step 2 is the step every later step leans on ("re-run `sling doctor --agent`
    to confirm"), and it was deletable with the suite green."""
    step = " ".join(_preflight_step(2).split())
    assert "sling doctor --agent" in step, "step 2 lost the doctor invocation"
    assert "exits `10`" in step or "exit `10`" in step, (
        "step 2 no longer says an unhealthy doctor exits 10 with the payload on stdout")
    assert "ok:" in step or "ok: false" in step.replace('"', ""), (
        "step 2 no longer tells the agent to act on the failing check")


def test_the_degraded_path_is_never_silent():
    """Step 6 — the gh-only fallback — was deletable with 45/45 green. It is the rule
    that stops the skill answering from `gh` while implying it had `sling`'s
    classification: the same never-a-silent-degrade family as ci-secure's
    a-check-that-could-not-run-is-not-a-pass."""
    step = _flat(_preflight_step(6))
    assert "gh run view" in step, "step 6 lost its concrete gh fallback commands"
    assert "unavailable" in step, (
        "step 6 no longer names what is lost when sling is absent")
    assert "silently degrade" in step or "tell the user plainly" in step, (
        "step 6 lost the do-not-silently-degrade rule")


def _exit_row(code: str) -> str:
    lines = [ln for ln in _BODY.splitlines() if ln.startswith(f"| `{code}` |")]
    assert lines, f"the exit-code table lost its row for {code}"
    return lines[0].lower()


def test_exit_rows_keep_their_meaning_not_just_their_numbers():
    """The old guard checked only that each code CELL existed, so row 10 could be
    rewritten as "CLI crash — file a bug" — the exact inversion its docstring claimed
    to prevent — with the suite green. Pin each row's semantic clause."""
    assert "not a cli error" in _exit_row("10") or "the answer" in _exit_row("10"), (
        "row 10 no longer says the exit IS the answer — an agent will report a "
        "failed run as a sling crash")
    assert "partial" in _exit_row("6") and "not an error" in _exit_row("6"), (
        "row 6 no longer says partial results are usable")
    assert "back off" in _exit_row("7") or "backoff" in _exit_row("7"), (
        "row 7 lost its backoff rule")
    assert "retry once" in _exit_row("5"), "row 5 lost its bounded retry"
    assert "fall back" in _exit_row("5") and "gh" in _exit_row("5"), (
        "row 5 no longer routes a second control-plane failure to the gh fallback")


def test_the_handoff_section_owns_its_table():
    """The whole handoff section — the three-engine table and suggest-don't-chain —
    was deletable while the description still promised one; the guards read only the
    frontmatter and the eval artifact, never the body."""
    i = _BODY.index("## Handoff to the audit skills")
    section = _BODY[i:_BODY.index("## `gh` fallback", i)].lower()
    for engine in ("ci-score", "ci-speedup", "ci-secure"):
        assert f"`{engine}`" in section, f"the handoff table lost {engine}"
    assert "do not auto-chain" in section or "never invoke another skill" in section, (
        "the handoff section lost the suggest-don't-chain rule")


def test_the_compound_ask_rule_orders_read_before_write():
    """Eval case 2's entire subject, and nothing but the never-run eval encoded it:
    the paragraph was replaceable with "just do both halves" with the suite green."""
    i = _BODY.index("**Compound asks**")
    para = _flat(_BODY[i:i + 700])
    assert "first" in para and "report" in para, (
        "the compound-ask rule no longer orders the sling half first, reported")
    assert "never chain" in para, (
        "the compound-ask rule lost 'never chain into a state change without telling "
        "the user'")


_GOTCHA_CLAUSES = [
    # (anchor that identifies the bullet, clause that carries its meaning)
    ("advice, not a verified command", "never run a `fix_command` unchecked"),
    ("unknown flags are ignored, not rejected", "check the rows you got back"),
    ("`--help` lists an abridged flag set", "absence from `--help` is not absence"),
    ("zero phase in `sling time`", "not measured"),
    ("exit `10` is an answer", "report what it says"),
    ("`sling runs show` gives no `job_id`", "jobs list --run"),
    ("`sling bill` has two totals", "amount_due"),
    ("no client-side timeout", "report the timeout itself"),
    ("`sling why` on a run (id or url) can answer for one selected job", "jobs list --run"),
    ("`sling update` exists, and the user runs it", "never you"),
    ("the version can change mid-session", "opting out is the user's choice"),
]


@pytest.mark.parametrize("anchor,clause", _GOTCHA_CLAUSES,
                         ids=[a[:28] for a, _ in _GOTCHA_CLAUSES])
def test_each_gotcha_survives(anchor, clause):
    """Ten of the eleven gotchas were individually deletable with the suite green —
    including the skill's own headline bug (`sling update`). Each bullet is a
    verified binary fact; pin the bullet AND its load-bearing clause, scoped to the
    bullet the way the empty-listing guard already is."""
    low = _BODY_FLAT
    assert anchor.lower() in low, f"the gotcha anchored on {anchor!r} is gone"
    i = low.index(anchor.lower())
    bullet = low[i:i + 700]
    assert clause.lower() in bullet, (
        f"the gotcha {anchor!r} lost its clause {clause!r}")


def test_the_platform_claim_survives():
    """Apple Silicon macOS / x64 glibc Linux only, no Windows build — deletable with
    the suite green, and the preflight's step-6 fallback depends on it being said."""
    step = " ".join(_preflight_step(1).split())
    assert "Apple Silicon" in step and "Linux" in step, (
        "step 1 no longer states the supported platforms")
    assert "Windows" in step, "step 1 no longer says there is no Windows build"


def test_the_casing_rule_survives():
    """snake_case everywhere except whoami's camelCase — a wrong-cased parse is a
    silent empty read, the false-negative class this skill exists to prevent."""
    low = " ".join(_BODY.split())
    assert "snake_case" in low and "whoami" in low and "camelCase" in low, (
        "the parser-casing rule (snake_case except whoami) is gone")


def test_routing_artifact_is_load_bearing():
    """24 of 27 rows were inert: deleting all but the three audit prompts passed, and
    repointing a spend prompt at `sling why` passed. Mirror ci-score's house pattern —
    a floor, no duplicates, and required prompt→command pins."""
    rows = _ROUTING["routings"]
    assert len(rows) >= 34, f"the routing artifact shrank to {len(rows)} rows"
    prompts = [r["prompt"] for r in rows]
    assert len(prompts) == len(set(prompts)), "duplicate prompts in the routing artifact"
    required = {
        "why did this job fail": "sling why",
        "what made this run so slow": "sling time",
        "how many runner minutes did each repo use": "sling usage",
        "which workflow is costing us the most": "sling top",
        "what did we spend on CI this month": "sling bill",
        # the three prompts published in the launch announcement — a description
        # edit must never silently stop these exact phrasings from routing here
        "why did CI fail on my last push": "sling why",
        "what's breaking the nightly build": "sling why",
        "is this test actually broken or just flaky": "sling why",
        # added on the 0.1.124 re-pin: the critical-path view and the
        # like-for-like Afterburner comparison
        "which part of this workflow is slow": "sling time",
        "is Afterburner making our jobs faster": "sling time",
        # the two commands the user runs rather than the agent
        "update sling to the latest version": "sling update",
        "sign me in to sling": "sling login",
    }
    by_prompt = {r["prompt"]: r for r in rows}
    for prompt, command in required.items():
        assert prompt in by_prompt, f"the routing artifact lost {prompt!r}"
        got = by_prompt[prompt].get("command", "")
        assert got == command or got.startswith(command + " "), (
            f"{prompt!r} now routes to {got!r}, expected {command!r}")
    # The flags are the answer for these two: `sling time` without `--workflow`
    # has no critical path, and `--group-by` without `--workflow` compares
    # different mixes of jobs rather than like for like.
    required_args = {
        "which part of this workflow is slow": ("--repo", "--workflow"),
        "is Afterburner making our jobs faster": ("--workflow", "--group-by afterburner"),
    }
    for prompt, flags in required_args.items():
        args = by_prompt[prompt].get("args", "")
        for flag in flags:
            assert flag in args, f"{prompt!r} lost {flag!r} from its args ({args!r})"
    row = next((c for c in _routing_table_rows() if "--group-by afterburner" in c[2]), None)
    assert row, "the routing table lost its `--group-by afterburner` row"
    assert "compare the per-hop `delta`" in row[2] and "alone mixes different jobs" in row[2], (
        "the Afterburner row lost its warning to compare the per-hop delta, not the "
        "pooled `--group-by` table")


def _base_command(cmd: str) -> str:
    toks = cmd.removeprefix("sling ").split()
    return " ".join(toks[:2]) if " ".join(toks[:2]) in _COMMANDS else toks[0]


def test_routing_artifact_commands_exist_in_the_routing_table():
    """Every sling command the artifact names must appear in SKILL.md's routing
    table, so the two contracts cannot drift apart silently. Inside a table row
    and as the full backticked command: a bare word anywhere in the body ("labels")
    let a command fall out of the table with the suite green."""
    table = "\n".join(" | ".join(r) for r in _routing_table_rows())
    for r in _ROUTING["routings"]:
        cmd = r.get("command", "")
        if cmd.startswith("sling "):
            base = _base_command(cmd)
            assert re.search(rf"`sling {re.escape(base)}(?=[`\s\[])", table), (
                f"{r['prompt']!r} routes to `sling {base}`, which SKILL.md's routing "
                f"table never names")


def test_evals_encode_rules_the_suite_cannot_otherwise_see():
    """evals.json is run by no harness, so its content must at least stay coherent
    with the routing artifact: every should-not-trigger eval prompt needs a matching
    non-sling route, and every eval's assertions must name a real command."""
    for case in _EVALS["evals"]:
        text = " ".join(case["assertions"]).lower()
        assert ("sling" in text or "gh" in text or "ci-" in text or "read" in text), (
            f"eval {case['id']} asserts nothing about any tool or route")
        if not case["should_trigger"]:
            assert any(k in " ".join(case["assertions"]).lower()
                       for k in ("hand", "read", "did not", "not invoke", "never")), (
                f"eval {case['id']} is should-not-trigger but its assertions do not "
                "describe a refusal or handoff")


# --- Re-pin parity: the facts a version bump must carry to every surface ----
#
# This skill's whole premise is "every fact was read off the binary at a pinned
# version". Two things make that premise rot silently, and both have happened:
# a re-pin that updates some surfaces and not others, and a documented `doctor`
# shape that drifts from the check list the preflight tells an agent to act on.

# Versions the docs cite deliberately as HISTORY, not as the current pin:
# 0.1.2 is the `fix_command: "sling update"` anecdote and 0.1.84 is the release
# that added `sling update`. Never add the previous pin here: that is how a
# missed surface would hide.
_HISTORICAL_VERSIONS = {"0.1.2", "0.1.84"}
# The one line allowed to cite the version before the pin: the outdated-binary
# side of the reference's illustrative `version` upgrade row. Exempted as a
# LINE, so the same version anywhere else still fails.
_ILLUSTRATIVE_LINES = {
    "references/command-reference.md":
        '"detail": "version 0.1.123\\n0.1.124 is available, to update:",',
}
# Scoped to sling's own 0.1.x family: these files also cite `gh` and the
# Actions runner, whose versions are not this skill's to pin.
_VERSION_RE = re.compile(r"\bv?(0\.1\.\d+)\b")
_PIN_SURFACES = (["SKILL.md"]
                 + [f"references/{p.name}" for p in _REFERENCES]
                 + [f"evals/{p.name}" for p in sorted((_SKILL / "evals").glob("*.json"))])


@pytest.mark.parametrize("name", _PIN_SURFACES)
def test_every_live_version_pin_matches_the_captured_cli_version(name):
    """A re-pin that misses a surface leaves the skill asserting two different
    versions of the truth, and nothing goes red. `command-surface.json` is the
    capture of record, so every other live mention must agree with it — in the
    WHOLE file: the frontmatter description and the smaller references were
    once outside the scan, and a stale `v0.1.8` in either passed."""
    pinned = _SURFACE["cli_version"]
    found: set[str] = set()
    for line in (_SKILL / name).read_text().splitlines():
        versions = set(_VERSION_RE.findall(line))
        if line.strip() == _ILLUSTRATIVE_LINES.get(name):
            versions.discard("0.1.123")
        found |= versions
    found -= _HISTORICAL_VERSIONS
    assert found <= {pinned}, (
        f"{name} cites version(s) {sorted(found - {pinned})} but "
        f"command-surface.json pins {pinned!r}. Re-pin every surface together, "
        f"or add a deliberate historical citation to _HISTORICAL_VERSIONS.")
    if name in ("SKILL.md", "references/command-reference.md"):
        assert found == {pinned}, f"{name} no longer states the pinned version {pinned}"


def test_the_illustrative_version_line_is_still_there():
    """The line exemption above must point at a real line, or it is dead code
    that a later edit could quietly widen."""
    assert any(ln.strip() == _ILLUSTRATIVE_LINES["references/command-reference.md"]
               for ln in _COMMAND_REF.splitlines())


def _sentence_before(text: str, pos: int) -> str:
    """The text from the start of the sentence holding `pos` up to `pos`."""
    return text[text.rfind(".", 0, pos) + 1:pos]


def test_the_agent_surfaces_fix_commands_and_never_runs_them():
    """Step 2 is where an agent meets `sling update` first, as the `version`
    row's `fix_command`. "Either way run it yourself" there was a one-line
    change with the suite green. Pin the step, ban "run it yourself" anywhere
    it is not inside a never / do-not / rather-than clause, and ban running,
    executing or invoking a user-run command (`sling update`, `login`,
    `logout`) unless its sentence says never / do not / ask the user. Both
    look-backs stop at the start of the sentence, so the "never you." that
    ends the previous sentence cannot excuse "run it yourself"."""
    step = _flat(_preflight_step(2))
    assert "fix_command" in step and "`sling update`" in step
    assert "surface it to the user rather than running it yourself" in step, (
        "step 2 no longer says to surface the `version` row's fix_command rather "
        "than run it")
    local = "|".join(re.escape(c) for c in sorted(_USER_RUN))
    run_local = re.compile(
        rf"(?<!`)\b(?:run|execute|invoke)\b[^.`]{{0,30}}(?<!\\)`sling (?:{local})\b")
    for where, text in _shipped_text():
        low = _flat(text)
        for m in re.finditer(r"\brun(?:ning)? it yourself", low):
            before = _sentence_before(low, m.start())
            assert re.search(r"\b(never|not|rather than|instead of)\b|n't\b", before), (
                f"{where} tells the agent to run something itself: …{before}{m.group(0)}")
        # Fenced blocks quote what the binary prints ("… Run `sling login`."),
        # not what the skill tells an agent to do.
        prose = _flat(re.sub(r"```.*?```", " ", text, flags=re.S))
        for m in run_local.finditer(prose):
            before = _sentence_before(prose, m.start())
            assert re.search(r"\b(never|do not|ask the user)\b|don't\b", before), (
                f"{where} has the agent run a user-run command: …{before}{m.group(0)}")


def _doctor_example_keys() -> list[str]:
    """The check keys in the reference's example `doctor --agent` payload."""
    block = _COMMAND_REF[_COMMAND_REF.index('{"checks": ['):]
    return re.findall(r'"key":\s*"([a-z_]+)"', block[:block.index("```")])


_NUMBER_WORDS = {"seven": 7, "eight": 8, "nine": 9, "six": 6, "five": 5, "ten": 10}


def test_doctor_check_keys_match_the_captured_split():
    """The capture records which `doctor` rows are actionable and which are
    advisory. The reference's example must emit exactly those keys, and step 2
    must list exactly the actionable ones: moving `auto_update` into the
    actionable list (an agent then "fixes" an advisory row by updating sling)
    passed with the suite green."""
    dc = _SURFACE["doctor_checks"]
    actionable, advisory = set(dc["actionable"]), set(dc["advisory"])
    assert actionable and advisory and not actionable & advisory
    keys = _doctor_example_keys()
    assert sorted(keys) == sorted(actionable | advisory), (
        f"the reference's doctor example emits {keys}, the capture records "
        f"{sorted(actionable | advisory)}")
    step = " ".join(_preflight_step(2).split())
    m = re.search(r"(\w+) of the (\w+) are actionable — (.*?) — so", step)
    assert m, "step 2 no longer lists the actionable checks"
    listed = set(re.findall(r"`([a-z_]+)`", m.group(3)))
    assert listed == actionable, (
        f"step 2 lists {sorted(listed)} as actionable; the capture says {sorted(actionable)}")
    assert _NUMBER_WORDS.get(m.group(1).lower()) == len(actionable)
    assert _NUMBER_WORDS.get(m.group(2).lower()) == len(actionable | advisory)
    for key in advisory:
        assert re.search(rf"the other \w+, [^.]*`{key}`[^.]*are advisory", step), (
            f"step 2 no longer names `{key}` as advisory")


def test_the_surface_captures_provenance_agree_with_each_other():
    """`_comment` and `captured_utc` are the file's only provenance, and they
    drifted six days apart once because a re-pin moved one and not the other."""
    assert _SURFACE["captured_utc"] in _SURFACE["_comment"], (
        f"captured_utc {_SURFACE['captured_utc']!r} is not the date named in "
        f"_comment: {_SURFACE['_comment']!r}")


def test_every_doctor_check_key_is_named_in_the_preflight():
    """The preflight tells an agent which rows to act on; the reference
    documents what `doctor` emits. A release that adds a check (`agent_skill`,
    then `auto_update`) must land on BOTH, or the agent meets a row its
    contract never mentions."""
    keys = _doctor_example_keys()
    assert len(keys) >= 9, f"doctor example lists only {keys}"
    step = _preflight_step(2)
    missing = [k for k in keys if f"`{k}`" not in step]
    assert not missing, (
        f"preflight step 2 never names doctor check(s) {missing}, which "
        f"references/command-reference.md documents `doctor` as emitting.")

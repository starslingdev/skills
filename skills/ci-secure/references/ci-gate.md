# Adding ci-secure as a CI gate — install, hand-over, refresh

The full runbook for the gate mode summarised in
[SKILL.md](../SKILL.md)'s "Add ci-secure as a CI gate" section. Read this
before running `vendor.py`; SKILL.md carries only the trigger and the
consent rule.

`<ci-secure>` in the commands below is a PLACEHOLDER for this skill's own
install directory — always expand it to the absolute path before running
anything. Pasted literally, the shell reads `<` as input redirection and the
command fails.

## Contents

- [What the gate is](#what-the-gate-is)
- [Install](#install)
- [What gets written](#what-gets-written)
- [The self-proof](#the-self-proof)
- [What refuses, and what the exit code means](#what-refuses-and-what-the-exit-code-means)
- [Hand-over: the six things the user needs](#hand-over-the-six-things-the-user-needs)
- [Refresh](#refresh)

---

## What the gate is

A scan says what is wrong today. A gate stops it coming back — the same
engine, on every pull request, red when a security fact fails. It is
**vendored, never fetched**: the engine, the gate and the licence are
COPIED into the user's repository, so the code judging their PRs is code
they can read and it cannot change underneath them. Fetching a pinned
SHA and executing it at CI time is a shape this skill FLAGS (P14.24); do
not ship it.

## Install

This WRITES INTO their working tree, so the "never write into the user's
tree unasked" rule binds: say exactly what will be written, get a yes,
and only then run it. Do it on a branch, as one self-contained setup change;
never commit, push, or open a pull request unless the user asks — SKILL.md's
NEVER rules bind here and this file does not relax them.

Check three things BEFORE saying what will be written, because two of
them change the answer and the third makes the install pointless:

- `git rev-parse --show-toplevel` gives `<repo-root>`. It is never the
  current directory — vendoring into a subdirectory produces a workflow
  GitHub never runs and an install that looks like it worked. If that
  command FAILS, this is not a git work tree: stop and ask, rather than
  falling back to the current directory, which is that same outcome
  reached by guessing.
- **Does `<repo-root>/ci-secure/VENDORED.json` already exist?** Install
  and Refresh are the same command, and this file is what decides which
  one runs. If it exists this is a REFRESH — go to Refresh, and do not
  promise a workflow, because a refresh writes none even when none is
  there.
- **Does `<repo-root>/.github/workflows/` hold any workflow?** With
  nothing to scan, the gate reports "no workflow files were scanned",
  which is a DEGRADED outcome and stays red even in `--advisory` — a
  permanently red check that neither documented remedy clears. Say so
  and let the user decide before installing.

```bash
<ci-secure>/scripts/vendor.py --into <repo-root>
```

## What gets written

All under `<repo-root>`:

- `ci-secure/scripts/` — the engine (`scan.py`, `config.py`,
  `config_facts.py`, `gh_utils.py`), `gate.py`, and `vendor.py` itself,
  which is what their CI runs to check the copy has not drifted;
- `ci-secure/references/security-patterns.md` — the pattern catalog the
  engine reads at runtime. It is a large document that quotes attack
  shapes, so a repo running its own secret or malware scanners may want
  to allow-list the path;
- `ci-secure/LICENSE` and `ci-secure/VENDORED.json`;
- `.github/workflows/ci-secure.yml`, only if it does not already exist
  (see Refresh).

## The self-proof

Then, before it reports the install complete, it **proves the gate can
fail**. The freshly vendored gate is pointed at a throwaway workflow
that fails a named security fact (`sec.permissions.workflow-declares`),
and must exit non-zero AND name that fact; then at the same workflow
with the hole closed — byte-for-byte the same but for the `permissions:`
block — where it must exit 0, because a gate wedged red reds on
everything and proves nothing. The two fixtures differ only by the fact
under test, so a green has only one explanation. Both fixtures are
temporary files that are deleted afterwards: **nothing is written into
the user's tree for the proof, and no workflow of theirs is broken to
demonstrate it.**

It then runs the gate on their real tree and prints what it found,
keeping the two kinds of red apart: failed FACTS, which the shipped
`--advisory` reports without blocking, and everything else — a crashed
engine, an unscannable workflow, a dropped match — which stays red even
in advisory, so their very first run will be red until it is resolved.
Relay that distinction; it is the difference between "green on day one"
and "red on day one". Read the proof line too, and relay it:

- `self-proof PASSED` — the gate has been observed failing and passing.
  Only then is this a working install.
- `self-proof FAILED` (exit 1) — the gate passed a vulnerable fixture, or
  redded a clean one. **Do not report a working install**, and **stop
  before the hand-over below** — skip it entirely rather than walking
  them through going blocking. Say the gate is not usable, and say what
  is on disk: the vendored files AND
  `.github/workflows/ci-secure.yml`, which runs on every pull request
  from the next push. Offer to revert them, or to refresh the copy.
  Committing them ships a check that cannot block — a green tick and no
  protection, the thing this skill exists to argue against.
- `self-proof COULD NOT RUN` (exit 2 from `--self-test`; the install
  itself still exits 0) — the proof did not happen here. The gate's own
  output is quoted above the line and says whether the engine could not
  start on this machine (PyYAML missing, most often — CI installs it) or
  the vendored copy is broken everywhere; relay which, and do not assert
  a cause the output does not give. The gate is installed and
  **unproven**: say so, and tell them to re-run
  `<repo-root>/ci-secure/scripts/vendor.py --self-test <repo-root>/ci-secure`
  once that is resolved. That command is theirs to re-run at any time; it
  writes nothing.

Nothing else counts as a proof line. If none of the three appears, treat
it as a failed proof, not as a pass.

On both non-PASSED outcomes the install says nothing about what the gate
makes of the user's own code, on purpose — a verdict from a gate that
failed its proof, or that could not run at all, is not an observation
about their repository. Do not fill that gap with a guess.

It also reads their `CLAUDE.md` / `AGENTS.md` / `CONTRIBUTING.md` for a
guard-registration convention (a register of build-breaking checks, a
mutation harness) and, if it finds one, says the new gate has NOT been
registered with it, quoting the line. It never edits their harness, and
**neither do you** — the harness is theirs, you do not know its shape,
and guessing means writing into files you have not read. Pass it to the
user as work they own. The check is a keyword read: it misses
conventions phrased other ways, and a false hit costs one glance.

## What refuses, and what the exit code means

Everything that can refuse refuses before the first byte is written, and
the workflow is written last, after the manifest. So a refusal means at
worst a partly-copied `ci-secure/`, never a live workflow with no gate
behind it. A non-zero exit is two different things, and the output says
which: a refusal (nothing wired up) or a complete install whose
self-proof failed (files on disk, gate not to be trusted). It refuses
on: a vendored copy trying to install, a
missing licence, an incomplete skill, a `<repo-root>` that is a
subdirectory of a repository rather than its root, a destination
redirected by a symlink, a `ci-secure` that exists and is not a
directory, and a `ci-secure/` directory that already holds someone
else's files — that last one because the workflow re-checks that
directory against the manifest on every run and would red on anything
else it finds there. That collision refusal applies to a FIRST install
only; a refresh expects to find our files there. Report the error and
stop; do not retry blind.

If it reports that `.github/workflows/ci-secure.yml` already existed, the
install did NOT wire anything up — resolve that with the user before
telling them they have a gate. Read the output; never infer success from
the exit code. `--into` exits 0 for a proved install AND for one whose
proof could not run, and exits 1 both for a refusal and for a failed
proof, so the status alone cannot tell you which of the four you have —
only the proof line and the refusal message can.

The install leaves everything UNCOMMITTED, and nothing runs until those
files are on a branch GitHub can see. Say that plainly when handing over
— committing is theirs to do unless they ask, and SKILL.md's NEVER rules
bind. If their tree already had uncommitted work in it, say that too:
the vendored files are now mixed in with it.

## Hand-over: the six things the user needs

**Only if the self-proof PASSED**, walk the user through these in the
message that hands the work over. They need them whether or not a PR
gets opened. On `self-proof FAILED` this hand-over does not apply at
all: it ends in making an unusable gate a required check. Items 1, 3 and 4 also
belong in the PR body; items 2, 5 and 6 name weaknesses the repo still
has, so on a public repository keep those out of the PR body and say
them to the user directly — the disclosure corollary in SKILL.md's NEVER
rules applies to an install PR as much as to a fix PR.

1. **Requirements**: Python 3.12 and PyYAML, both pinned in the
   workflow. The engine is not stdlib-only; the gate is. `vendor.py`
   itself needs Python 3.9 or newer to run. **Check their default
   branch**: the workflow ships `push: branches: [main]`, so if theirs
   is not `main`, change it in the file before handing it over — left as
   shipped that trigger silently covers nothing, and it is the one that
   re-judges what already merged. Pull requests are judged either way.
2. **It ships in `--advisory` mode.** A repo that has never been scanned
   usually reds two or three facts on its first run (workflows with no
   `permissions:`, no CODEOWNERS entry for `.github/`). Advisory reports
   them without blocking, so the installing PR does not brick their
   merge path. **`--advisory` downgrades FAILED FACTS ONLY** — a crashed
   engine, zero workflows scanned, an unrecognised outcome or an
   incomplete scan stay red, because a ramp for findings must never
   become a mute button for a broken scan.
   The install's self-proof already showed the gate failing on a
   throwaway fixture, so "will this ever actually block?" is answered
   before they commit anything — and `vendor.py --self-test` re-answers
   it whenever they want, without touching their tree.
3. **Going blocking**, once those are burned down: drop `--advisory`
   from the "Run ci-secure" step in
   `.github/workflows/ci-secure.yml`, then require **`ci-secure`** — the
   always-running verdict job, never the scan job. A conditional job that
   gets skipped reports Success to a required-check rule, so requiring
   one is a rule that can be satisfied by never running it. The second
   half is a repository setting only they can change.

   "Make ci-secure block my PRs" on a repo that already has the gate is
   asking for this, not for an install — the install command would run a
   refresh, touch no workflow, and leave `--advisory` exactly where it
   was. Editing that one line is a write into their tree like any other:
   say which line, get a yes, then make the edit.
4. **Getting out**, if it ever reds their default branch: un-require
   `ci-secure` (one settings change, reversible, and it needs admin).
   That unblocks MERGES; the branch itself stays red until the cause is
   fixed, because the `push:` trigger keeps running. **Not** deleting
   the workflow — that leaves them believing they have a check they do
   not. Putting `--advisory` back is the narrower remedy and only clears
   a red caused by a failed fact; it will not clear a crashed engine, an
   incomplete scan, or a rate-limited weekly run — the workflow also
   runs weekly, which is where that last one comes from. If they want
   the gate gone entirely, the order matters: delete the workflow FIRST,
   then `ci-secure/`. The other way round reds every run in between, on
   the drift check, before the gate is even reached.
5. **Two facts stay UNMEASURED** on any CI token: whether required
   checks are skippable, and the fork-PR approval policy. Both are
   admin-scoped API reads. They are disclosed and dropped from the
   score, never counted as passes.
6. **A pull request can edit the workflow that judges it — and the gate
   it runs.** On `pull_request` GitHub checks out the PR's tree, so both
   `.github/workflows/ci-secure.yml` and the vendored `ci-secure/` are
   the PR's versions. Tell them to require review on **both** paths
   before making `ci-secure` a required check. A CODEOWNERS entry for
   `.github/` is one of the facts this gate checks; `/ci-secure/` is not
   — nothing checks it for them, and `.github/` alone leaves the gate,
   the engine, `config.py` (which defines which outcomes block) and the
   manifest editable by an ordinary approval. Hashing does not help
   here: whoever edits the vendored gate edits `VENDORED.json` in the
   same commit.

## Refresh

"Update the ci-secure gate": re-run `vendor.py --into` from the current
skill version. This writes into their tree exactly as the install does,
so the same rule binds — say what will be rewritten, get a yes, and only
then run it. Show them the resulting diff; open a PR only if they ask
for one.

- Run `vendor.py --verify ci-secure` and `git status` FIRST. A refresh
  overwrites every vendored file, and an UNCOMMITTED local edit is gone
  for good — `git diff` afterwards cannot show what it replaced, because
  there is no committed version to compare against. If either command
  shows local changes, surface them and get a decision before running
  anything.
- The vendored CODE is replaced, and files a newer version no longer
  ships are removed. Committed hand edits show up in the resulting
  `git diff` for the user to resolve. Their CI re-checks the copy every run
  (`vendor.py --verify ci-secure`), which catches the local edit made
  while debugging and never removed — not a determined attacker, who can
  edit the manifest in the same commit.
- **A refresh writes no workflow at all**, whether or not one is sitting
  at `.github/workflows/ci-secure.yml`. That file is theirs: the runner,
  the triggers, the path they moved it to, and the `--advisory` flag they
  deleted when they went blocking. Rewriting it — or re-adding the
  template beside a copy they renamed — quietly returns a blocking gate
  to advisory, and since it is deliberately not checksummed nothing
  downstream would catch that. If the template has changed in a way they
  want, show them the diff against `<ci-secure>/scaffold/ci-secure.yml`
  and let them choose.
- **A refresh re-proves the gate**, on the same throwaway fixtures and
  with the same three outcomes as an install. It replaces the engine,
  the gate and the rule, which is exactly when a gate can stop being
  able to fail — and their `git diff` shows code, not behaviour. Relay
  the proof line as you would on an install.
- There is no dry run, and `--verify` compares their copy against its own
  manifest, not against this skill — so "is it already current?" can only
  be answered after the refresh, from `git diff`. If that diff is empty,
  say so and open nothing: a PR whose only change is a rewritten manifest
  is noise.

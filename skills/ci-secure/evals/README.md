# ci-secure behavioral evals

Five cases that run the **whole skill** — an agent, from the user's request to
the close — and grade what it did. They exist to catch regressions in the
agent-facing contract in `SKILL.md`, which no other test in this repository
covers: `pytest` exercises the deterministic scanner underneath the skill, not
the behavior the skill promises on top of it.

> ## This suite has never been executed
>
> `claude plugin eval` is in early access and is not enabled on the machine
> these cases were written on — it prints ``` `plugin eval` is currently in
> early access ``` and exits before running anything. **No case here has ever
> produced a score.** Treat the suite as an unvalidated first draft, not as a
> gate that is passing. What has and has not been checked is spelled out under
> [What is verified](#what-is-verified) below; read it before quoting a result
> from this directory.

## Running them

```bash
# from the repository root
claude plugin eval skills/ci-secure \
  --scaffold \
  --allow-tools Bash \
  --ablation with-without \
  --output-dir /tmp/ci-secure-evals \
  --no-publish \
  --model <pin a model> --judge-model <pin a model>
```

Every flag above is load-bearing:

| Flag | Why it is not optional |
|---|---|
| `--scaffold` | Scaffolds are **off by default**. Without it no case gets a `.github/workflows/` tree, the skill correctly reports that there is nothing to scan, and all five cases fail for a reason that has nothing to do with the skill. |
| `--allow-tools Bash` | `Bash` is not in the harness's read-only set and a case cannot self-grant it. Without the grant the scanner can never execute and every case fails. The cases list `Bash` in `allowed_tools`, so an ungranted run is reported per case on stderr. |
| `--ablation with-without` | Passed explicitly so the arm structure never depends on whether the target happened to resolve as a plugin. The delta against a no-plugin arm is the point of several graders. |
| `--no-publish` | Publishing the HTML report to claude.ai is the **default** where the account supports it, and that report carries full prompts, transcript excerpts and grader evidence from whatever repository was scanned. That is the same disclosure the `--output-dir` row guards against on disk; guarding one and not the other guards neither. |
| `--output-dir` | Results default to `skills/ci-secure/evals/results/` — **inside the installable skill**, which the `skills` CLI copies wholesale into end-user installs. `tests/test_ci_secure_install_surface.py` fails if they land there. |
| `--model` / `--judge-model` | Pin both, or a model rollout reads as a skill regression. |

`--allow-tools Bash` is an **unscoped** grant, and the plugin under test is your
real checkout rather than a copy — nothing is sandboxed away from it. That is 30
agent runs holding unrestricted `Bash` while `SKILL.md` Phase 5 is a phase whose
job is to edit files. `--allow-tools` accepts `Tool(pattern:*)` syntax; scope it,
or run the suite from a throwaway clone.

Cost is roughly `cases × runs × arms` agent runs (5 × 3 × 2 = 30) plus three
judge calls per `llm` grader. Pilot with `--case template-injection --runs 1
--ablation none` first: by this file's own framing the first execution is suite
debugging, and one case is enough to find out.

Preconditions on the machine: `git` available to the scaffold, and `python3` on
`PATH` with **PyYAML** importable — the scanner's one third-party dependency,
which hard-exits 1 without it and fails every findings grader for a reason that
is not the skill. The child gets a scrubbed env, so a venv-only PyYAML may not be
visible to the `python3` it finds; check with `env -i PATH="$PATH" python3 -c
'import yaml'` before spending on a run. The sandbox gives the scaffold only
`PATH`, `HOME`, `TMPDIR`, `TERM` and `GIT_CONFIG_NOSYSTEM`.

## The cases

| Case | Fixture | What it pins |
|---|---|---|
| `template-injection` | `files/inject` | The engine is run rather than the YAML eyeballed; P14.10 is reported at its real file and line; the finding carries an attacker scenario; nothing is committed. |
| `clean-repo` | `files/clean` | Zero findings is reported plainly and positively, with the scope stated honestly, the impostor-check state named, and no invented lesser observations. |
| `pwn-request` | `files/pwn-request` | The findings land on `vulnerable.yml` at their real lines and `safe.yml` comes back clean — a real negative, not a coverage gap — and the scenario names the fork-PR entry point. (Two groups fire on `vulnerable.yml`, not one.) |
| `many-findings` | `files/many-findings` | All nine statically-detectable groups are found and offered as a complete numbered table built from the render plan, rather than through a capped question widget. |
| `impostor-check-skipped` | `files/inject` | The nine static vectors still complete with the network-gated check disabled, and the skipped check is named loudly and never presented as a pass. |

Each case is one `case.yaml`. `case.yaml` is used rather than the
`prompt.md` + `graders/*.md` layout because `context.scaffold_script` cannot be
set from `prompt.md`, and every case needs a scaffold.

### Why there is a scaffold at all

The fixtures are deliberately vulnerable workflow YAML. Tracked at a literal
`.github/workflows/` path they would read to a registry scanner as this
repository's own live automation — the class of finding that once rated a
sibling skill CRITICAL — so
`tests/test_ci_secure_install_surface.py::test_no_tracked_workflow_shaped_fixture_paths`
makes that a hard failure. The tracked form is therefore cloaked
(`files/<slug>/dot-github/workflows/*.yml.fixture`) and every consumer un-cloaks
it at runtime: `pytest` through `tests/conftest.py`, the eval harness through
`_scaffold_common.sh`.

The scaffold also runs `git init` in the sandbox working directory. That is not
incidental: the harness places its own empty repository one level **above** the
working directory, so without a repository of its own `git rev-parse
--show-toplevel` — the first thing ci-secure's Phase 1 does — would resolve to a
directory containing no workflows at all.

## Grader choices

The suite prefers **mechanical** graders and uses `llm` graders only where the
expectation is genuinely about prose. The reason is a documented hazard: an
`llm` text grader can score full marks in the *no-plugin* arm because the agent
reads the skill's `SKILL.md` off disk and describes the correct behavior without
the skill ever loading. "Did it sound right" does not discriminate; running the
engine does.

Three anchors do most of the work:

- **`tool_used` on the engine scripts.** `run.py` / `scan.py` exist nowhere the
  without-arm can reach, so this is both the "the engine actually ran, it was
  not YAML eyeballing" assertion and the sharpest ablation signal in the suite.
- **`run.py`'s printed group list.** It emits the pattern ids present, sorted as
  *strings*, which puts the two-digit suffixes ahead of the one-digit ones. That
  ordering is one no person or model writing the ids out by hand produces, so a
  single regex asserts the engine ran and ran on *this* fixture. Note it is a
  `contains` match: it does **not** assert that no other group was reported.
- **`report.py`'s pre-drawn banner.** The vector count and the impostor-check
  state are computed by the renderer and written by separate f-strings, so they
  are adjacent on one line only in a real render. `SKILL.md` mandates the banner
  be grepped out of the report and pasted verbatim, so it reaches the transcript
  through a contract-mandated tool call.

The expected strings themselves are deliberately **not** written out here.
`evals/` ships inside the installable skill and is therefore on disk, readable,
in the sandbox the agent under test is running in — a worked answer key in this
file is an answer key the subject can read. The ground truth lives in each
`case.yaml`'s patterns and in the fixtures; regenerate it by running
`scripts/run.py` and `scripts/report.py` against a scaffolded fixture tree.

### The vacuity rule

A `trace` grader reads the whole transcript, and the transcript of a run *with*
the skill contains text the skill itself shipped. So a `contains` pattern that
already appears there passes for free, and a `not_contains` pattern that appears
there can never pass at all. `tests/test_evals_cases.py::test_no_trace_regex_matches_the_skills_own_prose`
makes that a hard failure.

It is not a theoretical concern. Three of the first drafts of these graders
(`P14.10`, `did NOT run`, `Impostor-SHA check (P14.11): ran`) are quotations
from `SKILL.md` and were flagged by that test; a fourth (`No critical attack
vectors`) was re-anchored for other reasons and the test does *not* flag it.

The corpus the rule checks is wider than `SKILL.md`. The `skills` CLI copies
`skills/ci-secure/` recursively, so `scripts/*.py` and this file sit on disk
beside the `scripts/run.py` path `SKILL.md` tells the agent to resolve, and one
`Grep` over the skill directory pulls any of them into the trace. Checking only
the prose files is what let a `not_contains` grader be written against a literal
in `scripts/scan.py`, where it could never pass. Two shipped surfaces stay out
of the corpus on purpose, and a third with a caveat — all documented at
`_agent_readable_sources()`. `tests/`, whose oracle assertions quote the
renderer's exact output by construction; `evals/files/`, which *is* the input
under audit, so a grader pinning a finding to its real `file:line` has to quote
it; and the `case.yaml` files themselves, since every pattern is a literal in
its own case file and including them would flag all fourteen trace regexes
against themselves.

That third exclusion carries real residual risk, because `evals/` ships: an
agent that greps the eval directory sees the answer key. The *fatal* half of it
is covered unconditionally by a separate test — a `not_contains` pattern that
matches its own declaration can never pass, and that is a silent failure, so
`test_negative_regexes_do_not_match_the_case_file_that_declares_them` forbids
it outright. The free-pass half is accepted and named here rather than papered
over; the honest fix is to stop shipping `evals/` to end users at all, which is
a larger change than this suite.

One consequence is worth stating plainly rather than leaving to be discovered.
A pattern that is a plain literal — the banner-count graders are — necessarily
matches its own `pattern:` line, so four of them are satisfiable by text
copied from the case file that declares them. This cannot be fixed by
re-anchoring; it is a property of shipping the definitions next to the subject.
What *is* enforced is that no case's **completion anchor** has this shape: the
graders that carry the "the engine actually finished" assertion are checked
against their own case file along with everything else the agent can reach, and
the comments around them no longer quote the expected output in full. So a run
that copied the whole eval directory into its answer still fails every case.

Two smaller bypasses are closed alongside it. A regex grader with no `target:`
line is invisible to the rule, so `target` is now required. And a pattern
opening with a bare digit matches inside a longer number — `0 critical
findings` is contained in `10 critical findings`, so the zero-findings case
would have passed under the single worst regression it exists to catch — so
count-bearing patterns must carry a `(?<![\d.])` guard.

Two schema traps are also pinned by that test file, because both fail silently:
a `tool_used` grader on the `Skill` tool is demoted to an *unscored* indicator
unless it says `arm: both`, and `max: 0` without `min: 0` asserts "between 1 and
0 calls" and can never pass.

## What is verified

Verified, and re-checkable with `python3 -m pytest skills/ci-secure/tests/test_evals_cases.py`:

- every `case.yaml` parses, and every top-level, `context`, `execution` and
  grader key is one the shipped CLI accepts (grader objects are strict — an
  unknown key there is a hard validation error);
- grader shapes: types, unique names, positive weights, valid `arm`, valid
  `target`/`focus`, JavaScript-legal regex flags, patterns that compile,
  `min`/`max` pairs that are satisfiable;
- no grader relies on a known trap (unscored `Skill` grader, unsatisfiable
  `max: 0`, vacuous trace regex, untargeted regex, a count that matches inside a
  larger number, a negative that matches its own declaration, an
  ungrantable `allowed_tools` entry);
- every case carries at least one grader that a *completed* engine run
  satisfies and a failed one does not — matching `run.py`'s or `report.py`'s
  real output on that fixture while matching neither the skill's shipped text
  nor the fixture as `grep -n` would render it. `tool_used` cannot serve as
  that anchor: a Bash command that mentioned `run.py` and exited 1 looks
  identical to one that succeeded. The test scaffolds each fixture and runs the
  real engine, so it also fails if the fixtures drift;
- every referenced scaffold, fixture directory and `plugins:` path exists and
  resolves where the case says it does;
- no stray `case.yaml` / `prompt.md` under `files/`, which would be silently
  discovered and run with real API spend.

The same command also covers the scaffold, which is no longer hand-checked:
`test_scaffold_still_materializes_the_tree_in_an_empty_sandbox` drives each
`scaffold.sh` under a mock of the sandbox's minimal environment and asserts the
tree it produces, and two sibling tests assert it REFUSES a non-empty working
directory and one it cannot list. `git rev-parse --show-toplevel` resolving to
the working directory rather than the harness's parent repository was confirmed
by hand while authoring.

Verified by hand during review, and previously listed here as the largest
untested assumption: **the agent under test can resolve `<ci-secure>`, the
skill's own install directory.** `SKILL.md` needs an absolute path to it for
every scripted phase, and if it could not be resolved all five cases would fail
together. It can: the `Skill` tool result carries the skill's base directory in
its own header, so the path arrives on the very tool call the
`ci-secure-skill-fired` grader already requires. The bare skill folder was also
confirmed to load as a plugin (`--plugin-dir skills/ci-secure` exposes
`ci-secure:ci-secure`, which that grader's `input_match` accepts). Every expected finding — pattern ids, files,
line numbers, banner text, group-list ordering, `gh_checks` strings — was read
off a real run of `run.py` and `report.py` against these fixtures, not assumed.

**Not verified, because the runner is gated:**

- that any case scores anything at all — no case has been executed;
- **that the scanner exited successfully, as opposed to having been invoked and
  then described.** No grader type in this harness observes a tool's exit
  status or its output: `tool_used` matches the invocation's *input*, and
  `file_exists` cannot reach the one artifact whose absence would prove failure
  — `run.py` clears its output path before scanning, so absence-after-failure
  is a real contract, but `SKILL.md` keys that path to a hash of the repository
  root, and the sandbox's root differs every run. Each case therefore asserts
  invocation *plus* output shape, anchored on serializations an eyeballing
  agent does not produce. The residual — an agent that runs the scanner,
  watches it fail, and writes the expected string itself — is **accepted, not
  solved.** Closing it needs either a harness grader that reads tool results,
  or a prompt naming a fixed output path, which would trade away the cases'
  realism since the report is written only on a user's save pick;
- whether the interaction contract survives having no user. `AskUserQuestion`
  is auto-allowed, so the skill *will* ask — and `SKILL.md` Phase 3 puts the
  banner and receipt lines INSIDE that question's text whenever a question is
  the next act. On `clean-repo` that is where the banner graders expect their
  strings to come from, and on `many-findings` the whole selection contract
  sits there. Whether the tool returns, errors, or burns the turn cap in an
  eval child is unknown;
- whether `many-findings`' promised fix dispatch can happen at all: no case
  grants `Write` or `Edit`, and Phase 5 subagents edit workflow YAML. No grader
  asserts it, so it would fail as a stated-but-unreachable outcome rather than
  as a red case;
- whether the graders' assertions actually land in the `trace`. Several depend
  on contract-mandated `grep`/`python3` calls surfacing report content into the
  transcript; that is what `SKILL.md` says happens, but it has not been observed
  here;
- whether the `llm` graders' rubrics discriminate in practice, and whether they
  also pass in the no-plugin arm. All seven are `focus: trace` and none is
  covered by the vacuity rule, which filters on `type == "regex"` — they are 7
  of the 45 graders and they carry every prose-quality assertion in the suite,
  so the first real run should be read grader-by-grader, not as one score;
- that the harness accepts `plugins: ["../.."]` as a loadable plugin. There is
  no `plugin.json` or `.claude-plugin/` anywhere in this repository;
  `test_plugins_entry_resolves_to_this_skill` proves only that the *path*
  resolves to the skill root, not that the runner will load it. If it does not,
  the with-arm loads nothing and the ablation delta is meaningless;
- whether `max_turns` (60, and 90 for `many-findings`) is generous enough. An
  exhausted turn cap is a run error and depresses the score for a reason
  unrelated to the skill;
- score stability across the three runs per case. The default pass threshold is
  **1.0**: every scored grader must pass on every run.

**Not covered by any grader, and named here so the gap is not mistaken for
coverage:**

- **Fix subagents staying inside their one target file.** `SKILL.md` Phase 5
  requires a fix subagent not to edit anything outside its finding's
  `workflow_file`. The retired `evals.json` asserted it; nothing grades it now,
  and `many-findings` only reaches the selection table.
- **The report's scope-honesty line** (`Critical exploit-chain checks only …`).
  It is emitted on every report and the skill's positioning rests on it, but it
  survives only inside `clean-repo`'s `llm` criteria — so a regression on a
  finding-bearing repo, where padding pressure actually exists, keeps four of
  five cases green.
- **The scanner's own degradation path.** `impostor-check-skipped` tells the
  agent to pass `--gh-impostor off`, so it grades honest *reporting* of a
  disabled check, not the `auto` mode discovering `gh` is absent and degrading
  by itself. The prompt cannot change `PATH` (`execution.env` accepts only
  `EVAL_*` keys), which is why it is written this way.

The first real execution should be treated as suite debugging, not as a
measurement of the skill.

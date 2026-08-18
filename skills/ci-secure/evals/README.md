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
  --model <pin a model> --judge-model <pin a model>
```

Every flag above is load-bearing:

| Flag | Why it is not optional |
|---|---|
| `--scaffold` | Scaffolds are **off by default**. Without it no case gets a `.github/workflows/` tree, the skill correctly reports that there is nothing to scan, and all five cases fail for a reason that has nothing to do with the skill. |
| `--allow-tools Bash` | `Bash` is not in the harness's read-only set and a case cannot self-grant it. Without the grant the scanner can never execute and every case fails. The cases list `Bash` in `allowed_tools`, so an ungranted run is reported per case on stderr. |
| `--ablation with-without` | A path target defaults to `--ablation none`. The delta against a no-plugin arm is the point of several graders. |
| `--output-dir` | Results default to `skills/ci-secure/evals/results/` — **inside the installable skill**, which the `skills` CLI copies wholesale into end-user installs. `tests/test_ci_secure_install_surface.py` fails if they land there. |
| `--model` / `--judge-model` | Pin both, or a model rollout reads as a skill regression. |

Cost is roughly `cases × runs × arms` agent runs (5 × 3 × 2 = 30) plus three
judge calls per `llm` grader. Pilot with `--runs 1 --ablation none` first.

Preconditions on the machine: `python3` on `PATH` with **PyYAML** importable
(the scanner's one third-party dependency) and `git` available to the scaffold.
The sandbox gives the scaffold only `PATH`, `HOME`, `TMPDIR`, `TERM` and
`GIT_CONFIG_NOSYSTEM`.

## The cases

| Case | Fixture | What it pins |
|---|---|---|
| `template-injection` | `files/inject` | The engine is run rather than the YAML eyeballed; P14.10 is reported at its real file and line; the finding carries an attacker scenario; nothing is committed. |
| `clean-repo` | `files/clean` | Zero findings is reported plainly and positively, with the scope stated honestly, the impostor-check state named, and no invented lesser observations. |
| `pwn-request` | `files/pwn-request` | The finding lands on `vulnerable.yml` and `safe.yml` comes back clean — a real negative, not a coverage gap — and the scenario names the fork-PR entry point. |
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
  *strings* — `["P14.25", "P14.9"]`, or the full nine with `.24`/`.25` landing
  before `.7`/`.9`. That ordering is one no person or model writing the ids out
  by hand produces, so a single regex asserts the engine ran, ran on *this*
  fixture, and produced exactly those groups.
- **`report.py`'s pre-drawn banner.** `0 of 10 vectors hit`, `9 of 10 vectors
  hit`, `impostor check SKIPPED` are counts and states only the renderer can
  compute, and `SKILL.md` mandates the banner be grepped out of the report and
  pasted verbatim — so they reach the transcript through a contract-mandated
  tool call.

### The vacuity rule

A `trace` grader reads the whole transcript, and the transcript of a run *with*
the skill contains the text of `SKILL.md`. So a `contains` pattern that already
appears in the skill's own prose passes for free, and a `not_contains` pattern
that appears there can never pass at all. `tests/test_evals_cases.py::test_no_trace_regex_matches_the_skills_own_prose`
makes that a hard failure. It is not a theoretical concern — the first drafts of
these graders used `P14.10`, `did NOT run`, `No critical attack vectors` and
`Impostor-SHA check (P14.11): ran`, and **every one of them is a quotation from
`SKILL.md`**. Each was re-anchored on a scanner- or renderer-produced string
after the test flagged it.

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
  `max: 0`, vacuous trace regex);
- every referenced scaffold, fixture directory and `plugins:` path exists and
  resolves where the case says it does;
- no stray `case.yaml` / `prompt.md` under `files/`, which would be silently
  discovered and run with real API spend.

Verified by hand while authoring: the scaffold was executed under a mock of the
sandbox's exact minimal environment and directory layout, and `git rev-parse
--show-toplevel` was confirmed to resolve to the working directory rather than
the harness's parent repository. Every expected finding — pattern ids, files,
line numbers, banner text, group-list ordering, `gh_checks` strings — was read
off a real run of `run.py` and `report.py` against these fixtures, not assumed.

**Not verified, because the runner is gated:**

- that any case scores anything at all — no case has been executed;
- that the agent under test can resolve `<ci-secure>`, the skill's own install
  directory, from inside the sandbox. `SKILL.md` requires an absolute path to
  it for every scripted phase. The harness passes the plugin as `--plugin-dir`
  pointing at the real checkout, so the directory is present, but whether the
  agent locates it unaided is untested. **If this does not hold, all five cases
  fail together** — that is the first thing to check against a red suite;
- whether the graders' assertions actually land in the `trace`. Several depend
  on contract-mandated `grep`/`python3` calls surfacing report content into the
  transcript; that is what `SKILL.md` says happens, but it has not been observed
  here;
- whether the `llm` graders' rubrics discriminate in practice, and whether they
  also pass in the no-plugin arm;
- whether `max_turns` (60, and 90 for `many-findings`) is generous enough. An
  exhausted turn cap is a run error and depresses the score for a reason
  unrelated to the skill;
- score stability across the three runs per case. The default pass threshold is
  **1.0**: every scored grader must pass on every run.

The first real execution should be treated as suite debugging, not as a
measurement of the skill.

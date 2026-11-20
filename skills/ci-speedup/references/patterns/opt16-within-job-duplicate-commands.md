---
pattern: OPT16
impact: LOW
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: within-job-duplicate-commands
title_template: 'Within-Job Duplicate Commands'
category: 2
---

# OPT16 — Within-Job Duplicate Commands

**Anti-pattern**: The same script or command is invoked twice within a single job's steps — once in a dedicated "build" step, then again inside a "run tests" step (often with a comment like "rebuild in case of stale artifacts").

**Detection heuristic**:

The static scanner also recognizes one form of repeated analysis in which
the runs differ only in report or issue-selection flags. It covers Knip only:
Knip's `--include`, `--reporter` and issue-type flags (`--files`, `--exports`,
`--dependencies` and the like) select or render issues from one project
analysis, so the scanner can tell them apart from scope-defining arguments.
Other analysis tools run several times over one scope are not flagged by this
static form. Each step's command is read through the shared run-leaf resolver,
so package scripts (`pnpm knip:json`, `npm run knip`) and `\`-continued lines
are followed. Runs in one job match, in one step or in several, when the
working directory, the step `if:` condition and every other argument
(`--config`, `--production`, `--workspace` and so on) are identical; a `>`
redirect of the report counts as output. A different config, production mode,
workspace, working directory or step condition stays quiet, and so do a run
with `--fix`, a step that changes directory, and runs in one step that a
conditional chain (`||`, `if`, `case`) chooses between. Evidence: the public
[OpenClaw #185851](https://github.com/openclaw/openclaw/pull/185851) change
took 91.3 s to 65.9 s.

For this form, run Knip once with the combined issue selection, then split or
filter its output, keeping each selection's pass/fail policy. The
runner-minute effect is a saving (25.4 s, about 0.42 runner-minutes per run,
in the cited change), but the finding reports `runner-minute saving: unsized`
because the combined run still performs the analysis once and its cost is
specific to each repository. Time the separate runs and the combined run on
the same commit before crediting minutes.

Job logs can prove the Cargo variant even when the workflow commands differ:
if two distinct step groups print the same in-repository
`Compiling <crate> v<version> (<path>)` line, every local crate in the later
step already appeared in the earlier step, and both commands share the same
observable profile, target, feature, toolchain, compiler and target-directory
settings, the second step repeats compilation. Different crates or artifact
settings stay quiet. The later step's full `Finished` wall is an upper bound,
because it can include linking or other work beside the repeated compilation.

The measured critical-path drill also recognizes wrappers outside Cargo. It
reports OPT16 when two invocations (in separate step groups or inside one wrapper
step) print the same completed test signature (matching test-file and test totals,
including TAP totals) or the same package-build signature (package plus output
directory). It requires a readable active runtime version, carries that version
forward from setup steps, and compares package scope, so different runtime
versions and different packages stay quiet. A matching signature is evidence to
investigate the wrapper; it is not permission to remove distinct platform,
feature, coverage, or failure-path coverage.

For these wrapper-hidden repeats, the later completion duration is a measured
upper bound. It can include nearby work in that step, so the report never credits
more than that duration and requires re-measurement after the duplicate leaf
command is removed.

```bash
# For each job in each workflow, check if any command appears in multiple steps
for f in /tmp/workflows/*.yml; do
  # Extract run: commands per job and check for duplicates
  grep -oP '(?<=run: ).*' "$f" | sort | uniq -d
done
```

**Fix**: Trace both wrappers to the commands they ultimately run, then remove the
duplicate invocation only when their runtime, platform, settings and coverage are
equivalent. If an aggregate build repeats one package, exclude only that
already-built package. For Cargo, preserve every intended
package, feature, platform and test target. Combine package selections in one
invocation when they are meant to share artifacts, or align the feature, target
directory, toolchain and compiler settings that should produce the same artifact;
then confirm every original test target still builds and runs. If distinct feature
or platform coverage is intentional, keep both builds. If the rebuild is a
workaround for stale artifacts, fix the root cause (e.g., cache key, build system
incremental support) instead.

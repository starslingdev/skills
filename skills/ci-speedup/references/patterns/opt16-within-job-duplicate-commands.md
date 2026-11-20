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

The static scanner also recognizes repeated Knip analysis in separate steps of
one job when the project scope is unchanged and only issue-selection or report
flags differ. Scope-defining arguments such as `--config`, `--production` and
the working directory must match. A different config, production mode or
working directory stays quiet. This covers the public
[OpenClaw #185851](https://github.com/openclaw/openclaw/pull/185851) shape,
where several report commands rebuilt the same analysis graph.

For this variant, run Knip once with the combined issue selection, then split or
filter its output while preserving every existing severity policy. Measure the
old commands and the combined command on the same commit and dependency state.
The runner-minute effect is a saving only when the combined command uses less
total runner time; the static finding does not size that saving by itself. The
public change measured one warm-cache screening pair at 91.28 s before and
65.91 s after: a 25.37 s (0.423 runner-minute) saving for that sample, not a
transferable clean-CI median or whole-pipeline estimate.

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

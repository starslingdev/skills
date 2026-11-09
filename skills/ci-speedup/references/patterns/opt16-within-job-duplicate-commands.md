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

Job logs can prove the Cargo variant even when the workflow commands differ:
if two distinct step groups print the same in-repository
`Compiling <crate> v<version> (<path>)` line, every local crate in the later
step already appeared in the earlier step, and both commands share the same
observable profile, target, feature, toolchain, compiler and target-directory
settings, the second step repeats compilation. Different crates or artifact
settings stay quiet. The later step's full `Finished` wall is an upper bound,
because it can include linking or other work beside the repeated compilation.

```bash
# For each job in each workflow, check if any command appears in multiple steps
for f in /tmp/workflows/*.yml; do
  # Extract run: commands per job and check for duplicates
  grep -oP '(?<=run: ).*' "$f" | sort | uniq -d
done
```

**Fix**: Remove the duplicate invocation. For Cargo, preserve every intended
package, feature, platform and test target. Combine package selections in one
invocation when they are meant to share artifacts, or align the feature, target
directory, toolchain and compiler settings that should produce the same artifact;
then confirm every original test target still builds and runs. If distinct feature
or platform coverage is intentional, keep both builds. If the rebuild is a
workaround for stale artifacts, fix the root cause (e.g., cache key, build system
incremental support) instead.

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
`Compiling <crate> v<version> (<path>)` line and finish the same profile, the
second step repeats compilation. Different crates, target triples, or dev and
release profiles are distinct work and stay quiet.

```bash
# For each job in each workflow, check if any command appears in multiple steps
for f in /tmp/workflows/*.yml; do
  # Extract run: commands per job and check for duplicates
  grep -oP '(?<=run: ).*' "$f" | sort | uniq -d
done
```

**Fix**: Remove the duplicate invocation. For Cargo, align `-p`/`--package` and
`--features` selection across every command in the job so the later test target
reuses the first compile, then confirm that target still builds and runs. If the
rebuild is a workaround for stale artifacts, fix the root cause (e.g., cache key,
build system incremental support) instead.

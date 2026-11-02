---
pattern: OPT83
impact: MEDIUM
class: structural
detector: critical-path
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: run-independent-pole-steps-in-parallel
title_template: 'Independent steps on the long pole run one after another'
category: 14
---

# OPT83 — Independent Steps on the Long Pole Run One After Another

**TL;DR**: The slowest job gating the merge runs several independent compute steps (say lint, typecheck and unit tests) one after another on one runner. Grouping them to run side by side could cut that job's wall time, by at most the sum of the steps minus the longest. That is an upper bound, not a saving: it is never credited until a benchmark confirms it.

**Anti-pattern**: A drilled long-pole job whose measured step decomposition shows two or more heavy compute steps, adjacent in the workflow file, none of which depends on another's output.

**Detection heuristic** (routed from the measured critical path, once per drilled pole; no gh call of its own). A step is a candidate when:

1. its category is compute (build, test, scan or package);
2. its name is not boilerplate and does not contain the word sleep, wait, wait-all or cancel;
3. it measures at least 20s and at least 15% of the pole's decomposed p50.

Two or more candidates that sit directly next to each other in the job's top-level `steps:` list form a run. Any other step between them breaks the run, including a setup step, a `parallel:` group and a `wait`, `wait-all` or `cancel` step: its effects, or the order it enforces, would change if the candidates moved together. A step already inside a `parallel:` group or marked `background: true` is not a candidate. When several runs qualify, the one with the largest upper bound is the only one checked: if it fails independence, the pole is held back (the held-back row names its steps), and no other run is tried.

**Independence (fails closed)**: independence is read from the workflow YAML, never from timings. If any fact below cannot be established, the pole is **held back** (counted, listed, and shown in the Data sources row "independent steps: held back"), never reported.

| Held back when | Why |
|---|---|
| step timings unreadable (a missing, non-numeric or non-finite p50 included); a step record unreadable (not an object, no name, no category, or one name twice); workflow file unreadable | nothing to derive the run from |
| the pole's check is produced by more than one workflow file | which file's steps ran is unknown |
| pole job not matched to one YAML job; a qualifying step not matched to exactly one YAML step | the timings cannot be tied to the YAML that would change |
| the pole's job holds a `parallel:` group that could not be read (malformed: not a list, contains itself, nested too deep, or holds a non-step item) | that group's steps were never read, so whether they write the build tree, start a server or set GITHUB_ENV is unknown |
| a candidate, or any step before the last one, sets `background:` with a `${{ }}` expression | whether it runs alongside them is only known when the workflow runs |
| two candidates use the same cache or artifact (`actions/cache`, `upload-artifact`, `download-artifact` with the same path, key or name) | they would race on it |
| a candidate is an action (`uses:`) | its effects are not read |
| a candidate publishes, deploys or uploads (measured category `package`, or `publish`, `deploy`, `upload`, `push`, `gh release`, `goreleaser`, an `s3`/`gsutil`/`azcopy` copy in its name or command; a bare `release` such as `cargo build --release` does not count) | it relies on the checks beside it having passed first; inside a group the default `if: success()` ordering between them is gone |
| a candidate reads another's `steps.<id>.outputs` | a real dependency |
| any candidate but the last writes `GITHUB_ENV`, `GITHUB_OUTPUT` or `GITHUB_PATH` | later steps would see different state |
| a `${{ }}` anywhere in a candidate other than its name (command, working directory, `env:`, `shell:`, ...), or in the job's or workflow's `defaults.run.working-directory` | it is only known when the step starts |
| a candidate changes directory (`cd`, `pushd`, `popd`) | the effective working directory is not statically known |
| a candidate that may write build output (build or package by name or by any command line; `tsc --noEmit` is not a build) shares its effective working directory with another candidate, or one directory sits inside the other (`./web`, `web/` and `web` are one directory), whichever step comes first. A working directory outside the checkout (`..`, `../x`, an absolute path) overlaps every tree. When a build is among the candidates, any candidate whose command reaches outside its own tree (`--prefix`, `--cwd`, `--dir`, `-C`, `--outDir` or `--out-dir`, a `../` or absolute path) counts as sharing every tree | run side by side, the build writes files the other step is reading |
| a candidate carries a step-level `if:` | the group's shape would change by condition |
| a candidate sets `continue-on-error` (anything but a literal `false`) | a step allowed to fail changes what a failing child does to the job once they run side by side |
| the job has `services:` containers; two candidates drive Docker; or two candidates in one tree run the same Cargo, Maven, Gradle, .NET (`dotnet test`, `build`, `run`, `publish`), sbt, Swift (`swift test`, `swift build`), Mix (`mix test`, `mix compile`) or Xcode (`xcodebuild`) build, or both run a recognised coverage tool (`pytest --cov`, `coverage run`, `nyc`, `c8`, `--coverage`, `go test -coverprofile`) | they share state the workflow file does not show as a dependency (one `target/`, `bin/` and `obj/`, `.build`, `_build` or DerivedData folder and its lock, one coverage file, one daemon or database) |
| any line, or any `&&`, `\|\|`, `;` or `\|` part of a line, of a candidate's command is a dependency install (`npm ci`, `pip install`, `uv sync`, `bundle install`, ...) | it writes the dependency tree every other candidate reads |
| any step up to the last candidate (before them or one of them) runs Docker, Podman or Compose, or is a container action (`docker://`, a Docker or Compose action); or any step before the first candidate runs with `background: true` | the job starts a container or a server the candidates would all use at once (one database, one port) |

These verdicts are counted only, never listed as held back: the pole has no workflow file (a check no workflow produces; one that more than one workflow produces is held back instead), fewer than two qualifying steps, qualifying steps not adjacent (anything between them: a step, a group or a wait), steps already run in parallel.

**Fix recipe**: rewrite only the qualifying steps as one `parallel:` group, leaving everything else in place:

```yaml
steps:
  - uses: actions/checkout@v4
  - run: npm ci
  - parallel:
      - name: Lint
        run: npm run lint
      - name: Typecheck
        run: npm run typecheck
      - name: Unit tests
        run: npm test
```

Parallel steps (changelog 2026-06-25) run the group's children concurrently with an implicit wait at the group's end. Sources: [GitHub changelog](https://github.blog/changelog/2026-06-25-actions-steps-can-now-be-run-in-parallel/) and [workflow syntax: `jobs.<job_id>.steps.parallel`](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idstepsparallel). **Runner caveat**: GitHub-hosted runners support it; on self-hosted runners, run actions/runner 2.336.0 or newer: its release notes list background steps in 2.335.0 and, in 2.336.0, a fix that stops a cancelled background step from affecting the job result. GitHub publishes no minimum runner version for the feature itself. The feature is documented for github.com and GitHub Enterprise Cloud, not GitHub Enterprise Server. At most 10 background steps run at once (more queue for a free slot); a background step's outputs are visible only after a `wait` or `wait-all` that covers it (this audit assumes the same of its `GITHUB_ENV` and `GITHUB_PATH` writes); the syntax is not usable inside composite actions.

**Mandatory guardrail (this pattern is invalid without it)**: the independence checklist above, a CPU and memory headroom check on the runner, and each step keeping its own log so a failure still names its step.

**No-weakening rail**: use `parallel:` (a failing child fails the job at the group's end or the next wait) rather than bare `background:`, unless every background step is covered by a `wait` or `wait-all` and none sets `continue-on-error`. Never `cancel:` a step that does real verification. Never drop, narrow or skip a step.

**Conservative rollout (REQUIRED)**: one PR; compare the job's wall time over the next sampled runs; keep the serial form on the merge queue until the gain is measured.

**Sizing — deliberately uncredited**: no `_SIZING` model, no wall-clock or runner-minute saving. The only number is the upper bound (sum of the candidates' p50s minus the largest), which assumes each step keeps its solo speed on one runner; CPU, memory and disk contention can erase it. It is never summed into any total, the headline, Tier 2 or Also noticed. **Benchmark first**: rewrite only those steps as one `parallel:` group on a branch, compare the job's wall time over the next sampled runs, and keep it only if the measured gain holds.

**Risk**: **MEDIUM** — a missed dependency can turn a green job into an order-dependent one.

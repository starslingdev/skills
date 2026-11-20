---
pattern: OPT114
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,Dockerfile,**/Dockerfile*'
fix_strategy: avoid-duplicate-docker-npm-audits
title_template: 'Docker image layers repeat npm advisory lookups already run by CI'
category: 3
---

# OPT114 — Docker Image Layers Repeat npm Advisory Lookups Already Run by CI

**Anti-pattern**: A CI job runs `npm ci` on the host with npm's default audit
reporting, then builds a local Dockerfile whose `RUN npm ci`, `npm install` or
`npm prune` commands make the same advisory lookup again. Several image layers
can repeat the request, and a slow advisory service can hold up the image build.

**Detection heuristic**: Require both halves in one readable job: a direct host
`npm ci` whose flags, environment and repository `.npmrc` keep audit enabled,
and a local Docker build whose resolved Dockerfile has at least one direct npm
install/prune command without `--no-audit`. Resolve action and raw `docker build`
paths inside the checkout. Expressions, remote contexts, unreadable files,
non-command mentions, a disabled host audit, and Docker commands that already
disable audit stay quiet.

**Fix recipe (safest first)**:

1. Keep the host `npm ci` audit reporting unchanged so the job still reports
   advisories once.
2. Add `--no-audit` only to each image-layer `npm ci`, `npm install` and
   `npm prune` that the finding names. Preserve lockfile resolution, lifecycle
   scripts, production pruning and every image input.
3. Build the old and new image from the same lockfile, compare the application
   artifacts and installed production package versions/integrities, then run the
   image's existing smoke or syntax check.

**Sizing**: Runner-minute effect is a **saving** because the same image-build
runner waits on fewer advisory requests. The static finding is **unsized**: it
has neither measured Docker-layer duration nor the job's 30-day run count. Use
matched before/after layer timings to size it; do not import another project's
numbers as a forecast. Wall-clock also goes down only when these layers are on
the job's gating path.

**Public evidence**:

- [OpenClaw Crabbox pull request #1804](https://github.com/openclaw/crabbox/pull/1804)
  retained audit reporting in the ordinary host install while adding
  `--no-audit` to the Dockerfile's install and production prune.
- [OpenClaw Crabbox pull request #1807](https://github.com/openclaw/crabbox/pull/1807)
  reports the original Dockerfile's install at 242.8 seconds and combined
  build/prune layer at 423.4 seconds, then an exact-head Docker build completing
  in 24 seconds. Those are that pull request's samples, not a forecast.

**Risk**: LOW when the host audit remains mandatory. The change must not disable
the last advisory report, alter dependency resolution or pruning, or skip the
image's parity checks.

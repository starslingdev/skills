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

**Detection heuristic**: Require both halves in one readable job. The host
half is a direct `npm ci` that keeps audit on: its flags, step/job/workflow
environment and every repository `.npmrc` from its effective working directory
(`defaults.run.working-directory` included) up to the checkout root leave audit
enabled, and it runs whenever the image build runs (no `if:`, or the build's
own `if:`). The image half is a local Docker build whose selected stage, or a
stage it builds from, runs `npm ci`, `npm install` or `npm prune` with audit
still on after copying in that same host lockfile. Image audit settings are read
as Docker applies them: `ENV`/`ARG NPM_CONFIG_AUDIT`, `npm config set audit`
and an `.npmrc` copied into the image, in instruction order; a build-context
`.npmrc` the Dockerfile never copies does not count. Heredoc bodies and quoted
text are data, not commands. Expressions, `cd`/`--prefix` directory changes,
remote contexts, unreadable files, unknown audit values, an unselected stage, a
separate lockfile, and Docker commands that already disable audit stay quiet.

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
  is the merged change for this shape.
- [OpenClaw Crabbox pull request #1807](https://github.com/openclaw/crabbox/pull/1807)
  measured the original Dockerfile reproducing the stall with install at 242.8
  seconds and build/prune at 423.4 seconds; the Docker build then completed in
  24 seconds. Those are that pull request's samples, not a forecast.

**Risk**: LOW when the host audit remains mandatory. The change must not disable
the last advisory report, alter dependency resolution or pruning, or skip the
image's parity checks.

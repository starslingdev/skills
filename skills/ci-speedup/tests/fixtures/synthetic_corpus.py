"""The synthetic repository the offline replay corpus (`gh_replay/`) was recorded against.

Shared by every consumer so the corpus and the checkout it describes cannot drift
between them:

  - `tests/test_offline_pipeline_e2e.py`, which drives scan -> collect -> render ->
    verify against it as a subprocess pipeline;
  - `evals/_scaffold_common.sh`, which every eval case's `scaffold.sh` sources to
    materialise the checkout, and the maintainer-only behavioural eval harness
    (`maintainers/ci-speedup/scripts/run_skill_evals.py`), which builds the replay
    dir, so a real agent session gets the same checkout and the same corpus; and
  - `tests/test_ci_speedup_evals_cases.py`, which renders the report offline from
    both to check the eval cases' graders against it.

It lives under `tests/fixtures/` so pytest never collects it, and so the eval
harness's plugin copy (which withholds `tests/fixtures/`) never mounts it: it
names every job and workflow the graders assert on.

Run as a script it materialises either half into a directory:

    python3 synthetic_corpus.py repo <empty-dir>
    python3 synthetic_corpus.py replay <dest-dir> [--span-seconds N]

The builder below this docstring was moved out of the e2e test verbatim with one
addition, the optional `span_s` forward band on the monthly-volume bracket; the
comments still speak from that file's point of view.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FIXTURES_DIR = _HERE / "gh_replay"
_SCRIPTS = _HERE.parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import collect_runs as cr  # noqa: E402

# The synthetic repo/workflow the committed fixture corpus was recorded
# against. `_REPO` must match the `owner/name` baked into every fixture
# filename (see `_fixture_name`); `_WF_YAML` must match the workflow content
# behind the `contents/.github/workflows/ci.yml` fixture, and produce the same
# job/step shape the `actions/runs/.../jobs` fixture describes.
_REPO = "synthetic/repo"
_WF_ID = 1001
_JOB_ID = 9001

# PR-H1: `push` is UNSCOPED (no `branches:`) so the same-head_sha push+PR run
# pair in the corpus satisfies OPT47's structural precondition (a push scoped
# only to the default branch is excluded by design).
_WF_YAML = """name: CI
on:
  pull_request:
  push:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      # The name must equal the replayed `steps[].name` in the jobs fixtures
      # ("Checkout"); the unnamed form folds to the same identity in production,
      # so this is fixture consistency, not a detector fix.
      - name: Checkout
        uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - name: Install dependencies
        run: npm ci
      - name: Run tests
        run: npm test
      # A `parallel:` group and a `wait-all:` on the POLE job (T8): the stamp
      # names this job in `jobs_with_groups`, so the step drill and the long-pole
      # map must word its steps as overlapping, end to end through the verifier.
      - parallel:
          - name: Docs
            run: npm run docs
          - name: Bundle size
            run: npm run size
      - wait-all:
"""

# PR-H1: the second workflow (wf id 1002). Three tiny same-SKU matrix legs
# (20/21/22s — each under OPT65's 60s tiny-job bar and under the workflow's own
# floor, which is its SECOND-slowest job: integration at 180s under the
# cacheless e2e long pole at 240s) drive the promotable OPT65 below-floor case, and its >10-run success
# sample makes the bill-pole fetch loop deepen a workflow OFFLINE (the shallow
# depth is 10). Push-only, so it never joins the PR spine or the close's poles.
_WF2_ID = 1002
_WF2_YAML = """name: Unit matrix
on:
  push:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # A GitHub Actions `parallel:` group: the static detectors read both
      # children through the step walker, and the findings carry the
      # `parallel_steps` stamp that the report's "Parallel steps" row renders.
      - parallel:
          - name: Build
            run: npm run build
          - name: Build docs
            run: npm run docs
  unit:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        part: [a, b, c]
    steps:
      - uses: actions/checkout@v4
      - name: Run unit slice
        run: npm run unit -- --part ${{ matrix.part }}
  integration:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with:
          path: node_modules
          key: integration-deps-${{ hashFiles('**/package-lock.json') }}
      - run: npm ci
      - name: Integration suite
        run: npm run integration
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: End-to-end suite
        run: npm run e2e
  lint-eslint:
    name: lint (eslint)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run lint:eslint
  lint-biome:
    name: lint (biome)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run lint:biome
  lint-stylelint:
    name: lint (stylelint)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run lint:stylelint
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run smoke
        run: npm run smoke
  deps:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with:
          path: node_modules
          key: node-modules-${{ hashFiles('**/package-lock.json') }}
      - run: npm ci
      - run: npm run typecheck
  docs-spell:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -r docs/requirements.txt
      - run: make docs-spell
  docs-links:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -r docs/requirements.txt
      - run: make docs-links
  docs-format:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -r docs/requirements.txt
      - run: make docs-format
"""


# ENG-1 PR-N1: the third workflow (wf id 1003) — a real `needs:` chain (`prep`
# 120s → `verify` 100s, artifact hand-off so OPT21 stays quiet). Its 220s chain
# EXCEEDS the 197s `CI / test` singleton, so the stamped per-PR chain facts
# exercise a genuine two-member chain offline. The stamped argmax
# (`critical_path_check`) still crowns `CI / test` — that field's semantics
# are unchanged; PR-N2 makes the rendered HEADLINE chain-aware, and the
# ranking/cascade work is PR-N3's (recorded as-built in the plan).
# Check-runs are named by the plain job name (GitHub's naming for plain jobs),
# which is what the graph resolver keys on.
#
# `prep` also restores a cache before `npm ci`, and the corpus carries SIX
# sampled pull_request runs (7001-7006, the last three reusing the first
# three's head shas on later timestamps, so no new check-runs fixture and no
# new PR population). Three are cache HITS (restore 30s + install 3s + post 0s
# = 33s) and three are MISSES (1s + 7s + 4s = 12s): a 21s excess per hit on
# the workflow's SLOWEST job, on a workflow that gates pull requests — OPT79's
# credited WALL-CLOCK arm, capped at the 20s headroom to `verify`. Every `prep`
# job is still exactly 120s in total (`Build artifact` absorbs the difference),
# so prep's p50, the 220s chain, the poles order and every other corpus number
# are unchanged by the cache steps.
_WF3_ID = 1003
_WF3_YAML = """name: Chained
on:
  pull_request:

jobs:
  prep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with:
          path: node_modules
          key: prep-deps-${{ hashFiles('**/package-lock.json') }}
      - run: npm ci
      - name: Build artifact
        run: npm run build
      - uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/
  verify:
    runs-on: ubuntu-latest
    needs: prep
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: dist
      - name: Verify artifact
        run: npm run verify
"""


# OPT77's WHOLE-WORKFLOW arm, both outcomes. A group is "the whole workflow"
# when every job the YAML DECLARES is a credited member and the sample carried
# nothing else — there is then no remaining job to measure the consolidated job
# against, so the comparison is against the gate the group sets TODAY (its
# slowest member's measured p50). That arm has its own stamps
# (`group_is_the_whole_workflow`, `gate_today_p50_s`, and `remaining_tallest_*`
# deliberately `None`), its own rendered sentence, and its own re-derivation in
# `verify_report`. None of it was reached by this e2e before these two
# workflows existed: a hard `raise` placed inside either branch left the whole
# test green.
#
# Both are PUSH-only, so neither joins the PR spine, the critical path or the
# close's poles — the chain/headline assertions above are untouched by them.
#
# (wf id 1004) FIRES. Three independent same-runner jobs, each declaring the
# same checkout → setup-node → `npm ci` prefix and measuring 400s (200s of
# setup, a 200s task). The projection is `max(setup) + max(useful)` = 400s,
# which is exactly the 400s the slowest member takes today, so consolidating
# them does NOT lengthen the merge gate and the runner-minute saving is
# credited. (The projection can never be BELOW the tallest member's own
# setup + task, so an exact tie is the whole of this arm's firing region —
# which is why the old "re-assemble the gate from step components" code could
# fire almost nowhere.)
_WF4_ID = 1004
_WF4_YAML = """name: Checks
on:
  push:

jobs:
  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
      - run: npm run typecheck
  contracts:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
      - run: npm run contracts
  schema:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
      - run: npm run schema
"""

# (wf id 1005) WITHHELD. The same shape, sized so the projection LOSES:
# `alpha` and `beta` pay 100s of setup for a 10s task, `gamma` pays 60s for a
# 50s task, and every one of them measures 110s. The consolidated job projects
# to max(setup) 100s + max(task) 50s = 150s, which is 40s MORE than the 110s
# the group sets today — so the bill saving is real but taking it would make
# the merge wait longer. No finding; the group is disclosed on the findings
# document and in the report's held-back row instead of vanishing.
_WF5_ID = 1005
_WF5_YAML = """name: Gates
on:
  push:

jobs:
  alpha:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
      - run: npm run alpha
  beta:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
      - run: npm run beta
  gamma:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
      - run: npm run gamma
"""


# OPT82 (lint builds the whole type graph), wf id 1006. One push-only job that
# runs `npm run lint`, which resolves through the repo-tree package.json
# (`lint` -> `lint:eslint` -> `eslint .`) to ESLint. It measures 93s at p50
# (lint step 72s): above OPT82's 60s bar, and far below the 220s chain and the
# 197s `CI / test` headline, so no pole, chain or headline assertion moves.
# `build-matrix.yml`'s `lint (eslint)` job runs `npm run lint:eslint` too, at
# 20s off that workflow's critical path: the below-threshold VERDICT, counted
# and never listed as held back.
# A second job, `eslint-web`, runs `npx eslint .` in `packages/web` (its
# `defaults.run.working-directory`), whose own flat config sets
# `projectService: process.env.CI` - a value this read cannot evaluate. At 75s
# p50 it is over the bar, so it is HELD BACK and listed, never a verdict. It
# rides on the same three sampled runs (`runs/820{1,2,3}/jobs` pages), so it
# costs no gh call, and it is shorter than `eslint`, so lint.yml's long pole
# does not move.
_WF6_ID = 1006
_WF6_YAML = """name: Lint
on:
  push:

jobs:
  eslint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
      - name: Lint
        run: npm run lint
  eslint-web:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: packages/web
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
      - name: Lint web
        run: npx eslint .
"""
# `packages/web`'s own flat config: type-aware parsing hangs on an environment
# variable, so whether the lint builds type information cannot be read offline.
_ESLINT_CONFIG_WEB_UNRESOLVED = """export default [
  {
    languageOptions: { parserOptions: { projectService: process.env.CI } },
    rules: { '@typescript-eslint/no-floating-promises': 'error' },
  },
];
"""

# The repo-tree side of OPT82: an ESLint flat config with type-aware parsing
# on (`projectService: true`), two enumerable typescript-eslint rules, and one
# CUSTOM rule whose source asks the parser for type information.
_PACKAGE_JSON = """{
  "name": "synthetic-repo",
  "private": true,
  "scripts": {
    "lint": "npm run lint:eslint",
    "lint:eslint": "eslint . --cache --max-warnings 0",
    "lint:biome": "biome lint .",
    "lint:stylelint": "stylelint 'src/**/*.css'",
    "test": "vitest run"
  },
  "devDependencies": {
    "eslint": "^9.12.0"
  }
}
"""
# ESLint 9 is declared so the nested `packages/web` flat config does not apply
# to the root `eslint` job (ESLint 9 looks the config up from the working
# directory); without a version the root job is held back as ambiguous.
_ESLINT_CONFIG_TYPE_AWARE = """import tseslint from 'typescript-eslint';
import local from './eslint-rules/index.mjs';

export default tseslint.config(
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
  },
  {
    plugins: { local },
    rules: {
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      '@typescript-eslint/no-explicit-any': 'warn',
      'local/no-unsafe-enum-access': 'error',
    },
  },
);
"""
# The syntax-only twin: its rules block holds only `no-explicit-any`, with no
# type-aware parsing and no type-checked preset. This pins the
# `type_aware_parsing_off` VERDICT (a measured "no", not a held-back withhold)
# for the lint job, and the detector emits nothing.
_ESLINT_CONFIG_SYNTAX_ONLY = """import tseslint from 'typescript-eslint';

export default tseslint.config(
  { rules: { '@typescript-eslint/no-explicit-any': 'warn' } },
);
"""
_ESLINT_RULES_INDEX = """import noUnsafeEnumAccess from './no-unsafe-enum-access.mjs';
export default { rules: { 'no-unsafe-enum-access': noUnsafeEnumAccess } };
"""
_ESLINT_TYPED_RULE = """import { ESLintUtils } from '@typescript-eslint/utils';
export default {
  meta: { type: 'problem', schema: [] },
  create(context) {
    const checker = ESLintUtils.getParserServices(context).program.getTypeChecker();
    return { MemberExpression(node) { void checker; void node; } };
  },
};
"""


# OPT81 A1 (wf id 1007, runs 8301-8308; these never collide with OPT82's
# `lint.yml`, which holds 1006 / 8201-8203).
# A runner-comparison matrix: one job, `bench`, with a
# STATIC display name, run on a standard GitHub-hosted label and on a larger
# GitHub-hosted size in every one of eight push runs (the workflow is
# `on: push`) — the same steps on both, 150s at the median on `ubuntu-latest`
# and 90s on `ubuntu-latest-8-cores`.
# The repo's own history therefore already holds both distributions, eight
# successful samples each, which is exactly what A1 compares; nothing is
# projected.
#
# Blast radius, chosen deliberately: `bench` is this workflow's long pole, but
# both labels run in every one of the eight runs, so A1 reads it as a runner
# matrix: the gap is measured and stated, and no wall-clock is credited (every
# run executes both legs). The workflow is push-only, so even a credited gap
# would be floored to 0 by `bound_developer_facing` (not a pull-request wait).
# The headline, the chain and every pole assertion above stay exactly as they
# were. Making it the pole would have rewritten the headline to prove nothing
# A1's own unit tests do not.
# `timeout-minutes` is declared so OPT57 plans no extra event-scoped run list.
_WF7_ID = 1007
_WF7_YAML = """name: Bench
on:
  push:

jobs:
  bench:
    name: bench
    timeout-minutes: 30
    strategy:
      matrix:
        runner: [ubuntu-latest, ubuntu-latest-8-cores]
    runs-on: ${{ matrix.runner }}
    steps:
      - uses: actions/checkout@v4
      - name: Run benchmarks
        run: npm run bench
"""


# OPT83 (independent steps on the long pole run one after another), wf id 1008,
# runs 8401-8403 (never colliding with OPT81's `bench.yml`, 1007 / 8301-8308).
# OPT83 is routed from the DRILLED POLES only, and the poles come from the pull
# request critical path, so this is a pull_request workflow (a push-only one
# would never be drilled) whose one job, `quality`, is a REQUIRED check (a
# non-required check is scoped off the spine and never becomes a pole). It runs
# on the same three PR head shas as the rest of the corpus, so it adds a check
# run to each existing check-runs page and no new PR. At 96s it is the shortest
# required check (under `verify`'s 100s, the 197s `CI / test` and the 220s
# chain), so the chain, the headline, the critical-path check and the first
# three poles are unchanged: it renders as pole 4.
# Its steps: checkout 3s, `npm ci` 12s, then `Lint` 30s, `Typecheck` 25s and
# `Unit tests` 25s, three compute steps written one after another, each at least
# 20s and 15% of the job, none reading another's outputs, env or files and none
# a build. OPT83 fires with an upper bound of 80 - 30 = 50s, uncredited.
# Lint is Biome (never ESLint, so OPT82 stays on lint.yml), and scan steps
# dominate (55s vs 25s of test), so OPT81's advisory reads it as not compute.
_WF8_ID = 1008
_WF8_YAML = """name: Quality
on:
  pull_request:

jobs:
  quality:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - run: npm ci
      - name: Lint
        run: npx biome check .
      - name: Typecheck
        run: npx tsc --noEmit
      - name: Unit tests
        run: npm test
"""
# The withhold twin: `Lint` writes a value to GITHUB_OUTPUT that `Typecheck`
# reads, so the steps are not independent. Same timings, same job: OPT83 must
# hold the pole back and name it, never fire.
_WF8_YAML_DEPENDENT = _WF8_YAML.replace(
    "      - name: Lint\n        run: npx biome check .\n",
    "      - name: Lint\n        id: lint\n"
    "        run: npx biome check . && echo \"count=3\" >> \"$GITHUB_OUTPUT\"\n",
).replace(
    "        run: npx tsc --noEmit\n",
    "        run: npx tsc --noEmit --maxNodeModuleJsDepth ${{ steps.lint.outputs.count }}\n")


# OPT84 (a slow step on the long pole runs a tool that has a native drop-in),
# wf id 1009, runs 8501-8503 (never colliding with OPT83's `quality.yml`, 1008 /
# 8401-8403). Shaped like mastra's lint pole. OPT84 is routed from the DRILLED
# POLES only, so, like `quality.yml`, this is a pull_request workflow whose one
# job, `style`, is a REQUIRED check, run on the corpus's three PR head shas (one
# more check run on each existing check-runs page, no new PR). At 92s it is the
# shortest required check (under `quality`'s 96s), so the chain, the headline
# and poles 1-4 are unchanged: it renders as pole 5 (the drill's cap). It must
# stay at or above the 90s triage floor (`_TRIAGE_WALLCLOCK_FLOOR_S`): a 68s
# version was triaged fast, its jobs never drilled, and it never became a pole.
# Its steps: checkout 3s, `npm ci` 36s, then ONE heavy step, `Run lint and
# format checks` (52s, 57% of the job), which runs `pnpm lint` in `apps/site`.
# That package's own package.json resolves it to `eslint . && prettier --check .`
# (two table tools on one step, so the split is unknown), declares `eslint` and
# `prettier` and no `typescript`, and its flat config loads one framework plugin
# (react), so the ESLint row is a dual-run. The config is not type-aware, so
# OPT82 has nothing to report on this step and never defers it. OPT83 reads one
# qualifying step (a verdict). OPT84 fires once, uncredited, ceiling 52s.
# The workflow file is `style.yml`, not `lint.yml`: `lint.yml` is OPT82's
# corpus workflow (1006).
_WF9_ID = 1009
_WF9_YAML = """name: Style
on:
  pull_request:

jobs:
  style:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - run: npm ci
      - name: Run lint and format checks
        working-directory: apps/site
        run: pnpm lint
"""
_SITE_PACKAGE_JSON = """{
  "name": "site",
  "private": true,
  "scripts": {
    "lint": "eslint . && prettier --check ."
  },
  "devDependencies": {
    "eslint": "^9.12.0",
    "eslint-plugin-react": "^7.37.0",
    "prettier": "^3.3.3"
  }
}
"""
_SITE_ESLINT_CONFIG = """import react from 'eslint-plugin-react';

export default [
  {
    plugins: { react },
    rules: { 'react/jsx-key': 'error' },
  },
];
"""

def _init_repo(root: Path, origin: str | None = _REPO,
               eslint_config: str = _ESLINT_CONFIG_TYPE_AWARE,
               quality_yaml: str = _WF8_YAML) -> None:
    """A one-commit git checkout carrying just the workflows the fixture corpus
    was recorded against. Committer identity travels via env vars (not global
    git config), so this works on a bare runner with no configured identity.

    An `origin` remote naming `_REPO` is part of the model, not decoration: collect_runs
    only reads workflow YAML off disk once it has VERIFIED the checkout is a clone of
    `--repo` (`_root_is_clone_of`), so a checkout with no origin — or somebody else's
    origin — falls back to the gh contents API. Pass `origin=None` (or another slug) to
    build that unverifiable checkout on purpose."""
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(_WF_YAML, encoding="utf-8")
    (root / ".github" / "workflows" / "build-matrix.yml").write_text(_WF2_YAML, encoding="utf-8")
    (root / ".github" / "workflows" / "chained.yml").write_text(_WF3_YAML, encoding="utf-8")
    (root / ".github" / "workflows" / "checks.yml").write_text(_WF4_YAML, encoding="utf-8")
    (root / ".github" / "workflows" / "gates.yml").write_text(_WF5_YAML, encoding="utf-8")
    (root / ".github" / "workflows" / "lint.yml").write_text(_WF6_YAML, encoding="utf-8")
    (root / ".github" / "workflows" / "bench.yml").write_text(_WF7_YAML, encoding="utf-8")
    (root / ".github" / "workflows" / "quality.yml").write_text(quality_yaml, encoding="utf-8")
    (root / ".github" / "workflows" / "style.yml").write_text(_WF9_YAML, encoding="utf-8")
    (root / "apps" / "site").mkdir(parents=True)
    (root / "apps" / "site" / "package.json").write_text(_SITE_PACKAGE_JSON, encoding="utf-8")
    (root / "apps" / "site" / "eslint.config.mjs").write_text(
        _SITE_ESLINT_CONFIG, encoding="utf-8")
    (root / "package.json").write_text(_PACKAGE_JSON, encoding="utf-8")
    (root / "eslint.config.mjs").write_text(eslint_config, encoding="utf-8")
    (root / "eslint-rules").mkdir()
    (root / "eslint-rules" / "index.mjs").write_text(_ESLINT_RULES_INDEX, encoding="utf-8")
    (root / "eslint-rules" / "no-unsafe-enum-access.mjs").write_text(
        _ESLINT_TYPED_RULE, encoding="utf-8")
    (root / "packages" / "web").mkdir(parents=True)
    (root / "packages" / "web" / "eslint.config.mjs").write_text(
        _ESLINT_CONFIG_WEB_UNRESOLVED, encoding="utf-8")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "ci-speedup-test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "ci-speedup-test", "GIT_COMMITTER_EMAIL": "test@example.com"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    # Pin the branch to the corpus repo's DEFAULT branch (`repos_synthetic_repo.json`
    # says `main`). `git init` otherwise takes the runner's `init.defaultBranch`, which
    # may be `master` — and collect_runs now checks the checkout's branch against the
    # repo's default (`_root_branch_skew`), so an unpinned branch name would make the
    # clean-collection e2e report a branch skew that has nothing to do with the code
    # under test. `symbolic-ref` works on every git version (`init -b` does not).
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=root, check=True, env=env)
    if origin:
        subprocess.run(["git", "remote", "add", "origin",
                        f"https://github.com/{origin}.git"],
                       cwd=root, check=True, env=env)
    subprocess.run(["git", "add", ".github", "package.json", "eslint.config.mjs",
                    "eslint-rules", "packages", "apps"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True, env=env)


def _monthly_volume_endpoints_bracket(span_s: int = 31) -> list[str]:
    """Every monthly-volume endpoint name the collect subprocess might compute
    for today's UNPINNED window (`--created-before` absent, as `run.py` invokes
    it): the workflow-wide count plus event-scoped counts used by measured
    bill-only detectors. The window's lower bound is `(now - 30d)` truncated to
    the SECOND (see `collect_runs._window_30d`), and the subprocess computes its
    `now` a beat AFTER this test writes fixtures — and on a slow/loaded CI runner
    the gap (Python startup, imports, `git init`, the corpus copy) can be many
    seconds, rolling the timestamp over. Bracket a ~30s forward band of candidate
    timestamps so the fixture is present whichever second the subprocess hits;
    all but the one it actually requests are harmless unused files. (Mirrors
    `_window_30d(None)`'s `>=<since>` / `%Y-%m-%dT%H:%M:%SZ` construction; if the
    engine ever changes that format the bracket stops matching and this test
    fails loudly with a gh_error, which is the correct signal.)

    `span_s` widens the forward band. The e2e's subprocess starts within seconds,
    so the default ~30s is enough there; an agent session reaches `run.py`
    minutes after its replay dir is written, so the eval harness passes its own
    session budget instead."""
    now = _dt.datetime.now(_dt.timezone.utc)
    out = []
    for off in range(-1, span_s):
        since = ((now + _dt.timedelta(seconds=off)) - _dt.timedelta(days=30)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        for wf_id in (_WF_ID, _WF2_ID, _WF3_ID, _WF4_ID, _WF5_ID, _WF6_ID, _WF7_ID,
                      _WF8_ID, _WF9_ID):
            out.append(f"repos/{_REPO}/actions/workflows/{wf_id}/runs"
                       f"?per_page=1&created=>={since}")
            out.append(f"repos/{_REPO}/actions/workflows/{wf_id}/runs"
                       f"?per_page=1&event=pull_request&created=>={since}")
    return out


def _replay_dir(tmp_path: Path, span_s: int = 31) -> Path:
    """A ready-to-replay fixture dir: the committed corpus PLUS the one
    time-dependent `_monthly_volume` fixture, bracketed over a forward band of
    `span_s` seconds (~30 by default; the eval harness passes its session
    budget, see `_monthly_volume_endpoints_bracket`), so the offline collection reports
    ZERO gh errors (no missed endpoint) and the clean-run assertion in the e2e
    can be an exact `== 0`."""
    dst = tmp_path / "gh_replay"
    shutil.copytree(_FIXTURES_DIR, dst)
    for endpoint in _monthly_volume_endpoints_bracket(span_s):
        (dst / cr._fixture_name(endpoint, "json")).write_text(
            json.dumps({"total_count": 42}), encoding="utf-8")
    return dst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("repo", help="materialise the synthetic checkout into DIR")
    r.add_argument("dir", type=Path)
    p = sub.add_parser("replay", help="write a ready-to-replay fixture dir under DIR/gh_replay")
    p.add_argument("dir", type=Path)
    p.add_argument("--span-seconds", type=int, default=31)
    args = ap.parse_args(argv)
    if args.cmd == "repo":
        _init_repo(args.dir)
        print(args.dir)
    else:
        print(_replay_dir(args.dir, args.span_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

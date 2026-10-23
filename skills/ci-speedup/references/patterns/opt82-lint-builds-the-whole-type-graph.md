---
pattern: OPT82
impact: MEDIUM
class: data-driven
detector: eslint-type-aware-lint
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,eslint.config.*,.eslintrc*,package.json'
fix_strategy: lint-builds-the-whole-type-graph
title_template: 'Lint Builds the Whole Type Graph'
category: 11
---

# OPT82 — Lint Builds the Whole Type Graph

**TL;DR**: A slow lint job runs ESLint with type-aware rules switched on. Those
rules make ESLint build a TypeScript program for the files it lints, the whole
type graph, so lint can cost close to a type-check. The report names the
rules that force that cost and asks for a benchmark before any change. It
carries no saving number, and it never recommends turning rules off.

**Anti-pattern**: ESLint is normally a syntax-only tool: it parses one file and
walks its tree. A type-aware rule (`no-floating-promises`, `no-unsafe-*`,
`no-misused-promises` and the rest of the list in
`references/type-aware-lint-rules.tsv`) needs types, so the parser is configured
with `parserOptions.project` or `projectService` and builds a TypeScript program
that resolves every import of every linted file. One such rule is enough: the
program is built once for the run, for all rules, including the ones that never
look at a type.

```
before:  eslint  ->  build TS program (whole type graph)  ->  every rule, every file   (lint ~ a type-check)
after:   fast pass:  eslint, syntax-only rules, no program        (cheap, every change)
         type pass:  eslint, ONLY the type-aware rules            (whole tree, a required PR check;
                                                                   changed files only WITH a merge queue)
         merge queue (if any) / default branch: the full type-aware pass, unchanged
```

**Detection heuristic** (every gate required; on a slow lint job, every input
the read could not see is a held-back "could not tell", never a verdict):

1. A YAML job runs ESLint: `eslint` directly (including the `npx`, `pnpm exec`,
   `yarn` and `bunx` forms, and a version-pinned `eslint@8`), or
   `npm|pnpm|yarn|bun run <script>` where the script, resolved through
   `package.json` `scripts` in the step's working directory, runs ESLint. A
   LINT-NAMED step the read cannot follow is held back, never guessed, under
   its own gate: a lint-named script that is not defined there, is scoped to
   another package (`--filter`, `yarn workspace <pkg> lint`) or nests past the
   depth cap (`lint_script_unresolvable`); a `package.json` that could not be
   read (`package_json_unreadable`); a `${{ }}` expression in the command or the
   working directory (`lint_step_uses_runtime_expression`); and lint handed to
   another tool or action (`turbo`, `nx`, `lerna`, `make`, `next lint`,
   `run-p` / `npm-run-all`, a `node` script, an action step whose name or
   reference says ESLint, or a local `./` or `$/` action named lint:
   `lint_delegated_to_unread_tool`). A step that runs a linter that is not
   ESLint (golangci-lint, stylelint, biome, actionlint) is not a lint step here.
2. The job is expensive: its measured p50 is at least 60s
   (`_OPT82_MIN_LINT_P50_S`), or it is its workflow's measured long pole in a
   workflow with more than one job (a one-job workflow's only job is trivially
   its own pole and does not waive the bar).
3. Type-aware parsing is ON in the ESLint config that applies to the lint job's
   directory: the file passed with `-c` / `--config`; otherwise ESLint's mode
   first: a flat `eslint.config.*` at or above the directory means flat mode,
   where every `.eslintrc*` is ignored (unless `ESLINT_USE_FLAT_CONFIG=false`
   inline or in the step, job or workflow env) and the nearest flat config
   applies; with no flat config there, the legacy mode uses the nearest
   config at or above that directory, plus nested legacy `.eslintrc*` configs
   below it, and, when the nearest is a legacy `.eslintrc*`, its ancestors up to
   the first with `root: true` (a nearer config that sets `project: false` /
   `null` shadows the farther ones). A nested FLAT config below the directory
   applies under ESLint 10, which looks the config up from each linted file,
   and not under ESLint 9, which looks it up from the working directory. The
   `eslint` version the nearest `package.json` declares decides: below 10 the
   nested config is not counted; 10 or above, or no version readable, and the
   candidate is held back (`eslint_config_lookup_ambiguous`). ON means a literal
   `parserOptions.project` (`true`, a string or an array of strings), a
   `projectService` (`true`, an object or a string value), or a type-checked preset referenced
   as a config (`tseslint.configs.recommendedTypeChecked`,
   `plugin:@typescript-eslint/strict-type-checked`,
   `recommended-requiring-type-checking`, typescript-eslint's `configs.all`;
   the bare name in an unrelated string does not count). Each preset also
   counts in its `-type-checked-only` / `TypeCheckedOnly` form. A preset only
   turns rules on, so reading it as "the type graph is built" is an INFERENCE,
   not a read fact: a typed rule with no `parserOptions.project` /
   `projectService` makes ESLint throw, so a lint run that passes with one of
   these presets must set type-aware parsing somewhere, possibly in a file this
   read cannot see. The
   `disable-type-checked` config never counts, and `project: false` / `null` is
   OFF. Comments are stripped before reading. A local base the config pulls in
   (`import base from './base.mjs'` spread into a flat config, or a relative
   `extends: ['./eslint/base.js']`) is followed and read as part of the config.
   Any other value (an identifier, an environment variable, a ternary, a
   `parserOptions` taken from a variable) is unresolvable: the candidate is held
   back, not read as on or off. "Off" is a verdict only when the read saw
   everything that could turn it on: a config that extends a shareable config
   package (`@acme/eslint-config`, `extends: ['airbnb']`; `prettier` and the
   `next` configs are known not to and stay a verdict) is held back
   (`shared_config_unfollowable`), as is one with a local import the read could
   not follow (`config_import_unfollowed`) and one that turns a type-aware rule
   on with no parser setting in view (`type_aware_rule_without_parser_setting`:
   the rule cannot run without one, so it is set somewhere unread).
   The config walk is bounded; when it stopped before it could show the lint
   directory was visited (no config recorded at or below it), the candidate is
   held back (`eslint_config_walk_incomplete`), and the verifier fails an OPT82
   finding stamped from such a walk or from a failed read.
4. At least one type-aware rule can be NAMED from the config. Type-aware parsing
   being on somewhere, with no rule named, is a claim about shape and the
   candidate is held back.

How the rules are enumerated: every rule in `references/type-aware-lint-rules.tsv`
that the config turns on, either explicitly (an entry not set to `off` / `0`) or
through a type-checked preset and not explicitly off, PLUS custom rules. A custom
rule counts when its key `<namespace>/<name>` is turned on in the config and its
source file, reached by following the config's RELATIVE imports (bounded), calls
`getParserServices` or `getTypeChecker`, AND the match is proven: the plugin
module that imports the source maps `<name>` to it, and the config binds that
module under `<namespace>` (`plugins: { local }`). A file-name match alone is
unproven and the rule is unresolved, never named. Settings are read without
their order, so a rule that is on and also turned off for every file, or a
type-checked preset with an unscoped `disableTypeChecked`, is unresolved; an
`off` or `disableTypeChecked` scoped by `files:` leaves the rest on. A rule
whose severity is only known at run time (a computed value) is never named;
when that leaves no rule to name,
the candidate is held back (`rule_setting_unresolvable`) rather than read as
having none. The prompt's `eslint --print-config` ledger covers any rule the
static read could not name.

The data file lists 63 rules, every typescript-eslint rule documented as
requiring type information, with the core ESLint rule each one extends (where
typescript-eslint documents one) and the type-checked presets that enable it.
Source: typescript-eslint `main` at commit 706298716de5, fetched 2026-10-07 (UTC);
the header of the file carries the full provenance. The file is sorted and
deduplicated and a test pins both.

**What is NOT detected** (silent today, by design, not by oversight):

- Lint run through a marketplace action whose name and reference do not say
  ESLint (a generic "super-linter" step), where no ESLint command is in the
  workflow YAML.
- A slow job that is not lint-named and runs ESLint only through a script or
  tool this read does not follow.
- On a config that IS read as on with a named rule, a shared config package it
  also extends may add more type-aware rules than the static read names. The
  prompt handles this at fix time: it tells the agent to compute the REAL
  enabled rule set with `eslint --print-config <file>` instead of trusting the
  static read.

**Withhold gates.** Every exit is counted in `findings_doc["opt82_withheld_by_gate"]`.
A verdict is a measured "no"; a held-back exit is a "could not tell", listed in
`findings_doc["opt82_withheld_candidates"]` and rendered in the Data sources
table as the `type-aware lint: held back` row (noun "candidate lint job(s)"),
which feeds "Why a slow lint job with type-aware ESLint produced no finding".

| gate | kind | what it means |
|---|---|---|
| `lint_job_never_ran_in_sample` | verdict | the lint job did not run in any sampled run |
| `lint_job_below_cost_threshold` | verdict | the job is under 60s and not a long pole |
| `type_aware_parsing_off` | verdict | the config applying to the job does not build the type graph |
| `workflow_yaml_unparsed` | workflow count | the workflow YAML did not parse; there is no job to name, so it is counted only, and disclosed through `detectors_skipped` |
| `lint_script_unresolvable` | held back | the lint step runs a package script this audit could not trace to the command it runs |
| `lint_step_uses_runtime_expression` | held back | the lint step's command or working directory is only known when the workflow runs |
| `lint_step_cd_untraceable` | held back | the lint step changes directory (`cd`) to a place the read cannot follow (absolute, `-`, home, outside the repository) |
| `lint_delegated_to_unread_tool` | held back | the lint step hands lint to another tool or action whose own configuration this audit did not read |
| `package_json_unreadable` | held back | the package.json that defines the lint step's script could not be read |
| `type_aware_lint_scan_missing` | held back | the repository scan behind this report carries no ESLint config read (an older scan) |
| `type_aware_config_reader_failed` | held back | the ESLint config reader failed, so whether lint builds the type graph is unknown |
| `eslint_config_walk_incomplete` | held back | the bounded config walk stopped before the lint job's directory, so a nearer config may have been missed |
| `no_eslint_config_found` | held back | no ESLint config was found for the lint job's directory |
| `eslint_config_unreadable` | held back | an ESLint config the lint job uses could not be read |
| `eslint_config_lookup_ambiguous` | held back | a nested flat config below the lint directory applies under ESLint 10 and not 9, and the version is 10+ or unknown |
| `type_aware_setting_unresolvable` | held back | the config sets type-aware parsing from a value only known when it runs |
| `shared_config_unfollowable` | held back | the lint config extends a package this audit did not read |
| `config_import_unfollowed` | held back | the ESLint config imports a local file this audit could not read |
| `type_aware_rule_without_parser_setting` | held back | a type-aware rule is on but type-aware parsing is set somewhere this audit did not read |
| `rule_setting_unresolvable` | held back | the config sets a type-aware rule from a value only known when it runs |
| `no_enumerable_type_aware_rule` | held back | type-aware parsing is on, but no type-aware rule could be named from the config |

**Sizing — uncredited, benchmark required**: There is no `_SIZING` model. The
finding carries `wall_clock_p50_s = 0`, no runner-minute saving and sizing basis
`uncredited`, because nothing in the sampled data says how much of the lint step
is the type graph: that depends on the repository's import graph and on how many
rules need types. What the report can state is a ceiling: the lint step's
measured p50 (or the lint job's, when the step is not separately measured) is an
upper bound on what any change to lint can save. It is not a forecast. The
prompt asks the agent to run a two-command benchmark first, on the same runner,
before touching any config: the lint exactly as CI runs it, then the same lint
with `--parser-options project:false --parser-options projectService:false` plus
`--rule '<rule>: off'` for each enumerated rule. That second run is for TIMING
ONLY and is never committed; both runs are cold (no `--cache`). The difference
is the real size of the prize, measured, and a small difference ends the work.

For scale, Linear rewrote their custom type-aware rules to work on the syntax
tree alone (fix (a) below), which let their ESLint run without TypeScript, and
reported API lint time falling 68% and full-repository lint time falling 55%
(https://linear.app/now/ci-bottleneck-reworked). That is their result from
taking type information out of lint, not from a split, and not a forecast for
any other repository.

**Fix recipe**, in this order. Dropping the type-aware rules is NOT a fix this
pattern recommends: every rule the repository enforces today must still run
somewhere.

- (a) **REWRITE.** For each enumerated rule, ask whether a syntax-only form of the
  same check exists (the `syntax_only_equivalent` column is a pointer, not an
  answer: the core rule checks less because it cannot see types). A rule that is
  satisfied by a syntax-only rule in this repository moves to the fast pass; the
  question for each rule is whether the replacement catches the same defects.
- (b) **SPLIT.** Run two ESLint passes. The fast pass runs every syntax-only rule
  with type-aware parsing off, on every change (it may use `--cache`, see OPT9).
  The type-aware pass runs exactly the enumerated rules. Where it runs depends on
  whether a full pass still gates the merge; the finding records which sampled
  workflows declare `merge_group` (`merge_group_workflows`):
  - **With a merge queue** (or another required full pass before merge), the
    pull-request type-aware pass may lint only the changed files, as an explicit
    list (`git diff --name-only --diff-filter=ACMR <base>...HEAD` filtered to
    TypeScript sources and passed to `eslint <files>`), or only the touched
    packages (OPT70). The full type-aware pass over the whole tree runs as a
    required check on the merge queue and on the default branch.
  - **Without one**, the type-aware pass stays a REQUIRED pull-request check over
    the whole tree. It may move into its own parallel job for wall clock, but it
    is never scoped to changed files: the full pass would then run only after
    merge, and a type error a change causes in an unchanged file would stop
    blocking the pull request, which is the move to a non-blocking job the
    guardrail forbids. If the split cannot help without that, go to (c) or keep
    the single full pass.
  - **Never ESLint's `--cache` on the type-aware pass.** typescript-eslint's FAQ
    says not to use it with typed linting: the cache does not track cross-file
    type dependencies, so a cached file can pass after a change elsewhere breaks
    its types.
- (c) **NATIVE.** Move the type-aware rules to a native type-aware linter
  (`tsgolint` through oxlint's type-aware mode) only when the remaining rule set
  is one that tool supports, under OPT84's tool-swap table and its
  no-weakening rail.

**Guardrail**: The prompt requires a rule-by-rule ledger, one row per rule the
current config enables (from `eslint --print-config`): the fast pass, the
type-aware pass, or REPLACED BY `<rule>` with one line on what the replacement no
longer checks and why that is acceptable. A REPLACED row is a deliberate,
reviewed reduction in what lint checks (fix (a) can swap, for example,
`@typescript-eslint/require-await` for the base `require-await`, which cannot see
types) and is listed for a human to approve before merging. The ledger test
asserts that every original rule is enabled in one of the two configs or has a
REPLACED row; a rule with no row blocks the change. The shared no-weakening rail applies: the change may not delete a rule,
lower a severity, widen an ignore list or move a check to a non-blocking job.

**Risk**: MEDIUM. The split changes what runs on every change, and a rule left out
of both configs would pass silently. The ledger and its test make that failure
loud. A file touched in one package can break a type-aware rule in another that
a scoped pass does not lint, which is why a scoped pull-request pass is offered
only where the full type-aware pass still gates the merge on a merge queue.

**Why not just oxlint**: oxlint covers most syntax-only rules at a fraction of the
cost and is the right fast pass. It does not remove the type-aware rules; those
still need a type-aware tool. See the ESLint → oxlint row in OPT84's tool-swap
table: OPT82's ledger is the prerequisite that row assumes.

# Contributing

Thanks for your interest in improving `ci-speedup`. Pull requests are welcome.

## Ground rules

- **Open a PR — never push to `main`.** `main` is protected; branch protection
  requires the `test` check (the full `pytest -v` suite) to pass before a PR can
  merge.
- **The full test suite must pass.** From the repo root:

  ```bash
  python3 -m pytest -v
  ```

  CI runs the same command on every push and PR (`.github/workflows/ci.yml`).
  Tests live under `skills/ci-speedup/tests/`, `maintainers/ci-speedup/tests/`,
  and the repo-root `tests/`; the root `pyproject.toml` wires the paths so one
  `pytest -v` finds them all. A pre-commit hook (`.githooks/pre-commit`, enable
  with `git config core.hooksPath .githooks`) runs the same suite locally.

- **Keep the changelog current.** Every change that alters skill behavior adds a
  dated (UTC) bullet to
  [`skills/ci-speedup/CHANGELOG.md`](skills/ci-speedup/CHANGELOG.md) under the
  right Added / Changed / Fixed heading, **in the same PR** — if you changed the
  skill and didn't touch its changelog, the PR is incomplete. Pure-docs or
  test-only refactors that don't change behavior can be noted briefly or skipped.

- **Stage by explicit path.** When committing, `git add` only the files you
  changed — never `git add -A` / `git add .`, which sweeps in unrelated or
  generated files.

- **Don't commit local run data.** `.ci-speedup-gaps/`, `.ci-speedup-loop/`, and
  `.ci-speedup-dogfood/` are gitignored maintainer-loop capture dirs — they may
  hold third-party job logs (and, for the dogfood dir, full repo clones) and are
  never committed.

## Pull requests from forks

External contributions are welcome and follow the standard fork flow:

- **Fork, branch, and open a PR against `main`.**
- **CI runs the full test suite on your PR** on GitHub-hosted runners
  (`ci-fork.yml`), with no access to repo secrets.
- **Internal pushes and PRs run on StarSling's own runners** (`ci.yml` — we
  dogfood what we sell). By design, fork-PR code never executes on those
  self-hosted runners; the hosted workflow gives you the identical suite.
- **Local gate: green on your machine means green in CI.** The same
  `python3 -m pytest -v` runs in both places, so you can reproduce CI locally
  before you push.

## Registry security scanning

Skill registries publish third-party security audits of every skill they list, and
a skill can fail one without failing anything in this repo — the text a skill ships
is the thing being scanned, so an illustrative example can read to a scanner as the
attack it describes. `.github/workflows/registry-scan.yml` runs one of those
scanners (Snyk Agent Scan) over the installable trees under `skills/` on every
internal pull request, on pushes to `main`, weekly on a schedule, and on demand, so
a violation fails our build instead of appearing as a public FAIL badge days after
release.

Two things about that gate are worth knowing before you rely on it.

**It reproduces one scanner, not all of them.** Registries run several. This gate
covers Snyk Agent Scan only. It says nothing about Gen Agent Trust Hub — which
publishes no rule catalog and no CLI, and whose verdicts are LLM-generated and not
reproducible from one run to the next — or about Socket. A green check means "Snyk
Agent Scan finds nothing we have not accepted", not "every registry scanner will
pass".

**Only critical findings block.** The scanner sorts findings into a critical class
(`E`-codes) and a warning class (`W`-codes). A critical finding fails the build. A
warning never does — warnings on these skills are usually accurate descriptions of
what the skills legitimately do, such as W011 "exposure to untrusted third-party
content", which is what reading a repository's own CI configuration and job logs
looks like to a scanner. They are judged by a human, not by CI.

Warnings are never hidden, though. Every run surfaces the full finding set three
ways, because "does not block" must not quietly become "does not appear":

- **Annotations** on the checks tab, one per warning — also readable through the
  checks API, which is how an automated review agent picks them up.
- **A job summary table** listing every finding, its code, severity, and skill.
- **A `registry-scan-findings` artifact** holding the scanner's raw JSON, kept for
  14 days. Review agents and tooling should read this rather than scraping the log:
  `gh run download <run-id> --name registry-scan-findings`.

If you are adding a warning code to the ignore list, it must be a `W`-code from
[the scanner's published catalog](https://github.com/snyk/agent-scan/blob/main/docs/issue-codes.md),
and it goes in both the workflow and `tests/test_registry_scan_workflow.py`. An
`E`-code can never go in the ignore list — that would disarm the gate while leaving
it green, and the tests fail if one appears.

The gate needs a `SNYK_TOKEN` repository secret (a free Snyk account). Without it
the job fails loudly rather than passing quietly — a skipped security scan reported
as green is the exact failure this gate exists to prevent. Fork pull requests cannot
reach repository secrets, so they get a separate, clearly named check that reports
the coverage gap; a maintainer re-runs the scan on an internal branch before merging
fork changes under `skills/`.

## Adding or changing detection patterns

The pattern catalog is
[`skills/ci-speedup/references/optimization-patterns.md`](skills/ci-speedup/references/optimization-patterns.md);
each pattern's detector is registered in `skills/ci-speedup/scripts/`. See
[`skills/ci-speedup/ARCHITECTURE.md`](skills/ci-speedup/ARCHITECTURE.md) for how
the pipeline fits together and
[`maintainers/ci-speedup/MAINTAINERS.md`](maintainers/ci-speedup/MAINTAINERS.md)
for the maintainer runbook.

## Support

Issues are welcome and handled on a **best-effort basis — there is no SLA**. For
a security vulnerability, use private reporting instead (see
[SECURITY.md](SECURITY.md)).

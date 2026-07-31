# ci-score — maintainer notes

**Maintainers only.** This file is a sibling of the installable
`skills/ci-score/` tree; the `skills` CLI never copies `maintainers/` into an
end-user install.

## What lives here, and what deliberately does not

ci-score shipped to this public repo as a **finished, dogfooded skill** at CI
Score registry `ci-score-v0.1.3`. The port *is* the launch.

The skill's maintainer-only development infrastructure — the graded third-party
**calibration corpus** (dry-run score tables and collected receipts for the
named repos the rubric was calibrated against), the **build/launch planning
specs**, and the **dogfood-sweep captures** — is **not part of this public
repository.** It stays in the skill's pre-public development archive, because
publishing graded analyses of named third-party repositories would put those
third-party grades into public git history.

`tests/test_ci_score_install_surface.py` makes that boundary a PASS/FAIL
invariant: it fails if a calibration/specs/dogfood/loop directory, a
`.ci-score-*` runtime-capture dir, a maintainer-data file (a `calibration` /
`dry-run` / `collected` / `receipts` / `grade` / `corpus` / `dogfood` name), or a
third-party **grade table** (a markdown row pairing a Repo/Repository column with
a Score/Grade column) appears under the installable `skills/ci-score/` tree. A
positive-control test asserts each detector actually fires, so the guard cannot
pass vacuously if a pattern is later broken.

**Scope of the "no third-party grades" boundary.** What stays out is the bulk
calibration **corpus** — the multi-repo dry-run grade *tables* and collected
per-repo receipts. It is *not* a blanket ban on any score of any public repo
appearing in the tree: the frozen `references/ci-score-spec.json` decision record
and `skills/ci-score/CHANGELOG.md` legitimately cite a handful of public
positive-control reference scores (e.g. the two calibration anchors) as
governance receipts for version bumps. Those are part of the frozen registry, are
computed from public CI configuration, and ship by design — the install-surface
invariant intentionally does not flag them.

## The frozen registry

`skills/ci-score/references/ci-score-spec.json` is the frozen CI Score registry
(`spec_version` `ci-score-v0.1.3`). It changes **only** with a deliberate,
calibrated version bump and a recorded entry in its own `decision_log` /
`changelog`. `spec_version` moves when scoring semantics move (checks, gates,
refusals, bands, formula) — never for a prose-only edit. The scorer
(`scripts/ci_score.py`) is deterministic and byte-identical across runs; the
test suite pins the spec's version, structure, and scorer contract.

## Tests

From the repo root, `python3 -m pytest -q` runs every suite (the root
`pyproject.toml` wires the paths, including `skills/ci-score/tests`). CI runs
the same command.

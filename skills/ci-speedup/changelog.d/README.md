# Pending changelog entries for `ci-speedup`

Each file here is one pending entry for [`../CHANGELOG.md`](../CHANGELOG.md).
A pull request that changes this skill's behavior adds a new file here and
leaves `CHANGELOG.md` alone, so two open pull requests never edit the same
lines. CI fails a pull request that edits `CHANGELOG.md` by hand.

## Format

One file per change, named `<slug>.<kind>.md`:

- `<slug>` is anything unique: lowercase letters, digits, `-` and `_`
  (a few words describing the change, or the pull request number).
- `<kind>` is `added`, `changed`, `fixed` or `removed`. It picks the
  `### Added` / `### Changed` / `### Fixed` / `### Removed` heading the entry
  is filed under.

The file holds the exact markdown bullet(s) that would have gone into
`CHANGELOG.md`. Each bullet opens with its UTC date in bold and cites its pull
request as `(#N)`. Continuation lines are indented:

```markdown
- **2026-11-20** — One sentence a reader of the changelog needs (#123).
  Wrapped lines are indented two spaces.
```

No heading goes in the file.

## When the entries move into CHANGELOG.md

A maintainer runs the assembler, at release or whenever `CHANGELOG.md` should
catch up, and opens a pull request with its output unedited:

```bash
python3 maintainers/scripts/assemble_changelog.py
```

It files every entry here under `## [Unreleased]` in `CHANGELOG.md`, newest
first, and deletes the files it filed. CI accepts a `CHANGELOG.md` change only
when it is exactly the assembler's output for the entries that same change
deletes. To validate entries without writing anything:

```bash
python3 maintainers/scripts/assemble_changelog.py --check
```

The assembler lives under `maintainers/`, outside this installable directory.

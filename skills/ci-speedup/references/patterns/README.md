# Pattern files

One file per ci-speedup catalog pattern. Agents: find the pattern in
[`INDEX.md`](INDEX.md), then read its `opt<N>-*.md` file in full.

## File contract

- **Name:** `opt<N>-<slug>.md`, no zero padding; `<N>` is the pattern id and
  `<slug>` its `fix_strategy`. A file never moves once published.
- **Frontmatter**, in this order: `pattern`, `impact` (HIGH / MEDIUM / LOW),
  `class` (static / data-driven / structural), `detector`, then the optional
  `risk`, `match`, `wf_name_filter`, `wf_name_exclude`, then `affected_files`,
  `fix_strategy`, `title_template`, then `category: <1-14>`. Optional:
  `status: cut | removed` (the id stays retired, never reused); `aliases:` (old
  heading anchors that published links still use, kept when a pattern is
  retitled) with an `alias_note:` saying why. Quote values with `'...'`.
- **Body:** `# OPT<N> — Title` on the first line after the frontmatter, one
  blank line, then the entry (`**TL;DR**`, `**Anti-pattern**`, detection,
  fix, risk). Sub-headings start at `##`.

## Adding or changing a pattern

1. Write or edit the file. A new pattern takes the next unused id.
2. Add its line to `INDEX.md` under its category, where it should sit in the
   compiled catalog: `- [OPT<N> — Title](file) · IMPACT · class · one sentence.`
3. Run `python3 maintainers/ci-speedup/scripts/build_catalog.py` and commit
   the file, `INDEX.md` and the regenerated `../optimization-patterns.md`.

`../optimization-patterns.md` is generated (it keeps every published report
link and anchor working); never edit it by hand. `_frame.md` holds its
preamble and category headings. The build checks the contract above and CI
runs it with `--check`. Registering a detector: `maintainers/ci-speedup/MAINTAINERS.md`.

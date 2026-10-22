#!/usr/bin/env python3
"""Compile ci-speedup's per-pattern files into the single public catalog.

    python3 maintainers/ci-speedup/scripts/build_catalog.py           # write it
    python3 maintainers/ci-speedup/scripts/build_catalog.py --check   # CI: diff only

Sources, all under `skills/ci-speedup/references/patterns/`:

  opt<N>-<slug>.md  one pattern: YAML frontmatter (the METADATA keys in their
                    order, then `category`, optional `status`, `aliases`,
                    `alias_note`), then `# OPT<N> — Title` and the body.
  INDEX.md          one `## Category N — Name` section per category (its
                    Contents bullet is rebuilt from the heading and the blurb
                    under it) and one line per pattern; the line order is the
                    compiled order.
  _frame.md         the compiled file's other text: the preamble, the text
                    after the Contents bullets, and each `## Category N:` block.

Output: `skills/ci-speedup/references/optimization-patterns.md`, a generated
file kept at its published path so every report link and `#optN--` anchor
still lands. Each title becomes `### OPT<N> — Title`, deeper headings drop two
levels, the frontmatter is re-emitted as the `<!-- METADATA -->` comment,
`aliases` as `<a id>` lines, and a banner tops the file.

Checks (every run, before writing): INDEX.md lists every pattern file exactly
once and nothing else; each file name's `opt<N>` matches its `pattern`; the
required keys are present; the INDEX line's title, impact and class match the
file; each category's patterns are contiguous and its frame block lands before
the first of them; the compiled file parses (`scan.load_catalog`) to exactly
the entries the directory parses to, and every compiled heading's GFM anchor
equals that entry's `anchor`. `--check` exits 1 when the committed file differs
from the build (a hand edit, or a source edited without rebuilding).
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SKILL = _REPO / "skills" / "ci-speedup"
PATTERNS = _SKILL / "references" / "patterns"
COMPILED = _SKILL / "references" / "optimization-patterns.md"
sys.path.insert(0, str(_SKILL / "scripts"))
import scan  # noqa: E402

BANNER = (
    "<!-- GENERATED FILE, do not edit: maintainers/ci-speedup/scripts/build_catalog.py builds it from\n"
    "references/patterns/ (one file per pattern, ordered by patterns/INDEX.md). Agents: do not read this file;\n"
    "find a pattern in references/patterns/INDEX.md and read references/patterns/opt<N>-*.md. -->\n"
)
REQUIRED = ("pattern", "impact", "class", "detector", "affected_files", "title_template", "category")
# METADATA values written in double quotes (every other key is bare).
_QUOTED = {"affected_files", "title_template", "match", "wf_name_filter", "wf_name_exclude"}
_INDEX_LINE = re.compile(
    r"^- \[(OPT\d+) — (.+)\]\((opt\d+-[a-z0-9_-]+\.md)\) · (HIGH|MEDIUM|LOW) · ([a-z-]+) · \S.*$")
_INDEX_CAT = re.compile(r"^## (Category (\d+) — .+?)(?: (\(`OPT.*\)))?$")
_FRAME_MARK = re.compile(r"^<!-- frame: (.+) -->\n", re.M)
_ALIAS = re.compile(r"^[a-z0-9_-]+$")


class BuildError(Exception):
    pass


def _read_index(path: Path) -> tuple[list[tuple[int, str, str, str]], list[tuple[str, str, str, str, str, int]]]:
    """(categories [(n, name, paren, blurb)], patterns [(id, title, file, impact, class, category)])."""
    cats, pats = [], []
    lines = path.read_text(encoding="utf-8").splitlines()
    cur = None
    for i, line in enumerate(lines):
        m = _INDEX_CAT.match(line)
        if m:
            blurb = next((l for l in lines[i + 1:] if l.strip()), "")
            if blurb.startswith(("- [", "## ")):
                raise BuildError(f"INDEX.md: {m.group(1)} has no blurb line under its heading")
            cur = int(m.group(2))
            cats.append((cur, m.group(1), m.group(3) or "", blurb))
            continue
        if line.startswith("- ["):
            p = _INDEX_LINE.match(line)
            if not p:
                raise BuildError(f"INDEX.md line {i + 1} is not `- [OPT<n> — Title](file) · IMPACT · class · sentence`")
            if cur is None:
                raise BuildError(f"INDEX.md line {i + 1}: pattern listed before any category heading")
            pats.append((*p.groups(), cur))
    return cats, pats


def _frame(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    marks = list(_FRAME_MARK.finditer(text))
    return {m.group(1): text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))]
            for i, m in enumerate(marks)}


def _demote(body: str) -> str:
    """Headings outside code fences drop two levels (`## X` -> `#### X`)."""
    out, fence = [], False
    for line in body.split("\n"):
        if line.startswith("```"):
            fence = not fence
        elif not fence and re.match(r"^#{2,4} ", line):
            line = "##" + line
        out.append(line)
    return "\n".join(out)


def build(patterns: Path = PATTERNS) -> str:
    """The compiled catalog text, after every source check."""
    cats, listed = _read_index(patterns / "INDEX.md")
    frame = _frame(patterns / "_frame.md")
    files = sorted(p.name for p in patterns.glob("opt*.md"))
    names = [f for _, _, f, _, _, _ in listed]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise BuildError(f"INDEX.md lists {', '.join(dup)} more than once")
    if sorted(names) != files:
        raise BuildError("INDEX.md and the pattern files disagree: not listed "
                         f"{sorted(set(files) - set(names))}, no such file {sorted(set(names) - set(files))}")
    ids = [i for i, *_ in listed]
    if len(set(ids)) != len(ids):
        raise BuildError("INDEX.md lists an OPT id twice")
    for key in ("preamble", "after-contents"):
        if key not in frame:
            raise BuildError(f"_frame.md has no `<!-- frame: {key} -->` section")
    cat_blocks = {int(k.split()[1]): v for k, v in frame.items() if k.startswith("category ")}

    out = [BANNER, frame["preamble"]]
    for n, name, paren, blurb in cats:
        out.append(f"- **{name}**{' ' + paren if paren else ''}: {blurb}\n")
    out.append(frame["after-contents"])
    seen_cats: list[int] = []
    for k, (oid, title, fname, impact, klass, cat) in enumerate(listed):
        meta, fid, ftitle, body = scan.parse_pattern_file(patterns / fname)
        where = f"{fname}"
        if not fname.startswith(oid.lower() + "-") or meta.get("pattern") != oid or fid != oid:
            raise BuildError(f"{where}: file name, frontmatter `pattern` and title id must all be {oid}")
        missing = [r for r in REQUIRED if r not in meta]
        if missing:
            raise BuildError(f"{where}: frontmatter is missing {missing}")
        if meta["category"] != cat:
            raise BuildError(f"{where}: category {meta['category']} but INDEX.md lists it under Category {cat}")
        if (ftitle, str(meta["impact"]), str(meta["class"])) != (title, impact, klass):
            raise BuildError(f"{where}: INDEX.md line says {title!r} · {impact} · {klass}; the file says "
                             f"{ftitle!r} · {meta['impact']} · {meta['class']}")
        if meta.get("status") not in (None, "cut", "removed"):
            raise BuildError(f"{where}: status must be cut or removed")
        if seen_cats and seen_cats[-1] != cat:
            if cat in seen_cats:
                raise BuildError(f"{where}: Category {cat} is split in INDEX.md; keep a category's lines together")
        if not seen_cats or seen_cats[-1] != cat:
            seen_cats.append(cat)
            if cat in cat_blocks:
                out.append(cat_blocks[cat])
        aliases = meta.get("aliases") or []
        if aliases:
            if not meta.get("alias_note") or any(not _ALIAS.match(str(a)) for a in aliases):
                raise BuildError(f"{where}: aliases need an alias_note and must be anchor slugs")
            out.append(f"<!-- {meta['alias_note']} -->\n"
                       + "".join(f'<a id="{a}"></a>\n' for a in aliases) + "\n")
        md = []
        for key, val in meta.items():
            if key in scan.PATTERN_FILE_KEYS:
                continue
            val = str(val)
            md.append(f'{key}: "{val}"' if key in _QUOTED else f"{key}: {val}")
        if not body.endswith("\n"):
            raise BuildError(f"{where}: the file must end with a newline")
        body = body[:-1]
        if not body.startswith("\n\n"):
            raise BuildError(f"{where}: leave one blank line after the title")
        out.append(f"### {oid} — {ftitle}\n\n<!-- METADATA\n" + "\n".join(md) + "\n-->"
                   + _demote(body) + ("\n\n---\n\n" if k + 1 < len(listed) else "\n\n---\n"))
    unknown = sorted(set(cat_blocks) - set(seen_cats))
    if unknown:
        raise BuildError(f"_frame.md has category block(s) {unknown} with no pattern")
    text = "".join(out)
    _verify(text, patterns)
    return text


def _verify(text: str, patterns: Path) -> None:
    """The compiled text parses to the same entries as the directory, and each
    heading's GFM anchor is the entry's anchor."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "optimization-patterns.md"
        f.write_text(text, encoding="utf-8")
        compiled = scan.load_catalog(f)
    direct = scan.load_catalog(patterns)
    if compiled != direct:
        bad = [a.pattern for a, b in zip(compiled, direct) if a != b] or ["(count differs)"]
        raise BuildError(f"compiled catalog parses differently from patterns/: {bad}")
    for m, e in zip(re.finditer(r"^### (OPT\d+) — (.+?)$", text, re.M), direct):
        if scan._slug_anchor(m.group(1), m.group(2).strip()) != e.anchor:
            raise BuildError(f"{e.pattern}: compiled heading anchor differs from the entry's anchor")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed compiled file differs from the build")
    args = ap.parse_args(argv)
    try:
        text = build()
    except (BuildError, ValueError) as exc:
        print(f"build_catalog: {exc}", file=sys.stderr)
        return 2
    current = COMPILED.read_text(encoding="utf-8") if COMPILED.is_file() else ""
    if args.check:
        if current == text:
            print(f"build_catalog: {COMPILED.relative_to(_REPO)} is current")
            return 0
        diff = difflib.unified_diff(current.splitlines(), text.splitlines(), "committed", "built",
                                    lineterm="", n=1)
        print("\n".join(list(diff)[:60]))
        print(f"build_catalog: {COMPILED.relative_to(_REPO)} is stale or hand-edited; run "
              "python3 maintainers/ci-speedup/scripts/build_catalog.py and commit it", file=sys.stderr)
        return 1
    COMPILED.write_text(text, encoding="utf-8")
    print(f"build_catalog: wrote {COMPILED.relative_to(_REPO)} ({len(text.encode()):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

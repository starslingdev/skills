"""Every catalog deep link a committed findings file carries must still land.

Findings files (`findings.json` and its siblings) stamp `fix_recipe_anchor` and
`#opt…` URLs into the pattern catalog. Those files are published (`examples/`) or
pinned as fixtures, and the website links the same anchors, so renaming a catalog
heading silently strands them at the top of the page. This guard collects every
anchor those files reference and asserts each one resolves in the catalog: either
as a heading-derived anchor (GitHub's GFM rule) or as an explicit `<a id>` alias
kept above a renamed heading.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_CATALOGS = {
    # anchor prefix -> the catalog that owns it
    "opt": _REPO / "skills" / "ci-speedup" / "references" / "optimization-patterns.md",
    "p": _REPO / "skills" / "ci-secure" / "references" / "security-patterns.md",
}
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_EXPLICIT = re.compile(r"""<a\s+(?:id|name)=["']([^"']+)["']""")
_URL_ANCHOR = re.compile(r"#((?:opt|p)\d[\w-]*)")


def _gfm_slug(heading: str) -> str:
    raw = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", raw)


def _anchors(catalog: Path) -> set[str]:
    text = catalog.read_text(encoding="utf-8")
    return ({_gfm_slug(m.group(1)) for m in _HEADING.finditer(text)}
            | set(_EXPLICIT.findall(text)))


def _findings_files() -> list[Path]:
    roots = [_REPO / "examples", *(_REPO / "skills").glob("*/tests/fixtures/corpora")]
    out: list[Path] = []
    for root in roots:
        out += [p for p in root.rglob("*.json") if "findings" in p.name]
    return sorted(out)


def _walk(node, found: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "fix_recipe_anchor" and isinstance(v, str) and v:
                found.add(v.lstrip("#"))
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)
    elif isinstance(node, str):
        found.update(_URL_ANCHOR.findall(node))


def _referenced() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for path in _findings_files():
        found: set[str] = set()
        _walk(json.loads(path.read_text(encoding="utf-8")), found)
        for anchor in found:
            refs.setdefault(anchor, set()).add(str(path.relative_to(_REPO)))
    return refs


def test_the_guard_sees_committed_findings_files():
    files = _findings_files()
    assert files, "no committed findings files found — the guard would pass vacuously"
    assert any(a.startswith("opt") for a in _referenced()), "no #opt anchors collected"


def test_every_committed_catalog_anchor_resolves():
    resolved = {prefix: _anchors(path) for prefix, path in _CATALOGS.items()}
    dead = []
    for anchor, files in sorted(_referenced().items()):
        prefix = "opt" if anchor.startswith("opt") else "p"
        if anchor not in resolved[prefix]:
            dead.append(f"#{anchor}  <- {sorted(files)}")
    assert not dead, (
        "catalog anchors referenced by committed findings files no longer resolve; "
        "add an explicit <a id=\"…\"></a> alias above the renamed heading:\n  "
        + "\n  ".join(dead))

"""Every repo-relative link in README.md points at a file that exists.

The README is the front door: it names each skill and links into that skill's
own files, which is where a reader goes to judge whether the thing is worth
installing. Those links rot silently — a reference gets renamed inside a skill
and nothing here fails, because no test reads the README. A 404 on the front
page costs more than a broken link anywhere else in the tree.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_LINK = re.compile(r"\]\((?!https?:|#|mailto:)([^)\s]+)\)")


def _links() -> list[str]:
    return _LINK.findall((_REPO / "README.md").read_text(encoding="utf-8"))


def test_there_are_links_to_check():
    """A regex that silently matched nothing would make the check below vacuous."""
    links = _links()
    assert len(links) > 20, f"only {len(links)} repo-relative links found in README.md"


def test_every_repo_relative_readme_link_resolves():
    missing = [link for link in _links()
               if not (_REPO / link.split("#", 1)[0]).exists()]
    assert not missing, (
        "README.md links to path(s) that do not exist: " + ", ".join(sorted(set(missing)))
    )


def test_each_shipped_skill_is_linked_from_the_readme():
    """A skill nobody can find from the front door may as well not ship."""
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    for skill in sorted(p.name for p in (_REPO / "skills").iterdir() if p.is_dir()):
        assert f"skills/{skill}/SKILL.md" in readme, (
            f"{skill} ships but the README never links its SKILL.md")


def test_every_in_page_anchor_matches_a_real_heading():
    """The README's table of contents is entirely `](#…)` links, and the existence
    check above skips them by construction — it only resolves paths on disk.

    An anchor that matches no heading is a click that silently does nothing, and the
    table is the first thing a reader uses. Renaming a section is the ordinary way to
    break one, and nothing would have failed.
    """
    text = (_REPO / "README.md").read_text(encoding="utf-8")
    anchors = set(re.findall(r"\]\(#([^)\s]+)\)", text))
    assert anchors, "no in-page anchors found — the table of contents is gone?"

    # GitHub's slug: lowercase, drop anything but word chars/spaces/hyphens, spaces to
    # hyphens. Enough for this file's plain headings.
    def slug(heading: str) -> str:
        s = heading.strip().lower()
        s = re.sub(r"[^\w\s-]", "", s)
        return re.sub(r"\s+", "-", s)

    headings = {slug(m) for m in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.M)}
    missing = sorted(a for a in anchors if a not in headings)
    assert not missing, (
        "README anchor(s) point at no heading: " + ", ".join(missing))

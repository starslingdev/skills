"""Local install inventory and release label. Never open an unexpected path."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath


MANIFEST_NAME = "install-manifest.json"
MANIFEST_FILES = "files"
_COPY_EXCLUSIONS = frozenset({".git", "__pycache__", "__pypackages__"})
# Folder metadata Finder and Explorer write into any directory a user browses.
# They are never skill files, so they are no sign of an older version.
_OS_METADATA = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_UNRELEASED = "+unreleased"
# The inventory covers only where a stale file can be run or read as guidance:
# the skill's top-level files plus these directories. tests/ (and its fixtures)
# is left out on purpose; a stale test file is inert in an install.
_INVENTORY_DIRS = frozenset({"scripts", "references", "evals"})
_FRAGMENT = re.compile(r"[a-z0-9_-]+\.(?:added|changed|fixed|removed)\.md")
_VERSION = r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|v?[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?)"
_HEADING = re.compile(rf"^## +(?:\[({_VERSION})\]|({_VERSION}))(?=\s|$)", re.M)


def inventory_path(path: PurePosixPath) -> bool:
    """Whether the path belongs to the durable installer inventory."""
    if _COPY_EXCLUSIONS.intersection(path.parts) or path.name in _OS_METADATA:
        return False
    return len(path.parts) == 1 or path.parts[0] in _INVENTORY_DIRS


def _read_metadata(path: Path) -> str | None:
    try:
        if not path.is_symlink() and path.is_file():
            return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        pass
    return None


def has_extra_files(root: Path) -> bool:
    """Compare installed path names only, without following directory links."""
    try:
        expected = json.loads(_read_metadata(root / MANIFEST_NAME) or "{}").get(
            MANIFEST_FILES)
    except (AttributeError, ValueError):
        return False
    # An unreadable or malformed inventory says nothing about extra files: stay quiet.
    if not isinstance(expected, list) or not expected or not all(
            isinstance(name, str) and name for name in expected):
        return False
    known = set(expected)
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in _COPY_EXCLUSIONS]
        current = Path(directory)
        links = [name for name in dirs if (current / name).is_symlink()]
        for name in files + links:
            path = PurePosixPath((current / name).relative_to(root).as_posix())
            if inventory_path(path) and path.as_posix() not in known:
                return True
    return False


def latest_release(root: Path) -> str | None:
    """The newest dated or versioned release heading, ignoring Unreleased.

    An install taken between releases carries changes newer than that heading
    (bullets under ``## [Unreleased]`` or pending ``changelog.d`` fragments), so
    the label then ends in ``+unreleased`` instead of naming the release alone.
    """
    text = _read_metadata(root / "CHANGELOG.md") or ""
    heading = _HEADING.search(text)
    if not heading:
        return None
    label = heading[1] or heading[2]
    pending = re.search(r"^\s*[-*] ", text[:heading.start()], re.M) is not None
    if not pending:
        try:
            pending = any(_FRAGMENT.fullmatch(entry.name) and entry.is_file()
                          and not entry.is_symlink()
                          for entry in (root / "changelog.d").iterdir())
        except OSError:
            pending = False
    return label + _UNRELEASED if pending else label


def release_suffix(label: object) -> str:
    """Only render a release identifier, never arbitrary artifact text."""
    if not isinstance(label, str):
        return ""
    base, unreleased = (label[:-len(_UNRELEASED)], True) if label.endswith(
        _UNRELEASED) else (label, False)
    if re.fullmatch(_VERSION, base):
        return f" · latest release `{base}`" + (" plus unreleased changes" if unreleased else "")
    return ""

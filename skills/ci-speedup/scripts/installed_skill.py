"""Local install inventory and release label. Never open an unexpected path."""

from __future__ import annotations

import json
import os
import re
from hashlib import sha256
from pathlib import Path, PurePosixPath


MANIFEST_NAME = "install-manifest.json"
MANIFEST_FINGERPRINTS = "path_sha256_ap"
_COPY_EXCLUSIONS = frozenset({".git", "__pycache__", "__pypackages__"})
_FRAGMENT = re.compile(r"[a-z0-9_-]+\.(?:added|changed|fixed|removed)\.md")
_VERSION = r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|v?[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?)"
_HEADING = re.compile(rf"^## +(?:\[({_VERSION})\]|({_VERSION}))(?=\s|$)", re.M)
_PATH_SHA256 = re.compile(r"[a-p]{64}")
_BASE16_ALPHABET = "abcdefghijklmnop"


def inventory_path(path: PurePosixPath) -> bool:
    """Whether the path belongs to the durable installer inventory."""
    if _COPY_EXCLUSIONS.intersection(path.parts):
        return False
    # Assembly moves these entries into CHANGELOG.md. Their presence is not
    # evidence about leftover executable or reference files.
    return not (len(path.parts) == 2 and path.parts[0] == "changelog.d"
                and _FRAGMENT.fullmatch(path.name))


def inventory_fingerprint(path: PurePosixPath) -> str:
    """Fingerprint the POSIX relative path alone, without opening the file."""
    digest = sha256(path.as_posix().encode("utf-8", errors="surrogatepass")).digest()
    return "".join(_BASE16_ALPHABET[byte >> 4] + _BASE16_ALPHABET[byte & 15] for byte in digest)


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
            MANIFEST_FINGERPRINTS)
    except (AttributeError, ValueError):
        return False
    if not isinstance(expected, list) or not expected or not all(
            isinstance(digest, str) and _PATH_SHA256.fullmatch(digest) for digest in expected):
        return False
    known = set(expected)
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in _COPY_EXCLUSIONS]
        current = Path(directory)
        links = [name for name in dirs if (current / name).is_symlink()]
        for name in files + links:
            path = PurePosixPath((current / name).relative_to(root).as_posix())
            if inventory_path(path) and inventory_fingerprint(path) not in known:
                return True
    return False


def latest_release(root: Path) -> str | None:
    """The newest dated or versioned release heading, ignoring Unreleased."""
    heading = _HEADING.search(_read_metadata(root / "CHANGELOG.md") or "")
    return (heading[1] or heading[2]) if heading else None


def release_suffix(label: object) -> str:
    """Only render a release identifier, never arbitrary artifact text."""
    if isinstance(label, str) and re.fullmatch(_VERSION, label):
        return f" · latest release `{label}`"
    return ""

#!/usr/bin/env python3
"""Build the path-fingerprint inventory for installed copies.

The inventory records path identities without publishing a list of fixture names
or hashing file contents. Each digest is base16 with nibble values 0–15 encoded
by the letters a–p, most significant nibble first. Use --check to detect drift
without writing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

_ROOT = Path(__file__).resolve().parents[3]
_SKILL = _ROOT / "skills" / "ci-speedup"
sys.path.insert(0, str(_SKILL / "scripts"))
from installed_skill import (  # noqa: E402
    MANIFEST_FINGERPRINTS, MANIFEST_NAME, inventory_fingerprint, inventory_path,
)


def _generated_inventory() -> str:
    output = subprocess.check_output([
        "git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
        "--", _SKILL.relative_to(_ROOT).as_posix(),
    ], cwd=_ROOT, text=True, encoding="utf-8")
    names = {MANIFEST_NAME}  # include the generated file even on its first build
    for name in output.split("\0"):
        if not name:
            continue
        path = _ROOT / name
        relative = PurePosixPath(path.relative_to(_SKILL).as_posix())
        if inventory_path(relative) and (path.is_symlink() or path.is_file()):
            names.add(relative.as_posix())
    fingerprints = sorted(inventory_fingerprint(PurePosixPath(name)) for name in names)
    return json.dumps({MANIFEST_FINGERPRINTS: fingerprints}, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when the inventory is stale.")
    args = parser.parse_args()
    manifest = _SKILL / MANIFEST_NAME
    expected = _generated_inventory()
    if args.check:
        if not manifest.is_file() or manifest.read_text(encoding="utf-8") != expected:
            print("ci-speedup install inventory is stale; run "
                  "python3 maintainers/ci-speedup/scripts/build_install_manifest.py",
                  file=sys.stderr)
            return 1
    else:
        manifest.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

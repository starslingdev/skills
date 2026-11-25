#!/usr/bin/env python3
"""Build the file inventory shipped with ci-speedup (install-manifest.json).

The inventory is a sorted JSON list of the skill's repo-relative POSIX paths:
tracked and untracked-but-not-ignored files under skills/ci-speedup/, minus what
the skills CLI never copies (.git, __pycache__, __pypackages__) and OS folder
metadata. It is scoped to where a stale file can be run or read as guidance:
the skill's top-level files (SKILL.md, ARCHITECTURE.md, CHANGELOG.md, ...) and
scripts/, references/ and evals/. tests/ and its fixtures are left out of the
inventory and of run.py's comparison by design: a stale test file is inert in
an install, while #7's risk is stale code and docs (scripts/billing.py,
references/runner-rates.json). run.py compares an installed copy against the
list to spot files left behind by an older version. Use --check to detect drift
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
    MANIFEST_FILES, MANIFEST_NAME, inventory_path,
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
    return json.dumps({MANIFEST_FILES: sorted(names)}, indent=2) + "\n"


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

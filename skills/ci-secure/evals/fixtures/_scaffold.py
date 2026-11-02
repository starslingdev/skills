"""Shared body of every ci-secure fixture's `scaffold.py`. Maintainer harness only.

    <slug>/scaffold.py <empty sandbox dir> [aux dir]

Un-cloaks `<slug>/dot-github/workflows/*.yml.fixture` into
`<sandbox>/.github/workflows/*.yml`, then makes the sandbox its own git
repository with one commit (ci-secure resolves the tree to scan with
`git rev-parse --show-toplevel`). Prints no KEY=VALUE lines: the session needs
no extra environment.

The fixtures are deliberately vulnerable workflow YAML, so they are tracked
cloaked (a registry scanner reads a tracked `.github/workflows/` path as this
repository's own automation). Writing and committing them is safe only in an
empty directory, so anything else is refused before the first write,
including a directory that cannot be listed: unknown is not empty.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main(fixture_dir: Path, argv: list[str]) -> None:
    if not argv:
        sys.exit("usage: scaffold.py <empty sandbox dir> [aux dir]")
    sandbox = Path(argv[0])
    try:
        contents = os.listdir(sandbox)
    except OSError as exc:
        sys.exit(f"scaffold: refusing: cannot list {sandbox} to confirm it is empty ({exc})")
    if contents:
        sys.exit(f"scaffold: refusing: {sandbox} is not empty; this writes and commits "
                 "intentionally vulnerable workflows, which is only safe in an empty sandbox")
    fixtures = sorted((fixture_dir / "dot-github" / "workflows").glob("*.yml.fixture"))
    if not fixtures:
        sys.exit(f"scaffold: no cloaked *.yml.fixture files under {fixture_dir}")
    dest = sandbox / ".github" / "workflows"
    dest.mkdir(parents=True)
    for f in fixtures:
        (dest / f.name[: -len(".fixture")]).write_bytes(f.read_bytes())
    # No inherited GIT_DIR / GIT_WORK_TREE: either would send the commit below
    # at a real repository. No hooks and no signing from the user's global config.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    for args in (["init", "-q"], ["add", ".github/workflows"],
                 ["-c", "user.email=evals@ci-secure.invalid", "-c", "user.name=ci-secure evals",
                  "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}",
                  "commit", "-q", "-m", "workflows under audit"]):
        done = subprocess.run(["git", "-C", str(sandbox), *args], env=env,
                              capture_output=True, text=True)
        if done.returncode != 0:  # git's own reason, not a bare traceback
            sys.exit(f"scaffold: git {args[-1] if args[0] == '-c' else args[0]} failed "
                     f"(exit {done.returncode}): {done.stderr.strip()}")
    print(f"scaffold: materialized {len(fixtures)} workflow(s) from {fixture_dir.name}",
          file=sys.stderr)

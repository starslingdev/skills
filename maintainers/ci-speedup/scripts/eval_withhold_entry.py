#!/usr/bin/env python3
"""Run ci-speedup evals with one catalog entry cut from the session's skill copy.

    python3 maintainers/ci-speedup/scripts/eval_withhold_entry.py OPT81 --id 11 --model M

The discrimination proof for a reading-sensitive eval: the shared runner
(`maintainers/scripts/run_skill_evals.py`) mounts a copy of the skill; this
wrapper deletes the `### <id> — ...` section from that copy's
`references/optimization-patterns.md` (up to the next `##`/`###` heading)
and, for the split catalog, deletes `references/patterns/<id>-*.md` and its
`INDEX.md` line, before the session starts, and exits non-zero if no mounted
copy was cut. It
also stops the session fetching the published copy instead:

  - every HTTP(S) client that honours the proxy variables (`curl`, `git`,
    Python, the CLI's own requests) goes through a dead proxy for every host
    except the model API (`anthropic.com`, `claude.ai`), GitHub's API
    included;
  - `gh` is always wrapped: it refuses any call whose arguments or `GH_REPO`
    name the publishing repository's owner (`starslingdev`, any case), and
    reaches GitHub directly (without the proxy) for everything else, so the
    skill's `gh` gate still works. With a replay fixture the wrapper sits
    behind the eval's `gh` shim; without one it is first on `PATH`.

A client that ignores the proxy variables, or a `gh` binary called by absolute
path, is not stopped. The repository is never touched. A case that still
passes without the entry was not measuring the lookup. Remaining arguments go
to the runner unchanged (`--skill ci-speedup` is implied).
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

_RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "run_skill_evals.py"
_CATALOG = Path("references") / "optimization-patterns.md"
_PATTERNS = Path("references") / "patterns"
_SKILL = "ci-speedup"
_DEAD = "http://127.0.0.1:9"
_SPARED = "anthropic.com,.anthropic.com,claude.ai,.claude.ai"
_NO_WEB = {"HTTPS_PROXY": _DEAD, "HTTP_PROXY": _DEAD, "https_proxy": _DEAD, "http_proxy": _DEAD,
           "NO_PROXY": _SPARED, "no_proxy": _SPARED}


def withhold(text: str, pattern_id: str) -> str:
    """`text` without the catalog section headed `### <pattern_id> — `."""
    m = re.search(rf"(?m)^### {re.escape(pattern_id)} — .*$", text)
    if not m:
        raise SystemExit(f"withhold: no `### {pattern_id} —` heading in the catalog")
    end = re.compile(r"(?m)^#{2,3} ").search(text, m.end())
    return text[:m.start()] + (text[end.start():] if end else "")


def withhold_split(patterns: Path, pattern_id: str) -> None:
    """Delete `pattern_id`'s file and INDEX.md line from a split catalog
    (`references/patterns/`); nothing when the copy has no split catalog."""
    if not patterns.is_dir():
        return
    files = list(patterns.glob(f"{pattern_id.lower()}-*.md"))
    if not files:
        raise SystemExit(f"withhold: no {pattern_id.lower()}-*.md in {patterns}")
    for f in files:
        f.unlink()
    index = patterns / "INDEX.md"
    if index.is_file():
        keep = [l for l in index.read_text(encoding="utf-8").splitlines(keepends=True)
                if not l.startswith(f"- [{pattern_id} — ")]
        index.write_text("".join(keep), encoding="utf-8")


def _guard_gh(real: str | None) -> Path:
    """A `gh` that refuses the publishing repository and runs `real` without
    the dead proxy for everything else."""
    guard = Path(tempfile.mkdtemp(prefix="withhold-gh-")) / "gh"
    run = f'exec "{real}" "$@"' if real else "echo 'gh: command not found' >&2; exit 127"
    guard.write_text(
        "#!/bin/sh\n"
        "case \"$(printf '%s ' \"$@\" \"${GH_REPO:-}\" | tr '[:upper:]' '[:lower:]')\" in\n"
        "  *starslingdev*) echo 'HTTP 404: Not Found' >&2; exit 1;;\n"
        "esac\n"
        "unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy\n"
        f"{run}\n")
    guard.chmod(0o755)
    return guard


class _Shutil:
    """The runner's `shutil` with `copytree` replaced (the real module is untouched)."""

    def __init__(self, base, copytree):
        self._base, self.copytree = base, copytree

    def __getattr__(self, name):
        return getattr(self._base, name)


def install(runner, pattern_id: str) -> list[str]:
    """Patch `runner` so every mount of the skill loses `pattern_id` and every
    session is cut off from the published copy. Returns the list the cut
    catalog paths are appended to."""
    cut: list[str] = []
    base = runner.shutil

    def copy_then_cut(src, dst, *a, **k):
        out = base.copytree(src, dst, *a, **k)
        if Path(out).name != _SKILL:
            return out
        cat = Path(out) / _CATALOG
        if not cat.is_file():
            raise SystemExit(f"withhold: the mounted copy has no {_CATALOG}: {out}")
        cat.write_text(withhold(cat.read_text(encoding="utf-8"), pattern_id), encoding="utf-8")
        withhold_split(Path(out) / _PATTERNS, pattern_id)
        cut.append(str(cat))
        print(f"    withheld {pattern_id} from the mounted catalog: {cat}")
        return out
    runner.shutil = _Shutil(base, copy_then_cut)
    # The report links the published catalog on GitHub, and a session denied the
    # local entry fetches it from there: observed with `curl` of the raw file
    # and with `gh api .../contents/...`.
    env = runner._env
    guard = _guard_gh(shutil.which("gh", path=env().get("PATH")))

    def env_without_the_published_copy(extra=None):
        extra = dict(extra or {})
        if extra.get("EVAL_GH_REAL"):  # the replay shim delegates here
            extra["EVAL_GH_REAL"] = str(guard)
        else:
            extra["PATH"] = f"{guard.parent}{os.pathsep}{extra.get('PATH') or env().get('PATH', '')}"
        return env({**_NO_WEB, **extra})
    runner._env = env_without_the_published_copy
    return cut


def main(argv: list[str]) -> int:
    pattern_id, rest = argv[0], argv[1:]
    spec = importlib.util.spec_from_file_location("run_skill_evals", _RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    cut = install(runner, pattern_id)
    rc = runner.main(["--skill", _SKILL, *rest])
    if not cut:
        print(f"withhold: no mounted copy of {_SKILL} was cut; nothing was measured", file=sys.stderr)
        return 2
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

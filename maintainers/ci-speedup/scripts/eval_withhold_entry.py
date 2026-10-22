#!/usr/bin/env python3
"""Run ci-speedup evals with one catalog entry cut from the session's skill copy.

    python3 maintainers/ci-speedup/scripts/eval_withhold_entry.py OPT81 --id 11 --model M

The discrimination proof for a reading-sensitive eval: the shared runner
(`maintainers/scripts/run_skill_evals.py`) mounts a copy of the skill; this
wrapper deletes the `### <id> — ...` section from that copy's
`references/optimization-patterns.md` (up to the next `##`/`###` heading)
before the session starts, and stops the session fetching the published copy
instead: web traffic goes to a dead proxy (the model API and the GitHub API
stay reachable) and `gh` refuses calls naming the publishing repository. The
repository is never touched. A case that still
passes without the entry was not measuring the lookup. Remaining arguments go
to the runner unchanged (`--skill ci-speedup` is implied).
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
from pathlib import Path

_RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "run_skill_evals.py"
_CATALOG = Path("references") / "optimization-patterns.md"


def withhold(text: str, pattern_id: str) -> str:
    """`text` without the catalog section headed `### <pattern_id> — `."""
    m = re.search(rf"(?m)^### {re.escape(pattern_id)} — .*$", text)
    if not m:
        raise SystemExit(f"withhold: no `### {pattern_id} —` heading in the catalog")
    end = re.compile(r"(?m)^#{2,3} ").search(text, m.end())
    return text[:m.start()] + (text[end.start():] if end else "")


def main(argv: list[str]) -> int:
    pattern_id, rest = argv[0], argv[1:]
    spec = importlib.util.spec_from_file_location("run_skill_evals", _RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    copytree = runner.shutil.copytree

    def copy_then_cut(src, dst, *a, **k):
        out = copytree(src, dst, *a, **k)
        cat = Path(out) / _CATALOG
        if cat.is_file():
            cat.write_text(withhold(cat.read_text(encoding="utf-8"), pattern_id), encoding="utf-8")
            print(f"    withheld {pattern_id} from the mounted catalog: {cat}")
        return out
    runner.shutil.copytree = copy_then_cut
    # The report links the published catalog on GitHub, and a session denied the
    # local entry will fetch it from there (observed: `curl` of the raw file).
    # Observed both ways: `curl` of the raw file, and `gh api .../contents/...`.
    # So web traffic goes to a dead proxy (sparing the model API and the GitHub
    # API the skill's gh gate needs), and the real `gh` behind the eval shim is
    # wrapped to refuse any call that names the publishing repository.
    env = runner._env
    guard = Path(tempfile.mkdtemp(prefix="withhold-gh-")) / "gh"

    def env_without_the_published_copy(extra=None):
        extra = dict(extra or {})
        if extra.get("EVAL_GH_REAL"):
            guard.write_text("#!/bin/sh\ncase \"$(echo \"$*\" | tr A-Z a-z)\" in *starslingdev*) echo 'HTTP 404: Not Found'"
                             f" >&2; exit 1;; esac\nexec \"{extra['EVAL_GH_REAL']}\" \"$@\"\n")
            guard.chmod(0o755)
            extra["EVAL_GH_REAL"] = str(guard)
        return env({**_NO_WEB, **extra})
    runner._env = env_without_the_published_copy
    return runner.main(["--skill", "ci-speedup", *rest])


_DEAD = "http://127.0.0.1:9"
_SPARED = "anthropic.com,.anthropic.com,claude.ai,.claude.ai,api.github.com"
_NO_WEB = {"HTTPS_PROXY": _DEAD, "HTTP_PROXY": _DEAD, "https_proxy": _DEAD, "http_proxy": _DEAD,
           "NO_PROXY": _SPARED, "no_proxy": _SPARED}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

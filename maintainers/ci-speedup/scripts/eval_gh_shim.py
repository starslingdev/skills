#!/usr/bin/env python3
"""A `gh` that answers the REPLAYED repository from the replay corpus.

Maintainer-only, used by `run_skill_evals.py`, which puts it first on the eval
session's PATH under the name `gh`. Why it exists, measured rather than
assumed: the replay seam (`CI_SPEEDUP_GH_FIXTURES`) covers every GitHub read
the skill's OWN scripts make, but SKILL.md's phase 1 also has the agent run
`gh` directly to resolve and confirm the target (`gh repo view`, and in
practice a `gh api repos/<owner>/<repo>` probe of its own). `synthetic/repo`
exists only in the corpus, so on real GitHub those calls 404, and one of the
first real sessions stopped at phase 1 and asked the user to fix their access.
That is the skill behaving correctly against the world it was shown; the world
was inconsistent (the scripts saw a repository the CLI could not).

So the shim makes the CLI see the same repository the scripts see, and nothing
more:

  - `gh repo view` of the replayed repository (named, or resolved from the
    checkout's `origin`) and `gh api <endpoint>` for an endpoint under
    `repos/<replayed repo>` are served from the corpus. An endpoint the corpus
    does not hold is a 404, exactly as the replay client treats it.
  - EVERYTHING ELSE goes to the real `gh` untouched. In particular
    `gh auth status`, the gate itself, is never answered here: an eval session
    without real GitHub auth still stops at phase 1, as it should.

It edits nothing in the skill. With the shim off the PATH (or outside an eval)
the skill talks to the real `gh` as always.

Environment (set by the harness):
  CI_SPEEDUP_GH_FIXTURES        the replay corpus directory
  CI_SPEEDUP_EVAL_REPLAYED_REPO the `owner/repo` the corpus answers for
  CI_SPEEDUP_EVAL_REAL_GH       absolute path of the real `gh` binary
  CI_SPEEDUP_EVAL_SCRIPTS       the skill's scripts/ dir (for `_fixture_name`)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# `gh api` flags that consume the next argument.
_API_VALUE_FLAGS = {"-q", "--jq", "-H", "--header", "-X", "--method", "-f",
                    "--raw-field", "-F", "--field", "-t", "--template",
                    "--hostname", "--cache", "-p", "--preview", "--input"}
_REPO_VIEW_VALUE_FLAGS = {"--json", "-q", "--jq", "-t", "--template", "-b", "--branch"}


def _real_gh(argv: list[str]) -> int:
    real = os.environ.get("CI_SPEEDUP_EVAL_REAL_GH") or ""
    if not real or not Path(real).is_file():
        print("gh: command not found (eval shim: no real gh to delegate to)",
              file=sys.stderr)
        return 127
    os.execv(real, [real, *argv])
    return 0  # unreachable


def _fixture_name(endpoint: str) -> str:
    scripts = os.environ.get("CI_SPEEDUP_EVAL_SCRIPTS", "")
    if scripts and scripts not in sys.path:
        sys.path.insert(0, scripts)
    import collect_runs  # noqa: E402  (the skill's own mapping, never a copy)
    return collect_runs._fixture_name(endpoint, "json")


def _load(endpoint: str):
    fixtures = Path(os.environ.get("CI_SPEEDUP_GH_FIXTURES", ""))
    path = fixtures / _fixture_name(endpoint)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _jq(doc, expr: str) -> str:
    """`gh`'s `-q`: jq when it is installed, else the plain `.a.b` paths that
    phase-1 probes use. Strings print raw, as `gh -q` prints them."""
    if shutil.which("jq"):
        r = subprocess.run(["jq", "-r", expr], input=json.dumps(doc),
                           capture_output=True, text=True)
        return r.stdout
    cur = doc
    for part in [p for p in expr.strip().split(".") if p]:
        cur = cur.get(part) if isinstance(cur, dict) else None
    if isinstance(cur, str):
        return cur + "\n"
    return json.dumps(cur) + "\n"


def _split(argv: list[str], value_flags: set[str]) -> tuple[list[str], dict[str, str]]:
    positional, flags = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if "=" in a and a.split("=", 1)[0] in value_flags:
            k, v = a.split("=", 1)
            flags[k] = v
        elif a in value_flags and i + 1 < len(argv):
            flags[a] = argv[i + 1]
            i += 1
        elif a.startswith("-"):
            flags[a] = ""
        else:
            positional.append(a)
        i += 1
    return positional, flags


def _origin_repo() -> str | None:
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def _not_found(endpoint: str) -> int:
    """What real `gh` prints for a missing resource. The replay client treats a
    missing fixture as a failed call, and so does this, in gh's own words."""
    print(json.dumps({"message": "Not Found", "status": "404"}))
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    return 1


def _api(argv: list[str], repo: str) -> int | None:
    positional, flags = _split(argv, _API_VALUE_FLAGS)
    if not positional:
        return None
    endpoint = positional[0].lstrip("/")
    if not (endpoint == f"repos/{repo}" or endpoint.startswith(f"repos/{repo}/")):
        return None
    if {"-f", "--raw-field", "-F", "--field", "--input"} & set(flags):
        return None  # a request with a body or fields is not a replayable read
    method = flags.get("-X") or flags.get("--method") or "GET"
    if method.upper() != "GET":
        return None
    doc = _load(endpoint)
    if doc is None:
        return _not_found(endpoint)
    expr = flags.get("-q") or flags.get("--jq")
    sys.stdout.write(_jq(doc, expr) if expr else json.dumps(doc) + "\n")
    return 0


def _repo_view(argv: list[str], repo: str) -> int | None:
    positional, flags = _split(argv, _REPO_VIEW_VALUE_FLAGS)
    if "--web" in flags or "-w" in flags:
        return None
    target = positional[0] if positional else _origin_repo()
    if target != repo:
        return None
    doc = _load(f"repos/{repo}")
    if doc is None:
        return _not_found(f"repos/{repo}")
    owner, name = repo.split("/", 1)
    view = {
        "nameWithOwner": doc.get("full_name", repo),
        "name": name,
        "owner": {"login": owner},
        "defaultBranchRef": {"name": doc.get("default_branch", "main")},
        "isPrivate": bool(doc.get("private")),
        "visibility": str(doc.get("visibility", "public")).upper(),
        "url": f"https://github.com/{repo}",
        "description": doc.get("description") or "",
    }
    fields = flags.get("--json")
    if fields is not None and fields != "":
        out = {f: view.get(f) for f in fields.split(",")}
        expr = flags.get("-q") or flags.get("--jq")
        sys.stdout.write(_jq(out, expr) if expr else json.dumps(out) + "\n")
        return 0
    sys.stdout.write(f"name:\t{view['nameWithOwner']}\n"
                     f"description:\t{view['description']}\n")
    return 0


def main(argv: list[str]) -> int:
    repo = os.environ.get("CI_SPEEDUP_EVAL_REPLAYED_REPO", "")
    if repo and os.environ.get("CI_SPEEDUP_GH_FIXTURES"):
        served = None
        if argv[:1] == ["api"]:
            served = _api(argv[1:], repo)
        elif argv[:2] == ["repo", "view"]:
            served = _repo_view(argv[2:], repo)
        if served is not None:
            return served
    return _real_gh(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

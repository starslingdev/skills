#!/usr/bin/env python3
"""A `gh` for eval sessions that answers the replayed repository from its corpus.

`run_skill_evals.py` puts this first on the session's PATH when a fixture asks
for it. The skill's own scripts already replay through `CI_SPEEDUP_GH_FIXTURES`,
but phase 1 has the agent run `gh auth status` and `gh repo view` itself, and
the replayed repository exists only in the corpus, so real GitHub answers 404
and the session stops to ask about access. This makes the CLI see what the
scripts see, and nothing more:

  - every `gh auth` subcommand, `--version`/`version`/`help`, and read-only
    (GET, no field or input flags) `gh api` calls that do not name the replayed
    repository or its owner go to the real `gh`;
  - a GET `gh api` under `repos/<replayed repo>` and `gh repo view` of it are
    served from the corpus;
  - everything else, including any `gh api` write and `graphql`, fails closed
    with a local 404. The replayed slug is a namespace nobody here owns on
    GitHub, and an eval session never needs to write, so neither reaches GitHub.

Environment (set by the runner): EVAL_GH_SHIM_REPO, CI_SPEEDUP_GH_FIXTURES,
EVAL_GH_REAL (the real gh), EVAL_GH_SCRIPTS (the skill copy's scripts/ dir).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_VALUE_FLAGS = {"-q", "--jq", "-X", "--method", "-H", "--header", "-f", "-F",
                "--raw-field", "--field", "--json", "-t", "--template", "--input"}


def _split(argv):
    pos, flags, i = [], {}, 0
    while i < len(argv):
        a = argv[i]
        if "=" in a and a.split("=", 1)[0] in _VALUE_FLAGS:
            k, v = a.split("=", 1)
            flags.setdefault(k, []).append(v)
        elif a in _VALUE_FLAGS and i + 1 < len(argv):
            flags.setdefault(a, []).append(argv[i + 1])
            i += 1
        elif a.startswith("-"):
            flags.setdefault(a, []).append("")
        else:
            pos.append(a)
        i += 1
    return pos, flags


def _emit(doc, flags) -> int:
    expr = (flags.get("-q") or flags.get("--jq") or [None])[-1]
    if not expr:
        print(json.dumps(doc))
        return 0
    r = subprocess.run(["jq", "-r", expr], input=json.dumps(doc), text=True)
    return r.returncode


def _not_found() -> int:
    print(json.dumps({"message": "Not Found", "status": "404"}))
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    return 1


def _fixture(endpoint: str):
    sys.path.insert(0, os.environ.get("EVAL_GH_SCRIPTS", ""))
    import collect_runs  # the skill's own endpoint -> filename mapping
    root = Path(os.environ.get("CI_SPEEDUP_GH_FIXTURES", ""))
    for ext in ("json", "txt"):
        p = root / collect_runs._fixture_name(endpoint, ext)
        if p.is_file():
            return p
    return None


def main(argv: list[str]) -> int:
    repo = os.environ.get("EVAL_GH_SHIM_REPO", "")
    owner, name = repo.split("/", 1) if "/" in repo else (repo, "")
    if not repo or argv[:1] in (["auth"], ["--version"], ["version"], ["help"]):
        return _real(argv)
    args = [a.replace("{owner}", owner).replace("{repo}", name) for a in argv]
    pos, flags = _split(args[1:])
    if args[:1] == ["api"] and pos:
        endpoint = re.sub(r"^(https://api\.github\.com)?/+", "", pos[0])
        mentions = re.search(rf"(?i)\b{re.escape(owner)}\b", " ".join(args[1:]))
        method = (flags.get("-X") or flags.get("--method") or ["GET"])[-1].upper()
        writes = any(k in flags for k in ("-f", "-F", "--raw-field", "--field", "--input"))
        if method != "GET" or writes:
            return _not_found()
        if not mentions and endpoint != "graphql":
            return _real(argv)
        if not endpoint.lower().startswith(f"repos/{repo}".lower()):
            return _not_found()
        path = _fixture(endpoint)
        if path is None:
            return _not_found()
        if path.suffix == ".txt":
            sys.stdout.write(path.read_text(encoding="utf-8"))
            return 0
        return _emit(json.loads(path.read_text(encoding="utf-8")), flags)
    if args[:2] == ["repo", "view"] and (len(pos) < 2 or pos[1].lower() == repo.lower()):
        path = _fixture(f"repos/{repo}")
        if path is None:
            return _not_found()
        doc = json.loads(path.read_text(encoding="utf-8"))
        view = {"nameWithOwner": repo, "name": name, "owner": {"login": owner},
                "defaultBranchRef": {"name": doc.get("default_branch", "main")},
                "isPrivate": bool(doc.get("private")), "url": f"https://github.com/{repo}",
                "visibility": str(doc.get("visibility", "public")).upper()}
        fields = (flags.get("--json") or [""])[-1]
        if not fields:
            print(f"name:\t{repo}")
            return 0
        return _emit({f: view.get(f) for f in fields.split(",")}, flags)
    return _not_found()


def _real(argv: list[str]) -> int:
    real = os.environ.get("EVAL_GH_REAL", "")
    if not real:
        print("gh: no real gh to delegate to (eval shim)", file=sys.stderr)
        return 127
    os.execv(real, [real, *argv])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

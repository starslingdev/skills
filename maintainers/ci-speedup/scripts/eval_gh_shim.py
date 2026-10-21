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
    checkout's remotes) and a GET `gh api <endpoint>` under
    `repos/<replayed repo>` (`{owner}`/`{repo}` placeholders and full
    `https://api.github.com/` URLs included; `-X GET` fields fold into the
    query string, as gh sends them) are served from the corpus: JSON fixtures
    as JSON, job logs from their `.txt` fixtures raw.
  - ANYTHING ELSE THAT TARGETS THE REPLAYED REPOSITORY FAILS CLOSED with a
    local 404 and never reaches real GitHub: a write or a request body, an
    endpoint the corpus does not hold, an argument naming the slug, a
    `-R/--repo` or `GH_REPO` naming it, and a repo-scoped command (`run`,
    `pr`, `workflow`, ...) run inside a checkout whose remote is it. The
    replayed slug is a namespace we do not own on GitHub; whoever registers
    it must not be able to feed data into an eval session.
  - Everything else goes to the real `gh` untouched: `gh auth status` (the
    gate itself, so an eval without real GitHub auth still stops at phase 1),
    `--version`/`help`, `gh api` of any other endpoint, and commands whose
    `-R/--repo` names a different repository.

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

# Flags that consume a value, mapped to one canonical spelling. Short ones also
# take the value attached (`-XPOST`, `-fname=x`), as gh's flag parser does.
_API_FLAGS = {"-q": "-q", "--jq": "-q", "-H": "-H", "--header": "-H",
              "-X": "-X", "--method": "-X", "-f": "-f", "--raw-field": "-f",
              "-F": "-F", "--field": "-F", "-t": "-t", "--template": "-t",
              "-p": "-p", "--preview": "-p", "--hostname": "--hostname",
              "--cache": "--cache", "--input": "--input"}
_VIEW_FLAGS = {"--json": "--json", "-q": "-q", "--jq": "-q", "-t": "-t",
               "--template": "-t", "-b": "-b", "--branch": "-b"}
_REPO_FLAGS = {"-R": "-R", "--repo": "-R"}
# Commands that act on "the current repository" when no -R/--repo is given.
_REPO_SCOPED = {"run", "workflow", "pr", "co", "issue", "release", "cache",
                "secret", "variable", "label", "repo", "browse", "ruleset",
                "attestation"}
_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PLAIN_PATH = re.compile(r"^\.$|^(\.[A-Za-z_][A-Za-z0-9_]*)+$")


def _real_gh(argv: list[str]) -> int:
    real = os.environ.get("CI_SPEEDUP_EVAL_REAL_GH") or ""
    if not real or not Path(real).is_file():
        print("gh: command not found (eval shim: no real gh to delegate to)",
              file=sys.stderr)
        return 127
    os.execv(real, [real, *argv])
    return 0  # unreachable


def _fixture(endpoint: str) -> Path | None:
    """The corpus file for `endpoint`: the JSON fixture, else the raw `.txt`
    one the recorder writes for job logs. Named by the skill's own mapping."""
    scripts = os.environ.get("CI_SPEEDUP_EVAL_SCRIPTS", "")
    if scripts and scripts not in sys.path:
        sys.path.insert(0, scripts)
    import collect_runs  # noqa: E402  (the skill's own mapping, never a copy)
    fixtures = Path(os.environ.get("CI_SPEEDUP_GH_FIXTURES", ""))
    for ext in ("json", "txt"):
        path = fixtures / collect_runs._fixture_name(endpoint, ext)
        if path.is_file():
            return path
    return None


def _jq(doc, expr: str) -> tuple[int, str, str]:
    """`gh`'s `-q`: jq when it is installed (its exit code and errors kept),
    else only the plain `.a.b` paths phase-1 probes use. Strings print raw."""
    if shutil.which("jq"):
        r = subprocess.run(["jq", "-r", expr], input=json.dumps(doc),
                           capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr
    if not _PLAIN_PATH.match(expr.strip()):
        return 1, "", (f"eval gh shim: jq is not installed; only plain `.a.b` "
                       f"paths are supported, not {expr!r}\n")
    cur = doc
    for part in [p for p in expr.strip().split(".") if p]:
        cur = cur.get(part) if isinstance(cur, dict) else None
    return 0, (cur if isinstance(cur, str) else json.dumps(cur)) + "\n", ""


def _emit(doc, expr: str | None) -> int:
    if not expr:
        sys.stdout.write(json.dumps(doc) + "\n")
        return 0
    rc, out, err = _jq(doc, expr)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


def _split(argv: list[str], value_flags: dict[str, str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Positionals, and (canonical flag, value) pairs in order. A flag not in
    `value_flags` is recorded with an empty value."""
    positional, flags = [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and "=" in a and a.split("=", 1)[0] in value_flags:
            k, v = a.split("=", 1)
            flags.append((value_flags[k], v))
        elif a in value_flags and i + 1 < len(argv):
            flags.append((value_flags[a], argv[i + 1]))
            i += 1
        elif not a.startswith("--") and len(a) > 2 and a[:2] in value_flags:
            flags.append((value_flags[a[:2]], a[2:]))
        elif a.startswith("-"):
            flags.append((a, ""))
        else:
            positional.append(a)
        i += 1
    return positional, flags


def _get(flags, name: str) -> str | None:
    vals = [v for k, v in flags if k == name]
    return vals[-1] if vals else None


def _remote_repos() -> list[str]:
    """`owner/repo` of every GitHub remote of the cwd's checkout, origin first."""
    try:
        out = subprocess.run(["git", "remote", "-v"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    repos = []
    for line in sorted(out.splitlines(), key=lambda l: not l.startswith("origin")):
        m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?\s", line + " ")
        if m and m.group(1) not in repos:
            repos.append(m.group(1))
    return repos


def _same(a: str | None, b: str) -> bool:
    """GitHub slugs compare case-insensitively; accept URL / host forms."""
    if not a:
        return False
    a = re.sub(r"^(https?://)?([^/]+\.[^/]+/)?", "", a.strip()).removesuffix(".git").strip("/")
    return a.lower() == b.lower()


def _not_found(endpoint: str) -> int:
    """What real `gh` prints for a missing resource. The replay client treats a
    missing fixture as a failed call, and so does this, in gh's own words."""
    print(json.dumps({"message": "Not Found", "status": "404"}))
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    return 1


def _expand(arg: str, origin: str | None) -> str:
    """gh's `{owner}`/`{repo}` placeholders, filled from the checkout's remote."""
    if origin and ("{owner}" in arg or "{repo}" in arg):
        owner, name = origin.split("/", 1)
        arg = arg.replace("{owner}", owner).replace("{repo}", name)
    return arg


def _targets(argv: list[str], repo: str, remotes: list[str]) -> bool:
    """Does this command act on the replayed repository?"""
    origin = remotes[0] if remotes else None
    args = [_expand(a, origin) for a in argv] if argv[:1] == ["api"] else argv
    if any(repo.lower() in a.lower() for a in args):
        return True
    if argv[:2] == ["api", "graphql"]:  # owner and name may arrive as separate fields
        owner = re.escape(repo.split("/", 1)[0])
        return any(re.search(rf"(?i)\b{owner}\b", a) for a in args)
    cmd = next((a for a in argv if not a.startswith("-")), None)
    if cmd not in _REPO_SCOPED:
        return False
    positional, flags = _split(argv, _REPO_FLAGS)
    named = _get(flags, "-R") or os.environ.get("GH_REPO")
    if not named and cmd == "repo" and len(positional) > 2 and _SLUG.match(positional[2]):
        named = positional[2]  # `gh repo view|clone|... other/repo`
    if named:
        return _same(named, repo)
    return any(_same(r, repo) for r in remotes)


def _api(argv: list[str], repo: str, origin: str | None) -> int | None:
    positional, flags = _split(argv, _API_FLAGS)
    if not positional:
        return None
    endpoint = _expand(positional[0], origin)
    endpoint = re.sub(r"^https?://api\.github\.com/", "", endpoint).lstrip("/")
    low, prefix = endpoint.lower(), f"repos/{repo}".lower()
    if not (low == prefix or low.startswith(prefix + "/") or low.startswith(prefix + "?")):
        return None  # not ours; `_targets` still fails closed on a near-miss slug
    fields = [(k, _expand(v, origin)) for k, v in flags if k in ("-f", "-F")]
    method = (_get(flags, "-X") or ("POST" if fields else "GET")).upper()
    if (method != "GET" or _get(flags, "--input") is not None
            or any(k == "-F" and v.split("=", 1)[-1].startswith("@") for k, v in fields)):
        return _not_found(endpoint)  # a write: never replayed, never delegated
    if fields:  # `-X GET` sends fields as the query string
        endpoint += ("&" if "?" in endpoint else "?") + "&".join(v for _, v in fields)
    path = _fixture(endpoint)
    if path is None:
        return _not_found(endpoint)
    expr = _get(flags, "-q")
    if path.suffix == ".txt":
        if expr:
            print("gh: cannot apply --jq to a non-JSON response", file=sys.stderr)
            return 1
        sys.stdout.write(path.read_text(encoding="utf-8"))
        return 0
    return _emit(json.loads(path.read_text(encoding="utf-8")), expr)


def _repo_view(argv: list[str], repo: str, origin: str | None) -> int | None:
    positional, flags = _split(argv, _VIEW_FLAGS)
    if ("--web", "") in flags or ("-w", "") in flags:
        return None
    if not _same(positional[0] if positional else origin, repo):
        return None
    path = _fixture(f"repos/{repo}")
    if path is None:
        return _not_found(f"repos/{repo}")
    doc = json.loads(path.read_text(encoding="utf-8"))
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
    fields = _get(flags, "--json")
    if fields:
        unknown = [f for f in fields.split(",") if f not in view]
        if unknown:  # real gh refuses a field it does not know; so does the shim
            print(f'Unknown JSON field: "{unknown[0]}"\nAvailable fields:\n  '
                  + "\n  ".join(sorted(view)), file=sys.stderr)
            return 1
        return _emit({f: view[f] for f in fields.split(",")}, _get(flags, "-q"))
    sys.stdout.write(f"name:\t{view['nameWithOwner']}\n"
                     f"description:\t{view['description']}\n")
    return 0


def main(argv: list[str]) -> int:
    repo = os.environ.get("CI_SPEEDUP_EVAL_REPLAYED_REPO", "")
    if repo and os.environ.get("CI_SPEEDUP_GH_FIXTURES"):
        remotes = _remote_repos()
        origin = remotes[0] if remotes else None
        served = None
        if argv[:1] == ["api"]:
            served = _api(argv[1:], repo, origin)
        elif argv[:2] == ["repo", "view"]:
            served = _repo_view(argv[2:], repo, origin)
        if served is not None:
            return served
        if _targets(argv, repo, remotes):
            return _not_found(f"repos/{repo}")  # fail closed: never real GitHub
    return _real_gh(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

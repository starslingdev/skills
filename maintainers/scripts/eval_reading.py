"""Reading accounting for skill-eval sessions: how much of the skill's
`references/` the agent pulled into its context.

From a session's stream-json events, every `Read` call, every `Grep` call in
`content` mode, and every `Bash` call with a command that prints file content
(`cat`, `sed`, `head`, `tail`, `grep`, `egrep`, `fgrep`, `rg`, `less`, `more`,
`awk`, `nl`, `jq`, `tac`, `cut`, `sort`, `uniq`, `strings`, `bat`, `diff`,
`xargs` feeding one of these, `find -exec`, and `perl` / `python` / `ruby` /
`node` inline scripts) is matched to its tool RESULT; the result's size (UTF-8
bytes and lines) is attributed to the file(s) under `references/` of the skill
under test that the reading command names: `references/<file>` (a glob expands
against the real listing; `$VAR/references/...` counts as the mounted copy), or
a bare file name after the call names the `references` directory itself (`cd
.../references; grep x optimization-patterns.md`, `grep -r x references/`,
where a bare directory stands for every file in it). With several candidates,
each output line goes to the file whose name prefixes it (grep's `file:line:`),
and unprefixed lines split evenly over the files that printed prefixed lines. A
reading command that names no reference file (a search from the skill root or
above) is charged line by line: a line counts when its path prefix is
`<mounted copy>/references/<file>`.

Name-only output reads nothing: `Grep` in its default `files_with_matches` (or
`count`) mode, `grep -l` / `-L` / `-c`, `rg --files`, `ls`, `find` without
`-exec`, `wc`, `stat` and the like. A `Bash` call that names a mounted
reference file in a command the tracker cannot read (`cp`, a script) counts
in `unattributed_calls` instead of being dropped. Output, per session:

    {reference_bytes, reference_lines, catalog_opened, catalog_entries_read,
     unattributed_calls, reference_files: [{path, bytes, lines, calls}]}

`catalog_entries_read` lists the catalog entry ids (`OPT81`, ...) whose
`## <id> — ` or `### <id> — ` heading appears in a result attributed to the
catalog with some of the entry's body under it (a heading listed alone, as an
index grep prints it, is not a read): what an agent actually saw, not what it
asked for. A failed `Read` / `Grep` counts nothing; a `Bash` command that
exits non-zero counts what it printed. For a skill with no `optimization-patterns.md` catalog,
`catalog_opened` is None and the printed figure leaves the catalog out. Tested
offline in `maintainers/tests/test_eval_reading.py`.
"""
from __future__ import annotations

import fnmatch
import json
import posixpath
import re
from pathlib import Path

CATALOG = "optimization-patterns.md"
_READERS = {"cat", "sed", "head", "tail", "grep", "egrep", "fgrep", "rg", "less", "more", "awk",
            "nl", "jq", "tac", "cut", "sort", "uniq", "strings", "bat", "diff"}
_SEARCHERS = {"grep", "egrep", "fgrep", "rg"}
_SCRIPTED = {"perl", "python", "python3", "ruby", "node"}  # readers as inline scripts only
_LISTERS = {"ls", "find", "wc", "stat", "file", "du", "tree", "test", "[", "realpath", "readlink",
            "dirname", "basename", "md5sum", "shasum", "sha256sum", "echo", "touch", "mkdir"}
_LIST_FLAGS = {"--files-with-matches", "--files-without-match", "--count", "--count-matches",
               "--files", "--quiet"}
_PREFIX = ("export", "sudo", "time", "command", "env", "nohup", "exec")
_REF = re.compile(r"(?<![\w.${-])(?:([\w.-]+)/)?references(?:/([^\s'\"`;|&<>(){}]*))?")
_HEADING = re.compile(r"^\s*(?:\S*?[:-])?\d*[:\t-]?\s*#{2,3} (OPT\d+) — ")
_NUMBER = re.compile(r"^\s*(?:\S*?[:-])?\d*[:\t-]?")
_VAR = re.compile(r"(?:^|[\s;&|(])(?:export\s+)?([A-Za-z_]\w*)=(['\"]?)([^\s'\";&|]+)\2")
_CHECK_KEYS = {"type", "name", "field", "min", "max", "pattern"}


def _text(body) -> str:
    if isinstance(body, str):
        return body
    if not isinstance(body, list):
        return ""
    return "\n".join(str(b.get("text", "")) for b in body
                     if isinstance(b, dict) and b.get("type") == "text")


def _content(ev) -> list:
    msg = ev.get("message") if isinstance(ev, dict) else None
    c = msg.get("content") if isinstance(msg, dict) else None
    return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []


def _expand(cmd: str) -> str:
    """`cmd` with `$V` / `${V}` / `"$V"` replaced by `V=value` set in it."""
    vals = {k: v for k, _, v in _VAR.findall(cmd)}
    return re.sub(r"\"?\$\{?([A-Za-z_]\w*)\}?\"?", lambda m: vals.get(m.group(1), m.group(0)), cmd)


def _norm(p: str) -> str:
    p = posixpath.normpath(p) if p else p
    return p[len("/private"):] if p.startswith("/private/") else p


def _elsewhere(text: str, m: re.Match, root: str | None) -> bool:
    """The path `m` sits in is not the mounted copy `root`: a URL or a `gh api`
    path (the published copy), or an absolute (or `~/`, `$HOME/`) path outside
    `root`, or a relative one after a `cd` to such a path (another install of
    the skill). An unexpanded `$VAR/references/...` is the mounted copy."""
    path = re.split(r"[\s'\"`;|&<>()=]", text[:m.start()])[-1] + m.group(0)
    if "://" in path or re.match(r"/?repos/", path):
        return True
    path = re.sub(r"^\$(?:HOME\b|\{HOME\})", "~", path)
    if path.startswith("$"):
        return False
    if not root:
        return False
    if not path.startswith(("/", "~")):
        cds = re.findall(r"(?:^|[\s;&|(])cd\s+['\"]?([^\s'\";&|]+)", text[:m.start()])
        if not cds or not cds[-1].startswith(("/", "~")):
            return False
        path = cds[-1]
    return not (_norm(path) + "/").startswith(_norm(str(root).rstrip("/")) + "/")


def referenced(text: str, skill: str, names: list[str] | None = None,
               root: str | None = None) -> list[str]:
    """`references/` files of `skill` that `text` names, in order, unique. A
    path qualified by another skill's directory (`ci-secure/references/x`) is
    not ours, nor is an absolute one outside the mounted copy `root` (a
    globally installed copy of the same skill); an unqualified one is. Globs
    and bare-directory mentions resolve against `names` (the real listing)."""
    out: list[str] = []
    bare_dir = False
    text = _expand(text)
    for m in _REF.finditer(text):
        owner, rest = m.group(1), (m.group(2) or "").rstrip(".,:")
        if (owner and owner not in (skill, ".", "..")) or _elsewhere(text, m, root):
            continue
        if not rest:  # the directory itself, when it reads as a path (not prose)
            bare_dir |= bool(owner) or m.group(0).endswith("/") or bool(
                re.search(r"cd\s+$", text[:m.start()]))
            continue
        hits = [n for n in names or [] if fnmatch.fnmatch(n, rest)] if any(
            ch in rest for ch in "*?[") else [rest]
        out += [h for h in hits if h not in out]
    if bare_dir and names:
        named = [n for n in names if n not in out and re.search(
            rf"(?<![\w.-]){re.escape(n)}(?![\w-])", text)]
        out += named or ([] if out else list(names))
    return out


def _split_shell(cmd: str) -> list[str]:
    """Simple commands of `cmd`: split on `|`, `||`, `&&`, `;`, `&`, newlines,
    parentheses and backticks outside quotes."""
    segs, cur, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            quote = None if ch == quote else quote
            cur.append(ch)
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in "|;&\n()`":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


def _kind(word: str, args: list[str]) -> str:
    """`reader`, `xargs` (a reader fed file names on stdin), `lister`, `cd` or `other`."""
    if word == "cd":
        return "cd"
    if word in _SEARCHERS and any(a in _LIST_FLAGS or a in ("-l", "-L", "-c", "-q") or re.fullmatch(
            r"-[A-Za-z]*[lLcq][A-Za-z]*", a) for a in args):
        return "lister"
    if word in _READERS:
        return "reader"
    if word in _SCRIPTED:
        return "reader" if any(re.fullmatch(r"-\w*[ce]\w*", a) for a in args) else "other"
    if word == "xargs":
        inner = [a for a in args if not a.startswith("-") and a != "{}"]
        return "xargs" if inner and _kind(inner[0], inner[1:]) == "reader" else "lister"
    if word == "find":
        if any(a in ("-exec", "-execdir") for a in args):
            at = next(i for i, a in enumerate(args) if a in ("-exec", "-execdir"))
            if at + 1 < len(args) and _kind(Path(args[at + 1]).name, args[at + 2:]) == "reader":
                return "reader"
        return "lister"
    if word in _LISTERS:
        return "lister"
    return "other"


def _segments(cmd: str) -> list[tuple[str, str]]:
    out = []
    for seg in _split_shell(cmd):
        toks = seg.split()
        while toks and (re.match(r"^[A-Za-z_]\w*=", toks[0]) or toks[0] in _PREFIX):
            toks = toks[1:]
        if toks:
            out.append((_kind(Path(toks[0].strip("'\"")).name, [t.strip("'\"") for t in toks[1:]]), seg))
    return out


def _bash_paths(command: str, skill: str, names, root) -> tuple[bool, list[str], bool]:
    """(reads, paths, unattributed) for one `Bash` command."""
    segs = _segments(_expand(command))
    reads = any(k in ("reader", "xargs") for k, _ in segs)
    keep = {"cd", "reader", "xargs"} | ({"lister"} if any(k == "xargs" for k, _ in segs) else set())
    paths = referenced("; ".join(s for k, s in segs if k in keep), skill, names, root) if reads else []
    other = referenced("; ".join(s for k, s in segs if k == "other"), skill, names, root)
    return reads, paths, bool(other) and not paths


def _owner_lines(body: str, paths: list[str]) -> dict[str, list]:
    """Per path: [bytes, lines, text lines]. One path takes it all; otherwise
    each line goes to the path that prefixes it (prefix stripped), and the
    rest splits evenly over the paths that printed prefixed lines."""
    if len(paths) == 1:
        return {paths[0]: [len(body.encode("utf-8")), len(body.splitlines()), body.splitlines()]}
    got = {p: [0, 0, []] for p in paths}
    loose, last = [0, 0, []], None
    for line in body.splitlines():
        size = len(line.encode("utf-8")) + 1
        hit = next(((p, m) for p in paths for m in [re.search(
            rf"(?:^|/){re.escape(p)}[:-]", line)] if m), None)
        if hit:
            last = hit[0]
            acc, text = got[last], line[hit[1].end():]
        else:
            acc, text = loose, line
            if last:
                got[last][2].append(line)
        acc[0], acc[1] = acc[0] + size, acc[1] + 1
        acc[2].append(text) if hit or not last else None
    takers = [p for p in paths if got[p][0]] or paths
    for p in takers:
        got[p][0] += loose[0] // len(takers)
        got[p][1] += loose[1] // len(takers)
        if not got[p][2]:
            got[p][2] = loose[2]
    return {p: v for p, v in got.items() if p in takers}


def _prefixed_lines(body: str, names: list[str], root: str | None, cd: str | None) -> dict[str, list]:
    """Per reference file: [bytes, lines, text lines] of the output lines whose
    path prefix is `<root>/references/<file>` (absolute, or relative to `cd`)."""
    if not names:
        return {}
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    pat = re.compile(rf"^(?P<dir>\S*?)(?:^|/)?references/(?P<name>{alt})[:-]")
    got: dict[str, list] = {}
    for line in body.splitlines():
        m = pat.match(line)
        if not m:
            continue
        d = m.group("dir").rstrip("/") or "."
        if not d.startswith("/"):
            if not cd or not cd.startswith("/"):
                continue
            d = posixpath.join(cd, d)
        if root and _norm(d) != _norm(str(root).rstrip("/")):
            continue
        acc = got.setdefault(m.group("name"), [0, 0, []])
        acc[0], acc[1] = acc[0] + len(line.encode("utf-8")) + 1, acc[1] + 1
        acc[2].append(line[m.end():])
    return got


def _entries(lines: list[str]) -> list[str]:
    """Catalog ids whose heading appears in `lines` with some of the entry's
    text under it: a heading listed alone (an index grep) is not a read."""
    out = []
    for i, line in enumerate(lines):
        h = _HEADING.match(line)
        if not h or h.group(1) in out:
            continue
        for nxt in lines[i + 1:]:
            if _HEADING.match(nxt):
                break
            if _NUMBER.sub("", nxt, count=1).strip():
                out.append(h.group(1))
                break
    return out


def session_totals(evs: list, wall_s: float | None = None) -> dict:
    """Whole-session usage. A session whose agent starts background subagents
    emits one `result` event per turn it resumes for, each with only that
    turn's `usage`; `modelUsage` on the last one is cumulative, subagents
    included, so input tokens come from there."""
    results = [e for e in evs if isinstance(e, dict) and e.get("type") == "result"]
    last = results[-1] if results else {}
    models = last.get("modelUsage")
    models = [m for m in models.values() if isinstance(m, dict)] if isinstance(models, dict) else []
    stats = last.get("subagent_stats")

    def n(m: dict, k: str) -> int:
        v = m.get(k)
        return v if isinstance(v, (int, float)) else 0
    return {"wall_s": None if wall_s is None else round(wall_s, 1),
            "input_tokens": sum(n(m, "inputTokens") + n(m, "cacheReadInputTokens")
                                + n(m, "cacheCreationInputTokens") for m in models),
            "output_tokens": sum(n(m, "outputTokens") for m in models),
            "result_events": len(results),
            "subagents": n(stats, "spawned") if isinstance(stats, dict) else 0}


def summarize(evs: list, skill: str, names: list[str] | None = None,
              wall_s: float | None = None, root: str | None = None) -> dict:
    """The reading figures for one session (see the module docstring), plus
    `session`: whole-session tokens and wall time (`session_totals`)."""
    pending: dict[str, tuple[list[str], str | None, bool]] = {}
    unattributed = 0
    for ev in evs:
        for c in _content(ev):
            if c.get("type") != "tool_use":
                continue
            name, inp = c.get("name"), c.get("input")
            inp = inp if isinstance(inp, dict) else {}
            cd = None
            if name == "Read":
                paths = referenced(str(inp.get("file_path", "")), skill, names, root)
                reads = bool(paths)
            elif name == "Grep":
                reads = inp.get("output_mode") == "content"  # other modes list names only
                paths = referenced(f"{inp.get('path', '')} {inp.get('glob', '')}",
                                   skill, names, root) if reads else []
            elif name == "Bash":
                command = str(inp.get("command", ""))
                reads, paths, lost = _bash_paths(command, skill, names, root)
                unattributed += lost
                cds = re.findall(r"(?:^|[\s;&|(])cd\s+['\"]?([^\s'\";&|]+)", _expand(command))
                cd = cds[-1] if cds else None
            else:
                continue
            if reads:
                pending[str(c.get("id"))] = (paths, cd, name == "Bash")
    has_catalog = names is None or any(Path(n).name == CATALOG for n in names)
    files: dict[str, dict] = {}
    entries: list[str] = []
    for ev in evs:
        for c in _content(ev):
            got = pending.pop(str(c.get("tool_use_id")), None) if c.get(
                "type") == "tool_result" else None
            if not got:
                continue
            paths, cd, shell = got
            body = _text(c.get("content"))
            if c.get("is_error"):
                # A failed Read / Grep put nothing of the file in context. A shell
                # command that exits non-zero still returns what it printed (the
                # read may have succeeded before a later part failed): count that,
                # minus the `Exit code N` line, toward files that really exist.
                if not shell:
                    continue
                body = re.sub(r"\AExit code \d+\n?", "", body)
                if names is not None:
                    paths = [p for p in paths if p in names]
            share = _owner_lines(body, paths) if paths else _prefixed_lines(body, names or [], root, cd)
            for p, (size, lines, text) in share.items():
                if not size:
                    continue
                f = files.setdefault(p, {"path": p, "bytes": 0, "lines": 0, "calls": 0})
                f["bytes"], f["lines"], f["calls"] = f["bytes"] + size, f["lines"] + lines, f["calls"] + 1
                if has_catalog and Path(p).name == CATALOG:
                    entries += [i for i in _entries(text) if i not in entries]
    rows = sorted(files.values(), key=lambda f: -f["bytes"])
    return {"reference_bytes": sum(f["bytes"] for f in rows),
            "reference_lines": sum(f["lines"] for f in rows),
            "catalog_opened": any(Path(f["path"]).name == CATALOG for f in rows)
            if has_catalog else None,
            "catalog_entries_read": entries,
            "unattributed_calls": unattributed,
            "reference_files": rows,
            "session": session_totals(evs, wall_s)}


def reference_names(skill_dir: Path) -> list[str]:
    """Every file under `<skill_dir>/references/`, relative to it."""
    root = skill_dir / "references"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def check(c: dict, reading: dict) -> tuple[bool, str]:
    """A `reading` check: `{"type": "reading", "field": F, "min"?, "max"?,
    "pattern"?}`. A list field counts its items matching `pattern` (default:
    all); a bool counts as 0/1; the count must sit in [min (default 1), max].
    An unknown field or key is an error, never a silent pass."""
    unknown = sorted(set(c) - _CHECK_KEYS)
    if unknown:
        raise ValueError(f"reading check {c.get('name')!r} has unknown key(s) {unknown}")
    if c.get("field") not in reading:
        raise ValueError(f"reading check names unknown field {c.get('field')!r}")
    v = reading[c["field"]]
    if isinstance(v, list):
        n = sum(1 for x in v if re.search(c.get("pattern", ""), json.dumps(x)
                                          if isinstance(x, dict) else str(x)))
    else:
        n = int(v or 0)
    lo, hi = c.get("min", 1), c.get("max")
    return n >= lo and (hi is None or n <= hi), f"{c.get('field')}={n}"


def figure(reading: dict) -> str:
    """The compact figure for the PASS / FAIL line."""
    s = reading.get("session") or {}
    opened = reading.get("catalog_opened")
    lost = reading.get("unattributed_calls") or 0
    return (f"refs {reading['reference_bytes'] / 1024:.1f} KB in "
            f"{len(reading['reference_files'])} files"
            + ("" if opened is None else f", catalog {'opened' if opened else 'not opened'}")
            + (f", {lost} unattributed" if lost else "")
            + f"; session {(s.get('input_tokens') or 0) / 1000:.0f}k in, {s.get('wall_s') or 0:.0f}s wall")

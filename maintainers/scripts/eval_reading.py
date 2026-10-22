"""Reading accounting for skill-eval sessions: how much of the skill's
`references/` the agent pulled into its context.

From a session's stream-json events, every `Read` / `Grep` tool call and every
`Bash` call that reads a file (`cat`, `sed`, `head`, `tail`, `grep`, `rg`,
`less`, `awk`) is matched to its tool RESULT; the result's size (UTF-8 bytes and
lines) is attributed to the file(s) under `references/` of the skill under test
that the call names: `references/<file>` (a glob expands against the real
listing), or a bare file name after the call names the `references` directory
itself (`cd .../references; grep x optimization-patterns.md`, `grep -r x
references/`, where a bare directory stands for every file in it). With
several candidates, each output line goes to the file whose name prefixes it
(grep's `file:line:`), and unprefixed lines split evenly. Output, per session:

    {reference_bytes, reference_lines, catalog_opened, catalog_entries_read,
     reference_files: [{path, bytes, lines, calls}]}

`catalog_entries_read` lists the catalog entry ids (`OPT81`, ...) whose
`### <id> — ` heading appears in a result attributed to the catalog: what an
agent actually saw, not what it asked for. Tested offline in
`maintainers/tests/test_eval_reading.py`.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

CATALOG = "optimization-patterns.md"
_READERS = re.compile(r"(?<![\w-])(?:cat|sed|head|tail|grep|egrep|rg|less|awk)(?![\w-])")
_REF = re.compile(r"(?<![\w.-])(?:([\w.-]+)/)?references(?:/([^\s'\"`;|&<>(){}]*))?")
_HEADING = re.compile(r"#{2,3} (OPT\d+) — ")


def _text(body) -> str:
    if isinstance(body, str):
        return body
    return "\n".join(str(b.get("text", "")) for b in body or []
                     if isinstance(b, dict) and b.get("type") == "text")


def _content(ev: dict) -> list:
    c = (ev.get("message") or {}).get("content")
    return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []


def _elsewhere(text: str, m: re.Match, root: str | None) -> bool:
    """The path `m` sits in is absolute (or `~/`) and outside the mounted copy
    `root`: another install of the skill, not the one under test."""
    if not root:
        return False
    path = re.split(r"[\s'\"`;|&<>(){}=]", text[:m.start()])[-1] + m.group(0)
    if not path.startswith(("/", "~")):
        return False

    def norm(p: str) -> str:
        return p[len("/private"):] if p.startswith("/private/") else p
    return not norm(path).startswith(norm(str(root).rstrip("/")) + "/")


def referenced(text: str, skill: str, names: list[str] | None = None,
               root: str | None = None) -> list[str]:
    """`references/` files of `skill` that `text` names, in order, unique. A
    path qualified by another skill's directory (`ci-secure/references/x`) is
    not ours, nor is an absolute one outside the mounted copy `root` (a
    globally installed copy of the same skill); an unqualified one is. Globs
    and bare-directory mentions resolve against `names` (the real listing)."""
    out: list[str] = []
    bare_dir = False
    for m in _REF.finditer(text):
        owner, rest = m.group(1), (m.group(2) or "").rstrip(".,:")
        if (owner and owner != skill) or _elsewhere(text, m, root):
            continue
        if not rest:  # the directory itself, when it reads as a path (not prose)
            bare_dir |= bool(owner) or text[m.end():m.end() + 1] == "/" or bool(
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


def _split(body: str, paths: list[str]) -> dict[str, tuple[int, int]]:
    """(bytes, lines) of `body` per path: one path takes it all; otherwise each
    line goes to the path that prefixes it, and the rest splits evenly."""
    if len(paths) == 1:
        return {paths[0]: (len(body.encode("utf-8")), len(body.splitlines()))}
    got = {p: [0, 0] for p in paths}
    loose = [0, 0]
    for line in body.splitlines():
        size = len(line.encode("utf-8")) + 1
        owner = next((p for p in paths if re.search(
            rf"(?:^|/){re.escape(p)}[:-]", line)), None)
        acc = got[owner] if owner else loose
        acc[0], acc[1] = acc[0] + size, acc[1] + 1
    takers = [p for p in paths if got[p][0]] or paths
    for p in takers:
        got[p][0] += loose[0] // len(takers)
        got[p][1] += loose[1] // len(takers)
    return {p: (b, n) for p, (b, n) in got.items() if p in takers}


def session_totals(evs: list[dict], wall_s: float | None = None) -> dict:
    """Whole-session usage. A session whose agent starts background subagents
    emits one `result` event per turn it resumes for, each with only that
    turn's `usage`; `modelUsage` on the last one is cumulative, subagents
    included, so input tokens come from there."""
    results = [e for e in evs if e.get("type") == "result"]
    models = (results[-1].get("modelUsage") or {}) if results else {}
    return {"wall_s": None if wall_s is None else round(wall_s, 1),
            "input_tokens": sum((m.get("inputTokens") or 0) + (m.get("cacheReadInputTokens") or 0)
                                + (m.get("cacheCreationInputTokens") or 0) for m in models.values()),
            "output_tokens": sum(m.get("outputTokens") or 0 for m in models.values()),
            "result_events": len(results),
            "subagents": ((results[-1].get("subagent_stats") or {}).get("spawned") or 0)
            if results else 0}


def summarize(evs: list[dict], skill: str, names: list[str] | None = None,
              wall_s: float | None = None, root: str | None = None) -> dict:
    """The reading figures for one session (see the module docstring), plus
    `session`: whole-session tokens and wall time (`session_totals`)."""
    pending: dict[str, list[str]] = {}
    for ev in evs:
        for c in _content(ev):
            if c.get("type") != "tool_use":
                continue
            name, inp = c.get("name"), c.get("input") or {}
            if name == "Read":
                paths = referenced(str(inp.get("file_path", "")), skill, names, root)
            elif name == "Grep":
                paths = referenced(f"{inp.get('path', '')} {inp.get('glob', '')}", skill, names, root)
            elif name == "Bash" and _READERS.search(str(inp.get("command", ""))):
                paths = referenced(str(inp.get("command", "")), skill, names, root)
            else:
                continue
            if paths:
                pending[str(c.get("id"))] = paths
    files: dict[str, dict] = {}
    entries: list[str] = []
    for ev in evs:
        for c in _content(ev):
            paths = pending.pop(str(c.get("tool_use_id")), None) if c.get(
                "type") == "tool_result" else None
            if not paths:
                continue
            body = _text(c.get("content"))
            for p, (size, lines) in _split(body, paths).items():
                f = files.setdefault(p, {"path": p, "bytes": 0, "lines": 0, "calls": 0})
                f["bytes"], f["lines"], f["calls"] = f["bytes"] + size, f["lines"] + lines, f["calls"] + 1
                if Path(p).name == CATALOG:
                    entries += [i for i in _HEADING.findall(body) if i not in entries]
    rows = sorted(files.values(), key=lambda f: -f["bytes"])
    return {"reference_bytes": sum(f["bytes"] for f in rows),
            "reference_lines": sum(f["lines"] for f in rows),
            "catalog_opened": any(Path(f["path"]).name == CATALOG for f in rows),
            "catalog_entries_read": entries,
            "reference_files": rows,
            "session": session_totals(evs, wall_s)}


def reference_names(skill_dir: Path) -> list[str]:
    """Every file under `<skill_dir>/references/`, relative to it."""
    root = skill_dir / "references"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def check(c: dict, reading: dict) -> tuple[bool, str]:
    """A `reading` check: `{"type": "reading", "field": F, "min"?, "max"?,
    "pattern"?}`. A list field counts its items matching `pattern` (default:
    all); a bool counts as 0/1; the count must sit in [min (default 1), max]."""
    v = reading.get(c.get("field"))
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
    return (f"refs {reading['reference_bytes'] / 1024:.1f} KB in "
            f"{len(reading['reference_files'])} files, catalog "
            f"{'opened' if reading['catalog_opened'] else 'not opened'}; session "
            f"{(s.get('input_tokens') or 0) / 1000:.0f}k in, {s.get('wall_s') or 0:.0f}s wall")

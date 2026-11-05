"""Closed-world contract for cache-key advice shipped to users."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


_SKILL = Path(__file__).parents[1]
_PATTERNS = _SKILL / "references" / "patterns"
_SCRIPTS = _SKILL / "scripts"
_TOOL_RESULT = (
    "eslint", "prettier", "jest", "vitest", "tsbuildinfo",
    "tool-result", "tool result", "build-output", "build output",
    "generated client", ".eslintcache",
)
_ROLLING = ("github.sha", "github.run_id", "changes every run", "per-run", "commit sha")
_KEY_SHAPE = ("stable key", "deterministic cache key", "keyed on", "keyed to",
              "key on", "lockfile")


def _fix_sections() -> dict[str, str]:
    sections: dict[str, str] = {}
    for path in sorted(_PATTERNS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        marker = "**Fix**:"
        if marker not in text:
            continue
        sections[path.name] = text.split(marker, 1)[1].split("\n---", 1)[0].strip()
    return sections


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for nested in value:
            yield from _strings(nested)


def _load_blocking_path():
    path = _SCRIPTS / "blocking_path.py"
    spec = importlib.util.spec_from_file_location("cache_key_blocking_path", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prompt_literals() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(_SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "prompt" not in node.name.lower():
                continue
            strings = [part.value for part in ast.walk(node)
                       if isinstance(part, ast.Constant) and isinstance(part.value, str)]
            if strings:
                out[f"{path.name}:{node.name}"] = " ".join(strings)
    return out


def test_tool_and_build_cache_guidance_uses_a_rolling_primary_key():
    fixes = _fix_sections()
    opt6 = fixes["opt6-cache-key-entropy-too-high-or-unstable.md"]
    opt9 = fixes["opt9-tool-specific-cache-flag-not-enabled.md"]

    for text in (opt6, opt9):
        lowered = text.lower()
        assert any(token in lowered for token in _ROLLING), text
        assert "restore-keys" in lowered, text
    assert "dependency" in opt6.lower() and "lockfile" in opt6.lower()
    assert "exact" in opt9.lower() and "save" in opt9.lower()

    bp = _load_blocking_path()
    advice = {f"pattern:{name}": text for name, text in fixes.items()}
    advice.update({f"fix-meta:{index}": text
                   for index, text in enumerate(_strings(bp._FIX_META))})
    advice.update({f"prompt:{name}": text for name, text in _prompt_literals().items()})

    relevant = {
        label: text for label, text in advice.items()
        if "cache" in text.lower() and "key" in text.lower()
        and any(token in text.lower() for token in _TOOL_RESULT)
        and any(token in text.lower() for token in _KEY_SHAPE)
    }
    assert relevant, "closed-world scan found no tool-result cache-key advice"
    for label, text in relevant.items():
        lowered = text.lower()
        assert any(token in lowered for token in _ROLLING), (label, text)
        assert "restore-keys" in lowered, (label, text)

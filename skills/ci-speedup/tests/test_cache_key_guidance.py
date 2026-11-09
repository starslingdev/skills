"""Closed-world contract for cache-key advice shipped to users."""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path


_SKILL = Path(__file__).parents[1]
_PATTERNS = _SKILL / "references" / "patterns"
_SCRIPTS = _SKILL / "scripts"
_TOOL_RESULT = (
    "eslint", "prettier", "jest", "vitest", "tsbuildinfo",
    "tool-result", "tool result", "build-output", "build output",
    "generated client", ".eslintcache", "ccache", "sccache", "gocache",
    "go-build", "target/", ".next/cache", ".turbo", ".nx/cache",
    "gradle build cache", "bazel disk cache",
)
_ROLLING = ("github.sha", "github.run_id", "changes every run", "per-run", "commit sha")
_KEY_SHAPE = ("stable key", "deterministic cache key", "keyed on", "keyed to",
              "key on", "lockfile")


def _fix_sections() -> dict[str, str]:
    sections: dict[str, str] = {}
    for path in sorted(_PATTERNS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        matches = list(re.finditer(r"(?m)^\*\*Fix[^\n]*", text))
        for index, match in enumerate(matches):
            next_fix = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            divider = text.find("\n---", match.end(), next_fix)
            end = divider if divider >= 0 else next_fix
            key = path.name if index == 0 else f"{path.name}#{index + 1}"
            sections[key] = text[match.start():end].strip()
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

    def target_is_prompt(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return "prompt" in node.id.lower() or node.id.lower() == "fix_hint"
        if isinstance(node, ast.Subscript):
            key = node.slice
            return (isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and ("prompt" in key.value.lower() or key.value == "fix_hint"))
        return False

    def literal_text(node: ast.AST) -> str:
        return " ".join(part.value for part in ast.walk(node)
                        if isinstance(part, ast.Constant) and isinstance(part.value, str))

    for path in sorted(_SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "prompt" in node.name.lower():
                    out[f"{path.name}:{node.name}"] = literal_text(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if node.value is not None and any(target_is_prompt(target) for target in targets):
                    out[f"{path.name}:line-{node.lineno}"] = literal_text(node.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                            and ("prompt" in key.value.lower() or key.value == "fix_hint")):
                        out[f"{path.name}:line-{node.lineno}:{key.value}"] = literal_text(value)
    return out


def _documentation_sections() -> dict[str, str]:
    out: dict[str, str] = {}
    paths = [_SKILL / "SKILL.md"]
    paths += sorted((_SKILL / "evals").rglob("*"))
    paths += sorted((_SKILL / "references").glob("*.md"))
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in {".md", ".json", ".yaml", ".yml"}:
            continue
        text = path.read_text(encoding="utf-8")
        for index, section in enumerate(re.split(r"\n\s*\n", text), 1):
            if section.strip():
                out[f"{path.relative_to(_SKILL)}#{index}"] = section.strip()
    return out


def _prescribes_cache_key(text: str) -> bool:
    flat = " ".join(text.split())
    return bool(re.search(
        r"(?:\bcache\b.{0,160}(?:keyed\s+(?:on|to)|key\s+on|stable\s+key|"
        r"deterministic\s+cache\s+key)|(?:keyed\s+(?:on|to)|key\s+on|stable\s+key|"
        r"deterministic\s+cache\s+key).{0,160}\bcache\b)",
        flat, re.IGNORECASE,
    ))


def _relevant_advice(advice: dict[str, str]) -> dict[str, str]:
    return {
        label: text for label, text in advice.items()
        if "cache" in text.lower() and "key" in text.lower()
        and any(token in text.lower() for token in _TOOL_RESULT)
        and any(token in text.lower() for token in _KEY_SHAPE)
        and (not label.startswith("docs:") or _prescribes_cache_key(text))
    }


def _guidance_errors(advice: dict[str, str]) -> set[str]:
    return {
        label for label, text in _relevant_advice(advice).items()
        if (not any(token in text.lower() for token in _ROLLING)
            or "restore-keys" not in text.lower())
    }


def test_tool_and_build_cache_guidance_uses_a_rolling_primary_key():
    fixes = _fix_sections()
    opt6 = fixes["opt6-cache-key-entropy-too-high-or-unstable.md"]
    opt9 = fixes["opt9-tool-specific-cache-flag-not-enabled.md"]
    opt61 = fixes["opt61-missing-dependency-caching.md"]

    for text in (opt6, opt9):
        lowered = text.lower()
        assert any(token in lowered for token in _ROLLING), text
        assert "restore-keys" in lowered, text
    assert "dependency" in opt6.lower() and "lockfile" in opt6.lower()
    assert "exact" in opt9.lower() and "save" in opt9.lower()
    assert any(token in opt61.lower() for token in _ROLLING), opt61
    assert "restore-keys" in opt61.lower(), opt61
    assert "cache budget" in opt61.lower(), opt61

    bp = _load_blocking_path()
    advice = {f"pattern:{name}": text for name, text in fixes.items()}
    advice.update({f"fix-meta:{index}": text
                   for index, text in enumerate(_strings(bp._FIX_META))})
    advice.update({f"prompt:{name}": text for name, text in _prompt_literals().items()})
    advice.update({f"docs:{name}": text for name, text in _documentation_sections().items()})

    relevant = _relevant_advice(advice)
    assert relevant, "closed-world scan found no tool-result cache-key advice"
    assert not _guidance_errors(advice), _guidance_errors(advice)


def test_contract_scans_skill_evals_and_reference_docs() -> None:
    labels = set(_documentation_sections())
    assert any(label == "SKILL.md#1" for label in labels)
    assert any(label.startswith("evals/") for label in labels)
    assert any(label.startswith("references/") for label in labels)


def test_ccache_mutant_in_documentation_is_rejected() -> None:
    mutant = "Persist the ccache directory with actions/cache keyed on the lockfile hash."
    label = "docs:SKILL.md#mutant"
    assert _guidance_errors({label: mutant}) == {label}


def test_noun_first_ccache_mutant_in_documentation_is_rejected() -> None:
    mutant = "Cache ccache, keyed on the lockfile hash."
    label = "docs:SKILL.md#noun-first-mutant"
    assert _guidance_errors({label: mutant}) == {label}


def test_rolling_key_examples_are_safe_to_follow():
    texts = {
        name: text for name, text in _fix_sections().items()
        if name.startswith(("opt6-", "opt9-"))
    }
    for name, text in texts.items():
        assert "hashFiles('**/lockfiles')" not in text, (name, text)


def test_opt6_does_not_flag_a_rolling_key_with_a_restore_prefix():
    opt6 = (_PATTERNS / "opt6-cache-key-entropy-too-high-or-unstable.md").read_text(
        encoding="utf-8"
    )
    run_id_flags = [line.lower() for line in opt6.splitlines()
                    if "flag" in line.lower() and "github.run_id" in line]
    assert run_id_flags, "OPT6 must explain how run-varying keys are classified"
    assert all("restore-keys" in line for line in run_id_flags), run_id_flags


def test_eslint_prompt_uses_the_same_prefix_for_restore_and_primary_keys():
    bp = _load_blocking_path()
    text = " ".join(_strings(bp._FIX_META["eslint-no-cache"]))
    assert "<tool>-<os>-<lockfile-hash>-${{ github.sha }}" in text
    assert "<tool>-<os>-<lockfile-hash>-" in text


def test_rolling_tokens_are_not_called_permanent_misses_with_restore_fallback():
    for path in sorted(_PATTERNS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
        for sentence in sentences:
            lowered = sentence.lower()
            if not any(token in lowered for token in ("github.sha", "run_id")):
                continue
            if not any(claim in lowered for claim in ("nothing ever hits", "never hits")):
                continue
            assert "no restore-keys" in lowered, (path.name, sentence)


def test_tool_result_rolling_keys_name_the_cache_budget_tradeoff():
    fixes = _fix_sections()
    for name in (
        "opt6-cache-key-entropy-too-high-or-unstable.md",
        "opt9-tool-specific-cache-flag-not-enabled.md",
    ):
        assert "cache budget" in fixes[name].lower(), (name, fixes[name])

"""The skill must import on the oldest Python this repo says it supports.

`pyproject.toml` declares `requires-python = ">=3.9"`, and ci-secure is a
PUBLICLY INSTALLED skill: it runs against whatever `python3` an end user
happens to have, not against the 3.12 every workflow in this repo pins. That
asymmetry is the whole reason this file exists — CI cannot see the break,
because CI never runs the version that breaks.

The specific hazard is PEP 604 (`X | Y`) in an annotation that is EVALUATED at
definition time. Annotations on a parameter that carries a default, and any
annotation outside a `from __future__ import annotations` module, are evaluated
when the `def` executes — so on 3.9 they raise `TypeError: unsupported operand
type(s) for |` at IMPORT time and take the entire module down. Not a subtle
degradation: `import config` fails, so `scan.py`, `report.py` and the CI gate
all fail, for every 3.9 user, silently as far as this repo's CI is concerned.

`from __future__ import annotations` defers annotation evaluation to a string,
which makes PEP 604 safe on 3.9. Every other module under `scripts/` already
carries it; this test makes that a rule instead of a habit.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _modules() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py"))


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _pep604_annotations(tree: ast.Module) -> list[str]:
    """Every annotation in the module that uses a PEP 604 `X | Y` union."""
    found: list[str] = []

    def walk_annotation(annotation: ast.expr | None, where: str) -> None:
        if annotation is None:
            return
        for node in ast.walk(annotation):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                found.append(f"{where} (line {annotation.lineno})")
                return

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                        args.vararg, args.kwarg):
                if arg is not None:
                    walk_annotation(arg.annotation, f"{node.name}({arg.arg})")
            walk_annotation(node.returns, f"{node.name}() -> ...")
        elif isinstance(node, ast.AnnAssign):
            target = getattr(node.target, "id", "<target>")
            walk_annotation(node.annotation, f"{target}")
    return found


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_pep604_annotations_are_deferred(module: Path) -> None:
    """A `X | Y` annotation without the future import breaks the skill on 3.9.

    Red on the unfixed tree: `config.py` grew `frozenset[str] | set[str]` on a
    parameter WITH a default, which 3.9 evaluates at `def` time and refuses.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    unions = _pep604_annotations(tree)
    if not unions:
        return
    assert _has_future_annotations(tree), (
        f"{module.name} uses PEP 604 unions in {unions} but has no "
        "`from __future__ import annotations`. On Python 3.9 — which "
        "pyproject.toml declares as supported, and which an installed skill "
        "may well run under — importing this module raises TypeError and "
        "takes the whole scanner down. Every workflow here pins 3.12, so CI "
        "cannot catch this for you."
    )


def test_the_guard_can_actually_fail(tmp_path: Path) -> None:
    """The check above is not vacuous: it reds on a module shaped like the bug."""
    broken = ast.parse("def f(x: int | None = None) -> None: ...\n")
    assert _pep604_annotations(broken)
    assert not _has_future_annotations(broken)

    fixed = ast.parse(
        "from __future__ import annotations\n"
        "def f(x: int | None = None) -> None: ...\n"
    )
    assert _pep604_annotations(fixed)
    assert _has_future_annotations(fixed)

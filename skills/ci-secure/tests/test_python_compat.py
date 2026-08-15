"""The skill must import on the oldest Python this repo says it supports.

`pyproject.toml` declares `requires-python = ">=3.9"`, and ci-secure is a
PUBLICLY INSTALLED skill: it runs against whatever `python3` an end user
happens to have, not against the 3.12 every workflow in this repo pins. That
asymmetry is the whole reason this file exists — CI cannot see the break,
because CI never runs the version that breaks.

Two hazards, and they need different guards.

1. PEP 604 (`X | Y`) in an ANNOTATION. Parameter and return annotations are
   evaluated when the `def` executes, so on 3.9 they raise `TypeError:
   unsupported operand type(s) for |` at IMPORT time and take the whole module
   down. `from __future__ import annotations` defers them to strings, which
   makes the syntax safe — so for this hazard the future import IS the fix.

2. Anything 3.9 cannot even PARSE (`match` statements, and PEP 604 outside an
   annotation — a type alias, an `isinstance` argument — where the future
   import does not help because the expression is evaluated for real). Only
   compiling against 3.9's grammar catches these.

Not a subtle degradation either way: `import config` fails, so `scan.py`,
`report.py` and the CI gate all fail together, for every 3.9 user, and silently
as far as this repository's CI is concerned — every workflow here pins 3.12.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCAFFOLD = Path(__file__).resolve().parents[1] / "scaffold"


def _modules() -> list[Path]:
    # `scaffold/` as well as `scripts/`, because `scaffold/gate.py` is the file
    # with the LEAST say over its interpreter: it is copied into adopters'
    # repositories and run by whatever `python3` they have. Its own comment
    # argues the point at length — "this file is vendored into adopters' repos
    # where python3 is whatever they have" — and then nothing checked it, so
    # one `-> str | None` in a signature would have broken every adopter below
    # 3.10 with this suite green, and the byte-identity test would have carried
    # the break from the gate we run on ourselves straight into the copy.
    return sorted([*SCRIPTS.glob("*.py"), *SCAFFOLD.glob("*.py")])


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

    Red on the unfixed tree: `config.py` grew `frozenset[str] | set[str]`
    parameter annotations, which 3.9 evaluates at `def` time and refuses.
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


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_the_module_parses_under_the_oldest_supported_grammar(module: Path) -> None:
    """Syntax 3.9 cannot compile at all, which no future import can defer.

    `feature_version` makes CPython's own parser refuse anything newer than the
    target, so this covers `match` statements and every other post-3.9 form in
    one line — including the PEP 604 unions that live OUTSIDE an annotation
    (a module-level type alias, an `isinstance` argument), where the future
    import genuinely does not help because the expression is evaluated for real.
    """
    try:
        ast.parse(module.read_text(encoding="utf-8"), feature_version=(3, 9))
    except SyntaxError as exc:
        raise AssertionError(
            f"{module.name} uses syntax Python 3.9 cannot parse (line "
            f"{exc.lineno}): {exc.msg}. pyproject.toml declares 3.9 as the "
            "floor, and this is an installed skill — CI pins 3.12 and cannot "
            "catch this for you."
        ) from None


def test_the_grammar_guard_can_actually_fail() -> None:
    """The 3.9 parse check is not a no-op that accepts everything."""
    newer = "def f(v):\n    match v:\n        case 1:\n            return 2\n"
    ast.parse(newer)                                   # fine on this interpreter
    with pytest.raises(SyntaxError):
        ast.parse(newer, feature_version=(3, 9))


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

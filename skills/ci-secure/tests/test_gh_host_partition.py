"""Keep action lookups on github.com and audited-repository reads on GH_HOST."""

from __future__ import annotations

import ast
from pathlib import Path


_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_DIRECT_ACTION_READERS = {
    "_gh_release_immutability",
    "_gh_repo_visible",
    "_gh_commit_probe",
    "_gh_peel_tag_object",
}
_HELPER_ACTION_READERS = {"_verdict_action_via_gh"}


def _call_name(call: ast.Call) -> str:
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


def _list_strings(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    values: list[str] = []
    for item in node.elts:
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            values.append(item.value)
        else:
            values.append("<dynamic>")
    return values


def _functions(path: Path) -> list[ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]


def test_action_repository_api_reads_pin_github_com() -> None:
    direct: list[tuple[str, int]] = []
    helper: list[tuple[str, int]] = []

    for path in sorted(_SCRIPTS.glob("*.py")):
        for fn in _functions(path):
            for call in (node for node in ast.walk(fn) if isinstance(node, ast.Call)):
                argv = _list_strings(call.args[0]) if call.args else None
                if argv is not None and argv[:2] == ["gh", "api"]:
                    assert fn.name in _DIRECT_ACTION_READERS, (
                        f"classify the gh api call at {path.name}:{call.lineno} "
                        "as an action-repository or audited-repository read"
                    )
                    assert any(
                        argv[index:index + 2] == ["--hostname", "github.com"]
                        for index in range(len(argv) - 1)
                    ), f"action-repository read is not pinned at {path.name}:{call.lineno}"
                    direct.append((fn.name, call.lineno))

                if _call_name(call) != "run_gh_api":
                    continue
                hostname = next(
                    (kw.value for kw in call.keywords if kw.arg == "hostname"),
                    None,
                )
                if fn.name in _HELPER_ACTION_READERS:
                    assert isinstance(hostname, ast.Constant)
                    assert hostname.value == "github.com"
                    helper.append((fn.name, call.lineno))
                else:
                    assert hostname is None, (
                        f"audited-repository read must follow GH_HOST at "
                        f"{path.name}:{call.lineno}"
                    )

    assert {name for name, _ in direct} == _DIRECT_ACTION_READERS
    assert {name for name, _ in helper} == _HELPER_ACTION_READERS

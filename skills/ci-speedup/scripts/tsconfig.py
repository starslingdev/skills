"""Read the effective `noEmit` of a TypeScript project without running tsc."""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path

_MAX_CONFIG_BYTES = 256 * 1024
_MAX_CONFIG_FILES = 32
# Keep strings intact as comments and trailing commas are removed. Joining
# the tokens with whitespace keeps malformed adjacent tokens malformed.
_JSONC_TOKENS = re.compile(
    r'"(?:[^"\\]|\\.)*"|//[^\r\n]*|/\*|\s+|[^"/\s{}\[\],:]+|.', re.S)


def _jsonc(text: str) -> object:
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        match = _JSONC_TOKENS.match(text, pos)
        assert match is not None  # the final alternative matches one character
        token = match.group()
        pos = match.end()
        if token == "/*":
            end = text.find("*/", pos)
            if end < 0:
                raise ValueError("unclosed block comment in TypeScript project")
            pos = end + 2
            continue
        if token.isspace() or token.startswith("//"):
            continue
        if (token in ("}", "]") and len(tokens) >= 2 and tokens[-1] == ","
                and tokens[-2] not in ("{", "[", ",", ":")):
            tokens.pop()
        tokens.append(token)
    return json.loads(" ".join(tokens))


def _in_checkout(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("TypeScript project path leaves the checkout")
    return resolved


def _read_config(root: Path, path: Path) -> dict:
    resolved = _in_checkout(root, path)
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError("TypeScript project path is not a regular file")
    with resolved.open("rb") as stream:
        data = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(data) > _MAX_CONFIG_BYTES:
        raise ValueError("TypeScript project file is too large to read")
    doc = _jsonc(data.decode("utf-8-sig"))
    if not isinstance(doc, dict):
        raise ValueError("TypeScript project requires a JSON object")
    return doc


def _project_path(root: Path, directory: Path, project: str | None) -> Path:
    if project is not None:
        if not project or any(c in project for c in "$%`~*?[]"):
            raise ValueError("TypeScript project path is not literal")
        path = directory / project
        return path / "tsconfig.json" if _in_checkout(root, path).is_dir() else path
    # With no inputs and no project argument, tsc searches up from cwd.
    for parent in (directory, *directory.parents):
        path = parent / "tsconfig.json"
        if _in_checkout(root, path).is_file():
            return path
        if parent == root:
            break
    raise FileNotFoundError("tsconfig.json")


# A base whose value this offline read cannot know (a package not installed
# in the checkout, or one resolved through `exports`).
_UNKNOWN = object()


def _package_config(root: Path, directory: Path, spec: str) -> Path | None:
    """The installed config file a package `extends` entry names, or None.

    Mirrors tsc's lookup: climb `node_modules` from the config's directory;
    for a subpath, the file itself or the file with `.json` added; for a bare
    package, the package.json `tsconfig` field, else `tsconfig.json`. A
    package that declares `exports` is resolved by tsc through them, which
    this read does not model."""
    if spec.startswith("/") or "\\" in spec or ":" in spec:
        raise ValueError("TypeScript extends is not a relative or package path")
    parts = spec.split("/")
    size = 2 if spec.startswith("@") else 1
    if len(parts) < size or any(p in ("", ".", "..") for p in parts):
        raise ValueError("TypeScript extends is not a package name")
    name, sub = "/".join(parts[:size]), "/".join(parts[size:])
    for parent in (directory, *directory.parents):
        package = parent / "node_modules" / name
        if package.is_dir():
            try:
                _in_checkout(root, package)
                manifest = package / "package.json"
                meta = _read_config(root, manifest) if manifest.exists() else {}
                if "exports" in meta:
                    return None
                if sub:
                    found = package / sub
                    if not found.exists() and not sub.endswith(".json"):
                        found = package / (sub + ".json")
                else:
                    field = meta.get("tsconfig")
                    found = package / (field if isinstance(field, str) and field
                                       else "tsconfig.json")
                return found if _in_checkout(root, found).is_file() else None
            except ValueError:
                # Outside the checkout (a store link) or an unreadable
                # manifest: what tsc would read is unknown here.
                return None
        if parent == root:
            break
    return None


def project_no_emit(root: Path | None, wd: str, project: str | None) -> bool | None:
    """True/False for a resolved project's option, None if it cannot be read.

    Follow `extends` entries in order, then apply the child, preserving the
    difference between unset and explicitly false across multiple bases. A
    relative entry must resolve inside the checkout. A package entry is read
    from an installed `node_modules` package inside the checkout; one that is
    not installed, or that declares `exports`, leaves the value unknown unless
    a later base or the child itself sets `noEmit`.
    """
    if root is None:
        return None
    reads = 0

    def visit(path: Path, stack: frozenset[Path]) -> object:
        nonlocal reads
        real_path = _in_checkout(root, path)
        if real_path in stack or reads >= _MAX_CONFIG_FILES:
            raise ValueError("TypeScript project inheritance is cyclic or too large")
        reads += 1
        doc = _read_config(root, path)
        parents = doc.get("extends", [])
        if isinstance(parents, str):
            parents = [parents]
        if not isinstance(parents, list):
            raise ValueError("TypeScript project extends must be a path or list")
        effective: object = None
        for base in parents:
            if not isinstance(base, str) or not base:
                raise ValueError("TypeScript extends entry is not a path")
            if base.startswith(("./", "../")):
                inherited = path.parent / base
                if not _in_checkout(root, inherited).exists() and not base.endswith(".json"):
                    inherited = inherited.with_name(inherited.name + ".json")
                value = visit(inherited, stack | {real_path})
            else:
                package_base = _package_config(root, path.parent, base)
                value = (_UNKNOWN if package_base is None
                         else visit(package_base, stack | {real_path}))
            if value is not None:
                effective = value
        options = doc.get("compilerOptions", {})
        if not isinstance(options, dict):
            raise ValueError("TypeScript compilerOptions must be an object")
        if "noEmit" in options:
            effective = options["noEmit"]
            if not isinstance(effective, bool):
                raise ValueError("TypeScript noEmit must be a boolean")
        return effective

    try:
        root = root.resolve(strict=True)
        directory = _in_checkout(root, root / wd)
        if not directory.is_dir():
            return None
        value = visit(_project_path(root, directory, project), frozenset())
        return None if value is _UNKNOWN else value is True
    except (OSError, ValueError, RuntimeError):
        return None

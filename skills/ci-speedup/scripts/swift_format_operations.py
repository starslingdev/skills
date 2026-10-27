"""Bounded Swift source-formatting operations for OPT92. Never execute input.

This module proves only the operation grammar. The caller must separately
resolve setup actions, source files, selected toolchain and measured history.
"""
import re
import shlex


def formatter(command):
    if any(c in command for c in '\n;$`|&<>\\'):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if words[:2] != ['swift', 'format']:
        return None
    if words[2:5] not in (["lint", "--recursive", "--configuration"],
                         ["--in-place", "--recursive", "--configuration"]):
        return None
    if len(words) < 7:
        return None
    config, paths = words[5], [p.rstrip('/') for p in words[6:]]
    if not re.fullmatch(r'[A-Za-z_.][A-Za-z0-9_.-]*', config) or config in ('.', '..'):
        return None
    if not 1 <= len(paths) <= 8 or len(set(paths)) != len(paths):
        return None
    # Top-level directories only: no ancestor/descendant copies or shell globs.
    if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', p) for p in paths):
        return None
    return words[2], config, paths


def copy_format_diff(command, config, paths):
    """Recognize one bounded copy/format/diff block; reject every other token."""
    if not isinstance(command, str) or len(command) > 16384:
        return False
    lines = [line.strip() for line in command.splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    position = 0

    def take(pattern):
        nonlocal position
        if position == len(lines) or re.fullmatch(pattern, lines[position]) is None:
            return False
        position += 1
        return True

    for path in paths:
        p = re.escape(path)
        if not take(r'cp -r ' + p + ' ' + p + r'\.bak(?: \|\| true)?'):
            return False
    if position == len(lines):
        return False
    formatting = lines[position].removesuffix(' || true')
    if formatter(formatting) != ('--in-place', config, paths):
        return False
    position += 1
    # Echoes must be literal: shell substitutions in a message are executable.
    echo = r'echo "[^"$`\\]*"'
    for path in paths:
        p = re.escape(path)
        if not take(r'if (?:\[ -d "' + p + r'" \] && )?! diff -r ' + p
                    + ' ' + p + r'\.bak > /dev/null 2>&1; then'):
            return False
        if not take(echo):
            return False
        if not take(r'diff -r ' + p + ' ' + p + r'\.bak \|\| true'):
            return False
        if not take('exit 1') or not take('fi'):
            return False
    return take(echo) and position == len(lines)


def source_paths_ok(paths, config, modes):
    """Inputs must be tracked plain files; backup destinations must not exist."""
    if modes.get(config) not in ('100644', '100755'):
        return False
    for path in paths:
        children = {p: m for p, m in modes.items() if p.startswith(path + '/')}
        if not children or not any(p.endswith('.swift') for p in children):
            return False
        if any(m not in ('100644', '100755') for m in children.values()):
            return False
        if any(p == path + '.bak' or p.startswith(path + '.bak/') for p in modes):
            return False
    return True

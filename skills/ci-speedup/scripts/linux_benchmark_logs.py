"""Bounded runner-log provenance for the dedicated OPT92 Swift rule.

Only runner preparation and the first two known action steps are inspected.
Never search arbitrary project output for a convenient SHA or version string.
The caller must independently match the executed workflow and source tree.
"""
import re


def swift_provenance(log):
    if not isinstance(log, str) or len(log) > 2_000_000:
        return None
    lines = []
    for line in log.lstrip('\ufeff').splitlines():
        match = re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z (.*)', line)
        if not match:
            return None
        lines.append(match[1])
    boundaries = [i for i, line in enumerate(lines) if line.startswith('##[group]Run ')]
    if len(boundaries) < 3:
        return None
    first, second, third = boundaries[:3]
    checkout = lines[first].removeprefix('##[group]Run ')
    setup = lines[second].removeprefix('##[group]Run ')
    if not checkout.startswith('actions/checkout@') or not setup.startswith('swift-actions/setup-swift@'):
        return None
    downloads = {}
    for line in lines[:first]:
        match = re.fullmatch(r"Download action repository '([^']+)' \(SHA:([0-9a-f]{40})\)", line)
        if match:
            if match[1] in downloads:
                return None
            downloads[match[1]] = match[2]
    if set(downloads) != {checkout, setup}:
        return None
    commits = []
    for i in range(first, second - 1):
        if re.fullmatch(r'\[command\]/[^\s]+/git log -1 --format=%H', lines[i]):
            if not re.fullmatch(r'[0-9a-f]{40}', lines[i + 1]):
                return None
            commits.append(lines[i + 1])
    if len(commits) != 1:
        return None
    versions = []
    for i in range(second, third - 2):
        if re.fullmatch(r'\[command\]/[^\s]+/swift --version', lines[i]):
            version = re.fullmatch(r'(?:Apple )?Swift version (\d+\.\d+\.\d+) \([^\n]+\)', lines[i + 1])
            target = re.fullmatch(r'Target: (arm64|x86_64)-apple-macosx[\d.]+', lines[i + 2])
            if not version or not target:
                return None
            versions.append((version[1], target[1]))
    if len(versions) != 1:
        return None
    version, arch = versions[0]
    return {'executed_sha': commits[0], 'action_revisions': downloads,
            'toolchains': {'swift': version}, 'architecture': {'arm64': 'arm64', 'x86_64': 'x64'}[arch]}

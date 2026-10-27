"""OPT92's deliberately restricted portability admission rules.

Commit whitespace checks, isolated CPython syntax compilation and dedicated
Swift source formatting are supported. None runs project code. No fallback,
dependency installation, native build, test suite or release is admitted.
"""
from __future__ import annotations

import re
import shlex
import itertools
import json
import swift_format_operations as swift_ops

CHECKOUT = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
SETUP_PYTHON = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
SWIFT_CHECKOUT = "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09"
SETUP_SWIFT = "swift-actions/setup-swift@7ca6abe6b3b0e8b5421b88be48feee39cbf52c6a"
# Explicit hosted labels; aliases whose architecture can change are withheld.
RUNNERS = {"macos-15": "arm64", "macos-26": "arm64",
           "macos-15-intel": "x64", "macos-26-intel": "x64",
           "windows-2022": "x64", "windows-2025": "x64"}
PHRASES = {
    "runner_unresolved": "the runner or its architecture is not explicitly supported",
    "work_unresolved": "the job runs commands, actions or inputs outside the supported portability rules",
    "native_work": "the job explicitly builds, tests, signs or packages native platform work",
    "release_path": "the job participates in a release or publishing path",
    "history_unmatched": "no comparable executed hosted sample has complete source and toolchain evidence for this job",
}


def expand_jobs(key, job):
    """Resolve small literal non-OS matrices with explicit, unique leg names.

    None means that identity or expansion is unresolved. Runner expressions
    never become supported through this helper. Include/exclude and generated
    default names are deliberately outside this identity contract.
    """
    strategy = job.get("strategy")
    if not strategy:
        return [job]
    if not isinstance(strategy, dict) or set(strategy) - {"matrix", "fail-fast", "max-parallel"}:
        return None
    if not isinstance(job.get("runs-on"), str) or "${{" in job["runs-on"]:
        return None
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict) or not 1 <= len(matrix) <= 4:
        return None
    if not isinstance(job.get("name"), str):
        return None
    count = 1
    for axis, values in matrix.items():
        if (not isinstance(axis, str) or not re.fullmatch(r"[A-Za-z_][\w-]*", axis)
                or axis.lower() in {"include", "exclude", "os", "runner", "platform", "arch", "architecture"}
                or not isinstance(values, list) or not values
                or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,128}", v) for v in values)):
            return None
        count *= len(values)
    if count > 32:
        return None

    def resolve(value, context, depth=0):
        if depth > 12:
            raise ValueError("matrix nesting limit")
        if isinstance(value, str):
            if len(value) > 8192:
                raise ValueError("matrix string limit")
            value = re.sub(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}",
                           lambda m: context[m[1]], value)
            if "${{" in value:
                raise ValueError("unresolved matrix expression")
        elif isinstance(value, dict):
            return {k: resolve(v, context, depth + 1) for k, v in value.items()}
        elif isinstance(value, list):
            return [resolve(v, context, depth + 1) for v in value]
        return value

    try:
        legs = [resolve({k: v for k, v in job.items() if k != "strategy"}, dict(zip(matrix, values)))
                for values in itertools.product(*matrix.values())]
        names = [leg["name"] for leg in legs]
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            return None
        return legs
    except (ValueError, KeyError, TypeError):
        return None


def native_command(command):
    """Positive rejection evidence from executable/arguments, never quoted prose."""
    for line in str(command).splitlines():
        if re.search(r"[;&|`$]", line):
            continue
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if not words:
            continue
        tool, args = words[0], words[1:]
        if tool in {"xcodebuild", "xcrun", "codesign", "notarytool", "simctl", "productbuild", "pkgbuild", "signtool", "makeappx", "vcvarsall.bat"}:
            return True
        if tool == "flutter" and args[:1] == ["build"] and len(args) > 1 and args[1] in {"ios", "ipa", "macos", "windows"}:
            return True
        if tool == "npx" and args[:2] == ["react-native", "run-ios"]:
            return True
        targets = [args[i + 1] for i, x in enumerate(args[:-1]) if x in {"--target", "-f", "--framework", "-r", "--runtime"}]
        targets += [x.split("=", 1)[1] for x in args if x.startswith(("--target=", "--framework=", "--runtime=", "-p:TargetFramework="))]
        if tool == "cargo" and any(x.endswith("-apple-darwin") for x in targets):
            return True
        if tool == "dotnet" and args[:3] == ["workload", "install", "maui-windows"]:
            return True
        if tool == "dotnet" and args and args[0] in {"build", "test", "publish"}:
            if any(x.startswith("osx-") or re.fullmatch(r"net\d+\.\d+-(?:windows[\d.]*|ios[\d.]*|maccatalyst[\d.]*|macos[\d.]*)", x) for x in targets):
                return True
    return False


def admit(doc, key, source_files=None, source_contents=None):
    try:
        return _admit(doc, key, source_files, source_contents)
    except (AttributeError, TypeError, ValueError):
        return "hold", "work_unresolved", {}


def _swift_format_job(job, defaults, source_files, source_contents):
    """Dedicated formatter only, pinned reviewed actions and static JSON input.

    The setup revision resolves both 6.1 and 6.1.3 to 6.1.3. Its mismatch
    branch does not fail, so a separate observed-version gate is mandatory.
    Its Linux archive selector supports x64, not ARM64.
    """
    steps = job.get("steps", [])
    if len(steps) not in (3, 4) or not job["runs-on"].startswith("macos-"):
        return None
    if any(not isinstance(s, dict) or set(s) - {"name", "id", "uses", "with", "run", "shell"}
           for s in steps):
        return None
    checkout, setup = steps[:2]
    if (checkout.get("uses") != SWIFT_CHECKOUT or checkout.get("with")
            or set(checkout) - {"name", "id", "uses", "with"}
            or setup.get("uses") != SETUP_SWIFT
            or set(setup) - {"name", "id", "uses", "with"}
            or setup.get("with") not in ({"swift-version": "6.1"}, {"swift-version": "6.1.3"})):
        return None
    for step in steps[2:]:
        if (set(step) - {"name", "id", "run", "shell"}
                or not isinstance(step.get("run"), str)
                or step.get("shell", defaults.get("shell", "bash")) != "bash"):
            return None
    parsed = swift_ops.formatter(steps[2]["run"].strip())
    if not parsed or parsed[0] != "lint":
        return None
    _, config, paths = parsed
    if not isinstance(source_files, dict) or not swift_ops.source_paths_ok(paths, config, source_files):
        return None
    raw = (source_contents or {}).get(config)
    if not isinstance(raw, str) or len(raw) > 65536:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        return None
    if len(steps) == 4 and not swift_ops.copy_format_diff(steps[3]["run"], config, paths):
        return None
    return {"runner": job["runs-on"], "architecture": RUNNERS[job["runs-on"]],
            "required_toolchain": {"swift": "6.1.3"},
            "benchmark_runner": "ubuntu-24.04", "benchmark_architecture": "x64",
            "benchmark_action_pins": [SWIFT_CHECKOUT, SETUP_SWIFT],
            "checks": ["Swift 6.1.3 source formatting only; explicit JSON configuration and regular tracked inputs",
                       "reviewed pinned checkout and Swift installer; Linux installer requires x64",
                       ("bounded literal copy/format/diff commands; " if len(steps) == 4 else "literal formatter lint command; ")
                       + "no project builds, tests or plugins executed"]}


def _admit(doc, key, source_files, source_contents):
    """(decision, reason, evidence). `skip` is known native/non-target work."""
    jobs = doc.get("jobs", {})
    job = jobs.get(key, {})
    runner = job.get("runs-on")
    if isinstance(runner, str) and runner.startswith("ubuntu-"):
        return "skip", "not_native_runner", {}
    if not isinstance(runner, str) or runner not in RUNNERS:
        return "hold", "runner_unresolved", {}
    if "if" in job:
        return "hold", "work_unresolved", {}
    if any(native_command(step.get("run", "")) for step in job.get("steps", []) if isinstance(step, dict) and "if" not in step):
        return "skip", "native_work", {}
    on = doc.get("on", doc.get(True, {}))
    if isinstance(on, str):
        events = {on: None}
    elif isinstance(on, list) and all(isinstance(event, str) for event in on):
        events = dict.fromkeys(on)
    elif isinstance(on, dict):
        events = on
    else:
        return "hold", "work_unresolved", {}
    push = events.get("push")
    if (any(event in events for event in ("release", "workflow_call"))
            or (isinstance(push, dict) and any(key in push for key in ("tags", "tags-ignore")))
            or job.get("environment")):
        return "hold", "release_path", {}
    if (job.get("container") or job.get("services") or job.get("env") or doc.get("env")
            or job.get("needs") or job.get("outputs")
            or job.get("continue-on-error") or "uses" in job):
        return "hold", "work_unresolved", {}
    steps = job.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        return "hold", "work_unresolved", {}
    checked_out, python, rules = False, None, []
    defaults = dict(doc.get("defaults", {}).get("run", {}))
    defaults.update(job.get("defaults", {}).get("run", {}))
    if defaults.get("working-directory", ".") != ".":
        return "hold", "work_unresolved", {}
    swift = _swift_format_job(job, defaults, source_files, source_contents)
    if swift:
        return "candidate", "supported_operations", swift
    for step in steps:
        if not isinstance(step, dict) or set(step) - {"name", "id", "uses", "with", "run", "shell"}:
            return "hold", "work_unresolved", {}
        inputs = step.get("with") or {}
        if "uses" in step:
            action = step["uses"]
            if action == CHECKOUT and not checked_out and not rules:
                if set(inputs) - {"fetch-depth", "persist-credentials"}:
                    return "hold", "work_unresolved", {}
                if not str(inputs.get("fetch-depth", 1)).isdigit():
                    return "hold", "work_unresolved", {}
                checked_out = True
            elif action == SETUP_PYTHON and python is None and checked_out and not rules:
                if set(inputs) != {"python-version", "architecture"}:
                    return "hold", "work_unresolved", {}
                version = str(inputs["python-version"])
                if not re.fullmatch(r"3\.\d+\.\d+", version) or inputs["architecture"] != RUNNERS[runner]:
                    return "hold", "work_unresolved", {}
                python = version
            else:
                return "hold", "work_unresolved", {}
            continue
        if not checked_out or not isinstance(step.get("run"), str):
            return "hold", "work_unresolved", {}
        shell = step.get("shell", defaults.get("shell", "pwsh" if runner.startswith("windows") else "bash"))
        if shell not in ("bash", "sh"):
            return "hold", "work_unresolved", {}
        command = step["run"].strip()
        if re.search(r"[\n;$`|&<>\\]", command):
            return "hold", "work_unresolved", {}
        try:
            words = shlex.split(command)
        except ValueError:
            return "hold", "work_unresolved", {}
        if words == ["git", "show", "--check", "--format=", "--no-ext-diff", "--no-textconv", "HEAD"]:
            rules.append("committed-content whitespace check; no external diff or text conversion")
        elif python and words[:6] == ["python", "-I", "-S", "-m", "compileall", "-q"] and len(words) == 7:
            path = words[6]
            if not re.fullmatch(r"\.|[A-Za-z0-9_/-]+", path) or path.startswith("/") or ".." in path.split("/"):
                return "hold", "work_unresolved", {}
            if (not isinstance(source_files, dict) or not source_files
                    or any(mode not in ("100644", "100755") for mode in source_files.values())
                    or not any(name.endswith(".py") and (path == "." or name.startswith(path.rstrip("/") + "/"))
                               for name in source_files)):
                return "hold", "work_unresolved", {}
            rules.append(f"CPython {python} isolated syntax compilation of {path}; project modules are not executed")
        else:
            return "hold", "work_unresolved", {}
    if not rules:
        return "hold", "work_unresolved", {}
    return "candidate", "supported_operations", {"runner": runner, "architecture": RUNNERS[runner], "checks": rules}

#!/usr/bin/env python3
"""Run a bounded, offline mutation audit for the ci-speedup test suite."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


E2E_FILES = ("skills/ci-speedup/tests/test_e2e_fixture_set.py",
             "skills/ci-speedup/tests/test_offline_pipeline_e2e.py")
TEST_ROOTS = ("skills/ci-speedup/tests", "maintainers/ci-speedup/tests", "tests")
PROTECTED_WORDS = ("e2e", "install_surface", "privacy", "public_output",
                   "repo_guard", "verify_report")


@dataclass(frozen=True)
class Mutant:
    id: str
    path: str
    old: str
    new: str

    @property
    def module(self) -> str:
        return Path(self.path).name


MUTANTS = (
    Mutant(
        "collect-opt32-measurement",
        "skills/ci-speedup/scripts/collect_runs.py",
        'targets = [f for f in findings if f.get("pattern") == "OPT32"]',
        "targets = []",
    ),
    Mutant(
        "wall-clock-bound-direction",
        "skills/ci-speedup/scripts/wall_clock.py",
        "bounded = min(v, r.value)",
        "bounded = max(v, r.value)",
    ),
    Mutant(
        "blocking-path-credit-floor",
        "skills/ci-speedup/scripts/blocking_path.py",
        "_WALL_CLOCK_LONG_POLE_FLOOR_S = 30.0",
        "_WALL_CLOCK_LONG_POLE_FLOOR_S = 30000.0",
    ),
    Mutant(
        "scan-opt21-dispatch",
        "skills/ci-speedup/scripts/scan.py",
        '    "OPT21": _detect_opt21,',
        '    "OPT21": lambda doc, raw: [],',
    ),
)


def _run(root: Path, args: list[str], *, timeout: int = 900,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=root, text=True, capture_output=True, timeout=timeout,
        env=env, check=False,
    )


def _collected_tests(root: Path) -> list[str]:
    result = _run(root, [sys.executable, "-m", "pytest", "--collect-only", "-q"])
    if result.returncode:
        raise RuntimeError(f"baseline collection failed:\n{result.stdout[-2000:]}")
    return sorted(line for line in result.stdout.splitlines() if "::" in line)


def _nodeid_index(nodeids: list[str]) -> dict[tuple[str, str], list[str]]:
    index: dict[tuple[str, str], list[str]] = {}
    for nodeid in nodeids:
        path, *parts = nodeid.split("::")
        if not parts:
            continue
        index.setdefault((Path(path).stem, parts[-1]), []).append(nodeid)
    return index


def _junit_failures(path: Path, nodeids: list[str]) -> list[str]:
    index = _nodeid_index(nodeids)
    failures: set[str] = set()
    for case in ET.parse(path).iterfind(".//testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        module = str(case.get("classname") or "").split(".")[-1]
        name = str(case.get("name") or "")
        matches = index.get((module, name), [])
        failures.update(matches or [f"{module}::{name}"])
    return sorted(failures)


def _run_mutant(root: Path, mutant: Mutant, nodeids: list[str]) -> dict[str, object]:
    original = (root / mutant.path).read_text(encoding="utf-8")
    if original.count(mutant.old) != 1:
        raise RuntimeError(f"{mutant.id}: expected one mutation site")
    mutated = original.replace(mutant.old, mutant.new)
    with tempfile.TemporaryDirectory(prefix="ci-speedup-mutant-") as temp:
        temp_path = Path(temp)
        run_root = temp_path / "repo"
        added = _run(root, ["git", "worktree", "add", "--quiet", "--detach",
                            str(run_root), "HEAD"])
        if added.returncode:
            raise RuntimeError(f"{mutant.id}: could not create worktree\n{added.stderr}")
        source_path = run_root / mutant.path
        source_path.write_text(mutated, encoding="utf-8")
        junit = temp_path / "junit.xml"
        env = dict(os.environ)
        env["PYTHONPYCACHEPREFIX"] = str(temp_path / "pycache")
        started = time.monotonic()
        try:
            result = _run(
                run_root,
                [sys.executable, "-m", "pytest", "-q", "-n", "4", "--dist",
                 "worksteal", f"--junitxml={junit}"],
                env=env,
            )
        finally:
            _run(root, ["git", "worktree", "remove", "--force", str(run_root)])
        if not junit.exists():
            raise RuntimeError(f"{mutant.id}: pytest produced no JUnit report")
        killers = _junit_failures(junit, nodeids)
        if result.returncode not in (0, 1) or not killers:
            tail = (result.stdout + result.stderr)[-2000:]
            raise RuntimeError(f"{mutant.id}: invalid mutation run ({result.returncode})\n{tail}")
        return {
            "id": mutant.id,
            "module": mutant.module,
            "killers": killers,
            "wall_seconds": round(time.monotonic() - started, 3),
        }


def _natural_phrase(value: str) -> str | None:
    phrase = " ".join(value.split())
    words = re.findall(r"[A-Za-z]{2,}", phrase)
    if not (24 <= len(phrase) <= 180 and len(words) >= 4):
        return None
    if "/" in phrase or "::" in phrase or "_" in phrase:
        return None
    return phrase


def _phrase_clusters(root: Path) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], set[str]] = {}
    for test_root in TEST_ROOTS:
        for path in sorted((root / test_root).glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            rel = path.relative_to(root).as_posix()
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not function.name.startswith("test_"):
                    continue
                assertions = [n for n in ast.walk(function) if isinstance(n, ast.Assert)]
                if len(assertions) != 1:
                    continue
                comparison = assertions[0].test
                negated = False
                if isinstance(comparison, ast.UnaryOp) and isinstance(comparison.op, ast.Not):
                    comparison = comparison.operand
                    negated = True
                if (not isinstance(comparison, ast.Compare)
                        or len(comparison.ops) != 1
                        or not isinstance(comparison.ops[0], (ast.In, ast.NotIn))
                        or not isinstance(comparison.left, ast.Constant)
                        or not isinstance(comparison.left.value, str)):
                    continue
                phrase = _natural_phrase(comparison.left.value)
                if phrase:
                    present = isinstance(comparison.ops[0], ast.In) != negated
                    expectation = "present" if present else "absent"
                    groups.setdefault((phrase, expectation), set()).add(
                        f"{rel}::{function.name}")
    return [
        {"expectation": expectation, "phrase": phrase, "tests": sorted(tests)}
        for (phrase, expectation), tests in sorted(groups.items()) if len(tests) >= 2
    ]


def _overlap_candidates(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    killed_by: dict[str, set[str]] = {}
    e2e_kills: set[str] = set()
    for row in rows:
        mutant_id = str(row["id"])
        for nodeid in row["killers"]:  # type: ignore[union-attr]
            nodeid = str(nodeid)
            killed_by.setdefault(nodeid, set()).add(mutant_id)
            if any(nodeid.startswith(path + "::") for path in E2E_FILES):
                e2e_kills.add(mutant_id)
    candidates = []
    for nodeid, kills in sorted(killed_by.items()):
        lowered = nodeid.casefold()
        if any(word in lowered for word in PROTECTED_WORDS):
            continue
        if kills and kills <= e2e_kills:
            candidates.append({"test": nodeid, "mutants": sorted(kills)})
    return candidates


def _deletion_candidates(rows: list[dict[str, object]],
                         clusters: list[dict[str, object]]) -> list[dict[str, object]]:
    killed_by: dict[str, set[str]] = {}
    e2e_kills: set[str] = set()
    for row in rows:
        mutant_id = str(row["id"])
        for nodeid in row["killers"]:
            killed_by.setdefault(str(nodeid), set()).add(mutant_id)
            if str(nodeid).split("::", 1)[0] in E2E_FILES:
                e2e_kills.add(mutant_id)
    candidates: list[dict[str, object]] = []
    for cluster in clusters:
        tests = sorted(str(test) for test in cluster["tests"])
        keep = tests[-1]
        for test in tests[:-1]:
            if any(word in test for word in PROTECTED_WORDS):
                continue
            mutants = killed_by.get(test, set())
            if mutants <= e2e_kills:
                candidates.append({
                    "delete": test,
                    "keep": keep,
                    "mutants": sorted(mutants),
                    "phrase": cluster["phrase"],
                    "prerequisite": "fold both input shapes into the kept test",
                })
    return candidates


def build_report(root: Path, *, starting_count: int, local_seconds: float,
                 ci_seconds: float) -> dict[str, object]:
    nodeids = _collected_tests(root)
    print(f"baseline: {len(nodeids)} collected tests", flush=True)
    baseline_started = time.monotonic()
    baseline = _run(
        root,
        [sys.executable, "-m", "pytest", "-q", "-n", "4", "--dist", "worksteal"],
    )
    baseline_seconds = time.monotonic() - baseline_started
    if baseline.returncode:
        raise RuntimeError(f"baseline suite failed:\n{baseline.stdout[-2000:]}")
    rows = []
    for mutant in MUTANTS:
        print(f"mutant: {mutant.id}", flush=True)
        rows.append(_run_mutant(root, mutant, nodeids))
    phrase_clusters = _phrase_clusters(root)
    return {
        "schema_version": 1,
        "command": "python3 maintainers/ci-speedup/scripts/mutation_prune.py --starting-test-count COUNT --starting-local-suite-wall-seconds SECONDS --ci-suite-wall-seconds SECONDS",
        "baseline": {
            "test_count": starting_count,
            "local_suite_wall_seconds": local_seconds,
            "ci_suite_wall_seconds": ci_seconds,
        },
        "audit_test_count": len(nodeids),
        "audit_baseline_wall_seconds": round(baseline_seconds, 3),
        "e2e_test_files": list(E2E_FILES),
        "mutants": rows,
        "overlap_candidates": _overlap_candidates(rows),
        "phrase_pin_clusters": phrase_clusters,
        "deletion_candidates": _deletion_candidates(rows, phrase_clusters),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--starting-test-count", required=True, type=int)
    parser.add_argument("--starting-local-suite-wall-seconds", required=True, type=float)
    parser.add_argument("--ci-suite-wall-seconds", required=True, type=float)
    parser.add_argument(
        "--output", type=Path,
        default=Path("maintainers/ci-speedup/mutation-prune-report.json"),
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[3]
    report = build_report(
        root,
        starting_count=args.starting_test_count,
        local_seconds=args.starting_local_suite_wall_seconds,
        ci_seconds=args.ci_suite_wall_seconds,
    )
    output = args.output if args.output.is_absolute() else root / args.output
    output.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

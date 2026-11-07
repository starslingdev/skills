#!/usr/bin/env python3
"""Regenerate compact expected facts for ci-speedup whole-pipeline fixtures."""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
TEST_MODULE = REPO / "skills/ci-speedup/tests/test_e2e_fixture_set.py"


def _load_test_module():
    spec = importlib.util.spec_from_file_location("e2e_fixture_regen", TEST_MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write", action="store_true",
        help="write expected.json after printing each review diff")
    args = parser.parse_args()
    module = _load_test_module()
    changed = 0
    for fixture in sorted(path for path in module.FIXTURE_ROOT.iterdir() if path.is_dir()):
        monkeypatch = pytest.MonkeyPatch()
        try:
            with tempfile.TemporaryDirectory() as directory:
                doc, _report, checks = module.run_fixture(
                    fixture, monkeypatch, Path(directory))
                actual = module.fixture_facts(doc, checks)
        finally:
            monkeypatch.undo()
        expected_path = fixture / "expected.json"
        before = expected_path.read_text(encoding="utf-8") if expected_path.exists() else ""
        after = json.dumps(actual, indent=2, sort_keys=True) + "\n"
        if before == after:
            continue
        changed += 1
        print("".join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True),
            fromfile=str(expected_path), tofile=str(expected_path))))
        if args.write:
            expected_path.write_text(after, encoding="utf-8")
    print(f"{changed} fixture expectation file(s) changed")
    return 0 if args.write or changed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

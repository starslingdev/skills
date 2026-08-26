#!/usr/bin/env python3
"""Print the non-blocking risk names as the comma-separated list `--ignore-risks` takes.

A tiny shim so the workflow never restates the ruling: the list has exactly one home,
in registry_scan_contract.py, and both the gate and the red-proof read it from there.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from registry_scan_contract import NON_BLOCKING_RISKS  # noqa: E402

print(",".join(NON_BLOCKING_RISKS))

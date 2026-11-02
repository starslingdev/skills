#!/usr/bin/env python3
"""Scaffold this directory's cloaked fixture workflows; see ../_scaffold.py."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _scaffold  # noqa: E402

_scaffold.main(Path(__file__).resolve().parent, sys.argv[1:])

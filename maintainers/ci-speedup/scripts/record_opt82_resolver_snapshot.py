#!/usr/bin/env python3
"""Record the OPT82 resolver replay snapshot (maintainers-only).

WHAT IT RECORDS
  Every call the OPT82 test module (`skills/ci-speedup/tests/test_opt82_type_aware_lint.py`)
  makes — directly or through the detector — to `collect_runs._opt82_resolve_lint` and
  `collect_runs._detect_opt82_type_aware_lint`, with each call's full arguments (defaults
  filled in) and its output, run against the REFERENCE engine: `collect_runs.py` as it stood
  at `--engine-ref` (default `a7c2db4`, main just before OPT84 generalized the command
  resolver into `_resolve_run_leaves`). The replay tests in
  `maintainers/ci-speedup/tests/test_opt82_resolver_snapshot.py` feed the same arguments to
  today's engine and assert byte-identical output, so the refactor provably changed nothing
  OPT82 returns. Recorded at the reference engine, the resolver list includes the nested
  calls the old resolver made to itself (depth 1-5), which today's resolver no longer routes
  through `_opt82_resolve_lint`.

HOW IT RECORDS
  It exports the reference skill tree with `git archive <engine-ref> skills/ci-speedup` into a
  temp dir, copies the CURRENT OPT82 test module over the archived one, and in a child Python
  process imports the archived `collect_runs`, wraps the two functions in place (module-global
  rebinding, so the detector's internal and the resolver's recursive calls are caught too), and
  runs the OPT82 test module through `pytest.main`. Each call is kept in completion order and
  repeats are dropped (first occurrence wins): many cases share one fixture, so the module's
  ~650 resolver and ~140 detector calls reduce to 128 and 92 distinct ones.
  The output is plain JSON: sorted keys, one-space indent, trailing newline — deterministic, so
  re-recording unchanged inputs reproduces the committed file byte for byte
  (`test_recording_reproduces_the_committed_snapshot_byte_for_byte` pins that).

HOW TO RUN (from the repo root; needs git history containing --engine-ref, pytest, PyYAML)
    python3 maintainers/ci-speedup/scripts/record_opt82_resolver_snapshot.py
    python3 maintainers/ci-speedup/scripts/record_opt82_resolver_snapshot.py --out /tmp/x.json

WHEN TO RE-RECORD
  Whenever OPT82's tests or fixtures change on purpose (a new OPT82 case, an edited fixture):
  the byte-identity test goes red until the snapshot is re-recorded and committed. If OPT82's
  BEHAVIOR changes on purpose, pass `--engine-ref` naming the commit whose behavior is the new
  reference, and update the default here with it. Never re-record to silence a replay failure
  you have not explained: a replay failure means today's engine disagrees with the reference.
"""
from __future__ import annotations

import argparse
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_OPT82_TESTS = _REPO / "skills" / "ci-speedup" / "tests" / "test_opt82_type_aware_lint.py"
DEFAULT_OUT = _REPO / "maintainers" / "ci-speedup" / "tests" / "fixtures" / "opt82_resolver_snapshot.json"
DEFAULT_ENGINE_REF = "a7c2db4"


def _norm(x):
    """JSON-normal form: sets as sorted lists, anything else unserializable as str."""
    return json.loads(json.dumps(
        x, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o)))


def dumps(snap: dict) -> str:
    """The one serialization of a snapshot (the committed file is exactly this)."""
    return json.dumps(snap, sort_keys=True, indent=1) + "\n"


def _child(skill_dir: Path, out: Path) -> int:
    """Runs in a fresh interpreter: record against the archived engine under `skill_dir`."""
    sys.path.insert(0, str(skill_dir / "scripts"))
    import collect_runs as cr  # the archived reference engine
    import pytest

    snap: dict = {"resolve": [], "detect": []}
    seen: set = set()

    def keep(kind: str, entry: dict) -> None:
        key = (kind, json.dumps(entry, sort_keys=True))
        if key not in seen:
            seen.add(key)
            snap[kind].append(entry)
    resolve, detect = cr._opt82_resolve_lint, cr._detect_opt82_type_aware_lint
    rsig, dsig = inspect.signature(resolve), inspect.signature(detect)

    def rec_resolve(*a, **k):
        b = rsig.bind(*a, **k)
        b.apply_defaults()
        args = _norm(list(b.arguments.values()))
        got = resolve(*a, **k)
        keep("resolve", {"args": args, "out": _norm(got)})
        return got

    def rec_detect(*a, **k):
        b = dsig.bind(*a, **k)
        b.apply_defaults()
        vals = list(b.arguments.values())
        args, w, c = _norm(vals[:6]), vals[6], vals[7]
        w_in, c_in = _norm(w), _norm(c)
        got = detect(*a, **k)
        keep("detect", {"args": args, "withheld_in": w_in, "cands_in": c_in,
                        "out": _norm(got), "withheld": _norm(w), "cands": _norm(c)})
        return got

    cr._opt82_resolve_lint = rec_resolve
    cr._detect_opt82_type_aware_lint = rec_detect
    ini = skill_dir.parent / "pytest.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    rc = pytest.main(["-q", "-p", "no:cacheprovider", "-c", str(ini),
                      "--rootdir", str(skill_dir.parent),
                      str(skill_dir / "tests" / _OPT82_TESTS.name)])
    if rc != 0:
        print(f"OPT82 test module did not pass against the reference engine (exit {rc}); "
              "nothing written", file=sys.stderr)
        return 1
    out.write_text(dumps(snap), encoding="utf-8")
    print(f"recorded {len(snap['resolve'])} resolver and {len(snap['detect'])} detector "
          f"calls -> {out}")
    return 0


def record(out: Path, engine_ref: str = DEFAULT_ENGINE_REF) -> None:
    """Record the snapshot into `out`; raises on any failure (never writes a partial file)."""
    with tempfile.TemporaryDirectory(prefix="opt82-snapshot-") as tmp:
        tree = Path(tmp) / "tree"
        tree.mkdir()
        archive = subprocess.run(
            ["git", "-C", str(_REPO), "archive", "--format=tar", engine_ref,
             "skills/ci-speedup"], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
        skill_dir = tree / "skills" / "ci-speedup"
        shutil.copyfile(_OPT82_TESTS, skill_dir / "tests" / _OPT82_TESTS.name)
        tmp_out = Path(tmp) / "snapshot.json"
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_child",
                        str(skill_dir), str(tmp_out)], check=True, cwd=tmp)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp_out, out)


def main(argv: "list[str] | None" = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--_child"]:
        return _child(Path(argv[1]), Path(argv[2]))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--engine-ref", default=DEFAULT_ENGINE_REF)
    ns = ap.parse_args(argv)
    record(ns.out, ns.engine_ref)
    return 0


if __name__ == "__main__":
    sys.exit(main())

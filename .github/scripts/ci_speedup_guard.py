#!/usr/bin/env python3
"""Compare ci-speedup's static findings between a PR base and merge tree.

Exit 0 means the comparison ran and policy passed. Exit 1 is a policy block.
Exit 2 is an operational failure: no trustworthy comparison was produced.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

BLOCKING_ALLOWLIST = frozenset()
SCAN_TIMEOUT_SECONDS = 60
VALID_SEVERITIES = frozenset({"HIGH", "MEDIUM", "LOW"})
FAIL_ON_NEW_GAP = frozenset({
    "workflow-unreadable", "workflow-unparseable", "workflow-not-mapping",
    "parallel-group-unread", "symlink-out-of-tree", "no-workflows-dir",
    "opt19-source-unreadable", "turbo-unreadable",
})
KNOWN_GAPS = FAIL_ON_NEW_GAP | {"opt19-file-cap"}
WORKFLOW_SUFFIXES = (".yml", ".yaml")
LAYOUTS = (
    ("wave1", Path("../../skills/ci-speedup/scripts"),
     Path("../../skills/ci-speedup/references")),
    ("wave2", Path("../scripts"), Path("../references")),
    ("vendored", Path("."), Path("../references")),
)


class OperationalFailure(RuntimeError):
    pass


def esc(value: Any, *, prop: bool = False) -> str:
    """Escape a GitHub workflow-command property/value."""
    text = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def flat(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value).split()).replace("`", "'").replace("|", "\\|")
    return text[:limit] + ("..." if len(text) > limit else "")


def annotation(level: str, message: str, finding: dict[str, Any] | None = None) -> None:
    props = ""
    if finding:
        path, line = finding.get("workflow_file"), finding.get("line")
        if isinstance(path, str) and path and not path.startswith("("):
            props = f" file={esc(path, prop=True)}"
            if isinstance(line, int) and line > 0:
                props += f",line={line}"
    print(f"::{level}{props}::{esc(flat(message))}")


def resolve_layout(guard_file: Path | None = None) -> tuple[Path, Path, str]:
    here = (guard_file or Path(__file__)).resolve().parent
    for name, engine_rel, refs_rel in LAYOUTS:
        engine = (here / engine_rel / "scan.py").resolve()
        refs = (here / refs_rel).resolve()
        if engine.is_file() and (refs / "patterns").is_dir() \
                and (refs / "type-aware-lint-rules.tsv").is_file():
            return engine, refs, name
    raise OperationalFailure("no supported guard/engine layout found")


def load_engine(engine: Path):
    spec = importlib.util.spec_from_file_location("ci_speedup_guard_engine", engine)
    if spec is None or spec.loader is None:
        raise OperationalFailure(f"could not load engine at {engine}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:  # a trusted engine load still must fail stated
        raise OperationalFailure(f"engine import failed: {flat(exc)}") from exc
    return module


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def catalog_sha(patterns: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(patterns.glob("opt*.md")):
        h.update(path.name.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def run_scan(engine: Path, tree: Path, default_branch: str) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as stream:
        out = Path(stream.name)
    try:
        try:
            proc = subprocess.run(
                [sys.executable, str(engine), "--root", str(tree), "--out", str(out),
                 "--default-branch", default_branch],
                text=True, capture_output=True, timeout=SCAN_TIMEOUT_SECONDS, check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            raise OperationalFailure(
                f"scanner timed out after {SCAN_TIMEOUT_SECONDS}s for {tree}") from exc
        if proc.returncode:
            detail = flat(proc.stderr or proc.stdout or "no output")
            raise OperationalFailure(
                f"scanner exited {proc.returncode} for {tree}: {detail}")
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationalFailure(f"scanner produced invalid JSON for {tree}: {flat(exc)}") from exc
    finally:
        out.unlink(missing_ok=True)


def validate_scan(doc: Any, engine_module: Any) -> None:
    if not isinstance(doc, dict):
        raise OperationalFailure("scanner output is not an object")
    for key, typ in (("findings", list), ("scan_incomplete", list),
                     ("scanned_workflows", int),
                     ("catalog_patterns_without_detector", list)):
        if not isinstance(doc.get(key), typ):
            raise OperationalFailure(f"scanner output field {key!r} has the wrong type")
    catalog_ids = set()
    for entry in engine_module.load_catalog(
            Path(engine_module.__file__).resolve().parents[1] / "references" / "patterns"):
        catalog_ids.add(entry.pattern)
    dispatched = set(engine_module._DETECTORS) | set(engine_module._NON_PERFILE_PATTERNS)
    dispatched |= {entry.pattern for entry in engine_module.load_catalog(
        Path(engine_module.__file__).resolve().parents[1] / "references" / "patterns")
        if entry.match or entry.yaml_path}
    granular = set(getattr(engine_module, "INSTANCE_GRANULAR", ()))
    for pattern in BLOCKING_ALLOWLIST:
        if pattern not in catalog_ids or pattern not in dispatched or pattern not in granular:
            raise OperationalFailure(
                f"blocking allowlist entry {pattern} lacks a catalog entry, dispatched detector, "
                "or INSTANCE_GRANULAR proof")
    for i, finding in enumerate(doc["findings"]):
        if not isinstance(finding, dict):
            raise OperationalFailure(f"finding {i} is not an object")
        for key, typ in (("pattern", str), ("severity", str),
                         ("workflow_file", str), ("subject", str),
                         ("advisory", bool), ("needs_log_confirmation", bool)):
            if key in ("advisory", "needs_log_confirmation") and key not in finding:
                continue
            if not isinstance(finding.get(key), typ):
                raise OperationalFailure(f"finding {i} field {key!r} is missing or malformed")
        if not finding["subject"]:
            raise OperationalFailure(f"finding {i} has an empty subject")
        if finding["severity"] not in VALID_SEVERITIES:
            raise OperationalFailure(f"finding {i} has unknown severity {finding['severity']!r}")
        if finding["pattern"] not in catalog_ids:
            raise OperationalFailure(f"finding {i} names unknown pattern {finding['pattern']}")
    for i, record in enumerate(doc["scan_incomplete"]):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise OperationalFailure(f"scan_incomplete record {i} is malformed")
        if record.get("kind") not in KNOWN_GAPS:
            raise OperationalFailure(
                f"scan_incomplete record {i} has missing or unknown kind {record.get('kind')!r}")
    unresolved = doc.get("opt96_unresolved", [])
    if not isinstance(unresolved, list) or any(not isinstance(x, dict) for x in unresolved):
        raise OperationalFailure("opt96_unresolved is malformed")
    parallel = doc.get("parallel_steps", {})
    if not isinstance(parallel, dict) or not isinstance(parallel.get("held_back", []), list):
        raise OperationalFailure("parallel_steps.held_back is malformed")


def workflow_files(root: Path) -> dict[str, bytes]:
    directory = root / ".github" / "workflows"
    if not directory.is_dir():
        return {}
    result = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix in WORKFLOW_SUFFIXES:
            try:
                if path.resolve().is_relative_to(root.resolve()):
                    result[path.relative_to(root).as_posix()] = path.read_bytes()
            except (OSError, ValueError):
                continue
    return result


def _normalized_lines(data: bytes) -> set[str]:
    text = data.decode("utf-8", "replace")
    return {" ".join(line.split()) for line in text.splitlines() if line.strip()}


def rename_map(base_root: Path, merge_root: Path) -> dict[str, str]:
    """Map merge workflow paths to the base content identity they continue."""
    base, merge = workflow_files(base_root), workflow_files(merge_root)
    mapping: dict[str, str] = {}
    unused_b, unused_m = set(base), set(merge)
    hashes_b: dict[str, list[str]] = defaultdict(list)
    hashes_m: dict[str, list[str]] = defaultdict(list)
    for path, data in base.items():
        hashes_b[hashlib.sha256(data).hexdigest()].append(path)
    for path, data in merge.items():
        hashes_m[hashlib.sha256(data).hexdigest()].append(path)
    for digest in sorted(set(hashes_b) & set(hashes_m)):
        bs, ms = sorted(hashes_b[digest]), sorted(hashes_m[digest])
        pairs: list[tuple[str, str]] = []
        for path in sorted(set(bs) & set(ms)):
            pairs.append((path, path)); bs.remove(path); ms.remove(path)
        pairs.extend(zip(bs, ms))
        for bp, mp in pairs:
            mapping[mp] = bp; unused_b.discard(bp); unused_m.discard(mp)

    scores: dict[tuple[str, str], float] = {}
    for bp in unused_b:
        left = _normalized_lines(base[bp])
        for mp in unused_m:
            right = _normalized_lines(merge[mp])
            scores[bp, mp] = difflib.SequenceMatcher(
                None, sorted(left), sorted(right), autojunk=False).ratio()
    best_b: dict[str, tuple[float, list[str]]] = {}
    best_m: dict[str, tuple[float, list[str]]] = {}
    for bp in unused_b:
        vals = [(scores[bp, mp], mp) for mp in unused_m]
        if vals:
            high = max(v[0] for v in vals)
            best_b[bp] = (high, sorted(mp for score, mp in vals if score == high))
    for mp in unused_m:
        vals = [(scores[bp, mp], bp) for bp in unused_b]
        if vals:
            high = max(v[0] for v in vals)
            best_m[mp] = (high, sorted(bp for score, bp in vals if score == high))
    for bp in sorted(unused_b):
        score, mps = best_b.get(bp, (0.0, []))
        if score >= 0.8 and len(mps) == 1:
            mp = mps[0]
            back_score, bps = best_m.get(mp, (0.0, []))
            if back_score == score and bps == [bp]:
                mapping[mp] = bp
    for path in sorted(unused_m):
        if path not in mapping and path in base:
            mapping[path] = path
    return mapping


def scope(finding: dict[str, Any], mapping: dict[str, str], non_perfile: set[str]) -> str:
    if finding["pattern"] in non_perfile:
        return "(repo)"
    path = finding["workflow_file"]
    if path.startswith(".github/workflows/") and path.endswith(WORKFLOW_SUFFIXES):
        return mapping.get(path, path)
    return path


def finding_key(f: dict[str, Any], mapping: dict[str, str], non_perfile: set[str]):
    return (f["pattern"], scope(f, mapping, non_perfile), f["subject"],
            bool(f.get("advisory")), bool(f.get("needs_log_confirmation")))


def confirmed(f: dict[str, Any]) -> bool:
    return not f.get("advisory") and not f.get("needs_log_confirmation")


def multiset_delta(base: list[dict[str, Any]], merge: list[dict[str, Any]],
                   mapping: dict[str, str], non_perfile: set[str]):
    b_buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    m_buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for f in base: b_buckets[finding_key(f, {}, non_perfile)].append(f)
    for f in merge: m_buckets[finding_key(f, mapping, non_perfile)].append(f)
    new, resolved, existing = [], [], []
    for key in sorted(set(b_buckets) | set(m_buckets)):
        bs, ms = b_buckets[key], m_buckets[key]
        common = min(len(bs), len(ms))
        existing.extend(ms[:common]); resolved.extend(bs[common:]); new.extend(ms[common:])
    return new, resolved, existing


def pair_aggregate_scopes(
        base: list[dict[str, Any]], merge: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Pair repeated aggregate findings by exact jobs, then greatest overlap."""
    remaining_base = list(base)
    remaining_merge = list(merge)
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    for current in list(remaining_merge):
        jobs = set(current.get("affected_jobs", []))
        exact = next((old for old in remaining_base
                      if set(old.get("affected_jobs", [])) == jobs), None)
        if exact is not None:
            pairs.append((current, exact))
            remaining_merge.remove(current)
            remaining_base.remove(exact)

    while remaining_base and remaining_merge:
        candidates = []
        for current in remaining_merge:
            current_jobs = set(current.get("affected_jobs", []))
            for old in remaining_base:
                old_jobs = set(old.get("affected_jobs", []))
                candidates.append((
                    -len(current_jobs & old_jobs),
                    sorted(current_jobs), sorted(old_jobs), current, old))
        *_, current, old = min(candidates, key=lambda row: row[:3])
        pairs.append((current, old))
        remaining_merge.remove(current)
        remaining_base.remove(old)
    pairs.extend((current, None) for current in remaining_merge)
    return pairs


def job_body(root: Path, finding: dict[str, Any]) -> str:
    """Best-effort normalized YAML body for a job-id subject, stdlib only."""
    subject, rel = finding.get("subject"), finding.get("workflow_file")
    if not isinstance(subject, str) or not isinstance(rel, str) or subject.startswith("("):
        return ""
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    escaped = re.escape(subject)
    match = re.compile(rf"^(\s+)(?:['\"]?{escaped}['\"]?):\s*(?:#.*)?$")
    for index, line in enumerate(lines):
        found = match.match(line)
        if not found:
            continue
        indent = len(found.group(1))
        body = []
        for child in lines[index + 1:]:
            stripped = child.lstrip()
            if stripped and len(child) - len(stripped) <= indent:
                break
            body.append(" ".join(stripped.split()))
        return "\n".join(part for part in body if part)
    return ""


def pair_moves(new: list[dict[str, Any]], resolved: list[dict[str, Any]],
               mapping: dict[str, str], non_perfile: set[str],
               base_root: Path, merge_root: Path):
    pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    left_new = [f for f in new if not confirmed(f)]
    available_new = [f for f in new if confirmed(f)]
    available_resolved = list(resolved)
    while True:
        candidates = []
        for nf in available_new:
            ns = scope(nf, mapping, non_perfile)
            for rf in available_resolved:
                if not confirmed(rf) or rf["pattern"] != nf["pattern"]:
                    continue
                rs = scope(rf, {}, non_perfile)
                preference = 0 if rf["subject"] == nf["subject"] else (1 if rs == ns else 2)
                left = job_body(base_root, rf) or json.dumps(
                    rf.get("affected_jobs", []), sort_keys=True)
                right = job_body(merge_root, nf) or json.dumps(
                    nf.get("affected_jobs", []), sort_keys=True)
                similarity = difflib.SequenceMatcher(None, left, right).ratio()
                candidates.append((preference, -similarity,
                                   str(nf.get("workflow_file", "")), str(nf["subject"]),
                                   str(rf.get("workflow_file", "")), str(rf["subject"]), nf, rf))
        if not candidates:
            break
        *_, nf, rf = min(candidates, key=lambda row: row[:6])
        available_new.remove(nf); available_resolved.remove(rf)
        label = "moved" if rf["subject"] == nf["subject"] else "offset"
        pairs.append((label, nf, rf))
    left_new.extend(available_new)
    return left_new, available_resolved, pairs


def gap_key(record: dict[str, Any], mapping: dict[str, str], tree: Path) -> tuple:
    path, kind = record["path"], record["kind"]
    if path.startswith(".github/workflows/"):
        path = mapping.get(path, path)
    if kind == "opt19-source-unreadable":
        candidate = tree / path
        try:
            path = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            pass
    if os.path.isabs(path):
        path = Path(path).name
    return path, kind


def held_back(doc: dict[str, Any], mapping: dict[str, str]) -> set[tuple]:
    records = set()
    for row in doc.get("opt96_unresolved", []):
        wf = mapping.get(row.get("workflow_file", ""), row.get("workflow_file", ""))
        records.add(("opt96", wf, row.get("job"), row.get("family")))
    for row in doc.get("parallel_steps", {}).get("held_back", []):
        path = mapping.get(row.get("path", ""), row.get("path", ""))
        records.add(("parallel", path, row.get("job")))
    return records


def write_summary(path: Path | None, lines: list[str]) -> None:
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines).rstrip() + "\n")
    except OSError as exc:
        annotation("warning", f"could not write job summary: {exc}")


def tree_sha(tree: Path) -> str:
    proc = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                          text=True, capture_output=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else "unavailable"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--merge", type=Path, required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--summary", type=Path)
    args, unknown = parser.parse_known_args(argv)
    for arg in unknown:
        annotation("warning", f"ignoring unknown guard argument: {arg}")
    summary = args.summary
    try:
        base, merge = args.base.resolve(), args.merge.resolve()
        if not base.is_dir() or not merge.is_dir():
            raise OperationalFailure("--base and --merge must name directories")
        engine, refs, layout = resolve_layout()
        module = load_engine(engine)
        base_doc = run_scan(engine, base, args.default_branch)
        merge_doc = run_scan(engine, merge, args.default_branch)
        validate_scan(base_doc, module); validate_scan(merge_doc, module)
        if base_doc["scanned_workflows"] > 0 and merge_doc["scanned_workflows"] == 0:
            raise OperationalFailure("merge tree has no workflow files but base does")

        mapping = rename_map(base, merge)
        non_perfile = set(module._NON_PERFILE_PATTERNS)
        new, resolved, existing = multiset_delta(
            base_doc["findings"], merge_doc["findings"], mapping, non_perfile)
        unpaired, resolved, pairs = pair_moves(
            new, resolved, mapping, non_perfile, base, merge)

        base_counts = Counter(f["pattern"] for f in base_doc["findings"] if confirmed(f))
        merge_counts = Counter(f["pattern"] for f in merge_doc["findings"] if confirmed(f))
        increases = {p: (base_counts[p], merge_counts[p]) for p in BLOCKING_ALLOWLIST
                     if merge_counts[p] > base_counts[p]}
        bootstrap = False
        try:
            Path(__file__).resolve().relative_to(merge)
            bootstrap = True
        except ValueError:
            pass

        base_gaps = Counter(gap_key(r, {}, base) for r in base_doc["scan_incomplete"])
        merge_gaps = Counter(gap_key(r, mapping, merge) for r in merge_doc["scan_incomplete"])
        new_gaps = list((merge_gaps - base_gaps).elements())
        fatal_gaps = [g for g in new_gaps if g[1] in FAIL_ON_NEW_GAP]
        if fatal_gaps:
            kinds = ", ".join(f"{kind} at {path}" for path, kind in fatal_gaps)
            raise OperationalFailure(f"new scan coverage gap: {kinds}")

        base_held = held_back(base_doc, {})
        merge_held = held_back(merge_doc, mapping)
        new_held = sorted(merge_held - base_held)

        warnings = 0
        blocked_findings = []
        remaining_by_pattern: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for f in unpaired:
            remaining_by_pattern[f["pattern"]].append(f)
        for pattern, (before, after) in sorted(increases.items()):
            amount = after - before
            candidates = [f for f in remaining_by_pattern[pattern] if confirmed(f)]
            blocked_findings.extend(candidates[-amount:])
        if bootstrap:
            annotation("warning", "bootstrap: the base branch has no guard at this path; findings are report-only")
            warnings += 1
        for f in unpaired:
            is_blocked = f in blocked_findings and not bootstrap
            prefix = (f"ci-speedup-guard blocked: {f['pattern']} - " if is_blocked
                      else f"ci-speedup-guard warning: {f['pattern']} - ")
            message = prefix + flat(f.get("evidence") or f.get("title") or "new configuration match")
            annotation("error" if is_blocked else "warning", message, f)
            print(message)
            if not is_blocked: warnings += 1
        for label, nf, rf in pairs:
            message = (f"ci-speedup-guard warning: {nf['pattern']} {label}: "
                       f"{rf.get('workflow_file')}:{rf.get('subject')} -> "
                       f"{nf.get('workflow_file')}:{nf.get('subject')}")
            annotation("warning", message, nf); warnings += 1
        for path, kind in new_gaps:
            annotation("warning", f"new non-blocking scan coverage gap: {kind} at {path}")
            warnings += 1
        for row in new_held:
            annotation("warning", f"new detector held-back record: {row}"); warnings += 1
        shared_gaps = base_gaps & merge_gaps
        for path, kind in sorted(shared_gaps):
            annotation("warning", f"not scanned on either side: {kind} at {path}")
            warnings += 1
        base_workflows, merge_workflows = workflow_files(base), workflow_files(merge)
        for merge_path, base_path in sorted(mapping.items()):
            kinds = {kind for path, kind in shared_gaps if path == base_path}
            if (kinds and base_path in base_workflows and merge_path in merge_workflows
                    and base_workflows[base_path] != merge_workflows[merge_path]):
                annotation("warning", f"changed but unscannable: {merge_path} ({', '.join(sorted(kinds))})")
                warnings += 1

        # An aggregate workflow finding can keep its key while covering more jobs.
        base_aggregates: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        merge_aggregates: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        for f in base_doc["findings"]:
            if f["subject"] == "(workflow)":
                base_aggregates[finding_key(f, {}, non_perfile)].append(f)
        for f in merge_doc["findings"]:
            if f["subject"] == "(workflow)":
                merge_aggregates[finding_key(f, mapping, non_perfile)].append(f)
        for key, current_findings in merge_aggregates.items():
            old_findings = base_aggregates.get(key, [])
            all_old_jobs = {job for old in old_findings
                            for job in old.get("affected_jobs", [])}
            for f, old in pair_aggregate_scopes(old_findings, current_findings):
                old_jobs = (set(old.get("affected_jobs", []))
                            if old is not None else all_old_jobs)
                grew = sorted(set(f.get("affected_jobs", []))
                              - old_jobs)
                if grew:
                    annotation("warning", f"{f['pattern']} scope grew to jobs: {', '.join(grew)}", f)
                    warnings += 1
                if old is None or f.get("pattern") != "OPT14":
                    continue
                old_legs = old.get("matrix_setup_legs")
                new_legs = f.get("matrix_setup_legs")
                repeated = f.get("matrix_setup_repeated")
                if not all(isinstance(value, dict)
                           for value in (old_legs, new_legs, repeated)):
                    continue
                for job in sorted(set(old_legs) & set(new_legs)):
                    before, after = old_legs.get(job), new_legs.get(job)
                    work = repeated.get(job)
                    if (not isinstance(before, int) or not isinstance(after, int)
                            or after <= before or not isinstance(work, list)
                            or not ({"install", "build"} & set(work))):
                        continue
                    repeated_text = ", ".join(str(name) for name in work[:-1])
                    if len(work) > 1:
                        repeated_text += f" and {work[-1]}"
                    elif work:
                        repeated_text = str(work[0])
                    annotation(
                        "warning",
                        f"{f['pattern']} job {job}: {before} -> {after} legs; each leg "
                        f"repeats {repeated_text}; try running the work in parallel on "
                        "one machine first (see OPT88)",
                        f,
                    )
                    warnings += 1

        if increases and not bootstrap:
            verdict = "Policy block: " + ", ".join(
                f"{p} count {a} -> {b}" for p, (a, b) in sorted(increases.items()))
        elif bootstrap:
            verdict = "Pass (report-only bootstrap)"
        else:
            verdict = "Pass"
        lines = ["## ci-speedup-guard", "", verdict + ".",
                 (f"{len(blocked_findings) if not bootstrap else 0} new blocking occurrences, "
                  f"{warnings} warnings, {len(existing)} existing, {len(resolved)} resolved; "
                  f"{base_doc['scanned_workflows']} workflow files in base and "
                  f"{merge_doc['scanned_workflows']} in merge. Blocking list: "
                  f"{', '.join(sorted(BLOCKING_ALLOWLIST)) or '(empty)'}."), "",
                 "### New blocking findings", ""]
        if blocked_findings and not bootstrap:
            lines.extend(f"- {f['pattern']} - {flat(f.get('evidence') or f.get('title'))}"
                         for f in blocked_findings)
        else:
            lines.append("- None.")
        lines.extend(["", "### New warnings", ""])
        warning_findings = [f for f in unpaired if f not in blocked_findings or bootstrap]
        lines.extend((f"- {f['pattern']} at {flat(f.get('workflow_file'))} ({flat(f.get('subject'))})"
                      for f in warning_findings),)
        lines.extend(f"- {nf['pattern']} {label}: {flat(rf.get('workflow_file'))} -> {flat(nf.get('workflow_file'))}"
                     for label, nf, rf in pairs)
        if not warning_findings and not pairs and not new_gaps and not new_held:
            lines.append("- None.")
        lines.extend(["", "### Resolved findings", ""])
        lines.extend((f"- {f['pattern']} at {flat(f.get('workflow_file'))} ({flat(f.get('subject'))})"
                      for f in resolved),)
        if not resolved:
            lines.append("- None.")
        existing_counts = Counter(f["pattern"] for f in existing)
        lines.extend(["", "### Existing findings", "",
                      "- " + (", ".join(f"{p}: {n}" for p, n in sorted(existing_counts.items()))
                                or "None."), "", "### Coverage", "",
                      "- Blocking patterns: " + (", ".join(sorted(BLOCKING_ALLOWLIST)) or "none"),
                      "- Static catalog entries without a detector: " +
                      (", ".join(merge_doc["catalog_patterns_without_detector"]) or "none"),
                      f"- Pre-existing scan gaps: {sum(shared_gaps.values())}",
                      f"- New held-back detector records: {len(new_held)}", "",
                      "Only static configuration patterns were checked. Run history and critical-path "
                      "detectors did not run; these matches are not measured CI costs.", "",
                 f"Engine sha256: `{sha256_file(engine)}`  ",
                 f"Catalog sha256: `{catalog_sha(refs / 'patterns')}`  ",
                 f"Mode: `{'report-only' if bootstrap else 'enforcing'}`  ",
                 f"Layout: `{layout}`  ",
                 f"Merge first parent: `{tree_sha(base)}`  ",
                 f"Event base SHA: `{os.environ.get('EVENT_BASE_SHA', 'not provided')}`"])
        write_summary(summary, lines)
        print(verdict)
        return 1 if increases and not bootstrap else 0
    except OperationalFailure as exc:
        message = f"ci-speedup-guard operational failure: {flat(exc)}"
        annotation("error", message)
        print(message, file=sys.stderr)
        write_summary(summary, ["## ci-speedup-guard", "", "Operational failure.", "", message])
        return 2
    except BaseException as exc:  # never emit an unexplained red
        message = f"ci-speedup-guard operational failure: unexpected error: {flat(exc)}"
        annotation("error", message)
        print(message, file=sys.stderr)
        write_summary(summary, ["## ci-speedup-guard", "", "Operational failure.", "", message])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

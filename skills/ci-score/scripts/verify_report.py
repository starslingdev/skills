"""ci-score report invariant checker (B2) — one command, PASS/FAIL.

`verify(doc, report_text, registry)` asserts the report faithfully renders its
findings document. Verifying a report must never be manual archaeology: CI,
the dogfood sweep, and a human all run the same checks and get the same
verdict. Invariants (each failure is a named, quoted message):

0. HEADER — the provenance table quotes the document's own commit (short SHA
   present; a -dirty run is labelled dirty; a clean run never claims dirt).
1. STAMP↔CARD — the headline value/grade/passed/applicable on the card are
   the stamp's own numbers (nothing recomputes), and every stamp check row
   appears on the card. The score gauge on the card shows the stamp's value,
   a filled-block count equal to round-half-up(value·25/100), and a
   pass/applicable/na tail equal to the stamp's own tallies.
2. INTERNAL CONSISTENCY — the stamp's passed/applicable equal a recount of
   its own check states (a drifted or hand-edited stamp fails loudly).
3. DISCLOSURE — the adherence-not-speed sentence sits in the report whenever
   a grade is shown (never on refusals — there is no grade to misread).
4. RANKED RECS — every FAILED check has a recommendation section carrying an
   impact tier, a risk tier, a fix recipe block, a guide link, and an agent
   handoff prompt that quotes the finding's evidence (grounding).
5. RANK ORDER — the recommendation order actually follows the fix table's
   impact×risk ordering (a mis-ordered report fails).
6. FILES — every file a recommendation cites is a file the stamp's check
   itself cites (the report never invents a path).

Exit 0 all green; exit 1 with one line per violated invariant.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_SPEC = _SCRIPT_DIR.parent / "references" / "ci-score-spec.json"


def _load_sibling(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def verify(doc: dict[str, Any], report: str, registry: dict[str, Any]) -> list[str]:
    """All violated invariants, empty when the report is faithful."""
    rr = _load_sibling("ci_score_render_report", "render_report.py")
    problems: list[str] = []

    # 0. HEADER — the provenance table states the document's own commit
    # faithfully: the short SHA appears, a -dirty run says so, and a clean
    # run never claims dirt. (Display fields like repo_slug are unchecked
    # cosmetics; the commit + dirty marker are the load-bearing provenance.)
    # Scoped to the header region — everything before the score gauge's code
    # fence — so a 7-char SHA echoed in an evidence string or a pinned-action
    # URL further down can't stand in for the provenance table, and stray
    # "tree was dirty" text below the header can't flip the verdict.
    # Runs BEFORE the collection_refusal return: a refusal that carries a
    # commit (no_parseable_workflows refuses AFTER provenance is stamped)
    # still renders a real header, which must be just as faithful.
    header = report.split("```", 1)[0]
    commit = str(doc.get("commit_sha", ""))
    if commit:
        dirty = commit.endswith("-dirty")
        short = (commit[: -len("-dirty")] if dirty else commit)[:7]
        if short not in header:
            problems.append(f"HEADER: scored commit {short!r} absent from the provenance header")
        if dirty and "tree was dirty" not in header:
            problems.append("HEADER: commit_sha is -dirty but the header does not say the tree was dirty")
        if not dirty and "tree was dirty" in header:
            problems.append("HEADER: header claims a dirty tree but commit_sha is clean")

    if "collection_refusal" in doc:
        want = str(doc["collection_refusal"].get("human_reason", ""))
        if want and want not in report:
            problems.append("REFUSAL: collection_refusal reason not stated in report")
        return problems

    stamp = doc.get("ci_score")
    if not isinstance(stamp, dict):
        err = (doc.get("data_sources") or {}).get("ci_score_error")
        if err and "unavailable" not in report.lower():
            problems.append("ERROR: ci_score_error recorded but report does not say the score was unavailable")
        return problems

    checks = [c for c in (stamp.get("checks") or []) if isinstance(c, dict)]

    # The card links every check name to an in-document "What each check means"
    # subsection; that anchor target (a `### <label>` heading) must exist in the
    # SAME document, so no link points at an external file (owner, 2026-07-28).
    # This holds on BOTH scored and REFUSAL reports: a refusal stamp still
    # carries a full check list (all n/a), and the card links each name, so the
    # appendix must render on refusal paths too (OD-CS20 made this live).
    appendix_heads = set(re.findall(r"^### (.+)$", report, flags=re.M))

    def _appendix_problems() -> list[str]:
        out = []
        for chk in checks:
            label = str(chk.get("label") or chk.get("check_id"))
            if label in report and label not in appendix_heads:
                out.append(
                    f"APPENDIX: card links check {label!r} but no '### {label}' "
                    "section exists in the report (broken in-document anchor)")
        return out

    if stamp.get("refusal"):
        reason = str(stamp["refusal"].get("human_reason", ""))
        # strip either prefix, matching render_card (OD-CS20's automation_only
        # reason begins "Not scored:", the others "No score:").
        core = reason
        low = reason.lower()
        for prefix in ("no score:", "not scored:"):
            if low.startswith(prefix):
                core = reason[len(prefix):].strip()
                break
        if core and core not in report:
            problems.append("REFUSAL: stamp refusal reason not stated in report")
        if rr.DISCLOSURE in report:
            problems.append("DISCLOSURE: shown on a refusal (there is no grade to misread)")
        problems += _appendix_problems()
        return problems

    # 1. stamp↔card headline agreement — the card line quotes the stamp.
    head = (f"## CI Score: **{stamp.get('value')}/100** — "
            f"{stamp.get('checks_passed')} of {stamp.get('checks_applicable')} "
            "applicable checks")
    if head not in report:
        problems.append(f"CARD: headline does not match the stamp verbatim ({head!r} absent)")
    for chk in checks:
        label = str(chk.get("label") or chk.get("check_id"))
        if label not in report:
            problems.append(f"CARD: stamp check {label!r} missing from report")
    problems += _appendix_problems()

    # 1b. GAUGE↔STAMP — every number in the score gauge IS the stamp's: the
    # value, the filled-block count (= round-half-up(value·25/100)), AND the
    # pass/applicable/na tail. Doctoring the rendered bar (a block added or
    # removed, the value nudged) or the tail (a miscounted pass/na) goes red —
    # the gauge cannot state a number the stamp does not.
    gauge = re.search(r"CI Score  (\d+)/100  ▏([█░]+)▕  ([^\n]*)", report)
    if gauge is None:
        problems.append("GAUGE: score gauge line absent from the card")
    else:
        shown = int(gauge.group(1))
        bar = gauge.group(2)
        tail = gauge.group(3).rstrip()
        filled = bar.count("█")
        if shown != stamp.get("value"):
            problems.append(
                f"GAUGE: gauge value {shown} != stamp value {stamp.get('value')}")
        if len(bar) != 25:
            problems.append(f"GAUGE: bar is {len(bar)} blocks, not 25")
        want = (shown * 25 + 50) // 100
        if filled != want:
            problems.append(
                f"GAUGE: {filled} filled blocks != round-half-up({shown}·25/100)={want}")
        na = sum(1 for c in checks if c.get("state") == "not_applicable")
        want_tail = (f"{stamp.get('checks_passed')} of "
                     f"{stamp.get('checks_applicable')} checks pass")
        if na > 0:
            want_tail += f" · {na} n/a"
        if tail != want_tail:
            problems.append(
                f"GAUGE: tail {tail!r} != the stamp's {want_tail!r}")

    # 2. internal consistency — recount the stamp's own states.
    passed = sum(1 for c in checks if c.get("state") == "pass")
    applicable = passed + sum(1 for c in checks if c.get("state") == "fail")
    if passed != stamp.get("checks_passed") or applicable != stamp.get("checks_applicable"):
        problems.append(
            f"STAMP: passed/applicable ({stamp.get('checks_passed')}/"
            f"{stamp.get('checks_applicable')}) != recount of its own states "
            f"({passed}/{applicable}) — drifted or hand-edited stamp")

    # 3. disclosure beside the grade.
    if rr.DISCLOSURE not in report:
        problems.append("DISCLOSURE: adherence-not-speed sentence missing")

    # 4 + 6. every FAIL has a full ranked rec; files are the stamp's own.
    fails = [c for c in checks if c.get("state") == "fail"]
    rec_heads = re.findall(r"^### (\d+)\. (.+?) — impact: (\S+), risk: (\S+)$",
                           report, flags=re.M)
    rec_by_label = {label: (int(n), impact, risk)
                    for n, label, impact, risk in rec_heads}
    for chk in fails:
        label = str(chk.get("label") or chk.get("check_id"))
        if label not in rec_by_label:
            problems.append(f"RECS: FAILED check {label!r} has no ranked recommendation")
            continue
        # the rec's own section: from its heading to the next ### or EOF
        sec_m = re.search(rf"^### \d+\. {re.escape(label)} — .*?(?=^### |\Z)",
                          report, flags=re.M | re.S)
        sec = sec_m.group(0) if sec_m else ""
        if "```yaml" not in sec:
            problems.append(f"RECS: {label!r} has no fix recipe block")
        if "Agent handoff prompt" not in sec:
            problems.append(f"RECS: {label!r} has no handoff prompt")
        evidence = str(chk.get("evidence") or "")
        if evidence and evidence not in sec:
            problems.append(f"RECS: {label!r} prompt/finding does not quote the stamp's evidence")
        stamp_files = set(chk.get("files") or [])
        for cited in re.findall(r"`([^`]+)`", sec.split("**Guide:**")[0]):
            if "/" in cited and cited not in stamp_files and not cited.startswith("<"):
                problems.append(f"RECS: {label!r} cites {cited!r}, not a file the stamp cites")

    # 5. rank order follows the fix table.
    order = {c["check_id"]: i for i, c in enumerate(registry["checks"])}
    id_by_label = {str(c.get("label")): str(c.get("check_id")) for c in fails}
    expected = sorted((id_by_label[l] for l in rec_by_label if l in id_by_label),
                      key=lambda cid: rr._rank_key(cid, order))
    actual = [id_by_label[l] for l, _ in
              sorted(rec_by_label.items(), key=lambda kv: kv[1][0])
              if l in id_by_label]
    if expected != actual:
        problems.append(f"ORDER: recommendations are {actual}, fix table says {expected}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ci-score: verify a report against its findings")
    parser.add_argument("--findings", default="findings.json")
    parser.add_argument("--report", default="report.md")
    parser.add_argument("--spec", default=str(_DEFAULT_SPEC), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    doc = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    registry = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    problems = verify(doc, Path(args.report).read_text(encoding="utf-8"), registry)
    for p in problems:
        print(f"FAIL {p}", file=sys.stderr)
    print("report: OK" if not problems else f"report: {len(problems)} invariant violation(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

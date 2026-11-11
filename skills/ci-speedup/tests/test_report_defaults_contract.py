"""Keep the owner-reviewed report defaults tied to their engine constants."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
DOC = SKILL / "references" / "report-defaults.md"

REQUIRED_DECISIONS = {
    "pr-gate-sample-size", "pr-gate-eligibility", "pr-gate-span",
    "pr-candidate-pages", "config-era-boundary", "rolling-volume-window",
    "run-list-depth", "default-branch-push-cohort", "rendered-pole-count",
    "holds-up-threshold", "rare-presence-rule", "within-group-order",
    "needs-chain-handling", "bookkeeping-cutoff", "ranking-minimum-count",
    "gate-frequency-interval", "magnitude-noise-band", "bimodal-noise-band",
    "ten-percent-sizing-rules", "per-pattern-minimums", "saving-caps",
    "rendered-row-caps",
}


def _assignments(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
    return out


def _contract(text: str) -> dict[str, dict[str, object]]:
    match = re.search(r"```json report-defaults-contract\n(.*?)\n```", text, re.DOTALL)
    assert match, "report-defaults.md must carry the machine-readable contract"
    return json.loads(match.group(1))


def test_report_defaults_review_covers_every_owner_decision():
    assert DOC.exists(), "report-defaults.md is missing"
    text = DOC.read_text(encoding="utf-8")
    assert "Status: **awaiting owner approval**" in text
    decisions = set(re.findall(r"^### `([^`]+)`$", text, re.MULTILINE))
    assert decisions == REQUIRED_DECISIONS, REQUIRED_DECISIONS ^ decisions
    for decision in decisions:
        section = text.split(f"### `{decision}`", 1)[1].split("\n### ", 1)[0]
        for field in ("**Current value:**", "**Report statements:**", "**Reason:**",
                      "**Proposed decision:**"):
            assert field in section, f"{decision} lacks {field}"
    assert "references/report-defaults.md" in (SKILL / "SKILL.md").read_text(encoding="utf-8")


def test_report_defaults_machine_contract_matches_the_engine():
    text = DOC.read_text(encoding="utf-8")
    contract = _contract(text)
    modules = {
        "collect_runs.py": _assignments(SCRIPTS / "collect_runs.py"),
        "blocking_path.py": _assignments(SCRIPTS / "blocking_path.py"),
    }
    assert contract, "the report-defaults contract cannot be empty"
    for key, row in contract.items():
        module, symbol = str(row["source"]).split(":", 1)
        assert symbol in modules[module], f"{key}: unknown source {row['source']}"
        assert row["value"] == modules[module][symbol], (
            f"{key}: documented {row['value']!r}, engine has {modules[module][symbol]!r}")

    collect_source = (SCRIPTS / "collect_runs.py").read_text(encoding="utf-8")
    assert re.search(r'add_argument\("--max-runs", type=int, default=20\)', collect_source)
    assert "end - _dt.timedelta(days=30)" in collect_source
    assert "**Current value:** 20 runs per workflow" in text
    assert "**Current value:** 30 days" in text

    tree = ast.parse(collect_source)
    split = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_bimodal_split")
    defaults = {
        arg.arg: ast.literal_eval(default)
        for arg, default in zip(split.args.kwonlyargs, split.args.kw_defaults)
    }
    assert defaults == {"min_n": 8, "min_frac": 0.30,
                        "min_ratio": 1.6, "min_high_s": 60.0}
    assert "**Keep 8 / 30% / 1.6x / 60 seconds.**" in text

    for pattern in ("OPT29", "OPT35", "OPT38", "OPT44"):
        assert re.search(
            rf'"{pattern}"\s*:\s*\{{[^}}]*"hit_rate"\s*:\s*0\.1(?:0)?\b',
            collect_source, re.DOTALL), pattern
    assert "10% modeled occurrence for OPT29, OPT35, OPT38, and OPT44" in text

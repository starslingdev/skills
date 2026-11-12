"""Keep the proposed report defaults tied to their engine constants."""

from __future__ import annotations

import ast
import functools
import json
import operator
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
    "rendered-row-caps", "workflow-timing-depth", "required-suite-eligibility",
    "typical-floor-thresholds", "severity-cutoffs", "success-only-caveat",
    "minimum-timed-runs", "rare-push-lane", "lifecycle-minimum",
    "merge-queue-proof-thresholds", "timeout-recommendation-thresholds",
    "dependency-cache-floor", "cold-cache-miss-threshold",
    "cache-partial-miss-floor",
    "ceiling-minimum-gates", "partial-majority-rule",
}

REQUIRED_VERIFIER_MIRRORS = {
    "verify_report.py:_VR_DEFAULT_BRANCH_PUSH_SAMPLE_MAX",
    "verify_report.py:_VR_RARE_PRESENCE_MIN_PR",
    "verify_report.py:_VR_RARE_PRESENCE_FRAC",
    "verify_report.py:_VR_POLE_RECUR_FLOOR",
    "verify_report.py:_VR_CHAIN_DIVERGENCE_PCT",
    "verify_report.py:_VR_AGG_GATE_TRIVIAL_S",
    "verify_report.py:_VR_TIER2_CAP",
    "verify_report.py:_VR_RUNNER_MINUTE_SPINE_CAP",
    "verify_report.py:_VR_OPT64_MIN_RETRIED_RUNS",
    "verify_report.py:_VR_OPT79_MIN_HITS",
    "verify_report.py:_VR_OPT79_MIN_MISSES",
    "verify_report.py:_VR_OPT79_MIN_WASTE_FRAC",
    "verify_report.py:_VR_OPT79_MIN_WASTE_S",
    "verify_report.py:_VR_OPT79_TAIL_MIN_FRAC",
    "verify_report.py:_VR_OPT79_POLE_MIN_HEADROOM_S",
    "verify_report.py:_VR_OPT80_MIN_SAMPLED_OCCURRENCES",
    "verify_report.py:_VR_OPT80_TAIL_P95_MULTIPLE",
    "verify_report.py:_VR_OPT80_TAIL_P95_ABS_S",
    "verify_report.py:_VR_OPT80_MIN_TAIL_RUNS",
    "verify_report.py:_VR_OPT80_MIN_PROVEN_TAIL_RUNS",
    "verify_report.py:_VR_OPT80_MIN_GAP_S",
    "verify_report.py:_VR_OPT80_LOG_PROBE_MAX",
    "verify_report.py:_VR_OPT81_MIN_SAMPLES_PER_LABEL",
    "verify_report.py:_VR_OPT81_MIN_GAP_FRAC",
    "verify_report.py:_VR_OPT81_MIN_GAP_S",
    "verify_report.py:_VR_OPT81_COVERED_FRAC",
    "verify_report.py:_VR_OPT81_CACHE_LEVER_MIN_S",
    "verify_report.py:_VR_OPT83_MIN_SHARE",
    "verify_report.py:_VR_OPT83_MIN_STEP_S",
    "verify_report.py:_VR_OPT84_MIN_STEP_S",
    "verify_report.py:_VR_OPT84_MIN_SHARE",
    "verify_report.py:_VR_CACHE_TAIL_MIN_FRAC",
    "verify_report.py:_VR_CACHE_COLD_MISS_PCT",
    "verify_report.py:_VR_CACHE_FLOOR_BY_FIX_KEY",
    "verify_report.py:_VR_OPT57_NEAR_DEFAULT_TIMEOUT_S",
    "verify_report.py:_VR_OPT57_DEFAULT_KILL_CEILING_S",
    "verify_report.py:_VR_OPT57_MIN_TIMEOUT_S",
    "verify_report.py:_VR_OPT57_TIMEOUT_BUFFER_S",
    "verify_report.py:_VR_WITHHELD_JOBS_SHOWN",
    "verify_report.py:_VR_PARTIAL_MISS_FLOOR_PCT",
}

PROVISIONAL_MODULE_LANDING_MARKERS = {
    "pr_sample_contract.py": "(#333)",
    "gate_frequency_contract.py": "(#317)",
}

PROVISIONAL_SYMBOL_LANDING_MARKERS = {
    "gate_frequency_contract.py:POLE_HOLDS_UP_MIN_COUNT": "(#318)",
}

REQUIRED_PROVISIONAL_SOURCES = {
    "pr_sample_contract.py:PR_SAMPLE_TARGET": 100,
    "pr_sample_contract.py:PR_SAMPLE_MIN_DISTINCT_DAYS": 5,
    "pr_sample_contract.py:PR_SAMPLE_CANDIDATE_PAGE_CAP": 12,
    "pr_sample_contract.py:PR_SAMPLE_PARTIAL_MAJORITY_NUMERATOR": 1,
    "pr_sample_contract.py:PR_SAMPLE_PARTIAL_MAJORITY_DENOMINATOR": 2,
    "gate_frequency_contract.py:GATE_BOOKKEEPING_CUTOFF_S": 60.0,
    "gate_frequency_contract.py:GATE_MIN_COUNT_FOR_RANKING": 5,
    "gate_frequency_contract.py:GATE_INTERVAL_Z": 1.96,
    "gate_frequency_contract.py:POLE_HOLDS_UP_MIN_COUNT": 2,
}

REQUIRED_CURRENT_SOURCES = {
    "blocking_path.py:_AGG_GATE_TRIVIAL_S": 30.0,
    "blocking_path.py:_ALSO_NOTICED_CAP": 12,
    "blocking_path.py:_CHAIN_MAKESPAN_DIVERGENCE_PCT": 25.0,
    "blocking_path.py:_FLOOR_COOCCUR_MAJORITY_FRAC": 0.5,
    "blocking_path.py:_FLOOR_NEAR_UNIVERSAL_FRAC": 0.8,
    "blocking_path.py:_MAG_WIDE_REL": 0.25,
    "blocking_path.py:_OPT64_MIN_RETRIED_RUNS": 3,
    "blocking_path.py:_PARTIAL_MISS_FLOOR_PCT": 40.0,
    "blocking_path.py:_POLE_RECUR_FLOOR": 2,
    "blocking_path.py:_RARE_PRESENCE_FRAC": 0.5,
    "blocking_path.py:_RARE_PRESENCE_MIN_PR": 6,
    "blocking_path.py:_RUNNER_MINUTE_SPINE_CAP": 12,
    "blocking_path.py:_SEV_HIGH_S": 300.0,
    "blocking_path.py:_SEV_MED_S": 120.0,
    "blocking_path.py:_TIER2_CAP": 12,
    "blocking_path.py:_TOP_WORKFLOWS": 5,
    "blocking_path.py:_WALL_CLOCK_LONG_POLE_FLOOR_S": 30.0,
    "blocking_path.py:_WITHHELD_JOBS_SHOWN": 5,
    "collect_runs.py:_AGG_GATE_TRIVIAL_S": 30.0,
    "collect_runs.py:_CACHE_BUDGET_FULL_FRACTION": 0.9,
    "collect_runs.py:_CACHE_COLD_MISS_PCT": 99.5,
    "collect_runs.py:_CACHE_TAIL_MIN_FRAC": 0.25,
    "collect_runs.py:_COST_RUNLIST_MAX": 100,
    "collect_runs.py:_DEEPEN_TOP_CHECKS": 12,
    "collect_runs.py:_DEFAULT_BRANCH_PUSH_SAMPLE_MAX": 10,
    "collect_runs.py:_DEPENDENCY_CACHE_FLOOR_PCT": 50.0,
    "collect_runs.py:_GHA_DEFAULT_JOB_TIMEOUT_S": 21600.0,
    "collect_runs.py:_MAG_WIDE_REL": 0.25,
    "collect_runs.py:_MIN_TIMED_RUNS": 3,
    "collect_runs.py:_NOOP_FLOOR_S": 30.0,
    "collect_runs.py:_OPT57_DEFAULT_KILL_CEILING_S": 22500.0,
    "collect_runs.py:_OPT57_KILL_GRACE_S": 900.0,
    "collect_runs.py:_OPT57_MIN_TIMEOUT_S": 900.0,
    "collect_runs.py:_OPT57_NEAR_DEFAULT_TIMEOUT_S": 20520.0,
    "collect_runs.py:_OPT57_TIMEOUT_BUFFER_S": 600.0,
    "collect_runs.py:_OPT57_TIMEOUT_MULTIPLIER": 1.5,
    "collect_runs.py:_OPT64_MIN_RETRIED_RUNS": 3,
    "collect_runs.py:_OPT79_MIN_HITS": 3,
    "collect_runs.py:_OPT79_MIN_MISSES": 3,
    "collect_runs.py:_OPT79_MIN_WASTE_FRAC": 0.2,
    "collect_runs.py:_OPT79_MIN_WASTE_S": 5.0,
    "collect_runs.py:_OPT79_POLE_MIN_HEADROOM_S": 1.0,
    "collect_runs.py:_OPT80_LOG_PROBE_MAX": 4,
    "collect_runs.py:_OPT80_MIN_GAP_S": 20.0,
    "collect_runs.py:_OPT80_MIN_PROVEN_TAIL_RUNS": 2,
    "collect_runs.py:_OPT80_MIN_SAMPLED_OCCURRENCES": 6,
    "collect_runs.py:_OPT80_MIN_TAIL_RUNS": 2,
    "collect_runs.py:_OPT80_TAIL_P95_ABS_S": 30.0,
    "collect_runs.py:_OPT80_TAIL_P95_MULTIPLE": 3.0,
    "collect_runs.py:_OPT81_CACHE_LEVER_MIN_S": 30.0,
    "collect_runs.py:_OPT81_COVERED_FRAC": 0.5,
    "collect_runs.py:_OPT81_MIN_GAP_FRAC": 0.25,
    "collect_runs.py:_OPT81_MIN_GAP_S": 30.0,
    "collect_runs.py:_OPT81_MIN_SAMPLES_PER_LABEL": 8,
    "collect_runs.py:_OPT82_MIN_LINT_P50_S": 60.0,
    "collect_runs.py:_OPT83_MIN_SHARE": 0.15,
    "collect_runs.py:_OPT83_MIN_STEP_S": 20.0,
    "collect_runs.py:_OPT84_MIN_SHARE": 0.0,
    "collect_runs.py:_OPT84_MIN_STEP_S": 20.0,
    "collect_runs.py:_OPT98_CHURN_SAMPLE": 30,
    "collect_runs.py:_OPT98_DIFFERENT_RATIO": 1.5,
    "collect_runs.py:_OPT98_MIN_MERGES_PER_DAY": 30,
    "collect_runs.py:_OPT98_MIN_RUNS": 15,
    "collect_runs.py:_OPT98_REPROVE_RATIO": 0.8,
    "collect_runs.py:_OPT98_WINDOW_DAYS": 30,
    "collect_runs.py:_POLE_RECUR_FLOOR": 2,
    "collect_runs.py:_PR_CANDIDATE_PAGE_CAP": 3,
    "collect_runs.py:_PUSH_LANE_RARE_PER_30D": 8,
    "collect_runs.py:_RARE_PRESENCE_FRAC": 0.5,
    "collect_runs.py:_RARE_PRESENCE_MIN_PR": 6,
    "collect_runs.py:_REPR_PR_TARGET": 20,
    "collect_runs.py:_SHALLOW_RUNS": 10,
    "collect_runs.py:_SUCCESS_ONLY_CAVEAT_FAILURE_RATE": 0.2,
    "collect_runs.py:_SUCCESS_ONLY_CAVEAT_MIN_RUNS": 10,
    "collect_runs.py:_TRIAGE_WALLCLOCK_FLOOR_S": 90.0,
    "pr_lifecycle.py:MIN_MEASURED": 10,
    "verify_report.py:_CEILING_MIN_GATING_PR": 5,
    "verify_report.py:_VR_AGG_GATE_TRIVIAL_S": 30.0,
    "verify_report.py:_VR_CACHE_COLD_MISS_PCT": 99.5,
    "verify_report.py:_VR_CACHE_FLOOR_BY_FIX_KEY": {
        "dependency-cache": 50.0
    },
    "verify_report.py:_VR_CACHE_TAIL_MIN_FRAC": 0.25,
    "verify_report.py:_VR_CHAIN_DIVERGENCE_PCT": 25.0,
    "verify_report.py:_VR_DEFAULT_BRANCH_PUSH_SAMPLE_MAX": 10,
    "verify_report.py:_VR_OPT57_DEFAULT_KILL_CEILING_S": 22500.0,
    "verify_report.py:_VR_OPT57_DEFAULT_TIMEOUT_MIN": 360.0,
    "verify_report.py:_VR_OPT57_MIN_SUCCESS_SAMPLES": 3,
    "verify_report.py:_VR_OPT57_MIN_TIMEOUT_S": 900.0,
    "verify_report.py:_VR_OPT57_NEAR_DEFAULT_TIMEOUT_S": 20520.0,
    "verify_report.py:_VR_OPT57_TIMEOUT_BUFFER_S": 600.0,
    "verify_report.py:_VR_OPT57_TIMEOUT_MULTIPLIER": 1.5,
    "verify_report.py:_VR_OPT64_MIN_RETRIED_RUNS": 3,
    "verify_report.py:_VR_OPT79_MIN_HITS": 3,
    "verify_report.py:_VR_OPT79_MIN_MISSES": 3,
    "verify_report.py:_VR_OPT79_MIN_WASTE_FRAC": 0.2,
    "verify_report.py:_VR_OPT79_MIN_WASTE_S": 5.0,
    "verify_report.py:_VR_OPT79_POLE_MIN_HEADROOM_S": 1.0,
    "verify_report.py:_VR_OPT79_TAIL_MIN_FRAC": 0.25,
    "verify_report.py:_VR_OPT80_LOG_PROBE_MAX": 4,
    "verify_report.py:_VR_OPT80_MIN_GAP_S": 20.0,
    "verify_report.py:_VR_OPT80_MIN_PROVEN_TAIL_RUNS": 2,
    "verify_report.py:_VR_OPT80_MIN_SAMPLED_OCCURRENCES": 6,
    "verify_report.py:_VR_OPT80_MIN_TAIL_RUNS": 2,
    "verify_report.py:_VR_OPT80_TAIL_P95_ABS_S": 30.0,
    "verify_report.py:_VR_OPT80_TAIL_P95_MULTIPLE": 3.0,
    "verify_report.py:_VR_OPT81_CACHE_LEVER_MIN_S": 30.0,
    "verify_report.py:_VR_OPT81_COVERED_FRAC": 0.5,
    "verify_report.py:_VR_OPT81_MIN_GAP_FRAC": 0.25,
    "verify_report.py:_VR_OPT81_MIN_GAP_S": 30.0,
    "verify_report.py:_VR_OPT81_MIN_SAMPLES_PER_LABEL": 8,
    "verify_report.py:_VR_OPT83_MIN_SHARE": 0.15,
    "verify_report.py:_VR_OPT83_MIN_STEP_S": 20.0,
    "verify_report.py:_VR_OPT84_MIN_SHARE": 0.0,
    "verify_report.py:_VR_OPT84_MIN_STEP_S": 20.0,
    "verify_report.py:_VR_PARTIAL_MISS_FLOOR_PCT": 40.0,
    "verify_report.py:_VR_POLE_RECUR_FLOOR": 2,
    "verify_report.py:_VR_RARE_PRESENCE_FRAC": 0.5,
    "verify_report.py:_VR_RARE_PRESENCE_MIN_PR": 6,
    "verify_report.py:_VR_RUNNER_MINUTE_SPINE_CAP": 12,
    "verify_report.py:_VR_TIER2_CAP": 12,
    "verify_report.py:_VR_WITHHELD_JOBS_SHOWN": 5,
    "verify_report.py:_WALL_CLOCK_LONG_POLE_FLOOR_S": 30.0
}


@functools.cache
def _assignment_nodes(path: Path) -> dict[str, ast.expr]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and node.value is not None:
                out[target.id] = node.value
    return out


_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
}


@functools.cache
def _assignments(path: Path) -> dict[str, object]:
    nodes = _assignment_nodes(path)
    values: dict[str, object] = {}

    def evaluate(node: ast.expr, stack: frozenset[str]) -> object:
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            pass
        if isinstance(node, ast.Name):
            if node.id in stack or node.id not in nodes:
                raise ValueError(node.id)
            if node.id not in values:
                values[node.id] = evaluate(nodes[node.id], stack | {node.id})
            return values[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
            return _BINARY_OPS[type(node.op)](
                evaluate(node.left, stack), evaluate(node.right, stack))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand, stack)
            return value if isinstance(node.op, ast.UAdd) else -value
        raise ValueError(ast.dump(node))

    for name, node in nodes.items():
        try:
            values[name] = evaluate(node, frozenset({name}))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return values


@functools.cache
def _symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } | set(_assignment_nodes(path))


def _module(source: str) -> tuple[Path, str]:
    module, symbol = source.split(":", 1)
    if module == "verify_report.py":
        return SKILL / "tests" / module, symbol
    return SCRIPTS / module, symbol


def _assert_value(source: str, expected: object) -> None:
    path, symbol = _module(source)
    assert path.exists(), f"missing contract module {path.name}"
    assignments = _assignments(path)
    assert symbol in assignments, f"unknown source {source}"
    assert assignments[symbol] == expected, (
        f"{source}: documented {expected!r}, engine has {assignments[symbol]!r}")


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
        assert "awaiting owner approval" in section, (
            f"{decision} does not preserve its pending approval status")
    assert "references/report-defaults.md" in (SKILL / "SKILL.md").read_text(encoding="utf-8")


def test_report_defaults_machine_contract_matches_the_engine():
    text = DOC.read_text(encoding="utf-8")
    contract = _contract(text)
    assert contract, "the report-defaults contract cannot be empty"
    assert {row.get("decision") for row in contract.values()} == REQUIRED_DECISIONS
    sources = {
        source
        for row in contract.values()
        for source in row.get("sources", {})
    }
    planned_sources = {
        source: value
        for row in contract.values()
        for source, value in row.get("planned_sources", {}).items()
    }
    documented_sources = {
        source: value
        for row in contract.values()
        for source, value in row.get("sources", {}).items()
    }
    changelog = (SKILL / "CHANGELOG.md").read_text(encoding="utf-8")
    assert REQUIRED_VERIFIER_MIRRORS <= sources
    assert documented_sources == REQUIRED_CURRENT_SOURCES
    assert REQUIRED_PROVISIONAL_SOURCES.items() <= planned_sources.items()
    for key, row in contract.items():
        for source, expected in row.get("sources", {}).items():
            _assert_value(source, expected)
        for source in row.get("symbols", []):
            path, symbol = _module(source)
            assert path.exists() and symbol in _symbols(path), f"{key}: unknown source {source}"
        # #315/#317/#318 own these provisional modules. Pin the proposed values
        # now; once a module is present on this branch, the same guard checks it.
        for source, expected in row.get("planned_sources", {}).items():
            path, symbol = _module(source)
            if not path.exists():
                marker = PROVISIONAL_MODULE_LANDING_MARKERS[path.name]
                assert marker not in changelog, (
                    f"missing landed provisional module {path.name} ({marker})")
                continue
            names = _assignment_nodes(path)
            if path.name == "gate_frequency_contract.py" and symbol == "POLE_HOLDS_UP_MIN_COUNT":
                # #317 creates the shared module; #318 adds this fourth value.
                if symbol not in names:
                    marker = PROVISIONAL_SYMBOL_LANDING_MARKERS[source]
                    assert marker not in changelog, (
                        f"missing landed provisional source {source} ({marker})")
                    continue
            assert symbol in names, f"missing provisional source {source}"
            _assert_value(source, expected)

    collect_source = (SCRIPTS / "collect_runs.py").read_text(encoding="utf-8")
    assert "end - _dt.timedelta(days=30)" in collect_source
    assert "**Current value:** up to 20 selected PR commits" in text
    assert re.search(r"does\s+not set this population", text)
    assert "**Current value:** 30 days" in text
    assert "collect_runs.py::_window_30d" in text

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
    assert "when per-pole co-occurrence data is available" in text
    assert "presence fallback" in text
    assert "`max(3 × P50, P50 + 30 seconds)`" in text

    for pattern in ("OPT29", "OPT35", "OPT38", "OPT44"):
        assert re.search(
            rf'"{pattern}"\s*:\s*\{{[^}}]*"hit_rate"\s*:\s*0\.1(?:0)?\b',
            collect_source, re.DOTALL), pattern
    assert "10% modeled occurrence for OPT29, OPT35, OPT38, and OPT44" in text

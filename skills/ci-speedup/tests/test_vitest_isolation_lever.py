"""OPT78 — per-file test isolation rebuilding shared module state.

The lever: a vitest suite that is the measured long pole spends more time
re-importing the app's module graph for every test file (per-file isolation,
vitest's default) than running assertions. Opting named, reviewed files into a
shared module registry removes that repeated import — and can silently break
correctness by leaking state between files, which is why the finding carries a
HIGH risk rating, a mandatory guardrail and a rollout.

Two halves are tested here:

- the SCAN half (`scan.py`) reads the repo's vitest config and reports whether
  the isolation opt-out is already configured — a fact, never an inference;
- the DRILL half (`blocking_path._parse_log`) fires the `vitest-isolate-pool`
  leaf only when the drilled long-pole log is import-bound AND that scanned
  config fact says isolation is still on. No config fact ⇒ no finding
  (fail closed): the finding's whole claim is "you are still paying per-file
  isolation", and that is not readable from a log alone.

Run: pytest -v skills/ci-speedup/tests/test_vitest_isolation_lever.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SCAN_SCRIPT = _SKILL_DIR / "scripts" / "scan.py"
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)
import collect_runs as cr  # noqa: E402  (uniquely-named module; no cross-skill clash)


# --- fixtures ---------------------------------------------------------------

_IMPORT_BOUND_LOG = "\n".join([
    " RUN  v4.1.4 /repo/api",
    " Test Files  149 passed (149)",
    " Duration  96.12s (transform 8.97s, setup 1.01s, import 245.03s, "
    "tests 214.54s, environment 8ms)",
])

# Same shape, but the assertions dominate the imports — nothing to move.
_TEST_BOUND_LOG = "\n".join([
    " RUN  v4.1.4 /repo/api",
    " Test Files  149 passed (149)",
    " Duration  96.12s (transform 8.97s, setup 1.01s, import 20.03s, "
    "tests 214.54s, environment 8ms)",
])

_ISO_ON = {"runner": "vitest", "readable": True, "isolation_opt_out": False,
           "configs": ["vitest.config.ts"], "opt_out_evidence": []}
_ISO_OFF = {"runner": "vitest", "readable": True, "isolation_opt_out": True,
            "configs": ["vitest.config.ts"],
            "opt_out_evidence": ["vitest.config.ts:7: isolate: false"]}
_ISO_UNREADABLE = {"runner": None, "readable": False, "isolation_opt_out": False,
                   "configs": [], "opt_out_evidence": []}


def _have_yaml() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import yaml"], capture_output=True, text=True,
    ).returncode == 0


def _scan(root: Path) -> dict:
    if not _have_yaml():
        pytest.skip("PyYAML not installed in the test runner")
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "ci.yml").write_text("name: x\non: push\njobs: {}\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


# --- the scan half: what the config actually says ----------------------------

def test_scan_reports_isolation_still_on_when_config_does_not_opt_out(tmp_path: Path):
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: { pool: 'threads' } })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["runner"] == "vitest"
    assert iso["readable"] is True
    assert iso["isolation_opt_out"] is False
    assert "vitest.config.ts" in iso["configs"]


def test_scan_reports_the_opt_out_with_the_line_it_read(tmp_path: Path):
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({\n"
        "  test: {\n"
        "    isolate: false,\n"
        "  },\n"
        "})\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["isolation_opt_out"] is True
    # The fact is quoted, not asserted: the evidence carries the line it read.
    assert any("isolate: false" in e for e in iso["opt_out_evidence"])


def test_scan_reads_a_workspace_project_opt_out(tmp_path: Path):
    """Linear's shape: a separate opt-in project inside the workspace file."""
    (tmp_path / "vitest.workspace.ts").write_text(
        "export default [\n"
        "  { test: { name: 'isolated' } },\n"
        "  { test: { name: 'shared', isolate: false } },\n"
        "]\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["isolation_opt_out"] is True


def test_scan_fails_closed_when_there_is_no_vitest_config(tmp_path: Path):
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["readable"] is False
    assert iso["isolation_opt_out"] is False   # never asserted from an absent file


# --- the drill half: the leaf only fires on a corroborated pole --------------

def test_leaf_fires_when_the_pole_is_import_bound_and_isolation_is_on():
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_ON)
    assert leaf is not None and leaf["fix_key"] == "vitest-isolate-pool"
    assert leaf["deeper"][-1]["rows"][0][0].startswith("import")
    # The config fact it read is quoted in the evidence — the finding never
    # asserts "isolation is on" without showing where it read that.
    assert any("vitest.config.ts" in e for e in leaf["evidence"])


def test_leaf_does_not_fire_when_the_repo_already_opted_out():
    assert bp._parse_log(_IMPORT_BOUND_LOG, _ISO_OFF) is None


def test_leaf_does_not_fire_when_the_config_cannot_be_read():
    assert bp._parse_log(_IMPORT_BOUND_LOG, _ISO_UNREADABLE) is None


def test_leaf_does_not_fire_without_a_config_fact_at_all():
    """Fail closed: a log alone cannot establish that isolation is still on."""
    assert bp._parse_log(_IMPORT_BOUND_LOG) is None


def test_leaf_does_not_fire_when_the_run_already_passes_no_isolate():
    log = _IMPORT_BOUND_LOG + "\n$ vitest run --no-isolate\n"
    assert bp._parse_log(log, _ISO_ON) is None


def test_leaf_does_not_fire_when_assertions_dominate_the_imports():
    assert bp._parse_log(_TEST_BOUND_LOG, _ISO_ON) is None


def test_leaf_is_demoted_when_the_suite_is_not_the_poles_dominant_work():
    """A vitest marker in a build-dominated pole's log must not crown the cause."""
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_ON)
    pole = {"dominant_category": "build", "dominant_step": "Build packages"}
    crowned, demoted = bp._demote_offcategory_leaf(leaf, pole)
    assert crowned is None and demoted is not None


# --- the risk axis: HIGH risk, mandatory guardrail, rollout ------------------

def _prompt() -> str:
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_ON)
    pole = {"check": "api-tests", "job": "api-tests", "p50_s": 320.0,
            "workflow_file": ".github/workflows/ci.yml",
            "dominant_category": "test", "dominant_step": "Run vitest"}
    return bp._build_agent_prompt(leaf, pole, [], None, "demo/repo", "abc1234", 4, 4)


def test_prompt_carries_risk_guardrail_and_rollout():
    p = _prompt()
    assert "RISK: HIGH" in p
    assert "GUARDRAIL (MANDATORY)" in p
    assert "ROLLOUT" in p


def test_prompt_prescribes_opt_in_not_a_global_flip():
    p = _prompt().lower()
    assert "opt-in" in p
    assert "teardown" in p
    assert "fake timer" in p                      # stays isolated
    assert "order-dependent" in p                 # the failure mode, named plainly


def test_prompt_keeps_the_no_weakening_rail():
    assert any(line.strip() in _prompt() for line in bp._NO_WEAKENING_LINES)


# --- the catalog entry -------------------------------------------------------

def _catalog_section(pat: str) -> str:
    catalog = (_SKILL_DIR / "references" / "optimization-patterns.md").read_text()
    start = catalog.index(f"### {pat} —")
    nxt = catalog.find("\n### ", start + 1)
    return catalog[start:] if nxt < 0 else catalog[start:nxt]


def test_opt78_is_catalogued_as_a_high_risk_structural_lever():
    body = _catalog_section("OPT78")
    assert "class: structural" in body
    assert "risk: HIGH" in body
    assert "**Guardrail" in body or "**Mandatory guardrail" in body
    assert "ollout" in body


def test_opt78_is_deliberately_uncredited():
    """The realizable saving needs a benchmark, so OPT78 must never carry a
    sizing model that credits a saving the skill did not measure."""
    assert "OPT78" not in cr._SIZING
    assert "benchmark" in _catalog_section("OPT78").lower()


def test_opt78_is_reported_as_having_no_critical_path_router(tmp_path: Path):
    """OPT78 is routed by the drill-time leaf detector, not by the structural
    router in collect_runs — so scan must list it as uncovered by that router
    rather than silently implying coverage."""
    assert "OPT78" in set(
        _scan(tmp_path)["catalog_structural_patterns_without_detector"])

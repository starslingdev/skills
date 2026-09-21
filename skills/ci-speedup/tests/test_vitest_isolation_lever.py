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
           "truncated": False,
           "configs": ["vitest.config.ts"], "opt_out_evidence": []}
_ISO_OFF = {"runner": "vitest", "readable": True, "isolation_opt_out": True,
            "truncated": False, "configs": ["vitest.config.ts"],
            "opt_out_evidence": ["vitest.config.ts:7: isolate: false"]}
_ISO_UNREADABLE = {"runner": None, "readable": False, "isolation_opt_out": False,
                   "truncated": False, "configs": [], "opt_out_evidence": []}


def test_a_bundle_predating_the_truncation_key_fails_closed():
    """A findings.json from a scan that never reported whether its config search
    was complete was produced by a root-only read - its silence is not evidence
    of a complete search, so the lever must not fire off it."""
    legacy = {k: v for k, v in _ISO_ON.items() if k != "truncated"}
    assert bp._parse_log(_IMPORT_BOUND_LOG, legacy) is None


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


def test_scan_finds_a_monorepo_package_config(tmp_path: Path):
    """The suites this lever is about usually live under `packages/*` — a
    root-only read would report "no config" on exactly those repos."""
    pkg = tmp_path / "packages" / "api"
    pkg.mkdir(parents=True)
    (pkg / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["readable"] is True
    assert any("packages/api/vitest.config.ts" in c for c in iso["configs"])


def test_scan_does_not_descend_into_vendored_trees(tmp_path: Path):
    dep = tmp_path / "node_modules" / "some-dep"
    dep.mkdir(parents=True)
    (dep / "vitest.config.ts").write_text("export default {}\n", encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["readable"] is False


def test_scan_treats_an_unresolvable_isolate_value_as_an_opt_out(tmp_path: Path):
    """A config is executable TS/JS: `isolate` can come from an import, a spread
    or a computed expression. Unresolvable is NOT evidence isolation is on."""
    (tmp_path / "vitest.config.ts").write_text(
        "import { shared } from './flags'\n"
        "export default defineConfig({ test: { isolate: shared.isolate } })\n",
        encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["isolation_opt_out"] is True
    assert any("isolate: shared.isolate" in e for e in iso["opt_out_evidence"])


def test_scan_accepts_an_explicit_isolate_true(tmp_path: Path):
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: { isolate: true } })\n", encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["isolation_opt_out"] is False


def test_scan_reads_a_quoted_isolate_key(tmp_path: Path):
    """`vitest.config.json` is an accepted candidate, and JSON ALWAYS quotes the
    key — so a bare-identifier-only match misses the opt-out on exactly the file
    format where it can never be written any other way. TS/JS under
    `quoteProps: "consistent"` writes it the same way."""
    (tmp_path / "vitest.config.json").write_text(
        '{"test": {"isolate": false}}\n', encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["isolation_opt_out"] is True


def test_scan_fails_closed_when_the_config_walk_is_truncated(tmp_path: Path):
    """The finding's claim is "no `isolate: false` ANYWHERE". A walk that hit
    its file cap cannot establish that: the opt-out may sit in a config the walk
    never reached, so the repo that has ALREADY adopted this lever would be told
    to adopt it. "Walk exhausted" and "no config exists" must reach the same
    suppressed outcome - the catalog promises the pattern fails closed."""
    for i in range(45):
        pkg = tmp_path / "packages" / f"pkg{i:02d}"
        pkg.mkdir(parents=True)
        body = ("export default defineConfig({ test: { isolate: false } })\n"
                if i == 44 else
                "export default defineConfig({ test: {} })\n")
        (pkg / "vitest.config.ts").write_text(body, encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["truncated"] is True, "the walk hit its cap and must say so"
    assert bp._parse_log(_IMPORT_BOUND_LOG, iso) is None, (
        "a truncated walk cannot establish 'no opt-out anywhere' - must suppress")


def test_scan_fails_closed_when_a_package_sits_below_the_depth_bound(tmp_path: Path):
    """Same leak by the other bound: `apps/web/packages/x/...` reaches depth 5
    on a real monorepo. An unvisited PACKAGE root (it carries a package.json) is
    exactly where an opt-out config lives, so the walk must call itself
    incomplete rather than report a clean 'no opt-out anywhere'."""
    deep = tmp_path / "apps" / "web" / "packages" / "inner" / "pkg"
    deep.mkdir(parents=True)
    (deep / "package.json").write_text('{"name": "pkg"}\n', encoding="utf-8")
    (deep / "vitest.config.ts").write_text(
        "export default defineConfig({ test: { isolate: false } })\n",
        encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["truncated"] is True
    assert bp._parse_log(_IMPORT_BOUND_LOG, iso) is None


def test_scan_does_not_flag_truncation_on_an_ordinary_repo(tmp_path: Path):
    """The suppression above must not swallow the normal case, or the lever
    never fires at all."""
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["truncated"] is False
    assert bp._parse_log(_IMPORT_BOUND_LOG, iso) is not None


def test_a_deep_source_tree_is_not_truncation(tmp_path: Path):
    """THE failure mode this guard must not have. Every real repo nests source
    directories past the depth bound; none of them can hold a vitest config,
    because a config lives at a package root. Counting them would mark
    essentially every repo truncated and retire the pattern in silence - a
    permanent false negative traded for a rare false positive."""
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    deep = tmp_path / "src" / "components" / "widgets" / "buttons" / "icons"
    deep.mkdir(parents=True)
    (deep / "Icon.tsx").write_text("export const Icon = () => null\n",
                                   encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["truncated"] is False, (
        "an ordinary deep source tree must not suppress the lever")
    assert bp._parse_log(_IMPORT_BOUND_LOG, iso) is not None


def test_a_realistic_monorepo_still_fires(tmp_path: Path):
    """End to end on the shape this lever exists for: several packages, each
    with its own config and its own nested source tree."""
    for name in ("api", "web", "worker"):
        pkg = tmp_path / "packages" / name
        (pkg / "src" / "lib" / "helpers").mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "p"}\n', encoding="utf-8")
        (pkg / "vitest.config.ts").write_text(
            "export default defineConfig({ test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["truncated"] is False
    assert len(iso["configs"]) == 3
    assert bp._parse_log(_IMPORT_BOUND_LOG, iso) is not None


def test_scan_fails_closed_when_there_is_no_vitest_config(tmp_path: Path):
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["readable"] is False
    assert iso["isolation_opt_out"] is False   # never asserted from an absent file


# --- the drill half: the leaf only fires on a corroborated pole --------------

def test_leaf_fires_when_the_pole_is_import_bound_and_isolation_is_on():
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_ON)
    assert leaf is not None and leaf["fix_key"] == "vitest-isolate-pool"
    assert leaf["deeper"][-1]["rows"][0][0].startswith("import")
    # The config fact it read is named in the evidence — the finding never
    # asserts "isolation is on" without showing where it read that.
    assert any("vitest.config.ts" in e for e in leaf["evidence"])


def test_the_config_half_of_the_evidence_declares_it_is_not_log_text():
    """Both render sites label the evidence list "verbatim from the captured
    job log", and the agent prompt additionally wraps it in an UNTRUSTED-content
    fence. The config half is neither: it is a statement this skill composed
    about the ABSENCE of an opt-out, and by construction there is no line to
    quote. It must say so where it is rendered, or the report presents a
    skill-derived assertion as quoted log text the reader can go and find."""
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_ON)
    cfg_line = next(e for e in leaf["evidence"] if "vitest.config.ts" in e)
    assert "not this log" in cfg_line, (
        "the config half must name its provenance where it renders")
    # The measured half stays a real, findable log line.
    assert any(e.lstrip().startswith("Duration ") for e in leaf["evidence"])


@pytest.mark.parametrize("cfgs", [
    ["vitest.config.ts"],                                   # the common shape
    ["packages/api/vitest.config.ts", "vitest.config.ts"],  # a monorepo
])
def test_the_config_evidence_states_the_absence_not_its_opposite(cfgs: list):
    """The finding fires only when NO opt-out was found, so its evidence must
    say exactly that. A sentence reading "<config> sets `isolate: false`" states
    the opposite of the fact that let the finding fire, and lands in the prompt
    that tells the agent to go and change `test.isolate` - self-contradicting
    evidence. Pinned for one config and for several, because the two render
    through different branches."""
    iso = {"runner": "vitest", "readable": True, "truncated": False,
           "isolation_opt_out": False, "configs": cfgs, "opt_out_evidence": []}
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, iso)
    assert leaf is not None
    line = next(e for e in leaf["evidence"] if "not this log" in e)
    assert "isolate: false" in line, "the evidence must name what it looked for"
    # ...and must say it was NOT found.
    assert ("does not set" in line or "none of" in line), (
        f"evidence claims the opt-out IS configured: {line!r}")


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


@pytest.mark.parametrize("flag", ["--no-isolate", "--isolate=false"])
def test_leaf_does_not_fire_for_either_spelling_of_the_cli_opt_out(flag: str):
    """vitest documents BOTH spellings of the CLI opt-out (`--no-isolate` and
    `--isolate=false`). A repo that already applied this exact lever from the
    command line must not be told to apply it."""
    assert bp._parse_log(_IMPORT_BOUND_LOG + f"\n$ vitest run {flag}\n",
                         _ISO_ON) is None


@pytest.mark.parametrize("flag", ["--no-isolate", "--isolate=false"])
def test_scan_reads_either_spelling_of_the_cli_opt_out(tmp_path: Path, flag: str):
    (tmp_path / "vitest.config.ts").write_text(
        f"// CI runs this as `vitest run {flag}`\n"
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["isolation_opt_out"] is True


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


def test_prompt_says_the_ceiling_is_not_this_levers_saving():
    """The pole's addressable ceiling is measured wall, not a benchmarked
    saving for this lever — the prompt must not let the agent read it as one."""
    p = _prompt()
    assert "credits NO saving for this lever" in p
    assert "benchmark" in p.lower()


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


# --- the measurement spine: the gated leaf still gets its cross-run check -----

def _vitest_job(jid: int, dur_s: int) -> dict:
    return {
        "name": "api-tests", "id": jid, "conclusion": "success",
        "html_url": f"https://github.com/o/r/actions/runs/9/job/{jid}",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": f"2026-01-01T00:{dur_s // 60:02d}:{dur_s % 60:02d}Z",
    }


def _import_bound_log(import_s: float) -> str:
    return "\n".join([
        " RUN  v4.1.4 /repo/api",
        " Test Files  149 passed (149)",
        f" Duration  96.12s (transform 8.97s, setup 1.01s, import {import_s}s, "
        "tests 214.54s, environment 8ms)",
    ])


def test_persist_pole_logs_cross_run_checks_the_isolation_levers_import_share(
        tmp_path: Path):
    """The OPT78 leaf is gated on scan's `test_runner_isolation` fact, so the
    MEASUREMENT SPINE must be handed that fact too.

    `_persist_pole_logs` re-parses the pole's log to find the load-bearing
    magnitude and bracket it across runs. Without the fact it reads an
    import-bound vitest pole as matching no detector and silently downgrades
    the finding's load-bearing number - the import share the report crowns - to
    a generic dominant-step wall sample. The prompt still appends "validated
    across runs" (it only checks that SOME cross-run block rendered), so the
    report would claim a cross-run check on a quantity that was never sampled.
    """
    logs = {5: _import_bound_log(245.03), 6: _import_bound_log(245.03),
            7: _import_bound_log(120.0)}

    class FakeClient:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            jid = int(endpoint.split("/jobs/")[1].split("/")[0])
            return logs.get(jid, _import_bound_log(245.03))

    poles = [{"check": "api-tests", "job": "api-tests", "p50_s": 720.0,
              "workflow_file": ".github/workflows/ci.yml"}]
    jpr = {".github/workflows/ci.yml": [
        [_vitest_job(5, 700)], [_vitest_job(6, 720)], [_vitest_job(7, 740)]]}

    manifest = cr._persist_pole_logs(
        FakeClient(), "o/r", poles, jpr, tmp_path, mag_runs=3, iso=_ISO_ON)
    entry = manifest[0]
    assert entry["mag_file"], "the drilled vitest pole must get a magnitude sidecar"
    mag = json.loads((tmp_path / entry["mag_file"]).read_text())
    # The leaf's OWN magnitude (import share, %), not the no-detector fallback.
    assert mag.get("kind") != "step-wall", (
        "the spine fell back to a dominant-step sample - it did not see the leaf")
    assert mag["unit"] == "%"
    assert "import share" in mag["label"]
    # The bracket is built from the leaf's own quantity across runs, and the
    # drilled run is in it - that is what "validated across runs" has to mean.
    assert len(mag["values"]) >= 2
    assert any(v.get("drilled") for v in mag["values"])
    assert mag["this_run"] == pytest.approx(52.3, abs=0.5)


def test_persist_pole_logs_still_falls_back_when_isolation_is_opted_out(
        tmp_path: Path):
    """The mirror: when the config fact suppresses the leaf, the spine must
    agree with the render path and fall back - never size a finding the report
    will not make."""
    class FakeClient:
        def text(self, endpoint: str, allow_missing: bool = False) -> str:
            return _import_bound_log(245.03)

    job = _vitest_job(6, 720)
    job["steps"] = [
        {"name": "Set up job", "started_at": "2026-01-01T00:00:00Z",
         "completed_at": "2026-01-01T00:00:05Z"},
        {"name": "Run vitest", "started_at": "2026-01-01T00:00:05Z",
         "completed_at": "2026-01-01T00:11:00Z"},
    ]
    poles = [{"check": "api-tests", "job": "api-tests", "p50_s": 720.0,
              "workflow_file": ".github/workflows/ci.yml"}]
    jpr = {".github/workflows/ci.yml": [[job]]}
    manifest = cr._persist_pole_logs(
        FakeClient(), "o/r", poles, jpr, tmp_path, mag_runs=3, iso=_ISO_OFF)
    mag_file = manifest[0]["mag_file"]
    assert mag_file
    assert json.loads((tmp_path / mag_file).read_text())["kind"] == "step-wall"

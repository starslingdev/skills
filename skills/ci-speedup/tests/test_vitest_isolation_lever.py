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
import re
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

# EVERY key `scan.py`'s `_iso_block` emits is spelled out here. A fixture that
# omits one exercises the consumer's `.get()` default instead of the real fact -
# which is exactly where a fail-open default hides - so
# `test_the_fixtures_carry_every_key_the_scan_emits` pins these against the
# producer's own shape.
_ISO_BASE = {
    "runner": "vitest",
    "configs": ["vitest.config.ts"],
    "unreadable": [],
    "unresolved_imports": [],
    "isolate_unresolved": [],
    "readable": True,
    "truncated": False,
    "isolation_opt_out": False,
    "opt_out_scope": "repo",
    "opt_out_evidence": [],
    "opt_out_evidence_count": 0,
    "opt_out_configs": [],
    "vm_pool": False,
    "error": None,
    "verdict": "isolation_on",
}
_ISO_ON = dict(_ISO_BASE)
_ISO_OFF = dict(_ISO_BASE, isolation_opt_out=True, verdict="opted_out",
                opt_out_evidence=["vitest.config.ts:7: isolate: false"],
                opt_out_evidence_count=1, opt_out_configs=["vitest.config.ts"])
_ISO_UNREADABLE = dict(_ISO_BASE, runner=None, readable=False, configs=[],
                       verdict="unknown")


def _verdict_from_fields(iso: dict) -> str:
    """The verdict recomputed from the raw fields, INDEPENDENTLY of the producer
    (the repo's mirror idiom): a collapsed field is only trustworthy while it
    still agrees with the facts it collapses."""
    if iso.get("isolation_opt_out"):
        return "opted_out"
    if (iso.get("error") or iso.get("truncated") or iso.get("unreadable")
            or iso.get("unresolved_imports") or iso.get("isolate_unresolved")
            or not iso.get("configs")):
        return "unknown"
    if iso.get("vm_pool"):
        return "not_applicable"
    return "isolation_on"


def _fires(log: str, iso=None) -> bool:
    """Did the OPT78 LEVER fire? A withheld pole still gets a leaf (the guarded
    `vitest-import-bound` one), so "no lever" is not "no leaf"."""
    leaf = bp._parse_log(log, iso)
    return bool(leaf) and leaf["fix_key"] == "vitest-isolate-pool"


def test_a_bundle_predating_the_truncation_key_fails_closed():
    """A findings.json from a scan that never reported whether its config search
    was complete was produced by a root-only read - its silence is not evidence
    of a complete search, so the lever must not fire off it."""
    legacy = {k: v for k, v in _ISO_ON.items() if k != "truncated"}
    assert not _fires(_IMPORT_BOUND_LOG, legacy)


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
    doc = json.loads(result.stdout)
    # EVERY fact this suite ever emits is checked against the independently
    # recomputed verdict, so the collapsed field can never drift from the raw
    # fields a second reader might use instead.
    iso = doc.get("test_runner_isolation")
    if isinstance(iso, dict):
        assert iso.get("verdict") == _verdict_from_fields(iso), iso
    return doc


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
    """vitest 3.x's `vitest.workspace.*` file (removed in vitest 4, still read
    defensively): an opt-out in one of its projects counts."""
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


def test_scan_reports_an_unresolvable_isolate_value_as_unresolved_not_an_opt_out(
        tmp_path: Path):
    """A config is executable TS/JS: `isolate` can come from an import, a spread
    or a computed expression. Unresolvable is NOT evidence isolation is on - and
    it is NOT evidence of an opt-out either. Reporting it as one made the report
    state, as a fact about the repo, "the repo already opts out of per-file
    isolation (first at `vitest.config.ts:2`)" when line 2 reads
    `isolate: shared.isolate`. Two different facts, two different messages, both
    withholding."""
    (tmp_path / "vitest.config.ts").write_text(
        "import { shared } from './flags'\n"
        "export default defineConfig({ test: { isolate: shared.isolate } })\n",
        encoding="utf-8")
    # The imported module IS readable — so the only thing this read cannot
    # resolve is the `isolate` value itself.
    (tmp_path / "flags.ts").write_text("export const shared = {}\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["unresolved_imports"] == [], iso
    assert iso["isolation_opt_out"] is False, iso
    assert iso["opt_out_evidence"] == [], iso
    assert any("isolate: shared.isolate" in e for e in iso["isolate_unresolved"]), iso
    assert iso["verdict"] == "unknown", iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)
    _ok, why = bp._isolation_lever_available(iso, "")
    assert "could not resolve" in why, why
    assert "already opts out" not in why, why


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
    for i in range(405):
        pkg = tmp_path / "packages" / f"pkg{i:03d}"
        pkg.mkdir(parents=True)
        body = ("export default defineConfig({ test: { isolate: false } })\n"
                if i == 404 else
                "export default defineConfig({ test: {} })\n")
        (pkg / "vitest.config.ts").write_text(body, encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["truncated"] is True, "the walk hit its cap and must say so"
    assert not _fires(_IMPORT_BOUND_LOG, iso), (
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
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_scan_does_not_flag_truncation_on_an_ordinary_repo(tmp_path: Path):
    """The suppression above must not swallow the normal case, or the lever
    never fires at all."""
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["truncated"] is False
    assert _fires(_IMPORT_BOUND_LOG, iso)


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
    assert _fires(_IMPORT_BOUND_LOG, iso)


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
    assert _fires(_IMPORT_BOUND_LOG, iso)


def test_scan_fails_closed_when_there_is_no_vitest_config(tmp_path: Path):
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["readable"] is False
    assert iso["isolation_opt_out"] is False   # never asserted from an absent file


# --- the drill half: the leaf only fires on a corroborated pole --------------

def test_leaf_fires_when_the_pole_is_import_bound_and_isolation_is_on():
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_ON)
    assert leaf is not None and leaf["fix_key"] == "vitest-isolate-pool"
    assert leaf["deeper"][-1]["rows"][0][0].startswith("import")
    # The config fact it read is named — the finding never asserts "isolation is
    # on" without showing where it read that. It travels as `config_fact`, NOT in
    # `evidence`, which is log text only.
    assert "vitest.config.ts" in leaf["config_fact"]
    assert not any("vitest.config.ts" in e for e in leaf["evidence"]), leaf["evidence"]


def test_the_config_half_of_the_evidence_declares_it_is_not_log_text():
    """Both render sites label the evidence list "verbatim from the captured
    job log", and the agent prompt additionally wraps it in an UNTRUSTED-content
    fence. The config half is neither: it is a statement this skill composed
    about the ABSENCE of an opt-out, and by construction there is no line to
    quote. It must say so where it is rendered, or the report presents a
    skill-derived assertion as quoted log text the reader can go and find."""
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_ON)
    cfg_line = leaf["config_fact"]
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
    iso = dict(_ISO_BASE, configs=cfgs)
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, iso)
    assert leaf is not None
    line = leaf["config_fact"]
    assert "not this log" in line
    assert "isolate: false" in line, "the evidence must name what it looked for"
    # ...and must say it was NOT found.
    assert ("does not set" in line or "none of" in line), (
        f"evidence claims the opt-out IS configured: {line!r}")


def test_leaf_does_not_fire_when_the_repo_already_opted_out():
    assert not _fires(_IMPORT_BOUND_LOG, _ISO_OFF)


def test_leaf_does_not_fire_when_the_config_cannot_be_read():
    assert not _fires(_IMPORT_BOUND_LOG, _ISO_UNREADABLE)


def test_leaf_does_not_fire_without_a_config_fact_at_all():
    """Fail closed: a log alone cannot establish that isolation is still on."""
    assert not _fires(_IMPORT_BOUND_LOG)


def test_leaf_does_not_fire_when_the_run_already_passes_no_isolate():
    log = _IMPORT_BOUND_LOG + "\n$ vitest run --no-isolate\n"
    assert not _fires(log, _ISO_ON)


@pytest.mark.parametrize("flag", ["--no-isolate", "--isolate=false"])
def test_leaf_does_not_fire_for_either_spelling_of_the_cli_opt_out(flag: str):
    """vitest documents BOTH spellings of the CLI opt-out (`--no-isolate` and
    `--isolate=false`). A repo that already applied this exact lever from the
    command line must not be told to apply it."""
    assert not _fires(_IMPORT_BOUND_LOG + f"\n$ vitest run {flag}\n", _ISO_ON)


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


def test_persist_pole_logs_sizes_the_withheld_leaf_on_its_import_share(
        tmp_path: Path):
    """The mirror: when the config fact withholds the lever, the render path
    still names the measured import-bound split (the guarded withheld leaf), so
    the spine must size THAT quantity too - never a dominant-step wall sample
    the report does not show."""
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
    mag = json.loads((tmp_path / mag_file).read_text())
    assert mag.get("kind") != "step-wall"
    assert "import share" in mag["label"]


# --- review fixes: config discovery must not miss an opt-out -----------------

def _scan_mod():
    """`scan.py` imported in-process under a unique name (other skills ship a
    `scan.py` too), for the walk-level tests that need to fake an OS error."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ci_speedup_scan_opt78",
                                                  _SCAN_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod     # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", [
    "vitest.unit.config.ts",       # a per-suite config beside the default one
    "vitest.config.unit.ts",       # the same, suffix-style
    "vitest.shared.config.ts",     # a shared base pulled in by mergeConfig
    "vitest.shared.ts",            # a base that carries no `.config` at all
    "vitest.base.mts",
    "vite.config.e2e.ts",
])
def test_scan_reads_an_opt_out_in_a_non_default_config_name(tmp_path: Path, name: str):
    """Only the exact default names were read, so an opt-out written in any of
    these was invisible and the repo that already applied the lever was told to
    apply it."""
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    (tmp_path / name).write_text(
        "export default defineConfig({ test: { isolate: false } })\n",
        encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["isolation_opt_out"] is True, iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_scan_follows_a_relative_import_the_config_merges(tmp_path: Path):
    """`mergeConfig(base, ...)` with the base in an arbitrarily named local file:
    the opt-out lives in the imported module, so the read must follow it."""
    (tmp_path / "vitest.config.ts").write_text(
        "import { defineConfig, mergeConfig } from 'vitest/config'\n"
        "import base from './config/test-base'\n"
        "export default mergeConfig(base, defineConfig({ test: {} }))\n",
        encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "test-base.ts").write_text(
        "export default { test: { isolate: false } }\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["isolation_opt_out"] is True, iso


def test_scan_fails_closed_on_a_config_import_it_cannot_follow(tmp_path: Path):
    """A merged base the read cannot reach (a missing file, a shared config
    package) may carry the opt-out. Unfollowable is not evidence isolation is on."""
    (tmp_path / "vitest.config.ts").write_text(
        "import { mergeConfig } from 'vitest/config'\n"
        "import base from '@acme/vitest-config'\n"
        "export default mergeConfig(base, { test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["unresolved_imports"], iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_ordinary_config_imports_do_not_suppress_the_lever(tmp_path: Path):
    """The fail-closed rule above must not fire on the imports every config has,
    or the pattern retires itself."""
    (tmp_path / "vitest.config.ts").write_text(
        "import path from 'node:path'\n"
        "import { defineConfig } from 'vitest/config'\n"
        "import react from '@vitejs/plugin-react'\n"
        "export default defineConfig({ plugins: [react()], test: {} })\n",
        encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["unresolved_imports"] == [], iso
    assert _fires(_IMPORT_BOUND_LOG, iso)


def test_an_unreadable_config_suppresses_the_lever(tmp_path: Path):
    """A broken symlink (or an EACCES config) was listed as unreadable and then
    ignored, so a readable sibling let the lever fire past a config the read
    never saw."""
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    pkg = tmp_path / "packages" / "api"
    pkg.mkdir(parents=True)
    (pkg / "vitest.config.ts").symlink_to(tmp_path / "missing-target.ts")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["unreadable"], iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_an_unreadable_entry_in_the_block_suppresses_the_lever():
    iso = dict(_ISO_ON, unreadable=["packages/api/vitest.config.ts"])
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_scan_reads_an_isolate_value_on_the_next_line(tmp_path: Path):
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({\n  test: {\n    isolate:\n      false,\n  },\n})\n",
        encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["isolation_opt_out"] is True


@pytest.mark.parametrize("body", [
    "const isolate = process.env.CI !== 'true'\n"
    "export default defineConfig({ test: { isolate } })\n",
    "export default defineConfig({ test: { pool: 'forks', isolate,\n } })\n",
    "export default defineConfig({ test: { ['isolate']: flag } })\n",
])
def test_scan_treats_a_shorthand_or_computed_isolate_as_unresolved(tmp_path: Path,
                                                                   body: str):
    """Unreadable either way: it withholds the lever, but it is reported as
    unresolved - never as a resolved opt-out the report would then quote."""
    (tmp_path / "vitest.config.ts").write_text(body, encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["isolate_unresolved"], iso
    assert iso["isolation_opt_out"] is False, iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_an_explicit_isolate_true_on_the_next_line_is_still_isolation_on(tmp_path: Path):
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({\n  test: {\n    isolate:\n      true,\n  },\n})\n",
        encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["isolation_opt_out"] is False


def test_scan_reads_the_cli_opt_out_in_package_json_scripts(tmp_path: Path):
    """The flag most often lives in a package script, not the config or the
    workflow line the log echoes."""
    pkg = tmp_path / "packages" / "api"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(
        '{"name": "api", "scripts": {"test": "vitest run --no-isolate"}}\n',
        encoding="utf-8")
    (pkg / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["isolation_opt_out"] is True, iso
    assert any("package.json" in e for e in iso["opt_out_evidence"])


def test_a_package_one_grouping_level_below_the_depth_bound_is_truncation(tmp_path: Path):
    """The depth bound is pinned: a directory at the bound is checked for a
    package root beside it OR one grouping level beneath it (`<bound>/libs/x/`),
    so a nested workspace package is never read as clean silence."""
    grp = tmp_path / "a" / "b" / "c" / "d" / "libs"
    pkg = grp / "inner"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text('{"name": "inner"}\n', encoding="utf-8")
    (pkg / "vitest.config.ts").write_text(
        "export default defineConfig({ test: { isolate: false } })\n",
        encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["truncated"] is True, iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


@pytest.mark.parametrize("pruned", ["build", "vendor", "target", "out"])
def test_a_workspace_package_under_a_pruned_name_is_truncation(tmp_path: Path,
                                                              pruned: str):
    pkg = tmp_path / pruned
    pkg.mkdir()
    (pkg / "package.json").write_text('{"name": "p"}\n', encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["truncated"] is True


def test_a_build_output_package_json_under_dist_is_not_truncation(tmp_path: Path):
    """`dist/` (and node_modules) routinely carry a copied package.json; they are
    never a workspace, so pruning them must stay silent or the lever retires."""
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "package.json").write_text('{"name": "p"}\n', encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["truncated"] is False


def test_hidden_directories_and_bazel_links_are_not_read_as_the_repo(tmp_path: Path):
    """A local, untracked copy of the repo (an agent worktree under a dot-dir) must
    not decide the verdict for the committed config, and Bazel's `bazel-*` output
    links (a mirror of the workspace, root package.json included) must not mark
    every Bazel repo truncated."""
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    copy = tmp_path / ".claude" / "worktrees" / "agent-x"
    copy.mkdir(parents=True)
    (copy / "vitest.config.ts").write_text(
        "export default defineConfig({ test: { isolate: false } })\n",
        encoding="utf-8")
    mirror = tmp_path / "_bazel_mirror"
    mirror.mkdir()
    (mirror / "package.json").write_text('{"name": "root"}\n', encoding="utf-8")
    (tmp_path / "bazel-bin").symlink_to(mirror, target_is_directory=True)
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["isolation_opt_out"] is False, iso
    assert iso["truncated"] is False, iso
    assert iso["configs"] == ["vitest.config.ts"], iso


def test_an_entry_that_raises_on_stat_is_truncation_not_a_crash(tmp_path: Path,
                                                               monkeypatch):
    """A directory that can be listed but not entered made `is_dir()` raise, and
    nothing caught it: the whole scan (every pattern, not just OPT78) exited 1."""
    scan = _scan_mod()
    (tmp_path / "vitest.config.ts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "locked").mkdir()
    real = Path.is_dir

    def is_dir(self, *a, **k):
        if self.name == "locked":
            raise PermissionError("denied")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "is_dir", is_dir)
    files, truncated, _manifests = scan._vitest_config_files(tmp_path)
    assert truncated is True
    assert [f.name for f in files] == ["vitest.config.ts"]


def test_the_scan_survives_a_config_reader_crash(tmp_path: Path, monkeypatch):
    """Belt and braces: whatever the isolation reader hits, the rest of the scan
    must still run, and the block must fail closed."""
    scan = _scan_mod()

    def boom(_root):
        raise PermissionError("denied")
    monkeypatch.setattr(scan, "_vitest_config_files", boom)
    iso = scan._read_test_runner_isolation(tmp_path)
    assert iso["truncated"] is True
    assert not _fires(_IMPORT_BOUND_LOG, iso)


# --- review fixes: the evidence quotes the run that was sized -----------------

_TWO_RUN_LOG = "\n".join([
    " RUN  v4.1.4 /repo/utils",
    " Test Files  3 passed (3)",
    " Duration  4.02s (transform 0.50s, setup 0ms, import 1.50s, tests 3.20s)",
    " RUN  v4.1.4 /repo/api",
    " Test Files  149 passed (149)",
    " Duration  96.12s (transform 8.97s, setup 1.01s, import 245.03s, "
    "tests 214.54s, environment 8ms)",
])


def test_evidence_quotes_the_slowest_run_it_sized_not_the_first_in_the_log():
    """The detector sizes the slowest run, but quoted the first Duration line in
    log order - a small run whose tests outweigh its imports, contradicting the
    claim the finding makes."""
    leaf = bp._parse_log(_TWO_RUN_LOG, _ISO_ON)
    assert leaf is not None
    dur = [e for e in leaf["evidence"] if e.startswith("Duration")]
    assert dur and "import 245.03s" in dur[0] and len(dur) == 1, leaf["evidence"]
    files = [e for e in leaf["evidence"] if e.startswith("Test Files")]
    assert files == ["Test Files  149 passed (149)"], leaf["evidence"]


# --- review fixes: a withheld lever is named, and is not a new-detector gap ----

def _gap_doc_with(iso):
    doc = {
        "repo": "o/r",
        "pr_critical_path": {"poles": [{
            "check": "tests-web", "job": "tests-web", "p50_s": 255.0,
            "workflow_file": ".github/workflows/pipeline.yml",
            "dominant_step": "run tests", "dominant_category": "test"}]},
    }
    if iso is not None:
        doc["test_runner_isolation"] = iso
    return doc


def test_a_withheld_isolation_lever_is_not_captured_as_a_new_detector_gap():
    """The log DID match the catalog's OPT78 shape; only its config gate failed.
    Capturing it as a gap sends the maintainer loop off to draft a duplicate
    detector for a pattern the catalog already has."""
    assert bp._gap_poles(_gap_doc_with(_ISO_OFF), {"tests-web": _IMPORT_BOUND_LOG}) == []


def test_a_genuinely_unmatched_log_is_still_a_gap():
    gaps = bp._gap_poles(_gap_doc_with(_ISO_OFF), {"tests-web": "nothing to see\n"})
    assert len(gaps) == 1


def test_a_fired_isolation_leaf_is_not_a_gap():
    assert bp._gap_poles(_gap_doc_with(_ISO_ON), {"tests-web": _IMPORT_BOUND_LOG}) == []


@pytest.mark.parametrize("iso,why", [
    (_ISO_OFF, "already opts out"),
    (dict(_ISO_ON, truncated=True), "could not cover"),
    (_ISO_UNREADABLE, "no vitest config"),
    (None, "no vitest config"),
])
def test_the_report_says_the_isolation_lever_was_withheld_and_why(iso, why):
    doc = {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": "tests-web", "p50_s": 255.0,
                "workflow_file": ".github/workflows/pipeline.yml", "job": "tests-web",
                "dominant_step": "run tests", "dominant_p50_s": 91.0,
                "steps": [{"step": "run tests", "category": "test", "p50_s": 91.0}],
            }]},
    }
    if iso is not None:
        doc["test_runner_isolation"] = iso
    md = bp.render(doc, {"pipeline": _IMPORT_BOUND_LOG}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/123"},
                   "2026-06-08")
    line = next((ln for ln in md.splitlines()
                 if "OPT78" in ln and "withheld" in ln), None)
    assert line is not None, "the report never says the isolation lever was withheld"
    assert why in line, line


def test_a_withheld_pole_gets_a_guarded_import_bound_leaf_not_silence():
    """Withholding the lever is not withholding the measurement: the pole keeps a
    catalog leaf that names the split, says OPT78 was withheld and why, and
    forbids flipping isolation - instead of dead-ending into an unguarded LLM
    gap-fill free to prescribe exactly that."""
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_OFF)
    assert leaf is not None and leaf["fix_key"] == "vitest-import-bound"
    assert leaf["magnitude"]["label"] == "import share of the vitest run"
    assert "OPT78 withheld" in leaf["config_fact"], leaf["config_fact"]
    assert not any("OPT78" in e for e in leaf["evidence"]), leaf["evidence"]
    pole = {"check": "api-tests", "job": "api-tests", "p50_s": 320.0,
            "workflow_file": ".github/workflows/ci.yml",
            "dominant_category": "test", "dominant_step": "Run vitest"}
    p = bp._build_agent_prompt(leaf, pole, [], None, "demo/repo", "abc1234", 4, 4)
    assert "Do NOT turn per-file isolation off" in p
    assert "OPT78 withheld" in p
    assert "RISK: HIGH" not in p and "GUARDRAIL (MANDATORY)" not in p


def test_a_crowned_lever_stamps_its_risk_on_the_drill_down():
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_ON)
    assert "OPT78, HIGH RISK" in leaf["deeper"][-1]["blocker_note"]


def test_a_demoted_isolation_lever_is_not_framed_as_a_quick_cleanup():
    leaf = bp._parse_log(_IMPORT_BOUND_LOG, _ISO_ON)
    pole = {"dominant_category": "build", "dominant_step": "Build packages"}
    note = "\n".join(bp._offcategory_note_block(leaf, pole))
    assert "smaller, separate cleanup" not in note
    assert "HIGH-risk" in note and "OPT78" in note


def test_a_vm_pool_withholds_the_lever(tmp_path: Path):
    """`isolate` has no effect on the vm pools, so the opt-in-project recipe
    would do nothing there."""
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: { pool: 'vmThreads' } })\n",
        encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["vm_pool"] is True
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_a_space_separated_isolate_false_is_not_the_opt_out():
    """vitest's `--isolate` takes no value; only `--no-isolate` and
    `--isolate=false` are documented opt-outs."""
    assert _fires(_IMPORT_BOUND_LOG + "\n$ vitest run --isolate false\n", _ISO_ON)


def test_scan_reads_a_3x_single_fork_as_an_opt_out(tmp_path: Path):
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: { poolOptions: { forks: "
        "{ singleFork: true } } } })\n", encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["isolation_opt_out"] is True


def test_a_symlinked_workspace_package_is_truncation(tmp_path: Path):
    real = tmp_path / "_real_pkg"
    real.mkdir()
    (real / "package.json").write_text('{"name": "p"}\n', encoding="utf-8")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "linked").symlink_to(real, target_is_directory=True)
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    assert _scan(tmp_path)["test_runner_isolation"]["truncated"] is True


def test_an_unlistable_directory_is_truncation(tmp_path: Path):
    import os
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    locked = tmp_path / "packages"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running with privileges that ignore directory modes")
        assert _scan(tmp_path)["test_runner_isolation"]["truncated"] is True
    finally:
        os.chmod(locked, 0o755)


def test_a_readable_block_with_no_config_names_fails_closed_not_crashes():
    iso = dict(_ISO_ON, configs=[])
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_render_without_a_config_fact_never_crowns_the_high_risk_lever():
    doc = {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": "tests-web", "p50_s": 255.0,
                "workflow_file": ".github/workflows/pipeline.yml", "job": "tests-web",
                "dominant_step": "run tests", "dominant_p50_s": 91.0,
                "steps": [{"step": "run tests", "category": "test", "p50_s": 91.0}],
            }]},
    }
    md = bp.render(doc, {"pipeline": _IMPORT_BOUND_LOG}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/123"},
                   "2026-06-08")
    assert "RISK: HIGH" not in md
    assert "no drill-down available" not in md


def test_the_config_half_of_the_evidence_reaches_the_agent_prompt():
    """Both halves of the claim must reach the agent, not just the leaf dict."""
    p = _prompt()
    assert "does not set `isolate: false`" in p
    assert "not this log" in p


# --- round 2: what the config IMPORTS, and what the read can honestly claim ----
#
# A bare specifier (a package) is one this read cannot follow. Whether that
# matters is decided by HOW THE CONFIG USES IT, not by its name — a name test
# was wrong in both directions at once.

def test_a_shared_config_base_the_read_cannot_follow_withholds_the_lever(
        tmp_path: Path):
    """The opt-out can live in a package whose name says nothing about tests
    (`@acme/tooling`). Dropping unknown specifiers on the floor let the read
    report itself COMPLETE and told a repo that had already opted out to opt out
    again — the one outcome this HIGH-risk lever must never produce."""
    (tmp_path / "vitest.config.ts").write_text(
        "import { mergeConfig } from 'vitest/config'\n"
        "import base from '@acme/tooling'\n"
        "export default mergeConfig(base, { test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert any("@acme/tooling" in u for u in iso["unresolved_imports"]), iso
    assert iso["verdict"] == "unknown", iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_a_bare_config_re_exported_whole_withholds_the_lever(tmp_path: Path):
    """`export default shared` IS the config: whatever the package sets, the repo
    gets — including `isolate: false`."""
    (tmp_path / "vitest.config.ts").write_text(
        "import shared from '@repo/vitest-config'\n"
        "export default shared\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert any("@repo/vitest-config" in u for u in iso["unresolved_imports"]), iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


@pytest.mark.parametrize("body", [
    # A test ENVIRONMENT — its name says "vitest", it cannot carry the opt-out.
    "import nuxt from 'vitest-environment-nuxt'\n"
    "export default defineConfig({ plugins: [nuxt()], test: {} })\n",
    # A side-effect import: nothing is bound, so nothing can be merged.
    "import '@cloudflare/vitest-pool-workers/config'\n"
    "export default defineConfig({ test: {} })\n",
    # `dotenv/config` — a subpath whose last segment is literally "config".
    "import 'dotenv/config'\n"
    "export default defineConfig({ test: {} })\n",
    # A Storybook test runner used as a plugin.
    "import { storybookTest } from '@storybook/test-runner'\n"
    "export default defineConfig({ plugins: [storybookTest()], test: {} })\n",
])
def test_a_package_that_is_not_a_config_base_does_not_withhold(tmp_path: Path,
                                                               body: str):
    """Cloudflare Workers, Nuxt and Storybook repos are a large slice of the
    vitest population. A name-token test withheld the lever from every one of
    them, forever — a permanent false silence, which is the worse failure."""
    (tmp_path / "vitest.config.ts").write_text(body, encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["unresolved_imports"] == [], iso
    assert _fires(_IMPORT_BOUND_LOG, iso), iso


def test_a_config_base_named_by_extends_withholds_the_lever(tmp_path: Path):
    """A project's string `extends` IS its base, no binding involved."""
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: { projects: ["
        "{ extends: '@acme/base-config' }] } })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert any("@acme/base-config" in u for u in iso["unresolved_imports"]), iso


# --- round 2: bytes the read cannot decode are UNREAD, not clean --------------

@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_a_config_the_read_cannot_decode_is_unreadable_not_clean(tmp_path: Path,
                                                                 encoding: str):
    """Decoding a UTF-16 config with `errors="replace"` produced garbage that
    matched no pattern — and the read then reported itself COMPLETE with no
    opt-out. A repo that had opted out in that very file was told to opt out
    again."""
    (tmp_path / "vitest.config.ts").write_bytes(
        "export default defineConfig({ test: { isolate: false } })\n".encode(encoding))
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["unreadable"] == ["vitest.config.ts"], iso
    assert iso["configs"] == [], iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


# --- round 2: a crashed reader says so ---------------------------------------

def test_a_reader_crash_is_named_as_a_crash_not_a_big_monorepo(tmp_path: Path,
                                                               monkeypatch,
                                                               capsys):
    """A broken reader and a huge monorepo both withhold the lever. Rendering
    them identically (and reading nothing back) retires the pattern silently, so
    the crash prints to stderr AND gets its own withheld reason."""
    scan = _scan_mod()

    def boom(_root):
        raise PermissionError("denied")
    monkeypatch.setattr(scan, "_vitest_config_files", boom)
    iso = scan._read_test_runner_isolation(tmp_path)
    err = capsys.readouterr().err
    assert "PermissionError" in err and "OPT78" in err, err
    assert iso["error"] == "PermissionError", iso
    assert iso["verdict"] == "unknown", iso
    ok, why = bp._isolation_lever_available(iso, "")
    assert not ok
    assert "config reader failed" in why and "PermissionError" in why, why
    assert "could not cover the whole repo" not in why, why


# --- round 2: the collapsed verdict ------------------------------------------

def test_the_fixtures_carry_every_key_the_scan_emits():
    """A fixture missing a producer key silently tests the consumer's `.get()`
    default instead of the fact."""
    scan = _scan_mod()
    emitted = set(scan._iso_block().keys())
    for name, fixture in (("_ISO_ON", _ISO_ON), ("_ISO_OFF", _ISO_OFF),
                          ("_ISO_UNREADABLE", _ISO_UNREADABLE)):
        assert set(fixture) == emitted, (name, emitted ^ set(fixture))


def test_the_verdict_mirrors_the_raw_fields_the_producer_collapsed():
    """The producer's own collapse, checked against an independent recompute."""
    scan = _scan_mod()
    for block in (
        scan._iso_block(),
        scan._iso_block(configs=["vitest.config.ts"], readable=True, truncated=False),
        scan._iso_block(configs=["a"], truncated=False, isolation_opt_out=True),
        scan._iso_block(configs=["a"], truncated=False, vm_pool=True),
        scan._iso_block(configs=["a"], truncated=False, isolate_unresolved=["a:1: x"]),
        scan._iso_block(configs=["a"], truncated=False, unresolved_imports=["a: @x/y"]),
        scan._iso_block(configs=["a"], truncated=False, error="PermissionError"),
    ):
        block["verdict"] = scan._isolation_verdict(block)
        assert block["verdict"] == _verdict_from_fields(block), block


def test_a_fact_whose_verdict_is_not_isolation_on_withholds_the_lever():
    """The four per-reason `.get()` checks all fail OPEN if the producer renames a
    key. The collapsed verdict defaults to `unknown`, so the lever does not."""
    renamed = {k: v for k, v in _ISO_ON.items() if k != "verdict"}
    assert not _fires(_IMPORT_BOUND_LOG, renamed)
    assert not _fires(_IMPORT_BOUND_LOG, dict(_ISO_ON, verdict="not_applicable"))


def test_a_package_script_opt_out_with_no_config_file_still_withholds(tmp_path: Path):
    """The contradictory state: nothing the walk calls a config, but a package
    script passes `--no-isolate`. The repo HAS opted out — it must be told so,
    not told "no config was found"."""
    (tmp_path / "package.json").write_text(
        '{"name": "app", "scripts": {"test": "vitest run --no-isolate"}}\n',
        encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["configs"] == [], iso
    assert iso["isolation_opt_out"] is True, iso
    assert iso["verdict"] == "opted_out", iso
    ok, why = bp._isolation_lever_available(iso, "")
    assert not ok
    assert "already opts out" in why, why


# --- round 2: the fact actually reaches the measurement spine ----------------

def test_collect_forwards_the_config_fact_to_the_measurement_spine(tmp_path: Path,
                                                                   monkeypatch):
    """Executed, not read: `collect()` must hand scan's config fact to the
    pole-log pass, or the spine's leaf detection disagrees with the renderer's
    (a config-gated leaf is invisible without it). Passing `iso=None` there left
    every other test in this suite green."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ci_speedup_opt78_collect_fakes",
        _SKILL_DIR / "tests" / "test_disclosure_reaches_the_artifact.py")
    fakes = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fakes
    spec.loader.exec_module(fakes)

    seen: dict = {}

    def _capture(*_a, **kw):
        seen["iso"] = kw.get("iso", "NOT PASSED")
        return {}

    monkeypatch.setattr(cr, "GhClient", fakes._TwoWorkflowClient)
    monkeypatch.setattr(cr, "_persist_pole_logs", _capture)
    doc = fakes._doc()
    doc["test_runner_isolation"] = _ISO_ON
    cr.collect(doc, "o/r", max_runs=8, shallow_runs=8, with_logs=True,
               data_dir=tmp_path / "bundle")
    assert seen.get("iso") == _ISO_ON, seen


def test_every_engine_parse_log_call_passes_the_config_fact():
    """Grep-shaped guard: a `_parse_log(log)` anywhere in the engine silently
    withholds OPT78 on that path (the fact defaults to absent), and no assertion
    anywhere notices. Cheaper than making the parameter required."""
    offenders = []
    for path in sorted((_SKILL_DIR / "scripts").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"(?<!def )\b_parse_log\s*\(", src):
            depth, args, cur, i = 0, [], "", m.end()
            while i < len(src):
                ch = src[i]
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    if depth == 0:
                        break
                    depth -= 1
                elif ch == "," and depth == 0:
                    args.append(cur.strip())
                    cur = ""
                    i += 1
                    continue
                cur += ch
                i += 1
            if cur.strip():
                args.append(cur.strip())
            if len(args) < 2:
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}: _parse_log({', '.join(args)})")
    assert not offenders, (
        "these `_parse_log` calls drop the scanned config fact, which silently "
        f"withholds the OPT78 lever on that path: {offenders}")


# --- round 2: the report, the prompt, and the gap capture --------------------

def _pole_doc(iso=None) -> dict:
    """The one-pole findings doc the render tests share."""
    doc = {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": "tests-web", "p50_s": 255.0,
                "workflow_file": ".github/workflows/pipeline.yml", "job": "tests-web",
                "dominant_step": "run tests", "dominant_p50_s": 91.0,
                "steps": [{"step": "run tests", "category": "test", "p50_s": 91.0}],
            }]},
    }
    if iso is not None:
        doc["test_runner_isolation"] = iso
    return doc


def _render(iso=None) -> str:
    return bp.render(_pole_doc(iso), {"pipeline": _IMPORT_BOUND_LOG}, {},
                     {"pipeline": "https://github.com/o/r/actions/runs/123"},
                     "2026-06-08")


def test_the_report_crowns_the_lever_with_its_risk_when_the_fact_confirms_it():
    """The positive twin of `test_render_without_a_config_fact_never_crowns_...`:
    when the config fact DOES confirm isolation is on, the reader must see the
    HIGH-risk framing — on the biggest-lever line itself, not only further down
    the section — and the provenance of the half that is not log text."""
    md = _render(_ISO_ON)
    lever = next(ln for ln in md.splitlines() if "BIGGEST LEVER" in ln)
    assert "OPT78" in lever and "HIGH RISK" in lever, lever
    assert "RISK: HIGH" in md
    assert "GUARDRAIL (MANDATORY)" in md
    assert "not this log" in md


def test_the_config_fact_is_rendered_outside_the_untrusted_log_block():
    """Skill-authored text inside the BEGIN/END UNTRUSTED LOG markers — and under
    a heading that calls the block verbatim run output — presents a composed
    statement as quoted log the agent could go and find."""
    for text in (_render(_ISO_ON), _prompt()):
        begin = text.index("BEGIN UNTRUSTED LOG CONTENT")
        end = text.index("END UNTRUSTED LOG CONTENT")
        assert "isolate: false" not in text[begin:end], text[begin:end]
        assert "read from the repo's vitest config" in text[end:], text[end:end + 600]


def test_a_withheld_isolation_lever_is_not_a_gap_on_the_bundle_path():
    """`_gap_poles` has two binding paths and only the fallback was covered. The
    bundle path is the one a real drill takes."""
    doc = _gap_doc_with(_ISO_OFF)
    doc["data_bundle"] = {"logs": [{
        "check": "tests-web", "job": "tests-web",
        "workflow_file": ".github/workflows/pipeline.yml",
        "html_url": "https://github.com/o/r/actions/runs/1"}]}
    try:
        from summary import _render_keys
        keys = _render_keys(doc["data_bundle"]["logs"])
    except Exception:                                   # pragma: no cover
        pytest.skip("summary._render_keys unavailable")
    assert bp._gap_poles(doc, {keys[0]: _IMPORT_BOUND_LOG}) == []
    assert len(bp._gap_poles(doc, {keys[0]: "nothing to see\n"})) == 1


@pytest.mark.parametrize("body", [
    '{"name": "api", "scripts": {"test": "vitest run --pool=vmThreads"}}\n',
    '{"name": "api", "scripts": {"test": "vitest run --pool vmForks"}}\n',
])
def test_a_vm_pool_named_in_a_package_script_withholds_the_lever(tmp_path: Path,
                                                                 body: str):
    (tmp_path / "package.json").write_text(body, encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        "export default defineConfig({ test: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["vm_pool"] is True, iso
    assert iso["verdict"] == "not_applicable", iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_a_pure_vite_app_is_not_reported_as_a_vitest_repo(tmp_path: Path):
    """`vite.config.ts` is read (it can hold `test: {}`), but a repo with no
    vitest anywhere is not a vitest repo and must not be labelled one."""
    (tmp_path / "vite.config.ts").write_text(
        "import { defineConfig } from 'vite'\n"
        "export default defineConfig({ build: {} })\n", encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["configs"] == ["vite.config.ts"], iso
    assert iso["runner"] != "vitest", iso


def test_a_config_under_a_dot_config_directory_is_found(tmp_path: Path):
    """`.config/vitest.config.ts` is a conventional home for it. Pruning every
    dot-directory asserted "no config exists" about a repo that has one."""
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "vitest.config.ts").write_text(
        "export default defineConfig({ test: { isolate: false } })\n",
        encoding="utf-8")
    iso = _scan(tmp_path)["test_runner_isolation"]
    assert iso["isolation_opt_out"] is True, iso
    assert not _fires(_IMPORT_BOUND_LOG, iso)


def test_the_missing_fact_message_says_how_to_restore_it():
    _ok, why = bp._isolation_lever_available(None, "")
    assert "re-run the scan" in why, why


def test_the_test_files_line_is_never_borrowed_from_another_project():
    """A red or flaky drill prints `Test Files  1 failed | 148 passed (149)`,
    which the `passed`-only scan skipped — so the search walked up into the
    PREVIOUS project's block and paired a 149-file run with its `12 passed`."""
    log = "\n".join([
        " RUN  v4.1.4 /repo/utils",
        " Test Files  12 passed (12)",
        " Duration  4.02s (transform 0.50s, setup 0ms, import 1.50s, tests 3.20s)",
        " RUN  v4.1.4 /repo/api",
        " Test Files  1 failed | 148 passed (149)",
        " Duration  96.12s (transform 8.97s, setup 1.01s, import 245.03s, "
        "tests 214.54s, environment 8ms)",
    ])
    leaf = bp._parse_log(log, _ISO_ON)
    files = [e for e in leaf["evidence"] if e.startswith("Test Files")]
    assert files == ["Test Files  1 failed | 148 passed (149)"], leaf["evidence"]
    # ...and a run with NO summary of its own quotes nobody else's.
    no_summary = "\n".join([
        " RUN  v4.1.4 /repo/utils",
        " Test Files  12 passed (12)",
        " Duration  4.02s (transform 0.50s, setup 0ms, import 1.50s, tests 3.20s)",
        " RUN  v4.1.4 /repo/api",
        " Duration  96.12s (transform 8.97s, setup 1.01s, import 245.03s, "
        "tests 214.54s, environment 8ms)",
    ])
    leaf2 = bp._parse_log(no_summary, _ISO_ON)
    assert not [e for e in leaf2["evidence"] if e.startswith("Test Files")], leaf2

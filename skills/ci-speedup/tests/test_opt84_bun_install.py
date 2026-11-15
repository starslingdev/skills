"""OPT84's install row: an npm / pnpm / Yarn install that is a heavy share of a
drilled long pole, and `bun install` as its native replacement.

Same evidence bar as the rest of OPT84: routed from the pole's measured step
decomposition, uncredited (the step's p50 is a ceiling), and fail-closed on
anything the read cannot see. Unlike the lint rows it has a share-of-pole floor
(`_OPT84_INSTALL_MIN_SHARE`): a quick install on a long pole is not the lever.
A caveat that clearly blocks the swap (Yarn Plug'n'Play, a pnpm `.pnpmfile.cjs`
hook) is the `install_swap_blocked` verdict; a repo already on bun is
`target_already_in_use`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))
sys.path.insert(0, str(_SKILL_DIR / "tests"))
import blocking_path as bp  # noqa: E402
import collect_runs as cr  # noqa: E402
import scan  # noqa: E402
import verify_report as vr  # noqa: E402

_WF = ".github/workflows/ci.yml"
_STEP = "Install dependencies"


def _pm(**over):
    facts = {"lockfiles": ["pnpm-lock.yaml"], "package_manager_field": None,
             "pnpm_workspace": False, "pnpmfile": [], "yarn_pnp": False,
             "unreadable": []}
    facts.update(over)
    return facts


def _block(pm="default", scripts=None):
    return scan._tal_block(
        configs=[], truncated=False,
        package_scripts={"": {}} if scripts is None else scripts,
        package_manager=_pm() if pm == "default" else pm)


def _doc(run, runs_on="ubuntu-latest"):
    return {"name": "CI", "on": "pull_request", "jobs": {"test": {
        "name": "Test", "runs-on": runs_on,
        "steps": [{"uses": "actions/checkout@v4"},
                  {"name": _STEP, "run": run},
                  {"name": "Run tests", "run": "pnpm test"}]}}}


def _pole(step_p50=90.0, job_p50=240.0, category="install"):
    return {"check": "Test", "workflow_file": _WF, "job": "Test", "job_p50_s": job_p50,
            "steps": [{"step": "Set up job", "category": "setup", "p50_s": 5.0},
                      {"step": _STEP, "category": category, "p50_s": step_p50},
                      {"step": "Run tests", "category": "test",
                       "p50_s": round(job_p50 - step_p50 - 5.0, 1)}]}


def _run(run, *, pole=None, block=None, runs_on="ubuntu-latest"):
    withheld: dict[str, int] = {}
    cands: list[dict] = []
    out = cr._detect_opt84_tool_swap(
        [pole or _pole()], {_WF: _doc(run, runs_on)},
        _block() if block is None else block, [], 10,
        withheld=withheld, withheld_candidates=cands)
    return out, withheld, cands


def _install(f):
    return next(t for t in f["tool_swap"]["tools"] if t["row"] == "install")


# ---- fires on a heavy install ------------------------------------------------

@pytest.mark.parametrize("run,lock", [
    ("pnpm install --frozen-lockfile", "pnpm-lock.yaml"),
    ("npm ci", "package-lock.json"),
    ("yarn install --immutable", "yarn.lock"),
    ("yarn --frozen-lockfile", "yarn.lock"),
])
def test_fires_on_a_long_pole_install(run, lock):
    out, withheld, cands = _run(run, block=_block(_pm(lockfiles=[lock])))
    assert len(out) == 1, (withheld, cands)
    f = out[0]
    ts = f["tool_swap"]
    assert ts["category"] == "install" and ts["ceiling_s"] == 90.0
    assert f["sizing_basis"] == "uncredited" and f["wall_clock_p50_s"] is None
    t = _install(f)
    assert t["target"] == "bun install"
    assert t["fit"] == "dual-run"
    assert t["leaf_command"] in ts["resolved_leaves"]
    words = " ".join(t["fit_reasons"])
    assert lock in words and "bun.lock" in words
    assert "trustedDependencies" in words
    assert "Node" in words                       # installer only, runtime stays
    assert t["docs_url"] == "https://bun.com/docs/pm/cli/install"
    assert t["migration_docs_url"] == "https://bun.com/docs/pm/lockfile"
    assert t["benchmark_commands"][1].endswith("time bun install)")
    assert f["risk"] == "MEDIUM"
    assert withheld == {} and cands == []


def test_windows_runner_and_pnpm_workspace_add_their_caveats():
    out, _w, _c = _run("pnpm install", runs_on="windows-latest",
                       block=_block(_pm(pnpm_workspace=True)))
    words = " ".join(_install(out[0])["fit_reasons"])
    assert "Windows" in words
    assert "catalog" in words and "workspaces" in words


def test_a_build_step_that_installs_first_reports_the_install_row():
    out, _w, _c = _run("pnpm install && pnpm build",
                       pole=_pole(category="build"),
                       block=_block(scripts={"": {"build": "vite build"}}))
    assert [t["row"] for t in out[0]["tool_swap"]["tools"]] == ["install"]


# ---- silent when the install is a trivial share ---------------------------------

def test_silent_when_the_install_is_a_trivial_share_of_the_pole():
    # 25s clears the 20s floor but is about 6% of a 400s pole.
    out, withheld, cands = _run("pnpm install --frozen-lockfile",
                                pole=_pole(step_p50=25.0, job_p50=400.0))
    assert out == [] and cands == []
    assert withheld == {"no_candidate_step": 1}


def test_a_trivial_install_inside_a_build_step_is_not_the_lever():
    out, withheld, _c = _run("pnpm install && pnpm build",
                             pole=_pole(step_p50=25.0, job_p50=400.0, category="build"),
                             block=_block(scripts={"": {"build": "vite build"}}))
    assert out == [] and withheld == {"tool_not_in_table": 1}


def test_a_global_tool_install_is_not_a_dependency_install():
    out, withheld, _c = _run("npm install -g pnpm@9")
    assert out == [] and withheld == {"tool_not_in_table": 1}


# ---- withheld on a blocking caveat ----------------------------------------------

@pytest.mark.parametrize("facts", [
    _pm(lockfiles=["yarn.lock"], yarn_pnp=True),
    _pm(pnpmfile=[".pnpmfile.cjs"]),
])
def test_withholds_on_a_caveat_that_blocks_the_swap(facts):
    out, withheld, cands = _run("yarn install --immutable", block=_block(facts))
    assert out == [] and cands == []
    assert withheld == {"install_swap_blocked": 1}


@pytest.mark.parametrize("pm", [None, _pm(unreadable=[".yarnrc.yml"])])
def test_holds_back_when_the_package_manager_files_were_not_read(pm):
    out, withheld, cands = _run("pnpm install", block=_block(pm))
    assert out == []
    assert withheld == {"install_layout_unreadable": 1}
    assert cands == [{"workflow_file": _WF, "job": "Test",
                      "gate": "install_layout_unreadable", "step": _STEP}]


# ---- silent when the repo already uses bun --------------------------------------

@pytest.mark.parametrize("run,facts", [
    ("bun install --frozen-lockfile", _pm(lockfiles=["bun.lock"])),
    ("pnpm install", _pm(lockfiles=["bun.lock", "pnpm-lock.yaml"])),
    ("pnpm install", _pm(lockfiles=["bun.lockb"])),
    ("pnpm install", _pm(package_manager_field="bun@1.3.0")),
])
def test_silent_when_the_repo_already_uses_bun(run, facts):
    out, withheld, cands = _run(run, block=_block(facts))
    assert out == [] and cands == []
    assert withheld == {"target_already_in_use": 1}


# ---- the scan reads the package-manager facts ------------------------------------

def test_scan_reads_lockfiles_pnpmfile_and_yarn_pnp(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "packageManager": "yarn@4.5.0"}), encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    (tmp_path / ".yarnrc.yml").write_text("enableTelemetry: false\n", encoding="utf-8")
    (tmp_path / ".pnpmfile.cjs").write_text("module.exports = {}\n", encoding="utf-8")
    pm = scan._read_type_aware_lint(tmp_path)["package_manager"]
    assert pm["lockfiles"] == ["yarn.lock"]
    assert pm["package_manager_field"] == "yarn@4.5.0"
    assert pm["yarn_pnp"] is True              # Yarn's default nodeLinker is pnp
    assert pm["pnpmfile"] == [".pnpmfile.cjs"]
    assert pm["unreadable"] == []


def test_scan_reads_a_node_modules_linker_as_not_pnp(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".yarnrc.yml").write_text("nodeLinker: node-modules\n", encoding="utf-8")
    (tmp_path / "pnpm-workspace.yaml").write_text("packages: ['a/*']\n", encoding="utf-8")
    pm = scan._read_type_aware_lint(tmp_path)["package_manager"]
    assert pm["yarn_pnp"] is False and pm["pnpm_workspace"] is True


def test_scan_marks_an_unparsable_yarnrc_unreadable(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".yarnrc.yml").write_text("nodeLinker: [unclosed\n", encoding="utf-8")
    pm = scan._read_type_aware_lint(tmp_path)["package_manager"]
    assert pm["unreadable"] == [".yarnrc.yml"]


# ---- the card and the verifier ---------------------------------------------------

def _findings_doc(f, pm=None):
    return {"repo": "o/r", "type_aware_lint": _block(pm or _pm()),
            "findings": [f],
            "pr_critical_path": {"poles": [_pole()]}}


def _rendered():
    out, _w, _c = _run("pnpm install --frozen-lockfile")
    f = out[0]
    return f, "\n".join(bp._opt84_card(f, "https://x/catalog.md"))


def test_card_names_bun_install_and_the_side_by_side_recipe():
    _f, card = _rendered()
    flat = " ".join(card.split())
    assert "- **npm / pnpm / Yarn install → `bun install`:** runs " \
           "`pnpm install --frozen-lockfile`" in flat
    assert " ".join(bp._OPT84_INSTALL_RECIPE.split()) in flat
    assert "published range 5-21× in CI (vendor-stated)" in flat


def test_verifier_passes_the_install_card(tmp_path):
    f, card = _rendered()
    report = "## 🧰 Tool swaps on other long poles\n\n" + card
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(_findings_doc(f)), encoding="utf-8")
    c = vr.check_opt84_tool_swap(report, p)
    assert c.ok, c.detail


def test_verifier_fails_an_install_card_without_its_recipe(tmp_path):
    f, card = _rendered()
    report = "## 🧰 Tool swaps on other long poles\n\n" + card.replace(
        bp._OPT84_INSTALL_RECIPE, "Install it.")
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(_findings_doc(f)), encoding="utf-8")
    c = vr.check_opt84_tool_swap(report, p)
    assert not c.ok and "install recipe" in c.detail


def test_verifier_fails_an_install_row_in_a_repo_already_on_bun(tmp_path):
    f, card = _rendered()
    report = "## 🧰 Tool swaps on other long poles\n\n" + card
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(_findings_doc(f, _pm(lockfiles=["bun.lock"]))),
                 encoding="utf-8")
    c = vr.check_opt84_tool_swap(report, p)
    assert not c.ok and "already" in c.detail


def test_verifier_fails_an_install_row_below_the_share_floor(tmp_path):
    f, card = _rendered()
    doc = _findings_doc(f)
    doc["pr_critical_path"]["poles"] = [_pole(job_p50=900.0)]
    f["tool_swap"]["job_p50_s"] = 900.0
    f["tool_swap"]["share"] = round(90.0 / 900.0, 4)
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    c = vr.check_opt84_tool_swap("## 🧰 Tool swaps on other long poles\n\n" + card, p)
    assert not c.ok and "qualify" in c.detail


def test_install_floor_and_recipe_copies_are_pinned():
    assert vr._VR_OPT84_INSTALL_MIN_SHARE == cr._OPT84_INSTALL_MIN_SHARE
    assert vr._VR_OPT84_INSTALL_CATEGORY == cr._OPT84_INSTALL_CATEGORY == "install"
    assert vr._VR_OPT84_INSTALL_RECIPE == bp._OPT84_INSTALL_RECIPE


# ---- review fixes: flags, nested projects, pnpm/Yarn settings -------------------

def _run_in(run, wd, block):
    doc = _doc(run)
    doc["jobs"]["test"]["steps"][1]["working-directory"] = wd
    withheld: dict[str, int] = {}
    cands: list[dict] = []
    out = cr._detect_opt84_tool_swap([_pole()], {_WF: doc}, block, [], 10,
                                     withheld=withheld, withheld_candidates=cands)
    return out, withheld, cands


def _nested_block(web_pm, root_pm=None):
    b = _block(root_pm or _pm(lockfiles=[]), scripts={"": {}, "web": {}})
    b["package_manager_dirs"] = {} if web_pm is None else {"web": web_pm}
    return b


@pytest.mark.parametrize("run,bun", [
    ("npm ci --omit=dev --ignore-scripts",
     "bun install --production --ignore-scripts"),
    ("pnpm install --prod --ignore-scripts",
     "bun install --production --ignore-scripts"),
    ("yarn install --production --frozen-lockfile", "bun install --production"),
    ("npm ci --no-optional", "bun install --omit optional"),
])
def test_benchmark_installs_the_same_dependency_set(run, bun):
    out, withheld, cands = _run(run)
    assert len(out) == 1, (withheld, cands)
    assert _install(out[0])["benchmark_commands"][1] == f"(time {bun})"


@pytest.mark.parametrize("run", ["yarn --network-timeout 600000",
                                 "yarn install --network-timeout 600000"])
def test_yarn_flag_values_do_not_hide_the_install(run):
    out, withheld, cands = _run(run, block=_block(_pm(lockfiles=["yarn.lock"])))
    assert len(out) == 1, (withheld, cands)
    assert _install(out[0])["leaf_command"] == run


@pytest.mark.parametrize("tokens", [["yarn", "--version"], ["npm", "ci", "--help"],
                                    ["pnpm", "install", "-v"]])
def test_a_version_or_help_call_is_not_an_install(tokens):
    assert cr._opt84_install_kind(tokens) is None


def test_a_nested_install_already_on_bun_is_silent():
    out, withheld, _c = _run_in("npm ci", "web",
                                _nested_block(_pm(lockfiles=["bun.lock"])))
    assert out == [] and withheld == {"target_already_in_use": 1}


def test_a_nested_yarn_pnp_project_is_blocked():
    out, withheld, _c = _run_in("yarn install --immutable", "web",
                                _nested_block(_pm(lockfiles=["yarn.lock"],
                                                  yarn_pnp=True)))
    assert out == [] and withheld == {"install_swap_blocked": 1}


def test_a_nested_install_whose_directory_was_not_read_holds_back():
    out, withheld, _c = _run_in("npm ci", "web", _nested_block(None))
    assert out == [] and withheld == {"install_layout_unreadable": 1}


def test_a_nested_install_names_its_own_lockfile():
    out, _w, _c = _run_in("npm ci", "web",
                          _nested_block(_pm(lockfiles=["package-lock.json"])))
    t = _install(out[0])
    assert "web/package-lock.json" in " ".join(t["fit_reasons"])
    assert t["install_dir"] == "web"


def test_a_nearer_node_modules_linker_overrides_a_default_pnp():
    # web pins Yarn 4 (default PnP) but the root `.yarnrc.yml` sets node-modules.
    out, withheld, cands = _run_in(
        "yarn install --immutable", "web",
        _nested_block(_pm(lockfiles=["yarn.lock"], yarn_pnp=True,
                          node_linker=None, pnp_files=False),
                      root_pm=_pm(lockfiles=[], yarn_pnp=False,
                                  node_linker="node-modules", pnp_files=False)))
    assert len(out) == 1, (withheld, cands)


def test_scan_reads_each_nested_project(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}", encoding="utf-8")
    (web / "bun.lock").write_text("", encoding="utf-8")
    (web / ".yarnrc.yml").write_text("nodeLinker: pnp\n", encoding="utf-8")
    dirs = scan._read_type_aware_lint(tmp_path)["package_manager_dirs"]
    assert dirs["web"]["lockfiles"] == ["bun.lock"]
    assert dirs["web"]["node_linker"] == "pnp" and dirs["web"]["yarn_pnp"] is True


def test_scan_reads_a_pnpmfile_set_in_npmrc(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".npmrc").write_text(
        "# hooks\nregistry=https://registry.npmjs.org/\npnpmfile = ./scripts/hooks.cjs\n",
        encoding="utf-8")
    pm = scan._read_type_aware_lint(tmp_path)["package_manager"]
    assert pm["pnpmfile"] == [".npmrc pnpmfile: ./scripts/hooks.cjs"]


@pytest.mark.parametrize("run", ["npm install playwright", "pnpm add -D vite",
                                 "yarn add left-pad"])
def test_an_install_naming_packages_is_not_the_dependency_install(run):
    out, withheld, _c = _run(run)
    assert out == [] and "install_swap_blocked" not in withheld


def _verify(f, pm=None, extra=None):
    doc = _findings_doc(f, pm)
    if extra:
        doc["type_aware_lint"].update(extra)
    report = "## 🧰 Tool swaps on other long poles\n\n" + "\n".join(
        bp._opt84_card(f, "https://x/catalog.md"))
    return doc, report


def _check(tmp_path, doc, report):
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return vr.check_opt84_tool_swap(report, p)


@pytest.mark.parametrize("pm,word", [
    (_pm(yarn_pnp=True), "Plug'n'Play"),
    (_pm(pnpmfile=[".npmrc pnpmfile: ./h.cjs"]), "Plug'n'Play"),
    (_pm(unreadable=["package.json"]), "readable"),
])
def test_verifier_fails_an_install_row_the_read_does_not_allow(tmp_path, pm, word):
    f, _card = _rendered()
    doc, report = _verify(f, pm)
    c = _check(tmp_path, doc, report)
    assert not c.ok and word in c.detail


def test_verifier_fails_an_install_row_not_stamped_dual_run(tmp_path):
    f, _card = _rendered()
    _install(f)["fit"] = "drop-in"
    doc, report = _verify(f)
    c = _check(tmp_path, doc, report)
    assert not c.ok and "dual-run" in c.detail


def test_verifier_fails_an_install_row_on_a_step_already_running_bun(tmp_path):
    f, _card = _rendered()
    f["tool_swap"]["resolved_leaves"].append("bun install")
    doc, report = _verify(f)
    c = _check(tmp_path, doc, report)
    assert not c.ok and "already runs" in c.detail


def test_verifier_walks_the_install_directory(tmp_path):
    out, _w, _c = _run_in("npm ci", "web",
                          _nested_block(_pm(lockfiles=["package-lock.json"])))
    f = out[0]
    doc, report = _verify(f, _pm(lockfiles=[]),
                          {"package_scripts": {"": {}, "web": {}},
                           "package_manager_dirs": {"web": _pm(lockfiles=["bun.lock"])}})
    c = _check(tmp_path, doc, report)
    assert not c.ok and "already on bun" in c.detail


@pytest.mark.parametrize("field,pnp", [("yarn@4.5.0", True), ("yarn@2.4.3", True),
                                       ("yarn@1.22.22", False), ("pnpm@9.0.0", False)])
def test_scan_reads_yarn_berrys_default_linker_from_package_manager(tmp_path, field, pnp):
    (tmp_path / "package.json").write_text(
        json.dumps({"packageManager": field}), encoding="utf-8")
    pm = scan._read_type_aware_lint(tmp_path)["package_manager"]
    assert pm["yarn_pnp"] is pnp

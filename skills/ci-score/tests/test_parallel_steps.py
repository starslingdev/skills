"""GitHub Actions parallel steps (shipped 2026-06-25): a step may be
`- parallel:` holding a LIST of ordinary child steps, and the control steps
`- wait:` / `- wait-all:` / `- cancel:` carry no `run:`/`uses:`.

Every check that reads steps read `job.steps` as a flat list, so a child step
inside a `parallel:` group was invisible and a verdict could flip on a step the
engine never saw. The invariant pinned here: wrapping the DECISIVE step of a
check's positive or negative fixture in `- parallel:` never changes the
verdict. Plus: a control step never crashes a check, the checks that read no
steps are unaffected, and a malformed `parallel:` (not a list) is disclosed in
the findings document and the report header, never silently skipped.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SPEC_PATH = _SKILL_DIR / "references" / "ci-score-spec.json"


def _load(mod_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(mod_name, _SKILL_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


pf_mod = _load("ci_score_practice_facts", "scripts/practice_facts.py")
cc_mod = _load("ci_score_collect_config", "scripts/collect_config.py")
rr_mod = _load("ci_score_render_report", "scripts/render_report.py")
vr_mod = _load("ci_score_verify_report", "scripts/verify_report.py")

SHA = "8f4b7f84864484a7bf31766abe9204da3cbe65b3"
PR_ON = {"pull_request": None}


def _wf(steps: list, on=None, job_id: str = "b") -> dict:
    return {"on": PR_ON if on is None else on, "jobs": {job_id: {"steps": steps}}}


def _parsed(doc: dict, rel: str = "ci.yml") -> list[tuple[str, dict, str]]:
    raw = yaml.safe_dump(doc, sort_keys=False)
    return [(rel, yaml.safe_load(raw), raw)]


def _wrap(doc: dict, idx: int, job_id: str = "b") -> dict:
    """The same workflow with step `idx` moved inside a `parallel:` group (next
    to a harmless sibling, as a real group would hold)."""
    out = copy.deepcopy(doc)
    steps = out["jobs"][job_id]["steps"]
    steps[idx] = {"parallel": [steps[idx], {"run": "true"}]}
    return out


def _roots(tmp_path: Path, files: tuple[str, ...]) -> Path:
    for f in files:
        (tmp_path / f).write_text("{}\n")
    return tmp_path


CHECKOUT_DEEP = {"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}}

# (case id, check id, root files, flat steps, index of the decisive step, verdict)
CASES = [
    ("dep-cache pass: cache action", "ci.cache.dependency-cache", ("package.json",),
     [{"run": "npm ci"}, {"uses": "actions/cache@v4"}], 1, "pass"),
    ("dep-cache pass: setup-* cache input", "ci.cache.dependency-cache", ("package.json",),
     [{"uses": "actions/setup-node@v4", "with": {"cache": "pnpm"}}], 0, "pass"),
    ("dep-cache fail: install, no cache", "ci.cache.dependency-cache", ("package.json",),
     [{"run": "pnpm install"}], 0, "fail"),
    ("dep-cache applicable: inline install is the only signal", "ci.cache.dependency-cache", (),
     [{"run": "pip install pytest"}], 0, "fail"),
    ("dep-cache applicable: setup action is the only signal", "ci.cache.dependency-cache", (),
     [{"uses": "actions/setup-python@v5"}], 0, "fail"),
    ("build-cache pass: cache action", "ci.cache.build-cache", ("turbo.json",),
     [{"run": "turbo build"}, {"uses": "actions/cache@v4"}], 1, "pass"),
    ("build-cache fail: no cache", "ci.cache.build-cache", ("turbo.json",),
     [{"run": "turbo build"}], 0, "fail"),
    ("shallow fail: fetch-depth 0", "ci.checkout.shallow-clone", (),
     [CHECKOUT_DEEP, {"run": "npm test"}], 0, "fail"),
    ("shallow pass: history op exempts the job", "ci.checkout.shallow-clone", (),
     [CHECKOUT_DEEP, {"run": "git log --oneline"}], 1, "pass"),
    ("shallow pass: history action exempts the job", "ci.checkout.shallow-clone", (),
     [CHECKOUT_DEEP, {"uses": "tj-actions/changed-files@v45"}], 1, "pass"),
    ("shallow pass: shallow checkout", "ci.checkout.shallow-clone", (),
     [{"uses": "actions/checkout@v4"}], 0, "pass"),
    ("change-scoped pass: --filter", "ci.build.change-scoped", ("turbo.json",),
     [{"run": "pnpm turbo run build --filter=...[origin/main]"}], 0, "pass"),
    ("change-scoped pass: changed-files action", "ci.build.change-scoped", ("turbo.json",),
     [{"uses": "dorny/paths-filter@v3"}, {"run": "pnpm turbo run build"}], 0, "pass"),
    ("change-scoped pass: github-script code", "ci.build.change-scoped", ("turbo.json",),
     [{"uses": "actions/github-script@v7", "with": {"script": "const affected = 1"}}], 0, "pass"),
    ("change-scoped applicable: nx command is the only task-graph signal",
     "ci.build.change-scoped", (), [{"run": "npx nx affected -t test"}], 0, "pass"),
    ("change-scoped fail: unscoped", "ci.build.change-scoped", ("turbo.json",),
     [{"run": "pnpm turbo run build"}], 0, "fail"),
    ("pinned pass: every action pinned", "ci.security.pinned-action-shas", (),
     [{"uses": f"actions/checkout@{SHA}"}], 0, "pass"),
    ("pinned fail: unpinned action inside the group", "ci.security.pinned-action-shas", (),
     [{"uses": f"actions/checkout@{SHA}"}, {"uses": "some-org/deploy@main"}], 1, "fail"),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_wrapping_the_decisive_step_in_parallel_never_changes_the_verdict(tmp_path, case):
    _cid, check, root_files, steps, idx, want = case
    root = _roots(tmp_path, root_files)
    flat = _wf(steps)
    assert pf_mod._practice_facts(_parsed(flat), root)[check]["state"] == want, \
        "fixture precondition: the flat workflow must give the pinned verdict"
    wrapped = _wrap(flat, idx)
    got = pf_mod._practice_facts(_parsed(wrapped), root)[check]
    assert got["state"] == want, (
        f"{check} read {got['state']!r} once the decisive step moved inside a "
        f"`parallel:` group (flat workflow reads {want!r}): {got['evidence']}")


def test_nested_parallel_groups_are_read_too(tmp_path):
    flat = _wf([{"uses": f"actions/checkout@{SHA}"}, {"uses": "some-org/deploy@main"}])
    nested = copy.deepcopy(flat)
    nested["jobs"]["b"]["steps"][1] = {"parallel": [{"parallel": [{"uses": "some-org/deploy@main"}]}]}
    f = pf_mod._practice_facts(_parsed(nested), tmp_path)["ci.security.pinned-action-shas"]
    assert f["state"] == "fail" and "1 of 2" in f["evidence"], f["evidence"]


AUTOMATION_SIGNALS = [
    ("test command", {"run": "pytest -q"}),
    ("install command", {"run": "npm ci"}),
    ("build command", {"run": "go build ./..."}),
    ("build action", {"uses": "docker/build-push-action@v6"}),
]


@pytest.mark.parametrize("label,step", AUTOMATION_SIGNALS, ids=[a[0] for a in AUTOMATION_SIGNALS])
def test_a_project_work_signal_inside_parallel_never_refuses_as_automation_only(tmp_path, label, step):
    """The automation-only refusal reads steps too: real CI whose only
    test/build/install step sits in a `parallel:` group must still score."""
    flat = _wf([{"run": "echo hi"}, step], on={"push": None}, job_id="ci")
    assert pf_mod._automation_only(_parsed(flat), tmp_path) is False
    wrapped = _wrap(flat, 1, job_id="ci")
    assert pf_mod._automation_only(_parsed(wrapped), tmp_path) is False, label


def test_composite_action_parallel_children_are_read(tmp_path):
    act = tmp_path / ".github" / "actions" / "setup"
    act.mkdir(parents=True)
    act.joinpath("action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - parallel:\n"
        "        - uses: actions/setup-node@v4\n          with:\n            cache: pnpm\n"
        "        - run: echo hi\n          shell: bash\n")
    (tmp_path / "package.json").write_text("{}\n")
    wf = _wf([{"uses": "./.github/actions/setup"}])
    f = pf_mod._practice_facts(_parsed(wf), tmp_path)["ci.cache.dependency-cache"]
    assert f["state"] == "pass", f["evidence"]


def test_checks_that_read_no_steps_are_unaffected_by_parallel_groups(tmp_path):
    """Concurrency, cancel-superseded, path filters, job timeouts, OIDC scoping
    and test sharding read workflow/job structure, not steps — a parallel
    group must leave every one of them exactly as the flat workflow reads."""
    base = {
        "on": {"pull_request": {"paths": ["src/**"]}},
        "permissions": {"contents": "read"},
        "concurrency": {"group": "g", "cancel-in-progress": True},
        "jobs": {"test": {
            "timeout-minutes": 10,
            "permissions": {"id-token": "write"},
            "strategy": {"matrix": {"shard": [1, 2]}},
            "steps": [{"uses": "actions/checkout@v4"}, {"run": "pytest"}]}},
    }
    flat = pf_mod._practice_facts(_parsed(base), tmp_path)
    wrapped = pf_mod._practice_facts(_parsed(_wrap(base, 1, job_id="test")), tmp_path)
    for cid in ("ci.trigger.concurrency-groups", "ci.trigger.cancel-superseded",
                "ci.trigger.path-filter", "ci.hygiene.job-timeouts",
                "ci.security.scoped-id-token", "ci.parallel.test-sharding"):
        assert wrapped[cid]["state"] == flat[cid]["state"] == "pass", cid


CONTROL_STEPS = [{"wait": ["lint"]}, {"wait-all": True}, {"cancel": "lint"},
                 {"wait": None}, {"parallel": None}, {"parallel": "oops"},
                 {"parallel": {"run": "npm ci"}}, {"parallel": []}]


@pytest.mark.parametrize("step", CONTROL_STEPS, ids=[json.dumps(s) for s in CONTROL_STEPS])
def test_control_and_malformed_steps_never_crash_a_check(tmp_path, step):
    (tmp_path / "package.json").write_text("{}\n")
    wf = _wf([step], job_id="test")
    facts = pf_mod._practice_facts(_parsed(wf), tmp_path)
    assert len(facts) == 11
    assert all(f["state"] in ("pass", "fail", "not_applicable") for f in facts.values())
    pf_mod._automation_only(_parsed(wf), tmp_path)  # no raise


def test_walker_yields_leaves_in_declaration_order_and_counts_what_it_skipped():
    stats = pf_mod._new_step_stats()
    steps = [{"run": "a"},
             {"parallel": [{"run": "b"}, {"parallel": [{"run": "c"}]}, {"wait-all": True}]},
             {"wait": ["x"]}, {"cancel": "y"}, {"parallel": "not-a-list"},
             None, "junk", {"run": "d"}]
    leaves = pf_mod._walk_steps(steps, stats, "ci.yml")
    assert [s["run"] for s in leaves] == ["a", "b", "c", "d"]
    assert stats["groups"] == 3            # outer, nested, malformed
    assert stats["steps_in_groups"] == 2   # b, c
    assert stats["control_steps"] == 3     # wait-all, wait, cancel
    assert stats["malformed_groups"] == 1
    assert stats["malformed_files"] == ["ci.yml"]


# --- disclosure: findings document + report header ---------------------------

def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args],
                         capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                              "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                              "HOME": str(root)})
    return out.stdout.strip()


def _repo(tmp_path: Path, workflow: str) -> Path:
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / ".github" / "workflows" / "ci.yml").write_text(workflow)
    _git(root, "add", ".github/workflows/ci.yml")
    _git(root, "commit", "-qm", "init")
    return root


PARALLEL_WF = (
    "on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - parallel:\n"
    "          - run: npm ci\n"
    "          - uses: actions/cache@v4\n"
    "      - wait-all: true\n"
    "      - run: npm test\n")


def _render_and_verify(doc: dict) -> str:
    registry = json.loads(_SPEC_PATH.read_text())
    report = rr_mod.render_report(doc, registry)
    assert vr_mod.verify(doc, report, registry) == []
    return report


def test_collector_records_and_report_discloses_steps_read_from_parallel_groups(tmp_path):
    doc, code = cc_mod.collect(_repo(tmp_path, PARALLEL_WF))
    assert code == 0
    rec = doc["data_sources"]["parallel_steps"]
    assert rec["groups"] == 1 and rec["steps_in_groups"] == 2
    assert rec["control_steps"] == 1 and rec["malformed_groups"] == 0
    assert doc["practice_facts"]["ci.cache.dependency-cache"]["state"] == "pass"
    header = _render_and_verify(doc).split("```", 1)[0]
    assert "2 step(s) inside `parallel:` groups read (1 group(s))" in header
    assert "1 `wait`/`wait-all`/`cancel` control step(s) skipped" in header


def test_a_malformed_parallel_group_is_disclosed_never_silently_skipped(tmp_path):
    wf = ("on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
          "      - parallel:\n          run: npm ci\n      - run: npm test\n")
    doc, code = cc_mod.collect(_repo(tmp_path, wf))
    assert code == 0
    rec = doc["data_sources"]["parallel_steps"]
    assert rec["malformed_groups"] == 1
    assert rec["malformed_files"] == [".github/workflows/ci.yml"]
    header = _render_and_verify(doc).split("```", 1)[0]
    assert "1 malformed `parallel:` group(s) not read" in header
    assert "`.github/workflows/ci.yml`" in header


def test_verify_goes_red_when_the_parallel_disclosure_is_dropped(tmp_path):
    doc, _code = cc_mod.collect(_repo(tmp_path, PARALLEL_WF))
    registry = json.loads(_SPEC_PATH.read_text())
    report = rr_mod.render_report(doc, registry)
    stripped = "\n".join(l for l in report.splitlines() if "**Parallel steps**" not in l)
    assert any("parallel" in p for p in vr_mod.verify(doc, stripped, registry))


def test_no_parallel_syntax_leaves_the_document_and_header_unchanged(tmp_path):
    flat = PARALLEL_WF.replace("      - parallel:\n          - run: npm ci\n"
                               "          - uses: actions/cache@v4\n      - wait-all: true\n",
                               "      - run: npm ci\n      - uses: actions/cache@v4\n")
    doc, _code = cc_mod.collect(_repo(tmp_path, flat))
    assert "parallel_steps" not in doc["data_sources"]
    assert "Parallel steps" not in _render_and_verify(doc)

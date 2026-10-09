"""Steps inside a `parallel:` group are scanned like any other step.

GitHub Actions lets a job run steps concurrently: a step may be written as
`- parallel:` followed by a LIST of ordinary child steps, and a job may carry
pure control steps (`wait:`, `wait-all:`, `cancel:`) that hold no `run:` and
no `uses:`. The `parallel:` entry itself has neither key either, so a detector
that walks `job.steps` as a flat list and reads `step.get("run")` /
`step.get("uses")` skips every child — template injection, curl|bash, cache
poisoning and the rest, all unscanned, and the report reads clean.

The oracle here is the existing fixture corpus itself: every fixture is
scanned as written AND with each job's steps wrapped in a `- parallel:` group,
and the two scans must report the same findings at the same (shifted) lines.
No new workflow text is invented for the attack vectors; the shapes are the
fixtures' own.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_TESTS_DIR = str(Path(__file__).resolve().parent)
sys.path.insert(0, _TESTS_DIR)
try:
    from _scan_import import load_scan, load_script  # noqa: E402
finally:
    try:
        sys.path.remove(_TESTS_DIR)
    except ValueError:                          # pragma: no cover - defensive
        pass

scan = load_scan()
_SCAN_SCRIPT = _SKILL_DIR / "scripts" / "scan.py"
_CLOAKED = _SKILL_DIR / "tests" / "fixtures" / "dot-github" / "workflows"

_STEPS_KEY_RE = re.compile(r"^(\s*)steps:\s*(?:#.*)?$")


def _wrap_steps_in_parallel(text: str) -> tuple[str, list[int]]:
    """Wrap every `steps:` list in one `- parallel:` group.

    Returns the new text and the ORIGINAL 1-based line numbers of each
    `steps:` key a group line was inserted after, so a finding's line can be
    mapped from the original file to the wrapped one.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inserted_after: list[int] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _STEPS_KEY_RE.match(line.rstrip("\n"))
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        j = i + 1
        block: list[str] = []
        while j < len(lines):
            cur = lines[j]
            stripped = cur.strip()
            cur_indent = len(cur) - len(cur.lstrip(" "))
            if not stripped or stripped.startswith("#") or cur_indent > indent \
                    or (cur_indent == indent and stripped.startswith("- ")):
                block.append(cur)
                j += 1
                continue
            break
        while block and (not block[-1].strip() or block[-1].strip().startswith("#")):
            block.pop()
            j -= 1
        items = [b for b in block if b.strip().startswith("- ")]
        if not items:
            i += 1
            continue
        item_indent = len(items[0]) - len(items[0].lstrip(" "))
        out.append(" " * item_indent + "- parallel:\n")
        inserted_after.append(i + 1)
        out.extend(("    " + b) if b.strip() else b for b in block)
        i = j
    return "".join(out), inserted_after


def _shift(line: int, inserted_after: list[int]) -> int:
    return line + sum(1 for at in inserted_after if at < line)


def _scan_root(root: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(_SCAN_SCRIPT), "--root", str(root),
         "--gh-impostor", "off"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _write(root: Path, name: str, text: str) -> None:
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(text, encoding="utf-8")


def _signature(findings: list[dict], inserted_after: list[int] | None) -> list:
    return sorted(
        (f["pattern"],
         _shift(f["line"], inserted_after) if inserted_after is not None
         and isinstance(f.get("line"), int) else f.get("line"),
         tuple(sorted(f.get("affected_jobs") or [])))
        for f in findings
    )


# One positive fixture per attack vector that has one (P14.11 is network-gated
# and covered below through its pin collector; P14.18 is a document-level
# permission fact with no step in it).
_POSITIVE_FIXTURES = [
    ("p14_10_template_injection.yml.fixture", "P14.10"),
    ("p14_10_new_sinks.yml.fixture", "P14.10"),
    ("p14_10_line_attribution.yml.fixture", "P14.10"),
    ("p14_24_curl_pipe_bash.yml.fixture", "P14.24"),
    ("p14_24_mutable_fetch_exec.yml.fixture", "P14.24"),
    ("p14_14_context_dump.yml.fixture", "P14.14"),
    ("p14_15_env_path_inject.yml.fixture", "P14.15"),
    ("p14_7_pr_target_writes_cache.yml.fixture", "P14.7"),
    ("p14_7_pr_target_writes_cache.yml.fixture", "P14.9"),
    ("p14_19_credential_in_cache_path.yml.fixture", "P14.19"),
    ("p14_25_install_scripts_privileged.yml.fixture", "P14.25"),
]


@pytest.mark.parametrize("fixture,pattern", _POSITIVE_FIXTURES)
def test_vector_fires_the_same_inside_a_parallel_group(
    tmp_path: Path, fixture: str, pattern: str,
) -> None:
    text = (_CLOAKED / fixture).read_text(encoding="utf-8")
    wrapped, inserted_after = _wrap_steps_in_parallel(text)
    assert inserted_after, f"{fixture}: the transform wrapped nothing"
    assert "- parallel:" in wrapped

    plain_root, wrapped_root = tmp_path / "plain", tmp_path / "wrapped"
    name = fixture.removesuffix(".fixture")
    _write(plain_root, name, text)
    _write(wrapped_root, name, wrapped)
    plain = _scan_root(plain_root)
    inside = _scan_root(wrapped_root)

    want = [s for s in _signature(plain["findings"], inserted_after)
            if s[0] == pattern]
    got = [s for s in _signature(inside["findings"], None) if s[0] == pattern]
    assert want, f"{fixture}: the unwrapped fixture no longer fires {pattern}"
    assert got == want, (
        f"{pattern} inside a parallel: group: expected {want}, got {got}")
    # No new gap invented by the wrapping, and none lost.
    assert inside["dropped_matches"] == plain["dropped_matches"]


@pytest.mark.parametrize(
    "fixture", sorted(p.name for p in _CLOAKED.glob("*.yml.fixture")))
def test_whole_corpus_reports_identically_when_wrapped(
    tmp_path: Path, fixture: str,
) -> None:
    """Every fixture, positive and negative: wrapping must neither lose a
    finding nor invent one."""
    text = (_CLOAKED / fixture).read_text(encoding="utf-8")
    wrapped, inserted_after = _wrap_steps_in_parallel(text)
    if not inserted_after:
        pytest.skip(f"{fixture} has no step list to wrap")
    name = fixture.removesuffix(".fixture")
    _write(tmp_path / "plain", name, text)
    _write(tmp_path / "wrapped", name, wrapped)
    plain = _scan_root(tmp_path / "plain")
    inside = _scan_root(tmp_path / "wrapped")
    assert _signature(inside["findings"], None) == \
        _signature(plain["findings"], inserted_after)


def test_wrapped_scan_discloses_the_parallel_steps_it_read(tmp_path: Path) -> None:
    """Never silent: the scan says how many steps it read inside parallel:
    groups, so a reader can tell the new syntax was understood."""
    text = (_CLOAKED / "p14_10_template_injection.yml.fixture").read_text()
    wrapped, _ = _wrap_steps_in_parallel(text)
    _write(tmp_path, "a.yml", wrapped)
    data = _scan_root(tmp_path)
    stats = data.get("parallel_steps")
    assert stats is not None, "findings JSON carries no parallel_steps record"
    n_children = sum(
        1 for ln in wrapped.splitlines()
        if re.match(r"^\s*- (?!parallel:)", ln)
    )
    assert stats["steps_scanned"] == n_children
    assert stats["workflows"] == [".github/workflows/a.yml"]

    report = load_script("ci_secure_report", "report.py")
    md = report.render(data)
    assert f"{n_children} step(s) inside `parallel:` groups scanned" in md


_CONTROL_STEPS = textwrap.dedent("""\
    on: pull_request_target
    jobs:
      build:
        runs-on: ubuntu-latest
        steps:
          - id: a
            run: echo one
            background: true
          - wait: a
          - wait: [a]
          - wait-all:
          - cancel: a
          - parallel:
              - run: echo two
              - wait: a
    """)


def test_control_steps_never_crash_a_detector_and_are_counted(tmp_path: Path) -> None:
    _write(tmp_path, "ctl.yml", _CONTROL_STEPS)
    data = _scan_root(tmp_path)
    assert data["scan_incomplete"] == []
    stats = data["parallel_steps"]
    assert stats["steps_scanned"] == 1          # `echo two`
    assert stats["control_steps"] == 5          # 4 top-level + 1 nested
    assert stats["background_steps"] == 1


def test_walker_yields_leaves_in_declaration_order_with_tags() -> None:
    job = {"steps": [
        {"run": "one", "background": True},
        {"wait": "a"},
        {"parallel": [{"run": "two"}, {"uses": "x/y@v1"},
                      {"parallel": [{"run": "deep"}]}]},
        {"run": "three"},
    ]}
    leaves = list(scan._iter_job_steps(job))
    assert [(s.step.get("run") or s.step.get("uses")) for s in leaves] == \
        ["one", "two", "x/y@v1", "deep", "three"]
    assert [s.in_parallel_group for s in leaves] == \
        [False, True, True, True, False]
    assert [s.background for s in leaves] == [True, False, False, False, False]
    assert leaves[3].path == (2, 2, 0)


def test_malformed_parallel_is_disclosed_not_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "bad.yml", textwrap.dedent("""\
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - parallel:
                  run: echo not-a-list
        """))
    data = _scan_root(tmp_path)
    reasons = [e["reason"] for e in data["coverage_notes"]
               if e["workflow_file"].endswith("bad.yml")]
    assert any("parallel:" in r and "NOT scanned" in r for r in reasons), reasons

    # The banner headline must say what happened to this step: it was NOT
    # scanned. The headline written for steps that WERE read ("carry a value
    # this scan cannot know") would contradict the bullet underneath it.
    report = load_script("ci_secure_report", "report.py")
    md = report.render(data)
    assert "carry a value this scan cannot know" not in md, md
    assert "1 `parallel:` group(s) in 1 workflow(s)" in md, md


def _give_each_group_a_run(wrapped: str) -> str:
    """Turn every `- parallel:` entry into one that ALSO carries `run:`.

    GitHub does not accept that shape. The command added is the inert `make`
    already used in this file, so the only findings are the fixture's own."""
    return re.sub(r"^(\s*)- parallel:$",
                  lambda m: f"{m.group(1)}- run: make\n{m.group(1)}  parallel:",
                  wrapped, flags=re.MULTILINE)


def test_a_parallel_group_that_also_carries_run_is_scanned_and_disclosed(
    tmp_path: Path,
) -> None:
    """A `parallel:` entry that ALSO has `run:` (or `uses:`) used to be read as
    an ordinary step: its own command was scanned, its children never were,
    and nothing said so. Its children must be scanned at their own lines, and
    the shape must surface as a coverage note so the report is not clean."""
    fixture = "p14_10_template_injection.yml.fixture"
    text = (_CLOAKED / fixture).read_text(encoding="utf-8")
    wrapped, inserted_after = _wrap_steps_in_parallel(text)
    mixed = _give_each_group_a_run(wrapped)
    assert mixed.count("- run: make") == len(inserted_after) > 0
    name = fixture.removesuffix(".fixture")
    _write(tmp_path / "plain", name, text)
    _write(tmp_path / "mixed", name, mixed)
    plain = _scan_root(tmp_path / "plain")
    inside = _scan_root(tmp_path / "mixed")
    # Two lines inserted per group (`- run: make` + `parallel:`).
    shifted_twice = sorted(inserted_after + inserted_after)
    want = [s for s in _signature(plain["findings"], shifted_twice)
            if s[0] == "P14.10"]
    got = [s for s in _signature(inside["findings"], None) if s[0] == "P14.10"]
    assert want and got == want, (want, got)
    notes = [e["reason"] for e in inside["coverage_notes"]]
    assert len(notes) == len(inserted_after), notes
    assert all("also carries `run:`" in r for r in notes), notes


def _node_get(node, key: str):
    """A scalar child of a composed mapping node, through `<<:` merges."""
    for k, v in node.value:
        if k.value == key:
            return v.value
    for k, v in node.value:
        if k.value == "<<":
            return _node_get(v, key)
    return None


def _lockstep_shapes() -> list:
    deep: list = [{"run": "deepest"}]
    for _ in range(scan._WALK_MAX_DEPTH + 2):
        deep = [{"parallel": deep}]
    return [
        [{"run": "own", "parallel": [{"run": "child"}, {"uses": "x/y@v1"}]}],
        [{"uses": "x/y@v1", "parallel": "not-a-list"}, {"run": "after"}],
        deep,
    ]


@pytest.mark.parametrize("steps", _lockstep_shapes())
def test_node_walker_stays_in_lockstep_with_the_step_walker(steps) -> None:
    """Line attribution pairs the two walkers' outputs by position, so they
    must yield the same leaves in the same order — for every shape, including
    one nested past the depth cap and one carrying `run:` beside `parallel:`."""
    import yaml
    text = yaml.safe_dump({"jobs": {"j": {"steps": steps}}})
    leaves = [s.step for s in scan._iter_job_steps(
        yaml.safe_load(text)["jobs"]["j"])]
    node = yaml.compose(text)
    jobs = dict((k.value, v) for k, v in node.value)["jobs"]
    steps_node = dict((k.value, v) for k, v in
                      dict((k.value, v) for k, v in jobs.value)["j"].value)["steps"]
    nodes = list(scan._iter_step_nodes(steps_node))
    assert len(nodes) == len(leaves), (len(nodes), len(leaves))
    assert [_node_get(n, "run") or _node_get(n, "uses")
            for n in nodes] == \
        [s.get("run") or s.get("uses") for s in leaves]


def test_node_walker_follows_a_merge_key_holding_a_group() -> None:
    """`- <<: *grp` resolves to a `parallel:` group when loaded, so the node
    walker must see the same group, or every later step's line shifts."""
    import yaml
    text = textwrap.dedent("""\
        x-grp: &grp
          parallel:
            - run: one
            - run: two
        jobs:
          j:
            steps:
              - <<: *grp
              - run: three
        """)
    leaves = [s.step["run"] for s in scan._iter_job_steps(
        yaml.safe_load(text)["jobs"]["j"])]
    assert leaves == ["one", "two", "three"]
    root = yaml.compose(text)
    jobs = dict((k.value, v) for k, v in root.value)["jobs"]
    steps_node = dict((k.value, v) for k, v in
                      dict((k.value, v) for k, v in jobs.value)["j"].value)["steps"]
    assert [_node_get(n, "run")
            for n in scan._iter_step_nodes(steps_node)] == leaves


def test_impostor_pin_collector_sees_a_pin_inside_a_parallel_group(
    tmp_path: Path,
) -> None:
    """P14.11 is network-gated; what it can check is what the pin collector
    hands it, so the collector is the thing that must see the child step."""
    _write(tmp_path, "ci.yml", textwrap.dedent("""\
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - parallel:
                  - uses: actions/checkout@8ade135a41bc03ea155e62e844d188df1ea18608
                  - uses: example-org/some-action@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        """))
    pins = scan._collect_sha_pins(tmp_path, scan.all_workflow_files(tmp_path))
    assert [(line, repo) for _, line, repo, _ in pins] == [
        (7, "actions/checkout"), (8, "example-org/some-action")]


def test_config_facts_read_checkouts_inside_a_parallel_group() -> None:
    facts = load_script("ci_secure_config_facts", "config_facts.py")
    doc = {
        "on": "pull_request_target",
        "jobs": {"t": {"steps": [{"parallel": [
            {"uses": "actions/checkout@v4",
             "with": {"ref": "${{ github.event.pull_request.head.sha }}"}},
            {"run": "make"},
        ]}]}},
    }
    assert facts._unpersisted_checkout_violations(doc) == ["t"]
    assert facts._jobs_checking_out_attacker_head(scan, doc) == ["t"]


def test_a_group_past_the_depth_cap_is_not_called_a_non_list() -> None:
    """A valid list nested too deep is a coverage gap, but saying its value
    "is not a list of steps" would be false."""
    stats = scan._StepWalkStats()
    job = {"steps": _lockstep_shapes()[2]}
    assert list(scan._iter_job_steps(job, stats)) == []
    assert stats.malformed and stats.malformed[0][1] == scan._GROUP_TOO_DEEP
    sentence = scan._GROUP_GAP_SENTENCE[scan._GROUP_TOO_DEEP].format(
        depth=scan._WALK_MAX_DEPTH)
    assert "not a list" not in sentence
    assert f"more than {scan._WALK_MAX_DEPTH} groups deep" in sentence


def test_parallel_row_never_opens_with_a_zero_and_names_background_steps() -> None:
    report = load_script("ci_secure_report", "report.py")
    control_only = report._parallel_steps_cell(
        {"steps_scanned": 0, "control_steps": 1, "background_steps": 0})
    assert control_only and not control_only.startswith("0 "), control_only
    background_only = report._parallel_steps_cell(
        {"steps_scanned": 0, "control_steps": 0, "background_steps": 2})
    assert background_only == "2 `background: true` step(s) scanned"
    assert report._parallel_steps_cell(
        {"steps_scanned": 0, "control_steps": 0, "background_steps": 0}) == ""

"""Where the gate finds the code it runs, and the rule it enforces.

Two files decide every verdict this gate reaches: the ENGINE (`scan.py`) that
produces the facts, and the RULE (`config.py`) that says which fact outcomes
block. Both must come from the gate's own trust boundary — never from the
repository being audited.

The distinction is the whole point. `GITHUB_WORKSPACE` is the tree under
examination; on a fork pull request an attacker writes it. A gate that resolves
its engine from there executes attacker code and prints whatever verdict that
code returns, which is indistinguishable from a clean scan. So the engine is
resolved relative to the gate file itself, and `CI_SECURE_ENGINE` is the one
deliberate redirect — used by a repository that vendors ci-secure into a
directory of its own rather than checking out this whole tree.

`config.py` then follows the ENGINE, not the gate: it is loaded from the
directory of whichever engine was resolved. That single rule serves both
layouts — in this repository the gate sits at `.github/scripts/` and the engine
at `skills/ci-secure/scripts/`, while a vendored install puts both in one
directory — and it keeps the rule in the same trust class as the engine it
configures, instead of inventing a third thing that can be pointed somewhere
else independently.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_GATE = _REPO / ".github" / "scripts" / "ci_secure_gate.py"
_ENGINE = _REPO / "skills" / "ci-secure" / "scripts" / "scan.py"
_CONFIG = _REPO / "skills" / "ci-secure" / "scripts" / "config.py"

_FACTS = {
    "scanned_workflows": 1,
    "findings": [],
    "scan_incomplete": [],
    "dropped_matches": [],
    "gh_checks": {"P14.11": "skipped: disabled via --gh-impostor=off"},
    "security_score": {
        "facts": [{"fact_id": "sec.demo.fact", "outcome": "deferred",
                   "evidence": "an outcome this repo's config.py does not know"}],
        "score": 100, "passed": 1, "scored_count": 1,
        "applicable_count": 1, "unmeasured": [],
    },
}

# A config.py a VENDORED install could legitimately ship: it knows one outcome
# more than this repo's does, and blocks on it. Nothing here is derived from
# anything else — that independence is the subject of its own test below.
_VENDORED_CONFIG = '''\
BLOCKING_OUTCOMES = frozenset({"fail", "deferred"})
KNOWN_OUTCOMES = frozenset({"pass", "fail", "unmeasured", "deferred"})
OUTCOME_MARKS = {"pass": "PASS", "fail": "**FAIL**",
                 "unmeasured": "UNMEASURED", "deferred": "**DEFERRED**"}


def coverage_is_complete() -> bool:
    return True


def flatten_scanned(value):
    return " ".join(str(value).split()).replace("`", "'").replace("|", "\\\\|")
'''


def _run(engine: Path, tmp_path: Path, *, env_engine: bool = True):
    """Run the real gate against a stub engine, capturing its verdict."""
    summary = tmp_path / "summary.md"
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_STEP_SUMMARY": str(summary),
        # Explicit, because the gate refuses an unset value on purpose.
        "CI_SECURE_GH_IMPOSTOR": "off",
    }
    if env_engine:
        env["CI_SECURE_ENGINE"] = str(engine)
    proc = subprocess.run([sys.executable, str(_GATE)],
                          capture_output=True, text=True, env=env)
    proc.summary = summary.read_text(encoding="utf-8") if summary.exists() else ""
    return proc


def _stub_engine(directory: Path, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    engine = directory / "scan.py"
    engine.write_text(
        "import sys\n"
        f"sys.stdout.write({json.dumps(payload)!r})\n",
        encoding="utf-8")
    return engine


# --------------------------------------------------------------------------
# config.py follows the resolved ENGINE, in both layouts
# --------------------------------------------------------------------------

def test_the_vendored_layout_loads_config_from_beside_its_engine(tmp_path: Path) -> None:
    """A vendored engine's own config.py decides the verdict, not ours.

    This is the layout an external adopter installs: `scan.py` and `config.py`
    together in one vendored directory, with `CI_SECURE_ENGINE` pointing at it.
    The gate must load the rule from there. Proof is behavioural rather than
    introspective: the vendored config blocks on an outcome this repository's
    config has never heard of, so the two rules produce DIFFERENT red reasons
    for the same scan — and only the vendored one names the fact.
    """
    vendored = tmp_path / "ci-secure"
    engine = _stub_engine(vendored, _FACTS)
    (vendored / "config.py").write_text(_VENDORED_CONFIG, encoding="utf-8")

    proc = _run(engine, tmp_path)

    assert proc.returncode == 1
    assert "ci-secure fact failed: sec.demo.fact" in proc.stdout, (
        "the vendored config blocks on `deferred`, so the gate must report a "
        "FAILED FACT — reporting an unrecognised outcome instead would mean it "
        "read this repository's config.py rather than the engine's:\n"
        + proc.stdout)
    assert "**DEFERRED**" in proc.summary, (
        "the summary marks come from the same config.py as the rule")


def test_an_engine_without_a_config_falls_back_to_this_repos_rule(tmp_path: Path) -> None:
    """No config.py beside the engine → the gate's own tree supplies the rule.

    The fallback must still be a REAL rule, not an empty permissive one: an
    outcome nothing recognises is a contract change between engine and gate,
    and it goes red for exactly that stated reason.
    """
    engine = _stub_engine(tmp_path / "engine-only", _FACTS)

    proc = _run(engine, tmp_path)

    assert proc.returncode == 1
    assert "unrecognised fact outcome(s)" in proc.stdout, (
        "with no config beside the engine the gate falls back to its own tree, "
        "which does not know `deferred`:\n" + proc.stdout)


def test_the_engine_default_is_gate_relative_not_the_audited_workspace(
        tmp_path: Path) -> None:
    """With no override, the engine comes from the gate's tree — never the workspace.

    A workspace-relative default is the hole this rule exists to close: the
    audited repository may itself contain `skills/ci-secure/scripts/scan.py`
    (a vendored install, or a fork PR that adds one), and a trusted gate that
    ran THAT would be executing the code it is supposed to be judging.
    """
    # A decoy engine at our own well-known path, inside the audited workspace.
    workspace = tmp_path / "workspace"
    decoy = workspace / "skills" / "ci-secure" / "scripts"
    decoy.mkdir(parents=True)
    (decoy / "scan.py").write_text(
        "import sys; sys.stdout.write('decoy engine ran')\n", encoding="utf-8")

    proc = _run(_ENGINE, tmp_path, env_engine=False)

    assert "decoy engine ran" not in proc.stdout + proc.stderr, (
        "the gate executed an engine out of the tree it was auditing")
    # Whatever it decided, it decided it OUT LOUD. The old form of this line was
    # `... or proc.returncode in (0, 1)`, which this script can never violate —
    # it only ever exits 0 or 1 — so the assertion could not fail at all. The
    # real engine scanning an empty workspace is legitimately red here; what
    # must never happen is a red with no stated cause.
    assert proc.returncode == 0 or "::error::" in proc.stdout, (
        f"the gate exited {proc.returncode} with no stated cause:\n"
        f"{proc.stdout}\n{proc.stderr}")


def test_the_gate_imports_no_test_helper(tmp_path: Path) -> None:
    """The shipped gate stands alone: no test scaffolding in its import graph.

    A vendored copy carries the engine and the gate, never this repository's
    test tree. A gate that reached for `_scan_import` or `assert_is_ci_secure`
    would work here and fail on every adopter.
    """
    tree = ast.parse(_GATE.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    stdlib_only = {"importlib", "json", "os", "subprocess", "sys",
                   "traceback", "pathlib"}
    assert imported <= stdlib_only, (
        f"the gate imports {sorted(imported - stdlib_only)}; it must be stdlib "
        "only, since a vendored install carries neither this repository's test "
        "tree nor its package layout")

    # The helpers by name, in code rather than in prose: a call to one would
    # show up as a Name node even if the import were indirect.
    called = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for helper in ("_scan_import", "assert_is_ci_secure"):
        assert helper not in called, (
            f"the gate calls the test helper {helper!r}; a vendored install "
            "has no test tree to import it from")


# --------------------------------------------------------------------------
# The three outcome names are INDEPENDENT (§2b)
# --------------------------------------------------------------------------

def _assignment(name: str) -> ast.AST:
    tree = ast.parse(_CONFIG.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = ([node.target] if isinstance(node, ast.AnnAssign)
                   else getattr(node, "targets", []))
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return node.value
    raise AssertionError(f"config.py defines no {name}")


def test_the_blocking_rule_is_never_derived_from_the_display_dict() -> None:
    """`KNOWN_OUTCOMES`/`BLOCKING_OUTCOMES` may not be computed from `OUTCOME_MARKS`.

    Deriving the allowlist from the display table couples a cosmetic edit to
    the security rule: adding a new display style would widen the set of
    outcomes the gate accepts, so the unknown-outcome red never fires — and
    because failure is its own separate set, the new outcome would be neither
    blocked nor flagged. A new failure state shipping green out of a one-line
    display edit is precisely the coupling these three names exist to break.
    """
    for name in ("KNOWN_OUTCOMES", "BLOCKING_OUTCOMES"):
        referenced = {n.id for n in ast.walk(_assignment(name))
                      if isinstance(n, ast.Name)}
        assert "OUTCOME_MARKS" not in referenced, (
            f"{name} is derived from OUTCOME_MARKS; a display edit would move "
            "the security rule")


def _outcomes_emitted_by(source: Path) -> set[str]:
    """Every fact-outcome string literal `config_facts.py` can actually produce.

    An extraction, not a checklist. Three shapes produce an outcome in that
    module, and all three are read out of the AST:

      1. a dict literal carrying an `"outcome"` key   -> its value expression
      2. a comparison against `something["outcome"]`  -> the comparators
      3. a helper documented `(outcome, evidence)`    -> the first tuple slot

    Shape 3 matters because the engine's three-state facts compute their own
    result in a helper and pass it through, so a new outcome could enter there
    without ever appearing beside the `"outcome"` key.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: set[str] = set()

    def strings(node: ast.AST) -> set[str]:
        return {n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    for node in ast.walk(tree):
        # 1. {"outcome": <expr>}
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "outcome"):
                    found |= strings(value)
        # 2. x["outcome"] == "..." / in ("...", "...")
        elif isinstance(node, ast.Compare):
            subs = [n for n in [node.left, *node.comparators]
                    if isinstance(n, ast.Subscript)
                    and isinstance(n.slice, ast.Constant)
                    and n.slice.value == "outcome"]
            if subs:
                for operand in (node.left, *node.comparators):
                    if operand not in subs:
                        found |= strings(operand)
        # 3. def f(...) -> """(outcome, evidence) ..."""
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node) or ""
            if doc.startswith("(outcome, evidence)"):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Return) and isinstance(
                            inner.value, ast.Tuple) and inner.value.elts:
                        found |= strings(inner.value.elts[0])
    return found


def test_the_census_extracts_rather_than_filters(tmp_path: Path) -> None:
    """The census must SEE an outcome nobody told it about.

    This is the property that makes the three-table design safe: adding an
    outcome to the engine has to red the build until a human classifies it. A
    census implemented as `{v for v in ("pass","fail","unmeasured") if ...}`
    passes every other assertion in this file while being structurally
    incapable of noticing a fourth — so the guard is tested with a fourth.
    """
    invented = tmp_path / "config_facts_like.py"
    invented.write_text(
        'def _thing(root):\n'
        '    """(outcome, evidence) for a fact that computes its own result."""\n'
        '    return ("deferred", "waiting on an admin-scoped read")\n'
        '\n'
        'def _record(facts, passed):\n'
        '    facts.append({"fact_id": "x", "outcome": "quarantined"})\n'
        '    facts.append({"fact_id": "y", "outcome": "pass" if passed else "fail"})\n'
        '\n'
        'def _score(facts):\n'
        '    return [f for f in facts if f["outcome"] in ("pass", "provisional")]\n',
        encoding="utf-8")

    found = _outcomes_emitted_by(invented)
    assert {"deferred", "quarantined", "pass", "fail", "provisional"} <= found, (
        f"the census missed an outcome it was never told about: {sorted(found)}")


def test_every_outcome_the_engine_emits_is_known_marked_and_covered() -> None:
    """A census, not a spot check: the engine's vocabulary ⊆ the gate's.

    `config_facts.py` is the only producer of fact outcomes. If it learns a new
    one and these tables do not, the gate meets an outcome it cannot classify —
    which is a red build here rather than a wrong verdict in production.
    """
    sys.path.insert(0, str(_CONFIG.parent))
    try:
        import config  # noqa: PLC0415
    finally:
        sys.path.pop(0)

    emitted = _outcomes_emitted_by(_CONFIG.parent / "config_facts.py")
    assert emitted, "found no outcome literals in config_facts.py — census broken"
    # The census must EXTRACT, never filter a list written here: a filter can
    # only ever return a subset of what it already knew, so a fourth outcome in
    # config_facts.py would leave the result unchanged and this test green —
    # the exact drift it exists to catch. `_census_is_an_extraction` pins that.
    assert {"pass", "fail", "unmeasured"} <= emitted, (
        f"the census lost a known outcome; it found only {sorted(emitted)}")

    assert emitted <= set(config.KNOWN_OUTCOMES), (
        f"config_facts emits {sorted(emitted - set(config.KNOWN_OUTCOMES))}, "
        "which the gate cannot classify")
    assert set(config.KNOWN_OUTCOMES) <= set(config.OUTCOME_MARKS), (
        "every known outcome needs a display mark, or the summary invents one")
    assert set(config.BLOCKING_OUTCOMES) <= set(config.KNOWN_OUTCOMES), (
        "an outcome that blocks must also be one the gate recognises")
    assert config.coverage_is_complete() is True


def test_coverage_is_complete_can_actually_fail() -> None:
    """The census predicate is not a constant `True` in disguise."""
    sys.path.insert(0, str(_CONFIG.parent))
    try:
        import config  # noqa: PLC0415
    finally:
        sys.path.pop(0)

    assert config.coverage_is_complete(
        blocking={"nonesuch"}, known={"pass"}, marks={"pass": "PASS"}) is False

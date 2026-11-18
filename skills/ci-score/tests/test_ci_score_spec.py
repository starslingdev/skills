"""Structural validation for `references/ci-score-spec.json` (v0.1-basic).

The OD-CS15 reset: the spec is now a registry of pass/fail CONFIGURATION
FACTS. These tests pin the registry's shape, the band arithmetic that carried
over from the granular design, and the decision record itself — the reset must
stay legible in the artifact, not only in chat history.
"""
from __future__ import annotations

import ast
import json
import operator
import re
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
_SPEC_PATH = _SKILL_DIR / "references" / "ci-score-spec.json"
_METHODOLOGY_PATH = _SKILL_DIR / "references" / "ci-score-methodology.md"
_SKILL_MD_PATH = _SKILL_DIR / "SKILL.md"
_SCRIPTS_DIR = _SKILL_DIR / "scripts"

EXPECTED_CHECK_IDS = [
    "ci.cache.dependency-cache",
    "ci.cache.build-cache",
    "ci.checkout.shallow-clone",
    "ci.parallel.test-sharding",
    "ci.build.change-scoped",
    "ci.trigger.concurrency-groups",
    "ci.trigger.cancel-superseded",
    "ci.trigger.path-filter",
    "ci.hygiene.job-timeouts",
    "ci.security.scoped-id-token",
    "ci.security.pinned-action-shas",
]

# The live /best-practices/github-actions/ pages. Every slug the spec cites
# must be one of these; the one page with no check must be accounted for.
SITE_PRACTICE_SLUGS = {
    "cache-dependencies", "shallow-checkout", "shard-tests", "build-only-affected",
    "path-filter-workflows", "cancel-superseded-runs", "keep-advisory-checks-non-blocking",
    "cut-queue-time", "pin-action-shas", "scope-id-token-per-job",
    # shipped after the v0.1.1 freeze (OD-CS21 accounted them in the note):
    "bound-job-timeouts", "replace-fixed-sleeps-with-polling",
}


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(_SPEC_PATH.read_text())


def test_spec_version(spec):
    assert spec["spec_version"] == "ci-score-v0.1.8"


def test_registry_is_the_eleven_basic_checks(spec):
    # v0.1.2 (OD-CS18) removed ci.trigger.draft-gate: 12 -> 11 checks.
    assert [c["check_id"] for c in spec["checks"]] == EXPECTED_CHECK_IDS
    assert len(spec["checks"]) == 11
    assert "ci.trigger.draft-gate" not in EXPECTED_CHECK_IDS


def test_every_check_states_its_fact(spec):
    """A check IS its fact: the one-sentence thing a maintainer verifies in
    their own YAML. An empty fact is an unreviewable check."""
    for check in spec["checks"]:
        assert check["fact"].strip(), check["check_id"]


def test_practice_slugs_resolve_and_every_page_is_accounted_for(spec):
    cited = {c["practice_slug"] for c in spec["checks"] if c.get("practice_slug")}
    assert cited <= SITE_PRACTICE_SLUGS, cited - SITE_PRACTICE_SLUGS
    # The owner's directive: every published practice is IN the score — except
    # the one that cannot be a config fact, which must be named, not missing.
    unmapped = SITE_PRACTICE_SLUGS - cited
    # Three pages carry no registry practice_slug binding, for three different
    # reasons the note must state (OD-CS21 — the note rotted once when pages
    # shipped after it was written; this census is what keeps it honest):
    # no-check-possible, no-check-in-v0.1.x, and scored-but-null-binding.
    assert unmapped == {"keep-advisory-checks-non-blocking",
                        "replace-fixed-sleeps-with-polling",
                        "bound-job-timeouts"}
    note = spec["practice_coverage_note"]
    for slug in sorted(unmapped):
        assert slug in note, f"unmapped page {slug!r} not accounted for in the note"
    # bound-job-timeouts is the odd one out: scored, null binding — the note
    # must name the scoring check so a reader can confirm it is not a gap.
    assert "ci.hygiene.job-timeouts" in note


def test_the_nothing_to_check_refusals(spec):
    """OD-CS15: config facts need no run history, no branch-protection access,
    no merge-queue calibration — every repo with REAL CI scores on a fresh run.
    The refusals are the two nothing-to-check shapes plus OD-CS20's
    automation-only refusal (workflows that do no build or test)."""
    assert [r["reason_code"] for r in spec["refusals"]] == [
        "no_workflow_yaml", "facts_unavailable", "automation_only"]


def test_score_formula_is_passed_over_applicable(spec):
    f = spec["formula"]
    assert set(f["check_states"]) == {"pass", "fail", "not_applicable"}
    assert "checks_passed / checks_applicable" in f["score"]
    assert "ROUND HALF UP" in f["rounding"]
    assert "self-verify" in f["rules"]["config_facts_only"]
    assert "never changes a state" in f["rules"]["evidence_display_only"]


# --- the band arithmetic carries over unchanged from the granular design ------

def test_bands_are_contiguous_and_cover_zero_to_one_hundred(spec):
    bands = sorted(spec["bands"], key=lambda b: b["min"])
    assert bands[0]["min"] == 0 and bands[-1]["max"] == 100
    for lower, upper in zip(bands, bands[1:]):
        assert upper["min"] == lower["max"] + 1, f"gap/overlap at {lower['grade']}"
    assert [b["grade"] for b in bands] == ["F", "D", "C", "B", "A"]


def test_every_score_maps_to_exactly_one_grade(spec):
    for value in range(0, 101):
        hits = []
        for band in spec["bands"]:
            for suffix, span in (("-", band.get("minus")), ("", band.get("bare")), ("+", band.get("plus"))):
                if span and span[0] <= value <= span[1]:
                    hits.append(band["grade"] + suffix)
        assert len(hits) == 1, f"score {value} maps to {hits}"


# --- the decision record and the punt must stay legible in the artifact -------

def test_od_cs15_records_the_reset_with_the_owner_directives(spec):
    od = next(d for d in spec["decision_log"] if d["id"] == "OD-CS15")
    assert len(od["owner_directives"]) == 4
    assert any("score any repo" in d for d in od["owner_directives"])
    assert any("every best practice" in d for d in od["owner_directives"])
    # The history stays: the superseded decisions' records are not erased.
    ids = {d["id"] for d in spec["decision_log"]}
    assert "OD-CS13" in ids


def test_v2_map_names_the_punted_machinery(spec):
    punted = " ".join(spec["v2_map"]["punted"])
    for phrase in ("measured-magnitude", "occurrence-ratio", "tiers and category weights",
                   "9-refusal", "queue-cause attribution"):
        assert phrase in punted, phrase


def test_stamp_schema_is_the_flat_check_list(spec):
    stamp = spec["stamp_schema"]
    for key in ("spec_version", "scope_statement", "value", "grade", "refusal",
                "checks", "checks_passed", "checks_applicable"):
        assert key in stamp
    check = stamp["checks"][0]
    assert "evidence" in check and "measured_note" in check


def test_changelog_records_the_reset(spec):
    assert any("OD-CS15 basic reset" in e["change"] for e in spec["changelog"])
    # Historical entries keep the version they shipped under; the LAST entry
    # must match the current version (every bump records itself).
    assert spec["changelog"][-1]["version"] == spec["spec_version"]
    for e in spec["changelog"]:
        assert re.fullmatch(r"ci-score-v\d+\.\d+(\.\d+)?", e["version"]), e["version"]


# --- surface consistency -------------------------------------------------------

def test_methodology_and_skill_md_describe_the_basic_rubric():
    m = re.sub(r"\s+", " ", _METHODOLOGY_PATH.read_text())
    assert "pass/fail" in m and "configuration" in m.lower()
    assert "13-check" not in m, "the granular canonical description survived in the methodology"
    sk = re.sub(r"\s+", " ", _SKILL_MD_PATH.read_text())
    assert "13-check" not in sk, "the granular canonical description survived in SKILL.md"


def _contains_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, list):
        return any(_contains_number(item) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_number(item)
            for pair in value.items()
            for item in pair
        )
    return False


def _methodology_row_matches(
    name: str, methodology_value: str, rows: list[str]
) -> bool:
    row = next((line for line in rows if line.startswith(f"| `{name}` |")), "")
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    return len(cells) >= 2 and cells[:2] == [f"`{name}`", methodology_value]


def _methodology_value(name: str, value: object) -> str:
    """Derive the human-facing table cell from the machine contract."""

    if isinstance(value, int) and not isinstance(value, bool):
        suffix = " bytes" if name == "_SCRIPT_MAX_BYTES" else ""
        return f"{value:,}{suffix}"
    if name == "_SETUP_CACHE_INPUTS" and isinstance(value, dict):
        return "; ".join(
            f"{action.rsplit('/', 1)[-1]} "
            f"{settings[1] if settings[1] is not None else 'off'}"
            for action, settings in value.items()
        )
    if name == "_PLAUSIBLE_MAJOR" and isinstance(value, dict):
        return "; ".join(
            f"{action.rsplit('/', 1)[-1]} {major}"
            for action, major in value.items()
        )
    raise AssertionError(f"no methodology display rule for {name}")


def test_numeric_dictionary_keys_are_part_of_the_constant_contract():
    assert _contains_number({64: "limit"})


def test_methodology_value_must_match_the_whole_value_cell():
    rows = ["| `_MAX_PARALLEL_DEPTH` | 640 | reason |"]
    assert not _methodology_row_matches("_MAX_PARALLEL_DEPTH", "64", rows)


_CONSTANT_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.LShift: operator.lshift, ast.RShift: operator.rshift,
    ast.BitOr: operator.or_, ast.BitAnd: operator.and_,
    ast.BitXor: operator.xor,
}
_CONSTANT_UNARYOPS = {
    ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert,
}


def _numeric_constants_in(source: str) -> dict[str, object]:
    """Return module constants in ``source`` whose literal value has a number.

    Public and private names count. The small evaluator accepts data literals,
    unary and binary arithmetic, and names of constants already evaluated
    above; calls such as ``chr(0xFEFF)`` are strings at runtime and stay out
    of this grade/read-bound contract. Chained (``_A = _B = 3``), annotated
    and augmented assignments are all read, so no spelling of a new numeric
    constant escapes the record.
    """

    known: dict[str, object] = {}

    def evaluate(node: ast.AST) -> object:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [evaluate(item) for item in node.elts]
        if isinstance(node, ast.Dict):
            if any(key is None for key in node.keys):
                raise ValueError
            return {
                evaluate(key): evaluate(value)
                for key, value in zip(node.keys, node.values)
            }
        if isinstance(node, ast.Name) and node.id in known:
            return known[node.id]
        if isinstance(node, ast.UnaryOp) and type(node.op) in _CONSTANT_UNARYOPS:
            return _CONSTANT_UNARYOPS[type(node.op)](evaluate(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in _CONSTANT_BINOPS:
            return _CONSTANT_BINOPS[type(node.op)](
                evaluate(node.left), evaluate(node.right))
        raise ValueError

    constants: dict[str, object] = {}
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign):
            targets, value_node = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value_node = [statement.target], statement.value
        elif isinstance(statement, ast.AugAssign):
            targets = [statement.target]
            value_node = ast.BinOp(
                left=ast.Name(id=getattr(statement.target, "id", ""),
                              ctx=ast.Load()),
                op=statement.op, right=statement.value)
        else:
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        try:
            value = evaluate(value_node)
        except (TypeError, ValueError, ArithmeticError):
            for name in names:
                known.pop(name, None)
                constants.pop(name, None)
            continue
        for name in names:
            known[name] = value
            if _contains_number(value):
                constants[name] = value
            else:
                constants.pop(name, None)
    return constants


def _literal_numeric_constants() -> dict[str, object]:
    """The live engine's numeric module constants (see `_numeric_constants_in`)."""

    return _numeric_constants_in((_SCRIPTS_DIR / "practice_facts.py").read_text())


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("_NEG = -1\n", {"_NEG": -1}),
        ("_SUM = 60 + 4\n", {"_SUM": 64}),
        ("_SHIFT = 1 << 20\n", {"_SHIFT": 1 << 20}),
        ("_A = _B = 3\n", {"_A": 3, "_B": 3}),
        ("MAX_PUBLIC = 7\n", {"MAX_PUBLIC": 7}),
        ("_LIMIT: int = 8\n", {"_LIMIT": 8}),
        ("_BASE = 4\n_DERIVED = _BASE * 2 - 1\n", {"_BASE": 4, "_DERIVED": 7}),
        ("_TABLE = {'a': -(2 ** 3)}\n", {"_TABLE": {"a": -8}}),
        ("_STEP = 1\n_STEP += 1\n", {"_STEP": 2}),
    ],
    ids=["unary-minus", "addition", "shift", "chained", "public", "annotated",
         "known-name", "nested", "augmented"],
)
def test_constant_scan_records_every_numeric_form(source, expected):
    # Each form must reach the contract, so a new constant written this way
    # fails the spec test until it is recorded under engine_constants.
    assert _numeric_constants_in(source) == expected


def test_constant_scan_skips_runtime_values():
    source = "_BOM = chr(0xFEFF)\n_FLAG = True\n_NAME = 'x'\n_RE = re.compile('a')\n"
    assert _numeric_constants_in(source) == {}


def test_every_numeric_practice_fact_constant_is_bound_to_the_spec(spec):
    recorded = spec.get("engine_constants", {})
    live = _literal_numeric_constants()
    assert not (live.keys() - recorded.keys()), (
        "unrecorded module-level numeric constants: "
        f"{sorted(live.keys() - recorded.keys())}"
    )
    assert not (recorded.keys() - live.keys()), (
        f"stale engine constants: {sorted(recorded.keys() - live.keys())}"
    )
    for name, value in live.items():
        assert recorded[name]["value"] == value, name
        assert recorded[name]["reason"].strip(), name


def test_methodology_lists_the_numeric_engine_contract(spec):
    rows = _METHODOLOGY_PATH.read_text().splitlines()
    for name, entry in spec.get("engine_constants", {}).items():
        expected = _methodology_value(name, entry["value"])
        assert entry["methodology_value"] == expected, name
        assert _methodology_row_matches(name, expected, rows), name


# Every prose copy of a bound engine value, tied to its constant. Each entry is
# (constant, pattern whose one group is the number). The counts pin how many
# copies each surface carries, so a copy reworded out of a pattern's reach
# fails here instead of silently going stale.
_PROSE_BINDINGS = (
    ("_MAX_PARALLEL_DEPTH", r"contains itself, nested more than ([\d,]+) deep"),
    ("_MAX_WALK_STEPS", r"past the ([\d,]+)-entry read"),
    ("_MAX_LOCAL_ACTION_DEPTH",
     r"(?:sits|a chain|in the wrong case, or) more than ([\d,]+)(?: calls)? deep"),
)
_PROSE_COUNTS = {
    "methodology": {"_MAX_PARALLEL_DEPTH": 1, "_MAX_WALK_STEPS": 1,
                    "_MAX_LOCAL_ACTION_DEPTH": 4},
    "spec facts": {"_MAX_LOCAL_ACTION_DEPTH": 4},
}
# Any phrase of these shapes must be claimed by a binding above.
_BOUND_PHRASE_RE = re.compile(r"more than [\d,]+(?: calls)? deep|[\d,]+-entry")


def _prose_bound_value_errors(
    surface: str, text: str, constants: dict[str, object]
) -> list[str]:
    """Name every prose copy of a bound value that disagrees with its constant."""

    errors: list[str] = []
    claimed: set[tuple[int, int]] = set()
    counts: dict[str, int] = {}
    for name, pattern in _PROSE_BINDINGS:
        for match in re.finditer(pattern, text):
            counts[name] = counts.get(name, 0) + 1
            claimed.add(match.span(1))
            if int(match.group(1).replace(",", "")) != constants[name]:
                errors.append(
                    f"{surface}: {match.group(0)!r} disagrees with "
                    f"{name} = {constants[name]}")
    for match in _BOUND_PHRASE_RE.finditer(text):
        number = re.search(r"[\d,]+", match.group(0))
        span = (match.start() + number.start(), match.start() + number.end())
        if span not in claimed:
            errors.append(f"{surface}: {match.group(0)!r} is bound to no constant")
    expected = _PROSE_COUNTS.get(surface, {})
    if counts != expected:
        errors.append(f"{surface}: prose copies {counts} != pinned {expected}")
    return errors


def _prose_surfaces(spec: dict) -> dict[str, str]:
    return {
        "methodology": _METHODOLOGY_PATH.read_text(),
        "spec facts": "\n".join(check["fact"] for check in spec["checks"]),
    }


def test_prose_copies_of_bound_values_match_their_constants(spec):
    live = _literal_numeric_constants()
    errors = [
        error
        for surface, text in _prose_surfaces(spec).items()
        for error in _prose_bound_value_errors(surface, text, live)
    ]
    assert not errors, errors


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("sits more than 16 calls deep", "sits more than 17 calls deep"),
        ("a chain more than 16 deep", "a chain more than 17 deep"),
        ("in the wrong case, or more than 16 deep",
         "in the wrong case, or more than 17 deep"),
        ("nested more than 64 deep", "nested more than 65 deep"),
        ("the 10,000-entry read", "the 10,001-entry read"),
        ("a chain more than 16 deep", "a chain deeper than 16"),
    ],
)
def test_any_drifted_prose_copy_fails_the_binding(spec, old, new):
    live = _literal_numeric_constants()
    text = _METHODOLOGY_PATH.read_text()
    assert old in text
    assert _prose_bound_value_errors("methodology", text.replace(old, new, 1), live)


def test_a_moved_constant_fails_the_unchanged_prose(spec):
    for name in ("_MAX_PARALLEL_DEPTH", "_MAX_WALK_STEPS",
                 "_MAX_LOCAL_ACTION_DEPTH"):
        moved = {**_literal_numeric_constants()}
        moved[name] += 1
        assert any(
            name in error
            for surface, text in _prose_surfaces(spec).items()
            for error in _prose_bound_value_errors(surface, text, moved)
        ), name


def test_scorer_module_is_basic_sized():
    """The reset's debuggability claim, kept honest: the scorer must stay
    small enough to read in one sitting. (607 lines was the granular design.)"""
    lines = len((_SCRIPTS_DIR / "ci_score.py").read_text().splitlines())
    assert lines < 200, f"ci_score.py is {lines} lines — the basic design is growing back"

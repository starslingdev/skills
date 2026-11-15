"""CI splits the suite into parts (`--shard K/N`, root conftest.py). Splitting may
only change WHERE a test runs, never WHETHER it runs: every file must land in
exactly one part, whatever the weights say, and every worker must compute the
same split."""
import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("_root_conftest", _ROOT / "conftest.py")
_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conftest)

_FILES = [f"tests/test_{i:03d}.py" for i in range(50)]
_WEIGHTS = {f: float((i * 7) % 13 + 1) for i, f in enumerate(_FILES[:40])}  # 10 unknown


@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_every_file_lands_in_exactly_one_part(n):
    assignment = _conftest.assign_shards(_FILES, _WEIGHTS, n)
    assert set(assignment) == set(_FILES)
    assert set(assignment.values()) <= set(range(1, n + 1))
    parts = [{f for f, k in assignment.items() if k == part} for part in range(1, n + 1)]
    assert sum(len(p) for p in parts) == len(_FILES)
    assert set().union(*parts) == set(_FILES)


def test_split_does_not_depend_on_arrival_order():
    """xdist workers each collect the suite; they must all keep the same part."""
    forward = _conftest.assign_shards(_FILES, _WEIGHTS, 2)
    backward = _conftest.assign_shards(list(reversed(_FILES)), _WEIGHTS, 2)
    assert forward == backward


def test_files_missing_from_the_weights_still_run():
    assignment = _conftest.assign_shards(["tests/test_new.py"], {}, 2)
    assert assignment == {"tests/test_new.py": 1}


def test_parts_are_balanced_by_weight():
    weights = {"a.py": 10.0, "b.py": 6.0, "c.py": 4.0}
    assignment = _conftest.assign_shards(weights, weights, 2)
    load = {1: 0.0, 2: 0.0}
    for f, part in assignment.items():
        load[part] += weights[f]
    assert load == {1: 10.0, 2: 10.0}


def test_committed_weights_cover_real_test_files():
    """A weights file keyed by paths that no longer exist balances nothing."""
    import json
    weights = json.loads((_ROOT / "tests" / "shard_weights.json").read_text("utf-8"))
    assert weights, "tests/shard_weights.json is empty"
    present = [f for f in weights if (_ROOT / f).is_file()]
    assert len(present) >= 0.9 * len(weights), (
        "most entries in tests/shard_weights.json name files that no longer exist; "
        "refresh it (see the sharding note in conftest.py)")


@pytest.mark.parametrize("value", ["1/2", "2/2", "1/1", "3/5"])
def test_valid_shard_values_parse(value):
    k, n = _conftest.parse_shard(value)
    assert f"{k}/{n}" == value


@pytest.mark.parametrize("value", ["0/2", "3/2", "1/0", "2", "a/b", "1/2/3", ""])
def test_invalid_shard_values_are_rejected(value):
    with pytest.raises(pytest.UsageError):
        _conftest.parse_shard(value)

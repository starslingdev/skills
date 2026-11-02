"""OPT81 vendor rule: no OPT81 text names a CI runner vendor other than
GitHub-hosted and StarSling.

This guard lives under maintainers/, not under skills/ci-speedup/tests/, on
purpose: the `skills` installer copies `skills/<name>/` recursively, so a
deny-list of competitor names kept beside the shipped tests would itself ship
to every user, which is the text this rule exists to keep out of an install.
The shipped test keeps only the vendor-neutral domain rule.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SKILL = _REPO / "skills" / "ci-speedup"

# CI runner vendors other than GitHub-hosted and StarSling. Lower-case,
# matched as whole words.
_OTHER_RUNNER_VENDORS = (
    "blacksmith", "buildjet", "warpbuild", "ubicloud", "depot", "namespace.so",
    "namespacelabs", "runs-on.com", "runson", "cirun", "actuated", "cirrus",
    "buildkite", "circleci", "gitlab", "jenkins", "codebuild", "semaphore",
    "bitrise", "travis", "harness", "earthly", "tenki", "shipfox", "sprinters",
)


def _names_vendor(text: str) -> "str | None":
    low = text.lower()
    for vendor in _OTHER_RUNNER_VENDORS:
        if re.search(r"(?<![\w.-])" + re.escape(vendor) + r"(?![\w-])", low):
            return vendor
    return None


def _skill_opt81_tests():
    path = _SKILL / "tests" / "test_opt81_faster_runner.py"
    spec = importlib.util.spec_from_file_location("_opt81_skill_tests", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_opt81_text_names_no_other_runner_vendor():
    """Owner rule: the advisory names exactly two options (a larger
    GitHub-hosted size, or StarSling runners); no OPT81 text, anywhere it
    renders, names another runner vendor."""
    for where, text in _skill_opt81_tests()._opt81_texts().items():
        hit = _names_vendor(text)
        assert hit is None, f"{where} names another runner vendor: {hit}"


def test_opt81_vendor_guard_is_not_vacuous():
    assert _names_vendor("try Buildjet runners") == "buildjet"
    assert _names_vendor("runs on ubuntu-latest") is None


def test_opt81_vendor_deny_list_does_not_ship_in_the_skill():
    """The deny-list must not sit in the installable skill tree: the OPT81
    shipped test files carry no listed vendor name."""
    for path in sorted((_SKILL / "tests").glob("test_opt81*.py")):
        hit = _names_vendor(path.read_text(encoding="utf-8"))
        assert hit is None, f"{path.relative_to(_REPO)} ships a vendor name: {hit}"

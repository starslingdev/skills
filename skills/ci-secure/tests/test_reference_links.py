"""Every reference file SKILL.md points at must actually exist.

SKILL.md moved four load-bearing runbooks (the CI-gate procedure, the terminal
summary's provenance, the findings-JSON shape, troubleshooting) out of its own
body and behind links. The install-surface guards assert maintainer infra is
ABSENT; nothing asserted the shipped reference set is PRESENT. A rename or a
packaging slip would ship a SKILL.md whose "Read the full runbook before doing
anything" resolves to nothing, with a green suite.
"""

import re
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent.parent
_SKILL_MD = _SKILL_DIR / "SKILL.md"

# [text](target) — markdown inline links.
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _relative_link_targets(text: str) -> list[str]:
    """Repo-relative link targets only: no scheme, no bare anchor. The `#anchor`
    suffix is stripped — this guard is about the FILE existing."""
    out = []
    for raw in _LINK.findall(text):
        if "://" in raw or raw.startswith("#") or raw.startswith("mailto:"):
            continue
        out.append(raw.split("#", 1)[0])
    return [t for t in out if t]


def _docs_under_test() -> list[Path]:
    return [_SKILL_MD] + sorted((_SKILL_DIR / "references").glob("*.md"))


def test_every_link_in_the_shipped_docs_resolves_to_a_real_file():
    """SKILL.md and every reference it ships must not point at a missing file."""
    broken = []
    for doc in _docs_under_test():
        for target in _relative_link_targets(doc.read_text(encoding="utf-8")):
            if not (doc.parent / target).resolve().exists():
                broken.append(f"{doc.relative_to(_SKILL_DIR)} -> {target}")
    assert not broken, (
        "shipped ci-secure docs link to files that do not exist: "
        + "; ".join(broken)
    )


def test_skill_md_still_links_every_reference_it_depends_on():
    """The four runbooks this skill's contract defers to are named explicitly, so
    dropping a link (rather than the file) is caught too. These are the pointers
    the agent must be able to follow at runtime; a bare rename of one of them
    silently strips a rule out of the contract."""
    text = _SKILL_MD.read_text(encoding="utf-8")
    for required in (
        "references/ci-gate.md",
        "references/terminal-summary.md",
        "references/scan-output.md",
        "references/troubleshooting.md",
        "references/security-patterns.md",
        "references/why-these-ten.md",
        "references/scenario-authoring.md",
    ):
        assert required in text, (
            f"SKILL.md no longer links {required} — the rules it holds are "
            "unreachable from the always-loaded contract."
        )


def test_the_link_guard_actually_fires_on_a_missing_target():
    """Red-proof: the same extraction + existence predicate must FAIL on a doc
    that links a file which is not there, so the guard cannot go tautological."""
    targets = _relative_link_targets("see [x](references/does-not-exist.md) now")
    assert targets == ["references/does-not-exist.md"]
    assert not (_SKILL_DIR / targets[0]).exists()

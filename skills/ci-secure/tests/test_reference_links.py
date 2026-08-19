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


# The runbooks SKILL.md defers a RULE to, rather than merely cites for colour.
# Each must be reachable as a real link target from the always-loaded body, and
# each must still carry its rules — an existing-but-empty file satisfies "the
# file is there" while the contract behind the pointer is gone.
_REQUIRED_REFERENCES = (
    "references/ci-gate.md",
    "references/terminal-summary.md",
    "references/scan-output.md",
    "references/troubleshooting.md",
    "references/security-patterns.md",
    "references/why-these-ten.md",
    "references/scenario-authoring.md",
)


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
    """The runbooks this skill's contract defers to are named explicitly, so
    dropping a link (rather than the file) is caught too. These are the pointers
    the agent must be able to follow at runtime; a bare rename of one of them
    silently strips a rule out of the contract.

    The assertion is on the link TARGETS, not on the page text. A substring
    check passes when the path survives only as anchor text, as a backticked
    prose mention, or as the label on a link pointing somewhere else entirely —
    all three of which leave the agent unable to follow the pointer, which is
    the whole failure this guard exists to catch.
    """
    targets = set(_relative_link_targets(_SKILL_MD.read_text(encoding="utf-8")))
    for required in _REQUIRED_REFERENCES:
        assert required in targets, (
            f"SKILL.md no longer links {required} as a followable target — the "
            "rules it holds are unreachable from the always-loaded contract. "
            f"Targets found: {sorted(targets)}"
        )


def test_every_deferred_runbook_still_carries_its_rules():
    """Existence is not enough. `references/ci-gate.md` can be truncated to zero
    bytes and every other guard here stays green, while SKILL.md still orders the
    agent to "read the full runbook before doing anything". A stub or emptied
    reference is a contract with a hole in it, so assert each deferred runbook
    still has substance: a top-level heading and enough prose to be a runbook."""
    thin = []
    for required in _REQUIRED_REFERENCES:
        doc = _SKILL_DIR / required
        body = doc.read_text(encoding="utf-8") if doc.exists() else ""
        lines = [ln for ln in body.splitlines() if ln.strip()]
        if len(lines) < 20 or not any(ln.startswith("#") for ln in lines):
            thin.append(f"{required} ({len(lines)} non-blank lines)")
    assert not thin, (
        "SKILL.md defers rules to these references, but they no longer carry "
        "them (empty, stubbed, or heading-less): " + "; ".join(thin)
    )


def test_the_link_guard_actually_fires_on_a_missing_target():
    """Red-proof: the same extraction + existence predicate must FAIL on a doc
    that links a file which is not there, so the guard cannot go tautological."""
    targets = _relative_link_targets("see [x](references/does-not-exist.md) now")
    assert targets == ["references/does-not-exist.md"]
    assert not (_SKILL_DIR / targets[0]).exists()

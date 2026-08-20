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


def _anchored_link_targets(text: str) -> list[tuple[str, str]]:
    """(file, anchor) for every repo-relative link that names a section. The
    file half may be empty for a same-document link."""
    out = []
    for raw in _LINK.findall(text):
        if "://" in raw or raw.startswith("mailto:") or "#" not in raw:
            continue
        path, _, anchor = raw.partition("#")
        if anchor:
            out.append((path, anchor))
    return out


def _outside_code_fences(text: str) -> list[str]:
    """Lines with fenced code blocks removed.

    A `#` comment inside a ```bash example is indistinguishable from a `#`
    markdown heading when a guard reads line by line, and these docs are full
    of shell examples whose comments start at column 0. Counting one as a
    heading invents an anchor: a link to `#the-self-proof` could resolve
    against a shell comment instead of the section, so renaming the real
    heading would pass the check by accident. Nothing collides today; the
    guards were simply weaker than their docstrings claimed.
    """
    out, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return out


def _heading_slugs(text: str) -> set[str]:
    """GitHub's heading-anchor slug rules: lowercase, drop anything that is not
    a word character, space or hyphen, then turn EACH remaining space into a
    hyphen. Runs are not collapsed — an em dash between two words leaves the
    spaces that surrounded it, which is why the real anchors carry `--`."""
    slugs = set()
    for line in _outside_code_fences(text):
        if not line.startswith("#"):
            continue
        heading = line.lstrip("#").strip()
        slug = re.sub(r"[^\w\s-]", "", heading.lower()).strip()
        slugs.add(re.sub(r"\s", "-", slug))
    return slugs


def _docs_under_test() -> list[Path]:
    return [_SKILL_MD] + sorted((_SKILL_DIR / "references").glob("*.md"))


# The runbooks SKILL.md defers a RULE to, rather than merely cites for colour.
# Each must be reachable as a real link target from the always-loaded body, and
# each must still carry its rules — an existing-but-empty file satisfies "the
# file is there" while the contract behind the pointer is gone.
_REQUIRED_REFERENCES = (
    # Phase 5's fix-subagent prompt. It is the only phase that writes to the
    # user's repository, and this file carries the containment that makes that
    # safe: the UNTRUSTED-REPO-CONTENT markers and the "edit ONLY the finding's
    # workflow_file" scope limit. If any deferred reference has to be reachable,
    # it is this one.
    "references/prompts.md",
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


def test_every_section_link_resolves_to_a_real_heading():
    """A link that names a section is a pointer to a RULE, not to a file. SKILL.md
    sends the agent to "Adding a pattern — the mechanical checklist" inside a
    ~230-line reference; renaming or deleting that heading lands the agent at the
    top of the file with no sign anything is missing, which is worse than a link
    that visibly 404s. File existence alone does not catch it."""
    dangling = []
    for doc in _docs_under_test():
        text = doc.read_text(encoding="utf-8")
        for path, anchor in _anchored_link_targets(text):
            target = (doc.parent / path).resolve() if path else doc
            if not target.exists():
                continue  # the existence guard above owns this failure
            if anchor.lower() not in _heading_slugs(target.read_text(encoding="utf-8")):
                dangling.append(f"{doc.relative_to(_SKILL_DIR)} -> {path}#{anchor}")
    assert not dangling, (
        "shipped ci-secure docs link to sections that no longer exist — the "
        "rule behind the pointer is unreachable: " + "; ".join(dangling)
    )


# Per-file substance, because one global floor protects the big files and
# almost nothing else. The old rule was "20 non-blank lines or more", and
# `troubleshooting.md` has 26 — so most of its failure-mode table could be
# deleted with the suite green. Each floor is 90% of what the file carried when
# this was written, and the section count is what it had; a deliberate trim
# below either fails once and the number is updated consciously, which is the
# same contract the scanner's ignore list carries.
_REFERENCE_SUBSTANCE = {
    "references/prompts.md": (97, 2),
    "references/ci-gate.md": (226, 8),
    "references/terminal-summary.md": (46, 4),
    "references/scan-output.md": (70, 4),
    "references/troubleshooting.md": (23, 1),
    "references/security-patterns.md": (521, 4),
    "references/why-these-ten.md": (145, 7),
    "references/scenario-authoring.md": (63, 2),
}


def test_every_deferred_runbook_still_carries_its_rules():
    """Existence is not enough. `references/ci-gate.md` can be truncated to zero
    bytes and every other guard here stays green, while SKILL.md still orders the
    agent to "read the full runbook before doing anything". A stub, an emptied
    reference, or a file quietly stripped of half its table is a contract with a
    hole in it, so hold each deferred runbook to its OWN floor and section
    count rather than to one global minimum."""
    thin = []
    for required in _REQUIRED_REFERENCES:
        doc = _SKILL_DIR / required
        body = doc.read_text(encoding="utf-8") if doc.exists() else ""
        # Fenced blocks COUNT as substance — `prompts.md` is mostly one fenced
        # prompt, and that prompt is the containment rule. Fence-awareness is
        # only for telling a heading from a shell comment.
        lines = [ln for ln in body.splitlines() if ln.strip()]
        sections = [ln for ln in _outside_code_fences(body)
                    if ln.startswith("## ")]
        floor, want_sections = _REFERENCE_SUBSTANCE.get(required, (20, 0))
        if len(lines) < floor:
            thin.append(f"{required}: {len(lines)} substantive lines, floor {floor}")
        elif len(sections) < want_sections:
            thin.append(
                f"{required}: {len(sections)} sections, had {want_sections}")
        elif not any(ln.startswith("#") for ln in _outside_code_fences(body)):
            thin.append(f"{required}: no headings at all")
    assert not thin, (
        "SKILL.md defers rules to these references, but they no longer carry "
        "them (emptied, stubbed, or stripped of sections): " + "; ".join(thin)
        + ". If the trim is deliberate, update _REFERENCE_SUBSTANCE in the same "
        "change so the reduction is a decision rather than a silent loss."
    )


def test_the_substance_floor_is_pinned_to_every_required_reference():
    """A floor nobody set is the 20-line default, which is the weak rule this
    replaced. Every required reference must carry its own number."""
    unpinned = [r for r in _REQUIRED_REFERENCES if r not in _REFERENCE_SUBSTANCE]
    assert not unpinned, (
        "these required references have no substance floor of their own, so "
        "they fall back to a global minimum that protects almost nothing: "
        + ", ".join(unpinned))

def test_the_link_guard_actually_fires_on_a_missing_target():
    """Red-proof: the same extraction + existence predicate must FAIL on a doc
    that links a file which is not there, so the guard cannot go tautological."""
    targets = _relative_link_targets("see [x](references/does-not-exist.md) now")
    assert targets == ["references/does-not-exist.md"]
    assert not (_SKILL_DIR / targets[0]).exists()


# --- a pointer is only as good as the section it points INTO -----------------

# The line-count floor above catches an emptied file. It does not catch the
# failure this branch was written to prevent: a merge that silently drops ONE
# section from a reference while the rest of the file survives. Deleting `## The
# self-proof` from `ci-gate.md` — 81 lines, the exact section this branch
# narrates rescuing — left the whole suite green, while SKILL.md went on
# ordering the agent through three outcomes whose mechanics no longer existed.
#
# So bind the pointer to its content: every branch label SKILL.md tells the
# agent to act on must be present in the runbook that explains it. A label is a
# string the agent matches against real output, which makes it exactly the kind
# of anchor that cannot drift silently.
_SELF_PROOF_LABELS = ("self-proof PASSED", "self-proof FAILED",
                      "self-proof COULD NOT RUN")


def test_the_self_proof_runbook_still_explains_every_outcome_skill_md_branches_on():
    """SKILL.md branches on three self-proof outcomes and defers their mechanics
    to `references/ci-gate.md`. If that section is dropped, the branches become
    instructions to interpret output nothing documents."""
    skill = _SKILL_MD.read_text(encoding="utf-8")
    gate = (_SKILL_DIR / "references" / "ci-gate.md").read_text(encoding="utf-8")

    branched_on = [lbl for lbl in _SELF_PROOF_LABELS if lbl in skill]
    assert branched_on, (
        "SKILL.md no longer names any self-proof outcome — if the contract "
        "moved, move this guard with it rather than deleting it"
    )
    missing = [lbl for lbl in branched_on if lbl not in gate]
    assert not missing, (
        "SKILL.md tells the agent to act on these self-proof outcomes, but "
        "references/ci-gate.md no longer explains them: " + ", ".join(missing)
        + ". A section can be dropped from a reference while the file keeps "
        "enough lines to pass the substance floor — that is how the self-proof "
        "runbook was nearly lost once already."
    )
    assert "## The self-proof" in gate, (
        "references/ci-gate.md lost its `## The self-proof` heading; the "
        "table of contents and SKILL.md's deep link both point at it"
    )


# --- a command block cannot expand a name the contract never binds -----------

# Phase 2 expanded `${REPO:+--repo}` while the only assignment of `REPO` had
# moved into `references/troubleshooting.md`. An agent that did not open that
# reference ran the scan with no `--repo`, and the four gh-gated checks
# silently degraded to "skipped" on a repository where full coverage was
# available. Safe direction — a skip is never a false pass — but a real
# coverage loss, and invisible: no link was broken and every other guard was
# green. This is the trim's characteristic failure, so it is pinned.
#
# The environment supplies these; the contract is not expected to assign them.
_AMBIENT_SHELL_VARS = {"TMPDIR", "HOME", "PATH", "PWD", "USER", "SHELL"}
_VAR_EXPANSION = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)")
_VAR_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, re.DOTALL)


def test_every_shell_variable_skill_md_expands_is_bound_in_skill_md():
    """Every name a command block expands must be assigned somewhere in the
    always-loaded body — not behind a link. A reference can explain HOW a value
    is derived; it cannot be the only place the name exists, or skipping the
    reference produces a silently degraded run instead of a stop."""
    blocks = _bash_blocks(_SKILL_MD.read_text(encoding="utf-8"))
    assert blocks, "no ```bash blocks found in SKILL.md — the extractor drifted"
    joined = "\n".join(blocks)
    expanded = {v for v in _VAR_EXPANSION.findall(joined)}
    assigned = set(_VAR_ASSIGNMENT.findall(joined))
    unbound = sorted(expanded - assigned - _AMBIENT_SHELL_VARS)
    assert not unbound, (
        "SKILL.md's command blocks expand shell variable(s) that nothing in "
        "SKILL.md assigns: " + ", ".join(unbound) + ". An agent that does not "
        "open the reference explaining them runs the command with the value "
        "empty, which degrades coverage silently instead of stopping. Bind the "
        "name in the body; the reference can still carry the derivation."
    )


def test_a_shell_comment_in_an_example_is_not_read_as_a_heading():
    """Red-proof for the fence-tracking above. Without it the `# Install` line
    inside the code block below becomes the anchor `install`, so a link to a
    section that no longer exists would resolve against a shell comment."""
    doc = "\n".join([
        "## Real Heading",
        "",
        "```bash",
        "# Install",
        "make install",
        "```",
        "",
    ])
    slugs = _heading_slugs(doc)
    assert "real-heading" in slugs, "the real heading must still be found"
    assert "install" not in slugs, (
        "a shell comment inside a fenced block was counted as a heading — a "
        "renamed section could then resolve against an example's comment"
    )


def test_the_contract_says_what_to_do_when_a_deferred_file_cannot_be_read():
    """The failure mode the trim created, and the rule that answers it.

    SKILL.md defers load-bearing procedure to reference files in several
    places, each phrased as an instruction to read the file before acting. When
    that procedure was inline, "the reference cannot be read" was not a state
    the contract could reach; behind a pointer it is — a partial install, a
    packaging slip, a truncated copy — and without a rule the agent improvises
    the runbook, which is precisely what the reference exists to prevent.

    Guarding the rule's PRESENCE rather than its wording: this is prose an
    agent obeys, so no test can prove it is followed. What a test can do is
    stop it being dropped the next time the body runs out of room.
    """
    skill = _SKILL_MD.read_text(encoding="utf-8")
    never = skill[skill.index("## NEVER rules"):]
    assert "deferred file cannot be read" in never, (
        "SKILL.md's NEVER rules no longer say what to do when a file the "
        "contract defers to cannot be read. Several pointers order the agent "
        "to read a reference before acting; without this rule a missing or "
        "truncated reference produces an improvised runbook instead of a stop. "
        "If the wording changed, update this guard deliberately — do not "
        "delete the rule to reclaim a line of the body budget."
    )

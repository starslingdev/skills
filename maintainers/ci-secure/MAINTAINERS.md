# ci-secure — maintainer notes

**Maintainers only.** This file is a sibling of the installable
`skills/ci-secure/` tree; the `skills` CLI never copies `maintainers/` into an
end-user install, and `tests/test_ci_secure_install_surface.py` makes that
boundary a PASS/FAIL invariant.

How the ten-vector catalog is governed — the three admission tests, what a
pattern change has to touch, how a removal is recorded and what the rejection
record must still add up to — already ships with the skill, in
[`skills/ci-secure/references/why-these-ten.md`](../../skills/ci-secure/references/why-these-ten.md).
Read that first; it is the authority, and repeating it here would only give it
a second copy to drift from.

What does not live anywhere else is the rule below, because it governs this
repository rather than the skill: not what ci-secure looks for, but what may
be done with what it finds.

## Third-party findings and disclosure

**A ci-secure run against a repository we do not own can surface a live,
undisclosed vulnerability in someone else's code.** That output is sensitive
until its owner has had a chance to fix it, and we are the ones who generated
it.

The rule:

- **Never publish a finding against a third-party repo that its owner has not
  already disclosed or fixed.** Not in `examples/`, not in a report committed
  to this repo, not in a PR body, an issue, a screenshot, a blog post, or a
  social post. A committed file is worse than a post: it is permanent and
  indexed.
- **Report it privately first.** Use the project's own security policy or
  bug-bounty program. Give them the file, the line, and the attacker scenario
  the skill already produced.
- **Scope the scan when the target is a demonstration.** If the point is to
  show one already-public bug, fetch and scan that one workflow file rather
  than the whole repository, so an unrelated live finding cannot ride along
  into the artifact.
  [`examples/snowflakedb-snowflake-connector-net/README.md`](../../examples/snowflakedb-snowflake-connector-net/README.md)
  records the scoped fetch that shipped example uses, and what to check before
  scanning.
- **A scan of our own repositories has no such constraint**, and dogfooding
  ci-secure on this repo is encouraged.

This applies to anything derived from a run, including a report a loop or an
agent generated automatically. Read what you are about to commit.

## The guard that enforces it

A rule written down in a maintainers directory stops nobody, because the
person who would break it is exactly the person who has not read the file. So
the rule is also mechanical.

[`tests/test_findings_disclosure.py`](../../tests/test_findings_disclosure.py)
fails the suite when a committed ci-secure findings artifact reports a finding
against a repository that is not named in its `DISCLOSED_TARGETS` allowlist,
alongside the disclosure that clears it. It recognises a ci-secure artifact by
its top-level `catalog_patterns_evaluated` key or its `P<n>.<n>` pattern ids,
so the other engines' `findings.json` files are not swept in and the allowlist
keeps meaning "cleared for publication".

Adding a line to that allowlist **is** the decision, and it shows up in
review. The guard does not judge whether publishing is acceptable — it forces
somebody to say on the record that they thought about it.

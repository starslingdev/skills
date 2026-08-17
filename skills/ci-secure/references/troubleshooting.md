# Common issues

Operator-facing failure modes for a ci-secure run. The behavioral rules
these point back to live in [SKILL.md](../SKILL.md) (Phase 2 and the NEVER
rules) — this file is the lookup table, not the contract.

| Issue | Solution |
|-------|----------|
| "no .github/workflows directory" | Run from a repo root that contains GitHub Actions workflows |
| PyYAML missing | `pip install pyyaml` (the scanner's only third-party dep) |
| gh not installed / not logged in | The scan still runs; three checks go unmeasured and are reported as such — the impostor-SHA vector, and the two API-gated config facts (required-checks-skippable, fork-PR approval). `gh auth login` to enable them |
| Scanner emits zero findings | Most likely the workflows are actually clean — that's the headline, not a bug. Otherwise check for a detector regression via `tests/` |
| Scanner exits non-zero or writes unparseable output | Coverage failure, not a clean repo — surface exit code + stderr and stop (SKILL.md NEVER rules) |
| Subagent stops with a question | Surface it to the user; the finding's heading stays unmarked until the subagent completes |
| `run.py` exits 2 with an argv complaint | The `--repo` expansion was collapsed into one token by zsh — use the two-token `${REPO:+--repo} ${REPO:+"$REPO"}` form from Phase 2 |

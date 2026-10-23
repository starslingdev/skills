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

## Deriving `owner/repo` from the remote

The Phase 2 block passes `--repo owner/repo` when gh is authenticated, and
derives it in the SAME shell that runs the scan: shell variables do not
survive between an agent's separate commands, so a `REPO` set while
following Phase 1 would be empty by Phase 2 and every gh-gated check would
silently go unmeasured. Precedence: an explicitly exported `REPO` wins (to
point the lookups at another repo); otherwise it comes from the origin
remote, which arrives in two shapes:

```bash
# https://github.com/owner/repo[.git]  or  git@github.com:owner/repo.git
git remote get-url origin \
  | sed -nE 's#/$##; s#\.git$##; s#^(https://([^@/]+@)?github\.com/|(ssh://)?git@github\.com[:/])##p'
```

`sed -n` with the `p` flag prints only when the `github.com` prefix actually
matched, so a remote without one (another host, or a relative path such as
`owner/repo.git`) yields nothing. The result is passed only if it is exactly
`owner/repo` — one `/`, no scheme, no `.git` suffix — and anything else
leaves `REPO` empty rather than passing it on: the scan still
runs, and the checks it feeds are reported as skipped or unmeasured, never as
passes.

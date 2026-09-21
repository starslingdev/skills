# Structural / critical-path track — risk model & intent interrogation

Depth for the structural finding class (catalog category 14, OPT70–OPT75 plus
OPT78) — the findings routed from the measured critical path rather than a YAML
match. OPT78 is routed by a drill-time log leaf rather than the critical-path
router, so it emits no finding dict and the render boundary below never sees it;
its equivalent risk / guardrail / rollout discipline rides in the agent prompt
`blocking_path.py` builds for the `vitest-isolate-pool` leaf.
Routing mechanics and the full risk model live in `ARCHITECTURE.md` §11; this
doc covers what the render boundary enforces and the intent check baked into
every per-finding prompt. A normal run does not construct any of this by hand
(the renderer bakes it into each prompt) — read it for depth.

## Contents

- [Risk is mandatory and renders loud](#risk-is-mandatory-and-renders-loud-but-never-demotes-the-rank)
- [Interrogate the target file's history & intent](#interrogate-the-target-files-history--intent)

## Risk is mandatory and renders loud (but never demotes the rank)

Structural levers can degrade **correctness**, not just performance, so every
structural finding carries a **`risk`** (`LOW`/`MEDIUM`/`HIGH`), a mandatory
**`guardrail`**, and a **`rollout`** — and the render boundary **rejects** any
structural finding missing `risk` or `guardrail` (a future edit can't silently
turn a HIGH-risk lever into a safe-looking one). They rank in the same findings
table by Δ wall-clock — the biggest win is usually the slowest gating check, a
structural lever, so risk does **NOT** demote it; instead the risk is loud where
it renders (a `Risk` row, a 🔴 HIGH banner, and a prompt making the agent state
the failure mode + fallback + rollout). The canonical case is **scoping a
build/test to "only what changed"** (`turbo --filter` / `nx affected` /
`vitest --changed`, OPT70): the biggest wall-clock lever and the most dangerous
change — it can miss a transitive dependency, silently drop coverage, or turn a
build/import error into a false pass. **NEVER** present it as a safe quick win:
every such finding states its failure mode, a full-build/full-suite fallback, and
a parallel-run rollout.

## Interrogate the target file's history & intent

A detector firing says a pattern *matches*; it does **not** say the matched code
is there by mistake. So every per-finding prompt **instructs the user's agent** to
read the target file's **git history and recover its intent** before shaping a
change:

```bash
git log -p --follow -- .github/workflows/<file>.yml
gh pr list --search "<file/feature>" --state all
```

Many waste-looking patterns are deliberate — a check on *every* push may keep
triage labels/comments fresh; a "redundant" full build may back a correctness gate;
a removable-looking step may be load-bearing for a security split (unprivileged
exec + a privileged labeler). **Recency matters**: code shipped/iterated in the last
days/weeks by an active owner is almost always intentional. **Decision rule (in the
prompt):** if the fix contradicts the evident intent, the agent does NOT emit it as
a quick win — it flags a **behavioral/policy change needing the owner's sign-off**
(documenting the intent it changes) or returns an explicit "skip, here's why". The
prompt carries this rule so the agent applies it without re-deriving it.

# pallets/flask — two engines, one repo

Real, unedited-except-for-sanitization reports produced by the shipped skills
against [`pallets/flask`](https://github.com/pallets/flask). Nothing here is
hand-authored: each file is exactly what its pipeline emitted (only local
filesystem paths were stripped).

## ci-speedup — first run

Audited at commit `36e4a82` on 2026-07-24. Every provenance and evidence link
resolves to `pallets/flask` run/job/commit pages or the `starslingdev/skills`
pattern catalog.

**Result:** a typical flask PR waits **34s** for all checks to finish; the biggest
single measured win is **~7s** off the slowest fixable check, `Windows` (its
`uv run … tox run` step is the addressable lever). Full breakdown, per-check
drill-downs, and copy-paste agent prompts are in
[`ci-speedup-findings-report.md`](./ci-speedup-findings-report.md).

Produced by installing and running the skill:

```bash
npx skills add starslingdev/skills --skill ci-speedup
# then, in your coding agent: "audit pallets/flask for CI speedups"
```

## ci-score — configuration best practices

Scored at commit
[`d318b68`](https://github.com/pallets/flask/commit/d318b683471101618febed18996405ad26462110)
on 2026-08-17 (UTC), with `ci-score` at `starslingdev/skills` commit
[`a43d237`](https://github.com/starslingdev/skills/commit/a43d237), run from a
clean checkout of that commit. Rubric `ci-score-v0.1.3`. The scorer is offline —
it reads the local tree and fetches nothing.

**Result: 89/100 — 8 of 9 applicable checks pass**, across 5 workflow files. The
one failing check is job timeouts: no job sets `timeout-minutes`, so a hung job
bills GitHub's 6-hour default. Two checks are not applicable (no build tool with
a cache, no turbo/nx task graph). The report ranks the single recommendation by
impact × risk and ships a paste-able agent prompt for it:
[`ci-score-report.md`](./ci-score-report.md), raw facts in
[`ci-score-findings.json`](./ci-score-findings.json).

A high score is not a speed verdict — the report says so beside the card, and
this repo is the proof: the same repo's `ci-speedup` run above measures a 34s
gate. Adherence and speed move independently.

```bash
npx skills add starslingdev/skills --skill ci-score
# then, in your coding agent: "/ci-score"
```

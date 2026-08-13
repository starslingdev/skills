---
description: Check a published skill's skills.sh security audit, decide the next action, and drive a stale badge to green unattended
argument-hint: [owner/repo/skill]
---

Run the maintainer-only `skills-registry-security` skill against `$ARGUMENTS`
(default `starslingdev/skills/ci-secure` if no target is given).

Read `maintainers/skills-registry-security/SKILL.md` and follow it. In short:

1. Run the audit and read the `NEXT ACTION` verdict:

   ```bash
   python3 maintainers/skills-registry-security/scripts/registry_audit.py <owner/repo/skill> --repo-root .
   ```

   Add `--no-install` for the ~3s check instead of ~40s.

2. Act on the verdict:
   - `RESOLVED` — the badge is clean. Report and stop.
   - `ACTION_REQUIRED` — a cited literal is still in HEAD. It is a real finding;
     surface it with the code and stop. Do not fix silently.
   - `MONITOR` — the finding is stale, there is nothing to patch, and the
     registry's own ~daily re-audit is what clears it. Arm the unattended watch
     per SKILL.md's "Drive it to green unattended" section and report only on a
     decision change.
   - `DISAGREEMENT` / `UNVERIFIED` — surfaces disagree or could not be
     classified. Re-check before trusting; never report a badge as clean.

Rails from SKILL.md that always apply: never rename or re-slug a published
skill, never auto-file an issue on the registry's repo, and keep any research
subagents read-only.

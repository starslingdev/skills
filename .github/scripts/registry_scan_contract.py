"""One place that knows what the scanner says, so a rename breaks one file loudly.

The workflow reads the scanner's answer in three independent places — the gate
decides pass/fail, the reporter surfaces warnings, the red-proof control checks the
scanner can still see. Each used to hardcode its own understanding of the vendor's
vocabulary, and nothing tied them together.

That is why scanner 0.6.0 broke the check three ways and hid two of them. It replaced
issue codes (`E005` critical, `W011` warning) with named risks carrying 0-1000 scores,
and: the gate kept grepping for E-codes that can no longer appear, so a real finding
fell through to "NOT A FINDING"; the reporter kept parsing the 0.5.x JSON, returning
empty unconditionally so the summary read "No findings" whatever the content; and the
gating exemption was passed to `--ignore-failure-codes`, which accepts only the
scanner's X-class runtime codes, silently switching the whole warning policy off.

So the vocabulary lives here now, checked against the scanner's own models
(`agent_scan/models/api/v20260710.py`) rather than inferred from its printed output.
"""
from __future__ import annotations

# Every skill risk 0.6.0 can report, verbatim from SkillRiskIndexes. The gate refuses
# to run if the scanner grows one this list does not know: a new risk defaulting to
# "not blocking" is the silent-exemption failure all over again.
SKILL_RISKS: tuple[str, ...] = (
    "prompt_injection_skill_instructions",
    "suspicious_download_url",
    "malicious_code",
    "insecure_credential_handling",
    "secret_detection",
    "direct_money_access",
    "third_party_content_exposure",
    "unverifiable_dependencies",
    "modifying_system_services",
    "missing_skill_md",
)

# THE GATING RULE (owner ruling 2026-08-10, restated for 0.6.0's model 2026-08-26).
#
# The old ruling was "E-class blocks, W-class never does". 0.6.0 has neither class, so
# it is restated as the risks that do NOT block. It is deliberately shorter than the
# fifteen W-codes it replaces, because only ONE of those fifteen has ever actually
# fired on our skills:
#
#   W011 third-party content exposure — recorded in all four audits of one skill, and
#   inherently true here: a skill that audits other people's repositories ingests
#   outsider-authored text by design. That is `third_party_content_exposure`.
#
# The other fourteen were exempted pre-emptively and have no recorded history. Porting
# a guess into a new vocabulary would re-create the thing that just cost a week of
# blind scanning, so anything unevidenced blocks until it fires and earns an entry.
# In a security gate a false block is loud and one line to fix; a false exemption is
# silent.
#
# Not exempt, and never to be: `suspicious_download_url` — the old E005, the only code
# that has ever turned our skills red (ci-score once, ci-secure twice), AND the rule
# the red-proof control anchors on. Exempting it would disable the check that proves
# the scanner still detects anything.
NON_BLOCKING_RISKS: tuple[str, ...] = ("third_party_content_exposure",)

BLOCKING_RISKS: tuple[str, ...] = tuple(r for r in SKILL_RISKS if r not in NON_BLOCKING_RISKS)


def unknown_risks(seen: set[str]) -> set[str]:
    """Risk names the scanner reported that this contract does not know about."""
    return seen - set(SKILL_RISKS)


def iter_findings(payload: dict) -> list[dict]:
    """Every risk in a 0.6.0 `--json` payload, as flat rows.

    Shape (ScanPathResponse): `{"scan_path_responses": [{"path", "skill_risks":
    [{"name", "risk_indexes": {<risk name>: {"score", "evidence", ...} | null}}]}]}`.
    A risk that did not fire is present as null rather than absent, so `None` values
    are skipped rather than counted.
    """
    rows: list[dict] = []
    for response in payload.get("scan_path_responses") or []:
        if not isinstance(response, dict):
            continue
        for skill in response.get("skill_risks") or []:
            if not isinstance(skill, dict):
                continue
            indexes = skill.get("risk_indexes") or {}
            if not isinstance(indexes, dict):
                continue
            for name, score in indexes.items():
                if not isinstance(score, dict):
                    continue  # null = this risk did not fire
                rows.append(
                    {
                        "risk": name,
                        "skill": str(skill.get("name") or response.get("path") or "unknown"),
                        "score": score.get("score"),
                        "evidence": " ".join(str(score.get("evidence") or "").split()),
                        "blocking": name not in NON_BLOCKING_RISKS,
                    }
                )
    rows.sort(key=lambda r: (not r["blocking"], r["risk"], r["skill"]))
    return rows

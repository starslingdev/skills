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
(`agent_scan/models/api/v20260710.py`) rather than inferred from its printed output,
and pinned offline by `tests/test_registry_scan_workflow.py` so a hand-copied list
cannot drift unnoticed.
"""
from __future__ import annotations

# Every skill risk 0.6.0 can report, verbatim from `SkillRiskIndexes`.
#
# The list is not what blocks — the exemption list below is, by omission — but it is
# what lets this build NOTICE that the scanner's catalog has moved. A risk name outside
# it is surfaced by the reporter as unrecognised (see `unknown_risks`), because the
# alternative is finding out the vocabulary drifted the way we found out last time:
# from a public audit page.
# The pinned scanner. Stated here so the hint below can name it, and so a guard can
# prove the workflow's two invocations and the red-proof all pin the SAME version — a
# half-bumped pin would have two passes disagreeing about the contract they read.
PINNED_SCANNER = "0.6.0"

# Every coverage-gap message ends with this. When this check goes red for a reason that
# is not a finding, the FIRST thing worth suspecting is that the vendor moved and the
# pin did not: that is what happened on 2026-08-19, and it cost a week because the red
# said "critical finding" and nobody thought to look at the version. Naming the pin in
# the failure itself is what turns "why is this red" into "check whether 0.6.0 is still
# current" without anyone having to remember this history.
STALE_PIN_HINT = (
    f"If this is not obviously a problem with the skills themselves, suspect the "
    f"scanner pin first: this gate runs snyk-agent-scan=={PINNED_SCANNER}, and a newer "
    f"release can rename a flag, a risk, or the JSON shape out from under it. Compare "
    f"against the current release before assuming the tree is at fault."
)

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

# The MCP-server risks, verbatim from `McpServerRiskIndexes`. The scanner's `--ci` exit
# weighs these alongside the skill risks, so a run can fail the gate on one; the reporter
# has to be able to name it rather than print "No findings." over it.
SERVER_RISKS: tuple[str, ...] = (
    "dangerous_words",
    "prompt_injection_tool_desc",
    "untrusted_content",
    "private_data",
    "destructive_capabilities",
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
# Never to be exempted, and pinned offline by
# `tests/test_registry_scan_workflow.py::test_no_blocking_risk_is_ever_exempt`:
#
#   `suspicious_download_url` — the old E005, the only rule that has ever turned our
#   skills red (ci-score once, ci-secure twice).
#   `unverifiable_dependencies` — what the red-proof control's fixture actually fires
#   on 0.6.0. The scanner labels it `Unverifiable URLs:` in the output the control
#   greps, and exempting it would disable the check that proves the scanner still
#   detects anything.
NON_BLOCKING_RISKS: tuple[str, ...] = ("third_party_content_exposure",)

BLOCKING_RISKS: tuple[str, ...] = tuple(
    r for r in SKILL_RISKS + SERVER_RISKS if r not in NON_BLOCKING_RISKS
)


class UnrecognisedPayload(ValueError):
    """The scanner's JSON parsed, but is not a shape this contract understands."""


def unknown_risks(seen) -> set[str]:
    """Risk names the scanner reported that this contract does not know about.

    An unknown risk already BLOCKS — it is not in the exemption list, so `--ci` fails
    on it. What it does not do on its own is tell anyone the catalog moved, which is
    why the reporter calls this and says so out loud.
    """
    return set(seen) - set(SKILL_RISKS) - set(SERVER_RISKS)


def iter_findings(payload: dict) -> list[dict]:
    """Every risk in a 0.6.0 `--json` payload, as flat rows.

    Shape (`ScanPathResponse`): `{"scan_path_responses": [{"path", "error",
    "server_risks": [...], "skill_risks": [{"name", "error", "risk_indexes":
    {<risk name>: {"score", "evidence", ...}}}]}]}`.

    A risk that did not fire is ABSENT, not null: the scanner serialises with
    `model_dump(mode="json", exclude_none=True)`. Nulls are tolerated anyway, because
    tolerating a shape costs nothing and assuming one is what broke the reporter.

    Both `server_risks` and `skill_risks` are read, because the scanner's `--ci` exit
    weighs both — a server risk the reporter skipped would fail the gate under a
    summary saying "No findings."

    Raises `UnrecognisedPayload` if the top-level key is missing. Returning `[]` there
    is indistinguishable from a clean scan, and that silence is exactly how the 0.5.x
    reader survived a week of reporting nothing.
    """
    if "scan_path_responses" not in payload:
        raise UnrecognisedPayload(
            "no `scan_path_responses` key: this is not scanner 0.6.0's `--json` shape, "
            "so no finding in it can be read. Do not treat this run as clean."
        )
    rows: list[dict] = []
    for response in payload.get("scan_path_responses") or []:
        if not isinstance(response, dict):
            continue
        path = str(response.get("path") or "unknown")
        _append_error(rows, response.get("error"), path)
        for key in ("skill_risks", "server_risks"):
            for entity in response.get(key) or []:
                if not isinstance(entity, dict):
                    continue
                name = str(entity.get("name") or path)
                _append_error(rows, entity.get("error"), name)
                indexes = entity.get("risk_indexes") or {}
                if not isinstance(indexes, dict):
                    continue
                for risk, score in indexes.items():
                    if not isinstance(score, dict):
                        continue  # absent/null = this risk did not fire
                    rows.append(
                        {
                            "risk": risk,
                            "skill": name,
                            "score": score.get("score"),
                            "evidence": " ".join(str(score.get("evidence") or "").split()),
                            "blocking": risk not in NON_BLOCKING_RISKS,
                        }
                    )
    rows.sort(key=lambda r: (not r["blocking"], r["risk"], r["skill"]))
    return rows


def _append_error(rows: list[dict], error, where: str) -> None:
    """A scan error is not a finding, but it is emphatically not a clean result either.

    Without this the summary printed "No findings." over a run where the scanner failed
    on part of the tree — the gate caught it in a separate pass, so the two surfaces
    said opposite things and the human-glanceable one was the one that was wrong.
    """
    if not isinstance(error, dict):
        return
    detail = " ".join(str(error.get("message") or error.get("code") or error).split())
    rows.append(
        {
            "risk": f"scan_error:{error.get('code') or 'unknown'}",
            "skill": where,
            "score": None,
            "evidence": f"The scanner could not analyse this entry: {detail}",
            "blocking": True,
        }
    )

"""Issue #12 (skills.sh Snyk W007 HIGH / W011) - deterministic credential masking on
every piece of verbatim third-party text the renderer quotes.

Job logs and workflow YAML are untrusted third-party data. GitHub masks the secrets it
KNOWS about, but an accidentally-echoed unregistered token reaches the captured log
verbatim - and the report is the artifact users commit and share. So the render boundary
masks credential-SHAPED strings itself, at the same chokepoints that already defuse
backtick runs (`_fence_safe`, and the LLM gap-fill's prose fields).

The no-false-positive half is as load-bearing as the masking half: an audit report whose
step names, durations, run URLs or provenance shas came back `[REDACTED:...]` would be
useless. There is deliberately NO entropy heuristic - only the shaped patterns below.

Run: pytest -v skills/ci-speedup/tests/test_secret_redaction.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)

# Every PROVIDER-SHAPED fixture below is assembled at import time from halves that are
# each inert on their own - none is written as a literal, and each split breaks the
# provider prefix the detectors key on. Two scanners have already proven why: GitHub's
# push protection rejects a push whose files CONTAIN a Slack-token-shaped string (fake or
# not), and GitHub secret scanning raised an "Action needed: possible valid secret" alert
# on the Google-key literal this file used to carry. A fixture that LOOKS like a
# credential to us looks like one to every scanner that reads the shipped tree.
#
# `_fixture()` rather than a bare `"a" + "b"`: adjacent string literals are folded into
# the finished value at compile time, so a static analyzer that evaluates constant
# expressions reconstructs exactly the string we are hiding. A call is not folded.
#
# The remaining fixtures - the generic hex/opaque values and the `Bearer` payload - carry
# no provider prefix and match no scanner's shape, so they stay verbatim. Do not
# "simplify" the assembled ones back into literals.
def _fixture(*parts: str) -> str:
    return "".join(parts)


_FAKE_SLACK = _fixture("xox", "b-2411000000-2411000000-AbCdEfGhIjKlMnOpQrStUvWx")
_FAKE_GHP = _fixture("ghp_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
_FAKE_GHO = _fixture("gho_", "9zYx8Wv7Ut6Sr5Qp4On3Ml2Kj1Ih0Gf9Ed8")
_FAKE_GH_PAT = _fixture("github_", "pat_11ABCDEFG0aBcDeFgHiJkL_mNoPqRsTuVwXyZ012345")
_FAKE_AWS = _fixture("AKIA", "IOSFODNN7EXAMPLE")  # AWS's documented example key
_FAKE_AWS_SESSION = _fixture("ASIA", "Y34FZKBOKMUTVV7A")
_FAKE_JWT = _fixture("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", ".",
                     "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".",
                     "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
_FAKE_GOOGLE = _fixture("AIza", "SyD-1234567890abcdefghijklmnopqrstu")
_FAKE_NPM = _fixture("npm_", "9f3a1c7e5b2d4086abcd1234")
_FAKE_NPM_PUBLISH = _fixture("npm_", "1a2b3c4d5e6f7g8h9i0jKLMNOPQRSTUVWXYZ")
_FAKE_DOCKER = _fixture("dckr_", "pat_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123")
_FAKE_OPENAI = _fixture("sk-", "proj-A1b2C3d4E5f6G7h8I9j0K1l2M3n4")
_FAKE_PEM_HEADER = _fixture("-----BEGIN RSA ", "PRIVATE KEY-----")

# (label, line as it appears in a log, the secret substring that must not survive, kind)
_SECRET_LINES = [
    ("github classic token",
     f"fatal: could not read Password for 'https://{_FAKE_GHP}@github.com'",
     _FAKE_GHP, "github-token"),
    ("github oauth token",
     f"env GH_TOKEN={_FAKE_GHO} exported by setup step",
     _FAKE_GHO, "github-token"),
    ("github fine-grained PAT",
     f"curl -H 'Authorization: token {_FAKE_GH_PAT}'",
     _FAKE_GH_PAT, "github-token"),
    ("aws long-term access key",
     f"aws_access_key_id {_FAKE_AWS} used for the cache bucket",
     _FAKE_AWS, "aws-access-key"),
    ("aws session access key",
     f"assumed role -> {_FAKE_AWS_SESSION} (expires in 3600s)",
     _FAKE_AWS_SESSION, "aws-access-key"),
    ("slack bot token",
     f"notify.sh: posting with {_FAKE_SLACK}", _FAKE_SLACK, "slack-token"),
    ("jwt",
     f"auth ok {_FAKE_JWT} done",
     _FAKE_JWT, "jwt"),
    ("google api key",
     f"GOOGLE_MAPS={_FAKE_GOOGLE} in the test env",
     _FAKE_GOOGLE, "google-api-key"),
    ("private key header",
     f"{_FAKE_PEM_HEADER} written to /tmp/deploy.pem",
     _FAKE_PEM_HEADER, "private-key"),
    ("generic assignment",
     f"  NPM_TOKEN={_FAKE_NPM} (from the org secret)",
     _FAKE_NPM, "credential"),
    ("generic colon form",
     "config: api-key: 8f2b91aa5c7d3e40 loaded",
     "8f2b91aa5c7d3e40", "credential"),
    # `Authorization: Bearer <opaque>` is the single most common real credential line in a
    # job log, and the scheme word sits between the separator and the value.
    ("authorization bearer header",
     "> Authorization: Bearer 7c1d0e9f8a6b5c4d3e2f1a09 (retrying)",
     "7c1d0e9f8a6b5c4d3e2f1a09", "credential"),
    ("npm publish token",
     f"npm notice using {_FAKE_NPM_PUBLISH} for registry auth",
     _FAKE_NPM_PUBLISH, "npm-token"),
    ("docker hub pat",
     f"docker login -u ci -p {_FAKE_DOCKER}",
     _FAKE_DOCKER, "docker-token"),
    ("llm api key",
     f"OPENAI probe failed for {_FAKE_OPENAI}",
     _FAKE_OPENAI, "llm-api-key"),
]

# Lines the mask must leave BYTE-IDENTICAL. Ordinary CI text that superficially rhymes
# with a credential: durations, matrix step names, deep URLs, a 40-hex provenance sha,
# short `key: value` prose, and a masked line fed back in (idempotence).
_CLEAN_LINES = [
    "Run tests (chunk 3 of 8) completed in 1245.37s",
    " Duration  96.12s (transform 8.97s, setup 1.01s, import 245.03s, tests 214.54s)",
    "token: yes",
    "api_key: enabled",
    "authorization: required",
    "password: changeme",
    "uses: actions/checkout@v4 with fetch-depth: 0",
    "https://github.com/o/r/actions/runs/12345678901/job/34567890123?check_suite_focus=true",
    "provenance: skills @ 4f9c2b1a8d7e6f5c4b3a2918e7d6c5b4a3928170",
    "downloading node-v20.11.1-linux-x64.tar.gz (23847361 bytes)",
    "cache restored from key Linux-pnpm-store-9f8e7d6c5b4a39281706f5e4d3c2b1a0",
    "[REDACTED:github-token] was already masked upstream",
    # A CORRECTLY-written workflow line: the value is a reference, not a credential. These
    # are exactly the lines the catalog detectors quote as evidence, so masking them would
    # destroy the diagnostic AND falsely imply the repo hardcodes a token.
    "  GITHUB_TOKEN: ${{secrets.GITHUB_TOKEN}}",
    "  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
    "  NPM_TOKEN: ${{secrets.NPM_PUBLISH_TOKEN}}",
    "export NODE_AUTH_TOKEN=${NPM_TOKEN_FOR_PUBLISH}",
    "set NUGET_API_KEY=%NUGET_KEY_FROM_CI%",
]
# The idempotence fixture above is the one clean line that ALREADY carries a mask marker, so
# the "a clean report renders no `[REDACTED`" end-to-end guard has to exclude it. Filtered by
# value, not by slice index - a positional `[:-1]` silently stops excluding it the moment a
# line is appended.
_CLEAN_LINES_NO_MARKER = [l for l in _CLEAN_LINES if "[REDACTED" not in l]


# --------------------------------------------------------------------------- #
# The mask itself
# --------------------------------------------------------------------------- #

def test_every_credential_shape_is_masked_with_its_kind():
    for label, line, secret, kind in _SECRET_LINES:
        out = bp._redact_secrets(line)
        assert secret not in out, f"{label}: the secret survived the mask -> {out}"
        assert f"[REDACTED:{kind}]" in out, f"{label}: wrong/absent kind -> {out}"


def test_masking_keeps_the_surrounding_evidence_readable():
    # Evidence stays interpretable: only the secret is replaced, never the whole line,
    # and a generic `key=value` keeps its KEY (which is half the diagnostic value).
    out = bp._redact_secrets(f"  NPM_TOKEN={_FAKE_NPM} (from the org secret)")
    assert out == "  NPM_TOKEN=[REDACTED:credential] (from the org secret)"
    out2 = bp._redact_secrets(
        f"aws_access_key_id {_FAKE_AWS} used for the cache bucket")
    assert out2 == "aws_access_key_id [REDACTED:aws-access-key] used for the cache bucket"


def test_ordinary_ci_text_is_never_masked():
    for line in _CLEAN_LINES:
        assert bp._redact_secrets(line) == line, f"false positive on: {line}"


def test_mask_is_idempotent():
    for _label, line, _secret, _kind in _SECRET_LINES:
        once = bp._redact_secrets(line)
        assert bp._redact_secrets(once) == once, line


# --------------------------------------------------------------------------- #
# The chokepoints
# --------------------------------------------------------------------------- #

def test_fence_safe_is_the_chokepoint_for_verbatim_evidence():
    # `_fence_safe` is the one function every verbatim log/YAML line, every repo-controlled
    # name and every agent-prompt line already flows through (via `_clean_label`,
    # `_safe_span`, `_fence_body`, and the direct evidence sites). Masking there covers the
    # whole class by construction rather than site by site.
    for _label, line, secret, kind in _SECRET_LINES:
        for rendered in (bp._fence_safe(line), bp._fence_body([line]),
                         bp._safe_span(line), bp._clean_label(line)):
            assert secret not in rendered
            assert f"[REDACTED:{kind}]" in rendered
    # Still byte-identical for clean single-line text (the existing `_fence_safe` contract).
    for line in _CLEAN_LINES:
        assert bp._fence_safe(line) == line


def test_flatten_cell_masks_workflow_yaml_evidence():
    # `_flatten_cell` is the OTHER verbatim sink: the appendix `**Evidence:**` lines and the
    # Tier-2 / structural rows quote workflow YAML through it without touching `_fence_safe`.
    # A hardcoded token in a workflow file is the likeliest thing to be quoted from, so the
    # mask has to hold here too - otherwise coverage is site-by-site, not by construction.
    for _label, line, secret, kind in _SECRET_LINES:
        cell = bp._flatten_cell(line)
        assert secret not in cell, line
        assert f"[REDACTED:{kind}]" in cell, line
    # Clean cells keep the pre-existing contract: whitespace collapse + pipe escape only,
    # with no mask introduced.
    for line in _CLEAN_LINES:
        expected = re.sub(r"\s+", " ", line).replace("|", "\\|").strip()
        assert bp._flatten_cell(line) == expected, f"false positive on cell: {line}"


# --------------------------------------------------------------------------- #
# End to end: the artifact users share
# --------------------------------------------------------------------------- #

def _gap_fill_doc() -> dict:
    # One pole whose log matches NO catalog detector -> the phase-4a LLM gap-fill renders,
    # quoting verbatim log lines as evidence. The exact shape Snyk's W007 flagged.
    return {
        "repo": "o/r", "scanned_at": "2026-06-08T00:00:00Z",
        "data_sources": {"runs_sampled": 100, "jobs_sampled": 300,
                         "workflows_analyzed": 5},
        "pr_critical_path": {
            "sampled_pr_count": 20, "sample_target": 20, "sample_complete": True,
            "poles": [{
                "check": "tests-web", "p50_s": 255.0,
                "workflow_file": ".github/workflows/pipeline.yml", "job": "tests-web",
                "dominant_step": "run tests", "dominant_p50_s": 91.0,
                "steps": [{"step": "run tests", "category": "test", "p50_s": 91.0},
                          {"step": "Build", "category": "build", "p50_s": 60.0}],
            }]},
    }


def _gap_fill_md() -> str:
    analysis = {
        "cause": "The auth bootstrap re-mints a token every run: " + _SECRET_LINES[0][2],
        "breakdown": [["auth bootstrap", "~40s, see " + _SECRET_LINES[3][2]]],
        "evidence": [line for _l, line, _s, _k in _SECRET_LINES],
        "prompt": "REPO: o/r\nThe runner logs " + _SECRET_LINES[6][2] + " on every job.",
    }
    return bp.render(_gap_fill_doc(), {"pipeline": "a log matching no leaf detector"}, {},
                     {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                     analyses={"pipeline": analysis})


def test_no_credential_reaches_the_rendered_report_or_the_agent_prompt():
    md = _gap_fill_md()
    assert "🤖 LLM root-cause analysis" in md          # fixture sanity: the gap-fill rendered
    assert "Prompt for your coding agent" in md
    for label, _line, secret, kind in _SECRET_LINES:
        assert secret not in md, f"{label}: a credential reached the shipped report"
        assert f"[REDACTED:{kind}]" in md, f"{label}: no mask marker rendered"


def test_the_report_still_reads_as_evidence_after_masking():
    # Masking must not gut the evidence: the non-secret context of each quoted line survives.
    md = _gap_fill_md()
    for frag in ("could not read Password for", "aws_access_key_id",
                 "notify.sh: posting with", "NPM_TOKEN=", "The auth bootstrap re-mints",
                 "auth bootstrap"):
        assert frag in md, frag


def test_ordinary_report_text_is_untouched_by_the_mask():
    # Regression guard for the whole renderer: a normal audit (no credentials anywhere)
    # renders byte-identically with the mask in place - no `[REDACTED` marker anywhere.
    md = bp.render(_gap_fill_doc(), {"pipeline": "a log matching no leaf detector"}, {},
                   {"pipeline": "https://github.com/o/r/actions/runs/9"}, "2026-06-08",
                   analyses={"pipeline": {
                       "cause": "The test job installs its toolchain from scratch.",
                       "breakdown": [["toolchain install", "~40s of the 91s step"]],
                       "evidence": _CLEAN_LINES_NO_MARKER,
                       "prompt": "REPO: o/r\nInvestigate the toolchain install."}})
    assert "[REDACTED" not in md
    for line in _CLEAN_LINES_NO_MARKER:
        assert line.strip() in md, line

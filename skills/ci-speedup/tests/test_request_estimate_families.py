"""The pre-collection request estimate prices every family the collector issues,
and stays within a stated factor of the count measured on the recorded corpus."""
from __future__ import annotations

import ast
import json
import math
import re
import sys
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import request_estimate as est  # noqa: E402

_CORPUS = Path(__file__).resolve().parent / "fixtures" / "gh_replay"
# The recorded corpus's measured count must sit inside [low, high], and high may
# overstate it by at most this factor.
_STATED_FACTOR = 4


def _template(node: ast.AST) -> str | None:
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}"
                       for v in node.values)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route(text: str) -> str:
    """`repos/{repo}/actions/runs/{id}/jobs?per_page=100` -> `repos/*/actions/runs/*/jobs`."""
    path = text.split("?", 1)[0]
    segs: list[str] = []
    for seg in path.split("/"):
        if seg == "contents":
            segs.append("contents/*")
            break
        segs.append("*" if "{}" in seg else seg)
    return "/".join(segs)


def _endpoint_routes(path: Path) -> set[str]:
    routes = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # A literal piece of an f-string is walked on its own too; only the whole
    # template is an endpoint.
    pieces = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
              for v in n.values}
    for node in ast.walk(tree):
        if id(node) in pieces:
            continue
        text = _template(node)
        if text and re.match(r"^(repos|search)/", text) and " " not in text:
            routes.add(_route(text))
    return routes


def test_every_collector_request_family_is_priced():
    routes = _endpoint_routes(_SCRIPTS / "collect_runs.py")
    priced = {f.route for f in est.FAMILIES}
    # The GraphQL lifecycle block is the client's only GraphQL caller.
    assert "def graphql(" in (_SCRIPTS / "collect_runs.py").read_text(encoding="utf-8")
    assert "graphql" in priced
    missing = sorted(routes - priced)
    assert not missing, f"collect_runs.py requests unpriced families: {missing}"
    stale = sorted(priced - routes - {"graphql"})
    assert not stale, f"priced families the collector no longer requests: {stale}"


def test_route_template_normalisation():
    assert _route("repos/{}/actions/runs/{}/jobs?per_page=100") == "repos/*/actions/runs/*/jobs"
    assert _route("repos/{}/contents/{}?ref={}") == "repos/*/contents/*"
    assert _route("repos/{}/contents/package.json") == "repos/*/contents/*"
    assert _route("repos/{}/compare/{}...{}") == "repos/*/compare/*"


def test_estimate_uses_the_collectors_own_run_sample_default():
    src = (_SCRIPTS / "collect_runs.py").read_text(encoding="utf-8")
    m = re.search(r'add_argument\("--max-runs", type=int, default=(\d+)\)', src)
    assert m and int(m.group(1)) == est.COLLECT_MAX_RUNS
    # run.py never forwards a --max-runs of its own to the collector.
    assert not re.search(r"'--max-runs'|\"--max-runs\",", (_SCRIPTS / "run.py").read_text(encoding="utf-8"))


def test_lifecycle_graphql_pages_match_the_collector():
    src = (_SCRIPTS / "pr_lifecycle.py").read_text(encoding="utf-8")
    m = re.search(r"for page in range\(1,\s*(\d+)\)", src)
    assert m and int(m.group(1)) - 1 == est.LIFECYCLE_GRAPHQL_PAGES


def test_job_logs_are_priced_without_with_logs():
    """Several detectors read bounded job logs on every pass; the corpus's plain
    run downloads 27 of them."""
    facts = est.Facts(workflows=3, job_pages=1, repo_runs=None, self_hosted=False,
                      scanner_schedules=0, with_logs=False)
    plain = est.estimate(facts)
    assert plain.log_high > 0
    drilled = est.estimate(est.Facts(**{**facts.__dict__, "with_logs": True}))
    assert drilled.log_high > plain.log_high


def _corpus_probe_facts(root: Path) -> est.Facts:
    workflows = json.loads(
        (_CORPUS / "repos_synthetic_repo_actions_workflows_per_page_100.json").read_text())
    job_files = sorted(_CORPUS.glob("repos_synthetic_repo_actions_runs_*_jobs_per_page_100.json"))
    run_ids = {re.search(r"runs_(\d+)_jobs", p.name).group(1) for p in job_files}
    newest = max(run_ids, key=int)
    jobs = json.loads(
        (_CORPUS / f"repos_synthetic_repo_actions_runs_{newest}_jobs_per_page_100.json"
         ).read_text())
    runs = {"total_count": len(run_ids), "workflow_runs": [{"id": int(newest)}]}
    return est.facts_from_probe(workflows, runs, jobs, root=root, with_logs=False)


def test_recorded_corpus_count_is_inside_the_range_within_the_stated_factor(tmp_path):
    e2e = pytest.importorskip("test_offline_pipeline_e2e")
    root = tmp_path / "repo"
    e2e._init_repo(root)
    facts = _corpus_probe_facts(root)
    assert facts.workflows == 9 and facts.repo_runs == 51
    t = est.estimate(facts)
    low = t.json_low + t.log_low + t.graphql_low + t.search_low
    high = t.json_high + t.log_high + t.graphql_high + t.search_high
    # run.py skips the live probe under replay, so the golden count has no probes.
    actual = e2e._GOLDEN_GH_QUERY_COUNT
    assert low <= actual <= high, (low, actual, high)
    assert high <= _STATED_FACTOR * actual, (high, actual)
    assert t.json_ceiling + t.log_ceiling >= high


def test_workflow_count_is_the_listed_workflows_in_the_checkout(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("on: push\n")
    listed = {"total_count": 3, "workflows": [
        {"path": ".github/workflows/ci.yml", "state": "active"},
        {"path": ".github/workflows/old.yml", "state": "disabled_manually"},
        {"path": "dynamic/pages/pages-build-deployment", "state": "active"}]}
    facts = est.facts_from_probe(listed, {"total_count": 5}, {"total_count": 150},
                                 root=tmp_path, with_logs=False)
    assert facts.workflows == 1
    assert facts.job_pages == math.ceil(150 / 100)
    assert facts.repo_runs == 5


def test_distinct_run_reads_never_exceed_the_repos_run_count():
    small = est.estimate(est.Facts(workflows=4, job_pages=1, repo_runs=6,
                                   self_hosted=False, scanner_schedules=0,
                                   with_logs=False))
    big = est.estimate(est.Facts(workflows=4, job_pages=1, repo_runs=None,
                                 self_hosted=False, scanner_schedules=0,
                                 with_logs=False))
    assert small.json_high < big.json_high

#!/usr/bin/env python3
"""Local-only, hypothetical required-summary census; never executes workflows.

Capture immutable public workflow blobs, then measure them with a chosen engine
checkout. These are hypothetical required contexts, not branch-protection facts.
Snapshot and report destinations must be outside this source checkout.
"""
from __future__ import annotations

import argparse
import base64
import collections
import importlib.util
import json
import inspect
from pathlib import Path
import re
import subprocess
import time

import yaml

ROOT = Path(__file__).resolve().parents[3]
SUMMARY_REFERENCE = re.compile(r'needs\.[\w*-]+\.result|tojson\(needs\)|alls-green', re.I)


def _engine(root):
    path = Path(root) / 'skills/ci-secure/scripts/config_facts.py'
    spec = importlib.util.spec_from_file_location('census_config_facts', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gh(endpoint):
    result = subprocess.run(['gh', 'api', endpoint], capture_output=True, text=True,
                            timeout=90, check=True)
    return json.loads(result.stdout)


def capture(repository, fetch=gh):
    """Read public workflow blobs at one immutable revision, never repo code."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('expected owner/repository')
    metadata = fetch('repos/' + repository)
    if metadata.get('private') is not False:
        raise ValueError('census accepts explicitly public repositories only')
    revision = fetch('repos/' + repository + '/commits/HEAD')['sha']
    files = fetch('repos/' + repository + '/contents/.github/workflows?ref=' + revision)
    workflows = []
    for item in files:
        if item.get('type') == 'file' and item['name'].endswith(('.yml', '.yaml')):
            blob = fetch('repos/' + repository + '/git/blobs/' + item['sha'])
            workflows.append({'path': item['path'], 'sha': item['sha'],
                              'text': base64.b64decode(blob['content']).decode('utf-8')})
    return {'repository': repository, 'revision': revision, 'workflows': workflows,
            'action_resolutions': resolve_tags(workflows, fetch)}


def resolve_tags(workflows, fetch=gh):
    """Record public tag resolutions for deterministic later census replay."""
    from urllib.parse import quote
    prefix = _engine(ROOT)._verdict_assertions().ALLS_GREEN.split('@')[0] + '@'
    refs = set()
    for item in workflows:
        try:
            doc = yaml.safe_load(item['text'])
            if not isinstance(doc, dict):
                continue
            for job in (doc.get('jobs') or {}).values():
                for step in job.get('steps', []):
                    ref = step.get('uses', '') if isinstance(step, dict) else ''
                    if isinstance(ref, str) and ref.startswith(prefix):
                        tag = ref[len(prefix):]
                        if (not re.fullmatch(r'[a-fA-F0-9]{40}', tag)
                                and re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_./-]{0,127}', tag)):
                            refs.add(ref)
        except (yaml.YAMLError, AttributeError, TypeError):
            continue
    resolved = {}
    for ref in sorted(refs)[:16]:
        try:
            response = fetch('repos/' + prefix[:-1] + '/commits/' + quote(ref[len(prefix):], safe=''))
            resolved[ref] = response.get('sha')
        except Exception:
            resolved[ref] = None
    return resolved


def measure(snapshot, engine_root):
    """Require every discovered summary name together and each one separately.

    Discovery is explicit and reproducible: a dependent job whose steps or env
    read dependency results/JSON or use the supported verdict action. Keep the
    selection in the output so maintainers can review ambiguous job roles.
    A caller may supply `summary_jobs` as path/job pairs to freeze that review.
    """
    cf = _engine(engine_root)
    resolution_args = {}
    if 'action_resolver' in inspect.signature(cf._required_checks_skippable).parameters:
        resolution_args['action_resolver'] = lambda ref: snapshot.get('action_resolutions', {}).get(ref)
    docs, errors = [], []
    for item in snapshot['workflows']:
        try:
            doc = yaml.safe_load(item['text'])
            if not isinstance(doc, dict):
                raise ValueError('workflow is not a mapping')
            docs.append((item['path'], doc))
        except (yaml.YAMLError, ValueError, TypeError) as exc:
            errors.append({'path': item['path'], 'reason': type(exc).__name__})
    selected = snapshot.get('summary_jobs')
    # The fact matches producers by display name across every workflow, so a
    # shared name combines jobs: one supported job can hide a sibling's
    # change. Each row also records its own job's assertion for comparison.
    names = collections.Counter(cf._display_name(key, job) for _, doc in docs
                                for key, job in cf._jobs(doc))
    summaries = []
    for rel, doc in docs:
        for key, job in cf._jobs(doc):
            body = str(job.get('steps', [])) + str(job.get('env', {}))
            included = ([rel, key] in selected if selected is not None else
                        cf._needs_of(job) and SUMMARY_REFERENCE.search(body))
            if not included:
                continue
            context = cf._display_name(key, job)
            try:
                outcome, evidence = cf._required_checks_skippable(
                    docs, snapshot['repository'],
                    lambda repo: ([context], 'hypothetical required summary checks'), **resolution_args)
                assertion, detail = cf._verdict_assertions().analyze(dict(cf._jobs(doc)), key, doc, **resolution_args)
                reason = None
                if outcome == 'unmeasured':
                    # A proven assertion can still have an unknown producer or
                    # trigger. Do not misrank its proof as a refusal reason.
                    reason = detail if assertion == 'unmeasured' else 'producer or trigger unresolved'
            except Exception as exc:  # a census must retain coverage failures
                outcome, evidence = 'unmeasured', 'engine error: ' + type(exc).__name__
                assertion, reason = 'unmeasured', evidence
            summaries.append({'path': rel, 'job': key, 'context': context,
                              'outcome': outcome, 'evidence': evidence, 'reason': reason,
                              'assertion': assertion, 'ambiguous': names[context] > 1})
    contexts = sorted({row['context'] for row in summaries})
    started = time.monotonic()
    if errors:
        outcome, evidence = 'unmeasured', 'workflow coverage incomplete'
    elif not contexts:
        outcome, evidence = 'not_applicable', 'no summary jobs selected'
    else:
        try:
            outcome, evidence = cf._required_checks_skippable(
                docs, snapshot['repository'],
                lambda repo: (contexts, 'hypothetical required summary checks'), **resolution_args)
        except Exception as exc:
            outcome, evidence = 'unmeasured', 'engine error: ' + type(exc).__name__
    return {'repository': snapshot['repository'], 'revision': snapshot['revision'],
            'hypothetical': True, 'required_contexts': contexts, 'outcome': outcome,
            'evidence': evidence, 'summaries': summaries, 'coverage_errors': errors,
            'seconds': time.monotonic() - started}


def summarize(rows):
    eligible = [row for row in rows if row['outcome'] != 'not_applicable']
    reasons = collections.Counter(summary['reason'] for row in eligible
                                  for summary in row['summaries']
                                  if summary['outcome'] == 'unmeasured')
    counts = collections.Counter(row['outcome'] for row in eligible)
    return {'repository_count': len(eligible), 'outcomes': dict(counts),
            'unmeasured_share': counts['unmeasured'] / len(eligible) if eligible else None,
            'unmeasured_reasons': [{'reason': reason, 'count': count}
                                   for reason, count in reasons.most_common()],
            'repositories': rows}


def compare(before, after):
    old = {row['repository']: row for row in before}
    new = {row['repository']: row for row in after}
    if old.keys() != new.keys():
        raise ValueError('comparison requires identical repository membership')
    transitions, review = collections.Counter(), []
    summary_transitions, summary_review = collections.Counter(), []
    for repository in sorted(old):
        a, b = old[repository], new[repository]
        if a.get('revision') != b.get('revision'):
            raise ValueError('comparison requires identical revisions')
        def keyed(row):
            return {(item.get('path'), item.get('job'), item.get('context'), index): item
                    for index, item in enumerate(row.get('summaries', []))}
        old_summaries, new_summaries = keyed(a), keyed(b)
        if old_summaries.keys() != new_summaries.keys():
            raise ValueError('comparison requires identical summary selection')
        for key in old_summaries:
            previous, current = old_summaries[key], new_summaries[key]
            change = previous['outcome'] + '->' + current['outcome']
            summary_transitions[change] += 1
            # A shared name's outcome can stay pass while this job's own
            # assertion newly passes; audit that job too.
            masked = (current.get('ambiguous') and current.get('assertion') == 'pass'
                      and previous.get('assertion') != 'pass')
            if masked or current['outcome'] == 'pass' and previous['outcome'] != 'pass':
                summary_review.append({'repository': repository, 'transition': change,
                                       'before': previous, 'after': current})
        transition = a['outcome'] + '->' + b['outcome']
        transitions[transition] += 1
        if b['outcome'] == 'pass' and a['outcome'] != 'pass':
            review.append({'repository': repository, 'transition': transition,
                           'before': a, 'after': b})
    return {'transitions': dict(transitions), 'requires_manual_review': review,
            'summary_transitions': dict(summary_transitions),
            'summary_requires_manual_review': summary_review}


def _outside_checkout(path):
    path = Path(path).resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError('census data must stay outside the source checkout')
    return path


def _data_target(directory, target):
    """Keep every final destination inside the chosen local data directory."""
    if target.is_symlink():
        raise ValueError('census output target must not be a symlink')
    resolved = _outside_checkout(target)
    if directory not in resolved.parents:
        raise ValueError('census output escaped the data directory')
    return resolved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repositories', type=Path, help='JSON array of public owner/repository names')
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--engine-root', type=Path, default=ROOT)
    parser.add_argument('--report', default='report.json', help='report basename inside data-dir')
    parser.add_argument('--compare', type=Path, help='previous report on the same snapshots')
    args = parser.parse_args()
    directory = _outside_checkout(args.data_dir)
    if Path(args.report).name != args.report:
        parser.error('--report must be a basename')
    directory.mkdir(parents=True, exist_ok=True)
    snapshots = _data_target(directory, directory / 'snapshots')
    report_path = _data_target(directory, directory / args.report)
    snapshots.mkdir(exist_ok=True)
    if args.repositories:
        for repo in json.loads(args.repositories.read_text()):
            target = _data_target(directory, snapshots / (repo.replace('/', '__') + '.json'))
            if not target.exists():
                target.write_text(json.dumps(capture(repo), indent=2) + '\n')
    rows = []
    for path in sorted(snapshots.glob('*.json')):
        path = _data_target(directory, path)
        row = measure(json.loads(path.read_text()), args.engine_root)
        rows.append(row)
        print(path.name + ': ' + row['outcome'], flush=True)
    report = summarize(rows)
    if args.compare:
        report['comparison'] = compare(json.loads(args.compare.read_text())['repositories'], rows)
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ('repositories', 'comparison')}, indent=2))


if __name__ == '__main__':
    main()

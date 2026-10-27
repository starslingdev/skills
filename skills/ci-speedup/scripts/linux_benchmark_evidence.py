"""Bounded, read-only executed-source resolution for OPT92 Swift jobs."""
import base64
import copy
import hashlib
import re
from urllib.parse import quote

import linux_benchmark as lb
from linux_benchmark_logs import swift_provenance
from swift_format_operations import formatter


def swift_shape(job):
    steps = job.get('steps')
    return (isinstance(steps, list) and len(steps) in (3, 4)
            and all(isinstance(s, dict) for s in steps)
            and str(steps[0].get('uses', '')).startswith('actions/checkout@')
            and str(steps[1].get('uses', '')).startswith('swift-actions/setup-swift@')
            and isinstance(steps[2].get('run'), str)
            and formatter(steps[2]['run'].strip()) is not None)


class SwiftEvidence:
    """At most 32 GETs per audit, with exact workflow-era matching.

    API failures, exhausted budget and ambiguous logs return no evidence.
    A workflow comparison includes all jobs, defaults and triggers. Input
    files may differ across commits, but every observed tree is re-admitted.
    """
    def __init__(self, client, repo, workflow_texts, source_sha=None, budget=32):
        self.client, self.repo, self.workflow_texts = client, repo, workflow_texts
        self.source_sha = source_sha
        self.remaining = budget
        self.cache = {}
        self.reason = 'source'

    def get(self, endpoint, text=False):
        cache_key = (endpoint, text)
        if cache_key not in self.cache:
            if self.remaining <= 0:
                self.reason = 'budget'
                return None
            self.remaining -= 1
            method = self.client.text if text else self.client.json
            self.cache[cache_key] = method(endpoint, allow_missing=True)
        return self.cache[cache_key]

    def contents(self, path, sha):
        data = self.get(f'repos/{self.repo}/contents/{quote(path, safe="/")}?ref={sha}')
        if (not isinstance(data, dict) or data.get('type') != 'file'
                or data.get('encoding') != 'base64' or data.get('size', 1_000_001) > 1_000_000):
            return None
        try:
            return base64.b64decode(data['content']).decode('utf-8')
        except (KeyError, ValueError, UnicodeError):
            return None

    def sample(self, wf, doc, key, occurrence):
        self.reason = 'source'
        job = doc['jobs'][key]
        if (not swift_shape(job) or not self.workflow_texts.get(wf)
                or not re.fullmatch(r'[0-9a-f]{40}', str(self.source_sha))):
            return None
        job_id = occurrence.get('id')
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
            return None
        self.reason = 'logs'
        provenance = swift_provenance(self.get(f'repos/{self.repo}/actions/jobs/{job_id}/logs', text=True))
        if not provenance or provenance['toolchains'] != {'swift': '6.1.3'}:
            return None
        self.reason = 'source'
        resolved = copy.deepcopy(doc)
        steps = resolved['jobs'][key]['steps']
        for step, supported in zip(steps[:2], (lb.SWIFT_CHECKOUT, lb.SETUP_SWIFT)):
            action = step['uses']
            revision = provenance['action_revisions'].get(action)
            if action.split('@')[0] + '@' + str(revision) != supported:
                return None
            step['uses'] = supported
        executed = provenance['executed_sha']
        workflow = self.contents(wf, executed)
        if workflow is None or workflow != self.workflow_texts[wf]:
            return None
        modes = self.tree(executed)
        if modes is None:
            return None
        _, config, paths = formatter(steps[2]['run'].strip())
        contents = self.contents(config, executed)
        verdict, _, proof = lb.admit(resolved, key, modes, {config: contents})
        if verdict != 'candidate' or proof['architecture'] != provenance['architecture']:
            return None
        audited_modes = self.tree(self.source_sha)
        audited_config = self.contents(config, self.source_sha)
        if audited_modes is None or audited_config != contents:
            return None
        audited_verdict, _, audited_proof = lb.admit(resolved, key, audited_modes, {config: audited_config})
        if audited_verdict != 'candidate' or audited_proof != proof:
            return None
        provenance.update(workflow_sha256=hashlib.sha256(workflow.encode()).hexdigest(),
                          config_sha256=hashlib.sha256(contents.encode()).hexdigest(),
                          audited_source_sha=self.source_sha)
        return proof, provenance

    def tree(self, sha):
        tree = self.get(f'repos/{self.repo}/git/trees/{sha}?recursive=1')
        if (not isinstance(tree, dict) or tree.get('truncated') is not False
                or not isinstance(tree.get('tree'), list) or len(tree['tree']) > 20000):
            return None
        modes = {}
        for entry in tree['tree']:
            if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
                return None
            if entry.get('type') != 'tree':
                modes[entry['path']] = entry.get('mode')
        return modes

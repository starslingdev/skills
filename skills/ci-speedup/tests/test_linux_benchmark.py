"""Synthetic OPT92 admission, measured history and rendering contracts."""
import importlib.util
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import linux_benchmark as lb
import linux_benchmark_logs as lb_logs
import linux_benchmark_evidence as lb_evidence
import collect_runs as cr
import blocking_path as bp

spec = importlib.util.spec_from_file_location("linux_benchmark_verifier", ROOT / "tests" / "verify_report.py")
vr = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = vr
spec.loader.exec_module(vr)

WF = ".github/workflows/check.yml"
SHA = "a" * 40


def fixture():
    return {"on": ["push", "pull_request"], "jobs": {"syntax": {
        "runs-on": "windows-2022", "steps": [
            {"uses": lb.CHECKOUT},
            {"uses": lb.SETUP_PYTHON, "with": {"python-version": "3.12.7", "architecture": "x64"}},
            {"shell": "bash", "run": "python -I -S -m compileall -q src"},
        ]}}}


def history():
    return {WF: [[{"name": "syntax", "id": 1, "_run_id": 2, "_run_head_sha": SHA,
                   "_run_event": "push",
                   "runner_group_id": 0, "runner_group_name": "GitHub Actions",
                   "labels": ["windows-2022"], "conclusion": "success",
                   "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:02:00Z",
                   "steps": [{"name": "Compile", "conclusion": "success"}]}]]}


class LinuxBenchmarkTests(unittest.TestCase):
    def test_swift_log_provenance_is_scoped_to_known_action_steps(self):
        lines = [
            "Download action repository 'actions/checkout@v5' (SHA:" + lb.SWIFT_CHECKOUT.split('@')[1] + ")",
            "Download action repository 'swift-actions/setup-swift@v2' (SHA:" + lb.SETUP_SWIFT.split('@')[1] + ")",
            "Complete job name: format",
            "##[group]Run actions/checkout@v5",
            "[command]/usr/bin/git log -1 --format=%H", SHA,
            "##[group]Run swift-actions/setup-swift@v2",
            "[command]/tools/swift/usr/bin/swift --version",
            "Apple Swift version 6.1.3 (swift-6.1.3-RELEASE)",
            "Target: arm64-apple-macosx15.0",
            "##[group]Run swift format lint --recursive --configuration .style Code/",
        ]
        def log(values):
            return '\n'.join('2026-01-01T00:00:00.000Z ' + v for v in values)
        proof = lb_logs.swift_provenance(log(lines))
        self.assertEqual(proof['executed_sha'], SHA)
        self.assertEqual(proof['toolchains'], {'swift': '6.1.3'})
        self.assertEqual(proof['architecture'], 'arm64')
        # A convenient version or checkout hash in later command output cannot
        # replace missing evidence from the action that actually selected it.
        for index in (4, 5, 7, 8, 9):
            broken = lines[:index] + lines[index + 1:] + lines[4:10]
            self.assertIsNone(lb_logs.swift_provenance(log(broken)))
        self.assertIsNone(lb_logs.swift_provenance(log(lines[:7] + lines[7:10] + lines[7:])))
        self.assertIsNone(lb_logs.swift_provenance('untimestamped\n' + log(lines)))
        self.assertIsNone(lb_logs.swift_provenance(log(lines).replace('Run actions/checkout@v5', 'Run ./wrapper')))

    def test_swift_executed_source_matches_workflow_and_checks_each_tree(self):
        doc = self.swift_fixture()
        lines = [
            "Download action repository '" + lb.SWIFT_CHECKOUT + "' (SHA:" + lb.SWIFT_CHECKOUT.split('@')[1] + ")",
            "Download action repository '" + lb.SETUP_SWIFT + "' (SHA:" + lb.SETUP_SWIFT.split('@')[1] + ")",
            "##[group]Run " + lb.SWIFT_CHECKOUT,
            "[command]/usr/bin/git log -1 --format=%H", "b" * 40,
            "##[group]Run " + lb.SETUP_SWIFT,
            "[command]/tools/bin/swift --version", "Apple Swift version 6.1.3 (release)",
            "Target: arm64-apple-macosx15.0", "##[group]Run swift format lint",
        ]
        log = '\n'.join('2026-01-01T00:00:00.000Z ' + v for v in lines)
        def content(value):
            return {'type': 'file', 'encoding': 'base64', 'size': len(value),
                    'content': base64.b64encode(value.encode()).decode()}
        class Client:
            workflow = 'same workflow'
            mode = '100644'
            audited_mode = '100644'
            audited_config = '{}'
            truncated = False
            def text(self, endpoint, **kwargs):
                return log
            def json(self, endpoint, **kwargs):
                if '/git/trees/' in endpoint:
                    mode = self.audited_mode if SHA in endpoint else self.mode
                    return {'truncated': self.truncated, 'tree': [
                        {'path': '.style', 'mode': '100644', 'type': 'blob'},
                        {'path': 'Code/a.swift', 'mode': mode, 'type': 'blob'}]}
                return content(self.workflow if WF in endpoint else self.audited_config if SHA in endpoint else '{}')
        client = Client()
        def resolver(budget=32):
            return lb_evidence.SwiftEvidence(client, 'owner/repo', {WF: 'same workflow'}, SHA, budget)
        data = history()
        data[WF][0][0].update(name='format', labels=['macos-15'], _run_event='pull_request')
        held = []
        findings = cr._detect_opt92_linux_benchmark({WF: doc}, data, SHA, {WF: 10},
                                                   withheld=held, swift_evidence=resolver())
        self.assertFalse(held)
        self.assertEqual(len(findings), 1)
        evidence = findings[0]['linux_benchmark']
        self.assertEqual(evidence['observations'][0]['head_sha'], SHA)
        self.assertEqual(evidence['observations'][0]['provenance']['executed_sha'], 'b' * 40)
        self.assertEqual(evidence['history_basis'], 'executed-workflow')
        for field, value in [('workflow', 'changed workflow'), ('mode', '120000'), ('truncated', True),
                             ('audited_mode', '120000'), ('audited_config', '{"version": 2}')]:
            previous = getattr(client, field)
            setattr(client, field, value)
            self.assertIsNone(resolver().sample(WF, doc, 'format', data[WF][0][0]))
            setattr(client, field, previous)
        self.assertIsNone(resolver(1).sample(WF, doc, 'format', data[WF][0][0]))
        data[WF].append([dict(data[WF][0][0], id=3, _run_id=4)])
        limited = cr._detect_opt92_linux_benchmark({WF: doc}, data, SHA, {WF: 10},
                                                  swift_evidence=resolver(6))
        self.assertEqual(limited[0]['linux_benchmark']['sample_exclusions'], {'budget': 1})
        report = '\n'.join(bp._opt92_unrouted_block(limited, 'https://example.invalid/catalog'))
        with tempfile.TemporaryDirectory(prefix='review-opt92-budget-') as tmp:
            path = Path(tmp) / 'review-findings.json'
            path.write_text(json.dumps({'findings': limited}))
            self.assertTrue(vr.check_opt92_linux_benchmark(report, path).ok)
            without_limit = '\n'.join(line for line in report.splitlines() if '**Sample limits:**' not in line)
            self.assertFalse(vr.check_opt92_linux_benchmark(without_limit, path).ok)

    def swift_fixture(self):
        return {"on": "push", "jobs": {"format": {"runs-on": "macos-15", "steps": [
            {"uses": lb.SWIFT_CHECKOUT},
            {"uses": lb.SETUP_SWIFT, "with": {"swift-version": "6.1.3"}},
            {"run": "swift format lint --recursive --configuration .style Code/"},
        ]}}}

    def test_swift_format_requires_source_config_and_reviewed_actions(self):
        files = {".style": "100644", "Code/example.swift": "100644"}
        contents = {".style": '{"version": 1}'}
        doc = self.swift_fixture()
        verdict, _, proof = lb.admit(doc, "format", files, contents)
        self.assertEqual(verdict, "candidate")
        self.assertEqual(proof["required_toolchain"], {"swift": "6.1.3"})
        self.assertEqual(proof["benchmark_architecture"], "x64")
        self.assertEqual(proof["architecture"], "arm64")
        self.assertNotIn('copy/format/diff', '; '.join(proof['checks']))
        for bad_contents in ({}, {".style": "not json"}, {".style": '{"version": 2}'}):
            self.assertEqual(lb.admit(doc, "format", files, bad_contents)[0], "hold")
        for mode in ("120000", "160000"):
            bad_files = dict(files, **{"Code/linked": mode})
            self.assertEqual(lb.admit(doc, "format", bad_files, contents)[0], "hold")
        for action in ("swift-actions/setup-swift@v2", "example/setup@v1"):
            doc["jobs"]["format"]["steps"][1]["uses"] = action
            self.assertEqual(lb.admit(doc, "format", files, contents)[0], "hold")

    def test_swift_format_never_admits_builds_or_shell_substitutions(self):
        for command in ("swift build", "swift test", "swift package plugin lint",
                        "swift format lint --recursive --configuration .style $(target)",
                        "swift format lint --recursive --configuration .style Code/; swift build"):
            doc = self.swift_fixture()
            doc["jobs"]["format"]["steps"][-1]["run"] = command
            self.assertEqual(lb.admit(doc, "format", {".style": "100644", "Code/a.swift": "100644"},
                                     {".style": "{}"})[0], "hold")

    def test_swift_copy_format_diff_block_is_bounded_and_source_checked(self):
        command = '''cp -r Code Code.bak
swift format --in-place --recursive --configuration .style Code/ || true
if ! diff -r Code Code.bak > /dev/null 2>&1; then
  echo "formatting changed"
  diff -r Code Code.bak || true
  exit 1
fi
echo "formatting checked"
'''
        files = {'.style': '100644', 'Code/example.swift': '100644'}
        def admit(block=command, modes=files):
            doc = self.swift_fixture()
            doc['jobs']['format']['steps'].append({'run': block})
            return lb.admit(doc, 'format', modes, {'.style': '{}'})
        self.assertEqual(admit()[0], 'candidate')
        self.assertIn('copy/format/diff', '; '.join(admit()[2]['checks']))
        for block in (command + 'swift build\n',
                      command.replace('exit 1', 'xcodebuild\nexit 1'),
                      command.replace('formatting checked', '$(uname) checked'),
                      command.replace('formatting checked', '`uname` checked'),
                      command.replace('Code Code.bak', '/ Code.bak', 1),
                      command.replace('Code/ || true', 'Code/ Other/ || true'),
                      command.replace('--configuration .style', '--configuration .other'),
                      command.replace('exit 1', 'exit 0'),
                      command.replace('swift format', './wrapper.sh'),
                      command.replace('then', 'then\ntrue'),
                      command + 'true &\n'):
            with self.subTest(block=block):
                self.assertEqual(admit(block)[0], 'skip' if 'xcodebuild' in block else 'hold')
        for path, mode in [('Code/link', '120000'), ('Code/module', '160000'), ('Code.bak/existing', '100644')]:
            self.assertEqual(admit(modes=dict(files, **{path: mode}))[0], 'hold')

    def test_swift_history_requires_observed_toolchain_not_setup_success(self):
        observations = history()
        job = observations[WF][0][0]
        job.update(name="format", labels=["macos-15"])
        args = ({WF: self.swift_fixture()}, observations, SHA, {WF: 10})
        kwargs = {"source_files": {".style": "100644", "Code/a.swift": "100644"},
                  "source_contents": {".style": "{}"}}
        self.assertFalse(cr._detect_opt92_linux_benchmark(*args, **kwargs))
        job["_opt92_toolchains"] = {"swift": "6.1.2"}
        self.assertFalse(cr._detect_opt92_linux_benchmark(*args, **kwargs))
        job["_opt92_toolchains"] = {"swift": "6.1.3"}
        self.assertEqual(len(cr._detect_opt92_linux_benchmark(*args, **kwargs)), 1)

    def detect(self, doc=None, observations=None, sha=SHA):
        held = []
        out = cr._detect_opt92_linux_benchmark({WF: doc or fixture()},
                    history() if observations is None else observations, sha, {WF: 10}, withheld=held, source_files={"src/check.py": "100644"})
        return out, held

    def test_positive_syntax_and_whitespace(self):
        doc = fixture()
        self.assertEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[0], "candidate")
        doc["jobs"]["syntax"]["steps"] = [
            {"uses": lb.CHECKOUT},
            {"shell": "bash", "run": "git show --check --format= --no-ext-diff --no-textconv HEAD"}]
        self.assertEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[0], "candidate")

    def test_generic_native_and_unknown_commands_never_fire(self):
        for command in ["dotnet build app.csproj", "dotnet publish -r osx-arm64",
                        "dotnet workload install maui-windows", "cargo build --target aarch64-apple-darwin",
                        "flutter build ios", "npx react-native run-ios", "npm run package",
                        "cargo tauri build", "xcodebuild test", "./nested-wrapper.sh",
                        "pwsh ./test.ps1", "python -m compileall -q src", "cmake --build build"]:
            with self.subTest(command=command):
                doc = fixture()
                doc["jobs"]["syntax"]["steps"][-1]["run"] = command
                self.assertNotEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[0], "candidate")

    def test_native_project_imports_and_local_action_wrappers_do_not_escape(self):
        files = {'app/app.csproj': '100644', 'Directory.Build.props': '100644',
                 '.github/actions/mobile/action.yml': '100644', 'scripts/outer.sh': '100644',
                 'scripts/inner.sh': '100644'}
        contents = {
            'app/app.csproj': '<Project><Import Project="../Directory.Build.props" /></Project>',
            'Directory.Build.props': '<Project><PropertyGroup><TargetFramework>net10.0-windows</TargetFramework><UseWinUI>true</UseWinUI></PropertyGroup></Project>',
            '.github/actions/mobile/action.yml': 'runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: xcodebuild test\n',
            'scripts/outer.sh': './scripts/inner.sh\n',
            'scripts/inner.sh': 'xcodebuild test\n',
        }
        for step in ({'run': 'dotnet build app/app.csproj -p:StoreBuild=true', 'shell': 'bash'},
                     {'run': 'dotnet test app/app.csproj', 'shell': 'bash'},
                     {'uses': './.github/actions/mobile'},
                     {'run': './scripts/outer.sh', 'shell': 'bash'}):
            doc = fixture()
            doc['jobs']['syntax']['steps'] = [{'uses': lb.CHECKOUT}, step]
            with self.subTest(step=step):
                self.assertEqual(lb.admit(doc, 'syntax', files, contents)[0], 'hold')

    def test_unknown_actions_and_revision_hold(self):
        for action in ["./.github/actions/nested", "example/action@v1", "actions/checkout@v4"]:
            doc = fixture()
            doc["jobs"]["syntax"]["steps"][0]["uses"] = action
            self.assertEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[0], "hold")

    def test_runner_expression_and_release_hold(self):
        for runner in ["${{ matrix.os }}", ["self-hosted", "Windows"], "windows-custom", "macos-latest"]:
            doc = fixture()
            doc["jobs"]["syntax"]["runs-on"] = runner
            self.assertEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[0], "hold")
        doc = fixture()
        doc["on"] = {"push": {"tags": ["v*"]}}
        self.assertEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[1], "release_path")

    def test_setup_and_environment_do_not_prove_portability(self):
        doc = fixture()
        doc["jobs"]["syntax"]["steps"].pop()
        self.assertEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[0], "hold")
        doc = fixture()
        doc["env"] = {"PYTHONPATH": "native"}
        self.assertEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[0], "hold")

    def test_manual_trigger_alone_is_not_a_release_path(self):
        doc = fixture()
        doc["on"] = {"push": None, "pull_request": None, "workflow_dispatch": None}
        findings, held = self.detect(doc)
        self.assertEqual(len(findings), 1)
        self.assertFalse(held)
        doc["jobs"]["syntax"]["environment"] = "production"
        self.assertFalse(self.detect(doc)[0])
        doc["jobs"]["syntax"].pop("environment")
        doc["on"]["release"] = {"types": ["published"]}
        self.assertFalse(self.detect(doc)[0])

    def test_unknown_input_tree_symlinks_and_architecture_hold(self):
        self.assertEqual(lb.admit(fixture(), "syntax")[0], "hold")
        self.assertEqual(lb.admit(fixture(), "syntax", {"src/check.py": "120000"})[0], "hold")
        self.assertEqual(lb.admit(fixture(), "syntax", {"other.py": "100644"})[0], "hold")
        doc = fixture()
        doc["jobs"]["syntax"]["steps"][1]["with"]["architecture"] = "arm64"
        self.assertEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[0], "hold")

    def test_disabled_or_background_work_does_not_establish_portability(self):
        for key, value in [("if", False), ("background", True), ("continue-on-error", True)]:
            doc = fixture()
            doc["jobs"]["syntax"]["steps"][-1][key] = value
            self.assertEqual(lb.admit(doc, "syntax", {"src/check.py": "100644"})[0], "hold")

    def test_native_arguments_are_not_inferred_from_printed_prose(self):
        self.assertTrue(lb.native_command("dotnet publish -r osx-arm64"))
        self.assertTrue(lb.native_command("cargo build --target=aarch64-apple-darwin"))
        self.assertTrue(lb.native_command("dotnet workload install maui-windows"))
        self.assertFalse(lb.native_command('echo "xcodebuild"'))
        self.assertFalse(lb.native_command("dotnet test --filter net8.0-windows"))

    def test_executed_same_commit_history_required(self):
        for observations, sha in [({}, SHA), (history(), None), (history(), "b" * 40)]:
            out, held = self.detect(observations=observations, sha=sha)
            self.assertFalse(out)
            self.assertEqual(held[0]["gate"], "history_unmatched")
        data = history()
        data[WF][0][0]["conclusion"] = "skipped"
        self.assertFalse(self.detect(observations=data)[0])
        data = history()
        data[WF][0][0]["_run_event"] = "pull_request"
        self.assertFalse(self.detect(observations=data)[0])
        data = history()
        data[WF][0][0]["runner_group_id"] = 42
        self.assertFalse(self.detect(observations=data)[0])

    def test_measurement_and_null_savings(self):
        findings, held = self.detect()
        self.assertFalse(held)
        finding = findings[0]
        self.assertEqual(finding["linux_benchmark"]["job_p50_s"], 120)
        self.assertEqual(finding["linux_benchmark"]["sample_count"], 1)
        self.assertIsNone(finding["wall_clock_p50_s"])
        self.assertIsNone(finding["runner_min_saving"])
        self.assertEqual(finding["realization"], "none")
        self.assertFalse(bp._is_tier2_finding(finding))

    def test_literal_runner_non_os_matrix_matches_each_leg(self):
        doc = fixture()
        job = doc["jobs"]["syntax"]
        job["name"] = "syntax (${{ matrix.python }})"
        job["strategy"] = {"matrix": {"python": ["3.12.7", "3.13.1"]}}
        job["steps"][1]["with"]["python-version"] = "${{ matrix.python }}"
        observations = history()
        first = observations[WF][0][0]
        first["name"] = "syntax (3.12.7)"
        observations[WF][0].append(dict(first, name="syntax (3.13.1)", id=3,
                                       completed_at="2026-01-01T00:03:00Z"))
        findings, held = self.detect(doc, observations)
        self.assertFalse(held)
        proofs = {f["linux_benchmark"]["job"]: f["linux_benchmark"] for f in findings}
        self.assertEqual(set(proofs), {"syntax (3.12.7)", "syntax (3.13.1)"})
        self.assertEqual(proofs["syntax (3.12.7)"]["job_p50_s"], 120)
        self.assertEqual(proofs["syntax (3.13.1)"]["job_p50_s"], 180)
        self.assertIn("3.13.1", proofs["syntax (3.13.1)"]["checks"][0])
        # One leg's observation must never stand in for an unobserved leg.
        observations[WF][0].pop()
        findings, held = self.detect(doc, observations)
        self.assertEqual(len(findings), 1)
        self.assertEqual(held, [{"workflow_file": WF, "job": "syntax (3.13.1)",
                                "gate": "history_unmatched"}])

    def test_matrix_unknowns_and_colliding_names_hold(self):
        for matrix in ({"python": ["3.12.7", "3.12.7"]},
                       {"python": "${{ fromJSON(needs.setup.outputs.versions) }}"},
                       {"include": [{"python": "3.12.7"}]},
                       {"os": ["windows-2022", "ubuntu-24.04"]},
                       {"python": [str(i) for i in range(33)]}):
            doc = fixture()
            job = doc["jobs"]["syntax"]
            job["name"] = "syntax (${{ matrix.python }})"
            job["strategy"] = {"matrix": matrix}
            with self.subTest(matrix=matrix):
                self.assertIsNone(lb.expand_jobs("syntax", job))
                self.assertFalse(self.detect(doc)[0])
        doc = fixture()
        job = doc["jobs"]["syntax"]
        job["name"] = "syntax"
        job["strategy"] = {"matrix": {"python": ["3.12.7"]}}
        doc["jobs"]["collision"] = dict(job)
        self.assertFalse(self.detect(doc)[0])
        doc = fixture()
        doc["jobs"]["collision"] = {
            "name": "${{ matrix.check }}", "runs-on": "ubuntu-latest",
            "strategy": {"matrix": "${{ fromJSON(needs.setup.outputs.matrix) }}"},
            "steps": [{"run": "unknown-check"}]}
        self.assertFalse(self.detect(doc)[0])

    def test_matrix_does_not_resolve_runner_or_admit_native_leg(self):
        doc = fixture()
        job = doc["jobs"]["syntax"]
        job["name"] = "syntax (${{ matrix.target }})"
        job["strategy"] = {"matrix": {"target": ["src", "native"]}}
        job["steps"][-1]["run"] = "dotnet publish -r osx-${{ matrix.target }}"
        self.assertFalse(self.detect(doc)[0])
        job["runs-on"] = "${{ matrix.target }}"
        self.assertIsNone(lb.expand_jobs("syntax", job))

    def test_card_and_verifier_reject_missing_or_credited_cards(self):
        findings, _ = self.detect()
        report = "\n".join(bp._opt92_unrouted_block(findings, "https://example.invalid/catalog"))
        with tempfile.TemporaryDirectory(prefix="review-opt92-") as tmp:
            path = Path(tmp) / "review-findings.json"
            path.write_text(json.dumps({"findings": findings}))
            self.assertTrue(vr.check_opt92_linux_benchmark(report, path).ok)
            self.assertFalse(vr.check_opt92_linux_benchmark(report + report, path).ok)
            self.assertFalse(vr.check_opt92_linux_benchmark("", path).ok)
            findings[0]["runner_min_saving"] = 10
            path.write_text(json.dumps({"findings": findings}))
            self.assertFalse(vr.check_opt92_linux_benchmark(report, path).ok)

    def test_pole_identity_and_shared_refusal_contract(self):
        findings, _ = self.detect()
        self.assertEqual(bp._opt92_for_pole({"workflow_file": WF, "job": "syntax"}, findings), findings)
        self.assertFalse(bp._opt92_for_pole({"workflow_file": ".github/workflows/other.yml", "job": "syntax"}, findings))
        self.assertEqual(lb.PHRASES, bp._WITHHELD_PHRASES_BY_KEY["opt92_withheld_candidates"])
        self.assertEqual(lb.PHRASES, vr._VR_WITHHELD_PHRASES_BY_KEY["opt92_withheld_candidates"])

    def test_verifier_binds_runtime_and_observations_to_each_card(self):
        findings, _ = self.detect()
        second = json.loads(json.dumps(findings[0]))
        second["id"] = "f2"
        second["linux_benchmark"].update(job="other", job_p50_s=180)
        second["linux_benchmark"]["observations"][0].update(job="other", job_id=3, duration_s=180)
        findings.append(second)
        report = "\n".join(bp._opt92_unrouted_block(findings, "https://example.invalid/catalog"))
        with tempfile.TemporaryDirectory(prefix="review-opt92-identity-") as tmp:
            path = Path(tmp) / "review-findings.json"
            path.write_text(json.dumps({"findings": findings}))
            self.assertTrue(vr.check_opt92_linux_benchmark(report, path).ok)
            swapped = report.replace("120s p50", "SWAP").replace("180s p50", "120s p50").replace("SWAP", "180s p50")
            self.assertFalse(vr.check_opt92_linux_benchmark(swapped, path).ok)
            findings[0]["linux_benchmark"]["observations"][0]["job"] = "other"
            path.write_text(json.dumps({"findings": findings}))
            self.assertFalse(vr.check_opt92_linux_benchmark(report, path).ok)

    def test_full_render_places_card_once_with_and_without_pole(self):
        findings, _ = self.detect()
        for job in ("syntax", "other", None):
            poles = [] if job is None else [{"check": job, "job": job, "workflow_file": WF,
                "p50_s": 120, "job_p50_s": 120, "dominant_step": "Compile", "dominant_p50_s": 90,
                "steps": [{"step": "Compile", "category": "build", "p50_s": 90}]}]
            doc = {"repo": "owner/repo", "scanned_at": "2026-01-01T00:00:00Z", "findings": findings,
                   "data_sources": {"runs_sampled": 1, "jobs_sampled": 1, "workflows_analyzed": 1},
                   "pr_critical_path": {"sampled_pr_count": 1, "sample_target": 1,
                                        "sample_complete": True, "poles": poles}}
            report = bp.render(doc, {}, {}, {}, "2026-01-01")
            self.assertEqual(report.count('<a id="opt92-f1"></a>'), 1)
            self.assertEqual('id="linux-benchmarks"' in report, job != "syntax")
            with tempfile.TemporaryDirectory(prefix="review-opt92-render-") as tmp:
                path = Path(tmp) / "review-findings.json"
                path.write_text(json.dumps(doc))
                checked = vr.check_opt92_linux_benchmark(report, path)
                self.assertTrue(checked.ok, checked.detail)
            appendix, _, _ = bp._also_noticed_block(findings, "https://example.invalid/catalog")
            self.assertNotIn("OPT92", "\n".join(appendix))


if __name__ == "__main__":
    unittest.main()

"""Synthetic verdict graphs: no repository commands or network calls."""
import importlib.util
from pathlib import Path
import unittest

_path = Path(__file__).resolve().parents[1] / "scripts" / "verdict_assertions.py"
_spec = importlib.util.spec_from_file_location("verdict_oracle", _path)
va = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(va)
_fact_spec = importlib.util.spec_from_file_location("verdict_fact_oracle", _path.with_name("config_facts.py"))
cf = importlib.util.module_from_spec(_fact_spec)
_fact_spec.loader.exec_module(cf)


def graph(assertion, include_detector=False):
    return {
        "changes": {"runs-on": "ubuntu-latest", "outputs": {"changed": "false"}, "steps": [{"run": "detect-changes"}]},
        "suite": {"needs": "changes", "if": "needs.changes.outputs.changed == 'true'",
                  "runs-on": "ubuntu-latest", "steps": [{"run": "run-tests"}]},
        "verdict": {"needs": ["changes", "suite"] if include_detector else ["suite"],
                    "if": "always()", "runs-on": "ubuntu-latest",
                    "steps": [{"run": assertion}]},
    }


FAIL_ONLY = 'test "${{ !contains(needs.*.result, \'failure\') }}" = "true"'
STRICT = 'test "${{ needs.suite.result }}" = "success"'
SAFE = ('test "${{ needs.changes.result == \'success\' && '
        "((needs.changes.outputs.changed == 'false' && needs.suite.result == 'skipped') || "
        "(needs.changes.outputs.changed == 'true' && needs.suite.result == 'success'))"
        ' }}" = "true"')
GUARD = '''if [[ "$RESULT" != "success" || ! "$CHANGED" =~ ^(true|false)$ ]]; then
  echo "Detection did not return a valid result."
  exit 1
fi
'''


class VerdictTests(unittest.TestCase):
    def outcome(self, jobs):
        return va.analyze(jobs, "verdict")[0]

    def test_omitted_detector_fails(self):
        self.assertEqual(self.outcome(graph(FAIL_ONLY)), "fail")

    def test_listed_but_ignored_detector_fails(self):
        assertion = 'test "${{ needs.suite.result != \'failure\' }}" = "true"'
        self.assertEqual(self.outcome(graph(assertion, True)), "fail")

    def test_cancellation_is_not_failure(self):
        self.assertEqual(self.outcome(graph(FAIL_ONLY, True)), "fail")

    def test_matrix_suite_aggregate_keeps_strict_result_semantics(self):
        jobs = graph(STRICT)
        jobs['suite']['strategy'] = {'matrix': {'runtime': ['stable', 'next']}}
        self.assertEqual(self.outcome(jobs), 'pass')
        jobs['verdict']['steps'][0]['run'] = FAIL_ONLY
        self.assertEqual(self.outcome(jobs), 'fail')

    def test_matrix_detector_outputs_and_jq_assertions_remain_unmeasured(self):
        jobs = graph(SAFE, True)
        jobs['changes']['strategy'] = {'matrix': {'part': ['one', 'two']}}
        self.assertEqual(self.outcome(jobs), 'unmeasured')
        jobs = graph('jq -e \'all(.[]; .result == "success")\' <<< "$NEEDS"', True)
        jobs['verdict']['steps'][0]['env'] = {'NEEDS': '${{ toJSON(needs) }}'}
        self.assertEqual(self.outcome(jobs), 'unmeasured')

    def test_false_output_before_failure_is_not_unchanged(self):
        assertion = ('test "${{ needs.changes.outputs.changed == \'false\' || '
                     'needs.suite.result == \'success\' }}" = "true"')
        self.assertEqual(self.outcome(graph(assertion, True)), "fail")

    def test_explicit_success_and_valid_unchanged_passes(self):
        self.assertEqual(self.outcome(graph(SAFE, True)), "pass")

    def test_bash_guard_validates_detection_before_allowing_skip(self):
        jobs = graph('test "${{ needs.changes.outputs.changed == \'false\' || needs.suite.result == \'success\' }}" = true', True)
        jobs["verdict"]["steps"].insert(0, {"shell": "bash", "run": GUARD, "env": {
            "RESULT": "${{ needs.changes.result }}", "CHANGED": "${{ needs.changes.outputs.changed }}"}})
        self.assertEqual(self.outcome(jobs), "pass")
        jobs["verdict"]["steps"][0]["continue-on-error"] = True
        self.assertEqual(self.outcome(jobs), "fail")

    def test_bash_guard_accepts_only_success_and_valid_boolean(self):
        for result in ("success", "failure", "cancelled", "skipped"):
            for changed in ("true", "false", "", "invalid", "TRUE", "src/**", '"false"',
                            "false\ntrue", "$(exit 0)"):
                with self.subTest(result=result, changed=changed):
                    actual = va.run_step(GUARD, {"RESULT": result, "CHANGED": changed}, {}, {}, [])
                    self.assertEqual(actual, result == "success" and changed in ("true", "false"))

    def test_bash_guard_unknown_syntax_and_shell_hold(self):
        env = {"RESULT": "failure", "CHANGED": "false"}
        for source in (GUARD + "echo done\n", GUARD.replace("exit 1", "false || true"),
                       GUARD.replace("^(true|false)$", ".*"),
                       GUARD.replace('"success"', "success*")):
            with self.subTest(source=source), self.assertRaises(va.Unknown):
                va.run_step(source, env, {}, {}, [])
        with self.assertRaises(va.Unknown):
            va.run_step(GUARD, env, {}, {}, [], shell="sh")

    def test_strict_suite_success_rejects_omitted_detector_failure(self):
        self.assertEqual(self.outcome(graph(STRICT)), "pass")

    def test_invalid_output_must_not_allow_skip(self):
        assertion = ('test "${{ needs.changes.result == \'success\' && '
                     'needs.suite.result != \'failure\' && '
                     'needs.suite.result != \'cancelled\' }}" = "true"')
        jobs = graph(assertion, True)
        jobs["changes"]["outputs"]["changed"] = "invalid"
        self.assertEqual(self.outcome(jobs), "fail")

    def test_echo_is_not_assertion(self):
        self.assertEqual(self.outcome(graph('echo "${{ needs.suite.result }}"')), "fail")

    def test_disabled_assertion_does_not_gate(self):
        jobs = graph(STRICT)
        jobs["verdict"]["steps"][0]["if"] = "false"
        self.assertEqual(self.outcome(jobs), "fail")

    def test_failed_detector_takes_no_change_branch_before_unknown_action(self):
        jobs = graph(STRICT, True)
        jobs["changes"]["outputs"]["changed"] = "${{ steps.detect.outputs.changed }}"
        jobs["verdict"]["steps"] = [
            {"if": "needs.changes.outputs.changed != 'true'", "run": 'echo "unchanged"'},
            {"if": "needs.changes.outputs.changed == 'true'", "uses": "example/verdict@v1"}]
        self.assertEqual(self.outcome(jobs), "fail")

    def test_conditional_failure_only_steps_accept_cancellation(self):
        jobs = graph(STRICT)
        jobs["verdict"]["steps"] = [
            {"if": "!contains(needs.*.result, 'failure')", "run": "exit 0"},
            {"if": "contains(needs.*.result, 'failure')", "run": "exit 1"}]
        self.assertEqual(self.outcome(jobs), "fail")

    def test_tolerated_assertion_cannot_gate(self):
        jobs = graph(STRICT)
        jobs["verdict"]["steps"][0]["continue-on-error"] = True
        self.assertEqual(self.outcome(jobs), "fail")

    def test_unknown_action_is_unmeasured(self):
        jobs = graph(STRICT)
        jobs["verdict"]["steps"] = [{"uses": "example/verdict@v1"}]
        self.assertEqual(self.outcome(jobs), "unmeasured")

    def test_verified_action_requires_success_by_default(self):
        jobs = graph(STRICT)
        jobs["verdict"]["steps"] = [{"uses": va.ALLS_GREEN, "with": {"jobs": "${{ toJSON(needs) }}"}}]
        self.assertEqual(self.outcome(jobs), "pass")
        jobs["verdict"]["steps"][0]["continue-on-error"] = True
        self.assertEqual(self.outcome(jobs), "fail")

    def test_verified_action_allowed_lists_can_bypass_missing_suite(self):
        for name in ("allowed-skips", "allowed-failures"):
            for value in ("suite", '["suite"]'):
                jobs = graph(STRICT)
                jobs["verdict"]["steps"] = [{"uses": va.ALLS_GREEN,
                    "with": {"jobs": "${{ toJSON(needs) }}", name: value}}]
                with self.subTest(name=name, value=value):
                    self.assertEqual(self.outcome(jobs), "fail")
        step = {"uses": va.ALLS_GREEN, "with": {
            "jobs": "${{ toJSON(needs) }}", "allowed-failures": "suite"}}
        self.assertTrue(va.action_success(step, {}, {"suite": "cancelled"}, ["suite"]))
        step["with"] = {"jobs": "${{ toJSON(needs) }}", "allowed-skips": "suite"}
        self.assertFalse(va.action_success(step, {}, {"suite": "cancelled"}, ["suite"]))

    def test_action_revision_inputs_and_environment_must_be_resolved(self):
        cases = [
            {"uses": "re-actors/alls-green@v1", "with": {"jobs": "${{ toJSON(needs) }}"}},
            {"uses": va.ALLS_GREEN, "with": {"jobs": '{"suite":{"result":"success"}}'}},
            {"uses": va.ALLS_GREEN, "with": {"jobs": "${{ toJSON(needs) }}", "allowed-skips": "${{ inputs.skip }}"}},
            {"uses": va.ALLS_GREEN, "with": {"jobs": "${{ toJSON(needs) }}", "allowed-failures": '{"suite":true}'}},
            {"uses": va.ALLS_GREEN, "env": {"BASH_ENV": "./override.sh"}, "with": {"jobs": "${{ toJSON(needs) }}"}},
        ]
        for step in cases:
            jobs = graph(STRICT)
            jobs["verdict"]["steps"] = [step]
            with self.subTest(step=step):
                self.assertEqual(self.outcome(jobs), "unmeasured")

    def test_multihop_failure(self):
        jobs = graph(FAIL_ONLY)
        jobs["prepare"] = {"needs": "changes", "steps": [{"run": "prepare"}]}
        jobs["suite"]["needs"] = "prepare"
        jobs["suite"].pop("if")
        self.assertEqual(self.outcome(jobs), "fail")

    def test_swallowing_intermediate_unmeasured(self):
        jobs = graph(STRICT)
        jobs["changes"]["needs"] = "root"
        jobs["changes"]["if"] = "always()"
        jobs["root"] = {"steps": [{"run": "prepare"}]}
        self.assertEqual(self.outcome(jobs), "unmeasured")

    def test_reusable_dependency_unmeasured(self):
        jobs = graph(STRICT)
        jobs["suite"]["uses"] = "./.github/workflows/suite.yml"
        self.assertEqual(self.outcome(jobs), "unmeasured")

    def test_expression_output_cannot_evade_strict_proof(self):
        assertion = ('test "${{ needs.suite.result == \'success\' || '
                     'needs.changes.outputs.changed == \'false\' }}" = "true"')
        self.assertEqual(self.outcome(graph(assertion, True)), "fail")

    def test_env_exit_propagates(self):
        jobs = graph("exit $STATUS")
        jobs["verdict"]["env"] = {"STATUS": "${{ needs.suite.result == 'success' && '0' || '1' }}"}
        self.assertEqual(self.outcome(jobs), "pass")

    def test_single_quoted_shell_variables_cannot_prove_an_assertion(self):
        for variable in ("$RESULT", "${RESULT}"):
            jobs = graph("\n".join(
                f"test '{variable}' != {state}"
                for state in ("failure", "cancelled", "skipped")))
            jobs["verdict"]["env"] = {"RESULT": "${{ needs.suite.result }}"}
            with self.subTest(variable=variable):
                self.assertEqual(self.outcome(jobs), "unmeasured")

    def test_double_quoted_variables_and_single_quoted_expressions_are_distinct(self):
        jobs = graph('test "$RESULT" = success')
        jobs["verdict"]["env"] = {"RESULT": "${{ needs.suite.result }}"}
        self.assertEqual(self.outcome(jobs), "pass")
        self.assertEqual(self.outcome(graph("test '${{ needs.suite.result }}' = success")), "pass")

    def test_step_success_is_not_upstream_success(self):
        jobs = graph('test "${{ success() }}" = true')
        self.assertEqual(self.outcome(jobs), "unmeasured")

    def test_unknown_shell_and_cycle_hold(self):
        jobs = graph(STRICT)
        jobs["verdict"]["steps"][0]["shell"] = "bash {0}"
        self.assertEqual(self.outcome(jobs), "unmeasured")
        jobs = graph(STRICT)
        jobs["changes"]["needs"] = "suite"
        self.assertEqual(self.outcome(jobs), "unmeasured")

    def test_required_fact_keeps_api_gate_and_analyzes_assertion(self):
        fetch = lambda repo: (["verdict"], "synthetic branch protection")
        for assertion, expected in [(FAIL_ONLY, "fail"), (STRICT, "pass"), ("./unknown.sh", "unmeasured")]:
            docs = [("ci.yml", {"on": ["pull_request"], "jobs": graph(assertion)})]
            self.assertEqual(cf._required_checks_skippable(docs, "owner/repo", fetch)[0], expected)
            self.assertEqual(cf._required_checks_skippable(docs, None, fetch)[0], "unmeasured")

    def test_ordinary_suite_does_not_require_a_verdict_assertion(self):
        for source in ("pytest -q", "pytest -q\n", "# Run our own suite\n\npytest -q\n"):
            jobs = graph(source)
            docs = [("ci.yml", {"on": ["pull_request"], "jobs": jobs})]
            outcome = cf._required_checks_skippable(docs, "owner/repo", lambda _: (["verdict"], "test"))
            with self.subTest(source=source):
                self.assertEqual(outcome[0], "pass")

    def test_unexecuted_suite_text_does_not_exempt_a_verdict(self):
        for source in ("exit 0\npytest -q\n", "if false; then\npytest -q\nfi",
                       "cat <<'EOF'\npytest -q\nEOF\n"):
            jobs = graph(source)
            docs = [("ci.yml", {"on": ["pull_request"], "jobs": jobs})]
            outcome = cf._required_checks_skippable(docs, "owner/repo", lambda _: (["verdict"], "test"))
            with self.subTest(source=source):
                self.assertNotEqual(outcome[0], "pass")
        jobs = graph("pytest -q\n")
        jobs["verdict"]["steps"][0]["if"] = False
        docs = [("ci.yml", {"on": ["pull_request"], "jobs": jobs})]
        self.assertEqual(cf._required_checks_skippable(
            docs, "owner/repo", lambda _: (["verdict"], "test"))[0], "fail")


if __name__ == "__main__":
    unittest.main()

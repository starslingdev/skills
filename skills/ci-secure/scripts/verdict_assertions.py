"""Bounded, non-executing analysis of required verdict jobs.

This is deliberately a small language, not a shell interpreter. Unsupported
expressions, scripts, actions and dependency graphs are unmeasured. No repository
command is executed. A counterexample must use a supported execution path.
"""
from __future__ import annotations

import itertools
import copy
import re
import shlex
import json

# Reviewed action.yml, src/normalize_needed_jobs_status.py and src/job_outcome.py
# at this immutable revision. No action name/tag inference and no remote code
# execution: only its input and result contract is modeled here.
ALLS_GREEN = "re-actors/alls-green@b5b5b37504aa4183270bd3d855c52a67f212be35"


class Unknown(ValueError):
    pass


# Scenario key: whether the whole workflow run was cancelled.
WORKFLOW_CANCELLED = ("", "<workflow cancelled>")


# GitHub expression function names are case-insensitive. Any job `if:` naming a
# status function replaces the implicit success(), so the job may run after an
# ancestor failed, was cancelled or was skipped.
STATUS_FUNCTION = re.compile(r"\b(success|always|cancelled|failure)\s*\(", re.I)
STEP_STATUS_FUNCTION = re.compile(r"\b(success|failure|cancelled)\s*\(", re.I)
CONTEXT = re.compile(r"\bgithub\.[A-Za-z_][\w.*-]*", re.I)


class _Choice(Exception):
    """A job condition that cannot be evaluated: both branches are explored."""
    def __init__(self, name):
        super().__init__(name)
        self.name = name


def needs(job):
    value = job.get("needs", [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return value
    raise Unknown("unresolved needs")


def unwrap(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    value = str(value).strip()
    if value.startswith("${{") and value.endswith("}}"):
        return value[3:-2].strip()
    return value


def truth(value):
    return value not in (False, None, "", 0)


class Expression:
    """GitHub's supported boolean/string subset, with complete token coverage."""
    TOKEN = re.compile(r"\s*(('[^']*(?:''[^']*)*')|(&&|\|\||==|!=|[!(),])|([A-Za-z_][\w.*-]*)|([01]))")

    def __init__(self, source, results, outputs, direct, failure_scope=None):
        self.tokens = []
        source = unwrap(source)
        if len(source) > 4096:
            raise Unknown("expression size limit")
        while source:
            m = self.TOKEN.match(source)
            if not m:
                raise Unknown("unsupported verdict expression")
            self.tokens.append(next(x for x in (m[1], m[3], m[4], m[5]) if x is not None))
            if len(self.tokens) > 512:
                raise Unknown("expression token limit")
            source = source[m.end():]
        self.i = 0
        self.results, self.outputs, self.direct = results, outputs, direct
        self.failure_scope = direct if failure_scope is None else failure_scope

    def take(self, token):
        if self.i < len(self.tokens) and self.tokens[self.i] == token:
            self.i += 1
            return True
        return False

    def evaluate(self):
        value = self.disjunction()
        if self.i != len(self.tokens):
            raise Unknown("trailing expression tokens")
        return value

    def disjunction(self):
        value = self.conjunction()
        while self.take("||"):
            right = self.conjunction()
            value = value if truth(value) else right
        return value

    def conjunction(self):
        value = self.equality()
        while self.take("&&"):
            right = self.equality()
            value = right if truth(value) else value
        return value

    def equality(self):
        value = self.atom()
        for op in ("==", "!="):
            if self.take(op):
                right = self.atom()
                if type(value) is not type(right):
                    raise Unknown("expression coercion")
                equal = value.lower() == right.lower() if isinstance(value, str) else value == right
                return equal if op == "==" else not equal
        return value

    def atom(self):
        if self.take("!"):
            return not truth(self.atom())
        if self.take("("):
            value = self.disjunction()
            if not self.take(")"):
                raise Unknown("unclosed expression")
            return value
        if self.i == len(self.tokens):
            raise Unknown("empty expression")
        token = self.tokens[self.i]
        self.i += 1
        if token.startswith("'"):
            return token[1:-1].replace("''", "'")
        if token in ("true", "false"):
            return token == "true"
        if token in ("0", "1"):
            return int(token)
        if token.lower() in ("always", "success", "failure", "cancelled"):
            if not self.take("(") or not self.take(")"):
                raise Unknown("status function arguments")
            states = [self.results[n] for n in self.direct]
            if token.lower() == "cancelled":
                # True only when the WORKFLOW was cancelled. A dependency that
                # timed out reports `cancelled` while cancelled() stays false,
                # so this is a scenario (see analyze), never a needs result.
                return self.outputs.get(WORKFLOW_CANCELLED, False)
            return {"always": True, "success": all(x == "success" for x in states),
                    "failure": any(self.results[n] == "failure" for n in self.failure_scope)}[token.lower()]
        if token.lower() == "contains":
            if not self.take("("):
                raise Unknown("contains syntax")
            array = self.disjunction()
            if not self.take(","):
                raise Unknown("contains syntax")
            item = self.disjunction()
            if not self.take(")") or not isinstance(array, list) or not isinstance(item, str):
                raise Unknown("contains arguments")
            return item.lower() in [x.lower() for x in array]
        if token == "needs.*.result":
            return [self.results[n] for n in self.direct]
        m = re.fullmatch(r"needs\.([\w-]+)\.(result|outputs\.([\w-]+))", token)
        if m and m[1] in self.direct:
            return self.results[m[1]] if m[2] == "result" else self.outputs.get((m[1], m[3]), "")
        if ("", token.lower()) in self.outputs:
            # An enumerated `github.*` context value (see analyze).
            return self.outputs[("", token.lower())]
        raise Unknown("unresolved expression context")


def expression(source, results, outputs, direct, failure_scope=None):
    return Expression(source, results, outputs, direct, failure_scope).evaluate()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


_VARIABLE = r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)\})"
_WORD = re.compile(r"""\s*("[^"]*"|'[^']*'|&&|\|\||[^\s"']+)""")
_SET_FLAGS = {"-e", "-u", "-x", "-eu", "-ue", "-ex", "-xe", "-eux", "-eo", "-euo", "-euxo", "-o",
              "pipefail"}
_KEYWORD = re.compile(r"(?:if|elif|else|then|fi|while|until|for|case|do|done)\b")


def _words(text):
    words, rest = [], text.strip()
    while rest:
        match = _WORD.match(rest)
        if not match:
            raise Unknown("unsupported shell quoting")
        words.append(match[1])
        rest = rest[match.end():].strip()
    return words


def _operand(word, env):
    """A test operand as data: a quoted literal, one quoted variable, or a
    bare literal without glob, expansion or splitting characters."""
    variable = re.fullmatch('"' + _VARIABLE + '"', word)
    if variable:
        name = variable[1] or variable[2]
        if name not in env:
            raise Unknown("unresolved shell guard variable")
        return env[name]
    literal = re.fullmatch(r'"([^"$`\\]*)"' + "|'([^']*)'", word)
    if literal:
        return literal[1] if literal[1] is not None else literal[2]
    if re.fullmatch(r"[A-Za-z0-9_.:/-]+", word):
        return word
    raise Unknown("unsupported shell guard operand")


def _primary(words, env, double, shell):
    negate = False
    if double and words[:1] == ["!"]:
        negate, words = True, words[1:]
    if len(words) != 3:
        raise Unknown("unsupported shell guard condition")
    left, op, right = words
    if op == "=~":
        if not double or right != "^(true|false)$":
            raise Unknown("unsupported shell guard condition")
        value = _operand(left, env) in ("true", "false")
    elif op in ("=", "==", "!="):
        if op == "==" and shell != "bash":
            # POSIX test has no `==`; dash (Ubuntu's sh) errors on it.
            raise Unknown("non-POSIX test operator under sh")
        value = (_operand(left, env) == _operand(right, env)) == (op != "!=")
    else:
        raise Unknown("unsupported shell guard condition")
    return value != negate


def _split(words):
    """[a, '&&', b, ...] groups; mixed `&&`/`||` precedence is refused."""
    parts, current = [], []
    for word in words:
        if word in ("&&", "||"):
            if not current:
                raise Unknown("unsupported shell guard condition")
            parts += [current, word]
            current = []
        else:
            current.append(word)
    if not current:
        raise Unknown("unsupported shell guard condition")
    parts.append(current)
    operators = {parts[i] for i in range(1, len(parts), 2)}
    if len(operators) > 1:
        raise Unknown("mixed shell guard operators")
    return parts[0::2], operators == {"||"}


def _test(words, env, shell):
    """One `[ ... ]`, `[[ ... ]]` or `test ...` command as a boolean."""
    if words[0] == "[[":
        if shell != "bash" or words[-1] != "]]" or len(words) < 3:
            raise Unknown("unsupported shell test")
        groups, either = _split(words[1:-1])
        values = [_primary(group, env, True, shell) for group in groups]
        return any(values) if either else all(values)
    if words[0] == "[":
        if words[-1] != "]":
            raise Unknown("unsupported shell test")
        return _primary(words[1:-1], env, False, shell)
    if words[0] == "test":
        return _primary(words[1:], env, False, shell)
    raise Unknown("unsupported shell guard condition")


def _condition(text, env, shell):
    """An `if` list of tests joined by one kind of `&&`/`||`. Only the
    bracket boundaries split it: `||` inside [[ ]] belongs to the test."""
    words = _words(text)
    groups, current, depth = [], [], 0
    for word in words:
        depth += word == "[["
        depth -= word == "]]"
        if word in ("&&", "||") and depth == 0:
            groups += [current, word]
            current = []
        else:
            current.append(word)
    groups.append(current)
    if any(not g for g in groups[0::2]) or len({groups[i] for i in range(1, len(groups), 2)}) > 1:
        raise Unknown("unsupported shell guard condition")
    values = [_test(g, env, shell) for g in groups[0::2]]
    return any(values) if "||" in groups[1::2] else all(values)


def _command(line, env, shell):
    """One supported simple command: True/False for its exit status, or
    ("exit", bool) for an explicit exit."""
    for match in re.finditer(_VARIABLE, line):
        if (match[1] or match[2]) not in env:
            raise Unknown("unresolved shell guard variable")
    if re.search(r"[$`\\;&|<>]", re.sub(_VARIABLE, "", line)):
        # No command substitution, redirection, pipes or lists.
        raise Unknown("unsupported shell control flow")
    words = _words(line)
    head = words[0]
    if head == "echo":
        return True
    if head == "set":
        flags = words[1:]
        if not flags or any(flag not in _SET_FLAGS for flag in flags):
            raise Unknown("unsupported shell option")
        if "pipefail" in flags and shell != "bash":
            raise Unknown("pipefail requires bash")
        return True
    if words in (["true"], [":"]):
        return True
    if words == ["false"]:
        return False
    if len(words) == 2 and head == "exit":
        variable = re.fullmatch('"?' + _VARIABLE + '"?', words[1])
        status = env[variable[1] or variable[2]] if variable else words[1]
        if status not in ("0", "1"):
            raise Unknown("unsupported exit status")
        return ("exit", status == "0")
    if head in ("test", "[", "[["):
        return _test(words, env, shell)
    raise Unknown("unsupported verdict command")


def shell_script(source, env, shell):
    """Evaluate a bounded errexit script: simple commands, explicit exits and
    flat `if CONDITION; then ... fi` blocks over quoted scalar data.

    GitHub runs bash and sh steps with errexit, so a failing command ends the
    step. Variables are compared as data and never re-parsed as script text.
    Anything else (else/elif, nesting, loops, pipes, substitutions) is Unknown.
    """
    lines = [x.strip() for x in source.splitlines()
             if x.strip() and not x.lstrip().startswith("#")]
    if len(lines) > 32:
        raise Unknown("verdict command limit")
    i = 0
    while i < len(lines):
        guard = re.fullmatch(r"if\s+(.+?)\s*;\s*then", lines[i])
        if guard:
            end = next((j for j in range(i + 1, len(lines)) if lines[j] == "fi"), None)
            if end is None:
                raise Unknown("unsupported shell control flow")
            body = lines[i + 1:end]
            if any(_KEYWORD.match(x) for x in body):
                raise Unknown("unsupported shell control flow")
            taken = _condition(guard[1], env, shell)
            i = end + 1
            if not taken:
                continue
        else:
            if _KEYWORD.match(lines[i]):
                raise Unknown("unsupported shell control flow")
            body, i = [lines[i]], i + 1
        for text in body:
            result = _command(text, env, shell)
            if isinstance(result, tuple):
                return result[1]
            if not result:
                return False
    return True


def run_step(source, env, results, outputs, direct, shell="bash"):
    """Return shell success only for fully understood, failure-propagating text."""
    if len(str(source)) > 8192 or len(str(env)) > 8192:
        raise Unknown("verdict command size limit")
    def substitute(match):
        if STEP_STATUS_FUNCTION.search(match[1]):
            raise Unknown("step status function is not a dependency result assertion")
        value = expression(match[1], results, outputs, direct)
        if isinstance(value, list):
            # An array interpolates as `Array`, not as its items.
            raise Unknown("array interpolated into a script")
        return str(value).lower() if isinstance(value, bool) else str(value)

    source = re.sub(r"\$\{\{(.*?)\}\}", substitute, str(source), flags=re.S)
    # GitHub expressions are replaced before the shell parses quotes, but shell
    # variables are not expanded inside single quotes. Do not let the scalar
    # replacement below turn a literal '$RESULT' into an effective assertion.
    quote = None
    for char in source:
        if char == "\\":
            raise Unknown("unsupported shell escaping")
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        elif char == "$" and quote == "'":
            raise Unknown("literal shell variable in single quotes")
    # Substitution is limited to scalar statuses/numeric exit codes. No shell
    # evaluation, expansion, pipes, command substitution or redirections.
    if any(key in env for key in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PATH")):
        raise Unknown("shell behavior overridden by environment")
    # The runner sets GITHUB_* and RUNNER_* itself; a workflow `env:` of the
    # same name does not reach the shell, so those names stay unresolved.
    resolved_env = {key: re.sub(r"\$\{\{(.*?)\}\}", substitute, str(value), flags=re.S)
                    for key, value in env.items() if not re.match(r"(?:GITHUB|RUNNER)_", str(key))}
    if len(source.splitlines()) > 32:
        raise Unknown("verdict command limit")
    if shell not in ("bash", "sh"):
        # verdict_success admits only a literal `exit N` or echo here.
        return source.strip() != "exit 1"
    return shell_script(source, resolved_env, shell)


def action_success(step, job, results, direct):
    if step.get("uses") != ALLS_GREEN:
        raise Unknown("an action step other than re-actors/alls-green pinned to "
                      + ALLS_GREEN.split("@")[1] + " is not read")
    if job.get("env") or step.get("env") or job.get("container") or job.get("services"):
        raise Unknown("verdict action execution environment overridden")
    if job.get("defaults", {}).get("run", {}).get("working-directory") not in (None, "."):
        raise Unknown("unresolved verdict action working directory")
    inputs = step.get("with") or {}
    if (not isinstance(inputs, dict) or set(inputs) - {"jobs", "allowed-skips", "allowed-failures"}
            or not re.fullmatch(r"\$\{\{\s*toJSON\(needs\)\s*\}\}", str(inputs.get("jobs", "")), re.I)):
        raise Unknown("unresolved verdict action jobs input")

    def allowed(name):
        raw = inputs.get(name, "[]")
        if not isinstance(raw, str) or len(raw) > 4096 or "${{" in raw:
            raise Unknown("unresolved verdict action allowed list")
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            values = [value.strip() for value in raw.split(",")]
        if (not isinstance(values, list) or len(values) > 32
                or any(not isinstance(value, str) or (value and value not in direct) for value in values)):
            raise Unknown("unresolved verdict action allowed list")
        return set(values) - {""}

    skips, failures = allowed("allowed-skips"), allowed("allowed-failures")
    return bool(direct) and all(
        results[name] == "success" or name in failures
        or (name in skips and results[name] == "skipped") for name in direct)


def verdict_success(job, results, outputs):
    direct = needs(job)
    if job.get("continue-on-error"):
        raise Unknown("job continue-on-error")
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 32:
        raise Unknown("no readable verdict steps")
    for step in steps:
        if not isinstance(step, dict):
            raise Unknown("unreadable verdict step")
        if "if" in step:
            # At step scope success() refers to preceding steps, not needs.
            cond = unwrap(step["if"])
            if STEP_STATUS_FUNCTION.search(cond):
                raise Unknown("step status expression")
            if not truth(expression(cond, results, outputs, direct)):
                continue
        if "uses" in step:
            if step.get("continue-on-error") not in (None, False, True):
                raise Unknown("step continue-on-error")
            if not action_success(step, job, results, direct) and step.get("continue-on-error") is not True:
                return False
            continue
        working_directory = step.get("working-directory", job.get("defaults", {}).get("run", {}).get("working-directory"))
        if working_directory not in (None, "."):
            raise Unknown("unresolved verdict working directory")
        runner = job.get("runs-on", "")
        # GitHub-hosted Linux and macOS images default `run:` to bash. Any
        # other label (self-hosted, larger runners, Windows) is not inferred.
        hosted_unix = isinstance(runner, str) and re.fullmatch(r"(?:ubuntu|macos)-[\w.-]+", runner)
        # A job container may lack bash, and GitHub then runs `sh`.
        default = "bash" if hosted_unix and not job.get("container") else "unknown"
        shell = step.get("shell", job.get("defaults", {}).get("run", {}).get("shell", default))
        if shell not in ("bash", "sh"):
            literal = str(step.get("run", "")).strip()
            if shell not in ("unknown", "pwsh", "powershell") or not re.fullmatch(
                    r"exit [01]|echo (?:[A-Za-z][A-Za-z0-9 ,.:-]*|\"[A-Za-z][A-Za-z0-9 ,.:-]*\"|'[A-Za-z][A-Za-z0-9 ,.:-]*')", literal):
                raise Unknown("unsupported verdict shell: the runner's default shell is not known; "
                              "set `shell: bash` on the step")
        if step.get("continue-on-error") not in (None, False, True):
            raise Unknown("step continue-on-error")
        env = dict(job.get("env") or {})
        env.update(step.get("env") or {})
        if not run_step(step.get("run", ""), env, results, outputs, direct, shell) and step.get("continue-on-error") is not True:
            return False
    return True


def analyze(jobs, key, workflow=None):
    """Return pass/fail/unmeasured and the supported path or refusal.

    A strict suite-success assertion can be proved independently of arbitrary
    suite conditions, unless an intermediate can run after an ancestor failed.
    Otherwise enumerate failed/cancelled ancestors. A failed producer's output
    is reproducible only when it is a literal or empty; any other value written
    before a failure is unproved, so it can withhold but never fail or pass.
    An intermediate whose `if:` names a status function is evaluated, and when
    it runs after a non-success dependency its own steps must be resolved.
    Successful detectors' valid/invalid output decisions are also enumerated.
    Reusable-workflow callers and matrix output producers can reproduce a
    hole but cannot certify a pass; nor can an ancestor with conditional steps,
    though its literal outputs still count as failure evidence. All limits
    withhold.
    """
    try:
        if workflow:
            jobs = copy.deepcopy(jobs)
            inherited = workflow.get("defaults", {}).get("run", {})
            for job in jobs.values():
                run_defaults = dict(inherited)
                run_defaults.update(job.get("defaults", {}).get("run", {}))
                job.setdefault("defaults", {})["run"] = run_defaults
                env = dict(workflow.get("env") or {})
                env.update(job.get("env") or {})
                job["env"] = env
        order, active, visited = [], set(), set()
        def visit(name):
            if name in active or len(active) > 24 or len(visited) > 32:
                raise Unknown("dependency cycle or traversal limit")
            if name in visited:
                return
            if name not in jobs or not isinstance(jobs[name], dict):
                raise Unknown("unresolved dependency: " + name)
            active.add(name)
            for parent in needs(jobs[name]):
                visit(parent)
            active.remove(name)
            visited.add(name)
            order.append(name)
        visit(key)
        direct = needs(jobs[key])
        if not direct:
            return "pass", "no verdict dependencies"
        ancestors = order[:-1]
        # A reusable caller's result summarizes jobs this file does not show
        # (they can all skip), so it cannot certify a pass, only reproduce a
        # skip that GitHub decides at the caller.
        uncertified = [n for n in ancestors if "uses" in jobs[n]]
        # A successful job result does not prove its conditionally executed
        # steps ran, so such an ancestor withholds a pass. It stays out of
        # `uncertified`: its literal declared outputs are still reproducible,
        # so a concrete failing path through it can establish a failure.
        conditional_steps = [n for n in ancestors if any(
            isinstance(step, dict) and "if" in step
            and unwrap(step["if"]).lower() not in ("true", "always()", "success()")
            for step in jobs[n].get("steps", []))]
        ancestor_scope = {}
        for name in order:
            ancestor_scope[name] = set(needs(jobs[name]))
            for parent in needs(jobs[name]):
                ancestor_scope[name].update(ancestor_scope[parent])
        swallowers = set()
        for name in ancestors:
            job = jobs[name]
            if job.get("continue-on-error"):
                raise Unknown("dependency tolerates failure: " + name)
            if needs(job) and STATUS_FUNCTION.search(unwrap(job.get("if", ""))):
                swallowers.add(name)
        refs = set()
        for name in order:
            text = str(jobs[name])
            refs.update(re.findall(r"needs\.([\w-]+)\.outputs\.([\w-]+)", text))
        # No accepting non-success combination implies skipped suites cannot
        # pass. Cap combinations; this proof does not rely on job if semantics.
        verdict_refs = re.findall(r"needs\.([\w-]+)\.outputs\.([\w-]+)", str(jobs[key]))
        if len(direct) <= 5 and not verdict_refs and not swallowers and not uncertified and not conditional_steps:
            strict = True
            for states in itertools.product(("success", "failure", "cancelled", "skipped"), repeat=len(direct)):
                if all(s == "success" for s in states):
                    continue
                results = dict(zip(direct, states))
                try:
                    accepts = verdict_success(jobs[key], results, {})
                except Unknown:
                    # No universal proof, but a different reachable path may
                    # still reproduce a bypass without executing unknown work.
                    strict = False
                    break
                if accepts:
                    strict = False
                    break
            if strict:
                return "pass", "executed assertion rejects every non-success dependency result"
        # Include every literal the assertion can distinguish, not just boolean
        # values: an escape clause for an arbitrary string must not be missed.
        domain = {"true", "false", "", "invalid"}
        for name in order:
            for source in strings(jobs[name]):
                domain.update(re.findall(r"'([^']*)'", source))
                domain.update(re.findall(r'"([^"\n]*)"', source))
            # Shell guards can compare against env scalars, and literal job
            # outputs are reachable even when no assertion quotes their value.
            for mapping in [jobs[name].get("outputs"), jobs[name].get("env"),
                            *(step.get("env") for step in jobs[name].get("steps", [])
                              if isinstance(step, dict))]:
                if isinstance(mapping, dict):
                    domain.update(str(value) for value in mapping.values()
                                  if isinstance(value, (str, int, bool)) and "${{" not in str(value))
        for parent, _ in refs:
            if parent not in jobs:
                raise Unknown("unresolved detector output producer")
            # Matrix legs can write different values; the last one wins, so a
            # valid-skip decision on such an output cannot certify a pass.
            if jobs[parent].get("strategy", {}).get("matrix") or "uses" in jobs[parent]:
                uncertified.append(parent)
        # Scalar `github.*` values in job conditions and the verdict are
        # enumerated over the same literal domain plus an unseen value, so
        # event-routed suites (`event_name == 'push'` / `!= 'push'`) are
        # evaluated on both branches. Any other unevaluable job condition is a
        # free choice: both branches are explored.
        context_sources = [str(jobs[name].get("if", "")) for name in ancestors] + list(strings(jobs[key]))
        contexts = sorted({m.lower().rstrip(".") for text in context_sources
                           for m in CONTEXT.findall(text) if "*" not in m})
        if len(contexts) > 3:
            contexts = []
        # A context value is only compared against literals in those same
        # conditions and the verdict, so that is its distinguishing domain.
        context_domain = {"", "unseen-context-value"}
        for text in context_sources:
            context_domain.update(re.findall(r"'([^']*)'", text))
            context_domain.update(re.findall(r'"([^"\n]*)"', text))
        context_domain = sorted(context_domain)
        if (len(refs) > 3 or len(domain) ** len(refs) * 3 ** len(ancestors)
                * len(context_domain) ** len(contexts) > 20000):
            raise Unknown("detector output enumeration limit")
        refs = sorted(refs)

        def simulate(env, seeds, choices):
            results, invalid_skip = {}, False
            for name in ancestors:
                job = jobs[name]
                parents = needs(job)
                parents_ok = all(results[n] == "success" for n in parents)
                if not parents_ok and name not in swallowers:
                    results[name] = "skipped"
                    continue
                cond = job.get("if", "true")
                try:
                    runs, resolved = truth(expression(cond, results, env, parents, ancestor_scope[name])), True
                except Unknown:
                    if name not in choices:
                        raise _Choice(name)
                    runs, resolved = choices[name], False
                if runs and not parents_ok:
                    # Running after a non-success dependency: its own
                    # steps decide whether it reports success.
                    if "uses" in job:
                        raise Unknown("reusable workflow runs after ancestor failure: " + name)
                    results[name] = "success" if verdict_success(job, results, env) else "failure"
                    continue
                results[name] = seeds[name] if runs else "skipped"
                if not runs and parents_ok and resolved:
                    # Supported intentional skips: the explicit boolean-output
                    # decision, or routing on the triggering event alone.
                    # Arbitrary predicates cannot establish valid unchanged work.
                    text = unwrap(cond)
                    match = re.fullmatch(r"needs\.([\w-]+)\.outputs\.([\w-]+)\s*==\s*'true'", text)
                    if match:
                        if env.get((match[1], match[2])) != "false":
                            invalid_skip = True
                    elif "needs." in text or STATUS_FUNCTION.search(text) or not CONTEXT.search(text):
                        raise Unknown("unresolved intentional skip condition")
            return results, invalid_skip

        def leaf(outputs, env, results, invalid_skip):
            """("cx", message) for a reproduced bypass, "maybe" or "ok"."""
            failed = next((n for n in ancestors if results[n] in ("failure", "cancelled")), None)
            if failed is None and not invalid_skip:
                return "ok", None
            if not verdict_success(jobs[key], results, env):
                return "ok", None
            visible = ", ".join(n + "=" + results[n] for n in direct)
            cause = f"`{failed}` is {results[failed]}" if failed else "an invalid detection output skips a suite"
            concrete = True
            for (parent, output), value in outputs.items():
                declared = (jobs[parent].get("outputs") or {}).get(output)
                if parent in uncertified:
                    concrete &= results[parent] != "success" and value == ""
                elif declared is None:
                    concrete &= value == ""
                elif isinstance(declared, str) and "${{" not in declared:
                    concrete &= value == declared or (results[parent] != "success" and value == "")
                else:
                    # A failed producer may terminate before it writes
                    # outputs. Successful arbitrary scripts' output domains
                    # are not execution evidence.
                    concrete &= results[parent] != "success" and value == ""
            if concrete:
                return "cx", f"{cause}, but `{key}` succeeds ({visible})"
            return "maybe", "an accepting output assignment is not proved reachable"

        unknown = None
        for values in itertools.product(sorted(domain), repeat=len(refs)):
            outputs = dict(zip(refs, values))
            for outcomes, cancelled in (
                    (o, c) for o in itertools.product(("success", "failure", "cancelled"), repeat=len(ancestors))
                    # A cancelled workflow is its own scenario; a failed or
                    # timed-out job leaves cancelled() false.
                    for c in ((False, True) if "cancelled" in o else (False,))):
                seeds = dict(zip(ancestors, outcomes))
                # A bypass must hold for every context value and every branch
                # of an unevaluable condition: failure takes precedence over
                # unrelated unknowns, but never rests on a guessed value.
                verdicts = []
                try:
                    for context_values in itertools.product(context_domain, repeat=len(contexts)):
                        env = dict(outputs)
                        env.update({("", c): v for c, v in zip(contexts, context_values)})
                        env[WORKFLOW_CANCELLED] = cancelled
                        pending = [{}]
                        while pending:
                            choices = pending.pop()
                            try:
                                results, invalid_skip = simulate(env, seeds, choices)
                            except _Choice as choice:
                                if len(choices) >= 4:
                                    raise Unknown("unresolved job condition limit")
                                pending += [{**choices, choice.name: True}, {**choices, choice.name: False}]
                                continue
                            try:
                                verdicts.append(leaf(outputs, env, results, invalid_skip))
                            except Unknown as exc:
                                verdicts.append(("maybe", str(exc)))
                except Unknown as exc:
                    unknown = str(exc)
                    continue
                if verdicts and all(kind == "cx" for kind, _ in verdicts):
                    return "fail", verdicts[0][1]
                if any(kind == "cx" for kind, _ in verdicts):
                    unknown = "a bypass depends on an unresolved job condition or event context"
                elif any(kind == "maybe" for kind, _ in verdicts):
                    unknown = next(text for kind, text in verdicts if kind == "maybe")
        if unknown:
            return "unmeasured", unknown
        if uncertified:
            return "unmeasured", "reusable-workflow or matrix result cannot certify the verdict: " + ", ".join(sorted(set(uncertified)))
        if conditional_steps:
            return "unmeasured", "a successful result does not prove conditional steps ran: " + ", ".join(conditional_steps)
        return "pass", "executed assertion rejects upstream failure, cancellation and invalid skip decisions"
    except (Unknown, TypeError, KeyError, AttributeError, RecursionError) as exc:
        return "unmeasured", str(exc)

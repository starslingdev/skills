"""The install-command matcher runs in linear time on hostile workflow lines.

`_INSTALL_CMD_RE` finds `npm ci` / `pnpm install` / `yarn` in command position
after any wrappers (`sudo -u root`, `timeout 60`, `env`) and `VAR=value`
assignments. Before this fix several pieces of it offered two ways to read the
same text, inside a repeat, so a line built from a short repeated unit made
`search()` backtrack exponentially and froze the scan:

* an assignment value: the bare class also matched quote characters, so
  `_=""` was both a quoted and a bare value;
* a wrapper option: `-{1,2}[\\w-]*` read `--a` as `-` + `-a` or `--` + `a`;
* a wrapper option's value: the token after an option could be its value or
  the next wrapper word, assignment, `export NAME=`, or numeric operand;
* the yarn option tail: a space-separated value could be the next `-option`,
  and a bare value could also be a quoted one.

Each now has exactly one parse. The tests below pin three things. First, the
adversarial lines finish well under half a second. They run in a subprocess
with a timeout, so the pre-fix pattern fails this file in seconds instead of
hanging CI. Second, the new pattern returns the same `finditer()` spans as the
pre-fix one on realistic short lines and on seeded random word lines; the old
pattern runs only on short strings, where it is fast. Third, the intentional
differences, listed in `test_intentional_differences_are_pinned`.
"""
from __future__ import annotations

import itertools
import json
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS))

from _scan_import import load_scan  # noqa: E402

scan = load_scan()

# ---------------------------------------------------------------------------
# The pattern as shipped before the fix, rebuilt from the unchanged pieces.
# ---------------------------------------------------------------------------
_OLD_PREFIX = (
    r"(?:"
    r"(?:" + "|".join(re.escape(w) for w in scan._CMD_WRAPPERS) + r")"
    r"(?:[ \t]+-{1,2}[\w-]*(?:[ \t]+[^-\s;&|][^\s;&|]*)?)*"
    r"(?:[ \t]+\d[\w.]*)?[ \t]+"
    r"|(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*="
    r"(?:\"[^\"]*\"|'[^']*'|[^\s;&|]*)[ \t]+"
    r")*"
)
_OLD_CMD = (
    r"(?<![\w./-])(?P<cmd>"
    r"npm\s+(?:ci|install|i)(?![\w-])"
    r"|pnpm\s+(?:install|i)(?![\w-])"
    r"|yarn\s+install(?![\w-])"
    r"|yarn(?!\s+-{0,2}(?:version|help|[vV]|h)(?![\w-]))"
    r"(?:\s+-[\w-]+(?:[= ](?:\"[^\"]*\"|'[^']*'|[^\s;&|]+))?)*\s*(?:$|[\n;&|])"
    r")"
)
OLD = re.compile(scan._CMD_START + _OLD_PREFIX + _OLD_CMD, re.MULTILINE)
NEW = scan._INSTALL_CMD_RE

# Every equivalence string stays short: the old pattern is fast on each of them.
_MAX_LEN = 160


def _spans(pattern: re.Pattern[str], text: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    return [(m.span(), m.span("cmd")) for m in pattern.finditer(text)]


def _same(text: str) -> None:
    assert len(text) <= _MAX_LEN, "keep equivalence lines short: the old pattern runs on them"
    assert _spans(OLD, text) == _spans(NEW, text), text


# ---------------------------------------------------------------------------
# (a) Adversarial inputs: linear time, checked in a subprocess with a timeout.
# ---------------------------------------------------------------------------
# Each shape is `head + unit * n + tail` and must NOT match, so a backtracking
# pattern has to try every parse. `n_red` is sized so the pre-fix pattern needs
# far longer than `_SUBPROCESS_TIMEOUT_S` (it roughly doubles per unit, and the
# yarn `-a -b` shape grows faster); `n_big` proves the fixed one is linear.
_ADVERSARIAL = {
    "assignment-double-quote": ("; ", '_="" ', "!"),
    "assignment-single-quote": ("; ", "_='' ", "!"),
    "assignment-quote-spans-next-assignment": ("; ", 'A=" ', "!"),
    "wrapper-double-dash-option": ("; sudo", " --a", " !"),
    "wrapper-value-is-next-wrapper": ("; ", "sudo -x ", "!"),
    "wrapper-value-is-negation": ("; ", "nice -n ! ", "x"),
    "wrapper-value-is-numeric-operand": ("; ", "timeout -k 1 ", "!"),
    "wrapper-value-is-assignment": ("; ", "sudo -u A=1 ", "!"),
    "wrapper-value-is-export": ("; ", "sudo -u export A=1 ", "!"),
    "wrapper-value-is-quoted-assignment": ("; ", 'sudo -u A="" ', "!"),
    "yarn-option-value-is-next-option": ("; yarn", " -a -b", "  !"),
    "yarn-quoted-value-space": ("; yarn", ' -a ""', "  !"),
    "yarn-quoted-value-equals": ("; yarn", ' -a=""', "  !"),
}
_N_RED = 28
_N_BIG = 3000
_TIME_LIMIT_S = 0.5
_SUBPROCESS_TIMEOUT_S = 10

_CHILD = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from _scan_import import load_scan
R = load_scan()._INSTALL_CMD_RE
head, unit, tail = json.loads(sys.argv[2])
out = {}
for n in json.loads(sys.argv[3]):
    text = head + unit * n + tail
    start = time.perf_counter()
    found = R.search(text)
    out[n] = [time.perf_counter() - start, found is not None]
print(json.dumps(out))
"""


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_adversarial_line_finishes_in_linear_time(name: str) -> None:
    shape = _ADVERSARIAL[name]
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, str(_TESTS), json.dumps(shape),
             json.dumps([_N_RED, _N_BIG])],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S, check=True)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{name}: search did not finish within {_SUBPROCESS_TIMEOUT_S}s "
                    f"(catastrophic backtracking)")
    for n, (elapsed, found) in json.loads(proc.stdout).items():
        assert not found, (name, n)
        assert elapsed < _TIME_LIMIT_S, f"{name} x {n}: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# (b) Equivalence with the old pattern on realistic lines.
# ---------------------------------------------------------------------------
_CORPUS = [
    # plain installs
    "npm ci", "npm install", "npm i", "pnpm install", "pnpm i --frozen-lockfile",
    "yarn install", "yarn", "yarn --frozen-lockfile", "yarn --immutable",
    "yarn --cwd packages/app", "yarn -s --network-timeout 100000",
    "yarn --cache-folder=\"/tmp/c\"", "yarn --registry 'https://r.example'",
    "yarn --mutex network --prefer-offline", "yarn -a -b c", "yarn -a -b",
    "yarn --flag=x --other", "yarn --flag= --other", "yarn -a \"\"",
    "yarn -a ''", "yarn -a=\"\"", "yarn -a \"x y\"", "yarn -a \"x\"y",
    "yarn -a -\"x\"", "yarn -a -", "yarn -a -c= ", "yarn -a\n-b\n", "yarn -a\n  -b x\n",
    "yarn build", "yarn version", "yarn --version", "yarn -v", "yarn help",
    "yarn -a -b; npm ci", "yarn -a | tee log", "yarn -a && npm ci",
    # wrappers
    "sudo npm ci", "sudo -E npm ci", "sudo -u root npm ci", "sudo -H -u root npm ci",
    "sudo --preserve-env npm ci", "sudo -- npm ci", "sudo - npm ci",
    "timeout 60 npm ci", "timeout -k 5 60 npm ci", "timeout -k 5 npm ci",
    "timeout --signal KILL 10m pnpm install", "retry 3 pnpm install",
    "retry -t 3 yarn install", "nice -n 10 npm ci", "ionice -c 3 npm ci",
    "env npm ci", "env CI=1 pnpm install", "env -i PATH=$PATH npm ci",
    "command npm ci", "exec npm ci", "nohup npm ci", "setsid npm ci",
    "stdbuf -oL npm ci", "xvfb-run -a npm ci", "time npm ci",
    "sudo -u env npm ci", "sudo -u sudo npm ci", "sudo -x sudo -x npm ci",
    "sudo -u A=1 npm ci", "sudo -u A=1 -E npm ci", "sudo -u A=1 5 npm ci",
    "sudo -u export A=1 npm ci", "sudo -u export npm ci", "sudo -u ! npm ci",
    "sudo -u 5 npm ci", "sudo -u 5 -E npm ci", "sudo -u 5 6 npm ci",
    "timeout -k 1 timeout -k 1 npm ci", "nice -n ! npm ci",
    "sudo -u A=\"x\" npm ci", "sudo -u A='x y' npm ci",
    # shell keywords
    "if true; then npm ci; fi", "then yarn install", "else pnpm i", "do npm ci",
    "! npm ci", "while npm ci; do :; done", "until yarn; do sleep 1; done",
    # assignments
    "CI=1 npm ci", "FOO= npm ci", "FOO=  npm ci", "FOO=\"\" npm ci", "FOO='' npm ci",
    "FOO=\"a b\" npm ci", "FOO='a b' npm ci", "FOO=a\"b\"c npm ci", "FOO=\"a\"b npm ci",
    "FOO='a'\"b\" npm ci", "FOO=\"a\" BAR='b' BAZ=c npm ci", "export FOO=1 npm ci",
    "export FOO=\"a b\" npm ci", "FOO=${{ secrets.X }} npm ci", "FOO=\"${{ secrets.X }}\" npm ci",
    "NODE_OPTIONS=--max-old-space-size=4096 npm ci", "PATH=$PATH:/x pnpm install",
    "FOO=\"a\nb\" npm ci", "FOO=\"x npm ci y\" npm ci", "FOO=don't npm ci",
    "FOO=\"a npm ci", "A=1 B=2 C=3 D=4 E=5 npm ci",
    " ".join(f"V{i}=x" for i in range(20)) + " npm ci",
    "FOO=bar", "FOO=bar echo npm ci", "echo FOO=bar npm ci",
    # combinations
    "sudo CI=1 npm ci", "CI=1 sudo -E npm ci", "env CI=1 sudo -u root timeout 60 npm ci",
    "export CI=1; sudo -E npm ci", "FOO=1 retry 3 yarn --frozen-lockfile",
    # run-key and command-position shapes
    "- run: npm ci", "run: sudo npm ci", "  - run: CI=1 yarn install",
    "(npm ci)", "{ npm ci; }", "a) npm ci ;;", "x | npm ci", "a && b || npm ci",
    "cd app && npm ci && npm test", "npm ci --ignore-scripts", "npm i -g corepack@0.31",
    "npm install @playwright/test", "set -e\nCI=1 npm ci\nnpm test\n",
    # near misses
    "echo npm ci", "./npm ci", "my-npm ci", "npm cit", "npm run ci", "pnpm installs",
    "yarnpkg install", "sudo echo npm ci", "dosomething npm ci", "", "sudo", "FOO=",
]


@pytest.mark.parametrize("text", _CORPUS, ids=range(len(_CORPUS)))
def test_new_pattern_matches_exactly_what_the_old_one_did(text: str) -> None:
    _same(text)


def test_corpus_exercises_both_outcomes() -> None:
    hits = sum(1 for t in _CORPUS if NEW.search(t))
    assert 100 <= hits < len(_CORPUS), hits


_WORDS = (
    "npm ci", "npm install", "pnpm install", "pnpm i", "yarn", "yarn install", "yarn build",
    "sudo", "env", "timeout", "retry", "nice", "time", "exec", "!", "then", "do", "if",
    "-E", "-u", "--a", "-k", "-", "--", "-a", "-b", "--cwd", "--flag=x", "-a=\"\"",
    "root", "60", "5", "10m", "x", "build",
    "A=1", "B=", "C=\"\"", "D=''", "E='a b'", "F=\"c d\"", "G=a\"b\"c", "H='x'y",
    "export", "export I=1", "J=\"x", "K=$PATH:/x", "\"\"", "''", "\"x y\"",
    ";", "&&", "||", "|", "(", ")", "{", "}", "echo", "ls",
)
_SEPARATORS = (" ", " ", " ", "  ", "\t", "\n", " \\\n  ")


def test_random_word_lines_agree() -> None:
    rng = random.Random(20261109)
    compared = 0
    for _ in range(6000):
        words = [rng.choice(_WORDS) for _ in range(rng.randint(1, 10))]
        text = words[0]
        for word in words[1:]:
            text += rng.choice(_SEPARATORS) + word
        if len(text) > _MAX_LEN:
            continue
        _same(text)
        compared += 1
    assert compared > 5000


def test_every_short_combination_agrees() -> None:
    core = ("npm ci", "yarn", "sudo", "-u", "--a", "A=1", "C=\"\"", "export", "5", ";", "x")
    for n in range(0, 4):
        for parts in itertools.product(core, repeat=n):
            _same(" ".join(parts))


# ---------------------------------------------------------------------------
# (c) Intentional differences.
# ---------------------------------------------------------------------------
def test_intentional_differences_are_pinned() -> None:
    """One shape moves, besides the bounds below.

    A wrapper option whose value is an assignment with a quote left open
    inside the token (`sudo -u A="x … y" npm ci`). The old pattern read the
    option value `A="x` first and so matched the install INSIDE the quotes;
    the option value no longer duplicates the assignment branch, so the
    assignment reads the whole quoted string, as the shell does, and the
    match lands on the real install after it. Both still report an npm
    install; only the span moves.
    """
    text = 'sudo -u A="x npm ci y" npm ci'
    assert [m.span("cmd") for m in OLD.finditer(text)] == [(13, 19)]
    assert [m.span("cmd") for m in NEW.finditer(text)] == [(23, 29)]


def test_bounds_leave_real_prefix_runs_alone() -> None:
    """64 prefixes, 16 options on one wrapper, 32 yarn options: pinned both sides."""
    for n, hit in ((64, True), (65, False)):
        text = " ".join(f"V{i}=x" for i in range(n)) + " npm ci"
        assert bool(NEW.search(text)) is hit and OLD.search(text), n
    for n, hit in ((16, True), (17, False)):
        text = "sudo" + " -a" * n + " npm ci"
        assert bool(NEW.search(text)) is hit and OLD.search(text), n
    for n, hit in ((32, True), (33, False)):
        text = "yarn" + " -a" * n
        assert bool(NEW.search(text)) is hit and OLD.search(text), n

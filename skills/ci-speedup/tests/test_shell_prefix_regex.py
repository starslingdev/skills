"""`SHELL_PREFIX_SRC` skips a run of shell prefixes in linear time.

`_OPT102_COMPILE_RE`, `_OPT89_COMPILE_RE` (scan.py) and `_EMULATED_COMPILE_RE`
(blocking_path.py) all let `KEY=value` assignments and `env` / `sudo` / `time`
stand before the build command they look for. The old fragment offered a bare
value as "any non-space, non-`;&|` character" beside a single-quoted and a
double-quoted branch. That bare class also matched quote characters, so `_=""`
had two parses and a line of repeated `_=""` backtracked exponentially (CodeQL
`py/redos`, the same shape `_STATUS_POST_RE` had before #257). The fragment now
lives once in `shell_prefix.py`: a bare run never contains a quote, a quote
always opens a string that must close, and the pieces chain in one order.

These tests pin three things. First, every detector uses the shared fragment
exactly once, so the old form can be swapped back in for comparison. Second,
the old and new detectors return the same `finditer()` spans on realistic
lines: plain commands, wrappers, one and many assignments with bare, quoted and
mixed values, values with `=` and spaces inside quotes, Dockerfile `--mount`
shapes and multi-line blocks. The old form runs only on short strings here; it
is never run on the attack inputs, because that is the hang being fixed. Third,
the new detectors finish the adversarial inputs well under half a second.

Two shapes move on purpose and are pinned so a later edit cannot widen the gap
unnoticed. A value with an unpaired quote (`FOO=don\\'t`, `FOO="a`) is no longer
a prefix: an open quote has no single parse, the shell rejects it unescaped,
and the escaped form is rare before a build command. A quoted piece with
whitespace glued to bare characters (`FOO="a b"c`) now is a prefix: it is one
word to the shell, and the old single-unit value could not read it.

Both repeats are bounded (64 prefixes, 16 quoted pieces in one value).
`test_bounds_leave_real_prefix_runs_alone` pins the limits from both sides.
"""
from __future__ import annotations

import itertools
import random
import re
import sys
import time
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_DIR / "scripts"))

import blocking_path as bp  # noqa: E402  (uniquely-named module; no cross-skill clash)
import scan  # noqa: E402  (uniquely-named module; no cross-skill clash)
from shell_prefix import SHELL_PREFIX_SRC  # noqa: E402

# The fragment as shipped before the fix.
_OLD_SRC = (
    r"(?:(?:(?:[A-Za-z_][A-Za-z0-9_]*=(?:[^\s;&|]+|'[^']*'|\"[^\"]*\"))|"
    r"(?:env|sudo|time))\s+)*"
)


def _old(pattern: re.Pattern[str]) -> re.Pattern[str]:
    """The same detector with the old prefix fragment swapped back in."""
    assert pattern.pattern.count(SHELL_PREFIX_SRC) == 1, pattern.pattern[:80]
    return re.compile(pattern.pattern.replace(SHELL_PREFIX_SRC, _OLD_SRC), pattern.flags)


_NEW = {
    "opt102": scan._OPT102_COMPILE_RE,
    "opt89": scan._OPT89_COMPILE_RE,
    "emulated": bp._EMULATED_COMPILE_RE,
}
_PAIRS = {name: (_old(new), new) for name, new in _NEW.items()}

# Every string is short: the old pattern is safe on each of them.
_MAX_CORPUS_LEN = 400

_CORPUS = [
    # --- plain commands ---
    "go build ./...",
    "make",
    "make -j4 all",
    "cargo build --release",
    "npm run build",
    "cmake --build build",
    "./configure && make && make install",
    "gcc -O2 -o app main.c",
    "echo building; make",
    "cat Makefile.in | make -f -",
    "if [ -f Makefile ]; then make; fi",
    "for d in a b; do make -C $d; done",
    # --- wrappers ---
    "env go build ./cmd/app",
    "sudo make install",
    "time cargo build --release",
    "sudo env PATH=$PATH make",
    "time sudo make -j2",
    "sudo -E make",
    # --- one assignment, bare values ---
    "CGO_ENABLED=0 go build -o app .",
    "GOOS=linux GOARCH=arm64 go build ./...",
    "RUSTFLAGS=-Ctarget-cpu=native cargo build --release",
    "CC=clang CXX=clang++ cmake --build build",
    "NODE_ENV=production npm run build",
    "MAKEFLAGS=-j$(nproc) make",
    "PATH=/usr/local/go/bin:$PATH go build",
    "FOO=${{ matrix.target }} cargo build",
    "a=1 make",
    "_=x make",
    "FOO=a\\ b make",
    # --- quoted values, with spaces and `=` inside ---
    "CFLAGS='-O2 -g' make",
    "CFLAGS=\"-O2 -g\" make -j2",
    "RUSTFLAGS='-C target-feature=+crt-static' cargo build",
    "GOFLAGS=\"-ldflags=-s -w\" go build ./...",
    "LDFLAGS='-L/opt/lib -lfoo=bar' ./configure",
    "FOO='' make",
    "FOO=\"\" make",
    "FOO='a b' BAR=\"c d\" BAZ=e make",
    "FOO=\"it's\" make",
    "FOO='say \"hi\"' make",
    "FOO=a\"b\"c make",
    "FOO='a'b make",
    "FOO=\"a\"'b' make",
    "FOO=\"${VAR}\" go build",
    "FOO=\"$(cat x)\" make",
    "FOO='a;b' make",
    "FOO=\"a|b\" make",
    # --- many prefixes ---
    "A=1 B=2 C=3 D=4 E=5 F=6 G=7 H=8 make",
    " ".join(f"V{i}=x" for i in range(30)) + " go build",
    "env A=1 sudo B='two words' time make",
    # --- Dockerfile shapes, as the instruction body and with the keyword ---
    "--mount=type=cache,target=/root/.cache/go-build go build -o /app .",
    "--mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build"
    " CGO_ENABLED=0 go build ./...",
    "RUN --mount=type=cache,target=/root/.cache/go-build go build -o /app .",
    "RUN apk add --no-cache gcc musl-dev && CGO_ENABLED=1 go build -o app",
    "RUN set -eux; GOOS=linux go build -o /out/app",
    "cargo chef cook --release --recipe-path recipe.json",
    "xx-cargo build --release",
    "npm ci && npm run build",
    "pip wheel . -w /wheels",
    "dotnet publish -c Release",
    "./gradlew build --no-daemon",
    "./mvnw -B package -DskipTests",
    # --- multi-line run blocks ---
    "set -e\nexport GOFLAGS=-mod=vendor\nGOOS=linux go build ./...\n",
    "CGO_ENABLED=0 \\\n  go build ./...",
    "FOO=\"a\nb\" make",
    "FOO=a\\\nmake",
    # --- no detection expected ---
    "echo FOO=bar",
    "export FOO=bar",
    "FOO=bar echo make",
    "makefile=1 ls",
    "cargo fmt --check",
    "go test ./...",
    "npm test",
    "docker build -t x .",
    "make_it_so",
    "remake",
    "FOO=bar",
    "FOO= make",
    "env",
    "",
]


def _spans(pattern: re.Pattern[str], text: str) -> list[tuple[int, int]]:
    return [m.span() for m in pattern.finditer(text)]


def _same(text: str) -> None:
    assert len(text) <= _MAX_CORPUS_LEN, "keep corpus lines short: the old pattern runs on them"
    for name, (old, new) in _PAIRS.items():
        assert _spans(old, text) == _spans(new, text), (name, text)


def test_every_detector_uses_the_shared_fragment_once() -> None:
    for name, new in _NEW.items():
        assert new.pattern.count(SHELL_PREFIX_SRC) == 1, name
        assert _OLD_SRC not in new.pattern, name


@pytest.mark.parametrize("text", _CORPUS, ids=range(len(_CORPUS)))
def test_new_pattern_matches_exactly_what_the_old_one_did(text: str) -> None:
    _same(text)


def test_corpus_exercises_both_outcomes() -> None:
    for name, new in _NEW.items():
        hits = sum(1 for t in _CORPUS if new.search(t))
        assert 25 <= hits < len(_CORPUS), (name, hits)


def test_realistic_prefixed_commands_are_detected() -> None:
    for text in (
        "CGO_ENABLED=0 GOOS=linux go build ./...",
        "CFLAGS='-O2 -g' make -j2",
        "GOFLAGS=\"-ldflags=-s -w\" go build ./...",
        "sudo make install",
        "time cargo build --release",
        "env A=1 sudo B='two words' time make",
        "FOO=a\"b\"c make",
    ):
        for name, new in _NEW.items():
            assert new.search(text), (name, text)
    body = "--mount=type=cache,target=/c GOFLAGS=\"-ldflags=-s -w\" go build ./..."
    assert scan._OPT102_COMPILE_RE.search(body)
    assert bp._EMULATED_COMPILE_RE.search(body)
    assert scan._OPT89_COMPILE_RE.search("CC=clang CXX=clang++ cmake -S . -B build")
    assert scan._OPT89_COMPILE_RE.search("./configure && CFLAGS='-O2' make")
    for text in ("echo FOO=bar", "FOO=bar echo make", "go test ./...", "FOO= make", "FOO=bar"):
        for name, new in _NEW.items():
            assert new.search(text) is None, (name, text)


# Whole shell words. Every quote is paired inside its word and no quoted piece is
# glued to bare characters across whitespace, so the generator stays inside the
# shapes where the old and new fragment agree; the two documented differences are
# pinned separately below.
_WORDS = (
    "make", "go build", "cargo build", "npm run build", "cmake --build .", "./configure",
    "gcc -c x.c", "ninja", "echo hi", "ls", "cargo test", "go test",
    "env", "sudo", "time",
    "A=1", "B=two", "CC=clang", "PATH=$PATH:/x", "F=${{ matrix.x }}", "G=a\"b\"c", "H='x'y",
    "C='a b'", "D=\"c d\"", "E=''", "F2=\"\"", "I='a=b c'", "J=\"k=v w\"", "K=\"it's\"",
    "--mount=type=cache,target=/c",
    ";", "&&", "||", "|", "then", "do", "RUN",
)
_SEPARATORS = (" ", "  ", " \\\n  ", "\n", "\t")


def test_random_word_lines_agree() -> None:
    rng = random.Random(20261110)
    for _ in range(3000):
        words = [rng.choice(_WORDS) for _ in range(rng.randint(1, 12))]
        text = words[0]
        for word in words[1:]:
            text += rng.choice(_SEPARATORS) + word
        _same(text)


def test_every_short_combination_agrees() -> None:
    core = ("make", "env", "A=1", "C='a b'", "D=\"c d\"", "E=''", "G=a\"b\"c", ";", "do", "ls")
    compared = 0
    for n in range(0, 4):
        for parts in itertools.product(core, repeat=n):
            _same(" ".join(parts))
            compared += 1
    assert compared == 1 + 10 + 100 + 1000


def test_unpaired_quote_no_longer_counts_as_a_prefix() -> None:
    # The old bare class took the quote as one more character; the new one needs
    # the string to close. The shell rejects the unescaped forms.
    # An odd number of escaped quotes inside a double-quoted value is the same
    # case: the string closes early and leaves one quote unpaired.
    for text in ("FOO=don\\'t make", "FOO=\"a make", "FOO=a'b make", "FOO=' make",
                 "X=\"a\\\"b\" make"):
        for name, (old, new) in _PAIRS.items():
            assert old.search(text), (name, text)
            assert new.search(text) is None, (name, text)
    # Close the quote and both agree again.
    _same("FOO=\"don't\" make")
    _same("FOO=a'b' make")


def test_quoted_piece_glued_to_bare_text_is_one_word() -> None:
    # One word to the shell. The old single-unit value stopped at the space inside
    # the quotes (bare) or at the closing quote (quoted) and never reached `make`.
    for text in ("FOO=\"a b\"c make", "FOO=x'a b' make", "FOO='a b'\"c d\" make"):
        for name, (old, new) in _PAIRS.items():
            assert old.search(text) is None, (name, text)
            assert new.search(text), (name, text)
    # Without whitespace inside the quotes the old bare class already read the word.
    _same("FOO=\"ab\"c make")
    _same("FOO=x'ab' make")


def test_bounds_leave_real_prefix_runs_alone() -> None:
    for name, new in _NEW.items():
        assert new.search(" ".join(f"V{i}=x" for i in range(64)) + " make"), name
        assert new.search(" ".join(f"V{i}=x" for i in range(65)) + " make") is None, name
        assert new.search("FOO=" + "'a'" * 16 + " make"), name
        assert new.search("FOO=" + "'a'" * 17 + " make") is None, name
        assert new.search("FOO=b" + "'a'b" * 16 + " make"), name


# The old fragment needs minutes on these inputs; the new one takes milliseconds.
# The limit stays far below what a backtracking pattern needs and well above a
# loaded CI runner's noise.
_TIME_LIMIT_S = 0.5


@pytest.mark.parametrize("fragment,count", [
    ("\"\" _=", 5000),  # CodeQL's witness: `_=` followed by many `"" _=`
    ("'' _=", 5000),
    ("\"\" ", 5000),
    ("'' ", 5000),
    ("a ", 20000),
    ("do ", 5000),  # every `do` is a start token
    ("1; _=", 5000),
    (" _=\"", 5000),
    ("'a'", 5000),
])
def test_adversarial_input_finishes_in_linear_time(fragment: str, count: int) -> None:
    # Never run the old fragment here: on these inputs it takes minutes to hours.
    text = "_=" + fragment * count
    for name, new in _NEW.items():
        start = time.perf_counter()
        found = new.search(text)
        elapsed = time.perf_counter() - start
        assert found is None, (name, fragment, count)
        assert elapsed < _TIME_LIMIT_S, f"{name} {fragment!r} x {count}: {elapsed:.2f}s"


@pytest.mark.parametrize("text", [
    "\"" * 20000,
    "'" * 20000,
    "env " * 20000,
    "A=1 " * 20000,
    "A=\"" + "x" * 20000,
    "A='" + " " * 20000,
])
def test_long_prefix_runs_without_a_command_finish_in_linear_time(text: str) -> None:
    for name, new in _NEW.items():
        start = time.perf_counter()
        found = new.search(text)
        elapsed = time.perf_counter() - start
        assert found is None, (name, text[:12])
        assert elapsed < _TIME_LIMIT_S, f"{name} {text[:12]!r}: {elapsed:.2f}s"

"""The shell prefixes a build command may carry, as one regex source string.

`KEY=value` assignments and the `env`, `sudo` and `time` wrappers can stand
before the command a detector looks for (`CGO_ENABLED=0 GOOS=linux go build`,
`sudo make install`, `time cargo build`). `scan.py` (OPT89, OPT102) and
`blocking_path.py` (the emulated image compile leaf) skip that run of prefixes
with this fragment, so it lives in one place.

A value is a bare run, a single- or double-quoted string, or a chain of those
(`a"b c"d` is one word to the shell). An earlier form offered the bare run as
"any non-space, non-`;&|` character", which also matched quote characters
beside the quoted branches, so `_=""` had two parses and a line of repeated
`_=""` backtracked exponentially (CodeQL `py/redos`). The branches are now
disjoint: a bare run never contains a quote, a quote always opens a string that
must close, and the pieces chain in one fixed order, so every character has
exactly one way to match.

Both repeats are bounded. No real command carries 64 prefixes or a value with
16 quoted pieces, and the bound caps the work each start token (`;`, `&&`,
`then`, `do`) can do on a hostile line, which keeps a whole-file search linear.

One shape moves on purpose: a value with an unpaired quote (`FOO=don\\'t`,
`FOO="a`) no longer counts as a prefix, because an open quote has no single
parse. The shell rejects an unclosed quote, and the escaped form is rare in a
build prefix. `tests/test_shell_prefix_regex.py` pins the agreement with the
old form on every realistic shape and the difference on these.
"""
from __future__ import annotations

SHELL_PREFIX_SRC = (
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*=(?=[^\s;&|])[^\s;&|'\"]*"
    r"(?:(?:'[^']*'|\"[^\"]*\")[^\s;&|'\"]*){0,16}"
    r"|env|sudo|time)\s+){0,64}"
)

"""Root-logger setup shared by every ci-speedup entry point.

The scripts log through module loggers. Without `configure_logging()`, only
WARNING and above reach stderr, as bare messages (Python's last-resort
handler); DEBUG/INFO lines are dropped. Each program's `__main__` block calls
it first. The level comes from the `STARSLING_LOG_LEVEL` environment variable,
a level name in any case: DEBUG, INFO, WARNING (or its alias WARN), ERROR,
CRITICAL (or its alias FATAL), or NOTSET, which shows everything, like DEBUG.
Unset or empty means WARNING; an unknown name also means WARNING, plus one
warning line naming the value that was ignored. A line logged while a module is
still loading, before `__main__` runs (collect_runs' warning about a bad
`CI_SPEEDUP_FETCH_CONCURRENCY`), bypasses the setting and prints bare.

Debug lines record endpoints, response sizes and pattern / workflow names, not
gh response bodies, with two exceptions: a failed gh call's DEBUG line prints up
to the first 200 characters of gh's own error text, and an unparsable workflow
file's DEBUG line prints the YAML parser's error, which can quote a snippet of
the file. Workflow file paths also appear, so review a captured log before
sharing it.
"""
from __future__ import annotations

import logging
import os
import sys

_ENV = "STARSLING_LOG_LEVEL"
_FORMAT = "%(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    raw = os.environ.get(_ENV, "")
    name = raw.strip().upper()
    level = logging.getLevelName(name) if name else logging.WARNING
    valid = isinstance(level, int)
    logging.basicConfig(level=level if valid else logging.WARNING,
                        stream=sys.stderr, format=_FORMAT)
    if not valid:
        logging.getLogger(__name__).warning(
            "%s=%r is not a log level (DEBUG, INFO, WARNING or WARN, ERROR, "
            "CRITICAL or FATAL, NOTSET); using WARNING", _ENV, raw)

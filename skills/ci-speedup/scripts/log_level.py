"""Root-logger setup shared by every ci-speedup entry point.

The scripts log through `logging.getLogger(__name__)`; nothing is printed unless
a program's `__main__` block calls `configure_logging()`. The level comes from
the `STARSLING_LOG_LEVEL` environment variable (a level name such as DEBUG or
INFO, any case). Unset or empty means WARNING; an unknown name also means
WARNING, plus one warning line naming the value that was ignored.

Debug lines record endpoints, response sizes and pattern / workflow names, never
gh response bodies. Workflow file paths do appear, so review a captured log
before sharing it.
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
            "%s=%r is not a log level (DEBUG, INFO, WARNING, ERROR, CRITICAL); "
            "using WARNING", _ENV, raw)

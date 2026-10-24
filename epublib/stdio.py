"""Give a windowed build somewhere to write.

A Windows app built without a console (and `pythonw`) starts with `sys.stdout`
and `sys.stderr` set to None. `print` quietly does nothing then, but uvicorn's
logging setup calls `sys.stderr.isatty()` and the server never starts. A frozen
macOS app launched from Finder has the streams but nobody reads them. Both are
pointed at one log file so a user with a problem has something to send.
"""
from __future__ import annotations

import sys
from pathlib import Path


def needs_log_file() -> bool:
    """True when the output would go nowhere: a stream is missing, or this is a
    frozen app that was not started from a terminal."""
    if sys.stdout is None or sys.stderr is None:
        return True
    return bool(getattr(sys, "frozen", False)) and not sys.stderr.isatty()


def redirect_to_log(path: Path) -> bool:
    """Send stdout and stderr to `path` (appended, line-buffered) when
    `needs_log_file()`. Returns whether it did. A log that can't be opened leaves
    the streams alone, apart from replacing a missing one with a null stream so
    nothing that writes to it fails."""
    if not needs_log_file():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        log = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        log = open("nul" if sys.platform == "win32" else "/dev/null", "w", encoding="utf-8")
    sys.stdout = sys.stderr = log
    return True

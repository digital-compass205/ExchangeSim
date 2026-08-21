"""Logging setup.

One place to configure it, so every entry point produces identically shaped
lines and CI log scraping stays stable.
"""

import logging
import logging.handlers
import os
import sys

LINE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 5 MB x 5 keeps a busy venue's history to 30 MB, which is a few days of a
# conformance run and small enough to attach to a ticket.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUPS = 5


def configure(level="INFO", path=None, max_bytes=DEFAULT_MAX_BYTES,
              backups=DEFAULT_BACKUPS, console=True):
    # type: (str, str, int, int, bool) -> None
    """Install handlers on the root logger.

    Logs go to stderr, and additionally to ``path`` when given. stderr is used
    rather than stdout so that CLI output stays machine-parseable when piped.

    A file log is rotated in place by the writing process: ``max_bytes`` caps
    one file and ``backups`` caps how many are kept, so an unattended daemon
    cannot fill a disk. Pass ``max_bytes=0`` for a single unbounded file.

    ``console=False`` drops the stderr handler, which is what a supervised
    daemon wants: its stderr is captured to a *separate* file, and duplicating
    every line into both would double the volume and rotate only one of them.
    """
    root = logging.getLogger()
    root.setLevel(_level_value(level))

    for handler in list(root.handlers):
        root.removeHandler(handler)
        _close(handler)

    formatter = logging.Formatter(LINE_FORMAT, TIME_FORMAT)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    if path:
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        if max_bytes and max_bytes > 0:
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=int(max_bytes), backupCount=int(backups))
        else:
            file_handler = logging.FileHandler(path)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if not root.handlers:
        # Nothing configured at all: silence the "no handlers" warning rather
        # than letting records fall through to lastResort on stderr.
        root.addHandler(logging.NullHandler())


def _close(handler):
    try:
        handler.close()
    except Exception:  # pragma: no cover -- a handler already torn down
        pass


def _level_value(level):
    if isinstance(level, int):
        return level
    resolved = getattr(logging, str(level).upper(), None)
    if not isinstance(resolved, int):
        raise ValueError("unknown log level %r" % (level,))
    return resolved

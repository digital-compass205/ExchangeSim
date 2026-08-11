"""Logging setup.

One place to configure it, so every entry point produces identically shaped
lines and CI log scraping stays stable.
"""

import logging
import os
import sys

LINE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure(level="INFO", path=None):
    # type: (str, str) -> None
    """Install handlers on the root logger.

    Logs go to stderr, and additionally to ``path`` when given. stderr is used
    rather than stdout so that CLI output stays machine-parseable when piped.
    """
    root = logging.getLogger()
    root.setLevel(_level_value(level))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LINE_FORMAT, TIME_FORMAT)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if path:
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        file_handler = logging.FileHandler(path)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def _level_value(level):
    if isinstance(level, int):
        return level
    resolved = getattr(logging, str(level).upper(), None)
    if not isinstance(resolved, int):
        raise ValueError("unknown log level %r" % (level,))
    return resolved

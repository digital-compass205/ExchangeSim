"""Test suite. Run with: venv36\\Scripts\\python.exe -m unittest discover -s tests -t .

Many tests deliberately drive error paths that log warnings. A NullHandler on
the root logger suppresses Python's last-resort stderr output so the run stays
readable; ``assertLogs`` still works, because it attaches its own handler.
"""

import logging

logging.getLogger().addHandler(logging.NullHandler())

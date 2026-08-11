"""Declarative FIX scenarios: the artifact CI runs.

A scenario is a JSON file of steps -- connect, set state, send, expect -- run
against a live simulator over real sockets. The runner exits non-zero on the
first mismatch, so a CI job needs nothing more than::

    exsimd --config config/japannext.json &
    python -m exchangesim.scenario.runner scenarios/*.json
"""

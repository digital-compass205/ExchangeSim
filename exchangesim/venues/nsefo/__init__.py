"""NSE India, Futures & Options, over the NNF Trimmed Protocol.

Transcribed from ``TP_FO_Trimmed_NNF_PROTOCOL 9.50``; see
``docs/specs/NSE_FO_TRANSCRIPTION.md`` for the field-by-field research this
package was built from, and ``docs/specs/README.md`` for what each part of the
document feeds. The wire itself is :mod:`exchangesim.nnf`, which knows nothing
about a segment; this package is the Futures & Options half of it, the way
:mod:`exchangesim.venues.nse` is the Capital Market half.

This is Phase 1: the dialect (``transactions.py``, ``dictionary.py``) and the
wire layouts (``layouts.py``), plus a small reference universe of contracts.
There is no ``venue.py``, no ``handlers.py``, no ``rules.py`` and nothing that
trades yet -- a security that is scoped for a later phase.

The single most dangerous property of this venue is that several transaction
codes are numerically identical to Capital Market's own -- ``2000``, ``2300``,
``1600`` and others -- while decoding to a *different* structure. See
``transactions.py`` for the full list.
"""

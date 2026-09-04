"""NSE India, Capital Market, over the NNF Trimmed Protocol.

Transcribed from ``TP_CM_Trimmed_NNF_PROTOCOL 6.6``; see ``docs/specs/README.md``
for what each part of the document feeds. The wire itself is
:mod:`exchangesim.nnf`, which knows nothing about a segment; this package is
the Capital Market half of it.

What is deliberately not built, and refused rather than half-simulated, is in
``rules.ASSUMPTIONS`` and reported by ``exsim venue.assumptions``.
"""

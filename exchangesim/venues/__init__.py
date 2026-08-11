"""Per-exchange modules.

Everything venue-specific lives here: FIX dialect, validation rules, reference
data and message translation. The core knows nothing about any of it, so adding
an exchange means adding a package here and registering it in
:mod:`exchangesim.venues.registry` -- with no core changes.
"""

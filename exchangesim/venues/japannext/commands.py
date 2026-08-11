"""Control-plane commands specific to the Japannext venue.

Almost everything an operator drives -- trading state, instruments, market data,
orders, injected behaviour, sessions -- is venue-agnostic and lives in
:mod:`exchangesim.venues.common_commands`. What remains here is the handful that
cannot: commands that need to read something only this venue knows.
"""

import logging

from .. import common_commands

log = logging.getLogger(__name__)


def register(registry, venue):
    """Attach every Japannext control command to ``registry``."""
    common_commands.register(registry, venue)

    @registry.add("venue.assumptions",
                  "Behaviours the specification does not define.", audit=False)
    def _assumptions(context, args):
        from . import rules
        return {"assumptions": rules.ASSUMPTIONS}

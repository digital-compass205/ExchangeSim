"""NSE control commands.

Everything venue-agnostic comes from :mod:`exchangesim.venues.common_commands`,
so what is left here is what needs this venue's own state: the assumption
register, what is deliberately unbuilt, the box connections (which are not
sessions and so are not covered by ``sessions``), and the secrets the Gateway
Router has issued.
"""

from ...control.commands import (
    CommandError,
    E_NOT_FOUND,
    arg_int,
)
from .. import common_commands
from . import rules
from . import transactions as X


def register(registry, venue):
    common_commands.register(registry, venue)

    @registry.add("venue.assumptions",
                  "Behaviours the specification does not define.", audit=False)
    def _assumptions(context, args):
        return {"assumptions": rules.ASSUMPTIONS,
                "not_implemented": rules.NOT_IMPLEMENTED}

    @registry.add("boxes",
                  "List the NNF box connections and the users on each.",
                  audit=False)
    def _boxes(context, args):
        """The connections, which ``sessions`` does not show.

        A box is a connection and a session is a signed-on user, and at this
        venue several of the second share one of the first. ``sessions`` lists
        users, because that is what an order's owner and a report's recipient
        mean; this lists the connections they arrived on.
        """
        return {"boxes": venue.manager.describe_boxes() if venue.manager else []}

    @registry.add("box.kill", "Disconnect one box, and every user on it.")
    def _box_kill(context, args):
        box_id = arg_int(args, "box_id", required=True)
        box = _require_box(venue, box_id)
        if not box.connected:
            return {"box_id": box_id, "disconnected": False, "users": 0}
        users = len(box.users)
        box.disconnect("disconnected by operator")
        return {"box_id": box_id, "disconnected": True, "users": users}

    @registry.add("gateway_router",
                  "What the Gateway Router has issued, without the secrets.",
                  audit=False)
    def _gateway_router(context, args):
        issued = [venue.issued(box.box_id) for box in venue.manager.boxes]
        return {
            "listening": (list(venue.router.address[:2])
                          if venue.router and venue.router.address else None),
            "tls": venue.router.tls if venue.router else None,
            "encryption": venue.router.methodology if venue.router else None,
            "required": venue.requires_encryption,
            "issued": [secrets.describe() for secrets in issued
                       if secrets is not None],
        }

    @registry.add("transactions",
                  "The interactive transaction codes this venue serves.",
                  audit=False)
    def _transactions(context, args):
        """What a client may send, and what it will be sent.

        Useful when a client is being brought up against the simulator: the
        answer to "is 2040 supported" should not require reading the source.
        """
        served = []
        for layout in venue.layouts.layouts:
            definition = venue.dictionary.message(layout.msg_type)
            served.append({
                "code": int(layout.msg_type),
                "name": layout.name,
                "structure_bytes": layout.size,
                "inbound": bool(definition and definition.inbound),
            })
        return {"transactions": served}


def _require_box(venue, box_id):
    for box in venue.manager.boxes:
        if box.box_id == box_id:
            return box
    raise CommandError("unknown box %d" % box_id, E_NOT_FOUND)

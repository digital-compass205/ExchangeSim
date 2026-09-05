"""F&O control commands.

Everything venue-agnostic comes from :mod:`exchangesim.venues.common_commands`.
What is left here is what needs this venue's own state: the assumption
register, the box connections (not sessions, so not covered by ``sessions``),
and the secrets the Gateway Router has issued -- exactly the same residue
:mod:`exchangesim.venues.nse.commands` has, minus the pre-open commands: this
slice runs continuous trading only (see ``rules.NOT_IMPLEMENTED``), so there
is no auction to lock or report on.
"""

from ...control.commands import CommandError, E_NOT_FOUND, arg_int
from .. import common_commands
from . import rules


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
        material = venue.tls_certificate
        certificate = material.describe() if material is not None else {}
        return {
            "listening": (list(venue.router.address[:2])
                          if venue.router and venue.router.address else None),
            "tls": venue.router.tls if venue.router else None,
            "encryption": venue.router.methodology if venue.router else None,
            "required": venue.requires_encryption,
            "ca_certificate": certificate.get("ca_certificate"),
            "certificate_expires": certificate.get("not_after"),
            "fingerprint": certificate.get("fingerprint"),
            "issued": [secrets.describe() for secrets in issued
                       if secrets is not None],
        }

    @registry.add("transactions",
                  "The interactive transaction codes this venue serves.",
                  audit=False)
    def _transactions(context, args):
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

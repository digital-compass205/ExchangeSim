"""HKEX control commands.

Everything venue-agnostic comes from :mod:`exchangesim.venues.common_commands`,
so what is left here is only what needs the HKEX dictionary or the venue's own
state: the assumption register, and the self-match prevention registry that the
real exchange keeps out of band.
"""

from ...control.commands import (
    CommandError,
    E_BAD_ARGS,
    E_CONFLICT,
    E_NOT_FOUND,
    arg_str,
)
from ...core.config import ConfigError
from ...core.prices import PriceError
from .. import common_commands
from . import rules


def register(registry, venue):
    common_commands.register(registry, venue)

    @registry.add("venue.assumptions",
                  "Behaviours the specification does not define.", audit=False)
    def _assumptions(context, args):
        return {"assumptions": rules.ASSUMPTIONS}

    @registry.add("smp",
                  "List the registered SelfMatchPreventionID instructions.",
                  audit=False)
    def _smp(context, args):
        return {"smp": [{"id": smp_id,
                         "instruction": rules.SMP_INSTRUCTION_TO_NAME[mode],
                         "mode": mode}
                        for smp_id, mode in sorted(venue.smp_instructions.items())]}

    @registry.add("smp.register",
                  "Register the instruction held against a SelfMatchPreventionID.")
    def _smp_register(context, args):
        smp_id = arg_str(args, "id", required=True)
        instruction = arg_str(args, "instruction", required=True, upper=True)
        try:
            mode = venue.register_smp(smp_id, instruction)
        except ConfigError as exc:
            raise CommandError(str(exc), E_BAD_ARGS)
        return {"id": smp_id,
                "instruction": rules.SMP_INSTRUCTION_TO_NAME[mode],
                "mode": mode}

    @registry.add("smp.clear", "Forget a registered SelfMatchPreventionID.")
    def _smp_clear(context, args):
        smp_id = arg_str(args, "id", required=True)
        existed = venue.smp_instructions.pop(smp_id, None) is not None
        return {"id": smp_id, "removed": existed}

    # Not audited: the board asks for this on every refresh, so it would fill
    # the tape with the fact that somebody is watching.
    @registry.add("auction",
                  "The running auction: its stage, limits and indicative price.",
                  audit=False)
    def _auction(context, args):
        market = _require_running_market(venue, args)
        symbol = arg_str(args, "symbol")
        if symbol is not None and symbol not in venue.instruments:
            raise CommandError("unknown security '%s'" % symbol, E_NOT_FOUND)
        state = venue.auction_state(market, symbol)
        if state is None:
            raise CommandError(
                "market '%s' is not running an auction" % market, E_CONFLICT)
        return state

    @registry.add("auction.lock",
                  "End the input period: no more cancellations, prices pinned.")
    def _auction_lock(context, args):
        market = _require_running_market(venue, args)
        try:
            session = venue.lock_auction(market)
        except KeyError:
            raise CommandError(
                "market '%s' is not running an auction" % market, E_CONFLICT)
        return {"market": market, "auction": session.kind,
                "stage": session.stage,
                "pinned": len(session.stage2)}

    @registry.add("auction.reference",
                  "Override the reference price the auction is anchored on.")
    def _auction_reference(context, args):
        market = _require_running_market(venue, args)
        session = venue.auctions.get(market)
        if session is None:
            raise CommandError(
                "market '%s' is not running an auction" % market, E_CONFLICT)
        symbol = arg_str(args, "symbol", required=True)
        if symbol not in venue.instruments:
            raise CommandError("unknown security '%s'" % symbol, E_NOT_FOUND)
        try:
            price = venue.codec.parse(arg_str(args, "price", required=True))
        except PriceError as exc:
            raise CommandError(str(exc), E_BAD_ARGS)
        session.set_reference(symbol, price)
        return session.describe(symbol)

    @registry.add("segments", "Which market segment each security trades on.",
                  audit=False)
    def _segments(context, args):
        segment = arg_str(args, "segment", upper=True)
        rows = [{"symbol": symbol, "segment": value}
                for symbol, value in sorted(venue.segments.items())
                if segment is None or value == segment]
        return {"segments": rows}


def _require_running_market(venue, args):
    """Resolve the 'market' argument, defaulting when only one segment runs."""
    name = arg_str(args, "market", upper=True)
    if name is None:
        if len(venue.markets) == 1:
            return list(venue.markets)[0]
        raise CommandError(
            "argument 'market' is required; this venue runs %s"
            % ", ".join(sorted(venue.markets)))
    if name not in venue.markets:
        raise CommandError("unknown market segment '%s'" % name, E_NOT_FOUND)
    return name

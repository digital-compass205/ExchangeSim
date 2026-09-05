"""Control-plane commands every venue gets for free.

These are what the ``exsim`` CLI drives and what a CI job scripts. Anything that
changes venue state goes through here, so there is one audited path from an
operator's intent to the engine.

Nothing in this module knows a wire dialect. A venue calls :func:`register` and
then adds only what is genuinely its own -- for Japannext that is a single
command, ``venue.assumptions``. The division is worth keeping: a command that
needs ``from .dictionary import ...`` belongs in the venue, everything else
belongs here.
"""

import itertools
import logging
import re
from datetime import datetime

from ..audit import KIND_CONTROL, KIND_FIX
from ..control.commands import (
    CommandError,
    E_BAD_ARGS,
    E_CONFLICT,
    E_NOT_FOUND,
    arg_bool,
    arg_choice,
    arg_int,
    arg_str,
)
from ..core.behaviour import ACTIONS
from ..core.commands import NewOrderRequest
from ..core.config import ConfigError
from ..core.instrument import symbol_key
from ..core.enums import (
    CancelReason,
    RejectReason,
    Side,
    StpMode,
    TimeInForce,
    TradingState,
)
# Generic FIX, not a dialect: naming a tag and labelling its value is the same
# work at every venue, and the venue's own dictionary supplies the answers.
from ..fix import render
from ..fix.message import MalformedMessage, decode

log = logging.getLogger(__name__)

#: Trading phases in the order a session runs through them. The core's full
#: vocabulary, kept as the name this module has always exported; what a
#: *command* offers and accepts is ``venue.trading_states``, which is a subset
#: at every venue that has said so.
TRADING_STATE_ORDER = TradingState.ORDER

#: Reject reasons an operator may force via ``behaviour.set``.
_REJECT_REASONS = frozenset(
    name for name in vars(RejectReason) if not name.startswith("_"))

_TIME_IN_FORCE = frozenset((TimeInForce.DAY, TimeInForce.IOC, TimeInForce.FOK))

#: Orders entered through the control plane are tagged with this instead of a
#: FIX session key. A session key is a ``(begin_string, sender, target)`` tuple,
#: so a string can never collide with one -- and the FIX application will not
#: resolve it to a session, which is exactly right: there is no client to report
#: to, and the events go out on a control topic instead.
INJECTED_OWNER_PREFIX = "control:"

DEFAULT_OWNER = "WEB"

#: Owners name a book of injected orders, so keep them short and boring.
_OWNER_PATTERN = re.compile(r"^[A-Z0-9_-]{1,16}$")


def register(registry, venue):
    """Attach the venue-agnostic control commands to ``registry``."""

    #: ClOrdIDs for injected orders that did not bring their own.
    injected_ids = itertools.count(1)

    # -- venue ---------------------------------------------------------

    @registry.add("venue.info",
                  "Venue summary: markets, instruments, sessions.",
                  audit=False)
    def _venue_info(context, args):
        return venue.describe()

    # -- markets and trading state -------------------------------------

    @registry.add("markets", "List markets with their trading state.", audit=False)
    def _markets(context, args):
        # The phases are listed alongside so a client offering to change one
        # need not carry its own copy of the vocabulary. They are in the order a
        # session runs, not alphabetical, because that is how a menu should read.
        # And they are the *venue's* phases, not the core's: offering a board
        # NSE's non-existent lunch break would be offering a button that can
        # only produce an error.
        return {"markets": [market.describe()
                            for market in venue.markets.values()],
                "states": list(venue.trading_states)}

    @registry.add("state.set",
                  "Set the trading state venue-wide, per market, or per symbol.")
    def _state_set(context, args):
        # The venue's phases, not the core's. A venue that never enters a phase
        # cannot represent it on the wire either -- Japannext would report a
        # closing auction as HALTED, NSE a lunch break as CLOSED -- so accepting
        # one would put the book in a state no client could be told about.
        state = arg_choice(args, "state", venue.trading_states, required=True)
        market = _optional_market(venue, args)
        symbol = _optional_symbol(venue, args)

        events = venue.set_trading_state(state, market, symbol)

        return {
            "state": state,
            "market": market or "(all)",
            "symbol": symbol,
            "transitions": [event.describe() for event in events
                            if type(event).__name__ == "TradingStateChanged"],
            "orders_cancelled": len([event for event in events
                                     if type(event).__name__ == "OrderCancelled"]),
        }

    @registry.add("state.clear",
                  "Remove a per-symbol override so it follows its market again.")
    def _state_clear(context, args):
        market = _require_market(venue, args)
        symbol = _require_symbol(venue, args)

        events = venue.markets[market].clear_instrument_state(symbol)
        for event in events:
            venue.publish("state:%s" % market, event.describe())

        return {"market": market, "symbol": symbol,
                "state": venue.markets[market].state.state_for(symbol),
                "changed": bool(events)}

    @registry.add("state.get", "Report the effective state for a symbol.",
                  audit=False)
    def _state_get(context, args):
        market = _require_market(venue, args)
        symbol = _optional_symbol(venue, args)
        machine = venue.markets[market].state
        return {
            "market": market,
            "symbol": symbol,
            "state": machine.state_for(symbol) if symbol else machine.market_state,
            "overrides": machine.overrides(),
        }

    @registry.add("stp.set", "Change a market's self-trade prevention mode.")
    def _stp_set(context, args):
        market = _require_market(venue, args)
        mode = arg_choice(args, "mode", StpMode.ALL, required=True)
        venue.markets[market].stp_mode = mode
        return {"market": market, "stp_mode": mode}

    # -- instruments ---------------------------------------------------

    @registry.add("instruments", "List the instrument universe.", audit=False)
    def _instruments(context, args):
        symbol = arg_str(args, "symbol")
        instruments = venue.instruments
        if symbol:
            instrument = instruments.get(symbol)
            if instrument is None:
                raise CommandError("unknown symbol '%s'" % symbol, E_NOT_FOUND)
            return {"instruments": [instrument.describe(venue.codec)]}
        return {"instruments": [
            instruments[symbol].describe(venue.codec)
            for symbol in sorted(instruments, key=symbol_key)]}

    @registry.add("instrument.set",
                  "Override an instrument's base price, band or tradability.")
    def _instrument_set(context, args):
        symbol = _require_symbol(venue, args)
        instrument = venue.instruments[symbol]

        base_price = arg_str(args, "base_price")
        if base_price is not None:
            instrument.base_price = _parse_price(venue, base_price, "base_price")

        low = arg_str(args, "band_low")
        high = arg_str(args, "band_high")
        if low is not None or high is not None:
            if low is None or high is None:
                raise CommandError("band_low and band_high must be given together")
            instrument.band_override = (_parse_price(venue, low, "band_low"),
                                        _parse_price(venue, high, "band_high"))

        if arg_bool(args, "clear_band_override", default=False):
            instrument.band_override = None

        tradable = arg_bool(args, "tradable")
        if tradable is not None:
            instrument.tradable = tradable

        return instrument.describe(venue.codec)

    @registry.add("instrument.add",
                  "Add an instrument to the universe without a restart.")
    def _instrument_add(context, args):
        symbol = arg_str(args, "symbol", required=True).strip()
        if not symbol:
            raise CommandError("argument 'symbol' must not be empty")
        if symbol in venue.instruments:
            raise CommandError("symbol '%s' already exists" % symbol, E_CONFLICT)

        base_price = arg_str(args, "base_price")
        instrument = _unsupported_to_conflict(
            venue.create_instrument,
            symbol=symbol,
            name=arg_str(args, "name") or "",
            lot_size=arg_int(args, "lot_size", default=100, minimum=1),
            base_price=(_parse_price(venue, base_price, "base_price")
                        if base_price is not None else None),
            tier=arg_str(args, "tier"),
            shares_outstanding=arg_int(args, "shares_outstanding",
                                       default=0, minimum=0),
            tradable=arg_bool(args, "tradable", default=True))

        _admit(venue, instrument)
        log.info("instrument '%s' added to venue '%s'", symbol, venue.name)
        return instrument.describe(venue.codec)

    @registry.add("instrument.remove",
                  "Remove an instrument; refused while it has live orders.")
    def _instrument_remove(context, args):
        symbol = _require_symbol(venue, args)

        resting = venue.engine.orders(symbol=symbol, live_only=True)
        if resting:
            raise CommandError(
                "symbol '%s' has %d live order(s); cancel them first"
                % (symbol, len(resting)), E_CONFLICT)

        del venue.instruments[symbol]
        for market in venue.markets.values():
            market.remove_book(symbol)
        log.info("instrument '%s' removed from venue '%s'", symbol, venue.name)
        return {"symbol": symbol, "removed": True}

    @registry.add("reference.reload",
                  "Re-read the reference-data CSVs from disk.")
    def _reference_reload(context, args):
        try:
            return _unsupported_to_conflict(venue.reload_reference_data)
        except ConfigError as exc:
            # A typo in a CSV is an operator error, not a simulator bug, and
            # the venue is unchanged -- report it as such rather than as E_INTERNAL.
            raise CommandError(str(exc))

    # -- market data ---------------------------------------------------

    @registry.add("book", "Aggregated depth for a symbol.", audit=False)
    def _book(context, args):
        market = _require_market(venue, args)
        symbol = _require_symbol(venue, args)
        depth = arg_int(args, "depth", default=5, minimum=1, maximum=50)

        view = venue.markets[market].data.depth(symbol, depth)
        if view is None:
            return {"symbol": symbol, "market": market, "bids": [], "asks": []}
        return view

    @registry.add("ladder",
                  "Tick-aligned board (ita) around the touch, with OVER/UNDER.",
                  audit=False)
    def _ladder(context, args):
        market = _require_market(venue, args)
        symbol = _require_symbol(venue, args)
        rows = arg_int(args, "rows", default=10, minimum=1, maximum=100)

        centre = arg_str(args, "center")
        view = venue.markets[market].data.ladder(
            symbol,
            instrument=venue.instruments[symbol],
            rows=rows,
            center=_parse_price(venue, centre, "center") if centre else None)
        if view is None:
            raise CommandError("no book for symbol '%s'" % symbol, E_NOT_FOUND)
        return view

    @registry.add("bbo", "Best bid and offer for a symbol.", audit=False)
    def _bbo(context, args):
        market = _require_market(venue, args)
        symbol = _require_symbol(venue, args)
        view = venue.markets[market].data.bbo(symbol)
        if view is None:
            return {"symbol": symbol, "market": market, "bid": None, "ask": None,
                    "bid_qty": 0, "ask_qty": 0, "spread": None}
        return view

    @registry.add("trades", "Recent trades for a symbol.", audit=False)
    def _trades(context, args):
        market = _require_market(venue, args)
        symbol = _require_symbol(venue, args)
        limit = arg_int(args, "limit", default=20, minimum=1, maximum=500)
        return {"trades": venue.markets[market].data.trades(symbol, limit) or []}

    @registry.add("stats",
                  "Session statistics for one symbol or all of them.",
                  audit=False)
    def _stats(context, args):
        market = _require_market(venue, args)
        symbol = _optional_symbol(venue, args)
        data = venue.markets[market].data

        if symbol:
            statistics = data.statistics(symbol)
            if statistics is None:
                raise CommandError("no data for symbol '%s'" % symbol, E_NOT_FOUND)
            return statistics.describe(venue.codec)
        return {"stats": data.summary()}

    @registry.add("stats.reset", "Clear statistics and the trade tape.")
    def _stats_reset(context, args):
        market = _require_market(venue, args)
        symbol = _optional_symbol(venue, args)
        venue.markets[market].data.reset_statistics(symbol)
        return {"market": market, "symbol": symbol, "reset": True}

    # -- orders --------------------------------------------------------

    @registry.add("orders", "List orders, optionally filtered.", audit=False)
    def _orders(context, args):
        live_only = arg_bool(args, "live", default=True)

        # 'session' selects a FIX client by TargetCompID; 'owner' selects a book
        # of injected orders. They address different populations, so asking for
        # both at once is a mistake worth reporting rather than silently
        # resolving in favour of one.
        session = arg_str(args, "session")
        owner = arg_str(args, "owner")
        if session and owner:
            raise CommandError("give 'session' or 'owner', not both")
        if owner:
            session_key = INJECTED_OWNER_PREFIX + _owner(args)
        else:
            session_key = _session_key(venue, session)

        orders = venue.engine.orders(
            session_key=session_key,
            symbol=arg_str(args, "symbol"),
            market=arg_str(args, "market", upper=True),
            live_only=live_only)
        return {"orders": [order.describe(venue.codec) for order in orders]}

    @registry.add("order.new",
                  "Enter an order directly, without a FIX client.")
    def _order_new(context, args):
        market = _require_market(venue, args)
        symbol = _require_symbol(venue, args)
        owner = _owner(args)

        request = NewOrderRequest(
            session_key=INJECTED_OWNER_PREFIX + owner,
            market=market,
            cl_ord_id=(arg_str(args, "cl_ord_id")
                       or "%s-%06d" % (owner, next(injected_ids))),
            symbol=symbol,
            side=arg_choice(args, "side", Side.ALL, required=True),
            quantity=arg_int(args, "quantity", required=True, minimum=1),
            price=_parse_price(venue, arg_str(args, "price", required=True),
                               "price"),
            time_in_force=arg_choice(args, "tif", _TIME_IN_FORCE,
                                     default=TimeInForce.DAY),
            min_qty=arg_int(args, "min_qty", default=0, minimum=0),
            mpid=arg_str(args, "mpid"),
            received_at=venue.clock.now() if venue.clock else None)

        events = venue.engine.new_order(request)

        # A fill against a resting FIX order still owes that client its report;
        # _emit routes by the order's own session and skips events whose owner
        # is this synthetic one, which has no FIX session to write to.
        if venue.application is not None:
            venue.application._emit(None, events)

        # Route by owner for the same reason the FIX side does: a trade produces
        # an event per side, and the counterparty's belongs to the counterparty.
        # Reading events[0] blindly would report *their* order back to us.
        mine = [event for event in events
                if getattr(getattr(event, "order", None), "session_key", None)
                == request.session_key]

        topic = "order:%s:%s" % (market, owner)
        described = [_describe_event(event, venue.codec) for event in mine]
        for payload in described:
            venue.publish(topic, payload)

        order = next((event.order for event in mine), None)
        return {"owner": owner,
                "order": order.describe(venue.codec) if order else None,
                "events": described}

    @registry.add("order.get", "Fetch one order by venue OrderID.", audit=False)
    def _order_get(context, args):
        order_id = arg_str(args, "order_id", required=True)
        order = venue.engine.registry.by_order_id(order_id)
        if order is None:
            raise CommandError("unknown order '%s'" % order_id, E_NOT_FOUND)
        return order.describe(venue.codec)

    @registry.add("order.cancel",
                  "Administratively cancel an order; the owner is notified.")
    def _order_cancel(context, args):
        order_id = arg_str(args, "order_id", required=True)
        order = venue.engine.registry.by_order_id(order_id)
        if order is None:
            raise CommandError("unknown order '%s'" % order_id, E_NOT_FOUND)
        if not order.is_live:
            raise CommandError("order '%s' is %s" % (order_id, order.status))

        market = venue.markets.get(order.market)
        events = market.cancel(order, CancelReason.ADMINISTRATIVE,
                               arg_str(args, "text") or "cancelled by operator")
        venue.application._emit(None, events)
        return {"order_id": order_id, "cancelled": True}

    @registry.add("orders.cancel_all",
                  "Cancel every live order, or those of one session or symbol.")
    def _orders_cancel_all(context, args):
        session = arg_str(args, "session")
        symbol = arg_str(args, "symbol")
        text = arg_str(args, "text") or "cancelled by operator"

        if session:
            events = venue.engine.cancel_session_orders(
                _session_key(venue, session), CancelReason.ADMINISTRATIVE, text)
        else:
            events = []
            for market in venue.markets.values():
                events.extend(market.cancel_all(
                    lambda order: symbol is None or order.symbol == symbol,
                    CancelReason.ADMINISTRATIVE, text))

        venue.application._emit(None, events)
        return {"cancelled": len(events)}

    # -- injected behaviour --------------------------------------------

    @registry.add("behaviour.set",
                  "Force rejects, delays or dropped reports, for negative testing.")
    def _behaviour_set(context, args):
        action = arg_choice(args, "action", ACTIONS, required=True, upper=False)
        count = arg_int(args, "count", default=1, minimum=0)
        delay_ms = arg_int(args, "delay_ms", default=0, minimum=0, maximum=600000)

        if action == "delay" and not delay_ms:
            raise CommandError("a 'delay' rule needs a non-zero 'delay_ms'")

        reason = arg_str(args, "reason", upper=True)
        if reason is not None and reason not in _REJECT_REASONS:
            raise CommandError("unknown reason '%s'; expected one of: %s"
                               % (reason, ", ".join(sorted(_REJECT_REASONS))))

        session = arg_str(args, "session")
        rule = venue.engine.behaviour.add(
            action,
            count=count,
            session_key=_session_key(venue, session) if session else None,
            symbol=_optional_symbol(venue, args),
            market=_optional_market(venue, args),
            reason=reason,
            text=arg_str(args, "text") or "",
            delay_ms=delay_ms)
        return rule.describe()

    @registry.add("behaviour.list",
                  "Show the active injected behaviour rules.", audit=False)
    def _behaviour_list(context, args):
        return {"rules": venue.engine.behaviour.describe()}

    @registry.add("behaviour.clear", "Remove one rule, or all of them.")
    def _behaviour_clear(context, args):
        rule_id = arg_int(args, "id")
        if rule_id is not None:
            if not venue.engine.behaviour.remove(rule_id):
                raise CommandError("unknown rule id %d" % rule_id, E_NOT_FOUND)
            return {"removed": 1}
        return {"removed": venue.engine.behaviour.clear()}

    # -- sessions ------------------------------------------------------

    @registry.add("sessions",
                  "List configured FIX sessions and their state.",
                  audit=False)
    def _sessions(context, args):
        return {"sessions": venue.manager.describe() if venue.manager else []}

    @registry.add("session.reset",
                  "Reset a session's sequence numbers and stored messages.")
    # Refused rather than faked at a venue whose protocol has no sequence
    # numbers to reset -- see NnfSession.reset.
    def _session_reset(context, args):
        session = _require_session(venue, args)
        if session.connected:
            raise CommandError(
                "session '%s' is connected; disconnect it first"
                % session.target_comp_id)
        try:
            session.reset()
        except NotImplementedError as exc:
            raise CommandError(str(exc), E_CONFLICT)
        return session.describe()

    @registry.add("session.kill", "Disconnect a session's active connection.")
    def _session_kill(context, args):
        session = _require_session(venue, args)
        if not session.connected:
            return {"target_comp_id": session.target_comp_id, "disconnected": False}
        session.disconnect("disconnected by operator")
        return {"target_comp_id": session.target_comp_id, "disconnected": True}

    # -- audit ---------------------------------------------------------
    #
    # None of these is itself audited: a tail polling the tape would otherwise
    # feed it. They read the venue's recorder and name its fields using the
    # venue's own dictionary, which is generic FIX rather than a dialect -- so
    # they stay here rather than being repeated per venue.

    @registry.add("audit",
                  "Messages and commands, newest last, filtered.", audit=False)
    def _audit(context, args):
        audit = _require_audit(venue)
        page = audit.entries(
            after=arg_int(args, "after", minimum=0),
            before=arg_int(args, "before", minimum=1),
            limit=arg_int(args, "limit", default=100, minimum=1, maximum=500),
            symbol=_optional_symbol(venue, args),
            include_session=arg_bool(args, "include_session", default=True),
            direction=arg_choice(args, "direction", ("in", "out"), upper=False),
            kind=arg_choice(args, "kind", (KIND_FIX, KIND_CONTROL), upper=False),
            types=_audit_types(args, "types"),
            exclude_types=_audit_types(args, "exclude_types"),
            since=_audit_time(args, "since", venue.clock),
            until=_audit_time(args, "until", venue.clock))

        entries = []
        for entry in page.entries:
            row = entry.describe()
            row["summary"] = _audit_summary(
                entry, venue.dictionary_for(entry.protocol))
            entries.append(row)

        return {"entries": entries,
                "last_seq": page.last_seq,
                "oldest": page.oldest,
                "truncated": page.truncated,
                "capacity": page.capacity}

    @registry.add("audit.entry",
                  "One recorded message or command, field by field.",
                  audit=False)
    def _audit_entry(context, args):
        audit = _require_audit(venue)
        seq = arg_int(args, "seq", required=True, minimum=1)
        entry = audit.entry(seq)
        if entry is None:
            raise CommandError(
                "entry %d is not in the audit; it has been overwritten" % seq,
                E_NOT_FOUND)

        dictionary = venue.dictionary_for(entry.protocol)
        row = entry.describe()
        row["summary"] = _audit_summary(entry, dictionary)

        result = {"entry": row, "fields": [], "wire": None, "detail": None}
        if entry.kind == KIND_CONTROL:
            result["detail"] = entry.detail
            return result

        if entry.raw is not None:
            # Read back with the codec that recorded it: a venue may serve one
            # protocol in more than one encoding, and bytes do not say which
            # they are. Rendered from the raw bytes rather than echoed, so a
            # redacted field cannot survive in the wire string -- and decoded
            # here, not at record time, because only an opened entry needs it.
            codec = venue.wire_codec(entry.protocol)
            if codec is None:
                return result
            result["wire"] = codec.raw_string(entry.raw, dictionary)
            try:
                message = codec.decode(entry.raw, validate_checksum=False)
            except MalformedMessage:
                # Exactly the entry an audit exists for: keep the bytes, say so.
                return result
            result["fields"] = render.describe_fields(message, dictionary)
        return result

    @registry.add("audit.types",
                  "The message types and commands the audit can filter on.",
                  audit=False)
    def _audit_types_command(context, args):
        audit = _require_audit(venue)
        dictionary = getattr(venue, "dictionary", None)
        # Taken from the dialect, so the filter menu cannot drift from what the
        # venue can actually send or receive.
        msg_types = []
        if dictionary is not None:
            for msg_type in sorted(dictionary.messages):
                definition = dictionary.messages[msg_type]
                msg_types.append({"type": msg_type,
                                  "name": definition.name,
                                  "inbound": definition.inbound})
        commands = sorted(command.name for command in registry.commands()
                          if command.audit)
        return {"msg_types": msg_types,
                "commands": commands,
                "seen": audit.types(),
                "audit": audit.describe()}

    def _require_audit(venue_):
        if venue_.audit is None:
            raise CommandError(
                "the audit is switched off (audit.capacity is 0)", E_CONFLICT)
        return venue_.audit


# -- audit helpers -----------------------------------------------------------

#: Accepted by ``since``/``until``. A bare time is the common case -- "since
#: 09:30" while watching an opening -- and is read as today on the venue's clock.
_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%H:%M:%S", "%H:%M")


def _audit_time(args, key, clock):
    text = arg_str(args, key)
    if not text:
        return None
    if "." in text:
        # Tolerate the fractional seconds the audit itself hands out, so a value
        # copied from an entry can be pasted straight back into a filter.
        text = text.split(".", 1)[0]
    for pattern in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern.startswith("%H"):
            today = clock.now().date()
            return parsed.replace(year=today.year, month=today.month,
                                  day=today.day)
        return parsed
    raise CommandError(
        "'%s' must be a time (HH:MM:SS) or a timestamp "
        "(YYYY-MM-DDTHH:MM:SS)" % key)


def _audit_types(args, key):
    """A type filter: MsgTypes for FIX entries, names for commands.

    Serves both ``types`` (show only these) and ``exclude_types`` (hide these,
    which is how a tail drops heartbeats).
    """
    values = args.get(key)
    if values is None:
        return None
    if isinstance(values, str):
        values = [part for part in values.split(",") if part]
    if not isinstance(values, list):
        raise CommandError("'%s' must be a list of message types" % key)
    for value in values:
        if not isinstance(value, str) or not value:
            raise CommandError(
                "each entry of '%s' must be a non-empty string" % key)
    return frozenset(values) if values else None


def _audit_summary(entry, dictionary):
    """The one line that identifies an entry in a list.

    Built here rather than at record time: a message that nobody looks at never
    costs the string work.
    """
    if entry.kind == KIND_CONTROL:
        detail = entry.detail or {}
        args = detail.get("args") or {}
        parts = ["%s=%s" % (key, args[key]) for key in sorted(args)]
        if entry.error:
            parts.append("-> %s" % entry.error)
        return " ".join(parts)
    if entry.detail:
        summary = render.summarise(entry.detail, dictionary)
    else:
        summary = ""
    if entry.error:
        summary = ("%s %s" % (summary, entry.error)).strip()
    return summary


# -- universe helpers --------------------------------------------------------

def _admit(venue, instrument):
    """Put a new instrument into the universe and give it its books.

    Books are created up front for the same reason ``_build_markets`` does it at
    startup: market-data summaries should list the whole universe, not only the
    names that happen to have traded. Which markets carry it is the venue's
    call -- see :meth:`Venue.books_for` -- because a security may belong to one
    segment rather than to all of them.
    """
    venue.instruments[instrument.symbol] = instrument
    for market in venue.books_for(instrument):
        market.book(instrument.symbol)


def _owner(args):
    owner = (arg_str(args, "owner", default=DEFAULT_OWNER) or "").upper()
    if not _OWNER_PATTERN.match(owner):
        raise CommandError(
            "argument 'owner' must be 1-16 characters of A-Z, 0-9, '_' or '-'")
    return owner


def _describe_event(event, codec):
    """An event as JSON, with prices formatted rather than left as raw units.

    ``Event.describe`` is written for the journal, where integer price units are
    the honest representation. Anything crossing the control plane goes through
    the codec first, like every other price the simulator publishes.
    """
    described = event.describe()
    for field in ("price", "avg_px"):
        value = described.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            described[field] = codec.format(value)
    return described


def _unsupported_to_conflict(function, **kwargs):
    """Turn a venue's refusal into a reportable command failure.

    ``NotImplementedError`` is the venue saying it does not offer the operation
    at all; ``ValueError`` is it rejecting these particular arguments -- a
    market segment that does not exist, say. Neither is a simulator fault, so
    neither should surface as an internal error.
    """
    try:
        return function(**kwargs)
    except NotImplementedError as exc:
        raise CommandError(str(exc), E_CONFLICT)
    except ValueError as exc:
        raise CommandError(str(exc), E_BAD_ARGS)


# -- argument resolution -----------------------------------------------------

def _require_market(venue, args):
    name = arg_str(args, "market", upper=True)
    if name is None:
        if len(venue.markets) == 1:
            return list(venue.markets)[0]
        raise CommandError("argument 'market' is required; known markets: %s"
                           % ", ".join(sorted(venue.markets)))
    if name not in venue.markets:
        raise CommandError("unknown market '%s'; known markets: %s"
                           % (name, ", ".join(sorted(venue.markets))), E_NOT_FOUND)
    return name


def _optional_market(venue, args):
    name = arg_str(args, "market", upper=True)
    if name is None:
        return None
    if name not in venue.markets:
        raise CommandError("unknown market '%s'; known markets: %s"
                           % (name, ", ".join(sorted(venue.markets))), E_NOT_FOUND)
    return name


def _require_symbol(venue, args):
    return _resolve_symbol(venue, arg_str(args, "symbol", required=True))


def _optional_symbol(venue, args):
    symbol = arg_str(args, "symbol")
    return None if symbol is None else _resolve_symbol(venue, symbol)


def _resolve_symbol(venue, symbol):
    """The venue's own spelling, or a refusal naming what was asked for.

    Through the venue rather than straight at ``venue.instruments`` so the
    control plane accepts exactly what the wire does: an HKEX code is a number
    and 00001 is 1, whichever door it came in by. The answer is always the
    venue's spelling, so a command cannot report back a code the venue would
    not itself write.
    """
    resolved = venue.resolve_symbol(symbol)
    if resolved is None:
        raise CommandError("unknown symbol '%s'" % symbol, E_NOT_FOUND)
    return resolved


def _require_session(venue, args):
    target = arg_str(args, "target_comp_id", required=True)
    for session in venue.manager.sessions:
        if session.target_comp_id == target:
            return session
    raise CommandError("unknown session '%s'" % target, E_NOT_FOUND)


def _session_key(venue, target_comp_id):
    """Turn a TargetCompID into the session key orders are tagged with."""
    if not target_comp_id:
        return None
    for session in venue.manager.sessions:
        if session.target_comp_id == target_comp_id:
            return session.key
    raise CommandError("unknown session '%s'" % target_comp_id, E_NOT_FOUND)


def _parse_price(venue, text, field):
    from ..core.prices import PriceError
    try:
        return venue.codec.parse(text)
    except PriceError as exc:
        raise CommandError("%s: %s" % (field, exc))

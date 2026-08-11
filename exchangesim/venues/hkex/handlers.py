"""HKEX OCG-C FIX application: messages in, execution reports out.

The whole venue-specific translation layer. Inbound it turns NewOrderSingle,
OrderCancelRequest, OrderCancelReplaceRequest and OrderMassCancelRequest into
core requests; outbound it renders core events as the ExecutionReport shapes of
section 7.7.7, plus OrderCancelReject, OrderMassCancelReport and
BusinessMessageReject.

Three things here have no Japannext counterpart:

* **Repeating groups.** ``<Parties>`` carries the Broker Number, the BCAN and
  the BS User ID as parallel ``PartyID(448)`` / ``PartyRole(452)`` sequences,
  and ``<DisclosureInstructionGrp>`` is mandatory on order messages. Both are
  read positionally from ``Message.get_all``; the count field is checked so a
  malformed group is a session Reject rather than a silent misread.
* **Market routing by reference data.** No message names its market segment, so
  it comes from the security. An order in a security the venue does not list is
  refused here rather than reaching the engine, because there is no book to
  route it to.
* **Two sides to a self-match prevention cancel.** HKEX reports the aggressive
  and the passive order with different ``ExecRestatementReason(378)`` values,
  which the renderer distinguishes by the instruction that fired.
"""

import logging

from ...core.behaviour import DELAY, DROP
from ...core.commands import CancelRequest, NewOrderRequest, ReplaceRequest
from ...core.enums import (
    CancelReason,
    ExecInst,
    Liquidity,
    OrderStatus,
    OrderType,
    RejectReason,
    StpMode,
    TimeInForce,
)
from ...core.prices import PriceError
from ...fix import constants as C
from ...fix.dictionary import Failure
from ...fix.message import Message
from ...fix.session import Application
from . import dictionary as D
from . import rules

log = logging.getLogger(__name__)

#: OrderID reported when no order was created.
NO_ORDER_ID = "NONE"

#: PartyRoles this venue reads off an inbound order.
_BROKER = D.PartyRole.EXECUTING_FIRM
_BCAN = D.PartyRole.CLIENT_ID
_LOCATION = D.PartyRole.LOCATION_ID


class HkexApplication(Application):
    """Bridges FIXT.1.1 sessions to the venue engine."""

    def __init__(self, venue):
        self.venue = venue
        self.engine = venue.engine
        self.codec = venue.codec
        self.clock = venue.clock
        self.exec_ids = venue.exec_ids
        self.mass_cancel_ids = venue.mass_cancel_ids
        #: session key -> Session, so reports can be routed after a disconnect.
        self._sessions = {}

    # -- session callbacks -------------------------------------------------

    def on_logon(self, session):
        self._sessions[session.key] = session

    def on_logout(self, session, reason):
        """Apply Cancel on Disconnect (section 6.10).

        The resulting reports are numbered and stored even though nobody is
        connected; the client retrieves them by resend at its next logon.
        """
        if not session.config.cancel_on_disconnect:
            return

        events = self.engine.cancel_session_orders(
            session.key, CancelReason.CANCEL_ON_DISCONNECT,
            "cancelled on disconnect")
        if events:
            log.info("cancel on disconnect: %d order(s) for %s",
                     len(events), session.target_comp_id)
        self._emit(session, events)

    def on_message(self, session, message):
        self._sessions[session.key] = session
        handler = _HANDLERS.get(message.msg_type)
        if handler is None:
            self._send_business_reject(
                session, message,
                C.BusinessRejectReason.UNSUPPORTED_MESSAGE_TYPE,
                "MsgType '%s' is not supported" % message.msg_type)
            return None
        return handler(self, session, message)

    # -- inbound: new order ------------------------------------------------

    def _on_new_order(self, session, message):
        failure = self._check_groups(message, disclosure=True)
        if failure is not None:
            return failure

        parties = self._parties(message)
        security_id = message.get(D.SECURITY_ID)

        if message.get(D.LOT_TYPE) == D.LotType.ODD_LOT:
            # The odd/special lot book is semi-automatic and matched by trade
            # request; simulating it as a continuous book would be a lie.
            self._reject_order(
                session, message, parties, D.OrdRejReason.OTHER,
                "odd and special lot orders are not supported by this simulator")
            return None

        time_in_force = message.get(D.TIME_IN_FORCE, D.TimeInForce.DAY)
        if time_in_force == D.TimeInForce.AT_CROSSING:
            self._reject_order(
                session, message, parties, D.OrdRejReason.OTHER,
                "TimeInForce 9 (At Crossing) requires an auction session")
            return None

        market = self.venue.segment_for(security_id)
        if market is None or market not in self.engine.markets:
            self._reject_order(
                session, message, parties, D.OrdRejReason.OTHER,
                "security '%s' is not listed on this venue" % security_id)
            return None
        if (session.config.allowed_sub_ids
                and market not in session.config.allowed_sub_ids):
            self._reject_order(
                session, message, parties, D.OrdRejReason.OTHER,
                "this session may not trade market segment '%s'" % market)
            return None

        try:
            price = self._price(message, D.PRICE)
        except PriceError as exc:
            return Failure(C.SessionRejectReason.INCORRECT_DATA_FORMAT,
                           D.PRICE, str(exc))

        smp_id = message.get(D.SELF_MATCH_PREVENTION_ID)

        request = NewOrderRequest(
            session_key=session.key,
            market=market,
            cl_ord_id=message.get(D.CL_ORD_ID),
            symbol=security_id,
            side=rules.SIDE_TO_CORE.get(message.get(D.SIDE)),
            quantity=message.get_int(D.ORDER_QTY, 0),
            price=price,
            order_type=rules.ORD_TYPE_TO_CORE.get(
                message.get(D.ORD_TYPE), OrderType.LIMIT),
            time_in_force=rules.TIF_TO_CORE.get(time_in_force, TimeInForce.DAY),
            exec_inst=self._exec_inst(message),
            capacity=rules.CAPACITY_TO_CORE.get(message.get(D.ORDER_CAPACITY)),
            account=parties.get(_BCAN),
            mpid=parties.get(_BROKER),
            stp_id=smp_id,
            stp_instruction=self.venue.smp_instruction_for(smp_id),
            received_at=self.clock.now(),
        )

        self._emit(session, self.engine.new_order(request))
        return None

    # -- inbound: cancel ---------------------------------------------------

    def _on_cancel(self, session, message):
        failure = self._check_groups(message)
        if failure is not None:
            return failure

        if self._in_no_cancellation_period(message):
            self._send_cancel_reject(
                session, message, D.CxlRejReason.TOO_LATE_TO_CANCEL,
                "the auction's No Cancellation Period has begun")
            return None

        request = CancelRequest(
            session_key=session.key,
            # The order carries its own market; the engine reads it from there.
            market=self.venue.segment_for(message.get(D.SECURITY_ID)),
            cl_ord_id=message.get(D.CL_ORD_ID),
            orig_cl_ord_id=message.get(D.ORIG_CL_ORD_ID),
            symbol=message.get(D.SECURITY_ID),
            side=rules.SIDE_TO_CORE.get(message.get(D.SIDE)),
            quantity=message.get_int(D.ORDER_QTY),
            received_at=self.clock.now(),
        )

        self._emit(session, self.engine.cancel_order(request))
        return None

    # -- inbound: replace --------------------------------------------------

    def _on_replace(self, session, message):
        failure = self._check_groups(message, disclosure=True)
        if failure is not None:
            return failure

        if self._in_no_cancellation_period(message):
            # An amend is a cancel-replace, so the same bar applies.
            self._send_cancel_reject(
                session, message, D.CxlRejReason.TOO_LATE_TO_CANCEL,
                "the auction's No Cancellation Period has begun",
                is_replace=True)
            return None

        try:
            price = self._price(message, D.PRICE)
        except PriceError as exc:
            return Failure(C.SessionRejectReason.INCORRECT_DATA_FORMAT,
                           D.PRICE, str(exc))

        time_in_force = message.get(D.TIME_IN_FORCE)
        request = ReplaceRequest(
            session_key=session.key,
            market=self.venue.segment_for(message.get(D.SECURITY_ID)),
            cl_ord_id=message.get(D.CL_ORD_ID),
            orig_cl_ord_id=message.get(D.ORIG_CL_ORD_ID),
            symbol=message.get(D.SECURITY_ID),
            side=rules.SIDE_TO_CORE.get(message.get(D.SIDE)),
            quantity=message.get_int(D.ORDER_QTY),
            price=price,
            time_in_force=(rules.TIF_TO_CORE.get(time_in_force)
                           if time_in_force else None),
            exec_inst=self._exec_inst(message),
            capacity=rules.CAPACITY_TO_CORE.get(message.get(D.ORDER_CAPACITY)),
            received_at=self.clock.now(),
        )

        self._emit(session, self.engine.replace_order(request))
        return None

    # -- inbound: mass cancel ----------------------------------------------

    def _on_mass_cancel(self, session, message):
        """Section 7.7.5. Scope is always this session's own live orders."""
        failure = self._check_groups(message)
        if failure is not None:
            return failure

        scope = message.get(D.MASS_CANCEL_REQUEST_TYPE)
        security_id = message.get(D.SECURITY_ID)
        segment = message.get(D.MARKET_SEGMENT_ID)

        if scope == D.MassCancelRequestType.SECURITY and not security_id:
            self._send_mass_cancel_reject(
                session, message, D.MassCancelRejectReason.OTHER,
                "SecurityID (48) is required for MassCancelRequestType 1")
            return None

        if scope == D.MassCancelRequestType.MARKET_SEGMENT:
            if not segment:
                self._send_mass_cancel_reject(
                    session, message, D.MassCancelRejectReason.OTHER,
                    "MarketSegmentID (1300) is required for "
                    "MassCancelRequestType 9")
                return None
            if segment not in self.engine.markets:
                self._send_mass_cancel_reject(
                    session, message,
                    D.MassCancelRejectReason.INVALID_MARKET_SEGMENT,
                    "market segment '%s' is not running" % segment)
                return None

        side = rules.SIDE_TO_CORE.get(message.get(D.SIDE))

        def affected(order):
            if order.session_key != session.key:
                return False
            if security_id and order.symbol != security_id:
                return False
            if segment and order.market != segment:
                return False
            if side is not None and order.side != side:
                return False
            return True

        events = []
        for market in self.engine.markets.values():
            events.extend(market.cancel_all(
                affected, CancelReason.MASS_CANCEL,
                "cancelled by mass cancel request"))

        # The report goes out first: it answers the request, and the individual
        # cancels are consequences of it.
        self._send_mass_cancel_report(session, message, scope, len(events))
        self._emit(session, events)
        return None

    # -- parsing helpers ---------------------------------------------------

    def _in_no_cancellation_period(self, message):
        """True when the security's market is in a locked auction period.

        The control plane deliberately does *not* consult this -- an operator
        driving the simulator can always clear a book, and a scenario's teardown
        has to be able to run whatever phase the venue was left in.
        """
        session = self.venue.auctions.get(
            self.venue.segment_for(message.get(D.SECURITY_ID)))
        return session is not None and not session.cancellable

    def _parties(self, message):
        """The <Parties> block as ``{PartyRole: PartyID}``.

        Roles are unique within the group in every inbound message this venue
        accepts, so a dict loses nothing and reads far better than two parallel
        lists at every use site.
        """
        ids = message.get_all(D.PARTY_ID)
        roles = message.get_all(D.PARTY_ROLE)
        return dict(zip(roles, ids))

    def _check_groups(self, message, disclosure=False):
        """Validate the repeating-group counts against the entries present.

        The codec models groups as repeated tags rather than as structures, so
        this is the point at which a count that disagrees with reality is
        caught. FIX gives it a reason of its own -- 16, incorrect NumInGroup.
        """
        failure = _check_count(
            message, D.NO_PARTY_IDS, "NoPartyIDs",
            (D.PARTY_ID, D.PARTY_ID_SOURCE, D.PARTY_ROLE))
        if failure is not None:
            return failure

        if not disclosure:
            return None

        return _check_count(
            message, D.NO_DISCLOSURE_INSTRUCTIONS, "NoDisclosureInstructions",
            (D.DISCLOSURE_TYPE, D.DISCLOSURE_INSTRUCTION))

    def _price(self, message, tag):
        raw = message.get(tag)
        if raw is None:
            return None
        return self.codec.parse(raw)

    def _exec_inst(self, message):
        raw = message.get(D.EXEC_INST)
        if not raw:
            return ()
        instructions = []
        for value in raw.split(" "):
            if value == D.ExecInstValue.IGNORE_PRICE_CHECKS:
                instructions.append(ExecInst.IGNORE_PRICE_CHECK)
            elif value in rules.EXEC_INST_TO_CORE:
                instructions.append(rules.EXEC_INST_TO_CORE[value])
        return tuple(instructions)

    # -- outbound ----------------------------------------------------------

    def _emit(self, session, events):
        """Render core events onto the session that owns each affected order."""
        for event in events:
            message = self._render(event)
            if message is None:
                continue
            target = self._session_for(event, session)
            if target is None:
                continue
            self._deliver(target, message, getattr(event, "order", None))

    def _deliver(self, target, message, order):
        """Send a report, honouring any injected drop or delay."""
        behaviour = self.engine.behaviour
        if not behaviour.active or order is None:
            target.send(message)
            return

        if behaviour.take(DROP, order) is not None:
            log.info("dropping %s for order %s (injected behaviour)",
                     message.get(D.EXEC_TYPE), order.order_id)
            return

        rule = behaviour.take(DELAY, order)
        if rule is None or not rule.delay_ms:
            target.send(message)
            return

        delay = rule.delay_ms / 1000.0
        log.info("delaying %s for order %s by %dms (injected behaviour)",
                 message.get(D.EXEC_TYPE), order.order_id, rule.delay_ms)
        self.venue.reactor.call_later(delay, lambda: target.send(message))

    def _session_for(self, event, fallback):
        """The session that owns the order an event concerns.

        A trade or a self-match prevention cancel touches a resting order that
        may belong to a different client, so reports route by owner rather than
        to whoever sent the triggering message.
        """
        order = getattr(event, "order", None)
        if order is None:
            return fallback
        return self._sessions.get(order.session_key, fallback)

    def _render(self, event):
        renderer = _RENDERERS.get(type(event).__name__)
        return renderer(self, event) if renderer else None

    # -- execution reports -------------------------------------------------

    def _base_report(self, order, exec_type, ord_status):
        message = Message.create(C.EXECUTION_REPORT)
        message.set(D.EXEC_ID, self.exec_ids.next())
        message.set(D.CL_ORD_ID, order.cl_ord_id)
        message.set(D.ORDER_ID, order.order_id)
        self._set_parties(message, order)
        self._set_instrument(message, order.symbol)
        message.set(D.ORD_TYPE, rules.ORD_TYPE_TO_FIX.get(
            order.order_type, D.OrdType.LIMIT))
        message.set_if(D.TIME_IN_FORCE,
                       rules.TIF_TO_FIX.get(order.time_in_force))
        message.set(D.SIDE, rules.SIDE_TO_FIX.get(order.side, D.SideValue.BUY))
        message.set(D.ORDER_QTY, order.quantity)
        if order.price is not None:
            message.set(D.PRICE, self.codec.format(order.price))
        message.set(D.TRANSACT_TIME, self.clock.timestamp())
        message.set_if(D.ORDER_CAPACITY,
                       rules.CAPACITY_TO_FIX.get(order.capacity))
        message.set_if(D.SELF_MATCH_PREVENTION_ID, order.stp_id)
        message.set(D.ORD_STATUS, ord_status)
        message.set(D.EXEC_TYPE, exec_type)
        message.set(D.CUM_QTY, order.cum_qty)
        message.set(D.LEAVES_QTY, order.leaves_qty)
        message.set(D.LOT_TYPE, D.LotType.ROUND_LOT)
        if order.orig_cl_ord_id:
            message.set(D.ORIG_CL_ORD_ID, order.orig_cl_ord_id)
        return message

    def _set_parties(self, message, order):
        """Rebuild the <Parties> block from what the order kept."""
        entries = []
        if order.mpid:
            entries.append((order.mpid, _BROKER))
        if order.account:
            entries.append((order.account, _BCAN))
        if not entries:
            return
        message.set(D.NO_PARTY_IDS, len(entries))
        for party_id, role in entries:
            message.append(D.PARTY_ID, party_id)
            message.append(D.PARTY_ID_SOURCE, D.PartyIDSource.PROPRIETARY)
            message.append(D.PARTY_ROLE, role)

    def _set_instrument(self, message, symbol):
        message.set(D.SECURITY_ID, symbol)
        message.set(D.SECURITY_ID_SOURCE, D.SecurityIDSource.EXCHANGE_SYMBOL)
        message.set(D.SECURITY_EXCHANGE, D.SECURITY_EXCHANGE_VALUE)

    def _render_accepted(self, event):
        return self._base_report(event.order, D.ExecType.NEW, D.OrdStatus.NEW)

    def _render_rejected(self, event):
        order = event.order
        message = self._base_report(order, D.ExecType.REJECTED,
                                    D.OrdStatus.REJECTED)
        message.set(D.ORD_REJ_REASON, self._reject_reason(event, order))
        message.set_if(D.REJECT_TEXT, _truncate(event.text))
        if event.reason == RejectReason.DUPLICATE_ORDER:
            message.set(D.ORDER_ID, NO_ORDER_ID)
        return message

    def _reject_reason(self, event, order):
        """OrdRejReason(103), distinguishing "no reference price" from the band.

        HKEX has a dedicated code, 19, for an instrument with no nominal price,
        and the core reports that case as an ordinary band violation because it
        has no separate reason for it.
        """
        if event.reason == RejectReason.PRICE_OUTSIDE_BAND:
            instrument = self.engine.instrument(order.symbol)
            if instrument is not None and instrument.price_limits is None:
                return rules.NO_REFERENCE_PRICE
        return rules.REJECT_TO_ORD_REJ_REASON.get(
            event.reason, D.OrdRejReason.OTHER)

    def _render_filled(self, event):
        order = event.order
        complete = event.is_complete
        status = (D.OrdStatus.FILLED if complete
                  else D.OrdStatus.PARTIALLY_FILLED)

        message = self._base_report(order, D.ExecType.TRADE, status)

        # Restate the running totals from the event's snapshot: the order has
        # moved on by the time a batch of events is rendered.
        message.set(D.ORDER_QTY, event.order_qty)
        message.set(D.CUM_QTY, event.cum_qty)
        message.set(D.LEAVES_QTY, event.leaves_qty)

        message.set(D.LAST_PX, self.codec.format(event.price))
        message.set(D.LAST_QTY, event.quantity)
        message.set(D.TRD_MATCH_ID, event.trade_id)
        message.set(D.MATCH_TYPE, D.MatchType.AUTO_MATCH)
        message.set(D.AGGRESSOR_INDICATOR,
                    C.YES if event.liquidity == Liquidity.REMOVED else C.NO)
        return message

    def _render_cancelled(self, event):
        order = event.order
        expired = event.reason in rules.EXPIRING_CANCEL_REASONS
        exec_type = D.ExecType.EXPIRED if expired else D.ExecType.CANCELED
        status = D.OrdStatus.EXPIRED if expired else D.OrdStatus.CANCELED

        message = self._base_report(order, exec_type, status)
        message.set(D.LEAVES_QTY, 0)
        if expired:
            message.set_if(D.REJECT_TEXT, _truncate(event.text))
        else:
            message.set_if(C.TEXT, _truncate(event.text))
        restatement = self._restatement_for(event)
        if restatement is not None:
            message.set(D.EXEC_RESTATEMENT_REASON, restatement)
        return message

    def _restatement_for(self, event):
        """ExecRestatementReason(378) for an unsolicited removal.

        Self-match prevention is the interesting case: HKEX names the two sides
        separately, and which side this order was follows from the instruction
        that fired -- cancel-aggressive removes the incoming order, cancel-passive
        the resting one.
        """
        if event.reason != CancelReason.SELF_TRADE_PREVENTION:
            return rules.CANCEL_REASON_TO_RESTATEMENT.get(event.reason)
        if getattr(event.order, "stp_instruction", None) == StpMode.CANCEL_OLDEST:
            return D.ExecRestatementReason.SMP_CANCEL_PASSIVE
        return D.ExecRestatementReason.SMP_CANCEL_AGGRESSIVE

    def _render_replaced(self, event):
        order = event.order
        message = self._base_report(
            order, D.ExecType.REPLACED,
            rules.STATUS_TO_FIX.get(order.status, D.OrdStatus.NEW))
        message.set(D.ORIG_CL_ORD_ID, event.previous_cl_ord_id)
        return message

    def _render_decremented(self, event):
        """A self-match reduced an order without cancelling it.

        HKEX's own prevention never decrements -- it cancels one side outright --
        so this only arises if a market is deliberately configured into the
        core's DECREMENT mode. It is reported as a market-operation restatement
        so that the client at least sees the size change.
        """
        order = event.order
        message = self._base_report(
            order, D.ExecType.REPLACED,
            rules.STATUS_TO_FIX.get(order.status, D.OrdStatus.NEW))
        message.set(D.EXEC_RESTATEMENT_REASON,
                    D.ExecRestatementReason.MARKET_OPERATION)
        return message

    def _render_cancel_rejected(self, event):
        message = Message.create(C.ORDER_CANCEL_REJECT)
        message.set(D.CL_ORD_ID, event.cl_ord_id)
        message.set_if(D.ORIG_CL_ORD_ID, event.orig_cl_ord_id)
        order = event.order
        message.set(D.ORDER_ID, order.order_id if order else NO_ORDER_ID)
        if order is not None:
            self._set_parties(message, order)
        message.set(D.TRANSACT_TIME, self.clock.timestamp())
        message.set(D.ORD_STATUS,
                    rules.STATUS_TO_FIX.get(order.status, D.OrdStatus.REJECTED)
                    if order else D.OrdStatus.REJECTED)
        message.set(D.CXL_REJ_RESPONSE_TO,
                    D.CxlRejResponseTo.CANCEL_REPLACE_REQUEST if event.is_replace
                    else D.CxlRejResponseTo.CANCEL_REQUEST)
        message.set(D.CXL_REJ_REASON, rules.CANCEL_REJECT_TO_REASON.get(
            event.reason, D.CxlRejReason.OTHER))
        message.set_if(D.REJECT_TEXT, _truncate(event.text))
        return message

    # -- other outbound messages -------------------------------------------

    def _reject_order(self, session, message, parties, reason, text):
        """Refuse an order the engine never saw.

        Anything the engine builds an order for is rejected through the normal
        event path with a real OrderID. This covers what it cannot: an unlisted
        security, an odd lot, a time-in-force that needs an auction.
        """
        report = Message.create(C.EXECUTION_REPORT)
        report.set(D.EXEC_ID, self.exec_ids.next())
        report.set_if(D.CL_ORD_ID, message.get(D.CL_ORD_ID))
        report.set(D.ORDER_ID, NO_ORDER_ID)
        if parties.get(_BROKER):
            report.set(D.NO_PARTY_IDS, 1)
            report.append(D.PARTY_ID, parties[_BROKER])
            report.append(D.PARTY_ID_SOURCE, D.PartyIDSource.PROPRIETARY)
            report.append(D.PARTY_ROLE, _BROKER)
        self._set_instrument(report, message.get(D.SECURITY_ID) or "")
        report.set_if(D.ORD_TYPE, message.get(D.ORD_TYPE))
        report.set_if(D.SIDE, message.get(D.SIDE))
        report.set_if(D.ORDER_QTY, message.get(D.ORDER_QTY))
        report.set_if(D.PRICE, message.get(D.PRICE))
        report.set(D.TRANSACT_TIME, self.clock.timestamp())
        report.set(D.ORD_STATUS, D.OrdStatus.REJECTED)
        report.set(D.EXEC_TYPE, D.ExecType.REJECTED)
        report.set(D.CUM_QTY, 0)
        report.set(D.LEAVES_QTY, 0)
        report.set(D.ORD_REJ_REASON, reason)
        report.set(D.REJECT_TEXT, _truncate(text))
        session.send(report)
        log.info("rejected order %s: %s", message.get(D.CL_ORD_ID), text)

    def _send_cancel_reject(self, session, message, reason, text,
                            is_replace=False):
        """Refuse a cancel or amend the venue never passed to the engine."""
        reject = Message.create(C.ORDER_CANCEL_REJECT)
        reject.set(D.CL_ORD_ID, message.get(D.CL_ORD_ID))
        reject.set_if(D.ORIG_CL_ORD_ID, message.get(D.ORIG_CL_ORD_ID))
        order = self.engine.registry.resolve(
            session.key, message.get(D.ORIG_CL_ORD_ID))
        reject.set(D.ORDER_ID, order.order_id if order else NO_ORDER_ID)
        reject.set(D.TRANSACT_TIME, self.clock.timestamp())
        reject.set(D.ORD_STATUS,
                   rules.STATUS_TO_FIX.get(order.status, D.OrdStatus.REJECTED)
                   if order else D.OrdStatus.REJECTED)
        reject.set(D.CXL_REJ_RESPONSE_TO,
                   D.CxlRejResponseTo.CANCEL_REPLACE_REQUEST if is_replace
                   else D.CxlRejResponseTo.CANCEL_REQUEST)
        reject.set(D.CXL_REJ_REASON, reason)
        reject.set(D.REJECT_TEXT, _truncate(text))
        session.send(reject)

    def _mass_cancel_report(self, message, response):
        report = Message.create(D.ORDER_MASS_CANCEL_REPORT)
        report.set_if(D.CL_ORD_ID, message.get(D.CL_ORD_ID))
        report.set(D.MASS_ACTION_REPORT_ID, self.mass_cancel_ids.next())
        report.set_if(D.MASS_CANCEL_REQUEST_TYPE,
                      message.get(D.MASS_CANCEL_REQUEST_TYPE))
        for tag in (D.NO_PARTY_IDS, D.PARTY_ID, D.PARTY_ID_SOURCE, D.PARTY_ROLE):
            for value in message.get_all(tag):
                report.append(tag, value)
        if message.get(D.SECURITY_ID):
            self._set_instrument(report, message.get(D.SECURITY_ID))
        report.set(D.MASS_CANCEL_RESPONSE, response)
        report.set(D.TRANSACT_TIME, self.clock.timestamp())
        return report

    def _send_mass_cancel_report(self, session, message, scope, cancelled):
        report = self._mass_cancel_report(message, scope)
        session.send(report)
        log.info("mass cancel (type %s) removed %d order(s) for %s",
                 scope, cancelled, session.target_comp_id)

    def _send_mass_cancel_reject(self, session, message, reason, text):
        report = self._mass_cancel_report(message, D.MassCancelResponse.REJECTED)
        report.set(D.MASS_CANCEL_REJECT_REASON, reason)
        report.set(C.TEXT, _truncate(text))
        session.send(report)
        log.info("mass cancel rejected for %s: %s", session.target_comp_id, text)

    def _send_business_reject(self, session, message, reason, text):
        reject = Message.create(C.BUSINESS_MESSAGE_REJECT)
        reject.set_if(C.REF_SEQ_NUM, message.seq_num)
        reject.set(C.REF_MSG_TYPE, message.msg_type)
        reject.set(C.BUSINESS_REJECT_REASON, reason)
        reject.set_if(C.BUSINESS_REJECT_REF_ID, message.get(D.CL_ORD_ID))
        reject.set(C.TEXT, _truncate(text))
        session.send(reject)

    # -- registration ------------------------------------------------------

    def register_session(self, session):
        self._sessions[session.key] = session

    def sessions(self):
        return list(self._sessions.values())


def _check_count(message, count_tag, name, member_tags):
    """NumInGroup against the entries actually present."""
    declared = message.get_int(count_tag)
    if declared is None:
        return Failure(C.SessionRejectReason.REQUIRED_TAG_MISSING, count_tag,
                       "%s (%d) is required" % (name, count_tag))
    for tag in member_tags:
        present = len(message.get_all(tag))
        if present != declared:
            return Failure(
                C.SessionRejectReason.INCORRECT_NUM_IN_GROUP, count_tag,
                "%s (%d) says %d but tag %d appears %d time(s)"
                % (name, count_tag, declared, tag, present))
    return None


def _truncate(text, limit=255):
    if not text:
        return None
    return text[:limit]


_HANDLERS = {
    C.NEW_ORDER_SINGLE: HkexApplication._on_new_order,
    C.ORDER_CANCEL_REQUEST: HkexApplication._on_cancel,
    C.ORDER_CANCEL_REPLACE_REQUEST: HkexApplication._on_replace,
    D.ORDER_MASS_CANCEL_REQUEST: HkexApplication._on_mass_cancel,
}

_RENDERERS = {
    "OrderAccepted": HkexApplication._render_accepted,
    "OrderRejected": HkexApplication._render_rejected,
    "OrderFilled": HkexApplication._render_filled,
    "OrderCancelled": HkexApplication._render_cancelled,
    "OrderReplaced": HkexApplication._render_replaced,
    "OrderDecremented": HkexApplication._render_decremented,
    "CancelRejected": HkexApplication._render_cancel_rejected,
    # TradeExecuted is the market-data view; clients see the two OrderFilled
    # reports instead.
    "TradeExecuted": None,
    "TradingStateChanged": None,
}

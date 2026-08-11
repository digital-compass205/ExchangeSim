"""Japannext FIX application: messages in, execution reports out.

This is the whole venue-specific translation layer. Inbound, it turns
NewOrderSingle / OrderCancelRequest / OrderCancelReplaceRequest into core
requests. Outbound, it renders core events as the six shapes of ExecutionReport
the specification defines, plus OrderCancelReject, BusinessMessageReject and
TradingSessionStatus.

Market routing is by SubID: ``TargetSubID(57)`` selects J-Market Daytime,
J-Market Nighttime, X-Market or U-Market, falling back to the session's
configured default when the tag is absent, exactly as the specification says.
"""

import logging

from ...core.behaviour import DELAY, DROP
from ...core.commands import CancelRequest, NewOrderRequest, ReplaceRequest
from ...core.enums import CancelReason, OrderStatus, OrderType, RejectReason
from ...core.prices import PriceError
from ...fix import constants as C
from ...fix.dictionary import Failure
from ...fix.message import Message
from ...fix.session import Application
from . import dictionary as D
from . import rules

log = logging.getLogger(__name__)

#: OrderID reported when no order was created, per the specification.
NO_ORDER_ID = "NONE"


class JapannextApplication(Application):
    """Bridges FIX sessions to the venue engine."""

    def __init__(self, venue):
        self.venue = venue
        self.engine = venue.engine
        self.codec = venue.codec
        self.clock = venue.clock
        self.exec_ids = venue.exec_ids
        #: session key -> Session, so reports can be routed after a disconnect.
        self._sessions = {}

    # -- session callbacks -------------------------------------------------

    def on_logon(self, session):
        self._sessions[session.key] = session
        # A newly logged-on client is told the state of every market it may
        # address, so it never has to guess whether the venue is open.
        for market_name in self._markets_for(session):
            market = self.engine.market(market_name)
            if market is not None:
                session.send(self.trading_session_status(
                    market_name, market.state.market_state))

    def on_logout(self, session, reason):
        """Apply Cancel on Disconnect, if the session is configured for it.

        The resulting reports are numbered and stored even though nobody is
        connected; the client retrieves them by resend at its next logon, which
        is what the specification means by "sent upon reconnection".
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
        market, failure = self._resolve_market(session, message)
        if failure is not None:
            return failure

        try:
            price = self._price(message, D.PRICE)
        except PriceError as exc:
            return Failure(C.SessionRejectReason.INCORRECT_DATA_FORMAT,
                           D.PRICE, str(exc))

        request = NewOrderRequest(
            session_key=session.key,
            market=market,
            cl_ord_id=message.get(D.CL_ORD_ID),
            symbol=message.get(D.SYMBOL),
            side=rules.SIDE_TO_CORE.get(message.get(D.SIDE)),
            quantity=message.get_int(D.ORDER_QTY, 0),
            price=price,
            order_type=OrderType.LIMIT,
            time_in_force=rules.TIF_TO_CORE.get(
                message.get(D.TIME_IN_FORCE, D.TimeInForce.DAY)),
            min_qty=message.get_int(D.MIN_QTY, 0),
            exec_inst=self._exec_inst(message),
            capacity=rules.CAPACITY_TO_CORE.get(
                message.get(D.RULE_80A, D.Rule80A.PRINCIPAL)),
            account=message.get(D.ACCOUNT),
            mpid=message.get(D.CLIENT_ID),
            cash_margin=message.get(D.CASH_MARGIN),
            margin_type=message.get(D.MARGIN_TRANSACTION_TYPE),
            received_at=self.clock.now(),
        )

        self._emit(session, self.engine.new_order(request))
        return None

    # -- inbound: cancel ---------------------------------------------------

    def _on_cancel(self, session, message):
        market, failure = self._resolve_market(session, message)
        if failure is not None:
            return failure

        request = CancelRequest(
            session_key=session.key,
            market=market,
            cl_ord_id=message.get(D.CL_ORD_ID),
            orig_cl_ord_id=message.get(D.ORIG_CL_ORD_ID),
            symbol=message.get(D.SYMBOL),
            side=rules.SIDE_TO_CORE.get(message.get(D.SIDE)),
            # OrderQty is required on the wire but the specification says its
            # value is ignored.
            quantity=message.get_int(D.ORDER_QTY),
            received_at=self.clock.now(),
        )

        self._emit(session, self.engine.cancel_order(request))
        return None

    # -- inbound: replace --------------------------------------------------

    def _on_replace(self, session, message):
        market, failure = self._resolve_market(session, message)
        if failure is not None:
            return failure

        try:
            price = self._price(message, D.PRICE)
        except PriceError as exc:
            return Failure(C.SessionRejectReason.INCORRECT_DATA_FORMAT,
                           D.PRICE, str(exc))

        time_in_force = message.get(D.TIME_IN_FORCE)
        request = ReplaceRequest(
            session_key=session.key,
            market=market,
            cl_ord_id=message.get(D.CL_ORD_ID),
            orig_cl_ord_id=message.get(D.ORIG_CL_ORD_ID),
            symbol=message.get(D.SYMBOL),
            side=rules.SIDE_TO_CORE.get(message.get(D.SIDE)),
            quantity=message.get_int(D.ORDER_QTY),
            price=price,
            time_in_force=(rules.TIF_TO_CORE.get(time_in_force)
                           if time_in_force else None),
            min_qty=message.get_int(D.MIN_QTY),
            exec_inst=self._exec_inst(message),
            capacity=rules.CAPACITY_TO_CORE.get(message.get(D.RULE_80A)),
            received_at=self.clock.now(),
        )

        self._emit(session, self.engine.replace_order(request))
        return None

    # -- parsing helpers ---------------------------------------------------

    def _resolve_market(self, session, message):
        """Pick the market from TargetSubID, or the session default."""
        sub_id = message.get(C.TARGET_SUB_ID) or session.config.default_sub_id
        if sub_id is None:
            return None, Failure(C.SessionRejectReason.REQUIRED_TAG_MISSING,
                                 C.TARGET_SUB_ID,
                                 "TargetSubID (57) is required; this session "
                                 "has no default market")
        if self.engine.market(sub_id) is None:
            return None, Failure(C.SessionRejectReason.VALUE_INCORRECT,
                                 C.TARGET_SUB_ID,
                                 "unknown market '%s'" % sub_id)
        if (session.config.allowed_sub_ids
                and sub_id not in session.config.allowed_sub_ids):
            return None, Failure(C.SessionRejectReason.VALUE_INCORRECT,
                                 C.TARGET_SUB_ID,
                                 "this session may not trade market '%s'" % sub_id)
        return sub_id, None

    def _price(self, message, tag):
        raw = message.get(tag)
        if raw is None:
            return None
        return self.codec.parse(raw)

    def _exec_inst(self, message):
        raw = message.get(D.EXEC_INST)
        if not raw:
            return ()
        return tuple(rules.EXEC_INST_TO_CORE[value]
                     for value in raw.split(" ")
                     if value in rules.EXEC_INST_TO_CORE)

    def _markets_for(self, session):
        if session.config.allowed_sub_ids:
            return sorted(session.config.allowed_sub_ids)
        if session.config.default_sub_id:
            return [session.config.default_sub_id]
        return sorted(self.engine.markets)

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
            self._stamp_market(message, event)
            self._deliver(target, message, getattr(event, "order", None))

    def _deliver(self, target, message, order):
        """Send a report, honouring any injected drop or delay.

        A dropped report still consumes no sequence number, so the client sees
        no gap -- it simply never learns of the event, which is the harsher
        test. A delayed report is sent normally once the timer fires, so its
        sequence number is assigned at send time and ordering is preserved.
        """
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

        A trade or a self-trade prevention cancel touches a resting order that
        may belong to a different client, so reports must be routed by owner
        rather than to whoever sent the triggering message.
        """
        order = getattr(event, "order", None)
        if order is None:
            return fallback
        return self._sessions.get(order.session_key, fallback)

    def _stamp_market(self, message, event):
        order = getattr(event, "order", None)
        market = getattr(order, "market", None) or getattr(event, "market", None)
        if market:
            message.set(C.SENDER_SUB_ID, market)

    def _render(self, event):
        renderer = _RENDERERS.get(type(event).__name__)
        return renderer(self, event) if renderer else None

    # -- execution reports -------------------------------------------------

    def _base_report(self, order, exec_type, ord_status):
        message = Message.create(C.EXECUTION_REPORT)
        message.set(D.EXEC_ID, self.exec_ids.next())
        message.set(D.EXEC_TRANS_TYPE, D.ExecTransType.NEW)
        message.set(D.EXEC_TYPE, exec_type)
        message.set(D.ORD_STATUS, ord_status)
        message.set(D.ORDER_ID, order.order_id)
        message.set(D.CL_ORD_ID, order.cl_ord_id)
        message.set(D.SYMBOL, order.symbol)
        message.set(D.SIDE, rules.SIDE_TO_FIX.get(order.side, D.SideValue.BUY))
        message.set(D.ORDER_QTY, order.quantity)
        message.set(D.ORD_TYPE, D.OrdType.LIMIT)
        message.set(D.CUM_QTY, order.cum_qty)
        message.set(D.LEAVES_QTY, order.leaves_qty)
        message.set(D.AVG_PX, self.codec.format_average(
            order.notional_units, order.cum_qty))
        message.set(D.TRANSACT_TIME, self.clock.timestamp())

        if order.price is not None:
            message.set(D.PRICE, self.codec.format(order.price))
        message.set_if(D.ACCOUNT, order.account)
        message.set_if(D.CLIENT_ID, order.mpid)
        message.set_if(D.RULE_80A, rules.CAPACITY_TO_FIX.get(order.capacity))
        message.set_if(D.TIME_IN_FORCE, rules.TIF_TO_FIX.get(order.time_in_force))
        message.set_if(D.CASH_MARGIN, order.cash_margin)
        message.set_if(D.MARGIN_TRANSACTION_TYPE, order.margin_type)
        if order.min_qty:
            message.set(D.MIN_QTY, order.min_qty)
        if order.orig_cl_ord_id:
            message.set(D.ORIG_CL_ORD_ID, order.orig_cl_ord_id)
        return message

    def _render_accepted(self, event):
        return self._base_report(event.order, D.ExecType.NEW, D.OrdStatus.NEW)

    def _render_rejected(self, event):
        order = event.order
        message = self._base_report(order, D.ExecType.REJECTED,
                                    D.OrdStatus.REJECTED)
        reason = rules.REJECT_TO_ORD_REJ_REASON.get(
            event.reason, D.OrdRejReason.OTHER)
        message.set(D.ORD_REJ_REASON, reason)
        message.set_if(C.TEXT, _truncate(event.text))
        if event.reason == RejectReason.DUPLICATE_ORDER:
            # The specification requires OrderID to be NONE for a duplicate.
            message.set(D.ORDER_ID, NO_ORDER_ID)
        return message

    def _render_filled(self, event):
        order = event.order
        complete = event.is_complete
        exec_type = D.ExecType.FILL if complete else D.ExecType.PARTIAL_FILL
        status = (D.OrdStatus.FILLED if complete
                  else D.OrdStatus.PARTIALLY_FILLED)

        message = self._base_report(order, exec_type, status)

        # Restate the running totals from the event's snapshot: the order has
        # moved on by the time a batch of events is rendered.
        message.set(D.ORDER_QTY, event.order_qty)
        message.set(D.CUM_QTY, event.cum_qty)
        message.set(D.LEAVES_QTY, event.leaves_qty)
        message.set(D.AVG_PX, self.codec.format_average(
            event.notional_units, event.cum_qty))

        message.set(D.LAST_PX, self.codec.format(event.price))
        message.set(D.LAST_SHARES, event.quantity)
        message.set(D.TRD_MATCH_ID, event.trade_id)
        message.set(D.LAST_LIQUIDITY_IND,
                    rules.LIQUIDITY_TO_FIX.get(event.liquidity))
        return message

    def _render_cancelled(self, event):
        order = event.order
        message = self._base_report(order, D.ExecType.CANCELED,
                                    D.OrdStatus.CANCELED)
        message.set(D.LEAVES_QTY, 0)
        message.set_if(C.TEXT, _truncate(event.text))
        restatement = rules.CANCEL_REASON_TO_RESTATEMENT.get(event.reason)
        if restatement is not None:
            message.set(D.EXEC_RESTATEMENT_REASON, restatement)
        return message

    def _render_replaced(self, event):
        order = event.order
        status = rules.STATUS_TO_FIX.get(order.status, D.OrdStatus.REPLACED)
        if order.status == OrderStatus.NEW:
            status = D.OrdStatus.REPLACED
        message = self._base_report(order, D.ExecType.REPLACED, status)
        message.set(D.ORIG_CL_ORD_ID, event.previous_cl_ord_id)
        return message

    def _render_decremented(self, event):
        """Self-trade prevention reduced an order without cancelling it.

        JNX_Self-Trade_Prevention_2.00 says this is reported as an Order
        Canceled or Order Replaced message carrying ExecRestatementReason 100.
        """
        order = event.order
        message = self._base_report(
            order, D.ExecType.REPLACED,
            rules.STATUS_TO_FIX.get(order.status, D.OrdStatus.REPLACED))
        message.set(D.EXEC_RESTATEMENT_REASON,
                    D.ExecRestatementReason.TRADE_PREVENTION)
        return message

    def _render_cancel_rejected(self, event):
        message = Message.create(C.ORDER_CANCEL_REJECT)
        message.set(D.CL_ORD_ID, event.cl_ord_id)
        message.set(D.ORIG_CL_ORD_ID, event.orig_cl_ord_id)
        order = event.order
        message.set(D.ORDER_ID, order.order_id if order else NO_ORDER_ID)
        message.set(D.ORD_STATUS,
                    rules.STATUS_TO_FIX.get(order.status, D.OrdStatus.REJECTED)
                    if order else D.OrdStatus.REJECTED)
        message.set(D.CXL_REJ_REASON, rules.CANCEL_REJECT_TO_REASON.get(
            event.reason, D.CxlRejReason.OTHER))
        message.set(D.CXL_REJ_RESPONSE_TO,
                    D.CxlRejResponseTo.CANCEL_REPLACE_REQUEST if event.is_replace
                    else D.CxlRejResponseTo.CANCEL_REQUEST)
        message.set_if(C.TEXT, _truncate(event.text))
        return message

    # -- other outbound messages -------------------------------------------

    def trading_session_status(self, market, state):
        message = Message.create(C.TRADING_SESSION_STATUS)
        message.set(D.TRADING_SESSION_ID, market)
        message.set(D.TRAD_SES_MODE, D.TradSesMode.TESTING)
        message.set(D.TRAD_SES_STATUS,
                    rules.STATE_TO_TRAD_SES_STATUS.get(
                        state, D.TradSesStatus.HALTED))
        return message

    def broadcast_trading_status(self, market, state):
        """Push a TradingSessionStatus to every session that can see a market."""
        sent = 0
        for session in self._sessions.values():
            if not session.logged_on:
                continue
            if market not in self._markets_for(session):
                continue
            message = self.trading_session_status(market, state)
            message.set(C.SENDER_SUB_ID, market)
            session.send(message)
            sent += 1
        return sent

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


def _truncate(text, limit=255):
    if not text:
        return None
    return text[:limit]


_HANDLERS = {
    C.NEW_ORDER_SINGLE: JapannextApplication._on_new_order,
    C.ORDER_CANCEL_REQUEST: JapannextApplication._on_cancel,
    C.ORDER_CANCEL_REPLACE_REQUEST: JapannextApplication._on_replace,
}

_RENDERERS = {
    "OrderAccepted": JapannextApplication._render_accepted,
    "OrderRejected": JapannextApplication._render_rejected,
    "OrderFilled": JapannextApplication._render_filled,
    "OrderCancelled": JapannextApplication._render_cancelled,
    "OrderReplaced": JapannextApplication._render_replaced,
    "OrderDecremented": JapannextApplication._render_decremented,
    "CancelRejected": JapannextApplication._render_cancel_rejected,
    # TradeExecuted is the market-data view; clients see the two OrderFilled
    # reports instead.
    "TradeExecuted": None,
    "TradingStateChanged": None,
}

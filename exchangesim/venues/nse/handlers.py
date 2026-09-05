"""The NSE gateway: NNF messages in, core requests out, reports back.

Three things about this venue shape the whole file.

**An order has no client identifier.** Modification and cancellation name the
``OrderNumber`` the exchange assigned, which is why every request built here
passes ``order_id`` rather than ``orig_cl_ord_id``. Nothing invents a ClOrdID to
fill the gap: an identifier no client ever sent has no business in the order
record, the audit or the board.

**There is no session-level Reject**, so every refusal is the erroring form of
the transaction that caused it -- ``ORDER_ERROR (2231)`` for an order,
``ORDER_MOD_REJECT (2042)`` for a modification, ``ORDER_CANCEL_REJECT (2072)``
for a cancellation -- carrying a numeric ``ErrorCode`` in the message header.
That is why the session layer hands a dialect failure back here through
``on_invalid`` instead of answering it itself.

**One structure serves the whole order flow.** ``ORDER_ENTRY_REQUEST`` is 290
bytes whether it is an entry, a confirmation, a modification or a refusal, so
:meth:`NseApplication._base_report` fills it once and each renderer changes the
transaction code and whichever running totals differ.
"""

import logging

from ...core.behaviour import DELAY, DROP
from ...core.commands import CancelRequest, NewOrderRequest, ReplaceRequest
from ...core.enums import CancelReason, OrderType, TradingState
from ...core.prices import PriceError
from ...fix.message import Message
from ...nnf import types as WT
from ...nnf.session import Application
from . import dictionary as D
from . import rules
from . import transactions as X

log = logging.getLogger(__name__)


class NseApplication(Application):
    """Bridges NNF box connections to the venue engine."""

    def __init__(self, venue):
        self.venue = venue
        self.engine = venue.engine
        self.codec = venue.codec
        self.clock = venue.clock
        #: session key -> NnfSession, so a report reaches the order's owner even
        #: when a different client's message caused it.
        self._sessions = {}
        #: order id -> the member's own NNFField reference, echoed on reports.
        self._references = {}

    # -- the box sequence --------------------------------------------------

    def on_box_message(self, box, message):
        """Everything that arrives before a user is signed on.

        Registration, box sign-on and user sign-on, in that order. The session
        layer drives the sequence; only the venue knows the structures, so it
        builds every answer here.
        """
        code = int(message.msg_type)
        handler = _BOX_HANDLERS.get(code)
        if handler is None:
            log.warning("box %d sent %s before signing a user on",
                        box.box_id, rules.describe_transaction(code))
            return False
        return handler(self, box, message)

    def _on_registration(self, box, message):
        """SECURE_BOX_REGISTRATION_REQUEST: the last message in clear."""
        if box.box_id != _int(message.get(D.BOX_ID)):
            self._box_error(box, X.SECURE_BOX_REGISTRATION_REQUEST_OUT, 16588)
            box.disconnect("registration named box %s"
                           % message.get(D.BOX_ID))
            return True

        cipher = self.venue.cipher_for(box.box_id)
        if cipher is None and self.venue.requires_encryption:
            # The member collected no key from the Gateway Router, so there is
            # nothing to decrypt the next message with.
            self._box_error(box, X.SECURE_BOX_REGISTRATION_REQUEST_OUT, 19030)
            box.disconnect("no session key was issued for box %d" % box.box_id)
            return True

        box.send(self._box_reply(X.SECURE_BOX_REGISTRATION_REQUEST_OUT, box))
        box.registered(cipher)
        return True

    def _on_box_sign_on(self, box, message):
        """BOX_SIGN_ON_REQUEST: the box names its broker and its session key."""
        expected = self.venue.session_key_for(box.box_id)
        offered = message.get(D.SESSION_KEY)
        if expected is not None and offered != expected:
            self._box_error(box, X.BOX_SIGN_ON_REQUEST_OUT, 16006)
            box.disconnect("box %d offered the wrong session key" % box.box_id)
            return True

        broker_id = message.get(D.BROKER_ID)
        if broker_id not in self.venue.brokers_of(box.box_id):
            self._box_error(box, X.BOX_SIGN_ON_REQUEST_OUT, 16041)
            box.disconnect("box %d is not registered to broker %s"
                           % (box.box_id, broker_id))
            return True

        reply = self._box_reply(X.BOX_SIGN_ON_REQUEST_OUT, box)
        box.send(reply)
        box.signed_on(broker_id)
        return True

    def _on_sign_on(self, box, message):
        """SIGN_ON_REQUEST: one user, over an active box."""
        user_id = _int(message.get(D.SIGNON_USER_ID))
        config = self.venue.manager.user_config(user_id) if user_id else None

        if config is None or config.box_id != box.box_id:
            self._box_error(box, X.SIGN_ON_REQUEST_OUT, 16042, user_id=user_id)
            return True
        if box.user(user_id) is not None:
            self._box_error(box, X.SIGN_ON_REQUEST_OUT, 16004, user_id=user_id)
            return True
        if message.get(D.BROKER_ID) not in (None, config.broker_id):
            self._box_error(box, X.SIGN_ON_REQUEST_OUT, 16041, user_id=user_id)
            return True

        session = box.sign_on_user(config)
        session.send(self._sign_on_reply(config))
        return True

    def _sign_on_reply(self, config):
        """SIGNON_OUT. The password fields come back blank, as Chapter 3 says."""
        message = Message.create(str(X.SIGN_ON_REQUEST_OUT))
        message.set(D.ERROR_CODE, str(X.NO_ERROR))
        message.set(D.USER_ID, str(config.user_id))
        message.set(D.SIGNON_USER_ID, str(config.user_id))
        message.set(D.BROKER_ID, config.broker_id)
        message.set(D.BRANCH_ID, str(config.branch_id))
        message.set(D.TRADER_NAME, config.trader_name or "")
        message.set(D.BROKER_NAME, config.broker_name or "")
        message.set(D.USER_TYPE, config.user_type or D.UserType.DEALER)
        message.set(D.LOG_TIME, str(self._nse_seconds()))
        # The markets this member may trade. Only the Normal market exists here.
        for tag in (D.ELIGIBLE_NORMAL, D.ELIGIBLE_PREOPEN):
            message.set(tag, "Y")
        for tag in (D.ELIGIBLE_ODDLOT, D.ELIGIBLE_SPOT, D.ELIGIBLE_AUCTION,
                    D.ELIGIBLE_CALL_AUCTION1, D.ELIGIBLE_CALL_AUCTION2):
            message.set(tag, "N")
        return message

    def _box_reply(self, code, box):
        message = Message.create(str(code))
        message.set(D.ERROR_CODE, str(X.NO_ERROR))
        message.set(D.BOX_ID, str(box.box_id))
        message.set(D.LOG_TIME, str(self._nse_seconds()))
        return message

    def _box_error(self, box, code, error_code, user_id=None):
        message = Message.create(str(code))
        message.set(D.ERROR_CODE, str(error_code))
        message.set(D.BOX_ID, str(box.box_id))
        if user_id is not None:
            message.set(D.USER_ID, str(user_id))
            message.set(D.SIGNON_USER_ID, str(user_id))
        message.set(D.LOG_TIME, str(self._nse_seconds()))
        box.send(message)
        log.warning("box %d refused %s: %s", box.box_id,
                    rules.describe_transaction(code), X.error_name(error_code))

    def on_checksum_failure(self, box, error):
        """A box sign-off carrying 19031, which the document requires."""
        message = Message.create(str(X.SIGN_OFF_REQUEST_OUT))
        message.set(D.ERROR_CODE, "19031")
        message.set(D.LOG_TIME, str(self._nse_seconds()))
        box.send(message)

    # -- user session callbacks --------------------------------------------

    def on_logon(self, session):
        self._sessions[session.key] = session
        # A newly signed-on user is told the state of the market, so it never
        # has to guess whether the venue is open.
        session.send(self.system_information())

    def on_logout(self, session, reason):
        """Cancel on disconnect, where the user is configured for it.

        The resulting reports are recorded and dropped rather than queued: NNF
        has no resend, and the client learns of them by asking for an order and
        trade download at its next sign-on.
        """
        self._sessions.pop(session.key, None)
        if not session.cancel_on_disconnect:
            return
        events = self.engine.cancel_session_orders(
            session.key, CancelReason.CANCEL_ON_DISCONNECT,
            "cancelled on disconnect")
        if events:
            log.info("cancel on disconnect: %d order(s) for user %s",
                     len(events), session.target_comp_id)
        self._emit(session, events)

    def on_box_detached(self, box):
        """Discard the box's issued key material -- see :meth:`NseVenue.forget`."""
        if self.venue.forget(box.box_id) is not None:
            log.info("box %d: issued key material discarded on disconnect",
                     box.box_id)

    def on_message(self, session, message):
        self._sessions[session.key] = session
        handler = _HANDLERS.get(int(message.msg_type))
        if handler is None:
            self._order_error(session, message, 16003)
            return None
        return handler(self, session, message)

    def on_invalid(self, session, message, failure):
        """Answer a dialect failure the only way this protocol can.

        There is no Reject here, so the answer is the erroring form of whatever
        transaction was refused. When nothing sensible fits -- an unknown
        transaction code, or a failure before any user signed on -- False lets
        the session drop the connection, which is what the specification does
        with traffic it cannot read.
        """
        code = rules.SESSION_FAILURE_TO_ERROR_CODE.get(
            failure.reason, rules.INVALID_ORDER_PARAM)
        if session is not None and int(message.msg_type or 0) in _HANDLERS:
            self._order_error(session, message, code)
            return True
        log.warning("refusing %s: %s", message.msg_type, failure.text)
        return False

    # -- inbound: order entry ----------------------------------------------

    def _on_new_order(self, session, message):
        symbol, error = self._symbol(message)
        if error is not None:
            return self._order_error(session, message, error)

        error = self._unsupported(message)
        if error is not None:
            return self._order_error(session, message, error)

        try:
            price = self._price(message, D.PRICE)
        except PriceError:
            return self._order_error(session, message,
                                     rules.INVALID_ORDER_PARAM)

        market = self.venue.market_name
        request = NewOrderRequest(
            session_key=session.key,
            market=market,
            cl_ord_id=None,             # NNF gives the client no handle at all
            symbol=symbol,
            side=rules.SIDE_TO_CORE.get(message.get(D.BUY_SELL)),
            quantity=message.get_int(D.VOLUME, 0),
            price=price,
            order_type=rules.order_type(message),
            time_in_force=rules.time_in_force(message),
            capacity=rules.CAPACITY_TO_CORE.get(message.get(D.PRO_CLIENT)),
            account=message.get(D.ACCOUNT_NUMBER),
            received_at=self.clock.now(),
        )
        events = self.engine.new_order(request)
        self._remember(message, events)
        self._emit(session, events)
        return None

    def _on_modify(self, session, message):
        order_id = self._order_number(message)
        try:
            price = self._price(message, D.PRICE)
        except PriceError:
            return self._reject_change(session, message, X.ORDER_MOD_REJECT,
                                       rules.INVALID_ORDER_PARAM)

        error = self._unsupported(message)
        if error is not None:
            return self._reject_change(session, message, X.ORDER_MOD_REJECT,
                                       error)

        request = ReplaceRequest(
            session_key=session.key,
            market=self.venue.market_name,
            cl_ord_id=None,
            orig_cl_ord_id=None,
            order_id=order_id,
            quantity=self._quantity(message, D.VOLUME),
            price=price,
            received_at=self.clock.now(),
        )
        events = self.engine.replace_order(request)
        self._remember(message, events)
        self._emit(session, events)
        return None

    def _on_cancel(self, session, message):
        request = CancelRequest(
            session_key=session.key,
            market=self.venue.market_name,
            cl_ord_id=None,
            orig_cl_ord_id=None,
            order_id=self._order_number(message),
            received_at=self.clock.now(),
        )
        events = self.engine.cancel_order(request)
        self._remember(message, events)
        self._emit(session, events)
        return None

    def _on_sign_off(self, session, message):
        reply = Message.create(str(X.SIGN_OFF_REQUEST_OUT))
        reply.set(D.ERROR_CODE, str(X.NO_ERROR))
        reply.set(D.USER_ID, str(session.user_id))
        reply.set(D.LOG_TIME, str(self._nse_seconds()))
        session.send(reply)
        session.disconnect("signed off at client request")
        return None

    def _on_system_information(self, session, message):
        session.send(self.system_information())
        return None

    def _on_download_request(self, session, message):
        """Refused, with the protocol's own way of saying so.

        The message download is the only recovery NNF has -- there is no
        resend -- and it is deliberately unbuilt, because a MESSAGE_RECORD is
        80 to 512 bytes: the actual message wrapped, with its own inner header,
        inside an outer one. Every structure in this venue is fixed width, and
        the layout engine is built on that being true; a variable-length record
        is a change to the engine rather than a table to transcribe.

        So it is refused rather than answered with an empty download, which
        would tell a client its orders were gone. ERROR_RESPONSE_OUT is the
        protocol's own carrier for a refusal that fits no other message.
        """
        reply = Message.create(str(X.ERROR_RESPONSE_OUT))
        reply.set(D.ERROR_CODE, "16123")            # CANT_COMPLETE_YOUR_REQUEST
        reply.set(D.USER_ID, str(session.user_id))
        reply.set(D.LOG_TIME, str(self._nse_seconds()))
        reply.set(D.ERROR_MESSAGE,
                  "message download is not implemented by this simulator")
        session.send(reply)
        log.info("user %s asked for a message download, which is unbuilt",
                 session.target_comp_id)
        return None

    # -- refusals ----------------------------------------------------------

    def _unsupported(self, message):
        """The error code for an order this venue will not take, or None."""
        book = message.get(D.BOOK_TYPE)
        if book is not None and book != rules.SUPPORTED_BOOK:
            refusal = rules.UNSUPPORTED_BOOKS.get(book)
            if refusal is not None:
                return refusal[0]
            return 16422        # ERR_INVALID_BOOK_TYPE

        for tag, code, _why in rules.UNSUPPORTED_ATTRIBUTES:
            if message.get(tag) == "Y":
                return code

        if message.get_int(D.DISCLOSED_VOL, 0):
            return 16400        # ERR_DQ_EXCEEDS_LIMIT
        if self._price(message, D.TRIGGER_PRICE) is not None:
            return 16423        # ERR_INVALID_TRIGGER_PRICE
        return None

    def _order_error(self, session, message, error_code):
        """ORDER_ERROR: the refusal of an order that never reached the book."""
        return self._reject_change(session, message, X.ORDER_ERROR, error_code)

    def _reject_change(self, session, message, code, error_code):
        reply = Message.create(str(code))
        for tag, value in message.fields:
            if tag not in (D.MESSAGE_LENGTH,):
                reply.set(tag, value)
        reply.set(D.TRANSACTION_CODE, str(code))
        reply.set(D.ERROR_CODE, str(error_code))
        reply.set(D.REASON_CODE, str(error_code))
        reply.set(D.USER_ID, str(session.user_id))
        reply.set(D.LOG_TIME, str(self._nse_seconds()))
        session.send(reply)
        log.info("user %s: %s refused with %s", session.target_comp_id,
                 rules.describe_transaction(int(message.msg_type)),
                 X.error_name(error_code))
        return None

    # -- parsing helpers ---------------------------------------------------

    def _symbol(self, message):
        """The venue's own spelling of the security a message names."""
        symbol = self.venue.resolve_symbol(
            _join(message.get(D.SYMBOL), message.get(D.SERIES)))
        if symbol is None:
            return None, 16012          # ERR_INVALID_SYMBOL
        return symbol, None

    def _price(self, message, tag):
        """A price field's value, or None when it carries nothing.

        Every field of a fixed-width structure travels on every message, so an
        unset price arrives as zero rather than absent. Zero is not a price at
        any venue -- and here it is how the protocol says "no price", which is
        what a market order and an unchanged amendment both need to say.
        """
        text = message.get(tag)
        if text is None:
            return None
        value = self.codec.parse(text)
        return value or None

    def _quantity(self, message, tag):
        """A quantity, or None when it carries nothing.

        Zero for the same reason a price is zero: the field is always there.
        An amendment naming no quantity means "leave it alone", and reading the
        zero literally would amend every order down to nothing.
        """
        value = message.get_int(tag, 0)
        return value or None

    def _order_number(self, message):
        value = message.get(D.ORDER_NUMBER)
        return str(value) if value not in (None, "0") else None

    def _remember(self, message, events):
        """Keep the member's own order reference against the order it names.

        ``NNFField`` is the CTCL audit-trail reference a member puts on an
        order. It is not a handle -- nothing can be cancelled by it -- but a
        client reconciles on it, so it is echoed back on every report about
        that order rather than dropped. Kept per order rather than in one slot,
        because several are in flight at once.
        """
        reference = message.get(D.NNF_FIELD)
        for event in events:
            order = getattr(event, "order", None)
            if order is None:
                continue
            if reference is not None:
                self._references.setdefault(order.order_id, reference)
            if order.status in rules.TERMINAL_STATUSES:
                self._references.pop(order.order_id, None)
            break

    # -- outbound ----------------------------------------------------------

    def _emit(self, session, events):
        for event in events:
            message = self._render(event)
            if message is None:
                continue
            target = self._session_for(event, session)
            if target is None:
                continue
            self._deliver(target, message, getattr(event, "order", None))

    def _session_for(self, event, fallback):
        """The user that owns the order an event concerns.

        A fill touches a resting order that may belong to a different user on a
        different box, so reports route by owner rather than to whoever sent the
        message that triggered them.
        """
        order = getattr(event, "order", None)
        if order is None:
            return fallback
        return self._sessions.get(order.session_key, fallback)

    def _deliver(self, target, message, order):
        """Send a report, honouring any injected drop or delay."""
        behaviour = self.engine.behaviour
        if not behaviour.active or order is None:
            target.send(message)
            return

        if behaviour.take(DROP, order) is not None:
            log.info("dropping %s for order %s (injected behaviour)",
                     message.msg_type, order.order_id)
            return

        rule = behaviour.take(DELAY, order)
        if rule is None or not rule.delay_ms:
            target.send(message)
            return

        delay = rule.delay_ms / 1000.0
        log.info("delaying %s for order %s by %dms (injected behaviour)",
                 message.msg_type, order.order_id, rule.delay_ms)
        self.venue.reactor.call_later(delay, lambda: target.send(message))

    def _render(self, event):
        renderer = _RENDERERS.get(type(event).__name__)
        return renderer(self, event) if renderer else None

    # -- reports -----------------------------------------------------------

    def _base_report(self, order, code):
        """The 290-byte order structure, filled from the order.

        Every report about an order is this structure; only the transaction
        code and a few running totals differ between them.
        """
        message = Message.create(str(code))
        message.set(D.ERROR_CODE, str(X.NO_ERROR))
        message.set(D.LOG_TIME, str(self._nse_seconds()))
        message.set(D.TIMESTAMP, str(self._nse_nanoseconds()))

        symbol, series = _split(order.symbol)
        message.set(D.SYMBOL, symbol)
        message.set(D.SERIES, series)
        message.set(D.ALPHA_CHAR, symbol[:2])

        message.set(D.ORDER_NUMBER, order.order_id)
        message.set(D.BOOK_TYPE, rules.SUPPORTED_BOOK)
        message.set(D.BUY_SELL, rules.SIDE_TO_WIRE.get(order.side,
                                                       D.BuySell.BUY))
        message.set(D.VOLUME, str(order.quantity))
        message.set(D.TOTAL_VOL_REMAINING, str(order.leaves_qty))
        message.set(D.VOLUME_FILLED_TODAY, str(order.cum_qty))
        if order.price is not None:
            message.set(D.PRICE, self.codec.format(order.price))
        message.set(D.ACCOUNT_NUMBER, order.account or "")
        message.set(D.PRO_CLIENT, rules.CAPACITY_TO_WIRE.get(
            order.capacity, D.ProClient.CLIENT))
        message.set(D.ENTRY_DATE_TIME, str(self._nse_seconds()))
        message.set(D.LAST_MODIFIED, str(self._nse_seconds()))
        message.set(D.LAST_ACTIVITY_REFERENCE, str(self._nse_nanoseconds()))
        reference = self._references.get(order.order_id)
        if reference is not None:
            message.set(D.NNF_FIELD, reference)

        session = self._sessions.get(order.session_key)
        if session is not None:
            message.set(D.USER_ID, str(session.user_id))
            message.set(D.TRADER_ID, str(session.user_id))
            message.set(D.BROKER_ID, session.broker_id or "")
            message.set(D.BRANCH_ID, str(session.config.branch_id))

        rules.set_flags(message, order, preopen=self._in_preopen())
        return message

    def _render_accepted(self, event):
        """ORDER_CONFIRMATION, as the market received the order.

        It carries the OrderNumber, and that is the client's only handle on the
        order, which is why it goes out before any fill. And because it does,
        the totals must be **the event's snapshot, not the order's**: the
        acceptance and the fills are produced in one batch, so by render time an
        order that traded on entry would otherwise acknowledge itself as already
        filled. That is the invariant CLAUDE.md states for every venue that
        acknowledges before matching.
        """
        message = self._base_report(event.order, X.ORDER_CONFIRMATION)
        message.set(D.VOLUME, str(event.order_qty))
        message.set(D.TOTAL_VOL_REMAINING, str(event.leaves_qty))
        message.set(D.VOLUME_FILLED_TODAY, str(event.cum_qty))
        message.set(D.FLAG_TRADED, "Y" if event.cum_qty else "N")
        return message

    def _render_rejected(self, event):
        message = self._base_report(event.order, X.ORDER_ERROR)
        code = rules.error_for(event.reason, rules.REJECT_TO_ERROR_CODE)
        message.set(D.ERROR_CODE, str(code))
        message.set(D.REASON_CODE, str(code))
        return message

    def _render_filled(self, event):
        """TRADE_CONFIRMATION, from the snapshot the event carries.

        The running totals are the ones taken at match time, not the order's
        current ones: by the time this renders, a multi-level sweep has moved
        on and the order would report the final state on every fill.
        """
        order = event.order
        message = Message.create(str(X.TRADE_CONFIRMATION))
        message.set(D.ERROR_CODE, str(X.NO_ERROR))
        message.set(D.LOG_TIME, str(self._nse_seconds()))
        message.set(D.TIMESTAMP, str(self._nse_nanoseconds()))

        symbol, series = _split(order.symbol)
        message.set(D.SYMBOL, symbol)
        message.set(D.SERIES, series)
        message.set(D.ALPHA_CHAR, symbol[:2])

        message.set(D.RESPONSE_ORDER_NUMBER, order.order_id)
        message.set(D.BOOK_TYPE, rules.SUPPORTED_BOOK)
        message.set(D.BUY_SELL, rules.SIDE_TO_WIRE.get(order.side,
                                                       D.BuySell.BUY))
        message.set(D.ORIGINAL_VOL, str(event.order_qty))
        message.set(D.REMAINING_VOL, str(event.leaves_qty))
        message.set(D.VOLUME_FILLED_TODAY, str(event.cum_qty))
        message.set(D.FILL_QTY, str(event.quantity))
        message.set(D.FILL_PRICE, self.codec.format(event.price))
        message.set(D.PRICE, self.codec.format(order.price)
                    if order.price is not None else "0")
        message.set(D.FILL_NUMBER, str(event.trade_id))
        message.set(D.ACTIVITY_TYPE, rules.ACTIVITY_TRADE)
        message.set(D.ACTIVITY_TIME, str(self._nse_seconds()))
        message.set(D.ACCOUNT_NUMBER, order.account or "")
        message.set(D.PRO_CLIENT, rules.CAPACITY_TO_WIRE.get(
            order.capacity, D.ProClient.CLIENT))
        message.set(D.LAST_ACTIVITY_REFERENCE, str(self._nse_nanoseconds()))

        session = self._sessions.get(order.session_key)
        if session is not None:
            message.set(D.USER_ID, str(session.user_id))
            message.set(D.TRADER_NUM, str(session.user_id))
            message.set(D.BROKER_ID, session.broker_id or "")

        rules.set_flags(message, order, preopen=self._in_preopen())
        message.set(D.FLAG_TRADED, "Y")
        return message

    def _render_cancelled(self, event):
        message = self._base_report(event.order, X.ORDER_CANCEL_CONFIRMATION)
        message.set(D.MOD_CXL_BY, _cancelled_by(event.reason))
        if event.reason is not CancelReason.USER_REQUEST:
            message.set(D.REASON_CODE, str(_CANCEL_REASON_CODES.get(
                event.reason, rules.INVALID_ORDER_PARAM)))
        return message

    def _render_replaced(self, event):
        message = self._base_report(event.order, X.ORDER_MOD_CONFIRMATION)
        message.set(D.MOD_CXL_BY, D.ModCxlBy.TRADER)
        message.set(D.FLAG_MODIFIED, "Y")
        return message

    def _render_decremented(self, event):
        return self._base_report(event.order, X.ORDER_MOD_CONFIRMATION)

    def _render_cancel_rejected(self, event):
        code = X.ORDER_MOD_REJECT if event.is_replace else X.ORDER_CANCEL_REJECT
        error = rules.error_for(event.reason, rules.CANCEL_REJECT_TO_ERROR_CODE)
        if event.order is not None:
            message = self._base_report(event.order, code)
        else:
            message = Message.create(str(code))
            message.set(D.LOG_TIME, str(self._nse_seconds()))
        message.set(D.ERROR_CODE, str(error))
        message.set(D.REASON_CODE, str(error))
        return message

    # -- unsolicited -------------------------------------------------------

    def system_information(self):
        """SYSTEM_INFORMATION_OUT: the state of every market, and the globals."""
        message = Message.create(str(X.SYSTEM_INFORMATION_OUT))
        message.set(D.ERROR_CODE, str(X.NO_ERROR))
        message.set(D.LOG_TIME, str(self._nse_seconds()))

        market = self.engine.market(self.venue.market_name)
        state = market.state.market_state if market is not None \
            else TradingState.CLOSED
        message.set(D.NORMAL_STATUS,
                    rules.STATE_TO_MARKET_STATUS.get(state,
                                                     D.MarketStatus.CLOSED))
        for tag in (D.ODDLOT_STATUS, D.SPOT_STATUS, D.AUCTION_STATUS,
                    D.CALL_AUCTION1_STATUS, D.CALL_AUCTION2_STATUS):
            message.set(tag, D.MarketStatus.CLOSED)

        message.set(D.BOARD_LOT_QUANTITY, "1")
        message.set(D.TICK_SIZE, self.codec.format(self.venue.tick_size()))
        message.set(D.MAXIMUM_GTC_DAYS, "0")
        message.set(D.DISCLOSED_QUANTITY_PERCENT, "0")
        for tag in (D.SECURITY_AON, D.SECURITY_MIN_FILL,
                    D.SECURITY_BOOKS_MERGED):
            message.set(tag, "N")
        return message

    def emit_auction(self, events):
        """Report an auction's fills to whoever owns each order.

        There is no sender to fall back on -- nobody's message caused this --
        so an event whose order has no signed-on owner is simply not reported,
        which is what a client that has gone away would get anyway.
        """
        self._emit(None, events)

    def broadcast_state(self, state):
        """Tell every signed-on user that the market moved."""
        message = self.system_information()
        for session in list(self._sessions.values()):
            session.send(message)

    # -- time --------------------------------------------------------------

    def _nse_seconds(self):
        """Seconds since 1 January 1980, which is what every time field holds."""
        return WT.to_nse_seconds(_epoch_seconds(self.clock.now()))

    def _nse_nanoseconds(self):
        return WT.to_nse_nanoseconds(_epoch_seconds(self.clock.now()))

    def _in_preopen(self):
        market = self.engine.market(self.venue.market_name)
        if market is None:
            return False
        return market.state.market_state in (TradingState.PRE_OPEN,
                                             TradingState.OPENING_AUCTION)


# -- helpers -----------------------------------------------------------------

def _epoch_seconds(when):
    import calendar
    return calendar.timegm(when.timetuple()) + when.microsecond / 1000000.0


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _join(symbol, series):
    if not symbol:
        return None
    return "%s-%s" % (symbol, series) if series else symbol


def _split(symbol):
    """``INFY-EQ`` back into the two fields SEC_INFO carries."""
    name, _, series = (symbol or "").partition("-")
    return name, series


def _cancelled_by(reason):
    if reason == CancelReason.USER_REQUEST:
        return D.ModCxlBy.TRADER
    return D.ModCxlBy.EXCHANGE


_CANCEL_REASON_CODES = {
    CancelReason.IOC_REMAINDER: 16388,          # ERR_FOK_ORDER_CANCELLED
    CancelReason.MARKET_REMAINDER: 16388,
    CancelReason.FOK_UNFILLED: 16388,
    CancelReason.MARKET_CLOSED: 16000,          # ERR_MARKET_NOT_OPEN
    CancelReason.CANCEL_ON_DISCONNECT: 16404,   # ERR_ADMIN_SUSP_CANCELLED
    CancelReason.ADMINISTRATIVE: 16404,
    CancelReason.MASS_CANCEL: 16404,
}


_BOX_HANDLERS = {
    X.SECURE_BOX_REGISTRATION_REQUEST_IN: NseApplication._on_registration,
    X.BOX_SIGN_ON_REQUEST_IN: NseApplication._on_box_sign_on,
    X.SIGN_ON_REQUEST_IN: NseApplication._on_sign_on,
}

_HANDLERS = {
    X.BOARD_LOT_IN: NseApplication._on_new_order,
    X.ORDER_MOD_IN: NseApplication._on_modify,
    X.ORDER_CANCEL_IN: NseApplication._on_cancel,
    X.SIGN_OFF_REQUEST_IN: NseApplication._on_sign_off,
    X.SYSTEM_INFORMATION_IN: NseApplication._on_system_information,
    X.DOWNLOAD_REQUEST: NseApplication._on_download_request,
}

_RENDERERS = {
    "OrderAccepted": NseApplication._render_accepted,
    "OrderRejected": NseApplication._render_rejected,
    "OrderFilled": NseApplication._render_filled,
    "OrderCancelled": NseApplication._render_cancelled,
    "OrderReplaced": NseApplication._render_replaced,
    "OrderDecremented": NseApplication._render_decremented,
    "CancelRejected": NseApplication._render_cancel_rejected,
}

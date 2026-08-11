"""Helpers for exercising the matching core directly."""

from exchangesim.core.book import OrderBook
from exchangesim.core.clock import FixedClock
from exchangesim.core.commands import (
    CancelRequest,
    NewOrderRequest,
    ReplaceRequest,
)
from exchangesim.core.engine import Engine
from exchangesim.core.enums import (
    OrderType,
    Side,
    StpMode,
    TimeInForce,
    TradingState,
)
from exchangesim.core.ids import IdGenerator, SequenceGenerator
from exchangesim.core.instrument import BandTable, Instrument, TickTable
from exchangesim.core.market import Market
from exchangesim.core.matching import MatchingEngine
from exchangesim.core.orders import Order
from exchangesim.core.prices import PriceCodec
from exchangesim.core.validation import StandardValidator, ValidationLimits

#: Japannext equities carry one decimal place, so a price unit is 0.1 JPY.
CODEC = PriceCodec(decimals=1)


class OrderFactory(object):
    """Builds orders with unique identifiers and increasing arrival sequence."""

    def __init__(self, codec=CODEC, symbol="7203", market="DAY"):
        self.codec = codec
        self.symbol = symbol
        self.market = market
        self.ids = IdGenerator("O", width=6)
        self.sequences = SequenceGenerator()

    def __call__(self, side, quantity, price=None, tif=TimeInForce.DAY,
                 mpid=None, min_qty=0, exec_inst=(), session="S1",
                 cl_ord_id=None, symbol=None, order_type=None, stp_id=None,
                 stp_instruction=None):
        order_id = self.ids.next()
        return Order(
            order_id=order_id,
            cl_ord_id=cl_ord_id or ("C" + order_id),
            session_key=session,
            symbol=symbol or self.symbol,
            market=self.market,
            side=side,
            quantity=quantity,
            price=self.codec.parse(price) if price is not None else None,
            time_in_force=tif,
            min_qty=min_qty,
            exec_inst=exec_inst,
            mpid=mpid,
            order_type=order_type or OrderType.LIMIT,
            stp_id=stp_id,
            stp_instruction=stp_instruction,
            sequence=self.sequences.next(),
        )

    def buy(self, quantity, price=None, **kwargs):
        return self(Side.BUY, quantity, price, **kwargs)

    def sell(self, quantity, price=None, **kwargs):
        return self(Side.SELL, quantity, price, **kwargs)


def make_engine(stp_mode=StpMode.NONE, codec=CODEC):
    return MatchingEngine(codec, IdGenerator("T", width=6), stp_mode=stp_mode)


def make_book(symbol="7203", market="DAY"):
    return OrderBook(symbol, market)


def px(value):
    """Price text to internal integer units."""
    return CODEC.parse(value)


def events_of(events, kind):
    return [event for event in events if type(event).__name__ == kind]


def fills_for(events, order):
    """OrderFilled events belonging to a particular order."""
    return [event for event in events
            if type(event).__name__ == "OrderFilled"
            and event.order is order]


def ladder(book, side="bids", levels=5):
    """Book side as ``[(price_text, quantity), ...]``, best first."""
    depth = book.depth(levels)[side]
    return [(CODEC.format(level["price"]), level["quantity"]) for level in depth]


# -- full engine stack -------------------------------------------------------

def band_table():
    """A simplified Japannext-style price band ladder."""
    return BandTable([
        (CODEC.parse("0"), CODEC.parse("30")),
        (CODEC.parse("100"), CODEC.parse("50")),
        (CODEC.parse("200"), CODEC.parse("80")),
        (CODEC.parse("1000"), CODEC.parse("300")),
        (CODEC.parse("3000"), CODEC.parse("700")),
    ])


def tick_table():
    """0.1 below 3,000 and 0.5 above, matching J-Market for TOPIX 100 names."""
    return TickTable([
        (CODEC.parse("0"), {None: CODEC.parse("0.1")}),
        (CODEC.parse("3000"), {None: CODEC.parse("0.5")}),
    ])


def make_instruments():
    bands, ticks = band_table(), tick_table()
    return {
        "7203": Instrument("7203", "Toyota", lot_size=100,
                           shares_outstanding=1000000,
                           base_price=CODEC.parse("2845.5"), tier="TOPIX100",
                           band_table=bands, tick_table=ticks),
        # No shares outstanding, so no 5% quantity cap -- used to exercise the
        # order-value limit in isolation.
        "9984": Instrument("9984", "SoftBank", lot_size=100,
                           base_price=CODEC.parse("150"), tier="TOPIX100",
                           band_table=bands, tick_table=ticks),
        # Cheap enough that the 5% quantity cap binds before the value cap.
        "1301": Instrument("1301", "Kyokuyo", lot_size=100,
                           shares_outstanding=1000000,
                           base_price=CODEC.parse("100"), tier="OTHER",
                           band_table=bands, tick_table=ticks),
        "0000": Instrument("0000", "Suspended", lot_size=100,
                           band_table=bands, tick_table=ticks, tradable=False),
    }


class EngineHarness(object):
    """A complete venue-shaped stack: instruments, markets and an engine."""

    def __init__(self, markets=("DAY", "NGHT"), state=TradingState.OPEN,
                 stp_mode=StpMode.NONE, limits=None, publisher=None,
                 tape_length=None):
        self.clock = FixedClock()
        self.codec = CODEC
        self.instruments = make_instruments()
        self.trade_ids = IdGenerator("T", width=6)
        self.markets = {}
        for name in markets:
            self.markets[name] = Market(
                name, CODEC, self.trade_ids, self.clock, stp_mode, state,
                publisher=publisher, tape_length=tape_length)
        self.validator = StandardValidator(
            CODEC, limits or ValidationLimits(max_order_value=100000000))
        self.engine = Engine(
            CODEC, self.instruments, self.markets, self.validator,
            IdGenerator("O", width=9), SequenceGenerator(), self.clock)

    # -- convenience --------------------------------------------------------

    def market(self, name="DAY"):
        return self.markets[name]

    def book(self, symbol="7203", market="DAY"):
        return self.markets[market].book(symbol)

    def new_order(self, cl_ord_id, side, quantity, price=None, session="S1",
                  market="DAY", symbol="7203", **kwargs):
        return self.engine.new_order(NewOrderRequest(
            session_key=session, market=market, cl_ord_id=cl_ord_id,
            symbol=symbol, side=side, quantity=quantity,
            price=CODEC.parse(price) if price is not None else None,
            **kwargs))

    def buy(self, cl_ord_id, quantity, price=None, **kwargs):
        return self.new_order(cl_ord_id, Side.BUY, quantity, price, **kwargs)

    def sell(self, cl_ord_id, quantity, price=None, **kwargs):
        return self.new_order(cl_ord_id, Side.SELL, quantity, price, **kwargs)

    def cancel(self, cl_ord_id, orig_cl_ord_id, session="S1", market="DAY",
               **kwargs):
        return self.engine.cancel_order(CancelRequest(
            session_key=session, market=market, cl_ord_id=cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id, **kwargs))

    def replace(self, cl_ord_id, orig_cl_ord_id, quantity=None, price=None,
                session="S1", market="DAY", **kwargs):
        return self.engine.replace_order(ReplaceRequest(
            session_key=session, market=market, cl_ord_id=cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id, quantity=quantity,
            price=CODEC.parse(price) if price is not None else None,
            **kwargs))

    def order(self, cl_ord_id, session="S1"):
        return self.engine.registry.by_cl_ord_id(session, cl_ord_id)

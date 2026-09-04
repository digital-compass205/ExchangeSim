"""NSE Capital Market venue wiring.

One market -- the Normal market, which is the only one whose Regular Lot book
this venue trades -- and **two** listeners, which is what makes this venue
unlike the other two.

The gateway listener is where members trade. The Gateway Router listener is a
separate port, TLS where the platform allows it, serving only ``GR_REQUEST`` and
``GR_RESPONSE``: a member dials it first, collects a key, an IV and an
additional key, and only then opens the gateway connection those secrets
encrypt. The two are joined by the Box ID, and by :meth:`NseVenue.cipher_for`,
which is the whole of the venue's part in the encryption.

Order numbers are the venue's own. NSE assigns a ``DOUBLE`` per order and gives
the client no other handle, so :class:`OrderNumberGenerator` produces plain
decimal strings and the core's ``Order.order_id`` *is* the OrderNumber -- no
mapping table, and the number a client cancels by is the number the audit shows.
"""

import itertools
import logging
import os

from ...core.config import ConfigError
from ...core.engine import Engine
from ...core.enums import StpMode, TradingState
from ...core.ids import IdGenerator, SequenceGenerator
from ...core.instrument import (
    Instrument,
    ReferenceDataError,
    load_band_table,
    load_instruments,
    load_tick_table,
    read_rows,
)
from ...core.market import Market
from ...core.prices import PriceCodec
from ...core.validation import StandardValidator
from ...nnf import crypto
from ...nnf.codec import NnfCodec
from ...nnf.session import NnfSessionConfig, NnfSessionManager
from ...wire.acceptor import Acceptor
from ..base import Venue
from . import commands as venue_commands
from . import dictionary as D
from . import layouts as LY
from . import rules
from .auctions import PREOPEN_RULES, PreOpenSession
from .gateway_router import GatewayRouter
from .handlers import NseApplication

log = logging.getLogger(__name__)

#: The Normal market, and the only one this venue runs. NSE's other market
#: types -- Odd Lot, Spot, Auction, Call Auction 2 -- are refused rather than
#: simulated, so no book is created for them.
NORMAL_MARKET = "NORMAL"


class OrderNumberGenerator(object):
    """The exchange-assigned order number, as a plain decimal string.

    NSE does not publish how it composes one, so this is simply monotonic from
    a configurable base -- unique, increasing, and comfortably inside the
    ``DOUBLE`` the wire carries, which is everything a client can depend on.
    Recorded as an ASSUMPTION.

    Strings rather than integers, because ``Order.order_id`` is a string
    everywhere else and the audit, the board and the control plane all render
    it as one -- the same call this project already made for HKEX's numeric
    stock codes.
    """

    __slots__ = ("_counter",)

    def __init__(self, start=1):
        self._counter = itertools.count(int(start))

    def next(self):
        return str(next(self._counter))

    def peek(self):
        return self._counter.__reduce__()[1][0]


class NseVenue(Venue):
    """NSE India Capital Market over the NNF Trimmed Protocol."""

    key = "nse"

    def __init__(self, config, reactor, publisher):
        Venue.__init__(self, config, reactor, publisher)
        self.codec = PriceCodec(D.PRICE_DECIMALS)
        self.dictionary = D.build_cm()
        self.layouts = LY.build_cm()
        self.codecs = {"nnf": NnfCodec(self.layouts)}

        session_date = config.get("session_date", "")
        self.order_ids = OrderNumberGenerator(
            config.get("first_order_number", 1))
        self.trade_ids = IdGenerator("", session_date, width=9, max_length=20)
        self.sequences = SequenceGenerator()

        self.instruments = {}
        self.markets = {}
        self.engine = None
        self.application = None
        self.manager = None
        self.acceptor = None
        self.router = None
        self.market_name = NORMAL_MARKET
        self.preopen = None
        self._band_table = None
        self._tick_table = None
        self._bands = {}                # symbol -> its own band, in per cent
        self._boxes = {}                # box id -> its configured brokers
        self._issued = {}               # box id -> what the Gateway Router gave

    # -- lifecycle ---------------------------------------------------------

    def setup(self):
        self._load_reference_data()
        self._build_market()
        self._build_engine()
        self._build_sessions()
        self._build_listeners()

    def teardown(self):
        if self.acceptor is not None:
            self.acceptor.stop()
        if self.router is not None:
            self.router.stop()
        if self.manager is not None:
            self.manager.close()

    def register_commands(self, registry):
        venue_commands.register(registry, self)

    # -- reference data ----------------------------------------------------

    def _read_reference_data(self):
        """Read every table from disk.

        Deliberately pure: it either returns a complete set or raises, so a
        typo in one CSV during a live reload cannot leave the venue holding
        half of the new reference data and half of the old.
        """
        default_dir = os.path.join(os.path.dirname(__file__), "reference")

        def resolve(key, default_name):
            path = self.config.resolve_path("reference.%s" % key)
            return path or os.path.join(default_dir, default_name)

        securities = resolve("securities", "securities.csv")
        try:
            bands = load_band_table(resolve("price_bands", "price_bands.csv"),
                                    self.codec, "NSE daily price range")
            ticks = load_tick_table(resolve("tick_sizes", "tick_sizes.csv"),
                                    self.codec, "NSE tick")
            instruments = load_instruments(securities, self.codec, bands, ticks)
            per_security = {}
            for row in read_rows(securities, ("symbol",)):
                symbol = row["symbol"].strip()
                band = (row.get("band") or "").strip()
                if not band:
                    continue
                if int(band) not in rules.PERMITTED_BANDS:
                    raise ValueError(
                        "%s names a %s%% price band; NSE assigns %s"
                        % (symbol, band,
                           ", ".join(str(p) for p in rules.PERMITTED_BANDS)))
                per_security[symbol] = rules.CircuitFilter(int(band))
            # A security's own circuit filter displaces the fallback table, so
            # the validator sees a percentage of the base price rather than the
            # absolute offset a BandTable holds.
            for symbol, instrument in instruments.items():
                if symbol in per_security:
                    instrument.band_table = per_security[symbol]
        except (ReferenceDataError, ValueError) as exc:
            raise ConfigError("NSE reference data: %s" % exc)
        return bands, ticks, instruments, per_security

    def _load_reference_data(self):
        bands, ticks, instruments, per_security = self._read_reference_data()
        self._band_table = bands
        self._tick_table = ticks
        self._bands = per_security
        self.instruments.clear()
        self.instruments.update(instruments)
        log.info("venue '%s' loaded %d instruments", self.name,
                 len(self.instruments))

    def reload_reference_data(self):
        """Re-read the CSVs, mutating the universe the engine already holds."""
        bands, ticks, loaded, per_security = self._read_reference_data()

        before = {symbol: instrument.describe(self.codec)
                  for symbol, instrument in self.instruments.items()}

        self._band_table = bands
        self._tick_table = ticks
        self._bands = per_security

        added, updated, withdrawn = [], [], []
        for symbol in sorted(loaded):
            instrument = loaded[symbol]
            known = symbol in self.instruments
            self.instruments[symbol] = instrument
            if not known:
                for market in self.markets.values():
                    market.book(symbol)
                added.append(symbol)
            elif instrument.describe(self.codec) != before[symbol]:
                updated.append(symbol)

        for symbol in sorted(before):
            if symbol in loaded:
                continue
            instrument = self.instruments[symbol]
            instrument.band_table = per_security.get(symbol, bands)
            instrument.tick_table = ticks
            if instrument.tradable:
                instrument.tradable = False
                withdrawn.append(symbol)

        log.info("venue '%s' reference reload: %d added, %d updated, "
                 "%d withdrawn, %d total", self.name, len(added), len(updated),
                 len(withdrawn), len(self.instruments))
        return {"added": added, "updated": updated, "withdrawn": withdrawn,
                "instruments": len(self.instruments)}

    def create_instrument(self, symbol, name="", lot_size=1, base_price=None,
                          tier=None, shares_outstanding=0, tradable=True):
        return Instrument(
            symbol=symbol, name=name, lot_size=lot_size,
            shares_outstanding=shares_outstanding, base_price=base_price,
            tier=tier, band_table=self._band_table,
            tick_table=self._tick_table, tradable=tradable)

    def books_for(self, instrument):
        """One market, so one book. Stated rather than inherited: the base
        class would answer with every market, which is the same answer here
        only because there happens to be one."""
        return [self.markets[self.market_name]]

    def resolve_symbol(self, value):
        """This venue's own spelling of a security a client named.

        A security is ``SYMBOL-SERIES`` here, because neither half names one on
        its own: ``INFY-EQ`` and ``INFY-BE`` are different books. A bare symbol
        resolves when exactly one series is listed for it, which is the common
        case and the one a person types.
        """
        if not value:
            return None
        text = str(value).strip().upper()
        if text in self.instruments:
            return text
        if "-" in text:
            return None
        matches = [symbol for symbol in self.instruments
                   if symbol.split("-")[0] == text]
        return matches[0] if len(matches) == 1 else None

    def tick_size(self, price=None):
        """The tick at ``price``, or at the smallest price when none is given."""
        return self._tick_table.tick_for(price if price is not None else 0)

    def band_for(self, symbol):
        """The security's own circuit filter, or None if it uses the fallback."""
        return self._bands.get(symbol)

    # -- market ------------------------------------------------------------

    def _build_market(self):
        configured = self.config.get("markets") or [
            {"name": NORMAL_MARKET, "description": "Normal Market"}]
        if len(configured) != 1:
            raise ConfigError(
                "NSE runs one market, the Normal market; %d were configured"
                % len(configured))

        entry = configured[0]
        self.market_name = entry.get("name") or NORMAL_MARKET
        state = str(entry.get("state",
                              self.config.get("initial_state",
                                              TradingState.CLOSED))).upper()
        if state not in TradingState.ALL:
            raise ConfigError("market '%s' has unknown state '%s'"
                              % (self.market_name, state))

        # NNF Capital Market has no self-trade prevention of any kind, and the
        # core's per-market mode keys on `mpid`, which this venue uses for the
        # participant code. Allowing a mode would silently cancel orders on a
        # key that means something else entirely.
        stp_mode = str(entry.get("stp_mode", StpMode.NONE)).upper()
        if stp_mode != StpMode.NONE:
            raise ConfigError(
                "NSE has no self-trade prevention; market '%s' asks for '%s'"
                % (self.market_name, stp_mode))

        self.markets[self.market_name] = Market(
            name=self.market_name,
            codec=self.codec,
            trade_ids=self.trade_ids,
            clock=self.clock,
            stp_mode=StpMode.NONE,
            initial_state=state,
            publisher=self.publisher,
            tape_length=self.config.get("tape_length", 500),
            description=entry.get("description", "Normal Market"),
            ack_on_entry=rules.ACK_BEFORE_EXECUTION,
            auction_rules=PREOPEN_RULES)
        self.preopen = PreOpenSession(self)

        for symbol in self.instruments:
            self.markets[self.market_name].book(symbol)
        log.info("market '%s' created: state=%s", self.market_name, state)

    def set_trading_state(self, state, market=None, symbol=None):
        """Move the market, uncrossing the pre-open on the way out of it.

        The uncrossing happens **before** the state change is applied, for the
        reason CLAUDE.md gives: moving to a closed state expires resting orders,
        and an auction run afterwards would find an empty book.
        """
        target = self.markets.get(market or self.market_name)
        if target is None:
            raise KeyError(market)

        events = []
        if self._leaving_preopen(target, state, symbol):
            _results, events = self.preopen.uncross()
            self.application.emit_auction(events)

        changed = target.set_state(state, symbol=symbol)
        if state == TradingState.PRE_OPEN and symbol is None:
            self.preopen.open()
        if self.application is not None:
            self.application.broadcast_state(state)
        return changed

    def _leaving_preopen(self, market, state, symbol):
        """Whether this transition is the one that executes the auction."""
        if symbol is not None or self.preopen is None:
            return False
        current = market.state.market_state
        return (current in (TradingState.PRE_OPEN, TradingState.OPENING_AUCTION)
                and state == TradingState.OPEN)

    # -- engine ------------------------------------------------------------

    def _build_engine(self):
        limits = rules.default_limits(
            max_order_value=self.config.get("limits.max_order_value"),
            max_quantity=self.config.get("limits.max_quantity"))
        self.engine = Engine(
            codec=self.codec,
            instruments=self.instruments,
            markets=self.markets,
            validator=StandardValidator(self.codec, limits),
            order_ids=self.order_ids,
            sequences=self.sequences,
            clock=self.clock)

    # -- sessions ----------------------------------------------------------

    def _build_sessions(self):
        nnf = self.config.section("nnf")
        self.application = NseApplication(self)
        self.manager = NnfSessionManager(
            self.clock, self.dictionary, self.layouts, audit=self.audit,
            heartbeat_seconds=nnf.get("heartbeat_interval", 30),
            drop_counter_limit=nnf.get("heartbeat_drop_limit", 10),
            user_id_tag=D.USER_ID, box_id_tag=D.BOX_ID,
            heartbeat_code=int(self.layouts.layout("23506").msg_type))
        self.manager.on_checksum_failure = self.application.on_checksum_failure

        boxes = nnf.get("boxes") or []
        if not boxes:
            log.warning("venue '%s' has no NNF boxes configured", self.name)

        for entry in boxes:
            box_id = entry.get("box_id")
            if box_id is None:
                raise ConfigError("each entry of 'nnf.boxes' needs a 'box_id'")
            brokers = entry.get("broker_ids") or []
            if not brokers:
                raise ConfigError(
                    "box %s needs at least one entry in 'broker_ids'" % box_id)
            self._boxes[int(box_id)] = [str(broker) for broker in brokers]
            self.manager.add_box(int(box_id), self.application)

            for user in entry.get("users") or []:
                self._add_user(int(box_id), brokers, user)

    def _add_user(self, box_id, brokers, entry):
        user_id = entry.get("user_id")
        if user_id is None:
            raise ConfigError("each user of box %d needs a 'user_id'" % box_id)
        broker_id = str(entry.get("broker_id", brokers[0]))
        if broker_id not in [str(broker) for broker in brokers]:
            raise ConfigError(
                "user %s names broker '%s', which box %d is not registered to"
                % (user_id, broker_id, box_id))
        markets = entry.get("markets") or [self.market_name]
        unknown = [name for name in markets if name not in self.markets]
        if unknown:
            raise ConfigError("user %s names unknown market(s): %s"
                              % (user_id, ", ".join(unknown)))
        self.manager.add_user(NnfSessionConfig(
            user_id=int(user_id),
            box_id=box_id,
            broker_id=broker_id,
            branch_id=entry.get("branch_id", 1),
            password=entry.get("password"),
            user_type=entry.get("user_type"),
            trader_name=entry.get("trader_name"),
            broker_name=entry.get("broker_name"),
            markets=markets,
            cancel_on_disconnect=bool(entry.get("cancel_on_disconnect", False))))

    # -- listeners ---------------------------------------------------------

    def _build_listeners(self):
        nnf = self.config.section("nnf")
        host = nnf.get("host", "127.0.0.1")
        port = nnf.get("port", 9021)

        self.acceptor = Acceptor(self.reactor, self.manager,
                                 codec=NnfCodec(self.layouts),
                                 dictionary=self.dictionary)
        self.acceptor.start(host, port)

        router = self.config.section("gateway_router")
        if router.get("enabled", True):
            self.router = GatewayRouter(self, router)
            self.router.start(router.get("host", host),
                              router.get("port", port + 1))

    # -- encryption --------------------------------------------------------

    @property
    def requires_encryption(self):
        """Whether a box must have collected a key before it may register.

        False by default, so a client can be pointed at the gateway alone while
        it is being brought up. Turn it on to hold a member to the whole
        Chapter 10 sequence.
        """
        return bool(self.config.get("nnf.require_encryption", False))

    def issue(self, box_id, secrets):
        """Record what the Gateway Router just handed to a box."""
        self._issued[int(box_id)] = secrets

    def issued(self, box_id):
        return self._issued.get(int(box_id))

    def session_key_for(self, box_id):
        secrets = self.issued(box_id)
        return secrets.session_key if secrets is not None else None

    def cipher_for(self, box_id):
        """The exchange half of this box's cipher, or None for a clear channel."""
        secrets = self.issued(box_id)
        return secrets.exchange_cipher() if secrets is not None else None

    def brokers_of(self, box_id):
        return self._boxes.get(int(box_id), [])

    # -- description -------------------------------------------------------

    def describe(self):
        summary = Venue.describe(self)
        summary.update({
            "market": self.market_name,
            "instruments": len(self.instruments),
            "preopen_locked": bool(self.preopen and self.preopen.locked),
            "boxes": self.manager.describe_boxes() if self.manager else [],
            "gateway": list(self.acceptor.address[:2]) if self.acceptor else None,
            "gateway_router": (list(self.router.address[:2])
                               if self.router and self.router.address else None),
            "encryption_required": self.requires_encryption,
        })
        return summary

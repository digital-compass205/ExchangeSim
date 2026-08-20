"""Japannext venue wiring.

Builds the four markets, loads reference data, constructs the engine and the
FIX acceptor, and exposes the venue over the control plane.

The four markets correspond to the SubID values in the specification:
``DAY`` (J-Market Daytime), ``NGHT`` (J-Market Nighttime), ``DAYX`` (X-Market)
and ``DAYU`` (U-Market). They share the instrument universe but have separate
books, separate trading states and, per the trading rules appendices, their own
price-band and tick-size tables.
"""

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
)
from ...core.market import Market
from ...core.prices import PriceCodec
from ...core.validation import StandardValidator
from ...fix.acceptor import Acceptor, SessionManager
from ...fix.codec import FixCodec
from ...fix.session import SessionConfig
from ..base import Venue
from . import commands as venue_commands
from . import dictionary as D
from . import rules
from .handlers import JapannextApplication

log = logging.getLogger(__name__)

#: Japannext equity prices carry one decimal place.
PRICE_DECIMALS = 1

DEFAULT_MARKETS = [
    {"name": D.SubID.J_MARKET_DAY, "description": D.SubID.LABELS[D.SubID.J_MARKET_DAY]},
    {"name": D.SubID.J_MARKET_NIGHT,
     "description": D.SubID.LABELS[D.SubID.J_MARKET_NIGHT]},
    {"name": D.SubID.X_MARKET, "description": D.SubID.LABELS[D.SubID.X_MARKET]},
    {"name": D.SubID.U_MARKET, "description": D.SubID.LABELS[D.SubID.U_MARKET]},
]


class JapannextVenue(Venue):
    """Japannext PTS equities over FIX 4.2."""

    key = "japannext"

    def __init__(self, config, reactor, publisher):
        Venue.__init__(self, config, reactor, publisher)
        self.codec = PriceCodec(PRICE_DECIMALS)
        self.dictionary = D.build()

        session_date = config.get("session_date", "")
        self.order_ids = IdGenerator("O", session_date, width=9)
        self.exec_ids = IdGenerator("E", session_date, width=9)
        self.trade_ids = IdGenerator("M", session_date, width=9)
        self.sequences = SequenceGenerator()

        self.instruments = {}
        self.markets = {}
        self.engine = None
        self.application = None
        self.manager = None
        self.acceptor = None
        self._band_tables = {}
        self._tick_tables = {}

    # -- lifecycle ---------------------------------------------------------

    def setup(self):
        self._load_reference_data()
        self._build_markets()
        self._build_engine()
        self._build_fix()

    def teardown(self):
        if self.acceptor is not None:
            self.acceptor.stop()
        if self.manager is not None:
            self.manager.close()

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
            if path:
                return path
            return os.path.join(default_dir, default_name)

        try:
            bands = {
                "default": load_band_table(
                    resolve("price_bands", "price_bands_j.csv"), self.codec,
                    "J/X-Market price band"),
                D.SubID.U_MARKET: load_band_table(
                    resolve("price_bands_u", "price_bands_u.csv"), self.codec,
                    "U-Market price band"),
            }
            ticks = {
                "default": load_tick_table(
                    resolve("tick_sizes", "tick_sizes_j.csv"), self.codec,
                    "J-Market tick size"),
                D.SubID.X_MARKET: load_tick_table(
                    resolve("tick_sizes_x", "tick_sizes_x.csv"), self.codec,
                    "X-Market tick size"),
                D.SubID.U_MARKET: load_tick_table(
                    resolve("tick_sizes_u", "tick_sizes_u.csv"), self.codec,
                    "U-Market tick size"),
            }
            instruments = load_instruments(
                resolve("symbols", "symbols.csv"), self.codec,
                bands["default"], ticks["default"])
        except ReferenceDataError as exc:
            raise ConfigError("Japannext reference data: %s" % exc)

        return bands, ticks, instruments

    def _load_reference_data(self):
        bands, ticks, instruments = self._read_reference_data()
        self._band_tables = bands
        self._tick_tables = ticks
        self.instruments = instruments

        log.info("loaded %d instruments for venue '%s'",
                 len(self.instruments), self.name)

    def reload_reference_data(self):
        """Re-read the CSVs and fold them into the running venue.

        The instrument map is mutated **in place** because the engine was
        handed the same dict at construction (``Engine.__init__``); rebinding
        it here would leave the engine looking at the old universe. Books,
        resting orders and statistics all survive.

        The CSV is taken as authoritative for any symbol it contains, so a
        runtime ``instrument.set`` override on such a symbol is discarded --
        that is what "reload from disk" should mean. A symbol that has
        *disappeared* from the CSV is marked non-tradable rather than deleted,
        because live orders may still reference it; use ``instrument.remove``
        to be rid of one entirely.
        """
        bands, ticks, loaded = self._read_reference_data()

        before = {symbol: instrument.describe(self.codec)
                  for symbol, instrument in self.instruments.items()}

        self._band_tables = bands
        self._tick_tables = ticks

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
            # Retained, but re-pointed at the newly loaded tables so it does
            # not keep a private reference to the superseded ones.
            instrument = self.instruments[symbol]
            instrument.band_table = bands["default"]
            instrument.tick_table = ticks["default"]
            if instrument.tradable:
                instrument.tradable = False
                withdrawn.append(symbol)

        log.info("venue '%s' reference reload: %d added, %d updated, "
                 "%d withdrawn, %d total", self.name, len(added), len(updated),
                 len(withdrawn), len(self.instruments))

        return {"added": added, "updated": updated, "withdrawn": withdrawn,
                "instruments": len(self.instruments)}

    def create_instrument(self, symbol, name="", lot_size=100, base_price=None,
                          tier=None, shares_outstanding=0, tradable=True):
        return Instrument(
            symbol=symbol,
            name=name,
            lot_size=lot_size,
            shares_outstanding=shares_outstanding,
            base_price=base_price,
            tier=tier,
            band_table=self._band_tables["default"],
            tick_table=self._tick_tables["default"],
            tradable=tradable)

    def band_table(self, market):
        return self._band_tables.get(market, self._band_tables["default"])

    def tick_table(self, market):
        return self._tick_tables.get(market, self._tick_tables["default"])

    # -- markets -----------------------------------------------------------

    def _build_markets(self):
        configured = self.config.get("markets") or DEFAULT_MARKETS
        default_state = self.config.get("initial_state", TradingState.CLOSED)
        default_stp = self.config.get("stp_mode", StpMode.CANCEL_NEWEST)

        for entry in configured:
            name = entry.get("name")
            if not name:
                raise ConfigError("each entry of 'markets' needs a 'name'")

            state = str(entry.get("state", default_state)).upper()
            if state not in TradingState.ALL:
                raise ConfigError(
                    "market '%s' has unknown state '%s'" % (name, state))

            stp_mode = str(entry.get("stp_mode", default_stp)).upper()
            if stp_mode not in StpMode.ALL:
                raise ConfigError(
                    "market '%s' has unknown stp_mode '%s'" % (name, stp_mode))

            self.markets[name] = Market(
                name=name,
                codec=self.codec,
                trade_ids=self.trade_ids,
                clock=self.clock,
                stp_mode=stp_mode,
                initial_state=state,
                publisher=self.publisher,
                tape_length=self.config.get("tape_length", 500),
                description=entry.get(
                    "description", D.SubID.LABELS.get(name, name)))

            # Create every instrument's book up front so market-data summaries
            # list the whole universe, not only names that have traded.
            for symbol in self.instruments:
                self.markets[name].book(symbol)

            log.info("market '%s' created: state=%s stp=%s", name, state, stp_mode)

    # -- engine ------------------------------------------------------------

    def _build_engine(self):
        limits = rules.default_limits(
            max_order_value=self.config.get("limits.max_order_value", 100000000))
        validator = StandardValidator(self.codec, limits)
        self.engine = Engine(
            codec=self.codec,
            instruments=self.instruments,
            markets=self.markets,
            validator=validator,
            order_ids=self.order_ids,
            sequences=self.sequences,
            clock=self.clock)

    # -- FIX ---------------------------------------------------------------

    def _build_fix(self):
        fix = self.config.section("fix")
        sender_comp_id = fix.get("sender_comp_id", "JNXSIM")
        store_root = self.config.resolve_path("fix.store")

        self.application = JapannextApplication(self)
        self.manager = SessionManager(self.clock, self.dictionary, store_root,
                                      audit=self.audit)

        sessions = fix.get("sessions") or []
        if not sessions:
            log.warning("venue '%s' has no FIX sessions configured", self.name)

        for entry in sessions:
            target = entry.get("target_comp_id")
            if not target:
                raise ConfigError(
                    "each entry of 'fix.sessions' needs a 'target_comp_id'")

            markets = entry.get("markets") or sorted(self.markets)
            unknown = [name for name in markets if name not in self.markets]
            if unknown:
                raise ConfigError(
                    "session '%s' references unknown market(s): %s"
                    % (target, ", ".join(unknown)))

            default_sub_id = entry.get("default_sub_id") or markets[0]
            if default_sub_id not in self.markets:
                raise ConfigError(
                    "session '%s' has unknown default_sub_id '%s'"
                    % (target, default_sub_id))

            self.manager.add(
                SessionConfig(
                    sender_comp_id=sender_comp_id,
                    target_comp_id=target,
                    heartbeat_interval=entry.get("heartbeat_interval", 30),
                    begin_string=fix.get("begin_string", "FIX.4.2"),
                    reset_on_logon=entry.get("reset_on_logon", False),
                    cancel_on_disconnect=entry.get("cancel_on_disconnect", False),
                    default_sub_id=default_sub_id,
                    allowed_sub_ids=markets),
                self.application)

        self.codecs = {"fix": FixCodec(fix.get("begin_string", "FIX.4.2"))}
        self.acceptor = Acceptor(self.reactor, self.manager, self.codecs["fix"])
        self.acceptor.start(fix.get("host", "127.0.0.1"), fix.get("port", 9001))

    # -- trading state -----------------------------------------------------

    def set_trading_state(self, state, market=None, symbol=None):
        """Move one market, one instrument, or the whole venue to a phase.

        Returns the core events, having already told connected clients via
        TradingSessionStatus and published on the control plane.
        """
        targets = [market] if market else list(self.markets)
        events = []

        for name in targets:
            target = self.markets.get(name)
            if target is None:
                raise KeyError(name)
            produced = target.set_state(state, symbol)
            events.extend(produced)

            for event in produced:
                if type(event).__name__ != "TradingStateChanged":
                    continue
                self.publish("state:%s" % name, event.describe())
                if event.symbol is None and self.application is not None:
                    self.application.broadcast_trading_status(name, event.current)

            # Orders expired by the transition still owe their owners a report.
            cancels = [event for event in produced
                       if type(event).__name__ == "OrderCancelled"]
            if cancels and self.application is not None:
                self.application._emit(None, cancels)

        return events

    # -- control plane -----------------------------------------------------

    def register_commands(self, registry):
        venue_commands.register(registry, self)

    def describe(self):
        summary = Venue.describe(self)
        summary.update({
            "markets": [market.describe() for market in self.markets.values()],
            "instruments": len(self.instruments),
            "fix_port": self.acceptor.address[1] if self.acceptor else None,
            "sessions": len(self.manager.sessions) if self.manager else 0,
        })
        return summary

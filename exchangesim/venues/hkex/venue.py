"""HKEX securities market wiring.

Builds one market per SEHK market segment, loads reference data, constructs the
engine and the FIXT.1.1 acceptor, and exposes the venue over the control plane.

Two things are shaped differently from Japannext and both come from the
specification rather than from taste:

* **A security belongs to exactly one segment.** ``MarketSegmentID(1300)``
  appears only on mass cancel, never on an order, so the segment is a property
  of the security -- the ``segment`` column of ``reference/securities.csv`` --
  and it decides which book an order reaches. There is no SubID routing.
* **There are two price rules, not one, and neither is a band table.** The
  9-times rule is multiplicative against the static nominal price, so it is a
  small object (``rules.NineTimesRule``) rather than a threshold ladder. The
  quotation rule -- 24 spreads behind your own side's best, 9 through the
  other's -- is anchored on the *live* BBO, so it lives in the validator, which
  is handed the book.
"""

import logging
import os

from ...core import auction as core_auction
from ...core.config import ConfigError
from ...core.engine import Engine
from ...core.enums import CancelReason, StpMode, TradingState
from ...core.ids import IdGenerator, SequenceGenerator
from ...core.instrument import (
    Instrument,
    ReferenceDataError,
    TickTable,
    load_instruments,
    read_rows,
)
from ...core.market import Market
from ...core.prices import PriceCodec
from ...binary.codec import BinaryCodec
from ...fix.acceptor import Acceptor, SessionManager
from ...fix.codec import FixCodec
from ...fix import constants as C
from ...fix.session import SessionConfig
from ..base import Venue
from . import commands as venue_commands
from . import binary as binary_layouts
from . import dictionary as D
from . import rules
from .auctions import AuctionSession
from .handlers import HkexApplication

log = logging.getLogger(__name__)

#: HKD prices carry three decimal places -- the narrowest SEHK spread is 0.001.
PRICE_DECIMALS = 3

BEGIN_STRING = "FIXT.1.1"

#: The two encodings of OCG-C a session may be registered for. HKEX publishes
#: the same protocol as tag=value FIX and as a fixed-width binary format, and a
#: Comp ID is entitled to one of them.
PROTOCOL_FIX = "fix"
PROTOCOL_BINARY = "binary"

#: Where the binary listener goes when a session asks for one and the config
#: does not say. Adjacent to the FIX port, as the real venue keeps them apart.
DEFAULT_BINARY_PORT = 9012

#: Which auction a trading phase belongs to. PRE_OPEN and OPENING_AUCTION are
#: the two periods of one POS, so moving between them continues the session
#: rather than starting a second one.
AUCTION_PHASES = {
    TradingState.PRE_OPEN: AuctionSession.POS,
    TradingState.OPENING_AUCTION: AuctionSession.POS,
    TradingState.CLOSING_AUCTION: AuctionSession.CAS,
}


def _indicative(result, codec):
    """The indicative price picture OMD-C disseminates during an auction."""
    return {"iep": codec.format(result.price) if result.price else None,
            "iev": result.volume,
            "imbalance": result.imbalance,
            "imbalance_side": result.surplus_side,
            "reason": result.reason}


def _uncrossed(session, symbol, result, codec):
    payload = {"auction": session.kind, "market": session.market,
               "symbol": symbol}
    payload.update(_indicative(result, codec))
    if not result.crossed:
        reference = session.reference_for(symbol)
        payload["price"] = codec.format(reference) if reference else None
    else:
        payload["price"] = payload["iep"]
    return payload


class HkexVenue(Venue):
    """HKEX securities market over OCG-C FIX (FIX 5.0 SP2 / FIXT.1.1)."""

    key = "hkex"

    def __init__(self, config, reactor, publisher):
        Venue.__init__(self, config, reactor, publisher)
        self.codec = PriceCodec(PRICE_DECIMALS)
        self.dictionary = D.build()

        session_date = config.get("session_date", "")
        self.order_ids = IdGenerator("O", session_date, width=9)
        self.exec_ids = IdGenerator("E", session_date, width=9)
        self.trade_ids = IdGenerator("M", session_date, width=9)
        self.mass_cancel_ids = IdGenerator("X", session_date, width=9)
        self.sequences = SequenceGenerator()

        self.instruments = {}
        #: symbol -> MarketSegmentID. The wire never carries it; see the module
        #: docstring.
        self.segments = {}
        #: SelfMatchPreventionID(2362) -> core StpMode, registered out of band
        #: exactly as HKEX registers it against the ID. See rules.ASSUMPTIONS.
        self.smp_instructions = {}
        #: market name -> the AuctionSession running there, if any.
        self.auctions = {}
        self.markets = {}
        self.engine = None
        self.application = None
        self.manager = None
        #: One acceptor per encoding of the protocol; see :meth:`_build_fix`.
        self.acceptor = None
        self.binary_acceptor = None
        #: The FIXT.1.1 dialect and, when a binary session is configured, the
        #: binary one. Both describe OCG-C; each is its own published table.
        self.binary_dictionary = None
        self.binary_layouts = None
        self._band_table = rules.NineTimesRule()
        self._tick_table = None

    # -- lifecycle ---------------------------------------------------------

    def setup(self):
        self._load_reference_data()
        self._load_smp_registry()
        self._build_markets()
        self._build_engine()
        self._build_fix()

    def teardown(self):
        for acceptor in (self.acceptor, self.binary_acceptor):
            if acceptor is not None:
                acceptor.stop()
        if self.manager is not None:
            self.manager.close()

    # -- reference data ----------------------------------------------------

    def _read_reference_data(self):
        """Read every table from disk.

        Deliberately pure: it either returns a complete set or raises, so a typo
        in one CSV during a live reload cannot leave the venue holding half of
        the new reference data and half of the old.
        """
        default_dir = os.path.join(os.path.dirname(__file__), "reference")

        def resolve(key, default_name):
            path = self.config.resolve_path("reference.%s" % key)
            if path:
                return path
            return os.path.join(default_dir, default_name)

        securities_path = resolve("securities", "securities.csv")

        # The nominal-price limit is a rule, not a table, so it is the same
        # object for every security and never reloaded.
        bands = self._band_table

        try:
            ticks = self._read_spread_table(
                resolve("spread_table", "spread_table.csv"))
            instruments = load_instruments(
                securities_path, self.codec, bands, ticks)
            segments = self._read_segments(securities_path)
        except ReferenceDataError as exc:
            raise ConfigError("HKEX reference data: %s" % exc)

        return bands, ticks, instruments, segments

    def _read_spread_table(self, path):
        """The SEHK spread table (Second Schedule, Part A) as a tick ladder."""
        rows = []
        for record in read_rows(path, ("lower_bound", "tick")):
            rows.append((self.codec.parse(record["lower_bound"]),
                         self.codec.parse(record["tick"])))
        if not rows:
            raise ReferenceDataError("%s defines no spreads" % path)
        rows.sort()

        return TickTable([(bound, {TickTable.DEFAULT_TIER: tick})
                          for bound, tick in rows], "SEHK spread")

    def _read_segments(self, path):
        segments = {}
        for record in read_rows(path, ("symbol",)):
            symbol = record["symbol"].strip()
            if not symbol:
                continue
            segment = (record.get("segment", "") or "").strip().upper()
            if segment and segment not in D.MarketSegment.ALL:
                raise ReferenceDataError(
                    "security '%s' has unknown segment '%s'" % (symbol, segment))
            segments[symbol] = segment or D.MarketSegment.MAIN
        return segments

    def _load_reference_data(self):
        bands, ticks, instruments, segments = self._read_reference_data()
        self._band_table = bands
        self._tick_table = ticks
        self.instruments = instruments
        self.segments = segments

        log.info("loaded %d securities for venue '%s'",
                 len(self.instruments), self.name)

    def reload_reference_data(self):
        """Re-read the CSVs and fold them into the running venue.

        The instrument map is mutated **in place** because the engine was handed
        the same dict at construction; rebinding it here would leave the engine
        looking at the old universe. Books, resting orders and statistics all
        survive.

        A security that has *disappeared* from the CSV is marked non-tradable
        rather than deleted, because live orders may still reference it. A
        security whose segment has changed keeps its existing book as well as
        gaining the new one, for the same reason.
        """
        bands, ticks, loaded, segments = self._read_reference_data()

        before = {symbol: instrument.describe(self.codec)
                  for symbol, instrument in self.instruments.items()}
        before_segments = dict(self.segments)

        self._band_table = bands
        self._tick_table = ticks

        added, updated, withdrawn = [], [], []

        for symbol in sorted(loaded):
            instrument = loaded[symbol]
            known = symbol in self.instruments
            self.instruments[symbol] = instrument
            self.segments[symbol] = segments[symbol]

            market = self.markets.get(segments[symbol])
            if market is not None:
                market.book(symbol)

            if not known:
                added.append(symbol)
            elif (instrument.describe(self.codec) != before[symbol]
                  or before_segments.get(symbol) != segments[symbol]):
                updated.append(symbol)

        for symbol in sorted(before):
            if symbol in loaded:
                continue
            instrument = self.instruments[symbol]
            instrument.band_table = bands
            instrument.tick_table = ticks
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
        # ``tier`` is the segment here: SEHK spreads do not vary by index tier,
        # so the field is free, and it is what a caller adding a GEM security
        # needs to be able to say.
        segment = (tier or D.MarketSegment.MAIN).upper()
        if segment not in D.MarketSegment.ALL:
            raise ValueError("unknown market segment '%s'; expected one of %s"
                             % (segment, ", ".join(D.MarketSegment.ALL)))
        if segment not in self.markets:
            raise ValueError("market segment '%s' is not running" % segment)

        self.segments[symbol] = segment
        return Instrument(
            symbol=symbol,
            name=name,
            lot_size=lot_size,
            shares_outstanding=shares_outstanding,
            base_price=base_price,
            tier=None,
            band_table=self._band_table,
            tick_table=self._tick_table,
            tradable=tradable)

    def books_for(self, instrument):
        market = self.markets.get(self.segment_for(instrument.symbol))
        return [market] if market is not None else []

    def segment_for(self, symbol):
        """The market an order in ``symbol`` reaches, or None if unlisted."""
        return self.segments.get(symbol)

    # -- self-match prevention ---------------------------------------------

    def _load_smp_registry(self):
        for entry in self.config.get("smp") or []:
            smp_id = str(entry.get("id", "")).strip()
            if not smp_id:
                raise ConfigError("each entry of 'smp' needs an 'id'")
            self.register_smp(smp_id, entry.get("instruction", ""))

    def register_smp(self, smp_id, instruction):
        """Record the instruction HKEX holds against an SMP ID."""
        name = str(instruction or "").strip().upper()
        mode = rules.SMP_INSTRUCTION_TO_CORE.get(name)
        if mode is None:
            raise ConfigError(
                "SMP instruction must be one of %s, not '%s'"
                % (", ".join(sorted(rules.SMP_INSTRUCTION_TO_CORE)), instruction))
        self.smp_instructions[smp_id] = mode
        return mode

    def smp_instruction_for(self, smp_id):
        if not smp_id:
            return None
        return self.smp_instructions.get(smp_id, rules.DEFAULT_SMP_INSTRUCTION)

    # -- markets -----------------------------------------------------------

    def _build_markets(self):
        configured = self.config.get("markets")
        if not configured:
            # One market per segment the universe actually uses, so a venue
            # loaded with Main Board names only does not present an empty GEM.
            present = sorted(set(self.segments.values()),
                             key=D.MarketSegment.ALL.index)
            configured = [{"name": name} for name in present]

        default_state = self.config.get("initial_state", TradingState.CLOSED)
        # HKEX self-match prevention is keyed on the order, never on the market,
        # so the market-wide mode stays off unless a config deliberately asks.
        default_stp = self.config.get("stp_mode", StpMode.NONE)

        for entry in configured:
            name = entry.get("name")
            if not name:
                raise ConfigError("each entry of 'markets' needs a 'name'")
            if name not in D.MarketSegment.ALL:
                raise ConfigError(
                    "market '%s' is not a MarketSegmentID; expected one of %s"
                    % (name, ", ".join(D.MarketSegment.ALL)))

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
                    "description", D.MarketSegment.LABELS.get(name, name)))

            log.info("market segment '%s' created: state=%s stp=%s",
                     name, state, stp_mode)

        # Each security gets a book in its own segment and nowhere else.
        for symbol in self.instruments:
            market = self.markets.get(self.segments.get(symbol))
            if market is not None:
                market.book(symbol)

    # -- engine ------------------------------------------------------------

    def _build_engine(self):
        limits = rules.default_limits(
            max_order_value=self.config.get("limits.max_order_value"),
            quotation_spreads=self.config.get("limits.quotation_spreads",
                                              rules.QUOTATION_SPREADS),
            aggression_spreads=self.config.get("limits.aggression_spreads",
                                               rules.AGGRESSION_SPREADS))
        validator = rules.HkexValidator(self.codec, limits, self)
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
        """Build the session table and one acceptor per encoding in use.

        HKEX publishes OCG-C twice: tag=value FIX and a fixed-width binary
        encoding of the same protocol. A Comp ID is registered for one of them,
        so there is one session table and one set of books; what differs per
        session is the codec that frames its bytes and the dialect table its
        messages are validated against. The binary listener is started only
        when a session asks for it -- a venue does not open a port nobody has
        been given.
        """
        fix = self.config.section("fix")
        sender_comp_id = fix.get("sender_comp_id", "HKEXSIM")
        store_root = self.config.resolve_path("fix.store")

        self.application = HkexApplication(self)
        self.manager = SessionManager(self.clock, self.dictionary, store_root,
                                      audit=self.audit)
        self.codecs = {"fix": FixCodec(fix.get("begin_string", BEGIN_STRING))}

        sessions = fix.get("sessions") or []
        if not sessions:
            log.warning("venue '%s' has no FIX sessions configured", self.name)

        seen = set()
        binary_sessions = 0

        for entry in sessions:
            target = entry.get("target_comp_id")
            if not target:
                raise ConfigError(
                    "each entry of 'fix.sessions' needs a 'target_comp_id'")
            if target in seen:
                # One Comp ID, one session: the protocols share a table, so a
                # repeat would otherwise quietly replace the earlier entry.
                raise ConfigError(
                    "'fix.sessions' names target_comp_id '%s' twice" % target)
            seen.add(target)

            protocol = entry.get("protocol", PROTOCOL_FIX)
            if protocol not in (PROTOCOL_FIX, PROTOCOL_BINARY):
                raise ConfigError(
                    "session '%s' has protocol '%s'; it must be '%s' or '%s'"
                    % (target, protocol, PROTOCOL_FIX, PROTOCOL_BINARY))
            binary = protocol == PROTOCOL_BINARY
            if binary:
                binary_sessions += 1
                self._build_binary_dialect(sender_comp_id)

            segments = entry.get("markets") or sorted(self.markets)
            unknown = [name for name in segments if name not in self.markets]
            if unknown:
                raise ConfigError(
                    "session '%s' references unknown market segment(s): %s"
                    % (target, ", ".join(unknown)))

            self.manager.add(
                SessionConfig(
                    sender_comp_id=sender_comp_id,
                    target_comp_id=target,
                    heartbeat_interval=entry.get("heartbeat_interval", 20),
                    begin_string=(D.BINARY_BEGIN_STRING if binary
                                  else fix.get("begin_string", BEGIN_STRING)),
                    cancel_on_disconnect=entry.get("cancel_on_disconnect", True),
                    # No SubID routing: the security picks the segment. The
                    # allowed set is still enforced, in the handler.
                    default_sub_id=None,
                    allowed_sub_ids=segments,
                    next_expected_seq_num=True,
                    allow_logon_reset=False,
                    # The binary Logon carries neither field: its encryption is
                    # fixed by the encoding and its heartbeat agreed out of band.
                    requires_encrypt_method=not binary,
                    requires_heart_bt_int=not binary,
                    appl_ver_id=None if binary else C.ApplVerID.FIX50SP2,
                    default_appl_ver_id=(None if binary
                                         else C.ApplVerID.FIX50SP2)),
                self.application,
                codec=self.codecs[protocol],
                dictionary=self.binary_dictionary if binary else self.dictionary)

        self.acceptor = Acceptor(self.reactor, self.manager,
                                 self.codecs[PROTOCOL_FIX], self.dictionary)
        self.acceptor.start(fix.get("host", "127.0.0.1"), fix.get("port", 9011))

        if binary_sessions:
            binary_config = self.config.section("binary")
            # Only the first acceptor polls the session table: they share it,
            # and ticking it twice a second would halve every heartbeat.
            self.binary_acceptor = Acceptor(
                self.reactor, self.manager, self.codecs[PROTOCOL_BINARY],
                self.binary_dictionary, tick=False)
            self.binary_acceptor.start(
                binary_config.get("host", fix.get("host", "127.0.0.1")),
                binary_config.get("port", DEFAULT_BINARY_PORT))

    def _build_binary_dialect(self, sender_comp_id):
        """The binary dictionary, layouts and codec, built once on demand."""
        if self.binary_dictionary is not None:
            return
        self.binary_dictionary = D.build_binary()
        self.binary_layouts = binary_layouts.build(self.binary_dictionary)
        self.codecs[PROTOCOL_BINARY] = BinaryCodec(self.binary_layouts,
                                                   sender_comp_id)

    def dictionary_for(self, protocol=None):
        """The dialect a recorded message should be read against."""
        if protocol == PROTOCOL_BINARY and self.binary_dictionary is not None:
            return self.binary_dictionary
        return self.dictionary

    # -- trading state -----------------------------------------------------

    def set_trading_state(self, state, market=None, symbol=None):
        """Move one segment, one security, or the whole venue to a phase.

        Entering an accumulating phase opens an auction session; leaving one
        uncrosses it. That ordering matters: the uncrossing has to happen before
        the core expires resting orders on the way into a closed state, or the
        closing auction would find an empty book.

        Unlike Japannext there is no TradingSessionStatus in this dialect, so
        connected clients learn of a transition only through the execution
        reports it produces. The control plane still publishes it.
        """
        targets = [market] if market else list(self.markets)
        events = []

        for name in targets:
            target = self.markets.get(name)
            if target is None:
                raise KeyError(name)

            events.extend(self._close_auction(target, state, symbol))
            produced = target.set_state(state, symbol)
            events.extend(produced)
            events.extend(self._open_auction(target, state, symbol))

            for event in produced:
                if type(event).__name__ == "TradingStateChanged":
                    self.publish("state:%s" % name, event.describe())

        reportable = [event for event in events
                      if type(event).__name__ != "TradingStateChanged"]
        if reportable and self.application is not None:
            self.application._emit(None, reportable)

        return events

    # -- auctions ----------------------------------------------------------

    def _open_auction(self, market, state, symbol):
        """Start POS or CAS if the new phase is one of theirs.

        Returns the events from cancelling orders carried in from continuous
        trading that the session's band will not admit.
        """
        if symbol is not None:
            # An instrument-level override does not open a session of its own;
            # the market's phase is what the auction belongs to.
            return []

        kind = AUCTION_PHASES.get(state)
        if kind is None:
            self.auctions.pop(market.name, None)
            return []

        existing = self.auctions.get(market.name)
        if existing is not None and existing.kind == kind:
            return []                # PRE_OPEN -> OPENING_AUCTION is one POS

        session = AuctionSession(kind, market.name, self.codec)
        for name in market.symbols:
            session.set_reference(name, self._reference_price(market, name, kind))
        self.auctions[market.name] = session

        events = market.cancel_all(
            lambda order: session.carried_forward_verdict(order) == "cancel",
            CancelReason.ADMINISTRATIVE,
            "price outside the %s band on entering the auction" % kind)

        log.info("market '%s' opened a %s auction over %d securit(ies); "
                 "%d carried-forward order(s) cancelled",
                 market.name, kind, len(session.reference), len(events))
        self.publish("auction:%s" % market.name, session.describe())
        return events

    def _close_auction(self, market, state, symbol):
        """Uncross, if the market is leaving an auction phase."""
        session = self.auctions.get(market.name)
        if session is None or symbol is not None:
            return []
        if state in AUCTION_PHASES:
            return []

        events = []
        for name in market.symbols:
            result, produced = market.uncross(name, session.reference_for(name))
            events.extend(produced)
            if produced or result.crossed:
                self.publish("auction:%s:%s" % (market.name, name),
                             _uncrossed(session, name, result, self.codec))

        del self.auctions[market.name]
        log.info("market '%s' uncrossed its %s auction: %d event(s)",
                 market.name, session.kind, len(events))
        return events

    def lock_auction(self, market_name):
        """Close the input period: no more cancellations, prices pinned."""
        session = self.auctions.get(market_name)
        if session is None:
            raise KeyError(market_name)

        market = self.markets[market_name]
        books = dict((symbol, market.book(symbol)) for symbol in market.symbols)
        session.lock(books)

        # POS names its locked period a separate phase; CAS does not, so only
        # the opening auction advances its trading state.
        if (session.kind == AuctionSession.POS
                and market.state.market_state == TradingState.PRE_OPEN):
            market.set_state(TradingState.OPENING_AUCTION)
            self.publish("state:%s" % market_name,
                         {"market": market_name,
                          "current": TradingState.OPENING_AUCTION})

        self.publish("auction:%s" % market_name, session.describe())
        return session

    def auction_state(self, market_name, symbol=None):
        """The live auction picture: limits, and the indicative price."""
        session = self.auctions.get(market_name)
        if session is None:
            return None
        summary = session.describe(symbol)
        if symbol is not None:
            market = self.markets[market_name]
            result = core_auction.uncross(market.book(symbol),
                                          session.reference_for(symbol))
            summary.update(_indicative(result, self.codec))
        return summary

    def _reference_price(self, market, symbol, kind):
        """What the auction is anchored on.

        POS uses the previous closing price, which is the nominal price the
        instrument was loaded with. CAS uses the median of five nominal prices
        sampled over the last minute of continuous trading -- see the 'CAS
        reference price' entry in :data:`rules.ASSUMPTIONS` for why this takes
        the current nominal price instead.
        """
        instrument = self.instruments.get(symbol)
        previous_close = instrument.base_price if instrument else None
        if kind == AuctionSession.POS:
            return previous_close

        statistics = market.data.statistics(symbol)
        if statistics is not None and statistics.last_price is not None:
            return statistics.last_price
        return previous_close

    # -- control plane -----------------------------------------------------

    def register_commands(self, registry):
        venue_commands.register(registry, self)

    def describe(self):
        summary = Venue.describe(self)
        summary.update({
            "markets": [market.describe() for market in self.markets.values()],
            "instruments": len(self.instruments),
            "fix_port": self.acceptor.address[1] if self.acceptor else None,
            "binary_port": (self.binary_acceptor.address[1]
                            if self.binary_acceptor else None),
            "sessions": len(self.manager.sessions) if self.manager else 0,
        })
        return summary

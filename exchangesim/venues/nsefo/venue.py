"""NSE Futures & Options venue wiring.

The shape of this module is deliberately close to
:mod:`exchangesim.venues.nse.venue`: one market, two listeners (the gateway and
the Gateway Router, joined by the Box ID exactly as Capital Market's are), and
an exchange-assigned order number with no client-supplied handle. See that
module's docstring for the two-listener story; it is not repeated here.

**What is genuinely new is contract identity.** Capital Market keys a security
on ``Symbol+Series``, two short fields that together name one book. F&O keys a
contract on ``CONTRACT_DESC`` -- ``InstrumentName``, ``Symbol``, ``ExpiryDate``,
``StrikePrice`` and ``OptionType`` -- five wire fields with no single spelling a
person would type or a log would read comfortably. Everything above the codec
(the book, the audit, the board, the control plane) needs *one* canonical
rendered name, the way ``NseVenue`` settled on ``SYMBOL-SERIES``. This module's
choice is::

    NIFTY-FUTIDX-24SEP2026                  (a future: no strike, no option type)
    RELIANCE-OPTSTK-24SEP2026-2800-CE       (an option: both, in that order)

-- underlying, instrument family, expiry (``DDMMMYYYY``, the way NSE's own
contract-note convention reads a derivatives expiry), and for an option the
strike and CE/PE. ``resolve_symbol`` forgives the reasonable variations a
person might type (case, ``_``/space in place of ``-``, an ISO expiry, a
strike with or without trailing zeroes) and answers in the canonical spelling
only, exactly as ``HkexVenue.resolve_symbol`` forgives a padded stock code. An
*unambiguous* partial -- naming a symbol and instrument family, or a
symbol/instrument family/expiry, that together name exactly one contract --
resolves too; an ambiguous one is refused rather than guessed, the same choice
``NseVenue.resolve_symbol`` makes for a bare symbol that names more than one
series.

The wire, by contrast, never needs forgiving: ``CONTRACT_DESC`` arrives as five
already-decoded fields (an expiry in whole seconds, a strike already scaled),
so an inbound order is matched to a contract by an **exact** key lookup
(:meth:`NsefoVenue.contract_by_wire`), never through the text-oriented
``resolve_symbol``. Keeping the two lookups separate is what lets
``resolve_symbol`` be forgiving without ever risking a wire order being
matched to the wrong contract by a fuzzy guess.
"""

import calendar
import itertools
import logging
import os
import time

from ...core.config import ConfigError
from ...core.engine import Engine
from ...core.enums import StpMode, TradingState
from ...core.ids import IdGenerator, SequenceGenerator
from ...core.instrument import Instrument, ReferenceDataError, TickTable, read_rows
from ...core.market import Market
from ...core.prices import PriceCodec
from ...core.validation import StandardValidator
from ...nnf import types as WT
from ...nnf import recovery
from ...nnf.codec import NnfCodec
from ...nnf.session import NnfSessionConfig, NnfSessionManager
from ...tls import certs
from ...wire.acceptor import Acceptor
from ..base import Venue
from . import commands as venue_commands
from . import dictionary as D
from . import layouts as LY
from . import rules
from . import transactions as X
from .gateway_router import SUPPORTED_TLS, GatewayRouter
from .handlers import NsefoApplication

log = logging.getLogger(__name__)

#: The Normal market, and the only one this venue runs (transcription §2.1:
#: three of F&O's own four market types are marked "Not used" outright).
NORMAL_MARKET = "NORMAL"

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class OrderNumberGenerator(object):
    """The exchange-assigned order number, as a plain decimal string.

    Independent of, but identical in shape to, Capital Market's own -- see
    that module's docstring for why a plain monotonic counter is the right
    reading of a specification that does not publish its own encoding. Kept as
    a separate class rather than an import: the two venues' order numbers are
    unrelated sequences (this simulator runs each venue as its own process),
    and a future change to one must not risk silently changing the other.
    """

    __slots__ = ("_counter",)

    def __init__(self, start=1):
        self._counter = itertools.count(int(start))

    def next(self):
        return str(next(self._counter))

    def peek(self):
        return self._counter.__reduce__()[1][0]


class _ExplicitCertificate(object):
    """An operator-supplied Gateway Router certificate. See
    ``venues/nse/venue.py``'s identical class for the reasoning."""

    __slots__ = ("ca_certificate",)

    def __init__(self, ca_certificate):
        self.ca_certificate = ca_certificate

    def describe(self):
        return {"ca_certificate": self.ca_certificate, "certificate": None,
                "fingerprint": None, "not_after": None, "names": []}


def _dedupe(items):
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


# -- contract identity --------------------------------------------------------

class Contract(object):
    """One F&O contract: its canonical name, and the wire values that name it.

    The venue keeps exactly one of these per canonical name, alongside (not
    instead of) the ``Instrument`` the engine and the books use -- an
    ``Instrument`` has no room for ``InstrumentName``/``ExpiryDate``/
    ``StrikePrice``/``OptionType``, and inventing room for them there would put
    F&O's own vocabulary into a core module.
    """

    __slots__ = ("canonical", "instrument_name", "symbol", "expiry_seconds",
                 "strike_text", "option_type")

    def __init__(self, canonical, instrument_name, symbol, expiry_seconds,
                strike_text, option_type):
        self.canonical = canonical
        self.instrument_name = instrument_name
        self.symbol = symbol
        self.expiry_seconds = expiry_seconds
        #: Already in the wire's own scaled-decimal spelling ("-1" for a
        #: future, "2800.00" for an option), so it round-trips onto
        #: CONTRACT_DESC.StrikePrice with no further conversion.
        self.strike_text = strike_text
        self.option_type = option_type

    @property
    def is_future(self):
        return self.instrument_name in (D.InstrumentName.FUTURES_INDEX,
                                        D.InstrumentName.FUTURES_STOCK)

    def wire_key(self):
        return (self.instrument_name, self.symbol, self.expiry_seconds,
               self.strike_text, self.option_type)


def _expiry_parts(iso_date):
    year, month, day = iso_date.split("-")
    return int(year), int(month), int(day)


def _expiry_display(iso_date):
    """``2026-09-24`` as ``24SEP2026`` -- NSE's own contract-note convention."""
    year, month, day = _expiry_parts(iso_date)
    return "%02d%s%d" % (day, _MONTHS[month - 1], year)


def _expiry_seconds(iso_date):
    """Seconds since the NSE epoch (midnight 1-Jan-1980) at midnight UTC on
    ``iso_date`` -- what ``CONTRACT_DESC.ExpiryDate`` carries (transcription
    §1.2)."""
    year, month, day = _expiry_parts(iso_date)
    unix_seconds = calendar.timegm((year, month, day, 0, 0, 0, 0, 0, 0))
    return WT.to_nse_seconds(unix_seconds)


def _iso_of(expiry_seconds):
    """``ExpiryDate`` seconds back to ``YYYY-MM-DD``, for the ISO alias."""
    unix_seconds = WT.from_nse_seconds(expiry_seconds)
    year, month, day = time.gmtime(unix_seconds)[:3]
    return "%04d-%02d-%02d" % (year, month, day)


def _trim_strike(text):
    """``"2800.00"`` -> ``"2800"``, so the canonical name reads the way a
    person quotes a strike; a fractional strike keeps its decimals."""
    if text.endswith(".00"):
        return text[:-3]
    return text


def _canonical_name(symbol, instrument_name, expiry_display, strike_text,
                    option_type):
    if instrument_name in (D.InstrumentName.FUTURES_INDEX,
                           D.InstrumentName.FUTURES_STOCK):
        return "%s-%s-%s" % (symbol, instrument_name, expiry_display)
    return "%s-%s-%s-%s-%s" % (symbol, instrument_name, expiry_display,
                               _trim_strike(strike_text), option_type)


def _normalize(value):
    """Upper-case, and every run of whitespace or ``_`` folded to one ``-``.

    The forgiving half of ``resolve_symbol``: a person types
    ``nifty_futidx 24sep2026`` as readily as ``NIFTY-FUTIDX-24SEP2026``, and
    both should mean the same contract.
    """
    text = str(value).strip().upper()
    out = []
    last_dash = False
    for ch in text:
        if ch in "-_ \t":
            if not last_dash:
                out.append("-")
            last_dash = True
        else:
            out.append(ch)
            last_dash = False
    return "".join(out).strip("-")


def _int(value, field):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("'%s' must be an integer, not %r" % (field, value))


class NsefoVenue(Venue):
    """NSE India Futures & Options over the NNF Trimmed Protocol."""

    key = "nsefo"

    #: This slice runs continuous trading only: no pre-open (its own
    #: uncrossing rule set is not built) and no Postclose (a genuine fifth
    #: phase, market orders only, with no analogue elsewhere in this project).
    #: Both are published phases -- see ``rules.NOT_IMPLEMENTED`` -- and are
    #: refused by ``state.set`` rather than folded onto OPEN or CLOSED.
    trading_states = (TradingState.OPEN, TradingState.CLOSED,
                      TradingState.HALTED)

    def __init__(self, config, reactor, publisher):
        Venue.__init__(self, config, reactor, publisher)
        self.codec = PriceCodec(D.PRICE_DECIMALS)
        self.dictionary = D.build_fo()
        self.layouts = LY.build_fo()
        self.codecs = {"nnf": NnfCodec(self.layouts)}

        session_date = config.get("session_date", "")
        self.order_ids = OrderNumberGenerator(
            config.get("first_order_number", 1))
        self.trade_ids = IdGenerator("", session_date, width=9, max_length=20)
        self.sequences = SequenceGenerator()

        self.instruments = {}          # canonical name -> Instrument
        self._contracts = {}           # canonical name -> Contract
        self._by_wire_key = {}         # Contract.wire_key() -> canonical name
        self._aliases = {}             # normalized alias -> canonical name
        self._partial = {}             # normalized partial key -> {canonical}
        self.markets = {}
        self.engine = None
        self.application = None
        self.manager = None
        self.acceptor = None
        self.router = None
        self.tls_certificate = None
        self.market_name = NORMAL_MARKET
        self._boxes = {}
        self._issued = {}

    # -- lifecycle -----------------------------------------------------------

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

    # -- reference data --------------------------------------------------

    def _read_reference_data(self):
        default_dir = os.path.join(os.path.dirname(__file__), "reference")
        path = self.config.resolve_path("reference.contracts") or \
            os.path.join(default_dir, "contracts.csv")
        required = ("instrument_name", "symbol", "expiry", "option_type",
                   "lot_size", "base_price", "tick", "band_pct")
        try:
            rows = read_rows(path, required)
        except ReferenceDataError as exc:
            raise ConfigError("F&O reference data: %s" % exc)

        instruments = {}
        contracts = {}
        for row in rows:
            try:
                contract, instrument = self._build_contract(row)
            except (ValueError, ReferenceDataError) as exc:
                raise ConfigError("F&O reference data (%s): %s" % (path, exc))
            if contract.canonical in contracts:
                raise ConfigError(
                    "F&O reference data (%s): duplicate contract '%s'"
                    % (path, contract.canonical))
            contracts[contract.canonical] = contract
            instruments[contract.canonical] = instrument
        return instruments, contracts

    def _build_contract(self, row):
        instrument_name = row["instrument_name"].strip().upper()
        symbol = row["symbol"].strip().upper()
        expiry_iso = row["expiry"].strip()
        option_type = row["option_type"].strip().upper()
        lot_size = _int(row["lot_size"], "lot_size")
        base_price = self.codec.parse(row["base_price"])
        tick = self.codec.parse(row["tick"])
        band_pct = _int(row["band_pct"], "band_pct")

        strike_raw = (row.get("strike") or "").strip()
        if strike_raw:
            strike_text = self.codec.format(self.codec.parse(strike_raw))
        else:
            strike_text = "-1"          # the sentinel: see layouts.StrikeScaled

        expiry_display = _expiry_display(expiry_iso)
        expiry_seconds = _expiry_seconds(expiry_iso)
        canonical = _canonical_name(symbol, instrument_name, expiry_display,
                                    strike_text, option_type)

        contract = Contract(canonical, instrument_name, symbol, expiry_seconds,
                           strike_text, option_type)
        instrument = Instrument(
            symbol=canonical, name=canonical, lot_size=lot_size,
            base_price=base_price, band_table=rules.CircuitFilter(band_pct),
            tick_table=TickTable([(0, {None: tick})], name=canonical))
        return contract, instrument

    def _load_reference_data(self):
        instruments, contracts = self._read_reference_data()
        self.instruments.clear()
        self.instruments.update(instruments)
        self._contracts.clear()
        self._contracts.update(contracts)
        self._index_contracts()
        log.info("venue '%s' loaded %d contracts", self.name,
                len(self.instruments))

    def reload_reference_data(self):
        instruments, contracts = self._read_reference_data()

        before = {name: instrument.describe(self.codec)
                 for name, instrument in self.instruments.items()}

        added, updated, withdrawn = [], [], []
        for name in sorted(contracts):
            instrument = instruments[name]
            known = name in self.instruments
            self.instruments[name] = instrument
            self._contracts[name] = contracts[name]
            if not known:
                for market in self.markets.values():
                    market.book(name)
                added.append(name)
            elif instrument.describe(self.codec) != before[name]:
                updated.append(name)

        for name in sorted(before):
            if name in contracts:
                continue
            instrument = self.instruments[name]
            if instrument.tradable:
                instrument.tradable = False
                withdrawn.append(name)

        self._index_contracts()
        log.info("venue '%s' reference reload: %d added, %d updated, "
                "%d withdrawn, %d total", self.name, len(added), len(updated),
                len(withdrawn), len(self.instruments))
        return {"added": added, "updated": updated, "withdrawn": withdrawn,
                "instruments": len(self.instruments)}

    def create_instrument(self, symbol, name="", lot_size=100, base_price=None,
                          tier=None, shares_outstanding=0, tradable=True):
        raise NotImplementedError(
            "venue '%s' contracts come from reference/contracts.csv; there is "
            "no standalone 'add a contract' operation, because a contract "
            "needs an instrument family, an expiry and (for an option) a "
            "strike and option type that 'instrument.add' has no arguments "
            "for" % self.key)

    def books_for(self, instrument):
        return [self.markets[self.market_name]]

    # -- contract identity -------------------------------------------------

    def _index_contracts(self):
        """Rebuild the wire-key, alias and partial-match indexes.

        Called after every load: reference data may be reloaded at runtime,
        and a stale index would resolve to a contract that no longer trades.
        """
        self._by_wire_key = {}
        aliases = {}          # normalized alias -> set of canonical names
        partial = {}          # normalized partial key -> set of canonical names

        for canonical, contract in self._contracts.items():
            self._by_wire_key[contract.wire_key()] = canonical

            family_key = _normalize("%s-%s" % (contract.symbol,
                                               contract.instrument_name))
            partial.setdefault(family_key, set()).add(canonical)

            parts = canonical.split("-")
            expiry_display = parts[2]
            iso_expiry = _iso_of(contract.expiry_seconds)

            if contract.is_future:
                # A future's only alternate exact spelling is an ISO expiry;
                # there is no strike or option type to vary.
                iso_alias = "%s-%s-%s" % (contract.symbol,
                                          contract.instrument_name, iso_expiry)
                aliases.setdefault(_normalize(iso_alias), set()).add(canonical)
                continue

            trimmed_strike = parts[3]

            # Exact alternate spellings: an ISO expiry, and the strike with
            # its original two decimals (the canonical name trims ".00").
            iso_alias = "%s-%s-%s-%s-%s" % (
                contract.symbol, contract.instrument_name, iso_expiry,
                trimmed_strike, contract.option_type)
            aliases.setdefault(_normalize(iso_alias), set()).add(canonical)

            if contract.strike_text != trimmed_strike:
                full_strike_alias = "%s-%s-%s-%s-%s" % (
                    contract.symbol, contract.instrument_name, expiry_display,
                    contract.strike_text, contract.option_type)
                aliases.setdefault(_normalize(full_strike_alias), set()) \
                    .add(canonical)

            # Unambiguous partials: symbol+instrument+expiry, with no strike
            # or option type named at all.
            expiry_key = _normalize("%s-%s-%s" % (
                contract.symbol, contract.instrument_name, expiry_display))
            partial.setdefault(expiry_key, set()).add(canonical)
            iso_expiry_key = _normalize("%s-%s-%s" % (
                contract.symbol, contract.instrument_name, iso_expiry))
            partial.setdefault(iso_expiry_key, set()).add(canonical)

        self._aliases = {key: next(iter(names)) for key, names in aliases.items()
                         if len(names) == 1}
        self._partial = partial

    def resolve_symbol(self, value):
        """This venue's own spelling of a contract a client named.

        Exact canonical spelling always resolves; a forgiving variation
        (case, ``_``/space, an ISO expiry, a strike with or without trailing
        zeroes) resolves when it names exactly one contract; and an
        unambiguous partial -- naming a symbol and instrument family, or a
        symbol/instrument family/expiry, that together name exactly one
        contract -- resolves too. An ambiguous partial is refused rather than
        guessed, the same choice ``NseVenue.resolve_symbol`` makes for a bare
        symbol that names more than one series.
        """
        if not value:
            return None
        text = _normalize(value)
        if text in self.instruments:
            return text
        canonical = self._aliases.get(text)
        if canonical is not None:
            return canonical
        matches = self._partial.get(text)
        if matches and len(matches) == 1:
            return next(iter(matches))
        return None

    def contract_by_wire(self, instrument_name, symbol, expiry_seconds,
                         strike_text, option_type):
        """The canonical name for an **exact** ``CONTRACT_DESC``, or ``None``.

        Deliberately not forgiving: the wire hands over five already-decoded
        values (an expiry in whole seconds, a strike in the venue's own
        scaled-decimal spelling), so there is nothing to forgive, and treating
        an inbound order the way a typed-in symbol is treated would risk
        matching the wrong contract on a fuzzy guess.
        """
        key = (str(instrument_name or "").strip().upper(),
              str(symbol or "").strip().upper(),
              int(expiry_seconds or 0),
              str(strike_text or "-1"),
              str(option_type or "").strip().upper())
        return self._by_wire_key.get(key)

    def contract_for(self, canonical):
        return self._contracts.get(canonical)

    # -- market --------------------------------------------------------------

    def _build_market(self):
        configured = self.config.get("markets") or [
            {"name": NORMAL_MARKET, "description": "Normal Market"}]
        if len(configured) != 1:
            raise ConfigError(
                "F&O runs one market, the Normal market; %d were configured"
                % len(configured))

        entry = configured[0]
        self.market_name = entry.get("name") or NORMAL_MARKET
        state = str(entry.get("state",
                              self.config.get("initial_state",
                                              TradingState.CLOSED))).upper()
        state = self.check_trading_state(
            state, "market '%s'" % self.market_name)

        # No self-trade prevention at all: the core's per-market mode keys on
        # `mpid`, and this venue has no such concept on order entry either.
        # ADDITIONAL_ORDER_FLAGS.STPC is refused per-order instead -- see
        # rules.STPC_NOT_ALLOWED.
        stp_mode = str(entry.get("stp_mode", StpMode.NONE)).upper()
        if stp_mode != StpMode.NONE:
            raise ConfigError(
                "F&O has no self-trade prevention; market '%s' asks for '%s'"
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
            ack_on_entry=rules.ACK_BEFORE_EXECUTION)

        for symbol in self.instruments:
            self.markets[self.market_name].book(symbol)
        log.info("market '%s' created: state=%s", self.market_name, state)

    def set_trading_state(self, state, market=None, symbol=None):
        """Move the market. No auction to uncross first -- there is no
        pre-open in this slice at all (see ``trading_states``)."""
        target = self.markets.get(market or self.market_name)
        if target is None:
            raise KeyError(market)
        changed = target.set_state(state, symbol=symbol)
        if self.application is not None:
            self.application.broadcast_state(state)
        return changed

    # -- engine ----------------------------------------------------------

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

    #: "Machine / Stream no. ... can range from 1 to 127". A member downloads
    #: one stream at a time, looping until each has answered, and learns how
    #: many to loop over from SYSTEM_INFORMATION_OUT's AlphaChar. This
    #: simulator is one machine and puts every message on one stream, so
    #: ``nnf.stream`` is both that stream's number and the count advertised:
    #: a member asking for any lower-numbered stream is answered with an empty
    #: download rather than an error, which is what its loop expects.
    def _build_sessions(self):
        nnf = self.config.section("nnf")
        self.stream = int(nnf.get("stream", 1))
        if not 1 <= self.stream <= 127:
            raise ConfigError("nnf.stream must be between 1 and 127, not %d"
                              % self.stream)
        self.application = NsefoApplication(self)
        self.manager = NnfSessionManager(
            self.clock, self.dictionary, self.layouts, audit=self.audit,
            heartbeat_seconds=nnf.get("heartbeat_interval", 30),
            drop_counter_limit=nnf.get("heartbeat_drop_limit", 10),
            user_id_tag=D.USER_ID, box_id_tag=D.BOX_ID,
            timestamp1_tag=D.TIMESTAMP1, timestamp2_tag=D.TIMESTAMP2,
            machine_number=self.stream,
            store=recovery.MessageStore(
                capacity=nnf.get("recovery_capacity",
                                 recovery.DEFAULT_CAPACITY),
                recoverable=rules.is_recoverable,
                normalise=rules.untrimmed),
            heartbeat_code=int(self.layouts.layout(str(X.HEARTBEAT)).msg_type))
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
        port = nnf.get("port", 9031)

        self.acceptor = Acceptor(self.reactor, self.manager,
                                 codec=NnfCodec(self.layouts),
                                 dictionary=self.dictionary)
        self.acceptor.start(host, port)

        router = self.config.section("gateway_router")
        if router.get("enabled", True):
            router_host = router.get("host", host)
            certfile, keyfile, ca_certificate = self._build_tls(
                router, router_host)
            self.router = GatewayRouter(self, router, certfile=certfile,
                                        keyfile=keyfile,
                                        ca_certificate=ca_certificate)
            self.router.start(router_host, router.get("port", port + 1))

    def _build_tls(self, router, host):
        policy = str(router.get("tls", "1.3")).lower()
        if policy not in SUPPORTED_TLS or policy == "none":
            self.tls_certificate = None
            return None, None, None

        explicit_cert = router.get("tls_cert")
        explicit_key = router.get("tls_key")
        if explicit_cert and explicit_key:
            certfile = router.resolve_path("tls_cert")
            keyfile = router.resolve_path("tls_key")
            ca_certificate = router.resolve_path("tls_ca_cert")
            self.tls_certificate = _ExplicitCertificate(ca_certificate)
            return certfile, keyfile, ca_certificate

        names = router.get("tls_names") or _dedupe(
            [host, "localhost", "127.0.0.1"])
        # A different default directory from Capital Market's 'var/tls', so
        # the two venues' generated certificates never collide when both run
        # at once from the same repo clone.
        tls_dir = router.resolve_path("tls_dir", "../var/tls-fo")
        store = certs.CertificateStore(tls_dir, common_name=host,
                                       names=names)
        issued = store.ensure()
        self.tls_certificate = store
        return issued.certificate, issued.key, issued.ca_certificate

    # -- encryption --------------------------------------------------------

    @property
    def requires_encryption(self):
        return bool(self.config.get("nnf.require_encryption", False))

    def issue(self, box_id, secrets):
        self._issued[int(box_id)] = secrets

    def issued(self, box_id):
        return self._issued.get(int(box_id))

    def forget(self, box_id):
        return self._issued.pop(int(box_id), None)

    def session_key_for(self, box_id):
        secrets = self.issued(box_id)
        return secrets.session_key if secrets is not None else None

    def cipher_for(self, box_id):
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
            "boxes": self.manager.describe_boxes() if self.manager else [],
            "gateway": list(self.acceptor.address[:2]) if self.acceptor else None,
            "gateway_router": (list(self.router.address[:2])
                               if self.router and self.router.address else None),
            "encryption_required": self.requires_encryption,
        })
        return summary

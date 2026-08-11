"""Managing the instrument universe without restarting the venue.

The simulator exists to remove friction from CI, so needing a process restart to
add a ticker defeats the point. These cover the three ways the universe changes
at runtime: reloading the CSVs, and adding or removing one instrument.
"""

import os
import shutil
import tempfile
import unittest

from exchangesim.control.commands import CommandError
from exchangesim.venues.japannext import dictionary as D

from .jnxsupport import CLIENT1, VenueHarness, venue_config

#: A symbol not in the shipped universe, so tests cannot collide with it.
NEW_SYMBOL = "1234"

#: base 1000 falls in the 300 band bracket (limits 700-1300) and, with no tier,
#: takes the 0.1 fallback tick -- so "1000" is a legal price for it.
NEW_PRICE = "1000"

HEADER = "symbol,name,lot_size,base_price,tier,shares_outstanding,tradable\n"

BASE_ROWS = [
    "7203,Toyota Motor,100,2845.5,TOPIX100,16000000,y\n",
    "6758,Sony Group,100,13500,TOPIX100,12000000,y\n",
]


class ReferenceHarness(object):
    """A venue whose symbol CSV lives in a temp directory we can rewrite."""

    def __init__(self, rows=None):
        self.tmp = tempfile.mkdtemp(prefix="exsim-ref-")
        self.path = os.path.join(self.tmp, "symbols.csv")
        self.write(rows if rows is not None else BASE_ROWS)
        self.harness = VenueHarness(
            venue_config(reference={"symbols": self.path}))

    def write(self, rows, header=HEADER):
        with open(self.path, "w") as handle:
            handle.write(header)
            for row in rows:
                handle.write(row)

    def close(self):
        self.harness.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class ReloadTest(unittest.TestCase):

    def setUp(self):
        self.fixture = ReferenceHarness()
        self.addCleanup(self.fixture.close)
        self.harness = self.fixture.harness
        self.venue = self.harness.venue

    def symbols(self):
        return [row["symbol"] for row
                in self.harness.command("instruments")["instruments"]]

    def test_the_shipped_universe_is_replaced_by_the_configured_one(self):
        self.assertEqual(["6758", "7203"], self.symbols())

    def test_a_new_row_is_added_and_becomes_orderable(self):
        self.fixture.write(BASE_ROWS + [
            "%s,New Listing,100,%s,,2000000,y\n" % (NEW_SYMBOL, NEW_PRICE)])

        result = self.harness.command("reference.reload")

        self.assertEqual([NEW_SYMBOL], result["added"])
        self.assertIn(NEW_SYMBOL, self.symbols())

        client = self.harness.client(CLIENT1)
        client.new_order("R1", symbol=NEW_SYMBOL, price=NEW_PRICE, quantity=100)

        self.assertEqual(D.ExecType.NEW, client.reports()[0].get(D.EXEC_TYPE))

    def test_a_new_row_gets_a_book_in_every_market(self):
        self.fixture.write(BASE_ROWS + [
            "%s,New Listing,100,%s,,2000000,y\n" % (NEW_SYMBOL, NEW_PRICE)])
        self.harness.command("reference.reload")

        for name in self.venue.markets:
            self.assertIn(NEW_SYMBOL, self.venue.markets[name].symbols,
                          "no book in market %s" % name)

    def test_a_changed_field_is_reported_as_updated(self):
        self.fixture.write([
            "7203,Toyota Motor,100,3000,TOPIX100,16000000,y\n",
            BASE_ROWS[1],
        ])

        result = self.harness.command("reference.reload")

        self.assertEqual(["7203"], result["updated"])
        self.assertEqual([], result["added"])
        self.assertEqual("3000.0", self.venue.instruments["7203"]
                         .describe(self.venue.codec)["base_price"])

    def test_an_unchanged_file_reports_no_differences(self):
        result = self.harness.command("reference.reload")

        self.assertEqual([], result["added"])
        self.assertEqual([], result["updated"])
        self.assertEqual([], result["withdrawn"])
        self.assertEqual(2, result["instruments"])

    def test_a_withdrawn_row_is_marked_untradable_rather_than_deleted(self):
        self.fixture.write([BASE_ROWS[0]])

        result = self.harness.command("reference.reload")

        self.assertEqual(["6758"], result["withdrawn"])
        # Still present -- live orders may reference it.
        self.assertIn("6758", self.symbols())
        self.assertFalse(self.venue.instruments["6758"].tradable)

    def test_a_withdrawn_row_stops_accepting_orders(self):
        self.fixture.write([BASE_ROWS[0]])
        self.harness.command("reference.reload")

        client = self.harness.client(CLIENT1)
        client.new_order("R2", symbol="6758", price="13500", quantity=100)

        self.assertEqual(D.ExecType.REJECTED,
                         client.reports()[0].get(D.EXEC_TYPE))

    def test_resting_orders_survive_a_reload(self):
        client = self.harness.client(CLIENT1)
        client.new_order("R3", symbol="7203", price="2845.5", quantity=100)
        client.drain()

        self.harness.command("reference.reload")

        self.assertEqual(1, len(self.harness.command("orders")["orders"]))

    def test_the_reloaded_map_is_the_one_the_engine_holds(self):
        """The engine was handed the dict by reference; rebinding would orphan it."""
        self.fixture.write(BASE_ROWS + [
            "%s,New Listing,100,%s,,2000000,y\n" % (NEW_SYMBOL, NEW_PRICE)])
        self.harness.command("reference.reload")

        self.assertIs(self.venue.instruments, self.venue.engine.instruments)
        self.assertIsNotNone(self.venue.engine.instrument(NEW_SYMBOL))

    def test_a_reload_discards_a_runtime_override(self):
        self.harness.command("instrument.set", symbol="7203", tradable=False)

        self.harness.command("reference.reload")

        self.assertTrue(self.venue.instruments["7203"].tradable,
                        "the CSV is authoritative for symbols it contains")

    def test_a_broken_file_is_a_command_error_and_changes_nothing(self):
        self.fixture.write(["7203,Toyota\n"], header="symbol_typo,name\n")

        with self.assertRaises(CommandError) as caught:
            self.harness.command("reference.reload")

        self.assertIn("symbol", str(caught.exception))
        self.assertEqual(["6758", "7203"], self.symbols())

    def test_a_missing_file_is_a_command_error(self):
        os.remove(self.fixture.path)

        with self.assertRaises(CommandError):
            self.harness.command("reference.reload")

        self.assertEqual(["6758", "7203"], self.symbols())


class InstrumentAddTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.venue = self.harness.venue

    def add(self, **overrides):
        args = {"symbol": NEW_SYMBOL, "name": "New Listing",
                "base_price": NEW_PRICE, "shares_outstanding": 2000000}
        args.update(overrides)
        return self.harness.dispatch("instrument.add", args)

    def test_an_added_instrument_appears_in_the_universe(self):
        described = self.add()

        self.assertEqual(NEW_SYMBOL, described["symbol"])
        self.assertIn(NEW_SYMBOL, self.venue.instruments)

    def test_an_added_instrument_takes_the_venue_band_and_tick_tables(self):
        described = self.add()

        # base 1000 -> band 300, and the no-tier fallback tick of 0.1.
        self.assertEqual("700.0", described["price_low"])
        self.assertEqual("1300.0", described["price_high"])
        self.assertEqual("0.1", described["tick_size"])

    def test_an_added_instrument_is_immediately_orderable(self):
        self.add()

        client = self.harness.client(CLIENT1)
        client.new_order("A1", symbol=NEW_SYMBOL, price=NEW_PRICE, quantity=100)

        self.assertEqual(D.ExecType.NEW, client.reports()[0].get(D.EXEC_TYPE))

    def test_an_added_instrument_enforces_its_band(self):
        self.add()

        client = self.harness.client(CLIENT1)
        client.new_order("A2", symbol=NEW_SYMBOL, price="1400", quantity=100)

        report = client.reports()[0]
        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         report.get(D.ORD_REJ_REASON))

    def test_an_added_instrument_gets_a_book_in_every_market(self):
        self.add()

        for name in self.venue.markets:
            self.assertIn(NEW_SYMBOL, self.venue.markets[name].symbols)

    def test_the_lot_size_is_honoured(self):
        self.add(lot_size=1000)

        client = self.harness.client(CLIENT1)
        client.new_order("A3", symbol=NEW_SYMBOL, price=NEW_PRICE, quantity=100)

        self.assertEqual(str(D.OrdRejReason.INCORRECT_QUANTITY),
                         client.reports()[0].get(D.ORD_REJ_REASON))

    def test_a_duplicate_symbol_is_refused(self):
        self.add()

        with self.assertRaises(CommandError) as caught:
            self.add()

        self.assertEqual("conflict", caught.exception.code)

    def test_an_empty_symbol_is_refused(self):
        with self.assertRaises(CommandError):
            self.harness.command("instrument.add", symbol="   ")

    def test_an_unparseable_base_price_is_refused(self):
        with self.assertRaises(CommandError) as caught:
            self.add(base_price="1000.005")

        self.assertIn("base_price", str(caught.exception))

    def test_an_instrument_can_be_added_untradable(self):
        described = self.add(tradable=False)
        self.assertFalse(described["tradable"])


class InstrumentRemoveTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.venue = self.harness.venue
        self.client = self.harness.client(CLIENT1)

    def test_an_unused_instrument_is_removed_everywhere(self):
        result = self.harness.command("instrument.remove", symbol="6758")

        self.assertTrue(result["removed"])
        self.assertNotIn("6758", self.venue.instruments)
        for name in self.venue.markets:
            self.assertNotIn("6758", self.venue.markets[name].symbols)

    def test_a_removed_instrument_is_no_longer_orderable(self):
        self.harness.command("instrument.remove", symbol="6758")

        self.client.new_order("D1", symbol="6758", price="13500", quantity=100)

        report = self.client.reports()[0]
        self.assertEqual(str(D.OrdRejReason.UNKNOWN_SYMBOL),
                         report.get(D.ORD_REJ_REASON))

    def test_removal_is_refused_while_an_order_rests(self):
        self.client.new_order("D2", symbol="7203", price="2845.5", quantity=100)
        self.client.drain()

        with self.assertRaises(CommandError) as caught:
            self.harness.command("instrument.remove", symbol="7203")

        self.assertEqual("conflict", caught.exception.code)
        self.assertIn("7203", self.venue.instruments)

    def test_removal_succeeds_once_the_orders_are_cancelled(self):
        self.client.new_order("D3", symbol="7203", price="2845.5", quantity=100)
        self.client.drain()
        self.harness.command("orders.cancel_all", symbol="7203")

        self.harness.command("instrument.remove", symbol="7203")

        self.assertNotIn("7203", self.venue.instruments)

    def test_a_filled_order_does_not_block_removal(self):
        """Only *live* orders matter; a terminated one is history."""
        buyer = self.harness.client(CLIENT1)
        seller = self.harness.client("CLIENT2")
        buyer.new_order("D4", symbol="7203", price="2845.5", quantity=100)
        seller.new_order("D5", side=D.SideValue.SELL, symbol="7203",
                         price="2845.5", quantity=100)
        buyer.drain()
        seller.drain()

        self.harness.command("instrument.remove", symbol="7203")

        self.assertNotIn("7203", self.venue.instruments)

    def test_removing_an_unknown_symbol_reports_not_found(self):
        with self.assertRaises(CommandError) as caught:
            self.harness.command("instrument.remove", symbol="0000")

        self.assertEqual("not_found", caught.exception.code)

    def test_statistics_and_tape_go_with_it(self):
        market = self.venue.markets["DAY"]
        self.harness.command("instrument.remove", symbol="6758")

        self.assertIsNone(market.data.statistics("6758"))
        self.assertIsNone(market.data.trades("6758"))


if __name__ == "__main__":
    unittest.main()

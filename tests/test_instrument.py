"""Reference data: price bands, tick tables, instrument limits and CSV loading.

The band and tick figures used here are taken from the appendices of
JNX_Trading_Rules_Equities_2.02.
"""

import os
import shutil
import tempfile
import unittest

from exchangesim.core.instrument import (
    BandTable,
    Instrument,
    ReferenceDataError,
    TickTable,
    load_band_table,
    load_instruments,
    load_tick_table,
    symbol_key,
)
from exchangesim.core.prices import PriceCodec

CODEC = PriceCodec(decimals=1)


def units(text):
    return CODEC.parse(text)


def jnx_band_table():
    """The first rows of Appendix 1: Price Range (J-Market, X-Market)."""
    return BandTable([
        (units("0"), units("30")),
        (units("100"), units("50")),
        (units("200"), units("80")),
        (units("500"), units("100")),
        (units("700"), units("150")),
        (units("1000"), units("300")),
        (units("1500"), units("400")),
        (units("2000"), units("500")),
        (units("3000"), units("700")),
    ])


def jnx_tick_table():
    """Appendix 3: J-Market tick sizes, by TOPIX tier."""
    return TickTable([
        (units("0"), {"TOPIX100": units("0.1"), None: units("0.1")}),
        (units("3000"), {"TOPIX100": units("0.5"), None: units("1")}),
        (units("5000"), {"TOPIX100": units("1"), None: units("1")}),
    ])


class BandTableTest(unittest.TestCase):

    def setUp(self):
        self.table = jnx_band_table()

    def test_the_lowest_bracket_applies_below_the_first_threshold(self):
        self.assertEqual(units("30"), self.table.band_for(units("50")))

    def test_a_threshold_is_inclusive_of_its_lower_bound(self):
        self.assertEqual(units("50"), self.table.band_for(units("100")))

    def test_a_price_just_below_a_threshold_uses_the_lower_bracket(self):
        self.assertEqual(units("30"), self.table.band_for(units("99.9")))

    def test_the_top_bracket_extends_upwards(self):
        self.assertEqual(units("700"), self.table.band_for(units("999999")))

    def test_limits_are_symmetric_around_the_base_price(self):
        low, high = self.table.limits_for(units("2845.5"))
        self.assertEqual(units("2345.5"), low)
        self.assertEqual(units("3345.5"), high)

    def test_the_lower_limit_never_reaches_zero(self):
        low, _high = self.table.limits_for(units("10"))
        self.assertEqual(1, low)

    def test_a_table_must_start_at_zero(self):
        with self.assertRaises(ReferenceDataError):
            BandTable([(units("100"), units("50"))])

    def test_an_empty_table_is_rejected(self):
        with self.assertRaises(ReferenceDataError):
            BandTable([])


class TickTableTest(unittest.TestCase):

    def setUp(self):
        self.table = jnx_tick_table()

    def test_tiers_can_differ_at_the_same_price(self):
        self.assertEqual(units("0.5"),
                         self.table.tick_for(units("4000"), "TOPIX100"))
        self.assertEqual(units("1"),
                         self.table.tick_for(units("4000"), "OTHER"))

    def test_an_unnamed_tier_falls_back_to_the_default_row(self):
        self.assertEqual(units("1"), self.table.tick_for(units("4000"), None))

    def test_the_lowest_bracket_applies_below_the_first_threshold(self):
        self.assertEqual(units("0.1"),
                         self.table.tick_for(units("1000"), "TOPIX100"))

    def test_a_missing_tier_without_a_default_is_an_error(self):
        table = TickTable([(0, {"TOPIX100": units("0.1")})])
        with self.assertRaises(ReferenceDataError):
            table.tick_for(units("100"), "OTHER")


class InstrumentTest(unittest.TestCase):

    def _instrument(self, **kwargs):
        settings = dict(symbol="7203", lot_size=100, base_price=units("2845.5"),
                        tier="TOPIX100", band_table=jnx_band_table(),
                        tick_table=jnx_tick_table())
        settings.update(kwargs)
        return Instrument(**settings)

    def test_price_limits_come_from_the_band_table(self):
        self.assertEqual((units("2345.5"), units("3345.5")),
                         self._instrument().price_limits)

    def test_prices_inside_the_band_are_accepted(self):
        instrument = self._instrument()
        self.assertTrue(instrument.is_within_band(units("2845.5")))
        self.assertTrue(instrument.is_within_band(units("2345.5")))
        self.assertTrue(instrument.is_within_band(units("3345.5")))

    def test_prices_outside_the_band_are_rejected(self):
        instrument = self._instrument()
        self.assertFalse(instrument.is_within_band(units("2345.4")))
        self.assertFalse(instrument.is_within_band(units("3345.6")))

    def test_an_override_replaces_the_table(self):
        instrument = self._instrument()
        instrument.band_override = (units("2800"), units("2900"))

        self.assertEqual((units("2800"), units("2900")), instrument.price_limits)
        self.assertFalse(instrument.is_within_band(units("2345.5")))

    def test_an_instrument_without_a_base_price_has_no_band(self):
        # An IPO before its first print legitimately has no reference price.
        instrument = self._instrument(base_price=None)
        self.assertIsNone(instrument.price_limits)
        self.assertTrue(instrument.is_within_band(units("999999")))

    def test_tick_size_uses_the_instrument_tier(self):
        instrument = self._instrument()
        self.assertEqual(units("0.5"), instrument.tick_size(units("4000")))

    def test_on_tick_prices_are_accepted(self):
        instrument = self._instrument()
        self.assertTrue(instrument.is_on_tick(units("4000.5")))

    def test_off_tick_prices_are_rejected(self):
        instrument = self._instrument()
        self.assertFalse(instrument.is_on_tick(units("4000.1")))

    def test_round_lots(self):
        instrument = self._instrument(lot_size=100)
        self.assertTrue(instrument.is_round_lot(300))
        self.assertFalse(instrument.is_round_lot(150))

    def test_max_quantity_is_five_percent_of_shares_outstanding(self):
        instrument = self._instrument(shares_outstanding=1000000)
        self.assertEqual(50000, instrument.max_quantity)

    def test_no_shares_outstanding_means_no_quantity_cap(self):
        self.assertIsNone(self._instrument(shares_outstanding=0).max_quantity)

    def test_describe_renders_prices_through_the_codec(self):
        described = self._instrument().describe(CODEC)

        self.assertEqual("2845.5", described["base_price"])
        self.assertEqual("2345.5", described["price_low"])
        self.assertEqual("3345.5", described["price_high"])


class SymbolOrderTest(unittest.TestCase):
    """A stock code is a number, so a listing has to read like one."""

    def test_numeric_codes_sort_as_numbers(self):
        # HKEX codes are carried unpadded, so plain string order would put
        # 1299 before 179 and 28 after 2318.
        codes = ["1299", "179", "28", "2318", "1", "9988", "700"]
        self.assertEqual(["1", "28", "179", "700", "1299", "2318", "9988"],
                         sorted(codes, key=symbol_key))

    def test_fixed_width_codes_are_unaffected(self):
        # Japannext's are all four digits, where the two orders agree.
        codes = ["9984", "7203", "6758"]
        self.assertEqual(sorted(codes), sorted(codes, key=symbol_key))

    def test_a_mixed_universe_still_sorts(self):
        codes = ["700", "ABC", "1", "ZZ"]
        self.assertEqual(["1", "700", "ABC", "ZZ"],
                         sorted(codes, key=symbol_key))


class CsvLoadingTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exsim-ref-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name, content):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def test_band_table_loads_from_csv(self):
        path = self._write("bands.csv",
                           "lower_bound,band\n0,30\n100,50\n200,80\n")

        table = load_band_table(path, CODEC)

        self.assertEqual(3, len(table))
        self.assertEqual(units("50"), table.band_for(units("150")))

    def test_tick_table_loads_tiers_from_csv(self):
        path = self._write(
            "ticks.csv",
            "lower_bound,tier,tick\n0,,0.1\n3000,TOPIX100,0.5\n3000,,1\n")

        table = load_tick_table(path, CODEC)

        self.assertEqual(units("0.5"), table.tick_for(units("4000"), "TOPIX100"))
        self.assertEqual(units("1"), table.tick_for(units("4000"), "OTHER"))

    def test_instruments_load_with_their_reference_tables(self):
        path = self._write(
            "symbols.csv",
            "symbol,name,lot_size,base_price,tier,shares_outstanding\n"
            "7203,Toyota,100,2845.5,TOPIX100,1000000\n"
            "9984,SoftBank,100,8500,TOPIX100,500000\n")

        instruments = load_instruments(path, CODEC, jnx_band_table(),
                                       jnx_tick_table())

        self.assertEqual({"7203", "9984"}, set(instruments))
        toyota = instruments["7203"]
        self.assertEqual("Toyota", toyota.name)
        self.assertEqual(units("2845.5"), toyota.base_price)
        self.assertEqual(50000, toyota.max_quantity)
        self.assertEqual((units("2345.5"), units("3345.5")), toyota.price_limits)

    def test_comment_lines_are_ignored(self):
        path = self._write(
            "symbols.csv",
            "# tradable universe, refreshed daily\n"
            "symbol,lot_size\n"
            "# Toyota\n"
            "7203,100\n")

        instruments = load_instruments(path, CODEC)

        self.assertEqual(["7203"], list(instruments))

    def test_a_missing_column_is_reported_with_the_filename(self):
        path = self._write("bands.csv", "lower_bound\n0\n")

        with self.assertRaises(ReferenceDataError) as caught:
            load_band_table(path, CODEC)

        self.assertIn("band", str(caught.exception))
        self.assertIn("bands.csv", str(caught.exception))

    def test_a_missing_file_is_reported(self):
        with self.assertRaises(ReferenceDataError):
            load_band_table(os.path.join(self.tmp, "absent.csv"), CODEC)

    def test_optional_columns_take_defaults(self):
        path = self._write("symbols.csv", "symbol\n7203\n")

        instrument = load_instruments(path, CODEC)["7203"]

        self.assertEqual(100, instrument.lot_size)
        self.assertIsNone(instrument.base_price)
        self.assertTrue(instrument.tradable)

    def test_an_instrument_can_be_marked_untradable(self):
        path = self._write("symbols.csv", "symbol,tradable\n7203,no\n")

        self.assertFalse(load_instruments(path, CODEC)["7203"].tradable)


if __name__ == "__main__":
    unittest.main()

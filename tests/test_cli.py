"""CLI: rendering, argument handling and exit codes.

Exit codes are asserted deliberately -- CI scripts branch on them, so they are
part of the interface.
"""

import io
import sys
import unittest

from exchangesim.cli import exsim
from exchangesim.cli.format import render_pairs, render_table
from exchangesim.control.commands import CommandError, E_NOT_FOUND

from .support import ServerThread, StubVenue


class RenderTableTest(unittest.TestCase):

    def test_columns_are_padded_to_the_widest_cell(self):
        rows = [{"symbol": "7203", "name": "Toyota"},
                {"symbol": "9984", "name": "SoftBank Group"}]

        lines = render_table(rows, ["symbol", "name"]).split("\n")

        self.assertEqual("symbol  name", lines[0])
        self.assertTrue(lines[1].startswith("------"))
        self.assertIn("Toyota", lines[2])

    def test_numeric_columns_can_be_right_aligned(self):
        rows = [{"qty": 5}, {"qty": 1000}]

        lines = render_table(rows, ["qty"], align={"qty": "r"}).split("\n")

        self.assertEqual("   5", lines[2])
        self.assertEqual("1000", lines[3])

    def test_an_empty_result_says_so(self):
        self.assertEqual("(none)", render_table([]))

    def test_missing_keys_render_as_blank(self):
        rows = [{"a": 1, "b": 2}, {"a": 3}]
        self.assertIn("3", render_table(rows, ["a", "b"]))

    def test_columns_default_to_first_seen_order(self):
        lines = render_table([{"z": 1, "a": 2}]).split("\n")
        self.assertEqual(["z", "a"], lines[0].split())

    def test_booleans_render_as_yes_and_no(self):
        table = render_table([{"tradable": True}, {"tradable": False}])
        self.assertIn("yes", table)
        self.assertIn("no", table)

    def test_none_renders_as_blank_not_the_word_none(self):
        self.assertNotIn("None", render_table([{"bid": None}]))

    def test_nested_values_render_as_compact_json(self):
        self.assertIn('{"7203":"HALTED"}',
                      render_table([{"overrides": {"7203": "HALTED"}}]))


class RenderPairsTest(unittest.TestCase):

    def test_keys_are_aligned(self):
        lines = render_pairs({"bid": "100", "bid_qty": 5},
                             ["bid", "bid_qty"]).split("\n")

        self.assertEqual("bid     : 100", lines[0])
        self.assertEqual("bid_qty : 5", lines[1])

    def test_order_is_honoured(self):
        lines = render_pairs({"a": 1, "b": 2}, ["b", "a"]).split("\n")
        self.assertTrue(lines[0].startswith("b"))

    def test_empty_mapping(self):
        self.assertEqual("(none)", render_pairs({}))


class RenderBookTest(unittest.TestCase):

    def test_bids_and_asks_sit_side_by_side(self):
        book = {
            "bids": [{"price": "2845.5", "quantity": 100, "orders": 1}],
            "asks": [{"price": "2846.0", "quantity": 300, "orders": 2}],
        }

        lines = exsim.render_book(book).split("\n")

        self.assertIn("2845.5", lines[2])
        self.assertIn("2846.0", lines[2])

    def test_uneven_sides_leave_blanks(self):
        book = {
            "bids": [{"price": "2845.5", "quantity": 100, "orders": 1},
                     {"price": "2845.0", "quantity": 200, "orders": 1}],
            "asks": [{"price": "2846.0", "quantity": 300, "orders": 1}],
        }

        lines = exsim.render_book(book).split("\n")

        self.assertEqual(4, len(lines))
        self.assertIn("2845.0", lines[3])

    def test_an_empty_book_says_so(self):
        self.assertEqual("(empty book)", exsim.render_book({"bids": [], "asks": []}))


class GuessLayoutTest(unittest.TestCase):

    def test_a_wrapped_list_of_objects_becomes_a_table(self):
        rendered = exsim._guess_layout({"markets": [{"market": "DAY"}]})
        self.assertIn("market", rendered)
        self.assertIn("DAY", rendered)

    def test_a_flat_object_becomes_aligned_pairs(self):
        self.assertIn("bid     : 100",
                      exsim._guess_layout({"bid": "100", "bid_qty": 5}))

    def test_a_nested_object_falls_back_to_json(self):
        rendered = exsim._guess_layout({"a": {"b": 1}, "c": 2})
        self.assertIn('"b"', rendered)


class ArgumentTest(unittest.TestCase):

    def setUp(self):
        self.parser = exsim.build_parser()

    def test_json_may_precede_the_subcommand(self):
        self.assertTrue(self.parser.parse_args(["--json", "ping"]).as_json)

    def test_json_may_follow_the_subcommand(self):
        # The original CLI only accepted the flag before the subcommand,
        # which is not where anyone types it.
        self.assertTrue(self.parser.parse_args(["ping", "--json"]).as_json)

    def test_market_has_a_short_option(self):
        opts = self.parser.parse_args(["book", "7203", "-m", "DAY"])
        self.assertEqual("DAY", opts.market)

    def test_book_takes_a_depth(self):
        opts = self.parser.parse_args(["book", "7203", "--depth", "10"])
        self.assertEqual(10, opts.depth)

    def test_state_set_takes_a_value(self):
        opts = self.parser.parse_args(["state", "set", "OPEN", "-m", "DAY"])
        self.assertEqual("set", opts.action)
        self.assertEqual("OPEN", opts.value)

    def test_an_unknown_state_action_is_rejected(self):
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["state", "sleep"])
        finally:
            sys.stderr = saved

    def test_orders_defaults_to_live_only(self):
        self.assertFalse(self.parser.parse_args(["orders"]).all)

    def test_every_subcommand_has_a_handler(self):
        actions = [action for action in self.parser._subparsers._actions
                   if hasattr(action, "choices") and action.choices]
        names = set(actions[-1].choices)
        self.assertEqual(names, set(exsim._HANDLERS),
                         "subcommands and handlers disagree")


class MainTest(unittest.TestCase):
    """Drives main() against a real control server over a real socket."""

    def setUp(self):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = self.stdout, self.stderr
        self.addCleanup(self._restore)

    def _restore(self):
        sys.stdout, sys.stderr = self._saved

    def _run(self, harness, *argv):
        return exsim.main(["--port", str(harness.port)] + list(argv))

    def test_ping_succeeds(self):
        with ServerThread(venue=StubVenue("JNX-SIM")) as harness:
            code = self._run(harness, "ping")

        self.assertEqual(exsim.EXIT_OK, code)
        self.assertIn("JNX-SIM", self.stdout.getvalue())

    def test_json_mode_emits_parseable_output(self):
        import json

        with ServerThread(venue=StubVenue()) as harness:
            self._run(harness, "ping", "--json")

        self.assertTrue(json.loads(self.stdout.getvalue())["pong"])

    def test_a_rejected_command_exits_one(self):
        with ServerThread() as harness:
            code = self._run(harness, "call", "no.such.command")

        self.assertEqual(exsim.EXIT_COMMAND_FAILED, code)
        self.assertIn("unknown_command", self.stderr.getvalue())

    def test_a_not_found_error_also_exits_one(self):
        def extra(registry):
            @registry.add("boom", "")
            def _boom(context, args):
                raise CommandError("no such symbol", E_NOT_FOUND)

        with ServerThread(extra=extra) as harness:
            code = self._run(harness, "call", "boom")

        self.assertEqual(exsim.EXIT_COMMAND_FAILED, code)
        self.assertIn("not_found", self.stderr.getvalue())

    def test_an_unreachable_server_exits_three(self):
        # Port 1 is reserved and never listening.
        code = exsim.main(["--port", "1", "--timeout", "1", "ping"])

        self.assertEqual(exsim.EXIT_TRANSPORT, code)

    def test_no_subcommand_prints_help_and_exits_two(self):
        self.assertEqual(exsim.EXIT_USAGE, exsim.main([]))
        self.assertIn("usage", self.stdout.getvalue())

    def test_malformed_call_arguments_exit_two(self):
        with ServerThread() as harness:
            code = self._run(harness, "call", "ping", "{not json}")

        self.assertEqual(exsim.EXIT_USAGE, code)

    def test_call_arguments_must_be_an_object(self):
        with ServerThread() as harness:
            code = self._run(harness, "call", "ping", "[1,2]")

        self.assertEqual(exsim.EXIT_USAGE, code)

    def test_state_set_without_a_value_exits_two(self):
        with ServerThread() as harness:
            code = self._run(harness, "state", "set")

        self.assertEqual(exsim.EXIT_USAGE, code)
        self.assertIn("needs a value", self.stderr.getvalue())

    def test_state_clear_without_a_symbol_exits_two(self):
        with ServerThread() as harness:
            code = self._run(harness, "state", "clear", "-m", "DAY")

        self.assertEqual(exsim.EXIT_USAGE, code)

    def test_commands_lists_the_registry(self):
        with ServerThread() as harness:
            code = self._run(harness, "commands")

        self.assertEqual(exsim.EXIT_OK, code)
        self.assertIn("ping", self.stdout.getvalue())

    def test_empty_arguments_are_omitted_rather_than_sent_as_null(self):
        seen = {}

        def extra(registry):
            @registry.add("book", "")
            def _book(context, args):
                seen.update(args)
                return {"bids": [], "asks": []}

        with ServerThread(extra=extra) as harness:
            self._run(harness, "book", "7203")

        self.assertNotIn("market", seen)
        self.assertEqual("7203", seen["symbol"])


if __name__ == "__main__":
    unittest.main()

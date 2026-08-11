"""Scenario runner: loading, field matching, and end-to-end execution.

The end-to-end tests run a real simulator on ephemeral ports and drive it with
the real runner over real sockets -- the same path CI takes.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from exchangesim.control.builtin import register as register_builtin
from exchangesim.control.commands import CommandRegistry
from exchangesim.control.server import ControlServer
from exchangesim.control.subscriptions import Publisher
from exchangesim.core.clock import RealClock
from exchangesim.core.reactor import Reactor
from exchangesim.fix.message import Message
from exchangesim.scenario.runner import (
    Runner,
    Scenario,
    ScenarioError,
    _matches,
    main,
)
from exchangesim.venues.japannext.venue import JapannextVenue

from .jnxsupport import CLIENT1, CLIENT2, venue_config


class FieldMatchingTest(unittest.TestCase):

    def setUp(self):
        self.message = Message().set(35, "8").set(150, "0").set(11, "B1")

    def test_all_named_tags_must_agree(self):
        self.assertTrue(_matches(self.message, {"35": "8", "150": "0"}))

    def test_one_disagreement_fails_the_match(self):
        self.assertFalse(_matches(self.message, {"35": "8", "150": "4"}))

    def test_an_absent_tag_fails_the_match(self):
        self.assertFalse(_matches(self.message, {"99": "x"}))

    def test_values_are_compared_as_strings(self):
        self.assertTrue(_matches(Message().set(38, "100"), {"38": 100}))

    def test_null_asserts_a_tag_is_absent(self):
        self.assertTrue(_matches(self.message, {"58": None}))
        self.assertFalse(_matches(self.message, {"11": None}))

    def test_an_empty_field_map_matches_anything(self):
        self.assertTrue(_matches(self.message, {}))


class LoadingTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exsim-scenario-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, data, name="s.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            if isinstance(data, str):
                handle.write(data)
            else:
                json.dump(data, handle)
        return path

    def test_a_valid_scenario_loads(self):
        path = self._write({"name": "example", "steps": [{"sleep": 0}]})

        scenario = Scenario.load(path)

        self.assertEqual("example", scenario.name)
        self.assertEqual(1, len(scenario.steps))

    def test_the_filename_is_the_default_name(self):
        path = self._write({"steps": [{"sleep": 0}]}, "unnamed.json")
        self.assertEqual("unnamed.json", Scenario.load(path).name)

    def test_missing_steps_is_an_error(self):
        with self.assertRaises(ScenarioError):
            Scenario.load(self._write({"name": "x"}))

    def test_empty_steps_is_an_error(self):
        with self.assertRaises(ScenarioError):
            Scenario.load(self._write({"steps": []}))

    def test_invalid_json_names_the_file(self):
        path = self._write("{not json", "broken.json")
        with self.assertRaises(ScenarioError) as caught:
            Scenario.load(path)
        self.assertIn("broken.json", str(caught.exception))

    def test_a_missing_file_is_reported(self):
        with self.assertRaises(ScenarioError):
            Scenario.load(os.path.join(self.tmp, "absent.json"))


class LiveSimulator(object):
    """A Japannext venue plus control plane on ephemeral ports, in a thread."""

    def __init__(self):
        self.reactor = Reactor(RealClock())
        self.publisher = Publisher()
        self.venue = JapannextVenue(
            venue_config(markets=[{"name": "DAY", "state": "CLOSED"}],
                         sessions=[
                             {"target_comp_id": CLIENT1, "default_sub_id": "DAY",
                              "markets": ["DAY"]},
                             {"target_comp_id": CLIENT2, "default_sub_id": "DAY",
                              "markets": ["DAY"]}]),
            self.reactor, self.publisher)
        self.venue.start()

        self.registry = CommandRegistry()
        register_builtin(self.registry)
        self.venue.register_commands(self.registry)
        self.control = ControlServer(self.reactor, self.registry,
                                     self.publisher, venue=self.venue)
        self.control_address = self.control.start("127.0.0.1", 0)
        self._thread = None

    @property
    def fix_port(self):
        return self.venue.acceptor.address[1]

    @property
    def control_port(self):
        return self.control_address[1]

    def __enter__(self):
        self._thread = threading.Thread(
            target=self.reactor.run, kwargs={"poll_interval": 0.01})
        self._thread.daemon = True
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.reactor.stop()
        if self._thread:
            self._thread.join(timeout=5.0)
        self.control.stop()
        self.venue.stop()
        self.reactor.close()
        return False


class EndToEndTest(unittest.TestCase):
    """Real sockets, real runner, real simulator."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exsim-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _scenario(self, steps, sessions=None, name="test"):
        path = os.path.join(self.tmp, "s.json")
        with open(path, "w") as handle:
            json.dump({"name": name,
                       "sessions": sessions or {"t": {"comp_id": CLIENT1}},
                       "steps": steps}, handle)
        return Scenario.load(path)

    def _runner(self, simulator):
        return Runner("127.0.0.1", simulator.fix_port, simulator.control_port,
                      "JNXSIM")

    def test_a_passing_scenario_reports_success(self):
        steps = [
            {"control": "state.set", "args": {"market": "DAY", "state": "OPEN"}},
            {"logon": "t", "reset": True},
            {"clear": "t", "for": 0.2},
            {"send": "t", "fields": {"35": "D", "11": "E1", "55": "7203",
                                     "54": "1", "38": "100", "40": "2",
                                     "44": "2845.5"}},
            {"expect": "t", "fields": {"35": "8", "150": "0", "11": "E1",
                                       "151": "100"}},
            {"logout": "t"},
        ]

        with LiveSimulator() as simulator:
            ok, detail = self._runner(simulator).run(self._scenario(steps))

        self.assertTrue(ok, detail)

    def test_a_failing_expectation_names_the_step(self):
        steps = [
            {"control": "state.set", "args": {"market": "DAY", "state": "OPEN"}},
            {"logon": "t", "reset": True},
            {"clear": "t", "for": 0.2},
            {"send": "t", "fields": {"35": "D", "11": "E2", "55": "7203",
                                     "54": "1", "38": "100", "40": "2",
                                     "44": "2845.5"}},
            # The order is accepted, not rejected.
            {"expect": "t", "fields": {"35": "8", "150": "8"}, "timeout": 0.5},
        ]

        with LiveSimulator() as simulator:
            ok, detail = self._runner(simulator).run(self._scenario(steps))

        self.assertFalse(ok)
        self.assertIn("step 5", detail)
        self.assertIn("expect t", detail)

    def test_expect_none_fails_when_something_arrives(self):
        steps = [
            {"control": "state.set", "args": {"market": "DAY", "state": "OPEN"}},
            {"logon": "t", "reset": True},
            {"clear": "t", "for": 0.2},
            {"send": "t", "fields": {"35": "D", "11": "E3", "55": "7203",
                                     "54": "1", "38": "100", "40": "2",
                                     "44": "2845.5"}},
            {"expect_none": "t", "for": 0.4},
        ]

        with LiveSimulator() as simulator:
            ok, detail = self._runner(simulator).run(self._scenario(steps))

        self.assertFalse(ok)
        self.assertIn("unexpected message", detail)

    def test_expect_none_passes_when_nothing_arrives(self):
        steps = [
            {"logon": "t", "reset": True},
            {"clear": "t", "for": 0.2},
            {"expect_none": "t", "for": 0.3},
        ]

        with LiveSimulator() as simulator:
            ok, detail = self._runner(simulator).run(self._scenario(steps))

        self.assertTrue(ok, detail)

    def test_a_control_result_can_be_asserted(self):
        steps = [
            {"control": "state.set",
             "args": {"market": "DAY", "state": "OPEN"},
             "expect_result": {"state": "OPEN"}},
        ]

        with LiveSimulator() as simulator:
            ok, detail = self._runner(simulator).run(self._scenario(steps))

        self.assertTrue(ok, detail)

    def test_a_wrong_control_result_fails(self):
        steps = [
            {"control": "state.set",
             "args": {"market": "DAY", "state": "OPEN"},
             "expect_result": {"state": "CLOSED"}},
        ]

        with LiveSimulator() as simulator:
            ok, detail = self._runner(simulator).run(self._scenario(steps))

        self.assertFalse(ok)
        self.assertIn("expected", detail)

    def test_an_unknown_session_is_reported(self):
        steps = [{"send": "nobody", "fields": {"35": "0"}}]

        with LiveSimulator() as simulator:
            ok, detail = self._runner(simulator).run(self._scenario(steps))

        self.assertFalse(ok)
        self.assertIn("unknown session", detail)

    def test_an_unrecognised_step_is_reported(self):
        with LiveSimulator() as simulator:
            ok, detail = self._runner(simulator).run(
                self._scenario([{"frobnicate": "t"}]))

        self.assertFalse(ok)
        self.assertIn("unrecognised step", detail)

    def test_two_sessions_can_trade(self):
        steps = [
            {"control": "state.set", "args": {"market": "DAY", "state": "OPEN"}},
            {"logon": "seller", "reset": True},
            {"logon": "buyer", "reset": True},
            {"clear": "seller", "for": 0.2},
            {"clear": "buyer", "for": 0.2},
            {"send": "seller", "fields": {"35": "D", "11": "E9-S", "55": "7203",
                                          "54": "2", "38": "100", "40": "2",
                                          "44": "2846.0"}},
            {"expect": "seller", "fields": {"35": "8", "150": "0"}},
            {"send": "buyer", "fields": {"35": "D", "11": "E9-B", "55": "7203",
                                         "54": "1", "38": "100", "40": "2",
                                         "44": "2846.0"}},
            {"expect": "buyer", "fields": {"35": "8", "150": "2", "851": "2"}},
            {"expect": "seller", "fields": {"35": "8", "150": "2", "851": "1"}},
        ]
        sessions = {"buyer": {"comp_id": CLIENT1},
                    "seller": {"comp_id": CLIENT2}}

        with LiveSimulator() as simulator:
            ok, detail = self._runner(simulator).run(
                self._scenario(steps, sessions))

        self.assertTrue(ok, detail)


class MainExitCodeTest(unittest.TestCase):
    """CI branches on these."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exsim-main-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # main() reports results on stdout/stderr by design; capture it so the
        # test run stays readable.
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        self.addCleanup(self._restore)

    def _restore(self):
        sys.stdout, sys.stderr = self._saved

    def _write(self, steps, name):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            json.dump({"name": name,
                       "sessions": {"t": {"comp_id": CLIENT1}},
                       "steps": steps}, handle)
        return path

    def _argv(self, simulator, path):
        return [path,
                "--fix-port", str(simulator.fix_port),
                "--control-port", str(simulator.control_port)]

    def test_all_passing_exits_zero(self):
        path = self._write(
            [{"control": "state.set",
              "args": {"market": "DAY", "state": "OPEN"}}], "ok.json")

        with LiveSimulator() as simulator:
            self.assertEqual(0, main(self._argv(simulator, path)))

    def test_a_failure_exits_one(self):
        path = self._write(
            [{"logon": "t", "reset": True},
             {"expect": "t", "fields": {"35": "ZZ"}, "timeout": 0.3}],
            "bad.json")

        with LiveSimulator() as simulator:
            self.assertEqual(1, main(self._argv(simulator, path)))

    def test_no_matching_files_exits_two(self):
        self.assertEqual(2, main([os.path.join(self.tmp, "nothing-*.json")]))

    def test_an_unreachable_simulator_exits_three(self):
        path = self._write([{"control": "state.set", "args": {}}], "x.json")
        self.assertEqual(3, main([path, "--control-port", "1"]))


if __name__ == "__main__":
    unittest.main()

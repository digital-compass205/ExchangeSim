"""Runtime wiring: venue resolution, control plane startup, clean shutdown."""

import threading
import unittest

from exchangesim.cli.client import ControlClient
from exchangesim.core.config import Config, ConfigError
from exchangesim.runner.main import Runtime
from exchangesim.venues import registry as venue_registry


class VenueRegistryTest(unittest.TestCase):

    def test_generic_venue_resolves(self):
        self.assertIn("generic", venue_registry.available())

    def test_japannext_is_registered(self):
        self.assertIn("japannext", venue_registry.available())

    def test_unknown_venue_names_the_alternatives(self):
        with self.assertRaises(ConfigError) as caught:
            venue_registry.create("nasdaq", Config({}), None, None)
        self.assertIn("generic", str(caught.exception))


class RuntimeTest(unittest.TestCase):
    """Builds the real Runtime and talks to it over a real socket."""

    def _runtime(self, overrides=None):
        data = {
            "venue": "generic",
            "name": "TEST",
            # Port 0 lets the OS pick, so parallel test runs cannot collide.
            "control": {"host": "127.0.0.1", "port": 0},
        }
        if overrides:
            data.update(overrides)
        runtime = Runtime(Config(data)).build()
        runtime.start()

        thread = threading.Thread(target=runtime.reactor.run, kwargs={"poll_interval": 0.02})
        thread.daemon = True
        thread.start()

        def shutdown():
            runtime.reactor.stop()
            thread.join(timeout=5.0)
            runtime.stop()

        self.addCleanup(shutdown)
        return runtime

    def test_control_plane_answers_after_start(self):
        runtime = self._runtime()
        port = runtime.control.address[1]

        with ControlClient("127.0.0.1", port) as client:
            result = client.call("ping")

        self.assertEqual("TEST", result["venue"])

    def test_venue_is_started_by_the_runtime(self):
        runtime = self._runtime()
        self.assertTrue(runtime.venue.started)

    def test_builtin_commands_are_registered(self):
        runtime = self._runtime()
        self.assertIn("ping", runtime.registry.names())
        self.assertIn("subscribe", runtime.registry.names())

    def test_token_from_config_is_enforced(self):
        runtime = self._runtime({"control": {"host": "127.0.0.1", "port": 0,
                                             "token": "letmein"}})
        port = runtime.control.address[1]

        with ControlClient("127.0.0.1", port, token="letmein") as client:
            self.assertIn("commands", client.call("help"))

    def test_unknown_venue_fails_the_build(self):
        config = Config({"venue": "no-such-exchange"})
        with self.assertRaises(ConfigError):
            Runtime(config).build()

    def test_stop_is_idempotent(self):
        runtime = self._runtime()
        runtime.venue.stop()
        runtime.venue.stop()
        self.assertFalse(runtime.venue.started)


if __name__ == "__main__":
    unittest.main()

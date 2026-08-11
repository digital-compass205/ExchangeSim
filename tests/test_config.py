"""Config loading, dotted lookup and path resolution."""

import json
import os
import shutil
import tempfile
import unittest

from exchangesim.core.config import Config, ConfigError


class ConfigTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exsim-cfg-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, data, name="venue.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            json.dump(data, handle)
        return path

    def test_dotted_lookup(self):
        cfg = Config.load(self._write({"fix": {"port": 9001, "sender": "JNX"}}))
        self.assertEqual(9001, cfg.get("fix.port"))
        self.assertEqual("JNX", cfg.get("fix.sender"))

    def test_missing_key_returns_default(self):
        cfg = Config.load(self._write({"fix": {}}))
        self.assertEqual(30, cfg.get("fix.heartbeat", 30))
        self.assertIsNone(cfg.get("nothing.here.at.all"))

    def test_traversing_through_a_scalar_returns_default(self):
        cfg = Config.load(self._write({"fix": 5}))
        self.assertEqual("fallback", cfg.get("fix.port", "fallback"))

    def test_require_names_the_missing_key(self):
        cfg = Config.load(self._write({}))
        with self.assertRaises(ConfigError) as caught:
            cfg.require("fix.port")
        self.assertIn("fix.port", str(caught.exception))

    def test_require_accepts_falsey_values(self):
        cfg = Config.load(self._write({"fix": {"port": 0}}))
        self.assertEqual(0, cfg.require("fix.port"))

    def test_relative_paths_resolve_against_the_config_directory(self):
        path = self._write({"reference": {"symbols": "data/symbols.csv"}})
        cfg = Config.load(path)
        self.assertEqual(os.path.join(self.tmp, "data", "symbols.csv"),
                         cfg.resolve_path("reference.symbols"))

    def test_absolute_paths_are_left_alone(self):
        absolute = os.path.join(self.tmp, "elsewhere.csv")
        cfg = Config.load(self._write({"reference": {"symbols": absolute}}))
        self.assertEqual(absolute, cfg.resolve_path("reference.symbols"))

    def test_section_shares_the_base_directory(self):
        path = self._write({"fix": {"store": "var/store"}})
        section = Config.load(path).section("fix")
        self.assertEqual(os.path.join(self.tmp, "var", "store"),
                         section.resolve_path("store"))

    def test_section_of_a_scalar_is_rejected(self):
        cfg = Config.load(self._write({"fix": 1}))
        with self.assertRaises(ConfigError):
            cfg.section("fix")

    def test_invalid_json_is_reported_with_the_filename(self):
        path = os.path.join(self.tmp, "broken.json")
        with open(path, "w") as handle:
            handle.write("{not json")
        with self.assertRaises(ConfigError) as caught:
            Config.load(path)
        self.assertIn("broken.json", str(caught.exception))

    def test_missing_file_is_reported(self):
        with self.assertRaises(ConfigError):
            Config.load(os.path.join(self.tmp, "absent.json"))

    def test_top_level_array_is_rejected(self):
        path = os.path.join(self.tmp, "list.json")
        with open(path, "w") as handle:
            json.dump([1, 2], handle)
        with self.assertRaises(ConfigError):
            Config.load(path)


if __name__ == "__main__":
    unittest.main()

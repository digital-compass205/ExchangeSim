"""Log configuration: where lines go, and what stops a file growing forever.

Rotation is done by the writing process rather than by an external tool
because there is no external tool on the target that is guaranteed to be
configured -- and because a daemon that rotates its own file cannot be caught
holding a renamed one.
"""

import logging
import os
import shutil
import tempfile
import unittest

from exchangesim.core import logutil


class ConfigureTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="exsim-log-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = os.path.join(self.root, "venue.log")
        # Every test here rearranges the root logger, so put it back.
        self.addCleanup(self._restore, list(logging.getLogger().handlers),
                        logging.getLogger().level)

    def _restore(self, handlers, level):
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            if handler not in handlers:
                handler.close()
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)

    def _handlers(self):
        return logging.getLogger().handlers

    def test_a_file_handler_is_added_for_a_path(self):
        logutil.configure("INFO", self.path, console=False)
        logging.getLogger("test").info("hello")

        with open(self.path) as handle:
            self.assertIn("hello", handle.read())

    def test_the_directory_is_created(self):
        path = os.path.join(self.root, "deeper", "venue.log")
        logutil.configure("INFO", path, console=False)
        logging.getLogger("test").info("hello")

        self.assertTrue(os.path.isfile(path))

    def test_console_off_leaves_only_the_file(self):
        # A supervised daemon's stderr is captured to a separate file, so a
        # stderr handler would write every line into both of them.
        logutil.configure("INFO", self.path, console=False)
        streams = [h for h in self._handlers()
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)]

        self.assertEqual([], streams)
        self.assertEqual(1, len(self._handlers()))

    def test_console_on_is_the_default(self):
        logutil.configure("INFO", self.path)
        self.assertEqual(2, len(self._handlers()))

    def test_no_file_and_no_console_still_leaves_a_handler(self):
        # Otherwise logging falls back to lastResort, which writes to the very
        # stderr this was meant to keep clean.
        logutil.configure("INFO", None, console=False)
        self.assertEqual(1, len(self._handlers()))
        self.assertIsInstance(self._handlers()[0], logging.NullHandler)

    def test_a_file_is_rotated_at_the_ceiling(self):
        logutil.configure("INFO", self.path, max_bytes=512, backups=2,
                          console=False)
        log = logging.getLogger("test.rotation")
        for index in range(200):
            log.info("filler %03d %s", index, "x" * 40)

        self.assertTrue(os.path.isfile(self.path + ".1"))
        self.assertTrue(os.path.isfile(self.path + ".2"))
        # backups=2 means exactly two, however long it runs.
        self.assertFalse(os.path.exists(self.path + ".3"))
        self.assertLessEqual(os.path.getsize(self.path), 512 + 200)

    def test_zero_max_bytes_never_rotates(self):
        logutil.configure("INFO", self.path, max_bytes=0, console=False)
        log = logging.getLogger("test.unbounded")
        for index in range(200):
            log.info("filler %03d %s", index, "x" * 40)

        self.assertFalse(os.path.exists(self.path + ".1"))
        self.assertGreater(os.path.getsize(self.path), 512)

    def test_reconfiguring_replaces_rather_than_accumulates(self):
        logutil.configure("INFO", self.path, console=False)
        logutil.configure("INFO", self.path, console=False)

        self.assertEqual(1, len(self._handlers()))

    def test_an_unknown_level_names_itself(self):
        with self.assertRaises(ValueError) as caught:
            logutil.configure("CHATTY")
        self.assertIn("CHATTY", str(caught.exception))

    def test_the_level_is_applied(self):
        logutil.configure("WARNING", self.path, console=False)
        log = logging.getLogger("test.level")
        log.info("quiet")
        log.warning("loud")

        with open(self.path) as handle:
            text = handle.read()
        self.assertNotIn("quiet", text)
        self.assertIn("loud", text)


if __name__ == "__main__":
    unittest.main()

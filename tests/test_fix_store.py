"""Store behaviour: sequence persistence, resend retrieval, reset, recovery."""

import os
import shutil
import tempfile
import unittest

from exchangesim.fix.store import FileStore, MemoryStore, session_directory


class StoreContractMixin(object):
    """Behaviour both store implementations must share."""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store().open()
        self.addCleanup(self.store.close)

    def test_sequence_numbers_start_at_one(self):
        self.assertEqual(1, self.store.next_out)
        self.assertEqual(1, self.store.next_in)

    def test_sequence_numbers_are_settable(self):
        self.store.set_next_out(42)
        self.store.set_next_in(17)
        self.assertEqual(42, self.store.next_out)
        self.assertEqual(17, self.store.next_in)

    def test_stored_messages_are_retrieved_by_range(self):
        for seq in range(1, 6):
            self.store.store_outbound(seq, b"msg-%d" % seq)

        self.assertEqual([(2, b"msg-2"), (3, b"msg-3")],
                         self.store.get_outbound(2, 3))

    def test_end_of_zero_means_through_the_end(self):
        for seq in range(1, 4):
            self.store.store_outbound(seq, b"msg-%d" % seq)

        result = self.store.get_outbound(2, 0)

        self.assertEqual([(2, b"msg-2"), (3, b"msg-3")], result)

    def test_range_beyond_what_is_stored_returns_what_exists(self):
        self.store.store_outbound(1, b"only")
        self.assertEqual([(1, b"only")], self.store.get_outbound(1, 99))

    def test_empty_range_returns_nothing(self):
        self.store.store_outbound(1, b"only")
        self.assertEqual([], self.store.get_outbound(5, 9))

    def test_get_outbound_on_empty_store(self):
        self.assertEqual([], self.store.get_outbound(1, 0))

    def test_messages_with_soh_and_newlines_round_trip(self):
        payload = b"8=FIX.4.2\x0135=D\x0158=line1\nline2\x0110=123\x01"
        self.store.store_outbound(1, payload)
        self.assertEqual([(1, payload)], self.store.get_outbound(1, 1))

    def test_reset_clears_messages_and_sequence_numbers(self):
        self.store.store_outbound(1, b"before")
        self.store.set_next_out(10)
        self.store.set_next_in(10)

        self.store.reset()

        self.assertEqual(1, self.store.next_out)
        self.assertEqual(1, self.store.next_in)
        self.assertEqual([], self.store.get_outbound(1, 0))

    def test_store_after_reset_works(self):
        self.store.store_outbound(1, b"before")
        self.store.reset()
        self.store.store_outbound(1, b"after")
        self.assertEqual([(1, b"after")], self.store.get_outbound(1, 0))


class MemoryStoreTest(StoreContractMixin, unittest.TestCase):

    def make_store(self):
        return MemoryStore()


class FileStoreTest(StoreContractMixin, unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="exsim-store-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        StoreContractMixin.setUp(self)

    def make_store(self):
        return FileStore(os.path.join(self.tmp, "JNX-CLIENT"))

    # -- durability, which is the whole point of this implementation --------

    def test_sequence_numbers_survive_reopen(self):
        self.store.set_next_out(57)
        self.store.set_next_in(23)
        self.store.close()

        reopened = self.make_store().open()
        self.addCleanup(reopened.close)

        self.assertEqual(57, reopened.next_out)
        self.assertEqual(23, reopened.next_in)

    def test_messages_survive_reopen_and_remain_resendable(self):
        for seq in range(1, 4):
            self.store.store_outbound(seq, b"8=FIX.4.2\x0134=%d\x01" % seq)
        self.store.set_next_out(4)
        self.store.close()

        reopened = self.make_store().open()
        self.addCleanup(reopened.close)

        self.assertEqual(4, reopened.next_out)
        self.assertEqual([(2, b"8=FIX.4.2\x0134=2\x01"),
                          (3, b"8=FIX.4.2\x0134=3\x01")],
                         reopened.get_outbound(2, 3))

    def test_appending_after_reopen_keeps_earlier_messages(self):
        self.store.store_outbound(1, b"first")
        self.store.close()

        reopened = self.make_store().open()
        self.addCleanup(reopened.close)
        reopened.store_outbound(2, b"second")

        self.assertEqual([(1, b"first"), (2, b"second")],
                         reopened.get_outbound(1, 0))

    def test_reset_survives_reopen(self):
        self.store.store_outbound(1, b"before")
        self.store.set_next_out(9)
        self.store.reset()
        self.store.close()

        reopened = self.make_store().open()
        self.addCleanup(reopened.close)

        self.assertEqual(1, reopened.next_out)
        self.assertEqual([], reopened.get_outbound(1, 0))

    def test_truncated_log_tail_is_ignored_not_fatal(self):
        # Simulates a crash mid-write: the good prefix must still load.
        self.store.store_outbound(1, b"complete")
        self.store.store_outbound(2, b"also-complete")
        self.store.close()

        log_path = os.path.join(self.store.directory, FileStore.LOG_FILE)
        with open(log_path, "ab") as handle:
            handle.write(b"3 100\nonly-a-few-bytes")

        with self.assertLogs("exchangesim.fix.store", level="WARNING"):
            reopened = self.make_store().open()
        self.addCleanup(reopened.close)

        self.assertEqual([(1, b"complete"), (2, b"also-complete")],
                         reopened.get_outbound(1, 0))

    def test_corrupt_state_file_falls_back_to_one(self):
        self.store.set_next_out(5)
        self.store.close()

        state_path = os.path.join(self.store.directory, FileStore.STATE_FILE)
        with open(state_path, "w") as handle:
            handle.write("{ not json")

        with self.assertLogs("exchangesim.fix.store", level="WARNING"):
            reopened = self.make_store().open()
        self.addCleanup(reopened.close)

        self.assertEqual(1, reopened.next_out)

    def test_no_partial_state_file_is_left_behind(self):
        self.store.set_next_out(3)
        entries = os.listdir(self.store.directory)
        self.assertNotIn(FileStore.STATE_FILE + ".tmp", entries)

    def test_directory_is_created_on_demand(self):
        nested = FileStore(os.path.join(self.tmp, "deep", "JNX-CLIENT2")).open()
        self.addCleanup(nested.close)
        self.assertTrue(os.path.isdir(nested.directory))

    def test_open_is_idempotent(self):
        self.store.store_outbound(1, b"kept")
        self.store.open()
        self.assertEqual([(1, b"kept")], self.store.get_outbound(1, 0))


class SessionDirectoryTest(unittest.TestCase):

    def test_comp_ids_form_the_directory_name(self):
        self.assertEqual(os.path.join("var", "JNX-CLIENT1"),
                         session_directory("var", "JNX", "CLIENT1"))

    def test_path_separators_in_comp_ids_are_neutralised(self):
        # A CompID is client-supplied at Logon; it must never escape the root.
        result = session_directory("var", "../../etc", "x/y")
        self.assertEqual(os.path.join("var", ".._.._etc-x_y"), result)
        self.assertNotIn("..%s" % os.sep, result.replace("var" + os.sep, "", 1))


if __name__ == "__main__":
    unittest.main()

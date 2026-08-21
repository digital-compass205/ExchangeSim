"""Process control: what services.json declares, and starting/stopping it.

Most of this is exercised without starting anything -- a service is a name, a
command line and three paths, and all three are worth pinning. The two
lifecycle tests do start a real child process, because the parts that have
actually broken here (detaching, capturing output, telling a live pid from a
recycled one) exist only at that boundary and a fake would assert nothing.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

from exchangesim.core.config import ConfigError
from exchangesim.ctl import process, supervisor
from exchangesim.ctl.main import main as ctl_main, new_lines, tail
from exchangesim.ctl.services import ServiceSet, default_config_path
from exchangesim.ctl.supervisor import (RUNNING, STALE, STOPPED, ServiceError,
                                        Supervisor, rotate)

# A child that stays up until it is stopped and answers on a port, which is
# what `start` waits for. Written as one -c argument so no file is needed.
LISTENER = (
    "import socket,sys,time\n"
    "s=socket.socket()\n"
    "s.bind(('127.0.0.1', PORT))\n"
    "s.listen(5)\n"
    "sys.stdout.write('listening\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(120)\n"
)


class ServiceSetTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="exsim-ctl-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.mkdir(os.path.join(self.root, "config"))

    def _write(self, data, name="services.json"):
        path = os.path.join(self.root, "config", name)
        with open(path, "w") as handle:
            json.dump(data, handle)
        return path

    def _venue_config(self, name, control_port=9101, fix_port=9001):
        path = os.path.join(self.root, "config", "%s.json" % name)
        with open(path, "w") as handle:
            json.dump({"venue": "generic",
                       "fix": {"port": fix_port},
                       "control": {"host": "127.0.0.1", "port": control_port}},
                      handle)
        return path

    def _minimal(self):
        self._venue_config("alpha")
        return self._write({"services": [
            {"name": "alpha", "config": "config/alpha.json"}]})

    def test_paths_resolve_against_the_project_root(self):
        services = ServiceSet.load(self._minimal())
        service = services.services[0]

        self.assertEqual(os.path.join(self.root, "config", "alpha.json"),
                         service.config_path)
        self.assertEqual(os.path.join(self.root, "var", "log", "alpha.log"),
                         service.log_path)
        self.assertEqual(os.path.join(self.root, "var", "run", "alpha.pid"),
                         service.pid_path)

    def test_kind_web_selects_the_board_module(self):
        self._venue_config("alpha")
        path = self._write({"services": [
            {"name": "alpha", "config": "config/alpha.json"},
            {"name": "board", "kind": "web", "config": "config/alpha.json"}]})
        services = ServiceSet.load(path)

        self.assertEqual("exchangesim.runner.main", services.services[0].module)
        self.assertEqual("exchangesim.web.main", services.services[1].module)

    def test_command_logs_to_a_file_and_not_to_stderr(self):
        service = ServiceSet.load(self._minimal()).services[0]
        argv = service.command("/usr/bin/python3")

        self.assertEqual("/usr/bin/python3", argv[0])
        self.assertIn("--log-file", argv)
        self.assertEqual(service.log_path, argv[argv.index("--log-file") + 1])
        # Without this the log and the capture file are two copies of one
        # stream, and only one of them is ever rotated.
        self.assertIn("--no-console", argv)

    def test_ports_are_read_from_the_service_config(self):
        service = ServiceSet.load(self._minimal()).services[0]
        self.assertEqual([("fix", 9001), ("control", 9101)], service.ports())

    def test_readiness_probes_the_control_port(self):
        service = ServiceSet.load(self._minimal()).services[0]
        self.assertEqual(("127.0.0.1", 9101), service.ready_address())

    def test_a_wildcard_bind_is_probed_on_loopback(self):
        path = os.path.join(self.root, "config", "alpha.json")
        with open(path, "w") as handle:
            json.dump({"control": {"host": "0.0.0.0", "port": 9101}}, handle)
        service = ServiceSet.load(self._minimal()).services[0]

        self.assertEqual(("127.0.0.1", 9101), service.ready_address())

    def test_selection_keeps_declaration_order(self):
        self._venue_config("alpha")
        self._venue_config("beta")
        path = self._write({"services": [
            {"name": "alpha", "config": "config/alpha.json"},
            {"name": "beta", "config": "config/beta.json"}]})
        services = ServiceSet.load(path)

        # The board must not come up before the venues it dials, whatever
        # order the operator typed.
        self.assertEqual(["alpha", "beta"],
                         [s.name for s in services.select(["beta", "alpha"])])

    def test_no_names_selects_everything(self):
        services = ServiceSet.load(self._minimal())
        self.assertEqual(1, len(services.select([])))

    def test_an_unknown_name_lists_the_real_ones(self):
        services = ServiceSet.load(self._minimal())
        with self.assertRaises(ConfigError) as caught:
            services.select(["nope"])
        self.assertIn("alpha", str(caught.exception))

    def test_a_duplicate_name_is_refused(self):
        self._venue_config("alpha")
        path = self._write({"services": [
            {"name": "alpha", "config": "config/alpha.json"},
            {"name": "alpha", "config": "config/alpha.json"}]})
        with self.assertRaises(ConfigError):
            ServiceSet.load(path)

    def test_a_service_without_a_config_is_refused(self):
        path = self._write({"services": [{"name": "alpha"}]})
        with self.assertRaises(ConfigError):
            ServiceSet.load(path)

    def test_an_empty_service_list_is_refused(self):
        path = self._write({"services": []})
        with self.assertRaises(ConfigError):
            ServiceSet.load(path)

    def test_log_ceiling_can_be_set_per_service(self):
        self._venue_config("alpha")
        path = self._write({
            "log": {"max_bytes": 100, "backups": 2},
            "services": [{"name": "alpha", "config": "config/alpha.json",
                          "log_max_bytes": 999}]})
        service = ServiceSet.load(path).services[0]

        self.assertEqual(999, service.max_bytes)
        self.assertEqual(2, service.backups)


class ShippedConfigTest(unittest.TestCase):
    """The file that ships must declare the venues the docs promise."""

    def test_services_json_declares_both_venues_and_the_board(self):
        services = ServiceSet.load(default_config_path())
        self.assertEqual(["japannext", "hkex", "web"], services.names)

    def test_the_board_starts_last(self):
        # It is a client of the venues: last up, first down.
        services = ServiceSet.load(default_config_path())
        self.assertEqual("web", services.names[-1])

    def test_every_declared_config_exists(self):
        for service in ServiceSet.load(default_config_path()):
            self.assertTrue(os.path.isfile(service.config_path),
                            "%s: %s" % (service.name, service.config_path))

    def test_the_declared_ports_are_the_documented_ones(self):
        services = dict((s.name, dict(s.ports()))
                        for s in ServiceSet.load(default_config_path()))
        self.assertEqual(9101, services["japannext"]["control"])
        self.assertEqual(9102, services["hkex"]["control"])
        # The binary encoding is the one HKEX clients arrive on.
        self.assertEqual(9011, services["hkex"]["binary"])
        self.assertEqual(9200, services["web"]["http"])


class RotateTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="exsim-rot-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = os.path.join(self.root, "capture.out")

    def _write(self, path, size):
        with open(path, "wb") as handle:
            handle.write(b"x" * size)

    def test_a_small_file_is_left_alone(self):
        self._write(self.path, 10)
        self.assertFalse(rotate(self.path, 1024, 3))
        self.assertTrue(os.path.exists(self.path))

    def test_a_full_file_becomes_dot_one(self):
        self._write(self.path, 2048)
        self.assertTrue(rotate(self.path, 1024, 3))

        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(2048, os.path.getsize(self.path + ".1"))

    def test_backups_shuffle_up_and_the_oldest_is_dropped(self):
        self._write(self.path + ".1", 1)
        self._write(self.path + ".2", 2)
        self._write(self.path, 2048)

        rotate(self.path, 1024, 2)

        # .2 was the last one kept, so the old .2 is gone and .1 became it.
        self.assertEqual(1, os.path.getsize(self.path + ".2"))
        self.assertEqual(2048, os.path.getsize(self.path + ".1"))

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(rotate(self.path, 1024, 3))

    def test_zero_max_bytes_means_never_rotate(self):
        self._write(self.path, 4096)
        self.assertFalse(rotate(self.path, 0, 3))
        self.assertTrue(os.path.exists(self.path))


class PidfileTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="exsim-pid-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = os.path.join(self.root, "alpha.pid")

    def test_round_trip(self):
        service = _FakeService(self.root, "alpha")
        supervisor.write_pidfile(self.path, service, 4242, "python -m thing")
        record = supervisor.read_pidfile(self.path)

        self.assertEqual(4242, record["pid"])
        self.assertEqual("alpha", record["name"])
        self.assertTrue(record["started_at"].endswith("Z"))

    def test_a_bare_pid_is_still_understood(self):
        with open(self.path, "w") as handle:
            handle.write("1234\n")
        self.assertEqual(1234, supervisor.read_pidfile(self.path)["pid"])

    def test_rubbish_reads_as_nothing(self):
        with open(self.path, "w") as handle:
            handle.write("not a pid at all")
        self.assertIsNone(supervisor.read_pidfile(self.path))

    def test_a_missing_file_reads_as_nothing(self):
        self.assertIsNone(supervisor.read_pidfile(self.path))

    def test_removing_a_missing_file_is_not_an_error(self):
        supervisor.remove_pidfile(self.path)

    def test_a_dead_pid_reports_stale_rather_than_running(self):
        service = _FakeService(self.root, "alpha")
        # Pid 1 is init on POSIX, so pick one nothing plausibly owns.
        supervisor.write_pidfile(service.pid_path, service, 999999, "x")
        status = supervisor.inspect(service)

        self.assertEqual(STALE, status.state)
        self.assertIn("gone", status.detail)

    def test_no_pidfile_reports_stopped(self):
        service = _FakeService(self.root, "alpha")
        self.assertEqual(STOPPED, supervisor.inspect(service).state)


class ProcessTest(unittest.TestCase):

    def test_this_process_is_alive(self):
        self.assertTrue(process.alive(os.getpid()))

    def test_a_nonexistent_pid_is_not(self):
        self.assertFalse(process.alive(999999))

    def test_pid_zero_is_never_alive(self):
        # Signalling 0 means "my process group" on POSIX, which would be a
        # spectacular way to answer this question.
        self.assertFalse(process.alive(0))

    def test_stopping_a_dead_process_says_so_rather_than_raising(self):
        self.assertFalse(process.request_stop(999999))
        self.assertFalse(process.force_stop(999999))


class LifecycleTest(unittest.TestCase):
    """Starts a real child, waits for its port, and stops it again."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="exsim-run-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.port = _free_port()
        self.services = _ServiceSetOf(_ListenerService(self.root, self.port))
        self.lines = []
        self.supervisor = Supervisor(
            self.services, python=sys.executable, report=self.lines.append)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.supervisor.stop(timeout=5.0)
        except ServiceError:
            pass

    def test_start_waits_for_the_port_then_stop_ends_it(self):
        status = self.supervisor.start(wait=20.0)[0]

        self.assertEqual(RUNNING, status.state)
        self.assertTrue(process.alive(status.pid))
        self.assertTrue(os.path.isfile(status.service.pid_path))

        stopped = self.supervisor.stop(timeout=10.0)[0]

        self.assertEqual(STOPPED, stopped.state)
        self.assertFalse(process.alive(status.pid))
        self.assertFalse(os.path.exists(status.service.pid_path))

    def test_the_childs_own_output_is_captured(self):
        status = self.supervisor.start(wait=20.0)[0]
        captured = _wait_for_text(status.service.output_path, "listening")

        self.assertIn("listening", captured)

    def test_starting_twice_is_a_no_op(self):
        first = self.supervisor.start(wait=20.0)[0]
        self.lines[:] = []
        second = self.supervisor.start(wait=20.0)[0]

        self.assertEqual(first.pid, second.pid)
        self.assertIn("already running", self.lines[0])

    def test_stopping_what_is_not_running_is_a_no_op(self):
        self.supervisor.stop(timeout=5.0)
        self.assertIn("not running", self.lines[-1])

    def test_a_stale_pidfile_does_not_block_a_start(self):
        service = self.services.services[0]
        supervisor.write_pidfile(service.pid_path, service, 999999, "gone")

        status = self.supervisor.start(wait=20.0)[0]

        self.assertEqual(RUNNING, status.state)
        self.assertNotEqual(999999, status.pid)
        self.assertTrue(any("stale" in line for line in self.lines))


class TailTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="exsim-tail-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = os.path.join(self.root, "a.log")

    def _write(self, count):
        # Binary, so the assertions are about tail() and not about which
        # platform's newline the test itself happened to write.
        with open(self.path, "wb") as handle:
            for index in range(count):
                handle.write(b"line %d\n" % index)

    def test_the_last_lines_come_back_in_order(self):
        self._write(10)
        self.assertEqual(["line 8\n", "line 9\n"], tail(self.path, 2))

    def test_asking_for_more_than_there_is_returns_everything(self):
        self._write(3)
        self.assertEqual(3, len(tail(self.path, 100)))

    def test_a_file_longer_than_one_block_still_tails(self):
        with open(self.path, "wb") as handle:
            for index in range(4000):
                handle.write(b"%04d %s\n" % (index, b"x" * 40))
        lines = tail(self.path, 3)

        self.assertEqual(3, len(lines))
        self.assertTrue(lines[-1].startswith("3999 "))

    def test_a_missing_file_says_so_instead_of_raising(self):
        self.assertIn("no a.log yet", tail(self.path, 5)[0])

    def test_following_reads_new_lines_as_text(self):
        self._write(2)
        cursor = os.path.getsize(self.path)
        with open(self.path, "ab") as handle:
            handle.write(b"line 2\n")

        lines, moved = new_lines(self.path, cursor)

        self.assertEqual(["line 2\n"], lines)
        self.assertEqual(os.path.getsize(self.path), moved)

    def test_following_holds_back_a_partial_line(self):
        # The daemon writes as it goes; half a record is not a line yet, and
        # the cursor must not move past it.
        self._write(1)
        cursor = os.path.getsize(self.path)
        with open(self.path, "ab") as handle:
            handle.write(b"line 1 but not fin")

        lines, moved = new_lines(self.path, cursor)

        self.assertEqual([], lines)
        self.assertEqual(cursor, moved)

    def test_following_a_quiet_file_returns_nothing(self):
        self._write(3)
        size = os.path.getsize(self.path)
        self.assertEqual(([], size), new_lines(self.path, size))


class CommandLineTest(unittest.TestCase):

    def test_dashed_verbs_are_accepted(self):
        # `exchangesim --start` is what an operator tries first.
        from exchangesim.ctl.main import _normalise
        self.assertEqual(["start"], _normalise(["--start"]))
        self.assertEqual(["stop", "hkex"], _normalise(["--stop", "hkex"]))

    def test_an_ordinary_flag_is_left_alone(self):
        from exchangesim.ctl.main import _normalise
        self.assertEqual(["--config", "x.json", "status"],
                         _normalise(["--config", "x.json", "--status"]))

    def test_an_unknown_service_exits_with_the_config_code(self):
        with _quiet():
            self.assertEqual(2, ctl_main(["status", "no-such-service"]))

    def test_check_validates_the_shipped_configs(self):
        # The same work `make check` does, through the same entry point.
        with _quiet():
            self.assertEqual(0, ctl_main(["check"]))


class _FakeService(object):
    """A service that owns paths but no command; enough for pidfile tests."""

    def __init__(self, root, name):
        self.name = name
        self.root = root
        self.config_path = os.path.join(root, "%s.json" % name)
        self.log_path = os.path.join(root, "%s.log" % name)
        self.output_path = os.path.join(root, "%s.out" % name)
        self.pid_path = os.path.join(root, "%s.pid" % name)
        self.max_bytes = 1024
        self.backups = 2

    def marker(self):
        return os.path.basename(self.config_path)

    def ports(self):
        return []

    def ready_address(self):
        return None


class _ListenerService(_FakeService):
    """A real startable service: a python one-liner that binds and waits."""

    def __init__(self, root, port):
        _FakeService.__init__(self, root, "listener")
        self.port = port
        with open(self.config_path, "w") as handle:
            handle.write("{}")

    def command(self, python):
        return [python, "-u", "-c", LISTENER.replace("PORT", str(self.port))]

    def marker(self):
        # The child's command line is a -c script, not this file's name, so
        # the recycled-pid check has to look for something actually in it.
        return "listening"

    def ports(self):
        return [("test", self.port)]

    def ready_address(self):
        return ("127.0.0.1", self.port)


class _ServiceSetOf(object):

    def __init__(self, *services):
        self.services = list(services)

    def __iter__(self):
        return iter(self.services)

    def select(self, names=None):
        return list(self.services)


class _quiet(object):
    """Swallow a command's own output so the suite's is readable."""

    def __enter__(self):
        import io as _io
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout = _io.StringIO()
        sys.stderr = _io.StringIO()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout, sys.stderr = self._saved
        return False


def _free_port():
    import socket
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _wait_for_text(path, needle, timeout=10.0):
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        try:
            with open(path, "r") as handle:
                text = handle.read()
        except (IOError, OSError):
            text = ""
        if needle in text:
            return text
        time.sleep(0.05)
    return text


if __name__ == "__main__":
    unittest.main()

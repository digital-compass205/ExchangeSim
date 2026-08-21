"""Starting, stopping and inspecting the declared services.

The state of the world is a pidfile per service under ``var/run``. That is
enough because a pidfile answers the only question that matters -- which
process did *we* start -- and because it survives the controller exiting,
which is the whole point: ``exchangesim start`` returns to the prompt and the
venues stay up.

A pidfile is never trusted on its own. The process it names may have exited, or
worse, its id may have been reused by something unrelated, so
:func:`inspect` checks that the process exists and, where the platform will
say, that its command line is still ours.
"""

import errno
import json
import os
import socket
import time

from ..core.clock import RealClock, format_iso
from . import process

RUNNING = "running"
STOPPED = "stopped"
STALE = "stale"

# How long `start` waits for a service to answer on its port before saying so.
DEFAULT_READY_TIMEOUT = 15.0
# How long `stop` waits after asking politely, before it stops asking.
DEFAULT_STOP_TIMEOUT = 15.0

_POLL_INTERVAL = 0.1


class ServiceError(Exception):
    """A service could not be started or stopped."""


class Status(object):
    """What is known about one service right now."""

    __slots__ = ("service", "state", "pid", "started", "detail")

    def __init__(self, service, state, pid=None, started=None, detail=None):
        self.service = service
        self.state = state
        self.pid = pid
        self.started = started
        self.detail = detail

    @property
    def name(self):
        return self.service.name

    @property
    def running(self):
        return self.state == RUNNING

    def uptime(self, now=None):
        if not self.running or not self.started:
            return None
        return max(0.0, (now or time.time()) - self.started)


def inspect(service):
    # type: (...) -> Status
    """Current state of one service, from its pidfile and the process table."""
    record = read_pidfile(service.pid_path)
    if record is None:
        return Status(service, STOPPED)

    pid = record.get("pid")
    if not isinstance(pid, int) or not process.alive(pid):
        return Status(service, STALE, pid=pid,
                      detail="pidfile %s names a process that is gone"
                             % _short(service.pid_path))

    # A recycled pid would otherwise be reported as our daemon, and `stop`
    # would then kill a stranger. Only Linux answers this; elsewhere the
    # pidfile is taken at its word.
    line = process.command_line(pid)
    if line and service.marker() not in line:
        return Status(service, STALE, pid=pid,
                      detail="pid %d is now some other process" % pid)

    return Status(service, RUNNING, pid=pid, started=record.get("started"))


def read_pidfile(path):
    """The record written by :func:`write_pidfile`, or None if unreadable."""
    try:
        with open(path, "r") as handle:
            raw = handle.read()
    except (IOError, OSError):
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        record = None
    if isinstance(record, dict):
        return record
    # A bare pid, hand-written or from a shell script. json.loads parses that
    # as a number rather than failing, so both paths land here.
    try:
        return {"pid": int(raw.strip())}
    except (TypeError, ValueError):
        return None


def write_pidfile(path, service, pid, command):
    _ensure_dir(os.path.dirname(path))
    now = time.time()
    record = {
        "pid": pid,
        "name": service.name,
        "started": now,
        "started_at": format_iso(RealClock().now()),
        "config": service.config_path,
        "command": command,
    }
    with open(path, "w") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")


def remove_pidfile(path):
    try:
        os.remove(path)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise


class Supervisor(object):
    """Drives a :class:`~exchangesim.ctl.services.ServiceSet`."""

    def __init__(self, services, python=None, report=None):
        self.services = services
        self.python = python or process.interpreter()
        self._report = report or (lambda line: None)

    # -- queries ---------------------------------------------------------

    def status(self, names=None):
        return [inspect(service) for service in self.services.select(names)]

    # -- transitions -----------------------------------------------------

    def start(self, names=None, wait=DEFAULT_READY_TIMEOUT):
        """Start every named service that is not already up.

        Returns the resulting statuses. Services are started in declaration
        order and each is waited for before the next begins, so the board never
        comes up before the venues it dials.
        """
        results = []
        for service in self.services.select(names):
            results.append(self._start_one(service, wait))
        return results

    def stop(self, names=None, timeout=DEFAULT_STOP_TIMEOUT):
        """Stop every named service, newest dependency first."""
        results = []
        # Reverse declaration order: the board is a client of the venues, so it
        # goes down first and never spends the shutdown redialling them.
        for service in reversed(self.services.select(names)):
            results.append(self._stop_one(service, timeout))
        return list(reversed(results))

    def restart(self, names=None, wait=DEFAULT_READY_TIMEOUT,
                timeout=DEFAULT_STOP_TIMEOUT):
        self.stop(names, timeout=timeout)
        return self.start(names, wait=wait)

    # -- one service -----------------------------------------------------

    def _start_one(self, service, wait):
        status = inspect(service)
        if status.running:
            self._report("%-12s already running (pid %d)"
                         % (service.name, status.pid))
            return status
        if status.state == STALE:
            self._report("%-12s clearing stale pidfile" % service.name)
            remove_pidfile(service.pid_path)

        if not os.path.isfile(service.config_path):
            raise ServiceError("%s: config %s does not exist"
                               % (service.name, service.config_path))

        _ensure_dir(os.path.dirname(service.log_path))
        _ensure_dir(os.path.dirname(service.pid_path))
        # The capture file is the one log nobody rotates while it is open, so
        # it is rolled here, on the way in. It should be empty anyway.
        rotate(service.output_path, service.max_bytes, service.backups)

        argv = service.command(self.python)
        pid = process.spawn(argv, service.root, service.output_path,
                            env=_child_env(service.root))
        write_pidfile(service.pid_path, service, pid, " ".join(argv))

        ready = self._await_ready(service, pid, wait)
        if ready is not None and not ready:
            raise ServiceError(
                "%s: started as pid %d but never answered on %s -- see %s"
                % (service.name, pid, _address(service.ready_address()),
                   _short(service.output_path)))
        if not process.alive(pid):
            remove_pidfile(service.pid_path)
            raise ServiceError("%s: exited immediately -- see %s"
                               % (service.name, _short(service.output_path)))

        self._report("%-12s started (pid %d)%s"
                     % (service.name, pid, _ports_note(service)))
        return inspect(service)

    def _stop_one(self, service, timeout):
        status = inspect(service)
        if status.state == STOPPED:
            self._report("%-12s not running" % service.name)
            return status
        if status.state == STALE:
            remove_pidfile(service.pid_path)
            self._report("%-12s not running (cleared stale pidfile)"
                         % service.name)
            return inspect(service)

        pid = status.pid
        process.request_stop(pid)
        if not _wait_until(lambda: not process.alive(pid), timeout):
            self._report("%-12s did not stop in %.0fs, killing"
                         % (service.name, timeout))
            process.force_stop(pid)
            _wait_until(lambda: not process.alive(pid), 5.0)

        if process.alive(pid):
            raise ServiceError("%s: pid %d will not die" % (service.name, pid))

        remove_pidfile(service.pid_path)
        self._report("%-12s stopped" % service.name)
        return inspect(service)

    def _await_ready(self, service, pid, wait):
        """True once the port answers, False on timeout, None if not knowable."""
        address = service.ready_address()
        if not address or wait <= 0:
            return None
        deadline = time.time() + wait
        while time.time() < deadline:
            if not process.alive(pid):
                return False
            if _connects(address):
                return True
            time.sleep(_POLL_INTERVAL)
        return _connects(address)


def rotate(path, max_bytes, backups):
    """Roll ``path`` aside if it has grown past ``max_bytes``.

    The same scheme as :class:`logging.handlers.RotatingFileHandler` -- ``.1``
    is the most recent -- so one directory listing reads consistently whichever
    writer produced the file.
    """
    if not max_bytes or max_bytes <= 0 or backups <= 0:
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size < max_bytes:
        return False

    oldest = "%s.%d" % (path, backups)
    if os.path.exists(oldest):
        os.remove(oldest)
    for index in range(backups - 1, 0, -1):
        source = "%s.%d" % (path, index)
        if os.path.exists(source):
            target = "%s.%d" % (path, index + 1)
            if os.path.exists(target):
                os.remove(target)
            os.rename(source, target)
    os.rename(path, "%s.1" % path)
    return True


def _connects(address):
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        sock.connect(address)
        return True
    except (socket.error, OSError):
        return False
    finally:
        sock.close()


def _wait_until(predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_INTERVAL)
    return predicate()


def _child_env(root):
    """The child's environment: ours, plus what an unbuffered daemon needs.

    ``PYTHONPATH`` is set so the child finds the package when it is started
    from a copied tree that was never installed, which is how the RHEL 8
    deployment works.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root if not existing else (root + os.pathsep + existing)
    return env


def _ensure_dir(path):
    if path and not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise


def _ports_note(service):
    ports = service.ports()
    if not ports:
        return ""
    return "  " + " ".join("%s=%d" % pair for pair in ports)


def _address(address):
    return "%s:%d" % address if address else "its port"


def _short(path):
    try:
        return os.path.relpath(path)
    except ValueError:  # different drive on Windows
        return path

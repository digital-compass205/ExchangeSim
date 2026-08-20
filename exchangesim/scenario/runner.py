"""Run declarative FIX scenarios against a live simulator.

A scenario file looks like this::

    {
      "name": "IOC cancels its remainder",
      "sessions": {
        "seller": {"comp_id": "CLIENT2"},
        "buyer":  {"comp_id": "CLIENT1"}
      },
      "steps": [
        {"control": "state.set", "args": {"market": "DAY", "state": "OPEN"}},
        {"logon": "seller", "reset": true},
        {"send": "seller", "fields": {"35": "D", "11": "S1", "55": "7203",
                                      "54": "2", "38": "100", "40": "2",
                                      "44": "2846.0"}},
        {"expect": "seller", "fields": {"35": "8", "150": "0"}},
        {"logout": "seller"}
      ]
    }

Field maps are raw FIX tags, deliberately: a scenario is a statement about what
goes on the wire, and a friendlier vocabulary would hide exactly the details
these tests exist to pin down. ``TransactTime(60)`` and the session header are
filled in automatically.

A scenario may also name the dialect and the venue it runs against, so that one
invocation covers every simulator a CI run has started::

    "begin_string": "FIXT.1.1",
    "fix_port": 9011,
    "control_port": 9102,
    "server_comp_id": "HKEXSIM",
    "logon_reset_flag": false,
    "logon_fields": {"789": "1", "1137": "9", "1402": "cGFzc3dvcmQ="}

``logon_fields`` carries whatever the dialect adds to Logon.
``logon_reset_flag`` is for a venue that refuses a client-initiated sequence
reset, as HKEX does: the scenario restarts its own numbering and asks the venue
to do the same with the ``session.reset`` control command.

Exit codes: 0 all scenarios passed, 1 a step failed, 2 usage, 3 could not
reach the simulator.
"""

import argparse
import glob
import json
import os
import socket
import sys
import time

from ..cli.client import CommandFailed, ControlClient, ControlClientError
from ..core.clock import RealClock
from ..fix import constants as C
from ..fix.codec import FixCodec
from ..fix.message import Message

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_TRANSPORT = 3

DEFAULT_EXPECT_TIMEOUT = 3.0

#: Tags the runner fills in itself if a step does not set them.
_AUTO_TAGS = (C.MSG_SEQ_NUM, C.SENDER_COMP_ID, C.TARGET_COMP_ID, C.SENDING_TIME)


class ScenarioError(Exception):
    """A malformed scenario file."""


class StepFailure(Exception):
    """A step did not do what the scenario said it should."""


def _client_codec(protocol, begin_string, server_comp_id):
    """The client half of one of a venue's encodings.

    The binary encoding is HKEX's, and its layouts live with that venue, so the
    import is deferred: a run that scripts nothing but FIX never touches it.
    """
    if protocol == "fix":
        return FixCodec(begin_string)
    if protocol == "binary":
        from ..binary.codec import BinaryCodec
        from ..venues.hkex import binary as layouts
        from ..venues.hkex import dictionary as hkex_dictionary
        return BinaryCodec(layouts.build(hkex_dictionary.build_binary()),
                           server_comp_id, client=True)
    raise ScenarioError("unknown protocol '%s'" % protocol)


class Session(object):
    """One scripted FIX client."""

    def __init__(self, name, comp_id, host, port, server_comp_id, sub_id=None,
                 timeout=5.0, begin_string="FIX.4.2", logon_fields=None,
                 logon_reset_flag=True, protocol="fix"):
        self.name = name
        self.comp_id = comp_id
        self.server_comp_id = server_comp_id
        self.sub_id = sub_id
        self.host = host
        self.port = port
        self.timeout = timeout
        #: The dialect's BeginString(8). FIXT.1.1 for a FIX 5.0 venue.
        self.begin_string = begin_string
        #: Extra tags every Logon carries -- FIXT.1.1 requires 789 and 1137.
        self.logon_fields = dict(logon_fields or {})
        #: HKEX refuses a client-initiated ResetSeqNumFlag, so a scenario there
        #: restarts its own numbering without asking the venue to.
        self.logon_reset_flag = logon_reset_flag
        #: Which encoding of the venue's protocol this session speaks.
        self.protocol = protocol
        self.codec = _client_codec(protocol, begin_string, server_comp_id)
        self.clock = RealClock()
        self.sock = None
        self.seq = 1
        self._framer = self.codec.framer()
        self._inbox = []

    # -- connection --------------------------------------------------------

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port),
                                                 self.timeout)
        except OSError as exc:
            raise ControlClientError(
                "session '%s' cannot connect to %s:%d: %s"
                % (self.name, self.host, self.port, exc))
        self.sock.settimeout(0.1)
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    @property
    def connected(self):
        return self.sock is not None

    # -- sending -----------------------------------------------------------

    def send(self, fields, sub_id=None):
        if self.sock is None:
            self.connect()

        message = Message()
        for tag, value in sorted(fields.items(), key=lambda item: int(item[0])):
            message.set(int(tag), str(value))

        message.set(C.MSG_SEQ_NUM, self.seq)
        self.seq += 1
        message.set(C.SENDER_COMP_ID, self.comp_id)
        message.set(C.TARGET_COMP_ID, self.server_comp_id)
        message.set(C.SENDING_TIME, self.clock.timestamp())

        target_sub = sub_id if sub_id is not None else self.sub_id
        if target_sub and not message.has(C.TARGET_SUB_ID):
            message.set(C.TARGET_SUB_ID, target_sub)

        # Application messages need a TransactTime; filling it in keeps
        # scenario files free of timestamps that would go stale.
        if message.msg_type in ("D", "F", "G", "q") and not message.has(60):
            message.set(60, self.clock.timestamp())

        self.sock.sendall(self.codec.encode(message))
        return message

    def logon(self, reset=True, heartbeat=30):
        if reset:
            self.seq = 1
        fields = {str(C.MSG_TYPE): C.LOGON,
                  str(C.ENCRYPT_METHOD): "0",
                  str(C.HEART_BT_INT): str(heartbeat)}
        if reset and self.logon_reset_flag:
            fields[str(C.RESET_SEQ_NUM_FLAG)] = C.YES
        fields.update(self.logon_fields)
        return self.send(fields, sub_id=False)

    def logout(self):
        return self.send({str(C.MSG_TYPE): C.LOGOUT}, sub_id=False)

    # -- receiving ---------------------------------------------------------

    def pump(self):
        """Read whatever has arrived, without blocking for long."""
        if self.sock is None:
            return
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return
        except OSError:
            return
        if not chunk:
            return
        self._inbox.extend(self.codec.decode(raw)
                           for raw in self._framer.feed(chunk))

    def match(self, fields):
        """Remove and return the first buffered message matching ``fields``."""
        for index, message in enumerate(self._inbox):
            if _matches(message, fields):
                return self._inbox.pop(index)
        return None

    def wait_for(self, fields, timeout):
        deadline = time.time() + timeout
        while True:
            found = self.match(fields)
            if found is not None:
                return found
            if time.time() >= deadline:
                return None
            self.pump()

    def buffered(self):
        return list(self._inbox)

    def clear(self):
        self._inbox = []


def _matches(message, fields):
    for tag, expected in fields.items():
        actual = message.get(int(tag))
        if expected is None:
            if actual is not None:
                return False
            continue
        if actual != str(expected):
            return False
    return True


class Scenario(object):
    """A loaded scenario file."""

    def __init__(self, path, data):
        self.path = path
        self.name = data.get("name") or os.path.basename(path)
        self.data = data
        self.steps = data.get("steps")
        if not isinstance(self.steps, list) or not self.steps:
            raise ScenarioError("%s: 'steps' must be a non-empty array" % path)

    @classmethod
    def load(cls, path):
        try:
            with open(path, "r") as handle:
                data = json.load(handle)
        except (IOError, OSError) as exc:
            raise ScenarioError("cannot read %s: %s" % (path, exc))
        except ValueError as exc:
            raise ScenarioError("invalid JSON in %s: %s" % (path, exc))
        if not isinstance(data, dict):
            raise ScenarioError("%s must contain a JSON object" % path)
        return cls(path, data)


class Runner(object):
    """Executes scenarios against a running simulator."""

    def __init__(self, host="127.0.0.1", fix_port=9001, control_port=9101,
                 server_comp_id="JNXSIM", token=None, verbose=False):
        self.host = host
        self.fix_port = fix_port
        self.control_port = control_port
        self.server_comp_id = server_comp_id
        self.token = token
        self.verbose = verbose

    def run(self, scenario):
        """Run one scenario, returning (passed, message)."""
        sessions = self._build_sessions(scenario)
        # A scenario may name its own ports so that one invocation can drive
        # every venue a CI run has started, not only the default one.
        control = ControlClient(
            self.host, scenario.data.get("control_port", self.control_port),
            10.0, self.token)
        control.connect()

        try:
            for number, step in enumerate(scenario.steps, start=1):
                try:
                    self._execute(step, sessions, control)
                except StepFailure as exc:
                    return False, "step %d (%s): %s" % (
                        number, _describe(step), exc)
                except ScenarioError as exc:
                    return False, "step %d: %s" % (number, exc)
            return True, "%d steps" % len(scenario.steps)
        finally:
            for session in sessions.values():
                session.close()
            control.close()

    def _build_sessions(self, scenario):
        declared = scenario.data.get("sessions") or {}
        if not isinstance(declared, dict):
            raise ScenarioError("%s: 'sessions' must be an object" % scenario.path)

        data = scenario.data
        sessions = {}
        for name, spec in declared.items():
            if isinstance(spec, str):
                spec = {"comp_id": spec}
            comp_id = spec.get("comp_id")
            if not comp_id:
                raise ScenarioError("session '%s' needs a 'comp_id'" % name)
            sessions[name] = Session(
                name, comp_id, self.host,
                spec.get("port", data.get("fix_port", self.fix_port)),
                data.get("server_comp_id", self.server_comp_id),
                spec.get("sub_id"),
                begin_string=data.get("begin_string", "FIX.4.2"),
                logon_fields=spec.get("logon_fields", data.get("logon_fields")),
                logon_reset_flag=data.get("logon_reset_flag", True),
                protocol=spec.get("protocol", data.get("protocol", "fix")))
        return sessions

    # -- step execution ----------------------------------------------------

    def _execute(self, step, sessions, control):
        if not isinstance(step, dict):
            raise ScenarioError("each step must be an object")

        for kind, handler in _STEPS:
            if kind in step:
                handler(self, step, sessions, control)
                return

        raise ScenarioError("unrecognised step: %s" % json.dumps(step))

    def _session(self, step, sessions, key):
        name = step[key]
        session = sessions.get(name)
        if session is None:
            raise ScenarioError("unknown session '%s'" % name)
        return session

    def _step_control(self, step, sessions, control):
        # A command that is *refused* is often the thing under test -- removing
        # an instrument that still has orders, entering one while order entry is
        # disabled -- so a scenario can assert the error code instead of a result.
        expected_error = step.get("expect_error")
        try:
            result = control.call(step["control"], step.get("args", {}))
        except CommandFailed as exc:
            if not expected_error:
                raise
            if exc.code != expected_error:
                raise StepFailure(
                    "control %s failed with '%s', expected '%s'"
                    % (step["control"], exc.code, expected_error))
            self._log("control %s refused with '%s', as expected"
                      % (step["control"], exc.code))
            return

        if expected_error:
            raise StepFailure(
                "control %s succeeded; expected it to fail with '%s'"
                % (step["control"], expected_error))

        self._log("control %s -> %s" % (step["control"], result))
        expected = step.get("expect_result")
        if expected:
            for key, value in expected.items():
                if result.get(key) != value:
                    raise StepFailure("result['%s'] was %r, expected %r"
                                      % (key, result.get(key), value))

        # For a command that answers with a list -- trades, orders, the audit --
        # where the assertion is that something is *in* it and the rest of the
        # reply is whatever else happened to be going on. A dict matches an
        # element field by field; anything else matches by equality, for the
        # lists whose elements are plain values.
        for key, wanted in (step.get("expect_contains") or {}).items():
            rows = result.get(key)
            if not isinstance(rows, list):
                raise StepFailure("result['%s'] is not a list" % key)
            if isinstance(wanted, dict):
                if not wanted:
                    raise StepFailure(
                        "expect_contains['%s'] is empty, which would match "
                        "anything" % key)
                found = any(isinstance(row, dict)
                            and all(row.get(field) == value
                                    for field, value in wanted.items())
                            for row in rows)
            else:
                found = wanted in rows
            if not found:
                raise StepFailure(
                    "no entry in result['%s'] matched %r (%d were returned)"
                    % (key, wanted, len(rows)))
            self._log("result['%s'] contains %r" % (key, wanted))

    def _step_logon(self, step, sessions, control):
        session = self._session(step, sessions, "logon")
        session.connect()
        session.logon(reset=step.get("reset", True),
                      heartbeat=step.get("heartbeat", 30))
        response = session.wait_for({str(C.MSG_TYPE): C.LOGON},
                                    step.get("timeout", DEFAULT_EXPECT_TIMEOUT))
        if response is None:
            raise StepFailure("no Logon response from the simulator")
        self._log("%s logged on" % session.name)

    def _step_logout(self, step, sessions, control):
        session = self._session(step, sessions, "logout")
        session.logout()
        session.wait_for({str(C.MSG_TYPE): C.LOGOUT},
                         step.get("timeout", DEFAULT_EXPECT_TIMEOUT))
        session.close()

    def _step_disconnect(self, step, sessions, control):
        """Drop the socket without a Logout, as a network failure would."""
        self._session(step, sessions, "disconnect").close()

    def _step_send(self, step, sessions, control):
        session = self._session(step, sessions, "send")
        fields = step.get("fields")
        if not isinstance(fields, dict):
            raise ScenarioError("a 'send' step needs a 'fields' object")
        message = session.send(fields, step.get("sub_id"))
        self._log("%s --> %s" % (session.name, message.to_string()))

    def _step_expect(self, step, sessions, control):
        session = self._session(step, sessions, "expect")
        fields = step.get("fields")
        if not isinstance(fields, dict):
            raise ScenarioError("an 'expect' step needs a 'fields' object")

        found = session.wait_for(fields,
                                 step.get("timeout", DEFAULT_EXPECT_TIMEOUT))
        if found is None:
            raise StepFailure(
                "no message matching %s arrived for '%s'; received: %s"
                % (json.dumps(fields), session.name,
                   _summarise(session.buffered())))
        self._log("%s <-- %s" % (session.name, found.to_string()))

    def _step_expect_none(self, step, sessions, control):
        session = self._session(step, sessions, "expect_none")
        fields = step.get("fields") or {}
        window = step.get("for", 0.5)
        deadline = time.time() + window
        while time.time() < deadline:
            session.pump()
        found = session.match(fields) if fields else (
            session.buffered()[0] if session.buffered() else None)
        if found is not None:
            raise StepFailure("unexpected message: %s" % found.to_string())

    def _step_clear(self, step, sessions, control):
        """Discard everything received so far.

        Drains the socket first: a message the simulator has already sent but
        which has not yet been read is still "received so far", and leaving it
        in the kernel buffer would let it surface later and fail an
        ``expect_none``.
        """
        session = self._session(step, sessions, "clear")
        deadline = time.time() + step.get("for", 0.3)
        while time.time() < deadline:
            session.pump()
        session.clear()

    def _step_sleep(self, step, sessions, control):
        time.sleep(float(step["sleep"]))

    def _log(self, text):
        if self.verbose:
            sys.stderr.write("    %s\n" % text)


_STEPS = [
    ("control", Runner._step_control),
    ("logon", Runner._step_logon),
    ("logout", Runner._step_logout),
    ("disconnect", Runner._step_disconnect),
    ("send", Runner._step_send),
    ("expect", Runner._step_expect),
    ("expect_none", Runner._step_expect_none),
    ("clear", Runner._step_clear),
    ("sleep", Runner._step_sleep),
]


def _describe(step):
    for kind, _handler in _STEPS:
        if kind in step:
            return "%s %s" % (kind, step[kind])
    return "?"


def _summarise(messages):
    if not messages:
        return "(nothing)"
    return "; ".join(message.to_string() for message in messages[:5])


# -- entry point -------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="exsim-scenario",
        description="Run declarative FIX scenarios against a running simulator.")
    parser.add_argument("paths", nargs="+",
                        help="scenario files or glob patterns")
    parser.add_argument("--host", default=os.environ.get("EXSIM_HOST",
                                                         "127.0.0.1"))
    parser.add_argument("--fix-port", type=int,
                        default=int(os.environ.get("EXSIM_FIX_PORT", "9001")))
    parser.add_argument("--control-port", type=int,
                        default=int(os.environ.get("EXSIM_PORT", "9101")))
    parser.add_argument("--server-comp-id",
                        default=os.environ.get("EXSIM_SERVER_COMP_ID", "JNXSIM"))
    parser.add_argument("--token", default=os.environ.get("EXSIM_TOKEN") or None)
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="print every message sent and received")
    return parser


def main(argv=None):
    opts = build_parser().parse_args(argv)

    paths = []
    for pattern in opts.paths:
        matched = sorted(glob.glob(pattern))
        paths.extend(matched or ([pattern] if os.path.isfile(pattern) else []))
    if not paths:
        sys.stderr.write("exsim-scenario: no scenario files matched\n")
        return EXIT_USAGE

    runner = Runner(opts.host, opts.fix_port, opts.control_port,
                    opts.server_comp_id, opts.token, opts.verbose)

    passed = failed = 0
    for path in paths:
        try:
            scenario = Scenario.load(path)
        except ScenarioError as exc:
            sys.stderr.write("FAIL %s: %s\n" % (path, exc))
            failed += 1
            continue

        try:
            ok, detail = runner.run(scenario)
        except ControlClientError as exc:
            sys.stderr.write("exsim-scenario: %s\n" % exc)
            return EXIT_TRANSPORT

        if ok:
            passed += 1
            sys.stdout.write("PASS %s (%s)\n" % (scenario.name, detail))
        else:
            failed += 1
            sys.stdout.write("FAIL %s\n     %s\n" % (scenario.name, detail))
        sys.stdout.flush()

    sys.stdout.write("\n%d passed, %d failed\n" % (passed, failed))
    return EXIT_OK if failed == 0 else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())

"""``exchangesim`` -- one command for the whole simulator.

    exchangesim start            # every service in config/services.json
    exchangesim status
    exchangesim logs hkex -f
    exchangesim stop

Each service is a separate process, as it must be -- one venue per process is
the rule the runner is built on -- but nothing about that needs to be the
operator's problem. Output goes to a rotated file per service under
``var/log``; the terminal gets one line per service and the prompt back.
"""

import argparse
import errno
import os
import subprocess
import sys
import time

from ..core.config import ConfigError
from .services import ServiceSet, default_config_path
from .supervisor import (DEFAULT_READY_TIMEOUT, DEFAULT_STOP_TIMEOUT,
                         ServiceError, Supervisor)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_NOT_RUNNING = 3

COMMANDS = ("start", "stop", "restart", "status", "logs", "check")

FOLLOW_INTERVAL = 0.25


def build_parser():
    parser = argparse.ArgumentParser(
        prog="exchangesim",
        description="Start, stop and inspect the exchange simulator.",
        epilog="--start, --stop, --restart and --status are accepted as "
               "spellings of the matching subcommand.")
    parser.add_argument("--config", "-c", default=None,
                        help="path to services.json "
                             "(default: config/services.json)")
    parser.add_argument("--python", default=None,
                        help="interpreter to start services with "
                             "(default: the one running this command)")

    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="start services that are not running")
    _names(start)
    start.add_argument("--wait", type=float, default=DEFAULT_READY_TIMEOUT,
                       help="seconds to wait for each service to answer on "
                            "its port; 0 to not wait")

    stop = sub.add_parser("stop", help="stop running services")
    _names(stop)
    stop.add_argument("--timeout", type=float, default=DEFAULT_STOP_TIMEOUT,
                      help="seconds to wait for a clean exit before killing")

    restart = sub.add_parser("restart", help="stop then start")
    _names(restart)
    restart.add_argument("--wait", type=float, default=DEFAULT_READY_TIMEOUT)
    restart.add_argument("--timeout", type=float, default=DEFAULT_STOP_TIMEOUT)

    status = sub.add_parser("status", help="what is running, and where")
    _names(status)

    logs = sub.add_parser("logs", help="show or follow a service's log")
    _names(logs)
    logs.add_argument("-n", "--lines", type=int, default=40,
                      help="how many trailing lines to show (default 40)")
    logs.add_argument("-f", "--follow", action="store_true",
                      help="keep printing as the file grows")
    logs.add_argument("--out", action="store_true",
                      help="show the captured stdout/stderr file instead of "
                           "the log")

    check = sub.add_parser("check", help="validate every service's config")
    _names(check)

    return parser


def _names(parser):
    parser.add_argument("names", nargs="*", metavar="SERVICE",
                        help="service names; all of them if omitted")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = _normalise(argv)
    opts = build_parser().parse_args(argv)

    if not opts.command:
        build_parser().print_help()
        return EXIT_OK

    path = opts.config or default_config_path()
    try:
        services = ServiceSet.load(path)
        selected = services.select(opts.names)
    except ConfigError as exc:
        sys.stderr.write("exchangesim: %s\n" % exc)
        return EXIT_CONFIG

    supervisor = Supervisor(services, python=opts.python, report=_say)
    names = [service.name for service in selected]

    try:
        if opts.command == "start":
            supervisor.start(names, wait=opts.wait)
            return EXIT_OK
        if opts.command == "stop":
            supervisor.stop(names, timeout=opts.timeout)
            return EXIT_OK
        if opts.command == "restart":
            supervisor.restart(names, wait=opts.wait, timeout=opts.timeout)
            return EXIT_OK
        if opts.command == "status":
            return _status(supervisor, names)
        if opts.command == "logs":
            return _logs(selected, opts)
        if opts.command == "check":
            return _check(selected, opts.python or sys.executable)
    except ServiceError as exc:
        sys.stderr.write("exchangesim: %s\n" % exc)
        return EXIT_FAILED
    except KeyboardInterrupt:
        return EXIT_OK

    return EXIT_OK


def _normalise(argv):
    """Accept ``--start`` as a spelling of ``start``.

    A dashed verb is what an operator reaches for first, and refusing it here
    would be a lesson in argparse rather than an answer.
    """
    out = []
    for token in argv:
        if token.startswith("--") and token[2:] in COMMANDS:
            out.append(token[2:])
        else:
            out.append(token)
    return out


def _status(supervisor, names):
    rows = supervisor.status(names)
    now = time.time()
    lines = [("SERVICE", "STATE", "PID", "UPTIME", "PORTS", "LOG")]
    for row in rows:
        uptime = row.uptime(now)
        lines.append((
            row.name,
            row.state,
            str(row.pid) if row.pid else "-",
            _duration(uptime) if uptime is not None else "-",
            " ".join("%s=%d" % pair for pair in row.service.ports()) or "-",
            "%s (%s)" % (_short(row.service.log_path),
                         _size(row.service.log_path)),
        ))
    _write_table(lines)

    for row in rows:
        if row.detail:
            sys.stdout.write("  %s: %s\n" % (row.name, row.detail))

    if all(row.running for row in rows):
        return EXIT_OK
    return EXIT_NOT_RUNNING


def _logs(selected, opts):
    paths = []
    for service in selected:
        paths.append((service.name,
                      service.output_path if opts.out else service.log_path))

    if opts.follow:
        return _follow(paths, opts.lines)

    for index, (name, path) in enumerate(paths):
        if len(paths) > 1:
            if index:
                sys.stdout.write("\n")
            sys.stdout.write("==> %s <==\n" % _short(path))
        for line in tail(path, opts.lines):
            sys.stdout.write(line)
        sys.stdout.flush()
    return EXIT_OK


def _follow(paths, lines):
    """Print trailing lines, then keep printing as the files grow.

    Polling rather than any platform's file-change notification: this waits on
    a file a *different* process is rotating, so the only portable answer is to
    notice that the size went backwards and reopen.
    """
    prefix = len(paths) > 1
    cursors = {}
    for name, path in paths:
        for line in tail(path, lines):
            sys.stdout.write(_prefixed(name, line) if prefix else line)
        cursors[path] = _size_of(path)
    sys.stdout.flush()

    try:
        while True:
            time.sleep(FOLLOW_INTERVAL)
            for name, path in paths:
                size = _size_of(path)
                start = cursors.get(path, 0)
                if size < start:
                    # Rotated out from under us: the new file starts at zero.
                    start = 0
                if size == start:
                    continue
                lines, cursors[path] = new_lines(path, start)
                for line in lines:
                    sys.stdout.write(_prefixed(name, line) if prefix else line)
                sys.stdout.flush()
    except KeyboardInterrupt:
        sys.stdout.write("\n")
    return EXIT_OK


def _check(selected, python):
    failures = 0
    for service in selected:
        argv = [python, "-m", service.module,
                "--config", service.config_path, "--check"]
        result = subprocess.Popen(argv, cwd=service.root,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT)
        output = result.communicate()[0]
        text = output.decode("utf-8", "replace").strip()
        if result.returncode == 0:
            sys.stdout.write("%-12s OK    %s\n" % (service.name, text))
        else:
            failures += 1
            sys.stdout.write("%-12s FAIL  %s\n" % (service.name, text))
    return EXIT_OK if not failures else EXIT_FAILED


def tail(path, lines):
    """The last ``lines`` lines of a file, without reading all of it."""
    if lines <= 0:
        return []
    try:
        handle = open(path, "rb")
    except (IOError, OSError) as exc:
        if exc.errno == errno.ENOENT:
            return ["(no %s yet)\n" % os.path.basename(path)]
        raise
    try:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        block = 8192
        data = b""
        while end > 0 and data.count(b"\n") <= lines:
            step = min(block, end)
            end -= step
            handle.seek(end)
            data = handle.read(step) + data
    finally:
        handle.close()
    text = data.decode("utf-8", "replace")
    return text.splitlines(True)[-lines:]


def new_lines(path, offset):
    """Complete lines written since ``offset``, and the offset after them.

    It stops at the last newline rather than at the end of what was read: the
    writer may be mid-record, and since the cursor is a byte offset, a
    multi-byte character split across two reads would otherwise decode as
    rubbish at the end of one chunk and again at the start of the next.
    """
    chunk = _read_from(path, offset)
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return [], offset
    text = chunk[:cut + 1].decode("utf-8", "replace")
    return text.splitlines(True), offset + cut + 1


def _read_from(path, offset):
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read()
    except (IOError, OSError):
        return b""


def _size_of(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _prefixed(name, line):
    return "%-12s| %s" % (name, line)


def _write_table(rows):
    widths = [0] * len(rows[0])
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    for row in rows:
        cells = [cell.ljust(widths[index]) for index, cell in enumerate(row)]
        sys.stdout.write("  ".join(cells).rstrip() + "\n")


def _duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "%dm %02ds" % (minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd %02dh" % (days, hours)


def _size(path):
    size = _size_of(path)
    if not os.path.exists(path):
        return "absent"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.0f %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % size


def _short(path):
    try:
        return os.path.relpath(path)
    except ValueError:
        return path


def _say(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())

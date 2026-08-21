"""The platform-specific half of process control.

Everything here exists because "is it running" and "ask it to stop" are the two
operations POSIX and Windows disagree about most. The target is RHEL 8, but
development is on Windows and the tests have to pass there, so both are real
implementations rather than one plus a stub.

Nothing above this module imports ``signal`` or ``ctypes``.
"""

import contextlib
import errno
import os
import subprocess
import sys

WINDOWS = os.name == "nt"

# CreateProcess flags. Named here rather than taken from ``subprocess`` so the
# module imports on POSIX, where those constants do not exist.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_HANDLE_FLAG_INHERIT = 0x00000001

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

_spawned = []


def reap():
    """Collect any child of ours that has already exited.

    On POSIX a process we started stays in the table as a zombie until it is
    waited for, and ``os.kill(zombie, 0)`` succeeds -- so without this, a
    process that starts *and* stops a service in one run would watch it die
    and still be told it was alive.
    """
    for child in list(_spawned):
        if child.poll() is not None:
            _spawned.remove(child)


def alive(pid):
    # type: (int) -> bool
    """True if a process with this id currently exists."""
    if not pid or pid <= 0:
        return False
    reap()
    if WINDOWS:
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            # Someone else's process. It exists, which is what was asked.
            return True
        raise
    return True


def request_stop(pid):
    # type: (int) -> bool
    """Ask a process to shut down cleanly. False if it was already gone.

    POSIX gets SIGTERM, which the daemons handle: the reactor stops, sessions
    are logged out and stores are closed.

    Windows has no such signal. The nearest thing is a console
    CTRL_BREAK_EVENT, which the daemons handle as SIGBREAK -- but a console
    control event only reaches processes sharing the *sender's* console, and
    ``exchangesim stop`` is usually a different terminal from the one that ran
    ``start``. That fails with ERROR_INVALID_PARAMETER, and the fallback is to
    terminate outright. It is not graceful, and it is survivable: the sequence
    store is written and fsynced on every update, never at shutdown.
    """
    if not alive(pid):
        return False
    import signal
    try:
        if WINDOWS:
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            except (OSError, SystemError):
                # Not our console. os.kill reports this either way round
                # depending on the interpreter, hence both exception types.
                return force_stop(pid)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise
    return True


def force_stop(pid):
    # type: (int) -> bool
    """Kill a process outright. False if it was already gone."""
    if not alive(pid):
        return False
    import signal
    try:
        if WINDOWS:
            # Any signal but the two console events reaches TerminateProcess.
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise
    return True


def spawn(argv, cwd, output_path, env=None):
    # type: (list, str, str, dict) -> int
    """Start a detached background process and return its pid.

    The child outlives this one: on POSIX it gets its own session, on Windows
    its own process group and no console at all. It must also not inherit
    anything of ours -- see :func:`_no_inherited_handles`, which is the whole
    difference between `exchangesim start | tail` returning and hanging.

    ``output_path`` receives the child's stdout and stderr. That should stay
    almost empty: the daemons log to their own rotated file. What lands here is
    what happens outside logging -- an import error, a traceback on the way
    down, a message from the interpreter itself.
    """
    handle = open(output_path, "ab", 0)
    try:
        kwargs = {
            "cwd": cwd,
            "stdin": subprocess.DEVNULL,
            "stdout": handle,
            "stderr": subprocess.STDOUT,
        }
        if env is not None:
            kwargs["env"] = env
        if WINDOWS:
            # DETACHED_PROCESS so the daemon survives its terminal, and no
            # console means `stop` terminates rather than signalling -- which
            # request_stop already had to do whenever stop ran from a second
            # terminal.
            kwargs["creationflags"] = (_CREATE_NEW_PROCESS_GROUP
                                       | _DETACHED_PROCESS)
        else:
            # Its own session, so closing the terminal does not take the venue
            # with it. close_fds is POSIX-only here: Python 3.6 on Windows
            # refuses it outright when the standard handles are redirected.
            kwargs["preexec_fn"] = os.setsid
            kwargs["close_fds"] = True
        with _no_inherited_handles():
            child = subprocess.Popen(argv, **kwargs)
    finally:
        handle.close()
    # The child is deliberately never waited for -- it outlives this process.
    # Holding the Popen keeps its destructor from warning about that, and the
    # controller exits moments later, so nothing accumulates.
    _spawned.append(child)
    return child.pid


@contextlib.contextmanager
def _no_inherited_handles():
    """Stop a Windows child inheriting *this* process's standard handles.

    Python 3.6 on Windows refuses ``close_fds`` when the standard handles are
    redirected, and then passes ``bInheritHandles=TRUE`` -- so every inheritable
    handle we hold goes to the child, the redirection notwithstanding. Run
    ``exchangesim start | tail`` and the daemon inherits the write end of that
    pipe; ``tail`` then waits for an EOF that cannot come until the venue exits,
    which is to say never. It hangs the terminal, and it hung this one twice.

    Clearing the inherit flag on fds 0-2 for the length of the spawn is the
    narrow fix: the handles the child is *given* are duplicated by
    CreateProcess and unaffected.

    A no-op everywhere else, where ``close_fds`` does the same job properly.
    """
    if not WINDOWS:
        yield
        return

    import ctypes
    import msvcrt

    kernel32 = ctypes.windll.kernel32
    saved = []
    for fd in (0, 1, 2):
        try:
            handle = msvcrt.get_osfhandle(fd)
        except (OSError, ValueError):
            continue
        flags = ctypes.c_ulong()
        if not kernel32.GetHandleInformation(
                ctypes.c_void_p(handle), ctypes.byref(flags)):
            continue
        saved.append((handle, flags.value))
        kernel32.SetHandleInformation(
            ctypes.c_void_p(handle), _HANDLE_FLAG_INHERIT, 0)
    try:
        yield
    finally:
        for handle, flags in saved:
            kernel32.SetHandleInformation(
                ctypes.c_void_p(handle), _HANDLE_FLAG_INHERIT,
                flags & _HANDLE_FLAG_INHERIT)


def command_line(pid):
    # type: (int) -> str
    """The process's command line, or '' where the platform will not say.

    Only Linux answers this without a dependency, which is enough: it is used
    to tell our own daemon from an unrelated process that inherited a recycled
    pid, and the deployment target is Linux.
    """
    path = "/proc/%d/cmdline" % pid
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except (IOError, OSError):
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def interpreter():
    # type: () -> str
    """The interpreter to start children with -- this one."""
    return sys.executable or "python"


def _windows_alive(pid):
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)

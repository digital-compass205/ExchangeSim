"""Control command registry.

Commands are plain callables taking ``(context, args)`` and returning anything
JSON-serialisable. ``context`` carries the venue and the calling control
session; ``args`` is the decoded ``args`` object from the request.

Keeping the registry separate from the server means commands can be dispatched
directly in unit tests without opening a socket.
"""

import logging

log = logging.getLogger(__name__)

# Error codes returned to clients. The CLI maps these onto process exit codes,
# so they are part of the interface and must stay stable.
E_BAD_REQUEST = "bad_request"
E_UNKNOWN_COMMAND = "unknown_command"
E_BAD_ARGS = "bad_args"
E_NOT_FOUND = "not_found"
E_CONFLICT = "conflict"
E_UNAUTHORISED = "unauthorised"
E_INTERNAL = "internal"


class CommandError(Exception):
    """A command failure that should be reported to the client, not logged as a bug."""

    def __init__(self, message, code=E_BAD_ARGS):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


class Command(object):
    __slots__ = ("name", "handler", "summary", "requires_auth", "audit")

    def __init__(self, name, handler, summary, requires_auth=True, audit=True):
        self.name = name
        self.handler = handler
        self.summary = summary
        self.requires_auth = requires_auth
        #: Whether an invocation belongs in the audit tape. True by default:
        #: a command that changes something must be recorded even if whoever
        #: added it never thought about the audit, and the cost of the default
        #: being wrong the other way -- a query filling the tape with noise --
        #: is visible rather than silent. Queries opt out explicitly.
        self.audit = audit


class CommandRegistry(object):
    """Name -> handler mapping with dispatch."""

    def __init__(self):
        self._commands = {}
        #: Set by the runner once the venue exists. Hooking here rather than in
        #: the control server covers every caller at once: the CLI, the web
        #: proxy, the scenario runner and the test harnesses, which dispatch
        #: against the registry directly.
        self.audit = None

    def register(self, name, handler, summary="", requires_auth=True,
                 audit=True):
        if name in self._commands:
            raise ValueError("control command %r already registered" % name)
        self._commands[name] = Command(name, handler, summary, requires_auth,
                                       audit)

    def add(self, name, summary="", requires_auth=True, audit=True):
        """Decorator form of :meth:`register`."""
        def decorate(handler):
            self.register(name, handler, summary, requires_auth, audit)
            return handler
        return decorate

    def get(self, name):
        return self._commands.get(name)

    def names(self):
        return sorted(self._commands)

    def commands(self):
        return [self._commands[name] for name in sorted(self._commands)]

    def describe(self):
        return [{"command": c.name, "summary": c.summary}
                for c in sorted(self._commands.values(), key=lambda c: c.name)]

    def dispatch(self, name, context, args):
        """Run a command. Raises :class:`CommandError` for anything reportable."""
        command = self._commands.get(name)
        if command is None:
            raise CommandError("unknown command '%s'" % name, E_UNKNOWN_COMMAND)
        if not isinstance(args, dict):
            raise CommandError("'args' must be an object", E_BAD_REQUEST)

        if self.audit is None or not command.audit:
            return command.handler(context, args)

        # Recorded *before* the handler runs, so the command sits above the
        # messages it causes rather than below them: an order entered from the
        # board would otherwise appear after its own execution reports. The
        # entry is completed in place once the handler answers.
        session = getattr(context, "session", None)
        entry = self.audit.record_command(
            command.name, args, session=getattr(session, "peer", None),
            symbol=args.get("symbol"))
        try:
            result = command.handler(context, args)
        except CommandError as exc:
            entry.complete(False, {"code": exc.code, "message": exc.message})
            raise
        except Exception as exc:
            entry.complete(False, {"code": E_INTERNAL,
                                   "message": "%s: %s" % (type(exc).__name__, exc)})
            raise
        entry.complete(True, None, result)
        return result


# -- argument helpers --------------------------------------------------------
#
# Control clients include hand-typed CLI invocations, so argument errors are
# expected traffic rather than exceptional. These produce messages that name
# the offending field.

def arg_str(args, key, default=None, required=False, upper=False):
    value = args.get(key, default)
    if value is None:
        if required:
            raise CommandError("missing required argument '%s'" % key)
        return None
    if not isinstance(value, str):
        raise CommandError("argument '%s' must be a string" % key)
    return value.upper() if upper else value


def arg_int(args, key, default=None, required=False, minimum=None, maximum=None):
    value = args.get(key, default)
    if value is None:
        if required:
            raise CommandError("missing required argument '%s'" % key)
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandError("argument '%s' must be an integer" % key)
    if minimum is not None and value < minimum:
        raise CommandError("argument '%s' must be >= %d" % (key, minimum))
    if maximum is not None and value > maximum:
        raise CommandError("argument '%s' must be <= %d" % (key, maximum))
    return value


def arg_bool(args, key, default=None, required=False):
    value = args.get(key, default)
    if value is None:
        if required:
            raise CommandError("missing required argument '%s'" % key)
        return None
    if not isinstance(value, bool):
        raise CommandError("argument '%s' must be a boolean" % key)
    return value


def arg_list(args, key, default=None, required=False):
    value = args.get(key, default)
    if value is None:
        if required:
            raise CommandError("missing required argument '%s'" % key)
        return None
    if not isinstance(value, list):
        raise CommandError("argument '%s' must be an array" % key)
    return value


def arg_choice(args, key, choices, default=None, required=False, upper=True):
    value = arg_str(args, key, default, required, upper=upper)
    if value is None:
        return None
    if value not in choices:
        raise CommandError("argument '%s' must be one of: %s"
                           % (key, ", ".join(sorted(choices))))
    return value

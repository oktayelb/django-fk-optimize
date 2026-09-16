"""Record every query, and the user line that caused it.

The hook is `connection.execute_wrapper`, which is Django's supported
instrumentation point and fires for every query in every context -- HTTP,
celery, a management command, a test.  Middleware only sees HTTP, so the
middleware is a shim over this and not the other way round.  Nothing in
`django.db` is monkeypatched; a profiler that rewrites the ORM is a profiler
that breaks on the next Django release.

**contextvars, not threading.local.**  A `threading.local` holds the recorder
against the thread that set it, and under ASGI the request is a task on an
event loop while the ORM work is handed to a `sync_to_async` executor thread --
a different thread, with a different local, and therefore no recorder.  A
`ContextVar` is copied into that call and restored out of it by asgiref, so the
recorder follows the request rather than the thread.  It also behaves correctly
for threads, which start with an empty context and so cannot see each other's
recorders.

**Everything here is wrapped in `try/except Exception`.**  This code runs in
somebody's production request path to collect a performance hint.  A bug in it
must never become a 500, so an internal failure switches recording off for the
rest of the process and the query goes through untouched.

**Parameters are never recorded.**  `params` arrives here and is passed
straight through; only the normalised shape is stored, with literals and
placeholders flattened out of it.  A recording file ends up in a repository, a
bug report or a CI artifact, and none of those are places for the contents of a
WHERE clause.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import sys
import sysconfig
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from . import store
from .store import Record

# The recorder the current request/task is writing into, if any.
_active: ContextVar[Recorder | None] = ContextVar("fk_optimize_recorder", default=None)
# Set by the analysis layer around its own benchmark and cardinality queries:
# a profiler that profiles itself reports its own work as the application's.
_suppress: ContextVar[bool] = ContextVar("fk_optimize_suppressed", default=False)

# Process-wide kill switch, flipped by any internal error.
_disabled = False

# path -> lines known to be in that file, so MAX_RECORDS costs one line count
# per process rather than one per flush.  Separate processes keep separate
# counts, so the cap is per worker and approximate in aggregate; the point of
# it is to bound the file, not to hit a number exactly.
_written: dict[str, int] = {}

# How far out to walk looking for user code, and how many extra user frames to
# keep once it is found.
MAX_DEPTH = 80
CONTEXT_FRAMES = 2

TEMPLATE = "template"
SERIALIZER = "serializer"
PYTHON = "python"


# ----------------------------------------------------------------------
# sql normalisation
# ----------------------------------------------------------------------

# Single quotes are string literals on every backend Django ships; double
# quotes and backticks are identifiers, and stripping those would erase the
# table name the whole analysis is keyed on.
_STRING = re.compile(r"'(?:[^']|'')*'")
_PLACEHOLDER = re.compile(r"%\([^)]*\)s|%s|:\w+|\$\d+|\?")
# Guarded on both sides so a digit inside an identifier -- col_2, table2 --
# survives, while `LIMIT 21` and `= 5` do not.
_NUMBER = re.compile(r"(?<![\w$])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b")
_WHITESPACE = re.compile(r"\s+")

PLACEHOLDER = "?"


def normalise(sql) -> str:
    """The shape of a query: no literals, no parameters, one canonical space.

    Literals are stripped as well as parameters because plenty of SQL reaches
    the database with its values already inlined -- `.extra()`, a raw query, a
    hand-built cursor call -- and a shape that still carries a customer's email
    address is not a shape.
    """
    text = sql if isinstance(sql, str) else str(sql)
    text = _STRING.sub(PLACEHOLDER, text)
    text = _PLACEHOLDER.sub(PLACEHOLDER, text)
    text = _NUMBER.sub(PLACEHOLDER, text)
    return _WHITESPACE.sub(" ", text).strip()


def shape_hash(shape: str) -> str:
    return hashlib.blake2b(shape.encode("utf-8", "replace"), digest_size=8).hexdigest()


# ----------------------------------------------------------------------
# attribution
# ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def _library_prefixes() -> tuple[str, ...]:
    """Directories whose frames are never the answer.

    Derived from the installed packages rather than matched by name: a project
    living in ~/code/django/ is user code, and a substring test for "django"
    would attribute every one of its queries to Django itself.
    """
    prefixes = {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
    try:
        import django

        prefixes.add(os.path.dirname(os.path.abspath(django.__file__)))
    except Exception:
        pass
    for name in ("stdlib", "platstdlib", "purelib", "platlib"):
        path = sysconfig.get_paths().get(name)
        if path:
            prefixes.add(path)
    return tuple(sorted(prefix for prefix in prefixes if prefix))


@lru_cache(maxsize=1)
def _template_marker() -> str:
    try:
        import django

        return (
            os.path.join(os.path.dirname(os.path.abspath(django.__file__)), "template")
            + os.sep
        )
    except Exception:
        return os.sep + "django" + os.sep + "template" + os.sep


_SERIALIZER_MARKER = os.sep + "rest_framework" + os.sep


def _is_library(filename: str) -> bool:
    if not filename or filename.startswith("<"):  # <string>, <frozen importlib>
        return True
    if filename.startswith(_library_prefixes()):
        return True
    return "site-packages" in filename or "dist-packages" in filename


def attribute():
    """(user frame, context frames, source) for whatever called the database.

    `sys._getframe` rather than `traceback.extract_stack`: the latter reads and
    caches the source file of every frame to fill in the text of the line,
    which is far too slow to leave switched on in a running application.  The
    line number is all this needs.

    The answer is the *deepest* non-library frame -- the line that touched the
    attribute, not the view several frames above it.  Library frames passed on
    the way out are not discarded either: a query that came through
    django/template/ was caused by a template expression the AST scanner cannot
    see, and that classification is the only trace of it that survives.
    """
    source = PYTHON
    user = None
    context: list[tuple[str, int, str]] = []
    frame = sys._getframe()
    depth = 0
    while frame is not None and depth < MAX_DEPTH:
        filename = frame.f_code.co_filename
        if _is_library(filename):
            if user is None:
                # Only frames inside the call being attributed count: a view
                # rendered by a template is not a template query.
                if source == PYTHON and filename.startswith(_template_marker()):
                    source = TEMPLATE
                elif source == PYTHON and _SERIALIZER_MARKER in filename:
                    source = SERIALIZER
        else:
            entry = (filename, frame.f_lineno, frame.f_code.co_name)
            if user is None:
                user = entry
            else:
                context.append(entry)
                if len(context) >= CONTEXT_FRAMES:
                    break
        frame = frame.f_back
        depth += 1
    return user, tuple(context), source


# ----------------------------------------------------------------------
# the wrapper
# ----------------------------------------------------------------------


def _disable() -> None:
    """Stop recording for the rest of the process.

    Deliberately silent and deliberately permanent: whatever went wrong will go
    wrong on the next query too, and an exception per query in a request path
    is a worse outage than no recording at all.
    """
    global _disabled
    _disabled = True


def _execute_wrapper(execute, sql, params, many, context):
    """Django's instrumentation signature. `params` is passed through, never read."""
    recorder = _active.get()
    if recorder is None or _disabled or _suppress.get():
        return execute(sql, params, many, context)
    started = time.perf_counter()
    try:
        return execute(sql, params, many, context)
    finally:
        try:
            recorder.capture(sql, time.perf_counter() - started, context)
        except Exception:
            _disable()


def _alias_of(context) -> str:
    try:
        return context["connection"].alias
    except Exception:
        return "default"


def _connections():
    from django.db import connections

    return [connections[alias] for alias in connections]


# ----------------------------------------------------------------------
# the recorder
# ----------------------------------------------------------------------


class Recorder:
    """Buffers one request's queries, writes them once at the end.

    One write per request, not one per query: a recorder that opens a file
    inside the query path has turned a read-heavy page into a write-heavy one
    and is now measuring itself.
    """

    def __init__(
        self,
        path=None,
        *,
        max_records: int | None = None,
        sample_rate: float | None = None,
        enabled: bool | None = None,
    ):
        self.path = Path(path) if path else None
        self.max_records = max_records
        self.sample_rate = sample_rate
        self.enabled = enabled
        self.records: list[Record] = []
        self.dropped = 0
        self.written = 0
        self.skipped = False  # sampling, or switched off, or already broken
        self.active = False
        self._token = None
        self._installed: list = []

    # -- configuration -------------------------------------------------

    def _configure(self) -> None:
        from ..conf import get_config

        config = get_config()
        if self.path is None:
            self.path = config.recording_path
        if self.max_records is None:
            self.max_records = config.max_records
        if self.sample_rate is None:
            self.sample_rate = config.sample_rate
        if self.enabled is None:
            self.enabled = config.enabled

    def _sampled_in(self) -> bool:
        rate = self.sample_rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return random.random() < rate

    # -- lifecycle -----------------------------------------------------

    def start(self):
        """Activate and install. Never raises."""
        try:
            if _disabled:
                self.skipped = True
                return self
            self._configure()
            if not self.enabled or not self.max_records or not self._sampled_in():
                self.skipped = True
                return self
            self.activate()
            self.install()
        except Exception:
            _disable()
            self.skipped = True
        return self

    def stop(self) -> int:
        """Uninstall, deactivate, and write the buffer. Never raises."""
        try:
            self.uninstall()
        except Exception:
            _disable()
        try:
            self.deactivate()
        except Exception:
            _disable()
        return self.flush()

    def activate(self) -> None:
        if not self.active:
            self._token = _active.set(self)
            self.active = True

    def deactivate(self) -> None:
        if not self.active:
            return
        self.active = False
        token, self._token = self._token, None
        if token is not None:
            try:
                _active.reset(token)
            except ValueError:
                # Set in one context and reset in another (an executor thread
                # that outlived its task). Clearing is the honest fallback.
                _active.set(None)

    def install(self) -> None:
        """Add the wrapper to this thread's connections.

        `execute_wrappers.remove()` rather than the context manager's blind
        `pop()`: under ASGI several requests share one executor thread and do
        not unwind in order, and popping somebody else's wrapper off the stack
        would be a bug in their tool, reported against them.
        """
        for connection in _connections():
            wrappers = getattr(connection, "execute_wrappers", None)
            if wrappers is None:
                continue
            wrappers.append(_execute_wrapper)
            self._installed.append(connection)

    def uninstall(self) -> None:
        while self._installed:
            connection = self._installed.pop()
            wrappers = getattr(connection, "execute_wrappers", None)
            if not wrappers:
                continue
            try:
                wrappers.remove(_execute_wrapper)
            except ValueError:
                pass

    # -- recording -----------------------------------------------------

    def capture(self, sql, duration: float, context=None) -> None:
        if self.max_records and len(self.records) >= self.max_records:
            self.dropped += 1
            return
        shape = normalise(sql)
        user, frames, source = attribute()
        path, line, function = user or ("", 0, "")
        self.records.append(
            Record(
                ts=time.time(),
                shape=shape,
                shape_hash=shape_hash(shape),
                duration=duration,
                file=path,
                line=line,
                function=function,
                source=source,
                context=frames,
                db=_alias_of(context) if context is not None else "default",
            )
        )

    def flush(self) -> int:
        """Write the buffer out and empty it. Never raises."""
        if not self.records:
            return 0
        pending, self.records = self.records, []
        try:
            room = _room(self.path, self.max_records)
            written = store.append(self.path, pending[:room])
            _spend(self.path, written)
        except Exception:
            _disable()
            return 0
        self.written += written
        self.dropped += len(pending) - written
        return written

    # -- as a context manager ------------------------------------------

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()
        return False


def _room(path, max_records: int | None) -> int:
    if not max_records:
        return 0
    key = os.path.abspath(str(path))
    known = _written.get(key)
    if known is None:
        known = store.count_lines(path)
        _written[key] = known
    return max(max_records - known, 0)


def _spend(path, count: int) -> None:
    key = os.path.abspath(str(path))
    _written[key] = _written.get(key, 0) + count


# ----------------------------------------------------------------------
# public entry points
# ----------------------------------------------------------------------


@contextmanager
def record(path=None, **kw):
    """Record every query issued in this block.

    The non-HTTP entry point: a celery task, a script, a test, a management
    command.  The middleware does exactly this around a request and nothing
    else.
    """
    recorder = Recorder(path, **kw)
    recorder.start()
    try:
        yield recorder
    finally:
        recorder.stop()


@contextmanager
def suppressed():
    """Do not record queries issued in this block.

    The analysis layer benchmarks and counts rows against the same database it
    is reporting on.  Without this, a run of the command would append its own
    traffic to the recording and then read it back as the application's.
    """
    token = _suppress.set(True)
    try:
        yield
    finally:
        _suppress.reset(token)


def is_recording() -> bool:
    return not _disabled and not _suppress.get() and _active.get() is not None


def is_suppressed() -> bool:
    return _suppress.get()


def is_disabled() -> bool:
    return _disabled


def reset() -> None:
    """Clear the process-wide state: the kill switch and the record budget.

    For tests, and for a long-lived process that wants to start recording
    again after whatever broke has been fixed.
    """
    global _disabled
    _disabled = False
    _written.clear()
    _library_prefixes.cache_clear()
    _template_marker.cache_clear()

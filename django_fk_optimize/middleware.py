"""Turn the recorder on for the duration of a request.

A shim, and deliberately nothing more.  The recording itself is
`connection.execute_wrapper`, which fires for queries from a celery task or a
management command as readily as from a view; middleware only ever sees HTTP,
so it is the entry point for one context rather than the mechanism.

Sync and async are both handled, and the async path is not just `await`.  Under
ASGI the request lives on the event loop while the ORM runs in a
`sync_to_async` executor thread, and `execute_wrappers` belongs to the
connection object of *that* thread.  So the wrapper is installed and removed
from inside `sync_to_async(thread_sensitive=True)`, which is the same thread
Django will use for the queries, while the ContextVar that names the active
recorder is set on the request's own context and copied into each of those
calls.  Installing from the event loop would instrument a connection nothing
ever uses.

Nothing here can break a request: every entry point swallows its own errors and
a failure leaves the recording off rather than the response unsent.

    MIDDLEWARE = [
        ...,
        "django_fk_optimize.middleware.FkOptimizeMiddleware",
    ]
"""

from __future__ import annotations

from asgiref.sync import sync_to_async

from .recording.wrapper import Recorder

try:  # asgiref >= 3.7, which every supported Django ships with
    from asgiref.sync import iscoroutinefunction, markcoroutinefunction
except ImportError:  # pragma: no cover - asgiref < 3.7
    import asyncio

    iscoroutinefunction = asyncio.iscoroutinefunction

    def markcoroutinefunction(obj):
        obj._is_coroutine = asyncio.coroutines._is_coroutine
        return obj


def _begin(install: bool = True) -> Recorder | None:
    """A started recorder, or None if there is nothing to record into."""
    try:
        recorder = Recorder().start(install=install)
    except Exception:
        return None
    return None if recorder.skipped else recorder


class FkOptimizeMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.async_mode:
            return self.__acall__(request)
        recorder = _begin()
        if recorder is None:
            return self.get_response(request)
        try:
            return self.get_response(request)
        finally:
            recorder.stop()

    async def __acall__(self, request):
        recorder = _begin(install=False)
        if recorder is not None:
            try:
                await sync_to_async(recorder.install, thread_sensitive=True)()
            except Exception:
                recorder.deactivate()
                recorder = None
        if recorder is None:
            return await self.get_response(request)
        try:
            return await self.get_response(request)
        finally:
            try:
                # The write is blocking, so it stays off the event loop.
                await sync_to_async(recorder.finish, thread_sensitive=True)()
            except Exception:
                pass
            recorder.deactivate()

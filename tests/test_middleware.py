"""The HTTP shim. It has one job and one thing it must never do."""

import asyncio

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.test.utils import override_settings

from django_fk_optimize import recording
from django_fk_optimize.middleware import FkOptimizeMiddleware
from django_fk_optimize.recording import store, wrapper


@pytest.fixture(autouse=True)
def clean_recorder_state():
    recording.reset()
    yield
    recording.reset()


@pytest.fixture
def request_():
    from django.test import RequestFactory

    return RequestFactory().get("/books/")


def settings_for(path, **extra):
    return override_settings(FK_OPTIMIZE={"RECORDING_PATH": str(path), **extra})


def test_a_request_is_recorded(db, tmp_path, request_):
    from tests.testapp.models import Publisher

    def get_response(request):
        list(Publisher.objects.all())
        return "response"

    path = tmp_path / "recording.jsonl"
    with settings_for(path):
        middleware = FkOptimizeMiddleware(get_response)
        assert middleware(request_) == "response"

    records = store.load(path).records
    assert len(records) == 1
    assert "testapp_publisher" in records[0].shape
    assert records[0].function == "get_response"


def test_each_request_is_one_invocation(db, tmp_path, request_):
    """Three hits on a page that issues two lookups is an N of two, not six.

    The middleware builds a recorder per request, so the boundary was always
    there; until the recorder stamped a run onto its records, nothing on disk
    could see it and the N grew with the traffic.
    """
    from tests.testapp.models import Publisher

    def get_response(request):
        for _ in range(2):
            list(Publisher.objects.all())
        return "response"

    path = tmp_path / "recording.jsonl"
    with settings_for(path):
        middleware = FkOptimizeMiddleware(get_response)
        for _ in range(3):
            middleware(request_)

    loaded = store.load(path)
    (found,) = loaded.groups()

    assert len({record.run for record in loaded.records}) == 3
    assert found.count == 2
    assert found.total == 6
    assert found.invocations == 3


def test_queries_outside_the_request_are_not_recorded(db, tmp_path, request_):
    from tests.testapp.models import Publisher

    path = tmp_path / "recording.jsonl"
    with settings_for(path):
        middleware = FkOptimizeMiddleware(lambda request: "response")
        middleware(request_)
        list(Publisher.objects.all())

    assert store.load(path).count == 0


def test_it_is_a_no_op_when_disabled(db, tmp_path, request_):
    from tests.testapp.models import Publisher

    def get_response(request):
        assert wrapper._execute_wrapper not in connection.execute_wrappers
        list(Publisher.objects.all())
        return "response"

    path = tmp_path / "recording.jsonl"
    with settings_for(path, ENABLED=False):
        assert FkOptimizeMiddleware(get_response)(request_) == "response"

    assert not path.exists()


def test_it_is_a_no_op_when_the_sample_rate_skips_the_request(db, tmp_path, request_):
    from tests.testapp.models import Publisher

    path = tmp_path / "recording.jsonl"
    with settings_for(path, SAMPLE_RATE=0.0):
        FkOptimizeMiddleware(lambda request: list(Publisher.objects.all()))(request_)

    assert not path.exists()


def test_the_wrapper_is_removed_even_when_the_view_raises(db, tmp_path, request_):
    from tests.testapp.models import Publisher

    def get_response(request):
        list(Publisher.objects.all())
        raise ValueError("boom")

    path = tmp_path / "recording.jsonl"
    with settings_for(path):
        with pytest.raises(ValueError):
            FkOptimizeMiddleware(get_response)(request_)

    assert connection.execute_wrappers == []
    # The queries the failed request did issue are still worth having.
    assert store.load(path).count == 1


def test_a_broken_recorder_does_not_break_the_request(
    db, tmp_path, request_, monkeypatch
):
    def explode(self, install=True):
        raise RuntimeError("bug in the recorder")

    monkeypatch.setattr(wrapper.Recorder, "start", explode)

    path = tmp_path / "recording.jsonl"
    with settings_for(path):
        assert FkOptimizeMiddleware(lambda request: "response")(request_) == "response"

    assert connection.execute_wrappers == []
    assert not path.exists()


def test_the_middleware_is_sync_by_default(request_):
    from asgiref.sync import iscoroutinefunction

    middleware = FkOptimizeMiddleware(lambda request: "response")

    assert middleware.async_mode is False
    assert iscoroutinefunction(middleware) is False


# -- asgi --------------------------------------------------------------


def test_the_middleware_marks_itself_async_for_an_async_handler():
    from asgiref.sync import iscoroutinefunction

    async def get_response(request):
        return "response"

    middleware = FkOptimizeMiddleware(get_response)

    assert middleware.async_mode is True
    assert iscoroutinefunction(middleware) is True


def test_an_async_request_records_the_queries_its_orm_thread_issues(
    db, tmp_path, request_
):
    from tests.testapp.models import Publisher

    def query():
        # Whatever thread this lands on is the thread the wrapper has to be on.
        return list(Publisher.objects.all())

    async def get_response(request):
        await sync_to_async(query, thread_sensitive=True)()
        return "response"

    path = tmp_path / "recording.jsonl"
    with settings_for(path):
        middleware = FkOptimizeMiddleware(get_response)
        assert asyncio.run(middleware(request_)) == "response"

    records = store.load(path).records
    assert len(records) == 1
    assert "testapp_publisher" in records[0].shape
    assert records[0].function == "query"


def test_an_async_request_is_a_no_op_when_disabled(db, tmp_path, request_):
    from tests.testapp.models import Publisher

    async def get_response(request):
        await sync_to_async(lambda: list(Publisher.objects.all()))()
        return "response"

    path = tmp_path / "recording.jsonl"
    with settings_for(path, ENABLED=False):
        assert asyncio.run(FkOptimizeMiddleware(get_response)(request_)) == "response"

    assert not path.exists()


def test_an_async_view_that_raises_still_flushes(db, tmp_path, request_):
    from tests.testapp.models import Publisher

    async def get_response(request):
        await sync_to_async(lambda: list(Publisher.objects.all()))()
        raise ValueError("boom")

    path = tmp_path / "recording.jsonl"
    with settings_for(path):
        with pytest.raises(ValueError):
            asyncio.run(FkOptimizeMiddleware(get_response)(request_))

    assert store.load(path).count == 1
    assert recording.is_recording() is False

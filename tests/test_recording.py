"""The recorder: what it captures, where it attributes it, and what it refuses.

Everything asserted here is deterministic -- counts, line numbers, file
contents.  Nothing asserts on a duration, because a duration asserted on is a
test that fails on a loaded machine.
"""

import sys

import pytest
from django.db import connection
from django.test.utils import override_settings

from django_fk_optimize import recording
from django_fk_optimize.recording import store, wrapper


@pytest.fixture(autouse=True)
def clean_recorder_state():
    """Reset the process-wide kill switch and record budget around each test."""
    recording.reset()
    yield
    recording.reset()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "recording.jsonl"


def recorded(path):
    """What ended up on disk. A recorder empties its buffer when it flushes."""
    return store.load(path).records


# -- normalisation -----------------------------------------------------


def test_placeholders_and_literals_both_become_one_token():
    assert (
        recording.normalise("SELECT * FROM t WHERE a = %s")
        == "SELECT * FROM t WHERE a = ?"
    )
    assert (
        recording.normalise("SELECT * FROM t WHERE a = 'bob'")
        == "SELECT * FROM t WHERE a = ?"
    )
    assert (
        recording.normalise("SELECT * FROM t WHERE a = :name")
        == "SELECT * FROM t WHERE a = ?"
    )
    assert recording.normalise("SELECT * FROM t LIMIT 21") == "SELECT * FROM t LIMIT ?"


def test_a_query_built_with_and_without_params_has_the_same_shape():
    with_params = recording.normalise('SELECT "a" FROM "t" WHERE "id" = %s')
    inlined = recording.normalise('SELECT "a" FROM "t" WHERE "id" = 4321')

    assert with_params == inlined


def test_identifiers_keep_their_digits():
    shape = recording.normalise('SELECT "col_2", table2.x FROM "table2"')

    assert '"col_2"' in shape
    assert '"table2"' in shape


def test_whitespace_is_collapsed():
    assert recording.normalise("  SELECT\n  a\tFROM  t  ") == "SELECT a FROM t"


def test_the_hash_follows_the_shape():
    first = recording.shape_hash(recording.normalise("SELECT a FROM t WHERE b = 1"))
    second = recording.shape_hash(recording.normalise("SELECT a FROM t WHERE b = 2"))
    third = recording.shape_hash(recording.normalise("SELECT a FROM t WHERE c = 1"))

    assert first == second
    assert first != third


# -- capture -----------------------------------------------------------


def test_a_query_inside_record_is_captured(db, path):
    from tests.testapp.models import Publisher

    with recording.record(path) as recorder:
        list(Publisher.objects.all())

    assert recorder.skipped is False
    assert recorder.written >= 1
    recording_file = store.load(path)
    assert recording_file.count >= 1
    assert any("testapp_publisher" in r.shape for r in recording_file.records)


def test_nothing_is_recorded_outside_the_block(db, path):
    from tests.testapp.models import Publisher

    with recording.record(path):
        pass
    list(Publisher.objects.all())

    assert store.load(path).count == 0


def test_is_recording_tracks_the_block(db, path):
    assert recording.is_recording() is False
    with recording.record(path):
        assert recording.is_recording() is True
        with recording.suppressed():
            assert recording.is_recording() is False
        assert recording.is_recording() is True
    assert recording.is_recording() is False


def test_attribution_lands_on_the_callers_own_line(db, path):
    from tests.testapp.models import Publisher

    with recording.record(path):
        expected_line = sys._getframe().f_lineno + 1
        list(Publisher.objects.all())

    first = recorded(path)[0]
    assert first.file == __file__
    assert first.line == expected_line
    assert first.function == "test_attribution_lands_on_the_callers_own_line"
    assert "/django/db/" not in first.file
    assert first.source == recording.PYTHON


def test_attribution_keeps_a_couple_of_outer_frames(db, path):
    from tests.testapp.models import Publisher

    def inner():
        list(Publisher.objects.all())

    def outer():
        inner()

    with recording.record(path):
        outer()

    first = recorded(path)[0]
    assert first.function == "inner"
    assert [frame[2] for frame in first.context] == [
        "outer",
        "test_attribution_keeps_a_couple_of_outer_frames",
    ]


def test_an_n_plus_one_over_a_real_fk_is_one_group(library, path):
    from tests.testapp.models import Book

    names = []
    with recording.record(path):
        for book in Book.objects.all():
            names.append(book.publisher.name)

    groups = store.load(path).groups()
    lookups = [g for g in groups if g.table == "testapp_publisher"]

    assert len(lookups) == 1
    found = lookups[0]
    assert found.count == len(library["books"])
    assert found.kind == store.SINGLE_ROW
    assert found.is_n_plus_one is True
    assert (
        found.attribution.function == "test_an_n_plus_one_over_a_real_fk_is_one_group"
    )
    # The listing itself is one bulk read of the other table, not an N+1.
    listings = [g for g in groups if g.table == "testapp_book"]
    assert listings and all(g.kind == store.BULK for g in listings)


def test_the_recorder_sees_exactly_the_queries_django_counts(library, path):
    """The one count that can be checked against something other than itself."""
    from django.test.utils import CaptureQueriesContext

    from tests.testapp.models import Book

    names = []
    with CaptureQueriesContext(connection) as captured:
        with recording.record(path):
            for book in Book.objects.all():
                names.append(book.publisher.name)

    assert store.count_lines(path) == len(captured.captured_queries)
    assert len(captured.captured_queries) == len(library["books"]) + 1


def test_select_related_leaves_no_n_plus_one(library, path):
    from tests.testapp.models import Book

    names = []
    with recording.record(path):
        for book in Book.objects.select_related("publisher"):
            names.append(book.publisher.name)

    assert [g for g in store.load(path).groups() if g.is_n_plus_one] == []


def test_a_template_touch_is_classified_as_a_template(library, path):
    from django.template import Context, Engine

    from tests.testapp.models import Book

    template = Engine().from_string("{{ book.publisher.name }}")
    book = Book.objects.first()

    with recording.record(path):
        template.render(Context({"book": book}))

    sources = {record.source for record in recorded(path)}
    assert recording.TEMPLATE in sources


# -- what must never be recorded ---------------------------------------


def test_parameters_never_reach_the_file(db, path):
    from tests.testapp.models import Publisher

    secret = "hunter2-correct-horse"
    Publisher.objects.create(name=secret)

    with recording.record(path):
        list(Publisher.objects.filter(name=secret))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM testapp_publisher WHERE name = 'hunter2-inlined'"
            )

    written = path.read_text()
    assert "hunter2" not in written
    assert "testapp_publisher" in written  # the shape itself survived


def test_suppressed_prevents_recording(db, path):
    from tests.testapp.models import Publisher

    with recording.record(path) as recorder:
        with recording.suppressed():
            list(Publisher.objects.all())

    assert recorder.records == []
    assert store.load(path).count == 0


def test_suppression_ends_with_the_block(db, path):
    from tests.testapp.models import Publisher

    with recording.record(path):
        with recording.suppressed():
            list(Publisher.objects.all())
        list(Publisher.objects.all())

    assert len(recorded(path)) == 1


# -- bounds ------------------------------------------------------------


def test_max_records_caps_what_is_written(db, path):
    from tests.testapp.models import Publisher

    with recording.record(path, max_records=2) as recorder:
        for _ in range(5):
            list(Publisher.objects.all())

    assert store.count_lines(path) == 2
    assert recorder.dropped == 3


def test_max_records_is_a_ceiling_across_blocks(db, path):
    from tests.testapp.models import Publisher

    for _ in range(3):
        with recording.record(path, max_records=2):
            list(Publisher.objects.all())

    assert store.count_lines(path) == 2


def test_sample_rate_of_zero_skips_the_whole_block(db, path):
    from tests.testapp.models import Publisher

    with recording.record(path, sample_rate=0.0) as recorder:
        list(Publisher.objects.all())

    assert recorder.skipped is True
    assert recorder.records == []
    assert not path.exists()


def test_sample_rate_of_one_always_records(db, path):
    from tests.testapp.models import Publisher

    with recording.record(path, sample_rate=1.0) as recorder:
        list(Publisher.objects.all())

    assert recorder.skipped is False
    assert recorder.records == [] and recorder.written == 1


def test_disabled_by_settings_records_nothing(db, path):
    from tests.testapp.models import Publisher

    with override_settings(FK_OPTIMIZE={"ENABLED": False}):
        with recording.record(path) as recorder:
            list(Publisher.objects.all())

    assert recorder.skipped is True
    assert not path.exists()


def test_settings_supply_the_defaults(db, tmp_path):
    from tests.testapp.models import Publisher

    configured = tmp_path / "from-settings.jsonl"
    with override_settings(FK_OPTIMIZE={"RECORDING_PATH": str(configured)}):
        with recording.record():
            list(Publisher.objects.all())

    assert store.load(configured).count == 1


# -- safety ------------------------------------------------------------


def test_a_recorder_bug_disables_recording_instead_of_raising(db, path, monkeypatch):
    from tests.testapp.models import Publisher

    def explode(sql):
        raise RuntimeError("bug in the recorder")

    monkeypatch.setattr(wrapper, "normalise", explode)

    with recording.record(path) as recorder:
        # The query still has to work, and the caller must see its rows.
        assert list(Publisher.objects.all()) == []

    assert recording.is_disabled() is True
    assert recorder.records == []
    assert not path.exists()


def test_recording_stays_off_after_a_failure(db, path, monkeypatch):
    from tests.testapp.models import Publisher

    monkeypatch.setattr(
        wrapper, "normalise", lambda sql: (_ for _ in ()).throw(RuntimeError("bug"))
    )
    with recording.record(path):
        list(Publisher.objects.all())
    monkeypatch.undo()

    with recording.record(path) as recorder:
        list(Publisher.objects.all())

    assert recorder.skipped is True
    assert store.load(path).count == 0


def test_an_unwritable_path_does_not_raise(db, tmp_path):
    from tests.testapp.models import Publisher

    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory\n")

    with recording.record(blocked / "nested" / "recording.jsonl") as recorder:
        list(Publisher.objects.all())

    assert recorder.written == 0
    assert recording.is_disabled() is True


def test_the_wrapper_is_removed_when_the_block_ends(db, path):
    before = list(connection.execute_wrappers)

    with recording.record(path):
        assert wrapper._execute_wrapper in connection.execute_wrappers

    assert connection.execute_wrappers == before


def test_a_foreign_wrapper_is_left_alone(db, path):
    def other(execute, sql, params, many, context):
        return execute(sql, params, many, context)

    with connection.execute_wrapper(other):
        with recording.record(path):
            pass
        assert connection.execute_wrappers == [other]

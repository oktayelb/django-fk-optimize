"""COUNT-based stats: the fallback for N, and the prefetch/select decision."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django_fk_optimize import recording
from django_fk_optimize.analysis.cardinality import Cardinalities, stats_for


@pytest.fixture(autouse=True)
def clean_recorder_state():
    recording.reset()
    yield
    recording.reset()


def test_high_fanout_relation_batches_well(library):
    """12 books, 3 publishers: prefetch fetches 3 rows for 12."""
    from tests.testapp.models import Book

    stats = stats_for(Book, "publisher")

    assert stats.rows == 12
    assert stats.present == 12
    assert stats.distinct == 3
    assert stats.nulls == 0
    assert stats.fanout == 4.0
    assert stats.distinct_ratio == 0.25
    assert stats.batches_well is True


def test_low_fanout_relation_does_not_batch_well(library):
    """12 books over 6 authors, 3 of them null: half the rows are distinct."""
    from tests.testapp.models import Book

    stats = stats_for(Book, "author")

    assert stats.rows == 12
    assert stats.present == 9  # index % 4 == 0 leaves three books authorless
    assert stats.nulls == 3
    assert stats.nullable is True
    assert stats.null_share == 0.25
    assert stats.distinct == 6
    assert stats.batches_well is False


def test_two_counts_and_no_more(library):
    from tests.testapp.models import Book

    with CaptureQueriesContext(connection) as captured:
        stats_for(Book, "publisher")

    assert len(captured.captured_queries) == 2
    assert "COUNT(DISTINCT" in captured.captured_queries[1]["sql"].upper()
    # No join to the target table: the FK column is already on the row.
    assert "JOIN" not in captured.captured_queries[1]["sql"].upper()


def test_min_rows_stops_before_the_second_count(library):
    from tests.testapp.models import Book

    with CaptureQueriesContext(connection) as captured:
        stats = stats_for(Book, "publisher", min_rows=1000)

    assert len(captured.captured_queries) == 1
    assert stats.counted is False
    assert stats.rows == 12
    assert stats.batches_well is False


def test_estimate_is_the_table_capped_by_the_sample_size(library):
    from tests.testapp.models import Book

    stats = stats_for(Book, "publisher")

    assert stats.estimate(500) == 12
    assert stats.estimate(5) == 5


def test_non_forward_relations_are_not_counted(library):
    from tests.testapp.models import Book, Publisher

    assert stats_for(Book, "tags") is None  # many to many
    assert stats_for(Publisher, "book_set") is None  # reverse, not a field
    assert stats_for(Book, "title") is None  # not a relation
    assert stats_for(Book, "nosuchfield") is None


def test_the_counts_are_never_recorded(library, tmp_path):
    from tests.testapp.models import Book

    path = tmp_path / "recording.jsonl"
    with recording.record(path) as recorder:
        stats_for(Book, "publisher")

    assert recorder.records == []
    assert not path.exists()


def test_cardinalities_counts_each_table_once(library):
    from tests.testapp.models import Book

    memo = Cardinalities()

    with CaptureQueriesContext(connection) as captured:
        first = memo.get(Book, "publisher")
        second = memo.get(Book, "publisher")

    assert first is second
    assert len(captured.captured_queries) == 2
    assert len(memo) == 1

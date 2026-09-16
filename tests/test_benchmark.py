"""The timing layer: what it measures, and what it refuses to measure.

Only query counts are asserted. A count is deterministic -- select_related()
turns thirteen queries into one on any machine on any day -- and a duration is
not, so every assertion here is on the count or on behaviour.
"""

import pytest
from django.core.exceptions import FieldError

from django_fk_optimize import recording
from django_fk_optimize.analysis import benchmark as bench
from django_fk_optimize.analysis.benchmark import (
    Benchmark,
    FieldOperation,
    plan_named,
)


@pytest.fixture(autouse=True)
def clean_recorder_state():
    recording.reset()
    yield
    recording.reset()


# -- the reverse one-to-one --------------------------------------------


def test_reverse_one_to_one_can_be_select_related(library):
    """Django joins the reverse side of a OneToOneField; phase 1 said it did not."""
    from tests.testapp.models import Author

    plan = plan_named(Author, "profile")
    timer = Benchmark(sample_size=12, repeat=1)

    vanilla = timer.measure(Author, [plan])
    selected = timer.measure(Author, [plan], select=[plan.name])

    # 6 authors, 3 of them with no profile at all -- and select_related caches
    # the absence too, so the misses cost nothing either.
    assert vanilla.queries == 1 + 6
    assert selected.queries == 1


def test_reverse_many_to_one_still_refuses_the_join(library):
    from tests.testapp.models import Publisher

    plan = plan_named(Publisher, "book_set")

    assert plan.can_select_related is False
    with pytest.raises(FieldError):
        list(Publisher.objects.select_related("book_set")[:1])


def test_many_to_many_still_refuses_the_join(library):
    from tests.testapp.models import Book

    assert plan_named(Book, "tags").can_select_related is False
    with pytest.raises(FieldError):
        list(Book.objects.select_related("tags")[:1])


# -- suppression -------------------------------------------------------


def test_benchmark_records_nothing_of_its_own(library, tmp_path):
    """The whole point of suppressed(): a profiler must not profile itself."""
    from tests.testapp.models import Book

    path = tmp_path / "recording.jsonl"
    plan = plan_named(Book, "publisher")
    timer = Benchmark(sample_size=12, repeat=1)

    with recording.record(path) as recorder:
        timer.compare(Book, plan)

    assert recorder.records == []
    assert recorder.written == 0
    assert not path.exists()


def test_the_recorder_still_works_around_the_benchmark(library, tmp_path):
    """The control: the same block records normally without the benchmark."""
    from tests.testapp.models import Book

    path = tmp_path / "recording.jsonl"

    with recording.record(path) as recorder:
        list(Book.objects.all()[:3])

    assert recorder.written >= 1


# -- comparison --------------------------------------------------------


def test_compare_ranks_the_offers_fastest_first(library):
    from tests.testapp.models import Book

    result = Benchmark(sample_size=12, repeat=1).compare(
        Book, plan_named(Book, "publisher")
    )
    offers = result.ranked()

    assert FieldOperation.VANILLA not in dict(offers)
    assert {op for op, _ in offers} == {
        FieldOperation.SELECT_RELATED,
        FieldOperation.PREFETCH_RELATED,
    }
    assert offers[0][1].seconds <= offers[1][1].seconds
    assert dict(offers)[FieldOperation.SELECT_RELATED].queries == 1


def test_at_narrows_the_slice_and_never_widens_it(library):
    from tests.testapp.models import Book

    timer = Benchmark(sample_size=10, repeat=1)
    plan = plan_named(Book, "publisher")

    assert timer.at(4).sample_size == 4
    assert timer.at(1000).sample_size == 10
    assert timer.at(0).sample_size == 1
    assert timer.at(4).measure(Book, [plan]).queries == 1 + 4


def test_plan_named_answers_to_either_name(library):
    from tests.testapp.models import Publisher

    assert plan_named(Publisher, "book_set") is not None
    assert plan_named(Publisher, "nosuchrelation") is None


def test_reverse_one_to_one_has_its_own_kind():
    from tests.testapp.models import Author, Publisher

    assert plan_named(Author, "profile").kind == bench.REVERSE_ONE_TO_ONE
    assert plan_named(Publisher, "book_set").kind == bench.REVERSE


def test_compare_survives_every_relation_kind(library):
    """The regression the command was born from, at the layer that measures."""
    from tests.testapp.models import Author, Book, Note, Publisher, Tag, Textbook

    timer = Benchmark(sample_size=6, repeat=1)
    for model in (Publisher, Tag, Author, Book, Textbook, Note):
        for plan in bench.plans_for(model):
            result = timer.compare(model, plan)
            assert result.winner in result.measurements
            assert FieldOperation.VANILLA in result.measurements
            if not plan.can_select_related:
                assert FieldOperation.SELECT_RELATED not in result.measurements


def test_a_whole_sweep_records_nothing(library, tmp_path):
    from tests.testapp.models import Book, Publisher

    path = tmp_path / "recording.jsonl"
    timer = Benchmark(sample_size=6, repeat=1)

    with recording.record(path) as recorder:
        for model in (Publisher, Book):
            for plan in bench.plans_for(model):
                timer.compare(model, plan)

    assert recorder.written == 0
    assert not path.exists()

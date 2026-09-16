"""The command's behaviour, asserted on query counts rather than the clock.

Every timing this command reports is a duration, and a duration is not a fact
you can assert on a shared CI box.  A query count is: select_related() turns
thirteen queries into one on any machine on any day.
"""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_fk_optimize.management.commands.fk_optimize import (
    FORWARD,
    MANY_TO_MANY,
    REVERSE,
    Command,
    Deadline,
    FieldOperation,
    plans_for,
)


def make_command(sample_size=500, repeat=1):
    command = Command()
    command.sample_size = sample_size
    command.repeat = repeat
    return command


def plan_named(model, accessor):
    for plan in plans_for(model):
        if plan.accessor == accessor:
            return plan
    raise AssertionError(f"{model.__name__} has no plan for {accessor!r}")


# -- classification ----------------------------------------------------


def test_forward_fk_gets_all_three_strategies():
    from tests.testapp.models import Book

    plan = plan_named(Book, "publisher")

    assert plan.kind == FORWARD
    assert plan.name == "publisher"
    assert plan.many is False
    assert plan.can_select_related is True


def test_reverse_fk_uses_the_accessor_not_the_query_name():
    from tests.testapp.models import Publisher

    plan = plan_named(Publisher, "book_set")

    # The related_query_name is "book"; getattr(publisher, "book") raises.
    assert plan.kind == REVERSE
    assert plan.name == "book_set"
    assert plan.many is True
    assert plan.can_select_related is False


def test_reverse_one_to_one_is_not_a_manager():
    from tests.testapp.models import Author

    plan = plan_named(Author, "profile")

    assert plan.kind == REVERSE
    assert plan.many is False
    assert plan.can_select_related is False


def test_many_to_many_is_never_select_related():
    from tests.testapp.models import Book, Tag

    forward = plan_named(Book, "tags")
    reverse = plan_named(Tag, "books")

    assert forward.kind == MANY_TO_MANY
    assert reverse.kind == MANY_TO_MANY
    assert forward.can_select_related is False
    assert reverse.can_select_related is False


def test_hidden_reverse_relation_is_skipped():
    from tests.testapp.models import Publisher

    accessors = {plan.accessor for plan in plans_for(Publisher)}

    # Author.favourite_publisher uses related_name="+", so Publisher has no
    # accessor for it at all.
    assert accessors == {"book_set"}


def test_parent_link_is_skipped():
    from tests.testapp.models import Textbook

    accessors = {plan.accessor for plan in plans_for(Textbook)}

    assert "book_ptr" not in accessors
    assert {"publisher", "author", "tags"} <= accessors


def test_generic_foreign_key_is_skipped():
    from tests.testapp.models import Note

    accessors = {plan.accessor for plan in plans_for(Note)}

    assert "content_type" in accessors
    assert "target" not in accessors


def test_self_reference_is_a_forward_relation():
    from tests.testapp.models import Author

    accessors = {plan.accessor for plan in plans_for(Author)}

    assert {"mentor", "proteges"} <= accessors
    assert plan_named(Author, "mentor").kind == FORWARD
    assert plan_named(Author, "proteges").kind == REVERSE


# -- measurement -------------------------------------------------------


def test_forward_fk_vanilla_is_one_query_per_row(library):
    from tests.testapp.models import Book

    command = make_command(sample_size=12)
    plan = plan_named(Book, "publisher")

    vanilla = command._measure(Book, [plan])
    selected = command._measure(Book, [plan], select=[plan.name])
    prefetched = command._measure(Book, [plan], prefetch=[plan.name])

    assert vanilla.queries == 1 + 12  # the N+1 this tool exists to find
    assert selected.queries == 1
    assert prefetched.queries == 2


def test_nullable_fk_costs_nothing_when_it_is_null(library):
    from tests.testapp.models import Book

    command = make_command(sample_size=12)
    plan = plan_named(Book, "author")

    vanilla = command._measure(Book, [plan])

    # 3 of the 12 books have no author, and a null FK needs no query.
    assert vanilla.queries == 1 + 9


def test_reverse_fk_is_measured_without_raising(library):
    from tests.testapp.models import Publisher

    command = make_command(sample_size=12)
    plan = plan_named(Publisher, "book_set")

    vanilla = command._measure(Publisher, [plan])
    prefetched = command._measure(Publisher, [plan], prefetch=[plan.name])

    # 3 publishers, one query each: the manager really was consumed.
    assert vanilla.queries == 1 + 3
    assert prefetched.queries == 2


def test_many_to_many_is_measured_without_raising(library):
    from tests.testapp.models import Book

    command = make_command(sample_size=12)
    plan = plan_named(Book, "tags")

    vanilla = command._measure(Book, [plan])
    prefetched = command._measure(Book, [plan], prefetch=[plan.name])

    assert vanilla.queries == 1 + 12
    assert prefetched.queries == 2


def test_reverse_one_to_one_survives_a_missing_row(library):
    from tests.testapp.models import Author

    command = make_command(sample_size=12)
    plan = plan_named(Author, "profile")

    vanilla = command._measure(Author, [plan])

    # 6 authors, only 3 with a profile -- the other 3 raise DoesNotExist and
    # still cost a query.
    assert vanilla.queries == 1 + 6


def test_sample_size_bounds_the_queryset(library):
    from tests.testapp.models import Book

    plan = plan_named(Book, "publisher")

    assert make_command(sample_size=4)._measure(Book, [plan]).queries == 1 + 4
    assert make_command(sample_size=1)._measure(Book, [plan]).queries == 1 + 1


def test_repeat_does_not_change_the_reported_query_count(library):
    from tests.testapp.models import Book

    plan = plan_named(Book, "publisher")

    once = make_command(sample_size=12, repeat=1)._measure(Book, [plan])
    thrice = make_command(sample_size=12, repeat=3)._measure(Book, [plan])

    assert once.queries == thrice.queries == 13


def test_relation_winner_is_reported_for_every_valid_strategy(library):
    from tests.testapp.models import Book, Publisher

    command = make_command(sample_size=12)

    forward = command._optimize_relation(Book, plan_named(Book, "publisher"))
    reverse = command._optimize_relation(Publisher, plan_named(Publisher, "book_set"))

    assert set(forward.measurements) == set(FieldOperation)
    assert set(reverse.measurements) == {
        FieldOperation.VANILLA,
        FieldOperation.PREFETCH_RELATED,
    }
    assert forward.measurements[FieldOperation.SELECT_RELATED].queries == 1


# -- deadline ----------------------------------------------------------


def test_deadline_without_a_limit_never_expires():
    deadline = Deadline(None)

    assert deadline.remaining is None
    assert deadline.expired() is False
    assert deadline.hit is False


def test_deadline_expires_and_stays_expired():
    deadline = Deadline(0)

    assert deadline.expired() is True
    assert deadline.hit is True


# -- end to end --------------------------------------------------------


def run(*args, **options):
    out = io.StringIO()
    call_command("fk_optimize", *args, stdout=out, **options)
    return out.getvalue()


def test_whole_app_runs_without_raising(library):
    """The regression test: this used to die on the first reverse relation."""
    output = run("testapp", "--repeat", "1", "--sample-size", "5")

    for label in (
        "testapp.Publisher",
        "testapp.Author",
        "testapp.Book",
        "testapp.Tag",
        "testapp.Textbook",
        "testapp.Note",
    ):
        assert label in output

    # Reverse and m2m relations are measured, not skipped and not crashed on.
    assert "book_set" in output
    assert "profile" in output
    assert "tags" in output
    assert REVERSE in output
    assert MANY_TO_MANY in output

    # select_related is never offered for a relation that cannot take it.
    for line in output.splitlines():
        if line.split()[1:2] and line.split()[0].isdigit():
            columns = line.split()
            if columns[2] in (REVERSE, MANY_TO_MANY):
                assert columns[3] != FieldOperation.SELECT_RELATED.value


def test_single_model_run_is_labelled(library):
    output = run("testapp.Book", "--repeat", "1", "--sample-size", "5")

    assert "testapp.Book -- foreign key optimization results" in output
    assert "testapp.Author --" not in output
    assert "q" in output  # the query-count column


def test_timeout_cuts_the_run_short_and_says_so(library):
    output = run("testapp", "--timeout", "0")

    assert "cut short" in output
    assert "partial" in output


def test_sample_size_and_repeat_must_be_positive():
    with pytest.raises(CommandError):
        run("testapp", "--sample-size", "0")
    with pytest.raises(CommandError):
        run("testapp", "--repeat", "0")


def test_unknown_selection_is_a_command_error():
    with pytest.raises(CommandError):
        run("nosuchapp")
    with pytest.raises(CommandError):
        run("testapp.NoSuchModel")

"""The command's behaviour, asserted on query counts rather than the clock.

Every timing this command reports is a duration, and a duration is not a fact
you can assert on a shared CI box.  A query count is: select_related() turns
thirteen queries into one on any machine on any day.
"""

import io
import json
import re
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_fk_optimize.management.commands.fk_optimize import (
    FORWARD,
    MANY_TO_MANY,
    REVERSE,
    REVERSE_ONE_TO_ONE,
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

    # A reverse one-to-one is the one reverse relation Django *can* join, and
    # it hands back an instance rather than a manager. Both facts separate it
    # from a reverse many-to-one, so it gets its own kind.
    assert plan.kind == REVERSE_ONE_TO_ONE
    assert plan.many is False
    assert plan.can_select_related is True


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
    # accessor for it at all. Asserted by absence rather than by an exact set,
    # so adding a model to the test app cannot make this fail for an unrelated
    # reason.
    assert "book_set" in accessors
    assert not any("author" in accessor for accessor in accessors)
    assert all(accessor for accessor in accessors), "every plan has an accessor"


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
    """The regression test: this used to die on the first reverse relation.

    --no-callsites is the mode this sweep survives in: with no source to join
    to there is no verdict to give, only what every relation costs.
    """
    output = run("testapp", "--no-callsites", "--repeat", "1", "--sample-size", "5")

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
    output = run(
        "testapp.Book", "--no-callsites", "--repeat", "1", "--sample-size", "5"
    )

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


# -- the verdict report, end to end ------------------------------------
#
# The default mode: scan the source, read the recording, join the two, price
# the alternative. Asserted on what the report says, never on how long it took.


@pytest.fixture(autouse=True)
def clean_recorder_state():
    from django_fk_optimize import recording

    recording.reset()
    yield
    recording.reset()


@pytest.fixture
def recorded(tmp_path, library):
    """A real recording of a real N+1, made by running the real call site."""
    from django_fk_optimize import recording
    from tests.testapp import views

    path = tmp_path / "recording.jsonl"
    with recording.record(path):
        views.book_list()
    assert path.exists()
    return path


def report(*args, **options):
    return run("testapp", "--repeat", "1", "--sample-size", "12", *args, **options)


def test_it_works_with_no_recording_and_says_n_was_estimated(library, tmp_path):
    output = report("--recording", str(tmp_path / "absent.jsonl"))

    assert "testapp.Book.publisher" in output
    assert "(estimated)" in output
    assert "(observed)" not in output
    assert "none at" in output
    assert "FkOptimizeMiddleware" in output


def test_a_recording_turns_the_estimate_into_an_observation(recorded):
    output = report("--recording", str(recorded))

    assert "(observed)" in output
    assert "book_list()" in output
    assert "13 records" in output  # one page query, then one per book
    assert "1 to a call site" in output
    # Which hint wins is decided by a stopwatch, so the assertion is on the
    # relation and the shape of the fix, never on the method that won.
    assert re.search(
        r'fix\s+Book\.objects\.all\(\)\.(select|prefetch)_related\("publisher"\)',
        output,
    )


def test_the_id_only_call_site_is_reported_as_fine_not_as_a_finding(library, tmp_path):
    output = report("--recording", str(tmp_path / "absent.jsonl"))

    assert "already fine" in output
    assert "publisher_id is already on the row" in output


def test_an_unused_hint_is_offered_for_removal(library, tmp_path):
    output = report("--recording", str(tmp_path / "absent.jsonl"))

    assert 'select_related("author") is never used here' in output


def test_json_parses_and_carries_the_coverage_block(recorded):
    payload = json.loads(report("--recording", str(recorded), "--json"))

    assert payload["schema_version"] == 1
    assert payload["generated_at"]
    coverage = payload["coverage"]
    for key in (
        "files",
        "sites",
        "sites_resolved",
        "sites_unresolved",
        "scan_errors",
        "records",
        "malformed",
        "recording_age_seconds",
    ):
        assert key in coverage, key
    assert coverage["records"] == 13
    assert coverage["recording_exists"] is True

    observed = [v for v in payload["verdicts"] if v["rows"]["provenance"] == "observed"]
    assert observed, payload["verdicts"]
    assert observed[0]["relation"] == "publisher"
    assert observed[0]["best"]["queries"] == 1


def test_json_to_stdout_replaces_the_text_report(recorded):
    output = report("--recording", str(recorded), "--json")

    assert output.lstrip().startswith("{")
    assert "coverage\n" not in output


def test_json_to_a_path_is_written_alongside_the_text(recorded, tmp_path):
    destination = tmp_path / "out" / "report.json"

    output = report("--recording", str(recorded), "--json", str(destination))

    assert "changes worth making" in output
    assert json.loads(destination.read_text())["schema_version"] == 1


def test_fail_on_findings_exits_non_zero(recorded):
    with pytest.raises(CommandError) as raised:
        report("--recording", str(recorded), "--fail-on-findings")

    assert "actionable finding" in str(raised.value)


def test_fail_on_findings_is_quiet_when_there_is_nothing_to_find(recorded):
    # Textbook inherits Book's relations but no call site iterates it.
    run(
        "testapp.Tag",
        "--recording",
        str(recorded),
        "--repeat",
        "1",
        "--fail-on-findings",
    )


def test_timeout_still_prints_what_it_had(recorded):
    output = report("--recording", str(recorded), "--timeout", "0")

    assert "cut short" in output
    assert "partial" in output
    assert "0 relations timed" in output


def test_no_benchmark_skips_the_timing_and_says_so(recorded):
    output = report("--recording", str(recorded), "--no-benchmark")

    assert "skipped (--no-benchmark)" in output
    assert "measured" not in output
    assert "(observed)" in output


def test_clear_recording_removes_the_file_after_reading_it(recorded):
    output = report("--recording", str(recorded), "--clear-recording")

    assert "(observed)" in output  # it was read before it was cleared
    assert not recorded.exists()


def test_min_rows_ignores_a_table_that_is_too_small(recorded):
    output = report("--recording", str(recorded), "--min-rows", "1000")

    assert "no change worth making" in output


def test_django_models_still_works_and_says_it_is_deprecated(library, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    call_command(
        "fk_optimize",
        "testapp",
        "--no-callsites",
        "--repeat",
        "1",
        "--sample-size",
        "2",
        "--django-models",
        stdout=out,
        stderr=err,
    )

    assert "deprecated" in err.getvalue()
    assert "--include-django" in err.getvalue()


def test_min_rows_cannot_be_negative():
    with pytest.raises(CommandError):
        run("testapp", "--min-rows", "-1")


def test_a_template_n_plus_one_is_reported_with_no_call_site(library, tmp_path):
    """Trap B, end to end: no AST can see `{{ book.publisher.name }}`."""
    import time

    from django_fk_optimize.recording import store

    path = tmp_path / "recording.jsonl"
    page = 'SELECT "testapp_book"."id" FROM "testapp_book"'
    lookup = (
        'SELECT "testapp_publisher"."id" FROM "testapp_publisher" '
        'WHERE "testapp_publisher"."id" = ?'
    )
    records = [
        store.Record(
            ts=time.time(),
            shape=page,
            shape_hash="page",
            duration=0.001,
            file="/elsewhere/shop/views.py",
            line=31,
            function="catalogue",
            source="python",
        )
    ] + [
        store.Record(
            ts=time.time(),
            shape=lookup,
            shape_hash="lookup",
            duration=0.002,
            file="/elsewhere/shop/views.py",
            line=31,
            function="catalogue",
            source="template",
        )
        for _ in range(40)
    ]
    store.append(path, records)

    output = report("--recording", str(path))

    assert "catalogue()" in output
    assert "testapp.Book.publisher" in output
    assert "40  (observed)" in output
    assert "template" in output
    assert "no call site" in output
    assert re.search(
        r'add \.(select|prefetch)_related\("publisher"\) to the '
        r"testapp\.Book queryset in catalogue\(\)",
        output,
    )
    assert "catalogue()" in output
    assert "1 with no call site" in output


def ambiguous_recording(path):
    """An N+1 on testapp_author with no page query to say whose it was.

    Both Book.author and Author.mentor point there, so nothing can name the
    relation -- which is a reason to print the candidates, not to say nothing.
    """
    import time

    from django_fk_optimize.recording import store

    shape = (
        'SELECT "testapp_author"."id" FROM "testapp_author" '
        'WHERE "testapp_author"."id" = ?'
    )
    store.append(
        path,
        [
            store.Record(
                ts=time.time(),
                shape=shape,
                shape_hash="lookup",
                duration=0.001,
                file="/elsewhere/shop/views.py",
                line=9,
                function="people",
                source="template",
            )
            for _ in range(25)
        ],
    )
    return path


def test_an_unnameable_runtime_finding_is_still_printed(library, tmp_path):
    output = report("--recording", str(ambiguous_recording(tmp_path / "r.jsonl")))

    assert "people()" in output
    assert "25  (observed)" in output
    assert "testapp.Book.author" in output
    assert "testapp.Author.mentor" in output
    assert "confidence: probable" in output


def test_an_unnameable_runtime_finding_respects_the_narrowing(library, tmp_path):
    output = run(
        "testapp.Tag",
        "--recording",
        str(ambiguous_recording(tmp_path / "r.jsonl")),
        "--repeat",
        "1",
    )

    assert "people()" not in output
    assert "no change worth making" in output


# -- whose code the run is about ---------------------------------------
#
# The test app stands in for an installed one: its path is moved under a
# directory named site-packages, which is what pip would really have done to
# it, and the flags are asserted against a real end-to-end run.


@pytest.fixture
def installed_testapp(monkeypatch, tmp_path):
    """Make `tests.testapp` look like a package somebody pip-installed.

    A symlink rather than a copy, so the app the registry, the scanner and the
    ORM are all looking at stays the one the rest of the suite uses -- only
    where it appears to live changes.
    """
    from django.apps.registry import apps

    config = apps.get_app_config("testapp")
    vendor = tmp_path / "site-packages"
    vendor.mkdir()
    (vendor / "testapp").symlink_to(Path(config.path), target_is_directory=True)
    monkeypatch.setattr(config, "path", str(vendor / "testapp"))
    return vendor / "testapp"


def whole_project(*args, **options):
    """A run with no selection: whatever the flags say is in scope."""
    return run("--repeat", "1", "--sample-size", "12", *args, **options)


def test_an_installed_app_is_not_reported_on_by_default(installed_testapp, library):
    output = whole_project("--recording", "absent.jsonl")

    assert "testapp.Book.publisher" not in output
    assert "No model found with a relation" in output


def test_include_third_party_brings_the_installed_app_back(installed_testapp, library):
    output = whole_project("--recording", "absent.jsonl", "--include-third-party")

    assert "testapp.Book.publisher" in output


def test_include_django_does_not_reach_an_installed_app(installed_testapp, library):
    """The two flags are separate switches, not one in two strengths."""
    output = whole_project("--recording", "absent.jsonl", "--include-django")

    assert "testapp.Book.publisher" not in output


def test_naming_an_installed_app_scans_it(installed_testapp, library):
    """An explicit selection is as explicit as the flag.

    Selecting the app and then declining to open its files would report no
    call sites for it, which reads as a clean bill of health for source the
    run never looked at.
    """
    output = run(
        "testapp", "--repeat", "1", "--sample-size", "12", "--recording", "absent.jsonl"
    )

    assert "testapp.Book.publisher" in output

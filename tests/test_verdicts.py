"""The join, and the rules that fall out of it.

Nothing here touches a database or a clock. The static half comes from the real
scanner over real source text, the runtime half from hand-built query groups,
and every assertion is on which call site a query was traced to, which relation
was named, where N came from, and what the verdict says to do.
"""

import pytest
from django.db import OperationalError, ProgrammingError

from django_fk_optimize.analysis import verdicts as V
from django_fk_optimize.analysis.cardinality import Cardinality
from django_fk_optimize.recording import store
from django_fk_optimize.recording.store import Attribution, QueryGroup
from django_fk_optimize.recording.wrapper import PYTHON, SERIALIZER, TEMPLATE
from django_fk_optimize.utils.callsites import PROBABLE, RESOLVED, scan_source
from django_fk_optimize.utils.vocabulary import Vocabulary

FILE = "/proj/testapp/views.py"

PUBLISHERS = "testapp_publisher"
AUTHORS = "testapp_author"
BOOKS = "testapp_book"


@pytest.fixture(scope="module")
def vocabulary():
    return Vocabulary.from_apps()


@pytest.fixture(scope="module")
def tables():
    return V.Tables.from_apps()


def sites_for(source, vocabulary, path=FILE):
    report = scan_source(source, path, vocabulary, package="testapp")
    assert not report.errors, report.errors
    return report.sites


def site_at(sites, function, model=None):
    for site in sites:
        if site.function == function and (model is None or site.model == model):
            return site
    raise AssertionError(f"no call site in {function}(): {[s.function for s in sites]}")


def bulk(table, *, line=1, function="", path=FILE):
    """The page query an N+1 hangs off: what says which model was the parent."""
    return QueryGroup(
        shape_hash=f"bulk-{table}",
        table=table,
        attribution=Attribution(path, line, function),
        source=PYTHON,
        count=1,
        total_seconds=0.01,
        kind=store.BULK,
        shape=f'SELECT "{table}"."id" FROM "{table}"',
    )


def lookup(
    table, *, line, count=12, function="", source=PYTHON, path=FILE, seconds=0.2
):
    """A recorded N+1: the same single-row lookup, `count` times, from one line."""
    return QueryGroup(
        shape_hash=f"hash-{table}",
        table=table,
        attribution=Attribution(path, line, function),
        source=source,
        count=count,
        total_seconds=seconds,
        kind=store.SINGLE_ROW,
        shape=f'SELECT "{table}"."id" FROM "{table}" WHERE "{table}"."id" = ?',
    )


def line_of(source, needle):
    for number, text in enumerate(source.splitlines(), start=1):
        if needle in text:
            return number
    raise AssertionError(f"{needle!r} not in source")


def counts(rows=12, present=12, distinct=3):
    def cardinality(label, relation):
        return Cardinality(
            model=label,
            relation=relation,
            rows=rows,
            present=present,
            distinct=distinct,
            counted=True,
        )

    return cardinality


# ----------------------------------------------------------------------
# the join
# ----------------------------------------------------------------------

TWO_UNHINTED = """
from tests.testapp.models import Book


def listing():
    for book in Book.objects.all():
        send(book.publisher.name, book.author.name)
"""

SIMPLE = """
from tests.testapp.models import Book


def dashboard():
    books = Book.objects.filter(active=True)
    for book in books:
        send(book.publisher.name)
"""


def test_a_runtime_line_joins_to_a_site_declared_on_another_line(vocabulary, tables):
    """The whole point of matching on scope: the two lines are never the same."""
    sites = sites_for(SIMPLE, vocabulary)
    site = site_at(sites, "dashboard")
    touch = line_of(SIMPLE, "book.publisher.name")

    assert site.line != touch
    result = V.join([lookup(PUBLISHERS, line=touch)], sites, vocabulary, tables)

    assert len(result.matched) == 1
    match = result.matched[0]
    assert match.site is site
    assert match.relation == "publisher"
    assert match.by_scope and match.by_table


def test_table_confirmation_is_what_makes_a_match_resolved(vocabulary, tables):
    sites = sites_for(SIMPLE, vocabulary)
    touch = line_of(SIMPLE, "book.publisher.name")

    confirmed = V.join([lookup(PUBLISHERS, line=touch)], sites, vocabulary, tables)
    assert confirmed.matched[0].confidence == RESOLVED


def test_a_table_that_does_not_match_stays_probable(vocabulary, tables):
    """The site touches Book.publisher; the query read the author table."""
    sites = sites_for(SIMPLE, vocabulary)
    touch = line_of(SIMPLE, "book.publisher.name")

    result = V.join([lookup(AUTHORS, line=touch)], sites, vocabulary, tables)
    match = result.matched[0]

    assert match.by_scope is True
    assert match.by_table is False
    assert match.confidence == PROBABLE
    # One unhinted relation at the site, so the name still follows -- as a
    # deduction, which is why the confidence did not go up.
    assert match.relation == "publisher"


NESTED = """
from tests.testapp.models import Book


def outer():
    everything = Book.objects.all()
    for book in everything:
        send(book.author.name)

    def inner():
        chosen = Book.objects.filter(rare=True)
        for book in chosen:
            send(book.publisher.name)

    return inner
"""


def test_the_narrowest_containing_scope_wins(vocabulary, tables):
    sites = sites_for(NESTED, vocabulary)
    outer = site_at(sites, "outer")
    inner = site_at(sites, "inner")
    touch = line_of(NESTED, "book.publisher.name")

    # Both scopes contain the line: inner() is written inside outer().
    assert outer.contains_line(touch) and inner.contains_line(touch)

    result = V.join([lookup(PUBLISHERS, line=touch)], sites, vocabulary, tables)

    assert result.matched[0].site is inner
    assert result.matched[0].relation == "publisher"


def test_a_line_in_no_scope_falls_back_to_the_table(vocabulary, tables):
    sites = sites_for(SIMPLE, vocabulary)

    result = V.join([lookup(PUBLISHERS, line=9999)], sites, vocabulary, tables)
    match = result.matched[0]

    assert match.by_scope is False
    assert match.by_table is True
    assert match.confidence == PROBABLE


def test_a_bulk_group_is_not_a_finding(vocabulary, tables):
    sites = sites_for(SIMPLE, vocabulary)
    bulk = QueryGroup(
        shape_hash="h",
        table=PUBLISHERS,
        attribution=Attribution(FILE, 7, "dashboard"),
        source=PYTHON,
        count=40,
        total_seconds=0.1,
        kind=store.BULK,
        shape=f'SELECT * FROM "{PUBLISHERS}"',
    )

    result = V.join([bulk], sites, vocabulary, tables)

    assert result.matched == [] and result.runtime_only == []


# ----------------------------------------------------------------------
# runtime-only findings
# ----------------------------------------------------------------------


def test_a_template_finding_is_reported_even_with_no_call_site(vocabulary, tables):
    """{{ book.publisher.name }} happens inside django/template/; no AST sees it."""
    group = lookup(
        PUBLISHERS,
        line=42,
        count=412,
        function="book_list",
        source=TEMPLATE,
        path="/proj/testapp/other.py",
    )

    # The view's own page query: without it there is nothing to say which of
    # the models pointing at testapp_publisher was the one being iterated.
    page = bulk(BOOKS, path="/proj/testapp/other.py", function="book_list")

    result = V.join([group, page], [], vocabulary, tables)

    assert result.matched == []
    assert len(result.runtime_only) == 1
    verdicts, _ = V.build([], [group, page], vocabulary, tables)
    (verdict,) = verdicts

    assert verdict.runtime_only is True
    assert verdict.source == TEMPLATE
    assert verdict.rows == V.Rows(412, V.OBSERVED)
    assert verdict.function == "book_list"
    assert verdict.file == "/proj/testapp/other.py"
    assert "template" in verdict.headline
    assert verdict.relation == "publisher"
    assert verdict.model == "testapp.Book"
    assert 'select_related("publisher")' in verdict.fix
    assert "book_list()" in verdict.fix


def test_a_proxy_model_does_not_make_a_nameable_finding_ambiguous(vocabulary, tables):
    """A proxy repeats its concrete model's relations against the same table.

    Counted naively that turns one candidate into two, and a finding that
    could be named exactly gets reported as a guess instead.
    """
    labels = {
        info.label
        for info in vocabulary.models.values()
        if "publisher" in info.relations
    }
    assert {"testapp.Book", "testapp.PopularBook"} <= labels, (
        "the proxy has to be in the vocabulary for this to prove anything"
    )

    group = lookup(PUBLISHERS, line=42, count=10, path="/proj/testapp/v.py")
    page = bulk(BOOKS, path="/proj/testapp/v.py")

    (match,) = V.join([group, page], [], vocabulary, tables).runtime_only

    assert match.named is True
    assert (match.model, match.relation) == ("testapp.Book", "publisher")


def test_a_reverse_relation_is_never_offered_for_a_single_row_lookup(
    vocabulary, tables
):
    """A repeated `WHERE id = ?` on the target table is a forward deref.

    Reverse and m2m access queries the *child* table with the parent's id --
    a different shape -- so including them would only dilute the candidates.
    """
    group = lookup(PUBLISHERS, line=42, count=10, path="/proj/testapp/v.py")
    page = bulk(BOOKS, path="/proj/testapp/v.py")

    (match,) = V.join([group, page], [], vocabulary, tables).runtime_only

    assert all(name != "book_set" for _label, name in match.candidates)


def test_a_serializer_finding_says_so(vocabulary, tables):
    group = lookup(PUBLISHERS, line=10, count=30, source=SERIALIZER)

    verdicts, _ = V.build([], [group, bulk(BOOKS)], vocabulary, tables)

    assert verdicts[0].source == SERIALIZER
    assert "serializer" in verdicts[0].headline


def test_several_candidate_relations_drop_the_confidence(vocabulary, tables):
    """testapp_author is pointed at by Book.author and by Author.mentor."""
    group = lookup(AUTHORS, line=10, count=30)

    verdicts, _ = V.build([], [group], vocabulary, tables)
    (verdict,) = verdicts

    assert verdict.confidence == PROBABLE
    assert verdict.relation == ""
    assert verdict.actionable is False
    assert set(verdict.candidates) >= {"testapp.Book.author", "testapp.Author.mentor"}


def test_an_unknown_table_is_counted_not_guessed(vocabulary, tables):
    result = V.join(
        [lookup("some_other_database_table", line=10)], [], vocabulary, tables
    )

    assert result.runtime_only == []
    assert len(result.unattributed) == 1


# ----------------------------------------------------------------------
# where N comes from
# ----------------------------------------------------------------------

BOUNDED = """
from tests.testapp.models import Book


def page():
    books = Book.objects.all()[:7]
    for book in books:
        send(book.publisher.name)
"""


def test_observed_beats_a_static_bound(vocabulary, tables):
    sites = sites_for(BOUNDED, vocabulary)
    touch = line_of(BOUNDED, "book.publisher.name")

    verdicts, _ = V.build(
        sites,
        [lookup(PUBLISHERS, line=touch, count=412)],
        vocabulary,
        tables,
        cardinality=counts(),
    )
    (verdict,) = verdicts

    assert verdict.rows == V.Rows(412, V.OBSERVED)
    assert verdict.observed_queries == 412


def test_a_static_bound_beats_an_estimate(vocabulary, tables):
    sites = sites_for(BOUNDED, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts(rows=900))
    (verdict,) = verdicts

    assert verdict.rows == V.Rows(7, V.STATIC_BOUND)
    assert str(verdict.rows) == "7 (static bound)"


def test_an_estimate_is_the_last_resort_and_is_labelled(vocabulary, tables):
    sites = sites_for(SIMPLE, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts(rows=900))
    (verdict,) = verdicts

    assert verdict.rows.provenance == V.ESTIMATED
    assert verdict.rows.n == 500  # capped by the default sample size


def test_an_estimate_is_capped_by_the_sample_size(vocabulary, tables):
    sites = sites_for(SIMPLE, vocabulary)

    verdicts, _ = V.build(
        sites, [], vocabulary, tables, cardinality=counts(rows=900), sample_size=50
    )

    assert verdicts[0].rows == V.Rows(50, V.ESTIMATED)


def test_with_nothing_to_go_on_n_is_unknown(vocabulary, tables):
    sites = sites_for(SIMPLE, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=None)

    assert verdicts[0].rows.provenance == V.UNKNOWN
    assert verdicts[0].rows.known is False


# ----------------------------------------------------------------------
# the verdict table
# ----------------------------------------------------------------------


def only(verdicts, kind):
    found = [verdict for verdict in verdicts if verdict.kind == kind]
    assert found, f"no {kind} in {[v.kind for v in verdicts]}"
    return found[0]


def test_touched_unhinted_with_many_rows_is_an_n_plus_one(vocabulary, tables):
    sites = sites_for(SIMPLE, vocabulary)
    touch = line_of(SIMPLE, "book.publisher.name")

    verdicts, _ = V.build(
        sites, [lookup(PUBLISHERS, line=touch, count=412)], vocabulary, tables
    )
    verdict = only(verdicts, V.N_PLUS_ONE)

    assert verdict.actionable is True
    assert verdict.confidence == RESOLVED
    assert verdict.fix == 'Book.objects.filter(active=True).select_related("publisher")'


SINGLE = """
from tests.testapp.models import Book


def detail():
    book = Book.objects.get(pk=1)
    send(book.publisher.name)
"""


def test_touched_unhinted_with_one_row_still_saves_a_query(vocabulary, tables):
    sites = sites_for(SINGLE, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables)
    verdict = only(verdicts, V.EXTRA_QUERY)

    assert verdict.rows == V.Rows(1, V.STATIC_BOUND)
    assert verdict.actionable is True
    # The hint goes before get(), not after it: get() returns an instance.
    assert verdict.fix == 'Book.objects.select_related("publisher").get(pk=1)'


UNUSED = """
from tests.testapp.models import Book


def titles():
    books = Book.objects.select_related("publisher", "author")
    for book in books:
        send(book.author.name)
"""


def test_a_hint_for_a_relation_never_touched_is_removed(vocabulary, tables):
    sites = sites_for(UNUSED, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables)
    verdict = only(verdicts, V.REMOVE_HINT)

    assert verdict.relation == "publisher"
    assert verdict.actionable is True
    assert verdict.fix == "Book.objects.select_related('author')"
    assert only(verdicts, V.ALREADY_HINTED).relation == "author"


M2M_HINT = """
from tests.testapp.models import Book


def tagged():
    books = Book.objects.prefetch_related("tags")
    for book in books:
        send(book.tags.all())
"""


def test_a_correct_m2m_prefetch_is_recognised_not_flagged(vocabulary, tables):
    """This used to report nothing at all, because touches were forward-only.

    A many-to-many hint therefore always looked unused, and the guard that
    stopped it being reported also stopped it being understood.
    """
    sites = sites_for(M2M_HINT, vocabulary)
    site = site_at(sites, "tagged")
    assert "tags" in site.touched
    assert site.unused == (), "a hint that is used must never be called unused"

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())

    assert [verdict.kind for verdict in verdicts] == [V.KEEP_PREFETCH]
    assert all(not verdict.actionable for verdict in verdicts)


M2M_UNHINTED = """
from tests.testapp.models import Book


def tagged():
    for book in Book.objects.all():
        send(book.tags.all())
"""


def test_an_unhinted_m2m_is_offered_prefetch_never_a_join(vocabulary, tables):
    sites = sites_for(M2M_UNHINTED, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    finding = only(verdicts, V.N_PLUS_ONE)

    assert finding.actionable is True
    assert 'prefetch_related("tags")' in finding.fix
    assert "select_related" not in finding.fix, (
        "select_related() raises FieldError on a many-to-many"
    )


REVERSE_UNHINTED = """
from tests.testapp.models import Publisher


def listing():
    for publisher in Publisher.objects.all():
        send(list(publisher.book_set.all()))
"""


def test_an_unhinted_reverse_fk_is_seen_and_offered_prefetch(vocabulary, tables):
    """The relations where prefetching matters most used to be invisible."""
    sites = sites_for(REVERSE_UNHINTED, vocabulary)
    assert "book_set" in site_at(sites, "listing").touched

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    finding = only(verdicts, V.N_PLUS_ONE)

    assert 'prefetch_related("book_set")' in finding.fix
    assert "select_related" not in finding.fix


REVERSE_ONE_TO_ONE = """
from tests.testapp.models import Author


def bios():
    for author in Author.objects.all():
        send(author.profile.bio)
"""


def test_a_reverse_one_to_one_is_offered_a_join(vocabulary, tables):
    """The one reverse relation Django can carry in the parent row."""
    sites = sites_for(REVERSE_ONE_TO_ONE, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    finding = only(verdicts, V.N_PLUS_ONE)

    assert 'select_related("profile")' in finding.fix


M2M_BARE = """
from tests.testapp.models import Book


def tagged():
    for book in Book.objects.all():
        register(book.tags)
"""


def test_reading_a_manager_without_consuming_it_is_free(vocabulary, tables):
    """Measured: bare access is 1 query, and prefetching it makes that 2."""
    sites = sites_for(M2M_BARE, vocabulary)
    site = site_at(sites, "tagged")
    assert site.free == ("tags",)
    assert site.touched == ()

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    verdict = only(verdicts, V.FREE_MANAGER)

    assert verdict.actionable is False
    assert any("add a query, not remove one" in note for note in verdict.notes)


M2M_FILTERED = """
from tests.testapp.models import Book


def tagged():
    books = Book.objects.prefetch_related("tags")
    for book in books:
        send(book.tags.filter(name="x"))
"""


def test_a_manager_consumed_by_filter_is_not_served_by_prefetch(vocabulary, tables):
    """Measured: .filter() re-queries per row *and* pays for the prefetch.

    Reporting this as a satisfied hint would have been wrong, and reporting
    it as an unused hint would have been wrong in the other direction.
    """
    sites = sites_for(M2M_FILTERED, vocabulary)
    site = site_at(sites, "tagged")
    assert site.bypassed == ("tags",)
    assert site.touched == ()
    assert site.unused == (), "it is used -- just not in a way the hint serves"

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    verdict = only(verdicts, V.PREFETCH_BYPASSED)

    assert any("re-query per row" in note for note in verdict.notes)
    assert any("paid for here and not used" in note for note in verdict.notes)


PREFETCHED = """
from tests.testapp.models import Book


def listing():
    books = Book.objects.prefetch_related("publisher")
    for book in books:
        send(book.publisher.name)
"""


def test_prefetch_on_a_forward_fk_with_few_distinct_targets_is_kept(vocabulary, tables):
    sites = sites_for(PREFETCHED, vocabulary)

    verdicts, _ = V.build(
        sites,
        [],
        vocabulary,
        tables,
        cardinality=counts(rows=1200, present=1200, distinct=3),
    )
    verdict = only(verdicts, V.KEEP_PREFETCH)

    assert verdict.actionable is False
    assert "right hint" in verdict.headline


def test_prefetch_on_a_forward_fk_with_many_distinct_targets_becomes_a_join(
    vocabulary, tables
):
    sites = sites_for(PREFETCHED, vocabulary)

    verdicts, _ = V.build(
        sites,
        [],
        vocabulary,
        tables,
        cardinality=counts(rows=1200, present=1200, distinct=1100),
    )
    verdict = only(verdicts, V.SWITCH_TO_SELECT)

    assert verdict.actionable is True
    assert verdict.fix == 'Book.objects.select_related("publisher")'


ID_ONLY = """
from tests.testapp.models import Book


def ids():
    books = Book.objects.all()
    for book in books:
        send(book.publisher_id)
"""


def test_touching_only_the_id_column_is_already_free(vocabulary, tables):
    sites = sites_for(ID_ONLY, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    (verdict,) = verdicts

    assert verdict.kind == V.ID_ONLY
    assert verdict.relation == "publisher"
    assert verdict.actionable is False
    assert verdict.fix == ""
    assert "already on the row" in verdict.headline


ESCAPES = """
from tests.testapp.models import Book


def handoff():
    books = Book.objects.all()
    for book in books:
        render(book)
        send(book.publisher.name)
"""


def test_a_row_that_escapes_is_reported_but_never_asserted(vocabulary, tables):
    sites = sites_for(ESCAPES, vocabulary)
    assert site_at(sites, "handoff").escapes is True

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    verdict = only(verdicts, V.N_PLUS_ONE)

    assert verdict.confidence == PROBABLE
    assert verdict.actionable is False
    assert any("not asserted" in note for note in verdict.notes)


OPAQUE = """
from tests.testapp.models import Book


def dynamic(names):
    books = Book.objects.select_related(*names)
    for book in books:
        send(book.publisher.name)
"""


def test_an_opaque_hint_is_reported_but_never_asserted(vocabulary, tables):
    sites = sites_for(OPAQUE, vocabulary)
    assert site_at(sites, "dynamic").hints.opaque is True

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    verdict = only(verdicts, V.N_PLUS_ONE)

    assert verdict.confidence == PROBABLE
    assert verdict.actionable is False


def test_findings_come_before_everything_else(vocabulary, tables):
    source = SIMPLE + ID_ONLY.split("from tests.testapp.models import Book")[1]
    sites = sites_for(source, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())

    assert [verdict.actionable for verdict in verdicts] == sorted(
        [verdict.actionable for verdict in verdicts], reverse=True
    )


# ----------------------------------------------------------------------
# costing survives a database that cannot answer
# ----------------------------------------------------------------------


class _UnreadableTable:
    """A benchmark whose table is not there, the way an unmigrated model is."""

    def __init__(self, message='relation "testapp_book" does not exist'):
        self.message = message
        self.attempts = 0

    def at(self, _rows):
        return self

    def compare(self, _model, _plan):
        self.attempts += 1
        raise ProgrammingError(self.message)


def test_a_table_the_database_cannot_read_is_skipped_not_raised(vocabulary, tables):
    """One unmigrated model must not take the whole run down.

    A development database is half-migrated more often than not. Before this,
    the first missing table aborted the command and every other verdict in the
    run was lost with it.
    """
    from django_fk_optimize.analysis.benchmark import plans_for
    from tests.testapp.models import Book

    sites = sites_for(SIMPLE, vocabulary)
    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    finding = only(verdicts, V.N_PLUS_ONE)
    assert plans_for(Book), "the relation has to be real for the skip to mean anything"

    benchmark = _UnreadableTable()
    costed = V.cost(verdicts, benchmark, lambda _label: Book)

    assert benchmark.attempts == 1, "it should have tried before giving up"
    assert costed.timed == 0
    assert costed.skipped == 1
    assert finding.best is None, "nothing may be presented as measured"
    assert any("would not read this table" in note for note in finding.notes)
    assert any("does not exist" in note for note in finding.notes)


def test_the_skipped_relation_does_not_stop_the_next_one(vocabulary, tables):
    """The verdict after the failure still gets priced."""
    from django_fk_optimize.analysis.benchmark import (
        FieldOperation,
        Measurement,
        RelationResult,
        plan_named,
    )
    from tests.testapp.models import Book

    priced = RelationResult(
        plan=plan_named(Book, "publisher"),
        winner=FieldOperation.SELECT_RELATED,
        measurements={
            FieldOperation.VANILLA: Measurement(0.010, 13),
            FieldOperation.SELECT_RELATED: Measurement(0.001, 1),
        },
    )

    class _FailsOnce(_UnreadableTable):
        def compare(self, model, plan):
            self.attempts += 1
            if self.attempts == 1:
                raise OperationalError("permission denied for table testapp_book")
            return priced

    sites = sites_for(TWO_UNHINTED, vocabulary)
    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    costed = V.cost(verdicts, _FailsOnce(), lambda _label: Book)

    assert costed.skipped == 1, "the unreadable one is skipped"
    assert costed.timed == 1, "and the next one is still priced"
    assert any(v.best is not None for v in verdicts), "the survivor kept its numbers"


# ----------------------------------------------------------------------
# several changes on one line
# ----------------------------------------------------------------------

TWO_UNUSED_HINTS = """
from tests.testapp.models import Book


def listing():
    books = Book.objects.select_related('publisher', 'author')
    for book in books:
        send(book.title)
"""


def test_two_changes_on_one_line_do_not_contradict(vocabulary, tables):
    """The unitel-star bug: each fix undid the other.

    One verdict said to keep select_related('author'), the next said to keep
    select_related('publisher'). Applied together they contradict; applied
    one at a time they silently drop the other's finding.
    """
    sites = sites_for(TWO_UNUSED_HINTS, vocabulary)
    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    removals = [v for v in verdicts if v.kind == V.REMOVE_HINT]
    assert len(removals) == 2, [v.kind for v in verdicts]
    assert removals[0].fix != removals[1].fix, "before reconciling they disagree"

    assert V.reconcile(verdicts) == 1

    assert removals[0].fix == removals[1].fix, "one line, one finished form"
    assert "publisher" not in removals[0].fix
    assert "author" not in removals[0].fix
    for verdict in removals:
        assert any("2 changes on this line" in note for note in verdict.notes)


def test_two_missing_hints_on_one_line_are_added_together(vocabulary, tables):
    """Both relations end up in the fix, not one each."""
    sites = sites_for(TWO_UNHINTED, vocabulary)
    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    findings = [v for v in verdicts if v.actionable]
    assert len(findings) == 2

    V.reconcile(verdicts)

    assert findings[0].fix == findings[1].fix
    assert 'select_related("publisher")' in findings[0].fix
    assert 'select_related("author")' in findings[0].fix


def test_a_line_with_one_change_is_left_exactly_as_it_was(vocabulary, tables):
    sites = sites_for(SIMPLE, vocabulary)
    verdicts, _ = V.build(sites, [], vocabulary, tables, cardinality=counts())
    finding = only(verdicts, V.N_PLUS_ONE)
    before = finding.fix

    assert V.reconcile(verdicts) == 0
    assert finding.fix == before
    assert not any("changes on this line" in note for note in finding.notes)


# ----------------------------------------------------------------------
# relations that are paths
# ----------------------------------------------------------------------

TWO_HOP = """
from tests.testapp.models import Book


def chain():
    for book in Book.objects.all():
        send(book.author.favourite_publisher.name)
"""


def test_a_two_hop_chain_is_one_finding_at_the_deeper_path(vocabulary, tables):
    """The bug this section exists for: the second hop used to vanish.

    `touched` carries both prefixes, `missing` collapses to the deepest, and a
    loop over `touched` that looked each entry up as a relation of Book found
    `author`, missed `author__favourite_publisher`, and -- because the prefix
    is not in `missing` -- reported the one relation it did find as already
    hinted. Two hops became one reassurance and no finding at all.
    """
    sites = sites_for(TWO_HOP, vocabulary)
    site = site_at(sites, "chain")
    assert site.touched == ("author", "author__favourite_publisher")
    assert site.missing == ("author__favourite_publisher",)

    verdicts, _ = V.build(sites, [], vocabulary, tables)

    assert [verdict.kind for verdict in verdicts] == [V.EXTRA_QUERY]
    assert verdicts[0].relation == "author__favourite_publisher"
    assert verdicts[0].actionable is True
    assert V.ALREADY_HINTED not in [verdict.kind for verdict in verdicts], (
        "nobody hinted anything here"
    )


def test_the_fix_for_a_two_hop_chain_is_one_hint_over_both(vocabulary, tables):
    touch = line_of(TWO_HOP, "favourite_publisher.name")
    sites = sites_for(TWO_HOP, vocabulary)

    verdicts, _ = V.build(
        sites, [lookup(PUBLISHERS, line=touch, count=120)], vocabulary, tables
    )
    finding = only(verdicts, V.N_PLUS_ONE)

    assert (
        finding.fix
        == 'Book.objects.all().select_related("author__favourite_publisher")'
    )
    assert finding.rows == V.Rows(120, V.OBSERVED)


def test_a_query_on_the_last_hop_confirms_the_whole_path(vocabulary, tables):
    """The recorded table is Publisher's; the path that ends there is the deep one."""
    touch = line_of(TWO_HOP, "favourite_publisher.name")
    sites = sites_for(TWO_HOP, vocabulary)

    result = V.join([lookup(PUBLISHERS, line=touch)], sites, vocabulary, tables)

    assert result.matched[0].relation == "author__favourite_publisher"
    assert result.matched[0].by_table is True
    assert result.matched[0].confidence == RESOLVED


def test_a_query_on_an_intermediate_hop_joins_the_finding_that_covers_it(
    vocabulary, tables
):
    """Both hops are real N+1s and one hint settles both, so they are one finding.

    The author lookup is as real as the publisher lookup -- the loop issues
    both, once per row. Attributing it to the bare `author` prefix would file
    it against a verdict nobody emits, and the observed numbers would be lost
    along with it.
    """
    touch = line_of(TWO_HOP, "favourite_publisher.name")
    sites = sites_for(TWO_HOP, vocabulary)

    result = V.join([lookup(AUTHORS, line=touch, count=77)], sites, vocabulary, tables)
    assert result.matched[0].relation == "author__favourite_publisher"

    verdicts, _ = V.build(
        sites, [lookup(AUTHORS, line=touch, count=77)], vocabulary, tables
    )
    finding = only(verdicts, V.N_PLUS_ONE)
    assert finding.observed_queries == 77


HINTED_TWO_HOP = """
from tests.testapp.models import Book


def chain():
    for book in Book.objects.select_related("author__favourite_publisher"):
        send(book.author.favourite_publisher.name)
"""


def test_a_deep_hint_covers_the_prefix_and_says_which_hint_did_it(vocabulary, tables):
    sites = sites_for(HINTED_TWO_HOP, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables)

    assert all(verdict.kind == V.ALREADY_HINTED for verdict in verdicts)
    assert not any(verdict.actionable for verdict in verdicts)
    # Both hops are accounted for, and each one names the hint really written
    # on the line rather than the path it was asked about.
    assert {verdict.relation for verdict in verdicts} == {
        "author",
        "author__favourite_publisher",
    }
    for verdict in verdicts:
        assert 'select_related("author__favourite_publisher")' in verdict.headline


UNUSED_DEEP = """
from tests.testapp.models import Book


def titles():
    for book in Book.objects.select_related("author__mentor"):
        send(book.title)
"""


def test_an_unused_deep_hint_is_still_reported(vocabulary, tables):
    """It used to be dropped: `author__mentor` is not a relation of Book."""
    sites = sites_for(UNUSED_DEEP, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables)
    verdict = only(verdicts, V.REMOVE_HINT)

    assert verdict.relation == "author__mentor"
    assert verdict.fix == "Book.objects"


PREFETCHED_DEEP = """
from tests.testapp.models import Club


def clubs():
    for club in Club.objects.prefetch_related("books__publisher"):
        for book in club.books.all():
            send(book.publisher.name)
"""


def test_a_deep_prefetch_is_never_reported_as_a_join(vocabulary, tables):
    """`books` is a many-to-many; select_related() on it raises FieldError.

    The old code asked whether the touched name was in `hints.prefetch`,
    found `books` was not (`books__publisher` is), and fell through to the
    branch that prints "already covered by select_related(...)" -- advice
    Django refuses to take.
    """
    sites = sites_for(PREFETCHED_DEEP, vocabulary)

    verdicts, _ = V.build(sites, [], vocabulary, tables)
    verdict = only(verdicts, V.KEEP_PREFETCH)

    assert 'prefetch_related("books__publisher")' in verdict.headline
    assert "select_related" not in verdict.headline


def test_a_hint_needs_every_hop_to_be_joinable(vocabulary, tables):
    """One manager hop anywhere makes the whole path a prefetch."""
    assert V._prefetch_only(vocabulary, "testapp.Club", "books__publisher") is True
    assert V._prefetch_only(vocabulary, "testapp.Book", "author__mentor") is False
    assert V._prefetch_only(vocabulary, "testapp.Author", "profile") is False, (
        "the reverse side of a one-to-one is the one reverse relation Django joins"
    )


DEEP_ID_ONLY = """
from tests.testapp.models import Book


def ids():
    for book in Book.objects.all():
        send(book.author.mentor_id)
"""


def test_a_column_attribute_at_the_end_of_a_path_is_recognised(vocabulary, tables):
    """`mentor_id` is a column of Author, not of Book.

    Checked against Book's columns it matched nothing and produced no verdict
    at all -- no crash, just silence about a touch the scanner had understood
    perfectly.
    """
    sites = sites_for(DEEP_ID_ONLY, vocabulary)
    assert site_at(sites, "ids").id_only == ("author__mentor_id",)

    verdicts, _ = V.build(sites, [], vocabulary, tables)
    verdict = only(verdicts, V.ID_ONLY)

    assert verdict.relation == "author__mentor"
    assert "mentor_id is already on the row" in verdict.headline
    # The author itself still has to be loaded to read a column off it.
    assert only(verdicts, V.EXTRA_QUERY).relation == "author"


CHAIN = """
from testapp.models import Book


def catalogue():
    for book in Book.objects.all():
        print(book.author.favourite_publisher.name)
"""


def test_a_chain_reports_the_rows_it_looped_over_and_the_queries_it_cost(
    vocabulary, tables
):
    """Two hops are two recorded groups, one finding, and two different numbers.

    `book.author.favourite_publisher` goes back to the database twice for
    every row, so the loop is twelve rows long and cost twenty-four queries.
    Reporting the sum as the row count would claim a loop nobody wrote;
    reporting one group's count as the cost prints "1 + 12 queries" beside
    "24 fewer queries" and contradicts itself inside one block.
    """
    sites = sites_for(CHAIN, vocabulary)
    site = site_at(sites, "catalogue")
    touch = line_of(CHAIN, "favourite_publisher.name")
    groups = [
        bulk(BOOKS, line=line_of(CHAIN, "Book.objects.all()"), function="catalogue"),
        lookup(AUTHORS, line=touch, count=12, function="catalogue"),
        lookup(PUBLISHERS, line=touch, count=12, function="catalogue"),
    ]

    verdicts, result = V.build([site], groups, vocabulary, tables)

    (finding,) = [v for v in verdicts if v.actionable]
    assert finding.relation == "author__favourite_publisher"
    assert finding.rows.n == 12  # the loop, not the traffic
    assert finding.rows.provenance == V.OBSERVED
    assert finding.observed_queries == 24  # the traffic, not the loop
    assert "24 extra queries" in finding.headline
    assert "2 per row" in finding.headline
    assert 'select_related("author__favourite_publisher")' in finding.fix

    # Neither hop is dropped: both groups matched, and both are spoken for by
    # the one finding whose single hint removes them.
    assert len(result.matched) == 2
    assert not result.unattributed

import pytest

from django_fk_optimize.utils import INSTANCE, ITERATION, PROBABLE, RESOLVED, Vocabulary

HEADER = "from tests.testapp.models import Book\n"


@pytest.fixture(scope="module")
def vocabulary():
    return Vocabulary.from_apps()


def scan(vocabulary, body, header=HEADER):
    from django_fk_optimize.utils import scan_source

    return scan_source(header + body, "views.py", vocabulary)


def only_site(vocabulary, body, header=HEADER):
    report = scan(vocabulary, body, header)
    assert len(report.sites) == 1, report.sites
    return report.sites[0]


def test_iteration_over_a_queryset(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    print(book.publisher.name)\n",
    )

    assert site.model == "testapp.Book"
    assert site.kind == ITERATION
    assert site.expression == "Book.objects.all()"
    assert site.touched == ("publisher",)
    assert site.missing == ("publisher",)
    assert site.confidence == RESOLVED
    assert site.line == 2


def test_comprehension_counts_as_an_iteration(vocabulary):
    site = only_site(
        vocabulary,
        "names = [book.publisher.name for book in Book.objects.filter(title='x')]\n",
    )

    assert site.kind == ITERATION
    assert site.touched == ("publisher",)


def test_slice_gives_a_row_bound(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all()[:50]:\n    print(book.author)\n",
    )

    assert site.bound == 50
    assert site.bound_reason == "slice"


def test_get_gives_a_single_instance(vocabulary):
    site = only_site(
        vocabulary,
        "book = Book.objects.get(pk=1)\nprint(book.publisher)\n",
    )

    assert site.kind == INSTANCE
    assert site.bound == 1
    assert site.bound_reason == "get()"
    assert site.touched == ("publisher",)


def test_hints_are_recorded_and_cover_touches(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.select_related('publisher'):\n"
        "    print(book.publisher.name)\n",
    )

    assert site.hints.select == ("publisher",)
    assert site.missing == ()
    assert site.unused == ()


def test_unused_hint_is_a_join_for_nothing(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.select_related('author'):\n"
        "    print(book.publisher.name)\n",
    )

    assert site.unused == ("author",)
    assert site.missing == ("publisher",)


def test_bare_select_related_is_opaque(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.select_related():\n    print(book.publisher)\n",
    )

    assert site.hints.opaque is True
    # An opaque hint may well cover the touch, so no unused claim is made.
    assert site.unused == ()


def test_id_only_touch_costs_no_query(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    print(book.publisher_id)\n",
    )

    assert site.touched == ()
    assert site.id_only == ("publisher_id",)
    assert site.missing == ()


def test_row_escaping_into_a_call_lowers_confidence(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    render(book)\n",
    )

    assert site.escapes is True
    assert site.confidence == PROBABLE
    assert site.unused == ()


def test_custom_manager_method_degrades_to_probable(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.published():\n    print(book.publisher)\n",
    )

    assert site.confidence == PROBABLE
    assert any("published()" in note for note in site.notes)
    assert site.touched == ("publisher",)


def test_terminal_values_produces_no_site(vocabulary):
    report = scan(
        vocabulary,
        "for row in Book.objects.values('title'):\n    print(row)\n",
    )

    assert report.sites == []
    # values() is understood, not unresolved; it simply has no hint to give.
    assert report.unresolved == []


def test_unresolvable_queryset_is_reported_not_dropped(vocabulary):
    report = scan(
        vocabulary,
        "for book in registry.objects.all():\n    print(book.publisher)\n",
    )

    assert report.sites == []
    assert len(report.unresolved) == 1
    assert "registry.objects.all()" in report.unresolved[0][2]


def test_syntax_error_is_an_error_not_a_crash(vocabulary):
    report = scan(vocabulary, "for book in :\n", header="")

    assert report.errors
    assert report.files == 1


def test_scan_files_aggregates(tmp_path, vocabulary):
    from django_fk_optimize.utils import scan_files

    first = tmp_path / "a.py"
    first.write_text(
        HEADER + "for book in Book.objects.all():\n    print(book.author)\n"
    )
    second = tmp_path / "b.py"
    second.write_text(
        HEADER + "for book in Book.objects.all():\n    print(book.publisher)\n"
    )

    report = scan_files([first, second], vocabulary)

    assert report.files == 2
    assert {site.touched[0] for site in report.sites} == {"author", "publisher"}


# -- enclosing scope ---------------------------------------------------
#
# The runtime recorder attributes a lazy load to the line that touched the
# relation, which is never the line that built the queryset.  These fields are
# what lets the two be joined.


def test_module_level_site_is_stamped_with_the_module_scope(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    print(book.publisher.name)\n",
    )

    assert site.function == "<module>"
    assert site.scope_start == 1
    assert site.scope_end == 3
    assert site.contains_line(site.line)


def test_function_level_site_is_stamped_with_the_function(vocabulary):
    site = only_site(
        vocabulary,
        "\n".join(
            [
                "def listing():",
                "    books = Book.objects.all()",
                "    for book in books:",
                "        print(book.publisher.name)",
                "",
            ]
        ),
    )

    assert site.function == "listing"
    assert site.scope_start == 2  # the `def`, after the import header
    assert site.scope_end == 5
    # The queryset line and the touch line are different lines; both are in.
    assert site.contains_line(3)
    assert site.contains_line(5)
    assert not site.contains_line(1)


def test_method_is_stamped_with_the_method_not_the_class(vocabulary):
    site = only_site(
        vocabulary,
        "\n".join(
            [
                "class View:",
                "    def get(self):",
                "        for book in Book.objects.all():",
                "            print(book.publisher.name)",
                "",
            ]
        ),
    )

    assert site.function == "get"
    assert site.scope_start == 3
    assert site.scope_end == 5


def test_nested_function_gets_the_narrowest_scope(vocabulary):
    report = scan(
        vocabulary,
        "\n".join(
            [
                "def outer():",
                "    for book in Book.objects.all():",
                "        print(book.publisher.name)",
                "",
                "    def inner():",
                "        for other in Book.objects.all():",
                "            print(other.author.name)",
                "",
            ]
        ),
    )

    by_function = {site.function: site for site in report.sites}
    assert set(by_function) == {"outer", "inner"}

    outer = by_function["outer"]
    inner = by_function["inner"]
    assert (outer.scope_start, outer.scope_end) == (2, 8)
    assert (inner.scope_start, inner.scope_end) == (6, 8)
    # Both contain the inner touch line; the narrower one is the right answer,
    # which is the join's job, not the scanner's.
    assert outer.contains_line(8) and inner.contains_line(8)
    assert outer.contains_line(4) and not inner.contains_line(4)


def test_async_function_is_a_scope_too(vocabulary):
    site = only_site(
        vocabulary,
        "\n".join(
            [
                "async def listing():",
                "    for book in Book.objects.all():",
                "        print(book.publisher.name)",
                "",
            ]
        ),
    )

    assert site.function == "listing"
    assert (site.scope_start, site.scope_end) == (2, 4)


def test_instance_site_is_stamped_too(vocabulary):
    site = only_site(
        vocabulary,
        "\n".join(
            [
                "def detail(pk):",
                "    book = Book.objects.get(pk=pk)",
                "    return book.publisher.name",
                "",
            ]
        ),
    )

    assert site.kind == INSTANCE
    assert site.function == "detail"
    assert (site.scope_start, site.scope_end) == (2, 4)


def test_scope_is_reachable_as_one_object(vocabulary):
    from django_fk_optimize.utils.callsites import Scope

    site = only_site(
        vocabulary,
        "def f():\n    for book in Book.objects.all():\n        print(book.author)\n",
    )

    assert site.scope == Scope("f", 2, 4)
    assert site.scope.contains(3)


# -- names bound by a dynamic model loader -----------------------------


def test_get_model_with_literal_arguments_resolves(vocabulary):
    """django-oscar and django-machina reach every model this way.

    Both arguments are constants, so declining to read it is declining to read
    the project: before this, oscar scanned to zero call sites from two
    hundred models.
    """
    report = scan(
        vocabulary,
        """
from oscar.core.loading import get_model

Book = get_model("testapp", "Book")


def listing():
    for book in Book.objects.all():
        send(book.publisher.name)
""",
        header="",
    )

    (site,) = report.sites
    assert site.model == "testapp.Book"
    assert site.touched == ("publisher",)


def test_get_model_takes_the_dotted_form_too(vocabulary):
    report = scan(
        vocabulary,
        """
from django.apps import apps

Book = apps.get_model("testapp.Book")


def listing():
    for book in Book.objects.all():
        send(book.publisher.name)
""",
        header="",
    )

    (site,) = report.sites
    assert site.model == "testapp.Book"


def test_get_model_matches_the_app_label_case_insensitively(vocabulary):
    report = scan(
        vocabulary,
        """
from django.apps import apps

Book = apps.get_model("TestApp", "Book")


def listing():
    for book in Book.objects.all():
        send(book.publisher.name)
""",
        header="",
    )

    (site,) = report.sites
    assert site.model == "testapp.Book"


def test_a_computed_get_model_argument_is_not_guessed(vocabulary):
    """A name built at import time is exactly what this must not invent."""
    report = scan(
        vocabulary,
        """
from django.apps import apps

Book = apps.get_model(app_label, model_name)


def listing():
    for book in Book.objects.all():
        send(book.publisher.name)
""",
        header="",
    )

    assert report.sites == []


def test_get_model_for_a_model_that_does_not_exist_resolves_nothing(vocabulary):
    report = scan(
        vocabulary,
        """
from django.apps import apps

Thing = apps.get_model("nowhere", "Thing")


def listing():
    for thing in Thing.objects.all():
        send(thing.publisher.name)
""",
        header="",
    )

    assert report.sites == []


# -- relation paths ----------------------------------------------------
#
# `book.publisher.country.name` is two queries per row, not one, and the fix
# for it is one select_related("publisher__country").  Reading only the first
# hop printed half a fix -- and once that half was applied the site reported
# itself as fine while the second N+1 went on running.


def test_a_chain_through_two_relations_is_one_path(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    print(book.author.mentor.name)\n",
    )

    # Every prefix, because a recorded N+1 may be on either hop and the
    # runtime half confirms them one at a time.
    assert site.touched == ("author", "author__mentor")
    # One finding, because one hint settles both hops.
    assert site.missing == ("author__mentor",)


def test_a_third_hop_is_followed_too(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n"
        "    print(book.author.mentor.favourite_publisher.name)\n",
    )

    assert site.touched == (
        "author",
        "author__mentor",
        "author__mentor__favourite_publisher",
    )
    assert site.missing == ("author__mentor__favourite_publisher",)


def test_half_a_fix_does_not_silence_the_other_half(vocabulary):
    """The bug this was written for: select_related("author") loads the
    author and leaves `author.mentor` a query per row."""
    site = only_site(
        vocabulary,
        "for book in Book.objects.select_related('author'):\n"
        "    print(book.author.mentor.name)\n",
    )

    assert site.missing == ("author__mentor",)
    assert site.unused == (), "the hint is used; it just does not go far enough"


def test_a_deeper_hint_covers_the_hop_it_passes_through(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.select_related('author__mentor'):\n"
        "    print(book.author.mentor.name)\n",
    )

    assert site.touched == ("author", "author__mentor")
    assert site.missing == ()
    assert site.unused == ()


def test_a_deeper_hint_than_the_site_needs_is_not_called_unused(vocabulary):
    """Over-reporting here tells someone to delete a join they need."""
    site = only_site(
        vocabulary,
        "for book in Book.objects.select_related('author__mentor'):\n"
        "    print(book.author.name)\n",
    )

    assert site.unused == ()
    assert site.missing == ()


def test_a_hint_no_path_overlaps_is_still_unused(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.select_related('publisher'):\n"
        "    print(book.author.mentor.name)\n",
    )

    assert site.unused == ("publisher",)


def test_a_manager_ends_the_path(vocabulary):
    """`book.tags.first().name` reads a row this queryset never loaded."""
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    print(book.tags.first().name)\n",
    )

    assert site.touched == ()
    assert site.bypassed == ("tags",)
    assert not any("__" in name for name in site.touched + site.bypassed)


def test_a_manager_at_the_end_of_a_path_is_classified_as_one(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n"
        "    print(list(book.author.proteges.all()))\n",
    )

    assert site.touched == ("author", "author__proteges")
    assert site.missing == ("author__proteges",)


def test_a_manager_read_but_not_consumed_is_free_at_depth_too(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    register(book.author.proteges)\n",
    )

    assert site.touched == ("author",)
    assert site.free == ("author__proteges",)


def test_a_column_attribute_deeper_in_a_path_is_still_free(vocabulary):
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    print(book.author.mentor_id)\n",
    )

    assert site.touched == ("author",), "the author itself is a query"
    assert site.id_only == ("author__mentor_id",)
    assert site.missing == ("author",)


def test_the_walk_stops_where_the_vocabulary_does():
    """A hop over the edge of the vocabulary is not a guess to be made."""
    from django_fk_optimize.utils import ModelInfo, Relation, Vocabulary

    book = ModelInfo(
        label="testapp.Book",
        name="Book",
        module="tests.testapp.models",
        relations={"publisher": Relation("publisher", "publisher_id", "other.Press")},
    )
    site = only_site(
        Vocabulary(models={"testapp.Book": book}),
        "for book in Book.objects.all():\n    print(book.publisher.country.name)\n",
    )

    assert site.touched == ("publisher",)


def test_covers_reads_a_path_in_one_direction_only():
    from django_fk_optimize.utils import Hints

    deep = Hints(select=("publisher__country",))
    assert deep.covers("publisher__country")
    assert deep.covers("publisher"), "the join passes through the publisher"

    shallow = Hints(select=("publisher",))
    assert shallow.covers("publisher")
    assert not shallow.covers("publisher__country"), (
        "select_related() stops where it was told to stop"
    )
    assert not shallow.covers("publisher_of_record"), "a prefix is not a hop"


def test_missing_keeps_only_the_deepest_uncovered_path():
    from django_fk_optimize.utils import CallSite
    from django_fk_optimize.utils.callsites import ITERATION

    site = CallSite(
        path="views.py",
        line=1,
        model="testapp.Book",
        kind=ITERATION,
        expression="Book.objects.all()",
        touched=("author", "author__mentor", "publisher"),
    )

    assert site.missing == ("author__mentor", "publisher")


# -- querysets a class hands its own methods ---------------------------
#
# A DRF viewset's `queryset`, a class-based view's `get_queryset()` and a plain
# `self.books` set in one method and read in another are between them most of
# the Django written since 2013, and all three used to scan to nothing at all:
# the queryset and the loop that consumes it are simply in different scopes.


def test_a_class_attribute_queryset_is_reachable_from_a_method(vocabulary):
    site = only_site(
        vocabulary,
        """
from tests.testapp.models import Book


class BookViewSet:
    queryset = Book.objects.all()

    def listing(self):
        for book in self.queryset:
            send(book.publisher.name)
""",
        header="",
    )

    assert site.model == "testapp.Book"
    assert site.touched == ("publisher",)
    # The chain itself resolved, and a class attribute is not overridable
    # halfway through a request.
    assert site.confidence == RESOLVED
    assert any("class attribute queryset" in note for note in site.notes)


def test_an_annotated_class_attribute_is_the_same_declaration(vocabulary):
    site = only_site(
        vocabulary,
        """
from tests.testapp.models import Book


class BookViewSet:
    queryset: object = Book.objects.select_related("publisher")

    def listing(self):
        for book in self.queryset:
            send(book.publisher.name)
""",
        header="",
    )

    assert site.hints.select == ("publisher",)
    assert site.missing == ()


def test_a_queryset_returning_method_is_followed_at_probable(vocabulary):
    site = only_site(
        vocabulary,
        """
from tests.testapp.models import Book


class BookList:
    def get_queryset(self):
        return Book.objects.all()

    def render(self):
        for book in self.get_queryset():
            send(book.publisher.name)
""",
        header="",
    )

    assert site.touched == ("publisher",)
    # get_queryset() is the most overridden method in Django, and the subclass
    # that overrides it is usually in a file this scan never reads.
    assert site.confidence == PROBABLE
    assert any("subclass can override" in note for note in site.notes)


def test_the_chain_continues_off_a_class_binding(vocabulary):
    site = only_site(
        vocabulary,
        """
from tests.testapp.models import Book


class BookList:
    def get_queryset(self):
        return Book.objects.all()

    def render(self):
        for book in self.get_queryset().select_related("publisher")[:20]:
            send(book.publisher.name)
""",
        header="",
    )

    assert site.hints.select == ("publisher",)
    assert site.missing == ()
    assert site.bound == 20


def test_an_attribute_assigned_in_one_method_is_read_in_another(vocabulary):
    site = only_site(
        vocabulary,
        """
from tests.testapp.models import Book


class Listing:
    def load(self):
        self.books = Book.objects.filter(title="x")

    def render(self):
        for book in self.books:
            send(book.publisher.name)
""",
        header="",
    )

    assert site.touched == ("publisher",)
    assert site.confidence == RESOLVED
    assert any("assigned in load()" in note for note in site.notes)


def test_a_classmethod_reaches_the_same_attribute(vocabulary):
    site = only_site(
        vocabulary,
        """
from tests.testapp.models import Book


class BookViewSet:
    queryset = Book.objects.all()

    @classmethod
    def listing(cls):
        for book in cls.queryset:
            send(book.publisher.name)
""",
        header="",
    )

    assert site.touched == ("publisher",)


def test_a_class_binding_does_not_leak_to_the_next_class(vocabulary):
    """Cross-class resolution is exactly the guess this scanner refuses."""
    report = scan(
        vocabulary,
        """
from tests.testapp.models import Book


class First:
    queryset = Book.objects.all()


class Second:
    def listing(self):
        for book in self.queryset:
            send(book.publisher.name)
""",
        header="",
    )

    assert report.sites == []


def test_a_method_that_does_not_always_return_a_queryset_is_not_followed(
    vocabulary,
):
    """One branch returning a list makes the whole method a guess."""
    report = scan(
        vocabulary,
        """
from tests.testapp.models import Book


class BookList:
    def get_queryset(self):
        if self.empty:
            return []
        return Book.objects.all()

    def render(self):
        for book in self.get_queryset():
            send(book.publisher.name)
""",
        header="",
    )

    assert report.sites == []


def test_a_method_returning_two_models_is_not_followed(vocabulary):
    report = scan(
        vocabulary,
        """
from tests.testapp.models import Author, Book


class Listing:
    def get_queryset(self):
        if self.authors:
            return Author.objects.all()
        return Book.objects.all()

    def render(self):
        for row in self.get_queryset():
            send(row.publisher.name)
""",
        header="",
    )

    assert report.sites == []


def test_branches_that_hint_differently_are_opaque_not_guessed(vocabulary):
    """Which hint this site got depends on which branch ran, so no relation
    can be called covered and none can be called an unused join."""
    site = only_site(
        vocabulary,
        """
from tests.testapp.models import Book


class BookList:
    def get_queryset(self):
        if self.full:
            return Book.objects.select_related("publisher")
        return Book.objects.all()

    def render(self):
        for book in self.get_queryset():
            send(book.publisher.name)
""",
        header="",
    )

    assert site.hints.opaque is True
    assert site.unused == ()
    assert any("hints differently" in note for note in site.notes)


def test_a_nested_functions_return_is_not_the_methods_return(vocabulary):
    report = scan(
        vocabulary,
        """
from tests.testapp.models import Book


class Listing:
    def get_queryset(self):
        def inner():
            return Book.objects.all()

        return [inner]

    def render(self):
        for book in self.get_queryset():
            send(book.publisher.name)
""",
        header="",
    )

    assert report.sites == []


def test_an_instance_attribute_is_not_followed_across_methods(vocabulary):
    """`self.book` is one row this class may never have loaded here."""
    report = scan(
        vocabulary,
        """
from tests.testapp.models import Book


class Detail:
    def load(self):
        self.book = Book.objects.get(pk=1)

    def render(self):
        send(self.book.publisher.name)
""",
        header="",
    )

    assert report.sites == []


def test_tuple_assignment_binds_element_wise(vocabulary):
    site = only_site(
        vocabulary,
        "books, count = Book.objects.all(), 1\n"
        "for book in books:\n"
        "    print(book.publisher.name)\n",
    )

    assert site.model == "testapp.Book"
    assert site.touched == ("publisher",)


def test_an_unpacked_call_binds_nothing(vocabulary):
    """Which element a name receives is a runtime fact there."""
    report = scan(
        vocabulary,
        "books, count = paginate(Book.objects.all())\n"
        "for book in books:\n"
        "    print(book.publisher.name)\n",
    )

    assert report.sites == []


# -- the census --------------------------------------------------------
#
# The coverage figure is only worth printing if its denominator is every
# queryset in the file rather than the ones the scan happened to look at.
# A shape nobody has taught the scanner yet has to make the number worse.

CENSUS = {
    "iterated": "for book in Book.objects.all():\n    print(book.publisher.name)\n",
    "assigned then iterated": (
        "books = Book.objects.all()\nfor book in books:\n    print(book.author.name)\n"
    ),
    "terminal": "Book.objects.filter(title='x').delete()\n",
    "counted": "total = Book.objects.count()\n",
    "created": "Book.objects.create(title='x')\n",
    "unfollowable": "for book in registry.objects.all():\n    print(book.publisher)\n",
    "handed to a callee": "render(Book.objects.all())\n",
    "wrapped in a call": (
        "for book in list(Book.objects.all()):\n    print(book.publisher.name)\n"
    ),
    "class attribute": (
        "class View:\n"
        "    queryset = Book.objects.all()\n"
        "\n"
        "    def listing(self):\n"
        "        for book in self.queryset:\n"
        "            print(book.publisher.name)\n"
    ),
    "method return": (
        "class View:\n"
        "    def get_queryset(self):\n"
        "        return Book.objects.all()\n"
        "\n"
        "    def listing(self):\n"
        "        for book in self.get_queryset():\n"
        "            print(book.publisher.name)\n"
    ),
    "a manager on a name we cannot read": (
        "for row in self.model.objects.all():\n    print(row.publisher)\n"
    ),
    "comprehension": "names = [b.publisher.name for b in Book.objects.all()]\n",
    "nothing at all": "print('hello')\n",
}


@pytest.mark.parametrize("name", sorted(CENSUS))
def test_every_manager_expression_lands_in_exactly_one_bucket(vocabulary, name):
    report = scan(vocabulary, CENSUS[name])

    assert report.seen == report.attributed + report.terminal + len(
        report.unresolved
    ), (
        f"{name}: {report.seen} seen, {report.attributed} sites, "
        f"{report.terminal} terminal, {report.unresolved}"
    )


def test_a_chain_is_counted_once_at_its_outermost_node(vocabulary):
    """`Book.objects`, `.filter(...)` and `.select_related(...)` are one
    expression, not the three nested ones the tree offers."""
    report = scan(
        vocabulary,
        "for book in Book.objects.filter(title='x').select_related('author')[:5]:\n"
        "    print(book.author.name)\n",
    )

    assert report.seen == 1
    assert report.attributed == 1


def test_a_queryset_that_is_understood_and_finished_is_not_backlog(vocabulary):
    report = scan(
        vocabulary,
        "Book.objects.filter(title='x').update(title='y')\n"
        "print(Book.objects.count())\n",
    )

    assert (report.seen, report.terminal) == (2, 2)
    assert report.unresolved == []


def test_one_queryset_iterated_twice_is_one_expression(vocabulary):
    report = scan(
        vocabulary,
        "books = Book.objects.all()\n"
        "for book in books:\n"
        "    print(book.publisher.name)\n"
        "for book in books:\n"
        "    print(book.author.name)\n",
    )

    assert len(report.sites) == 2
    assert (report.seen, report.attributed) == (1, 1)
    assert report.unresolved == []


def test_a_queryset_nobody_consumes_here_is_backlog_not_coverage(vocabulary):
    """Handed to a template or a callee, it may well be an N+1 nobody sees."""
    report = scan(vocabulary, "context = {'books': Book.objects.all()}\n")

    assert report.sites == []
    assert (report.seen, report.terminal) == (1, 0)
    assert len(report.unresolved) == 1


def test_a_manager_on_a_name_we_cannot_read_is_counted_too(vocabulary):
    """`self.model.objects` is a manager being used; the only thing missing is
    the one fact that would make it a call site."""
    report = scan(
        vocabulary,
        "for row in self.model.objects.all():\n    print(row.publisher)\n",
    )

    assert report.seen == 1
    assert len(report.unresolved) == 1


def test_an_attribute_that_merely_reads_like_a_manager_is_not_counted(
    vocabulary,
):
    """The first version of this matched the text of the expression, and
    ".objects" is in "self.objects_list" too."""
    report = scan(
        vocabulary,
        "for row in self.objects_list:\n    print(row.publisher)\n",
    )

    assert report.seen == 0
    assert report.unresolved == []


def test_a_queryset_inside_a_call_is_one_queryset(vocabulary):
    report = scan(
        vocabulary,
        "for book in list(Book.objects.all()):\n    print(book.publisher.name)\n",
    )

    assert report.seen == 1
    assert len(report.unresolved) == 1, "the list() is what we cannot follow"


def test_the_census_adds_up_across_files(tmp_path, vocabulary):
    from django_fk_optimize.utils import scan_files

    first = tmp_path / "a.py"
    first.write_text(HEADER + "Book.objects.all().delete()\n")
    second = tmp_path / "b.py"
    second.write_text(
        HEADER + "for book in Book.objects.all():\n    print(book.publisher.name)\n"
    )

    report = scan_files([first, second], vocabulary)

    assert (report.seen, report.attributed, report.terminal) == (2, 1, 1)
    assert report.unresolved == []


def test_assigning_a_relation_is_not_touching_it(vocabulary):
    """Setting an FK issues no query; it is how people *avoid* one.

    Misago writes `poll.category = thread.category` precisely so that nothing
    has to be loaded, and calling that an N+1 tells someone to fix a query
    that was never made.
    """
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    book.publisher = a_publisher\n",
    )

    assert site.touched == ()
    assert site.missing == ()


def test_a_write_at_the_end_of_a_path_does_not_extend_it(vocabulary):
    """`post.thread.category = x` reads the thread and writes the category."""
    site = only_site(
        vocabulary,
        "for book in Book.objects.all():\n    book.author.mentor = someone\n",
    )

    assert site.touched == ("author",)
    assert site.missing == ("author",)

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

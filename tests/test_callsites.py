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

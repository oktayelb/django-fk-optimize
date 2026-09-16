"""Reading a Vocabulary out of source, for projects that cannot be imported.

The registry-backed Vocabulary is exact and needs a configured project. This
one needs nothing but the files, which is what lets the corpus job point the
scanner at somebody else's code without standing it up first.

It is approximate on purpose, so the tests say where the edges are rather than
pretending there are none.
"""

import textwrap

import pytest

from django_fk_optimize.utils import static_vocabulary as S
from django_fk_optimize.utils.vocabulary import FORWARD, MANY_TO_MANY, REVERSE


def tree(tmp_path, **files):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    return tmp_path


SHOP = """
    from django.db import models


    class Publisher(models.Model):
        name = models.CharField(max_length=10)


    class Tag(models.Model):
        name = models.CharField(max_length=10)


    class Book(models.Model):
        publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE)
        editor = models.ForeignKey("Publisher", null=True, on_delete=models.SET_NULL,
                                   related_name="edited")
        tags = models.ManyToManyField(Tag, related_name="books")
"""


def test_it_reads_models_and_their_forward_relations(tmp_path):
    root = tree(tmp_path, **{"shop/models.py": SHOP})

    vocabulary, stats = S.from_tree(root)

    book = vocabulary.models["shop.Book"]
    assert book.relation("publisher").kind == FORWARD
    assert book.relation("publisher").target == "shop.Publisher"
    assert book.relation("publisher").attname == "publisher_id"
    assert book.relation("tags").kind == MANY_TO_MANY
    assert stats.errors == []


def test_a_string_reference_resolves_like_a_class_one(tmp_path):
    root = tree(tmp_path, **{"shop/models.py": SHOP})

    vocabulary, _stats = S.from_tree(root)

    assert vocabulary.models["shop.Book"].relation("editor").target == "shop.Publisher"
    assert vocabulary.models["shop.Book"].relation("editor").null is True


def test_reverse_accessors_are_synthesised(tmp_path):
    root = tree(tmp_path, **{"shop/models.py": SHOP})

    vocabulary, _stats = S.from_tree(root)
    publisher = vocabulary.models["shop.Publisher"]

    assert publisher.relation("book_set").kind == REVERSE
    assert publisher.relation("book_set").target == "shop.Book"
    # related_name wins over the default accessor.
    assert publisher.relation("edited") is not None
    assert vocabulary.models["shop.Tag"].relation("books").kind == MANY_TO_MANY


def test_a_self_reference_points_at_its_own_model(tmp_path):
    root = tree(
        tmp_path,
        **{
            "people/models.py": """
                from django.db import models


                class Person(models.Model):
                    mentor = models.ForeignKey("self", null=True,
                                               on_delete=models.SET_NULL)
            """
        },
    )

    vocabulary, _stats = S.from_tree(root)

    assert vocabulary.models["people.Person"].relation("mentor").target == (
        "people.Person"
    )


def test_related_name_plus_creates_no_reverse_accessor(tmp_path):
    root = tree(
        tmp_path,
        **{
            "shop/models.py": """
                from django.db import models


                class Publisher(models.Model):
                    name = models.CharField(max_length=10)


                class Book(models.Model):
                    publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE,
                                                  related_name="+")
            """
        },
    )

    vocabulary, _stats = S.from_tree(root)

    assert vocabulary.models["shop.Publisher"].relations == {}


def test_models_are_found_outside_models_py(tmp_path):
    """django-oscar keeps its models in abstract_models.py and re-exports them.

    A models.py-only pass finds eight models in the whole of oscar; scanning
    every file finds two hundred.
    """
    root = tree(
        tmp_path,
        **{
            "shop/abstract_models.py": SHOP,
            "shop/models.py": "from .abstract_models import *  # noqa\n",
        },
    )

    vocabulary, _stats = S.from_tree(root)

    assert "shop.Book" in vocabulary


def test_an_ambiguous_target_is_refused_not_guessed(tmp_path):
    """Two apps defining `Comment` is normal; picking one would be a guess."""
    root = tree(
        tmp_path,
        **{
            "blog/models.py": """
                from django.db import models


                class Comment(models.Model):
                    body = models.TextField()
            """,
            "shop/models.py": """
                from django.db import models


                class Comment(models.Model):
                    body = models.TextField()


                class Review(models.Model):
                    comment = models.ForeignKey("Comment", on_delete=models.CASCADE)
            """,
        },
    )

    vocabulary, stats = S.from_tree(root)

    assert vocabulary.models["shop.Review"].relations == {}
    assert any("Review.comment" in item for item in stats.unresolved_targets)


@pytest.mark.parametrize(
    "source",
    [
        "class Broken(models.Model):\n    x = (",  # a syntax error
        "",  # empty
        "x = 1",  # no models at all
        "class NotAModel:\n    pass",
    ],
)
def test_unreadable_input_never_raises(tmp_path, source):
    """A corpus scan that dies on one file in ten thousand says nothing about
    the other 9999."""
    root = tree(tmp_path, **{"app/models.py": source})

    vocabulary, stats = S.from_tree(root)

    assert isinstance(len(vocabulary), int)
    assert stats.files == 1


def test_a_missing_directory_is_empty_not_fatal(tmp_path):
    vocabulary, stats = S.from_tree(tmp_path / "nope")

    assert len(vocabulary) == 0
    assert stats.files == 0


def test_the_scanner_runs_against_a_source_built_vocabulary(tmp_path):
    """The whole point: scan code using a vocabulary nobody imported."""
    from django_fk_optimize.utils.callsites import scan_source

    root = tree(tmp_path, **{"shop/models.py": SHOP})
    vocabulary, _stats = S.from_tree(root)

    report = scan_source(
        textwrap.dedent("""
            from shop.models import Book


            def listing():
                for book in Book.objects.all():
                    send(book.publisher.name)
        """),
        "/shop/views.py",
        vocabulary,
    )

    (site,) = report.sites
    assert site.model == "shop.Book"
    assert site.touched == ("publisher",)
    assert site.missing == ("publisher",)


# -- what a corpus run found that the hand-written cases did not -------


OSCAR_SHAPE = """
    from django.db import models


    class AbstractPublisher(models.Model):
        name = models.CharField(max_length=10)

        class Meta:
            abstract = True


    class AbstractBook(models.Model):
        publisher = models.ForeignKey("Publisher", on_delete=models.CASCADE)

        class Meta:
            abstract = True
"""

OSCAR_CONCRETE = """
    from .abstract_models import AbstractBook, AbstractPublisher


    class Publisher(AbstractPublisher):
        pass


    class Book(AbstractBook):
        pass
"""


def test_a_model_is_found_through_a_project_local_abstract_base(tmp_path):
    """`class Product(AbstractProduct)` names no base spelled like a model.

    A name-only test finds 206 abstract classes in django-oscar and not one of
    the concrete classes anybody queries, so the whole project scanned to zero
    call sites.
    """
    root = tree(
        tmp_path,
        **{"shop/abstract_models.py": OSCAR_SHAPE, "shop/models.py": OSCAR_CONCRETE},
    )

    vocabulary, _stats = S.from_tree(root)

    assert vocabulary.by_name("Book") is not None
    assert vocabulary.by_name("Publisher") is not None


def test_relations_are_inherited_from_the_abstract_base(tmp_path):
    """The concrete class declares no fields at all; the base holds them."""
    root = tree(
        tmp_path,
        **{"shop/abstract_models.py": OSCAR_SHAPE, "shop/models.py": OSCAR_CONCRETE},
    )

    vocabulary, _stats = S.from_tree(root)

    book = vocabulary.by_name("Book")
    assert book.relation("publisher") is not None
    assert book.relation("publisher").target == "shop.Publisher"


def test_an_abstract_base_does_not_originate_the_reverse_accessor(tmp_path):
    """Both AbstractBook and Book carry the key; only one names the accessor.

    Synthesising from both gives `abstractbook_set`, which no code writes.
    """
    root = tree(
        tmp_path,
        **{"shop/abstract_models.py": OSCAR_SHAPE, "shop/models.py": OSCAR_CONCRETE},
    )

    vocabulary, _stats = S.from_tree(root)
    publisher = vocabulary.by_name("Publisher")

    assert publisher.relation("book_set") is not None
    assert publisher.relation("abstractbook_set") is None

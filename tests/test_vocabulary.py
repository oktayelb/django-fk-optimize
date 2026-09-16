from django_fk_optimize.utils import ModelInfo, Relation, Vocabulary
from django_fk_optimize.utils.vocabulary import _describe


def test_describe_records_forward_many_to_one_only():
    from tests.testapp.models import Book

    info = _describe(Book)

    assert set(info.relations) == {"publisher", "author"}
    assert info.relations["author"].null is True
    assert info.relations["publisher"].null is False
    assert info.relations["publisher"].attname == "publisher_id"
    assert info.relations["publisher"].target == "testapp.Publisher"


def test_describe_excludes_m2m_and_reverse_but_lists_them():
    from tests.testapp.models import Book, Publisher

    book = _describe(Book)
    publisher = _describe(Publisher)

    # tags is many-to-many: named, but not optimizable as a forward FK.
    assert "tags" not in book.relations
    assert "tags" in book.all_relation_names

    # Publisher's relations are all reverse, so it has nothing to optimize.
    assert publisher.relations == {}
    assert "book" in publisher.all_relation_names


def test_describe_excludes_parent_link():
    from tests.testapp.models import Textbook

    info = _describe(Textbook)

    assert "book_ptr" not in info.relations
    assert "book_ptr" in info.all_relation_names
    # The parent's own forward FKs are still reachable through inheritance.
    assert {"publisher", "author"} <= set(info.relations)


def test_describe_excludes_generic_foreign_key():
    from tests.testapp.models import Note

    info = _describe(Note)

    # content_type is a real FK; target is a GenericForeignKey with no single
    # related model, so it cannot be joined to anything.
    assert "content_type" in info.relations
    assert "target" not in info.relations


def test_describe_keeps_self_reference_and_hidden_forward_side():
    from tests.testapp.models import Author

    info = _describe(Author)

    assert info.relations["mentor"].target == "testapp.Author"
    # related_name="+" hides the *reverse* accessor; the forward field is a
    # perfectly ordinary FK and still worth optimizing.
    assert "favourite_publisher" in info.relations


def test_model_info_helpers():
    info = _describe(__import__("tests.testapp.models", fromlist=["Book"]).Book)

    assert info.qualname == "tests.testapp.models.Book"
    assert info.attnames == frozenset({"publisher_id", "author_id"})
    assert info.relation("publisher") is not None
    assert info.relation("nope") is None
    assert "objects" in info.managers


def test_vocabulary_lookup_by_qualname_and_name():
    info = ModelInfo(
        label="alarms.Alarm",
        name="Alarm",
        module="alarms.models",
        relations={"type": Relation("type", "type_id", "alarms.AlarmType")},
    )
    vocabulary = Vocabulary(models={"alarms.Alarm": info})

    assert vocabulary.by_qualname("alarms.models.Alarm") is info
    assert vocabulary.by_name("Alarm") is info
    assert vocabulary.by_qualname("other.models.Alarm") is None
    assert "alarms.Alarm" in vocabulary
    assert len(vocabulary) == 1


def test_vocabulary_refuses_ambiguous_bare_names():
    first = ModelInfo(label="a.Comment", name="Comment", module="a.models")
    second = ModelInfo(label="b.Comment", name="Comment", module="b.models")
    vocabulary = Vocabulary(models={"a.Comment": first, "b.Comment": second})

    # Guessing here would attribute call sites to the wrong model.
    assert vocabulary.by_name("Comment") is None
    assert vocabulary.by_qualname("a.models.Comment") is first


def test_from_apps_covers_the_test_app():
    vocabulary = Vocabulary.from_apps()

    assert "testapp.Book" in vocabulary
    assert set(vocabulary.models["testapp.Book"].relations) == {"publisher", "author"}
    # django.* apps are excluded by default.
    assert "contenttypes.ContentType" not in vocabulary

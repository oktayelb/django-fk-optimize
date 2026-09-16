from django_fk_optimize.utils import ModelInfo, Relation, Vocabulary
from django_fk_optimize.utils import vocabulary as vocabulary_module
from django_fk_optimize.utils.vocabulary import _describe


def test_describe_records_forward_relations_with_their_column():
    from tests.testapp.models import Book

    info = _describe(Book)

    assert info.forward_relations.keys() >= {"publisher", "author"}
    assert info.relations["author"].null is True
    assert info.relations["publisher"].null is False
    assert info.relations["publisher"].attname == "publisher_id"
    assert info.relations["publisher"].target == "testapp.Publisher"
    assert info.relations["publisher"].kind == vocabulary_module.FORWARD
    assert info.relations["publisher"].joinable is True
    assert info.relations["publisher"].manager is False


def test_describe_records_many_to_many_as_a_manager():
    from tests.testapp.models import Book

    info = _describe(Book)

    assert "tags" in info.relations
    assert "tags" in info.all_relation_names
    assert info.relations["tags"].kind == vocabulary_module.MANY_TO_MANY
    assert info.relations["tags"].manager is True
    assert info.relations["tags"].joinable is False, (
        "select_related() raises FieldError on a many-to-many"
    )
    assert info.relations["tags"].attname == "", "there is no column to read"


def test_describe_keys_a_reverse_relation_by_its_accessor():
    """`book_set`, not `book`.

    The related_query_name goes in a filter; the accessor is what an instance
    answers to. Using one where the other belongs is the AttributeError this
    project started from.
    """
    from tests.testapp.models import Publisher

    info = _describe(Publisher)

    assert "book_set" in info.relations
    assert "book" not in info.relations
    assert info.relations["book_set"].kind == vocabulary_module.REVERSE
    assert info.relations["book_set"].manager is True
    assert info.relations["book_set"].target == "testapp.Book"


def test_describe_marks_a_reverse_one_to_one_as_joinable():
    """The one reverse relation Django can carry in the parent row."""
    from tests.testapp.models import Author

    info = _describe(Author)

    assert info.relations["profile"].kind == vocabulary_module.REVERSE_ONE_TO_ONE
    assert info.relations["profile"].joinable is True
    assert info.relations["profile"].manager is False, (
        "a reverse one-to-one hands back an instance, not a manager"
    )


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
    book = vocabulary.models["testapp.Book"]
    assert set(book.forward_relations) >= {"publisher", "author"}
    assert "tags" in book.relations, "manager relations are covered now too"
    # django.* apps are excluded by default.
    assert "contenttypes.ContentType" not in vocabulary

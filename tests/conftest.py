"""Bootstrap Django by hand, so the suite runs under plain pytest.

pytest-django is not a dependency of this project and should not become one
for the sake of a test run: settings.configure() plus django.setup() is four
lines, and doing it here keeps the suite runnable with nothing installed but
pytest and Django.
"""

import django
import pytest
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=False,
        USE_TZ=True,
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django_fk_optimize",
            "tests.testapp",
        ],
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    )
    django.setup()


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """One schema for the whole session, torn down at the end."""
    from django.db import connection

    old_name = connection.creation.create_test_db(verbosity=0, autoclobber=True)
    try:
        yield
    finally:
        connection.creation.destroy_test_db(old_name, verbosity=0)


@pytest.fixture
def db():
    """Roll every test's writes back, the way pytest-django's `db` would."""
    from django.db import transaction

    atomic = transaction.atomic()
    atomic.__enter__()
    try:
        yield
    finally:
        transaction.set_rollback(True)
        atomic.__exit__(None, None, None)


@pytest.fixture
def library(db):
    """A small, fixed dataset: 3 publishers, 12 books, 6 authors, 4 tags."""
    from tests.testapp.models import Author, Book, Profile, Publisher, Tag

    publishers = [Publisher.objects.create(name=f"publisher-{i}") for i in range(3)]
    tags = [Tag.objects.create(name=f"tag-{i}") for i in range(4)]
    authors = [Author.objects.create(name=f"author-{i}") for i in range(6)]
    for author in authors[1:]:
        author.mentor = authors[0]
        author.favourite_publisher = publishers[0]
        author.save()
    for author in authors[:3]:
        Profile.objects.create(author=author, bio="a bio")

    books = []
    for index in range(12):
        book = Book.objects.create(
            title=f"book-{index}",
            publisher=publishers[index % 3],
            author=authors[index % 6] if index % 4 else None,
        )
        book.tags.set(tags[: (index % 4) + 1])
        books.append(book)

    return {
        "publishers": publishers,
        "tags": tags,
        "authors": authors,
        "books": books,
    }

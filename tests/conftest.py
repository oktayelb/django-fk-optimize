"""Bootstrap Django by hand, so the suite runs under plain pytest.

pytest-django is not a dependency of this project and should not become one
for the sake of a test run: settings.configure() plus django.setup() is four
lines, and doing it here keeps the suite runnable with nothing installed but
pytest and Django.

The database is in-memory sqlite unless ``FK_OPTIMIZE_TEST_DB=postgres`` says
otherwise.  The analysis layer writes real SQL -- ``COUNT(DISTINCT ...)``, and
a table name parsed back out of a query -- and sqlite is not enough to prove
any of that portable, so CI points this at a postgres service container.  The
default stays sqlite so a local run needs no argument and no server.
"""

import os

import django
import pytest
from django.conf import settings

BACKENDS = {"sqlite", "postgres"}


def database():
    """The ``DATABASES['default']`` this run wants.

    Connection details come from the standard libpq environment variables, so
    a CI service container needs no settings file of its own.
    """
    backend = os.environ.get("FK_OPTIMIZE_TEST_DB", "sqlite").strip().lower()
    if backend not in BACKENDS:
        raise RuntimeError(
            f"FK_OPTIMIZE_TEST_DB={backend!r}; expected one of {sorted(BACKENDS)}"
        )
    if backend == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "fk_optimize"),
        "USER": os.environ.get("PGUSER", "postgres"),
        "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
        "HOST": os.environ.get("PGHOST", "127.0.0.1"),
        "PORT": os.environ.get("PGPORT", "5432"),
    }


if not settings.configured:
    settings.configure(
        DEBUG=False,
        USE_TZ=True,
        DATABASES={"default": database()},
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django_fk_optimize",
            "tests.testapp",
        ],
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        # `tests.testapp` has no migrations, and `migrate` creates the tables
        # of unmigrated apps before it runs anybody's migrations.  Its
        # GenericForeignKey therefore points at a `django_content_type` that
        # does not exist yet, which sqlite tolerates and postgres does not.
        # Unmigrating contenttypes too puts both in the same CREATE pass,
        # where the schema editor defers every foreign key to the end.
        MIGRATION_MODULES={"contenttypes": None},
    )
    django.setup()


def release(connection):
    """Detach everything but this thread from the test database.

    The async middleware tests run the ORM on executor threads, and Django
    keeps one connection per thread.  `connections.close_all()` only reaches
    the current thread's, so those outlive the run -- which sqlite does not
    care about and postgres refuses to DROP a database over.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """One schema for the whole session, torn down at the end."""
    from django.db import connection, connections

    old_name = connection.creation.create_test_db(verbosity=0, autoclobber=True)
    try:
        yield
    finally:
        release(connection)
        connections.close_all()
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

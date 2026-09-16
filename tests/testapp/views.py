"""Call sites for the scanner to find and the recorder to record.

Not a Django view module in any real sense -- no request, no response. It is
here because the static half of this tool reads source text and the runtime
half attributes queries to a file and a line, and the only honest way to test
the join between them is to have real source that really issues the queries.
"""

from .models import Book


def book_list():
    """The N+1: one query for the books, then one per book for the publisher."""
    books = Book.objects.all()
    titles = []
    for book in books:
        titles.append(f"{book.title} ({book.publisher.name})")
    return titles


def book_ids():
    """Already free: the column is on the row, so nothing is loaded."""
    ids = []
    for book in Book.objects.all():
        ids.append(book.publisher_id)
    return ids


def over_hinted():
    """A join for nothing: "author" is hinted and never touched."""
    rows = Book.objects.select_related("publisher", "author")
    return [book.publisher.name for book in rows]


def one_book(pk):
    """One row, one extra query -- still worth a join."""
    book = Book.objects.get(pk=pk)
    return book.publisher.name

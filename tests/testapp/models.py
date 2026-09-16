"""Model shapes that exercise every relation kind the command has to survive.

Deliberately not a toy: a lookup table with high fanout, a table with low
fanout, a nullable FK, a self-reference, a hidden reverse relation, a reverse
one-to-one, an m2m, multi-table inheritance and a generic foreign key.  Those
last five are the ones that made the command raise FieldError or
AttributeError before it classified relations.
"""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Publisher(models.Model):
    """Small lookup table: few rows, many books point at each one."""

    name = models.CharField(max_length=100)


class Tag(models.Model):
    name = models.CharField(max_length=100)


class Author(models.Model):
    """Large table: roughly one author per book, so fanout is low."""

    name = models.CharField(max_length=100)
    # Self-referential and nullable.
    mentor = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="proteges",
    )
    # related_name="+" means there is no reverse accessor at all on Publisher.
    favourite_publisher = models.ForeignKey(
        Publisher, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )


class Profile(models.Model):
    """The reverse side of this is a one-to-one, not a manager."""

    author = models.OneToOneField(Author, on_delete=models.CASCADE)
    bio = models.TextField(blank=True)


class Book(models.Model):
    title = models.CharField(max_length=200)
    publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE)
    author = models.ForeignKey(Author, null=True, blank=True, on_delete=models.SET_NULL)
    tags = models.ManyToManyField(Tag, related_name="books")


class Textbook(Book):
    """Multi-table inheritance, so `book_ptr` is a parent link."""

    subject = models.CharField(max_length=100)


class Note(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")
    body = models.TextField(blank=True)

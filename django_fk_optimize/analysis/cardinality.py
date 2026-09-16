"""How many rows, how many distinct targets, how many nulls -- from COUNTs.

This is the cheap, stable, production-safe half of the analysis.  It answers
two questions with two aggregates and never loads a row:

* **What is N, when nothing was recorded?**  `Model.objects.count()`, capped by
  `--sample-size`.  An estimate, and labelled as one everywhere it is used --
  a number a tool presents as a measurement when it guessed is worse than no
  number at all.
* **Is `prefetch_related()` the right hint here, or `select_related()`?**  On a
  forward FK the two differ by how many *distinct* rows the related table has
  to give back.  Twelve books pointing at three publishers is three rows
  batched into one extra query; twelve books pointing at twelve authors is a
  second query that returns the same twelve rows a join would have carried for
  free.  `distinct_ratio` is that number, and it is the only input the
  "keep prefetch" / "switch to select_related" verdicts have.

Two aggregates, not `values_list()`: `COUNT(DISTINCT col)` is work the database
is built for, while pulling a column into Python to call `len(set(...))` on it
is a table scan into a process that is supposed to be diagnosing table scans.

Every query here runs under `recording.suppressed()`.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import FieldDoesNotExist
from django.db import DatabaseError
from django.db.models import Count

from ..recording import suppressed

# Above this share of distinct targets, batching buys nothing: the second query
# returns nearly one row per parent row, which is what a join carries for free.
DISTINCT_RATIO_THRESHOLD = 0.5


@dataclass(frozen=True)
class Cardinality:
    """Row counts for one (model, forward relation) pair.

    `counted` is False when `--min-rows` stopped the second aggregate: the
    table was measured and found too small to be worth a verdict, which is a
    different thing from a table that could not be measured at all.
    """

    model: str  # "testapp.Book"
    relation: str  # "publisher"
    rows: int  # rows in the model's own table
    present: int  # rows whose FK is not null
    distinct: int  # distinct target rows those FKs point at
    nullable: bool = False
    counted: bool = True

    @property
    def nulls(self) -> int:
        return max(self.rows - self.present, 0)

    @property
    def null_share(self) -> float:
        """Share of rows whose FK is null -- rows the relation costs nothing on."""
        return self.nulls / self.rows if self.rows else 0.0

    @property
    def fanout(self) -> float:
        """Rows per distinct target. High fanout is what prefetch is for."""
        return self.present / self.distinct if self.distinct else 0.0

    @property
    def distinct_ratio(self) -> float:
        """Distinct targets per row with a value. 1.0 means no batching to do."""
        return self.distinct / self.present if self.present else 0.0

    @property
    def batches_well(self) -> bool:
        return self.counted and 0.0 < self.distinct_ratio < DISTINCT_RATIO_THRESHOLD

    def estimate(self, sample_size: int) -> int:
        """N when nothing observed it: the table, capped by the sample size."""
        return max(min(self.rows, int(sample_size)), 0)


def stats_for(model, relation: str, *, min_rows: int = 0) -> Cardinality | None:
    """Two COUNTs for one forward relation, or None if it cannot be counted.

    None covers everything the caller cannot act on -- a name that is not a
    field, a relation that is not a forward many-to-one, a table the database
    does not have -- so a half-migrated development database degrades to "N was
    not estimated" instead of taking the whole run down.
    """
    try:
        field = model._meta.get_field(relation)
    except FieldDoesNotExist:
        return None
    if not getattr(field, "many_to_one", False) and not (
        getattr(field, "one_to_one", False) and getattr(field, "concrete", False)
    ):
        return None
    if field.related_model is None:
        return None

    label = model._meta.label
    nullable = bool(getattr(field, "null", False))
    try:
        with suppressed():
            rows = model.objects.count()
            if rows < min_rows:
                # Deliberately no second aggregate: --min-rows exists to keep
                # this command off tables it has already been told to ignore.
                return Cardinality(
                    model=label,
                    relation=relation,
                    rows=rows,
                    present=0,
                    distinct=0,
                    nullable=nullable,
                    counted=False,
                )
            totals = model.objects.aggregate(
                # Resolves to the local column: COUNT("book"."publisher_id"),
                # no join to the target table.
                present=Count(relation),
                distinct=Count(relation, distinct=True),
            )
    except DatabaseError:
        return None

    return Cardinality(
        model=label,
        relation=relation,
        rows=rows,
        present=int(totals.get("present") or 0),
        distinct=int(totals.get("distinct") or 0),
        nullable=nullable,
        counted=True,
    )


class Cardinalities:
    """A memo over `stats_for`, so one table is counted once per run.

    A project-wide run asks about the same model from every call site that
    iterates it. Without this the command's own query count grows with the
    size of the codebase rather than with the number of tables.
    """

    def __init__(self, *, min_rows: int = 0):
        self.min_rows = min_rows
        self._cache: dict[tuple[str, str], Cardinality | None] = {}

    def get(self, model, relation: str) -> Cardinality | None:
        key = (model._meta.label, relation)
        if key not in self._cache:
            self._cache[key] = stats_for(model, relation, min_rows=self.min_rows)
        return self._cache[key]

    def __len__(self):
        return len(self._cache)

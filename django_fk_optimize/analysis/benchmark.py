"""Time the alternatives, on the real table, over a bounded slice.

This is the only place in the package that classifies a relation and the only
place that times one.  It used to live inside the management command, which
meant the analysis layer either had to import a command or grow a second copy
of the classification -- and two copies of "is this relation joinable?" is
exactly how the `FieldError` this project started out with comes back.

Three rules hold every measurement together:

* **The slice is identical across strategies.**  `model.objects.all()` ordered
  by pk and cut to `sample_size`.  Compare two strategies over two different
  sets of rows and the comparison measures the rows, not the strategy.
* **One warmup is discarded, then the median of `repeat` runs is kept.**  A
  first run pays for connection setup, query-plan caching and a cold page
  cache; a mean would let one such outlier decide the answer.
* **Every query runs under `recording.suppressed()`.**  This package reads a
  recording of the application's queries.  Left unsuppressed it would append
  thousands of its own benchmark queries to that file and then read them back
  as the application's work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from statistics import median

from django.core.exceptions import ObjectDoesNotExist
from django.db import DEFAULT_DB_ALIAS, connections, router
from django.db.models import Model
from django.db.models.fields.reverse_related import ForeignObjectRel
from django.test.utils import CaptureQueriesContext

from ..recording import suppressed

DEFAULT_SAMPLE_SIZE = 500
DEFAULT_REPEAT = 5


class FieldOperation(str, Enum):
    VANILLA = "vanilla"
    SELECT_RELATED = "select_related"
    PREFETCH_RELATED = "prefetch_related"


# Every strategy is valid where the related row can be joined onto the parent:
# a forward many-to-one, a forward one-to-one, and -- less obviously -- the
# reverse side of a one-to-one, which Django joins with a LEFT OUTER JOIN and
# which has been documented as select_related()-able since long before 4.2.
ALL_STRATEGIES = (
    FieldOperation.VANILLA,
    FieldOperation.SELECT_RELATED,
    FieldOperation.PREFETCH_RELATED,
)
# A reverse many-to-one and a many-to-many produce a *set* per parent row.
# There is no single row to widen the parent with, and select_related() raises
# FieldError on both, so only two strategies exist.
NO_JOIN_STRATEGIES = (FieldOperation.VANILLA, FieldOperation.PREFETCH_RELATED)

FORWARD = "forward"
REVERSE = "reverse"
REVERSE_ONE_TO_ONE = "reverse1to1"
MANY_TO_MANY = "m2m"


@dataclass(frozen=True)
class RelationPlan:
    """How one relation has to be timed.

    The two names differ and conflating them is the bug this exists to stop.
    A reverse relation's `name` is the related_query_name ("book"), which is
    what a filter takes; its accessor is "book_set", which is what an instance
    answers to.  Passing one where the other belongs raises FieldError or
    AttributeError, which is most of what made the old command crash.
    """

    name: str  # what select_related()/prefetch_related() take
    accessor: str  # what getattr() on a row has to ask for
    kind: str
    many: bool  # the accessor hands back a manager, not an instance
    strategies: tuple[FieldOperation, ...]

    @property
    def can_select_related(self) -> bool:
        return FieldOperation.SELECT_RELATED in self.strategies


def plan_for(field) -> RelationPlan | None:
    """A timing plan for one entry of `Model._meta.get_fields()`, or None.

    None means "there is nothing here to optimize": a hidden reverse relation
    (related_name="+") has no accessor at all, a parent link is joined by
    inheritance whatever we do, and a GenericForeignKey has no single related
    model to join to.
    """
    if isinstance(field, ForeignObjectRel):
        if field.hidden:
            return None
        accessor = field.get_accessor_name()
        if not accessor:
            return None
        if field.many_to_many:
            kind, strategies = MANY_TO_MANY, NO_JOIN_STRATEGIES
        elif field.one_to_one:
            # Django joins the reverse side of a OneToOneField and caches the
            # *absence* of a row too, so a parent with no child costs no extra
            # query either.  Verified against 6.0; documented since 1.x.
            kind, strategies = REVERSE_ONE_TO_ONE, ALL_STRATEGIES
        else:
            kind, strategies = REVERSE, NO_JOIN_STRATEGIES
        return RelationPlan(
            name=accessor,
            accessor=accessor,
            kind=kind,
            many=not field.one_to_one,
            strategies=strategies,
        )

    if not getattr(field, "is_relation", False):
        return None
    if field.related_model is None:  # GenericForeignKey
        return None
    if field.many_to_many:
        return RelationPlan(
            name=field.name,
            accessor=field.name,
            kind=MANY_TO_MANY,
            many=True,
            strategies=NO_JOIN_STRATEGIES,
        )
    if getattr(field.remote_field, "parent_link", False):
        return None
    if field.many_to_one or field.one_to_one:
        return RelationPlan(
            name=field.name,
            accessor=field.name,
            kind=FORWARD,
            many=False,
            strategies=ALL_STRATEGIES,
        )
    return None


def plans_for(model: type[Model]) -> list[RelationPlan]:
    plans = []
    for field in model._meta.get_fields():
        plan = plan_for(field)
        if plan is not None:
            plans.append(plan)
    return plans


def plan_named(model: type[Model], name: str) -> RelationPlan | None:
    """The plan for one relation, by the name select_related() would take."""
    for plan in plans_for(model):
        if plan.name == name or plan.accessor == name:
            return plan
    return None


@dataclass(frozen=True)
class Measurement:
    """What one strategy cost.

    `queries` is the number a reviewer actually acts on: it is deterministic,
    it does not move with machine load, and "21 queries became 1" is a claim
    that survives being read on a different machine. `seconds` is the median
    of several runs and is still only an indication.
    """

    seconds: float
    queries: int


@dataclass
class RelationResult:
    plan: RelationPlan
    winner: FieldOperation
    measurements: dict[FieldOperation, Measurement] = dataclass_field(
        default_factory=dict
    )

    def get(self, operation: FieldOperation) -> Measurement | None:
        return self.measurements.get(operation)

    @property
    def best(self) -> Measurement | None:
        return self.measurements.get(self.winner)

    def ranked(self) -> list[tuple[FieldOperation, Measurement]]:
        """Strategies fastest first, vanilla excluded -- the fixes on offer."""
        offers = [
            (operation, measurement)
            for operation, measurement in self.measurements.items()
            if operation is not FieldOperation.VANILLA
        ]
        offers.sort(key=lambda pair: pair[1].seconds)
        return offers


class Deadline:
    """A real wall-clock budget for the whole run.

    Checked between units of work rather than enforced with a signal: a
    half-finished timing is worthless, but the timings already collected are
    not, so the run stops at the next boundary and still reports.
    """

    def __init__(self, seconds: float | None):
        self.seconds = seconds
        self.started = time.monotonic()
        self.hit = False

    @property
    def remaining(self) -> float | None:
        if self.seconds is None:
            return None
        return self.seconds - (time.monotonic() - self.started)

    def expired(self) -> bool:
        remaining = self.remaining
        if remaining is not None and remaining <= 0:
            self.hit = True
        return self.hit


class Benchmark:
    """Times strategies for one model at a fixed slice size and repeat count."""

    def __init__(
        self,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
        repeat: int = DEFAULT_REPEAT,
    ):
        self.sample_size = max(int(sample_size), 1)
        self.repeat = max(int(repeat), 1)

    def at(self, sample_size: int) -> Benchmark:
        """The same benchmark over a smaller slice -- never a larger one.

        A verdict knows its own N; timing 500 rows to explain a loop over 12
        measures a page the application never reads.
        """
        return Benchmark(max(min(int(sample_size), self.sample_size), 1), self.repeat)

    # -- one run -------------------------------------------------------

    def queryset(self, model, select=None, prefetch=None):
        # A fresh queryset per run: a queryset caches its rows after the first
        # evaluation, so reusing one would time an in-memory list.
        # Ordered by pk so every strategy reads the same rows, and sliced so
        # a timing never drags a whole table through memory.
        qs = model.objects.all().order_by("pk")
        if select:
            qs = qs.select_related(*select)
        if prefetch:
            qs = qs.prefetch_related(*prefetch)
        return qs[: self.sample_size]

    def touch(self, row: Model, plan: RelationPlan) -> None:
        """Provoke the query a relation costs when it is not hinted.

        A reverse or m2m accessor returns a related manager, and a manager
        issues no query until something consumes it -- so merely reading the
        attribute measures nothing at all.
        """
        try:
            value = getattr(row, plan.accessor)
        except ObjectDoesNotExist:
            # A reverse one-to-one with no row on the other side.
            return
        if plan.many and value is not None:
            for _related in value.all():
                pass

    def run_once(
        self,
        model: type[Model],
        plans,
        select=None,
        prefetch=None,
    ) -> float:
        qs = self.queryset(model, select, prefetch)
        # suppressed() is entered before the clock starts: setting a
        # ContextVar is cheap, but it is not part of what is being timed.
        with suppressed():
            started = time.perf_counter()
            for row in qs:
                for plan in plans:
                    self.touch(row, plan)
            return time.perf_counter() - started

    # -- measurement ---------------------------------------------------

    def measure(
        self,
        model: type[Model],
        plans,
        *,
        select=None,
        prefetch=None,
    ) -> Measurement:
        """Median of `repeat` runs, after one warmup run that is discarded.

        The warmup doubles as the query count: it runs under a debug cursor,
        which is too slow to time but counts exactly.
        """
        plans = list(plans)
        connection = connections[router.db_for_read(model) or DEFAULT_DB_ALIAS]
        with suppressed(), CaptureQueriesContext(connection) as captured:
            self.run_once(model, plans, select, prefetch)
        queries = len(captured.captured_queries)

        durations = [
            self.run_once(model, plans, select, prefetch)
            for _attempt in range(self.repeat)
        ]
        return Measurement(seconds=median(durations), queries=queries)

    def compare(self, model: type[Model], plan: RelationPlan) -> RelationResult:
        """Every strategy the relation supports, and which of them won."""
        measurements = {
            FieldOperation.VANILLA: self.measure(model, [plan]),
            FieldOperation.PREFETCH_RELATED: self.measure(
                model, [plan], prefetch=[plan.name]
            ),
        }
        if plan.can_select_related:
            measurements[FieldOperation.SELECT_RELATED] = self.measure(
                model, [plan], select=[plan.name]
            )

        vanilla = measurements[FieldOperation.VANILLA].seconds
        winner = min(measurements, key=lambda op: measurements[op].seconds)
        if measurements[winner].seconds >= vanilla:
            winner = FieldOperation.VANILLA

        return RelationResult(plan=plan, winner=winner, measurements=measurements)


# ----------------------------------------------------------------------
# formatting shared by the reports
# ----------------------------------------------------------------------


def format_measurement(measurement: Measurement | None, width: int = 22) -> str:
    if measurement is None:
        return "n/a".rjust(width)
    return f"{measurement.seconds:.6f}s {measurement.queries:>4}q".rjust(width)


def describe_measurement(measurement: Measurement | None) -> str:
    if measurement is None:
        return "not measured"
    plural = "query" if measurement.queries == 1 else "queries"
    return f"{measurement.seconds:.6f}s in {measurement.queries} {plural}"


def milliseconds(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds * 1000:.1f} ms"


def queries(count: int | None) -> str:
    if count is None:
        return "n/a"
    return f"{count} query" if count == 1 else f"{count} queries"

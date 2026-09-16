import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from statistics import median

from django.apps.registry import apps
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections, router
from django.db.models import Model
from django.db.models.fields.reverse_related import ForeignObjectRel
from django.test.utils import CaptureQueriesContext


class FieldOperation(str, Enum):
    VANILLA = "vanilla"
    SELECT_RELATED = "select_related"
    PREFETCH_RELATED = "prefetch_related"


# Every strategy is valid on a forward many-to-one / one-to-one: the related
# row can be joined in, batched in, or fetched one query at a time.
ALL_STRATEGIES = (
    FieldOperation.VANILLA,
    FieldOperation.SELECT_RELATED,
    FieldOperation.PREFETCH_RELATED,
)
# Reverse and many-to-many relations cannot be joined into the parent row --
# select_related() raises FieldError on them -- so only two strategies exist.
NO_JOIN_STRATEGIES = (FieldOperation.VANILLA, FieldOperation.PREFETCH_RELATED)

DEFAULT_SAMPLE_SIZE = 500
DEFAULT_REPEAT = 5

FORWARD = "forward"
REVERSE = "reverse"
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
        return RelationPlan(
            name=accessor,
            accessor=accessor,
            kind=MANY_TO_MANY if field.many_to_many else REVERSE,
            many=not field.one_to_one,
            strategies=NO_JOIN_STRATEGIES,
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


def plans_for(model: type[Model]) -> list[RelationPlan]:
    plans = []
    for field in model._meta.get_fields():
        plan = plan_for(field)
        if plan is not None:
            plans.append(plan)
    return plans


def format_measurement(measurement: Measurement | None, width: int = 22) -> str:
    if measurement is None:
        return "n/a".rjust(width)
    return f"{measurement.seconds:.6f}s {measurement.queries:>4}q".rjust(width)


def describe_measurement(measurement: Measurement | None) -> str:
    if measurement is None:
        return "not measured"
    plural = "query" if measurement.queries == 1 else "queries"
    return f"{measurement.seconds:.6f}s in {measurement.queries} {plural}"


class Command(BaseCommand):
    help = "Queryset optimizer tool for models containing foreign keys."

    sample_size = DEFAULT_SAMPLE_SIZE
    repeat = DEFAULT_REPEAT

    def add_arguments(self, parser):
        parser.add_argument("app.model", nargs="?", type=str, default=None)
        parser.add_argument(
            "--timeout",
            type=float,
            default=None,
            help=(
                "wall-clock budget in seconds for the whole run; partial "
                "results are still printed. Default: no limit."
            ),
        )
        parser.add_argument(
            "--sample-size",
            type=int,
            default=DEFAULT_SAMPLE_SIZE,
            help=(
                "rows per timed queryset. Every strategy reads the same "
                f"slice, ordered by pk. Default: {DEFAULT_SAMPLE_SIZE}."
            ),
        )
        parser.add_argument(
            "--repeat",
            type=int,
            default=DEFAULT_REPEAT,
            help=(
                "timed runs per strategy after a discarded warmup; the median "
                f"is reported. Default: {DEFAULT_REPEAT}."
            ),
        )
        parser.add_argument(
            "--django-models",
            action="store_true",
            help="when set includes django (and third party) models",
        )

    def _optimize_qs(
        self, model: type[Model], deadline: Deadline
    ) -> tuple[list[RelationResult], dict[str, Measurement]]:
        prefetch_names: list[str] = []
        select_names: list[str] = []
        results: list[RelationResult] = []

        measured: list[RelationPlan] = []
        for plan in plans_for(model):
            if deadline.expired():
                break
            measured.append(plan)
            result = self._optimize_relation(model, plan)
            results.append(result)
            if result.winner == FieldOperation.PREFETCH_RELATED:
                prefetch_names.append(plan.name)
            elif result.winner == FieldOperation.SELECT_RELATED:
                select_names.append(plan.name)

        combined: dict[str, Measurement] = {}
        if not deadline.expired():
            combined["no_optimization"] = self._measure(model, measured)
            combined["suggested_optimization"] = self._measure(
                model, measured, select=select_names, prefetch=prefetch_names
            )
        return results, combined

    # -- measurement ---------------------------------------------------

    def _touch(self, row: Model, plan: RelationPlan) -> None:
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

    def _run_once(
        self,
        model: type[Model],
        plans: list[RelationPlan],
        select: list[str] | None,
        prefetch: list[str] | None,
    ) -> float:
        # A fresh queryset per run: a queryset caches its rows after the first
        # evaluation, so reusing one would time an in-memory list.
        # Ordered by pk so every strategy reads the same rows, and sliced so
        # a timing never drags a whole table through memory.
        qs = model.objects.all().order_by("pk")
        if select:
            qs = qs.select_related(*select)
        if prefetch:
            qs = qs.prefetch_related(*prefetch)
        qs = qs[: self.sample_size]

        start_time: float = time.perf_counter()
        for row in qs:
            for plan in plans:
                self._touch(row, plan)
        return time.perf_counter() - start_time

    def _measure(
        self,
        model: type[Model],
        plans: list[RelationPlan],
        *,
        select: list[str] | None = None,
        prefetch: list[str] | None = None,
    ) -> Measurement:
        """Median of --repeat runs, after one warmup run that is discarded.

        The warmup doubles as the query count: it runs under a debug cursor,
        which is too slow to time but counts exactly.
        """
        connection = connections[router.db_for_read(model) or DEFAULT_DB_ALIAS]
        with CaptureQueriesContext(connection) as captured:
            self._run_once(model, plans, select, prefetch)
        queries = len(captured.captured_queries)

        durations = [
            self._run_once(model, plans, select, prefetch)
            for _attempt in range(self.repeat)
        ]
        return Measurement(seconds=median(durations), queries=queries)

    def _optimize_relation(
        self, model: type[Model], plan: RelationPlan
    ) -> RelationResult:
        measurements = {
            FieldOperation.VANILLA: self._measure(model, [plan]),
            FieldOperation.PREFETCH_RELATED: self._measure(
                model, [plan], prefetch=[plan.name]
            ),
        }
        if plan.can_select_related:
            measurements[FieldOperation.SELECT_RELATED] = self._measure(
                model, [plan], select=[plan.name]
            )

        vanilla = measurements[FieldOperation.VANILLA].seconds
        winner = min(measurements, key=lambda op: measurements[op].seconds)
        if measurements[winner].seconds >= vanilla:
            winner = FieldOperation.VANILLA

        return RelationResult(plan=plan, winner=winner, measurements=measurements)

    # -- reporting -----------------------------------------------------

    def _print_results(
        self,
        model: type[Model],
        results: list[RelationResult],
        combined: dict[str, Measurement],
    ) -> None:
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"{model._meta.label} -- foreign key optimization results"
            )
        )

        if not results:
            self.stdout.write("No per-relation timings were collected.")
        else:
            self.stdout.write(
                f"Per-relation timings (median of {self.repeat} runs, "
                f"at most {self.sample_size} rows):"
            )
            self.stdout.write(
                f"{'#':<4}{'relation':<24}{'kind':<9}{'winner':<18}"
                f"{FieldOperation.VANILLA.value:>22}"
                f"{FieldOperation.SELECT_RELATED.value:>22}"
                f"{FieldOperation.PREFETCH_RELATED.value:>22}"
            )

            operation_counts = dict.fromkeys(FieldOperation, 0)
            for index, result in enumerate(results, start=1):
                operation_counts[result.winner] += 1
                cells = "".join(
                    format_measurement(result.measurements.get(operation))
                    for operation in (
                        FieldOperation.VANILLA,
                        FieldOperation.SELECT_RELATED,
                        FieldOperation.PREFETCH_RELATED,
                    )
                )
                self.stdout.write(
                    f"{index:<4}{result.plan.accessor:<24}"
                    f"{result.plan.kind:<9}{result.winner.value:<18}{cells}"
                )

            self.stdout.write("")
            self.stdout.write("Suggested operations:")
            self.stdout.write(
                f"{FieldOperation.SELECT_RELATED.value}: "
                f"{operation_counts[FieldOperation.SELECT_RELATED]}, "
                f"{FieldOperation.PREFETCH_RELATED.value}: "
                f"{operation_counts[FieldOperation.PREFETCH_RELATED]}, "
                f"{FieldOperation.VANILLA.value}: "
                f"{operation_counts[FieldOperation.VANILLA]}"
            )

        vanilla = combined.get("no_optimization")
        suggested = combined.get("suggested_optimization")

        self.stdout.write("")
        self.stdout.write("Combined queryset timings:")
        self.stdout.write(f"No optimization:        {describe_measurement(vanilla)}")
        self.stdout.write(f"Suggested optimization: {describe_measurement(suggested)}")

        if vanilla is None or suggested is None:
            return

        difference = vanilla.seconds - suggested.seconds
        percentage = (difference / vanilla.seconds * 100) if vanilla.seconds else 0

        if difference > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Suggested optimization is faster by {difference:.6f}s "
                    f"({percentage:.2f}%), "
                    f"{vanilla.queries - suggested.queries} fewer queries."
                )
            )
        elif difference < 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Suggested optimization is slower by {abs(difference):.6f}s "
                    f"({abs(percentage):.2f}%)."
                )
            )
        else:
            self.stdout.write(
                "Suggested optimization matched the vanilla queryset timing."
            )

    # -- entry point ---------------------------------------------------

    def _select_models(self, selection: str | None, django_models: bool):
        model_s: list[type[Model]] = []
        try:
            if selection is None:
                model_s = list(apps.get_models())
                if not django_models:
                    local_apps = {
                        ac.label
                        for ac in apps.get_app_configs()
                        if not ac.name.startswith("django.")
                    }
                    model_s = [
                        mdl for mdl in model_s if mdl._meta.app_label in local_apps
                    ]
            elif "." in selection:
                model_s.append(apps.get_model(selection))
            else:
                model_s.extend(apps.get_app_config(selection).get_models())
        except (LookupError, ValueError) as e:
            raise CommandError(str(e)) from e
        return [mdl for mdl in model_s if plans_for(mdl)]

    def handle(self, *args, **options):
        if options["sample_size"] < 1:
            raise CommandError("--sample-size must be at least 1")
        if options["repeat"] < 1:
            raise CommandError("--repeat must be at least 1")
        self.sample_size = options["sample_size"]
        self.repeat = options["repeat"]

        model_s = self._select_models(options["app.model"], options["django_models"])

        if not model_s:
            self.stdout.write(
                "No model found with a fk field. "
                "No select_related/prefetch optimization can be done."
            )
            return

        deadline = Deadline(options["timeout"])
        for mdl in model_s:
            if deadline.expired():
                break
            results, combined = self._optimize_qs(mdl, deadline)
            self._print_results(mdl, results, combined)

        if deadline.hit:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"Run cut short after {deadline.seconds:g}s (--timeout); "
                    "the results above are partial."
                )
            )

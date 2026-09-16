import time
from dataclasses import dataclass
from enum import Enum

from django.apps.registry import apps
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Model
from django.db.models.fields.reverse_related import ForeignObjectRel


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


class Command(BaseCommand):
    help = "Queryset optimizer tool for models containing foreign keys."

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
            "--django-models",
            action="store_true",
            help="when set includes django (and third party) models",
        )

    def _optimize_qs(
        self, model: type[Model], deadline: Deadline
    ) -> tuple[
        list[tuple[RelationPlan, FieldOperation, float, float | None, float]],
        dict[str, float],
    ]:
        prefetch_names: list[str] = []
        select_names: list[str] = []
        per_field_time_metrics: list[
            tuple[RelationPlan, FieldOperation, float, float | None, float]
        ] = []

        plans = plans_for(model)
        measured: list[RelationPlan] = []
        for plan in plans:
            if deadline.expired():
                break
            measured.append(plan)
            field_results = self._optimize_relation(model, plan)
            per_field_time_metrics.append((plan, *field_results))
            winner = field_results[0]
            if winner == FieldOperation.PREFETCH_RELATED:
                prefetch_names.append(plan.name)
            elif winner == FieldOperation.SELECT_RELATED:
                select_names.append(plan.name)

        final_times: dict[str, float] = {}
        if not deadline.expired():
            final_times["no_optimization_time"] = self._time_qs(model, measured)
            final_times["suggested_optimization_time"] = self._time_qs(
                model, measured, select=select_names, prefetch=prefetch_names
            )
        return per_field_time_metrics, final_times

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

    def _warmup_cache(
        self, model: type[Model], plans: list[RelationPlan], count: int = 3
    ) -> None:
        for _attempt in range(count):
            self._time_qs(model, plans)

    def _time_qs(
        self,
        model: type[Model],
        plans: list[RelationPlan],
        *,
        select: list[str] | None = None,
        prefetch: list[str] | None = None,
    ) -> float:
        qs = model.objects.all()
        if select:
            qs = qs.select_related(*select)
        if prefetch:
            qs = qs.prefetch_related(*prefetch)

        start_time: float = time.perf_counter()
        for row in qs:
            for plan in plans:
                self._touch(row, plan)
        return time.perf_counter() - start_time

    def _optimize_relation(
        self, model: type[Model], plan: RelationPlan
    ) -> tuple[FieldOperation, float, float | None, float]:
        self._warmup_cache(model, [plan])

        vanilla_time = self._time_qs(model, [plan])
        select_related_time = (
            self._time_qs(model, [plan], select=[plan.name])
            if plan.can_select_related
            else None
        )
        prefetch_related_time = self._time_qs(model, [plan], prefetch=[plan.name])

        timings = {
            FieldOperation.VANILLA: vanilla_time,
            FieldOperation.PREFETCH_RELATED: prefetch_related_time,
        }
        if select_related_time is not None:
            timings[FieldOperation.SELECT_RELATED] = select_related_time

        winner = min(timings, key=timings.__getitem__)
        if timings[winner] >= vanilla_time:
            winner = FieldOperation.VANILLA

        return (winner, vanilla_time, select_related_time, prefetch_related_time)

    # -- reporting -----------------------------------------------------

    def _print_results(
        self,
        model: type[Model],
        per_field_time_metrics: list[
            tuple[RelationPlan, FieldOperation, float, float | None, float]
        ],
        final_times: dict[str, float],
    ) -> None:
        def format_time(value: float | None) -> str:
            if value is None:
                return "N/A"
            return f"{value:.6f}s"

        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"{model._meta.label} -- foreign key optimization results"
            )
        )

        if not per_field_time_metrics:
            self.stdout.write("No per-field relation timings were collected.")
        else:
            self.stdout.write("Per-relation timings:")
            self.stdout.write(
                f"{'#':<4}"
                f"{'relation':<24}"
                f"{'kind':<10}"
                f"{'winner':<18}"
                f"{FieldOperation.VANILLA.value:>14}"
                f"{FieldOperation.SELECT_RELATED.value:>18}"
                f"{FieldOperation.PREFETCH_RELATED.value:>20}"
            )

            operation_counts = {
                FieldOperation.VANILLA: 0,
                FieldOperation.SELECT_RELATED: 0,
                FieldOperation.PREFETCH_RELATED: 0,
            }
            for index, (
                plan,
                winner,
                vanilla_time,
                select_related_time,
                prefetch_related_time,
            ) in enumerate(per_field_time_metrics, start=1):
                operation_counts[winner] = operation_counts.get(winner, 0) + 1
                self.stdout.write(
                    f"{index:<4}"
                    f"{plan.accessor:<24}"
                    f"{plan.kind:<10}"
                    f"{winner.value:<18}"
                    f"{format_time(vanilla_time):>14}"
                    f"{format_time(select_related_time):>18}"
                    f"{format_time(prefetch_related_time):>20}"
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

        no_optimization_time = final_times.get("no_optimization_time")
        suggested_optimization_time = final_times.get("suggested_optimization_time")

        self.stdout.write("")
        self.stdout.write("Combined queryset timings:")
        self.stdout.write(f"No optimization: {format_time(no_optimization_time)}")
        self.stdout.write(
            f"Suggested optimization: {format_time(suggested_optimization_time)}"
        )

        if no_optimization_time is None or suggested_optimization_time is None:
            return

        difference = no_optimization_time - suggested_optimization_time
        if no_optimization_time:
            percentage = (difference / no_optimization_time) * 100
        else:
            percentage = 0

        if difference > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Suggested optimization is faster by {format_time(difference)} "
                    f"({percentage:.2f}%)."
                )
            )
        elif difference < 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Suggested optimization is slower by {format_time(abs(difference))} "
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
            per_field_time_metrics, final_times = self._optimize_qs(mdl, deadline)
            self._print_results(mdl, per_field_time_metrics, final_times)

        if deadline.hit:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"Run cut short after {deadline.seconds:g}s (--timeout); "
                    "the results above are partial."
                )
            )

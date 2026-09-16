"""The `fk_optimize` command.

Orchestration only. Everything it reports comes out of `analysis/`: the
relation classification and the timings from `analysis.benchmark`, and nothing
is defined twice between the two.
"""

from django.apps.registry import apps
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Model

from ...analysis.benchmark import (
    ALL_STRATEGIES,
    DEFAULT_REPEAT,
    DEFAULT_SAMPLE_SIZE,
    FORWARD,
    MANY_TO_MANY,
    NO_JOIN_STRATEGIES,
    REVERSE,
    REVERSE_ONE_TO_ONE,
    Benchmark,
    Deadline,
    FieldOperation,
    Measurement,
    RelationPlan,
    RelationResult,
    describe_measurement,
    format_measurement,
    plan_for,
    plans_for,
)

__all__ = [
    "ALL_STRATEGIES",
    "Benchmark",
    "Command",
    "DEFAULT_REPEAT",
    "DEFAULT_SAMPLE_SIZE",
    "Deadline",
    "FORWARD",
    "FieldOperation",
    "MANY_TO_MANY",
    "Measurement",
    "NO_JOIN_STRATEGIES",
    "REVERSE",
    "REVERSE_ONE_TO_ONE",
    "RelationPlan",
    "RelationResult",
    "describe_measurement",
    "format_measurement",
    "plan_for",
    "plans_for",
]


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

    def benchmark(self) -> Benchmark:
        """A benchmark at the sizes this run was asked for.

        Built per call rather than stored, so that `sample_size` and `repeat`
        may be set on the command after it is constructed and still be read.
        """
        return Benchmark(self.sample_size, self.repeat)

    def _measure(
        self,
        model: type[Model],
        plans: list[RelationPlan],
        *,
        select: list[str] | None = None,
        prefetch: list[str] | None = None,
    ) -> Measurement:
        return self.benchmark().measure(model, plans, select=select, prefetch=prefetch)

    def _optimize_relation(
        self, model: type[Model], plan: RelationPlan
    ) -> RelationResult:
        return self.benchmark().compare(model, plan)

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
            self.stdout.write(
                "Suggested operations (each relation measured on its own -- "
                "see the combined result below before applying them together):"
            )
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
            # Each winner above beat vanilla alone. Applied together they need
            # not: two select_related() joins multiply rows, and the product of
            # two individually cheap joins can be worse than the N+1 it
            # replaced. The measurement already knows this; say so rather than
            # hand back the per-relation winners as a safe set.
            self.stdout.write(
                self.style.WARNING(
                    f"Suggested optimization measured SLOWER than no "
                    f"optimization, by {abs(difference):.6f}s "
                    f"({abs(percentage):.2f}%), even though it used "
                    f"{vanilla.queries - suggested.queries} fewer queries."
                )
            )
            self.stdout.write(
                "Do not apply the winners above as a set. Joins combine "
                "multiplicatively; adopt them one at a time, re-running this "
                "command after each, and keep the ones that still help."
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

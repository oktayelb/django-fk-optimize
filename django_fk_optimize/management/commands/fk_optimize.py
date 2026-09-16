"""The `fk_optimize` command: orchestration, and nothing else.

Every number it prints is produced somewhere else -- `analysis.benchmark`
times, `analysis.cardinality` counts, `analysis.verdicts` decides and
`analysis.report` renders -- so there is one definition of each and the
command is the thing that puts them in order and bounds how long they run.

The command is useful with no recording at all: the scanner enumerates the
call sites, COUNTs estimate N, and the benchmark prices the alternative.  It is
much better with one, because then N is observed rather than guessed and the
template and serializer accesses no AST can see are visible too.  The coverage
block at the bottom of every report says which of those two runs just
happened, and says how to get the better one.

With `--no-callsites` there is nothing to join, so the command falls back to
what it can still do honestly: time every relation of every selected model,
each on its own and then all together.
"""

import argparse
from pathlib import Path

from django.apps.registry import apps
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Model

from ... import conf
from ...analysis import report as report_module
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
from ...analysis.cardinality import Cardinalities
from ...analysis.report import Coverage, render_text
from ...analysis.verdicts import Tables, build, cost
from ...recording import store
from ...utils.callsites import PROBABLE, RESOLVED, scan_files
from ...utils.sources import discover
from ...utils.vocabulary import Vocabulary

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

DEFAULT_MIN_ROWS = 0
STDOUT = "-"

STYLES = {
    report_module.HEADING: "MIGRATE_HEADING",
    report_module.SUCCESS: "SUCCESS",
    report_module.WARNING: "WARNING",
    report_module.NOTICE: "NOTICE",
}


class Command(BaseCommand):
    help = (
        "Find the foreign keys your code loads one row at a time, and measure "
        "what select_related()/prefetch_related() would save instead."
    )

    sample_size = DEFAULT_SAMPLE_SIZE
    repeat = DEFAULT_REPEAT

    def add_arguments(self, parser):
        parser.add_argument(
            "app.model",
            nargs="?",
            type=str,
            default=None,
            help=(
                "an app label or app_label.ModelName to narrow the report to. "
                "Give it before --json, which also takes an optional value."
            ),
        )
        parser.add_argument(
            "--callsites",
            action=argparse.BooleanOptionalAction,
            default=True,
            help=(
                "scan the project's source for querysets and join them to the "
                "recording. --no-callsites falls back to timing every relation "
                "of every selected model. Default: on."
            ),
        )
        parser.add_argument(
            "--benchmark",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="time the alternatives against the database. Default: on.",
        )
        parser.add_argument(
            "--recording",
            type=str,
            default=None,
            help=("JSONL recording to read. Default: FK_OPTIMIZE['RECORDING_PATH']."),
        )
        parser.add_argument(
            "--clear-recording",
            action="store_true",
            help="delete the recording once it has been read.",
        )
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
                "rows per timed queryset, and the cap on an estimated N. Every "
                "strategy reads the same slice, ordered by pk. Default: "
                f"{DEFAULT_SAMPLE_SIZE}."
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
            "--min-rows",
            type=int,
            default=DEFAULT_MIN_ROWS,
            help=(
                "ignore relations on tables with fewer rows than this. "
                f"Default: {DEFAULT_MIN_ROWS}."
            ),
        )
        parser.add_argument(
            "--json",
            nargs="?",
            const=STDOUT,
            default=None,
            metavar="PATH",
            help=(
                "write the report as JSON. With no path it replaces the text "
                "report on stdout; with one it is written alongside it."
            ),
        )
        parser.add_argument(
            "--fail-on-findings",
            action="store_true",
            help="exit non-zero when any actionable verdict exists (for CI).",
        )
        parser.add_argument(
            "--include-django",
            action="store_true",
            help="include django's own and third-party apps.",
        )
        parser.add_argument(
            "--django-models",
            action="store_true",
            help="deprecated alias for --include-django.",
        )

    # -- timing shims ---------------------------------------------------

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

    # -- selection ------------------------------------------------------

    def _select_models(self, selection: str | None, include_django: bool):
        model_s: list[type[Model]] = []
        try:
            if selection is None:
                model_s = list(apps.get_models())
                if not include_django:
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

    # -- entry point ----------------------------------------------------

    def handle(self, *args, **options):
        if options["sample_size"] < 1:
            raise CommandError("--sample-size must be at least 1")
        if options["repeat"] < 1:
            raise CommandError("--repeat must be at least 1")
        if options["min_rows"] < 0:
            raise CommandError("--min-rows cannot be negative")
        self.sample_size = options["sample_size"]
        self.repeat = options["repeat"]

        include_django = options["include_django"]
        if options["django_models"]:
            self.stderr.write(
                self.style.WARNING(
                    "--django-models is deprecated and will be removed; use "
                    "--include-django."
                )
            )
            include_django = True

        model_s = self._select_models(options["app.model"], include_django)
        if not model_s:
            self.stdout.write(
                "No model found with a relation. "
                "No select_related/prefetch optimization can be done."
            )
            return

        deadline = Deadline(options["timeout"])
        if not options["callsites"]:
            self._sweep(model_s, deadline)
            return
        self._report(model_s, deadline, include_django, options)

    # -- the verdict report ---------------------------------------------

    def _recording_path(self, options) -> Path:
        given = options["recording"]
        return Path(given) if given else conf.recording_path()

    def _report(self, model_s, deadline, include_django, options):
        labels = {model._meta.label for model in model_s}
        by_label = {model._meta.label: model for model in apps.get_models()}

        vocabulary = Vocabulary.from_apps(include_django)
        tables = Tables.from_apps(include_django)

        # Every project file is scanned even when the report is narrowed to one
        # model: the call site that iterates it is very often in another app.
        scan = scan_files(
            (path for path, _package in discover(include_django)),
            vocabulary,
            package_for=dict(discover(include_django)).get,
        )
        sites = [site for site in scan.sites if site.model in labels]

        path = self._recording_path(options)
        recording = store.load(path)
        groups = recording.groups()

        counts = Cardinalities(min_rows=options["min_rows"])

        def cardinality(label, relation):
            model = by_label.get(label)
            return counts.get(model, relation) if model is not None else None

        verdicts, joined = build(
            sites,
            groups,
            vocabulary,
            tables,
            cardinality=cardinality,
            sample_size=self.sample_size,
        )
        verdicts = [
            verdict
            for verdict in verdicts
            if verdict.model in labels
            and self._big_enough(verdict, cardinality, options["min_rows"])
        ]

        timed = 0
        if options["benchmark"] and not deadline.expired():
            timed = cost(verdicts, self.benchmark(), by_label.get, deadline)

        coverage = Coverage(
            files=scan.files,
            sites=len(scan.sites),
            sites_resolved=sum(1 for site in scan.sites if site.confidence == RESOLVED),
            sites_probable=sum(1 for site in scan.sites if site.confidence == PROBABLE),
            sites_unresolved=len(scan.unresolved),
            scan_errors=len(scan.errors),
            recording_path=str(path),
            recording_exists=recording.exists,
            records=recording.count,
            malformed=recording.malformed,
            groups=len(groups),
            recording_age_seconds=recording.age_seconds,
            findings_matched=len(joined.matched),
            findings_runtime_only=len(joined.runtime_only),
            findings_unattributed=len(joined.unattributed),
            models=len(model_s),
            relations_benchmarked=timed,
            benchmarked=bool(options["benchmark"]),
            scanned=True,
            timed_out=deadline.hit,
        )

        destination = options["json"]
        if destination != STDOUT:
            self._emit(render_text(verdicts, coverage))
        if destination is not None:
            payload = report_module.dumps(verdicts, coverage)
            if destination == STDOUT:
                self.stdout.write(payload)
            else:
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_text(payload + "\n", encoding="utf-8")

        if options["clear_recording"] and recording.exists:
            store.clear(path)

        findings = [verdict for verdict in verdicts if verdict.actionable]
        if options["fail_on_findings"] and findings:
            raise CommandError(
                f"{len(findings)} actionable finding"
                f"{'s' if len(findings) != 1 else ''} (--fail-on-findings)"
            )

    def _big_enough(self, verdict, cardinality, min_rows) -> bool:
        """Drop a verdict about a table --min-rows says to ignore."""
        if min_rows <= 0 or not verdict.relation:
            return True
        stats = cardinality(verdict.model, verdict.relation)
        return stats is None or stats.rows >= min_rows

    def _emit(self, lines) -> None:
        for text, style in lines:
            name = STYLES.get(style)
            if name and hasattr(self.style, name):
                text = getattr(self.style, name)(text)
            self.stdout.write(text)

    # -- the fallback sweep ---------------------------------------------

    def _sweep(self, model_s, deadline: Deadline) -> None:
        """Time every relation of every selected model, one at a time.

        What is left when there are no call sites to reason about: no idea
        where the relation is used, so no verdict -- only what each strategy
        costs on this database, including on the reverse and many-to-many
        relations the forward-FK verdict engine never looks at.
        """
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
                f"{'#':<4}{'relation':<24}{'kind':<13}{'winner':<18}"
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
                    f"{result.plan.kind:<13}{result.winner.value:<18}{cells}"
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

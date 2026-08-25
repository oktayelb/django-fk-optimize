from os import times

from django.core.management.base import BaseCommand , CommandError
from django.db.models import Model, Field, ForeignObjectRel
from django.apps.registry import apps

import time
from typing import Optional 
from enum import Enum


class FieldOperation(str, Enum):
    VANILLA = "vanilla"
    SELECT_RELATED = "select_related"
    PREFETCH_RELATED = "prefetch_related"

class Command(BaseCommand):

    help = "Queryset optimizer tool for models containing foreign keys."

    def add_arguments(self, parser):

        parser.add_argument("app.model", nargs="?", type=str , default= None)
        parser.add_argument("--timeout", type=int)
        parser.add_argument(
            "--django-models",
            action= "store_true",
            help= "when set includes django (and third party) models"
        )

    def _optimize_qs(self, model: type[Model]) ->tuple[list[tuple[FieldOperation,float,float,float]] ,dict[str,float]]:
        prefetch_fields: list[Field] = []
        select_fields : list[Field]  = []
        per_field_time_metrics: list[tuple[FieldOperation,float,float,float]] = []

        model_fields: list[Field[any,any] | ForeignObjectRel] = model._meta.get_fields()
        for field in model_fields:
            if not field.is_relation:
                continue
            field_results = self._optimize_relation(model,field)
            per_field_time_metrics.append(field_results)
            result = field_results[0]
            if result == FieldOperation.PREFETCH_RELATED:
                prefetch_fields.append(field)
            elif result == FieldOperation.SELECT_RELATED:
                select_fields.append(field)

        final_times: dict[str,float] = {}
        no_optimization_time = self._time_qs(model,vanilla_fields= model_fields)
        suggested_optimization_time = self._time_qs(model,select_fields= select_fields,prefetch_fields=prefetch_fields)
        final_times["no_optimization_time"] = no_optimization_time
        final_times["suggested_optimization_time"] = suggested_optimization_time
        # we should maybe
        # try all selectables by themselves
        # try all prefetchables by themselves
        # or try every possibility??

        return per_field_time_metrics, final_times
        
    def _warmup_cache(self, model: type[Model], field : Optional[Field] = None, count:int = 10) -> None:
        for i in range (0,count):
            list(model.objects.all())

    # generalize this to accept list of fields so we can actually time  objects.all().prefetch(f1,f2).select(f3,f4)
    def _time_qs(self, model:type[Model],
                *, 
                prefetch_fields: Optional[list[Field]] = None,
                select_fields: Optional[list[Field]]= None,
                vanilla_fields: Optional[list[Field]]= None
                ) -> float:
        
        qs = model.objects.all()
        fields: list[Field] = []
        if vanilla_fields:
            fields += vanilla_fields

        
        if select_fields:
            qs = qs.select_related(*(field.name for field in select_fields))
            fields += select_fields

        if prefetch_fields:
            qs = qs.prefetch_related(*(field.name for field in prefetch_fields))
            fields += prefetch_fields

        start_time: float = time.perf_counter()
        # not so sur eabout this part
        # we might need to time the query and the N+1 part seperately
        for element in qs:
            for field in fields:
                pass
            #access element.field to trigger N+1
        end_time: float = time.perf_counter()
        return end_time - start_time

    def _optimize_relation(self, model: type[Model], field:Field ) -> tuple[FieldOperation, float, float, float]:

        self._warmup_cache(model=model)
        winner: FieldOperation = FieldOperation.VANILLA


        vanilla_time: float =  self._time_qs(model,vanilla_fields=[field])

        select_related_time: float = self._time_qs(model,select_fields=[field])


        prefetch_related_time: float = self._time_qs(model,prefetch_fields=[field])

        if prefetch_related_time < select_related_time:
            if prefetch_related_time < vanilla_time:
                winner = FieldOperation.PREFETCH_RELATED
            else:
                winner = FieldOperation.VANILLA
        else:
            if select_related_time  < vanilla_time:
                winner = FieldOperation.SELECT_RELATED
            else:
                winner = FieldOperation.VANILLA

        return (winner, vanilla_time, select_related_time, prefetch_related_time)
        
    def _print_results(self, per_field_time_metrics: list[tuple[FieldOperation,float,float,float]],final_times: dict[str,float] ) -> None:
        def format_time(value: float | None) -> str:
            if value is None:
                return "N/A"
            return f"{value:.6f}s"

        self.stdout.write(self.style.MIGRATE_HEADING("Foreign key optimization results"))

        if not per_field_time_metrics:
            self.stdout.write("No per-field relation timings were collected.")
        else:
            self.stdout.write("Per-relation timings:")
            self.stdout.write(
                f"{'#':<4}"
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
                winner,
                vanilla_time,
                select_related_time,
                prefetch_related_time,
            ) in enumerate(per_field_time_metrics, start=1):
                operation_counts[winner] = operation_counts.get(winner, 0) + 1
                self.stdout.write(
                    f"{index:<4}"
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
        self.stdout.write(f"Suggested optimization: {format_time(suggested_optimization_time)}")

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
            self.stdout.write("Suggested optimization matched the vanilla queryset timing.")

    def handle (self, *args,**options):


        selection :str | None  = options["app.model"]
        model_s :list[type[Model]] = [] 

        try:
            if selection is None:
                model_s = list(apps.get_models())
                if not options["django_models"]:
                    local_apps = {ac.label for ac in apps.get_app_configs()
                            if not ac.name.startswith("django.")}
                    model_s = [mdl for mdl in model_s if mdl._meta.app_label in local_apps]
                
            elif "." in selection:
                model_s.append(apps.get_model(selection))

            else:    
                for mdl in apps.get_app_config(selection).get_models():
                    model_s.append(mdl)

        except (LookupError ,ValueError) as e:
            raise CommandError(str(e)) from e

        
        model_s = [mdl for mdl in model_s if any(f.is_relation for f in mdl._meta.get_fields())]

        if not model_s:
            self.stdout.write("No model found with a fk field. No  select_related/prefetch optimization can be done.")
            return

        else:
            for mdl in model_s:
                per_field_time_metrics, final_times = self._optimize_qs(mdl)
                self._print_results(per_field_time_metrics, final_times)
from django.core.management.base import BaseCommand , CommandError
from django.db.models import Model, ForeignObject , Field
from django.apps.registry import apps

import time
from typing import Optional

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

    def _optimize_qs(self, model: Model):
        prefetch_fields = []
        select_fields = []
        for field in model._meta.get_fields():
            if not field.is_relation:
                continue
            if self._optimize_relation(model, field)[0] == "prefetch":
                prefetch_fields.append(field)
            else:
                select_fields.append(field)



        #try all select related
        # try all prefetch related
        # try the per field optimal that we have derived.

        # report the best

        #combine and try for the combined version as well.
        
    def _warmup_cache(self, model, field : Optional[Field], count: Optional[int] = 10):
        for i in range (0,count):
            list(model.objects.all())


    def _optimize_relation(self, model: Model, field:Field):

        self._warmup_cache(model=model)
        winner : str = ""

        start_time: float = time.perf_counter()
        list(model.objects.all())
        end_time: float = time.perf_counter()

        vanilla_time: float = end_time - start_time

        start_time= time.perf_counter()
        list(model.objects.all().select_related(field))
        end_time = time.perf_counter()

        select_related_time: float = end_time - start_time

        start_time = time.perf_counter()
        list(model.objects.all().prefetch_related(field))
        end_time = time.perf_counter()

        prefetch_related_time: float = end_time - start_time

        if prefetch_related_time < select_related_time:
            if prefetch_related_time < vanilla_time:
                winner = "prefetch"
            else:
                winner = "vanilla"
        else:
            if select_related_time  < vanilla_time:
                winner = "select"
            else:
                winner = "vanilla"

        return [winner, vanilla_time, select_related_time, prefetch_related_time]
        

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
                self._optimize_qs(mdl)

       # return the results
        
from django.core.management.base import BaseCommand , CommandError
from django.db.models import Model
from django.apps.registry import apps

class Command(BaseCommand):

    help = "Queryset optimizer tool for models containing foreign keys."

    def add_arguments(self, parser):

        parser.add_argument("app.model", nargs="?", type=str , default= None)
        parser.add_argument("--timeout", type=int)
        parser.add_argument(
            "--django-models",
            action= "store_true",
            help= "when set includes models not written by the dev"
        )

    def _optimize_qs(self, model):
        pass

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

        #only keep the models that has at least one fk
        model_s = [mdl for mdl in model_s if any(f.is_relation for f in mdl._meta.get_fields())]

        if not model_s:
            self.stdout.write("No model found with a fk field. No  select_related/prefetch optimization can be done.")
            return

        else:
            for mdl in model_s:
                self._optimize_qs(mdl)

       # return the results
        
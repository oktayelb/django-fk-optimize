from django.core.management.base import BaseCommand
from django.db.models import Model
from django.apps.registry import apps

class Command(BaseCommand):

    help = "Queryset optimizer tool for models containing foreign keys."

    def add_arguments(self, parser):

        parser.add_argument("app.model", nargs="?", type=str , default= None)
        parser.add_argument("--timeout", type=int)

    def handle (self, *args,**options):


        selection :str  = options["app.model"]
        model_s :list[type[Model]] = [] 

        try:
            if selection is None:
                model_s = list(apps.get_models())
                
            elif "." in selection:
                model_s.append(apps.get_model(selection))

            else:    
                for mdl in apps.get_app_config(selection).get_models():
                    model_s.append(mdl)

        except LookupError:
            print("Couldnt Find Specified Model(s)")
        
       #right now eihter we threw an error, 
       # or model_s has all of the models we need

       # for every model:
       # if  no fk, skip
       # else find the more suitable operation (select_related/prefetch)
       # return the results
        
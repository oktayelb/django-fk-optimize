this project aims at optimizing django queries for models having foregin keys.

Determining select related and prefetch related just by the type of the relationship (one to many , many to many etc) is not always enough.
Sometimes seeing the count of the data gives a better view for the developer. For example if the amount of foreign keys are not much but the primary key elements are a lot, selecting prefetch related will be a better solution.
However, this needs testing and actual running. There is not a single healthy threshold for determining the ratio of pk/fk. 

So django-fk-optimizer aims to do this: 

Aquire every model inside a django project via

from django.apps import apps

# Returns a list of all model classes in the project
all_models = apps.get_models()

After doingso, iterate thru the fields of the model to determine if it has fks, and note the  metadata such as count of fks,  what types of relations they have etc.

The way this project actually aqcuires the data and runs is pretty simple, it will be a manage.py command that will be run by the developers to gather data in the initial stage. 

running 

python manage.py fk-optimize app_name.model_name will only do the optimiziation for app_name.model_name
python manage.py fk-optimize app_name will do the optimiziation for every model under the app app_name
python manage.py fk-optimize  has the scope of the entire project.

useful parameters we can add would be --timeout. Thats all I can think for now. 



After having the  models and access to the actual data, it starts executing model.objects.all() bare, with select related and with prefect related to find the best strategy for "each fk".


By default we ignore django models, if you want to include them you have to run with the --all-apps flag.
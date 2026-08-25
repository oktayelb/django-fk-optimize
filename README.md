# Django Fk Optimizer

this project aims at optimizing django queries for models having foreign keys.

Determining select_related() and prefetch_related() just by the type of the relationship is not always enough.

Sometimes seeing the count of the data gives a better view for the developer. For example if the amount of foreign keys are not much but the primary key elements are a lot, selecting prefetch_related() will be a better solution.

However, this needs testing and actual running. since there is not a single healthy threshold for determining.

django-fk-optimizer solves this by offering the dev's a management command that they can run to learn which operation is more suitable with the current distribution of data.


# Flow

1. Acquire every model of the django project

2. Get rid of the ones without a relation to another Model

3. Execute queries with every possible setting for every model & fk_field pair.

4. report the results.




# Commands & Args

```bash
python manage.py fk_optimize app_name.model_name
```

will only do the optimization for app_name.model_name

```bash
python manage.py fk_optimize app_name
```

will do the optimization for every model under the app app_name

```bash
python manage.py fk_optimize
```

has the scope of the entire project.


`--timeout` for maximum time spent for the command to run

`--django-models` flag to include django's (and third party apps) models


By default we only ignore models that begin with `django.` for the moment. Third party models are not ignored as they can be discarded by the developer.


# Future Features:

Serializer based optimizations 

for the final metrics,
we should maybe
try all selectables by themselves
try all prefetchables by themselves
or try every possibility??
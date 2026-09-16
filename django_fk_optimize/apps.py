from django.apps import AppConfig


class DjangoFkOptimizeConfig(AppConfig):
    name = "django_fk_optimize"
    label = "django_fk_optimize"
    verbose_name = "Django FK Optimize"
    # The app ships no models, but an app without this raises W042 in any
    # project that has not set DEFAULT_AUTO_FIELD.
    default_auto_field = "django.db.models.BigAutoField"

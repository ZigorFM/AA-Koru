from django.apps import AppConfig


class RekstatsConfig(AppConfig):
    name = "rekstats"
    label = "rekstats"
    verbose_name = "Rekium Stats"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        import rekstats.hooks  # noqa: F401 — registra hooks al arrancar AA

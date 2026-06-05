from django.apps import AppConfig


class KoruStatsConfig(AppConfig):
    name = "koru_stats"
    label = "koru_stats"
    verbose_name = "Koru Stats"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        import koru_stats.hooks  # noqa: F401

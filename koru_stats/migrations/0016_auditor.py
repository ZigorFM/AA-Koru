from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("koru_stats", "0015_characterkillrecord_kill_hour"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="general",
            options={
                "default_permissions": (),
                "managed": False,
                "permissions": (
                    ("basic_access", "Puede ver Estadísticas y Mi Dashboard"),
                    ("corp_finance_access", "Puede ver Finanzas Corp"),
                    ("moon_tax_access", "Puede ver Tax Lunas"),
                    ("moon_tax_admin", "Puede gestionar tax de lunas"),
                    ("pvp_access", "Puede ver estadísticas PvP detalladas"),
                    ("fc_access", "Puede ver panel FC/Director"),
                    ("auditor_access", "Puede ver el panel Auditor (seguridad)"),
                    ("auditor_admin", "Puede configurar el Auditor y revisar/descartar alertas"),
                ),
            },
        ),
        migrations.CreateModel(
            name="AuditorConfig",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tag", models.CharField(default="default", max_length=50, unique=True)),
                ("w_pvp", models.DecimalField(decimal_places=2, default=0.25, max_digits=4)),
                ("w_ciclo", models.DecimalField(decimal_places=2, default=0.20, max_digits=4)),
                ("w_espias", models.DecimalField(decimal_places=2, default=0.20, max_digits=4)),
                ("w_fuga", models.DecimalField(decimal_places=2, default=0.15, max_digits=4)),
                ("w_huecos", models.DecimalField(decimal_places=2, default=0.10, max_digits=4)),
                ("w_financiero", models.DecimalField(decimal_places=2, default=0.10, max_digits=4)),
                ("umbral_amarillo", models.IntegerField(default=30)),
                ("umbral_naranja", models.IntegerField(default=60)),
                ("umbral_rojo", models.IntegerField(default=80)),
                ("token_stale_dias", models.IntegerField(default=7, help_text="Días sin sync de wallet/assets = token stale")),
                ("inactividad_dias", models.IntegerField(default=14, help_text="Días sin last_known_login = inactivo")),
                ("donacion_externa_min", models.BigIntegerField(default=500000000, help_text="ISK mínimo de donación externa para señal")),
                ("blue_alliance_ids", models.JSONField(blank=True, default=list)),
                ("blue_corp_ids", models.JSONField(blank=True, default=list)),
                ("own_alliance_ids", models.JSONField(blank=True, default=list)),
                ("own_corp_ids", models.JSONField(blank=True, default=list)),
                ("blue_state_ids", models.JSONField(blank=True, default=list)),
                ("own_state_ids", models.JSONField(blank=True, default=list)),
                ("staging_location_ids", models.JSONField(blank=True, default=list)),
                ("modo_calibracion", models.BooleanField(default=True)),
                ("calibracion_hasta", models.DateField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Configuración del Auditor",
                "verbose_name_plural": "Configuración del Auditor",
            },
        ),
        migrations.CreateModel(
            name="AuditorRiskScore",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("main_character_id", models.IntegerField(db_index=True)),
                ("main_character_name", models.CharField(max_length=100)),
                ("corporation_id", models.IntegerField(db_index=True, default=0)),
                ("period", models.CharField(db_index=True, max_length=7)),
                ("score_pvp", models.IntegerField(default=0)),
                ("score_ciclo", models.IntegerField(default=0)),
                ("score_espias", models.IntegerField(default=0)),
                ("score_fuga", models.IntegerField(default=0)),
                ("score_huecos", models.IntegerField(default=0)),
                ("score_financiero", models.IntegerField(default=0)),
                ("risk_total", models.IntegerField(default=0, db_index=True)),
                ("nivel", models.CharField(default="verde", max_length=10)),
                ("detalle", models.JSONField(blank=True, default=dict)),
                ("computed_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Score de riesgo (Auditor)",
                "verbose_name_plural": "Scores de riesgo (Auditor)",
            },
        ),
        migrations.CreateModel(
            name="AuditorAlert",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("main_character_id", models.IntegerField(db_index=True)),
                ("main_character_name", models.CharField(max_length=100)),
                ("period", models.CharField(db_index=True, max_length=7)),
                ("familia", models.CharField(max_length=20)),
                ("codigo", models.CharField(max_length=50)),
                ("severidad", models.CharField(default="warn", max_length=10)),
                ("titulo", models.CharField(max_length=200)),
                ("detalle", models.JSONField(blank=True, default=dict)),
                ("estado", models.CharField(db_index=True, default="abierta", max_length=15)),
                ("revisada_at", models.DateTimeField(blank=True, null=True)),
                ("nota_revision", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("revisada_por", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="auditor_alerts_revisadas", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Alerta del Auditor",
                "verbose_name_plural": "Alertas del Auditor",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="auditorriskscore",
            constraint=models.UniqueConstraint(fields=("main_character_id", "period"), name="unique_auditor_score"),
        ),
        migrations.AddIndex(
            model_name="auditorriskscore",
            index=models.Index(fields=["period", "risk_total"], name="koru_aud_period_risk"),
        ),
        migrations.AddIndex(
            model_name="auditorriskscore",
            index=models.Index(fields=["period", "corporation_id"], name="koru_aud_period_corp"),
        ),
        migrations.AddConstraint(
            model_name="auditoralert",
            constraint=models.UniqueConstraint(fields=("main_character_id", "period", "codigo"), name="unique_auditor_alert_period_code"),
        ),
        migrations.AddIndex(
            model_name="auditoralert",
            index=models.Index(fields=["estado", "severidad"], name="koru_alert_estado_sev"),
        ),
    ]

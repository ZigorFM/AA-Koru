from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("koru_stats", "0017_auditor_fase2"),
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
                    ("tickets_reclutamiento", "Tickets: ve Reclutamiento"),
                    ("tickets_directores", "Tickets: ve A Directores"),
                    ("tickets_asuntos_internos", "Tickets: ve Asuntos Internos"),
                    ("tickets_it", "Tickets: ve IT y Soporte"),
                    ("tickets_admin", "Tickets: ve todos los tipos"),
                ),
            },
        ),
        migrations.CreateModel(
            name="TicketsConfig",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tag", models.CharField(default="default", max_length=50, unique=True)),
                ("baserow_base_url", models.CharField(default="https://rekipiloto.sinzg.synology.me", max_length=255)),
                ("baserow_token", models.CharField(blank=True, default="", help_text="Token de Baserow (solo lectura)", max_length=255)),
                ("enabled", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Configuración de Tickets",
                "verbose_name_plural": "Configuración de Tickets",
            },
        ),
        migrations.CreateModel(
            name="Ticket",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tipo", models.CharField(choices=[("reclutamiento", "Reclutamiento"), ("directores", "A Directores"), ("asuntos", "Asuntos Internos"), ("it", "A IT"), ("soporte", "A Soporte")], db_index=True, max_length=20)),
                ("baserow_table_id", models.IntegerField(db_index=True)),
                ("baserow_row_id", models.IntegerField()),
                ("numero", models.CharField(blank=True, default="", max_length=30)),
                ("discord_ticket", models.CharField(blank=True, default="", max_length=30)),
                ("fecha", models.DateField(blank=True, null=True)),
                ("estado", models.CharField(blank=True, default="", max_length=50)),
                ("tipo_detalle", models.CharField(blank=True, default="", max_length=100)),
                ("asunto", models.TextField(blank=True, default="")),
                ("main_character_name", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                ("main_character_id", models.IntegerField(db_index=True, default=0)),
                ("claim_name", models.CharField(blank=True, default="", max_length=100)),
                ("alerta_peligro", models.BooleanField(default=False)),
                ("visible_piloto", models.BooleanField(default=False)),
                ("extra", models.JSONField(blank=True, default=dict)),
                ("synced_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Ticket (espejo)",
                "verbose_name_plural": "Tickets (espejo)",
                "ordering": ["-fecha", "-baserow_row_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.UniqueConstraint(fields=("baserow_table_id", "baserow_row_id"), name="unique_ticket_baserow"),
        ),
        migrations.AddIndex(
            model_name="ticket",
            index=models.Index(fields=["tipo", "estado"], name="koru_ticket_tipo_estado"),
        ),
        migrations.AddIndex(
            model_name="ticket",
            index=models.Index(fields=["main_character_id", "tipo"], name="koru_ticket_main_tipo"),
        ),
    ]

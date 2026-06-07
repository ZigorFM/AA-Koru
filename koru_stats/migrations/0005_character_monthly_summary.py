from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("koru_stats", "0004_add_perf_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="CharacterMonthlySummary",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("main_character_id",   models.IntegerField(db_index=True)),
                ("main_character_name", models.CharField(max_length=100)),
                ("corporation_id",      models.IntegerField(db_index=True, default=0)),
                ("period",              models.CharField(max_length=7, db_index=True)),
                ("mining_units",        models.BigIntegerField(default=0)),
                ("mining_m3",           models.DecimalField(max_digits=20, decimal_places=2, default=0)),
                ("mining_isk",          models.DecimalField(max_digits=20, decimal_places=2, default=0)),
                ("bounty_isk",          models.DecimalField(max_digits=20, decimal_places=2, default=0)),
                ("ess_isk",             models.DecimalField(max_digits=20, decimal_places=2, default=0)),
                ("updated_at",          models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Resumen mensual por personaje",
                "verbose_name_plural": "Resúmenes mensuales por personaje",
            },
        ),
        migrations.AddConstraint(
            model_name="charactermonthlysummary",
            constraint=models.UniqueConstraint(
                fields=["main_character_id", "period"],
                name="unique_char_monthly_summary",
            ),
        ),
        migrations.AddIndex(
            model_name="charactermonthlysummary",
            index=models.Index(fields=["period", "corporation_id"], name="koru_cms_period_corp"),
        ),
        migrations.AddIndex(
            model_name="charactermonthlysummary",
            index=models.Index(fields=["period", "mining_isk"], name="koru_cms_period_mining"),
        ),
        migrations.AddIndex(
            model_name="charactermonthlysummary",
            index=models.Index(fields=["period", "bounty_isk"], name="koru_cms_period_bounty"),
        ),

        migrations.CreateModel(
            name="CharacterMonthlyOre",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("main_character_id", models.IntegerField(db_index=True)),
                ("corporation_id",    models.IntegerField(db_index=True, default=0)),
                ("period",            models.CharField(max_length=7, db_index=True)),
                ("type_id",           models.IntegerField()),
                ("type_name",         models.CharField(max_length=100)),
                ("quantity",          models.BigIntegerField(default=0)),
                ("m3",                models.DecimalField(max_digits=20, decimal_places=2, default=0)),
                ("isk",               models.DecimalField(max_digits=20, decimal_places=2, default=0)),
                ("updated_at",        models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Ore mensual por personaje",
                "verbose_name_plural": "Ore mensuales por personaje",
            },
        ),
        migrations.AddConstraint(
            model_name="charactermonthlyore",
            constraint=models.UniqueConstraint(
                fields=["main_character_id", "period", "type_id"],
                name="unique_char_monthly_ore",
            ),
        ),
        migrations.AddIndex(
            model_name="charactermonthlyore",
            index=models.Index(
                fields=["period", "corporation_id", "type_id"],
                name="koru_cmo_period_corp_type",
            ),
        ),
    ]

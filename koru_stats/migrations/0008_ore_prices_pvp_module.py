"""
Módulo de precios ore (3 valoraciones) + módulo PvP.

Cambios:
  - CharacterMonthlySummary: +mining_isk_compressed, +mining_isk_reprocessed
  - CharacterMonthlyOre:     +isk_compressed, +isk_reprocessed
  - General permissions:     +pvp_access, +fc_access
  - NEW: OreMarketPrice
  - NEW: CharacterMonthlyPvp
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("koru_stats", "0007_restore_character_monthly_tables"),
    ]

    operations = [

        # ── Nuevos campos en CharacterMonthlySummary ────────────────────────
        migrations.AddField(
            model_name="charactermonthlysummary",
            name="mining_isk_compressed",
            field=models.DecimalField(
                max_digits=20, decimal_places=2, default=0,
                help_text="ISK si se vendiera comprimido",
            ),
        ),
        migrations.AddField(
            model_name="charactermonthlysummary",
            name="mining_isk_reprocessed",
            field=models.DecimalField(
                max_digits=20, decimal_places=2, default=0,
                help_text="ISK si se reprocesara al 85%",
            ),
        ),

        # ── Nuevos campos en CharacterMonthlyOre ────────────────────────────
        migrations.AddField(
            model_name="charactermonthlyore",
            name="isk_compressed",
            field=models.DecimalField(
                max_digits=20, decimal_places=2, default=0,
                help_text="ISK si se vendiera comprimido",
            ),
        ),
        migrations.AddField(
            model_name="charactermonthlyore",
            name="isk_reprocessed",
            field=models.DecimalField(
                max_digits=20, decimal_places=2, default=0,
                help_text="ISK si se reprocesara al 85%",
            ),
        ),

        # ── Actualizar permisos de General ──────────────────────────────────
        migrations.AlterModelOptions(
            name="general",
            options={
                "default_permissions": (),
                "managed": False,
                "permissions": [
                    ("basic_access",        "Puede ver Estadísticas y Mi Dashboard"),
                    ("corp_finance_access", "Puede ver Finanzas Corp"),
                    ("moon_tax_access",     "Puede ver Tax Lunas"),
                    ("moon_tax_admin",      "Puede gestionar tax de lunas"),
                    ("pvp_access",          "Puede ver estadísticas PvP detalladas"),
                    ("fc_access",           "Puede ver panel FC/Director"),
                ],
            },
        ),

        # ── Nuevo modelo OreMarketPrice ──────────────────────────────────────
        migrations.CreateModel(
            name="OreMarketPrice",
            fields=[
                ("id",                models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("type_id",           models.IntegerField(unique=True, db_index=True)),
                ("type_name",         models.CharField(max_length=100)),
                ("price_raw",         models.DecimalField(max_digits=20, decimal_places=4, default=0)),
                ("price_compressed",  models.DecimalField(max_digits=20, decimal_places=4, default=0)),
                ("price_reprocessed", models.DecimalField(max_digits=20, decimal_places=4, default=0)),
                ("updated_at",        models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name":        "Precio de ore",
                "verbose_name_plural": "Precios de ore",
                "ordering":            ["type_name"],
            },
        ),

        # ── Nuevo modelo CharacterMonthlyPvp ─────────────────────────────────
        migrations.CreateModel(
            name="CharacterMonthlyPvp",
            fields=[
                ("id",                  models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("main_character_id",   models.IntegerField(db_index=True)),
                ("main_character_name", models.CharField(max_length=100)),
                ("corporation_id",      models.IntegerField(db_index=True, default=0)),
                ("period",              models.CharField(max_length=7, db_index=True)),
                ("ships_destroyed",     models.IntegerField(default=0)),
                ("ships_lost",          models.IntegerField(default=0)),
                ("isk_destroyed",       models.DecimalField(max_digits=20, decimal_places=2, default=0)),
                ("isk_lost",            models.DecimalField(max_digits=20, decimal_places=2, default=0)),
                ("updated_at",          models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name":        "PvP mensual por personaje",
                "verbose_name_plural": "PvP mensuales por personaje",
            },
        ),
        migrations.AddConstraint(
            model_name="charactermonthlypvp",
            constraint=models.UniqueConstraint(
                fields=["main_character_id", "period"],
                name="unique_char_monthly_pvp",
            ),
        ),
        migrations.AddIndex(
            model_name="charactermonthlypvp",
            index=models.Index(fields=["period", "corporation_id"],  name="koru_pvp_period_corp"),
        ),
        migrations.AddIndex(
            model_name="charactermonthlypvp",
            index=models.Index(fields=["period", "isk_destroyed"],   name="koru_pvp_period_isk"),
        ),
        migrations.AddIndex(
            model_name="charactermonthlypvp",
            index=models.Index(fields=["period", "ships_destroyed"], name="koru_pvp_period_ships"),
        ),
    ]

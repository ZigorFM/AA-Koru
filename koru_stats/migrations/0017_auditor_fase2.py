from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("koru_stats", "0016_auditor"),
    ]

    operations = [
        # --- victim fields en CharacterKillRecord (awox) ---
        migrations.AddField(
            model_name="characterkillrecord",
            name="victim_corp_id",
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="characterkillrecord",
            name="victim_corp_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="characterkillrecord",
            name="victim_alliance_id",
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        # --- KoruMarketPrice ---
        migrations.CreateModel(
            name="KoruMarketPrice",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("type_id", models.IntegerField(db_index=True, unique=True)),
                ("type_name", models.CharField(blank=True, default="", max_length=255)),
                ("average_price", models.DecimalField(decimal_places=2, default=0, max_digits=22)),
                ("adjusted_price", models.DecimalField(decimal_places=2, default=0, max_digits=22)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Precio de mercado (Koru)",
                "verbose_name_plural": "Precios de mercado (Koru)",
            },
        ),
        # --- CharacterValueSnapshot ---
        migrations.CreateModel(
            name="CharacterValueSnapshot",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("character_id", models.IntegerField(db_index=True)),
                ("main_character_id", models.IntegerField(db_index=True)),
                ("snapshot_date", models.DateField(db_index=True)),
                ("asset_value", models.DecimalField(decimal_places=2, default=0, max_digits=22)),
                ("wallet_balance", models.DecimalField(decimal_places=2, default=0, max_digits=22)),
                ("item_count", models.BigIntegerField(default=0)),
            ],
            options={
                "verbose_name": "Snapshot de patrimonio",
                "verbose_name_plural": "Snapshots de patrimonio",
            },
        ),
        # --- CharacterOwnershipSnapshot ---
        migrations.CreateModel(
            name="CharacterOwnershipSnapshot",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("snapshot_date", models.DateField(db_index=True)),
                ("character_id", models.IntegerField(db_index=True)),
                ("character_name", models.CharField(default="", max_length=100)),
                ("main_character_id", models.IntegerField(default=0)),
                ("corporation_id", models.IntegerField(default=0)),
            ],
            options={
                "verbose_name": "Snapshot de ownership",
                "verbose_name_plural": "Snapshots de ownership",
            },
        ),
        # --- CharacterLifecycleEvent ---
        migrations.CreateModel(
            name="CharacterLifecycleEvent",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("character_id", models.IntegerField(db_index=True)),
                ("character_name", models.CharField(default="", max_length=100)),
                ("main_character_id", models.IntegerField(db_index=True, default=0)),
                ("evento", models.CharField(max_length=30)),
                ("fecha", models.DateTimeField(db_index=True)),
                ("estado_anterior", models.CharField(blank=True, default="", max_length=50)),
                ("estado_nuevo", models.CharField(blank=True, default="", max_length=50)),
                ("notas", models.TextField(blank=True, default="")),
            ],
            options={
                "verbose_name": "Evento de ciclo de vida",
                "verbose_name_plural": "Eventos de ciclo de vida",
                "ordering": ["-fecha"],
            },
        ),
        # --- constraints / indexes ---
        migrations.AddConstraint(
            model_name="charactervaluesnapshot",
            constraint=models.UniqueConstraint(fields=("character_id", "snapshot_date"), name="unique_value_snapshot"),
        ),
        migrations.AddIndex(
            model_name="charactervaluesnapshot",
            index=models.Index(fields=["main_character_id", "snapshot_date"], name="koru_valsnap_main_date"),
        ),
        migrations.AddConstraint(
            model_name="characterownershipsnapshot",
            constraint=models.UniqueConstraint(fields=("snapshot_date", "character_id"), name="unique_ownership_snapshot"),
        ),
        migrations.AddIndex(
            model_name="characterlifecycleevent",
            index=models.Index(fields=["main_character_id", "fecha"], name="koru_lifecycle_main"),
        ),
    ]

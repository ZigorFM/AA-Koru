from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("koru_stats", "0011_charactermonthlypvp_damage_dealt_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="CharacterKillRecord",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("main_character_id",   models.BigIntegerField(db_index=True)),
                ("main_character_name", models.CharField(default="", max_length=255)),
                ("killmail_id",         models.BigIntegerField()),
                ("period",              models.CharField(db_index=True, max_length=7)),
                ("is_loss",             models.BooleanField(default=False)),
                ("ship_type_id",        models.IntegerField(default=0)),
                ("value_isk",           models.FloatField(default=0.0)),
                ("kill_date",           models.DateField(blank=True, null=True)),
                ("final_blow",          models.BooleanField(default=False)),
                ("solo",                models.BooleanField(default=False)),
            ],
            options={
                "verbose_name": "Registro killmail",
                "verbose_name_plural": "Registros killmail",
            },
        ),
        migrations.AlterUniqueTogether(
            name="characterkillrecord",
            unique_together={("main_character_id", "killmail_id")},
        ),
        migrations.AddIndex(
            model_name="characterkillrecord",
            index=models.Index(
                fields=["main_character_id", "period", "is_loss"],
                name="koru_killrec_char_period_loss",
            ),
        ),
    ]

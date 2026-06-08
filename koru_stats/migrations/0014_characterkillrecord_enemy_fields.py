from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("koru_stats", "0013_characterkillrecord_ship_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="characterkillrecord",
            name="enemy_char_id",
            field=models.BigIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="characterkillrecord",
            name="enemy_char_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="characterkillrecord",
            name="enemy_corp_id",
            field=models.IntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="characterkillrecord",
            name="enemy_corp_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("koru_stats", "0014_characterkillrecord_enemy_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="characterkillrecord",
            name="kill_hour",
            field=models.SmallIntegerField(blank=True, null=True),
        ),
    ]

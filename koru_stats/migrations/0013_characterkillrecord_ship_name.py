from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("koru_stats", "0012_characterkillrecord"),
    ]

    operations = [
        migrations.AddField(
            model_name="characterkillrecord",
            name="ship_name",
            field=models.CharField(default="", max_length=255),
        ),
    ]

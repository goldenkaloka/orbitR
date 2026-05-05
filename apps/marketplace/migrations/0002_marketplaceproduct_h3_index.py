from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("marketplace", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="marketplaceproduct",
            name="h3_index",
            field=models.CharField(
                blank=True, db_index=True, max_length=20, null=True
            ),
        ),
    ]

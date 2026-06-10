from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0006_shop_categories'),
    ]

    operations = [
        migrations.AddField(
            model_name='buyerdemand',
            name='categories',
            field=models.ManyToManyField(blank=True, related_name='demands', to='marketplace.marketplacecategory'),
        ),
    ]

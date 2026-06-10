from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0005_shop_buyerdemand_selleroffer'),
    ]

    operations = [
        migrations.AddField(
            model_name='shop',
            name='categories',
            field=models.ManyToManyField(blank=True, related_name='shops', to='marketplace.marketplacecategory'),
        ),
    ]

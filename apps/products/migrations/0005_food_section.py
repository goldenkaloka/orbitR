from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0004_trigram_index'),
        ('restaurants', '0014_restaurantsection_sectionstaff'),
    ]

    operations = [
        migrations.AddField(
            model_name='food',
            name='section',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='foods',
                to='restaurants.restaurantsection',
            ),
        ),
    ]

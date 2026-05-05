from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0002_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='food',
            name='category',
            field=models.CharField(choices=[('POPULAR', 'Popular'), ('MAIN', 'Main'), ('SIDES', 'Sides'), ('DRINKS', 'Drinks')], db_index=True, default='MAIN', max_length=20),
        ),
    ]


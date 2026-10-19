from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('delivery', '0008_alter_driver_home_restaurant_alter_driver_home_zone'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='deliveryassignment',
            name='assigned_by',
            field=models.ForeignKey(
                blank=True,
                help_text='Actor who allocated this driver (null for system allocation)',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='driver_assignments_made',
                to='users.user',
            ),
        ),
        migrations.AddField(
            model_name='deliveryassignment',
            name='assignment_source',
            field=models.CharField(
                choices=[
                    ('admin', 'Admin'),
                    ('vendor', 'Restaurant'),
                    ('driver', 'Driver (self-claim / delegation)'),
                    ('system', 'System'),
                ],
                db_index=True,
                default='admin',
                max_length=20,
            ),
        ),
    ]
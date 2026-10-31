import uuid

import django.contrib.gis.db.models.fields
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('geo', '0002_place_aliases'),
        ('orders', '0017_preordersheet_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='DropPoint',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(db_index=True, max_length=120)),
                ('description', models.TextField(blank=True, default='')),
                ('geo_location', django.contrib.gis.db.models.fields.PointField(db_index=True, geography=True, srid=4326)),
                ('h3_index', models.CharField(blank=True, db_index=True, default='', max_length=20)),
                ('min_orders', models.PositiveIntegerField(default=3, help_text='Orders needed for the trip to pay for itself')),
                ('max_orders', models.PositiveIntegerField(default=8, help_text='Orders one rider can carry on a single trip')),
                ('contact_name', models.CharField(blank=True, default='', max_length=120)),
                ('contact_phone', models.CharField(blank=True, default='', max_length=20)),
                ('notes', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('zone', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='drop_points', to='geo.operationalzone')),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.AddIndex(
            model_name='droppoint',
            index=models.Index(fields=['is_active', 'h3_index'], name='drop_point_active_h3_idx'),
        ),
        migrations.CreateModel(
            name='DropWindow',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('label', models.CharField(default='Lunch', help_text='Shown to customers, e.g. "Lunch" or "Dinner"', max_length=60)),
                ('weekday', models.PositiveSmallIntegerField(blank=True, choices=[(0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday')], db_index=True, null=True)),
                ('start_time', models.TimeField(help_text='When the hand-over window opens')),
                ('end_time', models.TimeField(help_text='When the rider is expected to arrive')),
                ('cutoff_time', models.TimeField(help_text='Last moment a customer may join this window')),
                ('delivery_fee', models.DecimalField(decimal_places=2, default=500.0, help_text='Flat fee per order — one trip is shared by many orders', max_digits=10)),
                ('max_orders', models.PositiveIntegerField(blank=True, help_text='Orders per trip; blank uses the drop point default', null=True)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('drop_point', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='windows', to='orders.droppoint')),
            ],
            options={
                'ordering': ['drop_point__name', 'start_time'],
            },
        ),
        migrations.AddConstraint(
            model_name='dropwindow',
            constraint=models.UniqueConstraint(fields=('drop_point', 'weekday', 'start_time'), name='unique_drop_window_per_point_day'),
        ),
        migrations.AddConstraint(
            model_name='dropwindow',
            constraint=models.CheckConstraint(check=models.Q(end_time__gt=models.F('start_time')), name='drop_window_ends_after_start'),
        ),
        migrations.AddConstraint(
            model_name='dropwindow',
            constraint=models.CheckConstraint(check=models.Q(cutoff_time__lte=models.F('start_time')), name='drop_window_cutoff_before_start'),
        ),
        migrations.AddField(
            model_name='order',
            name='drop_point',
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='orders', to='orders.droppoint'),
        ),
        migrations.AddField(
            model_name='order',
            name='drop_window',
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='orders', to='orders.dropwindow'),
        ),
        migrations.AddField(
            model_name='orderpool',
            name='drop_window',
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='pools', to='orders.dropwindow'),
        ),
        migrations.AddField(
            model_name='deliverybatch',
            name='drop_window',
            field=models.ForeignKey(blank=True, db_index=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='batches', to='orders.dropwindow'),
        ),
    ]

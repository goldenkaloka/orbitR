import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0011_restaurant_preorder_slot_capacity'),
    ]

    operations = [
        migrations.CreateModel(
            name='MealPeriod',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('BREAKFAST', 'Breakfast'), ('LUNCH', 'Lunch'), ('DINNER', 'Dinner')], db_index=True, max_length=16)),
                ('label', models.CharField(blank=True, help_text='Customer-facing name; defaults to the meal kind.', max_length=64)),
                ('start_time', models.TimeField()),
                ('end_time', models.TimeField()),
                ('slot_capacity', models.PositiveIntegerField(blank=True, help_text='Pre-orders accepted per slot during this meal; blank uses the restaurant capacity, 0 disables the meal.', null=True)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('restaurant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='meal_periods', to='restaurants.restaurant')),
            ],
            options={
                'ordering': ['start_time'],
            },
        ),
        migrations.AddConstraint(
            model_name='mealperiod',
            constraint=models.UniqueConstraint(fields=('restaurant', 'kind'), name='unique_meal_period_per_kind'),
        ),
        migrations.AddConstraint(
            model_name='mealperiod',
            constraint=models.CheckConstraint(check=models.Q(('end_time__gt', models.F('start_time'))), name='meal_period_ends_after_start'),
        ),
    ]

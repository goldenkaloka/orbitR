from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('geo', '0001_initial'), # Adjusted to match geo initial migration
        ('orders', '0001_initial'),
        ('restaurants', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='PricingPolicy',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('is_active', models.BooleanField(default=False, help_text='Only one policy can be active at a time')),
                ('base_fee', models.DecimalField(decimal_places=2, default=1000.0, max_digits=10)),
                ('per_km_rate', models.DecimalField(decimal_places=2, default=500.0, max_digits=10)),
                ('service_fee', models.DecimalField(decimal_places=2, default=300.0, max_digits=10)),
                ('road_buffer_multiplier', models.FloatField(default=1.3)),
                ('rainy_season_surcharge', models.DecimalField(decimal_places=2, default=0.0, max_digits=10)),
                ('night_surcharge_multiplier', models.DecimalField(decimal_places=2, default=1.2, max_digits=4)),
                ('rush_hour_multiplier', models.DecimalField(decimal_places=2, default=1.15, max_digits=4)),
                ('fuel_price_reference', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name_plural': 'Pricing Policies',
            },
        ),
        migrations.CreateModel(
            name='DeliveryFeeLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('distance_km', models.FloatField()),
                ('estimated_road_distance_km', models.FloatField()),
                ('base_fee_used', models.DecimalField(decimal_places=2, max_digits=10)),
                ('per_km_rate_used', models.DecimalField(decimal_places=2, max_digits=10)),
                ('final_fee', models.DecimalField(decimal_places=2, max_digits=10)),
                ('adjustments', models.JSONField(default=dict)),
                ('policy_name', models.CharField(default='UNKNOWN', max_length=100)),
                ('calculated_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('order', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='fee_log', to='orders.order')),
                ('pool', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='fee_log', to='orders.orderpool')),
                ('restaurant', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='restaurants.restaurant')),
            ],
        ),
        migrations.CreateModel(
            name='ZonePricingTier',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('per_km_multiplier', models.DecimalField(decimal_places=2, default=1.0, max_digits=5)),
                ('base_fee_override', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('zone', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='pricing_tier', to='geo.operationalzone')),
            ],
        ),
    ]

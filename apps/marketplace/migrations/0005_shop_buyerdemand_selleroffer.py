# Generated manually for Shop, BuyerDemand, SellerOffer models

import django.contrib.gis.db.models.fields
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0004_trigram_index'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Shop',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True, null=True)),
                ('location', django.contrib.gis.db.models.fields.PointField(blank=True, db_index=True, geography=True, null=True, srid=4326)),
                ('h3_index', models.CharField(blank=True, db_index=True, max_length=20, null=True)),
                ('whatsapp_number', models.CharField(blank=True, max_length=20, null=True)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shops', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='BuyerDemand',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('title', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True, null=True)),
                ('location', django.contrib.gis.db.models.fields.PointField(blank=True, db_index=True, geography=True, null=True, srid=4326)),
                ('h3_index', models.CharField(blank=True, db_index=True, max_length=20, null=True)),
                ('radius_km', models.PositiveIntegerField(choices=[(1, '1 km'), (3, '3 km'), (5, '5 km'), (10, '10 km'), (20, '20 km')], default=5)),
                ('status', models.CharField(choices=[('OPEN', 'Open'), ('EXPIRED', 'Expired'), ('ACCEPTED', 'Accepted'), ('CANCELLED', 'Cancelled')], db_index=True, default='OPEN', max_length=20)),
                ('expires_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('buyer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='demands', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='SellerOffer',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('price', models.DecimalField(decimal_places=2, max_digits=12)),
                ('availability', models.BooleanField(default=True, help_text='Does the seller have the item?')),
                ('message', models.TextField(blank=True, help_text='Optional note from seller', null=True)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('ACCEPTED', 'Accepted'), ('DECLINED', 'Declined'), ('WITHDRAWN', 'Withdrawn')], db_index=True, default='PENDING', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('demand', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='offers', to='marketplace.buyerdemand')),
                ('seller', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='offers', to=settings.AUTH_USER_MODEL)),
                ('shop', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='offers', to='marketplace.shop')),
            ],
        ),
        migrations.AddField(
            model_name='buyerdemand',
            name='accepted_offer',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='marketplace.selleroffer'),
        ),
    ]

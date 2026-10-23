import uuid

import django.db.models.deletion
from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('geo', '0001_initial'),
        ('users', '0002_user_posting_tokens_balance'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserAddress',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ('label', models.CharField(max_length=40)),
                ('place_name', models.CharField(max_length=120)),
                (
                    'address_line',
                    models.CharField(blank=True, default='', max_length=255),
                ),
                (
                    'landmark',
                    models.CharField(blank=True, default='', max_length=160),
                ),
                (
                    'delivery_location',
                    gis_models.PointField(geography=True),
                ),
                (
                    'h3_index',
                    models.CharField(
                        blank=True, db_index=True, default='', max_length=20
                    ),
                ),
                ('is_default', models.BooleanField(default=False)),
                ('use_count', models.PositiveIntegerField(default=0)),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='addresses',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'zone',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='saved_addresses',
                        to='geo.operationalzone',
                    ),
                ),
            ],
            options={
                'ordering': ['-is_default', '-last_used_at', '-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='useraddress',
            index=models.Index(
                fields=['user', 'is_default'], name='users_addr_user_default_idx'
            ),
        ),
    ]

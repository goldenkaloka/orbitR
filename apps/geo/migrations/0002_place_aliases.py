# Hand-written to match apps/geo/models.py PlaceAlias + PlaceAliasContributor.

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('geo', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PlaceAlias',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('h3_index', models.CharField(db_index=True, max_length=20)),
                ('text_normalized', models.CharField(db_index=True, max_length=80)),
                ('display_text', models.CharField(max_length=120)),
                ('sample_count', models.PositiveIntegerField(default=0)),
                ('distinct_users', models.PositiveIntegerField(default=0)),
                ('confidence', models.FloatField(default=0)),
                ('status', models.CharField(choices=[('pending', 'pending'), ('approved', 'approved'), ('rejected', 'rejected')], db_index=True, default='pending', max_length=12)),
                ('first_seen', models.DateTimeField(auto_now_add=True)),
                ('last_seen', models.DateTimeField(auto_now=True)),
                ('zone', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='place_aliases', to='geo.operationalzone')),
            ],
            options={
                'verbose_name_plural': 'Place aliases',
                'ordering': ['-distinct_users', '-confidence', '-last_seen'],
            },
        ),
        migrations.CreateModel(
            name='PlaceAliasContributor',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('alias', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='contributors', to='geo.placealias')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='place_alias_contributions', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name='placealias',
            constraint=models.UniqueConstraint(fields=('h3_index', 'text_normalized'), name='uniq_place_alias_cell_text'),
        ),
        migrations.AddIndex(
            model_name='placealias',
            index=models.Index(fields=['text_normalized', 'status'], name='geo_alias_text_status_idx'),
        ),
        migrations.AddConstraint(
            model_name='placealiascontributor',
            constraint=models.UniqueConstraint(fields=('alias', 'user'), name='uniq_place_alias_contributor'),
        ),
    ]

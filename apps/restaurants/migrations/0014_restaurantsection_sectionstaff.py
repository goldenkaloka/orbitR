from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0013_sync_existing_staff_roles'),
        ('users', '0004_user_avatar'),  # User model
    ]

    operations = [
        migrations.CreateModel(
            name='RestaurantSection',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('specialization', models.CharField(blank=True, db_index=True, max_length=100)),
                ('description', models.TextField(blank=True, null=True)),
                ('image', models.ImageField(blank=True, null=True, upload_to='sections/')),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('prep_time', models.PositiveIntegerField(default=20, help_text='Preparation time in minutes')),
                ('restaurant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sections', to='restaurants.restaurant')),
            ],
            options={
                'ordering': ['sort_order', 'name'],
            },
        ),
        migrations.CreateModel(
            name='SectionStaff',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('section', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='staff', to='restaurants.restaurantsection')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='section_staff_assignments', to='users.user')),
            ],
            options={
                'unique_together': {('user', 'section')},
            },
        ),
    ]

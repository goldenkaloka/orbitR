from django.db import migrations

def sync_existing_staff_roles(apps, schema_editor):
    SectionStaff = apps.get_model('restaurants', 'SectionStaff')
    User = apps.get_model('users', 'User')

    # Find users who are associated with any SectionStaff record
    staff_user_ids = SectionStaff.objects.values_list('user_id', flat=True).distinct()
    
    # Update their role to 'vendor' if they are currently 'customer'
    updated = User.objects.filter(id__in=staff_user_ids, role='customer').update(role='vendor')
    print(f"Synced {updated} existing staff users to 'vendor' role.")

class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0012_restaurant_average_prep_time'),
    ]

    operations = [
        migrations.RunPython(sync_existing_staff_roles),
    ]

from django.db import migrations

def sync_existing_staff_roles(apps, schema_editor):
    SectionStaff = apps.get_model('restaurants', 'SectionStaff')
    Restaurant = apps.get_model('restaurants', 'Restaurant')
    User = apps.get_model('users', 'User')

    staff_user_ids = SectionStaff.objects.values_list('user_id', flat=True).distinct()
    owner_user_ids = Restaurant.objects.values_list('owner_id', flat=True).distinct()
    non_owner_staff_ids = set(staff_user_ids) - set(owner_user_ids)

    updated = User.objects.filter(id__in=non_owner_staff_ids).exclude(role='customer').update(role='customer')
    print(f"Synced {updated} staff users to 'customer' role (will be dynamically resolved as 'vendor_staff').")

class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0012_restaurant_average_prep_time'),
    ]

    operations = [
        migrations.RunPython(sync_existing_staff_roles),
    ]

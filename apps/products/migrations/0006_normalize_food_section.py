from django.db import migrations, models
import django.db.models.deletion

def migrate_foods_to_sections(apps, schema_editor):
    Food = apps.get_model('products', 'Food')
    RestaurantSection = apps.get_model('restaurants', 'RestaurantSection')
    Restaurant = apps.get_model('restaurants', 'Restaurant')

    for food in Food.objects.filter(section__isnull=True):
        restaurant_id = getattr(food, 'vendor_id', None)
        if not restaurant_id:
            continue
        
        # Get or create a default section for this restaurant
        section = RestaurantSection.objects.filter(restaurant_id=restaurant_id).first()
        if not section:
            restaurant = Restaurant.objects.get(id=restaurant_id)
            section = RestaurantSection.objects.create(
                restaurant=restaurant,
                name="General",
                description="Default section created during migration",
                is_active=True,
                sort_order=0
            )
        food.section = section
        food.save()

class Migration(migrations.Migration):
    atomic = False  # Disable transaction for the migration to allow trigger events to clear in PostgreSQL

    dependencies = [
        ('products', '0005_food_section'),
        ('restaurants', '0012_restaurant_average_prep_time'),
    ]

    operations = [
        # Populate null sections using existing vendor_id before dropping the column
        migrations.RunPython(migrate_foods_to_sections),
        
        # Remove the vendor field from Food
        migrations.RemoveField(
            model_name='food',
            name='vendor',
        ),
        
        # Alter the section field to be non-nullable and CASCADE on delete
        migrations.AlterField(
            model_name='food',
            name='section',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='foods',
                to='restaurants.restaurantsection'
            ),
        ),
    ]

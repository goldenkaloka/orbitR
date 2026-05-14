"""
Migration: Collapse to OperationalZone-only architecture.

Step 1: Add max_delivery_radius_km to Restaurant.
Step 2: Clean up orphan columns from DeliveryZone (operational_zone, boundary).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0007_delete_operationalzone_remove_restaurant_latitude_and_more'),
        ('geo', '0001_initial'),
    ]

    operations = [
        # 1. Add max_delivery_radius_km to Restaurant
        migrations.AddField(
            model_name='restaurant',
            name='max_delivery_radius_km',
            field=models.FloatField(default=5.0, help_text='Maximum delivery distance in km from the restaurant'),
        ),

        # 2. Clean up orphan columns on DeliveryZone before deleting it.
        #    DECLARE must be at the top of the DO block, not nested inside IF.
        migrations.RunSQL(
            sql="""
                DO $$
                DECLARE
                    fk_name TEXT;
                BEGIN
                    -- Drop operational_zone_id column if it exists (from migration 0004)
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'restaurants_deliveryzone'
                        AND column_name = 'operational_zone_id'
                    ) THEN
                        -- Find and drop FK constraint first
                        SELECT tc.constraint_name INTO fk_name
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON tc.constraint_name = kcu.constraint_name
                        WHERE tc.table_name = 'restaurants_deliveryzone'
                          AND kcu.column_name = 'operational_zone_id'
                          AND tc.constraint_type = 'FOREIGN KEY';

                        IF fk_name IS NOT NULL THEN
                            EXECUTE 'ALTER TABLE restaurants_deliveryzone DROP CONSTRAINT ' || fk_name;
                        END IF;

                        ALTER TABLE restaurants_deliveryzone DROP COLUMN operational_zone_id;
                    END IF;

                    -- Drop boundary column if it exists (from migration 0004)
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'restaurants_deliveryzone'
                        AND column_name = 'boundary'
                    ) THEN
                        ALTER TABLE restaurants_deliveryzone DROP COLUMN boundary;
                    END IF;
                END $$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]

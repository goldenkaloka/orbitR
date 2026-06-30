from django.db import migrations, models


def backfill_verified_order_reviews(apps, schema_editor):
    Review = apps.get_model('reviews', 'Review')
    Review.objects.filter(
        order__isnull=False,
        review_type__in=['restaurant', 'driver'],
    ).update(
        is_verified_purchase=True,
        is_visible=True,
        moderation_status='APPROVED',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0002_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='review',
            name='is_verified_purchase',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='review',
            name='is_visible',
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AddField(
            model_name='review',
            name='moderation_status',
            field=models.CharField(
                choices=[
                    ('APPROVED', 'Approved'),
                    ('PENDING', 'Pending'),
                    ('REJECTED', 'Rejected'),
                ],
                db_index=True,
                default='APPROVED',
                max_length=20,
            ),
        ),
        migrations.AddIndex(
            model_name='review',
            index=models.Index(
                fields=['restaurant', 'is_visible', 'is_verified_purchase', 'created_at'],
                name='reviews_rev_restaur_a920e5_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='review',
            index=models.Index(
                fields=['driver', 'is_visible', 'is_verified_purchase', 'created_at'],
                name='reviews_rev_driver__a81cb1_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='review',
            index=models.Index(
                fields=['marketplace_product', 'is_visible', 'is_verified_purchase', 'created_at'],
                name='reviews_rev_market_2d4e10_idx',
            ),
        ),
        migrations.RunPython(backfill_verified_order_reviews, migrations.RunPython.noop),
    ]

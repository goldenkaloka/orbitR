from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Tracks real money already sent to the restaurant at pickup.

    When non-zero, ReleasePaymentService must not pay the restaurant again at
    delivery — the food was already settled when the driver collected it.
    """

    dependencies = [
        ('orders', '0007_order_cancellation_metadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='pickup_paid_amount',
            field=models.DecimalField(default=0.00, decimal_places=2, max_digits=10),
        ),
    ]
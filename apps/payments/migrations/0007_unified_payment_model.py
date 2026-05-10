from django.db import migrations, models

def backfill_payment_targets(apps, schema_editor):
    Payment = apps.get_model('payments', 'Payment')
    for payment in Payment.objects.all():
        if payment.order_id:
            payment.payable_type = 'order'
            payment.payable_id = payment.order_id
        elif payment.pool_member_id:
            payment.payable_type = 'pool_member'
            payment.payable_id = payment.pool_member_id
        elif payment.marketplace_product_id:
            payment.payable_type = 'marketplace_product'
            payment.payable_id = payment.marketplace_product_id
        
        if payment.payable_id:
            payment.save()

class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0006_payment_pool_member'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='payable_type',
            field=models.CharField(blank=True, db_index=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='payment',
            name='payable_id',
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill_payment_targets),
        migrations.AlterUniqueTogether(
            name='payment',
            unique_together={('payable_type', 'payable_id')},
        ),
    ]

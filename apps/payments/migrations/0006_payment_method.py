from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0005_alter_payment_status_payment_payment_status_valid'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='method',
            field=models.CharField(
                choices=[('MOBILE_MONEY', 'Mobile money'), ('WALLET', 'Wallet')],
                db_index=True,
                default='MOBILE_MONEY',
                max_length=20,
            ),
        ),
    ]

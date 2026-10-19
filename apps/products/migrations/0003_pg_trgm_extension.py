from django.contrib.postgres.operations import BtreeGistExtension, CryptoExtension, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0002_initial'),
    ]

    operations = [
        TrigramExtension(),
        BtreeGistExtension(),
        CryptoExtension(),
    ]

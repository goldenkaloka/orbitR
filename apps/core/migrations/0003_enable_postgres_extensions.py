from django.db import migrations
from django.contrib.postgres.operations import TrigramExtension, UnaccentExtension

class Migration(migrations.Migration):
    dependencies = [
        ('core', '0002_initial'),
    ]
    operations = [
        TrigramExtension(),
        UnaccentExtension(),
    ]

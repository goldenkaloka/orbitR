from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    """
    Enable pg_trgm, required by the fuzzy search in `UniversalSearchView`
    (`django.contrib.postgres.search.TrigramSimilarity`). Without it the search
    endpoint raises `function similarity(character varying, unknown) does not
    exist`.
    """

    dependencies = [
        ('core', '0002_initial'),
    ]

    operations = [
        TrigramExtension(),
    ]

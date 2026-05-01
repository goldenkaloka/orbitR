import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
from django.conf import settings
app.autodiscover_tasks(lambda: settings.INSTALLED_APPS)
app.conf.imports = (
    'domain.events.tasks',
    'domain.services.dispatch_tasks',
)

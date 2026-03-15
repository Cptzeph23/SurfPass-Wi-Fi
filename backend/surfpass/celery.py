from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "surfpass.settings")

app = Celery("surfpass")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
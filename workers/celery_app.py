# Celery application instance — broker + backend wired to Redis
# Owner: Workers team
# All tasks under workers/tasks/ are auto-discovered via autodiscover_tasks.

from celery import Celery

from app.config.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "workers",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

# Discovers all @celery_app.task-decorated functions in workers/tasks/*.py.
# This registers them under their full dotted name, e.g.:
#   "workers.tasks.ingestion.ingest_meeting_task"
# which must match exactly what webhook.py passes to send_task().
celery_app.autodiscover_tasks(["workers.tasks"])

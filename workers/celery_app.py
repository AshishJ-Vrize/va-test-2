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

# Redis Cluster mode requires all keys to hash to the same slot.
# The {celery} hash tag ensures consistent slot assignment across broker + backend keys.
celery_app.conf.update(
    broker_transport_options={"global_keyprefix": "{celery}."},
    result_backend_transport_options={"global_keyprefix": "{celery}."},
)

# Import task modules AFTER celery_app is fully defined.
# Each module uses @celery_app.task — importing here registers them.
# Must be at the bottom to avoid circular imports.
from workers.tasks import ingestion  # noqa: E402, F401

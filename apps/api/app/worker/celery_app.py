"""Celery 应用配置。

启动：
  celery -A app.worker.celery_app:celery_app worker -l info
  celery -A app.worker.celery_app:celery_app beat -l info
"""

from __future__ import annotations

from celery import Celery

from app.core.config import settings


def _result_backend_url() -> str:
    """结果放到 Redis 的另一个 db，避免和 broker 的 key 混在一起。"""

    base, _, _ = settings.redis_url.rpartition("/")
    return f"{base}/1" if base else settings.redis_url


celery_app = Celery(
    "devinsight",
    broker=settings.redis_url,
    backend=_result_backend_url(),
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.default_timezone,
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    # 每 5 分钟扫描一次到期来源；真正是否抓取由各来源 poll_interval_minutes 决定
    beat_schedule={
        "dispatch-due-sources": {
            "task": "app.worker.tasks.dispatch_due_sources",
            "schedule": 300.0,
        }
    },
)

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
        },
        # 采集只拿元数据，正文要单独补。放进 beat 之后「新故事进库 → 半小时内
        # 有摘要」成为默认行为，而不是等人想起来敲 make enrich
        "enrich-hot-batch": {
            "task": "app.worker.tasks.enrich_hot_batch",
            "schedule": 1800.0,
        },
        # 热度决定抓谁。分数会随时间衰减，回访取历史最大值作为排序依据
        "refresh-hot-scores": {
            "task": "app.worker.tasks.refresh_hot_scores",
            "schedule": 21600.0,
        },
        # 判重的输入是正文指纹，跟着 enrich 一起刷新，否则新条目永远游离在簇外
        "dedup-recent": {
            "task": "app.worker.tasks.dedup_recent",
            "schedule": 3600.0,
        },
    },
)

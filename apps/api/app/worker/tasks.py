"""Celery 任务。

Celery 任务是同步函数，内部用 asyncio.run 驱动异步采集；
任务结束前释放连接池，避免跨事件循环复用 asyncpg 连接。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.core.logging import get_logger
from app.db.session import dispose_engine, session_scope
from app.services import ingestion_service
from app.worker.celery_app import celery_app

logger = get_logger(__name__)


async def _run_sync(source_id: uuid.UUID, triggered_by: str) -> dict[str, Any]:
    try:
        async with session_scope() as session:
            run = await ingestion_service.sync_source_by_id(session, source_id, triggered_by=triggered_by)
    finally:
        await dispose_engine()

    if run is None:
        return {"source_id": str(source_id), "status": "not_found"}
    return {
        "source_id": str(source_id),
        "run_id": str(run.id),
        "status": run.status,
        "fetched": run.fetched_count,
        "created": run.created_count,
        "skipped": run.skipped_count,
        "duration_ms": run.duration_ms,
        "error": run.error,
    }


@celery_app.task(name="app.worker.tasks.sync_source", bind=True)
def sync_source_task(self, source_id: str, triggered_by: str = "schedule") -> dict[str, Any]:
    """同步单个来源。"""

    result = asyncio.run(_run_sync(uuid.UUID(source_id), triggered_by))
    logger.info("task_sync_source_done", extra={"source_id": source_id, "run_id": result.get("run_id")})
    return result


async def _dispatch_due() -> dict[str, Any]:
    from sqlalchemy import func, literal_column, or_, select

    from app.db.models import Source

    statement = select(Source.id).where(
        Source.enabled.is_(True),
        or_(
            Source.last_success_at.is_(None),
            Source.last_success_at
            < func.now() - (literal_column("interval '1 minute'") * Source.poll_interval_minutes),
        ),
    )
    try:
        async with session_scope() as session:
            due_ids = [str(row) for row in (await session.execute(statement)).scalars().all()]
    finally:
        await dispose_engine()
    return {"due": due_ids}


@celery_app.task(name="app.worker.tasks.dispatch_due_sources")
def dispatch_due_sources() -> dict[str, Any]:
    """Beat 每 5 分钟调用：把到期来源逐个丢进队列。"""

    result = asyncio.run(_dispatch_due())
    for source_id in result["due"]:
        sync_source_task.delay(source_id, "schedule")
    return {"dispatched": len(result["due"])}

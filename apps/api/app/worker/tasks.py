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


# --- 阶段 3 的常驻环节 ---
#
# 这三件事都有 CLI 入口（make enrich / engagement / dedup），但只靠人手跑等于
# 系统不会自转：采集每 5 分钟自动跑，补正文却要等人想起来。结果是列表里
# 一堆 pending，而「按热度抓」这个设计承诺始终停留在命令行的使用说明书里。


async def _run_enrich(limit: int, per_source: int, concurrency: int) -> dict[str, Any]:
    from app.services import content_service

    try:
        async with session_scope() as session:
            return await content_service.run_enrich(
                session, limit=limit, per_source=per_source, concurrency=concurrency
            )
    finally:
        await dispose_engine()


@celery_app.task(name="app.worker.tasks.enrich_hot_batch")
def enrich_hot_batch(limit: int = 20, per_source: int = 5, concurrency: int = 6) -> dict[str, Any]:
    """每 30 分钟补一小批正文。

    刻意跑小批量：一次抓 400 条要十几分钟，期间占满连接池并拖慢同步任务；
    拆成 20 条一轮，新故事入库后半小时内就能拿到摘要，代价是失败条目要
    多等几个轮次才轮到——而它们本来就有退避间隔。
    """

    result = asyncio.run(_run_enrich(limit, per_source, concurrency))
    logger.info("task_enrich_hot_batch_done", extra=result)
    return result


async def _run_refresh_engagement(within_days: int, limit: int, concurrency: int) -> dict[str, int]:
    from app.services import content_service

    try:
        async with session_scope() as session:
            return await content_service.refresh_engagement(
                session, within_days=within_days, limit=limit, concurrency=concurrency
            )
    finally:
        await dispose_engine()


@celery_app.task(name="app.worker.tasks.refresh_hot_scores")
def refresh_hot_scores(within_days: int = 14, limit: int = 300, concurrency: int = 8) -> dict[str, int]:
    """每 6 小时回访一次热度。

    抓取已经改走 `topstories`，入库那一刻的 score 就是真实票数，不再是 1。
    回访仍然必要，但理由变了：**分数会衰减**。一条 400 分的故事两天后掉到 200
    是常态，而「值不值得花一次请求抓正文」看的是它曾经有多热。`_merge_engagement`
    取历史最大值，回访只会把记录往高里修，不会把热帖重新判成冷门。
    """

    result = asyncio.run(_run_refresh_engagement(within_days, limit, concurrency))
    logger.info("task_refresh_hot_scores_done", extra=result)
    return result


async def _run_dedup(within_days: int) -> dict[str, int]:
    from app.services import dedup_service

    try:
        async with session_scope() as session:
            return await dedup_service.run_dedup(session, within_days=within_days)
    finally:
        await dispose_engine()


@celery_app.task(name="app.worker.tasks.dedup_recent")
def dedup_recent(within_days: int = 14) -> dict[str, int]:
    """每小时重跑判重与聚类。

    输入是正文指纹，而指纹随 enrich 增长。不跟着刷新的话，新抓到的条目会游离在
    簇外，前端看到的「几家在报同一件事」永远是上一轮的结论——比不显示更容易误导。
    """

    result = asyncio.run(_run_dedup(within_days))
    logger.info("task_dedup_recent_done", extra=result)
    return result

"""来源相关端点：列表、详情、健康、手动触发同步。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Query, Request

from app.api.deps import SessionDep
from app.core.logging import get_logger
from app.db.session import session_scope
from app.schemas.common import envelope
from app.schemas.source import IngestionRunOut, SourceHealthLogOut, SourceHealthOut, SourceOut, SyncAccepted
from app.services import ingestion_service, source_service

router = APIRouter()
logger = get_logger(__name__)


def _to_source_out(source, last_run=None) -> SourceOut:
    payload = SourceOut.model_validate(source)
    payload.last_run = IngestionRunOut.model_validate(last_run) if last_run is not None else None
    return payload


@router.get("/sources", summary="来源列表")
async def list_sources(
    request: Request,
    session: SessionDep,
    enabled: bool | None = Query(None, description="只看启用/停用的来源"),
    region: str | None = Query(None, description="cn / global"),
    health_status: str | None = Query(None, description="unknown/healthy/degraded/failing"),
):
    sources = await source_service.list_sources(
        session, enabled=enabled, region=region, health_status=health_status
    )
    runs = await source_service.latest_runs(session)
    data = [_to_source_out(source, runs.get(source.id)) for source in sources]
    return envelope(request, data, total=len(data))


@router.get("/sources/{source_id}", summary="来源详情")
async def get_source(source_id: uuid.UUID, request: Request, session: SessionDep):
    source = await source_service.get_source(session, source_id)
    runs = await source_service.recent_runs(session, source.id, limit=1)
    return envelope(request, _to_source_out(source, runs[0] if runs else None))


@router.get("/sources/{source_id}/health", summary="来源健康详情")
async def get_source_health(source_id: uuid.UUID, request: Request, session: SessionDep):
    source = await source_service.get_source(session, source_id)
    runs = await source_service.recent_runs(session, source.id, limit=10)
    logs = await source_service.recent_health_logs(session, source.id, limit=20)
    payload = SourceHealthOut(
        source=_to_source_out(source, runs[0] if runs else None),
        recent_runs=[IngestionRunOut.model_validate(run) for run in runs],
        recent_logs=[SourceHealthLogOut.model_validate(log) for log in logs],
    )
    return envelope(request, payload.model_dump(mode="json"))


async def _inline_sync(source_id: uuid.UUID) -> None:
    """Celery broker 不可用时的降级路径：请求返回后进程内执行一次。"""

    try:
        async with session_scope() as session:
            await ingestion_service.sync_source_by_id(session, source_id, triggered_by="manual")
    except Exception:
        logger.exception("inline_sync_failed", extra={"source_id": str(source_id)})


@router.post("/admin/sources/{source_id}/sync", status_code=202, summary="手动触发同步")
async def trigger_sync(
    source_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    background: BackgroundTasks,
):
    source = await source_service.get_source(session, source_id)

    try:
        from app.worker.tasks import sync_source_task

        task = sync_source_task.delay(str(source.id), "manual")
        accepted = SyncAccepted(
            source_id=source.id,
            task_id=task.id,
            execution="queued",
            message="已提交 Celery 队列，稍后可在 /sources 健康页看到结果",
        )
    except Exception as exc:  # broker 未启动时不能让接口直接 500
        logger.warning("celery_enqueue_failed", extra={"source_id": source.source_key})
        background.add_task(_inline_sync, source.id)
        accepted = SyncAccepted(
            source_id=source.id,
            task_id=None,
            execution="inline",
            message=f"队列不可用（{type(exc).__name__}），已改为请求后进程内同步",
        )

    return envelope(request, accepted.model_dump(mode="json"))

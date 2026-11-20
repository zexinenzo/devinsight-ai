"""采集服务：把 Connector 的输出落到 sources / documents / ingestion_runs。

一次 sync 的完整语义：
1. 建 run（running）；
2. 调用 Connector 抓取解析；
3. 按 (source_id, external_id) 幂等写入，重复条目只计入 skipped，不覆盖旧数据；
4. 无论成败都回写来源健康与 health log，失败不掩盖旧数据。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import NO_FETCH_SOURCES, SourceSpec
from app.connectors.registry import get_connector
from app.core.config import settings
from app.core.errors import RateLimitedError
from app.core.logging import get_logger
from app.db.models import Document, IngestionRun, Source, SourceHealthLog
from app.utils.engagement import normalize_engagement

logger = get_logger(__name__)

# 连续失败达到该次数，来源标记为 failing，前端需要给人看得见的红灯
FAILING_THRESHOLD = 3


async def _record_success(
    session: AsyncSession,
    source: Source,
    *,
    http_status: int | None,
    latency_ms: int,
) -> None:
    now = datetime.now(UTC)
    source.last_success_at = now
    source.last_http_status = http_status
    source.consecutive_failures = 0
    source.health_status = "healthy"
    session.add(
        SourceHealthLog(
            source_id=source.id,
            checked_at=now,
            status_code=http_status,
            latency_ms=latency_ms,
            ok=True,
        )
    )


async def _record_failure(
    session: AsyncSession,
    source: Source,
    *,
    message: str,
    http_status: int | None,
    latency_ms: int,
    count_as_failure: bool = True,
) -> None:
    now = datetime.now(UTC)
    source.last_failure_at = now
    source.last_http_status = http_status
    if count_as_failure:
        source.consecutive_failures = (source.consecutive_failures or 0) + 1
        source.health_status = "failing" if source.consecutive_failures >= FAILING_THRESHOLD else "degraded"
    elif source.health_status != "failing":
        # 限流不是故障：额度耗尽恰恰说明源是好的，只是此刻不该再抓。
        # 这里不累加 consecutive_failures，否则一个配额很小的源连跑三天
        # 就会被误判成 failing，运维看到的红灯指向了错误的方向。
        source.health_status = "degraded"
    session.add(
        SourceHealthLog(
            source_id=source.id,
            checked_at=now,
            status_code=http_status,
            latency_ms=latency_ms,
            ok=False,
            error_message=message[:1000],
        )
    )


def _document_rows(source: Source, documents) -> list[dict]:
    """把 CanonicalDocument 映射为 documents 表行，并做批内去重。"""

    rows: list[dict] = []
    seen: set[str] = set()
    for document in documents:
        if document.external_id in seen:
            continue
        seen.add(document.external_id)
        rows.append(
            {
                "source_id": source.id,
                "external_id": document.external_id,
                "canonical_url": document.canonical_url,
                "original_url": document.url,
                "title": document.title,
                "summary": document.summary,
                "content_text": document.content_text,
                "language": document.language,
                "region": document.region or source.region,
                "topics": document.topics,
                "content_type": document.content_type,
                "published_at": document.published_at,
                "fetched_at": document.fetched_at,
                "content_hash": document.content_hash,
                "rights_mode": document.rights_mode,
                "quality_flags": document.quality_flags,
                "engagement_raw": document.engagement or {},
                "engagement_score": normalize_engagement(document.engagement),
                # Connector 能在内存里拿到全文时就已经算好指纹（36氪这类）。
                # 拿到指纹的条目正文来源是 feed 本身，不该再进网页抓取队列——
                # 那些页面要么是 SPA、要么直接 403，重试只是浪费请求额度。
                "simhash": document.fingerprint,
                # 抓不到的源直接标 from_feed：留在 pending 会成为一个永远不变
                # 的死状态，前端一直显示「尚未补全正文」，而它根本不会被抓。
                "content_status": document.content_status
                or ("from_feed" if source.source_key in NO_FETCH_SOURCES else "pending"),
            }
        )
    return rows


async def sync_source(
    session: AsyncSession,
    source: Source,
    *,
    triggered_by: str = "schedule",
    max_items: int | None = None,
) -> IngestionRun:
    """同步单个来源，返回本次 run 记录（已提交）。"""

    started = time.perf_counter()
    run = IngestionRun(source_id=source.id, status="running", triggered_by=triggered_by)
    session.add(run)
    await session.flush()

    spec = SourceSpec.from_source(source, max_items=max_items or settings.max_items_per_sync)

    try:
        connector = get_connector(spec)
        outcome = await connector.fetch()
    # 采集失败不应打断整批任务：任何 Connector 异常都收敛成 run.status=failed
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - started) * 1000)
        message = str(exc) or type(exc).__name__
        rate_limited = isinstance(exc, RateLimitedError)
        run.status = "failed"
        run.error = message[:1000]
        run.finished_at = datetime.now(UTC)
        run.duration_ms = duration_ms
        await _record_failure(
            session,
            source,
            message=message,
            http_status=None,
            latency_ms=duration_ms,
            count_as_failure=not rate_limited,
        )
        await session.commit()
        logger.warning(
            "sync_failed",
            extra={
                "source_id": source.source_key,
                "run_id": str(run.id),
                "duration_ms": duration_ms,
                "error": message[:300],
                "rate_limited": rate_limited,
            },
        )
        return run

    rows = _document_rows(source, outcome.documents)
    created = 0
    if rows:
        # 不带冲突目标：`(source_id, external_id)` 和
        # `(source_id, canonical_url)` 任何一个被撞都跳过。
        # 只写 external_id 会漏——同一篇文章经两条写入路径进来时
        # （feed 给 Atom id、历史回填只给 URL），external_id 不同而
        # canonical_url 相同，会各插一行。
        statement = insert(Document).values(rows).on_conflict_do_nothing().returning(Document.id)
        result = await session.execute(statement)
        created = len(result.scalars().all())

    duration_ms = int((time.perf_counter() - started) * 1000)
    run.status = "succeeded"
    # 只在本次成功时推进游标。失败分支在上面已经 return，旧游标保持不动，
    # 下一轮从同一位置重试——否则一次网络抖动就会永久跳过一批条目。
    if outcome.next_cursor is not None:
        source.sync_cursor = outcome.next_cursor
    run.fetched_count = len(outcome.documents)
    run.created_count = created
    run.skipped_count = len(rows) - created
    run.updated_count = 0
    run.http_status = outcome.http_status
    run.duration_ms = duration_ms
    run.finished_at = datetime.now(UTC)

    await _record_success(session, source, http_status=outcome.http_status, latency_ms=outcome.latency_ms)
    await session.commit()

    logger.info(
        "sync_succeeded",
        extra={
            "source_id": source.source_key,
            "run_id": str(run.id),
            "duration_ms": duration_ms,
        },
    )
    return run


async def sync_source_by_id(
    session: AsyncSession,
    source_id,
    *,
    triggered_by: str = "manual",
) -> IngestionRun | None:
    source = await session.get(Source, source_id)
    if source is None:
        return None
    return await sync_source(session, source, triggered_by=triggered_by)


async def sync_all_due_sources(session: AsyncSession, *, limit: int = 20, triggered_by: str = "schedule") -> list[IngestionRun]:
    """同步所有"到期且启用"的来源，单个失败不影响其他源。

    到期判定：从未成功过，或距上次成功已超过该来源自己的 poll_interval_minutes。
    这样高频源不会被低频源拖慢，也不会全量空转。
    """

    due = or_(
        Source.last_success_at.is_(None),
        Source.last_success_at
        < func.now() - (literal_column("interval '1 minute'") * Source.poll_interval_minutes),
    )
    statement = (
        select(Source)
        .where(Source.enabled.is_(True), due)
        .order_by(Source.priority.asc(), Source.last_success_at.asc().nulls_first())
        .limit(limit)
    )
    sources = (await session.execute(statement)).scalars().all()

    runs: list[IngestionRun] = []
    for source in sources:
        runs.append(await sync_source(session, source, triggered_by=triggered_by))
    return runs

"""来源服务：注册表维护、列表查询、健康视图。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.seeds import SOURCE_SEEDS
from app.core.errors import NotFoundError
from app.db.models import IngestionRun, Source, SourceHealthLog


async def ensure_seed_sources(session: AsyncSession) -> dict[str, int]:
    """把内置来源写入注册表。

    只补缺失的 source_key，不覆盖人工在库里改过的启停/频率，
    避免每次启动都把用户的配置冲掉。
    """

    existing_keys = set((await session.execute(select(Source.source_key))).scalars().all())
    inserted = 0
    for seed in SOURCE_SEEDS:
        if seed.source_key in existing_keys:
            continue
        session.add(
            Source(
                source_key=seed.source_key,
                name=seed.name,
                canonical_domain=seed.canonical_domain,
                source_type=seed.source_type,
                access_method=seed.access_method,
                base_url=seed.base_url,
                region=seed.region,
                language=seed.language,
                credibility_tier=seed.credibility_tier,
                enabled=seed.enabled,
                priority=seed.priority,
                poll_interval_minutes=seed.poll_interval_minutes,
                request_timeout_seconds=seed.request_timeout_seconds,
                rate_limit_per_minute=seed.rate_limit_per_minute,
                parser_version=seed.parser_version,
                robots_url=seed.robots_url,
                terms_url=seed.terms_url,
                rights_note=seed.rights_note,
            )
        )
        inserted += 1
    await session.commit()
    return {"inserted": inserted, "existing": len(existing_keys)}


async def list_sources(
    session: AsyncSession,
    *,
    enabled: bool | None = None,
    region: str | None = None,
    health_status: str | None = None,
) -> list[Source]:
    statement = select(Source).order_by(Source.priority.asc(), Source.name.asc())
    if enabled is not None:
        statement = statement.where(Source.enabled.is_(enabled))
    if region:
        statement = statement.where(Source.region == region)
    if health_status:
        statement = statement.where(Source.health_status == health_status)
    return list((await session.execute(statement)).scalars().all())


async def get_source(session: AsyncSession, source_id: uuid.UUID) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise NotFoundError(f"来源不存在：{source_id}")
    return source


async def get_source_by_key(session: AsyncSession, source_key: str) -> Source | None:
    return (await session.execute(select(Source).where(Source.source_key == source_key))).scalar_one_or_none()


async def latest_runs(session: AsyncSession) -> dict[uuid.UUID, IngestionRun]:
    """每个来源取最近一条 run，供来源列表直接展示"上次采集结果"。"""

    statement = (
        select(IngestionRun)
        .distinct(IngestionRun.source_id)
        .order_by(IngestionRun.source_id, IngestionRun.started_at.desc())
    )
    runs = (await session.execute(statement)).scalars().all()
    return {run.source_id: run for run in runs}


async def recent_runs(session: AsyncSession, source_id: uuid.UUID, *, limit: int = 10) -> list[IngestionRun]:
    statement = (
        select(IngestionRun)
        .where(IngestionRun.source_id == source_id)
        .order_by(IngestionRun.started_at.desc())
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars().all())


async def recent_health_logs(session: AsyncSession, source_id: uuid.UUID, *, limit: int = 20) -> list[SourceHealthLog]:
    statement = (
        select(SourceHealthLog)
        .where(SourceHealthLog.source_id == source_id)
        .order_by(SourceHealthLog.checked_at.desc())
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars().all())

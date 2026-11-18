"""元数据与统计端点：给前端筛选器和总览用。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import func, select, text

from app.api.deps import SessionDep
from app.db.models import Document, Source
from app.schemas.common import envelope
from app.services.classifier import TOPIC_LABELS

router = APIRouter(tags=["meta"])


@router.get("/meta/topics", summary="六大领域字典")
async def list_topics(request: Request):
    data = [{"slug": slug, "label": label} for slug, label in TOPIC_LABELS.items()]
    return envelope(request, data, total=len(data))


@router.get("/stats", summary="总览统计")
async def stats(request: Request, session: SessionDep):
    total_documents = int((await session.execute(select(func.count()).select_from(Document))).scalar_one())
    total_sources = int((await session.execute(select(func.count()).select_from(Source))).scalar_one())
    enabled_sources = int(
        (await session.execute(select(func.count()).select_from(Source).where(Source.enabled.is_(True)))).scalar_one()
    )
    healthy_sources = int(
        (
            await session.execute(
                select(func.count()).select_from(Source).where(Source.health_status == "healthy")
            )
        ).scalar_one()
    )
    new_last_24h = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Document)
                .where(Document.fetched_at >= func.now() - text("interval '24 hours'"))
            )
        ).scalar_one()
    )

    region_rows = (await session.execute(select(Document.region, func.count()).group_by(Document.region))).all()
    topic_rows = (
        await session.execute(
            text(
                "SELECT t.topic, count(*) AS c "
                "FROM documents d, jsonb_array_elements_text(d.topics) AS t(topic) "
                "GROUP BY t.topic ORDER BY c DESC"
            )
        )
    ).all()
    source_rows = (
        await session.execute(
            select(Source.source_key, Source.name, func.count(Document.id))
            .join(Document, Document.source_id == Source.id, isouter=True)
            .group_by(Source.source_key, Source.name)
            .order_by(func.count(Document.id).desc())
        )
    ).all()

    payload = {
        "documents": total_documents,
        "documents_last_24h": new_last_24h,
        "sources": {"total": total_sources, "enabled": enabled_sources, "healthy": healthy_sources},
        "by_region": [{"region": region, "count": count} for region, count in region_rows],
        "by_topic": [
            {"topic": topic, "label": TOPIC_LABELS.get(topic, topic), "count": count} for topic, count in topic_rows
        ],
        "by_source": [{"source_key": key, "name": name, "count": count} for key, name, count in source_rows],
    }
    return envelope(request, payload)

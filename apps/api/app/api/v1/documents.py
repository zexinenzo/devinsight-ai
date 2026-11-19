"""文档端点：情报流列表与详情。"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, Request

from app.api.deps import SessionDep
from app.db.models import Document, Source
from app.schemas.common import envelope
from app.schemas.document import DocumentDetail, DocumentListItem, DocumentSourceBrief
from app.services import document_service

router = APIRouter(tags=["documents"])


def to_list_item(document: Document, source: Source, cluster_size: int = 1) -> DocumentListItem:
    return DocumentListItem(
        id=document.id,
        title=document.title,
        summary=document.summary,
        canonical_url=document.canonical_url,
        original_url=document.original_url,
        language=document.language,
        region=document.region,
        topics=list(document.topics or []),
        content_type=document.content_type,
        published_at=document.published_at,
        fetched_at=document.fetched_at,
        rights_mode=document.rights_mode,
        quality_flags=list(document.quality_flags or []),
        engagement_score=document.engagement_score,
        engagement_raw=dict(document.engagement_raw or {}),
        content_status=document.content_status,
        content_error=document.content_error,
        dup_of=document.dup_of,
        cluster_id=document.cluster_id,
        cluster_size=cluster_size,
        source=DocumentSourceBrief(
            id=source.id,
            source_key=source.source_key,
            name=source.name,
            source_type=source.source_type,
            credibility_tier=source.credibility_tier,
            region=source.region,
        ),
    )


@router.get("/documents", summary="情报列表（筛选 + 游标分页）")
async def list_documents(
    request: Request,
    session: SessionDep,
    q: str | None = Query(None, description="标题/摘要关键词"),
    source: str | None = Query(None, description="来源 source_key，如 36kr_zh"),
    region: str | None = Query(None, description="cn / global"),
    language: str | None = Query(None, description="zh / en"),
    topic: str | None = Query(None, description="领域 slug，如 ai-models"),
    content_type: str | None = Query(None, description="article / paper / release"),
    published_from: datetime | None = Query(None, alias="from", description="发布时间下限（ISO 8601）"),
    published_to: datetime | None = Query(None, alias="to", description="发布时间上限（ISO 8601）"),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, description="上一页返回的 next_cursor"),
    sort: str = Query("time", pattern="^(time|engagement)$", description="time 按时间 / engagement 按热度"),
    include_reposts: bool = Query(False, description="是否显示转载副本（默认隐藏）"),
):
    page = await document_service.list_documents(
        session,
        q=q,
        source_key=source,
        region=region,
        language=language,
        topic=topic,
        content_type=content_type,
        published_from=published_from,
        published_to=published_to,
        limit=limit,
        cursor=cursor,
        sort=sort,
        include_reposts=include_reposts,
    )
    items = [
        to_list_item(document, src, page.cluster_sizes.get(document.cluster_id, 1)).model_dump(mode="json")
        for document, src in page.items
    ]
    return envelope(request, items, total=page.total, next_cursor=page.next_cursor)


@router.get("/documents/{document_id}", summary="文档详情")
async def get_document(document_id: uuid.UUID, request: Request, session: SessionDep):
    document, source = await document_service.get_document(session, document_id)
    cluster_size = await document_service.count_cluster_members(session, document.cluster_id)
    item = to_list_item(document, source, cluster_size)
    detail = DocumentDetail(
        **item.model_dump(),
        content_text=document.content_text,
        content_hash=document.content_hash,
        simhash=document.simhash,
    )
    return envelope(request, detail.model_dump(mode="json"))

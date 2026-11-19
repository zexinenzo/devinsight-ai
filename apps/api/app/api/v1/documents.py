"""文档端点：情报流列表与详情。"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, Request

from app.api.deps import SessionDep
from app.db.models import Document, Source
from app.schemas.common import envelope
from app.schemas.document import (
    DocumentDetail,
    DocumentListItem,
    DocumentMatch,
    DocumentSourceBrief,
)
from app.services import document_service

router = APIRouter(tags=["documents"])


def to_list_item(
    document: Document,
    source: Source,
    cluster_size: int = 1,
    match: DocumentMatch | None = None,
) -> DocumentListItem:
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
        # 同一事件共有几家在报；1 表示只有这一条
        cluster_size=cluster_size,
        match=match,
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
    mode: str = Query(
        "keyword",
        pattern="^(keyword|vector|hybrid)$",
        description=(
            "检索方式。keyword 匹配标题与摘要；vector 用本地模型把问句编码后比正文块向量，"
            "能命中摘要之外的内容，但只有第一页；hybrid 把两者按名次融合"
        ),
    ),
    rerank: bool | None = Query(
        None,
        description=(
            "是否用 cross-encoder 精排前 40 条候选。不传则跟随配置（有模型就排）；"
            "true 表示必须重排，模型缺失返回 503；false 表示不重排。"
            "meta.reranked 会如实反映这次结果有没有排过"
        ),
    ),
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
        search_mode=mode,
        rerank=rerank,
    )
    items = [
        to_list_item(
            document,
            src,
            page.cluster_sizes.get(document.cluster_id, 1),
            _to_schema_match(page.matches.get(document.id)),
        ).model_dump(mode="json")
        for document, src in page.items
    ]
    # 只有语义检索才有「重排过没有」这件事；关键词模式给一个恒为 False 的字段
    # 会让人误以为「本该重排却没排」
    extra = {"reranked": page.reranked} if mode in ("vector", "hybrid") else None
    return envelope(
        request, items, total=page.total, next_cursor=page.next_cursor, extra=extra
    )


def _to_schema_match(match: document_service.MatchInfo | None) -> DocumentMatch | None:
    """服务层的 MatchInfo 转成响应模型。

    两个 dataclass 字段几乎一样，但仍然分成两层：服务层不该依赖 HTTP 层的
    Pydantic 模型，否则以后改响应字段会牵动检索逻辑。
    """
    if match is None:
        return None
    return DocumentMatch(
        mode=match.mode,
        similarity=match.similarity,
        char_start=match.char_start,
        rerank_score=match.rerank_score,
    )


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

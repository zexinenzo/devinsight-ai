"""文档（情报）查询服务：筛选 + 游标分页。

排序键统一用 coalesce(published_at, fetched_at)，保证 NULL 发布时间不会破坏分页；
返回给前端的仍是原始 published_at（可能为 null），不伪造时间。
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFoundError
from app.db.models import Document, Source

MAX_LIMIT = 100


def _encode_cursor(kind: str, value, document_id: uuid.UUID) -> str:
    payload = json.dumps(
        {"k": kind, "v": value, "i": str(document_id)}, separators=(",", ":"), ensure_ascii=False
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, datetime | float, uuid.UUID]:
    """解出游标：返回 (排序类型, 排序值, id)。

    排序类型写进游标而不是从请求参数推断，因为翻页到一半切换排序时，旧游标的
    排序值含义已经变了。用旧游标值去比新排序键会静默跳过或重复一批数据——
    显式带上类型，不匹配时直接报错，比错位翻页安全。
    """

    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        kind = payload.get("k", "time")
        document_id = uuid.UUID(payload["i"])
        if kind == "eng":
            return "eng", float(payload["v"]), document_id
        return "time", datetime.fromisoformat(payload["v"]), document_id
    except Exception as exc:
        raise AppError("cursor 无效", code="invalid_cursor") from exc


@dataclass(slots=True)
class DocumentPage:
    items: list[tuple[Document, Source]]
    total: int
    next_cursor: str | None
    # 同一事件的报道家数：cluster_id -> 条数。只为当前页涉及的簇查一次，
    # 不做全表聚合——列表页只关心"这一屏里的条目有几家在报"。
    cluster_sizes: dict[uuid.UUID, int] = field(default_factory=dict)


def _sort_at():
    return func.coalesce(Document.published_at, Document.fetched_at)


def _apply_filters(
    statement: Select,
    *,
    q: str | None = None,
    source_key: str | None = None,
    region: str | None = None,
    language: str | None = None,
    topic: str | None = None,
    content_type: str | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
) -> Select:
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(or_(Document.title.ilike(pattern), Document.summary.ilike(pattern)))
    if source_key:
        statement = statement.where(Source.source_key == source_key)
    if region:
        statement = statement.where(Document.region == region)
    if language:
        statement = statement.where(Document.language == language)
    if topic:
        statement = statement.where(Document.topics.contains([topic]))
    if content_type:
        # 论文 / 发布说明 / 文章 的阅读方式差别很大，混在一起看不出来是哪种。
        # 取值来自各 Connector 的写入（article / paper / release ...），不设枚举，
        # 新增来源类型时不需要改这里。
        statement = statement.where(Document.content_type == content_type)
    if published_from:
        statement = statement.where(Document.published_at >= published_from)
    if published_to:
        statement = statement.where(Document.published_at <= published_to)
    return statement


async def list_documents(
    session: AsyncSession,
    *,
    q: str | None = None,
    source_key: str | None = None,
    region: str | None = None,
    language: str | None = None,
    topic: str | None = None,
    content_type: str | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    limit: int = 20,
    cursor: str | None = None,
    sort: str = "time",
    include_reposts: bool = False,
) -> DocumentPage:
    limit = max(1, min(limit, MAX_LIMIT))

    count_statement = _apply_filters(
        select(func.count()).select_from(Document).join(Source, Document.source_id == Source.id),
        q=q,
        source_key=source_key,
        region=region,
        language=language,
        topic=topic,
        content_type=content_type,
        published_from=published_from,
        published_to=published_to,
    )
    if not include_reposts:
        # 默认不展示转载副本。同一篇稿子被五个站转走，列表里就该是一条，
        # 而不是五条看起来不同、点进去内容一样的条目。
        count_statement = count_statement.where(Document.dup_of.is_(None))
    total = int((await session.execute(count_statement)).scalar_one())

    statement = _apply_filters(
        select(Document, Source).join(Source, Document.source_id == Source.id),
        q=q,
        source_key=source_key,
        region=region,
        language=language,
        topic=topic,
        content_type=content_type,
        published_from=published_from,
        published_to=published_to,
    )
    if not include_reposts:
        statement = statement.where(Document.dup_of.is_(None))

    if sort == "engagement":
        # nulls_last 的表达式不能直接进 tuple 比较（NULL 参与比较的结果是 NULL，
        # 行会被静默过滤掉），所以先把缺失值折成 -1，让它们稳定排在末尾。
        sort_key = func.coalesce(Document.engagement_score, -1.0)
        kind = "eng"
    else:
        sort_key = _sort_at()
        kind = "time"

    if cursor:
        cursor_kind, cursor_value, cursor_id = _decode_cursor(cursor)
        if cursor_kind != kind:
            raise AppError("排序方式与游标不匹配，请从头开始翻页", code="cursor_sort_mismatch")
        statement = statement.where(tuple_(sort_key, Document.id) < tuple_(cursor_value, cursor_id))

    statement = statement.order_by(sort_key.desc(), Document.id.desc()).limit(limit + 1)
    rows = (await session.execute(statement)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last_document = rows[-1][0]
        if kind == "eng":
            cursor_value = float(last_document.engagement_score if last_document.engagement_score is not None else -1.0)
        else:
            cursor_value = (last_document.published_at or last_document.fetched_at).isoformat()
        next_cursor = _encode_cursor(kind, cursor_value, last_document.id)

    items = [(row[0], row[1]) for row in rows]
    cluster_sizes: dict[uuid.UUID, int] = {}
    cluster_ids = {document.cluster_id for document, _ in items if document.cluster_id}
    if cluster_ids:
        size_statement = (
            select(Document.cluster_id, func.count())
            .where(Document.cluster_id.in_(cluster_ids))
            .group_by(Document.cluster_id)
        )
        cluster_sizes = {
            cluster_id: int(count) for cluster_id, count in (await session.execute(size_statement)).all()
        }

    return DocumentPage(
        items=items, total=total, next_cursor=next_cursor, cluster_sizes=cluster_sizes
    )


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> tuple[Document, Source]:
    statement = select(Document, Source).join(Source, Document.source_id == Source.id).where(Document.id == document_id)
    row = (await session.execute(statement)).first()
    if row is None:
        raise NotFoundError(f"文档不存在：{document_id}")
    return row[0], row[1]


async def count_cluster_members(session: AsyncSession, cluster_id: uuid.UUID | None) -> int:
    """同一事件有几家在报；没有簇标识时返回 1（只有这一条）。"""

    if cluster_id is None:
        return 1
    total = await session.scalar(select(func.count()).where(Document.cluster_id == cluster_id))
    return int(total or 1)

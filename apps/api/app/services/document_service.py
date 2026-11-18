"""文档（情报）查询服务：筛选 + 游标分页。

排序键统一用 coalesce(published_at, fetched_at)，保证 NULL 发布时间不会破坏分页；
返回给前端的仍是原始 published_at（可能为 null），不伪造时间。
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFoundError
from app.db.models import Document, Source

MAX_LIMIT = 100


def _encode_cursor(sort_at: datetime, document_id: uuid.UUID) -> str:
    payload = json.dumps({"t": sort_at.isoformat(), "i": str(document_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        return datetime.fromisoformat(payload["t"]), uuid.UUID(payload["i"])
    except Exception as exc:
        raise AppError("cursor 无效", code="invalid_cursor") from exc


@dataclass(slots=True)
class DocumentPage:
    items: list[tuple[Document, Source]]
    total: int
    next_cursor: str | None


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
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> DocumentPage:
    limit = max(1, min(limit, MAX_LIMIT))

    count_statement = _apply_filters(
        select(func.count()).select_from(Document).join(Source, Document.source_id == Source.id),
        q=q,
        source_key=source_key,
        region=region,
        language=language,
        topic=topic,
        published_from=published_from,
        published_to=published_to,
    )
    total = int((await session.execute(count_statement)).scalar_one())

    statement = _apply_filters(
        select(Document, Source).join(Source, Document.source_id == Source.id),
        q=q,
        source_key=source_key,
        region=region,
        language=language,
        topic=topic,
        published_from=published_from,
        published_to=published_to,
    )
    if cursor:
        cursor_at, cursor_id = _decode_cursor(cursor)
        statement = statement.where(tuple_(_sort_at(), Document.id) < tuple_(cursor_at, cursor_id))

    statement = statement.order_by(_sort_at().desc(), Document.id.desc()).limit(limit + 1)
    rows = (await session.execute(statement)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last_document = rows[-1][0]
        next_cursor = _encode_cursor(last_document.published_at or last_document.fetched_at, last_document.id)

    return DocumentPage(items=[(row[0], row[1]) for row in rows], total=total, next_cursor=next_cursor)


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> tuple[Document, Source]:
    statement = select(Document, Source).join(Source, Document.source_id == Source.id).where(Document.id == document_id)
    row = (await session.execute(statement)).first()
    if row is None:
        raise NotFoundError(f"文档不存在：{document_id}")
    return row[0], row[1]

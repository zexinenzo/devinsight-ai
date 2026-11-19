"""文档（情报）查询服务：筛选 + 游标分页。

排序键统一用 coalesce(published_at, fetched_at)，保证 NULL 发布时间不会破坏分页；
返回给前端的仍是原始 published_at（可能为 null），不伪造时间。
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Select, String, cast, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, NotFoundError
from app.db.models import Document, DocumentChunk, Source
from app.services import embedding_service
from app.services.embedding_service import EmbeddingUnavailable

MAX_LIMIT = 100
# 向量检索先取多少个候选文档再过滤：太小会在筛选条件苛刻时返回空页，
# 太大就退化成全表扫描。1431 条语料下 300 覆盖得绰绰有余。
VECTOR_CANDIDATES = 300
# RRF 的平滑常数。取 60 是检索界的经验值：它让排名靠后的结果仍然有非零贡献，
# 但不会让「第 50 名」压过「第 1 名」。
RRF_K = 60


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
class MatchInfo:
    """语义检索的命中说明。

    这里**没有 `text` 字段，也不可能有**：`document_chunks` 只存向量和字符区间，
    正文什么时候都不落库。所以前端能展示的只能是「有多相关」和「在原文的哪个位置」，
    点进去仍然跳原文——这正是这套设计的意图，而不是缺陷。
    """

    similarity: float
    char_start: int | None = None
    mode: str = "vector"


@dataclass(slots=True)
class DocumentPage:
    items: list[tuple[Document, Source]]
    total: int
    next_cursor: str | None
    # 同一事件的报道家数：cluster_id -> 条数。只为当前页涉及的簇查一次，
    # 不做全表聚合——列表页只关心"这一屏里的条目有几家在报"。
    cluster_sizes: dict[uuid.UUID, int] = field(default_factory=dict)
    # 语义检索命中说明：document_id -> 相似度与命中位置。关键词检索时为空
    matches: dict[uuid.UUID, MatchInfo] = field(default_factory=dict)


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
    search_mode: str = "keyword",
) -> DocumentPage:
    limit = max(1, min(limit, MAX_LIMIT))

    if search_mode in ("vector", "hybrid"):
        # 语义检索按相似度排序，没有「时间游标」这种东西可言：第 2 页的相似度
        # 断点和第 1 页的游标不是同一个坐标系。所以这条路不支持 cursor，
        # 与其编一个看起来能翻页、实际会漏数据的游标，不如明确只给第一页。
        if cursor:
            raise AppError("语义检索按相似度排序，不支持游标翻页", code="cursor_with_vector_mode")
        return await _semantic_page(
            session,
            q=q,
            mode=search_mode,
            source_key=source_key,
            region=region,
            language=language,
            topic=topic,
            content_type=content_type,
            published_from=published_from,
            published_to=published_to,
            limit=limit,
            include_reposts=include_reposts,
        )

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


async def _vector_hits(
    session: AsyncSession,
    *,
    query_vector: list[float],
    model: str,
    source_key: str | None,
    region: str | None,
    language: str | None,
    topic: str | None,
    content_type: str | None,
    published_from: datetime | None,
    published_to: datetime | None,
    include_reposts: bool,
    candidates: int,
) -> list[tuple[uuid.UUID, float, int]]:
    """返回 [(document_id, 余弦距离, 最佳块在原文字符起点)]，按距离升序。

    用 `DISTINCT ON (document_id)` 取每篇文档最相近的那一块，而不是
    `GROUP BY document_id + MIN(distance)`：后者能算出最小距离，却拿不到
    **是哪一块**，也就无法告诉用户命中位置。多一列 char_start 就是多一个定位点。
    """

    distance = DocumentChunk.embedding.cosine_distance(
        cast(
            literal(embedding_service.to_vector_literal(query_vector), String),
            Vector(settings.embedding_dimensions),
        )
    )
    best_chunk = (
        select(
            DocumentChunk.document_id.label("document_id"),
            distance.label("distance"),
            DocumentChunk.char_start.label("char_start"),
        )
        .where(DocumentChunk.model == model)
        .distinct(DocumentChunk.document_id)
        .order_by(DocumentChunk.document_id, distance)
        .subquery()
    )

    statement = _apply_filters(
        select(best_chunk.c.document_id, best_chunk.c.distance, best_chunk.c.char_start)
        .join(Document, Document.id == best_chunk.c.document_id)
        .join(Source, Source.id == Document.source_id),
        # q 不传给 _apply_filters：语义检索不吃 ILIKE，传进去会把向量召回
        # 再按字面匹配过滤一遍，等于白算
        q=None,
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

    rows = (
        await session.execute(statement.order_by(best_chunk.c.distance).limit(candidates))
    ).all()
    return [(row[0], float(row[1]), int(row[2])) for row in rows]


async def _keyword_hits(
    session: AsyncSession,
    *,
    q: str,
    source_key: str | None,
    region: str | None,
    language: str | None,
    topic: str | None,
    content_type: str | None,
    published_from: datetime | None,
    published_to: datetime | None,
    include_reposts: bool,
    limit: int,
) -> list[uuid.UUID]:
    statement = _apply_filters(
        select(Document.id).join(Source, Document.source_id == Source.id),
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
    rows = (await session.execute(statement.order_by(_sort_at().desc()).limit(limit))).all()
    return [row[0] for row in rows]


def _reciprocal_rank_fusion(*ranked_lists: list[uuid.UUID]) -> list[uuid.UUID]:
    """RRF：只用名次、不用分数，把两个不同量纲的结果合成一个顺序。

    关键词检索的分数是「ILIKE 命中与否」，向量检索的分数是余弦距离，
    两者没有可比性——直接加权相加需要调一个没人调得准的系数。RRF 绕开这个问题：
    第 n 名贡献 1/(k+n)，只依赖名次。

    代价是丢掉了「一个特别强、一个特别弱」的区分度。对这个项目够用：
    真正的排序质量由后面的重排解决，这里只需要把两个召回源并起来。
    """

    scores: dict[uuid.UUID, float] = {}
    for ranked in ranked_lists:
        for position, document_id in enumerate(ranked):
            scores[document_id] = scores.get(document_id, 0.0) + 1.0 / (RRF_K + position + 1)
    return [document_id for document_id, _ in sorted(scores.items(), key=lambda item: -item[1])]


async def _semantic_page(
    session: AsyncSession,
    *,
    q: str | None,
    mode: str,
    source_key: str | None,
    region: str | None,
    language: str | None,
    topic: str | None,
    content_type: str | None,
    published_from: datetime | None,
    published_to: datetime | None,
    limit: int,
    include_reposts: bool,
) -> DocumentPage:
    """向量 / 混合检索。返回结构复用列表页，额外带上命中说明。"""

    query_text = (q or "").strip()
    if not query_text:
        raise AppError("语义检索需要一个问句或关键词", code="missing_query")

    try:
        embedder = embedding_service.get_embedder()
    except EmbeddingUnavailable as error:
        # 明确报错，不退回关键词：用户选了向量检索却拿到词面结果，
        # 会以为「语义检索怎么这么差」，实际是模型没装好
        raise AppError(
            "向量检索不可用：本地模型未就绪。" + str(error).splitlines()[0],
            code="embedding_unavailable",
            status_code=503,
        ) from error

    query_vector = await asyncio.to_thread(embedder.embed_query, query_text)
    hits = await _vector_hits(
        session,
        query_vector=query_vector,
        model=embedder.name,
        source_key=source_key,
        region=region,
        language=language,
        topic=topic,
        content_type=content_type,
        published_from=published_from,
        published_to=published_to,
        include_reposts=include_reposts,
        candidates=VECTOR_CANDIDATES,
    )
    distances = {document_id: distance for document_id, distance, _ in hits}
    starts = {document_id: char_start for document_id, _, char_start in hits}
    ordered = [document_id for document_id, _, _ in hits]

    if mode == "hybrid":
        keyword_ids = await _keyword_hits(
            session,
            q=query_text,
            source_key=source_key,
            region=region,
            language=language,
            topic=topic,
            content_type=content_type,
            published_from=published_from,
            published_to=published_to,
            include_reposts=include_reposts,
            limit=limit * 2,
        )
        ordered = _reciprocal_rank_fusion(ordered, keyword_ids)

    page_ids = ordered[:limit]
    if not page_ids:
        return DocumentPage(items=[], total=0, next_cursor=None)

    rows = (
        await session.execute(
            select(Document, Source)
            .join(Source, Document.source_id == Source.id)
            .where(Document.id.in_(page_ids))
        )
    ).all()
    by_id = {document.id: (document, source) for document, source in rows}
    items = [by_id[document_id] for document_id in page_ids if document_id in by_id]

    matches: dict[uuid.UUID, MatchInfo] = {}
    # items 的元素是 (Document, Source)，不是 (id, ...)。这里解包成 document 更直白，
    # 曾经写成 `for document_id, _ in items` 就是把 Document 当成了 id，
    # 字典能存能取、不报错，但调用方按 UUID 查永远是空的。
    for document, _ in items:
        distance = distances.get(document.id)
        matches[document.id] = MatchInfo(
            similarity=None if distance is None else 1.0 - distance,
            char_start=starts.get(document.id),
            mode=mode,
        )

    cluster_sizes = await _cluster_sizes(session, items)
    return DocumentPage(
        items=items,
        # 语义检索不做全表计数：总数对排序没有意义，还得多扫一遍
        total=len(items),
        next_cursor=None,
        cluster_sizes=cluster_sizes,
        matches=matches,
    )


async def _cluster_sizes(
    session: AsyncSession, items: list[tuple[Document, Source]]
) -> dict[uuid.UUID, int]:
    cluster_ids = {document.cluster_id for document, _ in items if document.cluster_id}
    if not cluster_ids:
        return {}
    size_statement = (
        select(Document.cluster_id, func.count())
        .where(Document.cluster_id.in_(cluster_ids))
        .group_by(Document.cluster_id)
    )
    return {cluster_id: int(count) for cluster_id, count in (await session.execute(size_statement)).all()}

"""文档（情报）查询服务：筛选 + 游标分页。

排序键统一用 coalesce(published_at, fetched_at)，保证 NULL 发布时间不会破坏分页；
返回给前端的仍是原始 published_at（可能为 null），不伪造时间。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Select, String, case, cast, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, NotFoundError
from app.core.logging import get_logger
from app.db.models import Document, DocumentChunk, Source
from app.services import embedding_service, rerank_service
from app.services.embedding_service import EmbeddingUnavailable
from app.services.rerank_service import RerankUnavailable
from app.services.vector_index_service import HEADER_CHUNK_INDEX

logger = get_logger(__name__)

MAX_LIMIT = 100
# 向量检索先取多少个候选文档再过滤：太小会在筛选条件苛刻时返回空页，
# 太大就退化成全表扫描。1431 条语料下 300 覆盖得绰绰有余。
VECTOR_CANDIDATES = 300
# RRF 的平滑常数。取 60 是检索界的经验值：它让排名靠后的结果仍然有非零贡献，
# 但不会让「第 50 名」压过「第 1 名」。
RRF_K = 60
# 文档级聚合取前几个块的平均距离。3 是在评测集上调出来的：取 1 就退化成单块信号，
# 取 5 会把「只有开头提到一句」的长文拉得太低。5 个块约占一篇周刊的三分之一。
DOC_AGG_TOP_N = 3

# 中文没有空格，整句 ILIKE 对自然语言问句是结构性失效：用户打一句
# 「GPT-3 一共有多少参数」，这句话不可能整体出现在任何标题里，注定 0 结果。
# 评测集上 keyword 模式的 fact / topic / en 三类得分全是 0.000，就是这个原因，
# 与「正文没有索引」无关——那条只解释短句为什么也搜不到。
#
# 兜底方案是把问句切成片段做 OR 匹配：英文按词、中文按相邻两字。
# 这些片段是纯虚词组合，命中它们不说明任何相关性，先剔掉。
_STOP_BIGRAMS = frozenset(
    {
        "一个", "这个", "那个", "我们", "你们", "他们", "就是", "不是", "可以", "什么",
        "怎么", "如何", "多少", "有没", "没有", "这些", "那些", "因为", "所以", "但是",
        "如果", "已经", "现在", "时候", "之间", "以及", "或者", "而且", "然后", "还是",
    }
)
_EN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#_-]*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _keyword_terms(q: str, *, max_terms: int = 12) -> list[str]:
    """把一段自然语言切成可用于 ILIKE 的片段。

    截断规则不是「取前 12 个」——那样会把句尾的关键词全部丢掉。实测：
    「27 寸的 4K 显示器一个月大概要用掉多少度电」按顺序取前 12 个，
    恰好把最关键的「度电」切掉了，而它才是区分这台显示器和别的显示器的词。
    改成先留英文/数字词，剩余名额在同长度的中文二元组上**均匀采样**，
    保证片段覆盖整句话而不是只覆盖开头。
    """

    english: list[str] = []
    for match in _EN_TOKEN_RE.finditer(q):
        token = match.group(0)
        if len(token) >= 2:
            english.append(token)

    cjk: list[str] = []
    for match in _CJK_RUN_RE.finditer(q):
        run = match.group(0)
        for position in range(len(run) - 1):
            bigram = run[position : position + 2]
            if bigram not in _STOP_BIGRAMS:
                cjk.append(bigram)

    # 英文词最多占一半名额：它们是完整单词，区分度高，但也要给中文留位置
    english = english[: max(1, max_terms // 2)]
    budget = max_terms - len(english)
    if len(cjk) > budget > 1:
        # 采样点包含首尾两个端点，否则句尾的二元组永远取不到
        last = len(cjk) - 1
        cjk = [cjk[round(index * last / (budget - 1))] for index in range(budget)]
    elif len(cjk) > budget == 1:
        cjk = cjk[-1:]

    seen: set[str] = set()
    ordered: list[str] = []
    for term in [*english, *cjk]:
        if term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered[:max_terms]


def _keyword_condition(q: str, *, fallback: bool = True):
    """整句优先，片段兜底。

    整句命中的精确度最高，保留它意味着短词查询（「移动支付」）的行为不变。
    片段匹配只在整句没有结果时才有机会被选中——注意这是同一条 SQL 里的 OR，
    不是两次查询：命中数阈值定在 `max(2, 片段数 × 0.4)`，只有同时命中
    足够多片段的文档才算相关，避免「搜一句话返回全库」。

    **`fallback=False` 给 hybrid 用。** 这是个踩过的坑：片段兜底让 keyword 模式
    自己的 R@5 从 0.100 涨到 0.460，但把它接进 RRF 之后，hybrid 的英文类反而从
    0.744 掉到 0.522——片段匹配对英文问句会召回大量「含这个词但不是这个意思」的
    文档，RRF 只看名次不看质量，噪音直接把向量结果挤下去。
    职责要分清：片段兜底是给纯关键词模式用的（用户只打关键词时不该 0 结果），
    hybrid 里关键词那一路只需要负责精确串（人名、版本号、专有名词），其余交给向量。
    """

    statement_q = q.strip()
    exact = or_(
        Document.title.ilike(f"%{statement_q}%"),
        Document.summary.ilike(f"%{statement_q}%"),
    )
    if not fallback:
        return exact
    terms = _keyword_terms(statement_q)
    if len(terms) < 2:
        return exact

    hits = None
    for term in terms:
        one = case((Document.title.ilike(f"%{term}%"), 1), else_=0) + case(
            (Document.summary.ilike(f"%{term}%"), 1), else_=0
        )
        hits = one if hits is None else hits + one
    threshold = max(2, int(len(terms) * 0.4))
    return or_(exact, hits >= threshold)


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
    # cross-encoder 给的原始 logit，越大越相关。只用于解释「为什么排在这个位置」，
    # 不要当概率看：int8 量化会改变它的绝对量级，跨模型、跨语料都不可比。
    rerank_score: float | None = None


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
    # 这次结果有没有经过 cross-encoder 精排。显式给出来，是因为「没重排」和
    # 「重排了但这段分数不高」在调用方看来都是 `rerank_score is None`，
    # 不标记的话，一个降级请求看起来和正常请求一模一样。
    reranked: bool = False


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
    q_fallback: bool = True,
) -> Select:
    if q and q.strip():
        statement = statement.where(_keyword_condition(q, fallback=q_fallback))
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
    rerank: bool | None = None,
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
            rerank=rerank,
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
        # 只看正文块。标题摘要块（chunk_index = -1）走 `_header_vector_hits` 独立成路：
        # 它的 char_start 恒为 0，混进来会让「命中位置」指向标题而不是正文，
        # 而这正是定位型查询唯一依赖的东西。
        .where(DocumentChunk.model == model, DocumentChunk.chunk_index >= 0)
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


async def _document_vector_hits(
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
    limit: int,
) -> list[uuid.UUID]:
    """按文档聚合前 N 块的平均距离，返回文档 ID 顺序。

    **为什么需要这一路信号。** `_vector_hits` 取的是每篇文档**最相近的那一块**，
    这对"定位型"问题是优点（"GPT-3 有多少参数"只要有一块讲到就够），但对
    "主题型"问题是噪声：一篇顺带提到该主题的文章，只要某一块恰好和问题很像，
    就能顶掉整篇都在讲这个主题的周刊。

    评测集量出来的数字印证了这一点——单块信号在 fact 类上是 0.923，
    在 topic 类上只有 0.465。差别不在模型，在聚合方式。

    取前 3 块的平均值，等于要求"这篇文章有多处都在讲这件事"。
    代价是定位型会略降（只有一个块相关时被其他块拉低），所以它**不替代**
    单块信号，只作为 hybrid 里的一路独立信号参与 RRF——两者的偏好正好互补：
    单块管"精准命中一处"，聚合管"整篇都在讲"。
    """

    distance = DocumentChunk.embedding.cosine_distance(
        cast(
            literal(embedding_service.to_vector_literal(query_vector), String),
            Vector(settings.embedding_dimensions),
        )
    )
    # 距离先在子查询里算一次，外层只引用列。写成 row_number() over (order by <distance 表达式>)
    # 会让 Postgres 对每一行重算余弦距离——同一行算两遍，白烧一次 CPU。
    scored = (
        select(DocumentChunk.document_id.label("document_id"), distance.label("distance"))
        # 同 `_vector_hits`：正文块单独成路，标题块不参与「前 N 块平均」，
        # 否则 3 块里混进一条标题，聚合的含义就从「多处都在讲」变成「标题 + 两处」
        .where(DocumentChunk.model == model, DocumentChunk.chunk_index >= 0)
        .subquery()
    )
    ranked = (
        select(
            scored.c.document_id,
            scored.c.distance,
            func.row_number()
            .over(partition_by=scored.c.document_id, order_by=scored.c.distance)
            .label("rn"),
        )
        .subquery()
    )
    aggregated = (
        select(ranked.c.document_id, func.avg(ranked.c.distance).label("distance"))
        .where(ranked.c.rn <= DOC_AGG_TOP_N)
        .group_by(ranked.c.document_id)
        .subquery()
    )

    statement = _apply_filters(
        select(aggregated.c.document_id)
        .join(Document, Document.id == aggregated.c.document_id)
        .join(Source, Source.id == Document.source_id),
        # 与 _vector_hits 同理：语义检索不吃 ILIKE
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

    statement = statement.order_by(aggregated.c.distance).limit(limit)
    rows = (await session.execute(statement)).all()
    return [row[0] for row in rows]


async def _header_vector_hits(
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
    limit: int,
) -> list[uuid.UUID]:
    """第四路：只拿标题摘要块（`chunk_index = -1`）去比。

    **为什么前三路不够。** 我在评测集上量过一个更基础的事实：topic 类 24 篇
    标准答案里有 10 篇（42%）**根本不在前 50 名内**。也就是说那一档的问题
    主要不是「排得不准」，是「没召回来」——精排再强也救不了池外的文档。

    池外的合理推测是：一篇 9000 字周刊切成 20 多块，每块都在讲一件具体的事，
    「这篇整体在讲开源生态」这个判断在任何单块上都体现不出来。标题和摘要才是
    它唯一的载体，所以单独索引、单独检索，作为独立一路参与融合。
    """

    distance = DocumentChunk.embedding.cosine_distance(
        cast(
            literal(embedding_service.to_vector_literal(query_vector), String),
            Vector(settings.embedding_dimensions),
        )
    )
    statement = _apply_filters(
        select(DocumentChunk.document_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(Source, Source.id == Document.source_id),
        q=None,
        source_key=source_key,
        region=region,
        language=language,
        topic=topic,
        content_type=content_type,
        published_from=published_from,
        published_to=published_to,
    ).where(DocumentChunk.model == model, DocumentChunk.chunk_index == HEADER_CHUNK_INDEX)
    if not include_reposts:
        statement = statement.where(Document.dup_of.is_(None))

    rows = (await session.execute(statement.order_by(distance).limit(limit))).all()
    return [row[0] for row in rows]


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
        # hybrid 的关键词那一路只取精确串：片段兜底会带来语义无关的噪音，
        # 而 RRF 只看名次，压不住它。自然语言问句交给向量那一路。
        q_fallback=False,
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


# 重排时喂给模型的文本预算。交叉编码器上限 512 token，中文约 1 字 1 token，
# 留出标题和特殊符号的余量后，三段合计控制在 460 字以内：
# 摘要 140 + 片段 260 + 标题最多 60。
# 超了会被截尾，而被截掉的恰好是片段的后半段——那往往才是答案所在的位置。
RERANK_SUMMARY_CHARS = 140
RERANK_SNIPPET_CHARS = 260


def _rerank_passage(title: str | None, summary: str | None, snippet: str | None) -> str:
    """拼出喂给交叉编码器的一段文本：标题 + 摘要 + 命中块原文。

    **三段都要给，缺哪段就掉哪类。** 这是两次实测逼出来的：

    - 只给「标题 + 摘要」：fact 类 R@5 从 1.000 掉到 0.923。因为「GPT-3 有多少
      参数」的答案在正文深处，摘要里没有这个数字，模型只能凭标题像不像瞎猜，
      反而把原本精准命中、排第 1 的文档压了下去。
    - 只给「标题 + 命中片段」：topic 类 R@5 从 0.625 掉到 0.507。因为片段是
      「某一处提到这个主题」的细节，恰恰丢失了「这篇整体在讲什么」。

    两类查询要的证据不在同一个地方，所以两段都放，让模型的注意力自己去权衡——
    这正是交叉编码器比向量点积强的地方，它有能力读懂两段之间的关系。

    `snippet` 来自 `documents.content_text` 按 `char_start` 切出的区间。不需要
    额外存储：正文本来就在库里（受 rights_mode 限制，全文或 600 字片段），
    `document_chunks` 不存原文只是为了避免重复存同一份正文，不是拿不到它。
    """

    head = (title or "").strip()[:60]
    summary_text = " ".join((summary or "").split())[:RERANK_SUMMARY_CHARS]
    snippet_text = " ".join((snippet or "").split())[:RERANK_SNIPPET_CHARS]
    parts = [part for part in (head, summary_text, snippet_text) if part]
    return "\n".join(parts)


async def _rerank_ordered(
    session: AsyncSession,
    *,
    query: str,
    ordered: list[uuid.UUID],
    candidates: int,
    starts: dict[uuid.UUID, int | None] | None = None,
) -> tuple[list[uuid.UUID], dict[uuid.UUID, float]]:
    """用 cross-encoder 重排前 N 条候选，返回 (按分数排的名次, 分数表)。

    只重排前 N 条：交叉编码器不能预计算，每篇候选每次查询都要过一次模型，
    全量重排会让一个查询变成几秒的等待。前 40 条覆盖了「本来能排进前 10」
    的全部可能，再往后的文档即使分数反超也不会进页面。
    """

    head = ordered[:candidates]
    if len(head) < 2:
        return ordered, {}

    # 取正文是为了按 char_start 切出命中那块。40 篇 × 最多 9000 字 ≈ 360 KB，
    # 一次查询的量级，比在 SQL 里为 40 个不同偏移拼 40 个 CASE 干净得多。
    rows = (
        await session.execute(
            select(Document.id, Document.title, Document.summary, Document.content_text).where(
                Document.id.in_(head)
            )
        )
    ).all()
    offsets = starts or {}
    passages: dict[uuid.UUID, str] = {}
    for document_id, title, summary, body in rows:
        snippet = None
        start = offsets.get(document_id)
        if body and start is not None and 0 <= start < len(body):
            snippet = body[start : start + RERANK_SNIPPET_CHARS]
        passages[document_id] = _rerank_passage(title, summary, snippet)

    reranker = rerank_service.get_reranker()
    texts = [passages.get(document_id) or " " for document_id in head]
    scores = await asyncio.to_thread(reranker.score, query, texts)

    if len(scores) != len(head):
        # 数量对不上时排序会错位，宁可放弃重排也不要把分数贴到错的文档上
        logger.warning(
            "rerank_score_count_mismatch", extra={"scores": len(scores), "head": len(head)}
        )
        return ordered, {}

    ranked = sorted(zip(head, scores, strict=True), key=lambda item: -item[1])
    # 只返回重排过的那 N 条。调用方要拿它作为 RRF 的一路投票——如果把后面
    # 没重排的 260 条一并带上，那 260 条的顺序就是原顺序，等于给原顺序又投了一票，
    # 把自己这一路的声音稀释掉了。
    return [document_id for document_id, _ in ranked], {
        document_id: float(score) for document_id, score in ranked
    }


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
    rerank: bool | None = None,
) -> DocumentPage:
    """向量 / 混合检索。返回结构复用列表页，额外带上命中说明。

    `rerank` 三态，含义必须分清楚，否则「重排没生效」会被当成「重排没用」：

    - `True`：必须重排。模型缺失直接 503，不拿未重排的结果充数。
    - `False`：不重排，直接给 RRF 顺序。
    - `None`（默认）：跟随配置。有模型就排，没有就跳过，但 `reranked` 会如实标记
      为 False，调用方能看出这次结果没经过精排。
    """

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
        # 第三路：文档级聚合。单块信号认「某一处很像」，聚合信号认「整篇都在讲」，
        # 评测集上两者在 fact / topic 两类的偏好正好互补，所以都参与融合而不是二选一。
        aggregate_ids = await _document_vector_hits(
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
            limit=VECTOR_CANDIDATES,
        )
        # 第四路：标题摘要块。前三路全部基于正文块，噪音来源相同；这一路换了个视角
        # （这篇整体在讲什么），对池外文档是唯一有机会把它们捞回来的信号。
        header_ids = await _header_vector_hits(
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
            limit=VECTOR_CANDIDATES,
        )
        ordered = _reciprocal_rank_fusion(ordered, keyword_ids, aggregate_ids, header_ids)

    # 重排放在所有召回之后、切页之前：它要改的是「前 40 条谁先谁后」，
    # 而切页只取前 limit 条——顺序必须在切之前定下来。
    reranked = False
    rerank_scores: dict[uuid.UUID, float] = {}
    if rerank is not False and len(ordered) > 1:
        try:
            rerank_ranked, rerank_scores = await _rerank_ordered(
                session,
                query=query_text,
                ordered=ordered,
                candidates=settings.rerank_candidates,
                starts=starts,
            )
            if rerank_scores:
                # 不直接覆盖顺序，而是作为第五路参与 RRF 投票。
                #
                # 这个选择是评测逼出来的：第一版让重排结果直接接管排序，结果
                # 原本排第 1–3 的正确答案被压到 6–11 名（t06 1→9、t09 2→10、
                # t03 3→11），topic R@5 从 0.625 掉到 0.528。原因是单块片段对
                # 「整篇在讲什么」的判断不可靠，而它一旦判错就全错。
                # 让它在 5 路里占 1 票，判错时会被其他 4 路投回来。
                ordered = _reciprocal_rank_fusion(ordered, rerank_ranked)
                reranked = True
        except RerankUnavailable as error:
            if rerank:
                # 显式要求却给不了，必须报错。否则「我明明开了重排」和
                # 「重排根本没生效」在调用方看来一模一样。
                raise AppError(
                    "重排不可用：本地模型未就绪。" + str(error).splitlines()[0],
                    code="rerank_unavailable",
                    status_code=503,
                ) from error
            # 默认模式下跳过，但结果里 reranked=False 会如实反映这件事
            logger.warning("rerank_skipped", extra={"reason": str(error).splitlines()[0]})

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
            rerank_score=rerank_scores.get(document.id),
        )

    cluster_sizes = await _cluster_sizes(session, items)
    return DocumentPage(
        items=items,
        # 语义检索不做全表计数：总数对排序没有意义，还得多扫一遍
        total=len(items),
        next_cursor=None,
        cluster_sizes=cluster_sizes,
        matches=matches,
        reranked=reranked,
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

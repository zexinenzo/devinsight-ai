"""去重与事件聚类：把「转载」和「同一件事的多家报道」分开处理。

这是阶段 3 最容易混成一件事的地方，而它们的处理方式相反：

- **同一文章多站转载**：正文高度重合，只是标题被改、加了站点水印。
  处理是把它们**合并成一条**，保留最早的原始来源，其余标记 `dup_of`。
- **同一事件多源报道**：各家自己写的，正文不同但讲的是同一件事。
  处理是**聚成一个事件簇**，每个源保留独立条目——删掉任何一条都是在丢信息。

所以用两套判据，互不替代：

| 判据 | 输入 | 阈值 | 结论 |
|---|---|---|---|
| SimHash 汉明距离 | 正文 | ≤ 10 / 64 位 | 转载，合并 |
| 标题 token Jaccard | 标题 | ≥ 0.35 且 48h 内、跨源 | 同事件，聚簇 |

**为什么不做 LSH 分桶**：库里的文档量级是几百条，两两比较是一次十几万次整数异或，
毫秒级完成。分桶是为了十万级以上的规模，现在引入只会增加一层出错的地方。
规模上来再换，接口不用变。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Document
from app.utils.simhash import hamming, tokenize

logger = get_logger(__name__)

# 转载判定：64 位指纹的汉明距离上限。
#
# 教科书上常写 3，但那是大规模英文语料的经验值。本项目实测（172 token 的中文正文，
# 转载时加前缀标题与尾注、改动约 20 个 bigram）距离是 8，而无关联的两篇中文文章
# 距离在 34 以上。所以取 10：
#
#   转载 8 ──── 10 ──── 20 ──── 34 无关文章
#
# 定在 3 会把真实转载全部漏掉（这是实测撞出来的，不是猜的）；定得太高则会把
# 「同一事件的两篇独立报道」误判成转载——那是信息损失，比漏合并严重得多。
REPOST_HAMMING = 10

# 事件聚类：标题 token 重叠率达到该值且发布时间在窗口内，视为同一事件。
#
# 阈值来自实测分布，不是拍脑袋：
#
#   同一事件不同措辞（三家媒体报 GPT-5 发布）  Jaccard 0.44 ~ 0.67
#   同领域无关事件（GPT-5 发布 / 英伟达发显卡）Jaccard 0.07
#
# 空档在 0.07 与 0.44 之间，取 0.35：离两边都有余量。
# 最初按教科书设 0.45，结果同一事件的三条里有一条卡在 0.438 掉队——
# 中文 bigram 对措辞变化比英文词更敏感，英文语料的经验值不能直接搬。
EVENT_JACCARD = 0.35
EVENT_WINDOW_HOURS = 48
# 少于该 token 数的标题（"New release"）不足以判断是否同事件
MIN_TITLE_TOKENS = 3

# 不参与事件聚类的类型。
#
# 这是实测撞出来的：GitHub Release 的标题形如 `ollama/ollama v0.40.9`，
# 相邻版本之间只差最后一位数字，标题 token 的 Jaccard 高达 0.9 以上，
# 于是 `v0.40.8` 到 `v0.40.11` 被聚成了「一起事件」——但每个版本是独立发布，
# 不是四家媒体在报道同一件事。
#
# 根因是「事件」这个概念对版本说明不成立：版本之间天然高度相似，而相似度正是
# 聚类的判据。所以不是调阈值能解决的（阈值调到 0.9 以上，真实的同事件报道又全漏了）。
EXCLUDED_CONTENT_TYPES = ("release",)


def _published(document: Document) -> datetime:
    """聚类与判重的排序基准：优先发布时间，缺失时退回采集时间。

    发布时间缺失是常见情况（HN 部分条目、解析失败的 RSS），这时用采集时间做
    近似是唯一选择——但要知道这个近似会让跨源比较产生几小时的误差。
    """

    return document.published_at or document.fetched_at


def _title_tokens(title: str) -> set[str]:
    tokens = tokenize(title)
    return {token for token in tokens if len(token) > 1} if len(tokens) >= MIN_TITLE_TOKENS else set()


def _domain(document: Document) -> str:
    """规范域名，用于判断两条是否来自同一个站点。"""

    host = urlparse(document.canonical_url or document.original_url or "").netloc.lower()
    return host.removeprefix("www.")


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


async def mark_reposts(
    session: AsyncSession,
    *,
    within_days: int = 14,
    threshold: int = REPOST_HAMMING,
) -> int:
    """标记转载：把 dup_of 指向最早的那条，返回新标记的数量。

    只在「已有指纹、尚未判为转载」的文档之间比较。一条被标为转载的文档不再参与
    后续比较，避免出现 A→B→C 的链——链会让「原始来源」这个概念失效。
    """

    cutoff = datetime.now(UTC) - timedelta(days=within_days)
    statement = (
        select(Document)
        .where(
            Document.simhash.is_not(None),
            Document.dup_of.is_(None),
            Document.fetched_at >= cutoff,
        )
        .order_by(Document.fetched_at.asc())
    )
    documents = list((await session.execute(statement)).scalars().all())

    marked = 0
    for index, current in enumerate(documents):
        if current.dup_of is not None:
            continue
        for earlier in documents[:index]:
            if earlier.dup_of is not None:
                continue
            if earlier.language and current.language and earlier.language != current.language:
                continue
            # 转载必须跨站。这条约束是真实数据撞出来的：arXiv 的 abs 页正文里，
            # 「Computer Science > xxx / arXiv:编号 / Submitted on / View PDF / Abstract:」
            # 这些模板文字占了 600 字的一半以上，于是两篇毫不相干的论文指纹只差
            # 10 位（阈值边界），被标成「转载」。同一个域名下的两篇文章不可能互为转载，
            # 相似只能说明它们共用了站点模板。
            if _domain(current) == _domain(earlier):
                continue
            # 时间差超过窗口的不比：同一篇文章的转载一定发生在原始发布之后不久
            if abs((_published(current) - _published(earlier)).total_seconds()) > within_days * 86400:
                continue
            if hamming(current.simhash, earlier.simhash) <= threshold:
                current.dup_of = earlier.id
                marked += 1
                logger.info(
                    "repost_detected",
                    extra={
                        "doc_id": str(current.id),
                        "original_id": str(earlier.id),
                        "distance": hamming(current.simhash, earlier.simhash),
                    },
                )
                break
    return marked


def group_similar_titles(
    items: list[tuple[uuid.UUID, datetime, str, uuid.UUID]],
    *,
    threshold: float = EVENT_JACCARD,
    window: timedelta = timedelta(hours=EVENT_WINDOW_HOURS),
) -> list[list[uuid.UUID]]:
    """把标题相似、时间相邻、**来自不同源**的条目分组。纯函数，便于单测。

    输入是 `(id, 发布时间, 标题, 来源 id)` 而不是 ORM 对象，这样这条逻辑不依赖数据库。

    **必须来自不同源，这一点是实测撞出来的。** 一开始只按标题相似度聚，结果：
    OpenAI 同一天发的两条公告（ChatGPT Images / ChatGPT for Financial Services）
    被聚成「同一事件」；HN 上同一天的 two 条 Show HN 也被聚成一条。它们的共同点是
    共用固定前缀或产品名，标题相似度天然很高，但它们明明是两件不同的事。

    根因是「事件」的定义：这个簇要回答的是「这件事有几家媒体在报」。同一个源发两次
    不构成「多家在报」，只是这家媒体自己更新了两条。加上跨源约束后，这类误聚全部消失，
    而且不需要给每个源维护停用词表。

    另一个细节：用并查集而不是两两分组。A 与 B 相似、B 与 C 相似时三条归一组，
    即使 A 与 C 低于阈值——事件的报道链常是「A 首发、B 转述 A、C 转述 B」，
    只做两两比较会漏掉首尾关系。
    """

    tokens = {item_id: _title_tokens(title) for item_id, _, title, _ in items}
    parent = {item_id: item_id for item_id, _, _, _ in items}

    def find(node: uuid.UUID) -> uuid.UUID:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: uuid.UUID, right: uuid.UUID) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for index, (current_id, current_at, _, current_source) in enumerate(items):
        current_tokens = tokens[current_id]
        if not current_tokens:
            continue
        for other_id, other_at, _, other_source in items[index + 1 :]:
            if current_source == other_source:
                continue
            if abs(other_at - current_at) > window:
                continue
            if _jaccard(current_tokens, tokens[other_id]) >= threshold:
                union(current_id, other_id)

    groups: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for item_id, _, _, _ in items:
        groups[find(item_id)].append(item_id)
    return list(groups.values())


async def cluster_events(
    session: AsyncSession,
    *,
    within_days: int = 14,
    threshold: float = EVENT_JACCARD,
) -> int:
    """按标题相似度把同一事件的独立报道聚簇，返回涉及改动的文档数。

    簇标识取簇内**最早发布**那条的 id，不另建 cluster 表。本阶段只需要「这件事
    有几家在报」，没有簇的独立生命周期；真要独立成表，等需要给事件加标题、
    摘要、时间线的时候再做。
    """

    cutoff = datetime.now(UTC) - timedelta(days=within_days)
    statement = (
        select(Document)
        .where(
            Document.fetched_at >= cutoff,
            Document.dup_of.is_(None),
            Document.content_type.not_in(EXCLUDED_CONTENT_TYPES),
        )
        .order_by(Document.fetched_at.asc())
    )
    documents = list((await session.execute(statement)).scalars().all())
    by_id = {document.id: document for document in documents}

    groups = group_similar_titles(
        [
            (document.id, _published(document), document.title, document.source_id)
            for document in documents
        ],
        threshold=threshold,
    )

    touched = 0
    for members in groups:
        if len(members) < 2:
            # 单独一条不设簇标识：cluster_id 有值就代表「至少两家在报」，
            # 让单条也有值会让前端的「N 家报道」变成永远显示 1
            for member_id in members:
                document = by_id[member_id]
                if document.cluster_id is not None:
                    document.cluster_id = None
                    touched += 1
            continue
        leader = min((by_id[member_id] for member_id in members), key=_published)
        for member_id in members:
            document = by_id[member_id]
            if document.cluster_id != leader.id:
                document.cluster_id = leader.id
                touched += 1

    return touched


async def run_dedup(
    session: AsyncSession,
    *,
    within_days: int = 14,
    threshold: int = REPOST_HAMMING,
    event_threshold: float = EVENT_JACCARD,
) -> dict[str, int]:
    """先判转载再聚类，顺序不能反。

    反过来的话，转载的副本会被当成「另一家媒体的报道」参与聚类，让簇的大小虚高，
    读者会以为一件事有五家在报，实际是同一篇稿子的五次转载。
    """

    reposts = await mark_reposts(session, within_days=within_days, threshold=threshold)
    clustered = await cluster_events(session, within_days=within_days, threshold=event_threshold)
    await session.commit()

    logger.info("dedup_done", extra={"reposts": reposts, "clustered": clustered})
    return {"reposts": reposts, "clustered": clustered}

"""正文补全：按热度挑候选、抓正文、算指纹。

这一层的三个决定需要先讲清楚，因为它们决定了「抓谁」和「抓完怎么存」。

**一、抓谁：按热度，不按时间。**

抓正文的成本比采集元数据高一个量级：要下载 HTML、解析 DOM、清洗。如果对每个条目
都抓，总成本 = 条目数 × 单页成本，而其中大部分内容既没人看、也不会被检索到。

热度信号由各源自己提供（HN 的 score、GitHub 的下载量），没有信号的源（RSS、arXiv）
不参与热度排序，退回时间窗。**排序必须按源分组**：GitHub 的下载量动辄几十万，
HN 的分数最多几千，全局混排会让 GitHub 永远排第一，RSS 和 arXiv 永远轮不到。

**二、抓完存多少：由 rights_mode 决定。**

`metadata_excerpt` 的源（媒体、聚合站）只存一段摘要级片段，全文只用于在内存里
算指纹和提摘要，落库前丢弃。这不是技术限制，是项目从阶段 1 就定下的合规底线：
媒体内容只保留标题、摘要、原文链接。

`full_text` / `public_domain` 的源（官方博客、arXiv）存全文。

**三、抓不到怎么办：区分「没有」和「失败」。**

`empty`（页面确实没有正文，比如纯图片页、登录墙）和 `failed`（网络错误、403）
是两种结论。前者重试一百次也没用；后者按原因存在 `content_error` 里，例如
`HTTP 403`、`ProxyError: 502`。

**失败分两种，只有一种会重试。** 实测失败原因一半是 `HTTP 403/405`（反爬）
和付费墙——重试只会把同一个请求再打一遍，既浪费时间，也可能让对方把 IP 拉黑；
另一半是本机代理 502、连接超时，这类是瞬时的，代理规则一改或网络恢复就能通。

所以 `failed` 不是终态，但也**不是无脑重试**：每次失败记一次
`content_attempts`，按 6h → 24h → 72h 退避后重新进入候选，三次用完为止。
确定性失败（4xx / 非 HTML）在写库时就把次数记满，等价于「永不重试」——
筛选时只比数字，不必在 SQL 里再做一遍字符串匹配。

**并发与写库分开。** 下载是 I/O，适合并发；ORM 对象不并发安全，多个协程同时改同一个
session 里的对象会串。所以流程是「并发下载 → 汇总 → 串行写库」，网络阶段不碰 session。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.connectors.base import NO_FETCH_SOURCES
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Document, Source
from app.services import embedding_service, vector_index_service
from app.services.embedding_service import EmbeddingUnavailable
from app.utils.engagement import normalize_engagement
from app.utils.simhash import simhash
from app.utils.webpage import extract_main_text, extract_meta_description

logger = get_logger(__name__)

# 落库策略：不在这个白名单里的源一律只存摘要片段
#
# cc_by_nc_nd 是「页面写明 CC 署名-非商用-非衍生」的源（目前只有阮一峰博客）。
# 它能进这个白名单，是因为权利人自己用协议放开了复制与转载，不涉及我们的合规判断；
# 但 ND（禁止演绎）这一条必须由调用方守住——只能原样展示，不能改写摘要。
FULL_TEXT_RIGHTS = ("full_text", "public_domain", "cc_by_nc_nd")
# 摘要片段长度。够回答「这条讲什么」，不足以替代原文阅读
EXCERPT_CHARS = 600
# 派生摘要的长度上限。列表只显示两行，300 字已经溢出可视区域；
# 定得比 EXCERPT_CHARS 短还有第二个作用：避免和 content_text 完全相等，
# 否则会被下方 is_summary_copy 逻辑误判成「connector 伪造的摘要副本」。
SUMMARY_MAX_CHARS = 300
# 全文上限。再长的正文对检索没有额外价值，只会拖慢入库与后续分块
FULL_TEXT_MAX_CHARS = 20000
# 单页下载超时；正文页比 API 慢得多，但也不能无限等
FETCH_TIMEOUT_SECONDS = 15
# 连接类错误重试次数（不含 4xx——那是确定性失败，重试无意义）
MAX_ATTEMPTS = 2
# 跨轮次的重试预算：一条条目最多被抓几次。一次抓取本身已经内含 MAX_ATTEMPTS 次
# 传输层重试，所以这里说的「3 次」是三个独立的调度轮次，中间隔着退避间隔
MAX_FETCH_ATTEMPTS = 3
# 第 N 次失败之后要等多久才重新进入候选。间隔递增的理由很实际：
# 瞬时故障（网络抖动、代理临时 502）通常几分钟就恢复，等 6 小时已经足够保守；
# 等了 24 小时还失败，说明大概率不是抖动，再等 72 小时是给「域名级封锁临时解除」
# 留的最后一次机会
RETRY_BACKOFF_HOURS = (6, 24, 72)

# HN 的 source_key；回访热度只对它有意义（详情接口能单独取单个 item）
HACKERNEWS_KEY = "hackernews_en"


@dataclass(slots=True)
class PageResult:
    """一次取页的纯数据结果——不持有 ORM 对象，可以在协程之间安全传递。"""

    status: str
    text: str | None = None
    fingerprint: str | None = None
    error: str = ""
    engagement: dict[str, Any] = field(default_factory=dict)
    chars: int = 0
    # 内容来自 <meta> 描述而不是正文：前端要能区分，判重时也不该拿它当依据
    from_meta: bool = False
    # 页面自带的描述（og:description / meta description）。它是编辑写的导语，
    # 比「正文前 300 字」更适合当摘要，所以正文和它都要，不能二选一。
    summary_hint: str | None = None


def _is_retryable_error(error: str) -> bool:
    """判断一次失败值不值得下一轮再试。

    分界线是「对方的回答是确定性的，还是环境性的」：

    - `HTTP 403/404/405`、付费墙、非 HTML 内容——站点已经明确表态，再敲一次门
      得到的还是同一句话，重试只是替对方刷日志；
    - `HTTP 5xx`、`HTTP 429`、`ProxyError` / `ConnectError` / `ReadTimeout`——
      失败发生在网络层或对方临时过载，换个时间大概率能通。

    这个判断放在写库时做一次，而不是留在 SQL 里靠 `LIKE 'HTTP 4%'` 之类匹配：
    错误串的格式会随 `_download` 的实现变化，而写库点是唯一知道「这次到底为什么
    失败」的地方。
    """

    if not error:
        return False
    if error.startswith("非 HTML"):
        return False

    matched = re.match(r"HTTP\s+(\d{3})", error)
    if matched:
        code = int(matched.group(1))
        # 4xx 是对方拒绝，429 除外——限流的正确反应是等，不是放弃
        return code == 429 or code >= 500
    return True


def _retry_backoff_expression():
    """把「第几次失败等多久」翻译成 SQL 的 interval 表达式。

    放在 SQL 里而不是 Python 里，是因为退避判断必须和候选筛选在同一次查询中完成：
    先取出所有 failed 再在内存里过滤，会把整张失败表拉进内存，而这个表只会越来越大。
    """

    first, second, last = RETRY_BACKOFF_HOURS
    return case(
        (Document.content_attempts <= 1, literal_column(f"interval '{int(first)} hours'")),
        (Document.content_attempts == 2, literal_column(f"interval '{int(second)} hours'")),
        else_=literal_column(f"interval '{int(last)} hours'"),
    )


async def select_candidates(
    session: AsyncSession,
    *,
    limit: int = 30,
    per_source: int = 10,
    within_days: int = 14,
) -> list[Document]:
    """挑出待抓正文的条目：每个源内部按热度取前 N，再全局按热度取前 limit。

    先按源分组再合并，是为了让每个源都有配额。直接对全表按 engagement_score 排序，
    有下载量的 GitHub 会占满全部名额，而 RSS 那些没有热度信号的条目一条都进不来。
    """

    row_number = func.row_number().over(
        partition_by=Document.source_id,
        order_by=(
            Document.engagement_score.desc().nulls_last(),
            Document.published_at.desc().nulls_last(),
        ),
    )
    window = literal_column(f"interval '{int(within_days)} days'")

    # 失败条目重新入队的条件：还有预算、且退避时间已过。
    # content_fetched_at 为空说明它失败在加入这一列之前，没有时间参照，直接放行——
    # 这类记录最多只有一批，宁可多抓一次也不能让它永久卡住。
    retryable_failure = and_(
        Document.content_status == "failed",
        Document.content_attempts < MAX_FETCH_ATTEMPTS,
        or_(
            Document.content_fetched_at.is_(None),
            Document.content_fetched_at < func.now() - _retry_backoff_expression(),
        ),
    )

    ranked = (
        select(Document.id.label("doc_id"), row_number.label("rn"))
        .where(
            or_(Document.content_status == "pending", retryable_failure),
            Document.fetched_at >= func.now() - window,
            Document.source_id.not_in(select(Source.id).where(Source.source_key.in_(NO_FETCH_SOURCES))),
        )
        .subquery()
    )

    statement = (
        select(Document)
        .options(selectinload(Document.source))
        .join(ranked, ranked.c.doc_id == Document.id)
        .where(ranked.c.rn <= per_source)
        .order_by(Document.engagement_score.desc().nulls_last(), Document.published_at.desc().nulls_last())
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars().all())


async def _download(client: httpx.AsyncClient, url: str) -> tuple[str | None, str]:
    """下载页面，返回 (HTML, 失败原因)。失败原因为空串表示成功。"""

    last_error = "unknown"
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = await client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:200]
            if attempt + 1 < MAX_ATTEMPTS:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return None, last_error

        if response.status_code in (401, 403, 404, 410):
            # 确定性失败：付费墙、已删除、拒绝访问。重试不会改变结果
            return None, f"HTTP {response.status_code}"
        if response.status_code == 429:
            return None, "HTTP 429 限流"
        if response.status_code >= 400:
            return None, f"HTTP {response.status_code}"

        content_type = (response.headers.get("content-type") or "").lower()
        if "html" not in content_type and "xml" not in content_type:
            return None, f"非 HTML 内容（{content_type or 'unknown'}）"
        return response.text, ""

    return None, last_error


async def _refresh_hackernews_engagement(
    client: httpx.AsyncClient, external_id: str
) -> dict[str, Any] | None:
    """回访 HN 条目详情，拿最新的 score / descendants。

    为什么必须回访：分数会衰减。抓取已改走 `topstories`，入库时拿到的是上榜
    那一刻的真实票数，但两天后它可能从 400 掉到 200。「这条值不值得花一次请求
    抓正文」看的是它曾经有多热，所以回访负责保留历史最高值，
    见 `_merge_engagement`。
    """

    try:
        response = await client.get(f"https://hacker-news.firebaseio.com/v0/item/{external_id}.json")
        if response.status_code != 200:
            return None
        item = response.json()
    except (httpx.TimeoutException, httpx.TransportError, ValueError):
        return None

    if not isinstance(item, dict):
        return None

    engagement: dict[str, Any] = {}
    if isinstance(item.get("score"), int):
        engagement["score"] = item["score"]
        engagement["primary"] = item["score"]
    if isinstance(item.get("descendants"), int):
        engagement["comments"] = item["descendants"]
    return engagement or None


def _merge_engagement(document: Document, engagement: dict[str, Any]) -> None:
    """把新拿到的热度合并进已有记录，只增不减。

    HN 的分数会随时间下降——一条故事过两天从 400 分掉到 200 分是常态。
    热度在这里的用途是「这条值不值得花一次请求抓正文」，历史最高关注度才是
    这个判断的正确输入；用最新值会让曾经的热帖在爬完榜之后被重新判成冷门。
    """

    existing = document.engagement_raw or {}
    merged = dict(existing)
    merged.update({key: value for key, value in engagement.items()})
    if existing.get("primary") and engagement.get("primary"):
        merged["primary"] = max(existing["primary"], engagement["primary"])
    document.engagement_raw = merged
    document.engagement_score = normalize_engagement(merged)


async def refresh_engagement(
    session: AsyncSession,
    *,
    within_days: int = 14,
    limit: int = 300,
    concurrency: int = 8,
) -> dict[str, int]:
    """给已入库的 HN 条目回填热度，让「按热度抓正文」真正有排序依据。

    为什么需要单独做这一步：`engagement_score` 是阶段 3 新加的列，此前入库的
    255 条 HN 全是 NULL。NULL 在排序里被 `nulls_last` 推到末尾，于是选取退化成
    按发布时间取——「热门优先」名存实亡。增量同步也补不上：这些条目已存在，
    同步时会被跳过。

    只回填 HN：它是唯一能按 id 单独查详情的源（Firebase 的 item 接口一次一条）。
    GitHub 的下载量只能靠拉整个 release 响应拿到，未认证配额是 60 次/小时，
    给 60 条记录各发一次请求就会把配额耗尽、连带影响正常同步。arXiv 和 RSS
    本来就没有热度信号，回填无从下手。
    """

    window = literal_column(f"interval '{int(within_days)} days'")
    statement = (
        select(Document)
        .options(selectinload(Document.source))
        .where(
            Document.source.has(source_key=HACKERNEWS_KEY),
            Document.fetched_at >= func.now() - window,
        )
        .order_by(Document.fetched_at.desc())
        .limit(limit)
    )
    documents = list((await session.execute(statement)).scalars().all())
    if not documents:
        logger.info("engagement_no_candidate")
        return {"scanned": 0, "updated": 0, "unchanged": 0, "unavailable": 0}

    started = time.perf_counter()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    client = build_client()

    async def _one(document: Document) -> dict[str, Any] | None:
        async with semaphore:
            return await _refresh_hackernews_engagement(client, document.external_id)

    try:
        results = await asyncio.gather(*(_one(document) for document in documents))
    finally:
        await client.aclose()

    updated = unchanged = unavailable = 0
    # 上游返回条数不足时宁可尾部保留原值，也不要整批失败：这正是下面
    # 「拿不到不影响已有值」那一段的既定行为，所以显式 strict=False。
    for document, engagement in zip(documents, results, strict=False):
        if not engagement:
            # 拿不到（超时、条目被删、接口改格式）不影响已有值，原样保留
            unavailable += 1
            continue
        before = document.engagement_score
        _merge_engagement(document, engagement)
        if document.engagement_score != before:
            updated += 1
        else:
            unchanged += 1

    await session.commit()
    summary = {
        "scanned": len(documents),
        "updated": updated,
        "unchanged": unchanged,
        "unavailable": unavailable,
    }
    logger.info("engagement_refreshed", extra={**summary, "duration_ms": int((time.perf_counter() - started) * 1000)})
    return summary


async def fetch_page(client: httpx.AsyncClient, document: Document) -> PageResult:
    """抓一条的正文并算指纹。只做 I/O 与 CPU，不碰 session。"""

    engagement: dict[str, Any] = {}
    if document.source and document.source.source_key == HACKERNEWS_KEY:
        refreshed = await _refresh_hackernews_engagement(client, document.external_id)
        if refreshed:
            engagement = refreshed

    html, error = await _download(client, document.canonical_url)
    if html is None:
        return PageResult(status="failed", error=error, engagement=engagement)

    # 描述要在正文之前取：正文抽到了也要留着它当摘要候选。
    # 早期版本只在正文抽不到时才读 meta，结果是「抓到全文的条目反而没有摘要」。
    description = extract_meta_description(html)
    text_value = extract_main_text(html)
    from_meta = False
    if not text_value:
        # 正文抽不到时退回 <meta> 描述。36氪这类 SPA 站点的服务端 HTML 只有骨架，
        # 全文要执行 JS 才有——描述是这种情况下唯一能拿到的东西。
        text_value = description
        from_meta = text_value is not None

    if not text_value:
        # 页面既没有正文也没有描述：登录墙、纯图片页。重试没有意义
        return PageResult(status="empty", engagement=engagement)

    return PageResult(
        status="fetched",
        text=text_value,
        # 描述文本通常只有几十字，达不到 SimHash 的最小 token 数，会自然返回 None。
        # 这是正确的：用它算出的指纹会把同一站点的所有文章判成近似重复。
        fingerprint=simhash(text_value),
        engagement=engagement,
        chars=len(text_value),
        from_meta=from_meta,
        summary_hint=description,
    )


def _clip_summary(text_value: str) -> str:
    """把任意长度的文本收成一段摘要。

    截断时优先回退到最近的句末标点。硬切一半会让列表里出现「……三个原因导致」这种
    断在半句话上的摘要，读者第一反应是数据坏了。句号离窗口起点太近（说明这一句本身
    超长）时退回省略号，比在词中间切断诚实。
    """

    text_value = text_value.strip()
    if len(text_value) <= SUMMARY_MAX_CHARS:
        return text_value

    head = text_value[:SUMMARY_MAX_CHARS]
    for punct in ("。", "！", "？", ". ", "! ", "? "):
        position = head.rfind(punct)
        if position >= SUMMARY_MAX_CHARS // 2:
            return head[: position + len(punct)].strip()
    # 省略号本身占一个字符，必须从窗口里让出来。否则返回值是 301 字，
    # 而调用方判「超长」用的是 > 300——同一段文本每一轮都被判超长、再截一次，
    # 「修复 14 条」会永远挂在日志里，看起来像有 14 条坏数据修不好。
    return head[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"


def _looks_like_title(text_value: str, title: str) -> bool:
    """判断一段文字是否只是标题的重述。

    网页正文抽取常把 `<h1>` 当第一段抓下来，于是「摘要」和标题一模一样——
    列表里会出现标题下面紧跟同样的句子，读者第一反应是数据重复了。
    比较时去掉全部空白并转小写，因为站点常在标题里插换行或多余空格。
    """

    left = "".join(text_value.split()).lower()
    right = "".join((title or "").split()).lower()
    if not right:
        return False
    if left == right:
        return True
    # 站点常给标题加一句后缀（" - 站名"），也算只是标题
    return left.startswith(right) and len(left) - len(right) <= 8


# 明确的界面文案。抽正文时它们混进「最长文本块」的概率最高
NOISE_PHRASES = (
    "opens in a new tab",
    "skip to content",
    "skip to main",
    "sign in",
    "log in",
    "subscribe",
    "javascript is disabled",
    "enable javascript",
    "阅读全文",
    "点击查看",
    "登录后",
    "扫码",
)
# 一句话摘要至少该有一个标点。没有标点的短片段通常是导航项或按钮文案
_SENTENCE_PUNCTUATION = ("，", "。", "！", "？", "；", "：", "、", ",", ".", "!", "?", ";", ":")
MIN_SENTENCE_SUMMARY_CHARS = 40


def _is_noise_summary(text_value: str) -> bool:
    """判断一段文本是不是界面残渣而不是句子。

    实测撞出来的两类：「INSIDE Deutsche Bank」（页面导航里的栏目名）、
    「(opens in a new tab)」（无障碍提示）。它们都恰好超过 20 字的段落门槛，
    于是在付费墙页面上被当成了「正文第一段」。

    判据取「短且无标点」：真正的摘要，即使只有二十来字，中文也会带句号或逗号；
    英文摘要一定会带句点。没有标点的短片段几乎只可能是界面元素。
    """

    lowered = text_value.lower()
    if any(phrase in lowered for phrase in NOISE_PHRASES):
        return True
    return len(text_value) < MIN_SENTENCE_SUMMARY_CHARS and not any(
        mark in text_value for mark in _SENTENCE_PUNCTUATION
    )


def _derive_summary(text_value: str, title: str = "") -> str | None:
    """从正文里切出一段可当摘要的话。

    只取第一个「像正文」的段落，而不是从头截 300 字。原因很实际：正文开头往往是
    「记者 XXX 报道」「编者按」这种栏目套话，截出来对判断内容毫无帮助。跳过短于
    20 字的行（导航残渣、署名行）再取，命中率明显更高。
    """

    if not text_value:
        return None

    for block in re.split(r"\n{2,}|\n", text_value):
        block = block.strip()
        if len(block) < 20:
            continue
        if _looks_like_title(block, title):
            continue
        if _is_noise_summary(block):
            continue
        return _clip_summary(block)
    return None


def _next_attempts(document: Document, error: str) -> int:
    """算这次失败之后 `content_attempts` 该记几。

    可重试的错误累加真实的失败次数；确定性错误直接把预算记满——这样
    `select_candidates` 只要看到「次数到顶」就知道不必再考虑它，不需要理解
    错误串的含义。把判断结果**落到数据上**，而不是每次筛选重算，是这里的关键。
    """

    if not _is_retryable_error(error):
        return MAX_FETCH_ATTEMPTS
    return min((document.content_attempts or 0) + 1, MAX_FETCH_ATTEMPTS)


def apply_result(document: Document, result: PageResult) -> str:
    """把取页结果写到 ORM 对象上。只在主协程里调用。"""

    document.content_status = result.status
    document.content_fetched_at = datetime.now(UTC)
    # 成功时清空原因：否则上一轮的 403 会一直挂在这条记录上，看起来像这次也失败了
    document.content_error = result.error or None
    if result.status == "failed":
        document.content_attempts = _next_attempts(document, result.error)

    if result.engagement:
        _merge_engagement(document, result.engagement)

    if result.status == "fetched":
        if result.fingerprint:
            document.simhash = result.fingerprint
        stored = _store_text(result.text or "", document.rights_mode or "metadata_excerpt")
        # 这段判断必须放在补摘要之前：一旦摘要先被填上，content_text 与 summary
        # 的相等关系就变了，is_summary_copy 会漏判真正的伪造副本。
        is_summary_copy = document.content_text is not None and document.content_text == document.summary
        # 已有摘要（来自 feed / API）时保留它，正文片段只补空位。
        # 摘要通常是编辑写的导语，质量高于正文前 600 字这种机械截断。
        #
        # 但「有值」不等于「有正文」：如果 content_text 只是 summary 的副本
        # （Connector 早期把 summary 复制进 content_text 的写法），那它不是
        # 正文，必须让真抓到的网页文本覆盖掉。否则会出现「指纹是网页算的、
        # 正文却是 600 字摘要」这种自相矛盾的状态。
        if stored and (not document.content_text or is_summary_copy):
            document.content_text = stored
        # 摘要在抓取阶段补齐：Hacker News 的外链故事上游只有标题，feed 阶段
        # summary 永远是空，唯一来源就是这里。优先级是页面自带描述 > 正文首段，
        # 因为描述是编辑写的导语，而正文首段经常是署名或栏目套话。
        if not (document.summary or "").strip():
            hint = result.summary_hint
            if hint and _looks_like_title(hint, document.title):
                # 有些站的 og:description 直接抄标题，那还不如没有
                hint = None
            hint = hint or _derive_summary(result.text or "", document.title)
            if hint:
                document.summary = _clip_summary(hint)
                # 保留这个标记会让前端在已经有摘要的情况下继续显示「缺摘要」
                _remove_flag(document, "missing_summary")
        if result.from_meta:
            # 让下游知道这段文字是描述而不是正文，判重与检索的权重都该更低
            _add_flag(document, "content_from_meta")
        else:
            _remove_flag(document, "content_from_meta")
        _remove_flag(document, "content_failed")
        _remove_flag(document, "content_empty")
    elif result.status == "empty":
        _add_flag(document, "content_empty")
    else:
        _add_flag(document, "content_failed")

    return document.content_status


def _store_text(text_value: str, rights_mode: str) -> str:
    """按 rights_mode 决定落库多少内容。"""

    if rights_mode in FULL_TEXT_RIGHTS:
        return text_value[:FULL_TEXT_MAX_CHARS]
    # 只存摘要级片段：够回答「这条讲什么」，不构成对原文的替代
    return text_value[:EXCERPT_CHARS]


def _add_flag(document: Document, flag: str) -> None:
    flags = list(document.quality_flags or [])
    if flag not in flags:
        flags.append(flag)
        document.quality_flags = flags


def _remove_flag(document: Document, flag: str) -> None:
    flags = list(document.quality_flags or [])
    if flag in flags:
        flags.remove(flag)
        document.quality_flags = flags


def build_client() -> httpx.AsyncClient:
    """正文抓取的共用 client：跟随跳转、带可联系 UA、并发由调用方控制。"""

    return httpx.AsyncClient(
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
        follow_redirects=True,
        max_redirects=5,
        headers={
            "User-Agent": settings.http_user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )


async def backfill_summaries_from_content(session: AsyncSession, *, limit: int = 2000) -> int:
    """给「已经有正文、却没有摘要」的存量条目补上摘要。

    为什么会有这种状态：早期 `apply_result` 只往 content_text 写抓到的文本，
    summary 字段完全没碰。于是抓取明明成功了，列表上依然显示「尚未补全正文」——
    因为前端只读 summary。这类数据不会自己好，也不能靠重抓（状态已是 fetched，
    不在待抓队列里），只能在本地从已有正文反推。

    不做成一次性 SQL 的原因：只要还有源在上游只给标题，这个状态就会持续产生。
    放进 enrich 的开头，每跑一次就自愈一次，不依赖人记得修。
    """

    statement = (
        select(Document)
        .where(
            Document.content_text.is_not(None),
            func.length(func.trim(Document.content_text)) >= 20,
        )
        .limit(limit)
    )
    documents = list((await session.execute(statement)).scalars().all())
    if not documents:
        return 0

    repaired = 0
    for document in documents:
        current = (document.summary or "").strip()
        # 这里刻意不按长度截断。两个理由：
        # 一是展示层已经用 line-clamp 收口，库里长一点不会把页面撑坏；
        # 二是 arXiv 的摘要有 1400~1900 字，削到 300 会把结论部分丢掉——而那正是
        # 这条记录最有价值的信息。截断只应该发生在「机器从正文里切一段」的时候，
        # 不该发生在「上游给了完整摘要」的时候。
        # 只处理三种坏摘要：空的、只是标题的、界面残渣的。
        # 正常摘要（来自 feed 的编辑导语）一律不动——那比机器截的句子质量高。
        if current and not _looks_like_title(current, document.title) and not _is_noise_summary(current):
            continue
        hint = _derive_summary(document.content_text or "", document.title)
        if hint:
            document.summary = hint
            _remove_flag(document, "missing_summary")
        elif current:
            # 正文里也挑不出像样的话：宁可空着，也别留一句「INSIDE Deutsche Bank」
            document.summary = None
            _add_flag(document, "missing_summary")
        else:
            continue
        repaired += 1
    if repaired:
        await session.commit()
        logger.info("summary_backfilled", extra={"repaired": repaired})
    return repaired


async def mark_self_posts_as_from_feed(session: AsyncSession) -> int:
    """把「摘要就是正文」的 HN 自帖标成 from_feed，别再去抓讨论页。

    判据是 `content_text == summary`。外链故事的 summary 恒为空（HN 不提供），
    所以两者相等只可能出现在 Ask HN / Show HN 这类作者自己写正文的条目上。
    旧版 connector 把 summary 复制进 content_text，这批数据停在 pending，
    enrich 会去抓讨论页——抓回来的是整页评论，不是文章，纯属噪音，
    还会占用「每源配额」，把真正需要抓的外链故事挤出去。
    """

    statement = select(Document).where(
        Document.source_id.in_(select(Source.id).where(Source.source_key == HACKERNEWS_KEY)),
        Document.content_status.in_(("pending", "failed", "empty")),
        Document.summary.is_not(None),
        func.trim(Document.summary) != "",
        Document.content_text == Document.summary,
    )
    documents = list((await session.execute(statement)).scalars().all())
    for document in documents:
        document.content_status = "from_feed"
        _remove_flag(document, "missing_summary")
        for stale in ("content_failed", "content_empty"):
            _remove_flag(document, stale)
    if documents:
        await session.commit()
        logger.info("self_posts_marked", extra={"count": len(documents)})
    return len(documents)


def _enrich_summary() -> dict[str, Any]:
    """enrich 返回值的模板。

    「有候选」和「没候选」两条路径共用它，键集合因此不会漂移。调用方是按直接下标
    取值的（`summary["indexed_documents"]`），少一个键就会在「恰好没有候选」这个
    最不该出问题的分支上抛 KeyError——这不是假设，是实测撞出来的。
    """

    return {
        "selected": 0,
        "retried": 0,
        "fetched": 0,
        "empty": 0,
        "failed": 0,
        "summary_gained": 0,
        "summary_repaired": 0,
        "self_posts_marked": 0,
        "indexed_documents": 0,
        "indexed_chunks": 0,
        "index_error": None,
        "duration_ms": 0,
    }


async def run_enrich(
    session: AsyncSession,
    *,
    limit: int = 30,
    per_source: int = 10,
    concurrency: int = 4,
) -> dict[str, Any]:
    """按热度补全正文与指纹，返回分状态的计数。

    并发只发生在下载阶段；写库串行，因为 AsyncSession 不是并发安全的，
    多个协程同时改同一个 session 里的对象会出现互相覆盖。
    """

    # 先自愈「有正文没摘要」的存量，再抓新的。顺序不能反：这一步不花网络请求，
    # 放在前面能立刻让列表里那批「抓成功了却还显示缺摘要」的条目恢复正常。
    repaired = await backfill_summaries_from_content(session)
    marked = await mark_self_posts_as_from_feed(session)

    candidates = await select_candidates(session, limit=limit, per_source=per_source)
    # 这一批里有多少是「上次失败、退避已过」的条目。运维上必须能区分
    # 「在追新内容」和「在还旧账」：否则失败率长期降不下来时，看不出是重试在打转
    retried = sum(1 for document in candidates if document.content_status == "failed")
    if not candidates:
        logger.info("enrich_no_candidate")
        # 用同一个构造函数产出，而不是手写一份"零值字典"。手写副本迟早和正常路径
        # 漂移——实测就是少了一个键，调用方按直接下标取值，在「恰好没有候选」
        # 这种最不该崩的时刻抛了 KeyError。
        summary = _enrich_summary()
        summary["summary_repaired"] = repaired
        summary["self_posts_marked"] = marked
        return summary

    started = time.perf_counter()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    client = build_client()

    async def _one(document: Document) -> PageResult:
        async with semaphore:
            return await fetch_page(client, document)

    try:
        results = await asyncio.gather(*(_one(document) for document in candidates))
    finally:
        await client.aclose()

    before_with_summary = sum(1 for document in candidates if (document.summary or "").strip())
    counters = Counter(
        apply_result(document, result)
        for document, result in zip(candidates, results, strict=False)
    )
    after_with_summary = sum(1 for document in candidates if (document.summary or "").strip())
    await session.commit()

    # 正文此刻还在内存里（`result.text`），**这是它唯一一次可用于向量化的时机**。
    # `metadata_excerpt` 级别的源走完 `_store_text` 之后库里只剩 600 字，剩下几万字
    # 随这个函数返回就被 GC 掉。所以索引必须在这里做，挪到事后的批处理任务就晚了——
    # 那时能索引的只有已经入库的那点片段，等于把短摘要再向量化一遍，白忙。
    index_stats: dict[str, int] = {"documents": 0, "chunks": 0}
    index_error: str | None = None
    long_texts = [
        (document.id, result.text or "")
        for document, result in zip(candidates, results, strict=False)
        if result.status == "fetched" and len(result.text or "") > embedding_service.CHUNK_CHARS
    ]
    if long_texts:
        try:
            index_stats = await vector_index_service.index_documents(session, long_texts)
            await session.commit()
        except EmbeddingUnavailable as error:
            # 模型没装好不该连累抓取：正文已经落库，索引下一轮还能补。
            # 但必须把原因带进返回值，让 CLI 打印出来——静默跳过会变成长期空洞。
            index_error = str(error).splitlines()[0]
            logger.warning("vector_index_unavailable", extra={"error": index_error})

    duration_ms = int((time.perf_counter() - started) * 1000)
    summary = _enrich_summary()
    summary.update(
        selected=len(candidates),
        retried=retried,
        fetched=counters.get("fetched", 0),
        empty=counters.get("empty", 0),
        failed=counters.get("failed", 0),
        summary_gained=after_with_summary - before_with_summary,
        summary_repaired=repaired,
        self_posts_marked=marked,
        indexed_documents=index_stats.get("documents", 0),
        indexed_chunks=index_stats.get("chunks", 0),
        index_error=index_error,
        duration_ms=duration_ms,
    )
    logger.info("enrich_done", extra=summary)
    return summary


async def backfill_fingerprints(session: AsyncSession, *, include_api: bool = False) -> dict[str, int]:
    """重拉上游，用新解析逻辑修正已入库条目：补指纹、补更完整的摘要。

    为什么需要单独一遍：`sync` 遇到已存在的 external_id 会直接跳过（唯一约束兜底），
    所以「让 Connector 开始算指纹」「让 arXiv 不再把摘要截到 600 字」这类改动
    不会自动修好历史数据。而库里的文本已经被旧逻辑截过，没法从它反推——只能重拉。

    修正原则是**只增不减**：
    - 指纹只在原来为空时写入（正文页算的指纹比 feed 摘要更准，不该被覆盖）
    - 摘要只在新解析结果更长时替换（解析逻辑改进过，长的那份信息更全）

    默认只处理 RSS/Atom 源：它们各自只有一个 feed 地址，一次请求，几秒跑完。
    加 `include_api=True` 才会覆盖 HN / GitHub / arXiv——那会打真实的 API 额度，
    而且 HN 一次要并发取几百个 item，是「有明确需要时才跑」的操作。
    """

    from app.connectors.base import SourceSpec
    from app.connectors.registry import get_connector

    conditions = [Source.enabled.is_(True)]
    if not include_api:
        conditions.append(Source.access_method.in_(("rss", "atom")))
    sources = list((await session.execute(select(Source).where(*conditions))).scalars().all())

    counters: Counter[str] = Counter()
    for source in sources:
        counters["sources"] += 1
        try:
            outcome = await get_connector(SourceSpec.from_source(source)).fetch()
        # 单个源失败不影响其他源
        except Exception as exc:  # noqa: BLE001
            counters["source_failed"] += 1
            logger.warning(
                "backfill_fetch_failed",
                extra={"source_id": source.source_key, "error": f"{type(exc).__name__}: {exc}"},
            )
            continue

        by_external_id = {d.external_id: d for d in outcome.documents}
        if not by_external_id:
            continue

        rows = list(
            (
                await session.execute(
                    select(Document).where(
                        Document.source_id == source.id,
                        Document.external_id.in_(tuple(by_external_id)),
                    )
                )
            )
            .scalars()
            .all()
        )
        counters["matched"] += len(rows)
        for document in rows:
            derived = by_external_id[document.external_id]
            if document.simhash is None and derived.fingerprint is not None:
                document.simhash = derived.fingerprint
                counters["fingerprint_added"] += 1
            # 旧逻辑把 arXiv 摘要截到 600 字、把 release 正文复制成摘要副本，
            # 这里用新解析结果把更长的那份换回来
            if derived.summary and len(derived.summary) > len(document.summary or ""):
                document.summary = derived.summary
                counters["summary_improved"] += 1
            # 正文来源也一并修正：文本已经在 feed 里了，继续留在 pending
            # 只会让抓取队列白跑一遍然后失败。
            if derived.content_status == "from_feed":
                if document.content_status == "pending":
                    document.content_status = "from_feed"
                    counters["status_marked"] += 1
                # 这些标记是旧一轮抓取失败留下的。现在确认正文不该抓，
                # 留着它们会让前端把「源自上游」显示成红色的「原文无正文」，
                # 属于用过期结论描述当前状态。
                for stale in ("content_empty", "content_failed", "content_from_meta"):
                    _remove_flag(document, stale)

    await session.commit()
    summary = dict(counters)
    logger.info("backfill_fingerprints_done", extra=summary)
    return summary

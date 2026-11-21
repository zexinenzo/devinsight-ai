"""Hacker News 连接器（Firebase 官方 API）。

为什么用 Firebase 而不是 Algolia：
- Firebase 是 HN 官方数据源，字段口径与站点一致；Algolia 是第三方检索镜像，
  元数据有延迟，且排序规则与站点榜单不完全等价。
- 代价是 N+1 请求（先取 id 列表，再逐条取详情），所以这里用信号量限制在途并发，
  并按「已见 id 集合」做增量，避免每轮把整个榜单重拉一遍。

榜单选择（为什么是 topstories 而不是 newstories）：
- `newstories` 是「按发布先后」的流水，那一刻每条故事的 `score` 恒为 1。
  从它抓，等于「按时间全收」——库里 68% 的条目没有任何可抽取事实，
  很大一部分就是这么进来的（Show HN 的冷门小工具、个人随笔）。
- `topstories` 是按 score 排好的榜（500 条），榜单序本身就是热度信号。
  抓到的那一刻 score 已经是真实值，不需要等回访才知道哪条值得要。
- 代价必须说清：这是一份**滚动快照**，不是全集。从没进过前 500 的故事永远
  不会被看到；同步间隔越长，短暂上榜又快速回落的故事漏得越多。目前的
  `poll_interval_minutes=30` 与榜单轮换速度大致匹配，再稀就会明显漏。

增量策略（为什么不能再用 max_item_id）：
- 单调游标能成立的前提是「id 越大越新」，而榜单的 id 是**乱序**的：
  三天前发布的老帖今天可能重新回榜，id 比游标小得多。沿用 `id > 游标`
  会把它当成过期货直接过滤掉——而那恰恰是「按热度」最该抓的东西。
- 所以游标改成一份 `seen_ids` 集合：只抓「榜上有、但这份集合里没有」的条目，
  id 大小与是否见过无关。集合会随轮次增长，截断到最近 SEEN_IDS_LIMIT 个。
- 截断的后果是「掉出榜很久又回来」的条目会被重新请求一次，由
  `(source_id, external_id)` 唯一约束挡掉——多一次请求，不会产生重复行。

限流：
- Firebase 对普通读取没有硬性配额，但逐条请求容易形成突发流量，
  因此限制并发数，并把 429 明确抛成 RateLimitedError —— 让采集层知道这是
  "暂时别抓"而不是"这个源坏了"。

失败取舍：
- 单条 item 返回 null 是该条目已被删除，由过滤规则自然跳过；
- 单条详情请求失败只记 warning 并跳过，不阻断整批。原因是 HN 条目随时可能
  被删或 404，重试没有意义。
- 失败的条目也计入 seen：它已经消耗掉本轮的请求预算，不计入只会让下一轮
  再为一个死条目白试一次。代价是偶发的网络抖动会造成一次漏抓——这里选
  「漏一条」，不选「一堆死条目每轮重试、把 200 个名额慢慢堵满」。
  唯一约束保证了即便重复抓取也不会产生脏数据。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.connectors.base import BaseConnector, CanonicalDocument, FetchOutcome
from app.core.config import settings
from app.core.errors import RateLimitedError, UpstreamError
from app.core.logging import get_logger
from app.services.classifier import classify_topics
from app.utils.text import clean_html, content_hash, truncate
from app.utils.urls import canonicalize_url

logger = get_logger(__name__)

STORY_LIST_PATH = "topstories.json"
ITEM_PATH = "item/{item_id}.json"
ITEM_PAGE_TEMPLATE = "https://news.ycombinator.com/item?id={item_id}"
# Firebase 没有公布配额，这个数字是按"不制造突发流量"的经验值定的，不是官方限制
MAX_CONCURRENCY = 8
# seen_ids 的上限。榜单本身 500 条，留一倍余量给「已掉榜但短期内可能回榜」的条目。
# 按每个 id 约 7 字节算，1000 条约 7KB，存 JSONB 没有压力；不截断则会无限增长。
SEEN_IDS_LIMIT = 1000


def _parse_cursor(cursor: Any) -> tuple[list[int], int | None]:
    """把游标拆成 (已见 id 集合, 旧版 max_item_id)。

    两种格式要并存一段时间：改榜单那天之前入库的来源，`sync_cursor` 里只有
    `{"max_item_id": N}`。如果直接当作「没有游标」，第一次同步会把榜上 200 条
    全部重抓一遍——不算错（唯一约束挡得住），但白花 200 次请求。这里把它读成
    「id ≤ N 的都见过」，迁移轮只补新上榜的，下一轮写回 seen_ids 后就换代完成。
    """

    if not isinstance(cursor, dict):
        return [], None

    raw_seen = cursor.get("seen_ids")
    seen = [item for item in raw_seen if isinstance(item, int)] if isinstance(raw_seen, list) else []

    raw_legacy = cursor.get("max_item_id")
    legacy = raw_legacy if isinstance(raw_legacy, int) else None

    # 两者同时存在时以 seen_ids 为准：它信息更全，legacy 只是过渡期的拐杖
    return (seen, None) if seen else ([], legacy)


class HackerNewsConnector(BaseConnector):
    """HN 最新故事采集器。"""

    def __init__(self, spec, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(spec)
        self._client = client

    # --- 网络层 ---

    def _endpoint(self, path: str) -> str:
        return f"{self.spec.base_url.rstrip('/')}/{path}"

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> Any:
        try:
            response = await client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamError(f"{self.spec.source_key}: 网络请求失败（{type(exc).__name__}: {exc}）") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                seconds = float(retry_after) if retry_after else None
            except ValueError:
                seconds = None
            raise RateLimitedError(
                f"{self.spec.source_key}: 上游限流（HTTP 429）",
                retry_after_seconds=seconds,
            )
        if response.status_code >= 400:
            raise UpstreamError(
                f"{self.spec.source_key}: HTTP {response.status_code}",
                code="upstream_http_error",
            )
        return response.json()

    # --- 解析层 ---

    def _build_document(self, item: dict, fetched_at: datetime) -> CanonicalDocument | None:
        item_id = item.get("id")
        title = clean_html(item.get("title"))
        if item_id is None or not title:
            return None

        # Ask HN / Show HN 这类自帖没有外链，用讨论页本身做规范地址，
        # 否则这些条目会因为缺 url 被整批丢掉。
        raw_link = (item.get("url") or "").strip()
        has_external_link = bool(raw_link)
        if not raw_link:
            raw_link = ITEM_PAGE_TEMPLATE.format(item_id=item_id)

        canonical_url = canonicalize_url(raw_link)
        summary = clean_html(item.get("text"), max_chars=settings.summary_max_chars)

        # 自帖的 item.text 就是作者写的正文，直接当正文存（标 from_feed，不再抓网页）。
        # 外链故事没有 text，正文只能去站外抓，这里必须留空走 pending——
        # 早期版本把 summary 复制进 content_text，让「有正文」变成假象：
        # 指纹算的是这段文本，判重时却拿它跟网页正文比，永远对不上。
        body_text = truncate(summary, 4000) if summary and not has_external_link else None

        published_at = None
        if isinstance(item.get("time"), (int, float)):
            published_at = datetime.fromtimestamp(item["time"], tz=UTC)

        quality_flags: list[str] = []
        if published_at is None:
            quality_flags.append("missing_published_at")
        if not summary:
            quality_flags.append("missing_summary")
        if not (item.get("url") or "").strip():
            quality_flags.append("self_post")
        if item.get("dead") or item.get("deleted"):
            quality_flags.append("upstream_flagged")

        # 热度信号来自 item 详情本身，不需要额外请求：
        # score 是投票数，descendants 是整棵评论树的条数。
        # 从榜单抓到的条目已经过一轮投票筛选，这里记的就是「上榜此刻」的分数；
        # 但分数会随时间衰减（两天后从 400 掉到 200 是常态），所以回访任务仍然
        # 负责保留历史最高值，见 content_service._merge_engagement。
        engagement: dict[str, Any] = {}
        if isinstance(item.get("score"), int):
            engagement["score"] = item["score"]
            engagement["primary"] = item["score"]
        if isinstance(item.get("descendants"), int):
            engagement["comments"] = item["descendants"]

        return CanonicalDocument(
            source_key=self.spec.source_key,
            external_id=str(item_id),
            url=raw_link,
            canonical_url=canonical_url,
            title=title,
            summary=summary,
            content_text=body_text,
            # 自帖正文已在手，不必再抓网页；外链故事留给 enrich 按热度补
            content_status="from_feed" if body_text else None,
            language="en",
            region=self.spec.region,
            content_type="article",
            published_at=published_at,
            fetched_at=fetched_at,
            content_hash=content_hash(title, canonical_url, summary),
            topics=classify_topics(title, summary),
            rights_mode=self.spec.rights_mode,
            quality_flags=quality_flags,
            engagement=engagement,
        )

    # --- 入口 ---

    async def fetch(self, cursor: dict[str, Any] | None = None) -> FetchOutcome:
        started = time.perf_counter()
        active_cursor = cursor if cursor is not None else self.spec.cursor
        seen_ids, legacy_max_id = _parse_cursor(active_cursor)

        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.spec.request_timeout_seconds),
            follow_redirects=True,
            max_redirects=3,
            headers={"User-Agent": settings.http_user_agent, "Accept": "application/json"},
        )

        try:
            story_ids = await self._get_json(client, self._endpoint(STORY_LIST_PATH))
            if not isinstance(story_ids, list):
                raise UpstreamError(
                    f"{self.spec.source_key}: 榜单响应不是数组", code="upstream_schema_error"
                )

            story_ids = [item for item in story_ids if isinstance(item, int)]
            batch, next_cursor = self._select_batch(story_ids, seen_ids, legacy_max_id)

            semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

            async def _load(item_id: int):
                async with semaphore:
                    try:
                        return await self._get_json(
                            client, self._endpoint(ITEM_PATH.format(item_id=item_id))
                        )
                    except RateLimitedError:
                        raise
                    except UpstreamError as exc:
                        logger.warning(
                            "hn_item_failed",
                            extra={"source_id": self.spec.source_key, "item_id": item_id, "error": str(exc)[:200]},
                        )
                        return None

            items = await asyncio.gather(*(_load(item_id) for item_id in batch))
        finally:
            if own_client:
                await client.aclose()

        latency_ms = int((time.perf_counter() - started) * 1000)
        fetched_at = datetime.now(UTC)

        documents: list[CanonicalDocument] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "story":
                continue
            document = self._build_document(item, fetched_at)
            if document is None or document.canonical_url in seen:
                continue
            seen.add(document.canonical_url)
            documents.append(document)

        if not batch:
            logger.info("hn_no_new_items", extra={"source_id": self.spec.source_key})

        return FetchOutcome(
            documents=documents,
            http_status=200,
            latency_ms=latency_ms,
            next_cursor=next_cursor,
        )

    def _select_batch(
        self,
        story_ids: list[int],
        seen_ids: list[int],
        legacy_max_id: int | None = None,
    ) -> tuple[list[int], dict[str, Any] | None]:
        """从榜单里挑出没见过的那批，并给出下一轮的游标。

        榜单本身已按热度降序，所以「保留榜单顺序」等于「按热度优先」——
        超出 max_items 而被截掉的永远是榜尾，而不是「id 最大」的那几条。
        这正是旧实现（按 id 升序）做不到的：它会把昨天的高分老帖排在今天
        刚上榜的低分新帖后面。

        没有新条目时返回上一轮的 seen_ids，而不是 None。None 会被采集层理解成
        「游标不动」，虽然结果一样，但显式返回能让「这轮看过榜单、确实没有新的」
        和「这轮没跑」在库里有区别。
        """

        if not story_ids:
            # 榜单为空是上游异常，不覆盖已有游标——否则一次空响应就把历史清空
            return [], ({"seen_ids": seen_ids} if seen_ids else None)

        seen = set(seen_ids)
        candidates = [
            item_id
            for item_id in story_ids
            if item_id not in seen and (legacy_max_id is None or item_id > legacy_max_id)
        ]
        batch = candidates[: self.spec.max_items]

        # 只把本轮真去请求过的计入 seen。被 max_items 截掉的下轮还会再出现，
        # 它们没有「处理过」，不该被记成已见。
        merged = seen_ids + batch
        deduped = list(dict.fromkeys(merged))[-SEEN_IDS_LIMIT:]
        return batch, {"seen_ids": deduped}

"""Hacker News 连接器（Firebase 官方 API）。

为什么用 Firebase 而不是 Algolia：
- Firebase 是 HN 官方数据源，字段口径与站点一致；Algolia 是第三方检索镜像，
  元数据有延迟，且排序规则与站点榜单不完全等价。
- 代价是 N+1 请求（先取 id 列表，再逐条取详情），所以这里用信号量限制在途并发，
  并按 item id 做增量，避免每轮把整个榜单重拉一遍。

增量策略：
- HN 的 item id 全局单调递增，新故事一定拿到更大的 id；
- 游标只需记 max_item_id，下一轮只处理 id 更大的条目；
- 候选按 id 升序处理，超出 max_items 的部分留到下一轮，保证不跳条目不漏条目。

限流：
- Firebase 对普通读取没有硬性配额，但逐条请求容易形成突发流量，
  因此限制并发数，并把 429 明确抛成 RateLimitedError —— 让采集层知道这是
  "暂时别抓"而不是"这个源坏了"。

失败取舍：
- 单条 item 返回 null 是该条目已被删除，由过滤规则自然跳过；
- 单条详情请求失败只记 warning 并跳过，不阻断整批。原因是 HN 条目随时可能
  被删或 404，重试没有意义；而游标一旦因此卡住，会让后续所有新条目都进不来。
  唯一约束保证了即便重复抓取也不会产生脏数据。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
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

STORY_LIST_PATH = "newstories.json"
ITEM_PATH = "item/{item_id}.json"
ITEM_PAGE_TEMPLATE = "https://news.ycombinator.com/item?id={item_id}"
# Firebase 没有公布配额，这个数字是按"不制造突发流量"的经验值定的，不是官方限制
MAX_CONCURRENCY = 8


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
        if not raw_link:
            raw_link = ITEM_PAGE_TEMPLATE.format(item_id=item_id)

        canonical_url = canonicalize_url(raw_link)
        summary = clean_html(item.get("text"), max_chars=settings.summary_max_chars)

        published_at = None
        if isinstance(item.get("time"), (int, float)):
            published_at = datetime.fromtimestamp(item["time"], tz=timezone.utc)

        quality_flags: list[str] = []
        if published_at is None:
            quality_flags.append("missing_published_at")
        if not summary:
            quality_flags.append("missing_summary")
        if not (item.get("url") or "").strip():
            quality_flags.append("self_post")
        if item.get("dead") or item.get("deleted"):
            quality_flags.append("upstream_flagged")

        return CanonicalDocument(
            source_key=self.spec.source_key,
            external_id=str(item_id),
            url=raw_link,
            canonical_url=canonical_url,
            title=title,
            summary=summary,
            content_text=truncate(summary, 4000) if summary else None,
            language="en",
            region=self.spec.region,
            content_type="article",
            published_at=published_at,
            fetched_at=fetched_at,
            content_hash=content_hash(title, canonical_url, summary),
            topics=classify_topics(title, summary),
            rights_mode=self.spec.rights_mode,
            quality_flags=quality_flags,
        )

    # --- 入口 ---

    async def fetch(self, cursor: dict[str, Any] | None = None) -> FetchOutcome:
        started = time.perf_counter()
        active_cursor = cursor if cursor is not None else self.spec.cursor
        last_max_id = None
        if isinstance(active_cursor, dict):
            raw_last = active_cursor.get("max_item_id")
            if isinstance(raw_last, int):
                last_max_id = raw_last

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
            batch, next_max_id = self._select_batch(story_ids, last_max_id)

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
        fetched_at = datetime.now(timezone.utc)

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
            next_cursor={"max_item_id": next_max_id} if next_max_id else None,
        )

    def _select_batch(self, story_ids: list[int], last_max_id: int | None) -> tuple[list[int], int | None]:
        """挑出本轮要处理的 id，并给出下一轮的游标。

        首次运行：取榜单最新的 max_items 条作为基线。
        之后：只取 id 大于游标的条目，按 id 升序处理，超出部分留给下一轮 ——
        这样即便某轮只处理了一部分，游标也不会跳过没处理的条目。
        """

        if not story_ids:
            return [], last_max_id

        if last_max_id is None:
            batch = story_ids[: self.spec.max_items]
            return batch, max(story_ids)

        candidates = sorted(item for item in story_ids if item > last_max_id)
        if not candidates:
            return [], last_max_id

        batch = candidates[: self.spec.max_items]
        return batch, max(batch)

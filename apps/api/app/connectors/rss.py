"""RSS 连接器。

实现要点（对应手册 12.2 / 12.3）：
- 统一 User-Agent，声明联系方式；
- 429/5xx 与网络错误按指数退避重试，并优先遵守 Retry-After；
- feed 内的 HTML 一律视为不可信输入，清洗为纯文本后再入库；
- 发布时间解析失败保留 None，不用采集时间顶替。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import feedparser
import httpx
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.connectors.base import BaseConnector, CanonicalDocument, FetchOutcome
from app.core.config import settings
from app.core.errors import UpstreamError
from app.core.logging import get_logger
from app.services.classifier import classify_topics
from app.utils.dates import is_implausible, parse_any, parse_struct_time
from app.utils.text import clean_html, content_hash, truncate
from app.utils.urls import canonicalize_url

logger = get_logger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


class RetryableUpstreamStatus(Exception):
    """可重试的上游状态码（4xx 客户端错误不在此列）。"""

    def __init__(self, status_code: int, retry_after: float | None) -> None:
        super().__init__(f"retryable upstream status: {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), 60.0)
    except ValueError:
        return None


def _wait_strategy(state: RetryCallState) -> float:
    """有 Retry-After 就听上游的，否则指数退避（2s→30s 封顶）。"""

    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, RetryableUpstreamStatus) and exc.retry_after:
        return exc.retry_after
    return wait_exponential(multiplier=1, min=2, max=30)(state)


class RSSConnector(BaseConnector):
    """通用 RSS / Atom 采集器。"""

    def __init__(self, spec, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(spec)
        self._client = client

    # --- 网络层 ---

    async def _request(self, client: httpx.AsyncClient) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=_wait_strategy,
            retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError, RetryableUpstreamStatus)),
            reraise=True,
        ):
            with attempt:
                response = await client.get(self.spec.base_url)
                if response.status_code in RETRYABLE_STATUS:
                    raise RetryableUpstreamStatus(response.status_code, _retry_after_seconds(response))
                return response
        raise UpstreamError(f"{self.spec.source_key}: 重试次数耗尽")  # pragma: no cover

    # --- 解析层 ---

    def _build_document(self, entry, feed_language: str | None, fetched_at: datetime) -> CanonicalDocument | None:
        raw_link = (entry.get("link") or "").strip()
        title = clean_html(entry.get("title"))
        if not raw_link or not title:
            return None

        canonical_url = canonicalize_url(raw_link)
        summary = clean_html(
            entry.get("summary") or entry.get("description"), max_chars=settings.summary_max_chars
        )
        content_text = None
        if entry.get("content"):
            try:
                content_text = clean_html(entry["content"][0].get("value"), max_chars=4000)
            except (IndexError, AttributeError, TypeError):
                content_text = None

        published_at = (
            parse_struct_time(entry.get("published_parsed"))
            or parse_struct_time(entry.get("updated_parsed"))
            or parse_any(entry.get("published"))
            or parse_any(entry.get("updated"))
        )

        quality_flags: list[str] = []
        if published_at is None:
            quality_flags.append("missing_published_at")
        elif is_implausible(published_at, fetched_at):
            quality_flags.append("implausible_published_at")
        if not summary:
            quality_flags.append("missing_summary")

        language = (entry.get("language") or feed_language or self.spec.language or None)
        external_id = str(entry.get("id") or entry.get("guid") or canonical_url)

        return CanonicalDocument(
            source_key=self.spec.source_key,
            external_id=external_id,
            url=raw_link,
            canonical_url=canonical_url,
            title=title,
            summary=summary,
            content_text=truncate(content_text, 4000),
            language=language,
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

    async def fetch(self, cursor: str | None = None) -> FetchOutcome:
        started = time.perf_counter()
        headers = {
            "User-Agent": settings.http_user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        }

        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.spec.request_timeout_seconds),
            follow_redirects=True,
            max_redirects=3,
            headers=headers,
        )

        try:
            response = await self._request(client)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamError(f"{self.spec.source_key}: 网络请求失败（{type(exc).__name__}）") from exc
        except RetryableUpstreamStatus as exc:
            raise UpstreamError(f"{self.spec.source_key}: 上游持续返回 {exc.status_code}") from exc
        finally:
            if own_client:
                await client.aclose()

        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            raise UpstreamError(
                f"{self.spec.source_key}: HTTP {response.status_code}",
                code="upstream_http_error",
            )

        fetched_at = datetime.now(timezone.utc)
        parsed = await asyncio.to_thread(feedparser.parse, response.content)
        entries = parsed.get("entries") or []

        if not entries:
            if parsed.get("bozo"):
                raise UpstreamError(
                    f"{self.spec.source_key}: feed 解析失败（{parsed.get('bozo_exception')}）",
                    code="feed_parse_error",
                )
            logger.warning("empty_feed", extra={"source_id": self.spec.source_key})

        feed_language = (parsed.get("feed") or {}).get("language")
        documents: list[CanonicalDocument] = []
        seen: set[str] = set()

        for entry in entries[: self.spec.max_items]:
            document = self._build_document(entry, feed_language, fetched_at)
            if document is None:
                continue
            if document.canonical_url in seen:  # 同一次响应内部的重复条目
                continue
            seen.add(document.canonical_url)
            documents.append(document)

        return FetchOutcome(
            documents=documents,
            http_status=response.status_code,
            latency_ms=latency_ms,
        )

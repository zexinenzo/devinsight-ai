"""实时搜索：把「库外还能查到什么」这条腿接上。

**为什么需要它。** 本地库只覆盖已采集的来源，而采集是周期性的——问「今天发生了什么」，
本地库天然答不上来。这不是召回算法的问题，是语料边界的问题。所以 Agent 必须先有
两条腿：本地检索（深、可追溯到原文）和实时搜索（新、但不可信）。

**为什么这个模块的重点是「不可信」而不是「能查」。** 实测两条真实结果：

1. `topic=general`（默认）不返回任何日期字段。同一句 query，分数最高的一条
   （0.79）是 2023 年 GPT-4 开放 API 的旧闻——它确实「相关」，但拿它回答
   「最新进展」就是错的，而且从响应里看不出它有多旧。
2. `topic=news` + `days=7` 才带 `published_date`，且真的做了时间窗过滤
   （实测返回全部落在 3 天内）。

所以默认走 news：**拿得到日期比拿得到覆盖面更重要**。日期不是装饰字段，
它是判断一条证据该不该被引用的前提。

**两类异常，对应两种 HTTP 语义。** `WebSearchUnavailable` 是「这个部署没配」，
调用方要求实时能力时应得 503；`WebSearchFailed` 是「配了但对方现在不行」
（限流、超时、5xx），应得 502。混成一个异常会让「没配」和「挂了」在监控里长得一样。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.utils.dates import parse_any

logger = get_logger(__name__)

# news 模式的时间窗只有 1~30 天有效，超出会被服务端拒绝。把边界写在这里，
# 而不是让调用方在运行到一半时才发现自己传了 days=90。
MIN_DAYS = 1
MAX_DAYS = 30


class WebSearchUnavailable(RuntimeError):
    """没有可用的实时搜索后端（未配置 provider / key）。"""


class WebSearchFailed(RuntimeError):
    """后端可用但本次调用失败（限流、超时、上游 5xx）。"""


@dataclass(slots=True)
class WebHit:
    """一条实时搜索命中。

    `date_verified` 是这个结构里最重要的字段：它区分「我们知道它发布于什么时候」
    和「我们不知道，只能猜」。没有日期的结果不能被当作「最新事实」引用——把它
    和带日期的结果混在一起排个序，等于用未知冒充已知。
    """

    url: str
    title: str
    snippet: str
    published_at: datetime | None
    score: float | None
    # False 表示上游没给日期（general 模式固定如此），或给了但解析不出来
    date_verified: bool
    # 上游原始日期字符串，解析失败时留在日志里便于排查，不往外透
    raw_date: str | None = None


class SearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        limit: int,
        days: int,
        topic: str,
    ) -> list[WebHit]:
        """返回命中的网页列表，按上游给出的相关性排序。"""
        ...


def _time_range_for(days: int) -> str:
    """general 模式没有 `days` 参数，只有粗粒度的 time_range。

    映射是「取不小于 days 的那一档」：问 7 天就给 week，不会因为 days=7 而漏掉
    刚好第 7 天的结果。粗粒度是上游限制，不该靠加倍窗口来补偿——那会把旧结果放进来。
    """

    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"


def parse_results(payload: dict[str, Any], *, provider: str = "tavily") -> list[WebHit]:
    """把上游响应解析成 `WebHit`。纯函数，不碰网络。

    抽出来是为了让「额度用尽时还能验证解析」成为可能：把一次真实响应存成 JSON
    回放，走的是同一段代码，不是另写一份假的解析。否则额度一到上限，整条链路
    就没法验证，只能等第二天。
    """

    hits: list[WebHit] = []
    for item in payload.get("results") or []:
        hit_url = str(item.get("url") or "").strip()
        if not hit_url:
            # 没有 URL 的结果无法回溯原文，等于不可引用的证据，直接丢
            continue
        raw_date = item.get("published_date") or item.get("published_at")
        published = parse_any(raw_date if isinstance(raw_date, str) else None)
        score = item.get("score")
        hits.append(
            WebHit(
                url=hit_url,
                title=str(item.get("title") or "").strip(),
                snippet=str(item.get("content") or "").strip(),
                published_at=published,
                score=float(score) if isinstance(score, (int, float)) else None,
                date_verified=published is not None,
                raw_date=raw_date if isinstance(raw_date, str) else None,
            )
        )

    verified = sum(1 for hit in hits if hit.date_verified)
    logger.info(
        "web_search_done",
        extra={
            "provider": provider,
            "hits": len(hits),
            "dated": verified,
            "results": len(payload.get("results") or []),
        },
    )
    if hits and verified == 0:
        # 这条日志是给未来的自己看的：如果哪天 provider 改了字段名，
        # 症状就是「日期全空、结果看起来都不可信」，而不是抛异常。
        logger.warning("web_search_no_dates", extra={"provider": provider})
    return hits


class TavilyProvider:
    """Tavily 的实现。

    keyless 与 keyed 走同一套请求体与解析：实测响应 schema 一致，所以「先用
    keyless 开发、后面换 key」不需要分叉代码。只有鉴权头不同。
    """

    name = "tavily"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        keyless: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.keyless = keyless
        self.timeout = timeout
        if not self.api_key and not self.keyless:
            raise WebSearchUnavailable(
                "web_search_provider=tavily 但没有 key，且 keyless 已关闭。\n"
                "  要么设 WEB_SEARCH_API_KEY，要么设 WEB_SEARCH_KEYLESS=true"
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # 同时给 keyless 头和 key 时，key 优先（上游文档明确写了）。
            # 这里不两个都发：让「有没有 key」这件事在请求里只有一种表达。
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            headers["X-Tavily-Access-Mode"] = "keyless"
        return headers

    def _body(self, query: str, *, limit: int, days: int, topic: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query,
            "max_results": max(1, min(limit, 20)),
            # basic 是 1 credit，advanced 是 2 且延迟更高。阶段 5 要的是「有什么新东西」，
            # 不是「最全」，用 basic。
            "search_depth": "basic",
            "include_raw_content": False,
            "include_answer": False,
        }
        if topic == "news":
            body["topic"] = "news"
            body["days"] = max(MIN_DAYS, min(days, MAX_DAYS))
        else:
            body["topic"] = "general"
            # general 给不了精确窗口，只能给档位；也正因如此它很可能不带日期
            body["time_range"] = _time_range_for(days)
        return body

    async def search(
        self,
        query: str,
        *,
        limit: int,
        days: int,
        topic: str,
    ) -> list[WebHit]:
        url = f"{self.base_url}/search"
        body = self._body(query, limit=limit, days=days, topic=topic)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=body, headers=self._headers())
        except httpx.TimeoutException as error:
            raise WebSearchFailed(f"实时搜索超时（{self.timeout:.0f}s）：{error}") from error
        except httpx.HTTPError as error:
            raise WebSearchFailed(f"实时搜索网络错误：{error}") from error

        if response.status_code == 429:
            # keyless 的额度是**每日**上限，不是每小时。这一点实测纠正过一次：
            # 文档与第三方评测里「30 次/小时」的说法都有，但上游实际返回的错误码是
            # `daily_cap_reached`。文案写错单位会让调用方一直重试到明天。
            detail = response.text[:300]
            raise WebSearchFailed(
                "实时搜索额度已用尽（429）。keyless 有每日上限（约 30 次/天），"
                "次日恢复；配 WEB_SEARCH_API_KEY 可提到 1000 次/月且按小时限流。"
                f" 上游原文：{detail}"
            )
        if response.status_code in (401, 403):
            raise WebSearchFailed(
                f"实时搜索鉴权失败（{response.status_code}）。key 无效或 keyless 已停用。"
                f" 上游原文：{response.text[:200]}"
            )
        if response.status_code >= 500:
            raise WebSearchFailed(
                f"实时搜索上游故障（{response.status_code}）：{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise WebSearchFailed(
                f"实时搜索请求被拒（{response.status_code}）：{response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise WebSearchFailed(f"实时搜索返回了非 JSON 内容：{response.text[:200]}") from error

        logger.info(
            "web_search_request",
            extra={
                "provider": self.name,
                "topic": topic,
                "days": days,
                "limit": limit,
            },
        )
        return parse_results(payload, provider=self.name)


# --- 进程内单例：与 embedding / rerank 保持同一套装配方式 ---

_lock = threading.Lock()
_provider: SearchProvider | None = None
# 「没配置」是一个合法且稳定的结果，必须和「还没构建」区分开：
# 只用 `_provider is None` 判断的话，没配置的部署每次请求都会重新进锁、重新解析配置。
_initialized = False


def build_web_search() -> SearchProvider | None:
    """按配置构造 provider。返回 None 表示这项能力没启用（不是错误）。"""

    provider_name = (settings.web_search_provider or "").strip().lower()
    if not provider_name:
        return None
    if provider_name == "tavily":
        return TavilyProvider(
            base_url=settings.web_search_base_url,
            api_key=settings.web_search_api_key,
            keyless=settings.web_search_keyless,
            timeout=settings.http_timeout_seconds,
        )
    raise WebSearchUnavailable(
        f"未知的 web_search_provider：{provider_name}（目前支持 tavily）"
    )


def get_web_search() -> SearchProvider | None:
    global _provider, _initialized
    if _initialized:
        return _provider
    with _lock:
        if not _initialized:
            _provider = build_web_search()
            _initialized = True
            if _provider is not None:
                logger.info("web_search_ready", extra={"provider": _provider.name})
    return _provider


def set_web_search(provider: SearchProvider | None) -> None:
    """测试注入用。传 None 表示「这轮当作没配」。"""

    global _provider, _initialized
    with _lock:
        _provider = provider
        _initialized = True

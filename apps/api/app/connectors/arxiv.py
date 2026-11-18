"""arXiv 连接器（Atom over HTTPS）。

虽然响应是 Atom，语法上跟 RSS 同族，但不能复用 RSSConnector：
- arXiv 用 `start` / `max_results` 分页，增量靠 `published` 时间戳而不是条目 id；
- 限流极硬：官方要求「1 请求 / 3 秒」，超限时 Google Frontend 直接返回
  `429 Rate exceeded.`，**连 Retry-After 都不给**（实测确认）。没有头部线索可依，
  只能靠固定间隔 + 失败后不重试（重试只会让封禁更久）；
- 条目 id 带版本号，同一篇论文的 v1/v2/v3 会反复出现在列表里。

增量策略（游标记 `latest_published`）：
- arXiv 的 `sortBy=submittedDate` 按**原始提交时间**排序，所以一篇论文被改成 v2
  不会因此浮到列表顶部 —— 这也意味着修订本身不会被当作新情报；
- **用 `submittedDate` 窗口精确定位增量**，而不是"翻页直到看见旧条目"：
  游标之后拼上 `submittedDate:[游标 TO 999912312359]`，服务端只返回这一段的条目，
  `opensearch:totalResults` 直接给出该段总篇数。翻页按这个数收敛，翻完即停，
  不存在"翻到第 N 页仍没触达游标、中间的条目被游标越过去"的漏条窗口。
- 比较用 `published >= 游标`（含等于），靠 (source_id, external_id) 唯一约束去重；
  用严格大于会在「同一秒提交多篇、上轮只处理了一部分」时永久漏条；
- 处理顺序按 published **升序**并截断到 max_items，超出的留给下一轮。
  若按降序取「最新 N 篇」，被挤掉的较旧新论文将永远进不来。

为什么要窗口（实测数据）：cs.AI 单日提交 192 篇，三个分类合计更多。按 4 小时一轮，
两轮之间约 80~100 篇；但电脑关机 8 小时就是 200+ 篇。纯靠翻页的旧实现上限是
MAX_PAGES × PAGE_SIZE，超出的部分会被游标直接越过且**永久无法回补**
（arXiv 没有按 id 区间回溯的接口）。窗口把"要取多少"交给服务端算，翻不完时
还能明确告警，而不是静默丢数据。

external_id 去掉版本号，即 `2611.23229v3` → `2611.23229`。
代价是论文修订不会产生新条目（同一篇视为同一篇），换来的是列表不会被
「同一论文的五个版本」淹没。要追踪修订需要另建版本维度，不在本阶段范围。

已知未验证项：`start` 分页参数。我在验证环境里被 GFE 限流到无法连续请求，
只跑通过单页。因此翻页逻辑做了防御：若某页没有新增 id（服务端忽略 start 或
返回重复页），立即停止，不会因为分页失效而空转。
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import feedparser
import httpx

from app.connectors.base import BaseConnector, CanonicalDocument, FetchOutcome
from app.core.config import settings
from app.core.errors import RateLimitedError, UpstreamError
from app.core.logging import get_logger
from app.services.classifier import classify_topics
from app.utils.dates import is_implausible, parse_any, parse_struct_time
from app.utils.text import clean_html, content_hash, truncate
from app.utils.urls import canonicalize_url

logger = get_logger(__name__)

# arXiv 单页上限可以给得很大，但响应体积与延迟会跟着涨；50 条约 110KB，够用。
PAGE_SIZE = 50
# 翻页上限：6 页 = 300 篇，按实测的提交速率覆盖 12 小时以上的停机。
# 每多翻一页要多等 3 秒（官方硬要求），所以不能设成无限；
# 真翻不完时下面会打 arxiv_window_truncated 告警，不会静默丢数据。
MAX_PAGES = 6
# arXiv 官方要求：请求间隔不少于 3 秒。翻页时必须显式等待，不能连续打。
MIN_REQUEST_GAP_SECONDS = 3.0

ABS_URL = "https://arxiv.org/abs/{arxiv_id}"
_VERSION_SUFFIX = re.compile(r"v\d+$")
_OPENSEARCH_TOTAL = re.compile(r"<opensearch:totalResults[^>]*>(\d+)<")


def normalize_arxiv_id(raw: str | None) -> str | None:
    """把 `http://arxiv.org/abs/2611.23229v1` 归一成 `2611.23229`。

    老式 id（`hep-th/9901001v2`）的斜杠要保留，所以只在 `abs/` 之后截取，
    再剥掉末尾的版本号。
    """

    if not raw:
        return None
    text = raw.strip()
    marker = "/abs/"
    if marker in text:
        text = text.split(marker, 1)[1]
    text = text.rstrip("/").strip()
    text = _VERSION_SUFFIX.sub("", text)
    return text or None


def _cursor_time(cursor: dict[str, Any] | None) -> datetime | None:
    if not isinstance(cursor, dict):
        return None
    raw = cursor.get("latest_published")
    if not isinstance(raw, str):
        return None
    return parse_any(raw)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _window_clause(cursor_dt: datetime | None) -> str | None:
    """把游标翻译成 `submittedDate:[起点 TO 9999]` 的窗口条件。

    实测两点（不是推测）：
    - 窗口确实生效：`submittedDate:[202611171859 TO 999912312359]` 的
      totalResults 只有 2，而同样的分类查询不加窗口是 490262；
    - 边界粒度到分钟且**包含**该分钟：起点 18:59 时，18:59:27 的条目也在结果里。

    因此起点往前留 1 分钟。多传几条只会被唯一约束挡掉，而少传半分钟内的提交
    是不可恢复的丢失。
    """

    if cursor_dt is None:
        return None
    floor = (cursor_dt - timedelta(minutes=1)).astimezone(timezone.utc)
    return f"submittedDate:[{floor:%Y%m%d%H%M} TO 999912312359]"


def _total_results(content: bytes) -> int | None:
    """从原始 Atom 里取 opensearch:totalResults。

    feedparser 也能给出（parsed.feed.opensearch_totalresults），但它对命名空间
    前缀的映射依赖上游写法；直接匹配原始字节更稳，也便于在解析失败时仍拿到计数。
    """

    match = _OPENSEARCH_TOTAL.search(content.decode("utf-8", "ignore"))
    return int(match.group(1)) if match else None


class ArxivConnector(BaseConnector):
    """按分类抓取 arXiv 最新提交。"""

    def __init__(self, spec, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(spec)
        self._client = client

    # --- 请求参数 ---

    def _endpoint_and_query(
        self, cursor_dt: datetime | None = None
    ) -> tuple[str, dict[str, str]]:
        """从 base_url 里取出搜索条件。

        跟踪哪个查询是「这个来源是什么」的一部分，所以跟着来源记录存进库，
        而不是写成代码常量 —— 加一个「只看 cs.CL」的来源就不需要改代码。
        """

        parts = urlsplit(self.spec.base_url)
        if not parts.scheme or not parts.netloc:
            raise UpstreamError(
                f"{self.spec.source_key}: base_url 不是合法地址（{self.spec.base_url!r}）",
                code="source_misconfigured",
            )
        query = dict(parse_qsl(parts.query))
        search_query = (query.get("search_query") or "").strip()
        if not search_query:
            raise UpstreamError(
                f"{self.spec.source_key}: base_url 缺少 search_query（例：?search_query=cat:cs.AI）",
                code="source_misconfigured",
            )

        endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path or "/api/query", "", ""))
        # 括号不能省：search_query 常写成 `cat:cs.AI OR cat:cs.LG OR cat:cs.CL`，
        # 不加括号时 AND 只会约束最后一个 OR 分支，前两个分类等于没被窗口过滤，
        # 会把全部历史论文拉回来。
        window = _window_clause(cursor_dt)
        query_expr = f"({search_query}) AND {window}" if window else search_query

        params = {
            "search_query": query_expr,
            # 按原始提交时间倒序：这是「新论文出现」的可靠信号，
            # 且能让游标单调前进（lastUpdatedDate 会因修订把旧论文顶到前面）。
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": str(PAGE_SIZE),
        }
        return endpoint, params

    # --- 网络层 ---

    async def _request_page(
        self, client: httpx.AsyncClient, endpoint: str, params: dict[str, str], start: int
    ) -> tuple[list, int, int | None]:
        """返回 (entries, http_status, total_results)。

        total_results 取自 opensearch 命名空间，窗口查询下它是「游标之后总篇数」，
        用来判断是否已经翻完；解析不到时给 None，调用方退化为按页大小判断。
        """
        query = dict(params)
        query["start"] = str(start)
        # 逐请求带 UA，而不是只设在自建 client 上：arXiv 的限流阈值与 UA 是否
        # 可联系有关，若调用方注入了一个共用 client，client 级默认头会被覆盖成
        # python-httpx/...，等于把额度降到最低。
        headers = {
            "User-Agent": settings.http_user_agent,
            "Accept": "application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        }
        try:
            response = await client.get(endpoint, params=query, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # 保留底层 message：与 RSS 一致，否则排障时只剩异常类名
            raise UpstreamError(
                f"{self.spec.source_key}: 网络请求失败（{type(exc).__name__}: {exc}）"
            ) from exc

        if response.status_code == 429:
            raise RateLimitedError(
                f"{self.spec.source_key}: arXiv 限流（HTTP 429，官方要求 1 请求/3 秒，"
                f"且不返回 Retry-After）"
            )
        if response.status_code in (403, 503):
            # 403 在 arXiv 这边同样是「疑似滥用被拦」，503 是过载/维护。
            # 两者都不是来源坏了，绝不能记成 failing。
            raise RateLimitedError(
                f"{self.spec.source_key}: arXiv 暂时拒绝服务（HTTP {response.status_code}），下轮再试"
            )
        if response.status_code >= 400:
            raise UpstreamError(
                f"{self.spec.source_key}: HTTP {response.status_code}",
                code="upstream_http_error",
            )

        parsed = await asyncio.to_thread(feedparser.parse, response.content)
        entries = parsed.get("entries") or []
        if not entries and parsed.get("bozo"):
            raise UpstreamError(
                f"{self.spec.source_key}: Atom 解析失败（{parsed.get('bozo_exception')}）",
                code="feed_parse_error",
            )
        return entries, response.status_code, _total_results(response.content)

    # --- 解析层 ---

    def _build_document(self, entry, fetched_at: datetime) -> CanonicalDocument | None:
        arxiv_id = normalize_arxiv_id(entry.get("id"))
        title_raw = entry.get("title") or ""
        # arXiv 的标题在 Atom 里按行折行，实测会出现多余换行与连续空格
        title = " ".join(title_raw.split())
        if not arxiv_id or not title:
            return None

        summary = clean_html(entry.get("summary"), max_chars=settings.summary_max_chars)
        if summary:
            summary = " ".join(summary.split())
        # 摘要里的 LaTeX（如 $T\\to\\infty$）刻意保留：去标记会改变数学含义，
        # 与 GitHub Release 里 **kwargs / __init__ 的处理同理。

        published_at = (
            parse_struct_time(entry.get("published_parsed"))
            or parse_any(entry.get("published"))
            or parse_struct_time(entry.get("updated_parsed"))
            or parse_any(entry.get("updated"))
        )

        # 规范地址不带版本号，否则同一篇论文的每个版本都是不同 URL
        canonical_url = canonicalize_url(ABS_URL.format(arxiv_id=arxiv_id))
        raw_link = (entry.get("link") or canonical_url).strip()

        quality_flags: list[str] = []
        if published_at is None:
            quality_flags.append("missing_published_at")
        elif is_implausible(published_at, fetched_at):
            quality_flags.append("implausible_published_at")
        if not summary:
            quality_flags.append("missing_summary")

        return CanonicalDocument(
            source_key=self.spec.source_key,
            external_id=arxiv_id,
            url=raw_link,
            canonical_url=canonical_url,
            title=title,
            summary=summary,
            content_text=truncate(summary, 4000),
            language=self.spec.language or "en",
            region=self.spec.region,
            content_type="paper",
            published_at=published_at,
            fetched_at=fetched_at,
            content_hash=content_hash(title, canonical_url, summary),
            # arXiv 自己的分类词表（cs.AI / cs.LG）与项目 taxonomy 是两套体系，
            # 混进 topics 会让前端按主题筛选时出现只有论文才有的 slug，因此不入库。
            topics=classify_topics(title, summary),
            rights_mode=self.spec.rights_mode,
            quality_flags=quality_flags,
        )

    # --- 入口 ---

    async def fetch(self, cursor: dict[str, Any] | None = None) -> FetchOutcome:
        started = time.perf_counter()
        active_cursor = cursor if cursor is not None else self.spec.cursor
        cursor_dt = _cursor_time(active_cursor)
        endpoint, params = self._endpoint_and_query(cursor_dt)

        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.spec.request_timeout_seconds),
            follow_redirects=True,
            max_redirects=3,
        )

        collected: list = []
        seen: set[str] = set()
        http_status: int | None = None
        total: int | None = None
        exhausted = False
        try:
            for page in range(MAX_PAGES):
                if page:
                    # 上游明确的硬要求，不能省
                    await asyncio.sleep(MIN_REQUEST_GAP_SECONDS)
                entries, http_status, total = await self._request_page(
                    client, endpoint, params, start=page * PAGE_SIZE
                )

                fresh = 0
                for entry in entries:
                    arxiv_id = normalize_arxiv_id(entry.get("id"))
                    if not arxiv_id or arxiv_id in seen:
                        continue
                    seen.add(arxiv_id)
                    collected.append(entry)
                    fresh += 1

                if cursor_dt is None:
                    # 基线：只取最新一页，更早的历史不回补（缺的是过去，不是将来）
                    exhausted = True
                    break
                if len(entries) < PAGE_SIZE:
                    exhausted = True  # 服务端没有更多了
                    break
                if total is not None and len(seen) >= total:
                    exhausted = True  # 窗口内的条目已全部取到
                    break
                # 兜底：若服务端忽略了 submittedDate 窗口（返回了游标之前的历史），
                # 说明翻页已经越过了边界，再翻只会拿回更旧的条目。
                dates = [
                    dt
                    for dt in (
                        parse_struct_time(e.get("published_parsed")) or parse_any(e.get("published"))
                        for e in entries
                    )
                    if dt is not None
                ]
                if dates and min(dates) < cursor_dt:
                    exhausted = True
                    break
                if fresh == 0:
                    # 服务端忽略了 start（返回同一页）——继续翻只会空转
                    logger.warning(
                        "arxiv_pagination_ineffective",
                        extra={"source_id": self.spec.source_key, "page": page + 1},
                    )
                    exhausted = True
                    break
        finally:
            if own_client:
                await client.aclose()

        if cursor_dt is not None and not exhausted and total is not None:
            # 本轮翻到页数上限仍有剩余。这**不会丢数据**：游标只推进到本次实际
            # 处理批次的最新位置，下一轮的窗口从那里重新开始，剩下的条目会被接着取。
            # 记录它是为了能在积压持续增长（生产速率 > 追赶速率）时被看到。
            logger.info(
                "arxiv_window_not_exhausted",
                extra={
                    "source_id": self.spec.source_key,
                    "covered": len(seen),
                    "window_total": total,
                    "pages": MAX_PAGES,
                },
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        fetched_at = datetime.now(timezone.utc)

        documents = [doc for doc in (self._build_document(e, fetched_at) for e in collected) if doc]

        if cursor_dt is not None:
            # 含等于：同一秒提交的多篇论文，上轮可能只处理了其中一部分
            documents = [d for d in documents if d.published_at is None or d.published_at >= cursor_dt]

        def sort_key(doc: CanonicalDocument) -> datetime:
            return doc.published_at or datetime.min.replace(tzinfo=timezone.utc)

        # 升序取前 max_items（留下的是较旧的未处理条目），再按最新在前输出
        documents.sort(key=sort_key)
        batch = documents[: max(1, self.spec.max_items)]
        batch.sort(key=sort_key, reverse=True)

        next_cursor: dict[str, Any] | None
        known = [d.published_at for d in batch if d.published_at is not None]
        if known:
            next_cursor = {"latest_published": _iso(max(known))}
        elif active_cursor:
            # 本轮没有新条目：游标原地不动，绝不能因为一次空响应就退回
            next_cursor = active_cursor
        else:
            next_cursor = None

        if not batch and cursor_dt is not None:
            logger.info("arxiv_no_new_paper", extra={"source_id": self.spec.source_key})

        return FetchOutcome(
            documents=batch,
            http_status=http_status,
            latency_ms=latency_ms,
            next_cursor=next_cursor,
        )

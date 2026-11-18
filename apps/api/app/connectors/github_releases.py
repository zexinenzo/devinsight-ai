"""GitHub Release 连接器（REST API，X-GitHub-Api-Version: 2022-11-28）。

为什么盯 Release 而不是仓库事件流或热搜榜：
- Release 是"项目状态发生了实质变化"的最小可靠信号（新版本、能力边界变化、破坏性改动），
  信息密度远高于 push / star 这类高频噪音；
- 一个仓库一次请求就能拿到全部近期版本，配额消耗与仓库数成正比，可预测。

配额是这类源的核心约束，所以要处理三件事：

1. 匿名 60 次/小时，认证 5000 次/小时。配置 GITHUB_TOKEN 即可自动切到认证配额。
2. **条件请求**：带上上一轮的 ETag 发 If-None-Match，命中 304 时 GitHub 不计配额、
   也不返回 body。但命中率取决于两次轮询之间有没有人下载：带二进制附件的仓库
   （vllm、ollama）每有人下载，assets[].download_count 就会变，响应体随之变化，
   ETag 随之失效。实测 vllm 连续三轮是 [200, 304, 200]，所以 304 只能当"顺手省一次"，
   不能当配额预算的一部分。真正的成本控制手段是页大小和轮询间隔。
3. 额度耗尽返回的是 403 而不是 429（并带 x-ratelimit-remaining: 0），必须读响应头判断，
   否则会被误当成"仓库不存在"或"没有权限"，把健康的源记成故障。

页大小与翻页：

- 一个 release 的 Notes 常有几十 KB，vLLM 一页 20 条就 700KB，per_page=100 要传 3.6MB，
  单次轮询耗时近 30 秒。所以固定 20 条/页，只取"最新几个版本"。
- 页小就意味着**上游分页本身会截断**：若两轮之间新增超过一页，只取第 1 页又把游标推到
  本页最大 id，被挤出页面的较旧条目就永久漏掉。下游按 id 升序处理只能防住 max_items
  截断，挡不住上游分页。因此翻页直到出现 id ≤ 游标的条目为止（上限 3 页）；
  正常情况下第 1 页就包含该条目，多页只在长时间停机后触发。

增量策略：

- release id 单调递增，游标只记 max release id，下一轮只处理更大的；
- 首次运行取**最新**一页作为基线（更早的历史不再回补，但游标只前进，不会漏将来）；
- 增量时按 id **升序**处理并截断到 max_items，超出的留给下一轮 ——
  若按降序截断，被挤掉的较旧新版本将永远进不来。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.connectors.base import BaseConnector, CanonicalDocument, FetchOutcome
from app.core.config import settings
from app.core.errors import RateLimitedError, UpstreamError
from app.core.logging import get_logger
from app.services.classifier import classify_topics
from app.utils.text import content_hash, truncate
from app.utils.urls import canonicalize_url

logger = get_logger(__name__)

RELEASES_PATH = "releases"
# GitHub 单页上限 100，但绝不该用满：一个 release 的 Notes 动辄几十 KB，
# per_page=100 的响应在 vLLM 上接近 3.6MB（实测 20 条约 700KB），
# 单次轮询要跑二三十秒。轮询只需要"最新几个版本"，不需要一次拿全。
PAGE_SIZE = 20
# 翻页上限。正常情况下第 1 页就能覆盖到游标之前的条目，多页只在长时间停机后触发。
MAX_PAGES = 3
API_VERSION = "2022-11-28"

# --- Markdown 清洗：Release Notes 是 Markdown，直接入库会有大量 ### 和 ** 噪音 ---
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_LIST_MARK = re.compile(r"^\s{0,3}[-*+]\s+", re.M)
# 版本号里的预发布标记。GitHub 的 prerelease 布尔值并不可靠——vLLM 会发布
# tag=v0.33.0rc3、name=v0.33.0、prerelease=false 的 release，
# 只看布尔值会把候选版当正式版，而这正是"能力边界变化"最该被区分的地方。
# 注意不能要求 rc 前面有分隔符：真实 tag 是 "v0.33.0rc3"（直接贴在数字后）。
# 改为要求标记后面是结尾或分隔符，这样 "search-1.0" 里的 "rc" 不会被误命中。
_PRERELEASE_TAG = re.compile(r"(?:rc|alpha|beta|pre|preview|dev)\d*(?=$|[-_.+])", re.I)
# 刻意不剥离 **强调** 和 __强调__ 标记。
# 我们的来源全是 Python 项目（vLLM / transformers / ollama），Release Notes 里
# **kwargs、__init__、use_cache 这类标识符极常见；用正则去强调标记会把
# legacy_api 压成 legacyapi、把 **kwargs 压成 kwargs —— 摘要里多两个星号只是丑，
# 标识符被改坏是内容错误。清单符号已由 _LIST_MARK 单独处理。
_HSPACE = re.compile(r"[ \t]+")
_BLANK = re.compile(r"\n{2,}")


def markdown_to_text(raw: str | None, *, max_chars: int | None = None) -> str | None:
    """把 Release Notes 的 Markdown 压成一行可读纯文本。

    刻意不做"完整 Markdown 解析"——目标是给检索与摘要一层干净输入，
    而不是还原排版；代码块整体丢弃，因为版本说明里的代码块多为安装命令，
    对"这个版本发生了什么"没有信息量，却会挤占摘要长度。
    """

    if not raw or not raw.strip():
        return None
    text = _CODE_FENCE.sub(" ", raw)
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _HTML_TAG.sub(" ", text)
    text = _HEADING.sub("", text)
    text = _LIST_MARK.sub("· ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    lines = [_HSPACE.sub(" ", line).strip() for line in text.splitlines()]
    text = _BLANK.sub("\n", "\n".join(line for line in lines if line)).strip()
    if not text:
        return None
    text = " ".join(text.split("\n"))
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _parse_retry_after(response: httpx.Response) -> float | None:
    """从 429 / 403 响应里算出"还要等多少秒"。"""

    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            return None
    return None


def _remaining_quota(response: httpx.Response) -> int | None:
    raw = response.headers.get("x-ratelimit-remaining")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class GitHubReleaseConnector(BaseConnector):
    """单个仓库的 Release 采集器。

    base_url 约定为 `https://api.github.com/repos/{owner}/{repo}`，
    这样"哪个仓库"这个信息随来源记录一起存库，不需要额外的配置字段。
    """

    def __init__(self, spec, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(spec)
        self._client = client

    # --- 来源标识 ---

    @property
    def repo_path(self) -> str:
        """从 base_url 里取出 owner/repo。"""

        path = urlsplit(self.spec.base_url).path.strip("/")
        prefix = "repos/"
        if not path.startswith(prefix) or len(path) <= len(prefix):
            raise UpstreamError(
                f"{self.spec.source_key}: base_url 应形如 https://api.github.com/repos/{{owner}}/{{repo}}，"
                f"实际为 {self.spec.base_url!r}",
                code="invalid_source_config",
            )
        return path[len(prefix):]

    @property
    def repo_name(self) -> str:
        return self.repo_path.rsplit("/", 1)[-1]

    # --- 请求层 ---

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            # GitHub 强制要求 User-Agent，缺失直接 403，且报错信息与限流完全一样
            "User-Agent": settings.http_user_agent,
        }
        token = (settings.github_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _request_page(
        self,
        client: httpx.AsyncClient,
        *,
        page: int,
        per_page: int,
        etag: str | None,
    ) -> tuple[list[dict], str | None, bool]:
        """请求一页 release，返回 (release 列表, 本页 etag, 是否 304)。"""

        headers = self._headers()
        if etag:
            headers["If-None-Match"] = etag
        params = {"per_page": str(per_page), "page": str(page)}
        url = f"{self.spec.base_url.rstrip('/')}/{RELEASES_PATH}"

        try:
            response = await client.get(url, params=params, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamError(
                f"{self.spec.source_key}: 网络请求失败（{type(exc).__name__}: {exc}）"
            ) from exc

        remaining = _remaining_quota(response)

        if response.status_code == 304:
            return [], etag, True

        if response.status_code == 429:
            raise RateLimitedError(
                f"{self.spec.source_key}: 上游限流（HTTP 429，剩余额度 {remaining}）",
                retry_after_seconds=_parse_retry_after(response),
            )

        if response.status_code == 403:
            # 同样返回 403，但含义完全不同：额度耗尽 vs 无权限。
            # 只按 x-ratelimit-remaining 区分，不看 message 文案（文案会变）。
            if remaining == 0:
                retry_after = _parse_retry_after(response)
                raise RateLimitedError(
                    f"{self.spec.source_key}: GitHub 配额耗尽（剩余 0，"
                    f"{'约 ' + str(int(retry_after // 60)) + ' 分钟后' if retry_after else '稍后'}重置）",
                    retry_after_seconds=retry_after,
                )
            raise UpstreamError(
                f"{self.spec.source_key}: HTTP 403（仓库不存在、已私有或无权访问）",
                code="upstream_forbidden",
            )

        if response.status_code == 404:
            raise UpstreamError(
                f"{self.spec.source_key}: 仓库 {self.repo_path} 不存在或已转为私有",
                code="upstream_not_found",
            )

        if response.status_code >= 400:
            raise UpstreamError(
                f"{self.spec.source_key}: HTTP {response.status_code}",
                code="upstream_http_error",
            )

        if remaining is not None and remaining <= 5:
            # 不阻断本轮，但把"下一轮可能失败"提前留给日志
            logger.warning(
                "github_quota_low",
                extra={"source_id": self.spec.source_key, "remaining": remaining},
            )

        payload = response.json()
        if not isinstance(payload, list):
            raise UpstreamError(
                f"{self.spec.source_key}: releases 响应不是数组", code="upstream_schema_error"
            )
        return [item for item in payload if isinstance(item, dict)], response.headers.get("etag"), False

    async def _fetch_releases(
        self,
        cursor: dict[str, Any] | None,
        last_id: int,
    ) -> tuple[list[dict], str | None, bool]:
        """翻页直到覆盖到游标之前的条目，返回 (releases, etag, not_modified)。

        为什么要翻页：上游分页本身就是一种截断。若只取第 1 页、又把游标推到本页最大 id，
        那么"两轮之间新增条目超过一页"时，被挤出第 1 页的较旧条目就永久漏掉了。
        下游按 id 升序处理只能防住 max_items 截断，挡不住上游分页。
        正常情况下第 1 页就包含游标之前的条目，翻页不会发生。
        """

        per_page = max(1, min(self.spec.max_items, PAGE_SIZE))
        stored_etag = None
        if isinstance(cursor, dict):
            raw_etag = cursor.get("etag")
            if isinstance(raw_etag, str) and raw_etag:
                stored_etag = raw_etag

        timeout = httpx.Timeout(self.spec.request_timeout_seconds)
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, max_redirects=3, headers={"User-Agent": settings.http_user_agent}
        )

        collected: list[dict] = []
        etag: str | None = None
        try:
            for page in range(1, MAX_PAGES + 1):
                payload, page_etag, not_modified = await self._request_page(
                    client,
                    page=page,
                    per_page=per_page,
                    # 条件请求只对第 1 页有意义；后续页的 etag 各不相同
                    etag=stored_etag if page == 1 else None,
                )
                if not_modified:
                    return [], stored_etag, True
                if page == 1:
                    etag = page_etag
                collected.extend(payload)

                if len(payload) < per_page:
                    break  # 已经是最后一页
                if last_id <= 0:
                    break  # 基线只取最新一页，不需要回溯历史
                ids = [item["id"] for item in payload if isinstance(item.get("id"), int)]
                if ids and min(ids) <= last_id:
                    break  # 已翻到游标之前的条目，覆盖完整
        finally:
            if own_client:
                await client.aclose()

        return collected, etag, False

    # --- 解析层 ---

    def _build_document(self, release: dict, fetched_at: datetime) -> CanonicalDocument | None:
        release_id = release.get("id")
        tag = (release.get("tag_name") or "").strip()
        html_url = (release.get("html_url") or "").strip()
        if release_id is None or not tag or not html_url:
            return None

        # 标题用 tag 而不是 name：GitHub 上同一个 release 的 name 可能是
        # "v0.33.0"、tag 却是 "v0.33.0rc3"，用 name 会把 rc 标记吃掉。
        # tag 同时是 URL 的组成部分，与链接永远一致。
        title = f"{self.repo_path} {tag}"

        summary = markdown_to_text(release.get("body"), max_chars=settings.summary_max_chars)

        published_at = None
        for key in ("published_at", "created_at"):
            raw = release.get(key)
            if not raw:
                continue
            try:
                published_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                break
            except ValueError:
                continue

        quality_flags: list[str] = []
        if published_at is None:
            quality_flags.append("missing_published_at")
        if not summary:
            quality_flags.append("missing_summary")
        if release.get("prerelease") or _PRERELEASE_TAG.search(tag):
            quality_flags.append("prerelease")
        if release.get("draft"):
            quality_flags.append("draft")

        return CanonicalDocument(
            source_key=self.spec.source_key,
            external_id=str(release_id),
            url=html_url,
            canonical_url=canonicalize_url(html_url),
            title=title,
            summary=summary,
            content_text=truncate(summary, 4000) if summary else None,
            language=self.spec.language or "en",
            region=self.spec.region,
            content_type="release",
            published_at=published_at,
            fetched_at=fetched_at,
            content_hash=content_hash(title, canonicalize_url(html_url), summary),
            topics=classify_topics(title, summary),
            rights_mode=self.spec.rights_mode,
            quality_flags=quality_flags,
        )

    # --- 入口 ---

    async def fetch(self, cursor: dict[str, Any] | None = None) -> FetchOutcome:
        started = time.perf_counter()
        active_cursor = cursor if cursor is not None else self.spec.cursor

        last_id = 0
        if isinstance(active_cursor, dict):
            raw_last = active_cursor.get("release_id")
            if isinstance(raw_last, int):
                last_id = raw_last

        releases, etag, not_modified = await self._fetch_releases(active_cursor, last_id)
        latency_ms = int((time.perf_counter() - started) * 1000)

        if not_modified:
            # 304 不消耗配额，游标原样保留（返回 None 表示"不要覆盖"）
            logger.info("github_not_modified", extra={"source_id": self.spec.source_key})
            return FetchOutcome(documents=[], http_status=304, latency_ms=latency_ms, not_modified=True)

        fetched_at = datetime.now(timezone.utc)
        # 草稿不应进入情报流：它还没发布，随时会被改写或撤掉
        usable = [r for r in releases if isinstance(r, dict) and not r.get("draft")]
        usable = [r for r in usable if isinstance(r.get("id"), int)]

        page_size = max(1, min(self.spec.max_items, PAGE_SIZE))
        if last_id == 0:
            # 基线：取最新的若干个版本，更早的历史不回溯（游标只向前走，
            # 缺的是过去而不是将来）。数量与单页大小一致，一次请求即可。
            batch = sorted(usable, key=lambda r: r["id"], reverse=True)[:page_size]
        else:
            # 截断必须按 id 升序取：被挤掉的要是"更晚"的条目，留给下一轮；
            # 按降序截断会把夹在中间、还没处理过的旧条目永久跳过。
            batch = sorted((r for r in usable if r["id"] > last_id), key=lambda r: r["id"])[: self.spec.max_items]

        # 输出统一最新在前，便于日志与人工核对；截断方向上面已经定好，顺序不影响游标
        batch.sort(key=lambda r: r["id"], reverse=True)

        documents: list[CanonicalDocument] = []
        seen: set[str] = set()
        for release in batch:
            document = self._build_document(release, fetched_at)
            if document is None or document.canonical_url in seen:
                continue
            seen.add(document.canonical_url)
            documents.append(document)

        next_id = max((r["id"] for r in batch), default=last_id)
        next_cursor: dict[str, Any] = {"release_id": next_id, "repo": self.repo_path}
        if etag:
            next_cursor["etag"] = etag

        if not batch:
            logger.info("github_no_new_release", extra={"source_id": self.spec.source_key})

        return FetchOutcome(
            documents=documents,
            http_status=200,
            latency_ms=latency_ms,
            next_cursor=next_cursor,
        )

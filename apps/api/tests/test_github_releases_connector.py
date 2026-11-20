"""GitHub Release 连接器合约测试：MockTransport 固定上游响应，不依赖外网。

覆盖：基线取最新、增量只取更新、截断时按升序不跳条目、ETag/304 免配额、
403 两种含义的区分（配额耗尽 vs 无权限）、草稿过滤、预发布标记、Markdown 清洗。
"""

from __future__ import annotations

import time

import httpx
import pytest
from app.connectors.base import SourceSpec
from app.connectors.github_releases import (
    _PRERELEASE_TAG,
    GitHubReleaseConnector,
    markdown_to_text,
)
from app.core.errors import RateLimitedError, UpstreamError

BASE = "https://api.github.com/repos/acme/widget"
ETAG = 'W/"c0ffee"'


RELEASES: list[dict] = [
    {
        "id": 400,
        "tag_name": "v3.0.0",
        "name": "v3.0.0",
        "html_url": "https://github.com/acme/widget/releases/tag/v3.0.0",
        "body": "### Breaking\n\n- 移除 `legacy_api`\n\n```bash\npip install widget==3\n```\n\n详情见 [迁移指南](https://example.com/migrate)",
        "published_at": "2026-11-18T09:00:00Z",
        "prerelease": False,
        "draft": False,
    },
    {
        "id": 300,
        # 真实 tag 形态：rc 直接贴在版本号后面，没有分隔符
        "tag_name": "v2.5.0rc1",
        "name": "",
        "html_url": "https://github.com/acme/widget/releases/tag/v2.5.0rc1",
        "body": None,
        "published_at": "2026-11-10T09:00:00Z",
        # GitHub 常常把候选版的 prerelease 设为 false，只能靠 tag 判断
        "prerelease": False,
        "draft": False,
    },
    {
        "id": 200,
        "tag_name": "v2.4.0",
        "name": "Steady",
        "html_url": "https://github.com/acme/widget/releases/tag/v2.4.0",
        "body": "修复若干问题",
        "published_at": "2026-10-01T09:00:00Z",
        "prerelease": False,
        "draft": False,
    },
    {
        "id": 100,
        "tag_name": "v2.3.0",
        "html_url": "https://github.com/acme/widget/releases/tag/v2.3.0",
        "body": "旧版本",
        "published_at": "2026-09-01T09:00:00Z",
        "prerelease": False,
        "draft": False,
    },
    {
        # 草稿还没发布，不该进情报流
        "id": 500,
        "tag_name": "v4.0.0-draft",
        "html_url": "https://github.com/acme/widget/releases/tag/v4.0.0-draft",
        "body": "未发布",
        "published_at": "2026-11-18T10:00:00Z",
        "prerelease": False,
        "draft": True,
    },
]


def _spec(**overrides) -> SourceSpec:
    defaults = dict(
        source_key="github_release_widget",
        name="Widget Releases",
        base_url=BASE,
        access_method="api",
        region="global",
        language="en",
        max_items=10,
    )
    defaults.update(overrides)
    return SourceSpec(**defaults)


def _handler(
    releases: list[dict] | None = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    seen: list[dict] | None = None,
    paginate: bool = False,
):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append({"path": request.url.path, "headers": dict(request.headers), "params": dict(request.url.params)})
        if status == 304:
            return httpx.Response(304, headers=headers or {"etag": ETAG})
        if status >= 400:
            return httpx.Response(status, headers=headers or {}, json={"message": "error"})

        data = releases if releases is not None else RELEASES
        if paginate:
            # 模拟 GitHub 的分页：按 id 降序，page 从 1 开始
            per_page = int(request.url.params.get("per_page", "30"))
            page = int(request.url.params.get("page", "1"))
            ordered = sorted(data, key=lambda item: item["id"], reverse=True)
            start = (page - 1) * per_page
            data = ordered[start : start + per_page]
        return httpx.Response(200, json=data, headers=headers or {"etag": ETAG})

    return handle


def _many(count: int) -> list[dict]:
    return [
        {
            "id": i,
            "tag_name": f"v{i}.0.0",
            "html_url": f"https://github.com/acme/widget/releases/tag/v{i}.0.0",
            "body": f"第 {i} 版变更",
            "published_at": "2026-11-01T00:00:00Z",
            "prerelease": False,
            "draft": False,
        }
        for i in range(1, count + 1)
    ]


def _connector(handler, spec: SourceSpec | None = None) -> GitHubReleaseConnector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    return GitHubReleaseConnector(spec or _spec(), client=client)


# --- 解析层单元测试 ---


def test_markdown_is_flattened_for_summary():
    text = markdown_to_text("### 标题\n\n- 项目一\n- 项目二\n\n见[迁移指南](https://a.com)")
    assert text is not None
    assert "###" not in text
    # 链接保留文字、丢弃目标
    assert "迁移指南" in text and "https://a.com" not in text
    # 清单符号归一，避免摘要里出现零散的 "- "
    assert text.count("·") == 2


def test_markdown_keeps_code_identifiers_intact():
    """Python 项目的版本说明里 **kwargs / __init__ / snake_case 是主要信息载体，
    清洗不能把它们当成强调标记吃掉。"""

    text = markdown_to_text("新增 `use_cache` 参数，透传 **kwargs 到 __init__")
    assert text is not None
    assert "use_cache" in text
    assert "**kwargs" in text
    assert "__init__" in text


def test_markdown_drops_code_fences_entirely():
    text = markdown_to_text("改动如下\n\n```bash\npip install x\n```\n\n结束")
    assert text is not None
    assert "pip install" not in text
    assert "改动如下" in text and "结束" in text


def test_prerelease_detection_covers_real_tag_shapes():
    """rc 紧贴版本号（v0.33.0rc3）要能识别，单词里含 rc 的不能误判。"""

    assert _PRERELEASE_TAG.search("v0.33.0rc3")
    assert _PRERELEASE_TAG.search("v1.0.0-alpha.2")
    assert _PRERELEASE_TAG.search("v2.0.0-beta")
    assert _PRERELEASE_TAG.search("v1.0.0-dev")
    assert not _PRERELEASE_TAG.search("v1.0.0")
    assert not _PRERELEASE_TAG.search("search-1.0")  # "rc" 后面还跟着字母
    assert not _PRERELEASE_TAG.search("v1.0.0-betamax")


# --- 抓取行为 ---


async def test_baseline_takes_newest_and_records_etag():
    connector = _connector(_handler())
    outcome = await connector.fetch()

    # 草稿 500 被过滤，其余四条按 id 降序取最新的
    assert [d.external_id for d in outcome.documents] == ["400", "300", "200", "100"]
    assert outcome.next_cursor == {"release_id": 400, "repo": "acme/widget", "etag": ETAG}
    assert outcome.http_status == 200


async def test_document_fields_are_normalized():
    connector = _connector(_handler())
    outcome = await connector.fetch()
    by_id = {d.external_id: d for d in outcome.documents}

    latest = by_id["400"]
    # 标题带仓库名，跨源汇总时 "v3.0.0" 本身没有辨识度
    assert latest.title == "acme/widget v3.0.0"
    assert latest.content_type == "release"
    assert "legacy_api" in (latest.summary or "")
    assert "pip install" not in (latest.summary or "")
    assert latest.published_at is not None and latest.published_at.year == 2026
    assert latest.quality_flags == []

    # 预发布要标记但保留：AI 基础设施的 rc 版本往往先于正式版暴露能力变化。
    # 这里 prerelease 布尔值是 false，标记完全由 tag 名推出。
    rc = by_id["300"]
    assert "prerelease" in rc.quality_flags
    assert "missing_summary" in rc.quality_flags
    # name 为空的 release 不能产出 "acme/widget " 这种悬空标题
    assert rc.title == "acme/widget v2.5.0rc1"

    # 200 的 name 是 "Steady"、tag 是 v2.4.0；标题必须用 tag，
    # 否则同一个版本在不同来源里会对不上号，且 tag 才是 URL 的组成部分
    assert by_id["200"].title == "acme/widget v2.4.0"


async def test_incremental_only_returns_newer_releases():
    connector = _connector(_handler(), _spec(cursor={"release_id": 200}))
    outcome = await connector.fetch()

    # 增量按 id 升序输出（与截断规则一致），集合才是契约，顺序由查询层负责
    assert {d.external_id for d in outcome.documents} == {"300", "400"}
    assert outcome.next_cursor["release_id"] == 400


async def test_capped_incremental_processes_ascending_so_nothing_is_skipped():
    """截断时必须按 id 升序：若按降序取前 N 个，被挤掉的旧版本永远进不来。"""

    connector = _connector(_handler(), _spec(max_items=2, cursor={"release_id": 100}))
    outcome = await connector.fetch()

    # 取的是 id 最小的两个（200、300），400 留给下一轮；输出按最新在前
    assert [d.external_id for d in outcome.documents] == ["300", "200"]
    # 游标只推进到确实处理过的位置，400 留给下一轮
    assert outcome.next_cursor["release_id"] == 300


async def test_no_new_release_keeps_cursor_position():
    connector = _connector(_handler(), _spec(cursor={"release_id": 400}))
    outcome = await connector.fetch()

    assert outcome.documents == []
    assert outcome.next_cursor["release_id"] == 400


async def test_etag_is_sent_as_if_none_match():
    seen: list[dict] = []
    connector = _connector(_handler(seen=seen), _spec(cursor={"release_id": 400, "etag": ETAG}))
    await connector.fetch()

    assert seen[0]["headers"].get("if-none-match") == ETAG
    assert seen[0]["path"] == "/repos/acme/widget/releases"


async def test_304_reports_not_modified_and_leaves_cursor_untouched():
    connector = _connector(_handler(status=304), _spec(cursor={"release_id": 400, "etag": ETAG}))
    outcome = await connector.fetch()

    assert outcome.not_modified is True
    assert outcome.http_status == 304
    assert outcome.documents == []
    # next_cursor 为 None，采集服务据此不覆盖旧游标
    assert outcome.next_cursor is None


# --- 分页 ---


async def test_pagination_reaches_back_past_cursor():
    """单页装不下两轮之间的新增时，必须继续翻页，否则被挤出页面的条目永久丢失。"""

    seen: list[dict] = []
    # 25 条、每页 10 条（max_items=10）。游标停在 1，需要翻到第 3 页才覆盖到它。
    connector = _connector(_handler(_many(25), seen=seen, paginate=True), _spec(max_items=10, cursor={"release_id": 1}))
    outcome = await connector.fetch()

    pages = [int(item["params"].get("page", "1")) for item in seen]
    assert pages == [1, 2, 3]
    # 覆盖完整后按升序取前 10 条，游标只推进到 11，剩下的下一轮再取；
    # 输出统一按最新在前
    assert [d.external_id for d in outcome.documents] == [str(i) for i in range(11, 1, -1)]
    assert outcome.next_cursor["release_id"] == 11


async def test_pagination_stops_once_cursor_is_covered():
    seen: list[dict] = []
    connector = _connector(_handler(_many(25), seen=seen, paginate=True), _spec(max_items=10, cursor={"release_id": 15}))
    await connector.fetch()

    # 第 1 页返回 25..16，最小 16 仍大于游标 15，继续翻；第 2 页出现 15 即停
    assert [int(item["params"].get("page", "1")) for item in seen] == [1, 2]


async def test_baseline_never_pages():
    seen: list[dict] = []
    connector = _connector(_handler(_many(25), seen=seen, paginate=True), _spec(max_items=10))
    outcome = await connector.fetch()

    # 基线只取最新一页，不回溯历史
    assert [int(item["params"].get("page", "1")) for item in seen] == [1]
    assert [d.external_id for d in outcome.documents] == [str(i) for i in range(25, 15, -1)]
    assert outcome.next_cursor["release_id"] == 25


async def test_only_first_page_sends_conditional_header():
    seen: list[dict] = []
    connector = _connector(
        _handler(_many(25), seen=seen, paginate=True),
        _spec(max_items=10, cursor={"release_id": 1, "etag": ETAG}),
    )
    await connector.fetch()

    assert seen[0]["headers"].get("if-none-match") == ETAG
    # 各页 ETag 不同，对第 2 页回放第 1 页的 ETag 只会换来一个 200
    assert "if-none-match" not in seen[1]["headers"]


# --- 限流语义 ---


async def test_403_with_zero_remaining_raises_rate_limited():
    reset = str(int(time.time()) + 120)
    connector = _connector(_handler(status=403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}))
    with pytest.raises(RateLimitedError) as excinfo:
        await connector.fetch()

    assert excinfo.value.retry_after_seconds is not None
    assert 0 < excinfo.value.retry_after_seconds <= 120


async def test_403_with_quota_left_is_plain_upstream_error():
    """同样是 403，额度没耗尽说明是权限/仓库问题，不能被误当成限流。"""

    connector = _connector(_handler(status=403, headers={"x-ratelimit-remaining": "42"}))
    with pytest.raises(UpstreamError) as excinfo:
        await connector.fetch()

    assert not isinstance(excinfo.value, RateLimitedError)
    assert excinfo.value.code == "upstream_forbidden"


async def test_429_raises_rate_limited_with_retry_after():
    connector = _connector(_handler(status=429, headers={"retry-after": "30"}))
    with pytest.raises(RateLimitedError) as excinfo:
        await connector.fetch()

    assert excinfo.value.retry_after_seconds == 30


async def test_404_points_at_missing_repository():
    connector = _connector(_handler(status=404))
    with pytest.raises(UpstreamError) as excinfo:
        await connector.fetch()

    assert excinfo.value.code == "upstream_not_found"
    assert "acme/widget" in str(excinfo.value)


# --- 配置错误 ---


async def test_base_url_must_point_at_a_repo():
    connector = _connector(_handler(), _spec(base_url="https://api.github.com"))
    with pytest.raises(UpstreamError) as excinfo:
        await connector.fetch()

    assert excinfo.value.code == "invalid_source_config"

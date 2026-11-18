"""Hacker News 连接器合约测试：MockTransport 固定上游响应，不依赖外网。

覆盖：类型过滤、Ask HN 无外链、已删除条目、增量游标、首次基线、单条失败隔离、429 限流。
"""

from __future__ import annotations

import httpx
import pytest

from app.connectors.base import SourceSpec
from app.connectors.hackernews import HackerNewsConnector
from app.core.errors import RateLimitedError

BASE = "https://hacker-news.firebaseio.com/v0"

ITEMS: dict[int, dict | None] = {
    101: {
        "id": 101,
        "type": "story",
        "title": "Rust 1.90 released",
        "url": "https://blog.rust-lang.org/release?utm_source=hn&ref=feed",
        "time": 1731900000,
        "score": 120,
    },
    102: {
        "id": 102,
        "type": "story",
        "title": "Ask HN: How do you learn a new language?",
        "text": "<p>I want to learn Rust &amp; Go</p>",
        "time": 1731900100,
    },
    103: None,  # 已被删除，API 返回 null
    104: {
        "id": 104,
        "type": "job",
        "title": "Startup is hiring engineers",
        "url": "https://example.com/jobs",
        "time": 1731900200,
    },
    105: {
        "id": 105,
        "type": "story",
        "title": "",
        "url": "https://example.com/empty",
        "time": 1731900300,
    },
}


def _spec(**overrides) -> SourceSpec:
    defaults = dict(
        source_key="hackernews_en",
        name="Hacker News",
        base_url=BASE,
        access_method="api",
        region="global",
        language="en",
        max_items=10,
    )
    defaults.update(overrides)
    return SourceSpec(**defaults)


def _handler(story_ids, *, status: int = 200, seen_items: list[int] | None = None, fail_ids: frozenset[int] = frozenset()):
    def handle(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "newstories.json":
            if status != 200:
                return httpx.Response(status)
            return httpx.Response(200, json=story_ids)
        if name.endswith(".json"):
            item_id = int(name[: -len(".json")])
            if seen_items is not None:
                seen_items.append(item_id)
            if item_id in fail_ids:
                return httpx.Response(500)
            item = ITEMS.get(item_id)
            if item is None:
                # 必须显式写出 null 字面量：json=None 在 httpx 里表示"不设置 body"，
                # 会得到空字符串，response.json() 直接抛 JSONDecodeError。
                return httpx.Response(200, text="null")
            return httpx.Response(200, json=item)
        return httpx.Response(404)

    return handle


def _connector(handler, spec: SourceSpec | None = None) -> HackerNewsConnector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    return HackerNewsConnector(spec or _spec(), client=client)


async def test_keeps_stories_and_self_posts_only():
    connector = _connector(_handler([101, 102, 103, 104, 105]))
    outcome = await connector.fetch()

    by_id = {document.external_id: document for document in outcome.documents}
    # 103 已删除、104 是 job、105 标题为空，三者都应被过滤
    assert set(by_id) == {"101", "102"}

    story = by_id["101"]
    assert story.canonical_url == "https://blog.rust-lang.org/release"  # 追踪参数已剔除
    assert story.published_at is not None
    # 外链故事在 HN 上没有正文，摘要缺失是事实而非缺陷，仅打标记不丢弃
    assert story.quality_flags == ["missing_summary"]
    assert isinstance(story.topics, list)

    self_post = by_id["102"]
    # Ask HN 没有外链，用讨论页兜底而不是丢弃
    assert self_post.canonical_url == "https://news.ycombinator.com/item?id=102"
    assert "self_post" in self_post.quality_flags
    assert "learn Rust" in (self_post.summary or "")

    assert outcome.next_cursor == {"max_item_id": 105}


async def test_incremental_only_requests_items_after_cursor():
    seen: list[int] = []
    connector = _connector(_handler([101, 102, 103, 104, 105], seen_items=seen), _spec(cursor={"max_item_id": 102}))
    outcome = await connector.fetch()

    # 只应请求 id 大于 102 的条目，且按升序处理，保证不漏
    assert set(seen) == {103, 104, 105}
    assert outcome.next_cursor == {"max_item_id": 105}
    # 103 死条目、104 job、105 空标题，最终没有可入库内容
    assert outcome.documents == []


async def test_no_new_items_keeps_previous_cursor():
    seen: list[int] = []
    connector = _connector(_handler([101, 102], seen_items=seen), _spec(cursor={"max_item_id": 999}))
    outcome = await connector.fetch()

    assert seen == []
    assert outcome.documents == []
    # 游标不能因为"本轮没新东西"就退回 None，否则下一轮会重新全量拉
    assert outcome.next_cursor == {"max_item_id": 999}


async def test_first_run_takes_latest_batch_and_sets_baseline():
    seen: list[int] = []
    # 榜单按时间降序，最新的在前
    connector = _connector(_handler([105, 104, 103, 102, 101], seen_items=seen), _spec(max_items=2))
    outcome = await connector.fetch()

    assert set(seen) == {105, 104}
    assert outcome.next_cursor == {"max_item_id": 105}


async def test_single_item_failure_does_not_abort_batch():
    connector = _connector(_handler([101, 102], fail_ids=frozenset({101})))
    outcome = await connector.fetch()

    # 101 返回 500 只跳过该条，不影响同批其他条目
    assert [document.external_id for document in outcome.documents] == ["102"]


async def test_429_raises_rate_limited_not_generic_upstream_error():
    connector = _connector(_handler([101], status=429))
    with pytest.raises(RateLimitedError):
        await connector.fetch()

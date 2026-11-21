"""Hacker News 连接器合约测试：MockTransport 固定上游响应，不依赖外网。

覆盖：类型过滤、Ask HN 无外链、已删除条目、热度序保留、增量（seen_ids）、
旧游标迁移、老帖回榜、seen 截断、首次基线、单条失败隔离、429 限流。
"""

from __future__ import annotations

import httpx
import pytest
from app.connectors import hackernews
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


def _handler(
    story_ids,
    *,
    status: int = 200,
    seen_items: list[int] | None = None,
    fail_ids: frozenset[int] = frozenset(),
):
    def handle(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "topstories.json":
            if status != 200:
                return httpx.Response(status)
            return httpx.Response(200, json=story_ids)
        if name == "newstories.json":
            # 改榜单之后不该再有请求打到这个端点。显式 410 而不是 404，
            # 让「还是抓时间流水」这个回退在测试里立刻报错，而不是悄悄通过。
            return httpx.Response(410)
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

    # 首次运行没有游标，榜上 5 条全部请求过，全部计入 seen
    assert outcome.next_cursor == {"seen_ids": [101, 102, 103, 104, 105]}


async def test_documents_keep_ranking_order_not_id_order():
    """入库顺序应跟着榜单走（热度），而不是跟着 id（发布先后）。

    这条是改榜单的直接目的：库里 documents 的插入顺序决定前端默认列表的先后，
    若按 id 排，昨天的低分帖会压在今天的高分帖上面。
    """

    # 榜单 102 在前、101 在后；id 上 101 更小，按 id 排序会把顺序颠倒过来
    connector = _connector(_handler([102, 101]), _spec())
    outcome = await connector.fetch()
    assert [document.external_id for document in outcome.documents] == ["102", "101"]


async def test_incremental_only_requests_unseen_items():
    seen: list[int] = []
    connector = _connector(
        _handler([101, 102, 103, 104, 105], seen_items=seen),
        _spec(cursor={"seen_ids": [101, 102]}),
    )
    outcome = await connector.fetch()

    # 只请求没见过的三条；不再按 id 大小过滤，也不再排序
    assert set(seen) == {103, 104, 105}
    assert outcome.next_cursor == {"seen_ids": [101, 102, 103, 104, 105]}
    # 103 死条目、104 job、105 空标题，最终没有可入库内容
    assert outcome.documents == []


async def test_no_new_items_keeps_previous_cursor():
    seen: list[int] = []
    connector = _connector(_handler([101, 102], seen_items=seen), _spec(cursor={"seen_ids": [101, 102]}))
    outcome = await connector.fetch()

    assert seen == []
    assert outcome.documents == []
    # 全在 seen 里就不发请求，游标原样返回（不退回 None，否则下轮会被当成首轮）
    assert outcome.next_cursor == {"seen_ids": [101, 102]}


async def test_item_reentering_ranking_is_fetched():
    """老帖回榜必须被抓——这是旧 max_item_id 游标漏掉的场景。

    101 是三天前发的（id 比已见的最大 id 还小），今天重新上榜。
    旧实现用 `id > 游标` 过滤，会把它当过期货丢掉；seen_ids 只看「见过没有」。
    """

    seen: list[int] = []
    connector = _connector(
        _handler([105, 101], seen_items=seen),
        _spec(cursor={"seen_ids": [105]}),
    )
    outcome = await connector.fetch()

    assert seen == [101]
    assert outcome.next_cursor == {"seen_ids": [105, 101]}


async def test_legacy_max_item_id_cursor_is_migrated():
    """旧游标（{"max_item_id": N}）不必触发一次全量重抓。

    旧语义是「id ≤ N 的都处理过」。迁移轮沿用这个判断，只补新上榜的；
    写回的游标已是新格式，下一轮起恢复正常。
    """

    seen: list[int] = []
    connector = _connector(
        _handler([105, 101, 103], seen_items=seen),
        _spec(cursor={"max_item_id": 102}),
    )
    outcome = await connector.fetch()

    # 101 ≤ 102 被当作已见；105、103 比游标大，要请求
    assert set(seen) == {105, 103}
    assert outcome.next_cursor == {"seen_ids": [105, 103]}


async def test_seen_ids_truncated(monkeypatch):
    monkeypatch.setattr(hackernews, "SEEN_IDS_LIMIT", 3)
    connector = _connector(_handler([109]), _spec(cursor={"seen_ids": [101, 102, 103, 104]}))
    outcome = await connector.fetch()

    # 保留最近 3 个：101/102 被挤掉，104 与新增的 109 留下
    assert outcome.next_cursor == {"seen_ids": [103, 104, 109]}


async def test_first_run_takes_top_of_ranking():
    seen: list[int] = []
    # 榜单按热度降序：越靠前越热
    connector = _connector(_handler([104, 102, 101], seen_items=seen), _spec(max_items=2))
    outcome = await connector.fetch()

    # 取榜头两条，而不是按 id 排序后的头两条
    assert seen == [104, 102]
    assert outcome.next_cursor == {"seen_ids": [104, 102]}


async def test_single_item_failure_does_not_abort_batch():
    connector = _connector(_handler([101, 102], fail_ids=frozenset({101})))
    outcome = await connector.fetch()

    # 101 返回 500 只跳过该条，不影响同批其他条目
    assert [document.external_id for document in outcome.documents] == ["102"]
    # 失败的也计入 seen：不为一个死条目每轮重试，把名额慢慢堵满
    assert outcome.next_cursor == {"seen_ids": [101, 102]}


async def test_empty_ranking_does_not_wipe_cursor():
    connector = _connector(_handler([]), _spec(cursor={"seen_ids": [101, 102]}))
    outcome = await connector.fetch()

    assert outcome.documents == []
    # 上游给空榜单是一次异常，不能顺手把历史清空
    assert outcome.next_cursor == {"seen_ids": [101, 102]}


async def test_429_raises_rate_limited_not_generic_upstream_error():
    connector = _connector(_handler([101], status=429))
    with pytest.raises(RateLimitedError):
        await connector.fetch()

"""RSS 连接器合约测试：用 MockTransport 固定上游响应，不依赖外网。

覆盖：字段映射、批内去重、缺失发布时间不打假时间、429 重试、404 不重试。
"""

from __future__ import annotations

import httpx
import pytest

from app.connectors.base import SourceSpec
from app.connectors.rss import RSSConnector
from app.core.errors import UpstreamError

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <language>zh-cn</language>
  <item>
    <title>OpenAI 发布新模型</title>
    <link>https://example.com/p/1?utm_source=rss</link>
    <guid>guid-1</guid>
    <description>&lt;p&gt;摘要 &lt;b&gt;内容&lt;/b&gt; 在这里&lt;/p&gt;</description>
    <pubDate>Tue, 17 Nov 2026 08:00:00 GMT</pubDate>
  </item>
  <item>
    <title>没有发布时间的条目</title>
    <link>https://example.com/p/2</link>
    <guid>guid-2</guid>
  </item>
  <item>
    <title>重复 URL 条目</title>
    <link>https://example.com/p/1</link>
    <guid>guid-3</guid>
  </item>
  <item>
    <title></title>
    <link>https://example.com/p/4</link>
  </item>
</channel></rss>
"""

SPEC = SourceSpec(
    source_key="test_feed",
    name="Test Feed",
    base_url="https://example.com/rss.xml",
    access_method="rss",
    region="global",
    language="zh",
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


async def test_parses_and_dedupes_within_response():
    connector = RSSConnector(SPEC, client=_client(lambda request: httpx.Response(200, text=RSS_FIXTURE)))
    outcome = await connector.fetch()

    assert outcome.http_status == 200
    # 4 个条目：1 条正常、1 条缺时间、1 条重复 URL、1 条空标题 -> 保留 2 条
    assert len(outcome.documents) == 2

    first = outcome.documents[0]
    assert first.external_id == "guid-1"
    assert first.canonical_url == "https://example.com/p/1"  # utm 已剔除
    assert first.summary == "摘要 内容 在这里"
    assert first.published_at is not None
    assert first.language == "zh-cn"
    assert "ai-models" in first.topics
    assert first.content_hash.startswith("sha256:")
    assert first.quality_flags == []


async def test_missing_published_at_is_none_not_fetch_time():
    connector = RSSConnector(SPEC, client=_client(lambda request: httpx.Response(200, text=RSS_FIXTURE)))
    outcome = await connector.fetch()

    second = outcome.documents[1]
    assert second.published_at is None
    assert "missing_published_at" in second.quality_flags
    assert second.fetched_at is not None


async def test_retries_on_429_and_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.05"})
        return httpx.Response(200, text=RSS_FIXTURE)

    connector = RSSConnector(SPEC, client=_client(handler))
    outcome = await connector.fetch()

    assert calls["n"] == 2
    assert outcome.http_status == 200


async def test_404_is_not_retried_and_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    connector = RSSConnector(SPEC, client=_client(handler))
    with pytest.raises(UpstreamError):
        await connector.fetch()
    assert calls["n"] == 1


async def test_broken_feed_raises_parse_error():
    connector = RSSConnector(SPEC, client=_client(lambda request: httpx.Response(200, text="<html>not a feed")))
    with pytest.raises(UpstreamError):
        await connector.fetch()

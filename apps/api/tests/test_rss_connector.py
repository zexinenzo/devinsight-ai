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
    <description>&lt;p&gt;这是一段长度接近真实的摘要内容，用来验证字段映射&lt;/p&gt;</description>
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
    # clean_html 会跑一遍 NFKC 归一化，中文全角逗号因此变成半角
    assert first.summary == "这是一段长度接近真实的摘要内容,用来验证字段映射"
    assert first.published_at is not None
    # feed 里写的是 zh-cn，入库前归一成 zh：检索按主语言分组，
    # 保留 zh-cn / zh-tw 这类细分标签会让「中文语料几条」的统计失真
    assert first.language == "zh"
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


async def test_drops_navigation_only_summary():
    """纯导航文字的 summary 要丢弃，不能原样存进库。

    InfoQ 中文的 <description> 清洗后就是「点击查看原文>」7 个字，20 条记录
    共用同一句「摘要」。后果是列表里每条看起来都像同一篇文章，关键词检索
    也会被这句废话反复命中。宁可留空，让前端显示「该源未提供摘要」。
    """

    fixture = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Nav Feed</title>
  <language>zh</language>
  <item>
    <title>英伟达发布新一代架构</title>
    <link>https://example.com/p/9</link>
    <guid>nav-1</guid>
    <description><![CDATA[<div align="right"><a href="https://example.com">点击查看原文></a></div>]]></description>
    <pubDate>Tue, 17 Nov 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

    connector = RSSConnector(SPEC, client=_client(lambda request: httpx.Response(200, text=fixture)))
    outcome = await connector.fetch()

    assert len(outcome.documents) == 1
    document = outcome.documents[0]
    assert document.summary is None
    assert "missing_summary" in document.quality_flags


async def test_strips_trailing_read_more():
    """真摘要后面的「查看全文」要切掉。

    少数派每条摘要都以这个尾巴结束。留着的话，「查看全文」会成为所有条目
    共享的字符串，既干扰检索打分，也让摘要看起来像被截断了一半。
    """

    fixture = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Trailing Feed</title>
  <language>zh-CN</language>
  <item>
    <title>派早报：Apple 年度应用公布</title>
    <link>https://example.com/p/10</link>
    <guid>trail-1</guid>
    <description><![CDATA[<p>AMD 更新虚幻引擎 FSR 插件、Google Wallet 登陆 iOS 等。 查看全文</p>]]></description>
    <pubDate>Tue, 17 Nov 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

    connector = RSSConnector(SPEC, client=_client(lambda request: httpx.Response(200, text=fixture)))
    outcome = await connector.fetch()

    document = outcome.documents[0]
    assert document.summary is not None
    assert "查看全文" not in document.summary
    assert document.summary.endswith("。")
    assert document.language == "zh"


async def test_normalizes_regional_language_tag() -> None:
    """en-US 要收敛成 en。

    OpenAI News 的 feed 写的就是 en-US，200 条记录全部带着这个标签进库。
    按 language=en 筛选时它们整批不见了——不是数据丢了，是标签不统一。
    这类 bug 不会报错，只会让「某一类内容凭空少一半」。
    """

    fixture = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Regional Feed</title>
  <language>en-US</language>
  <item>
    <title>Introducing a new model</title>
    <link>https://example.com/p/11</link>
    <guid>region-1</guid>
    <description><![CDATA[<p>A new model is available to all developers starting today.</p>]]></description>
    <pubDate>Tue, 17 Nov 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

    connector = RSSConnector(SPEC, client=_client(lambda request: httpx.Response(200, text=fixture)))
    outcome = await connector.fetch()

    assert outcome.documents[0].language == "en"

"""arXiv 连接器合约测试：MockTransport 固定上游响应，不依赖外网。

覆盖：版本号归一、基线取一页、增量按 published 过滤、同秒不丢条、
分页翻到游标为止、服务端忽略 start 时不空转、429/503 都算限流、
配置缺 search_query 时报明确错误、截断时取较旧的未处理条目。
"""

from __future__ import annotations

import httpx
import pytest

from app.connectors import arxiv as arxiv_module
from app.connectors.arxiv import ArxivConnector, normalize_arxiv_id
from app.connectors.base import SourceSpec
from app.core.errors import RateLimitedError, UpstreamError

BASE = "https://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.LG"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _entry(arxiv_id: str, title: str, published: str, *, version: int = 1, summary: str = "摘要正文") -> str:
    return f"""  <entry>
    <id>http://arxiv.org/abs/{arxiv_id}v{version}</id>
    <title>{_esc(title)}</title>
    <updated>{published}</updated>
    <published>{published}</published>
    <link href="https://arxiv.org/abs/{arxiv_id}v{version}" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/{arxiv_id}v{version}" rel="related" type="application/pdf" title="pdf"/>
    <summary>{_esc(summary)}</summary>
    <category term="cs.AI"/>
    <arxiv:primary_category term="cs.AI"/>
  </entry>"""


def _atom(entries: list[str], total: int | None = None) -> bytes:
    """构造 Atom 响应。

    `total` 对应真实响应里的 `opensearch:totalResults`。真实 arXiv 一定带这个字段，
    窗口查询下它就是「游标之后总篇数」，连接器据此判断是否翻完；不传则退化为
    按页大小判断，用来覆盖「服务端不给计数」的降级路径。
    """

    body = "\n".join(entries)
    total_tag = ""
    if total is not None:
        total_tag = (
            '  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
            f"{total}</opensearch:totalResults>\n"
        )
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>arXiv Query: search_query=cat:cs.AI</title>
  <updated>2026-11-18T16:14:02Z</updated>
{total_tag}{body}
</feed>""".encode()


# 上游按 submittedDate 倒序返回，即最新在前
ENTRIES = [
    _entry("2611.30001", "Ambient LLMs: Data-Efficient SFT", "2026-11-18T10:00:00Z"),
    _entry("2611.20002", "Sparse Attention Revisited", "2026-11-17T09:00:00Z"),
    _entry("2611.10003", "Agent Benchmarks Are Broken", "2026-11-16T09:00:00Z"),
    _entry("2611.00004", "Older Work On Retrieval", "2026-11-15T09:00:00Z"),
]


def _spec(**overrides) -> SourceSpec:
    defaults = dict(
        source_key="arxiv_cs_ai",
        name="arXiv cs.AI",
        base_url=BASE,
        access_method="api",
        region="global",
        language="en",
        max_items=200,
    )
    defaults.update(overrides)
    return SourceSpec(**defaults)


def _handler(entries: list[str] | None = None, *, status: int = 200, seen: list[dict] | None = None):
    body = _atom(entries if entries is not None else ENTRIES)

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append({"params": dict(request.url.params), "headers": dict(request.headers)})
        if status >= 400:
            return httpx.Response(status, text="Rate exceeded.")
        return httpx.Response(200, content=body, headers={"content-type": "application/atom+xml; charset=utf-8"})

    return handle


def _connector(handler, spec: SourceSpec | None = None) -> ArxivConnector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    return ArxivConnector(spec or _spec(), client=client)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """翻页时的 3 秒等待在测试里没有意义，置 0 否则单测会跑几十秒。"""
    monkeypatch.setattr(arxiv_module, "MIN_REQUEST_GAP_SECONDS", 0.0)


# --- 解析层单元测试 ---


def test_normalize_arxiv_id_strips_version():
    assert normalize_arxiv_id("http://arxiv.org/abs/2611.23229v1") == "2611.23229"
    assert normalize_arxiv_id("http://arxiv.org/abs/2611.23229v12") == "2611.23229"
    # 老式 id 的斜杠要保留，只剥末尾版本号
    assert normalize_arxiv_id("http://arxiv.org/abs/hep-th/9901001v2") == "hep-th/9901001"
    # 没有版本号时原样返回
    assert normalize_arxiv_id("http://arxiv.org/abs/2611.23229") == "2611.23229"
    assert normalize_arxiv_id(None) is None
    # "v" 只出现在中间不能被当成版本后缀
    assert normalize_arxiv_id("http://arxiv.org/abs/2611.01234v1") == "2611.01234"


# --- 抓取与解析 ---


async def test_baseline_takes_latest_page():
    connector = _connector(_handler())
    outcome = await connector.fetch()

    assert outcome.http_status == 200
    # 基线不经游标过滤，全部返回；输出最新在前
    assert [d.external_id for d in outcome.documents] == [
        "2611.30001",
        "2611.20002",
        "2611.10003",
        "2611.00004",
    ]
    assert outcome.next_cursor == {"latest_published": "2026-11-18T10:00:00Z"}


async def test_document_fields():
    connector = _connector(_handler())
    outcome = await connector.fetch()
    doc = outcome.documents[0]

    assert doc.content_type == "paper"
    assert doc.language == "en"
    # 规范地址不带版本号，否则同一篇论文每个版本都是不同 URL
    assert doc.canonical_url == "https://arxiv.org/abs/2611.30001"
    # 原始链接保留版本，点进去看到的是读者实际读到的那一版
    assert doc.url == "https://arxiv.org/abs/2611.30001v1"
    assert doc.published_at is not None
    assert doc.quality_flags == []


async def test_title_whitespace_collapsed():
    # arXiv 的标题在 Atom 里按行折行，实测会出现多余换行
    entries = [_entry("2611.55555", "Long Title\n   Wrapped Over\n   Lines", "2026-11-18T10:00:00Z")]
    connector = _connector(_handler(entries))
    outcome = await connector.fetch()

    assert outcome.documents[0].title == "Long Title Wrapped Over Lines"


async def test_versions_collapse_to_one_document():
    # 同一篇论文 v1、v2 同时出现在一页里，应只产出一条
    entries = [
        _entry("2611.77777", "Same Paper", "2026-11-18T10:00:00Z", version=2),
        _entry("2611.77777", "Same Paper", "2026-11-18T09:00:00Z", version=1),
    ]
    connector = _connector(_handler(entries))
    outcome = await connector.fetch()

    assert [d.external_id for d in outcome.documents] == ["2611.77777"]


# --- 增量语义 ---


async def test_incremental_only_returns_newer():
    connector = _connector(_handler(), _spec(cursor={"latest_published": "2026-11-17T09:00:00Z"}))
    outcome = await connector.fetch()

    # 含等于：游标那一秒的条目要重新过一遍，靠唯一约束去重
    assert [d.external_id for d in outcome.documents] == ["2611.30001", "2611.20002"]
    assert outcome.next_cursor == {"latest_published": "2026-11-18T10:00:00Z"}


async def test_same_second_entry_not_dropped():
    entries = [
        _entry("2611.88888", "Poster A", "2026-11-18T10:00:00Z"),
        _entry("2611.88889", "Poster B", "2026-11-18T10:00:00Z"),
        _entry("2611.88890", "Poster C", "2026-11-18T09:00:00Z"),
    ]
    # 游标停在同一秒：若用严格大于比较，同秒的条目会被永久跳过
    connector = _connector(_handler(entries), _spec(cursor={"latest_published": "2026-11-18T10:00:00Z"}))
    outcome = await connector.fetch()

    assert {d.external_id for d in outcome.documents} == {"2611.88888", "2611.88889"}


async def test_no_new_items_keeps_cursor():
    connector = _connector(_handler(), _spec(cursor={"latest_published": "2026-11-19T00:00:00Z"}))
    outcome = await connector.fetch()

    assert outcome.documents == []
    # 空响应绝不能把游标退回去
    assert outcome.next_cursor == {"latest_published": "2026-11-19T00:00:00Z"}


async def test_truncation_keeps_oldest_unprocessed():
    connector = _connector(_handler(), _spec(max_items=2))
    outcome = await connector.fetch()

    # 升序取前 2 条（较旧的未处理），游标只推进到确实入库的位置
    assert {d.external_id for d in outcome.documents} == {"2611.10003", "2611.00004"}
    assert outcome.next_cursor == {"latest_published": "2026-11-16T09:00:00Z"}


# --- 分页 ---


async def test_pagination_uses_window_total(monkeypatch):
    """窗口模式下 totalResults 就是游标之后的篇数，据此收敛，不多发请求。"""

    monkeypatch.setattr(arxiv_module, "PAGE_SIZE", 2)
    seen: list[dict] = []
    # 游标之后只有 4 篇（P6..P3），窗口查询不会返回 P2/P1
    window = [
        _entry("2611.60000", "P6", "2026-11-18T06:00:00Z"),
        _entry("2611.50000", "P5", "2026-11-18T05:00:00Z"),
        _entry("2611.40000", "P4", "2026-11-18T04:00:00Z"),
        _entry("2611.30000", "P3", "2026-11-18T03:00:00Z"),
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        start = int(params.get("start", "0"))
        page_size = int(params["max_results"])
        return httpx.Response(
            200,
            content=_atom(window[start : start + page_size], total=len(window)),
            headers={"content-type": "application/atom+xml; charset=utf-8"},
        )

    connector = _connector(handle, _spec(cursor={"latest_published": "2026-11-18T03:00:00Z"}))
    outcome = await connector.fetch()

    # 两页正好取完 4 篇，不需要第 3 页
    assert [int(p["start"]) for p in seen] == [0, 2]
    assert {d.external_id for d in outcome.documents} == {
        "2611.60000",
        "2611.50000",
        "2611.40000",
        "2611.30000",
    }
    assert outcome.next_cursor == {"latest_published": "2026-11-18T06:00:00Z"}


async def test_pagination_falls_back_to_published_boundary(monkeypatch):
    """服务端不给 totalResults 时，靠「返回了游标之前的条目」判断已经翻过边界。"""

    monkeypatch.setattr(arxiv_module, "PAGE_SIZE", 2)
    seen: list[dict] = []
    entries = [
        _entry("2611.60000", "P6", "2026-11-18T06:00:00Z"),
        _entry("2611.50000", "P5", "2026-11-18T05:00:00Z"),
        _entry("2611.40000", "P4", "2026-11-18T04:00:00Z"),
        _entry("2611.30000", "P3", "2026-11-18T03:00:00Z"),
        _entry("2611.20000", "P2", "2026-11-18T02:00:00Z"),
        _entry("2611.10000", "P1", "2026-11-18T01:00:00Z"),
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        start = int(params.get("start", "0"))
        page_size = int(params["max_results"])
        return httpx.Response(
            200,
            content=_atom(entries[start : start + page_size]),
            headers={"content-type": "application/atom+xml; charset=utf-8"},
        )

    connector = _connector(handle, _spec(cursor={"latest_published": "2026-11-18T03:00:00Z"}))
    outcome = await connector.fetch()

    # 游标在第 3 条，必须翻满 3 页才能确认没有漏条
    assert [int(p["start"]) for p in seen] == [0, 2, 4]
    assert {d.external_id for d in outcome.documents} == {"2611.60000", "2611.50000", "2611.40000", "2611.30000"}
    assert outcome.next_cursor == {"latest_published": "2026-11-18T06:00:00Z"}


async def test_pagination_ineffective_does_not_spin(monkeypatch):
    monkeypatch.setattr(arxiv_module, "PAGE_SIZE", 2)
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        # 服务端忽略 start，每页都返回同样两条
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            content=_atom([_entry("2611.60000", "P1", "2026-11-18T06:00:00Z"),
                           _entry("2611.50000", "P2", "2026-11-18T05:00:00Z")]),
            headers={"content-type": "application/atom+xml; charset=utf-8"},
        )

    connector = _connector(handle, _spec(cursor={"latest_published": "2020-01-01T00:00:00Z"}))
    outcome = await connector.fetch()

    # 第二页没有任何新 id 就应当停下，而不是把 MAX_PAGES 全跑满
    assert len(seen) == 2
    assert {d.external_id for d in outcome.documents} == {"2611.60000", "2611.50000"}


# --- 限流与配置错误 ---


async def test_429_raises_rate_limited():
    connector = _connector(_handler(status=429))
    with pytest.raises(RateLimitedError) as info:
        await connector.fetch()
    assert "429" in str(info.value)


async def test_503_raises_rate_limited():
    # arXiv 过载返回 503，这不是来源坏了，不能记成 failing
    connector = _connector(_handler(status=503))
    with pytest.raises(RateLimitedError):
        await connector.fetch()


async def test_missing_search_query_is_config_error():
    connector = _connector(_handler(), _spec(base_url="https://export.arxiv.org/api/query"))
    with pytest.raises(UpstreamError) as info:
        await connector.fetch()
    assert info.value.code == "source_misconfigured"


async def test_request_carries_sort_and_page_params():
    seen: list[dict] = []
    connector = _connector(_handler(seen=seen))
    await connector.fetch()

    params = seen[0]["params"]
    assert params["sortBy"] == "submittedDate"
    assert params["sortOrder"] == "descending"
    assert params["start"] == "0"
    assert "cs.AI" in params["search_query"]
    # arXiv 要求 UA 里能联系到人，否则限流阈值更严
    assert "DevInsightAI" in seen[0]["headers"].get("user-agent", "")


# --- 增量窗口 ---


async def test_incremental_query_wraps_window_in_parentheses():
    seen: list[dict] = []
    connector = _connector(_handler(seen=seen), _spec(cursor={"latest_published": "2026-11-18T03:00:00Z"}))
    await connector.fetch()

    q = seen[0]["params"]["search_query"]
    # 括号必须有：原来那串是 `A OR B`，不加括号时 AND 只约束 B，
    # 前一个分类等于没被窗口过滤，会把全部历史论文拉回来。
    assert q.startswith("(cat:cs.AI OR cat:cs.LG) AND submittedDate:[")
    # 起点往前留 1 分钟：03:00 的条目在同一分钟里提交，舍掉会漏
    assert "202611180259" in q
    assert q.endswith("TO 999912312359]")


async def test_baseline_query_has_no_window():
    """首轮没有游标，不能带窗口——带了只会拿到一小段，基线就残了。"""

    seen: list[dict] = []
    connector = _connector(_handler(seen=seen))
    await connector.fetch()

    assert "submittedDate" not in seen[0]["params"]["search_query"]

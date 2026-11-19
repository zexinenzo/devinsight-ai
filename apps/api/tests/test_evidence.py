"""证据层的测试：域名身份、时效判定、排序优先级、统计口径。

这几条的共同特征是「判错了不会报错，只会悄悄给出误导性结论」：

1. 域名分级把聚合站当一手源 → 一条转载被当成一个独立声源；
2. 独立声源按子域名计数 → 同一家的 10 个子站被算成 10 家媒体在报；
3. 无日期的结果按相关性排在带日期的前面 → 用未知冒充已知；
4. general 模式忘了传时间窗 → 上游不设过滤，2023 年的旧闻混进「最新」。

第 4 条是实测发现的：同一句 query，general 模式返回的第一名是 2023 年
GPT-4 开放 API 的旧闻，分数 0.79，而响应里没有任何日期字段可供识别。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import settings
from app.services.evidence_service import (
    Evidence,
    _local_urls,
    _registrable_host,
    _summarize,
    _web_sort_key,
    classify_domain,
    freshness_band,
)
from app.services.web_search_service import (
    TavilyProvider,
    WebHit,
    WebSearchUnavailable,
    build_web_search,
)

# --- 域名身份 ---


def test_classify_official_domain_is_tier_a():
    profile = classify_domain("https://openai.com/index/gpt-5/")
    assert profile.tier == "A"
    assert profile.name == "OpenAI 官方"


def test_classify_matches_subdomain_of_official():
    # blog.nvidia.com 是一手发布方的子域，不该因为它不是裸域就降级
    profile = classify_domain("https://blogs.nvidia.com/blog/some-post/")
    assert profile.tier == "A"


def test_classify_media_is_tier_b():
    # www. 要在 domain_of 那一步被剥掉，这里验证剥干净了
    profile = classify_domain("https://www.reuters.com/technology/story/")
    assert profile.tier == "B"
    assert profile.name == "路透社"


def test_classify_aggregator_is_tier_c_with_reason():
    # 实测里出现过的一条：Yahoo Finance 转 Reuters 稿。
    # 按域名它是聚合站，按内容它是一手通讯社的报道——域名分级只能反映前者，
    # 所以 note 必须说清楚理由，不能让人以为这个分级做了内容核查。
    profile = classify_domain("https://finance.yahoo.com/news/openai-x-123.html")
    assert profile.tier == "C"
    assert "聚合" in profile.note
    assert profile.name == "yahoo.com"


def test_classify_unknown_domain_defaults_to_tier_c():
    profile = classify_domain("https://someone-blog.example.org/posts/1")
    assert profile.tier == "C"
    # 未知域名必须说明「为什么不信」，而不是静默给个 C
    assert "未收录" in profile.note


def test_community_subdomain_of_official_is_not_tier_a():
    # 真实数据抓到的坑：community.openai.com 是用户论坛，按后缀匹配会命中
    # openai.com 被判成一手源。白名单必须在大类匹配之前拦掉这类子域。
    profile = classify_domain("https://community.openai.com/t/gpt-x-is-out/123")
    assert profile.tier == "C"
    assert "社区" in profile.note


def test_plain_official_domain_still_tier_a_after_ugc_guard():
    # 加了拦截规则之后，裸域和正常子域不能被误伤
    assert classify_domain("https://openai.com/index/x").tier == "A"
    assert classify_domain("https://platform.openai.com/docs").tier == "A"


def test_classify_empty_url_does_not_crash():
    profile = classify_domain("")
    assert profile.tier == "C"
    assert profile.domain == ""


def test_registrable_host_collapses_subdomains():
    assert _registrable_host("finance.yahoo.com") == "yahoo.com"
    assert _registrable_host("news.yahoo.com") == "yahoo.com"
    assert _registrable_host("a.b.c.example.com") == "example.com"


def test_registrable_host_handles_second_level_suffix():
    # 按「最后两段」的粗暴规则，bbc.co.uk 会变成 co.uk，独立声源计数直接错
    assert _registrable_host("news.bbc.co.uk") == "bbc.co.uk"
    assert _registrable_host("bbc.co.uk") == "bbc.co.uk"
    assert _registrable_host("www.36kr.com") == "36kr.com"


def test_registrable_host_keeps_ip_as_is():
    assert _registrable_host("192.168.1.1") == "192.168.1.1"


# --- 时效分档 ---


@pytest.mark.parametrize(
    ("age", "band"),
    [
        (None, "unknown"),
        (0, "today"),
        (1, "today"),
        (2, "this_week"),
        (3, "this_week"),
        (4, "recent"),
        (7, "recent"),
        (8, "stale"),
        (365, "stale"),
    ],
)
def test_freshness_band(age, band):
    assert freshness_band(age) == band


# --- 排序：无日期不能冒充已知 ---


def _evidence(*, published: datetime | None, score: float) -> Evidence:
    return Evidence(
        channel="web",
        title="t",
        url="https://example.com/a",
        snippet=None,
        published_at=published,
        source_name="example.com",
        source_tier="C",
        score=score,
        date_verified=published is not None,
    )


def test_undated_result_sorts_after_dated_one_even_with_higher_score():
    now = datetime.now(UTC)
    dated = _evidence(published=now - timedelta(days=2), score=0.1)
    undated = _evidence(published=None, score=0.9)

    ordered = sorted([undated, dated], key=_web_sort_key, reverse=True)
    assert ordered[0] is dated
    assert ordered[1] is undated


def test_newer_result_sorts_before_older_when_both_dated():
    now = datetime.now(UTC)
    newer = _evidence(published=now - timedelta(hours=3), score=0.2)
    older = _evidence(published=now - timedelta(days=3), score=0.8)

    ordered = sorted([older, newer], key=_web_sort_key, reverse=True)
    assert ordered[0] is newer


# --- 统计口径 ---


def _web(url: str, tier: str, *, published: datetime | None, in_local: bool = False, age: int | None = None) -> Evidence:
    return Evidence(
        channel="web",
        title="t",
        url=url,
        snippet=None,
        published_at=published,
        source_name="x",
        source_tier=tier,
        score=0.5,
        date_verified=published is not None,
        in_local=in_local,
        age_days=age,
    )


def test_summary_counts_domains_not_urls():
    now = datetime.now(UTC)
    items = [
        _web("https://finance.yahoo.com/a", "C", published=None),
        _web("https://news.yahoo.com/b", "C", published=now, age=0),
        _web("https://www.reuters.com/c", "B", published=now, age=0),
    ]
    summary = _summarize(items)

    assert summary.web_total == 3
    # 三条 URL、两个可注册域名：Yahoo 的两个子站必须算一家
    assert summary.unique_domains == 2
    assert summary.date_unverified == 1
    assert summary.tier_counts == {"B": 1, "C": 2}


def test_summary_overlap_and_age_stats():
    now = datetime.now(UTC)
    items = [
        _web("https://a.com/1", "B", published=now - timedelta(days=2), age=2, in_local=True),
        _web("https://b.com/2", "A", published=now - timedelta(days=4), age=4),
        _web("https://c.com/3", "C", published=None),
    ]
    summary = _summarize(items)

    assert summary.overlap_with_local == 1
    assert summary.date_unverified == 1
    # 无日期的不参与均值：用 0 代替会让「平均新鲜度」看起来更好
    assert summary.mean_age_days == 3.0
    assert summary.freshest_at is not None and summary.oldest_at is not None
    assert summary.freshest_at > summary.oldest_at


def test_summary_empty_batch_is_all_zero():
    summary = _summarize([])
    assert summary.web_total == 0
    assert summary.unique_domains == 0
    assert summary.mean_age_days is None
    assert summary.freshest_at is None


# --- provider 请求构造 ---


def _provider(**kwargs) -> TavilyProvider:
    defaults = {"base_url": "https://api.tavily.com", "keyless": True}
    defaults.update(kwargs)
    return TavilyProvider(**defaults)


def test_news_mode_sends_days_and_not_time_range():
    body = _provider()._body("q", limit=5, days=7, topic="news")
    assert body["topic"] == "news"
    assert body["days"] == 7
    # general 的 time_range 在 news 模式下不该出现：两个参数语义不一致，
    # 同时传会让上游按哪个执行变得不确定
    assert "time_range" not in body


def test_news_mode_clamps_days_to_upstream_limit():
    assert _provider()._body("q", limit=5, days=90, topic="news")["days"] == 30
    assert _provider()._body("q", limit=5, days=0, topic="news")["days"] == 1


def test_general_mode_sends_coarse_time_range():
    body = _provider()._body("q", limit=5, days=7, topic="general")
    assert body["topic"] == "general"
    assert body["time_range"] == "week"
    assert "days" not in body


def test_keyless_uses_access_mode_header():
    headers = _provider()._headers()
    assert headers["X-Tavily-Access-Mode"] == "keyless"
    assert "Authorization" not in headers


def test_keyed_uses_bearer_without_keyless_header():
    headers = _provider(api_key="tvly-abc")._headers()
    assert headers["Authorization"] == "Bearer tvly-abc"
    # 两个都发会让「到底按哪个额度计费」变得不可预期
    assert "X-Tavily-Access-Mode" not in headers


def test_provider_requires_key_or_keyless():
    with pytest.raises(WebSearchUnavailable):
        TavilyProvider(base_url="https://api.tavily.com", api_key="", keyless=False)


def test_build_returns_none_when_provider_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "web_search_provider", "")
    assert build_web_search() is None


def test_build_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr(settings, "web_search_provider", "nope")
    with pytest.raises(WebSearchUnavailable):
        build_web_search()


# --- 与本地库的重叠判定 ---


class _FakeRows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.queried: list = []

    async def execute(self, statement):
        self.queried.append(statement)
        return _FakeRows(self.rows)


def test_local_urls_matches_after_canonicalisation():
    session = _FakeSession([("https://a.com/post",)])
    hits = [WebHit(url="https://a.com/post?utm_source=x", title="t", snippet="", published_at=None, score=0.1, date_verified=False)]
    found = asyncio_run(_local_urls(session, hits))
    assert found == {"https://a.com/post"}


def test_local_urls_skips_query_when_no_hits():
    session = _FakeSession([])
    found = asyncio_run(_local_urls(session, []))
    assert found == set()
    assert session.queried == []


def asyncio_run(coro):
    """本地跑协程，不引入 pytest-asyncio 的 marker 负担。"""

    import asyncio

    return asyncio.run(coro)

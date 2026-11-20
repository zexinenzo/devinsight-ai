"""工具层单测：URL 规范化、时间解析、文本清洗、主题分类。

这些函数是去重与质量规则的基石，出错会让"多源印证"虚高，所以必须有断言兜住。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.services.classifier import classify_topics
from app.utils.dates import is_implausible, parse_any, parse_struct_time
from app.utils.text import clean_html, content_hash
from app.utils.urls import canonicalize_url, domain_of


class TestCanonicalUrl:
    def test_strips_tracking_params_but_keeps_content_params(self):
        url = "https://Example.com/post/1/?utm_source=wechat&id=42&spm=abc&ref=homepage#section"
        assert canonicalize_url(url) == "https://example.com/post/1?id=42"

    def test_sorts_query_for_stable_identity(self):
        assert canonicalize_url("https://a.com/x?b=2&a=1") == canonicalize_url("https://a.com/x?a=1&b=2")

    def test_trailing_slash_and_root(self):
        assert canonicalize_url("https://a.com/x/") == "https://a.com/x"
        assert canonicalize_url("https://a.com/") == "https://a.com/"

    def test_drops_default_port_keeps_custom(self):
        assert canonicalize_url("https://a.com:443/x") == "https://a.com/x"
        assert canonicalize_url("https://a.com:8443/x") == "https://a.com:8443/x"

    def test_unparseable_returned_as_is(self):
        assert canonicalize_url("not a url") == "not a url"

    def test_domain_of_strips_www(self):
        assert domain_of("https://www.36kr.com/p/123") == "36kr.com"


class TestDates:
    def test_parse_struct_time(self):
        struct = (2026, 11, 17, 8, 0, 0, 1, 321, 0)
        parsed = parse_struct_time(struct)
        assert parsed == datetime(2026, 11, 17, 8, 0, tzinfo=UTC)

    def test_parse_rfc822(self):
        assert parse_any("Tue, 17 Nov 2026 08:00:00 GMT") == datetime(2026, 11, 17, 8, 0, tzinfo=UTC)

    def test_parse_iso_with_z(self):
        assert parse_any("2026-11-17T08:00:00Z") == datetime(2026, 11, 17, 8, 0, tzinfo=UTC)

    def test_parse_garbage_returns_none(self):
        assert parse_any("昨天下午") is None
        assert parse_any(None) is None

    def test_implausible_future(self):
        fetched = datetime(2026, 11, 17, tzinfo=UTC)
        assert is_implausible(fetched + timedelta(days=2), fetched) is True
        assert is_implausible(fetched - timedelta(days=1), fetched) is False


class TestText:
    def test_clean_html(self):
        assert clean_html("<p>Hello <b>World</b></p>") == "Hello World"
        assert clean_html("   ") is None

    def test_truncate_marks_ellipsis(self):
        assert clean_html("<p>" + "字" * 50 + "</p>", max_chars=10).endswith("…")

    def test_hash_is_stable_and_title_case_insensitive(self):
        a = content_hash("Hello World", "https://a.com/x", "summary")
        b = content_hash("hello world", "https://a.com/x", "summary")
        c = content_hash("Hello World", "https://a.com/y", "summary")
        assert a == b
        assert a != c
        assert a.startswith("sha256:")


class TestClassifier:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("OpenAI 发布新的多模态大模型", "ai-models"),
            ("某公司完成 B 轮融资，估值 10 亿美元", "startups-funding"),
            ("GitHub 上增长最快的开源 Agent 框架", "open-source"),
            ("国务院发布新一代人工智能发展规划政策", "policy-macro"),
        ],
    )
    def test_hits_expected_topic(self, title, expected):
        assert expected in classify_topics(title)

    def test_empty_input(self):
        assert classify_topics("", "") == []

    def test_returns_at_most_limit(self):
        title = "OpenAI 开源模型 融资 政策 市场 架构 全部命中"
        assert len(classify_topics(title, limit=2)) <= 2

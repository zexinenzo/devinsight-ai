"""来源注册表初始数据。

原则（手册 3.1 / 13.1）：
- 阶段 1 只启用 3 个已验证可访问的 RSS 源，先把闭环跑稳；
- 其余来源作为候选先入库、默认停用，通过后台启停，不在代码里假装"可用"；
- 每个来源都记录 rights_note，明确只存元数据与短摘要。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceSeed:
    source_key: str
    name: str
    canonical_domain: str
    source_type: str
    access_method: str
    base_url: str
    region: str = "global"
    language: str | None = None
    credibility_tier: str = "B"
    enabled: bool = False
    priority: int = 2
    poll_interval_minutes: int = 360
    request_timeout_seconds: int = 20
    rate_limit_per_minute: int = 30
    parser_version: str = "rss-v1"
    robots_url: str | None = None
    terms_url: str | None = None
    rights_note: str | None = None


SOURCE_SEEDS: tuple[SourceSeed, ...] = (
    SourceSeed(
        source_key="36kr_zh",
        name="36氪",
        canonical_domain="36kr.com",
        source_type="media",
        access_method="rss",
        base_url="https://www.36kr.com/feed",
        region="cn",
        language="zh",
        credibility_tier="B",
        enabled=True,
        priority=1,
        poll_interval_minutes=30,
        robots_url="https://36kr.com/robots.txt",
        rights_note="科技商业媒体。仅保存标题、摘要与原文链接，全文不入库；转载与全文授权需另行核实。",
    ),
    SourceSeed(
        source_key="infoq_en",
        name="InfoQ",
        canonical_domain="infoq.com",
        source_type="media",
        access_method="rss",
        base_url="https://feed.infoq.com/",
        region="global",
        language="en",
        credibility_tier="B",
        enabled=True,
        priority=1,
        poll_interval_minutes=60,
        robots_url="https://www.infoq.com/robots.txt",
        rights_note="软件工程媒体。仅保存标题、摘要与原文链接，保留文章链接与署名。",
    ),
    SourceSeed(
        source_key="openai_news_en",
        name="OpenAI News",
        canonical_domain="openai.com",
        source_type="official",
        access_method="rss",
        base_url="https://openai.com/news/rss.xml",
        region="global",
        language="en",
        credibility_tier="A",
        enabled=True,
        priority=1,
        poll_interval_minutes=120,
        robots_url="https://openai.com/robots.txt",
        rights_note="厂商官方发布，作为一手来源；仍与公告口径、后续修订区分记录。",
    ),
    # --- 以下为候选源：先注册、默认停用，阶段 2 逐个健康检查后再开启 ---
    SourceSeed(
        source_key="deepmind_blog_en",
        name="Google DeepMind Blog",
        canonical_domain="deepmind.google",
        source_type="official",
        access_method="rss",
        base_url="https://deepmind.google/blog/rss.xml",
        region="global",
        language="en",
        credibility_tier="A",
        enabled=False,
        priority=1,
        poll_interval_minutes=360,
        rights_note="官方研究博客，一手来源。",
    ),
    SourceSeed(
        source_key="techcrunch_en",
        name="TechCrunch",
        canonical_domain="techcrunch.com",
        source_type="media",
        access_method="rss",
        base_url="https://techcrunch.com/feed/",
        region="global",
        language="en",
        credibility_tier="B",
        enabled=False,
        priority=2,
        poll_interval_minutes=60,
        rights_note="科技媒体。注意付费/授权内容边界，仅存摘要与链接。",
    ),
)

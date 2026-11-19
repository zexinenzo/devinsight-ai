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
    # 落库长度的判据。默认 metadata_excerpt：只存 600 字片段。
    # 只有拿到明确开放授权（CC / 公有领域 / 站点明示）的源才能改成全文级，
    # 因为这一列直接决定 content_service._store_text 截多少字。
    rights_mode: str = "metadata_excerpt"


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
        source_key="ruanyifeng_zh",
        name="阮一峰的网络日志",
        canonical_domain="ruanyifeng.com",
        source_type="blog",
        access_method="rss",
        base_url="https://www.ruanyifeng.com/blog/atom.xml",
        region="cn",
        language="zh",
        # 一手作者署名、可追溯到具体某个人，比聚合媒体的可信度高一档
        credibility_tier="A",
        enabled=True,
        priority=1,
        # 周刊固定每周一期，2 小时轮询纯属浪费带宽
        poll_interval_minutes=720,
        robots_url="https://www.ruanyifeng.com/robots.txt",
        terms_url="http://creativecommons.org/licenses/by-nc-nd/3.0/deed.zh",
        # 全文可入库的依据是权利人自己放开的协议，不是我们的判断
        rights_mode="cc_by_nc_nd",
        rights_note=(
            "个人技术博客。页面明示授权「自由转载-非商用-非衍生-保持署名」，"
            "即 CC BY-NC-ND 3.0（creativecommons.org/licenses/by-nc-nd/3.0/deed.zh）。"
            "故全文可入库，但 ND 禁止演绎：摘要必须原样摘取，不能改写或机器润色；"
            "亦不可用于商业用途。"
        ),
    ),
    SourceSeed(
        source_key="infoq_zh",
        name="InfoQ 中文",
        canonical_domain="infoq.cn",
        source_type="media",
        access_method="rss",
        base_url="https://www.infoq.cn/feed",
        region="cn",
        language="zh",
        credibility_tier="B",
        enabled=True,
        priority=2,
        poll_interval_minutes=120,
        robots_url="https://www.infoq.cn/robots.txt",
        rights_note="技术媒体。未检索到开放授权声明，按默认版权处理：仅存摘要与原文链接，全文不入库。",
    ),
    SourceSeed(
        source_key="sspai_zh",
        name="少数派",
        canonical_domain="sspai.com",
        source_type="media",
        access_method="rss",
        base_url="https://sspai.com/feed",
        region="cn",
        language="zh",
        credibility_tier="B",
        enabled=True,
        priority=3,
        poll_interval_minutes=180,
        robots_url="https://sspai.com/robots.txt",
        rights_note="效率与消费科技媒体。未检索到开放授权声明，仅存摘要与原文链接，全文不入库。",
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
    # --- 阶段 2：API 源（按 item id 增量，游标存 sources.sync_cursor）---
    SourceSeed(
        source_key="hackernews_en",
        name="Hacker News",
        canonical_domain="news.ycombinator.com",
        source_type="community",
        access_method="api",
        base_url="https://hacker-news.firebaseio.com/v0",
        region="global",
        language="en",
        # 社区投票榜，热度高不等于可信，可信度低于媒体与官方源
        credibility_tier="C",
        enabled=True,
        priority=2,
        poll_interval_minutes=30,
        rate_limit_per_minute=60,
        parser_version="api-hn-v1",
        rights_note="社区提交聚合，多为站外原文链接。仅保存标题、讨论页地址与自帖正文摘要，外链正文不入库。",
    ),
    # --- 阶段 2：GitHub Release（base_url 即 owner/repo，游标记 release id + ETag）---
    # 只挑"版本变化等于能力边界变化"的项目：推理引擎、模型库、本地运行时。
    # 匿名配额 60 次/小时是全局共享的，三个仓库各 120 分钟一轮 ≈ 1.5 次/小时。
    # ETag 命中 304 时不计配额，但命中率取决于两次轮询之间有没有人下载，
    # 不能当作配额预算的一部分。
    SourceSeed(
        source_key="github_release_vllm",
        name="vLLM Releases",
        canonical_domain="github.com",
        source_type="repo",
        access_method="api",
        base_url="https://api.github.com/repos/vllm-project/vllm",
        region="global",
        language="en",
        credibility_tier="A",
        enabled=True,
        priority=2,
        poll_interval_minutes=120,
        rate_limit_per_minute=10,
        parser_version="api-github-v1",
        rights_note="开源项目版本发布说明。存 tag、发布时间与 Notes 摘要，正文链接回 GitHub。",
    ),
    SourceSeed(
        source_key="github_release_transformers",
        name="Transformers Releases",
        canonical_domain="github.com",
        source_type="repo",
        access_method="api",
        base_url="https://api.github.com/repos/huggingface/transformers",
        region="global",
        language="en",
        credibility_tier="A",
        enabled=True,
        priority=2,
        poll_interval_minutes=120,
        rate_limit_per_minute=10,
        parser_version="api-github-v1",
        rights_note="开源项目版本发布说明。存 tag、发布时间与 Notes 摘要，正文链接回 GitHub。",
    ),
    SourceSeed(
        source_key="github_release_ollama",
        name="Ollama Releases",
        canonical_domain="github.com",
        source_type="repo",
        access_method="api",
        base_url="https://api.github.com/repos/ollama/ollama",
        region="global",
        language="en",
        credibility_tier="A",
        enabled=True,
        priority=2,
        poll_interval_minutes=120,
        rate_limit_per_minute=10,
        parser_version="api-github-v1",
        rights_note="开源项目版本发布说明。存 tag、发布时间与 Notes 摘要，正文链接回 GitHub。",
    ),
    # --- 阶段 2：arXiv（Atom over HTTPS，游标记 latest_published）---
    # 分类条件写在 base_url 里：想只看 cs.CL 就再加一个来源，不用改代码。
    # 轮询间隔给到 240 分钟，因为 arXiv 的限流是"1 请求 / 3 秒"的硬墙，
    # 超限直接 429 且不给 Retry-After —— 这个源的价值在于不漏新论文，
    # 而不是比别人快几分钟。
    SourceSeed(
        source_key="arxiv_cs_ai",
        name="arXiv cs.AI / cs.LG / cs.CL",
        canonical_domain="arxiv.org",
        source_type="preprint",
        access_method="api",
        base_url="https://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.LG+OR+cat:cs.CL",
        region="global",
        language="en",
        # 预印本未经同行评审，可信度低于官方发布
        credibility_tier="B",
        enabled=True,
        priority=2,
        poll_interval_minutes=240,
        request_timeout_seconds=30,
        rate_limit_per_minute=1,
        parser_version="api-arxiv-v1",
        rights_note="预印本平台，未经同行评审。仅保存标题、摘要与 abs 页链接，PDF 不入库。",
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

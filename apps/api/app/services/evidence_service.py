"""证据：把本地检索与实时搜索的结果放进同一套结构，并标注各自有多可信。

**这个模块产出的不是「答案」，是「可以被追问的证据」。** 两者的区别在于：
每条证据都要能回答三个问题——它从哪来（渠道与域名）、它是什么时候的（发布时间
是否经过验证）、还有谁在说同一件事（独立声源数）。回答不了这三点的东西，
在 Agent 那边就只能当线索，不能当依据。

**为什么域名分级不等于事实核查。** 这是本模块最容易被误解的地方。实测里有一条
`finance.yahoo.com` 的结果，内容是 Reuters 的稿子：按域名它是聚合站（C 档），
按内容它是一手通讯社的报道。域名只能反映「这个站点自己有没有采编能力」，
不能反映「这一条的具体来源」。所以：

- `tier` 是一个**客观可观测**的属性，用于排序和展示，不用于下结论；
- 「这条是不是转载」交给 `in_local`（库里已有同一 URL）与声源去重来间接体现；
- 响应里永远不出现「已核实」这类词——本模块没有做事实核查，也不该假装做了。

**为什么未知域名默认 C 档。** 默认信任一个没见过的域名，比默认怀疑它危险得多。
A/B 档是白名单，白名单必须靠人维护；C 档是「剩下的世界」，它的默认含义是
「需要第二条独立来源才能采信」。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Document
from app.services import document_service
from app.services.web_search_service import (
    WebHit,
    WebSearchUnavailable,
    get_web_search,
)
from app.utils.urls import canonicalize_url, domain_of


def resolved_topic() -> str:
    """实时搜索用的模式。非法值退回 news：general 拿不到日期，退回它等于放弃时效判断。"""

    topic = (settings.web_search_topic or "news").strip().lower()
    return topic if topic in ("news", "general") else "news"

# --- 域名分级白名单 ---
#
# 判据是「这个域名自己是不是信息的发布方」，不是「这个网站有名不有名」。
# 一手发布方（A）：发布的内容就是原始材料，可追溯到主体本身。
# 专业媒体（B）：有署名、有编辑流程、有纠错机制，但仍是二手转述。
TIER_A_DOMAINS: dict[str, str] = {
    "openai.com": "OpenAI 官方",
    "anthropic.com": "Anthropic 官方",
    "deepmind.google": "Google DeepMind 官方",
    "blog.google": "Google 官方博客",
    "ai.meta.com": "Meta AI 官方",
    "microsoft.com": "Microsoft 官方",
    "nvidia.com": "NVIDIA 官方",
    "apple.com": "Apple 官方",
    "github.blog": "GitHub 官方博客",
    "huggingface.co": "Hugging Face 官方",
    "arxiv.org": "论文原文（arXiv）",
    "nature.com": "论文原文（Nature）",
    "science.org": "论文原文（Science）",
    "deepseek.com": "DeepSeek 官方",
    "mistral.ai": "Mistral 官方",
}

TIER_B_DOMAINS: dict[str, str] = {
    "reuters.com": "路透社",
    "bloomberg.com": "彭博社",
    "ft.com": "金融时报",
    "wsj.com": "华尔街日报",
    "nytimes.com": "纽约时报",
    "cnbc.com": "CNBC",
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "wired.com": "WIRED",
    "arstechnica.com": "Ars Technica",
    "theinformation.com": "The Information",
    "36kr.com": "36氪",
    "infoq.cn": "InfoQ 中文",
    "sspai.com": "少数派",
    "jiqizhixin.com": "机器之心",
    "qbitai.com": "量子位",
    "huxiu.com": "虎嗅",
    "caixin.com": "财新",
    "yicai.com": "第一财经",
}

# 明确属于聚合/UGC 的域名。列出来不是为了降级（它们本来就是 C），
# 而是为了在 `note` 里给出可解释的原因，而不是笼统的「未收录」。
TIER_C_NOTES: dict[str, str] = {
    "yahoo.com": "聚合站，正文多为转载通讯社稿",
    "msn.com": "聚合站",
    "medium.com": "个人博客平台",
    "substack.com": "个人通讯订阅",
    "linkedin.com": "社交平台，多为二手转述",
    "reddit.com": "论坛讨论，非报道",
    "x.com": "社交平台，非报道",
    "twitter.com": "社交平台，非报道",
    "baijiahao.baidu.com": "自媒体分发平台",
    "toutiao.com": "自媒体分发平台",
    "zhihu.com": "问答社区，非报道",
}

# 官方域名下的社区/论坛子域。**这类子域必须在大类匹配之前拦掉。**
# 真实数据里抓到的例子：`community.openai.com` 是用户论坛，一条帖子标题写着
# 「GPT-6.2 Astra and Sol are now available - Announcements」——按后缀规则它会
# 命中 `openai.com` 被判成 A 档一手源，但它其实是 UGC。白名单按后缀匹配的
# 通病就在这里：一个域名里有官方空间，也有用户空间。
_UGC_SUBDOMAIN_PREFIXES = (
    "community.",
    "forum.",
    "forums.",
    "discuss.",
    "discussions.",
    "bbs.",
    "club.",
    "support.",
)

# 二级后缀：把 `bbc.co.uk` 归到 `bbc.co.uk` 而不是 `co.uk`
_SECOND_LEVEL_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.cn",
        "net.cn",
        "org.cn",
        "gov.cn",
        "edu.cn",
        "ac.cn",
        "co.jp",
        "co.kr",
        "com.au",
        "com.br",
        "co.in",
        "com.tw",
        "com.hk",
    }
)


@dataclass(frozen=True, slots=True)
class DomainProfile:
    """一个域名的身份判定。`note` 会原样出现在接口里——判定理由必须能被外部读懂。"""

    domain: str
    name: str
    tier: str
    note: str


@dataclass(slots=True)
class Evidence:
    """一条证据。本地与实时共用这个结构，`channel` 区分来源。"""

    channel: str
    title: str
    url: str
    snippet: str | None
    published_at: datetime | None
    source_name: str
    source_tier: str
    # 渠道内的排序分，**不同渠道之间不可比**：本地是余弦相似度，实时是上游相关性。
    # 放在同一字段里只是为了让调用方少一个分支，绝不意味着可以混排。
    score: float | None
    # 发布时间是否经过确认。False 不等于「旧」，而是「不知道多旧」
    date_verified: bool
    # 这条 URL 是否已经在本地的库里。对本地证据恒为 True（它当然在库里），
    # 这个字段的信息量全部在 web 侧：重叠条数越多，「实时补了新信息」的说法越站不住。
    in_local: bool = False
    age_days: int | None = None
    # 由 age_days 算出的档位：today / this_week / recent / stale / unknown。
    # Agent 的措辞要跟着它走，而不是跟着自己的“感觉”走。
    freshness: str = "unknown"
    # 本地证据独有：点回原文时定位到哪一段
    document_id: uuid.UUID | None = None
    char_start: int | None = None


@dataclass(slots=True)
class VerificationSummary:
    """证据集的整体可信度画像。

    这些数字是给 Agent 与前端做**可编程判断**用的，不是给人看的装饰。
    比如 `date_unverified > 0` 时，任何「最新」措辞都应该降级成「近期」。
    """

    web_total: int = 0
    # 按可注册域名去重后的独立声源数。同一家的 10 个子域名只是 1 个声源。
    unique_domains: int = 0
    # 与本地库重叠的条数：这些不是新信息，是已知信息被再次看到
    overlap_with_local: int = 0
    # 上游没给日期、或给了但解析失败的条数
    date_unverified: int = 0
    # 各可信度档的条数，形如 {"A": 2, "B": 3, "C": 2}
    tier_counts: dict[str, int] = field(default_factory=dict)
    freshest_at: datetime | None = None
    oldest_at: datetime | None = None
    # 有日期的那批里的平均新旧程度（天）。无日期的不参与计算
    mean_age_days: float | None = None


@dataclass(slots=True)
class EvidenceBundle:
    query: str
    local: list[Evidence]
    web: list[Evidence]
    verification: VerificationSummary
    local_mode: str
    local_reranked: bool
    # 实时侧的状态：disabled（未配置）/ ok（取到结果）/ empty（配置了但没结果）
    web_status: str
    web_provider: str | None = None
    web_topic: str | None = None
    web_days: int | None = None


def _registrable_host(host: str) -> str:
    """把 `finance.yahoo.com` 归成 `yahoo.com`。

    独立声源计数必须建立在这一层上：`news.bbc.co.uk` 与 `bbc.co.uk` 是同一家，
    按子域名数会凭空把「一家」算成「两家」。
    """

    if not host:
        return host
    # IPv4 不适用后缀规则
    if host.replace(".", "").isdigit():
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _SECOND_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _match_table(host: str, table: dict[str, str]) -> tuple[str, str] | None:
    """按「等于或为其子域」匹配。长尾域名用 `.endswith("." + key)`，避免误配。"""

    if host in table:
        return host, table[host]
    for key, label in table.items():
        if host.endswith("." + key):
            return key, label
    return None


def classify_domain(url: str) -> DomainProfile:
    """判定一个 URL 的域名身份。"""

    host = domain_of(url)
    if not host:
        return DomainProfile(domain="", name="未知来源", tier="C", note="URL 无法解析出域名")

    # 顺序很重要：先拦 UGC 子域，再查白名单。反过来 community.openai.com
    # 会在白名单里命中 openai.com，被当成一手发布方。
    if any(host.startswith(prefix) for prefix in _UGC_SUBDOMAIN_PREFIXES):
        return DomainProfile(
            domain=host,
            name=f"{_registrable_host(host)} 社区",
            tier="C",
            note="官方域名下的社区/论坛子域，内容来自用户而非发布方",
        )

    matched = _match_table(host, TIER_A_DOMAINS)
    if matched:
        _, label = matched
        return DomainProfile(domain=host, name=label, tier="A", note="一手发布方")

    matched = _match_table(host, TIER_B_DOMAINS)
    if matched:
        _, label = matched
        return DomainProfile(domain=host, name=label, tier="B", note="专业媒体，二手转述")

    for key, note in TIER_C_NOTES.items():
        if host == key or host.endswith("." + key):
            return DomainProfile(domain=host, name=_registrable_host(host), tier="C", note=note)

    return DomainProfile(
        domain=host,
        name=_registrable_host(host),
        tier="C",
        note="未收录域名，按「需要第二条独立来源」对待",
    )


def _age_days(published_at: datetime | None, now: datetime) -> int | None:
    if published_at is None:
        return None
    delta = now - published_at
    # 上游时间可能略超前（时区误差、预发布），负数按 0 算，不用它推翻整条结果
    return max(0, int(delta.total_seconds() // 86400))


def freshness_band(age_days: int | None) -> str:
    """把新旧程度压成档位，给 Agent 做措辞降级用。

    分档的意义在于：Agent 说「今天」必须对应 band=today，而 band 是算出来的、
    不是模型自己说的。模型可以用错词，但这个字段不会跟着错。
    """

    if age_days is None:
        return "unknown"
    if age_days <= 1:
        return "today"
    if age_days <= 3:
        return "this_week"
    if age_days <= 7:
        return "recent"
    return "stale"


async def _local_urls(session: AsyncSession, hits: list[WebHit]) -> set[str]:
    """一次查库，判断哪些实时结果本地已经有了。

    只跟「本次返回的本地前 N 条」比对是不够的：实时结果很可能命中库里第 300 条，
    那样重叠数会系统性偏低，看起来像「实时补了一大堆新信息」，实际全是已知内容。
    所以这里按 URL 反查全库，不做 top-N 限定。
    """

    candidates = {canonicalize_url(hit.url) for hit in hits if hit.url}
    if not candidates:
        return set()
    statement = select(Document.canonical_url).where(Document.canonical_url.in_(candidates))
    rows = await session.execute(statement)
    return {row[0] for row in rows if row[0]}


def _summarize(web: list[Evidence]) -> VerificationSummary:
    dated = [item for item in web if item.published_at is not None]
    domains = {_registrable_host(domain_of(item.url)) for item in web if item.url}
    tier_counts: dict[str, int] = {}
    for item in web:
        tier_counts[item.source_tier] = tier_counts.get(item.source_tier, 0) + 1

    ages = [item.age_days for item in dated if item.age_days is not None]
    return VerificationSummary(
        web_total=len(web),
        unique_domains=len(domains),
        overlap_with_local=sum(1 for item in web if item.in_local),
        date_unverified=sum(1 for item in web if not item.date_verified),
        tier_counts=dict(sorted(tier_counts.items())),
        freshest_at=max((item.published_at for item in dated), default=None),
        oldest_at=min((item.published_at for item in dated), default=None),
        mean_age_days=round(sum(ages) / len(ages), 1) if ages else None,
    )


def _web_sort_key(item: Evidence) -> tuple:
    """实时证据的排序：先「有没有日期」，再新旧，最后相关性。

    这不是一个「混合分」，而是一个显式写出的优先级链。理由：阶段 5 的用途是补
    时效盲区，一条不知道日期的结果无法承担这个用途；把它按相关性排到带日期的
    结果前面，等于用未知冒充已知。
    """

    return (
        item.date_verified,
        item.published_at or datetime.min.replace(tzinfo=UTC),
        item.score or 0.0,
    )


async def collect_evidence(
    session: AsyncSession,
    query: str,
    *,
    days: int = 7,
    local_limit: int = 8,
    web_limit: int = 8,
    include_web: bool = True,
    # Agent 的 search_web 工具只要实时这一段。不关掉本地检索的话，每调一次
    # 实时搜索就会白跑一次向量检索（几十毫秒 + 一次 embedder 调用），
    # 而结果会被调用方全部丢掉。
    include_local: bool = True,
    search_mode: str = "hybrid",
    # 透传给精排：只排多少条候选。Agent 只要 6 条结果，排 40 条纯属浪费 CPU。
    rerank_candidates: int | None = None,
) -> EvidenceBundle:
    """取一个问题的完整证据集：本地检索 + 实时搜索，各自标注可信度。"""

    now = datetime.now(UTC)

    local: list[Evidence] = []
    local_reranked = False

    if include_local:
        page = await document_service.list_documents(
            session,
            q=query,
            limit=local_limit,
            search_mode=search_mode if search_mode in ("vector", "hybrid") else "keyword",
            rerank_candidates=rerank_candidates,
        )
        local_reranked = page.reranked

        for document, source in page.items:
            match = page.matches.get(document.id)
            age = _age_days(document.published_at, now)
            local.append(
                Evidence(
                    channel="local",
                    title=document.title,
                    url=document.canonical_url,
                    snippet=document.summary,
                    published_at=document.published_at,
                    source_name=source.name,
                    # 本地来源的分级来自 sources 表的 credibility_tier，取值 A/B/C：
                    # A 一手（openai_news_en、ruanyifeng_zh、GitHub Release）、
                    # B 专业媒体（36kr_zh、infoq_zh、arxiv）、C 聚合与 UGC（hackernews_en）。
                    # web 侧用的是同一套语义，所以两边的 tier 可以直接比较——
                    # 这件事一开始写错过，注释里说「C 是 web 独有档」，查了 sources 表
                    # 才发现 HN 本来就是 C。
                    source_tier=source.credibility_tier,
                    score=match.similarity if match else None,
                    date_verified=document.published_at is not None,
                    in_local=True,
                    age_days=age,
                    freshness=freshness_band(age),
                    document_id=document.id,
                    char_start=match.char_start if match else None,
                )
            )

    web: list[Evidence] = []
    verification = VerificationSummary()
    web_status = "disabled"
    provider = None

    if include_web:
        provider = get_web_search()
        if provider is None:
            # 明确要求实时证据却没有后端：报错，不返回一个「看起来成功但 web 为空」
            # 的响应。那种响应会让调用方以为「今天全网没有相关新闻」。
            raise WebSearchUnavailable(
                "实时搜索未启用（WEB_SEARCH_PROVIDER 为空），无法提供 web 证据。\n"
                "  开发期可用 keyless：WEB_SEARCH_PROVIDER=tavily WEB_SEARCH_KEYLESS=true"
            )

        hits = await provider.search(query, limit=web_limit, days=days, topic=resolved_topic())
        local_urls = await _local_urls(session, hits)
        for hit in hits:
            profile = classify_domain(hit.url)
            age = _age_days(hit.published_at, now)
            web.append(
                Evidence(
                    channel="web",
                    title=hit.title,
                    url=hit.url,
                    snippet=hit.snippet or None,
                    published_at=hit.published_at,
                    source_name=profile.name,
                    source_tier=profile.tier,
                    score=hit.score,
                    date_verified=hit.date_verified,
                    in_local=canonicalize_url(hit.url) in local_urls,
                    age_days=age,
                    freshness=freshness_band(age),
                )
            )
        web.sort(key=_web_sort_key, reverse=True)
        verification = _summarize(web)
        web_status = "ok" if web else "empty"

    return EvidenceBundle(
        query=query,
        local=local,
        web=web,
        verification=verification,
        local_mode=search_mode,
        local_reranked=local_reranked,
        web_status=web_status,
        web_provider=provider.name if provider else None,
        web_topic=resolved_topic() if include_web else None,
        web_days=days if include_web else None,
    )

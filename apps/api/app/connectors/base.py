"""Connector 抽象与统一数据结构。

约定：
- 每个来源一个 Connector，输出统一的 CanonicalDocument；
- Connector 不碰数据库、不碰 ORM，只负责"抓 + 解析 + 归一"，因此可以单测；
- 失败向上抛 UpstreamError，由采集服务决定重试/降频/记健康。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 这些源的正文页已实测为确定性失败（36氪是前端渲染的 SPA，服务端 HTML 里
# 连 meta 描述都没有；InfoQ 无论换什么 UA 都 405；OpenAI 是 Cloudflare 的
# bot 管理、一律 403）。它们的条目一律不进网页抓取队列——进了也是每轮白跑、
# 每轮失败，还会把「抓取失败率」长期钉在高位，掩盖真正需要关注的失败。
# 放在 base 里是因为 Connector 与采集层都要用它判断正文来源。
#
# infoq_zh / sspai_zh 加进来是另一类原因：不是抓不到，是不该抓。
# 这两家只授权了 RSS 摘要（feed 里也没给正文），没有页面抓取的许可。
# 把它们留在候选里，enrich 每轮都会去请求文章页——技术上是爬虫行为，
# 法律上没有依据，收益又只是把 150 字的摘要换成 600 字。
NO_FETCH_SOURCES = ("36kr_zh", "infoq_en", "openai_news_en", "infoq_zh", "sspai_zh")


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Connector 运行所需的来源参数（从 sources 表投影而来）。"""

    source_key: str
    name: str
    base_url: str
    access_method: str
    region: str = "global"
    language: str | None = None
    rights_mode: str = "metadata_excerpt"
    request_timeout_seconds: int = 20
    max_items: int = 200
    parser_version: str = "rss-v1"
    # 上一次成功采集留下的游标，结构由各 Connector 自行约定。
    # 为 None 表示该源还没有增量基线，Connector 应退化为全量拉取。
    cursor: dict[str, Any] | None = None

    @classmethod
    def from_source(
        cls,
        source,
        *,
        max_items: int = 200,
        cursor: dict[str, Any] | None = None,
    ) -> SourceSpec:
        """从 ORM Source 构造，避免 Connector 依赖数据库模型细节。"""

        return cls(
            source_key=source.source_key,
            name=source.name,
            base_url=source.base_url,
            access_method=source.access_method,
            region=source.region,
            language=source.language,
            # 之前这一行漏了，导致 sources.rights_mode 永远读不到，
            # 所有源都在用 dataclass 的默认值 metadata_excerpt。
            rights_mode=getattr(source, "rights_mode", "metadata_excerpt") or "metadata_excerpt",
            request_timeout_seconds=source.request_timeout_seconds,
            max_items=max_items,
            parser_version=source.parser_version,
            cursor=cursor if cursor is not None else source.sync_cursor,
        )


@dataclass(slots=True)
class CanonicalDocument:
    """统一情报条目（L1 标准文档层的输入形态）。"""

    source_key: str
    external_id: str
    url: str
    canonical_url: str
    title: str
    fetched_at: datetime
    content_hash: str
    summary: str | None = None
    content_text: str | None = None
    language: str | None = None
    region: str = "global"
    content_type: str = "article"
    published_at: datetime | None = None
    topics: list[str] = field(default_factory=list)
    rights_mode: str = "metadata_excerpt"
    quality_flags: list[str] = field(default_factory=list)
    # 各源的互动指标（score / descendants / download_count …）。口径互不相同，原样保留，
    # 不在 Connector 里拍一个跨源可比的分数——归一由采集层按源内相对位置完成。
    # 空字典表示该源不暴露任何热度信号（RSS、arXiv 都是这样）。
    engagement: dict[str, Any] = field(default_factory=dict)
    # 内容指纹。有些 feed 的 summary 字段本身就是全文（36氪一次给 3000~14000 字），
    # 但 rights_note 明确「全文不入库」，所以全文不能被写到 content_text。
    # 折中办法：在 Connector 局部用全文算 64 位指纹——哈希不构成对原文的复制，
    # 判重能力照样拿到，而全文本身不随这个对象传递出去，也就不会被误写入库。
    fingerprint: int | None = None
    # 正文来源。留空表示走默认推导（pending → 等抓取）；"from_feed" 表示
    # feed 自己就带长文本，不需要也不应该再去抓网页——抓也抓不到（SPA / 反爬）。
    content_status: str | None = None


@dataclass(slots=True)
class FetchOutcome:
    """一次抓取的结果，包含可观测信息（状态码、耗时）与下一轮的游标。"""

    documents: list[CanonicalDocument]
    http_status: int | None = None
    latency_ms: int = 0
    not_modified: bool = False
    # 采集服务只在本次成功且 next_cursor 非空时才覆盖 sources.sync_cursor。
    # 失败时不写回，保证游标只会前进到"确实抓取成功"的位置。
    next_cursor: dict[str, Any] | None = None


class BaseConnector(ABC):
    """所有来源连接器的基类。"""

    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec

    @abstractmethod
    async def fetch(self, cursor: dict[str, Any] | None = None) -> FetchOutcome:
        """抓取并解析，返回规范化文档列表。

        cursor 为上一轮成功采集留下的游标（结构由各 Connector 自定）；
        为 None 时从 spec.cursor 兜底，两者都为空则表示还没有增量基线。
        """

        raise NotImplementedError

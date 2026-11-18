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

    @classmethod
    def from_source(cls, source, *, max_items: int = 200) -> "SourceSpec":
        """从 ORM Source 构造，避免 Connector 依赖数据库模型细节。"""

        return cls(
            source_key=source.source_key,
            name=source.name,
            base_url=source.base_url,
            access_method=source.access_method,
            region=source.region,
            language=source.language,
            request_timeout_seconds=source.request_timeout_seconds,
            max_items=max_items,
            parser_version=source.parser_version,
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


@dataclass(slots=True)
class FetchOutcome:
    """一次抓取的结果，包含可观测信息（状态码、耗时）。"""

    documents: list[CanonicalDocument]
    http_status: int | None = None
    latency_ms: int = 0
    not_modified: bool = False


class BaseConnector(ABC):
    """所有来源连接器的基类。"""

    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec

    @abstractmethod
    async def fetch(self, cursor: str | None = None) -> FetchOutcome:
        """抓取并解析，返回规范化文档列表。"""

        raise NotImplementedError

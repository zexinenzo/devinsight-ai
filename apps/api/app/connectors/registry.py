"""Connector 工厂：按 access_method 选择实现。

rss / atom 是协议统一的，一个实现覆盖全部来源；
api 则不同——HN 是 Firebase JSON，GitHub 是带限流头的 REST，arXiv 是 Atom over HTTPS，
协议差异大到无法用一个实现兜住，所以按 source_key 显式绑定到具体类。
新增 API 源时只需在 API_CONNECTORS 里加一行，不必改动分派逻辑。
"""

from __future__ import annotations

from app.connectors.arxiv import ArxivConnector
from app.connectors.base import BaseConnector, SourceSpec
from app.connectors.github_releases import GitHubReleaseConnector
from app.connectors.hackernews import HackerNewsConnector
from app.connectors.rss import RSSConnector

# source_key -> Connector 实现。显式声明而非按前缀猜，避免名字相似就路由错。
API_CONNECTORS: dict[str, type[BaseConnector]] = {
    "hackernews_en": HackerNewsConnector,
}

# 一类来源一个实现（比如每个被追踪的 GitHub 仓库是一个独立来源），
# 逐个列 source_key 会变成几十行样板，所以用前缀绑定。
API_CONNECTOR_PREFIXES: tuple[tuple[str, type[BaseConnector]], ...] = (
    ("github_release_", GitHubReleaseConnector),
    ("arxiv_", ArxivConnector),
)


def get_connector(spec: SourceSpec) -> BaseConnector:
    method = (spec.access_method or "").lower()
    if method in ("rss", "atom"):
        return RSSConnector(spec)
    if method == "api":
        connector_cls = API_CONNECTORS.get(spec.source_key)
        if connector_cls is None:
            connector_cls = next(
                (cls for prefix, cls in API_CONNECTOR_PREFIXES if spec.source_key.startswith(prefix)),
                None,
            )
        if connector_cls is None:
            raise NotImplementedError(
                f"source_key={spec.source_key!r} 尚未绑定 API Connector（在 API_CONNECTORS 中登记后可用）"
            )
        return connector_cls(spec)
    raise NotImplementedError(f"access_method={spec.access_method!r} 的 Connector 尚未实现（阶段 2 起逐个接入）")

"""Connector 工厂：按 access_method 选择实现。

阶段 1 只实现 rss；api / html 留出明确报错，避免"看起来支持其实没写"。
"""

from __future__ import annotations

from app.connectors.base import BaseConnector, SourceSpec
from app.connectors.rss import RSSConnector


def get_connector(spec: SourceSpec) -> BaseConnector:
    method = (spec.access_method or "").lower()
    if method in ("rss", "atom"):
        return RSSConnector(spec)
    raise NotImplementedError(f"access_method={spec.access_method!r} 的 Connector 尚未实现（阶段 2 起逐个接入）")

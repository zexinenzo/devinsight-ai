"""文档（情报）相关的响应模型。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class DocumentSourceBrief(BaseModel):
    """文档列表里携带的来源摘要，避免前端二次请求。"""

    id: uuid.UUID
    source_key: str
    name: str
    source_type: str
    credibility_tier: str
    region: str


class DocumentListItem(ORMModel):
    id: uuid.UUID
    title: str
    summary: str | None = None
    canonical_url: str
    original_url: str | None = None
    language: str | None = None
    region: str
    topics: list[str]
    content_type: str
    published_at: datetime | None = None
    fetched_at: datetime
    rights_mode: str
    quality_flags: list[str]
    source: DocumentSourceBrief


class DocumentDetail(DocumentListItem):
    content_text: str | None = None
    content_hash: str

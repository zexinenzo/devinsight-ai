"""文档（情报）相关的响应模型。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class DocumentSourceBrief(BaseModel):
    """文档列表里携带的来源摘要，避免前端二次请求。"""

    id: uuid.UUID
    source_key: str
    name: str
    source_type: str
    credibility_tier: str
    region: str


class DocumentMatch(BaseModel):
    """语义检索命中说明。

    没有 `text` 字段，也不该有：向量索引库里只存向量和字符区间，原文一个字都没落库
    （见 `document_chunks` 表注释）。所以这里能给出的只有「多相关」和「在原文的哪个位置」，
    正文仍然要回原站看——这是授权边界决定的，不是实现偷懒。
    """

    mode: str
    # 余弦相似度，1 表示方向一致；None 表示这条只被关键词召回（hybrid 模式）
    similarity: float | None = None
    # 最佳命中块在原文字符串里的起点，前端可渲染成「命中在第 1200 字附近」
    char_start: int | None = None


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
    # --- 阶段 3：热度与去重 ---
    # 归一后的源内热度分（log1p），None 表示该源不暴露互动数据
    engagement_score: float | None = None
    # 原始指标原样透出，前端按源渲染不同文案（HN 显示"票"，GitHub 显示"次下载"）
    engagement_raw: dict = Field(default_factory=dict)
    content_status: str = "pending"
    # 失败原因，前端可直接展示用于排查（HTTP 403 / Timeout …）
    content_error: str | None = None
    # 非空表示这条是转载，值指向被转载的原始条目
    dup_of: uuid.UUID | None = None
    cluster_id: uuid.UUID | None = None
    # 同一事件共有几家在报；1 表示只有这一条
    cluster_size: int = 1
    # 语义检索时才有值；关键词检索返回 null，前端不必据此分支
    match: DocumentMatch | None = None
    source: DocumentSourceBrief


class DocumentDetail(DocumentListItem):
    content_text: str | None = None
    content_hash: str
    simhash: str | None = None

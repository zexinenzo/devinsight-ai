"""证据集的响应模型。

与 `document.py` 分开的理由：`DocumentListItem` 描述的是「库里的一条情报」，
有 `dup_of` / `cluster_id` / `content_status` 这些只对已入库条目成立的字段。
证据描述的是「支撑某个问题的一条依据」，它可能来自库里，也可能来自全网，
共同的字段只有「是什么、在哪、多新、谁说的」。硬塞进同一个模型会让两边
各有一半字段恒为 null。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class EvidenceItem(ORMModel):
    """一条证据。`channel` 决定哪些字段有意义。"""

    # local 来自本地库（可定位到段落），web 来自实时搜索（可追溯到原文）
    channel: str
    title: str
    url: str
    snippet: str | None = None
    published_at: datetime | None = None
    source_name: str
    # A 一手 / B 专业媒体 / C 聚合与 UGC。本地（sources.credibility_tier）与
    # web（域名分级）共用这套语义，两边可以直接比较
    source_tier: str
    # 渠道内的排序分：本地是余弦相似度，web 是上游相关性。**跨渠道不可比**
    score: float | None = None
    # 发布时间是否确认。False 表示「不知道多旧」，不是「很旧」
    date_verified: bool = True
    # 这条 URL 是否已在本地库。对本地证据恒为 true，信息量全在 web 侧
    in_local: bool = False
    age_days: int | None = None
    # today / this_week / recent / stale / unknown。Agent 的措辞要跟这个走
    freshness: str = "unknown"
    # 以下两项只有 local 有值
    document_id: uuid.UUID | None = None
    char_start: int | None = None


class VerificationOut(BaseModel):
    """证据集的整体可信度画像。这些数字是给程序做判断用的，不是装饰。"""

    web_total: int = 0
    # 按可注册域名去重后的独立声源数（同一家的子域名只算 1 个）
    unique_domains: int = 0
    # 与本地库重叠的条数——这些不是新信息，是已知信息被再次看到
    overlap_with_local: int = 0
    # 上游没给日期的条数。>0 时「最新」类措辞就该降级成「近期」
    date_unverified: int = 0
    tier_counts: dict[str, int] = Field(default_factory=dict)
    freshest_at: datetime | None = None
    oldest_at: datetime | None = None
    mean_age_days: float | None = None


class EvidenceBundleOut(BaseModel):
    query: str
    local: list[EvidenceItem]
    web: list[EvidenceItem]
    verification: VerificationOut
    local_mode: str
    # 本地这批有没有经过 cross-encoder 精排。false 且 mode 是 hybrid 时，
    # 说明模型缺失、走了降级路径
    local_reranked: bool = False
    # disabled（未配置）/ ok（有结果）/ empty（配了但这次没搜到）
    web_status: str
    web_provider: str | None = None
    web_topic: str | None = None
    web_days: int | None = None

"""核心 ORM 模型（阶段 1：来源注册 + 标准文档）。

后续阶段的 entities / events / chunks / fact_claims / research_runs 在各自阶段加入，
避免此阶段建出一堆没有写入方的空表。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 来源健康状态：unknown 未探测 / healthy 正常 / degraded 偶发失败 / failing 连续失败 / disabled 人工停用
HEALTH_STATUSES = ("unknown", "healthy", "degraded", "failing", "disabled")

# 采集批次状态
RUN_STATUSES = ("running", "succeeded", "partial", "failed")


class Source(Base):
    """来源注册表：一个来源一行，可在后台启停、调频率、看健康。"""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("canonical_domain", "name", name="uq_sources_domain_name"),
        Index("ix_sources_health", "health_status", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # 稳定的字符串键，代码与配置里用它引用来源（如 "36kr_zh"）
    source_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_domain: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'global'"))
    language: Mapped[str | None] = mapped_column(Text)
    access_method: Mapped[str] = mapped_column(Text, nullable=False)  # rss / api / html
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    terms_url: Mapped[str | None] = mapped_column(Text)
    robots_url: Mapped[str | None] = mapped_column(Text)
    rights_note: Mapped[str | None] = mapped_column(Text)
    # 可信度分层 A/B/C/D，见手册 3.3
    credibility_tier: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'B'"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("360"))
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20"))
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    health_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'unknown'"))
    parser_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'rss-v1'"))
    # 增量游标：由各 Connector 自定义结构（如 HN 的 {"max_item_id": 123}）。
    # 用 JSONB 而非独立列，是因为不同来源的游标形态差异大，且它只是优化项——
    # 丢了顶多退化成全量拉取，唯一约束仍保证不重复入库。
    sync_cursor: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    runs: Mapped[list[IngestionRun]] = relationship(back_populates="source", cascade="all, delete-orphan")
    documents: Mapped[list[Document]] = relationship(back_populates="source", cascade="all, delete-orphan")


class IngestionRun(Base):
    """每次采集批次一条记录：可回答“这轮发生了什么、失败在哪”。"""

    __tablename__ = "ingestion_runs"
    __table_args__ = (Index("ix_ingestion_runs_source_started", "source_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    http_status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'schedule'"))

    source: Mapped[Source] = relationship(back_populates="runs")


class Document(Base):
    """L1 标准文档层：统一结构的情报条目。"""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
        Index("ix_documents_published", text("published_at DESC")),
        Index("ix_documents_fetched", text("fetched_at DESC")),
        Index("ix_documents_hash", "content_hash"),
        # 中英文混排先统一用 simple 配置；中文分词方案在阶段 5 引入
        Index(
            "ix_documents_search",
            text("to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, ''))"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    content_text: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'global'"))
    topics: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'article'"))
    # published_at 是来源声称的发布时间；fetched_at 是系统采集时间，二者不得混用
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    rights_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'metadata_excerpt'"))
    quality_flags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    quality_score: Mapped[float | None] = mapped_column(Numeric(5, 4))
    is_indexed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # --- 阶段 3：热度 / 正文 / 去重 ---
    # 归一后的热度分（源内可比），原始指标留 JSONB 保留可追溯性
    engagement_score: Mapped[float | None] = mapped_column(Float)
    engagement_raw: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # pending 未抓 / fetched 已抓 / empty 源站无正文 / failed 抓取失败 / skipped 不合规跳过
    content_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    content_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 失败原因：403 与 Timeout 都是 failed，但只有后者值得下一轮重试
    content_error: Mapped[str | None] = mapped_column(Text)
    # SimHash 是 64 位无符号，PG BIGINT 有符号，存十六进制字符串避免符号位来回翻转
    simhash: Mapped[str | None] = mapped_column(Text)
    # 判为转载时指向被转载的原始条目；cluster_id 是同事件聚簇标识（取簇内最早一条的 id）
    dup_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    source: Mapped[Source] = relationship(back_populates="documents")


class SourceHealthLog(Base):
    """来源健康探测记录，用于画健康曲线与排查失败。"""

    __tablename__ = "source_health_logs"
    __table_args__ = (Index("ix_source_health_logs_source_checked", "source_id", "checked_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status_code: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    error_message: Mapped[str | None] = mapped_column(Text)

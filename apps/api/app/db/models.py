"""核心 ORM 模型（阶段 1：来源注册 + 标准文档）。

后续阶段的 entities / events / chunks / fact_claims / research_runs 在各自阶段加入，
避免此阶段建出一堆没有写入方的空表。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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

from app.core.config import settings
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


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
    # 源的落库授权级别，决定 content_service._store_text 截多少字。
    # 这一列曾经缺失，导致所有源都吃 SourceSpec 的 dataclass 默认值，
    # rights_note 那句「全文不入库」其实只是一句注释、没有任何代码效力。
    rights_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'metadata_excerpt'"))
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
    # 增量游标：由各 Connector 自定义结构（如 HN 的 {"seen_ids": [123, ...]}）。
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
        # 同一来源的同一篇文章只能有一行。external_id 会因为写入路径不同
        # （feed 的 Atom id / 回填的 URL）而不同，canonical_url 也会因为
        # feed 给 http、归档页给 https 而不同，但「去掉 scheme 的 URL」是同一个。
        # 所以身份建在表达式上：scheme 不参与判定。
        Index(
            "uq_documents_source_canonical_url",
            "source_id",
            text("regexp_replace(canonical_url, '^https?://', '')"),
            unique=True,
        ),
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
    # 抓取尝试次数。两个用途：一是决定退避间隔（6h / 24h / 72h），二是让确定性
    # 失败（403/付费墙）直接把预算烧光——筛选时只比数字，不必再做一遍字符串匹配
    content_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
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


class DocumentChunk(Base):
    """正文块的向量索引：**只存向量和位置，不存文本**。

    这张表是「授权」与「召回」的折中产物。`metadata_excerpt` 级别的源只允许存
    600 字片段，可是长文里真正有用的信息往往在第三段以后。做法是：正文在抓取时
    在内存里切成块、算成向量，然后把文字丢掉，只留下 384 个浮点数和一个字符区间。

    所以这一列里查不到任何一句原文——它不能用来还原文章，只能回答「哪篇文章的
    哪一段和你的问题接近」。展示给用户的仍然是标题、摘要和原文链接。

    `model` 必须逐行记录：换模型就等于换向量空间，两种向量混在一张表里做余弦
    是没有意义的。查询时按 model 过滤，混进了别的模型能被 `group by model` 查出来。
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "model", "chunk_index", name="uq_document_chunks_doc_model_index"
        ),
        # 句向量是 384 维稠密向量，HNSW 让「最近邻」不必全表扫。
        # 余弦距离对应 vector_cosine_ops，建索引时必须和查询用的算子一致，
        # 否则索引不会被使用（而且不会报错，只是慢）。
        Index(
            "ix_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # 块在原文中的序号，从 0 起。和 char_start 一起用于告诉用户「命中在第几段」
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dimensions), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Entity(Base):
    """规范实体：公司、产品、人物、模型。

    `canonical_key` 是比对用的键，不是展示用的名字。库里出现过的写法
    （`OpenAI` / `openai` / `Open AI`）都要收敛到同一行，否则时间线会被切成三段：
    每段都只有一部分事实，而人看到的「这家公司做过什么」永远不完整。

    `kind` 参与唯一约束：`Apple` 作为公司是一个实体，作为水果是另一个。只按名字
    去重会把它们并成一个，之后所有事实都混在一起。
    """

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("kind", "canonical_key", name="uq_entities_kind_canonical"),
        Index("ix_entities_canonical_key", "canonical_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_key: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FactClaim(Base):
    """一条事实声明，带两条时间轴。

    **这是阶段 7 唯一不能简化的地方**：`valid_from`/`valid_to` 是事实在现实中成立的
    区间，`recorded_at` 是系统写下这条记录的时间。两者绝不能压成一列——压成一列就
    意味着「用新状态覆盖旧状态」，而覆盖之后，历史上那个状态和支撑它的证据一起消失。

    写入纪律：**只 INSERT，不 UPDATE 值**。状态变化 = 插新行 + 把旧行的 `valid_to`
    闭合。`valid_to IS NULL` 表示「至今仍成立」。

    这条纪律由数据库触发器 `trg_fact_claims_no_rewrite` 强制（见迁移 0009）：
    允许的 UPDATE 只有闭合 `valid_to`、指向 `superseded_by`、以及撤回（迁移 0010 加的
    三列）；改 `object_text` / `predicate` / `valid_from` / `subject_id` /
    `document_id` / `recorded_at` 会直接抛异常。
    ORM 层面看不出来，所以写在这里免得有人以为是普通可改字段。

    **表里有三个「这条记录不再有效」的字段，语义各不相同，填错会让 `as_of` 撒谎**：

    | 字段 | 轴 | 含义 |
    |---|---|---|
    | `valid_to` | 世界 | 这件事在现实中什么时候不再成立 |
    | `superseded_by` | 世界 | 被哪条新事实取代（因为世界变了） |
    | `retracted_at` | 认识 | 我们什么时候不再声称这条记录（因为它是错的） |

    撤回不删除，是因为判错无法挽回：行没了，证据链就没了，时间线少一条的表现是
    「这件事没发生」，而不是「这里有个待修的洞」。见 `docs/阶段8-数据修复设计.md`。

    `valid_from_source` 记录日期是怎么来的：`stated` 文中明说、`published` 用发文
    时间兜底、`unknown` 无从得知。时间线上「11 月 18 日」看起来一样硬，但一个是
    事实、一个是推测，这个区别必须留存。
    """

    __tablename__ = "fact_claims"
    __table_args__ = (
        # 重跑去重：同一文档对同一 (主语, 谓词, 宾语) 只有一条
        UniqueConstraint(
            "subject_id",
            "predicate",
            "object_text",
            "document_id",
            name="uq_fact_claims_identity",
        ),
        # 注意命名约定（app/db/base.py）会补上 `ck_<表名>_` 前缀，所以这里只写后缀。
        # 写成全名 `ck_fact_claims_period` 会得到 `ck_fact_claims_ck_fact_claims_period`，
        # 与迁移 0009 里建出来的名字对不上——元数据与真库不一致，autogenerate 会想重建。
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
            name="period",
        ),
        CheckConstraint(
            "valid_from_source IN ('stated', 'published', 'unknown')",
            name="from_source",
        ),
        # 撤回必须给理由：没有理由的撤回是一次不可审计的判断，
        # 而「不可审计」正是当初模型编造事实的同类问题
        CheckConstraint(
            "retracted_at IS NULL OR retracted_reason IS NOT NULL",
            name="retraction",
        ),
        # 主查询是「某实体时间线按时间倒序」，联合索引让排序不必再走 sort
        Index("ix_fact_claims_subject_valid", "subject_id", text("valid_from DESC")),
        Index("ix_fact_claims_predicate", "predicate"),
        Index("ix_fact_claims_document", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object_text: Mapped[str] = mapped_column(Text, nullable=False)
    object_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL")
    )
    # 现实时间。允许 NULL：报道可能完全不提时间，宁可留空也不拿采集时间冒充
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    valid_from_source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'")
    )
    # 系统时间：这条记录什么时候被写进来（事后复盘「当时我们知道什么」靠它）
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    evidence_text: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    # 指向取代它的那条。保留指针而不是删除旧行，才能解释「这条为什么失效了」
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fact_claims.id", ondelete="SET NULL")
    )
    # 撤回（迁移 0010）：**认识轴**，不是世界轴。NULL 表示「仍然采信这条记录」。
    # 撤回后行与证据都留着，恢复只需把两列置回 NULL——这是选撤回而不是 DELETE 的理由。
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retracted_reason: Mapped[str | None] = mapped_column(Text)
    # 撤回后被哪条替代（同一文档 + 同一主语 + 同一谓词才能确定，定不准就留空）。
    # 刻意不与上面的 `superseded_by` 共名：那个是世界轴，这个属于认识轴
    retracted_replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fact_claims.id", ondelete="SET NULL")
    )

"""阶段 1 初始表：来源注册、采集批次、标准文档、来源健康。

Revision ID: 0001_init
Revises:
Create Date: 2026-11-18 21:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 向量检索在本阶段不写入，但先把扩展装好，避免后续阶段再改库
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("canonical_domain", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), server_default=sa.text("'global'"), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("access_method", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("terms_url", sa.Text(), nullable=True),
        sa.Column("robots_url", sa.Text(), nullable=True),
        sa.Column("rights_note", sa.Text(), nullable=True),
        sa.Column("credibility_tier", sa.Text(), server_default=sa.text("'B'"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("2"), nullable=False),
        sa.Column("poll_interval_minutes", sa.Integer(), server_default=sa.text("360"), nullable=False),
        sa.Column("request_timeout_seconds", sa.Integer(), server_default=sa.text("20"), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("health_status", sa.Text(), server_default=sa.text("'unknown'"), nullable=False),
        sa.Column("parser_version", sa.Text(), server_default=sa.text("'rss-v1'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("canonical_domain", "name", name="uq_sources_domain_name"),
        sa.UniqueConstraint("source_key", name="uq_sources_source_key"),
    )
    op.create_index("ix_sources_health", "sources", ["health_status", "enabled"])

    op.create_table(
        "ingestion_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("fetched_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.Text(), server_default=sa.text("'schedule'"), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name="fk_ingestion_runs_source_id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_runs"),
    )
    op.create_index("ix_ingestion_runs_source_started", "ingestion_runs", ["source_id", "started_at"])

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), server_default=sa.text("'global'"), nullable=False),
        sa.Column("topics", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("content_type", sa.Text(), server_default=sa.text("'article'"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("rights_mode", sa.Text(), server_default=sa.text("'metadata_excerpt'"), nullable=False),
        sa.Column("quality_flags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("quality_score", sa.Numeric(5, 4), nullable=True),
        sa.Column("is_indexed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name="fk_documents_source_id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
    )
    op.create_index("ix_documents_published", "documents", [sa.text("published_at DESC")])
    op.create_index("ix_documents_fetched", "documents", [sa.text("fetched_at DESC")])
    op.create_index("ix_documents_hash", "documents", ["content_hash"])
    op.create_index(
        "ix_documents_search",
        "documents",
        [sa.text("to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, ''))")],
        postgresql_using="gin",
    )

    op.create_table(
        "source_health_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name="fk_source_health_logs_source_id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_source_health_logs"),
    )
    op.create_index("ix_source_health_logs_source_checked", "source_health_logs", ["source_id", "checked_at"])


def downgrade() -> None:
    op.drop_table("source_health_logs")
    op.drop_table("documents")
    op.drop_table("ingestion_runs")
    op.drop_table("sources")

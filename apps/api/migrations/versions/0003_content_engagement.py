"""阶段 3：热度信号、正文抓取状态、去重与事件簇。

为什么要这些列：

- `engagement_score` / `engagement_raw`：阶段 2 只存了标题和链接，没存"有多少人
  在看"。没有这个信号，"先抓哪批正文"就只能按时间拍，而时间与大众关注度无关。
  原始值放 JSONB（各源指标口径不同，score / descendants / download_count 不能合并
  成一列），归一后的分值单独一列以便排序。

- `content_status`：正文抓取是按批次推进的，必须能回答"这条抓过没有、为什么没抓到"。
  用状态机而不是"content_text IS NULL"，因为「抓了但源站没有正文」和「还没抓」
  是两件事，前者重试一百次也没用。

- `simhash`：正文的近似指纹，用来区分「同一文章多站转载」与「同一事件多源报道」。
  存 16 位十六进制字符串而不是 BIGINT：SimHash 是 64 位无符号，PostgreSQL 的
  BIGINT 是有符号的，存进去要来回翻转符号位，得不偿失。判重在应用层做汉明距离，
  数据库里不需要对它做数值运算。

- `dup_of`：判为转载时指向被转载的那条（保留最早的原始来源）。
- `cluster_id`：同一事件的多个独立报道归到同一个簇，取簇内最早那条的 id 作为簇标识，
  不另建表——事件簇在本阶段只用于展示"这件事有几家在报"，没有独立的生命周期。

Revision ID: 0003_content_engagement
Revises: 0002_source_sync_cursor
Create Date: 2026-11-19 01:20:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_content_engagement"
down_revision: str | None = "0002_source_sync_cursor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("engagement_score", sa.Float(), nullable=True))
    op.add_column(
        "documents",
        sa.Column(
            "engagement_raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "documents",
        sa.Column("content_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
    )
    op.add_column("documents", sa.Column("content_fetched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("simhash", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("dup_of", sa.UUID(as_uuid=True), nullable=True))
    op.add_column("documents", sa.Column("cluster_id", sa.UUID(as_uuid=True), nullable=True))

    # 索引只建普通 ASC：PostgreSQL 可以反向扫描，ORDER BY engagement_score DESC
    # 照样走这个索引。写 DESC 索引的收益要到大表高并发才体现，这里没必要多一层
    # 表达式索引带来的方言风险。
    op.create_index("ix_documents_engagement", "documents", ["engagement_score"])
    op.create_index("ix_documents_content_status", "documents", ["content_status"])
    op.create_index("ix_documents_simhash", "documents", ["simhash"])
    op.create_index("ix_documents_cluster", "documents", ["cluster_id"])
    op.create_foreign_key(
        "fk_documents_dup_of", "documents", "documents", ["dup_of"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_documents_dup_of", "documents", type_="foreignkey")
    op.drop_index("ix_documents_cluster", table_name="documents")
    op.drop_index("ix_documents_simhash", table_name="documents")
    op.drop_index("ix_documents_content_status", table_name="documents")
    op.drop_index("ix_documents_engagement", table_name="documents")
    for column in ("cluster_id", "dup_of", "simhash", "content_fetched_at", "content_status", "engagement_raw", "engagement_score"):
        op.drop_column("documents", column)

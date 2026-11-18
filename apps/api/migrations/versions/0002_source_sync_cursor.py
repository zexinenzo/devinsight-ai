"""阶段 2：来源表增加增量游标列。

为什么单独一列而不是塞进现有字段：
- 游标结构与来源类型强相关（RSS 不需要，HN 记 item id，GitHub 记 since 时间戳），
  用 JSONB 承载，避免每接一个新源就改表结构；
- 游标可空，表示"还没有增量基线"，Connector 遇到 None 会退化为全量拉取。

Revision ID: 0002_source_sync_cursor
Revises: 0001_init
Create Date: 2026-11-18 23:50:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_source_sync_cursor"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("sync_cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sources", "sync_cursor")

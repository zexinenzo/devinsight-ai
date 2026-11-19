"""阶段 3 补丁：正文抓取失败原因。

没有这一列时，`content_status='failed'` 只说明「没抓到」，不说明「为什么」。
排查时只能翻日志，而日志按行滚动，几十批之后就查不到当初那条了。

更实际的问题是：失败原因决定了要不要重试。
`HTTP 403`（反爬/付费墙）重试一百次也没用，`Timeout`（网络抖动）下一轮大概率能成。
把两者混成一个 failed，重试策略就只能拍脑袋。

Revision ID: 0004_content_error
Revises: 0003_content_engagement
Create Date: 2026-11-19 01:35:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_content_error"
down_revision: str | None = "0003_content_engagement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("content_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "content_error")

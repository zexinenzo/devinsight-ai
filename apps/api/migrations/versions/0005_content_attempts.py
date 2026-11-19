"""阶段 3 补丁：抓取尝试次数，用于退避重试。

`failed` 在此之前是终态：`select_candidates` 只取 `pending`，一旦失败就再也
不会被挑中。实测这带来两个后果：本机代理对 reuters / ft 这类域名的 502 是
**临时性**的，代理规则一改就能通，但库里那 136 条永远停在 failed；而
`Timeout` 这种更典型的瞬时失败也没有第二次机会。

加一列而不是两张表：重试策略只需要知道「试过几次」，独立的重试队列表在
这个规模下是过度设计。

**存量回填是本迁移的关键一半。** 已有 228 条 failed 的 `content_attempts`
默认是 0，若不处理，迁移后第一轮 enrich 会把它们全部当成「一次没试过」，
其中 55 条 `HTTP 403/405` 会被白白重打一遍——正是当年决定「不自动重试」的
那批请求。所以这里按 `content_error` 把确定性失败直接标记成预算已耗尽。

Revision ID: 0005_content_attempts
Revises: 0004_content_error
Create Date: 2026-11-19 10:50:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_content_attempts"
down_revision: str | None = "0004_content_error"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 content_service.MAX_FETCH_ATTEMPTS 保持一致
MAX_FETCH_ATTEMPTS = 3


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("content_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # 存量确定性失败：4xx（429 除外，它是限流不是拒绝）和「非 HTML 内容」
    op.execute(
        sa.text(
            """
            UPDATE documents
               SET content_attempts = :max_attempts
             WHERE content_status = 'failed'
               AND (
                    (content_error LIKE 'HTTP 4%' AND content_error NOT LIKE 'HTTP 429%')
                 OR content_error LIKE '非 HTML 内容%'
               )
            """
        ).bindparams(max_attempts=MAX_FETCH_ATTEMPTS)
    )


def downgrade() -> None:
    op.drop_column("documents", "content_attempts")

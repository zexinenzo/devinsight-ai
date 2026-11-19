"""阶段 3 补丁：把源的落库授权级别从注释变成字段。

在此之前，`sources` 表只有 `rights_note`（一句给人看的说明），没有机器可读的
授权级别。`SourceSpec.rights_mode` 虽然有默认值 `metadata_excerpt`，但
`from_source()` 从来没有把库里的值传进去——而库里本来也没有这一列可传。

结果是：**「全文不入库」这件事一直是靠注释和自觉维持的，不是靠代码。**
36氪、InfoQ、OpenAI 全都落在默认的 `metadata_excerpt` 上，看起来「合规」，
其实只是因为没有任何源能表达另一种可能。等接入 CC 授权的源（阮一峰博客，
页面明示 CC BY-NC-ND 3.0）时，这条链路必须打通，否则同一个 bug 会以
「明明授权了却只存 600 字」的形式再出现一次。

默认值仍取 `metadata_excerpt`：新增列不应改变任何存量源的行为。

Revision ID: 0006_source_rights_mode
Revises: 0005_content_attempts
Create Date: 2026-11-19 11:05:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_source_rights_mode"
down_revision: str | None = "0005_content_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "rights_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'metadata_excerpt'"),
        ),
    )

    # 存量源里唯一拿到明确开放授权的是阮一峰博客。它的源可能还没 seed 进来，
    # 这条 UPDATE 命中 0 行也无害——seed 时会带着 rights_mode 一起写入。
    op.execute(
        sa.text(
            """
            UPDATE sources
               SET rights_mode = 'cc_by_nc_nd'
             WHERE source_key = 'ruanyifeng_zh'
            """
        )
    )


def downgrade() -> None:
    op.drop_column("sources", "rights_mode")

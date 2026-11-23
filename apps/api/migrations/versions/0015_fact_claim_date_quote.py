"""0015_fact_claim_date_quote: fact_claims 增加 date_quote

`valid_from_source='stated'` 原本只是一句自述：模型说这一天，没有任何东西能验证。
加一列存「支撑这个日期的原文片段」，事后可以拿它回原文比对——「文中写了这一天」
和「模型抄了发布日期」从此在库里长得不一样。

只加列，不加约束：历史行没有这个字段，补不出来；空值表达的是「当时没记」，
不是「不存在」，用约束逼成非空只会逼出一批编造的引文。

Revision ID: 0015_fact_claim_date_quote
Revises: 0014_fact_claim_object_type
Create Date: 2026-11-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_fact_claim_date_quote"
down_revision = "0014_fact_claim_object_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fact_claims", sa.Column("date_quote", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("fact_claims", "date_quote")

"""0014_fact_claim_object_type: fact_claims 增加 object_type 与两条 NOT VALID 约束

阶段 9 第二步。第一步只在抽取入口校验；这两条约束拦的是「不经过抽取脚本的写入」。

object_type 落库不是为了查询方便：形状校验按它选规则，不落库的话，事后审计
只能用当前版本的 SPECS 反推，而 SPECS 会改——历史行当初按什么判的就丢了。

回填映射与 predicate_specs.SPECS 是同一份事实的两处表达（迁移必须自包含），
一致性由 test_fact_schema_guard.py 逐谓词断言。

Revision ID: 0014_fact_claim_object_type
Revises: 0013_fact_claim_valid_to_reason
Create Date: 2026-11-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_fact_claim_object_type"
down_revision = "0013_fact_claim_valid_to_reason"
branch_labels = None
depends_on = None

BACKFILL = """
UPDATE fact_claims
SET object_type = CASE predicate
        WHEN 'released' THEN 'product'
        WHEN 'released_version' THEN 'version'
        WHEN 'announced' THEN 'product'
        WHEN 'discontinued' THEN 'product'
        WHEN 'acquired' THEN 'org'
        WHEN 'partnered_with' THEN 'org'
        WHEN 'ceo' THEN 'person'
        WHEN 'founded' THEN 'year_or_org'
        WHEN 'funded' THEN 'amount'
        WHEN 'layoff' THEN 'scale'
        WHEN 'open_sourced' THEN 'product'
        WHEN 'benchmark' THEN 'benchmark'
    END
WHERE object_type IS NULL
"""

# NOT VALID 是刻意的：存量里可能已有词表外的谓词，加 VALID 会让整个迁移失败。
# 它表达的是「新规则从今天生效，存量另行清理」，且 convalidated=false 让这件事可见：
#   select conname, convalidated from pg_constraint
#   where conrelid = 'fact_claims'::regclass;
ADD_PREDICATE_CHECK = """
ALTER TABLE fact_claims
ADD CONSTRAINT ck_fact_claims_predicate_closed_set
CHECK (predicate IN ('released', 'released_version', 'announced', 'discontinued', 'acquired', 'partnered_with', 'ceo', 'founded', 'funded', 'layoff', 'open_sourced', 'benchmark')) NOT VALID
"""

ADD_OBJECT_TYPE_CHECK = """
ALTER TABLE fact_claims
ADD CONSTRAINT ck_fact_claims_object_type
CHECK (object_type IS NULL OR object_type IN ('product', 'version', 'org', 'person', 'year_or_org', 'amount', 'scale', 'benchmark')) NOT VALID
"""


def upgrade() -> None:
    op.add_column("fact_claims", sa.Column("object_type", sa.Text(), nullable=True))
    op.execute(BACKFILL)
    op.execute(ADD_PREDICATE_CHECK)
    op.execute(ADD_OBJECT_TYPE_CHECK)


def downgrade() -> None:
    op.execute("ALTER TABLE fact_claims DROP CONSTRAINT IF EXISTS ck_fact_claims_object_type")
    op.execute("ALTER TABLE fact_claims DROP CONSTRAINT IF EXISTS ck_fact_claims_predicate_closed_set")
    op.drop_column("fact_claims", "object_type")

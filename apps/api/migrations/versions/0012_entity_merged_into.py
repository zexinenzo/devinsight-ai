"""阶段 8：实体分裂用指针合并，不改事实的外键。

`normalize_key("NVIDIA") == "nvidia"`，`normalize_key("英伟达") == "英伟达"`——不做跨语言
对齐，于是唯一约束 `(kind, canonical_key)` 必然把它们放成两行。这不是 bug，是那个简化
在数据上的直接投影。

`entities.aliases` 这一列早就存在、`resolve_entity()` 也在读，但它解不了这个问题，原因
有两层，都很硬：

1. `resolve_entity()` 返回**单个**实体，`entity_timeline()` 又只查
   `FactClaim.subject_id == entity.id`。别名让「用中文名能查到英文那一行」，但查不到
   挂在另一个 id 上的事实。
2. 事实是抽取时按当时的写法分头落库的：`NVIDIA` 的发布记录挂在 A 行，`英伟达` 的合作
   记录挂在 B 行。别名写在 A 上，B 的那些记录依然不会出现在结果里。

**别名解决的是「用哪个名字能命中」，不是「命中之后拿到谁的事实」。**

所以加一列指针，把「这些行说的是同一个东西」这件事单独记下来：

    ALTER TABLE entities ADD COLUMN merged_into uuid REFERENCES entities(id)

查询侧从「一个实体」变成「一组 id」：沿 `merged_into` 向上找到根，再向下收集所有指向
该根的行，`subject_id IN (...)`。

**不用 `UPDATE fact_claims SET subject_id` 的原因**有两个，任何一个都够：
触发器 `trg_fact_claims_no_rewrite` 直接拒改 `subject_id`；而且改完就再也查不到
「这条事实当初挂在 `英伟达` 这个写法下」——合并变成不可逆，判错一次就永久损失信息。
指针并是**可逆**的：把 `merged_into` 置回 NULL 就还原，事实一行不动。

ON DELETE SET NULL：根被删掉时，子行退回独立实体的状态，而不是跟着消失或留下悬挂外键。
它们的事实一直都在，凭这一点就不该连坐。

Revision ID: 0012_entity_merged_into
Revises: 0011_document_facts_extracted_at
Create Date: 2026-11-21 21:20:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_entity_merged_into"
down_revision: str | None = "0011_document_facts_extracted_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE entities ADD COLUMN merged_into uuid "
        "REFERENCES entities(id) ON DELETE SET NULL"
    )

    # 自指在 SQL 里能拦，多跳环路不能：CHECK 看不到别的行。
    # A→B、B→A 这种只能靠查询层的深度上限 + 访问集合兜住（见 timeline_service）。
    # 名字用全名而不是后缀：这里是裸 SQL，命名约定（ck_%(table_name)s_%(constraint_name)s）
    # 不会替我补前缀，而元数据里实际生成的就是这个全名。
    op.execute(
        "ALTER TABLE entities ADD CONSTRAINT ck_entities_not_self_merged "
        "CHECK (merged_into IS NULL OR merged_into <> id)"
    )

    # 查询是「找所有指向根的行」，条件落在这一列上。实体在千级时顺序扫描也无所谓，
    # 但这是唯一一个会被反复按值查的列，加上不亏。
    op.execute("CREATE INDEX ix_entities_merged_into ON entities (merged_into)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_entities_merged_into")
    op.execute("ALTER TABLE entities DROP CONSTRAINT IF EXISTS ck_entities_not_self_merged")
    op.execute("ALTER TABLE entities DROP COLUMN IF EXISTS merged_into")

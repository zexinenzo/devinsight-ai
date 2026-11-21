"""阶段 8：闭合区间时记一条理由（`valid_to_reason`）。

`valid_to` 从建表起就存在，触发器也一直允许改它，但**没有任何写入方**——
抽取 pipeline 只产出 `valid_from`。于是库里 270 条事实全是「至今成立」，
`英伟达` 名下两个 CEO 同时在职，`as_of("2026-11-21")` 只能两个都返回。

这条记录的处置很特殊：撤回是错的。撤回的意思是「这条我们不再声称」，
而前任 CEO 曾经在任是事实；撤掉它，`as_of("2026-03-01")` 会回答「当时不是他」。
正确做法是给它的 `valid_to` 填一个结束日——**这是双时间表的右半边第一次被真正用上**。

问题在于：一旦有人能徒手改 `valid_to`，它就和当初没人写的 `aliases` 是同一类东西，
只是方向相反——那个是「有列没人写」，这个会变成「有列随便写」。而日期比别名更难查证：
填错一天不会报错，只会在某个历史时点给出错误答案，且**没有人会去看那一天**。

所以加一列理由，再加一条 CHECK：`valid_to` 非空就必须带理由。理由不用自由发挥，
`repair_facts.py supersede` 在给 `--by` 时会自动写上「被哪条记录取代」——指针本身
就是最硬的理由，文字只是给它一个人能读的样子。

**为什么不复用 `retracted_reason`**：那属于认识轴（我们什么时候不再声称），
这一列属于世界轴（这件事什么时候不再成立）。混用一列，等于把
「记错了」和「世界变了」压成同一个状态，`as_of` 又会开始撒谎。

Revision ID: 0013_fact_claim_valid_to_reason
Revises: 0012_entity_merged_into
Create Date: 2026-11-21 21:55:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_fact_claim_valid_to_reason"
down_revision: str | None = "0012_entity_merged_into"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE fact_claims ADD COLUMN valid_to_reason text")

    # 既有 270 行的 valid_to 全是 NULL，这条约束加得上，也不会要求回填历史。
    # 它拦的不是「日期对不对」，而是「这一行有没有人能追问的依据」——
    # 一条没有理由的闭合日，和当初模型编出来的事实一样不可审计，
    # 区别只是它更可信（因为它长得像个日期）。
    op.execute(
        """
        ALTER TABLE fact_claims
            ADD CONSTRAINT ck_fact_claims_closure
            CHECK (valid_to IS NULL OR valid_to_reason IS NOT NULL)
        """
    )


def downgrade() -> None:
    # CHECK 引用 valid_to_reason，必须先于列本身删掉；反过来的顺序会直接报
    # 「cannot drop column ... other objects depend on it」
    op.execute("ALTER TABLE fact_claims DROP CONSTRAINT IF EXISTS ck_fact_claims_closure")
    op.execute("ALTER TABLE fact_claims DROP COLUMN IF EXISTS valid_to_reason")

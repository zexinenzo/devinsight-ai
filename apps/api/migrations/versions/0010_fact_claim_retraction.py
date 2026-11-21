"""阶段 8：事实撤回（认识轴）。

阶段 7 建表时把「只 INSERT，不 UPDATE 值」做成了触发器，内容是冻住的。那条纪律是对的，
但它没回答一个问题：**如果这条记录本身就是错的怎么办**——模型把 CFO 写成了 CEO，
把许可证写成了被开源的产物（`Fardesk --open_sourced--> Apache-2.0`）。

第一反应是 DELETE。这里不做，理由不是「删不得」，是「判错之后无法挽回」：

- 判错一条就永久少一条；而这类记录的判定本身不可靠（238 条里能自动判的只有 2 条），
  判错的概率乘以批量，就是几十条真实信息被抹掉，且**抹掉之后没人知道少了什么**；
- 证据链跟着行一起消失，事后无法复盘「当初为什么这么写」；
- 时间线少一条的表现是「这件事没发生」，而不是「这里有个洞」。

所以加三列，不加删除语句：

- `retracted_at`：我们**不再声称**这条记录的时间。这是**认识轴**。
- `retracted_reason`：为什么撤。没有理由的撤回，是一次不可审计的判断。
- `retracted_replaced_by`：撤回后被哪条替代。只在能确定对应关系时填
  （同一 `document_id` + 同一 `subject_id` + 同一 `predicate`），定不准就留空。

**为什么不能复用 `valid_to`**：那是「这件事在现实世界里什么时候不再成立」，是世界轴。
撤回一条误抽的 CEO 时写 `valid_to`，会让 `as_of(那天)` 回答「当时不是他」——
错的是我们的记录，不是那天的世界。两个轴混用，`as_of` 会系统性地撒谎，
而它正是双时间表唯一存在的理由。

**为什么叫 `retracted_replaced_by` 而不叫 `replaced_by`**：表里已经有一个
`superseded_by`（世界轴：新事实取代旧事实，因为世界变了）。两个名字长得像、
语义相反的列放在一起，迟早有人填错。加 `retracted_` 前缀让它和另外两列排在一起。

这三列**不在触发器的禁止列表里**，所以不用改触发器：撤回改的是「我们信不信」，
而不是「原文说了什么」。触发器现在允许的 UPDATE 变成四项：闭合 `valid_to`、
指向 `superseded_by`、撤回（填这三列）、以及人工校准 `confidence` / `evidence_text`。

Revision ID: 0010_fact_claim_retraction
Revises: 0009_entities_fact_claims
Create Date: 2026-11-21 18:30:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_fact_claim_retraction"
down_revision: str | None = "0009_entities_fact_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE fact_claims
            ADD COLUMN retracted_at         timestamptz,
            ADD COLUMN retracted_reason     text,
            ADD COLUMN retracted_replaced_by uuid
                REFERENCES fact_claims(id) ON DELETE SET NULL
        """
    )

    # 撤回必须给出理由。这条 CHECK 不是为了「数据完整」，是为了让每一行撤回都能被追问：
    # 为什么撤、依据什么。没有它，`retracted_at` 会被当成一个布尔位随手置 true。
    op.execute(
        """
        ALTER TABLE fact_claims
            ADD CONSTRAINT ck_fact_claims_retraction
            CHECK (retracted_at IS NULL OR retracted_reason IS NOT NULL)
        """
    )

    # 刻意不加 partial index（WHERE retracted_at IS NULL）：现有 148 行里只有个位数会被撤回，
    # 这个索引等于把 95% 的行再抄一遍，planner 不会用。等表到十万行、撤回比例上来再加，
    # 那时只需加索引，查询语句不用动。


def downgrade() -> None:
    # 列被删掉时 FK 跟着走，CHECK 要先单独删（它引用 retracted_reason）
    op.execute("ALTER TABLE fact_claims DROP CONSTRAINT IF EXISTS ck_fact_claims_retraction")
    op.execute(
        """
        ALTER TABLE fact_claims
            DROP COLUMN IF EXISTS retracted_replaced_by,
            DROP COLUMN IF EXISTS retracted_reason,
            DROP COLUMN IF EXISTS retracted_at
        """
    )

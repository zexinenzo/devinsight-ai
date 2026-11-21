"""阶段 8：给文档记一个「抽取已判定」的时间戳。

`pick_documents` 在此之前只有两个条件：有正文/摘要、按 `published_at` 倒序取最新 N 篇。
它不记录「这篇抽过没有」，所以每轮都把同一批文档原样再送进模型一次。上一轮
`--limit 500 --apply` 的输出因此出现 **抽出 238 条、重复 90 条**——那 90 条不是模型
稳定，是同一批文档被重抽后撞上了 `uq_fact_claims_identity`。38% 的模型调用是白烧的。

比重复更贵的是那 342 篇「无事实」文档：它们一行事实都没写进库，于是
「用 `fact_claims` 有没有行来判断抽没抽过」这条路对它们永远返回 false，
每轮都要重新判定一次。**最需要被记住的那一类，恰好是唯一没有痕迹的一类。**

所以这个状态位必须独立存在：

- `NULL`：还没被判定过（包括上次抽取失败——截断/空响应/格式错都算没判定，
  回填了它们就再也不会重试，一篇被截断的文档会被永久跳过）；
- 有值：模型已经给出确定回答，抽到事实或明确「本文没有可抽的事实」都算。

回填是**部分**的，这一点刻意不装作完整：已经产过事实的文档能用
`max(fact_claims.recorded_at)` 反推出来，历史上那批「无事实」文档没有任何痕迹可反推，
只能保持 NULL、在下一轮被再判定一次，之后就被记住了。一次性成本，换后面的干净。

Revision ID: 0011_document_facts_extracted_at
Revises: 0010_fact_claim_retraction
Create Date: 2026-11-21 19:45:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_document_facts_extracted_at"
down_revision: str | None = "0010_fact_claim_retraction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN facts_extracted_at timestamptz")

    # 能从事实行反推的那部分先填上，省掉一次无意义的重复抽取。
    # 用 recorded_at 的 max，而不是 min：一篇文档被抽过多轮时，最后一次判定才是
    # 「它的结论已经定了」的时刻。
    op.execute(
        """
        UPDATE documents d
           SET facts_extracted_at = f.last_at
          FROM (
                SELECT document_id, max(recorded_at) AS last_at
                  FROM fact_claims
                 GROUP BY document_id
               ) f
         WHERE f.document_id = d.id
        """
    )

    # 不加部分索引（WHERE facts_extracted_at IS NULL）：852 行全是顺序扫描的规模，
    # planner 不会用。真到十万行，加索引即可，`pick_documents` 的语句不用改。


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS facts_extracted_at")

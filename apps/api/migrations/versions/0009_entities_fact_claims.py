"""阶段 7：实体与事实声明（双时间表）。

这张表存在的理由是**检索回答不了时间问题**。问「这家公司这半年在产品线上做了什么
变化」，向量检索能给出一批「提到了这家公司」的文档，但给不出「谁在什么时候发布了
什么」——因为它拿到的是一条条独立文档，没有跨文档的主语。

两条时间不能合成一条，这是整个设计的地基：

- `valid_from` / `valid_to`：这条事实在**现实世界**里成立的区间
- `recorded_at`：这条记录被**系统写入**的时间

报错场景是这样的：2024-03 有报道说「OpenAI 的 CEO 是 Sam Altman」，2025-11 有报道
说他被罢免。如果表里只有一列 `object_text` 并且用 UPDATE 覆盖，那么：
1. 再也回答不了「2024 年 3 月 CEO 是谁」——最终状态把当时状态吃掉了；
2. 也解释不了「当时那条记录为什么说 A」——证据链跟着被改写。

所以规则是：**只 INSERT，不 UPDATE 值**。状态变了就插新行，把旧行的 `valid_to`
闭合。`valid_to IS NULL` 表示「至今仍成立」。

这条纪律不能只写在文档里。下面那个触发器让数据库直接拒绝改写
`object_text` / `predicate` / `valid_from` / `subject_id`——允许的 UPDATE 只有
两种：闭合 `valid_to`、指向 `superseded_by`。把「历史事实不能覆盖」从约定变成
数据库约束，是因为约定会被下一个改代码的人忘掉，而约束不会。

`valid_from_source` 是后来补的一列，补的原因很实在：抽取出来的日期可信度不一样。
「2025-11-18 发布了 GPT-5.1」是文中明说的（`stated`）；「某篇文章提到了某产品」只能
拿发布时间兜底（`published`）；两种情况必须能区分，否则时间线上「11 月 18 日」这个
日期看起来一样硬，实际一个是事实、一个是猜测。

关于谓词（`predicate`）：应用层维持一份受控词表，数据库只校验非空与来源枚举。
不给谓词加 CHECK 是刻意的——词表会演进，而改 CHECK 要写新迁移；更重要的是，
一个不在词表里的谓词造成的后果是「这条查不出来」，不是「数据损坏」，用不着数据库
层面拦。

Revision ID: 0009_entities_fact_claims
Revises: 0008_document_chunks
Create Date: 2026-11-20 12:30:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_entities_fact_claims"
down_revision: str | None = "0008_document_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 实体：一个规范名一行。`canonical_key` 是归一化后的比对键（小写、去标点空白），
    # 靠它把「OpenAI」「openai」「Open AI」收敛到同一行。
    # `kind` 参与唯一约束是因为「Apple」作为公司和作为水果是两件事，
    # 只按名字去重会把它们合并。
    op.execute(
        """
        CREATE TABLE entities (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name          text NOT NULL,
            kind          text NOT NULL,
            canonical_key text NOT NULL,
            aliases       jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_entities_kind_canonical UNIQUE (kind, canonical_key)
        )
        """
    )
    op.execute("CREATE INDEX ix_entities_canonical_key ON entities (canonical_key)")

    op.execute(
        """
        CREATE TABLE fact_claims (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            subject_id        uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            predicate         text NOT NULL,
            object_text       text NOT NULL,
            object_entity_id  uuid REFERENCES entities(id) ON DELETE SET NULL,
            valid_from        date,
            valid_to          date,
            valid_from_source text NOT NULL DEFAULT 'unknown',
            recorded_at       timestamptz NOT NULL DEFAULT now(),
            document_id       uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            evidence_text     text,
            confidence        real,
            superseded_by     uuid REFERENCES fact_claims(id) ON DELETE SET NULL,
            -- 同一篇文档对同一 (主语, 谓词, 宾语) 只产生一条：抽取脚本可以放心重跑，
            -- 不用先查后插（那个窗口在并发下本来也不可靠）
            CONSTRAINT uq_fact_claims_identity
                UNIQUE (subject_id, predicate, object_text, document_id),
            -- 区间必须非退化的。允许两端为 NULL（时间未知），但不允许 valid_to <= valid_from
            CONSTRAINT ck_fact_claims_period
                CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from),
            CONSTRAINT ck_fact_claims_from_source
                CHECK (valid_from_source IN ('stated', 'published', 'unknown'))
        )
        """
    )

    # 主查询形状是「某实体的时间线，按时间倒序」：联合索引让 ORDER BY valid_from DESC
    # 直接走索引，不必再排序。`NULLS LAST` 不写进索引，交给查询语句显式声明，
    # 因为「时间未知」的排序位置是展示决策，不是数据属性。
    op.execute(
        """
        CREATE INDEX ix_fact_claims_subject_valid
            ON fact_claims (subject_id, valid_from DESC)
        """
    )
    op.execute("CREATE INDEX ix_fact_claims_predicate ON fact_claims (predicate)")
    op.execute("CREATE INDEX ix_fact_claims_document ON fact_claims (document_id)")

    # 禁止改写：把「历史事实不能覆盖」做成数据库约束。
    # 用 IS DISTINCT FROM 而不是 <>，因为两列都可能是 NULL，NULL <> NULL 结果是 NULL，
    # 在 IF 里会被当成 false，那么「把 valid_from 从 2025-01-01 改成 NULL」就能溜过去。
    #
    # `recorded_at` 也在禁止列表里：它是「系统什么时候知道这件事」的审计标记，
    # 改它等于伪造记录。允许改的只有 valid_to（闭合区间）、superseded_by（指向新行）、
    # confidence 和 evidence_text（人工校准）。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fact_claims_reject_rewrite() RETURNS trigger AS $$
        BEGIN
            IF NEW.object_text  IS DISTINCT FROM OLD.object_text
            OR NEW.predicate    IS DISTINCT FROM OLD.predicate
            OR NEW.valid_from   IS DISTINCT FROM OLD.valid_from
            OR NEW.subject_id   IS DISTINCT FROM OLD.subject_id
            OR NEW.document_id  IS DISTINCT FROM OLD.document_id
            OR NEW.recorded_at  IS DISTINCT FROM OLD.recorded_at
            THEN
                RAISE EXCEPTION
                    'fact_claims 不可改写（id=%）：请插入新行并闭合旧行的 valid_to', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fact_claims_no_rewrite
            BEFORE UPDATE ON fact_claims
            FOR EACH ROW EXECUTE FUNCTION fact_claims_reject_rewrite()
        """
    )


def downgrade() -> None:
    # 触发器随表一起消失，函数要单独删
    op.execute("DROP TRIGGER IF EXISTS trg_fact_claims_no_rewrite ON fact_claims")
    op.execute("DROP FUNCTION IF EXISTS fact_claims_reject_rewrite()")
    op.execute("DROP TABLE IF EXISTS fact_claims")
    op.execute("DROP TABLE IF EXISTS entities")

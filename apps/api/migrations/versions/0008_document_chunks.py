"""阶段 4 基础设施：正文块的向量索引（只存向量，不存文本）。

这张表的每一列都值得说明，因为它承载的是「授权」与「召回」的折中：

- `char_start` / `char_end` 是块在**原文**里的字符区间。有了它，检索命中之后
  可以告诉用户「相关内容在全文第 1200 字附近」，而不必把那段文字存下来。
- `embedding` 是 `vector(384)`，维度和 `intfloat/multilingual-e5-small` 的输出一致。
  维度写死在迁移里、不跟随运行时配置：向量维度和列类型绑死，改模型必须新写一条
  迁移重算，不能让「改个环境变量就让 ORM 和表结构对不上」这种事发生。
- `model` 逐行记录。换模型等于换向量空间，两套向量混在一张表里算余弦没有意义，
  查询按 model 过滤，混进来能被 `group by model` 查出来。
- **没有 text 列，这是刻意的。** 正文在抓取时切成块、算完向量就丢弃；
  600 字以上的原文一个字都不落库，`metadata_excerpt` 的授权边界因此没有被绕开。

索引用 HNSW（`vector_cosine_ops`）：算子必须和查询里的 `<=>` 一致，
不一致时 Postgres 不会报错，只是索引静默不生效——这类坑只能靠写对来避免。

Revision ID: 0008_document_chunks
Revises: 0007_document_url_unique
Create Date: 2026-11-19 12:40:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_document_chunks"
down_revision: str | None = "0007_document_url_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 镜像本身带 pgvector，但显式建扩展能保证在裸装 Postgres 上也能跑通迁移
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE document_chunks (
            id          bigserial PRIMARY KEY,
            document_id uuid    NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index integer NOT NULL,
            char_start  integer NOT NULL,
            char_end    integer NOT NULL,
            embedding   vector(384) NOT NULL,
            model       text    NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_document_chunks_doc_model_index
                UNIQUE (document_id, model, chunk_index)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_document_chunks_embedding
            ON document_chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS document_chunks")

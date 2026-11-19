"""阶段 3 补丁：同一来源的同一篇文章只能有一行（scheme 不参与身份判定）。

之前判重只认 `(source_id, external_id)`。这在「一个源只有一条写入路径」时够用，
一旦同一来源有两条写入路径就会漏：

- RSS 路径的 external_id 是 Atom 的 id（`tag:www.ruanyifeng.com,2026:/blog//1.2563`）；
- 历史回填路径的 external_id 只有 URL 可用（归档页不给 Atom id）。

更隐蔽的是 canonical_url 也对不上：**feed 里给的是 `http://`，归档页给的是
`https://`**，而 `canonicalize_url` 保留 scheme（它必须保留，抓正文时要有 scheme）。
于是同一篇文章在库里有两行、两个 external_id、两个 canonical_url，
按任何一列看都「不重复」。

实测：回填 390 篇之后跑一次 `make sync`，最近 3 期各插了一条重复。

修法是让「同一来源的同一篇文章只能有一行」成为数据库约束，并且明确
**scheme 不构成文章身份**——`http://a/x` 与 `https://a/x` 是同一篇。
所以唯一索引建在去掉 scheme 的表达式上，而不是建在 canonical_url 原值上。
`ingestion_service` 的 `on_conflict_do_nothing` 同时去掉冲突目标，
任何唯一约束被撞都跳过，不再只盯着 external_id 一个键。

建索引前先确认存量没有冲突；有的话迁移会直接失败，不会静默丢数据。

Revision ID: 0007_document_url_unique
Revises: 0006_source_rights_mode
Create Date: 2026-11-19 11:45:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_document_url_unique"
down_revision: str | None = "0006_source_rights_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX uq_documents_source_canonical_url
            ON documents (source_id, regexp_replace(canonical_url, '^https?://', ''))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_documents_source_canonical_url")

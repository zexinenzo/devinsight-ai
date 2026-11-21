"""迁移 0010 与 ORM 的一致性。

不连数据库，只读迁移文件的 SQL 文本。理由很实际：`make test` 在没有数据库的机器上也能跑，
而「ORM 说约束叫 A、迁移里建的是 B」这类错只有真实执行 `alembic upgrade head` 时才会炸，
那时报错离原因很远，autogenerate 还会试图重建同一个约束。

写这个文件时它立刻抓到一个已经存在的错：`CheckConstraint(name="ck_fact_claims_period")`
在命名约定 `ck_%(table_name)s_%(constraint_name)s`（见 app/db/base.py）下会被补成
`ck_fact_claims_ck_fact_claims_period`，与 0009 在库里建出来的名字不一致。
已改成只写后缀（`period` / `from_source` / `retraction`）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import CheckConstraint

from app.db.models import FactClaim

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
MIGRATION = VERSIONS / "0010_fact_claim_retraction.py"


def _norm(text: str) -> str:
    """压掉换行与连续空格，这样迁移里折行的 SQL 也能直接比对。"""
    return " ".join(text.split())


def migration_sql(path: Path = MIGRATION) -> str:
    return _norm(path.read_text(encoding="utf-8"))


def test_retraction_columns_exist_in_orm_and_migration() -> None:
    sql = migration_sql()
    for column in ("retracted_at", "retracted_reason", "retracted_replaced_by"):
        assert column in FactClaim.__table__.columns, f"ORM 缺列 {column}"
        assert column in sql, f"迁移 0010 缺列 {column}"


def test_retraction_check_constraint_matches_migration() -> None:
    """CHECK 的文本必须两边一字不差——差一个词就是两条不同的约束。"""
    found = [
        _norm(str(c.sqltext))
        for c in FactClaim.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_fact_claims_retraction"
    ]
    assert found, "ORM 里没有 ck_fact_claims_retraction"
    assert found[0] in migration_sql()


def test_every_check_constraint_name_exists_in_some_migration() -> None:
    """元数据里的约束名，必须在某个迁移里被真的建出来过。

    这条比逐个断言更值得留：命名约定会自动加前缀，手写全名时就会静默叠成
    `ck_x_ck_x_y`，而 ORM 侧完全不会报错。
    """
    all_sql = _norm(
        "\n".join(p.read_text(encoding="utf-8") for p in sorted(VERSIONS.glob("[0-9]*.py")))
    )
    for constraint in FactClaim.__table__.constraints:
        if isinstance(constraint, CheckConstraint):
            assert constraint.name in all_sql, f"{constraint.name} 没有对应迁移"


def test_retraction_columns_are_nullable() -> None:
    """三列默认都是 NULL：既有 148 行落库时还没有「撤回」这个概念，它们全部是「采信中」。"""
    for column in ("retracted_at", "retracted_reason", "retracted_replaced_by"):
        assert FactClaim.__table__.columns[column].nullable is True


def test_replaced_by_points_at_fact_claims() -> None:
    """替代关系指向自己这张表。写成 entities.id 不会报错，只会查出一堆空。"""
    column = FactClaim.__table__.columns["retracted_replaced_by"]
    assert {fk.target_fullname for fk in column.foreign_keys} == {"fact_claims.id"}


def test_superseded_by_and_retracted_replaced_by_are_different_columns() -> None:
    """两个指针一个是世界轴、一个是认识轴。

    这条断言看着废话，但它钉住的是一个真实的坑：`superseded_by` 已经存在，
    新增的列如果叫 `replaced_by`，两列在 `\\d` 输出里长得像一对同义词，
    迟早有人把「世界变了」写成「我们撤了」。
    """
    columns = set(FactClaim.__table__.columns.keys())
    assert "superseded_by" in columns
    assert "retracted_replaced_by" in columns
    assert "replaced_by" not in columns

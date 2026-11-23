"""库层防护：谓词闭集、宾语类型落库、迁移与规格表不漂移。

第一步只防住「从抽取脚本写入」这一条路。这三条断言守的是另外两个问题：
别人绕过脚本直接写库，以及迁移里硬编码的映射与 SPECS 悄悄漂开。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.db.models import FactClaim
from app.services.predicate_specs import SPECS

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _migration_text() -> str:
    """按内容找那支加了 object_type 的迁移，不按文件名找。

    文件名带版本前缀，而版本号会随分支变化；断言文件名等于把测试钉在某个编号上。
    """

    for path in sorted(MIGRATIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "object_type" in text and "fact_claims" in text:
            return " ".join(text.split())
    raise AssertionError("没找到涉及 fact_claims.object_type 的迁移")


def test_model_has_object_type_column() -> None:
    """类型必须落库。

    不落库的后果不是「少一列」，而是「审计时只能用当前版本的 SPECS 反推」——
    SPECS 一旦改动，历史行当初按什么规则判的就永远说不清了。
    """

    column = FactClaim.__table__.columns["object_type"]

    assert column.nullable is True
    assert column.type.__class__.__name__ == "Text"


def test_model_declares_both_checks() -> None:
    """约束名要断言全名。

    `__table_args__` 里写的是后缀（`name="object_type"`），命名约定补上
    `ck_fact_claims_` 前缀——断言后缀会在 metadatas 上找不到东西。
    """

    names = {c.name for c in FactClaim.__table__.constraints if c.__class__.__name__ == "CheckConstraint"}

    assert "ck_fact_claims_predicate_closed_set" in names
    assert "ck_fact_claims_object_type" in names


def test_migration_uses_not_valid() -> None:
    """两条约束都必须是 NOT VALID。

    加 VALID 的话，存量里任何一条词表外的谓词都会让迁移失败；而 NOT VALID 的表达
    正好是「新规则从今天生效，存量另行清理」，并且 convalidated=false 让这件事可见。
    """

    sql = _migration_text()

    # 数 `) NOT VALID` 而不是 `NOT VALID`：文档字符串和注释里也提到了这个词，
    # 数裸词得到 4，测试会以为多加了约束。
    assert sql.count(") NOT VALID") == 2
    # 迁移里是 ORM 调用而不是裸 DDL，所以断言 `add_column` 而不是 `ADD COLUMN`
    assert 'op.add_column("fact_claims"' in sql
    assert 'Column("object_type"' in sql


def test_backfill_map_matches_specs() -> None:
    """迁移里硬编码的「谓词 → 类型」必须与 SPECS 逐条一致。

    迁移不能 import 应用代码（历史迁移要自包含），所以映射必然存在两份。
    两份本身不是问题，**漂开而没人知道**才是问题——所以这条断言逐个谓词比对。
    """

    sql = _migration_text()

    for spec in SPECS:
        assert f"WHEN '{spec.key}' THEN '{spec.object_type}'" in sql, (
            f"迁移的回填映射缺 {spec.key} → {spec.object_type}"
        )


def test_no_orphan_predicate_in_migration() -> None:
    """迁移里出现的谓词不能比 SPECS 多。

    多出来的那条是一个已经不存在的谓词还在被回填——同样的漂移，反方向。
    """

    sql = _migration_text()
    keys = set(re.findall(r"WHEN '([a-z_]+)' THEN", sql))

    assert keys == {spec.key for spec in SPECS}


def test_revision_id_fits_alembic_version() -> None:
    """revision id 必须 ≤32 字符——`alembic_version.version_num` 是 varchar(32)。

    超长不会在 DDL 阶段报错：加列、回填、两条约束全都执行成功，直到最后
    `UPDATE alembic_version SET version_num=...` 才抛 StringDataRightTruncation，
    事务整体回滚。表现为「版本号没前进 + 列也没加上」，看着像两个互不相关的故障。
    所以这条断言放在文件名和语法之外，专门守长度。
    """

    for path in sorted(MIGRATIONS.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("revision"):
                rev = line.split("=", 1)[1].strip().strip('"').strip("'")
                assert len(rev) <= 32, (
                    f"{path.name}: revision id `{rev}` 长 {len(rev)}，超过 32"
                )
                break

"""阶段 8：实体分裂用 `merged_into` 指针合并（不连库）。

`normalize_key` 不做跨语言对齐，所以 `NVIDIA` 和 `英伟达` 必然是两行，两行各有各的
事实。别名救不了：别名解决「用哪个名字能命中」，不解决「命中之后拿到谁的事实」。

这里钉四类改坏了不会报错、只会静默出错的事：

1. **闭包算错**：链式（A→B→C）或环路（A↔B）下少收一行，时间线就少一半事实，
   而结果看起来完全正常。
2. **环路不兜**：A→B→A 会让「向上找根」变成死循环——不是返回错数据，是进程挂住。
3. **查询仍是单 id**：`subject_id == root.id` 能跑通、能返回结果，只是永远缺一半。
4. **约束名对不上**：迁移里写全名、ORM 里写后缀，元数据与真实库的约束名会分叉，
   下次 autogenerate 会试图重建一个已经存在的约束。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from app.db.models import Entity
from app.services import timeline_service
from app.services.timeline_service import build_entity_group

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0012_entity_merged_into.py"
)


def _entity(name: str, merged_into: uuid.UUID | None = None) -> SimpleNamespace:
    # build_entity_group 只读 id / name / merged_into，不需要真的 ORM 实例
    return SimpleNamespace(id=uuid.uuid4(), name=name, merged_into=merged_into)


def test_entity_without_pointer_is_a_group_of_one() -> None:
    nvidia = _entity("NVIDIA")
    group = build_entity_group(nvidia, [nvidia])

    assert group.root is nvidia
    assert group.ids == [nvidia.id]
    assert group.merged_names == []


def test_chain_collapses_to_the_top_and_collects_every_alias() -> None:
    """A→B→C：查其中任何一个，拿到的都必须是 C 名下的整组。

    指针允许链式，是因为合并是分批做的：先并 `英伟达`→`NVIDIA`，之后又发现
    `NVIDIA Corp` 也该并进去。要求「只能一层」会把这种事变成必须改历史数据。
    """
    top = _entity("NVIDIA")
    middle = _entity("NVIDIA Corp", merged_into=top.id)
    bottom = _entity("英伟达", merged_into=middle.id)
    rows = [top, middle, bottom]

    for start in rows:
        group = build_entity_group(start, rows)
        assert group.root is top
        assert set(group.ids) == {top.id, middle.id, bottom.id}

    # merged_names 不含根：根是 entity_name，再列一遍会让回复读成「NVIDIA、NVIDIA」
    assert build_entity_group(top, rows).merged_names == ["NVIDIA Corp", "英伟达"]


def test_cycle_does_not_hang_and_still_returns_a_group() -> None:
    """A↔B 是坏数据，不是致命错误。

    CHECK 只能拦自指（看不到别的行），环路只能在这里兜。选「停在当前行」而不是
    抛异常，是因为一个坏指针不该让整条时间线变成 500——但必须保证它**会停**。
    """
    left = _entity("A")
    right = _entity("B", merged_into=left.id)
    left.merged_into = right.id  # 造环

    group = build_entity_group(left, [left, right])
    assert group.root.id in {left.id, right.id}
    assert len(group.members) >= 1


def test_depth_limit_stops_a_long_chain() -> None:
    """超深链停在深度上限处，不会一直跟下去。

    正常合并最多两三层；超过上限说明指针坏了，此时「停在某处」比「跟到底」安全。
    """
    rows: list[SimpleNamespace] = [_entity("e0")]
    for index in range(1, timeline_service.MAX_MERGE_DEPTH + 4):
        rows.append(_entity(f"e{index}", merged_into=rows[-1].id))

    group = build_entity_group(rows[0], rows)
    assert group.root is not None
    # 没有走到链尾，说明上限生效了
    assert group.root.name != f"e{len(rows) - 1}"


def test_starting_point_missing_from_rows_still_works() -> None:
    """rows 里不含起点时不能崩——调用方可能只查了子行。"""
    loose = _entity("Solo")
    group = build_entity_group(loose, [])
    assert group.ids == [loose.id]


def test_merge_column_exists_on_both_orm_and_migration() -> None:
    column = Entity.__table__.c.merged_into
    assert column.nullable is True
    assert {fk.column.table.name for fk in column.foreign_keys} == {"entities"}

    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())
    assert "ADD COLUMN merged_into uuid" in sql
    assert "REFERENCES entities(id) ON DELETE SET NULL" in sql


def test_self_merge_is_rejected_by_a_named_check_constraint() -> None:
    """约束名必须两边一致。

    这里是 0009 那个双前缀 bug 的回归测试：迁移里写全名、ORM 里也写全名，
    元数据会生成 `ck_entities_ck_entities_not_self_merged`，与库里的真名对不上，
    而这件事只有在有人跑 autogenerate 时才会暴露。
    """
    names = {str(c.name) for c in Entity.__table__.constraints if c.name is not None}
    assert "ck_entities_not_self_merged" in names

    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())
    assert "ck_entities_not_self_merged" in sql
    assert "merged_into <> id" in sql


def test_deleting_the_root_keeps_the_child_rows() -> None:
    """ON DELETE SET NULL：根没了，子行退回独立实体，而不是被连坐。

    子行名下的事实一直都在，凭这一点就不该跟根一起消失。
    """
    fk = next(iter(Entity.__table__.c.merged_into.foreign_keys))
    assert fk.ondelete == "SET NULL"

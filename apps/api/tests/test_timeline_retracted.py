"""阶段 8：撤回在查询层生效的方式（不连库）。

`entity_timeline` / `as_of` 的 SQL 依赖 Postgres，项目没有测试库，所以这里
不去假装跑真库，只钉住三件**改坏了不会报错、只会静默变脏**的事：

1. 撤回的条目必须被过滤掉。漏了这条，一条判定不可靠的记录会继续参与排序、
   继续制造区间冲突——而界面上看不出任何异常。
2. 同一次查询里必须把**撤回条数**取出来。全过滤之后，「撤过一条」和
   「从来没记过」返回值完全一样，而这两件事对调用方含义相反。
3. 全部被撤回时，工具不能回答「这个实体没有任何事实记录」——那句话的意思是
   「还没抽取」，会把「我们撤了它」误读成「库里没有」。

判定方式是检查 SQLAlchemy 编译出的语句里有没有那个条件。字符串断言看着脆弱，
但它比 mock 返回值更接近真相：真正决定行为的是发出去的 SQL，不是 Python 分支。
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

from app.services import timeline_service
from app.services.agent_tools import _render_timeline
from app.services.timeline_service import TimelineResult


class _FakeResult:
    def __init__(self, rows: list | None = None, one: object | None = None) -> None:
        self._rows = rows or []
        self._one = one

    def all(self) -> list:
        return self._rows

    def scalar_one_or_none(self) -> object | None:
        return self._one


class _FakeSession:
    """记录收到的语句，不执行。第一次 execute 是 resolve_entity 的精确匹配。"""

    def __init__(self, entity: object, retracted: int = 0) -> None:
        self._entity = entity
        self._retracted = retracted
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if len(self.statements) == 1:
            return _FakeResult(one=self._entity)
        return _FakeResult(rows=[])

    async def scalar(self, stmt) -> int:
        self.statements.append(stmt)
        return self._retracted


def _entity() -> SimpleNamespace:
    # entity_timeline / as_of 只读 id / name / kind，不需要真的 ORM 实例
    return SimpleNamespace(id=uuid.uuid4(), name="OpenAI", kind="company", aliases=[])


async def test_timeline_query_filters_retracted():
    session = _FakeSession(_entity())
    await timeline_service.entity_timeline(session, "OpenAI")

    main = str(session.statements[1])
    assert "retracted_at IS NULL" in main


async def test_as_of_filters_retracted_in_both_queries():
    session = _FakeSession(_entity())
    await timeline_service.as_of(session, "OpenAI", date(2024, 3, 1))

    # 主查询（区间覆盖）与 undated 查询都必须带条件。
    # 只改一处的话，撤回的条目会从「有日期」这一侧消失，又从「时间未知」那一侧
    # 冒出来——过滤看起来生效了，实际上只是把问题挪到了另一个列表。
    for index in (1, 2):
        assert "retracted_at IS NULL" in str(session.statements[index])


async def test_retracted_count_is_surfaced():
    session = _FakeSession(_entity(), retracted=3)
    result = await timeline_service.entity_timeline(session, "OpenAI")

    assert result.retracted_count == 3
    # 计数与内容必须分开：撤回的条目不该混进 entries / undated
    assert result.entries == []
    assert result.undated == []


def test_render_mentions_retraction_only_when_present():
    base = dict(
        entity_id=uuid.uuid4(),
        entity_name="OpenAI",
        entity_kind="company",
    )

    quiet = _render_timeline("事实时间线", TimelineResult(**base))
    assert "撤回" not in quiet

    noisy = _render_timeline("事实时间线", TimelineResult(**base, retracted_count=2))
    # 数量要说出来，内容不能露出来：撤回的前提是判定不可靠，把内容再摆一遍
    # 等于请模型继续引用它
    assert "2 条事实曾被记录" in noisy
    assert "不应作为回答依据" in noisy

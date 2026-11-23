"""enrich 的时间窗口：默认不变，0 表示不限。

这条窗口的价值只在「还历史欠账」时才显出来。它默认写死 14 天，400 多篇待补正文里
只有 10 篇落进窗口——而日志打的是「候选=10」，看不出其余的都出局了。
"""

from __future__ import annotations

import asyncio

from app.services.content_service import select_candidates


class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _FakeSession:
    """只用来捕获编译后的语句：`select_candidates` 除 execute 外不碰 session。"""

    def __init__(self) -> None:
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult()


def _sql(**kwargs) -> str:
    session = _FakeSession()
    asyncio.run(select_candidates(session, **kwargs))
    return str(session.statements[-1])


def test_default_window_filters_by_fetched_at() -> None:
    """默认 14 天必须还在：它是「优先追新内容」的开关，不能被顺手删掉。"""

    assert "documents.fetched_at >=" in _sql()


def test_zero_window_removes_the_time_filter() -> None:
    """0 表示不限——这是补历史欠账的唯一入口。

    判据必须是「没有这个条件」，不是「窗口变成 0 天」：`interval '0 days'` 会让
    `fetched_at >= now()` 把全部候选排除，表现是「候选=0 篇」，看起来像「没有要补的」。
    """

    assert "documents.fetched_at >=" not in _sql(within_days=0)


def test_window_keeps_the_status_filter() -> None:
    """打开窗口不等于把已补好的文档再抓一遍：pending / 可重试失败的条件必须留着。"""

    sql = _sql(within_days=0)

    assert "documents.content_status =" in sql
    assert "documents.content_attempts <" in sql

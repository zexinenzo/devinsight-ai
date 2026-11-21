"""阶段 7 时间线的纯函数测试。

覆盖「不需要数据库就能钉住」的部分：实体归一化、区间冲突检测。

`entity_timeline` / `as_of` 的 SQL 语义依赖 Postgres 的 `NULLS LAST` 和日期比较，
而项目没有测试库（220 个用例全是 mock 的），这部分不在单元测试里假装覆盖。
它靠两样东西兜：迁移 0009 里的 CHECK 约束（区间必须非退化）和手工跑一遍真库。

测试重心是**不变量**：
- 同一个实体的不同写法必须收敛到同一个键，否则时间线会被切成几段；
- 区间重叠必须被抓出来，且同宾语的多源报道不能误报——误报会让「冲突」这个
  信号失去可信度，之后真冲突也没人看。
"""

from __future__ import annotations

import uuid
from datetime import date

from app.services.timeline_service import (
    MULTI_VALUED_PREDICATES,
    PREDICATES,
    TimelineEntry,
    find_overlaps,
    normalize_key,
)


def entry(
    predicate: str,
    object_text: str,
    start: date | None = None,
    end: date | None = None,
) -> TimelineEntry:
    return TimelineEntry(
        fact_id=uuid.uuid4(),
        predicate=predicate,
        predicate_label="",
        object_text=object_text,
        valid_from=start,
        valid_to=end,
        date_source="stated",
        confidence=0.9,
        evidence_text=None,
        document_id=uuid.uuid4(),
        document_title="标题",
        document_url="https://example.com/a",
        published_at=None,
    )


def test_normalize_key_collapses_spacing_and_case():
    assert normalize_key("OpenAI") == normalize_key("open ai") == normalize_key("OPENAI")


def test_normalize_key_drops_punctuation_but_keeps_digits():
    # GPT-4o / GPT 4o / gpt4o 是同一样东西，不收敛的话版本号时间线会断
    assert normalize_key("GPT-4o") == normalize_key("gpt 4o") == "gpt4o"


def test_normalize_key_keeps_cjk():
    # 中文不能被 isalnum 判掉——这是「中文实体全部归一化成空串」的经典坑
    assert normalize_key("深度求索") == "深度求索"
    assert normalize_key("智谱 AI") == "智谱ai"


def test_normalize_key_empty_for_punctuation_only():
    assert normalize_key("——") == ""


def test_overlap_detected_for_two_ceos_in_same_window():
    """两个 CEO 同时在任，是时间线上最危险的一种状态：看起来都像真的。"""

    left = entry("ceo", "Sam Altman", date(2023, 1, 1), None)
    right = entry("ceo", "Emmett Shear", date(2023, 11, 17), date(2023, 11, 21))
    problems = find_overlaps([left, right])
    assert len(problems) == 1
    assert "ceo" in problems[0]


def test_no_overlap_for_sequential_terms():
    left = entry("ceo", "Sam Altman", date(2023, 1, 1), date(2023, 11, 17))
    right = entry("ceo", "Emmett Shear", date(2023, 11, 17), date(2023, 11, 21))
    # 交接当天算后一任：半开区间 [from, to)。
    # 如果这里改成闭区间，同一职位前后任会在交接日「同时在职」，制造假冲突。
    assert find_overlaps([left, right]) == []


def test_same_object_reported_twice_is_not_a_conflict():
    """同一件事被两家媒体报道，是同宾语的两条记录，不是矛盾。

    用单值谓词测，不用 `released`：`released` 已归入多值谓词，会被整组跳过，
    这条断言会在「跳过」而不是「同宾语」这一支上通过——测试照样绿，
    但它测的东西已经不是原来那件事了。
    """

    left = entry("ceo", "Sam Altman", date(2026, 8, 7), None)
    right = entry("ceo", "Sam Altman", date(2026, 8, 7), None)
    assert find_overlaps([left, right]) == []


def test_open_ended_period_overlaps_anything_later():
    left = entry("released_version", "GPT-5", date(2026, 8, 7), None)
    right = entry("released_version", "GPT-6", date(2027, 1, 1), None)
    # 两条都没有 valid_to，说明都被当成「至今仍是当前版本」——这本身就是冲突
    assert len(find_overlaps([left, right])) == 1


def test_entries_without_dates_do_not_crash():
    left = entry("ceo", "Sam Altman", None, None)
    right = entry("ceo", "Emmett Shear", None, None)
    # 无日期的条目本该被分到 undated，不会进 find_overlaps；
    # 但函数不该因为收到 None 就抛异常——那会让整个时间线查询失败。
    assert len(find_overlaps([left, right])) == 1


def test_multi_valued_predicate_is_not_a_conflict():
    """六个合作方并不互相排斥。

    这条钉的是合并 NVIDIA / 英伟达 后暴露的问题：`partnered_with` 的 6 条进了
    同一次两两比较，一次产出 15 条「冲突」，而真冲突只有 3 条、渲染只取前 5 条——
    15 条噪音把 3 条真信号全挤出去了。
    """

    partners = ["台积电", "富士康", "安靠科技", "矽品精密", "景硕科技", "三星电机"]
    entries = [entry("partnered_with", name, date(2026, 11, 18), None) for name in partners]
    assert find_overlaps(entries) == []


def test_unknown_predicate_defaults_to_single_valued():
    """名单外的谓词按单值处理——宁可多报，不可漏报。

    将来加一个 `licensed_to` 之类的谓词，忘了同步多值名单时，结果是多几条噪音；
    如果默认反过来，结果是「两条矛盾并存却没人提示」，那才是这个项目最怕的错。
    """

    left = entry("licensed_to", "A 公司", date(2026, 1, 1), None)
    right = entry("licensed_to", "B 公司", date(2026, 1, 1), None)
    assert len(find_overlaps([left, right])) == 1


def test_multi_valued_names_all_exist_in_predicate_table():
    """名单写错一个名字，那条谓词会静默退回单值，噪音重新出现而没人报错。"""

    assert set(PREDICATES) >= MULTI_VALUED_PREDICATES


def test_single_entry_never_conflicts():
    assert find_overlaps([entry("ceo", "Sam Altman", date(2023, 1, 1), None)]) == []

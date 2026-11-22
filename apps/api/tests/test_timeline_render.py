"""时间线渲染：不连库，只看「该说的话有没有说」。"""

from __future__ import annotations

import uuid
from datetime import date

from app.services.timeline_render import format_timeline
from app.services.timeline_service import TimelineEntry, TimelineResult


def _entry(
    predicate: str,
    object_text: str,
    *,
    day: date | None = date(2026, 11, 18),
    source: str = "published",
) -> TimelineEntry:
    return TimelineEntry(
        fact_id=uuid.uuid4(),
        predicate=predicate,
        predicate_label=predicate,
        object_text=object_text,
        valid_from=day,
        valid_to=None,
        date_source=source,
        confidence=None,
        evidence_text=None,
        document_id=uuid.uuid4(),
        document_title="标题",
        document_url="https://example.com",
        published_at=None,
    )


def _result(entries, undated=(), *, merged=(), retracted=0, overlaps=()):
    return TimelineResult(
        entity_id=uuid.uuid4(),
        entity_name="英伟达",
        entity_kind="company",
        entries=list(entries),
        undated=list(undated),
        overlaps=list(overlaps),
        retracted_count=retracted,
        merged_names=list(merged),
    )


def test_hides_extra_objects_but_reports_them() -> None:
    """六个合作方不该把 CEO 挤出视野，也不该被无声吞掉。"""

    entries = [
        _entry("ceo", "黄仁勋", source="stated"),
        *[_entry("partnered_with", name) for name in ("台积电", "矽品精密", "景硕科技", "三星电机", "富士康")],
    ]
    lines = format_timeline(_result(entries))
    text = "\n".join(lines)

    assert "黄仁勋" in text, "单值谓词被多值谓词挤掉了"
    assert "另有 2 条" in text, "被省掉的条数没有交代"
    assert "三星电机" in text and "富士康" in text, "被省掉的宾语没有列出"
    assert text.count("台积电") == 1


def test_all_shows_everything() -> None:
    entries = [_entry("partnered_with", name) for name in ("A", "B", "C", "D")]
    text = "\n".join(format_timeline(_result(entries), per_predicate=99))

    for name in ("A", "B", "C", "D"):
        assert name in text
    assert "另有" not in text


def test_date_source_is_visible_per_entry() -> None:
    """两档日期必须长得不一样，否则等于把可信度混成一档。"""

    entries = [
        _entry("ceo", "黄仁勋", source="stated"),
        _entry("founded", "1993", source="published"),
    ]
    text = "\n".join(format_timeline(_result(entries)))

    assert "文中明说" in text
    assert "以发文时间兜底" in text


def test_undated_are_listed_separately() -> None:
    """没有日期的事实进不了时间轴，但不能消失。"""

    dated = [_entry("ceo", "黄仁勋")]
    undated = [_entry("partnered_with", "台积电", day=None)]
    lines = format_timeline(_result(dated, undated))
    text = "\n".join(lines)

    assert "1 条有日期" in text
    assert "1 条时间未知" in text
    assert "不参与排序" in text
    assert "台积电" in text


def test_retracted_and_merged_are_reported() -> None:
    """撤回条数与合并写法都是「数字覆盖了什么」的交代，缺了等于说谎。"""

    text = "\n".join(
        format_timeline(_result([_entry("ceo", "黄仁勋")], merged=["NVIDIA"], retracted=2))
    )

    assert "已撤回 2 条" in text
    assert "NVIDIA" in text


def test_overlaps_are_printed() -> None:
    text = "\n".join(
        format_timeline(_result([_entry("ceo", "黄仁勋")], overlaps=["ceo：'A' 与 'B' 区间重叠"]))
    )

    assert "区间重叠" in text
    assert "ceo：'A' 与 'B' 区间重叠" in text


def test_empty_timeline_does_not_crash() -> None:
    text = "\n".join(format_timeline(_result([])))

    assert "0 条有日期" in text
    assert "没有带日期的事实" in text


def test_empty_hint_can_be_made_specific() -> None:
    """`asof` 的「空」是「那天没有」，不是「从来没记过」，两句话不能共用。"""

    text = "\n".join(
        format_timeline(_result([]), note="在 2026-11-18 成立", empty_hint="这天没有成立的事实")
    )

    assert "这天没有成立的事实" in text
    assert "没有带日期的事实" not in text

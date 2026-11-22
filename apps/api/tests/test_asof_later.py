"""`as_of` 的空结果必须说明成因，不能替数据下否定结论。"""

from __future__ import annotations

import uuid
from datetime import date

from app.services.timeline_render import format_timeline
from app.services.timeline_service import TimelineEntry, TimelineResult


def _entry(predicate: str, object_text: str, day: date = date(2026, 11, 18)) -> TimelineEntry:
    return TimelineEntry(
        fact_id=uuid.uuid4(),
        predicate=predicate,
        predicate_label=predicate,
        object_text=object_text,
        valid_from=day,
        valid_to=None,
        date_source="published",
        confidence=None,
        evidence_text=None,
        document_id=uuid.uuid4(),
        document_title="标题",
        document_url="https://example.com",
        published_at=None,
    )


def _result(*, entries=(), later=()):
    return TimelineResult(
        entity_id=uuid.uuid4(),
        entity_name="英伟达",
        entity_kind="company",
        entries=list(entries),
        later=list(later),
    )


def test_later_entries_are_explained_not_denied() -> None:
    """空结果 + 更晚的起点：要说「不知道」，不能说「那天不是这样」。"""

    lines = format_timeline(
        _result(later=[_entry("ceo", "Jensen Huang"), _entry("ceo", "Colette Kress")]),
        note="在 2026-06-01 成立",
        empty_hint="这天没有成立的事实",
    )
    text = "\n".join(lines)

    assert "这天没有成立的事实" in text
    assert "2 条事实的起点晚于该日" in text
    assert "不知道" in text
    assert "Jensen Huang" in text and "Colette Kress" in text


def test_later_section_caps_the_list() -> None:
    """成因只要几条就够，列满一屏反而盖住重点。"""

    later = [_entry("partnered_with", f"合作方{i}") for i in range(8)]
    text = "\n".join(format_timeline(_result(later=later), note="在 2026-06-01 成立"))

    assert "8 条事实的起点晚于该日" in text
    assert "合作方4" in text
    assert "合作方5" not in text
    assert "另有 3 条" in text


def test_no_later_section_when_nothing_is_later() -> None:
    """没有更晚的起点就别说这一节，否则等于无中生有地解释空结果。"""

    text = "\n".join(format_timeline(_result(entries=[_entry("ceo", "Jensen Huang")])))

    assert "起点晚于该日" not in text

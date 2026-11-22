"""`as_of` 的措辞：全空、部分命中，两种结果的成因不同，说法不能共用。"""

from __future__ import annotations

import uuid
from datetime import date

from app.services.timeline_render import format_timeline
from app.services.timeline_service import TimelineEntry, TimelineResult

ON = "在 2026-06-01 成立"


def _entry(object_text: str) -> TimelineEntry:
    return TimelineEntry(
        fact_id=uuid.uuid4(),
        predicate="ceo",
        predicate_label="ceo",
        object_text=object_text,
        valid_from=date(2026, 11, 18),
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


def test_later_section_with_entries_does_not_deny_the_day() -> None:
    """当天已有成立的事实，后面这一节不能说「不知道」——那是在否定刚给出的答案。"""

    text = "\n".join(
        format_timeline(
            _result(entries=[_entry("Jensen Huang")], later=[_entry("Colette Kress")]),
            note=ON,
            empty_hint="这天没有成立的事实",
        )
    )

    assert "另有 1 条事实的起点晚于该日，未计入当天快照" in text
    assert "不知道" not in text
    assert "Jensen Huang" in text


def test_dated_label_can_be_made_specific() -> None:
    """`0 条有日期` 与紧随其后的 `11 条……起点` 自相矛盾，标签要能换。"""

    text = "\n".join(
        format_timeline(
            _result(later=[_entry("Jensen Huang")]),
            note=ON,
            dated_label="条在该日成立",
        )
    )

    assert "0 条在该日成立" in text
    assert "0 条有日期" not in text

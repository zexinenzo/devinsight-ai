"""date_quote：`stated` 这个档位必须能被验证。

真实缺口：`星曜宇航` 那条原文写 2026 年 2 月，模型没给日期，落库按 published
兜底成 11-21，差 9 个月。反过来的一类更隐蔽——模型直接把文章发布日期填成
valid_from，看起来「文中明说」，其实没有任何原文支撑。这一组测试守的是后者。
"""

from __future__ import annotations

from datetime import date

from app.services.fact_extraction import (
    parse_date_in_text,
    to_fact,
    verify_date_quote,
)


def test_cn_full_date() -> None:
    assert parse_date_in_text("公司于2026年2月13日正式启动") == (date(2026, 2, 13), "day")


def test_cn_month_day_uses_hint_year() -> None:
    got = parse_date_in_text("2月13日正式启动", hint_year=2026)

    assert got == (date(2026, 2, 13), "day")


def test_cn_month_only_is_month_precision() -> None:
    got = parse_date_in_text("2026年2月启动")

    assert got == (date(2026, 2, 1), "month")


def test_iso_in_quote() -> None:
    assert parse_date_in_text("released on 2026-02-13") == (date(2026, 2, 13), "day")


def test_en_month_name() -> None:
    assert parse_date_in_text("launched on Feb 13, 2026") == (date(2026, 2, 13), "day")


def test_month_day_without_hint_year_is_unparsed() -> None:
    """没有年份提示时，「2月13日」不足以定位到某一年——宁可判成无日期。"""

    assert parse_date_in_text("2月13日启动") is None


def test_quote_missing() -> None:
    assert verify_date_quote(None, date(2026, 2, 13), "任何原文", 2026) == "quote_missing"


def test_quote_not_in_source() -> None:
    got = verify_date_quote("2026年3月1日发布", date(2026, 2, 13), "原文里没有这句话", 2026)

    assert got == "quote_not_in_source"


def test_quote_without_date() -> None:
    quote = "公司宣布完成新一轮融资"

    assert verify_date_quote(quote, date(2026, 2, 13), f"今日，{quote}", 2026) == "quote_has_no_date"


def test_quote_day_matches() -> None:
    quote = "2026年2月13日正式启动"

    assert verify_date_quote(quote, date(2026, 2, 13), f"讯，{quote}。", 2026) is None


def test_quote_day_conflicts() -> None:
    quote = "2026年2月20日启动"

    assert verify_date_quote(quote, date(2026, 2, 13), quote, 2026) == "quote_date_mismatch"


def test_quote_month_matches_day() -> None:
    """月级引文 + 同年同月：放行。

    日不可验，但年月对得上时它仍比 published 兜底准——兜底给的是发文那天，
    可能差几个月。代价是 `stated` 从此只保证年月，这一点写在文档里。
    """

    quote = "2026年2月启动"

    assert verify_date_quote(quote, date(2026, 2, 13), quote, 2026) is None


def test_quote_month_conflicts() -> None:
    quote = "2026年3月启动"

    assert verify_date_quote(quote, date(2026, 2, 13), quote, 2026) == "quote_date_mismatch"


def test_quote_punctuation_width_ignored() -> None:
    """原文半角逗号、引文全角，不该判成「不在原文里」。

    与 `evidence_match` 同一条理由：比对的是字在不在，不是标点一致不一致。
    """

    quote = "2026年2月13日，正式启动"
    source = "2026年2月13日,正式启动"

    assert verify_date_quote(quote, date(2026, 2, 13), source, 2026) is None


def test_to_fact_keeps_date_quote() -> None:
    fact, reason = to_fact(
        {
            "subject": "美新科技",
            "predicate": "founded",
            "object": "四川星曜宇航航天科技有限公司",
            "valid_from": "2026-02-13",
            "date_quote": "于2月13日正式启动",
            "evidence": "美新科技子公司星曜宇航于2月13日正式启动",
            "confidence": 0.8,
        }
    )

    assert reason == ""
    assert fact is not None
    assert fact.date_quote == "于2月13日正式启动"

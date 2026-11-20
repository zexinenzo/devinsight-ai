"""阶段 7 事实抽取的解析测试。

被测的是「模型输出 → 能落库的事实」这一段，因为模型输出是不可信输入：
它会加 ```json 围栏、把数组包进 `{"facts": [...]}`、给中文类别、给只到月的日期、
发明词表里没有的谓词。

测试重心是**拒绝的方式**而不是拒绝本身：造词的谓词必须被丢掉并计入统计
（这样人才会去补词表），而类别不合枚举只该退化、不该丢掉整条事实——
两者的区别决定了这个抽取器是可维护的还是渐渐变成一堆噪声。
"""

from __future__ import annotations

from datetime import date

from app.services.fact_extraction import (
    MAX_OBJECT_CHARS,
    build_prompt,
    classify_unparsed,
    evidence_match,
    parse_date_value,
    parse_facts_json,
    to_fact,
    try_parse_facts_json,
)


def test_parse_plain_array():
    raw = '[{"subject": "OpenAI", "predicate": "released", "object": "GPT-5"}]'
    facts = parse_facts_json(raw)
    assert len(facts) == 1
    assert facts[0]["subject"] == "OpenAI"


def test_parse_strips_code_fence():
    raw = '```json\n[{"subject": "OpenAI"}]\n```'
    assert parse_facts_json(raw) == [{"subject": "OpenAI"}]


def test_parse_unwraps_object_with_facts_key():
    raw = '{"facts": [{"subject": "Anthropic"}, {"subject": "OpenAI"}]}'
    facts = parse_facts_json(raw)
    assert [item["subject"] for item in facts] == ["Anthropic", "OpenAI"]


def test_parse_ignores_leading_prose():
    raw = '好的，抽取结果如下：\n[{"subject": "OpenAI"}]\n以上。'
    assert parse_facts_json(raw) == [{"subject": "OpenAI"}]


def test_parse_garbage_returns_empty_instead_of_guessing():
    # 猜一个事实出来比没有更糟：它会进时间线，而没人知道它是编的
    assert parse_facts_json("这篇文章讲了很多事情，但没有结构化信息。") == []


def test_try_parse_distinguishes_empty_from_unparsable():
    # 「本文没有事实」和「模型没按格式回答」在批量抽取里必须分开：
    # 前者可以继续，后者要换模型。都塌成 [] 就永远看不出区别，
    # 而首跑 20 篇里 10 篇 0 条、同时 0 条被拒，这个「太干净」本身就是可疑信号。
    assert try_parse_facts_json("[]") == []
    assert try_parse_facts_json('{"facts": []}') == []
    assert try_parse_facts_json("这篇文章讲了很多事情。") is None


def test_try_parse_keeps_empty_list_inside_fence():
    # 围栏剥掉之后仍然是 []，不能因为「空列表是假值」就当成解析失败
    assert try_parse_facts_json("```json\n[]\n```") == []


def test_classify_unparsed_separates_three_causes():
    # 477 篇里 12 篇解析失败，但原因完全不同：6 篇输出被 max_tokens 砍断、
    # 6 篇上游返回空串。混成一个「解析失败」只能得出「模型不稳定」这种
    # 没法动手的结论。
    assert classify_unparsed("length", '[{"subject": "OpenAI", "object": "GP') == "truncated"
    assert classify_unparsed("stop", "") == "empty"
    assert classify_unparsed("stop", "好的，以下是抽取结果：") == "format"


def test_classify_unparsed_truncation_wins_over_empty():
    # 被截断时 content 可能刚好为空（工具调用之外的极端情况）。
    # 原因判定必须排在内容判定前面，否则 6 篇截断会被误报成上游故障。
    assert classify_unparsed("length", "") == "truncated"


def test_prompt_pins_benchmark_object_type():
    # 实测里 benchmark 这一个谓词出过两种宾语：`Autoform-Hard`（考卷名）和
    # `法律领域评测第一名`（名次）。宾语类型不固定，后续报道里的新成绩就永远
    # 对不上旧行，时间线在这里断掉。
    prompt = build_prompt()
    assert "宾语类型必须固定" in prompt
    assert "分数与名次一律写进 evidence" in prompt


def test_prompt_forbids_prose_around_json():
    # 二跑 20 篇里 8 篇的输出不是 JSON，正好是上一轮抽得出事实的那几篇。
    # 模型把「没有可抽事实」写成了中文句子——格式约束必须写在最前面，
    # 只写在规则 9 里会被当成建议。
    prompt = build_prompt()
    assert prompt.index("只能是一个 JSON 数组") < prompt.index("规则：")


def test_to_fact_accepts_known_predicate():
    fact, reason = to_fact(
        {
            "subject": "OpenAI",
            "subject_kind": "company",
            "predicate": "released",
            "object": "GPT-5",
            "valid_from": "2026-08-07",
            "evidence": "OpenAI 于 8 月 7 日发布 GPT-5。",
            "confidence": 0.9,
        }
    )
    assert reason == ""
    assert fact is not None
    assert fact.predicate == "released"
    assert fact.valid_from == date(2026, 8, 7)


def test_to_fact_rejects_invented_predicate():
    fact, reason = to_fact(
        {"subject": "OpenAI", "predicate": "launched", "object": "GPT-5"}
    )
    assert fact is None
    assert reason == "predicate"


def test_to_fact_degrades_unknown_kind_instead_of_dropping():
    # 模型给「公司」这种中文类别时，谓词和宾语都是对的，只差一个标签
    fact, reason = to_fact(
        {
            "subject": "OpenAI",
            "subject_kind": "公司",
            "predicate": "released",
            "object": "GPT-5",
        }
    )
    assert reason == ""
    assert fact is not None
    assert fact.subject_kind == "product"


def test_to_fact_rejects_missing_object():
    fact, reason = to_fact({"subject": "OpenAI", "predicate": "released", "object": ""})
    assert fact is None
    assert reason == "malformed"


def test_confidence_is_clamped():
    high, _ = to_fact(
        {"subject": "A", "predicate": "released", "object": "B", "confidence": 9.9}
    )
    low, _ = to_fact(
        {"subject": "A", "predicate": "released", "object": "B", "confidence": -3}
    )
    assert high is not None and high.confidence == 1.0
    assert low is not None and low.confidence == 0.0


def test_parse_date_accepts_iso_and_slash():
    assert parse_date_value("2026-08-07") == date(2026, 8, 7)
    assert parse_date_value("2026/8/7") == date(2026, 8, 7)
    assert parse_date_value("2026-08-07T10:00:00Z") == date(2026, 8, 7)


def test_parse_date_rejects_month_only():
    # 「2025 年 11 月」补成 11-01 会凭空造出一天精度，排序位置就是错的
    assert parse_date_value("2025-11") is None


def test_parse_date_rejects_nulls_and_words():
    for value in ("", "null", "None", "unknown", "上周", None, 20250807):
        assert parse_date_value(value) is None


def test_parse_date_rejects_impossible_date():
    assert parse_date_value("2026-02-31") is None


def test_prompt_lists_every_predicate():
    prompt = build_prompt()
    # 词表和 prompt 必须是同一份：两边不一致时，模型会按词表抽、校验按另一份拒
    assert "released" in prompt
    assert "ceo" in prompt
    assert "discontinued" in prompt


def test_to_fact_rejects_reflexive():
    # 实测输出里真实出现过 `Mode --open_sourced--> Mode`：零信息量，
    # 却会让「Mode 发生过什么」多一条记录
    fact, reason = to_fact({"subject": "Mode", "predicate": "open_sourced", "object": "Mode"})
    assert fact is None
    assert reason == "reflexive"


def test_to_fact_reflexive_check_ignores_case_and_spacing():
    fact, reason = to_fact(
        {"subject": "GPT-4o", "predicate": "released", "object": " gpt 4o "}
    )
    assert fact is None
    assert reason == "reflexive"


def test_to_fact_allows_object_that_extends_subject():
    # `OpenAI API` 是合法产品名，不能因为含主语就被拦
    fact, reason = to_fact({"subject": "OpenAI", "predicate": "released", "object": "OpenAI API"})
    assert reason == ""
    assert fact is not None


def test_to_fact_rejects_sentence_as_object():
    # 实测输出：`在覆盖 24 个法律领域的发布评测中排名第一`
    long_object = "ChatGPT for Academic Researchers 计划扩展，向全球 20 万研究人员免费开放前沿模型"
    fact, reason = to_fact(
        {"subject": "ChatGPT", "predicate": "announced", "object": long_object}
    )
    assert fact is None
    assert reason == "too_long"


def test_to_fact_object_length_boundary():
    at_limit, reason_ok = to_fact(
        {"subject": "X", "predicate": "released", "object": "a" * MAX_OBJECT_CHARS}
    )
    over_limit, reason_bad = to_fact(
        {"subject": "X", "predicate": "released", "object": "a" * (MAX_OBJECT_CHARS + 1)}
    )
    assert reason_ok == "" and at_limit is not None
    assert reason_bad == "too_long" and over_limit is None


def test_evidence_exact_match_ignores_whitespace():
    # 中文原文常被换行切断，逐字比对会把「原样抄的」误判成编造
    source = "OpenAI 于 8 月 7 日发布\nGPT-5，并同步上线 API。"
    assert evidence_match("OpenAI 于 8 月 7 日发布 GPT-5", source) == "exact"


def test_evidence_partial_when_only_head_matches():
    # 模型抄了原句又补了省略号，内容可信但结尾不同
    source = "OpenAI 于 8 月 7 日发布 GPT-5，并同步上线 API。"
    evidence = "OpenAI 于 8 月 7 日发布 GPT-5……后续细节未提及"
    assert evidence_match(evidence, source) == "partial"


def test_evidence_missing_when_fabricated():
    source = "OpenAI 于 8 月 7 日发布 GPT-5，并同步上线 API。"
    assert evidence_match("Anthropic 在同一天发布了 Claude 5", source) == "missing"


def test_evidence_missing_for_empty_input():
    assert evidence_match(None, "任何原文") == "missing"
    assert evidence_match("  ", "任何原文") == "missing"


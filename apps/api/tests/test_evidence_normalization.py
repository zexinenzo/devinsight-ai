"""证据比对只比字：全半角标点不该影响判定。

真实误判：原文 `称,FSD V15`（半角逗号），模型抄成 `称，FSD V15`（全角），
`evidence_match` 返回 `partial`。`partial` 用来提示「模型可能改了证据」，
被标点宽度占用之后，真正需要警惕的那一类就淹了。
"""

from __future__ import annotations

from app.services.fact_extraction import evidence_match

SOURCE = (
    "特斯拉CEO埃隆·马斯克在X平台上发帖称,FSD V15版本下个月将全面推送。"
    "他写道:“SFT(有监督微调)与其说是“微调”,倒不如说是“用电锯理发”!”"
)


def test_fullwidth_comma_does_not_break_exact() -> None:
    evidence = "特斯拉CEO埃隆·马斯克在X平台上发帖称，FSD V15版本下个月将全面推送。"

    assert evidence_match(evidence, SOURCE) == "exact"


def test_fullwidth_parentheses_and_quotes_do_not_break_exact() -> None:
    evidence = "他写道：“SFT（有监督微调）与其说是“微调”，倒不如说是“用电锯理发”！”"

    assert evidence_match(evidence, SOURCE) == "exact"


def test_fullwidth_letters_and_digits_are_normalized() -> None:
    assert evidence_match("发布 ＧＰＴ－５ 模型", "今天发布 GPT-5 模型。") == "exact"


def test_missing_middle_dot_still_matches() -> None:
    """人名里的间隔号是排版差异，不是内容差异。"""

    assert evidence_match("特斯拉CEO埃隆马斯克在X平台上发帖称", SOURCE) == "exact"


def test_fabricated_evidence_is_still_missing() -> None:
    """放宽标点不能把编造的句子也放过去。"""

    assert evidence_match("Anthropic 在同一天发布了 Claude 5", SOURCE) == "missing"


def test_truncated_tail_is_still_partial() -> None:
    """模型补了省略号仍是 partial——这一档要保住。"""

    evidence = "特斯拉CEO埃隆·马斯克在X平台上发帖称,FSD V15版本下个月将全面推送……后续略"

    assert evidence_match(evidence, SOURCE) == "partial"

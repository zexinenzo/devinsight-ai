"""摘要补全的判定逻辑测试。

这一层要守住三条性质：

1. 上游没给摘要时，能从抓到的页面里补出一段可读的话（否则列表只剩标题）；
2. 上游已经给了摘要时，不能被页面文本覆盖（编辑写的导语比机器截的句子准）；
3. `missing_summary` 标记要跟着摘要一起消失，否则前端会「有摘要却显示缺摘要」。
"""

from __future__ import annotations

from app.db.models import Document
from app.services.content_service import (
    SUMMARY_MAX_CHARS,
    PageResult,
    _clip_summary,
    _derive_summary,
    apply_result,
)
def _document(**overrides) -> Document:
    base = {
        "title": "标题",
        "rights_mode": "metadata_excerpt",
        "summary": None,
        "content_text": None,
        "quality_flags": ["missing_summary"],
    }
    base.update(overrides)
    return Document(**base)


def test_derive_summary_skips_short_leading_lines() -> None:
    """正文开头的署名行、栏目名不该成为摘要。"""

    text = "记者 张三\n编辑 李四\n\n过去一年里，大模型推理的单位成本下降了接近一个数量级，主要来自量化与蒸馏技术的成熟。"
    summary = _derive_summary(text)

    assert summary is not None
    assert summary.startswith("过去一年里")


def test_derive_summary_prefers_sentence_boundary() -> None:
    """超长段落要回退到句号，不能从半个词中间切断。"""

    long_paragraph = "第一句话讲清楚背景和结论。" + "后面是对这个结论的详细补充说明，" * 40
    summary = _derive_summary(long_paragraph)

    assert summary is not None
    assert len(summary) <= SUMMARY_MAX_CHARS + 1
    # 句号在开头附近时无法在窗口内收尾，此时用省略号收口，但绝不能停在逗号上
    assert summary.endswith(("。", "…"))
    assert not summary.endswith("，")


def test_derive_summary_returns_none_for_empty_text() -> None:
    assert _derive_summary("") is None
    assert _derive_summary("短\n短\n") is None


def test_apply_result_fills_summary_from_meta_hint() -> None:
    """HN 这类只给标题的源：描述就是唯一能当摘要的东西。"""

    document = _document()
    result = PageResult(status="fetched", text="正文" * 200, summary_hint="编辑写的导语，说明这条讲了什么。")

    apply_result(document, result)

    assert document.summary == "编辑写的导语，说明这条讲了什么。"
    assert "missing_summary" not in (document.quality_flags or [])


def test_apply_result_falls_back_to_body_when_no_meta() -> None:
    document = _document()
    result = PageResult(
        status="fetched",
        text="\n\n这是正文的第一段，长度足够用作列表里的一段摘要。\n\n第二段内容。",
    )

    apply_result(document, result)

    assert document.summary == "这是正文的第一段，长度足够用作列表里的一段摘要。"


def test_apply_result_keeps_existing_summary() -> None:
    """已有摘要时，页面文本不能顶掉它。"""

    document = _document(summary="上游 feed 给的原始摘要", quality_flags=[])
    result = PageResult(status="fetched", text="网页正文第一段。" * 30, summary_hint="页面描述")

    apply_result(document, result)

    assert document.summary == "上游 feed 给的原始摘要"


def test_apply_result_clips_long_meta_description() -> None:
    """og:description 有时长达六七十字（实测见过 677 字），必须收进上限。"""

    document = _document()
    long_hint = "这一段是页面描述。" + "补充说明，" * 120
    result = PageResult(status="fetched", text="正文" * 300, summary_hint=long_hint)

    apply_result(document, result)

    assert document.summary is not None
    assert len(document.summary) <= SUMMARY_MAX_CHARS + 1


def test_derive_summary_skips_title_repeat() -> None:
    """正文抽取常把 <h1> 当第一段，摘要不能就是标题。"""

    title = "Java4Shell is a reason to use Java"
    text = f"{title}\n\n这个漏洞影响范围很广，从构建工具到 CI 流水线都在受影响列表里，\n修复版本已经发布。"

    summary = _derive_summary(text, title)

    assert summary is not None
    assert summary != title
    assert summary.startswith("这个漏洞影响范围很广")


def test_apply_result_rejects_title_as_summary_hint() -> None:
    document = _document(title="理解 SimHash 去重")
    result = PageResult(
        status="fetched",
        text="正文第一段讲的是指纹怎么算，以及为什么汉明距离阈值要按语料实测。",
        summary_hint="理解 SimHash 去重",
    )

    apply_result(document, result)

    assert document.summary != "理解 SimHash 去重"
    assert document.summary is not None


def test_derive_summary_skips_navigation_residue() -> None:
    """实测撞到的两类界面残渣：栏目名和无障碍提示。"""

    text = "INSIDE Deutsche Bank\n\n这家银行在 2026 年第三季度的净利息收入同比下降了 8%，主要受存款成本上升影响。"
    summary = _derive_summary(text, "Deutsche Bank")
    assert summary is not None
    assert "INSIDE" not in summary

    text2 = "(opens in a new tab)\n\n微软在 Ignite 大会上发布了七个新模型，覆盖语音、图像与推理三类任务。"
    summary = _derive_summary(text2, "Microsoft AI")
    assert summary is not None
    assert "opens in a new tab" not in summary


def test_derive_summary_returns_none_when_only_noise() -> None:
    assert _derive_summary("INSIDE Deutsche Bank\n\nSkip to content", "Deutsche Bank") is None


def test_clip_summary_keeps_short_text_intact() -> None:
    assert _clip_summary("  一句话摘要。  ") == "一句话摘要。"


def test_apply_result_still_overwrites_fake_content_copy() -> None:
    """摘要补上之后，伪造正文的识别不能被带偏。"""

    document = _document(summary="这是上游给的摘要", content_text="这是上游给的摘要", quality_flags=[])
    result = PageResult(status="fetched", text="真正的网页正文。" * 60)

    apply_result(document, result)

    assert document.content_text.startswith("真正的网页正文")
    assert document.summary == "这是上游给的摘要"


def test_failed_result_leaves_summary_untouched() -> None:
    document = _document()
    apply_result(document, PageResult(status="failed", error="HTTP 403"))

    assert document.summary is None
    assert document.content_status == "failed"

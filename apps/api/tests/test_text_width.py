"""终端显示宽度：所有表格对齐都建在这两个函数上。"""

from __future__ import annotations

from app.utils.text_width import display_width, pad


def test_cjk_counts_as_two_columns() -> None:
    assert display_width("英伟达") == 6
    assert display_width("NVIDIA") == 6


def test_pad_uses_display_width_not_len() -> None:
    """按 len 补位会让中英混排的两列差出几个空格。"""

    assert display_width(pad("英伟达", 10)) == 10
    assert display_width(pad("NVIDIA", 10)) == 10
    # 同样的显示宽度，字符数并不相同——这正是不能按 len 补位的原因
    assert len(pad("英伟达", 10)) == 7
    assert len(pad("NVIDIA", 10)) == 10


def test_pad_never_truncates_long_text() -> None:
    assert pad("超出宽度的很长一段文字", 2) == "超出宽度的很长一段文字"

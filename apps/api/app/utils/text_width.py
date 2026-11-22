"""终端显示宽度。

中文、日文与全角标点占两列，而 `len()` 按一列算，所以任何按字符数补位的表
（f-string 的 `<20`）在中英混排时都会歪：来源名一长（`github_release_transformers`）
就把后一列顶出去。

这个模块只做这一件事。`cli` 与时间线渲染都从这里拿，不各写一份——
两份实现不是排版问题，是「同一个表在两处渲染出来不一样宽」的原因。

`scripts/repair_facts.py` 里还有一份同样的实现，**这是已知的重复，不是漏改**：
那个脚本要能独立跑，不依赖 `apps/api` 的包；而且它的补位允许多留一个空格
（`max(1, …)`），合并会改变它已有的输出宽度。
"""

from __future__ import annotations

import unicodedata


def display_width(text: str) -> int:
    """按终端列数算长度：`W`（宽）与 `F`（全角）各占两列。"""

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int, *, min_gap: int = 0) -> str:
    """补到 `width` 列。

    `min_gap` 是「至少留几个空格」：表格单元格之间不能贴死，但纯数字列
    又不需要额外间距，所以它默认 0，由调用方决定。
    """

    return text + " " * max(min_gap, width - display_width(text))

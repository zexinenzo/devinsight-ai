"""热度归一：把各源互不相通的互动指标压成一个可排序的分值。

**只做单调变换，不做跨源归一。** 源的量纲差得太远——HN 一个故事几百票，GitHub
一个版本的下载量可以到几十万——硬压到同一个 0~1 区间会变成「下载量永远第一」。
而实际选取时是按源分组取前 N，所以只需要**源内**排序正确。

用 `log1p` 而不是原始值：热度分布是长尾的，几千票和几百票的差别远小于
几票和零票的差别，取对数后才符合「有多热」的直觉。

返回 `None` 表示该源不暴露热度信号（RSS、arXiv）。**不要把它当成 0**——
「零热度」和「没有这个指标」是两件事，混同会让没有信号的源永远排在最后。
"""

from __future__ import annotations

import math


def normalize_engagement(engagement: dict | None) -> float | None:
    if not engagement:
        return None
    primary = engagement.get("primary")
    if not isinstance(primary, (int, float)) or isinstance(primary, bool) or primary < 0:
        return None
    return math.log1p(primary)

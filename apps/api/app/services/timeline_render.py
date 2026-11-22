"""时间线的终端渲染。

`timeline_service` 回答「有哪些事实」，这里只决定「先给人看哪几条、剩下的怎么交代」。
单独成文件有两个理由：渲染是**纯函数**，给一组 `TimelineEntry` 就能断言输出，不用连库；
`cli` 会连带导入采集与向量模块，测试不该为了验几行排版把那一串依赖拉起来。

三条规则，都不是排版偏好：

1. **日期来源逐条显示**。`以发文时间兜底` 与 `文中明说` 在时间线上长得一样，但前者
   不能当依据用。不显示这一列，等于把两档可信度混成一档——而这正是双时间表要区分的东西。
2. **同一谓词默认只显示前几条，但省掉的必须成句报出来**。`partnered_with` 六条合作方
   会把同一页上的 CEO 挤到看不见；而静默截断会让「只有 3 条事实」和「有 9 条、另外 6 条
   没显示」在调用方看来完全一样。
3. **无日期的一条不参与排序，也不许丢**。它们进不了时间轴，但丢掉就成了「这家公司
   从来没记过这件事」。
"""

from __future__ import annotations

from app.services.predicate_specs import PREDICATES
from app.services.timeline_service import DATE_SOURCE_LABELS, TimelineEntry, TimelineResult
from app.utils.text_width import pad

# 同一谓词默认显示几条。取 3 是因为时间线的常见读法是「最近发生了什么」，
# 而多值谓词一次能吐出六条同类（六个合作方、六个平台版本），两三轮就把一屏占满。
PREDICATE_CAP = 3

_DAY_WIDTH = 11
# 「起点晚于该日」一节最多列几条。它的作用是给空结果一个成因，不是把整条时间线
# 再倒着贴一遍——列满一屏反而盖住了「这天为什么是空的」这个重点。
_LATER_SHOWN = 5
_LABEL_WIDTH = 15


def _entry_line(entry: TimelineEntry) -> str:
    """一条事实一行：日期、日期来源、谓词、宾语。

    日期来源夹在中间而不是放行尾，因为它修饰的是日期本身——读者看到日期的那一刻
    就要知道这个日期是哪一档，而不是扫完宾语再回头找。

    无日期用 `?` 而不是破折号：`—` 的宽度东西文终端不一致（`east_asian_width`
    判为 ambiguous），拿它补位会让这一列整体差一列。
    """

    day = entry.valid_from.isoformat() if entry.valid_from else "?"
    source = DATE_SOURCE_LABELS.get(entry.date_source, entry.date_source)
    label = PREDICATES.get(entry.predicate, entry.predicate)
    return (
        "  "
        + pad(day, _DAY_WIDTH)
        + pad(source, _LABEL_WIDTH)
        + pad(label, _LABEL_WIDTH)
        + entry.object_text
    )


def format_timeline(
    result: TimelineResult,
    *,
    per_predicate: int = PREDICATE_CAP,
    note: str | None = None,
    empty_hint: str = "没有带日期的事实",
) -> list[str]:
    """把一次查询渲染成若干行。调用方只负责 print，不再做判断。"""

    title = f"{result.entity_name}（{result.entity_kind}）"
    if note:
        title = f"{title} · {note}"
    lines = [title]

    meta = [f"{result.dated_count} 条有日期"]
    if result.undated_count:
        meta.append(f"{result.undated_count} 条时间未知")
    if result.retracted_count:
        meta.append(f"已撤回 {result.retracted_count} 条（不计入）")
    if result.merged_names:
        meta.append("合并写法：" + "、".join(result.merged_names))
    lines.append("  " + " · ".join(meta))
    lines.append("")

    shown: dict[str, int] = {}
    hidden: dict[str, list[str]] = {}
    for entry in result.entries:
        count = shown.get(entry.predicate, 0)
        if count >= per_predicate:
            hidden.setdefault(entry.predicate, []).append(entry.object_text)
            continue
        shown[entry.predicate] = count + 1
        lines.append(_entry_line(entry))

    if not result.entries:
        lines.append(f"  {empty_hint}")

    if hidden:
        lines.append("")
        for predicate, objects in hidden.items():
            label = PREDICATES.get(predicate, predicate)
            listed = "、".join(objects[:4])
            if len(objects) > 4:
                listed += f" 等 {len(objects)} 条"
            lines.append(f"  … 另有 {len(objects)} 条「{label}」：{listed}")
        lines.append(f"  （同一谓词只显示前 {per_predicate} 条，加 --all 展开）")

    if result.undated:
        lines.append("")
        lines.append(
            f"  时间未知（{result.undated_count} 条，不参与排序与「某时点成立」的判断）："
        )
        for entry in result.undated:
            lines.append(_entry_line(entry))

    if result.later:
        lines.append("")
        lines.append(
            f"  有 {len(result.later)} 条事实的起点晚于该日——"
            "对当天的状态是「不知道」，不是「不是这样」："
        )
        for entry in result.later[:_LATER_SHOWN]:
            lines.append(_entry_line(entry))
        if len(result.later) > _LATER_SHOWN:
            lines.append(f"  … 另有 {len(result.later) - _LATER_SHOWN} 条")

    if result.overlaps:
        lines.append("")
        lines.append("  区间重叠（同一谓词下两个宾语同时成立，需人工核对）：")
        for message in result.overlaps:
            lines.append(f"    · {message}")

    return lines

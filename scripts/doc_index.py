#!/usr/bin/env python
"""生成 docs/README.md 索引。

为什么要有：2026-11-23 的会话里，同一份收尾文档被叫过六个名字
（阶段10-收尾与验收 / 阶段10-收尾 / 阶段10-收尾清单 / 收尾与验收 /
项目收尾与验收 / 项目完工清单），每次都要猜文件名，猜错就读不到、写错文件。
索引的作用是让「有哪些文档」这件事可以被查到，而不是靠记忆。

生成规则：
  · 扫描 docs/*.md，取首个 Markdown 标题当一句话说明
  · `<!-- manual:start -->` 与 `<!-- manual:end -->` 之间的内容原样保留——
    自动生成的表格答不了「什么时候该读它」，那一列要人写
  · 文件本身标注「由脚本生成」，避免手改后被覆盖后才发现

用法：
    .venv/bin/python scripts/doc_index.py           # 重新生成
    .venv/bin/python scripts/doc_index.py --check   # 只校验，过期则退出码 1

生成标记 retro-2026-11-23（重跑补丁时靠它判断这份文件归我维护）。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
TARGET = DOCS / "README.md"
MANUAL_START = "<!-- manual:start -->"
MANUAL_END = "<!-- manual:end -->"

MANUAL_DEFAULT = (
    MANUAL_START + "\n"
    "## 从哪里开始\n\n"
    "读的顺序按「想知道什么」定，不按阶段号：\n\n"
    "- **想知道这个系统怎么设计的** → 最长的那个总纲文档\n"
    "- **想查某个阶段做了什么、为什么这么做** → 对应阶段文档\n"
    "- **抽取/时间线出了问题** → 排查方法（先只读统计，再改代码）\n"
    "\n"
    "（这一段由人维护；脚本只重写下面的表格。）\n"
    + MANUAL_END
)


def first_heading(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
    except OSError:
        pass
    return "（无标题）"


def manual_block(existing: str) -> str:
    if MANUAL_START in existing and MANUAL_END in existing:
        start = existing.index(MANUAL_START)
        end = existing.index(MANUAL_END) + len(MANUAL_END)
        return existing[start:end]
    return MANUAL_DEFAULT


def render(existing: str) -> str:
    files = sorted(
        (p for p in DOCS.glob("*.md") if p.name != "README.md"),
        key=lambda p: p.name,
    )
    # 刻意不写时间戳：它会让文件每分钟都变一次，--check 永远报过期，git 也一直在抖。
    # 这份文件的唯一职责是「有哪些文档」，文档没变它就不该变。
    lines = [
        "# 文档索引",
        "",
        "<!-- 由 scripts/doc_index.py 生成；表格会被重写，手写内容请放进 manual 区块 -->",
        "",
        f"共 {len(files)} 篇。",
        "",
        "| 文档 | 说明（取首个标题） | 行数 | 最后修改 |",
        "| --- | --- | --- | --- |",
    ]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            rows = len(text.splitlines())
        except OSError:
            rows = 0
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        title = first_heading(path).replace("|", "\\|")
        lines.append(f"| [{path.name}](./{path.name}) | {title} | {rows} | {mtime} |")
    lines += ["", manual_block(existing), ""]
    return "\n".join(lines)


def main() -> int:
    check = "--check" in sys.argv[1:]
    if not DOCS.is_dir():
        print(f"找不到 {DOCS}", file=sys.stderr)
        return 1
    existing = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    fresh = render(existing)

    if check:
        if fresh != existing:
            print("docs/README.md 已过期：重跑 .venv/bin/python scripts/doc_index.py")
            return 1
        print("docs/README.md 是最新的")
        return 0

    if fresh == existing:
        print("docs/README.md 无变化，跳过写入")
        return 0
    TARGET.write_text(fresh, encoding="utf-8")
    print(f"已写入 {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

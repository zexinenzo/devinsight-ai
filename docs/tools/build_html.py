"""把 Stage 1 的 Markdown 转成符合 html_rules.md 的完整 HTML。

只处理本项目文档实际用到的语法：h1/h2/h3、段落、无序/有序列表、任务清单、
代码围栏、GFM 表格、引用块（转 callout）、行内 **粗体** 与 `代码`。
"""

from __future__ import annotations

import html
import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "stage1" / "final_draft.md"
OUT = Path(__file__).with_name("formatted-technical-manual.html")

CSS = """
:root {
  /* Typography */
  --fs-h1: 18pt;
  --fs-h2: 15pt;
  --fs-h3: 13pt;
  --fs-body: 12pt;
  --fs-table: 11pt;
  --fs-code: 9.5pt;
  --fs-small: 10pt;
  --fs-cover-title: 30pt;
  --fs-cover-sub: 14pt;
  --ff-heading: "Microsoft YaHei", "微软雅黑", "SimHei", sans-serif;
  --ff-body: "Microsoft YaHei", "微软雅黑", "SimSun", serif;
  --ff-mono: "Consolas", "Courier New", monospace;
  --lh-body: 1.75;
  --lh-heading: 1.35;
  --lh-code: 1.5;
  --fw-bold: 700;
  --fw-normal: 400;
  /* Color */
  --color-primary: #1F4E79;
  --color-text: #1F2328;
  --color-muted: #5B6472;
  --color-border: #D8DEE6;
  --color-bg: #FFFFFF;
  --color-code-bg: #F6F8FA;
  --color-table-head: #EEF2F7;
  --color-callout-bg: #FFF7E6;
  --color-callout-border: #D97706;
  --color-quote-border: #B9C4D2;
  --color-code-inline: #B03A2E;
  --color-code-text: #24292F;
  /* Spacing */
  --spacing-paragraph: 0.55em;
  --spacing-section: 1.8em;
  --spacing-block: 0.9em;
  --page-content-width: 15.6cm;
  --margin-page: 2.5cm;
}

@page {
  margin: var(--margin-page);
  @bottom-center { content: counter(page); }
}
@page cover { @bottom-center { content: none; } }
section[role="cover"] { page: cover; }

body {
  max-width: var(--page-content-width);
  margin-left: auto;
  margin-right: auto;
  font-family: var(--ff-body);
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  color: var(--color-text);
  background: var(--color-bg);
}

h1, h2, h3 {
  font-family: var(--ff-heading);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  line-height: var(--lh-heading);
}
h1 {
  font-size: var(--fs-h1);
  margin-top: var(--spacing-section);
  margin-bottom: var(--spacing-block);
  padding-bottom: 0.25em;
  border-bottom: 1.5pt solid var(--color-primary);
  text-align: left;
  text-indent: 0;
}
h2 {
  font-size: var(--fs-h2);
  margin-top: 1.4em;
  margin-bottom: 0.5em;
  text-align: left;
  text-indent: 0;
}
h3 {
  font-size: var(--fs-h3);
  margin-top: 1.1em;
  margin-bottom: 0.4em;
  color: var(--color-text);
  text-align: left;
  text-indent: 0;
}

p.body-text {
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  margin: var(--spacing-paragraph) 0;
  text-align: justify;
  text-indent: 2em;
}
p.no-indent {
  font-size: var(--fs-body);
  margin: var(--spacing-paragraph) 0;
  text-align: left;
  text-indent: 0;
}
li {
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  margin: 0.25em 0;
  text-align: left;
  text-indent: 0;
}
ul, ol { margin: 0.4em 0 0.6em 0; padding-left: 1.8em; }

code.inline {
  font-family: var(--ff-mono);
  font-size: var(--fs-code);
  background: var(--color-code-bg);
  padding: 0 2pt;
  color: var(--color-code-inline);
}

pre.code-block {
  font-family: var(--ff-mono);
  font-size: var(--fs-code);
  line-height: var(--lh-code);
  background: var(--color-code-bg);
  border: 0.5pt solid var(--color-border);
  padding: 7pt 9pt;
  margin: 0.5em 0 0.7em 0;
  white-space: pre-wrap;
  word-break: break-all;
  text-indent: 0;
  text-align: left;
  color: var(--color-code-text);
}

table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.6em 0 0.9em 0;
  font-size: var(--fs-table);
}
table caption {
  font-size: var(--fs-small);
  color: var(--color-muted);
  text-align: left;
  padding-bottom: 3pt;
}
th {
  background: var(--color-table-head);
  color: var(--color-text);
  font-family: var(--ff-heading);
  font-weight: var(--fw-bold);
  font-size: var(--fs-table);
  border: 0.5pt solid var(--color-border);
  padding: 4pt 6pt;
  text-align: left;
  text-indent: 0;
}
td {
  border: 0.5pt solid var(--color-border);
  padding: 4pt 6pt;
  vertical-align: top;
  font-size: var(--fs-table);
  line-height: 1.6;
  text-align: left;
  text-indent: 0;
}

p.callout {
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  border-left: 3pt solid var(--color-callout-border);
  background: var(--color-callout-bg);
  padding: 6pt 9pt;
  margin: 0.7em 0;
  text-align: left;
  text-indent: 0;
}
p.quote {
  font-size: var(--fs-body);
  border-left: 2pt solid var(--color-quote-border);
  padding: 2pt 0 2pt 9pt;
  margin: 0.6em 0;
  color: var(--color-muted);
  text-align: left;
  text-indent: 0;
}

/* 封面 */
section[role="cover"] > p.cover-title {
  font-family: var(--ff-heading);
  font-size: var(--fs-cover-title);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  line-height: 1.35;
  margin-top: 5.5cm;
  margin-bottom: 0.5em;
  text-align: center;
  text-indent: 0;
}
section[role="cover"] > p.cover-sub {
  font-family: var(--ff-heading);
  font-size: var(--fs-cover-sub);
  color: var(--color-muted);
  margin: 0 0 2.2em 0;
  text-align: center;
  text-indent: 0;
}
section[role="cover"] > table.cover-meta {
  width: 11cm;
  margin: 0 auto;
  font-size: var(--fs-table);
}
section[role="cover"] td.cover-key {
  border: none;
  border-bottom: 0.5pt solid var(--color-border);
  color: var(--color-muted);
  width: 3.2cm;
  padding: 5pt 4pt;
  text-align: left;
  text-indent: 0;
}
section[role="cover"] td.cover-val {
  border: none;
  border-bottom: 0.5pt solid var(--color-border);
  padding: 5pt 4pt;
  text-align: left;
  text-indent: 0;
}

/* 目录 */
nav.doc-toc { margin-top: 1.2cm; }
p.toc-title {
  font-family: var(--ff-heading);
  font-size: var(--fs-h1);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  text-align: center;
  margin-bottom: 1.2em;
  text-indent: 0;
}
.toc-list, .toc-list ol, .toc-list ul { list-style: none; padding-left: 0; margin: 0; }
.toc-list > li {
  font-size: var(--fs-body);
  font-weight: var(--fw-bold);
  margin-top: 0.5em;
  text-indent: 0;
}
.toc-list ol > li {
  font-size: var(--fs-small);
  font-weight: var(--fw-normal);
  color: var(--color-muted);
  margin: 0.15em 0 0.15em 1.6em;
  text-indent: 0;
}
"""


def inline(text: str) -> str:
    """行内格式：先转义，再处理 **粗体** 与 `代码`。"""

    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+?)`", r'<code class="inline">\1</code>', out)
    return out


def is_table_line(line: str) -> bool:
    return line.startswith("|") and line.count("|") >= 2


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_blocks(lines: list[str]) -> list[tuple[str, object]]:
    blocks: list[tuple[str, object]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", (lang, "\n".join(buf))))
            continue

        if not stripped:
            i += 1
            continue

        if stripped == "---":
            i += 1
            continue

        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            blocks.append((f"h{len(m.group(1))}", m.group(2)))
            i += 1
            continue

        if is_table_line(stripped):
            rows = []
            while i < len(lines) and is_table_line(lines[i].strip()):
                rows.append(split_row(lines[i].strip()))
                i += 1
            header = rows[0]
            body = [r for r in rows[1:] if not all(set(c) <= set("-: ") for c in r)]
            blocks.append(("table", (header, body)))
            continue

        if stripped.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            blocks.append(("quote", " ".join(x for x in buf if x)))
            continue

        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(re.sub(r"^[-*]\s+", "", lines[i].strip()))
                i += 1
            blocks.append(("ul", items))
            continue

        if re.match(r"^\d+\.\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i].strip()):
                items.append(re.sub(r"^\d+\.\s+", "", lines[i].strip()))
                i += 1
            blocks.append(("ol", items))
            continue

        # 普通段落：连续非空行合并
        buf = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith("#")
                or nxt.startswith("```")
                or nxt.startswith(">")
                or nxt == "---"
                or is_table_line(nxt)
                or re.match(r"^[-*]\s+", nxt)
                or re.match(r"^\d+\.\s+", nxt)
            ):
                break
            buf.append(nxt)
            i += 1
        blocks.append(("p", " ".join(buf)))

    return blocks


in_code = False


def render(blocks, toc, cover_title):
    out: list[str] = []
    section_no = 0
    h1_open = False
    current_item = None  # 当前 h1 在 toc 里的条目

    def close_section_list():
        nonlocal h1_open
        if h1_open:
            out.append("</div>")
            h1_open = False

    for kind, payload in blocks:
        if kind == "h1":
            close_section_list()
            section_no += 1
            anchor = f"section-{section_no}"
            title = payload
            toc.append((anchor, title))
            current_item = anchor
            out.append(f'<h1 id="{anchor}">{inline(title)}</h1>')
            out.append('<div class="chapter-body">')
            h1_open = True
        elif kind == "h2":
            out.append(f'<h2>{inline(payload)}</h2>')
            toc.append(("h2", payload))
        elif kind == "h3":
            out.append(f'<h3>{inline(payload)}</h3>')
        elif kind == "p":
            out.append(f'<p class="body-text">{inline(payload)}</p>')
        elif kind == "quote":
            text = payload
            if text.startswith("⚠️") or "注意" in text[:6]:
                out.append(f'<p class="callout">&nbsp;&nbsp;{inline(text)}</p>')
            else:
                out.append(f'<p class="quote">{inline(text)}</p>')
        elif kind == "code":
            lang, code = payload
            out.append(f'<pre class="code-block">{html.escape(code, quote=False)}</pre>')
        elif kind == "ul":
            out.append("<ul>")
            for item in payload:
                if item.startswith("[ ] "):
                    out.append(f"<li>☐ {inline(item[4:])}</li>")
                else:
                    out.append(f"<li>{inline(item)}</li>")
            out.append("</ul>")
        elif kind == "ol":
            out.append("<ol>")
            for item in payload:
                out.append(f"<li>{inline(item)}</li>")
            out.append("</ol>")
        elif kind == "table":
            header, body = payload
            out.append("<table>")
            out.append("<thead><tr>")
            for cell in header:
                out.append(f"<th>{inline(cell)}</th>")
            out.append("</tr></thead>")
            out.append("<tbody>")
            for row in body:
                out.append("<tr>")
                for cell in row:
                    out.append(f"<td>{inline(cell)}</td>")
                out.append("</tr>")
            out.append("</tbody>")
            out.append("</table>")

    close_section_list()
    return out


def build_toc(toc):
    items: list[str] = []
    i = 0
    while i < len(toc):
        anchor, title = toc[i]
        if anchor == "h2":
            i += 1
            continue
        subs = []
        j = i + 1
        while j < len(toc) and toc[j][0] == "h2":
            subs.append(toc[j][1])
            j += 1
        if subs:
            items.append(f'<li><a href="#{anchor}">{inline(title)}</a>')
            items.append("<ol>")
            for sub in subs:
                items.append(f"<li>{inline(sub)}</li>")
            items.append("</ol>")
            items.append("</li>")
        else:
            items.append(f'<li><a href="#{anchor}">{inline(title)}</a></li>')
        i = j if subs else i + 1
    return "\n".join(items)


def main():
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines()

    # 文档标题（第一行 # ...）用于封面
    cover_title = "从零搭建多源情报采集系统"
    blocks = parse_blocks(lines[1:] if lines[0].startswith("# ") else lines)

    # 第一个 h1 之前的内容（TL;DR 等）归入「阅读指南」
    first_h1 = next((n for n, (k, _) in enumerate(blocks) if k == "h1"), None)
    intro = blocks[:first_h1] if first_h1 is not None else []
    rest = blocks[first_h1:] if first_h1 is not None else blocks

    toc: list[tuple[str, str]] = []
    body = []
    body.append('<h1 id="section-0">阅读指南</h1>')
    body.append('<div class="chapter-body">')
    for kind, payload in intro:
        if kind == "h2":
            body.append(f"<h2>{inline(payload)}</h2>")
            continue
        tmp = render([(kind, payload)], [], cover_title)
        body.extend(tmp)
    body.append("</div>")
    body.extend(render(rest, toc, cover_title))

    cover = f"""<section role="cover">
<p class="cover-title">{html.escape(cover_title)}</p>
<p class="cover-sub">按开发阶段拆解的实战教程 · 从工程骨架到第一条真实数据闭环</p>
<table class="cover-meta">
<tbody>
<tr><td class="cover-key">文档类型</td><td class="cover-val">技术教程 / 工程实践</td></tr>
<tr><td class="cover-key">适用阶段</td><td class="cover-val">工程手册阶段 0–2</td></tr>
<tr><td class="cover-key">目标读者</td><td class="cover-val">有 Python 基础、未独立搭过数据管线的开发者</td></tr>
<tr><td class="cover-key">技术栈版本</td><td class="cover-val">Next.js 16.4.6 / FastAPI 0.163.2 / PostgreSQL 16 / SQLAlchemy 2.1.9</td></tr>
<tr><td class="cover-key">更新日期</td><td class="cover-val">2026 年 11 月 18 日</td></tr>
</tbody>
</table>
</section>"""

    toc_html = f"""<section role="cover">
<nav class="doc-toc" aria-label="文档目录">
<p class="toc-title">目录</p>
<ol class="toc-list">
{build_toc(toc)}
</ol>
</nav>
</section>"""

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(cover_title)}</title>
<meta name="docx-page-size" content="A4">
<style>{CSS}</style>
</head>
<body>
{cover}
{toc_html}
<section role="body" data-page-restart="1">
{chr(10).join(body)}
<p class="no-indent">以上代码与结论均来自可运行的仓库实现；若文中描述与代码不符，以仓库当前状态为准。</p>
</section>
</body>
</html>
"""
    OUT.write_text(doc, encoding="utf-8")
    print(f"已生成 {OUT.name}：{len(doc)} 字符，目录条目 {len(toc)} 个")


if __name__ == "__main__":
    main()

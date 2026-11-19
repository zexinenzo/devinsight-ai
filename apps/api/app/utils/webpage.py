"""从网页 HTML 里抽正文。

为什么自己写而不是装 readability：项目只要「一段干净的正文」，不需要完整还原排版。
readability 类的库要处理评分、分类、序列化，依赖重、行为不透明；而这里的目标是
给 SimHash 和摘要提供输入，抽多抽少都能用，抽错才是问题。

算法只有两步，都是为了让「正文」比「导航」得分高：

1. 删噪：脚本、样式、导航、页头页脚、侧栏、表单、按钮——这些标签里的文字
   在任何页面上都存在，会把指纹拉到「所有页面都相似」。
2. 段落密度：每个 `<p>` 的文本长度累加到它的所有祖先上，得分最高的祖先就是正文容器。
   这一步能自动绕过「正文外面套了五层 div」的站点，不需要为每个域名写选择器。

第 2 步只用 `>= 25 字`的段落参与打分。太短的 `<p>`（版权声明、"加载中"）会把
侧栏和页脚顶上来——它们的段落数量多但每段都很短。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

NOISE_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "form",
    "nav",
    "header",
    "footer",
    "aside",
    "button",
    "svg",
    "figure",
)

# 参与段落打分的段落最短长度；低于它的段落是噪音（版权行、按钮文案、"相关阅读"）
MIN_PARAGRAPH_CHARS = 25
# 抽出来的正文短于这个长度就认为没抽到，避免把登录墙和错误页当成正文存下来。
# 中文快讯常见 100 字上下，门槛定高了会把真实短讯判成 empty，之后不再重试。
MIN_CONTENT_CHARS = 100

_BLANK_LINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t\u00a0]+")


def _clean_block(text: str) -> str:
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def extract_main_text(html: str) -> str | None:
    """返回正文纯文本；抽不到足够内容时返回 None。

    返回 None 是一个明确的业务结论（「这个页面没有可用的正文」），不是异常。
    调用方据此把 content_status 记成 `empty` 而不是 `failed`——前者重试没有意义。
    """

    if not html or not html.strip():
        return None

    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()

    # 语义标签是最可靠的一层：站点作者已经告诉我们哪里是正文
    for selector in ("article", "main", "[role='main']", "[itemprop='articleBody']"):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = _clean_block(node.get_text("\n", strip=True))
        if len(text) >= MIN_CONTENT_CHARS:
            return text

    # 没有语义标签时，用段落密度找容器
    scores: dict[int, list] = {}
    for paragraph in soup.find_all("p"):
        text = paragraph.get_text(" ", strip=True)
        if len(text) < MIN_PARAGRAPH_CHARS:
            continue
        for parent in paragraph.parents:
            if parent.name in ("html", "body", "[document]"):
                break
            entry = scores.setdefault(id(parent), [parent, 0])
            entry[1] += len(text)

    if scores:
        best_node, _score = max(scores.values(), key=lambda item: item[1])
        text = _clean_block(best_node.get_text("\n", strip=True))
        if len(text) >= MIN_CONTENT_CHARS:
            return text

    # 最后兜底：整页文字。宁可多一些噪点，也比「抓到了但什么都没存」强，
    # 因为判重对这种文本仍有一定判别力（噪点在各页面是相似的，正文不是）
    text = _clean_block(soup.get_text("\n", strip=True))
    return text if len(text) >= MIN_CONTENT_CHARS else None


# 描述天然比正文短，门槛必须单独定：用正文的 100 字去卡 meta，会全部落空
MIN_META_CHARS = 40


def extract_meta_description(html: str) -> str | None:
    """正文抽不出来时，退回页面自带的描述。

    36氪这类站点是前端渲染的单页应用：服务端返回的 HTML 只有骨架，正文要执行 JS
    才有。抓取层永远拿不到全文，但 `<head>` 里通常有 og:description 或 description
    （编辑写的导语），是这种情况下唯一可用的内容。

    返回值的地位比正文低，调用方会打上 `content_from_meta` 标记——用它做判重不可靠
    （同一站点的描述风格接近），用它回答「这条讲什么」够用。
    """

    if not html or not html.strip():
        return None

    soup = BeautifulSoup(html, "lxml")
    for attrs in (
        {"property": "og:description"},
        {"name": "description"},
        {"name": "twitter:description"},
        {"property": "twitter:description"},
    ):
        node = soup.find("meta", attrs=attrs)
        if node is None:
            continue
        content = _clean_block(node.get("content") or "")
        if len(content) >= MIN_META_CHARS:
            return content
    return None

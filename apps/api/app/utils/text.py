"""文本清洗：HTML 去标签、空白归一、长度截断、内容指纹。"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from bs4 import BeautifulSoup

_WHITESPACE = re.compile(r"\s+")


def clean_html(raw: str | None, *, max_chars: int | None = None) -> str | None:
    """把 feed 里的 HTML 摘要转成纯文本（不信任 feed 中的标记）。"""

    if not raw:
        return None
    text = BeautifulSoup(raw, "html.parser").get_text(separator=" ")
    text = unicodedata.normalize("NFKC", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return None
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def normalize_title(title: str) -> str:
    """标题归一，用于指纹与近似去重。"""

    text = unicodedata.normalize("NFKC", title or "").strip().lower()
    return _WHITESPACE.sub("", text)


def content_hash(title: str, canonical_url: str, summary: str | None = None) -> str:
    """内容指纹：标题 + 规范化 URL + 摘要。

    不使用发布时间，避免同一文章因来源修正时间而被判为新文档。
    """

    payload = "\n".join([normalize_title(title), canonical_url, (summary or "").strip()])
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def truncate(text: str | None, max_chars: int) -> str | None:
    if not text:
        return None
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"

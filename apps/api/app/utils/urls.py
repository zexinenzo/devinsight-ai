"""URL 规范化。

去重第一步：同一个 URL 的不同写法（追踪参数、锚点、大小写域名、默认端口）
必须收敛到同一个 canonical_url，否则同一篇文章会被重复入库。
注意：会改变页面内容的参数（如 ?id=123、?p=2）必须保留。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 常见的纯追踪/营销参数，删除后不影响内容
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "spm",
    "scm",
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "msclkid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "referer",
    "from",
    "from_source",
    "share_token",
    "share_source",
    "s_kwcid",
    "_hsenc",
    "_hsmi",
    "igshid",
}

DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonicalize_url(url: str) -> str:
    """返回规范化后的 URL；无法解析时原样返回（去掉首尾空白）。"""

    raw = url.strip()
    if not raw:
        return raw

    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port is not None and str(port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    # 收紧尾斜杠规则：根路径保留 /，其余去掉多余的结尾 /
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    kept_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    kept_pairs.sort()
    query = urlencode(kept_pairs, doseq=True)

    # 丢弃 fragment：单页应用锚点不改变文章身份
    return urlunsplit((scheme, netloc, path, query, ""))


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host

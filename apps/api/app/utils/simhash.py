"""SimHash：判断「同一篇文章被多处转载」的近似指纹。

为什么不用内容哈希：`content_hash` 是精确哈希，改一个标点就完全不同。转载场景里
标题常被改写、正文会被插入站点水印和推荐位，精确哈希必然失效。

SimHash 的性质：文本越相似，指纹的汉明距离越小。64 位指纹下（以下分界是**本项目实测**，
不是教科书引用）：

- 距离 ≤ 8：同一篇稿子被转载（改写标题、加了站点前缀和尾注）
- 距离 10~20：同一事件的两篇独立报道
- 距离 34 以上：无关文章

教科书常写「≤ 3 就是转载」，那来自大规模英文语料的经验。中文正文按 bigram 切分后，
转载改写带来的距离会明显大于 3——本项目实测是 8；守着 3 会把真实转载全部漏掉。

这个分界在不同语料上会浮动，所以阈值不做成常量硬编码在判重逻辑里，而是通过参数传入，
便于用真实数据跑分布后再定。

中文处理的关键：中文没有空格，按字切分再直接喂给 SimHash 效果很差——「的」「是」
这类字在任何文章里都高频，会让所有指纹互相靠近。所以中文按**相邻两字 bigram** 切，
英文按词切并去掉停用词。这是本模块唯一需要针对语种特殊处理的地方。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

BITS = 64

_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")
_CJK_CHAR = re.compile(r"^[\u4e00-\u9fff]$")

# 英文高频虚词。它们在任何文章里都出现，参与加权只会把不同文章的指纹拉近。
_STOP_WORDS = frozenset(
    "a an the of to and or in on for with is are was were be been being it its this that these those "
    "as at by from not but if then than so such can will would should could may might have has had do does did".split()
)

# 少于该 token 数的"正文"多半是导航条、错误页或占位符，算出来的指纹没有判别力
MIN_TOKENS = 20


def tokenize(text: str) -> list[str]:
    """切分成 SimHash 的输入 token：中文 bigram + 英文词。"""

    raw = _TOKEN.findall((text or "").lower())
    tokens: list[str] = []
    cjk_run: list[str] = []

    def flush_cjk() -> None:
        for index in range(len(cjk_run) - 1):
            tokens.append(cjk_run[index] + cjk_run[index + 1])
        if len(cjk_run) == 1:
            # 单个汉字的孤立片段（比如标题里的一个词）保留，否则会整段丢失
            tokens.append(cjk_run[0])
        cjk_run.clear()

    for token in raw:
        if _CJK_CHAR.match(token):
            cjk_run.append(token)
            continue
        flush_cjk()
        if len(token) > 1 and token not in _STOP_WORDS:
            tokens.append(token)
    flush_cjk()
    return tokens


def _hash64(token: str) -> int:
    """把 token 映射到 64 位。用 blake2b 而不是内置 hash()：
    内置 hash 对 str 加了随机盐，进程重启后指纹全变，存进数据库就废了。"""

    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash(text: str, *, max_tokens: int = 4000) -> str | None:
    """计算 16 位十六进制指纹；文本过短时返回 None。

    `max_tokens` 截断是为了控制成本：一篇文章前几千个 token 已经足够表征，
    全文参与计算不会让判别更准，只会让批量判重变慢。
    """

    tokens = tokenize(text)
    if len(tokens) < MIN_TOKENS:
        return None

    counts = Counter(tokens[:max_tokens])
    vector = [0] * BITS
    for token, weight in counts.items():
        hashed = _hash64(token)
        for bit in range(BITS):
            vector[bit] += weight if (hashed >> bit) & 1 else -weight

    value = 0
    for bit in range(BITS):
        if vector[bit] > 0:
            value |= 1 << bit
    return f"{value:016x}"


def hamming(a: str | None, b: str | None) -> int:
    """两个指纹的汉明距离；任一为空时返回 BITS（视为完全不同）。"""

    if not a or not b:
        return BITS
    return (int(a, 16) ^ int(b, 16)).bit_count()

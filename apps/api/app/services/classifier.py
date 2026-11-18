"""主题分类（规则版）。

阶段 1 只做确定性关键词匹配，不调用 LLM：
成本为零、结果可复现、可单测。后续阶段再引入模型分类与人工校正。
"""

from __future__ import annotations

import re

# 六大业务领域（手册 1.2），slug 保持英文，便于 API 过滤与前端 i18n
TOPIC_LABELS: dict[str, str] = {
    "ai-models": "AI 与大模型",
    "software-engineering": "互联网与软件技术",
    "startups-funding": "科技公司与创业融资",
    "industry-trends": "科技产业与商业趋势",
    "open-source": "开源项目与开发者生态",
    "policy-macro": "政策与宏观经济",
}

# 关键词区分中英文；英文按单词边界匹配，避免 "ai" 命中 "said"
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ai-models": (
        "大模型", "模型", "人工智能", "智能体", "多模态", "推理", "微调", "训练", "算力", "芯片",
        "openai", "gpt", "claude", "gemini", "llama", "deepmind", "anthropic", "llm", "agent",
        "transformer", "diffusion", "benchmark", "inference", "fine-tun", "multimodal",
    ),
    "software-engineering": (
        "架构", "框架", "数据库", "微服务", "容器", "前端", "后端", "接口", "性能优化", "重构",
        "kubernetes", "docker", "react", "next.js", "rust", "golang", "python", "postgres",
        "redis", "api", "sdk", "observability", "devops", "cloud",
    ),
    "startups-funding": (
        "融资", "天使轮", "a轮", "b轮", "c轮", "估值", "并购", "收购", "上市", "ipo", "创始人",
        "funding", "series a", "series b", "valuation", "acquire", "acquisition", "vc", "startup",
    ),
    "industry-trends": (
        "市场", "竞争格局", "商业化", "供应链", "增长", "份额", "生态", "战略", "定价",
        "market", "competition", "revenue", "business model", "partnership", "launch",
    ),
    "open-source": (
        "开源", "社区", "贡献者", "许可证", "star", "fork", "pull request",
        "github", "hugging face", "huggingface", "apache", "mit license", "maintainer", "release",
    ),
    "policy-macro": (
        "政策", "监管", "法规", "标准", "规划", "十四五", "补贴", "合规", "备案", "央行", "统计",
        "regulation", "policy", "compliance", "antitrust", "tariff", "gdp", "inflation", "sec",
    ),
}

_ASCII_WORD = re.compile(r"^[a-z0-9.\- ]+$")


def _hit(text_lower: str, keyword: str) -> bool:
    if _ASCII_WORD.match(keyword) and keyword.isascii():
        # 英文关键词按边界匹配；结尾带连字符的（如 fine-tun）用前缀匹配
        if keyword.endswith("-"):
            return re.search(rf"\b{re.escape(keyword)}", text_lower) is not None
        return re.search(rf"\b{re.escape(keyword)}\b", text_lower) is not None
    return keyword in text_lower


def classify_topics(title: str | None, summary: str | None = None, *, limit: int = 3) -> list[str]:
    """返回按相关度降序的领域 slug，标题命中权重高于摘要。"""

    title_text = (title or "").lower()
    summary_text = (summary or "").lower()
    if not title_text and not summary_text:
        return []

    scores: dict[str, int] = {}
    for topic, keywords in _KEYWORDS.items():
        score = 0
        for keyword in keywords:
            if _hit(title_text, keyword):
                score += 3
            elif _hit(summary_text, keyword):
                score += 1
        if score:
            scores[topic] = score

    if not scores:
        return []

    ordered = sorted(scores.items(), key=lambda item: (-item[1], list(TOPIC_LABELS).index(item[0])))
    return [topic for topic, _ in ordered[:limit]]

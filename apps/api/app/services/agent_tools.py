"""Agent 的工具层：把已有的检索与证据能力包成模型可调用的函数。

三条设计约束，都是为了让「模型出错」变成可观测、可恢复的：

1. **参数校验在入口做**。模型会编参数（`limit: "十条"`、编造不存在的字段、
   给出截断的 JSON）。必须在调用前拦下并回一条明确的错误，模型才有机会改。
   放任它进到工具内部，表现就是一个与参数无关的异常，看不出该改什么。

2. **工具失败返回 ok=False，不抛异常**。抛出去等于终止整轮，模型永远学不到
   「这个查询没结果」——而它换个关键词再试本来是可以的。只有「没配 provider」
   这种确定性缺失才值得一路抛到 HTTP 层变 503。

3. **证据统一进 EvidencePool 拿全局编号**。答案里的 `[3]` 必须能唯一定位到
   一条证据，否则引用覆盖率只是一段正则，不是质量指标。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.services.evidence_service import Evidence, collect_evidence
from app.services.web_search_service import WebSearchUnavailable
from app.utils.urls import canonicalize_url

logger = get_logger(__name__)

# 给模型的摘要长度。不是随手定的：免费档常见 TPM 50,000，一次工具结果
# 6 条 × 200 字 ≈ 1,800 token，两三次调用后加上历史仍在安全区内。
# 截到 400 字会让两轮工具调用就吃掉一半预算。
SNIPPET_CHARS = 200
# 整个会话的证据上限。超出的部分不编号、不给模型——它们会占满上下文，
# 却不会进入答案。
#
# 16 试过，太小：一次 `limit=10` 的检索就占掉大半，后面几轮查到的证据全被丢，
# 而模型只看到「命中 2 条」，不知道自己错过了什么。20 条约 6000 字 ≈ 4000 token，
# 在 DeepSeek V4.1 的上下文里可以忽略，但能保证每次检索至少还有空位。
MAX_EVIDENCE_ITEMS = 20

# Agent 场景的精排候选数。列表页排 40 条，实测要 5.8 秒（Windows CPU、
# 每条 passage 接近 512 token 上限）；排 12 条约 1.8 秒。一次研究要调好几次
# 检索，40 条的话光精排就能吃掉二十秒，而它换来的只是候选池里 1.5 个百分点的
# R@5 提升（0.805 → 0.820，见阶段 5 评测）。12 条候选里取前 6，冗余仍是 2 倍，
# 该进前几名的还是进得来。
AGENT_RERANK_CANDIDATES = 12


@dataclass(slots=True)
class EvidenceItem:
    """给模型看的一条证据。`index` 就是答案里 `[n]` 的那个 n。"""

    index: int
    channel: str
    title: str
    url: str
    snippet: str
    source_name: str
    tier: str
    published_at: datetime | None
    date_verified: bool
    freshness: str
    in_local: bool
    score: float | None


class EvidencePool:
    """跨工具共享的证据编号表。

    同一 URL 只占一个编号：模型先 `search_local` 再 `search_web` 时，两路很可能
    命中同一篇文章。如果各编一个号，答案里会出现 `[2]` 和 `[7]` 指向同一个链接，
    看起来像两个独立来源——这正是「独立声源数」最容易虚高的地方。
    """

    def __init__(self, max_items: int = MAX_EVIDENCE_ITEMS) -> None:
        self._items: list[EvidenceItem] = []
        self._by_url: dict[str, int] = {}
        self.max_items = max_items
        self.reused = 0
        self.dropped = 0

    def add(self, evidence: Iterable[Evidence]) -> list[EvidenceItem]:
        """加入一批证据，返回它们对应的条目（含此前已存在的）。"""

        out: list[EvidenceItem] = []
        for item in evidence:
            key = canonicalize_url(item.url) or item.url
            if key in self._by_url:
                self.reused += 1
                out.append(self._items[self._by_url[key] - 1])
                continue
            if len(self._items) >= self.max_items:
                self.dropped += 1
                continue
            entry = EvidenceItem(
                index=len(self._items) + 1,
                channel=item.channel,
                title=item.title,
                url=item.url,
                snippet=item.snippet or "",
                source_name=item.source_name,
                tier=item.source_tier,
                published_at=item.published_at,
                date_verified=item.date_verified,
                freshness=item.freshness,
                in_local=item.in_local,
                score=item.score,
            )
            self._items.append(entry)
            self._by_url[key] = entry.index
            out.append(entry)
        return out

    @property
    def items(self) -> list[EvidenceItem]:
        return list(self._items)

    def by_index(self) -> dict[int, EvidenceItem]:
        return {item.index: item for item in self._items}


def _clip(text: str, limit: int = SNIPPET_CHARS) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _when(item: EvidenceItem) -> str:
    if item.published_at is None:
        # 阶段 5 的教训：日期缺失不等于「旧」，而是「不知道多旧」。
        # 这里必须显式说出来，否则模型会默认把结果当成新的。
        return "日期未知（上游未提供，不能当作近期）"
    return (
        f"{item.published_at:%Y-%m-%d}（{item.freshness}，"
        f"日期{'已确认' if item.date_verified else '来自上游未校验'}）"
    )


def render(items: Iterable[EvidenceItem]) -> str:
    """把证据渲染成模型能读的紧凑文本。"""

    blocks: list[str] = []
    for item in items:
        flags = [f"tier {item.tier}"]
        if item.in_local:
            flags.append("本地库已有")
        blocks.append(
            f"[{item.index}] {item.title}\n"
            f"    来源：{item.source_name}（{'，'.join(flags)}）\n"
            f"    时间：{_when(item)}\n"
            f"    摘要：{_clip(item.snippet)}\n"
            f"    URL：{item.url}"
        )
    return "\n".join(blocks)


# --- 参数校验 ---

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def validate_arguments(
    schema: dict[str, Any], raw: str
) -> tuple[dict[str, Any] | None, str | None]:
    """返回 (干净的参数, 错误说明)。

    手写而不是引 jsonschema：我们只有 4 种基本类型、没有嵌套，
    引一个库换来的是一层额外的依赖和一堆与业务无关的报错文案。
    """

    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        return None, (
            f"参数不是合法 JSON（{error.msg}，位置 {error.pos}）。"
            '请只输出一个 JSON 对象，例如 {"query": "关键词"}。'
        )

    if not isinstance(parsed, dict):
        return None, "参数必须是 JSON 对象，不能是数组或字符串。"

    properties = schema.get("properties") or {}

    for name in schema.get("required") or []:
        if name not in parsed:
            return None, f"缺少必填参数 `{name}`。"

    for name, value in parsed.items():
        spec = properties.get(name)
        if spec is None:
            # 多余的字段静默丢弃比报错好：模型多给一个 `explanation` 很常见，
            # 为这个中止一轮对话不划算。
            continue
        expected = spec.get("type")
        if expected == "integer" and isinstance(value, bool):
            # bool 是 int 的子类，不单独拦的话 True 会被当成 1 通过
            return None, f"参数 `{name}` 应为整数，收到布尔值。"
        py_type = _JSON_TYPES.get(expected or "")
        if py_type is not None and not isinstance(value, py_type):
            return None, (
                f"参数 `{name}` 应为 {expected}，收到 {type(value).__name__}。"
            )

    return {k: v for k, v in parsed.items() if k in properties}, None


def _clamp(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(number, high))


# --- 工具契约 ---


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    timeout_seconds: float = 20.0


@dataclass(slots=True)
class ToolOutcome:
    ok: bool
    content: str
    meta: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0


ToolHandler = Callable[[AsyncSession, dict[str, Any]], Awaitable[ToolOutcome]]


class ToolRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._handlers:
            raise ValueError(f"工具名重复：{spec.name}")
        self._handlers[spec.name] = (spec, handler)

    @property
    def names(self) -> list[str]:
        return list(self._handlers)

    def openai_specs(self) -> list[dict[str, Any]]:
        """转成 /chat/completions 的 tools 字段。"""

        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec, _ in self._handlers.values()
        ]

    async def run(
        self, name: str, raw_arguments: str, session: AsyncSession
    ) -> ToolOutcome:
        started = time.monotonic()
        entry = self._handlers.get(name)
        if entry is None:
            return ToolOutcome(
                ok=False,
                content=(
                    f"没有名为 `{name}` 的工具。可用工具："
                    f"{'、'.join(self._handlers)}。"
                ),
            )

        spec, handler = entry
        arguments, error = validate_arguments(spec.parameters, raw_arguments)
        if error is not None:
            # 必须记下原始参数：模型给错参数时，报错只有「参数不合法」，
            # 而它到底传了什么（字符串形式的数字？空 query？）只有这里能看到。
            logger.warning(
                "tool_bad_arguments",
                extra={
                    "tool": name,
                    "raw": raw_arguments[:300],
                    "error": error,
                },
            )
            return ToolOutcome(ok=False, content=f"参数不合法：{error}")

        try:
            outcome = await asyncio.wait_for(
                handler(session, arguments or {}),
                timeout=spec.timeout_seconds,
            )
        except TimeoutError:
            return ToolOutcome(
                ok=False,
                content=(
                    f"工具 `{name}` 超时（{spec.timeout_seconds:.0f}s）。"
                    "可以换更具体的关键词，或减少条数再试。"
                ),
            )
        except WebSearchUnavailable as error:
            # 这是确定的能力缺失，不是模型的错，也不该让模型盲试
            return ToolOutcome(ok=False, content=f"实时搜索不可用：{error}")
        except Exception as error:  # noqa: BLE001 - 工具是边界，必须兜住
            logger.warning(
                "tool_failed",
                extra={"tool": name, "error": f"{type(error).__name__}: {error}"},
            )
            # 只把类型和消息给模型，不带 traceback：它读不懂，只会占上下文
            return ToolOutcome(
                ok=False,
                content=f"工具 `{name}` 执行失败：{type(error).__name__}: {error}",
            )

        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "tool_done",
            extra={
                "tool": name,
                "ok": outcome.ok,
                "elapsed_ms": outcome.elapsed_ms,
                **outcome.meta,
            },
        )
        return outcome


# --- 两个工具 ---

SEARCH_LOCAL = ToolSpec(
    name="search_local",
    description=(
        "在本地已入库的情报库中做语义检索（向量 + 关键词混合，带精排）。"
        "适合回答「这件事之前报道过什么」「某个来源怎么说的」。"
        "结果是带编号的证据，写答案时用 [编号] 引用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索词。用名词短语（如「GPT-5 发布」）比整句效果好。",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数，默认 6，上限 10。",
            },
        },
        "required": ["query"],
    },
    timeout_seconds=20.0,
)

SEARCH_WEB = ToolSpec(
    name="search_web",
    description=(
        "在实时网络上搜索最近几天的公开报道，返回带日期与来源分级的证据。"
        "适合回答「今天/本周发生了什么」这类有时效要求的问题。"
        "注意：结果可能重复本地库已有的报道，会标注「本地库已有」。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索词。中英文都可以，建议用具体实体名。",
            },
            "days": {
                "type": "integer",
                "description": "时间窗（天），1–30，默认 7。",
            },
        },
        "required": ["query"],
    },
    timeout_seconds=40.0,
)


def make_search_local(pool: EvidencePool) -> ToolHandler:
    async def handler(
        session: AsyncSession, arguments: dict[str, Any]
    ) -> ToolOutcome:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolOutcome(ok=False, content="query 不能为空。")
        limit = _clamp(arguments.get("limit"), 1, 10, 6)

        # 走 collect_evidence 而不是直接调 list_documents：
        # 本地段的分级、时效分档、命中定位都在那个函数里，
        # 这里再写一遍就会出现两套不一致的实现。
        bundle = await collect_evidence(
            session,
            query,
            include_web=False,
            local_limit=limit,
            rerank_candidates=AGENT_RERANK_CANDIDATES,
        )
        before = pool.dropped
        items = pool.add(bundle.local)
        overflow = pool.dropped - before
        if not items:
            return ToolOutcome(
                ok=False,
                content=(
                    f"本地库没有与「{query}」相关的内容。"
                    "可以换个说法，或用 search_web 查外部。"
                ),
                meta={"hits": 0, "query": query},
            )

        header = (
            f"本地检索「{query}」：{len(items)} 条"
            f"（模式 {bundle.local_mode}，"
            f"{'已精排' if bundle.local_reranked else '未精排'}）"
        )
        if overflow:
            # 不说的话，模型会以为「这个查询只有这么几条」，
            # 从而错误判断「本地库没覆盖」，而实际是被证据池挤掉了。
            header += (
                f"\n注意：另有 {overflow} 条结果因证据池已满（{pool.max_items} 条）"
                "未纳入，不代表本地库只有这些。"
            )
        return ToolOutcome(
            ok=True,
            content=f"{header}\n\n{render(items)}",
            meta={
                "hits": len(items),
                "query": query,
                "overflow": overflow,
                "reused": pool.reused,
            },
        )

    return handler


def make_search_web(pool: EvidencePool) -> ToolHandler:
    async def handler(
        session: AsyncSession, arguments: dict[str, Any]
    ) -> ToolOutcome:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolOutcome(ok=False, content="query 不能为空。")
        days = _clamp(
            arguments.get("days"), 1, 30, settings.web_search_days
        )

        bundle = await collect_evidence(
            session,
            query,
            days=days,
            include_local=False,
            web_limit=settings.web_search_max_results,
        )
        before = pool.dropped
        items = pool.add(bundle.web)
        overflow = pool.dropped - before
        if not items:
            return ToolOutcome(
                ok=False,
                # 把 query 和 days 都回显出来：模型常常以为自己查的是 A，
                # 实际传的是 B，看不到回显就会反复用同一组参数重试。
                content=(
                    f"实时搜索「{query}」（最近 {days} 天）没有结果。"
                    "可以放宽 days 到 30，或换更通用的实体名。"
                ),
                meta={"hits": 0, "query": query, "days": days},
            )

        verification = bundle.verification
        lines = [
            (
                f"实时搜索「{query}」（{bundle.web_provider}，最近 {days} 天）："
                f"{len(items)} 条"
            ),
            (
                f"独立声源 {verification.unique_domains} 个；"
                f"本地库已有 {verification.overlap_with_local} 条；"
                f"无发布日期 {verification.date_unverified} 条"
            ),
        ]
        if verification.date_unverified:
            lines.append(
                "注意：无发布日期的结果不能当作近期事件，引用时要说明这一点。"
            )
        if verification.overlap_with_local and verification.overlap_with_local == len(items):
            # 全部重叠时，这次搜索没有带来任何新信息。不说清楚的话，
            # 模型会以为「实时又确认了一遍」，从而把这个事件说得比实际更确定。
            lines.append(
                "注意：本次实时结果全部已在本地库中，没有带来新信息，"
                "不要据此声称「刚刚发生」。"
            )
        if overflow:
            lines.append(f"另有 {overflow} 条因证据池已满未纳入。")
        return ToolOutcome(
            ok=True,
            content="\n".join(lines) + "\n\n" + render(items),
            meta={
                "hits": len(items),
                "query": query,
                "days": days,
                "overflow": overflow,
                "unique_domains": verification.unique_domains,
                "date_unverified": verification.date_unverified,
                "overlap_with_local": verification.overlap_with_local,
            },
        )

    return handler


def build_default_registry(pool: EvidencePool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(SEARCH_LOCAL, make_search_local(pool))
    registry.register(SEARCH_WEB, make_search_web(pool))
    return registry


__all__ = [
    "SEARCH_LOCAL",
    "SEARCH_WEB",
    "EvidenceItem",
    "EvidencePool",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
    "render",
    "validate_arguments",
]

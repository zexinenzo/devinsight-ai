"""Agent 研究循环：模型决定查什么，我们决定它能花多少。

这个模块只做一件事，但要做对：**在预算之内让模型自己规划；预算用尽时，
让它基于已有证据收尾，而不是直接断线。**

三条不变量，测试围绕它们写：

1. 工具调用数与实时搜索数都不超过配置上限。超了就拒绝执行，并把拒绝理由
   作为 tool 消息回传——模型看到「配额用尽」会停止请求，看到异常不会。
2. 总时长不超过 `max_agent_runtime_seconds`。超时不抛异常，而是把剩余调用全部
   拒绝、禁用工具、再请求一次收尾，让调用方至少拿到部分答案。
3. 答案里每个 `[n]` 都必须对应真实证据。编造的编号单独统计并暴露在 done 事件里，
   **不做静默修正**——改答案就等于掩盖模型的错误。
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.services.agent_tools import (
    EvidenceItem,
    EvidencePool,
    ToolRegistry,
    build_default_registry,
    render,
)
from app.services.llm_service import ChatMessage, ChatResult, require_llm

logger = get_logger(__name__)

# 剩余时间低于这个值就不再发起新工具调用：一次 web 搜索的上限是 40 秒，
# 15 秒可以容纳本地检索（毫秒级）和一次快速失败，容不下完整的实时搜索。
MIN_TOOL_SECONDS = 15.0

SYSTEM_PROMPT = """你是 DevInsight 的情报研究助手。你的任务是**基于给定的证据**回答问题，不要用记忆补事实。

## 工具
- `search_local`：查本地已入库的情报库，覆盖过去几个月的报道。
- `search_web`：查最近几天的公开报道，能拿到发布日期。

一次研究最多调用 12 次工具，其中实时搜索最多 4 次。配额用完后你会收到明确的拒绝消息，那时必须直接作答。

## 引用规则
1. 每个事实性句子后面标注证据编号，例如「OpenAI 发布了 GPT-5 预览版 [2]」。
2. 编号只能来自工具返回的结果，**不能自己编号，也不能引用没出现过的编号**。
3. 多个来源支持同一件事时，把编号一起写上：[1][4]。

## 时效规则
4. 标注「日期未知」的结果，不能写成「最近」「今天」「本周」。只能说「有报道称」，并说明时间未确认。
5. 同一事件有本地与实时两路证据时，优先引用时间更新的那条，并说明本地库是否已收录。

## 输出
6. 中文作答。先给 2–3 句结论，再分点列依据，总长控制在 400 字以内。
7. 证据不足以回答时，直接说缺什么（例如「本地库没有 11 月的中文报道」），不要用推测填补。
8. 证据够了就停止调用工具，直接作答。不要为了「更全面」反复检索。"""

# `[2]` 与 `[1][4]` 都要抓。只匹配 1–2 位：编号上限是 16，
# `[123]` 一定是模型编的，没必要为它放宽正则。
CITATION_RE = re.compile(r"\[(\d{1,2})\]")

EMPTY_ANSWER = "（模型没有给出答案，可能是预算耗尽或上游返回空内容。已知证据见下方列表。）"


@dataclass(slots=True)
class CitationReport:
    cited: list[int] = field(default_factory=list)
    unknown: list[int] = field(default_factory=list)
    evidence_total: int = 0

    @property
    def coverage(self) -> float:
        """被引用到的证据占全部证据的比例。

        分母用「提供过的全部证据」，不用「答案里出现的编号」：后者恒等于 1，
        没有信息量。覆盖率低说明工具给的多条模型根本没用上——要么检索偏了，
        要么证据太长它读不完。
        """

        if self.evidence_total == 0:
            return 0.0
        return len(self.cited) / self.evidence_total


@dataclass(slots=True)
class ResearchEvent:
    event: str
    data: dict[str, Any]


@dataclass(slots=True)
class ResearchResult:
    question: str
    answer: str
    evidence: list[EvidenceItem]
    evidence_text: str
    citations: CitationReport
    model: str
    turns: int = 0
    tool_calls: int = 0
    web_calls: int = 0
    elapsed_ms: int = 0
    # None 表示正常结束；否则是收尾原因：runtime_seconds / turns / empty_answer
    truncated: str | None = None
    tool_log: list[dict[str, Any]] = field(default_factory=list)


def check_citations(answer: str, pool: EvidencePool) -> CitationReport:
    known = set(pool.by_index())
    cited = {int(match) for match in CITATION_RE.findall(answer)}
    return CitationReport(
        cited=sorted(cited & known),
        unknown=sorted(cited - known),
        evidence_total=len(known),
    )


def _evidence_payload(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    """证据的可序列化形式。

    snippet 会一起带上：前端渲染引用卡片需要它，而如果 SSE 只推编号和标题，
    点击展开时还得再发一次请求。一次推全，代价是几十 KB，换来的是流结束后
    前端不再依赖后端。
    """

    return [
        {
            "index": item.index,
            "channel": item.channel,
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
            "source": item.source_name,
            "tier": item.tier,
            "freshness": item.freshness,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "date_verified": item.date_verified,
            "in_local": item.in_local,
        }
        for item in items
    ]


def _tool_message(content: str, tool_call_id: str) -> ChatMessage:
    return ChatMessage(role="tool", content=content, tool_call_id=tool_call_id)


async def stream_research(
    session: AsyncSession,
    question: str,
    *,
    max_turns: int | None = None,
    max_tool_calls: int | None = None,
    max_web_calls: int | None = None,
    max_runtime_seconds: float | None = None,
) -> AsyncIterator[ResearchEvent]:
    """跑一次研究，边跑边发事件。

    一定以 `done` 事件结束（除非被取消），`done.data.result` 是完整结果。
    SSE 端点与脚本共用这一条路径——不存在「接口版」和「脚本版」两套逻辑，
    那是最容易漂移的地方。
    """

    client = require_llm()

    # 必须用 `is None` 而不是 `or`：`or` 会把显式传入的 0 当成「没传」，
    # 于是 `max_runtime_seconds=0`（用于测试「一开始就超时」）会悄悄变成 180 秒。
    turns_limit = settings.max_agent_turns if max_turns is None else max_turns
    calls_limit = (
        settings.max_research_tool_calls if max_tool_calls is None else max_tool_calls
    )
    web_limit = (
        settings.max_web_search_calls if max_web_calls is None else max_web_calls
    )
    runtime_limit = (
        settings.max_agent_runtime_seconds
        if max_runtime_seconds is None
        else max_runtime_seconds
    )

    pool = EvidencePool()
    registry: ToolRegistry = build_default_registry(pool)

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=question),
    ]

    started = time.monotonic()
    deadline = started + runtime_limit
    turns = 0
    tool_calls = 0
    web_calls = 0
    tool_log: list[dict[str, Any]] = []
    answer = ""
    truncated: str | None = None

    def time_left() -> float:
        return deadline - time.monotonic()

    yield ResearchEvent(
        event="started",
        data={
            "question": question,
            "model": getattr(client, "model", "?"),
            "limits": {
                "turns": turns_limit,
                "tool_calls": calls_limit,
                "web_calls": web_limit,
                "runtime_seconds": runtime_limit,
            },
        },
    )

    while turns < turns_limit:
        if time_left() <= 0:
            truncated = "runtime_seconds"
            break

        turns += 1
        # 最后一轮禁用工具：无论前面发生了什么，都必须在这里拿到答案。
        last_turn = turns >= turns_limit
        tools = None if last_turn else registry.openai_specs()

        yield ResearchEvent(
            event="turn_started",
            data={"turn": turns, "tools_enabled": tools is not None},
        )

        result: ChatResult = await client.complete(messages, tools=tools)

        if not result.tool_calls:
            answer = result.content
            break

        messages.append(
            ChatMessage(
                role="assistant",
                content=result.content,
                tool_calls=result.tool_calls,
            )
        )

        for call in result.tool_calls:
            # 每个 tool_call 都必须有一条对应的 tool 消息，否则下一次请求会因为
            # 「assistant 请求了工具却没有结果」被上游拒绝。所以下面每条分支都要
            # append，包括被拒绝的那些。
            refusal: str | None = None
            if last_turn:
                refusal = "本轮已禁用工具，请直接基于现有证据作答。"
            elif tool_calls >= calls_limit:
                refusal = (
                    f"工具调用配额已用尽（{calls_limit} 次），请基于现有证据直接作答。"
                )
            elif call.name == "search_web" and web_calls >= web_limit:
                refusal = (
                    f"实时搜索配额已用尽（{web_limit} 次），"
                    "请改用本地证据作答，或直接说明实时信息缺失。"
                )
            elif time_left() < MIN_TOOL_SECONDS:
                refusal = "剩余时间不足以完成这次检索，请立即基于现有证据作答。"

            if refusal is not None:
                messages.append(_tool_message(refusal, call.id))
                continue

            yield ResearchEvent(
                event="tool_call",
                data={"name": call.name, "arguments": call.arguments},
            )

            outcome = await registry.run(call.name, call.arguments, session)
            tool_calls += 1
            if call.name == "search_web":
                web_calls += 1

            tool_log.append(
                {
                    "name": call.name,
                    "ok": outcome.ok,
                    "elapsed_ms": outcome.elapsed_ms,
                    "meta": outcome.meta,
                    # 失败时 content 就是错误说明。不记下来的话，一个 ok=False
                    # 在日志里只剩「失败了」，而原因（限流？参数错？没配置？）
                    # 恰恰是唯一有用的信息。
                    "content": outcome.content[:400],
                }
            )
            messages.append(_tool_message(outcome.content, call.id))

            yield ResearchEvent(
                event="tool_result",
                data={
                    "name": call.name,
                    "ok": outcome.ok,
                    "elapsed_ms": outcome.elapsed_ms,
                    "meta": outcome.meta,
                    "content": outcome.content[:400],
                    # 推全量而不是增量：同一 URL 只占一个编号，前端按 index 合并即可，
                    # 增量计算错了反而会出现「编号跳号」这种最难查的问题。
                    "evidence": _evidence_payload(pool.items),
                },
            )

        if time_left() <= 0:
            truncated = "runtime_seconds"
            break

    if not answer:
        if truncated is None:
            truncated = "turns"
        yield ResearchEvent(
            event="wrap_up",
            data={"reason": truncated, "evidence_total": len(pool.items)},
        )
        try:
            final = await client.complete(
                [
                    *messages,
                    ChatMessage(
                        role="user",
                        content=(
                            "研究预算已用尽。请只基于上面工具返回的证据直接作答，"
                            "不要再请求工具。证据不足的地方明确说明缺什么。"
                        ),
                    ),
                ],
                tools=None,
            )
            answer = final.content
        except Exception as error:  # noqa: BLE001 - 收尾失败不该让整个请求 500
            # 已经付出的检索是有价值的：答案拿不到，证据照样要返回。
            logger.warning(
                "research_wrapup_failed",
                extra={"error": f"{type(error).__name__}: {error}"},
            )
            answer = ""

    if not answer.strip():
        answer = EMPTY_ANSWER
        if truncated is None:
            truncated = "empty_answer"

    citations = check_citations(answer, pool)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if citations.unknown:
        # 编造引用是硬错误，必须留日志：它意味着模型在「编证据」，
        # 而答案本身读起来完全正常，只有做编号比对才能发现。
        logger.warning(
            "research_invalid_citations",
            extra={
                "unknown": citations.unknown,
                "evidence_total": citations.evidence_total,
            },
        )

    yield ResearchEvent(event="answer", data={"text": answer})

    yield ResearchEvent(
        event="done",
        data={
            "result": {
                "question": question,
                "answer": answer,
                "evidence": _evidence_payload(pool.items),
                "evidence_text": render(pool.items),
                "citations": {
                    "cited": citations.cited,
                    "unknown": citations.unknown,
                    "coverage": round(citations.coverage, 4),
                    "evidence_total": citations.evidence_total,
                },
                "model": getattr(client, "model", "?"),
                "turns": turns,
                "tool_calls": tool_calls,
                "web_calls": web_calls,
                "elapsed_ms": elapsed_ms,
                "truncated": truncated,
                "tool_log": tool_log,
            }
        },
    )


async def run_research(
    session: AsyncSession,
    question: str,
    **options: Any,
) -> ResearchResult:
    """跑完整流程只取结果。CLI 与评测脚本用这个，接口用 `stream_research`。"""

    payload: dict[str, Any] | None = None
    async for event in stream_research(session, question, **options):
        if event.event == "done":
            payload = event.data["result"]

    if payload is None:  # pragma: no cover - 生成器正常结束一定带 done
        raise RuntimeError("研究流程没有产出 done 事件")

    return ResearchResult(
        question=payload["question"],
        answer=payload["answer"],
        evidence=[_item_from_payload(item) for item in payload["evidence"]],
        evidence_text=payload["evidence_text"],
        citations=CitationReport(
            cited=payload["citations"]["cited"],
            unknown=payload["citations"]["unknown"],
            evidence_total=payload["citations"]["evidence_total"],
        ),
        model=payload["model"],
        turns=payload["turns"],
        tool_calls=payload["tool_calls"],
        web_calls=payload["web_calls"],
        elapsed_ms=payload["elapsed_ms"],
        truncated=payload["truncated"],
        tool_log=payload["tool_log"],
    )


def _item_from_payload(item: dict[str, Any]) -> EvidenceItem:
    """把 done 事件里的 dict 还原成 dataclass。

    事件里放 dict 而不是对象，是为了让 SSE 能直接 json.dumps——
    带 dataclass 的对象会在序列化那一步才炸，而那时响应头已经发出去了。
    """

    published = item.get("published_at")
    return EvidenceItem(
        index=item["index"],
        channel=item["channel"],
        title=item["title"],
        url=item["url"],
        snippet=item.get("snippet") or "",
        source_name=item["source"],
        tier=item["tier"],
        published_at=datetime.fromisoformat(published) if published else None,
        date_verified=item["date_verified"],
        freshness=item["freshness"],
        in_local=item["in_local"],
        score=None,
    )

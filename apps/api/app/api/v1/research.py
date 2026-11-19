"""Agent 研究端点：流式返回「规划 → 工具调用 → 带引用的答案」。

**为什么是 GET + SSE，而不是 POST 返回一个 JSON。** 一次研究要跑十几秒到几十秒，
调用方需要看到「它现在在查什么」——否则只能对着一片空白等，等到的还是一个
无法判断可信度的字符串。SSE 让每个阶段事件都可见：调到哪个工具、拿到几条证据、
引用覆盖率多少、有没有编造编号。

用 GET 是为了浏览器 `EventSource` 能直接用（它不支持自定义方法）；问题长度
上限 500 字，放 query 里没有实际障碍。

**LLM 是否就绪必须在返回 StreamingResponse 之前检查。** 一旦开始发送流，
HTTP 状态码就固定成 200 了，那时再发现「没配模型」只能发一个 error 事件——
调用方拿到的是 200 + 一段错误文本，和真正的 503 语义差得很远。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.api.deps import SessionDep
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.services.llm_service import LLMFailed, LLMUnavailable, require_llm
from app.services.research_service import ResearchEvent, stream_research

logger = get_logger(__name__)

router = APIRouter(tags=["research"])


def _format(event: ResearchEvent) -> str:
    # ensure_ascii=False：中文原样发出去，前端不用再解一层 \uXXXX。
    # JSON 会把换行转义成 \\n，所以这里不需要处理 SSE 的多行 data 语法。
    data = json.dumps(event.data, ensure_ascii=False)
    return f"event: {event.event}\ndata: {data}\n\n"


async def _sse_stream(session, question: str, **options) -> AsyncIterator[str]:
    try:
        async for event in stream_research(session, question, **options):
            yield _format(event)
    except LLMUnavailable as error:
        yield _format(ResearchEvent("error", {"code": "llm_unavailable", "message": str(error)}))
    except LLMFailed as error:
        yield _format(ResearchEvent("error", {"code": "llm_failed", "message": str(error)}))
    except Exception as error:
        # 这里不把 traceback 发给调用方：它解不了，只会以为是自己用错了。
        # 完整堆栈进日志，事件里只给类型与消息。
        logger.exception("research_stream_failed")
        yield _format(
            ResearchEvent(
                "error",
                {
                    "code": "internal_error",
                    "message": f"{type(error).__name__}: {error}",
                },
            )
        )


@router.get("/research", summary="Agent 研究：流式返回规划、工具调用与带引用的答案")
async def research(
    session: SessionDep,
    q: str = Query(
        ...,
        min_length=1,
        max_length=500,
        description="研究问题。陈述句或疑问句都行，越具体越好",
    ),
    max_turns: int | None = Query(
        None, ge=1, le=12, description="最多几轮「模型 → 工具 → 模型」，默认取配置"
    ),
    max_tool_calls: int | None = Query(
        None, ge=1, le=30, description="工具调用总次数上限，默认取配置"
    ),
    max_web_calls: int | None = Query(
        None, ge=0, le=10, description="实时搜索次数上限（花额度的那一个），默认取配置"
    ),
    max_runtime_seconds: float | None = Query(
        None, gt=0, le=600, description="总时长上限（秒），超时会收尾给出部分答案"
    ),
):
    """SSE 事件序列：

    - `started`：问题、模型名、四档预算
    - `turn_started`：第几轮，工具是否可用
    - `tool_call`：工具名 + 原始参数（模型生成的，可能是错的）
    - `tool_result`：是否成功、耗时、累计证据（前端按 index 合并）
    - `wrap_up`：预算用尽，正在强制收尾
    - `answer`：最终答案全文
    - `done`：完整结果（证据、引用校验、轮数、耗时、收尾原因）
    - `error`：`llm_unavailable` / `llm_failed` / `internal_error`
    """

    try:
        # 预检：流一旦开始，状态码就回不去了
        require_llm()
    except LLMUnavailable as error:
        raise AppError(
            str(error), code="llm_unavailable", status_code=503
        ) from error

    options = {
        "max_turns": max_turns,
        "max_tool_calls": max_tool_calls,
        "max_web_calls": max_web_calls,
        "max_runtime_seconds": max_runtime_seconds,
    }
    # 传 None 的项由 stream_research 回落到配置值（它用 is None 判断，
    # 不会把显式的 0 吞掉——见那边的注释）

    return StreamingResponse(
        _sse_stream(session, q, **options),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx 默认会缓冲响应体，SSE 会被攒成一坨再发，
            # 中间事件全部变成「流结束时一起出现」，等于没做流式
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/research/limits", summary="Agent 的预算上限（供前端展示）")
async def research_limits():
    """前端要在发起研究前告诉用户「最多查几次」，所以单独给一个只读端点。

    不放进 `started` 事件里：那个事件要等第一次模型调用之前才发，
    而 UI 需要在用户点击之前就显示这些数字。
    """

    return {
        "data": {
            "max_turns": settings.max_agent_turns,
            "max_tool_calls": settings.max_research_tool_calls,
            "max_web_calls": settings.max_web_search_calls,
            "max_runtime_seconds": settings.max_agent_runtime_seconds,
            "model": settings.llm_model or None,
            "provider": settings.llm_provider or None,
            "ready": bool(settings.llm_api_key and settings.llm_model),
        }
    }

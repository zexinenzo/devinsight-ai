"""LLM 适配层：只实现 OpenAI 兼容的 `/chat/completions`。

手写 httpx 而不装 openai SDK，三个原因：

1. 项目所有外部 HTTP 都走 httpx。多一个 SDK 就多一套重试 / 超时 / 异常语义，
   排查时要同时看两边的日志。
2. SDK 会牵进 httpx2、pydantic 的版本约束，和现有 pin 冲突时的报错很难读。
3. `tool_calls[].function.arguments` 是**模型生成的字符串**，可能是非法 JSON、
   可能缺必填字段。这一段必须由调用方看得见、兜得住；藏在 SDK 里只会让错误
   更晚暴露，而且暴露在工具内部。

只依赖两个东西：`POST {base_url}/chat/completions` 与 `Authorization: Bearer`。
这是 DeepSeek / 智谱 / 通义兼容模式 / 硅基流动 / vLLM 都实现的最小交集。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class LLMUnavailable(Exception):
    """模型没配。这是 503（能力缺失），不是 502（上游挂了）。"""


class LLMFailed(Exception):
    """配了但上游出错：鉴权、限流、超时、返回体不合法。"""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    # 故意保留原始字符串，不在这里 json.loads。
    # 模型偶尔给出截断或带注释的 JSON；解析应该发生在「要执行这个工具」的地方，
    # 那里能把失败变成一条可回报的错误，让模型换个说法重试，
    # 而不是在构造消息时抛异常，把整轮对话打掉。
    arguments: str


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass(slots=True)
class ChatResult:
    content: str
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: dict[str, Any]

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ChatClient(Protocol):
    name: str

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> ChatResult: ...


def message_payload(message: ChatMessage) -> dict[str, Any]:
    """把内部消息转成线上格式。

    `tool` 消息必须带 `tool_call_id`，否则上游会以「找不到对应的工具调用」拒绝——
    这个错在只在多轮里出现，单轮测试测不到。
    """

    if message.role == "tool":
        return {
            "role": "tool",
            "content": message.content or "",
            "tool_call_id": message.tool_call_id or "",
        }

    if message.tool_calls:
        return {
            "role": "assistant",
            # 带 tool_calls 的 assistant 消息，content 允许为空字符串，
            # 但字段不能省：部分兼容实现缺字段会 400。
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ],
        }

    return {"role": message.role, "content": message.content or ""}


def parse_completion(payload: dict[str, Any]) -> ChatResult:
    """解析响应体。抽成纯函数，测试可以直接喂录制下来的真实响应。"""

    choices = payload.get("choices") or []
    if not choices:
        # 上游 200 但没有 choice：不把原文带出来，日志里只会剩一句「空响应」，
        # 而真正的原因（内容被安全策略拦了、model 名写错了）就在这个 body 里。
        raise LLMFailed(
            "模型返回了空 choices："
            f"{json.dumps(payload, ensure_ascii=False)[:300]}"
        )

    choice = choices[0]
    message = choice.get("message") or {}

    content = message.get("content")
    if not isinstance(content, str):
        # 多模态实现会把 content 给成 list[dict]，这里不做拼接，
        # 直接当空处理——本系统的模型只负责写文字答案。
        content = ""

    calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        raw_args = function.get("arguments")
        # 兼容两种实现：多数返回 JSON 字符串，少数自建网关直接返回对象。
        if isinstance(raw_args, str):
            args = raw_args
        elif raw_args is None:
            args = "{}"
        else:
            args = json.dumps(raw_args, ensure_ascii=False)
        calls.append(
            ToolCall(
                # 个别实现不给 id；回传 tool 消息缺 id 会被拒，用序号兜底
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=args,
            )
        )

    return ChatResult(
        content=content.strip(),
        tool_calls=calls,
        finish_reason=str(choice.get("finish_reason") or ""),
        usage=payload.get("usage") or {},
    )


class OpenAICompatibleClient:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        max_tokens: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> ChatResult:
        """发一次**非流式**请求。

        为什么不用流式：流式下 `tool_calls` 的 arguments 是分片下发的，
        要自己累积再拼 JSON。拼错的表现是工具参数缺字段，报错位置却在工具内部，
        排查成本远大于收益。而用户真正想看的「过程」是阶段事件——开始检索、
        调了哪个工具、拿到几条证据——这些我们自己发，不依赖 token 流。
        最终答案一次性返回，SSE 里作为单个 answer 事件推出。
        """

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [message_payload(m) for m in messages],
            "temperature": temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            body["tools"] = tools
            # 固定 auto。智谱免费档不支持 tool_choice="required"，
            # 传了会被忽略（不同实现行为不一致）。而「模型自己决定要不要查」
            # 本来就是 Agent 该有的样子——写死必选只是掩盖了规划能力的缺失。
            body["tool_choice"] = "auto"

        url = f"{self.base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.TimeoutException as error:
            raise LLMFailed(
                f"模型调用超时（{self.timeout:.0f}s）：{url}。"
                "长答案可以把 LLM_TIMEOUT_SECONDS 调大，别超过 MAX_AGENT_RUNTIME_SECONDS。"
            ) from error
        except httpx.HTTPError as error:
            raise LLMFailed(f"模型调用网络错误：{error}") from error

        if response.status_code == 401:
            raise LLMFailed(
                "模型鉴权失败（401）：检查 LLM_API_KEY 是否有效，"
                "以及它是否与 LLM_BASE_URL 属于同一家。"
            )
        if response.status_code == 429:
            raise LLMFailed(
                "模型调用被限流（429）。免费档实测约 RPM 5 / TPM 50,000，"
                "批量评测必须串行并留间隔。"
                f"上游原文：{response.text[:160]}"
            )
        if response.status_code >= 400:
            raise LLMFailed(
                f"模型返回 HTTP {response.status_code}：{response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise LLMFailed(
                f"模型返回了非 JSON 内容：{response.text[:200]}"
            ) from error

        result = parse_completion(payload)
        logger.info(
            "llm_call_done",
            extra={
                "model": self.model,
                "finish_reason": result.finish_reason,
                "tool_calls": len(result.tool_calls),
                "tokens": result.usage.get("total_tokens"),
            },
        )
        return result


# --- 进程内单例：与 embedding / rerank / web_search 同一套装配方式 ---

_lock = threading.Lock()
_client: ChatClient | None = None
# 「没配」是合法且稳定的结果，必须和「还没构建」区分开，
# 否则没配的部署每次请求都要重新进锁、重新解析一遍配置。
_initialized = False


def build_llm() -> ChatClient | None:
    """按配置构造客户端。返回 None 表示模型没启用（不是错误）。"""

    api_key = (settings.llm_api_key or "").strip()
    model = (settings.llm_model or "").strip()

    if not api_key and not model:
        return None
    if not api_key or not model:
        # 只配了一半：这是最常见的配置失误，静默返回 None 会让人以为「没打算用」，
        # 然后在 503 里反复找原因。留一条日志，指向真正的问题。
        logger.warning(
            "llm_partially_configured",
            extra={"has_key": bool(api_key), "has_model": bool(model)},
        )
        return None

    return OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        api_key=api_key,
        model=model,
        timeout=settings.llm_timeout_seconds,
        max_tokens=settings.llm_max_tokens,
    )


def get_llm() -> ChatClient | None:
    global _client, _initialized
    if _initialized:
        return _client
    with _lock:
        if not _initialized:
            _client = build_llm()
            _initialized = True
            if _client is not None:
                logger.info(
                    "llm_ready",
                    extra={"model": getattr(_client, "model", "?")},
                )
    return _client


def set_llm(client: ChatClient | None) -> None:
    """测试注入用。传 None 表示「这轮当作没配」。"""

    global _client, _initialized
    with _lock:
        _client = client
        _initialized = True


def require_llm() -> ChatClient:
    """调用方要的是「一定能用的客户端」，不是「可能为 None」。"""

    client = get_llm()
    if client is None:
        raise LLMUnavailable(
            "Agent 未启用：缺少 LLM_API_KEY 或 LLM_MODEL。\n"
            "  智谱免费档（实测可用）：\n"
            "    LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4\n"
            "    LLM_MODEL=glm-4-flash"
        )
    return client

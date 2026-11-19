"""Agent 层的测试，全部用桩 LLM 与桩工具，不连模型、不连数据库。

守的是四条不变量，而不是「看起来能跑」：
- 参数非法 / 工具异常 / 超时，都必须变成 ok=False 的一条消息，而不是抛异常
- 工具调用与实时搜索的配额必须真的拦住，且拦住之后模型还能拿到答案
- 时间用尽必须收尾，不能把请求挂死
- 答案里编造的编号必须被发现，且不被静默「修正」
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from app.services import research_service
from app.services.agent_tools import (
    EvidencePool,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
    render,
    validate_arguments,
)
from app.services.evidence_service import Evidence
from app.services.llm_service import (
    ChatMessage,
    ChatResult,
    ToolCall,
    message_payload,
    parse_completion,
    set_llm,
)
from app.services.research_service import (
    check_citations,
    stream_research,
)


@pytest.fixture(autouse=True)
def _reset_llm():
    """每个用例结束后清掉全局单例，避免用例之间互相污染。"""

    yield
    set_llm(None)


# --- 参数校验 ---


LOCAL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["query"],
}


def test_validate_rejects_missing_required():
    args, error = validate_arguments(LOCAL_SCHEMA, '{"limit": 3}')
    assert args is None
    # 错误文案要指名缺哪个字段，否则模型改不动
    assert "query" in error


def test_validate_rejects_wrong_type():
    args, error = validate_arguments(LOCAL_SCHEMA, '{"query": 42}')
    assert args is None
    assert "string" in error


def test_validate_rejects_bool_as_integer():
    # bool 是 int 的子类，不单独拦的话 `True` 会被当成 1 悄悄通过
    args, error = validate_arguments(LOCAL_SCHEMA, '{"query": "x", "limit": true}')
    assert args is None
    assert "布尔" in error


def test_validate_drops_unknown_fields():
    # 模型多给字段很常见，为这个中止一轮不划算
    args, error = validate_arguments(
        LOCAL_SCHEMA, '{"query": "x", "explanation": "因为"}'
    )
    assert error is None
    assert args == {"query": "x"}


def test_validate_reports_broken_json_without_raising():
    args, error = validate_arguments(LOCAL_SCHEMA, '{"query": "x"')
    assert args is None
    assert "JSON" in error


# --- 证据池 ---


def _evidence(url: str, title: str = "标题") -> Evidence:
    return Evidence(
        channel="local",
        title=title,
        url=url,
        snippet="摘要",
        published_at=None,
        source_name="测试源",
        source_tier="B",
        score=0.5,
        date_verified=False,
        in_local=True,
    )


def test_pool_reuses_index_for_same_url():
    pool = EvidencePool()
    first = pool.add([_evidence("https://example.com/a?utm_source=x")])
    second = pool.add([_evidence("https://example.com/a")])
    # 同一篇的两种 URL 写法必须共用一个编号，否则它会看起来像两个独立来源
    assert first[0].index == second[0].index == 1
    assert len(pool.items) == 1
    assert pool.reused == 1


def test_pool_caps_total_items():
    pool = EvidencePool(max_items=3)
    items = pool.add([_evidence(f"https://example.com/{i}") for i in range(5)])
    assert len(pool.items) == 3
    assert len(items) == 3
    assert pool.dropped == 2


def test_render_marks_unknown_date_explicitly():
    pool = EvidencePool()
    items = pool.add([_evidence("https://example.com/a")])
    text = render(items)
    # 「日期未知」必须出现在文本里，否则模型会默认把结果当成新的
    assert "日期未知" in text
    assert "[1]" in text


# --- 响应解析 ---


def test_parse_completion_accepts_dict_arguments():
    # 部分自建网关直接把 arguments 给成对象，不兼容会让整轮工具调用失效
    result = parse_completion(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "abc",
                                "function": {"name": "search_local", "arguments": {"query": "x"}},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    assert result.tool_calls[0].arguments == '{"query": "x"}'


def test_parse_completion_falls_back_to_index_id():
    result = parse_completion(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [{"function": {"name": "search_local", "arguments": "{}"}}]
                    }
                }
            ]
        }
    )
    # 缺 id 时回传 tool 消息会被上游拒，所以必须兜一个
    assert result.tool_calls[0].id == "call_0"


def test_parse_completion_surfaces_body_on_empty_choices():
    with pytest.raises(Exception) as excinfo:
        parse_completion({"choices": [], "error": {"message": "model not found"}})
    # 不把 body 带出来，日志里只会剩一句「空响应」，查不到真正原因
    assert "model not found" in str(excinfo.value)


def test_message_payload_tool_role_keeps_call_id():
    payload = message_payload(
        ChatMessage(role="tool", content="结果", tool_call_id="abc")
    )
    assert payload == {"role": "tool", "content": "结果", "tool_call_id": "abc"}


# --- 工具注册表 ---


def _spec(name: str = "search_local", timeout: float = 1.0) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="测试用",
        parameters=LOCAL_SCHEMA,
        timeout_seconds=timeout,
    )


def test_registry_rejects_unknown_tool():
    registry = ToolRegistry()

    async def handler(session, args):
        return ToolOutcome(ok=True, content="x")

    registry.register(_spec(), handler)
    outcome = asyncio.run(registry.run("nope", "{}", None))
    assert outcome.ok is False
    assert "search_local" in outcome.content


def test_registry_reports_bad_arguments_as_outcome():
    registry = ToolRegistry()

    async def handler(session, args):  # pragma: no cover - 不该被调用
        raise AssertionError("参数非法时不应执行工具")

    registry.register(_spec(), handler)
    outcome = asyncio.run(registry.run("search_local", '{"limit": 1}', None))
    assert outcome.ok is False
    assert "query" in outcome.content


def test_registry_swallows_handler_exception():
    registry = ToolRegistry()

    async def handler(session, args):
        raise RuntimeError("数据库断了")

    registry.register(_spec(), handler)
    outcome = asyncio.run(registry.run("search_local", '{"query": "x"}', None))
    assert outcome.ok is False
    # 把类型带出来，模型才知道这是「服务挂了」而不是「没结果」
    assert "RuntimeError" in outcome.content


def test_registry_times_out_slow_handler():
    registry = ToolRegistry()

    async def handler(session, args):
        await asyncio.sleep(0.2)
        return ToolOutcome(ok=True, content="太慢了")

    registry.register(_spec(timeout=0.01), handler)
    outcome = asyncio.run(registry.run("search_local", '{"query": "x"}', None))
    assert outcome.ok is False
    assert "超时" in outcome.content


# --- 编排循环 ---


@dataclass
class _StubLLM:
    """按脚本回应。

    `tools is None` 时一律给答案——对应真实的「最后一轮禁用工具」，
    这时模型没有别的选择，只能作答。
    """

    model: str = "stub"
    answer: str = "结论：有报道称该事件发生 [1]。"
    tool_name: str = "search_local"
    calls: int = 0
    history: list[list[ChatMessage]] = field(default_factory=list)

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> ChatResult:
        self.calls += 1
        self.history.append(list(messages))
        if tools is None:
            return ChatResult(self.answer, [], "stop", {})
        return ChatResult(
            content="",
            tool_calls=[ToolCall(id=f"c{self.calls}", name=self.tool_name, arguments='{"query": "x"}')],
            finish_reason="tool_calls",
            usage={},
        )


@dataclass
class _StubbornLLM:
    """无视 tools=None 仍请求工具的模型（兼容实现里真实存在）。"""

    model: str = "stub"
    calls: int = 0

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> ChatResult:
        self.calls += 1
        return ChatResult(
            content="",
            tool_calls=[ToolCall(id=f"c{self.calls}", name="search_local", arguments='{"query": "x"}')],
            finish_reason="tool_calls",
            usage={},
        )


def _fake_factory(calls: list[str]):
    """造一个注册表工厂：工具不连数据库，只记下被调过几次并塞一条假证据。"""

    def factory(pool: EvidencePool) -> ToolRegistry:
        registry = ToolRegistry()

        def make(spec: ToolSpec):
            async def handler(session, args):
                calls.append(spec.name)
                pool.add(
                    [
                        Evidence(
                            channel="local",
                            title=f"{spec.name} 命中 {len(pool.items)}",
                            url=f"https://example.com/{spec.name}/{len(pool.items)}",
                            snippet="测试摘要",
                            published_at=None,
                            source_name="测试源",
                            source_tier="B",
                            score=0.5,
                            date_verified=False,
                            in_local=True,
                        )
                    ]
                )
                return ToolOutcome(ok=True, content="1 条证据", meta={"hits": 1})

            return handler

        registry.register(_spec("search_local"), make(_spec("search_local")))
        registry.register(_spec("search_web"), make(_spec("search_web")))
        return registry

    return factory


async def _collect(question: str, **options: Any) -> list[Any]:
    return [event async for event in stream_research(None, question, **options)]  # type: ignore[arg-type]


def _run(question: str, **options: Any) -> list[Any]:
    return asyncio.run(_collect(question, **options))


def _result(events: list[Any]) -> dict[str, Any]:
    assert events[-1].event == "done", [e.event for e in events]
    return events[-1].data["result"]


def test_research_stops_at_turn_limit_when_model_never_answers(monkeypatch):
    monkeypatch.setattr(
        research_service, "build_default_registry", _fake_factory([])
    )
    set_llm(_StubbornLLM())

    result = _result(_run("今天发生了什么", max_turns=3))
    # 3 轮结束，且走了收尾而不是挂死
    assert result["turns"] == 3
    assert result["truncated"] == "turns"


def test_research_enforces_tool_call_budget(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        research_service, "build_default_registry", _fake_factory(calls)
    )
    set_llm(_StubLLM())

    result = _result(_run("q", max_turns=5, max_tool_calls=2))
    # 超过配额的请求被拒，但每轮仍有一条 tool 消息回传，对话历史保持完整
    assert result["tool_calls"] == 2
    assert len(calls) == 2


def test_research_enforces_web_call_budget(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        research_service, "build_default_registry", _fake_factory(calls)
    )
    set_llm(_StubLLM(tool_name="search_web"))

    result = _result(_run("q", max_turns=5, max_web_calls=1))
    assert result["web_calls"] == 1
    # 实时搜索被拦，但循环没有崩，最后仍拿到答案
    assert result["answer"].startswith("结论")


def test_research_wraps_up_when_runtime_exhausted(monkeypatch):
    monkeypatch.setattr(
        research_service, "build_default_registry", _fake_factory([])
    )
    set_llm(_StubLLM())

    result = _result(_run("q", max_runtime_seconds=0))
    assert result["truncated"] == "runtime_seconds"
    # 时间用尽也必须给答案，不能返回空
    assert result["answer"]


def test_research_flags_fabricated_citation(monkeypatch):
    monkeypatch.setattr(
        research_service, "build_default_registry", _fake_factory([])
    )
    # 只给 1 条证据，答案却引用 [7]
    set_llm(_StubLLM(answer="有报道称发生了某件事 [7]。"))

    result = _result(_run("q", max_turns=2, max_tool_calls=1))
    assert result["citations"]["unknown"] == [7]
    # 不静默改答案：文本原样保留，错误只在元数据里暴露
    assert "[7]" in result["answer"]


def test_research_reports_coverage_over_all_evidence(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        research_service, "build_default_registry", _fake_factory(calls)
    )
    set_llm(_StubLLM(answer="只引用了第一条 [1]。"))

    result = _result(_run("q", max_turns=3, max_tool_calls=2))
    # 两条证据只引一条 → 覆盖率 0.5，这个数才能说明「检索给了多余的」
    assert result["citations"]["evidence_total"] == 2
    assert result["citations"]["coverage"] == 0.5


def test_check_citations_ignores_numbers_beyond_evidence():
    pool = EvidencePool()
    pool.add([_evidence("https://example.com/a")])
    report = check_citations("见 [1] 与 [2] 与 [99]", pool)
    assert report.cited == [1]
    assert report.unknown == [2, 99]


def test_stream_emits_ordered_events(monkeypatch):
    monkeypatch.setattr(
        research_service, "build_default_registry", _fake_factory([])
    )
    set_llm(_StubLLM())

    events = [e.event for e in _run("q", max_turns=2, max_tool_calls=1)]
    # 事件顺序是前端渲染的依据：先 started，工具结果在答案之前，done 最后
    assert events[0] == "started"
    assert events[-2:] == ["answer", "done"]
    assert "tool_result" in events
    assert events.index("tool_result") < events.index("answer")


def test_stream_events_are_json_serializable(monkeypatch):
    import json

    monkeypatch.setattr(
        research_service, "build_default_registry", _fake_factory([])
    )
    set_llm(_StubLLM())

    for event in _run("q", max_turns=2, max_tool_calls=1):
        # SSE 要能直接 json.dumps；把 dataclass 塞进事件会在序列化那一步才炸
        json.dumps(event.data, ensure_ascii=False)


def test_missing_llm_raises_unavailable(monkeypatch):
    set_llm(None)
    monkeypatch.setattr(research_service, "build_default_registry", _fake_factory([]))

    from app.services.llm_service import LLMUnavailable

    with pytest.raises(LLMUnavailable):
        # 生成器是惰性的，必须真的迭代到第一次 yield 才会触发
        asyncio.run(_collect("q"))


# --- 真实工具 handler ---
#
# 上面那批编排测试用的是假工具，绕过了 handler 本身。于是出现过一次
# `verification.independent_sources` 这种错误——字段名按记忆写，实际叫
# `unique_domains`，AttributeError 被 registry 兜成 ok=False，编排测试全绿，
# 只有真跑才会暴露。这两条专门守住「handler 会真的访问 bundle 的每个字段」。


def _bundle(**overrides):
    from app.services.evidence_service import EvidenceBundle, VerificationSummary

    defaults = {
        "query": "x",
        "local": [],
        "web": [],
        "verification": VerificationSummary(),
        "local_mode": "hybrid",
        "local_reranked": False,
        "web_status": "ok",
        "web_provider": "tavily",
    }
    defaults.update(overrides)
    return EvidenceBundle(**defaults)


def test_search_web_handler_reads_real_verification_fields(monkeypatch):
    from app.services import agent_tools
    from app.services.evidence_service import VerificationSummary

    async def fake_collect(session, query, **kwargs):
        return _bundle(
            web=[_evidence("https://example.com/news/1", "某条新闻")],
            verification=VerificationSummary(
                web_total=1,
                unique_domains=1,
                overlap_with_local=0,
                date_unverified=1,
            ),
        )

    monkeypatch.setattr(agent_tools, "collect_evidence", fake_collect)

    handler = agent_tools.make_search_web(EvidencePool())
    outcome = asyncio.run(handler(None, {"query": "x", "days": 7}))

    assert outcome.ok is True, outcome.content
    assert "独立声源 1 个" in outcome.content
    # 无日期时要明确提醒模型别当成近期
    assert "不能当作近期事件" in outcome.content
    assert outcome.meta["unique_domains"] == 1


def test_search_local_handler_reports_overflow(monkeypatch):
    from app.services import agent_tools

    async def fake_collect(session, query, **kwargs):
        return _bundle(
            local=[_evidence(f"https://example.com/a/{i}", f"文档{i}") for i in range(5)]
        )

    monkeypatch.setattr(agent_tools, "collect_evidence", fake_collect)

    # 池上限压到 2，逼出「有结果但装不下」的分支
    pool = EvidencePool(max_items=2)
    handler = agent_tools.make_search_local(pool)
    outcome = asyncio.run(handler(None, {"query": "x", "limit": 5}))

    assert outcome.ok is True, outcome.content
    # 不说「装不下」的话，模型会以为本地库只有 2 条
    assert "未纳入" in outcome.content
    assert outcome.meta["overflow"] == 3

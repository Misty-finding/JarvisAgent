from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.core.context import ExecutionContext
from jarvis.core.events.bus import EventBus
from jarvis.core.llm.openai_responses import OpenAIResponsesProvider
from jarvis.core.llm.provider import _anthropic_messages
from jarvis.core.session.store import SessionStore


# 将协议事件编码为真实 SSE 数据帧
def _sse(events: list[dict[str, Any]]) -> httpx.Response:
    body = "".join("event: event\ndata: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})


# 构造含缓存统计的 Responses 完成事件
def _complete(output: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "response.completed", "response": {
        "status": "completed", "output": output,
        "usage": {"input_tokens": 100, "output_tokens": 20,
                  "input_tokens_details": {"cached_tokens": 40}},
    }}


# 功能：验证真实 SSE 解码、工具续接及落盘恢复后仍保留加密推理状态
# 设计：在 HTTP 边界拦截两次请求，中途重建 provider 并重读 SessionStore，覆盖无服务端存储模式
async def test_tool_roundtrip_survives_session_reload(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [],
                 "encrypted_content": "opaque-test-state"}
    function = {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": "read_file", "arguments": '{"path":"README.md"}', "status": "completed"}

    # 首次返回工具请求，续接时返回流式回答
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://relay.test/v1/responses"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return _sse([_complete([reasoning, function])])
        return _sse([
            {"type": "response.output_text.delta", "delta": "read "},
            {"type": "response.output_text.delta", "delta": "OK"},
            _complete([{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "read OK"},
            ]}]),
        ])

    events: list[Any] = []

    # 收集与真实 TUI 相同的事件对象
    async def collect(event: Any) -> None:
        events.append(event)

    bus = EventBus()
    bus.subscribe(collect)
    tools = [{"name": "read_file", "input_schema": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }}]
    store = SessionStore(tmp_path)
    sid = "sess-probe"
    context = ExecutionContext("run", "read readme", 4)
    store.append_message(sid, "user", "read readme")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OpenAIResponsesProvider(
            "gpt-6.1-sol", api_key="test-key", base_url="https://relay.test/v1/",
            reasoning_effort="xhigh", client=client,
        )
        first = await provider.chat(context.messages, tools, bus, "run", system="system rules")
        assert first.stop_reason == "tool_use"
        call = first.tool_calls[0]
        blocks = [*first.provider_blocks, {"type": "tool_use", "id": call.id,
                  "name": call.name, "input": call.input}]
        store.append_message(sid, "assistant", blocks)
        store.append_message(sid, "user", [{"type": "tool_result", "tool_use_id": call.id,
                                          "content": "file contents"}])
        assert store.recover_thread(sid) == []
        restored = OpenAIResponsesProvider(
            "gpt-6.1-sol", api_key="test-key", base_url="https://relay.test/v1", client=client,
        )
        final = await restored.chat(store.read_messages(sid), tools, bus, "run")

    assert requests[0]["reasoning"] == {"effort": "xhigh"}
    assert requests[0]["instructions"] == "system rules"
    assert requests[0]["store"] is False
    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    assert requests[0]["tools"][0]["parameters"] == tools[0]["input_schema"]
    assert requests[0]["tools"][0]["strict"] is False
    assert "previous_response_id" not in requests[1]
    assert requests[1]["input"] == [
        {"role": "user", "content": "read readme"}, reasoning, function,
        {"type": "function_call_output", "call_id": "call_1", "output": "file contents"},
    ]
    assert final.text == "read OK" and final.stop_reason == "end_turn"
    assert final.usage is not None and final.usage.cache_read_input_tokens == 40
    assert [e.token for e in events if e.type == "llm.token"] == ["read ", "OK"]


# 功能：验证跨供应商切换时只发送规范消息，不泄漏另一协议的私有推理状态
# 设计：同时放入规范工具调用和原始输出，分别检查 OpenAI 与 Anthropic 请求避免重复调用
def test_cross_provider_history_uses_canonical_blocks() -> None:
    messages: list[dict[str, object]] = [{"role": "assistant", "content": [
        {"type": "provider_state", "provider": "openai_responses", "model": "other-model",
         "base_url": "https://other.test/v1", "items": [{"type": "reasoning", "id": "hidden"}]},
        {"type": "thinking", "thinking": "private", "signature": "sig"},
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "x"}},
    ]}]
    provider = OpenAIResponsesProvider("gpt-6.1-sol", api_key="test-key")
    converted = provider._input(messages)
    assert len(converted) == 2
    assert converted[0] == {"role": "assistant", "content": "checking"}
    assert converted[1]["type"] == "function_call"
    anthropic = _anthropic_messages(messages)
    assert all(b["type"] != "provider_state" for b in anthropic[0]["content"])  # type: ignore[union-attr]
    assert len(messages[0]["content"]) == 4  # type: ignore[arg-type]


# 功能：验证截断流和服务端失败不会被误标记为成功并执行工具
# 设计：模拟连接正常但协议未完整完成的情况，覆盖仅检查 HTTP 200 无法发现的错误
@pytest.mark.parametrize("events,match", [
    ([{"type": "response.output_text.delta", "delta": "partial"}], "without a completed"),
    ([{"type": "response.failed", "response": {"error": {"message": "upstream failed"}}}],
     "upstream failed"),
    ([{"type": "response.incomplete", "response": {
        "status": "incomplete", "output": [], "incomplete_details": {"reason": "content_filter"},
    }}], "content_filter"),
])
async def test_failed_response_is_not_success(events: list[dict[str, Any]], match: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _sse(events))) as client:
        provider = OpenAIResponsesProvider("gpt-6.1-sol", api_key="test-key", client=client)
        with pytest.raises(RuntimeError, match=match):
            await provider.chat([], [], EventBus(), "run")

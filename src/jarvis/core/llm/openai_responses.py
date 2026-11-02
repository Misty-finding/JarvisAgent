from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from jarvis.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from jarvis.core.events.bus import EventBus
from jarvis.core.llm.types import LlmResponse, ToolCallBlock, UsageStats


class OpenAIResponsesProvider:
    # 初始化 Responses 连接参数；可注入 HTTP 客户端进行协议测试
    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        reasoning_effort: str | None = None,
        store: bool = False,
        context_window: int = 200_000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._reasoning_effort = reasoning_effort
        self._store = store
        self._context_window = context_window
        self._client = client

    # 将统一会话消息转换为 Responses 输入，优先复用同一模型的原始推理状态
    def _input(self, messages: list[dict[str, object]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message["role"])
            content = message["content"]
            if isinstance(content, str):
                items.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                raise ValueError("Message content must be text or content blocks")
            native = next((
                block for block in content
                if block.get("type") == "provider_state"
                and block.get("provider") == "openai_responses"
                and block.get("model") == self._model
                and block.get("base_url") == self._base_url
            ), None)
            if role == "assistant" and native is not None:
                items.extend(native["items"])
                continue
            for block in content:
                kind = block.get("type")
                if kind == "text" and block.get("text"):
                    items.append({"role": role, "content": str(block["text"])})
                elif kind == "tool_use":
                    items.append({
                        "type": "function_call", "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block["input"], ensure_ascii=False),
                    })
                elif kind == "tool_result":
                    output = block.get("content", "")
                    if isinstance(output, list):
                        output = "\n".join(str(part.get("text", "")) for part in output)
                    items.append({
                        "type": "function_call_output", "call_id": block["tool_use_id"],
                        "output": str(output),
                    })
        return items

    # 构建无服务端会话依赖的 Responses 请求，显式携带工具和推理配置
    def _request(
        self, messages: list[dict[str, object]], tool_schemas: list[dict[str, object]],
        system: str | None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self._model,
            "input": self._input(messages),
            "stream": True,
            "store": self._store,
            "max_output_tokens": 32768,
            "include": ["reasoning.encrypted_content"],
        }
        if system:
            request["instructions"] = system
        if self._reasoning_effort:
            request["reasoning"] = {"effort": self._reasoning_effort}
        if tool_schemas:
            request["tools"] = [{
                "type": "function", "name": schema["name"],
                "description": schema.get("description", ""),
                "parameters": schema["input_schema"], "strict": False,
            } for schema in tool_schemas]
        return request

    # 消费 SSE 数据帧；缺失完成事件或服务端报告失败时终止本次模型调用
    async def _stream(
        self, client: httpx.AsyncClient, request: dict[str, Any],
        bus: EventBus, run_id: str,
    ) -> tuple[dict[str, Any], bool]:
        completed: dict[str, Any] | None = None
        streamed = False
        data: list[str] = []
        async with client.stream(
            "POST", self._base_url + "/responses", json=request,
            headers={"Authorization": "Bearer " + self._api_key},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                    continue
                if line or not data:
                    continue
                payload = "\n".join(data)
                data.clear()
                if payload == "[DONE]":
                    break
                event = json.loads(payload)
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    streamed = True
                    await bus.publish(LlmTokenEvent(
                        run_id=run_id, token=event["delta"], ts=datetime.now(UTC).isoformat(),
                    ))
                elif kind in ("error", "response.failed"):
                    error = event.get("error") or event.get("response", {}).get("error") or event
                    raise RuntimeError(f"Responses API failed: {error.get('message', kind)}")
                elif kind in ("response.completed", "response.incomplete"):
                    completed = event["response"]
        if completed is None:
            raise RuntimeError("Responses stream ended without a completed response")
        return completed, streamed

    # 解析完成响应，保留原始输出以便无存储模式下恢复推理及工具调用
    def _parse(self, response: dict[str, Any]) -> LlmResponse:
        status = response.get("status")
        if status not in ("completed", "incomplete"):
            raise RuntimeError(f"Unexpected Responses status: {status}")
        output = response.get("output", [])
        text: list[str] = []
        calls: list[ToolCallBlock] = []
        for item in output:
            if item["type"] == "message":
                for block in item.get("content", []):
                    if block["type"] == "output_text":
                        text.append(block["text"])
                    elif block["type"] == "refusal":
                        text.append(block["refusal"])
            elif item["type"] == "function_call":
                arguments = json.loads(item["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Function arguments must be a JSON object")
                calls.append(ToolCallBlock(item["call_id"], item["name"], arguments))
        if status == "incomplete":
            reason = (response.get("incomplete_details") or {}).get("reason")
            if reason != "max_output_tokens":
                raise RuntimeError(f"Responses generation incomplete: {reason}")
        raw_usage = response.get("usage") or {}
        input_tokens = int(raw_usage.get("input_tokens", 0))
        cached = (raw_usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
        return LlmResponse(
            stop_reason="max_tokens" if status == "incomplete" else (
                "tool_use" if calls else "end_turn"
            ),
            tool_calls=calls, text="".join(text),
            usage=UsageStats(
                input_tokens=input_tokens, output_tokens=int(raw_usage.get("output_tokens", 0)),
                cache_read_input_tokens=int(cached),
                context_pct=input_tokens / self._context_window,
            ),
            provider_blocks=[{
                "type": "provider_state", "provider": "openai_responses",
                "model": self._model, "base_url": self._base_url, "items": output,
            }],
        )

    # 发布现有 TUI 事件并调用 Responses 流，使主循环继续使用统一模型接口
    async def chat(
        self, messages: list[dict[str, object]], tool_schemas: list[dict[str, object]],
        bus: EventBus, run_id: str, *, step: int = 0, system: str | None = None,
    ) -> LlmResponse:
        await bus.publish(LlmModelSelectedEvent(
            run_id=run_id, model=self._model, strategy="static", ts=datetime.now(UTC).isoformat(),
        ))
        request = self._request(messages, tool_schemas, system)
        if self._client is not None:
            raw, streamed = await self._stream(self._client, request, bus, run_id)
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
                raw, streamed = await self._stream(client, request, bus, run_id)
        result = self._parse(raw)
        if result.text and not streamed:
            await bus.publish(LlmTokenEvent(
                run_id=run_id, token=result.text, ts=datetime.now(UTC).isoformat(),
            ))
        assert result.usage is not None
        usage = result.usage
        await bus.publish(LlmUsageEvent(
            run_id=run_id, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens, cache_creation_input_tokens=0,
            context_pct=usage.context_pct, ts=datetime.now(UTC).isoformat(),
        ))
        return result

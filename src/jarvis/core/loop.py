from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jarvis.core.bus.events import StepFinishedEvent, StepStartedEvent
from jarvis.core.context import ExecutionContext
from jarvis.core.events.bus import EventBus
from jarvis.core.llm.base import LLMProvider
from jarvis.core.prompt import build_runtime_system_prompt, finalization_system_prompt
from jarvis.core.tools.invocation import invoke_tool
from jarvis.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from jarvis.core.compact.compactor import Compactor
    from jarvis.core.permissions.manager import PermissionManager


log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    # 初始化 LLM、工具、事件总线及可选的权限管理器、压缩器和 session ID
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
        session_id: str = "",
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        finalization_turn = False
        base_system = build_runtime_system_prompt(context.max_steps)
        while not context.is_done():
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            # [plan] call LLM — API errors terminate the run
            try:
                system = context.system_prompt(base_system)
                if finalization_turn:
                    system = finalization_system_prompt(system)
                response = await self._provider.chat(
                    messages=context.messages,
                    tool_schemas=[] if finalization_turn else self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=system,
                )
            except asyncio.CancelledError:
                context.mark_failed("cancelled")
                raise
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                break

            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if finalization_turn:
                final_text = response.text.strip() or (
                    "I reached the tool-step limit before completing the task. "
                    "The collected evidence is preserved in the session, but the model did "
                    "not produce a final summary."
                )
                blocks.append({"type": "text", "text": final_text})
                context.add_assistant_message(blocks)
                context.result = final_text
                context.mark_success()
                await self._bus.publish(
                    StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
                )
                continue

            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            context.add_assistant_message(blocks)

            # [act] execute each requested tool; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                for tc in response.tool_calls:
                    result = await invoke_tool(
                        self._registry, tc, self._bus, context.run_id,
                        permission_manager=self._permission_manager,
                        session_id=self._session_id,
                    )
                    context.add_tool_result(tc.id, result.content, is_error=result.is_error)
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for tc in response.tool_calls:
                    context.add_tool_result(
                        tc.id,
                        "Error: output token limit reached before this tool call "
                        "could be completed. "
                        "Please break the task into smaller steps and try again.",
                        is_error=True,
                    )

            # end_turn wins over the tool budget. Otherwise reserve one no-tool final turn.
            if response.stop_reason == "end_turn":
                context.result = response.text or ""
                context.mark_success()
            elif context.step >= context.max_steps:
                finalization_turn = True

            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not context.is_done()
                and response.stop_reason == "tool_use"
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                await self._compactor.compact(context, self._provider)

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

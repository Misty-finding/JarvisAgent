from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.core.bus.events import RunFinishedEvent, RunStartedEvent
from jarvis.core.compact.compactor import Compactor
from jarvis.core.config import JarvisConfig
from jarvis.core.context import ExecutionContext
from jarvis.core.events.bus import EventBus, EventHandler
from jarvis.core.events.writer import EventWriter
from jarvis.core.execution.base import BackendTool, ExecutionBackend, ExecutionUnavailable
from jarvis.core.execution.cube import CubeBackend
from jarvis.core.execution.locking import execution_lock
from jarvis.core.llm.base import LLMProvider
from jarvis.core.llm.provider import create_llm_provider
from jarvis.core.loop import AgentLoop
from jarvis.core.mcp.server import McpServerManager
from jarvis.core.memory.loader import load_context_file
from jarvis.core.permissions.manager import PermissionManager
from jarvis.core.runs import RUNS_DIR, new_run_id
from jarvis.core.session.model import Session
from jarvis.core.session.store import SessionPersistenceError, SessionStore
from jarvis.core.skills.loader import SkillLoader
from jarvis.core.skills.tool import ActivateSkillTool
from jarvis.core.subagent.registry import BackgroundTaskRegistry
from jarvis.core.subagent.tool import AgentResultTool, SpawnAgentTool
from jarvis.core.task.manager import TaskManager
from jarvis.core.tools.builtin import (
    BashTool,
    ListDirTool,
    NoteSaveTool,
    ProjectMapTool,
    ReadFileTool,
    SearchFilesTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
)
from jarvis.core.tools.registry import ToolRegistry
from jarvis.core.trace.provider import TracingProvider
from jarvis.core.trace.writer import TraceWriter


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: JarvisConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        # 跨 run 共享的后台 subagent 任务注册表
        self._task_registry = BackgroundTaskRegistry()
        self._active_backend: CubeBackend | None = None
        self._cancel_requested = False

    def request_cancel(self) -> None:
        self._cancel_requested = True

    async def stop_execution(self) -> None:
        if self._active_backend is not None:
            await self._active_backend.cancel_run()

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）；可选注入 SpawnAgentTool
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        backend: ExecutionBackend | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist else None

        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        for t in [
            ProjectMapTool(),
            SearchFilesTool(),
            ReadFileTool(),
            ListDirTool(),
            BashTool(),
            WriteFileTool(),
        ]:
            if _ok(t.name):
                registry.register(BackendTool(t, backend, run_id=run_id) if backend else t)
        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _ok(t.name):
                registry.register(t)
        if _ok("activate_skill"):
            skill_tool = ActivateSkillTool(
                SkillLoader(self._config.workspace),
                bus or EventBus(),
                run_id or "",
                session_id,
            )
            if skill_tool.available:
                registry.register(skill_tool)
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(store, session.id, run_id)
            if _ok(note_tool.name):
                registry.register(note_tool)
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            if _ok("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=self._config.agent.max_steps,
                        task_registry=self._task_registry,
                        runs_dir=runs_dir,
                        session_id=session_id,
                        depth=0,
                        workspace=self._config.workspace,
                        backend=backend,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry))
        if self._mcp_manager is not None and backend is None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        return registry

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        global_ctx = load_context_file(Path("~/.jarvis/context.md").expanduser())
        project_ctx = load_context_file(self._config.workspace / ".jarvis/context.md")

        task_manager = TaskManager(run_path / ".tasks")

        bus = self._bus if self._bus is not None else EventBus()
        if store is not None:
            bus.subscribe(store.handle_sandbox_event)
        for h in self._extra_handlers:
            bus.subscribe(h)

        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            project_context=project_ctx,
            system_prompt_override=system_prompt_override,
        )
        if session is not None and store is not None:
            # 捕获当前会话和 run，持久化回调先于任何工具副作用执行
            def save_message(message: dict[str, Any]) -> None:
                assert store is not None and session is not None
                store.append_message(
                    session.id, str(message["role"]), message["content"], run_id=run_id
                )

            context.message_sink = save_message

            # 自动压缩后同步替换有效历史，避免恢复时重新载入压缩前内容
            def save_replacement(messages: list[dict[str, Any]]) -> None:
                assert store is not None and session is not None
                store.write_compacted(session.id, messages)

            context.replacement_sink = save_replacement

        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(
                RunStartedEvent(
                    run_id=run_id, goal=goal, session_id=session.id if session else None, ts=_now()
                )
            )

            cancelled = False
            backend: CubeBackend | None = None
            lease = None
            try:
                if (
                    session is not None
                    and session.execution_backend == "cubesandbox"
                    and self._config.execution.backend != "cubesandbox"
                ):
                    raise ExecutionUnavailable(
                        "This session belongs to CubeSandbox; local execution is forbidden."
                    )
                if self._config.execution.backend == "cubesandbox":
                    state_dir = (
                        store.session_dir(session.id) / "execution"
                        if session is not None and store is not None
                        else run_path / "execution"
                    )
                    pending_lease = execution_lock(state_dir)
                    pending_lease.__enter__()
                    lease = pending_lease
                    if session is not None and store is not None:
                        if session.sandbox_id and not (state_dir / "sandbox.json").exists():
                            raise ExecutionUnavailable(
                                "Sandbox mapping is missing; start a new session."
                            )
                        session.execution_backend = "cubesandbox"
                        store.write_meta(session)
                    try:
                        backend = CubeBackend(
                            self._config.execution,
                            self._config.workspace,
                            state_dir,
                            private_paths=[run_path.parent, self._runs_dir],
                            bus=bus,
                            session_id=session.id if session else None,
                            run_id=run_id,
                        )
                        self._active_backend = backend
                        await backend.prepare()
                    except (OSError, ValueError) as exc:
                        raise ExecutionUnavailable(
                            "Sandbox initialization failed; check project copy and connection "
                            "settings. "
                            "Local execution is disabled."
                        ) from exc
                    context.execution_runtime = backend.runtime_description
                    if session is not None and store is not None:
                        session.execution_backend = "cubesandbox"
                        session.sandbox_id = backend.sandbox_id
                        store.write_meta(session)
                if self._cancel_requested:
                    raise asyncio.CancelledError()
                if session is not None and session.model_alias != "default":
                    provider = self._provider or create_llm_provider(
                        self._config.llm,
                        session.model_alias,
                    )
                else:
                    provider = self._provider or create_llm_provider(self._config.llm)
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                session_id_str = session.id if session is not None else ""
                child_runs_dir = (
                    store.runs_dir(session.id)
                    if session is not None and store is not None
                    else self._runs_dir
                )
                registry = self._build_registry(
                    task_manager,
                    session=session,
                    store=store,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    child_runs_dir=child_runs_dir,
                    session_id=session_id_str,
                    tool_whitelist=tool_whitelist,
                    backend=backend,
                )
                session_dir = (
                    store.session_dir(session.id)
                    if session is not None and store is not None
                    else run_path
                )
                compactor = Compactor(bus, session_dir, session_id_str)
                loop = AgentLoop(
                    provider,
                    registry,
                    bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=session_id_str,
                    cancel_requested=lambda: self._cancel_requested,
                )
                await loop.run(context)
                if self._cancel_requested:
                    raise asyncio.CancelledError()
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")
                if backend is not None:
                    try:
                        await asyncio.shield(backend.cancel_run())
                    except ExecutionUnavailable:
                        cancelled = False
                        context.result = (
                            "Task cancellation could not be confirmed. Sandbox retained."
                        )
                        context.mark_failed("cancel_failed")
            except ExecutionUnavailable as exc:
                context.result = str(exc)
                context.mark_failed("execution_unavailable")
            except SessionPersistenceError:
                logging.getLogger(__name__).exception("session history could not be saved")
                context.mark_failed("storage_error")
            except Exception:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("llm_error")

            finally:
                # A run owns its child tasks; do not leave background workers after cancellation.
                tasks = [
                    task
                    for task, _ctx in self._task_registry.all()
                    if not task.done() and (backend is not None or cancelled)
                ]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    if backend is not None:
                        await backend.release()
                except SessionPersistenceError:
                    context.mark_failed("storage_error")
                    logging.getLogger(__name__).exception("sandbox lifecycle could not be saved")
                except ExecutionUnavailable as exc:
                    context.result = str(exc)
                    context.mark_failed("execution_unavailable")
                finally:
                    if lease is not None:
                        lease.__exit__(None, None, None)
                    self._active_backend = None

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.core.bus.envelope import HandlerError
from jarvis.core.bus.events import (
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from jarvis.core.config import LlmConfig
from jarvis.core.events.bus import EventBus
from jarvis.core.runs import new_run_id
from jarvis.core.session.model import Session, SessionMode
from jarvis.core.session.store import SessionHistoryError, SessionStore
from jarvis.core.skills.loader import SkillLoader

if TYPE_CHECKING:
    from jarvis.core.bus.commands import SessionListResult, SessionResumeResult
    from jarvis.core.llm.base import LLMProvider
    from jarvis.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_INVALID = -32013


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂、事件总线和可选的 LLM provider（用于手动压缩）
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        workspace: Path | None = None,
        llm_config: LlmConfig | None = None,
        execution_backend: str = "local",
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._llm_config = llm_config
        self._execution_backend = execution_backend
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel_locks: dict[str, asyncio.Lock] = {}
        self._runners: dict[str, AgentRunner] = {}
        self._skill_loader = SkillLoader(workspace)
        self._workspace = workspace.resolve() if workspace is not None else None

    # 创建新 session 并写入 meta.json
    async def create(self, mode: SessionMode, title: str = "") -> Session:
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
            workspace=str(self._workspace) if self._workspace is not None else "",
            execution_backend=self._execution_backend,
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 检查会话归属，旧会话按当前项目的存储目录确定作用域
    def _matches_workspace(self, session: Session) -> bool:
        return (
            not session.workspace
            or self._workspace is None
            or os.path.normcase(str(Path(session.workspace).resolve()))
            == os.path.normcase(str(self._workspace))
        )

    # 列出当前项目可恢复的聊天会话，并以真实协程锁判断是否仍在执行
    async def list_sessions(self) -> SessionListResult:
        from jarvis.core.bus.commands import SessionListResult, SessionSummary

        sessions, warnings = self._store.list_sessions()
        summaries: list[SessionSummary] = []
        for stored in sessions:
            session = self._sessions.get(stored.id, stored)
            if session.mode != "chat" or not self._matches_workspace(session):
                continue
            lock = self._locks.get(session.id)
            busy = lock is not None and lock.locked()
            state = session.last_run_state
            if not busy and (
                session.active_run_id
                or state == "running"
                or (session.status == "active" and session.run_ids)
            ):
                state = "interrupted"
            summaries.append(
                SessionSummary(
                    session_id=session.id,
                    title=session.title,
                    updated_at=session.updated_at,
                    status=session.status,
                    last_run_state=state,
                    busy=busy,
                )
            )
        return SessionListResult(sessions=summaries, warnings=warnings)

    # 从磁盘按需恢复聊天会话，整理中断历史后等待新输入，绝不自动调用模型或工具
    async def resume(self, sid: str) -> SessionResumeResult:
        from jarvis.core.bus.commands import SessionResumeResult

        try:
            session = self._sessions.get(sid) or self._store.read_meta(sid)
        except FileNotFoundError as exc:
            raise HandlerError(SESSION_NOT_FOUND, "session not found") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HandlerError(SESSION_INVALID, "会话元信息无法读取。") from exc
        if session.mode != "chat" or not self._matches_workspace(session):
            raise HandlerError(SESSION_INVALID, "只能恢复当前项目的聊天会话。")
        lock = self._locks.setdefault(sid, asyncio.Lock())
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "会话正在运行，请等待执行结束后恢复。")
        async with lock:
            interrupted_run = session.active_run_id
            if (
                not interrupted_run
                and session.run_ids
                and (session.status == "active" or session.last_run_state == "interrupted")
            ):
                interrupted_run = session.run_ids[-1]
            try:
                warnings = self._store.recover_thread(sid, interrupted_run)
                messages = self._store.read_messages(sid, truncate=False)
                if interrupted_run:
                    session.last_run_state = "interrupted"
                session.active_run_id = None
                session.status = "waiting_for_input"
                session.workspace = str(self._workspace) if self._workspace else session.workspace
                self._store.write_meta(session)
            except (OSError, SessionHistoryError) as exc:
                raise HandlerError(SESSION_INVALID, str(exc)) from exc
            self._sessions[sid] = session
            await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))
            return SessionResumeResult(
                session_id=sid,
                status=session.status,
                title=session.title,
                messages=messages,
                run_ids=session.run_ids,
                warnings=warnings,
                model_alias=session.model_alias,
                sandbox_id=session.sandbox_id,
            )

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(self, sid: str, content: str, *, run_id: str | None = None) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            try:
                self._store.recover_thread(sid)
            except (OSError, SessionHistoryError) as exc:
                raise HandlerError(SESSION_INVALID, str(exc)) from exc
            run_id = run_id or new_run_id()
            self._store.append_message(sid, "user", content, run_id=run_id)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )

            if not session.title:
                session.title = content[:40]

            session.run_ids.append(run_id)
            session.updated_at = _now()
            session.active_run_id = run_id
            session.last_run_state = "running"
            session.status = "active"
            self._store.write_meta(session)

            try:
                current_task = asyncio.current_task()
                if current_task is not None:
                    self._run_tasks[sid] = current_task
                # Skill 解析：检测斜杠调用，注入含资源根目录和用户参数的完整指令
                goal = content
                system_prompt_override: str | None = None
                tool_whitelist: list[str] | None = None
                if content.startswith("/"):
                    parts = content[1:].split(None, 1)
                    skill_name = parts[0] if parts else ""
                    arguments = parts[1] if len(parts) > 1 else ""
                    skill = self._skill_loader.resolve(skill_name)
                    if skill is not None:
                        goal = self._skill_loader.render_prompt(skill, arguments)
                        system_prompt_override = goal
                        tool_whitelist = skill.tool_whitelist
                        await self._bus.publish(
                            SkillInvokedEvent(
                                skill_name=skill_name,
                                arguments=arguments,
                                run_id=run_id,
                                session_id=sid,
                                ts=_now(),
                            )
                        )

                runner = self._runner_factory()
                self._runners[sid] = runner
                outcome = await runner.run_and_capture(
                    goal,
                    run_id=run_id,
                    session=session,
                    store=self._store,
                    system_prompt_override=system_prompt_override,
                    tool_whitelist=tool_whitelist,
                )
                session.last_run_state = "completed" if outcome.status == "success" else "failed"
            except asyncio.CancelledError:
                session.last_run_state = "interrupted"
                raise
            except Exception:
                session.last_run_state = "failed"
                raise
            finally:
                self._run_tasks.pop(sid, None)
                self._runners.pop(sid, None)
                session.active_run_id = None
                session.updated_at = _now()
                session.status = "closed" if session.mode == "one_shot" else "waiting_for_input"
                self._store.write_meta(session)
                if session.mode == "one_shot":
                    await self._bus.publish(
                        SessionClosedEvent(session_id=sid, ts=session.updated_at)
                    )
                else:
                    await self._bus.publish(
                        SessionWaitingForInputEvent(
                            session_id=sid,
                            last_run_id=run_id,
                            ts=session.updated_at,
                        )
                    )
            return run_id

    async def cancel(self, sid: str) -> bool:
        """Cancel a session run and wait until its backend has terminated execution."""
        self._get_session(sid)
        async with self._cancel_locks.setdefault(sid, asyncio.Lock()):
            task = self._run_tasks.get(sid)
            if task is None or task.done():
                return False
            runner = self._runners.get(sid)
            stopping = None
            if runner is not None:
                runner.request_cancel()
                stopping = asyncio.create_task(runner.stop_execution())
            task.cancel()
            results = await asyncio.gather(
                task, *([stopping] if stopping is not None else []), return_exceptions=True
            )
            if stopping is not None and isinstance(results[-1], Exception):
                raise HandlerError(
                    -32016, "Task stop could not be confirmed; sandbox was retained."
                )
            if self._get_session(sid).last_run_state == "failed":
                raise HandlerError(
                    -32016, "Task stop could not be confirmed; sandbox was retained."
                )
            return True

    def has_active_run(self, sid: str) -> bool:
        task = self._run_tasks.get(sid)
        return task is not None and not task.done()

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动压缩指定 session 的 thread，将摘要持久化写入 thread.jsonl
    async def compact(self, sid: str, focus: str = "") -> Any:
        from jarvis.core.llm.provider import create_llm_provider

        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        provider = self._provider
        if self._llm_config is not None and session.model_alias != "default":
            provider = create_llm_provider(self._llm_config, session.model_alias)
        if provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from jarvis.core.bus.commands import SessionCompactResult
            from jarvis.core.compact.compactor import Compactor

            messages = self._store.read_messages(sid)
            session_dir = self._store.session_dir(sid)
            compactor = Compactor(self._bus, session_dir, sid)
            result = await compactor.compact_messages(messages, provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            self._store.write_compacted(
                sid,
                [
                    {"role": "user", "content": result.summary_text},
                    {
                        "role": "assistant",
                        "content": "Understood, I'll continue from this summary.",
                    },
                ],
            )
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
            )

    # 返回会话可选模型，不向客户端暴露密钥及认证文件信息
    def list_models(self, sid: str) -> Any:
        from jarvis.core.bus.commands import ModelInfo, ModelListResult
        from jarvis.core.llm.provider import resolve_model_config

        session = self._get_session(sid)
        config = self._llm_config
        if config is None:
            raise HandlerError(-32022, "Model configuration is unavailable")
        models = []
        for name in ["default", *config.models]:
            selected = resolve_model_config(config, name)
            models.append(
                ModelInfo(
                    name=name,
                    provider=selected.provider,
                    model=selected.default_model,
                    display_name=config.models[name].display_name if name != "default" else None,
                    reasoning_effort=selected.reasoning_effort,
                )
            )
        return ModelListResult(models=models, selected=session.model_alias)

    # 在空闲会话中切换并持久化模型，检查配置但不发起模型请求
    async def set_model(self, sid: str, name: str) -> Any:
        from jarvis.core.bus.commands import SessionSetModelResult
        from jarvis.core.llm.provider import create_llm_provider, resolve_model_config

        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if session.status == "closed":
            raise HandlerError(SESSION_CLOSED, "session already closed")
        if self._llm_config is None:
            raise HandlerError(-32022, "Model configuration is unavailable")
        async with lock:
            try:
                config = resolve_model_config(self._llm_config, name)
                create_llm_provider(self._llm_config, name)
            except (ValueError, SystemExit) as exc:
                raise HandlerError(-32602, str(exc)) from exc
            previous = session.model_alias
            session.model_alias = name
            session.updated_at = _now()
            try:
                self._store.write_meta(session)
            except OSError:
                session.model_alias = previous
                raise
            return SessionSetModelResult(name=name, model=config.default_model)

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_messages(sid)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session

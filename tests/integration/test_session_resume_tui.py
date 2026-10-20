from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from textual.pilot import Pilot

from jarvis.core.app import CoreApp
from jarvis.core.bus.events import LlmTokenEvent
from jarvis.core.config import JarvisConfig
from jarvis.core.events.bus import EventBus
from jarvis.core.llm.types import LlmResponse
from jarvis.core.runner import AgentRunner
from jarvis.core.session.manager import SessionManager
from jarvis.core.session.store import SessionStore
from jarvis.core.transport.ipc_broadcaster import IpcEventBroadcaster
from jarvis.core.transport.socket_server import SocketServer
from jarvis.tui.app import ChatTextArea, JarvisTuiApp, LLMStreamBlock, SelectableStatic
from jarvis.tui.session_picker import SessionPicker


class _OfflineProvider:
    # 记录模型实际收到的上下文，离线返回新一轮消息
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.system = ""

    # 模拟流式 token 事件与完整响应，不进行任何外部 API 请求
    async def chat(
        self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]],
        bus: EventBus, run_id: str, *, step: int = 0, system: str | None = None,
    ) -> LlmResponse:
        self.messages = list(messages)
        self.system = system or ""
        await bus.publish(LlmTokenEvent(run_id=run_id, token="continued", ts="now"))
        return LlmResponse(stop_reason="end_turn", text="continued")


# 建立真实 TCP/Core handler 链路，同时使用全新 manager 模拟服务重启后的历史加载
@pytest.fixture
async def resume_core(
    tmp_path: Path, free_port: int,
) -> AsyncIterator[tuple[int, str, Mock, _OfflineProvider, SessionStore]]:
    store = SessionStore(tmp_path / "sessions")
    first = SessionManager(store, Mock(), EventBus(), workspace=tmp_path)
    session = await first.create("chat", "历史 [test]")
    store.append_message(session.id, "user", "old goal")
    store.append_message(session.id, "assistant", [{"type": "text", "text": "old answer"}])
    store.append_note(session.id, "old note", "old-run")
    await first.close(session.id)

    core = CoreApp()
    core._config = JarvisConfig(workspace=tmp_path)
    core._broadcaster = IpcEventBroadcaster()
    core._bus.subscribe(core._broadcaster.handle)
    provider = _OfflineProvider()

    # 每次新输入才建立 runner，便于断言恢复操作不会触发模型
    def make_runner() -> AgentRunner:
        assert core._config is not None
        return AgentRunner(core._config, provider=provider, bus=core._bus)  # type: ignore[arg-type]

    factory = Mock(side_effect=make_runner)
    core._sessions = SessionManager(store, factory, core._bus, workspace=tmp_path)
    server = SocketServer("127.0.0.1", free_port, core._broadcaster)
    for name, handler in [
        ("session.list", core._session_list_handler),
        ("session.resume", core._session_resume_handler),
        ("session.create", core._session_create_handler),
        ("session.send_message", core._session_send_handler),
        ("session.close", core._session_close_handler),
        ("event.subscribe", core._subscribe_handler),
        ("permission.set_mode", core._permission_set_mode_handler),
    ]:
        server.register(name, handler)
    await server.start()
    try:
        yield free_port, session.id, factory, provider, store
    finally:
        await server.stop()


# 等待真实 socket worker 和 Textual 消息泵完成一次状态变化，并设置超时避免挂起
async def _until(pilot: Pilot[Any], predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause(0.02)


# 功能：验证两种入口通过真实 IPC 加载历史，等待输入后沿用原会话执行
# 设计：无头 TUI 操作真实 Core handlers，模型使用替身，联合检查界面和传给模型的数据
@pytest.mark.parametrize("picker", [False, True])
async def test_resume_tui_over_ipc_then_continue(
    resume_core: tuple[int, str, Mock, _OfflineProvider, SessionStore], picker: bool,
) -> None:
    port, sid, factory, provider, store = resume_core
    app = JarvisTuiApp("127.0.0.1", port, resume_session_id="" if picker else sid)
    async with app.run_test(size=(110, 40)) as pilot:
        if picker:
            await _until(pilot, lambda: isinstance(app.screen, SessionPicker))
            await pilot.press("enter")
        await _until(pilot, lambda: app._session_id == sid and not app._opening_session)
        factory.assert_not_called()
        assert len(store.list_sessions()[0]) == 1
        assert app._permission_mode == "ask"
        assert not app._busy
        assert app.query_one(LLMStreamBlock)._text == "old answer"
        prompt = app.query_one(ChatTextArea)
        await _until(pilot, lambda: not prompt.disabled)
        prompt.text = "continue"
        prompt.focus()
        await pilot.press("enter")
        await _until(pilot, lambda: factory.call_count == 1 and not app._busy)
        assert provider.messages[0]["content"] == "old goal"
        assert provider.messages[-1]["content"] == "continue"
        assert "old note" in provider.system
        assert app.query(LLMStreamBlock).last()._text == "continued"
        assert store.read_meta(sid).last_run_state == "completed"


# 功能：验证指定不存在会话时 TUI 报错，且不会悄悄创建新会话
# 设计：调用真实 IPC 错误链路，断言原存储数量不变且输入保持禁用
async def test_missing_resume_tui_does_not_create_session(
    resume_core: tuple[int, str, Mock, _OfflineProvider, SessionStore],
) -> None:
    port, _, factory, _, store = resume_core
    app = JarvisTuiApp("127.0.0.1", port, resume_session_id="sess-missing")
    async with app.run_test(size=(110, 40)) as pilot:
        await _until(pilot, lambda: app._connection_state == "resume failed")
        assert app._session_id is None
        assert app.query_one(ChatTextArea).disabled
        assert len(store.list_sessions()[0]) == 1
        assert any("无法打开会话" in str(widget.render()) for widget in app.query(SelectableStatic))
        factory.assert_not_called()

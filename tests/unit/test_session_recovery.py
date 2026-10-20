from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from jarvis.core.bus.envelope import HandlerError
from jarvis.core.config import JarvisConfig
from jarvis.core.events.bus import EventBus
from jarvis.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from jarvis.core.runner import AgentRunner, RunOutcome
from jarvis.core.session.manager import SESSION_BUSY, SESSION_INVALID, SessionManager
from jarvis.core.session.model import Session
from jarvis.core.session.store import (
    SessionHistoryError,
    SessionPersistenceError,
    SessionStore,
)


# 建立完全离线的会话管理器，只有收到新输入才实例化 runner
def _manager(store: SessionStore, workspace: Path) -> tuple[SessionManager, Mock]:
    factory = Mock()
    return SessionManager(store, factory, EventBus(), workspace=workspace), factory


# 为历史写入同一批次两个工具调用，便于模拟部分结果已保存的中断
def _pending_tools(store: SessionStore, sid: str) -> None:
    store.append_message(sid, "user", "change files", "run-old")
    store.append_message(sid, "assistant", [
        {"type": "text", "text": "working"},
        {"type": "tool_use", "id": "a", "name": "write_file", "input": {}},
        {"type": "tool_use", "id": "b", "name": "bash", "input": {}},
    ], "run-old")
    store.append_message(sid, "user", [
        {"type": "tool_result", "tool_use_id": "a", "content": "saved"},
    ], "run-old")


# 功能：验证重建 manager 后可恢复已关闭会话，保留历史及笔记且不启动执行
# 设计：丢弃原 manager 模拟 daemon 重启，真实读写磁盘避免仅验证内存缓存
async def test_resume_after_restart_preserves_history_and_notes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first, _ = _manager(store, tmp_path)
    session = await first.create("chat", "恢复测试")
    store.append_message(session.id, "user", "hello")
    store.append_message(session.id, "assistant", [{"type": "text", "text": "world"}])
    store.append_note(session.id, "use Python 3.12", "old")
    await first.close(session.id)

    restarted, factory = _manager(SessionStore(tmp_path / "sessions"), tmp_path)
    listing = await restarted.list_sessions()
    assert [item.session_id for item in listing.sessions] == [session.id]
    result = await restarted.resume(session.id)

    assert result.status == "waiting_for_input"
    assert result.messages == store.read_messages(session.id, truncate=False)
    assert result.messages[1]["content"][0]["text"] == "world"
    assert "use Python 3.12" in store.read_notes(session.id)
    factory.assert_not_called()


# 功能：验证恢复中断批次时只补缺失结果，多次恢复不重复插入说明
# 设计：已完成结果和未完成调用共存，断言原始结果保持且补齐后满足消息协议
async def test_interrupted_tools_recover_once_without_running(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first, _ = _manager(store, tmp_path)
    session = await first.create("chat")
    _pending_tools(store, session.id)
    session.run_ids = ["run-old"]
    session.active_run_id = "run-old"
    session.last_run_state = "running"
    store.write_meta(session)

    restarted, factory = _manager(store, tmp_path)
    assert (await restarted.list_sessions()).sessions[0].last_run_state == "interrupted"
    result = await restarted.resume(session.id)
    results = result.messages[2]["content"]
    assert results[0] == {"type": "tool_result", "tool_use_id": "a", "content": "saved"}
    assert results[1]["tool_use_id"] == "b"
    assert results[1]["is_error"] is True
    assert "effects are unknown" in results[1]["content"]
    assert len(result.warnings) == 2
    original = (store.session_dir(session.id) / "thread.jsonl").read_bytes()
    again = await restarted.resume(session.id)
    assert again.messages == result.messages
    assert (store.session_dir(session.id) / "thread.jsonl").read_bytes() == original
    factory.assert_not_called()


# 功能：验证仍在执行的会话不能恢复，列表同步显示 busy
# 设计：占用真实 asyncio 锁覆盖恢复与正在运行协程之间的冲突
async def test_busy_session_cannot_resume(tmp_path: Path) -> None:
    manager, _ = _manager(SessionStore(tmp_path), tmp_path)
    session = await manager.create("chat")
    async with manager._locks[session.id]:
        assert (await manager.list_sessions()).sessions[0].busy
        with pytest.raises(HandlerError) as error:
            await manager.resume(session.id)
        assert error.value.code == SESSION_BUSY


# 功能：验证不同 workspace 以及 one_shot 会话不能通过恢复入口打开
# 设计：在共享存储中创建两种不可恢复会话，同时检查列表隐藏和指定 ID 拒绝
async def test_resume_scoped_to_chat_and_workspace(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first, _ = _manager(store, tmp_path / "one")
    chat = await first.create("chat")
    shot = await first.create("one_shot")
    other, _ = _manager(store, tmp_path / "two")
    assert not (await other.list_sessions()).sessions
    for manager, sid in [(other, chat.id), (first, shot.id)]:
        with pytest.raises(HandlerError) as error:
            await manager.resume(sid)
        assert error.value.code == SESSION_INVALID


# 功能：验证旧版元信息没有新增字段时仍可恢复
# 设计：直接写入旧版 JSON 而非当前序列化器，确保兼容真实的存量会话
async def test_legacy_metadata_can_resume(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    folder = store.session_dir("sess-legacy")
    folder.mkdir()
    (folder / "meta.json").write_text(json.dumps({
        "id": "sess-legacy", "mode": "chat", "status": "closed", "title": "old",
        "created_at": "2026-01-01", "updated_at": "2026-01-01", "run_ids": [],
    }), encoding="utf-8")
    manager, factory = _manager(store, tmp_path)
    assert (await manager.resume("sess-legacy")).title == "old"
    factory.assert_not_called()


# 功能：验证仅恢复末尾未写完的 JSON，并备份原始内容
# 设计：保留有效前缀加半条消息，模拟进程退出造成的尾部写入中断
def test_recover_partial_final_line_with_backup(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    sid = "sess-partial"
    store.append_message(sid, "user", "hello")
    path = store.session_dir(sid) / "thread.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"role":"assistant","content":')
    broken = path.read_bytes()
    assert store.recover_thread(sid)
    assert store.read_messages(sid) == [{"role": "user", "content": "hello"}]
    assert next(path.parent.glob("*.bak")).read_bytes() == broken


# 功能：验证历史中间损坏及跨批次未配对调用不会被静默截断
# 设计：分别构造完整坏行和缺结果后开始下一批调用，要求拒绝并保留原文件
@pytest.mark.parametrize("damage", ["json", "sequence"])
def test_interior_corruption_is_not_repaired(tmp_path: Path, damage: str) -> None:
    store = SessionStore(tmp_path)
    sid = "sess-corrupt"
    _pending_tools(store, sid)
    path = store.session_dir(sid) / "thread.jsonl"
    if damage == "json":
        with path.open("ab") as stream:
            stream.write(b'not-json\n')
    else:
        store.append_message(sid, "assistant", [
            {"type": "tool_use", "id": "c", "name": "bash", "input": {}},
        ])
    original = path.read_bytes()
    with pytest.raises(SessionHistoryError):
        store.recover_thread(sid)
    assert path.read_bytes() == original


# 功能：验证压缩替换失败不会丢失原始对话
# 设计：在原子替换边界注入磁盘错误，覆盖已备份但尚未替换的故障窗口
def test_failed_compaction_keeps_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-atomic", "user", "original")
    path = store.session_dir("sess-atomic") / "thread.jsonl"
    original = path.read_bytes()
    monkeypatch.setattr("jarvis.core.session.store.os.replace", Mock(side_effect=OSError("full")))
    with pytest.raises(SessionPersistenceError):
        store.write_compacted("sess-atomic", [{"role": "user", "content": "summary"}])
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


class _ToolProvider:
    # 保存测试存储与压缩模式，模型调用完全离线
    def __init__(self, store: SessionStore, sid: str, compact: bool = False) -> None:
        self.store, self.sid, self.compact = store, sid, compact
        self.turn = 0
        self.next_messages: list[dict[str, Any]] = []
        self.next_system = ""

    # 首轮请求工具，第二轮核对磁盘已保存结果，再返回最终答案
    async def chat(
        self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]],
        bus: EventBus, run_id: str, *, step: int = 0, system: str | None = None,
    ) -> LlmResponse:
        if run_id == "compact":
            return LlmResponse(stop_reason="end_turn", text="summary of work")
        self.turn += 1
        if self.turn == 1:
            return LlmResponse(
                stop_reason="tool_use", tool_calls=[ToolCallBlock("t1", "test", {})],
                usage=UsageStats(100, 10, context_pct=0.95 if self.compact else 0),
            )
        self.next_messages = json.loads(json.dumps(messages))
        self.next_system = system or ""
        assert self.store.read_messages(self.sid, truncate=False) == messages
        return LlmResponse(stop_reason="end_turn", text="done")


# 准备真实 runner 的离线运行环境，只替换模型和工具执行边界
async def _runner_session(
    tmp_path: Path, compact: bool = False,
) -> tuple[AgentRunner, SessionStore, Session, _ToolProvider]:
    store = SessionStore(tmp_path / "sessions")
    manager, _ = _manager(store, tmp_path)
    session = await manager.create("chat")
    store.append_message(session.id, "user", "start")
    store.append_note(session.id, "persistent note", "old")
    provider = _ToolProvider(store, session.id, compact)
    config = JarvisConfig(workspace=tmp_path)
    config.compaction.auto_threshold = 0.8
    runner = AgentRunner(config, provider=provider)  # type: ignore[arg-type]
    return runner, store, session, provider


# 功能：验证工具执行前调用已落盘，下一次模型请求前结果已落盘
# 设计：使用真实 AgentRunner/AgentLoop，在副作用边界读取文件校验时序
@pytest.mark.parametrize("compact", [False, True])
async def test_runner_persists_incrementally_and_after_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compact: bool,
) -> None:
    from jarvis.core.tools.base import ToolResult

    runner, store, session, provider = await _runner_session(tmp_path, compact)

    # 在工具真正执行时校验调用记录已保存，而不是在 run 结束后补写
    async def invoke(*args: Any, **kwargs: Any) -> ToolResult:
        rows = [json.loads(line) for line in (
            store.session_dir(session.id) / "thread.jsonl"
        ).read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["content"][-1]["id"] == "t1"
        return ToolResult(content="tool output")

    monkeypatch.setattr("jarvis.core.loop.invoke_tool", invoke)
    outcome = await runner.run_and_capture("start", session=session, store=store)
    assert outcome.status == "success"
    saved = store.read_messages(session.id, truncate=False)
    assert saved == provider.next_messages + [
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    assert "persistent note" in provider.next_system
    if compact:
        assert saved[0]["content"] == "summary of work"
        assert len(saved) == 3
        assert list(store.session_dir(session.id).glob("*.bak"))
    else:
        assert saved[-2]["content"][0]["content"] == "tool output"


# 功能：验证 assistant 消息写入失败时工具不会执行
# 设计：在模型响应后的写盘边界注入错误，通过真实 loop 验证不会越过持久化屏障
async def test_storage_failure_prevents_tool_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, store, session, _ = await _runner_session(tmp_path)
    tool = Mock()
    monkeypatch.setattr("jarvis.core.loop.invoke_tool", tool)
    monkeypatch.setattr(store, "append_message", Mock(side_effect=SessionPersistenceError("full")))
    outcome = await runner.run_and_capture("start", session=session, store=store)
    assert outcome.reason == "storage_error"
    tool.assert_not_called()


# 功能：验证取消执行后元信息记录中断，恢复后新输入才会启动下一轮
# 设计：runner 内等待事件再取消，覆盖协程取消清理和会话继续两个真实状态转换
async def test_cancel_then_resume_and_send(tmp_path: Path) -> None:
    entered = asyncio.Event()

    # 模拟已保存工具请求但尚未获得结果的执行
    async def interrupted(*args: Any, **kwargs: Any) -> RunOutcome:
        session, store = kwargs["session"], kwargs["store"]
        store.append_message(session.id, "assistant", [
            {"type": "tool_use", "id": "pending", "name": "bash", "input": {}},
        ])
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    manager, factory = _manager(SessionStore(tmp_path), tmp_path)
    factory.return_value.run_and_capture = interrupted
    session = await manager.create("chat")
    task = asyncio.create_task(manager.send_message(session.id, "start"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager._store.read_meta(session.id).last_run_state == "interrupted"

    restarted, new_factory = _manager(manager._store, tmp_path)
    await restarted.resume(session.id)
    new_factory.assert_not_called()

    # 新一轮只由新输入触发，并能读到补齐后的历史
    async def completed(*args: Any, **kwargs: Any) -> RunOutcome:
        history = manager._store.read_messages(session.id)
        assert history[-1]["content"] == "continue"
        assert history[2]["content"][0]["is_error"]
        return RunOutcome(status="success", result="done", reason=None)

    new_factory.return_value.run_and_capture = completed
    await restarted.send_message(session.id, "continue")
    assert manager._store.read_meta(session.id).last_run_state == "completed"

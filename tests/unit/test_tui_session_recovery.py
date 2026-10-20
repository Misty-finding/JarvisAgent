from __future__ import annotations

from typing import Any

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Button, ListView

from jarvis.tui.app import JarvisTuiApp, LLMStreamBlock, ToolCallBlock
from jarvis.tui.session_picker import SessionItem, SessionPicker


class _PickerApp(App[None]):
    # 保存选择结果，区分未选择、取消与显式新建
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        super().__init__()
        self.sessions = sessions
        self.selected: str | None = "unselected"

    # 挂载真实选择弹窗并通过回调记录用户操作
    def on_mount(self) -> None:
        self.push_screen(SessionPicker(self.sessions, []), self._selected)

    # 捕获弹窗返回值供无头交互测试断言
    def _selected(self, selected: str | None) -> None:
        self.selected = selected


# 功能：验证键盘能跳过运行中会话并恢复空闲会话
# 设计：真实挂载 ModalScreen 并发送按键，覆盖焦点、禁用行和选择事件的协作
async def test_picker_keyboard_skips_busy_session() -> None:
    app = _PickerApp([
        {"session_id": "sess-busy", "title": "running", "busy": True},
        {"session_id": "sess-ready", "title": "[brackets]", "busy": False},
    ])
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert app.screen.query(SessionItem).first().disabled
        listing = app.screen.query_one(ListView)
        listing.index = 0
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.selected == "sess-ready"


# 功能：验证无历史时可显式新建，也可以取消恢复
# 设计：以空列表分别测试按钮和 Esc，避免空列表导致 TUI 无法退出或继续
@pytest.mark.parametrize("cancel", [False, True])
async def test_empty_picker_new_or_cancel(cancel: bool) -> None:
    app = _PickerApp([])
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen.focused, Button)
        await pilot.press("escape" if cancel else "enter")
        await pilot.pause()
        assert app.selected is None if cancel else app.selected == ""


# 功能：验证历史渲染只生成静态消息和完成工具块，不恢复挂起审批
# 设计：包含 markup 字符及失败结果，直接检查模型与工具块的状态而非截图文本
def test_history_render_is_static_and_preserves_results() -> None:
    app = JarvisTuiApp("127.0.0.1", 1)
    rendered: list[Widget] = []
    app._append = rendered.append  # type: ignore[method-assign]
    app._render_history([
        {"role": "user", "content": "look at [x]"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "one", "name": "bash", "input": {"command": "x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "one", "content": "[unknown]",
             "is_error": True},
        ]},
    ])
    assert len(rendered) == 3
    assert isinstance(rendered[1], LLMStreamBlock)
    assert rendered[1]._text == "looking"
    assert isinstance(rendered[2], ToolCallBlock)
    assert rendered[2]._output == "[unknown]"
    assert rendered[2]._finished and rendered[2]._is_error
    assert "历史记录" in rendered[2]._summary()
    assert not app._pending_permission_blocks
    assert not app._pending_tool_blocks


# 功能：验证恢复会话后实时事件不会混入其他会话的输出
# 设计：给两个会话发送相同类型事件，断言只渲染所选会话的新 run
def test_restored_session_filters_live_events() -> None:
    app = JarvisTuiApp("127.0.0.1", 1)
    app._session_id = "sess-current"
    rendered: list[Widget] = []
    app._append = rendered.append  # type: ignore[method-assign]
    app._handle_event({"type": "run.started", "session_id": "sess-other", "run_id": "other"})
    app._handle_event({"type": "llm.token", "run_id": "other", "token": "wrong"})
    assert not rendered
    app._handle_event({"type": "run.started", "session_id": "sess-current", "run_id": "new"})
    app._handle_event({"type": "llm.token", "run_id": "new", "token": "right"})
    assert isinstance(rendered[-1], LLMStreamBlock)
    assert rendered[-1]._text == "right"
    app._handle_event({
        "type": "skill.invoked", "session_id": "sess-current", "run_id": "skill-run",
        "skill_name": "test", "arguments": "argument",
    })
    assert "skill-run" in app._session_run_ids
    assert "test" in str(rendered[-1].render())

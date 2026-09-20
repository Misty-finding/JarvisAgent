from __future__ import annotations

import pytest
from rich.markdown import Markdown
from textual.binding import Binding
from textual.selection import SELECT_ALL
from textual.widget import Widget

from jarvis.tui.app import (
    ChatTextArea,
    JarvisTuiApp,
    LLMStreamBlock,
    ToolCallBlock,
    _param_summary,
    _preview,
)


# 功能：验证 _preview 超出长度时截断并追加省略号
# 设计：不依赖任何 TUI 组件，纯函数测试
def test_preview_truncates() -> None:
    assert _preview("abcde", 3) == "abc…"
    assert _preview("ab", 5) == "ab"


# 功能：验证工具参数摘要优先展示工具最关键字段
# 设计：覆盖 read_file/bash/note_save 三类常见工具，避免工具块摘要退化成整段 JSON
def test_param_summary_prefers_key_fields() -> None:
    assert _param_summary("read_file", {"path": "README.md"}) == "path='README.md'"
    assert _param_summary("bash", {"command": "echo hi", "timeout": 1}) == "command='echo hi'"
    assert _param_summary("note_save", {"content": "Python 3.12"}) == "content='Python 3.12'"


# 功能：验证 Ctrl+V 从 Windows 系统剪贴板读取文本并插入当前光标位置
def test_chat_text_area_pastes_windows_clipboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jarvis.tui.app._read_windows_clipboard",
        lambda: " pasted\ntext",
    )
    area = ChatTextArea("hello")
    area.move_cursor((0, 5))

    area.action_paste()

    assert area.text == "hello pasted\ntext"


# 功能：验证粘贴时选区内容会被系统剪贴板文本替换
def test_chat_text_area_paste_replaces_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jarvis.tui.app._read_windows_clipboard",
        lambda: "Jarvis",
    )
    area = ChatTextArea("hello world")
    area.selection = ((0, 6), (0, 11))

    area.action_paste()

    assert area.text == "hello Jarvis"


# 功能：验证 llm.token 事件累积到 LLMStreamBlock，不连续 token 各自新开一块
# 设计：monkey-patch _append 收集追加的 widgets，断言 token 追加到同一块；
#       发送非 token 事件后新 block 被重置，下一个 token 开启新块
def test_llm_tokens_accumulate_in_block() -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "llm.token", "token": "Hello", "run_id": "r", "ts": "t"})
    app._handle_event({"type": "llm.token", "token": " world", "run_id": "r", "ts": "t"})

    assert len(appended) == 1  # same block reused
    assert isinstance(appended[0], LLMStreamBlock)
    assert appended[0]._text == "Hello world"  # type: ignore[attr-defined]


# 功能：验证 LLMStreamBlock 结束时会把累积文本渲染为 Rich Markdown
# 设计：直接调用 finalize_markdown，断言 renderable 类型，覆盖 Markdown polish 的核心行为
def test_llm_block_finalize_renders_markdown() -> None:
    block = LLMStreamBlock()
    block.append_token("## Title\n\n- one\n\n```python\nprint('hi')\n```")
    block.finalize_markdown()
    assert isinstance(block.content, Markdown)


# 功能：验证 Markdown 回答块向 Textual 选区系统暴露屏幕实际显示的纯文本
def test_llm_block_selection_returns_answer_text() -> None:
    block = LLMStreamBlock()
    block.append_token("## 标题\n\n回答内容")
    block.finalize_markdown()

    selected = block.get_selection(SELECT_ALL)

    assert selected == ("标题\n\n回答内容", "\n")


# 功能：验证非 token 事件后 _current_llm 被重置，下一个 token 开启新块
# 设计：插入 step.started 中断流，验证之前的 block 被 finalize，之后的 llm.token 创建新 LLMStreamBlock
def test_llm_block_resets_after_non_token_event() -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "llm.token", "token": "A", "run_id": "r", "ts": "t"})
    app._handle_event({"type": "step.started", "run_id": "r", "step": 2, "ts": "t"})
    app._handle_event({"type": "llm.token", "token": "B", "run_id": "r", "ts": "t"})

    llm_blocks = [w for w in appended if isinstance(w, LLMStreamBlock)]
    assert len(llm_blocks) == 2
    assert llm_blocks[0]._finalized  # type: ignore[attr-defined]


# 功能：验证 run.started 事件追加 Static widget 且包含 run_id 和 goal
# 设计：monkey-patch _append，断言追加的 widget 的 renderable 包含关键字段
def test_run_started_appends_widget_with_content() -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.started", "run_id": "run-abc", "goal": "do the thing", "ts": "t"
    })

    assert len(appended) == 1
    rendered = appended[0].content
    assert "run-abc" in rendered
    assert "do the thing" in rendered


# 功能：验证 run.finished success 追加包含 "completed" 的 widget
# 设计：monkey-patch _append，检查 rendered 内容包含 completed 和 green
def test_run_finished_success_shows_completed() -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.finished", "run_id": "r", "status": "success", "steps": 3, "ts": "t"
    })

    rendered = appended[0].content
    assert "completed" in rendered
    assert "green" in rendered


# 功能：验证 run.finished failed 追加包含 "failed" 和 red 的 widget
# 设计：与 success 对称，检查颜色标记差异
def test_run_finished_failed_shows_red() -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.finished", "run_id": "r", "status": "failed",
        "steps": 1, "reason": "llm_error", "ts": "t"
    })

    rendered = appended[0].content
    assert "failed" in rendered
    assert "red" in rendered


# 功能：验证 tool.call_started 追加 ToolCallBlock，call_finished 更新其结果
# 设计：直接调用 _handle_event 两次，通过 _pending_tool_blocks 验证状态流转
def test_tool_call_started_and_finished() -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "tool.call_started",
        "tool_use_id": "uid-1",
        "tool_name": "bash",
        "params": {"command": "echo hi"},
        "run_id": "r", "ts": "t",
    })
    assert "uid-1" in app._pending_tool_blocks  # type: ignore[attr-defined]

    app._handle_event({
        "type": "tool.call_finished",
        "tool_use_id": "uid-1",
        "tool_name": "bash",
        "elapsed_ms": 42,
        "output": "hi",
        "run_id": "r", "ts": "t",
    })
    assert "uid-1" not in app._pending_tool_blocks  # type: ignore[attr-defined]
    block = appended[0]
    assert isinstance(block, ToolCallBlock)
    assert block._finished  # type: ignore[attr-defined]
    assert block._output == "hi"  # type: ignore[attr-defined]


# 功能：验证 note_save 成功完成时工具块摘要显示 remembered
# 设计：直接操作 ToolCallBlock，覆盖 note_save 的特殊低噪声展示策略
def test_note_save_tool_block_shows_remembered() -> None:
    block = ToolCallBlock("note_save", {"content": "Python 3.12"})
    block.set_result("saved", 3)
    assert "remembered" in block._summary()  # type: ignore[attr-defined]


# 功能：验证提交用户输入时会追加 user turn，并进入 busy 状态
# 设计：用 fake client 替代 SocketClient，直接调用 on_chat_text_area_submitted，
#       覆盖 TextArea 清空内容 + 设置 busy 占位符的核心状态迁移
async def test_input_submit_appends_user_turn_and_disables_prompt() -> None:
    class _FakeArea:
        def __init__(self) -> None:
            self.disabled = False
            self.border_title = ""
            self.text = "hello"

    class _FakeEvent:
        def __init__(self, area: _FakeArea) -> None:
            self.value = area.text
            self.text_area = area

    class _FakeClient:
        async def send_command(self, method: str, params: dict) -> dict:
            return {"run_id": "run-1"}

    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]
    app._update_header = lambda state: None  # type: ignore[method-assign]
    app._client = _FakeClient()  # type: ignore[assignment]
    app._session_id = "sess-1"

    area = _FakeArea()
    event = _FakeEvent(area)
    await app.on_chat_text_area_submitted(event)  # type: ignore[arg-type]

    assert app._busy  # type: ignore[attr-defined]
    assert area.disabled
    assert area.text == ""
    assert "agent is working" in area.border_title.lower()
    assert appended[0].content == "[bold]>[/bold] hello"


# 功能：验证未知事件类型不抛异常也不追加任何 widget
# 设计：发送 type 为 unknown 的事件，断言 appended 为空
def test_unknown_event_silently_ignored() -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "some.unknown.type", "run_id": "r", "ts": "t"})
    assert appended == []


# 功能：验证权限模式快捷动作在 Ask/Auto 间切换并同步到 daemon
async def test_permission_mode_action_cycles_and_sends_command() -> None:
    calls: list[tuple[str, dict]] = []

    class _FakeClient:
        async def send_command(self, method: str, params: dict) -> dict:
            calls.append((method, params))
            return {"mode": params["mode"]}

    app = JarvisTuiApp("127.0.0.1", 9999)
    app._client = _FakeClient()  # type: ignore[assignment]
    app._session_id = "sess-1"
    app._update_header = lambda state: None  # type: ignore[method-assign]

    await app.action_cycle_permission_mode()
    assert app._permission_mode == "auto"  # type: ignore[attr-defined]
    assert calls[-1] == (
        "permission.set_mode",
        {"session_id": "sess-1", "mode": "auto"},
    )

    await app.action_cycle_permission_mode()
    assert app._permission_mode == "ask"  # type: ignore[attr-defined]
    assert calls[-1] == (
        "permission.set_mode",
        {"session_id": "sess-1", "mode": "ask"},
    )


# 功能：验证 Alt+Tab 和 Windows 可用的 Shift+Tab 都以高优先级切换权限模式
def test_permission_mode_bindings_are_priority_shortcuts() -> None:
    bindings = {
        binding.key: binding
        for binding in JarvisTuiApp.BINDINGS
        if isinstance(binding, Binding)
    }

    assert bindings["alt+tab"].action == "cycle_permission_mode"
    assert bindings["alt+tab"].priority is True
    assert bindings["shift+tab"].action == "cycle_permission_mode"
    assert bindings["shift+tab"].priority is True


# 功能：验证复制回答动作在无鼠标选区时复制最近一条完整回答
def test_copy_answer_action_uses_latest_llm_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    block = LLMStreamBlock()
    block.append_token("可复制的回答")
    copied: list[str] = []

    app._last_llm_block = block  # type: ignore[attr-defined]
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    monkeypatch.setattr(app, "notify", lambda *args, **kwargs: None)

    app.action_copy_answer()

    assert copied == ["可复制的回答"]


# 功能：验证回答复制快捷键提供终端通用键与可靠备用键
def test_copy_answer_bindings_are_priority_shortcuts() -> None:
    bindings = {
        binding.key: binding
        for binding in JarvisTuiApp.BINDINGS
        if isinstance(binding, Binding)
    }

    assert bindings["ctrl+shift+c"].action == "copy_answer"
    assert bindings["ctrl+shift+c"].priority is True
    assert bindings["alt+c"].action == "copy_answer"
    assert bindings["alt+c"].priority is True


# 功能：验证真实挂载后 Ctrl+C 会复制 Markdown 回答选区，而不是退出 TUI
async def test_ctrl_c_copies_selected_markdown_answer() -> None:
    class _CopyTestApp(JarvisTuiApp):
        def on_mount(self) -> None:
            block = LLMStreamBlock()
            block.append_token("## Heading\n\nAnswer **content**")
            block.finalize_markdown()
            self._append(block)
            self._last_llm_block = block

    app = _CopyTestApp("127.0.0.1", 9999)
    copied: list[str] = []
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
    app.notify = lambda *args, **kwargs: None  # type: ignore[method-assign]

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        block = app.query_one(LLMStreamBlock)
        app.screen.selections = {block: SELECT_ALL}

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert copied == ["Heading\n\nAnswer content"]
        assert app.is_running

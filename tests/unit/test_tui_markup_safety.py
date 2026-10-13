from __future__ import annotations

import random
from pathlib import Path

from textual.app import App, ComposeResult
from textual.content import Content
from textual.markup import MarkupError
from textual.selection import SELECT_ALL
from textual.widget import Widget
from textual.widgets import Label

from jarvis.tui.app import (
    JarvisTuiApp,
    LLMStreamBlock,
    PermissionBlock,
    PermissionSelect,
    SelectableStatic,
    SlashCompleteWidget,
    ToolCallBlock,
    _esc,
)

# 历史崩溃样本：未配对闭合标签 + 反引号包裹的方括号 + 中文说明
NASTY = "看这个 `[bold]` 标签、[/dim] 和 [`-c`] 参数，还有 [重要] 说明。\n"


class _OfflineTuiApp(JarvisTuiApp):
    """不启动 socket worker 的 TUI，用于把 widget 真正挂载起来做渲染验证。"""

    def on_mount(self) -> None:
        pass


# 功能：验证 _esc 转义后的文本经 Textual markup 解析后与原文逐字一致
# 设计：覆盖历史崩溃输入（未配对闭合标签、反引号方括号）与反斜杠、括号等边界字符
def test_esc_keeps_text_identical_after_markup_parse() -> None:
    for raw in (NASTY, "[/dim]", "[`code`]", "a\\b", "[", "path/to/file(1).py", "[bold]x[/bold]"):
        assert Content.from_markup(_esc(raw)).plain == raw


# 功能：验证模型流式输出里的 markup 片段不会抛 MarkupError（历史 llm.token 崩溃点）
# 设计：逐字符喂给未挂载的 LLMStreamBlock，断言内容原样保留
def test_llm_stream_block_survives_markup_like_tokens() -> None:
    block = LLMStreamBlock()
    for char in NASTY:
        block.append_token(char)

    assert block._text == NASTY  # type: ignore[attr-defined]
    assert Content(block.content).plain == NASTY


# 功能：验证真实挂载的流式块渲染 markup 片段时既刷新成功也不退出 App
async def test_mounted_llm_stream_block_renders_markup_like_tokens() -> None:
    class _StreamApp(App[None]):
        def compose(self) -> ComposeResult:
            yield LLMStreamBlock()

    app = _StreamApp()
    async with app.run_test(size=(80, 20)) as pilot:
        block = app.query_one(LLMStreamBlock)
        for char in NASTY:
            block.append_token(char)
        await pilot.pause()

        assert "[/dim]" in Content(block.content).plain

        block.finalize_markdown()
        await pilot.pause()

        rendered = block.visual.plain
        assert "[/dim]" in rendered
        assert "[重要]" in rendered
        assert app.is_running


# 功能：验证工具参数/输出含 markup 片段时，点击展开详情不抛 MarkupError
# 设计：挂载 ToolCallBlock 后点击展开，断言详情文本为原文且 App 仍在运行
async def test_tool_detail_expands_with_markup_like_output() -> None:
    params = {"command": "echo '[/dim]'", "note": "[`x`]"}
    output = "[bold]done[/bold]\n[/dim]"

    app = _OfflineTuiApp("127.0.0.1", 9999)
    async with app.run_test(size=(100, 30)) as pilot:
        block = ToolCallBlock("bash", params)
        await app.query_one("#log-view").mount(block)
        await pilot.pause()
        block.set_result(output, 7)
        block.on_click()
        await pilot.pause()

        detail = app.query_one(".detail", SelectableStatic)
        text, _ = detail.get_selection(SELECT_ALL)
        assert output in text
        assert "echo '[/dim]'" in text
        assert app.is_running


# 功能：验证 log.line 事件中带方括号的日志正文不会崩溃且原样展示
def test_log_line_event_escapes_markup_like_message() -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "log.line",
        "level": "ERROR",
        "source": "jarvis.core[1]",
        "message": "MarkupError: closing tag '[/dim]' 不含 [bold]",
        "ts": "t",
    })

    assert len(appended) == 1
    plain = Content.from_markup(appended[0].content).plain
    assert "closing tag '[/dim]' 不含 [bold]" in plain
    assert "jarvis.core[1]" in plain


# 功能：验证权限请求携带方括号参数预览时仍能挂载审批控件（历史崩溃点）
def test_permission_request_with_bracket_preview_mounts() -> None:
    app = JarvisTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    mounted: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]
    app._mount_permission_select = lambda select: mounted.append(select)  # type: ignore[method-assign]

    app._handle_event({
        "type": "permission.requested",
        "tool_use_id": "uid-1",
        "tool_name": "bash",
        "param_preview": "command='echo \"[/dim]\"'",
        "ts": "t",
    })

    assert isinstance(appended[0], PermissionBlock)
    assert isinstance(mounted[0], PermissionSelect)
    assert "[/dim]" in Content.from_markup(appended[0].content).plain


# 功能：验证审批结论渲染方括号预览时不抛异常且保留原文
def test_permission_block_resolve_with_bracket_preview() -> None:
    block = PermissionBlock("uid-1", "bash", "command='echo \"[/dim]\"'")

    block._resolve("always_allow")

    text, _ = block.get_selection(SELECT_ALL)
    assert "[/dim]" in text
    assert "always allowed" in text


# 功能：验证用户输入里的 markup 片段不会被解析（提交后按原文展示）
async def test_user_submit_escapes_markup_like_input() -> None:
    class _FakeArea:
        def __init__(self) -> None:
            self.disabled = False
            self.read_only = False
            self.border_title = ""
            self.text = "[/dim] 这是 [bold] 测试"

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

    await app.on_chat_text_area_submitted(_FakeEvent(_FakeArea()))  # type: ignore[arg-type]

    assert Content.from_markup(appended[0].content).plain == "> [/dim] 这是 [bold] 测试"


# 功能：验证工作区目录名含方括号时顶部标题栏渲染不抛异常且原文可见
# 设计：Windows 的 Path 把 / 当分隔符，样本用不含分隔符的目录名，避免测成路径解析而非转义
async def test_update_header_escapes_workspace_name(tmp_path: Path) -> None:
    app = _OfflineTuiApp("127.0.0.1", 9999, workspace=str(tmp_path / "[bold]ws"))

    async with app.run_test(size=(80, 20)) as pilot:
        app._update_header("ready")
        await pilot.pause()

        header = app.query_one("#header", Label)
        plain = Content.from_markup(header.content).plain
        assert "[bold]ws" in plain
        assert "127.0.0.1:9999" in plain
        assert app.is_running


# 功能：验证 skill 名称/描述含 markup 片段时，斜杠补全弹窗渲染不抛异常
# 设计：把带 [ 的描述挂载成真实 widget，覆盖 on_mount -> _redraw 这条挂载期崩溃路径
async def test_slash_popup_escapes_skill_names() -> None:
    items = [("do[/dim]thing", "desc with [bold]x[/bold]")]

    class _PopupApp(App[None]):
        def compose(self) -> ComposeResult:
            yield SlashCompleteWidget(items)

    app = _PopupApp()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()

        popup = app.query_one(SlashCompleteWidget)
        plain = Content.from_markup(popup.content).plain
        assert "/do[/dim]thing" in plain
        assert "desc with [bold]x[/bold]" in plain
        assert app.is_running


# 功能：验证审批标签取自事件字段时，方括号内容按原文展示且不抛异常
# 设计：直接传带 markup 的 decision（permission.denied 事件可能给出任意字符串）
def test_permission_block_resolve_with_markup_decision() -> None:
    block = PermissionBlock("uid-2", "bash", "command=echo")

    block._resolve("[/dim]")

    text, _ = block.get_selection(SELECT_ALL)
    assert "[/dim]" in text


# 功能：用固定种子随机文本验证 _esc 往返一致，覆盖反斜杠/中文/标签混排
# 设计：历史崩溃输入是模型逐 token 输出的任意文本，随机组合比手工样本更容易撞到边界
def test_esc_roundtrip_fuzz_seeded() -> None:
    rng = random.Random(20261013)
    alphabet = list("[]\\/`#@ abc") + ["[bold]", "[/dim]", "[#ff8c00]", "[/]", "中文", "\n"]
    for _ in range(300):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 20)))
        assert Content.from_markup(_esc(raw)).plain == raw, raw


# 功能：验证渲染类异常被吞掉，不再让整个 TUI 退出
async def test_render_error_does_not_exit_app() -> None:
    app = _OfflineTuiApp("127.0.0.1", 9999)

    async with app.run_test(size=(80, 20)) as pilot:
        app._handle_exception(MarkupError("closing tag '[/dim]' does not match any open tag"))
        await pilot.pause()

        assert app.is_running

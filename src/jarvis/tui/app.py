from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.selection import Selection
from textual.widget import Widget
from textual.widgets import Label, Static, TextArea

from jarvis.core.config import JarvisConfig
from jarvis.core.skills.loader import SkillLoader
from jarvis.core.transport.socket_client import IpcError, SocketClient

log = logging.getLogger(__name__)


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


# 转义外部文本（模型输出、工具参数/输出、日志、用户输入），禁止被当成 Textual/Rich markup 解析。
# 模型输出里常出现 [xxx]、[/dim]、Markdown 片段和反引号，未转义会被 Textual 当成标签：
# 轻则该行渲染异常，重则抛到 Textual 事件循环里让整个 TUI 退出。
# 这里给每个 [ 加反斜杠（Textual 只在 (?<!\)\[ 时才进入标签状态）；
# 不能用 rich.markup.escape：它只处理“[ 后跟 [a-z#/@]”且会把反斜杠翻倍（Textual 不还原）。
def _esc(value: Any) -> str:
    return str(value).replace("[", "\\[")


# 渲染整块不可信文本：优先用带标签的 markup 文本，失败时退化为纯文本。
# 保证异常不逃出 widget 事件回调（Textual 对未处理异常会直接结束 App）。
def _update_markup_or_plain(widget: Static, markup_text: str, plain_text: str) -> None:
    try:
        widget.update(markup_text)
    except Exception:
        log.exception("markup render failed; falling back to plain text")
        widget.update(Text(plain_text))


# 渲染类异常（markup/style/visual 解析失败）只影响显示，不该终止整个 TUI。
def _is_render_error(error: BaseException) -> bool:
    for cls in type(error).__mro__:
        if cls.__name__ in {"MarkupError", "StyleSyntaxError", "VisualError"}:
            return True
    return False


def _read_windows_clipboard() -> str | None:
    """Read Unicode text from the Windows clipboard without extra dependencies."""
    if os.name != "nt":
        return None

    import ctypes

    cf_unicode_text = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool

    if not user32.IsClipboardFormatAvailable(cf_unicode_text):
        return None

    opened = False
    for _ in range(5):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.01)
    if not opened:
        return None

    data_handle: int | None = None
    data_pointer: int | None = None
    try:
        data_handle = user32.GetClipboardData(cf_unicode_text)
        if not data_handle:
            return None
        data_pointer = kernel32.GlobalLock(data_handle)
        if not data_pointer:
            return None
        text = ctypes.wstring_at(data_pointer)
        return text.replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, ValueError):
        log.debug("failed to read Windows clipboard", exc_info=True)
        return None
    finally:
        if data_pointer:
            kernel32.GlobalUnlock(data_handle)
        user32.CloseClipboard()

def _params_str(params: dict[str, Any]) -> str:
    try:
        return json.dumps(params, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        # 参数里出现无法序列化的对象时也要能展示，不能拖垮整个工具块
        return str(params)


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "project_map": ("path", "max_depth"),
        "search_files": ("pattern", "path", "glob"),
        "bash": ("command",),
        "note_save": ("content",),
    }
    if not isinstance(params, dict):
        # 兼容旧事件/异常参数：params 不是 dict 时退化为整段预览
        return _preview(str(params), max_len)
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


class SelectableStatic(Static):
    """Rich 文本块，支持 Textual 鼠标选区复制。"""

    def __init__(self, content: str = "", *args: Any, **kwargs: Any) -> None:
        self._selection_source = content
        super().__init__(content, *args, **kwargs)

    def update(self, content: Any = "", *, layout: bool = True) -> None:
        if isinstance(content, str):
            self._selection_source = content
        super().update(content, layout=layout)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if not self._selection_source:
            return None
        if getattr(self, "_render_markup", True):
            try:
                # 与显示共用 Textual markup 解析器，保证复制出的文本与屏幕一致
                text = Content.from_markup(self._selection_source).plain
            except Exception:
                text = self._selection_source
        else:
            text = self._selection_source
        return selection.extract(text), "\n"


class LLMStreamBlock(Static):
    """在同一个 Static widget 中累积 LLM 流式 token。"""

    can_focus = True
    DEFAULT_CSS = """
    LLMStreamBlock { padding: 0 2; color: $text; }
    LLMStreamBlock:focus { background: $boost; }
    """

    # 初始化为空文本块
    def __init__(self) -> None:
        super().__init__("", markup=False)
        self._text = ""
        self._finalized = False
        self._rendered_text: Text | None = None

    # 追加一个 token 并刷新显示。必须保持 markup=False + 纯字符串更新：
    # 模型输出里的 [bold]、[/dim]、反引号一旦被当成 markup 解析就会抛 MarkupError
    #（历史崩溃现场见 ~/.jarvis/logs/tui.log 的 llm.token 报错）。
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        self.update(self._text)

    # 将累积文本渲染为 Markdown，供流式块结束后显示
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._text.strip():
            self._rendered_text = self._render_markdown_text()
            if self.is_attached:
                self.update(self._rendered_text)
            else:
                self.update(self._rendered_text.plain)

    def _render_markdown_text(self) -> Text:
        """Render Markdown into styled Text so mouse positions map to text offsets."""
        try:
            console = self.app.console
            options = self.app.console_options
            width = self.content_size.width
            if width < 20:
                width = max(20, self.app.size.width - 4)
        except Exception:
            console = Console(width=80, color_system=None)
            options = console.options
            width = 80
        try:
            lines = console.render_lines(
                Markdown(self._text, code_theme="monokai"),
                options.update(width=width, highlight=False),
                pad=False,
            )
        except Exception:
            # Markdown 渲染失败（结构异常/非法代码块）时退化显示原文，不丢整条回答
            log.exception("markdown render failed; falling back to plain text")
            return Text(self._text)
        rendered = Text()
        for line_number, line in enumerate(lines):
            rendered_line = Text()
            for segment in line:
                if not segment.control:
                    rendered_line.append(segment.text, segment.style)
            rendered_line.rstrip()
            rendered.append_text(rendered_line)
            if line_number < len(lines) - 1:
                rendered.append("\n")
        return rendered

    def _rendered_selection_text(self) -> str:
        """Return the same plain text layout displayed by the TUI."""
        rendered = self._rendered_text or self._render_markdown_text()
        return rendered.plain

    # Rich Markdown 不实现 Textual 默认文本提取，显式提供渲染后的文本供鼠标选区复制。
    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if not self._text:
            return None
        return selection.extract(self._rendered_selection_text()), "\n"

    # 点击回答后允许 Alt+C 等快捷键优先复制这一条，而不是总复制最后一条。
    def on_click(self) -> None:
        self.focus()


class ToolCallBlock(Widget):
    """可折叠的工具调用块：折叠时显示摘要，点击后展开完整 params 和 output。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 2; color: $text-muted; }
    ToolCallBlock > .summary { color: $text-muted; }
    ToolCallBlock > .detail { display: none; padding: 0 2 0 4; color: $text-muted; }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    # 初始化工具调用信息
    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield SelectableStatic(self._summary(), classes="summary")
        yield SelectableStatic("", classes="detail")

    # 生成摘要行文本
    def _summary(self) -> str:
        if self._tool_name == "note_save" and self._finished and not self._is_error:
            return f"  [green]remembered[/green]  [dim]{self._elapsed_ms}ms[/dim]"

        params_pre = _param_summary(self._tool_name, self._params)
        line = f"  [dim]tool[/dim] [bold]{_esc(self._tool_name)}[/bold]"
        if params_pre:
            line += f"  [dim]{_esc(params_pre)}[/dim]"
        if self._finished:
            color = "red" if self._is_error else "green"
            status = "failed" if self._is_error else "done"
            hint = "  [dim](click to expand)[/dim]" if self._output else ""
            line += f"  [{color}]{status}[/{color}]  [dim]{self._elapsed_ms}ms[/dim]{hint}"
        return line

    # 工具调用完成时更新结果并刷新摘要（widget 未挂载时跳过 DOM 更新）
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        if self.children:
            try:
                self.query_one(".summary", SelectableStatic).update(self._summary())
            except NoMatches:
                pass
            except Exception:
                # 摘要渲染失败不能影响工具调用本身的状态推进
                log.exception("ToolCallBlock summary render failed tool=%s", self._tool_name)

    # 展开详情的 markup 文本（params/output 是工具原始内容，必须整体转义）
    def _detail_markup(self) -> str:
        return (
            f"[dim]params[/dim]\n{_esc(self._params_full)}\n\n"
            f"[dim]output[/dim]\n{_esc(self._output)}\n\n"
            f"[dim]elapsed:[/dim] {self._elapsed_ms}ms"
        )

    # 展开详情的纯文本兜底版本
    def _detail_plain(self) -> str:
        return (
            f"params\n{self._params_full}\n\n"
            f"output\n{self._output}\n\n"
            f"elapsed: {self._elapsed_ms}ms"
        )

    # 点击时切换展开/折叠状态。展开会渲染工具原始参数/输出，
    # 历史上这里未捕获的 MarkupError 直接让整个 TUI 退出，
    # 因此整段展开逻辑都做兜底：任何异常都不允许逃出事件回调。
    def on_click(self) -> None:
        if not self._finished:
            return
        try:
            if "expanded" in self.classes:
                self.remove_class("expanded")
                return
            detail = self.query_one(".detail", SelectableStatic)
            _update_markup_or_plain(detail, self._detail_markup(), self._detail_plain())
            self.add_class("expanded")
        except Exception:
            log.exception("ToolCallBlock.on_click failed tool=%s", self._tool_name)


class PermissionSelect(Static):
    """内联权限选择控件：挂载在日志流中，键盘焦点无需 ModalScreen。"""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    _CHOICES: tuple[tuple[str, str, str], ...] = (
        ("allow_once",   "Allow once",   "y / 1"),
        ("always_allow", "Always allow", "a / 2"),
        ("deny_once",    "Deny",         "n / 3"),
        ("always_deny",  "Always deny",  "d / 4"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",  "1": "allow_once",
        "a": "always_allow","2": "always_allow",
        "n": "deny_once",   "3": "deny_once",
        "d": "always_deny", "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(self, widget: PermissionSelect, tool_use_id: str, decision: str) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            super().__init__()

    # 初始化控件，存储工具 ID（用于 IPC 回复）
    def __init__(self, tool_use_id: str) -> None:
        super().__init__("")
        self._tool_use_id = tool_use_id
        self._cursor = 0

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()
        log.debug(
            "PermissionSelect.on_mount  can_focus=%s  focused_after=%r",
            self.can_focus,
            self.app.focused,
        )
        self.app.call_after_refresh(self._log_deferred_focus)

    # 在下一帧记录焦点是否真正转移到本控件
    def _log_deferred_focus(self) -> None:
        log.debug(
            "PermissionSelect.deferred_focus  app.focused=%r  has_focus=%s  focusable=%s",
            self.app.focused,
            self.has_focus,
            self.focusable,
        )

    # 焦点到达时记录，用于确认 focus() 是否真正生效
    def on_focus(self, event: events.Focus) -> None:
        log.debug(
            "PermissionSelect.on_focus  has_focus=%s  app.focused=%r",
            self.has_focus,
            self.app.focused,
        )

    # 焦点离开时记录，用于追踪是否被其他控件抢走焦点
    def on_blur(self, event: events.Blur) -> None:
        log.debug("PermissionSelect.on_blur  app.focused=%r", self.app.focused)

    # 生成带光标高亮的选项列表文本
    def _render_ui(self) -> str:
        lines: list[str] = []
        for i, (_, label, key_hint) in enumerate(self._CHOICES):
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ {label}[/bold cyan]  [dim]{key_hint}[/dim]")
            else:
                lines.append(f"    {label}  [dim]{key_hint}[/dim]")
        lines.append("[dim]  ↑↓ navigate   enter confirm[/dim]")
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        log.debug("PermissionSelect.on_key  key=%r  char=%r", event.key, event.character)
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._CHOICES[self._cursor][0])
        else:
            decision = self._KEY_MAP.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        log.debug("PermissionSelect._pick  decision=%s", decision)
        self.post_message(self.Decided(self, self._tool_use_id, decision))


class PermissionBlock(SelectableStatic):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once":   "allowed (once)",
        "always_allow": "always allowed",
        "deny_once":    "denied",
        "always_deny":  "always denied",
        "timeout":      "⏱ timed out",
    }
    LABEL_MAP = _LABEL_MAP

    # 子类提交消息：用户作出权限决策时发布
    class Resolved(Message):
        def __init__(self, block: PermissionBlock, decision: str) -> None:
            self.block = block
            self.decision = decision
            super().__init__()

    # 初始化审批块，记录工具 ID、名称和参数预览
    def __init__(self, tool_use_id: str, tool_name: str, param_preview: str) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self._resolved = False
        try:
            pending_text = self._pending_text()
        except Exception:
            log.exception("PermissionBlock pending text render failed tool=%s", self._tool_name)
            pending_text = f"permission  {_esc(self._tool_name)}"
        super().__init__(pending_text, classes="log-line")

    def _pending_text(self) -> str:
        preview = f"  [dim]{_esc(self._param_preview)}[/dim]" if self._param_preview else ""
        return f"[bold red]? permission[/bold red]  [bold]{_esc(self._tool_name)}[/bold]{preview}"

    # 将块收缩为单行摘要并发布 Resolved 消息
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        allowed = decision in ("allow_once", "always_allow")
        icon = "[bold green]✓[/bold green]" if allowed else "[bold red]✗[/bold red]"
        label = _esc(self._LABEL_MAP.get(decision, decision))
        preview = f"  [dim]{_esc(self._param_preview)}[/dim]" if self._param_preview else ""
        name = _esc(self._tool_name)
        try:
            self.update(
                f"{icon} permission  [bold]{name}[/bold]{preview}  [dim]{label}[/dim]"
            )
        except Exception:
            log.exception("PermissionBlock._resolve render failed tool=%s", self._tool_name)
            self.update(f"permission  {name}  {label}")
        self.post_message(self.Resolved(self, decision))


class SlashCompleteWidget(Static):
    """斜杠命令自动补全弹出框：输入 / 时显示可用 skill 列表并支持键盘筛选与选择。"""

    can_focus = False

    DEFAULT_CSS = """
    SlashCompleteWidget {
        height: auto;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    # 用户选中某条命令时发布
    class Selected(Message):
        # 初始化，携带被选中的 skill 名称
        def __init__(self, skill_name: str) -> None:
            self.skill_name = skill_name
            super().__init__()

    # 初始化，接收全量 (name, description) 列表
    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__("")
        self._all_items = items
        self._filtered: list[tuple[str, str]] = list(items)
        self._cursor = 0

    # 根据查询字符串筛选列表，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = query.lower()
        self._filtered = [(n, d) for n, d in self._all_items if not q or q in n.lower()]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上移动光标并重新渲染
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下移动光标并重新渲染
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 选中当前光标项并发布 Selected 消息
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor][0]))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表，高亮当前光标项
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        lines: list[str] = []
        for i, (name, desc) in enumerate(self._filtered):
            # skill 名称与描述来自仓库文件，可能含 [xxx]/反引号，必须转义后再拼进 markup
            name_part = _esc(name)
            desc_part = f"  [dim]{_esc(desc)}[/dim]" if desc else ""
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ /{name_part}[/bold cyan]{desc_part}")
            else:
                lines.append(f"    [cyan]/{name_part}[/cyan]{desc_part}")
        lines.append("[dim]  ↑↓ navigate   tab/enter select   esc dismiss[/dim]")
        self.update("\n".join(lines))


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round $surface-lighten-2;
        background: $background;
        padding: 0 1;
        margin: 1 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round $accent;
        background: $background;
    }
    """

    # 子类自定义的提交消息，供宿主 App 监听
    class Submitted(Message):
        def __init__(self, area: ChatTextArea) -> None:
            self.text_area = area
            self.value = area.text
            super().__init__()

    # 输入内容以 / 开头且无空格时发布，query 为 / 之后的字符串（可为空串）；None 表示收起弹窗
    class SlashChanged(Message):
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    # Textual's clipboard is process-local in some Windows terminals.
    # Prefer the native Windows clipboard for Ctrl+V, then fall back to Textual.
    def action_paste(self) -> None:
        if self.read_only:
            return
        clipboard = _read_windows_clipboard()
        if clipboard is None:
            clipboard = self.app.clipboard
        if result := self._replace_via_keyboard(clipboard, *self.selection):
            self.move_cursor(result.end_location)

    # 文本变化时检测 / 前缀，通知宿主 App 更新自动补全弹窗
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and " " not in text:
            self.post_message(ChatTextArea.SlashChanged(query=text[1:]))
        else:
            self.post_message(ChatTextArea.SlashChanged(query=None))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            if popup is not None and popup.has_selection():
                popup.select_current()
                return
            if self.text.strip():
                self.post_message(self.Submitted(self))
            return
        if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
            return
        if popup is not None:
            if key == "up":
                event.stop()
                event.prevent_default()
                popup.move_up()
                return
            elif key == "down":
                event.stop()
                event.prevent_default()
                popup.move_down()
                return
            elif key == "tab":
                event.stop()
                event.prevent_default()
                popup.select_current()
                return
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self.post_message(ChatTextArea.SlashChanged(query=None))
                return
        await super()._on_key(event)


class JarvisTuiApp(App[None]):
    """Jarvis TUI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "Jarvis"
    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
        Binding("ctrl+shift+c", "copy_answer", "copy answer", priority=True),
        Binding("alt+c", "copy_answer", "copy answer", priority=True),
        Binding("alt+tab", "cycle_permission_mode", "permission mode", priority=True),
        Binding("shift+tab", "cycle_permission_mode", "permission mode", priority=True),
    ]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    #banner { padding: 1 2 0 2; }
    Static.user-turn { color: $text; padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    """

    _BANNER = (
        "[bold #ff8c00]     ██╗  █████╗  ██████╗  ██╗   ██╗ ██╗ ███████╗[/bold #ff8c00]\n"
        "[bold #ff8c00]     ██║ ██╔══██╗ ██╔══██╗ ██║   ██║ ██║ ██╔════╝[/bold #ff8c00]\n"
        "[bold #ff8c00]     ██║ ███████║ ██████╔╝ ██║   ██║ ██║ ███████╗[/bold #ff8c00]\n"
        "[bold #ff8c00]██   ██║ ██╔══██║ ██╔══██╗ ╚██╗ ██╔╝ ██║ ╚════██║[/bold #ff8c00]\n"
        "[bold #ff8c00]╚█████╔╝ ██║  ██║ ██║  ██║  ╚████╔╝  ██║ ███████║[/bold #ff8c00]\n"
        "[bold #ff8c00] ╚════╝  ╚═╝  ╚═╝ ╚═╝  ╚═╝   ╚═══╝   ╚═╝ ╚══════╝[/bold #ff8c00]\n"
        "[dim]JarvisAgent  ·  输入消息开始对话  ·  / 触发 skill"
        "  ·  Ctrl+Shift+C 复制  ·  Ctrl+Q 退出[/dim]"
    )

    # 初始化连接参数和 TUI 内部状态
    def __init__(
        self,
        host: str,
        port: int,
        replay_run_id: str | None = None,
        workspace: str | None = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        self._replay_run_id = replay_run_id
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._last_llm_block: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._session_id: str | None = None
        self._busy = False
        self._permission_mode = "ask"
        self._connection_state = "connecting"
        self._last_context_pct: float = 0.0
        self._slash_items: list[tuple[str, str]] = []
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time

    def compose(self) -> ComposeResult:
        yield Label("[bold]Jarvis[/bold]  [dim]connecting...[/dim]", id="header")
        yield VerticalScroll(id="log-view")
        yield ChatTextArea(id="prompt", show_line_numbers=False)

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(SelectableStatic(self._BANNER, id="banner"))
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "connecting..."

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [("compact", "compress context window")]
        try:
            loader = SkillLoader(self._workspace)
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append((skill.name, desc))
        except Exception:
            pass
        return items

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return
        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中自动补全项后将 /{name} 填入输入框并移除弹窗
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass

    # 记录按键焦点；当 PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        log.debug("App.on_key  key=%r  focused=%r", event.key, self.focused)
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
            if select.has_focus:
                return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里
            key = event.key
            decision = PermissionSelect._KEY_MAP.get(key)
            if decision:
                event.stop()
                select._pick(decision)
            elif key in ("up", "k"):
                event.stop()
                select._cursor = (select._cursor - 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key in ("down", "j"):
                event.stop()
                select._cursor = (select._cursor + 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key == "enter":
                event.stop()
                select._pick(PermissionSelect._CHOICES[select._cursor][0])
        except Exception:
            pass

    # 退出前尽力关闭当前 session，失败也不阻塞 TUI 退出
    async def action_quit(self) -> None:
        if self._client is not None and self._session_id is not None:
            try:
                await self._client.send_command("session.close", {"session_id": self._session_id})
            except (IpcError, RuntimeError, OSError):
                self._append(SelectableStatic("[yellow]warning: failed to close session[/yellow]"))
        self.exit()

    # 在 ask/auto 间切换当前 session 的权限模式。
    async def action_cycle_permission_mode(self) -> None:
        self._permission_mode = "auto" if self._permission_mode == "ask" else "ask"
        self._update_header(self._connection_state)
        if self._permission_mode == "auto":
            self._dismiss_pending_permissions()
        if self._client is None or self._session_id is None:
            return
        try:
            await self._client.send_command(
                "permission.set_mode",
                {"session_id": self._session_id, "mode": self._permission_mode},
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(SelectableStatic(
                f"[yellow]permission mode sync failed: {_esc(exc)}[/yellow]",
                classes="log-line",
            ))

    # 优先复制鼠标选区；无选区时复制当前聚焦或最近一条模型回答。
    def action_copy_answer(self) -> None:
        selected: str | None = None
        focused: Widget | None = None
        try:
            selected = self.screen.get_selected_text()
            focused = self.focused
        except Exception:
            pass

        block = focused if isinstance(focused, LLMStreamBlock) else None
        if block is None:
            block = self._last_llm_block
        text = selected or (block._text if block is not None else "")
        if not text:
            self.notify("No answer to copy", severity="warning")
            return
        self.copy_to_clipboard(text)
        self.notify("Copied")

    # Auto 模式由后端统一放行挂起请求；TUI 只负责清理旧审批控件和展示状态。
    def _dismiss_pending_permissions(self) -> None:
        for perm_block in self._pending_permission_blocks.values():
            perm_block._resolve("allow_once")
        self._pending_permission_blocks.clear()
        try:
            for select in list(self.query(PermissionSelect)):
                select.remove()
        except Exception:
            pass
        prompt = self._prompt()
        if prompt is None:
            return
        if self._busy:
            prompt.disabled = True
            prompt.border_title = "agent is working..."
        else:
            prompt.disabled = False
            prompt.read_only = False
            prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            prompt.focus()

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return
        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(
                SelectableStatic(
                    "[yellow]agent busy or disconnected[/yellow]",
                    classes="log-line",
                )
            )
            return
        self._busy = True
        prompt = event.text_area
        prompt.text = ""
        prompt.disabled = True
        prompt.read_only = False
        prompt.border_title = "agent is working..."
        self._append(SelectableStatic(f"[bold]>[/bold] {_esc(content)}", classes="user-turn"))
        self._update_header("running")
        self.run_worker(self._do_send_message(content), name="send_message", exclusive=False)

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return
        self._append(SelectableStatic("[dim]⚡ compacting context...[/dim]", classes="log-line"))
        try:
            result = await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": ""},
            )
            summary_tokens = result.get("summary_tokens", 0)
            saved_tokens = result.get("saved_tokens", 0)
            self._last_context_pct = 0.0
            self._append(SelectableStatic(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]summary={_esc(summary_tokens)} tokens"
                f"  saved≈{_esc(saved_tokens)} tokens[/dim]",
                classes="log-line",
            ))
        except (IpcError, RuntimeError, OSError) as e:
            self._append(SelectableStatic(
                f"[red]compact error: {_esc(e)}[/red]", classes="log-line",
            ))

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(self, content: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command(
                "session.send_message",
                {"session_id": self._session_id, "content": content},
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            self._update_header("ready")
            self._append(SelectableStatic(f"[red]send error: {_esc(e)}[/red]", classes="log-line"))

    # 处理内联审批控件的用户决策：发送 IPC 响应并恢复输入框
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        tool_use_id = msg.tool_use_id
        decision = msg.decision
        log.info("permission decided tool_use_id=%s decision=%s", tool_use_id, decision)
        try:
            msg.widget.remove()
            perm_block = self._pending_permission_blocks.pop(tool_use_id, None)
            if perm_block is not None:
                perm_block._resolve(decision)
            if self._client is not None:
                try:
                    await self._client.send_command(
                        "permission.respond",
                        {"tool_use_id": tool_use_id, "decision": decision},
                    )
                except (IpcError, RuntimeError, OSError):
                    pass
            if not self._pending_permission_blocks:
                p = self._prompt()
                if p is not None:
                    p.disabled = False
                    p.read_only = False
                    p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    p.focus()
        except Exception:
            log.exception("on_permission_select_decided failed tool_use_id=%s", tool_use_id)

    # 向日志视图追加一个 widget 并滚动到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 结束当前 LLM 流式块（下一个 token 将开启新块）
    def _break_llm(self) -> None:
        if self._current_llm is not None:
            self._current_llm.finalize_markdown()
        self._current_llm = None

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框，便于组件测试中未挂载时跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except Exception:
            return None

    # 生成 context 占用率的彩色进度条字符串
    def _render_ctx_bar(self, pct: float) -> str:
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        label = f"ctx:{pct * 100:.1f}%"
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"
        return f"[{color}]{label} {bar}[/{color}]"

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        self._connection_state = state
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        session = f"  [dim]{_esc(self._session_id)}[/dim]" if self._session_id else ""
        color = {
            "ready": "green",
            "running": "yellow",
            "disconnected": "red",
            "connecting": "dim",
        }.get(state, "dim")
        mode_badge = (
            "[bold green]AUTO[/bold green]"
            if self._permission_mode == "auto"
            else "[yellow]ASK[/yellow]"
        )
        try:
            header.update(
                f"[bold]Jarvis[/bold]  [cyan]{_esc(self._workspace.name)}[/cyan]"
                f"  [dim]{_esc(self._host)}:{_esc(self._port)}[/dim]"
                f"{session}  [{color}]{_esc(state)}[/{color}]  permission:{mode_badge}"
            )
        except Exception:
            # 顶部标题栏渲染失败不该影响会话主流程
            log.exception("header render failed state=%s", state)

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连
    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                log.warning("connection refused %s:%s, retrying", self._host, self._port)
                self._update_header("disconnected")
                await asyncio.sleep(2)
                continue

            log.info("connected to %s:%s", self._host, self._port)
            self._client = client
            self._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]) -> None:
                self._handle_event(event)

            client.on_event(on_event)

            try:
                loop_task.add_done_callback(
                    lambda t: log.error("loop_task failed: %s", t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
                params: dict[str, Any] = {
                    "topics": [
                        "session.*",
                        "run.*",
                        "step.*",
                        "tool.*",
                        "llm.token",
                        "llm.usage",
                        "log.*",
                        "permission.*",
                        "context.*",
                        "subagent.*",
                        "skill.*",
                    ],
                    "scope": "global",
                }
                if self._replay_run_id is not None:
                    params["replay_from_run"] = self._replay_run_id
                await client.send_command("event.subscribe", params)
                created = await client.send_command("session.create", {"mode": "chat"})
                self._session_id = str(created["session_id"])
                await client.send_command(
                    "permission.set_mode",
                    {"session_id": self._session_id, "mode": self._permission_mode},
                )
                log.info("session created session_id=%s", self._session_id)
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = False
                    prompt.read_only = False
                    prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    prompt.focus()
                self._update_header("ready")
                await loop_task
            except IpcError as e:
                try:
                    header.update(
                        f"[bold]Jarvis[/bold]  [red]subscribe error: {_esc(e)}[/red]"
                    )
                except Exception:
                    log.exception("header render failed")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._client = None
                self._session_id = None
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.read_only = False
                    prompt.border_title = "disconnected, retrying..."
                self._break_llm()
                await client.close()

            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 兜底：渲染类异常（markup/style/visual 解析失败）只影响显示，不该让 TUI 直接退出。
    # Textual 默认对未处理异常会打印 traceback 并结束 App；其余异常仍走默认处理，
    # 避免掩盖真正的逻辑 bug。
    def _handle_exception(self, error: Exception) -> None:
        if _is_render_error(error):
            log.exception("render error swallowed to keep TUI alive: %s", error)
            try:
                self.notify(f"render error: {error}", severity="error")
            except Exception:
                pass
            return
        super()._handle_exception(error)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "llm.token":
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
                self._last_llm_block = llm_block
            self._current_llm.append_token(token)
            return

        self._break_llm()

        if t == "session.waiting_for_input":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()
            self._update_header("ready")

        elif t == "session.closed":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("disconnected")

        elif t == "run.started":
            run_id = _esc(event.get("run_id", ""))
            goal = _esc(_preview(str(event.get("goal", "")), 96))
            self._append(SelectableStatic(
                f"[dim]run[/dim]  [cyan]{run_id}[/cyan]  [dim]{goal}[/dim]",
                classes="run-header",
            ))

        elif t == "skill.invoked":
            skill_name = _esc(event.get("skill_name", ""))
            arguments = str(event.get("arguments", ""))
            args_preview = _esc(_preview(arguments, 80)) if arguments else ""
            args_part = f"  [dim]{args_preview}[/dim]" if args_preview else ""
            self._append(SelectableStatic(
                f"[bold cyan]/{skill_name}[/bold cyan]{args_part}",
                classes="log-line",
            ))

        elif t == "subagent.started":
            run_id = event.get("run_id", "")
            description = str(event.get("description", ""))
            self._subagent_run_ids[run_id] = description
            self._subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            desc_preview = _esc(_preview(description, 72))
            self._append(SelectableStatic(
                f"[dim]┌─[/dim] [cyan]{desc_preview}[/cyan]  [dim]{_esc(short_id)}[/dim]",
                classes="log-line",
            ))

        elif t == "subagent.finished":
            run_id = event.get("run_id", "")
            status = event.get("status", "")
            description = str(self._subagent_run_ids.pop(run_id, event.get("description", "")))
            start = self._subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{_esc(_preview(description, 72))}[/cyan]{elapsed}"
            if status == "success":
                self._append(SelectableStatic(
                    f"[dim]└─[/dim] [bold green]✓[/bold green] {desc_part}",
                    classes="log-line",
                ))
            else:
                self._append(SelectableStatic(
                    f"[dim]└─[/dim] [bold red]✗[/bold red] {desc_part}",
                    classes="log-line",
                ))

        elif t == "step.started":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            step = _esc(event.get("step", ""))
            self._append(SelectableStatic(
                f"[dim]step {step}[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = event.get("run_id", "")
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
            self._pending_tool_blocks[tool_use_id] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(output, elapsed_ms)

        elif t == "tool.call_failed":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error_msg = str(event.get("error_message") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            if status == "success":
                self._append(SelectableStatic(
                    f"[bold green]✓ completed[/bold green]  [dim]{_esc(steps)} steps[/dim]",
                    classes="run-ok",
                ))
            else:
                detail = f"  [dim]{_esc(reason)}[/dim]" if reason else ""
                self._append(SelectableStatic(
                    f"[bold red]✗ failed[/bold red]{detail}  [dim]{_esc(steps)} steps[/dim]",
                    classes="run-err",
                ))

        elif t == "llm.usage":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            pct = float(event.get("context_pct") or 0.0)
            self._last_context_pct = pct
            ctx_bar = self._render_ctx_bar(pct)
            self._append(SelectableStatic(
                f"[dim]  tokens  "
                f"in={_esc(event.get('input_tokens'))} "
                f"out={_esc(event.get('output_tokens'))} "
                f"cache={_esc(event.get('cache_read_input_tokens'))}[/dim]"
                f"  {ctx_bar}",
                classes="usage",
            ))

        elif t == "context.compacted":
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            self._last_context_pct = 0.0
            self._append(SelectableStatic(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]original≈{_esc(orig)} tokens → summary={_esc(summary)} tokens[/dim]",
                classes="log-line",
            ))

        elif t == "permission.requested":
            if self._permission_mode == "auto":
                log.debug(
                    "ignoring stale permission request in auto mode id=%s",
                    event.get("tool_use_id", ""),
                )
                return
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            try:
                _focused_repr = repr(self.focused)
            except Exception:
                _focused_repr = "?"
            log.info(
                "permission.requested tool=%s id=%s  app.focused=%s",
                tool_name, tool_use_id, _focused_repr,
            )
            perm_block = PermissionBlock(tool_use_id, tool_name, param_preview)
            self._pending_permission_blocks[tool_use_id] = perm_block
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"
            self._append(perm_block)
            select = PermissionSelect(tool_use_id)
            self._mount_permission_select(select)
            log.debug(
                "PermissionSelect mounted before #prompt  pending=%d",
                len(self._pending_permission_blocks),
            )

        elif t == "permission.denied":
            # 处理超时或断连等非用户交互触发的 deny。
            # 用户主动 deny 已由 on_permission_select_decided 处理。
            tool_use_id = str(event.get("tool_use_id", ""))
            decision = str(event.get("decision", "denied"))
            if tool_use_id in self._pending_permission_blocks:
                perm_block = self._pending_permission_blocks.pop(tool_use_id)
                perm_block._resolve(decision)
                try:
                    select = self.query_one(PermissionSelect)
                    select.remove()
                except Exception:
                    pass
                if not self._pending_permission_blocks:
                    p = self._prompt()
                    if p is not None:
                        p.disabled = False
                        p.read_only = False
                        p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                        p.focus()

        elif t == "log.line":
            # 日志正文常包含异常堆栈/路径/括号，必须整体转义后再拼进 markup
            level = _esc(event.get("level", "INFO"))
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(SelectableStatic(
                f"[{color}]{level}[/{color}]  "
                f"[dim]{_esc(event.get('source', ''))}[/dim]  {_esc(event.get('message', ''))}",
                classes="log-line",
            ))


# TUI 入口：读取配置并启动 JarvisTuiApp
def run(config: JarvisConfig, replay_run_id: str | None = None) -> None:
    app = JarvisTuiApp(
        config.host,
        config.port,
        replay_run_id=replay_run_id,
        workspace=str(config.workspace),
    )
    app.run()

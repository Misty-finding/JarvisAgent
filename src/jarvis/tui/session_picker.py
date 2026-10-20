from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView


class SessionItem(ListItem):
    # 构建会话列表项，运行中的会话不可被选择
    def __init__(self, session: dict[str, Any]) -> None:
        self.session_id = str(session["session_id"])
        busy = bool(session.get("busy"))
        state = "正在运行" if busy else {
            "interrupted": "上次中断", "failed": "上次失败",
        }.get(str(session.get("last_run_state")), "可继续")
        title = str(session.get("title") or "未命名会话").replace("\n", " ")
        updated = str(session.get("updated_at", ""))
        super().__init__(Label(
            f"{title[:80]}  ·  {state}\n{self.session_id}  ·  {updated}",
            markup=False,
        ), disabled=busy)


class SessionPicker(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "取消")]
    DEFAULT_CSS = """
    SessionPicker { align: center middle; background: $background 70%; }
    #session-picker { width: 90%; max-width: 100; height: 85%; border: round $accent;
        background: $surface; padding: 1 2; }
    #session-picker-title { text-style: bold; margin-bottom: 1; }
    #session-list { height: 1fr; margin: 1 0; }
    SessionItem { height: auto; padding: 1; }
    SessionItem Label { width: 1fr; }
    #session-picker-hint { color: $text-muted; }
    #session-picker-warning { color: $warning; }
    #new-session { margin-top: 1; }
    """

    # 保存当前项目的会话列表与读取警告
    def __init__(self, sessions: list[dict[str, Any]], warnings: list[str]) -> None:
        super().__init__()
        self._sessions = sessions
        self._warnings = warnings

    # 组合历史选择界面，空列表时仍允许显式新建会话
    def compose(self) -> ComposeResult:
        with Vertical(id="session-picker"):
            yield Label("恢复历史会话", id="session-picker-title")
            yield Label("↑ ↓ 选择 · Enter 恢复 · Esc 退出", id="session-picker-hint")
            if self._warnings:
                yield Label("\n".join(self._warnings), markup=False, id="session-picker-warning")
            if not self._sessions:
                yield Label("当前项目还没有可恢复的聊天会话。", markup=False)
            yield ListView(*(SessionItem(session) for session in self._sessions), id="session-list")
            yield Button("新建会话", id="new-session")

    # 打开时将焦点放到列表或新建按钮
    def on_mount(self) -> None:
        if any(not session.get("busy") for session in self._sessions):
            self.query_one(ListView).focus()
        else:
            self.query_one(Button).focus()

    # 选中历史会话后返回其 ID，运行中的会话保持不可恢复
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SessionItem) and not event.item.disabled:
            self.dismiss(event.item.session_id)

    # 显式新建返回空字符串，与取消恢复相区分
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-session":
            self.dismiss("")

    # 取消选择时退出本次恢复入口
    def action_cancel(self) -> None:
        self.dismiss(None)

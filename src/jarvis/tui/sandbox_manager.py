from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from jarvis.core.transport.socket_client import IpcError, SocketClient


class SandboxManager(ModalScreen[None]):
    """User-only inventory; task cancellation never calls its destruction action."""

    BINDINGS = [Binding("escape", "close", "关闭", priority=True)]
    DEFAULT_CSS = """
    SandboxManager { align: center middle; background: $background 70%; }
    #sandbox-manager { width: 100; max-width: 96%; height: 85%;
        border: round $accent; background: $surface; padding: 1 2; }
    #sandbox-title { text-style: bold; margin-bottom: 1; }
    #sandbox-status { height: auto; margin: 1 0; }
    #sandbox-list { height: 1fr; }
    .sandbox-row { height: auto; border-bottom: solid $primary; padding: 1 0; }
    .sandbox-details { height: auto; }
    .sandbox-actions { height: auto; margin-top: 1; }
    .sandbox-actions Button { margin-right: 1; }
    .sandbox-error { color: $error; height: auto; }
    #sandbox-footer { height: auto; margin-top: 1; }
    """

    def __init__(self, client: SocketClient, current_session_id: str | None) -> None:
        super().__init__()
        self._client = client
        self._session_id = current_session_id
        self._items: list[dict[str, Any]] = []
        self._working = False
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="sandbox-manager"):
            yield Label("沙箱管理", id="sandbox-title")
            yield Label("默认永久保留。销毁前保存变更到 Core，不会自动写回本地。", markup=False)
            yield Label("正在加载…", id="sandbox-status", markup=False)
            yield VerticalScroll(id="sandbox-list")
            with Horizontal(id="sandbox-footer"):
                yield Button("刷新", id="sandbox-refresh")
                yield Button("关闭", id="sandbox-close")

    def on_mount(self) -> None:
        self.run_worker(self._refresh(), name="sandbox-refresh")

    async def _refresh(self) -> None:
        self._loading = True
        try:
            result = await self._client.send_command(
                "sandbox.list", {"current_session_id": self._session_id}
            )
            self._items = sorted(result["sandboxes"], key=lambda item: not item.get("current"))
            listing = self.query_one("#sandbox-list", VerticalScroll)
            await listing.remove_children()
            for index, item in enumerate(self._items):
                current = "当前会话 · " if item.get("current") else ""
                title = item.get("title") or "未命名会话"
                session = item.get("session_id") or "未关联会话"
                project = item.get("workspace") or "未知项目"
                details = (
                    f"{current}{title}\n会话：{session}\n项目：{project}\n"
                    f"沙箱：{item['sandbox_id']} · {item.get('state', 'running')}"
                )
                discard = Button("放弃内容并销毁", id=f"discard-{index}", variant="error")
                discard.display = False
                await listing.mount(
                    Vertical(
                        Label(details, markup=False, classes="sandbox-details"),
                        Horizontal(
                            Button("销毁", id=f"destroy-{index}", variant="error"),
                            discard,
                            classes="sandbox-actions",
                        ),
                        Label("", id=f"error-{index}", markup=False, classes="sandbox-error"),
                        classes="sandbox-row",
                    )
                )
            self.query_one("#sandbox-status", Label).update(
                f"共 {len(self._items)} 个沙箱，当前会话优先显示。" if self._items else "暂无沙箱。"
            )
        except (IpcError, RuntimeError, OSError, KeyError) as exc:
            self.query_one("#sandbox-status", Label).update(f"加载失败：{exc}")
        finally:
            self._loading = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        name = event.button.id or ""
        if name == "sandbox-close":
            self.action_close()
        elif not self._working and not self._loading and name == "sandbox-refresh":
            self.run_worker(self._refresh(), name="sandbox-refresh", exclusive=True)
        elif not self._working and not self._loading and name.startswith(("destroy-", "discard-")):
            index = int(name.split("-")[1])
            self._working = True
            self.run_worker(
                self._destroy(index, dict(self._items[index]), discard=name.startswith("discard-")),
                name="sandbox-destroy",
            )

    async def _destroy(self, index: int, item: dict[str, Any], *, discard: bool) -> None:
        self._working = True
        for button in self.query(Button):
            button.disabled = True
        self.query_one("#sandbox-status", Label).update("正在停止任务并销毁沙箱…")
        try:
            result = await self._client.send_command(
                "sandbox.manage",
                {
                    "action": "destroy",
                    "sandbox_id": item["sandbox_id"],
                    "discard_changes": discard,
                },
            )
            saved = result["data"].get("review_path")
            self.notify(f"沙箱已销毁。变更已保存：{saved}" if saved else "沙箱已销毁。", timeout=10)
            await self._refresh()
        except (IpcError, RuntimeError, OSError, KeyError) as exc:
            self.query_one(f"#error-{index}", Label).update(
                f"未销毁：{exc}\n可修复后重试；确认不再需要沙箱内容时，可选择放弃内容并销毁。"
            )
            self.query_one(f"#discard-{index}", Button).display = True
            self.query_one("#sandbox-status", Label).update("销毁未完成，沙箱仍保留。")
        finally:
            self._working = False
            for button in self.query(Button):
                button.disabled = False

    def action_close(self) -> None:
        if not self._working:
            self.dismiss(None)

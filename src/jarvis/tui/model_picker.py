from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView


class ModelItem(ListItem):
    # 显示模型名称和当前选择标记，保留用于切换的配置别名
    def __init__(self, model: dict[str, Any], selected: str) -> None:
        self.model_name = str(model["name"])
        label = str(model.get("display_name") or model["model"])
        if self.model_name == "default":
            label += "（默认）"
        super().__init__(
            Label(label, markup=False, classes="model-name"),
            Label("✓" if self.model_name == selected else "", classes="model-check"),
        )


class ModelPicker(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "取消", priority=True)]
    DEFAULT_CSS = """
    ModelPicker { align: center middle; background: $background 70%; }
    #model-picker { width: 64; max-width: 95%; height: auto; max-height: 85%;
        border: round $accent; background: $surface; padding: 1 2; }
    #model-picker-title { text-style: bold; margin-bottom: 1; }
    #model-list { height: auto; max-height: 24; margin-bottom: 1; }
    ModelItem { layout: horizontal; height: 2; padding: 0 1; }
    .model-name { width: 1fr; }
    .model-check { width: 2; color: $success; text-style: bold; }
    #model-picker-hint { color: $text-muted; height: auto; }
    """

    # 保存可选模型和当前模型，菜单只展示服务端已配置的条目
    def __init__(self, models: list[dict[str, Any]], selected: str) -> None:
        super().__init__()
        self._models = models
        self._selected = selected

    # 组合模型菜单，支持鼠标选择和键盘导航
    def compose(self) -> ComposeResult:
        with Vertical(id="model-picker"):
            yield Label("选择模型", id="model-picker-title")
            yield ListView(
                *(ModelItem(model, self._selected) for model in self._models),
                id="model-list",
            )
            yield Label("↑ ↓ 选择 · Enter 确认 · Esc 取消", id="model-picker-hint")

    # 打开时将光标定位到当前模型并聚焦列表
    def on_mount(self) -> None:
        listing = self.query_one(ListView)
        listing.index = next(
            (i for i, model in enumerate(self._models) if model["name"] == self._selected), 0,
        ) if self._models else None
        listing.focus()

    # 确认后返回模型别名，由宿主完成会话切换和错误处理
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ModelItem):
            self.dismiss(event.item.model_name)

    # 取消菜单时保留当前模型
    def action_cancel(self) -> None:
        self.dismiss(None)

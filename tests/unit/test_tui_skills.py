from __future__ import annotations

from pathlib import Path

import pytest
from textual.content import Content

from jarvis.core.skills.loader import SkillLoader
from jarvis.tui.app import ChatTextArea, JarvisTuiApp, SlashCompleteWidget


class _OfflineApp(JarvisTuiApp):
    # 使用真实界面但关闭网络连接，以隔离补全交互验证
    async def _socket_loop(self) -> None:
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = False
        prompt.focus()


# 功能：30 多项补全能够滚动选择，搜索后选项正确且关闭重开能发现新增 Skill
# 设计：使用真实 TUI 的输入消息和按键，验证弹窗、光标与输入框的集成行为
async def test_tui_skill_menu_paginates_and_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = Path(__file__).resolve().parents[2] / ".agents" / "skills"
    monkeypatch.setattr(SkillLoader, "_directories", lambda self: [
        library, self.workspace / ".agents" / "skills",
    ])
    app = _OfflineApp("127.0.0.1", 1, workspace=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.focus()
        prompt.text = "/"
        await pilot.pause()
        popup = app.query_one(SlashCompleteWidget)
        total = len(popup._filtered)
        assert total == 33  # 30 个仓库内编码 Skill 和三个内建命令
        for _ in range(total - 1):
            popup.move_down()
        plain = Content.from_markup(popup.content).plain
        assert f"{total}/{total}" in plain
        assert popup._filtered[-1][0] in plain
        assert len(plain.splitlines()) <= 9
        await pilot.press("escape")
        await pilot.pause()
        root = tmp_path / ".agents" / "skills" / "newly-added"
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            "---\nname: newly-added\ndescription: New skill visible without restarting TUI\n---\nbody",
            encoding="utf-8",
        )
        prompt.text = "/newly"
        await pilot.pause()
        popup = app.query_one(SlashCompleteWidget)
        assert popup._filtered == [("newly-added", "New skill visible without restarting TUI")]
        await pilot.press("tab")
        await pilot.pause()
        assert prompt.text == "/newly-added "

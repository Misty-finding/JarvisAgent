from __future__ import annotations

import sys
from unittest.mock import Mock

import pytest

from jarvis.cli import main as cli_main
from jarvis.core.config import JarvisConfig


def test_bare_jarvis_launches_tui_for_current_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = JarvisConfig()
    launch = Mock()
    monkeypatch.setattr(sys, "argv", ["jarvis"])
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", Mock())
    monkeypatch.setattr(cli_main, "cmd_launch", launch)

    cli_main.main()

    launch.assert_called_once_with(config, replay_run_id=None)


# 功能：验证恢复入口支持历史选择和指定 ID 两种形式
# 设计：只替换启动函数，覆盖真实 argparse 分支且不启动 daemon 或终端
@pytest.mark.parametrize("args, session_id", [([], ""), (["sess-abc"], "sess-abc")])
def test_resume_launches_tui(
    monkeypatch: pytest.MonkeyPatch, args: list[str], session_id: str,
) -> None:
    config = JarvisConfig()
    launch = Mock()
    monkeypatch.setattr(sys, "argv", ["jarvis", "--resume", *args])
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", Mock())
    monkeypatch.setattr(cli_main, "cmd_launch", launch)
    cli_main.main()
    launch.assert_called_once_with(config, resume_session_id=session_id)


# 功能：验证恢复与回放互斥，且不提供额外的 continue 入口
# 设计：直接测试 argparse 拒绝路径，防止不符合用户指定的命令契约
@pytest.mark.parametrize("args", [
    ["--resume", "sess-abc", "--replay", "run-abc"], ["--continue"],
])
def test_invalid_resume_options_rejected(
    monkeypatch: pytest.MonkeyPatch, args: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["jarvis", *args])
    with pytest.raises(SystemExit) as error:
        cli_main.main()
    assert error.value.code == 2

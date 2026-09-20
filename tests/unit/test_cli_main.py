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

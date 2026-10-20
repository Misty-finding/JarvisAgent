from __future__ import annotations

from jarvis.cli.commands.core import ensure_core_running, runtime_config
from jarvis.core.config import JarvisConfig
from jarvis.tui.__main__ import run_tui


# 确保当前项目 Core 已启动，再进入新建、回放或恢复模式的 TUI
def cmd_launch(
    config: JarvisConfig, *, replay_run_id: str | None = None,
    resume_session_id: str | None = None,
) -> None:
    runtime = ensure_core_running(config)
    run_tui(
        runtime_config(config, runtime), replay_run_id=replay_run_id,
        resume_session_id=resume_session_id,
    )

from __future__ import annotations

from jarvis.cli.commands.core import ensure_core_running, runtime_config
from jarvis.core.config import JarvisConfig
from jarvis.tui.__main__ import run_tui


def cmd_launch(config: JarvisConfig, *, replay_run_id: str | None = None) -> None:
    runtime = ensure_core_running(config)
    run_tui(runtime_config(config, runtime), replay_run_id=replay_run_id)

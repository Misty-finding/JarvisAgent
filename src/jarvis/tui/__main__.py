from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
from pathlib import Path

from jarvis.cli.commands.core import ensure_core_running, runtime_config
from jarvis.core.config import JarvisConfig, get_config
from jarvis.tui.app import JarvisTuiApp

_DEFAULT_TUI_LOG = "~/.jarvis/logs/tui.log"


# TUI 文件日志初始化：不写 stderr（避免干扰 Textual 渲染），只写滚动文件
def _setup_logging(level: str) -> None:
    log_path = Path(os.environ.get("JARVIS_TUI_LOG_FILE", _DEFAULT_TUI_LOG)).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            'level=%(levelname)s ts=%(asctime)s source=%(name)s msg="%(message)s"',
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    root.handlers.clear()
    root.addHandler(handler)


def run_tui(config: JarvisConfig, replay_run_id: str | None = None) -> None:
    _setup_logging(config.logging.level)
    app = JarvisTuiApp(
        config.host,
        config.port,
        replay_run_id=replay_run_id,
        workspace=str(config.workspace),
    )
    app.run()


# jarvis-tui 入口：自动确保当前项目 daemon 可用，然后启动 TUI 应用
def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-tui", description="Jarvis TUI")
    parser.add_argument(
        "--replay",
        metavar="RUN_ID",
        help="Replay events from a past run on connect",
    )
    args = parser.parse_args()

    config = get_config()
    runtime = ensure_core_running(config)
    run_tui(runtime_config(config, runtime), replay_run_id=args.replay)


if __name__ == "__main__":
    main()

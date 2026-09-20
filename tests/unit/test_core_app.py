from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any
from unittest.mock import MagicMock

import pytest

from jarvis.core.app import _install_signal_handlers, run


# 功能：验证 Windows 风格事件循环不支持 signal handler 时不会阻止 daemon 启动
def test_signal_handler_not_implemented_is_ignored() -> None:
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.add_signal_handler.side_effect = NotImplementedError
    shutdown = asyncio.Event()

    _install_signal_handlers(loop, shutdown)

    loop.add_signal_handler.assert_called_once()


# 功能：验证支持 signal handler 的平台会注册 SIGINT 和 SIGTERM
def test_signal_handlers_registered() -> None:
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    shutdown = asyncio.Event()

    _install_signal_handlers(loop, shutdown)

    assert loop.add_signal_handler.call_count == 2


# 功能：验证 Ctrl+C 已完成异步清理后不会向用户打印 traceback
def test_run_swallows_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(coro: Coroutine[Any, Any, Any]) -> None:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr("jarvis.core.app.asyncio.run", interrupt)

    run()

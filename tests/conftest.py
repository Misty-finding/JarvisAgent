from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port  # socket released; daemon can bind to this port


@pytest.fixture
async def running_daemon(
    free_port: int,
    tmp_path: Path,
) -> AsyncGenerator[subprocess.Popen[bytes], None]:
    env = os.environ.copy()
    env["JARVIS_PORT"] = str(free_port)
    env["JARVIS_LOG_FILE"] = ""
    env["JARVIS_LOG_LEVEL"] = "WARNING"
    env["JARVIS_LLM_PROVIDER"] = "anthropic"
    env["ANTHROPIC_API_KEY"] = "test-key"
    # Daemon tests must not read local credentials or modify the developer's sessions.
    test_config = tmp_path / "daemon.toml"
    test_config.write_text("", encoding="utf-8")
    env.setdefault("JARVIS_CONFIG", str(test_config))
    env.setdefault("JARVIS_WORKSPACE_ROOT", str(tmp_path))
    env["JARVIS_EXECUTION_BACKEND"] = "local"
    env["JARVIS_TRACE_FILE"] = str(tmp_path / "trace.jsonl")

    proc = subprocess.Popen([sys.executable, "-m", "jarvis.core"], env=env)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            pass
    else:
        proc.terminate()
        proc.wait()
        pytest.fail("Daemon did not start within 10 seconds")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

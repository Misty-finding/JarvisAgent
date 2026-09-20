from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from jarvis.cli.commands import core
from jarvis.core.config import JarvisConfig


def test_running_pid_uses_cross_platform_process_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "jarvis-core.pid"
    pid_file.write_text("1234")
    monkeypatch.setattr(core, "_PID_FILE", pid_file)
    monkeypatch.setattr(core, "_process_exists", lambda pid: pid == 1234)

    assert core._running_pid() == 1234


def test_running_pid_removes_stale_pid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "jarvis-core.pid"
    pid_file.write_text("9999")
    monkeypatch.setattr(core, "_PID_FILE", pid_file)
    monkeypatch.setattr(core, "_process_exists", lambda pid: False)

    assert core._running_pid() is None
    assert not pid_file.exists()


def test_core_start_disables_bytecode_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(*args: object, **kwargs: object) -> SimpleNamespace:
        captured["_args"] = args
        captured.update(kwargs)
        return SimpleNamespace(pid=4321)

    config = JarvisConfig(workspace=tmp_path)
    monkeypatch.setattr(core, "_PID_FILE", None)
    monkeypatch.setattr(core, "_available_port", lambda host, port: 8123)
    monkeypatch.setattr(core, "_wait_until_ready", AsyncMock())
    monkeypatch.setattr(core.subprocess, "Popen", fake_popen)
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)

    core.cmd_core_start(config)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["JARVIS_PORT"] == "8123"
    assert env["JARVIS_WORKSPACE_ROOT"] == str(tmp_path)
    assert captured["cwd"] == str(tmp_path)
    popen_args = captured["_args"]
    assert isinstance(popen_args, tuple)
    command = popen_args[0]
    assert isinstance(command, list)
    assert "-B" in command
    assert (tmp_path / ".jarvis" / "runtime" / "core.json").exists()


def test_initialize_workspace_creates_local_jarvis_runtime(tmp_path: Path) -> None:
    workspace = core.initialize_workspace(tmp_path)

    assert workspace == tmp_path
    assert (tmp_path / ".jarvis" / "runtime").is_dir()
    ignore = (tmp_path / ".jarvis" / ".gitignore").read_text(encoding="utf-8")
    assert "runtime/" in ignore
    assert "sessions/" in ignore


def test_ensure_core_reuses_ready_project_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = core.CoreRuntime(
        pid=4321,
        host="127.0.0.1",
        port=8123,
        workspace=tmp_path,
    )
    core.initialize_workspace(tmp_path)
    core._write_runtime(runtime)
    monkeypatch.setattr(core, "_runtime_is_ready", lambda candidate: candidate == runtime)
    popen = Mock()
    monkeypatch.setattr(core.subprocess, "Popen", popen)

    actual = core.ensure_core_running(JarvisConfig(workspace=tmp_path))

    assert actual == runtime
    popen.assert_not_called()

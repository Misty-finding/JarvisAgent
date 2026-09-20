from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.core.config import JarvisConfig

_RUNTIME_SCHEMA_VERSION = 1
_PID_FILE: Path | None = None  # Test override and compatibility with older callers.


@dataclass(frozen=True)
class CoreRuntime:
    pid: int
    host: str
    port: int
    workspace: Path


def _normalize_workspace(workspace: Path | None = None) -> Path:
    return (workspace or Path.cwd()).expanduser().resolve()


def _runtime_dir(workspace: Path) -> Path:
    return workspace / ".jarvis" / "runtime"


def _pid_file(workspace: Path) -> Path:
    return _PID_FILE or _runtime_dir(workspace) / "core.pid"


def _runtime_file(workspace: Path) -> Path:
    return _runtime_dir(workspace) / "core.json"


def initialize_workspace(workspace: Path | None = None) -> Path:
    root = _normalize_workspace(workspace)
    jarvis_dir = root / ".jarvis"
    runtime_dir = _runtime_dir(root)
    jarvis_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    ignore_file = jarvis_dir / ".gitignore"
    ignored = ["runtime/", "sessions/"]
    if ignore_file.exists():
        existing = ignore_file.read_text(encoding="utf-8").splitlines()
        missing = [entry for entry in ignored if entry not in existing]
        if missing:
            with ignore_file.open("a", encoding="utf-8") as stream:
                if ignore_file.stat().st_size:
                    stream.write("\n")
                stream.write("\n".join(missing) + "\n")
    else:
        ignore_file.write_text("\n".join(ignored) + "\n", encoding="utf-8")
    return root


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _terminate_process(pid: int) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 and _process_exists(pid):
            raise OSError(result.stderr.strip() or f"failed to stop process {pid}")
        return
    os.kill(pid, signal.SIGTERM)


def _same_workspace(left: str | Path, right: str | Path) -> bool:
    if not str(left).strip() or not str(right).strip():
        return False
    left_path = os.path.normcase(str(Path(left).expanduser().resolve()))
    right_path = os.path.normcase(str(Path(right).expanduser().resolve()))
    return left_path == right_path


async def _ping_details(host: str, port: int) -> dict[str, Any]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=1.5,
    )
    request = {
        "jsonrpc": "2.0",
        "id": "launcher-ping",
        "method": "core.ping",
        "params": {"client": "launcher"},
    }
    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=1.5)
        response = json.loads(line)
        result = response.get("result")
        if not isinstance(result, dict):
            raise OSError("endpoint did not return a Jarvis ping response")
        return result
    finally:
        writer.close()
        await writer.wait_closed()


async def _ping_check(config: JarvisConfig) -> None:
    await _ping_details(config.host, config.port)


def _read_runtime(workspace: Path) -> CoreRuntime | None:
    runtime_file = _runtime_file(workspace)
    pid_file = _pid_file(workspace)
    if not runtime_file.exists():
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pid_file.unlink(missing_ok=True)
                return None
            return CoreRuntime(pid, "127.0.0.1", 7437, workspace)
        return None
    try:
        raw = json.loads(runtime_file.read_text(encoding="utf-8"))
        runtime = CoreRuntime(
            pid=int(raw["pid"]),
            host=str(raw["host"]),
            port=int(raw["port"]),
            workspace=Path(str(raw["workspace"])).expanduser().resolve(),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        _clear_runtime(workspace)
        return None
    if not _same_workspace(runtime.workspace, workspace):
        _clear_runtime(workspace)
        return None
    return runtime


def _write_runtime(runtime: CoreRuntime) -> None:
    runtime_dir = _runtime_dir(runtime.workspace)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _pid_file(runtime.workspace).write_text(str(runtime.pid), encoding="utf-8")
    payload = {
        "schema_version": _RUNTIME_SCHEMA_VERSION,
        "pid": runtime.pid,
        "host": runtime.host,
        "port": runtime.port,
        "workspace": str(runtime.workspace),
        "started_at": datetime.now(UTC).isoformat(),
    }
    _runtime_file(runtime.workspace).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _clear_runtime(workspace: Path) -> None:
    _pid_file(workspace).unlink(missing_ok=True)
    _runtime_file(workspace).unlink(missing_ok=True)


def _running_pid(workspace: Path | None = None) -> int | None:
    root = _normalize_workspace(workspace)
    runtime = _read_runtime(root)
    if runtime is None:
        return None
    if not _process_exists(runtime.pid):
        _clear_runtime(root)
        return None
    return runtime.pid


def _available_port(host: str, preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    raise OSError(f"no available TCP port for host {host}")


async def _wait_until_ready(
    runtime: CoreRuntime,
    proc: subprocess.Popen[bytes],
    startup_log: Path,
    timeout_s: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            detail = ""
            if startup_log.exists():
                lines = startup_log.read_text(encoding="utf-8", errors="replace").splitlines()
                if lines:
                    detail = f": {lines[-1]}"
            raise RuntimeError(
                f"jarvis-core exited during startup with code {proc.returncode}{detail}"
            )
        try:
            details = await _ping_details(runtime.host, runtime.port)
            if _same_workspace(str(details.get("workspace", "")), runtime.workspace):
                return
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"jarvis-core did not become ready at {runtime.host}:{runtime.port}: {last_error}"
    )


def _runtime_is_ready(runtime: CoreRuntime) -> bool:
    if not _process_exists(runtime.pid):
        return False
    try:
        details = asyncio.run(_ping_details(runtime.host, runtime.port))
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return False
    return _same_workspace(str(details.get("workspace", "")), runtime.workspace)


def ensure_core_running(
    config: JarvisConfig,
    workspace: Path | None = None,
) -> CoreRuntime:
    root = initialize_workspace(workspace or config.workspace)
    existing = _read_runtime(root)
    if existing is not None:
        if _runtime_is_ready(existing):
            return existing
        if _process_exists(existing.pid):
            _terminate_process(existing.pid)
        _clear_runtime(root)

    port = _available_port(config.host, config.port)
    daemon_env = os.environ.copy()
    daemon_env["JARVIS_HOST"] = config.host
    daemon_env["JARVIS_PORT"] = str(port)
    daemon_env["JARVIS_WORKSPACE_ROOT"] = str(root)
    daemon_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    popen_kwargs: dict[str, Any] = {
        "cwd": str(root),
        "env": daemon_env,
        "start_new_session": True,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    startup_log = _runtime_dir(root) / "core.stderr.log"
    with startup_log.open("ab") as error_stream:
        popen_kwargs["stderr"] = error_stream
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "jarvis.core"],
            **popen_kwargs,
        )
    runtime = CoreRuntime(proc.pid, config.host, port, root)
    _write_runtime(runtime)
    try:
        asyncio.run(_wait_until_ready(runtime, proc, startup_log))
    except BaseException:
        if proc.poll() is None and _process_exists(proc.pid):
            _terminate_process(proc.pid)
        _clear_runtime(root)
        raise
    return runtime


def resolve_runtime_config(
    config: JarvisConfig,
    workspace: Path | None = None,
) -> JarvisConfig:
    root = _normalize_workspace(workspace or config.workspace)
    runtime = _read_runtime(root)
    if runtime is None or not _runtime_is_ready(runtime):
        return replace(config, workspace=root)
    return replace(
        config,
        host=runtime.host,
        port=runtime.port,
        workspace=root,
    )


def runtime_config(config: JarvisConfig, runtime: CoreRuntime) -> JarvisConfig:
    return replace(
        config,
        host=runtime.host,
        port=runtime.port,
        workspace=runtime.workspace,
    )


def cmd_core_status(config: JarvisConfig) -> None:
    root = _normalize_workspace(config.workspace)
    runtime = _read_runtime(root)
    if runtime is None or not _runtime_is_ready(runtime):
        if runtime is not None and not _process_exists(runtime.pid):
            _clear_runtime(root)
        print(f"not running  workspace={root}")
        return
    print(
        f"running  pid={runtime.pid}  {runtime.host}:{runtime.port}  "
        f"workspace={runtime.workspace}"
    )


def cmd_core_start(config: JarvisConfig) -> None:
    runtime = ensure_core_running(config)
    print(
        f"running  pid={runtime.pid}  {runtime.host}:{runtime.port}  "
        f"workspace={runtime.workspace}"
    )


def cmd_core_stop(config: JarvisConfig) -> None:
    root = _normalize_workspace(config.workspace)
    runtime = _read_runtime(root)
    if runtime is None:
        print(f"not running  workspace={root}")
        return
    if _process_exists(runtime.pid):
        _terminate_process(runtime.pid)
    _clear_runtime(root)
    print(f"stopped  pid={runtime.pid}  workspace={root}")

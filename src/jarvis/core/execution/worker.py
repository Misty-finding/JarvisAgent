"""Standard-library-only helper executed INSIDE a task MicroVM.

This module never imports Core configuration, credentials, or session storage.
Its source is sent with each envd request, so task edits cannot alter Core code.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path("/workspace/project")
TASK_UID = 1000
TASK_GID = 1000
IGNORED = {
    ".git",
    ".jarvis",
    "runs",
    ".tasks",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    "target",
}


def safe_path(value: str) -> Path:
    path = Path(value)
    if ".." in path.parts or "\\" in value:
        raise ValueError("Invalid sandbox project path")
    if not path.is_absolute():
        path = ROOT / path
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT) or ROOT.is_symlink():
        raise ValueError("Path is outside /workspace/project")
    current = path
    while current != ROOT and current != current.parent:
        if current.is_symlink():
            raise ValueError("Symbolic links are not supported by project file tools")
        current = current.parent
    return resolved


def files_under(root: Path, limit: int) -> list[Path]:
    if root.is_file():
        return [safe_path(str(root))]
    result: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs if d not in IGNORED and not (Path(current) / d).is_symlink()
        )
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            result.append(path)
            if len(result) > limit:
                raise ValueError("Project file count exceeds configured limit")
    return result


def execute(action: str, p: dict[str, Any]) -> Any:
    if action == "import":
        ROOT.mkdir(parents=True, exist_ok=True)
        for item in p["files"]:
            path = safe_path(item["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(item["content"], validate=True))
            path.chmod(0o755 if item.get("executable") else 0o644)
            for directory in (ROOT, *path.parents):
                if directory == ROOT.parent:
                    break
                getattr(os, "chown")(directory, TASK_UID, TASK_GID)
            getattr(os, "chown")(path, TASK_UID, TASK_GID)
        return {"imported": len(p["files"])}
    if action == "export":
        if ROOT.is_symlink() or not ROOT.is_dir():
            raise ValueError("Project root is missing or replaced by a symbolic link")
        result: list[dict[str, Any]] = []
        total = 0
        for current, dirs, names in os.walk(ROOT, followlinks=False):
            for name in dirs + names:
                path = Path(current) / name
                if path.is_symlink():
                    raise ValueError("Cannot export symbolic links: " + str(path.relative_to(ROOT)))
            dirs[:] = sorted(d for d in dirs if d not in IGNORED)
            for name in sorted(names):
                path = Path(current) / name
                if not path.is_file() or path.stat().st_nlink != 1:
                    raise ValueError("Cannot export special files or hard links")
                if path.stat().st_size > p["max_file_bytes"]:
                    raise ValueError("Export file exceeds configured size: " + name)
                raw = path.read_bytes()
                total += len(raw)
                if total > p["max_total_bytes"] or len(result) >= p["max_files"]:
                    raise ValueError("Export exceeds configured project limits")
                result.append(
                    {
                        "path": path.relative_to(ROOT).as_posix(),
                        "content": base64.b64encode(raw).decode(),
                        "executable": bool(path.stat().st_mode & 0o111),
                    }
                )
        return result
    if action == "bash":
        # Only the MicroVM environment is inherited. No Core env/config is transmitted.
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(  # noqa: S603 -- task shell execution inside the MicroVM
                ["/bin/bash", "--noprofile", "--norc", "-c", p["command"]],
                cwd=safe_path("."),
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            timed_out = False
            try:
                proc.wait(timeout=p.get("timeout", 60))
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    getattr(os, "killpg")(proc.pid, getattr(signal, "SIGKILL"))
                except ProcessLookupError:
                    pass
                proc.wait()
            output.seek(0)
            raw = output.read(65537)
        text = raw[:65536].decode("utf-8", "replace")
        if len(raw) > 65536:
            text += "\n[truncated]"
        if timed_out:
            return {"content": "[timeout]\n" + text, "is_error": True, "error_type": "timeout"}
        return {
            "content": f"[exit {proc.returncode}]\n" + (text or "[no output]"),
            "is_error": proc.returncode != 0,
            "error_type": "command_failed" if proc.returncode else None,
        }
    path = safe_path(p.get("path", "."))
    if action == "read_file":
        raw = path.read_bytes()
        if p.get("start_line") is not None or p.get("end_line") is not None:
            lines = raw[: 2 * 1024 * 1024].decode("utf-8", "replace").splitlines()
            start = p.get("start_line") or 1
            end = p.get("end_line") or len(lines)
            text = "\n".join(f"{i}: {line}" for i, line in enumerate(lines, 1) if start <= i <= end)
        else:
            text = raw[:1048576].decode("utf-8", "replace")
            if len(raw) > 1048576:
                text += "\n[truncated]"
        return {"content": text}
    if action == "write_file":
        raw = p["content"].encode("utf-8")
        if len(raw) > 1048576:
            raise ValueError("Content exceeds 1 MB")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"content": f"wrote {len(raw)} bytes to {p['path']}"}
    if action == "list_dir":
        lines = [str(path) + "/"]

        def walk(directory: Path, depth: int) -> None:
            if depth >= p.get("max_depth", 2) or len(lines) > 200:
                return
            for entry in sorted(directory.iterdir()):
                if len(lines) > 200:
                    break
                lines.append("  " * (depth + 1) + entry.name + ("/" if entry.is_dir() else ""))
                if entry.is_dir() and not entry.is_symlink():
                    walk(entry, depth + 1)

        walk(path, 0)
        return {"content": "\n".join(lines)}
    if action == "search_files":
        regex = re.compile(p["pattern"], 0 if p.get("case_sensitive") else re.IGNORECASE)
        matches = []
        for file in files_under(path, 25000):
            if p.get("glob") and not (
                fnmatch.fnmatch(file.name, p["glob"])
                or fnmatch.fnmatch(file.relative_to(ROOT).as_posix(), p["glob"])
            ):
                continue
            if file.stat().st_size > 2 * 1024 * 1024:
                continue
            for number, line in enumerate(
                file.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if regex.search(line):
                    matches.append(f"{file.relative_to(ROOT)}:{number}:{line}")
                    if len(matches) >= p.get("max_results", 100):
                        return {"content": "\n".join(matches) + "\n[truncated]"}
        return {"content": "\n".join(matches) or "[no matches]"}
    if action == "project_map":
        paths = files_under(path, p.get("max_files", 10000))
        counts = Counter(file.suffix or "[no extension]" for file in paths)
        tree = [
            file.relative_to(path).as_posix()
            for file in paths
            if len(file.relative_to(path).parts) <= p.get("max_depth", 4)
        ]
        return {
            "content": f"Root: {path}\nFiles: {len(paths)}\nTypes: {dict(counts)}\n"
            + "\n".join(tree[:350])
        }
    raise ValueError("Unsupported remote operation")


def _reply(result: Any = None, *, error: str | None = None) -> None:
    print(
        json.dumps(
            {"ok": False, "error": error} if error else {"ok": True, "result": result},
            ensure_ascii=True,
        ),
        flush=True,
    )


def _supervise(request: dict[str, Any]) -> None:
    """Privileged control stays outside the task cgroup; project code runs as user."""
    import ctypes
    import fcntl

    execution_id = request["execution_id"]
    if not re.fullmatch(r"[0-9a-f]{32}", execution_id):
        raise ValueError("Invalid execution identifier")
    control = Path("/run/jarvis")
    control.mkdir(mode=0o700, exist_ok=True)
    control.chmod(0o700)
    state = control / execution_id
    group = Path("/sys/fs/cgroup") / ("jarvis-" + execution_id)
    action = request["action"]
    with (control / (execution_id + ".lock")).open("a") as lock:
        getattr(fcntl, "flock")(lock, getattr(fcntl, "LOCK_EX"))
        if action in ("cancel_execution", "finish_execution"):
            state.write_text("stopped")
            if group.exists():
                for pid in (group / "cgroup.procs").read_text().split():
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                deadline = time.monotonic() + 1
                while (group / "cgroup.procs").read_text().strip() and time.monotonic() < deadline:
                    time.sleep(0.05)
                (group / "cgroup.kill").write_text("1")
                deadline = time.monotonic() + 3
                while (group / "cgroup.procs").read_text().strip() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if (group / "cgroup.procs").read_text().strip():
                    raise RuntimeError("Task processes are still running")
                group.rmdir()
            _reply({"stopped": True})
            return
        if action == "initialize_execution":
            group.mkdir(exist_ok=True)
            if not (group / "cgroup.kill").exists():
                raise RuntimeError("Task cancellation requires cgroup v2 cgroup.kill support")
            if state.exists() and state.read_text() == "stopped":
                raise RuntimeError("Execution has already stopped")
            state.write_text("running")
            ROOT.mkdir(parents=True, exist_ok=True)
            getattr(os, "chown")(ROOT, TASK_UID, TASK_GID)
            _reply({"initialized": True})
            return
        if not state.exists() or state.read_text() != "running":
            raise RuntimeError("Execution is not active")
        # The lock plus registration pipe closes the start-vs-cancel race. No task
        # instruction executes before membership is established and privileges dropped.
        ready_read, ready_write = os.pipe()
        pid = getattr(os, "fork")()
        if pid == 0:
            os.close(ready_read)
            try:
                (group / "cgroup.procs").write_text(str(os.getpid()))
                if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) != 0:
                    raise RuntimeError("Cannot restrict task privileges")
                if action != "import":
                    getattr(os, "setgroups")([])
                    getattr(os, "setgid")(TASK_GID)
                    getattr(os, "setuid")(TASK_UID)
                    os.environ.update(HOME="/home/user", USER="user", LOGNAME="user")
                os.write(ready_write, b"1")
                os.close(ready_write)
                _reply(execute(action, request["params"]))
            except Exception as exc:
                _reply(error=str(exc))
            finally:
                os._exit(0)
        os.close(ready_write)
        os.read(ready_read, 1)
        os.close(ready_read)
        getattr(fcntl, "flock")(lock, getattr(fcntl, "LOCK_UN"))
        _pid, status = getattr(os, "waitpid")(pid, 0)
        if status != 0:
            _reply(error="Task execution was stopped")


def main(encoded: str) -> None:
    try:
        request = json.loads(base64.b64decode(encoded))
        _supervise(request)
    except Exception as exc:
        _reply(error=str(exc))


if __name__ == "__main__":
    main(sys.argv[1])

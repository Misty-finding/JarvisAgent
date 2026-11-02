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
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path("/workspace/project")
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


def main(encoded: str) -> None:
    request = json.loads(base64.b64decode(encoded))
    try:
        result = execute(request["action"], request["params"])
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))


if __name__ == "__main__":
    main(sys.argv[1])

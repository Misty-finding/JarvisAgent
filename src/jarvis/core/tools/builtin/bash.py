from __future__ import annotations

import asyncio
import os
import re
import shutil

from pydantic import BaseModel, ConfigDict, Field

from jarvis.core.tools.base import BaseTool, ToolResult

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 60
_WINDOWS_POSIX_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(^|[;&|]\s*)ls\s+-", "Get-ChildItem -Force"),
    (r"(^|[|;&]\s*)head(\s|$)", "Select-Object -First <count>"),
    (r"(^|[|;&]\s*)tail(\s|$)", "Select-Object -Last <count>"),
    (r"(^|[|;&]\s*)grep(\s|$)", "Select-String"),
    (r"(^|[|;&]\s*)(sed|awk)(\s|$)", "PowerShell string and pipeline operators"),
    (r"(^|[|;&]\s*)cat(\s|$)", "Get-Content"),
    (r"(^|[|;&]\s*)rm\s+-rf(\s|$)", "Remove-Item -Recurse -Force"),
    (r"2>/dev/null", "2>$null"),
)


class BashParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=120)


class BashTool(BaseTool):
    params_model = BashParams
    name = "bash"
    description = (
        "Execute a native shell command and return stdout and stderr combined. "
        "On Windows this runs PowerShell directly; on Unix it runs a POSIX shell. "
        "Do not wrap Windows commands in another `powershell -Command` layer. "
        "Non-interactive only — commands requiring user input will hang and time out. "
        "Prefer short, focused commands. Output is truncated at 64 KB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum seconds to wait (default {_DEFAULT_TIMEOUT}, max 120).",
            },
        },
        "required": ["command"],
    }

    def _argv(self, command: str) -> list[str]:
        if os.name == "nt":
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if executable is None:
                raise RuntimeError("PowerShell was not found on this Windows host")
            utf8_setup = (
                "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "$OutputEncoding = [Console]::OutputEncoding; "
            )
            return [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                utf8_setup + command,
            ]
        shell = os.environ.get("SHELL") or "/bin/sh"
        return [shell, "-lc", command]

    def _windows_syntax_error(self, command: str) -> str | None:
        if os.name != "nt":
            return None
        hints = [
            replacement
            for pattern, replacement in _WINDOWS_POSIX_PATTERNS
            if re.search(pattern, command, flags=re.IGNORECASE)
        ]
        if not hints:
            return None
        replacements = ", ".join(dict.fromkeys(hints))
        return (
            "This host uses PowerShell, but the command contains Unix-only syntax. "
            f"Use PowerShell directly instead. Suggested equivalents: {replacements}."
        )

    # 在平台原生 shell 中执行命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        command = p.command
        timeout = p.timeout

        syntax_error = self._windows_syntax_error(command)
        if syntax_error is not None:
            return ToolResult(
                content=syntax_error,
                is_error=True,
                error_type="schema_error",
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(
                    content=f"[timeout after {timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        output = stdout_bytes.decode("utf-8", errors="replace")
        truncated = len(stdout_bytes) > _MAX_OUTPUT_BYTES
        if truncated:
            output = output[:_MAX_OUTPUT_BYTES] + "\n[truncated]"

        returncode = proc.returncode or 0
        if returncode != 0:
            return ToolResult(
                content=f"[exit {returncode}]\n{output}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=output or "[no output]")

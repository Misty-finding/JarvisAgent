from __future__ import annotations

import asyncio
import os
import re
import shutil
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from jarvis.core.tools.base import BaseTool, ToolResult

_IGNORED_DIRS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "bin",
    "build",
    "coverage",
    "dist",
    "logs",
    "node_modules",
    "obj",
    "out",
    "runtime-logs",
    "target",
    "temp",
    "tmp",
    "vendor",
}
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_SCANNED_FILES = 25_000


class SearchFilesParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern: str
    path: str = "."
    glob: str | None = None
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=500)


class SearchFilesTool(BaseTool):
    params_model = SearchFilesParams
    name = "search_files"
    description = (
        "Search file contents recursively with a regular expression. "
        "Designed for large repositories: common generated directories are skipped, "
        "results include file paths and line numbers, and an optional glob narrows files."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression to search for.",
            },
            "path": {
                "type": "string",
                "description": "Repository or directory to search (default '.').",
            },
            "glob": {
                "type": "string",
                "description": "Optional file glob such as '*.py' or '**/*.java'.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Whether matching is case-sensitive (default false).",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Maximum matching lines to return (default 100).",
            },
        },
        "required": ["pattern"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SearchFilesParams.model_validate(params)
        if ".." in Path(p.path).parts:
            raise PermissionError(f"path traversal not allowed: {p.path}")
        root = Path(p.path)
        if not root.exists():
            raise FileNotFoundError(f"no such path: {p.path}")

        rg = shutil.which("rg")
        if rg is not None:
            return await self._search_with_rg(rg, p)
        return self._search_with_python(root, p)

    async def _search_with_rg(self, rg: str, p: SearchFilesParams) -> ToolResult:
        args = [
            rg,
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--hidden",
        ]
        if not p.case_sensitive:
            args.append("--ignore-case")
        for ignored in sorted(_IGNORED_DIRS):
            args.extend(["--glob", f"!{ignored}/**"])
        if p.glob:
            args.extend(["--glob", p.glob])
        args.extend(["--", p.pattern, p.path])

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        lines: list[str] = []
        while len(lines) < p.max_results:
            raw = await proc.stdout.readline()
            if not raw:
                break
            lines.append(raw.decode("utf-8", errors="replace").rstrip())
        truncated = len(lines) >= p.max_results
        if truncated and proc.returncode is None:
            proc.kill()
        _stdout, stderr = await proc.communicate()
        returncode = proc.returncode or 0
        if returncode not in (0, 1) and not truncated:
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(
                content=detail or f"rg exited with code {returncode}",
                is_error=True,
                error_type="runtime_error",
            )
        if not lines:
            return ToolResult(content="[no matches]")
        if truncated:
            lines.append(f"[truncated after {p.max_results} matches]")
        return ToolResult(content="\n".join(lines))

    def _search_with_python(
        self,
        root: Path,
        p: SearchFilesParams,
    ) -> ToolResult:
        flags = 0 if p.case_sensitive else re.IGNORECASE
        regex = re.compile(p.pattern, flags)
        matches: list[str] = []
        scanned = 0

        paths = [root] if root.is_file() else self._iter_files(root)
        for path in paths:
            if scanned >= _MAX_SCANNED_FILES or len(matches) >= p.max_results:
                break
            if p.glob and not fnmatch(path.as_posix(), p.glob) and not fnmatch(path.name, p.glob):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{path}:{line_number}:{line.strip()}")
                    if len(matches) >= p.max_results:
                        break

        if not matches:
            return ToolResult(content=f"[no matches; scanned {scanned} files]")
        if len(matches) >= p.max_results or scanned >= _MAX_SCANNED_FILES:
            matches.append(
                f"[truncated; returned {len(matches)} matches after scanning {scanned} files]"
            )
        return ToolResult(content="\n".join(matches))

    def _iter_files(self, root: Path) -> list[Path]:
        files: list[Path] = []
        for current, dirs, names in os.walk(root):
            dirs[:] = [name for name in dirs if name not in _IGNORED_DIRS]
            current_path = Path(current)
            files.extend(current_path / name for name in names)
            if len(files) >= _MAX_SCANNED_FILES:
                break
        return files

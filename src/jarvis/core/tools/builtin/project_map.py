from __future__ import annotations

import os
from collections import Counter
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
_IMPORTANT_NAMES = {
    "AGENTS.md",
    "Cargo.toml",
    "Dockerfile",
    "README.md",
    "build.gradle",
    "build.gradle.kts",
    "compose.yml",
    "docker-compose.yml",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
    "settings.gradle",
    "settings.gradle.kts",
}
_MAX_TREE_ENTRIES = 350


class ProjectMapParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    max_depth: int = Field(default=4, ge=1, le=8)
    max_files: int = Field(default=10_000, ge=100, le=50_000)


class ProjectMapTool(BaseTool):
    params_model = ProjectMapParams
    name = "project_map"
    description = (
        "Create a compact structural map of a repository in one call. "
        "Reports modules, important manifests, file-type counts, and a bounded tree while "
        "skipping generated directories such as .git, node_modules, target, and build."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Repository root to inspect (default '.').",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "Maximum tree depth (default 4).",
            },
            "max_files": {
                "type": "integer",
                "minimum": 100,
                "maximum": 50000,
                "description": "Maximum files to scan for statistics (default 10000).",
            },
        },
        "required": [],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ProjectMapParams.model_validate(params)
        if ".." in Path(p.path).parts:
            raise PermissionError(f"path traversal not allowed: {p.path}")
        root = Path(p.path)
        if not root.exists():
            raise FileNotFoundError(f"no such directory: {p.path}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {p.path}")

        files = 0
        directories = 0
        extensions: Counter[str] = Counter()
        important: list[str] = []
        modules: set[str] = set()
        tree_entries: list[tuple[Path, bool]] = []
        truncated = False

        for current, dirs, names in os.walk(root):
            current_path = Path(current)
            relative_dir = current_path.relative_to(root)
            depth = len(relative_dir.parts)
            dirs[:] = sorted(name for name in dirs if name not in _IGNORED_DIRS)
            directories += len(dirs)

            if depth < p.max_depth:
                for name in dirs:
                    if len(tree_entries) < _MAX_TREE_ENTRIES:
                        tree_entries.append((relative_dir / name, True))

            for name in sorted(names):
                if files >= p.max_files:
                    truncated = True
                    break
                path = current_path / name
                relative = path.relative_to(root)
                files += 1
                suffix = path.suffix.lower() or "[no extension]"
                extensions[suffix] += 1
                if name in _IMPORTANT_NAMES or name.startswith("README"):
                    important.append(relative.as_posix())
                    if name in {
                        "Cargo.toml",
                        "build.gradle",
                        "build.gradle.kts",
                        "go.mod",
                        "package.json",
                        "pom.xml",
                        "pyproject.toml",
                    }:
                        modules.add(relative.parent.as_posix() or ".")
                if depth < p.max_depth and len(tree_entries) < _MAX_TREE_ENTRIES:
                    tree_entries.append((relative, False))
            if truncated:
                break

        top_types = ", ".join(
            f"{extension}={count}"
            for extension, count in extensions.most_common(15)
        ) or "[none]"
        important_text = "\n".join(f"- {path}" for path in important[:100]) or "- [none]"
        module_text = ", ".join(sorted(modules)) or "[not detected]"

        tree_lines = [root.as_posix() + "/"]
        for relative, is_dir in tree_entries:
            indent = "  " * (len(relative.parts) - 1)
            suffix = "/" if is_dir else ""
            tree_lines.append(f"{indent}- {relative.name}{suffix}")
        if len(tree_entries) >= _MAX_TREE_ENTRIES:
            tree_lines.append(f"... [tree truncated after {_MAX_TREE_ENTRIES} entries]")

        scan_note = " (scan truncated)" if truncated else ""
        return ToolResult(
            content=(
                f"Root: {root.resolve()}\n"
                f"Scanned: {files} files, {directories} directories{scan_note}\n"
                f"Detected module roots: {module_text}\n"
                f"Top file types: {top_types}\n\n"
                f"Important files:\n{important_text}\n\n"
                f"Tree (depth <= {p.max_depth}):\n" + "\n".join(tree_lines)
            )
        )

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis.core.tools.base import BaseTool, ToolResult

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> ReadFileParams:
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ReadFileTool(BaseTool):
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Absolute paths are supported. Use start_line/end_line for focused reading of "
        "large source files. Full-file reads larger than 512 KB are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file.",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional first line to read, inclusive and 1-based.",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional last line to read, inclusive and 1-based.",
            },
        },
        "required": ["path"],
    }

    # 读取文件内容；超 512KB 截断；禁止 .. 路径遍历
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ReadFileParams.model_validate(params)
        path_str = p.path

        if ".." in Path(path_str).parts:
            raise PermissionError(f"path traversal not allowed: {path_str}")

        path = Path(path_str)
        if p.start_line is not None or p.end_line is not None:
            start = p.start_line or 1
            end = p.end_line
            selected: list[str] = []
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if line_number < start:
                        continue
                    if end is not None and line_number > end:
                        break
                    selected.append(f"{line_number}: {line.rstrip()}")
            return ToolResult(content="\n".join(selected))

        raw = path.read_bytes()  # raises FileNotFoundError if absent
        truncated = len(raw) > _MAX_BYTES
        text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"

        return ToolResult(content=text)

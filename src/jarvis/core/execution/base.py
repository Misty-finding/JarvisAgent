from __future__ import annotations

from contextvars import ContextVar
from typing import Protocol

from jarvis.core.execution.errors import ExecutionUnavailable as ExecutionUnavailable
from jarvis.core.tools.base import BaseTool, ToolResult

execution_run_id: ContextVar[str | None] = ContextVar("execution_run_id", default=None)


class ExecutionBackend(Protocol):
    @property
    def runtime_description(self) -> str: ...

    async def invoke(self, name: str, params: dict[str, object]) -> ToolResult: ...


class BackendTool(BaseTool):
    """Preserve existing schemas/approval names while routing execution remotely."""

    def __init__(
        self, original: BaseTool, backend: ExecutionBackend, *, run_id: str | None = None
    ) -> None:
        self.name = original.name
        self.description = original.description
        if self.name == "bash":
            self.description = (
                "Execute non-interactive Bash in the Linux sandbox at /workspace/project. "
                "All file tools share this project copy. No commands run on Windows. "
                "Return combined output and exit status; timeout at most 120 seconds."
            )
        self.input_schema = original.input_schema
        self.params_model = original.params_model  # type: ignore[misc]
        self._backend = backend
        self._run_id = run_id

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        token = execution_run_id.set(self._run_id)
        try:
            return await self._backend.invoke(self.name, params)
        finally:
            execution_run_id.reset(token)

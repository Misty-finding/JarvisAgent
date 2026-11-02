from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any


@dataclass
class ExecutionConfig:
    """Core-only connection settings; never injected into task environments."""

    backend: str = "local"
    connection_file: str = ""
    api_url: str = "http://127.0.0.1:13000"
    api_key_env: str = "CUBE_API_KEY"
    template: str = ""
    proxy_url: str = "http://127.0.0.1:18080"
    sandbox_domain: str = "cube.app"
    sandbox_timeout: int = 3600
    allow_internet_access: bool = False
    allow_out: list[str] = field(default_factory=list)
    deny_out: list[str] = field(default_factory=list)
    include_uncommitted: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_files: int = 25000
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 50 * 1024 * 1024

    def validate(self) -> None:
        if self.backend not in ("local", "cubesandbox"):
            raise ValueError("execution.backend must be local or cubesandbox")
        for name in ("sandbox_timeout", "max_files", "max_file_bytes", "max_total_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"execution.{name} must be a positive integer")


def parse_execution(data: Any) -> ExecutionConfig:
    """Reject unknown or mistyped settings instead of silently disabling isolation."""
    if not isinstance(data, dict):
        raise ValueError("[execution] must be a table")
    default = ExecutionConfig()
    known = {item.name for item in fields(default)}
    if set(data) - known:
        raise ValueError(f"Unknown execution keys: {', '.join(sorted(set(data) - known))}")
    for name, value in data.items():
        expected = getattr(default, name)
        if type(value) is not type(expected):
            raise ValueError(f"Invalid execution.{name} type")
        if isinstance(value, list) and not all(isinstance(item, str) for item in value):
            raise ValueError(f"execution.{name} must contain strings")
    result = ExecutionConfig(**data)
    result.validate()
    return result

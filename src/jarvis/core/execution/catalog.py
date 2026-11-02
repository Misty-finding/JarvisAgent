from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvis.core.execution.locking import execution_lock
from jarvis.core.execution.project import load_object, save_private
from jarvis.core.execution.settings import ExecutionConfig


class SandboxCatalog:
    """Core-only index across project roots. Instance tokens stay in each session."""

    def __init__(self, config: ExecutionConfig) -> None:
        self.root = Path(config.registry_dir).expanduser().resolve()
        self.path = self.root / "catalog.json"

    def entries(self) -> dict[str, Any]:
        with execution_lock(self.root, wait_seconds=2):
            return load_object(self.path) if self.path.exists() else {}

    def record(self, sandbox_id: str, entry: dict[str, Any]) -> None:
        with execution_lock(self.root, wait_seconds=2):
            entries = load_object(self.path) if self.path.exists() else {}
            entries[sandbox_id] = entry
            save_private(self.path, entries)

    def lookup(self, sandbox_id: str, api_url: str) -> dict[str, Any]:
        entry = self.entries().get(sandbox_id)
        if not entry or entry.get("api_url") != api_url:
            raise ValueError("Sandbox has no trusted Jarvis session mapping")
        return dict(entry)

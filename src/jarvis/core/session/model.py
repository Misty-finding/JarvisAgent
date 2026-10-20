from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SessionStatus = Literal["active", "waiting_for_input", "closed"]
SessionMode = Literal["one_shot", "chat"]
RunState = Literal["running", "completed", "failed", "interrupted"]


@dataclass
class Session:
    id: str
    mode: SessionMode
    status: SessionStatus
    title: str
    created_at: str
    updated_at: str
    run_ids: list[str] = field(default_factory=list)
    schema_version: int = 1
    workspace: str = ""
    active_run_id: str | None = None
    last_run_state: RunState | None = None

    # 将 Session 转为可写入 meta.json 的普通 dict
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_ids": list(self.run_ids),
            "schema_version": self.schema_version,
            "workspace": self.workspace,
            "active_run_id": self.active_run_id,
            "last_run_state": self.last_run_state,
        }

    # 从 meta.json 的 dict 还原 Session 对象
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=str(data["id"]),
            mode=data["mode"],
            status=data["status"],
            title=str(data.get("title", "")),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            run_ids=[str(x) for x in data.get("run_ids", [])],
            schema_version=int(data.get("schema_version", 1)),
            workspace=str(data.get("workspace", "")),
            active_run_id=data.get("active_run_id"),
            last_run_state=data.get("last_run_state"),
        )

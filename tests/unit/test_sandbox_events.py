from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from jarvis.core.bus.events import Event, SandboxLifecycleEvent
from jarvis.core.events.bus import EventBus
from jarvis.core.execution.cube import CubeBackend
from jarvis.core.execution.errors import ExecutionUnavailable
from jarvis.core.execution.settings import ExecutionConfig
from jarvis.core.session.store import SessionStore


async def test_event_journal_survives_recovery_and_compaction(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    sid = "sess-audit"
    store.append_message(sid, "user", "read the project", "run-1")
    store.append_message(
        sid,
        "assistant",
        [
            {"type": "tool_use", "id": "read-1", "name": "read_file", "input": {}},
        ],
        "run-1",
    )
    event = SandboxLifecycleEvent(
        type="sandbox.using",
        session_id=sid,
        run_id="run-1",
        sandbox_id="vm-1",
        operation="execute",
        tool_name="read_file",
        ts="2026-11-02T00:00:00Z",
    )
    restored = TypeAdapter(Event).validate_json(event.model_dump_json())
    assert restored == event
    await store.handle_sandbox_event(event)
    store.recover_thread(sid)
    messages = store.read_messages(sid, truncate=False)
    assert len(messages) == 3 and messages[-1]["content"][0]["is_error"]
    assert all("kind" not in message for message in messages)
    summary = [{"role": "user", "content": "summary"}]
    store.write_compacted(sid, summary)
    store.recover_thread(sid)
    assert store.read_messages(sid) == summary
    rows = [
        json.loads(line)
        for line in (store.session_dir(sid) / "thread.jsonl").read_text().splitlines()
    ]
    assert [row["event"] for row in rows if row.get("kind") == "event"] == [event.model_dump()]


async def test_failed_prepare_and_destroy_are_reported_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    events: list[SandboxLifecycleEvent] = []

    async def collect(event: BaseModel) -> None:
        if isinstance(event, SandboxLifecycleEvent):
            events.append(event)

    bus.subscribe(collect)
    backend = CubeBackend(
        ExecutionConfig(template="fixture", registry_dir=str(tmp_path / "registry")),
        tmp_path,
        tmp_path / ".jarvis",
        bus=bus,
        session_id="sess-test",
        run_id="run-test",
    )

    async def offline(*args: Any, **kwargs: Any) -> Any:
        raise ExecutionUnavailable("service offline")

    monkeypatch.setattr(backend, "_control", offline)
    with pytest.raises(ExecutionUnavailable):
        await backend.prepare()
    assert [event.type for event in events] == ["sandbox.starting", "sandbox.failed"]
    events.clear()
    backend._data = {"sandboxID": "existing"}
    with pytest.raises(ExecutionUnavailable):
        await backend.destroy()
    assert [event.type for event in events] == ["sandbox.stopping", "sandbox.failed"]
    assert events[-1].reason == "destruction_unconfirmed"
    assert all(event.session_id == "sess-test" for event in events)
    assert all(event.elapsed_ms >= 0 for event in events)


async def test_concurrent_destroy_publishes_one_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    events: list[str] = []
    deletes = 0

    async def collect(event: BaseModel) -> None:
        events.append(str(event.model_dump()["type"]))

    async def remove(*args: Any, **kwargs: Any) -> None:
        nonlocal deletes
        deletes += 1
        await asyncio.sleep(0)

    bus.subscribe(collect)
    backend = CubeBackend(ExecutionConfig(), tmp_path, tmp_path / ".jarvis", bus=bus)
    backend._data = {"sandboxID": "fixture"}
    monkeypatch.setattr(backend, "_control", remove)
    await asyncio.gather(backend.destroy(), backend.destroy())
    assert deletes == 1
    assert events == ["sandbox.stopping", "sandbox.destroyed"]


async def test_shared_backend_keeps_concurrent_child_run_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarvis.core.execution.base import BackendTool
    from jarvis.core.tools.builtin import ReadFileTool

    bus = EventBus()
    runs: list[str | None] = []

    async def collect(event: BaseModel) -> None:
        if isinstance(event, SandboxLifecycleEvent):
            runs.append(event.run_id)

    async def worker(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"content": "same project"}

    bus.subscribe(collect)
    backend = CubeBackend(ExecutionConfig(), tmp_path, tmp_path / ".jarvis", bus=bus)
    monkeypatch.setattr(backend, "_worker", worker)
    first = BackendTool(ReadFileTool(), backend, run_id="child-one")
    second = BackendTool(ReadFileTool(), backend, run_id="child-two")
    await asyncio.gather(first.invoke({"path": "main.py"}), second.invoke({"path": "main.py"}))
    await backend.invoke("read_file", {"path": "main.py"})
    assert runs == ["child-one", "child-two", None]

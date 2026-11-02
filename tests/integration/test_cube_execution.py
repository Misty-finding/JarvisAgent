"""Opt in with JARVIS_TEST_CUBE=1 against the already deployed local service.

Uses a scripted model to exercise Jarvis's real loop, approvals, tools and session
storage without sending code or credentials to an external model provider.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.config import JarvisConfig
from jarvis.core.events.bus import EventBus
from jarvis.core.execution.base import ExecutionUnavailable
from jarvis.core.execution.cube import CubeBackend
from jarvis.core.execution.service import sandbox_command
from jarvis.core.execution.settings import ExecutionConfig
from jarvis.core.llm.types import LlmResponse, ToolCallBlock
from jarvis.core.permissions.manager import PermissionManager
from jarvis.core.runner import AgentRunner
from jarvis.core.session.manager import SessionManager
from jarvis.core.session.store import SessionStore

pytestmark = pytest.mark.skipif(
    os.getenv("JARVIS_TEST_CUBE") != "1", reason="local CubeSandbox opt-in"
)


@pytest.fixture
def cube_config(tmp_path: Path) -> JarvisConfig:
    config = JarvisConfig(workspace=tmp_path)
    config.execution = ExecutionConfig(
        backend="cubesandbox",
        connection_file=os.getenv(
            "JARVIS_TEST_CUBE_CONNECTION", "D:/CubeSandbox/client/connection.env"
        ),
    )
    config.agent.max_steps = 20
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class TestCalc(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )
    (tmp_path / "delete.txt").write_text("local original survives remote deletion\n")
    (tmp_path / "user.txt").write_text("before\n")
    (tmp_path / ".env").write_text("MODEL_API_KEY=core-only-sentinel")
    return config


class ScriptedModel:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = iter(calls)
        self.system = ""
        self.results: list[str] = []
        self.count = 0

    async def chat(self, *, messages: Any, system: str, **kwargs: Any) -> LlmResponse:
        self.system = system
        for message in messages:
            if isinstance(message["content"], list):
                for block in message["content"]:
                    if block.get("type") == "tool_result":
                        assert not block.get("is_error"), block["content"]
                        self.results.append(block["content"])
        call = next(self.calls, None)
        if call is None:
            return LlmResponse(stop_reason="end_turn", text="verified")
        self.count += 1
        return LlmResponse(
            stop_reason="tool_use",
            tool_calls=[
                ToolCallBlock(
                    id=f"call-{self.count}",
                    name=call[0],
                    input=call[1],
                )
            ],
        )


async def test_real_jarvis_loop_copy_reuse_review_apply(cube_config: JarvisConfig) -> None:
    config = cube_config
    bus = EventBus()
    store = SessionStore(config.workspace / ".jarvis" / "sessions")
    permission = PermissionManager(policy_file=config.workspace / ".jarvis" / "permissions.json")
    provider = ScriptedModel(
        [
            ("write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}),
            (
                "bash",
                {
                    "command": "python -m unittest -v && test ! -e .env && test ! -e .jarvis && test ! -e /mnt/d"
                },
            ),
            (
                "bash",
                {
                    "command": 'python -c "import os; assert not any(os.environ.get(k) '
                    "for k in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','CUBE_API_KEY'))\""
                },
            ),
            ("read_file", {"path": "calc.py"}),
            ("search_files", {"pattern": "return a \\+ b", "glob": "*.py"}),
            ("list_dir", {}),
            ("project_map", {}),
            ("bash", {"command": "rm delete.txt; printf 'added\\n' > added.txt"}),
        ]
    )
    manager = SessionManager(
        store,
        lambda: AgentRunner(config, bus=bus, provider=provider, permission_manager=permission),
        bus,
        workspace=config.workspace,
    )
    session = await manager.create("chat")
    permission.set_session_mode(session.id, "auto")
    try:
        await manager.send_message(session.id, "fix and test")
        assert session.last_run_state == "completed"
        first_id = session.sandbox_id
        assert first_id
        assert "Linux" in provider.system and "/workspace/project" in provider.system
        assert "PowerShell directly" not in provider.system
        assert any("OK" in text for text in provider.results)
        assert (config.workspace / "calc.py").read_text().endswith("return a - b\n")
        assert (config.workspace / "delete.txt").exists()
        # A second Runner, reconstructed from persisted mapping, sees the same files.
        provider.calls = iter([("bash", {"command": "test ! -e delete.txt; grep 'a + b' calc.py"})])
        await manager.send_message(session.id, "verify same workspace")
        assert session.sandbox_id == first_id
        (config.workspace / "user.txt").write_text("user's independent edit\n")
        review = await sandbox_command(config, "changes", session.id)
        assert {change["path"] for change in review["changes"]} == {
            "calc.py",
            "delete.txt",
            "added.txt",
        }
        with pytest.raises(ValueError, match="Inspect"):
            await sandbox_command(config, "apply", session.id, review_id=review["id"])
        result = await sandbox_command(
            config, "apply", session.id, review_id=review["id"], confirm=review["id"]
        )
        assert len(result["applied"]) == 3
        assert not (config.workspace / "delete.txt").exists()
        assert (config.workspace / "calc.py").read_text().endswith("return a + b\n")
        assert (config.workspace / "user.txt").read_text() == "user's independent edit\n"
    finally:
        if session.sandbox_id:
            await sandbox_command(config, "stop", session.id)


async def test_real_network_and_cancellation(cube_config: JarvisConfig) -> None:
    config = cube_config
    backend = CubeBackend(config.execution, config.workspace, config.workspace / ".jarvis" / "net")
    await backend.prepare()
    try:
        probe = (
            "python - <<'PY'\nimport socket\n"
            "try:\n socket.create_connection(('223.5.5.5',53),timeout=3).close(); print('allowed')\n"
            "except OSError:\n print('blocked')\nPY"
        )
        result = await backend.invoke("bash", {"command": probe})
        assert "blocked" in result.content
        await backend._control(
            "PUT",
            f"/sandboxes/{backend.sandbox_id}/network",
            json={
                "allowInternetAccess": False,
                "allowOut": ["223.5.5.5/32"],
            },
        )
        result = await backend.invoke("bash", {"command": probe})
        assert "allowed" in result.content
        await backend._control(
            "PUT",
            f"/sandboxes/{backend.sandbox_id}/network",
            json={
                "allowInternetAccess": True,
                "denyOut": ["223.5.5.5/32"],
            },
        )
        result = await backend.invoke("bash", {"command": probe})
        assert "blocked" in result.content
        task = asyncio.create_task(backend.invoke("bash", {"command": "sleep 100"}))
        await asyncio.sleep(2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state = json.loads((backend.state_dir / "sandbox.json").read_text())
        assert state["invalidated"]
        listing = await backend._control("GET", "/sandboxes")
        assert backend.sandbox_id not in {item["sandboxID"] for item in listing}
        with pytest.raises(ExecutionUnavailable):
            await CubeBackend(config.execution, config.workspace, backend.state_dir).prepare()
    finally:
        listing = await backend._control("GET", "/sandboxes")
        if backend.sandbox_id in {item["sandboxID"] for item in listing}:
            await backend.destroy()


async def test_real_session_cancel_terminates_remote_command(cube_config: JarvisConfig) -> None:
    config = cube_config
    store = SessionStore(config.workspace / ".jarvis" / "sessions")
    bus = EventBus()
    permission = PermissionManager(policy_file=config.workspace / ".jarvis" / "permissions.json")
    model = ScriptedModel([("bash", {"command": "touch started; sleep 100"})])
    manager = SessionManager(
        store,
        lambda: AgentRunner(config, provider=model, bus=bus, permission_manager=permission),
        bus,
        workspace=config.workspace,
    )
    session = await manager.create("chat")
    permission.set_session_mode(session.id, "auto")
    task = asyncio.create_task(manager.send_message(session.id, "run a long command"))
    backend = CubeBackend(
        config.execution, config.workspace, store.session_dir(session.id) / "execution"
    )
    try:
        async with asyncio.timeout(60):
            while not session.sandbox_id:
                await asyncio.sleep(0.1)
            backend._data = json.loads((backend.state_dir / "sandbox.json").read_text())["sandbox"]
            while True:
                started = await backend.invoke("read_file", {"path": "started"})
                if not started.is_error:
                    break
                await asyncio.sleep(0.1)
        assert await manager.cancel(session.id)
        assert session.last_run_state == "interrupted"
        assert session.active_run_id is None
        listing = await backend._control("GET", "/sandboxes")
        assert session.sandbox_id not in {item["sandboxID"] for item in listing}
        assert not (config.workspace / "started").exists()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_lifecycle_reaches_bus_thread_trace_and_ipc(
    cube_config: JarvisConfig,
    free_port: int,
) -> None:
    from pydantic import BaseModel

    from jarvis.cli.commands.sandbox import _request
    from jarvis.core.app import CoreApp
    from jarvis.core.bus.events import SandboxLifecycleEvent
    from jarvis.core.trace.writer import TraceWriter
    from jarvis.core.transport.ipc_broadcaster import IpcEventBroadcaster
    from jarvis.core.transport.socket_client import SocketClient
    from jarvis.core.transport.socket_server import SocketServer

    config = cube_config
    app = CoreApp()
    app._config = config
    store = SessionStore(config.workspace / ".jarvis" / "sessions")
    trace_path = config.workspace / ".jarvis" / "lifecycle-trace.jsonl"
    app._trace = TraceWriter(trace_path)
    await app._trace.start()
    app._broadcaster = IpcEventBroadcaster(trace=app._trace)
    app._bus.subscribe(store.handle_sandbox_event)
    app._bus.subscribe(app._trace_event_handler)
    app._bus.subscribe(app._broadcaster.handle)
    bus_events: list[dict[str, Any]] = []
    ipc_events: list[dict[str, Any]] = []

    async def collect(event: BaseModel) -> None:
        if isinstance(event, SandboxLifecycleEvent):
            bus_events.append(event.model_dump())

    async def receive(event: dict[str, Any]) -> None:
        ipc_events.append(event)

    app._bus.subscribe(collect)
    provider = ScriptedModel([("read_file", {"path": "calc.py"})])
    permission = PermissionManager(policy_file=config.workspace / ".jarvis" / "permissions.json")
    app._permission_manager = permission
    app._sessions = SessionManager(
        store,
        lambda: AgentRunner(config, bus=app._bus, provider=provider, permission_manager=permission),
        app._bus,
        workspace=config.workspace,
    )
    server = SocketServer("127.0.0.1", free_port, app._broadcaster, app._trace)
    server.register("event.subscribe", app._subscribe_handler)
    server.register("session.create", app._session_create_handler)
    server.register("session.send_message", app._session_send_handler)
    server.register("sandbox.manage", app._sandbox_manage_handler)
    await server.start()
    client = SocketClient("127.0.0.1", free_port)
    await client.connect()
    client.on_event(receive)
    listener = asyncio.create_task(client.run_event_loop())
    sid: str | None = None
    stopped = False
    try:
        await client.send_command("event.subscribe", {"topics": ["sandbox.*"]})
        created = await client.send_command("session.create", {"mode": "chat"})
        sid = created["session_id"]
        permission.set_session_mode(sid, "auto")
        await client.send_command("session.send_message", {"session_id": sid, "content": "read"})
        provider.calls = iter([("read_file", {"path": "calc.py"})])
        await client.send_command("session.send_message", {"session_id": sid, "content": "reuse"})
        # Exercise the actual CLI IPC client: a different connection triggers the stop,
        # while the original subscriber receives the lifecycle broadcast.
        await _request("127.0.0.1", free_port, "stop", sid, None, None)
        stopped = True
        async with asyncio.timeout(5):
            while not ipc_events or ipc_events[-1]["type"] != "sandbox.destroyed":
                await asyncio.sleep(0.01)
        assert ipc_events == bus_events
        kinds = [event["type"] for event in bus_events]
        assert kinds == [
            "sandbox.starting",
            "sandbox.created",
            "sandbox.ready",
            "sandbox.using",
            "sandbox.released",
            "sandbox.reused",
            "sandbox.using",
            "sandbox.released",
            "sandbox.stopping",
            "sandbox.destroyed",
        ]
        rows = [
            json.loads(line)
            for line in (store.session_dir(sid) / "thread.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        journal = [row["event"] for row in rows if row.get("kind") == "event"]
        assert journal == bus_events
        assert all(event["session_id"] == sid for event in journal)
        assert all(event["sandbox_id"] == journal[-1]["sandbox_id"] for event in journal[1:])
        assert all("kind" not in message for message in store.read_messages(sid))
    finally:
        if sid and not stopped:
            await sandbox_command(config, "stop", sid, bus=app._bus)
        await client.close()
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await server.stop()
        await app._trace.stop()
    trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    traced_events = [
        row["data"]
        for row in trace
        if row["layer"] == "event" and row["data"].get("type", "").startswith("sandbox.")
    ]
    assert traced_events == bus_events
    serialized = json.dumps([journal, traced_events, ipc_events])
    assert "core-only-sentinel" not in serialized
    assert "envdAccessToken" not in serialized and "X-API-Key" not in serialized

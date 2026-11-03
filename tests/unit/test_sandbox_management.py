from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from textual.app import App
from textual.widgets import Button, Label

from jarvis.core.app import CoreApp
from jarvis.core.config import JarvisConfig, _apply_toml, get_config
from jarvis.core.events.bus import EventBus
from jarvis.core.execution.cube import CubeBackend
from jarvis.core.execution.errors import ExecutionUnavailable
from jarvis.core.execution.project import (
    apply_review,
    collect_project,
    load_object,
    make_review,
    save_private,
)
from jarvis.core.execution.service import list_sandboxes, resolve_sandbox, sandbox_command
from jarvis.core.execution.settings import ExecutionConfig, parse_execution
from jarvis.core.llm.types import LlmResponse, ToolCallBlock
from jarvis.core.permissions.manager import PermissionManager
from jarvis.core.runner import AgentRunner
from jarvis.core.session.manager import SessionManager
from jarvis.core.session.model import Session
from jarvis.core.session.store import SessionStore
from jarvis.core.transport.ipc_broadcaster import IpcEventBroadcaster
from jarvis.core.transport.socket_client import SocketClient
from jarvis.core.transport.socket_server import SocketServer
from jarvis.tui.app import ChatTextArea, JarvisTuiApp
from jarvis.tui.sandbox_manager import SandboxManager


def mapped_session(config: JarvisConfig, sid: str, sandbox_id: str) -> tuple[SessionStore, Path]:
    config.workspace.mkdir(parents=True, exist_ok=True)
    (config.workspace / "code.txt").write_text("original\n")
    store = SessionStore(config.workspace / ".jarvis" / "sessions")
    store.write_meta(Session(
        id=sid, mode="chat", status="waiting_for_input", title="Project " + sid,
        created_at="now", updated_at="now", workspace=str(config.workspace.resolve()),
        execution_backend="cubesandbox", sandbox_id=sandbox_id,
    ))
    state = store.session_dir(sid) / "execution"
    save_private(state / "baseline.json", collect_project(config.workspace, config.execution))
    backend = CubeBackend(config.execution, config.workspace, state, session_id=sid)
    backend._data = {"sandboxID": sandbox_id, "envdAccessToken": "private-token"}
    backend._save(import_complete=True)
    return store, state


def config_at(tmp_path: Path) -> JarvisConfig:
    return JarvisConfig(workspace=tmp_path / "one", execution=ExecutionConfig(
        registry_dir=str(tmp_path / "registry"), template="fixture",
    ))


def test_default_isolation_and_no_expiry() -> None:
    assert ExecutionConfig().backend == "cubesandbox"
    assert parse_execution({"sandbox_timeout": -1}).sandbox_timeout == -1
    for timeout in (0, -2):
        with pytest.raises(ValueError):
            parse_execution({"sandbox_timeout": timeout})
    with pytest.raises(SystemExit, match="table"):
        _apply_toml(JarvisConfig(), {"execution": "local"})


@pytest.mark.parametrize("local_conflict", [False, True])
async def test_destroyed_session_recreates_and_keeps_old_reviews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_conflict: bool,
) -> None:
    config = config_at(tmp_path)
    _, state = mapped_session(config, "sess-recreate", "old-vm")
    baseline = load_object(state / "baseline.json")
    review = make_review(state, [{
        "path": "code.txt", "content": base64.b64encode(b"old sandbox edit\n").decode(),
        "executable": False,
    }], config.execution)
    old = CubeBackend(config.execution, config.workspace, state, session_id="sess-recreate")
    old._data = load_object(state / "sandbox.json")["sandbox"]
    monkeypatch.setattr(old, "_control", lambda *args, **kwargs: asyncio.sleep(0, result=None))
    await old.destroy()
    events: list[Any] = []
    bus = EventBus()

    async def record(event: Any) -> None:
        events.append(event)

    bus.subscribe(record)
    imported = []

    async def control(self: CubeBackend, method: str, path: str, **kwargs: Any) -> Any:
        if method == "GET" and path == "/sandboxes":
            return []
        assert method == "POST" and path == "/sandboxes"
        return {"sandboxID": "new-vm"}

    async def worker(self: CubeBackend, action: str, params: Any, **kwargs: Any) -> Any:
        if action == "initialize_execution":
            return {"initialized": True}
        if action == "import":
            imported.extend(params["files"])
            return {"imported": len(params["files"])}
        return {"stopped": True}

    monkeypatch.setattr(CubeBackend, "_control", control)
    monkeypatch.setattr(CubeBackend, "_worker", worker)
    backend = CubeBackend(config.execution, config.workspace, state, bus=bus,
                          session_id="sess-recreate", recreate_destroyed=True)
    await backend.prepare()
    assert backend.sandbox_id == "new-vm"
    assert base64.b64decode(imported[0]["content"]) == (config.workspace / "code.txt").read_bytes()
    assert "previous sandbox" in backend.runtime_description
    assert [e.type for e in events] == ["sandbox.starting", "sandbox.created", "sandbox.ready"]
    assert events[0].reason == "user_recreated"
    assert load_object(state / "baselines" / (baseline["revision"] + ".json")) == baseline
    current = load_object(state / "baseline.json")
    assert current["revision"] != baseline["revision"]
    if local_conflict:
        (config.workspace / "code.txt").write_text("user edit\n")
        with pytest.raises(ValueError, match="conflict"):
            apply_review(state, review["id"], config.execution)
        assert (config.workspace / "code.txt").read_text() == "user edit\n"
    else:
        assert apply_review(state, review["id"], config.execution) == {"applied": ["code.txt"]}
        assert (config.workspace / "code.txt").read_text() == "old sandbox edit\n"
    assert load_object(state / "baseline.json") == current
    with pytest.raises(ValueError, match="destroyed or replaced"):
        resolve_sandbox(config, "old-vm")
    with pytest.raises(ValueError, match="replaced"):
        await sandbox_command(config, "destroy", "sess-recreate", discard_changes=True,
                              expected_sandbox_id="old-vm")
    assert not load_object(state / "sandbox.json")["destroyed"]
    await backend.release()


@pytest.mark.parametrize("state", ["destroyed", "invalidated", "missing", "unreachable"])
async def test_management_and_unconfirmed_loss_never_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    config = config_at(tmp_path)
    _, directory = mapped_session(config, "sess-no-replace", "old-vm")
    saved = load_object(directory / "sandbox.json")
    saved["invalidated"] = state in ("destroyed", "invalidated")
    saved["destroyed"] = state == "destroyed"
    save_private(directory / "sandbox.json", saved)
    called = []

    async def control(self: CubeBackend, method: str, path: str, **kwargs: Any) -> Any:
        called.append((method, path))
        raise ExecutionUnavailable("instance missing or unreachable")

    monkeypatch.setattr(CubeBackend, "_control", control)
    backend = CubeBackend(config.execution, config.workspace, directory,
                          recreate_destroyed=state != "destroyed")
    with pytest.raises(ExecutionUnavailable):
        await backend.prepare()
    assert all(method == "GET" for method, _ in called)
    assert load_object(directory / "sandbox.json") == saved


def test_project_cannot_override_global_security_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, project = tmp_path / "home", tmp_path / "project"
    (home / ".jarvis").mkdir(parents=True)
    (project / ".jarvis").mkdir(parents=True)
    (home / ".jarvis" / "config.toml").write_text(
        '[execution]\nbackend="cubesandbox"\nconnection_file="trusted.env"\n'
        'max_total_bytes=1000\nexclude=["private/**"]\n'
    )
    (project / ".jarvis" / "config.toml").write_text(
        '[execution]\nbackend="local"\nconnection_file="untrusted.env"\n'
        'max_total_bytes=9999\nexclude=["build/**"]\n'
    )
    (project / ".env").write_text(
        "JARVIS_EXECUTION_BACKEND=local\nJARVIS_CUBE_CONNECTION_FILE=untrusted.env\n"
        "JARVIS_SANDBOX_REGISTRY_DIR=untrusted\nJARVIS_CONFIG=untrusted.toml\n"
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    for name in ("JARVIS_CONFIG", "JARVIS_EXECUTION_BACKEND", "JARVIS_CUBE_CONNECTION_FILE", "JARVIS_SANDBOX_REGISTRY_DIR"):
        monkeypatch.delenv(name, raising=False)
    loaded = get_config()
    assert loaded.execution.backend == "cubesandbox"
    assert loaded.execution.connection_file == "trusted.env"
    assert loaded.execution.max_total_bytes == 1000
    assert loaded.execution.exclude == ["private/**", "build/**"]


async def test_list_cross_project_current_first_and_no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_at(tmp_path)
    mapped_session(config, "sess-current", "z-current")
    other = replace(config, workspace=tmp_path / "two")
    mapped_session(other, "sess-other", "a-other")

    async def control(*args: Any, **kwargs: Any) -> Any:
        return [
            {"sandboxID": "a-other", "envdAccessToken": "secret"},
            {"sandboxID": "unmanaged", "metadata": {"jarvis_workspace": str(tmp_path / "evil")}},
            {"sandboxID": "z-current"},
        ]

    monkeypatch.setattr(CubeBackend, "_control", control)
    listing = await list_sandboxes(config, "sess-current")
    assert listing[0]["sandbox_id"] == "z-current"
    assert listing[0]["current"]
    assert next(item for item in listing if item["sandbox_id"] == "a-other")["session_id"] == "sess-other"
    unknown = next(item for item in listing if item["sandbox_id"] == "unmanaged")
    assert unknown["workspace"] is None and not unknown["managed"]
    assert "secret" not in str(listing) and "private-token" not in str(listing)


def test_foreign_management_uses_owner_exclusions_without_changing_process_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_at(tmp_path)
    config.workspace.mkdir()
    config.execution.exclude = ["code.txt"]
    other = replace(config, workspace=tmp_path / "two")
    _, state = mapped_session(other, "sess-owner", "vm-owner")
    (other.workspace / ".jarvis" / "config.toml").write_text(
        '[execution]\nexclude=["owner-cache/**"]\nbackend="local"\n'
    )
    home = tmp_path / "home"
    (home / ".jarvis").mkdir(parents=True)
    (home / ".jarvis" / "config.toml").write_text(
        '[execution]\nexclude=["global-secret/**"]\n'
    )
    monkeypatch.chdir(config.workspace)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(config.workspace))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    resolved, sid = resolve_sandbox(config, "vm-owner")
    assert sid == "sess-owner" and resolved.workspace == other.workspace
    assert resolved.execution.exclude == ["global-secret/**", "owner-cache/**"]
    assert resolved.execution.backend == "cubesandbox"
    assert Path.cwd() == config.workspace
    assert resolved.execution.api_url == config.execution.api_url
    assert state == other.workspace / ".jarvis" / "sessions" / sid / "execution"


async def test_destroy_exports_before_delete_and_keeps_local_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_at(tmp_path)
    _, state = mapped_session(config, "sess-save", "vm-save")
    deleted = []

    async def worker(self: CubeBackend, action: str, params: Any, **kwargs: Any) -> Any:
        if action == "initialize_execution":
            return {"initialized": True}
        if action == "export":
            return [{"path": "code.txt", "content": base64.b64encode(b"changed\n").decode(), "executable": False}]
        return {"stopped": True}

    async def control(self: CubeBackend, method: str, path: str, **kwargs: Any) -> Any:
        if method == "DELETE":
            reviews = list((state / "reviews").glob("*.json"))
            assert len(reviews) == 1
            assert load_object(reviews[0])["changes"][0]["path"] == "code.txt"
            deleted.append(path)
        return {}

    monkeypatch.setattr(CubeBackend, "_worker", worker)
    monkeypatch.setattr(CubeBackend, "_control", control)
    result = await sandbox_command(config, "destroy", "sess-save")
    assert result["review_id"] and deleted == ["/sandboxes/vm-save"]
    assert (config.workspace / "code.txt").read_text() == "original\n"
    assert load_object(state / "sandbox.json")["destroyed"]
    # The user can inspect the preserved review after the instance is gone.
    saved = await sandbox_command(config, "changes", "sess-save", review_id=result["review_id"])
    assert saved["id"] == result["review_id"] and len(deleted) == 1


async def test_export_failure_preserves_instance_until_explicit_discard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_at(tmp_path)
    mapped_session(config, "sess-error", "vm-error")
    deleted = []

    async def unavailable(self: CubeBackend) -> None:
        raise ExecutionUnavailable("cannot export")

    async def control(self: CubeBackend, method: str, path: str, **kwargs: Any) -> Any:
        deleted.append(method)
        return {}

    monkeypatch.setattr(CubeBackend, "prepare", unavailable)
    monkeypatch.setattr(CubeBackend, "_control", control)
    with pytest.raises(ExecutionUnavailable):
        await sandbox_command(config, "destroy", "sess-error")
    assert not deleted
    result = await sandbox_command(config, "destroy", "sess-error", discard_changes=True)
    assert result["discarded"] and deleted == ["DELETE"]


async def test_foreign_offline_destruction_journals_only_owner_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_at(tmp_path)
    config.workspace.mkdir()
    current = SessionStore(config.workspace / ".jarvis" / "sessions")
    other = replace(config, workspace=tmp_path / "two")
    foreign, _ = mapped_session(other, "sess-other", "vm-other")
    app = CoreApp()
    app._config = config
    app._bus.subscribe(current.handle_sandbox_event)

    async def control(self: CubeBackend, method: str, path: str, **kwargs: Any) -> Any:
        return [{"sandboxID": "vm-other"}] if path == "/sandboxes" else {}

    monkeypatch.setattr(CubeBackend, "_control", control)
    await app._sandbox_manage_handler({"action": "destroy", "sandbox_id": "vm-other", "discard_changes": True})
    assert not (current.session_dir("sess-other") / "thread.jsonl").exists()
    assert (foreign.session_dir("sess-other") / "thread.jsonl").read_text().count('"kind": "event"') == 2


async def test_foreign_live_core_cancels_owner_and_relays_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_port: int) -> None:
    config = config_at(tmp_path)
    config.workspace.mkdir()
    other = replace(config, workspace=tmp_path / "two")
    store, _ = mapped_session(other, "sess-owner", "vm-owner")
    actions: list[str] = []

    class Sessions:
        def has_active_run(self, sid: str) -> bool:
            return True

        async def cancel(self, sid: str) -> bool:
            actions.append("cancel-owner")
            return True

    async def control(self: CubeBackend, method: str, path: str, **kwargs: Any) -> Any:
        if method == "DELETE":
            assert actions == ["cancel-owner"]
            actions.append("delete")
        return [{"sandboxID": "vm-owner"}] if path == "/sandboxes" else {}

    monkeypatch.setattr(CubeBackend, "_control", control)
    owner = CoreApp()
    owner._config = other
    owner._sessions = cast(Any, Sessions())
    owner._broadcaster = IpcEventBroadcaster()
    owner._bus.subscribe(owner._broadcaster.handle)
    owner._bus.subscribe(store.handle_sandbox_event)
    server = SocketServer("127.0.0.1", free_port, owner._broadcaster)
    server.register("core.ping", owner._ping_handler)
    server.register("event.subscribe", owner._subscribe_handler)
    server.register("sandbox.manage", owner._sandbox_manage_handler)
    await server.start()
    save_private(other.workspace / ".jarvis" / "runtime" / "core.json", {
        "host": "127.0.0.1", "port": free_port,
    })
    caller = CoreApp()
    caller._config = config
    received: list[str] = []

    async def collect(event: Any) -> None:
        received.append(event.type)

    caller._bus.subscribe(collect)
    try:
        result = await caller._sandbox_manage_handler({
            "action": "destroy", "sandbox_id": "vm-owner", "discard_changes": True,
        })
        assert result.data["destroyed"] == "vm-owner"
        assert actions == ["cancel-owner", "delete"]
        assert received == ["sandbox.stopping", "sandbox.destroyed"]
    finally:
        await server.stop()


async def test_cancel_stops_remote_even_when_transport_swallows_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_at(tmp_path)
    config.workspace.mkdir()
    started, swallowed, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    store = SessionStore(config.workspace / ".jarvis" / "sessions")
    bus = EventBus()
    permission = PermissionManager(policy_file=config.workspace / ".jarvis" / "permissions.json")

    class Model:
        calls = 0

        async def chat(self, **kwargs: Any) -> LlmResponse:
            self.calls += 1
            assert self.calls == 1, "No model call may follow a cancelled tool"
            return LlmResponse(stop_reason="tool_use", tool_calls=[
                ToolCallBlock(id="long", name="bash", input={"command": "sleep 100"}),
            ])

    async def prepare(self: CubeBackend, started: float) -> None:
        self._data = {"sandboxID": "retained"}
        await self._begin_execution()

    async def worker(self: CubeBackend, action: str, params: Any, **kwargs: Any) -> Any:
        if action == "initialize_execution":
            return {"initialized": True}
        if action == "cancel_execution":
            await swallowed.wait()
            stopped.set()
            return {"stopped": True}
        if action == "bash":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                swallowed.set()
                await stopped.wait()
            return {"content": "stopped"}
        pytest.fail("Unexpected remote operation: " + action)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Cancellation must not destroy the instance")

    monkeypatch.setattr(CubeBackend, "_prepare", prepare)
    monkeypatch.setattr(CubeBackend, "_worker", worker)
    monkeypatch.setattr(CubeBackend, "destroy", forbidden)
    model = Model()
    manager = SessionManager(store, lambda: AgentRunner(config, bus=bus, provider=cast(Any, model), permission_manager=permission), bus, workspace=config.workspace)
    session = await manager.create("chat")
    permission.set_session_mode(session.id, "auto")
    running = asyncio.create_task(manager.send_message(session.id, "run"))
    async with asyncio.timeout(5):
        await started.wait()
        assert await manager.cancel(session.id)
    await asyncio.gather(running, return_exceptions=True)
    assert stopped.is_set() and model.calls == 1
    assert session.last_run_state == "interrupted"
    state = load_object(store.session_dir(session.id) / "execution" / "sandbox.json")
    assert not state["destroyed"] and not state["invalidated"]


@pytest.mark.parametrize("phase", ["connect", "cleanup", "initialize"])
async def test_cancel_during_reuse_preserves_completed_import_and_stops_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    config = config_at(tmp_path)
    _, state_dir = mapped_session(config, "sess-reuse", "vm-reuse")
    saved = load_object(state_dir / "sandbox.json")
    orphan_id = "a" * 32
    saved["active_execution_id"] = orphan_id
    save_private(state_dir / "sandbox.json", saved)
    blocked = asyncio.Event()
    stopped: list[str] = []
    imports: list[str] = []
    backend = CubeBackend(config.execution, config.workspace, state_dir, session_id="sess-reuse")

    async def control(self: CubeBackend, method: str, path: str, **kwargs: Any) -> Any:
        if phase == "connect" and method == "GET":
            blocked.set()
            await asyncio.Event().wait()
        return {}

    async def worker(self: CubeBackend, action: str, params: Any, **kwargs: Any) -> Any:
        if action == "cancel_execution":
            if phase == "cleanup" and not blocked.is_set():
                blocked.set()
                await asyncio.Event().wait()
            stopped.append(kwargs.get("execution_id", self._execution_id))
            return {"stopped": True}
        if action == "initialize_execution":
            if phase == "initialize" and not blocked.is_set():
                blocked.set()
                await asyncio.Event().wait()
            return {"initialized": True}
        if action == "import":
            imports.append(action)
            return {"imported": len(params["files"])}
        return {"stopped": True}

    monkeypatch.setattr(CubeBackend, "_control", control)
    monkeypatch.setattr(CubeBackend, "_worker", worker)
    preparing = asyncio.create_task(backend.prepare())
    async with asyncio.timeout(5):
        await blocked.wait()
        preparing.cancel()
        await asyncio.gather(preparing, return_exceptions=True)
        await backend.cancel_run()
    interrupted = load_object(state_dir / "sandbox.json")
    assert interrupted["import_complete"], "Cancellation must not mark an existing workspace unimported"
    assert orphan_id in stopped, "An old run may still be alive when reconnect is cancelled"
    assert interrupted["active_execution_id"] is None
    resumed = CubeBackend(config.execution, config.workspace, state_dir, session_id="sess-reuse")
    # The blocking phase has already passed; resume must never replay the original baseline.
    monkeypatch.setattr(resumed, "_control", lambda *args, **kwargs: asyncio.sleep(0, result={}))
    await resumed.prepare()
    assert not imports
    await resumed.release()


class PanelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.deleted = False

    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "sandbox.list":
            return {"sandboxes": [] if self.deleted else [
                {"sandbox_id": "other", "session_id": "sess-other", "workspace": "Other"},
                {"sandbox_id": "current", "session_id": "sess-current", "workspace": "Current", "current": True},
            ]}
        if not params.get("discard_changes"):
            raise RuntimeError("export failed")
        self.deleted = True
        return {"data": {"destroyed": params["sandbox_id"]}}


async def test_panel_orders_current_and_requires_separate_discard_click() -> None:
    client = PanelClient()
    app: App[None] = App()
    async with app.run_test(size=(120, 45)) as pilot:
        panel = SandboxManager(cast(SocketClient, client), "sess-current")
        app.push_screen(panel)
        await pilot.pause()
        assert panel._items[0]["sandbox_id"] == "current"
        assert "sess-current" in str(panel.query(".sandbox-details").first(Label).content)
        assert not panel.query_one("#discard-0", Button).display
        await pilot.click("#destroy-0")
        await pilot.pause()
        assert not client.deleted
        assert panel.query_one("#discard-0", Button).display
        assert client.calls[-1] == ("sandbox.manage", {
            "action": "destroy", "sandbox_id": "current", "discard_changes": False,
        })
        await pilot.click("#discard-0")
        await pilot.pause()
        assert client.deleted
        assert not panel._items


async def test_slash_sandbox_opens_during_run_and_shortcut_works_during_approval() -> None:
    client = PanelClient()

    class OfflineApp(JarvisTuiApp):
        async def _socket_loop(self) -> None:
            self._client = cast(SocketClient, client)
            self._session_id = "sess-current"
            self._busy = True
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.disabled = False
            prompt.focus()

    app = OfflineApp("127.0.0.1", 1)
    async with app.run_test(size=(120, 45)) as pilot:
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.text = "/sandbox"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SandboxManager) and app._busy
        assert all(method == "sandbox.list" for method, _ in client.calls)
        await pilot.press("escape")
        prompt.disabled = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SandboxManager)

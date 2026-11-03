from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from jarvis.core.bus.events import SandboxLifecycleEvent
from jarvis.core.config import JarvisConfig, get_config
from jarvis.core.events.bus import EventBus
from jarvis.core.events.writer import EventWriter
from jarvis.core.execution.catalog import SandboxCatalog
from jarvis.core.execution.cube import CubeBackend
from jarvis.core.execution.locking import execution_lock
from jarvis.core.execution.project import apply_review, load_object, make_review
from jarvis.core.session.store import SessionStore
from jarvis.core.transport.socket_client import SocketClient


def index_workspace(config: JarvisConfig) -> None:
    """Migrate existing session mappings without trusting paths from the remote service."""
    root = config.workspace / ".jarvis" / "sessions"
    catalog = SandboxCatalog(config.execution)
    for path in root.glob("sess-*/execution/sandbox.json"):
        saved = load_object(path)
        data = saved.get("sandbox") or {}
        if data.get("sandboxID") and saved.get("workspace") == str(config.workspace.resolve()):
            meta = path.parent.parent / "meta.json"
            title = load_object(meta).get("title", "") if meta.exists() else ""
            catalog.record(
                str(data["sandboxID"]),
                {
                    "sandbox_id": data["sandboxID"],
                    "session_id": path.parent.parent.name,
                    "title": title,
                    "workspace": saved["workspace"],
                    "state_dir": str(path.parent.resolve()),
                    "api_url": saved.get("api_url"),
                    "destroyed": saved.get("destroyed", False),
                },
            )


async def list_sandboxes(
    config: JarvisConfig, current_session_id: str | None
) -> list[dict[str, Any]]:
    index_workspace(config)
    backend = CubeBackend(config.execution, config.workspace, config.workspace / ".jarvis")
    entries = SandboxCatalog(config.execution).entries()
    instances = await backend._control("GET", "/sandboxes")
    result = []
    for instance in instances:
        sid = str(instance["sandboxID"])
        mapped = entries.get(sid, {})
        if mapped.get("api_url") != backend.api_url:
            mapped = {}
        current = (
            bool(current_session_id)
            and mapped.get("session_id") == current_session_id
            and mapped.get("workspace") == str(config.workspace.resolve())
        )
        result.append(
            {
                "sandbox_id": sid,
                "session_id": mapped.get("session_id"),
                "title": mapped.get("title", ""),
                "workspace": mapped.get("workspace"),
                "current": current,
                "state": instance.get("state", "running"),
                "managed": bool(mapped),
                "expires_at": instance.get("endAt"),
            }
        )
    result.sort(
        key=lambda item: (not item["current"], item.get("workspace") or "", item["sandbox_id"])
    )
    return result


def resolve_sandbox(config: JarvisConfig, sandbox_id: str) -> tuple[JarvisConfig, str]:
    """Only the private catalog can resolve a sandbox ID into a host filesystem path."""
    index_workspace(config)
    backend = CubeBackend(config.execution, config.workspace, config.workspace / ".jarvis")
    entry = SandboxCatalog(config.execution).lookup(sandbox_id, backend.api_url)
    workspace = Path(entry["workspace"]).resolve()
    session_id = str(entry["session_id"])
    expected = (
        SessionStore(workspace / ".jarvis" / "sessions").session_dir(session_id) / "execution"
    )
    if expected.resolve() != Path(entry["state_dir"]).resolve():
        raise ValueError("Invalid sandbox session mapping")
    if workspace == config.workspace.resolve():
        return config, session_id
    try:
        target = get_config(workspace).execution
    except SystemExit as exc:
        raise ValueError("Cannot read the sandbox owner's project configuration") from exc
    # File selection belongs to the owner project. The caller's exclude rules must
    # not silently remove another project's changes from its pre-destruction backup.
    execution = replace(
        config.execution,
        exclude=target.exclude,
        include_uncommitted=target.include_uncommitted,
        max_files=min(config.execution.max_files, target.max_files),
        max_file_bytes=min(config.execution.max_file_bytes, target.max_file_bytes),
        max_total_bytes=min(config.execution.max_total_bytes, target.max_total_bytes),
    )
    return replace(config, workspace=workspace, execution=execution), session_id


async def forward_sandbox(
    config: JarvisConfig, params: dict[str, Any], *, bus: EventBus | None = None
) -> dict[str, Any] | None:
    """Ask a foreign project's live Core to cancel its run before management."""
    runtime = config.workspace / ".jarvis" / "runtime" / "core.json"
    if not runtime.exists():
        return None
    data = load_object(runtime)
    host = str(data["host"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Sandbox owner Core must be local")
    client = SocketClient(host, int(data["port"]))
    try:
        await asyncio.wait_for(client.connect(), 3)
    except (OSError, TimeoutError):
        return None
    listener = asyncio.create_task(client.run_event_loop())
    try:
        pong = await asyncio.wait_for(client.send_command("core.ping", {}), 5)
        if Path(pong["workspace"]).resolve() != config.workspace.resolve():
            raise ValueError("Sandbox owner Core workspace does not match")
        if bus is not None:

            async def relay(event: dict[str, Any]) -> None:
                if event.get("session_id") == params.get("session_id"):
                    await bus.publish(SandboxLifecycleEvent.model_validate(event))

            client.on_event(relay)
            await client.send_command("event.subscribe", {"topics": ["sandbox.*"]})
        # A reachable owner is authoritative. Never bypass its errors with offline deletion.
        return await asyncio.wait_for(client.send_command("sandbox.manage", params), 180)
    finally:
        await client.close()
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


async def sandbox_command(
    config: JarvisConfig,
    action: str,
    session_id: str,
    *,
    review_id: str | None = None,
    confirm: str | None = None,
    bus: EventBus | None = None,
    discard_changes: bool = False,
) -> dict[str, Any]:
    """User-only control surface. It is deliberately absent from the model's tools."""
    if config.execution.backend != "cubesandbox":
        raise ValueError("Configure execution.backend=cubesandbox first")
    store = SessionStore(config.workspace / ".jarvis" / "sessions")
    if action not in ("status", "changes", "apply", "destroy", "stop"):
        raise ValueError("Unknown sandbox action")
    if bus is None:
        bus = EventBus()
        bus.subscribe(store.handle_sandbox_event)
    session = store.read_meta(session_id)
    if session.workspace and session.workspace != str(config.workspace.resolve()):
        raise ValueError("Session does not belong to this workspace")
    state_dir = store.session_dir(session_id) / "execution"
    async with EventWriter(state_dir / "events.jsonl") as writer:
        writer.subscribe(bus)
        return await _locked_command(
            config, action, session_id, review_id, confirm, store, bus, discard_changes
        )


async def _locked_command(
    config: JarvisConfig,
    action: str,
    session_id: str,
    review_id: str | None,
    confirm: str | None,
    store: SessionStore,
    bus: EventBus,
    discard_changes: bool,
) -> dict[str, Any]:
    state_dir = store.session_dir(session_id) / "execution"
    with execution_lock(state_dir):
        if not (state_dir / "sandbox.json").exists():
            raise ValueError("Session has no sandbox mapping")
        if action == "changes" and review_id:
            if not re.fullmatch(r"[0-9a-f]{32}", review_id):
                raise ValueError("Invalid review ID")
            stored_review = load_object(state_dir / "reviews" / (review_id + ".json"))
            if stored_review.get("id") != review_id or stored_review.get("root") != str(
                config.workspace.resolve()
            ):
                raise ValueError("Review does not belong to this project")
            return stored_review
        if action == "apply":
            if not review_id or confirm != review_id:
                raise ValueError("Inspect changes first, then provide --review ID --confirm ID")
            return apply_review(state_dir, review_id, config.execution)
        backend = CubeBackend(
            config.execution, config.workspace, state_dir, bus=bus, session_id=session_id
        )
        if action in ("destroy", "stop"):
            saved = load_object(state_dir / "sandbox.json")
            if saved.get("api_url") != backend.api_url:
                raise ValueError("Sandbox endpoint does not match session")
            backend._data = saved["sandbox"]
            review = None
            if not discard_changes and not saved.get("destroyed"):
                baseline = load_object(state_dir / "baseline.json")
                if baseline["root"] != str(config.workspace.resolve()):
                    raise ValueError("Project baseline belongs to another workspace")
                try:
                    await backend.prepare()
                    review = make_review(state_dir, await backend.export(), config.execution)
                finally:
                    await backend.release()
            await backend.destroy(reason="user_discard" if discard_changes else "user_destroy")
            return {
                "destroyed": backend.sandbox_id,
                "review_id": review["id"] if review else None,
                "review_path": str(state_dir / "reviews" / (review["id"] + ".json"))
                if review
                else None,
                "discarded": discard_changes,
            }
        baseline = load_object(state_dir / "baseline.json")
        if baseline["root"] != str(config.workspace.resolve()):
            raise ValueError("Project baseline belongs to another workspace")
        try:
            await backend.prepare()
            if action == "changes":
                return make_review(state_dir, await backend.export(), config.execution)
            return {
                "sandbox_id": backend.sandbox_id,
                "runtime": backend.runtime_description,
                "imported_files": len(baseline["files"]),
                "excluded_files": baseline["skipped"],
            }
        finally:
            await backend.release()

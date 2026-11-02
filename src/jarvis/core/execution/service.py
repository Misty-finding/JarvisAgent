from __future__ import annotations

from typing import Any

from jarvis.core.config import JarvisConfig
from jarvis.core.events.bus import EventBus
from jarvis.core.events.writer import EventWriter
from jarvis.core.execution.cube import CubeBackend
from jarvis.core.execution.locking import execution_lock
from jarvis.core.execution.project import apply_review, load_object, make_review
from jarvis.core.session.store import SessionStore


async def sandbox_command(
    config: JarvisConfig,
    action: str,
    session_id: str,
    *,
    review_id: str | None = None,
    confirm: str | None = None,
    bus: EventBus | None = None,
) -> dict[str, Any]:
    """User-only control surface. It is deliberately absent from the model's tools."""
    if config.execution.backend != "cubesandbox":
        raise ValueError("Configure execution.backend=cubesandbox first")
    store = SessionStore(config.workspace / ".jarvis" / "sessions")
    if action not in ("status", "changes", "apply", "stop"):
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
        return await _locked_command(config, action, session_id, review_id, confirm, store, bus)


async def _locked_command(
    config: JarvisConfig,
    action: str,
    session_id: str,
    review_id: str | None,
    confirm: str | None,
    store: SessionStore,
    bus: EventBus,
) -> dict[str, Any]:
    session = store.read_meta(session_id)
    state_dir = store.session_dir(session_id) / "execution"
    with execution_lock(state_dir):
        if not (state_dir / "sandbox.json").exists():
            raise ValueError("Session has no sandbox mapping")
        baseline = load_object(state_dir / "baseline.json")
        if baseline["root"] != str(config.workspace.resolve()):
            raise ValueError("Project baseline belongs to another workspace")
        if action == "apply":
            if not review_id or confirm != review_id:
                raise ValueError("Inspect changes first, then provide --review ID --confirm ID")
            return apply_review(state_dir, review_id, config.execution)
        backend = CubeBackend(
            config.execution, config.workspace, state_dir, bus=bus, session_id=session_id
        )
        if action == "stop":
            saved = load_object(state_dir / "sandbox.json")
            if saved.get("api_url") != backend.api_url:
                raise ValueError("Sandbox endpoint does not match session")
            backend._data = saved["sandbox"]
            await backend.destroy()
            return {"stopped": session.sandbox_id}
        await backend.prepare()
        if action == "changes":
            review = make_review(state_dir, await backend.export(), config.execution)
            return review
        return {
            "sandbox_id": backend.sandbox_id,
            "runtime": backend.runtime_description,
            "imported_files": len(baseline["files"]),
            "excluded_files": baseline["skipped"],
        }

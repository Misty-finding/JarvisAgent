from __future__ import annotations

import asyncio
import json
import unicodedata
from typing import Any

from jarvis.cli.commands.core import ensure_core_running
from jarvis.core.config import JarvisConfig
from jarvis.core.transport.socket_client import SocketClient


def _display(value: str) -> str:
    # Project diffs are untrusted terminal input; preserve text layout, escape controls.
    return "".join(
        char
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
        else repr(char)[1:-1]
        for char in value
    )


def cmd_sandbox(
    config: JarvisConfig,
    action: str,
    session_id: str | None,
    review_id: str | None = None,
    confirm: str | None = None,
    *,
    sandbox_id: str | None = None,
    discard_changes: bool = False,
) -> None:
    try:
        runtime = ensure_core_running(config)
        result = asyncio.run(
            _request(
                runtime.host,
                runtime.port,
                action,
                session_id,
                review_id,
                confirm,
                sandbox_id=sandbox_id,
                discard_changes=discard_changes,
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if action == "changes":
        print("Review:", result["id"])
        for change in result["changes"]:
            print(
                f"{change['kind']} {change['path']}"
                + (" [LOCAL CONFLICT]" if change["conflict"] else "")
            )
            print(_display(change["diff"]))
        print("No local project files were changed. Apply exactly these reviewed bytes with:")
        print(
            f"jarvis sandbox apply --session {session_id} "
            f"--review {result['id']} --confirm {result['id']}"
        )
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


async def _request(
    host: str,
    port: int,
    action: str,
    session_id: str | None,
    review_id: str | None,
    confirm: str | None,
    *,
    sandbox_id: str | None = None,
    discard_changes: bool = False,
) -> dict[str, Any]:
    client = SocketClient(host, port)
    await client.connect()
    listener = asyncio.create_task(client.run_event_loop())
    try:
        if action == "list":
            return await client.send_command("sandbox.list", {"current_session_id": session_id})
        if not session_id and not sandbox_id:
            raise ValueError("Provide --session or --id")
        result = await client.send_command(
            "sandbox.manage",
            {
                "action": action,
                "session_id": session_id,
                "sandbox_id": sandbox_id,
                "discard_changes": discard_changes,
                "review_id": review_id,
                "confirm": confirm,
            },
        )
        return dict(result["data"])
    finally:
        await client.close()
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import struct
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from jarvis.core.bus.events import SandboxEventType, SandboxLifecycleEvent
from jarvis.core.events.bus import EventBus
from jarvis.core.execution import worker
from jarvis.core.execution.base import ExecutionUnavailable, execution_run_id
from jarvis.core.execution.catalog import SandboxCatalog
from jarvis.core.execution.locking import execution_lock
from jarvis.core.execution.project import collect_project, load_object, save_private
from jarvis.core.execution.settings import ExecutionConfig
from jarvis.core.tools.base import ToolResult


class CubeBackend:
    """At most one live MicroVM per session; task operations use its envd API.

    Network clients ignore host proxy variables. Authentication stays in HTTP
    headers and is never copied into the task's environment or workspace.
    """

    def __init__(
        self,
        config: ExecutionConfig,
        workspace: Path,
        state_dir: Path,
        *,
        private_paths: list[Path] | None = None,
        bus: EventBus | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        recreate_destroyed: bool = False,
    ) -> None:
        config.validate()
        self.config = config
        self.workspace = workspace.resolve()
        self.state_dir = state_dir
        self._bus = bus or EventBus()
        self._session_id = session_id
        self._run_id = run_id
        self._recreate_destroyed = recreate_destroyed
        self._recreated_from: str | None = None
        self._destroy_lock = asyncio.Lock()
        self._cancel_lock = asyncio.Lock()
        self._execution_id = uuid.uuid4().hex
        self._active_execution_id: str | None = None
        self._cancelled = False
        self._cancel_requested = False
        self._prepared = False
        self._import_complete = False
        self._cancel_unconfirmed = False
        exclusions = list(config.exclude)
        for path in [state_dir, *(private_paths or [])]:
            resolved = path.resolve()
            if resolved.is_relative_to(self.workspace):
                relative = resolved.relative_to(self.workspace).as_posix()
                if relative == ".":
                    raise ValueError("Core runtime directory cannot be the project root")
                exclusions.extend([relative, relative + "/**"])
        self._copy_config = replace(config, exclude=exclusions)
        values = (
            dotenv_values(Path(config.connection_file).expanduser())
            if config.connection_file
            else {}
        )
        self.api_url = (values.get("CUBE_API_URL") or config.api_url).rstrip("/")
        self.api_key = os.environ.get(config.api_key_env) or values.get("CUBE_API_KEY")
        self.template = config.template or values.get("CUBE_TEMPLATE_ID")
        self.proxy_url = config.proxy_url.rstrip("/")
        self.domain = config.sandbox_domain
        if config.connection_file:
            self.proxy_url = (values.get("CUBE_REMOTE_PROXY_BASE") or self.proxy_url).rstrip("/")
            self.domain = values.get("CUBE_SANDBOX_DOMAIN") or self.domain
        for url in (self.api_url, self.proxy_url):
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Invalid CubeSandbox endpoint")
        self._data: dict[str, Any] | None = None
        self._broken = False
        self._destroyed = False
        self._source = base64.b64encode(Path(worker.__file__).read_bytes()).decode()

    @property
    def runtime_description(self) -> str:
        description = (
            "Operating system: Linux (CubeSandbox MicroVM)\n"
            "Shell: /bin/bash --noprofile --norc\n"
            "Working directory: /workspace/project\n"
            "All project tools and sub-agents share this sandbox project copy. "
            "Windows paths are unavailable. The original project is never mounted. "
            "Use Linux commands. Local changes require a user-reviewed export/apply. "
            "Core credentials and session data are not in this workspace. "
            "On execution_unavailable stop the task; never attempt local execution."
        )
        if self._recreated_from:
            description += (
                "\nThe user destroyed the previous sandbox. This is a replacement imported "
                "from the local project, not the old sandbox's files or installed dependencies. "
                "Chat history refers to an earlier environment; inspect current files before "
                "continuing. Old saved reviews remain available for explicit user apply."
            )
        return description

    @property
    def sandbox_id(self) -> str | None:
        return str(self._data["sandboxID"]) if self._data else None

    async def _control(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
                response = await client.request(
                    method, self.api_url + path, headers=headers, **kwargs
                )
                if method == "DELETE" and response.status_code == 404:
                    return None  # An already absent instance is also confirmed stopped.
                response.raise_for_status()
                return response.json() if response.content else None
        except (httpx.HTTPError, ValueError) as exc:
            # Never expose response bodies/URLs containing tokens to model traces.
            raise ExecutionUnavailable(
                "CubeSandbox control connection failed; local execution is disabled."
            ) from exc

    async def prepare(self) -> None:
        started = time.monotonic()
        try:
            await self._prepare(started)
        except (ExecutionUnavailable, OSError, ValueError, KeyError):
            await self._emit(
                "sandbox.failed", "prepare", reason="initialization_failed", started=started
            )
            raise

    async def _emit(
        self,
        event_type: SandboxEventType,
        operation: Literal["prepare", "execute", "export", "release", "destroy", "cancel"],
        *,
        reason: str | None = None,
        tool_name: str | None = None,
        started: float | None = None,
    ) -> None:
        await self._bus.publish(
            SandboxLifecycleEvent(
                type=event_type,
                session_id=self._session_id,
                run_id=execution_run_id.get() or self._run_id,
                sandbox_id=self.sandbox_id,
                project_workspace=str(self.workspace),
                operation=operation,
                reason=reason,
                tool_name=tool_name,
                elapsed_ms=int((time.monotonic() - started) * 1000) if started is not None else 0,
                ts=datetime.now(UTC).isoformat(),
            )
        )

    async def _prepare(self, started: float) -> None:
        """Reuse a live instance; replace confirmed destruction only on a new task."""
        state_path = self.state_dir / "sandbox.json"
        if state_path.exists():
            saved = load_object(state_path)
            if saved.get("workspace") != str(self.workspace):
                raise ExecutionUnavailable("Sandbox mapping belongs to another workspace")
            if saved.get("destroyed") and self._recreate_destroyed:
                self._archive_destroyed(saved)
                await self._create(started)
                return
            if (
                saved.get("api_url") != self.api_url
                or saved.get("proxy_url") != self.proxy_url
                or saved.get("invalidated")
                or saved.get("network_policy") != self._network_policy()
            ):
                raise ExecutionUnavailable(
                    "Sandbox mapping is invalid. Start a new Jarvis session."
                )
            self._data = saved["sandbox"]
            self._recreated_from = saved.get("recreated_from")
            # Restore durable state before the first await: a simultaneous cancellation
            # must preserve completed imports and terminate the old run, not just this one.
            self._import_complete = bool(saved.get("import_complete"))
            previous = saved.get("active_execution_id")
            self._active_execution_id = str(previous) if previous else None
            if not (self.state_dir / "baseline.json").exists():
                raise ExecutionUnavailable(
                    "Previous sandbox import was interrupted. Start a new session."
                )
            await self._control("GET", f"/sandboxes/{self.sandbox_id}")
            await self._control(
                "POST",
                f"/sandboxes/{self.sandbox_id}/timeout",
                json={"timeout": self.config.sandbox_timeout},
            )
            if previous:
                await self._end_execution(str(previous), "cancel_execution")
            self._check_cancel()
            complete = bool(saved.get("import_complete"))
            await self._begin_execution(import_complete=complete)
            if not complete:
                await self._import_project(load_object(self.state_dir / "baseline.json"))
                self._save(import_complete=True)
            await self._emit("sandbox.reused", "prepare", started=started)
            return
        await self._create(started)

    def _archive_destroyed(self, saved: dict[str, Any]) -> None:
        """Preserve review baselines before a replacement imports the local project."""
        baseline_path = self.state_dir / "baseline.json"
        if baseline_path.exists():
            baseline = load_object(baseline_path)
            revision = str(baseline["revision"])
            if not re.fullmatch(r"[0-9a-f]{32}", revision):
                raise ValueError("Invalid project baseline revision")
            archived = self.state_dir / "baselines" / (revision + ".json")
            if not archived.exists():
                save_private(archived, baseline)
        # Keep the confirmed destroyed mapping until creation is durably saved.
        # Failure or cancellation before then can safely retry the replacement.
        self._recreated_from = str(saved["sandbox"]["sandboxID"])

    async def _create(self, started: float) -> None:
        await self._emit(
            "sandbox.starting", "prepare",
            reason="user_recreated" if self._recreated_from else "new_session",
        )
        if not self.template:
            raise ExecutionUnavailable("CubeSandbox template is not configured")
        # Collect the copy before creating a VM, so import failures cannot leak VMs.
        baseline = await asyncio.to_thread(collect_project, self.workspace, self._copy_config)
        save_private(self.state_dir / "baseline.json", baseline)
        payload = {
            "templateID": self.template,
            "timeout": self.config.sandbox_timeout,
            "allow_internet_access": self.config.allow_internet_access,
            "network": {
                "allowPublicTraffic": False,
                "allowOut": self.config.allow_out,
                "denyOut": self.config.deny_out,
            },
            "metadata": {
                "jarvis_session_id": self._session_id or "",
                "jarvis_workspace": str(self.workspace),
            },
        }
        try:
            catalog = SandboxCatalog(self.config)
            with execution_lock(catalog.root / "creation"):
                listing = await self._control("GET", "/sandboxes")
                if len(listing) >= self.config.max_running_sandboxes:
                    raise ExecutionUnavailable(
                        "Sandbox limit reached. Open /sandbox to manage existing instances."
                    )
                creation = asyncio.create_task(self._control("POST", "/sandboxes", json=payload))
                try:
                    self._data = await asyncio.shield(creation)
                except asyncio.CancelledError:
                    self._data = await asyncio.shield(creation)
                    self._save(import_complete=False)
                    raise
            self._save(import_complete=False)
            await self._emit("sandbox.created", "prepare", started=started)
            await self._begin_execution(import_complete=False)
            await self._import_project(baseline)
            self._save(import_complete=True)
            await self._emit("sandbox.ready", "prepare", started=started)
        except BaseException:
            # Keep even partially initialized instances visible for explicit user cleanup.
            raise

    async def _begin_execution(self, *, import_complete: bool = True) -> None:
        self._check_cancel()
        # Record initialization intent before dispatch, even when its response is lost.
        self._active_execution_id = self._execution_id
        self._save(import_complete=import_complete)
        value = await self._worker("initialize_execution", {})
        self._check_cancel()
        if not value.get("initialized"):
            raise ExecutionUnavailable("Sandbox cannot provide managed task cancellation")
        self._active_execution_id = self._execution_id
        self._prepared = True
        self._cancel_unconfirmed = False
        self._save(import_complete=import_complete)

    async def _import_project(self, baseline: dict[str, Any]) -> None:
        # Only incomplete initialization is replayed; completed workspaces are never reimported.
        batch: list[dict[str, Any]] = []
        size = 0
        for item in baseline["files"]:
            if size + len(item["content"]) > 2_000_000 and batch:
                await self._import_batch(batch)
                batch, size = [], 0
            batch.append({key: item[key] for key in ("path", "content", "executable")})
            size += len(item["content"])
        await self._import_batch(batch)

    async def _end_execution(self, execution_id: str, action: str) -> None:
        value = await self._worker(action, {}, execution_id=execution_id)
        if not value.get("stopped"):
            raise ExecutionUnavailable("Task process termination could not be confirmed")

    async def cancel_run(self) -> None:
        self._cancel_requested = True
        async with self._cancel_lock:
            if not self._data or self._cancelled:
                return
            started = time.monotonic()
            await self._emit("sandbox.cancel_requested", "cancel")
            try:
                # Reconnect may be interrupted before the persisted previous run is stopped.
                for execution_id in dict.fromkeys(
                    [
                        self._active_execution_id or self._execution_id,
                        self._execution_id,
                    ]
                ):
                    await self._end_execution(execution_id, "cancel_execution")
            except ExecutionUnavailable:
                self._broken = True
                self._cancel_unconfirmed = True
                self._save(import_complete=self._import_complete)
                await self._emit(
                    "sandbox.cancel_failed",
                    "cancel",
                    reason="termination_unconfirmed",
                    started=started,
                )
                raise
            self._cancelled = True
            self._broken = False
            self._cancel_unconfirmed = False
            self._active_execution_id = None
            self._save(import_complete=self._import_complete)
            await self._emit(
                "sandbox.cancelled", "cancel", reason="instance_retained", started=started
            )

    async def _import_batch(self, files: list[dict[str, Any]]) -> None:
        result = await self._worker("import", {"files": files})
        if not isinstance(result, dict) or result.get("imported") != len(files):
            raise ExecutionUnavailable("Sandbox project import failed; start a new session.")

    def _save(self, *, import_complete: bool, invalidated: bool = False) -> None:
        self._import_complete = import_complete
        save_private(
            self.state_dir / "sandbox.json",
            {
                "workspace": str(self.workspace),
                "api_url": self.api_url,
                "proxy_url": self.proxy_url,
                "sandbox": self._data,
                "import_complete": import_complete,
                "invalidated": invalidated,
                "network_policy": self._network_policy(),
                "active_execution_id": self._active_execution_id,
                "cancel_unconfirmed": self._cancel_unconfirmed,
                "destroyed": self._destroyed,
                "recreated_from": self._recreated_from,
            },
        )
        if self._data and self._session_id:
            metadata = self.state_dir.parent / "meta.json"
            title = load_object(metadata).get("title", "") if metadata.exists() else ""
            SandboxCatalog(self.config).record(
                str(self.sandbox_id),
                {
                    "sandbox_id": self.sandbox_id,
                    "session_id": self._session_id,
                    "title": title,
                    "workspace": str(self.workspace),
                    "state_dir": str(self.state_dir.resolve()),
                    "api_url": self.api_url,
                    "destroyed": self._destroyed,
                },
            )

    def _network_policy(self) -> dict[str, Any]:
        return {
            "allow_internet_access": self.config.allow_internet_access,
            "allow_out": sorted(self.config.allow_out),
            "deny_out": sorted(self.config.deny_out),
        }

    async def destroy(self, *, reason: str = "user_stop") -> None:
        """Explicit user destruction only; task cancellation never deletes the instance."""
        self._broken = True
        async with self._destroy_lock:
            if self._data and not self._destroyed:
                started = time.monotonic()
                await self._emit("sandbox.stopping", "destroy", reason=reason)
                try:
                    self._save(import_complete=self._import_complete, invalidated=True)
                    await self._control("DELETE", f"/sandboxes/{self.sandbox_id}")
                except (ExecutionUnavailable, OSError):
                    await self._emit(
                        "sandbox.failed",
                        "destroy",
                        reason="destruction_unconfirmed",
                        started=started,
                    )
                    raise
                self._destroyed = True
                self._active_execution_id = None
                self._save(import_complete=self._import_complete, invalidated=True)
                await self._emit("sandbox.destroyed", "destroy", reason=reason, started=started)

    async def release(self) -> None:
        if self._data and not self._destroyed:
            if self._prepared and not self._cancelled and not self._broken:
                await self._end_execution(self._execution_id, "finish_execution")
                self._active_execution_id = None
                self._save(import_complete=self._import_complete)
            await self._emit(
                "sandbox.released",
                "release",
                reason="state_unknown" if self._broken else "instance_retained",
            )

    async def _process(self, command: str, timeout: float = 110, *, control: bool = False) -> str:
        if (self._broken and not control) or not self._data:
            raise ExecutionUnavailable(
                "Sandbox unavailable; start a new session. No host fallback."
            )
        headers = {
            "Host": f"49983-{self.sandbox_id}.{self.domain}",
            "Content-Type": "application/connect+json",
            "Connect-Protocol-Version": "1",
            "Connect-Content-Encoding": "identity",
            "Connect-Timeout-Ms": str(int(timeout * 1000)),
            "Authorization": "Basic " + base64.b64encode(b"root:").decode(),
        }
        for field, header in (
            ("envdAccessToken", "X-Access-Token"),
            ("trafficAccessToken", "e2b-traffic-access-token"),
        ):
            if self._data.get(field):
                headers[header] = str(self._data[field])
        payload = json.dumps(
            {
                "process": {
                    "cmd": "/bin/bash",
                    "args": ["--noprofile", "--norc", "-c", command],
                    "envs": {},
                    "cwd": "/tmp",  # noqa: S108 -- inside the dedicated task MicroVM
                },
                "stdin": False,
            }
        ).encode()
        frame = b"\0" + struct.pack(">I", len(payload)) + payload
        buffer = bytearray()
        output = bytearray()
        stderr = bytearray()
        exit_code = None
        limit = max(self.config.max_total_bytes * 2, 4 * 1024 * 1024)
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            async with client.stream(
                "POST", self.proxy_url + "/process.Process/Start", headers=headers, content=frame
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    while len(buffer) >= 5:
                        flags, size = buffer[0], struct.unpack(">I", buffer[1:5])[0]
                        if size > limit:
                            raise ExecutionUnavailable("Sandbox response exceeds size limit")
                        if len(buffer) < size + 5:
                            break
                        raw = bytes(buffer[5 : 5 + size])
                        del buffer[: 5 + size]
                        data = json.loads(raw) if raw else {}
                        if flags & 1 or (flags & 2 and data.get("error")):
                            raise ExecutionUnavailable("Sandbox process stream failed")
                        event = data.get("event") or {}
                        stdout = (event.get("data") or {}).get("stdout")
                        encoded_stderr = (event.get("data") or {}).get("stderr")
                        if encoded_stderr and len(stderr) < 4096:
                            stderr.extend(base64.b64decode(encoded_stderr)[:4096])
                        if stdout:
                            output.extend(base64.b64decode(stdout))
                            if len(output) > limit:
                                raise ExecutionUnavailable("Sandbox output exceeds size limit")
                        if event.get("end") is not None:
                            end = event["end"]
                            exit_code = end.get("exitCode", end.get("exit_code"))
                            if exit_code is None and end.get("status") == "exited":
                                exit_code = 0
                            if exit_code is None:
                                match = re.search(
                                    r"(?:exit status|exited with code)\s+(-?\d+)",
                                    str(end.get("status", "")),
                                )
                                if match:
                                    exit_code = int(match.group(1))
                if buffer or exit_code != 0:
                    raise ExecutionUnavailable(
                        f"Sandbox worker exited unexpectedly (code={exit_code}): "
                        + stderr.decode("utf-8", "replace")
                    )
        return output.decode("utf-8")

    async def _worker(
        self, action: str, params: dict[str, Any], *, execution_id: str | None = None
    ) -> Any:
        if action not in ("cancel_execution", "finish_execution"):
            self._check_cancel()
        request = base64.b64encode(
            json.dumps(
                {
                    "action": action,
                    "params": params,
                    "execution_id": execution_id or self._execution_id,
                }
            ).encode()
        ).decode()
        # Source and argument are trusted encodings, quoted independently from task values.
        code = (
            "import base64; ns={'__name__':'jarvis_worker'}; "
            f"exec(compile(base64.b64decode('{self._source}'),'<jarvis-worker>','exec'), ns); "
            "ns['main'](input())"
        )
        command = f"python3 -I -c {shlex.quote(code)} <<'JARVIS_REQUEST'\n{request}\nJARVIS_REQUEST"
        try:
            if len(command) > 60000:
                # Linux has a per-argument limit even when envd accepts a larger HTTP body.
                # Upload only task payload; no Core metadata or authentication in this file.
                path = "/tmp/jarvis-request-" + uuid.uuid4().hex  # noqa: S108 -- MicroVM only
                headers = {
                    "Host": f"49983-{self.sandbox_id}.{self.domain}",
                    "Content-Type": "application/octet-stream",
                }
                if self._data is None:
                    raise ExecutionUnavailable("Sandbox has not been prepared")
                for field, header in (
                    ("envdAccessToken", "X-Access-Token"),
                    ("trafficAccessToken", "e2b-traffic-access-token"),
                ):
                    if self._data.get(field):
                        headers[header] = str(self._data[field])
                async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
                    response = await client.post(
                        self.proxy_url + "/files",
                        headers=headers,
                        params={"path": path, "username": "root"},
                        content=request.encode(),
                    )
                    response.raise_for_status()
                code = code.replace(
                    "ns['main'](input())",
                    f"from pathlib import Path; p=Path('{path}'); "
                    "r=p.read_text(); p.unlink(); ns['main'](r)",
                )
                command = "python3 -I -c " + shlex.quote(code)
            control = action in ("initialize_execution", "cancel_execution", "finish_execution")
            response = json.loads(await self._process(command, timeout=135, control=control))
            if not response.get("ok"):
                return {
                    "content": response.get("error", "Remote operation failed"),
                    "is_error": True,
                    "error_type": "sandbox_operation_error",
                }
            return response["result"]
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, ExecutionUnavailable) as exc:
            self._broken = True
            raise ExecutionUnavailable(
                "CubeSandbox execution connection failed. Task stopped; no local fallback."
            ) from exc

    async def invoke(self, name: str, params: dict[str, object]) -> ToolResult:
        self._check_cancel()
        started = time.monotonic()
        await self._emit("sandbox.using", "execute", tool_name=name)
        try:
            value = await self._worker(name, params)
            self._check_cancel()
        except ExecutionUnavailable:
            await self._emit(
                "sandbox.failed",
                "execute",
                reason="connection_failed",
                tool_name=name,
                started=started,
            )
            raise
        return ToolResult(
            content=str(value["content"]),
            is_error=bool(value.get("is_error")),
            error_type=value.get("error_type"),
        )

    def _check_cancel(self) -> None:
        if self._cancel_requested:
            raise asyncio.CancelledError()

    async def export(self) -> Any:
        await self._emit("sandbox.using", "export")
        try:
            value = await self._worker(
                "export",
                {
                    "max_files": self.config.max_files,
                    "max_file_bytes": self.config.max_file_bytes,
                    "max_total_bytes": self.config.max_total_bytes,
                },
            )
        except ExecutionUnavailable:
            await self._emit("sandbox.failed", "export", reason="connection_failed")
            raise
        if not isinstance(value, list):
            raise ValueError(value.get("content", "Sandbox export failed"))
        return value

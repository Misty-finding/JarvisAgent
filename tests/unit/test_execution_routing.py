from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarvis.core.config import JarvisConfig
from jarvis.core.context import ExecutionContext
from jarvis.core.events.bus import EventBus
from jarvis.core.execution.base import BackendTool, ExecutionUnavailable
from jarvis.core.execution.cube import CubeBackend
from jarvis.core.execution.settings import ExecutionConfig
from jarvis.core.llm.types import LlmResponse, ToolCallBlock
from jarvis.core.loop import AgentLoop
from jarvis.core.mcp.server import McpServerManager
from jarvis.core.runner import AgentRunner
from jarvis.core.session.model import Session
from jarvis.core.subagent.registry import BackgroundTaskRegistry
from jarvis.core.subagent.tool import SpawnAgentTool
from jarvis.core.tools.base import BaseTool, ToolResult
from jarvis.core.tools.builtin import BashTool
from jarvis.core.tools.registry import ToolRegistry


class FakeBackend:
    runtime_description = "Linux /workspace/project Bash"

    async def invoke(self, name: str, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="remote-only")


class FatalTool(BaseTool):
    name = "fatal"
    description = "test"
    input_schema: dict[str, object] = {}

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        raise ExecutionUnavailable("connection lost; local execution disabled")


class CountingModel:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs: Any) -> LlmResponse:
        self.calls += 1
        return LlmResponse(
            stop_reason="tool_use",
            tool_calls=[
                ToolCallBlock(id="failure", name="fatal", input={}),
                ToolCallBlock(id="must-not-execute", name="bash", input={"command": "echo unsafe"}),
            ],
        )


async def test_fatal_backend_error_stops_batch_and_model_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> ToolResult:
        pytest.fail("Host shell must not execute after a fatal backend error")

    monkeypatch.setattr(BashTool, "invoke", forbidden)
    registry = ToolRegistry()
    registry.register(FatalTool())
    registry.register(BashTool())
    model = CountingModel()
    context = ExecutionContext(run_id="test", goal="test", max_steps=10)
    await AgentLoop(model, registry, EventBus()).run(context)
    assert context.reason == "execution_unavailable"
    assert model.calls == 1
    results = context.messages[-1]["content"]
    assert len(results) == 2 and all(block["is_error"] for block in results)


async def test_cube_unavailable_never_reaches_host_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(self: CubeBackend) -> None:
        raise ExecutionUnavailable("CubeSandbox unavailable")

    async def forbidden(*args: Any, **kwargs: Any) -> ToolResult:
        pytest.fail("Local shell fallback")

    monkeypatch.setattr(CubeBackend, "prepare", unavailable)
    monkeypatch.setattr(BashTool, "invoke", forbidden)
    config = JarvisConfig(workspace=tmp_path)
    config.execution = ExecutionConfig(backend="cubesandbox", template="test")
    model = CountingModel()
    outcome = await AgentRunner(config, provider=model, runs_dir=tmp_path / "runs").run_and_capture(
        "test"
    )
    assert outcome.reason == "execution_unavailable" and model.calls == 0


async def test_wrapper_and_subagents_share_backend(tmp_path: Path) -> None:
    backend = FakeBackend()
    wrapper = BackendTool(BashTool(), backend)
    assert (await wrapper.invoke({"command": "echo test"})).content == "remote-only"
    spawn = SpawnAgentTool(
        provider=CountingModel(),
        parent_bus=EventBus(),
        parent_run_id="parent",
        permission_manager=None,
        max_steps=5,
        task_registry=BackgroundTaskRegistry(),
        runs_dir=tmp_path,
        session_id="sess-test",
        backend=backend,
    )
    registry = spawn._build_child_registry(EventBus(), "child", None)
    for name in ("bash", "read_file", "write_file", "list_dir", "search_files", "project_map"):
        tool = registry.get(name)
        assert isinstance(tool, BackendTool) and tool._backend is backend
    nested = registry.get("spawn_agent")
    assert isinstance(nested, SpawnAgentTool) and nested._backend is backend


async def test_sandbox_session_cannot_switch_to_local(tmp_path: Path) -> None:
    session = Session(
        id="sess-test",
        mode="chat",
        status="active",
        title="test",
        created_at="now",
        updated_at="now",
        execution_backend="cubesandbox",
    )
    config = JarvisConfig(workspace=tmp_path)
    outcome = await AgentRunner(
        config, provider=CountingModel(), runs_dir=tmp_path / "runs"
    ).run_and_capture(
        "test",
        session=session,
    )
    assert outcome.reason == "execution_unavailable"


async def test_mcp_is_not_started_in_isolated_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core.config import McpServerConfig

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Host MCP process started")

    manager = McpServerManager()
    monkeypatch.setattr(manager, "_connect", forbidden)
    await manager.start_all(
        [McpServerConfig(name="local-shell", command="powershell")], isolated=True
    )
    assert manager.get_tools() == []


async def test_unreachable_real_endpoint_stops_before_model(tmp_path: Path) -> None:
    config = JarvisConfig(workspace=tmp_path)
    config.execution = ExecutionConfig(
        backend="cubesandbox",
        api_url="http://127.0.0.1:1",
        proxy_url="http://127.0.0.1:1",
        template="unreachable",
    )
    model = CountingModel()
    outcome = await AgentRunner(config, provider=model, runs_dir=tmp_path / "runs").run_and_capture(
        "Never run this locally",
    )
    assert outcome.reason == "execution_unavailable" and model.calls == 0


async def test_approval_denial_prevents_remote_execution(tmp_path: Path) -> None:
    from jarvis.core.llm.types import ToolCallBlock
    from jarvis.core.permissions.manager import PermissionManager
    from jarvis.core.tools.invocation import invoke_tool

    class DeniedBackend(FakeBackend):
        async def invoke(self, name: str, params: dict[str, object]) -> ToolResult:
            pytest.fail("Denied operation reached sandbox")

    permission = PermissionManager(policy_file=tmp_path / "permissions.json")
    registry = ToolRegistry()
    registry.register(BackendTool(BashTool(), DeniedBackend()))
    bus = EventBus()

    async def deny(event: Any) -> None:
        if event.type == "permission.requested":
            permission.respond(event.tool_use_id, "deny_once")

    bus.subscribe(deny)
    result = await invoke_tool(
        registry,
        ToolCallBlock(id="denied", name="bash", input={"command": "echo denied"}),
        bus,
        "run",
        permission_manager=permission,
        session_id="sess-test",
    )
    assert result.error_type == "permission_denied"


async def test_changed_network_policy_rejects_reuse(tmp_path: Path) -> None:
    backend = CubeBackend(ExecutionConfig(template="test"), tmp_path, tmp_path / ".jarvis")
    backend._data = {"sandboxID": "original"}
    backend._save(import_complete=True)
    changed = CubeBackend(
        ExecutionConfig(template="test", allow_internet_access=True), tmp_path, tmp_path / ".jarvis"
    )
    with pytest.raises(ExecutionUnavailable, match="mapping is invalid"):
        await changed.prepare()


def test_custom_runtime_directory_is_not_imported(tmp_path: Path) -> None:
    from jarvis.core.execution.project import collect_project

    runtime = tmp_path / "custom-runtime"
    runtime.mkdir()
    (runtime / "trace.json").write_text("private session history")
    (tmp_path / "main.py").write_text("print('hello')")
    backend = CubeBackend(
        ExecutionConfig(), tmp_path, runtime / "execution", private_paths=[runtime]
    )
    baseline = collect_project(tmp_path, backend._copy_config)
    assert [entry["path"] for entry in baseline["files"]] == ["main.py"]


async def test_tui_stop_uses_session_cancel() -> None:
    from jarvis.tui.app import JarvisTuiApp

    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        async def send_command(self, method: str, params: dict[str, object]) -> None:
            calls.append((method, params))

    class Screen:
        _client = Client()
        _session_id = "sess-cancel"
        _busy = True

        def notify(self, *args: Any, **kwargs: Any) -> None:
            pass

    await JarvisTuiApp.action_cancel_run(Screen())  # type: ignore[arg-type]
    assert calls == [("session.cancel", {"session_id": "sess-cancel"})]

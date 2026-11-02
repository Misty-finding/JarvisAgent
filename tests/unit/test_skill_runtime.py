from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarvis.core.config import JarvisConfig
from jarvis.core.events.bus import EventBus
from jarvis.core.llm.types import LlmResponse, ToolCallBlock
from jarvis.core.permissions.policy import PermissionDecision, evaluate
from jarvis.core.runner import AgentRunner
from jarvis.core.session.manager import SessionManager
from jarvis.core.session.store import SessionStore
from jarvis.core.skills.loader import SkillLoader
from jarvis.core.skills.tool import ActivateSkillTool


# 创建包含引用资源的标准 Skill，正文故意不使用旧参数占位符
def _skill(workspace: Path) -> Path:
    root = workspace / ".agents" / "skills" / "coding-demo"
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: coding-demo\ndescription: Debug asynchronous Python code.\n"
        "allowed-tools: Read Bash(git:*)\n---\n"
        "SPECIALIZED-INSTRUCTIONS: inspect references/async.md", encoding="utf-8",
    )
    (root / "references" / "async.md").write_text("ASYNC-REFERENCE-EVIDENCE", encoding="utf-8")
    return root


class _SkillProvider:
    # 保存执行方式及每轮模型能看到的上下文
    def __init__(self, root: Path, automatic: bool) -> None:
        self.root = root
        self.automatic = automatic
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, object]], str]] = []

    # 使用真实工具链激活 Skill 并读取资源，不依赖外部模型服务
    async def chat(
        self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, object]],
        bus: EventBus, run_id: str, *, step: int = 0, system: str | None = None,
    ) -> LlmResponse:
        self.calls.append(([dict(message) for message in messages], tool_schemas, system or ""))
        if self.automatic and len(self.calls) == 1:
            activation = next(tool for tool in tool_schemas if tool["name"] == "activate_skill")
            assert "coding-demo" in str(activation)
            assert "SPECIALIZED-INSTRUCTIONS" not in str(activation)
            return LlmResponse(stop_reason="tool_use", tool_calls=[ToolCallBlock(
                id="activate", name="activate_skill", input={"name": "coding-demo"},
            )])
        if len(self.calls) == (2 if self.automatic else 1):
            assert "SPECIALIZED-INSTRUCTIONS" in (str(messages) + str(system))
            return LlmResponse(stop_reason="tool_use", tool_calls=[ToolCallBlock(
                id="reference", name="read_file",
                input={"path": str(self.root / "references" / "async.md")},
            )])
        assert "ASYNC-REFERENCE-EVIDENCE" in str(messages)
        return LlmResponse(stop_reason="end_turn", text="verified skill and reference")


# 功能：斜杠调用和模型自主激活均能加载正文、读取绝对路径资源并保留原生运行规则
# 设计：通过真实 SessionManager、AgentRunner 和工具执行链，只替换模型响应以验证跨模块契约
@pytest.mark.parametrize("automatic", [False, True])
async def test_skill_activation_through_session_and_runner(tmp_path: Path, automatic: bool) -> None:
    root = _skill(tmp_path)
    cfg = JarvisConfig(workspace=tmp_path)
    cfg.agent.max_steps = 5
    provider = _SkillProvider(root, automatic)
    bus = EventBus()
    events: list[Any] = []

    # 收集真实事件，确认两种入口都对 TUI 发布激活信息
    async def collect(event: Any) -> None:
        events.append(event)

    bus.subscribe(collect)
    runner = AgentRunner(cfg, provider=provider, bus=bus)  # type: ignore[arg-type]
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: runner, bus, workspace=tmp_path)
    session = await manager.create("chat")
    await manager.send_message(session.id, "debug cancellation" if automatic
                               else "/coding-demo debug cancellation")
    assert session.last_run_state == "completed"
    assert len([event for event in events if event.type == "skill.invoked"]) == 1
    assert all("persistent software-engineering agent" in call[2] for call in provider.calls)
    assert "debug cancellation" in str(provider.calls[0])
    assert any(message["role"] == "assistant" for message in store.read_messages(session.id))
    # 标准 allowed-tools 不应误筛掉原生工具，也不应免除执行审批
    assert "bash" in {tool["name"] for tool in provider.calls[0][1]}
    assert evaluate("bash", {"command": "git status"}) == PermissionDecision.ASK
    assert evaluate("activate_skill", {}) == PermissionDecision.ALLOW


# 功能：禁止模型自动调用的 Skill 不泄露到目录，旧工具限制也不会被自动激活绕过
# 设计：同时测试工具 schema、直接调用和未知名称，防止仅前端隐藏而后端仍允许读取
async def test_skill_activation_respects_discovery_filters(tmp_path: Path) -> None:
    root = _skill(tmp_path)
    path = root / "SKILL.md"
    path.write_text(path.read_text().replace("allowed-tools:", "disable-model-invocation: true\nallowed-tools:"))
    loader = SkillLoader(tmp_path)
    tool = ActivateSkillTool(loader, EventBus(), "test-run")
    assert "coding-demo" not in tool.description
    assert (await tool.invoke({"name": "coding-demo"})).is_error
    assert (await tool.invoke({"name": "missing"})).is_error
    path.write_text(path.read_text().replace("disable-model-invocation: true\n", "")
                    .replace("allowed-tools: Read Bash(git:*)", "allowed_tools: [read_file]"))
    assert (await tool.invoke({"name": "coding-demo"})).is_error

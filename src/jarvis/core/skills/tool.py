from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from jarvis.core.bus.events import SkillInvokedEvent
from jarvis.core.events.bus import EventBus
from jarvis.core.skills.loader import SkillLoader
from jarvis.core.tools.base import BaseTool, ToolResult


class ActivateSkillParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: str = ""


class ActivateSkillTool(BaseTool):
    name = "activate_skill"
    params_model = ActivateSkillParams

    # 将名称和描述放入工具目录，完整正文与资源仅在激活时返回
    def __init__(
        self, loader: SkillLoader, bus: EventBus, run_id: str, session_id: str = "",
    ) -> None:
        self._loader = loader
        self._bus = bus
        self._run_id = run_id
        self._session_id = session_id
        skills = loader.list_all_skills()
        catalog = [{"name": skill.name, "description": skill.description} for skill in skills
                   if not skill.legacy_tool_restrictions
                   and skill.frontmatter.get("disable-model-invocation") is not True]
        self.available = bool(catalog)
        self.description = (
            "Load specialized instructions when a task matches a skill's description. "
            "Call before applying that skill. Only metadata is listed here; bundled files "
            "are read on demand using read_file. Loading a skill does not execute scripts "
            "or change tool permissions. Available skills: "
            + json.dumps(catalog, ensure_ascii=False)
        )
        self.input_schema: dict[str, object] = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": [item["name"] for item in catalog]},
                "arguments": {"type": "string", "description": "Task-specific requirements"},
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    # 加载选定 Skill 并发布与斜杠命令一致的激活事件
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = ActivateSkillParams.model_validate(params)
        skill = self._loader.resolve(parsed.name)
        if (skill is None or skill.legacy_tool_restrictions
                or skill.frontmatter.get("disable-model-invocation") is True):
            return ToolResult(content=f"Skill unavailable: {parsed.name}", is_error=True,
                              error_type="schema_error")
        prompt = self._loader.render_prompt(skill, parsed.arguments)
        await self._bus.publish(SkillInvokedEvent(
            skill_name=skill.name, arguments=parsed.arguments, run_id=self._run_id,
            session_id=self._session_id or None, ts=datetime.now(UTC).isoformat(),
        ))
        return ToolResult(
            content=f"<skill_content name=\"{skill.name}\">\n{prompt}\n</skill_content>"
        )

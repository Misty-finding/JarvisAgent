from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)
_FRONTMATTER_RE = re.compile(r"\A---[^\S\n]*\n(.*?)\n---[^\S\n]*(?:\n|\Z)", re.DOTALL)
_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_TOOL_RE = re.compile(r"[^\s,()]+(?:\([^)]*\))?")


@dataclass
class Skill:
    name: str
    description: str
    system_prompt_template: str
    allowed_tools: list[str] = field(default_factory=list)
    source_path: Path | None = None
    license: str = ""
    compatibility: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    legacy_tool_restrictions: bool = False

    @property
    # 返回 Skill 根目录，供相对资源路径解析使用
    def base_dir(self) -> Path | None:
        return self.source_path.parent if self.source_path is not None else None

    @property
    # 保留旧版工具白名单语义，标准 allowed-tools 仅声明预批准建议
    def tool_whitelist(self) -> list[str] | None:
        if self.legacy_tool_restrictions:
            return self.allowed_tools or None
        return None

    # 将资源路径解析为 Skill 目录内的绝对路径，拒绝目录逃逸
    def resolve_resource(self, relative_path: str) -> Path:
        if self.base_dir is None:
            raise ValueError("Skill has no source directory")
        root = self.base_dir.resolve()
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Resource path must stay inside the skill directory")
        return path


# 读取 YAML 字符串字段并校验标准长度与必填约束
def _string_field(
    front: dict[str, Any], key: str, *, required: bool = False, limit: int | None = None,
) -> str:
    value = front.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    if limit is not None and len(value) > limit:
        raise ValueError(f"{key} must be at most {limit} characters")
    return value


# 解析标准空格分隔工具声明，同时兼容旧版 YAML 列表
def _parse_tools(value: Any) -> list[str]:
    if isinstance(value, str):
        return _TOOL_RE.findall(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError("allowed-tools must be a string or a list of tool names")


# 安全解析标准 SKILL.md，兼容原有扁平 Markdown 和 allowed_tools 扩展
def _parse_skill_file(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8-sig")
    standard = path.name == "SKILL.md"
    front: dict[str, Any] = {}
    body = text
    match = _FRONTMATTER_RE.match(text)
    if match:
        loaded = yaml.safe_load(match.group(1))
        if not isinstance(loaded, dict) or any(not isinstance(key, str) for key in loaded):
            raise ValueError("Skill frontmatter must be a YAML mapping")
        front = loaded
        body = text[match.end():]
    elif standard or text.startswith("---\n"):
        raise ValueError("Skill must contain valid YAML frontmatter delimiters")

    default_name = path.parent.name if standard else path.stem
    name = _string_field(front, "name", required=standard) or default_name
    if standard and (
        len(name) > 64 or not _NAME_RE.fullmatch(name) or name != path.parent.name
    ):
        raise ValueError("Skill name must match its directory and use 1-64 lowercase letters, "
                         "digits or single hyphens")
    # 旧文件名允许下划线，但所有名称都禁止路径穿越与空白
    if any(char in name for char in "/\\") or any(char.isspace() for char in name):
        raise ValueError("Skill name cannot contain path separators or whitespace")
    description = _string_field(front, "description", required=standard, limit=1024)
    compatibility = _string_field(front, "compatibility", limit=500)
    metadata = front.get("metadata", {})
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items()
    ):
        raise ValueError("metadata must map string keys to string values")
    tools = _parse_tools(front.get("allowed-tools", front.get("allowed_tools", [])))
    return Skill(
        name=name,
        description=description,
        system_prompt_template=body.strip(),
        allowed_tools=tools,
        source_path=path.resolve(),
        license=_string_field(front, "license"),
        compatibility=compatibility,
        metadata=metadata,
        frontmatter=front,
        legacy_tool_restrictions="allowed_tools" in front and "allowed-tools" not in front,
    )


class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 固定工作区，避免后台 Core 的启动目录影响项目 Skill 发现
    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = (workspace or Path.cwd()).expanduser().resolve()

    @property
    # 返回当前 Skill 搜索工作区
    def workspace(self) -> Path:
        return self._workspace

    # 返回从低到高的搜索优先级，项目覆盖全局、Jarvis 专用目录覆盖通用目录
    def _directories(self) -> list[Path]:
        return [
            self._BUILTIN_DIR,
            Path("~/.agents/skills").expanduser(),
            Path("~/.jarvis/skills").expanduser(),
            self._workspace / ".agents" / "skills",
            self._workspace / ".jarvis" / "skills",
        ]

    # 按已发现元数据名称查找 Skill，拒绝将外部输入拼接成文件路径
    def resolve(self, name: str) -> Skill | None:
        return next((skill for skill in self.list_all_skills() if skill.name == name), None)

    # 列出可解析 Skill 的名称，保持补全和实际激活结果一致
    def list_all(self) -> list[str]:
        return [skill.name for skill in self.list_all_skills()]

    # 读取元数据与正文但不展开资源，隔离损坏 Skill 并记录可诊断日志
    def list_all_skills(self) -> list[Skill]:
        seen: dict[str, Skill] = {}
        for directory in self._directories():
            paths = sorted(directory.glob("*.md")) + sorted(directory.glob("*/SKILL.md"))
            for path in paths:
                # 通用目录仅接受标准目录式格式；旧扁平格式保留在 Jarvis 目录
                if directory.name == "skills" and directory.parent.name == ".agents":
                    if path.parent == directory:
                        continue
                try:
                    skill = _parse_skill_file(path)
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    log.warning("Skipping invalid skill %s: %s", path, exc)
                    continue
                if skill.name in seen:
                    log.debug("Skill %s at %s overrides %s", skill.name, path,
                              seen[skill.name].source_path)
                seen[skill.name] = skill
        return list(seen.values())

    # 只渲染激活的 Skill，附上绝对根目录、参数和资源索引而不读取资源正文
    def render_prompt(self, skill: Skill, arguments: str) -> str:
        parts = [skill.system_prompt_template.replace("$ARGUMENTS", arguments)]
        if arguments:
            parts.append("User request / arguments:\n" + arguments)
        if skill.base_dir is not None:
            parts.append(
                f"Skill directory: {skill.base_dir}\n"
                "Resolve all relative file references against this directory; use absolute "
                "paths in tool calls. Keep the project working directory for project commands. "
                "Read references and assets only when needed. Run bundled scripts only when "
                "needed for the user's task, through the normal permission policy."
            )
            resources: list[str] = []
            for folder in ("scripts", "references", "assets"):
                for path in sorted((skill.base_dir / folder).rglob("*")):
                    if path.is_file() and path.resolve().is_relative_to(skill.base_dir):
                        resources.append(path.relative_to(skill.base_dir).as_posix())
                    if len(resources) >= 100:
                        break
                if len(resources) >= 100:
                    break
            if resources:
                parts.append("Bundled resources (up to 100 paths; contents loaded on demand):\n"
                             + "\n".join(resources))
        if skill.compatibility:
            parts.append("Compatibility requirements: " + skill.compatibility)
        if skill.allowed_tools and not skill.legacy_tool_restrictions:
            parts.append(
                "Upstream allowed-tools declaration: " + json.dumps(skill.allowed_tools)
                + ". This metadata does not grant permission or override Jarvis tool policy."
            )
        parts.append(
            "Jarvis tool equivalents: Read=read_file; Grep/Glob=search_files; "
            "Bash=bash (native shell); Write=write_file; Edit=read_file then write_file; "
            "Skill=activate_skill; Task=spawn_agent. Use only tools actually available. "
            "Follow the user's request, runtime rules and repository instructions when they "
            "differ from generic skill guidance."
        )
        return "\n\n".join(parts)

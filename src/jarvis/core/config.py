from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.jarvis/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.jarvis/config.toml"
_DEFAULT_MAX_STEPS = 60
_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_LLM_PROVIDER = "anthropic"
_DEFAULT_TRACE_FILE = "~/.jarvis/traces/daemon.jsonl"


@dataclass
class LoggingConfig:
    level: str = _DEFAULT_LOG_LEVEL
    file: str = _DEFAULT_LOG_FILE
    format: str = _DEFAULT_LOG_FORMAT  # "text" | "json"


@dataclass
class AgentConfig:
    max_steps: int = _DEFAULT_MAX_STEPS


@dataclass
class LlmConfig:
    provider: str = _DEFAULT_LLM_PROVIDER
    default_model: str = _DEFAULT_MODEL
    base_url: str | None = None
    anthropic_api_key: str | None = field(default=None, repr=False)
    deepseek_api_key: str | None = field(default=None, repr=False)
    router: str = "static"  # "static" | "rule_based" (S4) | "cost_budget" (S6)


@dataclass
class TraceConfig:
    enabled: bool = True
    file: str = _DEFAULT_TRACE_FILE
    include_llm_payload: bool = True  # false 时 LLM 记录只保留摘要


@dataclass
class PermissionConfig:
    timeout_s: float = 60.0  # 审批超时秒数；0 表示不超时


@dataclass
class CompactionConfig:
    # context_pct 触发自动压缩的阈值（0 表示禁用，推荐用手动 /compact）
    auto_threshold: float = 0.0
    tool_result_limit: int = 8_000  # tool_result 截断触发字符数
    tool_result_keep: int = 4_000   # 截断后保留的前缀字符数


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"       # "stdio" | "tcp"
    command: str = ""              # stdio 专用：可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    host: str = "localhost"        # tcp 专用
    port: int = 3000               # tcp 专用


@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)


@dataclass
class JarvisConfig:
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    workspace: Path = field(default_factory=lambda: Path.cwd().resolve())
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    mcp: McpConfig = field(default_factory=McpConfig)


# 构建并返回运行时配置：默认值 → 全局 TOML → 项目本地 TOML → .env → 系统环境变量（后者优先级最高）
def get_config() -> JarvisConfig:
    config = JarvisConfig()

    global_env = dotenv_values(Path("~/.jarvis/.env").expanduser())
    project_env = dotenv_values(Path(".env"))
    system_env: Mapping[str, str | None] = os.environ

    # JARVIS_CONFIG 的来源本身也遵循 系统 > 项目 .env > 全局 .env。
    explicit = (
        system_env.get("JARVIS_CONFIG")
        or project_env.get("JARVIS_CONFIG")
        or global_env.get("JARVIS_CONFIG")
    )
    def apply_config_file(config_path: Path) -> None:
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"Config parse error ({config_path}): {e}") from e
            _apply_toml(config, data)

    # 明确分层，避免 load_dotenv 修改进程环境后造成跨项目配置泄漏。
    if explicit:
        _apply_env(config, global_env)
        apply_config_file(Path(explicit).expanduser())
    else:
        apply_config_file(Path(_DEFAULT_CONFIG_PATH).expanduser())
        _apply_env(config, global_env)
        local_config = Path(".jarvis/config.toml")
        apply_config_file(local_config)
    _apply_env(config, project_env)
    _apply_env(config, system_env)
    return config


# 将已解析的 TOML 根表写入 config；未知小节或类型错误时退出进程
def _apply_toml(config: JarvisConfig, data: dict[str, Any]) -> None:
    known_sections = {
        "core",
        "logging",
        "agent",
        "llm",
        "trace",
        "permission",
        "compaction",
        "mcp",
    }
    unknown = set(data.keys()) - known_sections
    if unknown:
        raise SystemExit(f"Unknown top-level config keys: {', '.join(sorted(unknown))}")

    if "core" in data:
        core = data["core"]
        if not isinstance(core, dict):
            raise SystemExit("Config error: [core] must be a table")
        unknown_core: set[str] = set(core.keys()) - {"host", "port"}
        if unknown_core:
            raise SystemExit(f"Unknown [core] keys: {', '.join(sorted(unknown_core))}")
        if "host" in core:
            val = core["host"]
            if not isinstance(val, str):
                raise SystemExit("Config error: core.host must be a string")
            config.host = val
        if "port" in core:
            val = core["port"]
            if not isinstance(val, int):
                raise SystemExit("Config error: core.port must be an integer")
            config.port = val

    if "logging" in data:
        log = data["logging"]
        if not isinstance(log, dict):
            raise SystemExit("Config error: [logging] must be a table")
        unknown_log: set[str] = set(log.keys()) - {"level", "file", "format"}
        if unknown_log:
            raise SystemExit(f"Unknown [logging] keys: {', '.join(sorted(unknown_log))}")
        for key in ("level", "file", "format"):
            if key in log:
                val = log[key]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: logging.{key} must be a string")
                setattr(config.logging, key, val)

    if "agent" in data:
        agent = data["agent"]
        if not isinstance(agent, dict):
            raise SystemExit("Config error: [agent] must be a table")
        unknown_agent: set[str] = set(agent.keys()) - {"max_steps"}
        if unknown_agent:
            raise SystemExit(f"Unknown [agent] keys: {', '.join(sorted(unknown_agent))}")
        if "max_steps" in agent:
            val = agent["max_steps"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: agent.max_steps must be a positive integer")
            config.agent.max_steps = val

    if "llm" in data:
        llm = data["llm"]
        if not isinstance(llm, dict):
            raise SystemExit("Config error: [llm] must be a table")
        unknown_llm: set[str] = set(llm.keys()) - {
            "provider",
            "default_model",
            "base_url",
            "router",
        }
        if unknown_llm:
            raise SystemExit(f"Unknown [llm] keys: {', '.join(sorted(unknown_llm))}")
        if "provider" in llm:
            val = llm["provider"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.provider must be a string")
            config.llm.provider = val
        if "default_model" in llm:
            val = llm["default_model"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.default_model must be a string")
            config.llm.default_model = val
        if "base_url" in llm:
            val = llm["base_url"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.base_url must be a string")
            config.llm.base_url = val
        if "router" in llm:
            val = llm["router"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.router must be a string")
            config.llm.router = val

    if "trace" in data:
        trace = data["trace"]
        if not isinstance(trace, dict):
            raise SystemExit("Config error: [trace] must be a table")
        unknown_trace: set[str] = set(trace.keys()) - {"enabled", "file", "include_llm_payload"}
        if unknown_trace:
            raise SystemExit(f"Unknown [trace] keys: {', '.join(sorted(unknown_trace))}")
        if "enabled" in trace:
            val = trace["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.enabled must be a boolean")
            config.trace.enabled = val
        if "file" in trace:
            val = trace["file"]
            if not isinstance(val, str):
                raise SystemExit("Config error: trace.file must be a string")
            config.trace.file = val
        if "include_llm_payload" in trace:
            val = trace["include_llm_payload"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.include_llm_payload must be a boolean")
            config.trace.include_llm_payload = val

    if "permission" in data:
        perm = data["permission"]
        if not isinstance(perm, dict):
            raise SystemExit("Config error: [permission] must be a table")
        unknown_perm: set[str] = set(perm.keys()) - {"timeout_s"}
        if unknown_perm:
            raise SystemExit(f"Unknown [permission] keys: {', '.join(sorted(unknown_perm))}")
        if "timeout_s" in perm:
            val = perm["timeout_s"]
            if not isinstance(val, (int, float)) or val < 0:
                raise SystemExit("Config error: permission.timeout_s must be a non-negative number")
            config.permission.timeout_s = float(val)

    if "compaction" in data:
        comp = data["compaction"]
        if not isinstance(comp, dict):
            raise SystemExit("Config error: [compaction] must be a table")
        unknown_comp: set[str] = set(comp.keys()) - {
            "auto_threshold",
            "tool_result_limit",
            "tool_result_keep",
        }
        if unknown_comp:
            raise SystemExit(f"Unknown [compaction] keys: {', '.join(sorted(unknown_comp))}")
        if "auto_threshold" in comp:
            val = comp["auto_threshold"]
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise SystemExit("Config error: compaction.auto_threshold must be between 0 and 1")
            config.compaction.auto_threshold = float(val)
        if "tool_result_limit" in comp:
            val = comp["tool_result_limit"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_limit must be a positive integer"
                )
            config.compaction.tool_result_limit = val
        if "tool_result_keep" in comp:
            val = comp["tool_result_keep"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_keep must be a positive integer"
                )
            config.compaction.tool_result_keep = val

    if "mcp" in data:
        mcp = data["mcp"]
        if not isinstance(mcp, dict):
            raise SystemExit("Config error: [mcp] must be a table")
        unknown_mcp: set[str] = set(mcp.keys()) - {"servers"}
        if unknown_mcp:
            raise SystemExit(f"Unknown [mcp] keys: {', '.join(sorted(unknown_mcp))}")
        servers_raw = mcp.get("servers", [])
        if not isinstance(servers_raw, list):
            raise SystemExit("Config error: mcp.servers must be an array of tables")
        for i, srv in enumerate(servers_raw):
            if not isinstance(srv, dict):
                raise SystemExit(f"Config error: mcp.servers[{i}] must be a table")
            name = srv.get("name")
            if not isinstance(name, str) or not name:
                raise SystemExit(f"Config error: mcp.servers[{i}].name must be a non-empty string")
            transport = srv.get("transport", "stdio")
            if transport not in ("stdio", "tcp"):
                raise SystemExit(
                    f"Config error: mcp.servers[{i}].transport must be 'stdio' or 'tcp'"
                )
            s = McpServerConfig(name=name, transport=transport)
            if "command" in srv:
                val = srv["command"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].command must be a string")
                s.command = val
            if "args" in srv:
                val = srv["args"]
                if not isinstance(val, list):
                    raise SystemExit(f"Config error: mcp.servers[{i}].args must be an array")
                s.args = [str(a) for a in val]
            if "env" in srv:
                val = srv["env"]
                if not isinstance(val, dict):
                    raise SystemExit(f"Config error: mcp.servers[{i}].env must be a table")
                s.env = {str(k): str(v) for k, v in val.items()}
            if "host" in srv:
                val = srv["host"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].host must be a string")
                s.host = val
            if "port" in srv:
                val = srv["port"]
                if not isinstance(val, int):
                    raise SystemExit(f"Config error: mcp.servers[{i}].port must be an integer")
                s.port = val
            config.mcp.servers.append(s)


# 用 JARVIS_* 环境变量覆盖 config 中对应字段（若变量已设置）
def _apply_env(config: JarvisConfig, env: Mapping[str, str | None]) -> None:
    host = env.get("JARVIS_HOST")
    if host is not None:
        config.host = host

    port_str = env.get("JARVIS_PORT")
    if port_str is not None:
        try:
            config.port = int(port_str)
        except ValueError:
            raise SystemExit(f"Config error: JARVIS_PORT must be an integer, got: {port_str!r}")

    log_level = env.get("JARVIS_LOG_LEVEL")
    if log_level is not None:
        config.logging.level = log_level

    log_file = env.get("JARVIS_LOG_FILE")
    if log_file is not None:
        config.logging.file = log_file

    log_format = env.get("JARVIS_LOG_FORMAT")
    if log_format is not None:
        config.logging.format = log_format

    max_steps_str = env.get("JARVIS_MAX_STEPS")
    if max_steps_str is not None:
        try:
            val = int(max_steps_str)
            if val <= 0:
                raise SystemExit(
                    "Config error: JARVIS_MAX_STEPS must be a positive integer,"
                    f" got: {max_steps_str!r}"
                )
            config.agent.max_steps = val
        except ValueError:
            raise SystemExit(
                f"Config error: JARVIS_MAX_STEPS must be an integer, got: {max_steps_str!r}"
            )

    provider = env.get("JARVIS_LLM_PROVIDER")
    if provider is not None:
        config.llm.provider = provider

    default_model = env.get("JARVIS_LLM_DEFAULT_MODEL")
    if default_model is not None:
        config.llm.default_model = default_model

    base_url = env.get("JARVIS_LLM_BASE_URL")
    if base_url is not None:
        config.llm.base_url = base_url

    anthropic_api_key = env.get("ANTHROPIC_API_KEY")
    if anthropic_api_key is not None:
        config.llm.anthropic_api_key = anthropic_api_key

    deepseek_api_key = env.get("DEEPSEEK_API_KEY")
    if deepseek_api_key is not None:
        config.llm.deepseek_api_key = deepseek_api_key

    workspace = env.get("JARVIS_WORKSPACE_ROOT")
    if workspace is not None:
        config.workspace = Path(workspace).expanduser().resolve()

    trace_enabled = env.get("JARVIS_TRACE_ENABLED")
    if trace_enabled is not None:
        config.trace.enabled = trace_enabled.lower() not in ("0", "false", "no")

    trace_file = env.get("JARVIS_TRACE_FILE")
    if trace_file is not None:
        config.trace.file = trace_file

    trace_payload = env.get("JARVIS_TRACE_INCLUDE_LLM_PAYLOAD")
    if trace_payload is not None:
        config.trace.include_llm_payload = trace_payload.lower() not in ("0", "false", "no")

    perm_timeout = env.get("JARVIS_PERMISSION_TIMEOUT_S")
    if perm_timeout is not None:
        try:
            perm_timeout_val = float(perm_timeout)
            if perm_timeout_val < 0:
                raise SystemExit(
                    f"Config error: JARVIS_PERMISSION_TIMEOUT_S must be >= 0, got: {perm_timeout!r}"
                )
            config.permission.timeout_s = perm_timeout_val
        except ValueError:
            raise SystemExit(
                f"Config error: JARVIS_PERMISSION_TIMEOUT_S must be a number, got: {perm_timeout!r}"
            )

    compact_threshold = env.get("JARVIS_COMPACT_THRESHOLD")
    if compact_threshold is not None:
        try:
            compact_threshold_val = float(compact_threshold)
            if not (0.0 <= compact_threshold_val <= 1.0):
                raise SystemExit(
                    "Config error: JARVIS_COMPACT_THRESHOLD must be between 0 and 1, "
                    f"got: {compact_threshold!r}"
                )
            config.compaction.auto_threshold = compact_threshold_val
        except ValueError:
            raise SystemExit(
                "Config error: JARVIS_COMPACT_THRESHOLD must be a number, "
                f"got: {compact_threshold!r}"
            )

    compact_tool_limit = env.get("JARVIS_COMPACT_TOOL_LIMIT")
    if compact_tool_limit is not None:
        try:
            compact_tool_limit_val = int(compact_tool_limit)
            if compact_tool_limit_val <= 0:
                raise SystemExit(
                    "Config error: JARVIS_COMPACT_TOOL_LIMIT must be a positive integer, "
                    f"got: {compact_tool_limit!r}"
                )
            config.compaction.tool_result_limit = compact_tool_limit_val
        except ValueError:
            raise SystemExit(
                "Config error: JARVIS_COMPACT_TOOL_LIMIT must be an integer, "
                f"got: {compact_tool_limit!r}"
            )

    compact_tool_keep = env.get("JARVIS_COMPACT_TOOL_KEEP")
    if compact_tool_keep is not None:
        try:
            compact_tool_keep_val = int(compact_tool_keep)
            if compact_tool_keep_val <= 0:
                raise SystemExit(
                    "Config error: JARVIS_COMPACT_TOOL_KEEP must be a positive integer, "
                    f"got: {compact_tool_keep!r}"
                )
            config.compaction.tool_result_keep = compact_tool_keep_val
        except ValueError:
            raise SystemExit(
                "Config error: JARVIS_COMPACT_TOOL_KEEP must be an integer, "
                f"got: {compact_tool_keep!r}"
            )

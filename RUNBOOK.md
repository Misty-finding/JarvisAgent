# 运维手册（RUNBOOK）

## 一条命令启动

首次在 Jarvis 仓库中安装全局命令：

```powershell
uv tool install --editable .
```

之后进入任意代码仓库，只需要：

```powershell
cd D:\path\to\your-project
jarvis
```

`jarvis` 会自动完成以下工作：

1. 把当前目录设为 Agent workspace。
2. 创建项目级 `.jarvis/` 目录。
3. 为当前项目复用或启动独立的 `jarvis-core`。
4. 端口被其他项目占用时自动选择空闲端口。
5. 等待 Core 健康检查通过后直接打开 TUI。

项目级运行状态写入 `.jarvis/runtime/`，会话写入 `.jarvis/sessions/`；这两个目录
默认由 `.jarvis/.gitignore` 忽略。`config.toml`、`context.md`、`skills/` 等项目资料
不会被忽略。

DeepSeek 等 API 配置可以放到全局文件，配置一次后所有项目共用：

```powershell
New-Item -ItemType Directory -Force "$HOME\.jarvis" | Out-Null
Copy-Item .env "$HOME\.jarvis\.env"
```

也可以只在某个项目根目录放置 `.env`，项目配置会覆盖全局配置。

## 日常操作

### 启动当前项目

```powershell
jarvis
```

直接进入 TUI；无需手动启动 Core。

### 管理当前项目 Core

```powershell
jarvis core status
jarvis core start
jarvis core stop
```

### 验证连通

```powershell
jarvis ping
# → pong server=0.0.1 uptime=12ms latency=2ms
```

---

## 配置

优先级（低 → 高）：**内建默认值 → `~/.jarvis/config.toml` →
`~/.jarvis/.env` → 项目 `.jarvis/config.toml` → 项目 `.env` → 系统环境变量**。

### `~/.jarvis/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level  = "INFO"
file   = "~/.jarvis/logs/core.log"
format = "text"    # "text" | "json"
```

### `.env`

从 `.env.example` 复制后修改，存放本机配置与密钥（不提交 git）：

```bash
cp .env.example .env
```

### 系统环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `JARVIS_CONFIG` | `~/.jarvis/config.toml` | 覆盖配置文件路径 |
| `JARVIS_WORKSPACE_ROOT` | 当前目录 | 启动器传给 Core 的 workspace，通常无需手动设置 |
| `JARVIS_HOST` | `127.0.0.1` | TCP 监听地址 |
| `JARVIS_PORT` | `7437` | TCP 监听端口 |
| `JARVIS_LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `JARVIS_LOG_FILE` | `~/.jarvis/logs/core.log` | 日志文件路径（留空则仅输出 stderr） |
| `JARVIS_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |
| `JARVIS_LLM_PROVIDER` | `anthropic` | LLM provider（`anthropic` 或 `deepseek`） |
| `JARVIS_LLM_DEFAULT_MODEL` | `claude-sonnet-4-6` | 默认模型名 |
| `JARVIS_LLM_BASE_URL` | provider 默认地址 | 可选 API base URL |
| `DEEPSEEK_API_KEY` | 无 | DeepSeek provider API key |
| `ANTHROPIC_API_KEY` | 无 | Anthropic provider API key |
| `JARVIS_MAX_STEPS` | `60` | 单次 Agent run 工具步骤预算，耗尽后额外总结一次 |
| `JARVIS_TRACE_ENABLED` | `true` | 是否启用 trace |
| `JARVIS_TRACE_INCLUDE_LLM_PAYLOAD` | `true` | trace 是否记录完整 LLM 请求和响应 |
| `JARVIS_PERMISSION_TIMEOUT_S` | `60` | 工具权限审批超时秒数 |

---

## 开发

```bash
uv run ruff check src tests scripts   # lint
uv run mypy src                       # 类型检查
uv run pytest tests/ -v               # 全量测试
uv run pytest tests/unit/ -v         # 仅单元测试（无需启动 daemon）

make docs                             # 重新生成 WIRE_PROTOCOL.md
make verify-s0                        # 完整验证（lint + 类型 + 测试 + 协议同源检查）
```

---

## 日志

```powershell
Get-Content "$HOME\.jarvis\logs\core.log" -Wait
```

---

## 常见错误

| 报错 | 原因 | 处理 |
|------|------|------|
| `core not running` | 当前项目 daemon 未启动 | 执行 `jarvis` 或 `jarvis core start` |
| 默认端口已占用 | 另一个项目或程序使用了 7437 | `jarvis` 会自动选择空闲端口 |
| `Config error: JARVIS_PORT must be an integer` | `.env` 或环境变量中端口值非整数 | 检查 `JARVIS_PORT` 的值 |

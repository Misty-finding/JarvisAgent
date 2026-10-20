# JarvisAgent

JarvisAgent 是一个面向本地代码仓库的 Coding Agent。它能够调用 LLM 读取代码、
检索文件、执行 Shell、修改文件、管理任务，并通过 TUI 展示流式回答、工具调用、
权限审批、上下文占用和运行日志。对话按项目保存，退出或重启 Core 后可以恢复
历史会话，继续使用已有上下文与 Notes。

项目采用 `jarvis-core daemon + CLI/TUI` 双进程架构：

```text
用户输入
  -> jarvis CLI / TUI
  -> JSON-RPC 2.0 over NDJSON/TCP
  -> jarvis-core
  -> Agent Loop
  -> LLM / Tools / Skills / Sub-Agent / MCP
  -> EventBus / Trace / Session Store
  -> TUI 实时展示
```

安装完成后，可以在任何代码仓库中直接执行：

```powershell
jarvis
```

Jarvis 会把当前目录作为 workspace，自动创建 `.jarvis/`，启动该项目对应的
Core daemon，然后进入 TUI。不需要先进入 JarvisAgent 源码目录，也不需要手动运行
`jarvis-core`。

日常使用入口：

| 目的 | 命令 |
|---|---|
| 在当前项目新建会话 | `jarvis` |
| 从当前项目的历史列表选择会话 | `jarvis --resume` |
| 继续指定会话 | `jarvis --resume sess-xxx` |
| 查看某次运行的历史事件 | `jarvis --replay <run-id>` |

## 功能概览

- ReAct Agent Loop：模型决策、工具执行、结果回填和继续决策
- 内置代码工具：项目结构、文件搜索、文件读写、PowerShell/Shell
- DeepSeek 与 Anthropic Provider
- Ask/Auto 权限模式，以及单次、长期允许或拒绝
- 按项目持久化会话，支持历史选择、指定 ID 恢复和中断提示
- Session Notes、Global/Project Context 与上下文压缩
- Skills、Sub-Agent 和 MCP 外部工具
- EventBus、`events.jsonl`、Trace 和日志
- Textual TUI，支持流式输出、复制回答和鼠标选择文本
- Windows 原生 PowerShell 支持

## 一、安装前准备

### 1. 安装 Git

确认命令可用：

```powershell
git --version
```

### 2. 安装 uv

Windows 可以使用：

```powershell
winget install --id=astral-sh.uv -e
```

安装后重新打开 PowerShell，验证：

```powershell
uv --version
```

### 3. 安装 Python 3.12

项目要求 Python `>=3.12,<3.13`。可以让 uv 独立安装，不需要替换系统 Python：

```powershell
uv python install 3.12
uv python find 3.12
```

## 二、下载并安装全局 jarvis 命令

### 方式 A：克隆后以可编辑模式安装，推荐开发者使用

```powershell
cd D:\agent
git clone https://github.com/Misty-finding/JarvisAgent.git
cd JarvisAgent
uv tool install --python 3.12 --editable .
```

可编辑安装的好处是：在这个仓库中更新代码后，全局 `jarvis` 命令会直接使用最新源码。

让 uv 的工具目录进入 PATH：

```powershell
uv tool update-shell
```

关闭并重新打开 PowerShell，然后验证：

```powershell
jarvis --version
Get-Command jarvis
```

### 方式 B：直接从 GitHub 安装

不准备修改源码时，可以直接安装：

```powershell
uv tool install --python 3.12 "git+https://github.com/Misty-finding/JarvisAgent.git@main"
uv tool update-shell
```

重新打开 PowerShell 后执行：

```powershell
jarvis --version
```

### 更新

可编辑安装：

```powershell
cd D:\agent\JarvisAgent
git pull --ff-only
uv tool install --python 3.12 --editable . --force
```

从 GitHub 安装：

```powershell
uv tool install --python 3.12 `
  "git+https://github.com/Misty-finding/JarvisAgent.git@main" --force
```

更新完成后，进入实际使用 Jarvis 的项目目录，重启该项目的 Core，再恢复会话：

```powershell
cd D:\work\my-project
jarvis core stop
jarvis --resume
```

已启动的 Core 会继续使用原进程中加载的代码和配置。先等待正在执行的任务结束，
再停止 Core；有多个项目时，分别在各项目目录执行。已有会话数据会保留。

## 三、配置 DeepSeek API

Jarvis 支持全局配置和项目配置。

- 全局配置：所有项目共用，放在 `$HOME\.jarvis\`
- 项目配置：只对当前项目生效，放在项目根目录
- API Key 推荐放在 `.env`，不要写进 Git
- 非敏感配置也可以写进 `config.toml`

### 1. 创建全局配置目录

```powershell
New-Item -ItemType Directory -Force "$HOME\.jarvis" | Out-Null
```

### 2. 创建 `$HOME\.jarvis\.env`

```powershell
@"
JARVIS_LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=替换成你的DeepSeek API Key
JARVIS_LLM_DEFAULT_MODEL=deepseek-flash
JARVIS_LLM_BASE_URL=https://api.deepseek.com/anthropic

JARVIS_MAX_STEPS=60
JARVIS_LOG_LEVEL=INFO
JARVIS_TRACE_ENABLED=true
JARVIS_TRACE_INCLUDE_LLM_PAYLOAD=false
JARVIS_PERMISSION_TIMEOUT_S=60
"@ | Set-Content -Encoding UTF8 "$HOME\.jarvis\.env"
```

必须替换这一行：

```text
DEEPSEEK_API_KEY=替换成你的DeepSeek API Key
```

Jarvis 使用 DeepSeek 的 Anthropic-compatible API，因此默认 Base URL 是：

```text
https://api.deepseek.com/anthropic
```

### 3. 可选：创建全局 `config.toml`

文件位置：

```text
C:\Users\<用户名>\.jarvis\config.toml
```

示例：

```toml
[core]
host = "127.0.0.1"
port = 7437

[llm]
provider = "deepseek"
default_model = "deepseek-flash"
base_url = "https://api.deepseek.com/anthropic"
router = "static"

[agent]
max_steps = 60

[logging]
level = "INFO"
file = "~/.jarvis/logs/core.log"
format = "text"

[trace]
enabled = true
file = "~/.jarvis/traces/daemon.jsonl"
include_llm_payload = false

[permission]
timeout_s = 60

[compaction]
auto_threshold = 0.0
tool_result_limit = 8000
tool_result_keep = 4000
```

API Key 不放在 TOML 中，仍然放在 `$HOME\.jarvis\.env`。

## 四、在任意目录启动

假设要分析的项目位于：

```text
D:\work\my-project
```

只需要：

```powershell
cd D:\work\my-project
jarvis
```

首次启动会自动完成：

1. 将 `D:\work\my-project` 设置为 workspace。
2. 创建 `D:\work\my-project\.jarvis\`。
3. 创建 `.jarvis\.gitignore`，忽略运行状态和会话记录。
4. 为当前 workspace 启动独立的 `jarvis-core`。
5. 默认端口被占用时自动选择可用端口。
6. 等待 Core 健康检查通过。
7. 启动 TUI 并创建会话。

生成的目录大致如下：

```text
my-project/
├─ .jarvis/
│  ├─ .gitignore
│  ├─ runtime/
│  │  ├─ core.json
│  │  ├─ core.pid
│  │  └─ core.stderr.log
│  ├─ sessions/
│  ├─ context.md          # 可选：项目说明
│  ├─ config.toml         # 可选：项目配置
│  ├─ skills/             # 可选：项目 Skills
│  └─ agents/             # 可选：项目 Sub-Agent Profiles
└─ ...
```

`.jarvis/runtime/` 和 `.jarvis/sessions/` 是运行产物，不应提交到 Git。
`context.md`、`config.toml`、`skills/` 和 `agents/` 可以按项目需要提交。

## 五、配置优先级

配置按以下顺序加载，右侧覆盖左侧：

```text
内建默认值
  -> ~/.jarvis/config.toml
  -> ~/.jarvis/.env
  -> <workspace>/.jarvis/config.toml
  -> <workspace>/.env
  -> 系统环境变量
```

因此推荐：

- DeepSeek API Key 放在 `$HOME\.jarvis\.env`
- 通用模型和日志配置放在 `$HOME\.jarvis\config.toml`
- 项目特殊配置放在 `<workspace>\.jarvis\config.toml`
- 临时覆盖使用项目 `.env` 或系统环境变量

### 项目级模型覆盖示例

在目标项目创建 `.jarvis\config.toml`：

```toml
[llm]
provider = "deepseek"
default_model = "deepseek-v4-pro"
base_url = "https://api.deepseek.com/anthropic"

[agent]
max_steps = 100
```

## 六、常用命令

### TUI 会话

```powershell
jarvis                       # 新建会话
jarvis --resume              # 选择当前项目的历史会话
jarvis --resume sess-xxx      # 恢复指定会话
jarvis --replay <run-id>      # 查看历史运行事件
```

`jarvis --resume` 的历史列表按最近更新时间排序，显示标题、会话 ID 和运行状态。
使用 `↑` / `↓` 选择、`Enter` 恢复、`Esc` 退出；也可以明确选择“新建会话”。正在运行的会话需要
等待本轮结束才能恢复。每个项目的历史独立存放在 `.jarvis/sessions/`。

恢复会加载已保存的对话；下一次输入会沿用这些上下文和 Notes。已压缩的会话加载
压缩后的摘要与后续消息。恢复完成后停在输入框，权限模式重新设为 Ask。
恢复本身不会请求模型，也不会自动重跑工具、后台任务或旧审批。

消息会在执行过程中逐条保存。若退出时工具结果尚未保存，恢复后会标注“实际效果
未知”，继续时应先检查文件、`git diff` 或外部状态。恢复会话不回滚工作区文件。
若仅最后一行 JSON 写入不完整，会备份并保留有效历史；历史中间损坏则报错，
不会静默丢弃。更多细节和手测步骤见 [会话恢复说明](docs/session-recovery.md)。

恢复适用于当前项目的聊天会话。`--resume` 与 `--replay` 互斥，恢复后需要发送
新消息才会开始下一轮执行；`jarvis run` 创建的一次性任务可通过 `--replay` 查看。

### Core 管理

以下命令均作用于当前项目：

```powershell
jarvis core status          # 查看 Core 状态
jarvis core start           # 启动或复用 Core
jarvis core stop            # 停止 Core
jarvis ping                 # 检查 IPC 连接
```

### Trace 与脚本调试

TUI 是主要交互入口。以下命令用于排查运行记录或脚本测试；`run`、`chat` 和 `ping`
需要已有 Core，可先执行 `jarvis core start`。

```powershell
jarvis trace
jarvis trace <run-id>
jarvis trace <run-id> --layer llm
jarvis trace --follow
jarvis run --goal "分析这个项目的入口和主要调用链"
jarvis chat
```

## 七、TUI 操作

| 操作 | 快捷键 |
|---|---|
| 提交问题 | `Enter` |
| 输入多行内容 | `Shift+Enter` |
| 粘贴 | `Ctrl+V` |
| 切换 Ask/Auto 权限模式 | `Alt+Tab` 或 `Shift+Tab` |
| 复制最近一条回答 | `Ctrl+Shift+C` 或 `Alt+C` |
| 鼠标复制 | 长按拖动选择后使用终端复制 |
| 退出 TUI | `Ctrl+Q` |
| 选择历史会话 | 启动时使用 `jarvis --resume`，再按 `↑` / `↓`、`Enter` |
| 退出历史选择器 | `Esc` |
| 手动压缩当前会话 | 输入 `/compact` |

`Ask` 模式下，高风险工具需要用户确认。`Auto` 模式会直接放行工具调用，
只应在可信代码仓库中使用。

## 八、Context、Skills 和 Agent Profiles

### Global Context

文件：

```text
C:\Users\<用户名>\.jarvis\context.md
```

适合保存所有项目都应遵守的规则，例如：

```markdown
# User Preferences

- 当前系统是 Windows，Shell 命令使用 PowerShell。
- 修改代码后运行相关测试。
- 不覆盖用户已有的未提交改动。
```

### Project Context

文件：

```text
<workspace>\.jarvis\context.md
```

适合保存当前项目的技术栈、启动方式、测试命令和目录约定。TUI 中执行 `/init`
可以让 Jarvis 分析当前仓库并生成初始项目说明。

### Skills 搜索顺序

同名 Skill 按以下优先级解析：

```text
项目 Skill
  -> 用户全局 Skill
  -> Jarvis 内建 Skill
```

对应目录：

```text
<workspace>\.jarvis\skills\
C:\Users\<用户名>\.jarvis\skills\
Jarvis 安装包中的 src\jarvis\core\skills\builtin\
```

支持两种文件结构：

```text
.jarvis\skills\review.md
.jarvis\skills\review\SKILL.md
```

最小 Skill 示例：

```markdown
---
name: explain
description: 解释指定代码，并给出调用链和风险点
allowed_tools:
  - project_map
  - search_files
  - read_file
---

请分析 $ARGUMENTS。

先定位入口和调用方，再解释实现，最后列出风险和测试建议。
```

保存为：

```text
<workspace>\.jarvis\skills\explain.md
```

在 TUI 中输入：

```text
/explain src/jarvis/core/loop.py
```

### Agent Profiles

搜索顺序：

```text
<workspace>\.jarvis\agents\
  -> C:\Users\<用户名>\.jarvis\agents\
  -> Jarvis 内建 agents
```

项目内建了 `planner`、`executor` 和 `reviewer` 三种角色。

## 九、MCP 配置

在全局或项目 `config.toml` 中添加：

```toml
[[mcp.servers]]
name = "example"
transport = "stdio"
command = "python"
args = ["D:/tools/example_mcp_server.py"]

[mcp.servers.env]
EXAMPLE_TOKEN = "replace-me"
```

TCP MCP 示例：

```toml
[[mcp.servers]]
name = "remote-example"
transport = "tcp"
host = "127.0.0.1"
port = 3000
```

敏感 Token 更推荐通过系统环境变量或项目 `.env` 传递。

## 十、日志和运行数据

全局 Core 日志：

```powershell
Get-Content "$HOME\.jarvis\logs\core.log" -Wait
```

当前项目启动失败日志：

```powershell
Get-Content ".jarvis\runtime\core.stderr.log" -Tail 100
```

当前项目会话：

```text
<workspace>\.jarvis\sessions\<session-id>\
├─ meta.json
├─ thread.jsonl
├─ notes.md
└─ runs\<run-id>\events.jsonl
```

| 文件 | 用途 |
|---|---|
| `meta.json` | 会话标题、项目目录、更新时间和运行状态 |
| `thread.jsonl` | 已保存的对话及工具结果，供恢复上下文使用 |
| `notes.md` | Agent 保存的会话笔记，下一轮执行时加载 |
| `runs/<run-id>/events.jsonl` | 每次运行的事件记录，供回放和排查使用 |

Daemon Trace：

```text
C:\Users\<用户名>\.jarvis\traces\daemon.jsonl
```

如果不希望 Trace 保存完整 LLM 请求和响应：

```text
JARVIS_TRACE_INCLUDE_LLM_PAYLOAD=false
```

## 十一、常见问题

### `jarvis` 不是内部或外部命令

执行：

```powershell
uv tool update-shell
uv tool dir --bin
```

重新打开 PowerShell，再检查：

```powershell
Get-Command jarvis
```

### `DEEPSEEK_API_KEY not set`

检查全局文件：

```powershell
Get-Content "$HOME\.jarvis\.env"
```

至少需要：

```text
JARVIS_LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
JARVIS_LLM_DEFAULT_MODEL=deepseek-flash
```

修改配置后，停止并重新启动当前项目 Core：

```powershell
jarvis core stop
jarvis
```

### Core 启动失败

```powershell
Get-Content ".jarvis\runtime\core.stderr.log" -Tail 100
jarvis core status
```

删除失效运行信息前，先确认没有仍在执行的任务。通常执行以下命令即可正常清理：

```powershell
jarvis core stop
jarvis
```

### 默认端口 7437 被占用

无需手工修改。`jarvis` 会为当前 workspace 自动选择空闲端口，并写入：

```text
.jarvis\runtime\core.json
```

### 恢复列表为空，或找不到之前的会话

先回到原项目的启动目录执行 `jarvis --resume`，检查该目录是否仍有
`.jarvis\sessions\`。会话按项目隔离，普通 `jarvis` 每次会新建会话，
`jarvis run` 的一次性任务也不会出现在可恢复的聊天列表中。

### 更新后恢复失败，或提示不支持 `session.list` / `session.resume`

在原项目目录停止旧 Core，再重新进入历史列表：

```powershell
jarvis core stop
jarvis --resume
```

如果命令本身不识别 `--resume`，先按上文更新全局安装，并用 `Get-Command jarvis`
确认当前命令路径。指定 ID 不存在或历史损坏时会显示错误，详见
[会话恢复说明](docs/session-recovery.md)。

### 切换目录后还是分析旧项目

每个 workspace 都有自己的 `.jarvis\runtime\core.json`。先在旧项目执行：

```powershell
jarvis core stop
```

然后进入新项目：

```powershell
cd D:\work\new-project
jarvis
```

### 卸载

```powershell
uv tool uninstall jarvis
```

卸载命令不会删除各项目中的 `.jarvis` 会话数据，也不会删除
`$HOME\.jarvis` 下的全局配置。

## 十二、开发和验证

```powershell
git clone https://github.com/Misty-finding/JarvisAgent.git
cd JarvisAgent
uv sync --python 3.12

uv run ruff check src tests scripts
uv run mypy src
uv run pytest tests -q
uv run python scripts/gen_protocol_doc.py --check
```

只运行单元测试：

```powershell
uv run pytest tests/unit -q
```

## 十三、文档与学习资料

| 内容 | 入口 |
|---|---|
| 会话恢复、持久化边界和手动验证 | [会话恢复说明](docs/session-recovery.md) |
| RPC 命令与事件定义 | [通信协议](WIRE_PROTOCOL.md) |
| 启动、配置与运行排查 | [运行手册](RUNBOOK.md) |
| 源码阅读与学习资料总览 | [学习资料索引](studydocs/README.md) |
| 七天源码阅读路线 | [源码阅读与面试指南](studydocs/CODE_READING_INTERVIEW_GUIDE.md) |
| Skills 配置与调用 | [Skills 使用指南](studydocs/SKILLS_GUIDE.md) |
| 项目面试复习与详细追问 | [逐词追问与答案手册](studydocs/JARVIS_逐词追问与答案手册.md) |

## License

本项目使用仓库中的 [LICENSE](LICENSE)。

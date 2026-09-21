# Jarvis 项目学习总档案

> 用途：这是后续阅读、提问与复盘时的共同索引。它记录项目的设计意图、阶段演进、术语、运行方式和阅读路径；不是替代源码的 API 文档。
> 当前本地快照：`stage/s2`（提交 `1d4d6ba`）。不同阶段的文件和功能不同，提问时请优先说明你正在看的分支。

## 1. 这是什么项目

Jarvis 是一个**本地运行的 AI Agent 系统**，而不只是“一次调用大模型 API 的脚本”。最终形态包含：

- 常驻后台服务 `jarvis-core`（Core daemon）：真正执行任务的唯一主体；
- 命令行客户端 `jarvis`（CLI）：发送命令、展示结果；
- 终端图形界面 `jarvis-tui`（TUI，S2 起）：订阅并展示同一份执行事件；
- Core 与客户端之间的类型化 IPC 协议；
- 大模型推理循环、工具调用、执行事件与可回放的运行记录；
- 后续阶段加入的任务规划、会话记忆、权限审批、上下文压缩、Skills、Subagents、MCP。

项目的核心工程观点是：把 Agent 当作一个**有协议、有状态、有事件、有权限、有上下文治理的系统**，不是一段 Prompt。

## 2. 总体结构与主数据流

```text
用户
 │
 ├─ jarvis <命令>  ─┐
 └─ jarvis-tui     ─┼─ TCP loopback（127.0.0.1:7437）
                  │  JSON-RPC 2.0 + NDJSON
                  ▼
             jarvis-core（常驻 daemon）
                  │
                  ├─ 命令处理 / AgentRunner
                  ├─ AgentLoop → LLM Provider
                  ├─ ToolRegistry → 内置工具 / MCP 工具
                  ├─ EventBus → events.jsonl / IPC 广播 / TUI
                  └─ session、权限、记忆、上下文压缩（后续阶段）
```

### 为什么一开始就分成两个进程？

- **执行边界清楚**：Core 是唯一执行体；CLI/TUI 都只是前端。
- **协议尽早固定**：所有跨进程数据必须可序列化，并具有统一的请求、响应和错误格式。
- **可扩展**：后续的事件订阅、TUI、权限审批与回放，都复用同一条 IPC 通道。
- **避免返工**：不先写单进程脚本、再痛苦地迁移成 daemon + client。

## 3. 必会术语（白话版）

| 术语 | 白话解释 |
|---|---|
| daemon（守护进程） | 长期运行在后台的服务；本项目里是 `jarvis-core`。 |
| CLI | 终端命令行界面，例如 `jarvis ping`。 |
| TUI | 在终端中显示交互界面的程序；不是网页 GUI。 |
| IPC | 进程间通信：不同程序如何互相传数据。 |
| TCP loopback | 只在本机 `127.0.0.1` 上建立网络连接，默认不对外网开放。 |
| JSON-RPC 2.0 | 约定“请求有 `method`/`params`/`id`，响应有 `result` 或 `error`”的 JSON 通信格式。 |
| NDJSON | 每一行都是一个独立 JSON；适合流式读取多条消息。 |
| Pydantic | Python 的数据模型/校验库；协议字段不合规时可以及时报错。 |
| discriminated union | 通过字段（此处是 `type`）区分多种消息模型，例如 `core.ping`、`agent.run`。 |
| EventBus | 事件总线：核心只“发布事件”，日志、文件、TUI 等各自订阅，不彼此耦合。 |
| run | 一次 Agent 任务执行的完整过程。 |
| session | 多次 run 所归属的一段持续会话（S4）。 |
| tool | 模型可以请求的外部动作，如读文件、执行命令；模型不能绕过安全策略直接执行。 |
| context | 送给 LLM 的上下文；长了要做预算、截断或压缩（S6）。 |
| editable 安装 | `uv sync` 让虚拟环境直接指向当前仓库源码；改源码后通常不用重装。 |

## 4. 项目阶段地图：S0 到 S7

每个 `stage/sN` 分支都是一个可独立阅读、运行的里程碑。学习时应从小到大切换，而不要从最终分支直接硬读。

| 阶段 | 主题 | 本阶段解决的问题 | 推荐验收 |
|---|---|---|---|
| S0 | 骨架与协议契约 | CLI 与 daemon 能否通过真实 IPC 完成 ping/pong？ | 启动 Core 后，`jarvis ping` 成功；能画出请求/响应路径。 |
| S1 | Agent 最小闭环 | 一个 goal 如何经过 LLM、工具、事件文件跑完整？ | 跑一次 `jarvis run`，找到 run 的事件记录。 |
| S2 | 事件流外化 | AgentRunner 放入 daemon 后，CLI/TUI 如何订阅同一事件流？ | 用 CLI/TUI 观察同一次执行。 |
| S3 | 自主规划与 TUI | Agent 如何拆解任务、使用内置工具，TUI 如何完整展示？ | 增加或跟踪一个简单工具调用。 |
| S4 | 会话与记忆 | 多个 run 如何延续同一会话？thread、notes 分别做什么？ | 同一 session 的后续对话能承接背景。 |
| S5 | 工具安全 | 调用前如何校验参数、审批权限、分类失败并重试？ | 描述“请求 → 审批 → 执行/拒绝 → 结果回传”的时序。 |
| S6 | 上下文治理 | 长会话怎样控制 token 预算、截断工具结果、压缩历史？ | 解释原始历史为何不能被简单覆盖。 |
| S7 | 扩展边界 | Skills、Subagents、MCP 怎样接到统一的执行链？ | 写一个 Skill 或接入一个外部工具。 |

## 5. 当前快照：S2 已经有什么

当前分支是 `stage/s2`，所以能看到 S0、S1、S2 的累积能力：

- 双进程 TCP 通信：Core 监听端口，CLI 作为客户端请求；
- `ping`、`run`、事件订阅等命令/事件的协议模型；
- LLM Provider 抽象与 AgentLoop；
- 工具注册、参数调用的基础能力；
- EventBus、事件文件写入、IPC 广播；
- CLI 命令与 Textual TUI；
- 集成测试验证真实双进程运行。

注意：仓库中的 `WIRE_PROTOCOL.md` 会随阶段扩展；它描述的是**当前分支**可用的协议，不应把 S2 的能力误认为 S0 也有。

## 6. 目录导览（当前 S2）

```text
src/jarvis/
├─ cli/                 # `jarvis` 命令行客户端
│  ├─ main.py           # 解析参数、读取配置、分发命令
│  └─ commands/         # ping、run、version、core 等具体命令
├─ core/                # 真正的后台执行系统
│  ├─ app.py            # `jarvis-core` 入口：启动 server、注册 handler、优雅关闭
│  ├─ config.py         # 配置读取与优先级
│  ├─ bus/              # 协议模型：命令、事件、JSON-RPC 信封
│  ├─ transport/        # TCP server/client 与 IPC 广播
│  ├─ llm/              # Provider 抽象、Anthropic 实现和消息类型
│  ├─ loop.py           # Agent 的推理/工具调用循环
│  ├─ runner.py         # 一次 run 的编排
│  ├─ tools/            # 工具基类、注册表、调用器、内置 read_file
│  ├─ events/           # EventBus 和 events.jsonl 写入
│  ├─ context.py        # system prompt 与模型上下文组装
│  ├─ runs.py           # run ID 与运行目录相关逻辑
│  └─ logging_setup.py  # 日志配置
├─ tui/                 # `jarvis-tui` 的 Textual 界面
└─ __init__.py

tests/
├─ unit/                # 快速、隔离的单元测试；适合与源码对照阅读
├─ integration/         # 真实进程/网络联调测试
└─ conftest.py          # 共享 fixture，例如随机端口和临时 daemon
```

## 7. 最重要的执行路径

### 7.1 S0：`jarvis ping`

```text
终端输入 `jarvis ping`
  → cli/main.py 解析出 command = "ping"
  → cli/commands/ping.py 通过 SocketClient 发送 JSON-RPC 请求
  → transport/socket_server.py 收到一行 NDJSON，按 method 分发
  → core/app.py 注册的 ping handler 返回 PongResult
  → server 发送 JSON-RPC success 响应
  → CLI 显示 pong
```

阅读顺序：`cli/main.py` → `cli/commands/ping.py` → `transport/socket_client.py` → `bus/envelope.py` / `bus/commands.py` → `transport/socket_server.py` → `core/app.py` → 对应测试。

### 7.2 S1/S2：`jarvis run <goal>`

```text
CLI/TUI 提交 goal
  → Core 创建 run
  → AgentRunner 组织运行资源
  → AgentLoop 组装 context，向 LLM 发请求
  → LLM 可能直接回答，也可能请求 tool
  → Invocation 执行工具，并把结果作为 tool result 返给 LLM
  → EventBus 依次发布 run / step / token / tool 等事件
  → EventWriter 写入 events.jsonl；IPCBroadcaster 推给订阅 CLI/TUI
  → run 完成并发布 run.finished
```

关键设计：`AgentLoop` 不应该自己打印终端或直接绑定 TUI；它只发事件。输出、文件持久化和可视化都靠订阅者完成。

## 8. 配置、环境与常用命令

### 环境

- Python：项目要求 3.12.x；
- 依赖与虚拟环境：`uv` 管理；
- 包使用 `src/` 布局，`uv sync` 会以 editable 模式安装当前项目；
- LLM 测试或真实调用可能需要在 `.env` 中配置 API Key，绝不提交 `.env`。

### 配置优先级

从低到高为：内置默认值 → `~/.jarvis/config.toml` → `~/.jarvis/.env`
→ 项目 `.jarvis/config.toml` → 项目 `.env` → 系统环境变量。

常见变量：`JARVIS_HOST`、`JARVIS_PORT`、`JARVIS_LOG_LEVEL`、`JARVIS_LOG_FILE`、
`JARVIS_CONFIG`、`JARVIS_WORKSPACE_ROOT`。

### 命令速查

```powershell
# 开发环境安装
uv sync

# 一次安装全局命令
uv tool install --editable .

# 在任意代码仓库直接启动 Core 和 TUI
cd D:\path\to\project
jarvis

# 查看当前项目 daemon
jarvis core status
jarvis ping

# 运行全部测试 / 单元测试
uv run pytest tests/ -v
uv run pytest tests/unit -v

# 检查代码风格与类型
uv run ruff check src tests scripts
uv run mypy src

# 协议模型改动后，重新生成协议文档
uv run python scripts/gen_protocol_doc.py
```

## 9. 协议与错误处理

传输层是 TCP loopback，消息是“每行一个 JSON”。命令使用 JSON-RPC 2.0：

```json
{
  "jsonrpc": "2.0",
  "id": "u-1",
  "method": "core.ping",
  "params": {"type": "core.ping", "client": "cli/0.0.1"}
}
```

成功时返回 `result`，失败时返回 `error`。常见错误码：

| 代码 | 含义 |
|---:|---|
| -32700 | 收到的 JSON 无法解析 |
| -32600 | JSON-RPC 请求格式缺字段或非法 |
| -32601 | 方法不存在 |
| -32602 | 参数无法通过模型校验 |
| -32603 | handler 出现未处理异常 |
| -32000 | 应用级错误，例如已有任务正在执行 |

`WIRE_PROTOCOL.md` 是从 `core/bus/` 模型自动生成的，**不能手改**。修改命令或事件模型后，应运行生成脚本并提交生成结果。

## 10. 后续阶段的关键边界

### 工具边界

模型提出的是“工具请求”，不是直接执行权。完整链路应包含：工具存在性检查 → 参数校验 → 权限判断/审批 → 执行 → 错误分类 → tool result 回到 LLM。失败结果也是模型继续推理的输入。

### 记忆边界（S4 起）

- `thread`：完整的消息对话历史；
- `notes`：Agent 主动保存的事实；
- `context.md`：用户或项目层面的固定背景。

它们都可能进入上下文，但职责不同；混在一起会破坏恢复、审计与压缩。

### 上下文边界（S6 起）

上下文不能无限增长。系统会管理水位、限制工具结果，并在必要时 compact（压缩）。压缩不是把原始历史简单删掉：原始记录仍应保留，以便审计与回放。

### 扩展边界（S7）

Skills、Subagents、MCP 不应该各自发明另一套运行机制；它们最终接入相同的 `AgentRunner`、`ToolRegistry` 和 `EventBus` 执行链。

## 11. 推荐学习节奏与阶段产出

学习原则：每个阶段都做“运行 → 追一条真实路径 → 读一个测试 → 产出自己的解释”，不要按文件树机械阅读。

| 阶段 | 建议先读 | 对照测试 | 你的学习产出 |
|---|---|---|---|
| S0 | `cli/main.py`、ping、bus、transport、app | `test_envelope.py`、`test_socket_server.py`、`test_ping_roundtrip.py` | 手画 ping 的时序图。 |
| S1 | `llm/`、`loop.py`、`runner.py`、`events/` | `test_loop.py`、`test_runner.py`、`test_event_*` | 解释一次 run 的事件顺序。 |
| S2 | `tui/app.py`、`ipc_broadcaster.py` | `test_tui_app.py`、`test_s2_dual_process.py` | 说明为什么 CLI/TUI 可订阅同一事件流。 |
| S3 | `tools/`、`context.py`、task 相关模块 | 工具注册/调用/内置工具测试 | 新增一个最简单的 `echo` 工具。 |
| S4 | `session/` | session 相关集成测试 | 解释 thread / notes / context.md 区别。 |
| S5 | `permissions/` 与 invocation 链 | permission flow 测试 | 写出审批时序。 |
| S6 | `compact/`、context 预算 | budget / compactor 测试 | 说明 compact 的输入、输出与原始记录去向。 |
| S7 | `subagent/`、`skills/`、`mcp/` | 对应 loader/tool 测试 | 创建一个 Skill 并运行验证。 |

## 12. Git 分支与当前学习状态

已配置远程：

```text
origin   git@github.com:Misty-finding/Jarvis.git
upstream https://github.com/youngyangyang04/Jarvis.git
```

可用阶段分支：`stage/s0` 到 `stage/s7`。查看某一阶段：

```powershell
git switch stage/s0
git switch stage/s1
```

切换前如果有未提交改动，优先使用独立工作树，以免改动被阻挡或需要暂存：

```powershell
git worktree add ..\Jarvis-s0 stage/s0
```

本档案创建时，主工作目录已位于 `stage/s2`；工作区中的 `tmp/` 是未跟踪目录，不是项目 S2 的正式源码。

## 13. 后续问我时可直接这样问

- “我在 S0 的 `socket_server.py` 第 X 行不懂，按 `jarvis ping` 解释。”
- “S1 的 AgentLoop 为什么要循环，不是请求一次模型就行吗？”
- “`EventBus` 和日志文件分别是什么关系？”
- “我切到 S3 后，多出来的模块是为了解决什么旧问题？”
- “带我完成 S0 的验收，不要直接给答案。”

我会以这份档案的阶段地图为上下文；涉及当前代码时，再以你所在分支的源码为准并更新本档案。

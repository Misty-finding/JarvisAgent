# Jarvis 源码阅读与面试准备指南

> 校对基准：2026-09-20，`main` 分支。
> 目标：不是机械地把每个文件看一遍，而是能讲清一次请求如何穿过系统、核心设计为何这样做、当前实现还有哪些边界。

## 简历驱动的阅读目标

这份指南以简历中的八个主题作为最终验收标准：

```text
Tool -> Context -> Memory -> Loop -> Safety -> Trace -> Eval -> Multi-Agent
```

调用链仍然是第一遍阅读顺序，但每读完一个模块，都要回到下面这张表，确认简历表述有源码和测试支撑。

| 简历主题 | 当前实现能够支持的表述 | 主要源码证据 | 主要测试证据 |
|---|---|---|---|
| Tool | 统一注册和调度内置工具、任务工具、Skills、Subagent 与 MCP 工具；执行前完成参数校验，执行后将结构化结果回填模型 | `core/tools/base.py`、`registry.py`、`invocation.py`、`runner.py`、`mcp/tool.py` | `test_tool_registry.py`、`test_tool_params.py`、`test_invocation.py`、`test_mcp_tool.py` |
| Context | 每次 Run 组装系统规则、全局/项目说明、角色指令、Session Notes、消息历史和工具结果；通过截断与摘要压缩控制规模 | `core/context.py`、`prompt.py`、`runner.py`、`compact/` | `test_context.py`、`test_context_system_prompt.py`、`test_prompt.py`、`test_budget.py`、`test_compactor.py` |
| Memory | Run 内以内存维护执行状态；Session 层持久化 thread 与 notes；Global/Project 层通过用户维护的 `context.md` 注入跨 Run 规则 | `core/context.py`、`session/store.py`、`memory/loader.py`、`tools/builtin/note_save.py` | `test_session_store.py`、`test_memory_loader.py`、`test_note_save_tool.py` |
| Loop | 实现 LLM 决策、工具执行、结果观察和再次决策的 ReAct 闭环，并处理流式输出、工具失败、步数上限和最终总结轮 | `core/loop.py`、`llm/provider.py`、`tools/invocation.py` | `test_loop.py`、`test_llm_provider.py`、`test_tool_retry.py` |
| Safety | 通过参数校验、权限策略、目录越界检测、人工审批、审批缓存、超时和错误分类约束工具调用 | `core/permissions/`、`core/tools/invocation.py` | `test_permission_policy.py`、`test_permission_manager.py`、`test_tool_retry.py`、`test_s5_permission_flow.py` |
| Trace | 统一记录 Run/Step、LLM、Tool、Permission、Token 与 IPC 事件，支持 `events.jsonl` 回放和 Trace 故障定位 | `core/bus/events.py`、`events/`、`trace/`、`transport/ipc_broadcaster.py` | `test_event_bus.py`、`test_event_writer.py`、`test_trace_writer.py`、`test_tracing_provider.py` |
| Eval | 在可复现分支中基于 BFCL v4 官方数据和 Checker 评测工具调用，并保留参数规范化审计信息 | `codex/bfcl-reproducible-eval` 分支的 `src/jarvis/evals/`、`argument_normalizer.py`、`repro/bfcl/` | 该分支的 `tests/evals/` 与 `test_tool_argument_normalizer.py` |
| Multi-Agent | 支持隔离上下文的子 Agent、前台阻塞/后台并行、角色 Profile、事件桥接、后台任务注册及按 run_id 查询结果 | `core/agents/`、`core/subagent/registry.py`、`core/subagent/tool.py` | `test_agent_profile_loader.py`、`test_spawn_agent_tool.py` |

表中的类名和函数名用于建立“简历表述 -> 源码证据”的映射，不建议直接堆进简历。简历写通用机制，面试追问时再落到具体类和函数。

### 简历表述红线

面试前必须记住这些实现边界：

1. **Context 和 Memory 不是一回事。** Memory 决定信息存在哪里、保留多久；Context 决定当前请求向模型注入哪些信息。
2. **项目上下文不是 Session Memory 自动保存的。** `.jarvis/context.md` 是用户维护的项目说明文件，每次 Run 读取后注入。
3. **当前没有自动学习用户偏好的长期记忆。** `~/.jarvis/context.md` 可以承载跨 Session 规则，但需要用户手动维护。
4. **落盘不等于完整会话恢复。** `meta.json`、`thread.jsonl` 和 `notes.md` 会保留，但 daemon 重启后尚未自动重建 SessionManager 内存索引。
5. **当前 Multi-Agent 没有 join barrier。** 也没有 Session 级子 Agent 状态机、并发上限、超时取消或基于 `asyncio.Event` 的完成驱动自动投递。
6. **BFCL 结果来自独立可复现分支。** 面试时要能指出分支、报告和运行命令，不能暗示所有评测代码都已经合并到 main。

## 最先看这里：严格按什么顺序读

第一次阅读不要从目录树逐个文件扫，也不要从 1263 行的 `tui/app.py` 第一行开始读。

先完成下面的 **20 个必读步骤**。这就是主阅读顺序，后文只是对每一步的展开说明。

### 第一组：先认识通信协议

| 顺序 | 文件 | 第一遍看什么 | 读完应知道什么 |
|---:|---|---|---|
| 1 | `src/jarvis/core/bus/envelope.py` | `JsonRpcRequest`、`JsonRpcSuccess`、`EventPushEnvelope` | 请求、响应、事件推送的外层格式 |
| 2 | `src/jarvis/core/bus/commands.py` | 所有 Command/Result，重点是 session、permission、subscribe | 前端能向 Core 发哪些命令 |
| 3 | `src/jarvis/core/bus/events.py` | 先看 session、run、step、llm、tool、permission 事件 | Core 会向前端推送哪些执行状态 |

这三个文件是整个项目的“共同语言”。没有读完它们，不要急着读 AgentLoop。

### 第二组：顺着一条 TUI 消息进入 Core

| 顺序 | 文件或位置 | 第一遍看什么 | 读完应知道什么 |
|---:|---|---|---|
| 4 | `src/jarvis/tui/__main__.py` | `main()` | TUI 如何读取配置并启动 |
| 5 | `src/jarvis/tui/app.py` | 只搜索 `_socket_loop()`、`on_chat_text_area_submitted()`、`_do_send_message()`、`_handle_event_inner()` | TUI 怎样发消息、订阅事件和渲染结果 |
| 6 | `src/jarvis/core/transport/socket_client.py` | `send_command()`、`run_event_loop()`、`_dispatch()` | 同一 TCP 连接怎样同时处理 RPC 响应和事件 |
| 7 | `src/jarvis/core/transport/socket_server.py` | `register()`、`_read_loop()`、`_handle_line()` | Core 怎样解析请求并路由到 handler |
| 8 | `src/jarvis/core/app.py` | 先看各 `_xxx_handler()`，再看 `run()` | RPC handler 怎样进入业务层，以及全局组件怎样装配 |

读到这里，先画出这一小段：

```text
TUI
  -> SocketClient.send_command("session.send_message")
  -> SocketServer._handle_line()
  -> CoreApp._session_send_handler()
```

### 第三组：进入一次 Agent run

| 顺序 | 文件 | 第一遍看什么 | 读完应知道什么 |
|---:|---|---|---|
| 9 | `src/jarvis/core/session/model.py` | `Session`、`SessionMode`、`SessionStatus` | session 的最小状态模型 |
| 10 | `src/jarvis/core/session/store.py` | `append_message()`、`read_messages()`、`append_messages()` | 对话历史如何落盘，并被同一 Session 的后续 Run 加载 |
| 11 | `src/jarvis/core/session/manager.py` | `create()`、`send_message()` | 一条用户消息怎样创建 run 并调用 Runner |
| 12 | `src/jarvis/core/runner.py` | `_build_registry()`、`run_and_capture()` | 一次 run 怎样组装 Provider、工具、权限、上下文和事件记录 |
| 13 | `src/jarvis/core/context.py` | 整个文件 | 模型消息、tool_use、tool_result 和运行状态怎样保存 |
| 14 | `src/jarvis/core/prompt.py` | `build_runtime_system_prompt()`、`finalization_system_prompt()` | Windows 环境、工具策略和步数限制怎样告诉模型 |
| 15 | `src/jarvis/core/loop.py` | `AgentLoop.run()`，逐行读 | LLM -> tool -> result -> 下一轮的核心循环 |
| 16 | `src/jarvis/core/llm/base.py`、`types.py`、`provider.py` | Protocol、响应类型、`chat()`、`create_llm_provider()` | Anthropic/DeepSeek 如何转换成统一 LLM 响应 |

读完第三组，必须能脱离代码讲出：

```text
SessionManager.send_message()
  -> AgentRunner.run_and_capture()
  -> ExecutionContext
  -> AgentLoop.run()
  -> LLMProvider.chat()
  -> 模型返回 text 或 tool_use
```

### 第四组：补齐工具执行和结果回传

| 顺序 | 文件 | 第一遍看什么 | 读完应知道什么 |
|---:|---|---|---|
| 17 | `src/jarvis/core/tools/base.py`、`registry.py`、`invocation.py` | `BaseTool`、`ToolRegistry`、`invoke_tool()` | 工具如何查找、校验、审批、重试并返回模型 |
| 18 | `src/jarvis/core/permissions/policy.py`、`manager.py` | `evaluate()`、`check_and_wait()`、`respond()`、`set_session_mode()` | Ask/Auto 和权限 Future 怎样工作 |
| 19 | `src/jarvis/core/events/bus.py`、`writer.py` | `publish()`、`EventWriter.handle()` | 运行事件如何分发并写入 events.jsonl |
| 20 | `src/jarvis/core/transport/ipc_broadcaster.py` | `subscribe()`、`handle()` | 事件怎样从 Core 推回客户端 |

完整主链最终应画成：

```text
TUI 输入
  -> SocketClient
  -> SocketServer
  -> CoreApp handler
  -> SessionManager
  -> AgentRunner
  -> ExecutionContext
  -> AgentLoop
  -> LLM Provider
  -> ToolRegistry / invoke_tool / PermissionManager
  -> EventBus
  -> EventWriter + IpcEventBroadcaster
  -> SocketClient._dispatch()
  -> TUI._handle_event_inner()
```

完成这 20 步，就已经覆盖了面试所需的项目主干。下面模块放到第二轮：

1. 上下文压缩：`core/compact/budget.py`、`core/compact/compactor.py`
2. Skills：`core/skills/loader.py`
3. Subagents：`core/agents/loader.py`、`core/subagent/registry.py`、`core/subagent/tool.py`
4. MCP：`core/mcp/client.py`、`core/mcp/tool.py`、`core/mcp/server.py`
5. 配置与可观测性：`core/config.py`、`core/trace/`、`core/logging_setup.py`
6. 最后才完整阅读 `tui/app.py` 的组件、样式、粘贴和复制逻辑

如果时间非常少，只读这 8 个位置：

```text
bus/commands.py + bus/events.py
-> transport/socket_client.py
-> core/app.py 的 handlers
-> session/manager.py 的 send_message()
-> runner.py 的 run_and_capture()
-> context.py
-> loop.py 的 AgentLoop.run()
-> tools/invocation.py 的 invoke_tool()
```

## 一、原推荐顺序有哪些问题

原顺序的大方向没有错，但不适合直接拿来准备面试：

1. **入口写得过于偏向 CLI。** 目前日常主入口是 TUI，`cli/main.py` 只负责 CLI 参数分发，并不会负责所有用户请求。
2. **协议模型出现得太晚。** `bus/commands.py`、`bus/events.py`、`bus/envelope.py` 定义了模块之间共同使用的语言，应该在传输层之前读。
3. **EventBus 放得太早。** 它只有很薄的一层发布/订阅逻辑；不知道谁发布什么事件时，先读它意义不大。
4. **SessionManager 和 AgentRunner 的职责混在了一起。** SessionManager 管会话状态、历史和 Skill 展开；AgentRunner 才负责组装 Provider、工具、权限、MCP、Subagent 和 AgentLoop。
5. **“Plan -> Act -> Observe”描述不够精确。** 当前代码实际是：调用 LLM -> 保存 assistant blocks -> 执行 tool calls -> 保存 tool results -> 判断结束或继续。
6. **LLM 层描述已经过时。** 当前同时支持 Anthropic 和 DeepSeek；DeepSeek 走 Anthropic-compatible endpoint。
7. **事件回传链写得不准确。** 实际是 `EventBus -> IpcEventBroadcaster -> StreamWriter -> SocketClient -> TUI`，不是事件重新进入 SocketServer 的请求路由。
8. **扩展模块的优先级过高。** MCP、Subagent、Skills 应在主循环和统一工具调用链之后阅读，否则容易只记住功能，讲不清抽象。

## 二、先记住六个核心对象

阅读前先建立这张最小心智地图：

| 对象 | 职责 | 不负责什么 |
|---|---|---|
| `CoreApp` | daemon 启动、全局组件装配、RPC handler 注册 | 不执行 Agent 循环 |
| `SessionManager` | session 状态、消息持久化、Skill 解析、触发 run | 不组装工具和 Provider |
| `AgentRunner` | 为一次 run 组装上下文、Provider、工具、事件记录 | 不决定每一步怎样循环 |
| `AgentLoop` | LLM/tool 循环、步数控制、结束条件、自动压缩触发 | 不关心 TUI 如何显示 |
| `ToolRegistry` | 统一保存内置工具、MCP 工具、Subagent 工具 | 不执行权限和重试 |
| `invoke_tool` | 查找、校验、审批、超时、重试、事件发布 | 不决定模型何时调用工具 |

一句话概括项目：

> Jarvis 是一个本地双进程、事件驱动、支持多轮会话和工具扩展的 Agent runtime；前端通过 JSON-RPC 发命令，通过事件订阅接收流式执行状态。

## 三、推荐阅读顺序

### 第 0 轮：先运行，不读实现

先观察一次真实请求：

```powershell
cd D:\path\to\project
jarvis
```

在 TUI 中提出一个会触发 `project_map`、`search_files` 或 `read_file` 的问题，然后观察：

- TUI 显示了哪些事件；
- 项目 `.jarvis/sessions/<session_id>/` 生成了哪些文件；
- `thread.jsonl` 和 `runs/<run_id>/events.jsonl` 分别保存什么；
- Ask/Auto 权限模式怎样影响工具调用。

本轮产出：手画一张不超过 10 个节点的请求时序图。

### 第 1 轮：先读协议，再读传输

#### 1. 协议契约

1. `src/jarvis/core/bus/envelope.py`
2. `src/jarvis/core/bus/commands.py`
3. `src/jarvis/core/bus/events.py`

重点回答：

- 命令、响应、事件有什么区别？
- 为什么命令使用 JSON-RPC，而流式状态使用 `EventPushEnvelope`？
- `session.send_message`、`permission.respond`、`event.subscribe` 分别解决什么问题？

配套测试：

- `tests/unit/test_envelope.py`
- `tests/unit/test_commands_events.py`

#### 2. TCP 客户端和服务端

1. `src/jarvis/core/transport/socket_client.py`
2. `src/jarvis/core/transport/socket_server.py`
3. `src/jarvis/core/transport/ipc_broadcaster.py`

重点看：

- `SocketClient.send_command()` 如何用 request id 匹配 Future；
- `SocketClient._dispatch()` 如何区分 RPC 响应和事件推送；
- `SocketServer._read_loop()` 为什么为每条命令创建独立 task；
- 权限审批为什么不会被一个长时间运行的 `session.send_message` 卡死；
- Broadcaster 如何按 topic 和 run scope 过滤事件。

配套测试：

- `tests/unit/test_socket_client.py`
- `tests/unit/test_socket_server.py`
- `tests/unit/test_ipc_broadcaster.py`
- `tests/integration/test_ping_roundtrip.py`

### 第 2 轮：顺着一次 TUI 对话走完整条主链

#### 3. TUI 只读关键方法

`src/jarvis/tui/app.py` 很长，不建议从第一行顺读到最后一行。第一遍只定位：

- `on_chat_text_area_submitted()`
- `_do_send_message()`
- `_socket_loop()`
- `_handle_event()` / `_handle_event_inner()`
- `action_cycle_permission_mode()`

入口再补：

- `src/jarvis/tui/__main__.py`

本步只理解：

```text
用户输入
  -> session.send_message
  -> 事件订阅持续接收 token/tool/permission/session 事件
  -> Textual widget 渲染
```

样式、补全控件、复制粘贴等 UI 细节放到最后。

#### 4. CoreApp：RPC 到领域层的接缝

阅读 `src/jarvis/core/app.py`，分两部分看：

1. `run()`：创建 EventBus、PermissionManager、MCP、SessionManager、SocketServer。
2. 各种 `_xxx_handler()`：校验命令并委托给 SessionManager 或 PermissionManager。

重点不是记代码，而是理解：

- `CoreApp` 是 composition root；
- handler 很薄，业务逻辑尽量下沉；
- `agent.run` 最终也被转换成 one-shot session，而不是维护另一套执行系统。

#### 5. Session 生命周期

阅读：

1. `src/jarvis/core/session/model.py`
2. `src/jarvis/core/session/store.py`
3. `src/jarvis/core/session/manager.py`

跟踪 `SessionManager.send_message()`：

```text
检查 session 和锁
  -> 追加用户消息
  -> 分配 run_id
  -> 可选解析 /skill
  -> 创建 AgentRunner
  -> run_and_capture()
  -> 更新 waiting_for_input / closed
```

重点区分：

- session：多轮对话生命周期；
- run：一条用户消息触发的一次 Agent 执行；
- thread：模型消息历史；
- events：执行过程的审计记录；
- notes：Agent 主动保存、仅供同一 Session 后续 Run 复用的关键事实和决策。

配套测试：

- `tests/unit/test_session_manager.py`
- `tests/unit/test_session_store.py`
- `tests/integration/test_s4_session_ipc.py`

### 第 3 轮：Agent 真正的核心

#### 6. AgentRunner：依赖装配

阅读 `src/jarvis/core/runner.py`。

重点看两个方法：

- `_build_registry()`：内置工具、任务工具、note、subagent、MCP 怎样汇入统一注册表；
- `run_and_capture()`：历史、notes、context、Provider、EventWriter、Compactor、AgentLoop 怎样组装。

面试时可把 Runner 描述为“一次 run 的依赖注入和生命周期边界”。

配套测试：`tests/unit/test_runner.py`

#### 7. 上下文和系统提示词

阅读：

1. `src/jarvis/core/context.py`
2. `src/jarvis/core/prompt.py`

重点看：

- Anthropic-style message blocks 如何保存 text、tool_use、tool_result；
- global/project/session context 的叠加顺序；
- Windows、PowerShell、大项目搜索策略怎样进入运行时系统提示词；
- 达到工具步数上限后为什么还有一次无工具 finalization turn。

配套测试：

- `tests/unit/test_context.py`
- `tests/unit/test_context_system_prompt.py`
- `tests/unit/test_prompt.py`

#### 8. AgentLoop：最重要的文件

阅读 `src/jarvis/core/loop.py`，建议逐行读。

真实循环：

```text
发布 step.started
  -> 组装 system prompt
  -> Provider.chat()
  -> 保存 assistant text/thinking/tool_use blocks
  -> invoke_tool()
  -> 保存 tool_result blocks
  -> 判断 end_turn / max_steps
  -> 可选触发 compact
  -> 发布 step.finished
```

必须能解释的边界：

- 工具失败为什么不直接结束 run；
- `end_turn`、`tool_use`、`max_tokens` 分别怎样处理；
- 为什么工具步数耗尽后要保留最后一次总结机会；
- `CancelledError` 为什么要单独处理并继续向上抛。

配套测试：`tests/unit/test_loop.py`

#### 9. LLM Provider

阅读：

1. `src/jarvis/core/llm/base.py`
2. `src/jarvis/core/llm/types.py`
3. `src/jarvis/core/llm/provider.py`

重点看：

- `LLMProvider` Protocol 怎样隔离 AgentLoop；
- Anthropic 流式 token、最终 message 和 usage 的关系；
- tool call 为什么从 final message 解析，而不是从 token 字符串解析；
- DeepSeek 为什么可以复用 AnthropicProvider 的大部分逻辑；
- 网络流中断的重试为什么只在第一次发布 token，避免 TUI 重复。

配套测试：`tests/unit/test_llm_provider.py`

### 第 4 轮：工具、安全和可扩展性

#### 10. 统一工具调用链

阅读：

1. `src/jarvis/core/tools/base.py`
2. `src/jarvis/core/tools/registry.py`
3. `src/jarvis/core/tools/invocation.py`
4. 任选两个内置工具，例如 `project_map.py`、`bash.py`

记住调用顺序：

```text
按名称找工具
  -> Pydantic 参数校验
  -> PermissionManager 审批
  -> timeout 包装
  -> 可重试错误指数退避
  -> 发布成功/失败事件
  -> ToolResult 回填上下文
```

配套测试：

- `tests/unit/test_tool_registry.py`
- `tests/unit/test_tool_params.py`
- `tests/unit/test_invocation.py`
- `tests/unit/test_tool_retry.py`
- `tests/unit/test_repository_tools.py`

#### 11. 权限系统

阅读：

1. `src/jarvis/core/permissions/policy.py`
2. `src/jarvis/core/permissions/manager.py`
3. `src/jarvis/core/permissions/storage.py`

重点看：

- 静态规则、session 缓存、持久化策略和人工审批的优先级；
- `Future` 怎样挂起工具调用并由另一个 RPC 请求恢复；
- Auto 模式为何能立刻释放当前 session 的 pending 请求；
- Auto 模式“放开所有权限”带来的安全取舍。

配套测试：

- `tests/unit/test_permission_policy.py`
- `tests/unit/test_permission_manager.py`
- `tests/integration/test_s5_permission_flow.py`

#### 12. 上下文压缩

阅读：

1. `src/jarvis/core/compact/budget.py`
2. `src/jarvis/core/compact/compactor.py`

重点区分：

- tool result 截断：局部、确定性、不调用 LLM；
- compaction：调用 LLM 生成摘要；
- 手动 `/compact` 与自动阈值触发；
- 原 thread 如何备份后再写入摘要。

配套测试：

- `tests/unit/test_budget.py`
- `tests/unit/test_compactor.py`

#### 13. Skills、Subagents、MCP

按这个顺序读：

1. `src/jarvis/core/skills/loader.py`
2. `src/jarvis/core/agents/loader.py`
3. `src/jarvis/core/subagent/registry.py`
4. `src/jarvis/core/subagent/tool.py`
5. `src/jarvis/core/mcp/client.py`
6. `src/jarvis/core/mcp/tool.py`
7. `src/jarvis/core/mcp/server.py`

理解三个不同概念：

- Skill：Prompt 模板和工具白名单；
- Subagent：复用同一个 AgentLoop 的隔离执行上下文；
- MCP：把外部服务工具适配成统一 BaseTool。

配套测试：

- `tests/unit/test_skill_loader.py`
- `tests/unit/test_agent_profile_loader.py`
- `tests/unit/test_spawn_agent_tool.py`
- `tests/unit/test_mcp_tool.py`

### 第 5 轮：事件、可观测性和 UI 细节

最后再读：

1. `src/jarvis/core/events/bus.py`
2. `src/jarvis/core/events/writer.py`
3. `src/jarvis/core/trace/`
4. `src/jarvis/core/logging_setup.py`
5. `src/jarvis/core/config.py`
6. `src/jarvis/tui/app.py` 的其余 UI 组件

此时你已经知道每种事件从哪里来，再看 EventBus、落盘和 TUI 渲染就会非常快。

## 四、七天面试准备计划

建议每天投入 4～6 小时。每个文件只围绕当天的问题阅读，不要求第一遍理解所有辅助函数。

### Day 1：协议与双进程通信

按顺序阅读：

1. `src/jarvis/core/bus/envelope.py`
2. `src/jarvis/core/bus/commands.py`
3. `src/jarvis/core/bus/events.py`
4. `src/jarvis/core/transport/socket_client.py`
5. `src/jarvis/core/transport/socket_server.py`
6. `src/jarvis/core/app.py`：只看 `_xxx_handler()` 和 RPC `register()`
7. `src/jarvis/tui/__main__.py`
8. `src/jarvis/tui/app.py`：只看 `_socket_loop()` 和 `_do_send_message()`

配套测试：

```powershell
uv run pytest tests/unit/test_envelope.py `
  tests/unit/test_commands_events.py `
  tests/unit/test_socket_client.py `
  tests/unit/test_socket_server.py `
  tests/integration/test_ping_roundtrip.py -q
```

当天产出：

- 画出命令请求、RPC 响应、事件推送三条路径；
- 能解释同一 TCP 连接如何同时承载 response 和 event；
- 能回答为什么长时间运行的 handler 不会阻塞 `permission.respond`。

### Day 2：Loop 主链与 Session 生命周期

按顺序阅读：

1. `src/jarvis/core/session/model.py`
2. `src/jarvis/core/session/store.py`
3. `src/jarvis/core/session/manager.py`
4. `src/jarvis/core/runner.py`
5. `src/jarvis/core/context.py`
6. `src/jarvis/core/prompt.py`
7. `src/jarvis/core/loop.py`

配套测试：

```powershell
uv run pytest tests/unit/test_session_manager.py `
  tests/unit/test_session_store.py `
  tests/unit/test_runner.py `
  tests/unit/test_context.py `
  tests/unit/test_context_system_prompt.py `
  tests/unit/test_loop.py -q
```

当天产出：

- 用自己的话区分 SessionManager、AgentRunner、ExecutionContext、AgentLoop；
- 不看代码讲出一条消息从 session 到最终回答的完整过程；
- 画出 LLM `tool_use` 到 `tool_result` 再到下一轮决策的消息结构。
- 用 60 秒解释简历中的 Loop：循环如何推进、怎样终止、工具失败后为什么还能继续。

### Day 3：Tool 与 Safety

按顺序阅读：

1. `src/jarvis/core/llm/base.py`
2. `src/jarvis/core/llm/types.py`
3. `src/jarvis/core/llm/provider.py`
4. `src/jarvis/core/tools/base.py`
5. `src/jarvis/core/tools/registry.py`
6. `src/jarvis/core/tools/invocation.py`
7. `src/jarvis/core/permissions/policy.py`
8. `src/jarvis/core/permissions/manager.py`
9. `src/jarvis/core/permissions/storage.py`
10. 任选两个内置工具：`project_map.py`、`bash.py`

配套测试：

```powershell
uv run pytest tests/unit/test_llm_provider.py `
  tests/unit/test_tool_registry.py `
  tests/unit/test_tool_params.py `
  tests/unit/test_invocation.py `
  tests/unit/test_tool_retry.py `
  tests/unit/test_permission_policy.py `
  tests/unit/test_permission_manager.py -q
```

当天产出：

- 写出工具查找、参数校验、权限审批、超时、重试、结果回填的顺序；
- 能解释哪些错误会重试、哪些不会；
- 能解释 Ask、Always、Auto 三类权限决策的生命周期和风险。
- 分别准备 Tool 和 Safety 两条简历 Bullet 的 60 秒回答，每条至少指出三个源码位置。

### Day 4：Context、Memory 与 Trace

按顺序阅读：

1. `src/jarvis/core/context.py`：复读运行时消息与状态
2. `src/jarvis/core/runner.py`：只看 history、notes、global/project context 的加载和注入
3. `src/jarvis/core/memory/loader.py`
4. `src/jarvis/core/session/store.py`：复读 notes、thread 和 compact 相关方法
5. `src/jarvis/core/tools/builtin/note_save.py`
6. `src/jarvis/core/compact/budget.py`
7. `src/jarvis/core/compact/compactor.py`
8. `src/jarvis/core/events/bus.py`
9. `src/jarvis/core/events/writer.py`
10. `src/jarvis/core/trace/record.py`
11. `src/jarvis/core/trace/writer.py`
12. `src/jarvis/core/trace/provider.py`
13. `src/jarvis/core/transport/ipc_broadcaster.py`

配套测试：

```powershell
uv run pytest tests/unit/test_memory_loader.py `
  tests/unit/test_session_store.py `
  tests/unit/test_note_save_tool.py `
  tests/unit/test_budget.py `
  tests/unit/test_compactor.py `
  tests/unit/test_event_bus.py `
  tests/unit/test_event_writer.py `
  tests/unit/test_trace_writer.py `
  tests/unit/test_tracing_provider.py `
  tests/unit/test_ipc_broadcaster.py -q
```

当天产出：

- 画出 Run、Session、Global/Project 三层记忆表，写清存储位置、生命周期、写入者和读取时机；
- 说明 working memory、thread、notes、global context、project context 的区别；
- 能明确回答“项目上下文是不是 Session Memory”和“daemon 重启后能否恢复原 Session”；
- 说明截断和 LLM 摘要压缩的区别；
- 用一个失败案例说明怎样结合 events.jsonl 和 Trace 定位问题。

### Day 5：Multi-Agent、Skills 与 MCP

按顺序阅读：

1. `src/jarvis/core/skills/loader.py`
2. `src/jarvis/core/skills/builtin/orchestrate.md`
3. `src/jarvis/core/agents/loader.py`
4. `src/jarvis/core/agents/builtin/planner.toml`
5. `src/jarvis/core/agents/builtin/executor.toml`
6. `src/jarvis/core/agents/builtin/reviewer.toml`
7. `src/jarvis/core/subagent/registry.py`
8. `src/jarvis/core/subagent/tool.py`
9. `src/jarvis/core/mcp/client.py`
10. `src/jarvis/core/mcp/tool.py`
11. `src/jarvis/core/mcp/server.py`

配套测试：

```powershell
uv run pytest tests/unit/test_skill_loader.py `
  tests/unit/test_agent_profile_loader.py `
  tests/unit/test_spawn_agent_tool.py `
  tests/unit/test_mcp_tool.py -q
```

当天产出：

- 做一张 Skill、Subagent、MCP 的职责对比表；
- 说明子 Agent 怎样使用隔离上下文、角色 Profile 和独立 AgentLoop，怎样桥接事件；
- 说明前台阻塞与后台 `asyncio.Task` 两种执行方式，以及 `agent_result` 如何查询结果；
- 明确 BackgroundTaskRegistry 的生命周期和当前跨 turn 查询限制；
- 明确当前实现没有 Session 级状态机、join barrier、并发上限、超时取消和完成驱动自动投递。

### Day 6：BFCL 评测与参数治理

进入 `codex/bfcl-reproducible-eval` 对应的 worktree：

```powershell
git worktree list
# 找到标记为 [codex/bfcl-reproducible-eval] 的目录后进入该目录
```

如果该分支没有挂载在其他 worktree，才直接执行
`git switch codex/bfcl-reproducible-eval`。

按顺序阅读：

1. `repro/bfcl/README.md`
2. `repro/bfcl/BFCL_OFFICIAL.md`
3. `src/jarvis/core/tools/argument_normalizer.py`
4. `src/jarvis/core/tools/result_budget.py`
5. `src/jarvis/evals/bfcl_data.py`
6. `src/jarvis/evals/bfcl_scorer.py`
7. `src/jarvis/evals/bfcl_reporting.py`
8. `src/jarvis/evals/bfcl_runner.py`：重点看参数解析和单 case 执行入口
9. `repro/bfcl/reports/jarvis-responses-luna-static-normalizer-v1.json`：只读顶部摘要、run identity、category summary 和 harness summary

配套测试：

```powershell
uv run pytest tests/unit/test_tool_argument_normalizer.py `
  tests/unit/test_tool_result_budget.py `
  tests/evals/test_bfcl_data.py `
  tests/evals/test_bfcl_scorer.py `
  tests/evals/test_bfcl_reporting.py `
  tests/evals/test_bfcl_runner.py -q
```

当天产出：

- 能准确解释 accuracy、coverage、attempt_accuracy；
- 记住 3,641 / 3,638 / 79.16% / 99.92% 四个数字的含义；
- 能说明 BFCL 分数是模型与 Agent Harness 的联合表现，不是纯模型排行榜成绩；
- 能说明 ToolArgumentNormalizer 改了什么、为什么必须保留审计记录。
- 准备 Eval Bullet 的 60 秒回答：数据从哪里来、怎样运行、怎样评分、结果怎样审计。

### Day 7：简历证据映射和模拟面试

复读这些关键位置：

1. Tool：`src/jarvis/core/tools/invocation.py`
2. Context：`src/jarvis/core/context.py`、`src/jarvis/core/runner.py`
3. Memory：`src/jarvis/core/session/store.py`、`src/jarvis/core/tools/builtin/note_save.py`
4. Loop：`src/jarvis/core/loop.py`
5. Safety：`src/jarvis/core/permissions/manager.py`
6. Trace：`src/jarvis/core/events/writer.py`、`src/jarvis/core/trace/provider.py`
7. Multi-Agent：`src/jarvis/core/subagent/registry.py`、`src/jarvis/core/subagent/tool.py`
8. Eval：可复现分支的 `repro/bfcl/README.md`

当天不再横向扩展新文件，只做五件事：

1. 建立八行证据表，列出“简历 Bullet、源码证据、测试证据、实现边界”；
2. 准备 30 秒、3 分钟、10 分钟三版项目介绍；
3. 完成两轮模拟追问，每个回答都落到具体类、函数和设计取舍；
4. 演示一次 TUI 请求、权限审批、工具调用和 Trace 定位。
5. 对照当前简历删除无法由代码证明的 Multi-Agent 能力，并补上缺失的 Safety Bullet。

Day 7 结束标准：

- 能从白板画出完整架构；
- 能解释至少三个设计取舍和三个现有缺陷；
- 能现场定位一个主链文件并解释代码；
- 能分别用 60 秒讲清 Tool、Context、Memory、Loop、Safety、Trace、Eval 和 Multi-Agent；
- 不使用当前代码没有实现的“join barrier”“Session 级子 Agent 状态机”等表述。

如果只有一天，优先读：

```text
commands/events
  -> socket_client/socket_server
  -> CoreApp handlers
  -> SessionManager.send_message
  -> AgentRunner.run_and_capture
  -> ExecutionContext
  -> AgentLoop
  -> provider
  -> invoke_tool
  -> PermissionManager
```

## 五、建议使用的断点或日志位置

不要到处加 `print`，优先使用现有 trace 和测试。确实需要调试时，只放这些位置：

1. `SocketClient.send_command()`：看 method、request id。
2. `SocketServer._handle_line()`：看 RPC 路由。
3. `CoreApp._session_send_handler()`：看传输层到领域层的边界。
4. `SessionManager.send_message()`：看 session/run 状态转换。
5. `AgentRunner.run_and_capture()`：看依赖装配。
6. `AgentLoop.run()`：看每一步 stop reason 和消息变化。
7. `invoke_tool()`：看校验、权限、重试结果。
8. `IpcEventBroadcaster.handle()`：看事件回传。
9. `JarvisTuiApp._handle_event_inner()`：看事件如何变成 UI。

## 六、面试中值得主动讲的设计取舍

### 1. 为什么是 daemon + client

- Core 是唯一执行主体；
- CLI/TUI 共用协议；
- 长任务不会和某个前端生命周期强绑定；
- 事件订阅、回放、权限响应可以复用同一 IPC。

代价是协议、连接生命周期和错误处理更复杂。

### 2. 为什么 AgentLoop 不直接打印

AgentLoop 只发布结构化事件，因此同一执行过程可以同时：

- 写入 `events.jsonl`；
- 推送给 TUI/CLI；
- 写 trace；
- 被测试捕获。

### 3. 为什么工具失败要回填模型

工具失败也是观察结果。把结构化错误作为 `tool_result` 交还模型，模型才有机会换路径，而不是整个 run 立即崩溃。

### 4. 为什么统一成 BaseTool

内置工具、任务工具、Subagent 和 MCP 最终都进入 ToolRegistry，AgentLoop 无需知道具体实现来源。

### 5. 为什么 session 和 run 分开

session 保存多轮连续状态；run 是一次可审计、可回放、可计步的执行单元。二者分离后，历史、事件和权限边界更清楚。

## 七、当前实现中可讨论的改进点

这些不是否定项目，而是面试时很好的“我理解了现状，也知道下一步”的材料：

1. **EventBus 顺序 await 所有订阅者。** 慢订阅者可能拖慢 Agent；可考虑每个订阅者队列或并发派发，并明确背压策略。
2. **Session 只在内存索引新建实例。** 虽然 meta/thread 已落盘，但 daemon 重启后没有恢复 SessionManager 内存索引；可增加启动扫描和恢复。
3. **Provider 抽象仍泄漏 Anthropic 消息格式。** Context、Store 和 Tool schema 都是 Anthropic-style；若要接更多协议，应引入内部统一消息模型和 provider adapter。
4. **Auto 权限模式直接绕过所有静态规则。** 这符合“完全自动”目标，但需要在 UI 中明确风险，或增加 workspace-only auto 等中间模式。
5. **后台 Subagent registry 实际绑定 AgentRunner 实例。** Core 每次消息会创建新 Runner，因此后台任务结果不适合跨 session turn 查询；可把 registry 提升到 CoreApp 或 SessionManager 生命周期。
6. **尚未实现自动长期偏好记忆。** Global/Project Context 由用户手动维护；若要称为长期偏好记忆，需要补充记忆提取、作用域、去重、更新、删除和用户审计机制。
7. **后台 Subagent 没有 join barrier。** 主 Agent 可以在后台任务完成前结束；后续可增加任务重要性标记、并发上限、超时取消和结束前等待策略。
8. **TUI 文件过大。** 网络连接、事件归约、权限控件和渲染都集中在一个文件；后续可按 state/controller/widgets 拆分。
9. **`session.send_message` RPC 会等完整 run 结束才返回。** 事件仍能实时推送，权限响应也能并发处理，但可以考虑改成快速返回 run_id，让命令语义更一致。

## 八、最终自测问题

面试前确认自己能不看代码回答：

1. **架构：** 一条 TUI 消息从输入到最终回答经过哪些模块？RPC 响应和事件推送如何共用一条 TCP 连接？
2. **Tool：** 一次 tool call 如何完成查找、参数校验、权限审批、执行、重试和结果回填？
3. **Context：** 当前 Run 的系统规则、项目说明、角色指令、消息历史、Notes 和工具结果按什么顺序进入模型？
4. **Memory：** Run、Session、Global/Project 三层分别保存什么，存在哪里，生命周期多长？
5. **Memory：** 项目上下文是不是 Session Memory？当前是否支持自动学习用户偏好和 daemon 重启后的完整会话恢复？
6. **Loop：** 工具失败后为什么 Agent 还能继续？`end_turn`、`tool_use`、`max_tokens` 和 `max_steps` 分别怎样处理？
7. **Safety：** Ask、Always、Auto 的优先级、持久化范围和风险是什么？目录越界怎样检测？
8. **Trace：** `events.jsonl`、Trace 和普通日志分别解决什么问题？怎样定位一次工具调用失败？
9. **Eval：** 3,641、3,638、79.16% 和 99.92% 分别代表什么？如何复现实验？
10. **Multi-Agent：** 前台与后台子 Agent 怎样执行？角色 Profile、事件桥接、registry 和 `agent_result` 分别负责什么？
11. **Multi-Agent：** 当前为什么不能声称有 join barrier、Session 级状态机、并发上限和完成驱动投递？
12. **Provider：** DeepSeek 如何接入，哪些内部模型仍依赖 Anthropic 消息格式？
13. **扩展性：** Skill、Subagent、MCP 的相同点和不同点是什么？
14. **改进：** 如果继续重构，你会优先补会话恢复、长期记忆、Subagent 协调还是 EventBus 背压？为什么？
